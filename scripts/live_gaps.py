#!/usr/bin/env python3
"""Every live check the development sandbox cannot run, in one command, on your own Linux machine or Mac
(docs/LIVE_CHECK.md).

    scripts/live_check.sh                     the default checks (about 15-30 minutes; images must be built)
    scripts/live_check.sh --list              the checks: what each proves, what it needs, what it sends where
    scripts/live_check.sh --only NAME[,NAME]  only these (repeatable; the optional ones too)
    scripts/live_check.sh --build             build the missing images first (codex, claude, opencode)
    scripts/live_check.sh --host NAME         also the remote MCP check on that host of `sandbox-ai hosts`
    scripts/live_check.sh --llm NAME [--yes]  also the local-model smoke runs on that server of `sandbox-ai llm`
    scripts/live_check.sh --device PATH       the device for devices-live (default: one `devices scan` found)
    scripts/live_check.sh --dmg-old A --dmg-new B   on a Mac: also the update of sandbox-ai.app from disk image A to
                                              B and back (each a .dmg, or the folder of a downloaded release)

Run it as yourself on the host (never with root or sudo, never inside a sandbox): rootless Podman and user
namespaces on Linux; on a Mac the macOS checks (macos-*: doctor, devices, the app, its update and rollback) - the
Linux checks are skipped there and say so. Your own sandbox-ai stays untouched: every check runs with a HOME,
configuration, state, data, cache and runtime folder of its own in a temporary folder, its own throwaway workspaces,
and its own sessions, stopped and deleted at the end. Podman alone sees your HOME (a small wrapper first on PATH), so your images are used and your
containers are never touched. No secret store of yours is read: the checks' secrets are plain files of their own.

The report - REPORT-DATE.md and REPORT-DATE.json in live-check/ of this checkout (--out DIR) - says per check
passed, failed or skipped, the exact command, every test's outcome and the last lines of its output, with secrets
and home paths replaced. The exit status is 0 whatever the checks found (2: refused to run, a wrong option).
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SAI = ROOT / "sandbox-ai"
GAPS = "tests/test_live_gaps.py"
TAIL = 60            # lines of a check's output in the report
DETAIL = 3000        # characters of one failing test's message in the report
REPORT_VERSION = 1


def load_live_check():
    """scripts/live_check.py as a module (its junit reader, its pytest venv, its container test)."""
    spec = importlib.util.spec_from_file_location("live_check_for_gaps", Path(__file__).with_name("live_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Check(NamedTuple):
    name: str
    proves: str
    needs: tuple            # prerequisite keys (PREREQUISITES), all of them needed
    tests: tuple            # pytest arguments: node ids or files
    network: str = ""       # what leaves this machine ("" nothing)
    optional: bool = False  # not in a run without --only (or its own option)
    keyword: str = ""       # pytest -k
    podman_only: bool = False
    timeout: int = 1800
    source: str = ""        # where the gap was named


CHECKS = (
    Check("release-gates",
          "Security reviews A and B's release gates in a real sandbox: sandbox-ai's system call filters beside "
          "Firejail's, nosuid/nodev mounts, Firejail's sealed folder and the run's manifest; --mode high starts with "
          "every part of its isolation; the terminal relays cannot be replaced from inside; --untrusted confines a "
          "command",
          ("sandbox", "image:codex"), ("tests/test_agent_sandbox_live.py",),
          keyword="isolation or strict_mode_starts or relays", timeout=2400,
          source="docs/reviews/security-review-fixes-A.md, -B.md"),
    Check("firejail-hides-control",
          "The agent and `sandbox-ai shell` cannot list or read /sandbox/proxy and /sandbox/control (Firejail's "
          "blacklist in effect), while /sandbox/session stays readable",
          ("sandbox", "image:codex"), (f"{GAPS}::test_live_firejail_hides_the_proxy_and_control_folders",),
          source="docs/reviews/credentials-verify-A.md, -B.md (untested)"),
    Check("firewall-login-hosts",
          "A Git login's server (--git-token-env https://codeberg.org=...) is opened and no other host is, with the "
          "connection proxy and with the firewall alone (--no-proxy)",
          ("sandbox", "image:codex"), (f"{GAPS}::test_live_a_logins_server_is_opened_and_no_other_host",),
          network="HTTPS to codeberg.org (one GET of its front page, a made-up token that is never sent); gitlab.com "
                  "is tried and must be refused before it leaves",
          source="docs/reviews/credentials-verify-B.md (nftables)"),
    Check("git-login-give",
          "A Git login given to a running sandbox (git-logins give --env) makes its git push work against a Git "
          "server that wants a login, and stops working once revoked; the token is in no listing",
          ("podman", "image:codex", "git"),
          (f"{GAPS}::test_live_a_git_login_given_while_it_runs_reaches_git_until_revoked",), podman_only=True,
          source="docs/reviews/credentials-verify-A.md, -B.md (live containers)"),
    Check("hide-workspace-add",
          "--hide applies to a workspace added during the run (sandbox-ai workspace add): the matching files are "
          "empty and read-only in the sandbox, the others readable, the host's files unchanged",
          ("sandbox", "image:codex"), (f"{GAPS}::test_live_hide_covers_a_workspace_added_while_it_runs",),
          source="knostic-gaps item 5"),
    Check("untrusted-home",
          "--untrusted (Landlock) under the real kernel: inside a sandbox with a crowded HOME and a credential "
          "folder reached through a link; and on this host with your real HOME (only whether each credential "
          "folder can be opened is printed, never a byte of it)",
          ("sandbox", "image:codex", "linux"),
          (f"{GAPS}::test_live_untrusted_with_a_crowded_and_linked_home_in_a_sandbox",
           f"{GAPS}::test_live_untrusted_with_this_hosts_real_home"),
          source="docs/releases/v0.1.99.md (--untrusted with a crowded or linked HOME)"),
    Check("kits-attach",
          "sandbox-ai kits attach gives a skill and a command to a running Claude Code sandbox: there at once, "
          "read-only, gone after kits detach",
          ("sandbox", "image:claude"), (f"{GAPS}::test_live_kits_attach_reaches_a_running_claude_sandbox",),
          source="runtime-kits"),
    Check("plugins-attach",
          "sandbox-ai plugins attach installs a plugin of the built-in marketplace (sandcastle@sandbox-ai) into "
          "the plugin store and enables it in a running Claude Code sandbox; plugins detach turns it off",
          ("sandbox", "image:claude"), (f"{GAPS}::test_live_plugins_attach_enables_a_plugin_in_a_running_claude",),
          timeout=2400, source="runtime-kits"),
    Check("devices-scan",
          "sandbox-ai devices scan on this machine's real hardware: valid JSON, every listed node exists, every "
          "refusal has a reason; the text form works too",
          ("linux",), (f"{GAPS}::test_live_devices_scan_on_this_machine",), timeout=300,
          source="board-devices"),
    Check("devices-live",
          "A device (--device, or one devices scan offers) added to a running sandbox appears there, devices list "
          "says so, and is gone after devices remove",
          ("sandbox", "image:codex", "linux", "device"),
          (f"{GAPS}::test_live_a_device_added_and_removed_while_it_runs",), source="board-devices"),
    Check("remote-mcp",
          "A real host of sandbox-ai hosts: hosts test, its MCP servers listed over SSH, its demo server started "
          "and stopped there (only when no instance of it runs)",
          ("host",), (f"{GAPS}::test_live_remote_mcp_on_a_real_host",), optional=True, timeout=1200,
          network="SSH to the host given with --host (sandbox-ai there; its demo MCP server)",
          source="remote MCP (docs/reviews/remote-mcp-board.md)"),
    Check("local-llm",
          "One smoke run (a file written with the agent's own tool, a reply) of each of the three best models "
          "x Claude Code, OpenCode and Codex through the real launcher with --llm",
          ("sandbox", "llm", "image:claude", "image:codex", "image:opencode"),
          (f"{GAPS}::test_live_local_model_smoke_run",), optional=True, timeout=4 * 3600,
          network="prompts to your LLM server given with --llm, nothing else (--no-provider-defaults)",
          source="research-llm/LOCAL_LLM_REPORT.md section 7"),
    Check("live-suites",
          "Every other live suite that needs no login (scripts/live_check.py's: the proxy, push check, network "
          "changes, devices, MCP, limits, approvals, namespaces, an escape probe)",
          ("sandbox", "image:codex"), (), optional=True, timeout=4 * 3600,
          network="what those suites reach: api.github.com and github.com (git ls-remote), the hosts their tests "
                  "allow",
          source="scripts/live_check.py"),
)
MAC = "tests/test_live_macos.py"
# The checks of a real Mac (macOS, podman machine, the app built by CI): Linux skips them and says so.
MAC_CHECKS = (
    Check("macos-doctor",
          "sandbox-ai doctor on this Mac: its macOS set (podman machine in place of user namespaces, subordinate ids, "
          "systemd, pasta, Landlock and Bubblewrap - none of those), every check with a state, and its verdict on "
          "podman machine as `podman machine inspect` says it (running, sharing HOME and $TMPDIR); the same through "
          "the app's command line (macos-app's app)",
          ("macos",), (f"{MAC}::test_live_macos_doctor",), timeout=600,
          source="doctor's macOS checks (tests/test_doctor_macos.py: stand-ins only)"),
    Check("macos-devices",
          "sandbox-ai devices scan on this Mac: valid JSON, every listed node exists, nothing offered for --device "
          "that a start here refuses; a start with --device is refused with the reason (the sandbox runs in podman "
          "machine's Linux VM)",
          ("macos",), (f"{MAC}::test_live_macos_devices",), timeout=300,
          source="board-devices on macOS (stand-ins only)"),
    Check("macos-app",
          "sandbox-ai.app (--app, else the one in --dmg-new): its signature holds (codesign --verify), its command "
          "line says its version and that it is the macOS app, the app starts against the sandbox-ai inside it on "
          "this Mac's own display and quits (--self-test)",
          ("macos", "app"), (f"{MAC}::test_live_macos_app_starts",), timeout=900,
          source="the macOS .app (built by CI, started by no test)"),
    Check("macos-update",
          "sandbox-ai.app of --dmg-old, put into a folder of the check's, updates itself to --dmg-new with "
          "`sandbox-ai update` (hdiutil, ditto: the new app's signature holds) from a release the check serves on "
          "127.0.0.1 - SHA256SUMS signed by the release's key (a downloaded release) or by a key of the check's "
          "(a local build) - and `update --rollback` brings the old one back; then the same with the old app's "
          "own Install now… and Roll back… (Settings > Updates, on its own Python and PySide6)",
          ("macos", "dmgs"), (f"{MAC}::test_live_macos_dmg_update_and_rollback",
                              f"{MAC}::test_live_macos_app_install_now_and_roll_back"), timeout=2400,
          source="0.1.154's updates of the macOS app (tests/test_update_github.py: hdiutil, ditto and codesign "
                 "stand-ins)"),
)
CHECKS = CHECKS + MAC_CHECKS
BY_NAME = {check.name: check for check in CHECKS}
LLM_MODELS = ("qwen3.8-flash-next", "ornith-1.5-35b-a3b-mlx", "ornith-1.5-122b-mlx")
IMAGES = ("codex", "claude", "opencode")
DEVICE_KINDS = ("serial", "video", "render", "kvm")  # devices-live's own pick, in this order
# Variables of the user's environment that never reach the checks (their sandbox-ai settings and runs).
KEPT_SANDBOX_AI = ("SANDBOX_AI_PODMAN_URL",)
# The folders of the check's own; Podman (the wrapper) gets the user's back.
XDG = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")


# --- the checks' own environment ------------------------------------------------------------------------------------

def podman_wrapper(directory, real, podman):
    """DIRECTORY/podman: runs PODMAN with REAL's HOME and XDG folders (unset where REAL has none), so the user's
    image storage, configuration and rootless runtime are Podman's while sandbox-ai runs with the check's own. The
    path of the file written."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/sh", "# scripts/live_gaps.py: Podman with your own HOME, configuration and image storage; the "
             "check's sandbox-ai has its own."]
    for name in ("HOME",) + XDG:
        value = real.get(name)
        lines.append(f"{name}={shlex.quote(value)}; export {name}" if value else f"unset {name}")
    lines.append(f"exec {shlex.quote(str(podman))} \"$@\"")
    path = directory / "podman"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o700)
    return path


