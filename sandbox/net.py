#!/usr/bin/env python3
"""sandbox-ai net: change what a running sandbox may reach, without restarting it.

  sandbox-ai net list                                  running sandboxes
  sandbox-ai net show [RUN]                            what was allowed during the run
  sandbox-ai net allow [RUN] [--allow-host HOST] [--allow-domain DOMAIN] [--allow-tcp IP:PORT] [--allow IP_OR_CIDR]
                       [--allow-PRESET]                  (--allow-domain D: --allow-host D --allow-host '*.D')
                       [--dns] [--nameserver IP]           real DNS lookups (as --dns at the start)
                       [--git-token-env|--git-token-file|--git-token-secret URL=REF]   a Git login
                       [--git-token-in-sandbox]            that login where the run's proxy cannot log in
                       [--allow-host-port PORT]            the host's 127.0.0.1:PORT, through a relay
                       [--api-key HOST=NAME[=SECRET]]      an API key the proxy puts in (runs from 0.1.92)
                       [--public-only]                     with --allow-host alone: only each name's public
                                                           addresses (--keep: remembered as an approval)
  sandbox-ai net revoke [RUN] (the same options)       take back what `allow` gave
  sandbox-ai net untaint [RUN]                         take back the run's taint (untrusted content entered it)
  sandbox-ai net cut [RUN] MODE [--only] | --all-runs MODE
                                                       the kill switch: MODE remote (only the local network and the
                                                       host's ports stay), local (only the internet stays), api (only
                                                       the model API stays), all (nothing); off lifts it. Also cuts
                                                       the run's MCP servers, helpers and team (not with --only)

RUN is the name a sandbox shows to its agent as $SANDBOX_AI_RUN, or the name of its start as `sandbox-ai list`
(ps) shows it, also after a rename; it may be left out
when only one sandbox runs, or only one for the current directory (not for `stop`). Changes last until
the sandbox ends; to keep them, add the same options to its start command or its .sandbox-ai.json. Only
you can make them: the agent cannot reach the control socket. Connections that are already open stay
open, except those a cut forbids: the kill switch closes them, and it never opens anything the run may not reach
otherwise. A sandbox is ended with `sandbox-ai stop NAME` (any start: a run, a session, an MCP server);
`sandbox-ai net stop RUN [--wait SECONDS]` is its older form for a run, kept for scripts and the app.

Standard library only.
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shlex
import signal
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
# Where a login session's runtime directory lives (XDG_RUNTIME_DIR is /run/user/UID there).
USER_RUNTIME = Path("/run/user")
STOP_WAIT = 30
# A launcher registers (run.json "started") right after it starts; a process of that PID that started
# later than this after the registration is another one (records without "pid_start").
START_SLACK = 5
_launcher = None
_launcher_source = None  # starts.py's launcher() when starts.py loaded this module: one launcher for all of its modules
_textout = None


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        made = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(made)
        with LOADING:
            loaded = sys.modules.setdefault("sandbox_ai_launchlog", made)
    return loaded


LOG = launch_log()


def textout():
    global _textout
    if _textout is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_textout_for_net", HERE / "textout.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _textout is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _textout = loaded
    return _textout


def launcher():
    global _launcher
    if _launcher is None and _launcher_source is not None:
        _launcher = _launcher_source()
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_net", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


_procinfo = None


def procinfo():
    """procinfo.py (start times, command lines, owners of processes): the launcher's once that is loaded, else one of
    its own - `net list`, `list` and the app's looks at the running sandboxes load no launcher for it."""
    global _procinfo
    if _launcher is not None:
        return _launcher.procinfo()
    if _procinfo is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_procinfo_for_net", HERE / "procinfo.py")
        with LOADING:
            if _procinfo is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _procinfo = loaded
    return _procinfo


def process_start(pid):
    """launcher().process_start(PID), without loading the launcher for it (a test keeps both the same)."""
    return _launcher.process_start(pid) if _launcher is not None else procinfo().start_time(pid)


def printable(text):
    """launcher().printable, without loading the launcher for it (a test keeps both the same): TEXT safe for a
    terminal, control, bidi and other non-printable characters escaped."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


_liveness = None


def liveness():
    """liveness.py: whether a run whose launcher is gone has ended, and what is left of it."""
    global _liveness
    if _liveness is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_liveness_for_net", HERE / "liveness.py")
        with LOADING:
            if _liveness is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _liveness = loaded
    return _liveness


def alive(pid, start=None):
    """PID still runs, and, when its START time was recorded (run.json's pid_start), it is still that process:
    a PID reused by another process after the run ended no longer passes for the run. Unknown either way (no
    /proc, a record of an older version) counts as alive: a live run's directory must never be removed."""
    try:
        pid = int(pid)
        if pid <= 0:
            # No process's own number (a damaged record): 0 and -1 would ask about a whole process group or every
            # process, and never about the run - unknown, as a value that is no number.
            return True
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OverflowError):  # (a number beyond a PID: OverflowError)
        return True
    if zombie(pid):
        return False  # ended, only not reaped yet (the terminal of 0.1.93 that started it never did): signal 0 passes
    if isinstance(start, int) and not isinstance(start, bool):
        now = process_start(int(pid))
        if now is not None and now != start:
            return False
    return True


def zombie(pid):
    """PID has ended and waits to be reaped (state Z in PROC/PID/stat); False where that cannot be read (macOS)."""
    try:
        text = (PROC / str(int(pid)) / "stat").read_text(errors="replace")
    except (OSError, ValueError):
        return False
    fields = text.rpartition(")")[2].split()  # (the command's name may hold spaces and parentheses)
    return bool(fields) and fields[0] == "Z"


def ours(path):
    """PATH is a directory of this user's."""
    try:
        info = os.stat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def own_runs_dir():
    """launcher().runs_dir(), without loading the launcher: `sandbox-ai approvals`, `list` and the app read the run
    directories often, and the launcher's load was half of such a command (a test keeps both the same)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "sandbox-ai" if runtime and Path(runtime).is_dir() else \
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"
    return base / "runs"


def run_dirs():
    """Every place a run of this user may have registered, also one started with another XDG_RUNTIME_DIR
    (or none): launcher().runs_dir() first, then $XDG_RUNTIME_DIR/sandbox-ai/runs, /run/user/UID/sandbox-ai/runs
    and the state directory's runs; each real directory once."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "runs"
    candidates = [own_runs_dir()]
    if os.environ.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs")
    user = USER_RUNTIME / str(os.getuid())
    if ours(user) and not user.is_symlink():
        candidates.append(user / "sandbox-ai" / "runs")
    candidates.append(state)
    found, seen = [], set()
    for directory in candidates:
        real = os.path.realpath(directory)
        if real not in seen and ours(directory):
            seen.add(real)
            found.append(directory)
    return found


def running(orphans=None):
    """{run: record} of the sandboxes registered whose launcher still runs (the first of a name wins). A run whose
    launcher is gone is removed only when it has ended for certain (liveness.verdict: where this process sees the
    launcher's processes, and with Podman once no container of its pod runs): its end is recorded, its pod and its
    directories go. One whose pod still runs a container ("orphaned": the launcher was killed without its cleanup)
    is kept, and put into ORPHANS (a dict, when given) with "orphaned": True; one this process cannot judge (inside
    a sandbox, Podman not answering) is kept and left out."""
    runs = {}
    for directory in run_dirs():
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                record = json.loads((entry / "run.json").read_text())
            except (OSError, ValueError, RecursionError):  # (damaged: nested beyond what Python reads)
                continue
            if not isinstance(record, dict):
                continue
            if not alive(record.get("pid"), record.get("pid_start")):
                verdict = liveness().verdict(record)
                if verdict == "dead":
                    forget_ended(entry, record)
                elif verdict == "orphaned" and orphans is not None:
                    orphans.setdefault(record.get("run", entry.name), {**record, "directory": str(entry),
                                                                       "orphaned": True})
                continue
            record["directory"] = str(entry)
            cut = cut_state(entry)
            if cut:  # the kill switch holds (sandbox-ai net cut): net list, list and the app show it
                record["cut"] = cut
            runs.setdefault(record.get("run", entry.name), record)
    return runs


def forget_ended(directory, record):
    """The run in DIRECTORY (RECORD: its run.json) ended without its launcher's cleanup (the launcher was killed):
    its end goes into its name's history and its start's exit.json (once), its pod goes if Podman still lists it
    with nothing running (on a Mac Cairn's relay volume too), then its directory and the folders beside it. One command at a time (a lock on the
    directory): the app lists the runs often, several commands at once, and each that found the run recorded its end
    before one of them removed the directory (two ends of one run in its history); one that finds the lock held, or
    the directory gone once it holds it, leaves the cleanup to the other."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return  # gone meanwhile: cleaned up by another command
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another command is at it
        try:
            now = json.loads(Path(directory, "run.json").read_text())
        except (OSError, ValueError, RecursionError):
            return  # it was done while this one waited
        if not isinstance(now, dict) or any(now.get(key) != record.get(key) for key in ("run", "pid", "pid_start")):
            return  # registered anew meanwhile (a start of that name): never this one's end
        with contextlib.suppress(Exception):  # the record is read from disk: whatever it holds, the cleanup goes on
            launcher().load_starts_module().run_event(directory, "exit")
        with contextlib.suppress(Exception):
            liveness().remove_ended_pod(record)
        with contextlib.suppress(Exception):
            liveness().remove_ended_volumes(record)  # (macOS: Cairn's relay, a volume in podman machine's VM)
        liveness().discard(directory)
    finally:
        os.close(fd)


_sessions = None
_starts = None


def sessions_module():
    global _sessions
    if _sessions is None:
        spec = importlib.util.spec_from_file_location("sandbox_sessions_for_net", HERE / "sessions.py")
        with LOADING:
            if _sessions is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _sessions = loaded
    return _sessions


def starts_module():
    global _starts
    if _starts is None:
        spec = importlib.util.spec_from_file_location("sandbox_starts_for_net", HERE / "starts.py")
        with LOADING:
            if _starts is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _starts = loaded
    return _starts


def label(run, record):
    """The name `sandbox-ai list` (ps) shows for the running RUN (RECORD: its run.json): the current name of its
    start (session.json, renamed perhaps), else the name its run.json says, else RUN itself (an MCP server: mcp-NAME).
    As starts.entries names it, also for a session of a version before 0.1.30 (run session-KEY)."""
    start = record.get("start") if isinstance(record.get("start"), str) else None
    if start is None and isinstance(run, str) and run.startswith("session-"):
        start = run[len("session-"):]
    if start:
        try:
            sessions = sessions_module()
            return sessions.display(start, sessions.load(start))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    name = record.get("name")
    return name if isinstance(name, str) and name else run


def who(record, run=None):
    """"RUN (NAME)": the run's id and the name of its start as `sandbox-ai list` shows it (RUN alone when they are
    the same) - what every message about a credential given to a run names."""
    printable = launcher().printable
    run = record.get("run") or run or "?"
    name = label(run, record)
    return f"{printable(run)} ({printable(str(name))})" if name and name != run else printable(run)


def shown_login(entry):
    """A Git login given during the run as it is listed: {url, user} - and "in_sandbox": True for one whose token is
    in the sandbox's session directory (readable by the agent); never the token."""
    return {"url": entry["url"], "user": entry.get("user"),
            **({"in_sandbox": True} if entry.get("where") == "sandbox" else {})}


def given_credentials(record):
    """{"git": [{url, user[, in_sandbox]}], "api_keys": [{name, host, port, placeholder, secret}]} the run was given during the run
    (`net allow --git-token-*`/`--api-key`, `git-logins give`, `secrets give`): names, never a token or a key."""
    return {"git": [shown_login(entry) for entry in git_logins(record)],
            "api_keys": [{"name": entry["name"], "host": entry.get("host"), "port": entry.get("port"),
                          "placeholder": entry.get("placeholder"), "secret": entry.get("secret")}
                         for entry in added_api_keys(record)]}


def list_given(run, kind, out=print, as_json=False, prog="net"):
    """`git-logins list --run RUN` (KIND "git") and `secrets list --run RUN` (KIND "api_keys"): what the running RUN
    was given during the run, by name (never a token or a key)."""
    printable = launcher().printable
    try:
        record = choose(running(), run, command=f"{prog} list")
        found = given_credentials(record)[kind]
        if as_json:
            out(json.dumps({"run": record.get("run"), "name": label(record.get("run"), record), kind: found},
                           indent=2))
            return 0
        t, name = textout(), who(record, run)
        if kind == "git":
            t.section(f"{name}: Git logins given during the run", out,
                      note="sandbox-ai git-logins give / revoke; its proxy logs in on the wire, no token is in the "
                           "sandbox (TOKEN \"in the sandbox\": given so, the agent can read it)")
            t.table(["SERVER", "USER", "TOKEN"], [[printable(e["url"]), printable(str(e.get("user") or "")),
                                                    "in the sandbox" if e.get("in_sandbox") else "with the proxy"]
                                                   for e in found],
                    out, empty=f"none (give one: sandbox-ai git-logins give {printable(str(record.get('run')))} URL "
                               "--secret NAME)")
        else:
            t.section(f"{name}: API keys given during the run", out,
                      note="sandbox-ai secrets give / revoke; the proxy puts each in on the wire, the agent sends "
                           "the placeholder")
            t.table(["VARIABLE", "HOST", "SECRET", "PLACEHOLDER"],
                    [[printable(str(e["name"])), printable(str(e.get("host") or "")),
                      printable(str(e.get("secret") or "-")), printable(str(e.get("placeholder") or ""))]
                     for e in found], out,
                    empty=f"none (give one: sandbox-ai secrets give {printable(str(record.get('run')))} NAME "
                          "--api-key HOST)")
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai {prog}: {exc}", file=sys.stderr)
        return 1


def labels(runs):
    """{run: what to call it}: its name as `sandbox-ai list` shows it, with "(run RUN)" when that name is not unique
    among RUNS."""
    named = {run: label(run, record) for run, record in runs.items()}
    counts = {}
    for name in named.values():
        counts[name] = counts.get(name, 0) + 1
    return {run: name if counts[name] == 1 or name == run else f"{name} (run {run})" for run, name in named.items()}


def added_hosts(record):
    """How many names the run was given since its start: allowed with `net allow` or by an answered question (its
    names.json), and the wildcards added (wildcards.json); files of its run directory only, the run is not asked."""
    names = remembered(record)
    try:
        added = json.loads(wildcards_file(record).read_text())
    except (OSError, ValueError, RecursionError):
        added = []
    return len(names) + len([w for w in added if isinstance(w, str)] if isinstance(added, list) else [])


APPROVAL_ID = re.compile(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}")  # approvals.ID: a question's file name


def waiting_questions(record):
    """How many of the run's questions wait for an answer (its approvals directory: QUESTION.json without .answer
    or .done beside it, and not one nobody waits for any more: timed out, still answerable - its .state)."""
    try:
        files = list((Path(record["directory"]) / "approvals").glob("*.json"))
    except (OSError, KeyError, TypeError):
        return 0
    return len([path for path in files if APPROVAL_ID.fullmatch(path.stem) and not path.with_suffix(".done").exists()
                and not path.with_suffix(".answer").exists() and not timed_out(path)])


def timed_out(path):
    """True when nobody waits for the open question PATH any more (its .state says "timeout"; approvals.question_state)."""
    try:
        return '"timeout"' in path.with_suffix(".state").read_text()[:4096]
    except OSError:
        return False


LIST_HEADERS = ["NAME", "AGENT", "MODE", "PROFILE", "VPN", "APPROVALS", "ADDED", "STARTED"]


def list_row(record, shown, colour, cut_column=False, wide=False):
    """One row of `net list` for a running sandbox (RECORD: its run.json; SHOWN: its name as `sandbox-ai list` shows
    it): what it may reach, in short."""
    t = textout()
    mode = record.get("mode") if isinstance(record.get("mode"), dict) else {}
    profiles = record.get("profiles")  # (runs of sandbox-ai 0.1.98 on)
    profile = (", ".join(printable(str(p)) for p in profiles) or "provider") if isinstance(profiles, list) else "-"
    if record.get("offline"):
        profile = "offline"
    vpn = record.get("vpn_profile")
    vpn = (printable(vpn) + (" (tor)" if record.get("vpn_type") == "tor" else "")) if isinstance(vpn, str) else "-"
    ask, waiting = record.get("ask"), waiting_questions(record)
    approvals = "no proxy" if record.get("proxy") is False else \
        {"on": "on", "off": "off", "allow": "allow all"}.get(ask, "-" if ask is None else printable(str(ask)))
    if waiting:
        approvals += f", {waiting} waiting"
    added = added_hosts(record)
    cut = record.get("cut")
    return [printable(str(shown)), printable(str(record.get("agent") or ("mcp" if record.get("kind") == "mcp" else "-"))),
            printable(str(mode.get("name") or "-")), profile, vpn,
            t.style(approvals, "bad" if ask == "allow" else "warn", colour) if ask == "allow" or waiting else approvals,
            f"{added} host{'s' if added != 1 else ''}" if added else "-",
            *([t.style(cut, "bad", colour) if cut else "-"] if cut_column else []),
            t.ago(record.get("started")),
            *([printable(str(record.get("run") or "-")), printable(t.home_short(record.get("workspace") or "-"))]
              if wide else [])]


def list_table(runs, out=print, wide=False, colour=None):
    """`sandbox-ai net list`: the running sandboxes, the newest first, one line each with what it may reach - its
    mode, profiles, VPN, approvals and the questions waiting, the hosts added since its start, CUT while a kill switch
    holds somewhere (`net show RUN` has the details); WIDE: its run id and workspace too. Colour on a terminal only."""
    t = textout()
    colour = t.colour_enabled() if colour is None else colour
    cut = any(r.get("cut") for r in runs.values())  # the column only while a kill switch holds somewhere
    names = labels(runs)
    ordered = sorted(runs.items(), key=lambda item: (-(t.utc_seconds(item[1].get("started")) or 0), str(item[0])))
    headers = LIST_HEADERS[:7] + (["CUT"] if cut else []) + LIST_HEADERS[7:] + (["RUN", "WORKSPACE"] if wide else [])
    t.table(headers, [list_row({"run": run, **record}, names[run], colour, cut, wide) for run, record in ordered],
            out, indent=0, colour=colour,
            empty="No sandbox is running. Start one with: sandbox-ai claude (or another agent: sandbox-ai --help)")


def running_names(runs):
    """"running: A, B" (the names `sandbox-ai list` shows), or "nothing runs"."""
    printable = launcher().printable
    shown = [printable(str(name)) for name in labels(runs).values()]
    return f"running: {', '.join(shown)}" if shown else "nothing runs"


class NotRunning(ValueError):
    """A command for a running sandbox named one that does not run. ENTRY: what `sandbox-ai list` knows of that name
    (a stopped kept start, an MCP server that does not run; starts.entries), None when it knows nothing of it."""

    def __init__(self, message, entry=None):
        super().__init__(message)
        self.entry = entry


