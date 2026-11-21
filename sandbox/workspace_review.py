#!/usr/bin/env python3
"""The workspace review: what the agent changed in a workspace that your host runs or trusts.

A sandboxed agent cannot touch the host, except through the workspace the host's IDE, git and agents trust: a Git hook
or a filter in .git/config runs at your next commit, a VS Code task at the folder's opening, a package.json script at
the next npm install, a Claude Code hook or an .mcp.json server at the next agent's start, a CI workflow on the next
push. At a start the launcher records those files (record); after the run it names every change (compare), with a
diff and the hidden-text checker's findings (lint.py): a warning, a safeguard record of kind workspace-review and a
line in the log directory's workspace-review.jsonl, which

  sandbox-ai workspace review [RUN] [--project DIR | --log-dir DIR | --all] [--json]
  sandbox-ai workspace review RUN [--log-dir DIR] --keep PATH | --restore PATH --yes

shows again with the diffs. Nothing is reverted on its own: the human decides, per file. Keep records that the
change is wanted; Restore puts the file back as it was at the start - from the before-state the launcher recorded
in sandbox-ai's own state folder (store_path: $XDG_STATE_HOME/sandbox-ai/workspace-review/, which no sandbox
reaches; never from the log folder, which the sandbox's proxy writes): a file the agent added is removed, a changed
or removed one written back with its mode, a link made again. Only text files of at most READ_LIMIT are recorded
(a binary or larger file has no before-state: Keep only, or the start's checkpoint: sandbox-ai checkpoint restore);
a file changed again since the review is not touched, and nothing is written through a link on the way. What is watched (WATCHED, WATCHED_TREES): Git's hooks and configuration, editor tasks
and settings (.vscode, .idea, .devcontainer, .envrc), the agents' settings and MCP servers (.claude, .mcp.json,
.cursor, .codex, .gemini, .qwen), build and install scripts (package.json, Makefile, .pre-commit-config.yaml, .husky),
CI workflows, and the agents' instruction files. Standard library only.
"""
import argparse
import contextlib
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
import time

HERE = Path(__file__).resolve().parent
FILE = "workspace-review.jsonl"   # in the run's log directory
FILES_LIMIT = 2000                # files recorded per workspace
READ_LIMIT = 256 << 10            # bytes of a file kept for its diff (larger: digest only)
HASH_LIMIT = 16 << 20             # bytes hashed (larger: size and times)
DIFF_LINES = 120                  # lines of one diff kept
TREE_DEPTH = 8
VERSION = 1
STORE_REVIEWS = 20                # reviews of one run kept in the state folder (a kept start reruns under its id)
STORE_LIMIT = 64 << 20            # bytes of one run's stored reviews read
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
DECISIONS = ("kept", "restored")

WATCHED = (".git/config", ".git/info/attributes", ".gitmodules", ".gitattributes", ".envrc", ".mcp.json",
           "package.json", ".npmrc", ".yarnrc", ".yarnrc.yml", "Makefile", "makefile", "GNUmakefile", "justfile",
           ".pre-commit-config.yaml", "setup.py", "conftest.py", ".python-version", ".tool-versions",
           ".claude.json")
WATCHED_TREES = (".git/hooks", ".vscode", ".idea", ".devcontainer", ".claude", ".cursor", ".codex", ".gemini",
                 ".qwen", ".github/workflows", ".gitlab-ci.yml", ".husky", ".continue", ".windsurf", ".clinerules",
                 ".opencode", ".goose", ".letta")
