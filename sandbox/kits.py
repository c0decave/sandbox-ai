#!/usr/bin/env python3
"""Kits, project memory and plugin declarations for agent sandboxes.

A kit is a directory with one subdirectory per agent (claude/, codex/, opencode/). Each holds
any of: skills/, agents/, commands/, prompts/ (whatever the agent knows), a memory template
(CLAUDE.md or AGENTS.md), marketplaces.json and hooks/ (the agent's hooks, which run only once
you approved them: see stage_hooks). Every sandbox gets the default kit
(~/.local/share/sandbox-ai/home/AGENT, created from the templates on first use) plus the kits
named with --kit NAME (~/.local/share/sandbox-ai/kits/NAME/AGENT) and --kit-dir DIR (DIR/AGENT).
Entries of all kits are merged; one name from two kits is an error, never silently replaced.
Skills, agents, commands and prompts reach the sandbox as a read-only snapshot.

Project memory (the agent's memory file and Claude's auto memory) lives per project in
~/.local/state/sandbox-ai/memory/PROJECT-HASH/AGENT. The first run of a project takes the memory
template from the kits once; afterwards the file belongs to the project. The sandbox works on a
copy; after the run, what the agent changed goes back to the project's memory, never to a kit.

Standard library only; the launcher passes in its own checks where host paths are involved.
"""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import tempfile
import time

HERE = Path(__file__).resolve().parent
AGENTS = ("claude", "codex", "opencode")
TEMPLATES = HERE / "templates"
# Shipped with sandbox-ai and part of every sandbox: the sandbox-ai skill. An entry of the same name in
# a kit replaces it (unlike two kits, which may not share a name).
BUILTIN = HERE / "builtin"
BUILTIN_LABEL = "built-in"
# Named kits shipped with sandbox-ai (--kit NAME), in BUILTIN_KITS/NAME/AGENT. A kit NAME of the user's
# for the same agent replaces it. Their plugins are installed only on request (plugins claude --kit NAME).
BUILTIN_KITS = BUILTIN / "kits"
# sandbox-ai's own Claude plugin marketplace, mounted read-only at the same place in every Claude sandbox.
MARKETPLACE = HERE / "marketplace"
MARKETPLACE_TARGET = "/sandbox/marketplace"

# Kit directory -> sandbox HOME directory, per agent; all of them read-only in the sandbox.
LAYOUT = {
    "claude": {"skills": ".claude/skills", "agents": ".claude/agents", "commands": ".claude/commands"},
    "codex": {"skills": ".agents/skills", "prompts": ".codex/prompts"},
    "opencode": {"skills": ".config/opencode/skills", "agents": ".config/opencode/agents",
                 "commands": ".config/opencode/commands"},
}
# (name in a kit, sandbox HOME path) of the agent's own memory file.
MEMORY_FILE = {"claude": ("CLAUDE.md", ".claude/CLAUDE.md"), "codex": ("AGENTS.md", ".codex/AGENTS.md"),
               "opencode": ("AGENTS.md", ".config/opencode/AGENTS.md")}
# Claude keeps its auto memory per working directory, named after the path with "/" as "-";
# the first workspace is always /work.
MEMORY_DIRS = {"claude": (".claude/projects/-work/memory",), "codex": (), "opencode": ()}
PLUGINS_FILE = "marketplaces.json"

KIT_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
PLUGIN_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
NPM_PLUGIN = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@[A-Za-z0-9._^~<>=*+-]{1,64})?"
KIT_FILES, KIT_BYTES, KIT_DEPTH = 5000, 64 << 20, 16
MEMORY_FILES, MEMORY_BYTES = 1000, 16 << 20
TEMPLATE_LIMIT = 1 << 20
DECLARATION_LIMIT = 256 << 10


def data_root():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai"


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def default_home(agent):
    """The default kit of AGENT: part of every sandbox unless --no-default-kit."""
    return data_root() / "home" / agent


def kits_dir():
    return data_root() / "kits"


def plugin_store(agent):
    """HOME of the sandbox that installs AGENT's plugins (sandbox-ai plugins AGENT)."""
    return data_root() / "plugins" / agent


def bad_name(name):
    """Why NAME cannot be a kit entry, or None. Hidden names are skipped without a word."""
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "its name holds control characters"
    if len(name) > 255:
        return "its name is too long"
    return None


def kit_path(name, agent):
    """The kit NAME for AGENT: the user's in the kits directory, else the built-in one of that name."""
    if not re.fullmatch(KIT_NAME, name):
        raise ValueError(f"--kit NAME: 1-64 letters, digits, '_', '.', '-' (not starting with '.' or '-'): {name!r}")
    path = kits_dir() / name / agent
    if not path.is_dir() and (BUILTIN_KITS / name / agent).is_dir():
        return BUILTIN_KITS / name / agent
    return path


def kit_names(root, agent=None):
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return []
    return [e.name for e in entries if re.fullmatch(KIT_NAME, e.name) and e.is_dir()
            and (agent is None or (Path(e.path) / agent).is_dir())]


def builtin_kits(agent):
    """Names of the kits shipped with sandbox-ai for AGENT that no kit of the user's replaces."""
    return [name for name in kit_names(BUILTIN_KITS, agent) if not (kits_dir() / name / agent).is_dir()]


def available_kits(agent=None, builtin=True):
    """Names of the kits in the kits directory (those with a directory for AGENT, if given), and of the
    built-in kits unless BUILTIN is false."""
    own = kit_names(kits_dir(), agent)
    return sorted(set(own) | set(kit_names(BUILTIN_KITS, agent) if builtin else ()))


def sources(a):
    """[(label, directory)] of the kits for this run: the built-in one, then the default kit and the others."""
    agent, result = a.agent, []
    if not a.no_default_kit and (BUILTIN / agent).is_dir():
        result.append((BUILTIN_LABEL, BUILTIN / agent))
    if not a.no_default_kit and default_home(agent).is_dir():
        result.append(("home", default_home(agent)))
    for name in dict.fromkeys(a.kit):
        path = kit_path(name, agent)
        if not path.is_dir():
            known = ", ".join(available_kits(agent)) or "none"
            if name in kit_names(BUILTIN_KITS):
                usable = ", ".join(other for other in AGENTS if (BUILTIN_KITS / name / other).is_dir())
                raise ValueError(f"--kit {name}: sandbox-ai's built-in kit {name} is not available for {agent} "
                                 f"(only for {usable})")
            raise ValueError(f"--kit {name}: {path} does not exist (kits for {agent}: {known})")
        result.append((f"kit {name}", path))
    for value in dict.fromkeys(a.kit_dir):
        path = Path(os.path.normpath(Path(value).expanduser().absolute())) / agent
        if not path.is_dir():
            raise ValueError(f"--kit-dir {value}: it has no {agent}/ directory")
        result.append((f"kit-dir {value}", path))
    return result


