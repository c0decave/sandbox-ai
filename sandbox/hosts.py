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
  sandbox-ai hosts update NAME...|--all [--version X.Y.Z] [--sudo] [--jobs N] [--json]
                                                       the host's own `sandbox-ai update` (signed, as install.sh
                                                       installed it): old -> new version per host, a summary
  sandbox-ai hosts run NAME -- ARGS...                 `sandbox-ai ARGS` on that host (e.g. -- list --json)

The desktop app and the web interface (sandbox-ai web) use the same list (~/.config/sandbox-ai/hosts.json) and
reach each host the same way: SSH with your keys, never a password prompt (BatchMode), the host key learnt at the
first contact and checked strictly from then on, each argument quoted on its own. Nothing listens on the network
on the hosts: they only need sshd and sandbox-ai. A non-interactive SSH login reads no ~/.bashrc on Debian, so the
remote command falls back to ~/.local/bin/sandbox-ai (install.sh's place) when sandbox-ai is not on its PATH.

A key file that others can read is refused by ssh ("UNPROTECTED PRIVATE KEY FILE"): add and test make your own
key files (the host's --identity, else ~/.ssh/id_*; regular files you own, never through a symbolic link)
private (0600), say so, and try the connection again; a key folder that others can write to is named, not
changed.

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
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

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
INSTALL_HINT = ("install it there with install.sh (see the README: curl -fsSL https://raw.githubusercontent.com/"
                "c0decave/sandbox-ai/main/install.sh | bash); it updates signed from then on")


def config_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "hosts.json"


def known_hosts_file():
    """sandbox-ai's own known_hosts: the keys of the hosts it manages, apart from yours (~/.ssh/known_hosts is
    read too, for keys you accepted already)."""
    return config_file().with_name("known_hosts")


_launcher = None


def printable(text):
    """TEXT safe for a terminal: the launcher's printable (one implementation for every command)."""
    global _launcher
    if _launcher is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_hosts",
                                                      Path(__file__).resolve().parent / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher.printable(str(text))


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
    if identity is not None and (not isinstance(identity, str) or not os.path.isabs(os.path.expanduser(identity))
                                 or "\n" in identity):
        raise ValueError(f"host {name}: identity is an absolute path (or ~/...)")
    description = entry.get("description", "")
    if not isinstance(description, str) or len(description) > 200 or not description.isprintable():
        raise ValueError(f"host {name}: description is one line of at most 200 characters")
    command = entry.get("command")
    if command is not None and (not isinstance(command, str) or not COMMAND.fullmatch(command)):
        raise ValueError(f"host {name}: command is the path of sandbox-ai there")
    update_sudo = entry.get("update_sudo", False)
    if not isinstance(update_sudo, bool):
        raise ValueError(f"host {name}: update_sudo is true or false")
    return {"ssh": target, "port": port, **({"identity": identity} if identity else {}),
            "description": description, **({"command": command} if command else {}),
            **({"update_sudo": True} if update_sudo else {})}


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
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from None
    hosts = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(hosts, dict):
        raise ValueError(f"{path}: {{\"version\": 1, \"hosts\": {{NAME: {{\"ssh\": \"USER@HOST\"}}}}}}")
    return {name: check(name, entry) for name, entry in hosts.items()}


def save(hosts, path=None):
    path = Path(path or config_file())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
# from every pipe (a master that ssh backgrounds itself keeps the first command's stderr open), and only in
# XDG_RUNTIME_DIR (a socket there is the user's alone); SANDBOX_AI_SSH_MULTIPLEX=0 switches it off.
CONTROL_PERSIST = 60
_masters = {}
_masters_lock = threading.Lock()


def control_path(entry):
    """The control socket for ENTRY's connection, or None (no XDG_RUNTIME_DIR, switched off, an odd path)."""
    if os.environ.get("SANDBOX_AI_SSH_MULTIPLEX", "1") == "0" or not os.environ.get("XDG_RUNTIME_DIR"):
        return None
    base = Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "ssh"
    key = hashlib.sha256(json.dumps([entry.get("ssh"), entry.get("port"), entry.get("identity"),
                                     entry.get("command")]).encode()).hexdigest()[:24]
    path = base / key
    if len(str(path)) > 88 or re.search(r"[\s\"'%]", str(path)):
        return None
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(base)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
    except OSError:
        return None
    return path


def ensure_master(entry, path, runner=subprocess.run):
    """Start the shared connection for ENTRY at PATH unless one answers there; False if it could not be started
    (the command then connects on its own, as without multiplexing)."""
    with _masters_lock:
        lock = _masters.setdefault(str(path), threading.Lock())
    with lock:
        base = ssh_base(entry, control=path)
        check = [base[0], *base[1:-2], "-O", "check", "--", entry["ssh"]]
        try:
            if path.exists() and runner(check, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
                return True
            with contextlib.suppress(FileNotFoundError):
                if path.is_socket():
                    path.unlink()  # a master that ended left it
            plain = ssh_base(entry)
            start = [plain[0], *plain[1:-2], "-o", f"ControlPath={path}", "-o", "ControlMaster=yes",
                     "-o", f"ControlPersist={CONTROL_PERSIST}", "-f", "-N", "--", entry["ssh"]]
            return runner(start, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=CONNECT_TIMEOUT + 15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
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
    return ["ssh", "-tt" if tty else "-T", *options, "--", entry["ssh"]]


SUDO = {"batch": "sudo -n -- ", "prompt": "sudo -- "}


def remote_command(entry, args, sudo=None):
    """The one string ssh hands the remote shell: sandbox-ai with ARGS, each quoted; sandbox-ai from PATH, else
    ~/.local/bin/sandbox-ai (a non-interactive login reads no ~/.bashrc), else the host's own command. SUDO:
    "batch" runs it as `sudo -n` (refused rather than asking for a password), "prompt" as `sudo` (in a terminal,
    which asks); sandbox-ai's path is found as you either way."""
    quoted = " ".join(shlex.quote(str(arg)) for arg in args)
    prefix = SUDO[sudo] if sudo else ""
    if entry.get("command"):
        return f"exec {prefix}{shlex.quote(entry['command'])} {quoted}".rstrip()
    if sudo:
        return (f'if command -v sandbox-ai >/dev/null 2>&1; then exec {prefix}"$(command -v sandbox-ai)" {quoted}; '
                f'else exec {prefix}"$HOME/.local/bin/sandbox-ai" {quoted}; fi')
    return (f'if command -v sandbox-ai >/dev/null 2>&1; then exec sandbox-ai {quoted}; '
            f'else exec "$HOME/.local/bin/sandbox-ai" {quoted}; fi')


def argv(name, args, tty=False, path=None, sudo=None, control=None):
    """The command line that runs `sandbox-ai ARGS` on host NAME (LOCAL: this machine's sandbox-ai); SUDO: see
    remote_command."""
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
    return [*ssh_base(entry, tty, control=None if tty else control), remote_command(entry, args, sudo)]


def run(name, args, input=None, timeout=120, path=None, runner=subprocess.run, sudo=None):
    """(exit status, stdout, stderr) of `sandbox-ai ARGS` on host NAME; 255 when SSH itself failed."""
    control = None
    if name != LOCAL and runner is subprocess.run:  # (tests give a runner of their own: one command each)
        entry = load(path).get(name)
        control = control_path(entry) if entry else None
        if control is not None and not ensure_master(entry, control):
            control = None
    command = argv(name, args, path=path, sudo=sudo, control=control)
    try:
        done = runner(command, input=input, capture_output=True, text=True, timeout=timeout,
                      **({} if input is not None else {"stdin": subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        return 124, "", f"sandbox-ai on {name}: no answer within {timeout} s"
    except OSError as exc:
        return 127, "", f"{command[0]}: {exc.strerror}"
    return done.returncode, done.stdout, done.stderr


def run_json(name, args, timeout=120, path=None, runner=subprocess.run):
    """The JSON a `sandbox-ai ARGS --json` prints on host NAME, or a ValueError with what it said."""
    status, out, err = run(name, args, timeout=timeout, path=path, runner=runner)
    try:
        return json.loads(out)
    except ValueError:
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
    paths = [(path, False) for path in key_candidates(entry)]
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


def run_fixing(name, args, timeout=30, path=None, runner=subprocess.run, sudo=None):
    """run() for host NAME with its key files made private first, and once more when ssh still refuses a key
    file as too open: (status, out, err, fixed, warnings)."""
    entry = None if name == LOCAL else load(path).get(name)
    fixed, warnings = protect_keys(entry) if entry else ([], [])
    status, out, err = run(name, args, timeout=timeout, path=path, runner=runner, sudo=sudo)
    if entry and status != 0 and TOO_OPEN.search(err or ""):
        more, more_warnings = protect_keys(entry, err)
        fixed += more
        warnings += [warning for warning in more_warnings if warning not in warnings]
        if more:
            status, out, err = run(name, args, timeout=timeout, path=path, runner=runner, sudo=sudo)
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
    text = printable((err or out).strip()[-500:]) or f"exit status {status}"
    if status == 255:
        text += " (SSH: is the key in ~/.ssh/authorized_keys there, and the host reachable?)"
    return text


# --- the sandbox-ai of a host: its version, its kind of installation, its update -------------------------

def version_key(version):
    """X.Y.Z of VERSION as numbers for comparing (a suffix does not count), or None."""
    found = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)", str(version or ""))
    return tuple(int(part) for part in found.groups()) if found else None


def installation(text):
    """{"version", "path", "kind", "prefix"} of what `sandbox-ai version --verbose` printed. KIND: "user" (install.sh
    in ~/.local/share/sandbox-ai or XDG_DATA_HOME), "system" (install.sh in PREFIX/lib/sandbox-ai), "package"
    (pyz, AppImage, macOS app), "pip", "checkout", or "unknown" (a version before --verbose: it says only its
    version)."""
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
    if info["kind"] == "unknown" and info["path"]:
        found = re.fullmatch(r"(/.*)/versions/[^/]+", info["path"])
        if found is None:
            info["kind"] = "checkout"
        elif found.group(1).endswith("/lib/sandbox-ai") and not found.group(1).startswith(HOMES):
            info["kind"], info["prefix"] = "system", found.group(1)[:-len("/lib/sandbox-ai")] or "/"
        else:
            info["kind"] = "user"
    return info


def not_updatable(name, info):
    """Why the sandbox-ai of host NAME (INSTALLATION info) is not updated over SSH, with the way to do it, or None."""
    where = printable(info.get("path") or "?")
    if info["kind"] == "checkout":
        return f"{name}: sandbox-ai there is a checkout ({where}): update it with git there (git pull), or {INSTALL_HINT}"
    if info["kind"] == "pip":
        return f"{name}: sandbox-ai there is a pip installation: pip install -U sandbox-ai there, or {INSTALL_HINT}"
    if info["kind"] == "package":
        return (f"{name}: sandbox-ai there runs from a package ({printable(info.get('package'))}): download the new one "
                f"from the release page there, or {INSTALL_HINT}")
    return None


def update_args(info, version=None):
    """`sandbox-ai update ...` for an installation (INSTALLATION info): the same kind of installation as before
    (a user one stays the user's, even for root; a system one keeps its prefix)."""
    if version is not None and not VERSION.fullmatch(version):
        raise ValueError("--version expects X.Y.Z")
    mode = (["--user"] if info["kind"] == "user" else ["--system", "--prefix", info["prefix"]]
            if info["kind"] == "system" else [])
    return ["update", *(["--version", version] if version else []), *mode]


def host_status(name, path=None, runner=subprocess.run):
    """{"name", "version", "kind", "path", "prefix", "available", "update", "error", "fixed", "checked"}: the
    sandbox-ai of host NAME and whether a newer release is there (its own `update --check`: the release it would
    install, checked as it would check it)."""
    result = {"name": name, "version": None, "kind": None, "path": None, "prefix": None, "available": None,
              "update": None, "error": None, "fixed": [], "checked": int(time.time())}
    status, out, err, result["fixed"], _ = run_fixing(name, ["version", "--verbose"], path=path, runner=runner)
    if status != 0:
        result["error"] = failure(status, out, err)
        return result
    info = installation(out)
    result.update({key: info.get(key) for key in ("version", "kind", "path", "prefix")})
    if info["kind"] in ("checkout", "pip"):
        return result
    # As your user `update --check` of a system installation reads none (it would install for you): only the
    # newest release it names counts; the installed version is the one --verbose said.
    status, out, err = run(name, ["update", "--check"], timeout=120, path=path, runner=runner)
    found = re.search(r"^available: (\S+)", out, re.M)
    if found and VERSION.fullmatch(found.group(1)):
        result["available"] = found.group(1)
        if version_key(result["version"]) and version_key(result["available"]):
            result["update"] = version_key(result["available"]) > version_key(result["version"])
    else:
        result["error"] = "update check: " + failure(status, out, err)
    return result


def status_file():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sandbox-ai" / "hosts-status.json"


def load_statuses():
    try:
        fd = os.open(status_file(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            data = json.loads(stream.read(LIMIT).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return {name: value for name, value in data.items() if isinstance(value, dict)} if isinstance(data, dict) else {}


def save_statuses(data):
    path = status_file()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".hosts-status-", dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=1)
        os.replace(temporary, path)
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
    kept = load_statuses()
    if kept.pop(name, None) is not None:
        save_statuses(kept)


def status_text(info):
    """One short column for hosts list: the version, and whether an update is there."""
    if not info:
        return "?"
    version = printable(info.get("version") or "?")
    if not info.get("version"):
        return "unreachable" if info.get("error") else "?"
    if info.get("kind") in ("checkout", "pip", "package"):
        return f"{version} ({info['kind']})"
    if info.get("update"):
        return f"{version}, update: {printable(info.get('available'))}"
    if info.get("update") is False:
        return f"{version}, up to date"
    return f"{version}, update unknown"


def update(name, version=None, sudo=None, path=None, runner=subprocess.run):
    """Update sandbox-ai on host NAME with its own `sandbox-ai update` (install.sh's signed update), as the same
    kind of installation. SUDO (None: the host's update_sudo): as `sudo -n`, which never asks for a password.
    {"name", "before", "after", "changed", "kind", "sudo", "error", "needs_sudo", "needs_password", "output",
    "notes", "fixed"}; "needs_password": sudo wants one (update_terminal asks in a terminal)."""
    entry = {} if name == LOCAL else load(path).get(name)
    if entry is None:
        raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
    result = {"name": name, "before": None, "after": None, "changed": False, "kind": None, "sudo": False,
              "error": None, "needs_sudo": False, "needs_password": False, "output": "", "notes": [], "fixed": []}
    status, out, err, result["fixed"], warnings = run_fixing(name, ["version", "--verbose"], path=path, runner=runner)
    result["notes"] += warnings
    if status != 0:
        result["error"] = failure(status, out, err)
        return result
    info = installation(out)
    result["before"], result["kind"] = info["version"], info["kind"]
    why = not_updatable(name, info)
    if why:
        result["error"] = why
        return result
    args = update_args(info, version)
    use_sudo = bool(entry.get("update_sudo")) if sudo is None else bool(sudo)
    if use_sudo and info["kind"] == "user":
        use_sudo = False
        result["notes"].append("without sudo: the installation there is the user's own (as root it would become "
                               "root's)")
    result["sudo"] = use_sudo
    status, out, err = run(name, args, timeout=UPDATE_TIMEOUT, path=path, runner=runner,
                           sudo="batch" if use_sudo else None)
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
        else:
            result["error"] = failure(status, out, err)
        return result
    return finish_update(name, result, path, runner)


def finish_update(name, result, path=None, runner=subprocess.run):
    """RESULT with the version host NAME has now ("after", "changed"); its kept status is asked anew."""
    status, out, err = run(name, ["--version"], timeout=30, path=path, runner=runner)
    found = VERSION.search(out) if status == 0 else None
    result["after"] = found.group(0) if found else None
    result["changed"] = bool(result["after"]) and result["after"] != result["before"]
    if status != 0:
        result["error"] = "updated, but the version could not be read afterwards: " + failure(status, out, err)
    forget_status(name)
    return result


def update_terminal(name, version=None, path=None, runner=subprocess.run):
    """argv of the update of host NAME in a terminal, where sudo may ask for the password (ssh -tt ... sudo)."""
    status, out, err = run(name, ["version", "--verbose"], timeout=30, path=path, runner=runner)
    if status != 0:
        raise ValueError(failure(status, out, err))
    info = installation(out)
    why = not_updatable(name, info)
    if why:
        raise ValueError(why)
    return argv(name, update_args(info, version), tty=True, path=path,
                sudo="prompt" if info["kind"] != "user" else None)


def update_lines(result):
    """What hosts update prints for one host."""
    name = result["name"]
    lines = [f"  key: {fixed_text(change)}" for change in result.get("fixed") or []]
    if result.get("error") and not result.get("after"):
        lines.insert(0, f"{name}: not updated: {printable(result['error'])}")
    elif result.get("changed"):
        lines.insert(0, f"{name}: {printable(result['before'] or '?')} -> {printable(result['after'])}"
                        + (" (sudo)" if result.get("sudo") else ""))
    else:
        lines.insert(0, f"{name}: {printable(result.get('after') or result.get('before') or '?')}, up to date"
                        + (f" ({printable(result['error'])})" if result.get("error") else ""))
    lines += [f"  note: {printable(note)}" for note in result.get("notes") or []]
    return lines


def update_many(names, version=None, sudo=None, jobs=1, path=None, runner=subprocess.run, done=None):
    """[update result] for the hosts NAMES in their order, JOBS at a time; DONE(result) as each one ends."""
    def one(name):
        try:
            result = update(name, version, sudo, path, runner)
        except (OSError, ValueError) as exc:
            result = {"name": name, "before": None, "after": None, "changed": False, "error": printable(exc),
                      "needs_password": False, "needs_sudo": False, "notes": [], "fixed": []}
        if done:
            done(result)
        return result
    if jobs <= 1 or len(names) <= 1:
        return [one(name) for name in names]
    with concurrent.futures.ThreadPoolExecutor(min(jobs, len(names))) as pool:
        return list(pool.map(one, names))


def summary(results):
    updated = sum(1 for r in results if r.get("changed"))
    failed = sum(1 for r in results if r.get("error") and not r.get("after"))
    return (f"{len(results)} host{'s' if len(results) != 1 else ''}: {updated} updated, "
            f"{len(results) - updated - failed} up to date, {failed} not updated")


def cli(argv_, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai hosts", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the hosts, with their sandbox-ai version and update")
    listing.add_argument("--json", action="store_true")
    asking = listing.add_mutually_exclusive_group()
    asking.add_argument("--cached", action="store_true", help="ask no host: what was found the last time")
    asking.add_argument("--refresh", action="store_true", help="ask every host now")
    adding = commands.add_parser("add", help="add a host reached over SSH")
    adding.add_argument("name")
    adding.add_argument("ssh", metavar="USER@HOST")
    adding.add_argument("--port", type=int, default=22)
    adding.add_argument("--identity", metavar="FILE", help="the SSH key to use (default: your SSH agent's)")
    adding.add_argument("--description", default="")
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
    testing.add_argument("--json", action="store_true")
    updating = commands.add_parser("update", help="update sandbox-ai on hosts (their own signed update)")
    updating.add_argument("names", nargs="*", metavar="NAME")
    updating.add_argument("--all", action="store_true", help="every host of the list (not this machine)")
    updating.add_argument("--version", dest="release", metavar="X.Y.Z", help="this release instead of the newest")
    updating.add_argument("--sudo", action="store_true", default=None,
                          help="as root with sudo -n (a system installation); a password is asked in a terminal")
    updating.add_argument("--no-sudo", dest="sudo", action="store_false", help="without sudo, whatever the host says")
    updating.add_argument("--jobs", type=int, default=1, metavar="N", help="hosts at a time (1-8, default 1)")
    updating.add_argument("--json", action="store_true")
    running = commands.add_parser("run", help="sandbox-ai ARGS on a host")
    running.add_argument("name")
    running.add_argument("args", nargs=argparse.REMAINDER)
    a = parser.parse_args(argv_)
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
    if a.command == "update":
        if a.all == bool(a.names):
            raise ValueError("name the hosts to update, or --all (every host of the list)")
        names = sorted(load()) if a.all else list(dict.fromkeys(a.names))
        if not names:
            out("no hosts to update; add one with: sandbox-ai hosts add NAME USER@HOST")
            return 0
        if not 1 <= a.jobs <= 8:
            raise ValueError("--jobs is 1-8")
        if a.release is not None and not VERSION.fullmatch(a.release):
            raise ValueError("--version expects X.Y.Z")
        hosts = load()
        for name in names:
            if name != LOCAL and name not in hosts:
                raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
        if not a.json:
            out(f"updating {', '.join(names)}" + (f" to {a.release}" if a.release else "") + " ...")
        results = update_many(names, a.release, a.sudo, a.jobs,
                              done=None if a.json else lambda result: [out(line) for line in update_lines(result)]
                              if not (result.get("needs_password") and sys.stdin.isatty()) else None)
        if not a.json and sys.stdin.isatty():
            for result in results:
                if not result.get("needs_password"):
                    continue
                out(f"{result['name']}: sudo there wants a password; asking in this terminal")
                try:
                    status = subprocess.run(update_terminal(result["name"], a.release)).returncode
                except (OSError, ValueError) as exc:
                    result["error"] = printable(exc)
                else:
                    if status == 0:
                        result.update(error=None, needs_password=False)
                        finish_update(result["name"], result)
                    else:
                        result["error"] = f"the update in the terminal ended with exit status {status}"
                for line in update_lines(result):
                    out(line)
        if a.json:
            out(json.dumps(results, indent=2))
        else:
            out(summary(results))
        return 0 if all(not r.get("error") or r.get("after") for r in results) else 1
    hosts = load()
    if a.command == "add":
        if a.name in hosts:
            raise ValueError(f"host {a.name} exists; remove it first")
        entry = check(a.name, {"ssh": a.ssh, "port": a.port, **({"identity": a.identity} if a.identity else {}),
                               "description": a.description,
                               **({"command": a.remote_command} if a.remote_command else {}),
                               **({"update_sudo": True} if a.update_sudo else {})})
        hosts[a.name] = entry
        save(hosts)
        out(f"host {a.name} added")
        fixed, warnings = protect_keys(entry)
        for change in fixed:
            out(f"  key: {fixed_text(change)}")
        for warning in warnings:
            out(f"  note: {warning}")
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
        if a.name not in hosts:
            raise ValueError(f"no host {printable(a.name)}")
        hosts[a.name] = check(a.name, {**hosts[a.name], "update_sudo": a.update_sudo == "on"})
        save(hosts)
        out(f"host {a.name}: its update " + ("runs with sudo" if a.update_sudo == "on" else "runs without sudo"))
        return 0
    if a.command == "remove":
        if a.name not in hosts:
            raise ValueError(f"no host {printable(a.name)}")
        del hosts[a.name]
        save(hosts)
        forget_status(a.name)
        out(f"host {a.name} removed")
        return 0
    if a.command == "test":
        result = test(a.name)
        if a.json:
            out(json.dumps(result, indent=2))
        else:
            for change in result["fixed"]:
                out(f"  key: {fixed_text(change)}")
            for warning in result["warnings"]:
                out(f"  note: {warning}")
            out(f"{a.name}: " + (f"ssh ok, {printable(result['version'])}" if result["ssh"] else "ssh failed")
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
    return subprocess.run(command).returncode


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
