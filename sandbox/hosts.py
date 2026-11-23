#!/usr/bin/env python3
"""sandbox-ai hosts: other machines (or Proxmox LXC containers) whose sandbox-ai this one manages over SSH.

  sandbox-ai hosts list [--json] [--cached|--refresh]  the hosts, "local" first, each with its sandbox-ai version
                                                       and whether an update is available (asked over SSH, kept
                                                       10 minutes; --cached asks no host)
  sandbox-ai hosts add NAME USER@HOST [--port N] [--identity FILE] [--description TEXT] [--command PATH]
                       [--update-sudo]
  sandbox-ai hosts set NAME --update-sudo on|off       whether its update needs sudo (a system installation)
  sandbox-ai hosts remove NAME
  sandbox-ai hosts test NAME [--json]                  SSH, the host's sandbox-ai version, its doctor
  sandbox-ai hosts update NAME...|--all [--version X.Y.Z] [--sudo] [--jobs N] [--installation user|system|all]
                       [--json]                        the host's own `sandbox-ai update` (signed, as install.sh
                                                       installed it): old -> new version per host, a summary
  sandbox-ai hosts update NAME...|--all --mode off|notify|download|auto [--at HH:MM[-HH:MM]] [--json]
                                                       the host's own update mode (sandbox-ai update mode there,
                                                       its timer as the SSH user); hosts list shows each one's
  sandbox-ai hosts install NAME [--user|--system [--prefix DIR]] [--sudo|--no-sudo] [--from-here|--download
                       [--version X.Y.Z]] [--json]     install sandbox-ai there: this machine's copy (default) or
                                                       the signed release the host downloads; for the SSH user, or
                                                       for every user as root (SSH as root, or with sudo)
  sandbox-ai hosts uninstall NAME [--installation user|system] [--purge [--secrets] [--mcp-data]] [--stop-running]
                       [--sudo|--no-sudo] [--remove-from-list] [--dry-run] [--yes] [--json]
                                                       remove sandbox-ai there (its own sandbox-ai uninstall, after
                                                       its table of what goes and a question); the host stays in
                                                       the list unless --remove-from-list
  sandbox-ai hosts run NAME -- ARGS...                 `sandbox-ai ARGS` on that host (e.g. -- list --json)

The desktop app and the web interface (sandbox-ai web) use the same list (~/.config/sandbox-ai/hosts.json) and
reach each host the same way: SSH with your keys, never a password prompt (BatchMode), the host key learnt at the
first contact and checked strictly from then on, each argument quoted on its own. Nothing listens on the network
on the hosts: they only need sshd and sandbox-ai. A non-interactive SSH login reads no ~/.bashrc on Debian, so the
remote command runs ~/.local/bin/sandbox-ai (the SSH user's own installation, install.sh's place) when there is
one - the one a terminal there finds first, through the PATH block in ~/.bashrc - else the sandbox-ai on its PATH.

A host may have several installations: the user's own and a system one (PREFIX/lib/sandbox-ai, its commands in
PREFIX/bin), a pip or pipx one, a package. hosts list names each (--json: "installations", with the one a terminal
there runs and the one the app's commands run; "differ" when those are not the same one); update and uninstall take
--installation user|system for another one than the one the commands run.

A key file that others can read is refused by ssh ("UNPROTECTED PRIVATE KEY FILE"): add and test make your own
key files (the host's --identity, else ~/.ssh/id_*; regular files you own, never through a symbolic link)
private (0600), say so, and try the connection again; a key folder that others can write to is named, not
changed. An --identity lies in your HOME (a regular file, not a link) and is changed only if it holds a private
key. Writers of hosts.json (add, set, remove, the web interface) hold hosts_file_lock (hosts.json.lock beside it).

An update runs the host's own `sandbox-ai update`: install.sh's signed update of the installation there (a user
one in ~/.local/share/sandbox-ai, or a system one in PREFIX/lib/sandbox-ai, which needs root: --sudo, or the
host's update_sudo, runs it as `sudo -n`, which never asks for a password; when sudo wants one, a terminal asks:
this one, or a terminal tab of the app or the web interface). A checkout, a pip installation or a package is not
updated over SSH: it is named, with the way to update it. --all updates every host of the list (not this
machine: sandbox-ai update).

Standard library only.
"""
import argparse
import concurrent.futures
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import importlib.util
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()
LOCAL = "local"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
TARGET = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}@)?(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])")
COMMAND = re.compile(r"[A-Za-z0-9_./~-]{1,200}")
CONNECT_TIMEOUT = 10
LIMIT = 1 << 20
KEYS = {"ssh", "port", "identity", "description", "command", "update_sudo"}
# The keys ssh tries without -i (ssh_config's IdentityFile defaults).
DEFAULT_KEYS = ("id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk", "id_dsa")
# What ssh says about a key file others can read (it then ignores the key).
TOO_OPEN = re.compile(r"Permissions 0?[0-7]{3,4} for '([^'\n]{1,4096})' are too open"
                      r"|Load key \"([^\"\n]{1,4096})\": bad permissions")
HOMES = ("/home/", "/root/", "/Users/", "/var/home/")  # a lib/sandbox-ai below them is a user's own
STATUS_TTL = 600  # seconds a host's version and update check are kept (hosts list)
UPDATE_TIMEOUT = 900
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.+-]+)?")
# What sudo -n says when it would need a password (or a terminal to ask for one), and when it is not allowed.
SUDO_PASSWORD = ("a password is required", "a terminal is required", "no tty present", "askpass")
SUDO_REFUSED = ("not in the sudoers", "is not allowed to run sudo", "may not run sudo")
# The README's command: the release's install.sh, which carries the release keys (the file on main carries none)
INSTALL_HINT = ("install it there with install.sh (see the README: curl -fsSL https://github.com/c0decave/sandbox-ai/"
                "releases/latest/download/install.sh | bash); it updates signed from then on")


def config_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "hosts.json"


def known_hosts_file():
    """sandbox-ai's own known_hosts: the keys of the hosts it manages, apart from yours (~/.ssh/known_hosts is
    read too, for keys you accepted already)."""
    return config_file().with_name("known_hosts")


def printable(text):
    """TEXT safe for a terminal: the launcher's printable, control, bidi and other non-printable characters escaped (a
    test keeps both the same; the launcher took a third of `hosts list` to load for it)."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def check(name, entry):
    """ENTRY (a host of the file) checked: {"ssh", "port", "identity", "description", "command"}."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or name == LOCAL:
        raise ValueError(f"{printable(name)!r}: a host's name is 1-32 of a-z, 0-9, '_', '-' (not {LOCAL})")
    if not isinstance(entry, dict) or set(entry) - KEYS:
        raise ValueError(f"host {name}: allowed keys are {', '.join(sorted(KEYS))}")
    target = entry.get("ssh")
    if not isinstance(target, str) or not TARGET.fullmatch(target) or target.startswith("-"):
        raise ValueError(f"host {name}: ssh is USER@HOST or HOST: {printable(target)!r}")
    port = entry.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"host {name}: port is 1-65535")
    identity = entry.get("identity")
    if identity is not None:
        check_identity(name, identity)
    description = entry.get("description", "")
    if not isinstance(description, str) or len(description) > 200 or not description.isprintable():
        raise ValueError(f"host {name}: description is one line of at most 200 characters")
    command = entry.get("command")
    if command is not None and (not isinstance(command, str) or not COMMAND.fullmatch(command)
                                or command.startswith("-") or "~" in command.removeprefix("~/")):
        raise ValueError(f"host {name}: command is the path of sandbox-ai there (absolute, or ~/..., not starting "
                         "with '-')")
    update_sudo = entry.get("update_sudo", False)
    if not isinstance(update_sudo, bool):
        raise ValueError(f"host {name}: update_sudo is true or false")
    return {"ssh": target, "port": port, **({"identity": identity} if identity else {}),
            "description": description, **({"command": command} if command else {}),
            **({"update_sudo": True} if update_sudo else {})}


def check_identity(name, identity):
    """Refuse host NAME's IDENTITY (its key file, for ssh -i) unless it is an absolute path (or ~/...) inside your
    HOME, also with its links resolved, and, if it exists, a regular file (not a symbolic link, a folder or a
    device): add and test make the key file private (0600), which must never reach a file elsewhere."""
    if not isinstance(identity, str) or any(c in identity for c in "\n\r\0") or not identity \
            or not os.path.isabs(os.path.expanduser(identity)):
        raise ValueError(f"host {name}: identity is an absolute path (or ~/...)")
    home = os.path.normpath(str(Path.home()))
    path = os.path.normpath(os.path.expanduser(identity))
    real_home, real = os.path.realpath(home), os.path.realpath(path)

    def below(inner, outer):
        return inner.startswith(outer.rstrip("/") + "/") and inner != outer
    if not below(path, home) or not below(real, real_home):
        raise ValueError(f"host {name}: identity {printable(identity)} lies outside your HOME ({printable(home)}); "
                         "give a key file there, e.g. ~/.ssh/id_ed25519")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return  # not there (yet): ssh says so at the connection
    except OSError as exc:
        raise ValueError(f"host {name}: identity {printable(identity)}: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        kind = "a symbolic link" if stat.S_ISLNK(info.st_mode) else "not a regular file"
        raise ValueError(f"host {name}: identity {printable(identity)} is {kind}; give the key file itself")


HOSTS_LOCK_TIMEOUT = 15  # seconds hosts_file_lock waits for another writer of hosts.json
_held_locks = threading.local()


@contextlib.contextmanager
def hosts_file_lock(path=None, timeout=HOSTS_LOCK_TIMEOUT):
    """Hold the lock for a read-modify-write of hosts.json (PATH, else config_file()): an flock on PATH.lock
    beside it (0600, this user's, never through a symbolic link), so that the CLI, the app and the web interface
    never lose each other's change. Reentrant within a thread (save() takes it as well); waits at most TIMEOUT
    seconds for another writer, then a ValueError. Yields PATH."""
    path = Path(path or config_file())
    held = getattr(_held_locks, "paths", None)
    if held is None:
        held = _held_locks.paths = set()
    key = str(path.absolute())
    if key in held:
        yield path
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"{printable(lock)}: not a regular file of yours; remove it")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise ValueError(f"{printable(path)} is being changed by another sandbox-ai (the app, the web "
                                     "interface or a command); try again") from None
                time.sleep(0.05)
        held.add(key)
        try:
            yield path
        finally:
            held.discard(key)
    finally:
        os.close(fd)  # (and with it the lock)


def load(path=None):
    """{name: host} of the file; an empty list when there is none. A damaged file is an error, never half read."""
    path = Path(path or config_file())
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError(f"{path}: too large")
    try:
        document = json.loads(data.decode("utf-8"))
    except RecursionError:  # (nested deeper than Python reads: damaged)
        raise ValueError(f"{path}: not valid JSON (nested too deeply)") from None
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from None
    hosts = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(hosts, dict):
        raise ValueError(f"{path}: {{\"version\": 1, \"hosts\": {{NAME: {{\"ssh\": \"USER@HOST\"}}}}}}")
    return {name: check(name, entry) for name, entry in hosts.items()}


