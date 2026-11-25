#!/usr/bin/env python3
"""sandbox-ai: one command for everything - the dispatcher of the command line.

`sandbox-ai --help` (help_text) lists every command once, grouped as Docker groups its own: the agents, the commands
used every day, the management commands (each with subcommands, their help laid out by helpstyle.py), the other
commands. The syntax of each is in its own --help.
"""
import contextlib
import importlib.util
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
PYTHON_COMMANDS = {"claude": ("agent_sandbox.py", "claude"), "codex": ("agent_sandbox.py", "codex"),
                   "opencode": ("agent_sandbox.py", "opencode"), "opencode2": ("agent_sandbox.py", "opencode2"),
                   "hermes": ("agent_sandbox.py", "hermes"),
                   "dsh": ("agent_sandbox.py", "dsh"), "qwen": ("agent_sandbox.py", "qwen"),
                   "goose": ("agent_sandbox.py", "goose"), "openhands": ("agent_sandbox.py", "openhands"), "nanobot": ("agent_sandbox.py", "nanobot"), "letta": ("agent_sandbox.py", "letta"), "pi": ("agent_sandbox.py", "pi"),
                   "session": ("sessions.py",), "board": ("board_bundle.py",), "checkpoint": ("checkpoints.py",),
                   "list": ("starts.py", "list"), "ps": ("starts.py", "ps"), "rename": ("starts.py", "rename"),
                   "attach": ("starts.py", "attach"), "shell": ("starts.py", "shell"), "pause": ("starts.py", "pause"),
                   "resume": ("starts.py", "resume"), "stop": ("starts.py", "stop"), "delete": ("starts.py", "delete"),
                   "gc": ("starts.py", "gc"),
                   "mcp": ("mcp.py",), "bridge": ("bridge.py",), "bridge-stub": ("bridge.py", "--stub"), "wizard": ("wizard.py",), "login": ("login.py",), "plugins": ("plugins.py",), "kits": ("kitctl.py",), "doctor": ("doctor.py",), "logs": ("logs.py",),
                   "activity": ("activity.py",),
                   "privacy-check": ("privacy.py", "check"), "net": ("net.py",), "notify": ("notify.py",),
                   "approvals": ("approvals.py",), "packages": ("packages.py",), "secret-guard": ("secret_guard.py",), "canaries": ("canary.py",), "safeguards": ("safeguards.py",), "secrets": ("secret_store.py",), "llm": ("llm.py",), "vpn": ("vpn.py",), "hosts": ("hosts.py",), "web": ("webui.py",), "defaults": ("defaults_cmd.py",), "team": ("team.py",), "presets": ("presets.py",), "demo-device": ("demo_device.py",), "git-logins": ("git_logins.py",), "publish-logins": ("publish_logins.py",), "identities": ("identities.py",), "completion": ("completion.py",), "__complete": ("completion.py", "__complete"),
                   "images": ("images.py",), "devices": ("devices.py",), "limits": ("devices.py", "--limits"),
                   "workspace": ("workspaces.py",), "toolchain": ("toolchains.py",), "usage": ("usage.py",),
                   "rules": ("rules_cmd.py",), "cairn": ("cairn_workers.py",),
                   "top": ("monitor.py", "top"), "stats": ("monitor.py", "stats"),
                   "report": ("report.py",), "scan": ("lint.py",), "schedule": ("schedule.py",), "spend": ("spend.py",), "models": ("cloud_models.py",), "artex": ("artex.py",),
                   "install": ("install.py",), "uninstall": ("uninstall.py",)}


# `sandbox-ai --help`: every command of the dispatcher once (tests/test_main_help.py checks it), in Docker's groups.
USAGE = ("Usage:  sandbox-ai [OPTIONS] COMMAND\n"
         "        sandbox-ai AGENT [OPTIONS] [-- AGENT ARGS]")
