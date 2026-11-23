#!/usr/bin/env python3
"""Remote folders for a sandbox: a folder of another machine as a workspace or a mount, mounted on this host.

  --workspace sshfs://[USER@]HOST[:PORT]/PATH              a remote folder as /work (or /workspaces/NAME)
  --mount sshfs://[USER@]HOST[:PORT]/PATH:DEST:ro|rw       a remote folder at DEST
  sandbox-ai workspace add [RUN] sshfs://... [--ro] [--name NAME]   the same while the sandbox runs

PATH is absolute on the remote machine (sshfs://dev@build1/srv/project); /~ is the remote HOME and /~/DIR a folder
in it (sshfs://build1/~/project); without a PATH the remote HOME is mounted. HOST is an SSH destination as for
`ssh`: your ~/.ssh/config applies (Host aliases, User, Port, IdentityFile, ProxyJump) and your ssh-agent's keys are
used. Without USER@ and :PORT, a HOST that names a host of `sandbox-ai hosts` is that host: its USER@HOST, port,
--identity and sandbox-ai's own known_hosts.

How: sandbox-ai runs sshfs on this host as you - never in the sandbox - and mounts the folder in a folder of its own
state (XDG_STATE_HOME/sandbox-ai/remote/RUN/N/NAME, only yours: 0700), which the sandbox is then given exactly as a
workspace or --mount of that folder: every check, the warnings, read-only (:ro, --ro, --workspace-ro, the security
mode) and the push check of a workspace apply as for a local folder. The agent never gets /dev/fuse, an SSH key, the
SSH agent or the connection: it sees files. Their traffic goes from this host over SSH, not through the sandbox's
proxy (its allow-list and connection log do not see it). Read-write, the agent changes and deletes files on the
other machine; read-only is the safer choice.

The host key is always checked: never StrictHostKeyChecking=no (a host of `sandbox-ai hosts` learns its first key as
`sandbox-ai hosts` does, accept-new; any other host must be known already, or you are asked in the terminal). A
start in a terminal may ask for a password or a key's passphrase (ssh asks there); a session's supervisor runs in
the background and never asks: its start in a terminal opens the connection first (asking there), and the
supervisor's sshfs uses that connection (an SSH ControlMaster in sandbox-ai's own runtime folder, kept 2 minutes).
A start of the app or the web interface, `resume` without a terminal: only keys (ssh-agent, a key without
passphrase, ~/.ssh/config) - else it says so.

Lifetime: unmounted when the run ends (after its pod), also a folder added during the run; a kept start keeps the
remote source (not the folder) in its options and mounts it again at its next start (sandbox-ai resume). A launcher
that was killed leaves the mount while its sandbox runs on; once the run has ended for certain (sandbox-ai stop,
list, gc) it is unmounted. Nothing below sandbox-ai's remote folder is ever deleted recursively: a mount point is
unmounted and then removed only when empty.

NFS and SMB, in the same places: nfs://SERVER[:PORT]/EXPORT[/PATH][?mount=fuse|sudo&vers=V&hard=1] - fuse-nfs (a
userspace client: no root) when installed or asked for, else the kernel's client through `sudo mount -t nfs`, soft
unless ?hard=1 (NFS_SOFT: a server that is gone fails a call instead of hanging whoever touches the folder) - and
smb://[[DOMAIN;]USER@]SERVER[:PORT]/SHARE[/PATH][?secret=NAME&vers=V] - the kernel's client through `sudo mount -t
cifs` (no userspace SMB client is usable), the files owned by you, the password from the sandbox-ai secret NAME in a
file only you can read, removed right after the mount (as guest without one). Always nosuid,nodev. sudo asks in a
terminal; without one (a session's supervisor, the app) `sudo -n` never asks: a sudoers rule for mount and umount,
else the start says what to do. Unmounted the same way (sudo umount).

macOS: the same with the Mac's sshfs (macFUSE or FUSE-T): the folder is mounted in your HOME, which podman machine
shares with its VM (NFS and SMB: not on macOS yet). Whether the VM's file sharing passes a FUSE mount through depends on its provider (no data
yet: no live check step covers it; when the sandbox sees an empty folder, mount it in the VM yourself: podman machine
ssh).

Standard library only.
"""
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
SCHEMES = ("sshfs", "nfs", "smb")
PREFIXES = tuple(f"{scheme}://" for scheme in SCHEMES)
# sshfs://[USER@]HOST[:PORT][/PATH]; HOST a name, an IPv4 address or [an IPv6 address].
SPEC = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:(?P<user>[^@/\s]*)@)?"
                  r"(?P<host>\[[0-9A-Fa-f:.]+\]|[^:/@\s\[\]]*)(?::(?P<port>[^/]*))?(?P<path>/.*)?", re.S)
USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}")
LABEL = re.compile(r"[^A-Za-z0-9_.-]")
MOUNT_SECONDS = 60          # sshfs gets this long to connect and mount without a prompt
PROMPT_SECONDS = 300        # and this long when it may ask in a terminal
UNMOUNT_SECONDS = 20
MASTER_PERSIST = 120        # a connection opened in the terminal for a session's supervisor lasts this long unused
STATE_FILE = "mounts.json"  # in the run's folder: what was mounted from where
LOCK_FILE = ".lock"
SOCKET_LIMIT = 100          # the longest control socket path used (sun_path holds 104 on macOS, 108 on Linux)
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_remotefs", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def printable(text):
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(text))


# --- the source ------------------------------------------------------------------------------------------------


