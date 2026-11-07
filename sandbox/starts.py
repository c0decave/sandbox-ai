#!/usr/bin/env python3
"""sandbox-ai list|rename|attach|pause|resume|stop|delete: every start of an agent in one registry, by name.

  sandbox-ai list [--all] [--json]       what runs and what is kept (alias: sandbox-ai ps)
  sandbox-ai list --history NAME         what happened to NAME: start, exit, pause, resume, rebirth, rename
  sandbox-ai rename OLD NEW              another name for a start
  sandbox-ai attach NAME                 this terminal into a running start (detach: Ctrl-b d)
  sandbox-ai pause|resume NAME           freeze every process of a start, and thaw it again
  sandbox-ai resume NAME                 a stopped kept start: reborn with its HOME and options, the agent
                                         continues its last conversation (running processes are not kept)
  sandbox-ai resume NAME --review-home   what changed in its HOME's start files since its last start
  sandbox-ai stop NAME [--wait SECONDS]  end a start (or an MCP server sandbox); what it keeps stays
  sandbox-ai delete NAME --yes           remove a stopped start with its kept HOME
  sandbox-ai gc [--older-than DAYS] [--dry-run] [--json]
                                         remove leftovers; with DAYS the kept starts unused for that long

Every interactive start (a terminal, the agent's own interface) runs as a detachable session in tmux: closing
the terminal or Ctrl-b d detaches, the agent works on, and attach brings it back. Starts that only run a task
(-p, codex exec, no terminal, --no-tmux, --probe) stay in the foreground; they are listed while they run, can
be paused and stopped, and have a history.

A start keeps its HOME, options and logs by default (--no-keep or the project file's "keep": false discard
the HOME at the end; the administrator's policy forbid_keep discards every HOME). Kept starts never expire on
their own: gc removes them on request, or every new start does with "keep_days" in
~/.config/sandbox-ai/defaults.json.

One table for what runs and what is kept: the starts of agents (detachable sessions included), and the MCP
server sandboxes. By default: what runs or is paused, and the starts whose HOME is kept; --all adds stopped
MCP servers. --json writes the same as a list of objects, for scripts and the desktop app; "attachable" says
whether attach can bring a start's terminal here, and "attach_why" why not (mcp, damaged, stopped, foreground,
paused; null when it can).

Every start has a name: --name NAME, or AGENT-PROJECT-xxxx (PROJECT: --project-name, the project file's
"name", else the main workspace's folder name). A name is 1-40 letters, digits, '_', '.' or '-' and unique
among what runs and what is kept. rename changes the name only: the run's id (its sockets, logs and
approvals) stays, and every command takes the name or the id. The history of each name is a small JSON
lines file in $XDG_STATE_HOME/sandbox-ai/history/.

Sources: the run registry (run.json of every running launcher, see net.py), the start directories
($XDG_STATE_HOME/sandbox-ai/sessions/ID/session.json) and the MCP server state
($XDG_STATE_HOME/sandbox-ai/mcp/NAME/mcp.json). Names, workspaces and everything else read from them are
passed through printable() before they are shown.

Standard library only.
"""
import argparse
import calendar
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
STATE_ORDER = {"running": 0, "paused": 1, "stopped": 2, "ended": 3, "damaged": 4}
HISTORY_LIMIT = 256 << 10  # bytes of one name's history; beyond, the oldest entries go
HISTORY_KEEP = 400         # entries kept when a history is trimmed
_modules = {}


def module(name):
    """sandbox-ai's module NAME (agent_sandbox, sessions, net, mcp, textout), loaded once by path (the
    commands run with python -I: the script directory is not on sys.path)."""
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_starts", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        if name != "agent_sandbox" and hasattr(loaded, "_launcher"):
            loaded._launcher = module("agent_sandbox")  # one launcher for all of them
        _modules[name] = loaded
    return _modules[name]


def launcher():
    return module("agent_sandbox")


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(launcher().SESSION_NAME, name) is not None


