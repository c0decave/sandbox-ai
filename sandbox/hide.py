#!/usr/bin/env python3
"""--hide GLOB: files of the workspaces the agent never sees, such as .env (README "Hiding files from the agent").

At the start every workspace is searched for what the globs match; each match gets a mount over it in the sandbox:
an empty read-only file over a file, an empty read-only folder over a folder (Podman: a bind of an empty file or
folder of the run's session folder; Bubblewrap the same, --ro-bind). The agent reads an empty file where the secret
was, cannot write it, and the host's file is untouched.

A directory added during the run (`sandbox-ai workspace add`) gets the same: the run records its globs (run.json
"hide") and the add hides what they match there before the sandbox sees it (workspaces.py).

A GLOB without "/" matches a name anywhere (.env, *.pem, id_*); one with "/" a path from the workspace's top
(config/secrets/*.json, **/.aws), "**" across folders. Limits, said at the start: a file made after the start is not
hidden; a link is never followed, so a matching symlink is not hidden (named); a hidden file Git tracks is still in
the repository's objects (git show HEAD:.env) - named too; the agent's tests see the empty file.

SUGGESTED: the globs the wizard and the Board offer. Standard library only.
"""
import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

SUGGESTED = (".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519", "id_ecdsa", "credentials*.json", ".npmrc",
             ".pypirc", ".netrc", ".git-credentials")
ENTRIES_LIMIT = 200000   # entries of one workspace looked at
HIDDEN_LIMIT = 500       # matches hidden per start (each is a mount)
SKIPPED = {".git", "node_modules"}  # folders not searched (a glob that names them still hides them)
GLOB = re.compile(r"[^\x00-\x1f\x7f]{1,200}")


def check_glob(value):
    """VALUE as a --hide glob, or ValueError."""
    if not isinstance(value, str) or not GLOB.fullmatch(value) or value.startswith("/") or value.strip() != value:
        raise ValueError(f"--hide {value!r}: a glob of 1-200 printable characters, relative to a workspace (.env, "
                         "*.pem, config/*.json)")
    parts = PurePosixPath(value).parts
    if any(part in ("..", ".") for part in parts) or value.endswith("/"):
        raise ValueError(f"--hide {value!r}: no '.', '..' or trailing '/' in it")
    if value in ("*", "**"):
        raise ValueError(f"--hide {value!r} would hide everything: name the files")
    return value


def translate(glob):
    """GLOB with "/" as a regex over a relative path: "**" across folders, "*" and "?" within one."""
    out, index = [], 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def matcher(globs):
    """MATCHES(relative path) for GLOBS."""
    names = [glob for glob in globs if "/" not in glob]
    paths = [translate(glob) for glob in globs if "/" in glob]

    def matches(relative):
        name = relative.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatchcase(name, glob) for glob in names) or any(p.match(relative) for p in paths)
    return matches


def find(workspace, globs):
    """([(relative, "file"|"dir")], notes) of what GLOBS match in WORKSPACE: regular files and real folders (a matching
    folder is hidden whole and not searched); a matching symlink is named, never hidden or followed."""
    workspace = Path(workspace)
    matches = matcher(globs)
    hidden, notes, entries = [], [], 0
    for directory, dirs, files in os.walk(workspace, followlinks=False):
        base = Path(directory)
        keep = []
        for name in sorted(dirs):
            relative = (base / name).relative_to(workspace).as_posix()
            entries += 1
            if (base / name).is_symlink():
                if matches(relative):
                    notes.append(f"{relative} is a symlink: not hidden (a link is never followed)")
                continue
            if matches(relative):
                hidden.append((relative, "dir"))
            elif name not in SKIPPED:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            entries += 1
            relative = (base / name).relative_to(workspace).as_posix()
            if not matches(relative):
                continue
            try:
                info = os.lstat(base / name)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                notes.append(f"{relative} is a symlink: not hidden (a link is never followed)")
            elif stat.S_ISREG(info.st_mode):
                hidden.append((relative, "file"))
        if entries > ENTRIES_LIMIT:
            notes.append(f"only the first {ENTRIES_LIMIT} entries were searched: hide deeper files by their path "
                         "(--hide sub/dir/.env)")
            break
        if len(hidden) > HIDDEN_LIMIT:
            notes.append(f"more than {HIDDEN_LIMIT} matches: only the first are hidden; name fewer files")
            hidden = hidden[:HIDDEN_LIMIT]
            break
    return hidden, notes


def tracked(workspace, relatives):
    """The RELATIVES Git tracks in WORKSPACE (a repository): still readable from its objects (git show HEAD:PATH).
    Plumbing only, nothing the repository's configuration names is run."""
    if not relatives or not os.path.lexists(Path(workspace) / ".git"):
        return []
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:
        done = subprocess.run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                               "-C", str(workspace), "ls-files", "-z", "--", *relatives], capture_output=True,
                              env=env, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    found = {item for item in done.stdout.decode(errors="replace").split("\0") if item}
    return [relative for relative in relatives if relative in found or any(f.startswith(relative + "/") for f in found)]


def plan(workspaces, globs):
    """For a start: {"hidden": [{"workspace", "sandbox", "path", "kind"}], "notes": [...]} of WORKSPACES
    ([(host path, sandbox path)])."""
    hidden, notes = [], []
    if not globs:
        return {"hidden": hidden, "notes": notes}
    for host, sandbox in workspaces:
        found, more = find(host, globs)
        hidden += [{"workspace": str(host), "sandbox": f"{sandbox}/{relative}", "path": relative, "kind": kind}
                   for relative, kind in found]
        notes += [f"{sandbox}: {note}" for note in more]
        for relative in tracked(host, [relative for relative, _ in found]):
            notes.append(f"{sandbox}/{relative} is hidden but Git tracks it: the agent can still read it from the "
                         f"repository (git show HEAD:{relative}); take it out of the repository's history")
    return {"hidden": hidden, "notes": notes}
