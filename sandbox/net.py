#!/usr/bin/env python3
"""sandbox-ai net: change what a running sandbox may reach, without restarting it.

  sandbox-ai net list                                  running sandboxes
  sandbox-ai net show [RUN]                            what was allowed during the run
  sandbox-ai net allow [RUN] [--allow-host HOST] [--allow-tcp IP:PORT] [--allow IP_OR_CIDR] [--allow-PRESET]
  sandbox-ai net revoke [RUN] (the same options)       take back what `allow` gave

RUN is the name a sandbox shows to its agent as $SANDBOX_AI_RUN (see `list`); it may be left out
when only one sandbox runs, or only one for the current directory. Changes last until the sandbox
ends; to keep them, add the same options to its start command or its .sandbox-ai.json. Only you can
make them: the agent cannot reach the control socket. Connections that are already open stay open.

Standard library only.
"""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys

HERE = Path(__file__).resolve().parent
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_net", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


def running():
    """{run: record} of the sandboxes registered and still running; stale entries are removed."""
    runs = {}
    directory = launcher().runs_dir()
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return runs
    for entry in entries:
        try:
            record = json.loads((entry / "run.json").read_text())
        except (OSError, ValueError):
            continue
        if not alive(record.get("pid")):
            shutil.rmtree(entry, ignore_errors=True)
            continue
        record["directory"] = str(entry)
        runs[record.get("run", entry.name)] = record
    return runs


def choose(runs, name, cwd=None):
    """The record of run NAME, or of the only one (for CWD) when NAME is None."""
    if name is not None:
        if name not in runs:
            raise ValueError(f"no running sandbox {name!r}; running: {', '.join(runs) or 'none'}")
        return runs[name]
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
    hosts = list(dict.fromkeys(args.allow_host + [h for preset in args.presets for h in l.PRESETS[preset]]))
    for host in hosts:
        if len(host) > 253 or not all(part and len(part) <= 63 and part.replace("-", "").isalnum()
                                      and not part.startswith("-") and not part.endswith("-")
                                      for part in host.split(".")):
            raise ValueError(f"invalid host name: {host!r}")
    for value in args.allow:
        if "%" in value:
            raise ValueError(f"--allow does not accept an IPv6 scope ID: {value!r}")
        ipaddress.ip_network(value, strict=False)
    tcp = l.tcp_targets(argparse.Namespace(allow_tcp=args.allow_tcp))
    names = resolver(hosts) if op == "allow" else {}
    https = sorted({address for addresses in names.values() for address in addresses}
                   | set(l.host_networks(hosts)))
    request = {"op": op, "nets": list(args.allow), "https": https, "tcp": [[a, p] for a, p in tcp]}
    return request, names, hosts


def ask(directory, request, timeout=15):
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
    REMOVE; the file is changed in place, so the sandbox sees it at once."""
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


def show(result, out=print):
    entries = ([f"{net} (all ports)" for net in result.get("nets", [])]
               + [f"{net} (TCP/443)" for net in result.get("https", [])]
               + [f"{address}:{port} (TCP)" for address, port in result.get("tcp", [])])
    out("allowed during this run: " + (", ".join(entries) if entries else "nothing"))


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai net", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n".join(__doc__.split("\n\n")[1:3]))
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="running sandboxes")
    shown = commands.add_parser("show", help="what was allowed during the run")
    shown.add_argument("run", nargs="?")
    for name in ("allow", "revoke"):
        command = commands.add_parser(name, help=f"{name} destinations while the sandbox runs")
        command.add_argument("run", nargs="?")
        command.add_argument("-A", "--allow-host", action="append", default=[], metavar="HOST",
                             help="pin HOST in the sandbox's /etc/hosts and allow TCP/443 to it")
        command.add_argument("-t", "--allow-tcp", action="append", default=[], metavar="IP:PORT")
        command.add_argument("-a", "--allow", action="append", default=[], metavar="IP_OR_CIDR",
                             help="every protocol and port")
        for preset, hosts in launcher().PRESETS.items():
            short = [launcher().SHORT_OPTIONS[f"--allow-{preset}"]] if f"--allow-{preset}" in launcher().SHORT_OPTIONS else []
            command.add_argument(*short, f"--allow-{preset}", action="append_const", const=preset, dest="presets",
                                 default=[], help=f"{', '.join(hosts)} (TCP/443)")
    return p


def main(argv, out=print, resolver=resolve):
    args = parser().parse_args(argv)
    try:
        runs = running()
        if args.command == "list":
            if not runs:
                out("no sandbox is running")
            for name, record in runs.items():
                session = f", session {record['session']}" if record.get("session") else ""
                out(f"{name}: {record.get('agent')} in {record.get('workspace')} (since {record.get('started')}"
                    f"{session})")
            return 0
        record = choose(runs, args.run)
        if args.command == "show":
            show(ask(record["directory"], {"op": "show"}), out)
            return 0
        request, names, hosts = request_for(args, args.command, resolver)
        if not (request["nets"] or request["https"] or request["tcp"] or hosts):
            raise ValueError(f"nothing to {args.command}: give --allow-host, --allow-tcp, --allow or a preset")
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
        result = ask(record["directory"], request)
        if args.command == "allow":
            update_hosts(record, names)
        elif hosts:
            update_hosts(record, {}, remove=hosts)
        show(result, out)
        if args.command == "allow":
            options = (" ".join(f"--allow-host {h}" for h in args.allow_host)
                       + "".join(f" --allow-{p}" for p in args.presets)
                       + "".join(f" --allow-tcp {v}" for v in args.allow_tcp)
                       + "".join(f" --allow {v}" for v in args.allow)).strip()
            out(f"{record['run']}: allowed until the sandbox ends; to keep it, start with: {options}")
        return 0
    except ValueError as exc:
        print(f"sandbox-ai net: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
