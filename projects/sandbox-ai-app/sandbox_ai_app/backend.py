"""What sandbox-ai-app knows about sandbox-ai, without any Qt: where it is installed, its options,
running sandboxes, sessions, logs, MCP servers, checks.

Reading is done with sandbox-ai's own modules (loaded from its installation, never copied);
every action runs the sandbox-ai command itself, so the app can do nothing the command line
cannot, and the command line's checks apply to everything the app does.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse

from .files import printable

# Fallbacks only, for the moments before an installation is loaded; SandboxAI reads the real values from
# sandbox-ai's own modules (the launcher's parser, approvals.ID, secret_store.NAME and LIMIT, ...).
AGENTS = ("claude", "codex", "opencode")
MIN_VERSION = "0.1.21"  # the first release with approvals, secrets, images and log telemetry
# What the app uses of sandbox-ai: a development checkout may have it before its version number says so.
REQUIRED = {"approvals": ("questions", "answer", "describe", "ID"), "secret_store": ("store", "FileStore", "NAME"),
            "images": ("images", "packages"), "logs": ("telemetry", "tool_calls"), "net": ("running",),
            "sessions": ("rows", "load", "session_dir")}
TERMINALS = (  # (program, arguments before the command)
    ("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]), ("kitty", []),
    ("alacritty", ["-e"]), ("wezterm", ["start", "--"]), ("foot", []), ("xfce4-terminal", ["-x"]),
    ("tilix", ["-e"]), ("xterm", ["-e"]),
)
ONE_ARGUMENT_TERMINALS = {"tilix"}
# Option groups of the launcher, in the order the app shows them.
GROUPS = {
    "Workspace": ["--workspace", "--workspace-ro", "--mount", "--state-dir", "--copy", "--name", "--project-name",
                  "--keep", "--no-keep"],
    "Network": ["--ask", "--profile", "--allow-host", "--allow-http", "--allow-pypi", "--allow-github", "--allow-gitlab",
                "--allow-kernel-org", "--allow-duckduckgo", "--allow-npm", "--allow-tcp", "--allow", "--allow-host-port",
                "--frontier", "--no-provider-defaults", "--dns", "--nameserver", "--egress-all-allow", "--offline",
                "--network", "--ip", "--ip6", "--subnet", "--subnet6", "--vpn", "--vpn-check", "--llm"],
    "Proxy & privacy": ["--mode", "--mode-at-least", "--no-proxy", "--no-inspect", "--rules", "--privacy-check", "--privacy-rules",
                        "--canaries", "--no-canaries", "--telemetry", "--log-dir", "--log-debug"],
    "Git": ["--identity", "--git-identity", "--git-token-env", "--git-token-file", "--git-token-secret", "--git-logins",
            "--token-proxy", "--git-credentials"],
    "Agent": ["--model", "--effort", "--preset", "--preset-value", "--dangerously-skip-permissions", "--mcp", "--helper", "--with", "--kit", "--kit-dir", "--no-default-kit",
              "--no-memory", "--no-plugins", "--local-config", "--env", "--secret-env", "--api-key", "--publish"],
    "Login": ["--copy-auth", "--no-auth", "--auth-file", "--import-codex-auth"],
    "Display & terminal": ["--gui", "--gui-vnc", "--gui-screen", "--gui-wayland", "--audio", "--tmux", "--no-tmux"],
    "Hardware": ["--device", "--gpu"],
    "Limits": ["--memory", "--cpus", "--pids"],
    "Engine": ["--backend", "--image", "--image-variant", "--podman-url", "--binary", "--runtime", "--config", "--no-config", "--probe"],
}
PATH_OPTIONS = {"--workspace", "--state-dir", "--privacy-rules", "--rules", "--log-dir", "--git-credentials", "--local-config",
                "--auth-file", "--import-codex-auth", "--gui-vnc", "--kit-dir", "--config", "--runtime", "--device"}
# Options the app sets itself (it starts sessions and dry runs on its own).
HIDDEN = {"--session", "--dry-run", "--help", "--version"}
SCOPES = ("once", "run", "always")
REMOTE_QUESTION_WAIT = 30  # seconds a host may take to list its open questions (over SSH)
NET_OPTIONS_SECONDS = 600  # how long a host's `net allow --help` is kept when its sandbox-ai version is not known
BASE_NET_OPTIONS = frozenset({"--allow-host", "--allow-tcp", "--allow"})  # what every sandbox-ai since 0.1.70 takes
RECENT_PROJECTS = 12  # projects the app's own list keeps
RECENT_FOLDERS = 10  # recent project folders the wizard offers
KIT_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"  # kits.KIT_NAME
SKILL_NAME = r"[a-z0-9][a-z0-9-]{0,63}"  # a new skill's name, as Claude wants it
DRAIN = 2  # seconds call() still reads the output of a command it ended at its timeout
KIT_ACTIONS = {"show", "new", "add-skill", "new-skill", "add-plugin", "add-marketplace", "remove-skill",
               "remove-plugin", "add-hook", "remove-hook", "delete"}


class Unavailable(RuntimeError):
    """sandbox-ai is not installed where the app can find it."""


def locate(which=shutil.which, run=subprocess.run, forbidden=None):
    """The directory with sandbox-ai's modules (agent_sandbox.py and main.py).

    With $SANDBOX_AI_HOME set, only there: the sandbox-ai command on $PATH is not even looked up. Otherwise
    the installation behind the sandbox-ai command (install.sh: its `current` link is kept, not resolved,
    so an update is picked up at the next start; pip or pipx: the package as the command's own Python finds
    it), or the package sandbox_ai importable here. Before the command's Python is run to find its package,
    the command and that interpreter are checked against FORBIDDEN (default: preload_trees(), the folders
    sandboxes can or could write): one lying there is refused (UnsafeInstallation), never run."""
    if os.environ.get("SANDBOX_AI_HOME"):
        home = Path(os.environ["SANDBOX_AI_HOME"]).expanduser()
        for candidate in (home / "sandbox", home):
            if (candidate / "agent_sandbox.py").is_file() and (candidate / "main.py").is_file():
                return candidate
        raise Unavailable(f"SANDBOX_AI_HOME={home} holds no sandbox-ai (agent_sandbox.py and main.py, there or "
                          "in its sandbox/ folder)")
    candidates = []
    command = which("sandbox-ai")
    if command:
        interpreter = script_interpreter(command)
        try:
            check_outside([command] + ([interpreter] if interpreter else []),
                          preload_trees() if forbidden is None else forbidden)
        except InsideSandbox as exc:
            raise UnsafeInstallation(f"The sandbox-ai command found on PATH, {command}, was not run: {exc.path} "
                                     f"lies in {exc.root}, which a sandbox can write or could write in an earlier "
                                     "run. Install sandbox-ai outside sandbox folders, or set SANDBOX_AI_HOME to a "
                                     "checkout you trust.") from None
        path = Path(command)
        for _ in range(8):  # one link at a time: install.sh links bin/sandbox-ai to DATA/current/sandbox-ai
            candidates += [path.parent / "sandbox", path.parent]
            if not path.is_symlink():
                break
            path = Path(os.path.normpath(path.parent / os.readlink(path)))
        candidates += [path.parent / "sandbox", path.parent]
        if interpreter:
            found = package_of(interpreter, run)
            if found:
                candidates.append(Path(found))
    spec = importlib.util.find_spec("sandbox_ai")
    if spec and spec.submodule_search_locations:
        candidates += [Path(p) for p in spec.submodule_search_locations]
    for candidate in candidates:
        if (candidate / "agent_sandbox.py").is_file() and (candidate / "main.py").is_file():
            return candidate
    raise Unavailable("sandbox-ai was not found: install it (install.sh, pip or pipx) or set SANDBOX_AI_HOME")


def script_interpreter(command):
    """The Python of a console script (pip, pipx: '#!/path/to/venv/bin/python'), or None."""
    try:
        with open(command, "rb") as stream:
            first = stream.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    words = first[2:].decode("utf-8", "replace").split()
    if words and Path(words[0]).name.startswith("python") and Path(words[0]).is_file():
        return words[0]
    return None


def package_of(interpreter, run=subprocess.run):
    """Where INTERPRETER finds the package sandbox_ai (a pipx or other virtual environment), or None."""
    script = ("import importlib.util; s = importlib.util.find_spec('sandbox_ai'); "
              "print(list(s.submodule_search_locations)[0] if s and s.submodule_search_locations else '')")
    try:
        result = run([interpreter, "-I", "-c", script], capture_output=True, text=True, timeout=20,
                     stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    found = result.stdout.strip()
    return found if result.returncode == 0 and found else None


class UnsafeInstallation(Unavailable):
    """The sandbox-ai found lies in a folder a sandbox can write: not run."""


class TooOld(RuntimeError):
    """The installed sandbox-ai lacks what the app needs."""


class SandboxAI:
    """One sandbox-ai installation."""

    def __init__(self, directory=None, runner=subprocess.run):
        self.directory = Path(directory) if directory else locate()
        self.run = runner
        self._modules = {}
        self._modules_lock = threading.RLock()  # pages load modules from several threads at once
        self._processes, self._signal_lock = set(), threading.RLock()  # commands of call() not yet reaped
        self._questions_lock = threading.Lock()  # the window scans in a thread and, after an answer, directly

    # -- modules and commands --

    def module(self, name):
        """sandbox-ai's module NAME, loaded once (check and load under one lock: background calls of several
        pages may ask for the same module at the same moment)."""
        with self._modules_lock:
            if name not in self._modules:
                spec = importlib.util.spec_from_file_location(f"sandbox_ai_app_{name}", self.directory / f"{name}.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if name == "presets" and hasattr(module, "EXPOSED_BEFORE"):
                    # Extra preset directories where sandboxes wrote are skipped: the launcher knows where.
                    module.EXPOSED_BEFORE = self.launcher.exposed_before
                self._modules[name] = module
            return self._modules[name]

    @property
    def launcher(self):
        return self.module("agent_sandbox")

    def command(self, *args):
        """argv of a sandbox-ai command, run with this Python like the installed entry points."""
        return [sys.executable, "-I", str(self.directory / "main.py"), *args]

    def call(self, *args, timeout=120, input=None):
        """(exit status, stdout, stderr) of a sandbox-ai command that does not need a terminal; INPUT, if
        given, is its standard input (how secrets are passed: never as an argument). The command leads a
        process group of its own, so stop_commands() (when the app quits) and a timeout end it with all it
        started. Its lead is reaped only under the signal lock: while a group is signalled, its number cannot
        have been given to another group."""
        if self.run is not subprocess.run:  # a runner given for tests
            streams = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
            try:
                result = self.run(self.command(*args), capture_output=True, text=True, timeout=timeout, **streams)
            except subprocess.TimeoutExpired:
                return 124, "", f"sandbox-ai {' '.join(args[:2])}: no answer within {timeout} s"
            return result.returncode, result.stdout, result.stderr
        try:
            process = subprocess.Popen(self.command(*args), stdin=subprocess.PIPE if input is not None else
                                       subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
        except OSError as exc:
            return 127, "", f"sandbox-ai {' '.join(args[:2])}: {exc}"
        with self._signal_lock:
            self._processes.add(process)
        # Its output is read in this thread (no reader threads: closing a pipe under a blocked read() never
        # returns, and a descendant that left the group - a daemon, ssh, dbus - may keep the pipes open).
        pipes = Pipes(process, None if input is None else str(input).encode())
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            if not pipes.pump(deadline):  # still open past the deadline: end the group, take what is left
                timed_out = True
                self.end_groups([process])
                pipes.pump(time.monotonic() + DRAIN)
        finally:
            pipes.close()
        while True:
            with self._signal_lock:
                if lead_exited(process):
                    process.wait()
                    self._processes.discard(process)
                    break
            if not timed_out and time.monotonic() > deadline:  # it closed its output but goes on
                timed_out = True
                self.end_groups([process])
            time.sleep(0.02)
        text = pipes.text()
        if timed_out:
            return 124, text[0], text[1] + f"sandbox-ai {' '.join(args[:2])}: no answer within {timeout} s"
        return process.returncode, text[0], text[1]

    def end_groups(self, processes, grace=3):
        """End the process groups PROCESSES lead: SIGTERM to all, one wait of at most GRACE seconds while
        any of them has a process left, then SIGKILL to every group - also where the lead has exited (its
        children may live on). Only groups whose lead is still unreaped are signalled (a zombie keeps the
        number), and nothing is reaped meanwhile (the lock). The number of groups signalled."""
        with self._signal_lock:
            live = [process for process in processes if process in self._processes and process.returncode is None]
            for process in live:
                signal_group(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and any(group_alive(process.pid) for process in live):
                time.sleep(0.05)
            for process in live:
                signal_group(process.pid, signal.SIGKILL)
        return len(live)

    def stop_commands(self, grace=3):
        """End every command of call() that still runs, with the processes it started, within GRACE seconds
        (see end_groups); the number ended."""
        with self._signal_lock:
            running = list(self._processes)
        return self.end_groups(running, grace)

    def version(self):
        return self.launcher.__version__

    def check_version(self):
        """None if this sandbox-ai has what the app needs, else a message (for an old installation)."""
        try:
            version = self.version()
        except Exception as exc:  # noqa: BLE001 - a launcher that does not even load
            return f"sandbox-ai in {self.directory} cannot be loaded: {exc}"
        missing = []
        for name, attributes in REQUIRED.items():
            if not (self.directory / f"{name}.py").is_file():
                missing.append(f"{name}.py")
                continue
            try:
                module = self.module(name)
            except Exception:  # noqa: BLE001
                missing.append(f"{name}.py")
                continue
            missing += [f"{name}.{attribute}" for attribute in attributes if not hasattr(module, attribute)]
        if missing and not newer(version, MIN_VERSION) and version != MIN_VERSION:
            return (f"The app needs sandbox-ai {MIN_VERSION} or newer; found {version} in {self.directory} "
                    f"(missing: {', '.join(missing[:6])}). Update it with: sandbox-ai update")
        if missing:
            return f"sandbox-ai {version} in {self.directory} lacks {', '.join(missing[:6])}"
        return None

    # -- what sandbox-ai itself defines --

    def parser_actions(self):
        """The launcher parser's actions, built once per launcher (building it takes about 10 ms, and the window
        asked for it eight times while it was built)."""
        launcher = self.launcher
        cached = getattr(self, "_parser_actions", None)
        if cached is None or cached[0] is not launcher:
            cached = self._parser_actions = (launcher, tuple(launcher.build_parser()._actions))
        return cached[1]

    @property
    def agents(self):
        for action in self.parser_actions():
            if action.dest == "agent" and action.choices:
                return tuple(action.choices)
        return AGENTS

    @property
    def question_id(self):
        return self.module("approvals").ID

    @property
    def secret_name(self):
        return self.module("secret_store").NAME

    @property
    def secret_limit(self):
        return self.module("secret_store").LIMIT

    @property
    def approval_seconds(self):
        """runtime.APPROVAL_SECONDS, read from the file (importing the runtime takes most of a second)."""
        if not hasattr(self, "_approval_seconds"):
            try:
                found = re.search(r"^APPROVAL_SECONDS = ([0-9]+)", (self.directory / "runtime.py").read_text(), re.M)
            except OSError:
                found = None
            self._approval_seconds = int(found[1]) if found else 60
        return self._approval_seconds

    # -- options --

    def options(self):
        """[Option] of the launcher, from its own parser: whatever the launcher offers, the app offers."""
        result = []
        for action in self.parser_actions():
            if not action.option_strings or action.help == argparse.SUPPRESS:
                continue
            long = next((o for o in action.option_strings if o.startswith("--")), None)
            if long is None or long in HIDDEN:
                continue
            short = next((o for o in action.option_strings if not o.startswith("--")), "")
            result.append(Option.from_action(action, long, short))
        return result

    def presets(self):
        return dict(self.launcher.PRESETS)

    # -- state --

    def runs(self):
        """{run: record} of the sandboxes running now (the registry of `sandbox-ai net`)."""
        return self.module("net").running()

    def sessions(self):
        """[[name, agent, backend, status, workspace, created]] of the named sessions."""
        return self.module("sessions").rows()

    def session_ids(self):
        """{name: id} of the named sessions: the id is the session's directory, which stays when it is renamed."""
        sessions = self.module("sessions")
        if not hasattr(sessions, "display") or not hasattr(sessions, "root"):
            return {}
        found, root = {}, sessions.root()
        if not root.is_dir():
            return found
        for directory in root.iterdir():
            try:
                with open(directory / "session.json", "rb") as stream:
                    data = json.loads(stream.read(1 << 20))
            except (OSError, ValueError):
                continue
            if valid_run(directory.name):
                found[sessions.display(directory.name, data)] = directory.name
        return found

    def mcp_servers(self):
        """[(name, status, what)] of the defined MCP servers."""
        mcp = self.module("mcp")
        return [(name, mcp.status(name), d["image"] if "image" in d else " ".join(d.get("command", [])))
                for name, d in sorted(mcp.definitions().items())]

    def mcp_serving(self):
        """{server: [start names]}: the agent starts that reach each MCP server now or at their next start ({} for
        a sandbox-ai that cannot attach servers to starts)."""
        mcp = self.module("mcp")
        return mcp.serving() if hasattr(mcp, "serving") else {}

    def mcp_attach_supported(self):
        return hasattr(self.module("mcp"), "attach")

    def mcp_attach_targets(self):
        """[{"name", "state", "agent", "mcp"}] of the agent starts an MCP server can be attached to: the kept
        ones (a run without a kept start keeps nothing to start again with)."""
        starts = self.module("starts").entries(True, prune=False)
        return [entry for entry in starts if entry.get("kind") != "mcp" and entry.get("directory")
                and entry.get("state") != "damaged"]

    def mcp_attach(self, name, start, restart=False):
        """The result of `sandbox-ai mcp attach NAME START --json [--restart]` ({"attached", "message", ...})."""
        status, out, err = self.call("mcp", "attach", name, start, "--json", *(["--restart"] if restart else []),
                                     timeout=600)
        try:
            return json.loads(out)
        except ValueError:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def mcp_definitions(self):
        """{name: definition} of the MCP servers this user defined (mcp.json) and the built-in ones."""
        return self.module("mcp").definitions()

    def mcp_catalog(self):
        """([(prefix, entry)], problems) of the MCP catalogs."""
        return self.module("mcp").catalog_entries()

    def doctor(self, backend="podman"):
        return self.module("doctor").checks(backend)

    def listing(self, *command):
        """[dict] of `sandbox-ai COMMAND --json` (a list of objects), [] when this sandbox-ai cannot say."""
        status, out, _ = self.call(*command, "--json", timeout=60)
        if status != 0:
            return []
        try:
            data = json.loads(out)
        except ValueError:
            return []
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []

    def vpn_profiles(self):
        """[{profile, type, provider, ...}] of `sandbox-ai vpn list --json` (no keys: the profiles name them)."""
        return [e for e in self.listing("vpn", "list") if isinstance(e.get("profile"), str)]

    def git_logins(self):
        """[{url, user, secret}] of `sandbox-ai git-logins list --json` (secret names, never tokens)."""
        return [e for e in self.listing("git-logins", "list") if isinstance(e.get("url"), str)]

    def publish_logins(self):
        """[{kind, host, secret}] of `sandbox-ai publish-logins list --json` (secret names, never tokens)."""
        return [e for e in self.listing("publish-logins", "list") if isinstance(e.get("kind"), str)]

    # -- identities (sandbox-ai identities) --

    def has_identities(self):
        return (self.directory / "identities.py").is_file()

    def identities(self):
        """{NAME: identity} of `sandbox-ai identities list --json` (names, addresses, servers, secret names; no
        token), {} when this sandbox-ai has none or cannot say."""
        status, out, _ = self.call("identities", "list", "--json", timeout=60)
        if status != 0:
            return {}
        try:
            data = json.loads(out)
        except ValueError:
            return {}
        return {name: entry for name, entry in data.items() if isinstance(entry, dict)} if isinstance(data, dict) else {}

    # The server of each kind of publish login when no own one is named (sandbox-ai publish_logins.KINDS).
    PUBLISH_HOSTS = {"pypi": "upload.pypi.org", "testpypi": "test.pypi.org", "npm": "registry.npmjs.org",
                     "crates": "crates.io", "gitlab": "gitlab.com", "github": "api.github.com"}

    @staticmethod
    def identity_steps(name, old, new):
        """The `sandbox-ai identities` argument lists that turn identity NAME from OLD (None: not there yet) into
        NEW ({name, email, description, git: [{url, user, secret}], publish: [{kind, host, secret, url?}]}):
        add or set, then the logins that went away, then the new or changed ones. Every value is one argument, never
        read as an option (the command line checks each again)."""
        steps = []
        fields = []
        for key in ("name", "email", "description"):
            value = (new.get(key) or "").strip()
            if old is None and not value:
                continue
            if old is not None and value == (old.get(key) or ""):
                continue
            fields += [f"--{key}={value}"]
        if old is None:
            steps.append(["identities", "add", name, *fields])
        elif fields:
            steps.append(["identities", "set", name, *fields])
        old_git = {e["url"]: e for e in (old or {}).get("git", [])}
        new_git = {e["url"]: e for e in new.get("git", []) if e.get("url")}
        for url in old_git.keys() - new_git.keys():
            steps.append(["identities", "logout", name, url])
        for url, entry in new_git.items():
            if old_git.get(url) != {"url": url, "user": entry.get("user") or "x-access-token", "secret": entry["secret"]}:
                steps.append(["identities", "login", name, url, f"--secret={entry['secret']}",
                              f"--user={entry.get('user') or 'x-access-token'}"])
        old_pub = {e["kind"]: e for e in (old or {}).get("publish", [])}
        new_pub = {e["kind"]: e for e in new.get("publish", []) if e.get("kind")}
        for kind in old_pub.keys() - new_pub.keys():
            steps.append(["identities", "unpublish", name, kind])
        for kind, entry in new_pub.items():
            before = old_pub.get(kind)
            host = urllib.parse.urlsplit(entry["url"]).hostname if entry.get("url") else SandboxAI.PUBLISH_HOSTS.get(kind)
            if before is None or before.get("secret") != entry["secret"] or before.get("host") != host:
                steps.append(["identities", "publish", name, kind, f"--secret={entry['secret']}",
                              *([f"--url={entry['url']}"] if entry.get("url") else [])])
        return steps

    def save_identity(self, name, old, new):
        """Run identity_steps one after the other; (status, output) of the first that failed, or (0, output)."""
        said = []
        for step in self.identity_steps(name, old, new):
            status, out, err = self.call(*step, timeout=60)
            said.append((out + err).strip())
            if status != 0:
                return status, "\n".join(said)
        return 0, "\n".join(part for part in said if part)

    VPN_KINDS = ("wireguard", "openvpn", "mullvad", "tailscale")

    def vpn_add_argv(self, name, kind, file=None, secret=None, location=None, exit_node=None, login_server=None):
        """The arguments of `sandbox-ai vpn add` for a profile, every value checked (none of them can be read as an
        option); keys and passwords are not among them: a SECRET names where sandbox-ai keeps one."""
        if not (isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name)):
            raise ValueError("a profile name is 1-32 lower-case letters, digits, '_' or '-'")
        if kind not in self.VPN_KINDS:
            raise ValueError("the kind is wireguard, openvpn, mullvad or tailscale")
        args = ["vpn", "add", name]
        if kind in ("wireguard", "openvpn"):
            if not (isinstance(file, str) and os.path.isabs(os.path.expanduser(file))):
                raise ValueError("the configuration is a file with its absolute path")
            args.append(f"--{kind}={os.path.expanduser(file)}")
        else:
            args.append(f"--{kind}")
        if secret:
            if not self.valid_secret(secret):
                raise ValueError("a secret's name is letters, digits and '_'")
            option = {"openvpn": "--auth-secret", "mullvad": "--account-secret", "tailscale": "--auth-key-secret"}.get(kind)
            if option is None:
                raise ValueError("a WireGuard configuration holds its own key: no secret")
            args.append(f"{option}={secret}")
        for value, option, kinds, pattern in ((location, "--location", ("mullvad",), r"[a-z]{2}(?:-[a-z0-9-]{1,20})?"),
                                              (exit_node, "--exit-node", ("tailscale",), r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}"),
                                              (login_server, "--login-server", ("tailscale",), r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?")):
            if value:
                if kind not in kinds or not re.fullmatch(pattern, value):
                    raise ValueError(f"{option} does not fit this profile: {value!r}")
                args.append(f"{option}={value}")
        return args

    def vpn_add(self, name, kind, secret_value=None, **fields):
        """Add a VPN profile; SECRET_VALUE (a key or password typed in the app) is first kept as the secret
        fields["secret"] with `sandbox-ai secrets set` (standard input), never passed on a command line."""
        args = self.vpn_add_argv(name, kind, **fields)
        if secret_value:
            if not fields.get("secret"):
                raise ValueError("a typed key or password needs the name of the secret to keep it as")
            status, out, err = self.set_secret(fields["secret"], secret_value)
            if status != 0:
                return status, out, err
        return self.call(*args, timeout=180)

    def vpn_remove(self, name):
        return self.call("vpn", "remove", name, timeout=60)

    def vpn_check(self, name):
        return self.call("vpn", "check", name, timeout=180)

    def llm_endpoints(self):
        """[{name, kind, url, vpn, model, ...}] of `sandbox-ai llm list --json` (keys stay in sandbox-ai secrets)."""
        return [e for e in self.listing("llm", "list") if isinstance(e.get("name"), str)]

    # -- approvals --

    def questions(self, answered=False, runs=None):
        """[(run, record, question)] of the running sandboxes (open ones unless ANSWERED)."""
        return self.module("approvals").questions(self.runs() if runs is None else runs, answered=answered)

    def describe(self, question):
        return printable(self.module("approvals").describe(question))

    def approvals_snapshot(self, runs=None, wait=None):
        """(open, answered) [(run, record, question)] of every running sandbox, read through a cache: a
        question file is parsed once (it never changes after it was written), its .answer and .done
        are looked up in the folder listing; only answered ones carry "result". WAIT: seconds to wait at
        most for a scan running in another thread (a stuck one must not block the window): TimeoutError."""
        if not self._questions_lock.acquire(timeout=-1 if wait is None else wait):
            raise TimeoutError("the questions are being read already")
        try:
            if not hasattr(self, "_questions"):
                self._questions = QuestionCache(self.question_id)
            return self._questions.scan(self.runs() if runs is None else runs)
        finally:
            self._questions_lock.release()

    def answer(self, ident, decision, scope="once"):
        """Answer question IDENT through `sandbox-ai approvals allow|deny --scope SCOPE`: deny "always" blocks
        it for the project (refused at every later start without a question). Scopes wider than the
        administrator's max_approval_scope are refused here already (sandbox-ai refuses them too)."""
        if not self.question_id.fullmatch(str(ident)) or decision not in ("allow", "deny") or scope not in SCOPES:
            return 2, "", "invalid question or answer"
        # The policy caps allows only (as sandbox-ai does): a deny, also Block always, is never refused for its scope.
        if decision == "allow" and SCOPES.index(scope) > SCOPES.index(self.max_scope()):
            return 2, "", f"the administrator's policy allows answers up to {self.max_scope()!r}"
        return self.call("approvals", decision, ident, "--scope", scope, timeout=60)

    # -- administrator policy --

    def policy(self):
        """(policy, problem, text, path) of the administrator's policy (/etc/sandbox-ai/policy.json): the
        policy as the launcher reads it ({} without one), why it stops every start (None if it does not),
        the file as it is (for display; None without one)."""
        launcher = self.launcher
        path = Path(getattr(launcher, "POLICY_FILE", "/etc/sandbox-ai/policy.json"))
        try:
            text = path.read_text(errors="replace")[:65536]
        except FileNotFoundError:
            text = None
        except OSError as exc:
            text = f"({exc.strerror})"
        if not hasattr(launcher, "load_policy"):
            return {}, None, text, path
        try:
            return launcher.load_policy(), None, text, path
        except ValueError as exc:
            return {}, str(exc), text, path

    def forbidden(self):
        """{option: forbidden value or None (any use)} of the policy's "forbid" list."""
        found = {}
        for entry in self.policy()[0].get("forbid", []):
            option, equals, value = str(entry).partition("=")
            found.setdefault(option, set()).add(value if equals else None)
        return found

    def max_scope(self):
        """The widest answer the policy allows: "once", "run" or "always"."""
        scope = self.policy()[0].get("max_approval_scope", "always")
        return scope if scope in SCOPES else "always"

    def remembered(self):
        """[(project, entry, kind)] of what was allowed or blocked "always" (approvals.remembered: read from
        the file, valid entries only); kind: "host", "tcp", "blocked-host" or "blocked-tcp". Older sandbox-ai
        versions lack it: AttributeError."""
        return remembered_rows(self.module("approvals").remembered())

    def host_remembered(self, name):
        """remembered() of host NAME: `sandbox-ai approvals remembered --json` there, over SSH. ValueError: no
        answer (or a sandbox-ai there too old to list them)."""
        data = self.host_json(name, "approvals", "remembered", "--json", timeout=60)
        if not isinstance(data, dict):
            raise ValueError(f"{name}: not a list of remembered answers")
        return remembered_rows(data)

    def forget_remembered(self, project, entry, host="local"):
        """`sandbox-ai approvals forget PROJECT ENTRY` (on HOST): the project's sandboxes ask again for ENTRY."""
        if not all(isinstance(v, str) and v and "\0" not in v and not v.startswith("-") and len(v) <= 4096
                   for v in (project, entry)):
            return 2, "", "invalid project or entry"
        return self.host_run(host, "approvals", "forget", project, entry, timeout=60)

    def run_log(self, run, record):
        """The newest connections log of the run RUN (record: its run.json), in the default log directory of
        its workspace (a run with --log-dir elsewhere has none here), or None."""
        workspace = record.get("workspace") if isinstance(record, dict) else None
        if not workspace or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(run)):
            return None
        directory = Path(self.launcher.default_log_dir(Path(workspace)))
        try:
            logs = sorted(p for p in directory.glob(f"connections-*-{run}.jsonl") if not p.is_symlink())
        except OSError:
            return None
        return logs[-1] if logs else None

    def question_context(self, run, record, question, recent=5):
        """What is already known about QUESTION, for the human (reads the run's log: call it in a thread).
        Nothing is resolved or fetched: the name in a question is the agent's (it may carry data, e.g.
        <secret>.attacker.example), and looking it up before the human allows it would send it out anyway.
        dns: {"name"}; connect: {"address", "port", "protocol", "host" (named by the agent, not verified)};
        both: "recent", the run's last RECENT log records to that address or name, as summary() rows."""
        kind = question.get("kind")
        context = {"kind": kind}
        if kind == "dns":
            name = str(question.get("name", ""))
            context["name"] = name
            wanted = {name}
        elif kind == "connect":
            for key in ("address", "port", "protocol", "host"):
                context[key] = question.get(key)
            wanted = {str(question.get("destination", ""))} | ({str(question["host"])} if question.get("host") else set())
        else:
            return context
        path = self.run_log(run, record)
        records = read_connections(path) if path else []
        matching = [r for r in records if wanted & {str(r.get("destination", "")), str(r.get("host", "")),
                                                    url_host(r.get("url"))}]
        context["recent"] = [summary(r) for r in matching[-recent:]]
        context["log"] = str(path) if path else None
        return context

    # -- devices and limits of a running sandbox (sandbox-ai devices / limits) --

    def devices_added(self, run):
        """The devices added to the running sandbox RUN during its run (devices.added; not those given at the
        start). Older sandbox-ai versions lack it: AttributeError."""
        record = self.runs().get(run)
        return list(self.module("devices").added(record)) if record else []

    def device_check(self, value):
        """(resolved path, notes) of a device for a running sandbox: the launcher's --device checks and the
        administrator's policy (devices.checked_device); ValueError says why it is refused."""
        return self.module("devices").checked_device(value)

    def device_candidates(self, root=Path("/dev")):
        """[(path, refused: why or None, note or None)] of the devices under ROOT a sandbox could be given:
        character devices and the directories passed whole (bus/usb, dri, snd), each with the launcher's own
        refusal (DEVICE_REFUSED) or warning (DEVICE_WARN)."""
        launcher = self.launcher
        refused = getattr(launcher, "DEVICE_REFUSED", [])
        warned = getattr(launcher, "DEVICE_WARN", [])
        found = []
        names = list(getattr(launcher, "DEVICE_DIRS", ("bus/usb", "dri", "snd")))
        try:
            for entry in sorted(os.scandir(root), key=lambda e: e.name):
                try:
                    if stat.S_ISCHR(entry.stat(follow_symlinks=False).st_mode):
                        names.append(entry.name)
                except OSError:
                    continue
        except OSError:
            pass
        for below in dict.fromkeys(names):
            if not (root / below).exists():
                continue
            why = next((reason for pattern, reason in refused if re.fullmatch(pattern, below)), None)
            note = next((reason for pattern, reason in warned if re.fullmatch(pattern, below)), None)
            found.append((str(root / below), why, note))
        return found

    def add_device(self, run, path, host="local"):
        """`sandbox-ai devices add RUN PATH` (on HOST): the device until the sandbox ends (own process group)."""
        if not valid_run(run) or not valid_device(path):
            return 2, "", "invalid run or device"
        return self.host_run(host, "devices", "add", run, path, timeout=120)

    def remove_device(self, run, path, host="local"):
        """`sandbox-ai devices remove RUN DEVICE` (on HOST): DEVICE a device added during the run, or its id (as
        `devices list --json` gives it)."""
        if not valid_run(run) or not (valid_device(path) or re.fullmatch(r"[0-9]{1,9}", str(path))):
            return 2, "", "invalid run or device"
        return self.host_run(host, "devices", "remove", run, path, timeout=120)

    def host_devices_added(self, host, run):
        """[(id, what)] of the devices added to the running sandbox RUN on HOST during its run and not removed
        (`sandbox-ai devices list RUN --json` there): what a removal is chosen from. ValueError: not answered."""
        if not valid_run(run):
            raise ValueError("invalid run")
        data = self.host_json(host, "devices", "list", run, "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a list of devices")
        found = []
        for entry in data:
            if not isinstance(entry, dict) or entry.get("source") != "added" or entry.get("state") == "removed" or \
                    not re.fullmatch(r"[0-9]{1,9}", str(entry.get("id"))):
                continue
            nodes = [printable(n)[:200] for n in entry.get("nodes") or [] if isinstance(n, str)][:10]
            what = printable(entry.get("value"))[:200] if isinstance(entry.get("value"), str) else "?"
            found.append((str(entry["id"]), f"{what} ({', '.join(nodes)})" if nodes else what))
        return found

    def host_workspaces(self, host, run):
        """The workspaces of the running sandbox RUN on HOST (`sandbox-ai workspace list RUN --json` there), as
        workspaces() gives this machine's: [{host, sandbox, mode, source, note}], the text fields escaped. ValueError:
        not answered."""
        if not valid_run(run):
            raise ValueError("invalid run")
        data = self.host_json(host, "workspace", "list", run, "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a list of workspaces")
        return [{key: printable(entry[key])[:1000] for key in ("host", "sandbox", "mode", "source", "note")
                 if isinstance(entry.get(key), str)} for entry in data if isinstance(entry, dict)]

    # -- workspaces of a running sandbox (sandbox-ai workspace) --

    def workspaces(self, run):
        """The workspaces of the running sandbox RUN: [{host, sandbox, mode, source ("start" or "added"), kept,
        since}] (workspaces.listing). Older sandbox-ai versions lack it: AttributeError (or ImportError)."""
        record = self.runs().get(run)
        return list(self.module("workspaces").listing(record)) if record else []

    def add_workspace(self, run, directory, read_only=False, host="local"):
        """`sandbox-ai workspace add RUN DIR [--ro]`: DIR at /workspaces/NAME until the sandbox ends (kept for a
        restart of a session). The launcher does every check of a start (scope, private places, the policy)."""
        if not valid_run(run) or not valid_directory(directory):
            return 2, "", "invalid run or directory"
        return self.host_run(host, "workspace", "add", run, str(directory), *(["--ro"] if read_only else []),
                             timeout=120)

    def remove_workspace(self, run, value, force=False, host="local"):
        """`sandbox-ai workspace remove RUN DIR|NAME [--force]`: refused while a process in the sandbox uses it,
        unless FORCE (detached lazily)."""
        if not valid_run(run) or not valid_directory(value) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                                                                                  str(value)):
            return 2, "", "invalid run or workspace"
        return self.host_run(host, "workspace", "remove", run, str(value), *(["--force"] if force else []),
                             timeout=120)

    def set_limits(self, run, memory=None, cpus=None, pids=None, host="local"):
        """`sandbox-ai limits RUN --memory/--cpus/--pids` (a value of "none": no limit) for the rest of the run."""
        if not valid_run(run):
            return 2, "", "invalid run"
        argv = []
        for option, value in (("--memory", memory), ("--cpus", cpus), ("--pids", pids)):
            if value not in (None, ""):
                if not re.fullmatch(r"none|[0-9]+(\.[0-9]+)?[kmgtKMGT]?", str(value)):
                    return 2, "", f"invalid value for {option}: {value}"
                argv += [option, str(value)]
        if not argv:
            return 2, "", "nothing to change"
        return self.host_run(host, "limits", run, *argv, timeout=120)

    def run_for(self, workspace):
        """The name of a running sandbox whose main workspace is WORKSPACE, or None."""
        if not workspace:
            return None
        real = os.path.realpath(os.path.expanduser(str(workspace)))
        try:
            runs = self.runs()
        except Exception:  # noqa: BLE001
            return None
        return next((name for name, record in runs.items()
                     if os.path.realpath(str(record.get("workspace", ""))) == real), None)

    # -- every sandbox-ai instance of this account --

    def net_options(self, command="allow"):
        """The option strings `sandbox-ai net COMMAND` takes in this installation (--dns, --allow-host-port and the
        --git-token-* logins came in later versions)."""
        try:
            parser = self.module("net").parser()
        except Exception:  # noqa: BLE001 - an old or damaged installation
            return set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) and command in action.choices:
                return {option for sub in action.choices[command]._actions for option in sub.option_strings}
        return set()

    def net_commands(self):
        """The subcommands of `sandbox-ai net` this installation has (e.g. "stop" since the app's 0.2 era)."""
        try:
            parser = self.module("net").parser()
        except Exception:  # noqa: BLE001 - an old or damaged installation: nothing beyond what it lists
            return set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
        return set()

    def foreground_start(self, name):
        """True if the kept start NAME runs (or ran) in the foreground of its own terminal (--no-tmux, a task):
        no tmux, nothing to attach to. False when sandbox-ai cannot say (older versions: every session had tmux)."""
        try:
            sessions = self.module("sessions")
            return bool(sessions.foreground(sessions.load(sessions.resolve(name))))
        except Exception:  # noqa: BLE001 - an older sandbox-ai, or a damaged start: as before
            return False

    def instances(self, proc=Path("/proc")):
        """Every sandbox-ai instance running on this account, and every session (also stopped ones), as
        [Instance]: the run registry (`sandbox-ai net list`: one-off runs, sessions and MCP servers that run,
        whoever started them - the command line, another copy of the app, a script), the named sessions
        (sessions.rows), the MCP servers whose supervisor runs, and launcher processes of this user that
        have not registered (yet: a sandbox being set up, or a sandbox-ai too old to register). Read with
        sandbox-ai's own modules and /proc; nothing is started or signalled."""
        found, problems = [], []
        try:
            runs = self.runs()
        except Exception as exc:  # noqa: BLE001 - the page shows what it can, and why
            runs, problems = {}, problems + [str(exc)]
        try:
            rows = self.sessions()
        except Exception as exc:  # noqa: BLE001
            rows, problems = [], problems + [str(exc)]
        try:
            mcp = self.module("mcp")
            servers = {name: mcp.load_state(name) for name in sorted(mcp.definitions()) if mcp.status(name) == "running"}
        except Exception as exc:  # noqa: BLE001 - e.g. a damaged mcp.json
            servers, problems = {}, problems + [str(exc)]
        try:
            ids = self.session_ids()
        except Exception:  # noqa: BLE001 - keyed by name then (an older sandbox-ai)
            ids = {}
        by_session = runs_of_starts(runs, ids)
        used = set()
        for row in rows:
            name, agent, engine, state, workspace, created = (list(row) + [""] * 6)[:6]
            run = by_session.get(name)
            record = runs.get(run, {}) if run else {}
            used.add(run)
            ident = ids.get(name, name)  # its directory: a renamed session keeps its row and its tab
            found.append(Instance("session", f"session:{ident}", name, run=run, session=name, project=workspace,
                                  agent=agent, backend=engine, started=record.get("started") or created, state=state,
                                  proxy=record.get("proxy") if run else None, pid=record.get("pid"), record=record,
                                  foreground=self.foreground_start(name), ident=ident))
        for name, state in servers.items():
            run = f"mcp-{name}" if f"mcp-{name}" in runs else None
            record = runs.get(run, {}) if run else {}
            used.add(run)
            found.append(Instance("mcp", f"mcp:{name}", name, run=run, mcp=name, project=record.get("workspace", ""),
                                  agent="mcp", backend=state.get("backend") or record.get("backend", ""),
                                  started=state.get("started") or record.get("started", ""), state="running",
                                  proxy=record.get("proxy") if run else None, pid=state.get("pid"), record=record))
        for name, record in runs.items():
            if name in used:
                continue
            session = session_of(record)
            kind = "session" if session else "mcp" if name.startswith("mcp-") else "run"
            found.append(Instance(kind, f"run:{name}", session or (name[4:] if kind == "mcp" else name), run=name,
                                  session=session, mcp=name[4:] if kind == "mcp" else None,
                                  project=record.get("workspace", ""), agent=record.get("agent", ""),
                                  backend=record.get("backend", ""), started=record.get("started", ""),
                                  state="running", proxy=record.get("proxy"), pid=record.get("pid"), record=record))
        known = {str(i.pid) for i in found if i.pid} | {str(state.get("pid")) for state in servers.values()}
        for pid, agent, argv, cwd in launcher_processes(self.agents, proc):
            if str(pid) in known:
                continue
            paths = paths_from_options(self.launcher, argv, cwd)
            project = paths["workspaces"][0] if paths["workspaces"] else cwd
            found.append(Instance("unregistered", f"pid:{pid}", f"pid {pid}", project=project or "", agent=agent,
                                  backend=option_value(argv, "--backend") or "", started=process_started(pid, proc) or "",
                                  state="starting", pid=pid))
        return found, problems

    def stop_run(self, run):
        """`sandbox-ai net stop RUN`: SIGTERM to the launcher of a one-off run, which cleans up and ends."""
        if not valid_run(run):
            return 2, "", "invalid run"
        return self.call("net", "stop", run, timeout=90)

    def stop_mcp(self, name):
        if not valid_run(name):
            return 2, "", "invalid name"
        return self.call("mcp", "stop", name, timeout=300)

    # -- projects --

    def recent_projects(self):
        """The app's own list of projects, newest first (settings: recent_projects)."""
        value = self.settings().get("recent_projects")
        if not isinstance(value, list):
            return []
        return [p for p in value if isinstance(p, str) and os.path.isabs(p) and "\0" not in p][:RECENT_PROJECTS]

    def remember_project(self, path):
        """PATH becomes the current project (settings: project) and the first of the recent ones."""
        path = os.path.realpath(os.path.expanduser(str(path)))
        data = self.settings()
        recent = [path] + [p for p in self.recent_projects() if p != path]
        data["recent_projects"], data["project"] = recent[:RECENT_PROJECTS], path
        # When each was used last (the wizard's list of recent folders says it): only for the folders listed.
        used = data.get("recent_projects_used") if isinstance(data.get("recent_projects_used"), dict) else {}
        used[path] = int(time.time())
        data["recent_projects_used"] = {p: used[p] for p in data["recent_projects"] if isinstance(used.get(p), int)}
        write_private_json(self.settings_file(), data)
        return path

    def recent_folders(self, limit=RECENT_FOLDERS):
        """[(folder, last used: seconds since the epoch or None)] of the project folders used last, newest first, at
        most LIMIT: the workspaces of the kept and running starts (sandbox-ai's registry, as `sandbox-ai list --all`
        has them; nothing is pruned) and the folders opened in this app. Only folders that exist, each once."""
        found = []
        try:
            entries = self.module("starts").entries(True, prune=False)
        except Exception:  # noqa: BLE001 - an older sandbox-ai, a damaged start: the app's own list still counts
            entries = []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("kind") != "mcp":
                when = max((parse_time(entry.get(key)) or 0 for key in ("last_used", "started", "created")),
                           default=0)
                found.append((entry.get("workspace"), when or None))
        used = self.settings().get("recent_projects_used")
        used = used if isinstance(used, dict) else {}
        for path in self.recent_projects():
            when = used.get(path)
            found.append((path, when if isinstance(when, int) and not isinstance(when, bool) else None))
        return merge_recent(found, limit)

    def forget_project(self, path):
        data = self.settings()
        data["recent_projects"] = [p for p in self.recent_projects() if p != path]
        if data.get("project") == path:
            data["project"] = ""
        write_private_json(self.settings_file(), data)

    def current_project(self):
        """The project chosen last (settings: project) while it is a folder, else None."""
        value = self.settings().get("project")
        return value if isinstance(value, str) and os.path.isabs(value) and os.path.isdir(value) else None

    def known_projects(self):
        """[(folder, sources)] of the projects this account has, newest first for the app's own list, then by
        name: the app's recent list ("recent"), sandbox-ai's trusted project configurations ("trusted"),
        the sessions ("session"), the running sandboxes ("running"), projects with answers remembered
        "always" ("approvals"), and folders of earlier runs from sandbox-ai's record of what runs exposed
        that have a log folder of their own ("earlier"). Only folders that exist; nothing inside them is read."""
        sources = {}

        def add(path, source):
            if not isinstance(path, (str, os.PathLike)) or not str(path) or "\0" in str(path):
                return
            real = os.path.realpath(os.path.expanduser(str(path)))
            if os.path.isdir(real):
                sources.setdefault(real, set()).add(source)
        for path in self.recent_projects():
            add(path, "recent")
        launcher = self.launcher
        try:
            for key in launcher.trusted_configs():
                add(os.path.dirname(str(key)), "trusted")
        except Exception:  # noqa: BLE001 - a damaged trust store: the other sources still count
            pass
        try:
            for row in self.sessions():
                add(row[4], "session")
        except Exception:  # noqa: BLE001
            pass
        try:
            for record in self.runs().values():
                add(record.get("workspace"), "running")
        except Exception:  # noqa: BLE001
            pass
        try:
            for project, _, _ in self.remembered():
                add(project, "approvals")
        except Exception:  # noqa: BLE001 - older sandbox-ai versions keep no such list
            pass
        try:
            data = json.loads((state_dir() / "exposed.json").read_text())
            for path in data if isinstance(data, list) else []:
                if isinstance(path, str) and os.path.isabs(path) and Path(launcher.default_log_dir(Path(path))).is_dir():
                    add(path, "earlier")
        except (OSError, ValueError, RecursionError, AttributeError):
            pass
        own = [os.path.realpath(p) for p in self.recent_projects()]
        order = {path: index for index, path in enumerate(own)}
        return sorted(sources.items(), key=lambda item: (order.get(item[0], len(order)), Path(item[0]).name.lower(),
                                                         item[0]))

    def project_state(self, directory):
        """(state, path) of DIRECTORY/.sandbox-ai.json: "none", "trusted", "changed" (since trusted),
        "untrusted" or "unreadable"; read with the launcher's bounded reader (no links, no FIFOs)."""
        try:
            review = self.review_project(directory)
        except FileNotFoundError:
            return "none", None
        except (OSError, ValueError) as exc:
            return "unreadable", printable(exc)
        return ("trusted" if review.trusted else "changed" if review.changed else "untrusted"), str(review.path)

    def project_kits(self, directory):
        """The kits (--kit) of DIRECTORY's .sandbox-ai.json, trusted or not; [] without one."""
        try:
            review = self.review_project(directory)
        except (OSError, ValueError):
            return []
        values, _ = values_from_argv(self.options(), review.options)
        return list(values.get("--kit", []))

    # -- kits (sandbox-ai kits) --

    def has_kits_command(self):
        try:
            return "kits" in self.module("main").PYTHON_COMMANDS
        except Exception:  # noqa: BLE001
            return False

    def kits_inventory(self):
        """`sandbox-ai kits list --json`: every kit (sandbox-ai's own, the default kit, the built-in kits such
        as egm, yours) with what it brings per agent, sandbox-ai's marketplace and the plugin store."""
        status, out, err = self.call("kits", "list", "--json", timeout=60)
        if status != 0:
            raise ValueError((err or out).strip() or f"sandbox-ai kits list: exit status {status}")
        document = json.loads(out)
        if not isinstance(document, dict) or not isinstance(document.get("kits"), list):
            raise ValueError("sandbox-ai kits list --json: not an inventory")
        return document

    def kit_command(self, action, name, *arguments, options=()):
        """`sandbox-ai kits ACTION [OPTIONS] -- NAME ARGUMENTS`: the kit NAME and the values after "--", so
        none of them is taken for an option."""
        if action not in KIT_ACTIONS or not re.fullmatch(KIT_NAME, str(name)):
            return 2, "", "invalid kit or action"
        if any(not isinstance(a, str) or not a or "\0" in a for a in arguments):
            return 2, "", "invalid argument"
        return self.call("kits", action, *options, "--", name, *arguments, timeout=300)

    def has_kit_hooks(self):
        """This sandbox-ai's kits carry hooks (sandbox-ai kits add-hook, trust-hooks)."""
        try:
            return "trust-hooks" in (self.directory / "kitctl.py").read_text()
        except OSError:
            return False

    def has_canary_log(self):
        """This sandbox-ai has `sandbox-ai canaries log` (every canary event with its process and links)."""
        try:
            return "def show_log(" in (self.directory / "canary.py").read_text()
        except OSError:
            return False

    def canary_log(self, run=None):
        """`sandbox-ai canaries log [-- RUN] --json`: [one run's log] (every run's, without RUN), each with its
        events, the process of each and the tool call, prompt turn and model requests linked to it."""
        status, out, err = self.call("canaries", "log", "--json", *(["--", run] if run else []), timeout=60)
        if status != 0:
            raise ValueError((err or out).strip() or f"sandbox-ai canaries log: exit status {status}")
        document = json.loads(out)
        if isinstance(document, dict):
            document = [document]
        if not isinstance(document, list):
            raise ValueError("sandbox-ai canaries log --json: not a log")
        return document

    def trust_hooks_argv(self, name):
        """`sandbox-ai kits trust-hooks -- NAME`, for a terminal: it shows every hook and the full text of every
        file, and approves only after the human types yes there (never --yes from the app)."""
        if not re.fullmatch(KIT_NAME, str(name)):
            raise ValueError(f"{name!r} is not a kit name")
        return self.command("kits", "trust-hooks", "--", name)

    def plugins_argv(self, update=False, kits=()):
        """`sandbox-ai plugins claude [--update] [--kit NAME]...` (built-in kits when this sandbox-ai can)."""
        argv = ["plugins", "claude"] + (["--update"] if update else [])
        try:
            supports = "--kit" in (self.directory / "plugins.py").read_text()
        except OSError:
            supports = False
        if supports:
            argv += [token for name in kits if re.fullmatch(KIT_NAME, name) for token in ("--kit", name)]
        return self.command(*argv)

    # -- statistics --

    def statistics_argv(self, directory):
        """`sandbox-ai logs DIR --json`: its "telemetry" and "tool_calls" are what the Statistics page shows."""
        return self.command("logs", str(directory), "--json")

    def doctor_argv(self, backend="podman"):
        return self.command("doctor", "--backend", backend, "--json")

    def statistics(self, directory):
        """logs.telemetry of DIRECTORY plus the MCP servers' tool calls (in this process; the window runs
        statistics_argv in the background instead)."""
        logs = self.module("logs")
        data = logs.telemetry(directory)
        data["tool_calls"] = logs.tool_calls()
        return data

    # -- logins and secrets --

    def login_status(self):
        """[(agent, where, problem, source)]: where "store" (sandbox-ai's own login) or "host"; problem
        None, "missing" or "expired"."""
        launcher, rows = self.launcher, []
        for agent in self.agents:
            try:
                where, problem = launcher.login_state(agent)
                source = printable(launcher.auth_sources(agent)[0][0])
            except (OSError, ValueError) as exc:
                where, problem, source = "?", "error", printable(exc)
            rows.append((agent, where, problem, source))
        return rows

    def secrets(self):
        """(backend name, [(name, where)]) of the kept secrets: names only, never values."""
        module = self.module("secret_store")
        store = module.store()
        names = set(store.names())
        files = set(module.FileStore().names()) if store.name != "file" else set()
        return store.name, [(name, store.name if name in names else "file") for name in sorted(names | files)]

    def set_secret(self, name, value, host="local"):
        """Keep VALUE as secret NAME with `sandbox-ai secrets set NAME` (on HOST: over SSH), the value on standard
        input - never an argument."""
        argv = self.set_secret_argv(name, value)
        if argv is None:
            return 2, "", "invalid name or value"
        if host != "local":
            return self.host_run(host, *argv[3:], timeout=90, input=value + "\n")
        return self.call(*argv[3:], input=value + "\n", timeout=60)

    def host_secrets(self, host):
        """(store, [(name, where)]) of the secrets kept on HOST - names only (`sandbox-ai secrets list` there, its
        table read as the web interface reads it); this machine's from secrets(). ValueError: not answered."""
        if host == "local":
            return self.secrets()
        status, out, err = self.host_run(host, "secrets", "list", timeout=60)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        lines, found = out.splitlines(), []
        if lines and lines[0].split() == ["NAME", "STORE"]:
            for line in lines[1:]:
                parts = line.split()
                if len(parts) == 2 and self.secret_name.fullmatch(parts[0]):
                    found.append((parts[0], printable(parts[1])[:32]))
        stores = {where for _, where in found}
        return (stores.pop() if len(stores) == 1 else "?"), found

    def secret_exists(self, name, host="local"):
        """Whether a secret NAME is kept on HOST (names only are read)."""
        return any(found == name for found, _ in self.host_secrets(host)[1])

    def save_token(self, url, token):
        """Keep a push TOKEN typed in the wizard as sandbox-ai's own wizard does: a private file (mode 0600,
        never through a link) under ~/.config/sandbox-ai/tokens, outside every workspace; the project file
        only names it (--git-token-file URL=FILE). Returns the file."""
        if not isinstance(token, str) or not token.strip() or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError("a token is one line of printable characters")
        if not re.fullmatch(r"https?://[^\s/]+", str(url)):
            raise ValueError("a Git server is http(s)://[USER@]HOST[:PORT]")
        wizard = self.module("wizard")
        path = wizard.token_path(re.sub(r"^https?://", "", url))
        wizard.write_token(path, token.strip())
        return path

    def valid_secret(self, name, value=None):
        if not isinstance(name, str) or not self.secret_name.fullmatch(name):
            return False
        return value is None or (bool(value) and len(value) <= self.secret_limit
                                 and not any(ord(c) < 32 or ord(c) == 127 for c in value))

    def set_secret_argv(self, name, value):
        """argv of `sandbox-ai secrets set NAME` (the value goes on standard input), or None if invalid."""
        return self.command("secrets", "set", name) if self.valid_secret(name, value) else None

    def delete_secret(self, name, host="local"):
        if not self.valid_secret(name):
            return 2, "", "invalid name"
        if host != "local":
            return self.host_run(host, "secrets", "delete", name, timeout=90)
        return self.call("secrets", "delete", name, timeout=60)

    def host_approvals_history(self, host, run=None):
        """The questions of HOST's sandboxes with their answers, as `sandbox-ai approvals history [RUN]` there says
        them (its table, escaped; the newest last). ValueError: not answered."""
        if run is not None and not valid_run(run):
            raise ValueError("invalid run")
        status, out, err = self.host_run(host, "approvals", "history", *([run] if run else []), timeout=60)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return "\n".join(printable(line) for line in out.strip()[-20000:].splitlines())

    # -- files --

    def file_roots(self, extra=()):
        """[(kind, label, path)] of folders the file browser may show: the workspaces and HOME of every
        running sandbox and session that exist on this host, and EXTRA ((kind, label, path) given by the app)."""
        found, seen = [], set()

        def add(kind, label, path):
            if not path:
                return
            path = os.path.realpath(os.path.expanduser(str(path)))
            if path in seen or not os.path.isdir(path):
                return
            seen.add(path)
            found.append((kind, label, path))
        try:
            runs = self.runs()
        except Exception:  # noqa: BLE001 - the browser shows what it can
            runs = {}
        sessions = self.module("sessions")
        for name, record in runs.items():
            session = session_of(record)
            add("run", session or name, record.get("workspace"))
            paths = run_paths(self.launcher, record.get("pid"))
            if session:
                try:
                    add("session-home", session, sessions.session_dir(session) / "home")
                except ValueError:
                    pass
            for workspace in paths["workspaces"]:
                add("run", session or name, workspace)
            add("run-home", name, paths["state_dir"])
        try:
            rows = sessions.rows()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            name = row[0]
            try:
                data = sessions.load(name)
            except (OSError, ValueError):
                continue
            add("session", name, data.get("workspace"))
            options = paths_from_options(self.launcher, data.get("options", []), data.get("cwd"))
            for workspace in options["workspaces"]:
                add("session", name, workspace)
            add("session-home", name, sessions.session_dir(name) / "home")
        for kind, label, path in extra:
            add(kind, label, path)
        return found

    def sandbox_folders(self):
        """The folders sandboxes can or could write: the workspaces, HOMEs and rw --mount sources of running
        sandboxes and sessions, sandbox-ai's registry of what earlier runs exposed (exposed_before), and
        what preload_trees() finds without modules."""
        folders = [path for kind, _, path in self.file_roots() if kind in ("run", "run-home", "session", "session-home")]
        folders += self.rw_mounts()
        try:
            folders += [str(p) for p in self.launcher.exposed_before()]
        except Exception:  # noqa: BLE001 - an older sandbox-ai: the registry is read directly below
            pass
        folders += preload_trees()
        return list(dict.fromkeys(os.path.realpath(f) for f in folders))

    def rw_mounts(self):
        """The rw --mount sources of running sandboxes (their command lines) and sessions (their options)."""
        found = []
        try:
            runs = self.runs()
        except Exception:  # noqa: BLE001
            runs = {}
        for record in runs.values():
            found += run_paths(self.launcher, record.get("pid"))["mounts"]
        sessions = self.module("sessions")
        try:
            rows = sessions.rows()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            try:
                data = sessions.load(row[0])
            except (OSError, ValueError):
                continue
            found += paths_from_options(self.launcher, data.get("options", []), data.get("cwd"))["mounts"]
        return found

    def startup_risk(self):
        """InsideSandbox if the app's own files or this sandbox-ai installation lie in a folder sandboxes can
        or could write, else None - decided without loading any sandbox-ai module (see preload_trees)."""
        try:
            check_outside(autostart_paths() + [str(self.directory)], preload_trees())
        except InsideSandbox as exc:
            return exc
        return None

    def autostart_risk(self):
        """Like startup_risk, with everything the loaded modules know (rw mounts, the registry)."""
        try:
            check_outside(autostart_paths() + [str(self.directory)], self.sandbox_folders())
        except InsideSandbox as exc:
            return exc
        return None

    # -- settings --

    def settings_file(self):
        return settings_file()

    def settings(self):
        return app_settings()

    def save_setting(self, key, value):
        self.save_settings({key: value})

    def save_settings(self, values):
        data = self.settings()
        data.update(values)
        write_private_json(self.settings_file(), data)

    # -- other hosts (sandbox-ai hosts): machines whose sandbox-ai this one manages over SSH --

    def hosts_supported(self):
        return (self.directory / "hosts.py").is_file()

    def host_entries(self):
        """[{"name", "kind", "ssh", "port", "description", ...}]: this machine ("local") first."""
        return self.module("hosts").entries()

    def host_add(self, name, ssh, port=22, identity="", description=""):
        args = ["hosts", "add", name, ssh, "--port", str(int(port))]
        if identity:
            args += ["--identity", identity]
        if description:
            args += ["--description", description]
        return self.checked(*args, timeout=30)

    def host_set(self, name, update_sudo):
        """`sandbox-ai hosts set NAME --update-sudo on|off`: whether hosts update runs its update with sudo."""
        return self.checked("hosts", "set", name, "--update-sudo", "on" if update_sudo else "off", timeout=30)

    def host_remove(self, name):
        return self.checked("hosts", "remove", name, timeout=30)

    def host_test(self, name):
        """{"ssh", "version", "doctor", "error"} of `sandbox-ai hosts test NAME`."""
        status, out, err = self.call("hosts", "test", name, "--json", timeout=150)
        try:
            return json.loads(out)
        except ValueError:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def host_run(self, name, *args, timeout=120, input=None):
        """(status, out, err) of `sandbox-ai ARGS` on host NAME (this machine for "local"). INPUT, if given, is its
        standard input (a secret's value: never an argument); `sandbox-ai hosts run` hands its own on to ssh."""
        extra = {} if input is None else {"input": input}
        if name == "local":
            return self.call(*args, timeout=timeout, **extra)
        return self.call("hosts", "run", name, "--", *args, timeout=timeout, **extra)

    def host_instances(self, name, timeout=60):
        """The starts on host NAME (`sandbox-ai list --json` there, over SSH) as [Instance] with HOST set: what the
        Sandboxes page lists beside this account's own."""
        found = []
        for entry in self.host_json(name, "list", "--json", timeout=timeout):
            # The other host (and a sandbox on it) chooses these: only a start's name may come back as an argument
            # (never one that reads as an option there).
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or \
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", entry["name"]):
                continue
            kind = entry.get("kind") if entry.get("kind") in ("session", "run", "mcp") else "run"
            ident = start_id(entry)
            found.append(Instance(kind, remote_key(name, ident), entry["name"], run=None, project=str(
                entry.get("workspace") or ""), agent=str(entry.get("agent") or ""), started=str(entry.get("started") or ""),
                state=str(entry.get("state") or ""), record=entry, host=name, backend=printable(entry.get("backend"))[:20]
                if isinstance(entry.get("backend"), str) else "", ident=ident))
        return found

    def ended_starts(self, name="local"):
        """The starts that ended on host NAME (this machine for "local"), as `sandbox-ai list --all --json` there lists
        them (stopped MCP servers, kept starts that do not run) - [Instance] keyed ended:HOST:NAME, never equal to a
        key of what runs. ValueError: no answer."""
        data = self.host_json(name, "list", "--all", "--json", timeout=60)
        found = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or \
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", entry["name"]):
                continue
            state = str(entry.get("state") or "")
            if state in ("running", "paused", "starting"):
                continue
            kind = entry.get("kind") if entry.get("kind") in ("session", "run", "mcp") else "run"
            ident = start_id(entry)
            found.append(Instance(kind, f"ended:{name}:{ident}", entry["name"], project=str(
                entry.get("workspace") or ""), agent=str(entry.get("agent") or ""),
                started=str(entry.get("started") or ""), state=state or "ended", record=entry, host=name,
                session=entry["name"] if kind == "session" else None, mcp=entry["name"] if kind == "mcp" else None,
                backend=printable(entry.get("backend"))[:20] if isinstance(entry.get("backend"), str) else "",
                ident=ident))
        return found

    def start_history(self, name, host="local"):
        """What happened to the start NAME on HOST: [{"time", "event", "run", ...}] of `sandbox-ai list --history NAME
        --json`. ValueError: not answered."""
        if not valid_run(name):
            raise ValueError("invalid start")
        data = self.host_json(host, "list", "--history", name, "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a history")
        return [entry for entry in data if isinstance(entry, dict)]

    def collect(self, host="local", dry_run=True, days=None):
        """`sandbox-ai gc [--older-than DAYS] [--dry-run] --json` on HOST: {"removed", "leftovers", "histories",
        "skipped", "damaged", "days", "dry_run"}. ValueError: not answered."""
        if days is not None and not (isinstance(days, int) and not isinstance(days, bool) and 0 <= days <= 36500):
            raise ValueError("invalid number of days")
        data = self.host_json(host, "gc", *([f"--older-than={days}"] if days is not None else []),
                              *(["--dry-run"] if dry_run else []), "--json", timeout=300)
        if not isinstance(data, dict):
            raise ValueError("not what sandbox-ai gc says")
        return data

    def collect_as_shown(self, host, shown):
        """`sandbox-ai gc` on HOST removing what its dry run SHOWN listed and nothing else: with the same
        --older-than, and only when a dry run now still lists exactly that (GcChanged otherwise: a start was used or
        ended meanwhile, keep_days changed). Returns what gc says it removed."""
        days = shown.get("days") if isinstance(shown, dict) else None
        days = days if isinstance(days, int) and not isinstance(days, bool) and days >= 0 else None
        if gc_plan(self.collect(host, True, days=days)) != gc_plan(shown):
            raise GcChanged(host)
        return self.collect(host, False, days=days)

    def host_net_options(self, name, command="allow"):
        """The options `sandbox-ai net COMMAND` takes on host NAME (its --help there, parsed), kept per host and
        sandbox-ai version (for NET_OPTIONS_SECONDS without a known version). ValueError: no answer."""
        try:
            version = (self.host_statuses(cached=True).get(name) or {}).get("version")
        except Exception:  # noqa: BLE001 - an older sandbox-ai, or no list of hosts: asked every NET_OPTIONS_SECONDS
            version = None
        cache = self.__dict__.setdefault("_net_options", {})
        key = (name, command, version)
        kept = cache.get(key)
        if kept is not None and (version or time.time() - kept[0] < NET_OPTIONS_SECONDS):
            return set(kept[1])
        status, out, err = self.host_run(name, "net", command, "--help", timeout=60)
        found = set(re.findall(r"(?<![\w-])--[a-z0-9][a-z0-9-]*", out)) if status == 0 else set()
        if not found:
            raise ValueError((err or out).strip()[-300:] or f"exit status {status}")
        cache[key] = (time.time(), found)
        return found

    def remote_hosts(self):
        """The names of the hosts of `sandbox-ai hosts` other than this machine ([] without hosts)."""
        if not self.hosts_supported():
            return []
        return [e["name"] for e in self.host_entries() if e.get("kind") != "local" and e.get("name") != "local"]

    def host_questions(self, name, timeout=REMOTE_QUESTION_WAIT):
        """The open questions of the sandboxes on host NAME (`sandbox-ai approvals list --json` there, over SSH) as
        [RemoteQuestion]; what does not look like a question of sandbox-ai is left out. ValueError: no answer.
        Their time left counts from when the read STARTED: that host said "seconds_left" somewhere during the SSH
        round trip, so a slow read never shows more time than there is."""
        started = time.time()
        data = self.host_json(name, "approvals", "list", "--json", timeout=timeout)
        if not isinstance(data, list):
            raise ValueError(f"{name}: not a list of questions")
        return remote_questions(name, data, self.question_id, read_at=started)

    def limits_shown(self, run, host="local"):
        """{"memory", "memory_high", "cpus", "pids"} (None: no limit) of the running sandbox RUN on HOST, from
        `sandbox-ai limits RUN --show --json`; numbers only are taken. ValueError: not answered."""
        if not valid_run(run):
            raise ValueError("invalid run")
        data = self.host_json(host, "limits", run, "--show", "--json", timeout=60)
        if not isinstance(data, dict):
            raise ValueError("not a list of limits")
        found = {}
        for key, kind in (("memory", int), ("memory_high", int), ("cpus", (int, float)), ("pids", int)):
            value = data.get(key)
            found[key] = value if isinstance(value, kind) and not isinstance(value, bool) and value > 0 else None
        return found

    def review_home(self, name, host="local"):
        """What changed in the start files of the HOME of the kept start NAME on HOST since its last start (`sandbox-ai
        resume NAME --review-home --json`): {"name", "state", "pending", "review": None or {"since", "changes":
        [{"path", "change", ...}], "complete"}}. ValueError: not reviewed (the text of sandbox-ai)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        status, out, err = self.host_run(host, "resume", name, "--review-home", "--json", timeout=120)
        try:
            data = json.loads(out) if status == 0 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return data

    def host_json(self, name, *args, timeout=120):
        status, out, err = self.host_run(name, *args, timeout=timeout)
        try:
            return json.loads(out)
        except ValueError:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def host_terminal(self, name, *args):
        """argv of `sandbox-ai ARGS` on host NAME in a terminal (ssh -tt for another host)."""
        return list(self.module("hosts").argv(name, list(args), tty=True))

    def host_update_supported(self):
        return hasattr(self.module("hosts"), "update")

    def host_statuses(self, refresh=False, cached=True):
        """{name: {"version", "kind", "available", "update", "error", ...} or None} of the hosts' sandbox-ai, as
        `sandbox-ai hosts list` keeps it (CACHED: no SSH at all; else asked when older than 10 minutes, REFRESH:
        now)."""
        hosts = self.module("hosts")
        names = sorted(hosts.load())
        return hosts.statuses(names, refresh=refresh, cached=cached and not refresh) if names else {}

    def host_update(self, name, sudo=None):
        """The result of `sandbox-ai hosts update NAME --json` ({"before", "after", "changed", "error",
        "needs_sudo", "needs_password", "notes", "fixed"}); SUDO None: the host's own setting."""
        args = ["hosts", "update", name, "--json", *(["--sudo"] if sudo else ["--no-sudo"] if sudo is False else [])]
        status, out, err = self.call(*args, timeout=self.module("hosts").UPDATE_TIMEOUT + 60)
        try:
            return json.loads(out)[0]
        except (ValueError, IndexError, KeyError, TypeError):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def host_install_supported(self):
        return hasattr(self.module("hosts"), "install")

    def host_install_args(self, name, mode=None, prefix=None, sudo=None, source="here", version=None):
        """The words after `sandbox-ai` of `hosts install NAME ...`, every value checked: MODE "user" (for the SSH
        user), "system" (for every user under PREFIX; as root, or with sudo) or None (system when the SSH user is
        root); SUDO True/False/None (sandbox-ai decides); SOURCE "here" (this machine's copy) or "download" (the
        signed release, VERSION or the newest). ValueError says what is wrong."""
        if not (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)) or name == "local":
            raise ValueError("not a host of the list")
        if mode not in (None, "user", "system"):
            raise ValueError("an installation is for the user or for the system")
        if prefix and (mode != "system" or not valid_directory(prefix)):
            raise ValueError("a prefix is an absolute folder, for a system installation")
        if source not in ("here", "download"):
            raise ValueError("the source is this machine's copy or the signed release")
        if version and (source != "download" or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}", version)):
            raise ValueError("a version is X.Y.Z, for a download of the signed release")
        args = ["hosts", "install", name]
        args += {"user": ["--user"], "system": ["--system"], None: []}[mode]
        args += [f"--prefix={prefix}"] if prefix else []
        args += ["--sudo"] if sudo is True else ["--no-sudo"] if sudo is False else []
        args += ["--download"] + ([f"--version={version}"] if version else []) if source == "download" else \
            ["--from-here"]
        return args

    def host_install(self, name, mode=None, prefix=None, sudo=None, source="here", version=None):
        """`sandbox-ai hosts install ... --json` on this machine (up to its INSTALL_TIMEOUT): {"name", "before",
        "after", "changed", "mode", "prefix", "sudo", "root", "source", "error", "needs_password", "output",
        "notes"}. ValueError: no such answer."""
        args = self.host_install_args(name, mode, prefix, sudo, source, version)
        timeout = getattr(self.module("hosts"), "INSTALL_TIMEOUT", 900) + 60
        status, out, err = self.call(*args, "--json", timeout=timeout)
        try:
            result = json.loads(out)
        except ValueError:
            result = None
        if not isinstance(result, dict):  # refused before it began (an option this sandbox-ai lacks, no such host)
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return result

    def host_install_terminal(self, name, mode=None, prefix=None, sudo=None, source="here", version=None):
        """argv of the same installation in a terminal tab, where sudo on the host may ask for its password."""
        return self.command(*self.host_install_args(name, mode, prefix, sudo, source, version))

    def host_update_terminal(self, name):
        """argv of host NAME's update in a terminal tab, where sudo may ask for its password."""
        return list(self.module("hosts").update_terminal(name))

    def checked(self, *args, timeout=120):
        status, out, err = self.call(*args, timeout=timeout)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return out

    # -- sandbox-ai's own defaults (~/.config/sandbox-ai/defaults.json): what every start uses --

    def telemetry_levels(self):
        """--telemetry's levels in this sandbox-ai, or () when it is older and has no such option."""
        launcher = self.launcher
        if "telemetry" not in getattr(launcher, "DEFAULTS_KEYS", ()):
            return ()
        return tuple(getattr(launcher, "TELEMETRY_LEVELS", ()))

    def sandbox_defaults_file(self):
        return Path(self.launcher.defaults_file())

    # -- extra preset directories (sandbox-ai presets dirs) --

    def has_preset_dirs(self):
        """This sandbox-ai reads presets from extra directories (defaults.json's preset_dirs)."""
        try:
            return "def dirs_main" in (self.directory / "presets.py").read_text()
        except OSError:
            return False

    def preset_dirs(self):
        """`sandbox-ai presets dirs list --json`: {"dirs": [{path, state, why, presets}], "yours", "builtin", ...}."""
        status, out, err = self.call("presets", "dirs", "list", "--json", timeout=60)
        if status != 0:
            raise ValueError((err or out).strip() or f"sandbox-ai presets dirs list: exit status {status}")
        document = json.loads(out)
        if not isinstance(document, dict) or not isinstance(document.get("dirs"), list):
            raise ValueError("sandbox-ai presets dirs list --json: not a list of directories")
        return document

    def preset_dir_command(self, action, path):
        """`sandbox-ai presets dirs add|remove -- PATH`: its checks apply (an absolute directory, never one where
        sandboxes write)."""
        if action not in ("add", "remove") or not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
            return 2, "", "invalid action or path"
        return self.call("presets", "dirs", action, "--", path, timeout=60)

    def sandbox_defaults(self):
        """(document, problem): sandbox-ai's defaults file ({} without it); a broken file is a problem, never
        rewritten by the app."""
        path = self.sandbox_defaults_file()
        try:
            document = json.loads(path.read_text())
        except FileNotFoundError:
            return {}, None
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return {}, f"{path}: {exc}"
        if not isinstance(document, dict) or document.get("version") != 1:
            return {}, f"{path}: expected an object with \"version\": 1"
        return document, None

    def save_sandbox_default(self, key, value):
        """Set KEY of sandbox-ai's defaults file to VALUE (None: remove it, sandbox-ai's own default applies);
        the other keys stay. Only keys and values this sandbox-ai accepts: a file it refused would stop every
        start."""
        if key != "telemetry" or value is not None and value not in self.telemetry_levels():
            raise ValueError(f"this sandbox-ai has no default {key} = {value!r}")
        document, problem = self.sandbox_defaults()
        if problem:
            raise ValueError(problem)
        document = {"version": 1, **document}
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
        write_private_json(self.sandbox_defaults_file(), document)

    def log_directories(self):
        """Directories that hold sandbox-ai logs: the default ones per project, newest first."""
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "logs"
        try:
            found = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            return []
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

    # -- profiles --

    def profiles_file(self):
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "app" / "profiles.json"

    def profiles(self):
        try:
            data = json.loads(self.profiles_file().read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_profile(self, name, agent, argv, agent_args=()):
        profiles = self.profiles()
        profiles[name] = {"agent": agent, "options": list(argv), "agent_args": list(agent_args)}
        write_private_json(self.profiles_file(), profiles)

    def delete_profile(self, name):
        profiles = self.profiles()
        if profiles.pop(name, None) is not None:
            write_private_json(self.profiles_file(), profiles)
            return True
        return False

    def project_document(self, agent, options, values, agent_args=()):
        """(.sandbox-ai.json document, main workspace) for the form's values: the first workspace is where
        the file lives, further ones are listed; options a project file may not set are left out."""
        forbidden = set(self.launcher.CONFIG_FORBIDDEN)
        workspaces = [w for value in values.get("--workspace", []) for w in str(value).split(",") if w.strip()]
        kept = {name: value for name, value in values.items() if name not in forbidden}
        document = {"version": 1, "agent": agent, "options": argv_from_values(options, kept)}
        if workspaces[1:]:
            document["workspaces"] = workspaces[1:]
        if agent_args:
            document["agent_args"] = list(agent_args)
        return document, (workspaces[0] if workspaces else None)

    def write_project(self, directory, document):
        """Write DIRECTORY/.sandbox-ai.json and trust exactly the bytes written.

        The workspace is the agent's to write: the file is created as a new file next to the target
        (O_EXCL | O_NOFOLLOW) and renamed over it, so a link the agent planted is never written through,
        and the digest recorded as trusted is that of the bytes the app wrote, not of whatever is there
        when it is read again (a file swapped in meanwhile stays untrusted and the launcher refuses it)."""
        launcher = self.launcher
        name = launcher.CONFIG_NAME
        data = (json.dumps(document, indent=2) + "\n").encode()
        if len(data) > launcher.CONFIG_LIMIT:
            raise ValueError("the configuration is too large")
        directory = os.path.realpath(os.fspath(directory))
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{name} in {directory} is a symbolic link; it was not written (remove the link)")
            if info is not None and not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{name} in {directory} is not a regular file; it was not written")
            temporary = f".{name}.sandbox-ai-app-{os.getpid()}-{secrets.token_hex(8)}"
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                          dir_fd=fd)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(out, view):]
                os.fsync(out)
            except BaseException:
                os.close(out)
                os.unlink(temporary, dir_fd=fd)
                raise
            os.close(out)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)
        path = Path(directory) / name
        launcher.trust_config(path, digest=hashlib.sha256(data).hexdigest())
        return path

    def review_project(self, directory):
        """ProjectReview of DIRECTORY/.sandbox-ai.json: read once with the launcher's own bounded reader
        (no links, no FIFOs or devices, at most CONFIG_LIMIT bytes), its digest compared with the trusted
        one, and what the launcher would refuse or what widens the sandbox listed. Nothing is applied here."""
        launcher = self.launcher
        path = launcher.config_key(Path(directory) / launcher.CONFIG_NAME)
        data = launcher.read_config_bytes(path)
        digest = hashlib.sha256(data).hexdigest()
        known = launcher.trusted_configs().get(str(path))
        review = ProjectReview(path, data, digest, trusted=known == digest, changed=bool(known) and known != digest)
        try:
            document = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError, RecursionError):
            review.problems.append("not valid JSON")
            return review
        if not isinstance(document, dict) or document.get("version") != 1:
            review.problems.append("not a version 1 project configuration")
            return review
        unknown = set(document) - set(launcher.CONFIG_KEYS)
        if unknown:
            review.problems.append("unknown keys: " + ", ".join(sorted(printable(k) for k in unknown)))
        for key in ("options", "agent_args", "workspaces"):
            values = document.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                review.problems.append(f"{key} must be a list of strings")
                return review
        agent = document.get("agent")
        if agent not in self.agents:
            review.problems.append(f"unknown agent {printable(agent)}")
        review.document, review.agent = document, agent if agent in self.agents else None
        try:
            options = launcher.canonical_options(document.get("options", []))
        except (ValueError, SystemExit):
            options = list(document.get("options", []))
        review.options = options
        for value in options:
            if any(value == flag or value.startswith(flag + "=") for flag in launcher.CONFIG_FORBIDDEN):
                review.problems.append(f"option {printable(value)} is not allowed in a project configuration")
        relative = launcher.relative_paths(options) + [
            w for w in document.get("workspaces", []) if not os.path.isabs(os.path.expanduser(w))]
        if relative:
            review.problems.append("relative paths: " + ", ".join(printable(r) for r in relative))
        review.workspaces = list(document.get("workspaces", []))
        review.agent_args = list(document.get("agent_args", []))
        index = 0
        while index < len(options):
            name = options[index].split("=", 1)[0]
            takes_value = "=" not in options[index] and index + 1 < len(options) and not options[index + 1].startswith("-")
            value = options[index + 1] if takes_value else options[index].partition("=")[2]
            if name in WIDENING:
                review.widening.append((name, printable(value)))
            index += 2 if takes_value else 1
        return review

    def trust_project(self, review):
        """Trust exactly the bytes shown in REVIEW (if the file changed since, the launcher refuses it)."""
        if review.problems:
            raise ValueError("this configuration cannot be used: " + "; ".join(review.problems))
        self.launcher.trust_config(review.path, digest=review.digest)

    # -- actions --

    def launch_argv(self, agent, argv, agent_args=(), session=None):
        """The sandbox-ai command line for a start."""
        extra = ["--session", session] if session else []
        return self.command(agent, *argv, *extra, *(["--", *agent_args] if agent_args else []))

    def dry_run_argv(self, agent, argv):
        return self.command(agent, *argv, "--dry-run")

    def dry_run(self, agent, argv):
        """(plan or None, error text) of a dry run with these options (blocking; the window uses
        dry_run_argv with AsyncCommand and parse_dry_run)."""
        return parse_dry_run(*self.call(agent, *argv, "--dry-run", timeout=60))

    def in_terminal(self, argv, title="sandbox-ai"):
        """argv that runs ARGV in a new terminal window, or None if no terminal emulator is known."""
        preferred = os.environ.get("SANDBOX_AI_TERMINAL")
        terminals = ([(preferred, ["-e"])] if preferred else []) + list(TERMINALS)
        for program, before in terminals:
            if program and shutil.which(program):
                # The window stays open after the end, so that a message or an error can be read.
                script = f"{shlex.join(argv)}; status=$?; echo; echo \"[sandbox-ai: exit status $status, press Enter]\"; read _"
                if Path(program).name in ONE_ARGUMENT_TERMINALS:  # -e takes the whole command as one string
                    return [program, *before, shlex.join(["sh", "-c", script])]
                return [program, *before, "sh", "-c", script]
        return None


