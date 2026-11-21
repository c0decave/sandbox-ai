#!/usr/bin/env python3
"""sandbox-ai's system monitor: what each run uses over time - CPU, memory, disk IO, network, processes, pressure,
GPU - by run, project and agent, with the host's totals beside them, warnings and what to do about them.

  sandbox-ai top [--host HOST] [--project DIR] [--sort cpu|mem|io|net|pids] [--json] [--watch [S]]
  sandbox-ai stats usage [--since 7d] [--by project|run|agent] [--project DIR] [--json] [--series]

The collector is part of each run's launcher on the host (Recorder): every INTERVAL seconds (10 by default;
monitor_interval in your defaults, SANDBOX_AI_MONITOR_INTERVAL) it reads the counters of the run's own cgroup -
the pod's with Podman (every container of it: the agent, the agents --with it, its services), the run's scope with
Bubblewrap; cpu.stat, memory.current/peak/events, io.stat, pids.current, cpu/memory/io.pressure - and the network
counters of its network namespace (/proc/PID/net/dev). A Bubblewrap run without a scope of its own (no limits) is
measured by its process tree in /proc. Where this host cannot see the run's cgroup (macOS: Podman runs in podman
machine's VM; --podman-url) one `podman stats` call every REMOTE_INTERVAL seconds stands in (CPU, memory, block IO,
network, processes; no pressure there). With --gpu and nvidia-smi present, the GPU's use by the run's processes
every GPU_INTERVAL seconds. MCP servers run in sandboxes of their own and are recorded alike; `top` and
`stats usage` count them, and helpers (--helper), to the start they serve and its project.

What is kept, per run, in the log folder of its project (an MCP server's without one: the state directory's
monitor/runs/), as JSON lines: monitor-TIME-RUN.jsonl (each sample, the last RAW_SECONDS), monitor-1m-...
(minutes: averages and peaks, MINUTE_SECONDS) and monitor-1h-... (hours, HOUR_SECONDS); their writer trims them,
so each stays bounded while the run runs; after it ended `sandbox-ai gc` (and its daily pass) compresses them like
the other logs of a run, and log_days removes them with the others. The host's totals (load, CPU, free memory,
swap, disk IO, network, processes and threads, pressure) are written by one launcher at a time (it holds
monitor/host.lock) into monitor/host*.jsonl. `stats usage --series` gives the charts of the app and the web each
run's and the host's detail (DETAIL_KEYS: every metric per time slot of the window, its gaps left out).
The latest look of each running run is in monitor/live/RUN.json (what `top` and the app read: no measuring then).

Warnings (each once per stretch, said again after RENOTIFY): memory near the limit (and the kernel's OOM kills),
the CPU limit holding the run back for long, a run waiting for its disk (IO pressure), a run idle for long that
holds a lot of memory, processes and threads near the limit (or a runaway number without one), and the host low on
memory or swapping. Each goes to the run's safeguard records (the app's and the web's popups, a grouped desktop
notification), to its activity (kind "resources") and to its series, with a suggestion: the command that helps
("sandbox-ai limits RUN --memory 12g", "sandbox-ai stop NAME").

Readers are bounded (READ_LIMIT bytes per file) and take every file as untrusted data. Standard library only.
"""
import argparse
import contextlib
import fcntl
import gzip
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
INTERVAL = 10.0             # seconds between two samples of a run
REMOTE_INTERVAL = 30.0      # with `podman stats` (macOS, --podman-url): one call each
STOP_WAIT = 2.0             # seconds a recorder's stop waits for a look under way (the launcher's end never waits more)
GPU_INTERVAL = 60.0         # nvidia-smi, only with --gpu
INTERVAL_RANGE = (1.0, 3600.0)
RAW_SECONDS = 86400         # every sample, for a day
MINUTE_SECONDS = 7 * 86400  # minutes, for a week
HOUR_SECONDS = 30 * 86400   # hours, for 30 days
TIERS = (("monitor", None, RAW_SECONDS), ("monitor-1m", 60, MINUTE_SECONDS), ("monitor-1h", 3600, HOUR_SECONDS))
TRIM_SLACK = 1.1            # a file is trimmed once it holds this much more than it keeps
READ_LIMIT = 32 << 20       # bytes read of one series file (decompressed)
LINE_LIMIT = 8192           # longer lines are skipped
SPARK = 90                  # samples in a live record (15 minutes at 10 s)
LIVE_FRESH = 3              # a live record older than this many intervals (+5 s) is stale
SERIES_POINTS = 240         # points of a downsampled series (--series)
HOST_FILE = "host"
HOST_STAMP = "00000000-000000"
FILE_NAME = re.compile(r"(monitor-1h|monitor-1m|monitor)-(\d{8}-\d{6})-([A-Za-z0-9._-]{1,128}?)\.jsonl(\.gz)?")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
GIB = 1 << 30

# Warnings: when they start, when they end, how long a condition must hold first (seconds).
MEMORY_NEAR, MEMORY_CLEAR = 0.90, 0.85
THROTTLE_RATIO, THROTTLE_CLEAR, THROTTLE_HOLD = 50.0, 20.0, 120.0
IO_SOME, IO_FULL, IO_CLEAR, IO_HOLD = 40.0, 20.0, 15.0, 60.0
IDLE_CPUS, IDLE_BUSY, IDLE_MEMORY, IDLE_HOLD = 0.02, 0.10, GIB, 1800.0
PIDS_NEAR, PIDS_CLEAR, PIDS_RUNAWAY = 0.90, 0.80, 4000
OOM_SHOWN = 600.0           # an OOM kill stays a warning this long
HOST_MEMORY_LOW, HOST_MEMORY_CLEAR, HOST_HOLD = 0.10, 0.15, 30.0
HOST_SWAP_USED, HOST_SWAP_IN, HOST_SWAP_HOLD = 0.50, 50.0, 60.0
RENOTIFY = 3600.0           # a warning that came back is said again at most this often
KINDS = {  # kind: (level, title)
    "memory": ("warning", "memory near the limit"),
    "oom": ("error", "out of memory: the kernel ended processes"),
    "cpu": ("warning", "held back by the CPU limit"),
    "io": ("warning", "waiting for the disk (IO pressure)"),
    "idle": ("warning", "idle, holding memory"),
    "pids": ("warning", "processes and threads near the limit"),
    "host-memory": ("warning", "the host is low on memory"),
    "host-swap": ("warning", "the host is swapping"),
}
# Kept as they are in a rollup (the last value), summed, at their highest; every other number is averaged.
LAST_KEYS = {"cpu_lim", "mem_lim", "pids_lim", "mem_total", "swap_total", "cpus"}
SUM_KEYS = {"oom", "high", "n", "dt", "self_us"}
MAX_KEYS = {"peak"}
PEAK_KEYS = ("cpu", "cpu_pct", "mem", "rd", "wr", "rx", "tx", "pids", "thr", "psi_io", "psi_mem", "psi_cpu", "gpu",
             "load1", "swap_in")
_modules = {}


def module(name):
    """sandbox-ai's module NAME, loaded once."""
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_monitor", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def monitor_root():
    return state_root() / "monitor"


def printable(value, limit=200):
    text = "".join(c if c.isprintable() else "?" for c in str(value if value is not None else ""))
    return text[:limit]


def number(value):
    """VALUE when it is a finite int or float (never a bool), else None: what a reader takes from a file."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def interval_setting(environ=None, defaults=None):
    """The seconds between two samples: SANDBOX_AI_MONITOR_INTERVAL, else monitor_interval in your defaults, else
    INTERVAL; a value out of INTERVAL_RANGE or not a number counts as not given. 0 in either: no recording."""
    environ = os.environ if environ is None else environ
    if defaults is None:
        path = Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"
        try:
            defaults = json.loads(path.read_text())
        except (OSError, ValueError, RecursionError):
            defaults = {}
    for value in (environ.get("SANDBOX_AI_MONITOR_INTERVAL"), (defaults or {}).get("monitor_interval")
                  if isinstance(defaults, dict) else None):
        if value is None or value == "":
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds == 0:
            return 0.0
        if INTERVAL_RANGE[0] <= seconds <= INTERVAL_RANGE[1]:
            return seconds
    return INTERVAL


# --- reading the counters ---------------------------------------------------------------------------------------

def read_text(path, limit=1 << 20):
    try:
        with open(path, "rb") as stream:
            return stream.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def read_int(path):
    text = read_text(path, 64)
    if text is None:
        return None
    text = text.strip()
    if text == "max":
        return 0
    return int(text) if text.isdigit() else None


def pairs(path):
    """{key: int} of a "key value" file (cpu.stat, memory.stat, memory.events, /proc/vmstat)."""
    found = {}
    for line in (read_text(path) or "").splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        if value.isdigit():
            found[key] = int(value)
    return found


def pressure(path):
    """{"some": avg10, "full": avg10} of a PSI file (cpu.pressure, memory.pressure, /proc/pressure/io), {} without."""
    found = {}
    for line in (read_text(path, 4096) or "").splitlines():
        kind, _, rest = line.partition(" ")
        for part in rest.split():
            key, _, value = part.partition("=")
            if key == "avg10":
                with contextlib.suppress(ValueError):
                    found[kind] = float(value)
    return found


def io_bytes(path):
    """(bytes read, bytes written) of an io.stat: every device summed; None without the file."""
    text = read_text(path)
    if text is None:
        return None
    read = written = 0
    for line in text.splitlines():
        for part in line.split()[1:]:
            key, _, value = part.partition("=")
            if value.isdigit():
                if key == "rbytes":
                    read += int(value)
                elif key == "wbytes":
                    written += int(value)
    return read, written


def net_bytes(pid, proc=PROC, own=None):
    """(received, sent) bytes of the network namespace of PID (/proc/PID/net/dev, every interface but lo), or None:
    no PID, gone, or the host's own namespace (OWN: this process's; a sandbox without one of its own would count
    the host's whole traffic)."""
    if not pid:
        return None
    try:
        theirs = os.readlink(Path(proc) / str(pid) / "ns" / "net")
        mine = own if own is not None else os.readlink(Path(proc) / "self" / "ns" / "net")
    except OSError:
        theirs = mine = None
    if theirs is not None and theirs == mine:
        return None
    return net_dev(Path(proc) / str(pid) / "net" / "dev")


def net_dev(path):
    """(received, sent) bytes of every interface but lo in the /proc/net/dev at PATH, or None."""
    text = read_text(path)
    if text is None:
        return None
    received = sent = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if name.strip() == "lo" or len(fields) < 9:
            continue
        with contextlib.suppress(ValueError):
            received += int(fields[0])
            sent += int(fields[8])
    return received, sent


# Podman's own cgroups (systemd's names for them): a container's scope and its pod's slice, the id in hex.
CONTAINER_SCOPE = re.compile(r"libpod-[0-9a-f]+\.scope")
POD_SLICE = re.compile(r"(?:[A-Za-z0-9_-]+-)?libpod_pod_[0-9a-f]+\.slice")


def sandbox_top(path):
    """The run's own top cgroup for the cgroup PATH of one of its processes - Podman: the pod's
    (*libpod_pod_<id>.slice) of the container's libpod-<id>.scope, or that scope when it is in no pod and inside no
    other container; Bubblewrap: its systemd-run scope (run-*.scope) - or None when PATH lies in no such cgroup (then
    nothing above it is the run's alone). Only Podman's forms count, innermost first: a cgroup named like one inside a
    container's scope without a pod's slice between them (libpod-x, a libpod-<hex>.scope made below it) is never the
    run's, and a sandbox-ai inside a sandbox measures its own pod."""
    path = Path(path)
    for candidate in (path, *path.parents):
        if not CONTAINER_SCOPE.fullmatch(candidate.name):
            continue
        if POD_SLICE.fullmatch(candidate.parent.name):
            return candidate.parent
        if not any(CONTAINER_SCOPE.fullmatch(above.name) or POD_SLICE.fullmatch(above.name)
                   for above in candidate.parents):
            return candidate
    for candidate in (path, *path.parents):
        if candidate.name.startswith("run-") and candidate.name.endswith(".scope"):
            return candidate
    return None


def cgroup_of(pid, proc=PROC, root=CGROUP_ROOT):
    """(top, leaf) cgroup directories of the run whose process PID is (sandbox_top), or None."""
    text = read_text(Path(proc) / str(int(pid)) / "cgroup", 4096) if pid else None
    line = next((entry for entry in (text or "").splitlines() if entry.startswith("0::")), None)
    if line is None:
        return None
    leaf = Path(root) / line[3:].strip().lstrip("/")
    top = sandbox_top(leaf)
    return (top, leaf) if top is not None and top.is_dir() else None