def is_remote(value):
    """True if VALUE (a --workspace, --mount source or `workspace add` argument) names a remote folder
    (sshfs://...)."""
    return isinstance(value, str) and value[:16].lower().startswith(PREFIXES)


def split_mount(value):
    """(source, DEST, mode) of a --mount SOURCE:DEST:ro|rw whose SOURCE is remote, or None for any other --mount.
    The remote source keeps its own colons (USER@HOST:PORT): DEST and the mode are the last two fields."""
    if not is_remote(value):
        return None
    source, dest, mode = (value.rsplit(":", 2) + ["", ""])[:3] if value.count(":") >= 3 else (value, "", "")
    if not dest.startswith("/") or mode not in ("ro", "rw"):
        raise ValueError(f"--mount {printable(value)}: a remote mount is sshfs://[USER@]HOST[:PORT]/PATH:DEST:ro|rw")
    return source, dest, mode


# What a source of NFS or SMB may say after "?" (key=value&...): how it is mounted (fuse: a userspace client of
# yours, no root; sudo: the kernel's client through sudo, which asks for your password in a terminal), the protocol
# version, and (SMB) the sandbox-ai secret that holds the password.
QUERY = {"nfs": {"mount": ("fuse", "sudo"), "vers": ("3", "4", "4.0", "4.1", "4.2"), "hard": ("1",)},
         "smb": {"mount": ("sudo",), "vers": ("2.0", "2.1", "3.0", "3.02", "3.1.1"), "secret": None}}
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
DOMAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# The kernel's NFS client mounts soft: a server that is gone makes a call fail after timeo (deciseconds) x retrans
# instead of hanging every process that touches the folder - the launcher's unmount, a supervisor, the app - until it
# is back ("hard", NFS's default; intr is ignored since Linux 2.6.25). ?hard=1 asks for a hard mount (a database on
# the share that must never see an error). SMB's cifs is soft by default; said explicitly all the same.
NFS_SOFT = ("soft", "timeo=150", "retrans=3")
FORMS = {"sshfs": "sshfs://[USER@]HOST[:PORT]/PATH", "nfs": "nfs://SERVER[:PORT]/EXPORT[/PATH][?mount=fuse|sudo&hard=1]",
         "smb": "smb://[[DOMAIN;]USER@]SERVER[:PORT]/SHARE[/PATH][?secret=NAME]"}


def parse(value):
    """The remote source VALUE checked: {"scheme", "user", "domain", "host", "port", "path", "home", "query",
    "source", "label"} - PATH absolute on the remote machine or (sshfs) relative to its HOME (home: True; "" is the
    HOME itself), QUERY the options after "?" (nfs, smb), SOURCE the canonical spelling, LABEL the folder's name for
    /workspaces/NAME. ValueError with what is wrong."""
    if not is_remote(value):
        raise ValueError(f"{printable(value)}: not a remote folder ({', '.join(p + '...' for p in PREFIXES)})")
    if not value.isprintable() or any(c in value for c in ",\n\r\0"):
        raise ValueError(f"{printable(value)}: a remote source cannot hold commas or control characters")
    scheme = value.split(":", 1)[0].lower()
    form = FORMS[scheme]
    query = {}
    if scheme in QUERY and "?" in value:
        value, _, given = value.partition("?")
        for item in given.split("&") if given else []:
            key, separator, setting = item.partition("=")
            allowed = QUERY[scheme].get(key, ())
            if key not in QUERY[scheme] or not separator or key in query or \
                    (allowed is None and not SECRET.fullmatch(setting)) or (allowed and setting not in allowed):
                raise ValueError(f"{printable(value)}: ?{printable(item)} is not one of "
                                 + ", ".join(f"{k}={'|'.join(v) if v else 'NAME'}" for k, v in QUERY[scheme].items()))
            query[key] = setting
    found = SPEC.fullmatch(value)
    if not found:
        raise ValueError(f"{printable(value)}: write {form}")
    user, host, port, path = found["user"], found["host"], found["port"], found["path"] or ""
    domain = None
    if scheme == "smb" and user is not None and ";" in user:
        domain, _, user = user.partition(";")
        if not DOMAIN.fullmatch(domain):
            raise ValueError(f"{printable(value)}: the domain is 1-64 letters, digits, '_', '.', '-'")
    if scheme == "nfs" and user is not None:
        raise ValueError(f"{printable(value)}: an NFS source names no user ({form})")
    if user is not None and not USER.fullmatch(user):
        raise ValueError(f"{printable(value)}: the user is 1-64 letters, digits, '_', '.', '-' (not starting with "
                         "'-' or '.')")
    if host.startswith("["):
        if host.count(":") < 2:
            raise ValueError(f"{printable(value)}: [...] holds an IPv6 address")
    elif not HOST.fullmatch(host or ""):
        raise ValueError(f"{printable(value)}: no host ({form}; a name, an address)")
    number = None
    if port is not None:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError(f"{printable(value)}: the port is 1-65535")
        number = int(port)
    home = scheme == "sshfs" and (path in ("", "/~") or path.startswith("/~/"))
    rest = path[3:] if home and path.startswith("/~/") else "" if home else path
    if rest:
        rest = posixpath.normpath(rest)
        if rest.startswith("//"):
            rest = "/" + rest.lstrip("/")
        if home and (rest == "." or rest.startswith("../") or rest == ".."):
            raise ValueError(f"{printable(value)}: /~/PATH lies in the remote HOME (no '..' out of it)")
        rest = "" if rest == "." else rest
    if scheme in ("nfs", "smb") and (not rest.startswith("/") or rest == "/" or ".." in rest.split("/")):
        raise ValueError(f"{printable(value)}: name the {'export' if scheme == 'nfs' else 'share'} ({form})")
    if len(rest) > 4000:
        raise ValueError(f"{printable(value)}: the path is too long")
    shown_path = ("/~" + ("/" + rest if rest else "")) if home else rest
    who = f"{domain + ';' if domain else ''}{user + '@' if user is not None else ''}"
    source = f"{scheme}://{who}{host}{':' + str(number) if number else ''}{shown_path}" + \
        ("?" + "&".join(f"{key}={query[key]}" for key in sorted(query)) if query else "")
    base = PurePosixPath(rest).name if rest and rest != "/" else ("home" if home else "root")
    label = LABEL.sub("_", base)[:64].lstrip(".-") or "remote"
    return {"scheme": scheme, "user": user, "domain": domain, "host": host, "port": number, "path": rest,
            "home": home, "query": query, "source": source, "label": label}


