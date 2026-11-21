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
import contextlib
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

MARKER = "SANDBOX_AI_TEST_HOME"
MOVED = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
DROPPED = ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG")
# Logins of the agents in the real HOME (and where their variables point): compared before and after a session.
# Not ~/.claude.json: Claude Code rewrites that state file all the time while it runs (also the one running the tests).
LOGIN_FILES = (".claude/.credentials.json", ".codex/auth.json", ".local/share/opencode/auth.json",
               ".local/share/sandbox-ai/auth")


def remove(root):
    """ROOT and everything in it - also the read-only folders a started .pyz unpacks into ~/.cache/sandbox-ai/pyz
    (0500: rmtree alone left some 70 MB behind per session, in a TMPDIR on a nearly full disk)."""
    def writable(function, path, _info):
        # Made writable, then removed again - never `function(path)`: rmtree's failing call may be os.open or
        # os.scandir, which take more than a path (TypeError on GitHub's runner: rootless Podman's storage below the
        # test HOME has folders 000). What still cannot go (files of Podman's mapped users) stays: a clean-up of a
        # temporary HOME never fails a session whose tests passed.
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(path), 0o700)
        with contextlib.suppress(OSError):
            if os.path.isdir(path) and not os.path.islink(path):
                os.chmod(path, 0o700)
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
    with contextlib.suppress(Exception):
        shutil.rmtree(root, **({"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}))


def ensure(environ=os.environ):
    """Point HOME and the XDG directories at a fresh temporary directory, unless that was done already (or
    conftest.py handles it: MARKER is set). The temporary directory, or None."""
    if environ.get(MARKER):
        return None
    root = Path(tempfile.mkdtemp(prefix="sandbox-ai-test-home-"))
    atexit.register(remove, root)  # /tmp of a sandbox is memory
    for name in MOVED:
        (root / name.lower()).mkdir()
        environ[name] = str(root / name.lower())
    for name in DROPPED:
        environ.pop(name, None)
    no_browser(root, environ)
    no_ssh(root, environ)
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


SSH_COMMANDS = ("ssh", "scp", "sftp")
# A test that starts an SSH server of its own (the Files page against a real sshd) opts in with this variable: ssh,
# scp and sftp are then the real ones. Set it for that test only (monkeypatch.setenv), never for a session.
REAL_SSH = "SANDBOX_AI_TEST_REAL_SSH"
# Destinations that are this machine: ssh to them passes to the real ssh (a test's own sshd on 127.0.0.1).
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
# ssh's options without and with a value (OpenSSH's getopt string); a test never needs the others.
_SSH_FLAGS = "46AaCfGgKkMNnqsTtVvXxYy"
_SSH_VALUED = "BbcDEeFIiJLlmOoPpQRSWw"
# Options that reach past the destination: forwards, a jump host, stdio forwarding, a tunnel device.
_SSH_ONWARD = "DJLRWw"


def no_ssh(root, environ=os.environ):
    """No test ever reaches a real host over SSH: the tests' hosts (192.0.2.x, documentation addresses) were tried
    for real when a test did not fake ssh, and in a sandbox each attempt became a question to the human ("7 Fragen
    zu 192.0.2.11:22 ... ohne Antwort abgelehnt"). The ssh, scp and sftp found first on PATH fail at once with
    ssh's own exit status 255 and write what they were given to ROOT/ssh-calls; a test that fakes ssh puts its own
    first on PATH. They hand over to the real ones only for `ssh -G` (prints the configuration, connects nowhere),
    for ssh to this machine (local_destination(): 127.0.0.1, ::1 or localhost, no jump host, proxy or forward) and
    while $SANDBOX_AI_TEST_REAL_SSH is 1 (a test with an SSH server of its own)."""
    directory = root / "no-ssh-bin"
    directory.mkdir(exist_ok=True)
    for name in SSH_COMMANDS:
        stub = directory / name
        stub.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(str(part)) for part in (
            sys.executable, "-I", Path(__file__).resolve(), "ssh-stub", name, root, directory)) + ' "$@"\n')
        stub.chmod(0o755)
    environ["PATH"] = f"{directory}{os.pathsep}{environ.get('PATH', '')}"
    return directory


def ssh_arguments(args):
    """ssh's command line as OpenSSH's getopt reads it: ([(option, value or None)], destination or None), or None
    for an option ssh does not know (refused, never guessed)."""
    options, destination, index = [], None, 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            if destination is None and index + 1 < len(args):
                destination = args[index + 1]
            break
        if arg.startswith("-") and len(arg) > 1:
            position = 1
            while position < len(arg):
                option = arg[position]
                if option in _SSH_FLAGS:
                    options.append((option, None))
                    position += 1
                elif option in _SSH_VALUED:
                    value = arg[position + 1:]
                    if not value:
                        index += 1
                        if index >= len(args):
                            return None
                        value = args[index]
                    options.append((option, value))
                    break
                else:
                    return None
            index += 1
        elif destination is None:  # options may follow the destination, as with OpenSSH
            destination = arg
            index += 1
        else:
            break  # the remote command
    return options, destination


def local_destination(args, real_ssh, run=None):
    """Whether ssh ARGS reach only this machine: the host ssh would connect to (its configuration applied, read with
    the real ssh's -G) is 127.0.0.1, ::1 or localhost, and nothing goes on from there (no jump host, ProxyCommand,
    forward or tunnel)."""
    parsed = ssh_arguments(args)
    if parsed is None or parsed[1] is None or any(option in _SSH_ONWARD for option, _ in parsed[0]):
        return False
    try:
        result = (run or subprocess.run)([real_ssh, "-G", *args], capture_output=True, text=True, timeout=15,
                                         stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    settings = {}
    for line in result.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        settings.setdefault(key.lower(), value.strip())
    if settings.get("hostname", "").strip("[]").lower() not in LOCAL_HOSTS:
        return False
    if any(settings.get(key, "none").lower() != "none" for key in ("proxyjump", "proxycommand")):
        return False
    if any(key in settings for key in ("localforward", "remoteforward", "dynamicforward")):
        return False
    return settings.get("tunnel", "false").lower() in ("false", "no")


def _real_command(name, directory, environ=os.environ):
    """NAME on PATH without the stubs' DIRECTORY (and earlier stub directories of nested sessions), or None."""
    path = os.pathsep.join(part for part in environ.get("PATH", "").split(os.pathsep)
                           if part and not part.endswith("no-ssh-bin") and part != str(directory))
    return shutil.which(name, path=path)


def ssh_stub(name, root, directory, args, environ=os.environ):
    """The stub of NAME: execs the real command where no_ssh() lets it, otherwise records the call and fails (255)."""
    real = _real_command(name, directory, environ)
    if real:
        parsed = ssh_arguments(args) if name == "ssh" else None
        if (environ.get(REAL_SSH) == "1"
                or (parsed is not None and any(option == "G" for option, _ in parsed[0]))
                or (name == "ssh" and local_destination(args, real))):
            os.execv(real, [real, *args])
    with open(Path(root) / "ssh-calls", "a", encoding="utf-8") as calls:
        calls.write(f"{name} {' '.join(args)}\n")
    print(f"{name}: tests reach no real host", file=sys.stderr)
    return 255


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


if __name__ == "__main__" and sys.argv[1:2] == ["ssh-stub"] and len(sys.argv) >= 5:
    sys.exit(ssh_stub(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]))
