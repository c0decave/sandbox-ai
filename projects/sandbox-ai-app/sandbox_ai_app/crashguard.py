"""What is known of the app's last run when it ended without quitting: a crash Python saw is in app.log already
(faulthandler, the exception hooks), but a process killed from outside - the kernel's out-of-memory killer, a
SIGKILL - leaves nothing there. So the running app keeps a small record (app-run.json beside app.log: its PID, when it
started, its versions, and every TICK seconds its memory and the page shown); quitting removes it. A record left by
a process that is gone is an unexpected end: the next start says so in the log window and app.log, with what the
record knew and, where the kernel's log can be read, whether the out-of-memory killer ended it; a crash that left its
lines in the log (faulthandler's, Chromium's) is quoted from there - app.log, or app.log.1, where the start script
moved the last run's log before this one began."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from . import applog

TICK = 30  # seconds between the updates of the record while the app runs
WARN_MB = 2048  # the app using more than this is said once in the log (a leak shows long before the kernel kills)
FILE = "app-run.json"


def record_path(environ=None):
    return applog.log_path(environ).with_name(FILE)


def memory_mb(pid=None):
    """The resident memory of process PID (this one) in MiB, or None (no /proc: macOS)."""
    try:
        status = Path(f"/proc/{pid or 'self'}/status").read_text()
    except OSError:
        return None
    found = re.search(r"^VmRSS:\s+(\d+)\s+kB", status, re.M)
    return round(int(found.group(1)) / 1024) if found else None


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True  # (someone else's process: not ours, but not proven gone either)
    return True


def write_record(path, record):
    """RECORD into PATH: private (0600), atomic, never through a link. Never raises."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, path)
    except OSError:
        pass


def read_record(path):
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return None
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get("pid"), int) else None


def oom_lines(text, pid):
    """The lines of a kernel log TEXT that say the out-of-memory killer ended process PID."""
    pattern = re.compile(rf"(Out of memory|oom[-_ ]kill|Killed process)\D*\b{int(pid)}\b", re.I)
    return [line.strip() for line in text.splitlines() if pattern.search(line)][-3:]


def kernel_log(since):
    """The kernel's messages since SINCE (epoch seconds), where they can be read (journalctl -k, else dmesg);
    "" when they cannot. Bounded in time and size."""
    stamp = datetime.datetime.fromtimestamp(max(0, since - 60)).strftime("%Y-%m-%d %H:%M:%S")
    for command in (["journalctl", "-k", "--no-pager", "-q", "--since", stamp], ["dmesg", "-T"]):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=5, errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout[-200_000:]
    return ""


# What a crash writes as its last words: faulthandler's first line (then the stack of each thread), Chromium's own
# fatal message ([pid:tid:time:FATAL:file(line)] ...), the stack dump of a crash Chromium caught itself.
CRASH = re.compile(r"Fatal Python error: |^\[\d+:\d+:[^\]]*:FATAL:|^Received signal \d+")
CRASH_LINES = 40


def crash_lines(pid, paths, limit=1 << 20):
    """What process PID wrote of its crash into the logs PATHS (app.log, and app.log.1: the app's start script moves
    the last run's log there): from the first line of a crash (CRASH) after its last entry, at most CRASH_LINES lines;
    [] when there is none (killed from outside). Only the last LIMIT bytes of each file are read."""
    for path in paths:
        try:
            with open(path, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - limit))
                text = stream.read().decode("utf-8", "replace")
        except OSError:
            continue
        mark = text.rfind(f" pid {int(pid)},")
        if mark < 0:
            continue
        lines = text[mark:].splitlines()
        for index, line in enumerate(lines):
            if CRASH.search(line):
                return [line.rstrip() for line in lines[index:index + CRASH_LINES]]
    return []


