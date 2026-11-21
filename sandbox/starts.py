#!/usr/bin/env python3
"""sandbox-ai list|rename|attach|shell|pause|resume|stop|delete: every start of an agent in one registry, by name.

  sandbox-ai list [--stopped] [--mcp] [--all] [--flat] [--no-color] [--json]
                                         what runs, grouped by start (also: sandbox-ai ps); --stopped, --mcp,
                                         --all: what is kept and the MCP servers too
  sandbox-ai list --history NAME         what happened to NAME: start, exit, pause, resume, rebirth, rename
  sandbox-ai list --folders [--json]     the host folders of the starts: workspaces and HOME of each running
                                         and each kept start (the desktop app's Files page reads it per host)
  sandbox-ai list --all-users | --user USER [--stopped] [--mcp] [--all] [--json]
                                         root: every user's starts (a USER column); stop|logs|report USER:NAME
                                         acts on one as that user (allusers.py)
  sandbox-ai rename OLD NEW              another name for a start
  sandbox-ai rename OLD [--codename]     a fresh codename for it, such as brisk-otter-lantern
  sandbox-ai attach NAME                 this terminal into a running start (detach: Ctrl-b d)
  sandbox-ai shell NAME [-n] [-- COMMAND ...]
                                         a shell of yours in a running start, under the agent's rules (or one
                                         COMMAND, its exit status passed on; -n: without a terminal, for scripts)
  sandbox-ai pause|resume NAME           freeze every process of a start, and thaw it again
  sandbox-ai resume NAME                 a stopped kept start: reborn with its HOME and options, the agent
                                         continues its last conversation (running processes are not kept)
  sandbox-ai resume NAME [--restart] [OPTIONS] [--drop-option OPTION[=VALUE]]
                                         reborn with options changed (--gui, --cpus 4, --allow-host H ...), kept
  sandbox-ai resume NAME --review-home   what changed in its HOME's start files since its last start
  sandbox-ai resume NAME --restore-home [--path PATH] [--yes]
                                         put back what those files held at its last start (small text files)
  sandbox-ai stop NAME [--wait SECONDS]  end a start (or an MCP server sandbox); what it keeps stays
  sandbox-ai delete NAME --yes           remove a stopped start with its kept HOME
  sandbox-ai gc [--older-than DAYS] [--logs-older-than DAYS] [--caches] [--images[=unnamed|old|prune]]
                [--no-compact] [--dry-run] [--json]                 remove leftovers, compress the logs of ended runs; with DAYS the kept
                                         starts unused for that long; --logs-older-than: the log folders not
                                         written for that long; --caches: the download caches in their HOMEs
  sandbox-ai gc --build-output | --toolchains | --transcripts DAYS [--home NAME] [--yes]
                                         also remove that of the stopped kept HOMEs (shown first, asked)
  sandbox-ai gc --pack [--older-than DAYS] [--home NAME] [--yes] | --unpack NAME
                                         pack stopped kept HOMEs into an archive each (unpacked on resume)
  sandbox-ai gc --compact-transcripts DAYS [--home NAME] [--yes]
                                         compress the agents' transcripts older than DAYS in place (storage.py)
  sandbox-ai gc --only [--caches] [--build-output] [--compact-transcripts DAYS] ... [--home NAME]
                                         that and nothing else (no leftovers, expiry, log compression, rules)
  sandbox-ai gc --check [--home NAME] [--dry-run] [--json]
                                         the storage check: the rules applied, a plan of what could be freed
                                         (how much, the command, safe while starts run), each start's growth
  sandbox-ai gc --usage [--no-color] [--json]
                                         how big each part of what sandbox-ai keeps here is, and what gc frees:
                                         a summary table first, then a table per part
  sandbox-ai gc --usage --detail [NAME]  what in the kept HOMEs is big, by kind (caches, toolchains, agent
                                         state, build output, the agent's work); NAME: one HOME in depth

Every interactive start (a terminal, the agent's own interface) runs as a detachable session in tmux: closing
the terminal or Ctrl-b d detaches, the agent works on, and attach brings it back. Starts that only run a task
(-p, codex exec, no terminal, --no-tmux, --probe) stay in the foreground; they are listed while they run, can
be paused and stopped, and have a history.

A start keeps its HOME, options and logs by default (--no-keep or the project file's "keep": false discard
the HOME at the end; the administrator's policy forbid_keep discards every HOME). Without "keep_days" in
~/.config/sandbox-ai/defaults.json the stopped kept starts unused for 90 days are removed by gc and its daily pass
- 14 days after that was first said (gc, gc --usage and the starts say it, and how to keep them) and never one that
runs, is paused or orphaned; "keep_days": N removes them after N days, also at every new start; 0: never.

One table for what runs and what is kept: the starts of agents (detachable sessions included), and the MCP
server sandboxes. By default: what runs, is paused or orphaned (as docker ps); --stopped adds the starts whose HOME
is kept, --mcp the running MCP servers, --all everything (also the MCP servers that do not run). Each start is followed by what belongs to it (its MCP servers and helpers); --flat lists every row on
its own. Colour on a terminal only (--no-color, NO_COLOR). --json writes the same as a list of objects, for
scripts and the desktop app; "attachable" says whether attach can bring a start's terminal here, and "attach_why"
why not (mcp, damaged, stopped, foreground, paused; null when it can); "listed_under" the start a row is shown
below, an MCP server's "serves" the starts that use it (the one it was started for first, "started_for";
"owner_recorded": its mcp.json says whom it was started for), a team member's "lead" its lead's name, a running
session's "app_tab" the desktop app's terminal tab it was started in (SANDBOX_AI_APP_TAB: the app attaches a tab
for it again after a restart).

Every start has a name: --name NAME, or a codename of three words such as brisk-otter-lantern (codenames.py),
or with --name-style project or "name_style": "project" in defaults.json AGENT-PROJECT-xxxx (PROJECT:
--project-name, the project file's "name", else the main workspace's folder name). A name is 1-40 letters,
digits, '_', '.' or '-' and unique among what runs and what is kept. rename changes the name only: the run's id
(its sockets, logs and approvals) stays, and every command takes the name or the id. The history of each name is
a small JSON lines file in $XDG_STATE_HOME/sandbox-ai/history/.

Sources: the run registry (run.json of every running launcher, see net.py), the start directories
($XDG_STATE_HOME/sandbox-ai/sessions/ID/session.json) and the MCP server state
($XDG_STATE_HOME/sandbox-ai/mcp/NAME/mcp.json). Names, workspaces and everything else read from them are
passed through printable() before they are shown.

A start whose launcher (its supervisor on the host) was killed without its cleanup while its Podman pod runs on is
"orphaned": its terminal can still be attached to (a session), but approvals, net allow and workspace add do not
reach it. stop ends its pod and records the end; a kept start is then resumed as any stopped one (liveness.py).

Standard library only.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()
STATE_ORDER = {"running": 0, "paused": 1, "orphaned": 1, "stopped": 2, "ended": 3, "damaged": 4}
HISTORY_LIMIT = 256 << 10  # bytes of one name's history; beyond, the oldest entries go
HISTORY_KEEP = 400         # entries kept when a history is trimmed
_modules = {}


def module(name):
    """sandbox-ai's module NAME (agent_sandbox, sessions, net, mcp, textout), loaded once by path (the
    commands run with python -I: the script directory is not on sys.path). One launcher for all of them, loaded when
    the first needs it: a listing (`sandbox-ai list`, which the app asks every few seconds) loads none."""
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_starts", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        if name == "agent_sandbox":
            for other in _modules.values():  # those loaded before it
                if getattr(other, "_launcher", False) is None:
                    other._launcher = loaded
        elif hasattr(loaded, "_launcher"):
            if "agent_sandbox" in _modules:
                loaded._launcher = _modules["agent_sandbox"]
            elif hasattr(loaded, "_launcher_source"):
                loaded._launcher_source = launcher
            else:
                loaded._launcher = module("agent_sandbox")
        if name != "liveness" and hasattr(loaded, "_liveness"):
            loaded._liveness = module("liveness")  # one Podman listing (cached) for all of them
        _modules[name] = loaded
    return _modules[name]


def launcher():
    return module("agent_sandbox")


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(module("sessions").SESSION_NAME, name) is not None


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


NAME_STYLES = ("codename", "project")  # the names of starts without --name: "name_style" in defaults.json


def default_name(agent, project, style="codename", taken=None):
    """The name of a start without --name: a codename such as brisk-otter-lantern (STYLE "codename", the default;
    codenames.py) not in TAKEN (default: names_taken()), or AGENT-PROJECT-xxxx (STYLE "project", auto_name)."""
    if style == "project":
        return auto_name(agent, project, taken)
    if style != "codename":
        raise ValueError(f"name style: one of {', '.join(NAME_STYLES)}")
    return module("codenames").free_codename(names_taken() if taken is None else taken)


def names_taken(found=None):
    """What a new codename must not be: the names and ids of what runs and what is kept (names_in_use), the
    teams' names (sandbox-ai team) and every name with a history (an ended start's, list --history)."""
    taken = set(names_in_use(found))
    try:
        teams = module("team").teams_dir()
        if teams.is_dir():
            taken.update(path.name for path in teams.iterdir())
    except OSError:
        pass
    try:
        if history_dir().is_dir():
            taken.update(path.name[:-len(".jsonl")] for path in history_dir().iterdir() if path.name.endswith(".jsonl"))
    except OSError:
        pass
    return taken


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
                # Its id or run id (renamed since: `list` shows another name): named as `list` shows it.
                listed = launcher().printable(str(entry.get("name") or name))
                which = "a kept start (stopped" if listed == shown else \
                    f"the kept start {listed} (stopped; {shown} is its run id" if name == entry.get("run") else \
                    f"the kept start {listed} (stopped; {shown} is its id, the name it was started with"
                raise ValueError(f"The name {shown} belongs to {which}); continue it with: sandbox-ai resume "
                                 f"{listed}, or remove it: sandbox-ai delete {listed}; or choose another name")
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
        except (ValueError, RecursionError):
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
    try:
        with os.fdopen(fd, "w") as stream:
            stream.writelines(json.dumps(entry) + "\n" for entry in found)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


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
    current = read_json(directory / "session.json")
    if current is not None and isinstance(current.get("run"), str) and current["run"] != run:
        return  # the end of an earlier run, found late (net.forget_ended): the start runs again since
    fd, temporary = tempfile.mkstemp(prefix=".exit-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"run": run, "status": status, "ended": now()}, stream)
        os.replace(temporary, directory / "exit.json")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def rewrite_json(path, changes):
    """Set CHANGES in the JSON object in PATH, replacing the file as a whole (readers never see half of it)."""
    data = read_json(path)
    if data is None:
        raise ValueError(f"cannot read {path}")
    data.update(changes)
    fd, temporary = tempfile.mkstemp(prefix=".rewrite-", dir=Path(path).parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def keep_days():
    """"keep_days" of your defaults file (~/.config/sandbox-ai/defaults.json), or None: kept starts unused for
    longer expire - at every start too. The launcher checks the file at every start; here a broken value only means
    no expiry. Without it the default expiry applies (expiry_setting)."""
    days, origin = expiry_setting()
    return days if origin == "keep_days" else None


# Without keep_days in your defaults, the stopped kept starts unused for DEFAULT_KEEP_DAYS days are removed by gc and
# the daily background pass (never at a start, never a start that runs, is paused or orphaned) - once it was announced
# (EXPIRY_FILE: when, and which starts it named) and EXPIRY_GRACE days have passed since; keep_days 0 switches it off.
DEFAULT_KEEP_DAYS = 90
EXPIRY_GRACE = 14
EXPIRY_FILE = "expiry.json"
EXPIRY_OFF = "sandbox-ai defaults set keep_days 0"


def expiry_setting():
    """(days, origin) of the kept starts' expiry: (N, "keep_days") with keep_days N in your defaults, (None, "off")
    with keep_days 0, (DEFAULT_KEEP_DAYS, "default") without keep_days, (None, "invalid") for a broken value (the
    launcher refuses it at a start; here it only means no expiry)."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"
    data = read_json(path)
    if data is None:
        if os.path.lexists(path):
            return None, "invalid"  # (unreadable: never a reason to remove anything)
        data = {}
    if "keep_days" not in data:
        return DEFAULT_KEEP_DAYS, "default"
    days = data["keep_days"]
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        return None, "invalid"
    return (days, "keep_days") if days >= 1 else (None, "off")


LOG_DAYS_OFF = "sandbox-ai defaults set log_days 0"


def log_days_setting():
    """(days, origin) of the logs' age limit (statedirs.old_logs): (N, "log_days") with log_days N in your defaults,
    (None, "off") with log_days 0, (statedirs.LOG_DAYS, "default") without it, (None, "invalid") for a broken value
    (the launcher refuses it at a start; here it only means nothing is removed)."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"
    data = read_json(path)
    if data is None:
        if os.path.lexists(path):
            return None, "invalid"
        data = {}
    if "log_days" not in data:
        return module("statedirs").LOG_DAYS, "default"
    days = data["log_days"]
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        return None, "invalid"
    return (days, "log_days") if days >= 1 else (None, "off")


def expire_logs(dry_run=False, now=None, shown=True):
    """The age limit of the logs: the log files nothing was written into for more than log_days days (statedirs.old_logs
    - never the log a running run writes) removed, or only listed (DRY_RUN). Without log_days (the default: 30 days)
    only from LOG_DAYS_GRACE days after it was first said to you: SHOWN - gc, its dry run, gc --usage and the app show
    their result; the daily pass in the background does not (it only records that it found such logs), a start says
    it itself (log_days_notice). state/log-days.json: "noticed" (found first), "said" (shown first). {"days",
    "origin", "off", "found" (count), "bytes", "removed" (paths), "freed", "removed_from" (the default's first day,
    ISO; None while it was not said), "errors"}."""
    statedirs = module("statedirs")
    now = time.time() if now is None else now
    days, origin = log_days_setting()
    result = {"days": days, "origin": origin, "off": LOG_DAYS_OFF, "found": 0, "bytes": 0, "removed": [], "freed": 0,
              "removed_from": None, "errors": []}
    if days is None:
        return result
    try:
        runs = registered_runs()
    except (OSError, ValueError, RuntimeError) as exc:
        result["errors"].append(f"which runs still run could not be told ({exc}): no log was removed")
        return result
    busy = [record["log_dir"] for record in runs.values() if isinstance(record.get("log_dir"), str)]
    found = statedirs.old_logs(None, days, set(runs), busy, now)
    result.update(found=len(found), bytes=sum(item["bytes"] for item in found))
    if origin == "default" and found:
        # Round 1 started the grace in the background (the daily pass): 14 days later the logs went, said to nobody.
        path = state_root() / statedirs.LOG_DAYS_FILE
        notice = read_json(path) or {}
        said = seconds(notice.get("said"))
        if seconds(notice.get("noticed")) is None or (said is None and shown):
            notice.setdefault("noticed", iso(now))
            if shown:
                notice["said"] = iso(now)
            if write_notice(path, notice) and shown:
                said = now
                LOG.info("starts", f"the default age limit of the logs ({days} days) announced; removals from "
                                   f"{iso(now + statedirs.LOG_DAYS_GRACE * 86400)} on")
        if said is None:
            return result  # not said to you yet: nothing goes
        result["removed_from"] = iso(said + statedirs.LOG_DAYS_GRACE * 86400)
        if now < said + statedirs.LOG_DAYS_GRACE * 86400:
            return result
    if dry_run or not found:
        return result
    removed = statedirs.remove_old_logs(found, days, now)
    result.update(removed=removed["removed"], freed=removed["bytes"], errors=removed["errors"])
    return result


def log_days_cutoff(now=None):
    """The time (seconds) before which a log goes by the age limit of the logs now (expire_logs: log_days set, or the
    default once its grace is over), or None (off, invalid, or the default not said or within its grace)."""
    now = time.time() if now is None else now
    days, origin = log_days_setting()
    if days is None:
        return None
    if origin == "default":
        notice = read_json(state_root() / module("statedirs").LOG_DAYS_FILE) or {}
        said = seconds(notice.get("said"))
        if said is None or now < said + module("statedirs").LOG_DAYS_GRACE * 86400:
            return None
    return now - days * 86400


def log_days_notice(when=None):
    """What a start says about the default age limit of the logs (once a day), from when a pass found logs older than
    it until its grace is over: from when gc and its daily pass remove them, and how to keep them - said here first,
    the grace starts now. None otherwise (nothing found, the grace over, log_days set or off)."""
    when = time.time() if when is None else when
    days, origin = log_days_setting()
    if origin != "default":
        return None
    path = state_root() / module("statedirs").LOG_DAYS_FILE
    notice = read_json(path)
    if notice is None or seconds(notice.get("noticed")) is None:
        return None
    grace = module("statedirs").LOG_DAYS_GRACE * 86400
    said, last = seconds(notice.get("said")), seconds(notice.get("last_said"))
    if said is not None and (when >= said + grace or (last is not None and 0 <= when - last < 86400)):
        return None
    if said is None:
        notice["said"] = iso(when)
        said = when
    notice["last_said"] = iso(when)
    if not write_notice(path, notice):
        return None
    return (f"logs older than {days} days (connection logs with their bodies, file, process and LLM traffic logs) are "
            f"removed from {shown_time(iso(said + grace))} UTC on, by sandbox-ai gc and its daily pass (the default; "
            f"no log_days in your defaults). To keep them longer: sandbox-ai defaults set log_days DAYS; never: "
            f"{LOG_DAYS_OFF}")


def seconds(value):
    """An ISO time of a record (UTC) as seconds since the epoch, or None."""
    import calendar
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
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def mode_of(record_):
    """The security mode a running sandbox records in its run.json (modes.py), or None."""
    mode = record_.get("mode") if isinstance(record_, dict) else None
    return text(mode.get("name")) if isinstance(mode, dict) else None


def ask_of(record_):
    """How a running sandbox's questions are answered, from its run.json: "on" (asked), "off" (refused), "allow"
    (--ask allow: every one allowed at once, nobody asked), or None (no proxy, or an older version: not recorded)."""
    ask = record_.get("ask") if isinstance(record_, dict) else None
    return ask if ask in ("on", "off", "allow") else None


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
        if not (directory / "session.json").exists() or not re.fullmatch(sessions.START_ID, key):
            continue
        if prune and sessions.prune(key):
            continue  # ended without a kept HOME: only its history is left
        data = read_json(directory / "session.json")
        if data is None or data.get("agent") not in AGENTS or \
                not isinstance(data.get("options"), list):  # as `session list` judges it
            found[key] = {"name": key, "id": key, "kind": "session", "agent": None, "state": "damaged",
                          "home": None, "attachable": False, "directory": str(directory)}
            continue
        try:
            state = sessions.status(key, data)
        except (OSError, ValueError, TypeError):
            state = "damaged"
        terminal = ("ready", "")
        if state == "running" and hasattr(sessions, "terminal_state"):
            try:
                terminal = sessions.terminal_state(key, data)
            except (OSError, ValueError, TypeError):
                terminal = ("ready", "")
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
                      "attachable": state in ("running", "orphaned") and not sessions.foreground(data)
                      and terminal[0] not in ("missing", "ended"),
                      # (a running session: whether its terminal is there yet - starting - or it runs without one)
                      **({"terminal": terminal[0]} if state == "running" else {}),
                      **({"terminal_log": terminal[1]} if terminal[0] in ("missing", "ended") else {}),
                      **({"terminal_step": terminal[1]} if terminal[0] == "starting" and terminal[1] else {}),
                      "run": text(data.get("run")) if state != "stopped" or sessions.foreground(data) else None,
                      "pid": data.get("pid") if state not in ("stopped", "orphaned") else None,
                      "started": text(data.get("started")) or text(data.get("created")),
                      "created": text(data.get("created")), "ended": text(ended.get("ended")),
                      "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                      "last_used": used or None, "expires": expires,
                      "expired": bool(expires and state == "stopped" and expires < now()),
                      "workspace": text(data.get("workspace")), "project": text(data.get("project")),
                      "mcp": mcp_names(data["options"]), "llm": llm_choice(data["options"]),
                      "directory": str(directory),
                      # the app's terminal tab it was started in (sessions.launch): the app attaches it after a restart
                      "app_tab": text(data.get("app_tab")) if state != "stopped" else None}
    return found


# llm.parse_models: NAME[:MODEL[,MODEL...]]
LLM_CHOICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}(?::[\w.:/@+-]{1,200}(?:,[\w.:/@+-]{1,200}){0,49})?",
                        re.ASCII)


def llm_choice(options):
    """The LLM server OPTIONS (a start's saved options) name as --llm NAME[:MODEL[,MODEL...]] (the last one given), or
    None: the app and the web show that server's status (sandbox-ai llm status NAME) beside the start."""
    found = None
    for index, token in enumerate(options if isinstance(options, list) else []):
        if token in ("--llm", "-lm") and index + 1 < len(options) and isinstance(options[index + 1], str):
            found = options[index + 1]
        elif isinstance(token, str) and token.startswith(("--llm=", "-lm=")):
            found = token.partition("=")[2]
    return found if isinstance(found, str) and LLM_CHOICE.fullmatch(found) else None


def record_llm(record_):
    """The --llm NAME[:MODEL] a running sandbox records in its run.json (None: none, or an older version)."""
    value = record_.get("llm") if isinstance(record_, dict) else None
    return value if isinstance(value, str) and LLM_CHOICE.fullmatch(value) else None


def mcp_names(options):
    """The MCP servers OPTIONS (a start's saved options, as on a command line) name: --mcp NAME, --mcp=NAME, -M NAME."""
    names = []
    for index, token in enumerate(options if isinstance(options, list) else []):
        if token in ("--mcp", "-M") and index + 1 < len(options) and isinstance(options[index + 1], str):
            names.append(options[index + 1])
        elif isinstance(token, str) and token.startswith("--mcp="):
            names.append(token.partition("=")[2])
    return [name for name in dict.fromkeys(names) if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name)]


def record_mcp(record_):
    """The MCP servers a running sandbox records in its run.json (None for a version that did not record them)."""
    value = record_.get("mcp_servers") if isinstance(record_, dict) else None
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else None


def record_services(record_):
    """The services (--service) a running sandbox records in its run.json: [{name, image, reference, ports,
    container, unchecked}], each checked (a damaged record leaves one out)."""
    value = record_.get("services") if isinstance(record_, dict) else None
    found = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or \
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", item["name"]):
            continue
        ports = [p for p in item.get("ports") or [] if type(p) is int and 0 < p < 65536] \
            if isinstance(item.get("ports"), list) else []
        found.append({"name": item["name"], "image": text(item.get("image")), "reference": text(item.get("reference")),
                      "ports": ports, "container": text(item.get("container")), "unchecked": True})
    return found


def record_browser(record_):
    """The sandbox's browser (--browser) a running sandbox records in its run.json: {kind, control, profile, vnc,
    cdp, container}, checked; None without one (or a damaged record)."""
    value = record_.get("browser") if isinstance(record_, dict) else None
    if not isinstance(value, dict) or value.get("kind") not in ("chromium", "firefox") or \
            value.get("control") not in ("human", "agent", "both"):
        return None
    vnc = value.get("vnc")
    vnc = vnc if isinstance(vnc, str) and vnc.startswith("/") and vnc.endswith("/vnc.sock") and len(vnc) < 4096 \
        and "\n" not in vnc else None
    cdp = value.get("cdp") if value.get("cdp") == "http://127.0.0.1:9222" else None
    profile = value.get("profile") if value.get("profile") in ("fresh", "session", "dir") else "fresh"
    return {"kind": value["kind"], "control": value["control"], "profile": profile, "vnc": vnc, "cdp": cdp,
            "container": text(value.get("container")), "tor": value.get("tor") is True}


