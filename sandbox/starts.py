#!/usr/bin/env python3
"""sandbox-ai list|rename|attach|pause|resume|stop|delete: every start of an agent in one registry, by name.

  sandbox-ai list [--all] [--flat] [--no-color] [--json]
                                         what runs and what is kept, grouped by start (also: sandbox-ai ps)
  sandbox-ai list --history NAME         what happened to NAME: start, exit, pause, resume, rebirth, rename
  sandbox-ai rename OLD NEW              another name for a start
  sandbox-ai rename OLD [--codename]     a fresh codename for it, such as brisk-otter-lantern
  sandbox-ai attach NAME                 this terminal into a running start (detach: Ctrl-b d)
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
MCP servers. Each start is followed by what belongs to it (its MCP servers and helpers); --flat lists every row on
its own. Colour on a terminal only (--no-color, NO_COLOR). --json writes the same as a list of objects, for
scripts and the desktop app; "attachable" says whether attach can bring a start's terminal here, and "attach_why"
why not (mcp, damaged, stopped, foreground, paused; null when it can); "listed_under" the start a row is shown
below, an MCP server's "serves" the starts that use it (the one it was started for first, "started_for";
"owner_recorded": its mcp.json says whom it was started for), a team member's "lead" its lead's name.

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
    current = read_json(directory / "session.json")
    if current is not None and isinstance(current.get("run"), str) and current["run"] != run:
        return  # the end of an earlier run, found late (net.forget_ended): the start runs again since
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
    except (OSError, ValueError):
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
        if data is None or data.get("agent") not in ("claude", "codex", "opencode") or \
                not isinstance(data.get("options"), list):  # as `session list` judges it
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
                      "attachable": state in ("running", "orphaned") and not sessions.foreground(data),
                      "run": text(data.get("run")) if state != "stopped" or sessions.foreground(data) else None,
                      "pid": data.get("pid") if state not in ("stopped", "orphaned") else None,
                      "started": text(data.get("started")) or text(data.get("created")),
                      "created": text(data.get("created")), "ended": text(ended.get("ended")),
                      "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                      "last_used": used or None, "expires": expires,
                      "expired": bool(expires and state == "stopped" and expires < now()),
                      "workspace": text(data.get("workspace")), "project": text(data.get("project")),
                      "mcp": mcp_names(data["options"]), "llm": llm_choice(data["options"]),
                      "directory": str(directory)}
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
    the foreground of its own terminal or script: a task, no terminal, --no-tmux), "paused" (resume it first).
    The desktop app and the web interface explain each in their own words."""
    if entry.get("attachable"):
        return None
    if entry.get("kind") == "mcp":
        return "mcp"
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
AGENTS = ("claude", "codex", "opencode")  # the launcher's AUTH_TARGETS (a test keeps both the same)
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")  # team.py's TEAM_NAME


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
    except ValueError:
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
                if options or dropped:
                    # Checked before it is stopped: a refused change leaves it running as it was.
                    data = sessions.load(key)
                    sessions.check_start(key, {**data, "options": sessions.changed_options(
                        {**data, "name": entry["name"]}, list(options), list(dropped))[0]})
                out(f"{shown}: stopping it to start it again")
                sessions.stop(key)
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


