"""Keeps tests away from the real HOME of whoever runs them.

Many tests write logins (~/.claude/.credentials.json, ~/.codex/auth.json, the login store) to test sandbox-ai's
login handling. They rely on fixtures that point HOME at a temporary directory; a runner that skips those fixtures
(another runner, a module run on its own) would write a fake login over the real one, and an agent running these
tests in a sandbox logs itself out ("401 Invalid bearer token"). So HOME is moved before any test runs: by
conftest.py for the whole pytest session (not for --agent-live, whose sandboxes need the real Podman storage), and,
when conftest.py did not, by each module that writes logins, as it is imported. conftest.py also fails the session
if the real login files changed during it.

Standard library only.
"""
import atexit
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

MARKER = "SANDBOX_AI_TEST_HOME"
MOVED = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
DROPPED = ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG")
# Logins of the agents in the real HOME (and where their variables point): compared before and after a session.
# Not ~/.claude.json: Claude Code rewrites that state file all the time while it runs (also the one running the tests).
LOGIN_FILES = (".claude/.credentials.json", ".codex/auth.json", ".local/share/opencode/auth.json",
               ".local/share/sandbox-ai/auth")


def ensure(environ=os.environ):
    """Point HOME and the XDG directories at a fresh temporary directory, unless that was done already (or
    conftest.py handles it: MARKER is set). The temporary directory, or None."""
    if environ.get(MARKER):
        return None
    root = Path(tempfile.mkdtemp(prefix="sandbox-ai-test-home-"))
    atexit.register(shutil.rmtree, root, True)  # /tmp of a sandbox is memory
    for name in MOVED:
        (root / name.lower()).mkdir()
        environ[name] = str(root / name.lower())
    for name in DROPPED:
        environ.pop(name, None)
    no_browser(root, environ)
    environ[MARKER] = str(root)
    return root


BROWSER_COMMANDS = ("xdg-open", "open", "sensible-browser", "x-www-browser", "www-browser", "gio")


def no_browser(root, environ=os.environ):
    """No test ever opens a real browser: in a sandbox $BROWSER and xdg-open hand every URL to the human's desktop as
    a question (a test answering a browser question with Allow asked the human to open https://login.example/...).
    $BROWSER and the usual openers found first on PATH only write what they were given to ROOT/browser-opens."""
    directory = root / "no-browser-bin"
    directory.mkdir(exist_ok=True)
    for name in BROWSER_COMMANDS:
        opener = directory / name
        opener.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{root}/browser-opens"\nexit 0\n')
        opener.chmod(0o755)
    environ["BROWSER"] = str(directory / "xdg-open")
    environ["PATH"] = f"{directory}{os.pathsep}{environ.get('PATH', '')}"
    return directory


def login_files(environ=os.environ):
    """The real login files of the agents, from the environment as it is now (before ensure())."""
    home = Path(environ.get("HOME") or Path.home())
    paths = [home / name for name in LOGIN_FILES]
    for name, relative in (("CLAUDE_CONFIG_DIR", ".credentials.json"), ("CODEX_HOME", "auth.json")):
        if environ.get(name):
            paths.append(Path(environ[name]) / relative)
    return paths


def fingerprint(paths):
    """{path: sha256 of the file, or of the names and contents below a directory, or None}."""
    result = {}
    for path in paths:
        digest = hashlib.sha256()
        try:
            if path.is_dir():
                for child in sorted(p for p in path.rglob("*") if p.is_file()):
                    digest.update(str(child.relative_to(path)).encode() + b"\0" + child.read_bytes())
            else:
                digest.update(path.read_bytes())
            result[str(path)] = digest.hexdigest()
        except OSError:
            result[str(path)] = None
    return result


def changed(before):
    """The paths of BEFORE (fingerprint()) whose contents differ now."""
    now = fingerprint([Path(path) for path in before])
    return sorted(path for path in before if before[path] != now[path])