# Options that widen what a sandbox may do or see: listed first when a project configuration is reviewed.
WIDENING = {
    "--dangerously-skip-permissions", "--egress-all-allow", "--mount", "--copy", "--device", "--gpu", "--gui-wayland",
    "--gui-vnc", "--allow", "--allow-tcp", "--allow-host", "--allow-http", "--allow-host-port", "--allow-pypi", "--allow-github",
    "--allow-gitlab", "--allow-kernel-org", "--allow-duckduckgo", "--frontier", "--profile", "--dns", "--nameserver",
    "--network", "--ip", "--ip6", "--no-proxy", "--no-inspect", "--ask", "--env", "--secret-env", "--api-key", "--publish", "--git-token-env",
    "--git-token-file", "--git-token-secret", "--git-credentials", "--git-identity", "--auth-file",
    "--import-codex-auth", "--copy-auth", "--local-config", "--runtime", "--binary", "--image", "--image-variant", "--podman-url",
    "--kit", "--kit-dir", "--mcp", "--state-dir", "--privacy-check", "--memory", "--cpus", "--pids", "--backend",
    "--vpn-check",  # warn: a VPN whose checks fail is used anyway
}


class ProjectReview:
    """What a project configuration holds, for the human to review before anything is applied."""

    def __init__(self, path, data, digest, trusted, changed):
        self.path, self.data, self.digest, self.trusted, self.changed = path, data, digest, trusted, changed
        self.document, self.agent, self.options, self.workspaces, self.agent_args = None, None, [], [], []
        self.problems, self.widening = [], []

    @property
    def usable(self):
        return not self.problems and self.agent is not None

    def text(self):
        """The file as it was read, for display (escaped)."""
        return printable(self.data.decode("utf-8", "replace"))


