#!/usr/bin/env python3
"""sandbox-ai net: change what a running sandbox may reach, without restarting it.

  sandbox-ai net list                                  running sandboxes
  sandbox-ai net show [RUN]                            what was allowed during the run
  sandbox-ai net allow [RUN] [--allow-host HOST] [--allow-tcp IP:PORT] [--allow IP_OR_CIDR] [--allow-PRESET]
                       [--dns] [--nameserver IP]           real DNS lookups (as --dns at the start)
                       [--git-token-env|--git-token-file|--git-token-secret URL=REF]   a Git login
                       [--allow-host-port PORT]            the host's 127.0.0.1:PORT, through a relay
  sandbox-ai net revoke [RUN] (the same options)       take back what `allow` gave
  sandbox-ai net untaint [RUN]                         take back the run's taint (untrusted content entered it)

RUN is the name a sandbox shows to its agent as $SANDBOX_AI_RUN, or the name of its start (see `list`,
`sandbox-ai list`); it may be left out
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
import shutil
import signal
import re
import socket
import stat
import subprocess
import sys
import threading
import time

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
        _textout = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_textout)
    return _textout


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_net", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


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


def run_dirs():
    """Every place a run of this user may have registered, also one started with another XDG_RUNTIME_DIR
    (or none): launcher().runs_dir() first, then $XDG_RUNTIME_DIR/sandbox-ai/runs, /run/user/UID/sandbox-ai/runs
    and the state directory's runs; each real directory once."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "runs"
    candidates = [launcher().runs_dir()]
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


def running():
    """{run: record} of the sandboxes registered and still running (the first of a name wins); stale
    entries are removed."""
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
                shutil.rmtree(entry, ignore_errors=True)
                continue
            record["directory"] = str(entry)
            runs.setdefault(record.get("run", entry.name), record)
    return runs


def choose(runs, name, cwd=None):
    """The record of run NAME (its id, or the name of its start), or of the only one (for CWD) when NAME is
    None."""
    if name is not None:
        if name in runs:
            return runs[name]
        named = [record for record in runs.values() if record.get("name") == name]
        if len(named) == 1:
            return named[0]
        printable = launcher().printable
        if named:
            raise ValueError(f"several sandboxes are named {printable(name)}; name one by its run: "
                             f"{', '.join(printable(r.get('run', '?')) for r in named)}")
        raise ValueError(f"no running sandbox '{printable(name)}'; running: "
                         f"{', '.join(printable(r) for r in runs) or 'none'}")
    if len(runs) == 1:
        return next(iter(runs.values()))
    cwd = str(Path(cwd or os.getcwd()).resolve())
    here = [record for record in runs.values() if record.get("workspace") == cwd]
    if len(here) == 1:
        return here[0]
    if not runs:
        raise ValueError("no sandbox is running")
    raise ValueError(f"several sandboxes run; name one: {', '.join(runs)}")


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
    answer = ask(record["directory"], {"op": "wildcards", "add" if op == "allow" else "remove": wildcards})
    now = sorted(f"*.{suffix}" for suffix in answer.get("wildcards", []))
    temporary = wildcards_file(record).with_name(".wildcards.json")
    temporary.write_text(json.dumps(now))
    os.replace(temporary, wildcards_file(record))
    return now


# Longer than a firewall change may take in the sandbox (runtime.NFT_SECONDS, 30 s): a change the supervisor
# applies late must not leave this command believing it failed, with names.json never written.
ASK_SECONDS = 45


def ask(directory, request, timeout=ASK_SECONDS):
    """The supervisor's answer to REQUEST on the run's control socket; ValueError when it refuses, does not answer
    (paused, or busy longer than TIMEOUT) or the socket fails."""
    if (Path(directory) / "paused").exists():
        raise ValueError("the sandbox is paused: resume it first (sandbox-ai resume NAME), then change it")
    try:
        return ask_socket(directory, request, timeout)
    except (OSError, TimeoutError) as exc:  # socket.timeout is an OSError
        raise ValueError(f"the sandbox did not answer ({getattr(exc, 'strerror', None) or exc or 'timed out'}); "
                         "see sandbox-ai net show") from None