def services_text(entry):
    """What the listing says of a start's services, or ""."""
    services = entry.get("services") or []
    if not services:
        return ""
    return "services: " + ", ".join(service["name"] + "".join(f":{port}" for port in service["ports"][:3])
                                    for service in services) + " (unchecked images)"


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
                       "started": text(data.get("started")), "workspace": None, "directory": str(directory),
                       "serves": [], "started_for": started_for(data),
                       # Whether its mcp.json says whom it was started for (null there: on its own); not before 0.1.78,
                       # whose servers the app shows below the start of their host started at the same time.
                       "owner_recorded": "started_for" in data}
    return found


def started_for(data):
    """The start an MCP server was started for, as its mcp.json records it ({"start": its start directory's id or
    None, "run": its run id or None}; mcp.py writes it when it starts the server), or None (started on its own, or
    by a version before 0.1.78). Read from disk: anything else is None."""
    value = data.get("started_for") if isinstance(data, dict) else None
    if not isinstance(value, dict):
        return None
    found = {key: value.get(key) if isinstance(value.get(key), str) and value.get(key) else None
             for key in ("start", "run")}
    return found if any(found.values()) else None




def attach_why(entry):
    """Why the start ENTRY has no terminal to attach to, in one word for programs (None: it has one, attachable):
    "mcp" (an MCP server sandbox), "damaged", "stopped" (nothing runs), "foreground" (it runs without tmux, in
    the foreground of its own terminal or script: a task, no terminal, --no-tmux), "paused" (resume it first),
    "no-terminal" (it runs without the terminal its start should have made: "terminal_log" has its log's end),
    "ended" (its tmux session has ended - no agent any more - while its sandbox still runs: "terminal_log" too).
    The desktop app and the web interface explain each in their own words."""
    if entry.get("attachable"):
        return None
    if entry.get("kind") == "mcp":
        return "mcp"
    if entry.get("terminal") == "missing":
        return "no-terminal"  # it runs, without the terminal its start should have made (its log says why)
    if entry.get("terminal") == "ended":
        return "ended"  # its tmux session has ended (the agent and its shell window), its sandbox not (yet)
    if entry.get("state") == "damaged":
        return "damaged"
    if entry.get("state") == "orphaned":
        return "orphaned"
    if entry.get("state") not in ("running", "paused"):
        return "stopped"
    if entry.get("kind") == "run":
        return "foreground"
    return "paused" if entry.get("state") == "paused" else "foreground"


POD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")  # the launcher's AUTH_TARGETS (a test keeps both the same)
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")  # team.py's TEAM_NAME
VPN_TYPES = ("wireguard", "openvpn", "tailscale", "tor")  # vpn.py's TYPES


def team_lead(team, leads):
    """The name of the start that leads team TEAM (sandbox-ai team: NAME-AGENT of its record's lead), or None (no
    record, a damaged one). LEADS caches the answers of one listing."""
    if team not in leads:
        try:
            data = module("team").load(team)
            leads[team] = f"{team}-{data['lead']}"
        except (OSError, ValueError, KeyError, TypeError):
            leads[team] = None
    return leads[team]


def grouping(entry, records, leads=None):
    """The grouping fields of ENTRY (pod, companions, helper_of, teams, lead) from its run's record in RECORDS. A
    run.json is read from disk and may be damaged: each field is checked and a bad value is left out (None, [])
    instead of failing the listing. helper_of names another run that runs now; companions are known agents; lead
    the start that leads the team ENTRY is a member of (its name NAME-AGENT; None for the lead itself). LEADS: the
    teams' leads read so far (team_lead)."""
    run = entry.get("run")
    record_ = records.get(run) if isinstance(run, str) else None
    record_ = record_ if isinstance(record_, dict) else {}
    pod = record_.get("pod")
    companions = record_.get("companions")
    companions = list(dict.fromkeys(c for c in companions if isinstance(c, str) and c in AGENTS)) \
        if isinstance(companions, list) else []
    helper_of = record_.get("helper_of")
    if not (isinstance(helper_of, str) and helper_of != run and isinstance(records.get(helper_of), dict)):
        helper_of = None
    names = []
    for key in ("mcp", "mcp_running"):
        value = entry.get(key)
        names += value if isinstance(value, list) else []
    teams = sorted({n[len("team-"):] for n in names
                    if isinstance(n, str) and n.startswith("team-") and TEAM_NAME.fullmatch(n[len("team-"):])})
    leads = {} if leads is None else leads
    lead = next((found for found in (team_lead(team, leads) for team in teams if entry.get("kind") != "mcp"
                                     and str(entry.get("name")).startswith(f"{team}-"))
                 if found and found != entry.get("name")), None)
    return {"pod": pod if isinstance(pod, str) and POD_NAME.fullmatch(pod) else None, "companions": companions,
            "helper_of": helper_of, "teams": teams, "lead": lead}


def break_helper_cycles(found):
    """Drop helper_of of the entries on a circle (A helps B helps A): no front end has to follow a cycle. An entry
    that only leads into one keeps its own."""
    by_run = {entry.get("run"): entry for entry in found if isinstance(entry.get("run"), str)}
    for entry in found:
        seen, current = set(), entry.get("helper_of")
        while current is not None and current not in seen:
            if current == entry.get("run"):
                entry["cyclic"] = True
                break
            seen.add(current)
            current = (by_run.get(current) or {}).get("helper_of")
    for entry in found:
        if entry.pop("cyclic", False):
            entry["helper_of"] = None


def entries(include_all=False, prune=True):
    """Every start this registry knows, as a list of entries (dicts, see list --json): the running sandboxes,
    the start directories and the MCP servers; with INCLUDE_ALL also the MCP servers that do not run."""
    starts = start_entries(prune)
    servers = mcp_entries(include_all)
    by_pid = {entry["pid"]: entry for entry in servers.values() if entry["pid"]}
    legacy = {f"session-{key}": entry for key, entry in starts.items()}  # sessions of versions before 0.1.30
    plain = []
    orphans = {}
    records = module("net").running(orphans)
    for run, record_ in orphans.items():  # their launcher was killed; their pod runs on (liveness.py)
        if not isinstance(run, str) or not isinstance(record_, dict) or record_.get("kind") == "mcp" or \
                run.startswith("mcp-"):
            continue
        session = starts.get(text(record_.get("start")) or "") or legacy.get(run)
        if session is not None:
            session["run"], session["run_dir"] = run, record_.get("directory")
            continue
        name = record_.get("name") if valid_name(record_.get("name")) else run
        plain.append({"name": name, "id": run, "kind": "run", "agent": text(record_.get("agent")),
                      "backend": text(record_.get("backend")), "state": "orphaned",
                      "home": "own" if record_.get("own_home") else "discarded", "keep": False,
                      "mode": mode_of(record_), "ask": ask_of(record_), "attachable": False, "run": run, "pid": None,
                      "started": text(record_.get("started")), "workspace": text(record_.get("workspace")),
                      "mcp": record_mcp(record_) or [], "llm": record_llm(record_), "directory": None,
                      "run_dir": record_.get("directory")})
    for run, record_ in records.items():
        if not isinstance(run, str) or not isinstance(record_, dict):
            continue  # a damaged run.json ("run": 5): not one of ours to list
        session = starts.get(text(record_.get("start")) or "") or legacy.get(run)
        if session is not None:
            session["run"], session["run_dir"], session["mode"] = run, record_.get("directory"), mode_of(record_)
            session["ask"] = ask_of(record_)
            running_mcp = record_mcp(record_)
            if running_mcp is not None:  # what it reaches now; its options say what it starts with next time
                session["mcp_running"] = running_mcp
            if record_llm(record_):  # the server it works with now (its options may name another for its next start)
                session["llm"] = record_llm(record_)
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
                      "mode": mode_of(record_), "ask": ask_of(record_),
                      "attachable": False, "run": run, "pid": record_.get("pid"),
                      "started": text(record_.get("started")), "workspace": text(record_.get("workspace")),
                      "mcp": record_mcp(record_) or [], "llm": record_llm(record_), "directory": None,
                      "run_dir": record_.get("directory")})
    found = list(starts.values()) + plain + list(servers.values())
    for entry in found:
        entry["attach_why"] = attach_why(entry)
        if entry.get("state") == "orphaned":
            entry["orphaned_why"] = orphaned_why(entry)
    # What belongs together (the app and the web group by it): a running start's pod (Podman), the agents --with in
    # its sandbox, whose helper it is, the teams it belongs to (its team-NAME board servers) and their lead.
    leads = {}
    for entry in found:
        entry.update(grouping(entry, records, leads))
        # The kill switch of a running start (sandbox-ai net cut): its mode, and whether it has one (0.1.94 on).
        record_ = records.get(entry.get("run")) if isinstance(entry.get("run"), str) else None
        record_ = record_ if isinstance(record_, dict) else {}
        cut = record_.get("cut") if isinstance(record_.get("cut"), str) else ""
        entry["cut"] = cut if cut and all(part in ("remote", "local", "api", "all") for part in cut.split("+")) else None
        entry["killswitch"] = bool(record_.get("net_cut"))
        entry["services"] = record_services(record_)  # --service: the containers of your images beside the agent
        entry["browser"] = record_browser(record_)  # --browser: the app's Browser button opens its VNC socket
        # Behind a VPN or Tor (run.json vpn_profile, vpn_type): the app opens no link of its terminal on this host
        # unasked (it would reach it with this host's own address, past the VPN or Tor).
        profile = record_.get("vpn_profile")
        entry["vpn_profile"] = profile if isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
                                                                                  profile) else None
        entry["vpn_type"] = record_.get("vpn_type") if record_.get("vpn_type") in VPN_TYPES and entry["vpn_profile"] \
            else None
    break_helper_cycles(found)
    # Whom each MCP server serves: the starts that reach it, or will at their next start (sandbox-ai mcp attach); the
    # start it was started for (its mcp.json says so) first, if it is one of them. That one is named in "started_for"
    # (None: not recorded, or no longer listed).
    for name, server in servers.items():
        users = [entry for entry in found if entry.get("kind") != "mcp"
                 and name in (entry.get("mcp") or []) + (entry.get("mcp_running") or [])]
        link = server.get("started_for") or {}
        starter = next((entry for entry in found if entry.get("kind") != "mcp" and (
            (link.get("start") and link["start"] == entry.get("id"))
            or (link.get("run") and link["run"] in (entry.get("run"), entry.get("id"))))), None)
        server["started_for"] = starter["name"] if starter is not None else None
        if starter is not None and starter in users:
            users = [starter] + [entry for entry in users if entry is not starter]
        server["serves"] = [entry["name"] for entry in users]
    found = sorted(found, key=lambda e: (STATE_ORDER.get(e["state"], 9), e.get("started") or "", e["name"]))
    # Where the grouped listing (list without --flat; the app and the web group alike) shows each row: below this
    # start's name, or at the top (None).
    below = parents(found)
    for index, entry in enumerate(found):
        entry["listed_under"] = found[below[index]]["name"] if index in below else None
    return found


# list --folders: what each folder is, as the desktop app's Files page names them (backend.file_roots): a running
# start's workspace and HOME, a session's (one that does not run, its HOME kept) workspace and HOME.
FOLDER_KINDS = ("run", "run-home", "session", "session-home")


def session_workspaces(data, shared=None):
    """The host folders a kept start whose session.json is DATA mounts as workspaces at its next start: its workspace,
    then those of its options (workspaces.next_start_targets: the launcher's own reading of them; SHARED: what it
    keeps between the sessions of one listing). Its workspace alone when the options cannot be read (an older or
    damaged record, a folder gone)."""
    found = [data.get("workspace")]
    options = data.get("options")
    if isinstance(options, list) and isinstance(data.get("cwd"), str) and isinstance(data.get("agent"), str):
        try:
            found += [host for host, _inside in module("workspaces").next_start_targets(data, options, shared)]
        except Exception:  # noqa: BLE001 - what can be read: its workspace
            pass
    return [path for path in found if isinstance(path, str) and path]


def folders(records=None, starts=None):
    """The host folders of the starts on this host: of each running start (not an MCP server's sandbox) its
    workspaces - the start's and those added since (workspaces.listing) - and its HOME; of each start that does not
    run but keeps its options and HOME (a session) its workspaces and HOME. [{"kind": FOLDER_KINDS, "name": the name
    list shows, "id": the start's id (a running start without one: its run), "path": the folder, links resolved}],
    each folder once and only one that is there (a folder of a stopped start may be gone). What the desktop app's
    Files page offers - read over SSH (sandbox-ai hosts run) for a host of sandbox-ai hosts: one call per host.
    Read only: nothing is pruned, nothing asked of Podman."""
    records = module("net").running() if records is None else records
    starts = start_entries(prune=False) if starts is None else starts
    legacy = {f"session-{key}": entry for key, entry in starts.items()}  # sessions of versions before 0.1.30
    found, seen, covered = [], set(), set()
    shared = {}  # the launcher's parsers, for this listing (session_workspaces)

    def add(kind, name, ident, path):
        if not isinstance(path, str) or not path or "\0" in path:
            return
        try:
            real = os.path.realpath(os.path.expanduser(path))
            there = os.path.isdir(real)
        except (OSError, ValueError):
            return
        if real in seen or not there:
            return
        seen.add(real)
        found.append({"kind": kind, "name": name, "id": ident, "path": real})
    for run, record_ in records.items():
        if not isinstance(run, str) or not isinstance(record_, dict) or record_.get("kind") == "mcp" or \
                run.startswith("mcp-"):
            continue
        start = starts.get(text(record_.get("start")) or "") or legacy.get(run)
        if start is not None:
            covered.add(start["id"])
            name, ident = start["name"], start["id"]
        else:
            name, ident = (record_.get("name") if valid_name(record_.get("name")) else run), run
        try:
            listed_ = module("workspaces").listing(record_)
        except Exception:  # noqa: BLE001 - an older or damaged record: the start's workspace
            listed_ = [{"host": record_.get("workspace"), "kind": "dir"}]
        for workspace in listed_:
            if isinstance(workspace, dict) and workspace.get("kind", "dir") == "dir":
                add("run", name, ident, workspace.get("host"))
        add("session-home" if start is not None else "run-home", name, ident, record_.get("agent_home"))
        if start is not None and start.get("directory"):
            add("session-home", name, ident, str(Path(start["directory"]) / "home"))
    for key, entry in starts.items():
        if key in covered or entry.get("state") == "damaged" or not entry.get("directory"):
            continue
        data = read_json(Path(entry["directory"]) / "session.json")
        if not isinstance(data, dict):
            continue
        for workspace in session_workspaces(data, shared):
            add("session", entry["name"], key, workspace)
        add("session-home", entry["name"], key, str(Path(entry["directory"]) / "home"))
    return found


def show_folders(found, out=print, as_json=False):
    if as_json:
        out(json.dumps(found, indent=2))
        return
    if not found:
        out("No folders: nothing runs or is kept here.")
        return
    printable = launcher().printable
    rows = [("NAME", "KIND", "FOLDER")] + [(printable(entry["name"]), entry["kind"], printable(entry["path"]))
                                           for entry in found]
    widths = [max(len(row[index]) for row in rows) for index in range(2)]
    for row in rows:
        out(f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  {row[2]}")


def orphaned_why(entry):
    """Why the orphaned start ENTRY runs without its supervisor and what to do, in one text: the record of its
    supervisor's end (liveness.supervisor_end: its launcher's, its sandbox's or its terminal's word - when, why,
    whether its network was cut) from its run directory, else from its start's directory; else the general reason."""
    live = module("liveness")
    run = entry.get("run") if isinstance(entry.get("run"), str) else None
    end = live.supervisor_end(entry.get("run_dir"), run=run) if entry.get("run_dir") else None
    if end is None and entry.get("directory") and run:
        end = live.supervisor_end(entry["directory"], run=run)
    return live.orphan_text(entry.get("name") or "NAME", end)


def parents(found):
    """{index: index} of the rows of FOUND (entries, in their order) that the grouped listing shows below another
    one: an MCP server below the first start it serves ("serves": the start it was started for comes first), a start
    below its lead ("helper_of": the lead's run; "lead": a team's lead, by name). One level deep, as in the app and the web: a row goes below the
    top of its chain (a lead's helper's MCP server below the lead); rows that name each other round in a circle stay
    at the top. Rows without such a link (an MCP server no listed start uses, older records) stay at the top."""
    refs = {}
    for index, entry in enumerate(found):
        if entry.get("kind") != "mcp":
            for ref in (entry.get("name"), entry.get("id"), entry.get("run")):
                if isinstance(ref, str) and ref:
                    refs.setdefault(ref, index)
    direct = {}
    for index, entry in enumerate(found):
        named = entry.get("serves") if entry.get("kind") == "mcp" else [entry.get("helper_of"), entry.get("lead")]
        for ref in named if isinstance(named, list) else []:
            parent = refs.get(ref) if isinstance(ref, str) else None
            if parent is not None and parent != index:
                direct[index] = parent
                break
    top = {}
    for index in direct:
        seen, at = {index}, index
        while at in direct:
            at = direct[at]
            if at in seen:
                at = index  # a circle: it stands on its own
                break
            seen.add(at)
        if at != index:
            top[index] = at
    return top


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


def renamed_since(name, found=None):
    """The name a start that was called NAME has now (renamed once or more, its history says), else None."""
    seen = set()
    while valid_name(name) and name not in seen:
        seen.add(name)
        events = history(name)
        if not events or events[-1].get("event") != "rename" or not isinstance(events[-1].get("to"), str):
            return None
        name = events[-1]["to"]
        found = entries(True) if found is None else found
        if any(entry.get("name") == name for entry in found):
            return name
    return None


def rename(old, new=None, out=print):
    """Give the start OLD the name NEW (None: a fresh codename, codenames.py): its start directory and its running
    run's record say so, and its history moves along. The run id stays (sockets, logs, approvals)."""
    if new is not None:
        check_name(new, "rename NEW")
    found = entries(True)
    entry = find(old, found)
    if new is None:
        new = module("codenames").free_codename(names_taken(found))
    printable = launcher().printable
    if entry["kind"] == "mcp":
        raise ValueError("MCP servers keep the name of their definition")
    if entry["state"] == "damaged":
        # (as resume, attach and delete say it: `sandbox-ai delete` refuses a damaged one too)
        folder = entry.get("directory") or str(module("sessions").session_dir(entry["id"]))
        raise ValueError(f"{printable(entry['id'])} is damaged (see {printable(str(folder))}/session.json); repair "
                         "that file, or remove that directory")
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
        restart=False, mcp=(), options=(), dropped=(), restore=False, paths=()):
    """attach, pause, resume, stop or delete the start NAME, whatever kind it is. REVIEW (resume): show what
    changed in its kept HOME instead, AS_JSON for programs; RESTORE: put back what the HOME's manifest kept of the
    changed files (PATHS: only these; YES: without asking). WORKSPACES, MOUNTS, MCP servers (resume): added at its
    start, OPTIONS (launcher options) and DROPPED (--drop-option) change its saved options; RESTART: a running one is
    stopped first."""
    changing = bool(workspaces or mounts or mcp or restart or options or dropped)
    printable = launcher().printable
    try:
        entry = find(name)
    except ValueError as exc:
        LOG.info("starts", f"{command} {name}: no start of that name", exc)
        pod = stray_pod(name) if command == "stop" else None
        if pod is not None:
            return stop_stray_pod(pod, module("net").STOP_WAIT if wait is None else wait, out)
        current = renamed_since(name)
        if current is not None:
            raise ValueError(f"{printable(name)} was renamed to {printable(current)}: sandbox-ai {command} "
                             f"{printable(current)}") from None
        if command == "resume" and valid_name(name) and history(name):
            raise ValueError(f"{printable(name)} has ended and its HOME was discarded; see: sandbox-ai list "
                             f"--history {printable(name)}") from None
        raise
    shown = printable(entry["name"])
    LOG.info("starts", f"{command} asked for {entry['name']} ({entry['kind']}, {entry['state']})"
                       + ("; with changes: " + LOG.command_line([*workspaces, *mounts, *mcp, *options, *dropped])
                          if changing else "") + (" (restart)" if restart else ""))
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
        if restore:
            return sessions.restore_home(key, list(paths), yes, as_json, out)
        if command == "stop":
            return sessions.stop(key) if wait is None else sessions.stop(key, wait)
        if command == "resume" and changing:
            if restart and entry["state"] in ("running", "paused", "orphaned"):
                # Its new start checked before it is stopped (options, MCP servers and their images, room on the
                # disks): a start that would fail leaves it running as it was.
                sessions.check_restart(key, list(workspaces), list(mounts), list(mcp), list(options), list(dropped))
                why = "to start it again" + (f" with the MCP server{'s' if len(mcp) > 1 else ''} {', '.join(mcp)}"
                                             if mcp else "")
                out(f"{shown}: stopping it {why}")
                sessions.stop(key, by=f"sandbox-ai resume {entry['name']} --restart ({why})")
                try:
                    return sessions.resume(key, list(workspaces), list(mounts), list(mcp), list(options),
                                           list(dropped))
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    LOG.warning("starts", f"resume {entry['name']} --restart: stopped, and its new start failed", exc)
                    raise sessions.restart_failed(key, why, exc) from None
            return sessions.resume(key, list(workspaces), list(mounts), list(mcp), list(options), list(dropped))
        return {"attach": sessions.attach, "pause": sessions.pause, "resume": sessions.resume}[command](key)
    if entry["state"] == "orphaned":
        if command == "stop":
            return stop_orphaned_run(entry, module("net").STOP_WAIT if wait is None else wait, out)
        raise ValueError(f"{shown} runs without its launcher (that ended unexpectedly; its Podman pod runs on): end it "
                         f"with: sandbox-ai stop {shown}")
    if changing:
        raise ValueError(f"{shown} keeps nothing to start again with (a run without a kept start): end it and start "
                         "anew with " + ("--mcp " + " --mcp ".join(mcp) if mcp and not (workspaces or mounts)
                                         else "the workspace"))
    record_ = module("net").running().get(entry["run"])
    if record_ is None:
        raise ValueError(f"{shown} has ended meanwhile")
    if review or restore:
        raise ValueError(f"{shown} keeps no HOME: there is nothing to {'review' if review else 'restore'}")
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


def shell(name, command=(), tty=None, out=print):
    """`sandbox-ai shell NAME [-- COMMAND ...]`: a shell of the human (or COMMAND) in the running start NAME, under the
    agent's own rules (sessions.shell); its exit status. Only a start in tmux has the relay it goes through: the
    same starts as attach."""
    printable = launcher().printable
    try:
        entry = find(name)
    except ValueError:
        current = renamed_since(name)
        if current is not None:
            raise ValueError(f"{printable(name)} was renamed to {printable(current)}: sandbox-ai shell "
                             f"{printable(current)}") from None
        raise
    shown = printable(entry["name"])
    if entry["kind"] == "mcp":
        raise ValueError(f"{shown} is an MCP server sandbox: no shell is opened in it (it runs only its server); "
                         "open one in an agent's start that uses it")
    if entry["state"] == "damaged":
        raise ValueError(f"{shown} is damaged (see {entry['directory']}/session.json); remove that directory")
    if entry.get("directory"):
        return module("sessions").shell(entry["id"], list(command), tty)
    if entry["state"] == "orphaned":
        raise ValueError(f"{shown} runs without its launcher (that ended unexpectedly; its Podman pod runs on): end it "
                         f"with: sandbox-ai stop {shown}")
    raise ValueError(f"{shown} runs without tmux (a task, no terminal, or --no-tmux): it has no shell relay to open a "
                     f"shell through; it ends with its task, or with: sandbox-ai stop {shown}")
