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
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import threading

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent
AGENTS = ("codex", "claude", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")
# The agents beyond the first three (opencode2, hermes, dsh, qwen, goose, openhands, nanobot, letta, pi): no image or
# login of them is not a warning (few use them all).
OPTIONAL = {"opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"}
_launcher = None
_modules = {}


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_doctor", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


def textout():
    if "textout" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_ai_textout_for_doctor", HERE / "textout.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["textout"] = loaded  # published only once loaded
    return _modules["textout"]


PODMAN_TIMEOUT = 30  # seconds one podman command of the checks may take
NO_ANSWER = 124       # run()'s status for a command that did not end in time
_unanswered = set()   # the podman command lines (before the subcommand) that did not answer: not asked again


def run(cmd, timeout=None):
    """(returncode, stdout) of CMD; 127 if it cannot run, NO_ANSWER if it did not end within TIMEOUT (default
    PODMAN_TIMEOUT). A podman that did not answer once is not asked again in this process: a hanging Podman made
    doctor wait 30 s for each of its checks (minutes)."""
    base = tuple(cmd[:3]) if cmd[1:2] == ["--remote"] else tuple(cmd[:1])
    if cmd[:1] == ["podman"] and base in _unanswered:
        return NO_ANSWER, ""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or PODMAN_TIMEOUT,
                                stdin=subprocess.DEVNULL)
        return result.returncode, result.stdout.strip()
    except subprocess.TimeoutExpired:
        if cmd[:1] == ["podman"]:
            _unanswered.add(base)
        return NO_ANSWER, ""
    except OSError:
        return 127, ""


def podman_answers(podman_url=None):
    return tuple(podman_base(podman_url)[:3 if podman_url else 1]) not in _unanswered


def check_user():
    if os.geteuid() == 0:
        return "fail", "Ordinary user", "running as root", "run sandbox-ai as your normal user, never with sudo"
    return "ok", "Ordinary user", getpass.getuser(), ""


def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 9):
        return "fail", "Python", version, "install Python 3.9 or newer"
    return "ok", "Python", version, ""


def subid_ranges(path, user, uid):
    """[(first, count)] delegated to USER (or UID) in /etc/subuid or /etc/subgid."""
    found = []
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.strip().split(":")
            if len(parts) == 3 and parts[0] in (user, str(uid)) and parts[1].isdigit() and parts[2].isdigit():
                found.append((int(parts[1]), int(parts[2])))
    except OSError:
        pass
    return found