def hosts_entry(spec, load=None):
    """The `sandbox-ai hosts` entry that SPEC's HOST names (no USER@ and no :PORT given), or None."""
    if spec["user"] is not None or spec["port"] is not None or spec["host"].startswith("["):
        return None
    try:
        entries = (load or (lambda: module("hosts").load()))()
    except (OSError, ValueError):
        return None
    entry = entries.get(spec["host"]) if isinstance(entries, dict) else None
    return entry if isinstance(entry, dict) and isinstance(entry.get("ssh"), str) else None


def connection(spec, load=None):
    """How SPEC is reached: {"target": [USER@]HOST for ssh, "port", "identity", "known_hosts" (sandbox-ai's own
    file, for a host of `sandbox-ai hosts`), "via"}."""
    entry = hosts_entry(spec, load)
    if entry is not None:
        known = module("hosts").known_hosts_file()
        return {"target": entry["ssh"], "port": entry.get("port", 22), "identity": entry.get("identity"),
                "known_hosts": str(known), "via": f"the host {spec['host']} of sandbox-ai hosts"}
    user = f"{spec['user']}@" if spec["user"] is not None else ""
    host = spec["host"][1:-1] if spec["host"].startswith("[") else spec["host"]
    # sshfs reads USER@[ADDRESS]:PATH (an IPv6 address in brackets), ssh USER@ADDRESS.
    return {"target": f"{user}{host}", "sshfs_target": f"{user}{spec['host']}", "port": spec["port"],
            "identity": None, "known_hosts": None, "via": "ssh"}


def remote_path(spec):
    """The path as sshfs takes it after HOST: - absolute, or relative to the remote HOME ("" is the HOME)."""
    return spec["path"]


# --- the tools ---------------------------------------------------------------------------------------------------


def install_hint(scheme="sshfs", platform=None, how=None):
    """How to install what SCHEME needs on this host (HOW: fuse-nfs or sudo for nfs and smb), in words."""
    platform = platform or sys.platform
    if scheme == "nfs" and how == "fuse-nfs":
        return ("install fuse-nfs, a userspace NFS client (github.com/sahlberg/fuse-nfs, with libnfs), or mount through "
                "the kernel's client with sudo (?mount=sudo; Debian/Ubuntu: sudo apt install nfs-common; Fedora: sudo "
                "dnf install nfs-utils; Arch: sudo pacman -S nfs-utils)")
    if scheme == "nfs":
        return ("install the kernel's NFS tools (Debian/Ubuntu: sudo apt install nfs-common; Fedora: sudo dnf install "
                "nfs-utils; Arch: sudo pacman -S nfs-utils) and sudo, or fuse-nfs (?mount=fuse) to mount without root")
    if scheme == "smb":
        return ("install the kernel's SMB tools (Debian/Ubuntu: sudo apt install cifs-utils; Fedora: sudo dnf install "
                "cifs-utils; Arch: sudo pacman -S cifs-utils) and sudo")
    if platform == "darwin":
        return ("install macFUSE (brew install --cask macfuse; allow its system extension) and sshfs (brew install "
                "gromgit/fuse/sshfs-mac), or FUSE-T with its sshfs (brew tap macos-fuse-t/cask && brew install "
                "fuse-t fuse-t-sshfs)")
    return ("install sshfs (it brings FUSE 3's fusermount3): Debian/Ubuntu: sudo apt install sshfs; Fedora: sudo "
            "dnf install fuse-sshfs; Arch: sudo pacman -S sshfs; openSUSE: sudo zypper install sshfs; Alpine: sudo "
            "apk add sshfs")


SBIN = ("/usr/sbin", "/sbin", "/usr/local/sbin")  # mount.nfs and mount.cifs live there (often not on PATH)


def method(spec, which=shutil.which):
    """How SPEC is mounted: "sshfs"; for NFS "fuse-nfs" (a userspace client of yours: without root; the default when
    it is installed, or ?mount=fuse) or "sudo" (the kernel's client through sudo: ?mount=sudo, or without fuse-nfs);
    for SMB "sudo" (the kernel's client: there is no userspace SMB client sandbox-ai can use)."""
    if spec["scheme"] == "sshfs":
        return "sshfs"
    wanted = spec["query"].get("mount")
    if spec["scheme"] == "nfs" and wanted != "sudo" and (wanted == "fuse" or which("fuse-nfs")):
        return "fuse-nfs"
    return "sudo"