def isolated_environment(root, base=None, home=True, podman=None):
    """The environment of a check: BASE (default os.environ) without its SANDBOX_AI_* settings (but
    KEPT_SANDBOX_AI), with HOME (when HOME is true) and every XDG folder in ROOT, secrets kept in plain files of its
    own (never the system keyring), and ROOT/bin first on PATH with the Podman wrapper (PODMAN: the podman to wrap,
    default the one on BASE's PATH; none found: no wrapper)."""
    base = dict(os.environ if base is None else base)
    root = Path(root)
    env = {key: value for key, value in base.items()
           if not key.startswith("SANDBOX_AI_") or key in KEPT_SANDBOX_AI}
    folders = {"HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state",
               "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "run"}
    for name, folder in folders.items():
        if name == "HOME" and not home:
            continue
        path = root / folder
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[name] = str(path)
    podman = podman or shutil.which("podman", path=base.get("PATH"))
    if podman:
        bin_dir = podman_wrapper(root / "bin", base, podman).parent
        env["PATH"] = os.pathsep.join([str(bin_dir), base.get("PATH") or os.defpath])
    env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1", SANDBOX_AI_LIVE_CHECK="1")
    return env


# --- prerequisites ------------------------------------------------------------------------------------------------

class Found(NamedTuple):
    ok: bool
    detail: str


def run_command(command, timeout=60, env=None, cwd=None, input=None):
    """(status, stdout, stderr); 127 when the program is missing, 124 after TIMEOUT."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd,
                              input=input, stdin=None if input is not None else subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr
    except FileNotFoundError:
        return 127, "", f"{command[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"


def read_number(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def user_namespaces(proc="/proc", runner=run_command):
    """Found: whether this user may make user namespaces (rootless Podman and Bubblewrap need them)."""
    proc = Path(proc)
    limit = read_number(proc / "sys/user/max_user_namespaces")
    if limit == 0:
        return Found(False, "user.max_user_namespaces is 0: sudo sysctl -w user.max_user_namespaces=28633 (and in "
                            "/etc/sysctl.d to keep it)")
    clone = read_number(proc / "sys/kernel/unprivileged_userns_clone")
    if clone == 0:
        return Found(False, "kernel.unprivileged_userns_clone is 0: sudo sysctl -w kernel.unprivileged_userns_clone=1")
    restricted = read_number(proc / "sys/kernel/apparmor_restrict_unprivileged_userns") == 1
    status, _, err = runner(["unshare", "--user", "--map-root-user", "true"], timeout=30)
    if status == 127:
        return Found(True, "unshare is not installed: not tried")
    if status != 0 and restricted:
        # Ubuntu 24.04 and later: a program without an AppArmor profile of its own gets none; Podman has its profile.
        return Found(True, "AppArmor restricts unprivileged user namespaces to programs with a profile (Podman has "
                           "one; Bubblewrap needs its own)")
    if status != 0:
        return Found(False, "unshare --user failed: " + (err.strip().splitlines() or ["?"])[-1][:300])
    return Found(True, "unshare --user works")


def subordinate_ids(user, uid, etc="/etc"):
    """Found: the user's ranges in /etc/subuid and /etc/subgid (rootless Podman maps its users with them)."""
    missing = []
    for name in ("subuid", "subgid"):
        try:
            lines = Path(etc, name).read_text().splitlines()
        except OSError:
            lines = []
        if not any(line.split(":")[0] in (user, str(uid)) for line in lines if line.strip()):
            missing.append(name)
    if missing:
        return Found(False, f"no range for {user} in /etc/{' and /etc/'.join(missing)}: sudo usermod --add-subuids "
                            f"100000-165535 --add-subgids 100000-165535 {user}")
    return Found(True, "ranges in /etc/subuid and /etc/subgid")


def podman_facts(env, runner=run_command):
    """(Found, facts) of `podman info`: installed, rootless, its version and cgroup version."""
    status, out, err = runner(["podman", "info", "--format", "json"], timeout=120, env=env)
    if status == 127:
        return Found(False, "podman is not installed (your distribution's package podman)"), {}
    if status != 0:
        return Found(False, "podman info failed: " + (err.strip().splitlines() or ["?"])[-1][:300]), {}
    try:
        info = json.loads(out)
    except ValueError:
        return Found(False, "podman info did not answer JSON"), {}
    host = info.get("host") or {}
    facts = {"podman": (info.get("version") or {}).get("Version"), "cgroup": host.get("cgroupVersion"),
             "rootless": (host.get("security") or {}).get("rootless")}
    if facts["rootless"] is False:
        return Found(False, "podman is not rootless here: run the check as your own user"), facts
    return Found(True, f"podman {facts['podman']}, rootless, cgroup {facts['cgroup']}"), facts


def prerequisites(env, args, system=None, runner=run_command, proc="/proc", etc="/etc", user=None, uid=None,
                  which=shutil.which):
    """{key: Found} of everything a check may need (Check.needs), and facts about this machine. ENV: the checks'
    environment (its PATH has the Podman wrapper)."""
    system = system or platform.system()
    uid = os.getuid() if uid is None else uid
    user = user or os.environ.get("USER") or str(uid)
    found, facts = {}, {"system": system, "kernel": platform.release(), "python": platform.python_version()}
    found["linux"] = Found(system == "Linux", "Linux" if system == "Linux" else
                           f"{system}: this check needs Linux" + (" (on a Mac the macos-* checks run)"
                                                                  if system == "Darwin" else ""))
    found["macos"] = macos_found(system, runner, which, facts)
    if system == "Linux":
        found["userns"] = user_namespaces(proc, runner)
        found["subuid"] = subordinate_ids(user, uid, etc)
    else:
        found["userns"] = found["subuid"] = Found(False, "Linux only")
    podman, podman_info = podman_facts(env, runner)
    facts.update({key: value for key, value in podman_info.items() if value is not None})
    if system == "Darwin":
        found["podman-machine"] = machine_found(env, runner, podman)
        podman = Found(False, "on a Mac Podman runs in podman machine's Linux VM: the Linux checks run on Linux "
                              "(here: the macos-* checks)")
    elif podman.ok and not (found["userns"].ok and found["subuid"].ok):
        podman = Found(False, "rootless Podman needs user namespaces and subordinate ids (above)")
    found["podman"] = podman
    status, out, err = (127, "", "") if system == "Darwin" else runner(["bwrap", "--version"], timeout=30, env=env)
    if system == "Darwin":
        found["bwrap"] = Found(False, "Linux only (Bubblewrap)")
    elif status != 0:
        found["bwrap"] = Found(False, "bwrap is not installed (package bubblewrap)" if status == 127 else
                               f"bwrap --version failed: {(err or out).strip()[-200:]}")
    elif not found["userns"].ok:
        found["bwrap"] = Found(False, f"{out.strip()}: needs user namespaces (above)")
    else:
        version = out.strip()
        status, _, err = runner(["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"], timeout=30, env=env)
        found["bwrap"] = Found(status == 0, version if status == 0 else
                               f"{version} cannot make a sandbox here: {(err.strip().splitlines() or ['?'])[-1][:300]}")
    found["sandbox"] = Found(podman.ok or found["bwrap"].ok, "Podman" if podman.ok else "Bubblewrap only" if
                             found["bwrap"].ok else "neither Podman nor Bubblewrap can start a sandbox here")
    for image in IMAGES:
        if not podman.ok:
            found[f"image:{image}"] = Found(False, "no Podman")
            continue
        status, _, _ = runner(["podman", "image", "exists", f"localhost/{image}-sandbox:latest"], timeout=60, env=env)
        found[f"image:{image}"] = Found(status == 0, f"localhost/{image}-sandbox" if status == 0 else
                                        f"missing: sandbox-ai build {image} (or --build)")
    status, out, _ = runner(["git", "--version"], timeout=30, env=env)
    found["git"] = Found(status == 0, out.strip() if status == 0 else "git is not installed")
    found["host"] = Found(False, "give --host NAME (a host of sandbox-ai hosts)")
    found["llm"] = Found(False, "give --llm NAME (a server of sandbox-ai llm)")
    found["device"] = Found(False, "give --device PATH")
    found["dmgs"] = Found(False, "give --dmg-old DMG --dmg-new DMG (two disk images of sandbox-ai.app, the old one "
                                 "0.1.154 or later: built here or downloaded with the release's SHA256SUMS and "
                                 "SHA256SUMS.sig)")
    found["app"] = Found(False, "give --app PATH (a sandbox-ai.app) or --dmg-new DMG")
    status, out, err = runner([str(SAI), "--version"], timeout=60, env=env)
    facts["sandbox-ai"] = (out or err).strip().splitlines()[0] if (out or err).strip() else f"exit {status}"
    status, out, _ = runner(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], timeout=30, env=env)
    if status == 0:
        facts["commit"] = out.strip()
    facts["distribution"] = distribution(etc, system)
    return found, facts