# What a command for a running sandbox says to do instead when the start it names is stopped (a kept start: sandbox-ai
# resume starts it again); {name} is its name. The phrase follows "NAME is stopped: ".
STOPPED_HINTS = {
    "workspace add": "start it with the folder: sandbox-ai resume {name} --workspace DIR",
    "workspace remove": "start it again first: sandbox-ai resume {name}",
    "workspace list": "its workspaces are listed while it runs; it starts again with: sandbox-ai resume {name}",
    "net allow": "start it first (sandbox-ai resume {name}), then: sandbox-ai net allow {name} ... (with --keep it "
                 "goes into its project file for every later start too)",
    "net revoke": "what was allowed during its run ended with it; start it again with: sandbox-ai resume {name}",
    "net show": "what was allowed during its run ended with it; start it again with: sandbox-ai resume {name}",
    "net untaint": "its taint ended with its run; start it again with: sandbox-ai resume {name}",
    "net stop": "nothing to stop",
    "git-logins give": "start it first (sandbox-ai resume {name}), then: sandbox-ai git-logins give {name} URL ... (with "
                       "--keep it goes into its project file for every later start too)",
    "git-logins revoke": "a Git login given during its run ended with it",
    "git-logins list": "what it was given during its run ended with it; start it again with: sandbox-ai resume {name}",
    "secrets give": "start it first (sandbox-ai resume {name}), then: sandbox-ai secrets give {name} NAME --api-key "
                    "HOST (with --keep it goes into its project file for every later start too)",
    "secrets revoke": "an API key given during its run ended with it",
    "secrets list": "what it was given during its run ended with it; start it again with: sandbox-ai resume {name}",
    "net cut": "a stopped start reaches nothing; its network is as its options say when it starts again",
    "limits": "start it first (sandbox-ai resume {name}), then: sandbox-ai limits {name} --memory|--cpus|--pids ...",
    "devices add": "start it first (sandbox-ai resume {name}), then: sandbox-ai devices add {name} DEVICE",
    "devices remove": "what was added during its run ended with it; start it again with: sandbox-ai resume {name}",
    "devices list": "its devices are listed while it runs; it starts again with: sandbox-ai resume {name}",
    "canaries resume": "only a running sandbox a canary paused waits to continue",
    "approvals history": "the questions of a run are kept while it runs; what happened during its last run: "
                         "sandbox-ai activity {name}",
}


# Commands that would widen what a run may reach, see or do. On an orphaned run (its supervisor on the host is gone,
# its pod runs on) they are refused - fail closed: nothing would supervise what they give, and nobody be asked.
WIDENING = frozenset(("net allow", "net untaint", "net cut off", "workspace add", "approvals allow",
                      "git-logins give", "secrets give", "devices add", "limits", "canaries resume",
                      "rules reload"))


def orphaned_text(entry, command=None):
    """What a COMMAND for a running sandbox says of the orphaned start ENTRY (starts.entries): why it runs without its
    supervisor - its end as recorded (starts.orphaned_why: when, why, whether its network was cut) - and what to do
    (stop, then resume); a command that widens access (WIDENING) is refused as such."""
    what = printable(str(entry.get("name") or "NAME"))
    why = entry.get("orphaned_why") if isinstance(entry.get("orphaned_why"), str) else None
    if not why:
        try:
            why = starts_module().orphaned_why(entry)
        except Exception:  # noqa: BLE001 - the general reason: what to do is said all the same
            why = liveness().orphan_text(entry.get("name") or "NAME")
    why = printable(why)
    if command in WIDENING:
        return (f"{what} is orphaned: nothing supervises it, so {command} is refused (fail closed: nothing would hold "
                f"what it gives); {why}")
    return f"{what} is orphaned: {printable(command or 'this command')} needs its supervisor, which is gone; {why}"


def not_running(name, runs, command=None):
    """The NotRunning for NAME, which names none of the running RUNS: what it is (a stopped kept start, an MCP server
    that does not run, a start whose HOME was discarded, nothing), what COMMAND ("workspace add", "net allow", ...:
    STOPPED_HINTS) can do instead, and for an unknown name the running ones by the names `sandbox-ai list` shows."""
    printable = launcher().printable
    shown = printable(str(name))
    entry, last = None, None
    try:
        starts = starts_module()
        found = starts.entries(True, prune=False)
        for key in ("name", "id", "run"):
            matches = [e for e in found if e.get(key) == name]
            if len(matches) == 1:
                entry = matches[0]
                break
        if entry is None and starts.valid_name(name):
            last = (starts.history(name) or [None])[-1]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        entry = None  # the registry unreadable: only the message's details are lost (it says "no start named")
    if entry is not None:
        what = printable(str(entry.get("name") or name))
        state = entry.get("state")
        if entry.get("kind") == "mcp":
            return NotRunning(f"{what} is an MCP server sandbox that does not run now: it starts with the agents that "
                              "use it (sandbox-ai mcp list)", entry)
        if state == "damaged":
            return NotRunning(f"{what} is damaged (see sandbox-ai list): repair or delete it", entry)
        if state == "stopped":
            hint = STOPPED_HINTS.get(command, "start it again first: sandbox-ai resume {name}")
            return NotRunning(f"{what} is stopped: " + hint.format(name=what), entry)
        if state == "orphaned":
            return NotRunning(orphaned_text(entry, command), entry)
        return NotRunning(f"{what} does not run now ({printable(str(state))}; see sandbox-ai list)", entry)
    if isinstance(last, dict) and last.get("event") == "rename" and isinstance(last.get("to"), str):
        return NotRunning(f"{shown} was renamed: it is {printable(last['to'])} now; {running_names(runs)} (see "
                          "sandbox-ai list)")
    if isinstance(last, dict):
        return NotRunning(f"{shown} has ended and its HOME was discarded: nothing of it runs or is kept (see "
                          f"sandbox-ai list --history {shown}); {running_names(runs)}")
    return NotRunning(f"no start named {shown}; {running_names(runs)} (see sandbox-ai list)")


def choose(runs, name, cwd=None, command=None):
    """The record of the running sandbox NAME (of RUNS: running()), or of the only one (for CWD) when NAME is None.
    NAME is what `sandbox-ai list` (ps) shows: the name of its start (also after a rename), its start's id, or its run
    id. A name of none of them raises NotRunning, which says what NAME is and what COMMAND can do instead
    (not_running); one several running sandboxes share names them by their run ids."""
    printable = launcher().printable
    if name is not None:
        if name in runs:
            return runs[name]
        named = [record for run, record in runs.items()
                 if name in (label(run, record), record.get("start"))]
        if len(named) == 1:
            return named[0]
        if named:
            raise ValueError(f"several running sandboxes are named {printable(name)}; name one by its run id: "
                             f"{', '.join(printable(str(r.get('run', '?'))) for r in named)}")
        raise not_running(name, runs, command)
    if len(runs) == 1:
        return next(iter(runs.values()))
    cwd = str(Path(cwd or os.getcwd()).resolve())
    here = [record for record in runs.values() if record.get("workspace") == cwd]
    if len(here) == 1:
        return here[0]
    if not runs:
        raise ValueError("no sandbox is running")
    raise ValueError(f"several sandboxes run; name one: {', '.join(printable(str(n)) for n in labels(runs).values())}")


# --- the kill switch: sandbox-ai net cut ---------------------------------------------------------------------------

CUT_MODES = ("remote", "local", "api", "all")  # as runtime.CUT_MODES (a test keeps both the same); and "off"
CUT_TEXT = {"remote": "the internet is cut: only the local network and the host's ports it may reach stay reachable",
            "local": "the local network and the host's ports are cut: only the internet it may reach stays reachable",
            "api": "only its model API stays reachable",
            "all": "everything is cut, its model API too",
            "off": "the cut is lifted: it reaches again what it may reach"}
CUT_FILE = "cut.json"  # in the run's directory (outside the sandbox): the cuts its supervisor confirmed
CUT_PENDING = "cut-pending.json"  # in the run's directory: cuts asked for while it was paused, set as it resumes
CUT_SECONDS = 20  # one run's answer to a cut, at most (its firewall change included): an emergency must not wait long
CUT_VERSION = "0.1.94"
CUT_PENDING_LIMIT = 64  # requests kept for a paused run
# A run whose supervisor does not listen yet (it starts: an MCP server, helper or team member joining a cut run) has no
# CUT_READY in its directory: a cut for it waits in CUT_STARTING, which its supervisor sets before the agent runs
# (runtime.starting_cuts, which also says why one of the two always sees the other's file).
CUT_READY = "cut.ready"
CUT_STARTING = "cut-starting.json"


def cut_label(value):
    """VALUE (a mode, or modes joined with "+" as the supervisor names cuts that hold together) if it names a cut,
    else None."""
    parts = str(value).split("+") if isinstance(value, str) else []
    return value if parts and all(part in CUT_MODES for part in parts) else None


def cut_state(directory):
    """The kill switch of the run in DIRECTORY as its supervisor confirmed it last (cut_label: "api", "api+remote"),
    or None."""
    return cut_label(cut_record(directory).get("mode"))


def cut_record(directory):
    """{"mode", "cuts": {by: mode}} of the run in DIRECTORY (CUT_FILE), {} when there is none."""
    try:
        with open(Path(directory) / CUT_FILE, "rb") as stream:
            found = json.loads(stream.read(65536))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(found, dict):
        return {}
    cuts = found.get("cuts") if isinstance(found.get("cuts"), dict) else {}
    return {"mode": cut_label(found.get("mode")),
            "cuts": {by: mode for by, mode in cuts.items() if isinstance(by, str) and mode in CUT_MODES}}


