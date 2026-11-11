#!/usr/bin/env python3
"""sandbox-ai logs: summarize a sandbox's logs and suggest options: blocked egress, file access, the
connection proxy's log (requests, model calls and tokens, pushes, credentials sent, refusals), processes
and the MCP tool calls of the servers.

  sandbox-ai logs [DIR]          a log directory (default: the wizard's one for the current project)
  sandbox-ai logs --project P    the wizard's log directory of project P
  sandbox-ai logs --json         machine-readable

Suggestions are options to review, not to apply blindly: every one widens the sandbox.
"""
import argparse
from collections import Counter, deque
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import threading

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent
_launcher = None


def default_log_dir(project):
    """launcher().default_log_dir, without loading the launcher for it (a test keeps both the same): the log
    directory the wizard uses for PROJECT, per project, also for equal directory names."""
    project = Path(project)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    return state / "sandbox-ai" / "logs" / f"{project.name or 'root'}-{digest}"


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_logs", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


# Bounds of what is read (the logs are not rotated and one connections-*.jsonl is written per run: read whole, a
# log directory of many runs took gigabytes - a machine froze swapping, `sandbox-ai logs` and the app's Statistics
# page alike). Each file: its newest TAIL_BYTES; all files of one kind together: TOTAL_BYTES, the newest files
# first; a line longer than LINE_BYTES is skipped. Records are counted as they are read, never all kept.
TAIL_BYTES = 32 << 20
TOTAL_BYTES = 128 << 20
LINE_BYTES = 1 << 20


def new_budget(total=None):
    """What the readers of one summary may still read and what they left out (older_lines_skipped)."""
    return {"left": TOTAL_BYTES if total is None else total, "skipped_bytes": 0, "read_bytes": 0, "lines": 0, "long": 0}


def iter_lines(path, budget=None, tail=None, line_bytes=None):
    """The lines of the newest TAIL bytes of PATH (and no more than BUDGET has left), as bytes; what was written
    after it was opened is not read (a running sandbox keeps writing). A missing file has none."""
    budget = new_budget() if budget is None else budget
    tail = TAIL_BYTES if tail is None else tail
    line_bytes = LINE_BYTES if line_bytes is None else line_bytes
    try:
        stream = open(path, "rb")
    except OSError:
        return
    with stream:
        try:
            size = os.fstat(stream.fileno()).st_size
        except OSError:
            return
        wanted = max(0, min(tail, budget["left"]))
        start = max(0, size - wanted)
        budget["skipped_bytes"] += start
        # One byte before START: when START is the first byte of a line, the newline before it ends the skip at once
        # (that line was lost as if it were a cut one).
        position = max(0, start - 1)
        stream.seek(position)
        if start:  # into the middle of a line: from the next one
            while position < size:
                piece = stream.readline(line_bytes)
                position += len(piece)
                if not piece or piece.endswith(b"\n"):
                    break
        while position < size:
            line = stream.readline(min(line_bytes + 1, size - position))
            if not line:
                break
            position += len(line)
            budget["left"] -= len(line)
            if len(line) > line_bytes or (not line.endswith(b"\n") and position < size):
                budget["long"] += 1
                while position < size and not line.endswith(b"\n"):  # the rest of an overlong line
                    line = stream.readline(min(line_bytes, size - position))
                    if not line:
                        break
                    position += len(line)
                    budget["left"] -= len(line)
                continue
            budget["read_bytes"] += len(line)
            budget["lines"] += 1
            yield line


def iter_records(path, budget=None, tail=None):
    """The JSON objects of the newest lines of PATH (iter_lines), one at a time. Damaged lines are skipped (the
    agent cannot write these files, but a crash can cut a line)."""
    for line in iter_lines(path, budget, tail):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            yield record


