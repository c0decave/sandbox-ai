#!/usr/bin/env python3
"""What changed in a kept HOME since its last start, in the files that decide what runs at a start.

A kept HOME carries over from one start to the next, so an agent could leave something there that runs the
next time: a line in .bashrc, a hook in its settings, a plugin, an MCP server in ~/.claude.json, a git alias,
an SSH configuration, a .pth file Python runs at every start. At every start of a kept HOME the launcher
records a manifest of these files (SHA-256, size, mode, link target; the text of small files that hold no
secrets) in the start's directory, outside the HOME, where the sandbox cannot reach it. When the start is
resumed, the HOME is compared with that manifest: every change since that start, by its agent or by anyone
else, is named, and `sandbox-ai resume NAME --review-home` shows the differences. `--restore-home` puts back the
text the manifest kept of changed or removed files (restorable, put_back): only where it is their exact content,
written anew without following a link, in the file or on the way to it.

Nothing is followed: links are recorded as links, only regular files are read (bounded), and names and
contents are escaped before they are shown. Standard library only.
"""
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
import time

MANIFEST = "home-manifest.json"
REVIEW = "home-review.json"
VERSION = 1
# Files and folders below HOME whose content decides what runs (or what is trusted) at the next start.
WATCHED = (
    # shells, terminal, desktop session
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc", ".zshenv", ".zprofile",
    ".zlogin", ".inputrc", ".tmux.conf", ".config/tmux", ".pam_environment", ".config/environment.d",
    ".config/autostart", ".local/bin", ".vimrc", ".config/nvim",
    # git, ssh, gnupg
    ".gitconfig", ".config/git", ".git-credentials", ".git-templates", ".ssh", ".gnupg/gpg.conf",
    ".gnupg/gpg-agent.conf",
    # package managers
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pypirc", ".config/pip", ".pip", ".netrc", ".cargo/config",
    ".cargo/config.toml", ".config/uv",
    # the agents
    ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks", ".claude/commands", ".claude/agents",
    ".claude/skills", ".claude/plugins", ".claude/output-styles", ".claude/CLAUDE.md",
    ".codex/config.toml", ".codex/AGENTS.md", ".codex/AGENTS.override.md", ".codex/prompts", ".codex/skills",
    ".codex/rules", ".config/opencode", ".opencode",
)
# Python runs these at every start of an interpreter (site-packages of the user).
PYTHON_STARTUP = re.compile(r"\.local/lib/python[0-9.]+/site-packages/(?:[^/]+\.pth|sitecustomize\.py|usercustomize\.py)")
# ~/.claude.json changes at every start (counters, tips); only these keys decide what runs or is allowed.
CLAUDE_STATE = ".claude.json"
CLAUDE_KEYS = ("mcpServers", "allowedTools", "enabledMcpjsonServers", "disabledMcpjsonServers",
               "hasTrustDialogAccepted")
CLAUDE_STATE_LIMIT = 64 << 20
# Contents that are never kept or shown: keys, tokens, credentials.
SECRET = re.compile(r"(^|/)(\.ssh/(?!config$|known_hosts|authorized_keys|[^/]*\.pub$).+|\.git-credentials|"
                    r"\.netrc|\.pypirc|\.npmrc|\.yarnrc(\.yml)?)$|token|secret|credential|password|\.key$",
                    re.IGNORECASE)
