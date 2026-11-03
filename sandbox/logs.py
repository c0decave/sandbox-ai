#!/usr/bin/env python3
"""sandbox-ai logs: summarize egress-denied.jsonl and files.jsonl of a --log-dir and suggest options.

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
    if a.json:
        print(json.dumps(summary, indent=2))
        return 0
    return report(summary)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