def sshfs_fixes(missing=None, needed=True):
    """The fix (fixes.py) for sshfs that is not installed: the distribution's package command (shown, never run: it
    needs root), or on a Mac what brings macFUSE and sshfs."""
    item = module("fixes").tool_fix(["sshfs"], missing=missing or
                                    "sshfs is not installed on this host (remote folders: sshfs://)")
    if not item["commands"] and sys.platform == "darwin":
        item["why"] = install_hint("sshfs", "darwin")
    item["needed"] = needed
    return [item]


def tool(scheme="sshfs", which=shutil.which, how=None):
    """The path of SCHEME's mount program (HOW: method's choice), or ValueError with how to install it."""
    if how in (None, "sshfs"):
        found = which("sshfs")
        if not found:
            error = ValueError(f"{scheme}:// needs sshfs on this host, which is not installed: {install_hint(scheme)}")
            error.fixes = sshfs_fixes()  # (fixes.py: the distribution's package command, shown - it needs root)
            raise error
        return found
    if how == "fuse-nfs":
        found = which("fuse-nfs")
        if not found:
            raise ValueError(f"{scheme}://...?mount=fuse needs fuse-nfs on this host, which is not installed: "
                             f"{install_hint(scheme, how=how)}")
        return found
    helper = "mount.nfs" if scheme == "nfs" else "mount.cifs"
    if not which("sudo") or not (which(helper) or any(os.path.exists(f"{d}/{helper}") for d in SBIN)):
        raise ValueError(f"{scheme}:// needs sudo and {helper} on this host: {install_hint(scheme)}")
    return which("sudo")


def unmount_command(point, lazy=False, which=shutil.which, platform=None):
    """The command that unmounts the FUSE mount POINT (Linux: fusermount3 -u, else fusermount -u; macOS: umount)."""
    if (platform or sys.platform) == "darwin":
        return ["umount", *(["-f"] if lazy else []), str(point)]
    program = which("fusermount3") or which("fusermount") or "fusermount3"
    return [program, "-u", *(["-z"] if lazy else []), str(point)]


def other_clients(which=shutil.which):
    """What mounts NFS and SMB here, in words: "NFS: fuse-nfs, sudo + mount.nfs; SMB: not available" ..."""
    def kernel(helper):
        return bool(which("sudo")) and bool(which(helper) or any(os.path.exists(f"{d}/{helper}") for d in SBIN))
    nfs = [name for name, ok in (("fuse-nfs", which("fuse-nfs")), ("sudo + mount.nfs", kernel("mount.nfs"))) if ok]
    smb = ["sudo + mount.cifs"] if kernel("mount.cifs") else []
    return f"NFS: {', '.join(nfs) or 'not available'}; SMB: {', '.join(smb) or 'not available'}"


def doctor_check(which=shutil.which, platform=None):
    """(status, title, result, hint) for `sandbox-ai doctor`: sshfs for remote folders - optional, so "skip" with
    how to install it when missing - and what mounts NFS and SMB here (other_clients)."""
    title = "Remote folders (sshfs)"
    found = which("sshfs")
    others = other_clients(which) if (platform or sys.platform) != "darwin" else "NFS, SMB: not on macOS yet"
    if not found:
        return "skip", title, f"sshfs is not installed: sshfs:// workspaces and mounts cannot be mounted ({others})", \
            install_hint("sshfs", platform)
    if (platform or sys.platform) != "darwin" and not (which("fusermount3") or which("fusermount")):
        return "warn", title, f"{found}, but no fusermount3 (sandbox-ai unmounts with it); {others}", \
            install_hint("sshfs", platform)
    if (platform or sys.platform) != "darwin" and not os.path.exists("/dev/fuse"):
        return "warn", title, f"{found}, but /dev/fuse is missing; {others}", "load the fuse module: sudo modprobe fuse"
    return "ok", title, f"{found}; {others}", ""


# --- where: sandbox-ai's own folders -------------------------------------------------------------------------------


def root():
    """XDG_STATE_HOME/sandbox-ai/remote: the run folders with the mount points (never another place)."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "remote"


def private_dir(path):
    """PATH made (0700) if missing; a folder of this user's, not a link, 0700 - else ValueError."""
    path = Path(path)
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{printable(path)}: not a folder of yours (sandbox-ai's remote folders must be)")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def run_dir(run, create=True):
    if not isinstance(run, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run):
        raise ValueError(f"{printable(run)}: not a run's name")
    if not create:
        return root() / run
    top = root()
    top.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_dir(top)
    return private_dir(top / run)


@contextlib.contextmanager
def locked(run):
    fd = os.open(run_dir(run) / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(run):
    try:
        data = json.loads((run_dir(run, create=False) / STATE_FILE).read_text())
    except (OSError, ValueError, RecursionError):
        return []
    entries = data.get("mounts") if isinstance(data, dict) else None
    return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("point"), str)] \
        if isinstance(entries, list) else []


def save(run, entries):
    path = run_dir(run) / STATE_FILE
    temporary = path.with_name(f".{STATE_FILE}.{os.getpid()}")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as stream:
        json.dump({"version": 1, "pid": os.getpid(), "pid_start": process_start(os.getpid()),
                   "mounts": entries}, stream)
    os.replace(temporary, path)


