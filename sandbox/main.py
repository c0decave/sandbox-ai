#!/usr/bin/env python3
"""sandbox-ai: one command for everything.

  sandbox-ai claude|codex|opencode [options] [-- agent arguments]   start an agent
  sandbox-ai list|ps [--all] [--json] [--history NAME]               what runs and what is kept: every start
  sandbox-ai rename OLD NEW                                          another name for a start
  sandbox-ai attach|pause|resume|stop|delete NAME                    any start by its name (detach: Ctrl-b d)
  sandbox-ai gc [--older-than DAYS] [--dry-run]                      remove leftovers and old kept starts
  sandbox-ai session list|attach|pause|resume|stop|delete ...       the same for sessions (older form)
  sandbox-ai mcp list|start|stop|attach ...                         MCP server sandboxes (attach: to a start)
  sandbox-ai login claude|codex|opencode | --status                  log in inside a sandbox; kept for all sandboxes
  sandbox-ai plugins [claude [--update] | --status]                  install the plugins the kits declare
  sandbox-ai kits [list|show|new|add-skill|new-skill|add-plugin|...] see and change kits (skills, plugins)
  sandbox-ai wizard [new|edit|trust|forget|models] ...               project setup
  sandbox-ai build [--pin VERSION] AGENT|all|VARIANT|team|vpn ...    build the images and their extensions
  sandbox-ai doctor [--backend podman|bwrap|both]                    check the host
  sandbox-ai logs [DIR|--project PATH]                               blocked egress and file access, with suggestions
  sandbox-ai activity [RUN|--session NAME|--project PATH] [--follow]  what the agents did: prompts, tools, files, network
  sandbox-ai privacy-check [PATH ...]                                what the next push would publish that should stay private
  sandbox-ai net list|show|allow|revoke|untaint ...                  change what a running sandbox may reach
  sandbox-ai devices list|add|remove [RUN] PATH|usb:VID:PID          hand devices to a running sandbox
  sandbox-ai workspace list|add|remove [RUN] DIR [--ro]             hand a host directory to a running sandbox
  sandbox-ai limits [RUN] --memory|--cpus|--pids ...                 change a running sandbox's limits
  sandbox-ai approvals list|allow|deny|history|remembered|forget ... answer what running sandboxes ask
  sandbox-ai canaries list [RUN] | resume RUN                        decoy files of the runs, and what happened to them
  sandbox-ai secrets list|set|delete|backend ...                     API keys and tokens in your keyring
  sandbox-ai llm add|list|show|remove|models|check ...                LLM servers, local or remote, for --llm
  sandbox-ai vpn add|list|show|remove|check ...                      VPN profiles for --vpn (WireGuard, OpenVPN, Mullvad, Tailscale)
  sandbox-ai hosts list|add|set|remove|test|update|run ...            other machines (LXC containers) managed over SSH
  sandbox-ai team start|list|show|post|stop|delete ...                a team of agents, one sandbox each, with a task board
  sandbox-ai web [serve|user|vendor|cert] ...                         the web interface (HTTPS, password and TOTP)
  sandbox-ai presets list|show|check|dirs ...                         ready setups to start an agent with (--preset NAME)
  sandbox-ai defaults [show|set|unset] ...                            your defaults for every start (defaults.json)
  sandbox-ai git-logins add|list|remove|check ...                     Git logins for pushes, used by the proxy
  sandbox-ai publish-logins add|list|remove|check|kinds ...           package registry logins, used by the proxy
  sandbox-ai identities add|set|login|publish|list|show|check|remove ...  commit identities with their logins
  sandbox-ai demo-device start|serve|status|stop ...                  a virtual device to try the device rules
  sandbox-ai images [packages|extend|check|new|remove|history|recipe] ...  the agent images, their contents and extensions
  sandbox-ai update [--check] [--version X.Y.Z] ...                  update an installation made by install.sh (signed)
  sandbox-ai version [--verbose]                                     version; --verbose: install place, release keys
  sandbox-ai completion bash|zsh|fish|install                        Tab completion for your shell (install.sh sets it up)

The older single commands (claude-sandbox, sandbox-session, sandbox-mcp, sandbox-wizard,
sandbox-build-image, ...) still work but are deprecated: they print a note (SANDBOX_AI_NO_DEPRECATION=1 hides
it) and will be removed in a later version.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
PYTHON_COMMANDS = {"claude": ("agent_sandbox.py", "claude"), "codex": ("agent_sandbox.py", "codex"),
                   "opencode": ("agent_sandbox.py", "opencode"), "session": ("sessions.py",),
                   "list": ("starts.py", "list"), "ps": ("starts.py", "list"), "rename": ("starts.py", "rename"),
                   "attach": ("starts.py", "attach"), "pause": ("starts.py", "pause"),
                   "resume": ("starts.py", "resume"), "stop": ("starts.py", "stop"), "delete": ("starts.py", "delete"),
                   "gc": ("starts.py", "gc"),
                   "mcp": ("mcp.py",), "wizard": ("wizard.py",), "login": ("login.py",), "plugins": ("plugins.py",), "kits": ("kitctl.py",), "doctor": ("doctor.py",), "logs": ("logs.py",),
                   "activity": ("activity.py",),
                   "privacy-check": ("privacy.py", "check"), "net": ("net.py",),
                   "approvals": ("approvals.py",), "canaries": ("canary.py",), "secrets": ("secret_store.py",), "llm": ("llm.py",), "vpn": ("vpn.py",), "hosts": ("hosts.py",), "web": ("webui.py",), "defaults": ("defaults_cmd.py",), "team": ("team.py",), "presets": ("presets.py",), "demo-device": ("demo_device.py",), "git-logins": ("git_logins.py",), "publish-logins": ("publish_logins.py",), "identities": ("identities.py",), "completion": ("completion.py",), "__complete": ("completion.py", "__complete"),
                   "images": ("images.py",), "devices": ("devices.py",), "limits": ("devices.py", "--limits"),
                   "workspace": ("workspaces.py",)}


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


def installation_mode(rest, here=None, euid=None, writable=None):
    """`update` ARGS with the kind of installation this copy is (install.sh decides by who runs it: root makes a
    system installation, anyone else one of their own): a system one (PREFIX/lib/sandbox-ai/versions/X) is
    updated as such (--system --prefix PREFIX), a user's own stays theirs when root updates it (--user). A
    ValueError when a system installation would be updated by someone who cannot write it."""
    here = Path(here or HERE)
    euid = os.geteuid() if euid is None else euid
    writable = writable or (lambda path: os.access(path, os.W_OK))
    given = any(arg in ("--user", "--system", "--prefix", "--uninstall") or arg.startswith("--prefix=")
                for arg in rest)
    if given or "--check" in rest or here.parent.parent.name != "versions":
        return list(rest)
    data = here.parent.parent.parent
    if data.name == "sandbox-ai" and data.parent.name == "lib" and not str(data).startswith(HOMES):
        prefix = data.parent.parent
        if euid != 0 and not writable(data):
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


def bash(script, args):
    executable = find_bash()
    os.execv(executable, [executable, str(script), *args])


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command in ("version", "--version", "-V"):
        if rest[:1] in (["--verbose"], ["-v"]):
            return verbose_version()
        print(f"sandbox-ai {version()}")
        return 0
    if command in PYTHON_COMMANDS:
        script, *prefix = PYTHON_COMMANDS[command]
        os.execv(sys.executable, [sys.executable, "-I", str(HERE / script), *prefix, *rest])
    if command == "build":
        os.environ["SANDBOX_AI_PYTHON"] = sys.executable  # the extension layers are built by images.py
        bash(HERE / "build_image.sh", rest)
    if command == "update":
        package = packaging()
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