def macos_found(system, runner=run_command, which=shutil.which, facts=None):
    """Found: this is a Mac with what the macOS checks use (hdiutil, ditto, codesign: part of macOS); its version
    and architecture go into FACTS."""
    if system != "Darwin":
        return Found(False, f"{system}: the macOS checks run on a Mac")
    status, out, _ = runner(["sw_vers", "-productVersion"], timeout=30)
    version = out.strip() if status == 0 and out.strip() else "?"
    if facts is not None:
        facts.update(macos=version, arch=platform.machine())
    missing = [tool for tool in ("hdiutil", "ditto", "codesign") if not which(tool)]
    if missing:
        return Found(False, f"macOS {version}: {', '.join(missing)} not found (part of macOS: /usr/bin)")
    # sandbox-ai's starts want bash 4 or later (macOS ships 3.2): the one on PATH or Homebrew's (main.find_bash)
    for bash in dict.fromkeys(filter(None, [which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash"])):
        status, out, _ = runner([bash, "-c", "echo ${BASH_VERSINFO[0]}"], timeout=30)
        if status == 0 and out.strip().isdigit() and int(out.strip()) >= 4:
            return Found(True, f"macOS {version} ({platform.machine()}), hdiutil, ditto, codesign, bash "
                               f"{out.strip()} ({bash})")
    return Found(False, f"macOS {version}: sandbox-ai needs bash 4 or later (macOS ships 3.2): brew install bash")


def machine_found(env, runner=run_command, podman=None):
    """Found: podman machine, the Linux VM Podman runs in on a Mac (what doctor's macOS checks look at; no check
    needs it to run - macos-doctor compares doctor's verdict with it either way)."""
    status, out, err = runner(["podman", "machine", "inspect"], timeout=60, env=env)
    if status == 127:
        return Found(False, "podman is not installed (podman.io's installer or brew install podman)")
    try:
        machines = json.loads(out) if status == 0 else None
    except ValueError:
        machines = None
    if not isinstance(machines, list) or not machines or not isinstance(machines[0], dict):
        return Found(False, "no podman machine: podman machine init; podman machine start" + (
            f" ({(err.strip().splitlines() or [''])[-1][:200]})" if err.strip() else ""))
    machine = machines[0]
    state = machine.get("State") or "?"
    resources = machine.get("Resources") or {}
    detail = f"{machine.get('Name') or '?'} {state}, {resources.get('CPUs') or '?'} CPUs"
    return Found(state == "running", detail if state == "running" else f"{detail}: podman machine start")


def release_assets_module():
    spec = importlib.util.spec_from_file_location("release_assets_for_live_check", ROOT / "sandbox/release_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dmg_file(given, machine=None):
    """(the disk image, None) of GIVEN - a .dmg, or a folder holding one (a release's: this Mac's
    sandbox-ai-ARCH.dmg) - or (None, why not)."""
    path = Path(given).expanduser()
    if path.is_file() and path.suffix == ".dmg":
        return path.resolve(), None
    if not path.is_dir():
        return None, f"{given}: neither a .dmg nor a folder"
    assets = release_assets_module()
    arch = assets.arch_of("macos-app", machine)
    wanted = path / assets.dmg(arch) if arch else None
    if wanted is not None and wanted.is_file():
        return wanted.resolve(), None
    images = sorted(path.glob("*.dmg"))
    if len(images) == 1:
        return images[0].resolve(), None
    return None, f"{given}: " + (f"no {wanted.name} in it" if wanted is not None else "no disk image for this "
                                 "machine in it") + (f" (it has {', '.join(p.name for p in images)})" if images else "")


def signed_release(dmg):
    """Whether the folder of DMG is a release as it was downloaded: SHA256SUMS listing DMG's name with its SHA-256,
    and SHA256SUMS.sig - then the update is checked with the release's own key; else the check signs a SHA256SUMS
    of its own (a local build)."""
    dmg = Path(dmg)
    sums, signature = dmg.parent / "SHA256SUMS", dmg.parent / "SHA256SUMS.sig"
    if not (sums.is_file() and signature.is_file()):
        return False
    listed = release_assets_module().checksums(sums.read_text(errors="replace"))
    return listed.get(dmg.name) == file_sha256(dmg)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_dmgs(args, env, machine=None):
    """Found: macos-update's two disk images (SAI_LIVE_DMG_OLD, SAI_LIVE_DMG_NEW)."""
    if not (args.dmg_old and args.dmg_new):
        return Found(False, "give --dmg-old DMG --dmg-new DMG (two disk images of sandbox-ai.app, the old one 0.1.154 "
                            "or later: built here or downloaded with the release's SHA256SUMS and SHA256SUMS.sig)")
    said = []
    for option, given, variable in (("--dmg-old", args.dmg_old, "SAI_LIVE_DMG_OLD"),
                                    ("--dmg-new", args.dmg_new, "SAI_LIVE_DMG_NEW")):
        dmg, problem = dmg_file(given, machine)
        if problem:
            return Found(False, f"{option} {problem}")
        env[variable] = str(dmg)
        said.append(f"{option} {dmg.name} ({'a signed release' if signed_release(dmg) else 'a local build'})")
    if env["SAI_LIVE_DMG_OLD"] == env["SAI_LIVE_DMG_NEW"]:
        return Found(False, "--dmg-old and --dmg-new are the same disk image")
    return Found(True, "; ".join(said))


def prepare_app(args, env):
    """Found: macos-app's sandbox-ai.app - --app (SAI_LIVE_APP), else the one in --dmg-new."""
    if args.app:
        app = Path(args.app).expanduser()
        if not (app / "Contents" / "MacOS" / "sandbox-ai").is_file():
            return Found(False, f"--app {args.app}: not a sandbox-ai.app (no Contents/MacOS/sandbox-ai)")
        env["SAI_LIVE_APP"] = str(app.resolve())
        return Found(True, f"{app.name} (--app; only read and started)")
    if env.get("SAI_LIVE_DMG_NEW"):
        return Found(True, f"the app in {Path(env['SAI_LIVE_DMG_NEW']).name} (--dmg-new), put into the check's folder")
    return Found(False, "give --app PATH (a sandbox-ai.app) or --dmg-new DMG")


def distribution(etc="/etc", system=None):
    if (system or platform.system()) == "Darwin":
        return "macOS"
    try:
        for line in Path(etc, "os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def missing_for(check, found, backends):
    """The prerequisites CHECK lacks (each "key: detail"); images only where Podman is used."""
    missing = []
    for key in check.needs:
        if key.startswith("image:") and "podman" not in backends:
            continue
        state = found.get(key) or Found(False, "unknown")
        if not state.ok:
            missing.append(f"{key}: {state.detail}")
    if check.podman_only and "podman" not in backends and not any(m.startswith("podman:") for m in missing):
        missing.append("podman: this check runs with Podman only")
    return missing


def backends_of(choice, found):
    """The backends to run with: CHOICE (podman, bwrap, both) or, with auto, each one that works here."""
    if choice == "auto":
        return [name for name in ("podman", "bwrap") if found[name].ok]
    return ["podman", "bwrap"] if choice == "both" else [choice]


# --- the optional sections' inputs --------------------------------------------------------------------------------

def real_config(env=None):
    env = os.environ if env is None else env
    return Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME") or Path.home()) / ".config") / "sandbox-ai"


def llm_endpoint(name, config):
    """(entry, problem) of the LLM endpoint NAME in CONFIG/llm.json (read, never changed): entry {"name", "record",
    "ca"} without anything secret; problem says why it cannot be used by the check (a key or header kept as a secret,
    a VPN profile: never copied into the check's own HOME)."""
    try:
        store = json.loads((Path(config) / "llm.json").read_text())
    except FileNotFoundError:
        return None, "no LLM server is configured (sandbox-ai llm add)"
    except (OSError, ValueError) as exc:
        return None, f"llm.json: {exc}"
    endpoints = store.get("endpoints") if isinstance(store, dict) else None
    if not isinstance(endpoints, dict) or name not in endpoints:
        names = ", ".join(sorted(endpoints or {})) or "none"
        return None, f"no LLM server {name!r} (configured: {names})"
    record = dict(endpoints[name])
    secret = [key for key in ("api_key_secret", "headers", "vpn") if record.get(key)]
    if secret:
        return None, (f"the server {name} uses {', '.join(secret)}: its secrets and VPN profiles are never copied into "
                      "the check's own HOME; run the smoke runs by hand (docs/LIVE_CHECK.md)")
    ca = record.get("ca")
    ca_path = None
    if ca:
        ca_path = Path(config) / "llm" / Path(ca).name if not Path(ca).is_absolute() else Path(ca)
        if not ca_path.is_file():
            return None, f"its CA file {ca} is not there"
    for key in ("checked", "contexts"):
        record.pop(key, None)
    return {"name": name, "record": record, "ca": str(ca_path) if ca_path else None}, None


def llm_names(config):
    try:
        store = json.loads((Path(config) / "llm.json").read_text())
        return sorted((store.get("endpoints") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return []


def host_files(name, config, target):
    """Copies the host NAME of CONFIG/hosts.json (only it) and sandbox-ai's known_hosts into TARGET; a problem or
    None."""
    try:
        document = json.loads((Path(config) / "hosts.json").read_text())
    except FileNotFoundError:
        return "no host is configured (sandbox-ai hosts add)"
    except (OSError, ValueError) as exc:
        return f"hosts.json: {exc}"
    hosts = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(hosts, dict) or name not in hosts:
        return f"no host {name!r} (configured: {', '.join(sorted(hosts or {})) or 'none'})"
    target = Path(target)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(target / "hosts.json", json.dumps({"version": document.get("version", 1),
                                                     "hosts": {name: hosts[name]}}, indent=2) + "\n")
    known = Path(config) / "known_hosts"
    if known.is_file():
        write_private(target / "known_hosts", known.read_text())
    return None


def write_private(path, text):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def pick_device(scan):
    """The value of `devices add` devices-live uses from a `devices scan --json` document: the first device it
    offers (not refused, given with --device as a path) of DEVICE_KINDS' kinds, in that order; None."""
    entries = [entry for entry in (scan or {}).get("devices") or [] if isinstance(entry, dict)]
    for kind in DEVICE_KINDS:
        for entry in entries:
            value = entry.get("value")
            if entry.get("kind") == kind and not entry.get("refused") and entry.get("option") == "--device" \
                    and isinstance(value, str) and value.startswith("/dev/"):
                return value
    return None


def ask(question, stream_in=None, stream_out=None):
    """True when the human answers yes on a terminal; False without a terminal."""
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout
    if not stream_in.isatty():
        return False
    stream_out.write(question + " [y/N] ")
    stream_out.flush()
    return stream_in.readline().strip().lower() in ("y", "yes", "j", "ja")


# --- running a check ------------------------------------------------------------------------------------------------

def pytest_command(python, check, backend, basetemp, junit, suites=None, needs_login=None):
    """The exact pytest command of CHECK with BACKEND ("both", "podman" or "bwrap")."""
    command = [str(python), "-X", "faulthandler", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfEs",
               "--agent-live", "--agent-backend", backend, f"--basetemp={basetemp}", f"--junitxml={junit}"]
    if check.name == "live-suites":
        return command + ["-k", needs_login, *suites]
    if check.keyword:
        command += ["-k", check.keyword]
    return command + list(check.tests)


def run_logged(command, log, timeout, env, cwd):
    """Runs COMMAND in a process group of its own, its output into LOG; (status, seconds). At TIMEOUT the group gets
    SIGTERM, 30 s later SIGKILL (only this process group: what this check started), status 124."""
    begin = time.monotonic()
    with open(log, "wb") as out:
        process = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                   cwd=cwd, start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 30)):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
            out.write(f"\n[live check: timed out after {timeout} s]\n".encode())
            status = 124
    return status, time.monotonic() - begin


def outcome_of(status, results):
    """passed, failed, skipped or error of a pytest run: its exit STATUS and its tests' RESULTS [(name, outcome,
    ...)]."""
    outcomes = [result[1] for result in results]
    if status == 124:
        return "failed"
    if any(outcome in ("failed", "error") for outcome in outcomes):
        return "failed"
    if not outcomes:
        return "skipped" if status == 5 else "error"
    if status not in (0, 5):
        return "error"  # pytest failed without a failing test: never a passing check
    if all(outcome == "skipped" for outcome in outcomes):
        return "skipped"
    return "passed"


def tail(path, lines=TAIL):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def leftover_sessions(root):
    """[(environment, session name)] of the sessions the checks under ROOT left behind (a check that timed out never
    reached its own cleanup): each ROOT/**/sandbox-ai/sessions/NAME/session.json, with the XDG folders found beside
    the state folder it lies in ({variable: folder})."""
    found = []
    for record in sorted(Path(root).glob("**/sandbox-ai/sessions/*/session.json")):
        state = record.parents[3]
        folders = {"XDG_STATE_HOME": str(state)}
        for name, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                             ("XDG_RUNTIME_DIR", "run")):
            if (state.parent / folder).is_dir():
                folders[name] = str(state.parent / folder)
        found.append((folders, record.parent.name))
    return found


def stop_leftovers(root, env, runner=run_command):
    """Stops and deletes every session the checks under ROOT left behind - only those: each by its name, with the
    folders it was made with."""
    stopped = []
    for folders, name in leftover_sessions(root):
        mine = {**env, **folders}
        runner([str(SAI), "session", "stop", name], timeout=180, env=mine)
        runner([str(SAI), "session", "delete", name, "--yes"], timeout=120, env=mine)
        stopped.append(name)
    return stopped


def remove_tree(path, env, runner=run_command):
    """Removes PATH; what rootless Podman left there under its mapped users goes through `podman unshare`."""
    shutil.rmtree(path, ignore_errors=True)
    if Path(path).exists() and shutil.which("podman", path=env.get("PATH")):
        runner(["podman", "unshare", "rm", "-rf", "--", str(path)], timeout=300, env=env)
    return not Path(path).exists()


# --- redaction and the report ----------------------------------------------------------------------------------------

SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "<private key>"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "<secret>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), "<secret>"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"), "<secret>"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "<secret>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<secret>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<secret>"),
    (re.compile(r"\b(?:hf|npm|pypi)_[A-Za-z0-9_-]{20,}"), "<secret>"),
    (re.compile(r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"',]+"),
     r"\1<secret>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 <secret>"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|passw(?:or)?d|credential)[\w-]*[\"']?\s*[:=]\s*[\"']?)"
                r"(?!<secret>)(?=[^\s\"',;}]*[A-Za-z])[^\s\"',;}]{6,}"), r"\1<secret>"),
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1<secret>@"),
)
# A home folder at the start of a path (not one below another folder: <tmp>/home is the checks' own HOME).
HOME_PATTERN = re.compile(r"(?<![\w.>/~-])(/(?:home|Users|var/home|export/home))/[^/\s\"':;,)\]}]+")
SECRET_NAME = re.compile(r"(?i)token|secret|passw|api_?key|credential|auth|cookie|private")