def scan(agent, kit_sources):
    """What the kits hold for AGENT: {"entries": {kind: {name: (label, path)}}, "memory": (label, path)
    or None, "plugins": [(label, path)], "notes": [...]}. A name in two kits is a ValueError."""
    entries = {kind: {} for kind in LAYOUT[agent]}
    memory, plugins, notes = None, [], []
    memory_name = MEMORY_FILE[agent][0]
    for label, root in kit_sources:
        try:
            children = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError as exc:
            raise ValueError(f"{label}: cannot read {root}: {exc.strerror}") from None
        for child in children:
            name, path = child.name, Path(child.path)
            if name.startswith("."):
                continue
            if name in entries:
                if not path.is_dir():
                    raise ValueError(f"{label}: {path} must be a directory")
                for item in sorted(os.scandir(path), key=lambda e: e.name):
                    if item.name.startswith("."):
                        continue
                    problem = bad_name(item.name)
                    if problem:
                        notes.append(f"{label}: {name}/ entry skipped: {problem}")
                        continue
                    if item.name in entries[name] and entries[name][item.name][0] == BUILTIN_LABEL:
                        notes.append(f"{label}: {name[:-1]} {item.name} replaces the built-in one")
                    elif item.name in entries[name]:
                        raise ValueError(f"{name[:-1]} {item.name!r} comes from both {entries[name][item.name][0]} "
                                         f"and {label}; remove or rename one")
                    if name == "skills" and not (Path(item.path) / "SKILL.md").is_file():
                        notes.append(f"{label}: skill {item.name} has no SKILL.md; the agent may ignore it")
                    entries[name][item.name] = (label, Path(item.path))
            elif name == memory_name:
                if not path.is_file():
                    raise ValueError(f"{label}: {path} must be a file")
                if memory:
                    raise ValueError(f"{memory_name} comes from both {memory[0]} and {label}; keep one")
                memory = (label, path)
            elif name == PLUGINS_FILE:
                plugins.append((label, path))
            elif name == HOOKS_DIR:
                continue  # the kit's hooks: stage_hooks, and only once approved
            else:
                known = ", ".join([*(f"{k}/" for k in LAYOUT[agent]), memory_name, PLUGINS_FILE, f"{HOOKS_DIR}/"])
                notes.append(f"{label}: {name} is not used for {agent} (known: {known})")
    return {"entries": entries, "memory": memory, "plugins": plugins, "notes": notes}


def summary(plan):
    """{kind: [names]} of a scan, for messages and the dry run."""
    return {kind: sorted(items) for kind, items in plan["entries"].items() if items}


def stage(agent, plan, staging, check):
    """Copy the kits' entries to STAGING (a fresh launcher-owned directory), one directory per kind;
    returns [(staged directory, sandbox path)] to mount read-only, and notes.

    CHECK(path, label) raises for a host path the agent could change. A kit entry that is a symlink is followed
    (its target checked too); below an entry nothing is followed (copy_tree); more than KIT_FILES files, KIT_BYTES
    bytes or KIT_DEPTH levels are errors."""
    mounts, notes, budget = [], [], {"files": 0, "bytes": 0}
    for kind, items in plan["entries"].items():
        if not items:
            continue
        target = staging / kind
        target.mkdir(mode=0o755)
        for name, (label, source) in items.items():
            copy_tree(source, target / name, check, label, budget, notes)
        mounts.append((target, f"/root/{LAYOUT[agent][kind]}"))
    return mounts, notes


def fd_path(fd, fallback):
    """Where the open file FD really is (Linux: /proc/self/fd; macOS: F_GETPATH), else FALLBACK."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        pass
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None:
        try:
            return Path(fcntl.fcntl(fd, getpath, bytes(1024)).split(b"\0", 1)[0].decode())
        except (OSError, UnicodeDecodeError):
            pass
    return Path(fallback)


def copy_tree(source, target, check, label, budget, notes):
    """Copy the kit entry SOURCE to TARGET. The entry itself may be a symlink the user made (to a skill in a Git
    checkout, say): it is resolved once, opened, and the place it was opened at is checked (CHECK). Below it the walk
    goes from directory handle to directory handle with nothing followed as a symlink (O_NOFOLLOW, each handle
    compared with what was listed by st_dev/st_ino), so that somebody who can change that checkout (an agent working
    in it) cannot make the copy reach any other host file, not even by swapping a folder for a symlink while the
    copy runs: symlinks, FIFOs, sockets and devices below the entry are skipped with a note."""
    check(source, label)
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError):
        notes.append(f"{label}: {source} skipped: dangling or looping symlink")
        return
    if resolved != source:
        check(resolved, label)
    try:
        listed = os.lstat(resolved)
    except OSError as exc:
        notes.append(f"{label}: {source} skipped: {exc.strerror}")
        return
    if not (stat.S_ISDIR(listed.st_mode) or stat.S_ISREG(listed.st_mode)):
        notes.append(f"{label}: {source} skipped: not a regular file or directory")
        return
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOCTTY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        notes.append(f"{label}: {source} skipped: {'swapped for a symlink' if exc.errno == errno.ELOOP else exc.strerror}")
        return
    try:
        opened = fd_path(fd, resolved)
        if opened != resolved:
            check(opened, label)  # a folder above it was swapped meanwhile: the place it was really opened at
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (listed.st_dev, listed.st_ino):
            notes.append(f"{label}: {source} skipped: changed while it was copied")
        elif stat.S_ISDIR(info.st_mode):
            copy_dir_at(fd, target, label, budget, notes, source, 0)
        else:
            copy_file_at(fd, info, target, label, budget, source)
    finally:
        os.close(fd)


def copy_file_at(fd, info, target, label, budget, shown):
    """Copy the open regular file FD (INFO: its fstat) to TARGET, counted against BUDGET."""
    budget["files"] += 1
    budget["bytes"] += info.st_size
    if budget["files"] > KIT_FILES or budget["bytes"] > KIT_BYTES:
        raise ValueError(f"The kits hold more than {KIT_FILES} files or {KIT_BYTES >> 20} MiB; "
                         f"the last one counted was {shown} ({label})")
    left = info.st_size
    with os.fdopen(os.dup(fd), "rb") as src, open(target, "xb") as dst:
        while left > 0:  # never more than it had when counted (a file that grows meanwhile is cut there)
            chunk = src.read(min(left, 1 << 20))
            if not chunk:
                break
            dst.write(chunk)
            left -= len(chunk)
    # Scripts in skills stay executable; nothing else is writable in the sandbox anyway.
    target.chmod(0o755 if info.st_mode & 0o111 else 0o644)


def copy_dir_at(directory, target, label, budget, notes, shown, depth):
    """Copy the open directory DIRECTORY to TARGET; every child is opened relative to it, never through a
    symlink, and must be what the listing saw (st_dev/st_ino), else it is skipped (it was swapped meanwhile)."""
    if depth >= KIT_DEPTH:
        raise ValueError(f"{label}: {shown} is nested deeper than {KIT_DEPTH} directories")
    target.mkdir(mode=0o755)
    for name in sorted(os.listdir(directory)):
        path = Path(shown) / name
        problem = bad_name(name)
        if problem:
            notes.append(f"{label}: {path} skipped: {problem}")
            continue
        try:
            listed = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed meanwhile
        if stat.S_ISLNK(listed.st_mode):
            notes.append(f"{label}: {path} skipped: a symlink below a kit entry is not followed (the copy is a "
                         "snapshot of the entry's own files)")
            continue
        if not (stat.S_ISDIR(listed.st_mode) or stat.S_ISREG(listed.st_mode)):
            notes.append(f"{label}: {path} skipped: not a regular file or directory")
            continue
        flags = DIR_FLAGS if stat.S_ISDIR(listed.st_mode) else \
            os.O_RDONLY | os.O_NOCTTY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(name, flags, dir_fd=directory)
        except OSError as exc:
            notes.append(f"{label}: {path} skipped: changed while it was copied ("
                         f"{'now a symlink' if exc.errno == errno.ELOOP else exc.strerror})")
            continue
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (listed.st_dev, listed.st_ino) or \
                    stat.S_IFMT(info.st_mode) != stat.S_IFMT(listed.st_mode):
                notes.append(f"{label}: {path} skipped: changed while it was copied")
                continue
            if stat.S_ISDIR(info.st_mode):
                copy_dir_at(fd, target / name, label, budget, notes, path, depth + 1)
            else:
                copy_file_at(fd, info, target / name, label, budget, path)
        finally:
            os.close(fd)


def ensure_default_home(agent):
    """Create the default kit of AGENT from the templates if it does not exist; True if created.
    An existing one is never changed, so a deleted example stays deleted."""
    target = default_home(agent)
    if target.exists() or target.is_symlink() or not (TEMPLATES / agent).is_dir():
        return False
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{agent}-", dir=target.parent))
    try:
        shutil.copytree(TEMPLATES / agent, staging / agent, symlinks=False)
        try:
            os.rename(staging / agent, target)
        except OSError:
            if target.exists():
                return False  # created meanwhile by another start
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return True


# --- project memory --------------------------------------------------------------------------------
#
# The sandbox HOME is the agent's: during a run, and between runs of a persistent HOME, it can turn
# any directory into a symlink, also while we are copying. All memory I/O therefore goes through
# directory file descriptors, each component opened with O_NOFOLLOW: a swapped component fails
# instead of leading out of the HOME. The same code serves the project's store, which is ours.

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
MEMORY_ENTRIES = 20000   # files, directories and skipped entries walked per collect, against slow DoS
MEMORY_NOTES = 20


def memory_store(project, agent):
    """Where the memory of PROJECT (its first workspace) is kept for AGENT."""
    project = Path(project)
    digest_ = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    return state_root() / "memory" / f"{(project.name or 'root')[:64]}-{digest_}" / agent


def memory_paths(agent):
    """[(sandbox HOME path, is directory)] that make up AGENT's memory."""
    return [(MEMORY_FILE[agent][1], False), *((path, True) for path in MEMORY_DIRS[agent])]