def ask_socket(directory, request, timeout):
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
        client.sendall(json.dumps(request).encode() + b"\n")
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
    """(state letter, start time in clock ticks after boot) of PID from /proc/PID/stat, or None if gone."""
    try:
        # comm (field 2) may contain spaces and parentheses: the fields after it follow the last ')'.
        fields = (PROC / str(pid) / "stat").read_text().rpartition(")")[2].split()
        return fields[0], int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def started_at(ticks):
    """Seconds since the epoch of a process start given in clock ticks after boot."""
    for line in (PROC / "stat").read_text().splitlines():
        if line.startswith("btime "):
            return int(line.split()[1]) + ticks / os.sysconf("SC_CLK_TCK")
    raise ValueError("cannot tell when this machine booted (no btime in /proc/stat)")


def launcher_process(record):
    """(PID, start ticks) of the launcher RECORD registered, after checking that the PID still is that
    launcher: this user's, of the recorded start time (or, for records without one, started no later
    than the registration) and running agent_sandbox.py. ValueError otherwise."""
    name, pid = record.get("run"), record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError(f"{name}: its record names no usable PID")
    try:
        owner = os.stat(PROC / str(pid)).st_uid
    except OSError:
        raise ValueError(f"{name} has ended (PID {pid} is gone)") from None
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
        if state is None or started_at(state[1]) > registered + START_SLACK:
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
                       help="keep it all for the next starts: into the project's .sandbox-ai.json (trusted), as the "
                            "options the run was given since its start (names, wildcards, addresses, DNS, host ports, "
                            "devices, workspaces added; no Git logins)")
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
    return tokens


HOST_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,62}")


def running_options(record, result):
    """(options, workspaces, notes): what the running sandbox RECORD was given during the run (RESULT: its `show`
    state), as start options for its project file: the names allowed (net allow, presets, approved lookups) and the
    host wildcards, addresses allowed without a name, TCP targets, networks, nameservers, host ports, devices, and the
    workspaces added (`sandbox-ai workspace add`). Git logins are not kept (their tokens stay out of project files).
    RESULT comes from the supervisor, which runs beside the agent: every value is checked again before it goes into
    a file that is trusted at the next start, and each option is written with its own value."""
    l, notes, tokens = launcher(), [], []

    def address(value):
        return str(ipaddress.ip_address(str(value).split("%")[0]))
    names = remembered(record)
    started = set(record.get("allow_wildcards") or [])
    wildcards = [w for w in run_wildcards(record) if w not in started]
    every = list(started) + wildcards
    named = set()
    for host in sorted(names):
        entry = names[host]
        named |= {bare(a) for a in (entry.get("addresses", []) if isinstance(entry, dict) else [])}
        if not HOST_NAME.fullmatch(str(host)):
            notes.append(f"{l.printable(str(host))[:100]}: not kept (not a host name)")
        elif not any(str(host).endswith(w[1:]) for w in every):  # below a wildcard: the wildcard is kept, not each
            tokens += ["--allow-host", host]
    for wildcard in wildcards:
        l.RUNTIME.wildcard_suffix(wildcard)
        tokens += ["--allow-host", wildcard]
    for value in sorted({bare(n) for n in result.get("https", [])} - named, key=address_sort):
        if "/" not in value:  # a single address allowed by an approval: its port, not a whole network
            value = address(value)
            tokens += ["--allow-tcp", f"[{value}]:443" if ":" in value else f"{value}:443"]
    for value, port in sorted({(bare(a), int(p)) for a, p in result.get("tcp", [])},
                              key=lambda ap: (address_sort(ap[0]), ap[1])):
        value = address(value)
        if not 1 <= port <= 65535:
            raise ValueError(f"the sandbox reported port {port}")
        tokens += ["--allow-tcp", f"[{value}]:{port}" if ":" in value else f"{value}:{port}"]
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
        if entry.get("mode") == "ro" and not checks.get("read_only"):
            notes.append(f"{entry['host']} was added read-only: not kept (a project file has no read-only workspace "
                         "of its own; start with --workspace-ro, or add it again with --ro)")
            continue
        if os.path.isabs(entry["host"]) and "," not in entry["host"] and entry["host"].isprintable():
            workspaces.append(entry["host"])
    if git_logins(record):
        notes.append("Git logins added during the run are not kept (tokens never go into a project file): start with "
                     "--git-token-env / --git-token-secret, or add one with sandbox-ai git-logins")
    return tokens, workspaces, notes