def process_start(pid, proc=Path("/proc")):
    """The start time of PID (clock ticks since boot), or None."""
    try:
        return int((proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def plan(run, value, mode, by="start", sandbox=None):
    """A mount point for the remote source VALUE in RUN's folder (made, empty, 0700; nothing mounted yet), recorded:
    the entry {"source", "point", "mode", "state": "planned", "by", "sandbox"}."""
    spec = parse(value)
    with locked(run):
        entries = load(run)
        taken = {int(name) for name in os.listdir(run_dir(run)) if name.isdigit()}
        index = max(taken | {int(e.get("index", 0)) for e in entries}, default=0) + 1
        folder = private_dir(run_dir(run) / str(index))
        point = private_dir(folder / spec["label"]).resolve()
        entry = {"index": index, "source": spec["source"], "point": str(point), "mode": mode, "state": "planned",
                 "by": by, "sandbox": sandbox, "since": time.time()}
        save(run, entries + [entry])
    return entry


def update(run, point, **fields):
    with locked(run):
        entries = load(run)
        for entry in entries:
            if entry.get("point") == str(point):
                entry.update(fields)
        save(run, entries)


def mounts(run):
    """The entries of RUN's remote mounts (what was planned and mounted, from where)."""
    return load(run)


def source_of(run, point):
    """The remote source mounted at POINT for RUN, or None."""
    return next((entry.get("source") for entry in load(run) if entry.get("point") == str(point)), None)


# --- mount and unmount ---------------------------------------------------------------------------------------------


def temp_root(base=None, make=True):
    """BASE/sandbox-ai-UID (BASE: $TMPDIR, else /tmp) when it is this user's folder (made 0700 when MAKE; not a link,
    nobody else's, no bit for others) - else, when another user made it first or it is a link, the user's own
    $XDG_CACHE_HOME/sandbox-ai/run (~/.cache: nobody else can make that; made 0700 when MAKE): never a folder another
    user owns. (hosts.temp_root, secret_store.temp_root and remotefs.temp_root are the same.)"""
    temporary = Path(base or os.environ.get("TMPDIR") or "/tmp") / f"sandbox-ai-{os.getuid()}"
    try:
        if make:
            with contextlib.suppress(FileExistsError):
                os.mkdir(temporary, 0o700)
        info = os.lstat(temporary)
    except FileNotFoundError:
        return temporary  # (not made yet: a question makes nothing)
    except OSError:
        info = None
    if info is not None and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
        return temporary
    cache = os.environ.get("XDG_CACHE_HOME") or ""
    fallback = Path(cache if os.path.isabs(cache) else Path.home() / ".cache") / "sandbox-ai" / "run"
    if make:
        with contextlib.suppress(OSError):
            fallback.mkdir(mode=0o700, parents=True, exist_ok=True)
    return fallback


def control_path(conn):
    """The control socket of a shared connection for CONN (sandbox-ai's own runtime folder), or None (no private
    folder, a path too long for a socket)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "sandbox-ai" if runtime and os.path.isdir(runtime) else temp_root()
    key = hashlib.sha256(json.dumps([conn.get(k) for k in ("target", "port", "identity", "known_hosts")])
                         .encode()).hexdigest()[:16]
    path = base / "sshfs" / key
    if len(os.fsencode(path)) > SOCKET_LIMIT or re.search(r"[\s\"'%,]", str(path)):
        return None
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_dir(base)
        private_dir(base / "sshfs")
    except (OSError, ValueError):
        return None
    return path


def ssh_options(conn, interactive):
    """[-o OPTION=VALUE ...] for ssh (through sshfs or for the shared connection): the host key always checked."""
    options = ["ConnectTimeout=20", "ServerAliveInterval=15", "ServerAliveCountMax=3"]
    if conn.get("known_hosts"):
        # A host of `sandbox-ai hosts`: as hosts.py reaches it (its first key learnt, then strictly checked).
        options += ["StrictHostKeyChecking=accept-new",
                    f'UserKnownHostsFile="{conn["known_hosts"]}" "~/.ssh/known_hosts"']
    else:
        # Never "no" (whatever ~/.ssh/config says): unknown keys are asked about in a terminal, refused without one.
        options.append("StrictHostKeyChecking=ask" if interactive else "StrictHostKeyChecking=yes")
    if not interactive:
        options.append("BatchMode=yes")
    if conn.get("identity"):
        options += [f"IdentityFile={os.path.expanduser(conn['identity'])}", "IdentitiesOnly=yes"]
    return options


def sshfs_command(spec, conn, point, read_only, interactive=False, control=None, program="sshfs"):
    """The sshfs command line that mounts SPEC at POINT: files owned by you (idmap=user), reconnecting, read-only
    when READ_ONLY; with CONTROL (an existing shared connection) through it."""
    for value in [conn["target"], str(point), conn.get("known_hosts") or "", conn.get("identity") or ""]:
        if "," in value or '"' in value or not value.isprintable():
            raise ValueError(f"{printable(value)}: cannot be given to sshfs (a comma, a double quote or a control "
                             "character)")
    cmd = [program, f"{conn.get('sshfs_target') or conn['target']}:{remote_path(spec)}", str(point), "-o", "idmap=user", "-o", "reconnect"]
    if read_only:
        cmd += ["-o", "ro"]
    if conn.get("port"):
        cmd += ["-p", str(conn["port"])]
    for option in ssh_options(conn, interactive):
        cmd += ["-o", option]
    if control is not None:
        cmd += ["-o", f"ControlPath={control}", "-o", "ControlMaster=no"]
    return cmd


def failure_hint(text, interactive):
    """What to do about sshfs's TEXT (its error)."""
    lower = text.lower()
    if "host key verification failed" in lower or "no matching host key" in lower or "remote host identification" \
            in lower:
        return ("the host key is unknown or has changed: connect once with ssh in a terminal and check the key "
                "(sandbox-ai never turns host key checking off)")
    if "permission denied" in lower or "too many authentication failures" in lower:
        return ("the login failed" + ("" if interactive else " without a prompt: load your key into ssh-agent "
                                      "(ssh-add), or start in a terminal (a password or passphrase is asked there)"))
    if "fuse: device not found" in lower or "/dev/fuse" in lower:
        return "FUSE is missing on this host: sudo modprobe fuse (and the fuse3 package)"
    if "fusermount" in lower and ("not found" in lower or "no such file" in lower):
        return install_hint()
    if "no such file" in lower:
        return "the remote folder does not exist"
    return "check that ssh to that host works in a terminal (ssh -p PORT USER@HOST)"


def mount(entry, interactive=False, runner=subprocess.run, which=shutil.which, load_hosts=None, secret=None,
          starter=None, check=None):
    """Mount ENTRY (plan) with sshfs, fuse-nfs or the kernel's NFS or SMB client through sudo (method); its mount
    point. ENTRY["via"] says how (unmount goes the same way). ValueError with the tool's words and what to do."""
    spec = parse(entry["source"])
    how = method(spec, which)
    entry["via"] = how
    if how != "sshfs":
        return mount_other(entry, spec, how, interactive, runner, which, secret, starter, check)
    program = tool(spec["scheme"], which)
    conn = connection(spec, load_hosts)
    control = control_path(conn)
    shared = control if control is not None and control.is_socket() else None
    point = Path(entry["point"])
    cmd = sshfs_command(spec, conn, point, entry.get("mode") == "ro", interactive, shared, program)
    try:
        done = runner(cmd, stdin=None if interactive else subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True, timeout=PROMPT_SECONDS if interactive else MOUNT_SECONDS)
    except subprocess.TimeoutExpired:
        unmount(point, runner=runner, which=which)
        raise ValueError(f"could not mount {printable(entry['source'])}: sshfs did not finish within "
                         f"{PROMPT_SECONDS if interactive else MOUNT_SECONDS} s ({failure_hint('', interactive)})") \
            from None
    except OSError as exc:
        raise ValueError(f"could not mount {printable(entry['source'])}: {printable(exc)}") from None
    if done.returncode != 0:
        said = " ".join((done.stderr or done.stdout or "").split())[-400:]
        raise ValueError(f"could not mount {printable(entry['source'])} (sshfs: {printable(said) or 'exit status ' + str(done.returncode)}): "
                         f"{failure_hint(said, interactive)}")
    return point


def sudo_words(interactive):
    """sudo as the mount and unmount of the kernel's clients run it: asking for your password in a terminal, never
    without one (-n: it fails instead, with what to do)."""
    return ["sudo"] if interactive else ["sudo", "-n"]


def kernel_command(spec, point, read_only, interactive, credentials=None):
    """The sudo mount command line for the kernel's NFS or SMB client: nosuid, nodev, read-only when READ_ONLY, soft
    (NFS_SOFT; NFS's ?hard=1: hard); for
    SMB the files owned by you (uid, gid) and the login from CREDENTIALS (a file only you can read, removed right
    after the mount), else as guest."""
    options = ["nosuid", "nodev", *(["ro"] if read_only else [])]
    if spec["scheme"] == "nfs":
        options += ["hard"] if spec["query"].get("hard") == "1" else list(NFS_SOFT)
    else:
        options.append("soft")
    if spec["port"]:
        options.append(f"port={spec['port']}")
    if spec["query"].get("vers"):
        options.append(f"vers={spec['query']['vers']}")
    host = spec["host"]
    if spec["scheme"] == "nfs":
        return [*sudo_words(interactive), "mount", "-t", "nfs", "-o", ",".join(options), f"{host}:{spec['path']}",
                str(point)]
    options += [f"uid={os.getuid()}", f"gid={os.getgid()}"]
    if credentials:
        options.append(f"credentials={credentials}")
    elif spec["user"]:
        options += [f"username={spec['user']}", *([f"domain={spec['domain']}"] if spec["domain"] else [])]
    else:
        options.append("guest")
    if any("," in option for option in options):
        raise ValueError(f"{printable(spec['source'])}: a mount option cannot hold a comma")
    return [*sudo_words(interactive), "mount", "-t", "cifs", f"//{host}{spec['path']}", str(point), "-o",
            ",".join(options)]


def smb_credentials(run_folder, spec, secret=None):
    """A credentials file for mount.cifs in RUN_FOLDER (0600, never the sandbox's) with SPEC's user and the
    password of the sandbox-ai secret its ?secret=NAME names (SECRET(name), default secret_store.get), or None
    without one. ValueError when the secret is not there."""
    name = spec["query"].get("secret")
    if not name:
        return None
    value = (secret or (lambda key: module("secret_store").get(key)))(name)
    if value is None:
        raise ValueError(f"{printable(spec['source'])}: the secret {name} is not set (sandbox-ai secrets set {name})")
    if any(c in str(value) for c in "\n\r\0"):
        raise ValueError(f"the secret {name}: a password of one line")
    path = Path(run_folder) / f".credentials-{os.getpid()}-{os.urandom(4).hex()}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"username={spec['user'] or 'guest'}\npassword={value}\n"
                     + (f"domain={spec['domain']}\n" if spec["domain"] else ""))
    return path


def mount_other(entry, spec, how, interactive, runner, which, secret=None, starter=None, check=None):
    """mount() for NFS and SMB: fuse-nfs (started, then waited for until the folder is mounted) or the kernel's
    client through sudo."""
    point = Path(entry["point"])
    tool(spec["scheme"], which, how)
    where = printable(entry["source"])
    if how == "fuse-nfs":
        url = f"nfs://{spec['host']}{':' + str(spec['port']) if spec['port'] else ''}{spec['path']}"
        log = point.parent / ".fuse-nfs.log"
        with open(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600),
                  "w") as stream:
            process = (starter or subprocess.Popen)([which("fuse-nfs") or "fuse-nfs", "-n", url, "-m", str(point)],
                                                    stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                                    start_new_session=True)
        deadline = time.monotonic() + MOUNT_SECONDS
        while time.monotonic() < deadline:
            if (check or mounted)(point):
                with contextlib.suppress(OSError):
                    log.unlink()
                return point
            if process.poll() is not None and process.returncode != 0:
                break
            time.sleep(0.1)
        said = ""
        with contextlib.suppress(OSError):
            said = " ".join(log.read_text()[-400:].split())
            log.unlink()
        if process.poll() is None:
            process.kill()
        raise ValueError(f"could not mount {where} (fuse-nfs: {printable(said) or 'no mount within '}"
                         f"{'' if said else str(MOUNT_SECONDS) + ' s'}): check that the export allows this host, or "
                         "mount through the kernel's client (?mount=sudo)")
    credentials = smb_credentials(point.parent.parent, spec, secret) if spec["scheme"] == "smb" else None
    try:
        cmd = kernel_command(spec, point, entry.get("mode") == "ro", interactive, credentials)
        try:
            done = runner(cmd, stdin=None if interactive else subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=PROMPT_SECONDS if interactive else MOUNT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ValueError(f"could not mount {where}: sudo mount did not finish in time") from None
        except OSError as exc:
            raise ValueError(f"could not mount {where}: {printable(exc)}") from None
    finally:
        if credentials is not None:
            with contextlib.suppress(OSError):
                os.unlink(credentials)
    if done.returncode != 0:
        said = " ".join((done.stderr or done.stdout or "").split())[-400:]
        hint = ("sudo needs your password: start in a terminal (it asks there), or let your user run mount and "
                "umount without one (sudoers)" if "password is required" in said or "a terminal is required" in said
                else "check the server, the share or export and the login")
        raise ValueError(f"could not mount {where} (sudo mount: {printable(said) or 'exit status ' + str(done.returncode)}"
                         f"): {hint}")
    return point


def open_shared(value, runner=subprocess.run, which=shutil.which, load_hosts=None):
    """Open the connection to VALUE's host in this terminal (a password or passphrase is asked here), kept
    MASTER_PERSIST seconds for a session's supervisor, whose sshfs then uses it without asking. True if open (or
    one is open already), False if it could not be (the supervisor then tries without)."""
    spec = parse(value)
    if spec["scheme"] != "sshfs":
        return False  # (NFS, SMB: sudo asks in the terminal of a foreground start; a session's needs none)
    conn = connection(spec, load_hosts)
    control = control_path(conn)
    if control is None or not which("ssh"):
        return False
    if control.is_socket():
        return True
    cmd = ["ssh", "-f", "-N", "-o", "ControlMaster=yes", "-o", f"ControlPersist={MASTER_PERSIST}",
           "-o", f"ControlPath={control}", *(["-p", str(conn["port"])] if conn.get("port") else [])]
    for option in ssh_options(conn, True):
        cmd += ["-o", option]
    try:
        done = runner([*cmd, "--", conn["target"]], timeout=PROMPT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def mounted(point, mountinfo=Path("/proc/self/mountinfo")):
    """True if POINT is a mount point now (Linux: /proc/self/mountinfo; elsewhere os.path.ismount)."""
    try:
        text = Path(mountinfo).read_text()
    except OSError:
        return os.path.ismount(point)
    wanted = str(point)
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 4 and re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]) == wanted:
            return True
    return False