STOP_ALL_STATES = ("running", "paused", "orphaned")


def stop_all(out=print, wait=None, yes=False, ask=None, inside=None):
    """`sandbox-ai stop --all`: every start of this user on this host that runs, is paused or runs on without its
    launcher (orphaned) - sessions, runs and tasks in the foreground; not the MCP servers, which the starts that use
    them share - stopped one after another, each as `stop NAME` stops it (the same cleanup). Asked first with the list
    unless YES (no terminal to ask on: refused without --yes); refused inside a sandbox (INSIDE: liveness's
    inside_sandbox; it would see none of the host's and must not end them). 0 when every one ended, else 1."""
    printable = launcher().printable
    if module("liveness").inside_sandbox() if inside is None else inside:
        raise ValueError("--all is refused inside a sandbox: run it on the host, where the starts run")
    targets = [entry for entry in entries() if entry.get("kind") != "mcp" and entry.get("state") in STOP_ALL_STATES]
    if not targets:
        LOG.info("starts", "stop --all: nothing to stop (no start runs)")
        out("nothing to stop: no start runs")
        return 0
    listing = ", ".join(f"{printable(e['name'])} ({e['state']})" for e in targets)
    LOG.info("starts", f"stop --all: {len(targets)} to stop: {listing}")
    if not yes:
        if ask is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError(f"this stops {len(targets)}: {listing}; confirm with --yes")
        answer = (ask or input)(f"Stop these {len(targets)} starts: {listing}? [y/N] ")
        if str(answer).strip().lower() not in ("y", "yes"):
            LOG.info("starts", "stop --all: not confirmed; nothing stopped")
            out("nothing stopped")
            return 1
    failed = []
    for number, entry in enumerate(targets, 1):
        shown = printable(entry["name"])
        out(f"stopping {shown} ({number}/{len(targets)})")
        try:
            act("stop", entry["name"], out, wait=wait)
        except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            failed.append(shown)
            LOG.warning("starts", f"stop --all: {shown} was not stopped", exc)
            print(f"sandbox-ai stop: {shown}: {printable(str(exc))}", file=sys.stderr)
    LOG.log("WARNING" if failed else "INFO", "starts", f"stop --all: stopped {len(targets) - len(failed)} of "
                                                     f"{len(targets)}" + (f"; not stopped: {', '.join(failed)}"
                                                                          if failed else ""))
    out(f"stopped {len(targets) - len(failed)} of {len(targets)}" + (f"; not stopped: {', '.join(failed)}"
                                                                     if failed else ""))
    return 1 if failed else 0


def stray_pod(name):
    """The running Podman pod NAME names (agent-RUN, or RUN) that no start knows (its launcher is gone, and an older
    version removed its run directory: gc names these), or None. Only sandbox-ai's own (liveness.OWN_POD names, or
    its labels, as gc judges them): a pod of yours that is merely named agent-... is never stopped and removed."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        return None
    liveness = module("liveness")
    pod = name if name.startswith("agent-") else f"agent-{name}"
    info = (liveness.pods(fresh=True) or {}).get(pod)
    if not info or not info["live"]:
        return None
    own = liveness.OWN_POD.fullmatch(pod) or liveness.RUN_LABEL in info["labels"] or \
        liveness.SESSION_LABEL in info["labels"]
    return pod if own else None


def stop_stray_pod(pod, wait, out=print):
    """End the running pod POD that no start knows, as asked by its name: stop it, then remove it."""
    printable = launcher().printable
    print(f"sandbox-ai: warning: no start knows {printable(pod)} (its launcher is gone); stopping and removing the "
          "Podman pod as you asked", file=sys.stderr)
    stopped, said = module("liveness").stop_pod(pod, None, wait)
    if not stopped:
        raise ValueError(f"the pod {printable(pod)} could not be stopped ({printable(said) or 'it still runs'})")
    out(f"{printable(pod)}: stopped")
    return 0


def stop_orphaned_run(entry, wait, out=print):
    """End the run ENTRY (no kept start) whose launcher is gone while its Podman pod runs on: stop and remove the pod,
    then record the end and remove the run's directory (net.running does, now that it has ended)."""
    shown = launcher().printable(entry["name"])
    orphans = {}
    module("net").running(orphans)
    record_ = orphans.get(entry["run"])
    if record_ is None:
        raise ValueError(f"{shown} has ended meanwhile")
    liveness = module("liveness")
    pod = liveness.pod_of(record_)
    url = text(record_.get("podman_url"))
    record(entry["name"], "stop", entry["id"], entry["run"])
    out(f"{shown}: its launcher is gone; stopping its pod {pod} (up to {wait:g} s)")
    stopped, said = liveness.stop_pod(pod, url, wait)
    if not stopped:
        raise ValueError(f"{shown}: its pod {pod} could not be stopped ({launcher().printable(said) or 'it still runs'});"
                         f" try: podman pod stop {pod} && podman pod rm {pod}")
    module("net").running()  # ended now: its end is recorded and its directory goes (net.forget_ended)
    out(f"{shown}: stopped")
    return 0


def pause_run(entry, record_, freeze):
    """Freeze (FREEZE) or thaw a run that has no start directory: its pod with Podman, the processes below its
    launcher with bwrap (the launcher stays: it may hold a terminal). The launcher is checked first."""
    shown = launcher().printable(entry["name"])
    pid, _ = module("net").launcher_process(record_)
    marker = Path(record_["directory"]) / "paused"
    if freeze == marker.exists():
        print(f"sandbox-ai: {shown} is {'paused' if freeze else 'running'} already", file=sys.stderr)
        return 0
    if record_.get("backend") == "podman":
        pod = record_.get("pod")
        if not isinstance(pod, str) or not re.fullmatch(r"agent-[A-Za-z0-9_.-]{1,80}", pod):
            raise ValueError(f"{shown} was started by an older version that did not record its pod; it cannot be "
                             "paused (stop it, or wait for it)")
        engine = SimpleNamespace(podman_url=text(record_.get("podman_url")))
        module("sessions").pod_freeze(engine, pod, freeze)
    else:
        module("sessions").signal_tree(pid, signal.SIGSTOP if freeze else signal.SIGCONT, include_root=False)
    if freeze:
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
        # The kill switch asked for while it was paused: set now, before this returns (sandbox-ai net cut).
        module("net").apply_pending_cut(record_["directory"])
    record(entry["name"], "pause" if freeze else "resume", entry["id"], entry["run"])
    print(f"sandbox-ai: {shown} {'paused' if freeze else 'resumed'}", file=sys.stderr)
    return 0