def check_name(name, option="name"):
    if not valid_name(name):
        raise ValueError(f"{option}: 1-40 characters from letters, digits, '_', '.', '-', starting with a letter or "
                         f"digit: {launcher().printable(str(name))!r}")
    if name.startswith("mcp-"):
        raise ValueError(f"{option}: names starting with mcp- belong to the MCP server sandboxes")


def sanitise(value):
    """VALUE (a folder name) made fit for a name: other characters become '-', at most 40 of them."""
    result = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value))).strip("-._")
    return result[:40].rstrip("-._") or "work"


def auto_name(agent, project, taken=None, token=secrets.token_hex):
    """AGENT-PROJECT-xxxx, not in use (TAKEN, default: names_in_use()); PROJECT is shortened to fit 40."""
    taken = names_in_use() if taken is None else taken
    project = sanitise(project)[:max(1, 40 - len(agent) - 6)].rstrip("-._") or "w"
    for _ in range(100):
        name = f"{agent}-{project}-{token(2)}"
        if name not in taken:
            return name
    raise ValueError("no free automatic name; choose one with --name")


def names_in_use(found=None):
    """The names and ids of what runs and what is kept (not of ended starts)."""
    found = entries() if found is None else found
    return {value for entry in found for value in (entry.get("name"), entry.get("id"), entry.get("run")) if value}


def check_free(name, found=None, own=None):
    """Refuse NAME when another start (not OWN, an entry) runs or is kept under it, or has it as its id."""
    found = entries() if found is None else found
    for entry in found:
        if entry is own or (own is not None and entry.get("id") == own.get("id")):
            continue
        if name in (entry.get("name"), entry.get("id"), entry.get("run")):
            shown = launcher().printable(name)
            if entry["state"] == "stopped":
                raise ValueError(f"The name {shown} belongs to a kept start (stopped); continue it with: sandbox-ai "
                                 f"resume {shown}, or remove it: sandbox-ai delete {shown}; or choose another name")
            raise ValueError(f"The name {shown} is in use ({entry['state']} {entry['kind']}); choose another with "
                             "--name, or see: sandbox-ai list")


def history_dir():
    return state_root() / "history"


def history_path(name):
    if not valid_name(name):
        raise ValueError(f"no history for {launcher().printable(str(name))!r}: not a name")
    return history_dir() / f"{name}.jsonl"


def record(name, event, ident, run=None, **details):
    """Append EVENT (start, exit, pause, resume, stop, rebirth, rename) of the start IDENT (its start directory or
    run id) to NAME's history; True if written. Never raises: a start does not fail over its history."""
    try:
        path = history_path(name)
        entry = {"time": now(), "event": event, "name": name, "id": ident, "run": run,
                 **{key: value for key, value in details.items()
                    if value is not None or (event, key) == ("exit", "status")}}  # an exit without a status: unknown
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            os.write(fd, (json.dumps(entry) + "\n").encode())
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > HISTORY_LIMIT:
            write_history(name, history(name)[-HISTORY_KEEP:])
        return True
    except (OSError, ValueError, TypeError):
        return False


def history(name):
    """The entries of NAME's history, oldest first ([] without one); damaged lines are skipped."""
    path = history_path(name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return []
        data = stream.read(HISTORY_LIMIT * 2)
    found = []
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("event"), str):
            found.append(entry)
    return found


def write_history(name, found):
    """Replace NAME's history with FOUND (entries); an empty one removes the file."""
    path = history_path(name)
    if not found:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".history-", dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        stream.writelines(json.dumps(entry) + "\n" for entry in found)
    os.replace(temporary, path)


def move_history(old, new, ident):
    """Rename: the entries of the start IDENT go from OLD's history to NEW's (merged in time order); OLD's other
    entries (earlier starts of that name) stay. Both note the rename."""
    if valid_name(old):
        moving = [entry for entry in history(old) if entry.get("id") == ident]
        write_history(old, [entry for entry in history(old) if entry.get("id") != ident])
        if moving:
            write_history(new, sorted(history(new) + moving, key=lambda entry: str(entry.get("time"))))
        record(old, "rename", ident, to=new)
    record(new, "rename", ident, **{"from": old})