@contextlib.contextmanager
def locked(directory):
    """Exclusive lock for one project's memory: two sandboxes of a project never merge at once."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(directory / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parts_of(relative):
    relative = PurePosixPath(relative)
    if relative.is_absolute() or not relative.parts or any(p in (".", "..") for p in relative.parts):
        raise ValueError(f"Invalid memory path: {relative}")
    return relative.parts


def open_dir(root, parts, create=False):
    """File descriptor of ROOT/PARTS with no component followed as a symlink; missing directories are
    created (mode 0700) with CREATE, else None is returned. ValueError for a symlink or a non-directory."""
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in parts:
            try:
                following = os.open(part, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                os.mkdir(part, 0o700, dir_fd=fd)
                following = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError:
                raise ValueError(f"{'/'.join(parts)}: {part} is a symlink or not a directory") from None
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_at(directory_fd, name, limit):
    """(bytes, None) of the regular file NAME in DIRECTORY_FD, else (None, why); never follows a symlink."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                     dir_fd=directory_fd)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "symlink" if exc.errno == errno.ELOOP else exc.strerror
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not a regular file"
        if info.st_size > limit:
            return None, f"larger than {limit >> 20} MiB"
        with os.fdopen(os.dup(fd), "rb") as stream:
            return stream.read(limit + 1), None
    finally:
        os.close(fd)