def log_projects():
    """{log folder name: workspace} of the workspaces the starts and the histories name (each one's default log folder,
    as the launcher names it): which project an old log folder was for."""
    found = set()
    with contextlib.suppress(OSError, ValueError):
        found |= {entry.get("workspace") for entry in start_entries(prune=False).values()}
    try:
        files = sorted(history_dir().glob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        name = path.name[:-len(".jsonl")]
        if valid_name(name) and not path.is_symlink():
            found |= {entry.get("workspace") for entry in history(name)}
    activity = module("activity")
    return {activity.default_log_dir(Path(workspace)).name: workspace for workspace in found
            if isinstance(workspace, str) and workspace.startswith("/")}


def old_log_folders(days, busy=None):
    """The log folders nothing was written into for more than DAYS days (statedirs.old_log_folders), never one a
    registered run logs into (BUSY: those folders, else asked), each with the project it was for where a start or a
    history names it ("project", and "project_gone": that folder is no more)."""
    busy = registered_log_dirs() if busy is None else busy
    found = module("statedirs").old_log_folders(None, days, busy)
    if found:
        projects = log_projects()
        for item in found:
            project = projects.get(item["name"])
            if project:
                item["project"], item["project_gone"] = project, not os.path.isdir(project)
    return found


def registered_log_dirs():
    """The log folders the registered runs (registered_runs: alive or not) log into."""
    return [record["log_dir"] for record in registered_runs().values() if isinstance(record.get("log_dir"), str)]


def gc(days=None, dry_run=False, expired_only=False, compact=True, caches=False, images=False, logs_days=None,
       kinds=(), transcripts_days=None, homes=(), compact_days=None, only=False, rules=True):
    """Remove what is no longer needed: sessions that ended without keeping their HOME (leftovers of a supervisor
    that died); with DAYS (else keep_days) also the stopped kept starts unused for more than DAYS days, and the
    histories of names no start uses whose last entry is that old; and the Podman pods of sandbox-ai's that nothing
    runs in any more and no start uses (leftovers of launchers that were killed, a reboot). EXPIRED_ONLY: only those
    kept starts (what a new start does with keep_days). DRY_RUN: say what would go, remove nothing. Pods that still
    run a container are never removed: those no start knows are named ("orphaned_pods"). COMPACT: the logs of runs
    that ended compressed and large shared logs split (compact_logs: "compacted"), the logs older than log_days removed
    (expire_logs: "log_days"); CACHES: the download caches of the
    stopped kept starts' HOMEs emptied ("caches"); IMAGES: the images of sandbox-ai's that a rebuild left without a
    name removed ("images"); LOGS_DAYS: the log folders nothing was written into for more than that many days
    removed, never one a registered run logs into ("log_folders"). KINDS ("build", "toolchains", "transcripts" -
    those older than TRANSCRIPTS_DAYS): that of the stopped kept HOMEs removed ("home_items", clear_home_kinds);
    HOMES (names or ids): the caches and KINDS of these starts only. COMPACT_DAYS: the transcripts older than that
    compacted in place ("transcripts", compact_transcripts - also of a running start). ONLY: nothing but what CACHES,
    KINDS, COMPACT_DAYS, IMAGES and LOGS_DAYS name (no leftovers, no expiry, no log compression: what a storage
    check's plan runs); LOGS_DAYS with HOMES: the log files of those starts' project folders (project_logs). RULES:
    the automatic storage rules applied too (apply_rules: "rules"; not with ONLY or EXPIRED_ONLY)."""
    origin = "older-than" if days is not None else "keep_days" if keep_days() is not None else None
    days = days if days is not None else keep_days()
    # 0 days (--older-than 0) is every stopped kept start, not "no expiry"
    cutoff = time.time() - days * 86400 if days is not None else None
    sessions = module("sessions")
    result = {"days": days, "dry_run": dry_run, "removed": [], "leftovers": [], "histories": [], "damaged": [],
              "skipped": [], "pods": [], "orphaned_pods": [], "lends": [], "cairn_volumes": [], "compacted": None,
              "caches": [], "images": [], "images_kept": {}, "log_folders": [], "expiring": [], "rootfs": [],
              "mcp_logs": None, "expiry": {"days": days, "origin": origin}, "home_items": [], "log_days": None,
              "checkpoints": [], "transcripts": [], "project_logs": [], "rules": None, "only": only}
    if only:
        return gc_only(result, dry_run, caches, images, logs_days, kinds, transcripts_days, homes, compact_days)
    if not expired_only:
        for key, entry in start_entries(prune=False).items():
            if entry["state"] == "damaged":
                result["damaged"].append(entry["directory"])
            elif not entry["keep"] and entry["state"] == "stopped":
                result["leftovers"].append(entry["name"])
                if not dry_run:
                    sessions.prune(key)
    found = entries(True, prune=not dry_run)
    if not expired_only:
        leftover_pods(found, result, dry_run)
        leftover_lends(result, dry_run)
        leftover_cairn_volumes(result, dry_run)
        leftover_remote_mounts(result, dry_run)
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
    if days is None and not expired_only:  # no --older-than, no keep_days: the default expiry (or keep_days 0: none)
        apply_default_expiry(found, result, dry_run)
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
    if expired_only:
        return result
    removed = {entry["id"] for entry in result["removed"]}
    if rules:
        result["rules"] = apply_rules([entry for entry in found if entry["id"] not in removed], dry_run)
    if compact_days is not None:
        result["transcripts"] = compact_transcripts(
            chosen_homes([entry for entry in found if entry["id"] not in removed], homes), compact_days, dry_run,
            result["skipped"])
    if caches:
        result["caches"] = clear_caches(chosen_homes([entry for entry in found if entry["id"] not in removed], homes),
                                        dry_run)
    if kinds:
        result["home_items"] = clear_home_kinds(
            chosen_homes([entry for entry in found if entry["id"] not in removed], homes), kinds, transcripts_days,
            dry_run, result["skipped"])
    if images:
        collect_images(result, images, dry_run)
    if logs_days is not None:
        collect_logs(result, chosen_homes([entry for entry in found if entry["id"] not in removed], homes)
                     if homes else None, logs_days, dry_run)
    result["rootfs"] = remove_unused_rootfs(dry_run, result["skipped"])
    # The workspace checkpoints beyond their limits (checkpoint_days, checkpoint_count, checkpoint_gb), never the
    # start checkpoint of a run that runs now; then the content no checkpoint holds.
    live = [entry["run"] for entry in found if entry.get("state") in ("running", "paused", "orphaned")
            and isinstance(entry.get("run"), str)]
    try:
        result["checkpoints"] = [item for item in module("checkpoints").prune_all(dry_run, live)
                                 if item.get("removed") or item.get("error")]
    except (OSError, ValueError) as exc:
        result["skipped"].append({"name": "checkpoints", "reason": str(exc)})
    if compact:
        result["compacted"] = compact_logs(dry_run)
        result["mcp_logs"] = module("statedirs").compact_mcp(dry_run=dry_run)
        result["log_days"] = expire_logs(dry_run)
    return result


def gc_only(result, dry_run, caches, images, logs_days, kinds, transcripts_days, homes, compact_days):
    """gc --only: what CACHES, KINDS, COMPACT_DAYS, IMAGES and LOGS_DAYS name and nothing else (the storage check's
    plan runs one at a time), of the starts HOMES names (all without)."""
    found = entries(True, prune=False)
    chosen = chosen_homes([entry for entry in found if entry.get("kind") != "mcp"], homes)
    if compact_days is not None:
        result["transcripts"] = compact_transcripts(chosen, compact_days, dry_run, result["skipped"])
    if caches:
        result["caches"] = clear_caches(chosen, dry_run)
    if kinds:
        result["home_items"] = clear_home_kinds(chosen, kinds, transcripts_days, dry_run, result["skipped"])
    if images:
        collect_images(result, images, dry_run)
    if logs_days is not None:
        collect_logs(result, chosen if homes else None, logs_days, dry_run)
    return result


def collect_images(result, images, dry_run):
    """gc --images[=LEVEL]: the Podman images of that level removed (DRY_RUN: listed) into RESULT."""
    result["image_level"] = "unnamed" if images is True else images
    if result["image_level"] == "unnamed":
        dangling = module("statedirs").dangling_images() or []
        result["images"] = [image["id"] for image in dangling]
        result["image_plan"] = [{"group": "sandbox_unnamed", **image, "names": []} for image in dangling]
        if not dry_run and dangling:
            result["images"], result["images_kept"] = module("statedirs").remove_images(result["images"])
    else:
        result["image_plan"] = image_plan(result["image_level"])
        refs = [ref for image in result["image_plan"] for ref in (image["names"] or [image["id"]])]
        result["images"] = refs
        if not dry_run and refs:
            result["images"], result["images_kept"] = module("statedirs").remove_image_refs(refs)


def collect_logs(result, chosen, days, dry_run):
    """gc --logs-older-than DAYS into RESULT: the log folders not written for that long ("log_folders"); with CHOSEN
    (--home) the log files of those starts' project folders instead ("project_logs", project_logs)."""
    if chosen is None:
        result["log_folders"] = remove_old_log_folders(days, dry_run, result["skipped"])
        return
    for entry in chosen:
        try:
            logs = project_logs(entry, days, dry_run)
        except (OSError, ValueError, RuntimeError) as exc:
            result["skipped"].append({"name": entry["name"], "reason": f"its logs: {exc}"})
            continue
        if logs["files"] or logs["removed"]:
            result["project_logs"].append({"name": entry["name"], **logs})


SHA256_TEXT = re.compile(r"[0-9a-f]{64}")


def rootfs_in_use():
    """Every SHA-256 your MCP servers' definitions (mcp.json), sandbox-ai's own and the teams' name, and the state of
    every MCP server sandbox (state/mcp/NAME/*.json) - the root file systems in use -, or None when one of them cannot
    be read (then none is unused)."""
    found = set()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "mcp.json"
    try:
        if os.path.lexists(config):
            content = config.read_text()
            json.loads(content)
            found |= set(SHA256_TEXT.findall(content))
        for path in sorted((state_root() / "mcp").glob("*/*.json")):
            found |= set(SHA256_TEXT.findall(path.read_text(errors="replace")))
        mcp = module("mcp")
        found |= set(SHA256_TEXT.findall(json.dumps([mcp.BUILTIN, mcp.team_definitions()], default=str)))
    except (OSError, ValueError, RecursionError, TypeError, AttributeError):
        return None
    return found


def remove_unused_rootfs(dry_run=False, skipped=None):
    """The MCP root file systems (data/mcp-rootfs/SHA256) no server uses (statedirs.unused_rootfs against
    rootfs_in_use) removed - each is unpacked again from its checked archive when a server needs it -, or listed
    (DRY_RUN): [{"sha256", "path", "bytes"}]."""
    referenced = rootfs_in_use()
    if referenced is None:
        if skipped is not None:
            skipped.append({"name": "mcp-rootfs", "reason": "which MCP servers there are could not be read: no root "
                                                           "file system was removed"})
        return []
    found = module("statedirs").unused_rootfs(referenced)
    if dry_run:
        return found
    removed = []
    for item in found:
        try:
            module("mcp_rootfs").remove(item["path"])
        except OSError as exc:
            if skipped is not None:
                skipped.append({"name": f"mcp-rootfs/{item['sha256'][:12]}", "reason": str(exc)})
            continue
        removed.append(item)
        LOG.info("starts", f"gc: removed the MCP root file system {item['path']} (no server uses it)")
    return removed


def expiring_by_default(found, when=None):
    """The entries of FOUND the default expiry is for: stopped kept starts (never one that runs, is paused or
    orphaned, never an MCP server) last used more than DEFAULT_KEEP_DAYS days ago (one whose last use is unknown never
    is)."""
    when = time.time() if when is None else when
    cutoff = when - DEFAULT_KEEP_DAYS * 86400
    return [entry for entry in found if entry.get("kind") != "mcp" and entry.get("state") == "stopped"
            and entry.get("keep") and (seconds(entry.get("last_used")) or when) <= cutoff]


def write_notice(path, data):
    """EXPIRY_FILE as DATA, replaced in one step; False when it could not be written."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".expiry-", dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def expiry_noticed(candidates, when=None, shown=True):
    """When the default expiry was first said to you (seconds) - its removals wait EXPIRY_GRACE days after that -, or
    None. The first time it has CANDIDATES, EXPIRY_FILE is written (naming them); SHOWN: what calls this shows it to
    you now (gc, gc --usage and the app show their result; the daily pass in the background does not, a start says it
    itself: expiry_notice), so the grace starts now if it has not."""
    when = time.time() if when is None else when
    path = state_root() / EXPIRY_FILE
    notice = read_json(path) or {}
    said = seconds(notice.get("said"))
    if said is not None or (not candidates and seconds(notice.get("noticed")) is None):
        return said
    if seconds(notice.get("noticed")) is None:
        notice = {"noticed": iso(when), "days": DEFAULT_KEEP_DAYS, "grace_days": EXPIRY_GRACE,
                  "names": [entry.get("name") for entry in candidates][:50]}
    if shown:
        notice["said"] = notice["last_said"] = iso(when)
    if not write_notice(path, notice):
        return None  # not recorded: not announced, so nothing is removed
    if shown:
        LOG.info("starts", f"the default expiry of kept starts ({DEFAULT_KEEP_DAYS} days unused) announced; removals "
                           f"from {iso(when + EXPIRY_GRACE * 86400)} on: "
                           f"{', '.join(map(str, notice.get('names') or []))}")
        return when
    return None


def apply_default_expiry(found, result, dry_run=False, when=None, shown=True):
    """Without keep_days in your defaults: the stopped kept starts of FOUND unused for DEFAULT_KEEP_DAYS days
    (expiring_by_default) removed with their HOME (into RESULT["removed"]) once the expiry was said to you and its
    grace is over; those still within it into RESULT["expiring"] with the time they go ("removed_from": None until
    it was said: SHOWN, see expiry_noticed). RESULT["expiry"] says how it is set ({"days", "origin", "noticed",
    "removed_from", "off": the command that switches it off})."""
    when = time.time() if when is None else when
    days, origin = expiry_setting()
    result.setdefault("expiring", [])
    result["expiry"] = {"days": days, "origin": origin, "off": EXPIRY_OFF}
    if origin != "default":
        return result
    candidates = expiring_by_default(found, when)
    noticed = expiry_noticed(candidates, when, shown)
    start = noticed + EXPIRY_GRACE * 86400 if noticed is not None else None
    result["expiry"].update(noticed=iso(noticed) if noticed is not None else None,
                            removed_from=iso(start) if start is not None else None)
    for entry in candidates:
        item = {key: entry.get(key) for key in ("name", "id", "agent", "last_used", "workspace")}
        if start is None or when < start:
            result["expiring"].append({**item, "removed_from": iso(start) if start is not None else None})
            continue
        if not dry_run:
            try:
                module("sessions").delete(entry["id"], yes=True)
            except (OSError, ValueError) as exc:  # busy (another command holds it), or gone meanwhile
                result.setdefault("skipped", []).append({"name": entry["name"], "reason": str(exc)})
                continue
            LOG.info("starts", f"removed the kept start {entry['name']}: unused for more than {DEFAULT_KEEP_DAYS} days "
                               f"(the default expiry; {EXPIRY_OFF} keeps them)")
        result["removed"].append({**item, "expiry": "default"})
    return result


def expire_by_default(dry_run=False):
    """The daily pass's share of gc: the default expiry of kept starts (apply_default_expiry) alone - in the
    background, so it starts no grace: what it finds first waits until a start, gc or the app said it."""
    return apply_default_expiry(entries(True, prune=False), {"removed": [], "skipped": []}, dry_run, shown=False)


def expiry_notice(when=None):
    """What a start says about the default expiry (once a day) from when it was found until its grace is over: which
    kept starts go from when, and how to keep them - said here first, the grace starts now. None otherwise (nothing
    found, the grace over, or keep_days set)."""
    when = time.time() if when is None else when
    if expiry_setting()[1] != "default":
        return None
    path = state_root() / EXPIRY_FILE
    notice = read_json(path)
    if notice is None or seconds(notice.get("noticed")) is None:
        return None
    said, last = seconds(notice.get("said")), seconds(notice.get("last_said"))
    if said is not None and (when >= said + EXPIRY_GRACE * 86400 or (last is not None and when - last < 86400)):
        return None
    if said is None:
        notice["said"] = iso(when)
        said = when
    notice["last_said"] = iso(when)
    if not write_notice(path, notice):
        return None
    start = said + EXPIRY_GRACE * 86400
    names = [str(name) for name in notice.get("names") or [] if isinstance(name, str)]
    shown = launcher_printable(", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else ""))
    return (f"kept starts unused for more than {DEFAULT_KEEP_DAYS} days are removed with their HOME from "
            f"{shown_time(iso(start))} UTC on, by sandbox-ai gc and its daily pass (the default; no keep_days in your "
            f"defaults){': ' + shown if shown else ''}. To keep them: resume them, or switch it off with: {EXPIRY_OFF}")


def launcher_printable(value):
    """Text as the launcher prints it, without loading the launcher for it (sessions.printable)."""
    return module("sessions").printable(value)


def remove_old_log_folders(days, dry_run=False, skipped=None):
    """gc --logs-older-than DAYS: the log folders nothing was written into for that long (old_log_folders) removed -
    each checked once more against the registered runs right before it goes (statedirs.remove_log_folder) -, or only
    listed (DRY_RUN). Those that could not be told or removed go into SKIPPED."""
    try:
        found = old_log_folders(days)
    except (OSError, ValueError, RuntimeError) as exc:
        if skipped is not None:
            skipped.append({"name": "logs", "reason": f"which runs still run could not be told ({exc}): no log "
                                                     "folder was removed"})
        return []
    if dry_run:
        return found
    removed = []
    for item in found:
        try:
            gone = module("statedirs").remove_log_folder(item["path"], busy=registered_log_dirs)
        except OSError as exc:
            gone = False
            if skipped is not None:
                skipped.append({"name": item["name"], "reason": str(exc)})
        if gone:
            removed.append(item)
            LOG.info("starts", f"gc: removed the log folder {item['path']} (last written {item['last_written']})")
    return removed


def registered_runs():
    """{run: run.json} of every run registered in a run directory, alive or not (net.running first removes those that
    ended for certain): a run whose state cannot be told counts as running."""
    found = {}
    orphans = {}
    module("net").running(orphans)
    for directory in module("net").run_dirs():
        with contextlib.suppress(OSError):
            for path in directory.glob("*/run.json"):
                data = read_json(path)
                if data is not None and isinstance(data.get("run"), str):
                    found[data["run"]] = data
    return found


def compact_logs(dry_run=False, budget=None):
    """The compaction of the log directories (statedirs.compact): the logs of runs that ended compressed, the shared
    logs over their limit split - never those of a registered run, nor a shared log of a directory one logs into -
    and the per-directory limits of the runs' logs applied (the launcher's rotate_connection_logs and
    rotate_traffic_logs, the compressed ones counted). {"compressed", "split", "dropped", "bytes", "saved", "errors",
    "unfinished"}."""
    statedirs = module("statedirs")
    try:
        runs = registered_runs()
    except (OSError, ValueError, RuntimeError) as exc:
        return {"compressed": [], "split": [], "dropped": [], "bytes": 0, "saved": 0, "unfinished": False,
                "errors": [f"which runs still run could not be told ({exc}): nothing was compressed"]}
    busy = [record["log_dir"] for record in runs.values() if isinstance(record.get("log_dir"), str)]

    def caps(folder):  # (never the logs of a run that runs: bounded by its own in-run limits)
        return launcher().rotate_connection_logs(folder, live=set(runs)) + \
            launcher().rotate_traffic_logs(folder, live=set(runs))
    result = statedirs.compact(None, set(runs), busy, dry_run, budget, caps=caps)
    result["bytes"] = sum(item["bytes"] for item in result["compressed"] + result["split"])
    return result


def clear_caches(found, dry_run=False):
    """The download caches (statedirs.CACHES) of the stopped kept starts of FOUND emptied, each under its start's lock
    (a start being resumed is left alone): [{"name", "path", "bytes"}]."""
    statedirs, sessions = module("statedirs"), module("sessions")
    cleared = []
    for entry in found:
        if entry.get("kind") == "mcp" or entry.get("state") != "stopped" or not entry.get("keep"):
            continue
        try:
            with sessions.session_lock(entry["id"]):
                if sessions.status(entry["id"], sessions.load(entry["id"])) != "stopped":
                    continue
                home = sessions.session_dir(entry["id"]) / "home"
                cleared += [{"name": entry["name"], "path": item["path"], "bytes": item["bytes"]}
                            for item in statedirs.clear_caches([home], dry_run)]
        except (OSError, ValueError):  # busy (another command holds it), or gone meanwhile
            continue
    return cleared


def images_wanted():
    """The image names the kept starts use (each start directory's session.json, any state): its agent's image, its
    --image, the team image with --with. Never fails: what cannot be read names nothing."""
    sessions = module("sessions")
    wanted = set()
    try:
        directories = [path for path in sessions.root().iterdir() if path.is_dir() and not path.is_symlink()]
    except OSError:
        return wanted
    for directory in directories:
        data = read_json(directory / "session.json") or {}
        options = data.get("options") if isinstance(data.get("options"), list) else []
        if isinstance(data.get("agent"), str) and re.fullmatch(r"[a-z0-9-]{1,40}", data["agent"]):
            wanted.add(f"localhost/{data['agent']}-sandbox:latest")
        withs = []
        for index, option in enumerate(options):
            if not isinstance(option, str):
                continue
            value = option.partition("=")[2] if option.startswith("--image=") else \
                options[index + 1] if option == "--image" and index + 1 < len(options) else None
            if isinstance(value, str) and value:
                name = value if "/" in value.split(":", 1)[0] or "." in value.split("/", 1)[0] \
                    else f"localhost/{value}"
                wanted.add(name if ":" in name.rsplit("/", 1)[-1] or "@" in name else f"{name}:latest")
            if option == "--with" or option.startswith("--with="):
                withs.append(option.partition("=")[2] if "=" in option else
                             options[index + 1] if index + 1 < len(options) else "")
        if withs:  # the team image with the layers of its agents (team_image.py), else the base
            try:
                wanted.add(module("team_image").image([data.get("agent"), *withs]))
            except (ValueError, TypeError):
                wanted.add("localhost/team-sandbox:latest")
    return wanted


def image_plan(level, groups=None):
    """What gc --images=LEVEL would remove (statedirs.IMAGE_LEVELS): [{"group", "id", "names", "bytes"}] the biggest
    first. GROUPS: statedirs.image_groups' answer (else asked now)."""
    statedirs = module("statedirs")
    groups = groups if groups is not None else statedirs.image_groups(images_wanted())
    if not groups:
        return []
    found = [{"group": group, **image} for group in statedirs.IMAGE_LEVELS[level]
             for image in groups["groups"][group]["images"]]
    return sorted(found, key=lambda image: -image["bytes"])


def show_image_plan(plan, verb, out=print, colour=None):
    """Podman images gc --images=LEVEL removes (or would), as a table with the total."""
    statedirs, t = module("statedirs"), module("textout")
    size, printable = statedirs.human, launcher().printable
    out(f"{verb} {len(plan)} Podman image(s) ({size(sum(image['bytes'] for image in plan))}):")
    t.grid(["SIZE", "IMAGE", "WHAT"], [[size(image["bytes"]), printable(", ".join(image["names"]) or image["id"]),
                                        statedirs.IMAGE_GROUPS[image["group"]][0]] for image in plan[:50]],
           right={0}, out=out, colour=colour,
           footer=[size(sum(image["bytes"] for image in plan)), f"{len(plan)} image(s)", "Total"]
           if len(plan) > 1 else None)
    if len(plan) > 50:
        out(f"  ... and {len(plan) - 50} more (--json lists all)")


def chosen_homes(found, names=()):
    """The entries of FOUND that NAMES (names or ids) name, all of them without NAMES. ValueError: a name that is
    none of them."""
    if not names:
        return list(found)
    chosen = []
    for name in names:
        matches = [entry for entry in found if name in (entry.get("name"), entry.get("id"))]
        if not matches:
            raise ValueError(f"--home {launcher().printable(str(name))}: no stopped start keeps a HOME of that name "
                             "here (see: sandbox-ai list)")
        chosen += [entry for entry in matches if entry not in chosen]
    return chosen


def stopped_kept(entry):
    return entry.get("kind") != "mcp" and entry.get("state") == "stopped" and entry.get("keep")


def clear_home_kinds(found, kinds, days=None, dry_run=False, skipped=None):
    """KINDS (statedirs.home_removable: "build", "toolchains", "transcripts" older than DAYS days) of the HOMEs of
    the stopped kept starts of FOUND removed, each under its start's lock and only while it is stopped (one being
    started meanwhile, or packed, is left alone; into SKIPPED with why): [{"name", "id", "kind", "what", "path",
    "bytes", "files"}] of what went (DRY_RUN: would go)."""
    statedirs, sessions = module("statedirs"), module("sessions")
    done, scan = [], statedirs.Scan()
    for entry in found:
        if not stopped_kept(entry):
            continue
        try:
            with sessions.session_lock(entry["id"]):
                data = sessions.load(entry["id"])
                if sessions.status(entry["id"], data) != "stopped":
                    continue
                home = sessions.session_dir(entry["id"]) / "home"
                if sessions.homepack().packed(home.parent):
                    if skipped is not None:
                        skipped.append({"name": entry["name"], "reason": "its HOME is packed (gc --pack): nothing "
                                                                         "of it was removed"})
                    continue
                items = statedirs.home_removable(scan, home, kinds, days)
                if not dry_run:
                    items = statedirs.remove_home_items(home, items, days)
                done += [{"name": entry["name"], "id": entry["id"], **item} for item in items]
        except (OSError, ValueError) as exc:  # busy (another command holds it), or gone meanwhile
            if skipped is not None:
                skipped.append({"name": entry["name"], "reason": str(exc)})
    return done


def pack_candidates(found, days=None, names=()):
    """The stopped kept starts of FOUND whose HOME gc --pack would pack: not packed yet, unused for more than DAYS
    days (all without DAYS), of NAMES (all without)."""
    sessions = module("sessions")
    cutoff = time.time() - days * 86400 if days is not None else None
    chosen = []
    for entry in chosen_homes(found, names):
        if not stopped_kept(entry):
            if names:
                raise ValueError(f"{launcher().printable(entry['name'])} is {entry.get('state')}"
                                 + ("" if entry.get("keep") else " and keeps no HOME") + ": only the HOME of a "
                                 "stopped kept start is packed")
            continue
        directory = sessions.session_dir(entry["id"])
        if sessions.homepack().packed(directory) or not (directory / "home").is_dir():
            continue
        used = seconds(entry.get("last_used"))
        if cutoff is not None and (used is None or used > cutoff):
            continue
        chosen.append(entry)
    return chosen


def pack_homes(found, out=print):
    """Pack the HOMEs of FOUND (pack_candidates), each under its start's lock and only while it is stopped and Podman
    says it does not run (homepack.pack: written, read back and compared, then the HOME removed). {"packed": [{"name",
    "id", "bytes", "archive_bytes", "archive"}], "skipped": [{"name", "reason"}]}."""
    statedirs, sessions = module("statedirs"), module("sessions")
    packs = sessions.homepack()
    result = {"packed": [], "skipped": []}
    for entry in found:
        shown = launcher().printable(entry["name"])
        try:
            with sessions.session_lock(entry["id"]):
                data = sessions.load(entry["id"])
                if sessions.status(entry["id"], data) != "stopped" or not sessions.keeps_home(data):
                    raise ValueError("it is not stopped any more")
                if sessions.lost(entry["id"], data) is None:
                    raise ValueError("Podman does not answer whether it still runs in its pod")
                directory = sessions.session_dir(entry["id"])
                size = statedirs.Scan().size(directory / "home")
                out(f"packing the HOME of {shown} ({statedirs.human(size)}) ...")
                record = packs.pack(directory, size=size)
        except (OSError, ValueError, packs.PackError) as exc:
            result["skipped"].append({"name": entry["name"], "reason": str(exc)})
            continue
        LOG.info("starts", f"gc --pack: the HOME of {entry['name']} packed: {statedirs.human(size)} into "
                           f"{statedirs.human(record['archive_bytes'])} ({record['archive']})")
        result["packed"].append({"name": entry["name"], "id": entry["id"], "bytes": size,
                                 "archive_bytes": record["archive_bytes"], "archive": record["archive"]})
    return result


# -- transcripts compacted, the storage check and its rules (storage.py) --------------------------------------------

def with_storage(found):
    """FOUND (entries()) with each start's size at the last storage check and its growth since the check before
    ("storage": {"bytes", "growth", "measured", "since"}; none before a check) - one small file read."""
    try:
        sizes = module("storage").last_sizes()
    except (OSError, ValueError):
        sizes = {}
    return [{**entry, "storage": sizes[entry["id"]]} if isinstance(entry.get("id"), str) and entry["id"] in sizes
            else entry for entry in found]


def compact_transcripts(found, days, dry_run=False, skipped=None, deadline=None):
    """The transcripts older than DAYS days of the kept HOMEs of FOUND compacted in place (storage.compact_home) -
    also of a start that runs, is paused or orphaned: its agent only appends to the newest file of a conversation,
    which is never taken, and a file written meanwhile is left as it was. Each under its start's lock (one a command
    starts or stops meanwhile is left alone, into SKIPPED), never a packed HOME. [{"name", "id", "state", "files",
    "bytes", "saved", "items", "errors"}] of the starts with something compacted (DRY_RUN: that would be)."""
    storage, sessions = module("storage"), module("sessions")
    done = []
    for entry in found:
        if entry.get("kind") == "mcp" or not entry.get("keep") or entry.get("state") in ("damaged", None):
            continue
        try:
            with sessions.session_lock(entry["id"]):
                directory = sessions.session_dir(entry["id"])
                if sessions.homepack().packed(directory):
                    continue
                home = directory / "home"
                if home.is_symlink() or not home.is_dir():
                    continue
                result = storage.compact_home(home, days, dry_run, deadline=deadline)
        except (OSError, ValueError) as exc:  # busy (another command holds it), or gone meanwhile
            if skipped is not None:
                skipped.append({"name": entry["name"], "reason": str(exc)})
            continue
        if result["files"] or result["errors"]:
            done.append({"name": entry["name"], "id": entry["id"], "state": entry.get("state"), **result})
        if not dry_run and result["files"]:
            LOG.info("starts", f"gc: compacted {result['files']} transcript(s) of {entry['name']} "
                               f"({statedirs_human(result['bytes'])}, {statedirs_human(result['saved'])} freed)")
    return done


def statedirs_human(size):
    return module("statedirs").human(size)


def project_logs(entry, days, dry_run=False):
    """The log files of the project folder of the start ENTRY (its main workspace's logs/PROJECT-HASH) not written
    for more than DAYS days (statedirs.old_logs: never the log a running run writes), removed unless DRY_RUN.
    {"folder", "files", "bytes", "removed", "errors"}."""
    statedirs = module("statedirs")
    result = {"folder": None, "files": 0, "bytes": 0, "removed": [], "errors": []}
    workspace = entry.get("workspace")
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        return result
    folder = module("activity").default_log_dir(Path(workspace))
    result["folder"] = str(folder)
    runs = registered_runs()
    busy = [record["log_dir"] for record in runs.values() if isinstance(record.get("log_dir"), str)]
    found = [item for item in statedirs.old_logs(None, days, set(runs), busy)
             if os.path.dirname(item["path"]) == str(folder)]
    result.update(files=len(found), bytes=sum(item["bytes"] for item in found))
    if not dry_run and found:
        gone = statedirs.remove_old_logs(found, days)
        result.update(removed=gone["removed"], bytes=gone["bytes"], errors=gone["errors"])
        LOG.info("starts", f"gc: removed {len(gone['removed'])} log file(s) of {entry['name']}'s project older than "
                           f"{days} days ({statedirs.human(gone['bytes'])})")
    return result


def apply_rules(found=None, dry_run=False, settings=None):
    """The automatic storage rules of your defaults (storage.RULES, each off unless set): the stopped kept starts
    unused for storage_caches_days have their download caches emptied, the transcripts older than
    storage_compact_days compacted (also of a running start: compact_transcripts), the stopped kept starts unused for
    storage_pack_days packed (pack_homes), the log folders not written for storage_logs_days removed. Each action
    logged and kept to be said in the next check's notice (DRY_RUN: only listed). {"settings", "actions": [{"rule",
    "name", "bytes", "files"}], "skipped"}."""
    storage = module("storage")
    settings = storage.settings() if settings is None else settings
    found = entries(True, prune=False) if found is None else found
    result = {"settings": {key: settings.get(key) for key in storage.RULES}, "actions": [], "skipped": [],
              "dry_run": dry_run}
    now = time.time()

    def unused(entry, days):
        when = seconds(entry.get("last_used"))
        return stopped_kept(entry) and when is not None and when <= now - days * 86400
    days = settings.get("storage_caches_days")
    if days:
        sizes = {}
        for item in clear_caches([entry for entry in found if unused(entry, days)], dry_run):
            sizes[item["name"]] = sizes.get(item["name"], 0) + item["bytes"]
        result["actions"] += [{"rule": "caches", "name": name, "bytes": size} for name, size in sizes.items()]
    days = settings.get("storage_compact_days")
    if days:
        result["actions"] += [{"rule": "compact", "name": item["name"], "bytes": item["saved"], "files": item["files"]}
                              for item in compact_transcripts(found, days, dry_run, result["skipped"])
                              if item["files"]]
    days = settings.get("storage_pack_days")
    if days:
        chosen = pack_candidates(found, days)
        if dry_run:
            result["actions"] += [{"rule": "pack", "name": entry["name"], "bytes": 0} for entry in chosen]
        elif chosen:
            packed = pack_homes(chosen, out=lambda line: None)
            result["skipped"] += packed["skipped"]
            result["actions"] += [{"rule": "pack", "name": item["name"],
                                   "bytes": max(0, item["bytes"] - item["archive_bytes"])} for item in packed["packed"]]
    days = settings.get("storage_logs_days")
    if days:
        result["actions"] += [{"rule": "logs", "name": item["name"], "bytes": item["bytes"]}
                              for item in remove_old_log_folders(days, dry_run, result["skipped"])]
    if not dry_run and result["actions"]:
        for action in result["actions"]:
            LOG.info("starts", f"storage rule: {storage.action_text(action)}")
        storage.note_actions([{**action, "time": iso(now)} for action in result["actions"]])
    return result


def plan_item(kind, what, size, argv, start=None, safe=False, estimate=False, checked=False):
    """One recommendation of the storage check: what, how much it frees, the exact command, whether it is safe while
    starts run (else: only for a start that is stopped - gc leaves one that was started meanwhile alone)."""
    return {"key": f"{kind}:{start['id'] if start else '-'}", "kind": kind, "start": start["name"] if start else None,
            "id": start["id"] if start else None, "state": start.get("state") if start else None, "what": what,
            "bytes": int(size), "estimate": estimate, "argv": argv, "command": "sandbox-ai " + shlex.join(argv),
            "safe_running": safe, "checked": checked}


def start_plan(item, full=False):
    """The storage check's recommendations for one start (ITEM: a start of usage(), or of usage_detail() with FULL:
    the Storage view of that start - every action, also those not recommended)."""
    storage, statedirs = module("storage"), module("statedirs")
    sessions = module("sessions")
    name, state, plan = item["name"], item.get("state"), []
    home_info = item.get("home") if isinstance(item.get("home"), dict) else item
    directory = sessions.session_dir(item["id"])
    packed = sessions.homepack().packed(directory)
    if not item.get("keep") or item.get("kind") == "mcp":
        return plan
    stopped = state == "stopped"
    kinds = [entry for entry in home_info.get("items") or [] if isinstance(entry, dict)]
    if stopped and not packed:
        caches = sum((home_info.get("caches") or {}).values())
        if caches or full:
            plan.append(plan_item("caches", "empty its download caches (pip, uv, npm, Go, Yarn, Bun, pnpm; filled "
                                  "again when needed)", caches, ["gc", "--only", "--caches", "--home", name, "--yes"],
                                  item, checked=True))
        build = sum(entry["bytes"] for entry in kinds if entry.get("kind") == "build")
        if build or full:
            plan.append(plan_item("build", "remove its build output and temp folders (node_modules, target/, "
                                  "virtualenvs ...; made again by a build)", build,
                                  ["gc", "--only", "--build-output", "--home", name, "--yes"], item))
        tools = sum(entry["bytes"] for entry in kinds if entry.get("kind") == "toolchains"
                    and entry.get("path") in statedirs.REMOVABLE_TOOLCHAINS)
        if tools or full:
            plan.append(plan_item("toolchains", "remove its toolchains (rustup, nvm, SDKMAN, pyenv ...; their tools "
                                  "install them again)", tools, ["gc", "--only", "--toolchains", "--home", name,
                                                                 "--yes"], item))
        if full:
            old = statedirs.home_removable(statedirs.Scan(), directory / "home", ["transcripts"], storage.REMOVE_DAYS)
            plan.append(plan_item("transcripts", f"remove its transcripts older than {storage.REMOVE_DAYS} days, for "
                                  "good (never the newest of a conversation)", sum(e["bytes"] for e in old),
                                  ["gc", "--only", "--transcripts", str(storage.REMOVE_DAYS), "--home", name,
                                   "--yes"], item))
        used = seconds(item.get("last_used"))
        size = home_info.get("bytes") or 0
        if size and (full or (used is not None and used <= time.time() - storage.PACK_DAYS * 86400)):
            plan.append(plan_item("pack", "pack its HOME into one archive (unpacked again when it is resumed)"
                                  + ("" if full else f"; unused for more than {storage.PACK_DAYS} days"),
                                  size * storage.PACK_RATIO, ["gc", "--pack", "--home", name, "--yes"], item,
                                  estimate=True))
    if packed and full:
        plan.append(plan_item("unpack", "unpack its HOME now (resume does it by itself; it then takes "
                              f"{statedirs.human(packed.get('bytes') or 0)} again)", 0, ["gc", "--unpack", name], item))
    if not packed and (directory / "home").is_dir():
        found = storage.compact_home(directory / "home", storage.COMPACT_DAYS, dry_run=True)
        if found["files"] or full:
            plan.append(plan_item("compact", f"compact its {found['files']} transcript(s) older than "
                                  f"{storage.COMPACT_DAYS} days ({statedirs.human(found['bytes'])}) in place - read "
                                  "as before; safe while it runs", found["saved"],
                                  ["gc", "--only", "--compact-transcripts", str(storage.COMPACT_DAYS), "--home", name,
                                   "--yes"], item, safe=True, estimate=True, checked=True))
    if full:
        try:
            logs = project_logs(item, storage.LOG_FILE_DAYS, dry_run=True)
        except (OSError, ValueError, RuntimeError):
            logs = {"files": 0, "bytes": 0}
        plan.append(plan_item("logs", f"remove the {logs['files']} log file(s) of its project not written for "
                              f"{storage.LOG_FILE_DAYS} days (never one a running run writes)", logs["bytes"],
                              ["gc", "--only", "--logs-older-than", str(storage.LOG_FILE_DAYS), "--home", name,
                               "--yes"], item, safe=True))
    return plan


def global_plan(measured):
    """The storage check's recommendations beyond the starts (MEASURED: usage()'s answer): what plain gc frees, the
    images a rebuild left without a name, the log folders not written for half a year."""
    storage = module("storage")
    would = measured.get("would_free") or {}
    plan = []

    def part(key):
        return would.get(key) if isinstance(would.get(key), dict) else {}
    due = part("expiry").get("due") or {}
    aged = part("log_days")
    old_logs = aged.get("bytes", 0) if aged.get("found") and not (
        aged.get("removed_from") and (seconds(aged["removed_from"]) or 0) > time.time()) else 0
    pieces = [(part("leftovers").get("bytes", 0), f"the leftovers of {part('leftovers').get('count', 0)} ended "
               "start(s)"), (part("compress").get("saves", 0), "the logs of ended runs compressed"),
              (part("rootfs").get("bytes", 0), "the MCP root file systems no server uses"),
              (old_logs, f"{aged.get('found', 0)} log file(s) older than {aged.get('days')} days (log_days)"),
              (due.get("bytes", 0), f"{due.get('count', 0)} kept start(s) past their expiry, with their HOME")]
    pieces = [(value, text) for value, text in pieces if value]
    if pieces:
        plan.append(plan_item("gc", "what plain gc does: " + "; ".join(text for _, text in pieces),
                              sum(value for value, _ in pieces), ["gc"], safe=True, estimate=True,
                              checked=not due.get("bytes")))
    images = part("images")
    if images.get("bytes"):
        plan.append(plan_item("images", f"remove {images.get('count', 0)} Podman image(s) of sandbox-ai's that a "
                              "rebuild left without a name (never one a container uses)", images["bytes"],
                              ["gc", "--only", "--images"], safe=True, checked=True))
    folders = next((entry for entry in would.get("logs_older_than") or []
                    if entry.get("days") == storage.LOG_FOLDER_DAYS), None)
    if folders and folders.get("bytes"):
        plan.append(plan_item("log_folders", f"remove {folders['count']} log folder(s) nothing was written into for "
                              f"{storage.LOG_FOLDER_DAYS} days, with their reports (never one a run logs into)",
                              folders["bytes"], ["gc", "--only", "--logs-older-than", str(storage.LOG_FOLDER_DAYS)],
                              safe=True))
    return plan


def storage_check(name=None, dry_run=False):
    """sandbox-ai gc --check [--home NAME] [--dry-run]: the automatic rules applied (not with DRY_RUN or NAME), what
    sandbox-ai keeps measured, a plan of what can be freed (each: what, how much, the command, safe while starts run),
    each start's size kept in the history and its growth since the check before (not with DRY_RUN or NAME), and the
    notice above the threshold. NAME: the Storage view of that start alone - its HOME by kind, its logs and
    checkpoints, its growth, every action for it."""
    storage = module("storage")
    settings = storage.settings()
    started = time.monotonic()
    view = name is not None
    # (a view of one start applies nothing and lists no rule: the rules are of every start)
    rules = {"settings": {key: settings.get(key) for key in storage.RULES}, "actions": [], "skipped": [],
             "dry_run": True} if view else apply_rules(dry_run=dry_run, settings=settings)
    said = storage.take_unsaid(keep=dry_run or view)
    if view:
        detail = usage_detail(name)
        item = detail["starts"][0]
        entry = start_entries(prune=False).get(item["id"]) or {}
        item = {**entry, **item}
        plan = start_plan(item, full=True) if not item.get("busy") else []
        sizes = {item["id"]: (item["name"], start_size(item))}
        measured = {"detail": detail}
    else:
        measured = usage()
        items = measured["parts"]["starts"]["items"]
        found = start_entries(prune=False)
        plan = []
        for item in items:
            plan += start_plan({**(found.get(item["id"]) or {}), **item})
        plan += global_plan(measured)
        sizes = {item["id"]: (item["name"], item["bytes"]) for item in items}
    checks = storage.history()
    grown = storage.growth(sizes, checks[-1] if checks else None)
    limit = int(settings["storage_growth_gb"] * (1 << 30))  # (GB as sandbox-ai says sizes: GiB)
    grew = sorted(((entry["name"], entry["growth"]) for entry in grown.values()
                   if entry["growth"] is not None and entry["growth"] >= limit), key=lambda pair: -pair[1])
    plan.sort(key=lambda entry: (-entry["bytes"], entry["key"]))
    total = sum(entry["bytes"] for entry in plan)
    threshold = int(settings["storage_threshold_gb"] * (1 << 30))
    if not dry_run and not view:
        storage.remember(sizes)
    actions = said  # (what the rules did before, and now; kept unsaid with DRY_RUN or NAME)
    result = {"time": iso(time.time()), "dry_run": dry_run, "name": name, "settings": settings,
              "plan": plan, "total": total, "threshold": threshold, "above": total >= threshold,
              "growth_limit": limit, "grew": [{"name": n, "bytes": b} for n, b in grew],
              "starts": [{"id": key, **value} for key, value in grown.items()],
              "rules": rules, "said": actions,
              "notice": None if view else storage.notice(total, threshold, grew, actions),
              "seconds": round(time.monotonic() - started, 2)}
    if view:
        result.update(detail=measured["detail"], logs=start_logs(item), checkpoints=start_checkpoints(item),
                      compacted=storage.marked(module("sessions").session_dir(item["id"])),
                      packed=module("sessions").homepack().packed(module("sessions").session_dir(item["id"])))
    else:
        result["scan"] = measured.get("scan")
        if not dry_run:
            storage.remember_check({"time": result["time"], "total": total, "above": result["above"],
                                    "notice": result["notice"]})
    return result


def start_size(item):
    """A start's size for the history: its HOME (or its packed archive)."""
    sessions = module("sessions")
    packed = sessions.homepack().packed(sessions.session_dir(item["id"]))
    return (packed.get("archive_bytes") or 0) if packed else item.get("bytes") or 0


def start_logs(item):
    """{"folder", "bytes", "old_files", "old_bytes", "days"} of the log folder of the start's project (shared with the
    project's other starts)."""
    storage = module("storage")
    try:
        logs = project_logs(item, storage.LOG_FILE_DAYS, dry_run=True)
    except (OSError, ValueError, RuntimeError):
        return {"folder": None, "bytes": 0, "old_files": 0, "old_bytes": 0, "days": storage.LOG_FILE_DAYS}
    folder = logs["folder"]
    size = module("statedirs").Scan().size(folder) if folder and os.path.isdir(folder) else 0
    return {"folder": folder, "bytes": size, "old_files": logs["files"], "old_bytes": logs["bytes"],
            "days": storage.LOG_FILE_DAYS}


def start_checkpoints(item):
    """{"count", "bytes"} of the workspace checkpoints of the start's project (shared with its other starts)."""
    workspace = item.get("workspace")
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        return {"count": 0, "bytes": 0}
    try:
        checkpoints = module("checkpoints")
        store = checkpoints.store_root() / checkpoints.project_key(os.path.realpath(workspace))
        count = len(checkpoints.listing(project=workspace))
    except (OSError, ValueError, RuntimeError, AttributeError):
        return {"count": 0, "bytes": 0}
    return {"count": count, "bytes": module("statedirs").Scan().size(store) if store.is_dir() else 0}


def show_check(result, out=print, as_json=False, colour=None):
    """gc --check as text: the plan as a table (the biggest first, with the total), the growth, what the rules did."""
    if as_json:
        out(json.dumps(result, indent=2))
        return
    t, statedirs = module("textout"), module("statedirs")
    colour = t.colour_enabled() if colour is None else colour
    size, printable = statedirs.human, launcher().printable
    if result.get("name"):
        detail = result["detail"]["starts"][0]
        t.section(f"Storage of {printable(detail['name'])}: {size(detail['bytes'])}", out, colour,
                  f"{detail.get('state') or '?'}" + (f"; packed into {size(result['packed'].get('archive_bytes'))}"
                                                     if result.get("packed") else ""))
        kinds = ", ".join(f"{statedirs.HOME_KINDS.get(kind, kind)} {size(value)}"
                          for kind, value in sorted(detail.get("kinds", {}).items(), key=lambda pair: -pair[1]))
        out(f"  its HOME by kind: {kinds or '-'}")
        logs, checks = result.get("logs") or {}, result.get("checkpoints") or {}
        out(f"  the logs of its project: {size(logs.get('bytes'))}" + (
            f" ({logs['old_files']} file(s) not written for {logs['days']} days: {size(logs['old_bytes'])})"
            if logs.get("old_files") else ""))
        out(f"  the workspace checkpoints of its project: {checks.get('count', 0)}, {size(checks.get('bytes'))}")
        if result.get("compacted", {}).get("files"):
            out(f"  compacted transcripts: {result['compacted']['files']} ({size(result['compacted']['saved'])} "
                "saved)")
    else:
        t.section(f"What could be freed: {size(result['total'])}", out, colour,
                  f"threshold {size(result['threshold'])}; measured in {result['seconds']:g} s")
    for entry in result["starts"]:
        if entry.get("growth") is not None and result.get("name"):
            out(f"  since the check of {shown_time(entry['since'])} UTC: {'+' if entry['growth'] >= 0 else '-'}"
                f"{size(abs(entry['growth']))}")
    rows = [[str(index), ("about " if entry["estimate"] else "") + size(entry["bytes"]),
             printable(entry["start"] or "-"), "yes" if entry["safe_running"] else "stopped only",
             printable(entry["what"])] for index, entry in enumerate(result["plan"], 1)]
    t.grid(["#", "FREES", "START", "WHILE RUNNING", "WHAT"], rows, right={0, 1}, out=out, colour=colour,
           empty="nothing to free", footer=["", size(result["total"]), "", "", "Total"] if len(rows) > 1 else None)
    if rows:
        out("the command of each (run it to apply it; the app's Optimize… runs those you tick):")
        for index, entry in enumerate(result["plan"], 1):
            out(f"  {index:>2}  {printable(entry['command'])}")
    for entry in result.get("grew") or []:
        out(t.style(f"grew more than {size(result['growth_limit'])} since the last check: {printable(entry['name'])} "
                    f"(+{size(entry['bytes'])})", "warn", colour))
    rules = result.get("rules") or {}
    verb = "would" if rules.get("dry_run") else "did"
    for action in rules.get("actions") or []:
        out(f"storage rule ({verb}): {printable(module('storage').action_text(action))}")
    if not any((rules.get("settings") or {}).values()):
        out(t.style("  no storage rules are set (storage_caches_days, storage_compact_days, storage_pack_days, "
                    "storage_logs_days in your defaults)", "muted", colour))
    for entry in rules.get("skipped") or []:
        out(f"left alone: {printable(entry['name'])}: {printable(entry['reason'])}")
    if result.get("notice"):
        out(t.style(printable(result["notice"]), "warn" if result["above"] else "muted", colour))


def confirm(question, yes=False, ask=None):
    """Whether to go ahead: YES, else the human's answer to QUESTION in a terminal (ASK(prompt) in tests). ValueError
    without a terminal: what would go was shown, nothing goes without --yes."""
    if yes:
        return True
    if ask is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError("nothing was removed or packed: no terminal to ask in; repeat with --yes (or --dry-run "
                             "to only see it)")
        ask = input
    try:
        answer = ask(f"{question} [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes", "j", "ja")


def show_home_items(items, verb, out=print, colour=None):
    """KINDS removed (or that would go) of the HOMEs as a table, the biggest first, with the total."""
    t, size = module("textout"), module("statedirs").human
    printable = launcher().printable
    titles = {"build": "build output", "toolchains": "toolchain", "transcripts": "transcripts"}
    rows = [[size(item["bytes"]), printable(item["name"]), titles.get(item["kind"], item["kind"]),
             printable(item["path"]) + (f" ({item['files']} file(s))" if item.get("files") else "")
             + f"  {printable(item['what'])}"] for item in sorted(items, key=lambda item: -item["bytes"])]
    out(f"{verb} in the stopped kept HOMEs ({size(sum(item['bytes'] for item in items))}):")
    t.grid(["SIZE", "START", "KIND", "PATH"], rows, right={0}, out=out, colour=colour,
           footer=[size(sum(item["bytes"] for item in items)), "", "", "Total"] if len(rows) > 1 else None)


def leftover_pods(found, result, dry_run):
    """The leftover pods of the local Podman (liveness.leftover_pods) into RESULT["pods"], removed unless DRY_RUN; the
    pods that still run although no start of FOUND uses them into RESULT["orphaned_pods"]. Nothing without Podman."""
    liveness = module("liveness")
    in_use = {entry["pod"] for entry in found if isinstance(entry.get("pod"), str)}
    in_use |= {liveness.pod_of({"run": entry["run"]}) for entry in found
               if isinstance(entry.get("run"), str) and entry.get("state") in ("running", "paused", "orphaned")}
    orphans = {}
    records = module("net").running(orphans)
    in_use |= {liveness.pod_of(record_) for record_ in [*records.values(), *orphans.values()]}
    for directory in module("sessions").root().glob("*/session.json"):
        data = read_json(directory)
        if data is not None and isinstance(data.get("pod"), str):
            in_use_live = liveness.pod_live(data["pod"], text(data.get("podman_url")))
            if in_use_live is not False:  # running (or Podman did not say): never a leftover
                in_use.add(data["pod"])
    pods = liveness.leftover_pods(in_use)
    if pods is None:
        return
    names = [name for name, _ in pods]
    result["pods"] = names if dry_run else liveness.remove_pods(names)
    result["orphaned_pods"] = liveness.unknown_live_pods(in_use) or []


def leftover_lends(result, dry_run):
    """The folders (macOS: and the Podman volumes) of lent VPN tunnels that starts left behind which ended without
    their cleanup (killed, a reboot) into RESULT["lends"] - the runs' names - removed unless DRY_RUN (vpn.py
    lend_leftovers). A run counts as ended only when its launcher holds no lock on its folder, no run of that name is
    registered or orphaned, and Podman lists no running container in its pod; when Podman does not answer it is left
    alone."""
    liveness = module("liveness")
    orphans = {}
    live = set(module("net").running(orphans)) | set(orphans)

    def alive(run):
        if run in live:
            return True
        pod = liveness.pod_of({"run": run})
        return pod is not None and liveness.pod_live(pod) is not False
    try:
        result["lends"] = launcher().load_vpn_module().lend_leftovers(
            alive, ["podman"] if sys.platform == "darwin" else None, remove=not dry_run)
    except (OSError, ValueError) as exc:
        LOG.warning("starts", "gc: the lent VPN tunnels' leftovers were not looked at", exc)


def leftover_cairn_volumes(result, dry_run):
    """The Podman volumes of Cairn's relay (macOS) that runs which ended without their cleanup left behind into
    RESULT["cairn_volumes"] - the runs' names - removed unless DRY_RUN (liveness.leftover_cairn_volumes). A run is
    alive while it is registered or orphaned, or Podman lists a running container in its pod or does not answer."""
    liveness = module("liveness")
    orphans = {}
    live = set(module("net").running(orphans)) | set(orphans)

    def alive(run):
        if run in live:
            return True
        return liveness.pod_live(liveness.pod_of({"run": run})) is not False
    result["cairn_volumes"] = liveness.leftover_cairn_volumes(alive, remove=not dry_run)


def leftover_remote_mounts(result, dry_run):
    """The remote folders (sshfs://) that runs which ended left mounted on this host (a killed launcher) into
    RESULT["remote_mounts"] ([{"run", "source", "point", "left"}]), unmounted unless DRY_RUN (remotefs.stale: only
    of runs no longer registered whose launcher is gone; nothing in them is ever deleted)."""
    try:
        found = module("remotefs").stale(module("net").run_dirs(), dry_run=dry_run)
    except (OSError, ValueError) as exc:
        result["skipped"].append({"name": "remote folders", "reason": str(exc)})
        return
    result["remote_mounts"] = [{"run": run, "source": source, "point": point, "left": why}
                               for run, source, point, why in found]


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
            f"{shown_time(entry.get('last_used'))} UTC" + (f"; unused for more than {DEFAULT_KEEP_DAYS} days, the "
                                                         "default expiry" if entry.get("expiry") == "default" else "")
            + ")")
    for entry in result.get("expiring") or []:
        out(f"the kept start {printable(entry['name'])} (last used {shown_time(entry.get('last_used'))} UTC) will be "
            f"removed from {shown_time(entry.get('removed_from'))} UTC on")
    expiry = result.get("expiry") or {}
    if expiry.get("origin") == "default" and (result.get("expiring") or any(
            entry.get("expiry") == "default" for entry in result["removed"])):
        out(f"kept starts unused for more than {DEFAULT_KEEP_DAYS} days are removed with their HOME by gc and its daily "
            f"pass, {EXPIRY_GRACE} days after this was first said (the default: there is no keep_days in your "
            f"defaults); to keep them: resume them, or switch it off with: {EXPIRY_OFF}")
    if result["histories"]:
        out(f"{verb} the history of {', '.join(printable(name) for name in result['histories'])}")
    if result.get("pods"):
        out(f"{verb} {len(result['pods'])} Podman pod(s) of ended runs: {', '.join(map(printable, result['pods']))}")
    if result.get("lends"):
        out(f"{verb} what {len(result['lends'])} ended run(s) left of their lent VPN tunnel: "
            f"{', '.join(map(printable, result['lends']))}")
    if result.get("cairn_volumes"):
        out(f"{verb} the Cairn relay volume of {len(result['cairn_volumes'])} ended run(s): "
            f"{', '.join(map(printable, result['cairn_volumes']))}")
    for item in result.get("remote_mounts") or []:
        if item.get("left"):
            out(f"left alone: the remote folder {printable(item.get('source') or item['point'])} of the ended run "
                f"{printable(item['run'])} could not be unmounted ({printable(item['left'])})")
        else:
            out(f"{'would unmount' if result['dry_run'] else 'unmounted'} the remote folder "
                f"{printable(item.get('source') or item['point'])} of the ended run {printable(item['run'])}")
    size = module("statedirs").human
    if result.get("caches"):
        out(f"{verb} the download caches in the kept HOMEs ({size(sum(e['bytes'] for e in result['caches']))}): "
            + ", ".join(f"{printable(e['name'])} {printable(e['path'])}" for e in result["caches"][:10])
            + (f" and {len(result['caches']) - 10} more" if len(result["caches"]) > 10 else ""))
    if result.get("images") and result.get("image_level", "unnamed") == "unnamed":
        out(f"{verb} {len(result['images'])} image(s) of sandbox-ai's that a rebuild left without a name: "
            + ", ".join(map(printable, result["images"])))
    elif result.get("images"):
        gone = set(result["images"])
        show_image_plan([image for image in result.get("image_plan") or []
                         if gone & set(image["names"] or [image["id"]])], verb, out)
    if result.get("home_items"):
        show_home_items(result["home_items"], verb, out)
    for item in result.get("transcripts") or []:
        if item["files"]:
            out(f"{'would compact' if result['dry_run'] else 'compacted'} {item['files']} transcript(s) of "
                f"{printable(item['name'])} in place ({size(item['bytes'])}; "
                + ("about " if result["dry_run"] else "") + f"{size(item['saved'])} freed; read as before)")
        for error in item.get("errors", [])[:5]:
            out(f"left as it was: {printable(item['name'])}: {printable(error['path'])}: {printable(error['reason'])}")
    for item in result.get("project_logs") or []:
        out(f"{verb} {len(item['removed']) if item['removed'] else item['files']} log file(s) of the project of "
            f"{printable(item['name'])} ({size(item['bytes'])}; {printable(item['folder'])})")
    rules = result.get("rules") or {}
    for action in rules.get("actions") or []:
        out(f"storage rule: {'would ' if result['dry_run'] else ''}{printable(module('storage').action_text(action))}")
    for ident, why in (result.get("images_kept") or {}).items():
        out(f"left alone: the image {printable(ident)}: {printable(why)}")
    for item in result.get("log_folders") or []:
        project = item.get("project")
        out(f"{verb} the log folder {printable(item['name'])} ({size(item['bytes'])}; last written "
            f"{shown_time(item.get('last_written'))} UTC" + (f"; its project {printable(project)}"
                                                              + (" is gone" if item.get("project_gone") else "")
                                                              if project else "") + ")")
    for item in result.get("rootfs") or []:
        out(f"{verb} the MCP root file system {printable(item['sha256'][:12])} ({size(item['bytes'])}; no MCP server "
            "uses it: unpacked again from its checked archive when one does)")
    mcp_logs = result.get("mcp_logs") or {}
    if mcp_logs.get("rotated") or mcp_logs.get("compressed") or mcp_logs.get("approvals"):
        done = "would compress" if result["dry_run"] else "compressed"
        out(f"{done} {len(mcp_logs.get('rotated') or []) + len(mcp_logs.get('compressed') or [])} of the MCP servers' "
            "logs (their older parts, beside them)" + (f"; {verb} {mcp_logs['approvals']} decided approval(s) older "
                                                       "than 30 days" if mcp_logs.get("approvals") else ""))
    for error in (mcp_logs.get("errors") or [])[:5]:
        out(f"left as it was: {printable(error)}")
    compacted = result.get("compacted") or {}
    if compacted.get("compressed") or compacted.get("split"):
        done = "would compress" if result["dry_run"] else "compressed"
        out(f"{done} {len(compacted['compressed'])} log(s) of ended runs"
            + (f" and the older part of {len(compacted['split'])} large shared log(s)" if compacted["split"] else "")
            + f" ({size(compacted['bytes'])}" + ("" if result["dry_run"] else f"; {size(compacted['saved'])} freed")
            + ")" + ("; the rest the next time (it ran out of time)" if compacted.get("unfinished") else ""))
    if compacted.get("dropped"):
        out(f"removed {len(compacted['dropped'])} of the oldest logs (over their limit per log folder)")
    for error in (compacted.get("errors") or [])[:10]:
        out(f"left as it was: {printable(error)}")
    aged = result.get("log_days") or {}
    if aged.get("removed"):
        out(f"removed {len(aged['removed'])} log file(s) older than {aged['days']} days ({size(aged['freed'])}; "
            + ("log_days in your defaults)" if aged["origin"] == "log_days" else f"the default; switch it off: "
                                                                                f"{LOG_DAYS_OFF})"))
    elif aged.get("found"):
        when = aged.get("removed_from")
        waits = when is not None and (seconds(when) or 0) > time.time()  # the default's grace
        out(("from " + shown_time(when) + " UTC on, gc and its daily pass remove " if waits else "would remove ")
            + f"{aged['found']} log file(s) older than {aged['days']} days ({size(aged['bytes'])}; "
            + ("log_days in your defaults)" if aged["origin"] == "log_days" else
               f"the default age limit of the logs; switch it off: {LOG_DAYS_OFF})"))
    for error in (aged.get("errors") or [])[:5]:
        out(f"left as it was: {printable(error)}")
    for item in result.get("checkpoints") or []:
        if item.get("error"):
            out(f"left as it was: the checkpoints of {printable(item.get('project') or item['store'])}: "
                f"{printable(item['error'])}")
        elif item.get("removed"):
            out(f"{verb} {len(item['removed'])} checkpoint(s) of {printable(item.get('project') or item['store'])} "
                f"beyond their limits ({size(item.get('freed') or 0)}; checkpoint_days, checkpoint_count, "
                "checkpoint_gb in your defaults)")
    for pod in result.get("orphaned_pods") or []:
        out(f"left alone: the Podman pod {printable(pod)} still runs, but no start uses it (its launcher is gone); end "
            f"it with: sandbox-ai stop {printable(pod)}")
    for entry in result["skipped"]:
        out(f"left alone: {printable(entry['name'])}: {printable(entry['reason'])}")
    for directory in result["damaged"]:
        out(f"left alone: {printable(directory)} is damaged (no readable session.json); remove it by hand")
    if not (result["leftovers"] or result["removed"] or result["histories"] or result.get("pods")
            or result.get("lends") or result.get("cairn_volumes") or result.get("caches") or result.get("images")
            or result.get("home_items") or result.get("transcripts") or result.get("project_logs")
            or (result.get("rules") or {}).get("actions")
            or result.get("remote_mounts")
            or result.get("log_folders") or result.get("expiring") or result.get("rootfs")
            or result.get("checkpoints")
            or mcp_logs.get("rotated") or mcp_logs.get("compressed") or mcp_logs.get("approvals")
            or compacted.get("compressed") or compacted.get("split") or compacted.get("dropped")):
        never = (result.get("expiry") or {}).get("origin") in ("off", "invalid") and result["days"] is None
        out("nothing to remove" + (f"; kept starts never expire (keep_days 0 in your defaults): --older-than DAYS "
                                   "removes those unused for that long" if never else ""))


