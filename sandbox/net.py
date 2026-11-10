#!/usr/bin/env python3
"""sandbox-ai net: change what a running sandbox may reach, without restarting it.

  sandbox-ai net list                                  running sandboxes
  sandbox-ai net show [RUN]                            what was allowed during the run
  sandbox-ai net allow [RUN] [--allow-host HOST] [--allow-tcp IP:PORT] [--allow IP_OR_CIDR] [--allow-PRESET]
                       [--dns] [--nameserver IP]           real DNS lookups (as --dns at the start)
                       [--git-token-env|--git-token-file|--git-token-secret URL=REF]   a Git login
                       [--allow-host-port PORT]            the host's 127.0.0.1:PORT, through a relay
                       [--api-key HOST=NAME[=SECRET]]      an API key the proxy puts in (runs from 0.1.92)
  sandbox-ai net revoke [RUN] (the same options)       take back what `allow` gave
  sandbox-ai net untaint [RUN]                         take back the run's taint (untrusted content entered it)

RUN is the name a sandbox shows to its agent as $SANDBOX_AI_RUN, or the name of its start as `sandbox-ai list`
(ps) shows it, also after a rename; it may be left out
when only one sandbox runs, or only one for the current directory (not for `stop`). Changes last until
the sandbox ends; to keep them, add the same options to its start command or its .sandbox-ai.json. Only
you can make them: the agent cannot reach the control socket. Connections that are already open stay
open. A sandbox is ended with `sandbox-ai stop NAME` (any start: a run, a session, an MCP server);
`sandbox-ai net stop RUN [--wait SECONDS]` is its older form for a run, kept for scripts and the app.

Standard library only.
"""
import argparse
import calendar
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
_textout = None


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
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_net", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


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
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    if isinstance(start, int) and not isinstance(start, bool):
        now = launcher().process_start(int(pid))
        if now is not None and now != start:
            return False
    return True


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
            except (OSError, ValueError):
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
            runs.setdefault(record.get("run", entry.name), record)
    return runs


def forget_ended(directory, record):
    """The run in DIRECTORY (RECORD: its run.json) ended without its launcher's cleanup (the launcher was killed):
    its end goes into its name's history and its start's exit.json (once), its pod goes if Podman still lists it
    with nothing running, then its directory and its browser directory."""
    with contextlib.suppress(Exception):  # the record is read from disk: whatever it holds, the cleanup goes on
        launcher().load_starts_module().run_event(directory, "exit")
    with contextlib.suppress(Exception):
        liveness().remove_ended_pod(record)
    liveness().discard(directory)


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


def labels(runs):
    """{run: what to call it}: its name as `sandbox-ai list` shows it, with "(run RUN)" when that name is not unique
    among RUNS."""
    named = {run: label(run, record) for run, record in runs.items()}
    counts = {}
    for name in named.values():
        counts[name] = counts.get(name, 0) + 1
    return {run: name if counts[name] == 1 or name == run else f"{name} (run {run})" for run, name in named.items()}


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
    "limits": "start it first (sandbox-ai resume {name}), then: sandbox-ai limits {name} --memory|--cpus|--pids ...",
    "devices add": "start it first (sandbox-ai resume {name}), then: sandbox-ai devices add {name} DEVICE",
    "devices remove": "what was added during its run ended with it; start it again with: sandbox-ai resume {name}",
    "devices list": "its devices are listed while it runs; it starts again with: sandbox-ai resume {name}",
    "canaries resume": "only a running sandbox a canary paused waits to continue",
    "approvals history": "the questions of a run are kept while it runs; what happened during its last run: "
                         "sandbox-ai activity {name}",
}


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


def request_for(args, op, resolver=resolve):
    """(request for the supervisor, {host: addresses} for /etc/hosts) from the command line."""
    l = launcher()
    wildcards = sorted({h for h in args.allow_host if h.startswith("*.")})
    for wildcard in wildcards:
        try:
            l.RUNTIME.wildcard_suffix(wildcard)
        except ValueError as exc:
            raise ValueError(f"--allow-host {wildcard}: {exc}") from None
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
    return request, names, hosts


def wildcards_file(record):
    """The wildcards added during the run (run directory, outside the sandbox): approvals.watch reads them."""
    return Path(record["directory"]) / "wildcards.json"


def run_wildcards(record):
    """Every *.DOMAIN of the run: the start's (run.json allow_wildcards) and those added since."""
    found = [w for w in record.get("allow_wildcards") or [] if isinstance(w, str)]
    try:
        added = json.loads(wildcards_file(record).read_text())
    except (OSError, ValueError):
        added = []
    found += [w for w in added if isinstance(w, str)] if isinstance(added, list) else []
    return sorted(set(found))