def remembered_rows(known):
    """[(project, entry, kind)] of approvals.remembered()'s {project: {"hosts", "tcp", "blocked_hosts",
    "blocked_tcp"}} (also another host's JSON: only strings are taken, sorted)."""
    rows = []
    for project, entry in sorted((known if isinstance(known, dict) else {}).items()):
        if not isinstance(entry, dict):
            continue
        for kind, field in (("host", "hosts"), ("tcp", "tcp"), ("blocked-host", "blocked_hosts"),
                            ("blocked-tcp", "blocked_tcp")):
            values = entry.get(field)
            rows += [(str(project), value, kind) for value in (values if isinstance(values, list) else [])
                     if isinstance(value, str)]
    return rows


def valid_run(run):
    return isinstance(run, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run))


def valid_directory(path):
    """An absolute path of one line (never read as an option)."""
    path = str(path or "")
    return path.startswith("/") and len(path) <= 4096 and path.isprintable() and "\0" not in path


def valid_device(path):
    """A device path of one line under /dev (never read as an option)."""
    path = str(path or "")
    return path.startswith("/dev/") and len(path) <= 4096 and path.isprintable() and ".." not in path.split("/")


# -- one confirmation policy --
# What ends, removes or weakens something: the web interface's table (webui.NEEDS_CONFIRM: the same actions;
# tests/test_backend.py compares them with webui.py), and the app's own on top (APP_NEEDS_CONFIRM: what no route of the
# web interface does; none now). Every call path of the app that does one of these asks first through
# widgets.ask_first(), which asks needs_confirm() (tests/test_references.py walks the mutating calls of the pages).
NEEDS_CONFIRM = {
    "start-stop": "stopping a start ends what runs in it (and its HOME, unless it is kept)",
    "start-delete": "deleting a start removes its kept HOME",
    "gc": "gc removes the leftovers of ended starts, and kept starts unused for that long with their HOME, for good",
    "approval-allow-always": "an allow always is remembered for the project: every later start of it gets this "
                             "without asking",
    "approval-deny-always": "a deny always is remembered for the project: every later question like it is refused",
    "approval-forget": "forgetting a remembered answer changes what every later start of that project gets (a "
                       "forgotten block is asked again)",
    "defaults-lower": "this lowers the protection of every later start on that host",
    "net-allow": "net allow widens what the running sandbox reaches, at once (real DNS, ports on the host, host "
                 "wildcards, Git logins included)",
    "net-untaint": "untaint takes back the rules for tainted runs: the sandbox is treated as if no untrusted content "
                   "had entered it",
    "workspace-writable": "a writable workspace lets the agent change the directory on that host",
    "workspace-force": "a forced removal leaves what a process there still holds usable, also for writing, until it "
                       "is closed; a copy the agent took stays usable until the sandbox restarts",
    "device-add": "a device gives the agent that hardware of the host",
    "limits-raise": "a limit above the current one (or none) lets the agent use more of that host",
    "host-remove": "removing a host ends this service's management of it",
    "vpn-remove": "removing a VPN profile stops the starts that use it from starting",
    "llm-remove": "removing an LLM server stops the starts that use it from starting",
    "kit-delete": "deleting a kit removes its folder",
    "kit-remove": "removing a skill or plugin from a kit takes it from every later start with that kit",
    "kit-add": "a skill, plugin or marketplace added to a kit is given to every later start with that kit",
    "mcp-attach": "an attached MCP server is reached by that start from its next start on (kept for its later "
                  "starts)",
    "mcp-restart": "restarting a running start ends what runs in it now, so that it reaches the MCP server (the agent "
                   "continues its conversation)",
    "host-edit": "changing a host's SSH target, port, key, command or sudo setting changes where and how this "
                 "service's commands run",
    "host-install": "installing sandbox-ai on a host puts this machine's copy (or the signed release) there, for the "
                    "SSH user or for every user, as root or with sudo",
    "secret-delete": "deleting a secret takes it from every start, VPN profile and Git login that names it",
    "secret-replace": "a secret of that name exists: its value is replaced",
    'project-trust': 'a trusted project configuration gives every later start of that project its options',
    'project-forget': 'a project configuration no longer trusted stops the starts of that project from starting until it is trusted again',
    'mcp-stop': 'stopping an MCP server ends it for every start that uses it now',
    'mcp-unsigned': 'an MCP server from a catalog no trusted key signed is taken without that check',
    'mcp-writable-mount': 'a writable mount lets the MCP server change that directory on the host',
    'git-login-add': 'a Git login is used by the connection proxy of every sandbox that reaches that server',
    'login-remove': 'removing a login or an identity takes it from every later start that uses it',
    'net-keep': "keeping what the run was given writes it into the project's .sandbox-ai.json: every later start of that project gets it",
    'start-restart': 'restarting a running start ends what runs in it now (the agent continues its conversation)',
    'canary-tokens': "the run's canary tokens in full: decoys, but shown here they can be copied and planted",
    'canary-resume': 'resuming a sandbox a canary paused lets it go on after something opened a decoy or sent a token away',
    'llm-insecure': 'plain http to an address outside your local networks is unencrypted: prompts, answers and the key can be read on the way',
    'vpn-socks': 'a SOCKS port in the sandbox lets what goes through it pass the connection proxy by',
    'login-logout': 'logging out forgets the stored login of that agent: its sandboxes need a new login',
    'kit-hooks-trust': 'trusted hooks of a kit run in every later start with that kit',
}
APP_NEEDS_CONFIRM = {}
MODE_ORDER = ("lax", "low", "medium", "high", "paranoid")  # the order of protection, weakest first (webui.MODE_ORDER)
TELEMETRY_ORDER = ("on", "off", "strict")  # the agents' telemetry, the least private first (webui.TELEMETRY_ORDER)
DEFAULT_MODE, DEFAULT_TELEMETRY = "medium", "off"
DEFAULT_CANARIES = ("credentials", "home")  # canary.DEFAULT_CATEGORIES, when a host does not say its canary_default
CANARY_CATEGORIES = ("credentials", "home", "workspace")
LIMIT_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([kmgt]?)b?", re.IGNORECASE)