def id_map(path):
    """[(first, count)] of the IDs this system has, from /proc/self/uid_map or gid_map (its own first ID and
    how many); None when it cannot be read. On a host that is 0 and 4294967295; in an unprivileged container
    (LXC, Proxmox pct) usually 0 and 65536."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return None
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            found.append((int(parts[0]), int(parts[2])))
    return found or None


def outside_map(ranges, mapped):
    """The first of RANGES that lies not wholly in MAPPED (the IDs the system has), or None."""
    for first, count in ranges:
        if count and not any(start <= first and first + count <= start + size for start, size in mapped):
            return first, count
    return None


# After the delegated IDs changed: images pulled or built before still carry the old IDs ("Permission denied" in
# a build, e.g. apt on /var/cache/apt/archives/partial); only a fresh storage helps.
RESET = ("podman system migrate, and podman system reset if images were pulled or built before (it removes this "
         "user's images and containers; their files still carry the old IDs). If the reset fails with \"permission "
         "denied\" (files of IDs no longer yours): sudo rm -rf ~/.local/share/containers/storage")
MORE_IDS = ("give this system more IDs from its host (an unprivileged LXC or Proxmox container: root:100000:1065536 "
            "in the host's /etc/subuid and /etc/subgid, lxc.idmap: u 0 100000 1065536 and g 0 100000 1065536 in "
            "/etc/pve/lxc/ID.conf, restart it), then: " + RESET)
NEEDED = 65534       # IDs 1 to 65534 in the user namespace: image builds use 65534 (apt's nogroup, nobody)
SUGGESTED = (100000, 65536)


def overlap(ranges, own):
    """Why RANGES ([(first, count)]) make a map the kernel refuses, or None: one holds the user's OWN ID (mapped
    as root of the namespace already) or 0 (the system's root), or two overlap ("write to uid_map failed:
    Invalid argument")."""
    spans = sorted((first, first + count) for first, count in ranges if count)
    for first, end in spans:
        if first <= own < end:
            return f"{first}-{end - 1} holds your own ID {own}"
        if first == 0:
            return f"{first}-{end - 1} holds ID 0 (root)"
    for (a, a_end), (b, _b_end) in zip(spans, spans[1:]):
        if b < a_end:
            return f"{a}-{a_end - 1} and {b}-{_b_end - 1} overlap"
    return None


def subid_advice(user, mapped):
    """What gives USER enough IDs: the usual 100000-165535 where this system has them, else more IDs from its
    host (never a range the system does not have)."""
    if mapped is not None and outside_map([SUGGESTED], mapped):
        return MORE_IDS
    return (f"make {user}:100000:65536 the only line for {user} in /etc/subuid and /etc/subgid (e.g. sudo usermod "
            f"--add-subuids 100000-165535 --add-subgids 100000-165535 {user}, after removing older lines), then: "
            + RESET)


def check_subids(user=None, uid=None, root=Path("/etc"), proc=Path("/proc/self"), backend="podman", gid=None):
    """The IDs delegated to USER for rootless Podman (and Bubblewrap): every range among the IDs this system has
    (an unprivileged container has few), none holding the user's own ID, 0 or another range, and 65534 in all.
    Bubblewrap needs one line of 65536 that starts above 0."""
    if gid is None:  # this user's group; a caller that names the UID means the same number for its group
        gid = os.getgid() if uid is None else uid
    user, uid = user or getpass.getuser(), os.getuid() if uid is None else uid
    title = "Delegated UID/GID ranges"
    totals = {}
    for name, own, map_name in (("subuid", uid, "uid_map"), ("subgid", gid, "gid_map")):
        ranges, mapped = subid_ranges(root / name, user, uid), id_map(proc / map_name)
        where = f"/etc/{name}"
        bad = outside_map(ranges, mapped) if mapped else None
        if bad:
            have = ", ".join(f"{start}-{start + size - 1}" for start, size in mapped)
            return ("fail", title, f"{where}: {bad[0]}-{bad[0] + bad[1] - 1}, but this system has only the IDs "
                                   f"{have} (an unprivileged container?)", MORE_IDS)
        clash = overlap(ranges, own)
        if clash:
            return ("fail", title, f"{where}: {clash}: the kernel refuses that map", subid_advice(user, mapped))
        totals[name] = sum(count for _, count in ranges)
        if totals[name] < NEEDED:
            return ("fail", title, f"{where}: {totals[name]} IDs for {user}, fewer than {NEEDED}: image builds fail "
                                   "(apt: setgroups 65534)", subid_advice(user, mapped))
        if backend in ("bwrap", "both") and not any(first > 0 and count >= 65536 for first, count in ranges):
            return ("fail", title, f"{where}: Bubblewrap needs one line of at least 65536 IDs that starts above 0",
                    subid_advice(user, mapped))
    return "ok", title, f"{totals['subuid']} UIDs, {totals['subgid']} GIDs", ""


def check_newuidmap(which=shutil.which):
    """newuidmap and newgidmap (package uidmap) map the delegated IDs: they need their setuid bit (root's) or file
    capabilities, or every rootless Podman fails with "write to uid_map failed: Operation not permitted"."""
    for name in ("newuidmap", "newgidmap"):
        found = which(name)
        if not found:
            return "fail", "newuidmap, newgidmap", f"{name} not found", "install the package uidmap"
        try:
            info = os.stat(found)
        except OSError as exc:
            return "fail", "newuidmap, newgidmap", f"{found}: {exc.strerror}", "reinstall the package uidmap"
        if info.st_mode & stat.S_ISUID and info.st_uid == 0:
            continue
        try:
            os.getxattr(found, "security.capability")
        except OSError:
            return ("fail", "newuidmap, newgidmap", f"{found}: neither setuid root nor file capabilities",
                    "sudo apt install --reinstall uidmap (or: sudo setcap cap_setuid,cap_setgid+ep on both)")
    return "ok", "newuidmap, newgidmap", "setuid or file capabilities", ""


def user_bus(uid=None, run=Path("/run/user")):
    """The bus socket of this user's systemd user session, if there is one."""
    path = run / str(os.getuid() if uid is None else uid) / "bus"
    try:
        return path if stat.S_ISSOCK(os.stat(path).st_mode) else None
    except OSError:
        return None


def check_user_session(uid=None, run=Path("/run/user"), backend="podman", podman_url=None):
    """A systemd user session (its bus): limits (--memory, --cpus, --pids) are enforced in it, and Podman's
    systemd cgroup manager needs it. Without one, starts run without limits and builds use cgroupfs."""
    uid = os.getuid() if uid is None else uid
    if podman_url:
        return "skip", "systemd user session", "remote engine", ""
    bus = user_bus(uid, run)
    if bus:
        return "ok", "systemd user session", str(bus), ""
    builds = ", builds use --cgroup-manager=cgroupfs" if backend in ("podman", "both") else ""
    return ("warn", "systemd user session", f"none: --memory, --cpus and --pids are not enforced{builds}",
            f"sudo loginctl enable-linger {getpass.getuser()}, install dbus-user-session, then log in again by ssh "
            "or on the desktop, not with su or sudo (in a container, it needs systemd as its init)")


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
    if code == NO_ANSWER:
        return ("fail", "Podman", f"podman info did not answer within {PODMAN_TIMEOUT} s",
                "check `podman info` (a hung podman process, a remote engine that does not answer); the checks "
                "that need Podman are skipped")
    if code != 0:
        return "fail", "Podman", "podman info failed", "check `podman info`; for a remote engine pass --podman-url"
    if rootless != "true":
        return "fail", "Podman", "not rootless", "run Podman as your user (rootless), not as root"
    code, version = run([*podman_base(podman_url), "version", "--format", "{{.Client.Version}}"])
    return "ok", "Podman", f"rootless, {version or 'version unknown'}", ""


TUN_HINT = ("in an LXC or Proxmox container its host passes it in: lxc.cgroup2.devices.allow: c 10:200 rwm and "
            "lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file in /etc/pve/lxc/ID.conf, then restart "
            "the container; on a host: sudo modprobe tun")


def check_pasta(podman_url=None, which=shutil.which, tun=Path("/dev/net/tun")):
    """pasta, the default network of rootless Podman, and the device it needs (/dev/net/tun: an LXC container
    has none unless its host passes it in; pasta fails with "Failed to open() /dev/net/tun")."""
    if podman_url:
        return "skip", "pasta", "remote engine", ""
    found = which("pasta")
    if not found:
        return "fail", "pasta", "not found", "install pasta (package passt); the default network needs it"
    try:
        device = stat.S_ISCHR(os.stat(tun).st_mode)
    except OSError:
        device = False
    if not device:
        return "fail", "pasta", f"{found}, but {tun} is missing: pasta cannot set up the network", TUN_HINT
    return "ok", "pasta", f"{found}, {tun}", ""


def hostinit():
    if "hostinit" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_ai_hostinit_for_doctor", HERE / "hostinit.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["hostinit"] = loaded  # published only once loaded
    return _modules["hostinit"]


def check_init(podman_url=None, found=None, manager=None):
    """Podman's --init takes its init (catatonit) from this host and puts it into the container as PID 1: needed
    only by containers of images that are not sandbox-ai's own and bring no init themselves (MCP image servers,
    --service) - the agents need none, the browser's and Cairn's containers bring sandbox-ai's own. Missing: a
    warning with this distribution's package command (never run for you)."""
    if podman_url:
        return "skip", "Podman init", "remote engine: its own init binary", ""
    h = hostinit()
    found = h.init_binary() if found is None else found
    if found:
        return "ok", "Podman init", f"{found} (--init: MCP image servers, services of your images)", ""
    command = h.install_command([h.INIT_NAME], manager)
    return ("warn", "Podman init",
            f"{h.INIT_NAME} not found: Podman's --init has no init binary here, so an MCP image server or a --service "
            "whose image brings no init of its own is not started (the agents, the browser and Cairn need none)",
            shlex.join(command) if command else f"install the package {h.INIT_NAME}")


def check_image(agent, podman_url=None):
    image = f"localhost/{agent}-sandbox:latest"
    title = f"Image {agent}"
    if not shutil.which("podman"):
        return "skip", title, "no podman", ""
    if not podman_answers(podman_url):
        return "skip", title, "Podman does not answer", ""
    code, version = run([*podman_base(podman_url), "image", "inspect", image, "--format",
                         '{{index .Labels "sandbox-ai.agent-version"}}'])
    if code != 0:
        return "skip" if agent in OPTIONAL else "warn", title, f"{image} missing", f"sandbox-ai build {agent}"
    if not version or version == "<no value>":
        return "ok", title, f"{image} (built before version labels)", f"sandbox-ai build {agent} to update"
    return "ok", title, f"{image}, {agent} {version}", ""


def check_vpn(podman_url=None, modules=Path("/sys/module"), tun=Path("/dev/net/tun")):
    """--vpn: the images the profiles need (localhost/sandbox-ai-vpn for WireGuard, OpenVPN and Tailscale,
    localhost/sandbox-ai-tor for Tor), /dev/net/tun, and whether WireGuard runs in the kernel or as wireguard-go.
    Only once there is a profile (sandbox-ai vpn list)."""
    spec = importlib.util.spec_from_file_location("sandbox_vpn_for_doctor", HERE / "vpn.py")
    vpn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vpn)
    profiles = vpn.list_profiles()
    if not profiles:
        return "skip", "VPN", "no profiles", ""
    if not shutil.which("podman"):
        return "fail", "VPN", "no podman", "--vpn needs the Podman backend"
    if not podman_answers(podman_url):
        return "skip", "VPN", "Podman does not answer", ""
    kinds = set()
    for profile in profiles:
        try:
            kinds.add(vpn.load_profile(profile).get("type"))
        except (OSError, ValueError):
            kinds.add(None)
    # A profile that cannot be read is no reason to ask for an image; one of a known type needs its own.
    images = sorted({vpn.image({"type": kind}) for kind in kinds if kind})
    missing = [image for image in images if run([*podman_base(podman_url), "image", "exists", image])[0] != 0]
    if missing:
        return ("warn", "VPN", " and ".join(missing) + " missing",
                "; ".join(vpn.build_command(image) for image in missing))
    # Tor has no tunnel device: /dev/net/tun only matters for the other kinds.
    if not podman_url and not tun.exists() and kinds - {"tor"}:
        return "fail", "VPN", "/dev/net/tun is missing", "sudo modprobe tun"
    if kinds == {"tor"}:
        return "ok", "VPN", f"{len(profiles)} profile(s); Tor only", ""
    kernel = (modules / "wireguard").exists()
    return ("ok", "VPN", f"{len(profiles)} profile(s); WireGuard "
            + ("in the kernel" if kernel or podman_url else
               "as wireguard-go (no wireguard module loaded; an image built without wireguard-go needs "
               "sudo modprobe wireguard)"), "")


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


