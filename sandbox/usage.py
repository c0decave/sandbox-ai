#!/usr/bin/env python3
"""sandbox-ai usage: what a running sandbox uses now - CPU, memory, processes - and its top processes.

  sandbox-ai usage [RUN] [--json] [--follow] [--interval S] [--top N]   one sandbox (RUN as for sandbox-ai net)
  sandbox-ai usage --all [--json]                                        every running sandbox

CPU: the CPUs used over the interval (cgroup cpu.stat), as a share of --cpus (or of the host's CPUs without a
limit), and how often --cpus held the sandbox back (throttled periods). Memory: used and the limit, and what the
files in its /tmp and HOME take (they count as memory). Processes: processes and threads against --pids. The top
processes by CPU and resident memory come from /proc. Where this host cannot read the sandbox's cgroup (a remote
engine, macOS) the launcher's last record (the session directory's resources.json) is shown instead.

Change the limits while it runs: sandbox-ai limits RUN --memory SIZE --cpus N --pids N (a strict security mode
keeps its caps; --json names them under "caps").

Standard library only.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
INTERVAL, TOP = 1.0, 8
_modules, _modules_lock = {}, threading.RLock()


def module(name):
    """sandbox-ai's module NAME, loaded once."""
    if name not in _modules:
        with _modules_lock:
            if name not in _modules:
                spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_usage", HERE / f"{name}.py")
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _modules[name] = loaded
    return _modules[name]


def launcher():
    return module("agent_sandbox")


def gib(value):
    return f"{(value or 0) / (1 << 30):.1f} GiB"


def caps(record):
    """{"mode", memory, cpus, pids: the security mode's cap (text) or None} of the run RECORD (run.json "mode")."""
    mode = record.get("mode") if isinstance(record.get("mode"), dict) else {}
    modes = module("modes")
    name = mode.get("name")
    if name not in getattr(modes, "MODES", {}):
        return {"mode": name if isinstance(name, str) else None, "memory": None, "cpus": None, "pids": None}
    return {"mode": name, **{key: modes.limit_ceiling(name, key) for key in ("memory", "cpus", "pids")}}


def limited_cgroup(path, root=CGROUP_ROOT):
    """The cgroup directory of PATH ("/user.slice/...") that carries the limits: PATH or one above it, never above the
    sandbox's own top cgroup (the user's slice holds the desktop session), else PATH itself; None if it is not
    there."""
    if not path:
        return None
    directory = Path(root) / str(path).lstrip("/")
    if not directory.is_dir():
        return None
    l = launcher()
    top = l.MemoryWatch.sandbox_top(directory)
    for candidate in (directory, *directory.parents):
        if l.cgroup_limited(candidate):
            return candidate
        if candidate == top or candidate == Path(root):
            break
    return directory


def process_times(pids, proc=PROC):
    """{pid: (name, utime+stime in clock ticks, resident bytes, threads)} of PIDS that still exist."""
    found = {}
    for pid in pids:
        try:
            stat = (Path(proc) / str(pid) / "stat").read_text()
            status = (Path(proc) / str(pid) / "status").read_text()
        except OSError:
            continue
        # The name is in parentheses and may hold spaces or parentheses itself: the fields follow the last ")".
        name, rest = stat[stat.find("(") + 1:stat.rfind(")")], stat[stat.rfind(")") + 2:].split()
        try:
            ticks = int(rest[11]) + int(rest[12])
        except (IndexError, ValueError):
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        try:
            rss = int(fields.get("VmRSS", "0 kB").split()[0]) * 1024 if fields.get("VmRSS", "").strip() else 0
            threads = int(fields.get("Threads", "1").strip() or 1)
        except (IndexError, ValueError):
            rss, threads = 0, 1
        found[pid] = (name, ticks, rss, threads)
    return found


def look(record, finder=None, root=CGROUP_ROOT, proc=PROC):
    """(cgroup directory or None, pids) of the running sandbox RECORD: the cgroup its limits are on (Podman: the
    agent container's own), its processes those of the whole sandbox (the pod)."""
    finder = finder or (lambda r: module("devices").sandbox_processes(r, proc=proc))
    found = finder(record) or {}
    return limited_cgroup(found.get("limits") or found.get("cgroup"), root), list(found.get("pids") or [])


def first(record, finder=None, root=CGROUP_ROOT, proc=PROC, clock=time.monotonic):
    """The first of two looks one interval apart: what the second needs to tell the CPU used since."""
    cgroup, pids = look(record, finder, root, proc)
    l = launcher()
    if cgroup is not None:
        pids = sorted(set(pids) | set(l.cgroup_processes(cgroup)))
    return {"cgroup": cgroup, "pids": pids, "time": clock(),
            "cpu": l.cpu_state(cgroup) if cgroup is not None else None, "processes": process_times(pids, proc)}


