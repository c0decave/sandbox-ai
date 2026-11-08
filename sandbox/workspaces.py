#!/usr/bin/env python3
"""sandbox-ai workspace: give a running sandbox another host directory, or take one back, without a restart.

  sandbox-ai workspace list [RUN] [--json]             the run's workspaces: the start's and the added ones
  sandbox-ai workspace add [RUN] DIR [--ro]            mount DIR at /workspaces/<name> while the sandbox runs
  sandbox-ai workspace remove [RUN] DIR|NAME [--force] take an added workspace away again

RUN as for `sandbox-ai net` (its run id or the name of its start; may be left out when only one sandbox runs).
A directory added gets the checks of --workspace at a start (no HOME, temporary or run directory, no private
location, nothing a sandbox may have planted a link in, no overlap with the run's workspaces, mounts and HOME,
none of the files the launcher reads inside it, no directory of your PATH in it); it is read-only with --ro, and
always when the run's security mode or --workspace-ro keeps further workspaces read-only. A workspace added to a
session is kept in its options, so `sandbox-ai resume NAME --restart` starts with it at the same place (not one
added with --ro: it would come back writable); `remove` takes it out again.

How: nothing runs in the sandbox. A helper of your own user opens DIR and checks that it is the directory that
was checked (device and inode), enters the sandbox's user namespace, clones DIR there (open_tree) and mounts it
in each of the sandbox's mount namespaces, the agent's under Firejail included (move_mount; Linux 5.2 or later,
read-only through mount_setattr: 5.12). The mount point is made on /workspaces, a small tmpfs every start of
0.1.76 or later has, only in the sandbox's own mount namespaces (never one the agent made). `remove` freezes the
sandbox for the check, refuses while a process there has a file open, its working directory or a mapping below it,
refuses while the agent holds a copy the mount tables show in a namespace of its own, and unmounts strictly (the
kernel refuses while a process of the sandbox's own namespaces still holds the tree); --force detaches it anyway:
what is still open stays usable, also for writing, until it is closed. A copy held only through a descriptor shows
nowhere: only a restart takes a workspace away for sure.

The file log (--log-dir) watches it from then on. Not for a workspace added during the run: the push check (a
push of its commits to a public remote is refused: push from /work, a start workspace, or from the host) and the
privacy check after the run; restart the session with the workspace (resume --restart --workspace DIR) for them.

Standard library only.
"""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
WORKSPACES_DIR = "/workspaces"
SYS_OPEN_TREE, SYS_MOVE_MOUNT, SYS_MOUNT_SETATTR = 428, 429, 442  # the same number on every architecture
OPEN_TREE_CLONE, OPEN_TREE_CLOEXEC, AT_RECURSIVE, AT_EMPTY_PATH = 1, 0o2000000, 0x8000, 0x1000
MOVE_MOUNT_F_EMPTY_PATH, MOVE_MOUNT_T_EMPTY_PATH = 0x4, 0x40
MOUNT_ATTR_RDONLY, MOUNT_ATTR_NOSUID, MOUNT_ATTR_NODEV = 0x1, 0x2, 0x4
CLONE_NEWNS, CLONE_NEWUSER = 0x00020000, 0x10000000
MNT_DETACH, UMOUNT_NOFOLLOW = 2, 8
STATES = ("attached", "removed")
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_workspaces", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def shown(value, limit=300):
    return module("agent_sandbox").printable(str(value))[:limit]


# --- the namespace helper (runs in a process of its own: setns needs a single thread) --------------


class MountAttr(ctypes.Structure):
    _fields_ = [("attr_set", ctypes.c_uint64), ("attr_clr", ctypes.c_uint64), ("propagation", ctypes.c_uint64),
                ("userns_fd", ctypes.c_uint64)]


def libc():
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    return library


def check(result, what, kernel="5.2"):
    if result < 0:
        code = ctypes.get_errno()
        if code == errno.ENOSYS:
            raise OSError(code, f"{what}: this kernel lacks the new mount API (Linux {kernel} or later is needed)")
        raise OSError(code, f"{what}: {os.strerror(code)}")
    return result