def run_event(control_dir, event, status=None):
    """EVENT of the run whose control directory is CONTROL_DIR (its run.json says name, start and run), in its
    name's history: the launcher's start and exit. An exit is noted once per run."""
    data = read_json(Path(control_dir) / "run.json")
    if data is None or not valid_name(data.get("name")):
        return False
    run, ident = data.get("run"), data.get("start") or data.get("run")
    if event == "exit" and any(entry.get("run") == run and entry.get("event") == "exit"
                               for entry in history(data["name"])[-20:]):
        return False
    details = {"agent": data.get("agent"), "workspace": data.get("workspace")} if event == "start" else {}
    if event == "exit":
        details["status"] = status
        if data.get("start"):
            write_exit(data["start"], run, status)
    return record(data["name"], event, ident, run, **details)


def write_exit(key, run, status):
    """exit.json in the directory of the start KEY: which run ended when, with which status (the start's last use
    and what sandbox-ai list shows of it)."""
    try:
        directory = module("sessions").session_dir(key)
    except ValueError:
        return
    if not directory.is_dir() or directory.is_symlink():
        return
    fd, temporary = tempfile.mkstemp(prefix=".exit-", dir=directory)
    with os.fdopen(fd, "w") as stream:
        json.dump({"run": run, "status": status, "ended": now()}, stream)
    os.replace(temporary, directory / "exit.json")