def usage(days=None, logs_days=None):
    """sandbox-ai gc --usage: statedirs.usage of this account's state, with what gc would free ("would_free"): the
    leftovers, the stopped kept starts unused for DAYS (else keep_days) and for 7, 30 and 90 days, the logs gc
    compresses, the log folders not written for LOGS_DAYS and 30, 90 and 180 days (gc --logs-older-than), the caches
    gc --caches empties and the images of sandbox-ai's that a rebuild left without a name."""
    statedirs = module("statedirs")
    found = start_entries(prune=False)
    live, orphans = {}, {}
    with contextlib.suppress(OSError, ValueError, RuntimeError):
        live = module("net").running(orphans)
    busy = None
    with contextlib.suppress(OSError, ValueError, RuntimeError):
        busy = [record["log_dir"] for record in registered_runs().values() if isinstance(record.get("log_dir"), str)]
    result = statedirs.usage(found, set(live) | set(orphans), busy_dirs=busy, aged_before=log_days_cutoff())
    items = result["parts"]["starts"]["items"]
    packs = module("sessions").homepack()
    for item in items:  # a HOME gc --pack packed: its archive (in the start's size), what it held
        record = packs.packed(module("sessions").session_dir(item["id"]))
        item["packed"] = {key: record.get(key) for key in ("archive", "archive_bytes", "bytes", "created")} \
            if record else None
    stopped_kept = [item for item in items if item.get("state") == "stopped" and item.get("keep")]
    leftovers = [item for item in items if item.get("state") == "stopped" and item.get("keep") is False]
    chosen = days if days is not None else keep_days()
    older = []
    for limit in sorted({7, 30, 90} | ({chosen} if chosen is not None else set())):
        cutoff = time.time() - limit * 86400
        old = [item for item in stopped_kept if (seconds(item.get("last_used")) or cutoff + 1) <= cutoff]
        older.append({"days": limit, "count": len(old), "bytes": sum(item["bytes"] for item in old),
                      "chosen": limit == chosen})
    logs = result["parts"]["logs"]
    old_logs = []
    wanted = sorted({30, 90, 180} | ({logs_days} if logs_days is not None else set()))
    try:
        found_logs = old_log_folders(wanted[0])
    except (OSError, ValueError, RuntimeError):
        found_logs = None  # which runs still run could not be told: nothing said
    for limit in wanted if found_logs is not None else []:
        cutoff = iso(time.time() - limit * 86400)
        old = [item for item in found_logs if item["last_written"] < cutoff]
        old_logs.append({"days": limit, "count": len(old), "bytes": sum(item["bytes"] for item in old),
                         "chosen": limit == logs_days, "folders": [item["name"] for item in old][:statedirs.TOP]})
    dangling = (result.get("podman") or {}).get("dangling") or {}
    groups = module("statedirs").image_groups(images_wanted()) if result.get("podman") else None
    if groups:
        result["podman"]["groups"] = groups
    # the kept starts' expiry as it is set: keep_days, the default (announced here the first time it has any), or off
    expiry_days, expiry_origin = expiry_setting()
    expiry = {"days": expiry_days, "origin": expiry_origin, "off": EXPIRY_OFF, "count": 0, "bytes": 0, "names": [],
              "noticed": None, "removed_from": None}
    if expiry_origin == "default":
        expiring = expiring_by_default([{**item, "kind": "session"} for item in items])
        noticed = expiry_noticed(expiring)
        expiry.update(count=len(expiring), bytes=sum(item["bytes"] for item in expiring),
                      names=[item["name"] for item in expiring][:statedirs.TOP],
                      noticed=iso(noticed) if noticed is not None else None,
                      removed_from=iso(noticed + EXPIRY_GRACE * 86400) if noticed is not None else None)
    elif expiry_origin == "keep_days":
        cutoff = time.time() - expiry_days * 86400
        old = [item for item in stopped_kept if (seconds(item.get("last_used")) or cutoff + 1) <= cutoff]
        expiry.update(count=len(old), bytes=sum(item["bytes"] for item in old),
                      names=[item["name"] for item in old][:statedirs.TOP])
    # what plain gc removes of the kept starts now: keep_days' expired ones, or the default's once its grace is over
    when = time.time()
    if expiry_origin == "keep_days":
        due = [item for item in stopped_kept if (seconds(item.get("last_used")) or when) <= when - expiry_days * 86400]
    elif expiry_origin == "default" and expiry.get("removed_from") and seconds(expiry["removed_from"]) <= when:
        due = expiring_by_default([{**item, "kind": "session"} for item in items], when)
    else:
        due = []
    expiry["due"] = {"count": len(due), "bytes": sum(item["bytes"] for item in due),
                     "caches": sum(sum(item["home"]["caches"].values()) for item in due)}
    for item in items:
        item["expires"], item["expires_why"] = start_expiry(item, expiry, when)
    compress = logs["compressible"] + logs["splittable"]
    compress_kinds = logs.get("compress_kinds") or {}
    cutoff = when - PACK_DAYS * 86400
    to_pack = [item for item in stopped_kept if not item.get("packed") and item["home"]["bytes"]
               and (seconds(item.get("last_used")) or when) <= cutoff]
    result["would_free"] = {
        "pack": {"days": PACK_DAYS, "count": len(to_pack), "bytes": sum(item["home"]["bytes"] for item in to_pack)},
        "expiry": expiry,
        "leftovers": {"count": len(leftovers), "bytes": sum(item["bytes"] for item in leftovers)},
        "older_than": older,
        # what compressing saves, per kind as measured on a sample of each (statedirs.compression_ratio)
        "compress": {"bytes": compress, "saves": sum(entry["saves"] for entry in compress_kinds.values()),
                     "kinds": compress_kinds},
        "logs_older_than": old_logs,
        # the logs' age limit (log_days): what plain gc removes, or from when (the default's grace)
        "log_days": expire_logs(dry_run=True),
        "rootfs": rootfs_usage(),
        "caches": {"count": sum(1 for item in stopped_kept if item["home"]["caches"]),
                   "bytes": sum(sum(item["home"]["caches"].values()) for item in stopped_kept)},
        # (what removing them frees, where Podman says it; their sizes count the layers they share)
        "images": {"count": groups["groups"]["sandbox_unnamed"]["count"],
                   "bytes": groups["groups"]["sandbox_unnamed"]["bytes"]} if groups else
        {"count": dangling.get("total", 0), "bytes": dangling.get("bytes", 0)},
        # gc --images=old and =prune: what each frees beyond the level before it
        "images_levels": {level: {"count": sum(groups["groups"][g]["count"] for g in kinds_),
                                  "bytes": sum(groups["groups"][g]["bytes"] for g in kinds_)}
                          for level, kinds_ in module("statedirs").IMAGE_LEVELS.items()} if groups else None}
    by_part = free_by_part(result["would_free"])
    result["would_free"]["total"] = {"bytes": sum(by_part.values()), "command": "sandbox-ai gc --caches --images",
                                     "by_part": by_part}
    result["summary"] = usage_summary(result)
    return result