def unmount(point, runner=subprocess.run, which=shutil.which, platform=None, check=mounted, via=None,
            interactive=False):
    """Unmount the mount point POINT (lazily when it is busy; VIA "sudo": the kernel's mount, through sudo) and
    remove it and its numbered folder when they are empty - never anything that is not (a remote folder still
    mounted is never deleted). (True, None) when it is gone, (False, why) when not."""
    point = Path(point)
    why = None
    for lazy in (False, True):
        command = [*sudo_words(interactive), "umount", *(["-l"] if lazy else []), str(point)] if via == "sudo" \
            else unmount_command(point, lazy, which, platform)
        try:
            done = runner(command, stdin=None if interactive and via == "sudo" else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=UNMOUNT_SECONDS)
            said = " ".join((done.stderr or "").split())
            if done.returncode == 0 or not check(point):
                why = None
                break
            why = said or f"exit status {done.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            why = str(exc)
            if not check(point):
                why = None
                break
    if why is not None:
        return False, why
    try:
        os.rmdir(point)  # never more: what is in it after the unmount stays (never deleted recursively)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return False, f"{printable(point)} is not empty after the unmount ({exc.strerror}): left as it is"
    with contextlib.suppress(OSError):
        os.rmdir(point.parent)  # its numbered folder, when empty
    return True, None