def save(hosts, path=None):
    """Write HOSTS to the file, atomically, under hosts_file_lock (a read-modify-write holds it around its load()
    as well)."""
    path = Path(path or config_file())
    with hosts_file_lock(path):
        fd, temporary = tempfile.mkstemp(prefix=".hosts-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, "hosts": hosts}, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def entries(path=None):
    """[{"name", "kind", ...}]: this machine, then each host of the file."""
    return [{"name": LOCAL, "kind": "local", "description": "this machine"},
            *({"name": name, "kind": "ssh", **entry} for name, entry in sorted(load(path).items()))]


# One SSH connection per host, shared by the commands that follow within a minute (the web interface and the app
# poll every few seconds: each would pay a full handshake otherwise). The master is started on its own, detached
# from every pipe (a master that ssh backgrounds itself keeps the first command's stderr open), in a folder that is
# this user's alone: XDG_RUNTIME_DIR/sandbox-ai/ssh, else (macOS) ~/Library/Caches/sandbox-ai/ssh, else
# $TMPDIR/sandbox-ai-UID/ssh (each folder sandbox-ai makes 0700 and checked to be the user's).
# The app, the web interface and the CLI are separate processes: a lock file beside the socket (flock) lets one of
# them at a time check, clean up and start a host's master; a command that finds it taken connects on its own
# meanwhile (through the master if one answers) instead of waiting. A start that failed is not tried again for
# MASTER_RETRY seconds (the lock file keeps its time for the other processes); commands connect on their own
# meanwhile. The start is spent out of the command's own timeout. A command through the master that fails as ssh
# (exit status 255), or gets no answer at all, is tried once more on its own connection and the master is asked to
# exit: one whose TCP connection died still answers -O check. SANDBOX_AI_SSH_MULTIPLEX=0 switches it off.
CONTROL_PERSIST = 60
MASTER_RETRY = 60
CONNECTION = ("ssh", "port", "identity", "command")  # what a shared connection is made for (its socket's name)
# A socket's path is at most 104 bytes on macOS (108 on Linux), and ssh binds PATH.<16 random characters> first.
SOCKET_LIMIT = 104 - 1 - 17
_failed = {}  # str(control path) -> time.monotonic() of a master start that failed in this process
_failed_lock = threading.Lock()


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


def control_folders(platform=None):
    """[(folder, mode bits it must not have)]: the folders sandbox-ai makes for the control sockets, the socket
    folder last (see above)."""
    platform = platform or sys.platform
    if os.environ.get("XDG_RUNTIME_DIR"):  # the user's alone (0700, by its definition)
        top = [(Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai", 0o002)]
    elif platform == "darwin":
        top = [(Path.home() / "Library" / "Caches" / "sandbox-ai", 0o022)]
    else:
        top = [(temp_root(make=False), 0o077)]  # (another user's sandbox-ai-UID is never used: temp_root)
    return [*top, (top[0][0] / "ssh", 0o077)]


def private_folder(folder, forbidden):
    """FOLDER made (0700) unless it exists; True when it is a folder of this user's (not a symbolic link) without
    the mode bits FORBIDDEN."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(folder, 0o700)
    info = os.lstat(folder)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & forbidden


def control_path(entry, platform=None):
    """The control socket for ENTRY's connection, or None (switched off, no private folder for it, a path too long
    for a socket or one ssh would read differently)."""
    if os.environ.get("SANDBOX_AI_SSH_MULTIPLEX", "1") == "0":
        return None
    folders = control_folders(platform)
    key = hashlib.sha256(json.dumps([entry.get(name) for name in CONNECTION]).encode()).hexdigest()[:16]
    path = folders[-1][0] / key
    if len(os.fsencode(path)) > SOCKET_LIMIT or re.search(r"[\s\"'%]", str(path)):
        return None
    try:
        if platform == "darwin" or (platform is None and sys.platform == "darwin") or \
                folders[0][0].name == "run":  # ~/Library/Caches, the cache's sandbox-ai (temp_root's fallback)
            folders[0][0].parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not all(private_folder(folder, forbidden) for folder, forbidden in folders):
            return None
    except OSError:
        return None
    return path


def master_lock(path):
    """An open descriptor of PATH.lock (made 0600, never through a symbolic link, this user's), or None."""
    try:
        fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        return None
    return fd


def master_failed(path, fd=None):
    """Whether a master start for PATH failed less than MASTER_RETRY seconds ago, in this process or (its time in
    the lock file FD) in another one."""
    with _failed_lock:
        when = _failed.get(str(path))
    if when is not None and time.monotonic() - when < MASTER_RETRY:
        return True
    if fd is None:
        return False
    try:
        noted = float(os.pread(fd, 32, 0).decode("ascii") or "nan")
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return -1 < time.time() - noted < MASTER_RETRY  # (-1: the time as written, rounded)


def note_master(path, fd, ok):
    """Remember whether the master start for PATH worked (in this process, and in the lock file FD)."""
    with _failed_lock:
        if ok:
            _failed.pop(str(path), None)
        else:
            _failed[str(path)] = time.monotonic()
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        if not ok:
            os.pwrite(fd, f"{time.time():.3f}".encode(), 0)


def ensure_master(entry, path, runner=subprocess.run, deadline=None):
    """Whether commands for ENTRY go through the shared connection at PATH: one answers there, this call started
    it, or another process or thread is checking or starting it right now and one may answer (the command then
    uses it if it does, else connects on its own). False when it could not be started (or failed less than
    MASTER_RETRY seconds ago): the command connects on its own, as without multiplexing. DEADLINE
    (time.monotonic()) bounds the check and the start: they are spent out of the command's own timeout."""
    def budget(limit):
        return limit if deadline is None else min(limit, deadline - time.monotonic())
    if master_failed(path) or budget(1) < 1:
        return False
    fd = master_lock(path)
    if fd is None:
        return False
    try:
        if master_failed(path, fd):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return False
            # Another process or thread checks or starts it: never wait for that (an unreachable host would
            # hold every poll for its whole ConnectTimeout).
            return path.exists()
        base = ssh_base(entry, control=path)
        check = [base[0], *base[1:-2], "-O", "check", "--", entry["ssh"]]
        try:
            if path.exists() and runner(check, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=budget(10)).returncode == 0:
                note_master(path, fd, True)
                return True
            if budget(1) < 1:
                return False
            # Under the lock nobody else is starting a master here: a socket that does not answer is one that
            # ended (or never finished starting).
            with contextlib.suppress(FileNotFoundError):
                if path.is_socket():
                    path.unlink()
            plain = ssh_base(entry)
            start = [plain[0], *plain[1:-2], "-o", f"ControlPath={path}", "-o", "ControlMaster=yes",
                     "-o", f"ControlPersist={CONTROL_PERSIST}", "-f", "-N", "--", entry["ssh"]]
            ok = runner(start, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=budget(CONNECT_TIMEOUT + 5)).returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.debug("hosts", f"the shared SSH connection to {entry['ssh']} could not be checked or started", exc)
            ok = False
        if not ok:
            LOG.debug("hosts", f"no shared SSH connection to {entry['ssh']}: each command connects on its own")
        note_master(path, fd, ok)
        return ok
    finally:
        os.close(fd)  # (and with it the lock)


def stop_master(entry, path=None, runner=subprocess.run, timeout=5, how="exit"):
    """Ask the shared connection for ENTRY (at PATH, else its usual place) to exit (HOW "exit": its sessions end
    too; for a host that was removed or whose connection settings changed) or to stop taking new sessions (HOW
    "stop": the commands running through it go on; for a master that seems dead). Best effort, at most TIMEOUT
    seconds."""
    path = path or control_path(entry)
    if path is None or not path.exists():
        return False
    try:
        base = ssh_base(entry, control=path)
        return runner([base[0], *base[1:-2], "-O", how, "--", entry["ssh"]], stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout).returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def ssh_base(entry, tty=False, control=None):
    """The ssh command line for ENTRY up to the remote command: no prompt, the host key strictly checked once
    known (the first key is learnt: accept-new), into sandbox-ai's own known_hosts as well."""
    known = known_hosts_file()
    if '"' in str(known):
        raise ValueError(f"{known}: a path with a double quote cannot be given to ssh")
    known.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # accept-new writes the first key there
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               # Quoted for ssh's own word splitting: a path with a space would otherwise be two files, neither of
               # them sandbox-ai's, and accept-new would accept any key at every connection.
               "-o", f'UserKnownHostsFile="{known}" "~/.ssh/known_hosts"', "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-p", str(entry["port"])]
    if entry.get("identity"):
        options += ["-o", "IdentitiesOnly=yes", "-i", os.path.expanduser(entry["identity"])]
    if control is not None:
        # Use the shared connection if it answers, else connect as usual (never become a master here).
        options += ["-o", f"ControlPath={control}", "-o", "ControlMaster=no"]
    if tty:
        # A terminal (a tab of the app, the web's, a terminal window) shows what ssh says too: with a terminal it says
        # "Connection to HOST closed." at every end - noise under the command's own output (user report 2026-11-19).
        # Its errors (refused, no key, a changed host key) are still said.
        options += ["-o", "LogLevel=ERROR"]
    return ["ssh", "-tt" if tty else "-T", *options, "--", entry["ssh"]]


SUDO = {"batch": "sudo -n -- ", "prompt": "sudo -- "}


def command_word(command):
    """A host's own COMMAND for the remote shell, quoted; a leading ~/ as "$HOME"/ (a quoted ~ is not expanded, so
    ~/bin/sandbox-ai would name a folder called ~)."""
    if command.startswith("~/"):
        return '"$HOME"/' + shlex.quote(command[2:])
    return shlex.quote(command)


# The one line ssh hands the remote user's login shell ($SHELL -c LINE) is read by that shell, whichever it is: sh,
# bash, zsh, but also fish (inside '...' it takes \' and \\ as escapes, and has no if/then) or csh and tcsh (a line
# break inside '...' is an error there, ! is history). shlex.quote is POSIX only. So the line is always
# /bin/sh -c '...' with the script inside in characters each of them reads alike - printable ASCII but ' " \ $ `
# and ! - and every other byte as \0NNN, which sh itself turns back into the script (printf %b, POSIX) and runs.
# No login shell parses an argument, and none needs to be POSIX.
NEUTRAL_PLAIN = frozenset(chr(code) for code in range(32, 127)) - set("'\"\\$`!")


NEUTRAL_HEAD, NEUTRAL_TAIL = "/bin/sh -c 'v=$(printf %b \"", "X\"); eval \"${v%X}\"'"


def neutral_command(script):
    """The line for ssh that runs the POSIX sh SCRIPT on the remote host whatever the login shell there is."""
    encoded = "".join(char if char in NEUTRAL_PLAIN else
                      "".join(f"\\0{byte:03o}" for byte in char.encode("utf-8", "surrogateescape"))
                      for char in script)
    # (X: command substitution drops the script's last line breaks, never the X after them.)
    return f"{NEUTRAL_HEAD}{encoded}{NEUTRAL_TAIL}"


def neutral_script(line):
    """The script of a LINE neutral_command made (what the remote sh runs), or None for another line."""
    if not (line.startswith(NEUTRAL_HEAD) and line.endswith(NEUTRAL_TAIL)):
        return None
    encoded = line[len(NEUTRAL_HEAD):len(line) - len(NEUTRAL_TAIL)].encode("utf-8", "surrogateescape")
    return re.sub(rb"\\0([0-7]{3})", lambda match: bytes([int(match.group(1), 8)]), encoded).decode(
        "utf-8", "surrogateescape")


# The sandbox-ai a host's commands run when its entry names none (--command): the SSH user's own installation
# (install.sh's ~/.local/bin/sandbox-ai) when there is one, else the one on the PATH of the SSH command. A terminal
# there finds the same: install.sh's PATH block (and Debian's, Fedora's ~/.profile) put ~/.local/bin first - but a
# non-interactive SSH command reads none of them, and found a system installation in /usr/local/bin instead (user
# report 2026-11-23: the terminal on coding-101 ran the user's 0.1.188, the app's commands and its Update the system's
# 0.1.234). Every command of the app, its terminals and its checks (a start's --help) goes to this one.
OWN_COMMAND = '"$HOME/.local/bin/sandbox-ai"'
# An installation's own command (hosts.installations: DATA/current/sandbox-ai of install.sh's user or system one).
EXECUTABLE = re.compile(r"/[^\0\n\r]{1,400}/current/sandbox-ai")


def remote_command(entry, args, sudo=None, executable=None):
    """The one string ssh hands the remote shell (neutral_command: whatever login shell the user has there):
    sandbox-ai with ARGS, each quoted; the host's own command (--command), else ~/.local/bin/sandbox-ai, else the one
    on PATH (OWN_COMMAND); EXECUTABLE: that installation's own command instead (installations: update and uninstall
    of the one chosen). SUDO: "batch" runs it as `sudo -n` (refused rather than asking for a password), "prompt" as
    `sudo` (in a terminal, which asks); sandbox-ai's path is found as you either way."""
    quoted = " ".join(shlex.quote(str(arg)) for arg in args)
    prefix = SUDO[sudo] if sudo else ""
    if executable is not None:
        if not EXECUTABLE.fullmatch(executable) or "/../" in executable:
            raise ValueError(f"not an installation's command: {printable(executable)[:200]}")
        return neutral_command(f"exec {prefix}{shlex.quote(executable)} {quoted}".rstrip())
    if entry.get("command"):
        return neutral_command(f"exec {prefix}{command_word(entry['command'])} {quoted}".rstrip())
    if sudo:
        return neutral_command(f'if [ -x {OWN_COMMAND} ]; then exec {prefix}{OWN_COMMAND} {quoted}; else exec '
                               f'{prefix}"$(command -v sandbox-ai || echo sandbox-ai)" {quoted}; fi')
    return neutral_command(f'if [ -x {OWN_COMMAND} ]; then exec {OWN_COMMAND} {quoted}; '
                           f'else exec sandbox-ai {quoted}; fi')


def argv(name, args, tty=False, path=None, sudo=None, control=None, executable=None):
    """The command line that runs `sandbox-ai ARGS` on host NAME (LOCAL: this machine's sandbox-ai); SUDO,
    EXECUTABLE: see remote_command."""
    if any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise ValueError("an argument is not a string without NUL")
    if sudo not in (None, *SUDO):
        raise ValueError(f"sudo is one of {', '.join(SUDO)}")
    if name == LOCAL:
        here = Path(__file__).resolve().parent
        command = [sys.executable, "-I", str(here / "main.py"), *args]
        return ["sudo", *(["-n"] if sudo == "batch" else []), "--", *command] if sudo else command
    hosts = load(path)
    if name not in hosts:
        raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
    entry = hosts[name]
    return [*ssh_base(entry, tty, control=None if tty else control),
            remote_command(entry, args, sudo, executable)]


def run(name, args, input=None, timeout=120, path=None, runner=subprocess.run, sudo=None, executable=None):
    """(exit status, stdout, stderr) of `sandbox-ai ARGS` on host NAME; 255 when SSH itself failed, 124 when it
    did not end within TIMEOUT seconds (starting the shared connection included). EXECUTABLE: an installation's own
    command there (remote_command)."""
    begun = time.monotonic()
    deadline = begun + timeout
    control = entry = None
    if name != LOCAL and runner is subprocess.run:  # (tests give a runner of their own: one command each)
        entry = load(path).get(name)
        control = control_path(entry) if entry else None
        if control is not None and not ensure_master(entry, control, deadline=deadline):
            control = None
    status, out, err, answered = run_once(name, args, input, deadline - time.monotonic(), timeout, path, runner,
                                          sudo, control, executable)
    if control is not None and (status == 255 or status == 124 and not answered):
        # The master may be dead (it still answers -O check, locally): it stops taking new sessions (-O stop, so
        # the commands running through it on that host go on), and the next command makes a new one. The command
        # runs once more on a connection of its own only when it never reached the host (the master refused the
        # session): a command that may have run (a timeout, a connection lost on the way) is never run twice.
        stop_master(entry, control, how="stop")
        if status == 255 and mux_refused(out, err):
            status, out, err, _ = run_once(name, args, input, max(deadline - time.monotonic(),
                                                                  min(timeout, CONNECT_TIMEOUT + 5)),
                                           timeout, path, runner, sudo, None, executable)
    ran(name, f"sandbox-ai {LOG.command_line(args)}", begun, status, err)
    return status, out, err


def ran(name, what, begun, status, err):
    """The line of a command on host NAME in launcher.log: DEBUG when it worked, INFO with the end of its error output
    when the command said no; that SSH could not reach the host (255, no answer: 124, no ssh: 127) once until it
    answers again (the app polls the hosts' status)."""
    text = f"{name}: {what} ({time.monotonic() - begun:.1f} s): exit status {status}"
    unreachable = status in (124, 127, 255)
    key = ("host reached", name)
    if unreachable:
        LOG.changed(key, False, "hosts", f"{text}: {LOG.tail(err)}", "WARNING")
        return
    if LOG.state(key) is False:
        LOG.changed(key, True, "hosts", f"{name} answers again")
    if status == 0:
        LOG.debug("hosts", text)
    else:
        LOG.info("hosts", f"{text}: {LOG.tail(err)}")


# What ssh itself says when the shared connection could not even open a session (nothing reached the remote host):
# a line of its own at the start of stderr, never what the remote command printed (mux_refused).
MUX_REFUSED = re.compile(r"(?:mux_client_\w+|muxclient|mux_master\w*): .*|"
                         r"Control socket connect\(.*\): (?:Connection refused|No such file or directory)|"
                         r"ControlSocket .* already exists, disabling multiplexing|"
                         r"Session open refused by peer|master (?:refused|is not running).*")


def mux_refused(out, err):
    """ssh's shared connection refused the session (MUX_REFUSED), so the command never reached the host: stdout is
    empty and the first line of stderr (after ssh's own "Warning: Permanently added" notes) is one of ssh's mux
    errors. Anything the remote command wrote first (a word like "Control socket" in its own errors) is no refusal:
    a command that may have run is never run twice."""
    if out:
        return False
    for line in (err or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Warning: Permanently added"):
            continue
        return bool(MUX_REFUSED.fullmatch(line))
    return False


def run_once(name, args, input, seconds, timeout, path, runner, sudo, control, executable=None):
    """(status, stdout, stderr, whether anything was answered) of one try of run() within SECONDS."""
    command = argv(name, args, path=path, sudo=sudo, control=control, executable=executable)
    if seconds <= 0:
        return 124, "", f"sandbox-ai on {name}: no answer within {timeout} s", False
    try:
        done = runner(command, input=input, capture_output=True, text=True, timeout=seconds,
                      **({} if input is not None else {"stdin": subprocess.DEVNULL}))
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"sandbox-ai on {name}: no answer within {timeout} s", bool(exc.stdout or exc.stderr)
    except OSError as exc:
        return 127, "", f"{command[0]}: {exc.strerror}", False
    return done.returncode, done.stdout, done.stderr, True


def run_with_file(name, args, source, timeout=3600, path=None, runner=subprocess.run):
    """(exit status, stdout, stderr) of `sandbox-ai ARGS` on host NAME with the file SOURCE as its standard input, byte
    for byte (a session's archive for `session import -`: no rsync or scp needed there, only sshd and sandbox-ai; what
    arrives is checked by the import there). A connection of its own (no shared one: a long transfer never holds up
    the commands of the app). 255 when SSH failed, 124 when it did not end within TIMEOUT seconds, 127 without ssh."""
    begun = time.monotonic()
    command = argv(name, args, path=path)
    try:
        with open(source, "rb") as stream:
            done = runner(command, stdin=stream, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        status, out, err = 124, "", f"sandbox-ai on {name}: no answer within {timeout} s"
    except OSError as exc:
        status, out, err = 127, "", f"{command[0]}: {exc.strerror or exc}"
    else:
        status = done.returncode
        out, err = (value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""
                    for value in (done.stdout, done.stderr))
    ran(name, f"sandbox-ai {LOG.command_line(args)} < {Path(source).name}", begun, status, err)
    return status, out, err


def run_json(name, args, timeout=120, path=None, runner=subprocess.run):
    """The JSON a `sandbox-ai ARGS --json` prints on host NAME, or a ValueError with what it said."""
    status, out, err = run(name, args, timeout=timeout, path=path, runner=runner)
    try:
        return json.loads(out)
    except (ValueError, RecursionError):
        raise ValueError(f"{name}: sandbox-ai {' '.join(args[:2])}: exit status {status}: "
                         f"{printable((err or out).strip()[-500:])}") from None


# --- key files others can read (ssh refuses them) ---------------------------------------------------------

def key_candidates(entry):
    """The key files ssh uses for ENTRY: its identity, else the default ones in ~/.ssh."""
    if entry.get("identity"):
        return [Path(os.path.expanduser(entry["identity"]))]
    return [Path.home() / ".ssh" / name for name in DEFAULT_KEYS]


def looks_like_private_key(fd):
    head = os.pread(fd, 80, 0)
    return head.startswith(b"-----BEGIN ") and b"PRIVATE KEY-----" in head.split(b"\n", 1)[0]


def protect_key(path, require_key=False):
    """Make the key file PATH private (0600) when group or others can read it: ({"path", "was", "now"} or None,
    a warning or None). Only a regular file this user owns, never through a symbolic link; REQUIRE_KEY (a path
    taken from ssh's output, not one of the host's keys): only a file that holds a private key."""
    shown = printable(str(path))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            return None, f"{shown}: {exc.strerror}"
        target = Path(path).resolve()
        try:
            too_open = stat.S_IMODE(target.stat().st_mode) & 0o077
        except OSError:
            too_open = False
        return None, (f"{shown} is a symbolic link to {printable(str(target))}, which others can read: left "
                      f"alone; make it private yourself: chmod 600 {printable(str(target))}") if too_open else None
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or not mode & 0o077:
            return None, None
        if info.st_uid != os.getuid():
            return None, (f"{shown} can be read by others (mode {mode:04o}), so ssh refuses it, but it belongs to "
                          f"another user: left alone; its owner can run: chmod 600 {shown}")
        if require_key and not looks_like_private_key(fd):
            return None, None
        os.fchmod(fd, 0o600)
        return {"path": str(path), "was": f"{mode:04o}", "now": "0600"}, None
    except OSError as exc:
        return None, f"{shown}: {exc.strerror}"
    finally:
        os.close(fd)


def protect_keys(entry, output=""):
    """([fixed], [warnings]) after making ENTRY's key files private, and the key files ssh's OUTPUT names as too
    open (one an ssh_config names); a key folder that others can write to is named, not changed."""
    fixed, warnings = [], []
    # The host's identity is changed only if it holds a private key, as what ssh's output names (never any file).
    paths = [(path, bool(entry.get("identity"))) for path in key_candidates(entry)]
    for match in TOO_OPEN.finditer(output or ""):
        path = Path(match.group(1) or match.group(2))
        if path.is_absolute() and all(path != known for known, _ in paths):
            paths.append((path, True))
    directories = {Path.home() / ".ssh"}
    for path, require_key in paths:
        done, warning = protect_key(path, require_key)
        if done:
            fixed.append(done)
        if warning:
            warnings.append(warning)
        if done or os.path.lexists(path):
            directories.add(path.parent)
    for directory in sorted(directories):
        try:
            info = directory.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o022:
            warnings.append(f"{printable(str(directory))} can be written by others (mode "
                            f"{stat.S_IMODE(info.st_mode):04o}): they could replace your keys there; left alone; "
                            f"make it yours alone: chmod 700 {printable(str(directory))}")
    return fixed, warnings


def fixed_text(change):
    return (f"{printable(change['path'])} could be read by others (mode {change['was']}), which ssh refuses: made "
            f"private ({change['now']})")


def run_fixing(name, args, timeout=30, path=None, runner=subprocess.run, sudo=None, executable=None):
    """run() for host NAME with its key files made private first, and once more when ssh still refuses a key
    file as too open: (status, out, err, fixed, warnings)."""
    entry = None if name == LOCAL else load(path).get(name)
    fixed, warnings = protect_keys(entry) if entry else ([], [])
    status, out, err = run(name, args, timeout=timeout, path=path, runner=runner, sudo=sudo, executable=executable)
    if entry and status != 0 and TOO_OPEN.search(err or ""):
        more, more_warnings = protect_keys(entry, err)
        fixed += more
        warnings += [warning for warning in more_warnings if warning not in warnings]
        if more:
            status, out, err = run(name, args, timeout=timeout, path=path, runner=runner, sudo=sudo,
                                   executable=executable)
    return status, out, err, fixed, warnings


def test(name, path=None, runner=subprocess.run):
    """{"name", "ssh", "version", "doctor": [...], "error", "fixed": [...], "warnings": [...]}: whether NAME
    answers and how its sandbox-ai is; "fixed": the key files made private on the way (protect_keys)."""
    result = {"name": name, "ssh": False, "version": None, "doctor": None, "error": None}
    status, out, err, result["fixed"], result["warnings"] = run_fixing(name, ["--version"], path=path,
                                                                        runner=runner)
    if status != 0:
        result["error"] = printable((err or out).strip()[-500:]) or f"exit status {status}"
        if status == 255:
            result["error"] += " (SSH: is the key in ~/.ssh/authorized_keys there, and the host reachable?)"
        return result
    result["ssh"], result["version"] = True, out.strip()
    try:
        result["doctor"] = run_json(name, ["doctor", "--json"], timeout=90, path=path, runner=runner)
    except ValueError as exc:
        result["error"] = str(exc)
    return result


def failure(status, out, err):
    """What a failed remote sandbox-ai said, in one short printable piece (with the SSH hint for status 255)."""
    # One line for the lists and the app: the remote's lines joined by "; " (not escaped: they showed as "\x0a").
    said = "; ".join(line.strip() for line in (err or out).strip()[-500:].splitlines() if line.strip())
    text = printable(said) or f"exit status {status}"
    if status == 255:
        text += " (SSH: is the key in ~/.ssh/authorized_keys there, and the host reachable?)"
    return text


# --- the sandbox-ai of a host: its version, its kind of installation, its update -------------------------

def version_key(version):
    """X.Y.Z of VERSION as numbers for comparing (a suffix does not count), or None."""
    found = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)", str(version or ""))
    return tuple(int(part) for part in found.groups()) if found else None


AS_GIVEN_SINCE = (0, 1, 78)  # the launcher's --workspaces-as-given (and the rule it switches off) came with it


def known_version(name, path=None):
    """(the version text, its key) of the sandbox-ai on host NAME as its last status found it (hosts list; never
    asked here), or (None, None) when that is not known. This machine: None too (it is this copy)."""
    if name == LOCAL:
        return None, None
    try:
        found = statuses([name], cached=True, path=path).get(name)
    except (OSError, ValueError):
        return None, None
    version = found.get("version") if isinstance(found, dict) else None
    key = version_key(version) if isinstance(version, str) else None
    return (version, key) if key else (None, None)


def older_than(name, since, path=None):
    """The version of the sandbox-ai on host NAME when its last status found one older than SINCE (a version key),
    else None (newer, the same, not known, or this machine)."""
    version, key = known_version(name, path)
    return version if key and key < since else None


def ask_version(name, path=None, command=None, timeout=30):
    """(the version text, its key) of the sandbox-ai on host NAME: its last status's, else asked now (a short
    `sandbox-ai version --verbose` over SSH, never the slow update check). What is asked is kept as a status that
    hosts list still refreshes (checked 0: as old as can be), so a host just added is known at its first start.
    COMMAND(name, args, timeout) -> (status, out, err): how it is asked (default: run). (None, None) when the host
    does not answer or this is this machine."""
    version, key = known_version(name, path)
    if key or name == LOCAL:
        return version, key
    try:
        status, out, _ = (command or (lambda host, args, timeout: run(host, args, timeout=timeout, path=path)))(
            name, ["version", "--verbose"], timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None
    info = installation(out) if status == 0 else {}
    key = version_key(info["version"]) if isinstance(info.get("version"), str) else None
    if not key:
        return None, None
    entry = load(path).get(name)
    if entry is not None:
        kept = load_statuses()
        kept[name] = {"name": name, **{field: info.get(field) for field in ("version", "kind", "path", "prefix")},
                      "available": None, "update": None, "error": None, "fixed": [], "checked": 0,
                      "ssh": entry["ssh"], "port": entry["port"]}
        save_statuses(kept)
    return info["version"], key


def knows_workspaces_as_given(name, path=None, ask=False):
    """The sandbox-ai on host NAME takes --workspaces-as-given: this machine's always; another host's when the version
    its last status found (hosts list) is 0.1.78 or later; ASK (True, or ask_version's COMMAND): a host whose version
    is not known yet is asked now (one short SSH command), else it counts as older. Older ones do not know the option, and neither do
    they let the directory a start runs in decide /work, so a start there without it is the same."""
    if name == LOCAL:
        return True
    _, key = ask_version(name, path, None if ask is True else ask) if ask else known_version(name, path)
    return bool(key and key >= AS_GIVEN_SINCE)


def installation(text):
    """{"version", "path", "kind", "prefix"} of what `sandbox-ai version --verbose` printed. KIND: "user" (install.sh
    in ~/.local/share/sandbox-ai or XDG_DATA_HOME), "system" (install.sh in PREFIX/lib/sandbox-ai), "package"
    (pyz, AppImage, macOS app), "pip", "checkout", or "unknown" (a version before --verbose: it says only its
    version). "self_update": a package or a pip installation whose own `sandbox-ai update` replaces it (the releases
    after 0.1.98, where whoever runs it may write it)."""
    info = {"version": None, "path": None, "kind": "unknown", "prefix": None}
    for line in text.splitlines():
        if line.startswith("sandbox-ai ") and info["version"] is None:
            found = VERSION.search(line)
            info["version"] = found.group(0) if found else None
        elif line.startswith("installed in: "):
            info["path"] = line[len("installed in: "):].strip()
        elif line.startswith("package: "):
            info["kind"], info["package"] = "package", line[len("package: "):].strip()[:40]
        elif line.startswith("updates: not through install.sh"):
            info["kind"] = "pip"
        elif line.startswith("self-update: "):
            info["self_update"] = line[len("self-update: "):].strip() == "yes"
        elif line.startswith("update mode: ") and line[len("update mode: "):].strip() in UPDATE_MODES:
            info["update_mode"] = line[len("update mode: "):].strip()  # (0.1.122 on: sandbox-ai update mode)
    if info["kind"] == "unknown" and info["path"]:
        found = re.fullmatch(r"(/.*)/versions/[^/]+", info["path"])
        if found is None:
            info["kind"] = "checkout"
        elif found.group(1).endswith("/lib/sandbox-ai") and not found.group(1).startswith(HOMES):
            info["kind"], info["prefix"] = "system", found.group(1)[:-len("/lib/sandbox-ai")] or "/"
        else:
            info["kind"] = "user"
    return info


# --- every sandbox-ai on a host: install.sh's (the user's own, a system one), pip, pipx, a package ------------
#
# One shell script over SSH (no sandbox-ai needed there, python3 is - install.sh's own requirement): the command a
# terminal there finds (the user's login shell, interactive: `$SHELL -lic 'command -v sandbox-ai'`, which reads
# ~/.bashrc and its PATH block), the one the app's commands run (OWN_COMMAND), and every sandbox-ai in ~/.local/bin,
# ~/.bin, the user's install.sh data folder, /usr/local/bin, /usr/bin and on PATH: an install.sh installation by its
# folder (DATA/versions/X, the version read from its file), any other by its own `version --verbose`.
INSTALLATIONS_PY = r"""
import json, os, re, subprocess, sys
home = os.path.expanduser("~")
data = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
names = [n for n in sys.argv[1:4] if n.startswith("/")]
names += [os.path.join(home, ".local/bin/sandbox-ai"), os.path.join(home, ".bin/sandbox-ai"),
          os.path.join(data, "sandbox-ai/current/sandbox-ai"), "/usr/local/bin/sandbox-ai", "/usr/bin/sandbox-ai"]
names += [os.path.join(d, "sandbox-ai") for d in os.environ.get("PATH", "").split(":") if d.startswith("/")]
found, seen = [], {}
for name in names:
    if not (os.path.isfile(name) and os.access(name, os.X_OK)):
        continue
    real = os.path.realpath(name)
    match = re.fullmatch(r"(/.*)/versions/([^/]+)/sandbox-ai", real)
    key = match.group(1) if match else real
    if key in seen:
        seen[key]["links"].append(name)
        continue
    item = {"links": [name], "real": real, "data": None, "tree": os.path.dirname(real), "version": None,
            "verbose": None}
    if match:
        item["data"] = match.group(1)
        try:
            with open(os.path.join(item["tree"], "sandbox", "agent_sandbox.py")) as stream:
                for line in stream:
                    if line.startswith("__version__ = "):
                        item["version"] = line.split('"')[1]
                        break
        except (OSError, IndexError, UnicodeDecodeError):
            pass
    else:
        try:
            item["verbose"] = subprocess.run([name, "version", "--verbose"], capture_output=True, text=True,
                                             stdin=subprocess.DEVNULL, timeout=30).stdout[:4000]
        except (OSError, subprocess.SubprocessError):
            pass
    seen[key] = item
    found.append(item)
uid = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4].isdigit() else str(os.getuid())
print(json.dumps({"uid": int(uid), "home": home, "terminal": sys.argv[1], "commands": sys.argv[2],
                  "plain": sys.argv[3], "installations": found}))
"""
INSTALLATIONS_SH = (
    'own="$HOME/.local/bin/sandbox-ai"; plain=$(command -v sandbox-ai 2>/dev/null); '
    'if [ -x "$own" ]; then commands=$own; else commands=$plain; fi; '
    'login=${SHELL:-}; case $login in */bash|*/zsh|*/ksh|*/mksh|*/dash|*/sh) ;; '
    '*) login=$(command -v bash 2>/dev/null) ;; esac; terminal=; '
    'if [ -n "$login" ]; then t=; command -v timeout >/dev/null 2>&1 && t="timeout 20"; '
    "terminal=$($t \"$login\" -lic 'printf \"\\nSANDBOX_AI_FOUND=%s\\n\" \"$(command -v sandbox-ai)\"' "
    "</dev/null 2>/dev/null | sed -n 's/^SANDBOX_AI_FOUND=//p' | tail -n 1); fi; "
    'case $terminal in /*) ;; *) terminal= ;; esac; '
    'exec python3 -I -c "$py" "$terminal" "$commands" "$plain" "$(id -u)"')


def installation_of(item):
    """{"kind", "version", "path", "prefix", "data", "links", "executable"} of one sandbox-ai the probe found: an
    install.sh installation (kind user or system, EXECUTABLE its own DATA/current/sandbox-ai), else what its
    `version --verbose` said (pip, package, checkout, unknown)."""
    if item.get("data"):
        info = installation(f"sandbox-ai {item.get('version') or '?'}\ninstalled in: {item.get('tree')}\n")
        executable = f"{item['data']}/current/sandbox-ai"
        info.update(data=item["data"], executable=executable if EXECUTABLE.fullmatch(executable) else None)
    else:
        # (a wrapper that runs an install.sh installation says that one's place: named, never run as one here)
        info = installation(item.get("verbose") or "")
        info.update(data=None, executable=None)
    info["links"] = [printable(link) for link in item.get("links") or [] if isinstance(link, str)][:8]
    info["version"] = printable(info["version"]) if info.get("version") else None
    info["path"] = printable(info["path"]) if info.get("path") else printable(item.get("real") or "") or None
    return {key: info.get(key) for key in ("kind", "version", "path", "prefix", "data", "links", "executable",
                                           "package")}


def installations(name, path=None, runner=subprocess.run):
    """{"installations": [installation_of ..., "terminal": a terminal there runs it, "commands": the app's commands
    run it], "terminal", "commands": the sandbox-ai each one finds, "differ": both found and not the same one,
    "uid", "error"} of host NAME (one SSH command, INSTALLATIONS_SH)."""
    result = {"installations": [], "terminal": None, "commands": None, "differ": False, "uid": None, "error": None}
    entry = load(path).get(name)
    if entry is None:
        raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
    script = f"py={shlex.quote(INSTALLATIONS_PY)}; {INSTALLATIONS_SH}"
    if entry.get("command"):  # (the host's own command: what the app's commands run)
        script = script.replace('if [ -x "$own" ]; then commands=$own; else commands=$plain; fi',
                                f"commands={command_word(entry['command'])}", 1)
    status, out, err = remote_shell(entry, script, timeout=CONNECT_TIMEOUT + 60, runner=runner)
    try:
        found = json.loads(out.decode("utf-8", "replace")) if status == 0 else None
    except (ValueError, RecursionError):
        found = None
    if not isinstance(found, dict) or not isinstance(found.get("installations"), list):
        result["error"] = failure(status, "", err) if status != 0 else "the host's answer was not understood"
        return result
    result["uid"] = found.get("uid") if isinstance(found.get("uid"), int) else None
    items = [item for item in found["installations"][:16] if isinstance(item, dict)]

    def owner(command):
        """The item of the probe a found COMMAND (a path) runs, by its links (or its folder)."""
        if not isinstance(command, str) or not command.startswith("/"):
            return None
        for index, item in enumerate(items):
            if command in (item.get("links") or []) or command == item.get("real"):
                return index
        return None
    terminal, commands = owner(found.get("terminal")), owner(found.get("commands"))
    for index, item in enumerate(items):
        info = installation_of(item)
        info["terminal"], info["commands"] = index == terminal, index == commands
        result["installations"].append(info)
    for key, command in (("terminal", found.get("terminal")), ("commands", found.get("commands"))):
        result[key] = printable(command) if isinstance(command, str) and command else None
    result["differ"] = terminal is not None and commands is not None and terminal != commands
    return result


def installations_text(found, running=None):
    """The other installations of a host beside the one RUNNING (its version) in a few words: "; system 0.1.236",
    and a warning when a terminal there runs another one than the app's commands."""
    items = (found or {}).get("installations") or []
    if not isinstance(items, list):
        return ""
    words = [f"{printable(item.get('kind') or '?')} {printable(item.get('version') or '?')}"
             for item in items if isinstance(item, dict) and not item.get("commands")]
    text = "".join(f"; {word}" for word in words)
    if (found or {}).get("differ"):
        terminal = next((item for item in items if isinstance(item, dict) and item.get("terminal")), {})
        commands = next((item for item in items if isinstance(item, dict) and item.get("commands")), {})
        text += (f"; a terminal there runs {printable(terminal.get('kind') or '?')} "
                 f"{printable(terminal.get('version') or '?')}, the app's commands "
                 f"{printable(commands.get('kind') or '?')} {printable(commands.get('version') or running or '?')}")
    return text


def chosen_installation(found, kind, name="the host"):
    """The installation of FOUND (installations()) of KIND: "user", "system" (the first one), or a data folder; a
    ValueError naming those there are when there is none."""
    items = [item for item in (found or {}).get("installations") or [] if isinstance(item, dict)]
    for item in items:
        if item.get("executable") and (item.get("kind") == kind or item.get("data") == kind):
            return item
    there = ", ".join(f"{printable(item.get('kind'))} {printable(item.get('version') or '?')} "
                      f"({printable(item.get('path') or '?')})" for item in items) or "none"
    raise ValueError(f"{name} has no install.sh installation of the kind {printable(kind)} (there: {there})")


def not_updatable(name, info):
    """Why the sandbox-ai of host NAME (INSTALLATION info) is not updated over SSH, with the way to do it, or None."""
    where = printable(info.get("path") or "?")
    if info.get("self_update") and info["kind"] in ("pip", "package"):
        return None  # (its own `sandbox-ai update` replaces it with the release's file, signed)
    if info["kind"] == "checkout":
        return f"{name}: sandbox-ai there is a checkout ({where}): update it with git there (git pull), or {INSTALL_HINT}"
    if info["kind"] == "pip":
        return (f"{name}: sandbox-ai there is a pip installation that does not replace itself (0.1.98 or older, or "
                f"in a folder its user cannot write): pip install -U sandbox-ai-app there, or {INSTALL_HINT}")
    if info["kind"] == "package":
        return (f"{name}: sandbox-ai there runs from a package ({printable(info.get('package'))}) that does not "
                "replace itself (0.1.98 or older, or its file is not writable there): download the new one from "
                f"the release page there, or {INSTALL_HINT}")
    return None


# The first release without the old single commands (claude-sandbox, codex-sandbox, ...): the installer of a
# sandbox-ai before it requires them in every tree it installs, so its own `update` refuses every release from this one
# on ("not a sandbox-ai tree: claude-sandbox missing"; said after its try: which release it takes is its own business,
# an older one it may still install). This machine's copy (--from-here) brings the new installer.
NEW_LAYOUT = (0, 1, 92)
OLD_LAYOUT_REFUSAL = re.compile(r"not a sandbox-ai tree: (?:claude|codex|opencode)-sandbox|not a sandbox-ai tree: "
                                r"sandbox-(?:session|mcp|wizard|build-image)")


def old_layout_hint(name, before):
    """Why host NAME's own update cannot install the releases since NEW_LAYOUT, and the way that works."""
    key, since = version_key(before), ".".join(map(str, NEW_LAYOUT))
    who = (f"sandbox-ai {printable(before)} on {name} is older than {since}" if key and key < NEW_LAYOUT
           else f"the installer of sandbox-ai on {name} is older than {since}")
    return (f"{who}: its installer still requires the old single commands (claude-sandbox "
            f"...), which the releases since then no longer have, so its own update refuses them; update it with this "
            f"machine's copy instead: sandbox-ai hosts update {name} --from-here")


def update_args(info, version=None):
    """`sandbox-ai update ...` for an installation (INSTALLATION info): the same kind of installation as before
    (a user one stays the user's, even for root; a system one keeps its prefix)."""
    if version is not None and not VERSION.fullmatch(version):
        raise ValueError("--version expects X.Y.Z")
    mode = (["--user"] if info["kind"] == "user" else ["--system", "--prefix", info["prefix"]]
            if info["kind"] == "system" else [])
    return ["update", *(["--version", version] if version else []), *mode]


def not_installed(status, err):
    """Whether a remote command's (STATUS, ERR) says the shell found no sandbox-ai there (sh: exec: ...: not found)."""
    return status == 127 and bool(re.search(r"sandbox-ai[^\n]*(?:not found|No such file)", err or ""))


def host_status(name, path=None, runner=subprocess.run, check=True):
    """{"name", "version", "kind", "path", "prefix", "available", "update", "error", "fixed", "checked",
    "installations", "differ", "missing"}: the sandbox-ai of host NAME (the one its commands run), every installation
    there (installations(): which one a terminal there runs, "differ" when that is another one) and, with CHECK,
    whether a newer release is there (its own `update --check`: the release it would install, checked as it would
    check it). "missing": the host answers but has no sandbox-ai where its commands look."""
    result = {"name": name, "version": None, "kind": None, "path": None, "prefix": None, "available": None,
              "update": None, "update_mode": None, "error": None, "fixed": [], "checked": int(time.time()),
              "installations": None, "differ": False, "missing": False}
    status, out, err, result["fixed"], _ = run_fixing(name, ["version", "--verbose"], path=path, runner=runner)
    if status != 255:
        try:
            found = installations(name, path, runner)
        except Exception as exc:  # noqa: BLE001 - only more to say: the version stands without it
            LOG.debug("hosts", f"{name}: its installations were not read: {exc}")
            found = {"error": str(exc)}
        if not found.get("error"):
            result["installations"], result["differ"] = found["installations"], found["differ"]
    if status != 0:
        result["missing"] = not_installed(status, err)
        result["error"] = (f"no sandbox-ai there (install it: sandbox-ai hosts install {name})" if result["missing"]
                           else failure(status, out, err))
        return result
    info = installation(out)
    result.update({key: info.get(key) for key in ("version", "kind", "path", "prefix", "update_mode")})
    if not check or info["kind"] == "checkout" or (info["kind"] == "pip" and not info.get("self_update")):
        return result
    # As your user `update --check` of a system installation reads none (it would install for you): only the
    # newest release it names counts; the installed version is the one --verbose said.
    status, out, err = run(name, ["update", "--check"], timeout=120, path=path, runner=runner)
    found = re.search(r"^available: (\S+)", out, re.M)
    if found and VERSION.fullmatch(found.group(1)):
        result["available"] = found.group(1)
        if version_key(result["version"]) and version_key(result["available"]):
            # (an update only of a release signed by a key the host trusts - what its update would take; a host
            # too old to say a signature is taken at its word; a host newer than the newest release has nothing to
            # update, and says so)
            line = re.search(r"^signature: (.*)$", out, re.M)
            signed = line is None or line.group(1).startswith("good")
            result["update"] = signed and version_key(result["available"]) > version_key(result["version"])
            result["ahead"] = version_key(result["version"]) > version_key(result["available"])
    else:
        result["error"] = "update check: " + failure(status, out, err)
    return result


def keep_status(name, path=None, runner=subprocess.run, newest=False):
    """Host NAME's status read again now and kept (after an update, an installation or an uninstall: the Hosts page
    and hosts list --cached show the version there at once, never an empty column): its version and installations;
    whether an update is there from its last update check (NEWEST: the newest release was just installed); the
    update check itself waits for the next hosts list after STATUS_TTL. The status, or None for a host not listed."""
    entry = load(path).get(name)
    if entry is None:
        return None
    before = load_statuses().get(name) or {}
    result = host_status(name, path, runner, check=False)
    available = before.get("available") if isinstance(before.get("available"), str) else None
    if newest and version_key(result["version"]) and (not version_key(available) or
                                                      version_key(result["version"]) > version_key(available)):
        available = result["version"]
    if version_key(result["version"]) and version_key(available):
        result["available"] = available
        result["update"] = version_key(available) > version_key(result["version"])
        result["ahead"] = version_key(result["version"]) > version_key(available)
    with STATUS_LOCK:  # (hosts update --jobs N: each host's status kept, none lost to another's write)
        kept = load_statuses()
        kept[name] = {**result, "ssh": entry["ssh"], "port": entry["port"]}
        save_statuses(kept)
    return result


STATUS_LOCK = threading.Lock()


def status_file():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sandbox-ai" / "hosts-status.json"


def load_statuses():
    try:
        fd = os.open(status_file(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            data = json.loads(stream.read(LIMIT).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return {}
    return {name: value for name, value in data.items() if isinstance(value, dict)} if isinstance(data, dict) else {}


def save_statuses(data):
    path = status_file()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".hosts-status-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, indent=1)
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    except OSError:
        pass  # a cache: without it the hosts are asked again next time


def statuses(names, refresh=False, cached=False, path=None, runner=subprocess.run, jobs=8):
    """{name: host_status} of the hosts NAMES, asked at most every STATUS_TTL seconds (REFRESH: now; CACHED: never,
    None for a host not asked yet), several at a time."""
    hosts, kept, now = load(path), load_statuses(), time.time()
    found, todo = {}, []
    for name in names:
        entry, old = hosts.get(name), kept.get(name)
        valid = (entry is not None and isinstance(old, dict) and old.get("ssh") == entry["ssh"]
                 and old.get("port") == entry["port"] and isinstance(old.get("checked"), (int, float)))
        if valid and (cached or (not refresh and 0 <= now - old["checked"] < STATUS_TTL)):
            found[name] = old
        elif cached or entry is None:
            found[name] = None
        else:
            todo.append(name)
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max(1, min(jobs, len(todo)))) as pool:
            for name, result in zip(todo, pool.map(lambda n: host_status(n, path, runner), todo)):
                found[name] = kept[name] = {**result, "ssh": hosts[name]["ssh"], "port": hosts[name]["port"]}
        save_statuses({name: value for name, value in kept.items() if name in hosts})
    return found


def forget_status(name):
    with STATUS_LOCK:
        kept = load_statuses()
        if kept.pop(name, None) is not None:
            save_statuses(kept)


def status_text(info):
    """One short column for hosts list: the version, whether an update is there, and the host's own update mode
    (sandbox-ai update mode there; 0.1.122 on)."""
    if not info:
        return "?"
    version = printable(info.get("version") or "?")
    others = installations_text(info, info.get("version"))
    if not info.get("version"):
        if info.get("missing"):
            return "not installed" + others
        return "unreachable" if info.get("error") else "?"
    mode = f"; updates: {info['update_mode']}" if info.get("update_mode") in UPDATE_MODES else ""
    # (the kind of installation is named when there are several: which one this is)
    kind = f" ({info['kind']})" if info.get("kind") in ("user", "system") and others else ""
    if info.get("kind") in ("checkout", "pip", "package"):
        return f"{version} ({info['kind']}){mode}{others}"
    if info.get("update"):
        return f"{version}{kind}, update: {printable(info.get('available'))}{mode}{others}"
    if info.get("ahead"):
        return f"{version}{kind}, newer than the newest release {printable(info.get('available'))}{mode}{others}"
    if info.get("update") is False:
        return f"{version}{kind}, up to date{mode}{others}"
    return f"{version}{kind}, update unknown{mode}{others}"


UPDATE_MODES = ("off", "notify", "download", "auto")  # updates.py MODES: a host's own update mode


def update_mode(name, mode, at=None, path=None, runner=subprocess.run):
    """Host NAME's own update mode (`sandbox-ai update mode MODE [--at WINDOW] --json` there, as its SSH user: its
    timer runs as that user): {"name", "mode", "error", "status"} - status: what `update mode --json` said there."""
    if mode not in UPDATE_MODES:
        raise ValueError(f"--mode is one of {', '.join(UPDATE_MODES)}")
    if at is not None and (mode != "auto" or not re.fullmatch(r"[0-9]{2}:[0-9]{2}(-[0-9]{2}:[0-9]{2})?", at)):
        raise ValueError("--at HH:MM[-HH:MM] is the window of --mode auto")
    status, out, err = run(name, ["update", "mode", mode, *(["--at", at] if at else []), "--json"], timeout=180,
                           path=path, runner=runner)
    result = {"name": name, "mode": None, "error": None, "status": None}
    try:
        found = json.loads(out) if status == 0 else None
    except (ValueError, RecursionError):  # (deep nesting from the host: damaged too)
        found = None
    if isinstance(found, dict) and found.get("mode") in UPDATE_MODES:
        result.update(mode=found["mode"], status={key: found.get(key) for key in (
            "mode", "window", "kind", "manual", "last_check", "next_check", "timer", "installed")})
        kept = load_statuses()
        if name in kept and isinstance(kept[name], dict):
            kept[name]["update_mode"] = found["mode"]
            save_statuses(kept)
    elif status == 2 and ("Usage: install.sh" in (err or "") or "unknown" in (err or "")):
        result["error"] = (f"{name}: the sandbox-ai there has no update mode yet (0.1.122 or later): update it first "
                           f"(sandbox-ai hosts update {name})")
    else:
        result["error"] = f"{name}: " + failure(status, out, err)
    return result


def update(name, version=None, sudo=None, path=None, runner=subprocess.run, from_here=False, terminal=False,
           installation_=None):
    """Update sandbox-ai on host NAME with its own `sandbox-ai update` (install.sh's signed update), as the same
    kind of installation. SUDO (None: the host's update_sudo): as `sudo -n`, which never asks for a password.
    {"name", "before", "after", "changed", "kind", "installation", "sudo", "error", "needs_sudo", "needs_password",
    "output", "notes", "fixed"}; "needs_password": sudo wants one (update_terminal asks in a terminal);
    "installation": {"kind", "path", "prefix"} of the one updated. FROM_HERE: this machine's copy of sandbox-ai is
    installed there instead (hosts install), as the same kind of installation (the user's own, or for every user in
    its prefix): for a host that cannot reach the release server, or before a release is published. INSTALLATION_:
    "user", "system" (or an installation's data folder) of installations(): that one, run by its own command; None:
    the one the host's commands run (OWN_COMMAND: the user's own first, as a terminal there finds it)."""
    entry = {} if name == LOCAL else load(path).get(name)
    if entry is None:
        raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
    result = {"name": name, "before": None, "after": None, "changed": False, "kind": None, "installation": None,
              "sudo": False, "error": None, "needs_sudo": False, "needs_password": False, "output": "", "notes": [],
              "fixed": []}
    executable = None
    if installation_ is not None:
        if name == LOCAL:
            raise ValueError("this machine updates the installation it runs from: sandbox-ai update")
        found = installations(name, path, runner)
        if found["error"]:
            result["error"] = found["error"]
            return result
        try:
            executable = chosen_installation(found, installation_, name)["executable"]
        except ValueError as exc:
            result["error"] = str(exc)
            return result
    status, out, err, result["fixed"], warnings = run_fixing(name, ["version", "--verbose"], path=path, runner=runner,
                                                             executable=executable)
    result["notes"] += warnings
    if status != 0:
        result["error"] = (f"no sandbox-ai there to update: install it (sandbox-ai hosts install {name})"
                           if not_installed(status, err) else failure(status, out, err))
        return result
    info = installation(out)
    result["before"], result["kind"] = info["version"], info["kind"]
    result["installation"] = {key: info.get(key) for key in ("kind", "path", "prefix")}
    why = not_updatable(name, info)
    if why:
        result["error"] = why
        return result
    if from_here:
        if name == LOCAL:
            raise ValueError("--from-here is for another host (this machine updates with: sandbox-ai update)")
        if version is not None:
            raise ValueError("--from-here installs this machine's version; --version is for the signed release")
        if info["kind"] in ("pip", "package"):
            what = "a pip installation" if info["kind"] == "pip" else f"a package ({printable(info.get('package'))})"
            result["error"] = (f"{name}: sandbox-ai there is {what}, which --from-here does not replace (it installs "
                               f"with install.sh): its own signed update is sandbox-ai hosts update {name}; to move "
                               f"it to install.sh's installation: sandbox-ai hosts install {name}")
            return result
        mode = "system" if info["kind"] == "system" else "user"
        done = install(name, mode, info.get("prefix") if mode == "system" else None,
                       (bool(entry.get("update_sudo")) or None) if sudo is None else sudo, "here", None, path, runner,
                       terminal=terminal)
        result.update(sudo=done.get("sudo", False), error=done.get("error"), output=done.get("output", ""),
                      needs_password=done.get("needs_password", False), after=done.get("after"),
                      changed=bool(done.get("after")) and done.get("after") != result["before"],
                      from_here=True, mode=mode, prefix=done.get("prefix") or (info.get("prefix") if mode == "system" else None))
        result["notes"] += ["from this machine's copy (hosts install --from-here)", *done.get("notes", [])]
        return result
    args = update_args(info, version)
    use_sudo = bool(entry.get("update_sudo")) if sudo is None else bool(sudo)
    if use_sudo and str(entry.get("ssh", "")).startswith("root@"):
        use_sudo = False
        result["notes"].append("without sudo: the SSH user is root")
    if use_sudo and info["kind"] == "user":
        use_sudo = False
        result["notes"].append("without sudo: the installation there is the user's own (as root it would become "
                               "root's)")
    result["sudo"] = use_sudo
    status, out, err = run(name, args, timeout=UPDATE_TIMEOUT, path=path, runner=runner,
                           sudo="batch" if use_sudo else None, executable=executable)
    said = f"{err}\n{out}"
    result["output"] = printable(said.strip()[-3000:])
    if status != 0:
        if use_sudo and any(text in said for text in SUDO_PASSWORD):
            result["needs_password"] = True
            result["error"] = (f"sudo on {name} wants a password, and nothing here asks for one: update it in a "
                               f"terminal (sandbox-ai hosts update {name} --sudo, there), or allow it without one "
                               "(sudoers NOPASSWD for sandbox-ai's update)")
        elif use_sudo and any(text in said for text in SUDO_REFUSED):
            result["error"] = f"{name}: this user may not use sudo there: {printable(said.strip()[-300:])}"
        elif info["kind"] == "system" and not use_sudo and ("needs root" in said or "Permission denied" in said):
            result["needs_sudo"] = True
            result["error"] = (f"the installation in {printable(info['prefix'])}/lib/sandbox-ai on {name} belongs to "
                               f"root: update it with --sudo (or for good: sandbox-ai hosts set {name} --update-sudo "
                               "on)")
        elif OLD_LAYOUT_REFUSAL.search(said):
            result["error"] = f"{failure(status, out, err)}; {old_layout_hint(name, result['before'])}"
        else:
            result["error"] = failure(status, out, err)
            if "no published release" in said:
                # Nothing to download yet (or the host cannot reach the release server): this machine's copy.
                result["error"] += (f"; update it with this machine's copy instead: sandbox-ai hosts update {name} "
                                    "--from-here")
        return result
    return finish_update(name, result, path, runner, newest=version is None, executable=executable)


def finish_update(name, result, path=None, runner=subprocess.run, newest=False, executable=None):
    """RESULT with the version host NAME has now ("after", "changed"; EXECUTABLE: of that installation); its status
    is read again and kept (keep_status; NEWEST: the newest release was installed): the Hosts page and hosts list
    show the new version at once."""
    status, out, err = run(name, ["--version"], timeout=30, path=path, runner=runner, executable=executable)
    found = VERSION.search(out) if status == 0 else None
    result["after"] = found.group(0) if found else None
    result["changed"] = bool(result["after"]) and result["after"] != result["before"]
    if status != 0:
        result["error"] = "updated, but the version could not be read afterwards: " + failure(status, out, err)
    refresh_status(name, path, runner, newest)
    return result


def refresh_status(name, path=None, runner=subprocess.run, newest=False):
    """keep_status after a change on host NAME; when that cannot be read, the kept status is forgotten (asked anew
    by the next hosts list) - never left as it was before the change."""
    try:
        if keep_status(name, path, runner, newest) is not None:
            return
    except Exception as exc:  # noqa: BLE001 - the change stands; its status is asked anew next time
        LOG.debug("hosts", f"{name}: its status was not read again: {exc}")
    forget_status(name)


def update_terminal(name, version=None, path=None, runner=subprocess.run, installation_=None):
    """argv of the update of host NAME in a terminal, where sudo may ask for the password (ssh -tt ... sudo);
    INSTALLATION_: as update's."""
    executable = None
    if installation_ is not None:
        found = installations(name, path, runner)
        if found["error"]:
            raise ValueError(found["error"])
        executable = chosen_installation(found, installation_, name)["executable"]
    status, out, err = run(name, ["version", "--verbose"], timeout=30, path=path, runner=runner,
                           executable=executable)
    if status != 0:
        raise ValueError(failure(status, out, err))
    info = installation(out)
    why = not_updatable(name, info)
    if why:
        raise ValueError(why)
    return argv(name, update_args(info, version), tty=True, path=path,
                sudo="prompt" if info["kind"] != "user" else None, executable=executable)


# --- installing sandbox-ai on a host (hosts install) ------------------------------------------------------------
#
# The host needs sshd, bash, tar, gzip and python3; sandbox-ai itself not. This machine's own sandbox-ai is copied
# there (SOURCE "here": the tree this command runs from, so the host gets exactly this version and needs no access
# to the release server), or only its install.sh with the release keys (SOURCE "download": the host fetches the
# signed release itself). Both are unpacked into a private temporary folder there and installed with install.sh:
# for the SSH user (MODE "user": ~/.local/share/sandbox-ai), or for every user (MODE "system": PREFIX/lib/sandbox-ai,
# root's: as root over SSH, or with sudo: `sudo -n`, which never asks; when sudo wants a password, a terminal asks).

INSTALL_TIMEOUT = 900
SKIPPED = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "node_modules", "build", "dist"}
SKIPPED_FILES = {"uv.lock", "pytest.txt", "tests.txt"}


def tree_root():
    """The sandbox-ai tree this command runs from (the folder holding install.sh and sandbox/)."""
    return Path(__file__).resolve().parent.parent


# Never sent to a host, whatever lists them: logins, keys and tokens a checkout may hold beside its files.
CREDENTIAL_FILES = re.compile(r"(?:^|/)(?:\.credentials\.json|credentials(?:\.\w+)?|auth\.json|\.netrc|\.git-credentials|"
                              r"\.env(?:\.[\w.-]+)?|\.pypirc|\.npmrc|id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?(?:\.pub)?|"
                              r"[^/]*\.(?:pem|key|p12|pfx|kdbx|age|gpg|asc)|"
                              r"[^/]*(?:secret|token|password)s?(?:\.(?:json|txt|env|ya?ml|toml|ini))?)$",
                              re.IGNORECASE)
NEVER_SENT = (".claude/", ".git/", ".sandbox-ai/")


def git_files(folder, runner=subprocess.run):
    """The files of the git checkout FOLDER as install.sh's cmd_copy takes them (tracked and new ones that are not
    ignored: git ls-files --cached --others --exclude-standard; deleted ones are gone), relative; None when FOLDER is
    no checkout (or git is missing)."""
    try:
        done = runner(["git", "-C", str(folder), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                      capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return [name for name in done.stdout.split("\0") if name]


def bundle_files(root, runner=subprocess.run):
    """[relative path] of the files of the sandbox-ai tree ROOT a host gets (hosts install --from here). A git
    checkout: what install.sh's cmd_copy copies (git_files; the app in projects/sandbox-ai-app, a repository of its
    own, likewise; the MCP catalog with its signatures), never .claude/ or .git/; else the tree as it is installed,
    without caches, VCS data and build output (SKIPPED). Never a file that looks like a login, key or token
    (CREDENTIAL_FILES), nor a link or anything else that is no regular file."""
    root = Path(root)
    names = git_files(root, runner) if (root / ".git").exists() else None
    if names is None:
        names = []
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIPPED and d != ".claude" and not d.endswith(".egg-info"))
            names += [os.path.relpath(os.path.join(folder, f), root) for f in files]
    app = Path("projects") / "sandbox-ai-app"
    if (root / app / ".git").exists():
        listed = git_files(root / app, runner)
        if listed is not None:
            names = [n for n in names if not n.startswith(f"{app}/")] + [f"{app}/{n}" for n in listed]
    catalog = Path("projects") / "sandbox-ai-mcp"
    names += [str(catalog / n) for n in ("catalog.json", "catalog.json.sig", "rootfs.json", "rootfs.json.sig")]
    kept = set()
    for name in names:
        parts = Path(name).parts
        if not parts or ".." in parts or name.startswith(NEVER_SENT) or any(f"/{d}" in f"/{name}" for d in NEVER_SENT):
            continue
        if any(part in SKIPPED for part in parts) or parts[-1] in SKIPPED_FILES or parts[-1].endswith(".pyc"):
            continue
        if CREDENTIAL_FILES.search(name):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue  # deleted, a link (cmd_copy skips those too) or no regular file
        kept.add(name)
    return sorted(kept)


def install_bundle(source="here", root=None, runner=subprocess.run):
    """The gzipped tar sent to the host: the tree's files (SOURCE "here": bundle_files, so that a checkout sends
    what install.sh would install from it and never untracked logins, logs or settings), or only install.sh and the
    release keys (SOURCE "download")."""
    import io
    import tarfile
    root = Path(root or tree_root())
    if not (root / "install.sh").is_file() or not (root / "sandbox" / "agent_sandbox.py").is_file():
        raise ValueError(f"{printable(str(root))} is no sandbox-ai tree with install.sh: install from a release "
                         "on that host instead (--download)")

    def keep(info):
        if not (info.isfile() or info.isdir()):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if source == "here":
            for name in bundle_files(root, runner):
                archive.add(root / name, arcname=name, filter=keep, recursive=False)
        else:
            archive.add(root / "install.sh", arcname="install.sh", filter=keep)
            if (root / "sandbox" / "release-signers").is_file():
                archive.add(root / "sandbox" / "release-signers", arcname="sandbox/release-signers", filter=keep)
    return buffer.getvalue()


PROBE = ('printf "uid=%s\\n" "$(id -u)"; for tool in bash tar gzip python3; do command -v "$tool" >/dev/null 2>&1 '
         '|| printf "missing=%s\\n" "$tool"; done; if [ "$(id -u)" != 0 ]; then if ! command -v sudo >/dev/null 2>&1; '
         'then echo sudo=none; elif sudo -n true 2>/dev/null; then echo sudo=ok; else echo sudo=password; fi; fi; '
         'v=$({ [ -x "$HOME/.local/bin/sandbox-ai" ] && "$HOME/.local/bin/sandbox-ai" --version; } 2>/dev/null || '
         '{ command -v sandbox-ai >/dev/null 2>&1 && sandbox-ai --version; } 2>/dev/null); printf "installed=%s\\n" "$v"')


def prefix_paths(prefix):
    """The folders a system installation under PREFIX depends on, from / down: every one above PREFIX, PREFIX, its
    bin, lib and lib/sandbox-ai. Whoever may change one of them may change what is installed for every user."""
    parts = [part for part in prefix.split("/") if part]
    above = ["/"] + ["/" + "/".join(parts[:index]) for index in range(1, len(parts))]
    own = "/" + "/".join(parts) if parts else "/"
    return [(path, "above") for path in above if path != own] + [
        (own, ""), (own.rstrip("/") + "/bin", "/bin"), (own.rstrip("/") + "/lib", "/lib"),
        (own.rstrip("/") + "/lib/sandbox-ai", "/lib/sandbox-ai")]


def prefix_problem(prefix, facts):
    """Why the folders of a system installation under PREFIX are no safe place (FACTS: what probe_script reported,
    with "uid", the SSH user's), or None: each must belong to root or the SSH user, and none may be writable by a
    group or by others (a folder above PREFIX may be, with the sticky bit, as /tmp: nobody can replace another's
    entry there)."""
    for index, (path, what) in enumerate(prefix_paths(prefix)):
        value = facts.get(f"folder{index}")
        if value is None:
            continue
        perms, _, owner = value.strip().partition(" ")
        if owner not in ("0", facts.get("uid")):
            return f"{path} belongs to the user {owner}, neither root nor the SSH user"
        sticky = perms[9:10] in ("t", "T")
        if len(perms) >= 10 and (perms[5] == "w" or perms[8] == "w") and not (what == "above" and sticky):
            return f"{path} ({perms}) may be changed by {'others' if perms[8] == 'w' else 'its group'}"
    return None


def probe_script(mode=None, prefix=None):
    """PROBE for an installation in MODE (system: under PREFIX, default /usr/local; None: not decided yet, system
    when the SSH user is root): the mode and owner of every folder prefix_paths names are reported (folderN=PERMS
    UID, by ls -ndL; nothing for one that does not exist), and for system the version is asked of
    PREFIX/bin/sandbox-ai first (the copy that installation replaces, not another one on PATH)."""
    if mode == "user":
        return PROBE
    if prefix is not None and (not re.fullmatch(r"/[A-Za-z0-9._/-]{0,200}", prefix) or ".." in prefix):
        raise ValueError("--prefix is an absolute path of letters, digits, . _ - and /")
    base = shlex.quote(prefix or "/usr/local")
    owners = "; ".join(f'if [ -e {shlex.quote(path)} ]; then printf "folder{index}=%s\\n" "$(ls -ndL '
                       f"{shlex.quote(path)} | awk '{{print $1, $3}}')\"; fi"
                       for index, (path, _) in enumerate(prefix_paths(prefix or "/usr/local")))
    first = (f'w=$({base}/bin/sandbox-ai --version 2>/dev/null); if [ -n "$w" ]; then printf "installed=%s\\n" '
             '"$w"; fi')
    return f"{owners}; {first + '; ' if mode == 'system' else ''}{PROBE}"


def remote_shell(entry, script, input=None, timeout=60, runner=subprocess.run, tty=False):
    """(status, stdout bytes, stderr text) of SCRIPT run by /bin/sh on host ENTRY (neutral_command: whatever the
    user's login shell is, fish or csh too; no sandbox-ai needed); INPUT: bytes on its standard input."""
    command = [*ssh_base(entry, tty), neutral_command(script)]
    name, begun = entry.get("ssh") if isinstance(entry, dict) else "?", time.monotonic()
    what = f"a shell script ({len(script)} characters)"
    if tty:
        status = runner(command).returncode
        ran(name, what, begun, status, "")
        return status, b"", ""
    try:
        done = runner(command, input=input if input is not None else b"", capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        ran(name, what, begun, 124, f"no answer within {timeout} s")
        return 124, b"", f"no answer within {timeout} s"
    except OSError as exc:
        ran(name, what, begun, 127, f"{command[0]}: {exc.strerror}")
        return 127, b"", f"{command[0]}: {exc.strerror}"
    err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
    out = done.stdout if isinstance(done.stdout, bytes) else (done.stdout or "").encode()
    ran(name, what, begun, done.returncode, err)
    return done.returncode, out, err


def install_args(mode, prefix=None, source="here", version=None, folder="$d"):
    """install.sh's arguments on the host (FOLDER: where the bundle lies there, a shell word)."""
    if mode not in ("user", "system"):
        raise ValueError("the mode is user or system")
    if prefix is not None and not re.fullmatch(r"/[A-Za-z0-9._/-]{0,200}", prefix) or prefix and ".." in prefix:
        raise ValueError("--prefix is an absolute path of letters, digits, . _ - and /")
    if version is not None and not VERSION.fullmatch(version):
        raise ValueError("--version expects X.Y.Z")
    # No PySide6 download on a host: the app runs where you are (it reaches the hosts over SSH).
    words = (["--user"] if mode == "user" else ["--system", "--prefix", prefix or "/usr/local"]) + ["--no-app-deps"]
    words += ["--version", version] if version else []
    quoted = " ".join(shlex.quote(word) for word in words)
    return (f'--from "{folder}" ' if source == "here" else "") + quoted


def installer_script(folder, args, sudo=None):
    """The script that runs install.sh from FOLDER on the host and removes the folder afterwards."""
    prefix = SUDO[sudo] if sudo else ""
    return (f"d={shlex.quote(folder)}; case $d in /*/sandbox-ai-install.*) ;; *) exit 2 ;; esac; "
            f"trap 'rm -rf -- \"$d\"' EXIT; {prefix}bash \"$d/install.sh\" {args}")


def cleanup_script(folder, sudo=False):
    """The script that removes the bundle's FOLDER on the host (only a sandbox-ai-install.* folder), as the SSH user,
    and with SUDO by `sudo -n` too for what is left (root's files)."""
    return (f"d={shlex.quote(folder)}; case $d in /*/sandbox-ai-install.*) ;; *) exit 2 ;; esac; "
            f"rm -rf -- \"$d\" 2>/dev/null" + ("; [ -e \"$d\" ] && sudo -n rm -rf -- \"$d\" 2>/dev/null; true"
                                              if sudo else "; true"))


def install(name, mode=None, prefix=None, sudo=None, source="here", version=None, path=None,
            runner=subprocess.run, terminal=False, dry_run=False):
    """Install sandbox-ai on host NAME (see above). MODE None: "system" when the SSH user is root, else "user" (the
    host's `id -u` decides, never the name in USER@HOST). SUDO None: with sudo when a system installation needs it;
    False: never (refused when it would be needed). A system PREFIX (or its bin/, lib/) that belongs to a user other
    than root and the SSH user is refused: that user could change what root installs there. TERMINAL: when sudo
    wants a password, ask for it in this terminal. DRY_RUN: only decide, and say what would be done ("planned":
    True; nothing is copied or installed). {"name", "before", "after", "changed", "mode", "prefix", "sudo",
    "root", "source", "planned", "error", "needs_password", "output", "notes"}."""
    if name == LOCAL:
        raise ValueError("this machine installs with install.sh itself (see sandbox-ai update)")
    if source not in ("here", "download"):
        raise ValueError("the source is here or download")
    entry = load(path).get(name)
    if entry is None:
        raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
    result = {"name": name, "before": None, "after": None, "changed": False, "mode": mode, "prefix": prefix,
              "sudo": False, "root": False, "source": source, "planned": False, "error": None,
              "needs_password": False, "output": "", "notes": []}
    try:
        probe = probe_script(mode, prefix)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    status, out, err = remote_shell(entry, probe, timeout=CONNECT_TIMEOUT + 30, runner=runner)
    if status != 0:
        result["error"] = failure(status, "", err)
        return result
    facts = {}
    for line in out.decode("utf-8", "replace").splitlines():  # the first of a key wins (installed= of the prefix)
        if "=" in line:
            facts.setdefault(*line.split("=", 1))
    missing = [line.split("=", 1)[1] for line in out.decode("utf-8", "replace").splitlines()
               if line.startswith("missing=")]
    if missing:
        result["error"] = f"{name} lacks {', '.join(printable(m) for m in missing)}: install them there first"
        return result
    result["root"] = facts.get("uid") == "0"
    found = VERSION.search(facts.get("installed", ""))
    result["before"] = found.group(0) if found else None
    mode = mode or ("system" if result["root"] else "user")
    result["mode"], result["prefix"] = mode, (prefix or "/usr/local") if mode == "system" else None
    if mode == "user" and result["root"]:
        result["notes"].append("for root (the SSH user): ~/.local of root")
    use_sudo = mode == "system" and not result["root"]
    if use_sudo and sudo is False:
        result["error"] = (f"a system installation on {name} needs root: the SSH user is no root; allow sudo "
                           "(--sudo) or install for the user (--user)")
        return result
    if use_sudo and facts.get("sudo") == "none":
        result["error"] = f"{name} has no sudo, and the SSH user is no root: install for the user (--user)"
        return result
    if mode == "system":
        problem = prefix_problem(result["prefix"], facts)
        if problem:
            result["error"] = (f"{printable(problem)} on {name}: that user could change what is installed there for "
                               "every user; choose another --prefix, or install for the user (--user)")
            return result
    result["sudo"] = use_sudo
    if dry_run:
        result["planned"] = True
        how = ("for every user in " + result["prefix"] if mode == "system" else "for the SSH user") + \
            (" with sudo" + (" (it asks for a password)" if facts.get("sudo") == "password" else "")
             if use_sudo else " as root" if result["root"] else "")
        result["notes"].append(f"would install {how}; nothing was copied")
        return result
    try:
        bundle = install_bundle(source)
    except (OSError, ValueError) as exc:
        result["error"] = printable(str(exc))
        return result
    upload = ('umask 077 && d=$(mktemp -d "${TMPDIR:-/tmp}/sandbox-ai-install.XXXXXX") && tar -xzf - -C "$d" '
              '&& printf "%s\\n" "$d"')
    status, out, err = remote_shell(entry, upload, input=bundle, timeout=INSTALL_TIMEOUT, runner=runner)
    folder = out.decode("utf-8", "replace").strip().splitlines()[-1:] if status == 0 else []
    if status != 0 or not folder or not re.fullmatch(r"/[^\s'\"\\$`]*/sandbox-ai-install\.[A-Za-z0-9]+", folder[0]):
        result["error"] = "the copy to the host failed: " + failure(status, "", err)
        return result
    args = install_args(mode, prefix, source, version)
    if use_sudo and facts.get("sudo") == "password":
        if not terminal:
            result["needs_password"] = True
            result["error"] = (f"sudo on {name} wants a password: install it in a terminal (sandbox-ai hosts install "
                               f"{name} --system there), or allow sudo without one")
            remote_shell(entry, cleanup_script(folder[0], False), timeout=30, runner=runner)
            return result
        status, _, _ = remote_shell(entry, installer_script(folder[0], args, "prompt"), runner=runner, tty=True)
        out, err = b"", ""
    else:
        status, out, err = remote_shell(entry, installer_script(folder[0], args, "batch" if use_sudo else None),
                                        timeout=INSTALL_TIMEOUT, runner=runner)
    said = f"{err}\n{out.decode('utf-8', 'replace')}"
    result["output"] = printable(said.strip()[-3000:])
    if status != 0:
        # The script's trap removes the folder; not when sudo or the connection failed on the way, or root left
        # files in it: once more, as the SSH user (and with sudo -n for what root made there).
        remote_shell(entry, cleanup_script(folder[0], use_sudo), timeout=30, runner=runner)
        if use_sudo and any(text in said for text in SUDO_REFUSED):
            result["error"] = f"{name}: this user may not use sudo there: {printable(said.strip()[-300:])}"
        else:
            result["error"] = f"install.sh on {name} ended with exit status {status}: {printable(said.strip()[-400:])}"
        return result
    forget_status(name)  # (read again below, when the version answers)
    if mode == "system":  # the copy just installed, not another one on PATH
        status, raw, err = remote_shell(entry, f"{shlex.quote(result['prefix'])}/bin/sandbox-ai --version", timeout=30,
                                        runner=runner)
        out = raw.decode("utf-8", "replace")
    else:
        status, out, err = run(name, ["--version"], timeout=30, path=path, runner=runner) \
            if runner is subprocess.run else (0, "", "")
    found = VERSION.search(out) if status == 0 else None
    result["after"] = found.group(0) if found else None
    result["changed"] = bool(result["after"]) and result["after"] != result["before"]
    if status != 0:
        result["notes"].append("installed, but sandbox-ai there does not answer yet: " + failure(status, out, err))
    elif runner is subprocess.run:
        refresh_status(name, path, runner, newest=source == "download" and version is None)
    return result


def install_lines(result):
    """What hosts install prints for one host."""
    name = result["name"]
    if result.get("error"):
        lines = [f"{name}: not installed: {printable(result['error'])}"]
    elif result.get("planned"):
        lines = [f"{name}: nothing done (--dry-run)"]
    else:
        how = ("for every user in " + printable(result.get("prefix") or "/usr/local") if result.get("mode") == "system"
               else "for the user") + (" with sudo" if result.get("sudo") else " as root" if result.get("root") else "")
        lines = [f"{name}: sandbox-ai {printable(result.get('after') or '?')} installed {how}"
                 + (f" (was {printable(result['before'])})" if result.get("before") else "")]
    return lines + [f"  note: {printable(note)}" for note in result.get("notes") or []]


def update_lines(result):
    """What hosts update prints for one host."""
    name = result["name"]
    lines = [f"  key: {fixed_text(change)}" for change in result.get("fixed") or []]
    if result.get("error") and not result.get("after"):
        lines.insert(0, f"{name}: not updated: {printable(result['error'])}")
    elif result.get("changed"):
        lines.insert(0, f"{name}: {printable(result['before'] or '?')} -> {printable(result['after'])}"
                        + (" (sudo)" if result.get("sudo") else ""))
    elif result.get("from_here") and result.get("after"):
        # The files were replaced with this machine's copy, of the same version: not "up to date" as it was.
        lines.insert(0, f"{name}: reinstalled from this machine's copy ({printable(result['after'])})"
                        + (" (sudo)" if result.get("sudo") else "")
                        + (f" ({printable(result['error'])})" if result.get("error") else ""))
    else:
        lines.insert(0, f"{name}: {printable(result.get('after') or result.get('before') or '?')}, up to date"
                        + (f" ({printable(result['error'])})" if result.get("error") else ""))
    where = result.get("installation") or {}
    if where.get("kind") in ("user", "system") and (result.get("after") or result.get("changed")):
        # which one (a host may have the user's own and a system one: user report 2026-11-23)
        lines.insert(1, f"  installation: {'the user' + chr(39) + 's own' if where['kind'] == 'user' else 'system'}"
                        f" ({printable(where.get('path') or '?')})")
    lines += [f"  note: {printable(note)}" for note in result.get("notes") or []]
    return lines


def update_many(names, version=None, sudo=None, jobs=1, path=None, runner=subprocess.run, done=None,
                from_here=False, installation_=None):
    """[update result] for the hosts NAMES in their order, JOBS at a time; DONE(result) as each one ends.
    INSTALLATION_: as update's; "all": each install.sh installation of a host, one result each."""
    def one(name, which=installation_):
        try:
            result = update(name, version, sudo, path, runner, from_here=from_here,
                            **({"installation_": which} if which is not None else {}))
        except (OSError, ValueError) as exc:
            result = {"name": name, "before": None, "after": None, "changed": False, "error": printable(exc),
                      "needs_password": False, "needs_sudo": False, "notes": [], "fixed": []}
        if done:
            done(result)
        return result

    def every(name):
        try:
            found = installations(name, path, runner)
        except (OSError, ValueError) as exc:
            found = {"error": printable(exc), "installations": []}
        folders = [item["data"] for item in found["installations"] if item.get("executable")]
        if found["error"] or not folders:
            result = {"name": name, "before": None, "after": None, "changed": False, "notes": [], "fixed": [],
                      "error": found["error"] or "no install.sh installation there", "needs_password": False,
                      "needs_sudo": False}
            if done:
                done(result)
            return [result]
        return [one(name, folder) for folder in folders]
    if installation_ == "all":
        return [result for name in names for result in every(name)]
    if jobs <= 1 or len(names) <= 1:
        return [one(name) for name in names]
    with concurrent.futures.ThreadPoolExecutor(min(jobs, len(names))) as pool:
        return list(pool.map(one, names))


# --- removing sandbox-ai on a host (hosts uninstall) -----------------------------------------------------------
#
# The host's own `sandbox-ai uninstall` of the installation chosen, run by that installation's own command (so the
# one removed is exactly that one, whichever another command would find): the user's own (its links, folder, PATH
# block; afterwards a system one is what runs there) or a system one (root's: as root over SSH, or `sudo -n`; when
# sudo wants a password, a terminal asks). --purge, --secrets, --mcp-data and --stop-running are its own options;
# --purge removes the data of whoever runs it, so never under sudo (that would be root's). The host stays in the
# list unless --remove-from-list.

def uninstall_args(purge=False, secrets=False, mcp_data=False, stop_running=False, dry_run=False):
    if (secrets or mcp_data) and not purge:
        raise ValueError(f"{'--secrets' if secrets else '--mcp-data'} goes with --purge")
    return ["uninstall", "--dry-run" if dry_run else "--yes", *(["--purge"] if purge else []),
            *(["--secrets"] if secrets else []), *(["--mcp-data"] if mcp_data else []),
            *(["--stop-running"] if stop_running and not dry_run else [])]


def uninstall_target(name, installation_=None, path=None, runner=subprocess.run):
    """(the installation of host NAME to remove, installations()): INSTALLATION_ "user", "system" or a data folder;
    None: the only install.sh installation there. ValueError: none, or several and none chosen (each named, with
    the option that chooses it)."""
    if name == LOCAL:
        raise ValueError("this machine's sandbox-ai is the one the app runs from: remove it in a terminal here with "
                         "sandbox-ai uninstall (--purge: also its data)")
    if load(path).get(name) is None:
        raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
    found = installations(name, path, runner)
    if found["error"]:
        raise ValueError(f"{name}: {found['error']}")
    if installation_ is not None:
        return chosen_installation(found, installation_, name), found
    items = [item for item in found["installations"] if item.get("executable")]
    if len(items) == 1:
        return items[0], found
    others = [item for item in found["installations"] if not item.get("executable")]
    if not items:
        there = "; ".join(f"{printable(item.get('kind'))} {printable(item.get('version') or '?')} in "
                          f"{printable(item.get('path') or '?')}" for item in others)
        raise ValueError(f"{name} has no install.sh installation of sandbox-ai" + (
            f" (there: {there}: remove that one there as it was installed, e.g. pip uninstall sandbox-ai-app)"
            if there else ""))
    raise ValueError(f"{name} has {len(items)} installations of sandbox-ai; choose one: " + ", or ".join(
        f"--installation {item['kind']} ({printable(item.get('version') or '?')} in "
        f"{printable(item.get('path') or '?')})" for item in items))


def uninstall(name, installation_=None, purge=False, secrets=False, mcp_data=False, stop_running=False, sudo=None,
              dry_run=False, remove=False, path=None, runner=subprocess.run, terminal=False):
    """Remove sandbox-ai on host NAME: the installation chosen (uninstall_target), by its own `sandbox-ai uninstall
    --yes` there. SUDO None: with `sudo -n` for a system installation when the SSH user is no root; False: never.
    DRY_RUN: only its table of what would go ("plan"). REMOVE: the host leaves the list afterwards. TERMINAL: when
    sudo wants a password, ask in this terminal. {"name", "removed", "sudo", "dry_run", "plan", "output", "error",
    "needs_password", "removed_from_list", "remaining": the installations left there, "notes"}."""
    args = uninstall_args(purge, secrets, mcp_data, stop_running, dry_run)
    item, found = uninstall_target(name, installation_, path, runner)
    entry = load(path)[name]
    where = {key: item.get(key) for key in ("kind", "version", "path", "prefix", "data")}
    result = {"name": name, "removed": None, "installation": where, "sudo": False, "dry_run": dry_run, "plan": "",
              "output": "", "error": None, "needs_password": False, "removed_from_list": False, "remaining": None,
              "notes": []}
    root = found.get("uid") == 0 or str(entry.get("ssh", "")).startswith("root@")
    use_sudo = item["kind"] == "system" and not root and sudo is not False
    if item["kind"] == "user" and sudo:
        result["notes"].append("without sudo: the installation there is the user's own")
    if purge and use_sudo:
        result["error"] = (f"--purge removes the data of whoever runs the uninstall - with sudo that is root, not "
                           f"the SSH user: remove the system installation on {name} without --purge (the user's data "
                           f"stays; that user's own sandbox-ai uninstall --purge removes it)")
        return result
    if item["kind"] == "system" and not root and sudo is False:
        result["error"] = (f"the system installation on {name} ({printable(item.get('path') or '?')}) is root's: "
                           "remove it with sudo (without --no-sudo)")
        return result
    result["sudo"] = use_sudo
    if dry_run:
        # (the list only reads: as the SSH user first - an older sandbox-ai there refuses a system one's list
        # to anyone but root, then with sudo -n)
        status, out, err = run(name, args, timeout=180, path=path, runner=runner, executable=item["executable"])
        if status != 0 and use_sudo:
            status, out, err = run(name, args, timeout=180, path=path, runner=runner, sudo="batch",
                                   executable=item["executable"])
        result["plan"] = printable(out.strip()[-6000:])
        if status != 0:
            result["error"] = f"{name}: " + failure(status, out, err)
        return result
    LOG.info("hosts", f"uninstall on {name}: the {item['kind']} installation in {item.get('data')}"
                      + (" and the SSH user's data" if purge else "") + (" with sudo" if use_sudo else ""))
    status, out, err = run(name, args, timeout=UPDATE_TIMEOUT, path=path, runner=runner,
                           sudo="batch" if use_sudo else None, executable=item["executable"])
    said = f"{err}\n{out}"
    result["output"] = printable(said.strip()[-3000:])
    if status != 0 and use_sudo and any(text in said for text in SUDO_PASSWORD):
        if not terminal:
            result["needs_password"] = True
            result["error"] = (f"sudo on {name} wants a password, and nothing here asks for one: remove it in a "
                               f"terminal (sandbox-ai hosts uninstall {name} there), or allow sudo without one")
            return result
        status = runner(argv(name, args, tty=True, path=path, sudo="prompt", executable=item["executable"])).returncode
        said = ""
    if status != 0:
        if use_sudo and any(text in said for text in SUDO_REFUSED):
            result["error"] = f"{name}: this user may not use sudo there: {printable(said.strip()[-300:])}"
        else:
            result["error"] = f"{name}: " + (failure(status, out, err) if said else f"exit status {status}")
        refresh_status(name, path, runner)
        return result
    result["removed"] = where
    if remove:
        remove_host(name, lambda line: result["notes"].append(line))
        result["removed_from_list"] = True
        return result
    try:
        kept = keep_status(name, path, runner)
    except Exception as exc:  # noqa: BLE001 - removed all the same; its status is asked anew next time
        LOG.debug("hosts", f"{name}: its status was not read again: {exc}")
        forget_status(name)
        kept = None
    if kept is not None and kept.get("installations") is not None:
        result["remaining"] = [{key: item.get(key) for key in ("kind", "version", "path")}
                               for item in kept["installations"]]
    return result


def uninstall_terminal(name, installation_=None, purge=False, secrets=False, mcp_data=False, stop_running=False,
                       path=None, runner=subprocess.run):
    """argv of the uninstall of host NAME in a terminal, where sudo may ask for the password (ssh -tt ... sudo)."""
    args = uninstall_args(purge, secrets, mcp_data, stop_running)
    item, found = uninstall_target(name, installation_, path, runner)
    root = found.get("uid") == 0 or str(load(path)[name].get("ssh", "")).startswith("root@")
    use_sudo = item["kind"] == "system" and not root
    if purge and use_sudo:
        raise ValueError("--purge removes the data of whoever runs it - with sudo that is root: without --purge")
    return argv(name, args, tty=True, path=path, sudo="prompt" if use_sudo else None, executable=item["executable"])


def uninstall_lines(result):
    """What hosts uninstall prints for one host."""
    name, where = result["name"], result.get("installation") or {}
    what = (f"{'the user' + chr(39) + 's own' if where.get('kind') == 'user' else 'the system'} installation "
            f"{printable(where.get('version') or '?')} ({printable(where.get('path') or '?')})")
    if result.get("error"):
        lines = [f"{name}: not removed: {printable(result['error'])}"]
    elif result.get("dry_run"):
        lines = [f"{name}: {what} - what its uninstall would remove there:", result.get("plan") or ""]
    else:
        lines = [f"{name}: removed {what}" + (" (sudo)" if result.get("sudo") else "")]
        if result.get("removed_from_list"):
            lines.append(f"  {name} is no longer in the list")
        elif result.get("remaining") is not None:
            left = ", ".join(f"{printable(item.get('kind'))} {printable(item.get('version') or '?')}"
                             for item in result["remaining"])
            lines.append(f"  there now: {left}" if left else f"  no sandbox-ai is left there (install it again: "
                                                             f"sandbox-ai hosts install {name})")
    return lines + [f"  note: {printable(note)}" for note in result.get("notes") or []]


def remove_host(name, out=print):
    """Host NAME out of the list (hosts remove): its shared LLM servers first, its kept status, its connection."""
    if name not in load():
        raise ValueError(f"no host {printable(name)}")
    try:  # a shared LLM server is for the hosts of the list: its copy (and the key sent) leaves this one first
        shares = llm_shares()
        if shares is not None:
            shares.host_leaving(name, out)
    except Exception as exc:  # noqa: BLE001 - the host is removed all the same
        out(f"note: shared LLM servers: {printable(exc)}")
    with hosts_file_lock():
        hosts = load()
        if name not in hosts:
            raise ValueError(f"no host {printable(name)}")
        removed = hosts.pop(name)
        save(hosts)
    forget_status(name)
    stop_master(removed)  # its shared connection would otherwise serve commands for up to CONTROL_PERSIST s


def summary(results):
    updated = sum(1 for r in results if r.get("changed"))
    failed = sum(1 for r in results if r.get("error") and not r.get("after"))
    return (f"{len(results)} host{'s' if len(results) != 1 else ''}: {updated} updated, "
            f"{len(results) - updated - failed} up to date, {failed} not updated")


_shares = None


def llm_shares():
    """llm_share.py (the LLM servers shared with every host: sandbox-ai llm share), or None when nothing is shared -
    then nothing is loaded (hosts list stays quick)."""
    global _shares
    if not config_file().with_name("llm-shares.json").exists():
        return None
    if _shares is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_llm_share_hosts", HERE / "llm_share.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _shares = loaded
    return _shares


def shares_pending(json_out=False, out=print):
    """A copy of a shared LLM server a host did not get yet (it did not answer): tried again now when it is due, and
    said (on stderr where the command prints JSON). Never fails the command."""
    try:
        shares = llm_shares()
        if shares is not None:
            shares.pending(out, err=(lambda line: print(line, file=sys.stderr)) if json_out else None)
    except Exception as exc:  # noqa: BLE001 - the hosts' own command goes on
        LOG.warning("hosts", f"shared LLM servers: {printable(exc)}")


def shared_to_new_host(name, out=print):
    """After hosts add NAME: the LLM servers shared with every host (llm share) copied there now."""
    try:
        shares = llm_shares()
        if shares is not None:
            shares.host_added(name, out)
    except Exception as exc:  # noqa: BLE001 - the host is added all the same
        out(f"  note: shared LLM servers: {printable(exc)}")


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def cli(argv_, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai hosts", formatter_class=help_style,
                                     description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the hosts, with their sandbox-ai version and update")
    listing.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    asking = listing.add_mutually_exclusive_group()
    asking.add_argument("--cached", action="store_true", help="ask no host: what was found the last time")
    asking.add_argument("--refresh", action="store_true", help="ask every host now")
    adding = commands.add_parser("add", help="add a host reached over SSH")
    adding.add_argument("name")
    adding.add_argument("ssh", metavar="USER@HOST")
    adding.add_argument("--port", type=int, default=22, help="the SSH port (default 22)")
    adding.add_argument("--identity", metavar="FILE", help="the SSH key to use (default: your SSH agent's)")
    adding.add_argument("--description", default="", help="a note about this host, shown in the list")
    adding.add_argument("--command", dest="remote_command", metavar="PATH",
                        help="sandbox-ai's path there, if not the usual")
    adding.add_argument("--update-sudo", action="store_true",
                        help="its sandbox-ai is a system installation: update it with sudo")
    setting = commands.add_parser("set", help="change a host's settings")
    setting.add_argument("name")
    setting.add_argument("--update-sudo", choices=["on", "off"], required=True,
                         help="whether hosts update runs its update with sudo (a system installation)")
    removing = commands.add_parser("remove", help="forget a host")
    removing.add_argument("name")
    testing = commands.add_parser("test", help="SSH, the host's sandbox-ai and its doctor")
    testing.add_argument("name")
    testing.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    updating = commands.add_parser("update", help="update sandbox-ai on hosts (their own signed update)")
    updating.add_argument("names", nargs="*", metavar="NAME")
    updating.add_argument("--all", action="store_true", help="every host of the list (not this machine)")
    updating.add_argument("--version", dest="release", metavar="X.Y.Z", help="this release instead of the newest")
    updating.add_argument("--sudo", action="store_true", default=None,
                          help="as root with sudo -n (a system installation); a password is asked in a terminal")
    updating.add_argument("--no-sudo", dest="sudo", action="store_false", help="without sudo, whatever the host says")
    updating.add_argument("--jobs", type=int, default=1, metavar="N", help="hosts at a time (1-8, default 1)")
    updating.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    updating.add_argument("--from-here", action="store_true",
                          help="install this machine's copy there instead of the signed release (no release server "
                               "needed; the same kind of installation)")
    updating.add_argument("--mode", choices=UPDATE_MODES,
                          help="set the host's own update mode instead (off, notify, download, auto: sandbox-ai "
                               "update mode there); nothing is installed now")
    updating.add_argument("--at", metavar="HH:MM[-HH:MM]", help="with --mode auto: its window there (local time)")
    updating.add_argument("--installation", choices=["user", "system", "all"],
                          help="which installation there (hosts list --json: installations): the user's own, the "
                               "system one, or all; default: the one the host's commands run (the user's own first, "
                               "as a terminal there finds it)")
    uninstalling = commands.add_parser("uninstall", help="remove sandbox-ai on a host (its own sandbox-ai uninstall)")
    uninstalling.add_argument("name")
    uninstalling.add_argument("--installation", choices=["user", "system"],
                              help="which one, when the host has both: the user's own (a system one is left and "
                                   "runs from then on) or the system one (with sudo)")
    uninstalling.add_argument("--purge", action="store_true",
                              help="also the SSH user's sandbox-ai data there (not with sudo)")
    uninstalling.add_argument("--secrets", action="store_true",
                              help="with --purge: also the secrets and logins there; they cannot be recovered")
    uninstalling.add_argument("--mcp-data", action="store_true",
                              help="with --purge: also the MCP servers' own folders there")
    uninstalling.add_argument("--stop-running", action="store_true",
                              help="stop the starts running there first (else they refuse the uninstall)")
    uninstalling.add_argument("--sudo", action="store_true", default=None,
                              help="with sudo -n for a system installation (the default when the SSH user is no root)")
    uninstalling.add_argument("--no-sudo", dest="sudo", action="store_false", help="never sudo")
    uninstalling.add_argument("--remove-from-list", action="store_true",
                              help="also remove the host from this list afterwards (default: it stays; install "
                                   "again with hosts install)")
    uninstalling.add_argument("--dry-run", action="store_true",
                              help="only show the host's own table of what would go; remove nothing")
    uninstalling.add_argument("-y", "--yes", action="store_true",
                              help="without asking here (needed without a terminal)")
    uninstalling.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    installing = commands.add_parser("install", help="install sandbox-ai on a host (from here, or its signed release)")
    installing.add_argument("name")
    target = installing.add_mutually_exclusive_group()
    target.add_argument("--user", dest="mode", action="store_const", const="user",
                        help="for the SSH user (default unless that is root)")
    target.add_argument("--system", dest="mode", action="store_const", const="system",
                        help="for every user under --prefix (as root, or with sudo)")
    installing.add_argument("--prefix", metavar="DIR", help="the system prefix (default /usr/local)")
    installing.add_argument("--sudo", action="store_true", default=None,
                            help="with sudo for a system installation (the default when the SSH user is no root)")
    installing.add_argument("--no-sudo", dest="sudo", action="store_false", help="never sudo")
    origin = installing.add_mutually_exclusive_group()
    origin.add_argument("--from-here", dest="source", action="store_const", const="here",
                        help="copy this machine's sandbox-ai there (default; the host needs no release server)")
    origin.add_argument("--download", dest="source", action="store_const", const="download",
                        help="the host downloads the signed release itself")
    installing.add_argument("--version", dest="release", metavar="X.Y.Z", help="with --download: this release")
    installing.add_argument("--dry-run", action="store_true",
                            help="only say what would be done (user or system, as root or with sudo)")
    installing.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    running = commands.add_parser("run", help="run sandbox-ai ARGS on a host")
    running.add_argument("name")
    running.add_argument("args", nargs=argparse.REMAINDER)
    a = parser.parse_args(argv_)
    if a.command in ("test", "set") or a.command == "list" and not a.cached:  # (--cached asks no host)
        shares_pending(getattr(a, "json", False), out)
    if a.command == "list":
        found = entries()
        remote = [entry["name"] for entry in found if entry["kind"] == "ssh"]
        info = statuses(remote, refresh=a.refresh, cached=a.cached) if remote else {}
        for entry in found:
            if entry["kind"] == "ssh":
                kept = info.get(entry["name"])
                entry["sandbox_ai"] = {key: value for key, value in kept.items() if key not in ("ssh", "port", "name")} \
                    if kept else None
            else:
                entry["sandbox_ai"] = {"version": printable(module_version())}
        if a.json:
            out(json.dumps(found, indent=2))
            return 0
        for entry in found:
            where = "this machine" if entry["kind"] == "local" else f"ssh {entry['ssh']}:{entry['port']}"
            version = (entry["sandbox_ai"] or {}).get("version") if entry["kind"] == "local" else \
                status_text(entry["sandbox_ai"])
            out(f"{entry['name']:<16} {where:<36} {version:<28} {printable(entry.get('description', ''))}".rstrip())
            for change in (entry.get("sandbox_ai") or {}).get("fixed") or []:
                out(f"  key: {fixed_text(change)}")
        return 0
    if a.command == "install":
        if a.prefix and a.mode != "system":
            raise ValueError("--prefix is for --system")
        if a.release and a.source != "download":
            raise ValueError("--version is for --download (--from-here installs this machine's version)")
        result = install(a.name, a.mode, a.prefix, a.sudo, a.source or "here", a.release,
                         terminal=not a.json and sys.stdin.isatty(), dry_run=a.dry_run)
        out(json.dumps(result, indent=2) if a.json else "\n".join(install_lines(result)))
        return 0 if not result.get("error") else 1
    if a.command == "uninstall":
        return uninstall_cli(a, out)
    if a.command == "update":
        if a.all == bool(a.names):
            raise ValueError("name the hosts to update, or --all (every host of the list)")
        names = sorted(load()) if a.all else list(dict.fromkeys(a.names))
        if not names:
            out("no hosts to update; add one with: sandbox-ai hosts add NAME USER@HOST")
            return 0
        if a.at is not None and a.mode is None:
            raise ValueError("--at is the window of --mode auto")
        if a.mode is not None:
            if a.release is not None or a.from_here or a.sudo is not None or a.installation is not None:
                raise ValueError("--mode sets the host's update mode; --version, --from-here, --sudo and "
                                 "--installation are for an update now")
            hosts = load()
            for name in names:
                if name != LOCAL and name not in hosts:
                    raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
            results = [update_mode(name, a.mode, a.at) for name in names]
            if a.json:
                out(json.dumps(results, indent=2))
            for result in [] if a.json else results:
                if result["error"]:
                    out(f"{result['name']}: {result['error'].removeprefix(result['name'] + ': ')}")
                    continue
                state = result["status"] or {}
                timer = state.get("timer") or {}
                out(f"{result['name']}: update mode {result['mode']}" +
                    (f", window {state.get('window')}" if result["mode"] == "auto" else "") +
                    ("" if result["mode"] == "off" else ", timer made" if timer.get("installed") else ", no timer") +
                    (f" ({state['manual']})" if state.get("manual") else ""))
            return 1 if any(result["error"] for result in results) else 0
        if not 1 <= a.jobs <= 8:
            raise ValueError("--jobs is 1-8")
        if a.release is not None and not VERSION.fullmatch(a.release):
            raise ValueError("--version expects X.Y.Z")
        if a.from_here and a.release is not None:
            # before any output or SSH: the two contradict each other (hosts install refuses them the same way)
            raise ValueError("--from-here installs this machine's version; --version is for the signed release")
        if a.from_here and LOCAL in names:
            raise ValueError("--from-here is for another host (this machine updates with: sandbox-ai update)")
        hosts = load()
        for name in names:
            if name != LOCAL and name not in hosts:
                raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
        if not a.json:
            out(f"updating {', '.join(names)}" + (f" to {a.release}" if a.release else "") + " ...")
        if a.installation is not None and LOCAL in names:
            raise ValueError("--installation is for another host (this machine updates the installation it runs "
                             "from: sandbox-ai update)")
        results = update_many(names, a.release, a.sudo, a.jobs,
                              done=None if a.json else lambda result: [out(line) for line in update_lines(result)]
                              if not (result.get("needs_password") and sys.stdin.isatty()) else None,
                              from_here=a.from_here, installation_=a.installation)
        if not a.json and sys.stdin.isatty():
            for result in results:
                if not result.get("needs_password"):
                    continue
                out(f"{result['name']}: sudo there wants a password; asking in this terminal")
                if a.from_here:
                    # this machine's copy again, its sudo asking here: not the release update
                    try:
                        again = update(result["name"], sudo=True, from_here=True, terminal=True)
                    except (OSError, ValueError) as exc:
                        again = {"error": printable(exc)}
                    result.update({key: again[key] for key in ("error", "after", "changed", "output", "sudo",
                                                                 "needs_password", "notes") if key in again})
                    for line in update_lines(result):
                        out(line)
                    continue
                which = (result.get("installation") or {}) if a.installation == "all" else {}
                which = (which.get("path") or "").rsplit("/versions/", 1)[0] if which.get("path") else a.installation
                try:
                    command = update_terminal(result["name"], a.release, installation_=which)
                    status = subprocess.run(command).returncode
                except (OSError, ValueError) as exc:
                    result["error"] = printable(exc)
                else:
                    if status == 0:
                        result.update(error=None, needs_password=False)
                        executable = None
                        if which is not None:
                            with contextlib.suppress(OSError, ValueError):
                                executable = chosen_installation(installations(result["name"]), which)["executable"]
                        finish_update(result["name"], result, newest=a.release is None, executable=executable)
                    else:
                        result["error"] = f"the update in the terminal ended with exit status {status}"
                for line in update_lines(result):
                    out(line)
        if a.json:
            out(json.dumps(results, indent=2))
        else:
            out(summary(results))
        for result in results:  # an updated host gets what it could not take before (a shared LLM server)
            if result.get("after") and result.get("name") != LOCAL:
                shared_to_new_host(result["name"], (lambda line: print(line, file=sys.stderr)) if a.json else out)
        return 0 if all(not r.get("error") or r.get("after") for r in results) else 1
    if a.command == "add":
        entry = check(a.name, {"ssh": a.ssh, "port": a.port, **({"identity": a.identity} if a.identity else {}),
                               "description": a.description,
                               **({"command": a.remote_command} if a.remote_command else {}),
                               **({"update_sudo": True} if a.update_sudo else {})})
        with hosts_file_lock():
            hosts = load()
            if a.name in hosts:
                raise ValueError(f"host {a.name} exists; remove it first")
            hosts[a.name] = entry
            save(hosts)
        out(f"host {a.name} added")
        fixed, warnings = protect_keys(entry)
        for change in fixed:
            out(f"  key: {fixed_text(change)}")
        for warning in warnings:
            out(f"  note: {warning}")
        shared_to_new_host(a.name, out)
        if not fixed:
            out(f"check it with: sandbox-ai hosts test {a.name}")
            return 0
        # A key ssh had refused is usable now: whether the host answers with it.
        status, said, err, more, _ = run_fixing(a.name, ["--version"])
        for change in more:
            out(f"  key: {fixed_text(change)}")
        out(f"  connection: ok, {printable(said.strip())}" if status == 0 else
            f"  connection: failed: {failure(status, said, err)}")
        return 0
    if a.command == "set":
        with hosts_file_lock():
            hosts = load()
            if a.name not in hosts:
                raise ValueError(f"no host {printable(a.name)}")
            before = hosts[a.name]
            hosts[a.name] = check(a.name, {**hosts[a.name], "update_sudo": a.update_sudo == "on"})
            save(hosts)
        if any(before.get(key) != hosts[a.name].get(key) for key in CONNECTION):
            stop_master(before)  # its shared connection was made for what the entry said before
        out(f"host {a.name}: its update " + ("runs with sudo" if a.update_sudo == "on" else "runs without sudo"))
        return 0
    if a.command == "remove":
        remove_host(a.name, out)
        out(f"host {a.name} removed")
        return 0
    if a.command == "test":
        result = test(a.name)
        LOG.log("INFO" if result["ssh"] and not result["error"] else "WARNING", "hosts",
                f"test {a.name}: " + (f"ssh ok, {result['version']}" if result["ssh"] else "ssh failed")
                + (f"; {result['error']}" if result["error"] else "")
                + "".join(f"; doctor {c['status']}: {c.get('check')}: {c.get('detail')}" for c in result["doctor"] or []
                          if isinstance(c, dict) and c.get("status") in ("warn", "fail")))
        if a.json:
            out(json.dumps(result, indent=2))
        else:
            for change in result["fixed"]:
                out(f"  key: {fixed_text(change)}")
            for warning in result["warnings"]:
                out(f"  note: {warning}")
            reached = "this machine, no SSH" if a.name == LOCAL else "ssh ok"  # (local: sandbox-ai here, no SSH)
            out(f"{a.name}: " + (f"{reached}, {printable(result['version'])}" if result["ssh"] else "ssh failed")
                + (" (after making the key private, tried again)" if result["fixed"] else ""))
            if result["error"]:
                out(f"  {printable(result['error'])}")
            for check_ in result["doctor"] or []:
                if check_.get("status") in ("warn", "fail"):
                    out(f"  {check_['status'].upper():<4}  {printable(check_.get('check'))}: "
                        f"{printable(check_.get('detail'))}")
        return 0 if result["ssh"] and not result["error"] else 1
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
    if not args:
        raise ValueError("sandbox-ai hosts run NAME -- ARGS...")
    command = argv(a.name, args, tty=sys.stdin.isatty())
    begun = time.monotonic()
    status = subprocess.run(command).returncode
    # (its output went to this terminal, not here: only how it ended - 255: SSH did not reach the host. Asked for,
    # so said each time, not once per change as the polls' ran())
    text = f"{a.name}: sandbox-ai {LOG.command_line(args)} (hosts run, {time.monotonic() - begun:.1f} s): exit " \
           f"status {status}"
    if status == 255:
        LOG.warning("hosts", f"{text}: SSH did not reach {a.name} ({hosts_entry_text(a.name)}; ssh's message is on "
                             "the terminal; check it with: sandbox-ai hosts test " + a.name + ")")
    else:
        LOG.log("DEBUG" if status == 0 else "INFO", "hosts", text)
    return status


def uninstall_cli(a, out):
    """hosts uninstall: the host's own table first and a question here (y/N; --yes answers it, and without a
    terminal nothing is removed unless --yes), then its uninstall; sudo that wants a password asks in this terminal."""
    uninstall_args(a.purge, a.secrets, a.mcp_data)  # (before any SSH: --secrets without --purge)
    asking = not (a.yes or a.dry_run)
    if asking and not sys.stdin.isatty():
        raise ValueError("no terminal to ask on: add --yes to remove it (--dry-run shows what would go)")
    common = dict(installation_=a.installation, purge=a.purge, secrets=a.secrets, mcp_data=a.mcp_data,
                  stop_running=a.stop_running, sudo=a.sudo)
    if a.dry_run or asking:
        plan = uninstall(a.name, dry_run=True, **common)
        if a.dry_run or plan["error"]:
            out(json.dumps(plan, indent=2) if a.json else "\n".join(uninstall_lines(plan)))
            return 0 if not plan["error"] else 1
        for line in uninstall_lines(plan):
            out(line)
        try:
            answer = input(f"Remove it on {a.name}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out("nothing removed")
            return 1
    result = uninstall(a.name, remove=a.remove_from_list, terminal=not a.json and sys.stdin.isatty(), **common)
    out(json.dumps(result, indent=2) if a.json else "\n".join(uninstall_lines(result)))
    return 0 if not result["error"] else 1


def hosts_entry_text(name):
    """How host NAME is reached (ssh USER@HOST:PORT), for a line of the log."""
    try:
        entry = load().get(name) or {}
    except (OSError, ValueError):
        return "its entry unreadable"
    return f"ssh {entry.get('ssh')}" + (f":{entry['port']}" if entry.get("port") else "")


def module_version():
    """This machine's sandbox-ai version (the launcher's __version__)."""
    for line in (Path(__file__).resolve().parent / "agent_sandbox.py").read_text().splitlines():
        if line.startswith("__version__ = "):
            return line.split('"')[1]
    return "unknown"


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai hosts: {printable(exc)}", file=sys.stderr)
        sys.exit(1)
