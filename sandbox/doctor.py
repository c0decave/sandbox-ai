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
import stat
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


def textout():
    if "textout" not in _modules:
        spec = importlib.util.spec_from_file_location("sandbox_ai_textout_for_doctor", HERE / "textout.py")
        _modules["textout"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_modules["textout"])
    return _modules["textout"]


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


def check_image(agent, podman_url=None):
    image = f"localhost/{agent}-sandbox:latest"
    title = f"Image {agent}"
    if not shutil.which("podman"):
        return "skip", title, "no podman", ""
    code, version = run([*podman_base(podman_url), "image", "inspect", image, "--format",
                         '{{index .Labels "sandbox-ai.agent-version"}}'])
    if code != 0:
        return "warn", title, f"{image} missing", f"sandbox-ai build {agent}"
    if not version or version == "<no value>":
        return "ok", title, f"{image} (built before version labels)", f"sandbox-ai build {agent} to update"
    return "ok", title, f"{image}, {agent} {version}", ""


def check_vpn(podman_url=None, modules=Path("/sys/module"), tun=Path("/dev/net/tun")):
    """--vpn: the image, /dev/net/tun, and whether WireGuard runs in the kernel or as wireguard-go. Only once
    there is a profile (sandbox-ai vpn list)."""
    spec = importlib.util.spec_from_file_location("sandbox_vpn_for_doctor", HERE / "vpn.py")
    vpn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vpn)
    profiles = vpn.list_profiles()
    if not profiles:
        return "skip", "VPN", "no profiles", ""
    if not shutil.which("podman"):
        return "fail", "VPN", "no podman", "--vpn needs the Podman backend"
    if run([*podman_base(podman_url), "image", "exists", vpn.VPN_IMAGE])[0] != 0:
        return "warn", "VPN", f"{vpn.VPN_IMAGE} missing", "sandbox-ai build vpn"
    # Tor has no tunnel device: /dev/net/tun only matters for the other kinds.
    kinds = set()
    for profile in profiles:
        try:
            kinds.add(vpn.load_profile(profile).get("type"))
        except (OSError, ValueError):
            kinds.add(None)
    if not podman_url and not tun.exists() and kinds - {"tor"}:
        return "fail", "VPN", "/dev/net/tun is missing", "sudo modprobe tun"
    kernel = (modules / "wireguard").exists()
    return ("ok", "VPN", f"{len(profiles)} profile(s); WireGuard "
            + ("in the kernel" if kernel or podman_url else "as wireguard-go (no wireguard module loaded)"), "")


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
    except ValueError:
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
        result = [check_user(), check_python(), check_installation(), check_machine(), check_podman(podman_url)]
        result += [check_image(agent, podman_url) for agent in AGENTS] + [check_vpn(podman_url)]
        result += [check_login(agent) for agent in AGENTS]
        result += [check_kit(agent) for agent in AGENTS] + [check_kits(), check_plugins(), check_memory(cwd)]
        result += [check_git_identity(), check_project(cwd), check_mode(cwd)]
        return result
    result = [check_user(), check_python(), check_installation(), check_userns(),
              check_user_session(backend=backend, podman_url=podman_url), check_proxy_nat()]
    if backend in ("podman", "both"):
        result += [check_podman(podman_url), check_pasta(podman_url)]
        if not podman_url:
            result += [check_subids(backend=backend), check_newuidmap()]
        result += [check_image(agent, podman_url) for agent in AGENTS] + [check_vpn(podman_url)]
    if backend in ("bwrap", "both"):
        result += [check_bwrap_tools(), check_tiocsti()]
        if backend == "bwrap":
            result += [check_subids(backend=backend), check_newuidmap()]
    result += [check_login(agent) for agent in AGENTS]
    result += [check_kit(agent) for agent in AGENTS] + [check_kits(), check_plugins(), check_memory(cwd)]
    result += [check_git_identity(), check_project(cwd), check_mode(cwd)]
    return result


SYMBOLS = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "--  "}
# ok green, warnings orange, problems red, what was not checked grey.
KINDS = {"ok": "ok", "warn": "orange", "fail": "bad", "skip": "muted"}
# The table's sections, by the checks' titles (a title that fits none goes to the last).
GROUPS = (("System", ("Ordinary user", "Python", "sandbox-ai", "User namespaces", "systemd user session",
                      "Connection proxy")),
          ("Engine", ("Podman", "podman machine", "pasta", "Delegated UID/GID ranges", "newuidmap", "Image ", "VPN", "Bubblewrap",
                      "TIOCSTI", "Firejail", "nft", "setpriv", "slirp")),
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
    parser.add_argument("-b", "--backend", choices=["podman", "bwrap", "both"], default="podman")
    parser.add_argument("-pu", "--podman-url")
    parser.add_argument("-j", "--json", action="store_true", help="Machine-readable output")
    parser.add_argument("-p", "--plain", action="store_true", help="One line per check, without the table")
    parser.add_argument("-nc", "--no-color", action="store_true",
                        help="Without colour (also without a terminal, with NO_COLOR or TERM=dumb)")
    a = parser.parse_args(argv)
    results = checks(a.backend, a.podman_url)
    if a.json:
        print(json.dumps([dict(zip(("status", "check", "detail", "hint"), r)) for r in results], indent=2))
        return 1 if any(r[0] == "fail" for r in results) else 0
    colour = False if a.no_color else None
    status = report(results, colour=colour, plain=a.plain)
    t = textout()
    print("")
    t.section("Security modes", colour=t.colour_enabled() if colour is None else colour,
              note="--mode, \"mode\" in the project file or in ~/.config/sandbox-ai/defaults.json")
    for line in launcher().MODES.summaries():
        print(f"  {line}")
    return status


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
