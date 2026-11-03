#!/usr/bin/env python3
"""Kits, project memory and plugin declarations for agent sandboxes.

A kit is a directory with one subdirectory per agent (claude/, codex/, opencode/). Each holds
any of: skills/, agents/, commands/, prompts/ (whatever the agent knows), a memory template
(CLAUDE.md or AGENTS.md) and marketplaces.json. Every sandbox gets the default kit
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
import shutil
import stat
import tempfile

HERE = Path(__file__).resolve().parent
AGENTS = ("claude", "codex", "opencode")
TEMPLATES = HERE / "templates"
# Shipped with sandbox-ai and part of every sandbox: the sandbox-ai skill. An entry of the same name in
# a kit replaces it (unlike two kits, which may not share a name).
BUILTIN = HERE / "builtin"
BUILTIN_LABEL = "built-in"
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
    if not re.fullmatch(KIT_NAME, name):
        raise ValueError(f"--kit NAME: 1-64 letters, digits, '_', '.', '-' (not starting with '.' or '-'): {name!r}")
    return kits_dir() / name / agent


def available_kits(agent=None):
    """Names of the kits in the kits directory (those with a directory for AGENT, if given)."""
    try:
        entries = sorted(os.scandir(kits_dir()), key=lambda e: e.name)
    except OSError:
        return []
    return [e.name for e in entries if re.fullmatch(KIT_NAME, e.name) and e.is_dir()
            and (agent is None or (Path(e.path) / agent).is_dir())]


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
            else:
                known = ", ".join([*(f"{k}/" for k in LAYOUT[agent]), memory_name, PLUGINS_FILE])
                notes.append(f"{label}: {name} is not used for {agent} (known: {known})")
    return {"entries": entries, "memory": memory, "plugins": plugins, "notes": notes}


def summary(plan):
    """{kind: [names]} of a scan, for messages and the dry run."""
    return {kind: sorted(items) for kind, items in plan["entries"].items() if items}


def stage(agent, plan, staging, check):
    """Copy the kits' entries to STAGING (a fresh launcher-owned directory), one directory per kind;
    returns [(staged directory, sandbox path)] to mount read-only, and notes.

    CHECK(path, label) raises for a host path the agent could change. Symlinks in kits are followed
    (their targets checked too); loops, more than KIT_FILES files or KIT_BYTES bytes are errors."""
    mounts, notes, budget = [], [], {"files": 0, "bytes": 0}
    for kind, items in plan["entries"].items():
        if not items:
            continue
        target = staging / kind
        target.mkdir(mode=0o755)
        for name, (label, source) in items.items():
            copy_tree(source, target / name, check, label, budget, notes, (), None)
        mounts.append((target, f"/root/{LAYOUT[agent][kind]}"))
    return mounts, notes


def copy_tree(source, target, check, label, budget, notes, ancestors, entry_root):
    """Copy SOURCE to TARGET. The kit entry itself may be a symlink the user made (to a skill in a Git
    checkout, say); below it, a symlink is followed only while it stays inside that entry, so that a
    file somebody else can change there (an agent in that checkout) cannot point at any host file."""
    check(source, label)
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError):
        notes.append(f"{label}: {source} skipped: dangling or looping symlink")
        return
    if resolved != source:
        check(resolved, label)
    if entry_root is None:
        entry_root = resolved
    elif source.is_symlink() and resolved != entry_root and entry_root not in resolved.parents:
        notes.append(f"{label}: {source} skipped: symlink out of its kit entry")
        return
    info = resolved.stat()
    if stat.S_ISDIR(info.st_mode):
        if resolved in ancestors:
            raise ValueError(f"{label}: {source} links back to a directory above it")
        if len(ancestors) >= KIT_DEPTH:
            raise ValueError(f"{label}: {source} is nested deeper than {KIT_DEPTH} directories")
        target.mkdir(mode=0o755)
        for child in sorted(os.scandir(resolved), key=lambda e: e.name):
            problem = bad_name(child.name)
            if problem:
                notes.append(f"{label}: {Path(child.path)} skipped: {problem}")
                continue
            copy_tree(Path(child.path), target / child.name, check, label, budget, notes, (*ancestors, resolved),
                      entry_root)
    elif stat.S_ISREG(info.st_mode):
        budget["files"] += 1
        budget["bytes"] += info.st_size
        if budget["files"] > KIT_FILES or budget["bytes"] > KIT_BYTES:
            raise ValueError(f"The kits hold more than {KIT_FILES} files or {KIT_BYTES >> 20} MiB; "
                             f"the last one counted was {source} ({label})")
        with open(resolved, "rb") as src, open(target, "xb") as dst:
            shutil.copyfileobj(src, dst)
        # Scripts in skills stay executable; nothing else is writable in the sandbox anyway.
        target.chmod(0o755 if info.st_mode & 0o111 else 0o644)
    else:
        notes.append(f"{label}: {source} skipped: not a regular file or directory")


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


def all_plugin_files(agent):
    """marketplaces.json of the default kit and of every kit: what `sandbox-ai plugins` installs."""
    files = []
    if (default_home(agent) / PLUGINS_FILE).is_file():
        files.append(("home", default_home(agent) / PLUGINS_FILE))
    for name in available_kits(agent):
        path = kits_dir() / name / agent / PLUGINS_FILE
        if path.is_file():
            files.append((f"kit {name}", path))
    return files


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