def read_regular(path, limit):
    """Bytes of the regular file PATH (its last component not followed), or None; for launcher-owned paths."""
    path = Path(path)
    try:
        directory = os.open(path.parent, DIR_FLAGS & ~os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        data, why = read_at(directory, path.name, limit)
    finally:
        os.close(directory)
    if why and why.startswith("larger"):
        raise ValueError(f"{path} is {why}")
    return data


def collect(root, agent):
    """({HOME path: bytes}, notes, complete) of AGENT's memory below ROOT (a sandbox HOME or a store).
    Symlinks and special files are skipped with a note. Past MEMORY_FILES files or MEMORY_BYTES the rest
    is left out and COMPLETE is False, so that callers never take a missing file for a deleted one."""
    files, notes, state = {}, [], {"bytes": 0, "entries": 0, "complete": True}

    def note(text):
        if len(notes) < MEMORY_NOTES:
            notes.append(text)
        elif len(notes) == MEMORY_NOTES:
            notes.append("further entries skipped without a note")

    def full(relative, size):
        if len(files) >= MEMORY_FILES or state["bytes"] + size > MEMORY_BYTES:
            if state["complete"]:
                note(f"memory larger than {MEMORY_FILES} files or {MEMORY_BYTES >> 20} MiB; "
                     f"{relative} and later files left out")
            state["complete"] = False
            return True
        return False

    def take(directory_fd, name, relative):
        data, why = read_at(directory_fd, name, MEMORY_BYTES)
        if data is None:
            if why:
                note(f"{relative} skipped: {why}")
        elif not full(relative, len(data)):
            files[str(relative)] = data
            state["bytes"] += len(data)

    def walk(parent_fd, name, relative, depth):
        try:
            fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError:
            note(f"{relative} skipped: symlink or not a directory")
            return
        try:
            with os.scandir(fd) as entries:
                children = sorted(entries, key=lambda e: e.name)
            for entry in children:
                state["entries"] += 1
                if state["entries"] > MEMORY_ENTRIES:
                    if state["complete"]:
                        note(f"memory has more than {MEMORY_ENTRIES} entries; the rest is left out")
                    state["complete"] = False
                    return
                child = relative / entry.name
                if bad_name(entry.name):
                    note(f"{child} skipped: {bad_name(entry.name)}")
                elif entry.is_symlink():
                    note(f"{child} skipped: symlink")
                elif entry.is_dir(follow_symlinks=False):
                    if depth >= KIT_DEPTH:
                        note(f"{child} skipped: nested too deep")
                    else:
                        walk(fd, entry.name, child, depth + 1)
                else:
                    take(fd, entry.name, child)
        finally:
            os.close(fd)

    for relative, directory in memory_paths(agent):
        parts = parts_of(relative)
        try:
            parent = open_dir(root, parts[:-1])
        except ValueError as exc:
            note(str(exc))
            continue
        if parent is None:
            continue
        try:
            if directory:
                walk(parent, parts[-1], PurePosixPath(relative), 0)
            else:
                take(parent, parts[-1], PurePosixPath(relative))
        finally:
            os.close(parent)
    return files, notes, state["complete"]


def write_at(root, relative, data):
    """Atomically make ROOT/RELATIVE a private file with DATA (parents created, mode 0700). A symlink
    there is replaced, never followed; a directory there is a ValueError, never removed."""
    parts = parts_of(relative)
    parent = open_dir(root, parts[:-1], create=True)
    try:
        temporary = f".memory-{os.getpid()}-{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException as exc:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            if isinstance(exc, OSError) and exc.errno in (errno.EISDIR, errno.ENOTEMPTY, errno.EEXIST):
                raise ValueError(f"{relative}: a directory of that name exists") from None
            raise
    finally:
        os.close(parent)


def remove_tree_at(parent_fd, name):
    """Remove NAME in PARENT_FD and, for a directory, everything below it; symlinks are unlinked."""
    try:
        os.unlink(name, dir_fd=parent_fd)
        return
    except FileNotFoundError:
        return
    except (IsADirectoryError, PermissionError):
        pass
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries]
        for child in names:
            remove_tree_at(fd, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def remove_at(root, relative, tree=True):
    """Remove ROOT/RELATIVE (a whole directory only with TREE); a missing path is fine."""
    parts = parts_of(relative)
    parent = open_dir(root, parts[:-1])
    if parent is None:
        return
    try:
        if tree:
            remove_tree_at(parent, parts[-1])
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(parts[-1], dir_fd=parent)
    finally:
        os.close(parent)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pending_path(store, home):
    """Record of a run that has not saved its memory yet, per store and HOME."""
    key = hashlib.sha256(os.fsencode(str(home))).hexdigest()[:12]
    return store.parent / f".pending-{store.name}-{key}.json"


def memory_prepare(agent, project, home, template=None, check=None, persistent=False):
    """Put the project's memory into the sandbox HOME; returns (manifest, seeded label or None, store, notes).

    The manifest {HOME path: sha256} is what memory_save() compares against after the run. The first run
    of a project creates its store from the kit's memory template (checked with CHECK(path, label) like
    every kit file). A PERSISTENT HOME whose last run never saved its memory (crash, failed save) is
    saved first, so that its changes are not overwritten."""
    store = memory_store(project, agent)
    seeded, notes, data = None, [], None
    if template:
        label, path = template
        if check:
            check(path, label)
            check(path.resolve(), label)
        data = read_regular(path.resolve(), TEMPLATE_LIMIT)
        if data is None:
            raise ValueError(f"{label}: {path} is not a regular file")
    with locked(store.parent):
        if store.is_symlink():
            raise ValueError(f"{store} is a symlink; refusing to use it")
        pending = pending_path(store, home)
        if pending.is_file() and not pending.is_symlink():
            try:
                record = json.loads(read_regular(pending, 4 << 20) or b"null")
            except ValueError:
                record = None
            if persistent and isinstance(record, dict) and record.get("home") == str(home) \
                    and isinstance(record.get("manifest"), dict) and store.is_dir():
                changed, more = save_locked(agent, store, home, record["manifest"])
                notes += [f"kept {changed} file(s) a previous run of this HOME had not saved"] if changed else []
                notes += more
            pending.unlink()
        for stale in store.parent.glob(f".pending-{store.name}-*.json"):
            with contextlib.suppress(OSError, ValueError):
                record = json.loads(read_regular(stale, 4 << 20) or b"null")
                if not isinstance(record, dict) or not Path(str(record.get("home"))).is_dir():
                    stale.unlink()
        if not store.exists():
            store.mkdir(mode=0o700)
            if data is not None:
                write_at(store, MEMORY_FILE[agent][1], data)
                seeded = template[0]
        files, more, _ = collect(store, agent)
        notes += more
        # A reused HOME (--state-dir) may hold an older copy: the store is the authority.
        for relative, _ in memory_paths(agent):
            remove_at(home, relative)
        for relative, content in files.items():
            write_at(home, relative, content)
        manifest = {relative: digest(content) for relative, content in files.items()}
        record = json.dumps({"home": str(home), "manifest": manifest}).encode()
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(record)
    return manifest, seeded, store, notes


def save_locked(agent, store, home, manifest):
    """memory_save() with the project's lock held."""
    files, notes, complete = collect(home, agent)
    if store.is_symlink() or not store.is_dir():
        raise ValueError(f"{store} is missing or a symlink; the memory of this run was not saved")
    stored, _, stored_complete = collect(store, agent)
    count, size, changed = len(stored), sum(map(len, stored.values())), 0
    for relative, data in files.items():
        if manifest.get(relative) == digest(data) or stored.get(relative) == data:
            continue
        new_count = count + (relative not in stored)
        new_size = size + len(data) - len(stored.get(relative, b""))
        if new_count > MEMORY_FILES or new_size > MEMORY_BYTES:
            notes.append(f"{relative} not kept: the project memory is full "
                         f"({MEMORY_FILES} files, {MEMORY_BYTES >> 20} MiB)")
            continue
        try:
            write_at(store, relative, data)
        except (OSError, ValueError) as exc:
            notes.append(f"{relative} not kept: {exc}")
            continue
        count, size, changed = new_count, new_size, changed + 1
    if complete and stored_complete:
        # A file this run deleted goes, unless another sandbox changed it meanwhile.
        for relative, known in manifest.items():
            if relative not in files and relative in stored and digest(stored[relative]) == known:
                try:
                    remove_at(store, relative, tree=False)
                    changed += 1
                except (OSError, ValueError) as exc:
                    notes.append(f"{relative} not removed: {exc}")
    return changed, notes


def memory_save(agent, project, home, manifest):
    """Take what the agent changed in its memory back into the project's store: (changed count, notes).
    Only files this run changed are written and only files it deleted are removed, so a second
    sandbox of the same project that saved meanwhile keeps its own changes."""
    store = memory_store(project, agent)
    with locked(store.parent):
        changed, notes = save_locked(agent, store, home, manifest)
        pending = pending_path(store, home)
        if pending.is_file() and not pending.is_symlink():
            pending.unlink()
    return changed, notes


# --- plugin declarations ---------------------------------------------------------------------------

def check_source(name, source):
    """A marketplace source as Claude's extraKnownMarketplaces takes it; ValueError if unsupported."""
    if not isinstance(source, dict) or not isinstance(source.get("source"), str):
        raise ValueError(f"marketplace {name}: \"source\" must be an object with a \"source\" kind")
    kind = source["source"]
    allowed = {"github": {"repo"}, "git": {"url"}, "url": {"url"}, "directory": {"path"}}
    if kind not in allowed or set(source) - allowed[kind] - {"source"}:
        raise ValueError(f"marketplace {name}: supported sources are github (repo), git (url), url (url) "
                         f"and sandbox-ai's own directory ({MARKETPLACE_TARGET})")
    for key in allowed[kind]:
        if not isinstance(source.get(key), str):
            raise ValueError(f"marketplace {name}: {kind} needs \"{key}\"")
    if kind == "github" and (not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", source["repo"])
                             or any(part in (".", "..") for part in source["repo"].split("/"))):
        raise ValueError(f"marketplace {name}: repo must be OWNER/REPO")
    if kind in ("git", "url") and not re.fullmatch(r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/[^\s]*)?",
                                                     source["url"]):
        raise ValueError(f"marketplace {name}: url must be https://HOST/...")
    if kind == "directory" and source["path"] != MARKETPLACE_TARGET:
        raise ValueError(f"marketplace {name}: a directory source can only be sandbox-ai's own "
                         f"({MARKETPLACE_TARGET}); publish others on a Git server")
    return source


def source_host(source):
    """Host a marketplace source is fetched from, or None (github: the GitHub preset; directory: local)."""
    if source["source"] in ("git", "url"):
        return re.match(r"https://([A-Za-z0-9.-]+)", source["url"])[1].lower()
    return None


def source_argument(source):
    """The argument of `claude plugin marketplace add` for SOURCE."""
    return source[{"github": "repo", "git": "url", "url": "url", "directory": "path"}[source["source"]]]


def read_declaration(label, path):
    try:
        data = read_regular(Path(path).resolve(), DECLARATION_LIMIT)
        document = json.loads(data) if data is not None else None
    except (OSError, ValueError, RecursionError):
        document = None
    if not isinstance(document, dict):
        raise ValueError(f"{label}: {path} must hold a JSON object")
    return document