def describe(record, oom, crash=()):
    when = record.get("seen") or record.get("started")
    try:
        at = datetime.datetime.fromtimestamp(float(when)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        at = "?"
    parts = [f"The app's last run (pid {record.get('pid')}, version {record.get('app', '?')}) ended without "
             f"quitting; last seen {at}"]
    if record.get("memory_mb") is not None:
        parts.append(f"using {record['memory_mb']} MiB" + (f" (at most {record['peak_mb']} MiB)"
                                                          if record.get("peak_mb") else ""))
    if record.get("page"):
        parts.append(f"on the page {record['page']}")
    text = ", ".join(parts) + "."
    if oom:
        text += " The kernel's out-of-memory killer ended it:\n" + "\n".join(oom)
    elif crash:
        text += " It crashed (from the log it left):\n" + "\n".join(crash)
    else:
        text += (" No Python error was written for it in app.log: it was ended from outside (a kill, the "
                 "out-of-memory killer, the session ending) or crashed where no stack could be written.")
    return text


class Guard:
    """The record of this run. start() looks at the last one and writes this one's; tick(page) updates it; stop()
    removes it. LAST: what was found of an unexpected end (the text said), or None."""

    def __init__(self, path=None, read_kernel=kernel_log, clock=time.time, wait=True):
        self.path = Path(path) if path else record_path()
        self.read_kernel, self.clock, self.wait = read_kernel, clock, wait
        self.record, self.last, self.crash = None, None, []

    def logs(self):
        """app.log beside the record, and app.log.1 (where the start script moved the last run's)."""
        log = self.path.with_name(applog.log_path().name)
        return [log, log.with_name(log.name + ".1")]

    def start(self):
        previous = read_record(self.path)
        if previous is not None and previous.get("pid") != os.getpid() and not alive(previous["pid"]):
            self.crash = crash_lines(previous["pid"], self.logs())
            self.last = describe(previous, [], self.crash)
            if self.read_kernel and not self.wait:
                # The kernel's log is read by a program (journalctl, dmesg): never on the GUI thread. Its lines are
                # said in a second entry once read.
                threading.Thread(target=self.kernel_said, args=(previous,), name="crashguard-kernel",
                                 daemon=True).start()
                applog.log("WARNING", "crash", self.last)
            else:
                self.kernel_said(previous, quiet=True)
                applog.log("WARNING", "crash", self.last)
        now = self.clock()
        self.record = {"pid": os.getpid(), "started": now, "seen": now, **{
            key: value for key, value in applog.versions().items() if key in ("app", "sandbox-ai", "qt", "python")}}
        self.tick()
        return self

    def kernel_said(self, previous, quiet=False):
        """What the kernel's log says of PREVIOUS's process: the out-of-memory killer's lines added to self.last
        (QUIET: only that), else a second entry of the log."""
        try:
            oom = oom_lines(self.read_kernel(float(previous.get("seen") or previous.get("started") or 0)),
                            previous["pid"]) if self.read_kernel else []
        except Exception:  # noqa: BLE001 - only a detail of the report
            oom = []
        if oom:
            self.last = describe(previous, oom, self.crash)
            if not quiet:
                applog.log("WARNING", "crash", "The kernel's out-of-memory killer ended the app's last run:\n"
                           + "\n".join(oom))

    def tick(self, page=None):
        if self.record is None:
            return
        memory = memory_mb()
        self.record.update(seen=self.clock(), memory_mb=memory,
                           peak_mb=max(memory or 0, self.record.get("peak_mb") or 0) or None)
        if page:
            self.record["page"] = str(page)[:40]
        if memory and memory >= WARN_MB and not self.record.get("warned"):
            self.record["warned"] = True
            applog.log("WARNING", "memory", f"the app uses {memory} MiB (page {page or '?'}): if it grows on, the "
                                            "kernel may end it; app.log and this window keep what happened")
        write_record(self.path, self.record)

    def stop(self):
        """A clean end: the record goes."""
        self.record = None
        try:
            if read_record(self.path) is not None and read_record(self.path).get("pid") == os.getpid():
                self.path.unlink()
        except OSError:
            pass