def check_landlock(supported=None):
    """Landlock on this kernel, which a sandbox shares: what `runtime.py --untrusted COMMAND` (a command of the sandbox
    kept from the agent's login, state, terminal, hooks and MCP relays) can hold. Without it such a command is not
    run at all."""
    title = "Landlock (--untrusted)"
    try:
        found = (supported or launcher().RUNTIME.landlock_supported)()
    except (OSError, AttributeError) as exc:
        return ("warn", title, f"not available ({getattr(exc, 'strerror', None) or exc})",
                "a command of a sandbox cannot be kept from the agent's login and state (--untrusted refuses to run "
                "it): boot a kernel of 5.13 or later with Landlock among its security modules (cat "
                "/sys/kernel/security/lsm; else add lsm=landlock,... to GRUB_CMDLINE_LINUX in /etc/default/grub, "
                "sudo update-grub, reboot)")
    parts = ["files and other processes' memory"] + (["signals"] if found["scope"] else []) + \
        (["Unix sockets (tmux, hooks, MCP relays)"] if found["unix"] else [])
    if found["unix"] and found["scope"]:
        return "ok", title, f"ABI {found['abi']}: {', '.join(parts)}", ""
    missing = ([] if found["scope"] else ["signals to the agent's processes (Linux 6.12)"]) + \
        ([] if found["unix"] else ["the Unix sockets of its terminal, hooks and MCP relays (a newer kernel)"])
    return ("warn", title, f"ABI {found['abi']}: {', '.join(parts)}",
            f"--untrusted keeps a command from the agent's files and memory, not from {' nor from '.join(missing)}: "
            "a newer kernel closes that")


def check_proxy_nat(modules=Path("/sys/module"), builtin=None):
    """The connection proxy redirects with nftables NAT: a loaded module shows in /sys/module, a built-in
    one in modules.builtin. A module that is only installed may still be loaded on demand: a warning."""
    if builtin is None:
        builtin = Path("/lib/modules") / os.uname().release / "modules.builtin"
    try:
        built_in = builtin.read_text()
    except OSError:
        built_in = ""
    missing = [name for name in ("nft_chain_nat", "nft_redir")
               if not (modules / name).exists() and f"/{name}.ko" not in built_in]
    if missing:
        return ("warn", "Connection proxy", "nftables NAT not loaded: " + ", ".join(missing) + " (the kernel may "
                "still load it on demand)", "sudo modprobe -a nft_chain_nat nft_redir (persist in "
                "/etc/modules-load.d/), or start with --no-proxy")
    return "ok", "Connection proxy", "nftables NAT available", ""


def check_syscall_filters(machine=None, environ=None):
    """sandbox-ai's own system call filters (runtime.own_filter) on this machine: made for x86-64 and arm64. On
    another machine a sandbox has Firejail's filter alone, and the modes high and paranoid do not start."""
    title = "Isolation: system calls"
    runtime = launcher().RUNTIME
    machine = machine or os.uname().machine
    value = (os.environ if environ is None else environ).get(launcher().SYSCALL_FILTER_ENV, "").strip().lower()
    if machine not in runtime.SECCOMP_MACHINES:
        return ("warn", title, f"no table of the system calls of {machine}",
                "sandboxes run with Firejail's filter alone here (io_uring, new namespaces and every socket family "
                "stay open to the agent); the security modes high and paranoid do not start")
    if value == "off":
        return ("warn", title, f"switched off in this environment ({launcher().SYSCALL_FILTER_ENV}=off)",
                f"unset {launcher().SYSCALL_FILTER_ENV}: without them the agent reaches io_uring, new namespaces "
                "and every socket family of this host's kernel")
    return ("ok", title, f"{machine}: sandbox-ai's own filters beside Firejail's - no io_uring, no new namespaces "
            "(but with --nested-containers), sockets of the families unix, inet, inet6 and netlink only, calls "
            "newer than its table answered \"no such call\"", "")


def check_io_uring(sysctl=Path("/proc/sys/kernel/io_uring_disabled")):
    """The host's io_uring: every sandbox's filter refuses it; the host's own setting closes it for all programs."""
    title = "Isolation: io_uring"
    try:
        value = sysctl.read_text().strip()
    except OSError:
        return "ok", title, "this kernel has no switch for it (before 6.6); refused in every sandbox by its filter", ""
    if value == "2":
        return "ok", title, "disabled for the whole host (kernel.io_uring_disabled=2)", ""
    return ("ok", title, f"kernel.io_uring_disabled={value}: refused in every sandbox by its own filter; for the "
            "whole host: sudo sysctl -w kernel.io_uring_disabled=2 (kept in /etc/sysctl.d/)", "")


def check_lsm(policy=None, modules=None, backend="podman"):
    """AppArmor or SELinux for the sandboxes: rootless Podman loads no profile of its own, so they run unconfined by
    them unless the administrator's policy names one (lsm)."""
    title = "Isolation: security module"
    names = {"apparmor": "AppArmor", "selinux": "SELinux"}
    try:
        policy = launcher().load_policy() if policy is None else policy
    except ValueError as exc:
        return "warn", title, str(exc), "correct the policy file"
    modules = launcher().host_lsm() if modules is None else modules
    wanted = policy.get("lsm") or {}
    active = " and ".join(names[name] for name in modules) or "neither AppArmor nor SELinux"
    if wanted:
        kind, name = next(iter(wanted.items()))
        if kind not in modules:
            return ("warn", title, f"the policy asks for the {names[kind]} confinement {name}, and this host runs "
                    f"{active}", f"no sandbox starts so: enable {names[kind]}, or take lsm out of "
                    f"{launcher().POLICY_FILE}")
        if backend == "bwrap":
            return ("warn", title, f"the policy asks for the {names[kind]} confinement {name}, which Bubblewrap "
                    "cannot apply", "use --backend podman")
        return ("ok", title, f"every sandbox's container is started as {name} ({names[kind]}, the administrator's "
                "policy); its supervisor starts nothing outside it", "")
    return ("ok", title, f"this host runs {active}; sandboxes are not confined by them (rootless Podman loads no "
            "profile of its own) - their namespaces, filters and empty capability set hold without. An "
            f"administrator's profile is named in {launcher().POLICY_FILE}: \"lsm\": {{\"apparmor\": \"PROFILE\"}} "
            "(loaded with sudo apparmor_parser -r FILE) or {\"selinux\": \"TYPE\"}", "")