def needs_confirm(action, detail=None):
    """Why ACTION must be confirmed first (the reason of NEEDS_CONFIRM or APP_NEEDS_CONFIRM), or None when it need not
    be: DETAIL says what it does where that decides it - "limits-raise": {"current": limits_shown() or None, "values":
    {"memory", "cpus", "pids": "8g", "none" or ""}}; "defaults-lower": {"old", "new", "canary_default"} (defaults
    values); "workspace-writable": {"read_only": bool}; "secret-replace": {"exists": bool}. Every other action is
    always confirmed. KeyError: an action the table does not have (a bug, never a pass)."""
    reason = NEEDS_CONFIRM.get(action) or APP_NEEDS_CONFIRM[action]
    detail = detail if isinstance(detail, dict) else {}
    if action == "limits-raise":
        return reason if raised_limits(detail.get("current"), detail.get("values") or {}) else None
    if action == "defaults-lower":
        return reason if defaults_lowered(detail.get("old") or {}, detail.get("new") or {},
                                          detail.get("canary_default")) else None
    if action == "workspace-writable" and detail.get("read_only") is True:
        return None
    if action == "secret-replace" and detail.get("exists") is False:
        return None
    return reason


def limit_number(key, value):
    """VALUE of limit KEY ("8g", "1.5", "4096") as a number (bytes, CPUs, processes); None: not a number."""
    text = str(value).strip().lower()
    if key == "memory":
        match = LIMIT_SIZE.fullmatch(text)
        if not match:
            return None
        return float(match.group(1)) * 1024 ** "_kmgt".index(match.group(2) or "_")
    try:
        return float(text)
    except ValueError:
        return None