def write_json(directory, name, value):
    """VALUE as JSON into DIRECTORY/NAME, replaced at once (None: the file goes)."""
    target = Path(directory) / name
    if value is None:
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value))
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def write_cut_state(directory, answer):
    """Keep the cuts the supervisor's ANSWER names ("cut", "cuts") as the run's kill switch (CUT_FILE; none: the file
    goes), for net list, sandbox-ai list and the app."""
    mode = cut_label(answer.get("cut")) if isinstance(answer, dict) else None
    cuts = answer.get("cuts") if isinstance(answer, dict) and isinstance(answer.get("cuts"), dict) else {}
    write_json(directory, CUT_FILE, {"mode": mode, "cuts": cuts,
                                     "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} if mode else None)


def team_servers(record):
    return {name for name in record.get("mcp_servers") or [] if isinstance(name, str) and name.startswith("team-")}


def mcp_server_of(run, record):
    """The MCP server whose sandbox the run RUN (RECORD) is, or None."""
    if record.get("kind") != "mcp" and not str(run).startswith("mcp-"):
        return None
    name = record.get("mcp_name")
    return name if isinstance(name, str) else str(run)[len("mcp-"):] if str(run).startswith("mcp-") else None


def related_runs(runs, record):
    """[(run, record)] of RUNS that act for RECORD's run in sandboxes of their own, with supervisors of their own: the
    MCP servers it reaches, its helpers (and theirs), the other members of the teams it works in, and theirs in turn;
    each once, never the run itself. An MCP server several runs reach is among them: each one's cut holds there."""
    found, todo, done = {}, [record], set()
    while todo:
        current = todo.pop()
        key = current.get("run")
        if key in done:
            continue
        done.add(key)
        servers = {name for name in current.get("mcp_servers") or [] if isinstance(name, str)}
        # A server it reaches in an instance with limits of its own (--mcp-limits): that instance's sandbox, NAME.HASH.
        instances = current.get("mcp_instances") if isinstance(current.get("mcp_instances"), dict) else {}
        servers = {name for name in servers if not isinstance(instances.get(name), str)} | \
            {value for value in instances.values() if isinstance(value, str)}
        teams = team_servers(current)
        for run, other in runs.items():
            if run in found or run == record.get("run") or not isinstance(other, dict):
                continue
            if mcp_server_of(run, other) in servers or (key and other.get("helper_of") == key) or \
                    (teams and teams & team_servers(other)):
                found[run] = other
                todo.append(other)
    return list(found.items())


def run_paused(record):
    """The run RECORD is paused: its run's marker (sandbox-ai pause of a run, a canary), or its session's."""
    if (Path(record["directory"]) / "paused").exists():
        return True
    start = record.get("start") if isinstance(record.get("start"), str) else None
    if start:
        with contextlib.suppress(Exception):  # an unreadable session: as its run says
            return (sessions_module().session_dir(start) / "paused").exists()
    return False


@contextlib.contextmanager
def cut_lock(directory):
    """The kill switch's requests to the run in DIRECTORY one after the other (cut_one, apply_pending_cut)."""
    fd = os.open(Path(directory) / "cut.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class Pending(Exception):
    """A cut asked for a paused run: kept (CUT_PENDING), set as it resumes (apply_pending_cut); or for a run that
    starts: kept (CUT_STARTING), set by its supervisor before the agent runs."""


def keep_request(directory, name, request):
    """REQUEST added to the list in DIRECTORY/NAME (at most CUT_PENDING_LIMIT, the newest kept); under cut_lock."""
    try:
        waiting = json.loads((Path(directory) / name).read_text())
    except (OSError, ValueError, RecursionError):
        waiting = []
    waiting = (waiting if isinstance(waiting, list) else [])[-(CUT_PENDING_LIMIT - 1):] + [request]
    write_json(directory, name, waiting)


def cut_one(record, mode, by, clear=False, timeout=CUT_SECONDS):
    """Ask the supervisor of the running RECORD for the kill switch MODE (CUT_MODES or "off") on behalf of BY (a run's
    name, or a list: the runs whose cut it is). "off" takes back only BY's cut (CLEAR: every one). The supervisor's
    answer, kept in the run's directory (CUT_FILE). A paused run keeps the request for its resume (Pending); a run of
    a sandbox-ai before 0.1.94 has none: refused, with what to do."""
    name = printable(str(label(record.get("run"), record)))
    older = (f"{name} was started by a sandbox-ai older than {CUT_VERSION}, which has no kill switch: stop it "
             f"(sandbox-ai stop {name}) or start it again with {CUT_VERSION} or newer")
    if not record.get("net_cut"):
        raise ValueError(older)
    request = {"op": "cut", "mode": mode, "by": by, **({"clear": True} if clear else {})}
    # Under the run's cut lock, which its resume takes to set the waiting cuts (apply_pending_cut): a run seen paused
    # here is still paused when its request is kept, or its resume has set what waited and this one is asked now -
    # never kept for a resume that has passed already, and never sent before what waited for the resume.
    with cut_lock(record["directory"]):
        if run_paused(record):
            keep_request(record["directory"], CUT_PENDING, request)
            raise Pending(f"{name} is paused: nothing leaves it now; the kill switch {mode} is set as it resumes, "
                          "before its sandbox-ai resume returns")
        ready = Path(record["directory"]) / CUT_READY
        if not ready.exists():
            # Kept first, looked again after: the supervisor writes CUT_READY first and reads CUT_STARTING after,
            # so either it sets this request before its agent runs, or it listens now and is asked below.
            keep_request(record["directory"], CUT_STARTING, request)
            if not ready.exists():
                raise Pending(f"{name} is starting: the kill switch {mode} is set before its agent runs (or it does "
                              "not start)")
        try:
            answer = ask(record["directory"], request, timeout=timeout)
        except ValueError as exc:
            if "op must be allow or revoke" in str(exc):  # a supervisor that knows no "cut"
                raise ValueError(older) from None
            raise
        cuts = answer.get("cuts") if isinstance(answer.get("cuts"), dict) else {}
        names = [by] if isinstance(by, str) else list(by)
        if (mode == "off" and (any(n in cuts for n in names) or clear and cuts)) or \
                (mode != "off" and any(cuts.get(n) != mode for n in names)):
            raise ValueError(f"{name}: its supervisor did not confirm the kill switch {mode}")
        write_cut_state(record["directory"], answer)
    return answer


def apply_pending_cut(directory):
    """The cuts asked for while the run in DIRECTORY was paused (CUT_PENDING), set now that it runs again (sandbox-ai
    resume calls this right after the thaw, before it returns). [(request, error or None)]."""
    try:
        with cut_lock(directory):  # a cut asked for meanwhile waits (cut_one), then goes straight to the supervisor
            return apply_pending_locked(directory)
    except FileNotFoundError:  # the run has ended: nothing to set
        return []


def apply_pending_locked(directory):
    path = Path(directory) / CUT_PENDING
    try:
        waiting = json.loads(path.read_text())
        record = json.loads((Path(directory) / "run.json").read_text())
    except (OSError, ValueError, RecursionError):
        return []
    record = {**record, "directory": str(directory)} if isinstance(record, dict) else {"directory": str(directory)}
    done = []
    for request in waiting if isinstance(waiting, list) else []:
        if not isinstance(request, dict):
            continue
        try:
            answer = ask(directory, {**request, "op": "cut"})
            write_cut_state(directory, answer)
            done.append((request, None))
        except (ValueError, OSError) as exc:
            done.append((request, str(exc)))
    if all(error is None for _, error in done):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    for request, error in done:
        if error:
            print(f"sandbox-ai: the kill switch {printable(str(request.get('mode')))} asked for while it was paused "
                  f"could not be set: {printable(error)}; set it again: sandbox-ai net cut "
                  f"{printable(str(record.get('run', '')))} {printable(str(request.get('mode')))}", file=sys.stderr)
    return done


def joined_cuts(run, directory=None):
    """{by: mode}: the cuts of the running runs that the run RUN (registered now, its supervisor not started yet)
    acts for: their own cuts (by themselves) that their MCP servers, helpers and team would hold. What the launcher
    hands the new supervisor, which sets them before the agent runs; also kept as the new run's CUT_FILE. Cheap when
    nothing is cut (no CUT_FILE anywhere)."""
    def any_cut():
        for base in run_dirs():
            try:
                entries = list(base.iterdir())
            except OSError:
                continue
            if any(entry.name != run and (entry / CUT_FILE).exists() for entry in entries):
                return True
        return False
    if not any_cut():
        return {}
    runs = running()
    found = {}
    for other, record in runs.items():
        if other == run:
            continue
        mode = cut_record(record["directory"]).get("cuts", {}).get(other)
        if mode and run in {r for r, _ in related_runs(runs, record)}:
            found[other] = mode
    if found and directory is not None:
        modes = sorted(set(found.values()))
        write_json(directory, CUT_FILE, {"mode": "all" if "all" in modes else "+".join(
            m for m in CUT_MODES if m in modes), "cuts": found})
    return found


def propagate_cut(record, runs=None):
    """The running RECORD's own cut (the one it holds by itself) for the runs that act for it now, as they joined
    later (sandbox-ai mcp attach --live): each one holds it as well. [(run, error or None)]."""
    runs = running() if runs is None else runs
    key = record.get("run")
    mode = cut_record(record["directory"]).get("cuts", {}).get(key)
    if not mode:
        return []
    done = []
    for run, other in related_runs(runs, record):
        if cut_record(other["directory"]).get("cuts", {}).get(key) == mode:
            continue
        try:
            cut_one(other, mode, key)
            done.append((run, None))
        except Pending:  # set as it resumes
            done.append((run, None))
        except (ValueError, OSError) as exc:
            done.append((run, str(exc)))
    return done


def cut_runs(runs, record, mode, out=print, alone=False, every=False, as_json=False):
    """The kill switch MODE for the running RECORD on its own behalf and, unless ALONE, for the runs that act for it
    (related_runs) on its behalf too; with EVERY for each of RUNS (and each one's on its behalf: a shared server holds
    every run's cut). Each run keeps every requester's cut and holds them together, so "off" for one run leaves a
    server cut that another run's cut still holds (said). All at once (an emergency waits for no run one after the
    other); each one's result said. 0 when every one confirmed it (a paused run's cut waits for its resume: said)."""
    wanted = {}  # run: [names on whose behalf]
    parents = {}
    sources = list(runs.items()) if every else [(record.get("run"), record)]
    for source, entry in sources:
        wanted.setdefault(source, []).append(source)
        if alone:
            continue
        for run, _ in related_runs(runs, entry):
            wanted.setdefault(run, []).append(source)
            if not every:
                parents.setdefault(run, source)
    targets = [(run, runs[run], parents.get(run)) for run in wanted if run in runs]
    results, answers, pending, threads = [], [], [], []

    def one(index, run, entry):
        try:
            answers[index] = cut_one(entry, mode, sorted(set(wanted[run])), clear=every and mode == "off")
        except Pending as exc:
            pending[index] = str(exc)
        except (ValueError, OSError) as exc:
            results[index] = getattr(exc, "strerror", None) or str(exc)

    def ask_all(first):
        for items in (results, answers, pending):
            items.extend([None] * (len(targets) - first))
        started = [threading.Thread(target=one, args=(index, run, entry), daemon=True)
                   for index, (run, entry, _) in enumerate(targets) if index >= first]
        threads.extend(started)
        for thread in started:
            thread.start()
        for thread in started:
            thread.join(CUT_SECONDS + 30)
    ask_all(0)
    if mode != "off" and not alone and targets:
        # A run registered after RUNS was read (an MCP server, helper or team member starting now) read the cuts
        # (joined_cuts) perhaps before these were confirmed: looked for once more now that they are, and cut too.
        fresh, first, late = running(), len(targets), set()
        for source, entry in sources:
            for run, other in related_runs(fresh, fresh.get(source, entry)):
                if run not in wanted:
                    late.add(run)
                    targets.append((run, other, None if every else source))
                if run in late:
                    wanted.setdefault(run, []).append(source)
        if len(targets) > first:
            ask_all(first)
    names = labels({run: entry for run, entry, _ in targets})
    failed = [index for index, (error, thread) in enumerate(zip(results, threads))
              if error is not None or thread.is_alive()]

    def holds(index):
        answer = answers[index] or {}
        return cut_label(answer.get("cut")), {by: m for by, m in (answer.get("cuts") or {}).items()}
    # Each run's result in launcher.log (said on standard output only, or as JSON for the app).
    for index, (run, entry, parent) in enumerate(targets):
        shown = f"{names.get(run, run)} (run {run}" + (f", acting for {names.get(parent, parent)})" if parent else ")")
        if index in failed:
            LOG.warning("net", f"kill switch {mode} for {shown} NOT {'lifted' if mode == 'off' else 'set'}: "
                               f"{results[index] or 'no answer in time'}")
        elif pending[index] is not None:
            LOG.info("net", f"kill switch {mode} for {shown}: waiting: {pending[index]}")
        else:
            LOG.info("net", f"kill switch {mode} for {shown}: confirmed by its supervisor; it holds now: "
                            f"{holds(index)[0] or 'none'}")
    if not targets:
        LOG.info("net", f"kill switch {mode}: no sandbox is running")
    if as_json:
        out(json.dumps({"mode": mode, "runs": [
            {"run": run, "name": names.get(run), "for": parent, "ok": index not in failed,
             "pending": pending[index] is not None,
             "cut": None if index in failed else holds(index)[0], "cuts": holds(index)[1],
             "error": (results[index] or "no answer in time") if index in failed else pending[index]}
            for index, (run, entry, parent) in enumerate(targets)]}, indent=2))
        return 1 if failed else 0
    if not targets:
        out("no sandbox is running")
        return 0
    for index, (run, entry, parent) in enumerate(targets):
        shown = printable(str(names.get(run, run)))
        why = f" (it acts for {printable(str(names.get(parent, parent)))})" if parent else ""
        if index in failed:
            out(f"{shown}: NOT {'lifted' if mode == 'off' else 'cut'}{why}: {printable(results[index] or 'no answer in time')}")
            continue
        if pending[index] is not None:
            out(f"{shown}{why}: {printable(pending[index])}")
            continue
        now, cuts = holds(index)
        others = sorted(by for by in cuts if by not in wanted[run])
        if mode == "off" and now:
            out(f"{shown}: still cut ({now}){why}: the cut of {', '.join(printable(o) for o in others)} holds there "
                f"too; lift it with: sandbox-ai net cut {printable(others[0]) if others else shown} off")
        elif mode == "off":
            out(f"{shown}: kill switch off{why}: {CUT_TEXT['off']}")
        elif now != mode:
            out(f"{shown}: kill switch {mode}{why}; with the cut of {', '.join(printable(o) for o in others)} it holds "
                f"{now}: only what every cut keeps stays reachable")
        else:
            out(f"{shown}: kill switch {mode}{why}: {CUT_TEXT[mode]}")
    if mode != "off" and len(failed) < len(targets) and not (isinstance(record, dict) and record.get("orphaned")):
        undo = "--all-runs" if every else printable(str(names.get(record.get("run"), record.get("run"))))
        out(f"undo: sandbox-ai net cut {undo} off")
    return 1 if failed else 0


# --- the kill switch for an orphaned run: its supervisor on the host is gone, its pod runs on --------------------------

ORPHAN_STOP_WAIT = 10  # seconds an orphaned run's pod has to end when the kill switch cannot reach its sandbox


def orphan_record(entry):
    """The orphaned start ENTRY (starts.entries) as the record the kill switch, net show and usage act on: its
    run.json with "directory" while its run directory is there (the sandbox's own supervisor still listens on its
    control socket), else what its start's directory knows (run, pod, Podman); "orphaned": True, "orphaned_why",
    "orphan" ({name, id, session}). None for any other entry (an MCP server's sandbox is not one)."""
    if not isinstance(entry, dict) or entry.get("state") != "orphaned" or entry.get("kind") == "mcp":
        return None
    run = entry.get("run") if isinstance(entry.get("run"), str) else None
    record = None
    if isinstance(entry.get("run_dir"), str) and entry["run_dir"]:
        try:
            found = json.loads((Path(entry["run_dir"]) / "run.json").read_text())
        except (OSError, ValueError, RecursionError):
            found = None
        if isinstance(found, dict) and found.get("run", run) == run:
            record = {**found, "directory": entry["run_dir"]}
    session = bool(entry.get("directory"))  # a start of its own (a session, a task): its directory outlives the run
    if record is None:
        data = {}
        if session:
            with contextlib.suppress(Exception):  # an unreadable start: only its pod's name (agent-RUN) is guessed
                data = sessions_module().load(entry["id"])
        record = {"run": run, "name": entry.get("name"), "backend": data.get("backend") or entry.get("backend"),
                  "pod": data.get("pod"), "podman_url": data.get("podman_url"), "directory": None}
    record.setdefault("run", run)
    why = entry.get("orphaned_why") if isinstance(entry.get("orphaned_why"), str) else None
    if not why:
        try:
            why = starts_module().orphaned_why(entry)
        except Exception:  # noqa: BLE001 - the general reason
            why = liveness().orphan_text(entry.get("name") or "NAME")
    return {**record, "orphaned": True, "orphaned_why": why,
            "orphan": {"name": entry.get("name") or run, "id": entry.get("id") or run, "session": session}}


def orphaned_runs():
    """{run: orphan_record} of the runs whose launcher is gone while their pod runs on and whose run directory is
    kept (running's ORPHANS; no MCP server's): what only reads (approvals list, usage) shows them, marked."""
    found = {}
    running(found)
    result = {}
    for run, record in found.items():
        if not isinstance(run, str) or record.get("kind") == "mcp" or run.startswith("mcp-"):
            continue
        start = record.get("start") if isinstance(record.get("start"), str) else None
        directory = None
        if start:
            with contextlib.suppress(Exception):  # (no such start: the run's own record says why)
                directory = str(sessions_module().session_dir(start))
        made = orphan_record({"name": label(run, record), "id": start or run, "run": run, "state": "orphaned",
                              "run_dir": record["directory"], "directory": directory})
        if made is not None:
            result[run] = made
    return result


def orphan_targets():
    """orphan_record of every orphaned start sandbox-ai list knows (net cut --all-runs: the emergency stop reaches them
    too); [] when they cannot be listed (said in launcher.log)."""
    try:
        found = starts_module().entries(prune=False)
    except Exception as exc:  # noqa: BLE001 - the running runs are cut all the same
        LOG.warning("net", "kill switch: the orphaned starts could not be listed", exc)
        return []
    return [record for record in map(orphan_record, found) if record is not None]


def cut_orphan(record, mode, wait=ORPHAN_STOP_WAIT):
    """The kill switch MODE for the orphaned run RECORD (orphan_record), on its own behalf: asked of the supervisor in
    its sandbox through its control socket as for any run, and kept when it confirms; when it does not (no run
    directory, no socket, no answer, a run without a kill switch, paused or starting), its pod is stopped instead -
    fail closed - recorded in launcher.log and in its start's history. {"cut", "cuts", "stopped", "why", "pod"}.
    ValueError when neither is confirmed, and for "off" (nothing would hold the network it gives back)."""
    orphan = record.get("orphan") or {}
    name, run = str(orphan.get("name") or record.get("run")), record.get("run")
    if mode == "off":
        raise ValueError(orphaned_text({"name": name, "orphaned_why": record.get("orphaned_why")}, "net cut off"))
    if record.get("directory") and not (Path(record["directory"]) / "control.sock").exists():
        why = "the sandbox does not answer: its control socket is gone"
    elif record.get("directory"):
        try:
            answer = cut_one(record, mode, [run])
            LOG.warning("net", f"kill switch {mode} for the orphaned {name} (run {run}): confirmed by the supervisor in "
                               f"its sandbox; it holds now: {cut_label(answer.get('cut')) or 'none'}")
            return {"cut": cut_label(answer.get("cut")), "cuts": answer.get("cuts") or {}, "stopped": False}
        except (Pending, ValueError, OSError) as exc:
            why = getattr(exc, "strerror", None) or str(exc)
    else:
        why = "its run directory is gone, and its control socket with it"
    pod = liveness().pod_of(record)
    if pod is None:
        raise ValueError(f"the kill switch could not reach it ({why}), and it names no pod to stop")
    url = record.get("podman_url") if isinstance(record.get("podman_url"), str) else None
    LOG.warning("net", f"kill switch {mode} for the orphaned {name} (run {run}): not confirmed ({why}); stopping its "
                       f"pod {pod}")
    stopped, said = liveness().stop_pod(pod, url, wait)
    if not stopped:
        LOG.error("net", f"kill switch {mode} for the orphaned {name} (run {run}): NOT cut ({why}) and its pod {pod} "
                         f"could not be stopped ({said or 'it still runs'})")
        raise ValueError(f"the kill switch could not reach it ({why}) and its pod {pod} could not be stopped "
                         f"({printable(said) or 'it still runs'}): end it now: podman pod rm --force {pod}")
    starts = starts_module()
    with contextlib.suppress(Exception):  # (its history: what list --history says; never fails the stop)
        starts.record(name, "stop", orphan.get("id") or run, run, by="kill switch", why=f"net cut {mode}: {why}"[:300])
    if record.get("directory"):
        with contextlib.suppress(Exception):
            running()  # it has ended: its end is recorded and its run directory goes (forget_ended)
    elif orphan.get("session"):
        with contextlib.suppress(Exception):
            starts.write_exit(orphan["id"], run, None)
            starts.record(name, "exit", orphan["id"], run, status=None)
    LOG.warning("net", f"kill switch {mode} for the orphaned {name} (run {run}): its pod {pod} was stopped")
    return {"cut": None, "cuts": {}, "stopped": True, "why": why, "pod": pod}


def cut_orphans(records, mode):
    """cut_orphan for each of RECORDS at once (an emergency waits for none of them one after the other): [its result
    as net cut --json says it ({"run", "name", "for", "ok", "pending", "cut", "cuts", "error", "orphaned",
    "stopped"}), with "said": its line]."""
    results = [None] * len(records)

    def entry(record):
        name = str((record.get("orphan") or {}).get("name") or record.get("run"))
        return {"run": record.get("run"), "name": name, "for": None, "ok": False, "pending": False, "orphaned": True,
                "stopped": False, "cut": None, "cuts": {}, "error": None}

    def one(index, record):
        found = entry(record)
        shown = printable(found["name"])
        try:
            done = cut_orphan(record, mode)
        except (ValueError, OSError) as exc:
            error = getattr(exc, "strerror", None) or str(exc)
            results[index] = {**found, "error": error,
                              "said": f"{shown}: NOT {'lifted' if mode == 'off' else 'cut'}: {printable(error)}"}
            return
        if done["stopped"]:
            resume = f" Continue it: sandbox-ai resume {shown}" if (record.get("orphan") or {}).get("session") else ""
            results[index] = {**found, "ok": True, "stopped": True,
                              "said": f"{shown}: the kill switch could not reach it ({printable(done['why'])}), so its "
                                      f"pod {printable(done['pod'])} was stopped: nothing of it runs now.{resume}"}
            return
        now = done["cut"]
        holds = f"; with the other cuts there it holds {now}" if now and now != mode else ""
        results[index] = {**found, "ok": True, "cut": now, "cuts": done["cuts"],
                          "said": f"{shown}: kill switch {mode}{holds}: {CUT_TEXT[mode]}. It is orphaned: "
                                  f"{printable(str(record.get('orphaned_why') or ''))}"}
    threads = [threading.Thread(target=one, args=(index, record), daemon=True) for index, record in enumerate(records)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(CUT_SECONDS + ORPHAN_STOP_WAIT + 4 * liveness().PODMAN_TIMEOUT + 30)
    return [result if result is not None else
            {**entry(record), "error": "no answer in time",
             "said": f"{printable(entry(record)['name'])}: NOT cut: no answer in time"}
            for result, record in zip(results, records)]


def cut_with_orphans(runs, record, mode, orphans, out=print, alone=False, every=False, as_json=False):
    """The kill switch MODE for RUNS (cut_runs: with EVERY each one, else RECORD's related runs unless ALONE - RECORD
    being an orphan, which is not among RUNS) and for ORPHANS (orphan_record; or a function that gives them: looked
    for once RUNS are cut, an emergency waits for nothing): one report, as cut_runs gives it."""
    said, status = [], 0
    others = bool(runs) if every else bool(not alone and record is not None and related_runs(runs, record))
    if others:
        status = cut_runs(runs, record, mode, said.append if as_json else out, alone=alone, every=every,
                          as_json=as_json)
    orphans = orphans() if callable(orphans) else orphans
    results = cut_orphans(orphans, mode) if orphans else []
    if as_json:
        report = json.loads("\n".join(said)) if said else {"mode": mode, "runs": []}
        report["runs"] = list(report.get("runs") or []) + [
            {key: value for key, value in result.items() if key != "said"} for result in results]
        out(json.dumps(report, indent=2))
    else:
        for result in results:
            out(result["said"])
        if not others and not results:
            out("no sandbox is running")
    return 1 if status or any(not result["ok"] for result in results) else 0


def resolve(hosts):
    """{host: sorted addresses} for TCP/443 from this machine's resolver."""
    result = {}
    for host in hosts:
        try:
            result[host] = sorted({info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        except OSError as exc:
            raise ValueError(f"cannot resolve {host}: {exc}") from None
        if not result[host]:
            raise ValueError(f"{host} has no address")
    return result


def vpn_resolver(record):
    """For a run started with --vpn: a resolver NAME -> [addresses] through the VPN's own resolvers (in its running
    container), else None: a name allowed during such a run is never looked up past the VPN."""
    if not (isinstance(record, dict) and record.get("vpn_container")):
        return None
    return launcher().load_vpn_module().resolver_for_run(record)


def tor_record(record):
    """Whether the run RECORD (run.json) is behind a Tor profile: its "vpn_type", or for a run of an older version its
    profile's type (a profile that cannot be read counts as Tor: nothing is opened past it on a guess)."""
    if not isinstance(record, dict) or not record.get("vpn_profile"):
        return False
    if record.get("vpn_type"):
        return record["vpn_type"] == "tor"
    try:
        return launcher().load_vpn_module().load_profile(record["vpn_profile"]).get("type") == "tor"
    except (OSError, ValueError, AttributeError):
        return True


def resolve_with(one):
    """resolve() with ONE (name -> [addresses]) instead of this machine's resolver."""
    def resolve_all(hosts):
        result = {}
        for host in hosts:
            try:
                result[host] = one(host)
            except OSError as exc:
                raise ValueError(f"cannot resolve {host}: {exc}") from None
            if not result[host]:
                raise ValueError(f"{host} has no address")
        return result
    return resolve_all


def public_resolver(resolve_all, notes):
    """RESOLVE_ALL (hosts -> {host: addresses}) keeping only each name's public addresses, as an answered lookup
    opens them (approvals.public_addresses: no loopback, private, link-local or NAT64 address); ValueError for a name
    with none. NOTES gets what was left out."""
    def resolve_public(hosts):
        found = {}
        for host, addresses in resolve_all(hosts).items():
            public = approvals_module().public_addresses(host, addresses)
            if len(public) < len(addresses):
                notes.append(f"{host}: not opened (not public): "
                             + ", ".join(address for address in addresses if address not in public))
            found[host] = public
        return found
    return resolve_public


def request_for(args, op, resolver=resolve):
    """(request for the supervisor, {host: addresses} for /etc/hosts) from the command line."""
    l = launcher()
    for value in getattr(args, "allow_domain", None) or []:
        l.domain_of(value)  # as the start refuses it, naming --allow-domain (expand_domains left it out)
    wildcards = sorted({h for h in args.allow_host if h.startswith("*.")})
    confirmed = set(getattr(args, "shared_confirmed", None) or ())
    shared = []
    for wildcard in wildcards:
        try:
            # A revoke only narrows: no confirmation. An allow below a shared service: only what the human confirmed.
            l.RUNTIME.wildcard_suffix(wildcard, shared_ok=op != "allow" or wildcard in confirmed)
        except l.RUNTIME.SharedWildcardError as exc:
            raise ValueError(f"--allow-host {wildcard}: {exc} (on a terminal net allow asks; --yes answers yes for "
                             "scripts)") from None
        except ValueError as exc:
            raise ValueError(f"--allow-host {wildcard}: {exc}") from None
        if op == "allow" and l.RUNTIME.shared_wildcard(wildcard):
            shared.append(wildcard)
    hosts = list(dict.fromkeys([h for h in args.allow_host if not h.startswith("*.")]
                               + [h for preset in args.presets for h in l.PRESETS[preset]]))
    for host in hosts:
        # As the start checks it (ASCII labels only: a name in another script is given in its xn-- form), so that
        # --keep never writes a name every later start refuses.
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                                      for part in host.split(".")):
            raise ValueError(f"invalid host name: {host!r} (a name outside ASCII in its xn-- form)")
    for value in args.allow:
        if "%" in value:
            raise ValueError(f"--allow does not accept an IPv6 scope ID: {value!r}")
        ipaddress.ip_network(value, strict=False)
    tcp = l.tcp_targets(argparse.Namespace(allow_tcp=args.allow_tcp))
    names = resolver(hosts) if op == "allow" else {}
    https = sorted({address for addresses in names.values() for address in addresses}
                   | set(l.host_networks(hosts)))
    request = {"op": op, "nets": list(args.allow), "https": https, "tcp": [[a, p] for a, p in tcp]}
    ports = sorted(set(getattr(args, "allow_host_port", None) or []))
    if ports:
        request["host_ports"] = ports
    servers = list(dict.fromkeys(getattr(args, "nameserver", []))) or \
        (list(l.DEFAULT_NAMESERVERS) if getattr(args, "dns", False) else [])
    for server in servers:
        if "%" in server:
            raise ValueError(f"--nameserver does not accept an IPv6 scope ID: {server!r}")
        try:
            ipaddress.ip_address(server)
        except ValueError:
            raise ValueError(f"--nameserver {server}: an IP address") from None
    if servers:
        request["dns"] = servers
    if wildcards:
        request["wildcards"] = wildcards  # not for the network change: control op "wildcards" (see change_wildcards)
    if shared:
        request["shared_wildcards"] = shared  # their risk confirmed (the control op's "shared")
    return request, names, hosts


def wildcards_file(record):
    """The wildcards added during the run (run directory, outside the sandbox): approvals.watch reads them."""
    return Path(record["directory"]) / "wildcards.json"


def shared_file(record):
    """The wildcards below a shared service whose risk you confirmed with `sandbox-ai net allow` (run directory,
    outside the sandbox; written from your command, not from the supervisor's answer): net show --keep keeps only
    those."""
    return Path(record["directory"]) / "shared-wildcards.json"


def confirmed_shared(record):
    """Every wildcard below a shared service whose risk you confirmed for the run: at the start (run.json
    shared_wildcards) and with `sandbox-ai net allow` since."""
    found = [w for w in (record.get("shared_wildcards") or {}) if isinstance(w, str)]
    try:
        added = json.loads(shared_file(record).read_text())
    except (OSError, ValueError, RecursionError, KeyError):
        added = []
    found += [w for w in added if isinstance(w, str)] if isinstance(added, list) else []
    return sorted(set(found))


def shared_of(wildcards):
    """{"*.DOMAIN": the shared suffix} of those of WILDCARDS below a shared service (marked as a risk)."""
    found = {}
    for wildcard in wildcards:
        with contextlib.suppress(ValueError):
            shared = launcher().RUNTIME.shared_wildcard(wildcard)
            if shared:
                found[wildcard] = shared
    return found


def confirm_shared(args, ask=None):
    """The wildcards below a shared service of an allow (--allow-host '*.DOMAIN') not confirmed by
    --allow-shared-wildcard: confirmed by --yes, or by your yes to "allow anyway? [y/N]" on a terminal (ASK: the
    question, for tests). What stays unconfirmed is refused by request_for, naming --allow-shared-wildcard."""
    for wildcard in dict.fromkeys(h for h in args.allow_host if h.startswith("*.")):
        if wildcard in args.shared_confirmed:
            continue
        try:
            risk = launcher().RUNTIME.shared_wildcard_risk(wildcard)
        except ValueError:
            risk = None
        if not risk:
            continue
        if getattr(args, "yes", False):
            args.shared_confirmed.add(wildcard)
            continue
        question = ask
        if question is None and sys.stdin.isatty() and sys.stderr.isatty():
            def question(text):
                print(f"sandbox-ai net: {launcher().printable(text)}", file=sys.stderr)
                try:
                    return input("sandbox-ai net: allow anyway? [y/N] ").strip().lower() in ("y", "yes")
                except EOFError:
                    return False
        if question is not None and question(f"--allow-host {wildcard}: {risk}"):
            args.shared_confirmed.add(wildcard)


def run_wildcards(record):
    """Every *.DOMAIN of the run: the start's (run.json allow_wildcards) and those added since."""
    found = [w for w in record.get("allow_wildcards") or [] if isinstance(w, str)]
    try:
        added = json.loads(wildcards_file(record).read_text())
    except (OSError, ValueError, RecursionError):
        added = []
    found += [w for w in added if isinstance(w, str)] if isinstance(added, list) else []
    return sorted(set(found))


def change_wildcards(record, op, wildcards, apply=True, shared=()):
    """Add (allow) or take back (revoke) WILDCARDS during the run: the supervisor's DNS stub holds the lookups below
    them, the launcher answers those on the host. Only for a run that asks questions. SHARED: those below a shared
    service whose risk you confirmed (refused where the administrator's policy forbids them, as the supervisor
    refuses them in a strict mode). APPLY False only checks (before anything of the command changes)."""
    l = launcher()
    started = set(record.get("allow_wildcards") or [])
    if op == "allow":
        try:
            asks = json.loads((Path(record["session_dir"]) / "config.json").read_text()).get("ask")
        except (OSError, ValueError, KeyError, RecursionError):
            asks = None
        if not asks:
            raise ValueError("--allow-host *.DOMAIN (and --allow-domain) needs a sandbox that asks (not one started "
                             "with --ask off): each name below it is opened as its lookup is answered")
        try:
            resolv = (Path(record["session_dir"]) / "resolv.conf").read_text()
        except (OSError, KeyError):
            resolv = ""
        if "nameserver 127.0.0.1" not in resolv:
            raise ValueError("--allow-host *.DOMAIN (and --allow-domain): this sandbox resolves names past its DNS "
                             "stub (started with --dns or --nameserver), so the names below it would resolve but "
                             "stay closed; name the hosts one by one")
        policy = l.load_policy()
        ceiling = argparse.Namespace(policy={k: policy[k] for k in ("allow_within", "allow_hosts") if k in policy})
        outside = [w for w in wildcards if not l.within_ceiling(ceiling, host=w)]
        if outside:
            raise ValueError(f"{', '.join(outside)} is not among the hosts the administrator's policy allows "
                             "(allow_hosts)")
        if shared and (policy.get("forbid_shared_wildcards") is True or
                       "--allow-shared-wildcard" in (policy.get("forbid") or [])):
            raise ValueError(f"{', '.join(shared)}: a wildcard below a shared service, which the administrator's "
                             "policy forbids (forbid_shared_wildcards); give the hosts by name instead")
    else:
        fixed = sorted(set(wildcards) & started)
        if fixed:
            raise ValueError(f"{', '.join(fixed)} was given at the start: it stays until the run ends")
        unknown = sorted(set(wildcards) - set(run_wildcards(record)))
        if unknown:
            raise ValueError(f"{', '.join(unknown)} was not allowed during this run")
    if not apply:
        return None
    # The supervisor's answer and the file written from it, as one step under the run's lock: two commands at once
    # (allow and revoke) must not write their answers in the other order, nor share a temporary file.
    with hosts_lock(record):
        answer = ask(record["directory"], {"op": "wildcards", "add" if op == "allow" else "remove": wildcards,
                                           **({"shared": list(shared)} if shared else {})})
        now = sorted(f"*.{suffix}" for suffix in answer.get("wildcards", []))
        if shared:  # your confirmation, recorded from your command (not from the supervisor's answer)
            confirmed = sorted(set(confirmed_shared({"directory": record["directory"]})) | set(shared))
            write_json_atomic(shared_file(record), confirmed)
        target = wildcards_file(record)
        fd, temporary = tempfile.mkstemp(prefix=".wildcards.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps(now))
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return now


def write_json_atomic(target, value):
    """VALUE as JSON into TARGET, replaced in one step (a temporary file of its own beside it)."""
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value))
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# Longer than a firewall change may take in the sandbox (runtime.NFT_SECONDS, 30 s): a change the supervisor
# applies late must not leave this command believing it failed, with names.json never written.
ASK_SECONDS = 45


def ask(directory, request, timeout=ASK_SECONDS, fds=()):
    """The supervisor's answer to REQUEST on the run's control socket; ValueError when it refuses, does not answer
    (paused, or busy longer than TIMEOUT) or the socket fails. FDS: descriptors handed over with it (SCM_RIGHTS;
    sandbox-ai mcp attach --live hands over an MCP server's socket directory)."""
    if (Path(directory) / "paused").exists():
        try:
            record = json.loads((Path(directory) / "run.json").read_text())
            name = launcher().printable(str(label(record.get("run") or Path(directory).name, record)))
        except (OSError, ValueError, AttributeError, RecursionError):
            name = "NAME"
        raise ValueError(f"the sandbox is paused: resume it first (sandbox-ai resume {name}), then change it")
    if sys.platform == "darwin":  # podman machine: the control socket is in its VM
        try:
            record = json.loads((Path(directory) / "run.json").read_text())
        except (OSError, ValueError, RecursionError):
            record = {}
        if launcher().machine_record(record):
            return ask_machine(directory, record, request, timeout, fds)
    try:
        return ask_socket(directory, request, timeout, fds)
    except (OSError, TimeoutError) as exc:  # socket.timeout is an OSError
        raise ValueError(f"the sandbox did not answer ({getattr(exc, 'strerror', None) or exc or 'timed out'}); "
                         "see sandbox-ai net show") from None


def ask_machine(directory, record, request, timeout, fds=(), runner=None):
    """macOS: REQUEST to the supervisor in podman machine's VM, where its control socket is (a Unix socket does not
    cross the VM's shared folders): runtime.py --internal-control-client through `podman exec`, the request with the
    run's token (runtime.VM_CONTROL_DIR) on its stdin, the answer on its stdout. Descriptors cannot go along."""
    if fds:
        raise ValueError("handing a socket to a running sandbox is not possible on macOS (podman machine's VM does "
                         "not share them); attach the server at the next start instead")
    try:
        token = (Path(directory) / launcher().RUNTIME.EXEC_TOKEN).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"the run has no token for podman exec ({Path(directory) / 'exec.token'}): it was started "
                         "by an older sandbox-ai; start it again") from None
    command = launcher().machine_exec(record, ["--internal-control-client"])
    try:
        done = (runner or subprocess.run)(command, input=json.dumps({**request, "exec_token": token}) + "\n",
                                          capture_output=True, text=True, timeout=timeout + 15, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("the sandbox did not answer (timed out); see sandbox-ai net show") from None
    except OSError as exc:
        raise ValueError(f"podman exec did not run ({exc.strerror or exc})") from None
    lines = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or not lines:
        why = launcher().printable((done.stderr or "").strip().splitlines()[-1][:300]) if (done.stderr or "").strip() \
            else f"status {done.returncode}"
        raise ValueError(f"the sandbox did not answer through podman machine ({why}); see sandbox-ai net show")
    try:
        result = json.loads(lines[-1])
    except (ValueError, RecursionError):
        raise ValueError("the sandbox's answer was not readable") from None
    if not isinstance(result, dict):
        raise ValueError("the sandbox's answer was not readable")
    if not result.get("ok"):
        raise ValueError(f"the sandbox refused: {result.get('error', 'no answer')}")
    return result


def ask_socket(directory, request, timeout, fds=()):
    path = Path(directory) / "control.sock"
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        # Through the directory's descriptor: socket paths are limited to 107 bytes, a HOME may be longer.
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            client.connect(f"/proc/self/fd/{fd}/control.sock")
        except FileNotFoundError:
            # (registered, its supervisor not listening yet - a start still coming up - or gone since)
            raise ValueError(f"the sandbox does not answer yet: its control socket {path} is not there - it is still "
                             "starting (try again once it is up) or has ended (sandbox-ai list)") from None
        except OSError as exc:
            raise ValueError(f"the sandbox does not answer on {path}: {exc.strerror or exc}") from None
        finally:
            os.close(fd)
        data = json.dumps(request).encode() + b"\n"
        if fds:  # the descriptors go with the first byte; the rest as usual
            sent = socket.send_fds(client, [data], list(fds))
            if sent < len(data):  # never an empty send: the supervisor may have answered and closed already
                client.sendall(data[sent:])
        else:
            client.sendall(data)
        answer = b""
        while not answer.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            answer += chunk
    result = json.loads(answer or b"{}")
    if not result.get("ok"):
        raise ValueError(f"the sandbox refused: {result.get('error', 'no answer')}")
    return result


GIT_KINDS = ("env", "file", "secret")


GIT_LOGINS_FILE = "git-logins.json"  # in the run's directory: the Git logins its proxy was given during the run


def sandbox_git_logins(record):
    """[{"url", "user", "token"}] of the Git logins given into the sandbox during the run (the session directory's
    file, which the sandbox's Git helper reads - and its agent can)."""
    try:
        data = json.loads((Path(record["session_dir"]) / launcher().RUNTIME_GIT_TOKENS).read_text())
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return []
    logins = data.get("logins") if isinstance(data, dict) else None
    return [entry for entry in logins if isinstance(entry, dict) and isinstance(entry.get("url"), str)] \
        if isinstance(logins, list) else []


def proxy_git_file(record):
    """The run's list of the Git logins its proxy was given during the run, on the host (its run directory: yours,
    hidden from the agent, as api-keys.json): what `revoke` and a later `give` start from. The proxy gets the logins
    from the supervisor."""
    directory = record.get("directory")
    return Path(directory) / GIT_LOGINS_FILE if isinstance(directory, str) and directory else None


def proxy_git_logins(record):
    """[{"url", "user", "host", "port", "loopback", "token"}] of the Git logins the run's proxy was given during
    the run."""
    path = proxy_git_file(record)
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError, RecursionError):
        return []
    logins = data.get("logins") if isinstance(data, dict) else None
    return [entry for entry in logins if isinstance(entry, dict) and isinstance(entry.get("url"), str)] \
        if isinstance(logins, list) else []


def git_logins(record):
    """[{"url", "user", "token", "where"}] of every Git login given during the run: "where" is "proxy" (the run's
    connection proxy logs in on the wire: no token in the sandbox) or "sandbox" (the session directory's file for
    the sandbox's Git helper, which the agent can read: only where the proxy cannot log in)."""
    return [{**entry, "where": "proxy"} for entry in proxy_git_logins(record)] + \
        [{**entry, "where": "sandbox"} for entry in sandbox_git_logins(record)]


def proxy_login_refusal(record, credential):
    """Why the run's connection proxy cannot log in to CREDENTIAL's server with a login given now (None: it can):
    the run has no proxy, was started by a sandbox-ai whose supervisor hands no login to it, or does not open that
    server's requests (--no-inspect)."""
    l = launcher()
    if not record.get("proxy"):
        return "it runs without the connection proxy (--no-proxy)"
    if not record.get("proxy_git_logins") or proxy_git_file(record) is None:
        return "it was started by an older sandbox-ai, whose proxy takes no login during the run"
    if not credential["loopback"] and any(l.host_matches_any(credential["host"], value)
                                          for value in record.get("no_inspect") or []):
        return f"it was started with --no-inspect {credential['host']}: its proxy does not open that server's requests"
    return None


@contextlib.contextmanager
def git_logins_lock(record):
    """The run's lock for the Git logins its proxy was given (read, give and keep as one step)."""
    fd = os.open(Path(record["directory"]) / "git-logins.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_proxy_git_logins(record, entries, options=()):
    """Give the run's proxy ENTRIES (the whole list of the logins given during the run) through its supervisor
    (control op "git-logins": the proxy runs as a user of its own, which reads them in its run directory - no token
    enters the sandbox), then keep the list here: 0600, written whole and renamed into place. Both go when the run
    ends. OPTIONS: the --git-token-* options of this command (the supervisor holds them to the policy too)."""
    path = proxy_git_file(record)
    if path is None or not ours(path.parent):
        raise ValueError("the run's directory is gone or not yours")
    try:
        ask(record["directory"], {"op": "git-logins", "options": sorted(options), "git_logins": [
            {key: entry[key] for key in ("host", "port", "user", "loopback", "token")} for entry in entries]})
    except ValueError as exc:
        if "op" in str(exc) and "allow" in str(exc):  # a supervisor that knows no "git-logins"
            raise ValueError(f"{launcher().printable(record.get('run', '?'))} takes no Git login for its proxy during "
                             "the run: it was started by an older sandbox-ai; start it again, or give the login "
                             "into the sandbox (--git-token-in-sandbox: the agent can read the token)") from None
        raise
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "logins": entries}, stream)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def write_git_logins(record, logins):
    """Replace the Git logins given INTO THE SANDBOX with LOGINS: a file of yours (0600) in the run's session
    directory, written whole and renamed into place, so the sandbox's helper never reads half of it. The agent can
    read it: only for a run whose proxy cannot log in, and only when the human said so (--git-token-in-sandbox). The
    session directory goes when the run ends, and the logins with it."""
    directory = Path(record["session_dir"])
    if not ours(directory):
        raise ValueError(f"the run's session directory {directory} is gone or not yours")
    target = directory / launcher().RUNTIME_GIT_TOKENS
    temporary = directory / f".{launcher().RUNTIME_GIT_TOKENS}.{os.getpid()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "logins": logins}, stream)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def register_sandbox_logins(record, asker=None):
    """The Git logins given INTO the sandbox (its agent can read their tokens) registered with the run's secret
    guard: the whole list, through the supervisor (control op "secret-values"), so that the proxy reports - or, where
    the run says so, holds - such a token on its way to another host than its own. True if the run took them; False
    for a run without the connection proxy or of a sandbox-ai older than the secret guard (nothing looks there).
    No token is printed or kept anywhere else for it."""
    guard = launcher().load_secret_guard_module()
    values = []
    for entry in sandbox_git_logins(record):
        host = (urllib.parse.urlsplit(entry["url"]).hostname or "").lower()
        value = guard.value_entry(f"git-login:{host}", f"the Git login for {host} (given into the sandbox)",
                                  entry.get("token"), guard.server_hosts(host)) if host else None
        if value is not None:
            values.append(value)
    try:
        (asker or ask)(record["directory"], {"op": "secret-values", "values": guard.added_entries(values)})
    except (ValueError, KeyError):
        return False
    return True


def api_keys_file(record):
    """The run's list of the API keys given during the run, on the host (its run directory: yours, hidden from the
    agent): what `revoke` and a later `allow` start from. The proxy gets the keys from the supervisor."""
    directory = record.get("directory")
    return Path(directory) / "api-keys.json" if isinstance(directory, str) and directory else None


def added_api_keys(record):
    """[{host, port, name, placeholder, key}] given during the run."""
    path = api_keys_file(record)
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError, RecursionError):
        return []
    entries = data.get("api_keys") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)] \
        if isinstance(entries, list) else []