PACK_DAYS = 30  # gc --usage names what gc --pack --older-than 30 would pack


def start_expiry(item, expiry, when=None):
    """(when, why) a start of gc --usage goes: why "keep_days" or "default" (when: an ISO time, the earliest gc removes
    it), "never" (keep_days 0, or a broken value), "leftover" (ended without keeping its HOME: the next gc), or
    "in use" (it runs, is paused or orphaned); when None but for keep_days and default."""
    when = time.time() if when is None else when
    if item.get("state") != "stopped":
        return None, "in use"
    if not item.get("keep"):
        return None, "leftover"
    origin, days = expiry.get("origin"), expiry.get("days")
    used = seconds(item.get("last_used"))
    if origin not in ("keep_days", "default") or not days or used is None:
        return None, "never"
    goes = used + days * 86400
    if origin == "default":  # never before the grace after its notice is over
        goes = max(goes, seconds(expiry.get("removed_from")) or when + EXPIRY_GRACE * 86400)
    return iso(goes), origin


def free_by_part(would):
    """What `sandbox-ai gc --caches --images` frees of each part of gc --usage (WOULD: its "would_free"), counted once:
    a start that goes is not counted again for its caches; the logs' by what compressing saves."""
    def entry(key):
        value = would.get(key)
        return value if isinstance(value, dict) else {}
    due = entry("expiry").get("due") if isinstance(entry("expiry").get("due"), dict) else {}
    starts_ = entry("leftovers").get("bytes", 0) + due.get("bytes", 0) + \
        max(0, entry("caches").get("bytes", 0) - due.get("caches", 0))
    aged = entry("log_days")
    old_logs = aged.get("bytes", 0) if aged.get("found") and not (
        aged.get("removed_from") and (seconds(aged["removed_from"]) or 0) > time.time()) else 0
    return {"starts": starts_, "logs": entry("compress").get("saves", 0) + old_logs,
            "data": entry("rootfs").get("bytes", 0),
            "podman_images": entry("images").get("bytes", 0)}


USAGE_TITLES = {"starts": "Starts (kept HOMEs)", "logs": "Logs", "mcp": "MCP servers", "history": "Histories",
                "checkpoints": "Workspace checkpoints",
                "launcher_log": "launcher.log", "state_other": "Other state", "data": "Data", "cache": "Cache",
                "config": "Configuration", "runtime": "Run directories", "podman_images": "Podman images",
                "podman_volumes": "Podman volumes", "podman_containers": "Podman containers"}


def usage_summary(result):
    """The overview of gc --usage: {"rows": [{"key", "title", "bytes", "share" (% of the whole), "reclaimable" (what
    sandbox-ai gc --caches --images frees of it), "how"}] biggest first, empty parts left out; "total" (sandbox-ai's
    folders and Podman's images, volumes and containers), "reclaimable"}."""
    size = module("statedirs").human
    parts = result.get("parts") or {}
    would = result.get("would_free") or {}
    by_part = (would.get("total") or {}).get("by_part") or free_by_part(would)
    found = [(key, (value or {}).get("bytes") or 0) for key, value in parts.items()]
    podman = result.get("podman") or {}
    for kind in ("images", "volumes", "containers"):
        if isinstance(podman.get(kind), dict) and podman[kind].get("bytes") is not None:
            found.append((f"podman_{kind}", podman[kind]["bytes"]))
    total = sum(value for _, value in found)

    def how(key):
        if key == "starts":
            older = next((entry for entry in would.get("older_than") or [] if entry.get("days") == 7), None)
            return "sandbox-ai gc --caches" + (f"; --older-than 7 frees {size(older['bytes'])}"
                                               if older and older.get("bytes") else "")
        if key == "logs":
            older = [entry for entry in would.get("logs_older_than") or [] if entry.get("count")]
            return "sandbox-ai gc (compresses ended runs' logs)" + (
                f"; --logs-older-than {older[-1]['days']} frees {size(older[-1]['bytes'])}" if older else "")
        if key == "data" and by_part.get("data"):
            return "sandbox-ai gc (MCP root file systems no server uses)"
        if key.startswith("podman_"):
            kind = key[len("podman_"):]
            unused = (podman.get(kind) or {}).get("reclaimable")
            counted = f"Podman counts {size(unused)} reclaimable" if unused else ""
            if kind == "images":
                levels = would.get("images_levels") or {}
                more = (levels.get("old") or {}).get("bytes", 0) - (levels.get("unnamed") or {}).get("bytes", 0)
                return "sandbox-ai gc --images (sandbox-ai's images a rebuild left without a name)" + (
                    f"; --images=old frees {size(more)} more" if more > 0 else "") + (
                    f"; {counted}: every image no container uses, every software's - see Podman below"
                    if counted else "")
            prune = {"volumes": "podman volume prune", "containers": "podman container prune"}[kind]
            return f"{counted}: {prune} (every software's)" if counted else "-"
        return "-"
    rows = [{"key": key, "title": USAGE_TITLES.get(key, key), "bytes": value,
             "share": round(value * 100 / total, 1) if total else 0.0, "reclaimable": by_part.get(key, 0),
             "how": how(key)} for key, value in found if value]
    rows.sort(key=lambda row: (-row["bytes"], row["key"]))
    return {"rows": rows, "total": total, "reclaimable": sum(row["reclaimable"] for row in rows)}


def home_entry(name, found):
    """The start NAME (a name or id) of FOUND (start_entries) whose HOME is kept, as (id, entry)."""
    shown = launcher().printable(str(name))
    for key in ("name", "id"):
        matches = [(ident, entry) for ident, entry in found.items() if entry.get(key) == name]
        if len(matches) > 1:
            raise ValueError(f"several starts are named {shown}; name one by its id: "
                             + ", ".join(launcher().printable(ident) for ident, _ in matches))
        if matches:
            return matches[0]
    raise ValueError(f"no start named {shown} keeps a HOME here (see: sandbox-ai list)")


def usage_detail(name=None, scan=None):
    """sandbox-ai gc --usage --detail [NAME]: what is in the HOME of the start NAME (else of every start), by kind
    (statedirs.home_detail) - each read under its start's lock (one a command starts, stops or changes meanwhile is
    named "busy" and not read), never following a link, within the time budget of one measurement. {"starts":
    [{"id", "name", "state", "keep", "last_used", "busy", and home_detail's fields}] the biggest first, "kinds",
    "scan"}."""
    statedirs, sessions = module("statedirs"), module("sessions")
    scan = scan or statedirs.Scan()
    started = time.monotonic()
    found = start_entries(prune=False)
    chosen = [home_entry(name, found)] if name else sorted(found.items())
    homes = []
    for key, entry in chosen:
        item = {"id": key, "name": entry.get("name") or key, "state": entry.get("state"), "keep": entry.get("keep"),
                "agent": entry.get("agent"), "last_used": entry.get("last_used"), "busy": None,
                "bytes": 0, "kinds": {}, "items": [], "other": [], "biggest": [], "caches": {}}
        if scan.spent():
            item["busy"] = "not measured: the measurement ran out of time"
        else:
            try:
                with sessions.session_lock(key):
                    item.update(statedirs.home_detail(scan, sessions.session_dir(key) / "home"))
            except ValueError as exc:  # another command holds it: it is being started, stopped or changed
                item["busy"] = str(exc)
        homes.append(item)
    homes.sort(key=lambda item: (-item["bytes"], item["name"]))
    kinds = {kind: sum(item["kinds"].get(kind, 0) for item in homes) for kind in statedirs.HOME_KINDS}
    return {"name": name, "starts": homes, "kinds": {kind: value for kind, value in kinds.items() if value},
            "titles": dict(statedirs.HOME_KINDS),
            "scan": {"seconds": round(time.monotonic() - started, 2), "partial": scan.partial,
                     "unreadable": scan.unreadable}}


def show_usage_detail(result, out=print, as_json=False, colour=None):
    """gc --usage --detail as text: for one start its HOME by kind and the biggest items of each; for all of them one
    row per start with its size by kind, and the total."""
    if as_json:
        out(json.dumps(result, indent=2))
        return
    statedirs, t = module("statedirs"), module("textout")
    colour = t.colour_enabled() if colour is None else colour
    size, printable = statedirs.human, launcher().printable
    titles, scan = statedirs.HOME_KINDS, result["scan"]

    def big(value):
        return t.style(size(value), "bold", colour) if (value or 0) >= 1 << 30 else size(value) if value else "-"
    homes = result["starts"]
    if scan["partial"]:
        out(t.style("partial: the measurement ran out of time; the sizes are at least these", "warn", colour))
    if scan["unreadable"]:
        out(t.style(f"{scan['unreadable']} folder(s) could not be read (not counted)", "warn", colour))
    if not result.get("name"):
        total = sum(item["bytes"] for item in homes)
        t.section(f"The HOMEs by kind: {size(total)} in {len(homes)} start(s)", out, colour,
                  f"measured in {scan['seconds']:g} s; one in depth: sandbox-ai gc --usage --detail NAME")
        order = [kind for kind in titles if result["kinds"].get(kind)] or ["other"]
        heads = {"caches": "CACHES", "toolchains": "TOOLCHAINS", "agent": "AGENT", "build": "BUILD", "other": "OTHER"}
        rows = [[printable(item["name"]), item.get("state") or "?", big(item["bytes"]),
                 *[big(item["kinds"].get(kind)) for kind in order],
                 printable(item["busy"]) if item["busy"] else next(
                     (f"{printable(entry['path'])} {size(entry['bytes'])}" for entry in sorted(
                         item["items"] + item["other"], key=lambda entry: -entry["bytes"])), "-")]
                for item in homes]
        t.grid(["NAME", "STATE", "SIZE", *[heads[kind] for kind in order], "BIGGEST"], rows,
               right=set(range(2, 3 + len(order))), out=out, colour=colour, empty="no start keeps a HOME here",
               footer=["Total", "", size(total), *[size(result["kinds"].get(kind, 0)) for kind in order], ""]
               if len(rows) > 1 else None)
        for line in t.wrap("; ".join(f"{heads[kind]}: {titles[kind]}" for kind in order), t.width() - 2):
            out(t.style("  " + line, "muted", colour))
        return
    item = homes[0]
    t.section(f"The HOME of {printable(item['name'])}: {size(item['bytes'])}", out, colour,
              f"{item.get('state') or '?'}; measured in {scan['seconds']:g} s")
    if item["busy"]:
        out(t.style(f"  not read: {printable(item['busy'])}", "warn", colour))
        return
    rows = [[titles[kind], big(value), f"{value * 100 / item['bytes']:.1f}%" if item["bytes"] else "-",
             ", ".join(f"{printable(entry['what'])} {size(entry['bytes'])}"
                       for entry in item["items"] if entry["kind"] == kind)[:200] if kind != "other" else
             ", ".join(f"{printable(entry['path'])} {size(entry['bytes'])}" for entry in item["other"][:4])]
            for kind, value in sorted(item["kinds"].items(), key=lambda pair: -pair[1])]
    t.grid(["KIND", "SIZE", "SHARE", "WHAT"], rows, right={1, 2}, out=out, colour=colour, empty="empty",
           footer=["Total", size(item["bytes"]), "100%", ""] if rows else None)
    out("")
    t.section("The biggest items", out, colour)
    entries_ = [(entry["bytes"], titles[entry["kind"]], entry["what"], entry["path"]) for entry in item["items"]]
    entries_ += [(entry["bytes"], titles["other"], ", ".join(
        f"{child['path'].rsplit('/', 1)[-1]} {size(child['bytes'])}" for child in entry.get("biggest") or []) or "-",
                  entry["path"]) for entry in item["other"]]
    entries_.sort(key=lambda row: -row[0])
    t.grid(["SIZE", "KIND", "PATH", "WHAT"], [[big(value), kind, printable(path), printable(what)]
                                              for value, kind, what, path in entries_[:15]],
           right={0}, out=out, colour=colour)
    cleared = sum(item["caches"].values())
    if cleared and item.get("state") == "stopped":
        out(t.style(f"  sandbox-ai gc --caches empties {size(cleared)} of it (the download caches the tools fill "
                    "again)", "muted", colour))


def rootfs_usage():
    """What gc would free of the MCP root file systems no server uses: {"count", "bytes"} (0 when that cannot be told)."""
    referenced = rootfs_in_use()
    found = module("statedirs").unused_rootfs(referenced) if referenced is not None else []
    return {"count": len(found), "bytes": sum(item["bytes"] for item in found)}


