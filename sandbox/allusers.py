"""Every user's starts, for root: `sandbox-ai list --all-users` (or `--user USER`), and `stop`, `logs`, `report` and
`list --history` of another user's start as USER:NAME (or with --user USER).

How a start is identified (README: "How sessions are identified"): each user has a state directory of their own,
~/.local/state/sandbox-ai (sessions/ID/session.json: a kept start, by its start id and name; mcp/NAME/mcp.json: an MCP
server sandbox), a run directory in their runtime directory, /run/user/UID/sandbox-ai/runs/RUN/run.json (a running
launcher, by its run id), and their own rootless Podman, whose pods are named agent-RUN (agent-mcp-NAME) and labelled
sandbox-ai.run=RUN (sandbox-ai.session=ID). Root's own `sandbox-ai list` reads root's directories and root's Podman
only, so a user's starts are not in it.

Root reads another user's state AS DATA ONLY, never through anything the user could plant: every directory from the
user's HOME down is opened relative to the one above it with O_NOFOLLOW (a link is never followed), must be a real
directory owned by that user, and every file must be a regular file of that user (not a link, a FIFO, a device or a
hard link to a file of someone else, such as /etc/shadow), of bounded size, read once. Nothing of the user's is
executed as root: Podman is asked as that user (runuser -u USER, else setpriv or su; on macOS sudo -u) with root's
own podman program and a clean environment, and an action runs root's own copy of sandbox-ai (owned by root, writable
by nobody else, checked first) with root's own Python, after the drop to the user's uid - never the user's installed
copy, never anything from their HOME. What the user's own sandbox-ai then reads and writes is the user's.

A user who is not logged in (no /run/user/UID: no lingering, no session) has no runtime directory and no Podman to
ask: their kept starts are listed from their records, as "stopped" when the end of their last run was recorded and
as "unknown (not logged in)" otherwise.

Seams for the tests (each can only narrow what is read, never widen it): SANDBOX_AI_PASSWD (a passwd file instead of
the system's user database), SANDBOX_AI_USER_RUNTIME (instead of /run/user), SANDBOX_AI_TEST_EUID (a uid other than
0: this process counts as that user, never as root).

Standard library only.
"""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PASSWD_ENV = "SANDBOX_AI_PASSWD"
RUNTIME_ENV = "SANDBOX_AI_USER_RUNTIME"
EUID_ENV = "SANDBOX_AI_TEST_EUID"
USER_RUNTIME = Path("/run/user")
PROC = Path("/proc")
FILE_LIMIT = 1 << 20      # bytes of one record read (session.json, run.json, mcp.json)
DIR_LIMIT = 4096          # entries of one directory looked at
PODMAN_LIMIT = 16 << 20   # bytes of Podman's answer read
PODMAN_TIMEOUT = 20
USER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}\$?")
START_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")           # sessions.START_ID
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")              # liveness.RUN_ID
START_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")         # sessions.SESSION_NAME
MCP_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
POD_NAME = re.compile(r"agent-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")   # liveness.POD_NAME
TARGET = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]{0,31}\$?):([A-Za-z0-9][A-Za-z0-9_.-]{0,80})")
# The commands that take USER:NAME or --user USER (list and ps only with --history: their --user is a filter).
COMMANDS = ("stop", "logs", "report", "list", "ps")
LIVE_STATES = ("running", "paused", "stopping")
STATE_ORDER = {"running": 0, "paused": 1, "orphaned": 1, "stopped": 3, "ended": 4}
UNKNOWN_OFFLINE = "unknown (not logged in)"
UNKNOWN_PODMAN = "unknown (Podman did not answer)"
SAFE_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "COLUMNS", "LINES", "NO_COLOR", "FORCE_COLOR", "TZ")
_modules = {}


def module(name):
    """sandbox-ai's module NAME (textout, procinfo), loaded once by path."""
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_allusers", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def procinfo():
    return module("procinfo")