class Redactor:
    """Replaces in a text every literal secret it was given, the checks' temporary folder, the real HOME and any
    other home folder, and what looks like a token, key or password."""

    def __init__(self, home=None, temporary=(), literals=(), environ=None):
        self.home = str(home).rstrip("/") if home else None
        self.temporary = [str(path).rstrip("/") for path in temporary if path]
        environ = os.environ if environ is None else environ
        found = {value for key, value in environ.items()  # (a path, such as SSH_AUTH_SOCK's, is no secret)
                 if SECRET_NAME.search(key) and len(value) >= 8 and not value.startswith("/")}
        self.literals = sorted({value for value in [*literals, *found] if value and len(value) >= 6}, key=len,
                               reverse=True)

    def text(self, text):
        if not isinstance(text, str):
            return text
        for literal in self.literals:
            text = text.replace(literal, "<secret>")
        for path in sorted(self.temporary, key=len, reverse=True):
            text = text.replace(path, "<tmp>")
        if self.home and self.home not in ("", "/"):
            text = re.sub(re.escape(self.home) + r"(?=/|\b|$)", "~", text)
        text = HOME_PATTERN.sub(r"\1/<user>", text)
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def data(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, tuple):
            return [self.data(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key) if isinstance(key, str) else key: self.data(item) for key, item in value.items()}
        return value