def limits_on_path(leaf, top):
    """{"mem_lim", "cpu_lim", "pids_lim"}: the smallest limit set from LEAF up to TOP (a pod's or a container's),
    0 where there is none."""
    found = {"mem_lim": 0, "cpu_lim": 0.0, "pids_lim": 0}
    leaf, top = Path(leaf), Path(top)
    path = [leaf, *[p for p in leaf.parents if p == top or top in p.parents]] if leaf != top else [top]
    for directory in path:
        memory = read_int(directory / "memory.max")
        if memory and (not found["mem_lim"] or memory < found["mem_lim"]):
            found["mem_lim"] = memory
        pids = read_int(directory / "pids.max")
        if pids and (not found["pids_lim"] or pids < found["pids_lim"]):
            found["pids_lim"] = pids
        text = (read_text(directory / "cpu.max", 64) or "").split()
        if len(text) >= 2 and text[0].isdigit() and text[1].isdigit() and int(text[1]):
            cpus = round(int(text[0]) / int(text[1]), 2)
            if not found["cpu_lim"] or cpus < found["cpu_lim"]:
                found["cpu_lim"] = cpus
    return found


def count_processes(top):
    """Processes in the cgroup TOP and below it (cgroup.procs; threads are pids.current)."""
    count = 0
    for directory, _, _ in os.walk(top):
        text = read_text(Path(directory) / "cgroup.procs")
        if text:
            count += sum(1 for line in text.split() if line.isdigit())
    return count


def cgroup_counters(top, leaf=None, pid=None, proc=PROC, clock=time.time, own_netns=None):
    """The counters of the run's cgroup TOP (its limits from LEAF up; the network of PID's namespace), or None when
    TOP is gone. Cumulative ones (CPU, IO, network, OOM kills) become rates in sample()."""
    top = Path(top)
    memory = read_int(top / "memory.current")
    cpu = pairs(top / "cpu.stat")
    if memory is None and not cpu:
        return None
    counters = {"time": clock(), "source": "cgroup", "mem": memory or 0, **limits_on_path(leaf or top, top)}
    peak = read_int(top / "memory.peak")
    if peak:
        counters["peak"] = peak
    if cpu:
        counters.update(cpu_usec=cpu.get("usage_usec", 0), periods=cpu.get("nr_periods", 0),
                        throttled=cpu.get("nr_throttled", 0))
    stat, events = pairs(top / "memory.stat"), pairs(top / "memory.events")
    counters.update(tmpfs=stat.get("shmem", 0), oom=events.get("oom_kill", 0), high=events.get("high", 0))
    io = io_bytes(top / "io.stat")
    if io is not None:
        counters["rbytes"], counters["wbytes"] = io
    tasks = read_int(top / "pids.current")
    if tasks is not None:
        counters["pids"] = tasks
    counters["procs"] = count_processes(top)
    for name, key in (("cpu", "cpu"), ("memory", "mem"), ("io", "io")):
        found = pressure(top / f"{name}.pressure")
        if "some" in found:
            counters[f"psi_{key}"] = found["some"]
        if "full" in found and key != "cpu":
            counters[f"psi_{key}_full"] = found["full"]
    net = net_bytes(pid, proc, own_netns)
    if net is not None:
        counters["rx_bytes"], counters["tx_bytes"] = net
    return counters


def descendants(pid, proc=PROC):
    """PID and every process below it (/proc/*/stat's parent PIDs)."""
    children = {}
    try:
        entries = os.listdir(proc)
    except OSError:
        return [pid]
    for entry in entries:
        if not entry.isdigit():
            continue
        text = read_text(Path(proc) / entry / "stat", 4096)
        if not text or ")" not in text:
            continue
        fields = text[text.rfind(")") + 2:].split()
        if len(fields) > 1 and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(entry))
    found, queue = [], [int(pid)]
    while queue and len(found) < 100000:
        current = queue.pop()
        found.append(current)
        queue.extend(children.get(current, ()))
    return found


def tree_counters(pid, proc=PROC, clock=time.time, own_netns=None):
    """The counters of the process tree below PID (a Bubblewrap run without a scope of its own), or None when PID
    is gone: CPU (its processes' times and those of the children each of them waited for: an ended process is in
    exactly one living parent's cutime/cstime, or the root's), resident memory, disk IO (/proc/PID/io), threads and
    processes. No limits, no pressure."""
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    ticks = rss = read = written = threads = processes = 0
    for index, member in enumerate(descendants(pid, proc)):
        text = read_text(Path(proc) / str(member) / "stat", 4096)
        if not text or ")" not in text:
            if index == 0:
                return None
            continue
        fields = text[text.rfind(")") + 2:].split()
        with contextlib.suppress(IndexError, ValueError):
            ticks += int(fields[11]) + int(fields[12]) + int(fields[13]) + int(fields[14])
            threads += int(fields[17])
            rss += int(fields[21]) * os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 0
        processes += 1
        io = pairs_colon(Path(proc) / str(member) / "io")
        read += io.get("read_bytes", 0)
        written += io.get("write_bytes", 0)
    counters = {"time": clock(), "source": "tree", "cpu_usec": ticks * 1_000_000 // tick, "mem": rss,
                "rbytes": read, "wbytes": written, "pids": threads, "procs": processes}
    net = net_bytes(pid, proc, own_netns)
    if net is not None:
        counters["rx_bytes"], counters["tx_bytes"] = net
    return counters


def pairs_colon(path):
    """{key: int} of a "key: value" file (/proc/PID/io, /proc/meminfo in kB as written)."""
    found = {}
    for line in (read_text(path) or "").splitlines():
        key, _, value = line.partition(":")
        value = value.split()
        if value and value[0].isdigit():
            found[key.strip()] = int(value[0])
    return found


UNITS = {"": 1, "b": 1, "k": 1000, "kb": 1000, "kib": 1024, "m": 1000 ** 2, "mb": 1000 ** 2, "mib": 1024 ** 2,
         "g": 1000 ** 3, "gb": 1000 ** 3, "gib": 1024 ** 3, "t": 1000 ** 4, "tb": 1000 ** 4, "tib": 1024 ** 4}


def size_value(text):
    """Bytes of a size as Podman prints it ("1.2kB", "8GiB", "648B"), or None."""
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*", str(text))
    if not match or match.group(2).lower() not in UNITS:
        return None
    return int(float(match.group(1)) * UNITS[match.group(2).lower()])


def stats_rows(text):
    """The rows of `podman stats --format {{json .ContainerStats}}` (one JSON object a line) or of --format json (a
    list): dicts, whatever their form."""
    text = (text or "").strip()
    if not text:
        return []
    with contextlib.suppress(ValueError, RecursionError):
        data = json.loads(text)
        return [row for row in (data if isinstance(data, list) else [data]) if isinstance(row, dict)]
    rows = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError, RecursionError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def pair_values(text):
    """(first, second) bytes of Podman's "1.2kB / 648B", or (None, None)."""
    first, _, second = str(text or "").partition("/")
    return size_value(first), size_value(second)


def stats_counters(text, prefix, clock=time.time):
    """The counters of the containers whose name starts with PREFIX (a run's pod: POD-...), summed, from what
    `podman stats` printed (TEXT); None when none of them is there. Raw numbers (ContainerStats) where Podman gives
    them, else its printed sizes and its CPU percentage of the moment."""
    found, any_row = {"time": clock(), "source": "podman", "mem": 0, "mem_lim": 0, "rx_bytes": 0, "tx_bytes": 0,
                      "rbytes": 0, "wbytes": 0, "pids": 0}, False
    now_cpus, usec = 0.0, 0
    for row in stats_rows(text):
        name = str(row.get("Name") or row.get("name") or "")
        if not name.startswith(prefix):
            continue
        any_row = True
        if number(row.get("CPUNano")) is not None:
            usec += int(row["CPUNano"]) // 1000
            found["mem"] += int(number(row.get("MemUsage")) or 0)
            found["mem_lim"] = max(found["mem_lim"], int(number(row.get("MemLimit")) or 0))
            found["rx_bytes"] += int(number(row.get("NetInput")) or 0)
            found["tx_bytes"] += int(number(row.get("NetOutput")) or 0)
            found["rbytes"] += int(number(row.get("BlockInput")) or 0)
            found["wbytes"] += int(number(row.get("BlockOutput")) or 0)
            found["pids"] += int(number(row.get("PIDs")) or 0)
        else:  # --format json: printed values ("cpu_percent": "12.5%", "mem_usage": "1.2GB / 8GB")
            with contextlib.suppress(ValueError):
                now_cpus += float(str(row.get("cpu_percent") or row.get("CPU") or "0").rstrip("%")) / 100
            used, limit = pair_values(row.get("mem_usage") or row.get("MemUsage"))
            found["mem"] += used or 0
            found["mem_lim"] = max(found["mem_lim"], limit or 0)
            received, sent = pair_values(row.get("net_io") or row.get("NetIO"))
            found["rx_bytes"] += received or 0
            found["tx_bytes"] += sent or 0
            read, written = pair_values(row.get("block_io") or row.get("BlockIO"))
            found["rbytes"] += read or 0
            found["wbytes"] += written or 0
            with contextlib.suppress(ValueError):
                found["pids"] += int(str(row.get("pids") or row.get("PIDS") or "0"))
    if not any_row:
        return None
    if usec:
        found["cpu_usec"] = usec
    else:
        found["cpu_now"] = round(now_cpus, 3)
    return found


STATS_FORMAT = "{{json .ContainerStats}}"


def podman_stats(podman_prefix, run=subprocess.run, timeout=30):
    """What `podman stats --no-stream` prints for every running container (raw numbers), or "" when it failed."""
    try:
        done = run([*podman_prefix, "stats", "--no-stream", "--format", STATS_FORMAT], capture_output=True,
                   text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0 or not done.stdout.strip():
        try:  # an older Podman without ContainerStats in its template: its own JSON, printed values
            done = run([*podman_prefix, "stats", "--no-stream", "--format", "json"], capture_output=True, text=True,
                       timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return ""
    return done.stdout if done.returncode == 0 else ""


def gpu_counters(pids, run=subprocess.run, timeout=10):
    """{"gpu": percent, "gpu_mem": bytes} of the NVIDIA GPU work of PIDS (nvidia-smi), or {} (none, no driver)."""
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        apps = run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                   capture_output=True, text=True, timeout=timeout, check=False).stdout
        gpus = run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                   capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    wanted, memory, used = set(pids or ()), 0, False
    for line in apps.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) in wanted and parts[1].isdigit():
            memory += int(parts[1]) << 20
            used = True
    if not used:
        return {}
    utilization = [int(part) for part in gpus.split() if part.strip().isdigit()]
    return {"gpu": max(utilization) if utilization else 0, "gpu_mem": memory}


def host_counters(proc=PROC, clock=time.time, sys_block=Path("/sys/block")):
    """The host's counters (Linux /proc): CPU jiffies, load, memory, swap, swapping, disk IO, pressure; None when
    /proc/stat cannot be read (macOS: host_counters_mac)."""
    text = read_text(Path(proc) / "stat", 4096)
    if not text:
        return None
    fields = text.splitlines()[0].split()
    if not fields or fields[0] != "cpu":
        return None
    values = [int(value) for value in fields[1:] if value.isdigit()]
    counters = {"time": clock(), "cpu_total": sum(values[:8]), "cpu_idle": sum(values[3:5]),
                "cpus": os.cpu_count() or 1}
    load = (read_text(Path(proc) / "loadavg", 256) or "").split()
    with contextlib.suppress(IndexError, ValueError):
        counters["load1"] = float(load[0])
    with contextlib.suppress(IndexError, ValueError):
        counters["pids"] = int(load[3].partition("/")[2])  # (processes and threads of the host, as pids.current)
    network = net_dev(Path(proc) / "net" / "dev")
    if network is not None:
        counters["rx_bytes"], counters["tx_bytes"] = network
    memory = pairs_colon(Path(proc) / "meminfo")
    if "MemTotal" in memory:
        counters["mem_total"] = memory["MemTotal"] * 1024
        counters["mem_avail"] = memory.get("MemAvailable", memory.get("MemFree", 0)) * 1024
        counters["swap_total"] = memory.get("SwapTotal", 0) * 1024
        counters["swap_used"] = max(0, memory.get("SwapTotal", 0) - memory.get("SwapFree", 0)) * 1024
    vm = pairs(Path(proc) / "vmstat")
    counters["pswpin"], counters["pswpout"] = vm.get("pswpin", 0), vm.get("pswpout", 0)
    try:
        disks = {name for name in os.listdir(sys_block) if not name.startswith(("loop", "ram", "zram", "dm-"))}
    except OSError:
        disks = set()
    read = written = 0
    for line in (read_text(Path(proc) / "diskstats") or "").splitlines():
        parts = line.split()
        if len(parts) >= 10 and parts[2] in disks:
            with contextlib.suppress(ValueError):
                read += int(parts[5]) * 512
                written += int(parts[9]) * 512
    counters["rbytes"], counters["wbytes"] = read, written
    for name, key in (("cpu", "cpu"), ("memory", "mem"), ("io", "io")):
        found = pressure(Path(proc) / "pressure" / name)
        if "some" in found:
            counters[f"psi_{key}"] = found["some"]
    return counters