def printable(text):
    """TEXT safe for a terminal (as the launcher's printable): control, bidi and other non-printable characters
    escaped. Everything shown here was read from a user's files."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def text(value, limit=4096):
    """VALUE when it is a string of at most LIMIT characters, else None (records are the user's: any type)."""
    return value if isinstance(value, str) and len(value) <= limit else None


# --- who may, and who is there ---------------------------------------------------------------------------------------

def effective_uid():
    """This process's effective uid; SANDBOX_AI_TEST_EUID (a uid other than 0) makes a root process count as that
    user - the seam only ever takes root away."""
    real = os.geteuid()
    seam = os.environ.get(EUID_ENV, "")
    if seam.isdigit() and int(seam) != 0:
        return int(seam)
    return real


def require_root(what):
    """ValueError unless this process runs as root: another user's starts are read and acted on by root only."""
    uid = effective_uid()
    if uid != 0:
        try:
            who = pwd.getpwuid(uid).pw_name
        except (KeyError, OverflowError):
            who = f"uid {uid}"
        raise ValueError(f"{what}: only root can see and act on another user's starts (you are {who}); your own: "
                         "sandbox-ai list - another user's: sudo sandbox-ai list --all-users")


def users():
    """[{name, uid, gid, home}] of the user database (SANDBOX_AI_PASSWD: a passwd file instead), each name one that
    is safe to hand to runuser; damaged lines are left out."""
    seam = os.environ.get(PASSWD_ENV)
    found = []
    if seam:
        try:
            lines = Path(seam).read_text(errors="replace").splitlines()[:100000]
        except OSError:
            return []
        entries = []
        for line in lines:
            parts = line.split(":")
            if len(parts) >= 7:
                entries.append((parts[0], parts[2], parts[3], parts[5]))
    else:
        entries = [(p.pw_name, str(p.pw_uid), str(p.pw_gid), p.pw_dir) for p in pwd.getpwall()]
    for name, uid, gid, home in entries:
        if not USER_NAME.fullmatch(name) or not uid.isdigit() or not gid.isdigit() or not home.startswith("/"):
            continue
        found.append({"name": name, "uid": int(uid), "gid": int(gid), "home": home})
    return found


def user_named(name):
    return next((user for user in users() if user["name"] == name), None)


def myself():
    """The name of the user this process runs as (root), or None."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OverflowError):
        return None


# --- reading a user's files as data ----------------------------------------------------------------------------------

def owner_ok(info, uid):
    """INFO (an os.stat result) belongs to UID."""
    return info.st_uid == uid


NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def open_home(user):
    """A descriptor of USER's HOME (the path the user database names, owned by the user), or None."""
    try:
        fd = os.open(user["home"], os.O_RDONLY | DIRECTORY | CLOEXEC)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and owner_ok(info, user["uid"]):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def open_dir(parent, name, uid):
    """The directory NAME in the directory PARENT (a descriptor), never through a link, owned by UID; None else."""
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        return None
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or not owner_ok(info, uid):
            return None
        fd = os.open(name, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=parent)
    except (OSError, ValueError):
        return None
    try:
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino) == (info.st_dev, info.st_ino) and owner_ok(now, uid):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def open_path(parent, parts, uid):
    """The directory PARTS (names, one below the other) below PARENT, each checked as open_dir does; None."""
    fd = os.dup(parent)
    for part in parts:
        below = open_dir(fd, part, uid)
        os.close(fd)
        if below is None:
            return None
        fd = below
    return fd


def read_file(parent, name, uid, limit=FILE_LIMIT):
    """The bytes of the regular file NAME in the directory PARENT, owned by UID, at most LIMIT bytes - None for a
    link, a FIFO, a device, a file of anybody else (a hard link to a root file too) or a bigger one. Checked before it
    is opened (nothing else is ever opened) and again on the open descriptor."""
    if not isinstance(name, str) or not name or "/" in name:
        return None
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not owner_ok(info, uid) or info.st_size > limit:
            return None
        fd = os.open(name, os.O_RDONLY | NOFOLLOW | CLOEXEC | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOCTTY", 0), dir_fd=parent)
    except (OSError, ValueError):
        return None
    try:
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(now.st_mode) or \
                not owner_ok(now, uid) or now.st_size > limit:
            return None
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks) if size <= limit else None
    except OSError:
        return None
    finally:
        os.close(fd)


