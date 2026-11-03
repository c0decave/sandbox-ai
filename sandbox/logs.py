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
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_logs", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


def read_records(path, limit=200_000):
    """The newest LIMIT JSON objects of a JSONL file, and how many older lines were left out.
    Damaged lines are skipped (the agent cannot write these files, but a crash can cut a line)."""
    lines, total = deque(maxlen=limit), 0
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                lines.append(line)
                total += 1
    except FileNotFoundError:
        pass
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, total - len(lines)


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


def summarize(directory):
    denied, denied_skipped = read_records(Path(directory) / "egress-denied.jsonl")
    files, files_skipped = read_records(Path(directory) / "files.jsonl")
    targets = Counter()
    suggestions = {}
    for record in denied:
        if "host" in record:
            targets[f"DNS {record['host']}"] += 1
        else:
            targets[f"{record.get('protocol', '?')} {record.get('address', '?')}:{record.get('port', '-')}"] += 1
        found = suggestion(record)
        if found:
            key, option, why = found
            entry = suggestions.setdefault(key, {"option": option, "why": why, "count": 0})
            entry["count"] += 1
    actions = Counter(record.get("action") for record in files if record.get("action"))
    directories = Counter(str(Path(record["path"]).parent) for record in files
                          if isinstance(record.get("path"), str) and record.get("action"))
    errors = sorted({record["error"] for record in files if isinstance(record.get("error"), str)})
    return {"directory": str(directory), "denied": len(denied), "targets": targets.most_common(),
            "suggestions": sorted(suggestions.values(), key=lambda s: -s["count"]),
            "older_lines_skipped": denied_skipped + files_skipped,
            "file_events": sum(1 for record in files if record.get("action")), "actions": dict(actions),
            "busiest_directories": directories.most_common(10), "file_errors": errors}


def telemetry(directory):
    """What the connection logs and processes.jsonl of DIRECTORY say: per run and in total."""
    connections, skipped = [], 0
    for path in sorted(Path(directory).glob("connections-*.jsonl")):
        records, older = read_records(path)
        connections += records
        skipped += older
    processes, older = read_records(Path(directory) / "processes.jsonl")
    skipped += older
    models, hosts, credentials = Counter(), Counter(), Counter()
    tokens = Counter()
    pushes = {"ok": 0, "refused": 0}
    refusals = Counter()
    for record in connections:
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
        if event in ("denied", "tls-refused", "tls-upstream-failed"):
            refusals[f"{event} {record.get('destination') or host}"] += 1
    commands = Counter((str(r.get("command", "")).split() or ["?"])[0].rsplit("/", 1)[-1] for r in processes)
    return {"requests": sum(hosts.values()), "hosts": hosts.most_common(20), "model_calls": dict(models),
            "tokens": dict(tokens), "pushes": pushes, "credentials": credentials.most_common(20),
            "refused": refusals.most_common(20), "processes": len(processes), "programs": commands.most_common(20),
            "older_lines_skipped": skipped}


def tool_calls(state_root=None):
    """{server: [(time, tool)]} of the MCP servers' supervisor logs."""
    root = Path(state_root or os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "mcp"
    calls = {}
    try:
        servers = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return calls
    for server in servers:
        try:
            with open(server / "supervisor.log", "rb") as stream:  # the newest 4 MiB: the log is not rotated
                stream.seek(max(0, stream.seek(0, os.SEEK_END) - (4 << 20)))
                lines = stream.read().decode("utf-8", "replace").splitlines()[-5000:]
        except OSError:
            continue
        for line in lines:
            if line.startswith("sandbox-ai-mcp-call "):
                try:
                    record = json.loads(line.split(" ", 1)[1])
                except (ValueError, RecursionError):
                    continue
                if not isinstance(record, dict):  # the server's own stderr shares this log
                    continue
                calls.setdefault(server.name, []).append((str(record.get("time", ""))[:40],
                                                          str(record.get("tool", ""))[:200]))
    return calls


def report(summary, out=print):
    p = safe
    out(f"Logs in {p(summary['directory'])}")
    if summary["older_lines_skipped"]:
        out(f"(only the newest entries; {summary['older_lines_skipped']} older lines not read)")
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
    for server, calls in (summary.get("tool_calls") or {}).items():
        out(f"MCP {p(server)}: {len(calls)} tool call(s): " + ", ".join(sorted({p(str(t)) for _, t in calls})[:10]))
    return 0


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-ai logs", description="Summarize a sandbox log directory.")
    parser.add_argument("directory", nargs="?", type=Path)
    parser.add_argument("-p", "--project", type=Path, help="Project whose wizard log directory to read")
    parser.add_argument("-j", "--json", action="store_true")
    a = parser.parse_args(argv)
    directory = a.directory or launcher().default_log_dir(Path(a.project or os.getcwd()).expanduser().absolute())
    if not directory.is_dir():
        print(f"sandbox-ai logs: no log directory {directory}; start the agent with --log-dir "
              "(the wizard's logging question) first", file=sys.stderr)
        return 1
    summary = summarize(directory)
    summary["telemetry"] = telemetry(directory)
    summary["tool_calls"] = tool_calls()
    if a.json:
        print(json.dumps(summary, indent=2))
        return 0
    return report(summary)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