def check_disk(path=None, statvfs=os.statvfs):
    """The disk behind sandbox-ai's state (the HOMEs of the sandboxes, their logs): no sandbox has a quota of its
    own there. Below the strict modes' reserve a run in high or paranoid is ended."""
    title = "Isolation: disk"
    path = Path(path or os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        info = statvfs(path)
    except OSError as exc:
        return "skip", title, f"{path}: {exc.strerror}", ""
    free = info.f_bavail * info.f_frsize
    reserve = launcher().DISK_RESERVE["stop"][0]
    quota = ("no sandbox has a quota of its own (a project quota or a file system of its own for the folders it "
             "writes is the host's to set: sudo xfs_quota -x -c 'limit -p bhard=20g PROJECT' MOUNTPOINT, or for "
             "ext4 sudo setquota -P PROJECT 0 20G 0 0 MOUNTPOINT)")
    if free < 2 * reserve:
        return ("warn", title, f"{free >> 20} MiB free behind {path}",
                f"free space: below {reserve >> 20} MiB a run in the modes high and paranoid is ended, and a full "
                f"disk ends every run's records; {quota}")
    return "ok", title, f"{free >> 30} GiB free behind {path}; {quota}", ""


def check_firejail_version(run_command=None):
    """Bubblewrap backend: the host's Firejail. Its --restrict-namespaces (0.9.72) is the second filter beside
    sandbox-ai's own; upstream supports its latest release only."""
    title = "Firejail version"
    status, out = (run_command or run)(["firejail", "--version"])
    version = re.search(r"firejail version ([0-9][0-9.]*)", out or "")
    if status != 0 or not version:
        return "skip", title, "not found", ""
    numbers = tuple(int(part) for part in version[1].rstrip(".").split(".") if part.isdigit())
    if numbers < (0, 9, 72):
        return ("warn", title, f"{version[1]}: older than 0.9.72 (no --restrict-namespaces)",
                "upgrade the host's firejail package; sandbox-ai's own filter refuses new namespaces all the same")
    return "ok", title, f"{version[1]} (started with --noprofile; its folder in the sandbox's HOME is sealed)", ""


def check_login(agent):
    """The login the agent's sandboxes get (sandbox-ai's login store first, else the host's);
    never prints token contents."""
    l = launcher()
    where, problem = l.login_state(agent)
    source = l.auth_sources(agent)[0][0]
    title = f"Login {agent}" + (" (login store)" if where == "store" else "")
    if problem == "missing" and agent == "claude" and where == "host" and l.claude_keychain_login():
        return ("warn", title, "Claude Code keeps this Mac's login in the Keychain, which sandboxes cannot use",
                f"sandbox-ai login {agent}")
    if problem == "missing":
        return "skip" if agent in OPTIONAL else "warn", title, f"{source}: no login", \
            f"sandbox-ai login {agent} (or log in on the host)"
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


def check_secrets(store=None):
    """Where sandbox-ai's secrets are kept and what protects them (secret_store.protection): the system's keyring
    or Keychain, the primary password of the private files (locked or unlocked), or nothing - plain files."""
    try:
        store = store or launcher().load_secret_store_module()
        info = store.protection()
        text = store.protection_text(info)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return "fail", "Secrets", str(exc), "sandbox-ai secrets status"
    # OpenVPN profiles and the wizard's token files still plain beside the store (encrypted since 0.1.98)
    files = getattr(store, "plain_files_text", lambda info: "")(info)
    text += f"; {files}" if files else ""
    plain = info["plain"] or info.get("plain_files")
    if plain and info["password"]:
        return "warn", "Secrets", text, "sandbox-ai secrets unlock"
    if plain and os.environ.get(store.PLAIN_CHOICE) == "1":
        return "ok", "Secrets", f"{text} - your choice ({store.PLAIN_CHOICE}=1)", ""
    if info["plain"] or (plain and info["backend"] == "file"):
        return "warn", "Secrets", text, store.SET_PASSWORD
    if plain:
        return "warn", "Secrets", text, "sandbox-ai vpn check NAME (encrypts its profile), or a start that uses the token"
    return "ok", "Secrets", text, ""


VERSION_CHECK = "sandbox-ai"  # the title of the first check: this sandbox-ai, where it is installed, its Python
VERSION_ID = "sandbox-ai-version"  # its "id" in --json (with "version", "kind", "path", "prefix", "python")
PACKAGINGS = ("pyz", "appimage", "macos-app")  # PACKAGING beside sandbox/ of a ready-made package (main.PACKAGES)
WHERE = {"system": "system installation in {prefix}", "user": "the user's own installation", "checkout": "a checkout",
         "pip": "a pip installation", "package": "a package ({package})", "unknown": "installed"}


def hosts_module():
    if "hosts" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_hosts_for_doctor", HERE / "hosts.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["hosts"] = loaded  # published only once loaded
    return _modules["hosts"]


def installation_facts(here=None):
    """{"version", "kind", "path", "prefix", "package", "python", "python_path"} of this running copy of sandbox-ai,
    read as `sandbox-ai hosts` reads another host's `version --verbose` (hosts.installation): KIND "system" (PREFIX/
    lib/sandbox-ai/versions/X), "user" (the user's own), "package", "pip", "checkout" or "unknown". Nothing is asked
    over the network: whether an update is there is `sandbox-ai update --check`'s (and the hosts list's) to say."""
    here = Path(here or HERE.parent)
    version = launcher().__version__
    lines = [f"sandbox-ai {version}", f"installed in: {here}"]
    try:
        package = (here / "PACKAGING").read_text().strip()
    except (OSError, UnicodeDecodeError):
        package = None
    if package in PACKAGINGS:
        lines.append(f"package: {package}")
    elif not (here / "install.sh").is_file():
        lines.append("updates: not through install.sh")
    try:
        info = hosts_module().installation("\n".join(lines))
    except Exception:  # noqa: BLE001 - the doctor still says the version and the place
        info = {"kind": "unknown", "path": str(here), "prefix": None}
    return {"version": version, "kind": info.get("kind") or "unknown", "path": info.get("path") or str(here),
            "prefix": info.get("prefix"), "package": info.get("package"),
            "python": ".".join(map(str, sys.version_info[:3])), "python_path": sys.executable or ""}


def where_text(facts):
    """Where FACTS (installation_facts) say this sandbox-ai is, in a few words with its path."""
    words = WHERE.get(facts.get("kind"), WHERE["unknown"]).format(prefix=facts.get("prefix") or "?",
                                                                  package=facts.get("package") or "?")
    return f"{words} ({facts.get('path')})"


def check_installation(here=None):
    """The first check: this sandbox-ai's version, where it is installed (the user's own or a system installation and
    its prefix, a package, pip, a checkout) and the Python it runs with; a checkout is a warning (no workspace)."""
    facts = installation_facts(here)
    python = f"Python {facts['python']}" + (f" ({facts['python_path']})" if facts["python_path"] else "")
    detail = f"{facts['version']}, {where_text(facts)}; {python}"
    checkout = (Path(here or HERE.parent) / ".git").exists()
    hint = "install it (./install.sh --from .) and never use the checkout as a workspace" if checkout else ""
    return ("warn" if checkout else "ok"), VERSION_CHECK, detail, hint


def path_order_module():
    if "path_order" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_path_order_for_doctor", HERE / "path_order.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["path_order"] = loaded  # published only once loaded
    return _modules["path_order"]


def check_path_order(here=None, path=None):
    """sandbox-ai and sandbox-ai-app installed more than once in different versions (a system installation before the
    user's own in PATH, 2026-11-18): which one PATH finds first, which version is installed where, and the ways out."""
    try:
        return path_order_module().doctor_check(Path(here or HERE.parent), path)
    except Exception as exc:  # noqa: BLE001 - the other checks still run
        return "skip", "Commands in PATH", f"not checked: {exc}", ""


def updates_module():
    if "updates" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_updates_for_doctor", HERE / "updates.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["updates"] = loaded  # published only once loaded
    return _modules["updates"]


UPDATES_CHECK = "Updates"


def check_updates(status=None):
    """The update mode (sandbox-ai update mode): which, its timer (made by update mode; download and auto need it),
    the last check and what it found - a newer release, one waiting for its window, a timer that is not active."""
    try:
        data = status if status is not None else updates_module().status()
    except Exception as exc:  # noqa: BLE001 - the other checks still run
        return "skip", UPDATES_CHECK, f"not checked: {exc}", ""
    U = updates_module()
    mode, timer = data["mode"], data["timer"]
    words = [f"mode {mode}" + ("" if data.get("chosen") else " (the default)")]
    if mode == "off":
        return "ok", UPDATES_CHECK, words[0] + ": nothing is checked unless asked", ""
    if timer["installed"]:
        words.append(f"timer {U.TIMER_NAME} " + {True: "active", False: "NOT active", None: "made"}[timer["active"]])
    else:
        words.append("no timer (checked when sandbox-ai is used on a terminal or the app starts)")
    words.append(f"last check {U.shown(data.get('last_check'))}" +
                 (f": {U.result_text(data['result'])}" if data.get("result") else ""))
    if data.get("waiting"):
        words.append(data["waiting"].get("text", "waiting"))
    detail = "; ".join(words)
    if timer["installed"] and timer["active"] is False:
        return "warn", UPDATES_CHECK, detail, (f"turn it on again: sandbox-ai update mode {mode}" +
                                               (" (or systemctl --user enable --now sandbox-ai-update.timer)"
                                                if timer["kind"] == "systemd" else ""))
    if mode in U.SELF_MODES and not timer["installed"]:
        return "warn", UPDATES_CHECK, detail, f"make the timer: sandbox-ai update mode {mode}"
    if (data.get("result") or {}).get("newer"):
        way = "sandbox-ai update" + (" --apply" if data.get("downloaded") else "") if data.get("self_updating") \
            else data.get("manual") or "sandbox-ai update"
        return "warn", UPDATES_CHECK, detail, f"a newer release is there: {way}"
    return "ok", UPDATES_CHECK, detail, ""


PACKAGES_CHECK = "Package guard"


def packages_module():
    if "packages" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_packages_for_doctor", HERE / "packages.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["packages"] = loaded  # published only once loaded
    return _modules["packages"]


def check_packages(status=None):
    """The package guard's offline copy of the known malicious packages (sandbox-ai packages): its age, its entries,
    whether a release key signed it. Without a copy, with an old or a damaged one, the guard misses malware that is
    known: a warning with the command that fetches it."""
    try:
        data = status if status is not None else packages_module().status()
    except Exception as exc:  # noqa: BLE001 - the other checks still run
        return "skip", PACKAGES_CHECK, f"not checked: {exc}", ""
    sync = "sandbox-ai packages sync (the signed copy; --osv builds it here from OSV's dumps)"
    if not data.get("present"):
        why = f"its copy of the known malicious packages cannot be read ({data['problem']})" if data.get("problem") \
            else "no copy of the known malicious packages on this machine"
        return "warn", PACKAGES_CHECK, f"{why}: release age and new names are still checked", sync
    age = data.get("age_days")
    detail = (f"known malicious packages: {data.get('total', 0)} entries, {age} day{'' if age == 1 else 's'} old "
              f"(made {data.get('generated')}), "
              + ("signed by a release key" if data.get("signed") else
                 f"not signed ({data.get('unsigned') or 'no release key vouches for it'})"))
    if data.get("problem"):
        return "warn", PACKAGES_CHECK, f"{detail}; {data['problem']}", sync
    if data.get("stale"):
        return "warn", PACKAGES_CHECK, detail + ": new malware is missing", sync
    return "ok", PACKAGES_CHECK, detail, ""


def kits():
    if "kits" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_kits_for_doctor", HERE / "kits.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["kits"] = loaded  # published only once loaded
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
    names = k.available_kits(builtin=False)
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
                    f"sandbox-ai wizard trust {shlex.quote(str(path))}")
    except (OSError, ValueError) as exc:
        return "warn", "Project configuration", l.printable(exc), ""
    return "ok", "Project configuration", f"{path} trusted", ""