def raised_limits(current, values):
    """The keys of VALUES ({"memory", "cpus", "pids": text}, "" left as it is) that let the sandbox use more than now:
    "none" (no limit), above CURRENT (limits_shown(); a missing one is no limit), or every one given when CURRENT is
    not known (it cannot be told)."""
    found = []
    for key in ("memory", "cpus", "pids"):
        value = values.get(key)
        if value in (None, ""):
            continue
        number = limit_number(key, value)
        now = current.get(key) if isinstance(current, dict) else None
        if not isinstance(current, dict) or str(value).strip().lower() == "none" or number is None or \
                (isinstance(now, (int, float)) and number > now):
            found.append(key)
    return found


def canary_categories(value, default, categories=CANARY_CATEGORIES):
    """The canary categories VALUE of defaults.json plants (None: DEFAULT, sandbox-ai's own default there), as
    `defaults set canaries` takes it: false, a list, "a,b", "all" or "none"."""
    value = list(default or DEFAULT_CANARIES) if value is None else value
    if value is False:
        return set()
    items = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    names = {str(item).strip() for item in items if str(item).strip()}
    if names == {"all"}:
        return set(categories)
    return set() if names <= {"none"} else names


def defaults_lowered(old, new, canary_default=None):
    """What of the defaults OLD -> NEW ({key: value} of defaults.json, a key missing: sandbox-ai's own default)
    lowers the protection of every later start: [(key, before, after)] - a weaker mode (MODE_ORDER), telemetry less
    private (TELEMETRY_ORDER), canary categories no longer planted (against that host's own CANARY_DEFAULT), and a
    keep_days set where there was none or lowered (kept starts and their HOME go sooner). Empty: nothing does."""
    found = []
    for key, order, default in (("mode", MODE_ORDER, DEFAULT_MODE), ("telemetry", TELEMETRY_ORDER, DEFAULT_TELEMETRY)):
        before, after = old.get(key, default), new.get(key, default)
        if after in order and (before not in order or order.index(after) < order.index(before)):
            found.append((key, before, after))
    lost = canary_categories(old.get("canaries"), canary_default) - canary_categories(new.get("canaries"),
                                                                                       canary_default)
    if lost:
        found.append(("canaries", sorted(lost), new.get("canaries")))
    before, after = old.get("keep_days"), new.get("keep_days")
    if isinstance(after, int) and not isinstance(after, bool) and (
            not isinstance(before, int) or isinstance(before, bool) or after < before):
        found.append(("keep_days", before, after))
    return found