FILE_LIMIT = 64 << 20    # bytes hashed per file (beyond: the rest is not read; size still counts)
TEXT_LIMIT = 64 << 10    # bytes of a file's text kept for the differences
TEXT_TOTAL = 4 << 20     # bytes of text kept in one manifest
ENTRY_LIMIT = 20000      # entries recorded in all; beyond, the manifest says it is incomplete
ROOT_LIMIT = 500         # entries of one watched folder: a flood in one (.config/tmux) leaves the others recorded
DEPTH_LIMIT = 12


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_regular(path, limit):
    """(bytes up to LIMIT, size, mode) of the regular file PATH, never through a link or into a FIFO; None
    for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks), info.st_size, stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def claude_state(home):
    """The part of ~/.claude.json that decides what runs or is allowed, as text (None without the file)."""
    found = read_regular(Path(home) / CLAUDE_STATE, CLAUDE_STATE_LIMIT)
    if found is None:
        return None
    try:
        data = json.loads(found[0].decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return "(not valid JSON)\n"
    if not isinstance(data, dict):
        return "(not a JSON object)\n"
    kept = {key: data[key] for key in CLAUDE_KEYS if key in data}
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    chosen = {project: {key: value[key] for key in CLAUDE_KEYS if key in value}
              for project, value in projects.items() if isinstance(value, dict)}
    chosen = {project: value for project, value in chosen.items() if value}
    if chosen:
        kept["projects"] = chosen
    return json.dumps(kept, indent=1, sort_keys=True) + "\n"


def walk(home):
    """(relative path, absolute path, lstat) of every watched entry in HOME: the WATCHED files and folders (their
    contents, links not followed) and Python's start-up files; at most ENTRY_LIMIT. Second value: complete."""
    home = Path(home)
    found, complete, seen = [], True, set()
    budget = [ROOT_LIMIT]  # of the watched entry being walked

    def add(relative):
        nonlocal complete
        if relative in seen:
            return True
        if len(found) >= ENTRY_LIMIT or budget[0] <= 0:
            complete = False
            return False
        budget[0] -= 1
        seen.add(relative)
        path = home / relative
        try:
            info = os.lstat(path)
        except OSError:
            return True
        found.append((relative, path, info))
        if stat.S_ISDIR(info.st_mode) and relative.count("/") < DEPTH_LIMIT:
            try:
                names = sorted(os.listdir(path))
            except OSError:
                return True
            for name in names:
                if not add(f"{relative}/{name}"):
                    return False
        return True
    # Every directory on the way to a watched entry must be a real directory: a link there would take the
    # walk elsewhere (it is recorded itself, as a link).
    for relative in WATCHED:
        budget[0] = ROOT_LIMIT  # each watched entry its own: a full one stops only itself
        parts = PurePosixPath(relative).parts
        blocked = next((str(PurePosixPath(*parts[:index])) for index in range(1, len(parts))
                        if not safe_dir(home / PurePosixPath(*parts[:index]))), None)
        if blocked is not None:
            if os.path.islink(home / blocked):
                add(blocked)
            continue
        add(relative)  # a full one is recorded as far as its budget goes; the next ones still are
    lib = home / ".local" / "lib"
    if all(safe_dir(path) for path in (home / ".local", lib)):
        for python in sorted(os.listdir(lib)) if lib.is_dir() else []:
            packages = lib / python / "site-packages"
            if not (safe_dir(lib / python) and safe_dir(packages)):
                continue
            budget[0] = ROOT_LIMIT
            for name in sorted(os.listdir(packages)):
                relative = f".local/lib/{python}/site-packages/{name}"
                if PYTHON_STARTUP.fullmatch(relative) and not add(relative):
                    break
    return found, complete