def skipped_lines(budget):
    """About how many older lines were not read: the bytes left out at the size of the lines read."""
    if not budget["skipped_bytes"]:
        return budget["long"]
    if not budget["lines"]:
        return budget["long"] + 1
    return budget["long"] + max(1, budget["skipped_bytes"] * budget["lines"] // max(1, budget["read_bytes"]))


def newest_first(paths):
    return sorted(paths, key=lambda path: path.name, reverse=True)  # connections-YYYYmmdd-HHMMSS-…: by time


def read_records(path, limit=200_000):
    """The newest LIMIT JSON objects of a JSONL file (of its newest TAIL_BYTES), and about how many older lines
    were left out."""
    budget = new_budget()
    records = deque(iter_records(path, budget), maxlen=limit)
    dropped = max(0, budget["lines"] - len(records))
    return list(records), dropped + (skipped_lines(budget) if budget["skipped_bytes"] or budget["long"] else 0)


def safe(value):
    """Text from the logs for the terminal: host names and paths are chosen by the agent, so every
    non-printable character, newlines and tabs included, is shown escaped."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(value))


def valid_host(host):
    return (len(host) <= 253 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) is not None
            and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.split(".")))


def preset_for(host):
    for name, hosts in launcher().PRESETS.items():
        if host in hosts:
            return name
    return None


def suggestion(record):
    """(key, option, why) for one denied record, or None."""
    host = record.get("host")
    if isinstance(host, str) and host:
        if not valid_host(host):
            # Never a ready-to-paste option for a name that is not a host name: it may carry shell syntax.
            return f"invalid {host}", None, "not a valid host name; looked up by the agent, do not allow"
        preset = preset_for(host)
        if preset:
            return f"--allow-{preset}", f"--allow-{preset}", f"{host} belongs to that preset"
        if host in launcher().FRONTIER:
            return "--frontier", "--profile frontier", f"{host} is an OpenAI/Anthropic host"
        return f"--allow-host {host}", f"--allow-host {host}", "name lookup was refused (allows TCP/443)"
    address, protocol, port = record.get("address"), record.get("protocol"), record.get("port")
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return None
    target = f"[{ip}]" if ip.version == 6 else str(ip)
    if ip.is_loopback and protocol == "tcp" and isinstance(port, int):
        return f"--allow-host-port {port}", f"--allow-host-port {port}", "a service on this host (Podman)"
    if protocol in ("tcp",) and isinstance(port, int):
        return f"--allow-tcp {target}:{port}", f"--allow-tcp {target}:{port}", "one TCP port on that address"
    return f"--allow {ip}", f"--allow {ip}", f"{protocol or 'traffic'} to that address (all ports and protocols)"


def by(record, pid=False):
    """" by NAME" (with PID: " by NAME (pid N)") for a record that names the process behind it, else ""."""
    process = record.get("process")
    if not isinstance(process, dict):
        return ""
    if "unknown" in process:  # with why (most often: the socket was closed before it was looked up)
        why = str(process.get("unknown") or "")[:80]
        return " by a process not identified" + (f" ({why})" if why else "")
    number = process.get("ns_pid") or process.get("pid")
    return f" by {process.get('comm', '?')}" + (f" (pid {number})" if pid and number else "")


def refusals(directory, limit=20):
    """The newest LIMIT refusals of the logs in DIRECTORY with what is known of them: when, what, why, the
    process (name, PID, program, command line, working directory, its parents) and the sandbox, and what the
    client sent (server name, ALPN, request line, Host, DNS name)."""
    found, budget = [], new_budget()
    # The firewall's and the DNS stub's refusals first: the connection logs of many runs would use up the budget.
    paths = [Path(directory) / "egress-denied.jsonl", *newest_first(Path(directory).glob("connections-*.jsonl"))]
    for path in paths:
        connections = path.name.startswith("connections-")
        newest = deque(maxlen=limit)  # (a file is written in time order: its last LIMIT refusals)
        for record in iter_records(path, budget, tail=TAIL_BYTES if connections else TAIL_BYTES // 4):
            if connections and (record.get("event") != "denied" or record.get("abandoned")):
                continue
            newest.append(record)
        for record in newest:
            process = record.get("process") if isinstance(record.get("process"), dict) else {}
            sandbox = record.get("sandbox") if isinstance(record.get("sandbox"), dict) else {}
            target = record.get("url") or (f"DNS {record['host']}" if not connections and record.get("host")
                                           else None) or \
                " ".join(str(record[k]) for k in ("protocol", "address", "port", "destination") if record.get(k))
            found.append({
                "time": record.get("time", ""), "what": f"{record.get('method', '')} {target}".strip(),
                "reason": record.get("reason") or ("dropped by the firewall" if not connections else ""),
                "rule": record.get("rule"), "process": process, "sandbox": sandbox,
                "client": {key: record[key] for key in ("sni", "alpn", "http_host", "request_line", "state")
                           if record.get(key)}})
    return sorted(found, key=lambda entry: str(entry["time"]))[-limit:]


def summarize(directory):
    budget = new_budget()
    targets = Counter()
    suggestions = {}
    denied = 0
    for record in iter_records(Path(directory) / "egress-denied.jsonl", budget):
        denied += 1
        if "host" in record:
            targets[f"DNS {record['host']}{by(record)}"] += 1
        else:
            targets[f"{record.get('protocol', '?')} {record.get('address', '?')}:{record.get('port', '-')}{by(record)}"] += 1
        found = suggestion(record)
        if found:
            key, option, why = found
            entry = suggestions.setdefault(key, {"option": option, "why": why, "count": 0})
            entry["count"] += 1
    actions, directories, errors, file_events = Counter(), Counter(), set(), 0
    for record in iter_records(Path(directory) / "files.jsonl", budget):
        if record.get("action"):
            file_events += 1
            actions[record["action"]] += 1
            if isinstance(record.get("path"), str):
                directories[str(Path(record["path"]).parent)] += 1
        if isinstance(record.get("error"), str) and len(errors) < 100:
            errors.add(record["error"])
    return {"directory": str(directory), "denied": denied, "targets": targets.most_common(),
            "suggestions": sorted(suggestions.values(), key=lambda s: -s["count"]),
            "older_lines_skipped": skipped_lines(budget) if budget["skipped_bytes"] or budget["long"] else 0,
            "file_events": file_events, "actions": dict(actions),
            "busiest_directories": directories.most_common(10), "file_errors": sorted(errors)}


def telemetry(directory):
    """What the connection logs and processes.jsonl of DIRECTORY say: per run and in total (the newest
    TOTAL_BYTES of them; older_lines_skipped says about how much was left out)."""
    budget = new_budget()
    models, hosts, credentials = Counter(), Counter(), Counter()
    tokens = Counter()
    pushes = {"ok": 0, "refused": 0}
    refusals = Counter()
    for path in newest_first(Path(directory).glob("connections-*.jsonl")):
        for record in iter_records(path, budget):
            event = record.get("event")
            host = str(record.get("host") or record.get("destination") or "?")
            if event == "http":
                hosts[host] += 1
            model = record.get("model")
            if isinstance(model, dict) and isinstance(model.get("usage"), dict):
                models[str(model.get("model") or "?")] += 1
                for key, value in model["usage"].items():
                    if isinstance(value, int):
                        tokens[key] += value
            for name in record.get("credentials") or []:
                credentials[f"{name} -> {host}"] += 1
            push = record.get("push")
            if isinstance(push, dict):
                pushes["ok" if push.get("ok") else "refused"] += 1
            if event in ("denied", "tls-refused", "tls-upstream-failed") and not record.get("abandoned"):
                refusals[f"{event} {record.get('destination') or host}"
                         + (f" rule {record['rule']}" if record.get("rule") else "") + by(record)] += 1
    commands, processes = Counter(), 0
    budget["left"] = max(budget["left"], TAIL_BYTES)  # processes.jsonl: always its newest part
    for record in iter_records(Path(directory) / "processes.jsonl", budget):
        processes += 1
        commands[(str(record.get("command", "")).split() or ["?"])[0].rsplit("/", 1)[-1]] += 1
    return {"requests": sum(hosts.values()), "hosts": hosts.most_common(20), "model_calls": dict(models),
            "tokens": dict(tokens), "pushes": pushes, "credentials": credentials.most_common(20),
            "refused": refusals.most_common(20), "processes": processes, "programs": commands.most_common(20),
            "older_lines_skipped": skipped_lines(budget) if budget["skipped_bytes"] or budget["long"] else 0}


def tool_calls(state_root=None):
    """{server: [(time, tool, decision)]} of the MCP servers' tool calls: mcp-calls.jsonl, and the
    `sandbox-ai-mcp-call` lines older versions wrote into supervisor.log."""
    root = Path(state_root or os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "mcp"
    calls = {}
    try:
        servers = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return calls
    for server in servers:
        for name, prefix in (("supervisor.log", "sandbox-ai-mcp-call "), ("mcp-calls.jsonl", "")):
            try:
                with open(server / name, "rb") as stream:  # the newest 4 MiB: the logs are not rotated
                    stream.seek(max(0, stream.seek(0, os.SEEK_END) - (4 << 20)))
                    lines = stream.read().decode("utf-8", "replace").splitlines()[-5000:]
            except OSError:
                continue
            for line in lines:
                if not line.startswith(prefix):
                    continue
                try:
                    record = json.loads(line[len(prefix):])
                except (ValueError, RecursionError):
                    continue
                if not isinstance(record, dict) or record.get("event", "call") != "call":
                    continue  # the server's own stderr shares supervisor.log; results and refusals are not calls
                calls.setdefault(server.name, []).append((str(record.get("time", ""))[:40],
                                                          str(record.get("tool", ""))[:200],
                                                          str(record.get("decision", "allow"))[:10]))
    return calls


def report(summary, out=print):
    p = safe
    out(f"Logs in {p(summary['directory'])}")
    if summary["older_lines_skipped"]:
        out(f"(only the newest entries; about {summary['older_lines_skipped']} older lines not read)")
    out("")
    if summary["denied"]:
        out(f"Blocked egress: {summary['denied']} event(s)")
        for target, count in summary["targets"][:20]:
            out(f"  {count:5}  {p(target)}")
        out("")
        out("To allow them (each widens the sandbox; review first):")
        for entry in summary["suggestions"]:
            option = p(entry["option"]) if entry["option"] else "(none)"
            out(f"  {option:40}  {p(entry['why'])} ({entry['count']}x)")
    else:
        out("Blocked egress: none")
    out("")
    if summary["file_events"]:
        actions = ", ".join(f"{count} {action}" for action, count in sorted(summary["actions"].items()))
        out(f"File access: {actions}")
        for directory, count in summary["busiest_directories"]:
            out(f"  {count:5}  {p(directory)}")
    else:
        out("File access: none recorded")
    for error in summary["file_errors"]:
        out(f"  note: {p(error)}")
    seen = summary.get("telemetry") or {}
    if seen.get("requests") or seen.get("processes"):
        out("")
        out(f"Connections: {seen['requests']} HTTP request(s); pushes {seen['pushes']['ok']} checked, "
            f"{seen['pushes']['refused']} refused")
        for host, count in seen["hosts"][:10]:
            out(f"  {count:5}  {p(host)}")
        if seen["model_calls"]:
            calls = ", ".join(f"{count}x {p(model)}" for model, count in seen["model_calls"].items())
            usage = ", ".join(f"{p(key)} {value}" for key, value in sorted(seen["tokens"].items()))
            out(f"Model calls: {calls}; tokens: {usage}")
        for entry, count in seen["credentials"][:10]:
            out(f"  credential {p(entry)} ({count}x)")
        for entry, count in seen["refused"][:10]:
            out(f"  refused {p(entry)} ({count}x)")
        out(f"Processes started: {seen['processes']}" + (": " + ", ".join(
            f"{p(name)} {count}" for name, count in seen["programs"][:10]) if seen["programs"] else ""))
    shown_refusals = summary.get("refusals") or []
    if shown_refusals:
        out("")
        out(f"Refused, newest last ({len(shown_refusals)}):")
    for entry in shown_refusals:
        process, sandbox, client = entry["process"], entry["sandbox"], entry["client"]
        out(f"  {p(entry['time'])}  {p(entry['what'])[:200]}" + (f"  [rule {p(entry['rule'])}]" if entry["rule"] else ""))
        if entry["reason"]:
            out(f"      why:     {p(entry['reason'])[:300]}")
        if "unknown" in process:
            out(f"      process: not identified ({p(process['unknown'])})")
        elif process:
            out(f"      process: {p(process.get('comm', '?'))} (pid {p(process.get('ns_pid') or process.get('pid'))}, "
                f"uid {p(process.get('uid', '?'))}) {p(process.get('exe') or '')}")
            out(f"      command: {p(' '.join(str(a) for a in process.get('argv') or []))[:300]}")
            if process.get("cwd") or process.get("parents"):
                out(f"      in:      {p(process.get('cwd') or '?')}, started by "
                    f"{p(' < '.join(str(c) for c in process.get('parents') or []) or '?')}")
        if sandbox:
            out(f"      sandbox: " + ", ".join(f"{key} {p(value)}" for key, value in sandbox.items()))
        if client:
            out(f"      sent:    " + ", ".join(f"{key} {p(value)}" for key, value in client.items())[:300])
    for server, calls in (summary.get("tool_calls") or {}).items():
        refused = sum(1 for call in calls if call[2:] and call[2] != "allow")
        out(f"MCP {p(server)}: {len(calls)} tool call(s)" + (f", {refused} not allowed outright" if refused else "")
            + ": " + ", ".join(sorted({p(str(call[1])) for call in calls})[:10]))
    return 0


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-ai logs", description="Summarize a sandbox log directory.")
    parser.add_argument("directory", nargs="?", type=Path)
    parser.add_argument("-p", "--project", type=Path, help="Project whose wizard log directory to read")
    parser.add_argument("-j", "--json", action="store_true")
    a = parser.parse_args(argv)
    directory = a.directory or default_log_dir(Path(a.project or os.getcwd()).expanduser().absolute())
    if not directory.is_dir():
        print(f"sandbox-ai logs: no log directory {directory}; start the agent with --log-dir "
              "(the wizard's logging question) first", file=sys.stderr)
        return 1
    summary = summarize(directory)
    summary["telemetry"] = telemetry(directory)
    summary["tool_calls"] = tool_calls()
    summary["refusals"] = refusals(directory)
    if a.json:
        print(json.dumps(summary, indent=2))
        return 0
    return report(summary)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
