#!/usr/bin/env python3
"""sandbox-ai workspace: give a running sandbox another host directory, or take one back, without a restart.

  sandbox-ai workspace list [RUN] [--json]             the run's workspaces: the start's and the added ones
  sandbox-ai workspace add [RUN] DIR|FILE [--ro] [--name NAME]
                                                       mount DIR (or a single regular FILE) at /workspaces/NAME
                                                       (default NAME: its own name) while the sandbox runs
  sandbox-ai workspace add [RUN] sshfs://[USER@]HOST[:PORT]/PATH [--ro] [--name NAME]
                                                       a folder of another machine: mounted on this host with
                                                       sshfs, then added as that folder (remotefs.py)
  sandbox-ai workspace remove [RUN] DIR|FILE|NAME [--force] take an added workspace away again
  sandbox-ai workspace review [RUN] [--project DIR | --log-dir DIR] [--json]
                                                       what the agent changed that your host runs or trusts
                                                       (workspace_review.py), with the diffs
  sandbox-ai workspace review RUN --keep PATH | --restore PATH --yes
                                                       per file: keep the change, or put the file back as it
                                                       was at the start (its recorded before-state)

RUN as for `sandbox-ai net` (its run id or the name `sandbox-ai list` shows; may be left out when only one sandbox
runs). For a stopped kept start, add keeps DIR for its next start instead (as resume NAME --workspace DIR would:
nothing is mounted now, `sandbox-ai resume NAME` mounts it with the checks of a start) and remove takes such a one
out again; a FILE and --ro wait for a running start.
A directory added gets the checks of --workspace at a start (no HOME, temporary or run directory, no private
location, nothing a sandbox may have planted a link in, no overlap with the run's workspaces, mounts and HOME,
none of the files the launcher reads inside it, no directory of your PATH in it); it is read-only with --ro, and
always when the run's security mode or --workspace-ro keeps further workspaces read-only. A workspace added to a
session is kept in its options, so `sandbox-ai resume NAME --restart` starts with it at the same place (not one
added with --ro: it would come back writable; in a session it is named NAME-ro, so that it never takes the place a
kept one gets back at a restart); `remove` takes it out again. --name NAME (1-64 letters, digits, '_', '.', '-',
starting with a letter or digit; no name a workspace of the run holds, nor one a kept workspace gets back at a
restart) is kept with it (--workspace-name NAME=DIR), so a restart puts it at /workspaces/NAME again.

A FILE gets the same checks (the file, the folders it lies in); only a regular file is taken (never a socket, FIFO
or device), mounted on an empty file the helper makes at /workspaces/NAME. It lasts for the run: a start takes no file
as a workspace, so it is not kept for the session, and the file log, the push check and the privacy check after the
run look at directories only. An editor that saves by writing a new file and renaming it over the old one replaces
the name on the host, not the mounted file: the sandbox keeps seeing the file it was given (add it again).

How: nothing runs in the sandbox. A helper of your own user opens DIR and checks that it is the directory that
was checked (device and inode), that each PID it is given still names the process found (its start time), enters
the sandbox's user namespace, clones DIR there (open_tree) and mounts it
in each of the sandbox's mount namespaces, the agent's under Firejail included (move_mount; Linux 5.2 or later,
nosuid, nodev and read-only through mount_setattr: Linux 5.12, without which nothing is added). Nothing of the
kernel's own (/proc, /sys, /dev, or a proc, sysfs, cgroup, devpts ... file system mounted below DIR) is handed over.
The mount point is made on /workspaces, a small tmpfs every start of
0.1.76 or later has, only in the sandbox's own mount namespaces (never one the agent made). `remove` freezes the
sandbox for the check, refuses while a process there has a file open, its working directory or a mapping below it,
refuses while the agent holds a copy the mount tables show in a namespace of its own, and unmounts strictly (the
kernel refuses while a process of the sandbox's own namespaces still holds the tree); --force detaches it anyway:
what is still open stays usable, also for writing, until it is closed. A copy held only through a descriptor shows
nowhere: only a restart takes a workspace away for sure.

--hide: the run's globs hold for a directory added as well. What they match in it gets the empty read-only file
or folder a start puts over such a file, before the directory reaches the sandbox: the helper mounts its clone in
a mount namespace of its own (on a staging folder of the run's), the blanks over the matches there (no link on the
way followed), and hands the sandbox a recursive clone of that - the agent never sees the folder without them. A
file added that a glob matches is added (you chose it), with a warning. `remove` unmounts the blanks first.

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
import socket
import stat
import shutil
import subprocess
import sys
import tempfile
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
AT_FDCWD = -100
# The registry's states: pending (recorded before the mount: it may be there, a failed save leaves no unknown mount),
# attached, partial (a removal that stopped half-way: gone in the namespaces of "gone_in", still in the others),
# removed. Every state but removed counts as present (names, overlaps, remove).
STATES = ("pending", "attached", "partial", "removed")
KINDS = ("dir", "file")  # what an added workspace is: a directory, or a single regular file (workspace add FILE)
# --name NAME: what /workspaces/NAME may be called (no "/", no leading "." or "-", nothing a shell or a list splits).
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PRESENT = ("pending", "attached", "partial")
PARTIAL = 3  # the helper's exit status when a removal stopped half-way (stdout: {"gone": [pid, ...]})
UNSURE = 4  # the helper's exit status when what it did may not have been undone (an add whose rollback failed)

# Why `workspace remove` refuses: each refusal carries one of these phrases, so that a front end can tell the reason
# (the CLI's text, the web UI's and the app's error):
#   REFUSED_IN_USE    "a process holds it": a process of the sandbox has a file, its working directory or a mapping
#                     below it (--force detaches it anyway)
#   REFUSED_COPY      "the agent took a copy into a namespace of its own": a mount namespace the agent made shows
#                     it; no unmount reaches it there (--force takes it out of the sandbox's own view)
#   REFUSED_UNKNOWN   "copies cannot be looked for": no cgroup of the run's own, or the sandbox's own mount of it not
#                     found (--force takes it out of the sandbox's own view)
#   REFUSED_BUSY      "the kernel still holds it": the strict unmount failed (something no look at /proc shows,
#                     a descriptor in flight, io_uring) (--force detaches it anyway)
#   REFUSED_PARTIAL   "partly removed": the removal stopped half-way and could not be undone (state partial)
REFUSED_IN_USE = "a process holds it"
REFUSED_COPY = "the agent took a copy into a namespace of its own"
REFUSED_UNKNOWN = "copies cannot be looked for"
REFUSED_BUSY = "the kernel still holds it"
REFUSED_PARTIAL = "partly removed"
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


def clone(c, source, identity, mode, kind="dir"):
    """A detached copy (open_tree) of the directory SOURCE (KIND file: the regular file SOURCE), refused unless it is
    still IDENTITY (device, inode): what was checked is what is mounted, whatever was swapped in on the way since.
    A file system of the kernel's own in it (proc, sysfs, cgroup ...) is yours to give: `workspace add` said so
    before (judged); a clone the kernel refuses fails with its error. Read-only for MODE ro; never
    set-user-ID or device files: without mount_setattr (Linux 5.12) nothing is added."""
    fd = os.open(source, os.O_PATH | (os.O_DIRECTORY if kind == "dir" else 0) | os.O_NOFOLLOW | os.O_CLOEXEC)
    tree = None
    try:
        info = os.fstat(fd)
        if kind == "file" and not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, f"{source} is no regular file: not added")
        if (info.st_dev, info.st_ino) != tuple(identity):
            raise OSError(errno.ESTALE, f"{source} changed after it was checked; add it again")
        tree = check(c.syscall(SYS_OPEN_TREE, fd, b"", OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | AT_RECURSIVE
                               | AT_EMPTY_PATH), f"clone {source}")
    except BaseException:
        if tree is not None:
            os.close(tree)
        raise
    finally:
        os.close(fd)
    attributes = MountAttr(MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV | (MOUNT_ATTR_RDONLY if mode == "ro" else 0), 0, 0, 0)
    result = c.syscall(SYS_MOUNT_SETATTR, tree, b"", AT_EMPTY_PATH | AT_RECURSIVE, ctypes.byref(attributes),
                       ctypes.sizeof(attributes))
    if result < 0:
        code = ctypes.get_errno()
        os.close(tree)
        if code == errno.ENOSYS:  # without it the tree would keep set-user-ID programs and device files: never
            raise OSError(code, "this kernel lacks mount_setattr (Linux 5.12 or later is needed to add a workspace "
                                "without set-user-ID programs and device files): restart the session with it "
                                "instead (sandbox-ai resume NAME --restart --workspace DIR)")
        ctypes.set_errno(code)
        check(result, f"make {source} {'read-only' if mode == 'ro' else 'nosuid'}", "5.12")
    return tree


def mount_point(name, create, kind="dir"):
    """(fd of /workspaces, fd of /workspaces/NAME or None) in the current mount namespace, neither through a link:
    the agent may have put anything there. CREATE makes the directory (KIND file: an empty regular file, the mount
    point of a file: the kernel mounts a file only on a file)."""
    base = os.open(WORKSPACES_DIR, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    what = "directory" if kind == "dir" else "regular file"
    try:
        if create and kind == "dir":
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o755, dir_fd=base)
        elif create:
            with contextlib.suppress(FileExistsError):  # O_EXCL: never through a link the agent put there
                os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                                 dir_fd=base))
        # O_PATH with O_NOFOLLOW opens a link itself: refused below (not a directory, not a regular file).
        target = os.open(name, os.O_PATH | (os.O_DIRECTORY if kind == "dir" else 0) | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=base)
    except FileNotFoundError:
        if create:
            os.close(base)
            raise
        return base, None
    except OSError as exc:
        os.close(base)
        raise OSError(exc.errno, f"{WORKSPACES_DIR}/{name} is no {what} in the sandbox (made there?)") from None
    if kind != "dir" and not stat.S_ISREG(os.fstat(target).st_mode):
        os.close(target)
        os.close(base)
        raise OSError(errno.ENOTDIR, f"{WORKSPACES_DIR}/{name} is no {what} in the sandbox (made there?)")
    return base, target


def mounted(base, target):
    """Something other than the /workspaces tmpfs is mounted at TARGET."""
    return os.fstat(base).st_dev != os.fstat(target).st_dev


def start_time(pid, proc=PROC):
    """The start time of PID (/proc/PID/stat field 22, clock ticks since boot), or None (gone): with the PID it names
    one process, never another that got the PID later."""
    try:
        text = (Path(proc) / str(pid) / "stat").read_text(errors="replace")
    except OSError:
        return None
    fields = text.rpartition(")")[2].split()  # the command (field 2) may hold spaces and parentheses
    return int(fields[19]) if len(fields) > 19 and fields[19].isdigit() else None


class Unsure(OSError):
    """An add that failed and whose rollback failed as well: it may still be mounted somewhere."""


class Partial(OSError):
    """A removal that stopped half-way and could not be undone: GONE are the pids of the namespaces it left."""

    def __init__(self, code, text, gone):
        super().__init__(code, text)
        self.gone = gone


def open_below(top, relative, kind):
    """O_PATH descriptor of RELATIVE below the folder descriptor TOP: every folder on the way a real folder, the
    entry itself of KIND (file: a regular file, dir: a folder), never a link (OSError otherwise)."""
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise OSError(errno.EINVAL, f"{relative}: not a path below the folder")
    fd = os.dup(top)
    try:
        for part in parts[:-1]:
            inner = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = inner
        target = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
                         | (os.O_DIRECTORY if kind == "dir" else 0), dir_fd=fd)
    finally:
        os.close(fd)
    mode = os.fstat(target).st_mode
    if not (stat.S_ISDIR(mode) if kind == "dir" else stat.S_ISREG(mode)):
        os.close(target)
        raise OSError(errno.ENOTDIR if kind == "dir" else errno.EINVAL,
                      f"{relative} is no {'folder' if kind == 'dir' else 'regular file'} now: not hidden, not added")
    return target


def staged(c, source, identity, mode, hidden, blanks, stage, count):
    """COUNT detached trees of the directory SOURCE (still IDENTITY) with the empty read-only BLANKS ({"file"|"dir":
    [path, identity]}) mounted over each of HIDDEN ([{"path", "kind"}]): the clone is mounted on STAGE in the
    helper's own mount namespace, the blanks on it, and each tree is a recursive clone of that (open_tree
    AT_RECURSIVE: the blanks come with it). The sandbox never sees the folder without them."""
    tree = clone(c, source, identity, mode, "dir")
    try:
        check(c.syscall(SYS_MOVE_MOUNT, tree, b"", AT_FDCWD, os.fsencode(stage), MOVE_MOUNT_F_EMPTY_PATH),
              "mount the folder on its staging folder")
    finally:
        os.close(tree)
    top = os.open(stage, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    trees = []
    try:
        for item in hidden:
            kind = "dir" if item.get("kind") == "dir" else "file"
            blank = clone(c, blanks[kind][0], blanks[kind][1], "ro", kind)
            try:
                target = open_below(top, item["path"], kind)
                try:
                    check(c.syscall(SYS_MOVE_MOUNT, blank, b"", target, b"",
                                    MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH), f"hide {item['path']}")
                finally:
                    os.close(target)
            finally:
                os.close(blank)
        for _ in range(count):
            trees.append(check(c.syscall(SYS_OPEN_TREE, top, b"", OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | AT_RECURSIVE
                                         | AT_EMPTY_PATH), "clone the folder with its hidden files"))
    except BaseException:
        for fd in trees:
            os.close(fd)
        raise
    finally:
        os.close(top)
    return trees


def helper(op, user_pid, name, targets, source=None, identity=None, mode="rw", proc=PROC, c=None, force=False,
           starts=None, kind="dir", hidden=(), blanks=None, stage=None):
    """In this (fresh, single-threaded) process: enter the user namespace of USER_PID, then mount SOURCE (op add)
    at /workspaces/NAME, or unmount it there (op remove), in the mount namespace of every pid in TARGETS (KIND: dir,
    or file for a single regular file, whose mount point is an empty file). STARTS
    ({pid: start time}) are the processes' start times when they were found: a PID that names another process now
    is refused. An add that fails in one namespace is undone in those it reached; a removal that fails in one is
    undone in those it left (mounted again from the one where it failed), else Partial."""
    c = c or libc()
    user = os.open(proc / str(user_pid) / "ns" / "user", os.O_RDONLY | os.O_CLOEXEC)
    namespaces = [os.open(proc / str(pid) / "ns" / "mnt", os.O_RDONLY | os.O_CLOEXEC) for pid in targets]
    if starts is not None:  # checked after the namespaces were opened: what the descriptors hold is that process's
        for pid in [user_pid, *targets]:
            if start_time(pid, proc) is None or start_time(pid, proc) != starts.get(str(pid)):
                raise OSError(errno.ESRCH, f"process {pid} of the sandbox is gone (its PID may name another process "
                                           "now): try again")
    check(c.setns(user, CLONE_NEWUSER), "enter the sandbox's user namespace")
    trees = []
    if op == "add":
        # A mount namespace of our own, owned by the sandbox's user namespace, may clone the host's directory;
        # each namespace needs a clone of its own (a detached tree is attached once).
        check(c.unshare(CLONE_NEWNS), "a private mount namespace")
        if hidden and kind == "dir":  # --hide: the blanks over its matches before the sandbox sees it (staged)
            trees = staged(c, source, identity, mode, hidden, blanks, stage, len(namespaces))
        else:
            trees = [clone(c, source, identity, mode, kind) for _ in namespaces]
    reached = []
    try:
        for index, fd in enumerate(namespaces):
            check(c.setns(fd, CLONE_NEWNS), "enter a mount namespace of the sandbox")
            if op == "add":
                attach(c, name, trees[index], kind)
            else:
                detach(c, name, force, kind, hidden)
            reached.append(index)
    except OSError as exc:
        if op == "add":
            failed = False
            for index in reached:
                try:
                    check(c.setns(namespaces[index], CLONE_NEWNS), "enter a mount namespace of the sandbox")
                    detach(c, name, True, kind)
                except OSError:
                    failed = True
            if failed:
                raise Unsure(exc.errno or errno.EIO, f"{exc.strerror or exc}; and it could not be taken out again "
                             "of every mount namespace it reached") from None
            raise
        if not reached:
            raise
        lost = restore(c, name, [namespaces[index] for index in reached], kind)
        if lost:
            gone = [targets[index] for index in reached if namespaces[index] in lost]
            raise Partial(exc.errno or errno.EBUSY, f"{WORKSPACES_DIR}/{name} is {REFUSED_PARTIAL}: gone in the mount "
                          f"namespace of process {', '.join(map(str, gone))}, still there in the others "
                          f"({exc.strerror or exc}); sandbox-ai workspace remove --force takes it out of every one",
                          gone) from None
        raise
    return 0


def restore(c, name, namespaces, kind="dir"):
    """Mount /workspaces/NAME again in the mount namespaces NAMESPACES (descriptors) a removal had left, from clones
    of the mount in the current one, where the removal failed (a clone keeps read-only, nosuid and nodev); the
    descriptors of those where it could not."""
    trees = []
    try:
        base, target = place(name, False, kind)
    except OSError:
        return list(namespaces)
    try:
        if target is None or not mounted(base, target):
            return list(namespaces)
        for _ in namespaces:
            tree = c.syscall(SYS_OPEN_TREE, target, b"", OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | AT_RECURSIVE
                             | AT_EMPTY_PATH)
            if tree < 0:
                break
            trees.append(tree)
    finally:
        if target is not None:
            os.close(target)
        os.close(base)
    lost = list(namespaces[len(trees):])
    for fd, tree in zip(namespaces, trees):
        try:
            check(c.setns(fd, CLONE_NEWNS), "enter a mount namespace of the sandbox")
            attach(c, name, tree, kind)
        except OSError:
            lost.append(fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(tree)
    return lost


def place(name, create, kind="dir"):
    try:
        return mount_point(name, create, kind)
    except FileNotFoundError:
        raise OSError(errno.ENOENT, f"the sandbox has no {WORKSPACES_DIR} of its own (started by sandbox-ai 0.1.75 "
                                    "or older?): restart it with the workspace") from None


def attach(c, name, tree, kind="dir"):
    """Mount TREE at /workspaces/NAME of the current mount namespace (KIND file: on an empty file made there)."""
    base, target = place(name, True, kind)
    try:
        if mounted(base, target):
            raise OSError(errno.EBUSY, f"{WORKSPACES_DIR}/{name} is in use in the sandbox")
        check(c.syscall(SYS_MOVE_MOUNT, tree, b"", target, b"", MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH),
              f"mount {WORKSPACES_DIR}/{name}")
    finally:
        os.close(target)
        os.close(base)


def detach(c, name, force=False, kind="dir", hidden=()):
    """Unmount /workspaces/NAME of the current mount namespace if something is mounted there; remove the mount
    point once no namespace has a mount on it (the tmpfs is shared, rmdir fails while one has; a file's mount point
    is removed when this namespace has none: an unlink of a mount point another namespace still uses fails with
    EBUSY). Strictly first: the kernel refuses (EBUSY) while anything still holds the tree, also what no look at
    /proc shows (a descriptor in flight over a socket, io_uring); FORCE detaches it then (what is held stays usable
    until it is closed)."""
    base, target = place(name, False, kind)
    try:
        if target is None:
            return
        busy = mounted(base, target)
        os.close(target)
        target = None
        if busy:
            # --hide's blanks below it first (deepest first): a mount with mounts below it is never unmounted strictly.
            # One the agent moved away by renaming a folder above it is not found here: the strict unmount below
            # then refuses (--force detaches the whole tree).
            for relative in sorted(hidden or (), reverse=True):
                if isinstance(relative, str) and relative and not relative.startswith("/") and ".." not in \
                        PurePosixPath(relative).parts:
                    c.umount2(f"{WORKSPACES_DIR}/{name}/{relative}".encode(), UMOUNT_NOFOLLOW)
            path = f"{WORKSPACES_DIR}/{name}".encode()
            if c.umount2(path, UMOUNT_NOFOLLOW) < 0:
                code = ctypes.get_errno()
                if code != errno.EBUSY or not force:
                    if code == errno.EBUSY:
                        raise OSError(code, f"{WORKSPACES_DIR}/{name} is still in use in the sandbox: {REFUSED_BUSY} "
                                            "(an open file or directory, or a mount below it, that no look at /proc "
                                            "shows): --force detaches it anyway")
                    check(-1 if code else 0, f"unmount {WORKSPACES_DIR}/{name}")
                check(c.umount2(path, MNT_DETACH | UMOUNT_NOFOLLOW), f"unmount {WORKSPACES_DIR}/{name}")
        with contextlib.suppress(OSError):
            (os.rmdir if kind == "dir" else os.unlink)(name, dir_fd=base)
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


def run_helper(op, found, name, source=None, identity=None, mode="rw", force=False, proc=PROC, runner=None, kind="dir",
               hidden=None, blanks=None, stage=None):
    """Run helper() in a process of its own; (status, error text), and for status PARTIAL a third item:
    {"gone": [pids of the namespaces the removal left]}; for a failed add whose undo is not confirmed (killed on
    the way and the undo failed too, or its rollback failed): {"undone": False}."""
    runner = runner or subprocess.run
    targets = own_namespaces(found, proc)
    if not targets:
        return 1, "no mount namespace of the sandbox found"
    # Bare PIDs could name other processes by the time the helper opens them: it checks their start times.
    starts = {str(pid): start_time(pid, proc) for pid in [found["user"], *targets]}
    gone = [pid for pid, value in starts.items() if value is None]
    if gone:
        return 1, f"process {gone[0]} of the sandbox is gone (has it ended?)"
    request = {"op": op, "user": found["user"], "name": name, "targets": targets, "source": source,
               "identity": identity, "mode": mode, "force": force, "starts": starts, "kind": kind,
               **({"hidden": hidden} if hidden else {}), **({"blanks": blanks, "stage": stage} if blanks else {})}
    command = [sys.executable, "-I", str(Path(__file__)), "--internal-helper", json.dumps(request)]
    try:
        result = runner(command, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        text = "the helper did not finish within 30 s (a mount namespace that does not answer?)"
        if op != "add":
            return 1, text
        # Killed before its own rollback: take out what it may have mounted already; only an undo that finished
        # says so ({"undone": False} keeps the entry: add()).
        undo = [sys.executable, "-I", str(Path(__file__)), "--internal-helper",
                json.dumps({**request, "op": "remove", "source": None, "identity": None, "force": True,
                            "hidden": [item["path"] for item in hidden or [] if isinstance(item, dict)],
                            "blanks": None, "stage": None})]
        try:
            undone = runner(undo, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError):
            undone = False
        return 1, text + ("" if undone else "; taking it out again did not finish either"), {"undone": undone}
    if op == "add" and result.returncode not in (0, 1):  # (UNSURE, or the helper died some other way)
        return result.returncode, result.stderr.strip()[-500:], {"undone": False}
    if result.returncode == PARTIAL:
        detail = {"gone": []}
        with contextlib.suppress(ValueError, AttributeError):
            data = json.loads(result.stdout.strip().splitlines()[-1])
            detail["gone"] = [int(pid) for pid in data.get("gone", []) if isinstance(pid, int)]
        return PARTIAL, result.stderr.strip()[-500:], detail
    return result.returncode, result.stderr.strip()[-500:]


def clone_helper(user_pid, source, identity, starts, channel, proc=PROC, c=None):
    """In this (fresh, single-threaded) process: enter the user namespace of USER_PID (the sandbox's), make a mount
    namespace of its own, take a detached read-only clone (open_tree) of the host directory SOURCE (still IDENTITY:
    device, inode) and send its descriptor over the Unix socket CHANNEL (a descriptor number). STARTS: the start
    times of the processes when they were found (a PID that names another process now is refused)."""
    c = c or libc()
    user = os.open(proc / str(user_pid) / "ns" / "user", os.O_RDONLY | os.O_CLOEXEC)
    if start_time(user_pid, proc) is None or start_time(user_pid, proc) != (starts or {}).get(str(user_pid)):
        raise OSError(errno.ESRCH, f"process {user_pid} of the sandbox is gone (its PID may name another process "
                                   "now): try again")
    check(c.setns(user, CLONE_NEWUSER), "enter the sandbox's user namespace")
    check(c.unshare(CLONE_NEWNS), "a private mount namespace")
    tree = clone(c, source, identity, "ro")
    with socket.socket(fileno=channel) as stream:
        socket.send_fds(stream, [b"t"], [tree])
    os.close(tree)
    return 0


def detached_copy(record, source, identity, finder=None, proc=PROC, runner=None):
    """The descriptor of a detached, read-only clone (open_tree, OPEN_TREE_CLONE) of the host directory SOURCE (still
    IDENTITY: device, inode), made by a helper in the user namespace of the running sandbox RECORD: '..' from it
    leads nowhere, so whoever holds it (the run's supervisor, `sandbox-ai mcp attach --live`) reaches that folder and
    nothing else of the host. ValueError when it cannot be made (Linux 5.12 or later; the sandbox ended)."""
    found = (finder or finder_default)(record)
    if not found or not found.get("user"):
        raise ValueError("the sandbox's processes were not found (has it ended?)")
    user = found["user"]
    starts = {str(user): start_time(user, proc)}
    if starts[str(user)] is None:
        raise ValueError(f"process {user} of the sandbox is gone (has it ended?)")
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        request = {"op": "clone", "user": user, "source": str(source), "identity": list(identity), "starts": starts,
                   "channel": theirs.fileno()}
        command = [sys.executable, "-I", str(Path(__file__)), "--internal-helper", json.dumps(request)]
        try:
            result = (runner or subprocess.run)(command, capture_output=True, text=True, timeout=30,
                                                stdin=subprocess.DEVNULL, pass_fds=(theirs.fileno(),))
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"the helper did not run: {exc}") from None
        theirs.close()
        if result.returncode:
            raise ValueError(result.stderr.strip()[-500:] or "the helper failed")
        ours.settimeout(5)
        try:
            _, fds, _, _ = socket.recv_fds(ours, 16, 4)
        except OSError as exc:
            raise ValueError(f"the helper sent no descriptor: {exc}") from None
        if len(fds) != 1:
            for fd in fds:
                os.close(fd)
            raise ValueError("the helper sent no descriptor")
        os.set_inheritable(fds[0], False)
        return fds[0]
    finally:
        ours.close()
        theirs.close()


def helper_main(argument):
    request = json.loads(argument)
    if request.get("op") == "clone":
        return clone_helper(int(request["user"]), request["source"], request.get("identity"), request.get("starts"),
                            int(request["channel"]))
    starts = request.get("starts")
    kind = request.get("kind", "dir")
    if kind not in KINDS:
        raise OSError(errno.EINVAL, f"no such kind of workspace: {kind!r}")
    hidden = request.get("hidden") or []
    if request["op"] == "add":
        hidden = [item for item in hidden if isinstance(item, dict) and isinstance(item.get("path"), str)]
    else:
        hidden = [item for item in hidden if isinstance(item, str)]
    return helper(request["op"], int(request["user"]), request["name"], [int(p) for p in request["targets"]],
                  request.get("source"), request.get("identity"), request.get("mode", "rw"),
                  force=bool(request.get("force")), starts=starts if isinstance(starts, dict) else None, kind=kind,
                  hidden=hidden, blanks=request.get("blanks"), stage=request.get("stage"))


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
    """(fd, real path, identity, kind) of the directory (kind dir) or regular file (kind file) VALUE, opened once: the
    checks look at what this descriptor is, the helper mounts only that (clone). Anything else (a socket, a FIFO, a
    device) is refused: whoever reaches a socket gets what runs behind it, a device is --device's."""
    l = module("agent_sandbox")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("give a directory or a file")
    source = Path(os.path.normpath(Path(value).expanduser().absolute()))
    if any(c in str(source) for c in (",", "\n", "\r")) or not str(source).isprintable():
        raise ValueError("a workspace path cannot hold commas or control characters")
    if any(path.is_symlink() and not path.exists() for path in (source, *source.parents)):
        raise ValueError("the workspace path contains a dangling symlink")
    # An agent of an earlier run may have planted a link to get a host directory mounted read-write.
    l.check_planted(str(source), "Workspace", l.exposed_before())
    try:
        fd = os.open(source, os.O_PATH | os.O_CLOEXEC)  # O_PATH: a FIFO is not opened for reading (never waited on)
    except FileNotFoundError:
        raise ValueError(f"{shown(source)}: no such directory or file") from None
    except NotADirectoryError:
        raise ValueError(f"{shown(source)}: not a directory") from None
    real = Path(os.readlink(f"/proc/self/fd/{fd}"))
    info = os.fstat(fd)
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else None
    if kind is None or not real.is_absolute():
        os.close(fd)
        what = ("a socket: whoever connects to it gets what runs behind it" if stat.S_ISSOCK(info.st_mode) else
                "a device (use --device for a device)" if stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)
                else "a FIFO" if stat.S_ISFIFO(info.st_mode) else "not a directory or a regular file")
        raise ValueError(f"{shown(source)}: {what}; only a directory or a regular file is added")
    return fd, real, [info.st_dev, info.st_ino], kind


def judged(record, real, read_only=False, kind="dir", warn=None):
    """The mode (ro or rw) RUN gives the workspace REAL (resolved; KIND dir or file), or ValueError: the start's
    checks of a workspace, with what run.json recorded of the run (workspace_checks). A risk you chose (HOME,
    sandbox-ai's own folders or launcher, the kernel's trees, what the launcher reads) is said with WARN (default:
    the launcher's warner, standard error and launcher.log), never refused."""
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
    # You decide what a sandbox gets: each risk below is said (WARN, default: the launcher's warner), never refused.
    say = l.warner(warn)
    # sandbox-ai's own folders as the start saw them too (another XDG_* environment than this command's); the
    # kernel's and the devices' trees (the launcher's KERNEL_TREES), whatever a start's version checks.
    l.check_workspace_scope(real, warn=say, refused=[Path(value) for value in checks.get("own_folders", [])
                                                     if isinstance(value, str) and os.path.isabs(value)])
    # The launcher the start runs (a checkout, a venv) and its package, not only this command's.
    if not read_only and not l.check_package_location(real, "Workspace", [(Path(value), str(label)) for value, label
                                                                           in (entry for entry in
                                                                               checks.get("packages", [])
                                                                               if isinstance(entry, list)
                                                                               and len(entry) == 2)
                                                                           if isinstance(value, str)
                                                                           and os.path.isabs(value)], warn=say):
        l.check_launcher_location(real, "Workspace", [Path(value) for value in checks.get("launcher", [])
                                                      if isinstance(value, str) and os.path.isabs(value)], warn=say)
    # A start lets a workspace hold the project's own .venv/bin or node_modules/.bin on your PATH, and checks what
    # the agent changed there after the run (tool_dirs_state); for a workspace added later nothing would.
    for directory in l.private_locations()[1]:
        if (directory == real or real in directory.parents or (kind == "file" and directory in real.parents)) \
                and not read_only:
            # You decide what a sandbox gets: given, with what it means (a start would check it after the run).
            print(f"sandbox-ai workspace: warning: {shown(real)} "
                  f"{'lies in' if kind == 'file' and directory in real.parents else 'holds'} {shown(directory)}, a "
                  "directory on your PATH: programs the agent puts there run on this host; nothing checks them "
                  "after the run", file=sys.stderr)
            break
    for entry in checks.get("workspaces", []) + present_workspaces(record):
        other = Path(entry["host"])
        if entry.get("kind") == "file" and replaced(entry) and other != real:
            continue  # its host path names another file now: no overlap with what is there now
        if overlaps(real, other):
            raise ValueError(f"workspaces must not overlap: {shown(real)} and {shown(other)} "
                             f"({shown(entry['sandbox'])})")
    for value in checks.get("trees", []):
        if overlaps(real, Path(value)):
            raise ValueError(f"{shown(real)} overlaps {shown(value)}, a mount, --state-dir or the HOME of the run")
    safer = l.SAFER_READ_ONLY.replace("--ro, a --mount with :ro", "--ro")
    for value, label in checks.get("held", []):
        path = Path(value)
        if path == real or real in path.parents:
            say(f"{shown(real)} holds {shown(value)} ({label}): the agent can change what the launcher reads there, "
                "and a link it puts in its place could make the launcher act on another file of yours; "
                f"{safer}, or keep it outside workspaces")
        # And the other way: a file or folder inside what the launcher reads (--privacy-rules, a --kit-dir): the
        # agent can change it as well (a --copy goes into the sandbox only).
        elif path in real.parents and label != "--copy":
            say(f"{shown(real)} lies in {shown(value)} ({label}), which the launcher reads: the agent can change it "
                f"there; {safer}, or keep workspaces outside it")
    for value, label in checks.get("private", []):
        if overlaps(real, Path(value)):
            say(f"{shown(real)} overlaps {shown(value)} ({label}), the run's own: the agent can read and change "
                f"what the launcher keeps there for this run (its log, its questions, its sockets); {safer}")
    l.check_stores([real], [(Path(value), label) for value, label in checks.get("stores", [])], warn=say)
    return "ro" if read_only or checks.get("read_only") else "rw"


def free_name(base, taken):
    """BASE, else BASE-2, BASE-3 ...: the first not in TAKEN (as a start names its workspaces)."""
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


def restart_names(record, extra=()):
    """{host path: name} a restart of the session gives the added workspaces it keeps (and EXTRA host paths after
    them): workspace_targets names the start's workspaces as now, then the kept ones in the order they were added
    (their --workspace options), each its directory's name, -2, -3 ... after the names taken before it."""
    checks = record.get("workspace_checks") or {}
    used = {PurePosixPath(entry["sandbox"]).name for entry in checks.get("workspaces", [])[1:]}
    kept = [entry for entry in sorted(present_workspaces(record), key=lambda e: e.get("since") or 0)
            if entry.get("kept")]
    # One added with --name keeps it (--workspace-name NAME=DIR in the session's options); the others' names are
    # given around those (workspace_targets).
    names = {entry["host"]: PurePosixPath(entry["sandbox"]).name for entry in kept if entry.get("named")}
    used |= set(names.values())
    for host in [*(entry["host"] for entry in kept if not entry.get("named")), *extra]:
        names[host] = free_name(PurePosixPath(host).name or "root", used)
        used.add(names[host])
    return names


def check_name(record, name):
    """NAME (workspace add --name) for /workspaces/NAME, or ValueError: a plain name (NAME) that no workspace of the
    run holds, nor one a restart gives a workspace the session keeps."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError(f"--name {shown(name)!r}: 1-64 letters, digits, '_', '.' or '-', starting with a letter or "
                         "a digit")
    checks = record.get("workspace_checks") or {}
    for entry in checks.get("workspaces", [])[1:] + present_workspaces(record):
        if PurePosixPath(entry.get("sandbox") or "").name == name:
            raise ValueError(f"{WORKSPACES_DIR}/{name} is taken by {shown(entry.get('host'))}: choose another --name")
    if name in restart_names(record).values():
        raise ValueError(f"{WORKSPACES_DIR}/{name} is where a workspace the session keeps comes back after a "
                         "restart: choose another --name")
    return name


def sandbox_name(record, real, keeping=False, kind="dir"):
    """(name, its name after a restart or None) of /workspaces/<name> for REAL. One the session keeps (KEEPING) gets
    the name a restart gives it (restart_names), so that it is at the same place then. One that is not kept while
    the run has a session (added read-only) is named <name>-ro (-ro-2 ...): it never takes the name a later kept one
    gets back at a restart. Else (no session: no restart) its name, -2, -3 ... after the names taken. The second
    item names the place after a restart when it cannot be the same (another workspace holds that name now: one
    that is not kept, or a kept one that a restart puts elsewhere, e.g. after a removal)."""
    checks = record.get("workspace_checks") or {}
    taken = {PurePosixPath(entry["sandbox"]).name for entry in checks.get("workspaces", [])[1:]}
    taken |= {PurePosixPath(entry["sandbox"]).name for entry in present_workspaces(record)}
    base = real.name or "root"
    if keeping:
        later = restart_names(record, [str(real)])[str(real)]
        if later not in taken:
            return later, None
        return free_name(base, taken), later
    if kind == "file":  # never kept (a start takes no file as a workspace): its own name
        return free_name(base, taken), None
    return free_name(f"{base}-ro" if session_of(record) is not None else base, taken), None


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
    except (OSError, ValueError, RecursionError):
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


def added_workspaces(record, kind=None):
    """The attached workspaces (KIND dir or file: only those; an entry of an older version is a directory)."""
    return [entry for entry in load(record) if entry.get("state") == "attached"
            and (kind is None or entry.get("kind", "dir") == kind)]


def present_workspaces(record):
    """The added workspaces that are, or may be, mounted (pending, attached, partial): what names, overlaps and
    `remove` go by."""
    return [entry for entry in load(record) if entry.get("state") in PRESENT]


def replaced(entry):
    """An added file whose host path names another file now (its device and inode changed, or it is gone): an
    editor that saves by writing a new file and renaming it over the old one. The sandbox still sees the file it
    was given; `workspace list` says "replaced", `add` of the path takes the old one out and adds the new one."""
    if entry.get("kind") != "file" or entry.get("state") != "attached" or not isinstance(entry.get("identity"), list):
        return False
    try:
        info = os.stat(entry["host"], follow_symlinks=False)
    except (OSError, KeyError, TypeError):
        return True
    return [info.st_dev, info.st_ino] != list(entry["identity"])


def publish_push_rules(record, entries=None):
    """push-added.json in the run's push check directory (run.json "push_check"): every attached workspace with the
    run's derived rules and its project's own, so the push check takes pushes of its commits. True if written, None
    if the run has no push check, False (and why) if it could not: then its pushes stay refused."""
    push = record.get("push_check") if isinstance(record.get("push_check"), dict) else None
    if not push or not push.get("dir"):
        return None, None
    privacy = module("privacy")
    directory = Path(push["dir"])
    try:
        config = json.loads((directory / "push.json").read_text())
        derived = privacy.parse_rules(config.get("derived") or {}, "derived")
        workspaces = []
        for entry in added_workspaces(record, "dir") if entries is None else entries:
            rules = privacy.builtin_rules().add(derived).add(privacy.user_rules(push.get("rules_dir"),
                                                                               Path(entry["host"]).name))
            workspaces.append({"path": entry["sandbox"], "rules": privacy.rules_document(rules)})
        temporary = directory / f".{privacy.ADDED_PUSH}.{os.getpid()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with open(fd, "w") as stream:
            json.dump({"workspaces": workspaces}, stream)
        os.replace(temporary, directory / privacy.ADDED_PUSH)
        return True, None
    except (OSError, ValueError, AttributeError, RecursionError) as exc:
        with contextlib.suppress(OSError, UnboundLocalError):
            temporary.unlink()
        return False, exc


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


def keep(record, host, add=True, named=None):
    """Add HOST to the saved options of the run's session (or take out what `add` put there); True if kept. NAMED:
    its name, kept as --workspace-name NAMED=HOST beside --workspace HOST (a restart puts it at /workspaces/NAMED)."""
    name = session_of(record)
    if name is None:
        return False
    sessions = module("sessions")
    with sessions.session_lock(name):
        data = sessions.load(name)
        options = list(data.get("options", []))
        if add:
            data["options"] = sessions.added_options(data, workspaces=[host])
            if named:
                data["options"] = [*data["options"], "--workspace-name", f"{named}={host}"]
        else:
            pairs = [i for i in range(len(options) - 1) if options[i] == "--workspace" and options[i + 1] == host]
            if not pairs:
                return False
            del options[pairs[-1]:pairs[-1] + 2]
            names = [i for i in range(len(options) - 1) if options[i] == "--workspace-name"
                     and options[i + 1].partition("=")[2] == host]
            if names:
                del options[names[-1]:names[-1] + 2]
            data["options"] = options
        sessions.save(name, data)
    return True


# --- a stopped kept start: what its next start mounts --------------------------------------------------------------


def stopped_start(entry):
    """True if ENTRY (sandbox-ai list: starts.entries) is a stopped start that keeps its HOME and options, so that
    `workspace add|remove` change what its next start (sandbox-ai resume) mounts."""
    return isinstance(entry, dict) and entry.get("state") == "stopped" and bool(entry.get("directory")) and \
        entry.get("kind") != "mcp" and bool(entry.get("keep"))


def next_start_targets(data, options, shared=None):
    """[(host path, sandbox path)] the session whose session.json is DATA mounts when it starts with OPTIONS. SHARED:
    a dict the caller keeps while it reads many sessions (starts.folders): the launcher's parser of each agent is
    built once into it instead of once per session."""
    l = module("agent_sandbox")
    sessions = module("sessions")
    parser = None
    if shared is not None and isinstance(data["agent"], str):
        if data["agent"] not in shared:
            shared[data["agent"]] = l.build_parser(data["agent"])
        parser = shared[data["agent"]]
    with sessions.in_directory(data["cwd"]):
        try:
            given = l.parse_args([data["agent"], *options], parser)
        except SystemExit:
            raise ValueError("these options do not go with the session's own; see sandbox-ai claude --help") from None
    return l.workspace_targets(given)


def keep_for_next_start(entry, value, read_only=False, name=None, out=print):
    """`workspace add NAME DIR` for the stopped kept start ENTRY (stopped_start): DIR goes into its saved options as
    `sandbox-ai resume NAME --workspace DIR` would put it there (--workspace DIR; with NAME also --workspace-name
    NAME=DIR), so that its next start mounts it at /workspaces/<name>, with every check of a start then. Nothing is
    mounted now. A file (a start takes none) and --ro (it would come back writable, unless the start keeps every
    workspace read-only) are refused with what to do instead."""
    sessions = module("sessions")
    key, shown_start = entry["id"], shown(entry["name"])
    source = remote_source(value)
    if source is None and module("remotefs").is_remote(value):
        module("remotefs").parse(value)  # (says what is wrong with it)
    if source is not None:
        real, kind = source, "dir"  # a remote folder: mounted by its next start (nothing is mounted now)
    else:
        fd, real, _identity, kind = opened(value)
        os.close(fd)
    if kind != "dir":
        raise ValueError(f"{shown_start} is stopped, and a file lasts for a run only (a start takes no file as a "
                         f"workspace): start it first (sandbox-ai resume {shown_start}), then add the file")
    if name is not None and (not NAME.fullmatch(name) or name in (".", "..")):
        raise ValueError(f"--name {shown(name)!r}: 1-64 letters, digits, '_', '.' or '-', starting with a letter or "
                         "a digit")
    with sessions.session_lock(key):
        data = sessions.load(key)
        still_stopped(key, data, shown_start)
        options = list(data.get("options", []))
        if read_only and not any(option in ("--workspace-ro", "-W") for option in options):
            raise ValueError(f"{shown_start} is stopped, and a workspace kept for its next start is mounted as its "
                             f"start mounts the others (writable: it is not started with --workspace-ro): start it "
                             f"first (sandbox-ai resume {shown_start}), then add it with --ro for that run")
        before = next_start_targets(data, options)
        if any(host_key(host) == str(real) for host, _ in before):
            raise ValueError(f"{shown(real)} is one of {shown_start}'s workspaces already (sandbox-ai resume "
                             f"{shown_start} mounts it)")
        if name is not None and any(PurePosixPath(place).name == name for _, place in before[1:]):
            raise ValueError(f"{WORKSPACES_DIR}/{name} is where another workspace of {shown_start} is mounted at its "
                             "next start: choose another --name")
        options = sessions.added_options(data, workspaces=[str(real)])
        if name is not None:
            options += ["--workspace-name", f"{name}={real}"]
        place = next((target for host, target in next_start_targets(data, options)
                      if host_key(host) == str(real)), None)
        data["options"] = options
        sessions.save(key, data)
    out(f"{shown_start} is stopped: {shown(real)} is kept for its next start, at {place or WORKSPACES_DIR} (rw); it "
        f"is mounted, with the checks of a start, when it starts again: sandbox-ai resume {shown_start}")
    return 0


def forget_for_next_start(entry, value, out=print):
    """`workspace remove NAME DIR|NAME` for the stopped kept start ENTRY (stopped_start): a workspace that `workspace
    add` (or resume --workspace) kept in its saved options (--workspace DIR) comes out of them, with its
    --workspace-name, so that its next start goes without it. The workspaces it was started with stay."""
    sessions = module("sessions")
    key, shown_start = entry["id"], shown(entry["name"])
    with sessions.session_lock(key):
        data = sessions.load(key)
        still_stopped(key, data, shown_start)
        options = list(data.get("options", []))
        real = host_key
        # Only a --workspace DIR of its own (what add and resume put there), never the first workspace option: that
        # one (or its comma list) is what the start was started with.
        given = [index for index, option in enumerate(options) if option in ("-w", "--workspace")
                 or option.startswith("--workspace=") or (option.startswith("-w") and not option.startswith("--"))]
        removable = {real(options[index + 1]): index for index in given[1:]
                     if options[index] == "--workspace" and index + 1 < len(options)}
        kept = [(host, place) for host, place in next_start_targets(data, options)[1:] if real(host) in removable]
        wanted = real(value) if "/" in value and not value.startswith(f"{WORKSPACES_DIR}/") else None
        if remote_source(value) is not None:
            wanted = remote_source(value)
        host = next((host for host, place in kept if real(host) == wanted or place == value
                     or PurePosixPath(place).name == value), None)
        if host is None:
            raise ValueError(f"{shown_start} is stopped, and no workspace kept for its next start is {shown(value)} "
                             f"(kept: {', '.join(shown(place) for _, place in kept) or 'none'}; the workspaces it was "
                             "started with stay)")
        index = removable[real(host)]
        del options[index:index + 2]
        names = [index for index in range(len(options) - 1) if options[index] == "--workspace-name"
                 and real(options[index + 1].partition("=")[2]) == real(host)]
        for index in reversed(names):
            del options[index:index + 2]
        data["options"] = options
        sessions.save(key, data)
    out(f"{shown_start} is stopped: {shown(host)} is no longer kept for its next start (sandbox-ai resume "
        f"{shown_start} starts without it)")
    return 0


def host_key(path):
    """A workspace option's value as compared: a remote source (sshfs://) in its canonical spelling, else the
    resolved path."""
    return remote_source(path) or os.path.realpath(os.path.expanduser(path))


def still_stopped(key, data, shown_start):
    """ValueError when the start KEY (session.json DATA) has started since the command found it stopped."""
    if module("sessions").status(key, data) != "stopped":
        raise ValueError(f"{shown_start} has started meanwhile: run the command again")


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
        watcher=None, name=None, remote=None):
    """Mount the host directory or regular file VALUE into the running sandbox RECORD at /workspaces/<name> (NAME:
    that name, see check_name); its sandbox path. REMOTE: the remote source (sshfs://...) VALUE is the mount point
    of (add_remote): kept for the session as that source, never as the mount point of this run."""
    l = module("agent_sandbox")
    fd, real, identity, kind = opened(value)
    globs = [glob for glob in record.get("hide") or [] if isinstance(glob, str)]
    hidden, hide_notes, staging = [], [], None
    if globs and kind == "dir":  # --hide: what the run's globs match here, hidden before the sandbox sees it
        found_hidden, hide_notes = module("hide").find(real, globs)
        hidden = [{"path": relative, "kind": what} for relative, what in found_hidden]
        hide_notes += [f"{relative} is hidden but Git tracks it: the agent can still read it from the repository "
                       f"(git show HEAD:{relative})"
                       for relative in module("hide").tracked(real, [item["path"] for item in hidden])]
    elif globs and module("hide").matcher(globs)(real.name):
        out(f"  warning: {shown(real.name)} matches --hide ({', '.join(shown(g, 60) for g in globs[:5])}): you added "
            "this very file, so the agent reads it")
    try:
        # The same file added before and replaced on the host since (an editor's save): the old one goes first.
        for old in [entry for entry in present_workspaces(record) if entry.get("host") == str(real) and
                    replaced(entry)]:
            out(f"  {shown(real)} was replaced on the host since it was added at {old['sandbox']}: taking that out "
                "first")
            remove(record, old["sandbox"], out=out, runner=runner, finder=finder)
        with locked(record):
            # A remote folder's mount point lies in sandbox-ai's own folder for this run: nothing of the local risks
            # applies to it (remotefs.warning said what does); overlaps are refused as for any workspace.
            mode = judged(record, real, read_only, kind, warn=(lambda _text: None) if remote else None)
            checks = record.get("workspace_checks") or {}
            # Kept in the session's options (see below) unless read-only without --workspace-ro, or a file (a start
            # takes no file as a workspace).
            keeping = kind == "dir" and session_of(record) is not None and \
                not (mode == "ro" and not checks.get("read_only"))
            named = name is not None
            if named:
                name, elsewhere = check_name(record, name), None
            else:
                name, elsewhere = sandbox_name(record, real, keeping, kind)
            found = finder(record)
            if not found:
                raise ValueError("the sandbox's processes were not found (has it ended?)")
            if mode == "rw":
                # A link planted in it later may be meant to get another directory mounted at a later start.
                (remember or (lambda trees: l.remember_exposed(None, trees)))([real])
            # Recorded before the mount (pending): a save failing afterwards, or a launcher killed on the way, never
            # leaves a mount the registry does not know (listed as pending, `remove` takes it out).
            entry = {"host": str(real), "sandbox": f"{WORKSPACES_DIR}/{name}", "mode": mode, "state": "pending",
                     "since": time.time(), "identity": identity, "kept": False, "kind": kind,
                     "named": named, **({"remote": remote} if remote else {}),
                     **({"hidden": [item["path"] for item in hidden]} if hidden else {})}
            if hidden:  # (made before the entry is saved: a failure here leaves no pending entry behind)
                staging, blanks = hide_staging(record)
            save(record, load(record) + [entry])
            if hidden:
                result = runner("add", found, name, str(real), identity, mode, kind=kind, hidden=hidden,
                                blanks=blanks, stage=str(staging / "stage"))
            else:
                result = runner("add", found, name, str(real), identity, mode, kind=kind)
            status, error = result[:2]
            if status:
                undone = (result[2] if len(result) > 2 and isinstance(result[2], dict) else {}).get("undone", True)
                if undone is not True:
                    # Killed on the way (or its rollback failed) and the undo not confirmed: it may be mounted in
                    # some namespaces. The entry stays (pending, with why), so that names, overlaps and `remove`
                    # still know it.
                    note = "the add failed and its undo was not confirmed: it may be mounted"
                    with contextlib.suppress(OSError):
                        entries = load(record)
                        for other in entries:
                            if other.get("sandbox") == entry["sandbox"] and other.get("state") == "pending":
                                other["note"] = note
                        save(record, entries)
                    raise ValueError(f"could not add {shown(real)}: {error or 'the helper failed'}; {note} at "
                                     f"{entry['sandbox']}: it stays listed (pending) until sandbox-ai workspace "
                                     f"remove --force {entry['sandbox']} takes it out")
                with contextlib.suppress(OSError):  # the helper undid its mounts: the pending entry goes
                    save(record, [other for other in load(record) if not (other.get("sandbox") == entry["sandbox"]
                                                                         and other.get("state") == "pending")])
                raise ValueError(f"could not add {shown(real)}: {error or 'the helper failed'}")
            entry["state"] = "attached"
            try:
                entries = load(record)
                for other in entries:
                    if other.get("sandbox") == entry["sandbox"] and other.get("state") == "pending":
                        other["state"] = "attached"
                save(record, entries)
            except OSError as exc:
                raise ValueError(f"{shown(real)} is mounted at {entry['sandbox']}, but the run's registry could not "
                                 f"be written ({exc.strerror or exc}): it is listed as pending; sandbox-ai workspace "
                                 f"remove {entry['sandbox']} takes it out") from None
            pushed, why = publish_push_rules(record) if kind == "dir" else (None, None)
    finally:
        os.close(fd)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    if not keeping:
        # Kept as --workspace it would come back writable at a restart (only --workspace-ro or the mode make further
        # workspaces read-only): this one lasts for the run. A file: a start takes none as a workspace.
        if session_of(record) is not None and kind == "file":
            out("  not kept for the session's next start: a file lasts for this run (add it again after a restart)")
        elif session_of(record) is not None:
            out("  not kept for the session's next start: it was added read-only (add it again after a restart, or "
                "start with --workspace-ro)")
    else:
        try:
            entry["kept"] = keep(record, remote or str(real), named=name if named else None)
        except (OSError, ValueError) as exc:
            out(f"  not kept for the session's next start: {shown(exc, 500)}")
    if entry["kept"]:
        with locked(record):
            entries = load(record)
            for other in entries:
                if other.get("sandbox") == entry["sandbox"] and other.get("state") == "attached":
                    other["kept"] = True
            save(record, entries)
    # The file log watches directories (a file added is not in it).
    watched = (watcher or watch_file_log)(record, entry["sandbox"], out) if kind == "dir" else None
    log(record, "add", entry, kept=entry["kept"], watched=watched, kind=kind if kind != "dir" else None)
    push_note = ("; the push check takes its commits" if pushed else
                 "; its commits cannot be pushed from the sandbox (the push check could not take it: "
                 f"{shown(why, 300)}): push them from the host" if pushed is False else "")
    out(f"added {'the file ' if kind == 'file' else ''}{shown(remote or real)} to {shown(record.get('run'))} at "
        f"{entry['sandbox']} ({mode}{', kept for the session' if entry['kept'] else ''}){push_note}")
    if hidden:
        paths = [f"{entry['sandbox']}/{item['path']}" for item in hidden]
        out(f"  hidden from the agent (--hide): {', '.join(shown(path) for path in paths[:10])}"
            + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")
            + " (an empty read-only file or folder in their place; files made later are not hidden)")
    elif globs and kind == "dir":
        out(f"  --hide {', '.join(shown(glob, 60) for glob in globs[:5])}: nothing matches in it now")
    for note in hide_notes:
        out(f"  warning: {entry['sandbox']}/{shown(note, 400)}")
    if entry["kept"] and elsewhere:
        holder = next((other for other in present_workspaces(record)
                       if PurePosixPath(other.get("sandbox") or "").name == elsewhere), None)
        if holder is not None and holder.get("kept"):
            why = (f"{shown(holder.get('host'))}, kept for the session too, holds that name now; a restart puts it "
                   "at its own place")
        elif holder is not None:
            why = f"{shown(holder.get('host'))} holds that name now; it is not kept for the session"
        else:
            why = "another workspace holds that name now"
        out(f"  after a restart it is at {WORKSPACES_DIR}/{elsewhere} ({why})")
    return entry["sandbox"]


def hide_staging(record):
    """(a private folder of the run's, {"file"|"dir": [path, identity]}) for --hide in an added directory: the empty
    read-only file and folder put over what the globs match (as a start's, hide.py) and the staging folder the
    helper mounts the clone on in its own mount namespace. Removed after the add."""
    parent = record.get("directory")
    folder = Path(tempfile.mkdtemp(prefix=".hide-", dir=parent if parent and os.path.isdir(parent) else None))
    (folder / "stage").mkdir(mode=0o700)
    (folder / "file").write_bytes(b"")
    (folder / "dir").mkdir()
    (folder / "file").chmod(0o444)
    (folder / "dir").chmod(0o555)
    return folder, {kind: [str(folder / kind), [(folder / kind).stat().st_dev, (folder / kind).stat().st_ino]]
                    for kind in ("file", "dir")}


def remote_source(value):
    """The canonical remote source (sshfs://...) VALUE names, or None for anything else (also a damaged one)."""
    remote = module("remotefs")
    if not remote.is_remote(value):
        return None
    try:
        return remote.parse(value)["source"]
    except ValueError:
        return None


def add_remote(record, value, read_only=False, out=print, name=None, interactive=None, mounter=None,
               unmounter=None, **given):
    """`workspace add [RUN] sshfs://...`: the remote folder VALUE mounted on this host with sshfs (in sandbox-ai's
    own folder for the run, remotefs.plan; MOUNTER(entry, interactive), default remotefs.mount), then added as that
    folder (add, with GIVEN: every check of a workspace); kept for the session as its source. Unmounted again when
    the add fails (UNMOUNTER(run, points), default remotefs.release), by `workspace remove` and at the end of the
    run. A password or passphrase is asked only in a terminal (INTERACTIVE, default: standard input is one). Its
    sandbox path."""
    remote = module("remotefs")
    spec = remote.parse(value)
    checks = record.get("workspace_checks") if isinstance(record.get("workspace_checks"), dict) else {}
    if any(entry.get("remote") == spec["source"] for entry in present_workspaces(record)):
        raise ValueError(f"{shown(spec['source'])} is added already (sandbox-ai workspace list)")
    run = record.get("run")
    mode = "ro" if read_only or checks.get("read_only") else "rw"
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    entry = remote.plan(run, spec["source"], mode, by="add")
    try:
        out(f"mounting {shown(spec['source'])} on this host")
        (mounter or remote.mount)(entry, interactive=interactive)
        remote.update(run, entry["point"], state="mounted", via=entry.get("via"))
        out(f"  warning: {remote.warning(entry, 'it')}")
        return add(record, entry["point"], read_only, out=out, name=name, remote=spec["source"], **given)
    except BaseException:
        (unmounter or (lambda run, points: remote.release(run, only=points, interactive=interactive)))(
            run, [entry["point"]])
        raise


def matching(record, value):
    """The added workspace VALUE names: its sandbox path, its name, its host path, or any directory (relative paths
    too) that resolves to its host path. A value that names one workspace by name and another as a directory is
    refused (ambiguous)."""
    entries = present_workspaces(record)
    resolved = None
    if not value.startswith(WORKSPACES_DIR + "/") and remote_source(value) is None:
        candidate = Path(value).expanduser()
        with contextlib.suppress(OSError, RuntimeError):
            if value.startswith(("/", "~", ".")) or candidate.is_dir():
                resolved = str(candidate.resolve())
    source = remote_source(value)
    by_name = [e for e in entries if value in (e["sandbox"], PurePosixPath(e["sandbox"]).name, e["host"])
               or (source is not None and e.get("remote") == source)]
    by_directory = [e for e in entries if resolved is not None and resolved == e["host"]]
    found = {e["sandbox"]: e for e in by_name + by_directory}
    if len(found) > 1:
        raise ValueError(f"{shown(value)} is ambiguous: it names {', '.join(sorted(found))} (by name and as the "
                         f"directory {shown(resolved)}); give the sandbox path (/workspaces/NAME) or the full host path")
    if found:
        return next(iter(found.values()))
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


def remove(record, value, force=False, out=print, runner=run_helper, finder=finder_default, using=users, taken=None,
           unmounter=None):
    taken = taken or copies
    with locked(record):
        entry = matching(record, value)
        if entry.get("kind", "dir") not in KINDS:
            raise ValueError(f"{entry['sandbox']}: an unknown kind of workspace ({shown(entry.get('kind'))})")
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
                raise ValueError(f"{entry['sandbox']}: {REFUSED_UNKNOWN} ({unknown[0]}); --force takes it out of the "
                                 "sandbox's own view anyway (only a restart is sure)")
            if held and not force:
                raise ValueError(f"{entry['sandbox']}: {REFUSED_COPY} (process "
                                 f"{', '.join(str(pid) for pid, _ in held[:8])}): no unmount reaches it there, only a "
                                 "restart of the sandbox takes it away. --force takes it out of the sandbox's own view")
            if busy and not force:
                shown_busy = "; ".join(f"{pid} {shown(command, 40)} ({what})" for pid, command, what in busy[:8])
                raise ValueError(f"{entry['sandbox']} is in use in the sandbox: {shown_busy}"
                                 f"{' ...' if len(busy) > 8 else ''}: {REFUSED_IN_USE}. Let the agent leave it, or "
                                 "--force (what is open stays usable, also for writing, until it is closed; only a "
                                 "restart is sure)")
            result = runner("remove", found, PurePosixPath(entry["sandbox"]).name, force=force,
                            kind=entry.get("kind", "dir"),
                            **({"hidden": list(entry["hidden"])} if entry.get("hidden") else {}))
        status, error = result[:2]
        if status == PARTIAL:
            # Gone in some of the sandbox's namespaces, still there in others: the registry says so, and which.
            gone = (result[2] if len(result) > 2 and isinstance(result[2], dict) else {}).get("gone", [])
            entries = load(record)
            for other in entries:
                if other.get("sandbox") == entry["sandbox"] and other.get("state") in PRESENT:
                    other.update(state="partial", gone_in=sorted(set(other.get("gone_in", [])) | set(gone)),
                                 partial_since=time.time())
            save(record, entries)
            publish_push_rules(record)  # half there: no longer offered to the push check (only attached ones are)
            log(record, "remove-partial", entry, gone_in=gone)
            raise ValueError(f"could not remove {entry['sandbox']}: {error or REFUSED_PARTIAL}")
        if status:
            raise ValueError(f"could not remove {entry['sandbox']}: {error or 'the helper failed'}")
        entries = load(record)
        for other in entries:
            if other.get("sandbox") == entry["sandbox"] and other.get("state") in PRESENT:
                other.update(state="removed", removed=time.time())
        save(record, entries)
        publish_push_rules(record)
    kept = False
    if entry.get("kept"):
        try:
            kept = keep(record, entry.get("remote") or entry["host"], add=False)  # its --workspace-name with it
        except (OSError, ValueError) as exc:
            out(f"  still in the session's options: {shown(exc, 500)}")
        if kept:  # the kept ones after it are named anew at a restart
            for host, later in restart_names(record).items():
                now = next((e["sandbox"] for e in present_workspaces(record) if e["host"] == host), None)
                if now and now != f"{WORKSPACES_DIR}/{later}":
                    out(f"  after a restart {shown(host)} is at {WORKSPACES_DIR}/{later} (now {now})")
    log(record, "remove", entry, forced=force or None, open=[pid for pid, _, _ in busy] or None)
    if held:
        out(f"  warning: the agent still holds {entry['sandbox']} in a namespace of its own (process "
            f"{', '.join(str(pid) for pid, _ in held[:8])}): it keeps the host directory until the sandbox is restarted")
    if force:
        out("  warning: detached by force: whatever a process in the sandbox still holds there stays usable, also "
            "for writing, until it is closed" + (f" (open in {', '.join(str(pid) for pid, _, _ in busy)})"
                                                 if busy else "") + "; a restart takes it away for sure")
    if entry.get("remote"):
        # A remote folder: unmounted on this host as well (what the agent still holds stays usable until closed).
        for source, point, why in (unmounter or (lambda run, points: module("remotefs").release(run, only=points)))(
                record["run"], [entry["host"]]):
            if why:
                out(f"  warning: {shown(source or point)} could not be unmounted on this host ({shown(why, 300)}); "
                    "it is unmounted when the run ends")
    out(f"removed {entry['sandbox']} ({shown(entry.get('remote') or entry['host'])}) from {shown(record.get('run'))}"
        f"{' and from the session options' if kept else ''}; what the agent may still hold in a namespace of its own "
        "(a copy no table shows) only a restart takes away for sure")


def listing(record):
    checks = record.get("workspace_checks") or {}
    # The start's remote folders (sshfs://): their source by mount point (run.json "remote").
    remote = {entry.get("point"): entry.get("source") for entry in record.get("remote") or []
              if isinstance(entry, dict)}
    result = [{"host": e.get("host"), "sandbox": e.get("sandbox"), "mode": e.get("mode"), "source": "start",
               "kind": "dir", **({"remote": remote[e.get("host")]} if remote.get(e.get("host")) else {})}
              for e in checks.get("workspaces", [])]
    if not checks and record.get("workspace"):
        result.append({"host": record["workspace"], "sandbox": "/work", "mode": None, "source": "start", "kind": "dir"})
    for entry in present_workspaces(record):
        row = {"host": entry["host"], "sandbox": entry["sandbox"], "mode": entry["mode"], "source": "added",
               "kind": entry.get("kind", "dir"), "named": bool(entry.get("named")),
               "kept": bool(entry.get("kept")), "state": entry["state"],
               **({"remote": entry["remote"]} if isinstance(entry.get("remote"), str) else {}),
               "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.get("since") or 0))}
        if entry["state"] == "partial":
            row["gone_in"] = [pid for pid in entry.get("gone_in", []) if isinstance(pid, int)]
        if replaced(entry):
            # The sandbox keeps the file it was given; add the path again for the new one (add takes the old out).
            row["state"] = "replaced"
            row["note"] = "the host path names another file now (saved by an editor?): add it again"
        if isinstance(entry.get("note"), str):
            row["note"] = entry["note"]
        result.append(row)
    return result


def list_lines(record, entries):
    rows = [["SANDBOX", "MODE", "SOURCE", "HOST"]]
    for entry in entries:
        source = entry["source"] + (", file" if entry.get("kind") == "file" else "") + \
            (", kept" if entry.get("kept") else "") + \
            (f", {entry['state']}" if entry.get("state") not in (None, "attached") else "")
        rows.append([entry["sandbox"], entry["mode"] or "-", source + (", remote" if entry.get("remote") else ""),
                     shown(entry.get("remote") or entry["host"], 500)])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[-1] for row in rows]


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def main(argv, out=print):
    net = module("net")
    parser = argparse.ArgumentParser(prog="sandbox-ai workspace", description=__doc__.split("\n\n")[0],
                                     formatter_class=help_style,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:6]))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("review", help="what a run changed that your host runs or trusts, with diffs; keep or "
                                       "restore each file",
                        parents=[module("workspace_review").parser(add_help=False)],
                        description="What the agent changed in a workspace that your host runs or trusts (Git hooks "
                                    "and configuration, editor tasks, agent settings, build scripts, CI workflows, "
                                    "instruction files), with the diffs; per file, keep the change or restore the "
                                    "file as it was at the start.")
    lister = commands.add_parser("list", help="the run's workspaces")
    lister.add_argument("run", nargs="?")
    lister.add_argument("--json", action="store_true", help="as JSON")
    adder = commands.add_parser("add", help="mount a host directory or file, or an sshfs:// folder, into the running sandbox")
    adder.add_argument("args", nargs="+", metavar="[RUN] DIR|FILE|sshfs://[USER@]HOST[:PORT]/PATH")
    adder.add_argument("--ro", action="store_true", help="read-only")
    adder.add_argument("--name", metavar="NAME", help="at /workspaces/NAME (default: the directory's or file's name); "
                                                      "kept for the session under that name")
    remover = commands.add_parser("remove", help="take an added workspace away again")
    remover.add_argument("args", nargs="+", metavar="[RUN] DIR|NAME")
    remover.add_argument("--force", action="store_true", help="also while a process in the sandbox uses it")
    args = parser.parse_args(argv)
    if args.command == "review":  # workspace_review.py, with the same options
        return module("workspace_review").main(list(argv[1:]), out)
    try:
        if args.command == "list":
            runs = net.running()
            if args.json and not args.run and not runs:
                out("[]")  # nothing runs: nothing to list (as net list --json), for the front ends to read
                return 0
            record = net.choose(runs, args.run, command="workspace list")
            entries = listing(record)
            out(json.dumps(entries, indent=2) if args.json else "\n".join(list_lines(record, entries)))
            return 0
        if len(args.args) > 2:
            raise ValueError("give [RUN] DIR|FILE" if args.command == "add" else "give [RUN] DIR|NAME")
        run, value = args.args if len(args.args) == 2 else (None, args.args[0])
        try:
            record = net.choose(net.running(), run, command=f"workspace {args.command}")
        except net.NotRunning as exc:
            # A stopped kept start: what its next start mounts (as resume --workspace would add it).
            if not stopped_start(exc.entry):
                raise
            if args.command == "add":
                return keep_for_next_start(exc.entry, value, args.ro, name=args.name, out=out)
            return forget_for_next_start(exc.entry, value, out=out)
        if args.command == "add" and module("remotefs").is_remote(value):
            add_remote(record, value, args.ro, out=out, name=args.name)
        elif args.command == "add":
            add(record, value, args.ro, out=out, name=args.name)
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
        except Partial as exc:
            print(exc.strerror or str(exc), file=sys.stderr)
            print(json.dumps({"gone": exc.gone}))
            sys.exit(PARTIAL)
        except Unsure as exc:
            print(exc.strerror or str(exc), file=sys.stderr)
            sys.exit(UNSURE)
        except OSError as exc:
            print(exc.strerror or str(exc), file=sys.stderr)
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001 (whatever it reached may still be mounted)
            print(f"the helper failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(UNSURE)
    sys.exit(main(sys.argv[1:]))
