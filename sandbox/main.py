#!/usr/bin/env python3
"""sandbox-ai: one command for everything.

  sandbox-ai claude|codex|opencode|hermes|dsh|qwen|goose [options] [-- agent arguments]   start an agent
  sandbox-ai list|ps [-a] [--flat] [--no-color] [--json] [-H NAME]   what runs and what is kept, grouped by start
  sandbox-ai rename OLD NEW                                          another name for a start
  sandbox-ai attach|pause|resume|stop|delete NAME                    any start by its name (detach: Ctrl-b d)
  sandbox-ai stop --all [--yes]                                      every start of yours on this host, asked first
  sandbox-ai gc [--older-than DAYS] [--dry-run]                      remove leftovers and old kept starts
  sandbox-ai session list|attach|pause|resume|stop|delete ...       the same for sessions (older form)
  sandbox-ai session copy|move NAME HOST [--map L=R]                 a session's configuration started on another host
  sandbox-ai mcp list|start|stop|attach|detach ... [--live]         MCP server sandboxes (attach/detach: a start's, --live: while it runs)
  sandbox-ai login claude|codex|opencode|hermes|qwen | --status      log in inside a sandbox; kept for all sandboxes
  sandbox-ai plugins [claude [--update] | --status | marketplaces]   install the plugins the kits declare; every marketplace
  sandbox-ai kits [list|show|new|add-skill|add-plugin|import-marketplace|...]  see and change kits (skills, plugins)
  sandbox-ai wizard [new|edit|trust|forget|models] ...               project setup
  sandbox-ai build [--pin VERSION] AGENT|all|VARIANT[@AGENT]|team|cairn|vpn|tor|browser  build the images (all: every one)
  sandbox-ai doctor [--backend podman|bwrap|both]                    check the host, settings files and kept sessions
  sandbox-ai logs [DIR|--project PATH]                               blocked egress and file access, with suggestions
  sandbox-ai activity [RUN|--session NAME|--project PATH] [--follow]  what the agents did: prompts, tools, files, network
  sandbox-ai privacy-check [PATH ...]                                what the next push would publish that should stay private
  sandbox-ai net list|show|allow|revoke|untaint|cut ...              change what a running sandbox may reach (cut: the kill switch)
  sandbox-ai devices list|add|remove [RUN] PATH|usb:VID:PID          hand devices to a running sandbox
  sandbox-ai workspace list|add|remove [RUN] DIR|FILE [--ro] [--name NAME]  hand a host folder or file to a running sandbox
  sandbox-ai limits [RUN] --memory|--cpus|--pids ...                 change a running sandbox's limits
  sandbox-ai usage [RUN|--all] [--json] [--follow]                   what a running sandbox uses: CPU, memory, processes
  sandbox-ai approvals list|show|allow|deny|ask-again|blocked|decided|history|remembered|forget ... answer what running sandboxes ask
  sandbox-ai rules check|show|reload ...                              check rulesets, show a start's, reload a running one's
  sandbox-ai canaries list [RUN] | resume RUN                        decoy files of the runs, and what happened to them
  sandbox-ai safeguards [RUN|--all] [--follow] [--json]               what the safeguards refused or ended, why, the fix
  sandbox-ai report [NAME|--session NAME|--all] [-f md|html|pdf] [-o FILE]  one report: runs, stats, network, security
  sandbox-ai secrets list|set|delete|backend|give|revoke ...         API keys and tokens in your keyring; give a run one
  sandbox-ai llm add|list|show|set|params|load|unload|detect ...      LLM servers, local or remote, for --llm
  sandbox-ai llm traffic [RUN|--session NAME] [--follow] [--full]     what an agent and its model exchanged
  sandbox-ai vpn add|list|show|set|remove|check|rotate ...           VPN profiles for --vpn (WireGuard, OpenVPN, Mullvad, Tailscale, Tor)
  sandbox-ai hosts list|add|set|remove|test|update|install|run ...    other machines (LXC containers) managed over SSH
  sandbox-ai team start|list|show|post|stop|delete ...                a team of agents, one sandbox each, with a task board
  sandbox-ai web [serve|user|vendor|cert] ...                         the web interface (HTTPS, password and TOTP)
  sandbox-ai presets list|show|check|dirs ...                         ready setups to start an agent with (--preset NAME)
  sandbox-ai defaults [show|set|unset] ...                            your defaults for every start (defaults.json)
  sandbox-ai git-logins add|list|remove|check|give|revoke ...         Git logins for pushes; give a running sandbox one
  sandbox-ai publish-logins add|list|remove|check|kinds ...           package registry logins, used by the proxy
  sandbox-ai identities add|set|login|publish|list|show|check|remove ...  commit identities with their logins
  sandbox-ai demo-device start|serve|status|stop ...                  a virtual device to try the device rules
  sandbox-ai images [packages|extend|check|new|remove|history|recipe|pull|build-image|list] ...  the agent images,
                                                                      their extensions, your images (services, bases)
  sandbox-ai update [--check] [--version X.Y.Z] ...                  update an installation made by install.sh (signed)
  sandbox-ai version [--verbose]                                     version; --verbose: install place, release keys
  sandbox-ai completion bash|zsh|fish|install                        Tab completion for your shell (install.sh sets it up)

The older single commands (claude-sandbox, sandbox-session, sandbox-mcp, sandbox-wizard, sandbox-build-image,
...) are removed: each is one of the commands above.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
PYTHON_COMMANDS = {"claude": ("agent_sandbox.py", "claude"), "codex": ("agent_sandbox.py", "codex"),
                   "opencode": ("agent_sandbox.py", "opencode"), "hermes": ("agent_sandbox.py", "hermes"),
                   "dsh": ("agent_sandbox.py", "dsh"), "qwen": ("agent_sandbox.py", "qwen"),
                   "goose": ("agent_sandbox.py", "goose"),
                   "session": ("sessions.py",),
                   "list": ("starts.py", "list"), "ps": ("starts.py", "ps"), "rename": ("starts.py", "rename"),
                   "attach": ("starts.py", "attach"), "pause": ("starts.py", "pause"),
                   "resume": ("starts.py", "resume"), "stop": ("starts.py", "stop"), "delete": ("starts.py", "delete"),
                   "gc": ("starts.py", "gc"),
                   "mcp": ("mcp.py",), "wizard": ("wizard.py",), "login": ("login.py",), "plugins": ("plugins.py",), "kits": ("kitctl.py",), "doctor": ("doctor.py",), "logs": ("logs.py",),
                   "activity": ("activity.py",),
                   "privacy-check": ("privacy.py", "check"), "net": ("net.py",),
                   "approvals": ("approvals.py",), "canaries": ("canary.py",), "safeguards": ("safeguards.py",), "secrets": ("secret_store.py",), "llm": ("llm.py",), "vpn": ("vpn.py",), "hosts": ("hosts.py",), "web": ("webui.py",), "defaults": ("defaults_cmd.py",), "team": ("team.py",), "presets": ("presets.py",), "demo-device": ("demo_device.py",), "git-logins": ("git_logins.py",), "publish-logins": ("publish_logins.py",), "identities": ("identities.py",), "completion": ("completion.py",), "__complete": ("completion.py", "__complete"),
                   "images": ("images.py",), "devices": ("devices.py",), "limits": ("devices.py", "--limits"),
                   "workspace": ("workspaces.py",), "usage": ("usage.py",), "rules": ("rules_cmd.py",),
                   "report": ("report.py",)}


def version():
    for line in (HERE / "agent_sandbox.py").read_text().splitlines():
        if line.startswith("__version__ = "):
            return line.split('"')[1]
    return "unknown"


# How a ready-made package of sandbox-ai was made (PACKAGING beside sandbox/, written by its build): it is updated
# by downloading the new package, not by install.sh.
PACKAGES = {"pyz": "sandbox-ai.pyz", "appimage": "the AppImage (sandbox-ai-ARCH.AppImage)",
            "macos-app": "the macOS app (sandbox-ai-ARCH.dmg)"}


def packaging():
    """'pyz', 'appimage', 'macos-app' or None (a checkout, install.sh, pip)."""
    try:
        kind = (HERE.parent / "PACKAGING").read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return kind if kind in PACKAGES else None


def package_update_hint(kind):
    return (f"this sandbox-ai runs from {PACKAGES[kind]}: download the new one from the release page "
            "(https://github.com/c0decave/sandbox-ai/releases, check it with SHA256SUMS), or install with install.sh")


def package_update_check(kind, rest):
    """`update --check` in a package: its version, then the newest release as install.sh --check finds it (with
    the package's own release keys; nothing of an install.sh installation is read or written)."""
    import subprocess
    import tempfile
    print(f"installed: {version()} ({PACKAGES[kind]})")
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-check.") as data:
        result = subprocess.run([find_bash(), str(HERE.parent / "install.sh"), "--check", *rest], text=True,
                                stdout=subprocess.PIPE, env={**os.environ, "XDG_DATA_HOME": data})
    for line in result.stdout.splitlines():
        if not line.startswith("installed:"):
            print(line)
    print(f"update: {package_update_hint(kind)}")
    return result.returncode


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
LOOKING_COMMANDS = {"list", "ps", "logs", "activity", "usage", "safeguards", "doctor", "completion", "__complete",
                    "version", "--version", "-V"}
LOOKING_WORDS = {"list", "ls", "show", "status", "--status", "catalog", "dirs", "history", "blocked", "decided",
                 "remembered", "kinds", "traffic", "explain", "help"}
SAID_LINES = 2  # lines of what a failing command said last (its error), kept in its end line


def only_looks(command, rest):
    return command in LOOKING_COMMANDS or bool(rest[:1]) and rest[0] in LOOKING_WORDS or \
        any(word in ("-h", "--help") for word in rest)


class LastLines:
    """Standard error while a command runs: everything written goes on to STREAM, unchanged; the last few whole
    lines are kept (what a command that fails said last: its error, for launcher.log)."""

    def __init__(self, stream, keep=8):
        self.stream, self.keep, self.lines, self._pending = stream, keep, [], ""

    def write(self, text):
        if isinstance(text, str):
            *whole, self._pending = (self._pending + text).split("\n")
            self._pending = self._pending[-2000:]
            self.lines = [*self.lines, *(line for line in whole if line.strip())][-self.keep:]
        return self.stream.write(text)

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
    if package:
        print(f"package: {package}")
        print(f"updates: {package_update_hint(package)}")
    elif not (HERE.parent / "install.sh").is_file():
        print("updates: not through install.sh (pip install -U sandbox-ai)")
    elif trusted:
        print("updates: must be signed by one of these keys (sandbox-ai update)")
    elif required:
        print("updates: refused: the administrator's policy requires signed updates, but no release key is trusted")
    else:
        print("updates: checked against SHA256SUMS only (no release key yet)")
    if required:
        print("policy: signed updates required (require_signed_updates); --allow-unsigned is refused")
    return 0


HOMES = ("/home/", "/root/", "/Users/", "/var/home/")


UPDATE_HELP = """Usage: sandbox-ai update [--check] [--version X.Y.Z] [--source URL] [--force] [--allow-downgrade]
                         [--user | --system [--prefix DIR]] [--uninstall] [install.sh's other options]

Updates this installation to the newest signed release (install.sh, as the same kind of installation: a system
one under its prefix, a user's own for that user).

  --check            show the installed and the newest version and its signature; change nothing
  --version X.Y.Z    this release instead of the newest
  --source URL       the repository with the releases (Gitea, Forgejo or https://github.com/OWNER/REPO)
  --force            reinstall the same version
  --allow-downgrade  install a release older than the installed one
  --uninstall        remove the installation, its command links and the PATH block
  -h, --help         this help

Other hosts: sandbox-ai hosts update NAME (their own signed update), or sandbox-ai hosts update NAME --from-here
(this machine's copy, no release server needed)."""


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
AGENT_IMAGES = ("codex", "claude", "opencode", "hermes", "dsh", "qwen", "goose")


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


def build_all_wanted(args):
    return build_split(args)[1] == "all"


def build_all(args, run=None, out=None):
    """`sandbox-ai build all`: every image sandbox-ai builds - the seven agents, the team image, Cairn (on the team
    image), the browser, the VPN and Tor images, and every variant of yours (sandbox-ai images) - each a build of its
    own, so one that fails stops none of the others; at the end what was built and what failed. 0 when all were."""
    import subprocess
    run = run or subprocess.run
    out = out or (lambda text: print(text, file=sys.stderr, flush=True))
    options, _, extra = build_split(args)
    if any(option == "--pin" for option, _ in options):
        out("sandbox-ai build: --pin applies to one agent, not all")
        return 2
    allowed = {"agent": {"--podman-url", "--uv", "--node", "--extension-only"},
               "team": {"--podman-url", "--uv", "--node"}, "plain": {"--podman-url"}, "browser": set(),
               "variant": {"--podman-url", "--extension-only"}}
    plan = [(agent, [agent], "agent") for agent in AGENT_IMAGES]
    plan += [("team", ["team"], "team"), ("cairn", ["cairn"], "plain"), ("browser", ["browser"], "browser"),
             ("vpn", ["vpn"], "plain"), ("tor", ["tor"], "plain")]
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_images_for_build", HERE / "images.py")
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
        found = images.variants()  # (a name several agents have: NAME@AGENT)
        plan += [(f"variant {name} ({agent})", ["--variant", images.shown_target(name, agent, found)], "variant")
                 for name, agent in found]
    except Exception as exc:  # noqa: BLE001 - the images themselves are still built
        out(f"sandbox-ai build: the variants could not be read ({exc}); building the others")
    executable = find_bash()
    built, failed = [], []
    for number, (label, target, kind) in enumerate(plan, 1):
        given = [part for option, value in options if option in allowed[kind]
                 for part in ([option] if value is None else [option, value])]
        out(f"sandbox-ai build: all ({number}/{len(plan)}): {label}")
        # A note every build would say alike (no systemd user session: cgroupfs) is said by the first one only.
        status = run([executable, str(HERE / "build_image.sh"), *given, *target, *extra], check=False,
                     env={**os.environ, **({"SANDBOX_AI_BUILD_CGROUP_SAID": "1"} if number > 1 else {})}).returncode
        (built if status == 0 else failed).append(label)
    out(f"sandbox-ai build: all: {len(built)} of {len(plan)} built" +
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
    if argv[:1] == ["help"] and argv[1:2] and argv[1] != "help":
        return main([*argv[1:2], "--help"])  # sandbox-ai help net: the help of that command
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command in REMOVED:
        print(f"sandbox-ai: {command} was removed in 0.1.92; use {REMOVED[command]}", file=sys.stderr)
        return 2
    if command in ("version", "--version", "-V"):
        if rest[:1] in (["--verbose"], ["-v"]):
            return verbose_version()
        print(f"sandbox-ai {version()}")
        return 0
    if command in PYTHON_COMMANDS:
        script, *prefix = PYTHON_COMMANDS[command]
        if script in EXECUTED:  # (the launcher logs its start itself)
            os.execv(sys.executable, [sys.executable, "-I", str(executed_script(script)), *prefix, *rest])
        return logged_command(command, rest, lambda: run_script(HERE / script, [*prefix, *rest]))
    if command == "build":
        os.environ["SANDBOX_AI_PYTHON"] = sys.executable  # the extension layers are built by images.py
        if os.geteuid() == 0 and not rest[:1] in (["-h"], ["--help"]):
            # Rootless Podman keeps one image store per user: root's build is not seen by a user's start.
            who = os.environ.get("SUDO_USER")
            print("sandbox-ai build: note: built as root, the image goes into root's Podman store "
                  "(/var/lib/containers/storage); starts by another user do not see it - run sandbox-ai build as "
                  f"the user who starts the sandboxes{' (' + who + ')' if who and who != 'root' else ''}",
                  file=sys.stderr)
        if build_all_wanted(rest):
            return logged_command(command, rest, lambda: build_all(rest))
        bash(HERE / "build_image.sh", rest)  # (it logs what it was asked and how it ended itself)
    if command == "update":
        package = packaging()
        if rest[:1] in (["-h"], ["--help"]) or "--help" in rest:
            # the help is shown whatever this copy is; only an update itself needs install.sh's installation
            print(UPDATE_HELP)
            installer = HERE.parent / "install.sh"
            if package:
                print(f"\nThis copy: {package_update_hint(package)}")
            elif not installer.is_file() or (HERE.parent / ".git").exists():
                print("\nThis copy was not installed by install.sh: update a checkout with git, a pip installation "
                      "with: pip install -U sandbox-ai")
            return 0
        if package and "--check" in rest:
            return package_update_check(package, [arg for arg in rest if arg != "--check"])
        if package:
            print(f"sandbox-ai update: {package_update_hint(package)}", file=sys.stderr)
            return 1
        installer = HERE.parent / "install.sh"
        if not installer.is_file() or (HERE.parent / ".git").exists():
            print("sandbox-ai update: this copy was not installed by install.sh; update a checkout with git, "
                  "a pip installation with: pip install -U sandbox-ai", file=sys.stderr)
            return 1
        try:
            rest = installation_mode(rest)
        except ValueError as exc:
            print(f"sandbox-ai update: {exc}", file=sys.stderr)
            return 1
        bash(installer, rest)
    print(f"sandbox-ai: unknown command {command!r}; see sandbox-ai --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