def release(run, runner=subprocess.run, which=shutil.which, platform=None, check=mounted, only=None,
            interactive=False):
    """Unmount the remote folders of RUN (ONLY: those mount points) and remove its folder when nothing is left:
    [(source, point, None or why it stayed)]. INTERACTIVE: sudo may ask in this terminal (a kernel's mount)."""
    folder = run_dir(run, create=False)
    if not folder.is_dir() or folder.is_symlink():
        return []
    folder = folder.resolve()
    result = []
    with locked(run):
        entries = load(run)
        known = {entry["point"]: entry.get("source") for entry in entries}
        via = {entry["point"]: entry.get("via") for entry in entries}
        # The mount points as their folders list them (scandir's entry types): never a look into one (stat), which
        # waits as long as a remote folder's host stays away - the unmount below is what ends that.
        points = [label.path for number in listed(folder) if number.name.isdigit() and
                  number.is_dir(follow_symlinks=False) for label in listed(number.path)
                  if label.is_dir(follow_symlinks=False)]
        present = set(points)
        for point in dict.fromkeys([*known, *points]):
            if only is not None and point not in only:
                continue
            if not Path(point).is_relative_to(folder):
                continue  # (a damaged record: never outside the run's folder)
            ok, why = unmount(point, runner, which, platform, check, via.get(point), interactive) \
                if point in present or check(point) else (True, None)
            result.append((known.get(point), point, why))
            for entry in entries:
                if entry.get("point") == point:
                    entry["state"] = "unmounted" if ok else "left"
        entries = [entry for entry in entries if entry.get("state") != "unmounted"]
        if entries:
            save(run, entries)
        else:
            with contextlib.suppress(OSError):
                (folder / STATE_FILE).unlink()
    if not entries:
        with contextlib.suppress(OSError):
            (folder / LOCK_FILE).unlink()
            folder.rmdir()
    return result