# Why a change matters (shown beside the diff), by the path's start.
WHY = (
    (".git/hooks/", "a Git hook runs on your host at the next commit, checkout or push"),
    (".git/config", "Git runs what core.hooksPath, core.fsmonitor, core.sshCommand, core.pager, alias.*, filter.*, "
                    "diff.*.textconv, credential.* and url.*.insteadOf name, on your host"),
    (".gitattributes", "attributes choose the filters and diff drivers Git runs"),
    (".git/info/attributes", "attributes choose the filters and diff drivers Git runs"),
    (".gitmodules", "submodules decide what Git fetches and checks out"),
    (".vscode/", "VS Code runs tasks (runOn: folderOpen), launch configurations and settings of the folder"),
    (".idea/", "JetBrains IDEs run the folder's run configurations"),
    (".devcontainer/", "a dev container's configuration runs commands on your host's Docker"),
    (".envrc", "direnv runs it when your shell enters the folder"),
    (".claude/", "Claude Code on your host runs the folder's hooks and reads its commands, agents and skills"),
    (".claude.json", "Claude Code's configuration (MCP servers it starts)"),
    (".mcp.json", "an agent on your host starts the MCP servers it names"),
    (".cursor/", "Cursor reads the folder's rules and MCP servers"),
    (".codex/", "Codex reads the folder's configuration"),
    (".gemini/", "Gemini CLI reads the folder's settings and commands"),
    (".qwen/", "Qwen Code reads the folder's settings and commands"),
    ("package.json", "npm runs its scripts (install, prepare, test ...)"),
    (".npmrc", "npm's configuration (registries, scripts)"),
    ("Makefile", "make runs it"), ("makefile", "make runs it"), ("GNUmakefile", "make runs it"),
    ("justfile", "just runs it"), ("setup.py", "pip runs it at an install from the folder"),
    ("conftest.py", "pytest runs it"), (".pre-commit-config.yaml", "pre-commit runs its hooks at your commits"),
    (".husky/", "husky's Git hooks run at your commits"),
    (".github/workflows/", "a CI workflow runs with the repository's secrets after a push"),
    (".gitlab-ci.yml", "GitLab CI runs it after a push"),
)
_lint = []


def lint():
    if not _lint:
        spec = importlib.util.spec_from_file_location("sandbox_ai_lint_for_review", HERE / "lint.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _lint.append(loaded)
    return _lint[0]


def why(relative):
    for prefix, text in WHY:
        if relative == prefix.rstrip("/") or relative.startswith(prefix):
            return text
    if relative in [path for path, _ in lint().instruction_files("/nonexistent")] or relative.endswith(".md"):
        return "an agent reads it as instructions"
    return "a file your host's tools run or trust"


def candidates(workspace):
    """[relative path] of WORKSPACE's watched files (none followed through a symlink outside it)."""
    workspace = Path(workspace)
    found = list(WATCHED) + list(lint().INSTRUCTION_FILES)
    for tree in WATCHED_TREES + lint().INSTRUCTION_TREES:
        top = workspace / tree
        try:
            info = os.lstat(top)
        except OSError:
            found.append(tree)  # a file of that name may appear
            continue
        if not stat.S_ISDIR(info.st_mode):
            found.append(tree)
            continue
        base = len(top.parts)
        for directory, dirs, names in os.walk(top, followlinks=False):
            depth = len(Path(directory).parts) - base
            dirs[:] = sorted(d for d in dirs if depth < TREE_DEPTH and d not in ("node_modules", ".git"))
            for name in sorted(names):
                found.append((Path(directory) / name).relative_to(workspace).as_posix())
                if len(found) > FILES_LIMIT:
                    return list(dict.fromkeys(found))
    return list(dict.fromkeys(found))


def entry(path, dir_fd=None):
    """{"kind", "size", "sha256", "text"?, "target"?} of PATH (never followed; in the folder DIR_FD when given), or
    None when it is not there."""
    try:
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        try:
            return {"kind": "link", "target": os.readlink(path, dir_fd=dir_fd)[:1000]}
        except OSError:
            return {"kind": "link", "target": "?"}
    if not stat.S_ISREG(info.st_mode):
        return {"kind": "other"} if not stat.S_ISDIR(info.st_mode) else None
    found = {"kind": "file", "size": info.st_size, "mode": stat.S_IMODE(info.st_mode)}
    if info.st_size > HASH_LIMIT:
        found["sha256"] = f"large:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
        return found
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    except OSError:
        found["sha256"] = "unreadable"
        return found
    try:
        data = b""
        while len(data) <= HASH_LIMIT:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    found["sha256"] = hashlib.sha256(data).hexdigest()
    if len(data) <= READ_LIMIT and b"\0" not in data[:8192]:
        found["text"] = data.decode("utf-8", "replace")
    return found


def record(workspaces):
    """At a start: {workspace: {relative: entry}} of the watched files of each WORKSPACE (host paths)."""
    manifest = {}
    for workspace in workspaces:
        workspace = Path(workspace)
        files = {}
        for relative in candidates(workspace):
            found = entry(workspace / relative)
            if found is not None:
                files[relative] = found
        manifest[str(workspace)] = files
    return manifest


def diff(before, after, relative):
    """A unified diff of two entries (text), bounded; a sentence where there is no text to compare."""
    if before and before.get("kind") == "link" or after and after.get("kind") == "link":
        return [f"link: {(before or {}).get('target', '(none)')} -> {(after or {}).get('target', '(none)')}"]
    old = (before or {}).get("text")
    new = (after or {}).get("text")
    if (before and old is None) or (after and new is None):
        return [f"(binary or larger than {READ_LIMIT >> 10} KiB: compare it yourself; sha256 "
                f"{str((before or {}).get('sha256', '-'))[:16]} -> {str((after or {}).get('sha256', '-'))[:16]})"]
    lines = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), f"a/{relative}",
                                      f"b/{relative}", lineterm="", n=2))
    if len(lines) > DIFF_LINES:
        lines = lines[:DIFF_LINES] + [f"... {len(lines) - DIFF_LINES} more lines"]
    if before and after and before.get("mode") != after.get("mode"):
        lines.append(f"mode {oct(before.get('mode') or 0)} -> {oct(after.get('mode') or 0)}")
    return lines