def declarations(agent, plugin_files):
    """({marketplace name: source}, [plugin ids], notes) of the marketplaces.json files of the kits.
    Claude: marketplaces + "NAME@MARKETPLACE" plugins; OpenCode: npm plugin names; Codex: not yet."""
    marketplaces, origins, plugins, notes = {}, {}, [], []
    for label, path in plugin_files:
        document = read_declaration(label, path)
        unknown = set(document) - {"marketplaces", "plugins", "$comment"}
        if unknown:
            raise ValueError(f"{label}: {path}: unknown keys {sorted(unknown)} (known: marketplaces, plugins)")
        listed = document.get("plugins", [])
        if not isinstance(listed, list) or not all(isinstance(p, str) for p in listed):
            raise ValueError(f"{label}: {path}: \"plugins\" must be a list of strings")
        declared = document.get("marketplaces", {})
        if not isinstance(declared, dict):
            raise ValueError(f"{label}: {path}: \"marketplaces\" must be an object")
        if agent == "codex":
            if listed or declared:
                notes.append(f"{label}: plugins and marketplaces are not supported for Codex yet; ignored")
            continue
        if agent == "opencode":
            if declared:
                notes.append(f"{label}: OpenCode has no marketplaces; \"marketplaces\" ignored")
            for plugin in listed:
                if not re.fullmatch(NPM_PLUGIN, plugin):
                    raise ValueError(f"{label}: {plugin!r} is not an npm package name (optionally @VERSION)")
                if plugin not in plugins:
                    plugins.append(plugin)
            continue
        for name, entry in declared.items():
            if not re.fullmatch(PLUGIN_NAME, name):
                raise ValueError(f"{label}: invalid marketplace name {name!r}")
            if not isinstance(entry, dict) or set(entry) - {"source"}:
                raise ValueError(f"{label}: marketplace {name} must be {{\"source\": {{...}}}}")
            source = check_source(name, entry.get("source"))
            if name in marketplaces and marketplaces[name] != source:
                raise ValueError(f"marketplace {name} comes from both {origins[name]} and {label} with different "
                                 "sources; keep one")
            marketplaces[name], origins[name] = source, label
        for plugin in listed:
            if not re.fullmatch(f"{PLUGIN_NAME}@{PLUGIN_NAME}", plugin):
                raise ValueError(f"{label}: plugin {plugin!r} must be NAME@MARKETPLACE")
            if plugin not in plugins:
                plugins.append(plugin)
    for plugin in list(plugins):
        if agent == "claude" and plugin.split("@")[1] not in marketplaces:
            notes.append(f"plugin {plugin} skipped: no kit declares marketplace {plugin.split('@')[1]}")
            plugins.remove(plugin)
    return marketplaces, plugins, notes


def all_plugin_files(agent, builtin=()):
    """marketplaces.json of the default kit and of every kit: what `sandbox-ai plugins` installs. A
    built-in kit only when named in BUILTIN (its plugins may come from far away, e.g. GitHub)."""
    files = []
    if (default_home(agent) / PLUGINS_FILE).is_file():
        files.append(("home", default_home(agent) / PLUGINS_FILE))
    for name in available_kits(agent, builtin=False):
        path = kits_dir() / name / agent / PLUGINS_FILE
        if path.is_file():
            files.append((f"kit {name}", path))
    for name in dict.fromkeys(builtin):
        if name not in kit_names(BUILTIN_KITS, agent):
            raise ValueError(f"--kit {name}: no built-in kit of that name for {agent} "
                             f"(built-in: {', '.join(kit_names(BUILTIN_KITS, agent)) or 'none'})")
        if name in builtin_kits(agent) and (BUILTIN_KITS / name / agent / PLUGINS_FILE).is_file():
            files.append((f"built-in kit {name}", BUILTIN_KITS / name / agent / PLUGINS_FILE))
    return files


def install_command(plugins, agent="claude"):
    """The command that installs PLUGINS (missing ones): `sandbox-ai plugins claude`, with --kit NAME
    for each built-in kit (not replaced by the user's) that declares one of them."""
    words = ["sandbox-ai", "plugins", agent]
    for name in builtin_kits(agent):
        document = read_store_json(BUILTIN_KITS / name / agent / PLUGINS_FILE) or {}
        listed = document.get("plugins") if isinstance(document.get("plugins"), list) else []
        if set(listed) & set(plugins):
            words += ["--kit", name]
    return " ".join(words)


def read_store_json(path):
    try:
        data = read_regular(path, 4 << 20)
        document = json.loads(data) if data is not None else None
    except (OSError, ValueError, RecursionError):
        document = None
    return document if isinstance(document, dict) else None


def plugin_dir(agent="claude"):
    """STORE/.claude/plugins of the plugin store, or None if missing or if any part below the store's
    root is a symlink (the store's own sandbox could have planted one pointing at a host directory)."""
    current = plugin_store(agent)
    for part in (None, ".claude", "plugins"):
        current = current / part if part else current
        if current.is_symlink() or not current.is_dir():
            return None
    return current


def installed(agent="claude"):
    """({marketplace name: entry}, {plugin id: entries}) in the plugin store; empty when there is none."""
    plugins = plugin_dir(agent)
    if plugins is None:
        return {}, {}
    known = read_store_json(plugins / "known_marketplaces.json") or {}
    document = read_store_json(plugins / "installed_plugins.json") or {}
    entries = document.get("plugins") if isinstance(document.get("plugins"), dict) else {}
    return ({k: v for k, v in known.items() if isinstance(v, dict) and re.fullmatch(PLUGIN_NAME, k)},
            {k: v for k, v in entries.items() if re.fullmatch(f"{PLUGIN_NAME}@{PLUGIN_NAME}", k)})


def describe(agent, label, directory):
    """One line on what the kit DIRECTORY holds for AGENT: "2 skills (a, b); CLAUDE.md; marketplaces.json"."""
    plan = scan(agent, [(label, directory)])
    parts = [f"{len(names)} {kind} ({', '.join(names)})" for kind, names in summary(plan).items()]
    parts += [MEMORY_FILE[agent][0]] if plan["memory"] else []
    parts += [PLUGINS_FILE] if plan["plugins"] else []
    return "; ".join(parts) or "empty"


# --- hooks: kits may carry the agent's hooks; they run only once the user approved them ------------------------
#
# AGENT/hooks/ in a kit. For Claude Code and Codex: hooks.json, the "hooks" object of Claude Code's settings
# ({"hooks": {EVENT: [{"matcher": ..., "hooks": [{"type": "command", "command": ..., "timeout": ...}]}]}}), and the
# files its commands run, named ${HOOK_DIR}/FILE. For OpenCode: plugin modules (*.js, *.mjs, *.ts at the top) and
# their helper files. A run copies the folder, hashes the copy and installs its hooks as the agent's managed
# configuration (next to the rule hooks, after them) only if the user approved exactly that content with
# `sandbox-ai kits trust-hooks`, recorded in trust_file(): outside every tree a sandbox can write. Any change, by
# anyone, takes the approval away until it is given again. A symlink or a special file makes a folder's hooks
# unusable: they would run something else than what was reviewed.