def clone(c, source, identity, mode):
    """A detached copy (open_tree) of the directory SOURCE, refused unless it is still IDENTITY (device, inode):
    what was checked is what is mounted, whatever was swapped in on the way since. Read-only for MODE ro; never
    set-user-ID or device files."""
    fd = os.open(source, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != tuple(identity):
            raise OSError(errno.ESTALE, f"{source} changed after it was checked; add it again")
        tree = check(c.syscall(SYS_OPEN_TREE, fd, b"", OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | AT_RECURSIVE
                               | AT_EMPTY_PATH), f"clone {source}")
    finally:
        os.close(fd)
    attributes = MountAttr(MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV | (MOUNT_ATTR_RDONLY if mode == "ro" else 0), 0, 0, 0)
    result = c.syscall(SYS_MOUNT_SETATTR, tree, b"", AT_EMPTY_PATH | AT_RECURSIVE, ctypes.byref(attributes),
                       ctypes.sizeof(attributes))
    if result < 0 and (mode == "ro" or ctypes.get_errno() != errno.ENOSYS):
        os.close(tree)
        check(result, f"make {source} {'read-only' if mode == 'ro' else 'nosuid'}", "5.12")
    return tree


def mount_point(name, create):
    """(fd of /workspaces, fd of /workspaces/NAME or None) in the current mount namespace, neither through a link:
    the agent may have put anything there. CREATE makes the directory."""
    base = os.open(WORKSPACES_DIR, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o755, dir_fd=base)
    try:
        target = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=base)
    except FileNotFoundError:
        if create:
            raise
        return base, None
    except OSError as exc:
        os.close(base)
        raise OSError(exc.errno, f"{WORKSPACES_DIR}/{name} is no directory in the sandbox (made there?)") from None
    return base, target


def mounted(base, target):
    """Something other than the /workspaces tmpfs is mounted at TARGET."""
    return os.fstat(base).st_dev != os.fstat(target).st_dev


def helper(op, user_pid, name, targets, source=None, identity=None, mode="rw", proc=PROC, c=None, force=False):
    """In this (fresh, single-threaded) process: enter the user namespace of USER_PID, then mount SOURCE (op add)
    at /workspaces/NAME, or unmount it there (op remove), in the mount namespace of every pid in TARGETS. An add
    that fails in one namespace is undone in those it reached."""
    c = c or libc()
    user = os.open(proc / str(user_pid) / "ns" / "user", os.O_RDONLY | os.O_CLOEXEC)
    namespaces = [os.open(proc / str(pid) / "ns" / "mnt", os.O_RDONLY | os.O_CLOEXEC) for pid in targets]
    check(c.setns(user, CLONE_NEWUSER), "enter the sandbox's user namespace")
    trees = []
    if op == "add":
        # A mount namespace of our own, owned by the sandbox's user namespace, may clone the host's directory;
        # each namespace needs a clone of its own (a detached tree is attached once).
        check(c.unshare(CLONE_NEWNS), "a private mount namespace")
        trees = [clone(c, source, identity, mode) for _ in namespaces]
    reached = []
    try:
        for index, fd in enumerate(namespaces):
            check(c.setns(fd, CLONE_NEWNS), "enter a mount namespace of the sandbox")
            if op == "add":
                attach(c, name, trees[index])
                reached.append(fd)
            else:
                detach(c, name, force)
    except OSError:
        for fd in reached:
            with contextlib.suppress(OSError):
                check(c.setns(fd, CLONE_NEWNS), "enter a mount namespace of the sandbox")
                detach(c, name, True)
        raise
    return 0


def place(name, create):
    try:
        return mount_point(name, create)
    except FileNotFoundError:
        raise OSError(errno.ENOENT, f"the sandbox has no {WORKSPACES_DIR} of its own (started by sandbox-ai 0.1.75 "
                                    "or older?): restart it with the workspace") from None


def attach(c, name, tree):
    """Mount TREE at /workspaces/NAME of the current mount namespace."""
    base, target = place(name, True)
    try:
        if mounted(base, target):
            raise OSError(errno.EBUSY, f"{WORKSPACES_DIR}/{name} is in use in the sandbox")
        check(c.syscall(SYS_MOVE_MOUNT, tree, b"", target, b"", MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH),
              f"mount {WORKSPACES_DIR}/{name}")
    finally:
        os.close(target)
        os.close(base)