def check_mode(cwd=None):
    """The security mode a start in CWD gets without --mode (the project file's, yours, the policy's floor), and
    the modes there are."""
    l = launcher()
    names = ", ".join(l.MODES.NAMES)
    try:
        a = l.parse_args(["codex", "--workspace", str(Path(cwd or os.getcwd()))])
        try:
            path, document = l.load_project_config(a)
        except ValueError:
            path, document = None, None  # Project configuration reports it
        name, origin = l.MODES.resolve(None, document, path, l.load_user_defaults(a), l.defaults_file(),
                                       l.load_policy(), l.POLICY_FILE)
    except (OSError, ValueError) as exc:
        return "warn", "Security mode", l.printable(exc), "fix the file named, or choose a mode with --mode"
    return "ok", "Security mode", f"{name} ({origin}); modes: {names}", ""


# The settings files a user (or the app) writes, in ~/.config/sandbox-ai: each read where it is used, and a damaged one
# stops only that use - said here together, with the files that are fine left unnamed.
SETTINGS_FILES = ("defaults.json", "llm.json", "llm-shares.json", "mcp.json", "hosts.json", "identities.json",
                  "git-logins.json", "publish-logins.json", "trusted.json", "app/settings.json", "app/presets.json")


def check_settings_files(config=None):
    """sandbox-ai's settings files that are there but not readable JSON objects (a crash while one was written, a
    full disk, an editor): each named with why."""
    config = Path(config or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai")
    damaged = []
    for name in SETTINGS_FILES:
        path = config / name
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            damaged.append(f"{name}: {exc.strerror or exc}")
            continue
        try:
            if not isinstance(json.loads(data.decode("utf-8")), dict):
                damaged.append(f"{name}: not a JSON object")
        except (ValueError, UnicodeDecodeError, RecursionError):
            damaged.append(f"{name}: not valid JSON" + (" (empty)" if not data.strip() else ""))
    if damaged:
        return ("warn", "Settings files", launcher().printable("; ".join(damaged)),
                f"fix or remove the file named in {config} (what it held is gone with it)")
    return "ok", "Settings files", f"readable ({config})", ""


def check_sessions(state=None):
    """Session folders without a readable session.json (damaged): every command refuses them by name."""
    state = Path(state or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai")
    folder = state / "sessions"
    try:
        entries = sorted(path for path in folder.iterdir() if path.is_dir())
    except OSError:
        return "skip", "Sessions", "none", ""
    damaged = []
    for path in entries:
        try:
            if not isinstance(json.loads((path / "session.json").read_text()), dict):
                damaged.append(path)
        except (OSError, ValueError, RecursionError, UnicodeDecodeError):
            damaged.append(path)
    if damaged:
        printable = launcher().printable
        return ("warn", "Sessions", f"{len(damaged)} damaged (no readable session.json): "
                + ", ".join(printable(path.name) for path in damaged),
                "nothing of it can be resumed; remove its folder: rm -r "
                + " ".join(shlex.quote(str(path)) for path in damaged))
    return "ok", "Sessions", f"{len(entries)} kept, none damaged", ""


def check_remote_folders(which=shutil.which, platform=None):
    """sshfs for remote folders (--workspace sshfs://..., --mount, workspace add): optional - not installed is
    "skip" with how to install it (remotefs.doctor_check)."""
    if "remotefs" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_ai_remotefs_for_doctor", HERE / "remotefs.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["remotefs"] = loaded
    return _modules["remotefs"].doctor_check(which, platform)


def check_orphans(entries=None):
    """Starts that run without their supervisor (orphaned: approvals, net allow, workspace add and the kill switch do
    not reach them): each with why - its supervisor's end as recorded - and what to do. ENTRIES: starts.entries."""
    try:
        found = (entries or (lambda: launcher().load_starts_module().entries(prune=False)))()
    except Exception as exc:  # noqa: BLE001 - the other checks go on
        return "skip", "Orphaned starts", f"not looked at ({launcher().printable(str(exc))[:200]})", ""
    orphans = [entry for entry in found if entry.get("state") == "orphaned"]
    if not orphans:
        return "ok", "Orphaned starts", "none (every running start has its supervisor)", ""
    printable = launcher().printable
    return ("fail", "Orphaned starts", f"{len(orphans)} run without their supervisor: "
            + ", ".join(printable(entry["name"]) for entry in orphans),
            " | ".join(f"{printable(entry['name'])}: {printable(entry.get('orphaned_why') or '')}" for entry in orphans))


def check_mcp_network(servers=None, problem=None):
    """The catalog MCP servers with network: where each reaches (the internet without your local network, only its
    hosts, or your network too), that this host can load their egress rules (nft, nsenter: without them they do not
    start), and what to change - a full server whose catalog entry needs only its hosts, one that reaches your
    network. SERVERS: mcp.server_list(); PROBLEM: mcp_egress.prerequisite_problem."""
    try:
        mcp = launcher().load_mcp_module()
        found = mcp.server_list() if servers is None else servers
        networked = [entry for entry in found if entry.get("network") not in (None, "none", "offline")]
        if networked and problem is None:
            problem = mcp.egress_module().prerequisite_problem("podman")
    except Exception as exc:  # noqa: BLE001 - the other checks go on
        return "skip", "MCP network", f"not looked at ({launcher().printable(str(exc))[:200]})", ""
    if not networked:
        return "skip", "MCP network", "no catalog server with network", ""
    printable = launcher().printable
    detail = "; ".join(printable(f"{entry['name']}: {entry['network']}") for entry in networked)
    if problem:
        return "fail", "MCP network", printable(problem), "install nftables and util-linux (nft, nsenter)"
    notes = [printable(entry["egress_note"]) for entry in networked if entry.get("egress_note")]
    if notes:
        return "warn", "MCP network", detail, " | ".join(notes)
    return "ok", "MCP network", detail, ""


def check_mcp_images(servers=None):
    """The catalog MCP servers of yours whose catalog pins a newer image (or rootfs) than they run - a definition
    keeps the digest it was added with - as each catalog was last read here (nothing downloaded): `sandbox-ai mcp
    update` takes it (its signature checked again, the definition's folders, variables, limits and network kept).
    SERVERS: mcp.server_list()."""
    try:
        mcp = launcher().load_mcp_module()
        found = mcp.server_list() if servers is None else servers
    except Exception as exc:  # noqa: BLE001 - the other checks go on
        return "skip", "MCP images", f"not looked at ({launcher().printable(str(exc))[:200]})", ""
    catalog = [entry for entry in found if entry.get("own") and entry.get("kind") in ("image", "rootfs")
               and (entry.get("definition") or {}).get("source")]
    if not catalog:
        return "skip", "MCP images", "no catalog server of yours", ""
    newer = [entry for entry in catalog if entry.get("newer")]
    printable = launcher().printable
    if not newer:
        return "ok", "MCP images", f"{len(catalog)} catalog server{'s' if len(catalog) != 1 else ''}, each with the " \
            "image its catalog pins (as the catalogs were last read here: sandbox-ai mcp update --check reads them " \
            "now)", ""
    names = [entry["name"] for entry in newer]
    unsigned = any(not entry["newer"].get("signed") for entry in newer)
    return ("warn", "MCP images", printable("; ".join(mcp.newer_text(entry["name"], entry["newer"]) for entry in newer)),
            printable(f"sandbox-ai mcp update {' '.join(names)}" + (" --unsigned" if unsigned else "")))


MCP_FAILURE_DAYS = 7  # how long a server's last failure is said by doctor


def check_mcp_servers(servers=None, now=None):
    """The MCP servers' health as this host saw it: one whose last failure (its container watch's, its bridge's:
    mcp.last_problem) is of the last MCP_FAILURE_DAYS days - with when, why, the fix and its log - and those below
    the limits their catalog recommends (they fail later, Chromium and the JVMs first). `sandbox-ai mcp check --all`
    starts each to find out now. SERVERS: mcp.server_list()."""
    try:
        mcp = launcher().load_mcp_module()
        found = mcp.server_list() if servers is None else servers
    except Exception as exc:  # noqa: BLE001 - the other checks go on
        return "skip", "MCP servers", f"not looked at ({launcher().printable(str(exc))[:200]})", ""
    if not found:
        return "skip", "MCP servers", "none defined", ""
    printable = launcher().printable
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - MCP_FAILURE_DAYS * 86400))
    failed = [entry for entry in found if isinstance(entry.get("last_problem"), dict)
              and str(entry["last_problem"].get("time") or "") >= since]
    below = [entry for entry in found if entry.get("below_recommended")]
    if not failed and not below:
        return "ok", "MCP servers", f"{len(found)} defined, no failure in the last {MCP_FAILURE_DAYS} days " \
            "(sandbox-ai mcp check --all starts each now)", ""
    detail = "; ".join([printable(f"{entry['name']}: {entry['last_problem'].get('time')}: "
                                  f"{entry['last_problem'].get('why')}")[:400] for entry in failed]
                       + [printable(f"{entry['name']}: below the limits its catalog recommends "
                                    f"({mcp.limits_text(entry['below_recommended'])})") for entry in below])
    fix = " | ".join([printable(f"{entry['name']}: {entry['last_problem'].get('fix')} (its log: "
                                f"{entry['last_problem'].get('log')})")[:500] for entry in failed]
                     + [f"sandbox-ai mcp set {entry['name']} --recommended" for entry in below]
                     + (["then: sandbox-ai mcp check " + " ".join(entry["name"] for entry in failed)] if failed else []))
    return "warn", "MCP servers", detail, fix