def read_json(parent, name, uid, limit=FILE_LIMIT):
    """The JSON object in NAME (read_file), or None."""
    raw = read_file(parent, name, uid, limit)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def names_in(fd):
    """The names in the directory FD (the first DIR_LIMIT it gives), sorted."""
    found = []
    try:
        with os.scandir(fd) as listing:
            for entry in listing:
                found.append(entry.name)
                if len(found) >= DIR_LIMIT:
                    break
    except OSError:
        pass
    return sorted(found)


def exists_in(parent, name):
    """NAME is there in PARENT (as anything; never followed)."""
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except (OSError, ValueError):
        return False


def runtime_dir(user):
    """USER's runtime directory (/run/user/UID, SANDBOX_AI_USER_RUNTIME instead) when it is there - a real directory
    owned by the user, so they are logged in or lingering - else None. On macOS there is none: Podman is asked
    anyway (its machine is the user's)."""
    base = Path(os.environ.get(RUNTIME_ENV) or USER_RUNTIME)
    path = base / str(user["uid"])
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return path if stat.S_ISDIR(info.st_mode) and owner_ok(info, user["uid"]) else None


def read_state(user):
    """What USER's state says, read as data: {"sessions": [(id, session.json or None, exit.json or None, paused)],
    "mcp": [(name, mcp.json)], "runs": [(run, run.json, paused)]}; None when the user has no state directory that
    passes the checks."""
    home = open_home(user)
    if home is None:
        return None
    try:
        state = open_path(home, (".local", "state", "sandbox-ai"), user["uid"])
    finally:
        os.close(home)
    if state is None:
        return None
    found = {"sessions": [], "mcp": [], "runs": []}
    try:
        sessions = open_dir(state, "sessions", user["uid"])
        if sessions is not None:
            try:
                for key in names_in(sessions):
                    if not START_ID.fullmatch(key):
                        continue
                    directory = open_dir(sessions, key, user["uid"])
                    if directory is None:
                        continue
                    try:
                        if not exists_in(directory, "session.json"):
                            continue
                        found["sessions"].append((key, read_json(directory, "session.json", user["uid"]),
                                                  read_json(directory, "exit.json", user["uid"], 65536),
                                                  exists_in(directory, "paused")))
                    finally:
                        os.close(directory)
            finally:
                os.close(sessions)
        servers = open_dir(state, "mcp", user["uid"])
        if servers is not None:
            try:
                for name in names_in(servers):
                    directory = open_dir(servers, name, user["uid"]) if MCP_NAME.fullmatch(name) else None
                    if directory is None:
                        continue
                    try:
                        data = read_json(directory, "mcp.json", user["uid"])
                        if data is not None:
                            found["mcp"].append((name, data))
                    finally:
                        os.close(directory)
            finally:
                os.close(servers)
        found["runs"] += read_runs(state, ("runs",), user)
    finally:
        os.close(state)
    runtime = runtime_dir(user)
    if runtime is not None:
        try:
            base = os.open(runtime, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC)
        except OSError:
            base = None
        if base is not None:
            try:
                if owner_ok(os.fstat(base), user["uid"]):
                    seen = {run for run, _, _ in found["runs"]}
                    found["runs"] += [item for item in read_runs(base, ("sandbox-ai", "runs"), user)
                                      if item[0] not in seen]
            finally:
                os.close(base)
    return found


def read_runs(parent, parts, user):
    """[(run, run.json, paused)] of the run directories at PARTS below PARENT."""
    runs = open_path(parent, parts, user["uid"])
    if runs is None:
        return []
    found = []
    try:
        for name in names_in(runs):
            directory = open_dir(runs, name, user["uid"]) if RUN_ID.fullmatch(name) else None
            if directory is None:
                continue
            try:
                record = read_json(directory, "run.json", user["uid"])
                if record is not None:
                    found.append((name, record, exists_in(directory, "paused")))
            finally:
                os.close(directory)
    finally:
        os.close(runs)
    return found