class GcChanged(ValueError):
    """What `sandbox-ai gc` would remove is no longer what was shown and confirmed: nothing was removed."""


def gc_plan(result):
    """What a `sandbox-ai gc --json` result removes, comparable: (kept starts, leftovers, histories), each sorted."""
    result = result if isinstance(result, dict) else {}

    def names(key):
        value = result.get(key)
        return tuple(sorted(str(e.get("name") if isinstance(e, dict) else e) for e in value)) \
            if isinstance(value, list) else ()
    return names("removed"), names("leftovers"), names("histories")


def remote_key(host, ident):
    """The key of the start IDENT (its id, start_id) of another HOST (Instance.key): in a namespace of its own, so it
    never equals a key of this account's own (session:, run:, mcp:, pid:), whatever the host is called. By its id, not
    its name: a start renamed keeps its row, its selection and its tab."""
    return f"host:{host}:{ident}"


def start_id(entry):
    """The id of a start as `sandbox-ai list --json` gives it ({"name", "id"}): "id" (it stays when the start is
    renamed), else its name (an older sandbox-ai)."""
    ident = entry.get("id") if isinstance(entry, dict) else None
    return ident if isinstance(ident, str) and valid_run(ident) else entry["name"]


def url_host(url):
    """The host of a URL in a log record, or ""."""
    match = re.match(r"[a-z]+://([^/?#:@]+)", str(url or ""))
    return match[1] if match else ""


# -- autostart (written only when the human asks for it) --

def autostart_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "sandbox-ai-app.desktop"


def desktop_quote(argument):
    """ARGUMENT for the Exec key of a desktop entry, quoted when it holds a reserved character. Inside the
    quotes ", `, $ and \\ get a backslash, and every backslash is doubled once more, because the value is
    unescaped as a string before it is split (the Desktop Entry Specification)."""
    if any(ord(c) < 32 or ord(c) == 127 for c in str(argument)):
        raise ValueError("a control character (a new line?) has no place in a desktop entry")
    if argument and not re.search(r"[\s\"'\\><~|&;$*?#()`=%]", argument):
        return argument
    quoted = re.sub(r'(["`$\\])', r"\\\1", argument).replace("\\", "\\\\")
    return '"' + quoted.replace("%", "%%") + '"'


PACKAGE_DIR = Path(__file__).resolve().parent  # this running copy of the app


class InsideSandbox(ValueError):
    """The program to start lies in a folder a sandbox can write."""

    def __init__(self, path, root):
        super().__init__(f"{path} lies in {root}, which a sandbox can write")
        self.path, self.root = path, root


def user_site():
    """The user site-packages this app imports from now (e.g. PySide6 installed with pip --user), or None:
    the autostart entry runs Python with -I, which leaves it out, so it is named explicitly."""
    import site
    folder = site.getusersitepackages() if site.ENABLE_USER_SITE else None
    return folder if folder and folder in sys.path and os.path.isdir(folder) else None


# $SANDBOX_AI_APP_PACKAGE_LAUNCHER as the start found it: main() takes it out of the environment, so that the
# programs the app starts (terminals, another app from there) do not inherit it.
PACKAGE_LAUNCHER = None


def packaged_copy(launcher, package=PACKAGE_DIR):
    """True if LAUNCHER (an AppImage, or sandbox-ai.app/Contents/MacOS/sandbox-ai-app) is an executable file and this
    copy of the app is one such a package runs: the AppImage's copy in the cache (.../sandbox-ai/appimage/...), or
    the app inside that very sandbox-ai.app (Contents/Resources)."""
    if not (os.path.isabs(launcher) and os.path.isfile(launcher) and os.access(launcher, os.X_OK)):
        return False
    try:
        here = Path(package).resolve()
        resources = Path(launcher).resolve().parent.parent / "Resources"
    except OSError:
        return False
    if Path(launcher).name == "sandbox-ai-app" and resources.name == "Resources" and resources in here.parents:
        return True
    parts = here.parts
    return any(parts[i:i + 2] == ("sandbox-ai", "appimage") for i in range(len(parts) - 1))