def check_machine(runner=None, home=None, tmpdir=None):
    """macOS: podman machine, the Linux VM Podman runs the sandboxes in: running, rootless, sharing HOME and
    $TMPDIR (a workspace or a session's files outside the shares cannot be mounted), its CPUs and memory."""
    runner = runner or run
    code, out = runner(["podman", "machine", "inspect"])
    if code != 0:
        return "fail", "podman machine", "none", "podman machine init --rootful=false && podman machine start"
    try:
        machines = json.loads(out)
        machine = machines[0] if isinstance(machines, list) and machines else {}
    except (ValueError, RecursionError):
        machine = {}
    if not isinstance(machine, dict) or not machine:
        return "fail", "podman machine", "podman machine inspect gave nothing usable", "podman machine init"
    name = machine.get("Name", "?")
    if machine.get("State") != "running":
        return "fail", "podman machine", f"{name} is {machine.get('State', 'not running')}", f"podman machine start {name}"
    if machine.get("Rootful"):
        return "fail", "podman machine", f"{name} is rootful", \
            f"sandbox-ai needs rootless Podman: podman machine stop {name}; podman machine set --rootful=false {name}"
    shared = [m.get("Source", "") for m in machine.get("Mounts") or [] if isinstance(m, dict)]
    home = str(home or Path.home())
    tmpdir = str(tmpdir or os.environ.get("TMPDIR") or "/tmp")
    missing = [path for path in (home, tmpdir) if not any(path == s or path.startswith(s.rstrip("/") + "/") for s in shared if s)]
    resources = machine.get("Resources") or {}
    cpus, memory = resources.get("CPUs"), resources.get("Memory")  # memory in MiB
    detail = f"{name}, rootless, {cpus or '?'} CPUs, {round(memory / 1024) if isinstance(memory, int) else '?'} GiB"
    if missing:
        return "fail", "podman machine", f"{detail}; not shared: {', '.join(missing)}", \
            "add a volume: podman machine stop; podman machine set -v /Users:/Users (or init with -v)"
    if isinstance(memory, int) and memory < 9 * 1024:
        return "warn", "podman machine", f"{detail}: less than the sandboxes' default 8 GiB plus the VM's own", \
            "podman machine stop; podman machine set --memory 12288 (or start sandboxes with --memory 4g)"
    return "ok", "podman machine", detail, ""