def detach(c, name, force=False):
    """Unmount /workspaces/NAME of the current mount namespace if something is mounted there; remove the mount
    point once no namespace has a mount on it (the tmpfs is shared, rmdir fails while one has). Strictly first: the
    kernel refuses (EBUSY) while anything still holds the tree, also what no look at /proc shows (a descriptor in
    flight over a socket, io_uring); FORCE detaches it then (what is held stays usable until it is closed)."""
    base, target = place(name, False)
    try:
        if target is None:
            return
        busy = mounted(base, target)
        os.close(target)
        target = None
        if busy:
            path = f"{WORKSPACES_DIR}/{name}".encode()
            if c.umount2(path, UMOUNT_NOFOLLOW) < 0:
                code = ctypes.get_errno()
                if code != errno.EBUSY or not force:
                    if code == errno.EBUSY:
                        raise OSError(code, f"{WORKSPACES_DIR}/{name} is still in use in the sandbox (an open file or "
                                            "directory, or a mount below it): --force detaches it anyway")
                    check(-1 if code else 0, f"unmount {WORKSPACES_DIR}/{name}")
                check(c.umount2(path, MNT_DETACH | UMOUNT_NOFOLLOW), f"unmount {WORKSPACES_DIR}/{name}")
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=base)
    finally:
        if target is not None:
            os.close(target)
        os.close(base)


def own_namespaces(found, proc=PROC):
    """The pids of FOUND's mount namespaces that belong to the sandbox's own user namespace: never one the agent
    made itself (a user and mount namespace of its own, where it holds every right and could undo read-only)."""
    def user(pid):
        try:
            return os.readlink(Path(proc) / str(pid) / "ns" / "user")
        except OSError:
            return None
    own = user(found["user"])
    return [pid for pid in found["mntns"].values() if own is not None and user(pid) == own]