def listed(folder):
    """The entries of FOLDER (os.DirEntry, by name), or [] when it cannot be read."""
    try:
        with os.scandir(folder) as listing:
            return sorted(listing, key=lambda entry: entry.name)
    except OSError:
        return []


def forget(run, points):
    """The planned mount points POINTS of RUN that were never mounted: removed (rmdir only, never what holds
    anything) with their records; RUN's folder too when nothing is left in it. [point] removed."""
    folder = run_dir(run, create=False)
    if not folder.is_dir() or folder.is_symlink():
        return []
    folder = folder.resolve()
    removed = []
    with locked(run):
        entries = load(run)
        for entry in entries:
            point = entry.get("point")
            if point in points and entry.get("state") == "planned" and Path(point).is_relative_to(folder):
                with contextlib.suppress(OSError):
                    os.rmdir(point)
                    removed.append(point)
                with contextlib.suppress(OSError):
                    os.rmdir(Path(point).parent)
        entries = [entry for entry in entries if entry.get("point") not in removed]
        if entries:
            save(run, entries)
        else:
            with contextlib.suppress(OSError):
                (folder / STATE_FILE).unlink()
    if not entries:
        with contextlib.suppress(OSError):
            (folder / LOCK_FILE).unlink()
            folder.rmdir()
    return removed


def owner_alive(run):
    """True if the launcher that recorded RUN's mounts last still runs (its PID with the same start time)."""
    try:
        data = json.loads((run_dir(run, create=False) / STATE_FILE).read_text())
        pid, start = int(data["pid"]), data.get("pid_start")
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return False
    return start is None or process_start(pid) == start


def stale(runs, runner=subprocess.run, which=shutil.which, platform=None, check=mounted, dry_run=False):
    """The remote folders of runs that ended (no run directory of that name in RUNS - the folder, or folders, where
    sandbox-ai's runs register - and the launcher that mounted them gone), unmounted (DRY_RUN: only named):
    [(run, source, point, why)]."""
    top = root()
    if not top.is_dir():
        return []
    registered = [Path(runs)] if isinstance(runs, (str, Path)) else [Path(path) for path in runs]
    found = []
    for folder in sorted(top.iterdir()):
        run = folder.name
        if folder.is_symlink() or not folder.is_dir() or any((path / run).exists() for path in registered) \
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run) or owner_alive(run):
            continue
        if dry_run:
            found += [(run, entry.get("source"), entry.get("point"), None) for entry in load(run)] or \
                [(run, None, str(folder), None)]
            continue
        found += [(run, source, point, why) for source, point, why in release(run, runner, which, platform, check)]
    return found


TOOLS = {"sshfs": "sshfs", "fuse-nfs": "fuse-nfs", "sudo": "the kernel's client through sudo"}
TRAFFIC = {"sshfs": "SSH", "nfs": "NFS", "smb": "SMB"}


def warning(entry, sandbox):
    """The warning a start says for a remote folder ENTRY given at SANDBOX (a path in the sandbox)."""
    spec = parse(entry["source"])
    where = connection(spec)["target"] if spec["scheme"] == "sshfs" else spec["host"]
    if entry.get("mode") == "ro":
        risk = "the agent reads the files of that machine (read-only)"
    else:
        risk = (f"the agent changes and deletes files on {printable(where)} itself (read-write); read-only (--ro, a "
                "--mount with :ro, --workspace-ro) is the safer choice")
    tool_name = TOOLS.get(entry.get("via") or method(spec), "sshfs")
    secret = "the SSH key" if spec["scheme"] == "sshfs" else "the password"
    return (f"{sandbox} is the remote folder {printable(entry['source'])}, mounted on this host with {tool_name} (at "
            f"{printable(entry['point'])}): {risk}; its traffic goes from this host over {TRAFFIC[spec['scheme']]}, "
            f"not through the sandbox's proxy, and the agent never gets {secret} or the connection")