def checks(backend="podman", podman_url=None, cwd=None):
    if sys.platform == "darwin" and not podman_url:
        # macOS: Podman in podman machine's Linux VM; user namespaces, systemd, pasta, subuids and Bubblewrap are
        # the VM's (or Linux only), not this host's.
        result = [check_installation(), check_path_order(), check_updates(), check_packages(), check_user(),
                  check_python(), check_machine(),
                  check_podman(podman_url)]
        result += [check_image(agent, podman_url) for agent in AGENTS] + [check_vpn(podman_url)]
        result += [check_login(agent) for agent in AGENTS]
        result += [check_kit(agent) for agent in AGENTS] + [check_kits(), check_plugins(), check_memory(cwd)]
        result += [check_git_identity(), check_secrets(), check_project(cwd), check_mode(cwd), check_settings_files(),
                   check_sessions(), check_orphans(), check_remote_folders()]
        return result
    result = [check_installation(), check_path_order(), check_updates(), check_packages(), check_user(),
              check_python(), check_userns(), check_user_session(backend=backend, podman_url=podman_url),
              check_proxy_nat(), check_landlock()]
    if backend in ("podman", "both"):
        result += [check_podman(podman_url), check_pasta(podman_url), check_init(podman_url)]
        if not podman_url:
            result += [check_subids(backend=backend), check_newuidmap()]
        result += [check_image(agent, podman_url) for agent in AGENTS] + [check_vpn(podman_url)]
    if backend in ("bwrap", "both"):
        result += [check_bwrap_tools(), check_tiocsti()]
        if backend == "bwrap":
            result += [check_subids(backend=backend), check_newuidmap()]
        result.append(check_firejail_version())
    # What a sandbox's isolation is made of beyond the engine: sandbox-ai's own system call filters, and what only
    # this host can add (its io_uring switch, an AppArmor or SELinux profile, a quota).
    result += [check_syscall_filters(), check_io_uring(), check_lsm(backend=backend), check_disk()]
    result += [check_login(agent) for agent in AGENTS]
    result += [check_kit(agent) for agent in AGENTS] + [check_kits(), check_plugins(), check_memory(cwd)]
    result += [check_git_identity(), check_secrets(), check_project(cwd), check_mode(cwd), check_settings_files(),
               check_sessions(), check_orphans(), check_remote_folders(), check_mcp_network(), check_mcp_images(),
               check_mcp_servers()]
    return result


def fixes():
    """fixes.py: the fix plan (what is missing and what makes each), as a start uses it."""
    if "fixes" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_ai_fixes_for_doctor", HERE / "fixes.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules["fixes"] = loaded  # published only once loaded
    return _modules["fixes"]


# A check whose hint is nothing but a command of sandbox-ai's own (sandbox-ai build codex, sandbox-ai plugins claude,
# "sandbox-ai login claude (or log in on the host)"): that command is its fix.
OWN_COMMAND = re.compile(r"(sandbox-ai (?:build|login|plugins) [a-z0-9@._ -]+?)(?: \(or [^)]*\)| to update)?")
TOOLS_OF = {"Podman": ["podman", "pasta"], "pasta": ["pasta"], "newuidmap, newgidmap": ["newuidmap"],
            "MCP network": ["nft", "nsenter"]}


def fix_of(status, title, detail, hint):
    """The fix (fixes.py) of one check that warns or fails, or None: a host tool that is not installed is its
    distribution's package command (shown, never run - it needs root); an image not built is built by `doctor
    --fix`; plugins not installed and a login that is missing are sandbox-ai's own commands that talk to the human
    (shown: not needed for a start, run when chosen)."""
    if status not in ("warn", "fail") and not (status == "skip" and title.startswith("Remote folders")):
        return None
    f = fixes()
    lacking = f"{title}: {detail}"
    if title == "Podman init":
        return f.init_fix()
    if title in TOOLS_OF and "not found" in detail:
        return f.tool_fix(TOOLS_OF[title], missing=lacking)
    if title == "Bubblewrap tools" and detail.startswith("missing: "):
        return f.tool_fix(detail[len("missing: "):].split(", "), missing=lacking)
    if title == "Delegated UID/GID ranges" and "usermod" in (hint or ""):
        # The usual range where this system has it (subid_advice): root's commands, shown, never run.
        user = getpass.getuser()
        return f.fix("host", "subids", lacking, "give your user the IDs 100000-165535 (its older lines for it removed "
                     "first), then let Podman take them",
                     [["sudo", "usermod", "--add-subuids", "100000-165535", "--add-subgids", "100000-165535", user],
                      ["podman", "system", "migrate"]],
                     manual="it needs root: sandbox-ai never runs sudo for you (podman system reset too, if images "
                            "were pulled or built before: it removes this user's images and containers)")
    if title == "Delegated UID/GID ranges":
        return f.fix("host", "subids", lacking, "give this system more IDs from its host",
                     manual=MORE_IDS)
    if title == "systemd user session" and status == "warn" and "enable-linger" in (hint or ""):
        user = getpass.getuser()
        package = f.hostinit().install_command(["dbus-user-session"]) if f.hostinit().package_manager()[0] == "apt" \
            else []
        return f.fix("host", "user-session", lacking, "let your user's systemd session run, then log in again (by "
                     "ssh or on the desktop, not with su or sudo)",
                     [["sudo", "loginctl", "enable-linger", user], *([package] if package else [])], needed=False,
                     manual="it needs root: sandbox-ai never runs sudo for you")
    if title.startswith("Remote folders") and "not installed" in detail:
        return remote_fixes()[0] if remote_fixes() else None
    match = OWN_COMMAND.fullmatch(hint or "")
    if match:
        words = match[1].split()
        kind = {"build": "image", "login": "login", "plugins": "plugin"}[words[1]]
        return f.fix(kind, " ".join(words[2:]), lacking,
                     {"image": "build it", "login": "log in inside a sandbox, in this terminal",
                      "plugin": "install them (the plugin installer's sandbox, in this terminal)"}[kind], [words],
                     duration="~5 min" if kind == "image" else "", terminal=kind != "image",
                     needed=kind == "image")
    return None


def remote_fixes():
    """The fix for sshfs that is not installed (remotefs.sshfs_fixes): optional here - only remote folders need it."""
    check_remote_folders(which=lambda name: None)  # (loads remotefs.py once)
    return _modules["remotefs"].sshfs_fixes(needed=False)