HOOKS_DIR = "hooks"
HOOKS_FILE = "hooks.json"
HOOK_DIR_VARIABLE = "${HOOK_DIR}"
HOOK_FILES, HOOK_BYTES, HOOK_DEPTH, HOOK_HANDLERS = 200, 8 << 20, 8, 100
HOOK_EVENT = re.compile(r"[A-Z][A-Za-z]{2,39}")
HOOK_TIMEOUT_LIMIT = 600
# Events the agents are known to run hooks for; another name is kept, with a note (a newer agent may know it).
HOOK_EVENTS = {
    "claude": ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "UserPromptSubmit",
               "Notification", "Stop", "SubagentStart", "SubagentStop", "PreCompact", "SessionStart", "SessionEnd"),
    "codex": ("PreToolUse", "PostToolUse", "PermissionRequest", "UserPromptSubmit", "SessionStart", "SessionEnd",
              "Stop", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact"),
}
PLUGIN_SUFFIXES = (".js", ".mjs", ".ts")
TRUST_VERSION = 1


def trust_file():
    """Where the approvals of kit hooks are kept: with your rules, outside every sandbox."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "trusted-hooks.json"


def hook_tree(directory, limits=None, keep=(), links=False):
    """{"files": [{"path", "size", "sha256", "executable"}], "sha256", "errors", "data", "kept"} of the hooks folder
    DIRECTORY, read through directory descriptors (nothing followed). "sha256" covers every file's path, content and
    executable bit, and is None once there is an error; "data": the bytes of the top-level hooks.json, if any;
    "kept": {path: bytes} of the files KEEP names. LIMITS: (files, bytes, depth), by default a kit's. LINKS: a
    symlink is no error but part of the content (its target, never followed; a plugin's node_modules/.bin)."""
    max_files, max_bytes, max_depth = limits or (HOOK_FILES, HOOK_BYTES, HOOK_DEPTH)
    files, errors, state = [], [], {"bytes": 0, "data": None, "kept": {}}
    try:
        top = os.open(directory, DIR_FLAGS)
    except OSError as exc:
        why = "a symlink" if exc.errno in (errno.ELOOP, errno.ENOTDIR) and os.path.islink(directory) else exc.strerror
        return {"files": [], "sha256": None, "errors": [f"{directory}: {why}"], "data": None, "kept": {}}

    def walk(fd, relative, depth):
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            shown = relative + name
            problem = bad_name(name)
            if problem:
                errors.append(f"{shown}: {problem}")
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                errors.append(f"{shown}: {exc.strerror}")
                continue
            if stat.S_ISLNK(info.st_mode) and links:
                target = os.readlink(name, dir_fd=fd)
                files.append({"path": shown, "size": 0, "link": target, "executable": False,
                              "sha256": hashlib.sha256(b"link:" + os.fsencode(target)).hexdigest()})
                if len(files) > max_files:
                    raise ValueError(f"more than {max_files} files")
            elif stat.S_ISLNK(info.st_mode):
                errors.append(f"{shown} is a symlink: hooks run only from files of their own, which were reviewed")
            elif stat.S_ISDIR(info.st_mode):
                if depth + 1 >= max_depth:
                    errors.append(f"{shown}: nested deeper than {max_depth} folders")
                    continue
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
                try:
                    walk(child, shown + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                data, why = read_at(fd, name, max_bytes)
                if data is None:
                    errors.append(f"{shown}: {why or 'gone while reading'}")
                    continue
                state["bytes"] += len(data)
                files.append({"path": shown, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                              "executable": bool(info.st_mode & 0o111)})
                if shown == HOOKS_FILE:
                    state["data"] = data
                if shown in keep:
                    state["kept"][shown] = data
                if len(files) > max_files or state["bytes"] > max_bytes:
                    raise ValueError(f"more than {max_files} files or {max_bytes >> 20} MiB")
            else:
                errors.append(f"{shown}: not a regular file or folder (FIFO, socket or device)")
    try:
        walk(top, "", 0)
    except (OSError, ValueError) as exc:
        errors.append(str(exc) if isinstance(exc, ValueError) else f"{directory}: {exc.strerror}")
    finally:
        os.close(top)
    listing = json.dumps([[f["path"], f["executable"], f["sha256"]] for f in files]).encode()
    return {"files": files, "sha256": None if errors else hashlib.sha256(listing).hexdigest(), "errors": errors,
            "data": state["data"], "kept": state["kept"]}


def hook_groups(agent, document):
    """The hook definitions DOCUMENT (hooks.json: {"hooks": {EVENT: [group]}} or {EVENT: [group]}) checked and in
    their normal form {EVENT: [{"matcher"?, "hooks": [{"type": "command", "command", "timeout"?}]}]}, and notes."""
    if isinstance(document, dict) and set(document) <= {"hooks", "description", "$comment"} and "hooks" in document:
        document = document["hooks"]
    if not isinstance(document, dict):
        raise ValueError(f"{HOOKS_FILE}: an object of EVENT: [groups], as under \"hooks\" in Claude Code's settings")
    groups, notes, count = {}, [], 0
    for event, entries in document.items():
        if not isinstance(event, str) or not HOOK_EVENT.fullmatch(event):
            raise ValueError(f"{HOOKS_FILE}: {str(event)[:60]!r} is not an event name (PreToolUse, Stop, ...)")
        if event not in HOOK_EVENTS.get(agent, ()):
            notes.append(f"{event} is not an event {agent} is known to run hooks for")
        if not isinstance(entries, list):
            raise ValueError(f"{HOOKS_FILE}: {event} is a list of groups {{\"matcher\", \"hooks\"}}")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"matcher", "hooks"} or \
                    not isinstance(entry.get("hooks"), list) or not entry["hooks"]:
                raise ValueError(f"{HOOKS_FILE}: {event}: a group is {{\"matcher\": ..., \"hooks\": [...]}}")
            matcher = entry.get("matcher")
            if matcher is not None and (not isinstance(matcher, str) or len(matcher) > 200 or not matcher.isprintable()):
                raise ValueError(f"{HOOKS_FILE}: {event}: \"matcher\" is one line of at most 200 characters")
            handlers = []
            for handler in entry["hooks"]:
                count += 1
                if count > HOOK_HANDLERS:
                    raise ValueError(f"{HOOKS_FILE}: more than {HOOK_HANDLERS} hooks")
                if not isinstance(handler, dict) or handler.get("type") != "command" or \
                        set(handler) - {"type", "command", "timeout"}:
                    raise ValueError(f"{HOOKS_FILE}: {event}: only {{\"type\": \"command\", \"command\", \"timeout\"}} "
                                     "hooks run from a kit")
                command, timeout = handler.get("command"), handler.get("timeout")
                if not isinstance(command, str) or not command.strip() or len(command) > 4000 or \
                        any(ord(c) < 32 and c != "\t" or 0xD800 <= ord(c) <= 0xDFFF for c in command):
                    raise ValueError(f"{HOOKS_FILE}: {event}: \"command\" is one line of at most 4000 characters")
                if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                            or not 1 <= timeout <= HOOK_TIMEOUT_LIMIT):
                    raise ValueError(f"{HOOKS_FILE}: {event}: \"timeout\" is 1 to {HOOK_TIMEOUT_LIMIT} seconds")
                handlers.append({"type": "command", "command": command,
                                 **({"timeout": timeout} if timeout is not None else {})})
            groups.setdefault(event, []).append({**({"matcher": matcher} if matcher is not None else {}),
                                                 "hooks": handlers})
    return groups, notes