def change_wildcards(record, op, wildcards, apply=True):
    """Add (allow) or take back (revoke) WILDCARDS during the run: the supervisor's DNS stub holds the lookups below
    them, the launcher answers those on the host. Only for a run that asks questions. APPLY False only checks
    (before anything of the command changes)."""
    l = launcher()
    started = set(record.get("allow_wildcards") or [])
    if op == "allow":
        try:
            asks = json.loads((Path(record["session_dir"]) / "config.json").read_text()).get("ask")
        except (OSError, ValueError, KeyError):
            asks = None
        if not asks:
            raise ValueError("--allow-host *.DOMAIN needs a sandbox that asks (not one started with --ask off): each "
                             "name below it is opened as its lookup is answered")
        try:
            resolv = (Path(record["session_dir"]) / "resolv.conf").read_text()
        except (OSError, KeyError):
            resolv = ""
        if "nameserver 127.0.0.1" not in resolv:
            raise ValueError("--allow-host *.DOMAIN: this sandbox resolves names past its DNS stub (started with --dns "
                             "or --nameserver), so the names below it would resolve but stay closed; name the hosts "
                             "one by one")
        policy = l.load_policy()
        ceiling = argparse.Namespace(policy={k: policy[k] for k in ("allow_within", "allow_hosts") if k in policy})
        outside = [w for w in wildcards if not l.within_ceiling(ceiling, host=w)]
        if outside:
            raise ValueError(f"{', '.join(outside)} is not among the hosts the administrator's policy allows "
                             "(allow_hosts)")
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
        answer = ask(record["directory"], {"op": "wildcards", "add" if op == "allow" else "remove": wildcards})
        now = sorted(f"*.{suffix}" for suffix in answer.get("wildcards", []))
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
        except (OSError, ValueError, AttributeError):
            name = "NAME"
        raise ValueError(f"the sandbox is paused: resume it first (sandbox-ai resume {name}), then change it")
    if sys.platform == "darwin":  # podman machine: the control socket is in its VM
        try:
            record = json.loads((Path(directory) / "run.json").read_text())
        except (OSError, ValueError):
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
    except ValueError:
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