TAGLINE = "Coding agents in sandboxes: sealed off, watched, asked before anything leaves."
HELP = (
    ("Agents", (
        ("claude", "Start Claude Code in a sandbox"),
        ("codex", "Start Codex in a sandbox"),
        ("opencode", "Start OpenCode in a sandbox"),
        ("opencode2", "Start OpenCode 2 in a sandbox"),
        ("hermes", "Start Hermes Agent in a sandbox"),
        ("dsh", "Start DeepSeek Harness in a sandbox"),
        ("qwen", "Start Qwen Code in a sandbox"),
        ("goose", "Start Goose in a sandbox"),
        ("openhands", "Start OpenHands in a sandbox"),
        ("nanobot", "Start nanobot in a sandbox"),
        ("letta", "Start Letta Code in a sandbox"),
        ("pi", "Start Pi in a sandbox"))),
    ("Common Commands", (
        ("list, ps", "What runs and what is kept, grouped by start"),
        ("attach", "Attach this terminal to a start (detach: Ctrl-b d)"),
        ("shell", "Your shell in a running start, under its rules"),
        ("pause", "Freeze a running start"),
        ("resume", "Thaw a paused start, or start a kept one again"),
        ("stop", "Stop a start, or every start of yours on this host"),
        ("delete", "Remove a stopped start with its HOME"),
        ("rename", "Give a start another name"),
        ("logs", "Blocked egress and file access, with suggestions"),
        ("activity", "What the agents did: prompts, tools, files, network"),
        ("login", "Log in inside a sandbox; kept for all sandboxes"),
        ("build", "Build the agent images and the others a start needs"),
        ("install", "Images, MCP servers, toolchains: all, nothing or a part"))),
    ("Management Commands", (
        ("approvals", "Answer what running sandboxes ask"),
        ("artex", "ARTEX's console (--artex): its address and password"),
        ("board", "A board or session as one file; take one in"),
        ("bridge", "Let one running session reach another"),
        ("cairn", "Cairn's board: how each harness fares as its worker"),
        ("canaries", "Decoy files of the runs, and what happened to them"),
        ("checkpoint", "The workspaces as they were; undo what an agent did"),
        ("defaults", "Your defaults for every start (defaults.json)"),
        ("demo-device", "A virtual device to try the device rules"),
        ("devices", "Hand devices to a running sandbox"),
        ("git-logins", "Git logins for pushes; give a running sandbox one"),
        ("hosts", "Other machines (LXC containers) managed over SSH"),
        ("identities", "Commit identities with their logins"),
        ("images", "The agent images, their extensions, your images"),
        ("kits", "See and change kits (skills, plugins)"),
        ("llm", "LLM servers, local or remote, for --llm; their traffic"),
        ("mcp", "MCP server sandboxes"),
        ("net", "Change what a running sandbox may reach; the kill switch"),
        ("notify", "Questions and alarms to your phone: ntfy, Telegram, WhatsApp"),
        ("packages", "The package guard: known malicious packages, your allow-list"),
        ("presets", "Ready setups to start an agent with"),
        ("publish-logins", "Package registry logins, used by the proxy"),
        ("rules", "Check rulesets, show a start's, reload a running one's"),
        ("schedule", "Start an agent on a timetable or a trigger; its reports"),
        ("secret-guard", "Secrets in outgoing requests: what is found, your allow-list"),
        ("secrets", "API keys and tokens in your keyring; give a run one"),
        ("session", "Sessions: copy or move one; its memory and files, exported"),
        ("spend", "What the model requests cost; budgets that stop a run"),
        ("stats", "What the runs used over time, by project, run or agent"),
        ("team", "A team of agents, one sandbox each, with a task board"),
        ("toolchain", "Build tools (Go, Rust, a JDK ...) mounted into a sandbox"),
        ("vpn", "VPN profiles for --vpn (WireGuard, OpenVPN, Tor ...)"),
        ("web", "The web interface (HTTPS, password and TOTP)"),
        ("wizard", "Project setup: create, edit and trust its configuration"),
        ("workspace", "Hand a host folder or file to a running sandbox"))),
    ("Commands", (
        ("completion", "Tab completion for your shell"),
        ("doctor", "Check the host, settings files and kept sessions"),
        ("gc", "Remove leftovers and old kept starts"),
        ("help", "Show this help, or the help of a command"),
        ("limits", "Change a running sandbox's limits"),
        ("models", "The cloud models of each agent and their effort levels"),
        ("plugins", "Install the plugins the kits declare; every marketplace"),
        ("privacy-check", "What the next push would publish that should stay private"),
        ("report", "One report of runs: stats, network, security"),
        ("safeguards", "What the safeguards refused or ended, why, the fix"),
        ("scan", "Hidden text and instructions to the model in files"),
        ("top", "The system monitor: CPU, memory, IO, network per run"),
        ("uninstall", "Remove this installation; --purge: also its data"),
        ("update", "Update an installation made by install.sh (signed)"),
        ("usage", "What a running sandbox uses: CPU, memory, processes"),
        ("version", "Show the version; --verbose: install place, release keys"))),
    ("Global Options", (
        ("-h, --help", "Show this help"),
        ("-V, --version", "Show the version"),
        ("--color WHEN", "Colour: auto (on a terminal, the default), always, never"),
        ("-nc, --no-color", "Without colour (also NO_COLOR=1); after a command too"))),
)
# Commands that reach another user's start for root (USER:NAME, --user USER; allusers.py).
OTHER_USER_COMMANDS = ("stop", "logs", "report", "list", "ps")
# Commands for sandbox-ai's own use, not shown: the bridge's stub in a sandbox, the shell completion's questions.
HIDDEN = {"bridge-stub", "__complete"}


COLOUR_CHOICES = ("auto", "always", "never")
# Commands whose words after these run elsewhere, taken as they are: a sandbox-ai on another host (hosts run NAME ...).
PASSED_ON = {("hosts", "run"): 1}