def safe_dir(path):
    """PATH is a directory itself, not a link to one."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def snapshot(home):
    """{"files": {path: {type, size, mode, sha256 | target}}, "texts": {path: text}, "complete": bool} of the
    watched entries in HOME."""
    files, texts, total = {}, {}, 0
    entries, complete = walk(home)
    for relative, path, info in entries:
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            try:
                files[relative] = {"type": "link", "target": os.readlink(path)}
            except OSError:
                files[relative] = {"type": "link", "target": None}
        elif stat.S_ISDIR(info.st_mode):
            files[relative] = {"type": "dir", "mode": mode}
        elif stat.S_ISREG(info.st_mode):
            found = read_regular(path, FILE_LIMIT)
            if found is None:
                continue  # replaced meanwhile
            data, size, mode = found
            files[relative] = {"type": "file", "size": size, "mode": mode, "sha256": hashlib.sha256(data).hexdigest()}
            if not SECRET.search(relative) and size <= TEXT_LIMIT and total + size <= TEXT_TOTAL and b"\0" not in data:
                texts[relative] = data.decode("utf-8", "replace")
                total += size
        else:
            files[relative] = {"type": "other", "mode": mode}
    state = claude_state(home)
    if state is not None:
        relative = f"{CLAUDE_STATE} (mcpServers, allowedTools, ...)"
        files[relative] = {"type": "file", "size": len(state), "sha256": hashlib.sha256(state.encode()).hexdigest()}
        texts[relative] = state
    return {"files": files, "texts": texts, "complete": complete}


def write_private(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".home-", dir=Path(path).parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
    os.replace(temporary, path)


def record(home, directory, run=None):
    """Record the manifest of HOME in DIRECTORY (the start's directory, outside HOME) at a start."""
    write_private(Path(directory) / MANIFEST, {"version": VERSION, "recorded": now(), "run": run, **snapshot(home)})


def load(directory):
    """The manifest recorded in DIRECTORY, or None (never recorded: a start before 0.1.30, or damaged)."""
    try:
        data = json.loads((Path(directory) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("files"), dict):
        return None
    if not isinstance(data.get("texts"), dict):
        data["texts"] = {}
    return data


def describe(before, after):
    """What happened to one entry: added, removed, changed, mode (only the permissions), type (file to link ...)."""
    if before is None:
        return "added"
    if after is None:
        return "removed"
    if before.get("type") != after.get("type"):
        return "type"
    if before.get("type") == "link":
        return "changed" if before.get("target") != after.get("target") else None
    if before.get("sha256") != after.get("sha256") or before.get("size") != after.get("size"):
        return "changed"
    if before.get("mode") != after.get("mode"):
        return "mode"
    return None


def differences(old_text, new_text, path):
    """A unified diff of two texts (either may be None: not kept), or None when there is nothing to show."""
    if old_text is None and new_text is None:
        return None
    lines = list(difflib.unified_diff((old_text or "").splitlines(), (new_text or "").splitlines(),
                                      f"{path} (at the last start)", f"{path} (now)", n=2, lineterm=""))
    return "\n".join(lines) if lines else None


def compare(home, directory, with_diffs=False):
    """The changes of HOME since the manifest in DIRECTORY was recorded: {"since", "run", "changes": [{"path",
    "change", "kind", "before", "after", "diff"?, "note"?}], "complete"}; None without a manifest."""
    manifest = load(directory)
    if manifest is None:
        return None
    current = snapshot(home)
    changes = []
    for path in sorted(set(manifest["files"]) | set(current["files"])):
        before, after = manifest["files"].get(path), current["files"].get(path)
        change = describe(before, after)
        if change is None:
            continue
        entry = {"path": path, "change": change, "kind": (after or before or {}).get("type"),
                 "before": before, "after": after}
        if with_diffs and change in ("added", "removed", "changed") and (after or before or {}).get("type") == "file":
            if SECRET.search(path):
                entry["note"] = "not shown: it may hold a key or token"
            else:
                old_text, new_text = manifest["texts"].get(path), current["texts"].get(path)
                missing = (before is not None and old_text is None) or (after is not None and new_text is None)
                if missing:
                    entry["note"] = "no text kept (binary or large); compare by hand"
                else:
                    entry["diff"] = differences(old_text, new_text, path)
        if change == "mode":
            entry["note"] = f"mode {oct(before.get('mode') or 0)} -> {oct(after.get('mode') or 0)}"
        if before and after and before.get("type") == "link" == after.get("type"):
            entry["note"] = f"link to {before.get('target')} -> {after.get('target')}"
        changes.append(entry)
    return {"since": manifest.get("recorded"), "run": manifest.get("run"), "changes": changes,
            "complete": bool(manifest.get("complete", True)) and current["complete"]}


def watched(relative):
    """RELATIVE (a path below HOME, as the manifest names it) is a watched entry or lies below one."""
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("", ".", "..") for part in parts) or PurePosixPath(relative).is_absolute():
        return False
    return PYTHON_STARTUP.fullmatch(relative) is not None or any(
        relative == root or relative.startswith(root + "/") for root in WATCHED)


def restorable(review, manifest):
    """What --restore-home can put back of REVIEW's changes (compare's result) from MANIFEST (load's): [{"path",
    "change", "data" (bytes) or None, "mode", "why" (when it cannot)}]. Only regular files the manifest kept the
    exact text of (its SHA-256 and size match: small text files that hold no key or token); never what was added
    since (there is nothing to put back), links, folders, or the part of ~/.claude.json the manifest records."""
    result = []
    for entry in (review or {}).get("changes", []):
        path, change, before, after = entry["path"], entry["change"], entry.get("before"), entry.get("after")
        item = {"path": path, "change": change, "data": None, "mode": None, "why": None}
        text = manifest["texts"].get(path) if before else None
        data = text.encode("utf-8") if isinstance(text, str) else None
        if not watched(path):
            item["why"] = "only a part of it is recorded" if path.startswith(CLAUDE_STATE) else "not a watched file"
        elif change == "added":
            item["why"] = "added since its last start: nothing to put back (remove it yourself if it is not yours)"
        elif (before or {}).get("type") != "file":
            item["why"] = f"a {'link' if (before or {}).get('type') == 'link' else 'folder or other entry'}: not put back"
        elif (after or {}).get("type") == "dir":
            item["why"] = "a folder is there now: remove it yourself first"
        elif SECRET.search(path):
            item["why"] = "it may hold a key or token: its text is never kept"
        elif data is None:
            item["why"] = "no text kept (binary, or larger than the limits)"
        elif len(data) != before.get("size") or hashlib.sha256(data).hexdigest() != before.get("sha256"):
            item["why"] = "the text kept is not its exact content (not UTF-8)"
        else:
            item.update(data=data, mode=int(before.get("mode") or 0o600) & 0o777)
        result.append(item)
    return result


def put_back(home, relative, data, mode, folder_modes=None):
    """Write DATA (with MODE) to HOME/RELATIVE as a new file that replaces what is there (a link there is replaced,
    never followed). Every folder on the way must be a real folder (a link there: OSError, nothing written); one
    that is missing is made (its mode as FOLDER_MODES {relative: mode} recorded it, else 0700)."""
    parts = PurePosixPath(relative).parts
    if not watched(relative):
        raise ValueError(f"{relative}: not a watched file")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(home, flags)
    temporary = None
    try:
        for index, part in enumerate(parts[:-1]):
            try:
                inner = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, (folder_modes or {}).get("/".join(parts[:index + 1]), 0o700) & 0o777, dir_fd=fd)
                inner = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = inner
        try:
            if stat.S_ISDIR(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
                raise ValueError(f"{relative}: a folder is there now")
        except FileNotFoundError:
            pass
        temporary = f".sandbox-ai-restore-{secrets.token_hex(6)}"
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                      0o600, dir_fd=fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(out, view):]
            os.fchmod(out, mode & 0o777)
            os.fsync(out)
        finally:
            os.close(out)
        os.rename(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)  # replaces a link itself, never its target
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=fd)
            except OSError:
                pass
        os.close(fd)