def run_helper(op, found, name, source=None, identity=None, mode="rw", force=False):
    """Run helper() in a process of its own; (status, error text)."""
    targets = own_namespaces(found)
    if not targets:
        return 1, "no mount namespace of the sandbox found"
    command = [sys.executable, "-I", str(Path(__file__)), "--internal-helper",
               json.dumps({"op": op, "user": found["user"], "name": name, "targets": targets, "source": source,
                           "identity": identity, "mode": mode, "force": force})]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        if op == "add":  # killed before its own rollback: take out what it may have mounted already
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                undo = [sys.executable, "-I", str(Path(__file__)), "--internal-helper",
                        json.dumps({"op": "remove", "user": found["user"], "name": name, "targets": targets,
                                    "force": True})]
                subprocess.run(undo, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return 1, "the helper did not finish within 30 s (a mount namespace that does not answer?)"
    return result.returncode, result.stderr.strip()[-500:]


def helper_main(argument):
    request = json.loads(argument)
    return helper(request["op"], int(request["user"]), request["name"], [int(p) for p in request["targets"]],
                  request.get("source"), request.get("identity"), request.get("mode", "rw"),
                  force=bool(request.get("force")))


# --- which processes use a workspace -----------------------------------------------------------------


def users(found, sandbox_path, proc=PROC):
    """[(pid, command, what)] of the sandbox's processes with a file, their working directory or a mapping below
    SANDBOX_PATH (as the processes see it: the paths in /proc are theirs)."""
    def below(value):
        value = value.removesuffix(" (deleted)")
        return value == sandbox_path or value.startswith(sandbox_path + "/")
    result = []
    for pid in found.get("pids", []):
        base = proc / str(pid)
        what = []
        for link in ("cwd", "root"):
            with contextlib.suppress(OSError):
                if below(os.readlink(base / link)):
                    what.append(link)
        with contextlib.suppress(OSError):
            for entry in os.listdir(base / "fd"):
                with contextlib.suppress(OSError):
                    if below(os.readlink(base / "fd" / entry)):
                        what.append("an open file")
                        break
        with contextlib.suppress(OSError):
            with open(base / "maps", "rb") as stream:
                for line in stream.read(1 << 22).decode("utf-8", "replace").splitlines():
                    parts = line.split(None, 5)
                    if len(parts) == 6 and below(parts[5]):
                        what.append("a mapped file")
                        break
        if what:
            command = ""
            with contextlib.suppress(OSError):
                command = (base / "comm").read_text().strip()
            result.append((pid, command, ", ".join(dict.fromkeys(what))))
    return result


def unescaped(field):
    """A path of /proc/PID/mountinfo as it is: the kernel writes space, tab, newline and backslash as \\ooo."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mountinfo(proc, pid):
    """[(major:minor, root, mount point)] of PID's mount namespace (/proc/PID/mountinfo; paths unescaped)."""
    try:
        text = (Path(proc) / str(pid) / "mountinfo").read_text(errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 5:
            found.append((fields[2], unescaped(fields[3]), unescaped(fields[4])))
    return found


def own_cgroup(record, found):
    """The cgroup of FOUND when it is the run's own (the pod's under Podman; the scope systemd-run made for
    Bubblewrap, recorded at the start as own_cgroup), else None: without its own scope, Bubblewrap runs in the
    launcher's cgroup, which holds your terminal's or tmux's processes too: those are never frozen or counted."""
    if record.get("backend") == "podman" or record.get("own_cgroup"):
        return found.get("cgroup")
    return None


def cgroup_members(found, proc=PROC, group=None):
    """Every process in the sandbox's own cgroup GROUP (see own_cgroup), also those in user namespaces the agent
    made itself, which the namespace-based list leaves out; without one, the namespace-based list alone."""
    pids = set(found.get("pids", []))
    if group:
        try:
            entries = [e.name for e in Path(proc).iterdir() if e.name.isdigit()]
        except OSError:
            entries = []
        for name in entries:
            text = module("devices").read(Path(proc) / name / "cgroup") or b""
            for line in text.decode("utf-8", "replace").splitlines():
                if line.startswith("0::") and (line[3:] == group or line[3:].startswith(group.rstrip("/") + "/")):
                    pids.add(int(name))
    return sorted(pids)


def copies(found, sandbox_path, proc=PROC, group=None):
    """[(pid, mount point)] of the mounts of the workspace at SANDBOX_PATH outside the sandbox's own mount
    namespaces: copies the agent took into a user and mount namespace of its own (unshare), which no unmount of
    ours reaches. The workspace is told by the device and root of its mount in a namespace of the sandbox; when
    that mount cannot be found, nothing can be told: [(None, reason)] (the caller refuses then). What the agent
    holds only through a descriptor, detached from every namespace, no table shows: only a restart is sure."""
    if not group:  # without the run's own cgroup the agent's namespaces cannot be found: never "none"
        return [(None, "without a cgroup of the run's own (Bubblewrap without its systemd scope) copies the agent "
                       "may hold in a namespace of its own cannot be looked for")]
    own = {module("devices").namespace(Path(proc), pid, "mnt") for pid in own_namespaces(found, proc)}
    ours = None
    for pid in own_namespaces(found, proc):
        ours = next(((dev, root) for dev, root, point in mountinfo(proc, pid) if point == sandbox_path), None)
        if ours:
            break
    if ours is None:
        return [(None, f"the sandbox's own mount of {sandbox_path} was not found, so copies cannot be looked for")]
    dev, root = ours
    seen, result = set(), []
    for pid in cgroup_members(found, proc, group):
        ns = module("devices").namespace(Path(proc), pid, "mnt")
        if ns is None or ns in own or ns in seen:
            continue
        seen.add(ns)
        for other_dev, other_root, point in mountinfo(proc, pid):
            if other_dev == dev and (other_root == root or other_root.startswith(root.rstrip("/") + "/")):
                result.append((pid, point))
                break
    return result


# --- the checks of a start ---------------------------------------------------------------------------------------


def overlaps(path, other):
    return path == other or other in path.parents or path in other.parents


def opened(value):
    """(fd, real path, identity) of the directory VALUE, opened once: the checks look at what this descriptor is,
    the helper mounts only that (clone)."""
    l = module("agent_sandbox")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("give a directory")
    source = Path(os.path.normpath(Path(value).expanduser().absolute()))
    if any(c in str(source) for c in (",", "\n", "\r")) or not str(source).isprintable():
        raise ValueError("a workspace path cannot hold commas or control characters")
    if any(path.is_symlink() and not path.exists() for path in (source, *source.parents)):
        raise ValueError("the workspace path contains a dangling symlink")
    # An agent of an earlier run may have planted a link to get a host directory mounted read-write.
    l.check_planted(str(source), "Workspace", l.exposed_before())
    try:
        fd = os.open(source, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ValueError(f"{shown(source)}: no such directory") from None
    except NotADirectoryError:
        raise ValueError(f"{shown(source)}: not a directory") from None
    real = Path(os.readlink(f"/proc/self/fd/{fd}"))
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or not real.is_absolute():
        os.close(fd)
        raise ValueError(f"{shown(source)}: not a directory")
    return fd, real, [info.st_dev, info.st_ino]


def judged(record, real, read_only=False):
    """The mode (ro or rw) RUN gives the workspace REAL (resolved), or ValueError: the start's checks of a
    workspace, with what run.json recorded of the run (workspace_checks)."""
    l = module("agent_sandbox")
    checks = record.get("workspace_checks")
    if not isinstance(checks, dict):
        raise ValueError(f"{shown(record.get('run'))} was started by sandbox-ai 0.1.75 or older, which made no place "
                         "for workspaces added later: restart it with the workspace (sandbox-ai resume NAME "
                         "--restart --workspace DIR)")
    if any(entry.split("=", 1)[0] == "--workspace" for entry in l.load_policy().get("forbid", [])):
        raise ValueError(f"--workspace is not allowed by the administrator's policy ({l.POLICY_FILE})")
    if any(c in str(real) for c in (",", "\n", "\r")) or not str(real).isprintable():
        raise ValueError("a workspace path cannot hold commas or control characters")
    l.check_workspace_scope(real)
    # A start lets a workspace hold the project's own .venv/bin or node_modules/.bin on your PATH, and checks what
    # the agent changed there after the run (tool_dirs_state); for a workspace added later nothing would.
    for directory in l.private_locations()[1]:
        if directory == real or real in directory.parents:
            raise ValueError(f"{shown(real)} holds {shown(directory)}, a directory on your PATH: programs the agent "
                             "puts there would run on the host. Start with it as a workspace instead (the start checks "
                             "it after the run), or take it off your PATH")
    for entry in checks.get("workspaces", []) + added_workspaces(record):
        other = Path(entry["host"])
        if overlaps(real, other):
            raise ValueError(f"workspaces must not overlap: {shown(real)} and {shown(other)} "
                             f"({shown(entry['sandbox'])})")
    for value in checks.get("trees", []):
        if overlaps(real, Path(value)):
            raise ValueError(f"{shown(real)} overlaps {shown(value)}, a mount, --state-dir or the HOME of the run")
    for value, label in checks.get("held", []):
        path = Path(value)
        if path == real or real in path.parents:
            raise ValueError(f"{shown(real)} holds {shown(value)} ({label}), which the agent could then change; "
                             "keep it outside workspaces")
    for value, label in checks.get("private", []):
        if overlaps(real, Path(value)):
            raise ValueError(f"{shown(real)} overlaps {shown(value)} ({label}), which the agent must not reach")
    l.check_stores([real], [(Path(value), label) for value, label in checks.get("stores", [])])
    return "ro" if read_only or checks.get("read_only") else "rw"


def sandbox_name(record, real):
    """/workspaces/<name> for REAL: its name, -2, -3 ... after the names taken, as a start names them
    (workspace_targets), so that a restart with it finds it at the same place."""
    checks = record.get("workspace_checks") or {}
    taken = {PurePosixPath(entry["sandbox"]).name for entry in checks.get("workspaces", [])[1:]}
    taken |= {PurePosixPath(entry["sandbox"]).name for entry in added_workspaces(record)}
    base = real.name or "root"
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


# --- the run's registry and log ----------------------------------------------------------------------------------


def registry(record):
    return Path(record["directory"]) / "workspaces.json"


@contextlib.contextmanager
def locked(record):
    fd = os.open(Path(record["directory"]) / "workspaces.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(record):
    try:
        data = json.loads(registry(record).read_text())
    except (OSError, ValueError):
        return []
    entries = data.get("workspaces") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict) and e.get("state") in STATES and isinstance(e.get("host"), str)
            and isinstance(e.get("sandbox"), str)] if isinstance(entries, list) else []


def save(record, entries):
    path = registry(record)
    temporary = path.with_name(".workspaces.json")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as stream:
        json.dump({"version": 1, "workspaces": entries}, stream)
    os.replace(temporary, path)


def added_workspaces(record):
    return [entry for entry in load(record) if entry.get("state") == "attached"]


def log(record, event, entry, **fields):
    """One line of workspaces.jsonl in the run's log directory (none without one)."""
    directory = record.get("log_dir")
    if not directory:
        return
    line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "run": record.get("run"),
            "host": entry.get("host"), "sandbox": entry.get("sandbox"), "mode": entry.get("mode"),
            **{key: value for key, value in fields.items() if value is not None}}
    with contextlib.suppress(OSError):
        fd = os.open(Path(directory) / "workspaces.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600)
        with open(fd, "a") as stream:
            stream.write(json.dumps(line) + "\n")


# --- the session's options: a restart starts with what was added -------------------------------------------------


def session_of(record):
    """The session (sessions.py) the run belongs to, or None (a run in the foreground keeps nothing)."""
    start = record.get("start")
    if not isinstance(start, str):
        return None
    sessions = module("sessions")
    try:
        return start if (sessions.session_dir(start) / "session.json").is_file() else None
    except ValueError:
        return None


def keep(record, host, add=True):
    """Add HOST to the saved options of the run's session (or take out what `add` put there); True if kept."""
    name = session_of(record)
    if name is None:
        return False
    sessions = module("sessions")
    with sessions.session_lock(name):
        data = sessions.load(name)
        options = list(data.get("options", []))
        if add:
            data["options"] = sessions.added_options(data, workspaces=[host])
        else:
            pairs = [i for i in range(len(options) - 1) if options[i] == "--workspace" and options[i + 1] == host]
            if not pairs:
                return False
            del options[pairs[-1]:pairs[-1] + 2]
            data["options"] = options
        sessions.save(name, data)
    return True


# --- add, remove, list ----------------------------------------------------------------------------------------


def finder_default(record):
    return module("devices").sandbox_processes(record)


def watch_file_log(record, sandbox_path, out=print):
    """Ask the run's supervisor to watch SANDBOX_PATH for its file log (files.jsonl); True if it does, False if the
    run keeps none, None if it could not be asked (a run of an older version, the socket gone)."""
    try:
        return bool(module("net").ask(record["directory"], {"op": "watch", "path": sandbox_path}).get("watched"))
    except (OSError, ValueError, KeyError) as exc:
        out(f"  not in the file log: {shown(exc, 300)}")
        return None


def add(record, value, read_only=False, out=print, runner=run_helper, finder=finder_default, remember=None,
        watcher=None):
    """Mount the host directory VALUE into the running sandbox RECORD at /workspaces/<name>; its sandbox path."""
    l = module("agent_sandbox")
    fd, real, identity = opened(value)
    try:
        with locked(record):
            mode = judged(record, real, read_only)
            name = sandbox_name(record, real)
            found = finder(record)
            if not found:
                raise ValueError("the sandbox's processes were not found (has it ended?)")
            if mode == "rw":
                # A link planted in it later may be meant to get another directory mounted at a later start.
                (remember or (lambda trees: l.remember_exposed(None, trees)))([real])
            status, error = runner("add", found, name, str(real), identity, mode)
            if status:
                raise ValueError(f"could not add {shown(real)}: {error or 'the helper failed'}")
            entries = load(record)
            entry = {"host": str(real), "sandbox": f"{WORKSPACES_DIR}/{name}", "mode": mode, "state": "attached",
                     "since": time.time(), "identity": identity, "kept": False}
            entries.append(entry)
            save(record, entries)
    finally:
        os.close(fd)
    checks = record.get("workspace_checks") or {}
    if mode == "ro" and not checks.get("read_only"):
        # Kept as --workspace it would come back writable at a restart (only --workspace-ro or the mode make further
        # workspaces read-only): this one lasts for the run.
        if session_of(record) is not None:
            out("  not kept for the session's next start: it was added read-only (add it again after a restart, or "
                "start with --workspace-ro)")
    else:
        try:
            entry["kept"] = keep(record, str(real))
        except (OSError, ValueError) as exc:
            out(f"  not kept for the session's next start: {shown(exc, 500)}")
    if entry["kept"]:
        with locked(record):
            entries = load(record)
            for other in entries:
                if other.get("sandbox") == entry["sandbox"] and other.get("state") == "attached":
                    other["kept"] = True
            save(record, entries)
    watched = (watcher or watch_file_log)(record, entry["sandbox"], out)
    log(record, "add", entry, kept=entry["kept"], watched=watched)
    out(f"added {shown(real)} to {shown(record.get('run'))} at {entry['sandbox']} ({mode}"
        f"{', kept for the session' if entry['kept'] else ''}); push its commits from the host or after "
        "a restart: the push check knows only the start's workspaces")
    return entry["sandbox"]


def matching(record, value):
    entries = added_workspaces(record)
    resolved = None
    if value.startswith(("/", "~", ".")) and not value.startswith(WORKSPACES_DIR + "/"):
        with contextlib.suppress(OSError, RuntimeError):
            resolved = str(Path(value).expanduser().resolve())
    for entry in entries:
        names = (entry["sandbox"], PurePosixPath(entry["sandbox"]).name, entry["host"])
        if value in names or resolved == entry["host"]:
            return entry
    started = (record.get("workspace_checks") or {}).get("workspaces", [])
    if any(value in (e.get("host"), e.get("sandbox")) or resolved == e.get("host") for e in started):
        raise ValueError(f"{shown(value)} is a workspace of the start: it stays until the run ends")
    raise ValueError(f"{shown(value)} was not added during this run (see sandbox-ai workspace list)")


CGROUP_ROOT = Path("/sys/fs/cgroup")


@contextlib.contextmanager
def frozen(found, root=CGROUP_ROOT, wait=2.0, group=None):
    """The sandbox's cgroup frozen (cgroup.freeze) while the block runs, so that nothing opens a file between the
    look at what is in use and the unmount; best effort (a cgroup that is not delegated stays as it is), and one
    paused already (sandbox-ai pause) stays paused."""
    path = Path(root) / str(group).lstrip("/") / "cgroup.freeze" if group else None
    thawed = False
    try:
        if path is not None and path.read_text().strip() == "0":
            path.write_text("1")
            thawed = True
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and "frozen 1" not in (path.parent / "cgroup.events").read_text():
                time.sleep(0.02)
    except OSError:
        pass
    try:
        yield thawed
    finally:
        if thawed:
            with contextlib.suppress(OSError):
                path.write_text("0")


def remove(record, value, force=False, out=print, runner=run_helper, finder=finder_default, using=users, taken=None):
    taken = taken or copies
    with locked(record):
        entry = matching(record, value)
        found = finder(record)
        if not found:
            raise ValueError("the sandbox's processes were not found (has it ended?)")
        group = own_cgroup(record, found)
        with frozen(found, group=group):
            busy = using(found, entry["sandbox"])
            held = taken(found, entry["sandbox"], group=group)
            unknown = [reason for pid, reason in held if pid is None]
            held = [(pid, point) for pid, point in held if pid is not None]
            if unknown and not force:
                raise ValueError(f"{unknown[0]}; --force takes it out of the sandbox's own view anyway (only a restart "
                                 "is sure)")
            if held and not force:
                raise ValueError(f"{entry['sandbox']} has been taken into a namespace of the agent's own (process "
                                 f"{', '.join(str(pid) for pid, _ in held[:8])}): no unmount reaches it there, only a "
                                 "restart of the sandbox takes it away. --force takes it out of the sandbox's own view")
            if busy and not force:
                shown_busy = "; ".join(f"{pid} {shown(command, 40)} ({what})" for pid, command, what in busy[:8])
                raise ValueError(f"{entry['sandbox']} is in use in the sandbox: {shown_busy}"
                                 f"{' ...' if len(busy) > 8 else ''}. Let the agent leave it, or --force (what is "
                                 "open stays usable, also for writing, until it is closed; only a restart is sure)")
            status, error = runner("remove", found, PurePosixPath(entry["sandbox"]).name, force=force)
        if status:
            raise ValueError(f"could not remove {entry['sandbox']}: {error or 'the helper failed'}")
        entries = load(record)
        for other in entries:
            if other.get("sandbox") == entry["sandbox"] and other.get("state") == "attached":
                other.update(state="removed", removed=time.time())
        save(record, entries)
    kept = False
    if entry.get("kept"):
        try:
            kept = keep(record, entry["host"], add=False)
        except (OSError, ValueError) as exc:
            out(f"  still in the session's options: {shown(exc, 500)}")
    log(record, "remove", entry, forced=force or None, open=[pid for pid, _, _ in busy] or None)
    if held:
        out(f"  warning: the agent still holds {entry['sandbox']} in a namespace of its own (process "
            f"{', '.join(str(pid) for pid, _ in held[:8])}): it keeps the host directory until the sandbox is restarted")
    if force:
        out("  warning: detached by force: whatever a process in the sandbox still holds there stays usable, also "
            "for writing, until it is closed" + (f" (open in {', '.join(str(pid) for pid, _, _ in busy)})"
                                                 if busy else "") + "; a restart takes it away for sure")
    out(f"removed {entry['sandbox']} ({shown(entry['host'])}) from {shown(record.get('run'))}"
        f"{' and from the session options' if kept else ''}; what the agent may still hold in a namespace of its own "
        "(a copy no table shows) only a restart takes away for sure")


def listing(record):
    checks = record.get("workspace_checks") or {}
    result = [{"host": e.get("host"), "sandbox": e.get("sandbox"), "mode": e.get("mode"), "source": "start"}
              for e in checks.get("workspaces", [])]
    if not checks and record.get("workspace"):
        result.append({"host": record["workspace"], "sandbox": "/work", "mode": None, "source": "start"})
    for entry in added_workspaces(record):
        result.append({"host": entry["host"], "sandbox": entry["sandbox"], "mode": entry["mode"], "source": "added",
                       "kept": bool(entry.get("kept")),
                       "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.get("since") or 0))})
    return result


def list_lines(record, entries):
    rows = [["SANDBOX", "MODE", "SOURCE", "HOST"]]
    for entry in entries:
        source = entry["source"] + (", kept" if entry.get("kept") else "")
        rows.append([entry["sandbox"], entry["mode"] or "-", source, shown(entry["host"], 500)])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[-1] for row in rows]


def main(argv, out=print):
    net = module("net")
    parser = argparse.ArgumentParser(prog="sandbox-ai workspace", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:5]))
    commands = parser.add_subparsers(dest="command", required=True)
    lister = commands.add_parser("list", help="the run's workspaces")
    lister.add_argument("run", nargs="?")
    lister.add_argument("--json", action="store_true", help="as JSON")
    adder = commands.add_parser("add", help="mount a host directory into the running sandbox")
    adder.add_argument("args", nargs="+", metavar="[RUN] DIR")
    adder.add_argument("--ro", action="store_true", help="read-only")
    remover = commands.add_parser("remove", help="take an added workspace away again")
    remover.add_argument("args", nargs="+", metavar="[RUN] DIR|NAME")
    remover.add_argument("--force", action="store_true", help="also while a process in the sandbox uses it")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            record = net.choose(net.running(), args.run)
            entries = listing(record)
            out(json.dumps(entries, indent=2) if args.json else "\n".join(list_lines(record, entries)))
            return 0
        if len(args.args) > 2:
            raise ValueError("give [RUN] DIR")
        run, value = args.args if len(args.args) == 2 else (None, args.args[0])
        record = net.choose(net.running(), run)
        if args.command == "add":
            add(record, value, args.ro, out=out)
        else:
            remove(record, value, args.force, out=out)
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai workspace: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-helper":
        try:
            sys.exit(helper_main(sys.argv[2]))
        except OSError as exc:
            print(exc.strerror or str(exc), file=sys.stderr)
            sys.exit(1)
    sys.exit(main(sys.argv[1:]))