def compare(manifest, mapping=None):
    """After the run: [{"workspace", "sandbox", "path", "change": added|changed|removed, "why", "diff": [lines],
    "findings": [lines]}] of the watched files that changed since record(). MAPPING: {host path: sandbox path}."""
    changes = []
    for workspace, before in manifest.items():
        after = record([workspace])[workspace]
        for relative in sorted(set(before) | set(after)):
            old, new = before.get(relative), after.get(relative)
            if old == new or (old and new and {k: v for k, v in old.items() if k != "text"} ==
                              {k: v for k, v in new.items() if k != "text"}):
                continue
            change = "added" if old is None else "removed" if new is None else "changed"
            findings = lint().lines(lint().check_text(new["text"], relative), limit=10) \
                if new and isinstance(new.get("text"), str) else []
            changes.append({"workspace": workspace, "sandbox": (mapping or {}).get(workspace, workspace),
                            "path": relative, "change": change, "why": why(relative),
                            "diff": diff(old, new, relative), "findings": findings,
                            "before": old, "after": state_of(new)})
    return changes


def state_of(found):
    """What restore() compares a file with before it writes (ENTRY without its text): kind, digest, link target."""
    return None if found is None else {k: v for k, v in found.items() if k != "text"}


def shown_change(change):
    """CHANGE as it is listed and logged: without the before and after states (file contents)."""
    return {k: v for k, v in change.items() if k not in ("before", "after")}


def summary_lines(changes, limit=12, diff_lines=12):
    """What a terminal shows of CHANGES after the run."""
    lines = []
    for item in changes[:limit]:
        lines.append(f"{item['change']} {item['sandbox']}/{item['path']}: {item['why']}")
        lines += [f"    {line}" for line in item["diff"][:diff_lines]]
        if len(item["diff"]) > diff_lines:
            lines.append(f"    ... (the whole diff: sandbox-ai workspace review)")
        lines += [f"    hidden-text finding: {line}" for line in item["findings"][:3]]
    if len(changes) > limit:
        lines.append(f"... and {len(changes) - limit} more (sandbox-ai workspace review)")
    return lines


def save(log_dir, run, changes):
    """A line of LOG_DIR/workspace-review.jsonl: this run's changes."""
    path = Path(log_dir) / FILE
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with open(fd, "a") as stream:
        stream.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run": run,
                                 "changes": [shown_change(change) for change in changes]}, ensure_ascii=False) + "\n")
    return path


def logged_reviews(log_dir, run=None):
    """The reviews of LOG_DIR's workspace-review.jsonl (newest last), RUN's only when given: what was shown after each
    run. The sandbox's proxy writes in that folder: nothing in it is ever written back to a workspace."""
    found = []
    try:
        with open(Path(log_dir) / FILE, "rb") as stream:
            data = stream.read(64 << 20)
    except OSError:
        return found
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict) and isinstance(item.get("changes"), list) and (run is None or item.get("run") == run):
            found.append(item)
    return found


# --- the before-state, in sandbox-ai's state folder (no sandbox reaches it): Keep and Restore -------------------------

def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def store_dir(log_dir):
    """The state folder of the reviews of LOG_DIR's runs: NAME-HASH of the log folder."""
    log_dir = Path(log_dir).expanduser().resolve()
    digest = hashlib.sha256(os.fsencode(str(log_dir))).hexdigest()[:16]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", log_dir.name or "root")[:60].lstrip(".") or "root"
    return state_root() / "workspace-review" / f"{name}-{digest}"