def show_usage(result, out=print, as_json=False, colour=None):
    """sandbox-ai gc --usage as text: the summary (one row per part: size, share, what gc frees of it and how), then
    a table per part (the starts with their HOME, the log folders, MCP servers, the other folders, Podman) and what
    gc would free, each command with its size and the total. COLOUR: None - on a terminal (textout.colour_enabled)."""
    if as_json:
        out(json.dumps(result, indent=2))
        return
    statedirs, t = module("statedirs"), module("textout")
    colour = t.colour_enabled() if colour is None else colour
    size, printable = statedirs.human, launcher().printable
    parts, scan = result["parts"], result["scan"]
    summary = result.get("summary") or usage_summary(result)

    def paint(text, kind):
        return t.style(text, kind, colour)

    def big(value, text=None):  # a size of 1 GiB or more stands out
        text = size(value) if text is None else text
        return paint(text, "bold") if (value or 0) >= 1 << 30 else text

    def free(value):
        return paint(size(value), "ok") if value else paint("-", "muted")
    podman_bytes = summary["total"] - result["total"]
    out(paint("What sandbox-ai keeps here: ", "bold") + big(summary["total"])
        + (f" ({size(result['total'])} in its folders, {size(podman_bytes)} in Podman)" if podman_bytes else "")
        + paint(f"  measured in {scan['seconds']:g} s", "muted"))
    if scan["partial"]:
        out(paint("  partial: the measurement ran out of time; the sizes are at least these", "warn"))
    if scan["unreadable"]:
        out(paint(f"  {scan['unreadable']} folder(s) could not be read (not counted)", "warn"))
    out("")
    t.section("Summary", out, colour, "biggest first; RECLAIMABLE: what sandbox-ai gc --caches --images frees")
    rows = [[row["title"], big(row["bytes"]), f"{row['share']:.1f}%", free(row["reclaimable"]), row["how"]]
            for row in summary["rows"]]
    t.grid(["CATEGORY", "SIZE", "SHARE", "RECLAIMABLE", "HOW"], rows, right={1, 2, 3}, out=out, colour=colour,
           footer=["Total", size(summary["total"]), "100%", size(summary["reclaimable"]), ""] if rows else None,
           empty="nothing kept here yet")

    starts = parts["starts"]
    out("")
    t.section(f"Starts: {size(starts['bytes'])} in {starts['count']} start(s)", out, colour, printable(starts["path"]))
    rows = []
    for item in starts["items"][:statedirs.TOP]:
        state = item.get("state") or "?"
        state_text = state + (", kept" if item.get("keep") else ", not kept" if item.get("keep") is False else "") \
            + (", packed" if item.get("packed") else "")
        state_text = paint(state_text, STATE_STYLES.get(state, "muted")) if state != "stopped" else state_text
        caches = sum((item["home"].get("caches") or {}).values())
        why, goes = item.get("expires_why"), item.get("expires")
        if why == "leftover":
            expires = paint("next gc", "warn")
        elif goes:
            soon = (seconds(goes) or 0) - time.time() < EXPIRY_GRACE * 86400
            expires = paint(shown_time(goes)[:10], "warn") if soon else shown_time(goes)[:10]
        else:
            expires = "never" if why == "never" else "-"
        biggest = ", ".join(f"{printable(entry['path'])} {size(entry['bytes'])}"
                            for entry in item["home"]["biggest"][:3])
        rows.append([printable(item["name"]), state_text, big(item["bytes"]), size(caches) if caches else "-",
                     t.ago(item.get("last_used")), expires, biggest or "-"])
    rest = starts["items"][statedirs.TOP:]
    t.grid(["NAME", "STATE", "SIZE", "CACHES", "LAST USED", "EXPIRES", "BIGGEST IN HOME"], rows, right={2, 3},
           out=out, colour=colour, empty="no start kept",
           footer=[f"and {len(rest)} more", size(sum(i["bytes"] for i in rest)), "", "", "",
                   "all of them: --json"] if rest else None)
    if rows:
        out(paint("  what in the HOMEs is big, by kind: sandbox-ai gc --usage --detail [NAME]", "muted"))

    logs = parts["logs"]
    out("")
    t.section(f"Logs: {size(logs['bytes'])} in {logs['count']} log folder(s)", out, colour, printable(logs["path"]))
    if logs["kinds"]:
        out("  by kind: " + ", ".join(f"{printable(kind)} {size(value)}"
                                      for kind, value in list(logs["kinds"].items())[:8]))
    rows = [[printable(item["name"]), big(item["bytes"]), str(item["runs"]), t.ago(item.get("last_written")),
             size(item["compressible"]) if item["compressible"] else "-",
             ", ".join(f"{printable(kind)} {size(value['bytes'])}" for kind, value in list(item["kinds"].items())[:3])]
            for item in logs["items"][:statedirs.TOP]]
    rest = logs["items"][statedirs.TOP:]
    t.grid(["FOLDER", "SIZE", "RUNS", "LAST WRITTEN", "COMPRESSIBLE", "BIGGEST KINDS"], rows, right={1, 2, 4}, out=out,
           colour=colour, empty="no logs",
           footer=[f"and {len(rest)} more", size(sum(i["bytes"] for i in rest)), "", "", "", ""] if rest else None)

    mcp_part = parts.get("mcp") or {}
    if mcp_part.get("count"):
        out("")
        t.section(f"MCP servers: {size(mcp_part['bytes'])} in {mcp_part['count']} server(s)", out, colour,
                  printable(mcp_part["path"]))
        t.grid(["SERVER", "SIZE"], [[printable(i["name"]), big(i["bytes"])] for i in mcp_part["items"]], right={1},
               out=out, colour=colour)

    out("")
    t.section("Other folders", out, colour)
    others = sorted((parts[key] | {"key": key} for key in ("data", "cache", "config", "runtime", "state_other",
                                                           "launcher_log", "history", "checkpoints")
                     if (parts.get(key) or {}).get("bytes")),
                    key=lambda part: -part["bytes"])
    rows = [[USAGE_TITLES[part["key"]], big(part["bytes"]),
             ", ".join(f"{printable(i['name'])} {size(i['bytes'])}" for i in (part.get("items") or [])[:4]) or "-"]
            for part in others]
    t.grid(["PART", "SIZE", "BIGGEST"], rows, right={1}, out=out, colour=colour)

    podman = result.get("podman")
    out("")
    if podman:
        t.section("Podman", out, colour, "podman system df: every software's, not only sandbox-ai's")
        rows = [[kind, str(value.get("total") if value.get("total") is not None else "-"),
                 str(value.get("active") if value.get("active") is not None else "-"), big(value["bytes"]),
                 size(value["reclaimable"]) if value.get("reclaimable") is not None else "-"]
                for kind, value in podman.items()
                if kind in ("images", "containers", "volumes") and isinstance(value, dict)
                and value.get("bytes") is not None]
        t.grid(["KIND", "COUNT", "IN USE", "SIZE", "RECLAIMABLE"], rows, right={1, 2, 3, 4}, out=out, colour=colour,
               empty="no sizes said")
        groups = podman.get("groups")
        if groups and groups.get("count"):
            out("")
            t.section("Podman images by what they are to sandbox-ai", out, colour,
                      "SIZE: what removing them frees" if groups.get("unique_sizes") else
                      "SIZE: their sizes, layers they share counted in each")
            titles = statedirs.IMAGE_GROUPS
            rows = [[titles[key][0], str(value["count"]), big(value["bytes"]),
                     paint(titles[key][1], "ok") if key in statedirs.IMAGE_LEVELS["prune"] else titles[key][1]]
                    for key, value in sorted(groups["groups"].items(), key=lambda kv: -kv[1]["bytes"])
                    if value["count"]]
            t.grid(["GROUP", "COUNT", "SIZE", "HOW"], rows, right={1, 2}, out=out, colour=colour,
                   footer=["Total", str(groups["count"]), size(groups["bytes"]), ""])
    else:
        t.section("Podman", out, colour, "not asked (no podman, or it did not answer)")

    would = result["would_free"]
    out("")
    t.section("What gc would free", out, colour, "nothing goes before you run the command")
    rows = []
    if would["leftovers"]["count"]:
        rows.append((would["leftovers"]["bytes"], "sandbox-ai gc",
                     f"the leftovers of {would['leftovers']['count']} ended start(s)"))
    if would["compress"]["bytes"]:
        ratios = ", ".join(f"{printable(kind)} {entry['ratio']:.1f}x" for kind, entry in sorted(
            (would["compress"].get("kinds") or {}).items(), key=lambda item: -item[1]["bytes"])[:4])
        rows.append((would["compress"].get("saves", would["compress"]["bytes"]), "sandbox-ai gc",
                     f"about this much by compressing {size(would['compress']['bytes'])} of logs (ended runs' logs and "
                     "older parts; how much each kind shrinks, measured on a sample" + (f": {ratios}" if ratios else "")
                     + ")"))
    expiry = would.get("expiry") or {}
    due = expiry.get("due") or {}
    if expiry.get("origin") == "default" and expiry.get("count"):
        rows.append((due.get("bytes", 0), "sandbox-ai gc",
                     f"{expiry['count']} stopped kept start(s) unused for more than {DEFAULT_KEEP_DAYS} days, with "
                     "their HOME (the default expiry"
                     + (f"; from {shown_time(expiry['removed_from'])} UTC on" if expiry.get("removed_from") else "")
                     + (f": {', '.join(map(printable, expiry['names'][:5]))}" if expiry["names"] else "")
                     + f"; switch it off: {EXPIRY_OFF})"))
    elif expiry.get("origin") == "keep_days" and expiry.get("count"):
        rows.append((expiry["bytes"], "sandbox-ai gc", f"{expiry['count']} stopped kept start(s) unused for more than "
                     f"{expiry['days']} days (keep_days), with their HOME; every start does it too"))
    aged = would.get("log_days") or {}
    if aged.get("found"):
        waits = aged.get("removed_from") and (seconds(aged["removed_from"]) or 0) > time.time()
        rows.append((aged["bytes"], "sandbox-ai gc",
                     f"{aged['found']} log file(s) older than {aged['days']} days ("
                     + ("log_days" if aged["origin"] == "log_days" else
                        "the default age limit" + (f"; from {shown_time(aged['removed_from'])} UTC on" if waits else "")
                        + f"; switch it off: {LOG_DAYS_OFF}") + "); the daily pass does it too"))
    if (would.get("rootfs") or {}).get("count"):
        rows.append((would["rootfs"]["bytes"], "sandbox-ai gc",
                     f"{would['rootfs']['count']} MCP root file system(s) no server uses"))
    if would["caches"]["bytes"]:
        rows.append((would["caches"]["bytes"], "sandbox-ai gc --caches",
                     f"the download caches (pip, uv, npm ...) in {would['caches']['count']} stopped kept HOME(s); the "
                     "tools fill them again"))
    if would["images"]["count"]:
        rows.append((would["images"]["bytes"], "sandbox-ai gc --images",
                     f"{would['images']['count']} image(s) of sandbox-ai's that a rebuild left without a name"))
    rows.sort(key=lambda row: -row[0])
    choices = [(entry["bytes"], f"sandbox-ai gc --older-than {entry['days']}",
                f"or: {entry['count']} stopped kept start(s) unused for that long, with their HOME"
                + (" (the days asked for, or keep_days)" if entry.get("chosen") else ""))
               for entry in would["older_than"] if entry["count"]]
    levels = would.get("images_levels") or {}
    for level, what in (("old", "also sandbox-ai's images no kept start uses (built again when needed)"),
                        ("prune", "also every software's images without a name (as podman image prune)")):
        entry = levels.get(level) or {}
        if entry.get("count") and entry["count"] > (levels.get("unnamed") or {}).get("count", 0):
            choices.append((entry["bytes"], f"sandbox-ai gc --images={level}",
                            f"or: {entry['count']} image(s): {what}; listed and asked first"))
    pack = would.get("pack") or {}
    if pack.get("count"):
        choices.append((pack["bytes"], f"sandbox-ai gc --pack --older-than {pack['days']}",
                        f"or: pack {pack['count']} stopped kept HOME(s) unused for that long into an archive each - "
                        "most of this is freed, they are unpacked when resumed"))
    choices += [(entry["bytes"], f"sandbox-ai gc --logs-older-than {entry['days']}",
                 f"or: {entry['count']} log folder(s) nothing was written into for that long"
                 + (f": {', '.join(map(printable, entry['folders'][:3]))}" + (" ..." if entry["count"] > 3 else "")))
                for entry in would.get("logs_older_than") or [] if entry["count"]]
    total = would.get("total") or {"bytes": sum(row[0] for row in rows), "command": "sandbox-ai gc --caches --images"}
    t.grid(["SIZE", "COMMAND", "WHAT"], [[free(value), command, what] for value, command, what in rows + choices],
           right={0}, out=out, colour=colour, empty="nothing: all is in use or kept",
           footer=[size(total["bytes"]), total["command"], "Total: all of the above but the \"or:\" choices"]
           if rows else None)
    if expiry.get("origin") == "off":
        out(paint("  kept starts never expire (keep_days 0 in your defaults)", "muted"))
    if aged.get("origin") in ("default", "log_days"):
        out(paint(f"  logs older than {aged['days']} days are removed by gc and its daily pass ("
                  + ("log_days in your defaults" if aged["origin"] == "log_days" else
                     f"the default; never: {LOG_DAYS_OFF}") + ")", "muted"))
    elif aged.get("origin") == "off":
        out(paint("  logs are kept until their size limits (log_days 0 in your defaults)", "muted"))


def shown_time(value):
    """An ISO time of a record (UTC) as "YYYY-MM-DD HH:MM", or "-"."""
    if not isinstance(value, str) or len(value) < 16:
        return "-"
    return value[:16].replace("T", " ")


HEADERS = ["NAME", "KIND", "AGENT", "STATE", "STARTED", "HOME", "WORKSPACE"]
EMPTY = "Nothing runs and no start is kept. Start one with: sandbox-ai claude (or another agent: sandbox-ai --help)"
EMPTY_FILTERED = "Nothing runs. Start one with: sandbox-ai claude (or another agent: sandbox-ai --help)"
# A start with --ask allow: after its state (never a question, every one allowed; the app and the web warn alike).
ASK_ALLOW_STATE = "asks nothing, allows every question"  # (the app's badge says the same)
STATE_STYLES = {"running": "ok", "paused": "warn", "orphaned": "bad", "damaged": "bad"}  # any other state (stopped, ended): muted
KIND_STYLES = {"session": "accent", "run": "blue", "mcp": "magenta"}
HELPER_PREFIX = "mcp-helper-"  # an MCP server that is another agent, a lead's helper (agent_sandbox.HELPER_PREFIX)
TREE = ("├─ ", "└─ ")
TREE_ASCII = ("|- ", "`- ")


def colour_choice(when, stream=None):
    """Whether list colours its table: WHEN is "always", "never" (--no-color) or "auto"/None: only on a terminal, and
    not with NO_COLOR set or TERM=dumb (FORCE_COLOR forces it; textout.colour_enabled)."""
    if when in ("always", "never"):
        return when == "always"
    return module("textout").colour_enabled(stream or sys.stdout)


def tree_marks(stream=None):
    """The tree markers of the grouped listing: box drawing where the output can carry it, else ASCII."""
    encoding = str(getattr(stream or sys.stdout, "encoding", "") or "").lower().replace("-", "")
    return TREE if encoding in ("utf8", "utf_8") else TREE_ASCII


def kind_style(entry):
    """The colour of a row's NAME and KIND: sessions cyan, runs blue, MCP servers magenta, helpers violet."""
    if entry.get("kind") == "mcp" and str(entry.get("name", "")).startswith(HELPER_PREFIX):
        return "violet"
    return KIND_STYLES.get(entry.get("kind"), "muted")


def row(entry, colour, mark="", under=None):
    """One row of the table for ENTRY: MARK before its name (the tree marker of a row below another), UNDER the name
    of the start it is shown below (its other users are named instead of all of them)."""
    t = module("textout")
    printable = module("sessions").printable  # the launcher's (a listing loads no launcher)
    serves = entry.get("serves") or []
    if under is None:
        where = entry.get("workspace") or ("serves " + ", ".join(serves) if serves else "-")
    else:
        others = [name for name in serves if name != under]
        also = "also serves " + ", ".join(others) if others else ""
        where = entry.get("workspace") or also or "-"
        where = f"{where}  ({also})" if entry.get("workspace") and also else where
    if services_text(entry):
        where = f"{where}  [{services_text(entry)}]"
    return [t.style(mark, "muted", colour) + t.style(printable(entry["name"]), kind_style(entry), colour),
            t.style(entry["kind"], kind_style(entry), colour),
            printable(entry.get("agent") or "-"),
            t.style(entry["state"] + (", expired" if entry.get("expired") else "")
                    + {"starting": " (starting)", "missing": " (no terminal: see its log)",
                       "ended": " (no agent: its tmux session ended; see its log)"}.get(entry.get("terminal"), ""),
                    STATE_STYLES.get(entry["state"], "muted") if entry.get("terminal") not in ("missing", "ended")
                    else "bad", colour)
            + (t.style(f", {ASK_ALLOW_STATE}", "bad", colour) if entry.get("ask") == "allow" else ""),
            module("textout").ago(entry.get("started")),
            entry.get("home") or "-",
            printable(module("textout").home_short(where))]


def grouped(found, colour, marks=TREE):
    """The rows of the grouped listing: each start (in the listed order) with what belongs to it below it (parents():
    the MCP servers and helpers it started or uses, a helper's own below its lead), then the MCP servers no listed
    start uses. A server several starts use is shown once, below the one it was started for (else the first), and
    names the others ("also serves ...")."""
    below = parents(found)
    children = {}
    for index in range(len(found)):
        if index in below:
            children.setdefault(below[index], []).append(index)
    tops = sorted((index for index in range(len(found)) if index not in below), key=lambda i: newest_first(found[i]))
    tops = [i for i in tops if found[i].get("kind") != "mcp"] + [i for i in tops if found[i].get("kind") == "mcp"]
    rows = []
    for index in tops:
        rows.append(row(found[index], colour))
        mine = children.get(index, [])
        for position, child in enumerate(mine):
            rows.append(row(found[child], colour, marks[position == len(mine) - 1], found[index]["name"]))
    return rows


def newest_first(entry):
    """The order of the table's rows: running first, then paused, stopped ...; the newest start first in each."""
    return (STATE_ORDER.get(entry.get("state"), 9), -(module("textout").utc_seconds(entry.get("started")) or 0),
            str(entry.get("name")))


LIVE_STATES = ("running", "paused", "orphaned", "damaged")  # shown by default (damaged: it needs a look)


def is_helper(entry):
    return entry.get("kind") == "mcp" and str(entry.get("name", "")).startswith(HELPER_PREFIX)


def shown_filter(found, stopped=False, mcp=False):
    """(what `list` shows, what it leaves out) of FOUND: by default what runs (as `docker ps`) - the starts that run,
    are paused, orphaned or damaged, with their helpers; STOPPED adds the stopped and ended starts, MCP the MCP
    server sandboxes (a helper is its lead's, shown with it)."""
    shown_, hidden = [], []
    for entry in found:
        if entry.get("kind") == "mcp" and not is_helper(entry):
            keep = mcp
        else:
            keep = stopped or entry.get("state") in LIVE_STATES
        (shown_ if keep else hidden).append(entry)
    return shown_, hidden


def hidden_text(hidden):
    """One line: what is not shown and the option that shows it; None when nothing is left out."""
    starts = sum(1 for entry in hidden if entry.get("kind") != "mcp" or is_helper(entry))
    servers = len(hidden) - starts
    parts = ([f"{starts} stopped start{'s' if starts != 1 else ''} (--stopped)"] if starts else []) + \
        ([f"{servers} MCP server{'s' if servers != 1 else ''} (--mcp)"] if servers else [])
    return f"Not shown: {', '.join(parts)}; everything: --all" if parts else None


def show(found, out=print, as_json=False, colour=None, flat=False, marks=None, trunc=True, hidden=None):
    """The table of FOUND (entries()), or JSON (AS_JSON: never coloured, in the order of FOUND). COLOUR: True, False,
    or None for colour_choice("auto"). FLAT: one row per entry; else grouped() (MARKS: the tree markers, default
    tree_marks()). Running first, the newest first (newest_first). TRUNC: the workspace cut to the terminal's width
    (its end kept, "…/app"); False (--no-trunc): whole, wrapped."""
    if as_json:
        out(json.dumps(found, indent=2))
        return
    colour = colour_choice("auto") if colour is None else colour
    rows = [row(entry, colour) for entry in sorted(found, key=newest_first)] if flat else \
        grouped(found, colour, marks or tree_marks())
    # The workspace last: it is the longest (cut to the terminal's width, or wrapped there with --no-trunc).
    module("textout").table(HEADERS, rows, out, indent=0, colour=colour,
                            empty=EMPTY_FILTERED if hidden else EMPTY, truncate="left" if trunc else None)
    # An orphaned start: why (its supervisor's end, as recorded) and what to do, below the table.
    t, printable = module("textout"), module("sessions").printable
    for entry in found:
        if entry.get("state") == "orphaned" and entry.get("orphaned_why"):
            out(t.style(f"{printable(entry['name'])} is orphaned: ", "bad", colour) + printable(entry["orphaned_why"]))
        elif entry.get("state") == "running" and entry.get("terminal") in ("missing", "ended"):
            # "(no terminal: see its log)" in its row: which log, and what to do
            shown = printable(entry["name"])
            log = f"{printable(str(entry['directory']))}/supervisor.log" if entry.get("directory") else "its log"
            what = "without a terminal to attach to" if entry["terminal"] == "missing" else \
                "without an agent (its tmux session ended)"
            out(t.style(f"{shown} runs {what}", "bad", colour) + f"; its log: {log}; end it with: sandbox-ai stop "
                f"{shown}, then start it again: sandbox-ai resume {shown}")
    said = hidden_text(hidden or [])
    if said:
        out(t.style(said, "muted", colour))


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
    if event == "restore-home" and isinstance(entry.get("paths"), list):
        return "put back: " + ", ".join(printable(str(path)) for path in entry["paths"])
    return printable(str(entry.get("note") or ""))


def show_history(name, out=print, as_json=False, colour=None):
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
              event_details(e)] for e in found], out, indent=0, colour=colour,
            empty=f"no history of {printable(str(name))}")
    return 0