def mac_net_bytes(text):
    """(received, sent) bytes of every interface but the loopback ones in TEXT (macOS `netstat -ibn`: each
    interface's <Link#N> row once - its address rows repeat its counters), or None for a table that is not one. The
    columns are counted from the right as its header names them (a row without an address has that column empty)."""
    lines = (text or "").splitlines()
    header = lines[0].split() if lines else []
    if "Ibytes" not in header or "Obytes" not in header:
        return None
    rx_at, tx_at = header.index("Ibytes") - len(header), header.index("Obytes") - len(header)
    received = sent = 0
    seen = set()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < len(header) - 1 or not parts[2].startswith("<Link#"):
            continue
        name = parts[0].rstrip("*")
        if name in seen or name.startswith("lo"):
            continue
        if not (parts[rx_at].isdigit() and parts[tx_at].isdigit()):
            continue
        seen.add(name)
        received += int(parts[rx_at])
        sent += int(parts[tx_at])
    return (received, sent) if seen else None


def host_counters_mac(run=subprocess.run, clock=time.time):
    """The Mac's counters: load, CPUs, memory (vm_stat: free, inactive and speculative pages count as available),
    swap (sysctl vm.swapusage), network (netstat -ibn: every interface but lo0, as /proc/net/dev on Linux) and the
    threads of every process (sysctl kern.num_threads, as the 4th field of Linux's /proc/loadavg). No disk IO, no
    pressure, no CPU busy share. A counter whose command does not answer is left out."""
    counters = {"time": clock(), "cpus": os.cpu_count() or 1}
    with contextlib.suppress(OSError):
        counters["load1"] = round(os.getloadavg()[0], 2)
    try:
        total = run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5, check=False).stdout
        vm = run(["vm_stat"], capture_output=True, text=True, timeout=5, check=False).stdout
        swap = run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return counters
    if total.strip().isdigit():
        counters["mem_total"] = int(total.strip())
    page = re.search(r"page size of (\d+) bytes", vm)
    size = int(page.group(1)) if page else 4096
    pages = {key.strip().lower(): int(value.strip().rstrip(".")) for key, value in
             (line.split(":", 1) for line in vm.splitlines()[1:] if ":" in line) if value.strip().rstrip(".").isdigit()}
    counters["mem_avail"] = size * sum(pages.get(key, 0) for key in ("pages free", "pages inactive",
                                                                      "pages speculative"))
    used = re.search(r"used = ([0-9.]+[KMGT]?)", swap)
    whole = re.search(r"total = ([0-9.]+[KMGT]?)", swap)
    if used and whole:  # (macOS's M and G are binary: 2048.00M is 2 GiB)
        counters["swap_used"], counters["swap_total"] = (size_value(used.group(1) + "iB"),
                                                         size_value(whole.group(1) + "iB"))
    for argv, take in ((["netstat", "-ibn"], mac_net_bytes), (["sysctl", "-n", "kern.num_threads"], None)):
        try:
            text = run(argv, capture_output=True, text=True, timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if take is not None:
            network = take(text)
            if network is not None:
                counters["rx_bytes"], counters["tx_bytes"] = network
        elif text.strip().isdigit():
            counters["pids"] = int(text.strip())  # (processes and threads of the host, as on Linux)
    return counters


# --- samples, rollups --------------------------------------------------------------------------------------------

def rate(after, before, key, seconds):
    if key not in after or key not in before or seconds <= 0:
        return None
    return max(0, after[key] - before[key]) / seconds


def sample(before, after, host_cpus=None):
    """One sample of a run from two looks at its counters (BEFORE, AFTER): the CPUs used and their share of the
    limit (else of the host), how often the limit held it back, memory and its limit, IO and network per second,
    processes, pressure, GPU; the keys without a value are left out. None without both looks."""
    if not before or not after:
        return None
    seconds = after["time"] - before["time"]
    if seconds <= 0:
        return None
    host_cpus = host_cpus or os.cpu_count() or 1
    found = {"t": int(after["time"]), "dt": round(seconds, 1)}
    if "cpu_now" in after:
        cpus = after["cpu_now"]
    else:
        used = rate(after, before, "cpu_usec", seconds)
        cpus = used / 1e6 if used is not None else None
    if cpus is not None:
        found["cpu"] = round(cpus, 3)
        of = after.get("cpu_lim") or host_cpus
        found["cpu_pct"] = round(100 * cpus / of, 1)
    periods = after.get("periods", 0) - before.get("periods", 0)
    if periods > 0:
        found["thr"] = round(100 * max(0, after.get("throttled", 0) - before.get("throttled", 0)) / periods, 1)
    for key in ("cpu_lim", "mem", "mem_lim", "peak", "tmpfs", "pids", "pids_lim", "procs", "psi_cpu", "psi_mem",
                "psi_mem_full", "psi_io", "psi_io_full", "gpu", "gpu_mem"):
        if after.get(key) not in (None, "") and (after.get(key) or key not in ("cpu_lim", "mem_lim", "pids_lim")):
            found[key] = after[key]
    if after.get("mem_lim"):
        found["mem_pct"] = round(100 * after["mem"] / after["mem_lim"], 1)
    for key, out in (("rbytes", "rd"), ("wbytes", "wr"), ("rx_bytes", "rx"), ("tx_bytes", "tx")):
        value = rate(after, before, key, seconds)
        if value is not None:
            found[out] = int(value)
    for key in ("oom", "high"):
        if key in after and key in before:
            found[key] = max(0, after[key] - before[key])
    return found


def host_sample(before, after):
    """One sample of the host from two looks (host_counters): its CPUs busy (percent of all), load, memory, swap,
    swapping (pages per second), disk IO per second, pressure."""
    if not after:
        return None
    found = {"t": int(after["time"]), "cpus": after.get("cpus")}
    for key in ("load1", "mem_total", "mem_avail", "swap_total", "swap_used", "psi_cpu", "psi_mem", "psi_io", "pids"):
        if after.get(key) is not None:
            found[key] = after[key]
    if before and after["time"] > before["time"]:
        seconds = after["time"] - before["time"]
        found["dt"] = round(seconds, 1)
        total = after.get("cpu_total", 0) - before.get("cpu_total", 0)
        if total > 0:
            found["cpu_pct"] = round(100 * (total - (after.get("cpu_idle", 0) - before.get("cpu_idle", 0))) / total, 1)
        for key, out in (("rbytes", "rd"), ("wbytes", "wr"), ("rx_bytes", "rx"), ("tx_bytes", "tx"),
                         ("pswpin", "swap_in"), ("pswpout", "swap_out")):
            value = rate(after, before, key, seconds)
            if value is not None:
                found[out] = round(value, 1) if out.startswith("swap") else int(value)
    return {key: value for key, value in found.items() if value is not None}


class Rollup:
    """Samples (or smaller rollups) gathered into records of WIDTH seconds: averages (weighted by the seconds each
    covers), the highest of PEAK_KEYS as KEY_max, sums of SUM_KEYS, the last of LAST_KEYS. add() returns the record
    of a bucket that is complete, else None; flush() the one begun."""

    def __init__(self, width):
        self.width, self.bucket, self.sums, self.weights, self.record = width, None, {}, {}, {}

    def add(self, entry):
        if number(entry.get("t")) is None:
            return None
        bucket = int(entry["t"]) // self.width * self.width
        done = self.flush() if self.bucket is not None and bucket != self.bucket else None
        self.bucket = bucket
        weight = number(entry.get("dt")) or 1.0
        record = self.record
        record["n"] = record.get("n", 0) + int(number(entry.get("n")) or 1)
        for key, value in entry.items():
            if key in ("t", "n") or number(value) is None:
                continue
            if key.endswith("_max"):
                record[key] = max(record.get(key, value), value)
            elif key in SUM_KEYS:
                record[key] = round(record.get(key, 0) + value, 1)
            elif key in LAST_KEYS:
                record[key] = value
            elif key in MAX_KEYS:
                record[key] = max(record.get(key, value), value)
            else:
                self.sums[key] = self.sums.get(key, 0.0) + value * weight
                self.weights[key] = self.weights.get(key, 0.0) + weight
                if key in PEAK_KEYS:
                    record[f"{key}_max"] = max(record.get(f"{key}_max", value), value)
        return done

    def flush(self):
        if self.bucket is None or not self.record:
            return None
        done = {"t": self.bucket, **self.record}
        for key, total in self.sums.items():
            value = total / self.weights[key] if self.weights[key] else 0
            done[key] = int(value) if isinstance(value, float) and value > 1000 else round(value, 3)
        self.bucket, self.sums, self.weights, self.record = None, {}, {}, {}
        return done


# --- the series files --------------------------------------------------------------------------------------------

TRIM_LIMIT = 64 << 20       # bytes of a series file a trim reads (a tier's file holds a few MiB at most)


def open_series(path, flags):
    """A descriptor of the series file PATH opened with FLAGS - a regular file, never a link, and never waited for
    (a FIFO in its place): a run's log folder is mounted into its sandbox, so what lies there is the sandbox's word.
    OSError for anything else."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOCTTY", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
    except OSError:
        os.close(fd)
        raise
    return fd


def read_bounded(fd, limit):
    """At most LIMIT bytes of the file FD, from its start."""
    chunks, size = [], 0
    while size < limit:
        chunk = os.pread(fd, min(1 << 20, limit - size), size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def count_lines(fd, limit=TRIM_LIMIT):
    """The lines of the file FD (within its first LIMIT bytes)."""
    count = offset = 0
    while offset < limit:
        chunk = os.pread(fd, min(1 << 20, limit - offset), offset)
        if not chunk:
            break
        count += chunk.count(b"\n")
        offset += len(chunk)
    return count


def write_private(path, data):
    """DATA as the new 0600 file PATH: made exclusively, never through a link (what lay there is removed first)."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


class Series:
    """A run's (or the host's) series in DIRECTORY: one file per tier (TIERS), named KIND-STAMP-NAME.jsonl, each
    beginning with HEADER; samples go in at write(), rollups follow, events (warnings) at event(). Each file is
    trimmed to what its tier keeps once it holds TRIM_SLACK times that."""

    def __init__(self, directory, name, header, interval, stamp=None, tiers=TIERS):
        self.directory, self.name, self.interval = Path(directory), name, interval
        self.header = {"header": 1, **header, "interval": interval}
        stamp = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.files, self.keep, self.lines, self.rollups = {}, {}, {}, {}
        for kind, width, seconds in tiers:
            self.files[kind] = self.directory / f"{kind}-{stamp}-{name}.jsonl"
            self.keep[kind] = max(10, int(seconds / (width or interval)))
            self.lines[kind] = None
            if width:
                self.rollups[kind] = Rollup(width)
        self.lock = threading.Lock()

    def append(self, kind, records):
        path = self.files[kind]
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = open_series(path, os.O_RDWR | os.O_APPEND | os.O_CREAT)
        try:
            if self.lines[kind] is None:
                existing = count_lines(fd)
                if not existing:
                    records = [self.header, *records]
                self.lines[kind] = max(0, existing - 1)
            data = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
            os.write(fd, data.encode())
        finally:
            os.close(fd)
        self.lines[kind] += sum(1 for record in records if "header" not in record)
        if self.lines[kind] > self.keep[kind] * TRIM_SLACK:
            self.trim(kind)

    def trim(self, kind):
        """The file of KIND keeps its header and its newest lines (what the tier keeps). Read and written as the
        folder's other files are: never through a link (open_series, write_private)."""
        path = self.files[kind]
        try:
            fd = open_series(path, os.O_RDONLY)
        except OSError:
            return
        try:
            lines = read_bounded(fd, TRIM_LIMIT).splitlines(keepends=True)
        except OSError:
            return
        finally:
            os.close(fd)
        if lines and not lines[-1].endswith(b"\n"):
            lines.pop()  # (a file longer than TRIM_LIMIT, or a line cut: only whole lines are kept)
        body = [line for line in lines[1:]][-self.keep[kind]:]
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        try:
            write_private(temporary, b"".join(lines[:1] + body))
            os.replace(temporary, path)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            return
        self.lines[kind] = len(body)

    def write(self, entry):
        with self.lock, contextlib.suppress(OSError):
            pending = {kind: [] for kind in self.files}
            first = next(iter(self.files))
            pending[first].append(entry)
            finished = entry
            for kind in list(self.files)[1:]:
                finished = self.rollups[kind].add(finished) if finished is not None else None
                if finished is not None:
                    pending[kind].append(finished)
            for kind, records in pending.items():
                if records:
                    self.append(kind, records)

    def event(self, entry):
        """A warning (or its end) into the samples and the minutes (the samples are kept for a day only)."""
        with self.lock, contextlib.suppress(OSError):
            for kind in list(self.files)[:2]:
                self.append(kind, [entry])

    def close(self):
        """The rollups begun are written as they are (a run that ended within a minute has its minute too)."""
        with self.lock, contextlib.suppress(OSError):
            carried = None
            for kind in list(self.files)[1:]:
                rollup = self.rollups[kind]
                if carried is not None:
                    rollup.add(carried)
                carried = rollup.flush()
                if carried is not None:
                    self.append(kind, [carried])


def write_json(path, data):
    """DATA as the JSON file PATH, replaced at once (never through a link)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    with contextlib.suppress(OSError):
        write_private(temporary, json.dumps(data, separators=(",", ":")).encode())
        os.replace(temporary, path)
    with contextlib.suppress(OSError):
        temporary.unlink()


# --- warnings --------------------------------------------------------------------------------------------------

def gib_text(value):
    return f"{(value or 0) / GIB:.1f}"


def duration_text(seconds):
    seconds = int(seconds or 0)
    if seconds >= 7200:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 120:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def memory_suggestion(limit, peak):
    """The --memory to suggest: half again the limit, at least a quarter above the peak, whole GiB."""
    wanted = max(limit * 1.5, peak * 1.25, limit + GIB)
    return f"{math.ceil(wanted / GIB)}g"


def cpu_suggestion(limit, host_cpus):
    wanted = min(float(host_cpus or 1), max(limit + 1, limit * 2))
    return f"{wanted:g}" if wanted != int(wanted) else str(int(wanted))


def idle_role(header):
    """(role, owner) of a run as its idle warning says it (Recorder's HEADER): "mcp" (an MCP server's sandbox, owner:
    the server's name), "helper" (a session's helper, owner: that session), "session" (a kept start: stopped, it is
    kept) or "run" (not kept: stopped, it ends for good)."""
    if header.get("kind") == "mcp":
        return "mcp", header.get("mcp_name") or header.get("name")
    if header.get("helper_of"):
        return "helper", header["helper_of"]
    return ("session" if header.get("kept") else "run"), None


class Assessor:
    """The warnings of one run from its samples (feed): when each starts and ends (see the thresholds above), with
    what was measured, a text, a suggestion and the command that helps. ROLE and OWNER (idle_role): what the idle
    warning suggests - a session is stopped and kept, a run ends for good, an MCP server and a helper wait between
    calls by nature (no command offered: stopping one would break the sandboxes that use it)."""

    def __init__(self, run, name, host_cpus=None, role="run", owner=None):
        self.run, self.name, self.host_cpus = run, name or run, host_cpus or os.cpu_count() or 1
        self.role, self.owner = role, owner
        self.active, self.since, self.peak, self.high, self.oom = {}, {}, 0, 0, 0
        self.throttle_from, self.idle_from, self.idle_memory = None, None, 0

    def warning(self, kind, t, values, text, advice, argv=None):
        level, title = KINDS[kind]
        return {"kind": kind, "level": level, "title": title, "run": self.run, "name": self.name, "since": t,
                "values": values, "text": text, "advice": advice, "argv": argv}

    def idle_advice(self, memory, seconds):
        """(advice, argv) of the idle warning for this run's role."""
        held = f"{gib_text(memory)} GiB for {duration_text(seconds)}"
        if self.role == "mcp":
            return (f"an MCP server waits for calls between them: if no running sandbox uses it any more, sandbox-ai "
                    f"mcp stop {self.owner or self.name} (it ends it for every sandbox that uses it)"), None
        if self.role == "helper":
            return (f"a helper waits for its session's tasks between them: it ends with {self.owner} (sandbox-ai stop "
                    f"{self.owner})"), None
        if self.role == "session":
            return (f"stop idle session {self.name}: {held}; it is kept: sandbox-ai resume {self.name} starts it "
                    "again"), ["stop", self.name]
        return f"stop idle run {self.name}: {held}; it is not kept: what runs in it ends for good", ["stop", self.name]

    def condition(self, kind, on, off, t, hold, make):
        """KIND starts when ON held for HOLD seconds, ends when OFF: (started, ended) warnings or None."""
        if kind in self.active:
            if off:
                self.since.pop(kind, None)
                return None, self.active.pop(kind)
            return None, None
        if not on:
            self.since.pop(kind, None)
            return None, None
        began = self.since.setdefault(kind, t)
        if t - began < hold:
            return None, None
        self.active[kind] = made = make(began)
        return made, None

    def feed(self, s):
        """(started, ended): the warnings sample S starts and ends."""
        t = int(time.time() if number(s.get("t")) is None else s["t"])
        started, ended = [], []
        mem, limit = number(s.get("mem")) or 0, number(s.get("mem_lim")) or 0
        self.peak = max(self.peak, mem, number(s.get("peak")) or 0)
        self.high += int(number(s.get("high")) or 0)
        results = []
        if limit:
            peak, high = self.peak, self.high
            results.append(self.condition(
                "memory", mem >= MEMORY_NEAR * limit, mem < MEMORY_CLEAR * limit, t, 0,
                lambda began: self.warning(
                    "memory", began, {"used": mem, "limit": limit, "peak": peak, "high": high},
                    f"{self.name} uses {gib_text(mem)} of {gib_text(limit)} GiB ({100 * mem / limit:.0f} %)",
                    f"raise --memory to {memory_suggestion(limit, peak)}: peak {gib_text(peak)} of {gib_text(limit)} "
                    f"GiB with {high} throttling events",
                    ["limits", self.run, "--memory", memory_suggestion(limit, peak)])))
        oom = int(number(s.get("oom")) or 0)
        if oom:
            self.oom += oom
            if "oom" in self.active:
                self.active["oom"]["values"]["kills"] = self.oom
            else:
                suggestion = memory_suggestion(limit, self.peak) if limit else None
                made = self.warning("oom", t, {"kills": self.oom, "limit": limit, "peak": self.peak},
                                    f"the kernel ended {self.oom} process{'es' if self.oom != 1 else ''} of "
                                    f"{self.name} for lack of memory",
                                    f"raise --memory to {suggestion}, or run fewer things at once" if suggestion
                                    else "run fewer things at once", ["limits", self.run, "--memory", suggestion]
                                    if suggestion else None)
                self.active["oom"] = made
                started.append(made)
        elif "oom" in self.active and t - self.active["oom"]["since"] >= OOM_SHOWN:
            ended.append(self.active.pop("oom"))
        thr, cpu_limit = number(s.get("thr")), number(s.get("cpu_lim")) or 0
        if cpu_limit and thr is not None:
            results.append(self.condition(
                "cpu", thr >= THROTTLE_RATIO, thr < THROTTLE_CLEAR, t, THROTTLE_HOLD,
                lambda began: self.warning(
                    "cpu", began, {"throttled": thr, "limit": cpu_limit, "cpus": s.get("cpu"), "seconds": t - began},
                    f"{self.name} is held back by its CPU limit ({cpu_limit:g} CPUs) in {thr:.0f} % of the periods",
                    f"raise --cpus to {cpu_suggestion(cpu_limit, self.host_cpus)}: held back in {thr:.0f} % of the "
                    f"periods for {duration_text(t - began)}",
                    ["limits", self.run, "--cpus", cpu_suggestion(cpu_limit, self.host_cpus)])))
        some, full = number(s.get("psi_io")), number(s.get("psi_io_full"))
        if some is not None or full is not None:
            some, full = some or 0.0, full or 0.0
            results.append(self.condition(
                "io", some >= IO_SOME or full >= IO_FULL, some < IO_CLEAR and full < IO_CLEAR / 2, t, IO_HOLD,
                lambda began: self.warning(
                    "io", began, {"some": some, "full": full, "read": s.get("rd"), "written": s.get("wr")},
                    f"{self.name} waits for the disk {some:.0f} % of the time",
                    "the disk holds it back: run fewer builds or tests at once, keep caches in /tmp (memory), or "
                    "move the workspace to a faster disk")))
        cpus = number(s.get("cpu"))
        if cpus is not None:
            idle_on = cpus < IDLE_CPUS and mem >= IDLE_MEMORY
            results.append(self.condition(
                "idle", idle_on, cpus >= IDLE_BUSY or mem < IDLE_MEMORY / 2, t, IDLE_HOLD,
                lambda began: self.warning(
                    "idle", began, {"memory": mem, "seconds": t - began, "role": self.role,
                                    **({"owner": self.owner} if self.owner else {})},
                    f"{self.name} has been idle for {duration_text(t - began)} and holds {gib_text(mem)} GiB",
                    *self.idle_advice(mem, t - began))))
        pids, pids_limit = number(s.get("pids")), number(s.get("pids_lim")) or 0
        if pids is not None:
            if pids_limit:
                on, off = pids >= PIDS_NEAR * pids_limit, pids < PIDS_CLEAR * pids_limit
                wanted = str(max(pids_limit * 2, 1024))
                advice = f"raise --pids to {wanted}: {pids} of {pids_limit} processes and threads"
                argv = ["limits", self.run, "--pids", wanted]
            else:
                on, off = pids >= PIDS_RUNAWAY, pids < PIDS_RUNAWAY * PIDS_CLEAR
                advice = (f"{pids} processes and threads without a limit: a runaway? look with sandbox-ai usage "
                          f"{self.run}")
                argv = None
            results.append(self.condition(
                "pids", on, off, t, 0,
                lambda began: self.warning("pids", began, {"current": pids, "limit": pids_limit},
                                           f"{self.name} has {pids} processes and threads"
                                           + (f" of {pids_limit}" if pids_limit else ""), advice, argv)))
        for made, gone in results:
            if made is not None:
                started.append(made)
            if gone is not None:
                ended.append(gone)
        return started, ended


class HostAssessor(Assessor):
    """The host's warnings: low on memory, swapping."""

    def __init__(self):
        super().__init__("host", "this host")

    def feed(self, s):
        t = int(time.time() if number(s.get("t")) is None else s["t"])
        started, ended = [], []
        total, avail = number(s.get("mem_total")) or 0, number(s.get("mem_avail"))
        if total and avail is not None:
            made, gone = self.condition(
                "host-memory", avail < HOST_MEMORY_LOW * total, avail >= HOST_MEMORY_CLEAR * total, t, HOST_HOLD,
                lambda began: self.warning("host-memory", began, {"available": avail, "total": total},
                                           f"the host has {gib_text(avail)} of {gib_text(total)} GiB free",
                                           "stop idle runs, or start the next ones with a smaller --memory",
                                           ["top", "--sort", "mem"]))
            started += [made] if made else []
            ended += [gone] if gone else []
        swap, used, swap_in = number(s.get("swap_total")) or 0, number(s.get("swap_used")) or 0, \
            number(s.get("swap_in")) or 0
        if swap:
            made, gone = self.condition(
                "host-swap", used >= HOST_SWAP_USED * swap and swap_in >= HOST_SWAP_IN, swap_in < HOST_SWAP_IN / 5,
                t, HOST_SWAP_HOLD,
                lambda began: self.warning("host-swap", began, {"swap_used": used, "swap_total": swap,
                                                                "swap_in": swap_in},
                                           f"the host is swapping ({swap_in:.0f} pages a second; {gib_text(used)} of "
                                           f"{gib_text(swap)} GiB swap used)",
                                           "it runs slowly: stop idle runs or lower their --memory",
                                           ["top", "--sort", "mem"]))
            started += [made] if made else []
            ended += [gone] if gone else []
        return started, ended


# --- the collector (in the launcher) ---------------------------------------------------------------------------

class HostLock:
    """monitor/host.lock: the one launcher that writes the host's series holds it."""

    def __init__(self, root):
        self.root, self.fd = Path(root), None

    def take(self):
        if self.fd is not None:
            return True
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.root / "host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None


class Recorder:
    """The collector of one run (a thread of its launcher): every INTERVAL seconds READ() gives the run's counters
    (or None while it starts, or when it is gone); the sample goes into its series in DIRECTORY and its live record,
    its warnings to WARN(warning, started) - True when it starts, False when it ends. HEADER: what the series says
    about the run (run, name, agent, workspace, kind, ...). The host's series is written too while this recorder
    holds the host lock (HOST: a function giving the host's counters, None for none). GPU: a function giving the
    GPU's counters (every GPU_INTERVAL), or None."""

    def __init__(self, header, read, directory, interval=INTERVAL, warn=None, host=None, gpu=None, root=None,
                 host_cpus=None, clock=time.time, wait=None):
        self.header, self.read, self.interval = dict(header), read, interval
        self.run = str(header.get("run"))
        self.root = Path(root) if root is not None else monitor_root()
        self.series = Series(directory, self.run, header, interval)
        self.assessor = Assessor(self.run, header.get("name"), host_cpus, *idle_role(header))
        self.host_cpus, self.warn, self.host, self.gpu, self.clock = host_cpus, warn, host, gpu, clock
        self.before, self.spark, self.latest, self.source = None, [], None, None
        self.host_lock, self.host_before, self.host_series, self.host_assessor = HostLock(self.root), None, None, None
        self.host_tried, self.gpu_at, self.gpu_values = None, None, {}
        self.told = {}  # kind -> when it was last said
        self.cost, self.spent, self.cost_from, self.samples = 0.0, 0.0, clock(), 0
        self.stopping, self.thread = threading.Event(), None
        self.wait = wait or self.stopping.wait
        self.closed, self.closing = False, threading.Lock()  # (a look that comes back after stop() writes nothing)

    @property
    def live_path(self):
        return self.root / "live" / f"{self.run}.json"

    def tick(self):
        """One look: a sample, its warnings, the live record; the host's too when this recorder writes it."""
        begun = time.thread_time()
        try:
            counters = self.read()
            now = counters["time"] if counters is not None else None
            if now is not None and self.gpu is not None and (self.gpu_at is None or now - self.gpu_at >= GPU_INTERVAL):
                self.gpu_at = now  # (read before the lock: a slow GPU tool never holds up stop())
                with contextlib.suppress(Exception):
                    self.gpu_values = self.gpu() or {}
            with self.closing:
                if not self.closed:
                    self.look(counters)
        finally:
            spent = time.thread_time() - begun
            self.cost += spent
            self.spent += spent

    def look(self, counters):
        """What one read gave (COUNTERS, None while the run starts): its sample, warnings, live record, the host's."""
        if counters is not None:
            counters.update(self.gpu_values)
            self.source = counters.get("source")
            found = sample(self.before, counters, self.host_cpus)
            self.before = counters
            if found is not None:
                self.record(found)
        self.tick_host()

    def record(self, found):
        self.samples += 1
        if self.samples % 6 == 0:  # (every minute at 10 s: the collector's own CPU in its minute rollup)
            found["self_us"] = int(self.cost * 1e6)
            self.cost = 0.0
        self.series.write(found)
        self.latest = found
        self.spark.append([found["t"], found.get("cpu"), found.get("mem"),
                           (found.get("rd") or 0) + (found.get("wr") or 0),
                           (found.get("rx") or 0) + (found.get("tx") or 0)])
        del self.spark[:-SPARK]
        started, ended = self.assessor.feed(found)
        for warning in started:
            self.say(warning, True)
        for warning in ended:
            self.say(warning, False)
        self.write_live()

    def say(self, warning, started):
        self.series.event({"event": "warning" if started else "clear", "t": warning["since"] if started
                           else int(self.clock()), "kind": warning["kind"],
                           **({"text": warning["text"], "advice": warning["advice"], "values": warning["values"]}
                              if started else {})})
        if self.warn is None:
            return
        if started:
            last = self.told.get(warning["kind"])
            if last is not None and self.clock() - last < RENOTIFY:
                return  # (it came back soon: recorded, not said again)
            self.told[warning["kind"]] = self.clock()
        with contextlib.suppress(Exception):
            self.warn(warning, started)

    def write_live(self):
        write_json(self.live_path, {
            **{key: self.header.get(key) for key in ("run", "name", "agent", "workspace", "kind", "mcp_name",
                                                     "helper_of", "backend")},
            "updated": int(self.clock()), "interval": self.interval, "source": self.source, "sample": self.latest,
            "warnings": list(self.assessor.active.values()), "spark": self.spark,
            "collector": {"cpu_seconds": round(self.spent, 4), "seconds": round(self.clock() - self.cost_from, 1)}})

    def tick_host(self):
        if self.host is None:
            return
        now = self.clock()
        if self.host_lock.fd is None and self.host_tried is not None and now - self.host_tried < 60:
            return
        self.host_tried = now
        if not self.host_lock.take():
            return
        if self.host_series is None:
            # One file per tier whoever writes it (HOST_STAMP): each writer trims it, none is left behind.
            self.host_series = Series(self.root, HOST_FILE, {"run": HOST_FILE, "kind": "host",
                                                            "cpus": self.host_cpus}, self.interval, stamp=HOST_STAMP)
            self.host_assessor = HostAssessor()
        counters = self.host()
        found = host_sample(self.host_before, counters) if counters else None
        self.host_before = counters or self.host_before
        if found is None or "dt" not in found:
            return
        self.host_series.write(found)
        started, ended = self.host_assessor.feed(found)
        for warning in started + ended:
            self.host_series.event({"event": "warning" if warning in started else "clear", "t": found["t"],
                                    "kind": warning["kind"], "text": warning["text"]})
        for warning in started:
            self.say(warning, True)
        write_json(self.root / "live" / f"{HOST_FILE}.json",
                   {"run": HOST_FILE, "updated": int(now), "interval": self.interval, "sample": found,
                    "warnings": list(self.host_assessor.active.values()), "by": self.run})

    def loop(self):
        while True:
            with contextlib.suppress(Exception):  # a broken look never ends the launcher's watch of the run
                self.tick()
            if self.wait(self.interval):
                return

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="sandbox-ai-monitor", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        """The last rollups written, the live record and the host lock given up - after STOP_WAIT seconds at most for
        a look under way (one inside a slow `podman stats` comes back later and writes nothing: closed)."""
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(STOP_WAIT)
        with self.closing:
            self.closed = True
        self.series.close()
        if self.host_series is not None:
            self.host_series.close()
        self.host_lock.release()
        with contextlib.suppress(OSError):
            self.live_path.unlink()
        if self.host_series is not None:
            with contextlib.suppress(OSError):
                (self.root / "live" / f"{HOST_FILE}.json").unlink()


def prune(root=None, days=None, now=None, budget=256 << 20):
    """monitor/runs/ (the series of runs without a log folder): files older than DAYS days removed, then the
    oldest until all fit in BUDGET bytes; the live records of runs that ended long ago too. The removed paths."""
    root = Path(root) if root is not None else monitor_root()
    now = time.time() if now is None else now
    removed, kept = [], []
    for folder in (root / "runs", root / "live"):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            old = folder.name == "live" and now - info.st_mtime > 86400
            old = old or (days is not None and now - info.st_mtime > days * 86400)
            if old:
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
                    removed.append(entry.path)
            elif folder.name == "runs":
                kept.append((info.st_mtime, info.st_size, entry.path))
    total = sum(size for _, size, _ in kept)
    for _, size, path in sorted(kept):
        if total <= budget:
            break
        with contextlib.suppress(OSError):
            os.unlink(path)
            removed.append(path)
            total -= size
    return removed


# --- reading -----------------------------------------------------------------------------------------------------

def series_folders():
    """The folders that hold series: every project's log folder, and monitor/runs/."""
    folders = []
    with contextlib.suppress(OSError):
        folders = [path for path in sorted((state_root() / "logs").iterdir())
                   if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")]
    return folders + [monitor_root() / "runs"]


def series_files(folders=None, since=None):
    """{run: {kind: [paths]}} of the series files in FOLDERS (default series_folders()) written since SINCE."""
    found = {}
    for folder in folders if folders is not None else series_folders():
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            match = FILE_NAME.fullmatch(entry.name)
            if not match:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not entry.is_file(follow_symlinks=False) or (since is not None and info.st_mtime < since):
                continue
            found.setdefault(match.group(3), {}).setdefault(match.group(1), []).append(Path(entry.path))
    return found


TEXT_KEYS = ("run", "name", "agent", "workspace", "kind", "mcp_name", "helper_of", "backend", "event", "text", "advice",
             "started", "lead", "source", "project")
NUMBER_LIMIT = 1e15         # no measurement is larger (bytes, microseconds of a window), none is negative
POSITIVE_KEYS = ("dt", "n", "interval")  # what others are divided by or weighted with


def measured(key, value):
    """VALUE when it is a number a series may hold under KEY (number(): finite; 0 to NUMBER_LIMIT; more than 0 for
    POSITIVE_KEYS), else None."""
    if number(value) is None or not 0 <= value <= NUMBER_LIMIT or (key in POSITIVE_KEYS and value <= 0):
        return None
    return value


def clean_record(data):
    """A line of a series as data: its numbers (measured()), a few known texts (TEXT_KEYS: texts only - a number
    under one of them is left out, what reads "name" or "agent" gets a text or nothing), the values of a warning;
    None if it is none."""
    if not isinstance(data, dict):
        return None
    found = {}
    for key, value in list(data.items())[:80]:
        if not isinstance(key, str) or len(key) > 40:
            continue
        if key in TEXT_KEYS:
            if isinstance(value, str):
                found[key] = printable(value, 600)
        elif key == "mcp_servers":
            if isinstance(value, list):
                found[key] = [printable(item, 64) for item in value[:64] if isinstance(item, str)]
        elif key in ("values", "limits"):
            if isinstance(value, dict):
                found[key] = {k: v for k, v in list(value.items())[:20]
                              if isinstance(k, str) and measured(k, v) is not None}
        elif measured(key, value) is not None:
            found[key] = value
    return found


SAMPLE_START = b'{"t":'  # how the writer begins every sample and rollup (Series.write: "t" first, no spaces)


def sample_time(line):
    """The time of a sample LINE as the writer wrote it (SAMPLE_START, then the number), without parsing the rest;
    None for any other line (an event, a header, one written otherwise): that one is parsed."""
    if not line.startswith(SAMPLE_START):
        return None
    end = line.find(b",", len(SAMPLE_START), len(SAMPLE_START) + 24)
    try:
        return float(line[len(SAMPLE_START):end]) if end > 0 else None
    except ValueError:
        return None


def read_series(path, since=None, limit=READ_LIMIT, samples_since=None):
    """(header, samples, events) of a series file (compressed or not), at most LIMIT bytes of it; samples and
    events from SINCE on (samples from SAMPLES_SINCE on, when later: what a coarser tier does not have). The lines
    of samples before that are skipped unparsed (a day of samples is 8640 lines a run)."""
    header, samples, events = None, [], []
    opener = gzip.open if str(path).endswith(".gz") else open
    try:
        with opener(path, "rb") as stream:
            data = stream.read(limit)
    except (OSError, EOFError, gzip.BadGzipFile):
        return header, samples, events
    first = max(value for value in (since, samples_since, float("-inf")) if value is not None)
    for line in data.split(b"\n"):
        if not line or len(line) > LINE_LIMIT:
            continue
        if first != float("-inf"):
            when = sample_time(line)
            if when is not None and when < first:
                continue
        try:
            record = clean_record(json.loads(line))
        except (ValueError, RecursionError):
            continue
        if record is None:
            continue
        if record.get("header") == 1 and header is None:
            header = record
            continue
        if number(record.get("t")) is None or (since is not None and record["t"] < since):
            continue
        if "event" in record:
            events.append(record)
        elif samples_since is None or record["t"] >= samples_since:
            samples.append(record)
    return header, samples, events


def tier_for(seconds):
    """The tier read for a window of SECONDS: every sample up to 6 hours, minutes up to a week, else hours."""
    return "monitor" if seconds <= 6 * 3600 else "monitor-1m" if seconds <= 7 * 86400 else "monitor-1h"


def read_run(files, since, kind):
    """(header, samples, events) of one run's series files (FILES: {kind: [paths]}) from SINCE on: the tier KIND,
    and what a finer tier has after its last record (the hour or minute begun)."""
    order = [name for name, _, _ in TIERS]
    header, samples, events, last = None, [], [], None
    for tier in order[order.index(kind)::-1]:  # the coarsest wanted first, then finer ones for what follows
        for path in sorted(files.get(tier, ())):
            found_header, found, found_events = read_series(path, since, samples_since=last)
            header = header or found_header
            width = dict((name, width) for name, width, _ in TIERS)[tier]
            if last is not None:
                found = [entry for entry in found if entry["t"] >= last]
            samples += found
            events += found_events
            if found and width:
                last = max(entry["t"] for entry in found) + width
            elif found:
                last = max(entry["t"] for entry in found) + 1
    seen, unique = set(), []
    for event in sorted(events, key=lambda e: e["t"]):
        key = (event["t"], event.get("kind"), event.get("event"))
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return header, sorted(samples, key=lambda entry: entry["t"]), unique


def summarise(samples, interval=INTERVAL):
    """Averages, peaks and totals of SAMPLES (raw or rollups): seconds covered, CPU seconds and CPUs on average and
    at most, memory on average and at most, bytes read, written, received and sent, processes at most, how long it
    was idle while holding memory, OOM kills."""
    found = {"seconds": 0.0, "cpu_seconds": 0.0, "cpu_avg": 0.0, "cpu_max": 0.0, "mem_avg": 0, "mem_max": 0,
             "mem_lim": 0, "cpu_lim": 0, "read": 0, "written": 0, "received": 0, "sent": 0, "pids_max": 0,
             "idle_seconds": 0.0, "idle_memory": 0, "oom": 0, "throttled_seconds": 0.0, "io_pressure_max": 0.0,
             "first": None, "last": None, "self_cpu_seconds": 0.0}
    memory_seconds = idle_memory = 0.0
    for entry in samples:
        dt = number(entry.get("dt")) or interval * (number(entry.get("n")) or 1)
        found["seconds"] += dt
        cpu = number(entry.get("cpu")) or 0.0
        mem = number(entry.get("mem")) or 0
        found["cpu_seconds"] += cpu * dt
        found["cpu_max"] = max(found["cpu_max"], number(entry.get("cpu_max")) or cpu)
        memory_seconds += mem * dt
        found["mem_max"] = max(found["mem_max"], number(entry.get("mem_max")) or mem, number(entry.get("peak")) or 0)
        found["mem_lim"] = number(entry.get("mem_lim")) or found["mem_lim"]
        found["cpu_lim"] = number(entry.get("cpu_lim")) or found["cpu_lim"]
        for key, out in (("rd", "read"), ("wr", "written"), ("rx", "received"), ("tx", "sent")):
            found[out] += int((number(entry.get(key)) or 0) * dt)
        found["pids_max"] = max(found["pids_max"], int(number(entry.get("pids_max")) or number(entry.get("pids")) or 0))
        found["oom"] += int(number(entry.get("oom")) or 0)
        found["throttled_seconds"] += (number(entry.get("thr")) or 0) / 100 * dt
        found["io_pressure_max"] = max(found["io_pressure_max"], number(entry.get("psi_io_max"))
                                       or number(entry.get("psi_io")) or 0.0)
        found["self_cpu_seconds"] += (number(entry.get("self_us")) or 0) / 1e6
        if cpu < IDLE_CPUS and mem >= IDLE_MEMORY:
            found["idle_seconds"] += dt
            idle_memory += mem * dt
        t = number(entry.get("t"))
        if t is not None:
            found["first"] = t if found["first"] is None else min(found["first"], t)
            found["last"] = t if found["last"] is None else max(found["last"], t)
    if found["seconds"]:
        found["cpu_avg"] = round(found["cpu_seconds"] / found["seconds"], 3)
        found["mem_avg"] = int(memory_seconds / found["seconds"])
    if found["idle_seconds"]:
        found["idle_memory"] = int(idle_memory / found["idle_seconds"])
    for key in ("seconds", "cpu_seconds", "idle_seconds", "throttled_seconds", "self_cpu_seconds"):
        found[key] = round(found[key], 1 if key != "self_cpu_seconds" else 4)
    return found


def downsample(samples, points=SERIES_POINTS):
    """[[t, cpu, mem, io bytes/s, net bytes/s], ...] of SAMPLES, at most POINTS (averages of neighbours; memory at
    its highest)."""
    rows = [[int(e["t"]), number(e.get("cpu")) or 0.0, number(e.get("mem")) or 0,
             (number(e.get("rd")) or 0) + (number(e.get("wr")) or 0),
             (number(e.get("rx")) or 0) + (number(e.get("tx")) or 0)] for e in samples]
    if len(rows) <= points:
        return rows
    size, out = len(rows) / points, []
    for index in range(points):
        chunk = rows[int(index * size):max(int((index + 1) * size), int(index * size) + 1)]
        out.append([chunk[-1][0], round(sum(r[1] for r in chunk) / len(chunk), 3), max(r[2] for r in chunk),
                    int(sum(r[3] for r in chunk) / len(chunk)), int(sum(r[4] for r in chunk) / len(chunk))])
    return out


def host_downsample(samples, points=SERIES_POINTS):
    rows = [[int(e["t"]), number(e.get("cpu_pct")) or 0.0, number(e.get("mem_avail")) or 0,
             (number(e.get("rd")) or 0) + (number(e.get("wr")) or 0), number(e.get("load1")) or 0.0] for e in samples]
    if len(rows) <= points:
        return rows
    size, out = len(rows) / points, []
    for index in range(points):
        chunk = rows[int(index * size):max(int((index + 1) * size), int(index * size) + 1)]
        out.append([chunk[-1][0], round(sum(r[1] for r in chunk) / len(chunk), 1), min(r[2] for r in chunk),
                    int(sum(r[3] for r in chunk) / len(chunk)), round(max(r[4] for r in chunk), 2)])
    return out


# The detail of a series (`stats usage --series`: the charts of the app and the web): per time slot each KEY, taken
# as its average, its highest, its lowest or its last value in the slot (a rollup's KEY_max for "max").
DETAIL_KEYS = {"cpu": "avg", "cpu_pct": "avg", "thr": "avg", "cpu_lim": "last", "mem": "max", "mem_lim": "last",
               "rd": "avg", "wr": "avg", "rx": "avg", "tx": "avg", "pids": "max", "pids_lim": "last"}
HOST_DETAIL_KEYS = {"cpu_pct": "avg", "cpus": "last", "load1": "max", "mem_total": "last", "mem_avail": "min",
                    "swap_total": "last", "swap_used": "max", "rd": "avg", "wr": "avg", "rx": "avg", "tx": "avg",
                    "pids": "max"}


def detail(samples, keys, since, until, points=SERIES_POINTS):
    """{"t": [...], KEY: [...]} of SAMPLES in at most POINTS slots of the window SINCE..UNTIL (aligned to it: every
    series of the window has the same slots), each KEY as KEYS says (avg, max, min, last; None without a value); t is
    a slot's end (UNTIL at most): the same for every series of the window. A slot without a sample is left out: a
    gap."""
    width = max(1, math.ceil((until - since) / max(1, points)))
    slots = {}
    for entry in samples:
        when = number(entry.get("t"))
        if when is None or not since <= when <= until:
            continue
        slot = slots.setdefault(int((when - since) // width), {"n": {}, "v": {}})
        for key, how in keys.items():
            value = number(entry.get(f"{key}_max")) if how == "max" else None
            value = number(entry.get(key)) if value is None else value
            if value is None:
                continue
            had = slot["v"].get(key)
            if had is None or how == "last":
                slot["v"][key] = value
            elif how == "avg":
                slot["v"][key] = had + value
            else:
                slot["v"][key] = max(had, value) if how == "max" else min(had, value)
            slot["n"][key] = slot["n"].get(key, 0) + 1
    found = {"t": [], **{key: [] for key in keys}}
    for index in sorted(slots)[-points:]:
        slot = slots[index]
        found["t"].append(int(min(until, since + (index + 1) * width)))
        for key, how in keys.items():
            value = slot["v"].get(key)
            if value is not None and how == "avg":
                value = round(value / slot["n"][key], 3)
                value = int(value) if value > 1000 else value
            found[key].append(value)
    return found


SINCE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([smhdw]?)")


def since_seconds(text):
    """Seconds of a window such as 30m, 24h, 7d, 2w (a bare number: hours)."""
    match = SINCE.fullmatch(str(text).strip().lower())
    if not match:
        raise ValueError(f"--since expects a time such as 30m, 24h, 7d or 2w: {text!r}")
    value = float(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 3600}[match.group(2)]
    if not 60 <= value <= 366 * 86400:
        raise ValueError("--since expects a time from 1 minute to a year")
    return value


def project_of(path):
    """The project folder a workspace path names (as written; "" for none)."""
    return printable(path, 4096) if isinstance(path, str) else ""


def attribute(rows):
    """Each row ({"run", "kind", "mcp_name", "mcp_servers", "helper_of", "workspace", "first", "last", ...}) gets
    "parent" (the name of the start an MCP server or a helper serves, else None) and "project" (its parent's
    workspace, else its own)."""
    by_run = {row["run"]: row for row in rows}
    agents = [row for row in rows if row.get("kind") != "mcp"]
    for row in rows:
        parent = None
        if row.get("helper_of") and row["helper_of"] in by_run:
            parent = by_run[row["helper_of"]]
        elif row.get("kind") == "mcp" and row.get("mcp_name"):
            users = [other for other in agents if row["mcp_name"] in (other.get("mcp_servers") or [])]
            overlapping = [other for other in users if (other.get("first") or 0) <= (row.get("last") or 0) + 60
                           and (other.get("last") or 0) + 60 >= (row.get("first") or 0)]
            parent = (overlapping or users or [None])[0]
        row["parent"] = parent.get("name") or parent["run"] if parent is not None and parent is not row else None
        row["project"] = project_of((parent or row).get("workspace") if parent is not row else row.get("workspace"))
    return rows


def usage(since_text="7d", by="project", project=None, series=False, now=None, folders=None, host_root=None):
    """What `stats usage --json` prints: per run and per group (BY project, run or agent) the averages, peaks and
    totals of the window SINCE_TEXT, the top consumers, the runs that were idle while holding memory, the warnings
    of the window, the host's, and what the collector itself cost."""
    now = time.time() if now is None else now
    window = since_seconds(since_text)
    since = now - window
    kind = tier_for(window)
    rows = []
    for run, files in sorted(series_files(folders, since).items()):
        if run == HOST_FILE:
            continue
        header, samples, events = read_run(files, since, kind)
        if header is None and not samples:
            continue
        header = header or {}
        summary = summarise(samples, number(header.get("interval")) or INTERVAL)
        row = {"run": run, "name": header.get("name") or run, "agent": header.get("agent"),
               "kind": header.get("kind") or "agent", "workspace": header.get("workspace"),
               "mcp_name": header.get("mcp_name"), "mcp_servers": header.get("mcp_servers") or [],
               "helper_of": header.get("helper_of"), "started": header.get("started"), **summary,
               "warnings": [{key: event.get(key) for key in ("t", "kind", "text", "advice", "values")}
                            for event in events if event.get("event") == "warning"][-20:]}
        if series:
            row["series"] = downsample(samples)
            row["detail"] = detail(samples, DETAIL_KEYS, since, now)
        rows.append(row)
    attribute(rows)
    if project:
        wanted = os.path.realpath(os.path.expanduser(project))
        rows = [row for row in rows if row.get("project") and os.path.realpath(row["project"]) == wanted]
    groups = {}
    for row in rows:
        key = {"project": row.get("project") or "(none)", "agent": row.get("agent") or "(none)"}.get(by, row["run"])
        group = groups.setdefault(key, {"key": key, "runs": 0, "seconds": 0.0, "cpu_seconds": 0.0, "cpu_max": 0.0,
                                        "mem_max": 0, "memory_seconds": 0.0, "read": 0, "written": 0, "received": 0,
                                        "sent": 0, "warnings": 0, "idle_seconds": 0.0, "oom": 0})
        group["runs"] += 1
        for name in ("seconds", "cpu_seconds", "read", "written", "received", "sent", "idle_seconds", "oom"):
            group[name] += row[name]
        group["memory_seconds"] += row["mem_avg"] * row["seconds"]
        group["cpu_max"] = max(group["cpu_max"], row["cpu_max"])
        group["mem_max"] = max(group["mem_max"], row["mem_max"])
        group["warnings"] += len(row["warnings"])
        if series:
            group.setdefault("_series", []).append(row["series"])
    for group in groups.values():
        span = window
        group["cpu_avg"] = round(group["cpu_seconds"] / span, 3)  # CPUs over the window (runs at once add up)
        group["mem_avg"] = int(group.pop("memory_seconds") / span)
        group["cpu_seconds"], group["seconds"] = round(group["cpu_seconds"], 1), round(group["seconds"], 1)
        if series:
            group["series"] = merge_series(group.pop("_series"))
    ordered = sorted(groups.values(), key=lambda g: (-g["cpu_seconds"], -g["mem_max"], g["key"]))
    top = sorted(rows, key=lambda r: (-r["cpu_seconds"], -r["mem_max"], r["run"]))[:10]
    idle = sorted([{"run": r["run"], "name": r["name"], "project": r["project"], "seconds": r["idle_seconds"],
                    "memory": r["idle_memory"]} for r in rows if r["idle_seconds"] >= IDLE_HOLD],
                  key=lambda r: -r["memory"] * r["seconds"])
    host = host_usage(since, kind, series, host_root, until=now)
    collector = {"cpu_seconds": round(sum(r["self_cpu_seconds"] for r in rows), 4),
                 "seconds": round(sum(r["seconds"] for r in rows), 1)}
    collector["percent_of_a_cpu"] = round(100 * collector["cpu_seconds"] / collector["seconds"], 4) \
        if collector["seconds"] else None
    return {"since": int(since), "until": int(now), "window": since_text, "tier": kind, "by": by, "groups": ordered,
            "runs": rows, "top": [{"run": r["run"], "name": r["name"], "project": r["project"],
                                   "cpu_seconds": r["cpu_seconds"], "mem_max": r["mem_max"]} for r in top],
            "idle": idle, "warnings": sorted([{**w, "run": r["run"], "name": r["name"]} for r in rows
                                              for w in r["warnings"]], key=lambda w: w.get("t") or 0)[-50:],
            "host": host, "collector": collector, "spend": spend_of(since, now, by, project)}


def spend_of(since, until, by="project", project=None):
    """What the model requests of the window cost (spend.py, the proxy's spend logs): {"total", "groups": [{"key",
    "requests", tokens, "usd", "unknown", "subscription", "local"}] BY project, run or agent, "runs": {run: {"usd",
    "requests", "unknown", "subscription", "local"}}}; None when the logs cannot be read."""
    try:
        spend = module("spend")
        runs = spend.collect(since=since, until=until, extra=spend.running_log_dirs())  # (--log-dir: their own)
        if project:
            key = spend.project_key(project)
            runs = [found for found in runs if found["run"].get("project_key") == key]
        table = spend.price_table()
        groups = spend.rows_of(runs, by if by in spend.BY else "project", table)
        per_run = {row["key"]: {key: row[key] for key in ("usd", "requests", "unknown", "subscription", "local")}
                   for row in spend.rows_of(runs, "run", table)}
        return {"total": spend.totals(groups), "groups": groups, "runs": per_run}
    except Exception:  # noqa: BLE001 - the rest of the statistics goes on without it
        return None


def merge_series(series_list):
    """Several runs' downsampled series as one (summed per time slot): a group's."""
    slots = {}
    for rows in series_list:
        for t, cpu, mem, io, net in rows:
            slot = slots.setdefault(t, [t, 0.0, 0, 0, 0])
            slot[1] = round(slot[1] + cpu, 3)
            slot[2] += mem
            slot[3] += io
            slot[4] += net
    return downsample_rows(sorted(slots.values()))


def downsample_rows(rows, points=SERIES_POINTS):
    if len(rows) <= points:
        return rows
    size, out = len(rows) / points, []
    for index in range(points):
        chunk = rows[int(index * size):max(int((index + 1) * size), int(index * size) + 1)]
        out.append([chunk[-1][0], round(sum(r[1] for r in chunk) / len(chunk), 3), max(r[2] for r in chunk),
                    int(sum(r[3] for r in chunk) / len(chunk)), int(sum(r[4] for r in chunk) / len(chunk))])
    return out


def host_usage(since, kind, series=False, root=None, until=None):
    """The host's window: CPU busy on average and at most, load at most, the least free memory, swapping, disk IO,
    and its warnings (None without a host series)."""
    root = Path(root) if root is not None else monitor_root()
    files = series_files([root], since).get(HOST_FILE)
    if not files:
        return None
    _, samples, events = read_run(files, since, kind)
    if not samples:
        return None
    seconds = sum(number(e.get("dt")) or INTERVAL for e in samples) or 1
    found = {"seconds": round(seconds, 1),
             "cpu_pct_avg": round(sum((number(e.get("cpu_pct")) or 0) * (number(e.get("dt")) or INTERVAL)
                                      for e in samples) / seconds, 1),
             "cpu_pct_max": max(number(e.get("cpu_pct_max")) or number(e.get("cpu_pct")) or 0 for e in samples),
             "load_max": max(number(e.get("load1_max")) or number(e.get("load1")) or 0 for e in samples),
             "mem_total": max(number(e.get("mem_total")) or 0 for e in samples),
             "mem_avail_min": min(number(e.get("mem_avail")) or 0 for e in samples),
             "swap_in_max": max(number(e.get("swap_in_max")) or number(e.get("swap_in")) or 0 for e in samples),
             "read": int(sum((number(e.get("rd")) or 0) * (number(e.get("dt")) or INTERVAL) for e in samples)),
             "written": int(sum((number(e.get("wr")) or 0) * (number(e.get("dt")) or INTERVAL) for e in samples)),
             "warnings": [{key: e.get(key) for key in ("t", "kind", "text")} for e in events
                          if e.get("event") == "warning"][-20:]}
    if series:
        found["series"] = host_downsample(samples)
        found["detail"] = detail(samples, HOST_DETAIL_KEYS, since, time.time() if until is None else until)
    return found


# --- the live view (top) -------------------------------------------------------------------------------------

def live_record(run, root=None, now=None):
    """The live record of RUN (monitor/live/RUN.json, its recorder's) when it is fresh, else None."""
    root = Path(root) if root is not None else monitor_root()
    path = root / "live" / f"{run}.json"
    text = read_text(path, 1 << 20)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    now = time.time() if now is None else now
    interval = number(data.get("interval")) or INTERVAL
    updated = number(data.get("updated"))
    if updated is None or now - updated > LIVE_FRESH * interval + 5:
        return None
    found = {"updated": updated, "interval": interval, "source": printable(data.get("source"), 20) or None,
             "sample": clean_record(data.get("sample")) or {}, "warnings": [], "spark": []}
    for warning in (data.get("warnings") or [])[:20] if isinstance(data.get("warnings"), list) else []:
        cleaned = clean_record(warning)
        if cleaned and cleaned.get("kind") in KINDS:
            argv = warning.get("argv") if isinstance(warning, dict) else None
            cleaned["argv"] = [printable(word, 200) for word in argv[:8]] \
                if isinstance(argv, list) and all(isinstance(word, str) for word in argv) else None
            cleaned["level"] = KINDS[cleaned["kind"]][0]
            found["warnings"].append(cleaned)
    for row in (data.get("spark") or [])[-SPARK:] if isinstance(data.get("spark"), list) else []:
        if isinstance(row, list) and len(row) == 5:
            found["spark"].append([number(value) or 0 for value in row])
    collector = data.get("collector") if isinstance(data.get("collector"), dict) else {}
    found["collector"] = {key: number(collector.get(key)) for key in ("cpu_seconds", "seconds")}
    return found


def measure_live(records, interval=1.0, sleep=time.sleep, proc=PROC, root=CGROUP_ROOT, podman=None):
    """{run: sample} of RECORDS that have no fresh live record, measured now over INTERVAL: their cgroup where this
    host sees it (usage.py finds it), else one `podman stats` for those with Podman (PODMAN: its command prefix)."""
    first, found = {}, {}
    usage_module = None
    for record in records:
        try:
            usage_module = usage_module or module("usage")
            cgroup, pids = usage_module.look(record, root=root, proc=proc)
        except Exception:  # noqa: BLE001 - not measured here
            cgroup, pids = None, []
        if cgroup is None:
            continue
        top = sandbox_top(cgroup) or Path(cgroup)
        pid = pids[0] if pids else None
        counters = cgroup_counters(top, cgroup, pid, proc)
        if counters is not None:
            first[record["run"]] = (top, cgroup, pid, counters)
    remote = [record for record in records if record["run"] not in first and record.get("backend") == "podman"]
    stats_before = podman_stats(podman) if remote and podman is not None else ""
    if first or stats_before:
        sleep(interval)
    for run, (top, cgroup, pid, before) in first.items():
        found[run] = sample(before, cgroup_counters(top, cgroup, pid, proc))
    if stats_before:
        stats_after = podman_stats(podman)
        for record in remote:
            prefix = f"{record.get('pod') or 'agent-' + record['run']}-"
            found[record["run"]] = sample(stats_counters(stats_before, prefix), stats_counters(stats_after, prefix))
    return {run: value for run, value in found.items() if value is not None}


def top_rows(records, now=None, root=None, measure=True, sleep=time.sleep, podman=None):
    """The rows of `top`: per running run its newest sample (from its recorder, else measured now), its warnings,
    its spark (the last samples), project and parent; and the host's live record."""
    rows, missing = [], []
    for record in records:
        live = live_record(record["run"], root, now)
        row = {"run": record["run"], "name": printable(record.get("name") or record["run"], 128),
               "agent": printable(record.get("agent"), 40) or None, "kind": record.get("kind") or "agent",
               "workspace": record.get("workspace") if isinstance(record.get("workspace"), str) else None,
               "mcp_name": record.get("mcp_name") if isinstance(record.get("mcp_name"), str) else None,
               "mcp_servers": [s for s in record.get("mcp_servers") or [] if isinstance(s, str)],
               "helper_of": record.get("helper_of") if isinstance(record.get("helper_of"), str) else None,
               "backend": record.get("backend"), "orphaned": record.get("orphaned") is True,
               "sample": {}, "warnings": [], "spark": [], "source": None, "first": 0, "last": 0}
        if live is not None:
            row.update(sample=live["sample"], warnings=live["warnings"], spark=live["spark"], source=live["source"],
                       collector=live["collector"], interval=live["interval"])
        else:
            missing.append(record)
        rows.append(row)
    if measure and missing:
        measured = measure_live(missing, sleep=sleep, podman=podman)
        for row in rows:
            if row["run"] in measured:
                row["sample"], row["source"] = measured[row["run"]], "measured"
    attribute(rows)
    for row in rows:
        row.pop("first", None)
        row.pop("last", None)
    host = live_record(HOST_FILE, root, now)
    if host is None and measure:
        before = host_counters()
        if before is not None:
            sleep(0.5)
            host = {"sample": host_sample(before, host_counters()) or {}, "warnings": []}
    return rows, host


SORT_KEYS = {"cpu": lambda s: s.get("cpu") or 0, "mem": lambda s: s.get("mem") or 0,
             "io": lambda s: (s.get("rd") or 0) + (s.get("wr") or 0),
             "net": lambda s: (s.get("rx") or 0) + (s.get("tx") or 0), "pids": lambda s: s.get("pids") or 0}


def size_text(value, per_second=False):
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            text = f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            return text + ("/s" if per_second else "")
        value /= 1024
    return ""


def top_table(rows, host):
    """The text of `top`: the host's line, then one row per run (heaviest first, as sorted)."""
    lines = []
    if host and host.get("sample"):
        s = host["sample"]
        parts = []
        if s.get("cpu_pct") is not None:
            parts.append(f"CPU {s['cpu_pct']:g} % of {s.get('cpus') or '?'}")
        if s.get("load1") is not None:
            parts.append(f"load {s['load1']:g}")
        if s.get("mem_total"):
            parts.append(f"memory free {gib_text(s.get('mem_avail'))} of {gib_text(s['mem_total'])} GiB")
        if s.get("swap_total"):
            parts.append(f"swap {gib_text(s.get('swap_used'))} of {gib_text(s['swap_total'])} GiB")
        if s.get("rd") is not None:
            parts.append(f"disk {size_text(s.get('rd'), True)} read, {size_text(s.get('wr'), True)} written")
        lines.append("host: " + ", ".join(parts))
        for warning in host.get("warnings") or []:
            lines.append(f"  warning: {printable(warning.get('text'))}")
    if not rows:
        lines.append("no sandbox is running")
        return "\n".join(lines)
    header = f"{'RUN':<24} {'PROJECT':<18} {'AGENT':<9} {'CPU':>12} {'MEMORY':>17} {'IO r/w':>19} " \
             f"{'NET in/out':>19} {'PIDS':>6}  WARNINGS"
    lines.append(header)
    for row in rows:
        s = row["sample"] or {}
        name = row["name"] + (f" <{row['parent']}" if row.get("parent") else "")
        project = os.path.basename(row.get("project") or "") or "-"
        cpu = f"{s['cpu']:.2f} {s.get('cpu_pct', 0):>3.0f}%" if s.get("cpu") is not None else "-"
        mem = f"{gib_text(s.get('mem'))}/{gib_text(s['mem_lim'])} GiB" if s.get("mem_lim") else \
            f"{gib_text(s.get('mem'))} GiB" if s.get("mem") is not None else "-"
        io = f"{size_text(s.get('rd'))}/{size_text(s.get('wr'))}" if s.get("rd") is not None else "-"
        net = f"{size_text(s.get('rx'))}/{size_text(s.get('tx'))}" if s.get("rx") is not None else "-"
        warnings = ",".join(w["kind"] for w in row["warnings"]) or ""
        lines.append(f"{name[:24]:<24} {project[:18]:<18} {(row.get('agent') or '-')[:9]:<9} {cpu:>12} {mem:>17} "
                     f"{io:>19} {net:>19} {s.get('pids', '-')!s:>6}  {warnings}")
    for row in rows:
        for warning in row["warnings"]:
            if warning.get("advice"):
                lines.append(f"  {row['name']}: {printable(warning['advice'], 400)}")
    return "\n".join(lines)


def running_records():
    """The running runs (and the orphaned ones, marked) as `sandbox-ai net` knows them."""
    net = module("net")
    orphans = {}
    runs = net.running(orphans)
    return [{**record, "run": run} for run, record in {**orphans, **runs}.items()
            if isinstance(run, str) and isinstance(record, dict) and RUN_ID.fullmatch(run)]


def podman_prefix():
    """The command that reaches this user's Podman (its remote socket with SANDBOX_AI_PODMAN_URL), or None."""
    if not shutil.which("podman"):
        return None
    url = os.environ.get("SANDBOX_AI_PODMAN_URL")
    return ["podman", *(["--remote", "--url", url] if url else [])]


def top_data(project=None, sort="cpu", measure=True, sleep=time.sleep):
    records = [record for record in running_records()]
    rows, host = top_rows(records, measure=measure, sleep=sleep, podman=podman_prefix())
    if project:
        wanted = os.path.realpath(os.path.expanduser(project))
        rows = [row for row in rows if row.get("project") and os.path.realpath(row["project"]) == wanted]
    key = SORT_KEYS[sort]
    rows.sort(key=lambda row: (-key(row["sample"] or {}), row["name"]))
    warnings = [w for row in rows for w in row["warnings"]] + list((host or {}).get("warnings") or [])
    return {"time": int(time.time()), "sort": sort, "runs": rows, "host": host, "warnings": warnings}


def on_host(host, argv, out=print, err=None):
    """`sandbox-ai ARGV` on host HOST (sandbox-ai hosts): its output; its exit status."""
    status, stdout, stderr = module("hosts").run(host, argv, timeout=120)
    if stdout:
        out(stdout.rstrip("\n"))
    if stderr:
        print(stderr.rstrip("\n"), file=err or sys.stderr)
    return status


def resolved_options(parser, argv):
    """ARGV with each abbreviated long option (--hos, --js=...) spelled out as PARSER took it (argparse's own prefix
    matching; a subcommand's options after its name): what goes to another host is never read differently there,
    and --host is found by without_host. Nothing after "--" is touched."""
    found, current = [], parser
    for index, word in enumerate(argv):
        if word == "--":
            return found + list(argv[index:])
        choices = next((action.choices for action in current._actions
                        if isinstance(action, argparse._SubParsersAction)), None)
        if choices and word in choices:
            current = choices[word]
        elif word.startswith("--") and len(word) > 2:
            name, equals, value = word.partition("=")
            if name not in current._option_string_actions:
                matches = [option for option in current._option_string_actions if option.startswith(name)
                           and option.startswith("--")]
                if len(matches) == 1:
                    word = matches[0] + equals + value
        found.append(word)
    return found


def without_host(argv):
    found, index = [], 0
    while index < len(argv):
        if argv[index] == "--host":
            index += 2
            continue
        if argv[index].startswith("--host="):
            index += 1
            continue
        found.append(argv[index])
        index += 1
    return found


def top_main(argv, out=print, sleep=time.sleep, clear=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai top", description="What the running sandboxes use now: CPU, "
                                     "memory (of the limit), disk IO, network, processes and their warnings, the "
                                     "heaviest first, with the host's totals.")
    parser.add_argument("--host", metavar="HOST", help="on a host of sandbox-ai hosts (over SSH)")
    parser.add_argument("--project", metavar="DIR", help="only the runs of this project (MCP servers and helpers of "
                        "its starts included)")
    parser.add_argument("--sort", choices=sorted(SORT_KEYS), default="cpu", help="what the heaviest is (default cpu)")
    parser.add_argument("--json", action="store_true", help="as JSON (with --watch: one line per look)")
    parser.add_argument("--watch", nargs="?", type=float, const=5.0, metavar="S",
                        help="again every S seconds (default 5) until Ctrl-C")
    parser.add_argument("--no-measure", action="store_true", help="only what the collectors recorded (no measuring "
                        "of runs without a fresh record)")
    args = parser.parse_args(argv)
    if args.watch is not None and not 1 <= args.watch <= 3600:
        parser.error("--watch expects 1 to 3600 seconds")
    if args.host and args.host != "local":
        return on_host(args.host, ["top", *without_host(resolved_options(parser, argv))], out)
    clear = clear if clear is not None else (lambda: out("\033[H\033[2J") if sys.stdout.isatty() else None)
    try:
        while True:
            data = top_data(args.project, args.sort, not args.no_measure, sleep)
            if args.json:
                out(json.dumps(data) if args.watch else json.dumps(data, indent=2))
            else:
                if args.watch:
                    clear()
                out(top_table(data["runs"], data["host"]))
            if args.watch is None:
                return 0
            with contextlib.suppress(OSError, ValueError):
                sys.stdout.flush()  # (a pipe: each look as it comes)
            sleep(args.watch)
    except KeyboardInterrupt:
        return 0


def usage_text(data):
    lines = [f"usage since {time.strftime('%Y-%m-%d %H:%M', time.localtime(data['since']))} ({data['window']}), "
             f"by {data['by']}:"]
    if not data["groups"]:
        lines.append("  nothing recorded in this window")
    for group in data["groups"]:
        lines.append(f"  {printable(group['key'], 80)}: {group['runs']} run{'s' if group['runs'] != 1 else ''}, "
                     f"CPU {group['cpu_seconds'] / 3600:.2f} CPU-hours (on average {group['cpu_avg']:g} CPUs, at most "
                     f"{group['cpu_max']:g}), memory on average {gib_text(group['mem_avg'])} GiB, at most "
                     f"{gib_text(group['mem_max'])} GiB, disk {size_text(group['read'])} read, "
                     f"{size_text(group['written'])} written, network {size_text(group['received'])} in, "
                     f"{size_text(group['sent'])} out" + (f", {group['warnings']} warnings" if group['warnings'] else ""))
    if data["top"]:
        lines.append("top consumers (CPU):")
        for row in data["top"][:5]:
            lines.append(f"  {printable(row['name'], 60)} ({os.path.basename(row['project'] or '') or '-'}): "
                         f"{row['cpu_seconds'] / 3600:.2f} CPU-hours, memory at most {gib_text(row['mem_max'])} GiB")
    if data["idle"]:
        lines.append("idle while holding memory:")
        for row in data["idle"][:10]:
            lines.append(f"  {printable(row['name'], 60)}: {gib_text(row['memory'])} GiB for "
                         f"{duration_text(row['seconds'])}")
    if data["warnings"]:
        lines.append("warnings:")
        for warning in data["warnings"][-10:]:
            when = time.strftime("%m-%d %H:%M", time.localtime(warning.get("t") or 0))
            lines.append(f"  {when} {printable(warning.get('name'), 60)}: {printable(warning.get('text'), 300)}"
                         + (f" - {printable(warning['advice'], 300)}" if warning.get("advice") else ""))
    host = data.get("host")
    if host:
        lines.append(f"host: CPU busy {host['cpu_pct_avg']:g} % on average ({host['cpu_pct_max']:g} % at most), load "
                     f"at most {host['load_max']:g}, free memory at least {gib_text(host['mem_avail_min'])} of "
                     f"{gib_text(host['mem_total'])} GiB")
    spent = data.get("spend")
    if spent and spent["groups"]:
        lines.append(f"model requests (sandbox-ai spend), by {data['by']}:")
        spend = module("spend")
        for group in spent["groups"][:20]:
            lines.append(f"  {printable(group['key'], 80)}: {spend.cost_text(group)} - {group['requests']} requests, "
                         f"input {spend.tokens_text(group['in'] + group['cached'] + group['write'])} (cached "
                         f"{spend.tokens_text(group['cached'])}), output {spend.tokens_text(group['out'])}")
        lines.append(f"  total: {spend.cost_text(spent['total'])}")
    collector = data["collector"]
    if collector.get("percent_of_a_cpu") is not None:
        lines.append(f"the collector itself: {collector['percent_of_a_cpu']:.3f} % of one CPU")
    return "\n".join(lines)


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def stats_main(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai stats", description="What the runs used over time: CPU, "
                                     "memory, disk IO and network by project, run or agent, from the series the "
                                     "system monitor keeps (sandbox-ai top shows the runs now).",
                                     formatter_class=help_style)
    sub = parser.add_subparsers(dest="command", required=True)
    used = sub.add_parser("usage", help="averages, peaks, totals; top consumers, idle runs, warnings",
                          description="Per project, run or agent: CPU, memory, disk IO and network on average, at "
                                      "most and in total; the top consumers; the runs that were idle while holding "
                                      "memory; the warnings of the window; the host's totals.")
    used.add_argument("--since", default="7d", metavar="TIME", help="the window: 30m, 24h, 7d (default), 30d")
    used.add_argument("--by", choices=("project", "run", "agent"), default="project",
                      help="what the runs are grouped by (default project)")
    used.add_argument("--project", metavar="DIR", help="only this project's runs")
    used.add_argument("--host", metavar="HOST", help="on a host of sandbox-ai hosts (over SSH)")
    used.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    used.add_argument("--series", action="store_true", help="with --json: each run's and group's series (at most "
                      f"{SERIES_POINTS} points: time, CPUs, memory, IO and network bytes a second), and each run's "
                      "and the host's detail for charts (CPUs and their limit, memory and its limit, read, written, "
                      "received, sent, processes and their limit)")
    args = parser.parse_args(argv)
    if args.host and args.host != "local":
        return on_host(args.host, ["stats", *without_host(resolved_options(parser, argv))], out)
    try:
        data = usage(args.since, args.by, args.project, args.series)
    except ValueError as exc:
        print(f"sandbox-ai stats: {printable(exc, 400)}", file=sys.stderr)
        return 2
    out(json.dumps(data) if args.json else usage_text(data))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv.pop(0) if argv else "top"
    if command == "top":
        return top_main(argv)
    if command == "stats":
        return stats_main(argv)
    print("usage: monitor.py top|stats ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