def check_run(run):
    if not isinstance(run, str) or not RUN_ID.fullmatch(run):
        raise ValueError(f"not a run id: {printable(str(run)[:60])!r} (sandbox-ai workspace review lists them)")
    return run


def store_path(log_dir, run):
    return store_dir(log_dir) / f"{check_run(run)}.json"


def private_dir(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{directory}: not a folder of yours (a link?) - remove it")
    return directory


@contextlib.contextmanager
def store_lock(log_dir):
    """One change of LOG_DIR's stored reviews at a time (the run that ends, a decision of the app or a terminal)."""
    import fcntl
    directory = private_dir(store_dir(log_dir))
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)


def read_store(path):
    """The stored reviews of one run ([{"time", "run", "changes", "decisions"}], oldest first), [] when none."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        data = os.read(fd, STORE_LIMIT + 1)
    finally:
        os.close(fd)
    try:
        document = json.loads(data) if len(data) <= STORE_LIMIT else None
    except (ValueError, RecursionError):
        return []
    if not isinstance(document, dict) or document.get("version") != VERSION:
        return []
    return [item for item in document.get("reviews") or [] if isinstance(item, dict)
            and isinstance(item.get("changes"), list) and isinstance(item.get("decisions", {}), dict)]


def write_store(path, reviews, log_dir):
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with open(fd, "w") as stream:
        json.dump({"version": VERSION, "log_dir": str(Path(log_dir).expanduser().resolve()),
                   "reviews": reviews[-STORE_REVIEWS:]}, stream, ensure_ascii=False)
    os.replace(temporary, path)
    return path


def store_log_dir(directory):
    """The log folder whose reviews the state folder DIRECTORY keeps (its files say), or None."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for name in names:
        if name.endswith(".json") and RUN_ID.fullmatch(name[:-5]):
            try:
                with open(Path(directory) / name, "rb") as stream:
                    document = json.loads(stream.read(STORE_LIMIT))
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(document, dict) and isinstance(document.get("log_dir"), str) and \
                    store_dir(document["log_dir"]) == Path(directory):
                return document["log_dir"]
    return None


def all_reviews():
    """The reviews of every project (newest last), each with its "log_dir": the log folders the state folder keeps
    before-states for, and every default log folder (state/sandbox-ai/logs/*)."""
    folders = []
    try:
        folders += [store_log_dir(entry) for entry in sorted((state_root() / "workspace-review").iterdir())]
    except OSError:
        pass
    try:
        folders += [str(entry) for entry in sorted((state_root() / "logs").iterdir()) if entry.is_dir()]
    except OSError:
        pass
    found = []
    for log_dir in dict.fromkeys(str(Path(folder).resolve()) for folder in folders if folder):
        found += [{**item, "log_dir": log_dir} for item in reviews(log_dir)]
    return sorted(found, key=lambda item: str(item.get("time") or ""))


def keep_before(log_dir, run, changes):
    """After the run: CHANGES (compare's, with their before and after states) kept for Keep and Restore in the state
    folder of LOG_DIR's reviews. Its path."""
    with store_lock(log_dir):
        path = store_path(log_dir, run)
        stored = read_store(path)
        stored.append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run": run, "changes": changes,
                       "decisions": {}})
        return write_store(path, stored, log_dir)


def key_of(change):
    """The name of CHANGE in a review: its path in the sandbox (/work/.git/hooks/pre-commit)."""
    return f"{str(change.get('sandbox') or '').rstrip('/')}/{change.get('path')}"


def why_not(change):
    """Why CHANGE cannot be restored from its before-state, or None when it can."""
    before, after = change.get("before"), change.get("after")
    relative = change.get("path")
    parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
    if not parts or PurePosixPath(relative).is_absolute() or any(part in ("", ".", "..") for part in parts):
        return "not a path inside the workspace"
    if change.get("change") not in ("added", "changed", "removed"):
        return "an unknown change"
    if change.get("change") == "added":
        return None if before is None and isinstance(after, dict) and after.get("kind") in ("file", "link", "other") \
            else "no before-state recorded"
    if not isinstance(before, dict):
        return "no before-state recorded (a run before sandbox-ai 0.1.133, or only its log line is left)"
    if before.get("kind") == "link":
        target = before.get("target")
        return None if isinstance(target, str) and 0 < len(target) < 1000 and target != "?" else \
            "no before-state recorded (the link's target was not readable)"
    if before.get("kind") != "file":
        return "no before-state recorded (neither a file nor a link)"
    text = before.get("text")
    if not isinstance(text, str):
        return f"no before-state recorded (binary, or larger than {READ_LIMIT >> 10} KiB)"
    if hashlib.sha256(text.encode()).hexdigest() != before.get("sha256"):
        return "no before-state recorded (its text is not its exact content: not UTF-8)"
    return None