def counts_of(checks):
    counts = {}
    for check in checks:
        counts[check["outcome"]] = counts.get(check["outcome"], 0) + 1
    return counts


def markdown(report):
    """The report as Markdown (already redacted)."""
    lines = [f"# sandbox-ai live check, {report['date']}", "",
             "Made by `scripts/live_check.sh` (docs/LIVE_CHECK.md). Secrets and home paths are replaced "
             "(`<secret>`, `~`, `/home/<user>`, `<tmp>`: the checks' own temporary folder).", "",
             "| | |", "|---|---|"]
    lines += [f"| {key} | {cell(value)} |" for key, value in report["facts"].items()]
    lines += ["", "## Prerequisites", "", "| | prerequisite | |", "|---|---|---|"]
    for item in report["prerequisites"]:
        lines.append(f"| {'ok' if item['ok'] else 'missing'} | {item['name']} | {cell(item['detail'])} |")
    if report.get("network"):
        lines += ["", "## What left this machine", "", *(f"- {line}" for line in report["network"])]
    counts = report["counts"]
    lines += ["", "## Results", "", "Counts: " + (", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
                                                or "nothing ran"), "",
              "| outcome | check | seconds | what it proves |", "|---|---|---|---|"]
    for check in report["checks"]:
        seconds = "" if check.get("seconds") is None else f"{check['seconds']:.0f}"
        lines.append(f"| {check['outcome']} | {check['name']} | {seconds} | {cell(check['proves'])} |")
    for check in report["checks"]:
        lines += ["", f"### {check['name']}: {check['outcome']}", ""]
        if check.get("missing"):
            lines += ["Missing: " + "; ".join(check["missing"]), ""]
        if check.get("note"):
            lines += [check["note"], ""]
        if check.get("network"):
            lines += [f"Network: {check['network']}", ""]
        if check.get("command"):
            lines += ["```", check["command"], "```", ""]
        for test in check.get("tests") or []:
            lines.append(f"- {test['outcome']}: `{test['name']}`" + (f" ({test['seconds']:.0f} s)"
                                                                     if test.get("seconds") else ""))
            if test.get("detail") and test["outcome"] != "passed":
                lines += ["", "  ```", *("  " + line for line in test["detail"].splitlines()), "  ```", ""]
        if check.get("tail"):
            lines += ["", "Last lines of its output:", "", "```", *check["tail"], "```"]
    return "\n".join(lines) + "\n"


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_report(report, directory, redactor, stamp):
    """REPORT-STAMP.md and .json in DIRECTORY, redacted; their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    clean = redactor.data(report)
    clean["counts"] = counts_of(clean["checks"])
    md, js = directory / f"REPORT-{stamp}.md", directory / f"REPORT-{stamp}.json"
    js.write_text(json.dumps(clean, indent=2) + "\n")
    md.write_text(markdown(clean))
    return md, js


def listing(out=print):
    """--list: every check, what it proves, what it needs, what it sends where."""
    for check in CHECKS:
        out(f"{check.name}{' (optional)' if check.optional else ''}")
        out(f"    proves:   {check.proves}")
        out(f"    needs:    {', '.join(check.needs)}" + (", Podman only" if check.podman_only else ""))
        out(f"    network:  {check.network or 'nothing leaves this machine'}")
        out(f"    timeout:  {check.timeout} s; from {check.source}")


def selected(only):
    """The checks of --only (comma lists, repeatable) or, without it, every check that is not optional; SystemExit
    names an unknown one."""
    if not only:
        return [check for check in CHECKS if not check.optional]
    names = [name.strip() for value in only for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in BY_NAME]
    if unknown:
        raise SystemExit(f"live check: unknown check {', '.join(unknown)}; the checks: {', '.join(BY_NAME)}")
    return [check for check in CHECKS if check.name in names]


# --- main ------------------------------------------------------------------------------------------------------------

def parser():
    parse = argparse.ArgumentParser(prog="scripts/live_check.sh", description=__doc__.split("\n\n")[0],
                                    formatter_class=argparse.RawDescriptionHelpFormatter,
                                    epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    parse.add_argument("--list", action="store_true", help="the checks, and exit")
    parse.add_argument("--only", action="append", metavar="NAME[,NAME]", help="only these checks (repeatable)")
    parse.add_argument("--backend", choices=["auto", "podman", "bwrap", "both"], default="auto",
                       help="the sandbox backends (auto: each one that works here)")
    parse.add_argument("--out", type=Path, metavar="DIR", help="where the report goes (default: live-check/ of "
                                                                 "this checkout)")
    parse.add_argument("--build", action="store_true", help="build the missing images first (network: their "
                                                            "packages)")
    parse.add_argument("--host", metavar="NAME", help="also remote-mcp on this host of sandbox-ai hosts")
    parse.add_argument("--llm", metavar="NAME", help="also local-llm on this server of sandbox-ai llm")
    parse.add_argument("--llm-models", metavar="MODEL[,MODEL]", default=",".join(LLM_MODELS),
                       help="the models of local-llm (default: the three best of the research)")
    parse.add_argument("--yes", action="store_true", help="answer yes to the questions of remote-mcp and local-llm")
    parse.add_argument("--device", metavar="PATH", help="the device of devices-live (default: one devices scan "
                                                        "offers: a serial port, camera, render node or /dev/kvm)")
    parse.add_argument("--dmg-old", metavar="DMG", help="macos-update: the disk image updated from (sandbox-ai.app "
                                                         "0.1.154 or later; a .dmg or a downloaded release's folder)")
    parse.add_argument("--dmg-new", metavar="DMG", help="macos-update: the disk image updated to (a .dmg or a "
                                                         "downloaded release's folder); macos-app's app without --app")
    parse.add_argument("--app", metavar="PATH", help="macos-app: this sandbox-ai.app (only read and started)")
    parse.add_argument("--keep-temp", action="store_true", help="keep the checks' temporary folder (its logs; it "
                                                                "may hold paths of yours)")
    parse.add_argument("--work-dir", type=Path, metavar="DIR", help="where the temporary folder is made (default: "
                                                                    "the system's; keep its path short)")
    return parse


def main(argv=None, out=print, stdin=None):
    args = parser().parse_args(argv)
    if args.list:
        listing(out)
        return 0
    try:
        checks = selected(args.only)
    except SystemExit as exc:
        out(str(exc))
        return 2
    lc = load_live_check()
    if os.geteuid() == 0:
        out("live check: refused - run it as your own user, never with root or sudo (rootless Podman)")
        return 2
    if lc.inside_a_container():
        out("live check: refused - this looks like a container or a sandbox; run it on the host")
        return 2
    config = real_config()
    if not args.only:  # the optional sections join a whole run when they are configured (each asks first)
        if args.host or host_names(config):
            checks.append(BY_NAME["remote-mcp"])
        if args.llm or llm_names(config):
            checks.append(BY_NAME["local-llm"])
    names = [check.name for check in checks]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    real_home = os.environ.get("HOME") or str(Path.home())
    temporary = Path(tempfile.mkdtemp(prefix="sai-lc-", dir=args.work_dir or tempfile.gettempdir()))
    env = isolated_environment(temporary, os.environ)
    token = "sai-lc-" + secrets.token_hex(16)
    env.update(SAI_LIVE_TOKEN=token, SAI_LIVE_REAL_HOME=real_home)
    redactor = Redactor(real_home, [temporary, temporary.resolve()], [token])
    out(f"sandbox-ai live check: {', '.join(names)}\nthe checks' own folder (HOME, configuration, state, "
        f"sessions): {temporary}")

    said = {}
    if "remote-mcp" in names:
        said["host"] = prepare_host(args, config, temporary, env, stdin)
    if "local-llm" in names:
        said["llm"] = prepare_llm(args, config, temporary, env, stdin)
    found, facts = prerequisites(env, args)
    ready = {"host": f"host {env.get('SAI_LIVE_HOST')}", "llm": f"server {getattr(args, 'llm_said', '')}"}
    for key, problem in said.items():  # what the optional sections' preparation found
        found[key] = Found(problem is None, problem or ready[key])
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path(real_home) / ".cache") / "sandbox-ai" / "live-check"
    found["pytest"] = pytest_found(cache)
    backends = backends_of(args.backend, found)
    facts.update({"date": stamp, "checkout": str(ROOT), "backends": ", ".join(backends) or "none"})
    if args.build and found["podman"].ok:
        build_images(found, env, out)
    if "devices-live" in names:
        found["device"] = prepare_device(args, env)
    if found["macos"].ok:
        found["dmgs"] = prepare_dmgs(args, env)
        found["app"] = prepare_app(args, env)
    system_keys = ("macos", "podman-machine") if found["macos"].ok else ("linux", "userns", "subuid", "podman", "bwrap")
    for key in (*system_keys, "git", "pytest", *(f"image:{i}" for i in IMAGES if not found["macos"].ok),
                *(key for key in ("device", "host", "llm", "dmgs", "app") if key in found and key in needed(checks))):
        if key not in found:
            continue
        state = found[key]
        out(f"  {'ok  ' if state.ok else 'MISS'}  {key}: {state.detail}")

    # (what the checks that will run send: a check skipped for what it lacks sends nothing)
    network = [f"{check.name}: {check.network}" for check in checks
               if check.network and not missing_for(check, found, backends)]
    if args.build:
        network.append("--build: the images' packages (Debian, npm, PyPI ...)")
    if not found["pytest"].ok or "PyPI" in found["pytest"].detail:
        network.append("pytest: " + found["pytest"].detail)
    if network:
        out("What these checks send from this machine:")
        for line in network:
            out(f"  - {line}")
    python, results = None, []
    for check in checks:
        missing = missing_for(check, found, backends)
        entry = {"name": check.name, "proves": check.proves, "needs": list(check.needs), "network": check.network,
                 "source": check.source, "outcome": "skipped", "seconds": None, "missing": missing, "command": "",
                 "tests": [], "tail": []}
        results.append(entry)
        if missing:
            out(f"  skip  {check.name}  ({missing[0][:160]})")
            continue
        if python is None:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                python = lc.pytest_python(cache)
            except SystemExit as exc:
                entry.update(outcome="error", note=str(exc))
                out(f"  ERR   {check.name}  ({exc})")
                continue
        run_check(check, entry, python, backends, temporary, env, lc, out)
    stopped = stop_leftovers(temporary, env)
    report = {"version": REPORT_VERSION, "date": stamp, "facts": facts,
              "prerequisites": [{"name": key, "ok": state.ok, "detail": state.detail} for key, state in found.items()],
              "network": network, "checks": results, "counts": counts_of(results),
              "left_behind_and_stopped": stopped}
    md, js = write_report(report, args.out or ROOT / "live-check", redactor, stamp)
    if args.keep_temp:
        out(f"the checks' folder is kept: {temporary} (it may hold paths of yours; remove it when done)")
    elif not remove_tree(temporary, env):
        out(f"could not remove all of {temporary}: podman unshare rm -rf {temporary}")
    counts = counts_of(results)
    out(f"\n{', '.join(f'{key} {value}' for key, value in sorted(counts.items()))}\nreport: {md}\n        {js}")
    return 0


def needed(checks):
    return {key for check in checks for key in check.needs}


def pytest_found(cache):
    """Found: pytest for the checks - this Python's, the venv of an earlier run, or one to install from PyPI."""
    status, _, _ = run_command([sys.executable, "-c", "import pytest"], timeout=60)
    if status == 0:
        return Found(True, f"pytest of {sys.executable}")
    if (Path(cache) / "venv" / "bin" / "python").exists():
        return Found(True, f"pytest in {Path(cache) / 'venv'}")
    return Found(True, f"installed now from PyPI into {Path(cache) / 'venv'} (packaging/requirements-ci.txt, hashes "
                       "checked)")