@contextlib.contextmanager
def api_keys_lock(record):
    """The run's lock for its API keys (a file of its own: hosts_lock is taken inside the network change)."""
    fd = os.open(Path(record["directory"]) / "api-keys.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_api_keys(record, entries):
    """Give the run's proxy ENTRIES (the whole list) through its supervisor (control op "api-keys": the proxy runs
    as a user of its own, which reads them in its run directory), then keep the list here: 0600, written whole and
    renamed into place. Both go when the run ends."""
    path = api_keys_file(record)
    if path is None or not ours(path.parent):
        raise ValueError("the run's directory is gone or not yours")
    try:  # (the name of each key's secret stays here: the proxy gets what it puts in, no more)
        ask(record["directory"], {"op": "api-keys", "api_keys": [{k: v for k, v in entry.items() if k != "secret"}
                                                                 for entry in entries]})
    except ValueError as exc:
        if "op" in str(exc) and "allow" in str(exc):  # a supervisor before 0.1.92 knows no "api-keys"
            raise ValueError(f"{launcher().printable(record.get('run', '?'))} takes no API key during the run: it "
                             "was started by a sandbox-ai older than 0.1.92; start it again with --api-key") from None
        raise
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "api_keys": entries}, stream)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def api_key_requests(args, record):
    """[(host, NAME, SECRET)] of --api-key, checked as a start checks them: well-formed, not a variable of the
    sandbox's own, the policy's forbid, a run whose proxy takes keys during the run. For revoke only NAME counts."""
    l = launcher()
    if "--api-key" in (l.load_policy().get("forbid") or []):
        raise ValueError("--api-key is forbidden by the administrator's policy")
    if args.command == "revoke":
        names = [value.split("=")[1] if "=" in value else value for value in args.api_key]
        given = {entry["name"] for entry in added_api_keys(record)}
        missing = [name for name in names if name not in given]
        if missing:
            raise ValueError(f"no API key {', '.join(missing)} was given during this run")
        return [(None, name, None) for name in names]
    keys = l.api_key_values(argparse.Namespace(api_key=args.api_key))
    run = l.printable(record.get("run", args.run))
    if not record.get("proxy") or api_keys_file(record) is None:
        raise ValueError(f"{run} takes no API key during the run: it runs without the connection proxy (which puts "
                         "keys in on the wire); start it again with --api-key")
    version = re.findall(r"\d+", str(record.get("version") or "0"))
    if tuple(int(v) for v in version[:3]) < (0, 1, 92):
        # Refused before anything changes (its supervisor knows no "api-keys"; the host would be opened for nothing).
        raise ValueError(f"{run} takes no API key during the run: it was started by a sandbox-ai older than 0.1.92; "
                         "start it again with --api-key")
    uninspected = [host for host, _, _ in keys
                   if any(l.host_matches_any(host, value) for value in record.get("no_inspect") or [])]
    if uninspected:
        raise ValueError(f"--api-key: {run} was started with --no-inspect {uninspected[0]}: the proxy does not open its "
                         "requests, so a key would never be put in")
    names = [name for _, name, _ in keys]
    for _, name, _ in keys:
        if names.count(name) > 1:
            raise ValueError(f"--api-key: the variable {name} is given twice")
        if name in l.SANDBOX_OWNED:
            raise ValueError(f"Sandbox-owned variable cannot be overridden: {name}")
    return keys