def colour_options(argv):
    """(ARGV without sandbox-ai's own colour options, the choice: "auto", "always", "never" or None). They are taken
    before the command (--no-color, -nc, --color WHEN, --color=WHEN) and after it (--no-color, --color WHEN), up to
    "--" - an agent's own arguments after it are never touched - and not from the words a command passes on to run
    elsewhere (hosts run NAME ...). -nc after a command stays the command's: an agent's --no-config, list's and
    doctor's own --no-color. A --color with a value it does not know stays too (the command says what is wrong)."""
    argv, choice, kept, index = list(argv), None, [], 0
    command, before_command = None, True
    stop = argv.index("--") if "--" in argv else len(argv)
    while index < stop:
        word = argv[index]
        if command is not None and (command, word) in PASSED_ON and index > 0:
            stop = min(stop, index + 1 + PASSED_ON[(command, word)])
            kept.append(word)
            index += 1
            continue
        if word in ("--no-color", "--no-colour") or (word == "-nc" and before_command):
            choice = "never"
        elif word.startswith(("--color=", "--colour=")) and word.split("=", 1)[1] in COLOUR_CHOICES:
            choice = word.split("=", 1)[1]
        elif word in ("--color", "--colour") and index + 1 < stop and argv[index + 1] in COLOUR_CHOICES:
            choice = argv[index + 1]
            index += 1
        else:
            if before_command and not word.startswith("-"):
                command, before_command = word, False
            kept.append(word)
        index += 1
    return kept + argv[stop:], choice


def apply_colour(choice):
    """The colour CHOICE for this process and every program it starts (the commands' scripts, the agents' launcher,
    build_image.sh, install.sh): never sets NO_COLOR, always FORCE_COLOR (over NO_COLOR: the option is the later
    choice), auto or None leave the environment as it is (textout.colour_enabled decides)."""
    if choice == "never":
        os.environ.pop("FORCE_COLOR", None)
        os.environ["NO_COLOR"] = "1"
    elif choice == "always":
        os.environ.pop("NO_COLOR", None)
        os.environ["FORCE_COLOR"] = "1"


def textout():
    """textout.py (styles, colour only where it belongs): the one copy of this process."""
    loaded = sys.modules.get("sandbox_ai_textout")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_textout", HERE / "textout.py")
        made = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(made)
        loaded = sys.modules.setdefault("sandbox_ai_textout", made)
    return loaded


def say(text):
    """One of sandbox-ai's own messages on standard error (an error red, a warning yellow on a terminal)."""
    print(textout().message(text, stream=sys.stderr), file=sys.stderr)


def show_help(text):
    """A help page on standard output, coloured on a terminal (textout.help_page)."""
    print(textout().help_page(text, stream=sys.stdout))


def help_text():
    """`sandbox-ai --help`: one line per command, the names in one column."""
    column = max(len(name) for _, rows in HELP for name, _ in rows) + 4
    lines = [USAGE, "", TAGLINE]
    for title, rows in HELP:
        lines += ["", f"{title}:", *(f"  {name}".ljust(column) + text for name, text in rows)]
    return "\n".join(lines + ["", "Run 'sandbox-ai COMMAND --help' for more information on a command."])


# The commands with a `list` that run it when given alone (approvals does it itself: also with --json alone).
LISTED_ALONE = {"mcp", "vpn", "llm", "secrets", "identities", "hosts", "presets", "devices", "canaries", "net",
                "git-logins", "publish-logins"}


def version():
    for line in (HERE / "agent_sandbox.py").read_text().splitlines():
        if line.startswith("__version__ = "):
            return line.split('"')[1]
    return "unknown"


# How a ready-made package of sandbox-ai was made (PACKAGING beside sandbox/, written by its build): it is updated
# by the release's new package (updates.py, package_update.py: it replaces itself), not by install.sh.
PACKAGES = {"pyz": "sandbox-ai.pyz", "appimage": "the AppImage (sandbox-ai-ARCH.AppImage)",
            "macos-app": "the macOS app (sandbox-ai-ARCH.dmg)"}


def packaging():
    """'pyz', 'appimage', 'macos-app' or None (a checkout, install.sh, pip)."""
    try:
        kind = (HERE.parent / "PACKAGING").read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return kind if kind in PACKAGES else None


def self_update_way():
    """How this copy is updated when install.sh did not install it (a package, a pip installation): "sandbox-ai
    update" where it replaces itself, else the command that does (updates.manual_way)."""
    updates = module("updates")
    return updates.manual_way(updates.installation())


# The agents' launcher keeps a process of its own (python -I agent_sandbox.py AGENT ...): net, the app and the
# sessions recognise a running start by that command line in /proc.
EXECUTED = ("agent_sandbox.py",)


def executed_script(script):
    """The file that runs SCRIPT in a process of its own: launcher/agent_sandbox.py, which runs agent_sandbox.py from
    its cached bytecode under the same name (a script Python runs itself is compiled anew: 55 ms of every start);
    SCRIPT itself in a copy without it."""
    stub = HERE / "launcher" / script
    return stub if stub.is_file() else HERE / script


def run_script(path, args):
    """Runs the command script PATH as __main__ with ARGS in this process, as `python -I PATH ARGS` would (the
    same interpreter, flags and environment) - without a second interpreter start, and from its cached bytecode
    (__pycache__, written by install.sh's compileall) instead of compiling it anew: what the app's polls, which
    run a command every few seconds, wait for. Its exit status is the script's (sys.exit), 0 when it just ends."""
    spec = importlib.util.spec_from_file_location("__main__", path)
    script = importlib.util.module_from_spec(spec)
    sys.argv = [str(path), *args]
    sys.modules["__main__"] = script
    spec.loader.exec_module(script)
    return 0