def build_images(found, env, out):
    for image in IMAGES:
        if not found[f"image:{image}"].ok:
            out(f"  ...   sandbox-ai build {image} (its packages come from the network)")
            status, _, err = run_command([str(SAI), "build", image], timeout=3600, env=env)
            found[f"image:{image}"] = Found(status == 0, f"built now: localhost/{image}-sandbox" if status == 0 else
                                            f"sandbox-ai build {image} failed: {err.strip()[-300:]}")


def host_names(config):
    try:
        document = json.loads((Path(config) / "hosts.json").read_text())
        return sorted((document.get("hosts") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return []


def prepare_host(args, config, temporary, env, stdin=None):
    """remote-mcp's host: --host NAME, or the one configured host after a question; that host's entry and
    sandbox-ai's known_hosts into the checks' folder (SAI_LIVE_HOST, SAI_LIVE_HOSTS_DIR). A problem or None."""
    configured = host_names(config)
    name = args.host or (configured[0] if len(configured) == 1 else None)
    if not name:
        return "give --host NAME" + (f" (configured: {', '.join(configured)})" if configured else
                                     ": no host is configured (sandbox-ai hosts add)")
    question = (f"remote-mcp: over SSH to your host {name}: hosts test, its MCP servers listed, its demo server "
                "started and stopped there if none of it runs. Run it?")
    if not args.host and not (args.yes or ask(question, stdin)):
        return "not run: not confirmed (on a terminal it asks; else --host NAME or --yes)"
    target = temporary / "given" / "hosts"
    problem = host_files(name, config, target)
    if problem:
        return problem
    env.update(SAI_LIVE_HOST=name, SAI_LIVE_HOSTS_DIR=str(target))
    return None


def prepare_llm(args, config, temporary, env, stdin=None):
    """local-llm's server: --llm NAME, or the one configured server; asked first (on a terminal; else --yes). The
    endpoint without secrets into the checks' folder (SAI_LIVE_LLM_ENDPOINT, SAI_LIVE_LLM_MODELS). A problem or
    None."""
    configured = llm_names(config)
    name = args.llm or (configured[0] if len(configured) == 1 else None)
    if not name:
        return "give --llm NAME" + (f" (configured: {', '.join(configured)})" if configured else
                                    ": no LLM server is configured (sandbox-ai llm add)")
    entry, problem = llm_endpoint(name, config)
    if problem:
        return problem
    models = [model.strip() for model in args.llm_models.split(",") if model.strip()]
    question = (f"local-llm: {len(models) * 3} smoke runs ({', '.join(models)} x Claude Code, OpenCode, Codex) "
                f"against your LLM server {name}; it loads each model, which takes a while. Run them?")
    if not (args.yes or ask(question, stdin)):
        return "not run: not confirmed (on a terminal it asks; else --yes)"
    path = temporary / "given" / "llm.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, json.dumps(entry) + "\n")
    env.update(SAI_LIVE_LLM_ENDPOINT=str(path), SAI_LIVE_LLM_MODELS=",".join(models))
    args.llm_said = f"{name}: {', '.join(models)}"
    return None


def prepare_device(args, env, runner=run_command):
    """Found: devices-live's device - --device, else one `devices scan` offers (SAI_LIVE_DEVICE)."""
    if args.device:
        env["SAI_LIVE_DEVICE"] = args.device
        return Found(True, f"{args.device} (--device)")
    status, out, err = runner([str(SAI), "devices", "scan", "--json"], timeout=120, env=env)
    if status != 0:
        return Found(False, f"devices scan failed: {(err or out).strip()[-300:]}")
    try:
        value = pick_device(json.loads(out))
    except ValueError:
        return Found(False, "devices scan did not answer JSON")
    if not value:
        return Found(False, "devices scan offers no serial port, camera, render node or /dev/kvm to hand over; give "
                            "--device PATH")
    env["SAI_LIVE_DEVICE"] = value
    return Found(True, f"{value} (from devices scan)")


def run_check(check, entry, python, backends, temporary, env, lc, out):
    """Runs CHECK's pytest and fills ENTRY: its command, outcome, tests, last lines."""
    # (the macOS checks use no backend of this host: --agent-backend is pytest's default then)
    backend = "podman" if check.podman_only or not backends else ("both" if len(backends) == 2 else backends[0])
    junit = temporary / "junit" / f"{check.name}.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    basetemp = temporary / "p" / check.name.replace("-", "")[:12]
    command = pytest_command(python, check, backend, basetemp, junit, lc.SUITES, lc.NEEDS_LOGIN)
    entry["command"] = shlex.join(str(part) for part in command)
    log = temporary / "logs" / f"{check.name}.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    out(f"  ...   {check.name} (at most {check.timeout // 60} min)")
    status, seconds = run_logged(command, log, check.timeout, env, ROOT)
    results = lc.junit_results(junit)
    entry["outcome"] = outcome_of(status, results)
    entry["seconds"] = round(seconds, 1)
    entry["status"] = status
    entry["tests"] = [{"name": name, "outcome": outcome, "seconds": round(spent, 1),
                       "detail": detail.strip()[-DETAIL:] if outcome != "passed" else ""}
                      for name, outcome, detail, spent in results]
    entry["tail"] = tail(log)
    mark = {"passed": "ok  ", "failed": "FAIL", "skipped": "skip", "error": "ERR "}[entry["outcome"]]
    out(f"  {mark}  {check.name}  ({seconds:.0f} s)")


if __name__ == "__main__":
    sys.exit(main())