def installed_launcher(environ=None):
    """The stable path of sandbox-ai's `sandbox-ai-app` command (DATA/current/sandbox-ai-app), when that
    command started this app from an installation: an update replaces the versioned folder this copy runs
    from (install.sh keeps two), never that path. Started from the AppImage or the macOS app, that file
    ($SANDBOX_AI_APP_PACKAGE_LAUNCHER): the copy runs with PySide6 from inside it. None otherwise."""
    environ = os.environ if environ is None else environ
    packaged = environ.get("SANDBOX_AI_APP_PACKAGE_LAUNCHER") or PACKAGE_LAUNCHER
    if packaged:  # the AppImage or the macOS app that started this copy (set by their start): it, not the copy
        return packaged if packaged_copy(packaged) else None
    launcher = environ.get("SANDBOX_AI_APP_LAUNCHER", "")
    if not launcher or not os.path.isabs(launcher) or Path(launcher).name != "sandbox-ai-app":
        return None
    try:  # the very copy that runs now, reached through the installation's current link
        here = PACKAGE_DIR.resolve()
        if (Path(launcher).resolve().parent / "projects" / "sandbox-ai-app" / "sandbox_ai_app").resolve() != here:
            return None
    except OSError:
        return None
    return launcher if os.path.isfile(launcher) else None


def autostart_command(executable=None, package=PACKAGE_DIR, extra=None):
    """This running app, by absolute paths (never looked up in $PATH): its Python (absolute, not resolved:
    a virtual environment's python is a link that must stay one), isolated (-I: neither the current
    directory nor PYTHON* variables reach the import path), imports the package from the folder it runs
    from now, hidden in the tray. EXTRA: further import folders (default: the user site, if used now)."""
    launcher = installed_launcher() if executable is None and package == PACKAGE_DIR and extra is None else None
    if launcher:
        return [launcher, "--hidden"]
    executable = os.path.abspath(executable or sys.executable)
    folders = [str(Path(package).resolve().parent)] + ([user_site()] if extra is None and user_site() else
                                                        list(extra or []))
    script = (f"import sys; sys.path[0:0] = {folders!r}; from sandbox_ai_app.main import main; "
              "sys.exit(main())")
    return [executable, "-I", "-c", script, "--hidden"]


def autostart_paths(executable=None, package=PACKAGE_DIR):
    """The files the autostart entry runs: the interpreter (as named and resolved), the package and the
    user site it imports from."""
    launcher = installed_launcher() if executable is None and package == PACKAGE_DIR else None
    executable = os.path.abspath(executable or sys.executable)
    return ([launcher, os.path.realpath(launcher)] if launcher else []) + \
        [executable, os.path.realpath(executable), str(Path(package).resolve())] + \
        ([user_site()] if user_site() else [])


def state_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def preload_trees(environ=None):
    """Folders sandboxes can or could write, found without loading any sandbox-ai module (so it can be
    asked before those are loaded): sandbox-ai's registry exposed.json (workspaces, rw mounts and
    --state-dir of every run so far), the workspaces of running sandboxes (run.json) and of sessions
    (session.json), and the sessions' HOMEs."""
    environ = os.environ if environ is None else environ
    state, found = state_dir(environ), []
    try:
        data = json.loads((state / "exposed.json").read_text())
        found += [p for p in data if isinstance(p, str) and os.path.isabs(p)] if isinstance(data, list) else []
    except (OSError, ValueError, RecursionError):
        pass
    runtime = environ.get("XDG_RUNTIME_DIR")
    runs = (Path(runtime) / "sandbox-ai" if runtime and Path(runtime).is_dir() else state) / "runs"
    for folder, home in ((runs, None), (state / "sessions", "home")):
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                record = json.loads((entry / ("run.json" if home is None else "session.json")).read_text())
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(record, dict) and isinstance(record.get("workspace"), str):
                found.append(record["workspace"])
            if home:
                found.append(str(entry / home))
            elif isinstance(record, dict) and isinstance(record.get("session_dir"), str):
                found.append(str(Path(record["session_dir"]) / "home"))
    return list(dict.fromkeys(os.path.realpath(os.path.expanduser(p)) for p in found))


def check_outside(paths, roots):
    """Refuse (InsideSandbox) when one of PATHS lies in one of ROOTS (folders sandboxes can write) - as
    named and as resolved: a link inside a sandbox folder can be pointed elsewhere by the agent, and a
    link elsewhere may point into one."""
    for path in paths:
        named = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
        for candidate in dict.fromkeys([named, os.path.realpath(path)]):
            for root in roots:
                root = os.path.realpath(root)
                if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
                    raise InsideSandbox(path, root)