def save_review(directory, review):
    """Keep REVIEW (compare's result, with differences) in DIRECTORY: what changed before the latest start, so
    that it can still be read while that start runs (its manifest is new by then)."""
    write_private(Path(directory) / REVIEW, {"version": VERSION, "reviewed": now(), **review})


def saved_review(directory):
    try:
        data = json.loads((Path(directory) / REVIEW).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == VERSION else None


def shown_path(path, printable):
    return printable(str(path)).replace("\n", "\\n")


def notice(review, name, printable, limit=20):
    """Lines that name what changed (REVIEW from compare) for the terminal, or [] when nothing did."""
    if not review or not review["changes"]:
        return [] if not review or review["complete"] else [
            f"{printable(name)}: its HOME has more files than checked; not every change can be named"]
    changes = review["changes"]
    lines = [f"the HOME of {printable(name)} changed since its last start ({review.get('since') or '?'}) in files "
             "that decide what runs at a start (shell, git, ssh, the agent's settings, hooks, plugins, skills):"]
    for entry in changes[:limit]:
        lines.append(f"  {entry['change']:<8} {shown_path(entry['path'], printable)}")
    if len(changes) > limit:
        lines.append(f"  ... and {len(changes) - limit} more")
    if not review["complete"]:
        lines.append("  (the HOME has more files than checked: not every change is named)")
    lines.append(f"  If you did not make these changes, check them before you go on: sandbox-ai resume "
                 f"{printable(name)} --review-home")
    return lines


def report(review, name, printable, out=print):
    """REVIEW with its differences, for the terminal; every line escaped."""
    if review is None:
        out(f"{printable(name)}: no record of its HOME yet (it is made at every start from version 0.1.30 on)")
        return
    if not review["changes"]:
        out(f"{printable(name)}: nothing changed in the watched files of its HOME since its last start "
            f"({review.get('since') or '?'})")
        return
    for line in notice(review, name, printable, limit=len(review["changes"]))[:-1]:
        out(line)
    for entry in review["changes"]:
        out("")
        out(f"== {entry['change']} {shown_path(entry['path'], printable)}"
            + (f" ({printable(entry['note'])})" if entry.get("note") else ""))
        for line in (entry.get("diff") or "").splitlines():
            out(printable(line))