def full_said(command, call):
    """CALL() (a command's run), a full file system (an OSError ENOSPC or EDQUOT naming its file, which the command
    did not catch itself) said in one line - what could not be written, which file system, the fix
    (agent_sandbox.full_text) - instead of a Python traceback: `defaults set`, `notify add`, `identities add` ...
    caught their own errors only."""
    import errno
    try:
        return call()
    except OSError as exc:
        if exc.errno not in (errno.ENOSPC, errno.EDQUOT) or not exc.filename:
            raise
        try:
            text = module("agent_sandbox").full_text(exc)
        except Exception:  # noqa: BLE001 - the words only: Python's own then
            raise exc from None
        if not text:
            raise
        print(f"sandbox-ai {command}: {text}", file=sys.stderr)
        return 1


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        made = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(made)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", made)
    return loaded


# Commands that only look (the app runs several every few seconds): their lines are DEBUG, and one that fails is said
# once until it says something else (launchlog.changed). The first word after the command decides for the others.
LOOKING_COMMANDS = {"list", "ps", "logs", "activity", "usage", "top", "stats", "safeguards", "doctor", "completion", "__complete", "models",
                    "version", "--version", "-V"}
LOOKING_WORDS = {"list", "ls", "show", "status", "--status", "catalog", "dirs", "history", "blocked", "decided",
                 "remembered", "kinds", "traffic", "explain", "help"}
SAID_LINES = 2  # lines of what a failing command said last (its error), kept in its end line


def only_looks(command, rest):
    return command in LOOKING_COMMANDS or bool(rest[:1]) and rest[0] in LOOKING_WORDS or \
        any(word in ("-h", "--help") for word in rest)


class LastLines:
    """Standard error while a command runs: everything written goes on to STREAM - on a terminal with sandbox-ai's own
    messages marked (textout.message: an error red, a warning yellow), else unchanged; the last few whole lines are
    kept, without colour (what a command that fails said last: its error, for launcher.log)."""

    def __init__(self, stream, keep=8):
        self.stream, self.keep, self.lines, self._pending = stream, keep, [], ""
        self._colour = None  # decided at the first write (a command that says nothing loads no textout)

    def write(self, text):
        shown = text
        if isinstance(text, str):
            if not self._pending and text:  # (a text that starts a line: one of sandbox-ai's messages, marked)
                if self._colour is None:
                    self._colour = textout().colour_enabled(self.stream)
                if self._colour:
                    shown = textout().message(text, True)
            *whole, self._pending = (self._pending + text).split("\n")
            self._pending = self._pending[-2000:]
            self.lines = [*self.lines, *(line for line in whole if line.strip())][-self.keep:]
        self.stream.write(shown)
        return len(text) if isinstance(text, str) else None

    def last(self, count, own=False):
        """The last COUNT lines said (OWN: only sandbox-ai's own messages, not a tool's output)."""
        lines = [*self.lines, *([self._pending] if self._pending.strip() else [])]
        if own:
            lines = [line for line in lines if line.startswith(("sandbox-ai", "agent-sandbox: "))]
        return " | ".join(line.strip() for line in lines[-count:])

    def __getattr__(self, name):
        return getattr(self.stream, name)


def logged_command(command, rest, call):
    """CALL() - the command COMMAND REST of sandbox-ai - with what was asked and how it ended in launcher.log: its
    command line (secret values taken out), its exit status, time and, when it failed, what it said last; a command
    that changes something (stop, delete, net allow, approvals allow ...) at INFO (WARNING when it failed), one that
    only looks at DEBUG (a failure once, as INFO). Its result or exception unchanged."""
    import time
    log = launch_log()
    looks = only_looks(command, rest)
    shown = log.command_line([command, *rest])
    shown = shown if len(shown) <= 300 else shown[:300] + "..."
    if not looks:  # (a look: its one end line says it, with SANDBOX_AI_DEBUG=1 - the app polls them)
        log.info("command", f"asked: sandbox-ai {shown}")
    begun, status, said = time.monotonic(), 1, None
    saved = sys.stderr
    tee = sys.stderr = LastLines(saved)
    try:
        status = call()
        return status
    except SystemExit as exc:
        status = 0 if exc.code is None else exc.code
        raise
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    except BaseException as exc:  # (the traceback goes to the terminal as before)
        said = log.exception_text(exc)
        raise
    finally:
        if sys.stderr is tee:
            sys.stderr = saved
        seconds = time.monotonic() - begun
        if isinstance(status, str) and status != "interrupted":  # sys.exit("message"): said as Python exits
            status, said = 1, status
        status = 0 if status is None else status
        failed = status != 0
        said = said or tee.last(SAID_LINES if failed else 1, own=not failed)
        text = (f"sandbox-ai {shown}: " + (f"interrupted after {seconds:.1f} s" if status == "interrupted" else
                                           f"exit status {status} after {seconds:.1f} s" if failed else
                                           f"done after {seconds:.1f} s") + (f": {said}" if said else ""))
        if looks:
            # (polled: a failure is said once until the command says something else; working again, it is noted)
            if failed:
                log.changed(("command failed", shown), f"{status}: {said}", "command", text, "INFO")
            else:
                log.debug("command", text)
                if log.state(("command failed", shown)) not in (None, "ok"):
                    log.changed(("command failed", shown), "ok", "command", None)
        else:
            log.log("WARNING" if failed and status != "interrupted" else "INFO", "command", text)
        log.flush()