def rewrite_json(path, changes):
    """Set CHANGES in the JSON object in PATH, replacing the file as a whole (readers never see half of it)."""
    data = read_json(path)
    if data is None:
        raise ValueError(f"cannot read {path}")
    data.update(changes)
    fd, temporary = tempfile.mkstemp(prefix=".rewrite-", dir=Path(path).parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
    os.replace(temporary, path)


def keep_days():
    """"keep_days" of your defaults file (~/.config/sandbox-ai/defaults.json), or None: kept starts unused for
    longer expire. The launcher checks the file at every start; here a broken value only means no expiry."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"
    data = read_json(path)
    days = data.get("keep_days") if data else None
    return days if isinstance(days, int) and not isinstance(days, bool) and days >= 1 else None


def seconds(value):
    """An ISO time of a record (UTC) as seconds since the epoch, or None."""
    try:
        return calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def text(value):
    """VALUE as a string for a JSON field, or None (records are read from disk: any type may be there)."""
    return value if isinstance(value, str) else None


def read_json(path):
    """The JSON object in PATH, or None (missing, unreadable, not an object)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mode_of(record_):
    """The security mode a running sandbox records in its run.json (modes.py), or None."""
    mode = record_.get("mode") if isinstance(record_, dict) else None
    return text(mode.get("name")) if isinstance(mode, dict) else None


def start_entries(prune=True):
    """{id: entry} of the start directories: detachable sessions and kept starts, in every state. PRUNE: remove
    the ended ones that kept no HOME on the way (only their history is left)."""
    sessions = module("sessions")
    days = keep_days()
    found = {}
    try:
        directories = sorted(path for path in sessions.root().iterdir() if path.is_dir() and not path.is_symlink())
    except OSError:
        return found
    for directory in directories:
        key = directory.name
        if not (directory / "session.json").exists() or not re.fullmatch(launcher().START_ID, key):
            continue
        if prune and sessions.prune(key):
            continue  # ended without a kept HOME: only its history is left
        data = read_json(directory / "session.json")
        if data is None or not isinstance(data.get("agent"), str) or not isinstance(data.get("options"), list):
            found[key] = {"name": key, "id": key, "kind": "session", "agent": None, "state": "damaged",
                          "home": None, "attachable": False, "directory": str(directory)}
            continue
        try:
            state = sessions.status(key, data)
        except (OSError, ValueError, TypeError):
            state = "damaged"
        ended = read_json(directory / "exit.json") or {}
        keep = sessions.keeps_home(data)
        used = max(value for value in (text(data.get("created")), text(data.get("started")), text(ended.get("ended")),
                                       "") if value is not None)
        expires = iso(seconds(used) + days * 86400) if days and keep and seconds(used) else None
        status = ended.get("status")
        found[key] = {"name": sessions.display(key, data), "id": key,
                      "kind": "run" if sessions.foreground(data) else "session", "agent": data["agent"],
                      "backend": text(data.get("backend")), "state": state,
                      "home": "kept" if keep else "discarded", "keep": keep,
                      "attachable": state == "running" and not sessions.foreground(data),
                      "run": text(data.get("run")) if state != "stopped" or sessions.foreground(data) else None,
                      "pid": data.get("pid") if state != "stopped" else None,
                      "started": text(data.get("started")) or text(data.get("created")),
                      "created": text(data.get("created")), "ended": text(ended.get("ended")),
                      "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                      "last_used": used or None, "expires": expires,
                      "expired": bool(expires and state == "stopped" and expires < now()),
                      "workspace": text(data.get("workspace")), "project": text(data.get("project")),
                      "directory": str(directory)}
    return found


def mcp_entries(include_stopped=False):
    """{name: entry} of the MCP server sandboxes that ran here (their state directories)."""
    mcp = module("mcp")
    found = {}
    root = state_root() / "mcp"
    try:
        directories = sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    except OSError:
        return found
    for directory in directories:
        name = directory.name
        data = read_json(directory / "mcp.json")
        if data is None:
            continue
        try:
            state = mcp.status(name)
        except (OSError, ValueError, TypeError):
            continue
        if state != "running" and not include_stopped:
            continue
        found[name] = {"name": f"mcp-{name}", "id": f"mcp-{name}", "kind": "mcp", "agent": None,
                       "backend": text(data.get("backend")), "state": state, "home": None, "attachable": False,
                       "run": None, "pid": data.get("pid") if state == "running" else None,
                       "started": text(data.get("started")), "workspace": None, "directory": str(directory)}
    return found


def attach_why(entry):
    """Why the start ENTRY has no terminal to attach to, in one word for programs (None: it has one, attachable):
    "mcp" (an MCP server sandbox), "damaged", "stopped" (nothing runs), "foreground" (it runs without tmux, in
    the foreground of its own terminal or script: a task, no terminal, --no-tmux), "paused" (resume it first).
    The desktop app and the web interface explain each in their own words."""
    if entry.get("attachable"):
        return None
    if entry.get("kind") == "mcp":
        return "mcp"
    if entry.get("state") == "damaged":
        return "damaged"
    if entry.get("state") not in ("running", "paused"):
        return "stopped"
    if entry.get("kind") == "run":
        return "foreground"
    return "paused" if entry.get("state") == "paused" else "foreground"


def entries(include_all=False, prune=True):
    """Every start this registry knows, as a list of entries (dicts, see list --json): the running sandboxes,
    the start directories and the MCP servers; with INCLUDE_ALL also the MCP servers that do not run."""
    starts = start_entries(prune)
    servers = mcp_entries(include_all)
    by_pid = {entry["pid"]: entry for entry in servers.values() if entry["pid"]}
    legacy = {f"session-{key}": entry for key, entry in starts.items()}  # sessions of versions before 0.1.30
    plain = []
    for run, record_ in module("net").running().items():
        session = starts.get(text(record_.get("start")) or "") or legacy.get(run)
        if session is not None:
            session["run"], session["run_dir"], session["mode"] = run, record_.get("directory"), mode_of(record_)
            continue
        server = by_pid.get(record_.get("pid"))
        if server is not None or record_.get("kind") == "mcp" or str(run).startswith("mcp-"):
            if server is not None:
                server["run"], server["workspace"] = run, text(record_.get("workspace"))
            continue
        name = record_.get("name") if valid_name(record_.get("name")) else run
        paused = (Path(str(record_.get("directory"))) / "paused").exists()
        plain.append({"name": name, "id": run, "kind": "run", "agent": text(record_.get("agent")),
                      "backend": text(record_.get("backend")), "state": "paused" if paused else "running",
                      "home": "own" if record_.get("own_home") else "discarded", "keep": False,
                      "mode": mode_of(record_),
                      "attachable": False, "run": run, "pid": record_.get("pid"),
                      "started": text(record_.get("started")), "workspace": text(record_.get("workspace")),
                      "directory": None, "run_dir": record_.get("directory")})
    found = list(starts.values()) + plain + list(servers.values())
    for entry in found:
        entry["attach_why"] = attach_why(entry)
    return sorted(found, key=lambda e: (STATE_ORDER.get(e["state"], 9), e.get("started") or "", e["name"]))


def find(name, found=None):
    """The entry NAME stands for: a start's name, else its id or the id of its run."""
    found = entries(True) if found is None else found
    shown = launcher().printable(str(name))
    for key in ("name", "id", "run"):
        matches = [entry for entry in found if entry.get(key) == name]
        if len(matches) > 1:
            raise ValueError(f"several starts are named {shown}; name one by its run id: "
                             + ", ".join(launcher().printable(str(e.get("run") or e["id"])) for e in matches))
        if matches:
            return matches[0]
    raise ValueError(f"nothing named {shown} runs or is kept (see: sandbox-ai list)")


def rename(old, new, out=print):
    """Give the start OLD the name NEW: its start directory and its running run's record say so, and its
    history moves along. The run id stays (sockets, logs, approvals)."""
    check_name(new, "rename NEW")
    found = entries(True)
    entry = find(old, found)
    printable = launcher().printable
    if entry["kind"] == "mcp":
        raise ValueError("MCP servers keep the name of their definition")
    if entry["state"] == "damaged":
        raise ValueError(f"{printable(entry['id'])} is damaged (see its session.json); delete it or repair it first")
    if new == entry["name"]:
        out(f"{printable(new)}: already its name")
        return 0
    check_free(new, found, own=entry)
    if entry["directory"] and entry["kind"] != "mcp":
        module("sessions").rename(entry["id"], new)
    if entry.get("run_dir"):
        try:
            rewrite_json(Path(entry["run_dir"]) / "run.json", {"name": new})
        except (OSError, ValueError):
            pass  # ended meanwhile: nothing runs under the old name any more
    move_history(entry["name"], new, entry["id"])
    out(f"{printable(entry['name'])} is now {printable(new)}")
    return 0


def act(command, name, out=print, wait=None, yes=False, review=False, as_json=False, workspaces=(), mounts=(),
        restart=False):
    """attach, pause, resume, stop or delete the start NAME, whatever kind it is. REVIEW (resume): show what
    changed in its kept HOME instead, AS_JSON for programs. WORKSPACES, MOUNTS (resume): added at its start;
    RESTART: a running one is stopped first."""
    printable = launcher().printable
    try:
        entry = find(name)
    except ValueError:
        if command == "resume" and valid_name(name) and history(name):
            raise ValueError(f"{printable(name)} has ended and its HOME was discarded; see: sandbox-ai list "
                             f"--history {printable(name)}") from None
        raise
    shown = printable(entry["name"])
    if entry["kind"] == "mcp":
        if command == "stop":
            return module("mcp").stop(entry["id"][len("mcp-"):])
        raise ValueError(f"{shown} is an MCP server sandbox: it starts with the agents that use it (--mcp) and "
                         f"ends with: sandbox-ai stop {shown}")
    if entry["state"] == "damaged":
        raise ValueError(f"{shown} is damaged (see {entry['directory']}/session.json); remove that directory")
    if entry.get("directory"):  # a session: its start directory
        sessions = module("sessions")
        key = entry["id"]
        if command == "delete":
            return sessions.delete(key, yes)
        if review:
            return sessions.review(key, as_json, out)
        if command == "stop":
            return sessions.stop(key) if wait is None else sessions.stop(key, wait)
        if command == "resume" and (workspaces or mounts or restart):
            if restart and entry["state"] in ("running", "paused"):
                out(f"{shown}: stopping it to start it again")
                sessions.stop(key)
            return sessions.resume(key, list(workspaces), list(mounts))
        return {"attach": sessions.attach, "pause": sessions.pause, "resume": sessions.resume}[command](key)
    if workspaces or mounts or restart:
        raise ValueError(f"{shown} keeps nothing to start again with (a run without a kept start): end it and start "
                         "anew with the workspace")
    record_ = module("net").running().get(entry["run"])
    if record_ is None:
        raise ValueError(f"{shown} has ended meanwhile")
    if review:
        raise ValueError(f"{shown} keeps no HOME: there is nothing to review")
    if command == "attach":
        raise ValueError(f"{shown} runs without tmux (a task, no terminal, or --no-tmux): there is no terminal "
                         f"to attach to; it ends with its task, or with: sandbox-ai stop {shown}")
    if command == "delete":
        raise ValueError(f"{shown} keeps nothing to delete; its HOME goes when it ends (sandbox-ai stop {shown})")
    if command == "stop":
        if (Path(record_["directory"]) / "paused").exists():
            pause_run(entry, record_, False)  # frozen processes could not end
        result = module("net").stop(record_, module("net").STOP_WAIT if wait is None else wait, out)
        record(entry["name"], "stop", entry["id"], entry["run"])
        return result
    return pause_run(entry, record_, command == "pause")


def pause_run(entry, record_, freeze):
    """Freeze (FREEZE) or thaw a run that has no start directory: its pod with Podman, the processes below its
    launcher with bwrap (the launcher stays: it may hold a terminal). The launcher is checked first."""
    shown = launcher().printable(entry["name"])
    pid, _ = module("net").launcher_process(record_)
    marker = Path(record_["directory"]) / "paused"
    if freeze == marker.exists():
        print(f"agent-sandbox: {shown} is {'paused' if freeze else 'running'} already", file=sys.stderr)
        return 0
    if record_.get("backend") == "podman":
        pod = record_.get("pod")
        if not isinstance(pod, str) or not re.fullmatch(r"agent-[A-Za-z0-9_.-]{1,80}", pod):
            raise ValueError(f"{shown} was started by an older version that did not record its pod; it cannot be "
                             "paused (stop it, or wait for it)")
        engine = SimpleNamespace(podman_url=text(record_.get("podman_url")))
        subprocess.run(launcher().podman_command(engine, "pod", "pause" if freeze else "unpause", pod), check=True,
                       stdout=subprocess.DEVNULL)
    else:
        module("sessions").signal_tree(pid, signal.SIGSTOP if freeze else signal.SIGCONT, include_root=False)
    if freeze:
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
    record(entry["name"], "pause" if freeze else "resume", entry["id"], entry["run"])
    print(f"agent-sandbox: {shown} {'paused' if freeze else 'resumed'}", file=sys.stderr)
    return 0


def gc(days=None, dry_run=False, expired_only=False):
    """Remove what is no longer needed: sessions that ended without keeping their HOME (leftovers of a supervisor
    that died); with DAYS (else keep_days) also the stopped kept starts unused for more than DAYS days, and the
    histories of names no start uses whose last entry is that old. EXPIRED_ONLY: only those kept starts (what a
    new start does with keep_days). DRY_RUN: say what would go, remove nothing."""
    days = days if days is not None else keep_days()
    # 0 days (--older-than 0) is every stopped kept start, not "no expiry"
    cutoff = time.time() - days * 86400 if days is not None else None
    sessions = module("sessions")
    result = {"days": days, "dry_run": dry_run, "removed": [], "leftovers": [], "histories": [], "damaged": [],
              "skipped": []}
    if not expired_only:
        for key, entry in start_entries(prune=False).items():
            if entry["state"] == "damaged":
                result["damaged"].append(entry["directory"])
            elif not entry["keep"] and entry["state"] == "stopped":
                result["leftovers"].append(entry["name"])
                if not dry_run:
                    sessions.prune(key)
    found = entries(True, prune=not dry_run)
    for entry in found:
        if cutoff is None or entry["kind"] == "mcp" or entry["state"] != "stopped" or not entry.get("keep"):
            continue
        used = seconds(entry.get("last_used"))
        if used is None or used > cutoff:
            continue
        if not dry_run:
            try:
                sessions.delete(entry["id"], yes=True)
            except (OSError, ValueError) as exc:  # busy (another command holds it), or gone meanwhile
                result["skipped"].append({"name": entry["name"], "reason": str(exc)})
                continue
        result["removed"].append({key: entry.get(key) for key in ("name", "id", "agent", "last_used", "workspace")})
    if cutoff is not None and not expired_only:
        removed = {entry["id"] for entry in result["removed"]}
        in_use = names_in_use([entry for entry in found if entry["id"] not in removed])
        try:
            files = sorted(history_dir().glob("*.jsonl"))
        except OSError:
            files = []
        for path in files:
            name = path.name[:-len(".jsonl")]
            if not valid_name(name) or name in in_use or path.is_symlink():
                continue
            newest = max((seconds(entry.get("time")) or 0 for entry in history(name)), default=0)
            if newest < cutoff:
                result["histories"].append(name)
                if not dry_run:
                    path.unlink(missing_ok=True)
    return result


def show_gc(result, out=print, as_json=False):
    if as_json:
        out(json.dumps(result, indent=2))
        return
    printable = launcher().printable
    verb = "would remove" if result["dry_run"] else "removed"
    for name in result["leftovers"]:
        out(f"{verb} the leftovers of {printable(name)} (ended; its HOME was not kept)")
    for entry in result["removed"]:
        out(f"{verb} the kept start {printable(entry['name'])} with its HOME (last used "
            f"{shown_time(entry.get('last_used'))} UTC)")
    if result["histories"]:
        out(f"{verb} the history of {', '.join(printable(name) for name in result['histories'])}")
    for entry in result["skipped"]:
        out(f"left alone: {printable(entry['name'])}: {printable(entry['reason'])}")
    for directory in result["damaged"]:
        out(f"left alone: {printable(directory)} is damaged (no readable session.json); remove it by hand")
    if not (result["leftovers"] or result["removed"] or result["histories"]):
        out("nothing to remove" + ("" if result["days"] is not None else "; kept starts never expire on their own: "
                                   "--older-than DAYS removes those unused for that long"))


def shown_time(value):
    """An ISO time of a record (UTC) as "YYYY-MM-DD HH:MM", or "-"."""
    if not isinstance(value, str) or len(value) < 16:
        return "-"
    return value[:16].replace("T", " ")


def show(found, out=print, as_json=False):
    if as_json:
        out(json.dumps(found, indent=2))
        return
    t = module("textout")
    printable = launcher().printable
    colours = {"running": "ok", "paused": "warn", "damaged": "bad"}
    rows = [[printable(e["name"]), e["kind"], printable(e.get("agent") or "-"),
             t.style(e["state"] + (", expired" if e.get("expired") else ""), colours.get(e["state"], "muted")),
             e.get("home") or "-",
             shown_time(e.get("started")), printable(e.get("workspace") or "-")] for e in found]
    # The workspace last: it is the longest and wraps at the terminal's width.
    t.table(["NAME", "KIND", "AGENT", "STATE", "HOME", "STARTED (UTC)", "WORKSPACE"], rows, out, indent=0,
            empty="nothing runs and no start is kept; start one with: sandbox-ai claude|codex|opencode")


def event_details(entry):
    """What a history entry says besides its event and run, as one line."""
    printable = launcher().printable
    event = entry.get("event")
    if event == "exit":
        return f"status {entry['status']}" if isinstance(entry.get("status"), int) else "status unknown"
    if event == "rename":
        if entry.get("to"):
            return f"renamed to {printable(str(entry['to']))}"
        return f"renamed from {printable(str(entry.get('from')))}"
    if event in ("start", "rebirth"):
        return " ".join(printable(str(entry[key])) for key in ("agent", "workspace") if entry.get(key))
    return printable(str(entry.get("note") or ""))


def show_history(name, out=print, as_json=False):
    """The history of NAME (a name, or a start's id or run id: then the history of its current name)."""
    found = history(name) if valid_name(name) else []
    if not found:
        try:
            entry = find(name)
        except ValueError:
            entry = None
        if entry is not None and entry["name"] != name:
            name, found = entry["name"], history(entry["name"])
    if as_json:
        out(json.dumps(found, indent=2))
        return 0
    t = module("textout")
    printable = launcher().printable
    t.table(["TIME (UTC)", "EVENT", "RUN", "DETAILS"],
            [[shown_time(e.get("time")), printable(str(e.get("event"))), printable(str(e.get("run") or "-")),
              event_details(e)] for e in found], out, indent=0,
            empty=f"no history of {printable(str(name))}")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:4]))
    commands = p.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", aliases=["ps"], help="what runs and what is kept")
    listing.add_argument("-a", "--all", action="store_true", help="also MCP servers that do not run")
    listing.add_argument("-H", "--history", metavar="NAME", help="what happened to NAME instead")
    listing.add_argument("-j", "--json", action="store_true", help="as JSON, for scripts and the desktop app")
    renamed = commands.add_parser("rename", help="another name for a start (its run id stays)")
    renamed.add_argument("old", metavar="OLD")
    renamed.add_argument("new", metavar="NEW")
    collect = commands.add_parser("gc", help="remove leftovers, and kept starts unused for --older-than DAYS")
    collect.add_argument("-o", "--older-than", type=int, metavar="DAYS",
                         help="also the stopped kept starts (with their HOME) unused for more than DAYS days, and old "
                              "histories (default: keep_days of defaults.json, else none)")
    collect.add_argument("-n", "--dry-run", action="store_true", help="say what would go, remove nothing")
    collect.add_argument("-j", "--json", action="store_true", help="as JSON")
    for command, text_ in (("attach", "this terminal into a running start (detach again: Ctrl-b d)"),
                           ("pause", "freeze every process of a start"),
                           ("resume", "thaw a paused start, or bring a stopped one back"),
                           ("stop", "end a start; what it keeps stays"),
                           ("delete", "remove a stopped start with its kept HOME")):
        sub = commands.add_parser(command, help=text_)
        sub.add_argument("name", metavar="NAME")
        if command == "stop":
            sub.add_argument("-w", "--wait", type=float, metavar="SECONDS",
                             help="how long to wait for it to end (default 30)")
        if command == "delete":
            sub.add_argument("-y", "--yes", action="store_true", help="confirm the removal of its HOME")
        if command == "resume":
            sub.add_argument("-r", "--review-home", action="store_true",
                             help="show what changed in its HOME's start files since its last start; start nothing")
            sub.add_argument("-j", "--json", action="store_true", help="with --review-home: as JSON")
            sub.add_argument("-w", "--workspace", action="append", default=[], metavar="DIR",
                             help="start it with this directory added (at /workspaces/<name>; repeatable), kept for "
                                  "its later starts")
            sub.add_argument("-m", "--mount", action="append", default=[], metavar="SOURCE:DESTINATION[:ro|rw]",
                             help="start it with this mount added (repeatable), kept for its later starts")
            sub.add_argument("--restart", action="store_true",
                             help="a running start is stopped first (the agent continues its conversation)")
    return p


def main(argv, out=print):
    args = parser().parse_args(argv)
    try:
        if args.command == "rename":
            return rename(args.old, args.new, out)
        if args.command == "gc":
            if args.older_than is not None and args.older_than < 0:
                raise ValueError("--older-than DAYS: 0 or more")
            show_gc(gc(args.older_than, args.dry_run), out, args.json)
            return 0
        if args.command in ("attach", "pause", "resume", "stop", "delete"):
            if getattr(args, "json", False) and not getattr(args, "review_home", False):
                raise ValueError("--json goes with --review-home")
            if getattr(args, "review_home", False) and (getattr(args, "workspace", None) or getattr(args, "mount", None)
                                                         or getattr(args, "restart", False)):
                raise ValueError("--review-home starts nothing: it goes without --workspace, --mount and --restart")
            return act(args.command, args.name, out, wait=getattr(args, "wait", None), yes=getattr(args, "yes", False),
                       review=getattr(args, "review_home", False), as_json=getattr(args, "json", False),
                       workspaces=getattr(args, "workspace", []), mounts=getattr(args, "mount", []),
                       restart=getattr(args, "restart", False))
        if args.history:
            return show_history(args.history, out, args.json)
        show(entries(args.all), out, args.json)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"sandbox-ai {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