# --- running something as the user ---------------------------------------------------------------------------------

def drop_command(user, argv, which=shutil.which, platform=None):
    """ARGV run as USER, through the first of runuser, setpriv or su there is (macOS: sudo -u); root's own programs
    found on root's PATH. ValueError when there is none."""
    platform = platform or sys.platform
    if platform == "darwin":
        sudo = which("sudo")
        if sudo:
            return [sudo, "-n", "-H", "-u", user["name"], "--", *argv]
        raise ValueError("no sudo to run a command as another user")
    runuser = which("runuser")
    if runuser:
        return [runuser, "-u", user["name"], "--", *argv]
    setpriv = which("setpriv")
    if setpriv:
        return [setpriv, f"--reuid={user['uid']}", f"--regid={user['gid']}", "--init-groups", "--", *argv]
    su = which("su")
    if su:
        return [su, "-s", "/bin/sh", "-c", "exec " + shlex.join(argv), user["name"]]
    raise ValueError("none of runuser, setpriv or su is there to run a command as another user")


def clean_env(user, runtime):
    """The environment of what runs as USER: `env -i` and only this - their HOME, USER, LOGNAME, root's PATH, their
    runtime directory (when they have one) and the terminal's settings."""
    path = ":".join(part for part in (os.environ.get("PATH") or "").split(":") if part.startswith("/"))
    pairs = {"HOME": user["home"], "USER": user["name"], "LOGNAME": user["name"],
             "PATH": path or "/usr/local/bin:/usr/bin:/bin"}
    if runtime is not None:
        pairs["XDG_RUNTIME_DIR"] = str(runtime)
    for key in SAFE_ENV:
        if os.environ.get(key):
            pairs[key] = os.environ[key]
    env = shutil.which("env") or "/usr/bin/env"
    return [env, "-i", *(f"{key}={value}" for key, value in pairs.items())]