def git_logins(record):
    """[{"url", "user", "token"}] of the Git logins added during the run (the session directory's file)."""
    try:
        data = json.loads((Path(record["session_dir"]) / launcher().RUNTIME_GIT_TOKENS).read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return []
    logins = data.get("logins") if isinstance(data, dict) else None
    return [entry for entry in logins if isinstance(entry, dict) and isinstance(entry.get("url"), str)] \
        if isinstance(logins, list) else []


def write_git_logins(record, logins):
    """Replace the run's Git logins with LOGINS: a file of yours (0600), written whole and renamed into place, so
    the sandbox's helper never reads half of it. The session directory goes when the run ends, and the logins
    with it."""
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
    except (OSError, ValueError):
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
    try:
        ask(record["directory"], {"op": "api-keys", "api_keys": entries})
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
    except (OSError, ValueError):
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


def show(result, out=print, names=None, run=None, as_json=False, logins=(), wildcards=()):
    """What was allowed during the run, grouped by kind; HTTPS addresses under the names they were allowed
    for (from `net allow --allow-host`, presets and approved lookups; never looked up anew: a name the
    agent chose may carry data)."""
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
    git = [{"url": entry["url"], "user": entry.get("user")} for entry in logins]  # never the token
    if as_json:
        out(json.dumps({"run": run, "all_ports": nets, "dns": dns, **({"taint": taint} if taint is not None else {}),
                        "host_wildcards": list(wildcards),
                        "git": git, "host_ports": relayed,
                        "https": [{"address": a, "names": sorted(by_address.get(a, []))} for a in https],
                        "tcp": [{"address": a, "port": p, "names": sorted(by_address.get(a, []))} for a, p in tcp]},
                       indent=2))
        return
    t = textout()
    if taint is not None:
        printable = launcher().printable
        line = printable(tainted_text(taint))
        out(t.style(f"  untrusted content: {line}", "warn" if taint.get("tainted") else "muted"))
        if taint.get("tainted"):
            out(t.style(f"  take it back: sandbox-ai net untaint {printable(run) if run else '[RUN]'}", "muted"))
        out("")
    total = len(nets) + len(https) + len(tcp) + len(dns) + len(relayed)
    if git:
        t.section("Git logins added during this run", out, note="the tokens stay in the run's session directory")
        t.table(["SERVER", "USER"], [[printable(entry["url"]), printable(str(entry.get("user") or ""))] for entry in git],
                out)
        out("")
    if wildcards:
        t.section("Host wildcards", out, note="every name below them is opened as it is looked up")
        t.table(["WILDCARD"], [[printable(w)] for w in wildcards], out)
        out("")
    t.section(f"{printable(run) if run else 'this sandbox'}: allowed during this run", out,
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
    out(t.style(f"  take back: sandbox-ai net revoke {printable(run) if run else '[RUN]'} --allow-host NAME | "
                "--allow-tcp IP:PORT | --allow IP_OR_CIDR | --dns", "muted"))


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
    except (OSError, ValueError, KeyError, TypeError):
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


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai net", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n".join(__doc__.split("\n\n")[1:3]))
    commands = p.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="running sandboxes")
    listing.add_argument("-j", "--json", action="store_true", help="as JSON, for scripts")
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
    untainted = commands.add_parser("untaint", help="take back the run's taint: the rules for tainted runs no longer "
                                                    "hold, until untrusted content enters it again")
    untainted.add_argument("run", nargs="?")
    stopped = commands.add_parser("stop", help="older form of `sandbox-ai stop NAME` (a run only)")
    stopped.add_argument("run")
    stopped.add_argument("--wait", type=seconds, default=STOP_WAIT, metavar="SECONDS",
                         help=f"how long to wait for it to end (default {STOP_WAIT}; 0: do not wait)")
    for name in ("allow", "revoke"):
        command = commands.add_parser(name, help=f"{name} destinations while the sandbox runs")
        command.add_argument("run", nargs="?")
        command.add_argument("-A", "--allow-host", action="append", default=[], metavar="HOST", type=str.lower,
                             help="pin HOST in the sandbox's /etc/hosts and allow TCP/443 to it")
        command.add_argument("-t", "--allow-tcp", action="append", default=[], metavar="IP:PORT")
        command.add_argument("-a", "--allow", action="append", default=[], metavar="IP_OR_CIDR",
                             help="every protocol and port")
        command.add_argument("-R", "--dns", action="store_true",
                             help="real DNS: the sandbox's DNS stub forwards every name to "
                                  f"{' and '.join(launcher().DEFAULT_NAMESERVERS)} (each lookup logged); addresses "
                                  "stay closed unless allowed as well")
        command.add_argument("-ns", "--nameserver", action="append", default=[], metavar="IP",
                             help="this resolver instead of the defaults (implies --dns); repeatable")
        command.add_argument("-P", "--allow-host-port", action="append", default=[], type=port_number, metavar="PORT",
                             help="the host's 127.0.0.1:PORT (a local Git server, an LLM server), through a relay "
                                  "that only you start (runs started with sandbox-ai 0.1.37 or newer)")
        for kind, ref, where in (("env", "NAME", "your variable NAME"), ("file", "FILE", "FILE (yours, mode 0600)"),
                                 ("secret", "SECRET", "the secret SECRET of sandbox-ai secrets")):
            short = launcher().SHORT_OPTIONS.get(f"--git-token-{kind}")
            command.add_argument(*([short] if short else []), f"--git-token-{kind}", action="append", default=[],
                                 metavar=f"URL={ref}",
                                 help=f"a Git login for http(s)://[USER@]HOST[:PORT], the token from {where}; the "
                                      "sandbox's Git uses it at once (runs started with sandbox-ai 0.1.37 or newer, "
                                      "without --token-proxy)" if name == "allow" else
                                      f"take back the Git login for URL (the {ref} part is not read)")
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
    return p


def kept_options(args, ports):
    """The options of an allow command, as tokens for a project file (logins as their references)."""
    tokens = [token for host in args.allow_host for token in ("--allow-host", host)]
    tokens += [f"--allow-{preset}" for preset in dict.fromkeys(args.presets)]
    tokens += [token for value in args.allow_tcp for token in ("--allow-tcp", value)]
    tokens += [token for value in args.allow for token in ("--allow", value)]
    tokens += [token for value in args.nameserver for token in ("--nameserver", value)] or (["--dns"] if args.dns else [])
    tokens += [token for port in ports for token in ("--allow-host-port", str(port))]
    tokens += [token for kind in GIT_KINDS for value in getattr(args, f"git_token_{kind}", None) or []
               for token in (f"--git-token-{kind}", value)]
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
    for wildcard in wildcards:
        l.RUNTIME.wildcard_suffix(wildcard)
        tokens += ["--allow-host", wildcard]
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