def shared_record(record):
    """The launcher's last record of the run (the session directory's resources.json), or None."""
    try:
        data = json.loads((Path(record.get("session_dir") or "") / "resources.json").read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def second(record, before, proc=PROC, clock=time.monotonic, top=TOP, host_cpus=None):
    """What RECORD uses: {"run", "name", "source", "cpu", "memory", "pids", "top", "caps", ...} from the look
    BEFORE and this one; source "cgroup" (measured here), "watch" (the launcher's resources.json) or None."""
    l = launcher()
    host_cpus = host_cpus or os.cpu_count() or 1
    result = {"run": record.get("run"), "name": record.get("name"), "agent": record.get("agent"),
              "backend": record.get("backend"), "workspace": record.get("workspace"), "caps": caps(record),
              "source": None, "cpu": None, "memory": None, "pids": None, "top": [], "warnings": []}
    cgroup = before.get("cgroup")
    if cgroup is not None:
        now = clock()
        try:
            memory = l.memory_state(cgroup)
        except (OSError, ValueError):
            memory = None
        if memory is not None:
            result["source"] = "cgroup"
            result["cgroup"] = str(cgroup)
            result["memory"] = {"used_bytes": memory["current"], "limit_bytes": memory["limit"] or None,
                                "percent": round(100 * memory["current"] / memory["limit"], 1) if memory["limit"]
                                else None, "tmpfs_bytes": memory["shmem"], "oom_kills": memory["oom_kill"]}
            cpu = l.cpu_usage(before.get("cpu"), l.cpu_state(cgroup), now - before["time"], host_cpus)
            if cpu is not None:
                result["cpu"] = {"used_cpus": cpu["cpus"], "limit_cpus": cpu["limit"] or None,
                                 "host_cpus": host_cpus, "percent": cpu["percent"],
                                 "throttled_percent": cpu["throttled_percent"],
                                 "throttled_seconds": round(cpu["throttled_usec"] / 1e6, 1)}
            pids = l.pids_state(cgroup)
            if pids is not None:
                result["pids"] = {"current": pids["current"], "limit": pids["limit"] or None,
                                  "percent": round(100 * pids["current"] / pids["limit"], 1) if pids["limit"]
                                  else None}
            after = process_times(before["pids"], proc)
            seconds = max(now - before["time"], 1e-6)
            tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            rows = []
            for pid, (name, ticks, rss, threads) in after.items():
                old = before["processes"].get(pid)
                used = max(0, ticks - old[1]) / tick / seconds if old and old[0] == name else 0.0
                rows.append({"pid": pid, "name": l.printable(name), "cpu_percent": round(100 * used, 1),
                             "rss_bytes": rss, "threads": threads})
            rows.sort(key=lambda row: (-row["cpu_percent"], -row["rss_bytes"], row["pid"]))
            result["top"] = rows[:top]
            result["processes"] = len(rows)
    if result["source"] is None:
        shared = shared_record(record)
        if shared is not None:
            result["source"] = "watch"
            result["updated"] = l.printable(str(shared.get("updated") or "?"))[:40]
            for key in ("memory", "cpu", "pids"):
                if isinstance(shared.get(key), dict):
                    result[key] = {name: value for name, value in shared[key].items()
                                   if isinstance(value, (int, float)) and not isinstance(value, bool)}
    memory, cpu, pids = result["memory"] or {}, result["cpu"] or {}, result["pids"] or {}
    if (memory.get("percent") or 0) >= 100 * l.MEMORY_WARN:
        result["warnings"].append("memory")
    if (cpu.get("throttled_percent") or 0) >= 100 * l.CPU_THROTTLE_RATIO and cpu.get("limit_cpus"):
        result["warnings"].append("cpu")
    if (pids.get("percent") or 0) >= 100 * l.PIDS_WARN:
        result["warnings"].append("pids")
    return result


def measure(records, interval=INTERVAL, finder=None, root=CGROUP_ROOT, proc=PROC, sleep=time.sleep,
            clock=time.monotonic, top=TOP, host_cpus=None):
    """[usage] of RECORDS, measured together over one INTERVAL."""
    befores = [first(record, finder, root, proc, clock) for record in records]
    if any(before["cgroup"] is not None for before in befores):
        sleep(interval)
    return [second(record, before, proc, clock, top, host_cpus) for record, before in zip(records, befores)]


def percent(value):
    return "-" if value is None else f"{value:g} %"


def lines(entry):
    """The text of one usage entry."""
    l, out = launcher(), []
    p = l.printable
    title = p(entry.get("name") or entry.get("run") or "?")
    if entry.get("name") and entry.get("run") and entry["name"] != entry["run"]:
        title += f" ({p(entry['run'])})"
    out.append(title)
    if entry["source"] is None:
        out.append("  no usage known: this host cannot read the sandbox's cgroup and the launcher has not recorded "
                   "any (it records only where it sees the cgroup: a local engine on Linux, with limits)")
        return out
    if entry["source"] == "watch":
        out.append(f"  (the launcher's last record, {p(str(entry.get('updated') or '?'))})")
    cpu, memory, pids = entry["cpu"] or {}, entry["memory"] or {}, entry["pids"] or {}
    if cpu:
        of = f"of {cpu['limit_cpus']:g} CPUs" if cpu.get("limit_cpus") else \
            f"no limit, {cpu.get('host_cpus')} on the host"
        held = f", held back in {cpu['throttled_percent']:g} % of the periods" if cpu.get("throttled_percent") else ""
        out.append(f"  CPU        {cpu.get('used_cpus') or 0:g} ({of}; {percent(cpu.get('percent'))}){held}")
    if memory:
        of = f"of {gib(memory['limit_bytes'])} ({percent(memory.get('percent'))})" if memory.get("limit_bytes") \
            else "(no limit)"
        out.append(f"  memory     {gib(memory.get('used_bytes'))} {of}, files in /tmp and HOME "
                   f"{gib(memory.get('tmpfs_bytes'))}")
    if pids:
        of = f"of {pids['limit']}" if pids.get("limit") else "(no limit)"
        out.append(f"  processes  {pids.get('current')} {of} (processes and threads)")
    for warning in entry["warnings"]:
        out.append("  warning: " + {"memory": "memory is nearly full",
                                    "cpu": "the CPU limit holds it back (it runs slower)",
                                    "pids": "processes and threads are nearly at the limit"}[warning])
    if entry["top"]:
        out.append("  top processes:")
        out.append(f"    {'PID':>8}  {'CPU':>7}  {'memory':>9}  {'threads':>7}  name")
        for row in entry["top"]:
            out.append(f"    {row['pid']:>8}  {row['cpu_percent']:>5g} %  {gib(row['rss_bytes']):>9}  "
                       f"{row['threads']:>7}  {row['name']}")
    limits = entry["caps"]
    capped = [f"{key} {limits[key]}" for key in ("memory", "cpus", "pids") if limits.get(key)]
    note = f" (security mode {p(limits['mode'])}: at most {', '.join(capped)})" if capped else ""
    out.append(f"  change: sandbox-ai limits {p(entry.get('run') or '')} --memory SIZE --cpus N --pids N{note}")
    return out


def main(argv, out=print, sleep=time.sleep):
    parser = argparse.ArgumentParser(prog="sandbox-ai usage", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    parser.add_argument("run", nargs="?", help="the run ($SANDBOX_AI_RUN in the sandbox) or the name of its start")
    parser.add_argument("-a", "--all", action="store_true", help="every running sandbox")
    parser.add_argument("-j", "--json", action="store_true", help="as JSON (with --follow: one line per look)")
    parser.add_argument("-f", "--follow", action="store_true", help="again every interval until Ctrl-C")
    parser.add_argument("-i", "--interval", type=float, default=INTERVAL, metavar="S",
                        help=f"seconds the CPU is measured over (default {INTERVAL:g})")
    parser.add_argument("-t", "--top", type=int, default=TOP, metavar="N", help=f"top processes (default {TOP})")
    args = parser.parse_args(argv)
    if not 0.1 <= args.interval <= 3600:
        parser.error("--interval expects 0.1 to 3600 seconds")
    if args.all and args.run:
        parser.error("--all goes without RUN")
    net = module("net")
    try:
        runs = net.running()
        if args.all:
            records = [record for record in runs.values() if record.get("kind", "agent") == "agent"]
        else:
            records = [net.choose(runs, args.run, command="usage")]
    except ValueError as exc:
        print(f"sandbox-ai usage: {launcher().printable(exc)}", file=sys.stderr)
        return 1
    try:
        while True:
            entries = measure(records, args.interval, sleep=sleep, top=max(0, min(args.top, 1000)))
            if args.json:
                data = entries if args.all else entries[0]
                out(json.dumps(data) if args.follow else json.dumps(data, indent=2))
            else:
                text = [line for entry in entries for line in lines(entry) + [""]][:-1]
                out("\n".join(text) if text else "no sandbox is running")
                if args.follow:
                    out("")
            if not args.follow:
                return 0
            if not any(entry["source"] == "cgroup" for entry in entries):
                sleep(args.interval)  # nothing measured over the interval: wait it here
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