def read_hooks(agent, directory):
    """What the hooks folder DIRECTORY holds for AGENT: {"path", "files", "sha256", "groups" (Claude Code, Codex),
    "plugins" (OpenCode), "hooks" (one line per hook: event, matcher, command, the folder's files it runs), "notes",
    "errors"}. Hooks with errors never run and cannot be approved."""
    tree = hook_tree(directory)
    result = {"path": str(directory), "files": tree["files"], "sha256": tree["sha256"], "groups": {},
              "plugins": [], "hooks": [], "notes": [], "errors": list(tree["errors"])}
    names = [f["path"] for f in tree["files"]]
    if agent == "opencode":
        result["plugins"] = [name for name in names if "/" not in name and name.endswith(PLUGIN_SUFFIXES)]
        result["hooks"] = [{"plugin": name, "files": [name]} for name in result["plugins"]]
        if names and not result["plugins"]:
            result["errors"].append(f"no plugin module ({', '.join(PLUGIN_SUFFIXES)}) at the top of the folder")
        return result
    if tree["data"] is None:
        if names or not tree["errors"]:
            result["errors"].append(f"no {HOOKS_FILE} at the top of the folder: it says which hooks run")
        return result
    try:
        document = json.loads(tree["data"].decode("utf-8"))
        result["groups"], result["notes"] = hook_groups(agent, document)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        result["errors"].append(str(exc) if isinstance(exc, ValueError) and HOOKS_FILE in str(exc)
                                else f"{HOOKS_FILE} is not valid JSON ({exc})")
        result["sha256"] = None
        return result
    for event, entries in result["groups"].items():
        for entry in entries:
            for handler in entry["hooks"]:
                runs = [name for name in names if name != HOOKS_FILE and f"{HOOK_DIR_VARIABLE}/{name}" in handler["command"]]
                result["hooks"].append({"event": event, "matcher": entry.get("matcher"), "command": handler["command"],
                                        "timeout": handler.get("timeout"), "files": runs})
    if result["errors"]:
        result["sha256"] = None
    return result


# --- the hooks of Claude plugins -----------------------------------------------------------------------------
#
# A plugin's hooks (hooks/hooks.json, or "hooks" in .claude-plugin/plugin.json) do not run while the agent's
# managed hooks are on (allowManagedHooksOnly). Approved like a kit's hooks (`sandbox-ai plugins claude
# trust-hooks PLUGIN`: the SHA-256 of the plugin's whole installed folder, which its commands may use), they
# join the managed configuration after the rule and kit hooks, with ${CLAUDE_PLUGIN_ROOT} set as Claude Code
# sets it for a plugin's own hooks. Without managed hooks they run as Claude Code runs them, as before.

PLUGIN_ROOT_VARIABLE = "${CLAUDE_PLUGIN_ROOT}"
PLUGIN_CACHE = PurePosixPath("/root/.claude/plugins/cache")  # where the plugin store's cache is in every sandbox
PLUGIN_LIMITS = (20000, 256 << 20, 32)  # files, bytes, depth of a plugin's folder (node_modules included)
PLUGIN_HOOKS_FILE = "hooks/hooks.json"
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"
PLUGIN_KNOWN_VARIABLES = {PLUGIN_ROOT_VARIABLE, "${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR", "$CLAUDE_PLUGIN_ROOT"}


def plugin_trust_key(plugin):
    """What an approval records as the "kit" of PLUGIN's hooks."""
    return f"plugin:{plugin}"


def plugin_install(plugin, entries, store=None):
    """(host folder, sandbox folder) of PLUGIN as installed_plugins.json lists it (ENTRIES), or None. The path
    must lie in the cache (the store's sandbox could have written anything there)."""
    store = store or plugin_dir("claude")
    if store is None or not isinstance(entries, list):
        return None
    for entry in entries:
        path = entry.get("installPath") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            continue
        sandbox = PurePosixPath(path)
        try:
            relative = sandbox.relative_to(PLUGIN_CACHE)
        except ValueError:
            continue
        if not relative.parts or any(part in ("", ".", "..") or bad_name(part) for part in relative.parts) \
                or path != os.path.normpath(path):
            continue
        return store / "cache" / Path(*relative.parts), str(sandbox)
    return None