def stray_pod(name):
    """The running Podman pod NAME names (agent-RUN, or RUN) that no start knows (its launcher is gone, and an older
    version removed its run directory: gc names these), or None."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        return None
    pod = name if name.startswith("agent-") else f"agent-{name}"
    return pod if module("liveness").pod_live(pod, fresh=True) else None


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
        subprocess.run(launcher().podman_command(engine, "pod", "pause" if freeze else "unpause", pod), check=True,
                       stdout=subprocess.DEVNULL)
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


def gc(days=None, dry_run=False, expired_only=False):
    """Remove what is no longer needed: sessions that ended without keeping their HOME (leftovers of a supervisor
    that died); with DAYS (else keep_days) also the stopped kept starts unused for more than DAYS days, and the
    histories of names no start uses whose last entry is that old; and the Podman pods of sandbox-ai's that nothing
    runs in any more and no start uses (leftovers of launchers that were killed, a reboot). EXPIRED_ONLY: only those
    kept starts (what a new start does with keep_days). DRY_RUN: say what would go, remove nothing. Pods that still
    run a container are never removed: those no start knows are named ("orphaned_pods")."""
    days = days if days is not None else keep_days()
    # 0 days (--older-than 0) is every stopped kept start, not "no expiry"
    cutoff = time.time() - days * 86400 if days is not None else None
    sessions = module("sessions")
    result = {"days": days, "dry_run": dry_run, "removed": [], "leftovers": [], "histories": [], "damaged": [],
              "skipped": [], "pods": [], "orphaned_pods": []}
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
    if result.get("pods"):
        out(f"{verb} {len(result['pods'])} Podman pod(s) of ended runs: {', '.join(map(printable, result['pods']))}")
    for pod in result.get("orphaned_pods") or []:
        out(f"left alone: the Podman pod {printable(pod)} still runs, but no start uses it (its launcher is gone); end "
            f"it with: sandbox-ai stop {printable(pod)}")
    for entry in result["skipped"]:
        out(f"left alone: {printable(entry['name'])}: {printable(entry['reason'])}")
    for directory in result["damaged"]:
        out(f"left alone: {printable(directory)} is damaged (no readable session.json); remove it by hand")
    if not (result["leftovers"] or result["removed"] or result["histories"] or result.get("pods")):
        out("nothing to remove" + ("" if result["days"] is not None else "; kept starts never expire on their own: "
                                   "--older-than DAYS removes those unused for that long"))


def shown_time(value):
    """An ISO time of a record (UTC) as "YYYY-MM-DD HH:MM", or "-"."""
    if not isinstance(value, str) or len(value) < 16:
        return "-"
    return value[:16].replace("T", " ")


HEADERS = ["NAME", "KIND", "AGENT", "STATE", "HOME", "STARTED (UTC)", "WORKSPACE"]
# A start with --ask allow: after its state (never a question, every one allowed; the app and the web warn alike).
ASK_ALLOW_STATE = "asks nothing, allows every question (--ask allow)"
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
    return [t.style(mark, "muted", colour) + t.style(printable(entry["name"]), kind_style(entry), colour),
            t.style(entry["kind"], kind_style(entry), colour),
            printable(entry.get("agent") or "-"),
            t.style(entry["state"] + (", expired" if entry.get("expired") else ""),
                    STATE_STYLES.get(entry["state"], "muted"), colour)
            + (t.style(f", {ASK_ALLOW_STATE}", "bad", colour) if entry.get("ask") == "allow" else ""),
            entry.get("home") or "-",
            shown_time(entry.get("started")),
            printable(where)]


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
    tops = [index for index in range(len(found)) if index not in below]
    tops = [i for i in tops if found[i].get("kind") != "mcp"] + [i for i in tops if found[i].get("kind") == "mcp"]
    rows = []
    for index in tops:
        rows.append(row(found[index], colour))
        mine = children.get(index, [])
        for position, child in enumerate(mine):
            rows.append(row(found[child], colour, marks[position == len(mine) - 1], found[index]["name"]))
    return rows


def show(found, out=print, as_json=False, colour=None, flat=False, marks=None):
    """The table of FOUND (entries()), or JSON (AS_JSON: never coloured). COLOUR: True, False, or None for
    colour_choice("auto"). FLAT: one row per entry in the listed order; else grouped() (MARKS: the tree markers,
    default tree_marks())."""
    if as_json:
        out(json.dumps(found, indent=2))
        return
    colour = colour_choice("auto") if colour is None else colour
    rows = [row(entry, colour) for entry in found] if flat else grouped(found, colour, marks or tree_marks())
    # The workspace last: it is the longest and wraps at the terminal's width.
    module("textout").table(HEADERS, rows, out, indent=0, colour=colour,
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
LIST_HELP = """What runs or is paused, the starts whose HOME is kept (stopped ones too) and the running MCP server
sandboxes; --all adds the MCP servers that do not run.

Grouped by default: each start with the MCP servers and helpers that belong to it below it (├─, └─): the servers
it was started with or uses, a helper below its lead. A server several starts use is shown once, below the start it
was started for (else the first that uses it), and names the others ("also serves ..."). The MCP servers no listed
start uses follow the starts. --flat lists every row on its own, in start order (as earlier versions did).

Colour on a terminal (not with NO_COLOR set or TERM=dumb; --color=always forces it, --no-color turns it off):
KIND and NAME by kind (session cyan, run blue, MCP server magenta, helper violet), STATE by state (running green,
paused yellow, damaged red, stopped grey). A start with --ask allow says after its state, in red, that it asks
nothing and allows every question ("ask" in --json: on, off or allow)."""


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
                             help="also the MCP servers that do not run (a start whose HOME is kept is always "
                                  "listed, stopped or not)")
        listing.add_argument("-H", "--history", metavar="NAME", help="what happened to NAME instead")
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
    for command, text_ in (("attach", "this terminal into a running start (detach again: Ctrl-b d)"),
                           ("pause", "freeze every process of a start"),
                           ("resume", "thaw a paused start, or bring a stopped one back"),
                           ("stop", "end a start; what it keeps stays"),
                           ("delete", "remove a stopped start with its kept HOME")):
        sub = commands.add_parser(command, help=text_, allow_abbrev=False)
        sub.add_argument("name", metavar="NAME")
        if command == "stop":
            sub.add_argument("-w", "--wait", type=float, metavar="SECONDS",
                             help="how long to wait for it to end (default 30)")
        if command == "delete":
            sub.add_argument("-y", "--yes", action="store_true", help="confirm the removal of its HOME")
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
                             help="start it with this mount added (repeatable), kept for its later starts")
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


def main(argv, out=print):
    argv = launcher_long_forms(list(argv))
    args, extra = parser().parse_known_args(argv)
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
            show_gc(gc(args.older_than, args.dry_run), out, args.json)
            return 0
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
        colour = colour_choice(args.color)
        if args.history:
            return show_history(args.history, out, args.json, colour)
        show(entries(args.all), out, args.json, colour, args.flat)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"sandbox-ai {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