_workspaces_module = None


def load_workspaces_module():
    global _workspaces_module
    if _workspaces_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_workspaces_for_net", HERE / "workspaces.py")
        _workspaces_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_workspaces_module)
    return _workspaces_module


def keep_in_project(record, tokens, write=True, workspaces=()):
    """Add TOKENS (options) to the run's project file, WORKSPACE/.sandbox-ai.json, and trust the result: only while
    the file is exactly what you trusted (an agent's change is never trusted along), or a new one for the run's
    agent. Returns (path, [added options]); ValueError otherwise. WRITE False only checks (before the run changes:
    a refused --keep changes nothing; the write checks again)."""
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
                            [[printable(name), printable(str(r.get("name") or r.get("session") or "-")),
                              printable(r.get("agent", "")), printable(r.get("started", "")),
                              printable(r.get("workspace", ""))]
                             for name, r in runs.items()], out, indent=0, empty="no sandbox is running")
            return 0
        record = choose(runs, args.run)
        if args.command == "stop":
            if sys.stderr.isatty() and not os.environ.get("SANDBOX_AI_NO_DEPRECATION"):
                print(f"note: sandbox-ai net stop is the older form; sandbox-ai stop "
                      f"{launcher().printable(record.get('name') or record.get('run', args.run))} ends any start "
                      "(also sessions and MCP servers)", file=sys.stderr)
            return stop(record, args.wait, out)
        if args.command == "show":
            result = ask(record["directory"], {"op": "show"})
            show(result, out, names=remembered(record), run=record.get("run", args.run), as_json=args.json,
                 logins=git_logins(record), wildcards=run_wildcards(record))
            if getattr(args, "keep", False):
                tokens, workspaces, notes = running_options(record, result)
                path, added = keep_in_project(record, tokens, workspaces=workspaces)
                said = (lambda line: print(line, file=sys.stderr)) if args.json else out  # the JSON stays JSON
                for note in notes:
                    said(f"note: {launcher().printable(note)}")
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
        request, names, hosts = request_for(args, args.command, resolver)
        wildcards = request.pop("wildcards", [])
        ports = request.get("host_ports", [])
        network = request["nets"] or request["https"] or request["tcp"] or request.get("dns") or hosts or ports
        if not (network or logins or wildcards):
            raise ValueError(f"nothing to {args.command}: give --allow-host, --allow-tcp, --allow, --dns, "
                             "--allow-host-port, a preset or a --git-token-* login")
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
                (["--dns"] if request.get("dns") else []) + (["--allow-host-port"] if ports else [])
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
            keep_in_project(record, [], write=False)  # a --keep that cannot be done changes nothing
        if logins and args.command == "allow":
            # Read every token before anything changes: a missing variable or secret refuses the whole command.
            tokens = {credential["url"]: launcher().git_token_value(credential, kind) for kind, credential in logins}
        if ports and args.command == "allow":
            for port in ports:
                start_relay(record, port)  # the host end first: the sandbox's end connects to its socket
        try:
            result = ask(record["directory"], request if network else {"op": "show"})
        except ValueError:
            if ports and args.command == "allow":
                for port in ports:
                    if port not in (ask(record["directory"], {"op": "show"}).get("host_ports") or []):
                        stop_relay(record, port)
            raise
        if ports and args.command == "revoke":
            for port in ports:
                stop_relay(record, port)
        if args.command == "allow":
            update_hosts(record, names)
        elif hosts:
            update_hosts(record, {}, remove=hosts)
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