def podman_pods(user, runtime, runner=subprocess.run):
    """{pod name: {"status", "live"}} of USER's rootless Podman, asked as USER (root's own podman program, a clean
    environment), or None when it does not answer."""
    podman = shutil.which("podman")
    if not podman:
        return {}
    try:
        command = drop_command(user, [*clean_env(user, runtime), podman, "pod", "ps", "--format", "json"])
        # Its own session: no controlling terminal of root's that a process of the user could push input into.
        done = runner(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=PODMAN_TIMEOUT, check=False,
                      cwd="/", start_new_session=True)
        if done.returncode != 0 or len(done.stdout or b"") > PODMAN_LIMIT:
            return None
        listed = json.loads((done.stdout or b"null").decode("utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired):
        return None
    if not isinstance(listed, list):
        return None
    found = {}
    for pod in listed:
        if not isinstance(pod, dict) or not isinstance(pod.get("Name"), str):
            continue
        infra = pod.get("InfraId")
        containers = pod.get("Containers") if isinstance(pod.get("Containers"), list) else []
        live = [c for c in containers if isinstance(c, dict) and c.get("Id") != infra
                and str(c.get("Status", "")).lower() in LIVE_STATES]
        found[pod["Name"]] = {"status": str(pod.get("Status") or ""), "live": len(live)}
    return found


# --- what each user has --------------------------------------------------------------------------------------------

def process_alive(pid, start, uid):
    """The process PID runs, belongs to UID and (when START was recorded) is still the one that was recorded."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        pass
    if sys.platform != "darwin":
        try:
            if not owner_ok(os.stat(PROC / str(pid)), uid):
                return False
        except OSError:
            return False
    if isinstance(start, int) and not isinstance(start, bool):
        return procinfo().start_time(pid) == start
    return True


def pod_state(pod, pods, runtime_known):
    """What Podman says of POD: "running", "paused", "gone" - or why that is unknown."""
    if not runtime_known:
        return UNKNOWN_OFFLINE
    if pods is None:
        return UNKNOWN_PODMAN
    info = pods.get(pod) if isinstance(pod, str) else None
    if not info or not info["live"]:
        return "gone"
    return "paused" if info["status"].lower() == "paused" else "running"


def user_entries(user, pods_of=None):
    """The entries of USER's starts, as list --json has them with "user" and "uid", read as data (read_state);
    Podman is asked as the user only when a record needs it and the user has a runtime directory."""
    state = read_state(user)
    if state is None:
        return []
    runtime = runtime_dir(user)
    runtime_known = runtime is not None or sys.platform == "darwin"
    needs_podman = any(isinstance(data, dict) and data.get("backend") == "podman"
                       for data in [d for _, d, _, _ in state["sessions"]] + [d for _, d in state["mcp"]]
                       + [d for _, d, _ in state["runs"]])
    pods = None
    if runtime_known and needs_podman:
        pods = (pods_of or podman_pods)(user, runtime)
    base = {"user": user["name"], "uid": user["uid"]}
    found = []
    for key, data, ended, paused in state["sessions"]:
        if not isinstance(data, dict) or not isinstance(data.get("options"), list):
            found.append({**base, "name": key, "id": key, "kind": "session", "agent": None, "backend": None,
                          "state": "damaged", "run": None, "pod": None, "started": None, "workspace": None})
            continue
        name = data.get("name") if isinstance(data.get("name"), str) and START_NAME.fullmatch(data["name"]) else key
        run = text(data.get("run"), 128)
        pod = data.get("pod") if isinstance(data.get("pod"), str) and POD_NAME.fullmatch(data["pod"]) else None
        if process_alive(data.get("pid"), data.get("pid_start"), user["uid"]):
            state_ = "paused" if paused else "running"
        elif isinstance(ended, dict) and (ended.get("run") == run or run is None):
            state_ = "stopped"
        elif data.get("backend") == "podman" and pod:
            seen = pod_state(pod, pods, runtime_known)
            state_ = {"running": "orphaned", "paused": "orphaned", "gone": "stopped"}.get(seen, seen)
        else:  # (Bubblewrap ends with its supervisor, whose process is gone)
            state_ = "stopped"
        found.append({**base, "name": name, "id": key, "kind": "run" if data.get("mode") == "foreground" else "session",
                      "agent": text(data.get("agent"), 64), "backend": text(data.get("backend"), 32), "state": state_,
                      "run": run, "pod": pod, "started": text(data.get("started"), 64) or text(data.get("created"), 64),
                      "workspace": text(data.get("workspace"))})
    known_runs = {entry["run"] for entry in found if entry.get("run")}
    known_starts = {entry["id"] for entry in found}
    for run, record, paused in state["runs"]:
        if record.get("kind") == "mcp" or run.startswith("mcp-") or run in known_runs or \
                text(record.get("start"), 128) in known_starts:
            continue
        pod = record.get("pod") if isinstance(record.get("pod"), str) and POD_NAME.fullmatch(record["pod"]) else \
            f"agent-{run}"
        if process_alive(record.get("pid"), record.get("pid_start"), user["uid"]):
            state_ = "paused" if paused else "running"
        elif record.get("backend") == "podman":
            seen = pod_state(pod, pods, runtime_known)
            state_ = {"running": "orphaned", "paused": "orphaned", "gone": "ended"}.get(seen, seen)
        else:
            state_ = "ended"
        name = record.get("name") if isinstance(record.get("name"), str) and START_NAME.fullmatch(record["name"]) \
            else run
        found.append({**base, "name": name, "id": run, "kind": "run", "agent": text(record.get("agent"), 64),
                      "backend": text(record.get("backend"), 32), "state": state_, "run": run, "pod": pod,
                      "started": text(record.get("started"), 64), "workspace": text(record.get("workspace"))})
    for name, data in state["mcp"]:
        pod = f"agent-mcp-{name}"
        if process_alive(data.get("pid"), data.get("pid_start"), user["uid"]):
            state_ = "running"
        elif data.get("backend") == "podman":
            seen = pod_state(pod, pods, runtime_known)
            state_ = {"gone": "stopped", "paused": "running"}.get(seen, seen)
        else:
            state_ = "stopped"
        found.append({**base, "name": f"mcp-{name}", "id": f"mcp-{name}", "kind": "mcp", "agent": None,
                      "backend": text(data.get("backend"), 32), "state": state_, "run": None, "pod": pod,
                      "started": text(data.get("started"), 64), "workspace": None})
    return found


def own_entries(include_all):
    """Root's own starts (starts.entries), with "user" and "uid"."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_starts_for_allusers", HERE / "starts.py")
    starts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(starts)
    name = myself() or "root"
    return [{"user": name, "uid": os.geteuid(), **entry} for entry in starts.entries(include_all)]


def all_entries(include_all=True, only=None, own=None, pods_of=None):
    """Every user's entries: root's own (OWN: a function, own_entries) and those of each user of the user database
    with a sandbox-ai state directory; ONLY: that user's alone. PODS_OF: instead of asking each user's Podman."""
    me = myself()
    found = []
    if only is None or only == me:
        found += (own or own_entries)(include_all)
    if only == me:
        return found
    my_home = os.path.realpath(os.path.expanduser("~"))
    seen = set()
    for user in users():
        if user["name"] == me or user["name"] in seen or (only is not None and user["name"] != only):
            continue
        seen.add(user["name"])
        if os.path.realpath(user["home"]) == my_home:
            continue
        found += user_entries(user, pods_of)
    return found


def shown_filter(found, stopped=False, mcp=False):
    """(shown, hidden) as plain list filters: by default what runs, is paused, orphaned, damaged or unknown; STOPPED
    adds the stopped and ended starts, MCP the MCP server sandboxes."""
    shown, hidden = [], []
    for entry in found:
        if entry.get("kind") == "mcp" and not str(entry.get("name", "")).startswith("mcp-helper-"):
            keep = mcp
        else:
            keep = stopped or entry.get("state") not in ("stopped", "ended")
        (shown if keep else hidden).append(entry)
    return shown, hidden


def order(entry):
    state = entry.get("state") or ""
    return (str(entry.get("user")), STATE_ORDER.get(state, 2 if state.startswith("unknown") else 5),
            -(module("textout").utc_seconds(entry.get("started")) or 0), str(entry.get("name")))


HEADERS = ["USER", "NAME", "KIND", "AGENT", "STATE", "STARTED", "WORKSPACE"]
STATE_STYLES = {"running": "ok", "paused": "warn", "orphaned": "bad", "damaged": "bad"}


def show(found, out=print, as_json=False, colour=False, trunc=True, hidden=None):
    """The table of FOUND (a USER column first), or JSON."""
    if as_json:
        out(json.dumps(sorted(found, key=order), indent=2))
        return
    t = module("textout")
    rows = [[t.style(printable(e["user"]), "bold", colour), printable(e["name"]), printable(e["kind"]),
             printable(e.get("agent") or "-"),
             t.style(printable(e["state"]), STATE_STYLES.get(e["state"], "warn" if e["state"].startswith("unknown")
                                                             else "muted"), colour),
             t.ago(e.get("started")), printable(t.home_short(e.get("workspace") or "-"))]
            for e in sorted(found, key=order)]
    t.table(HEADERS, rows, out, indent=0, colour=colour, empty="No user has a start that runs here" if hidden else
            "No user has a start here", truncate="left" if trunc else None)
    hidden = hidden or []
    starts = sum(1 for e in hidden if e.get("kind") != "mcp")
    servers = len(hidden) - starts
    parts = ([f"{starts} stopped start{'s' if starts != 1 else ''} (--stopped)"] if starts else []) + \
        ([f"{servers} MCP server{'s' if servers != 1 else ''} (--mcp)"] if servers else [])
    if parts:
        out(t.style(f"Not shown: {', '.join(parts)}; everything: --all", "muted", colour))
    if any(e["state"] == UNKNOWN_OFFLINE for e in found):
        out(t.style("unknown (not logged in): that user has no runtime directory (no login, no lingering), so "
                    "their Podman is not asked; their starts cannot run without one", "muted", colour))
    if found:
        out(t.style("Act on one as that user: sudo sandbox-ai stop|logs|report USER:NAME", "muted", colour))


def list_command(args, out=print, colour=False, own=None):
    """sandbox-ai list --all-users | --user USER [-s] [-m] [-a] [--json] [--no-trunc] (root only; --user with one's own
    name: anybody). OWN: root's own entries (starts.entries)."""
    if args.all_users or args.user != myself() or effective_uid() != os.geteuid():
        require_root("list --all-users" if args.all_users else "list --user")
    if args.user is not None and not USER_NAME.fullmatch(args.user):
        raise ValueError(f"--user: not a user name: {printable(args.user)!r}")
    if args.user is not None and args.user != myself() and user_named(args.user) is None:
        raise ValueError(f"--user {printable(args.user)}: no such user")
    found = all_entries(True, only=args.user, own=own)
    if args.json or args.all:
        show(found, out, args.json, colour, trunc=not args.no_trunc)
        return 0
    shown, hidden = shown_filter(found, stopped=args.stopped, mcp=args.mcp)
    show(shown, out, False, colour, trunc=not args.no_trunc, hidden=hidden)
    return 0


# --- acting as the user --------------------------------------------------------------------------------------------

def copy_problem(here=HERE, owner=0, python=None, top=None):
    """Why root's copy of sandbox-ai at HERE (its sandbox directory) may not be handed to a user to run, or None:
    it, its files, the directories above it and the Python that runs it must be owned by OWNER (root) and writable
    by nobody else (the user could change what root has them run - only their own rights, but never root's copy),
    and the user must be able to reach them (a copy in /root is not). TOP: the directories above HERE only up to it
    (the tests' temporary copy)."""
    here = Path(here)
    checks = [(here, "dir")] + [(path, "file") for path in sorted(here.glob("*.py"))]
    if (here / "__pycache__").is_dir():  # (the compiled modules Python loads instead)
        checks += [(here / "__pycache__", "dir")] + [(path, "file")
                                                     for path in sorted((here / "__pycache__").glob("*.pyc"))]
    parents = [p for p in here.parents if top is None or p == Path(top) or Path(top) in p.parents]
    if python:
        real = Path(os.path.realpath(python))
        checks.append((real, "file"))
        parents += [p for p in real.parents if p not in parents]
    checks += [(path, "parent") for path in parents]
    for path, kind in checks:
        try:
            info = os.stat(path)
        except OSError as exc:
            return f"{path}: {exc.strerror or exc}"
        if info.st_uid != owner:
            return f"{path} is not owned by root"
        sticky = stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not (kind == "parent" and sticky):
            return f"{path} is writable by others than root"
        if stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o001:
            return f"other users cannot reach {path} (mode {stat.filemode(info.st_mode)})"
        if kind == "file" and not info.st_mode & 0o004:
            return f"other users cannot read {path}"
    return None


def own_copy():
    """(Python, main.py) of root's own copy, checked (copy_problem); ValueError with what to do."""
    python = os.path.realpath(sys.executable)
    problem = copy_problem(HERE, 0 if os.geteuid() == 0 else os.geteuid(), python)
    if problem:
        raise ValueError(f"root's own sandbox-ai ({HERE.parent}) cannot be run as another user: {problem}. Install "
                         "it system-wide as root (install.sh --system: /usr/local), so that it is root's and "
                         "readable by every user; never the user's own copy is run")
    return python, str(HERE / "main.py")


def target(command, rest):
    """(the user COMMAND's REST names - by --user USER or USER:NAME - , REST without it) or (None, REST). A word
    USER:NAME counts only when USER is a user here and no file of that name is there. list/ps: only with --history
    (their --user alone is the listing's filter)."""
    if command not in COMMANDS:
        return None, list(rest)
    if command in ("list", "ps") and not any(w in ("-H", "--history") or w.startswith("--history=") for w in rest):
        return None, list(rest)
    named, words, index = [], [], 0
    rest = list(rest)
    while index < len(rest):
        word = rest[index]
        if word == "--user" or word.startswith("--user="):
            if word == "--user":
                if index + 1 >= len(rest) or rest[index + 1].startswith("-"):
                    raise ValueError("--user USER: the user whose start it is")
                value, index = rest[index + 1], index + 2
            else:
                value, index = word.partition("=")[2], index + 1
            if not USER_NAME.fullmatch(value):
                raise ValueError(f"--user: not a user name: {printable(value)!r}")
            named.append(value)
            continue
        head, equals, value = word.partition("=") if word.startswith("--") else ("", "", word)
        found = TARGET.fullmatch(value) if not value.startswith("-") else None
        if found and not os.path.lexists(value) and user_named(found.group(1)) is not None:
            named.append(found.group(1))
            word = head + equals + found.group(2)
        words.append(word)
        index += 1
    users_ = list(dict.fromkeys(named))
    if len(users_) > 1:
        raise ValueError(f"one user at a time: {', '.join(printable(u) for u in users_)}")
    return (users_[0] if users_ else None), words


IN_HOME = 'cd "$HOME" 2>/dev/null || cd /; exec "$@"'  # (run as the user: root never hands it a directory)


def relay(process, stdout, stderr):
    """Copy PROCESS's output and error output to STDOUT and STDERR (streams with .buffer, or byte streams) as it
    comes; on a terminal every control character is shown escaped (printable): what runs as the user may be changed
    by the user, and must not drive root's terminal with escape sequences. Its exit status."""
    import codecs
    import selectors
    targets = {}
    for pipe, stream in ((process.stdout, stdout), (process.stderr, stderr)):
        sink = getattr(stream, "buffer", stream)
        tty = bool(getattr(stream, "isatty", lambda: False)())
        targets[pipe] = (sink, codecs.getincrementaldecoder("utf-8")(errors="replace") if tty else None)
    chooser = selectors.DefaultSelector()
    for pipe in targets:
        os.set_blocking(pipe.fileno(), False)
        chooser.register(pipe, selectors.EVENT_READ)
    try:
        while chooser.get_map():
            for key, _ in chooser.select():
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    chooser.unregister(key.fileobj)
                    continue
                sink, decoder = targets[key.fileobj]
                sink.write(printable(decoder.decode(chunk)).encode("utf-8") if decoder else chunk)
                sink.flush()
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        return 130
    finally:
        chooser.close()


def run_as(name, command, rest, stdout=None, stderr=None):
    """sandbox-ai COMMAND REST as the user NAME: root's own copy (own_copy) with root's Python, dropped to the user
    (drop_command), a clean environment (clean_env), in their HOME (changed into after the drop); in a session of
    its own without stdin (no terminal of root's), its output relayed (relay). Its exit status. A question it would
    ask needs --yes (as without a terminal)."""
    require_root(f"{command} USER:NAME")
    user = user_named(name)
    if user is None:
        raise ValueError(f"{printable(name)}: no such user")
    python, script = own_copy()
    home = open_home(user)
    if home is None:
        raise ValueError(f"{printable(name)}: their HOME ({printable(user['home'])}) is not theirs or not there")
    os.close(home)
    argv = [*clean_env(user, runtime_dir(user)), "NO_COLOR=1", "/bin/sh", "-c", IN_HOME, "sh", python, "-I", script,
            command, *rest]
    process = subprocess.Popen(drop_command(user, argv), cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    return relay(process, stdout or sys.stdout, stderr or sys.stderr)


def redirect(command, rest):
    """main.py's hook: None when COMMAND REST names no other user's start; ("here", REST without the user) when the
    user named is the one who runs this (run here, without a drop); else what running it as that user returned (an
    exit status)."""
    user, words = target(command, rest)
    if user is None:
        return None
    if user == myself() and effective_uid() == os.geteuid():
        return ("here", words)
    return run_as(user, command, words)