def git_requests(args):
    """[(kind, credential)] of the --git-token-* options (credential: launcher().git_token_credential)."""
    found = []
    for kind in GIT_KINDS:
        for value in getattr(args, f"git_token_{kind}", None) or []:
            found.append((kind, launcher().git_token_credential(value, kind)))
    urls = [credential["url"] for _, credential in found]
    duplicate = next((url for url in urls if urls.count(url) > 1), None)
    if duplicate:
        raise ValueError(f"{duplicate} is given twice")
    return found


def named_token_files(values):
    """The --git-token-file values (URL=FILE) with FILE as the file it names from here: a relative path is this
    command's, and what is said ("start with: ...") and kept (--keep, the project file) is read by a start in
    another directory. A path with ~ stays (the start expands it)."""
    found = []
    for value in values or []:
        url, sep, file = str(value).partition("=")
        if sep and file and not file.startswith("~") and not os.path.isabs(file):
            file = os.path.abspath(file)
        found.append(f"{url}{sep}{file}")
    return found


def run_trees(record):
    """The host folders the agent of the run RECORD can write, as its start recorded them (run.json
    "workspace_checks": its workspaces, rw mounts and HOME) and as they were added since (workspace add)."""
    checks = record.get("workspace_checks") if isinstance(record.get("workspace_checks"), dict) else {}
    found = [entry.get("host") for entry in checks.get("workspaces") or [] if isinstance(entry, dict)]
    found += list(checks.get("trees") or [])
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        found += [entry.get("host") for entry in load_workspaces_module().added_workspaces(record)]
    return [Path(tree) for tree in dict.fromkeys(found) if isinstance(tree, str) and os.path.isabs(tree)]


def check_token_file(record, path):
    """A --git-token-file given during the run, held as a start holds it (the launcher's check_host_source): on a
    way through a link in a folder a sandbox could write it is refused (an agent may have planted the link, and the
    file it leads to would be read and written into the sandbox's git-tokens.json); inside a folder the agent can
    change it is given, with the start's warning."""
    l = launcher()
    trees = list(dict.fromkeys([*run_trees(record), *l.exposed_before()]))
    l.check_host_source(None, path, "--git-token-file", trees)


def git_network(args, logins):
    """Open what each login's server needs, as at the start: github.com its preset, another HTTPS server its name
    (TCP/443; another port needs --allow-tcp). A server on the host's loopback is reached only if the run forwards
    its port (--allow-host-port at the start): notes for those."""
    notes = []
    for _, credential in logins:
        if credential["loopback"]:
            if credential["port"] not in (getattr(args, "allow_host_port", None) or []):
                notes.append(f"{credential['url']} is on the host's loopback: the sandbox reaches it if it was started "
                             f"with --allow-host-port {credential['port']} (or a login for it), or relay it now: "
                             f"sandbox-ai net allow --allow-host-port {credential['port']}")
        elif credential["url"] == "https://github.com":
            if "github" not in args.presets:
                args.presets.append("github")
        elif credential["port"] != 443:
            notes.append(f"{credential['url']}: port {credential['port']} needs --allow-tcp as well")
        elif credential["host"] not in args.allow_host:
            args.allow_host.append(credential["host"])
    return notes