def read_plugin_hooks(plugin, host_root, sandbox_root):
    """PLUGIN's hooks in its installed folder HOST_ROOT (SANDBOX_ROOT in the sandbox): like read_hooks, with
    "plugin" and the commands made to run as managed hooks (${CLAUDE_PLUGIN_ROOT} replaced and exported)."""
    tree = hook_tree(host_root, PLUGIN_LIMITS, keep=(PLUGIN_HOOKS_FILE, PLUGIN_MANIFEST), links=True)
    result = {"plugin": plugin, "path": str(host_root), "files": tree["files"], "sha256": tree["sha256"],
              "groups": {}, "hooks": [], "notes": [], "errors": list(tree["errors"]),
              "declared": os.path.lexists(Path(host_root) / PLUGIN_HOOKS_FILE)}
    documents = []
    try:
        if PLUGIN_HOOKS_FILE in tree["kept"]:
            documents.append((PLUGIN_HOOKS_FILE, json.loads(tree["kept"][PLUGIN_HOOKS_FILE].decode("utf-8"))))
        manifest = json.loads(tree["kept"][PLUGIN_MANIFEST].decode("utf-8")) if PLUGIN_MANIFEST in tree["kept"] else {}
        declared = manifest.get("hooks") if isinstance(manifest, dict) else None
        result["declared"] = result["declared"] or declared is not None
        for value in (declared if isinstance(declared, list) else [declared] if declared is not None else []):
            if isinstance(value, dict):
                documents.append((PLUGIN_MANIFEST, value))
                continue
            name = os.path.normpath(value[2:] if isinstance(value, str) and value.startswith("./") else str(value))
            listed = {f["path"]: f for f in tree["files"]}
            if name == PLUGIN_HOOKS_FILE and PLUGIN_HOOKS_FILE in tree["kept"]:
                continue  # the default file, named once more
            if name not in listed or "link" in listed[name] or name.startswith(".."):
                raise ValueError(f"{PLUGIN_MANIFEST}: hooks {str(value)[:80]!r} is no file of the plugin")
            data, why = read_at_path(host_root, name)
            if data is None or hashlib.sha256(data).hexdigest() != listed[name]["sha256"]:
                raise ValueError(f"{name}: {why or 'changed while it was read'}")
            documents.append((name, json.loads(data.decode("utf-8"))))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        result["errors"].append(f"its hook definitions cannot be read: {exc}")
    for name, document in documents:
        try:
            groups, notes = hook_groups("claude", document)
        except ValueError as exc:
            result["errors"].append(f"{name}: {str(exc).replace(HOOKS_FILE + ': ', '')}")
            continue
        result["notes"] += notes
        for event, entries in groups.items():
            for entry in entries:
                handlers = []
                for handler in entry["hooks"]:
                    command = handler["command"]
                    unknown = sorted(set(re.findall(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", command))
                                     - PLUGIN_KNOWN_VARIABLES - {"$" + v for v in ("HOME", "PATH", "PWD")})
                    if unknown:
                        result["notes"].append(f"{event}: its command uses {', '.join(unknown)}, which Claude Code "
                                               "may set only for a plugin's own hooks")
                    result["hooks"].append({"event": event, "matcher": entry.get("matcher"), "command": command,
                                            "timeout": handler.get("timeout")})
                    handlers.append({**handler, "command": f"export CLAUDE_PLUGIN_ROOT={shlex.quote(sandbox_root)}; "
                                                           + command.replace(PLUGIN_ROOT_VARIABLE, sandbox_root)})
                result["groups"].setdefault(event, []).append({**entry, "hooks": handlers})
    if result["errors"]:
        result["sha256"] = None
    return result


def read_at_path(root, relative):
    """(bytes, None) of the file RELATIVE in ROOT, every folder on the way opened without following a link;
    (None, why) when that cannot be."""
    parts = PurePosixPath(relative).parts
    try:
        fd = os.open(root, DIR_FLAGS)
    except OSError as exc:
        return None, exc.strerror
    try:
        for part in parts[:-1]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return read_at(fd, parts[-1], HOOK_BYTES)
    except OSError as exc:
        return None, exc.strerror
    finally:
        os.close(fd)


def stage_plugin_hooks(plugins, installed_plugins, store=None, records=None):
    """The hooks of the run's PLUGINS (ids) as INSTALLED_PLUGINS lists them: [{"plugin", "label", "kit",
    "trusted", "groups", "hooks", "notes", "errors", "sha256"}], only plugins with hooks."""
    records = trust_records() if records is None else records
    staged = []
    for plugin in plugins:
        where = plugin_install(plugin, installed_plugins.get(plugin), store)
        if where is None:
            continue
        info = read_plugin_hooks(plugin, *where)
        if not info["declared"]:
            continue  # a plugin without hooks: nothing to approve
        staged.append({**info, "label": f"plugin {plugin}", "kit": plugin_trust_key(plugin),
                       "trusted": hooks_trusted("claude", info["sha256"], plugin_trust_key(plugin), records)})
    return staged


def trust_records(path=None):
    """The approvals in PATH (trust_file()): [{"agent", "sha256", "kit", "path", "time", "hooks"}]."""
    path = Path(path or trust_file())
    try:
        data = read_regular(path, 4 << 20)
        document = json.loads(data) if data else {}
    except (OSError, ValueError):
        return []
    records = document.get("hooks") if isinstance(document, dict) else None
    return [r for r in records if isinstance(r, dict) and isinstance(r.get("agent"), str)
            and isinstance(r.get("sha256"), str)] if isinstance(records, list) else []


def hooks_trusted(agent, sha, kit, records=None):
    """True if the user approved hooks of exactly this content (SHA, from read_hooks) for AGENT in exactly this KIT
    (trust_argument: a kit's name, `default` or a --kit-dir folder). The same content in another kit, a project's
    --kit-dir say, is not approved by it: each kit is reviewed and approved for itself."""
    if not sha or not kit:
        return False
    return any(r["agent"] == agent and r["sha256"] == sha and r.get("kit") == kit
               for r in (trust_records() if records is None else records))


def change_trust(agent, info, kit, approve=True, path=None):
    """Record (APPROVE) or take back the approval of the hooks INFO (read_hooks) of KIT for AGENT; returns the
    records. Taking back drops every approval of that kit for AGENT, whatever its content."""
    path = Path(path or trust_file())
    if approve and not info.get("sha256"):
        raise ValueError("these hooks cannot be approved: " + "; ".join(info.get("errors") or ["nothing to approve"]))
    with locked(path.parent):
        records = [r for r in trust_records(path) if not (r["agent"] == agent and r.get("kit") == kit)]
        if approve:
            records.append({"agent": agent, "sha256": info["sha256"], "kit": kit, "path": info["path"],
                            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "hooks": [h.get("plugin") or f"{h['event']} {h.get('matcher') or '*'}: {h['command']}"
                                      for h in info["hooks"]][:100]})
        write_at(path.parent, path.name, (json.dumps({"version": TRUST_VERSION, "hooks": records}, indent=2)
                                          + "\n").encode())
    return records


def copy_hooks(source, target):
    """Copy the hooks folder SOURCE to TARGET (new) through directory descriptors: nothing followed, symlinks and
    special files left out (read_hooks of the copy then reports them missing)."""
    budget = {"files": 0, "bytes": 0}

    def walk(source_fd, target_fd, depth):
        with os.scandir(source_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if bad_name(name):
                continue
            info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and depth + 1 < HOOK_DEPTH:
                os.mkdir(name, 0o755, dir_fd=target_fd)
                child, copy = os.open(name, DIR_FLAGS, dir_fd=source_fd), os.open(name, DIR_FLAGS, dir_fd=target_fd)
                try:
                    walk(child, copy, depth + 1)
                finally:
                    os.close(child)
                    os.close(copy)
            elif stat.S_ISREG(info.st_mode):
                data, _ = read_at(source_fd, name, HOOK_BYTES)
                if data is None:
                    continue
                budget["files"] += 1
                budget["bytes"] += len(data)
                if budget["files"] > HOOK_FILES or budget["bytes"] > HOOK_BYTES:
                    raise ValueError(f"more than {HOOK_FILES} files or {HOOK_BYTES >> 20} MiB")
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target_fd)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    os.fchmod(stream.fileno(), 0o755 if info.st_mode & 0o111 else 0o644)
    Path(target).mkdir(mode=0o755, parents=True)
    source_fd, target_fd = os.open(source, DIR_FLAGS), os.open(target, DIR_FLAGS)
    try:
        walk(source_fd, target_fd, 0)
    finally:
        os.close(source_fd)
        os.close(target_fd)


def kit_hook_sources(agent, kit_sources):
    """[(label, hooks folder)] of the kits of a run that have hooks for AGENT."""
    found = []
    for label, root in kit_sources:
        folder = Path(root) / HOOKS_DIR
        if os.path.lexists(folder):
            found.append((label, folder))
    return found


def trust_argument(label, folder):
    """What `sandbox-ai kits trust-hooks` takes for the kit LABEL: its name, `default`, or its folder."""
    if label.startswith("kit "):
        return label[4:]
    if label == "home":
        return "default"
    if label.startswith("built-in kit "):
        return label[len("built-in kit "):]
    return str(Path(folder).parent.parent)


def stage_hooks(agent, kit_sources, staging, target_root, records=None):
    """Copy the hooks folder of every kit of a run (AGENT/hooks) to STAGING/N, read the copy and check its approval:
    [{"kit", "label", "source", "staged", "target" (TARGET_ROOT/N in the sandbox), "sha256", "trusted", "groups"
    (with ${HOOK_DIR} replaced by TARGET), "plugins", "hooks", "errors"}]. Only the copy is judged and run, so a
    change after the copy reaches no sandbox."""
    records = trust_records() if records is None else records
    staged = []
    for number, (label, folder) in enumerate(kit_hook_sources(agent, kit_sources), 1):
        copy, target = Path(staging) / str(number), f"{target_root}/{number}"
        errors = []
        try:
            copy_hooks(folder, copy)
        except (OSError, ValueError) as exc:
            errors.append(f"not copied: {exc}")
            copy.mkdir(mode=0o755, parents=True, exist_ok=True)
        info = read_hooks(agent, copy)
        source = read_hooks(agent, folder)  # what the copy left out (symlinks, special files) counts against it
        info["errors"] = list(dict.fromkeys(errors + source["errors"] + info["errors"]))
        if info["errors"]:
            info["sha256"] = None
        groups = {event: [{**entry, "hooks": [{**handler, "command": handler["command"].replace(HOOK_DIR_VARIABLE,
                                                                                                    target)}
                                               for handler in entry["hooks"]]}
                          for entry in entries] for event, entries in info["groups"].items()}
        kit = trust_argument(label, folder)
        staged.append({"kit": kit, "label": label, "source": str(folder),
                       "staged": str(copy), "target": target, "sha256": info["sha256"],
                       "trusted": hooks_trusted(agent, info["sha256"], kit, records), "groups": groups,
                       "plugins": info["plugins"], "hooks": info["hooks"], "notes": info["notes"],
                       "errors": info["errors"]})
    return staged
