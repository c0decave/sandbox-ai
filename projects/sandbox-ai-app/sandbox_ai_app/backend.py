"""What sandbox-ai-app knows about sandbox-ai, without any Qt: where it is installed, its options,
running sandboxes, sessions, logs, MCP servers, checks.

Reading is done with sandbox-ai's own modules (loaded from its installation, never copied);
every action runs the sandbox-ai command itself, so the app can do nothing the command line
cannot, and the command line's checks apply to everything the app does.
"""
from __future__ import annotations

import argparse
import calendar
import codecs
import contextlib
import functools
import gzip
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import secrets
import selectors
import shlex
import tempfile
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import weakref
import zlib

from . import applog
from .files import printable, printable_text

# Fallbacks only, for the moments before an installation is loaded; SandboxAI reads the real values from
# sandbox-ai's own modules (the launcher's parser, approvals.ID, secret_store.NAME and LIMIT, ...).
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")
# A team's members: sandbox-ai's team.AGENTS - every agent since 0.1.98 (OpenHands, nanobot, Letta Code, OpenCode 2
# and Pi too; a test keeps both the same).
TEAM_AGENTS = ("claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta",
               "opencode2", "pi")
LOGIN_AGENTS = ("claude", "codex", "opencode")  # `sandbox-ai login AGENT` of a sandbox-ai without login.AGENTS
# The agents `sandbox-ai login` takes in this release (login.py's AGENTS): DSH, Goose, nanobot and Letta Code have no
# login command, on no host - a terminal with `sandbox-ai login goose` would only fail there.
LOGIN_AGENTS_ANYWHERE = ("claude", "codex", "opencode", "hermes", "qwen", "pi")
MIN_VERSION = "0.1.21"  # the first release with approvals, secrets, images and log telemetry
PLUGINS_WAIT = 1800  # seconds `sandbox-ai plugins claude` may take (each marketplace and plugin is fetched)
TRANSFER_SECONDS = 1200  # sandbox-ai session copy|move: its dry run there (up to 180 s) and the start (up to 900 s)
STATE_SECONDS = 7200  # ... with its state: the archive sent over SSH and imported there (session_state.transfer)
BUNDLE_SECONDS = 3600  # sandbox-ai board export|import: with --images, image archives saved, sent and loaded
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
# macOS: a terminal window of Terminal (or iTerm, SANDBOX_AI_TERMINAL=iTerm) through AppleScript. The shell command is
# the script's argument (on run argv), never part of its source: nothing in it is read as AppleScript.
MAC_TERMINALS = {
    # do script types the line into the user's login shell (zsh, fish, tcsh ...), which would read the command's
    # quotes its own way: it gets only `exec /bin/sh FILE` (mac_terminal), the command waiting in FILE.
    "Terminal": ("on run argv", 'tell application "Terminal"', "activate", "do script (item 1 of argv)", "end tell",
                 "end run"),
    "iTerm": ("on run argv", 'tell application "iTerm"', "activate",
              "create window with default profile command (\"/bin/sh -c \" & quoted form of (item 1 of argv))",
              "end tell", "end run"),
}


def mac_terminal(script, iterm=False, directory=None):
    """argv of osascript that opens a terminal window of Terminal (ITERM: iTerm) running the shell command SCRIPT.
    Terminal types its line into the login shell: SCRIPT goes into a private file (DIRECTORY: a new one of mkdtemp,
    0700) that /bin/sh runs and removes; the line typed holds only that file's path."""
    lines = MAC_TERMINALS["iTerm" if iterm else "Terminal"]
    if not iterm:
        folder = Path(directory or tempfile.mkdtemp(prefix="sandbox-ai-terminal-"))
        path = folder / "run.sh"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write('rm -f -- "$0"; rmdir -- "${0%/*}" 2>/dev/null\n' + script + "\n")
        script = f"exec /bin/sh {shlex.quote(str(path))}"
    return ["/usr/bin/osascript", *(part for line in lines for part in ("-e", line)), "--", script]
# Option groups of the launcher, in the order the app shows them.
GROUPS = {
    "Workspace": ["--workspace", "--workspace-ro", "--hide", "--mount", "--state-dir", "--copy", "--name", "--name-style",
                  "--project-name", "--keep", "--no-keep", "--no-checkpoint", "--checkpoint-every",
                  "--checkpoint-mounts"],
    "Network": ["--ask", "--ask-timeout", "--profile", "--allow-host", "--allow-domain", "--allow-shared-wildcard",
                "--allow-http", "--allow-pypi", "--allow-github", "--allow-gitlab",
                "--allow-kernel-org", "--allow-duckduckgo", "--allow-npm", "--allow-tcp", "--allow", "--allow-host-port",
                "--frontier", "--no-provider-defaults", "--dns", "--nameserver", "--egress-all-allow", "--offline",
                "--network", "--ip", "--ip6", "--subnet", "--subnet6", "--vpn", "--vpn-check", "--vpn-bypass-host-ports", "--vpn-bypass-tcp", "--llm", "--use-case", "--llm-models", "--llm-param", "--llm-tools", "--no-llm-profile", "--llm-traffic", "--llm-traffic-limit"],
    "Proxy & privacy": ["--mode", "--mode-at-least", "--no-proxy", "--no-inspect", "--rules", "--privacy-check", "--privacy-rules",
                        "--canaries", "--no-canaries", "--allow-package", "--no-package-guard", "--allow-send",
                        "--telemetry",
                        "--log-dir", "--log-debug", "--budget"],
    "Git": ["--identity", "--git-identity", "--git-token-env", "--git-token-file", "--git-token-secret", "--git-logins",
            "--token-proxy", "--git-credentials"],
    "Agent": ["--model", "--effort", "--show-thinking", "--preset", "--preset-value", "--dangerously-skip-permissions", "--mcp", "--mcp-limits", "--mcp-mount", "--mcp-env", "--service", "--helper", "--with", "--kit", "--kit-dir", "--no-default-kit",
              "--no-memory", "--no-plugins", "--local-config", "--env", "--secret-env", "--api-key", "--publish",
              "--hermes-enable", "--qwen-enable", "--goose-enable", "--openhands-enable", "--nanobot-enable", "--letta-enable",
              "--opencode2-enable",
              "--pi-enable"],
    "Login": ["--copy-auth", "--no-auth", "--auth-file", "--auth-from-session", "--import-codex-auth"],
    "Display & terminal": ["--gui", "--gui-vnc", "--gui-screen", "--gui-wayland", "--browser", "--browser-control",
                           "--browser-profile", "--audio", "--tmux", "--no-tmux"],
    "Cairn": ["--cairn", "--cairn-workers", "--cairn-origin", "--cairn-goal", "--cairn-hint", "--cairn-board"],
    "ARTEX": ["--artex", "--artex-data", "--artex-port"],
    "Hardware": ["--device", "--gpu"],
    "Limits": ["--memory", "--cpus", "--pids", "--tmp-size"],
    "Engine": ["--backend", "--image", "--image-variant", "--nested-containers", "--podman-url", "--binary", "--runtime", "--config", "--no-config", "--probe",
               "--toolchain", "--toolchain-offline"],
}
PATH_OPTIONS = {"--workspace", "--state-dir", "--privacy-rules", "--rules", "--log-dir", "--git-credentials", "--local-config",
                "--auth-file", "--import-codex-auth", "--gui-vnc", "--kit-dir", "--config", "--runtime", "--device"}
# Options the app sets itself (it starts sessions and dry runs on its own).
# --yes: the start's own risk question answered unseen - the app asks itself (ask_first "shared-wildcard") and passes
# what was confirmed as --allow-shared-wildcard, never --yes.
# --fix, --fix-only, --fix-plan: what a start lacks - the app reads the plan and asks itself (prerequisites.py).
HIDDEN = {"--session", "--dry-run", "--help", "--version", "--yes", "--fix", "--fix-only", "--fix-plan"}
# A hidden launcher option the app adds to its own starts on this machine (SandboxAI.as_given).
AS_GIVEN = "--workspaces-as-given"
SCOPES = ("once", "run", "always")
REMOTE_QUESTION_WAIT = 30  # seconds a host may take to list its open questions (over SSH)
# The Files page's folders of a host's sandboxes (host_file_roots: `sandbox-ai list --folders --json` there): how long
# the host has to answer, what each entry is (as file_roots names this machine's), how many are taken.
HOST_FOLDERS_WAIT = 20
HOST_FOLDER_KINDS = ("run", "run-home", "session", "session-home")
HOST_FOLDERS_LIMIT = 500
# How long a host's `COMMAND --help` is kept when its sandbox-ai version is not known (kept per version otherwise; an
# update there drops it, and an option it lacks is asked once more before anything is refused: require_options).
NET_OPTIONS_SECONDS = 120
BASE_NET_OPTIONS = frozenset({"--allow-host", "--allow-tcp", "--allow"})  # what every sandbox-ai since 0.1.70 takes
RECENT_PROJECTS = 12  # projects the app's own list keeps
RECENT_FOLDERS = 10  # recent project folders the wizard offers
MASTER_TRUSTED = 30  # seconds a host's shared SSH connection that carried a command is used without `ssh -O check`
PODS_CACHE_SECONDS = 10  # how long the app keeps a Podman listing (sandbox-ai's own: 2 s); forget_pods() drops it
KIT_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"  # kits.KIT_NAME
AGENT_PART = re.compile(r"[a-z0-9]{2,12}:[a-z-]{3,16}:[^\x00-\x1f\x7f]{1,500}")  # an id of sandbox-ai kits host-list
SKILL_NAME = r"[a-z0-9][a-z0-9-]{0,63}"  # a new skill's name, as Claude wants it
DRAIN = 2  # seconds call() still reads the output of a command it ended at its timeout
CANCELLED = -2  # the exit status call() gives a command cancelled inside watching() (as Task.run says it)
CANCEL_POLL = 0.1  # seconds between two looks at a watching() call's cancel
KIT_ACTIONS = {"show", "new", "add-skill", "new-skill", "add-plugin", "add-marketplace", "remove-skill",
               "remove-plugin", "add-hook", "remove-hook", "delete", "enable-plugin", "disable-plugin",
               "remove-marketplace", "import-marketplace"}
# What came with sandbox-ai 0.1.78 (with `plugins marketplaces` and `plugins --marketplaces-only`): the kit actions
# below, and the plugins and marketplaces of the default kit changed by `sandbox-ai kits` (webui's KIT_NEW_ACTIONS and
# KIT_DEFAULT_ACTIONS). Another host is asked first (require_marketplaces) and an older one told to update.
KIT_NEW_ACTIONS = {"enable-plugin", "disable-plugin", "remove-marketplace", "import-marketplace"}
KIT_DEFAULT_ACTIONS = {"add-plugin", "remove-plugin", "enable-plugin", "disable-plugin", "add-marketplace",
                       "remove-marketplace", "import-marketplace"}


class Unavailable(RuntimeError):
    """sandbox-ai is not installed where the app can find it."""


def locate(which=shutil.which, run=subprocess.run, forbidden=None, package=None):
    """The directory with sandbox-ai's modules (agent_sandbox.py and main.py).

    With $SANDBOX_AI_HOME set, only there: the sandbox-ai command on $PATH is not even looked up. Otherwise the
    sandbox_ai beside this app's package (PACKAGE, default PACKAGE_DIR) when there is one - the PyPI package
    sandbox-ai-app installs both into one place, and the command line of the same installation is the one this app
    was released with, whatever other sandbox-ai comes first on $PATH. Otherwise
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
    beside = Path(PACKAGE_DIR if package is None else package).parent / "sandbox_ai"
    if (beside / "agent_sandbox.py").is_file() and (beside / "main.py").is_file():
        return beside
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
        self._canonical = {}  # canonical_options(): {tuple of tokens: their canonical form}
        self._canonical_parser, self._canonical_lock = None, threading.Lock()  # (launcher, its parser), built once
        self._masters, self._masters_lock = {}, threading.Lock()  # control socket -> when it last carried a command

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
                if name == "liveness" and isinstance(getattr(module, "CACHE_SECONDS", None), (int, float)):
                    # The app asks every sandboxes_interval (3 s): a Podman listing (`podman pod ps`, a Go program
                    # that may start Podman's service) is kept a little longer than one look; an action's own
                    # look asks afresh (forget_pods).
                    module.CACHE_SECONDS = max(module.CACHE_SECONDS, PODS_CACHE_SECONDS)
                elif name != "liveness" and hasattr(module, "_liveness"):
                    # One Podman listing for net (runs whose launcher is gone) and sessions (stopped Podman
                    # sessions), as sandbox-ai's own starts.py shares it: half the `podman pod ps` per look.
                    module._liveness = self.module("liveness")
                self._modules[name] = module
            return self._modules[name]

    def procinfo(self):
        """sandbox-ai's procinfo.py on macOS (no /proc there: sysctl), None elsewhere or with a sandbox-ai too old to
        have it: /proc is read then, as always."""
        if sys.platform != "darwin":
            return None
        try:
            return self.module("procinfo")
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: what /proc would say (nothing on a Mac)
            applog.swallowed("backend", "SandboxAI.procinfo: an older sandbox-ai: what /proc would say (nothing…", _exc)
            return None

    def forget_pods(self):
        """The next look asks Podman afresh (after an action: what it changed is shown at once)."""
        cache = getattr(self._modules.get("liveness"), "_cache", None)
        if isinstance(cache, dict):
            cache.clear()

    @property
    def launcher(self):
        return self.module("agent_sandbox")

    def shared_wildcards(self, values):
        """{"*.DOMAIN": (the risk in words, the shared suffix)} of those of VALUES (host names and wildcards) below a
        shared service, where anyone can rent a name (sandbox-ai's runtime.shared_wildcard: the Public Suffix List,
        read once - in the background, BLOCKING_SAI). What sandbox-ai refuses always, or an older one without the
        check, is left out: the command says why."""
        runtime = self.launcher.RUNTIME
        found = {}
        for value in values:
            wildcard = str(value).strip().lower()
            if not wildcard.startswith("*."):
                continue
            try:
                shared = runtime.shared_wildcard(wildcard)
                risk = runtime.shared_wildcard_risk(wildcard) if shared else None
            except (ValueError, AttributeError):
                continue
            if shared and risk:
                found[wildcard] = (risk, shared)
        return found

    CANONICAL_KEPT = 4000  # option lists canonical_options() remembers

    def canonical_options(self, tokens):
        """The launcher's canonical_options(TOKENS) (short forms as long ones), remembered per TOKENS: the launcher
        builds its whole option parser at every call, and a catalogue of presets asks once per preset - on the GUI
        thread too (the gallery, the settings, the wizard), each time it is shown."""
        key = tuple(tokens)
        found = self._canonical.get(key)
        if found is None:
            launcher = self.launcher
            # The launcher's parser built once: canonical_options() only reads its options, and asks for it by
            # build_parser() - here, for that call, the one built before (the app's own copy of the module; one
            # call at a time).
            with self._canonical_lock:
                if self._canonical_parser is None or self._canonical_parser[0] is not launcher:
                    self._canonical_parser = (launcher, launcher.build_parser())
                parser, original = self._canonical_parser[1], launcher.build_parser
                launcher.build_parser = lambda: parser
                try:
                    found = tuple(launcher.canonical_options(list(key)))
                finally:
                    launcher.build_parser = original
            if len(self._canonical) >= self.CANONICAL_KEPT:
                self._canonical.clear()
            self._canonical[key] = found
        return list(found)

    def command(self, *args):
        """argv of a sandbox-ai command, run with this Python like the installed entry points."""
        return [sys.executable, "-I", str(self.directory / "main.py"), *args]

    _watch = threading.local()  # what watching() set for the calls of the current thread

    @contextlib.contextmanager
    def watching(self, output=None, cancelled=None, errors_only=False):
        """While in it, every call() of this thread (also inside host_run, host_kit_command, host_install, ...) hands
        what its command writes to OUTPUT(text) as it comes (stdout and stderr, decoded; ERRORS_ONLY: stderr, for a
        command whose stdout is --json), and ends the command with everything it started once CANCELLED() is true: it
        then returns (CANCELLED, out, err + "cancelled"). How a page's Task shows a long command's progress and offers
        Cancel, without a terminal."""
        previous = getattr(self._watch, "value", None)
        self._watch.value = (output, cancelled, errors_only)
        try:
            yield
        finally:
            self._watch.value = previous

    def call(self, *args, timeout=120, input=None, argv=None):
        """(exit status, stdout, stderr) of a sandbox-ai command that does not need a terminal; INPUT, if
        given, is its standard input (how secrets are passed: never as an argument). The command leads a
        process group of its own, so stop_commands() (when the app quits) and a timeout end it with all it
        started. Its lead is reaped only under the signal lock: while a group is signalled, its number cannot
        have been given to another group. Inside watching(): its output is handed on as it comes, and it is
        ended when cancelled. ARGV: the command line that does what `sandbox-ai ARGS` would (host_run's ssh), run
        instead of it; it is never given with a runner of the tests. Each command is logged (logged_call)."""
        began = time.monotonic()
        result = self._call(*args, timeout=timeout, input=input, argv=argv)
        try:
            self.logged_call(argv, args, result, time.monotonic() - began, timeout)
        except Exception:  # noqa: BLE001 - a log line never changes a command's outcome
            pass
        return result

    def logged_call(self, argv, args, result, seconds, timeout):
        """One command of call() in the log: its command line (secrets masked: applog.argv_text; a secret goes as
        standard input, never in it), how long it took, its exit status and, when it failed, the tail of its error
        output. A success at DEBUG; a failure at INFO once per change of what it said (a poll that fails every few
        seconds the same way: then at DEBUG); a timeout at WARNING."""
        status, _out, err = result
        key = ("command", tuple(str(a) for a in args[:3]))
        if status == 0 and not applog.in_work() and not applog.debug_wanted() and applog.last_change(key) in (None, 0):
            return  # (a read that went well, every few seconds: nothing to write, nothing worked out for it)
        shown = applog.argv_text(["sandbox-ai", *args]) + (" (its ssh command line run directly)" if argv else "")
        took = applog.seconds(seconds)
        if status == 0:
            if applog.last_change(key) not in (None, 0):  # it failed before: that it works again is said once
                applog.changed(key, "INFO", "command", f"exit status 0 again after {took}: {shown}", same=0)
            else:  # (a human's action at INFO: its command and outcome are what the log is read for)
                applog.log("INFO" if applog.in_work() else "DEBUG", "command", f"exit status 0 after {took}: {shown}")
            return
        why = applog.tail(err)
        said = f"{'cancelled' if status == CANCELLED else f'exit status {status}'} after {took}: {shown}" + \
            (f"\n{why}" if why else "")
        if status == 124:
            applog.log("WARNING", "command", f"no answer within {timeout:g} s: {shown}" + (f"\n{why}" if why else ""))
            return
        if applog.in_work():
            applog.log("WARNING", "command", said)
            return
        applog.changed(key, "INFO", "command", said, quiet="DEBUG", same=(status, why))

    def _call(self, *args, timeout=120, input=None, argv=None):
        output, cancelled, errors_only = getattr(self._watch, "value", None) or (None, None, False)
        if cancelled is not None and cancelled():
            return CANCELLED, "", "cancelled"
        if self.run is not subprocess.run:  # a runner given for tests
            streams = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
            try:
                result = self.run(self.command(*args), capture_output=True, text=True, timeout=timeout, **streams)
            except subprocess.TimeoutExpired:
                return 124, "", f"sandbox-ai {' '.join(args[:2])}: no answer within {timeout} s"
            said = (result.stderr or "") if errors_only else (result.stdout or "") + (result.stderr or "")
            if output is not None and said:
                output(said)
            return result.returncode, result.stdout, result.stderr
        try:
            try:
                process = subprocess.Popen(argv or self.command(*args), stdin=subprocess.PIPE if input is not None
                                           else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
            except OSError:
                if not argv:
                    raise
                # (ssh not found, say: sandbox-ai's own command says so in its words)
                process = subprocess.Popen(self.command(*args), stdin=subprocess.PIPE if input is not None else
                                           subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
        except OSError as exc:
            return 127, "", f"sandbox-ai {' '.join(args[:2])}: {exc}"
        with self._signal_lock:
            self._processes.add(process)
        # Its output is read in this thread (no reader threads: closing a pipe under a blocked read() never
        # returns, and a descendant that left the group - a daemon, ssh, dbus - may keep the pipes open).
        pipes = Pipes(process, None if input is None else str(input).encode(), output, errors_only)
        deadline = time.monotonic() + timeout
        timed_out = stopped = False
        try:
            if not pipes.pump(deadline, cancelled):  # still open past the deadline (or cancelled): end the group,
                stopped = pipes.cancelled            # take what is left
                timed_out = not stopped
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
            if not timed_out and not stopped:  # it closed its output but goes on
                if cancelled is not None and cancelled():
                    stopped = True
                    self.end_groups([process])
                elif time.monotonic() > deadline:
                    timed_out = True
                    self.end_groups([process])
            time.sleep(0.02)
        text = pipes.text()
        if stopped:
            return CANCELLED, text[0], text[1] + "cancelled"
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
            info = self.procinfo()
            while time.monotonic() < deadline and any(group_alive(process.pid, info=info) for process in live):
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
            except Exception as _exc:  # noqa: BLE001
                applog.swallowed("backend", "SandboxAI.check_version failed (went on without it)", _exc)
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
        """How long a request waits for its answer: your default (sandbox-ai's defaults: ask_timeout), else
        runtime.APPROVAL_SECONDS, read from the file (importing the runtime takes most of a second). A question says its
        own (its "timeout"): this is only for one that does not."""
        if not hasattr(self, "_approval_seconds"):
            try:
                found = re.search(r"^APPROVAL_SECONDS = ([0-9]+)", (self.directory / "runtime.py").read_text(), re.M)
            except OSError:
                found = None
            self._approval_seconds = int(found[1]) if found else 60
        try:
            mine = self.sandbox_defaults()[0].get("ask_timeout")
        except Exception as _exc:  # noqa: BLE001 - the built-in default then
            applog.swallowed("backend", "SandboxAI.approval_seconds: defaults not readable", _exc)
            mine = None
        return mine if isinstance(mine, int) and not isinstance(mine, bool) and mine > 0 else self._approval_seconds

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

    def taken_options(self):
        """[Option] of every option the launcher takes: options() and those it leaves out of its --help on purpose
        (--workspace-name NAME=DIR, which `sandbox-ai workspace add --name` keeps; what only its own starts pass) -
        what a preset may name without being "unknown" (presets.from_document). Only --help and --version left out."""
        result = []
        for action in self.parser_actions():
            long = next((o for o in action.option_strings if o.startswith("--")), None)
            if long is None or long in ("--help", "--version"):
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

    def session_terminal(self, ident):
        """{"terminal": "ready"|"starting"|"missing"|"ended", "terminal_log": the end of its supervisor.log (missing and
        ended only: no terminal, or its tmux session has ended)} of
        the running session IDENT (sessions.terminal_state: files only); {} for a sandbox-ai without it."""
        sessions = self.module("sessions")
        if not hasattr(sessions, "terminal_state"):
            return {}
        try:
            state, tail = sessions.terminal_state(ident, sessions.load(ident))
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        key = "terminal_log" if state in ("missing", "ended") else "terminal_step"
        return {"terminal": state, **({key: printable(tail)[-2000:]} if tail else {})}

    def session_transfer_supported(self):
        """Whether this sandbox-ai has `session copy|move NAME HOST` (a session to another host)."""
        sessions = self.module("sessions")
        return hasattr(sessions, "copy_to_host") and hasattr(sessions, "plan_json")

    def session_replace_supported(self):
        """Whether this sandbox-ai's session copy|move has --replace (a session of that name there deleted first)."""
        return hasattr(self.module("sessions"), "remote_session")

    def session_transfer_plan(self, name):
        """(host paths, problems) of session NAME's copy to another host (`sandbox-ai session plan NAME --json`, a
        process of its own: reading the plan changes directory, which in this process would move every thread's):
        the absolute folders and files of this machine its options name, with what its trusted project file adds -
        each one a candidate for --map - and what cannot go along (sandbox-ai session copy refuses then). ValueError:
        no such session, or an answer that is not a plan."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", str(name)):
            raise ValueError(f"not a session: {printable(name)}")
        status, out, err = self.call("session", "plan", str(name), "--json", timeout=60)
        if status != 0:
            lines = (err or out or "").strip().splitlines()
            raise ValueError(printable(lines[-1] if lines else f"sandbox-ai session plan: exit {status}")[:300])
        try:
            plan = json.loads(out)
            paths, problems = plan["paths"], plan["problems"]
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise ValueError(f"sandbox-ai session plan: not a plan ({exc})") from None
        if not (isinstance(paths, list) and isinstance(problems, list)):
            raise ValueError("sandbox-ai session plan: not a plan")
        return [str(p) for p in paths if isinstance(p, str)], [str(p) for p in problems]

    def session_copy(self, name, host, new_name=None, maps=(), check=False, replace=False, **state):
        """`sandbox-ai session copy NAME HOST [--as NEW] [--map=LOCAL=REMOTE]... [--check] [--replace]` (STATE:
        state_words - its state along, or --config-only): the state sent and checked on HOST over SSH (or only a dry
        run there), then the start there (minutes, at most; an archive of its HOME may take longer). (status, out,
        err)."""
        return self.call(*session_transfer_args("copy", name, host, new_name, maps, check, replace, **state),
                         timeout=TRANSFER_SECONDS + (STATE_SECONDS if state.get("state") else 0))

    def session_move(self, name, host, new_name=None, maps=(), check=False, replace=False, **state):
        """`sandbox-ai session move ...`: as session_copy, then the session here is stopped (its HOME stays)."""
        return self.call(*session_transfer_args("move", name, host, new_name, maps, check, replace, **state),
                         timeout=TRANSFER_SECONDS + (STATE_SECONDS if state.get("state") else 0))

    def session_state_supported(self):
        """Whether this sandbox-ai carries a session's state: session copy|move with it, duplicate, import (0.1.193)."""
        return (self.directory / "session_state.py").is_file()

    def session_state_json(self, *args, timeout=None):
        """The JSON object `sandbox-ai ARGS` prints; ValueError with the end of what it said when it failed (several
        lines: an import lists what it asks)."""
        status, out, err = self.call(*args, timeout=timeout or TRANSFER_SECONDS + STATE_SECONDS)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            lines = [line for line in (err or out or "").strip().splitlines() if line.strip()]
            raise ValueError(printable_text("\n".join(lines[-12:]) if lines else f"exit status {status}")[:3000])
        return data

    def session_duplicate(self, name, new_name, **state):
        """`sandbox-ai session duplicate NAME NEW --json` (STATE: state_words' with_workspace, include_secrets, grant):
        a second session of this machine from the state of NAME, not started. Its import's result; ValueError."""
        for value in (name, new_name):
            if not isinstance(value, str) or not SESSION_NAME.fullmatch(value):
                raise ValueError("a session's name is 1-40 letters, digits, '_', '.' or '-', beginning with a letter or "
                                 "digit")
        return self.session_state_json("session", "duplicate", name, new_name, "--json",
                                       *state_words(True, **state))

    def session_import(self, path, new_name=None, maps=(), check=False, replace=False, grant=False,
                       no_workspace=False, yes=False):
        """`sandbox-ai session import PATH --json ...` (session_import_args): with CHECK what it would start with -
        {"options", "asked", "approvals", "warnings", ...} - nothing kept; else the session made. ValueError."""
        return self.session_state_json(*session_import_args(path, new_name, maps, check, replace, grant, no_workspace,
                                                            yes))

    def board_bundle_supported(self):
        """Whether this sandbox-ai has `sandbox-ai board export|import` (a board or session as one file); looked up
        once (the Sandboxes page asks at every change of the selection)."""
        known = self.__dict__.get("_bundle_supported")
        if known is None:
            known = self.__dict__["_bundle_supported"] = (self.directory / "board_bundle.py").is_file()
        return known

    def board_export(self, out, session=None, board=None, host="local", images=False):
        """`sandbox-ai board export (--session NAME | --board -) OUT [--images] [--host HOST] --json`: the bundle of
        session SESSION (of HOST) or of the board document BOARD (a dict, given on standard input), written to OUT
        here. (status, out, err)."""
        args = board_export_args(out, session, board is not None, host, images)
        return self.call(*args, timeout=BUNDLE_SECONDS, input=None if board is None else json.dumps(board))

    def board_import(self, path, host="local", check=True, **choices):
        """`sandbox-ai board import PATH --json (--check | --yes) ...` (board_import_args): its plan (CHECK: nothing
        written) or what it wrote, as a dict (the plan's JSON, "status" its exit status); ValueError when it gave no
        plan (its words)."""
        status, out, err = self.call(*board_import_args(path, host, check=check, **choices), timeout=BUNDLE_SECONDS)
        try:
            plan = json.loads(out)
        except (ValueError, RecursionError):
            plan = None
        if not isinstance(plan, dict) or not isinstance(plan.get("problems"), list):
            lines = (err or out or "").strip().splitlines()
            raise ValueError(printable(lines[-1] if lines else f"sandbox-ai board import: exit {status}")[:600])
        return {**plan, "status": status, "said": printable_text(err or "")[-4000:]}

    def session_ids(self):
        """{name: id} of the named sessions: the id is the session's directory, which stays when it is renamed. The
        MCP servers each one's options name are kept on the way (session_servers: one read of each session.json)."""
        sessions = self.module("sessions")
        servers = self.__dict__["_session_servers"] = {}
        starts = self.__dict__["_session_starts"] = {}
        if not hasattr(sessions, "display") or not hasattr(sessions, "root"):
            return {}
        found, root = {}, sessions.root()
        if not root.is_dir():
            return found
        for directory in root.iterdir():
            try:
                with open(directory / "session.json", "rb") as stream:
                    data = json.loads(stream.read(1 << 20))
            except (OSError, ValueError, RecursionError):
                continue
            if valid_run(directory.name):
                found[sessions.display(directory.name, data)] = directory.name
                if isinstance(data, dict):
                    servers[directory.name] = mcp_option_names(data.get("options"))
                    options, arguments = data.get("options"), data.get("agent_args", [])
                    if data.get("agent") in self.agents and isinstance(options, list) and isinstance(arguments, list) \
                            and all(isinstance(v, str) for v in options + arguments):
                        starts[directory.name] = (data["agent"], list(options), list(arguments))
        return found

    def session_start(self, ident):
        """(agent, launcher options, agent arguments) session IDENT was created with, as session_ids last read them
        (the Start page shows them for its project), or None."""
        return self.__dict__.get("_session_starts", {}).get(ident)

    def session_exit(self, ident):
        """The exit status of the last run of session IDENT (its exit.json, sessions.exit_status), or None."""
        sessions = self.module("sessions")
        try:
            status = sessions.exit_status(ident, sessions.load(ident))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return status if isinstance(status, int) and not isinstance(status, bool) else None

    def session_servers(self, ident):
        """The MCP servers the options of session IDENT name (its later starts reach them), as session_ids last read
        them: what a stopped or paused session offers to Remove MCP server… (review 6 A-N2)."""
        return list(self.__dict__.get("_session_servers", {}).get(ident, []))

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
        except (ValueError, RecursionError):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def mcp_definitions(self):
        """{name: definition} of the MCP servers this user defined (mcp.json) and the built-in ones."""
        return self.module("mcp").definitions()

    def mcp_tool_budget(self, model, servers, project=None):
        """The MCP tools MODEL would see with the MCP servers SERVERS (names) in PROJECT (the first workspace) against
        the budget of its class (sandbox-ai's mcp_budget.start_report: each server's pin, else its catalog's list),
        or None: an older sandbox-ai without it, or profiles or definitions that cannot be read. Reads files."""
        try:
            budget = self.module("mcp_budget")
            table = self.module("model_profiles").load()
            definitions = self.mcp_definitions()
        except (OSError, ValueError, AttributeError) as exc:
            applog.swallowed("backend", "mcp_tool_budget: an older or damaged sandbox-ai counts no MCP tools", exc)
            return None
        return budget.start_report(model, list(servers or []), project, definitions, table)

    def mcp_catalog(self):
        """([(prefix, entry)], problems) of the MCP catalogs."""
        return self.module("mcp").catalog_entries()

    def user_images(self, host="local"):
        """[{name, kind, image, ref, digest, user, ports, os, unchecked, used_by, base_problem ...}] of your images
        (sandbox-ai images pull|build-image): here read from their record (no Podman call), on another host its
        `sandbox-ai images list --json`. [] for a sandbox-ai without them; ValueError when a host does not answer."""
        if host == "local":
            try:
                module = self.module("services")
            except Exception as _exc:  # noqa: BLE001 - an older or unreadable installation has none
                applog.swallowed("backend", "SandboxAI.user_images: an older or unreadable installation has none", _exc)
                return []
            if not hasattr(module, "listing"):
                return []
            damaged = {}
            try:
                rows = module.listing(check=False, damaged=damaged)
            except TypeError:  # (a sandbox-ai before 0.1.99: a damaged record made the whole list fail)
                rows = module.listing(check=False)
            for line in module.damaged_note(damaged) if damaged and hasattr(module, "damaged_note") else ():
                applog.log("WARNING", "backend", line)
            return [row for row in rows if isinstance(row, dict)]
        return self.host_listing(host, "images", "list")

    def service_templates(self, host="local"):
        """[{template, description, image, built}] of sandbox-ai's own Containerfiles for services on HOST (`sandbox-ai
        images templates`: images build-image NAME --builtin TEMPLATE; showcase-web): here read from this
        installation, on another host from its own sandbox-ai (its templates may differ). [] for a sandbox-ai without
        them (an older host); ValueError when a host does not answer."""
        if host == "local":
            try:
                module = self.module("services")
            except Exception as _exc:  # noqa: BLE001 - an older or unreadable installation has none
                applog.swallowed("backend", "SandboxAI.service_templates: an older installation has none", _exc)
                return []
            if not hasattr(module, "templates"):
                return []
            return [row for row in module.templates() if isinstance(row, dict)]
        try:
            rows = self.host_listing(host, "images", "templates")
        except ValueError as exc:
            if "invalid choice" in str(exc):
                return []  # (an older sandbox-ai there: none of its own)
            raise
        return [row for row in rows if isinstance(row.get("template"), str) and isinstance(row.get("image"), str)]

    def variant_bases(self, host="local"):
        """{variant: your image it is built on} of the image variants (sandbox-ai images new NAME --base IMAGE); here
        read from their files, on another host from `sandbox-ai images --json`."""
        if host != "local":
            return {str(row["target"]): str(row["base_image"]) for row in self.host_listing(host, "images")
                    if row.get("base_image") and row.get("target")}
        try:
            images = self.module("images")
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.variant_bases failed (went on without it)", _exc)
            return {}
        found = {}
        for name, _agent in images.variants() if hasattr(images, "variants") else []:
            try:
                base = images.load(name, strict=False)[0].get("base")
            except (ValueError, OSError):
                continue
            if isinstance(base, str) and base:
                found[name] = base
        return found

    def doctor(self, backend="podman"):
        return self.module("doctor").checks(backend)

    def listing(self, *command):
        """[dict] of `sandbox-ai COMMAND --json` (a list of objects), [] when this sandbox-ai cannot say."""
        status, out, _ = self.call(*command, "--json", timeout=60)
        if status != 0:
            return []
        try:
            data = json_part(out)
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
        except (ValueError, RecursionError):
            return {}
        return {name: entry for name, entry in data.items() if isinstance(entry, dict)} if isinstance(data, dict) else {}

    # The server of each kind of publish login when no own one is named (sandbox-ai publish_logins.KINDS).
    PUBLISH_HOSTS = {"pypi": "upload.pypi.org", "testpypi": "test.pypi.org", "npm": "registry.npmjs.org",
                     "crates": "crates.io", "gitlab": "gitlab.com", "github": "api.github.com"}
    # The secret a token typed for a publish login is kept as, unless another name is given.
    PUBLISH_SECRETS = {"pypi": "PYPI_TOKEN", "testpypi": "TESTPYPI_TOKEN", "npm": "NPM_TOKEN", "crates": "CARGO_TOKEN",
                       "gitlab": "GITLAB_TOKEN", "github": "GITHUB_TOKEN"}

    @classmethod
    def token_secret_name(cls, kind=None, url=None):
        """The secret name offered for a token typed for a login: PUBLISH_SECRETS of a publish KIND, GIT_TOKEN_<HOST>
        for a Git login to URL (its host in capitals, other characters as _)."""
        if kind:
            return cls.PUBLISH_SECRETS.get(kind) or (re.sub(r"[^A-Z0-9]+", "_", str(kind).upper()).strip("_")[:50]
                                                     or "PUBLISH") + "_TOKEN"
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
        tail = re.sub(r"[^A-Z0-9]+", "_", host.upper()).strip("_")[:50]
        return f"GIT_TOKEN_{tail}" if tail else "GIT_TOKEN"

    @staticmethod
    def login_steps(command, add_args, secret, value=None):
        """[(arguments, standard input)] of a Git or publish login whose token is VALUE (typed; None: SECRET is kept
        already): `secrets set SECRET` with VALUE on standard input first, then `COMMAND ADD_ARGS` (which names
        --secret SECRET) - so the login never names a secret that is not there. The value is never an argument."""
        steps = [(["secrets", "set", secret], value + "\n")] if value else []
        return steps + [([command, *add_args], None)]

    def login_url_problem(self, command, values):
        """Why sandbox-ai would refuse the server of a Git or publish login (VALUES: {url, kind}), checked as its
        `git-logins add` / `publish-logins add` check it, before anything is kept; None when it would take it. The
        dialog says it and stays open: no token is stored for a login that is then refused."""
        url = (values.get("url") or "").strip()
        try:
            if command == "git-logins":
                if not url:
                    return "give the server's address: https://HOST[:PORT]"
                self.launcher.git_server(url, "the Git server")
            elif command == "publish-logins" and url:
                self.module("publish_logins").server_host(values.get("kind"), url)
        except ValueError as exc:
            return printable_text(str(exc))
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai without the check: its add says it
            applog.swallowed("backend", "SandboxAI.login_url_problem: an older sandbox-ai without the check: it…", _exc)
            return None
        return None

    def add_login(self, host, command, add_args, secret, value=None, existed=None):
        """login_steps on HOST, one after the other: (status, out, err) of the first that failed, or of the add. A
        token typed for a secret that did not exist before (EXISTED False) is removed again when the add fails: no
        orphan secret stays behind a login that was refused."""
        if command not in ("git-logins", "publish-logins") or not self.valid_secret(secret, value):
            return 2, "", "invalid login or secret"
        said, result = [], (0, "", "")
        for args, stdin in self.login_steps(command, add_args, secret, value):
            if args[0] == "secrets":
                result = self.set_secret(secret, value, host)
            else:
                result = self.host_run(host, *args, timeout=90)
            said.append(((result[1] or "") + (result[2] or "")).strip())
            if result[0] != 0:
                if args[0] != "secrets" and value and existed is False and self.delete_secret(secret, host)[0] == 0:
                    said.append(f"(the secret {secret} kept for it was removed again)")
                return result[0], "", "\n".join(part for part in said if part)
        return 0, "\n".join(part for part in said if part), ""

    @staticmethod
    def identity_steps(name, old, new):
        """The `sandbox-ai identities` argument lists that turn identity NAME from OLD (None: not there yet) into
        NEW ({name, email, description, git: [{url, user, secret}], publish: [{kind, host, secret, url?}]}):
        add or set, then the logins that went away, then the new or changed ones. Every value is one argument, never
        read as an option: the options first, then "--", then the identity's name and the URL or kind (the command line
        checks each again)."""
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
            steps.append(["identities", "add", *fields, "--", name])
        elif fields:
            steps.append(["identities", "set", *fields, "--", name])
        old_git = {e["url"]: e for e in (old or {}).get("git", [])}
        new_git = {e["url"]: e for e in new.get("git", []) if e.get("url")}
        for url in old_git.keys() - new_git.keys():
            steps.append(["identities", "logout", "--", name, url])
        for url, entry in new_git.items():
            if old_git.get(url) != {"url": url, "user": entry.get("user") or "x-access-token", "secret": entry["secret"]}:
                steps.append(["identities", "login", f"--secret={entry['secret']}",
                              f"--user={entry.get('user') or 'x-access-token'}", "--", name, url])
        old_pub = {e["kind"]: e for e in (old or {}).get("publish", [])}
        new_pub = {e["kind"]: e for e in new.get("publish", []) if e.get("kind")}
        for kind in old_pub.keys() - new_pub.keys():
            steps.append(["identities", "unpublish", "--", name, kind])
        for kind, entry in new_pub.items():
            before = old_pub.get(kind)
            host = urllib.parse.urlsplit(entry["url"]).hostname if entry.get("url") else SandboxAI.PUBLISH_HOSTS.get(kind)
            if before is None or before.get("secret") != entry["secret"] or before.get("host") != host:
                steps.append(["identities", "publish", f"--secret={entry['secret']}",
                              *([f"--url={entry['url']}"] if entry.get("url") else []), "--", name, kind])
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
        """Add a VPN profile; SECRET_VALUE (a key or password typed in the app) is kept as the secret fields["secret"]
        with `sandbox-ai secrets set` (standard input), never passed on a command line - after `vpn add` succeeded (a
        profile refused keeps no key), but for Mullvad, whose account number `vpn add` reads, before it."""
        args = self.vpn_add_argv(name, kind, **fields)
        if secret_value and not fields.get("secret"):
            raise ValueError("a typed key or password needs the name of the secret to keep it as")
        first = bool(secret_value) and kind == "mullvad"
        if first:
            status, out, err = self.set_secret(fields["secret"], secret_value)
            if status != 0:
                return status, out, err
        status, out, err = self.call(*args, timeout=180)
        if status != 0 or not secret_value or first:
            return status, out, err
        kept, kept_out, kept_err = self.set_secret(fields["secret"], secret_value)
        if kept != 0:
            return kept, out + kept_out, err + kept_err
        return status, out, err

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
        return self.module("approvals").questions(self.live_runs() if runs is None else runs, answered=answered)

    def bridge_runs(self):
        """{"bridge-ID": record} of the session bridges whose broker asks (sandbox-ai bridge: a call across a bridge,
        every write with its diff); {} with a sandbox-ai that has none. Files only, never raises."""
        try:
            approvals = self.module("approvals")
            return approvals.bridge_runs() if hasattr(approvals, "bridge_runs") else {}
        except Exception:  # noqa: BLE001 - no bridges known
            return {}

    # -- session bridges (sandbox-ai bridge): read from its files, changed only through the command --

    def bridge_supported(self):
        """Whether this sandbox-ai has `sandbox-ai bridge` (bridge.py beside the launcher)."""
        return (self.directory / "bridge.py").is_file()

    def bridges(self, include_all=False):
        """[{id, state, a, b, a_run, b_run, a_to_b, b_to_a, observer_actions, ask, until, calls, warnings, reason}] of
        `sandbox-ai bridge list --json` (ALL: ended ones too), read from its files with sandbox-ai's own module (no
        process, no Podman: the Sandboxes page reads them with every look); [] with a sandbox-ai that has none."""
        if not self.bridge_supported():
            return []
        rows = self.module("bridge").listing(include_all)
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]

    @staticmethod
    def bridge_add_args(a, b, allow, both=False, until=None, ask="first", observer_actions=None, limits=()):
        """The words of `sandbox-ai bridge add A B --allow ...` (no --json, no --dry-run). ValueError for a name or a
        value that would be read as an option. A or B may be HOST:NAME: a session of a host of `sandbox-ai hosts`."""
        for value in (a, b):
            if not isinstance(value, str) or not re.fullmatch(
                    r"(?:[a-z0-9][a-z0-9_-]{0,31}:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
                raise ValueError(f"not a session's name: {value!r}")
        caps = [str(cap) for cap in allow]
        if not caps or any(not cap or cap.startswith("-") or "," in cap or any(c < " " for c in cap) for cap in caps):
            raise ValueError("choose at least one capability")
        args = ["bridge", "add", a, b, "--allow", ",".join(caps)]
        if both:
            args.append("--both")
        if until:
            args.append(f"--until={until}")
        if ask:
            args.append(f"--ask={ask}")
        if observer_actions is not None:
            args.append("--observer-actions=" + ",".join(observer_actions))
        for key, value in limits:
            args.append(f"--limit={key}={value}")
        return args

    def bridge_json(self, *args, timeout=120):
        """The JSON document `sandbox-ai ARGS --json` says; ValueError with its message when it refuses."""
        status, out, err = self.call(*args, "--json", timeout=timeout)
        if status != 0:
            raise ValueError(printable_text((err or out).strip()[-1500:]) or f"exit status {status}")
        return json_part(out)

    def bridge_plan(self, args):
        """What `sandbox-ai bridge add ... --dry-run` says of the bridge ARGS (bridge_add_args): its grants and every
        warning (the egress direction, a whole workspace, agents that read MCP servers only at their start). Nothing
        is made. ValueError: refused (a session that does not run, one bridged already, a path outside ...)."""
        return self.bridge_json(*args, "--dry-run", timeout=90)

    def bridge_add(self, args):
        """`sandbox-ai bridge add ...` (asked first by the page: bridge-add): the bridge made, {id, state, grants,
        warnings, notes, ...}."""
        return self.bridge_json(*args, timeout=90)

    def host_bridges(self, name, timeout=60):
        """The bridges of host NAME (`sandbox-ai bridge list --json` there, over SSH), as this machine shows them
        (bridges.from_host: "host", its own sessions HOST:NAME). ValueError: no answer."""
        from . import bridges as BR
        rows = self.host_json(name, "bridge", "list", "--json", timeout=timeout)
        if not isinstance(rows, list):
            raise ValueError(f"{name}: not a list of bridges")
        return BR.from_host(rows, name)

    def bridge_show(self, ident):
        """`sandbox-ai bridge show ID --json` (HOST:ID: one of that host's): its record, newest calls and alerts."""
        return self.bridge_json("bridge", "show", valid_bridge_id(ident), timeout=120)

    def bridge_revoke(self, ident):
        """`sandbox-ai bridge revoke ID` (asked first by the page: bridge-revoke)."""
        return self.checked("bridge", "revoke", valid_bridge_id(ident), timeout=120)

    def bridge_resume(self, ident):
        """`sandbox-ai bridge resume ID` (asked first by the page: bridge-resume)."""
        return self.checked("bridge", "resume", valid_bridge_id(ident), timeout=120)

    def live_runs(self):
        """{run: record} of the sandboxes whose launcher runs, as runs() lists them, without what runs() does about a
        run whose launcher is gone (asking Podman whether its pod still runs, removing the directories of one that
        ended; runs() leaves such a run out too): only the registry's files and /proc, so the approvals' scan (on the
        GUI thread too, every second) never waits for Podman."""
        net = self.module("net")
        if not hasattr(net, "run_dirs") or not hasattr(net, "alive"):  # an older sandbox-ai: its running() is this
            return net.running()
        runs = {}
        for directory in net.run_dirs():
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    record = json.loads((entry / "run.json").read_text())
                except (OSError, ValueError, RecursionError):  # (damaged: nested beyond what Python reads)
                    continue
                if not isinstance(record, dict) or not net.alive(record.get("pid"), record.get("pid_start")):
                    continue
                record["directory"] = str(entry)
                runs.setdefault(record.get("run", entry.name), record)
        return runs

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
            return self._questions.scan(self.live_runs() if runs is None else runs)
        finally:
            self._questions_lock.release()

    def watcher_heartbeat(self, on=True):
        """The app watches the questions and notifies of them itself (ON): its heartbeat (approvals.heartbeat("app"))
        tells the launchers to show no notification of their own, so each question has one prompt, the app's. OFF:
        the heartbeat goes (the app quits, or its notifications were switched off). False with a sandbox-ai that has
        no heartbeats (its launcher notifies as before). Never raises."""
        try:
            approvals = self.module("approvals")
            if not hasattr(approvals, "heartbeat"):
                return False
            approvals.heartbeat("app") if on else approvals.end_heartbeat()
            return True
        except Exception as _exc:  # noqa: BLE001 - a heartbeat that cannot be written: the launcher notifies as before
            applog.swallowed("backend", "SandboxAI.watcher_heartbeat: a heartbeat that cannot be written: the l…", _exc)
            return False

    def answer(self, ident, decision, scope="once"):
        """Answer question IDENT through `sandbox-ai approvals allow|deny --scope SCOPE` - an open one, one nobody
        answered in time, or one answered already (the run's decision changed; SCOPE None: sandbox-ai's default, for
        the run); DECISION "ask-again": `sandbox-ai approvals ask-again` (what the run decided is forgotten). Deny
        "always" blocks it for the project (refused at every later start without a question). Scopes wider than the
        administrator's max_approval_scope are refused here already (sandbox-ai refuses them too)."""
        if not self.question_id.fullmatch(str(ident)) or decision not in ("allow", "deny", "ask-again") or \
                scope not in (*SCOPES, None):
            return 2, "", "invalid question or answer"
        if decision == "ask-again":
            return self.call("approvals", "ask-again", ident, timeout=60)
        # The policy caps allows only (as sandbox-ai does): a deny, also Block always, is never refused for its scope.
        if decision == "allow" and scope is not None and SCOPES.index(scope) > SCOPES.index(self.max_scope()):
            return 2, "", f"the administrator's policy allows answers up to {self.max_scope()!r}"
        return self.call("approvals", decision, ident, *(["--scope", scope] if scope else []), timeout=90)

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
        found = self.module("devices").checked_device(value)
        if len(found) == 3:  # (entries, notes, USB device facts) since 0.1.76: the nodes it gives, then the notes
            entries, notes, _device = found
            paths = [str(entry.get("path")) for entry in entries if isinstance(entry, dict) and entry.get("path")]
            shown = value if not paths or paths == [value] else f"{value} ({', '.join(paths)})"
            return shown, list(notes)
        return found

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

    # -- build toolchains (sandbox-ai toolchain): the store, the signed catalog, a running sandbox's --

    def host_toolchains(self, host="local"):
        """{"installed": [row], "catalog": [row], "signed": bool, "problem": text or None} of HOST: its store
        (`toolchain list --json`) and what the signed catalog offers its machine (`toolchain catalog --json`; not
        read: "problem" says why, the store is listed all the same). ValueError: the store was not read (a
        sandbox-ai without toolchains there)."""
        installed = self.host_json(host, "toolchain", "list", "--json", timeout=60)
        if not isinstance(installed, list):
            raise ValueError("not a list of toolchains")
        rows = [row for row in installed if isinstance(row, dict) and valid_toolchain(row.get("name"))
                and isinstance(row.get("version"), str)]
        catalog, signed, problem = [], False, None
        try:
            found = self.host_json(host, "toolchain", "catalog", "--json", timeout=90)
            catalog = [row for row in found.get("toolchains", []) if isinstance(row, dict)
                       and valid_toolchain(row.get("name")) and isinstance(row.get("version"), str)]
            signed = (found.get("signature") or {}).get("state") == "good"
            if not signed:
                problem = printable_text(str((found.get("signature") or {}).get("reason") or "not signed"))[:300]
        except (ValueError, OSError, AttributeError) as exc:
            problem = printable_text(str(exc))[-300:]
        return {"installed": rows, "catalog": catalog if signed else [], "signed": signed, "problem": problem}

    def toolchains_of_run(self, run, host="local"):
        """[row] of the toolchains of the running sandbox RUN on HOST (`toolchain list --run RUN --json`): "name",
        "version", "source" (start, added), "commands". ValueError: not answered."""
        if not valid_run(run):
            raise ValueError("invalid run")
        data = self.host_json(host, "toolchain", "list", "--run", run, "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a list of toolchains")
        return [row for row in data if isinstance(row, dict) and valid_toolchain(row.get("name"))]

    def toolchain_add(self, name, host="local"):
        """`sandbox-ai toolchain add NAME` on HOST: from the signed catalog into its store (a download)."""
        if not valid_toolchain(name):
            return 2, "", "invalid toolchain"
        return self.host_run(host, "toolchain", "add", name, timeout=3600)

    def toolchain_attach(self, run, name, host="local", offline=False):
        """`sandbox-ai toolchain attach RUN NAME` on HOST: mounted read-only into the running sandbox."""
        if not valid_run(run) or not valid_toolchain(name):
            return 2, "", "invalid run or toolchain"
        return self.host_run(host, "toolchain", "attach", run, name, *(["--offline"] if offline else []), timeout=300)

    def toolchain_detach(self, run, name, host="local", force=False):
        """`sandbox-ai toolchain detach RUN NAME [--force]` on HOST."""
        if not valid_run(run) or not valid_toolchain(name):
            return 2, "", "invalid run or toolchain"
        return self.host_run(host, "toolchain", "detach", run, name, *(["--force"] if force else []), timeout=150)

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

    def device_scan(self, host="local"):
        """The devices of HOST a sandbox could be given (`sandbox-ai devices scan --json` there, over SSH for another
        host - never this machine's for a host): [{"kind", "value", "option", "label", "nodes", "usb", "links",
        "refused", "note"}], every text escaped, every value one --device or --gpu takes. ValueError: not answered (an
        older sandbox-ai there: DeviceScanMissing)."""
        status, out, err = self.host_run(host, "devices", "scan", "--json", timeout=60)
        try:
            data = json.loads(out)
        except (ValueError, RecursionError):
            text = (err or out).strip()
            if status != 0 and re.search(r"invalid choice: '?scan'?", text):
                raise DeviceScanMissing(text[-300:]) from None
            raise ValueError(text[-500:] or f"exit status {status}") from None
        entries = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError("not a list of devices")
        found = []
        for entry in entries[:DEVICE_SCAN_LIMIT]:
            if not isinstance(entry, dict) or entry.get("kind") not in DEVICE_SCAN_KINDS or \
                    not isinstance(entry.get("value"), str):
                continue
            option = "--gpu" if entry.get("option") == "--gpu" else "--device"
            value = entry["value"]
            if (option == "--gpu" and value not in GPU_KINDS) or (option == "--device" and not valid_device(value)):
                continue
            text = {key: printable(entry[key])[:500] if isinstance(entry.get(key), str) else None
                    for key in ("label", "usb", "product", "refused", "note")}
            found.append({"kind": entry["kind"], "value": value, "option": option,
                          "label": text["label"] or value, "usb": text["usb"], "product": text["product"],
                          "refused": text["refused"], "note": text["note"],
                          "nodes": [printable(n)[:300] for n in entry.get("nodes") or [] if isinstance(n, str)][:16],
                          "links": [printable(n)[:300] for n in entry.get("links") or [] if isinstance(n, str)][:8]})
        return found

    def devices_listed(self, run, host="local"):
        """The devices of the running sandbox RUN on HOST, given at the start or added (`sandbox-ai devices list RUN
        --json` there): [{"id", "value", "source", "state", "nodes", "device"}], the text escaped. ValueError: not
        answered."""
        if not valid_run(run):
            raise ValueError("invalid run")
        data = self.host_json(host, "devices", "list", run, "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a list of devices")
        found = []
        for entry in data[:DEVICE_SCAN_LIMIT]:
            if not isinstance(entry, dict):
                continue
            device = entry.get("device") if isinstance(entry.get("device"), dict) else {}
            found.append({"id": str(entry.get("id")) if re.fullmatch(r"[0-9]{1,9}", str(entry.get("id"))) else None,
                          "value": printable(entry.get("value"))[:300] if isinstance(entry.get("value"), str) else "",
                          "source": "added" if entry.get("source") == "added" else "start",
                          "state": printable(entry.get("state"))[:40] if isinstance(entry.get("state"), str) else "",
                          "nodes": [printable(n)[:300] for n in entry.get("nodes") or [] if isinstance(n, str)][:16],
                          "usb": printable(device.get("usb"))[:20] if isinstance(device.get("usb"), str) else None,
                          "product": printable(device.get("product"))[:100] if isinstance(device.get("product"), str)
                          else None})
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
        return [{**{key: printable(entry[key])[:1000] for key in ("host", "sandbox", "mode", "source", "note",
                                                                  "remote")
                    if isinstance(entry.get(key), str)},
                 **({"kind": entry["kind"]} if entry.get("kind") in ("dir", "file") else {}),
                 **({"named": entry["named"]} if isinstance(entry.get("named"), bool) else {})}
                for entry in data if isinstance(entry, dict)]

    # -- workspaces of a running sandbox (sandbox-ai workspace) --

    def workspaces(self, run):
        """The workspaces of the running sandbox RUN: [{host, sandbox, mode, source ("start" or "added"), kept,
        since}] (workspaces.listing). Older sandbox-ai versions lack it: AttributeError (or ImportError)."""
        record = self.runs().get(run)
        return list(self.module("workspaces").listing(record)) if record else []

    def add_workspace(self, run, directory, read_only=False, name=None, host="local"):
        """`sandbox-ai workspace add RUN DIR|FILE [--ro] [--name NAME]`: a folder (or one regular file) at
        /workspaces/NAME until the sandbox ends (a folder is kept for a restart of a session; NAME with it). The
        launcher does every check of a start (scope, private places, the policy). DIRECTORY may be a folder of
        another machine (sshfs://...: remote_source.valid_source): that host mounts it with sshfs first."""
        if not valid_run(run) or not (valid_directory(directory) or valid_remote(directory)):
            return 2, "", "invalid run or directory"
        if name and not valid_workspace_name(name):
            return 2, "", "invalid workspace name"
        try:
            self.require_options(host, ("workspace", "add"), ["--name"] if name else [])
        except HostTooOld as exc:
            return 2, "", str(exc)
        return self.host_run(host, "workspace", "add", run, str(directory), *(["--ro"] if read_only else []),
                             *([f"--name={name}"] if name else []), timeout=120)

    def remove_workspace(self, run, value, force=False, host="local"):
        """`sandbox-ai workspace remove RUN DIR|NAME [--force]`: refused while a process in the sandbox uses it,
        unless FORCE (detached lazily)."""
        if not valid_run(run) or not valid_directory(value) and not valid_remote(value) and \
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(value)):
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
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.run_for failed (went on without it)", _exc)
            return None
        return next((name for name, record in runs.items()
                     if os.path.realpath(str(record.get("workspace", ""))) == real), None)

    # -- every sandbox-ai instance of this account --

    def net_options(self, command="allow"):
        """The option strings `sandbox-ai net COMMAND` takes in this installation (--dns, --allow-host-port and the
        --git-token-* logins came in later versions)."""
        try:
            parser = self.module("net").parser()
        except Exception as _exc:  # noqa: BLE001 - an old or damaged installation
            applog.swallowed("backend", "SandboxAI.net_options: an old or damaged installation", _exc)
            return set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) and command in action.choices:
                return {option for sub in action.choices[command]._actions for option in sub.option_strings}
        return set()

    def net_commands(self):
        """The subcommands of `sandbox-ai net` this installation has (e.g. "stop" since the app's 0.2 era)."""
        try:
            parser = self.module("net").parser()
        except Exception as _exc:  # noqa: BLE001 - an old or damaged installation: nothing beyond what it lists
            applog.swallowed("backend", "SandboxAI.net_commands: an old or damaged installation: nothing beyond…", _exc)
            return set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
        return set()

    def foreground_start(self, name, ident=None):
        """True if the kept start NAME runs (or ran) in the foreground of its own terminal (--no-tmux, a task):
        no tmux, nothing to attach to. False when sandbox-ai cannot say (older versions: every session had tmux).
        IDENT: its id when known (session_ids): its own record is read, not every start's to find it by name."""
        try:
            sessions = self.module("sessions")
            return bool(sessions.foreground(sessions.load(ident if ident and valid_run(ident) else
                                                          sessions.resolve(name))))
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai, or a damaged start: as before
            applog.swallowed("backend", "SandboxAI.foreground_start: an older sandbox-ai, or a damaged start: a…", _exc)
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
            servers = {}
            for name in sorted(mcp.definitions()):
                # Its running instances: NAME, and NAME.HASH of the starts with limits of their own (--mcp-limits).
                running = mcp.instances(name) if hasattr(mcp, "instances") else \
                    [name] if mcp.status(name) == "running" else []
                servers.update({instance: mcp.load_state(instance) for instance in running})
        except Exception as exc:  # noqa: BLE001 - e.g. a damaged mcp.json
            servers, problems = {}, problems + [str(exc)]
        try:
            ids = self.session_ids()
        except Exception as _exc:  # noqa: BLE001 - keyed by name then (an older sandbox-ai)
            applog.swallowed("backend", "SandboxAI.instances: keyed by name then (an older sandbox-ai)", _exc)
            ids = {}
        by_session = runs_of_starts(runs, ids)
        sizes = self.storage_sizes()  # (each start's size at the last storage check: the Size column)
        used = set()
        for row in rows:
            name, agent, engine, state, workspace, created = (list(row) + [""] * 6)[:6]
            run = by_session.get(name)
            record = runs.get(run, {}) if run else {}
            used.add(run)
            ident = ids.get(name, name)  # its directory: a renamed session keeps its row and its tab
            if not run:  # not running: what it keeps for its later starts (start_tree.reached: "mcp")
                record = {"mcp": self.session_servers(ident)}
                if state == "stopped":  # how its last run ended (a failed one keeps its tab: ui.sync_tabs)
                    status = self.session_exit(ident)
                    if status is not None:
                        record["status"] = status
            if state == "running":  # its terminal there yet, still starting, or none (terminal_state)
                record = {**record, **self.session_terminal(ident)}
            if isinstance(sizes.get(ident), dict):
                record = {**record, "storage": sizes[ident]}
            found.append(Instance("session", f"session:{ident}", name, run=run, session=name, project=workspace,
                                  agent=agent, backend=engine, started=record.get("started") or created, state=state,
                                  proxy=record.get("proxy") if run else None, pid=record.get("pid"), record=record,
                                  foreground=self.foreground_start(name, ids.get(name)), ident=ident))
        by_pid = {record.get("pid"): run for run, record in runs.items()
                  if isinstance(record, dict) and record.get("kind") == "mcp" and record.get("pid")}
        for name, state in servers.items():
            # Its run: mcp-NAME with Podman; with Bubblewrap the launcher names it AGENT-xxxx, found by its PID (as
            # sandbox-ai list finds it): never a "run" of its own beside the server, with a tab of its own.
            run = f"mcp-{name}" if f"mcp-{name}" in runs else by_pid.get(state.get("pid")) if state.get("pid") else None
            record = dict(runs.get(run, {})) if run else {}
            used.add(run)
            # Whom it was started for (mcp.json, since 0.1.78): the Sandboxes page shows it below that start
            # (start_tree.owners); "owner_recorded": the key is there (None: started on its own).
            record.update({"started_for": state.get("started_for"), "owner_recorded": "started_for" in state})
            found.append(Instance("mcp", f"mcp:{name}", name, run=run, mcp=name, project=record.get("workspace", ""),
                                  agent="mcp", backend=state.get("backend") or record.get("backend", ""),
                                  started=state.get("started") or record.get("started", ""), state="running",
                                  proxy=record.get("proxy") if run else None, pid=state.get("pid"), record=record))
        for name, record in runs.items():
            if name in used:
                continue
            session = session_of(record)
            kind = "session" if session else "mcp" if name.startswith("mcp-") or record.get("kind") == "mcp" else "run"
            server = (name[4:] if name.startswith("mcp-") else name) if kind == "mcp" else None
            found.append(Instance(kind, f"run:{name}", session or server or name, run=name,
                                  session=session, mcp=server,
                                  project=record.get("workspace", ""), agent=record.get("agent", ""),
                                  backend=record.get("backend", ""), started=record.get("started", ""),
                                  state=run_state(record), proxy=record.get("proxy"), pid=record.get("pid"),
                                  record=record))
        known = {str(i.pid) for i in found if i.pid} | {str(state.get("pid")) for state in servers.values()}
        info = self.procinfo()
        for pid, agent, argv, cwd in launcher_processes(self.agents, proc, info):
            if str(pid) in known:
                continue
            paths = paths_from_options(self.launcher, argv, cwd)
            project = paths["workspaces"][0] if paths["workspaces"] else cwd
            options = argv[:argv.index("--")] if "--" in argv else argv
            # An MCP server's (or a helper's) launcher that has not registered yet: it belongs to the start that is
            # starting it (start_tree.owners, by the time).
            server = any(o == "--mcp-socket-dir" or o.startswith("--mcp-socket-dir=") for o in options)
            found.append(Instance("unregistered", f"pid:{pid}", f"pid {pid}", project=project or "", agent=agent,
                                  backend=option_value(argv, "--backend") or "",
                                  started=process_started(pid, proc, info) or "",
                                  state="starting", pid=pid, record={"mcp_server": True} if server else None))
        self.add_team_leads(found)
        return found, problems

    def add_team_leads(self, found):
        """The members of a team (sandbox-ai team: a start that reaches the board team-NAME) learn their lead's
        start ("lead" in their record, as sandbox-ai list --json says it): the Sandboxes page shows them below it,
        and they open no tab of their own. Read from the team's record; nothing for the lead itself, or when there
        is no record (an older sandbox-ai, a damaged one)."""
        leads = {}
        for instance in found:
            if instance.kind not in ("session", "run") or not isinstance(instance.record, dict):
                continue
            record = instance.record
            names = [n for key in ("mcp_servers", "mcp") for n in (record.get(key) if isinstance(record.get(key), list)
                                                                   else [])]
            for name in dict.fromkeys(n for n in names if isinstance(n, str) and n.startswith("team-")):
                team = name[len("team-"):]
                if team not in leads:
                    try:
                        data = self.module("team").load(team)
                        leads[team] = f"{team}-{data['lead']}"
                    except Exception as _exc:  # noqa: BLE001
                        # (no team of that name, a damaged record, an older sandbox-ai)
                        applog.swallowed("backend", "SandboxAI.add_team_leads: no team of that name, a damaged …", _exc)
                        leads[team] = None
                lead = leads[team]
                if lead and lead != instance.name and instance.name.startswith(f"{team}-"):
                    instance.record = {**record, "lead": lead}
                    break

    def stop_run(self, run):
        """`sandbox-ai net stop RUN`: SIGTERM to the launcher of a one-off run, which cleans up and ends."""
        if not valid_run(run):
            return 2, "", "invalid run"
        return self.call("net", "stop", run, timeout=90)

    def stop_mcp(self, name):
        if not valid_run(name):
            return 2, "", "invalid name"
        return self.call("mcp", "stop", name, timeout=300)

    # -- the kill switch (sandbox-ai net cut) --

    def cut_supported(self):
        """Whether this sandbox-ai has the kill switch (sandbox-ai net cut, 0.1.94)."""
        return "cut" in self.net_commands()

    def cut_run(self, run, mode, host="local"):
        """`sandbox-ai net cut RUN MODE --json` on HOST (the run with its MCP servers, helpers and team): (status,
        out, err); the JSON report on stdout says each run's result (cut_report)."""
        if not valid_run(run) or mode not in CUT_MODES:
            return 2, "", "invalid run or mode"
        return self.host_run(host, "net", "cut", run, mode, "--json", timeout=CUT_WAIT)

    def cut_every_host(self, mode):
        """The emergency stop (MODE "all") or Lift every cut ("off") on this machine and on every host of `sandbox-ai
        hosts`, the hosts read afresh (not what a page saw last: right after the start it has seen none). A list of
        hosts that cannot be read is said as the result of "hosts" (this machine is cut all the same)."""
        try:
            hosts, problem = list(self.remote_hosts()), None
        except Exception as exc:  # noqa: BLE001 - this machine is cut regardless; the list's failure is said
            hosts, problem = [], str(exc) or type(exc).__name__
        found = self.cut_everywhere(mode, ["local", *hosts])
        if problem is not None:
            found["sandbox-ai hosts"] = (1, "", f"the list of hosts could not be read: {problem}")
        return found

    def cut_everywhere(self, mode, hosts=("local",)):
        """`sandbox-ai net cut --all-runs MODE --json` on each of HOSTS at once (the emergency stop must not wait for
        one host after another): {host: (status, out, err)}; a host that fails or does not answer says so."""
        if mode not in CUT_MODES:
            raise ValueError("invalid mode")
        found, threads = {}, []

        def one(host):
            try:
                found[host] = self.host_run(host, "net", "cut", "--all-runs", mode, "--json", timeout=CUT_WAIT)
            except Exception as exc:  # noqa: BLE001 - one host's failure is its own result
                found[host] = (1, "", str(exc))
        for host in dict.fromkeys(hosts):
            thread = threading.Thread(target=one, args=(host,), daemon=True)
            thread.start()
            threads.append((host, thread))
        for host, thread in threads:
            thread.join(CUT_WAIT + 30)
            found.setdefault(host, (124, "", "no answer in time"))
        return found

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
        data = json_for_change(self.settings_file())
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
        except Exception as _exc:  # noqa: BLE001
            # (an older sandbox-ai, a damaged start: the app's own list still counts)
            applog.swallowed("backend", "SandboxAI.recent_folders: an older sandbox-ai, a damaged start: the ap…", _exc)
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
        data = json_for_change(self.settings_file())
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
        except Exception as _exc:  # noqa: BLE001 - a damaged trust store: the other sources still count
            applog.swallowed("backend", "SandboxAI.known_projects: a damaged trust store: the other sources sti…", _exc)
        try:
            for row in self.sessions():
                add(row[4], "session")
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.known_projects failed (went on without it)", _exc)
        try:
            for record in self.runs().values():
                add(record.get("workspace"), "running")
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.known_projects failed (went on without it)", _exc)
        try:
            for project, _, _ in self.remembered():
                add(project, "approvals")
        except Exception as _exc:  # noqa: BLE001 - older sandbox-ai versions keep no such list
            applog.swallowed("backend", "SandboxAI.known_projects: older sandbox-ai versions keep no such list", _exc)
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
            return "unreadable", printable_text(exc)
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
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.has_kits_command failed (went on without it)", _exc)
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

    def source_text(self, name):
        """The text of sandbox-ai's module NAME, read once per version of the file (its mtime and size): the
        capability checks (has_kit_hooks, has_canary_log, ...) ran on the GUI thread at every refresh and read
        files of thousands of lines each time. OSError if it cannot be read."""
        path = self.directory / name
        info = path.stat()
        cache = self.__dict__.setdefault("_source_texts", {})
        key = (str(path), info.st_mtime_ns, info.st_size)
        if cache.get(name, (None,))[0] != key:
            cache[name] = (key, path.read_text())
        return cache[name][1]

    def has_kit_hooks(self):
        """This sandbox-ai's kits carry hooks (sandbox-ai kits add-hook, trust-hooks)."""
        try:
            return "trust-hooks" in self.source_text("kitctl.py")
        except OSError:
            return False

    def has_canary_log(self):
        """This sandbox-ai has `sandbox-ai canaries log` (every canary event with its process and links)."""
        try:
            return "def show_log(" in self.source_text("canary.py")
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

    # -- reports (sandbox-ai report): runs, statistics, network and the security report as Markdown, HTML or PDF --

    REPORT_FORMATS = ("pdf", "html", "md")

    def has_report(self):
        """This sandbox-ai writes reports (report.py, `sandbox-ai report`)."""
        return (self.directory / "report.py").is_file()

    def report(self, path, kind="pdf", names=(), log_dir=None, parts=True, since=None, traffic=False, host="local"):
        """`sandbox-ai report NAME... -f KIND -o PATH` (or --log-dir DIR for every run of a log folder): one report of
        the starts NAMES (each with its runs and, with PARTS, its MCP servers, helpers and team) written to PATH, from
        SINCE on (10m, 2h, an ISO time) when given; TRAFFIC: with the section on the LLM traffic log (--llm-traffic).
        HOST: the starts of a host of sandbox-ai hosts - the report made there (its logs are there), written to its
        standard output and into PATH here (HTML or Markdown: text over the connection; no log folder).
        Returns PATH; ValueError: not written."""
        if kind not in self.REPORT_FORMATS:
            raise ValueError(f"no report format {kind!r}")
        if host != "local":
            return self.host_report(host, path, kind, names, parts, since, traffic)
        if not valid_directory(path):
            raise ValueError("not an absolute path for the report")
        names = list(names or [])
        if not names and not log_dir:
            raise ValueError("no start and no log folder for the report")
        if any(not valid_run(name) for name in names):
            raise ValueError("invalid start")
        if log_dir is not None and not valid_directory(log_dir):
            raise ValueError("not an absolute path for the log folder")
        args = ["report", "--format", kind, "--output", str(path)]
        if not parts:
            args.append("--no-parts")
        if traffic:
            args.append("--llm-traffic")
        if since:
            if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z:.+ -]{0,39}", str(since)):
                raise ValueError("not a time")
            args += ["--since", str(since)]
        if log_dir is not None:
            args += ["--log-dir", str(log_dir)]
        status, out, err = self.call(*args, "--", *names, timeout=600)
        if status != 0:
            raise ValueError((err or out).strip() or f"sandbox-ai report: exit status {status}")
        return str(path)

    REMOTE_REPORT_FORMATS = ("html", "md")  # what a host's report comes back as (text on its standard output)

    def host_report(self, host, path, kind, names, parts=True, since=None, traffic=False):
        """report() of the starts NAMES of HOST: `sandbox-ai report -f KIND -- NAME...` there, its output written to
        PATH here (created new or replaced, 0600). ValueError: not made (an older sandbox-ai there: the update)."""
        if kind not in self.REMOTE_REPORT_FORMATS:
            raise ValueError(f"a report of another host comes as HTML or Markdown, not {kind!r}")
        if not valid_directory(path):
            raise ValueError("not an absolute path for the report")
        names = list(names or [])
        if not names or any(not valid_run(name) for name in names):
            raise ValueError("invalid start")
        args = ["report", "--format", kind, *([] if parts else ["--no-parts"]), *(["--llm-traffic"] if traffic else [])]
        if since:
            if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z:.+ -]{0,39}", str(since)):
                raise ValueError("not a time")
            args += ["--since", str(since)]
        result = self.host_run(host, *args, "--", *names, timeout=600)
        status, out, err = result
        if status != 0:
            raise ValueError(self.too_old_text(host, result, ("report",)) or (err or out).strip()[-800:]
                             or f"sandbox-ai report: exit status {status}")
        if not out.strip():
            raise ValueError("the report came back empty")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
            handle.write(out)
        return str(path)

    # -- safeguards (sandbox-ai safeguards): what refused or ended something in a run, why, and the fix --

    def has_safeguards(self):
        """This sandbox-ai records its safeguards (safeguards.py: safeguards.jsonl, `sandbox-ai safeguards`)."""
        return (self.directory / "safeguards.py").is_file()

    def safeguards(self, run=None, tail=1000, host="local"):
        """`sandbox-ai safeguards --json`: the records of RUN (of every run: the running ones and the default log
        directories, without RUN), oldest first, at most TAIL; on HOST (a host of sandbox-ai hosts) read there."""
        if run is not None and not valid_run(run):
            raise ValueError("invalid run")
        where = ["--", run] if run else ["--all"]
        args = ("safeguards", "--json", "--tail", str(int(tail)), *where)
        result = self.call(*args, timeout=60) if host == "local" else self.host_run(host, *args, timeout=90)
        status, out, err = result
        if status != 0:
            raise ValueError(self.too_old_text(host, result, ("safeguards",)) or (err or out).strip()
                             or f"sandbox-ai safeguards: exit status {status}")
        records = []
        for line in out.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def safeguard_kinds(self):
        """{kind: (level, title)} of this sandbox-ai's safeguards ({} for one without them)."""
        if not self.has_safeguards():
            return {}
        return {kind: (spec[0], spec[1]) for kind, spec in self.module("safeguards").KINDS.items()}

    def muted_safeguards(self):
        """The kinds muted in sandbox-ai's defaults file (safeguard_mute): recorded, never announced."""
        document, _ = self.sandbox_defaults()
        value = document.get("safeguard_mute")
        return set(value) if isinstance(value, list) else set()

    def trust_hooks_argv(self, name):
        """`sandbox-ai kits trust-hooks -- NAME`, for a terminal: it shows every hook and the full text of every
        file, and approves only after the human types yes there (never --yes from the app)."""
        if not re.fullmatch(KIT_NAME, str(name)):
            raise ValueError(f"{name!r} is not a kit name")
        return self.command("kits", "trust-hooks", "--", name)

    def has_marketplaces(self):
        """This sandbox-ai lists every marketplace (`sandbox-ai plugins marketplaces`, since 0.1.78)."""
        try:
            return "def marketplaces_cli(" in self.source_text("plugins.py")
        except OSError:
            return False

    def marketplaces(self):
        """`sandbox-ai plugins marketplaces --json` (version 1): every plugin marketplace (sandbox-ai's own, the kits',
        the plugin store's, Claude's and Codex's) with its plugins and their state, OpenCode's npm plugins, each
        agent's about and notes, the kits that can be chosen and the errors of kits. Read only: nothing is fetched,
        run or changed (the agents' own configuration is only read)."""
        status, out, err = self.call("plugins", "marketplaces", "--json", timeout=90)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"sandbox-ai plugins marketplaces: exit status {status}")
        return marketplaces_document(json.loads(out))

    def plugins_argv(self, update=False, kits=(), marketplaces_only=False, agent="claude"):
        """`sandbox-ai plugins claude [--update] [--marketplaces-only] [--kit NAME]...` (built-in kits when this
        sandbox-ai can). --marketplaces-only fetches the kits' marketplaces (their plugin lists) in the plugin store's
        sandbox and installs nothing."""
        return self.command(*self.plugins_args(update=update, kits=kits, marketplaces_only=marketplaces_only,
                                               agent=agent))

    def plugins_args(self, update=False, kits=(), marketplaces_only=False, agent="claude"):
        """The arguments of plugins_argv: `plugins AGENT [--update] [--marketplaces-only] [--kit NAME]...`; AGENT
        "all" (Claude's, Codex's and OpenCode 2's kit plugins, one store) only where this sandbox-ai has it, else
        claude."""
        try:
            source = self.source_text("plugins.py")
        except OSError:
            source = ""
        if agent == "all" and '"all"' not in source:
            agent = "claude"
        argv = ["plugins", agent] + (["--update"] if update else []) + \
            (["--marketplaces-only"] if marketplaces_only else [])
        supports = "--kit" in source
        if supports:
            argv += [token for name in kits if re.fullmatch(KIT_NAME, name) for token in ("--kit", name)]
        return argv

    # -- statistics --

    def statistics_argv(self, directory):
        """`sandbox-ai logs DIR --json`: its "telemetry" and "tool_calls" are what the Statistics page shows."""
        return self.command("logs", str(directory), "--json")

    def statistics(self, directory):
        """logs.telemetry of DIRECTORY plus the MCP servers' tool calls (in this process; the window runs
        statistics_argv in the background instead)."""
        logs = self.module("logs")
        data = logs.telemetry(directory)
        data["tool_calls"] = logs.tool_calls()
        return data

    # -- logins and secrets --

    def login_agents(self):
        """The agents `sandbox-ai login AGENT` takes here (its login.AGENTS: DSH and Goose have none), in the
        launcher's order."""
        try:
            takes = tuple(self.module("login").AGENTS)
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: the three it always had
            applog.swallowed("backend", "SandboxAI.login_agents: an older sandbox-ai: the three it always had", _exc)
            takes = LOGIN_AGENTS
        return tuple(agent for agent in self.agents if agent in takes)

    def login_status(self):
        """[(agent, where, problem, source)] of the agents `sandbox-ai login` takes (login_agents): where "store"
        (sandbox-ai's own login) or "host"; problem None, "missing" or "expired"."""
        launcher, rows = self.launcher, []
        for agent in self.login_agents():
            try:
                where, problem = launcher.login_state(agent)
                source = printable(launcher.auth_sources(agent)[0][0])
            except (OSError, ValueError) as exc:
                where, problem, source = "?", "error", printable_text(exc)
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

    def secrets_backend(self, host="local"):
        """Where `sandbox-ai secrets set` keeps a secret on HOST: "keyring" or "file" (`sandbox-ai secrets backend`
        there). ValueError: not answered."""
        status, out, err = self.host_run(host, "secrets", "backend", timeout=60)
        # "file", or since 0.1.98 "file (what protects it)": the first word
        word = (out.strip().splitlines() or [""])[-1].strip().split(" ", 1)[0]
        if status != 0 or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", word):
            raise ValueError(printable_text((err or out).strip())[-300:] or f"exit status {status}")
        return word

    def secrets_protection(self, host="local"):
        """What protects the secrets on HOST (`sandbox-ai secrets status --json` there): {"backend": keyring,
        keychain or file, "password": a primary password is set for the private files, "locked": True, False or
        None (no password), "left": seconds it stays unlocked, "plain": how many are in plain files, "encrypted",
        "plain_choice": plain files are that host's choice (SANDBOX_AI_SECRETS_PLAIN=1 there)}.
        None: the sandbox-ai there knows no primary password (before 0.1.98). ValueError: not answered."""
        status, out, err = self.host_run(host, "secrets", "status", "--json", timeout=60)
        if status == 2 and re.search(r"invalid choice|unrecognized arguments", err or ""):
            return None
        try:
            info = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            info = None
        if not isinstance(info, dict) or not isinstance(info.get("backend"), str):
            raise ValueError(printable_text((err or out).strip())[-300:] or f"exit status {status}")
        plain, left = info.get("plain"), info.get("left")
        return {"backend": printable(info["backend"])[:32], "password": info.get("password") is True,
                "locked": info.get("locked") if isinstance(info.get("locked"), bool) else None,
                "left": left if isinstance(left, int) and not isinstance(left, bool) else None,
                "plain": len(plain) if isinstance(plain, list) else 0,
                "encrypted": info.get("encrypted") if isinstance(info.get("encrypted"), int) else 0,
                "plain_choice": info.get("plain_choice") is True}

    def secrets_unlock(self, password, host="local"):
        """Unlock the encrypted secrets on HOST (`sandbox-ai secrets unlock --password-stdin` there): the primary
        password goes on standard input - over SSH too - never an argument. (status, out, err)."""
        if not isinstance(password, str) or not password or "\n" in password:
            return 2, "", "a primary password is one line"
        return self.host_run(host, "secrets", "unlock", "--password-stdin", timeout=120, input=password + "\n")

    def secrets_lock(self, host="local"):
        """`sandbox-ai secrets lock` on HOST: the key is forgotten there."""
        return self.host_run(host, "secrets", "lock", timeout=60)

    def secrets_new_password(self, password, host="local"):
        """A first primary password for the private files on HOST (`sandbox-ai secrets password set
        --password-stdin`: what is kept there is encrypted, and unlocked); the password on standard input."""
        if not isinstance(password, str) or not password or "\n" in password:
            return 2, "", "a primary password is one line"
        return self.host_run(host, "secrets", "password", "set", "--password-stdin", timeout=240,
                             input=password + "\n")

    def secret_exists(self, name, host="local"):
        """Whether a secret NAME is kept on HOST (names only are read)."""
        return any(found == name for found, _ in self.host_secrets(host)[1])

    def save_token(self, url, token):
        """Keep a push TOKEN typed in the wizard as sandbox-ai's own wizard does (since 0.1.98): as the secret
        GIT_TOKEN_HOST of sandbox-ai secrets (`secrets set NAME`, the token on its standard input - encrypted by the
        keyring or the primary password); the project file only names it (--git-token-secret URL=NAME). Returns the
        name. ValueError: not kept, with what the command said (locked: "... sandbox-ai secrets unlock"; no primary
        password yet: "... sandbox-ai secrets password set")."""
        if not isinstance(token, str) or not token.strip() or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError("a token is one line of printable characters")
        if not re.fullmatch(r"https?://[^\s/]+", str(url)):
            raise ValueError("a Git server is http(s)://[USER@]HOST[:PORT]")
        name = self.module("secret_store").token_secret_name(re.sub(r"^https?://", "", url))
        status, out, err = self.set_secret(name, token.strip())
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return name

    def moved_token(self, path):
        """The secret a token file of the wizard's was moved into (--git-token-file of an older project file), or
        None."""
        return self.module("secret_store").moved_token_name(Path(str(path)).expanduser())

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
        except Exception as _exc:  # noqa: BLE001 - the browser shows what it can
            applog.swallowed("backend", "SandboxAI.file_roots: the browser shows what it can", _exc)
            runs = {}
        sessions = self.module("sessions")
        try:
            ids = self.session_ids()  # {name shown: id}: a renamed session keeps its id (its directory)
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: the names are the ids
            applog.swallowed("backend", "SandboxAI.file_roots: an older sandbox-ai: the names are the ids", _exc)
            ids = {}
        names = {key: name for name, key in ids.items()}

        def key_of(name):
            """The id of the session NAME (its name shown, or its id already)."""
            if name in ids:
                return ids[name]
            if name in names:
                return name
            try:
                return sessions.resolve(name) if hasattr(sessions, "resolve") else name
            except ValueError:
                return name
        for name, record in runs.items():
            session = session_of(record)
            shown = names.get(key_of(session), session) if session else None
            add("run", shown or name, record.get("workspace"))
            paths = run_paths(self.launcher, record.get("pid"), info=self.procinfo())
            if session:
                try:
                    add("session-home", shown, sessions.session_dir(key_of(session)) / "home")
                except ValueError:
                    pass
            for workspace in paths["workspaces"]:
                add("run", shown or name, workspace)
            add("run-home", name, paths["state_dir"])
        try:
            rows = sessions.rows()
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.file_roots failed (went on without it)", _exc)
            rows = []
        for row in rows:
            name = row[0]  # the name shown (after a rename); load() and session_dir() take the session's id
            key = key_of(name)
            try:
                data = sessions.load(key)
            except (OSError, ValueError):
                continue
            add("session", name, data.get("workspace"))
            options = paths_from_options(self.launcher, data.get("options", []), data.get("cwd"))
            for workspace in options["workspaces"]:
                add("session", name, workspace)
            try:
                add("session-home", name, sessions.session_dir(key) / "home")
            except ValueError:
                pass
        for kind, label, path in extra:
            add(kind, label, path)
        return found

    def host_file_roots(self, name, timeout=HOST_FOLDERS_WAIT):
        """[(kind, label, path)] of the folders of the running sandboxes and kept sessions on host NAME of `sandbox-ai
        hosts` - their workspaces and HOMEs, as file_roots gives them for this machine, PATH a folder of that host (the
        Files page opens it over the host's SFTP connection) - read with one call there: `sandbox-ai list --folders
        --json` (sandbox-ai 0.1.99 on). None: its sandbox-ai is older and lists no folders (the host's own entry still
        works). ValueError: no answer within TIMEOUT seconds, or not such a list."""
        if not isinstance(name, str) or name == "local" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("invalid host")
        status, out, err = self.host_run(name, "list", "--folders", "--json", timeout=timeout)
        if status == 124:
            raise ValueError(f"no answer within {timeout:g} s")
        if status != 0 and "--folders" in (err or "") and ("unrecognized arguments" in err or
                                                            "unrecognised arguments" in err):
            return None
        try:
            data = json.loads(out)
        except (ValueError, RecursionError):
            raise ValueError(printable_text((err or out).strip()[-300:]) or f"exit status {status}") from None
        if not isinstance(data, list):
            raise ValueError("not a list of folders")
        found, seen = [], set()
        for entry in data:
            if not isinstance(entry, dict) or entry.get("kind") not in HOST_FOLDER_KINDS:
                continue
            path, label = entry.get("path"), entry.get("name")
            if not isinstance(path, str) or not path.startswith("/") or len(path) > 4096 or \
                    any(c in path for c in "\0\n\r"):
                continue
            path = posixpath.normpath(path)
            if path.startswith("//"):
                path = "/" + path.lstrip("/")
            if path in seen:
                continue
            seen.add(path)
            found.append((entry["kind"], printable(label)[:100] if isinstance(label, str) else "", path))
        return found[:HOST_FOLDERS_LIMIT]

    def sandbox_paths(self):
        """{host folder (resolved): its path in the sandbox} for the workspaces and HOME of every running sandbox and
        session on this host (sandbox_targets; HOME is SANDBOX_HOME): what the Files page's "Copy path in the
        sandbox" gives. A folder two of them share keeps the first one's."""
        found = {}

        def add(pairs):
            for host, inside in pairs:
                found.setdefault(os.path.realpath(host), inside)

        def ordered(first, others):
            """FIRST (the record's workspace: the one at /work, also when a project file chose it over --workspace)
            and the other workspaces after it, each once."""
            first = os.path.realpath(os.path.expanduser(str(first))) if first else None
            rest = [os.path.realpath(os.path.expanduser(str(w))) for w in others if w]
            return [*([first] if first else []), *(w for w in dict.fromkeys(rest) if w != first)]
        try:
            runs = self.runs()
        except Exception as _exc:  # noqa: BLE001 - what can be read
            applog.swallowed("backend", "SandboxAI.sandbox_paths: what can be read", _exc)
            runs = {}
        sessions = self.module("sessions")
        for record in runs.values():
            paths = run_paths(self.launcher, record.get("pid"), info=self.procinfo())
            add(sandbox_targets(ordered(record.get("workspace"), paths["workspaces"]), paths.get("workspace_names")))
            if paths["state_dir"]:
                add([(paths["state_dir"], SANDBOX_HOME)])
        try:
            rows = sessions.rows()
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.sandbox_paths failed (went on without it)", _exc)
            rows = []
        for row in rows:
            try:
                key = sessions.resolve(row[0]) if hasattr(sessions, "resolve") else row[0]
                data = sessions.load(key)
            except (OSError, ValueError):
                continue
            options = paths_from_options(self.launcher, data.get("options", []), data.get("cwd"))
            add(sandbox_targets(ordered(data.get("workspace"), options["workspaces"]), options["workspace_names"]))
            try:
                add([(sessions.session_dir(key) / "home", SANDBOX_HOME)])
            except ValueError:
                pass
        return found

    def sandbox_folders(self):
        """The folders sandboxes can or could write: the workspaces, HOMEs and rw --mount sources of running
        sandboxes and sessions, sandbox-ai's registry of what earlier runs exposed (exposed_before), and
        what preload_trees() finds without modules."""
        folders = [path for kind, _, path in self.file_roots() if kind in ("run", "run-home", "session", "session-home")]
        folders += self.rw_mounts()
        try:
            folders += [str(p) for p in self.launcher.exposed_before()]
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: the registry is read directly below
            applog.swallowed("backend", "SandboxAI.sandbox_folders: an older sandbox-ai: the registry is read d…", _exc)
        folders += preload_trees()
        return list(dict.fromkeys(os.path.realpath(f) for f in folders))

    def rw_mounts(self):
        """The rw --mount sources of running sandboxes (their command lines) and sessions (their options)."""
        found = []
        try:
            runs = self.runs()
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.rw_mounts failed (went on without it)", _exc)
            runs = {}
        for record in runs.values():
            found += run_paths(self.launcher, record.get("pid"), info=self.procinfo())["mounts"]
        sessions = self.module("sessions")
        try:
            rows = sessions.rows()
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.rw_mounts failed (went on without it)", _exc)
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
        data = json_for_change(self.settings_file())
        changed = {key: value for key, value in values.items() if data.get(key) != value}
        data.update(values)
        try:
            write_private_json(self.settings_file(), data)
        except OSError as exc:
            applog.log("WARNING", "settings", f"{self.settings_file()}: not written ({applog.error_text(exc)}): "
                                              f"{', '.join(map(str, values))}")
            raise
        if changed:  # (the values of settings whose name says secret are not shown)
            applog.log("INFO", "settings", "changed: " + ", ".join(
                f"{key}=" + ("***" if applog.SECRET_OPTION.match(f'--{key}') else applog.redact(repr(value))[:200])
                for key, value in changed.items()))

    # -- other hosts (sandbox-ai hosts): machines whose sandbox-ai this one manages over SSH --

    def hosts_supported(self):
        return (self.directory / "hosts.py").is_file()

    def hosts_file(self):
        """Where sandbox-ai keeps its list of hosts (hosts.config_file: the same rule, without loading hosts.py)."""
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "hosts.json"

    def host_entries(self):
        """[{"name", "kind", "ssh", "port", "description", ...}]: this machine ("local") first."""
        return self.module("hosts").entries()

    def host_add(self, name, ssh, port=22, identity="", description="", command="", update_sudo=False):
        """`sandbox-ai hosts add NAME USER@HOST ...`; COMMAND: sandbox-ai's path there if not the usual, UPDATE_SUDO:
        its sandbox-ai is a system installation (updated with sudo)."""
        # (each value joined to its option and the names after "--": a description "-prod" or a key path typed with
        # a leading "-" is never read as an option of its own)
        args = ["hosts", "add", f"--port={int(port)}"]
        if identity:
            args.append(f"--identity={identity}")
        if description:
            args.append(f"--description={description}")
        if command:
            args.append(f"--command={path_value(command, 'sandbox-ai on the host')}")
        if update_sudo:
            args.append("--update-sudo")
        return self.checked(*args, "--", name, ssh, timeout=30)

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
        except (ValueError, RecursionError):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def host_run(self, name, *args, timeout=120, input=None):
        """(status, out, err) of `sandbox-ai ARGS` on host NAME (this machine for "local"). INPUT, if given, is its
        standard input (a secret's value: never an argument); `sandbox-ai hosts run` hands its own on to ssh.
        Through the host's shared SSH connection, as `sandbox-ai hosts run` does it (shared_connection)."""
        extra = {} if input is None else {"input": input}
        if name == "local":
            return self.call(*args, timeout=timeout, **extra)
        deadline = time.monotonic() + timeout
        shared = self.shared_connection(name, deadline)
        argv = self.ssh_argv(name, args, control=shared[1] if shared else None)
        if argv is None:
            shared = None
        left = max(1, deadline - time.monotonic()) if shared else timeout
        status, out, err = self.call("hosts", "run", name, "--", *args, timeout=left, argv=argv, **extra)
        if shared is None:
            return status, out, err
        hosts, (entry, control) = self.module("hosts"), shared
        if status == 255 or (status == 124 and not out):
            # As sandbox-ai's own hosts.run: the master may be dead (it still answers -O check): it takes no new
            # sessions, the next command makes a new one; run once more on a connection of its own only when the
            # command never reached the host (the master refused the session) - never a command that may have run.
            self.forget_master(control)
            with contextlib.suppress(Exception):
                hosts.stop_master(entry, control, how="stop")
            if status == 255 and hosts.mux_refused(out, err):
                argv = self.ssh_argv(name, args)
                status, out, err = self.call("hosts", "run", name, "--", *args, argv=argv, timeout=max(
                    deadline - time.monotonic(), min(timeout, getattr(hosts, "CONNECT_TIMEOUT", 10) + 5)), **extra)
        elif "Control socket connect(" in (err or ""):
            self.forget_master(control)  # a socket nobody answers: checked (and cleaned up) at the next command
        elif status not in (124, 127, CANCELLED):
            with self._masters_lock:  # it carried this command: used without a check for MASTER_TRUSTED seconds
                self._masters[str(control)] = time.monotonic()
        return status, out, err

    def shared_connection(self, name, deadline):
        """(entry, control socket) of host NAME's shared SSH connection (sandbox-ai's hosts module: one ssh master per
        host, ControlPersist), started when needed, or None (switched off, an older sandbox-ai, it could not start:
        the command connects on its own). A read through it costs a session on an open connection - not a TCP
        connection, a key exchange and a login on the host every few seconds. A connection that carried a command
        in the last MASTER_TRUSTED seconds is used without asking it first (`ssh -O check`, a process each time)."""
        if self.run is not subprocess.run:
            return None
        try:
            hosts = self.module("hosts")
            if not all(callable(getattr(hosts, n, None)) for n in
                       ("load", "control_path", "ensure_master", "stop_master", "mux_refused", "argv")) or \
                    "control" not in inspect.signature(hosts.argv).parameters:
                return None
            entry = hosts.load().get(name)
            control = hosts.control_path(entry) if isinstance(entry, dict) else None
            if control is None:
                return None
            with self._masters_lock:
                used = self._masters.get(str(control))
            if used is not None and time.monotonic() - used < MASTER_TRUSTED and control.exists():
                return entry, control
            if not hosts.ensure_master(entry, control, deadline=deadline):
                return None
        except Exception as _exc:  # noqa: BLE001 - the command connects on its own (and says what is wrong)
            applog.swallowed("backend", "SandboxAI.shared_connection: the command connects on its own (and says…", _exc)
            return None
        return entry, control

    def forget_master(self, control):
        with self._masters_lock:
            self._masters.pop(str(control), None)

    def ssh_argv(self, name, args, control=None):
        """The command line `sandbox-ai hosts run NAME -- ARGS` runs (hosts.argv: ssh with the host's settings and the
        remote command; CONTROL: through that shared connection), built here with sandbox-ai's own hosts module: a
        read of another host - the polls ask each host every few seconds - costs an ssh, not two Python starts
        (main.py, hosts.py: about 0.1 s of CPU) on top. None - the command itself runs then - with a runner of the
        tests, an older sandbox-ai, or a host it does not know (its own message says so)."""
        if self.run is not subprocess.run:
            return None
        try:
            hosts = self.module("hosts")
            if not callable(getattr(hosts, "argv", None)):
                return None
            argv = hosts.argv(name, [str(arg) for arg in args], **({"control": control} if control else {}))
        except Exception as _exc:  # noqa: BLE001 - sandbox-ai's own command reports it
            applog.swallowed("backend", "SandboxAI.ssh_argv: sandbox-ai's own command reports it", _exc)
            return None
        return list(argv) if argv and isinstance(argv, (list, tuple)) and all(isinstance(a, str) for a in argv) \
            else None

    def host_instances(self, name, timeout=60):
        """The starts on host NAME (`sandbox-ai list --json` there, over SSH) as [Instance] with HOST set: what the
        Sandboxes page lists beside this account's own."""
        return [i for i in (listed_instance(name, entry) for entry in self.host_json(name, "list", "--json",
                                                                                  timeout=timeout)) if i is not None]

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

    # --- the workspace checkpoints of a start (sandbox-ai checkpoint) -------------------------------------------------

    def checkpoints_supported(self, host="local"):
        """Whether this sandbox-ai has `sandbox-ai checkpoint` (looked up once; a host's: its version, 0.1.99 on)."""
        if host != "local":
            status = (self.host_statuses(cached=True) or {}).get(host) or {}
            version = str(status.get("version") or "")
            parts = tuple(int(x) for x in re.findall(r"\d+", version)[:3])
            return not parts or parts >= (0, 1, 99)
        known = self.__dict__.get("_checkpoints_supported")
        if known is None:
            known = self.__dict__["_checkpoints_supported"] = (self.directory / "checkpoints.py").is_file()
        return known

    def checkpoints(self, name, host="local"):
        """The checkpoints of the start NAME on HOST, newest first (`sandbox-ai checkpoint list NAME --json`); [] when
        it has none yet. ValueError: not answered."""
        if not valid_run(name):
            raise ValueError("invalid start")
        status, out, err = self.host_run(host, "checkpoint", "list", name, "--json", timeout=60)
        if status != 0:
            text = (err or out).strip()
            if "has checkpoints" in text:  # (no start of that name has any yet)
                return []
            raise ValueError(printable(text.splitlines()[-1] if text else f"exit status {status}")[:500])
        data = json.loads(out or "[]")
        return [entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("id"), str)
                and CHECKPOINT_ID.fullmatch(entry["id"])] if isinstance(data, list) else []

    def checkpoint_create(self, name, host="local", label=None):
        """A checkpoint of the start NAME's workspaces now (`checkpoint create NAME --json [--label TEXT]`): its
        record. ValueError: not taken (its words)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        if label is not None and (not isinstance(label, str) or len(label) > 500 or any(c in label for c in "\n\r\0")):
            raise ValueError("a label is one line")
        return self.checkpoint_json(host, "checkpoint", "create", name, "--json",
                                    *([f"--label={label}"] if label else []), timeout=CHECKPOINT_SECONDS)

    def checkpoint_diff(self, ident, host="local"):
        """What changed since the checkpoint IDENT (`checkpoint diff ID --json`): {"roots", "counts"}."""
        return self.checkpoint_json(host, "checkpoint", "diff", checkpoint_id(ident), "--json",
                                    timeout=CHECKPOINT_SECONDS)

    def checkpoint_preview(self, ident, host="local", paths=()):
        """What restoring IDENT would change (`checkpoint restore ID --dry-run --json [--path P]...`): nothing is
        written."""
        return self.checkpoint_json(host, "checkpoint", "restore", checkpoint_id(ident), "--dry-run", "--json",
                                    *checkpoint_paths(paths), timeout=CHECKPOINT_SECONDS)

    def checkpoint_restore(self, ident, host="local", paths=()):
        """IDENT put back (`checkpoint restore ID --yes --json [--path P]...`; asked first: checkpoint-restore); a
        checkpoint of the state before is taken first ("before")."""
        return self.checkpoint_json(host, "checkpoint", "restore", checkpoint_id(ident), "--yes", "--json",
                                    *checkpoint_paths(paths), timeout=CHECKPOINT_SECONDS)

    def checkpoint_delete(self, ident, host="local"):
        """The checkpoint IDENT removed (`checkpoint delete ID --yes`; asked first: checkpoint-delete)."""
        status, out, err = self.host_run(host, "checkpoint", "delete", checkpoint_id(ident), "--yes", timeout=300)
        if status != 0:
            text = (err or out).strip()
            raise ValueError(printable(text.splitlines()[-1] if text else f"exit status {status}")[:500])
        return True

    def checkpoint_json(self, host, *args, timeout=120):
        """The JSON object of `sandbox-ai ARGS` on HOST; ValueError with its last words when it failed."""
        status, out, err = self.host_run(host, *args, timeout=timeout)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            text = (err or out).strip()
            raise ValueError(printable(text.splitlines()[-1] if text else f"exit status {status}")[:500])
        return data

    # --- the workspace review after each run, and the MCP tool definitions pinned per project ------------------------

    def workspace_reviews(self, host="local"):
        """What the runs of every project on HOST changed in files the host runs or trusts (`sandbox-ai workspace review
        --all --json`): [{"run", "time", "log_dir", "stored", "changes": [{"workspace", "sandbox", "path", "change",
        "why", "diff", "findings", "restorable", "why_not", "decision"}]}], oldest first. ValueError: not answered."""
        status, out, err = self.host_run(host, "workspace", "review", "--all", "--json", timeout=120)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, list):
            text = (err or out).strip()
            raise ValueError(printable_text(text.splitlines()[-1] if text else f"exit status {status}")[:500])
        return [item for item in data if isinstance(item, dict) and isinstance(item.get("changes"), list)
                and valid_run(item.get("run")) and absolute_line(item.get("log_dir"))]

    def workspace_review_decide(self, log_dir, run, path, decision, host="local"):
        """Keep (DECISION "keep") the change of PATH in RUN, or restore the file as it was at the start ("restore";
        asked first: workspace-restore) - `sandbox-ai workspace review RUN --log-dir DIR --keep|--restore PATH [--yes]
        --json` on HOST. ValueError: refused (its words)."""
        if decision not in ("keep", "restore"):
            raise ValueError("a decision is keep or restore")
        if not valid_run(run) or not absolute_line(log_dir) or not plain_line(path):
            raise ValueError("invalid run, log folder or path")
        status, out, err = self.host_run(host, "workspace", "review", run, f"--log-dir={log_dir}",
                                         f"--{decision}={path}", *(["--yes"] if decision == "restore" else []),
                                         "--json", timeout=120)
        try:
            data = json.loads(out)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or status != 0:
            failed = data.get("failed") if isinstance(data, dict) else None
            text = (failed[0].get("error") if failed and isinstance(failed[0], dict) else None) or (err or out).strip()
            raise ValueError(printable_text(str(text).splitlines()[-1] if text else f"exit status {status}")[:500])
        return data

    def mcp_tool_pins(self, host="local"):
        """The MCP tool definitions pinned for every project on HOST (`sandbox-ai mcp tools --all --json`): [{"project",
        "server", "tools", "pinned", "how", "seen", "differs"}]. ValueError: not answered."""
        status, out, err = self.host_run(host, "mcp", "tools", "--all", "--json", timeout=60)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, list):
            text = (err or out).strip()
            raise ValueError(printable_text(text.splitlines()[-1] if text else f"exit status {status}")[:500])
        return [entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("server"), str)
                and re.fullmatch(MCP_NAME, entry["server"])]

    def mcp_tool_pin(self, project, server, host="local"):
        """SERVER's pin for PROJECT with the list seen last and how it differs (`mcp tools NAME --project DIR --json`):
        {"server", "pinned", "how", "tools": {name: {"digest", "definition"}}, "seen", "changes"} or None."""
        status, out, err = self.host_run(host, "mcp", "tools", self.pin_server(server), "--json",
                                         f"--project={self.pin_project(project)}", timeout=60)
        try:
            data = json.loads(out) if status == 0 else ValueError
        except (ValueError, RecursionError):
            data = ValueError
        if data is ValueError or not (data is None or isinstance(data, dict)):
            text = (err or out).strip()
            raise ValueError(printable_text(text.splitlines()[-1] if text else f"exit status {status}")[:500])
        return data

    def mcp_pin_change(self, project, server, action, host="local"):
        """Re-pin SERVER for PROJECT to the list seen last (ACTION "repin"; asked first: mcp-pin-repin) or drop its pin
        (ACTION "forget"; asked first: mcp-pin-forget) on HOST. ValueError: refused (its words)."""
        if action not in ("repin", "forget"):
            raise ValueError("re-pin or forget")
        status, out, err = self.host_run(host, "mcp", "tools", self.pin_server(server), f"--{action}", "--json",
                                         f"--project={self.pin_project(project)}", timeout=60)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            text = (err or out).strip()
            raise ValueError(printable_text("\n".join(text.splitlines()[-12:]) if text else f"exit status {status}")
                             [:2000])
        return data

    @staticmethod
    def pin_server(server):
        if not isinstance(server, str) or not re.fullmatch(MCP_NAME, server):
            raise ValueError("invalid MCP server name")
        return server

    @staticmethod
    def pin_project(project):
        if not absolute_line(project):
            raise ValueError("a project is an absolute folder")
        return project

    # --- a start's agent memory and session files, read only; its folder as one archive (sandbox-ai session) ---------

    def session_inspect_supported(self, host="local"):
        """Whether this sandbox-ai has `sandbox-ai session memory|files|export` (a host's: its version, 0.1.99 on)."""
        if host != "local":
            status = (self.host_statuses(cached=True) or {}).get(host) or {}
            parts = tuple(int(x) for x in re.findall(r"\d+", str(status.get("version") or ""))[:3])
            return not parts or parts >= (0, 1, 99)
        known = self.__dict__.get("_session_inspect_supported")
        if known is None:
            known = self.__dict__["_session_inspect_supported"] = (self.directory / "session_inspect.py").is_file()
        return known

    def hide_secrets_here(self):
        """Settings > Privacy: Hide logins and keys in file views (hide_secrets of this machine's defaults)."""
        return self.sandbox_defaults()[0].get("hide_secrets") is True

    def file_view_json(self, host, *args, timeout=120):
        """checkpoint_json of a file view (`session memory|files`): of another host with --hide-secrets when this
        machine's setting hides logins and keys (this machine's sandbox-ai reads it itself); an older sandbox-ai there
        knows no --hide-secrets - it never lists them - and is asked again without it."""
        if host == "local" or not self.hide_secrets_here():
            return self.checkpoint_json(host, *args, timeout=timeout)
        try:
            return self.checkpoint_json(host, *args, "--hide-secrets", timeout=timeout)
        except ValueError as exc:
            if "--hide-secrets" not in str(exc):
                raise
        return self.checkpoint_json(host, *args, timeout=timeout)

    def session_memory(self, name, host="local", source="auto", file=None, show_secret=False):
        """The agent's memory of the start NAME on HOST (`sandbox-ai session memory NAME --from SOURCE --json [--file
        F [--show-secret]]`): {"source": "home"|"store", "path", "files": [{"name", "size", "modified", "secret"(,
        "text", "binary")}], "notes", "hidden"}. SHOW_SECRET: the text of a file marked as a login or key (asked
        first: show-secret-file). ValueError: not read (its words)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        if source not in ("auto", "home", "store"):
            raise ValueError("invalid source")
        return self.file_view_json(host, "session", "memory", name, f"--from={source}", "--json",
                                   *([f"--file={listed_file(file)}"] if file is not None else []),
                                   *(["--show-secret"] if show_secret and file is not None else []), timeout=120)

    def session_files(self, name, host="local", file=None, full=False, show_secret=False):
        """The agents' session files of the start NAME on HOST (`sandbox-ai session files NAME --json [--file F
        [--full] [--show-secret]]`): {"home", "files": [{"name", "agent", "size", "modified", "format", "verified",
        "secret"}], "hidden"}, with FILE its "messages" [{"role", "time", "text", "tool", "error"}] and "notes".
        SHOW_SECRET: a file marked as a login or key (asked first: show-secret-file). ValueError: not read (its
        words)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        return self.file_view_json(host, "session", "files", name, "--json",
                                   *([f"--file={listed_file(file)}"] if file is not None else []),
                                   *(["--full"] if full and file is not None else []),
                                   *(["--show-secret"] if show_secret and file is not None else []), timeout=300)

    def session_export(self, name, out, include_secrets=False, with_workspace=False):
        """The folder of the kept start NAME of this machine as a new tar.gz OUT (`sandbox-ai session export NAME OUT
        --json [--include-secrets --yes]`): {"path", "files", "bytes", "left_out": [{"path", "why", "secret"}],
        "secrets_left_out", "include_secrets"}. INCLUDE_SECRETS: its logins and keys too (asked first:
        session-export-secrets). ValueError: not written (its words)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        if not isinstance(out, str) or not os.path.isabs(out) or any(c in out for c in "\n\r\0"):
            raise ValueError("choose where the archive is written (a new file of this machine)")
        return self.checkpoint_json("local", "session", "export", name, "--json",
                                    *(["--include-secrets", "--yes"] if include_secrets else []),
                                    *(["--with-workspace"] if with_workspace else []), "--", out,
                                    timeout=3600)

    def collect(self, host="local", dry_run=True, days=None, caches=False):
        """`sandbox-ai gc [--older-than DAYS] [--caches] [--dry-run] --json` on HOST: {"removed", "leftovers",
        "histories", "skipped", "damaged", "days", "dry_run", "compacted", "caches"}. CACHES: the download caches of
        the stopped kept starts' HOMEs too. ValueError: not answered."""
        if days is not None and not (isinstance(days, int) and not isinstance(days, bool) and 0 <= days <= 36500):
            raise ValueError("invalid number of days")
        data = self.host_json(host, "gc", *([f"--older-than={days}"] if days is not None else []),
                              *(["--caches"] if caches else []), *(["--dry-run"] if dry_run else []), "--json",
                              timeout=300 if dry_run else 1800)  # (compressing gigabytes of logs takes minutes)
        if not isinstance(data, dict):
            raise ValueError("not what sandbox-ai gc says")
        return data

    def collect_as_shown(self, host, shown, caches=False):
        """`sandbox-ai gc` on HOST removing what its dry run SHOWN listed and nothing else: with the same
        --older-than (and --caches), and only when a dry run now still lists exactly that (GcChanged otherwise: a start
        was used or ended meanwhile, keep_days changed). Returns what gc says it removed."""
        days = shown.get("days") if isinstance(shown, dict) else None
        days = days if isinstance(days, int) and not isinstance(days, bool) and days >= 0 else None
        extra = {"caches": True} if caches else {}
        if gc_plan(self.collect(host, True, days=days, **extra)) != gc_plan(shown):
            raise GcChanged(host)
        return self.collect(host, False, days=days, **extra)

    def state_usage(self, host="local"):
        """`sandbox-ai gc --usage --json` on HOST: how big each part of what sandbox-ai keeps there is, the biggest
        items, and what gc would free ({"total", "parts", "podman", "would_free", "scan"}). ValueError: not answered
        (an older sandbox-ai there knows no --usage)."""
        data = self.host_json(host, "gc", "--usage", "--json", timeout=330)
        if not isinstance(data, dict) or not isinstance(data.get("parts"), dict):
            raise ValueError("not what sandbox-ai gc --usage says")
        return data

    def home_details(self, host="local", name=None):
        """`sandbox-ai gc --usage --detail [NAME] --json` on HOST: what is in the kept HOMEs (or NAME's) by kind
        ({"starts", "kinds", "scan"}). ValueError: not answered (an older sandbox-ai there knows no --detail)."""
        data = self.host_json(host, "gc", "--usage", "--detail", *([name] if name else []), "--json", timeout=330)
        if not isinstance(data, dict) or not isinstance(data.get("starts"), list):
            raise ValueError("not what sandbox-ai gc --usage --detail says (sandbox-ai 0.1.98 or later knows it)")
        return data

    def storage_check(self, host="local", name=None, dry_run=False):
        """`sandbox-ai gc --check [--home NAME] [--dry-run] --json` on HOST: the storage check's plan ({"plan":
        [{"kind", "start", "what", "bytes", "estimate", "argv", "command", "safe_running", "checked"}], "total",
        "threshold", "above", "grew", "starts", "said", "notice"}; with NAME that start's storage: "detail", "logs",
        "checkpoints", "compacted", "packed"). Without DRY_RUN the storage rules are applied and the sizes recorded
        (the app's own check). ValueError: not answered (sandbox-ai 0.1.99 or later knows --check)."""
        if name is not None and not valid_run(name):
            raise ValueError("invalid start")
        data = self.host_json(host, "gc", "--check", "--json", *(["--dry-run"] if dry_run else []),
                              *(["--home", name] if name else []), timeout=900)
        if not isinstance(data, dict) or not isinstance(data.get("plan"), list):
            raise ValueError("not what sandbox-ai gc --check says (sandbox-ai 0.1.99 or later knows it)")
        return data

    def storage_apply(self, host, argv):
        """One item of the storage check's plan: its command ARGV (storage_argv: only the gc forms the plan names)
        run on HOST, with --json. What gc says ({} when it says no JSON). ValueError: it failed (its words)."""
        argv = storage_argv(argv)
        status, out, err = self.host_run(host, *argv, "--json", timeout=3600)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        try:
            found = json.loads(out)
        except (ValueError, RecursionError):
            return {}
        return found if isinstance(found, dict) else {}

    def storage_last_check(self):
        """When this machine's last storage check ran (seconds since the epoch; storage.json), or None."""
        try:
            storage = self.module("storage")
            found = storage.read_state(storage.STATE).get("last_check")
            return storage.seconds(found.get("time")) if isinstance(found, dict) else None
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai has no storage.py
            applog.swallowed("backend", "SandboxAI.storage_last_check: none", _exc)
            return None

    def storage_sizes(self):
        """{start id: {"bytes", "growth", "measured", "since"}} of this machine's last storage check (one small file;
        {} before one, or with an older sandbox-ai)."""
        try:
            return self.module("storage").last_sizes()
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai has no storage.py
            applog.swallowed("backend", "SandboxAI.storage_sizes: none (an older sandbox-ai, or not readable)", _exc)
            return {}

    def host_net_options(self, name, command="allow"):
        """The options `sandbox-ai net COMMAND` takes on host NAME (its --help there, parsed), kept per host and
        sandbox-ai version (for NET_OPTIONS_SECONDS without a known version). ValueError: no answer."""
        return self.host_command_options(name, "net", command)

    def host_command_options(self, name, *command, fresh=False):
        """The options `sandbox-ai COMMAND... --help` lists on host NAME, kept per host, command and sandbox-ai version
        (for NET_OPTIONS_SECONDS without a known version); FRESH: asked now. ValueError: no answer."""
        try:
            version = (self.host_statuses(cached=True).get(name) or {}).get("version")
        except Exception as _exc:  # noqa: BLE001
            # (an older sandbox-ai, or no list of hosts: asked every NET_OPTIONS_SECONDS)
            applog.swallowed("backend", "SandboxAI.host_command_options: an older sandbox-ai, or no list of hos…", _exc)
            version = None
        cache = self.__dict__.setdefault("_net_options", {})
        key = (name, *command, version)
        kept = cache.get(key)
        if kept is not None and not fresh and (version or time.time() - kept[0] < NET_OPTIONS_SECONDS):
            return set(kept[1])
        status, out, err = self.host_run(name, *command, "--help", timeout=60)
        found = set(re.findall(r"(?<![\w-])--[a-z0-9][a-z0-9-]*", out)) if status == 0 else set()
        if not found:
            raise ValueError((err or out).strip()[-300:] or f"exit status {status}")
        cache[key] = (time.time(), found)
        return found

    def host_answer(self, host, argv, timeout=60):
        """`sandbox-ai approvals allow|deny ID --scope S [--no-open]` (ARGV) on HOST: (status, out, err); --no-open only
        where that host's sandbox-ai knows it (else said: update the host)."""
        if len(argv) < 2 or argv[0] != "approvals" or argv[1] not in ("allow", "deny"):
            return 2, "", "not an answer"
        try:
            self.require_options(host, ("approvals", argv[1]), [o for o in argv if o == "--no-open"])
        except HostTooOld as exc:
            return 2, "", str(exc)
        return self.host_run(host, *argv, timeout=timeout)

    def host_answer_relaying(self, host, argv, ident, url, timeout=60):
        """host_answer, then - an allowed browser question of another HOST whose page opens here and whose URL sends
        the browser back to the sandbox's loopback (its "callback", approvals list --json) - this machine's one-shot
        catcher for that callback: `sandbox-ai approvals relay ID --url URL --host HOST` here, which forwards the one
        request over the host's SSH connection (`approvals callback ID` there). Its note (or why not: a busy port,
        use the device-code login) is added to the answer's output; the answer stands either way."""
        status, out, err = self.host_answer(host, argv, timeout=timeout)
        if status != 0:
            return status, out, err
        _, said, why = self.call("approvals", "relay", str(ident), "--url", str(url), "--host", str(host),
                                       timeout=30)
        note = (said or why).strip()
        return status, "\n".join(part for part in (out.strip(), note) if part), err

    def require_options(self, host, command, options):
        """Before COMMAND (a tuple of words after sandbox-ai) with OPTIONS that newer sandbox-ai versions brought goes
        to another HOST: HostTooOld when that host's `COMMAND --help` does not list them all - said plainly (update
        the host) instead of its "unrecognized arguments". This machine's sandbox-ai is the app's own: no check.
        When the host's help cannot be read, nothing is said here (the command's own error will be)."""
        options = [o for o in options if o]
        if host == "local" or not options:
            return
        try:
            known = self.host_command_options(host, *command)
        except Exception as _exc:  # noqa: BLE001 - not answered: the command itself goes, and says what is wrong
            applog.swallowed("backend", "SandboxAI.require_options: not answered: the command itself goes, and …", _exc)
            return
        missing = [o for o in options if o not in known]
        if missing:
            # What was kept may be from before an update there (user report 2026-11-23: --toolchain refused on
            # coding-101 just updated): the host is asked once more before a start is refused.
            try:
                known = self.host_command_options(host, *command, fresh=True)
            except Exception as _exc:  # noqa: BLE001 - not answered now: the command itself goes, and says
                applog.swallowed("backend", "SandboxAI.require_options: asked again, not answered", _exc)
                return
            missing = [o for o in options if o not in known]
        if missing:
            raise HostTooOld(host, command, missing)

    def forget_host_options(self, name):
        """What host_command_options kept of host NAME (after its sandbox-ai changed: an update, an installation, an
        uninstall)."""
        for cache in (self.__dict__.get("_net_options") or {}, self.__dict__.get("_launch_options") or {}):
            for key in [key for key in cache if key and key[0] == name]:
                cache.pop(key, None)

    def too_old_text(self, host, result, command):
        """What to say when the sandbox-ai on HOST answered RESULT ((status, out, err) of `sandbox-ai COMMAND...` there)
        with its usage - an older version that does not know COMMAND (`invalid choice`, `unrecognized arguments`): the
        update to do (HostTooOld with this machine's version), else None (this machine, another failure, success)."""
        if host in (None, "local") or not isinstance(result, tuple) or len(result) != 3 or result[0] != 2:
            return None
        text = f"{result[2] or ''}\n{result[1] or ''}"
        if not re.search(r"invalid choice|unrecognized arguments|the following arguments are required", text):
            return None
        try:
            version = self.version()
        except Exception as _exc:  # noqa: BLE001 - not known here: said without it
            applog.swallowed("backend", "SandboxAI.too_old_text: this machine's version is not known", _exc)
            version = "?"
        words = command.split() if isinstance(command, str) else command
        return str(HostTooOld(host, tuple(words), [], needs=version))

    def remote_hosts(self):
        """The names of the hosts of `sandbox-ai hosts` other than this machine ([] without hosts)."""
        if not self.hosts_supported():
            return []
        return [e["name"] for e in self.host_entries() if e.get("kind") != "local" and e.get("name") != "local"]

    def host_questions(self, name, timeout=REMOTE_QUESTION_WAIT):
        """The questions of the sandboxes on host NAME in every state (`sandbox-ai approvals list --state all --json`
        there, over SSH; an older sandbox-ai there, which knows no --state: its open ones) as [RemoteQuestion]; what
        does not look like a question of sandbox-ai is left out. ValueError: no answer. Their time left counts from
        when the read STARTED: that host said "seconds_left" somewhere during the SSH round trip, so a slow read never
        shows more time than there is."""
        started = time.time()
        if name in getattr(self, "_states_unknown", set()):
            data = self.host_json(name, "approvals", "list", "--json", timeout=timeout)
        else:
            try:
                data = self.host_json(name, "approvals", "list", "--state", "all", "--json", timeout=timeout)
            except ValueError as exc:
                if not re.search(r"unrecognized arguments|invalid choice", str(exc)):
                    raise
                self._states_unknown = {*getattr(self, "_states_unknown", set()), name}
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
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return data

    def restore_home(self, name, host="local", paths=()):
        """Put back, in the HOME of the stopped kept start NAME on HOST, the recorded text of its changed start files
        (`sandbox-ai resume NAME --restore-home --yes --json`, PATHS: only these): {"restored": [paths], "skipped":
        [{"path", "reason"}]}. ValueError: nothing put back (the text of sandbox-ai)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        if not all(isinstance(path, str) and path and not path.startswith("-") and "\n" not in path for path in paths):
            raise ValueError("invalid path")
        status, out, err = self.host_run(host, "resume", name, "--restore-home", "--yes", "--json",
                                         *[token for path in paths for token in ("--path", path)], timeout=120)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return data

    def rules_check(self, paths):
        """`sandbox-ai rules check FILE... --json` on this machine: {"ok", "rulesets": [{"name", "path", "ok",
        "error"?, "counts"?, "summary"?}]} (a broken file is in the answer, not an error)."""
        # Absolute: a file name is never read as an option; builtin:NAME is one of sandbox-ai's own rulesets.
        files = [str(path) if re.fullmatch(r"builtin:[a-z0-9-]{1,64}", str(path))
                 else os.path.abspath(os.path.expanduser(str(path))) for path in paths]
        status, out, err = self.call("rules", "check", "--json", *files, timeout=60)
        try:
            data = json.loads(out)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("rulesets"), list):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return data

    def rules_show(self, agent, argv, project=None):
        """The rulesets a start of AGENT with the launcher options ARGV (in PROJECT, else the app's folder) would
        have (`sandbox-ai rules show --json`): {"agent", "project_config", "rulesets": [{"name", "from", ...}]}."""
        if agent not in AGENTS:
            raise ValueError("invalid agent")
        status, out, err = self.call("rules", "show", "--agent", agent, "--json",
                                     *(["--project", str(project)] if project else []), "--",
                                     *self.as_given(argv), timeout=60)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("rulesets"), list):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return data

    def rules_reload(self, name, host="local"):
        """The running start NAME on HOST reads its HTTP rules anew (`sandbox-ai rules reload NAME --json`): {"rules":
        [{"name", "http", "kept"}], "reloaded", "not_reloaded"}. ValueError: refused (the text of sandbox-ai)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        status, out, err = self.host_run(host, "rules", "reload", name, "--json", timeout=90)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return data

    def host_json(self, name, *args, timeout=120):
        status, out, err = self.host_run(name, *args, timeout=timeout)
        try:
            return json.loads(out)
        except (ValueError, RecursionError):
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

    def host_update_from_here_supported(self):
        """Whether this sandbox-ai updates a host with its own copy (hosts update --from-here, 0.1.78)."""
        update = getattr(self.module("hosts"), "update", None)
        try:
            return update is not None and "from_here" in inspect.signature(update).parameters
        except (TypeError, ValueError):
            return False

    def host_update(self, name, sudo=None, from_here=False, installation=None):
        """The result of `sandbox-ai hosts update NAME --json` ({"before", "after", "changed", "error",
        "needs_sudo", "needs_password", "notes", "fixed", "installation"}); SUDO None: the host's own setting.
        FROM_HERE: with this machine's copy, as the same kind of installation (--from-here), instead of the host's
        signed update. INSTALLATION: "user" or "system" (--installation; None: the one the host's commands run).
        Its status is read again by hosts update itself; what was kept of its commands' options is dropped."""
        if from_here and name == "local":
            raise ValueError("this machine updates with: sandbox-ai update")
        if installation not in (None, "user", "system"):
            raise ValueError("the installation is user or system")
        args = ["hosts", "update", name, "--json", *(["--sudo"] if sudo else ["--no-sudo"] if sudo is False else []),
                *(["--from-here"] if from_here else []), *(["--installation", installation] if installation else [])]
        hosts = self.module("hosts")
        timeout = max(hosts.UPDATE_TIMEOUT, getattr(hosts, "INSTALL_TIMEOUT", 0) if from_here else 0) + 60
        try:
            status, out, err = self.call(*args, timeout=timeout)
        finally:
            self.forget_host_options(name)
        try:
            return json.loads(out)[0]
        except (ValueError, IndexError, KeyError, TypeError, RecursionError):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def host_uninstall_supported(self):
        """Whether this sandbox-ai removes sandbox-ai on a host (hosts uninstall, 0.1.237)."""
        return hasattr(self.module("hosts"), "uninstall")

    def host_uninstall_args(self, name, installation=None, purge=False, secrets=False, mcp_data=False,
                            stop_running=False, remove=False, dry_run=False):
        """The words after `sandbox-ai` of `hosts uninstall NAME ... --json`, every value checked."""
        if not (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)) or name == "local":
            raise ValueError("not a host of the list (this machine: sandbox-ai uninstall in a terminal here)")
        if installation not in (None, "user", "system"):
            raise ValueError("the installation is user or system")
        if (secrets or mcp_data) and not purge:
            raise ValueError("--secrets and --mcp-data go with --purge")
        return ["hosts", "uninstall", name, "--json", *(["--installation", installation] if installation else []),
                *(["--purge"] if purge else []), *(["--secrets"] if secrets else []),
                *(["--mcp-data"] if mcp_data else []), *(["--stop-running"] if stop_running else []),
                *(["--remove-from-list"] if remove else []), "--dry-run" if dry_run else "--yes"]

    def host_uninstall_plan(self, name, installation=None, purge=False, secrets=False, mcp_data=False):
        """What `sandbox-ai uninstall` would remove on host NAME (its own table, --dry-run there): the result of
        hosts uninstall --dry-run --json ({"plan", "installation", "sudo", "error"})."""
        status, out, err = self.call(*self.host_uninstall_args(name, installation, purge, secrets, mcp_data,
                                                               dry_run=True), timeout=240)
        try:
            return json.loads(out)
        except (ValueError, RecursionError):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def host_uninstall(self, name, installation=None, purge=False, secrets=False, mcp_data=False, stop_running=False,
                       remove=False):
        """`sandbox-ai hosts uninstall NAME --yes --json ...`: {"removed", "remaining", "sudo", "error",
        "needs_password", "removed_from_list", "output", "notes"}."""
        args = self.host_uninstall_args(name, installation, purge, secrets, mcp_data, stop_running, remove)
        try:
            status, out, err = self.call(*args, timeout=self.module("hosts").UPDATE_TIMEOUT + 60)
        finally:
            self.forget_host_options(name)
        try:
            return json.loads(out)
        except (ValueError, RecursionError):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}") from None

    def host_uninstall_terminal(self, name, installation=None, purge=False, secrets=False, mcp_data=False,
                                stop_running=False):
        """argv of host NAME's uninstall in a terminal tab, where sudo may ask for its password."""
        return list(self.module("hosts").uninstall_terminal(name, installation, purge, secrets, mcp_data,
                                                            stop_running))

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
        try:
            status, out, err = self.call(*args, "--json", timeout=timeout)
        finally:
            self.forget_host_options(name)  # (another sandbox-ai there now: its commands' options asked anew)
        try:
            result = json.loads(out)
        except (ValueError, RecursionError):
            result = None
        if not isinstance(result, dict):  # refused before it began (an option this sandbox-ai lacks, no such host)
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return result

    def host_install_terminal(self, name, mode=None, prefix=None, sudo=None, source="here", version=None):
        """argv of the same installation in a terminal tab, where sudo on the host may ask for its password."""
        return self.command(*self.host_install_args(name, mode, prefix, sudo, source, version))

    def host_update_terminal(self, name, installation=None):
        """argv of host NAME's update in a terminal tab, where sudo may ask for its password (INSTALLATION: as
        host_update's)."""
        if installation is None:
            return list(self.module("hosts").update_terminal(name))
        return list(self.module("hosts").update_terminal(name, installation_=installation))

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
            return "def dirs_main" in self.source_text("presets.py")
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
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            return {}, f"{path}: {exc}"
        if not isinstance(document, dict) or document.get("version") != 1:
            return {}, f"{path}: expected an object with \"version\": 1"
        return document, None

    def desktop_notification_levels(self):
        """What the launchers may show on the desktop ("desktop_notifications" in sandbox-ai's defaults): all, alarms,
        none - () for a sandbox-ai without that default (it shows them all)."""
        try:
            return tuple(getattr(self.launcher, "DESKTOP_NOTIFICATIONS", ()))
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.desktop_notification_levels failed (went on without it)", _exc)
            return ()

    def fix_questions_known(self):
        """This sandbox-ai has the fix plan (fixes.py: what a start lacks, offered) and its default fix_questions."""
        try:
            return "fix_questions" in getattr(self.launcher, "DEFAULTS_KEYS", ())
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("backend", "SandboxAI.fix_questions_known failed (went on without it)", _exc)
            return False

    def fix_questions(self):
        """How what a start lacks is asked about: "all" (one question for everything, the default) or "each" (one per
        fix) - sandbox-ai's default fix_questions, which the command line reads too."""
        try:
            value = self.sandbox_defaults()[0].get("fix_questions")
        except Exception as _exc:  # noqa: BLE001 - not read: the default
            applog.swallowed("backend", "SandboxAI.fix_questions: the defaults were not read (all)", _exc)
            value = None
        return value if value in ("all", "each") else "all"

    def host_fix_plan(self, host, agent, argv, timeout=150):
        """What the start `AGENT ARGV` lacks on HOST and what would fix each, as that host's sandbox-ai says it
        (`AGENT ARGV --fix-plan`: fixes.plan_json): {"fixes": [...], "questions", "mode"}, every text printable and
        every command one of sandbox-ai's own (checked_fix_plan). ValueError when the host does not answer one (an
        older sandbox-ai: its start says what is missing itself)."""
        status, out, err = self.host_run(host, agent, *self.as_given(argv), "--fix-plan", timeout=timeout)
        if status == 2 and host != "local" and re.search(r"unrecognized arguments|invalid choice", err or ""):
            # An older sandbox-ai there, which does not know --fix-plan or an option of the start: HostTooOld - the
            # prerequisites' question offers its update (sandbox-ai hosts update HOST, run on this machine).
            named = re.search(r"unrecognized arguments: ([^\n]+)", err or "")
            raise HostTooOld(host, (str(agent),), named[1].split()[:8] if named else ["--fix-plan"])
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return checked_fix_plan(out)

    def save_sandbox_default(self, key, value):
        """Set KEY of sandbox-ai's defaults file to VALUE (None: remove it, sandbox-ai's own default applies);
        the other keys stay. Only keys and values this sandbox-ai accepts: a file it refused would stop every
        start."""
        if key == "safeguard_mute":
            if not self.has_safeguards():
                raise ValueError("this sandbox-ai has no safeguard records yet; update it")
            if value is not None:
                value = self.module("safeguards").check_mute(value)
        elif key == "desktop_notifications":
            if value is not None and value not in self.desktop_notification_levels():
                raise ValueError(f"this sandbox-ai has no default {key} = {value!r}")
        elif key == "hide_secrets":
            # Settings > Privacy: Hide logins and keys in file views (session_inspect.py reads it)
            if "hide_secrets" not in getattr(self.launcher, "DEFAULTS_KEYS", ()):
                raise ValueError("this sandbox-ai has no default hide_secrets yet; update it")
            if value is not None and value is not True:
                raise ValueError("hide_secrets is true, or unset (listed and marked: the default)")
        elif key.startswith("storage_"):
            # Settings > Storage: the storage check and its rules (storage.py checks each value)
            if key not in getattr(self.launcher, "DEFAULTS_KEYS", ()):
                raise ValueError("this sandbox-ai has no storage check yet; update it")
            if value is not None:
                why = self.module("storage").check_value(key, value)
                if why:
                    raise ValueError(why)
        elif key == "fix_questions":
            # What a start lacks: one question for everything, or one per fix (fixes.QUESTIONS).
            if not self.fix_questions_known():
                raise ValueError("this sandbox-ai has no default fix_questions yet; update it")
            if value is not None and value not in ("all", "each"):
                raise ValueError("fix_questions is all or each")
        elif key == "ask_timeout":
            # How long a request waits for its answer (agent_sandbox.ASK_TIMEOUT_RANGE): only with this sandbox-ai.
            if "ask_timeout" not in getattr(self.launcher, "DEFAULTS_KEYS", ()):
                raise ValueError("this sandbox-ai has no default ask_timeout yet; update it")
            low, high = getattr(self.launcher, "ASK_TIMEOUT_RANGE", (5, 3600))
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high):
                raise ValueError(f"ask_timeout is a number of seconds from {low} to {high}")
        elif key == "loop_guard":
            # The loop guard's layers for every LLM server (llm.check_loop: known layers, their types and ranges).
            module = self.module("llm")
            if not hasattr(module, "check_loop"):
                raise ValueError("this sandbox-ai has no loop guard layers yet; update it")
            if value is not None:
                value = dict(module.check_loop(value, "loop_guard"))
        elif key != "telemetry" or value is not None and (value not in self.telemetry_levels()
                                                           or value not in TELEMETRY_ORDER):  # never on
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

        def changed(path):
            try:
                return path.stat().st_mtime
            except OSError:  # removed since it was listed
                return 0
        return sorted(found, key=changed, reverse=True)

    # -- profiles --

    def profiles_file(self):
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "app" / "profiles.json"

    def profiles(self):
        try:
            data = json.loads(self.profiles_file().read_text())
        except (OSError, ValueError, RecursionError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_profile(self, name, agent, argv, agent_args=(), host=None):
        """HOST: the host of `sandbox-ai hosts` the profile starts on (None: this machine)."""
        profiles = json_for_change(self.profiles_file())
        profiles[name] = {"agent": agent, "options": list(argv), "agent_args": list(agent_args),
                          **({"host": host} if host and host != "local" else {})}
        write_private_json(self.profiles_file(), profiles)

    def delete_profile(self, name):
        profiles = json_for_change(self.profiles_file())
        if profiles.pop(name, None) is not None:
            write_private_json(self.profiles_file(), profiles)
            return True
        return False

    def project_document(self, agent, options, values, agent_args=()):
        """(.sandbox-ai.json document, main workspace) for the form's values: the first workspace is where
        the file lives, further ones are listed; options a project file may not set are left out, and so are values
        it may not hold (--llm-traffic all: PROJECT_REFUSED_VALUES) or that are gone (--telemetry on)."""
        forbidden = set(self.launcher.CONFIG_FORBIDDEN)
        workspaces = [w for value in values.get("--workspace", []) for w in str(value).split(",") if w.strip()]
        kept = {name: value for name, value in values.items()
                if name not in forbidden and not project_refused(name, value) and not retired(name, value)}
        document = {"version": 1, "agent": agent, "options": argv_from_values(options, kept)}
        if workspaces[1:]:
            document["workspaces"] = workspaces[1:]
        if agent_args:
            document["agent_args"] = list(agent_args)
        return document, (workspaces[0] if workspaces else None)

    def create_folders(self, paths, warnings=None):
        """Make each folder of PATHS (with the folders above it) that a start names as its workspace or project
        folder, checked as a start checks a workspace (the launcher's create_workspace: never a file or a link to
        elsewhere in its path). A risk a start would say (a system folder, XDG_RUNTIME_DIR, HOME, sandbox-ai's own
        tree or folders) is yours to take: the folder is made, and the launcher's warning goes into WARNINGS (a
        list) for the app to show. Returns the folders made or there; ValueError with the reason (never a bare
        errno) for the first one refused."""
        create = getattr(self.launcher, "create_workspace", None)
        if create is None:
            raise ValueError("this sandbox-ai cannot create a workspace for the app: create the folder first")
        said = warnings if warnings is not None else []
        made = []
        for path in paths:
            try:
                made.append(str(create(path, warn=said.append)))
            except TypeError:  # a launcher before 0.1.98 (it refuses such a folder itself)
                made.append(str(create(path)))
        return made

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
        keep_trusted_copy(data)  # what the next change is shown against
        return path

    def trusted_baseline(self, directory):
        """(state, bytes) of the version of DIRECTORY/.sandbox-ai.json trusted last: ("kept", its bytes) from the
        app's copies (or the file itself while it is still exactly the trusted one), ("unknown", None) when another
        version is trusted but the app never saw it, ("none", None) when nothing is trusted for that path. A change
        is shown against this, never against the file in the folder, which whoever can write the folder wrote."""
        launcher = self.launcher
        path = launcher.config_key(Path(directory) / launcher.CONFIG_NAME)
        known = launcher.trusted_configs(note=lambda _line: None).get(str(path))
        if not known:
            return "none", None
        data = trusted_copy(known)
        if data is None:
            try:
                current = launcher.read_config_bytes(path)
            except (OSError, ValueError):
                current = None
            if current is not None and hashlib.sha256(current).hexdigest() == known:
                keep_trusted_copy(current)
                data = current
        return ("kept", data) if data is not None else ("unknown", None)

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
        if review.trusted:
            keep_trusted_copy(data)  # the baseline a later change of the file is shown against
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
        review.widening = [(name, printable(value or "")) for name, value in
                           weakening_options(launcher, document.get("options", []))]
        return review

    def trust_project(self, review):
        """Trust exactly the bytes shown in REVIEW (if the file changed since, the launcher refuses it)."""
        if review.problems:
            raise ValueError("this configuration cannot be used: " + "; ".join(review.problems))
        self.launcher.trust_config(review.path, digest=review.digest)
        keep_trusted_copy(review.data)

    # -- actions --

    def knows_option(self, option):
        """Whether this machine's launcher takes OPTION (hidden ones too)."""
        return any(option in action.option_strings for action in self.parser_actions())

    def as_given(self, argv):
        """ARGV (a start's options on this machine) with --workspaces-as-given when it names its workspace: the
        workspace chosen in the app is the main one, never a directory with a .sandbox-ai.json the app itself runs
        in (sandbox-ai 0.1.78 makes that one /work otherwise). Not for another host, whose older sandbox-ai may not
        know the option; not when this machine's launcher does not either."""
        argv = list(argv)
        names_workspace = any(word == "--workspace" or word.startswith("--workspace=") for word in argv)
        if names_workspace and AS_GIVEN not in argv and self.knows_option(AS_GIVEN):
            argv.append(AS_GIVEN)
        return argv

    def launch_argv(self, agent, argv, agent_args=(), session=None):
        """The sandbox-ai command line for a start on this machine."""
        extra = ["--session", session] if session else []
        return self.command(agent, *self.as_given(argv), *extra, *(["--", *agent_args] if agent_args else []))

    def dry_run_argv(self, agent, argv):
        return self.command(agent, *self.as_given(argv), "--dry-run")

    def dry_run(self, agent, argv):
        """(plan or None, error text) of a dry run with these options (blocking; the window uses
        dry_run_argv with AsyncCommand and parse_dry_run)."""
        return parse_dry_run(*self.call(agent, *self.as_given(argv), "--dry-run", timeout=60))

    def in_terminal(self, argv, title="sandbox-ai"):
        """argv that runs ARGV in a new terminal window, or None if no terminal emulator is known."""
        preferred = os.environ.get("SANDBOX_AI_TERMINAL")
        # The window stays open after the end, so that a message or an error can be read.
        script = f"{shlex.join(argv)}; status=$?; echo; echo \"[sandbox-ai: exit status $status, press Enter]\"; read _"
        if sys.platform == "darwin":
            return mac_terminal(script, iterm=(preferred or "").lower() in ("iterm", "iterm2", "iterm.app"))
        terminals = ([(preferred, ["-e"])] if preferred else []) + list(TERMINALS)
        for program, before in terminals:
            if program and shutil.which(program):
                if Path(program).name in ONE_ARGUMENT_TERMINALS:  # -e takes the whole command as one string
                    return [program, *before, shlex.join(["sh", "-c", script])]
                return [program, *before, "sh", "-c", script]
        return None

    # == command-line parity on every host (Manage pages, Activity, Statistics, Teams): the parity agent's section ==
    # Each method runs `sandbox-ai ...` on this machine ("local") or a host of `sandbox-ai hosts` (over SSH, through
    # host_run, host_json or host_command); values that go back as arguments are checked first and never read as an
    # option (a "--" before them where the command takes one). Secrets: names only; a value goes on standard input.

    def host_command(self, name, *args):
        """argv of `sandbox-ai ARGS` on host NAME without a terminal (for AsyncCommand, Task and QProcess): this
        machine's sandbox-ai for "local", else `sandbox-ai hosts run NAME -- ARGS` (standard input goes on to ssh)."""
        if name == "local":
            return self.command(*args)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("invalid host")
        return self.command("hosts", "run", name, "--", *args)

    def host_listing(self, name, *command, timeout=60):
        """[dict] of `sandbox-ai COMMAND --json` on host NAME. ValueError: not answered, or not a list."""
        data = self.host_json(name, *command, "--json", timeout=timeout)
        if not isinstance(data, list):
            raise ValueError("not a list")
        return [entry for entry in data if isinstance(entry, dict)]

    def host_vpn_profiles(self, name):
        if name == "local":
            return self.vpn_profiles()
        return [e for e in self.host_listing(name, "vpn", "list") if isinstance(e.get("profile"), str)]

    def host_git_logins(self, name):
        if name == "local":
            return self.git_logins()
        return [e for e in self.host_listing(name, "git-logins", "list") if isinstance(e.get("url"), str)]

    def host_publish_logins(self, name):
        if name == "local":
            return self.publish_logins()
        return [e for e in self.host_listing(name, "publish-logins", "list") if isinstance(e.get("kind"), str)]

    def host_identities(self, name):
        """{NAME: identity} of `sandbox-ai identities list --json` on host NAME."""
        if name == "local":
            return self.identities()
        data = self.host_json(name, "identities", "list", "--json", timeout=60)
        return {key: entry for key, entry in data.items() if isinstance(entry, dict)} if isinstance(data, dict) else {}

    def host_save_identity(self, host, name, old, new):
        """save_identity on HOST: the steps of identity_steps one after the other; (status, output)."""
        said = []
        for step in self.identity_steps(name, old, new):
            status, out, err = self.host_run(host, *step, timeout=90)
            said.append((out + err).strip())
            if status != 0:
                return status, "\n".join(said)
        return 0, "\n".join(part for part in said if part)

    # A line of `sandbox-ai login --status` there: every agent it names (Hermes and Qwen Code too) that this app knows.
    LOGIN_LINE = re.compile(rf"({'|'.join(AGENTS)})\s+(login store|host)\s+(.*?)\s+\((.*)\)\s*")

    def host_login_status(self, name):
        """[(agent, where, problem, source)] as login_status() says them, for host NAME from `sandbox-ai login
        --status` there (its lines read; where "store" or "host", problem None, "missing" or "expired")."""
        if name == "local":
            return self.login_status()
        status, out, err = self.host_run(name, "login", "--status", timeout=90)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        rows = []
        for line in out.splitlines():
            found = self.LOGIN_LINE.fullmatch(line.strip())
            if not found:
                continue
            agent, place, detail, source = found.groups()
            problem = "missing" if detail.startswith("no login") else "expired" if detail.startswith("expired") \
                else None
            rows.append((agent, "store" if place == "login store" else "host", problem, printable(source)[:300]))
        return rows

    def host_logins(self, host):
        """{agent: {"where": "store" or "host", "problem": None, "missing" or "expired"}} of the logins the sandboxes
        of HOST get (`sandbox-ai login --status --json` there; this machine for "local"); an older sandbox-ai's
        answer is read from its lines (host_login_status). ValueError: not answered."""
        status, out, err = self.host_run(host, "login", "--status", "--json", timeout=90)
        data = None
        if status == 0:
            with contextlib.suppress(ValueError, RecursionError):
                data = json.loads(out)
        agents = data.get("agents") if isinstance(data, dict) else None
        if isinstance(agents, dict):
            return {agent: {"where": "store" if entry.get("where") == "store" else "host",
                            "problem": entry.get("problem") if entry.get("problem") in ("missing", "expired") else None}
                    for agent, entry in agents.items() if agent in AGENTS and isinstance(entry, dict)}
        said = (err or out)
        if status != 0 and "--json" not in said and "usage:" not in said:
            raise ValueError(printable_text(said.strip()[-500:]) or f"exit status {status}")
        return {agent: {"where": where, "problem": problem if problem in ("missing", "expired") else None}
                for agent, where, problem, _ in self.host_login_status(host)}

    def session_seen(self, host, name):
        """What is there on HOST under the session name NAME (`sandbox-ai session status NAME --json` there): {"name",
        "agent", "state" (running, paused, stopped, orphaned, damaged), "foreground", "terminal" (ready, starting,
        missing, ended or None), "attachable", "attach_why", "login" (its HOME holds its agent's login), "seen" (its
        one line in words)}, or None when there is no start of that name. An older sandbox-ai (no `session status`)
        is asked for its list (`sandbox-ai list --json`: what runs). ValueError: not answered."""
        if not valid_run(name):
            raise ValueError("invalid start")
        status, out, err = self.host_run(host, "session", "status", name, "--json", timeout=60)
        said = (err or out)
        if status == 0:
            try:
                data = json.loads(out)
            except (ValueError, RecursionError):
                data = None
            if not isinstance(data, dict):
                raise ValueError("sandbox-ai session status: not an object")
            return seen_facts(data)
        if "No session named" in said:
            return None
        if "invalid choice" in said or "usage:" in said:
            for entry in self.host_json(host, "list", "--json", timeout=60):
                if isinstance(entry, dict) and name in (entry.get("name"), entry.get("id")):
                    return seen_facts({**entry, "foreground": entry.get("kind") == "run"})
            return None
        last = said.strip().splitlines()[-1] if said.strip() else f"exit status {status}"
        raise ValueError(printable_text(last)[:500])

    def local_session_seen(self, name):
        """session_seen for this machine without a process: what the session's own files and its supervisor's entry
        in /proc say (sandbox-ai's sessions module, in this process) - running or paused while its supervisor runs,
        else stopped (whether a pod of it still runs without its supervisor is Podman's to say: the launcher asks at
        the start). For a start in the window's thread: a few small files, never Podman, never SSH. None: no start of
        that name, or one whose record cannot be read (the launcher says what it reads there)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        sessions = self.module("sessions")
        try:
            key = sessions.resolve(name)
            data = sessions.load(key)
            running = bool(sessions.alive(key, data))
            fore = bool(sessions.foreground(data))
            state = "stopped" if not running else \
                "paused" if (sessions.session_dir(key) / "paused").exists() else "running"
            terminal = sessions.terminal_state(key, data) if state == "running" and not fore else (None, "")
            words = sessions.seen_text(key, data, state, terminal) if hasattr(sessions, "seen_text") else ""
            target = self.launcher.AUTH_TARGETS.get(data.get("agent"))
            login = bool(target) and (sessions.session_dir(key) / "home" / target).is_file()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return seen_facts({"name": sessions.display(key, data), "agent": data.get("agent"), "state": state,
                           "foreground": fore, "terminal": terminal[0],
                           "attachable": state == "running" and not fore and terminal[0] not in ("missing", "ended"),
                           "attach_why": None, "login": login, "seen": words})

    def host_logged_in(self, host, agents, session=None):
        """The agents of AGENTS whose sandboxes on HOST have a login now (host_logins: none missing or expired) - and
        the agent of the session SESSION there when its HOME holds its own (session_seen): a start of it needs no
        other. ValueError: not answered."""
        if host == "local":
            return self.logged_in_here(agents, session)
        logins = self.host_logins(host)
        found = {agent for agent in agents if agent in logins and logins[agent]["problem"] is None}
        if session and valid_run(session) and set(agents) - found:
            with contextlib.suppress(Exception):  # (not answered: the launcher has the last word)
                seen = self.session_seen(host, session)
                if seen and seen.get("login") and seen.get("agent") in agents:
                    found.add(seen["agent"])
        return found

    def logged_in_here(self, agents, session=None):
        """host_logged_in for this machine without a process: the launcher's own look at each agent's login file
        (login_state: the login store's, else this computer's; none missing or expired) and at the session SESSION's
        own HOME (local_session_seen). For a start in the window's thread: a few small files. An agent whose login
        cannot be told counts as there (the launcher says what is wrong at the start)."""
        launcher, found = self.launcher, set()
        for agent in agents:
            try:
                if launcher.login_state(agent)[1] is None:
                    found.add(agent)
            except Exception as _exc:  # noqa: BLE001 - not told: the launcher has the last word
                applog.swallowed("backend", "SandboxAI.logged_in_here: an agent's login could not be told", _exc)
                found.add(agent)
        if session and valid_run(session) and set(agents) - found:
            seen = self.local_session_seen(session)
            if seen and seen.get("login") and seen.get("agent") in agents:
                found.add(seen["agent"])
        return found

    def session_stop(self, host, name):
        """`sandbox-ai stop NAME` on HOST (asked first: start-stop); ValueError with its last words when it failed."""
        if not valid_run(name):
            raise ValueError("invalid start")
        status, out, err = self.host_run(host, "stop", name, timeout=150)
        if status != 0:
            text = (err or out).strip()
            raise ValueError(printable(text.splitlines()[-1] if text else f"exit status {status}")[:500])
        return True

    def attach_argv(self, host, name):
        """The command line that brings the terminal of the session NAME on HOST here: `sandbox-ai session attach
        NAME` on this machine, `ssh -tt ... sandbox-ai attach NAME` there."""
        if not valid_run(name):
            raise ValueError("invalid start")
        return self.command("session", "attach", name) if host == "local" else self.host_terminal(host, "attach", name)

    def resume_argv(self, host, name):
        """The same for `sandbox-ai resume NAME` (a paused session continues, its terminal comes here)."""
        if not valid_run(name):
            raise ValueError("invalid start")
        return self.command("resume", name) if host == "local" else self.host_terminal(host, "resume", name)

    def session_logins(self, host="local"):
        """Which login each running or kept session of HOST uses (`sandbox-ai login --status --sessions --json`):
        {"sessions": [{"name", "id", "agent", "state", "source" (store, host, file, session, none), "source_session",
        "login", "problem", "info": {"kind", "account": {"email", "organization", "plan", "account_id", "providers"},
        "expires", "refresh_expires", "last_refresh", "fingerprint"}, "shares_central"}], "central": {agent: info or
        None}} - what each login file says, never a token. ValueError: not read (its words)."""
        data = self.checkpoint_json(host, "login", "--status", "--sessions", "--json", timeout=120)
        if not isinstance(data.get("sessions"), list):
            raise ValueError("sandbox-ai login --status --sessions: not a list of sessions")
        data["sessions"] = [row for row in data["sessions"] if isinstance(row, dict)]
        data["central"] = data.get("central") if isinstance(data.get("central"), dict) else {}
        return data

    def import_session_login(self, host, agent, name, fingerprint, replace=False):
        """The login of the session NAME on HOST as the central login of AGENT (`sandbox-ai login AGENT --from-session
        NAME --yes --expect FINGERPRINT [--replace] --json`): only the login shown (its FINGERPRINT); REPLACE: also over
        a central login of another account. {"imported", "agent", "session", "login", "replaced", "notes"};
        ValueError: not imported (its words)."""
        if agent not in LOGIN_AGENTS_ANYWHERE:
            raise ValueError("unknown agent")
        if not valid_run(name):
            raise ValueError("invalid start")
        if not (isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{8}", fingerprint)):
            raise ValueError("invalid fingerprint")
        return self.checkpoint_json(host, "login", agent, "--from-session", name, "--yes", f"--expect={fingerprint}",
                                    *(["--replace"] if replace else []), "--json", timeout=180)

    def login_argv(self, host, agent, logout=False):
        """`sandbox-ai login AGENT [--logout]` on HOST, for a terminal (the agent's own login asks there)."""
        if agent not in AGENTS or agent not in LOGIN_AGENTS_ANYWHERE:
            raise ValueError("unknown agent")
        return self.host_terminal(host, "login", agent, *(["--logout"] if logout else []))

    # kits and plugins
    def host_kits_inventory(self, name):
        if name == "local":
            return self.kits_inventory()
        document = self.host_json(name, "kits", "list", "--json", timeout=90)
        if not isinstance(document, dict) or not isinstance(document.get("kits"), list):
            raise ValueError("sandbox-ai kits list --json: not an inventory")
        return document

    def host_kit_command(self, host, action, name, *arguments, options=()):
        """kit_command on HOST: `sandbox-ai kits ACTION [OPTIONS] -- NAME ARGUMENTS` there. What came with 0.1.78 (the
        new actions, the default kit's plugins and marketplaces) goes to another host only when its sandbox-ai has
        it; an older one is told to update (said, not sent)."""
        if host == "local":
            return self.kit_command(action, name, *arguments, options=options)
        if action not in KIT_ACTIONS or not re.fullmatch(KIT_NAME, str(name)):
            return 2, "", "invalid kit or action"
        if any(not isinstance(a, str) or not a or "\0" in a for a in arguments):
            return 2, "", "invalid argument"
        if action in KIT_NEW_ACTIONS or (name == "default" and action in KIT_DEFAULT_ACTIONS):
            try:
                self.require_marketplaces(host)
            except HostTooOld as exc:
                return 2, "", str(exc)
        return self.host_run(host, "kits", action, *options, "--", name, *arguments, timeout=300)

    # what the agents on a host have set up themselves (sandbox-ai kits host-list, host-show, host-import)

    def host_agent_parts(self, host):
        """`sandbox-ai kits host-list --json` on HOST: {"agents", "parts", ...}; ValueError with what it said (a host
        before 0.1.98: the update to do). Read only. Over SSH: never on the GUI thread."""
        result = self.host_run(host, "kits", "host-list", "--json", timeout=150)
        document = self.agent_parts_document(host, result, ("kits", "host-list"))
        if not isinstance(document.get("parts"), list) or not isinstance(document.get("agents"), list):
            raise ValueError("sandbox-ai kits host-list --json: not a list of the agents' parts")
        return document

    def host_agent_part(self, host, part):
        """`sandbox-ai kits host-show --json -- PART` on HOST: one part's files, scripts, secret-looking lines (masked),
        text and conversions. Read only."""
        if not isinstance(part, str) or not AGENT_PART.fullmatch(part):
            raise ValueError("not a part's id")
        result = self.host_run(host, "kits", "host-show", "--json", "--", part, timeout=150)
        return self.agent_parts_document(host, result, ("kits", "host-show"))

    def agent_parts_document(self, host, result, command):
        status, out, err = result
        if status != 0:
            raise ValueError(self.too_old_text(host, result, command) or (err or out).strip()[-1500:]
                             or f"exit status {status}")
        try:
            document = json.loads(out)
        except (ValueError, RecursionError):
            raise ValueError(f"sandbox-ai {' '.join(command)}: no JSON") from None
        if not isinstance(document, dict):
            raise ValueError(f"sandbox-ai {' '.join(command)}: not an object")
        return document

    def import_args(self, parts, agents=(), allow_secrets=False, allow_findings=False):
        if not parts or any(not isinstance(p, str) or not AGENT_PART.fullmatch(p) for p in parts):
            raise ValueError("choose the parts to import (their ids)")
        if any(agent not in AGENTS for agent in agents):
            raise ValueError("unknown agent")
        return ([f"--agent={agent}" for agent in dict.fromkeys(agents)]
                + (["--allow-secrets"] if allow_secrets else [])
                + (["--allow-findings"] if allow_findings else []), ["--", *dict.fromkeys(parts)])

    def host_import_parts(self, host, kit, parts, new=False, agents=(), allow_secrets=False, allow_findings=False):
        """`sandbox-ai kits host-import --kit KIT [--new] ... -- PARTS` on HOST: the parts chosen into a kit of yours
        there (the page asked first: kit-add). (status, out, err)."""
        if not re.fullmatch(KIT_NAME, str(kit)) or kit in ("default", "built-in"):
            return 2, "", "invalid kit"
        try:
            options, tail = self.import_args(parts, agents, allow_secrets, allow_findings)
        except ValueError as exc:
            return 2, "", str(exc)
        result = self.host_run(host, "kits", "host-import", f"--kit={kit}", *(["--new"] if new else []), *options,
                               *tail, timeout=300)
        return self.too_old_result(host, result, ("kits", "host-import"))

    def host_import_memory(self, host, project, parts, agents=(), allow_secrets=False, allow_findings=False):
        """`sandbox-ai kits host-import --project DIR ... -- PARTS` on HOST: memory parts into the memory sandbox-ai
        keeps for the project DIR there (the page asked first: memory-add). (status, out, err)."""
        if not isinstance(project, str) or not project.startswith("/") or "\0" in project:
            return 2, "", "the project is an absolute path"
        try:
            options, tail = self.import_args(parts, agents, allow_secrets, allow_findings)
        except ValueError as exc:
            return 2, "", str(exc)
        result = self.host_run(host, "kits", "host-import", f"--project={project}", *options, *tail, timeout=300)
        return self.too_old_result(host, result, ("kits", "host-import"))

    def too_old_result(self, host, result, command):
        text = self.too_old_text(host, result, command)
        return (result[0], result[1], text) if text else result

    def require_marketplaces(self, host):
        """HostTooOld when the sandbox-ai on HOST predates the marketplace browser (0.1.78: `plugins marketplaces`,
        `plugins --marketplaces-only`, kits enable-plugin, disable-plugin, remove-marketplace, import-marketplace and
        the default kit's plugins): its `plugins --help` does not list --marketplaces-only. Over SSH: never on the
        GUI thread."""
        self.require_options(host, ("plugins",), ["--marketplaces-only"])

    def host_marketplaces(self, host):
        """marketplaces() on HOST (`sandbox-ai plugins marketplaces --json` there). HostTooOld: that host's sandbox-ai
        has no marketplace browser yet (update it); ValueError: no answer, or not that document."""
        if host == "local":
            return self.marketplaces()
        self.require_marketplaces(host)
        return marketplaces_document(self.host_json(host, "plugins", "marketplaces", "--json", timeout=90))

    def host_trust_hooks_argv(self, host, name):
        """`sandbox-ai kits trust-hooks -- NAME` on HOST, for a terminal: the review and the yes are there."""
        if host == "local":
            return self.trust_hooks_argv(name)
        if not re.fullmatch(KIT_NAME, str(name)):
            raise ValueError(f"{name!r} is not a kit name")
        return self.host_terminal(host, "kits", "trust-hooks", "--", name)

    def revoke_hooks(self, host, name):
        """`sandbox-ai kits trust-hooks --revoke -- NAME` on HOST: the approval is taken back (nothing is widened)."""
        if not re.fullmatch(KIT_NAME, str(name)):
            return 2, "", "invalid kit"
        return self.host_run(host, "kits", "trust-hooks", "--revoke", "--", name, timeout=120)

    def host_plugins_argv(self, host, update=False, kits=(), marketplaces_only=False):
        """`sandbox-ai plugins claude [--update] [--marketplaces-only] [--kit NAME]...` on HOST, for a terminal (the
        page offers --marketplaces-only only where the host's marketplaces could be read)."""
        if host == "local":
            return self.plugins_argv(update=update, kits=kits, marketplaces_only=marketplaces_only)
        return self.host_terminal(host, "plugins", "claude", *(["--update"] if update else []),
                                  *(["--marketplaces-only"] if marketplaces_only else []),
                                  *[token for kit in kits if re.fullmatch(KIT_NAME, kit) for token in ("--kit", kit)])

    def host_plugins(self, host, update=False, kits=(), marketplaces_only=False, timeout=PLUGINS_WAIT, agent="claude"):
        """(status, out, err) of `sandbox-ai plugins claude [--update] [--marketplaces-only] [--kit NAME]...` on
        HOST, without a terminal: it asks nothing (each step runs in the plugin store's own sandbox); its output comes
        as it is written inside watching() (the Kits page's Task)."""
        if host == "local":
            return self.call(*self.plugins_args(update=update, kits=kits, marketplaces_only=marketplaces_only,
                                                agent=agent), timeout=timeout)
        return self.host_run(host, "plugins", "claude", *(["--update"] if update else []),
                             *(["--marketplaces-only"] if marketplaces_only else []),
                             *[token for kit in kits if re.fullmatch(KIT_NAME, kit) for token in ("--kit", kit)],
                             timeout=timeout)

    def plugins_status(self, host):
        """(status, out, err) of `sandbox-ai plugins --status` on HOST: what is declared and what is installed."""
        return self.host_run(host, "plugins", "--status", timeout=120)

    # canaries
    def host_canary_log(self, host, run=None):
        if host == "local":
            return self.canary_log(run)
        if run is not None and not valid_run(run):
            raise ValueError("invalid run")
        document = self.host_json(host, "canaries", "log", "--json", *(["--", run] if run else []), timeout=90)
        document = [document] if isinstance(document, dict) else document
        if not isinstance(document, list):
            raise ValueError("sandbox-ai canaries log --json: not a log")
        return document

    def canary_runs(self, host):
        """[{run, started, status, canaries, events, alerts}] of `sandbox-ai canaries list --json` on HOST."""
        return [e for e in self.host_listing(host, "canaries", "list") if isinstance(e.get("run"), str)]

    def canary_run(self, host, run, tokens=False):
        """`sandbox-ai canaries list [--tokens] --json -- RUN` on HOST: the run's canaries and events; the tokens
        masked unless TOKENS (asked first: canary-tokens)."""
        if not valid_run(run):
            raise ValueError("invalid run")
        data = self.host_json(host, "canaries", "list", *(["--tokens"] if tokens else []), "--json", "--", run,
                              timeout=60)
        if not isinstance(data, dict):
            raise ValueError("not a run's canaries")
        return data

    def canary_resume(self, host, run):
        """`sandbox-ai canaries resume -- RUN` on HOST: the sandbox a canary paused goes on (asked first)."""
        if not valid_run(run):
            return 2, "", "invalid run"
        return self.host_run(host, "canaries", "resume", "--", run, timeout=120)

    # presets, preset directories and trusted project configurations
    def host_presets(self, host):
        """[document] of `sandbox-ai presets list --json` on HOST: its presets (built-in, from its preset directories
        and its user's), each with "origin" (and "directory" for one of an extra directory). ValueError: not
        answered."""
        data = self.host_json(host, "presets", "list", "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("sandbox-ai presets list --json: not a list of presets")
        return [entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("id"), str)
                and re.fullmatch(KIT_NAME, entry["id"])]

    def host_preset(self, host, name):
        """The document of `sandbox-ai presets show --json NAME` on HOST (what it sets, needs and asks for, as it is
        there now). ValueError: no such preset, or not answered."""
        if not (isinstance(name, str) and re.fullmatch(KIT_NAME, name)):
            raise ValueError("not a preset name")
        data = self.host_json(host, "presets", "show", "--json", name, timeout=60)
        if not isinstance(data, dict) or data.get("id") != name:
            raise ValueError(f"sandbox-ai presets show {name}: not that preset")
        return data

    def host_image_variants(self, host):
        """[name] of the image variants on HOST (`sandbox-ai images --json` there: the entries that are variants),
        what --image-variant takes. ValueError: not answered."""
        data = self.host_json(host, "images", "--json", timeout=90)
        if not isinstance(data, list):
            raise ValueError("sandbox-ai images --json: not a list of images")
        # (a name several agents have a variant of is listed as NAME@AGENT: --image-variant takes NAME, the agent's)
        return sorted({str(e["target"]).partition("@")[0] for e in data if isinstance(e, dict) and e.get("variant")
                       and isinstance(e.get("target"), str)
                       and re.fullmatch(KIT_NAME, str(e["target"]).partition("@")[0])})

    def host_agent_images(self, host):
        """[Podman image name] of the agents' images and their variants on HOST (`sandbox-ai images --json` there:
        localhost/claude-sandbox:latest ...), what --image takes. ValueError: not answered."""
        data = self.host_json(host, "images", "--json", timeout=90)
        if not isinstance(data, list):
            raise ValueError("sandbox-ai images --json: not a list of images")
        return sorted({str(e["image"]) for e in data if isinstance(e, dict) and isinstance(e.get("image"), str)
                       and e["image"].strip()})

    START_CHOICES = ("--vpn", "--kit", "--identity", "--mcp", "--image-variant", "--image", "--service",
                     "--secret-env", "--api-key", "--git-token-secret")

    def host_start_choices(self, host):
        """({option: [name]}, {option: problem}) of what HOST has for the Start page's choosers: --vpn its VPN profiles
        (vpn list --json), --kit its kits that are named (kits list --json), --identity its identities (identities
        list --json), --mcp its MCP servers (mcp list --json), --image-variant its image variants and --image the
        agents' images (images --json, read once), --service your images (images list; this machine: their record),
        --secret-env, --api-key and --git-token-secret the names of its secrets (secrets list, read once; never a
        value). A list that could not be read is a problem (its field stays typed in)."""
        secrets_read, images_read = [], []

        def agent_images():
            if not images_read:
                try:
                    images_read.append((self.host_json(host, "images", "--json", timeout=90), None))
                except Exception as exc:  # noqa: BLE001 - said for both options
                    images_read.append((None, exc))
            data, problem = images_read[0]
            if problem is not None:
                raise problem
            if not isinstance(data, list):
                raise ValueError("sandbox-ai images --json: not a list of images")
            return [e for e in data if isinstance(e, dict)]

        def secret_names():
            if not secrets_read:
                try:
                    secrets_read.append(([name for name, _ in self.host_secrets(host)[1]], None))
                except Exception as exc:  # noqa: BLE001 - said for each of the three options
                    secrets_read.append((None, exc))
            names, problem = secrets_read[0]
            if problem is not None:
                raise problem
            return names
        readers = {
            "--vpn": lambda: [e["profile"] for e in self.host_vpn_profiles(host)],
            "--kit": lambda: [k["name"] for k in self.host_kits_inventory(host).get("kits", [])
                              if isinstance(k, dict) and isinstance(k.get("name"), str)
                              and k.get("kind") not in ("always", "default")],
            "--identity": lambda: list(self.host_identities(host)),
            "--mcp": lambda: [row[0] for row in self.host_mcp_servers(host)],
            # (a name several agents have a variant of is listed as NAME@AGENT: --image-variant takes NAME)
            "--image-variant": lambda: [str(e["target"]).partition("@")[0] for e in agent_images()
                                        if e.get("variant") and isinstance(e.get("target"), str)
                                        and re.fullmatch(KIT_NAME, str(e["target"]).partition("@")[0])],
            "--image": lambda: [str(e["image"]) for e in agent_images() if isinstance(e.get("image"), str)],
            "--service": lambda: [str(row["name"]) for row in self.user_images(host)
                                  if isinstance(row, dict) and isinstance(row.get("name"), str)],
            "--secret-env": secret_names, "--api-key": secret_names, "--git-token-secret": secret_names,
        }
        found, problems = {}, {}
        for option, read in readers.items():
            try:
                found[option] = sorted({printable(str(name))[:64] for name in read() if str(name).strip()})
            except Exception as exc:  # noqa: BLE001 - one list not answered: the others are still offered
                problems[option] = printable_text(str(exc))[-300:]
        return found, problems

    def host_preset_dirs(self, host):
        if host == "local":
            return self.preset_dirs()
        document = self.host_json(host, "presets", "dirs", "list", "--json", timeout=60)
        if not isinstance(document, dict) or not isinstance(document.get("dirs"), list):
            raise ValueError("sandbox-ai presets dirs list --json: not a list of directories")
        return document

    def host_preset_dir_command(self, host, action, path):
        """preset_dir_command on HOST (PATH: a directory on that host)."""
        if host == "local":
            return self.preset_dir_command(action, path)
        if action not in ("add", "remove") or not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            return 2, "", "invalid action or path"
        return self.host_run(host, "presets", "dirs", action, "--", path, timeout=60)

    # phone notifications (sandbox-ai notify on this machine or a host): channels with secret names, never a token
    def has_notify(self):
        return (self.directory / "notify.py").is_file()

    def notify_channels(self, host="local"):
        """{"channels": [{name, kind, where, events, when, detail, quiet, rate, secrets, status}], "link", "events",
        "kinds", "default_events"} of `sandbox-ai notify list --json` on HOST. ValueError: not answered."""
        self.require_options(host, ("notify", "list"), ["--json"])
        data = self.host_json(host, "notify", "list", "--json", timeout=60)
        if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
            raise ValueError("not a list of phone channels")
        data["channels"] = [c for c in data["channels"] if isinstance(c, dict) and isinstance(c.get("name"), str)]
        return data

    def notify_add(self, host, fields):
        """`sandbox-ai notify add` on HOST for the channel of FIELDS (notify.json's names), checked first as the
        command checks it (this machine's notify.py: a ValueError before anything runs); (status, out, err).
        Asked first by the caller (notify-add)."""
        args = self.module("notify").add_arguments(fields)
        return self.host_run(host, *args, timeout=60)

    def notify_remove(self, host, name):
        if not (isinstance(name, str) and self.module("notify").NAME.fullmatch(name)):
            return 2, "", "invalid channel name"
        return self.host_run(host, "notify", "remove", name, timeout=60)

    def notify_test(self, host, name):
        """`sandbox-ai notify test NAME` on HOST: a message sent now; (status, out, err) - status 1: not sent (why)."""
        if not (isinstance(name, str) and self.module("notify").NAME.fullmatch(name)):
            return 2, "", "invalid channel name"
        return self.host_run(host, "notify", "test", name, timeout=60)

    def notify_link(self, host, url):
        """`sandbox-ai notify link URL` (the web interface's public address in the messages; None: --none)."""
        if url in (None, ""):
            return self.host_run(host, "notify", "link", "--none", timeout=60)
        try:
            url = self.module("notify").check_link(url)
        except ValueError as exc:
            return 2, "", str(exc)
        return self.host_run(host, "notify", "link", url, timeout=60)

    # scheduled runs (sandbox-ai schedule on this machine or a host): an agent started on a timetable or a trigger
    # the update mode (sandbox-ai update mode): Settings › Updates; this machine's, or a host's
    def has_update_mode(self):
        return (self.directory / "updates.py").is_file()

    def update_status(self, host="local"):
        """`sandbox-ai update mode --json` on HOST: {"mode", "window", "last_check", "result", "next_check",
        "downloaded", "waiting", "rollback", "refused", "manual", ...}. ValueError: not answered."""
        data = self.host_json(host, "update", "mode", "--json", timeout=60)
        if not isinstance(data, dict) or data.get("mode") not in UPDATE_MODES:
            raise ValueError("not an update mode")
        return data

    def set_update_mode(self, mode, window=None, host="local"):
        """`sandbox-ai update mode MODE [--at WINDOW] --json` on HOST (asked first by the caller): what it says now."""
        if mode not in UPDATE_MODES:
            raise ValueError(f"the mode is one of {', '.join(UPDATE_MODES)}")
        status, out, err = self.host_run(host, "update", "mode", mode, *(["--at", window] if window else []),
                                         "--json", timeout=180)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return data

    def update_check_now(self):
        """`sandbox-ai update --check` here: (status, out, err); what it found is kept as the last check."""
        return self.call("update", "--check", timeout=200)

    def update_apply(self, while_running=False):
        """`sandbox-ai update --apply` here: (status, out, err); status 3 (update_busy_reasons): something runs."""
        return self.call("update", "--apply", *(["--while-running"] if while_running else []), timeout=1800)

    def has_update_install(self):
        """Whether this sandbox-ai has `update --install` (it came with package_update.py: the copies that replace
        themselves)."""
        return (self.directory / "package_update.py").is_file()

    def update_install(self, while_running=False):
        """`sandbox-ai update --install` here - the newest signed release downloaded, checked and installed, for every
        kind of copy that replaces itself: (status, out, err); status 3 (update_busy_reasons): something runs."""
        return self.call("update", "--install", *(["--while-running"] if while_running else []), timeout=1900)

    def update_rollback(self, while_running=False):
        """`sandbox-ai update --rollback` here: (status, out, err); status 3: something runs."""
        return self.call("update", "--rollback", *(["--while-running"] if while_running else []), timeout=300)

    def has_schedule(self):
        return (self.directory / "schedule.py").is_file()

    def schedules(self, host="local"):
        """{"schedules": [...], "problems", "timer"} of `sandbox-ai schedule list --json` on HOST. ValueError: not
        answered."""
        data = self.host_json(host, "schedule", "list", "--json", timeout=60)
        if not isinstance(data, dict) or not isinstance(data.get("schedules"), list):
            raise ValueError("not a list of schedules")
        data["schedules"] = [x for x in data["schedules"] if isinstance(x, dict) and isinstance(x.get("name"), str)]
        return data

    def schedule_name(self, name):
        if not (isinstance(name, str) and self.module("schedule").NAME.fullmatch(name)):
            raise ValueError("invalid schedule name")
        return name

    def schedule_history(self, host, name):
        """[run] of `sandbox-ai schedule history NAME --json` on HOST, the newest last."""
        data = self.host_json(host, "schedule", "history", self.schedule_name(name), "--json", timeout=60)
        runs = data.get("runs") if isinstance(data, dict) else None
        return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []

    def schedule_report(self, host, name, run=None):
        """The Markdown of a run of NAME (its summary and the start's report) on HOST: `sandbox-ai schedule report`."""
        name = self.schedule_name(name)
        if run is not None and not self.module("schedule").RUN_ID.fullmatch(str(run)):
            raise ValueError("invalid run")
        status, out, err = self.host_run(host, "schedule", "report", name, *([run] if run else []), timeout=60)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return out

    def schedule_add(self, host, fields):
        """`sandbox-ai schedule add` on HOST for FIELDS (schedule.add_arguments' names), checked first as the command
        checks them (a ValueError before anything runs); (status, out, err). Asked first by the caller."""
        args = self.module("schedule").add_arguments(fields)
        return self.host_run(host, *args, timeout=120)

    def schedule_action(self, host, name, action, keep_history=False):
        """run-now, pause, resume or remove of NAME on HOST: (status, out, err)."""
        if action not in ("run-now", "pause", "resume", "remove"):
            return 2, "", "invalid action"
        try:
            name = self.schedule_name(name)
        except ValueError as exc:
            return 2, "", str(exc)
        return self.host_run(host, "schedule", action, name,
                             *(["--keep-history"] if action == "remove" and keep_history else []), timeout=120)

    def schedule_timer(self, host, on):
        """`sandbox-ai schedule enable` (ON) or `disable` on HOST: the user-level timer there."""
        return self.host_run(host, "schedule", "enable" if on else "disable", timeout=120)

    def trusted_projects(self, host="local"):
        """[{path, digest, state}] of `sandbox-ai wizard trust --list --json` on HOST (state trusted, changed,
        missing or unreadable)."""
        self.require_options(host, ("wizard", "trust"), ["--list", "--json"])
        data = self.host_json(host, "wizard", "trust", "--list", "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a list of trusted configurations")
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("path"), str)]

    def forget_trusted(self, host, path):
        """`sandbox-ai wizard forget -- PATH` on HOST: the configuration is no longer trusted (asked first)."""
        if not valid_directory(path):
            return 2, "", "invalid path"
        return self.host_run(host, "wizard", "forget", "--", path, timeout=60)

    # the web interface of a host (sandbox-ai web there): its logins, certificate, xterm.js and the service
    def host_web_users(self, host):
        """[(name, since)] of `sandbox-ai web user list` on HOST (names as webui.USER_NAME takes them; any other line
        is left out). ValueError: no answer."""
        status, out, err = self.host_run(host, "web", "user", "list", timeout=60)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        found = []
        for line in out.splitlines():
            name, _, since = line.strip().partition("  since ")
            since, _, flag = since.partition("  (")  # "  (no TOTP)": a login with the password only
            if WEB_USER.fullmatch(name):
                found.append((name, printable(since.strip())[:40] + (" (no TOTP)" if flag.startswith("no TOTP") else "")))
        return found

    def host_web_user_remove(self, host, name):
        """`sandbox-ai web user remove NAME` on HOST (asked first: web-user-remove): its sessions end at their next
        request."""
        if not isinstance(name, str) or not WEB_USER.fullmatch(name):
            return 2, "", "invalid user name"
        return self.host_run(host, "web", "user", "remove", name, timeout=60)

    def host_web_cert(self, host, listen=None, renew=False):
        """`sandbox-ai web cert [--listen ADDRESS:PORT] [--renew]` on HOST: its certificate's path and SHA-256
        fingerprint (made when there is none; RENEW, asked first: web-cert-renew, makes a new one)."""
        if listen is not None and web_listen_problem(listen):
            return 2, "", web_listen_problem(listen)
        return self.host_run(host, "web", "cert", *([f"--listen={listen}"] if listen else []),
                             *(["--renew"] if renew else []), timeout=120)

    def host_web_argv(self, host, action, name=None, listen=None):
        """argv of `sandbox-ai web ...` on HOST for a terminal (ssh -tt for another host): "add" and "reset" a login
        NAME (the password and the TOTP code are typed there, never through the app), "vendor" (xterm.js, pinned and
        checked) and "serve" (the service, on LISTEN; it runs as long as the terminal does)."""
        if action in ("add", "reset"):
            if not isinstance(name, str) or not WEB_USER.fullmatch(name):
                raise ValueError("a user name is 1-32 lowercase letters, digits, '_', '.' or '-', starting with a "
                                 "letter")
            return self.host_terminal(host, "web", "user", action, name)
        if action == "vendor":
            return self.host_terminal(host, "web", "vendor")
        if action == "serve":
            if listen is not None and web_listen_problem(listen):
                raise ValueError(web_listen_problem(listen))
            return self.host_terminal(host, "web", "serve", *([f"--listen={listen}"] if listen else []))
        raise ValueError("not an action of sandbox-ai web")

    def host_web_tunnel_argv(self, host, listen=None, local_port=None):
        """(argv, local port) of an SSH tunnel to the web interface that listens on LISTEN (default WEB_LISTEN) of
        HOST's loopback: `ssh -N -L 127.0.0.1:LOCAL:ADDRESS:PORT` with the host's own SSH settings (its key, port and
        known hosts), for a terminal tab; https://localhost:LOCAL/ then reaches it from this machine, for as long as
        that tab runs (review 6 N4: the service's own URL named the host's localhost, unreachable from here)."""
        if host == "local":
            raise ValueError("this machine's web interface needs no tunnel")
        listen = listen or WEB_LISTEN
        address, port = web_listen_address(listen), web_listen_port(listen)
        hosts = self.module("hosts")
        entry = hosts.load().get(host)
        if entry is None:
            raise ValueError(f"no host {printable(host)}")
        base = list(hosts.ssh_base(entry, tty=False))
        target = str(address) if not address.is_unspecified else "127.0.0.1"
        target = f"[{target}]" if ":" in target else target
        local_port = local_port or free_local_port(port)
        cut = base.index("--")
        return [*base[:cut], "-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{local_port}:{target}:{port}",
                *base[cut:]], local_port

    def host_project_show(self, host, path):
        """`sandbox-ai wizard trust --show --json -- PATH` on HOST: what trusting the project file PATH there would
        trust, nothing trusted - {"path", "digest", "document", "trusted", "trusted_digest", "problem"} as that host's
        sandbox-ai read it once ("error" set and digest None when it could not). HostTooOld where --show is not known
        yet; ValueError for an answer that is not such an object."""
        if not valid_directory(path):
            raise ValueError("invalid path")
        self.require_options(host, ("wizard", "trust"), ["--show", "--json"])
        data = self.host_json(host, "wizard", "trust", "--show", "--json", "--", path, timeout=60)
        sha = re.compile(r"[0-9a-f]{64}")
        if not (isinstance(data, dict) and valid_directory(data.get("path"))
                and (data.get("digest") is None or isinstance(data["digest"], str) and sha.fullmatch(data["digest"]))
                and (data.get("trusted_digest") is None or isinstance(data["trusted_digest"], str)
                     and sha.fullmatch(data["trusted_digest"]))
                and (data.get("document") is None or isinstance(data["document"], dict))):
            raise ValueError("not the answer of sandbox-ai wizard trust --show --json")
        if data.get("digest") is None and not data.get("error"):
            data["error"] = data.get("problem") or "not read"
        return data

    def host_trust_defaults(self, host):
        """The defaults a project file on HOST is measured against ({"mode", "canaries"} as defaults.json has them
        there; {} when that host's `defaults show --json` does not answer): a laxer mode or fewer canaries than its own
        defaults weaken its starts."""
        try:
            data = self.host_json(host, "defaults", "show", "--json", timeout=60)
        except Exception as _exc:  # noqa: BLE001 - not answered: measured against sandbox-ai's own defaults
            applog.swallowed("backend", "SandboxAI.host_trust_defaults: not answered: measured against sandbox-…", _exc)
            return {}
        values = data.get("values") if isinstance(data, dict) and isinstance(data.get("values"), dict) else {}
        return {key: values[key] for key in ("mode", "canaries") if key in values}

    def shown_baseline(self, shown):
        """(state, text) of the version trusted last of the project file SHOWN describes (host_project_show): ("kept",
        its text) from the app's copies (or the file itself while it is exactly the trusted one: kept now), ("unknown",
        None) when another version is trusted there that the app never saw, ("none", None) when nothing is trusted
        for that path. Never the file in the folder, which whoever can write that folder wrote."""
        known = shown.get("trusted_digest")
        if not known:
            return "none", None
        text = shown_copy(known)
        if text is None and known == shown.get("digest") and isinstance(shown.get("document"), dict):
            keep_shown_copy(known, shown["document"])
            text = shown_text(shown["document"])
        return ("kept", text) if text is not None else ("unknown", None)

    def host_project_trust(self, host, path, digest, document=None):
        """`sandbox-ai wizard trust --digest SHA256 -- PATH` on HOST: trusted there only if the file is still exactly
        the version reviewed (its sha256 DIGEST; sandbox-ai refuses it otherwise). DOCUMENT, as shown, is kept as the
        version a later change is shown against. (status, out, err)."""
        if not valid_directory(path) or not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            return 2, "", "invalid path or digest"
        try:
            self.require_options(host, ("wizard", "trust"), ["--digest"])
        except HostTooOld as exc:
            return 2, "", str(exc)
        result = self.host_run(host, "wizard", "trust", f"--digest={digest}", "--", path, timeout=60)
        if result[0] == 0 and document is not None:
            keep_shown_copy(digest, document)
        return result

    # images
    def image_build_argv(self, host, target, extension_only=False, pin=None, uv=None, node=None, variant=False):
        """`sandbox-ai build [--extension-only] [--pin V] [--uv V] [--node IMAGE] [--variant] TARGET` on HOST, for a
        terminal. VARIANT: TARGET is a variant (NAME, or NAME@AGENT where several agents have one of that name) - built
        as one even where its name is a build target too (tor: `build tor` is the Tor VPN's image)."""
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(@[a-z]{1,20})?", target):
            raise ValueError("invalid image")
        options = ["--extension-only"] if extension_only else []
        for option, value, pattern in (("--pin", pin, r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}"),
                                       ("--uv", uv, r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}"),
                                       ("--node", node, r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}")):
            if value:
                if not re.fullmatch(pattern, value):
                    raise ValueError(f"{option}: {value!r}")
                options += [option, value]
        return self.host_terminal(host, "build", *options, *(["--variant"] if variant else []), target)

    # activity and statistics
    def host_activity(self, host, source=(), tail=5000, since=None, kinds=(), grep=None, timeout=120):
        """[record] of `sandbox-ai activity SOURCE --json --tail N [--since T] [--kind K,...] [--grep TEXT]` on HOST
        (the window polls it again to follow a host: ssh hands no stream). ValueError: not answered."""
        args = ["activity", *source, "--json", "--tail", str(int(tail))]
        if since:
            args.append(f"--since={since}")
        if kinds:
            args.append("--kind=" + ",".join(kinds))
        if grep:
            args.append(f"--grep={grep}")
        status, out, err = self.host_run(host, *args, timeout=timeout)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        records = []
        for line in out.splitlines()[-(int(tail) + 10):]:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict) and isinstance(record.get("kind"), str):
                records.append(record)
        return records

    def host_statistics(self, host, directory=None, project=None):
        """What the Statistics page shows for a log folder on HOST: `sandbox-ai logs DIR --json` (or --project PATH)
        there, its "telemetry" with its "tool_calls"."""
        args = ["logs"]
        if project:
            if not valid_directory(project):
                raise ValueError("invalid project")
            args += ["--project", project]
        elif directory:
            if not valid_directory(directory):
                raise ValueError("invalid directory")
            args.append(directory)
        data = self.host_json(host, *args, "--json", timeout=300)
        if not isinstance(data, dict) or not isinstance(data.get("telemetry"), dict):
            raise ValueError("sandbox-ai logs --json: not a summary")
        found = dict(data["telemetry"])
        found["tool_calls"] = data.get("tool_calls") if isinstance(data.get("tool_calls"), dict) else {}
        return found

    # teams (sandbox-ai team)
    TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")
    TEAM_AGENT = "(?:" + "|".join(AGENTS) + ")"
    TEAM_LINE = re.compile(rf"([a-z0-9][a-z0-9_-]{{0,19}})\s+((?:{TEAM_AGENT}, )*{TEAM_AGENT})\s+(.*)")
    VPN_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")  # vpn.PROFILE_NAME
    # a team whose record is damaged: NAME  (damaged)  remove it with: sandbox-ai team delete NAME --yes
    TEAM_DAMAGED_LINE = re.compile(r"([a-z0-9][a-z0-9_-]{0,19})\s+\(damaged\)(?:\s.*)?")

    def teams(self, host="local"):
        """[{name, members, workspace}] of `sandbox-ai team list` on HOST (its lines read); a team whose record is
        damaged as {name, members: [], workspace: "", damaged: True}: shown, so that it can be deleted."""
        status, out, err = self.host_run(host, "team", "list", timeout=60)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        found = []
        for line in out.splitlines():
            match = self.TEAM_LINE.fullmatch(line.strip())
            if match:
                found.append({"name": match[1], "members": match[2].split(", "), "workspace": printable(match[3])})
                continue
            match = self.TEAM_DAMAGED_LINE.fullmatch(line.strip())
            if match:
                found.append({"name": match[1], "members": [], "workspace": "", "damaged": True})
        return found

    def team_show(self, host, name):
        """{name, members, lead, workspace, options, states: {agent: state}, board: {tasks}} of `sandbox-ai team
        show --json -- NAME` on HOST."""
        if not self.TEAM_NAME.fullmatch(str(name)):
            raise ValueError("invalid team")
        data = self.host_json(host, "team", "show", "--json", "--", name, timeout=60)
        if not isinstance(data, dict):
            raise ValueError("not a team")
        return data

    TEAM_MEMBER_FLAGS = {"--model": "--member-model", "--llm": "--member-llm", "--mode": "--member-mode"}

    def team_start_args(self, host, name, agents, workspace, options=(), vpn=None, member_vpn=None,
                        shared_openvpn=False, roles=None, member_options=None, login_question=True):
        """The words of `sandbox-ai team start --agents A,B[,...] --workspace DIR [--vpn PROFILE] [--member-vpn
        AGENT=PROFILE]... [--allow-shared-openvpn] [--role AGENT=TEXT]... [--member-model|--member-llm|--member-mode
        AGENT=VALUE]... NAME [-- OPTIONS]` on HOST: ROLES {agent: text}, MEMBER_OPTIONS {agent: {--model/--llm/--mode:
        value}}. Their values are team start's to check (team.check_roles, check_member_options): only their shape
        here, so that nothing is read as an option. Without LOGIN_QUESTION: --no-login-question (the app asked for
        the members' logins before; only for a team start there that knows it - its dry run says "logins")."""
        if not self.TEAM_NAME.fullmatch(str(name)):
            raise ValueError("a team's name: 1-20 of a-z, 0-9, '_' and '-'")
        agents = list(dict.fromkeys(agents))
        if len(agents) < 2 or any(agent not in AGENTS for agent in agents):
            raise ValueError("two or more agents")
        member_vpn = dict(member_vpn or {})
        if any(p is not None and not self.VPN_PROFILE.fullmatch(str(p)) for p in [vpn, *member_vpn.values()]) \
                or any(agent not in agents for agent in member_vpn):
            raise ValueError("invalid VPN profile")
        vpn_args = [*(["--vpn", vpn] if vpn else []),
                    *[value for agent, profile in member_vpn.items() for value in ("--member-vpn", f"{agent}={profile}")],
                    *(["--allow-shared-openvpn"] if shared_openvpn else [])]
        for agent, text in (roles or {}).items():
            if agent not in agents or not isinstance(text, str) or not text.isprintable():
                raise ValueError("a role is one line of text, for a member of the team")
            if text.strip():
                vpn_args += ["--role", f"{agent}={text.strip()}"]
        for agent, own in (member_options or {}).items():
            if agent not in agents or not isinstance(own, dict) or set(own) - set(self.TEAM_MEMBER_FLAGS):
                raise ValueError("a member's own options are --model, --llm and --mode, for a member of the team")
            for option, value in own.items():
                if not isinstance(value, str) or not value.isprintable():
                    raise ValueError("invalid member option")
                if value.strip():
                    vpn_args += [self.TEAM_MEMBER_FLAGS[option], f"{agent}={value.strip()}"]
        if not valid_directory(workspace):
            raise ValueError("the workspace is an absolute path")
        if any(not isinstance(o, str) or "\0" in o for o in options):
            raise ValueError("invalid option")
        # Each member's start: the team's workspace is the main one (see as_given; on another host where its launcher
        # is known to take the option).
        options = self.host_as_given(host, agents[0], ["--workspace", workspace, *options])[2:]
        return ["team", "start", "--agents", ",".join(agents), "--workspace", workspace, *vpn_args,
                *([] if login_question else ["--no-login-question"]), name, *(["--", *options] if options else [])]

    def team_start_argv(self, host, name, agents, workspace, options=(), vpn=None, member_vpn=None,
                        shared_openvpn=False, roles=None, member_options=None, login_question=True):
        """team_start_args on HOST, for a terminal (each member starts there, one after the other)."""
        return self.host_terminal(host, *self.team_start_args(host, name, agents, workspace, options, vpn, member_vpn,
                                                              shared_openvpn, roles, member_options, login_question))

    def team_check(self, host, name, agents, workspace, options=(), vpn=None, member_vpn=None, shared_openvpn=False,
                   roles=None, member_options=None):
        """`team start ... --dry-run --json` on HOST (team_start_args): every check of team start there, then each
        member's start as the launcher's dry run; nothing is made. {"ok": True, "plan": [...], ...} or {"ok": False,
        "error": why, "code": "exists" or None}. A sandbox-ai there without the dry run (older than 0.1.98) is said
        as such. ValueError: no answer."""
        args = self.team_start_args(host, name, agents, workspace, options, vpn, member_vpn, shared_openvpn, roles,
                                    member_options)
        at = args.index("--") - 1 if "--" in args else len(args) - 1  # NAME: its own options come before it
        args = [*args[:at], "--dry-run", "--json", *args[at:]]
        status, out, err = self.host_run(host, *args, timeout=240)
        try:
            found = json.loads(out)
        except (ValueError, RecursionError):
            found = None
        if isinstance(found, dict) and isinstance(found.get("ok"), bool):
            return found
        said = (err or out).strip()
        if "unrecognized arguments" in said or "invalid choice" in said:
            return {"ok": False, "code": "too-old", "error": "the sandbox-ai there is older than 0.1.98: its team "
                    "start has no dry run, roles or members' own options (update it: sandbox-ai hosts update)"}
        raise ValueError(said[-500:] or f"exit status {status}")

    def team_post(self, host, name, agent, task):
        """`sandbox-ai team post -- NAME AGENT TASK` on HOST: a task for a member, from the human."""
        if not self.TEAM_NAME.fullmatch(str(name)) or agent not in AGENTS:
            return 2, "", "invalid team or agent"
        if not isinstance(task, str) or not task.strip() or "\0" in task:
            return 2, "", "empty task"
        return self.host_run(host, "team", "post", "--", name, agent, task, timeout=60)

    def team_stop(self, host, name):
        """`sandbox-ai team stop -- NAME` on HOST: every member and the board end (asked first: start-stop)."""
        if not self.TEAM_NAME.fullmatch(str(name)):
            return 2, "", "invalid team"
        return self.host_run(host, "team", "stop", "--", name, timeout=300)

    def team_delete(self, host, name):
        """`sandbox-ai team delete --yes -- NAME` on HOST: stopped, its members' HOMEs and its board removed (asked
        first: start-delete)."""
        if not self.TEAM_NAME.fullmatch(str(name)):
            return 2, "", "invalid team"
        return self.host_run(host, "team", "delete", "--yes", "--", name, timeout=300)

    # == end of the parity agent's section ==

    # == starts on every host, resume and stop, MCP servers, net keep, privacy check, version, demo device: the
    # start/host agent's section ==
    # Each runs `sandbox-ai ...` on this machine ("local") or a host of `sandbox-ai hosts` (host_run, host_json,
    # host_terminal); every value that goes back as an argument is checked first (start_options.py-like rules of the
    # web interface: absolute paths, names, X.Y.Z) and never read as an option.

    def host_launch_options(self, name, agent, fresh=False):
        """The option strings `sandbox-ai AGENT` takes on host NAME: this machine's from the launcher's own parser,
        another host's from its `sandbox-ai AGENT --help` there (the option lines parsed; kept per host, agent and
        sandbox-ai version, for NET_OPTIONS_SECONDS without a known version; FRESH: asked now). ValueError: no
        answer."""
        if not (isinstance(agent, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", agent)):
            raise ValueError("not an agent")
        if name == "local":
            return {option for action in self.parser_actions() for option in action.option_strings}
        self.host_version_known(name)  # (a worker: SSH is fine here) a host just added says its version first
        try:
            version = (self.host_statuses(cached=True).get(name) or {}).get("version")
        except Exception as _exc:  # noqa: BLE001
            # (an older sandbox-ai, or no list of hosts: asked every NET_OPTIONS_SECONDS)
            applog.swallowed("backend", "SandboxAI.host_launch_options: an older sandbox-ai, or no list of host…", _exc)
            version = None
        cache = self.__dict__.setdefault("_launch_options", {})
        key = (name, agent, version)
        kept = cache.get(key)
        if kept is not None and not fresh and (version or time.time() - kept[0] < NET_OPTIONS_SECONDS):
            return set(kept[1])
        status, out, err = self.host_run(name, agent, "--help", timeout=60)
        found = help_options(out) if status == 0 else set()
        if not found:
            raise ValueError((err or out).strip()[-300:] or f"exit status {status}")
        cache[key] = (time.time(), found)
        return set(found)

    def host_launch_argv(self, name, agent, argv, agent_args=(), session=None):
        """argv of a start on host NAME in a terminal: launch_argv here, `ssh -tt ... sandbox-ai AGENT ...` there."""
        if name == "local":
            return self.launch_argv(agent, argv, agent_args, session)
        if session is not None and not valid_run(session):
            raise ValueError("a session name is letters, digits, '_', '.' or '-'")
        extra = ["--session", session] if session else []
        argv = self.host_as_given(name, agent, argv)
        return self.host_terminal(name, agent, *argv, *extra, *(["--", *agent_args] if agent_args else []))

    def cached_launch_options(self, name, agent=None):
        """The options host NAME's launcher takes for AGENT (any agent when None) as host_launch_options last read them,
        or None when not read yet: never asked here (a start in the window's thread must not wait for SSH)."""
        cache = self.__dict__.get("_launch_options", {})
        found = [(kept[0], kept[1]) for key, kept in cache.items()
                 if key[0] == name and (agent is None or key[1] == agent)]
        return set(max(found, key=lambda item: item[0])[1]) if found else None

    def host_version_known(self, name):
        """Ask host NAME for its sandbox-ai version (one short command through its shared SSH connection) when no
        status of it is kept yet (hosts.ask_version); for a worker, never the window's thread."""
        hosts = self.module("hosts")
        if name != "local" and hasattr(hosts, "ask_version"):
            try:
                hosts.ask_version(name, command=lambda host, args, timeout: self.host_run(host, *args, timeout=timeout))
            except Exception as _exc:  # noqa: BLE001 - an older or broken list of hosts: the version stays unknown
                applog.swallowed("backend", "SandboxAI.host_version_known: an older or broken list of hosts: th…", _exc)

    def host_knows_as_given(self, name, agent=None):
        """Host NAME's launcher takes --workspaces-as-given: its kept status names 0.1.78 or later (a small local
        file, no SSH: hosts.knows_workspaces_as_given, as the web asks it), or its options read already list it.
        The option is hidden from --help, so the version is what tells (review 6 A-N3)."""
        hosts = self.module("hosts")
        try:
            if hasattr(hosts, "knows_workspaces_as_given") and hosts.knows_workspaces_as_given(name):
                return True
        except Exception as _exc:  # noqa: BLE001 - no list of hosts, a damaged status file: not known
            applog.swallowed("backend", "SandboxAI.host_knows_as_given: no list of hosts, a damaged status file…", _exc)
        known = self.cached_launch_options(name, agent)
        return bool(known and AS_GIVEN in known)

    def host_as_given(self, name, agent, argv):
        """ARGV for a start on host NAME with --workspaces-as-given when it names its workspace and that host's
        launcher is known to take the option (host_knows_as_given: its version, kept, or its options read): the
        workspace chosen in the app is /work there too, never a project file in the SSH login's directory
        (sandbox-ai 0.1.78). An older host, or one not known yet, gets ARGV as it is (its launcher has no such rule
        or would refuse it)."""
        if name == "local":
            return self.as_given(argv)
        argv = list(argv)
        names_workspace = any(word == "--workspace" or word.startswith("--workspace=") for word in argv)
        if names_workspace and AS_GIVEN not in argv and self.host_knows_as_given(name, agent):
            argv.append(AS_GIVEN)
        return argv

    def host_dry_run(self, name, agent, argv):
        """(plan or None, message) of `sandbox-ai AGENT ARGV --dry-run` on host NAME (nothing starts there)."""
        if name == "local":
            return self.dry_run(agent, argv)
        self.host_version_known(name)  # (a worker) a host just added: its version first, for host_as_given
        argv = self.host_as_given(name, agent, argv)
        status, out, err = self.host_run(name, agent, *argv, "--dry-run", timeout=90)
        if status == 255 or (status == 124 and not (out or "").strip()):
            # ssh's own failure (not reached, refused, a host key that changed) or nothing in time: no answer of that
            # host's launcher - never shown as its refusal of the options ("The launcher refuses: ssh: connect ...")
            raise HostUnreachable(name, (err or out or "").strip()[-600:] or f"exit status {status}")
        return parse_dry_run(status, out, err)

    def mcp_origins(self):
        """{name: the catalog it was added from, with "signed"/"unsigned"} of this machine's MCP servers
        (mcp.origin); {} for a sandbox-ai that does not record it."""
        mcp = self.module("mcp")
        if not hasattr(mcp, "origin"):
            return {}
        return {name: origin_text(mcp.origin(name, d)) for name, d in mcp.definitions().items()}

    def host_mcp_servers(self, name):
        """[(name, status, the starts it serves, what runs, the catalog it came from)] of the MCP servers defined on
        host NAME (`sandbox-ai mcp list --json` there; this machine's from sandbox-ai's own module)."""
        if name == "local":
            try:
                served = self.mcp_serving()
            except Exception as _exc:  # noqa: BLE001 - the starts could not be read: the servers are listed anyway
                applog.swallowed("backend", "SandboxAI.host_mcp_servers: the starts could not be read: the serv…", _exc)
                served = {}
            try:
                origins = self.mcp_origins()
            except Exception as _exc:  # noqa: BLE001 - the origin is only shown
                applog.swallowed("backend", "SandboxAI.host_mcp_servers: the origin is only shown", _exc)
                origins = {}
            return [(server, state, ", ".join(served.get(server, [])), what, origins.get(server, ""))
                    for server, state, what in self.mcp_servers()]
        data = self.host_json(name, "mcp", "list", "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a list of MCP servers")
        self.__dict__.setdefault("_mcp_listed", {})[name] = [  # (host_mcp_notes: its problems and recommendations)
            entry for entry in data if isinstance(entry, dict) and re.fullmatch(MCP_NAME, str(entry.get("name") or ""))]
        rows = []
        for entry in data:
            if not isinstance(entry, dict) or not re.fullmatch(MCP_NAME, str(entry.get("name") or "")):
                continue
            serves = entry.get("serves") if isinstance(entry.get("serves"), list) else []
            rows.append((entry["name"], printable(str(entry.get("status") or ""))[:20],
                         printable(", ".join(str(s) for s in serves))[:300],
                         printable(str(entry.get("image") or entry.get("command") or ""))[:300],
                         origin_text(entry) if "catalog" in entry else ""))
        return rows

    def host_mcp_notes(self, name):
        """{server: {"problem": {time, what, why, fix} or None, "below": {key: value}}} of host NAME's MCP servers: what
        ended or failed a server's processes last (mcp.py's watch) and the limits below its catalog's recommendation.
        This machine: sandbox-ai's own module; a host: what its last `mcp list --json` said (host_mcp_servers)."""
        if name == "local":
            mcp = self.module("mcp")
            if not hasattr(mcp, "last_problem"):
                return {}
            found = {}
            try:
                own = set(mcp.read_config()[1])
            except (OSError, ValueError):
                own = set()
            for server, definition in mcp.definitions().items():
                problems = [p for p in (mcp.last_problem(i) for i in mcp.instances(server, include_stopped=True)) if p]
                problem = max(problems, key=lambda p: str(p.get("time") or "")) if problems else None
                below = mcp.below_recommended(definition) if "image" in definition or "rootfs" in definition else {}
                # The newer image its catalog pins (as last read here): Update takes it (sandbox-ai mcp update).
                newer = mcp.newer_pin(definition) if hasattr(mcp, "newer_pin") and server in own else None
                if problem or below or newer:
                    found[server] = {"problem": problem, "below": below, "newer": newer_shown(newer)}
            return found
        found = {}
        for entry in self.__dict__.get("_mcp_listed", {}).get(name, []):
            problem = entry.get("last_problem") if isinstance(entry.get("last_problem"), dict) else None
            below = entry.get("below_recommended") if isinstance(entry.get("below_recommended"), dict) else {}
            newer = newer_shown(entry.get("newer"))
            if problem or below or newer:
                found[entry["name"]] = {"problem": {k: printable_text(str(problem.get(k) or ""))[:600]
                                                    for k in ("time", "what", "why", "fix")} if problem else None,
                                        "below": {printable(str(k))[:20]: printable(str(v))[:20] for k, v in below.items()},
                                        "newer": newer}
        return found

    def host_mcp_update(self, host, names, unsigned=False):
        """`sandbox-ai mcp update NAMES --yes --json` on HOST (no names: every server of its own from a catalog), asked
        first by the page (mcp-update): the catalogs read again there, each signature checked, the images pulled by
        digest. Its result ({"servers": [...], "updated": [names]}); ValueError with what it said when it failed."""
        names = [str(name) for name in names]
        if not all(re.fullmatch(MCP_NAME, name) for name in names):
            raise ValueError("not an MCP server's name")
        status, out, err = self.host_run(host, "mcp", "update", *names, "--yes", "--json",
                                         *(["--unsigned"] if unsigned else []), timeout=3600)
        try:
            result = json.loads(out)
        except (ValueError, RecursionError):
            result = None
        if status != 0 or not isinstance(result, dict):
            raise ValueError(printable_text((err or out).strip()[-1500:] or f"exit status {status}"))
        return result

    def host_mcp_list(self, name):
        """[{"name", "status", "limits", "instances", "kind", "own", "definition", "builtin_defaults", ...}] of the MCP
        servers of host NAME, as `sandbox-ai mcp list --json` there lists them (this machine: sandbox-ai's own module,
        mcp.server_list); a server without one of these keys comes from an older sandbox-ai. ValueError: not read."""
        if name == "local":
            mcp = self.module("mcp")
            if hasattr(mcp, "server_list"):
                try:
                    served = self.mcp_serving()
                except Exception as _exc:  # noqa: BLE001 - the starts could not be read: the servers are listed anyway
                    applog.swallowed("backend", "SandboxAI.host_mcp_list: the starts could not be read: the ser…", _exc)
                    served = {}
                return mcp.server_list(served)
        data = self.host_json(name, "mcp", "list", "--json", timeout=60)
        if not isinstance(data, list):
            raise ValueError("not a list of MCP servers")
        return [entry for entry in data if isinstance(entry, dict) and re.fullmatch(MCP_NAME, str(entry.get("name")))]

    def host_mcp_catalog(self, name, catalogs=()):
        """([(prefix, entry)], problems) of the MCP catalogs as `sandbox-ai mcp catalog [--catalog SOURCE]...` on host
        NAME lists them (its lines read; an entry: {"name", "network", "hosts", "title", "digest" (only "published" is
        known), "_signature": {"state"}}). This machine without CATALOGS: sandbox-ai's own module (every field)."""
        if name == "local" and not catalogs:
            entries, problems = self.mcp_catalog()
            label = getattr(self.module("mcp"), "catalog_label", None)
            return [(prefix, {**entry, "_catalog_name": printable(label(entry["_catalog"]))[:300]}
                     if label and isinstance(entry.get("_catalog"), str) else entry)
                    for prefix, entry in entries], problems
        # The whole entries as this machine's module gives them (`mcp catalog --json` there, since 0.1.95): Add… shows
        # the same fields for a host's entry - its folders with their container paths and reasons, its variables, its
        # limits. Only a host older than that answers with its lines: the entry's line alone ("_listed").
        args = ("mcp", "catalog", *catalog_options(catalogs))
        status, out, err = self.host_run(name, *args, "--json", timeout=150)
        try:
            document = json.loads(out)
        except (ValueError, RecursionError):
            document = None
        if isinstance(document, dict) and isinstance(document.get("entries"), list):
            found = [entry for entry in (self.remote_catalog_entry(item, catalogs) for item in document["entries"])
                     if entry is not None]
            problems = [printable(str(p))[:600] for p in document.get("problems") or [] if isinstance(p, str)][:20]
            return found, problems
        if status == 2 and re.search(r"unrecognized arguments", err or ""):  # before 0.1.95: its lines
            status, out, err = self.host_run(name, *args, timeout=150)
        if status != 0:
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        found = []
        for line in out.splitlines():
            match = MCP_CATALOG_LINE.fullmatch(line.rstrip())
            if not match:
                continue
            network, _, hosts = match["network"].partition(" ")
            found.append((match["prefix"], {
                "name": match["name"], "network": printable(network), "hosts": printable(hosts).split(),
                "title": printable(match["title"]), "digest": None if match["unpublished"] else "published",
                "_signature": {"state": {"signed": "good", "SIGNATURE BAD": "bad"}.get(match["signature"], "unsigned")},
                "_listed": True, "_catalog_name": self.catalog_name(match["prefix"], catalogs)}))
        problems = [printable(line)[:600] for line in (err or "").splitlines() if line.strip()][:20]
        return found, problems

    def remote_catalog_entry(self, item, catalogs=()):
        """(prefix, entry) of one entry of `mcp catalog --json` on a host, in the shape this machine's catalog module
        gives (what AddServerDialog reads): its name, title, description, network and hosts, published or not, its
        signature, the variables and folders it takes, the limits it recommends - every text printable and bounded
        (an unsigned catalog of that host is anyone's text), anything else left out. None: not an entry."""
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", str(item.get("name"))):
            return None
        prefix = str(item.get("prefix") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", prefix):
            return None

        def text(value, limit=300):
            return printable(" ".join(str(value or "").split()))[:limit]
        env = [{"name": v["name"], "description": text(v.get("description"), 500), "secret": v.get("secret") is True,
                "required": v.get("required") is True}
               for v in item.get("env") or [] if isinstance(v, dict) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}",
                                                                                      str(v.get("name")))]
        mounts = [{"target": str(v["target"]), "required": v.get("required") is True,
                   "description": text(v.get("description"))}
                  for v in item.get("mounts") or [] if isinstance(v, dict) and isinstance(v.get("target"), str)
                  and re.fullmatch(r"/[A-Za-z0-9._/-]{0,199}", v["target"])]
        limits = {key: value for key, value in (item.get("limits") or {}).items()
                  if key in ("memory", "cpus", "pids", "tmp") and isinstance(value, (str, int, float))
                  and not isinstance(value, bool) and re.fullmatch(r"[0-9.]{1,10}[kmgKMG]?", str(value))} \
            if isinstance(item.get("limits"), dict) else {}
        signature = {"good": "good", "bad": "bad"}.get(str(item.get("signature")), "unsigned")
        entry = {"name": str(item["name"]), "title": text(item.get("title")),
                 "description": text(item.get("description"), 1000), "network": text(item.get("network") or "none", 20),
                 "hosts": [text(h, 260) for h in item.get("hosts") or [] if isinstance(h, str)][:32],
                 "digest": "published" if item.get("published") else None, "_signature": {"state": signature},
                 "env": env, "mounts": mounts,
                 "_catalog_name": text(item.get("catalog")) if item.get("catalog") else self.catalog_name(prefix,
                                                                                                       catalogs)}
        if limits:
            entry["limits"] = limits
        if isinstance(item.get("tool_timeout"), int) and not isinstance(item.get("tool_timeout"), bool):
            entry["tool_timeout"] = item["tool_timeout"]
        return prefix, entry

    def catalog_name(self, prefix, catalogs=()):
        """The catalog behind PREFIX of `mcp catalog`'s lines, for people (webui.catalog_name): sandbox-ai-mcp (mcp:),
        axross-mcp-hub (hub:), the Nth of CATALOGS for cN:."""
        try:
            mcp = self.module("mcp")
            label, defaults = mcp.catalog_label, dict(mcp.CATALOGS)
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: the prefix
            applog.swallowed("backend", "SandboxAI.catalog_name: an older sandbox-ai: the prefix", _exc)
            label, defaults = (lambda source: source), {}
        numbered = re.fullmatch(r"c([0-9]{1,2})", str(prefix))
        catalogs = list(catalogs or ())
        if numbered and int(numbered[1]) < len(catalogs):
            return printable(label(str(catalogs[int(numbered[1])])))[:300]
        return printable(label(defaults[prefix]) if prefix in defaults else str(prefix))[:300]

    def host_mcp_targets(self, name):
        """The agent starts on host NAME an MCP server can be attached to: the kept ones (mcp_attach_targets here,
        the sessions of `sandbox-ai list --json` there)."""
        if name == "local":
            return self.mcp_attach_targets()
        return [{"name": i.name, "agent": i.agent, "state": i.state, "mcp": []}
                for i in self.host_instances(name) if i.kind == "session"]

    def host_mcp_attach(self, name, server, start, restart=False):
        """mcp_attach on host NAME: {"attached", "message", ...}. ValueError: not attached."""
        if name == "local":
            return self.mcp_attach(server, start, restart)
        if not re.fullmatch(MCP_NAME, str(server)) or not valid_run(start):
            raise ValueError("invalid server or start")
        data = self.host_json(name, "mcp", "attach", server, start, "--json", *(["--restart"] if restart else []),
                              timeout=600)
        if not isinstance(data, dict):
            raise ValueError("not what sandbox-ai mcp attach says")
        return data

    def host_mcp_attach_live(self, host, start, server=None, helper=None):
        """`sandbox-ai mcp attach NAME START --live --json` (or --helper AGENT START) on HOST (mcp_live)."""
        return self.mcp_live(host, start, server, helper)

    def host_mcp_detach_live(self, host, start, server=None, helper=None):
        """`sandbox-ai mcp detach NAME START --live --json` (or --helper AGENT START) on HOST (mcp_live)."""
        return self.mcp_live(host, start, server, helper, detach=True)

    def host_mcp_detach(self, host, start, server=None, helper=None):
        """`sandbox-ai mcp detach NAME START --json` (or --helper AGENT START) on HOST: the server out of a stopped or
        paused start's options, for its later starts (mcp_live's answer)."""
        return self.mcp_live(host, start, server, helper, detach=True, live=False)

    def host_mcp_copy(self, source, target, names, **choices):
        """`sandbox-ai mcp copy NAME... --from SOURCE --to TARGET --json` on this machine (mcp_copy_args: the check with
        dry_run, else the copy - asked first by the page - and with attach the start there given them now). Its answer
        {"from", "to", "servers": [plan], "attached", "written", "dry_run"} - also when the check found what TARGET
        cannot take as it is (each server's "problems", with their fixes). ValueError: refused (an older sandbox-ai
        there: the update to do), with sandbox-ai's reason."""
        args = mcp_copy_args(source, target, names, **choices)
        status, out, err = self.call(*args, timeout=3600 if not choices.get("dry_run", True) else 300)
        try:
            data = json.loads(out)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
            said = printable_text((err or out).strip())
            older = re.search(r"the sandbox-ai on ([a-z0-9][a-z0-9_-]{0,31}) does not know `sandbox-ai (mcp "
                              r"[a-z-]+)[^`]*` yet", said)
            if older:  # the update to do, in the app's words (and language)
                try:
                    version = self.version()
                except Exception as _exc:  # noqa: BLE001 - said without it
                    applog.swallowed("backend", "SandboxAI.host_mcp_copy: this machine's version is not known", _exc)
                    version = "?"
                raise HostTooOld(older[1], tuple(older[2].split()), [], needs=version)
            raise ValueError(said.removeprefix("sandbox-ai mcp: ")[-1500:] or f"exit status {status}")
        data["status"] = status
        data["said"] = [printable(line)[:600] for line in (err or "").splitlines() if line.strip()][-20:]
        return data

    def host_mcp_copy_check(self, source, target, names, **choices):
        """The check of a copy (host_mcp_copy with dry_run): what would be copied and what TARGET cannot take as it is;
        nothing is copied or written anywhere."""
        return self.host_mcp_copy(source, target, names, **{**choices, "dry_run": True})

    def host_mcp_elsewhere(self, target):
        """[(host, [{"name", "kind", "catalog"}], error or None)] of the MCP servers of your own on every host but
        TARGET (this machine and the hosts of `sandbox-ai hosts`): what "From another host…" offers. Each host is asked
        in a thread of its own (`mcp list --json` there); one that does not answer says why."""
        hosts = [host for host in ["local", *self.remote_hosts()] if host != target]
        found = {}

        def read(host):
            try:
                listed = self.host_mcp_list(host)
                found[host] = ([{"name": e["name"], "kind": printable(str(e.get("kind") or ""))[:20],
                                 "catalog": printable(str(e.get("catalog") or ""))[:200]}
                                for e in listed if e.get("own", e.get("catalog") != "built-in")
                                and e.get("catalog") != "built-in" and not str(e["name"]).startswith("team-")], None)
            except Exception as exc:  # noqa: BLE001 - that host's row says why
                found[host] = ([], printable_text(str(exc))[:300])
        threads = [threading.Thread(target=read, args=(host,), name="mcp-elsewhere", daemon=True) for host in hosts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        return [(host, *found.get(host, ([], "no answer"))) for host in hosts]

    def mcp_live(self, host, start, server=None, helper=None, detach=False, live=True):
        """`sandbox-ai mcp attach NAME START --live --json` (HELPER: --helper AGENT in place of NAME; DETACH: `mcp
        detach ... --live --json`) on HOST: the server into (or out of) the running sandbox of START now, without a
        restart. Its answer ({"server", "start", "attached" or "detached", "agent_restart", "message", ...}) with
        "notes": what it said besides on its error output. ValueError: refused, with sandbox-ai's reason."""
        args = mcp_live_args(start, server, helper, detach, live)
        self.require_options(host, tuple(args[:2]), [o for o in args if o in ("--live", "--helper")])
        status, out, err = self.host_run(host, *args, timeout=600)
        data = None
        if status == 0:
            try:
                data = json.loads(out)
            except (ValueError, RecursionError):
                data = None
        if not isinstance(data, dict):
            text = (err or out).strip()
            if host != "local" and status == 2 and re.search(
                    r"invalid choice|unrecognized arguments|the following arguments are required", text):
                # an older sandbox-ai there (0.1.77 has no mcp detach): "update it", not its usage (review 6 W-F2)
                raise HostTooOld(host, tuple(args[:2]), [o for o in args if o in ("--live", "--helper")] or
                                 [args[1]])
            # the command's prefix off: "sandbox-ai mcp: " (its errors; every message from 0.1.95 on), "sandbox-mcp: "
            # (the rest, before)
            lines = [re.sub(r"^(?:sandbox-ai mcp|sandbox-mcp): ", "", line)
                     for line in text.splitlines() if line.strip()]
            # each line on its own: a line break stays one (printable would write it as \x0a)
            raise ValueError("\n".join(printable(line) for line in lines[-5:])[-800:] or f"exit status {status}")
        message = data.get("message") if isinstance(data.get("message"), str) else ""
        data["message"] = printable_text(message)[:2000]
        data["notes"] = [printable(line.strip())[:500] for line in (err or "").splitlines()
                         if line.strip() and line.strip() != message.strip()][:10]
        return data

    def host_kits_attach(self, host, args):
        """kits_live_args (`kits attach` or `plugins attach` … --json) on HOST: given to the running start now."""
        return self.kits_live(host, args)

    def host_kits_detach(self, host, args):
        """kits_detach_args (`kits detach` or `plugins detach` … --json) on HOST: taken out of the running start."""
        return self.kits_live(host, args)

    def host_agent_restart(self, host, start):
        """`sandbox-ai kits restart-agent START --json` on HOST: its agent started again inside the running sandbox."""
        return self.kits_live(host, restart_agent_args(start))

    def kits_live(self, host, args):
        """`sandbox-ai ARGS` on HOST (kits_live_args, kits_detach_args, restart_agent_args): a kit, skill, command,
        subagent or plugin given to (taken out of) a running start, or its agent started again inside the sandbox. Its
        answer ({"message", "reload": [{"kind", "reload": auto|command|restart|sandbox, "command", "text"}], ...}),
        with "notes": what it said besides. ValueError: refused, with sandbox-ai's reason (an older sandbox-ai there:
        the update to do)."""
        args = list(args)
        if args[:2] not in (["kits", "attach"], ["kits", "detach"], ["kits", "restart-agent"], ["plugins", "attach"],
                            ["plugins", "detach"]) or args[-1] != "--json":
            raise ValueError("not a command for a running start's kits")
        status, out, err = self.host_run(host, *args, timeout=960)
        data = None
        if status == 0:
            try:
                data = json.loads(out)
            except (ValueError, RecursionError):
                data = None
        if not isinstance(data, dict):
            text = (err or out).strip()
            if host != "local" and status == 2 and re.search(r"invalid choice|unrecognized arguments", text):
                raise HostTooOld(host, tuple(args[:2]), [args[1]])
            lines = [re.sub(r"^sandbox-ai(?: kits| plugins)?: ", "", line) for line in text.splitlines() if line.strip()]
            raise ValueError("\n".join(printable(line) for line in lines[-5:])[-800:] or f"exit status {status}")
        message = data.get("message") if isinstance(data.get("message"), str) else ""
        data["message"] = printable_text(message)[:2000]
        data["notes"] = [printable(str(note))[:500] for note in data.get("notes") or [] if isinstance(note, str)][:10]
        data["reload"] = [item for item in data.get("reload") or [] if isinstance(item, dict)][:10]
        return data

    def host_running_starts(self, host):
        """The running kept starts of HOST a kit or plugin can be given to now: [{"name", "agent", "state"}]."""
        return [entry for entry in self.host_mcp_targets(host) if entry.get("state") == "running"]

    def host_llm_endpoints(self, host):
        """The model servers registered on HOST (`sandbox-ai llm list --json` there; this machine: llm_endpoints),
        each checked as the wizard checks this machine's (wizard_llm.parse_endpoints)."""
        if host == "local":
            return self.llm_endpoints()
        from .wizard_llm import parse_endpoints
        status, out, err = self.host_run(host, "llm", "list", "--json", timeout=90)
        if status != 0:
            raise ValueError(printable_text((err or out).strip())[-500:] or f"exit status {status}")
        return parse_endpoints(out)[0]

    def host_llm_models(self, host, name, timeout=60):
        """[model] the server NAME of HOST offers now (`sandbox-ai llm models NAME --json` there, within TIMEOUT
        seconds): each {name, size, loaded, parameters, type, capabilities, families ...} as wizard_llm.parse_models
        checks it. ValueError: not answered (the server is down, say)."""
        from .wizard_llm import models_argv, parse_models
        status, out, err = self.host_run(host, *models_argv(name), timeout=timeout)
        if status != 0:
            raise ValueError(printable_text((err or out).strip())[-500:] or f"exit status {status}")
        return parse_models(out)

    LLM_MODELS_SECONDS = 15  # a server that has not answered by then is listed without its models (the hint says why)
    LLM_MODELS_AT_ONCE = 8

    def host_llm_servers(self, host, endpoints=None, timeout=None):
        """The model servers of HOST for the Model choice beside the agent: every one `llm list --json` lists there
        (ENDPOINTS: that list, read already), each asked for its models (`llm models NAME --json`) - all of them at
        once (LLM_MODELS_AT_ONCE at a time), each within TIMEOUT seconds (LLM_MODELS_SECONDS; another host's a few
        more for SSH) - so NAME:MODEL is offered for that host's servers, not this machine's. A server that does not
        answer keeps what the list said, with "answered": False and "models_error" (why: the Model choice says it);
        one that answered has "answered": True. Never on the GUI thread (a background call)."""
        from concurrent.futures import ThreadPoolExecutor
        endpoints = [dict(e) for e in (self.host_llm_endpoints(host) if endpoints is None else endpoints)
                     if isinstance(e, dict) and isinstance(e.get("name"), str)]
        if not endpoints:
            return endpoints
        timeout = timeout or (self.LLM_MODELS_SECONDS if host == "local" else self.LLM_MODELS_SECONDS + 10)
        # (this machine's too: a server added without --model, e.g. LM Studio on a laptop, listed no models, and only
        # its bare name was offered; one after another with a minute each, a few servers that were down took longer
        # than the whole read may and the choice listed none)
        with ThreadPoolExecutor(max_workers=min(self.LLM_MODELS_AT_ONCE, len(endpoints)),
                                thread_name_prefix="llm-models") as pool:
            asked = [(endpoint, pool.submit(self.host_llm_models, host, endpoint["name"], timeout))
                     for endpoint in endpoints]
            for endpoint, future in asked:
                try:
                    listed = future.result()
                    # (its names for the choice; what is known of each - size, parameters, what it can do - for the
                    # model picker's tags, sorts and filters: wizard_llm.picker_rows)
                    endpoint["models"] = [m["name"] if isinstance(m, dict) else m for m in listed]
                    endpoint["model_info"] = {m["name"]: m for m in listed if isinstance(m, dict)}
                    endpoint["answered"] = True
                except Exception as exc:  # noqa: BLE001 - not answered, whatever the reason: what the list said
                    applog.swallowed("backend", "SandboxAI.host_llm_servers: not answered, whatever the reason: …", exc)
                    endpoint["answered"] = False
                    endpoint["models_error"] = printable_text(str(exc).strip())[-300:] or type(exc).__name__
        return endpoints

    def host_connections(self, host, source, tail=5000, since=None):
        """What the connection proxy of HOST logged for SOURCE (the arguments of `sandbox-ai activity`: a run, --session
        NAME, --log-dir DIR or --project DIR): `activity SOURCE --kind=network --json --tail N` there, each record as
        a connection log's ({"time", "event", "method", "status", "url", "destination", "bytes_in", ...}: what
        summary() shows) with its "summary" and "verdict"."""
        found = []
        for record in self.host_activity(host, source, tail=tail, since=since, kinds=("network",)):
            details = record.get("details") if isinstance(record.get("details"), dict) else {}
            entry = {key: value for key, value in details.items() if isinstance(key, str)}
            if not isinstance(entry.get("event"), str):
                entry["event"] = str(entry.get("verdict") or "")
            entry["time"] = record.get("time") or ""
            entry["summary"] = record.get("summary") or ""
            if not entry.get("url") and not entry.get("destination") and isinstance(entry.get("target"), str):
                entry["host"] = entry["target"]
            found.append(entry)
        return found

    def host_log_summary(self, host, source):
        """`sandbox-ai logs DIR --json` (or `logs --project DIR --json`) on HOST: what its connection logs say in
        short - refusals, their targets, the requests per host (a dict). ValueError: not answered."""
        if len(source) == 2 and source[0] in ("--log-dir", "--project") and valid_directory(source[1]):
            args = ["logs", source[1], "--json"] if source[0] == "--log-dir" else \
                ["logs", f"--project={source[1]}", "--json"]
        else:
            raise ValueError("a log folder or a project folder")
        data = self.host_json(host, *args, timeout=120)
        if not isinstance(data, dict):
            raise ValueError("not what sandbox-ai logs says")
        return data

    def net_keep(self, name, run, every=False):
        """`sandbox-ai net show RUN --keep` (EVERY: --keep-all) on host NAME: what the run was given, kept for its
        project's later starts (into its trusted .sandbox-ai.json). (status, out, err)."""
        if not valid_run(run):
            return 2, "", "invalid run"
        try:
            self.require_options(name, ("net", "show"), ["--keep-all" if every else "--keep"])
        except HostTooOld as exc:
            return 2, "", str(exc)
        return self.host_run(name, "net", "show", run, "--keep-all" if every else "--keep", timeout=90)

    def privacy_check(self, name, paths, rules_dir=None, project=None, every=False):
        """`sandbox-ai privacy-check --json ...` on host NAME: ([{"repository", "rules", "scanned", "notes",
        "findings": [{"where", "path", "id", "severity", "reason", "line"}]}], problems: the lines it said on its error
        output, e.g. a repository refused). ValueError: no such answer."""
        status, out, err = self.host_run(name, *privacy_check_args(paths, rules_dir, project, every), timeout=600)
        try:
            data = json.loads(out)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, list):
            raise ValueError((err or out).strip()[-800:] or f"exit status {status}")
        return [entry for entry in data if isinstance(entry, dict)], \
            [printable(line)[:600] for line in (err or "").splitlines() if line.strip()][:40]

    def version_details(self, name="local"):
        """(status, out, err) of `sandbox-ai version --verbose` on host NAME: where it is installed, its release
        keys, whether signed updates are required."""
        return self.host_run(name, "version", "--verbose", timeout=60)

    def host_doctor(self, name, engine="podman"):
        """[{check}] of `sandbox-ai doctor --backend ENGINE --json` on host NAME (the Hosts page's doctor with a
        backend of its choice). ValueError: no answer."""
        if engine not in ("podman", "bwrap", "both"):
            raise ValueError("the backend is podman, bwrap or both")
        status, out, err = self.host_run(name, "doctor", f"--backend={engine}", "--json", timeout=300)
        try:
            data = json.loads(out)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, list):
            raise ValueError((err or out).strip()[-500:] or f"exit status {status}")
        return [entry for entry in data if isinstance(entry, dict)]

    def demo_device(self, name, action, port=None):
        """`sandbox-ai demo-device start [--port PORT]|status|stop` on host NAME (the emulated serial device of the
        hardware-analysis preset). (status, out, err)."""
        return self.host_run(name, *demo_device_args(action, port), timeout=60)

    # == end of the start/host agent's section ==


def weakening_options(launcher, options):
    """[(long name, value or None)] of the start OPTIONS (as a project file has them) that weaken the sandbox or widen
    what it may do or see, however they are written (short forms, bundles, -x=VALUE): the launcher's own table and
    parser (modes.weakening_options). A launcher without that table: every option is named (none goes unmarked)."""
    modes = getattr(launcher, "MODES", None)
    if modes is not None and hasattr(modes, "weakening_options"):
        return modes.weakening_options(launcher, options)
    return [(str(word).split("=", 1)[0], str(word).partition("=")[2] or None) for word in options
            if str(word).startswith("-")]


WEB_USER = re.compile(r"[a-z][a-z0-9_.-]{0,31}")  # webui.USER_NAME: a login of the web interface
WEB_LISTEN = "127.0.0.1:8443"  # webui.DEFAULT_LISTEN


def web_listen_address(value):
    """The address of LISTEN (ADDRESS:PORT, as `sandbox-ai web serve --listen` takes it), an ipaddress object;
    ValueError otherwise."""
    import ipaddress
    host, sep, port = (value or "").rpartition(":")
    if not sep or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ValueError(f"--listen {printable(value)}: ADDRESS:PORT, e.g. 127.0.0.1:8443 or [::]:8443")
    return ipaddress.ip_address(host.strip("[]") or "127.0.0.1")


def web_listen_problem(value):
    try:
        web_listen_address(value)
    except ValueError as exc:
        return printable_text(str(exc)) if str(exc).startswith("--listen") else \
            f"--listen {printable(value)}: ADDRESS:PORT, e.g. 127.0.0.1:8443 or [::]:8443"
    return None


def web_listen_port(value):
    """The port of LISTEN (ADDRESS:PORT; checked by web_listen_address)."""
    web_listen_address(value)
    return int(value.rpartition(":")[2])


def free_local_port(preferred):
    """PREFERRED when nothing listens on it on this machine's loopback, else a free port the system names."""
    import socket
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
        except OSError:
            continue
    raise ValueError("no free port on this machine's loopback")


def web_service_url(host_target, listen, local=False):
    """The https:// URL of a web interface that listens on LISTEN of a host reached as HOST_TARGET (its ssh
    USER@HOST; LOCAL: this machine): its own address, or for a wildcard listener (0.0.0.0, ::) the host's name."""
    address, port = web_listen_address(listen), web_listen_port(listen)
    if address.is_unspecified:
        name = "localhost" if local else str(host_target or "").rpartition("@")[2] or "HOST"
    else:
        name = "localhost" if local and address.is_loopback else str(address)
    if ":" in name:
        name = f"[{name}]"
    return f"https://{name}:{port}/"


def web_listen_beyond(value):
    """Whether the web interface on LISTEN can be reached from other machines (any address but the loopback)."""
    return not web_listen_address(value).is_loopback


def marketplaces_document(document):
    """DOCUMENT if it is what `sandbox-ai plugins marketplaces --json` prints (version 1: marketplaces, agents and
    kits), else ValueError."""
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(
            document.get("marketplaces"), list) or not isinstance(document.get("agents"), dict) \
            or not isinstance(document.get("kits"), list):
        raise ValueError("sandbox-ai plugins marketplaces --json: not a list of marketplaces (version 1)")
    return document


class HostUnreachable(ValueError):
    """HOST gave no answer of its sandbox-ai: ssh itself failed (exit status 255: not reached, refused, a host key that
    changed) or nothing came in time. Not a refusal of the launcher there; str(): ssh's own words."""

    def __init__(self, host, message):
        super().__init__(message)
        self.host = host


class HostTooOld(ValueError):
    """The sandbox-ai on HOST does not know OPTIONS of COMMAND yet (require_options): told as an update to do. NEEDS
    (a version, this machine's: the command that has it): said with the update from here (too_old_text)."""

    def __init__(self, host, command, missing, needs=None):
        super().__init__(host, command, missing)
        self.host, self.command, self.missing, self.needs = host, tuple(command), list(missing), needs

    def __str__(self):
        from .i18n import tr
        if self.needs is not None:
            return tr("The sandbox-ai on {host} does not know `sandbox-ai {command}` yet: it needs a newer version "
                      "(this machine has {version}). Update it from here: sandbox-ai hosts update {host} --from-here "
                      "(Hosts › Update from this machine's copy), then try again.", host=printable(self.host),
                      command=" ".join(self.command), version=printable(str(self.needs or "?")))
        return tr("The sandbox-ai on {host} is too old for this: `sandbox-ai {command}` there does not know {options}. "
                  "Update the host (Hosts › Update), then try again.", host=printable(self.host),
                  command=" ".join(self.command), options=", ".join(self.missing))


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


def valid_bridge_id(ident):
    """IDENT when it is a bridge's id (eight hex digits), or HOST:ID for one of a host of `sandbox-ai hosts` (the
    launcher asks that host over SSH); else ValueError."""
    if not isinstance(ident, str) or not re.fullmatch(r"(?:[a-z0-9][a-z0-9_-]{0,31}:)?[0-9a-f]{8}", ident):
        raise ValueError(f"not a bridge's id: {ident!r}")
    return ident


CHECKPOINT_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")  # a workspace checkpoint's id (sandbox-ai checkpoint)
CHECKPOINT_SECONDS = 1800  # a checkpoint, diff or restore of a large workspace (a first one copies every file)


def checkpoint_id(ident):
    if not isinstance(ident, str) or not CHECKPOINT_ID.fullmatch(ident):
        raise ValueError("not a checkpoint's id")
    return ident


def checkpoint_paths(paths):
    """--path=P for each of PATHS (relative to /work, /work/..., /workspaces/NAME/... or a host path), checked."""
    found = []
    for path in paths or ():
        if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(c in path for c in "\n\r\0"):
            raise ValueError("a path is one line")
        found.append(f"--path={path.strip()}")
    return found


def valid_run(run):
    return isinstance(run, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run))


def plain_line(value):
    """VALUE is one printable line of at most 4096 characters, never read as an option."""
    return isinstance(value, str) and 0 < len(value) <= 4096 and value.isprintable() and not value.startswith("-")


def absolute_line(value):
    """VALUE is an absolute path on one printable line (a log folder, a project)."""
    return plain_line(value) and value.startswith("/")


def listed_file(value):
    """A file's name as `sandbox-ai session memory|files` lists it (a path in the start's HOME, one printable line):
    the command takes only a name of its own listing. ValueError for anything else."""
    if not (isinstance(value, str) and value and len(value) <= 4096 and value.isprintable()
            and not value.startswith(("/", "-"))):
        raise ValueError("a file is named as the listing names it")
    return value


# The kill switch (sandbox-ai net cut): its modes, and how long one host's answer may take (every run at once there).
CUT_MODES = ("remote", "local", "api", "all", "off")
CUT_WAIT = 90


def cut_name(value):
    """VALUE if it names a cut ("api", or modes that hold together: "remote+api"), else None."""
    parts = value.split("+") if isinstance(value, str) else []
    return value if parts and all(part in CUT_MODES[:-1] for part in parts) else None


def cut_report(result):
    """The runs of a `sandbox-ai net cut ... --json` RESULT ((status, out, err)): [{"run", "name", "for", "ok", "cut",
    "error"}], or None when it said no report (an older sandbox-ai, no answer); then its error text explains."""
    try:
        report = json.loads(result[1])
    except (ValueError, TypeError, IndexError, RecursionError):
        return None
    runs = report.get("runs") if isinstance(report, dict) else None
    if not isinstance(runs, list):
        return None
    return [{"run": str(entry.get("run") or ""), "name": str(entry.get("name") or entry.get("run") or ""),
             "for": entry.get("for") if isinstance(entry.get("for"), str) else None, "ok": entry.get("ok") is True,
             "cut": cut_name(entry.get("cut")),
             "error": str(entry.get("error") or "")} for entry in runs if isinstance(entry, dict)]


def valid_directory(path):
    """An absolute path of one line (never read as an option)."""
    path = str(path or "")
    return path.startswith("/") and len(path) <= 4096 and path.isprintable() and "\0" not in path


def valid_remote(value):
    """A folder of another machine as sandbox-ai takes it (sshfs://[USER@]HOST[:PORT]/PATH; remote_source.parse)."""
    from . import remote_source
    return remote_source.valid_source(value)


WORKSPACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # workspaces.NAME: workspace add --name NAME


def valid_workspace_name(name):
    """NAME for /workspaces/NAME as `sandbox-ai workspace add --name` takes it: 1-64 letters, digits, '_', '.' or
    '-', starting with a letter or digit (never "." or "..")."""
    return isinstance(name, str) and bool(WORKSPACE_NAME.fullmatch(name)) and name not in (".", "..")


DEVICE_SCAN_KINDS = ("usb", "serial", "video", "render", "kvm", "sound", "gpu", "input")  # devices.SCAN_KINDS
DEVICE_SCAN_LIMIT = 512
GPU_KINDS = ("nvidia", "dri", "rocm")  # agent_sandbox.GPU_KINDS


class DeviceScanMissing(ValueError):
    """The host's sandbox-ai has no `devices scan` (older than this app): its devices are typed in."""


USB_DEVICE = re.compile(r"usb:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}(?::[!-~]{1,126})?")  # devices.SELECTOR


def valid_toolchain(value):
    """A toolchain's name as sandbox-ai takes it: NAME or NAME@VERSION (toolchains.spec)."""
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9-]{0,31}(?:@[0-9A-Za-z][0-9A-Za-z.+_-]{0,63})?",
                                                        value)) and value.partition("@")[0] != "bin"


def toolchain_version_key(version):
    """VERSION for ordering as sandbox-ai orders them (toolchains.version_key): its numbers first, then its text."""
    return [int(part) for part in re.findall(r"[0-9]+", str(version))][:8], str(version)


def toolchain_selector(name, version, versions, bare=True):
    """What a sandbox-ai with versions (toolchains.selector) takes for NAME VERSION among VERSIONS: NAME for the newest
    (unless not BARE), else the shortest NAME@START that picks it up to a '.', '+', '-' or '_' (jdk@17,
    python@3.12), else NAME@VERSION."""
    versions = {*versions, version}

    def newest(start=None):
        found = [v for v in versions if start is None or v == start
                 or (v.startswith(start) and v[len(start)] in ".+-_")]
        if start is not None and start in versions:
            found = [start]
        return max(found, key=toolchain_version_key) if found else None
    if bare and newest() == version:
        return name
    for index, char in enumerate(version):
        if char in ".+-_" and newest(version[:index]) == version:
            return f"{name}@{version[:index]}"
    return f"{name}@{version}"


def toolchain_choices(found, attached=()):
    """What a list of toolchains offers of a host's store and signed catalog (FOUND: host_toolchains):
    [{"value", "name", "version", "row", "stored"}] - every version in the store, then the catalog's versions that
    are not, newest of a name first. "value" is what --toolchain, `toolchain add` and `attach` take for exactly that
    version, among the store's and the catalog's: NAME for the newest of both, else NAME@START (jdk@17); never NAME
    alone for one of the catalog when the store has another version of NAME (NAME at a start is the store's
    newest). A host's sandbox-ai before versions (its rows name no "toolchain") takes exact versions only:
    NAME@VERSION. A name in ATTACHED (one version of a toolchain per sandbox) is left out."""
    installed = [row for row in found.get("installed") or [] if row.get("name") not in attached]
    catalog = [row for row in found.get("catalog") or [] if row.get("name") not in attached]
    versioned = any("toolchain" in row for row in [*installed, *catalog])
    stored = {}
    for row in installed:
        stored.setdefault(row["name"], set()).add(row["version"])
    known = {name: set(versions) for name, versions in stored.items()}
    for row in catalog:
        known.setdefault(row["name"], set()).add(row["version"])

    def value_of(row, in_store):
        name, version = row["name"], row["version"]
        bare = in_store or not stored.get(name)
        if versioned:
            return toolchain_selector(name, version, known[name], bare=bare)
        among = stored[name] if in_store else known[name]
        newest = max(among, key=toolchain_version_key)
        return name if bare and version == newest else f"{name}@{version}"

    def ordered(rows):
        return sorted(rows, key=lambda row: (row["name"], [-n for n in toolchain_version_key(row["version"])[0]]))
    choices, values = [], set()
    for row in ordered(installed):
        value = value_of(row, True)
        if value not in values:
            values.add(value)
            choices.append({"value": value, "name": row["name"], "version": row["version"], "row": row,
                            "stored": True})
    for row in ordered(catalog):
        value = value_of(row, False)
        if row["version"] not in stored.get(row["name"], ()) and value not in values:
            values.add(value)
            choices.append({"value": value, "name": row["name"], "version": row["version"], "row": row,
                            "stored": False})
    return choices


def valid_device(path):
    """A device path of one line under /dev, or a USB device by identity, usb:VENDOR:PRODUCT[:SERIAL] (as
    `sandbox-ai devices add` takes it); never read as an option."""
    path = str(path or "")
    if USB_DEVICE.fullmatch(path):
        return True
    return path.startswith("/dev/") and len(path) <= 4096 and path.isprintable() and ".." not in path.split("/")


# -- one confirmation policy --
# What ends, removes or weakens something: the web interface's table (webui.NEEDS_CONFIRM: the same actions;
# tests/test_backend.py compares them with webui.py), and the app's own on top (APP_NEEDS_CONFIRM: what no route of the
# web interface does; none now). Every call path of the app that does one of these asks first through
# widgets.ask_first(), which asks needs_confirm() (tests/test_references.py walks the mutating calls of the pages).
NEEDS_CONFIRM = {
    "checkpoint-restore": "a restore puts the workspaces back as the checkpoint holds them: what changed since is "
                          "replaced or removed (a checkpoint of the state now is taken first, so it can be undone)",
    "checkpoint-delete": "deleting a checkpoint removes it and the content only it held, for good",
    "session-export": "the archive holds the start's HOME as its agent left it - its transcripts, its memory, the "
                      "files it kept (logins and keys are left out) - in a new file on that host",
    "session-export-secrets": "the archive then holds the start's logins, keys and secrets too: whoever has it can "
                              "log in as you with them, wherever it goes",
    "session-grant-approvals": "the network approvals are granted again as options of that session: it reaches those "
                               "destinations at every start",
    "session-import": "the imported session's configuration gives the sandbox secrets or folders of this machine (as "
                      "listed): they reach it at its first resume",
    "show-secret-file": "the file is marked as a login or key: its text is shown on this screen, where anyone who "
                        "sees it can read it",
    "board-import": "importing a bundle from elsewhere writes its parts here (MCP and LLM servers, kits, presets, "
                    "rulesets, image recipes; replaced ones when chosen) and keeps its board",
    "start-stop": "stopping a start ends what runs in it (and its HOME, unless it is kept)",
    "kits-attach": "skills, subagents, commands or a plugin given to a running start reach its agent at once "
                   "(read-only; a plugin's code runs as the agent; kept: also its later starts)",
    "kits-detach": "taking a skill, subagent, command or plugin out of a running start takes it from its agent (and "
                   "from its later starts where it was kept)",
    "agent-restart": "restarting the agent inside the running sandbox ends what it does now (its conversation "
                     "continues; the sandbox, network and MCP servers stay)",
    "start-delete": "deleting a start removes its kept HOME",
    "storage-apply": "the chosen items of the storage check run: caches, build output, toolchains or old transcripts "
                     "removed for good, transcripts compacted, HOMEs packed - as each one says",
    "gc": "gc removes the leftovers of ended starts, and kept starts unused for that long with their HOME, for good",
    "approval-forget": "forgetting a remembered answer changes what every later start of that project gets (a "
                       "forgotten block is asked again)",
    "defaults-lower": "this lowers the protection of every later start on that host",
    "shared-wildcard": "a host wildcard below a shared service opens every name anyone rents there: the agent could "
                       "open any such server, and send data there",
    "net-allow": "net allow widens what the running sandbox reaches, at once (real DNS, ports on the host, host "
                 "wildcards, Git logins included)",
    "net-untaint": "untaint takes back the rules for tainted runs: the sandbox is treated as if no untrusted content "
                   "had entered it",
    "workspace-writable": "a writable workspace lets the agent change the directory on that host",
    "workspace-force": "a forced removal leaves what a process there still holds usable, also for writing, until it "
                       "is closed; a copy the agent took stays usable until the sandbox restarts",
    "device-add": "a device gives the agent that hardware of the host",
    "toolchain-add": "a toolchain gives the agent that compiler or interpreter, read-only, and opens the package "
                     "registries it names for the run",
    "toolchain-force": "a forced detach leaves what a process there still runs or holds of the toolchain usable "
                       "until it is closed",
    "limits-raise": "a limit above the current one (or none) lets the agent use more of that host",
    "host-remove": "removing a host ends this service's management of it",
    "vpn-remove": "removing a VPN profile stops the starts that use it from starting",
    "llm-remove": "removing an LLM server stops the starts that use it from starting",
    "kit-delete": "deleting a kit removes its folder",
    "kit-remove": "removing a skill or plugin from a kit takes it from every later start with that kit",
    "kit-add": "a skill, plugin or marketplace added to a kit is given to every later start with that kit",
    "memory-add": "memory added to a project's memory is given to every later start of that project (it may hold "
                  "private notes)",
    "mcp-attach": "an attached MCP server is reached by that start from its next start on (kept for its later "
                  "starts)",
    "mcp-restart": "restarting a running start ends what runs in it now, so that it reaches the MCP server (the agent "
                   "continues its conversation)",
    "host-edit": "changing a host's SSH target, port, key, command or sudo setting changes where and how this "
                 "service's commands run",
    "host-uninstall": "uninstalling sandbox-ai on a host removes that installation there (with --purge also the "
                      "SSH user's data, with --secrets its secrets and logins, which cannot be recovered)",
    "host-install": "installing sandbox-ai on a host puts this machine's copy (or the signed release) there, for the "
                    "SSH user or for every user, as root or with sudo",
    "secret-delete": "deleting a secret takes it from every start, VPN profile and Git login that names it",
    "secret-replace": "a secret of that name exists: its value is replaced",
    'project-trust': 'a trusted project configuration gives every later start of that project its options',
    'project-forget': 'a project configuration no longer trusted stops the starts of that project from starting until it is trusted again',
    'mcp-stop': 'stopping an MCP server ends it for every start that uses it now',
    "mcp-update": "the server's image is replaced by the newer one its catalog pins (its signature checked again; its "
                  "folders, variables, limits and network stay): every later start runs that image",
    'mcp-unsigned': 'an MCP server from a catalog no trusted key signed is taken without that check',
    "mcp-build": "an MCP server its catalog has no signed image of is built on that host from its source at the commit "
                 "and source archive shown, and added unsigned: nobody signed the image built",
    'mcp-writable-mount': 'a writable mount lets the MCP server change that directory on the host',
    "mcp-lan": "network lan lets the MCP server reach your local network and that host, not only the internet",
    'git-login-add': 'a Git login is used by the connection proxy of every sandbox that reaches that server',
    "login-replace": "a login for that server or kind exists: its token is replaced by this secret's for every later "
                     "start that uses it",
    "mcp-replace": "an MCP server of that name exists: its definition is replaced for every start that uses it",
    "mcp-copy": "an MCP server copied from another host is defined on this one, with the folders, variables and "
                "network shown (a server of that name replaced where asked), and reached by the start it is attached "
                "to",
    'login-remove': 'removing a login or an identity takes it from every later start that uses it',
    'net-keep': "keeping what the run was given writes it into the project's .sandbox-ai.json: every later start of that project gets it",
    'start-restart': 'restarting a running start ends what runs in it now (the agent continues its conversation)',
    'canary-tokens': "the run's canary tokens in full: decoys, but shown here they can be copied and planted",
    'canary-resume': 'resuming a sandbox a canary paused lets it go on after something opened a decoy or sent a token away',
    'llm-insecure': 'plain http to an address outside your local networks is unencrypted: prompts, answers and the key can be read on the way',
    'vpn-socks': 'a SOCKS port in the sandbox lets what goes through it pass the connection proxy by',
    'login-logout': 'logging out forgets the stored login of that agent: its sandboxes need a new login',
    "login-import": "every sandbox of that agent started from now on uses this session's login: the central login is "
                    "replaced (with replace: also one of another account); the session keeps its own copy",
    "login-give": "an agent login given to a running sandbox lets its agent work as that account from now on; the "
                  "two then share the login",
    "login-edit": "a changed login is what every later start that uses it logs in with (a sandbox that runs keeps "
                  "the login it was started with)",
    "secret-rename": "a renamed secret gets its new name in every login, VPN profile, trusted project file and kept "
                     "start that names it",
    "secret-send": "a secret sent to another host is kept in that host's secret store from then on: whoever controls "
                   "that host can use it",
    'kit-hooks-trust': 'trusted hooks of a kit run in every later start with that kit',
    'mcp-detach': 'detaching an MCP server from a running start closes its relay there now: the agent\'s connections to it end and its calls fail (its later starts go without it)',
    "vpn-replace": "replacing a VPN profile of that name changes the tunnel of every start that uses it",
    "llm-replace": "replacing an LLM server of that name changes where every start that uses it sends its prompts "
                   "and its key",
    "llm-share-key": "the key of a server shared with all hosts is kept in the secret store of each host it is sent "
                     "to from then on: whoever controls that host can use it",
    "llm-unshare": "unsharing removes the server's copies (and the keys sent) from every host it was shared with: "
                   "sessions there cannot start with it any more",
    "team-stop": "stopping a team ends what runs in every member and its board",
    "team-delete": "deleting a team removes its members with their HOMEs and its board",
    "image-file": "a host file added to an image's extension is built into the image: every later start of it can "
                  "read it",
    "image-remove": "removing an extension drops what it adds from the next build (a variant's image goes with it, "
                    "unless kept)",
    # sandbox-ai bridge (the web interface's Bridges page since 0.1.98)
    "bridge-add": "a bridge lets one running session reach another (its files, its agent, what it does); data can flow "
                  "from the more restricted one to the less restricted one",
    "bridge-revoke": "revoking ends the bridge at once: both sessions lose it, a call on its way ends",
    "bridge-resume": "resuming lets the sessions reach each other again after a kill switch or a canary alarm "
                     "suspended the bridge",
    # sandbox-ai notify (Settings > Notifications; the web interface's Defaults page)
    "notify-add": "a phone channel sends that host's questions and alarms - the kind, the run's name, a short what and, "
                  "with the detail full, destinations and paths - to the service it names",
    "notify-remove": "removing a phone channel: its questions and alarms no longer reach you there",
    # sandbox-ai schedule (the Sandboxes page's Schedules; the web interface's Starts page)
    "schedule-add": "a schedule starts that agent on that host by itself, on its timetable or trigger, under the "
                    "rules of a normal start; its questions are answered as its ask mode says (off: refused)",
    "schedule-remove": "removing a schedule: it starts nothing any more, and without keep_history its runs' "
                       "reports go too",
    "schedule-timer": "the timer runs sandbox-ai schedule tick every minute as that host's user (a systemd user "
                      "timer or a LaunchAgent): off, no schedule starts by itself",
    "update-mode": "the update mode decides what that host's sandbox-ai does by itself: off checks nothing, notify "
                   "announces a newer signed release, download also fetches and checks it, auto also installs it in "
                   "its window while nothing runs (a timer as that host's user)",
    # sandbox-ai spend raise (the System monitor page; the web interface's too)
    "spend-raise": "raising a budget lets the running start's model requests cost that much more (its refused "
                   "requests go again)",
    # the workspace review (sandbox-ai workspace review RUN --restore PATH) and the MCP tool pins (mcp tools NAME)
    "workspace-restore": "a restore puts the file back as it was at the start of the run: what the agent wrote there "
                         "is replaced (a file it added is removed)",
    "mcp-pin-repin": "re-pinning makes the list the server gave last the one its later lists are compared with: "
                     "what changed in it is no longer asked about",
    "mcp-pin-forget": "forgetting the pin lets the server's next list be pinned without a question (trust on first "
                      "use again)",
}
APP_NEEDS_CONFIRM = {
    # Settings › Updates: sandbox-ai update --apply and --rollback (the web interface has neither)
    "update-apply": "installing the downloaded release changes the sandbox-ai every later start, command and app uses "
                    "(what runs now goes on with its version)",
    "update-rollback": "rolling back switches this installation to the version kept before (what runs now goes on "
                       "with its version)",
    # the Approvals page's Allowed now: Revoke takes back what was allowed during the run, at once
    "net-revoke": "revoking takes back what the running sandbox may reach, at once (open connections to it end)",
    # sandbox-ai session move: no route of the web interface does it
    "session-move": "moving a session stops it here once it runs on the other host (its HOME stays here until it is "
                    "deleted)",
    # sandbox-ai session copy|move --replace: a session of that name on the other host is deleted first
    "session-replace": "replacing deletes the session of that name on the other host first, its HOME there too",
    "preset-delete": "deleting a preset of your own removes it for good (export it first to keep a copy)",
    "preset-replace": "replacing a preset of your own overwrites it with the board shown now (export it first to keep "
                      "a copy)",
    # sandbox-ai llm unload (and a load that replaces what is loaded): no route of the web interface does it
    "llm-unload": "unloading a model takes it from the server's memory: a running sandbox that uses it waits for it to "
                  "load again at its next request (or fails)",
    "files-delete": "deleting removes the files or folders for good (there is no wastebasket)",
    "files-replace": "replacing overwrites what is there now with what is copied or moved",
    # a use case's pair the local-model research marks unsafe for it (use_case_view.PairList): asked, never hidden
    "unsafe-pair": "the research marks this model unsafe for the use case: it followed an instruction planted in the "
                   "input (a command run, .env sent) and may do it again with untrusted input",
    # what the window itself ends, drops or runs (no sandbox-ai command): asked the same way, Cancel the default
    "home-review-skip": "resuming without a clean review of the kept HOME runs whatever was written there (the agent "
                        "too) at the next start",
    "startup-risk": "sandbox-ai's code or the app lies where a sandbox writes: loading it runs what a sandbox may have "
                    "changed",
    "board-discard": "dropping the board's unapplied changes loses them",
    "editor-discard": "closing or reloading the file manager's editor loses the changes not saved to the file",
    "board-remove-part": "the part and its connections go from the board (Undo brings them back)",
    "tab-close": "closing the tab drops the board's unapplied changes",
    "terminal-end": "ending the command of a terminal tab loses what it did not save",
    "login-browser-forget": "forgetting the login browser's data signs you out of every provider there (its cookies, "
                            "storage and cache go)",
    "app-quit": "quitting ends what runs only in the window (its commands and work) and leaves the sandboxes' questions "
                "unanswered",
    "self-update": "updating replaces the sandbox-ai every later start and command uses (a system installation's for "
                   "every user, as root); the previous version is kept for a rollback",
    # sandbox-ai resume NAME OPTIONS, --restore-home and rules reload: no route of the web interface does them yet
    "start-options": "other options at a start's rebirth change what it may reach or do, from now on at every start",
    "home-restore": "restoring the HOME overwrites its changed start files with the text they had at the last start",
    "rules-reload": "reloading the rules makes what you loosened in your rulesets apply to the running sandbox at once",
    # the web interface of a host (sandbox-ai web there): what no web route does
    "web-user-remove": "removing a login of the web interface ends its sessions at their next request",
    "web-user-reset": "a new password and TOTP secret replace the login's: the old ones stop working",
    "web-cert-renew": "a new certificate has another fingerprint: every browser that trusted the old one asks again",
    "web-listen": "the web interface listening beyond the loopback can be reached from other machines (a password and "
                  "a TOTP code are still asked)",
}
MODE_ORDER = ("lax", "low", "medium", "high", "paranoid")  # the order of protection, weakest first (webui.MODE_ORDER)
# The agents' telemetry, the least private first (webui.TELEMETRY_ORDER): always off - there is no on since
# sandbox-ai 0.1.95 (an old "on" counts as off, as the launcher reads it).
TELEMETRY_ORDER = ("off", "strict")
DEFAULT_MODE, DEFAULT_TELEMETRY = "medium", "off"
DEFAULT_CANARIES = ("credentials", "home")  # canary.DEFAULT_CATEGORIES, when a host does not say its canary_default
CANARY_CATEGORIES = ("credentials", "home", "workspace")
LIMIT_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([kmgt]?)b?", re.IGNORECASE)


def needs_confirm(action, detail=None):
    """Why ACTION must be confirmed first (the reason of NEEDS_CONFIRM or APP_NEEDS_CONFIRM), or None when it need not
    be: DETAIL says what it does where that decides it - "limits-raise": {"current": limits_shown() or None, "values":
    {"memory", "cpus", "pids": "8g", "none" or ""}}; "defaults-lower": {"old", "new", "canary_default"} (defaults
    values); "workspace-writable": {"read_only": bool}; "secret-replace": {"exists": bool}; "web-listen": {"beyond":
    bool} (False: the loopback only, nothing to ask). Every other action is
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
    if action == "session-replace" and detail.get("replace") is False:  # a copy without --replace deletes nothing
        return None
    if action == "board-remove-part" and detail.get("ask") is False:  # Settings: remove without asking
        return None
    if action == "web-listen" and detail.get("beyond") is False:
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


DEFAULT_KEEP_DAYS = 90  # without keep_days, sandbox-ai gc and its daily pass remove kept starts unused this long


def kept_starts_expire(days):
    """After how many days unused the kept starts go with keep_days DAYS (a missing or broken one: DEFAULT_KEEP_DAYS),
    or None: never (keep_days 0)."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        return DEFAULT_KEEP_DAYS
    return None if days == 0 else days


def defaults_lowered(old, new, canary_default=None):
    """What of the defaults OLD -> NEW ({key: value} of defaults.json, a key missing: sandbox-ai's own default)
    lowers the protection of every later start: [(key, before, after)] - a weaker mode (MODE_ORDER), telemetry less
    private (TELEMETRY_ORDER), canary categories no longer planted (against that host's own CANARY_DEFAULT), a
    keep_days lowered (kept starts and their HOME go sooner; none is 90 days, 0 never), preset_dirs gaining a
    directory (later starts read presets from it too) and any change of subnet or subnet6 (the network of every later
    start changes): the rules of the web interface's lowered_default. Empty: nothing does."""
    found = []
    for key, order, default in (("mode", MODE_ORDER, DEFAULT_MODE), ("telemetry", TELEMETRY_ORDER, DEFAULT_TELEMETRY)):
        before, after = old.get(key, default), new.get(key, default)
        if key == "telemetry" and before == RETIRED_VALUES["--telemetry"]:
            before = DEFAULT_TELEMETRY  # an old on: off
        if after in order and (before not in order or order.index(after) < order.index(before)):
            found.append((key, before, after))
    lost = canary_categories(old.get("canaries"), canary_default) - canary_categories(new.get("canaries"),
                                                                                       canary_default)
    if lost:
        found.append(("canaries", sorted(lost), new.get("canaries")))
    before, after = kept_starts_expire(old.get("keep_days")), kept_starts_expire(new.get("keep_days"))
    if after is not None and (before is None or after < before):
        found.append(("keep_days", old.get("keep_days"), new.get("keep_days")))
    value = new.get("preset_dirs")
    if value is not None:  # unset: fewer places presets are read from
        kept = set(old.get("preset_dirs")) if isinstance(old.get("preset_dirs"), list) else set()
        added = [item for item in value if item not in kept] if isinstance(value, list) else [value]
        if added:
            found.append(("preset_dirs", sorted(kept), added))
    for key in ("subnet", "subnet6"):  # set, changed or unset: another network for every later start
        if new.get(key) != old.get(key):
            found.append((key, old.get(key), new.get(key)))
    return found


# The commands of the storage check's plan the app runs (sandbox-ai gc --check: starts.start_plan, global_plan):
# gc's own forms only - what another host's answer names is checked here before it is run there.
STORAGE_FLAGS = {"--only", "--caches", "--build-output", "--toolchains", "--images", "--yes"}
STORAGE_DAYS = {"--compact-transcripts", "--transcripts", "--logs-older-than"}


def storage_argv(argv):
    """ARGV (a plan item's "argv") when it is one of the gc commands the storage check names - gc alone, gc --only
    with --caches, --build-output, --toolchains, --images, --compact-transcripts DAYS, --transcripts DAYS,
    --logs-older-than DAYS, --home NAME, --yes; gc --pack --home NAME --yes; gc --unpack NAME -, else ValueError."""
    if not isinstance(argv, (list, tuple)) or not argv or argv[0] != "gc" or len(argv) > 12 or \
            not all(isinstance(part, str) for part in argv):
        raise ValueError("not a command of the storage check")
    rest = list(argv[1:])
    if rest[:1] == ["--unpack"]:
        if len(rest) == 2 and valid_run(rest[1]):
            return ["gc", "--unpack", rest[1]]
        raise ValueError("not a command of the storage check")
    if rest[:1] == ["--pack"]:
        if len(rest) == 4 and rest[1] == "--home" and valid_run(rest[2]) and rest[3] == "--yes":
            return list(argv)
        raise ValueError("not a command of the storage check")
    if rest and rest[0] != "--only":
        raise ValueError("not a command of the storage check")
    index = 0
    while index < len(rest):
        part = rest[index]
        if part in STORAGE_FLAGS:
            index += 1
        elif part in STORAGE_DAYS and index + 1 < len(rest) and re.fullmatch(r"[1-9][0-9]{0,4}", rest[index + 1]):
            index += 2
        elif part == "--home" and index + 1 < len(rest) and valid_run(rest[index + 1]):
            index += 2
        else:
            raise ValueError("not a command of the storage check")
    return list(argv)


class GcChanged(ValueError):
    """What `sandbox-ai gc` would remove is no longer what was shown and confirmed: nothing was removed."""


def gc_plan(result):
    """What a `sandbox-ai gc --json` result removes, comparable: (kept starts, leftovers, histories, the HOMEs whose
    caches are emptied), each sorted. The logs it compresses are not compared: they keep what they hold."""
    result = result if isinstance(result, dict) else {}

    def names(key):
        value = result.get(key)
        return tuple(sorted(str(e.get("name") if isinstance(e, dict) else e) for e in value)) \
            if isinstance(value, list) else ()
    return names("removed"), names("leftovers"), names("histories"), tuple(sorted(set(names("caches"))))


def remote_key(host, ident):
    """The key of the start IDENT (its id, start_id) of another HOST (Instance.key): in a namespace of its own, so it
    never equals a key of this account's own (session:, run:, mcp:, pid:), whatever the host is called. By its id, not
    its name: a start renamed keeps its row, its selection and its tab."""
    return f"host:{host}:{ident}"


def listed_instance(host, entry):
    """A start of HOST as `sandbox-ai list --json` there gives it (ENTRY) as an Instance with HOST set, or None when it
    is not one. The other host (and a sandbox on it) chooses these: only a start's name may come back as an argument
    (never one that reads as an option there)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or \
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", entry["name"]):
        return None
    kind = entry.get("kind") if entry.get("kind") in ("session", "run", "mcp") else "run"
    ident = start_id(entry)
    return Instance(kind, remote_key(host, ident), entry["name"], run=None, project=str(
        entry.get("workspace") or ""), agent=str(entry.get("agent") or ""), started=str(entry.get("started") or ""),
        state=str(entry.get("state") or ""), record=entry, host=host, backend=printable(entry.get("backend"))[:20]
        if isinstance(entry.get("backend"), str) else "", ident=ident)


SEEN_STATES = ("running", "paused", "stopped", "orphaned", "damaged", "starting")


def seen_facts(data):
    """What `sandbox-ai session status NAME --json` (or a start's entry of `list --json`) says, as the app's check
    before a start takes it: each value of the type it is used as, text printable (the other host chooses it)."""
    terminal = data.get("terminal") if data.get("terminal") in ("ready", "starting", "missing", "ended") else None
    state = data.get("state") if data.get("state") in SEEN_STATES else "damaged"
    word = data.get("attach_why") if isinstance(data.get("attach_why"), str) else None  # (one word: starts.attach_why)
    return {"name": printable(str(data.get("name") or ""))[:128], "agent": printable(str(data.get("agent") or ""))[:40],
            "state": "running" if state == "starting" else state, "foreground": bool(data.get("foreground")),
            "terminal": "starting" if state == "starting" and terminal is None else terminal,
            "attachable": bool(data.get("attachable")), "attach_why": printable(word)[:40] if word else None,
            "login": data.get("login") is True, "seen": printable(str(data.get("seen") or ""))[:600]}


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

# macOS: the app starts at login as a LaunchAgent of the user (~/Library/LaunchAgents), not a desktop entry.
LAUNCH_AGENT_LABEL = "org.sandbox-ai.app"


def autostart_file():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "sandbox-ai-app.desktop"


def launch_agent(command):
    """The LaunchAgent (a property list) that starts COMMAND at the user's next login, in the GUI session only. Its
    arguments are a list (ProgramArguments): no shell and no quoting, nothing an argument could break out of."""
    import plistlib
    if any(not isinstance(argument, str) or any(ord(c) < 32 or ord(c) == 127 for c in argument)
           for argument in command):
        raise ValueError("a control character (a new line?) has no place in a login item")
    return plistlib.dumps({"Label": LAUNCH_AGENT_LABEL, "ProgramArguments": list(command), "RunAtLoad": True,
                           "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua"}).decode()


def unload_launch_agent(run=None, environ=None):
    """launchctl bootout of the app's LaunchAgent (macOS), so that removing it takes effect now; nothing when it was
    not loaded (written in this login session: launchd reads it at the next one). Not when this app is the one
    that LaunchAgent started (bootout would end it, and with it the answers to the sandboxes' questions): the
    removed file is enough, it is not started again."""
    if (os.environ if environ is None else environ).get("XPC_SERVICE_NAME") == LAUNCH_AGENT_LABEL:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        (run or subprocess.run)(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)


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


WAIT_FOR_PID = "--wait-for-pid="  # sandbox-ai-app --wait-for-pid=PID: start once PID (the app before) has ended
RESTART_WAIT = 30  # seconds a restarted app waits for the one before to end, at most


def restart_command(pid, directory=None, which=None):
    """argv that starts the app again once this process (PID) has ended - after an update, the new version: the
    installation's stable launcher (installed_launcher, else DATA/current/sandbox-ai-app of the installation whose
    sandbox/ folder is DIRECTORY), else `sandbox-ai-app` on PATH, else this copy as the autostart runs it. It waits for
    PID first (main.py): the single-instance guard is free only once this app has gone."""
    launcher = installed_launcher()
    if not launcher and directory is not None:
        tree = installed_tree(directory)
        current = tree.parent.parent / "current" / "sandbox-ai-app"
        if tree.parent.name == "versions" and current.is_file():
            launcher = str(current)
    launcher = launcher or (which or shutil.which)("sandbox-ai-app")
    command = [launcher] if launcher else [arg for arg in autostart_command() if arg != "--hidden"]
    return [*command, f"{WAIT_FOR_PID}{int(pid)}"]


def wait_for_exit(pid, limit=RESTART_WAIT, sleep=time.sleep, clock=time.monotonic):
    """Wait until process PID has ended, LIMIT seconds at most (a restart: before the new app claims the
    single-instance guard). True when it has ended."""
    end = clock() + limit
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            pass  # there (another user's, or not to be signalled): waited for like the app's own
        if clock() >= end:
            return False
        sleep(0.1)


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
        if sys.platform == "darwin":
            unload_launch_agent()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return None
    if command is None:
        command, paths = autostart_command(), autostart_paths()
    check_outside(list(paths if paths is not None else [a for a in command if os.path.isabs(a)]) + list(also),
                  forbidden)
    if sys.platform == "darwin":
        text = launch_agent(command)
    else:
        text = ("[Desktop Entry]\nType=Application\nName=sandbox-ai\n"
                "Comment=Answers what running sandboxes ask (in the system tray)\n"
                f"Exec={' '.join(desktop_quote(a) for a in command)}\nTerminal=false\n"
                "X-GNOME-Autostart-enabled=true\n")
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

    def __init__(self, process, data=None, watch=None, errors_only=False):
        self.process = process
        self.output = ([], [])  # chunks of stdout and stderr
        self.watch, self.cancelled = watch, False  # WATCH(text): what comes, as it comes (SandboxAI.watching)
        self.watched = {process.stderr.fileno()} if errors_only else {process.stdout.fileno(), process.stderr.fileno()}
        self.decoders = {}
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

    def pump(self, deadline, cancelled=None):
        """Read (and write) until stdout and stderr reach their end - True - or DEADLINE passes or CANCELLED() is
        true - False (self.cancelled says which)."""
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
                if cancelled is not None and cancelled():
                    self.cancelled = True
                    return False
                for key, _ in selector.select(left if cancelled is None else min(left, CANCEL_POLL)):
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
                        if self.watch is not None and key.fd in self.watched:
                            decoder = self.decoders.setdefault(key.fd, codecs.getincrementaldecoder("utf-8")("replace"))
                            text = decoder.decode(chunk)
                            if text:
                                self.watch(text)
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


def mac_processes(proc, info):
    """sandbox-ai's procinfo.py (INFO) where there is no /proc to read (macOS), for PROC the real /proc only (the
    tests hand in a fake one); None elsewhere: /proc is read as always."""
    return info if info is not None and sys.platform == "darwin" and Path(proc) == Path("/proc") else None


def group_alive(pgid, proc=Path("/proc"), info=None):
    """True while a process of group PGID is left that is not a zombie (macOS: INFO, procinfo.py)."""
    mac = mac_processes(proc, info)
    if mac is not None:
        return any(group == pgid and state != "Z" for _, group, state in mac.table().values())
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


def json_for_change(path):
    """The JSON object in PATH ({} without the file) to change and write back: OSError when it cannot be read,
    ValueError when it is no JSON object - never {} then, which written back would replace everything in it by the
    one value changed."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise ValueError(f"{path} is no JSON object (damaged, or being written): not changed")
    return data


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
SETTINGS = {"theme": "system", "ui_font": "", "mono_font": "",
            # a font that comes with the app for all its text, for reading (reading_fonts.READING): "" the theme's
            "reading_font": "", "language": "system", "terminal": "app", "tray": True, "notify": True,
            "notifications": False, "notify_safeguards": True, "short_notices": True,
            "notify_seconds": 15, "notify_style": "themed", "approvals_interval": 1000, "sandboxes_interval": 3000, "connections_interval": 2000,
            "sandbox_ai_home": "", "default_agent": "claude", "default_backend": "podman",
            "default_template": "development", "board_grid": 20, "board_confirm_remove": True, "auto_tabs": True, "reopen_terminals": False, "guide_nodes": True,
            "guide_tour": True, "intro": True, "debug": False, "help_buttons": True,
            # which intro plays (intro_choice.py): "rotate" - a different one at each start - or one of its IDS
            "intro_animation": "rotate",
            # the explanatory texts of the pages and dialogs (widgets.explanation): off, only the (?)s and tooltips
            "explanations": False,
            # the Sessions page's icon buttons (button_icons.py): their texts beside the icons; off: the icons alone,
            # the text in the tooltip
            "button_texts": False,
            "web_without_sandbox": False, "show_flags": False,
            # the terminal tabs: where sign-in links open (app, system), copy what is selected, ask before a paste of
            # several lines, a program's OSC 52 clipboard write (ask, allow, off; a read is never answered)
            "login_links": "app", "copy_on_select": False, "paste_warn": True, "terminal_osc52": "ask",
            # a device sign-in (Codex's one-time code, Hermes' Nous Portal, Qwen OAuth): its page opens by itself in
            # the system browser (system) or in the app's login browser (app); the code is shown either way
            "device_signin": "system"}


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


def trusted_copies_dir():
    """Where the app keeps a copy of every project file version it saw trusted, named by its sha256 (beside the app's
    other settings, outside every workspace)."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "app" / "trusted"


def read_bounded(path, limit):
    """The bytes of the regular file PATH (not through a link), at most LIMIT; ValueError when larger."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
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


def keep_trusted_copy(data):
    """Keep DATA (the bytes of a trusted project file) under its sha256; a failure only loses the diff later."""
    digest = hashlib.sha256(data).hexdigest()
    keep_copy(trusted_copies_dir(), f"{digest}.json", data)


def shown_text(document):
    """How the app shows a project file another host's sandbox-ai read (`wizard trust --show --json`: what takes
    effect, parsed there) and diffs it: indented JSON, the keys in the file's order."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def keep_shown_copy(digest, document):
    """Keep the DOCUMENT another host's sandbox-ai showed for the project file of sha256 DIGEST (trusted there, as
    shown): the version a later change of it on that host is shown against. Its bytes never reach the app, so the copy
    is the document as shown (shown_text); a failure only loses the diff later."""
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) and isinstance(document, dict):
        keep_copy(trusted_copies_dir() / "shown", f"{digest}.json", shown_text(document).encode())


def shown_copy(digest):
    """The text of the project file of sha256 DIGEST as the app last saw it trusted: its own bytes (trusted_copy,
    checked by their sha256) shown as a host shows them, else the document a host showed (keep_shown_copy); None
    without one (or with one that is no JSON object)."""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    data = trusted_copy(digest)
    if data is None:
        try:
            data = read_bounded(trusted_copies_dir() / "shown" / f"{digest}.json", 1 << 20)
        except (OSError, ValueError):
            return None
    try:
        document = json.loads(data.decode())
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return shown_text(document) if isinstance(document, dict) else None


def keep_copy(folder, name, data):
    """DATA as FOLDER/NAME, private, written once (an existing regular file of that name stays)."""
    target = folder / name
    digest = name.split(".", 1)[0]
    try:
        if target.is_file() and not target.is_symlink():
            return
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = folder / f".{digest}.{os.getpid()}-{secrets.token_hex(4)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except OSError:
        pass


def trusted_copy(digest):
    """The kept bytes whose sha256 is DIGEST, checked again (a copy that does not match is no copy), or None."""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    try:
        data = read_bounded(trusted_copies_dir() / f"{digest}.json", 1 << 20)
    except (OSError, ValueError):
        return None
    return data if hashlib.sha256(data).hexdigest() == digest else None


def write_private_json(path, data):
    """Write DATA as JSON to PATH, mode 0600 from the start (never briefly readable by others), atomically and durably:
    the text is made first and written in one go (json.dump wrote it in thousands of small pieces), and it is on the
    disk (fsync) before it replaces the file - a crash or a power loss right after leaves the old file or the new
    one, never an empty or a cut one."""
    write_private_text(path, json.dumps(data, indent=2))


def write_private_text(path, text):
    """TEXT to PATH as write_private_json writes (0600, atomic, synced: the old file or the new one after a crash)."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        with contextlib.suppress(OSError):  # the rename itself durable too
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# The commands of sandbox-ai a fix may be (fixes.COMMANDS): what the app runs of a plan a host answered - never
# anything else that answer names (a host's answer is its text, not this machine's instructions).
FIX_COMMANDS = ("build", "images", "mcp", "toolchain", "identities", "login", "vpn", "plugins", "kits", "hosts", "llm",
                "secrets", "publish-logins")
FIX_VERDICTS = ("offered", "manual", "refused")


def checked_fix_plan(text):
    """`AGENT ... --fix-plan`'s JSON checked: {"fixes": [{id, kind, name, missing, title, commands, command, duration,
    unsigned, security, terminal, needed, verdict, why}], "questions": all|each, "mode"}. Every text one printable
    line of bounded length; a fix whose commands are not all `sandbox-ai COMMAND ...` with a command of FIX_COMMANDS
    is never "offered" here (it is shown as manual, with its commands as text). ValueError when it is no plan."""
    from .files import printable
    try:
        plan = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("not a fix plan") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("fixes"), list):
        raise ValueError("not a fix plan (no list of fixes)")

    def line(value, limit=600):
        return printable(" ".join(str(value if value is not None else "").split()))[:limit]
    fixes = []
    for item in plan["fixes"][:64]:
        if not isinstance(item, dict):
            continue
        commands = [[str(word) for word in command] for command in item.get("commands") or []
                    if isinstance(command, list) and command and all(isinstance(word, str) for word in command)][:8]
        own = bool(commands) and all(command[0] == "sandbox-ai" and len(command) > 1 and command[1] in FIX_COMMANDS
                                     and not any("\0" in word or "\n" in word for word in command)
                                     for command in commands)
        verdict = item.get("verdict") if item.get("verdict") in FIX_VERDICTS else "manual"
        why = line(item.get("why"))
        # A fix that changes the start's own options (add --with claude; fixes.py "options"): launcher options and
        # their values only, each one printable word - applied by the page that starts, never run.
        change = item.get("options") if isinstance(item.get("options"), dict) else None
        if change is not None:
            words = {key: [str(word) for word in change.get(key) or [] if isinstance(word, str)][:16]
                     for key in ("add", "remove")}
            fine = all(word == printable(word) and word.strip() == word and word and len(word) <= 400
                       for key in words for word in words[key]) and \
                all(word.startswith("--") for word in words["remove"]) and \
                (not words["add"] or words["add"][0].startswith("--")) and "--" not in words["add"]
            change = words if fine and (words["add"] or words["remove"]) else None
        if verdict == "offered" and not own and change is None:
            verdict, why = "manual", why or "not a command of sandbox-ai's own"
        fixes.append({"id": line(item.get("id"), 200), "kind": line(item.get("kind"), 40),
                      "name": line(item.get("name"), 200), "missing": line(item.get("missing")),
                      "title": line(item.get("title")), "commands": commands,
                      "command": line(item.get("command"), 2000), "duration": line(item.get("duration"), 80),
                      "unsigned": item.get("unsigned") is True, "security": line(item.get("security")),
                      "terminal": item.get("terminal") is True, "needed": item.get("needed") is not False,
                      "options": change, "choice": line(item.get("choice"), 200), "start": line(item.get("start"), 2000),
                      "verdict": verdict, "why": why})
    return {"fixes": fixes, "questions": plan.get("questions") if plan.get("questions") in ("all", "each") else "all",
            "mode": line(plan.get("mode"), 40)}


def parse_dry_run(status, out, err):
    """(plan or None, message) of `sandbox-ai AGENT ... --dry-run`."""
    if status != 0:
        return None, err.strip() or out.strip()
    try:
        return json.loads(out), err.strip()
    except (ValueError, RecursionError):
        return None, out.strip()


# The launcher's options whose value may name several items, separated by commas and/or whitespace
# (agent_sandbox.LIST_OPTIONS): a field of one of them may list them so ("pypi.org, *.example.com" or one per line),
# and the app passes each item as an option of its own, which every sandbox-ai version (an older host's too) takes.
# Other options' values may hold a comma or a space by meaning (--api-key HOST=NAME=SECRET, --allow-http rules,
# paths): never split.
LIST_OPTIONS = ("--allow", "--allow-host", "--allow-domain", "--allow-shared-wildcard", "--allow-tcp", "--allow-host-port", "--no-inspect",
                "--nameserver", "--vpn-bypass-tcp")


def list_items(text):
    """The items of TEXT (a field of a LIST_OPTIONS option, a value of one): split at commas and whitespace (line
    breaks too), empty ones left out, each once (the first kept), in their order."""
    return list(dict.fromkeys(item for item in re.split(r"[\s,]+", str(text or "")) if item))


def option_items(option, values):
    """VALUES of the list option OPTION (long name) as its items: a LIST_OPTIONS option's split (list_items, each
    once), any other's each stripped as given (empty ones left out)."""
    if option in LIST_OPTIONS:
        return list(dict.fromkeys(item for value in values or [] for item in list_items(value)))
    return [str(value).strip() for value in values or [] if str(value).strip()]


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
        """The command-line tokens for VALUE (bool for a flag, text for a value, [texts] for a list). A value that
        starts with "-" (a goal "- fix it", a folder "-old") is attached, --OPTION=VALUE: given apart, the launcher's
        parser takes it for an option and refuses the start ("expected one argument")."""
        if self.kind == "flag":
            return [self.long] if value else []
        if self.kind in ("list", "multi"):
            return [token for item in option_items(self.long, value) for token in self.pair(item)]
        text = "" if value is None else str(value).strip()
        if not text or (self.kind == "choice" and text == str(self.default)):
            return []
        return self.pair(text)

    def pair(self, text):
        return [f"{self.long}={text}"] if text.startswith("-") else [self.long, text]


# Values the launcher refuses in a project file (agent_sandbox.load_project_config: only the command line or the
# defaults file records the cloud providers' traffic): never written into one.
PROJECT_REFUSED_VALUES = {"--llm-traffic": "all"}
# Values that are gone: the agents' telemetry is always off since sandbox-ai 0.1.95, and an old --telemetry on (in
# the app's state, a board, a preset, a project file) is dropped wherever the app reads it - shown as off, never
# written, never passed (the launcher would ignore it, with a note).
RETIRED_VALUES = {"--telemetry": "on"}
RETIRED_SHORT = {"-tm": "--telemetry"}  # the launcher's short form (agent_sandbox.SHORT_OPTIONS)


def project_refused(option, value):
    """Whether OPTION with VALUE may not be in a project file although the option itself may (--llm-traffic all)."""
    return option in PROJECT_REFUSED_VALUES and str(value).strip() == PROJECT_REFUSED_VALUES[option]


def retired(option, value):
    """Whether OPTION with VALUE is gone (--telemetry on): dropped wherever the app reads it."""
    option = RETIRED_SHORT.get(option, option)
    return option in RETIRED_VALUES and str(value).strip() == RETIRED_VALUES[option]


def without_retired(tokens):
    """Command-line TOKENS without the gone values (RETIRED_VALUES: "--telemetry on", "--telemetry=on", "-tm on")."""
    tokens, kept, index = [str(token) for token in tokens], [], 0
    while index < len(tokens):
        name, equals, value = tokens[index].partition("=")
        if equals and retired(name, value):
            index += 1
        elif not equals and index + 1 < len(tokens) and retired(name, tokens[index + 1]):
            index += 2
        else:
            kept.append(tokens[index])
            index += 1
    return kept


def argv_from_values(options, values):
    """Launcher arguments from {long option: value}, in the order of OPTIONS."""
    argv = []
    for option in options:
        if option.long in values:
            argv += option.tokens(values[option.long])
    return argv


def canonical_of(sai):
    """SAI's canonical_options (remembered per option list), or its launcher's own (a stand-in without it)."""
    return getattr(sai, "canonical_options", None) or sai.launcher.canonical_options


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
        elif option.kind == "choice" and option.const is not None and (
                index + 1 >= len(tokens) or tokens[index + 1].startswith("-")):
            value, index = option.const, index + 1  # an optional value not given (--browser: chromium)
        elif index + 1 < len(tokens):
            value, index = tokens[index + 1], index + 2
        else:
            unknown.append(token)
            index += 1
            continue
        if option.kind in ("list", "multi"):
            given = values.setdefault(name, [])
            # (a list option's items one by one, as the launcher reads them: an older host's canonical_options or a
            # stand-in without one leaves "a,b" whole)
            given += [item for item in option_items(name, [value]) if item not in given] if name in LIST_OPTIONS \
                else [value]
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


CONNECTION_PART = re.compile(r"connections-(.+?)\.jsonl(?:\.(\d{1,3}))?(?:\.gz)?")


def connection_logs(directory):
    """The proxy's logs in DIRECTORY, newest first (as sandbox-ai's logs.connection_logs): each run's
    connections-TIME-RUN.jsonl, the older parts a long run rotated while it ran (NAME.1.gz the newest of them), and
    those sandbox-ai gc compressed once the run ended (NAME.gz)."""
    found = []
    with contextlib.suppress(OSError):
        for path in Path(directory).glob("connections-*.jsonl*"):
            match = CONNECTION_PART.fullmatch(path.name)
            if match:
                found.append(((match.group(1), -int(match.group(2) or 0)), path))
    return [path for _, path in sorted(found, key=lambda item: item[0], reverse=True)]


def compressed_tail(fd, max_bytes):
    """(the newest MAX_BYTES of the compressed log FD, decompressed; the offset they start at): a compressed log is
    read from its start, only its end kept. A damaged one gives what was read up to the damage."""
    held, dropped = bytearray(), 0
    with contextlib.suppress(OSError, EOFError, zlib.error), \
            gzip.GzipFile(fileobj=os.fdopen(os.dup(fd), "rb"), mode="rb") as stream:
        while True:
            chunk = stream.read1(1 << 16)  # (piece by piece: a damaged end loses only its piece)
            if not chunk:
                break
            held += chunk
            if len(held) > 2 * max_bytes + (1 << 20):
                cut = len(held) - max_bytes
                del held[:cut]
                dropped += cut
    if len(held) > max_bytes:
        dropped += len(held) - max_bytes
        del held[:len(held) - max_bytes]
    return bytes(held), dropped


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
        if str(path).endswith(".gz"):  # compressed by sandbox-ai gc once its run ended: never appended to
            data, start = compressed_tail(fd, max_bytes)
        else:
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
            data = b"".join(chunks)
    finally:
        os.close(fd)
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
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            records.append(record)
    if track:
        return records[::-1], (info.st_ino, end), appended
    return records[::-1]


BODY_KEYS = ("text", "base64")
FIND_BODY_BYTES = 256 << 20  # bytes of a log searched for a body a reference names (one part of a run's log)


def body_references(record):
    """[(side, body)] of RECORD's bodies that are references to a body logged before in the same part of the log
    ("same_as": {"connection", "time", "digest"} - sandbox-ai writes a body it logged already that way)."""
    found = []
    for side in ("request", "response"):
        part = record.get(side) if isinstance(record, dict) else None
        body = part.get("body") if isinstance(part, dict) else None
        if isinstance(body, dict) and isinstance(body.get("same_as"), dict) and \
                isinstance(body["same_as"].get("digest"), str):
            found.append((side, body))
    return found


def logged_bodies(record, digests):
    """{digest: (key, value, record)} of RECORD's bodies whose "digest" is in DIGESTS (the body itself: "text" or
    "base64")."""
    found = {}
    for side in ("request", "response"):
        part = record.get(side) if isinstance(record, dict) else None
        body = part.get("body") if isinstance(part, dict) else None
        if isinstance(body, dict) and body.get("digest") in digests:
            key = next((k for k in BODY_KEYS if isinstance(body.get(k), str)), None)
            if key:
                found[body["digest"]] = (key, body[key], record)
    return found


def find_bodies(path, digests, max_bytes=FIND_BODY_BYTES):
    """{digest: (key, value, record)} of the bodies with DIGESTS in the log PATH (a part of a run's connection log,
    compressed or not: where the references of its records point), from its start, at most MAX_BYTES of it."""
    found, wanted = {}, set(digests)
    needles = [f'"digest": "{digest}"'.encode() for digest in wanted]
    try:
        fd = open_log(path)
    except (OSError, TypeError):
        return found
    try:
        stream = gzip.GzipFile(fileobj=os.fdopen(os.dup(fd), "rb"), mode="rb") if str(path).endswith(".gz") \
            else os.fdopen(os.dup(fd), "rb")
        with stream, contextlib.suppress(OSError, EOFError, zlib.error):
            read = 0
            for line in stream:
                read += len(line)
                if read > max_bytes or len(found) == len(wanted):
                    break
                if not any(needle in line for needle in needles):
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                found.update(logged_bodies(record, wanted - set(found)))
    finally:
        os.close(fd)
    return found


def with_bodies(record, records=(), path=None):
    """(RECORD as it is shown, references left): each body logged as a reference ("same_as") with the body it names
    put in - found among RECORDS (the records read, newest last), else in the log PATH -, and a note which connection
    it was logged with. A reference whose body is in neither stays as it is (counted)."""
    references = body_references(record)
    if not references:
        return record, 0
    wanted = {body["same_as"]["digest"] for _, body in references}
    found = {}
    for other in reversed(records):
        found.update(logged_bodies(other, wanted - set(found)))
        if len(found) == len(wanted):
            break
    if path is not None and len(found) < len(wanted):
        found.update(find_bodies(path, wanted - set(found)))
    shown, left = dict(record), 0
    for side, body in references:
        reference = body["same_as"]
        hit = found.get(reference["digest"])
        if hit is None:
            left += 1
            shown[side] = {**shown[side], "body": {**body, "same_as": {**reference, "note": (
                f"the same body as connection {reference.get('connection')} at {reference.get('time')}, logged "
                "once in this part of the log" + (" (not found there)" if path is not None else ""))}}}
            continue
        key, value, origin = hit
        filled = {**body, key: value, "same_as": {**reference, "note": (
            f"logged once, with connection {origin.get('connection', reference.get('connection'))} at "
            f"{origin.get('time', reference.get('time'))}; shown here from there")}}
        shown[side] = {**shown[side], "body": filled}
    return shown, left


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
KNOWN_ARITY = {"--workspace": True, "--state-dir": True, "--workspace-ro": False, "--workspace-name": True}
_arity = weakref.WeakKeyDictionary()  # {launcher: {option string: nargs}} (a launcher that went takes its entry along)


def option_arity(launcher):
    """{option string (long and short): nargs} from the launcher's own parser (0 for a flag, None for one
    value, "?" for an optional one), or {} if it cannot be built."""
    try:
        cached = _arity.get(launcher)
    except TypeError:  # (a test double that takes no weak reference)
        cached = None
    if cached is not None:
        return cached
    try:
        found = {option: action.nargs for action in launcher.build_parser()._actions
                 for option in action.option_strings}
    except Exception as _exc:  # noqa: BLE001 - an old launcher or a test double
        applog.swallowed("backend", "option_arity: an old launcher or a test double", _exc)
        found = {}
    try:
        _arity[launcher] = found
    except TypeError:
        pass
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
    except Exception as _exc:  # noqa: BLE001 - an old or damaged record: read what can be read
        applog.swallowed("backend", "paths_from_options: an old or damaged record: read what can be read", _exc)
    arity = {**KNOWN_ARITY, **option_arity(launcher)}
    result = {"workspaces": [], "state_dir": None, "mounts": [], "workspace_names": {}}
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
        elif name == "--workspace-name":  # NAME=DIR: that workspace is /workspaces/NAME in the sandbox
            label, separator, folder = value.partition("=")
            if separator and label and folder:
                result["workspace_names"][os.path.realpath(path(folder))] = label
        elif name == "--mount":
            source, _, mode = value.rpartition(":")
            source = source.rpartition(":")[0]  # SRC:DEST:MODE
            if mode == "rw" and source:
                result["mounts"].append(path(source))
    return result


SANDBOX_HOME = "/root"  # the agent's HOME in the sandbox (Podman and Bubblewrap alike: agent_sandbox.py)


def sandbox_targets(workspaces, names=None):
    """[(host path, path in the sandbox)] of WORKSPACES (in the order given to the launcher), as agent_sandbox's
    workspace_targets mounts them: the first at /work, one named by --workspace-name (NAMES {resolved host path: NAME})
    at /workspaces/NAME, the others at /workspaces/ and their folder's name (-2, -3 ... after the names taken)."""
    names = dict(names or {})
    workspaces = [os.path.realpath(os.path.expanduser(str(w))) for w in workspaces if w]
    if not workspaces:
        return []
    targets, used = [(workspaces[0], "/work")], set(names.values())
    for value in workspaces[1:]:
        named = names.get(value)
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


def launcher_agents(launcher):
    try:
        return tuple(launcher.AUTH_TARGETS)
    except AttributeError:
        return AGENTS


def run_paths(launcher, pid, proc=Path("/proc"), info=None):
    """Workspaces, --state-dir and rw --mount sources of a running launcher, from its command line in /proc (only for a
    process of this user; macOS: INFO, procinfo.py); empty when that cannot be read."""
    empty = {"workspaces": [], "state_dir": None, "mounts": [], "workspace_names": {}}
    mac = mac_processes(proc, info)
    try:
        pid = int(pid)
        if mac is not None:
            if mac.owner(pid) != os.getuid() or mac.cmdline(pid) is None or mac.cwd(pid) is None:
                return empty
            argv = [a.decode("utf-8", "surrogateescape") for a in mac.cmdline(pid) if a]
            cwd = mac.cwd(pid)
        else:
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
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
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


def process_started(pid, proc=Path("/proc"), info=None):
    """When process PID started, as sandbox-ai writes times (UTC), or None (macOS: INFO, procinfo.py)."""
    mac = mac_processes(proc, info)
    if mac is not None:
        try:
            moment = mac.started_epoch(int(pid))
        except (ValueError, TypeError):
            moment = None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)) if moment is not None else None
    try:
        ticks = int((proc / str(int(pid)) / "stat").read_text().rpartition(")")[2].split()[19])
        boot = next(int(line.split()[1]) for line in (proc / "stat").read_text().splitlines() if line.startswith("btime "))
        moment = boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, TypeError):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def launcher_processes(agents=AGENTS, proc=Path("/proc"), info=None):
    """[(pid, agent, launcher options, cwd)] of sandbox-ai launchers of this user (python ... agent_sandbox.py
    AGENT ...), dry runs left out (they start nothing). Read from /proc (macOS: INFO, procinfo.py); processes of
    others are skipped."""
    found = []
    mac = mac_processes(proc, info)
    try:
        entries = [str(pid) for pid in mac.pids()] if mac is not None else \
            [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError:
        return found
    for entry in entries:
        directory = proc / entry
        try:
            if mac is not None:
                arguments = mac.cmdline(int(entry)) if mac.owner(int(entry)) == os.getuid() else None
                if arguments is None:
                    continue
                argv = [a.decode("utf-8", "surrogateescape") for a in arguments if a]
            else:
                if directory.stat().st_uid != os.getuid():
                    continue
                argv = [a.decode("utf-8", "surrogateescape") for a in (directory / "cmdline").read_bytes().split(b"\0")
                        if a]
        except OSError:
            continue
        for index, token in enumerate(argv[:-1]):
            if token.endswith("agent_sandbox.py") and argv[index + 1] in agents:
                options = argv[index + 2:]
                if "--dry-run" in options[:options.index("--")] if "--" in options else "--dry-run" in options:
                    break
                try:
                    cwd = (mac.cwd(int(entry)) or "") if mac is not None else os.readlink(directory / "cwd")
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


def mcp_option_names(options):
    """The MCP servers OPTIONS (a start's saved options) name: --mcp NAME, --mcp=NAME, -M NAME (starts.mcp_names)."""
    found = []
    options = options if isinstance(options, list) else []
    for index, token in enumerate(options):
        if token in ("--mcp", "-M") and index + 1 < len(options) and isinstance(options[index + 1], str):
            found.append(options[index + 1])
        elif isinstance(token, str) and token.startswith("--mcp="):
            found.append(token.partition("=")[2])
    return [name for name in dict.fromkeys(found) if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name)]


def run_state(record):
    """"paused" for a run that `sandbox-ai pause` froze (the marker in its run directory, as starts.py reads it),
    else "running" (review 6 A-N1: every run read as running, so Resume was never offered for one)."""
    directory = record.get("directory") if isinstance(record, dict) else None
    if isinstance(directory, str) and directory:
        try:
            if (Path(directory) / "paused").exists():
                return "paused"
        except OSError:
            pass
    return "running"


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


def json_part(out):
    """The JSON document in OUT, the output of a `sandbox-ai ... --json`: all of it, or - when the command said notes
    on standard output before it (`llm list --json` names an unusable server in a line of its own) - from the first
    line that begins with "[" or "{" on (anything after the document is left out). ValueError: no JSON in it."""
    text = out or ""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        pass
    decoder, offset = json.JSONDecoder(), 0
    for line in text.splitlines(keepends=True):
        if line.lstrip()[:1] in ("[", "{"):
            try:
                return decoder.raw_decode(text, offset + len(line) - len(line.lstrip()))[0]
            except ValueError:
                pass
        offset += len(line)
    raise ValueError("no JSON in the answer")


def session_of(record):
    """The session NAME of a run record, or None. A session's sandbox registers with its pod name
    (agent-session-NAME, see sessions.launch); `--session` runs carry the name itself."""
    value = record.get("session")
    if not isinstance(value, str) or not value:
        return None
    return value[len("agent-session-"):] if value.startswith("agent-session-") else value


# -- approvals --

class QuestionCache:
    """Questions of the run directories, parsed once per file; a run's folder whose listing did not change since a
    scan (its modification time, older than QUIET seconds then) is not read again: the scan every second of a run
    with a long history of answered questions costs a stat, not hundreds of lookups."""

    QUIET = 2.0  # seconds a folder's modification time must lie back before its listing is reused (coarse clocks)

    # Bytes of a question file read: a tool call's question carries its arguments in full (64 KiB of UTF-8, escaped
    # once more in the file: up to three times that), as sandbox-ai's supervisor reads it (1 MiB).
    def __init__(self, ident=re.compile(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}"), limit=1 << 20):
        self.ident, self.limit, self.parsed, self.results = ident, limit, {}, {}
        self.listings = {}  # {folder: (its stat key, [(kind, question, result, paths)])} of folders that were quiet

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

    def _entries(self, directory):
        """[(kind, question, result, paths)] of the folder DIRECTORY ("open" or "answered"; RESULT of an answered
        one), and whether all of it could be read (a file in the middle of being written: no); None: no folder."""
        try:
            with os.scandir(directory) as entries:
                names = {entry.name for entry in entries}
        except OSError:
            return None, False
        found, complete = [], True
        for file in sorted(names):
            stem, dot, suffix = file.rpartition(".")
            if suffix != "json" or not self.ident.fullmatch(stem):
                continue
            path = directory / file
            paths = [str(path)]
            question = self._json(path, self.parsed)
            if question is None and str(path) not in self.parsed:
                complete = False
            if question is None or question.get("id") != stem:
                found.append((None, None, None, paths))
                continue
            if f"{stem}.done" in names:
                done = directory / f"{stem}.done"
                result = self._json(done, self.results)
                if result is None and str(done) not in self.results:
                    complete = False
                found.append(("answered", question, result or {}, paths))
            elif f"{stem}.answer" in names:
                found.append(("answered", question, {"reason": "answered, being applied"}, paths))
            elif f"{stem}.state" in names:
                # The supervisor's word on it (0.1.98): "timeout" - nobody answered in time, it stays answerable -
                # or "open" again (a later try waits for it, counting from then). Read anew: it changes.
                state = read_state(directory / f"{stem}.state")
                if state.get("state") == "timeout":
                    found.append(("open", {**question, "state": "timeout", "timed_out": state.get("time")}, None,
                                  paths))
                elif state.get("state") == "open" and isinstance(state.get("time"), str):
                    found.append(("open", {**question, "state": "open", "asked_first": question.get("time"),
                                           "time": state["time"]}, None, paths))
                else:
                    found.append(("open", question, None, paths))
            else:
                found.append(("open", question, None, paths))
        return found, complete

    def scan(self, runs):
        pending, answered, live, listings = [], [], set(), {}
        now = time.time()
        for name, record in runs.items():
            directory = Path(record.get("directory", "")) / "approvals"
            try:
                stat = os.stat(directory)
            except OSError:
                continue
            key = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
            kept = self.listings.get(str(directory))
            if kept is not None and kept[0] == key:
                entries = kept[1]
            else:
                entries, complete = self._entries(directory)
                if entries is None:
                    continue
                if not complete or now - stat.st_mtime < self.QUIET:
                    key = None  # read again next time
            if key is not None:
                listings[str(directory)] = (key, entries)
            for kind, question, result, paths in entries:
                live.update(paths)
                if kind == "answered":
                    answered.append((name, record, {**question, "result": result}))
                elif kind == "open":
                    pending.append((name, record, question))
        self.listings = listings
        for store in (self.parsed, self.results):  # forget the files of ended runs
            for key in [k for k in store if k.rsplit(".", 1)[0] + ".json" not in live]:
                del store[key]
        return pending, answered


def read_state(path):
    """{"state": "open"|"timeout", "time", ...} of a question's .state file (the supervisor's), or {}."""
    try:
        with open(path, "rb") as stream:
            value = json.loads(stream.read(4096))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


QUESTION_STATES = ("open", "timeout", "allowed", "refused")  # sandbox-ai's approvals.STATES
ENTRY_ACTIONS = ("allow", "deny", "deny-once", "block", "ask-again")  # approvals.entry_actions


def question_state(question):
    """open, timeout, allowed or refused: what QUESTION says of itself (sandbox-ai 0.1.98 on: "state"), or what its
    answer says ("result"; an older sandbox's refusal of one nobody answered counts as timed out)."""
    if question.get("state") in QUESTION_STATES:
        return question["state"]
    result = question.get("result")
    if not isinstance(result, dict):
        return "open"
    if result.get("decision") == "allow":
        return "allowed"
    return "timeout" if str(result.get("reason") or "").startswith(("no answer", "not answered yet")) else "refused"


def answerable(question):
    """QUESTION still waits for its answer (open, or timed out and still open) - answered as a question."""
    return question_state(question) in ("open", "timeout") and not isinstance(question.get("result"), dict) and \
        question.get("answerable") is not False


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
        # Its state (sandbox-ai 0.1.98 on: list --state all), what the run decided and what may be done about it.
        if item.get("state") in QUESTION_STATES:
            self["state"] = item["state"]
        if isinstance(item.get("timed_out"), str):
            self["timed_out"] = printable(item["timed_out"])[:40]
        if item.get("answerable") is False:
            self["answerable"] = False
        if item.get("asked_again") is True:
            self["asked_again"] = True
        if isinstance(item.get("actions"), list):
            self["actions"] = [a for a in item["actions"] if a in ENTRY_ACTIONS]
        result = item.get("result")
        if isinstance(result, dict):
            self["result"] = {k: printable(str(result[k]))[:300] for k in ("decision", "scope", "reason", "changed")
                              if isinstance(result.get(k), str)}
        # A tool call's arguments in full (approvals.question_details there): lines kept, escaped here too, bounded.
        if isinstance(item.get("details"), str) and item["details"]:
            self["details"] = bounded_details(item["details"])
            self["details_whole"] = item.get("details_whole") is True
        # A login's callback to the sandbox's loopback (sandbox-ai's oauth_relay.fields): this machine's catcher
        # relays it when the page opens here (host_answer_relaying). Kept only in its checked shape.
        callback = item.get("callback")
        if self.get("kind") == "browser" and self.get("url") and isinstance(callback, dict) and \
                isinstance(callback.get("port"), int) and not isinstance(callback.get("port"), bool) and \
                1 <= callback["port"] <= 65535 and isinstance(callback.get("url"), str) and \
                isinstance(callback.get("path"), str):
            self["callback"] = {"url": printable(callback["url"])[:1100], "port": callback["port"],
                                "path": printable(callback["path"])[:1000]}
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


DETAILS_LIMIT = (64 << 10) + 512  # characters of a question's details shown (sandbox-ai's approvals.DETAILS_LIMIT)
# Questions whose details carry a call's arguments in full (approvals.DETAILS_KINDS; a write across a session bridge:
# its diff).
DETAILS_KINDS = ("mcp", "tool", "bridge", "mcp-tools")


def bounded_details(text, limit=DETAILS_LIMIT):
    """TEXT of a question's details for the screen: its lines kept, every other control character escaped
    (printable_text), at most LIMIT characters, with a line that says how many more there were."""
    text = str(text)
    more = len(text) - limit
    return printable_text(text[:limit]) + (f"\n… {more} more characters not shown" if more > 0 else "")


def arguments_whole(question):
    """QUESTION's line shows its tool call's arguments whole (as sandbox-ai's approvals.arguments_whole): no details,
    or a short form of at most the 300 characters the line shows that is the same JSON as the details."""
    full = question.get("arguments_full")
    if question.get("kind") not in DETAILS_KINDS or not isinstance(full, str):
        return True
    short = question.get("arguments")
    if not isinstance(short, str) or len(printable(short)) > 300:
        return False
    try:
        return json.loads(short) == json.loads(full)
    except (ValueError, RecursionError):
        return False


def question_details(question):
    """(details, whole) of QUESTION: a tool call's arguments in full, as the call will send them (pretty-printed JSON,
    credentials redacted: sandbox-ai's runtime.full_arguments), and whether its line shows them whole too; ("", True)
    for a question without them (another kind, an older sandbox-ai). Another host's come as its sandbox-ai said them
    (RemoteQuestion: "details", escaped there and here); this machine's from the question file, escaped here."""
    if isinstance(question, RemoteQuestion):
        return question.get("details", ""), question.get("details_whole", True)
    full = question.get("arguments_full") if question.get("kind") in DETAILS_KINDS else None
    return (bounded_details(full), arguments_whole(question)) if isinstance(full, str) and full else ("", True)


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
    return _parse_time(text) if isinstance(text, str) else _parse_time(str(text)) if text is not None else None


@functools.lru_cache(maxsize=4096)
def _parse_time(text):
    """parse_time of a string, kept: the Sandboxes tree turns the same start times into its rows at every look (UTC,
    so the same text is always the same moment; strptime cost ~15 us each)."""
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
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

UPDATE_MODES = ("off", "notify", "download", "auto")  # sandbox/updates.py MODES
UPDATE_BUSY = 3  # updates.BUSY_STATUS: --apply or --rollback refused while something runs


def update_busy_reasons(status, err):
    """What runs, as `sandbox-ai update --apply` or `--rollback` named it when it refused (STATUS 3), else []."""
    if status != UPDATE_BUSY:
        return []
    found = re.search(r"in use: (.*?) \(stop them first", err or "", re.S)
    return [part.strip() for part in found[1].split("; ") if part.strip()][:50] if found else []


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


def update_check_signature(out):
    """What `sandbox-ai update --check` said of the newest release's signature (its line "signature: ..."): "good"
    (by a trusted key), "bad", "none" (no SHA256SUMS.sig), "unchecked" (no key is trusted), or None (no such line)."""
    found = re.search(r"^signature:\s*(.*)$", out or "", re.M)
    if not found:
        return None
    said = found[1]
    return "good" if said.startswith("good") else "bad" if said.startswith("BAD") else \
        "none" if said.startswith("none") else "unchecked"


def update_check_error(status, out, err):
    """Why `sandbox-ai update --check` found nothing (offline, GitHub's rate limit, ...): its last line, without the
    "sandbox-ai update: " or "install.sh: " in front; None when it worked."""
    if status == 0 and parse_update_check(status, out) is not None:
        return None
    lines = [line.strip() for line in (err or "").splitlines() if line.strip()] or \
        [line.strip() for line in (out or "").splitlines() if line.strip() and
         not re.match(r"(installed|available):", line.strip())]
    why = lines[-1] if lines else ""
    why = re.sub(r"^(sandbox-ai update|install\.sh):\s*(error:\s*)?", "", why)
    return why or f"exit status {status}"


UPDATE_PACKAGES = ("pyz", "appimage", "macos-app")  # sandbox/main.py's PACKAGES: updated by a new download
SYSTEM_HOMES = ("/home/", "/root/", "/Users/", "/var/home/")  # sandbox/main.py's HOMES: never a system installation


def installed_tree(directory):
    """The tree whose sandbox/ folder is DIRECTORY, with install.sh's `current` link followed: the app finds an
    installation through DATA/current (kept, so that an update is picked up at the next start), and what kind of
    installation it is - DATA/versions/X below a prefix's lib/sandbox-ai, or in a home - shows only behind the
    link."""
    tree = Path(directory).parent
    if tree.name == "current" and tree.is_symlink():
        with contextlib.suppress(OSError, RuntimeError):
            real = tree.resolve()
            if real.parent.name == "versions" and real.parent.parent == tree.parent:
                return real
    return tree


_PACKAGE_UPDATERS = {}  # {sandbox/ folder: its package_update.py, or None (a sandbox-ai before it)}


def package_updater(directory):
    """sandbox-ai's package_update.py of the copy whose sandbox/ folder is DIRECTORY (the copies that replace
    themselves: the .pyz, the AppImage, the macOS app, a pip installation), loaded once; None for a sandbox-ai that has
    none yet."""
    key = str(directory)
    if key not in _PACKAGE_UPDATERS:
        module, path = None, Path(directory) / "package_update.py"
        if path.is_file():
            try:
                spec = importlib.util.spec_from_file_location("sandbox_ai_app_package_update", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:  # noqa: BLE001 - unreadable: as a copy without it
                applog.swallowed("backend", "package_updater: package_update.py could not be loaded", exc)
                module = None
        _PACKAGE_UPDATERS[key] = module
    return _PACKAGE_UPDATERS[key]


def self_update_target(directory, kind, environ=None, access=None):
    """(PATH, None) when `sandbox-ai update` replaces the package KIND ("pyz", "appimage", "macos-app") or the pip
    installation ("pip") whose sandbox/ folder is DIRECTORY - PATH: the file or folder it replaces -, else (None, WHY):
    why not, with the command that updates it (sandbox-ai's package_update.target; an older sandbox-ai: no reason)."""
    updater = package_updater(directory)
    if updater is None:
        return None, None
    info = {"kind": "pip" if kind == "pip" else "package", "package": None if kind == "pip" else kind,
            "tree": str(Path(directory).parent), "data": None, "prefix": None}
    try:
        return updater.target(info, environ, **({"access": access} if access else {}))
    except Exception as exc:  # noqa: BLE001 - never an error of the status line
        applog.swallowed("backend", "self_update_target", exc)
        return None, None


def update_route(directory, euid=None, writable=None, environ=None, access=None):
    """How `sandbox-ai update` updates the copy of sandbox-ai whose sandbox/ folder is DIRECTORY, decided as sandbox/main.py
    decides it (its `update` and installation_mode()): (ROUTE, PATH) with ROUTE
    - "user": install.sh's installation that this user may update (their own, or a system one they can write);
    - "root": a system installation (PREFIX/lib/sandbox-ai, installed by root for every user) whose files this user
      cannot write: only root updates it (sudo sandbox-ai update); PATH is its folder;
    - "self": the AppImage, the macOS app, the .pyz or a pip installation that replaces itself with its file of the
      release (PATH: the file or folder it replaces): sandbox-ai update, as "user";
    - "package": the AppImage, the macOS app or the .pyz (PATH: the kind) that cannot (not writable, unpacked, a
      sandbox-ai before that): update_blocker says the command that updates it;
    - "checkout": a git checkout (git pull); "other": a pip installation that cannot replace itself.
    Only files are looked at (no process): quick enough for the GUI thread."""
    tree = installed_tree(directory)
    euid = os.geteuid() if euid is None else euid
    writable = writable or (lambda path: os.access(path, os.W_OK))
    try:
        kind = (tree / "PACKAGING").read_text().strip()
    except (OSError, UnicodeDecodeError):
        kind = None
    if kind in UPDATE_PACKAGES:
        path, _ = self_update_target(directory, kind, environ, access)
        return ("self", str(path)) if path is not None else ("package", kind)
    if (tree / ".git").exists():
        return "checkout", str(tree)
    if not (tree / "install.sh").is_file():
        path, _ = self_update_target(directory, "pip", environ, access)
        return ("self", str(path)) if path is not None else ("other", str(tree))
    data = tree.parent.parent
    if tree.parent.name == "versions" and data.name == "sandbox-ai" and data.parent.name == "lib" \
            and not str(data).startswith(SYSTEM_HOMES) and euid != 0 and not writable(data):
        return "root", str(data)
    return "user", str(data if tree.parent.name == "versions" else tree)


def update_blocker(directory, environ=None, access=None):
    """Why the package or the pip installation whose sandbox/ folder is DIRECTORY does not replace itself, with the
    command that updates it instead (sandbox-ai's own words), or None (it does, or it is another kind of copy)."""
    tree = Path(directory).parent
    try:
        kind = (tree / "PACKAGING").read_text().strip()
    except (OSError, UnicodeDecodeError):
        kind = None
    if kind not in UPDATE_PACKAGES:
        if (tree / ".git").exists() or (tree / "install.sh").is_file():
            return None
        kind = "pip"
    path, why = self_update_target(directory, kind, environ, access)
    return why if path is None else None


class NoSudo(ValueError):
    """A system installation to update, and no sudo on PATH: the message says how to run it as root."""


def self_update_command(sai, route, which=None):
    """argv of the update of this machine's sandbox-ai for ROUTE (update_route), to run in a terminal where the human
    sees it: `sandbox-ai update` with the app's Python; for "root" `sudo sandbox-ai update` - this installation's own
    `sandbox-ai` command (root's files, run with root's python3; never the app's Python, which this user may write)
    after sudo from PATH, which asks for the password in that terminal (the app never sees it). NoSudo without sudo
    (its text: what to run as root instead); ValueError for a route that `sandbox-ai update` does not update."""
    if route in ("user", "self"):  # ("self": a package or a pip installation that replaces itself)
        return sai.command("update")
    if route != "root":
        raise ValueError(f"sandbox-ai update does not update this copy ({route})")
    launcher = Path(sai.directory).parent / "sandbox-ai"
    command = [str(launcher)] if launcher.is_file() and os.access(launcher, os.X_OK) else sai.command()
    sudo = (which or shutil.which)("sudo")
    if not sudo:
        raise NoSudo(shlex.join([*command, "update"]))
    return [sudo, "--", *command, "update"]


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


# == the start/host agent's section (module level): the words of commands, checked as the web interface checks them ==

MCP_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"  # webui.MCP_NAME: a server's name for --mcp
MCP_ENTRY = r"(?:[a-z0-9][a-z0-9-]{0,31}:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"  # webui.MCP_ENTRY: duckduckgo, hub:binwalk
MCP_CATALOG_LINE = re.compile(r"(?P<prefix>[^:\s]+):(?P<name>\S+)  \[(?P<network>[^\]]*)\]  (?P<title>.*?)"
                              r"(?P<unpublished>  \(not published yet\))?  (?P<signature>signed|SIGNATURE BAD|unsigned)")
VPN_PROFILE = r"[a-z0-9][a-z0-9_-]{0,31}"  # webui.VPN_NAME
VARIABLE = r"[A-Za-z_][A-Za-z0-9_]{0,63}"  # a variable of your environment (mcp add --env)
SIZE = r"[0-9]{1,12}(?:\.[0-9]{1,6})?[kmgtKMGT]?"  # webui.SIZE_VALUE: 512m, 2g
VERSION = r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}"  # X.Y.Z (update --version)
PROJECT_RULES = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"  # privacy-check --project: a rule file's name
DEMO_ACTIONS = ("start", "status", "stop")


def help_options(text):
    """The long options of an argparse --help TEXT: the option strings at the start of its option lines (aliases
    included), not the ones its help texts mention."""
    found = set()
    for line in str(text).splitlines():
        if not re.match(r"\s{1,6}-", line):  # an option line; a help text's continuation is indented further
            continue
        head = line.strip().split("  ")[0]  # "-ps, --preset NAME": up to the gap before the help text
        found |= set(re.findall(r"(?<![\w-])--[a-z0-9][a-z0-9-]*", head))
        # A choice's values ("--ask {on,off,allow}") as OPTION=VALUE: a value newer versions brought is found out
        # (--ask allow came with sandbox-ai 0.1.80; choice_unknown).
        for name, values in re.findall(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*) \{([^{}\s]*)\}", head):
            found |= {f"{name}={value}" for value in values.split(",") if value}
    return found


def choice_unknown(option, value, known):
    """True when VALUE of the choice OPTION is one a host's launcher (its options KNOWN, help_options) lists no
    "OPTION=VALUE" for while it lists other values of it: a value it is too old for (--ask allow before 0.1.80)."""
    if option is None or option.kind != "choice" or value in (None, "") or str(value) == str(option.default):
        return False
    listed = {name for name in known if name.startswith(f"{option.long}=")}
    return bool(listed) and f"{option.long}={value}" not in listed


def argv_for_host(options, values, known=None):
    """Launcher arguments of VALUES (argv_from_values) without the options a host's launcher does not know (KNOWN:
    host_launch_options; None: not known, all of them)."""
    return argv_from_values([o for o in options if known is None or o.long in known], values)


def dropped_options(options, values, known=None):
    """The options of VALUES that are set (they give command-line tokens) but that a host's launcher does not know
    (KNOWN; None: not known, none): what argv_for_host would leave out. A start refuses them by name instead - a
    restrictive one (--workspace-ro, --no-provider-defaults) left out would start a weaker sandbox without a word."""
    if known is None:
        return []
    return [o.long for o in options if o.long not in known and o.long in values and o.tokens(values[o.long])] + \
        [f"{o.long} {values[o.long]}" for o in options if o.long in known and o.long in values
         and choice_unknown(o, values[o.long], known)]


def unknown_options(options, argv, known):
    """The options of ARGV (up to a "--") a host's launcher does not know (KNOWN), once each, in order; the values of
    this launcher's OPTIONS are skipped."""
    by_name = {option.long: option for option in options}
    found, index, tokens = [], 0, list(argv)
    while index < len(tokens):
        token = str(tokens[index])
        if token == "--":
            break
        name, equals, value = token.partition("=")
        index += 1
        if not name.startswith("--"):
            continue
        if name not in known and name not in found:
            found.append(name)
        option = by_name.get(name)
        if option is not None and option.kind != "flag" and not equals:
            value = str(tokens[index]) if index < len(tokens) else ""
            index += 1  # its value
        if name in known and choice_unknown(option, value, known) and f"{name} {value}" not in found:
            found.append(f"{name} {value}")  # a value it is too old for (--ask allow)
    return found


def path_value(value, what):
    """An absolute path of one line on whichever host it is for (never read as an option); ValueError otherwise."""
    value = str(value or "").strip()
    if not valid_directory(value) or any(part in (".", "..") for part in value.split("/")):
        raise ValueError(f"{what}: an absolute path (without . or ..)")
    return value


def resume_args(name, restart=False, workspaces=(), mounts=(), mcp=(), options=(), dropped=()):
    """The words after `sandbox-ai` of `resume [--workspace=DIR]... [--mount=SOURCE:DESTINATION:MODE]...
    [--mcp=NAME]... [--drop-option=OPTION]... [--restart] NAME [OPTIONS]` (each kept for the start's later starts),
    checked: DIRs and the mounts' paths absolute (on the start's host), MOUNTS [(source, destination, "ro"|"rw")]
    without ':' in them, MCP servers by name; OPTIONS (launcher options, after NAME: sandbox-ai checks them as a new
    start) begin with an option, DROPPED are option names (with =VALUE). ValueError says what is wrong."""
    if not valid_run(name):
        raise ValueError("not a start's name")
    args = ["resume"]
    for directory in workspaces:
        directory = str(directory or "").strip()
        args.append(f"--workspace={directory if valid_remote(directory) else path_value(directory, 'a workspace')}")
    for source, destination, mode in mounts:
        # (a folder of another machine, sshfs://[USER@]HOST[:PORT]/PATH, as sandbox-ai resume --mount takes it)
        source = str(source or "").strip()
        remote = valid_remote(source)
        source = source if remote else path_value(source, "a mount's source")
        destination = path_value(destination, "a mount's destination")
        if mode not in ("ro", "rw") or ":" in (destination if remote else source + destination):
            raise ValueError("a mount is a folder and where the sandbox sees it, read-only or writable (no ':' in "
                             "the paths)")
        args.append(f"--mount={source}:{destination}:{mode}")
    for server in mcp:
        if not re.fullmatch(MCP_NAME, str(server)):
            raise ValueError("an MCP server's name is 1-32 letters, digits, '_' or '-'")
        args.append(f"--mcp={server}")
    for value in dropped:
        if not re.fullmatch(r"-{0,2}[a-z][a-z0-9-]{0,40}(=[^\n\x00]{1,500})?", str(value)):
            raise ValueError("an option to take out is its name, with =VALUE for one of its values")
        args.append(f"--drop-option={value}")
    options = [str(word) for word in options]
    if options and not options[0].startswith("--"):
        raise ValueError("the other options begin with an option (--gui, --cpus 4 …)")
    if any("\n" in word or "\x00" in word or len(word) > 1000 for word in options) or len(options) > 64:
        raise ValueError("these options cannot be passed on")
    if len(args) > 1 + 48:
        raise ValueError("too many workspaces, mounts and servers at once")
    return args + (["--restart"] if restart else []) + [name] + options


SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")  # the launcher's SESSION_NAME (--session, --as)
REMOTE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # a host of sandbox-ai hosts (host_choice.HOST)
TRANSFER_MAPS = 64  # --map options one copy takes at most


BUNDLE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")  # a board's name (sandbox-ai board: board_bundle.NAME)
BUNDLE_KINDS = ("mcp", "llm", "kit", "variant", "preset", "image", "board")


def board_export_args(out, session=None, board=False, host="local", images=False):
    """The words after `sandbox-ai` of `board export (--session NAME | --board -) OUT [--images] [--host HOST] --json`,
    checked: OUT an absolute path of this machine (the bundle is written here, also of a host's session), SESSION a
    session's name, BOARD the board document given on standard input. ValueError says what is wrong."""
    if not isinstance(out, str) or not os.path.isabs(out) or any(c in out for c in "\n\r\0"):
        raise ValueError("choose where the bundle is written (a file of this machine)")
    if bool(session) == bool(board):
        raise ValueError("a session or a board")
    if session and (not isinstance(session, str) or not SESSION_NAME.fullmatch(session)):
        raise ValueError("not a session's name")
    if host != "local" and (not isinstance(host, str) or not REMOTE_HOST.fullmatch(host)):
        raise ValueError("not a host of sandbox-ai hosts")
    return ["board", "export", f"--session={session}" if session else "--board=-", "--json",
            *(["--images"] if images else []), *([f"--host={host}"] if host != "local" else []), "--", out]


def board_import_args(path, host="local", as_name=None, maps=(), values=(), renames=(), replaces=(), images=False,
                      check=True, unsigned=False):
    """The words after `sandbox-ai` of `board import PATH --json (--check | --yes) [--as NAME] [--map=OLD=NEW]...
    [--value=NAME=VALUE]... [--rename=KIND:OLD=NEW]... [--replace=KIND:NAME]... [--images] [--host HOST]`, checked:
    PATH a bundle of this machine (also when it is imported on a host). ValueError says what is wrong."""
    if not isinstance(path, str) or not os.path.isabs(path) or any(c in path for c in "\n\r\0"):
        raise ValueError("choose the bundle (a file of this machine)")
    if host != "local" and (not isinstance(host, str) or not REMOTE_HOST.fullmatch(host)):
        raise ValueError("not a host of sandbox-ai hosts")
    if as_name not in (None, "") and (not isinstance(as_name, str) or not BUNDLE_NAME.fullmatch(as_name)):
        raise ValueError("a board's name is 1-40 lower-case letters, digits, '_' and '-'")
    words = ["board", "import", "--json", "--check" if check else "--yes"]
    words += [f"--as={as_name}"] if as_name else []
    for old, new in maps:
        if not all(isinstance(v, str) and v and not any(c in v for c in "\n\r\0") for v in (old, new)) or \
                not os.path.isabs(os.path.expanduser(new)):
            raise ValueError("a folder is given as an absolute path")
        words.append(f"--map={old}={new}")
    for name, value in values:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name) or \
                not isinstance(value, str) or any(c in value for c in "\n\r\0"):
            raise ValueError("a value is one line")
        words.append(f"--value={name}={value}")
    for kind, old, new in renames:
        if kind not in BUNDLE_KINDS or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}",
                                                                                  v) for v in (old, new)):
            raise ValueError("not a name a part can have")
        words.append(f"--rename={kind}:{old}={new}")
    for kind, name in replaces:
        if kind not in BUNDLE_KINDS or not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", name):
            raise ValueError("not a name a part can have")
        words.append(f"--replace={kind}:{name}")
    words += ["--images"] if images else []
    words += ["--unsigned"] if unsigned else []
    words += [f"--host={host}"] if host != "local" else []
    return words + ["--", path]


def session_transfer_args(command, name, host, new_name=None, maps=(), check=False, replace=False, state=None,
                          **extra):
    """The words after `sandbox-ai` of `session copy|move NAME HOST [--as NEW] [--map=LOCAL=REMOTE]... [--check]
    [--replace]`, checked: COMMAND copy or move, NAME and NEW session names, HOST one of sandbox-ai hosts (never this
    machine), MAPS [(local, remote)] absolute paths (a local one without '='); a pair that does not move anything is
    left out. REPLACE: a session of that name there is deleted first. ValueError says what is wrong."""
    if command not in ("copy", "move"):
        raise ValueError(f"not copy or move: {printable(command)}")
    if not isinstance(name, str) or not SESSION_NAME.fullmatch(name):
        raise ValueError("not a session's name")
    if not isinstance(host, str) or host == "local" or not REMOTE_HOST.fullmatch(host):
        raise ValueError("choose a host of sandbox-ai hosts (not this machine)")
    args = ["session", command, name, host]
    if new_name not in (None, "", name):
        if not isinstance(new_name, str) or not SESSION_NAME.fullmatch(new_name):
            raise ValueError("a session's name is 1-40 letters, digits, '_', '.' or '-', beginning with a letter or "
                             "digit")
        args += ["--as", new_name]
    moved = 0
    for local, remote in maps:
        local, remote = path_value(local, "a folder here"), path_value(remote, "its folder there")
        if "=" in local:
            raise ValueError(f"a folder here with '=' in it cannot be mapped: {printable(local)}")
        local, remote = local.rstrip("/") or "/", remote.rstrip("/") or "/"
        if local != remote:
            moved += 1
            args.append(f"--map={local}={remote}")
    if moved > TRANSFER_MAPS:
        raise ValueError("too many folders at once")
    return args + (["--check"] if check else []) + (["--replace"] if replace else []) + state_words(state, **extra)


def state_words(state=None, with_workspace=False, include_secrets=False, grant=False):
    """The words of what a copy, move or duplicate takes along (sandbox-ai 0.1.193 on): STATE False only its
    configuration (--config-only), True its state - with WITH_WORKSPACE its workspace's files, INCLUDE_SECRETS its
    logins and keys, GRANT its network approvals granted again there (both asked first by the dialog: --yes). STATE
    None: none of them (a sandbox-ai before 0.1.193 knows none)."""
    if state is None:
        return []
    if state is False:
        return ["--config-only"]
    return ((["--with-workspace"] if with_workspace else []) + (["--include-secrets"] if include_secrets else [])
            + (["--grant-approvals"] if grant else []) + (["--yes"] if include_secrets or grant else []))


def session_import_args(path, new_name=None, maps=(), check=False, replace=False, grant=False, no_workspace=False,
                        yes=False):
    """The words after `sandbox-ai` of `session import PATH --json [--as NEW] [--map=OLD=HERE]... [--check]
    [--replace] [--grant-approvals] [--no-workspace] [--yes]`, checked: PATH an absolute file of this machine, NEW a
    session name, MAPS [(folder of the machine it came from, where it lies here)] absolute paths. ValueError."""
    path = path_value(path, "the archive")
    args = ["session", "import", "--json"]
    if new_name not in (None, ""):
        if not isinstance(new_name, str) or not SESSION_NAME.fullmatch(new_name):
            raise ValueError("a session's name is 1-40 letters, digits, '_', '.' or '-', beginning with a letter or "
                             "digit")
        args += ["--as", new_name]
    maps = list(maps)
    if len(maps) > TRANSFER_MAPS:
        raise ValueError("too many folders at once")
    for there, here in maps:
        there, here = path_value(there, "a folder there"), path_value(here, "its folder here")
        if "=" in there:
            raise ValueError(f"a folder with '=' in it cannot be mapped: {printable(there)}")
        there, here = there.rstrip("/") or "/", here.rstrip("/") or "/"
        if there != here:
            args.append(f"--map={there}={here}")
    args += ((["--check"] if check else []) + (["--replace"] if replace else [])
             + (["--grant-approvals"] if grant else []) + (["--no-workspace"] if no_workspace else [])
             + (["--yes"] if yes else []))
    return args + ["--", path]


LIVE_HELPERS = AGENTS  # mcp attach --helper AGENT (and the launcher's --helper): every agent can be a helper
HELPER_SERVER = re.compile(rf"helper-({'|'.join(AGENTS)})-[0-9a-f]{{8}}")  # agent_sandbox.helper_name


def helper_of(server):
    """The agent of a helper's server name (helper-AGENT-HASH), or None for any other server."""
    match = HELPER_SERVER.fullmatch(str(server or ""))
    return match[1] if match else None


PLUGIN_ID = re.compile(rf"{KIT_NAME}@{KIT_NAME}")  # NAME@MARKETPLACE (kits.PLUGIN_NAME twice)
LIVE_ENTRY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}")  # kits_live.ENTRY_NAME, or a kit's name
LIVE_KINDS = ("kit", "skill", "command", "agent_file", "plugin")  # what kits_live_args gives a running start


def kits_live_args(start, kind, value, keep=False, restart=False, allow_findings=False):
    """The words after `sandbox-ai` that give the running START a KIND (LIVE_KINDS) now: `kits attach START KIT`,
    `kits attach START --skill=DIR` (--command=FILE, --agent-file=FILE: an absolute path on that host) or `plugins
    attach START NAME@MARKETPLACE`, with --keep (also its later starts) and --restart (its agent started again inside
    the sandbox), --allow-findings (also with what the hidden-text checker finds), always --json. ValueError says what
    is wrong."""
    if not valid_run(start):
        raise ValueError("not a start's name")
    flags = [*(["--keep"] if keep else []), *(["--restart"] if restart else []),
             *(["--allow-findings"] if allow_findings else []), "--json"]
    value = str(value or "").strip()
    if kind == "plugin":
        if not PLUGIN_ID.fullmatch(value):
            raise ValueError("a plugin is NAME@MARKETPLACE")
        return ["plugins", "attach", start, value, *flags]
    if kind == "kit":
        if not re.fullmatch(KIT_NAME, value):
            raise ValueError("a kit's name is 1-64 letters, digits, '_', '.' or '-'")
        return ["kits", "attach", start, value, *flags]
    if kind not in LIVE_KINDS:
        raise ValueError(f"one of {', '.join(LIVE_KINDS)}")
    if not (value.startswith("/") and value.isprintable() and len(value) < 4096
            and not any(part in (".", "..") for part in value.split("/"))):
        raise ValueError("an absolute path on that host")
    return ["kits", "attach", start, f"--{kind.replace('_', '-')}={value}", *flags]


def kits_detach_args(start, value, restart=False):
    """`kits detach START NAME --json` (a skill, subagent, command or a kit attached), or `plugins detach START
    NAME@MARKETPLACE --json`; with --restart its agent started again inside the sandbox."""
    if not valid_run(start):
        raise ValueError("not a start's name")
    value = str(value or "").strip()
    flags = [*(["--restart"] if restart else []), "--json"]
    if PLUGIN_ID.fullmatch(value):
        return ["plugins", "detach", start, value, *flags]
    if not LIVE_ENTRY.fullmatch(value):
        raise ValueError("the name of what was given (a skill, subagent, command or kit)")
    return ["kits", "detach", start, value, *flags]


def restart_agent_args(start):
    """`kits restart-agent START --json`: its agent started again inside the running sandbox."""
    if not valid_run(start):
        raise ValueError("not a start's name")
    return ["kits", "restart-agent", start, "--json"]


def mcp_live_args(start, server=None, helper=None, detach=False, live=True):
    """The words after `sandbox-ai` of `mcp attach NAME START --live --json` (HELPER: `--helper AGENT START`), or
    with DETACH `mcp detach ... --live --json`: a server into (out of) a running start now. Without LIVE (a detach
    only): out of a stopped or paused start's options, for its later starts. ValueError says what is wrong."""
    if not valid_run(start):
        raise ValueError("not a start's name")
    if helper:
        if helper not in LIVE_HELPERS or server:
            raise ValueError(f"a helper is one of {', '.join(LIVE_HELPERS)} (and no server's name with it)")
        named = ["--helper", helper]
    elif re.fullmatch(MCP_NAME, str(server or "")):
        named = [server]
    else:
        raise ValueError("an MCP server's name is 1-32 letters, digits, '_' or '-'")
    if not live and not detach:
        raise ValueError("an attach without --live is mcp_attach")
    return ["mcp", "detach" if detach else "attach", *named, start, *(["--live"] if live else []), "--json"]


HOST_WORD = r"(?:local|[a-z0-9][a-z0-9_-]{0,31})"  # hosts.NAME, or this machine
MCP_COPY_FLAGS = (("secrets_later", "--secrets-later"), ("pull_image", "--pull-image"), ("copy_image", "--copy-image"),
                  ("no_vpn", "--no-vpn"), ("current_pin", "--current-pin"), ("unsigned", "--unsigned"))


def mcp_copy_args(source, target, names, as_name=None, paths=None, drop_paths=(), replace=False, attach=None,
                  dry_run=True, **flags):
    """The words after `sandbox-ai` of `mcp copy NAME... --from SOURCE --to TARGET --json` (run on this machine,
    which reaches both hosts) with the choices that resolve what its check said: AS_NAME (one server), PATHS ({the
    source's folder: one of TARGET}), DROP_PATHS, REPLACE (asked first by the caller: --replace --yes), ATTACH (a
    running start of TARGET), and FLAGS (MCP_COPY_FLAGS' keys: true or false); DRY_RUN: the check. ValueError says
    what is wrong."""
    for host in (source, target):
        if not re.fullmatch(HOST_WORD, str(host or "")):
            raise ValueError("not a host's name")
    if source == target and not as_name:
        raise ValueError("from and to are the same host: give it another name there")
    names = list(dict.fromkeys(names or ()))
    if not names or not all(re.fullmatch(MCP_NAME, str(name)) for name in names):
        raise ValueError("an MCP server's name is 1-32 letters, digits, '_' or '-'")
    unknown = set(flags) - {key for key, _ in MCP_COPY_FLAGS}
    if unknown:
        raise ValueError(f"unknown choices: {', '.join(sorted(unknown))}")
    if flags.get("pull_image") and flags.get("copy_image"):
        raise ValueError("pull the image there or send it: one of them")
    args = ["mcp", "copy", *names, "--from", source, "--to", target, "--json"]
    if as_name:
        if len(names) != 1 or not re.fullmatch(MCP_NAME, str(as_name)):
            raise ValueError("another name is for one server: 1-32 letters, digits, '_' or '-'")
        args.append(f"--as={as_name}")
    for old, new in (paths or {}).items():
        args.append(f"--path={path_value(old, 'a folder of the other host')}={path_value(new, 'a folder there')}")
    for old in drop_paths or ():
        args.append(f"--drop-path={path_value(old, 'a folder of the other host')}")
    args += [flag for key, flag in MCP_COPY_FLAGS if flags.get(key)]
    if attach:
        if not valid_run(attach):
            raise ValueError("not a start's name")
        args.append(f"--attach={attach}")
    if replace:
        args += ["--replace", "--yes"]
    if dry_run:
        args.append("--dry-run")
    return args


def stop_args(name, wait=None):
    """`stop NAME [--wait SECONDS]` (how long sandbox-ai waits for the start to end; its default is 30 s)."""
    if not valid_run(name):
        raise ValueError("not a start's name")
    if wait is None:
        return ["stop", name]
    if isinstance(wait, bool) or not isinstance(wait, int) or not 1 <= wait <= 600:
        raise ValueError("wait is 1-600 seconds")
    return ["stop", name, f"--wait={wait}"]


def catalog_options(catalogs):
    """--catalog=SOURCE of each catalog: an https:// URL or an absolute path on that host."""
    found = []
    for value in catalogs or ():
        value = str(value).strip()
        if value.startswith("https://") and value.isprintable() and " " not in value and len(value) <= 2048:
            found.append(f"--catalog={value}")
        else:
            found.append(f"--catalog={path_value(value, 'a catalog')}")
    return found


# An MCP server's network (sandbox-ai mcp add|set --network) and how the app names it: the internet without your local
# network, only the hosts its catalog entry declares, the internet and your network (asked first: mcp-lan), nothing.
MCP_NETWORKS = ("full", "allowlist", "lan", "none")


MCP_DEFAULT_FOLDER = "default"  # --mount TARGET=default: the folder's default (sandbox-ai's mcp.DEFAULT_HOST)


def mcp_folder_value(value):
    """The HOST of an MCP server's --mount: an absolute path (path_value), or MCP_DEFAULT_FOLDER - its default, the
    start's workspace for /work, else a folder of the server's own, made by sandbox-ai where it is added."""
    return MCP_DEFAULT_FOLDER if str(value or "").strip() == MCP_DEFAULT_FOLDER else \
        path_value(value, "a mount's host directory")


def mcp_add_args(entry, name_as=None, mounts=(), env=(), network=None, memory=None, pids=None, catalogs=(),
                 no_pull=False, engine=None, unsigned=False, vpn=None, cpus=None, tmp=None):
    """(the words after `sandbox-ai` of `mcp add ... ENTRY`, the host directories mounted writable) with --as,
    --mount TARGET=HOST:MODE, --env, --network, --memory, --cpus, --pids, --catalog, --no-pull, --backend,
    --unsigned and --vpn, every value checked as the web interface checks it. ValueError says what is wrong."""
    if not re.fullmatch(MCP_ENTRY, str(entry or "")):
        raise ValueError("not a catalog entry (duckduckgo, hub:binwalk)")
    args, writable = ["mcp", "add"], []
    if name_as:
        if not re.fullmatch(MCP_NAME, str(name_as)):
            raise ValueError("a name for --mcp is 1-32 letters, digits, '_' or '-'")
        args.append(f"--as={name_as}")
    for target, source, mode in mounts:
        target, source = path_value(target, "a mount's target"), mcp_folder_value(source)
        if mode not in ("ro", "rw") or "=" in target + source or ":" in target + source:
            raise ValueError("a mount is TARGET=HOST with ro or rw (no '=' or ':' in the paths)")
        args.append(f"--mount={target}={source}:{mode}")
        if mode == "rw" and source != MCP_DEFAULT_FOLDER:  # (its default: the start's workspace, its own folder)
            writable.append(source)
    for variable in env:
        args.append(f"--env={env_spec(variable)}")  # NAME, NAME=VALUE or NAME@SECRET
    if network:
        if network not in MCP_NETWORKS:
            raise ValueError("the network is none, full, allowlist or lan")
        args.append(f"--network={network}")
    if memory:
        if not re.fullmatch(SIZE, str(memory)):
            raise ValueError("memory is a size: 512m, 2g")
        args.append(f"--memory={memory}")
    if cpus is not None:
        if not re.fullmatch(CPUS, str(cpus)):
            raise ValueError("CPUs: a number, like 2 or 1.5")
        args.append(f"--cpus={cpus}")
    if pids is not None:
        if isinstance(pids, bool) or not isinstance(pids, int) or not 1 <= pids <= 1000000:
            raise ValueError("processes: a whole number")
        args.append(f"--pids={pids}")
    if tmp:
        if not re.fullmatch(SIZE, str(tmp)):
            raise ValueError("/tmp is a size: 64m, 1g")
        args.append(f"--tmp={tmp}")
    args += catalog_options(catalogs)
    if no_pull:
        args.append("--no-pull")
    if engine:
        if engine not in ("podman", "bwrap", "both"):
            raise ValueError("the backend is podman, bwrap or both")
        args.append(f"--backend={engine}")
    if vpn:
        if not re.fullmatch(VPN_PROFILE, str(vpn)):
            raise ValueError("a VPN profile's name is 1-32 lower-case letters, digits, '_' or '-'")
        args.append(f"--vpn={vpn}")
    if unsigned:
        args.append("--unsigned")
    return args + [entry], writable


CPUS = r"[0-9]{1,4}(?:\.[0-9]{1,2})?"  # a CPU limit (mcp.checked_cpus checks its range)


ENV_VALUE_LIMIT = 1024  # mcp.ENV_VALUE_LIMIT: a plain value of an MCP server's variable


def env_spec(value):
    """VALUE as one --env of `mcp add`/`mcp set` (or the VAR part of --mcp-env NAME:VAR...): NAME, NAME=VALUE (a value
    of one line, no control character, at most ENV_VALUE_LIMIT characters; mcp.py refuses what looks like a secret) or
    NAME@SECRET. ValueError says what is wrong."""
    text = str(value if value is not None else "")
    name, equals, rest = text.partition("=")
    if equals:
        if not re.fullmatch(VARIABLE, name):
            raise ValueError("a variable is letters, digits and _, not starting with a digit")
        if len(rest) > ENV_VALUE_LIMIT or any(ord(c) < 32 or ord(c) == 127 for c in rest):
            raise ValueError(f"{name}: a value is one line of at most {ENV_VALUE_LIMIT} characters")
        return text
    name, at, secret = text.partition("@")
    if not re.fullmatch(VARIABLE, name) or (at and not re.fullmatch(VARIABLE, secret)):
        raise ValueError("a variable is letters, digits and _, not starting with a digit (NAME, NAME=VALUE or "
                         "NAME@SECRET)")
    return text


def mcp_set_args(name, stop=False, **changes):
    """The words after `sandbox-ai` of `mcp set NAME` with CHANGES: memory, cpus, pids (a value, or "default": back to
    the default), network (MCP_NETWORKS), mounts ([(target, host folder, "ro"|"rw")]: all of them; [] none), env
    ([NAME, NAME=VALUE or NAME@SECRET]: all of them; [] none - env_spec), vpn (a profile, or None: none); STOP: --stop.
    Every value checked for its form here (the
    command checks the rest as `mcp add` does). ValueError says what is wrong."""
    if not re.fullmatch(MCP_NAME, str(name or "")):
        raise ValueError("a name for --mcp is 1-32 letters, digits, '_' or '-'")
    if set(changes) - {"memory", "cpus", "pids", "tmp", "network", "mounts", "env", "vpn", "recommended"}:
        raise ValueError("only memory, cpus, pids, tmp, network, mounts, env and vpn can be set")
    args = ["mcp", "set", str(name)]
    if changes.get("recommended"):
        args.append("--recommended")
    for key, pattern in (("memory", SIZE), ("cpus", CPUS), ("pids", r"[0-9]{1,7}"), ("tmp", SIZE)):
        if key in changes:
            value = str(changes[key])
            if value != "default" and not re.fullmatch(pattern, value):
                raise ValueError(f"{key}: not a value of its kind ({value[:40]})")
            args.append(f"--{key}={value}")
    if "network" in changes:
        if changes["network"] not in MCP_NETWORKS:
            raise ValueError("the network is none, full, allowlist or lan")
        args.append(f"--network={changes['network']}")
    if "mounts" in changes:
        if not changes["mounts"]:
            args.append("--no-mounts")
        for target, source, mode in changes["mounts"]:
            target, source = path_value(target, "a mount's target"), mcp_folder_value(source)
            if mode not in ("ro", "rw") or "=" in target + source or ":" in target + source:
                raise ValueError("a mount is TARGET=HOST with ro or rw (no '=' or ':' in the paths)")
            args.append(f"--mount={target}={source}:{mode}")
    if "env" in changes:
        if not changes["env"]:
            args.append("--no-env")
        for variable in changes["env"]:
            args.append(f"--env={env_spec(variable)}")
    if "vpn" in changes:
        if changes["vpn"]:
            if not re.fullmatch(VPN_PROFILE, str(changes["vpn"])):
                raise ValueError("a VPN profile's name is 1-32 lower-case letters, digits, '_' or '-'")
            args.append(f"--vpn={changes['vpn']}")
        else:
            args.append("--no-vpn")
    if stop:
        args.append("--stop")
    return args


# What `sandbox-ai mcp add` says when it refuses an entry only because its catalog is not signed by a trusted key
# (mcp.check_signature; webui.MCP_UNSIGNED_HINT the same): `mcp add` has no --json, so its words are the signal. A
# signature that does not verify is a different refusal, never offered to be taken anyway.
MCP_UNSIGNED_HINT = "--unsigned adds it anyway"
MCP_UNSIGNED_SOURCE = re.compile(r"\((?P<catalog>(?:https://|/)\S+?)\.sig cannot be read\)")


def origin_text(origin):
    """What the MCP list shows of where a server comes from (mcp list --json's "catalog" and "signed"): the catalog,
    and whether its signature was verified."""
    from .i18n import N_, tr  # (no Qt) the words sandbox-ai uses for a server not from a catalog, translated
    catalog = origin.get("catalog") if isinstance(origin.get("catalog"), str) else ""
    if catalog in (N_("built-in"), N_("own (mcp.json)"), N_("team"), N_("unknown (added before 0.1.78)")):
        catalog = tr(catalog)
    signed = origin.get("signed")
    if isinstance(origin.get("built"), dict):  # (mcp add --build: never signed, said so wherever it is shown)
        return printable(catalog + ", " + tr("built here, not signed"))[:300]
    return printable(catalog + (", " + tr("signed") if signed is True else ", " + tr("unsigned") if signed is False
                                else ""))[:300]


def mcp_unsigned_refusal(text):
    """None, or {"catalog": the catalog's URL or path, else ""} when TEXT (what a failed `sandbox-ai mcp add` said)
    is the refusal of an entry whose catalog no trusted key signed, which `mcp add --unsigned` takes."""
    text = str(text or "")
    if MCP_UNSIGNED_HINT not in text or "not signed by a trusted key" not in text:
        return None
    match = MCP_UNSIGNED_SOURCE.search(text)
    return {"catalog": printable(match["catalog"])[:2048] if match else ""}


def newer_shown(newer):
    """What the app shows of mcp.newer_pin's NEWER (a host's answer too): {"image": [old, new] or None, "rootfs": [...]
    or None, "signed": bool}, each digest printable and bounded; None for nothing newer (or not of that shape)."""
    if not isinstance(newer, dict):
        return None
    shown = {}
    for key in ("image", "rootfs"):
        pair = newer.get(key)
        if isinstance(pair, list) and len(pair) == 2:
            shown[key] = [printable(str(value))[:200] for value in pair]
    return {**shown, "signed": newer.get("signed") is True} if shown else None


def short_digest(value):
    """The first 12 of an image's digest (REF@sha256:HEX) or of a rootfs's SHA-256."""
    value = str(value)
    return value.rpartition("@sha256:")[2][:12] if "@sha256:" in value else value[:12]


def mcp_start_args(name, engine=None, vpn=None, for_start=None, restart=False):
    """The words after `sandbox-ai` of `mcp start [--backend B] [--vpn PROFILE] [--for START --json [--restart]]
    NAME`, checked. ValueError says what is wrong."""
    if not re.fullmatch(MCP_NAME, str(name or "")):
        raise ValueError("not an MCP server's name")
    args = ["mcp", "start"]
    if engine:
        if engine not in ("podman", "bwrap"):
            raise ValueError("the backend is podman or bwrap")
        args.append(f"--backend={engine}")
    if vpn:
        if not re.fullmatch(VPN_PROFILE, str(vpn)):
            raise ValueError("a VPN profile's name is 1-32 lower-case letters, digits, '_' or '-'")
        args.append(f"--vpn={vpn}")
    if for_start:
        if not valid_run(for_start):
            raise ValueError("not a start's name")
        args += [f"--for={for_start}", "--json"] + (["--restart"] if restart else [])
    elif restart:
        raise ValueError("a restart goes with a start (--for)")
    return args + [name]


def privacy_check_args(paths, rules_dir=None, project=None, every=False):
    """The words after `sandbox-ai` of `privacy-check --json [--rules-dir=DIR] [--project=NAME] [--all] -- PATH...`
    (paths absolute, on the host that checks them). ValueError says what is wrong."""
    paths = [path_value(path, "a repository") for path in paths or () if str(path).strip()]
    if not paths:
        raise ValueError("the repositories to check: absolute paths")
    if len(paths) > 32:
        raise ValueError("at most 32 repositories at once")
    args = ["privacy-check", "--json"]
    if rules_dir:
        args.append(f"--rules-dir={path_value(rules_dir, 'the rules directory')}")
    if project:
        if not re.fullmatch(PROJECT_RULES, str(project)):
            raise ValueError("the project is the name of a rule file (letters, digits, '_', '.', '-')")
        args.append(f"--project={project}")
    if every:
        args.append("--all")
    return [*args, "--", *paths]


def update_args(version=None, check=False):
    """`update [--check] [--version X.Y.Z]`: this machine's own signed update (install.sh's)."""
    if version and not re.fullmatch(VERSION, str(version)):
        raise ValueError("a version is X.Y.Z")
    return ["update", *(["--check"] if check else []), *([f"--version={version}"] if version else [])]


def demo_device_args(action, port=None):
    """`demo-device start [--port PORT]|status|stop`."""
    if action not in DEMO_ACTIONS:
        raise ValueError("start, status or stop")
    if port is not None and action == "start":
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("the port is 1024-65535")
        return ["demo-device", "start", f"--port={port}"]
    return ["demo-device", action]

# == end of the start/host agent's section (module level) ==