def keep_in_project(record, tokens, write=True, workspaces=(), preview=None):
    """Add TOKENS (options) to the run's project file, WORKSPACE/.sandbox-ai.json, and trust the result: only while
    the file is exactly what you trusted (an agent's change is never trusted along), or a new one for the run's
    agent. Returns (path, [added options]); ValueError otherwise. WRITE False only checks (before the run changes:
    a refused --keep changes nothing; the write checks again). PREVIEW(path, added) is called before the write
    with exactly what it adds."""
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


def main(argv, out=print, resolver=resolve):
    if argv[:1] == ["--internal-relay"]:
        return relay_main(argv[1:])
    args = parser().parse_args(argv)
    try:
        runs = running()
        if args.command == "list":
            printable = launcher().printable
            if args.json:
                out(json.dumps([{"run": name, "name": r.get("name"), "agent": r.get("agent"),
                                 "workspace": r.get("workspace"), "started": r.get("started"),
                                 "session": r.get("session")} for name, r in runs.items()], indent=2))
                return 0
            textout().table(["RUN", "NAME", "AGENT", "SINCE", "WORKSPACE"],
                            [[printable(name), printable(str(label(name, r) if label(name, r) != name else
                                                             r.get("session") or "-")),
                              printable(r.get("agent", "")), printable(r.get("started", "")),
                              printable(r.get("workspace", ""))]
                             for name, r in runs.items()], out, indent=0, empty="no sandbox is running")
            return 0
        record = choose(runs, args.run, command=f"net {args.command}")
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
                 logins=git_logins(record), wildcards=run_wildcards(record))
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
                path, added = keep_in_project(record, tokens, workspaces=workspaces, preview=preview)
                shown_added = " ".join(added)
                said(f"{record.get('run', args.run)}: " + (f"kept in {launcher().printable(path)} (trusted): "
                                                            f"{launcher().printable(shown_added)}" if added else
                                                            f"{launcher().printable(path)} already has it all"))
            return 0
        if args.command == "untaint":
            answer = ask(record["directory"], {"op": "untaint"})
            was = answer.get("was") if isinstance(answer.get("was"), dict) else {}
            out(f"{launcher().printable(record.get('run', args.run))}: " + (
                f"taint taken back ({tainted_text(was)})" if was.get("tainted") else "was not tainted"))
            return 0
        logins = git_requests(args)
        notes = []
        if logins:
            options = sorted({f"--git-token-{kind}" for kind, _ in logins})
            forbidden = [o for o in options if o in (launcher().load_policy().get("forbid") or [])]
            if forbidden:
                raise ValueError(f"{' '.join(forbidden)} is forbidden by the administrator's policy")
            if not record.get("runtime_git_tokens"):
                raise ValueError(f"{launcher().printable(record.get('run', args.run))} takes no Git login during the run: "
                                 "it was started with --token-proxy or by a sandbox-ai older than 0.1.37; start it "
                                 "again with the --git-token-* option")
            if args.command == "allow":
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
        request, names, hosts = request_for(args, args.command, resolve_with(through) if through else resolver)
        wildcards = request.pop("wildcards", [])
        ports = request.get("host_ports", [])
        network = request["nets"] or request["https"] or request["tcp"] or request.get("dns") or hosts or ports
        if not (network or logins or wildcards or keys):
            raise ValueError(f"nothing to {args.command}: give --allow-host, --allow-tcp, --allow, --dns, "
                             "--allow-host-port, a preset, a --git-token-* login or an --api-key")
        if ports and args.command == "allow":
            if "--allow-host-port" in (launcher().load_policy().get("forbid") or []):
                raise ValueError("--allow-host-port is forbidden by the administrator's policy")
            web = launcher().web_service_port()
            if web in ports:  # as at the start: the web interface controls every sandbox
                raise ValueError(f"--allow-host-port {web}: the web interface (sandbox-ai web) listens there; no "
                                 "sandbox may reach it")
            if not record.get("host_relay"):
                raise ValueError(f"{launcher().printable(record.get('run', args.run))} cannot relay host ports: it was "
                                 "started by a sandbox-ai older than 0.1.37; start it again with --allow-host-port")
        if args.command == "allow":
            inside_subnet(record, request)
            # The run's security mode (run.json): what it refuses at the start stays closed during the run. The
            # supervisor checks it as well.
            wanted = (["--allow"] if args.allow else []) + (["--allow-host"] if args.allow_host else []) + \
                (["--allow-tcp"] if args.allow_tcp else []) + [f"--allow-{p}" for p in dict.fromkeys(args.presets)] + \
                (["--dns"] if request.get("dns") else []) + (["--allow-host-port"] if ports else []) + \
                (["--api-key"] if keys else [])
            refused = launcher().MODES.runtime_refusal(record.get("mode"), wanted, record.get("run", args.run))
            if refused:
                raise ValueError(refused)
        if wildcards:
            change_wildcards(record, args.command, wildcards, apply=False)
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
            keep_in_project(record, [], write=False)  # a --keep that cannot be done changes nothing
        if logins and args.command == "allow":
            # Read every token before anything changes: a missing variable or secret refuses the whole command.
            tokens = {credential["url"]: launcher().git_token_value(credential, kind) for kind, credential in logins}
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
        if args.command == "allow":
            update_hosts(record, names)
        elif hosts:
            update_hosts(record, {}, remove=hosts)
        if hosts or args.allow_tcp:  # what `net show --keep` keeps (given.json): you gave it, no approval did
            note_given(record, args.command, hosts,
                       launcher().tcp_targets(argparse.Namespace(allow_tcp=args.allow_tcp)))
        if wildcards:
            now = change_wildcards(record, args.command, wildcards)
            out(f"{record.get('run', args.run)}: host wildcards: {', '.join(now) or 'none'}" + (
                "; the names opened below them stay open for the run (revoke them by name)"
                if args.command == "revoke" else "; each name below them is opened as it is looked up"))
        if logins:
            urls = {credential["url"] for _, credential in logins}
            kept = [entry for entry in git_logins(record) if entry["url"] not in urls]
            if args.command == "revoke":
                missing = urls - {entry["url"] for entry in git_logins(record)}
                if missing:
                    raise ValueError(f"no Git login for {', '.join(sorted(missing))} was added during this run")
                write_git_logins(record, kept)
            else:
                write_git_logins(record, kept + [{"url": credential["url"], "user": credential["user"],
                                                  "token": tokens[credential["url"]]} for _, credential in logins])
        if keys:
            names_given = {name for _, name, _ in keys}
            with api_keys_lock(record):  # read, give and keep as one step: two at once lose no key
                kept_keys = [entry for entry in added_api_keys(record) if entry["name"] not in names_given]
                write_api_keys(record, kept_keys + (key_entries if args.command == "allow" else []))
            for host, name, _ in keys:
                if args.command == "allow":
                    out(f"{record.get('run', args.run)}: API key {name} for {host}: in the sandbox, "
                        f"export {name}={launcher().API_KEY_PLACEHOLDER.format(name=name)} (the placeholder; the "
                        "proxy puts the key in on the wire, the agent never sees it)")
                else:
                    out(f"{record.get('run', args.run)}: API key {name} taken back")
        show(result, out, names=remembered(record), run=record.get("run"), logins=git_logins(record),
             wildcards=run_wildcards(record))
        for note in notes:
            out(f"note: {launcher().printable(note)}")
        if args.command == "allow":
            options = (" ".join(f"--allow-host {h}" for h in args.allow_host)
                       + "".join(f" --allow-{p}" for p in args.presets)
                       + "".join(f" --allow-tcp {v}" for v in args.allow_tcp)
                       + "".join(f" --allow {v}" for v in args.allow)
                       + ("".join(f" --nameserver {v}" for v in args.nameserver) or (" --dns" if args.dns else ""))
                       + "".join(f" --allow-host-port {v}" for v in ports)
                       + "".join(f" --git-token-{kind} {value}" for kind in GIT_KINDS
                                 for value in getattr(args, f"git_token_{kind}", None) or [])
                       + "".join(f" --api-key {value}" for value in args.api_key)
                       ).strip()
            if getattr(args, "keep", False):
                path, added = keep_in_project(record, kept_options(args, ports))
                if added:
                    out(f"{record['run']}: kept in {launcher().printable(path)} (trusted): {' '.join(added)}")
                else:
                    out(f"{record['run']}: {launcher().printable(path)} already has it")
            else:
                out(f"{record['run']}: allowed until the sandbox ends; to keep it, start with: {options} "
                    f"(or add --keep: into the project's .sandbox-ai.json)")
        return 0
    except (ValueError, OSError) as exc:  # OSError: a run directory or socket gone meanwhile; never a traceback
        where = getattr(exc, "filename", None)
        text = f"{exc.strerror}: {where}" if getattr(exc, "strerror", None) and where else \
            getattr(exc, "strerror", None) or exc
        print(f"sandbox-ai net: {text}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