def listed(change, decisions):
    """CHANGE as the listing shows it: whether it can be restored, why not, and what was decided."""
    reason = why_not(change)
    return {**shown_change(change), "restorable": reason is None, "why_not": reason,
            "decision": decisions.get(key_of(change))}


def reviews(log_dir, run=None):
    """Every review of LOG_DIR's runs (newest last), RUN's only when given: the stored ones (with their before-state:
    Keep and Restore) and those only the log folder has (Keep only), each change with "restorable", "why_not" and
    "decision"."""
    found = []
    stored_runs = set()
    if run is not None:
        names = [f"{check_run(run)}.json"]
    else:
        try:
            names = sorted(os.listdir(store_dir(log_dir)))
        except OSError:
            names = []
    for name in names:
        if not name.endswith(".json") or not RUN_ID.fullmatch(name[:-5]):
            continue
        for item in read_store(store_dir(log_dir) / name):
            decisions = item.get("decisions") or {}
            stored_runs.add((item.get("run"), item.get("time")))
            found.append({"time": item.get("time"), "run": item.get("run"), "stored": True,
                          "changes": [listed(change, decisions) for change in item["changes"]
                                      if isinstance(change, dict)]})
    stored_by_run = {}
    for run_id, when in stored_runs:
        stored_by_run.setdefault(run_id, []).append(str(when))
    for item in logged_reviews(log_dir, run):
        times = stored_by_run.get(item.get("run"), [])
        if any(abs(seconds(item.get("time")) - seconds(when)) <= 120 for when in times):
            continue  # (the same review, stored with its before-state)
        found.append({"time": item.get("time"), "run": item.get("run"), "stored": False,
                      "changes": [listed({k: v for k, v in change.items() if k not in ("before", "after")}, {})
                                  for change in item["changes"] if isinstance(change, dict)]})
    return sorted(found, key=lambda item: str(item.get("time") or ""))


def seconds(text):
    try:
        return time.mktime(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return 0.0


def find_change(reviews_of_run, wanted):
    """(review index, change) of the newest stored review naming WANTED: its path in the sandbox (/work/x), relative
    to its workspace (x, when only one workspace has it) or on the host (/home/you/project/x)."""
    for index in range(len(reviews_of_run) - 1, -1, -1):
        matches = []
        for change in reviews_of_run[index]["changes"]:
            if not isinstance(change, dict):
                continue
            host = f"{str(change.get('workspace') or '').rstrip('/')}/{change.get('path')}"
            if wanted in (key_of(change), change.get("path"), host):
                matches.append(change)
        if len(matches) > 1:
            raise ValueError(f"{printable(wanted)} names a file in {len(matches)} workspaces: give its path in the "
                             f"sandbox ({', '.join(printable(key_of(m)) for m in matches[:4])})")
        if matches:
            return index, matches[0]
    return None, None


def walk_to(workspace, relative, make=False):
    """(descriptor of the folder RELATIVE lies in, its last part): every folder on the way a real folder, never a
    link (OSError ELOOP or ENOTDIR otherwise); MAKE makes a missing one (0755)."""
    parts = PurePosixPath(relative).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(workspace, flags)
    try:
        for part in parts[:-1]:
            try:
                inner = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not make:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                inner = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = inner
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def current_state(workspace, relative):
    """The state of WORKSPACE/RELATIVE now, as compare() recorded it (state_of(entry)), read through no link on the
    way (a link there: OSError)."""
    try:
        fd, name = walk_to(workspace, relative)
    except FileNotFoundError:
        return None
    try:
        return state_of(entry(name, dir_fd=fd))
    finally:
        os.close(fd)


def put_back(workspace, relative, before):
    """Write BEFORE (a file's text and mode, a link) to WORKSPACE/RELATIVE as a new entry that replaces what is there
    (a link there is replaced, never followed); BEFORE None removes it. No link on the way."""
    fd, name = walk_to(workspace, relative, make=before is not None)
    temporary = None
    try:
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode):
            raise ValueError("a folder is there now: remove it yourself first")
        if before is None:
            if info is not None:
                os.unlink(name, dir_fd=fd)
            return
        temporary = f".sandbox-ai-restore-{secrets.token_hex(6)}"
        if before.get("kind") == "link":
            os.symlink(before["target"], temporary, dir_fd=fd)
        else:
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                          dir_fd=fd)
            try:
                view = memoryview(before["text"].encode())
                while view:
                    view = view[os.write(out, view):]
                os.fchmod(out, int(before.get("mode") or 0o644) & 0o7777 & ~0o6000)
                os.fsync(out)
            finally:
                os.close(out)
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)  # replaces a link itself, never its target
        temporary = None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=fd)
        os.close(fd)