def remembered(record):
    """{host: {"addresses": [...], "lines": [[address, host], ...]}} of the names allowed during the run;
    "lines" are the /etc/hosts entries this command added (not those the sandbox started with)."""
    try:
        data = json.loads((Path(record["directory"]) / "names.json").read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def update_hosts(record, names, remove=()):
    """Add NAMES ({host: addresses}) to the sandbox's /etc/hosts and drop what was added for the hosts in
    REMOVE; the file is changed in place, so the sandbox sees it at once. Under the run's lock: answers to lookups
    below a wildcard arrive in threads of their own, beside `net allow`."""
    with hosts_lock(record):
        return update_hosts_locked(record, names, remove)


@contextlib.contextmanager
def hosts_lock(record):
    fd = os.open(Path(record["directory"]) / "names.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def update_hosts_locked(record, names, remove=()):
    hosts_file = Path(record["session_dir"]) / "hosts"
    added = remembered(record)
    dropped = {tuple(line) for host in remove for line in added.pop(host, {}).get("lines", [])}
    lines = [line for line in hosts_file.read_text().splitlines() if tuple(line.split()[:2]) not in dropped]
    known = {tuple(line.split()[:2]) for line in lines}
    for host, addresses in names.items():
        entry = added.setdefault(host, {"addresses": [], "lines": []})
        # A name allowed again may have new addresses: all of them are kept.
        entry["addresses"] = sorted(set(entry["addresses"]) | set(addresses))
        for address in addresses:
            if (address, host) not in known:
                lines.append(f"{address} {host}")
                known.add((address, host))
                entry["lines"].append([address, host])
    with open(hosts_file, "r+") as stream:  # the same file: the sandbox has it bound to /etc/hosts
        stream.write("\n".join(lines) + "\n")
        stream.truncate()
    (Path(record["directory"]) / "names.json").write_text(json.dumps(added))
    return added


def bare(net):
    """An address without its /32 or /128; other networks stay CIDR."""
    try:
        network = ipaddress.ip_network(net, strict=False)
    except ValueError:
        return str(net)
    return str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network)


def address_sort(value):
    try:
        network = ipaddress.ip_network(value, strict=False)
        return (network.version, network.network_address.packed, network.prefixlen)
    except ValueError:
        return (9, str(value).encode(), 0)


def tainted_text(state):
    """One line about a taint state: since when, from where."""
    if not isinstance(state, dict) or not state.get("tainted"):
        return "not tainted"
    source = state.get("source") if isinstance(state.get("source"), dict) else {}
    rule = f", rule {source['rule']} of {source.get('ruleset', '?')}" if source.get("rule") else ""
    more = int(state.get("sources", 1)) - 1 if isinstance(state.get("sources"), int) else 0
    return f"tainted since {state.get('since', '?')} by {source.get('what', '?')}{rule}" + \
        (f" (and {more} later source{'s' if more != 1 else ''})" if more > 0 else "")


def show(result, out=print, names=None, run=None, as_json=False, logins=(), wildcards=(), api_keys=(), title=None,
         hints=True):
    """What was allowed during the run, grouped by kind; HTTPS addresses under the names they were allowed
    for (from `net allow --allow-host`, presets and approved lookups; never looked up anew: a name the
    agent chose may carry data). HINTS: how to lift the cut, untaint and take back (not for an orphaned run: refused)."""
    printable = launcher().printable
    names = names or {}
    by_address = {}
    for host, entry in names.items():
        for address in entry.get("addresses", []) if isinstance(entry, dict) else []:
            by_address.setdefault(bare(address), set()).add(host)
    nets = sorted({bare(n) for n in result.get("nets", [])}, key=address_sort)
    https = sorted({bare(n) for n in result.get("https", [])}, key=address_sort)
    tcp = sorted({(bare(a), int(p)) for a, p in result.get("tcp", [])}, key=lambda ap: (address_sort(ap[0]), ap[1]))
    dns = sorted({bare(n) for n in result.get("dns", [])}, key=address_sort)
    relayed = sorted({int(p) for p in result.get("host_ports", []) if isinstance(p, int)})
    taint = result.get("taint") if isinstance(result.get("taint"), dict) else None
    git = [shown_login(entry) for entry in logins]  # never the token
    keys = [{"name": e.get("name"), "host": e.get("host"), "port": e.get("port"), "placeholder": e.get("placeholder"),
             "secret": e.get("secret")} for e in api_keys]  # never the key
    cut = cut_label(result.get("cut"))
    risky = shared_of(wildcards)  # below a shared service: anyone can rent a name there (marked as a risk)
    if as_json:
        out(json.dumps({"run": run, "cut": cut, "all_ports": nets, "dns": dns,
                        **({"taint": taint} if taint is not None else {}),
                        "host_wildcards": list(wildcards),
                        **({"shared_wildcards": risky} if risky else {}),
                        "git": git, "api_keys": keys, "host_ports": relayed,
                        "https": [{"address": a, "names": sorted(by_address.get(a, []))} for a in https],
                        "tcp": [{"address": a, "port": p, "names": sorted(by_address.get(a, []))} for a, p in tcp]},
                       indent=2))
        return
    t = textout()
    if cut:
        out(t.style(f"  kill switch: {cut}: {'; '.join(CUT_TEXT[part] for part in cut.split('+'))}", "warn"))
        if hints:
            out(t.style(f"  lift it: sandbox-ai net cut {printable(run) if run else '[RUN]'} off", "muted"))
        out("")
    if taint is not None:
        printable = launcher().printable
        line = printable(tainted_text(taint))
        out(t.style(f"  untrusted content: {line}", "warn" if taint.get("tainted") else "muted"))
        if taint.get("tainted") and hints:
            out(t.style(f"  take it back: sandbox-ai net untaint {printable(run) if run else '[RUN]'}", "muted"))
        out("")
    total = len(nets) + len(https) + len(tcp) + len(dns) + len(relayed)
    if git:
        t.section("Git logins added during this run", out,
                  note="its proxy logs in on the wire; a token \"in the sandbox\" is readable by the agent")
        t.table(["SERVER", "USER", "TOKEN"], [[printable(entry["url"]), printable(str(entry.get("user") or "")),
                                                "in the sandbox" if entry.get("in_sandbox") else "with the proxy"]
                                               for entry in git], out)
        out("")
    if keys:
        t.section("API keys given during this run", out,
                  note="the proxy puts each in on the wire for HTTPS requests to its host; the agent sends the "
                       "placeholder")
        t.table(["VARIABLE", "HOST", "SECRET"], [[printable(str(e["name"])), printable(str(e["host"] or "")),
                                                   printable(str(e.get("secret") or "-"))] for e in keys], out)
        out("")
    if wildcards:
        t.section("Host wildcards", out, note="every name below them is opened as it is looked up")
        if risky:  # the risk you confirmed, said again wherever the run's wildcards are shown
            t.table(["WILDCARD", "RISK"], [[printable(w), f"below the shared service {printable(risky[w])}: any "
                                            "server anyone rents there" if w in risky else "-"] for w in wildcards],
                    out)
        else:
            t.table(["WILDCARD"], [[printable(w)] for w in wildcards], out)
        out("")
    t.section(f"{title or (printable(run) if run else 'this sandbox')}: allowed during this run", out,
              note=f"{total} destination{'s' if total != 1 else ''}, added with `sandbox-ai net allow` or approvals")
    if not total:
        out("  nothing beyond its start options")
        return
    if https:
        out("")
        t.section("HTTPS (TCP 443)", out)
        groups = {}
        for address in https:
            key = ", ".join(sorted(printable(h) for h in by_address.get(address, []))) or "(no name)"
            groups.setdefault(key, []).append(address)
        rows = []
        for key in sorted(groups, key=lambda k: (k == "(no name)", k)):
            v4 = [a for a in groups[key] if ":" not in a]
            v6 = [a for a in groups[key] if ":" in a]
            rows.append([key, str(len(v4)), str(len(v6)), "  ".join(v4 + v6)])
        t.table(["NAME", "IPv4", "IPv6", "ADDRESSES"], rows, out)
    if tcp:
        out("")
        t.section("TCP", out)
        t.table(["ADDRESS", "PORT", "NAME"],
                [[a, str(p), ", ".join(sorted(printable(h) for h in by_address.get(a, []))) or "-"] for a, p in tcp], out)
    if relayed:
        out("")
        t.section("Host ports (relayed)", out, note="the host's 127.0.0.1:PORT, through the connection proxy")
        t.table(["PORT"], [[str(p)] for p in relayed], out)
    if dns:
        out("")
        t.section("DNS (port 53)", out, note="the sandbox's lookups go to these nameservers; each one is logged")
        t.table(["NAMESERVER"], [[n] for n in dns], out)
    if nets:
        out("")
        t.section("All ports and protocols", out)
        t.table(["NETWORK", "NAME"],
                [[n, ", ".join(sorted(printable(h) for h in by_address.get(n, []))) or "-"] for n in nets], out)
    out("")
    if hints:
        out(t.style(f"  take back: sandbox-ai net revoke {printable(run) if run else '[RUN]'} --allow-host NAME | "
                    "--allow-domain DOMAIN | --allow-tcp IP:PORT | --allow IP_OR_CIDR | --dns", "muted"))


def show_orphan(record, out=print, as_json=False):
    """`net show NAME` for the orphaned run RECORD (orphan_record), marked as such: what its sandbox's supervisor
    answers on its control socket, else what its run directory recorded (the names allowed and their addresses, the
    kill switch, logins and keys given), said as such. ValueError when its run directory is gone (nothing is left)."""
    name = str((record.get("orphan") or {}).get("name") or record.get("run"))
    why = str(record.get("orphaned_why") or "")
    if not record.get("directory"):
        raise ValueError(f"{printable(name)} is orphaned and its run directory is gone: nothing of what was allowed "
                         f"during its run is left to show; {printable(why)}")
    names = remembered(record)
    try:
        result, answered, silent = ask(record["directory"], {"op": "show"}, timeout=CUT_SECONDS), True, None
    except ValueError as exc:
        result, answered, silent = {"cut": cut_state(record["directory"]), "https": sorted(
            {address for entry in names.values() if isinstance(entry, dict)
             for address in entry.get("addresses", []) if isinstance(address, str)})}, False, str(exc)
    given = {"names": names, "run": record.get("run"), "logins": git_logins(record),
             "wildcards": run_wildcards(record), "api_keys": added_api_keys(record), "title": who(record),
             "hints": False}
    if as_json:
        shown = []
        show(result, shown.append, as_json=True, **given)
        out(json.dumps({**json.loads("\n".join(shown)), "orphaned": True, "orphaned_why": why, "answered": answered},
                       indent=2))
        return 0
    t = textout()
    out(t.style(f"{printable(name)} is orphaned: ", "bad") + printable(why))
    if not answered:
        out(t.style(f"its supervisor did not answer ({printable(silent)}): what its run directory recorded", "warn"))
    out("")
    show(result, out, **given)
    return 0


def process_state(pid):
    """(state letter, start time) of PID (/proc/PID/stat; sysctl on macOS: procinfo.py), or None if gone."""
    return launcher().procinfo().state(pid, PROC)


def started_at(pid):
    """Seconds since the epoch at which PID started."""
    found = launcher().procinfo().started_epoch(pid, PROC)
    if found is None:
        raise ValueError("cannot tell when this process started (no btime in /proc/stat)")
    return found


def launcher_process(record):
    """(PID, start ticks) of the launcher RECORD registered, after checking that the PID still is that
    launcher: this user's, of the recorded start time (or, for records without one, started no later
    than the registration) and running agent_sandbox.py. ValueError otherwise."""
    import calendar
    name, pid = record.get("run"), record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError(f"{name}: its record names no usable PID")
    owner = launcher().procinfo().owner(pid, PROC)
    if owner is None:
        raise ValueError(f"{name} has ended (PID {pid} is gone)")
    if owner != os.getuid():
        raise ValueError(f"PID {pid} of {name} belongs to another user; not stopping it")
    start = record.get("pid_start")
    if start is not None:
        if not isinstance(start, int) or isinstance(start, bool):
            raise ValueError(f"{name}: its record has an unusable pid_start; not stopping PID {pid}")
        cmdline = launcher().process_cmdline(pid, start, PROC)
        if not cmdline:
            raise ValueError(f"PID {pid} is not the launcher of {name} any more (another process has that PID now); "
                             "not stopping it")
    else:
        try:
            registered = calendar.timegm(time.strptime(str(record.get("started")), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            raise ValueError(f"{name}: its record has no start time to check PID {pid} against; not stopping it") from None
        cmdline = launcher().process_cmdline(pid, None, PROC)
        state = process_state(pid)
        if state is None or started_at(pid) > registered + START_SLACK:
            raise ValueError(f"PID {pid} started after {name} registered: it is another process now; not stopping it")
        start = state[1]
    if not any(arg.endswith(b"agent_sandbox.py") for arg in cmdline):
        raise ValueError(f"PID {pid} does not run a sandbox-ai launcher (no agent_sandbox.py in its command line); "
                         "not stopping it")
    return pid, start


def ended(pid, start):
    state = process_state(pid)
    return state is None or state[1] != start or state[0] in ("Z", "X")


def terminate(pid, start):
    """SIGTERM to PID if it still is the process started at START (through a pidfd where there is one, so a
    PID reused meanwhile is never hit)."""
    pidfd = None
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        try:
            pidfd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        except OSError:
            pidfd = None  # no pidfd support (old kernel, seccomp): the plain signal below
    try:
        if ended(pid, start):
            return
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    finally:
        if pidfd is not None:
            os.close(pidfd)


# --- host ports relayed during the run (sandbox-ai net allow --allow-host-port) --------------------------------
#
# pasta forwards the host's loopback ports given at the start (--allow-host-port) and cannot add one later. A relay
# can: this host process listens on a Unix socket in the run's directory (mode 0700, hidden from the agent) and
# connects each connection to the host's 127.0.0.1:PORT, nothing else; the supervisor in the sandbox listens on
# 127.0.0.1:PORT there and joins the agent's connections (through the connection proxy) to that socket. Only the
# human starts one (this command); it ends with the run, or with `revoke`.

RELAY_WAIT = 5
RELAY_CONNECTIONS = 64


def relay_files(directory, port):
    return Path(directory) / f"relay-{int(port)}.sock", Path(directory) / f"relay-{int(port)}.json"


def host_connect(port, timeout=10):
    """A connection to this host's loopback PORT (127.0.0.1, else ::1)."""
    error = None
    for address in ("127.0.0.1", "::1"):
        try:
            return socket.create_connection((address, port), timeout=timeout)
        except OSError as exc:
            error = exc
    raise error


def serve_relay(directory, port, pid, start, connect=host_connect, idle=1.0):
    """The host end of a relay: listen on DIRECTORY/relay-PORT.sock and join each connection to the host's
    loopback PORT, until the launcher PID (started at START) ends, the socket is removed or replaced (revoke), or
    SIGTERM. At most RELAY_CONNECTIONS at a time."""
    sock_path, _ = relay_files(directory, port)
    join = launcher().RUNTIME.join_streams
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # Through the directory's descriptor: socket paths are limited to 107 bytes, a HOME may be longer.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f"/proc/self/fd/{fd}/{sock_path.name}")
        listener.bind(f"/proc/self/fd/{fd}/{sock_path.name}")
        os.chmod(f"/proc/self/fd/{fd}/{sock_path.name}", 0o600)
        mine = os.stat(f"/proc/self/fd/{fd}/{sock_path.name}").st_ino
        listener.listen(32)
        listener.settimeout(idle)
        slots = threading.BoundedSemaphore(RELAY_CONNECTIONS)
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set()) if threading.current_thread() is threading.main_thread() else None

        def handle(connection):
            try:
                try:
                    upstream = connect(port)
                except OSError:
                    connection.close()  # nothing listens on the host's port (now)
                    return
                upstream.settimeout(None)
                connection.settimeout(None)
                join(connection, upstream)
            finally:
                slots.release()
        while not stopping.is_set():
            try:
                if os.stat(f"/proc/self/fd/{fd}/{sock_path.name}").st_ino != mine:
                    break  # replaced: another relay for this port
            except OSError:
                break  # removed: revoked, or the run directory went
            if ended(pid, start):
                break  # the run ended
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            if not slots.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    finally:
        listener.close()
        with contextlib.suppress(OSError):
            if os.stat(f"/proc/self/fd/{fd}/{sock_path.name}").st_ino == mine:
                os.unlink(f"/proc/self/fd/{fd}/{sock_path.name}")
        os.close(fd)
    return 0


def relay_running(directory, port):
    """(pid, start) of the host relay for PORT of the run in DIRECTORY, or None."""
    _, info = relay_files(directory, port)
    try:
        data = json.loads(info.read_text())
        pid, start = int(data["pid"]), int(data["start"])
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    if ended(pid, start):
        return None
    cmdline = launcher().process_cmdline(pid, start, PROC) or []
    if b"--internal-relay" not in cmdline or os.fsencode(str(directory)) not in cmdline:
        return None  # another process has that PID now
    return pid, start


def start_relay(record, port, wait=RELAY_WAIT):
    """Start the host relay for PORT of RECORD's run (once: a running one is kept); its socket exists on return."""
    directory = Path(record["directory"])
    if relay_running(directory, port):
        return
    pid, start = launcher_process(record)
    sock_path, info = relay_files(directory, port)
    process = subprocess.Popen([sys.executable, "-I", str(HERE / "net.py"), "--internal-relay", str(directory),
                                str(int(port)), str(pid), str(start)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError(f"the relay for host port {port} did not start")
        if sock_path.exists():
            state = process_state(process.pid)
            info.write_text(json.dumps({"pid": process.pid, "start": state[1] if state else None, "port": int(port)}))
            info.chmod(0o600)
            return
        time.sleep(0.05)
    process.terminate()
    raise ValueError(f"the relay for host port {port} did not start within {wait:g} s")


def stop_relay(record, port):
    """End the host relay for PORT of RECORD's run, if one runs."""
    directory = Path(record["directory"])
    running = relay_running(directory, port)
    sock_path, info = relay_files(directory, port)
    with contextlib.suppress(OSError):
        sock_path.unlink()  # the relay sees it gone within a second
    if running:
        terminate(*running)
    with contextlib.suppress(OSError):
        info.unlink()


def stop(record, wait=STOP_WAIT, out=print):
    """End the one-off sandbox RECORD: SIGTERM to its launcher, which cleans up and exits; 0 once it ended."""
    name = record.get("run")
    if record.get("session"):
        shown = launcher().printable(str(record.get("name") or record["session"]))
        raise ValueError(f"{name} is the session {shown}; use: sandbox-ai stop {shown}")
    if str(name).startswith("mcp-"):
        raise ValueError(f"{name} is an MCP server; use: sandbox-ai mcp stop {str(name)[len('mcp-'):]}")
    pid, start = launcher_process(record)
    terminate(pid, start)
    deadline = time.monotonic() + wait
    while not ended(pid, start) and time.monotonic() < deadline:
        time.sleep(0.1)
    if ended(pid, start):
        out(f"{name}: stopped (its launcher, PID {pid}, cleaned up and ended)")
        return 0
    if not wait:
        out(f"{name}: asked its launcher (PID {pid}) to end; it cleans up first (see: sandbox-ai net list)")
        return 0
    out(f"{name}: asked its launcher (PID {pid}) to end, but it still runs after {wait:g} s (still cleaning up?); "
        "see: sandbox-ai net list")
    return 1


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("a port from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("a port from 1 to 65535")
    return port


def seconds(value):
    number = float(value)
    if not 0 <= number <= 3600:
        raise argparse.ArgumentTypeError("0 to 3600 seconds")
    return number


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser(lean=False):
    """LEAN: allow and revoke without their options, which ask the launcher (its presets, nameservers and short
    options): for a command line that argparse never hands to them (`net list`, which the app asks every few
    seconds, loads no launcher)."""
    p = argparse.ArgumentParser(prog="sandbox-ai net", description=__doc__.split("\n\n")[0],
                                formatter_class=help_style, allow_abbrev=False,
                                epilog="\n".join(__doc__.split("\n\n")[1:3]))
    commands = p.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="running sandboxes and what each may reach, in short",
                                  description="The running sandboxes and what each may reach, one line each: its "
                                              "security mode, network profiles, VPN, approvals (questions waiting), "
                                              "the hosts added while it runs (sandbox-ai net allow, approvals) and "
                                              "the kill switch's cut, newest first (--wide: its run id and "
                                              "workspace too). The details of one: sandbox-ai net show RUN.")
    listing.add_argument("-j", "--json", action="store_true", help="as JSON, for scripts")
    listing.add_argument("-w", "--wide", "--no-trunc", dest="wide", action="store_true",
                         help="also each run's id and workspace (whole)")
    shown = commands.add_parser("show", help="what was allowed during the run")
    shown.add_argument("run", nargs="?")
    shown.add_argument("-j", "--json", action="store_true", help="as JSON, for scripts")
    shown.add_argument("-k", "--keep", action="store_true",
                       help="keep for the next starts what you gave the run since its start: into the project's "
                            ".sandbox-ai.json (trusted), as options (names and addresses you allowed with net allow or "
                            "approved always, wildcards, networks, DNS, host ports, devices, workspaces added; no Git "
                            "logins). Refused when the administrator's policy caps approvals below always")
    shown.add_argument("--keep-all", action="store_true",
                       help="--keep, and also the names and addresses approvals opened once or for the run")
    untainted = commands.add_parser("untaint", help="take back the run's taint",
                                               description="Take back the run's taint: the rules for tainted runs "
                                                           "no longer hold, until untrusted content enters it "
                                                           "again.")
    untainted.add_argument("run", nargs="?")
    cutting = commands.add_parser(
        "cut", help="the kill switch: cut a running sandbox's network",
        description="The kill switch: cut a running sandbox's network down at once. MODE: remote (the internet is "
                    "cut; only the local network and the host's ports it may reach stay), local (the local network "
                    "and the host's ports are cut; only the internet it may reach stays), api (only its model API "
                    "stays: its provider's hosts as the start opened them, or its --llm server), all (everything, "
                    "the model API too), off (lifted: it reaches again what it may reach). A cut never opens "
                    "anything; open connections it forbids are closed. Its firewall, its connection proxy and its "
                    "supervisor hold it, out of the agent's reach, until it is lifted or the run ends. It holds for "
                    "the run's MCP servers, helpers and team members too (each with a sandbox of its own), also those "
                    "that join later, unless --only. Every run's cut is kept apart: a server two runs share stays cut "
                    "while one of them is (only what every cut keeps stays reachable). A paused sandbox is cut as it "
                    "resumes. Runs started by a sandbox-ai older than 0.1.94 have no kill switch.")
    cutting.add_argument("words", nargs="+", metavar="[RUN] MODE",
                         help="the run (left out: the only one, or the only one for this directory) and the mode: "
                              "remote, local, api, all or off")
    cutting.add_argument("--all-runs", action="store_true",
                         help="every running sandbox of yours (the emergency stop: sandbox-ai net cut --all-runs all)")
    cutting.add_argument("--only", action="store_true",
                         help="this run alone, not its MCP servers, helpers and team members")
    cutting.add_argument("-j", "--json", action="store_true", help="as JSON, for scripts")
    stopped = commands.add_parser("stop", help="older form of `sandbox-ai stop NAME` (a run only)")
    stopped.add_argument("run")
    stopped.add_argument("--wait", type=seconds, default=STOP_WAIT, metavar="SECONDS",
                         help=f"how long to wait for it to end (default {STOP_WAIT}; 0: do not wait)")
    for name in ("allow", "revoke"):
        command = commands.add_parser(name, help=f"{name} destinations while the sandbox runs")
        if lean:
            continue
        command.add_argument("run", nargs="?")
        lists = launcher().ListOption  # several items in one value: "a.example, b.example" (LIST_OPTIONS)
        each = launcher().LIST_HELP
        command.add_argument("-A", "--allow-host", action=lists, default=[], metavar="HOST", type=str.lower,
                             help=f"pin HOST in the sandbox's /etc/hosts and allow TCP/443 to it; {each}")
        command.add_argument("-ad", "--allow-domain", action=lists, default=[], metavar="DOMAIN", type=str.lower,
                             help="DOMAIN and every name below it: --allow-host DOMAIN --allow-host '*.DOMAIN' in one, "
                                  f"with the same checks (no public suffix or hosting service); {each}")
        command.add_argument("-t", "--allow-tcp", action=lists, default=[], metavar="IP:PORT",
                             help=f"allow TCP to IP:PORT ([IPv6]:PORT); {each}")
        command.add_argument("-a", "--allow", action=lists, default=[], metavar="IP_OR_CIDR",
                             help=f"every protocol and port; {each}")
        command.add_argument("-R", "--dns", action="store_true",
                             help="real DNS: the sandbox's DNS stub forwards every name to "
                                  f"{' and '.join(launcher().DEFAULT_NAMESERVERS)} (each lookup logged); addresses "
                                  "stay closed unless allowed as well")
        command.add_argument("-ns", "--nameserver", action=lists, default=[], metavar="IP",
                             help=f"this resolver instead of the defaults (implies --dns); {each}")
        command.add_argument("-P", "--allow-host-port", action=lists, default=[], item_type=port_number, metavar="PORT",
                             help="the host's 127.0.0.1:PORT (a local Git server, an LLM server), through a relay "
                                  f"that only you start (runs started with sandbox-ai 0.1.37 or newer); {each}")
        for kind, ref, where in (("env", "NAME", "your variable NAME"), ("file", "FILE", "FILE (yours, mode 0600)"),
                                 ("secret", "SECRET", "the secret SECRET of sandbox-ai secrets")):
            short = launcher().SHORT_OPTIONS.get(f"--git-token-{kind}")
            command.add_argument(*([short] if short else []), f"--git-token-{kind}", action="append", default=[],
                                 metavar=f"URL={ref}",
                                 help=f"a Git login for http(s)://[USER@]HOST[:PORT], the token from {where}; the "
                                      "run's connection proxy logs in with it on the wire at once, for clone, fetch "
                                      "and push (no token enters the sandbox; it wins over a login the run was "
                                      "started with for that server)" if name == "allow" else
                                      f"take back the Git login for URL (the {ref} part is not read)")
        if name == "allow":
            command.add_argument("--git-token-in-sandbox", action="store_true",
                                 help="a Git login the run's connection proxy cannot use (a run without the proxy, "
                                      "--no-inspect for that server, a run started by an older sandbox-ai) "
                                      "is put into the sandbox's session directory for its Git helper "
                                      "instead: the agent can read the token there. Without this such a login is "
                                      "refused")
        command.add_argument("-ky", "--api-key", action="append", default=[], metavar="HOST=NAME[=SECRET]",
                             help="an API key for HTTPS requests to HOST, from the secret SECRET (default NAME) of "
                                  "sandbox-ai secrets: as at a start, the connection proxy puts it in on the wire "
                                  "where the agent sends the placeholder $NAME stands for; the agent never sees "
                                  "it; HOST is allowed too (runs started with sandbox-ai 0.1.92 or newer)"
                             if name == "allow" else "take back the API key NAME given during the run")
        for preset, hosts in launcher().PRESETS.items():
            short = [launcher().SHORT_OPTIONS[f"--allow-{preset}"]] if f"--allow-{preset}" in launcher().SHORT_OPTIONS else []
            command.add_argument(*short, f"--allow-{preset}", action="append_const", const=preset, dest="presets",
                                 default=[], help=f"{', '.join(hosts)} (TCP/443)")
        if name == "allow":
            command.add_argument("-k", "--keep", action="store_true",
                                 help="also keep it for the next starts: the options go into the project's "
                                      ".sandbox-ai.json (only while that file is exactly what you trusted; the new "
                                      "file is trusted at once). Logins are kept as references, never as tokens")
            command.add_argument("-sw", "--allow-shared-wildcard", action=lists, default=[], metavar="'*.DOMAIN'",
                                 type=str.lower,
                                 help="a host wildcard below a shared service where anyone can rent a name "
                                      "(*.blob.core.windows.net): the agent could open any server anyone rents there "
                                      "and send data there; giving it here is your confirmation of that risk "
                                      "(--allow-host '*.DOMAIN' asks on a terminal, else it is refused). Refused in "
                                      "the modes high and paranoid and where the administrator's policy forbids it; "
                                      f"*.com and a country's suffix (*.co.uk) always; {each}")
            command.add_argument("-ys", "--yes", action="store_true",
                                 help="answer the risk question of a wildcard below a shared service given with "
                                      "--allow-host with yes, for scripts (as --allow-shared-wildcard)")
            command.add_argument("--public-only", action="store_true",
                                 help="--allow-host only: open just the public addresses of each name (one the agent "
                                      "chose may point at this host, your network or a VPN), as an answered lookup "
                                      "does; none is refused. With --keep it is remembered as an approval "
                                      "(approved.json: public addresses at every start), not in the project file")
    return p


def kept_options(args, ports):
    """The options of an allow command, as tokens for a project file (logins as their references). An
    --allow-domain is kept as given, not as the two --allow-host it stands for."""
    confirmed = getattr(args, "shared_confirmed", None) or ()
    tokens = [token for host in args.allow_host if host not in getattr(args, "domain_hosts", ())
              for token in ("--allow-shared-wildcard" if host in confirmed and shared_of([host]) else "--allow-host",
                            host)]
    tokens += [token for domain in dict.fromkeys(launcher().domain_of(value)
                                                 for value in getattr(args, "allow_domain", None) or [])
               for token in ("--allow-domain", domain)]
    tokens += [f"--allow-{preset}" for preset in dict.fromkeys(args.presets)]
    tokens += [token for value in args.allow_tcp for token in ("--allow-tcp", value)]
    tokens += [token for value in args.allow for token in ("--allow", value)]
    tokens += [token for value in args.nameserver for token in ("--nameserver", value)] or (["--dns"] if args.dns else [])
    tokens += [token for port in ports for token in ("--allow-host-port", str(port))]
    tokens += [token for kind in GIT_KINDS for value in getattr(args, f"git_token_{kind}", None) or []
               for token in (f"--git-token-{kind}", value)]  # (a token file: absolute by now, named_token_files)
    tokens += [token for value in getattr(args, "api_key", None) or [] for token in ("--api-key", value)]
    return tokens


HOST_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,62}")


def given_file(record):
    """What you allowed with `sandbox-ai net allow` during the run (run directory, outside the sandbox): what
    `net show --keep` keeps of the names and TCP targets; those approvals opened as well are not in it."""
    return Path(record["directory"]) / "given.json"


def given(record):
    """{"hosts": [...], "tcp": ["IP:PORT", ...]} of given_file, or empty lists."""
    try:
        data = json.loads(given_file(record).read_text())
    except (OSError, ValueError, RecursionError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {field: [v for v in data.get(field, []) if isinstance(v, str)] if isinstance(data.get(field), list) else []
            for field in ("hosts", "tcp")}


def tcp_text(address, port):
    address = str(ipaddress.ip_address(str(address).strip("[]").split("%")[0]))
    return f"[{address}]:{int(port)}" if ":" in address else f"{address}:{int(port)}"


def note_given(record, op, hosts, tcp):
    """Record HOSTS and TCP ([(address, port)]) as given (allow) or taken back (revoke) by you, under the run's
    lock; written whole and renamed into place."""
    with hosts_lock(record):
        known = given(record)
        targets = [tcp_text(a, p) for a, p in tcp]
        if op == "allow":
            known["hosts"] = sorted(set(known["hosts"]) | set(hosts))
            known["tcp"] = sorted(set(known["tcp"]) | set(targets))
        else:
            known["hosts"] = sorted(set(known["hosts"]) - set(hosts))
            known["tcp"] = sorted(set(known["tcp"]) - set(targets))
        target = given_file(record)
        fd, temporary = tempfile.mkstemp(prefix=".given.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps(known))
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


_approvals_module = None


def approvals_module():
    global _approvals_module
    if _approvals_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_approvals_for_net", HERE / "approvals.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _approvals_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _approvals_module = loaded
    return _approvals_module


def running_options(record, result, everything=False):
    """(options, workspaces, notes): what the running sandbox RECORD was given during the run (RESULT: its `show`
    state), as start options for its project file. By default only what you gave: the names and TCP targets you
    allowed with `sandbox-ai net allow` (given.json) or approved "always" for the project (approved.json), and what
    only you can add (host wildcards, networks, nameservers, host ports, devices, workspaces added with
    `sandbox-ai workspace add`); the names and addresses approvals opened "once" or for the run, and addresses with no
    record of who opened them, are left out (a note lists them). EVERYTHING (--keep-all): those too. Git logins are
    never kept (their tokens stay out of project files). RESULT comes from the supervisor, which runs beside the
    agent: every value is checked again before it goes into a file that is trusted at the next start, and each option
    is written with its own value."""
    l, notes, tokens, left = launcher(), [], [], []

    def address(value):
        return str(ipaddress.ip_address(str(value).split("%")[0]))
    names = remembered(record)
    started = set(record.get("allow_wildcards") or [])
    wildcards = [w for w in run_wildcards(record) if w not in started]
    every = list(started) + wildcards
    yours = given(record)
    try:
        always = approvals_module().approved_for(record["workspace"]) if record.get("workspace") else {}
    except (OSError, ValueError, KeyError):
        always = {}
    kept_hosts = set(yours["hosts"]) | set(always.get("hosts") or [])
    kept_tcp = set(yours["tcp"]) | set(always.get("tcp") or [])
    named = set()
    for host in sorted(names):
        entry = names[host]
        named |= {bare(a) for a in (entry.get("addresses", []) if isinstance(entry, dict) else [])}
        if not HOST_NAME.fullmatch(str(host)):
            notes.append(f"{l.printable(str(host))[:100]}: not kept (not a host name)")
        elif not any(str(host).endswith(w[1:]) for w in every):  # below a wildcard: the wildcard is kept, not each
            (tokens if everything or host in kept_hosts else left).extend(["--allow-host", host])
    confirmed = set(confirmed_shared(record))
    for wildcard in wildcards:
        try:
            l.RUNTIME.wildcard_suffix(wildcard)
            tokens += ["--allow-host", wildcard]
        except l.RUNTIME.SharedWildcardError:
            # Below a shared service: kept only where you confirmed its risk (your command's record, not the
            # supervisor's word), and kept so - as that confirmation.
            if wildcard not in confirmed:
                notes.append(f"{l.printable(wildcard)}: not kept (below a shared service; you did not confirm it)")
                continue
            tokens += ["--allow-shared-wildcard", wildcard]
    for value in sorted({bare(n) for n in result.get("https", [])} - named, key=address_sort):
        if "/" not in value:  # a single address allowed by an approval: its port, not a whole network
            target = tcp_text(address(value), 443)
            (tokens if everything or target in kept_tcp else left).extend(["--allow-tcp", target])
    for value, port in sorted({(bare(a), int(p)) for a, p in result.get("tcp", [])},
                              key=lambda ap: (address_sort(ap[0]), ap[1])):
        value = address(value)
        if not 1 <= port <= 65535:
            raise ValueError(f"the sandbox reported port {port}")
        target = tcp_text(value, port)
        (tokens if everything or target in kept_tcp else left).extend(["--allow-tcp", target])
    for net in sorted({bare(n) for n in result.get("nets", [])}, key=address_sort):
        tokens += ["--allow", str(ipaddress.ip_network(net, strict=False))]
    servers = sorted({address(bare(n)) for n in result.get("dns", [])}, key=address_sort)
    if servers and set(servers) == set(l.DEFAULT_NAMESERVERS):
        tokens += ["--dns"]
    else:
        tokens += [token for server in servers for token in ("--nameserver", server)]
    for port in sorted({int(p) for p in result.get("host_ports", []) if isinstance(p, int) and not isinstance(p, bool)}):
        if not 1 <= port <= 65535:
            raise ValueError(f"the sandbox reported port {port}")
        tokens += ["--allow-host-port", str(port)]
    try:
        added = l.load_devices_module().added(record)
    except (OSError, ValueError, KeyError):
        added = []
    for value in added:
        if isinstance(value, str) and (value.startswith("/dev/") or value.startswith("usb:")) and value.isprintable():
            tokens += ["--device", value]
    workspaces = []
    checks = record.get("workspace_checks") or {}
    for entry in load_workspaces_module().added_workspaces(record):
        if entry.get("kind", "dir") == "file":
            notes.append(f"{entry['host']} is a file added during the run: not kept (a project file names directories "
                         "as workspaces)")
            continue
        if entry.get("named"):
            notes.append(f"{entry['host']} is kept as a workspace, without its name {entry['sandbox']} (a project file's "
                         "workspaces are named after their directories)")
        if entry.get("mode") == "ro" and not checks.get("read_only"):
            notes.append(f"{entry['host']} was added read-only: not kept (a project file has no read-only workspace "
                         "of its own; start with --workspace-ro, or add it again with --ro)")
            continue
        if os.path.isabs(entry["host"]) and "," not in entry["host"] and entry["host"].isprintable():
            workspaces.append(entry["host"])
    if git_logins(record):
        notes.append("Git logins added during the run are not kept (tokens never go into a project file): start with "
                     "--git-token-env / --git-token-secret, or add one with sandbox-ai git-logins")
    keys = [entry for entry in added_api_keys(record) if isinstance(entry.get("host"), str)]
    if keys:
        # (Their hosts are kept above, --allow-host: said, or the next start reaches them without its keys.)
        notes.append("API keys given during the run are not kept: start with " + " ".join(
            f"--api-key {entry['host']}={entry['name']}" + (f"={entry['secret']}" if isinstance(
                entry.get("secret"), str) and entry["secret"] != entry["name"] else "") for entry in keys)
            + ", or give them with --keep (sandbox-ai secrets give RUN NAME --api-key HOST --keep)")
    if left:
        notes.append("left out (opened by an approval answered once or for the run, or with no record that you "
                     f"allowed it; --keep-all keeps them too): {' '.join(left)}")
    return tokens, workspaces, notes


_workspaces_module = None


def load_workspaces_module():
    global _workspaces_module
    if _workspaces_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_workspaces_for_net", HERE / "workspaces.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _workspaces_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _workspaces_module = loaded
    return _workspaces_module


def keep_allowed():
    """ValueError unless the administrator's policy lets what is allowed during a run be kept for later starts
    (max_approval_scope always): net show --keep and net allow --keep, checked before anything changes."""
    widest = approvals_module().policy_scope()
    if widest != "always":
        raise ValueError(f"--keep: the administrator's policy approves at most for {widest}, so what was "
                         "allowed during the run is not kept for later starts (nothing was written)")


def own_port(record, ports):
    """As at the start (dsh_web_port, a service's ports): a port of PORTS that the sandbox of RECORD listens on
    itself - DSH's web UI (its session browser reaches it there), a --service - is never relayed from the host: the
    relay's redirect would take the sandbox's 127.0.0.1:PORT, and the browser and the agent would reach the host's
    port instead. ValueError naming what listens there."""
    name = launcher().printable(record.get("run", "?"))
    for port in sorted(ports):
        if record.get("dsh_web_port") == port:
            raise ValueError(f"--allow-host-port {port}: DeepSeek Harness' web UI listens there in {name} (the "
                             "sandbox's own loopback port; a relay would take it from DSH); another port reaches "
                             "the host's")
        for service in record.get("services") or []:
            if isinstance(service, dict) and port in (service.get("ports") or []):
                raise ValueError(f"--allow-host-port {port}: the service {launcher().printable(service.get('name'))} "
                                 f"listens there in {name} (the sandbox's own port); another port reaches the host's")


def new_project_file_note(record):
    """" (a new project file, for AGENT: ...)" when keep_in_project is about to make the project file of the run
    RECORD (there is none yet): a project file is for one agent - another one's start there is refused with "is for
    AGENT" until it is started with --no-config. "" when the file is there (or the run names no workspace)."""
    workspace = record.get("workspace")
    if not workspace:
        return ""
    try:
        if launcher().config_key(Path(workspace) / launcher().CONFIG_NAME).exists():
            return ""
    except (OSError, ValueError):
        return ""
    agent = launcher().printable(str(record.get("agent")))
    return f" (a new project file, for {agent}: another agent starts in this project with --no-config)"


def keep_in_project(record, tokens, write=True, workspaces=(), preview=None, change=None):
    """Add TOKENS (options) to the run's project file, WORKSPACE/.sandbox-ai.json, and trust the result: only while
    the file is exactly what you trusted (an agent's change is never trusted along), or a new one for the run's
    agent. Returns (path, [added options]); ValueError otherwise. WRITE False only checks (before the run changes:
    a refused --keep changes nothing; the write checks again). PREVIEW(path, added) is called before the write
    with exactly what it adds. CHANGE(document): another change of the document (an exception rule in its "rules",
    approvals.rule_exception), made in place; it returns what it added ([] for nothing)."""
    l = launcher()
    workspace = record.get("workspace")
    if not workspace:
        raise ValueError("the run does not name its workspace (started by an older sandbox-ai): add the options "
                         "to .sandbox-ai.json yourself, then: sandbox-ai wizard trust FILE")
    # The project file of the run's first workspace (/work), never one of its further workspaces: the one its
    # start read (load_project_config), so that the next start of this project reads what is kept here.
    path = l.config_key(Path(workspace) / l.CONFIG_NAME)
    trusted = l.trusted_configs()
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink; refusing to write through it")
    if path.exists():
        data = l.read_config_bytes(path)
        if trusted.get(str(path)) != hashlib.sha256(data).hexdigest():
            raise ValueError(f"{path} is not what you trusted (changed since, or never trusted): review it and run "
                             f"sandbox-ai wizard trust {shlex.quote(str(path))} first; nothing was kept")
        try:
            document = json.loads(data.decode())
        except (ValueError, RecursionError):
            raise ValueError(f"{path}: invalid JSON") from None
        if not isinstance(document, dict) or not isinstance(document.get("options", []), list):
            raise ValueError(f"{path}: not a project configuration")
        if document.get("agent") != record.get("agent"):
            raise ValueError(f"{path} is for {document.get('agent')}, this run is {record.get('agent')}: nothing "
                             "was kept")
    else:
        if str(path) in trusted:
            raise ValueError(f"the trusted {path} is missing: restore it or run sandbox-ai wizard forget "
                             f"{shlex.quote(str(path))} first")
        document = {"version": 1, "agent": record.get("agent"), "options": []}
    if not write:
        return path, []
    options, added = list(document.get("options", [])), []
    listed = list(document.get("workspaces", []))
    for workspace in workspaces:
        if workspace not in listed and workspace != str(record.get("workspace")):
            listed.append(workspace)
            added += ["workspace", workspace]
    if listed:
        document["workspaces"] = listed
    index = 0
    flags_alone = {"--dns"} | {f"--allow-{preset}" for preset in launcher().PRESETS}
    while index < len(tokens):
        flag = tokens[index]
        takes_value = flag not in flags_alone and index + 1 < len(tokens)
        pair = tokens[index:index + 2] if takes_value else [flag]
        present = any(options[i:i + len(pair)] == pair for i in range(len(options)))
        if not present:
            options += pair
            added += pair
        index += len(pair)
    if change is not None:
        added += change(document)
    if not added:
        return path, []
    if preview is not None:
        preview(path, added)
    document["options"] = options
    data = (json.dumps(document, indent=2) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    l.trust_config(path, hashlib.sha256(data).hexdigest())
    return path, added


def telemetry_refusal(record, args):
    """ValueError for `net allow` ARGS that would open the telemetry of a run's agent whose telemetry is never
    switchable (Hermes, Qwen Code, DSH, Goose, OpenHands, nanobot, Letta Code) where the run has no connection proxy to refuse it: one of its telemetry
    hosts (also through a wildcard) or a public network (as the start refuses them: the agent's check_telemetry,
    the launcher's check_telemetry_unproxied). With the proxy its rules refuse those hosts whatever is opened."""
    agent = record.get("agent")
    names = launcher().UNSWITCHABLE_TELEMETRY
    if agent not in names or record.get("proxy") is not False:
        return
    module = launcher().load_hermes_module() if agent == "hermes" else launcher().agent_module(agent)
    opened = module.telemetry_hosts_in(getattr(args, "allow_host", None) or [])
    if opened:
        raise ValueError(f"{names[agent]}'s telemetry is always off: {', '.join(opened)} is never opened for "
                         f"{names[agent]}, and this run has no connection proxy to refuse it")
    launcher().check_telemetry_unproxied(argparse.Namespace(
        agent=agent, no_proxy=True, offline=False, allow=list(getattr(args, "allow", None) or []),
        allow_tcp=list(getattr(args, "allow_tcp", None) or [])))


def inside_subnet(record, request):
    """Refuse destinations inside the sandbox's own --subnet/--subnet6 (run.json): they would reach its link,
    gateway or DNS forwarder, not what was meant (the launcher refuses them at the start too)."""
    for value in record.get("subnets") or []:
        own = ipaddress.ip_network(value, strict=False)
        wanted = list(request["nets"]) + list(request["https"]) + [a for a, _ in request["tcp"]]
        for dest in wanted:
            net = ipaddress.ip_network(dest, strict=False)
            if net.version == own.version and net.overlaps(own):
                raise ValueError(f"{dest} lies inside the sandbox's own network {own}; it would not reach that "
                                 "destination")


def relay_main(argv):
    """--internal-relay DIRECTORY PORT PID START: the host end of a relay (start_relay runs it)."""
    if len(argv) != 4:
        print("usage: net.py --internal-relay DIRECTORY PORT PID START", file=sys.stderr)
        return 2
    directory, port, pid, start = argv[0], int(argv[1]), int(argv[2]), int(argv[3])
    if not 1 <= port <= 65535 or not ours(directory):
        return 2
    return serve_relay(directory, port, pid, start)


def change_network(record, args, request, network, ports):
    """The network part of an allow or revoke (PORTS: the host ports relayed); the supervisor's answer. A relay
    started for it is stopped again when the supervisor refuses."""
    # macOS: the supervisor in podman machine's VM connects to the Mac's port itself (runtime.PortRelay
    # upstream); no host relay, whose socket would not cross the VM's shared folders.
    relayed = [] if launcher().machine_record(record) else ports
    if relayed and args.command == "allow":
        for port in relayed:
            start_relay(record, port)  # the host end first: the sandbox's end connects to its socket
    try:
        result = ask(record["directory"], request if network else {"op": "show"})
    except ValueError:
        if relayed and args.command == "allow":
            for port in relayed:
                if port not in (ask(record["directory"], {"op": "show"}).get("host_ports") or []):
                    stop_relay(record, port)
        raise
    if relayed and args.command == "revoke":
        for port in relayed:
            stop_relay(record, port)
    return result


def main(argv, out=print, resolver=resolve, brief=False, prog="net", command=None):
    """BRIEF: an allow or revoke says what it gave or took back, without the table of everything the run was given
    (git-logins give, secrets give: net show and their list --run have it). PROG: the command a refusal names
    ("sandbox-ai PROG: ..."); COMMAND: the one a stopped start's hint is for (STOPPED_HINTS; default net ...)."""
    if argv[:1] == ["--internal-relay"]:
        return relay_main(argv[1:])
    # The first word names the command (the parser has no option but --help before it): only allow and revoke need
    # their options.
    args = parser(lean=argv[:1] in (["list"], ["show"], ["untaint"], ["stop"], ["cut"])).parse_args(argv)
    # --allow-domain D is --allow-host D '*.D' from here on, as at the start (the launcher's expand_domains).
    args.domain_hosts = launcher().expand_domains(args) if getattr(args, "allow_domain", None) else []
    # --allow-shared-wildcard '*.D' is --allow-host '*.D', its risk confirmed (the launcher's expand_shared_wildcards).
    args.shared_confirmed = set()
    if hasattr(args, "allow_host"):
        launcher().expand_shared_wildcards(args)
    try:
        runs = running()
        if args.command == "list":
            if args.json:
                # "cut": the kill switch's mode (None: none); "killswitch": the run has one (sandbox-ai 0.1.94 on).
                out(json.dumps([{"run": name, "name": r.get("name"), "agent": r.get("agent"),
                                 "workspace": r.get("workspace"), "started": r.get("started"),
                                 "session": r.get("session"), "kind": r.get("kind"), "cut": r.get("cut"),
                                 "killswitch": bool(r.get("net_cut"))} for name, r in runs.items()], indent=2))
                return 0
            list_table(runs, out, wide=args.wide)
            return 0
        if args.command == "cut":
            if len(args.words) > (1 if args.all_runs else 2):
                raise ValueError("give [RUN] MODE" if not args.all_runs else "--all-runs takes the MODE alone")
            mode = args.words[-1]
            if mode not in (*CUT_MODES, "off"):
                raise ValueError(f"no kill switch mode {printable(mode)}: remote, local, api, all or off" +
                                 (f" (sandbox-ai net cut [RUN] MODE: the mode comes last, e.g. net cut "
                                  f"{printable(mode)} api)" if len(args.words) == 1 and not args.all_runs else ""))
            if args.all_runs:  # (the app's emergency stop): the orphaned runs too, once the running ones are cut
                return cut_with_orphans(runs, None, mode, orphan_targets, out, every=True, as_json=args.json)
            try:
                record = choose(runs, args.words[0] if len(args.words) == 2 else None, command="net cut")
            except NotRunning as exc:
                orphan = orphan_record(exc.entry)  # its supervisor gone, its sandbox runs on: reached all the same
                if orphan is None:
                    raise
                if mode == "off":
                    raise ValueError(orphaned_text(exc.entry, "net cut off")) from None
                return cut_with_orphans(runs, orphan, mode, [orphan], out, alone=args.only, as_json=args.json)
            return cut_runs(runs, record, mode, out, alone=args.only, as_json=args.json)
        try:
            record = choose(runs, args.run, command=command or f"net {args.command}")
        except NotRunning as exc:
            orphan = orphan_record(exc.entry) if args.command == "show" and not (
                getattr(args, "keep", False) or getattr(args, "keep_all", False)) else None
            if orphan is None:
                raise
            return show_orphan(orphan, out, as_json=args.json)  # what it may reach: read where it is kept
        if args.command == "stop":
            if sys.stderr.isatty() and not os.environ.get("SANDBOX_AI_NO_DEPRECATION"):
                print(f"note: sandbox-ai net stop is the older form; sandbox-ai stop "
                      f"{launcher().printable(record.get('name') or record.get('run', args.run))} ends any start "
                      "(also sessions and MCP servers)", file=sys.stderr)
            return stop(record, args.wait, out)
        if args.command == "show":
            if getattr(args, "keep", False) or getattr(args, "keep_all", False):
                keep_allowed()
            result = ask(record["directory"], {"op": "show"})
            show(result, out, names=remembered(record), run=record.get("run", args.run), as_json=args.json,
                 logins=git_logins(record), wildcards=run_wildcards(record), api_keys=added_api_keys(record),
                 title=who(record, args.run))
            if getattr(args, "keep", False) or getattr(args, "keep_all", False):
                tokens, workspaces, notes = running_options(record, result, everything=args.keep_all)
                said = (lambda line: print(line, file=sys.stderr)) if args.json else out  # the JSON stays JSON
                for note in notes:
                    said(f"note: {launcher().printable(note)}")

                def preview(path, added):
                    said(f"{record.get('run', args.run)}: writing into {launcher().printable(path)}:")
                    pairs, index = [], 0
                    while index < len(added):  # an option alone (--dns, --allow-PRESET) or with its value
                        alone = added[index] == "--dns" or index + 1 >= len(added) or added[index + 1].startswith("--")
                        pairs.append(added[index:index + (1 if alone else 2)])
                        index += len(pairs[-1])
                    for pair in pairs:
                        said(f"  {launcher().printable(' '.join(pair))}")
                made = new_project_file_note(record)
                path, added = keep_in_project(record, tokens, workspaces=workspaces, preview=preview)
                shown_added = " ".join(added)
                said(f"{record.get('run', args.run)}: " + (f"kept in {launcher().printable(path)} (trusted): "
                                                            f"{launcher().printable(shown_added)}{made}" if added else
                                                            f"{launcher().printable(path)} already has it all"))
            return 0
        if args.command == "untaint":
            answer = ask(record["directory"], {"op": "untaint"})
            was = answer.get("was") if isinstance(answer.get("was"), dict) else {}
            out(f"{launcher().printable(record.get('run', args.run))}: " + (
                f"taint taken back ({tainted_text(was)})" if was.get("tainted") else "was not tainted"))
            return 0
        if getattr(args, "git_token_file", None):
            args.git_token_file = named_token_files(args.git_token_file)
        logins = git_requests(args)
        notes = []
        if logins:
            options = sorted({f"--git-token-{kind}" for kind, _ in logins})
            forbidden = [o for o in options if o in (launcher().load_policy().get("forbid") or [])]
            if forbidden:
                raise ValueError(f"{' '.join(forbidden)} is forbidden by the administrator's policy")
            if args.command == "allow":
                # Where each login goes: to the run's connection proxy (no token in the sandbox) wherever it can log
                # in; into the sandbox's session directory (for its Git helper: the agent can read the token) only
                # where it cannot, and only when the human says so.
                shown_run = launcher().printable(record.get("run", args.run))
                for kind, credential in logins:
                    why = proxy_login_refusal(record, credential)
                    credential["where"] = "proxy" if why is None else "sandbox"
                    if why is None:
                        continue
                    if not record.get("runtime_git_tokens"):
                        raise ValueError(f"{shown_run} takes no Git login during the run: {why}, and it was started "
                                         "with --token-proxy or by a sandbox-ai older than 0.1.37 (no Git helper "
                                         "for one in the sandbox); start it again with the --git-token-* option")
                    if not getattr(args, "git_token_in_sandbox", False):
                        raise ValueError(
                            f"{who(record, args.run)} cannot be given the Git login for {printable(credential['url'])} "
                            f"through its connection proxy: {why}. It can only be put into the sandbox's session "
                            "directory for its Git helper, where the agent can read the token: to give it so, add "
                            "--in-sandbox (sandbox-ai git-logins give ... --in-sandbox; net allow: "
                            "--git-token-in-sandbox); or start it again with the login")
                for kind, credential in logins:
                    if kind == "file":
                        check_token_file(record, credential["file"])
                notes = git_network(args, logins)
        keys = api_key_requests(args, record) if args.api_key else []
        if keys and args.command == "allow":
            # Read every key before anything changes: a missing secret refuses the whole command. Its host is allowed
            # with it (a key for a host the sandbox cannot reach would go nowhere).
            key_entries = launcher().api_key_entries(argparse.Namespace(api_key=args.api_key))
            if len(launcher().RUNTIME.checked_api_keys(key_entries)) != len(key_entries):  # as the supervisor checks
                raise ValueError("--api-key: a key of at most 4096 characters, usable in a header")
            args.allow_host = list(dict.fromkeys(args.allow_host + [host for host, _, _ in keys
                                                                    if host not in remembered(record)]))
        through = vpn_resolver(record) if resolver is resolve else None
        public_only = getattr(args, "public_only", False)
        if public_only and (args.allow_domain or args.allow_tcp or args.allow or args.presets or args.dns or
                            args.nameserver or getattr(args, "allow_host_port", None) or logins or args.api_key or
                            not args.allow_host or any(h.startswith("*.") for h in args.allow_host)):
            raise ValueError("--public-only goes with --allow-host NAME alone (names, no wildcard and no other "
                             "option)")
        chosen = resolve_with(through) if through else resolver
        if args.command == "allow":
            confirm_shared(args)  # --allow-host '*.D' below a shared service: --yes, or asked on a terminal
        request, names, hosts = request_for(args, args.command, public_resolver(chosen, notes) if public_only
                                            else chosen)
        wildcards = request.pop("wildcards", [])
        shared = request.pop("shared_wildcards", [])
        ports = request.get("host_ports", [])
        network = request["nets"] or request["https"] or request["tcp"] or request.get("dns") or hosts or ports
        if not (network or logins or wildcards or keys):
            raise ValueError(f"nothing to {args.command}: give --allow-host, --allow-domain, --allow-tcp, --allow, "
                             "--dns, --allow-host-port, a preset, a --git-token-* login or an --api-key")
        if ports and args.command == "allow":
            if "--allow-host-port" in (launcher().load_policy().get("forbid") or []):
                raise ValueError("--allow-host-port is forbidden by the administrator's policy")
            web = sorted(set(ports) & set(launcher().web_service_ports()))
            if web:  # as at the start: the web interface controls every sandbox
                raise ValueError(f"--allow-host-port {web[0]}: the web interface (sandbox-ai web) listens there; no "
                                 "sandbox may reach it")
            own_port(record, ports)
            if not record.get("host_relay"):
                raise ValueError(f"{launcher().printable(record.get('run', args.run))} cannot relay host ports: it was "
                                 "started by a sandbox-ai older than 0.1.37; start it again with --allow-host-port")
            if tor_record(record) and not record.get("vpn_bypass_host_ports"):
                # As at the start: a host port lies past Tor (vpn.check_tor_host_ports).
                raise ValueError(f"{launcher().printable(record.get('run', args.run))}: " + launcher().load_vpn_module()
                                 .host_ports_past_tor(sorted(ports), "--vpn", record["vpn_profile"]))
        if args.command == "allow":
            telemetry_refusal(record, args)
            inside_subnet(record, request)
            # The run's security mode (run.json): what it refuses at the start stays closed during the run. The
            # supervisor checks it as well.
            own_hosts = [h for h in args.allow_host if h not in args.domain_hosts and h not in shared]
            wanted = (["--allow-shared-wildcard"] if shared else []) + \
                (["--allow"] if args.allow else []) + (["--allow-host"] if own_hosts else []) + \
                (["--allow-domain"] if args.allow_domain else []) + \
                (["--allow-tcp"] if args.allow_tcp else []) + [f"--allow-{p}" for p in dict.fromkeys(args.presets)] + \
                (["--dns"] if request.get("dns") else []) + (["--allow-host-port"] if ports else []) + \
                (["--api-key"] if keys else [])
            refused = launcher().MODES.runtime_refusal(record.get("mode"), wanted, record.get("run", args.run))
            run_mode = record.get("mode") if isinstance(record.get("mode"), dict) else {}
            if not refused and shared and "--allow-shared-wildcard" not in (run_mode.get("refuses") or []):
                # A run whose mode record predates --allow-shared-wildcard: what it stands for, --allow-host, decides.
                refused = launcher().MODES.runtime_refusal(record.get("mode"), ["--allow-host"],
                                                           record.get("run", args.run))
            if not refused and args.domain_hosts:
                # A run whose mode record predates --allow-domain: what it stands for, --allow-host, decides.
                refused = launcher().MODES.runtime_refusal(record.get("mode"), ["--allow-host"],
                                                           record.get("run", args.run))
            if refused:
                raise ValueError(refused)
        if wildcards:
            change_wildcards(record, args.command, wildcards, apply=False, shared=shared)
        if args.command == "revoke" and hosts:
            # The addresses these names had when they were allowed, minus those another name allowed during
            # the run still needs (a CDN serves many names from one address).
            known = remembered(record)
            missing = [host for host in hosts if host not in known and host not in launcher().HOST_NETWORKS]
            if missing:
                raise ValueError(f"{', '.join(missing)} was not allowed during this run")
            kept = {a for host, entry in known.items() if host not in hosts for a in entry.get("addresses", [])}
            mine = {a for host in hosts for a in known.get(host, {}).get("addresses", [])}
            request["https"] = sorted((set(request["https"]) | {f"{a}/32" if ":" not in a else f"{a}/128" for a in mine})
                                      - {f"{a}/32" if ":" not in a else f"{a}/128" for a in kept})
        if getattr(args, "keep", False):
            keep_allowed()  # as net show --keep: before anything changes
            if public_only:
                if not record.get("workspace"):
                    raise ValueError("the run does not name its workspace (started by an older sandbox-ai): nothing "
                                     "can be remembered for it")
            else:
                keep_in_project(record, [], write=False)  # a --keep that cannot be done changes nothing
        if logins and args.command == "allow":
            # Read every token before anything changes: a missing variable or secret refuses the whole command.
            tokens = {credential["url"]: launcher().git_token_value(credential, kind) for kind, credential in logins}
        if logins and args.command == "revoke":  # refused before anything changes, the network too
            missing = {credential["url"] for _, credential in logins} - {entry["url"] for entry in git_logins(record)}
            if missing:
                raise ValueError(f"no Git login for {', '.join(sorted(missing))} was added during this run")
        name = who(record, args.run)
        # The credentials first, then the network: a login or key that cannot be written changes nothing else, and
        # one written before a network change that fails is put back as it was (restore) - never half of a command.
        restore = []
        try:
            if logins:
                # A login given for a server replaces the one given for it before, wherever that one was (the
                # proxy's, the sandbox's); a revoke takes it from both.
                giving = args.command == "allow"
                urls = {credential["url"] for _, credential in logins}
                inside = [credential for _, credential in logins if giving and credential.get("where") == "sandbox"]
                through = [credential for _, credential in logins if giving and credential.get("where") == "proxy"]
                before = sandbox_git_logins(record)
                if inside or any(entry["url"] in urls for entry in before):
                    file = Path(record["session_dir"]) / launcher().RUNTIME_GIT_TOKENS
                    existed = file.exists()
                    write_git_logins(record, [entry for entry in before if entry["url"] not in urls] + [
                        {"url": credential["url"], "user": credential["user"], "token": tokens[credential["url"]]}
                        for credential in inside])
                    restore.append(lambda: write_git_logins(record, before) if existed else file.unlink())
                    # The agent can read these tokens: the run's secret guard knows them from now on (and no longer
                    # those taken back); put back with the logins when the command fails.
                    register_sandbox_logins(record)
                    restore.append(lambda: register_sandbox_logins(record))
                if proxy_git_file(record) is not None and (through or any(
                        entry["url"] in urls for entry in proxy_git_logins(record))):
                    with git_logins_lock(record):  # read, give and keep as one step: two at once lose no login
                        proxy_before = proxy_git_logins(record)

                        def give_back_logins(previous=proxy_before):
                            with git_logins_lock(record):
                                write_proxy_git_logins(record, previous)
                        # Before the write: the supervisor may have the logins already when the run's own list
                        # cannot be kept (a full disk) - given back as they were then, never left unlisted.
                        restore.append(give_back_logins)
                        write_proxy_git_logins(
                            record, [entry for entry in proxy_before if entry["url"] not in urls] + [
                                {**{key: credential[key] for key in ("url", "user", "host", "port", "loopback")},
                                 "token": tokens[credential["url"]]} for credential in through],
                            options={f"--git-token-{kind}" for kind, _ in logins})
            if keys:
                names_given = {key_name for _, key_name, _ in keys}
                if args.command == "allow":
                    key_entries = [{**entry, "secret": secret}
                                   for entry, (_, _, secret) in zip(key_entries, keys)]
                with api_keys_lock(record):  # read, give and keep as one step: two at once lose no key
                    keys_before = added_api_keys(record)
                    kept_keys = [entry for entry in keys_before if entry["name"] not in names_given]

                    def give_back_keys(previous=keys_before):
                        with api_keys_lock(record):
                            write_api_keys(record, previous)
                    # Before the write: the supervisor may have the keys already when the run's own list cannot be
                    # kept (a full disk) - they are given back as they were then, never left with the proxy unlisted.
                    restore.append(give_back_keys)
                    write_api_keys(record, kept_keys + (key_entries if args.command == "allow" else []))
            result = change_network(record, args, request, network, ports)
        except BaseException:
            if args.command == "allow":  # (a revoke keeps what it took back: the safe half)
                for undo in reversed(restore):
                    with contextlib.suppress(ValueError, OSError):
                        undo()
            raise
        # What changed, in launcher.log: names and addresses, ports, presets, how many logins and keys (never theirs).
        addresses = {h: " ".join(names.get(h, []) if isinstance(names, dict) else []) or "no address" for h in hosts}
        LOG.info("net", f"{args.command} for {name} (run {record.get('run')}): done: " + "; ".join(part for part in (
            "hosts " + ", ".join(f"{h} ({shown})" for h, shown in addresses.items()) if hosts else "",
            f"wildcards {', '.join(wildcards)}" if wildcards else "",
            f"TCP {', '.join(args.allow_tcp)}" if args.allow_tcp else "",
            f"addresses {', '.join(args.allow)}" if args.allow else "",
            f"presets {', '.join(dict.fromkeys(args.presets))}" if args.presets else "",
            f"host ports {', '.join(map(str, ports))}" if ports else "", "DNS" if request.get("dns") else "",
            f"{len(logins)} Git logins" if logins else "", f"API keys {', '.join(k for _, k, _ in keys)}"
            if keys else "") if part) + (f"; notes: {'; '.join(notes)}" if notes else ""))
        if args.command == "allow":
            update_hosts(record, names)
        elif hosts:
            update_hosts(record, {}, remove=hosts)
        if hosts or args.allow_tcp:  # what `net show --keep` keeps (given.json): you gave it, no approval did
            note_given(record, args.command, hosts,
                       launcher().tcp_targets(argparse.Namespace(allow_tcp=args.allow_tcp)))
        if wildcards:
            now = change_wildcards(record, args.command, wildcards, shared=shared)
            out(f"{name}: host wildcards: {', '.join(now) or 'none'}" + (
                "; the names opened below them stay open for the run (revoke them by name)"
                if args.command == "revoke" else "; each name below them is opened as it is looked up"))
        for _, credential in logins:
            if args.command != "allow":
                out(f"{name}: Git login for {printable(credential['url'])} taken back")
            elif credential.get("where") == "proxy":
                out(f"{name}: Git login for {printable(credential['url'])} (user {printable(credential['user'])}): its "
                    "connection proxy logs in with it on the wire until the run ends (clone, fetch, push; it wins over "
                    "a login the run was started with for that server); the token does not enter the sandbox")
            else:
                out(f"{name}: Git login for {printable(credential['url'])} (user {printable(credential['user'])}): its "
                    "Git logs in with it until the run ends - IN THE SANDBOX: the token is in its session directory, "
                    "where the agent can read it (its connection proxy cannot log in there)")
        for host, key_name, _ in keys:
            if args.command == "allow":
                out(f"{name}: API key {key_name} for {host}: in the sandbox, "
                    f"export {key_name}={launcher().API_KEY_PLACEHOLDER.format(name=key_name)} (the placeholder; the "
                    "proxy puts the key in on the wire, the agent never sees it)")
            else:
                out(f"{name}: API key {key_name} taken back")
        if not brief:
            show(result, out, names=remembered(record), run=record.get("run"), logins=git_logins(record),
                 wildcards=run_wildcards(record), api_keys=added_api_keys(record), title=name)
        for note in notes:
            out(f"note: {launcher().printable(note)}")
        if args.command == "allow":
            options = (" ".join(f"--allow-shared-wildcard {h}" if h in shared else f"--allow-host {h}"
                                for h in args.allow_host if h not in args.domain_hosts)
                       + "".join(f" --allow-domain {d}" for d in dict.fromkeys(launcher().domain_of(v)
                                                                                for v in args.allow_domain))
                       + "".join(f" --allow-{p}" for p in args.presets)
                       + "".join(f" --allow-tcp {v}" for v in args.allow_tcp)
                       + "".join(f" --allow {v}" for v in args.allow)
                       + ("".join(f" --nameserver {v}" for v in args.nameserver) or (" --dns" if args.dns else ""))
                       + "".join(f" --allow-host-port {v}" for v in ports)
                       + "".join(f" --git-token-{kind} {value}" for kind in GIT_KINDS
                                 for value in getattr(args, f"git_token_{kind}", None) or [])
                       + "".join(f" --api-key {value}" for value in args.api_key)
                       ).strip()
            if getattr(args, "keep", False) and public_only:
                # As an answered lookup "always": its public addresses at every later start (remembered_pins).
                for host in hosts:
                    approvals_module().remember(record["workspace"], host=host,
                                                details={"kind": "dns", "verified": "lookup",
                                                         "asked": f"net allow --allow-host {host} --public-only"})
                out(f"{name}: allowed always for this project, its public addresses at every start: "
                    f"{', '.join(hosts)} (sandbox-ai approvals remembered; approvals forget)")
            elif getattr(args, "keep", False):
                made = new_project_file_note(record)
                path, added = keep_in_project(record, kept_options(args, ports))
                if added:
                    out(f"{name}: kept in {launcher().printable(path)} (trusted): {' '.join(added)}{made}")
                else:
                    out(f"{name}: {launcher().printable(path)} already has it")
            elif public_only:
                out(f"{name}: allowed until the sandbox ends (its public addresses); add --keep to allow it always "
                    "for this project, as an approval")
            else:
                out(f"{name}: allowed until the sandbox ends; to keep it, start with: {options} "
                    f"(or add --keep: into the project's .sandbox-ai.json)")
        return 0
    except (ValueError, OSError) as exc:  # OSError: a run directory or socket gone meanwhile; never a traceback
        where = getattr(exc, "filename", None)
        text = f"{exc.strerror}: {where}" if getattr(exc, "strerror", None) and where else \
            getattr(exc, "strerror", None) or exc
        print(f"sandbox-ai {prog}: {text}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
