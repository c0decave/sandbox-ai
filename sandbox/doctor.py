#!/usr/bin/env python3
"""sandbox-ai doctor: check the host for everything the sandboxes need and say what to fix.

Every check returns (status, title, detail, hint) with status "ok", "warn", "fail" or "skip".
Nothing is changed; with --backend podman (default) only Podman requirements can fail.
"""
import argparse
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
AGENTS = ("codex", "claude", "opencode")
_launcher = None
_modules = {}


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_doctor", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


def run(cmd, timeout=30):
    """(returncode, stdout) of CMD; 127 if it cannot run."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def check_user():
    if os.geteuid() == 0:
        return "fail", "Ordinary user", "running as root", "run sandbox-ai as your normal user, never with sudo"
    return "ok", "Ordinary user", getpass.getuser(), ""


def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 9):
        return "fail", "Python", version, "install Python 3.9 or newer"
    return "ok", "Python", version, ""


def subid_range(path, user, uid):
    """Largest delegated range for USER (or UID) in /etc/subuid or /etc/subgid, or 0."""
    best = 0
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.strip().split(":")
            if len(parts) == 3 and parts[0] in (user, str(uid)) and parts[2].isdigit():
                best = max(best, int(parts[2]))
    except OSError:
        pass
    return best


def check_subids(user=None, uid=None, root=Path("/etc")):
    user, uid = user or getpass.getuser(), os.getuid() if uid is None else uid
    sizes = {name: subid_range(root / name, user, uid) for name in ("subuid", "subgid")}
    small = [name for name, size in sizes.items() if size < 65536]
    if small:
        return ("fail", "Delegated UID/GID ranges", ", ".join(f"/etc/{n}: {sizes[n]}" for n in sizes),
                f"sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 {user}")
    return "ok", "Delegated UID/GID ranges", f"{sizes['subuid']} UIDs, {sizes['subgid']} GIDs", ""


def check_userns(proc=Path("/proc/sys")):
    try:
        if int((proc / "user/max_user_namespaces").read_text()) == 0:
            return "fail", "User namespaces", "user.max_user_namespaces is 0", "sudo sysctl user.max_user_namespaces=15000"
    except (OSError, ValueError):
        pass
    restrict = proc / "kernel/apparmor_restrict_unprivileged_userns"
    try:
        if restrict.read_text().strip() == "1":
            return ("warn", "User namespaces", "AppArmor restricts unprivileged user namespaces",
                    "Podman usually has a profile; for bwrap allow it in AppArmor or use --backend podman")
    except OSError:
        pass
    return "ok", "User namespaces", "available", ""


def podman_base(podman_url):
    return ["podman", *(["--remote", "--url", podman_url] if podman_url else [])]


def check_podman(podman_url=None):
    if not shutil.which("podman"):
        return "fail", "Podman", "not found", "install podman (rootless) and pasta (package passt)"
    code, rootless = run([*podman_base(podman_url), "info", "--format", "{{.Host.Security.Rootless}}"])
    if code != 0:
        return "fail", "Podman", "podman info failed", "check `podman info`; for a remote engine pass --podman-url"
    if rootless != "true":
        return "fail", "Podman", "not rootless", "run Podman as your user (rootless), not as root"
    code, version = run([*podman_base(podman_url), "version", "--format", "{{.Client.Version}}"])
    return "ok", "Podman", f"rootless, {version or 'version unknown'}", ""


def check_pasta(podman_url=None):
    if podman_url:
        return "skip", "pasta", "remote engine", ""
    if shutil.which("pasta"):
        return "ok", "pasta", shutil.which("pasta"), ""
    return "fail", "pasta", "not found", "install pasta (package passt); the default network needs it"


def check_image(agent, podman_url=None):
    image = f"localhost/{agent}-sandbox:latest"
    title = f"Image {agent}"
    if not shutil.which("podman"):
        return "skip", title, "no podman", ""
    code, version = run([*podman_base(podman_url), "image", "inspect", image, "--format",
                         '{{index .Labels "sandbox-ai.agent-version"}}'])
    if code != 0:
        return "warn", title, f"{image} missing", f"sandbox-build-image {agent}"
    if not version or version == "<no value>":
        return "ok", title, f"{image} (built before version labels)", f"sandbox-build-image {agent} to update"
    return "ok", title, f"{image}, {agent} {version}", ""


def check_bwrap_tools():
    tools = ["bwrap", "firejail", "nft", "setpriv", "newuidmap", "newgidmap", "slirp4netns"]
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        return "fail", "Bubblewrap tools", "missing: " + ", ".join(missing), "install them, or use --backend podman"
    return "ok", "Bubblewrap tools", "all present", ""


def check_tiocsti(sysctl=Path("/proc/sys/dev/tty/legacy_tiocsti")):
    try:
        enabled = sysctl.read_text().strip() != "0"
    except OSError:
        enabled = True
    if enabled:
        return ("fail", "TIOCSTI (bwrap)", "terminal input injection allowed",
                "sudo sysctl dev.tty.legacy_tiocsti=0 (kernel 6.2+), or use --backend podman")
    return "ok", "TIOCSTI (bwrap)", "disabled", ""


def check_login(agent):
    """The login the agent's sandboxes get (sandbox-ai's login store first, else the host's);
    never prints token contents."""
    l = launcher()
    where, problem = l.login_state(agent)
    source = l.auth_sources(agent)[0][0]
    title = f"Login {agent}" + (" (login store)" if where == "store" else "")
    if problem == "missing":
        return "warn", title, f"{source}: no login", f"sandbox-ai login {agent} (or log in on the host)"
    if problem == "expired":
        return "warn", title, f"{source}: login expired", f"sandbox-ai login {agent}"
    if agent != "claude":
        return "ok", title, str(source), ""
    oauth = l.claude_oauth(source, follow=where == "host")
    hours = (oauth["expiresAt"] / 1000 - time.time()) / 3600
    state = f"access token valid for {hours:.1f} h" if hours > 0 else "access token expired (refreshes on first use)"
    return "ok", title, state, ""


def check_git_identity():
    """Exactly what --git-identity would use (global scope, from HOME, without GIT_* overrides)."""
    try:
        name, email = launcher().host_git_identity()
    except ValueError:
        return ("warn", "Git identity", "no usable global user.name/user.email",
                "git config --global user.name ...; needed only for --git-identity")
    return "ok", "Git identity", f"{name} <{email}>", ""


def check_installation():
    version = launcher().__version__
    checkout = (HERE.parent / ".git").exists()
    where = "checkout" if checkout else str(HERE.parent)
    hint = "install it (./install.sh --from .) and never use the checkout as a workspace" if checkout else ""
    return ("warn" if checkout else "ok"), "sandbox-ai", f"{version} ({where})", hint


def kits():
    if "kits" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_kits_for_doctor", HERE / "kits.py")
        _modules["kits"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_modules["kits"])
    return _modules["kits"]


def check_kit(agent):
    """What the default kit of AGENT gives every sandbox: skills, agents, commands, prompts, memory template."""
    k = kits()
    home, title = k.default_home(agent), f"Kit {agent}"
    if not home.is_dir():
        return "skip", title, f"{home}: created with examples on the first start", ""
    try:
        return "ok", title, f"{home}: {k.describe(agent, 'home', home)}", ""
    except (OSError, ValueError) as exc:
        return "warn", title, str(exc), "fix the kit; sandboxes of this agent refuse to start until then"


def check_kits():
    """The named kits (--kit NAME) and what they hold per agent."""
    k = kits()
    names = k.available_kits()
    if not names:
        return "skip", "Kits", f"none in {k.kits_dir()}", ""
    parts, problems = [], []
    for name in names:
        for agent in AGENTS:
            directory = k.kits_dir() / name / agent
            if directory.is_dir():
                try:
                    parts.append(f"{name}/{agent}: {k.describe(agent, f'kit {name}', directory)}")
                except (OSError, ValueError) as exc:
                    problems.append(str(exc))
    if problems:
        return "warn", "Kits", "; ".join(problems), "fix the kit; --kit with it refuses to start until then"
    return "ok", "Kits", "; ".join(parts) or f"{', '.join(names)}: no agent directories", ""


def check_plugins():
    """Claude plugins: what the kits declare against what the plugin store holds."""
    k = kits()
    try:
        known, installed = k.installed()
        marketplaces, plugins, _ = k.declarations("claude", k.all_plugin_files("claude"))
    except (OSError, ValueError) as exc:
        return "warn", "Plugins claude", str(exc), "fix marketplaces.json"
    detail = (f"marketplaces: {', '.join(sorted(known)) or 'none'}; "
              f"plugins: {', '.join(sorted(installed)) or 'none'}")
    missing = [p for p in plugins if p not in installed] + [m for m in marketplaces if m not in known]
    if missing:
        return "warn", "Plugins claude", f"{detail}; declared, not installed: {', '.join(missing)}", \
            "sandbox-ai plugins claude"
    return "ok", "Plugins claude", detail, ""


def check_memory(cwd=None):
    """The memory sandbox-ai keeps for the project in the current directory, per agent."""
    k = kits()
    project = Path(cwd or os.getcwd()).resolve()
    parts = []
    for agent in AGENTS:
        store = k.memory_store(project, agent)
        if store.is_dir() and not store.is_symlink():
            try:
                files, _, _ = k.collect(store, agent)
            except (OSError, ValueError) as exc:
                return "warn", "Project memory", f"{store}: {exc}", "check the directory's permissions"
            names = sorted(Path(name).name for name in files)
            parts.append(f"{agent}: {len(files)} file(s)" + (f" ({', '.join(names[:5])}"
                                                            f"{', ...' if len(names) > 5 else ''})" if names else ""))
    if not parts:
        return "skip", "Project memory", f"none yet for {project}", ""
    return "ok", "Project memory", f"{k.memory_store(project, 'claude').parent}: " + "; ".join(parts), ""


def check_project(cwd=None):
    l = launcher()
    path = Path(cwd or os.getcwd()) / l.CONFIG_NAME
    if not path.exists():
        return "skip", "Project configuration", f"none in {path.parent}", ""
    try:
        trusted = l.trusted_configs().get(str(l.config_key(path)))
        if trusted != hashlib.sha256(l.read_config_bytes(path)).hexdigest():
            return ("warn", "Project configuration", f"{path} not trusted",
                    f"sandbox-wizard trust {shlex.quote(str(path))}")
    except (OSError, ValueError) as exc:
        return "warn", "Project configuration", l.printable(exc), ""
    return "ok", "Project configuration", f"{path} trusted", ""


def checks(backend="podman", podman_url=None, cwd=None):
    result = [check_user(), check_python(), check_installation(), check_userns()]
    if backend in ("podman", "both"):
        result += [check_podman(podman_url), check_pasta(podman_url)]
        if not podman_url:
            result.append(check_subids())
        result += [check_image(agent, podman_url) for agent in AGENTS]
    if backend in ("bwrap", "both"):
        result += [check_bwrap_tools(), check_tiocsti()]
        if backend == "bwrap":
            result.append(check_subids())
    result += [check_login(agent) for agent in AGENTS]
    result += [check_kit(agent) for agent in AGENTS] + [check_kits(), check_plugins(), check_memory(cwd)]
    result += [check_git_identity(), check_project(cwd)]
    return result


SYMBOLS = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "--  "}


def report(results, out=print):
    width = max(len(title) for _, title, _, _ in results)
    for status, title, detail, hint in results:
        out(f"{SYMBOLS[status]}  {title.ljust(width)}  {launcher().printable(detail)}")
        if hint and status in ("warn", "fail"):
            out(f"{' ' * (width + 8)}-> {launcher().printable(hint)}")
    failed = sum(status == "fail" for status, *_ in results)
    warned = sum(status == "warn" for status, *_ in results)
    out("")
    out(f"{failed} problem(s), {warned} warning(s)" if failed or warned else "Everything needed is in place.")
    return 1 if failed else 0


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-ai doctor", description="Check the host for sandbox-ai.")
    parser.add_argument("--backend", choices=["podman", "bwrap", "both"], default="podman")
    parser.add_argument("--podman-url")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    a = parser.parse_args(argv)
    results = checks(a.backend, a.podman_url)
    if a.json:
        print(json.dumps([dict(zip(("status", "check", "detail", "hint"), r)) for r in results], indent=2))
        return 1 if any(r[0] == "fail" for r in results) else 0
    return report(results)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