def fix_plan(results):
    """The fixes of RESULTS (checks()), each once, what a start needs first."""
    found = [fix_of(*result) for result in results]
    return fixes().unique([item for item in found if item is not None])


def fix_lines(plan):
    """What the report says after its table when checks have fixes: the commands, one per line, to copy - and that
    `sandbox-ai doctor --fix` runs those that are sandbox-ai's own."""
    f = fixes()
    commands = [f.command_text(command) for item in plan for command in item["commands"]]
    if not commands:
        return []
    own = [item for item in plan if item["verdict"] == "offered" and item["needed"]]
    lines = ["What fixes it (copy these lines):", *(f"  {command}" for command in commands)]
    if own:
        lines.append(f"sandbox-ai doctor --fix runs {'the build' if len(own) == 1 else f'the {len(own)} builds'} "
                     "among them, without asking"
                     + ("; a login, the plugin installer and what needs root are yours to run"
                        if len(own) < len(plan) else ""))
    else:
        lines.append("They need root: sandbox-ai never runs sudo for you.")
    return lines


SYMBOLS = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "--  "}
# ok green, warnings orange, problems red, what was not checked grey.
KINDS = {"ok": "ok", "warn": "orange", "fail": "bad", "skip": "muted"}
# The table's sections, by the checks' titles (a title that fits none goes to the last).
GROUPS = (("System", ("sandbox-ai", "Commands in PATH", "Updates", "Package guard", "Ordinary user", "Python", "User namespaces",
                      "systemd user session", "Connection proxy", "Landlock", "Remote folders", "Secrets")),
          ("Engine", ("Podman", "podman machine", "pasta", "Delegated UID/GID ranges", "newuidmap", "Image ", "VPN", "Bubblewrap",
                      "TIOCSTI", "Firejail", "nft", "setpriv", "slirp", "MCP network", "Isolation")),
          ("Agents", ("Login ", "Kit ", "Kits", "Plugins")),
          ("Project", ()))


def group_of(title):
    for name, prefixes in GROUPS:
        if any(title == prefix or title.startswith(prefix) for prefix in prefixes):
            return name
    return GROUPS[-1][0]


def summary(results):
    failed = sum(status == "fail" for status, *_ in results)
    warned = sum(status == "warn" for status, *_ in results)
    text = f"{failed} problem(s), {warned} warning(s)" if failed or warned else "Everything needed is in place."
    return text, "bad" if failed else "orange" if warned else "ok"


def report(results, out=print, colour=None, plain=False):
    """The checks for a human: a table by section (status, check, result, the hint below it), wrapped at the
    terminal's width, in colour on a terminal (ok green, WARN orange, FAIL red); PLAIN: one line per check, as
    before. 1 if a check failed."""
    t = textout()
    colour = t.colour_enabled() if colour is None else colour
    clean = [(status, title, launcher().printable(detail), launcher().printable(hint))
             for status, title, detail, hint in results]
    if plain:
        width = max(len(title) for _, title, _, _ in clean)
        for status, title, detail, hint in clean:
            out(f"{t.style(SYMBOLS[status], KINDS[status], colour)}  {title.ljust(width)}  {detail}")
            if hint and status in ("warn", "fail"):
                out(f"{' ' * (width + 8)}{t.style('-> ' + hint, KINDS[status], colour)}")
    else:
        width = max(len(title) for _, title, _, _ in clean)
        lead = 2 + 4 + 2 + width + 2  # indent, status, gap, check, gap
        room = max(30, t.width() - lead)
        for name, _ in GROUPS:
            rows = [row for row in clean if group_of(row[1]) == name]
            if not rows:
                continue
            out("")
            t.section(name, out, colour)
            for status, title, detail, hint in rows:
                lines = t.wrap(detail, room) if detail else [""]
                out(f"  {t.style(SYMBOLS[status], KINDS[status], colour)}  {title.ljust(width)}  {lines[0]}".rstrip())
                for more in lines[1:]:
                    out(" " * lead + more)
                if hint and status in ("warn", "fail"):
                    for index, part in enumerate(t.wrap(hint, room - 3)):
                        out(" " * lead + t.style(("-> " if index == 0 else "   ") + part, KINDS[status], colour))
    text, kind = summary(clean)
    out("")
    out(t.style(text, kind, colour))
    return 1 if any(status == "fail" for status, *_ in clean) else 0


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-ai doctor", description="Check the host for sandbox-ai.")
    parser.add_argument("-b", "--backend", choices=["podman", "bwrap", "both"], default="podman", help="which backend to check: podman, bwrap or both")
    parser.add_argument("-pu", "--podman-url", help="the URL of the Podman API socket to use instead of the default")
    parser.add_argument("-j", "--json", action="store_true", help="Machine-readable output")
    parser.add_argument("-p", "--plain", action="store_true", help="One line per check, without the table")
    parser.add_argument("-nc", "--no-color", action="store_true",
                        help="Without colour (also without a terminal, with NO_COLOR or TERM=dumb)")
    parser.add_argument("-f", "--fix", action="store_true",
                        help="After the checks: build the images that are not built, without asking; a missing "
                             "login and plugins that are not installed are shown with their commands (they talk to "
                             "you), a host package that is missing with your distribution's command (never installed "
                             "for you)")
    a = parser.parse_args(argv)
    results = checks(a.backend, a.podman_url)
    if a.json:
        # "group": the area of the table (System, Engine, Agents, Project), for the app and other readers. The first
        # check, this sandbox-ai, also has "id": "sandbox-ai-version" and its facts (version, kind, path, prefix,
        # package, python, python_path): the Hosts page heads a host's doctor with them.
        entries = []
        for r in results:
            entry = {**dict(zip(("status", "check", "detail", "hint"), r)), "group": group_of(r[1])}
            fix = fix_of(*r)  # (what fixes it, as a start's fix plan says it: its commands, whether it needs root)
            if fix is not None:
                entry["fix"] = fix
            if r[1] == VERSION_CHECK:
                entry = {"id": VERSION_ID, **entry, **installation_facts()}
            entries.append(entry)
        print(json.dumps(entries, indent=2))
        return 1 if any(r[0] == "fail" for r in results) else 0
    colour = False if a.no_color else None
    status = report(results, colour=colour, plain=a.plain)
    plan = fix_plan(results)
    if plan:
        print("")
        for line in fix_lines(plan):
            print(launcher().printable(line))
    if a.fix:
        f = fixes()
        # (the builds; a login or the plugin installer talks to you: its command is in the block, yours to run)
        own = [item for item in plan if item["verdict"] == "offered" and item["needed"]]
        print("")
        if not own:
            print("sandbox-ai doctor --fix: nothing here is sandbox-ai's own to fix")
            return status
        try:
            f.run(own, out=lambda text: print(f"sandbox-ai doctor: {text}", file=sys.stderr),
                  printable=launcher().printable, again="run sandbox-ai doctor again")
        except ValueError as exc:
            print(f"sandbox-ai doctor: {launcher().printable(str(exc))}", file=sys.stderr)
            return 1
        print("sandbox-ai doctor: done; run it again to see the host as it is now", file=sys.stderr)
        return status
    t = textout()
    print("")
    t.section("Security modes", colour=t.colour_enabled() if colour is None else colour,
              note="--mode, \"mode\" in the project file or in ~/.config/sandbox-ai/defaults.json")
    for line in launcher().MODES.summaries():
        print(f"  {line}")
    return status


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