def install_help_style():
    """Every --help of the command about to run laid out as Docker lays out its own (helpstyle.install), coloured on a
    terminal: the one copy of helpstyle.py of this process, the one the commands' own parsers load."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        loaded = sys.modules.setdefault("sandbox_ai_helpstyle", loaded)
    loaded.install()


def module(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_main", HERE / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def verbose_version():
    """Where this copy lives, which keys its updates must be signed by, and whether the policy demands it."""
    print(f"sandbox-ai {version()}")
    print(f"installed in: {HERE.parent}")
    keys = module("release_keys")
    try:
        trusted = keys.load()
    except ValueError as exc:
        trusted = None
        print(f"release keys: unreadable: {exc}")
    if trusted is not None:
        print(f"release keys ({keys.SIGNERS}): {len(trusted) or 'none'}")
        for kind, key in trusted:
            print(f"  {keys.fingerprint(key)} ({kind})")
    try:
        required = module("agent_sandbox").load_policy().get("require_signed_updates", False)
    except ValueError as exc:
        required = None
        print(f"policy: {exc}")
    package = packaging()
    if package or not (HERE.parent / "install.sh").is_file():
        # (hosts.installation reads these lines: "package: " and "updates: not through install.sh" say the kind,
        # "self-update: yes" that this copy's own `sandbox-ai update` replaces it - the releases after 0.1.98)
        way = self_update_way()
        if package:
            print(f"package: {package}")
            print("updates: " + ("the signed release replaces it (sandbox-ai update)" if way == "sandbox-ai update"
                                 else way))
        else:
            print("updates: not through install.sh (a pip installation: "
                  f"{'the signed wheel of the release, sandbox-ai update' if way == 'sandbox-ai update' else way})")
        print(f"self-update: {'yes' if way == 'sandbox-ai update' else 'no'}")
    elif trusted:
        print("updates: must be signed by one of these keys (sandbox-ai update)")
    elif required:
        print("updates: refused: the administrator's policy requires signed updates, but no release key is trusted")
    else:
        print("updates: checked against SHA256SUMS only (no release key yet)")
    if required:
        print("policy: signed updates required (require_signed_updates); --allow-unsigned is refused")
    with contextlib.suppress(Exception):  # (hosts list reads it: each host's update mode)
        print(f"update mode: {module('updates').settings()['mode']}")
    return 0


HOMES = ("/home/", "/root/", "/Users/", "/var/home/")


UPDATE_HELP = """Usage:  sandbox-ai update [OPTIONS] [install.sh's other options]
        sandbox-ai update mode [off|notify|download|auto] [--at HH:MM[-HH:MM]] [--source URL] [--json]

Update this installation to the newest signed release (install.sh, as the same kind of installation: a
system one under its prefix, a user's own for that user). The .pyz, the AppImage, the macOS app and a
pip installation replace themselves with their file of the release (checked the same way).

Options:
  --check            Show the installed and the newest version and its signature; change nothing
                     (every copy, a git checkout too)
  --apply            Install the release update mode download fetched (checked again first)
  --install          Download the newest signed release now, check it and install it (what the app's
                     Install now does; nothing runs in a terminal)
  --rollback         Back to the version before (install.sh keeps the last 2 in versions/; a package
                     keeps the one before its last update)
  --while-running    With --apply, --install or --rollback: also while sandboxes, sessions or MCP servers
                     run (they go on with the version they started with)
  --scheduled        What the update timer runs: the daily check, the download, the install in the window
  --version X.Y.Z    This release instead of the newest
  --source URL       The repository with the releases (Gitea, Forgejo or https://github.com/OWNER/REPO)
  --force            Reinstall the same version
  --allow-downgrade  Install a release older than the installed one
  --user             A user's own installation (also when root updates it)
  --system           A system installation (under --prefix DIR)
  --prefix DIR       The prefix of a system installation
  --uninstall        Remove the installation, its command links and the PATH block
  -h, --help         Show this help

Update mode (sandbox-ai update mode --help): off, notify (the default: a check once a day, a newer
signed release announced), download (also fetched and checked: --apply installs it), auto (also
installed in a window while nothing runs).

The package guard's copy of the known malicious packages is synced along with an update (signed, at
most once a day; sandbox-ai packages).

Other hosts: sandbox-ai hosts update NAME (their own signed update), or sandbox-ai hosts update NAME
--from-here (this machine's copy, no release server needed); sandbox-ai hosts update NAME --mode MODE
sets a host's own update mode.

Examples:
  sandbox-ai update --check  the installed and the newest version; nothing changes
  sandbox-ai update          the newest signed release
  sandbox-ai update mode auto  install new signed releases at night, while nothing runs"""
VERSION_HELP = """Usage:  sandbox-ai version [OPTIONS]

Show the installed version.

Options:
  -v, --verbose  Also where it is installed, the release keys its updates must be signed by and how it
                 is updated
  -h, --help     Show this help

Run 'sandbox-ai --help' for all commands."""


def installation_mode(rest, here=None, euid=None, writable=None):
    """`update` ARGS with the kind of installation this copy is (install.sh decides by who runs it: root makes a
    system installation, anyone else one of their own): a system one (PREFIX/lib/sandbox-ai/versions/X) is
    updated as such (--system --prefix PREFIX), a user's own stays theirs when root updates it (--user). A
    ValueError when a system installation would be updated by someone who cannot write it."""
    here = Path(here or HERE)
    euid = os.geteuid() if euid is None else euid
    writable = writable or (lambda path: os.access(path, os.W_OK))
    # --uninstall and --check act on an installation too: the one this copy belongs to, unless one is named.
    given = any(arg in ("--user", "--system", "--prefix") or arg.startswith("--prefix=") for arg in rest)
    if given or here.parent.parent.name != "versions":
        return list(rest)
    data = here.parent.parent.parent
    if data.name == "sandbox-ai" and data.parent.name == "lib" and not str(data).startswith(HOMES):
        prefix = data.parent.parent
        if euid != 0 and not writable(data) and "--check" not in rest:  # (a check changes nothing)
            raise ValueError(f"this is a system installation ({data}): update it as root: sudo sandbox-ai update")
        return ["--system", "--prefix", str(prefix), *rest]
    return ["--user", *rest] if euid == 0 else list(rest)


BASH_CANDIDATES = ("/opt/homebrew/bin/bash", "/usr/local/bin/bash")  # Homebrew on Apple silicon, on Intel


def bash_version(executable):
    """(major, minor) of the bash at EXECUTABLE, or None."""
    import subprocess
    try:
        out = subprocess.run([executable, "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'], capture_output=True,
                             text=True, timeout=10).stdout.split()
        return int(out[0]), int(out[1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def find_bash(which=shutil.which, version=bash_version):
    """A bash of version 4 or later (the scripts use associative arrays): the one on PATH, else Homebrew's (macOS
    ships bash 3.2 as /bin/bash). sys.exit with what to install when there is none."""
    for executable in dict.fromkeys([which("bash"), *BASH_CANDIDATES]):
        if executable and os.access(executable, os.X_OK) and (version(executable) or (0, 0)) >= (4, 0):
            return executable
    if which("bash") is None:
        sys.exit("sandbox-ai: bash is required")
    sys.exit("sandbox-ai: bash 4 or later is required (macOS ships 3.2): brew install bash")


BUILD_VALUE_OPTIONS = {"-pu": "--podman-url", "--podman-url": "--podman-url", "-p": "--pin", "--pin": "--pin",
                       "-u": "--uv", "--uv": "--uv", "-n": "--node", "--node": "--node"}
BUILD_FLAGS = {"-e": "--extension-only", "--extension-only": "--extension-only"}
AGENT_IMAGES = ("codex", "claude", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot",
                "letta", "pi")


def build_split(args):
    """([(option, value or None)], target or None, [podman build options after it]) of `sandbox-ai build ARGS`."""
    options, index = [], 0
    while index < len(args):
        token = args[index]
        if token in BUILD_VALUE_OPTIONS and index + 1 < len(args):
            options.append((BUILD_VALUE_OPTIONS[token], args[index + 1]))
            index += 2
        elif token in BUILD_FLAGS:
            options.append((BUILD_FLAGS[token], None))
            index += 1
        else:
            return options, token, args[index + 1:]
    return options, None, []


BUILD_PLAIN_TARGETS = ("team", "cairn", "artex", "browser", "vpn", "tor")


def build_several(args):
    """The targets of `sandbox-ai build claude codex team ...` when there are two or more (agents, team, cairn, artex,
    browser, vpn, tor: each a build of its own, the README's way to rebuild some images), else None."""
    options, target, extra = build_split(args)
    if target not in (*AGENT_IMAGES, *BUILD_PLAIN_TARGETS):
        return None
    names = [target]
    while extra and extra[0] in (*AGENT_IMAGES, *BUILD_PLAIN_TARGETS):
        names.append(extra[0])
        extra = extra[1:]
    return names if len(names) > 1 else None


def build_all_wanted(args):
    return build_split(args)[1] == "all"


def build_all(args, run=None, out=None):
    """`sandbox-ai build all`: every image sandbox-ai builds - the twelve agents, the team image, Cairn and ARTEX (their
    own images), the browser, the VPN and Tor images, and every variant of yours (sandbox-ai images) - each a build of its
    own, so one that fails stops none of the others; at the end what was built and what failed. 0 when all were.
    `sandbox-ai build claude codex team` (build_several) is the same for the targets named, in that order."""
    import subprocess
    run = run or subprocess.run
    out = out or (lambda text: print(text, file=sys.stderr, flush=True))
    options, _, extra = build_split(args)
    several = build_several(args)
    word = "several" if several else "all"
    if several:
        extra = extra[len(several) - 1:]
    if any(option == "--pin" for option, _ in options):
        out(f"sandbox-ai build: --pin applies to one agent, not {word}")
        return 2
    allowed = {"agent": {"--podman-url", "--uv", "--node", "--extension-only"},
               "team": {"--podman-url", "--uv", "--node"}, "plain": {"--podman-url"}, "browser": set(),
               "variant": {"--podman-url", "--extension-only"}}
    plan = [(agent, [agent], "agent") for agent in AGENT_IMAGES]
    plan += [("team", ["team"], "team"), ("cairn", ["cairn"], "plain"), ("artex", ["artex"], "plain"),
             ("browser", ["browser"], "browser"),
             ("vpn", ["vpn"], "plain"), ("tor", ["tor"], "plain")]
    if several:
        plan = [entry for name in several for entry in plan if entry[0] == name]
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_images_for_build", HERE / "images.py")
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
        found = [] if several else images.variants()  # (a name several agents have: NAME@AGENT)
        plan += [(f"variant {name} ({agent})", ["--variant", images.shown_target(name, agent, found)], "variant")
                 for name, agent in found]
    except Exception as exc:  # noqa: BLE001 - the images themselves are still built
        out(f"sandbox-ai build: the variants could not be read ({exc}); building the others")
    executable = find_bash()
    built, failed = [], []
    for number, (label, target, kind) in enumerate(plan, 1):
        given = [part for option, value in options if option in allowed[kind]
                 for part in ([option] if value is None else [option, value])]
        out(f"sandbox-ai build: {word} ({number}/{len(plan)}): {label}")
        # A note every build would say alike (no systemd user session: cgroupfs) is said by the first one only.
        status = run([executable, str(HERE / "build_image.sh"), *given, *target, *extra], check=False,
                     env={**os.environ, **({"SANDBOX_AI_BUILD_CGROUP_SAID": "1"} if number > 1 else {})}).returncode
        (built if status == 0 else failed).append(label)
    out(f"sandbox-ai build: {word}: {len(built)} of {len(plan)} built" +
        (f"; failed: {', '.join(failed)} (each said why above; build one again with sandbox-ai build NAME)"
         if failed else ""))
    return 0 if not failed else 1


def bash(script, args):
    executable = find_bash()
    os.execv(executable, [executable, str(script), *args])


# The single commands removed in 0.1.92 (deprecated since 0.1.30) and what took their place.
REMOVED = {"claude-sandbox": "sandbox-ai claude", "codex-sandbox": "sandbox-ai codex",
           "opencode-sandbox": "sandbox-ai opencode", "sandbox-session": "sandbox-ai session",
           "sandbox-mcp": "sandbox-ai mcp", "sandbox-wizard": "sandbox-ai wizard",
           "sandbox-build-image": "sandbox-ai build"}


def main(argv):
    argv, choice = colour_options(argv)
    apply_colour(choice)
    if argv[:1] == ["help"] and argv[1:2] and argv[1] != "help":
        return main([*argv[1:2], "--help"])  # sandbox-ai help net: the help of that command
    if not argv or argv[0] in ("-h", "--help", "help"):
        show_help(help_text())
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command in REMOVED:
        say(f"sandbox-ai: {command} was removed in 0.1.92; use {REMOVED[command]}")
        return 2
    if command in ("version", "--version", "-V"):
        if rest[:1] in (["-h"], ["--help"]):
            show_help(VERSION_HELP)
            return 0
        if rest[:1] in (["--verbose"], ["-v"]) and len(rest) == 1:
            return verbose_version()
        if rest:
            say(f"sandbox-ai version: unknown argument {rest[0]!r}; usage: sandbox-ai version [--verbose]")
            return 2
        print(f"sandbox-ai {version()}")
        return 0
    if command in LISTED_ALONE and not rest:
        rest = ["list"]  # (alone it lists, as images, kits, plugins and defaults alone do)
    update_hint(command, rest)
    if command in OTHER_USER_COMMANDS and any(word == "--user" or word.startswith("--user=") or ":" in word
                                              for word in rest):
        # Another user's start (USER:NAME, --user USER): root runs the command as that user, with root's own copy
        # (allusers.py); one's own name is just the start here.
        try:
            redirected = module("allusers").redirect(command, rest)
        except ValueError as exc:
            say(f"sandbox-ai {command}: {exc}")
            return 1
        if isinstance(redirected, tuple):
            rest = redirected[1]
        elif redirected is not None:
            return redirected
    if command in PYTHON_COMMANDS:
        script, *prefix = PYTHON_COMMANDS[command]
        if script in EXECUTED:  # (the launcher logs its start itself)
            os.execv(sys.executable, [sys.executable, "-I", str(executed_script(script)), *prefix, *rest])
        if any(word in ("-h", "--help") for word in rest):  # (only a help page is laid out: nothing else loads it)
            install_help_style()
        return logged_command(command, rest, lambda: full_said(command, lambda: run_script(HERE / script,
                                                                                         [*prefix, *rest])))
    if command == "build":
        os.environ["SANDBOX_AI_PYTHON"] = sys.executable  # the extension layers are built by images.py
        if any(word in ("-h", "--help") for word in rest[:rest.index("--") if "--" in rest else len(rest)]):
            rest = ["--help"]  # (build NAME --help is build's own help: after the target the words go to podman build)
        if os.geteuid() == 0 and not rest[:1] in (["-h"], ["--help"]):
            # Rootless Podman keeps one image store per user: root's build is not seen by a user's start.
            who = os.environ.get("SUDO_USER")
            say("sandbox-ai build: note: built as root, the image goes into root's Podman store "
                "(/var/lib/containers/storage); starts by another user do not see it - run sandbox-ai build as "
                f"the user who starts the sandboxes{' (' + who + ')' if who and who != 'root' else ''}")
        if build_all_wanted(rest) or build_several(rest):
            return logged_command(command, rest, lambda: build_all(rest))
        bash(HERE / "build_image.sh", rest)  # (it logs what it was asked and how it ended itself)
    if command == "update":
        package = packaging()
        if rest[:1] == ["mode"] or any(word in UPDATE_MODE_WORDS for word in rest) and \
                not any(word in ("-h", "--help") for word in rest):
            return logged_command(command, rest, lambda: full_said(command, lambda: update_then_packages(
                module("updates").main, rest)))
        installer = HERE.parent / "install.sh"
        checkout = (HERE.parent / ".git").exists()
        if rest[:1] in (["-h"], ["--help"]) or "--help" in rest:
            # the help is shown whatever this copy is; only an update itself needs an installation
            page = UPDATE_HELP
            if package or not installer.is_file():
                page += f"\n\nThis copy ({package or 'a pip installation'}): {self_update_way()}"
            elif checkout:
                page += ("\n\nThis copy was not installed by install.sh: it is a git checkout; update it with git "
                         "(git pull), then ./install.sh --from . (--check shows the newest release all the same)")
            show_help(page + "\n\nRun 'sandbox-ai --help' for all commands.")
            return 0
        if checkout and not package and "--check" in rest and "--uninstall" not in rest:
            # (only a look: what the release source has, signed by a key of this checkout's - kept as the last check,
            # which the app's check shows; the update itself is git's)
            return logged_command(command, rest, lambda: module("updates").package_main(rest))
        if checkout and not package:
            say("sandbox-ai update: this copy was not installed by install.sh: it is a git checkout; update it with "
                "git (git pull), then install it: ./install.sh --from .")
            return 1
        if package or not installer.is_file():
            # A package or a pip installation replaces itself with its file of the signed release.
            return logged_command(command, rest, lambda: update_then_packages(module("updates").package_main, rest))
        try:
            rest = installation_mode(rest)
        except ValueError as exc:
            say(f"sandbox-ai update: {exc}")
            return 1
        source = None
        with contextlib.suppress(Exception):
            source = module("updates").settings()["source"]
        if source and not any(arg == "--source" or arg.startswith("--source=") for arg in rest) \
                and "--uninstall" not in rest:
            rest = [*rest, "--source", source]  # (the release source update mode was given)
        if "--check" in rest and "--uninstall" not in rest:
            # (install.sh says it as always; what it found is kept as the last check: update mode, the app, the hint)
            return module("updates").manual_check(rest)
        if "--uninstall" not in rest:
            # The package guard's copy of the known malicious packages goes along with an update (signed, one try a
            # day at most; before install.sh takes over this process - it never fails the update).
            with contextlib.suppress(Exception):
                module("updates").sync_packages_along(rest, source_option(rest), say)
        bash(installer, rest)
    say(f"sandbox-ai: unknown command {command!r}; see sandbox-ai --help")
    return 2


def update_then_packages(update, rest):
    """UPDATE(REST) - `sandbox-ai update` of this copy, or its timer's run - and, when it went through, the package
    guard's copy of the known malicious packages along with it (updates.sync_packages_along: signed, one try a day
    at most, never a failure of the update). Not for the update mode's own commands, a --check or a --rollback:
    they fetch nothing."""
    status = update(rest)
    if status == 0 and rest[:1] != ["mode"] and not {"--check", "--rollback"} & set(rest):
        with contextlib.suppress(Exception):
            module("updates").sync_packages_along(rest, source_option(rest), say)
    return status


def source_option(rest):
    """The value of --source among an update's options REST (--source URL, --source=URL), or None."""
    return next((rest[index + 1] for index, word in enumerate(rest[:-1]) if word == "--source"), None) or \
        next((word.split("=", 1)[1] for word in rest if word.startswith("--source=")), None)


UPDATE_MODE_WORDS = ("--apply", "--install", "--rollback", "--scheduled")


def update_hint(command, rest):
    """notify's line on the command line (updates.cli_hint: on a terminal, at most once a day). Never an error."""
    if command in ("update", "__complete", "bridge-stub", "completion") or not (HERE / "updates.py").is_file():
        return
    with contextlib.suppress(Exception):
        if not (sys.stderr.isatty() and sys.stdout.isatty()) or "--json" in rest:
            return  # (nothing is loaded for a program or a pipe)
        module("updates").cli_hint(command, rest)


def ended(exc):
    """The exit status of a command that ended with sys.exit(EXC.code): a text (sys.exit("sandbox-ai X: ...")) is
    said as an error first - red on a terminal - and is status 1, as Python would make it."""
    if isinstance(exc.code, str):
        say(exc.code)
        return 1
    return exc.code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit as exc:
        sys.exit(ended(exc))