def set_autostart(enabled, command=None, forbidden=(), paths=None, also=()):
    """Write (ENABLED) or remove the autostart entry; never follows a link (a link there is replaced).
    The program (and ALSO: the sandbox-ai installation it loads in-process) must not lie in FORBIDDEN (what
    sandboxes can or could write, see SandboxAI.sandbox_folders): a sandbox could change what the session
    starts at the next login."""
    path = autostart_file()
    if not enabled:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return None
    if command is None:
        command, paths = autostart_command(), autostart_paths()
    check_outside(list(paths if paths is not None else [a for a in command if os.path.isabs(a)]) + list(also),
                  forbidden)
    text = ("[Desktop Entry]\nType=Application\nName=sandbox-ai\n"
            "Comment=Answers what running sandboxes ask (in the system tray)\n"
            f"Exec={' '.join(desktop_quote(a) for a in command)}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def autostart_enabled():
    return autostart_file().is_file()


class Pipes:
    """The standard streams of a process started by SandboxAI.call(), served in the calling thread: DATA is
    written to its stdin (then closed), stdout and stderr are read as they come. Nothing blocks: a pipe is
    only read or written when select() says it is ready, so a deadline always holds."""

    def __init__(self, process, data=None):
        self.process = process
        self.output = ([], [])  # chunks of stdout and stderr
        self.chunks = dict(zip((process.stdout.fileno(), process.stderr.fileno()), self.output))
        self.reading = set(self.chunks)  # pipes not at their end yet
        self.data = memoryview(data) if data else None
        for fd in self.chunks:
            os.set_blocking(fd, False)
        if process.stdin is not None:
            if self.data is None:
                self.close_input()
            else:
                os.set_blocking(process.stdin.fileno(), False)

    def close_input(self):
        self.data = None
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def pump(self, deadline):
        """Read (and write) until stdout and stderr reach their end - True - or DEADLINE passes - False."""
        with selectors.DefaultSelector() as selector:
            for fd in self.reading:
                selector.register(fd, selectors.EVENT_READ)
            stdin = self.process.stdin.fileno() if self.data is not None else None
            if stdin is not None:
                selector.register(stdin, selectors.EVENT_WRITE)
            while self.reading:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                for key, _ in selector.select(left):
                    if key.fd == stdin:
                        try:
                            self.data = self.data[os.write(stdin, self.data[:1 << 16]):]
                        except BlockingIOError:
                            continue
                        except OSError:  # it closed its input
                            self.data = None
                        if not self.data:
                            selector.unregister(stdin)
                            stdin = None
                            self.close_input()
                        continue
                    try:
                        chunk = os.read(key.fd, 1 << 16)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if chunk:
                        self.chunks[key.fd].append(chunk)
                    else:
                        selector.unregister(key.fd)
                        self.reading.discard(key.fd)
        return True

    def close(self):
        """Close every pipe (no other thread uses them: this cannot block)."""
        self.close_input()
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def text(self):
        """[stdout, stderr] as read so far."""
        return [b"".join(chunks).decode("utf-8", "replace") for chunks in self.output]


FOLDER_WAIT = 0.3  # seconds is_folder() waits for the disk
_folder_checks, _folder_lock = {}, threading.Lock()


def is_folder(path, wait=FOLDER_WAIT):
    """os.path.isdir(PATH) if the disk answers within WAIT seconds, else None (the caller looks again in
    the background). A stat that hangs (a network file system) keeps one thread per path, not one per call."""
    path = str(path)
    with _folder_lock:
        check = _folder_checks.get(path)
        if check is None:
            found = {}
            thread = threading.Thread(target=lambda: found.update(folder=os.path.isdir(path)), daemon=True)
            check = _folder_checks[path] = (thread, found)
            thread.start()
    thread, found = check
    thread.join(wait)
    if thread.is_alive():
        return None
    with _folder_lock:
        if _folder_checks.get(path) is check:
            del _folder_checks[path]
    return found.get("folder", False)


def lead_exited(process):
    """True if the lead of PROCESS has exited; it is not reaped here (WNOWAIT), so its number stays taken."""
    if process.returncode is not None:
        return True
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def signal_group(pgid, number):
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass


def group_alive(pgid, proc=Path("/proc")):
    """True while a process of group PGID is left that is not a zombie."""
    try:
        entries = [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError:
        return False
    for entry in entries:
        try:
            fields = (proc / entry / "stat").read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
            return True
    return False


def settings_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "app" / "settings.json"


def app_settings():
    """The app's settings ({} without them); readable before sandbox-ai is located (its location is one)."""
    try:
        data = json.loads(settings_file().read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def settings_problem():
    """Why the app's settings.json cannot be used (it is then moved aside and the defaults apply), or None."""
    path = settings_file()
    try:
        with open(path, "rb") as stream:
            data = json.loads(stream.read(1 << 20))
        if isinstance(data, dict):
            return None
        problem = "not a JSON object"
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{path}: {exc.strerror or exc}"
    except (ValueError, RecursionError, UnicodeDecodeError) as exc:
        problem = str(exc)
    aside = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(path, aside)
    except OSError:
        return f"{path}: {problem}"
    return f"{path}: {problem} (kept as {aside.name})"


# The settings the app knows, with their defaults (the Settings dialog edits them; the file keeps them).
SETTINGS = {"theme": "system", "language": "system", "terminal": "app", "tray": True, "notify": True,
            "notify_seconds": 15, "notify_style": "themed", "approvals_interval": 1000, "sandboxes_interval": 3000, "connections_interval": 2000,
            "sandbox_ai_home": "", "default_agent": "claude", "default_backend": "podman",
            "default_template": "development", "board_grid": 20, "auto_tabs": True, "guide_nodes": True,
            "guide_tour": True, "intro": True, "debug": False, "help_buttons": True}


def setting(name, settings=None):
    """The value of setting NAME, of the type of its default (a damaged value falls back to the default)."""
    settings = app_settings() if settings is None else settings
    default = SETTINGS[name]
    value = settings.get(name, default)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3_600_000 else default
    return value if isinstance(value, str) else default


def sandbox_ai_home_override(environ=None):
    """The checkout chosen in the settings, for this start: used only while $SANDBOX_AI_HOME is unset."""
    environ = os.environ if environ is None else environ
    if environ.get("SANDBOX_AI_HOME"):
        return None
    return setting("sandbox_ai_home") or None


def write_private_json(path, data):
    """Write DATA as JSON to PATH, mode 0600 from the start (never briefly readable by others)."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parse_dry_run(status, out, err):
    """(plan or None, message) of `sandbox-ai AGENT ... --dry-run`."""
    if status != 0:
        return None, err.strip() or out.strip()
    try:
        return json.loads(out), err.strip()
    except ValueError:
        return None, out.strip()


class Option:
    """One launcher option, as the form shows it."""

    def __init__(self, long, short, kind, help, metavar=None, choices=None, default=None, const=None, dest=None):
        self.long, self.short, self.kind, self.help = long, short, kind, help or ""
        self.metavar, self.choices, self.default, self.const, self.dest = metavar, choices, default, const, dest

    @classmethod
    def from_action(cls, action, long, short):
        if action.nargs == 0:
            kind = "flag"
        elif isinstance(action, argparse._AppendAction):
            kind = "multi" if action.choices else "list"
        elif action.choices:
            kind = "choice"
        else:
            kind = "value"
        choices = list(action.choices) if action.choices else None
        return cls(long, short, kind, action.help, action.metavar, choices, action.default, getattr(action, "const", None),
                   action.dest)

    @property
    def group(self):
        return next((title for title, names in GROUPS.items() if self.long in names), "More")

    @property
    def is_path(self):
        return self.long in PATH_OPTIONS

    def tokens(self, value):
        """The command-line tokens for VALUE (bool for a flag, text for a value, [texts] for a list)."""
        if self.kind == "flag":
            return [self.long] if value else []
        if self.kind in ("list", "multi"):
            return [token for item in value or [] if str(item).strip() for token in (self.long, str(item).strip())]
        text = "" if value is None else str(value).strip()
        if not text or (self.kind == "choice" and text == str(self.default)):
            return []
        return [self.long, text]


def argv_from_values(options, values):
    """Launcher arguments from {long option: value}, in the order of OPTIONS."""
    argv = []
    for option in options:
        if option.long in values:
            argv += option.tokens(values[option.long])
    return argv


def values_from_argv(options, argv, canonical=None):
    """{long option: value} of an argument list (short forms included); unknown tokens are returned too."""
    by_name = {o.long: o for o in options}
    tokens = canonical(list(argv)) if canonical else list(argv)
    values, unknown, index = {}, [], 0
    while index < len(tokens):
        token = tokens[index]
        name, equals, attached = token.partition("=")
        option = by_name.get(name)
        if option is None:
            unknown.append(token)
            index += 1
            continue
        if option.kind == "flag":
            values[name] = True
            index += 1
            continue
        if equals:
            value, index = attached, index + 1
        elif index + 1 < len(tokens):
            value, index = tokens[index + 1], index + 2
        else:
            unknown.append(token)
            index += 1
            continue
        if option.kind in ("list", "multi"):
            values.setdefault(name, []).append(value)
        else:
            values[name] = value
    return values, unknown


LOG_BYTES = 16 << 20  # the newest part of a connection log that is read


def open_log(path):
    """A descriptor of the regular file PATH, opened without following a link or blocking on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        os.set_blocking(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def log_state(path):
    """(inode, size, mtime) of PATH, without following a link; None if it cannot be read. A change of
    any means the log has to be read again (records are appended; a rotation replaces the file)."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, TypeError):
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns


def read_connections(path, limit=5000, max_bytes=LOG_BYTES, strict=False, since=None, track=False):
    """The newest LIMIT records of a connections-*.jsonl log, read from at most its last MAX_BYTES
    (damaged lines and a cut first line skipped). Links, FIFOs and devices are not read: [] for them, or
    OSError with STRICT (so a caller can tell an empty log from one it could not read).

    With TRACK: (records, position, appended). POSITION (inode, offset after the last complete line) is
    where this read ended; given as SINCE, only the complete lines appended after it are read (APPENDED is
    then True) - unless the file was replaced or cut, or more than MAX_BYTES were appended: then it is read
    as without SINCE. Only complete lines count with TRACK (a line being written is read the next time)."""
    try:
        fd = open_log(path)
    except (OSError, TypeError):
        if strict:
            raise
        return ([], None, False) if track else []
    appended = False
    try:
        info = os.fstat(fd)
        size = info.st_size
        start = max(0, size - max_bytes)
        if track and since is not None and since[0] == info.st_ino and start <= since[1] <= size:
            start, appended = since[1], True
        os.lseek(fd, start, os.SEEK_SET)
        chunks, left = [], size - start
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    end = start + len(data)
    if track:
        complete = data.rfind(b"\n") + 1
        data, end = data[:complete], start + complete
    lines = data.split(b"\n")
    if start > 0 and lines and not appended:
        lines = lines[1:]  # begins in the middle of a line
    records = []
    for line in reversed(lines):  # newest first, until LIMIT good records
        if len(records) >= limit:
            break
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    if track:
        return records[::-1], (info.st_ino, end), appended
    return records[::-1]


def summary(record):
    """(time, event, method or protocol, status, target, bytes) of a log record for the table."""
    event = record.get("event", "")
    push = record.get("push")
    if isinstance(push, dict):
        event = "push " + ("ok" if push.get("ok") else "REFUSED")
    target = record.get("url") or record.get("host") or record.get("destination") or ""
    why = record.get("reason") or record.get("error")
    if why and event in ("denied", "error", "tls-refused", "tls-upstream-failed"):
        target = f"{target}  ({why})"
    size = (record.get("bytes_out") or 0) + (record.get("bytes_in") or 0)
    return (str(record.get("time", "")), event, str(record.get("method") or record.get("protocol") or ""),
            str(record.get("status") or ""), target, str(size) if size else "")


# -- paths of runs and sessions --

# Without the launcher's parser: which of the options read here take a value.
KNOWN_ARITY = {"--workspace": True, "--state-dir": True, "--workspace-ro": False}
_arity = {}  # id(launcher): (launcher, {option string: nargs})


def option_arity(launcher):
    """{option string (long and short): nargs} from the launcher's own parser (0 for a flag, None for one
    value, "?" for an optional one), or {} if it cannot be built."""
    cached = _arity.get(id(launcher))
    if cached is not None and cached[0] is launcher:
        return cached[1]
    try:
        found = {option: action.nargs for action in launcher.build_parser()._actions
                 for option in action.option_strings}
    except Exception:  # noqa: BLE001 - an old launcher or a test double
        found = {}
    _arity[id(launcher)] = (launcher, found)
    return found


def paths_from_options(launcher, tokens, cwd=None):
    """{"workspaces": [...], "state_dir": path or None} from launcher options (short forms included);
    relative paths are taken relative to CWD, as the launcher took them. Whether an option takes a value
    comes from the launcher's parser (--workspace-ro is a flag: the token after it is an option of its own)."""
    tokens = list(tokens)
    if "--" in tokens:
        tokens = tokens[:tokens.index("--")]
    try:
        tokens = launcher.canonical_options(tokens)
    except Exception:  # noqa: BLE001 - an old or damaged record: read what can be read
        pass
    arity = {**KNOWN_ARITY, **option_arity(launcher)}
    result = {"workspaces": [], "state_dir": None, "mounts": []}
    base = Path(cwd) if cwd else None

    def path(value):
        value = Path(os.path.expanduser(value))
        return str(value if value.is_absolute() or base is None else base / value)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, equals, attached = token.partition("=")
        index += 1
        if not name.startswith("-") or name not in arity:
            continue  # a value of an option not known here, or an unknown option: nothing to take
        nargs = arity[name]
        if nargs == 0:
            continue
        if equals:
            value = attached
        elif index < len(tokens) and not (nargs == "?" and tokens[index].startswith("-")):
            value, index = tokens[index], index + 1
        else:
            continue
        if name == "--state-dir":
            result["state_dir"] = path(value)
        elif name == "--workspace":
            result["workspaces"] += [path(v) for v in value.split(",") if v.strip()]
        elif name == "--mount":
            source, _, mode = value.rpartition(":")
            source = source.rpartition(":")[0]  # SRC:DEST:MODE
            if mode == "rw" and source:
                result["mounts"].append(path(source))
    return result


def launcher_agents(launcher):
    try:
        return tuple(launcher.AUTH_TARGETS)
    except AttributeError:
        return AGENTS


def run_paths(launcher, pid, proc=Path("/proc")):
    """Workspaces, --state-dir and rw --mount sources of a running launcher, from its command line in /proc (only for a
    process of this user); empty when that cannot be read."""
    empty = {"workspaces": [], "state_dir": None, "mounts": []}
    try:
        pid = int(pid)
        directory = proc / str(pid)
        if directory.stat().st_uid != os.getuid():
            return empty
        argv = [a.decode("utf-8", "surrogateescape") for a in (directory / "cmdline").read_bytes().split(b"\0") if a]
        cwd = os.readlink(directory / "cwd")
    except (OSError, ValueError, TypeError):
        return empty
    for index, token in enumerate(argv):
        if token in launcher_agents(launcher) and index > 0 and argv[index - 1].endswith(("agent_sandbox.py", "-sandbox", "sandbox-ai")):
            return paths_from_options(launcher, argv[index + 1:], cwd)
    return empty


class Instance:
    """One sandbox-ai instance on this account (see SandboxAI.instances). KIND: "run" (a one-off start),
    "session", "mcp" (an MCP server's sandbox) or "unregistered" (a launcher process not in the registry);
    RUN: its name in the run registry while it runs (what `sandbox-ai net`, `devices` and `limits` take)."""

    def __init__(self, kind, key, name, run=None, session=None, mcp=None, project="", agent="", backend="",
                 started="", state="", proxy=None, pid=None, record=None, foreground=False, host="local", ident=None):
        self.kind, self.key, self.name, self.run, self.session, self.mcp = kind, key, name, run, session, mcp
        # Its id, which stays when it is renamed (a session's directory, a start's id there): what KEY is made of.
        self.ident = ident or name
        self.host = host  # "local" (this account) or the name of a host of `sandbox-ai hosts` (read over SSH)
        self.project, self.agent, self.backend, self.started, self.state = project, agent, backend, started, state
        self.proxy, self.pid, self.record = proxy, pid, record or {}
        self.foreground = foreground  # a kept start without tmux: nothing to attach to

    def __repr__(self):
        return f"Instance({self.kind!r}, {self.key!r}, state={self.state!r})"


def memory_share(instance):
    """(percent, used bytes, limit bytes) of a local running sandbox from the memory.json its launcher's memory
    watch writes into the session directory (the sandbox sees it read-only), or None. Numbers only are taken."""
    record = getattr(instance, "record", None) or {}
    directory = record.get("session_dir") if isinstance(record, dict) else None
    if getattr(instance, "host", "local") != "local" or not isinstance(directory, str):
        return None
    try:
        with open(Path(directory) / "memory.json", "rb") as stream:
            data = json.loads(stream.read(4096))
        used, limit = int(data["used_bytes"]), int(data["limit_bytes"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if used < 0 or limit <= 0:
        return None
    return round(100 * used / limit, 1), used, limit


def option_value(argv, name):
    """The value of option NAME in ARGV (--name VALUE or --name=VALUE), the last one given, or None."""
    value = None
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == name and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith(name + "="):
            value = token.partition("=")[2]
    return value


def process_started(pid, proc=Path("/proc")):
    """When process PID started, as sandbox-ai writes times (UTC), or None."""
    try:
        ticks = int((proc / str(int(pid)) / "stat").read_text().rpartition(")")[2].split()[19])
        boot = next(int(line.split()[1]) for line in (proc / "stat").read_text().splitlines() if line.startswith("btime "))
        moment = boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, TypeError):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def launcher_processes(agents=AGENTS, proc=Path("/proc")):
    """[(pid, agent, launcher options, cwd)] of sandbox-ai launchers of this user (python ... agent_sandbox.py
    AGENT ...), dry runs left out (they start nothing). Read from /proc; processes of others are skipped."""
    found = []
    try:
        entries = [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError:
        return found
    for entry in entries:
        directory = proc / entry
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            argv = [a.decode("utf-8", "surrogateescape") for a in (directory / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        for index, token in enumerate(argv[:-1]):
            if token.endswith("agent_sandbox.py") and argv[index + 1] in agents:
                options = argv[index + 2:]
                if "--dry-run" in options[:options.index("--")] if "--" in options else "--dry-run" in options:
                    break
                try:
                    cwd = os.readlink(directory / "cwd")
                except OSError:
                    cwd = ""
                found.append((int(entry), argv[index + 1], options, cwd))
                break
    return sorted(found)


def merge_recent(candidates, limit=RECENT_FOLDERS):
    """[(folder, when)] of CANDIDATES ([(path, seconds or None)]): each folder once (by its real path, with its
    latest time), only those that exist, newest first (those without a time after, in the order given), at most
    LIMIT."""
    best, order = {}, []
    for path, when in candidates:
        if not isinstance(path, str) or not path or "\0" in path:
            continue
        real = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(real):
            continue
        if real not in best:
            order.append(real)
            best[real] = when
        elif when and (best[real] or 0) < when:
            best[real] = when
    ranked = sorted(order, key=lambda p: (best[p] is None, -(best[p] or 0), order.index(p)))
    return [(path, best[path]) for path in ranked[:limit]]


def home_short(path):
    """PATH with the HOME at its start shown as ~."""
    home = str(Path.home())
    path = str(path)
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def runs_of_starts(runs, ids):
    """{start's name: its run} - which running sandbox of RUNS ({run: run.json record}) is the run of which kept start
    (IDS: {name: id} of the start directories), as sandbox-ai's own list merges them (starts.entries): by the
    record's "start" (its start id, since 0.1.30), by "session" (the name, or agent-session-NAME), or by the run
    name session-ID of older versions. A start and its run are one row, never a stopped start beside a running
    run."""
    names = {ident: name for name, ident in ids.items()}
    found = {}
    for run, record in runs.items():
        record = record if isinstance(record, dict) else {}
        start = record.get("start") if isinstance(record.get("start"), str) else ""
        name = names.get(start) or (start if start in ids else None) or session_of(record) or (
            names.get(run[len("session-"):]) if str(run).startswith("session-") else None)
        if name:
            found[name] = run
    return found


def session_of(record):
    """The session NAME of a run record, or None. A session's sandbox registers with its pod name
    (agent-session-NAME, see sessions.launch); `--session` runs carry the name itself."""
    value = record.get("session")
    if not isinstance(value, str) or not value:
        return None
    return value[len("agent-session-"):] if value.startswith("agent-session-") else value


# -- approvals --

class QuestionCache:
    """Questions of the run directories, parsed once per file."""

    def __init__(self, ident=re.compile(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}"), limit=64 << 10):
        self.ident, self.limit, self.parsed, self.results = ident, limit, {}, {}

    def _json(self, path, store):
        key = str(path)
        if key not in store:
            try:
                with open(path, "rb") as stream:  # in the run's control directory: not the agent's
                    data = stream.read(self.limit + 1)
                value = json.loads(data) if len(data) <= self.limit else None
            except (OSError, ValueError, RecursionError):
                return None  # not cached: it may be in the middle of being written
            store[key] = value if isinstance(value, dict) else None
        return store[key]

    def scan(self, runs):
        pending, answered, live = [], [], set()
        for name, record in runs.items():
            directory = Path(record.get("directory", "")) / "approvals"
            try:
                with os.scandir(directory) as entries:
                    names = {entry.name for entry in entries}
            except OSError:
                continue
            for file in sorted(names):
                stem, dot, suffix = file.rpartition(".")
                if suffix != "json" or not self.ident.fullmatch(stem):
                    continue
                path = directory / file
                live.add(str(path))
                question = self._json(path, self.parsed)
                if question is None or question.get("id") != stem:
                    continue
                if f"{stem}.done" in names:
                    result = self._json(directory / f"{stem}.done", self.results)
                    answered.append((name, record, {**question, "result": result or {}}))
                elif f"{stem}.answer" in names:
                    answered.append((name, record, {**question, "result": {"reason": "answered, being applied"}}))
                else:
                    pending.append((name, record, question))
        for store in (self.parsed, self.results):  # forget the files of ended runs
            for key in [k for k in store if k.rsplit(".", 1)[0] + ".json" not in live]:
                del store[key]
        return pending, answered


class RemoteQuestion(dict):
    """An open question of a sandbox on another HOST, as `sandbox-ai approvals list --json` there lists it: the
    question's text comes from that host's sandbox-ai ("question", escaped there), the time left from its own clock
    ("seconds_left" when it was read, READ_AT: time.time() then here; null: no deadline, as for a page to open). Only
    these fields are kept, checked. "max_scope" null with "allow_refused" (why): no allow is offered; a page that host
    will not let be opened elsewhere has no "url" but "url_refused" (why) and "url_host" (where it leads);
    "url_truncated": the URL is not whole."""

    FIELDS = {"id": 40, "run": 128, "sandbox": 400, "kind": 20, "time": 40, "question": 2000, "always": 400,
              "url": 65536, "max_scope": 10, "allow_refused": 400, "url_refused": 400, "url_host": 255}

    def __init__(self, host, item, read_at=None):
        super().__init__()
        self.host, self.read_at = host, time.time() if read_at is None else read_at
        for key, limit in self.FIELDS.items():
            if isinstance(item.get(key), str):
                self[key] = printable(item[key])[:limit]
        for key in ("timeout", "seconds_left"):
            value = item.get(key)
            self[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and \
                0 <= value <= 86400 else None
        if "max_scope" in item and item["max_scope"] is None:
            # That host allows no answer but a deny (its policy, the run's mode): said why, never offered here.
            self["max_scope"] = None
            self.setdefault("allow_refused", "its host allows only a deny")
        elif self.get("max_scope") not in SCOPES:
            self["max_scope"] = "always"  # an older sandbox-ai there: it refuses what its policy does not allow
        if item.get("url_truncated") is True:
            self["url_truncated"] = True
        if self.get("kind") != "browser":
            for key in ("url", "url_refused", "url_host", "url_truncated"):
                self.pop(key, None)

    def timing(self, now=None):
        """(age in seconds or None, seconds left or None) as question_timing gives them, counted on from
        seconds_left (the other host's clock never matters)."""
        now = time.time() if now is None else now
        elapsed = max(0, now - self.read_at)
        left, timeout = self.get("seconds_left"), self.get("timeout")
        age = None if left is None or timeout is None else max(0, int(timeout - left + elapsed))
        if self.get("kind") == "browser" or left is None:
            return age, None
        return age, max(0, int(left - elapsed))


def remote_questions(host, items, ident=re.compile(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}"), read_at=None):
    """[RemoteQuestion] of HOST from the JSON list ITEMS; items without a question id or run name of sandbox-ai
    are left out (both go back to that host as arguments: never one that reads as an option)."""
    found = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ident.fullmatch(item["id"]) \
                or not valid_run(item.get("run")):
            continue
        found.append(RemoteQuestion(host, item, read_at))
    return found


def parse_time(text):
    """Seconds since the epoch of a UTC time as sandbox-ai writes it (2026-11-03T10:00:00Z), or None."""
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def question_timing(question, now=None, default=60):
    """(age in seconds or None, seconds left before sandbox-ai refuses it, or None when nobody waits
    for an answer: questions to open a URL stay until they are answered)."""
    now = time.time() if now is None else now
    asked = parse_time(question.get("time"))
    age = None if asked is None else max(0, int(now - asked))
    if question.get("kind") == "browser":
        return age, None
    timeout = question.get("timeout", default)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = default
    return age, (None if age is None else max(0, int(timeout - age)))


def question_url(question):
    """The URL of a question to open a browser, for display (escaped), or None."""
    return printable(question.get("url", "")) if question.get("kind") == "browser" else None


# -- update check --

def parse_update_check(status, out):
    """(installed, available) versions from `sandbox-ai update --check`, or None if it did not work
    (no network, not an installation made by install.sh)."""
    if status != 0:
        return None
    installed = re.search(r"^installed:\s*v?([0-9][0-9A-Za-z.\-+]*)", out, re.M)
    available = re.search(r"^available:\s*v?([0-9][0-9A-Za-z.\-+]*)", out, re.M)
    if not installed or not available:
        return None
    return installed[1], available[1]


def version_key(version):
    """Comparable key of 0.1.21, v0.1.21, 0.1.21rc1 (a pre-release sorts before its release)."""
    match = re.fullmatch(r"v?([0-9]+(?:\.[0-9]+)*)(?:[-.]?(a|alpha|b|beta|rc|dev)\.?([0-9]*))?(?:\+.*)?", str(version))
    if not match:
        raise ValueError(f"not a version: {version!r}")
    numbers = tuple(int(n) for n in match[1].split("."))
    numbers += (0,) * (4 - len(numbers))
    stage = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3}.get(match[2], 4)
    return numbers + (stage, int(match[3] or 0))


def newer(available, installed):
    try:
        return version_key(available) > version_key(installed)
    except (TypeError, ValueError):
        return False


# -- images --

def image_rows(document):
    """Table rows of `sandbox-ai images --json`: (agent, present, agent version, created, size, image)."""
    rows = []
    for image in document if isinstance(document, list) else []:
        if not isinstance(image, dict):
            continue
        size = image.get("size") or 0
        rows.append((str(image.get("agent", "")), bool(image.get("present")), str(image.get("agent_version") or ""),
                     str(image.get("created") or "").replace("T", " "),
                     f"{size / 1e9:.2f} GB" if isinstance(size, (int, float)) and size else "",
                     str(image.get("image", ""))))
    return rows


def package_rows(document):
    return [(str(p.get("kind", "")), str(p.get("name", "")), str(p.get("version", "")))
            for p in (document if isinstance(document, list) else []) if isinstance(p, dict)]


# -- statistics --

def split_credential(entry):
    """("Authorization", "api.example.org") of a telemetry entry "Authorization -> api.example.org" (a
    header name has no spaces, a host may be anything the agent sent: split at the first arrow)."""
    name, arrow, host = str(entry).partition(" -> ")
    return (printable(name), printable(host)) if arrow else (printable(entry), "")