LIST_COMMANDS = ("list", "ps")  # ps: another name for list, with its own parser (its usage and help name it)
LIST_HELP = """What runs, as docker ps shows it: the starts that run, are paused or orphaned, with their helpers.
--stopped adds the stopped starts (their HOME kept, to resume), --mcp the running MCP server sandboxes, --all
everything (also the MCP servers that do not run). A line below the table says what is not shown. --json lists
everything, as before.

Grouped by default: each start with the MCP servers and helpers that belong to it below it (├─, └─): the servers
it was started with or uses, a helper below its lead. A server several starts use is shown once, below the start it
was started for (else the first that uses it), and names the others ("also serves ..."). The MCP servers no listed
start uses follow the starts. --flat lists every row on its own. Running starts first, the newest first;
STARTED says how long ago ("2 hours ago"), WORKSPACE writes your HOME as ~ and is cut to the terminal's width
from the left ("…/src/app"; whole with --no-trunc). --json is unchanged: every field, full values, UTC times.

Colour on a terminal (not with NO_COLOR set or TERM=dumb; --color=always forces it, --no-color turns it off):
KIND and NAME by kind (session cyan, run blue, MCP server magenta, helper violet), STATE by state (running green,
paused yellow, damaged red, stopped grey). A start with --ask allow says after its state, in red, that it asks
nothing and allows every question ("ask" in --json: on, off or allow).

Every user's starts (root only): --all-users lists them with a USER column first ("user", "uid" in --json), the
same filters; --user USER one user's. Each user's state is read as data (never through a link they planted), their
Podman asked as them; a user not logged in (no /run/user/UID) shows "unknown (not logged in)". On one of them, as
that user with root's own copy: sandbox-ai stop|logs|report USER:NAME, list --history USER:NAME."""


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:4]))
    commands = p.add_subparsers(dest="command", required=True)
    for command in LIST_COMMANDS:
        listing = commands.add_parser(command, help="what runs and what is kept" if command == "list"
                                      else "the same as list", description=LIST_HELP,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
        listing.add_argument("-a", "--all", action="store_true",
                             help="everything: the stopped starts and every MCP server sandbox, also those that do "
                                  "not run")
        listing.add_argument("-s", "--stopped", action="store_true",
                             help="also the stopped starts (their HOME kept, to resume)")
        listing.add_argument("-m", "--mcp", action="store_true", help="also the running MCP server sandboxes")
        listing.add_argument("-H", "--history", metavar="NAME", help="what happened to NAME instead")
        listing.add_argument("--folders", action="store_true",
                             help="the host folders of the starts instead: each running start's workspaces and HOME, "
                                  "each kept start's (what the desktop app's Files page offers)")
        listing.add_argument("-j", "--json", action="store_true",
                             help="as JSON, for scripts and the desktop app (never coloured)")
        listing.add_argument("-f", "--flat", action="store_true",
                             help="one row per start and MCP server in start order, nothing grouped below a start")
        listing.add_argument("--color", dest="color", choices=["auto", "always", "never"], default="auto",
                             metavar="WHEN",
                             help="colour the table: auto (the default: on a terminal, not with NO_COLOR set), always or "
                                  "never")
        listing.add_argument("-nc", "--no-color", dest="color", action="store_const", const="never",
                             help="without colour (the same as --color=never)")
        listing.add_argument("--no-trunc", "--wide", dest="no_trunc", action="store_true",
                             help="nothing shortened: a long workspace wraps instead of being cut to the "
                                  "terminal's width with …")
        listing.add_argument("--all-users", action="store_true",
                             help="root only: every user's starts on this host, with a USER column (each user's "
                                  "state read as data, their Podman asked as them; the same filters)")
        listing.add_argument("--user", metavar="USER",
                             help="root only: USER's starts alone (as --all-users); with --history NAME: that "
                                  "history, read as USER (also --history USER:NAME)")
    renamed = commands.add_parser("rename", help="another name for a start (its run id stays)")
    renamed.add_argument("old", metavar="OLD")
    renamed.add_argument("new", metavar="NEW", nargs="?", help="its new name (default: a fresh codename)")
    renamed.add_argument("-c", "--codename", action="store_true",
                         help="a fresh codename such as brisk-otter-lantern (the same as giving no NEW; a NEW "
                              "given wins)")
    collect = commands.add_parser("gc", help="remove leftovers, and kept starts unused for --older-than DAYS")
    collect.add_argument("-o", "--older-than", type=int, metavar="DAYS",
                         help="also the stopped kept starts (with their HOME) unused for more than DAYS days, and old "
                              "histories (default: keep_days of defaults.json, else none)")
    collect.add_argument("-n", "--dry-run", action="store_true", help="say what would go, remove nothing")
    collect.add_argument("-j", "--json", action="store_true", help="as JSON")
    collect.add_argument("--caches", action="store_true",
                         help="also empty the download caches (pip, uv, npm, Go, Yarn, Bun, pnpm) in the HOMEs of the "
                              "stopped kept starts; the tools fill them again when needed")
    collect.add_argument("--images", nargs="?", const="unnamed", choices=["unnamed", "old", "prune"],
                         metavar="LEVEL",
                         help="also remove Podman images, never one a container uses: unnamed (the default) "
                              "sandbox-ai's that a rebuild left without a name; old: also sandbox-ai's that no kept "
                              "start uses (other agents, variants; built again when needed); prune: also every "
                              "software's images without a name, as podman image prune. old and prune are listed "
                              "first and asked (or --yes)")
    collect.add_argument("--build-output", action="store_true",
                         help="also remove the build output and temp folders (node_modules, Rust target/, virtualenvs, "
                              "__pycache__, tmp/ ...) in the HOMEs of the stopped kept starts; shown first and asked "
                              "(or --yes)")
    collect.add_argument("--toolchains", action="store_true",
                         help="also remove the toolchains (rustup, nvm, SDKMAN, pyenv, uv's Pythons ...) in the HOMEs "
                              "of the stopped kept starts - their tools install them again; shown first and asked "
                              "(or --yes)")
    collect.add_argument("--transcripts", type=int, metavar="DAYS",
                         help="also remove the agents' transcripts (Claude, Codex) older than DAYS days in the HOMEs "
                              "of the stopped kept starts - never the newest of a conversation folder, the one resume "
                              "continues; shown first and asked (or --yes)")
    collect.add_argument("--compact-transcripts", type=int, metavar="DAYS",
                         help="also compress the agents' transcripts (Claude, Codex, Pi) older than DAYS days in place "
                              "(gzip, FILE.jsonl.gz; read as before by session files, export and resume) - never the "
                              "newest of a conversation, each read back and compared before the original goes; also "
                              "of a running start (its agent only appends to the newest); shown first and asked (or "
                              "--yes)")
    collect.add_argument("--only", action="store_true",
                         help="nothing but what --caches, --build-output, --toolchains, --transcripts, "
                              "--compact-transcripts, --images and --logs-older-than name: no leftovers, no expiry, no "
                              "log compression, no storage rules (what the storage check's plan runs)")
    collect.add_argument("--check", action="store_true",
                         help="the storage check: apply the storage rules of your defaults, measure, and list what "
                              "could be freed - each with how much, the exact command and whether it is safe while "
                              "starts run - and each start's growth since the last check (kept in the state folder); "
                              "with --home NAME: that start's storage alone (nothing applied or recorded); --dry-run: "
                              "nothing applied or recorded")
    collect.add_argument("--pack", action="store_true",
                         help="only pack the HOMEs of the stopped kept starts (with --older-than DAYS: those unused "
                              "for that long) into a compressed archive beside them, unpacked again when they are resumed; "
                              "each written, read back and compared before the HOME goes; shown first and asked (or "
                              "--yes)")
    collect.add_argument("--unpack", metavar="NAME",
                         help="only unpack the packed HOME of NAME now (resume does it by itself)")
    collect.add_argument("--home", action="append", default=[], metavar="NAME",
                         help="with --caches, --build-output, --toolchains, --transcripts, --compact-transcripts or "
                              "--pack: only this start's HOME (repeatable); with --logs-older-than: the log files of "
                              "its project instead of whole log folders; with --check: that start's storage")
    collect.add_argument("-y", "--yes", action="store_true",
                         help="with --build-output, --toolchains, --transcripts, --compact-transcripts, --caches, "
                              "--logs-older-than or --pack: without asking")
    collect.add_argument("--logs-older-than", type=int, metavar="DAYS",
                         help="also remove the log folders (logs/PROJECT-HASH) nothing was written into for more than "
                              "DAYS days - projects that are gone, or not used for that long - with their reports; "
                              "never one a registered run logs into")
    collect.add_argument("--no-compact", action="store_true",
                         help="leave the logs as they are (by default gc compresses the logs of runs that ended and "
                              "splits large shared logs)")
    collect.add_argument("--color", dest="color", choices=["auto", "always", "never"], default="auto", metavar="WHEN",
                         help="with --usage: colour its tables: auto (the default: on a terminal, not with NO_COLOR "
                              "set), always or never")
    collect.add_argument("--no-color", dest="color", action="store_const", const="never",
                         help="with --usage: without colour (the same as --color=never)")
    collect.add_argument("--detail", nargs="?", const="", metavar="NAME",
                         help="with --usage: what is in the kept HOMEs by kind - download caches, toolchains, agent "
                              "state (transcripts ...), build output and temp, the agent's other work; with NAME: "
                              "that start's HOME in depth")
    collect.add_argument("-u", "--usage", action="store_true",
                         help="remove nothing: how big each part of what sandbox-ai keeps here is (starts and their "
                              "HOMEs, the logs of each project, MCP servers, Podman's images ...), the biggest items, "
                              "and what gc would free")
    for command, text_ in (("attach", "this terminal into a running start (detach again: Ctrl-b d)"),
                           ("pause", "freeze every process of a start"),
                           ("resume", "thaw a paused start, or bring a stopped one back"),
                           ("stop", "end a start; what it keeps stays"),
                           ("delete", "remove a stopped start with its kept HOME")):
        sub = commands.add_parser(command, help=text_, allow_abbrev=False)
        sub.add_argument("name", metavar="NAME", **({"nargs": "?"} if command == "stop" else {}))
        if command == "stop":
            sub.add_argument("-w", "--wait", type=float, metavar="SECONDS",
                             help="how long to wait for it (each of them) to end (default 30)")
            sub.add_argument("-a", "--all", action="store_true",
                             help="every start of yours on this host that runs, is paused or orphaned (sessions, "
                                  "runs, tasks; not the MCP servers), one after another; asked first with the list")
            sub.add_argument("-y", "--yes", action="store_true", help="with --all: without asking")
            sub.add_argument("--user", metavar="USER",
                             help="root only: stop USER's start NAME (also NAME as USER:NAME), run as USER with "
                                  "root's own sandbox-ai copy (sandbox-ai list --all-users shows them)")
        if command == "delete":
            sub.add_argument("-y", "--yes", action="store_true", help="confirm the removal of its HOME")
        if command == "attach":
            shells = commands.add_parser(
                "shell", allow_abbrev=False, usage="sandbox-ai shell [-h] [-n] NAME [-- COMMAND ...]",
                help="a shell of yours in a running start, under the agent's rules (or one COMMAND)",
                description="Open a shell in the running start NAME - or run COMMAND there, its exit status "
                            "passed on - under exactly the agent's rules: the same user, /work, environment, "
                            "firewall, proxy and CA, no capability, the start's mounts only, as the agent's own "
                            "tmux window \"shell\". Only a start in tmux (the ones attach reaches) has one. Recorded "
                            "in its activity timeline. Another host's: sandbox-ai hosts run HOST -- shell NAME.")
            shells.add_argument("name", metavar="NAME")
            shells.add_argument("-n", "--no-tty", action="store_true",
                                help="without a terminal there, for scripts: input to its end, output and error "
                                     "output apart (the default when this one has no terminal)")
        if command == "resume":
            sub.add_argument("-r", "--review-home", action="store_true",
                             help="show what changed in its HOME's start files since its last start; start nothing")
            sub.add_argument("--restore-home", action="store_true",
                             help="put back the text its HOME's start files had at its last start, as recorded "
                                  "(small text files only, never keys or tokens; links are never followed): asked "
                                  "file by file, or --yes; start nothing")
            sub.add_argument("--path", action="append", default=[], metavar="PATH",
                             help="with --restore-home: only this file (relative to the HOME; repeatable)")
            sub.add_argument("--yes", action="store_true", help="with --restore-home: put every file back unasked")
            sub.add_argument("-j", "--json", action="store_true", help="with --review-home or --restore-home: as JSON")
            sub.add_argument("-w", "--workspace", action="append", default=[], metavar="DIR",
                             help="start it with this directory added (at /workspaces/<name>; repeatable), kept for "
                                  "its later starts")
            sub.add_argument("-m", "--mount", action="append", default=[], metavar="SOURCE:DESTINATION[:ro|rw]",
                             help="start it with this mount added (read-only without :rw; repeatable), kept for "
                                  "its later starts")
            sub.add_argument("-M", "--mcp", action="append", default=[], metavar="NAME",
                             help="start it with this MCP server connected (repeatable; sandbox-ai mcp list), kept "
                                  "for its later starts")
            sub.add_argument("--restart", action="store_true",
                             help="a running start is stopped first (the agent continues its conversation)")
            sub.add_argument("--drop-option", action="append", default=[], metavar="OPTION[=VALUE]",
                             help="take an option out of its saved options: every value of a list option "
                                  "(--allow-host), or the one VALUE (--drop-option allow-host=example.org); a single "
                                  "one goes back to its default (repeatable)")
            sub.epilog = RESUME_OPTIONS
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
    return p


RESUME_OPTIONS = """\
Any option of the launcher after NAME changes the start's saved options at this rebirth and for its later ones
(sandbox-ai resume NAME --gui --cpus 4 --allow-host example.org): an option with one value (--cpus, --memory, --gui,
--mode, --canaries ...) replaces the saved one, a list option (--allow-host, --env, --allow-pypi ...) adds its
value; --drop-option takes one out. The start gets the checks of a new start first (the administrator's policy,
the security mode, private locations, its project file's trust): refused, nothing changes. Not changed by a
rebirth: its name, HOME and backend, --config, and the workspaces (--workspace adds one)."""


def launcher_long_forms(argv):
    """ARGV of `resume NAME ...` with each short option of the launcher (-me 4g, -ru FILE, -wa) written as its long
    form, unless resume has that short option itself: argparse would read -me as resume's -m with the value "e".
    Words after "--" stay as they are."""
    if len(argv) < 2 or argv[0] != "resume":
        return argv
    own = set(parser()._subparsers._group_actions[0].choices["resume"]._option_string_actions)
    actions = launcher().option_actions() if hasattr(launcher(), "option_actions") else {}
    result, rest = list(argv[:2]), list(argv[2:])
    for index, token in enumerate(rest):
        if token == "--":
            result += rest[index:]
            break
        name, equals, value = token.partition("=")  # -me=4g too, as the launcher reads it
        if token.startswith("-") and not token.startswith("--") and name not in own and name in actions:
            token = max(actions[name].option_strings, key=len) + equals + value
        result.append(token)
    return result


def pack_command(args, out=print, ask=None):
    """sandbox-ai gc --pack [--older-than DAYS] [--home NAME] [--dry-run] [--yes], or gc --unpack NAME: only that."""
    others = [option for option, given in (("--caches", args.caches), ("--images", args.images),
                                           ("--build-output", args.build_output), ("--toolchains", args.toolchains),
                                           ("--transcripts", args.transcripts is not None),
                                           ("--logs-older-than", args.logs_older_than is not None),
                                           ("--no-compact", args.no_compact), ("--usage", args.usage),
                                           ("--pack", args.pack and args.unpack)) if given]
    if others:
        raise ValueError(f"{'--unpack' if args.unpack else '--pack'} does only that: it goes without "
                         + ", ".join(others))
    statedirs, sessions = module("statedirs"), module("sessions")
    size, printable = statedirs.human, launcher().printable
    if args.unpack:
        if args.older_than is not None or args.home or args.dry_run or args.yes:
            raise ValueError("--unpack NAME goes without --older-than, --home, --dry-run and --yes")
        entry = find(args.unpack, entries(True, prune=False))
        with sessions.session_lock(entry["id"]):
            data = sessions.load(entry["id"])
            if not sessions.homepack().packed(sessions.session_dir(entry["id"])):
                raise ValueError(f"the HOME of {printable(entry['name'])} is not packed")
            found = sessions.unpack_home(entry["id"], data)
        if args.json:
            out(json.dumps({"unpacked": entry["name"], "entries": found.get("entries")}, indent=2))
        else:
            out(f"unpacked the HOME of {printable(entry['name'])} ({found.get('entries')} entries)")
        return 0
    chosen = pack_candidates(entries(True, prune=False), args.older_than, args.home)
    total = sum(statedirs.Scan().size(sessions.session_dir(entry["id"]) / "home") for entry in chosen)
    if args.json and (args.dry_run or not chosen):
        out(json.dumps({"dry_run": args.dry_run, "packed": [], "would_pack": [
            {"name": entry["name"], "id": entry["id"], "last_used": entry.get("last_used")} for entry in chosen],
            "bytes": total, "skipped": []}, indent=2))
        return 0
    if not chosen:
        out("nothing to pack" + (f": no stopped kept start is unused for more than {args.older_than} days and not "
                                 "packed yet" if args.older_than is not None else ": no stopped kept HOME that is "
                                                                                  "not packed yet"))
        return 0
    t = module("textout")
    if not args.json:
        out(f"{'would pack' if args.dry_run else 'to pack'}: {len(chosen)} HOME(s), {size(total)} - each becomes one "
            "compressed archive beside it and is unpacked again when the start is resumed")
        t.grid(["NAME", "LAST USED", "WORKSPACE"],
               [[printable(entry["name"]), t.ago(entry.get("last_used")), printable(entry.get("workspace") or "-")]
                for entry in chosen], out=out)
    if args.dry_run:
        return 0
    if not confirm(f"Pack these {len(chosen)} HOME(s)?", args.yes, ask):
        out("nothing packed")
        return 1
    result = pack_homes(chosen, out if not args.json else (lambda line: None))
    if args.json:
        out(json.dumps({"dry_run": False, **result}, indent=2))
    else:
        for item in result["packed"]:
            out(f"packed the HOME of {printable(item['name'])}: {size(item['bytes'])} into "
                f"{size(item['archive_bytes'])} ({printable(item['archive'])})")
        for item in result["skipped"]:
            out(f"left as it was: {printable(item['name'])}: {printable(item['reason'])}")
    return 1 if result["skipped"] and not result["packed"] else 0


def main(argv, out=print, ask=None):
    argv = launcher_long_forms(list(argv))
    shell_command = []
    if argv[:1] == ["shell"] and "--" in argv:  # what runs in the sandbox, taken as it is
        argv, shell_command = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    args, extra = parser().parse_known_args(argv)
    if extra and args.command == "shell":
        print(f"sandbox-ai shell: a command goes after --: sandbox-ai shell NAME -- {shlex.join(extra)}",
              file=sys.stderr)
        return 2
    if extra and args.command != "resume":
        parser().parse_args(argv)  # the usual error for what it does not know
    try:
        if args.command == "rename":
            if args.codename and args.new is not None:  # the name given wins; said, not refused
                print(f"sandbox-ai rename: NEW given, so --codename is not used: the name is "
                      f"{launcher().printable(args.new)}", file=sys.stderr)
            return rename(args.old, args.new, out)
        if args.command == "gc":
            if args.older_than is not None and args.older_than < 0:
                raise ValueError("--older-than DAYS: 0 or more")
            if args.logs_older_than is not None and args.logs_older_than < 1:
                raise ValueError("--logs-older-than DAYS: 1 or more")
            if args.detail is not None and not args.usage:
                raise ValueError("--detail goes with --usage: sandbox-ai gc --usage --detail [NAME]")
            if args.usage and (args.pack or args.unpack or args.build_output or args.toolchains or args.home
                               or args.transcripts is not None or args.yes or args.check or args.only
                               or args.compact_transcripts is not None):
                raise ValueError("--usage removes nothing: it goes without --pack, --unpack, --build-output, "
                                 "--toolchains, --transcripts, --compact-transcripts, --only, --check, --home and "
                                 "--yes")
            if args.usage and args.detail is not None:
                if args.dry_run or args.caches or args.images or args.no_compact or args.older_than is not None \
                        or args.logs_older_than is not None:
                    raise ValueError("--usage --detail measures only: it goes without other options but --json and "
                                     "--color")
                show_usage_detail(usage_detail(args.detail or None), out, args.json, colour_choice(args.color))
                return 0
            if args.usage:
                if args.dry_run or args.caches or args.images or args.no_compact:
                    raise ValueError("--usage removes nothing: it goes without --dry-run, --caches, --images and "
                                     "--no-compact")
                show_usage(usage(args.older_than, args.logs_older_than), out, args.json, colour_choice(args.color))
                return 0
            if args.check:
                others = [option for option, given in (
                    ("--caches", args.caches), ("--images", args.images), ("--build-output", args.build_output),
                    ("--toolchains", args.toolchains), ("--transcripts", args.transcripts is not None),
                    ("--compact-transcripts", args.compact_transcripts is not None), ("--pack", args.pack),
                    ("--unpack", args.unpack), ("--only", args.only), ("--yes", args.yes),
                    ("--older-than", args.older_than is not None), ("--no-compact", args.no_compact),
                    ("--logs-older-than", args.logs_older_than is not None)) if given]
                if others or len(args.home) > 1:
                    raise ValueError("--check lists, it removes nothing itself: it goes with --home NAME (one), "
                                     "--dry-run, --json and --color only" + (f", not {', '.join(others)}"
                                                                              if others else ""))
                show_check(storage_check(args.home[0] if args.home else None, args.dry_run), out, args.json,
                           colour_choice(args.color))
                return 0
            if args.pack or args.unpack:
                if args.only or args.compact_transcripts is not None:
                    raise ValueError(f"{'--unpack' if args.unpack else '--pack'} does only that: it goes without "
                                     + ("--only" if args.only else "--compact-transcripts"))
                return pack_command(args, out, ask)
            kinds = [kind for kind, wanted in (("build", args.build_output), ("toolchains", args.toolchains),
                                               ("transcripts", args.transcripts is not None)) if wanted]
            if args.transcripts is not None and args.transcripts < 1:
                raise ValueError("--transcripts DAYS: 1 or more")
            compact_days = args.compact_transcripts
            if compact_days is not None and compact_days < 1:
                raise ValueError("--compact-transcripts DAYS: 1 or more (the newest of a conversation is never taken)")
            logs_days = args.logs_older_than
            if args.home and not (kinds or args.caches or compact_days is not None or logs_days is not None):
                raise ValueError("--home goes with --caches, --build-output, --toolchains, --transcripts, "
                                 "--compact-transcripts, --logs-older-than, --check or --pack")
            if args.only and not (kinds or args.caches or args.images or compact_days is not None
                                  or logs_days is not None):
                raise ValueError("--only goes with what it does: --caches, --build-output, --toolchains, --transcripts, "
                                 "--compact-transcripts, --images or --logs-older-than")
            if args.only and (args.older_than is not None or args.no_compact):
                raise ValueError("--only does what the options name and nothing else: it goes without --older-than "
                                 "and --no-compact")
            if args.yes and not (kinds or args.caches or compact_days is not None or logs_days is not None) \
                    and args.images not in ("old", "prune"):
                raise ValueError("--yes goes with --build-output, --toolchains, --transcripts, --compact-transcripts, "
                                 "--caches, --logs-older-than, --images=old|prune or --pack")
            if args.images in ("old", "prune") and not args.dry_run and not args.yes:
                plan = image_plan(args.images)
                if plan:
                    show_image_plan(plan, "would remove", out)
                    if not confirm("Remove these images (podman image rm, never forced)?", ask=ask):
                        out("nothing removed")
                        return 1
            if kinds and not args.dry_run and not args.yes:
                planned = clear_home_kinds(chosen_homes(entries(True, prune=False), args.home), kinds,
                                           args.transcripts, dry_run=True)
                if planned:
                    show_home_items(planned, "would remove", out)
                    if not confirm("Remove these (they are gone for good; gc then does the rest)?", ask=ask):
                        out("nothing removed")
                        return 1
            if compact_days is not None and not args.dry_run and not args.yes:
                planned = compact_transcripts(chosen_homes([entry for entry in entries(True, prune=False)
                                                            if entry.get("kind") != "mcp"], args.home),
                                              compact_days, dry_run=True)
                if planned:
                    size = module("statedirs").human
                    out(f"would compact the transcripts older than {compact_days} days in place (gzip; read as "
                        "before):")
                    module("textout").grid(
                        ["START", "STATE", "FILES", "SIZE", "FREES"],
                        [[launcher().printable(item["name"]), item.get("state") or "?", str(item["files"]),
                          size(item["bytes"]), "about " + size(item["saved"])] for item in planned],
                        right={2, 3, 4}, out=out)
                    if not confirm("Compact these (each is read back and compared before the original goes)?",
                                   ask=ask):
                        out("nothing compacted")
                        return 1
            show_gc(gc(args.older_than, args.dry_run, compact=not args.no_compact, caches=args.caches,
                       images=args.images, logs_days=logs_days, kinds=kinds,
                       transcripts_days=args.transcripts, homes=args.home, compact_days=compact_days,
                       only=args.only), out, args.json)
            return 0
        if args.command == "shell":
            return shell(args.name, shell_command, False if args.no_tty else None, out)
        if getattr(args, "user", None) is not None and (args.command == "stop" or getattr(args, "history", None)):
            # (sandbox-ai's dispatcher runs another user's as that user, main.py; here only one's own name is left)
            raise ValueError(f"--user {launcher_printable(args.user)}: another user's start is reached through "
                             f"sandbox-ai {args.command} (as root): sandbox-ai {args.command} --user USER ...")
        if args.command == "stop" and (args.all or args.name is None):
            if args.all == (args.name is not None) or (args.yes and not args.all):
                raise ValueError("stop takes NAME, or --all (with --yes: without asking)")
            return stop_all(out, wait=args.wait, yes=args.yes)
        if args.command == "stop" and args.yes:
            raise ValueError("--yes goes with --all")
        if args.command in ("attach", "pause", "resume", "stop", "delete"):
            review, restore = getattr(args, "review_home", False), getattr(args, "restore_home", False)
            if getattr(args, "json", False) and not (review or restore):
                raise ValueError("--json goes with --review-home or --restore-home")
            if review and restore:
                raise ValueError("--review-home shows, --restore-home puts back: one at a time")
            if (getattr(args, "path", None) or (args.command == "resume" and args.yes)) and not restore:
                raise ValueError("--path and --yes go with --restore-home")
            if (review or restore) and (getattr(args, "workspace", None) or getattr(args, "mount", None)
                                        or getattr(args, "mcp", None) or getattr(args, "restart", False)
                                        or getattr(args, "drop_option", None) or extra):
                raise ValueError(f"{'--review-home' if review else '--restore-home'} starts nothing: it goes without "
                                 "--workspace, --mount, --mcp, --restart and other options")
            return act(args.command, args.name, out, wait=getattr(args, "wait", None), yes=getattr(args, "yes", False),
                       review=review, as_json=getattr(args, "json", False),
                       workspaces=getattr(args, "workspace", []), mounts=getattr(args, "mount", []),
                       restart=getattr(args, "restart", False), mcp=getattr(args, "mcp", []), options=extra,
                       dropped=getattr(args, "drop_option", []), restore=restore, paths=getattr(args, "path", []))
        if args.folders:
            if args.history or args.flat or args.all or args.stopped or args.mcp or args.all_users or \
                    args.user is not None:
                raise ValueError("--folders goes without --history, --flat, --all, --stopped, --mcp, --all-users and "
                                 "--user (it lists the folders of your running and kept starts)")
            show_folders(folders(), out, args.json)
            return 0
        colour = colour_choice(args.color)
        if args.all_users or args.user is not None:  # root: every user's starts, each user's state read as data
            if args.history or args.flat:
                raise ValueError(f"{'--history' if args.history else '--flat'} goes without --all-users and --user "
                                 "(another user's history: sandbox-ai list --history USER:NAME)")
            return module("allusers").list_command(args, out, colour, own=lambda include_all: [
                {"user": module("allusers").myself() or "root", "uid": os.geteuid(), **entry}
                for entry in entries(include_all)])
        if args.history:
            return show_history(args.history, out, args.json, colour)
        found = entries(args.all)
        if args.json:
            found = with_storage(found)
        if args.json or args.all:  # --json: everything, as before (scripts, the app, the web interface)
            show(found, out, args.json, colour, args.flat, trunc=not args.no_trunc)
        else:
            listed_, hidden = shown_filter(found, stopped=args.stopped, mcp=args.mcp)
            show(listed_, out, False, colour, args.flat, trunc=not args.no_trunc, hidden=hidden)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        # (a full file system named, with its room - not "[Errno 28] No space left on device: PATH")
        print(f"sandbox-ai {args.command}: {launcher().full_text(exc) or exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