def decide(log_dir, run, wanted, decision, say=print):
    """Keep (DECISION "kept") or restore ("restored") the change WANTED names in RUN's newest stored review that has
    it; the decision is recorded beside it. ValueError: refused (why)."""
    if decision not in DECISIONS:
        raise ValueError(f"a decision is one of {', '.join(DECISIONS)}")
    with store_lock(log_dir):
        path = store_path(log_dir, run)
        stored = read_store(path)
        index, change = find_change(stored, wanted)
        if change is None:
            logged = [item for item in logged_reviews(log_dir, run) if any(
                isinstance(c, dict) and wanted in (key_of(c), c.get("path")) for c in item["changes"])]
            if logged and decision == "restored":
                raise ValueError(f"{printable(wanted)}: no before-state recorded for run {printable(run)} (only its "
                                 "log line is left): keep it (--keep) or put it back from the start's checkpoint "
                                 "(sandbox-ai checkpoint restore)")
            if not logged:
                raise ValueError(f"run {printable(run)} changed no host-trusted file named {printable(wanted)} "
                                 "(sandbox-ai workspace review lists them)")
            # Kept from its log line only: the review is stored without any before-state (the log folder is the
            # sandbox's too: nothing of it can ever be restored), for the decision to be recorded beside it.
            stored.append({"time": logged[-1].get("time"), "run": run, "decisions": {}, "logged": True,
                           "changes": [shown_change(c) for c in logged[-1]["changes"] if isinstance(c, dict)]})
            index, change = find_change(stored, wanted)
        key = key_of(change)
        done = stored[index].setdefault("decisions", {}).get(key)
        if done and done.get("decision") == "restored":
            raise ValueError(f"{printable(key)} was already restored ({printable(done.get('time'))})")
        if decision == "restored":
            reason = why_not(change)
            if reason is not None:
                raise ValueError(f"{printable(key)}: {reason}: keep it (--keep) or put it back from the start's "
                                 "checkpoint (sandbox-ai checkpoint restore)")
            workspace = str(change.get("workspace") or "")
            if not workspace or not os.path.isabs(workspace):
                raise ValueError(f"{printable(key)}: its workspace is not known")
            try:
                now = current_state(workspace, change["path"])
            except OSError as exc:
                raise ValueError(f"{printable(key)}: a link or something other than a folder is on the way to it now "
                                 f"({exc.strerror or exc}): nothing was written") from None
            if now != change.get("after"):
                raise ValueError(f"{printable(key)} changed since the review: compare it yourself (nothing was "
                                 "written)")
            try:
                put_back(workspace, change["path"], None if change["change"] == "added" else change["before"])
            except OSError as exc:
                raise ValueError(f"{printable(key)}: not restored ({exc.strerror or exc}): a link or something "
                                 "other than a folder may be on the way to it") from None
        stored[index]["decisions"][key] = {"decision": decision,
                                           "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        write_store(path, stored, log_dir)
    say(f"{printable(key)}: {'restored as it was at the start' + (' (removed: the agent added it)' if decision == 'restored' and change['change'] == 'added' else '') if decision == 'restored' else 'kept as the agent left it'}")
    return key


def printable(text):
    return "".join(c if c.isprintable() or c == "\t" else lint().code(c) for c in str(text))


def default_log_dir(project):
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    project = Path(project)
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    return state / "sandbox-ai" / "logs" / f"{project.name or 'root'}-{digest}"


def parser(add_help=True):
    p = argparse.ArgumentParser(prog="sandbox-ai workspace review", allow_abbrev=False, add_help=add_help,
                                description="What the agent changed in a workspace that your host runs or trusts "
                                            "(Git hooks and configuration, editor tasks, agent settings, build "
                                            "scripts, CI workflows, instruction files), with the diffs; per file, "
                                            "keep the change or restore the file as it was at the start.")
    p.add_argument("run_id", nargs="?", metavar="RUN", help="only this run (its id); default: every run with changes")
    p.add_argument("--project", metavar="DIR", default=".", help="the project (default: here)")
    p.add_argument("--log-dir", metavar="DIR", help="this log directory instead of the project's")
    p.add_argument("--run", metavar="RUN", help="only this run (as RUN)")
    p.add_argument("--all", action="store_true", help="the runs of every project, each with its log folder")
    p.add_argument("--keep", action="append", default=[], metavar="PATH",
                   help="keep this change of RUN (its path in the sandbox, in the workspace, or on the host); "
                        "repeatable")
    p.add_argument("--restore", action="append", default=[], metavar="PATH",
                   help="put this file of RUN back as it was at the start (removed if the agent added it), from the "
                        "before-state recorded outside every sandbox; with --yes; repeatable")
    p.add_argument("--yes", action="store_true", help="restore without asking (a terminal asks; a script must say so)")
    p.add_argument("--json", action="store_true", help="as JSON")
    return p


def confirm(paths, out):
    """The human's yes, on a terminal; never without one (a script gives --yes)."""
    if not sys.stdin.isatty():
        return False
    out(f"Restore {', '.join(printable(path) for path in paths)} as at the start of the run? What is there now is "
        "replaced (an added file is removed).")
    try:
        return input("Restore? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv, out=print):
    a = parser().parse_args(argv)
    if a.run_id and a.run and a.run_id != a.run:
        print("sandbox-ai workspace review: RUN and --run differ", file=sys.stderr)
        return 2
    run = a.run_id or a.run
    log_dir = Path(a.log_dir).expanduser() if a.log_dir else default_log_dir(Path(a.project).expanduser().resolve())
    try:
        if a.keep or a.restore:
            if not run:
                raise ValueError("--keep and --restore need the run (sandbox-ai workspace review lists the runs)")
            if a.restore and not a.yes and not confirm(a.restore, out):
                raise ValueError("nothing restored: confirm on a terminal, or give --yes")
            done, failed = [], []
            for decision, values in (("kept", a.keep), ("restored", a.restore)):
                for value in values:
                    try:
                        done.append({"path": decide(log_dir, run, value, decision,
                                                    say=(lambda _line: None) if a.json else out),
                                     "decision": decision})
                    except ValueError as exc:
                        failed.append({"path": value, "decision": decision, "error": str(exc)})
                        print(f"sandbox-ai workspace review: {printable(exc)}", file=sys.stderr)
            if a.json:
                out(json.dumps({"done": done, "failed": failed}, ensure_ascii=False))
            return 1 if failed else 0
        if a.all:
            found = [item for item in all_reviews() if run is None or item.get("run") == run]
        else:
            found = [{**item, "log_dir": str(Path(log_dir).resolve())} for item in reviews(log_dir, run)]
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai workspace review: {printable(exc)}", file=sys.stderr)
        return 1
    if a.json:
        out(json.dumps(found, indent=2, ensure_ascii=False))
        return 0
    if not found:
        out(f"No run of {'any project' if a.all else log_dir} changed a file the host runs or trusts.")
        return 0
    for item in found:
        out(f"run {item.get('run')} ({item.get('time')}): {len(item['changes'])} change(s)"
            + (f" - {printable(item['log_dir'])}" if a.all else ""))
        for change in item["changes"]:
            decided = change.get("decision") or {}
            state = (f" [{decided['decision']} {decided.get('time', '')}]" if decided.get("decision") else
                     "" if change.get("restorable") else " [no before-state: keep only]")
            out(f"  {change['change']} {printable(change['sandbox'])}/{printable(change['path'])}: "
                f"{printable(change['why'])}{state}")
            for line in change.get("diff") or []:
                out(f"    {printable(line)}")
            for line in change.get("findings") or []:
                out(f"    hidden-text finding: {printable(line)}")
    out("Nothing is reverted on its own. Per file: sandbox-ai workspace review RUN --keep PATH, or --restore PATH "
        "--yes (as it was at the start; or the start's checkpoint: sandbox-ai checkpoint restore NAME)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
