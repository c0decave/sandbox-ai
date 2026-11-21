#!/usr/bin/env python3
"""sandbox-ai schedule: start an agent on a timetable or a trigger, and a report afterwards.

  sandbox-ai schedule add NAME (--every SPEC | --at TIME | --cron "M H DOM MON DOW") [--on-push REPO]
                          [--prompt TEXT | --prompt-file FILE] [--preset NAME [--preset-value KEY=VALUE]...]
                          [--board NAME] [--workdir DIR] [--ask off|allow|on] [--limit DURATION]
                          [--missed once|skip] [--keep-starts N] [--commit BRANCH [--push REMOTE]] [--no-notify]
                          [--replace] -- AGENT [OPTIONS] [-- AGENT ARGS]
  sandbox-ai schedule list [--json]                   every schedule: when, the agent, the next and the last run
  sandbox-ai schedule show NAME [--json]              one of them, with its start's command line and next times
  sandbox-ai schedule history NAME [--limit N] [--json]   its runs: status, duration, what changed, the report
  sandbox-ai schedule report NAME [RUN]               a run's summary and report (Markdown; default: the last)
  sandbox-ai schedule pause|resume NAME               stop and go on (runs missed while paused are not made up)
  sandbox-ai schedule run-now NAME [--wait]           one run now, as the timer would start it
  sandbox-ai schedule remove NAME [--keep-history]
  sandbox-ai schedule enable [--dry-run] | disable    the user-level timer (systemd --user, launchd)
  sandbox-ai schedule tick                            what the timer runs every minute: starts what is due

A scheduled run is a normal start under the same rules - its security mode, network, approvals, limits; telemetry
off as always - with the agent's one-shot mode (claude -p, codex exec, opencode run, qwen --prompt, goose run, ...;
an agent without one is refused) and the start's --ask from the schedule: off (the default: what is not allowed is
refused, nobody answers at night), allow (every question allowed and recorded) or on (questions wait as always).

Timing: --every 30m|2h|1d|1w|1h30m counts real time from when the schedule was added; --at HH:MM (every day) or
--at "YYYY-MM-DD HH:MM" (once) and --cron (five fields, names, ranges, steps, @daily ...) follow the local clock: a
time that does not exist on a day the clock jumps forward runs at the jump, a time that is there twice when it jumps
back runs once. --on-push REPO: a run when a branch or tag of that repository (a local path, bare or not) moved.
Runs missed while the machine was off or asleep run once on wake (--missed once, the default) or are skipped
(--missed skip). Never two runs of one schedule at once (a lock: a due run is skipped while one runs); each run has
a time limit (--limit, default 60m: then it is stopped).

Afterwards: the start's `sandbox-ai report` (Markdown) and a summary - status, duration, the git diff stat of what the
agent changed in its workspace - in the schedule's history (~/.local/state/sandbox-ai/schedules/NAME); --commit
BRANCH puts the changes on that branch (never the one checked out; the workspace stays as it is), --push REMOTE pushes
it there after the privacy check (only to the remote's address recorded when the schedule was added). The workspace
and its .git are the agent's to write: the host reads the work tree's files itself, never through a link (a folder
made a link to one of this machine is not read), runs no program the repository's configuration names, pushes from
a repository of its own that only borrows the workspace's objects (your Git configuration counts, never the
workspace's), and leaves a .git that leads outside the workspace (a worktree's, a submodule's) alone. A desktop
notification and the phone channels of `sandbox-ai notify` (when this sandbox-ai has it) say how it ended.

The timer: `schedule enable` installs a systemd user timer (Linux) or a LaunchAgent (macOS) that runs `sandbox-ai
schedule tick` every minute; nothing of the system's (no root, no crontab). SANDBOX_AI_SCHEDULE_NOW (epoch seconds or
an ISO time) stands in for the clock (tests).

Standard library only.
"""
import argparse
import contextlib
from datetime import date, datetime, timedelta, timezone
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9 has it
    ZoneInfo = None

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
VERSION = 1
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}")
ASK_MODES = ("off", "allow", "on")
MISSED = ("once", "skip")
DEFAULT_LIMIT = 3600         # seconds a run may take
LIMIT_RANGE = (10, 86400)
DEFAULT_KEEP_STARTS = 5      # the newest kept starts of a schedule; older ones are deleted (their reports stay)
HISTORY_KEEP = 200           # runs in a schedule's history (with their summaries and reports)
GRACE = 300                  # seconds after its time a run still counts as on time (not missed)
FIRES_LIMIT = 200000         # times counted between two ticks at most
PROMPT_LIMIT = 64 << 10
STOP_WAIT = 60               # seconds a run's start gets to stop after its time limit
REPORT_TIMEOUT = 600
GIT_TIMEOUT = 300
SNAPSHOT_FILES = 50000       # files of a workspace the summary looks at
SNAPSHOT_BYTES = 4 << 30
OUTPUT_TAIL = 40             # lines of the run's output in the summary
TIMER_NAME = "sandbox-ai-schedule"
LAUNCHD_LABEL = "ai.sandbox-ai.schedule"
# The start's own options a schedule sets itself: given among the OPTIONS they are refused.
OWN_OPTIONS = {"--name": "the schedule names each start (NAME-MMDDhhmmss)",
               "--session": "the schedule names each start", "--tmux": "a scheduled run has no terminal",
               "--ask": "use the schedule's --ask", "--dry-run": "a schedule starts for real",
               "--probe": "a schedule starts the agent", "--preset": "use the schedule's --preset",
               "--preset-value": "use the schedule's --preset-value", "--config": "a schedule reads the project's",
               "--no-config": "a board's schedule starts without the project file itself"}
# How each agent takes a one-shot task in a sandbox (the launcher's COMPANION_COMMANDS, agent_mcp.command without a
# helper's own flags): the AGENT ARGS of a scheduled run. An agent missing here has none: a schedule refuses it.
NANOBOT_NOTE = ("(sandbox-ai: the project is /work; work there - your tools' relative paths start in your own "
                "workspace.)\n\n")
ONESHOT = {
    "claude": lambda task: ["-p", "--", task],
    "codex": lambda task: ["exec", "--skip-git-repo-check", "--", task],
    "opencode": lambda task: ["run", "--", task],
    "opencode2": lambda task: ["run", "--", task],
    "hermes": lambda task: ["--oneshot=" + task],
    "qwen": lambda task: ["--prompt=" + task],
    "goose": lambda task: ["run", "--quiet", "--text=" + task],
    "openhands": lambda task: ["task", "--text=" + task],
    "nanobot": lambda task: ["agent", "--no-markdown", "--message=" + NANOBOT_NOTE + task],
    "letta": lambda task: ["-p", "--new", "--", task],
    "pi": lambda task: ["-p", "--", " " + task if task.startswith("@") else task],
    "dsh": lambda task: ["headless", " " + task if task.startswith("-") else task],
}
ONESHOT_HINT = {"claude": "-p TASK", "codex": "exec TASK", "opencode": "run TASK", "opencode2": "run TASK",
                "hermes": "--oneshot=TASK", "qwen": "--prompt=TASK", "goose": "run --text=TASK",
                "openhands": "task --text=TASK", "nanobot": "agent --message=TASK", "letta": "-p TASK",
                "pi": "-p TASK", "dsh": "headless TASK"}


def oneshot_given(agent, args):
    """Whether ARGS (AGENT ARGS given after the start's --) are AGENT's one-shot form."""
    words = [arg.split("=", 1)[0] for arg in args[:args.index("--")] if arg.startswith("-")] \
        if "--" in args else [arg.split("=", 1)[0] for arg in args if arg.startswith("-")]
    head = args[:args.index("--")] if "--" in args else args
    if agent in ("claude", "letta", "pi"):
        return "-p" in words or "--print" in words
    if agent in ("codex",):
        return "exec" in head or "e" in head[:1]
    if agent in ("opencode", "opencode2"):
        return "run" in head
    if agent == "hermes":
        return "--oneshot" in words or "-z" in words
    if agent == "qwen":
        return "--prompt" in words or "-p" in words
    if agent == "goose":
        return "run" in head and "--interactive" not in words and "-s" not in words
    if agent == "openhands":
        return "task" in head
    if agent == "nanobot":
        return "agent" in head and ("--message" in words or "-m" in words)
    if agent == "dsh":
        return "headless" in head
    return False


# -- helpers ----------------------------------------------------------------------------------------------------------

def load_module(name, alias=None):
    alias = alias or f"sandbox_ai_{name}_for_schedule"
    loaded = sys.modules.get(alias)
    if loaded is None:
        spec = importlib.util.spec_from_file_location(alias, HERE / f"{name}.py")
        with LOADING:
            loaded = sys.modules.get(alias)
            if loaded is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                sys.modules[alias] = loaded
    return loaded


def launch_log():
    return load_module("launchlog", "sandbox_ai_launchlog")


LOG = launch_log()


def launcher():
    return load_module("agent_sandbox", "agent_sandbox_for_schedule")


def printable(text, limit=2000):
    text = "".join(c if c.isprintable() or c in "\n\t" else "?" for c in str(text))
    return text if len(text) <= limit else text[:limit] + "…"


def redact(text):
    try:
        return LOG.redact(text)
    except Exception:  # noqa: BLE001 - the text as it is, never an error
        return text


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "schedules"


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sandbox-ai" / "schedules"


def checked_name(name):
    if name is None or (isinstance(name, str) and not name.strip()):  # (a form's field left empty: never "'None'")
        raise ValueError("a schedule needs a name: 1-24 lower-case letters, digits, '_' and '-'")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"a schedule's name is 1-24 lower-case letters, digits, '_' and '-': {printable(name, 80)!r}")
    return name


def schedule_path(name):
    return config_dir() / f"{checked_name(name)}.json"


def state_dir(name):
    return state_root() / checked_name(name)


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write_private(path, text):
    """TEXT into PATH (mode 0600) at once: a temporary file beside it, then renamed."""
    private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def read_private(path, limit=4 << 20):
    """PATH's text: a regular file of yours, never through a symlink; FileNotFoundError when it is not there."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not (info.st_mode & 0o170000 == 0o100000) or info.st_uid != os.getuid():
            raise ValueError(f"{path} must be a regular file of yours")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f"{path} is larger than {limit >> 10} KiB")
    return data.decode("utf-8", "replace")


# -- the clock --------------------------------------------------------------------------------------------------------

def now():
    """Seconds since the epoch; SANDBOX_AI_SCHEDULE_NOW (epoch seconds or an ISO time, local) stands in (tests)."""
    given = os.environ.get("SANDBOX_AI_SCHEDULE_NOW")
    if given:
        try:
            return float(given)
        except ValueError:
            pass
        try:
            moment = datetime.fromisoformat(given)
        except ValueError:
            raise ValueError(f"SANDBOX_AI_SCHEDULE_NOW: not seconds or an ISO time: {given!r}") from None
        return moment.timestamp() if moment.tzinfo else wall_to_ts(moment, local_zone())
    return time.time()


def local_zone():
    """The local time zone with its rules (TZ, else /etc/localtime); a fixed offset only when neither is there."""
    name = (os.environ.get("TZ") or "").lstrip(":")
    if ZoneInfo is not None:
        if name:
            with contextlib.suppress(Exception):
                return ZoneInfo(name)
            if os.path.isabs(name):
                with contextlib.suppress(Exception), open(name, "rb") as stream:
                    return ZoneInfo.from_file(stream, key=name)
        with contextlib.suppress(Exception), open("/etc/localtime", "rb") as stream:
            return ZoneInfo.from_file(stream, key="localtime")
    return datetime.now().astimezone().tzinfo


def wall_to_ts(naive, tz):
    """The moment the local clock (TZ) shows NAIVE: the first of two when the clock went back (fold 0); for a time the
    clock jumped over, the moment of the jump (the first minute after it that exists)."""
    for minutes in range(0, 24 * 60 + 1):
        candidate = naive + timedelta(minutes=minutes)
        if minutes:
            candidate = candidate.replace(second=0, microsecond=0)
        moment = candidate.replace(tzinfo=tz, fold=0)
        ts = moment.timestamp()
        if datetime.fromtimestamp(ts, tz).replace(tzinfo=None) == candidate:
            return ts
    return naive.replace(tzinfo=tz).timestamp()  # pragma: no cover - no zone skips a whole day


def local(ts, tz):
    return datetime.fromtimestamp(ts, tz)


def shown_time(ts, tz=None):
    if ts is None:
        return "-"
    return local(ts, tz or local_zone()).strftime("%Y-%m-%d %H:%M")


# -- timing: --every, --at, --cron ------------------------------------------------------------------------------------

UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
DURATION_UNITS = {"s": 1, **UNITS}


def parse_every(text):
    """Seconds of --every SPEC: 30m, 2h, 1d, 1w, 1h30m (at least 1m, at most 366d)."""
    text = str(text).strip().lower()
    parts = re.findall(r"([0-9]{1,6})([mhdw])", text)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != text:
        raise ValueError(f"--every {printable(text, 40)!r}: a duration such as 30m, 2h, 1d, 1w or 1h30m")
    seconds = sum(int(n) * UNITS[u] for n, u in parts)
    if not 60 <= seconds <= 366 * 86400:
        raise ValueError("--every: from 1m to 366d")
    return seconds


def parse_duration(text, what="--limit"):
    """Seconds of a duration such as 90s, 30m, 2h, 1h30m (a plain number: minutes)."""
    text = str(text).strip().lower()
    if re.fullmatch(r"[0-9]{1,5}", text):
        return int(text) * 60
    parts = re.findall(r"([0-9]{1,6})([smhd])", text)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != text:
        raise ValueError(f"{what} {printable(text, 40)!r}: a duration such as 90s, 30m, 2h or 1h30m")
    return sum(int(n) * DURATION_UNITS[u] for n, u in parts)


def parse_at(text):
    """--at HH:MM (every day: the cron fields) or YYYY-MM-DD HH:MM (once: the naive local time)."""
    text = str(text).strip()
    match = re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", text)
    if match:
        return {"daily": (int(match.group(1)), int(match.group(2)))}
    try:
        moment = datetime.fromisoformat(text.replace(" ", "T", 1) if re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{1,2}:[0-9]{2}", text) else text)
    except ValueError:
        raise ValueError(f"--at {printable(text, 40)!r}: HH:MM (every day) or YYYY-MM-DD HH:MM (once)") from None
    if moment.tzinfo is not None:
        raise ValueError("--at: a local time, without a zone")
    return {"once": moment.replace(second=0, microsecond=0)}


MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DAYS = {name: number for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
ALIASES = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
           "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}


class Cron:
    """Five cron fields (minute hour day-of-month month day-of-week) with lists, ranges, steps and names; day of
    month and day of week both restricted: either one matches (as cron does)."""
    FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}), ("month", 1, 12, MONTHS),
              ("day of week", 0, 7, DAYS))

    def __init__(self, text):
        text = " ".join(str(text).split())
        self.text = ALIASES.get(text.lower(), text)
        fields = self.text.split(" ")
        if len(fields) != 5:
            raise ValueError(f"--cron {printable(text, 80)!r}: five fields, minute hour day-of-month month "
                             "day-of-week (or @hourly, @daily, @weekly, @monthly, @yearly)")
        values = [self.field(value, *spec) for value, spec in zip(fields, self.FIELDS)]
        self.minutes, self.hours, self.days, self.months = (sorted(v) for v in values[:4])
        self.weekdays = {0 if day == 7 else day for day in values[4]}
        self.any_day, self.any_weekday = fields[2].startswith("*"), fields[4].startswith("*")
        if not any(self.day_possible(month) for month in self.months) and self.any_weekday:
            raise ValueError(f"--cron {printable(text, 80)!r}: those days never come in those months")

    @staticmethod
    def field(value, name, low, high, names):
        found = set()
        for item in value.lower().split(","):
            match = re.fullmatch(r"(\*|[a-z0-9]+(?:-[a-z0-9]+)?)(?:/([0-9]{1,2}))?", item)
            if not match:
                raise ValueError(f"--cron: {name} {printable(value, 40)!r} is not a cron field")
            span, step = match.group(1), int(match.group(2)) if match.group(2) else 1
            if step < 1:
                raise ValueError(f"--cron: {name}: a step of at least 1")

            def number(word):
                if word in names:
                    return names[word]
                if not word.isdigit() or not low <= int(word) <= high:
                    raise ValueError(f"--cron: {name} {printable(word, 20)!r} is not within {low}-{high}")
                return int(word)
            if span == "*":
                first, last = low, high
            elif "-" in span:
                first, last = (number(word) for word in span.split("-", 1))
                if first > last:
                    raise ValueError(f"--cron: {name} {span}: a range from low to high")
            else:
                first = number(span)
                last = high if match.group(2) else first
            found.update(range(first, last + 1, step))
        return found

    def day_possible(self, month):
        longest = {2: 29, 4: 30, 6: 30, 9: 30, 11: 30}.get(month, 31)
        return any(day <= longest for day in self.days)

    def day_matches(self, day):
        if day.month not in self.months:
            return False
        weekday = (day.weekday() + 1) % 7  # Monday is 1, Sunday 0 (cron's)
        in_month, in_week = day.day in self.days, weekday in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return in_week
        if self.any_weekday:
            return in_month
        return in_month or in_week

    def next_after(self, after, tz):
        """The first moment after AFTER (seconds) the local clock (TZ) matches; None within eight years."""
        start = (local(after, tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1))
        first_day = start.date()
        for offset in range(0, 366 * 8 + 2):
            day = first_day + timedelta(days=offset)
            if not self.day_matches(day):
                continue
            for hour in self.hours:
                for minute in self.minutes:
                    naive = datetime(day.year, day.month, day.day, hour, minute)
                    if naive < start:
                        continue
                    ts = wall_to_ts(naive, tz)
                    if ts > after:
                        return ts
        return None


def timing_of(schedule):
    """(kind, value) of SCHEDULE's time table: ("every", seconds), ("cron", Cron), ("once", naive) or (None, None)."""
    timing = schedule.get("timing") or {}
    kind, value = timing.get("kind"), timing.get("value")
    if kind == "every":
        return "every", parse_every(value)
    if kind == "cron":
        return "cron", Cron(value)
    if kind == "at":
        parsed = parse_at(value)
        if "daily" in parsed:
            hour, minute = parsed["daily"]
            return "cron", Cron(f"{minute} {hour} * * *")
        return "once", parsed["once"]
    return None, None


def next_due(schedule, after, tz=None):
    """The first time SCHEDULE is due after AFTER (seconds); None: never again (a time that has passed, push only)."""
    tz = tz or local_zone()
    kind, value = timing_of(schedule)
    if kind == "every":
        anchor = float(schedule.get("created") or 0)
        count = max(0, int((after - anchor) // value) + 1)
        due = anchor + count * value
        while due <= after:  # (float rounding)
            due += value
        return due
    if kind == "cron":
        return value.next_after(after, tz)
    if kind == "once":
        ts = wall_to_ts(value, tz)
        return ts if ts > after else None
    return None


def fires_between(schedule, after, until, tz=None):
    """(how many times, the last of them) SCHEDULE was due in (AFTER, UNTIL]; (0, None) for none."""
    tz = tz or local_zone()
    kind, value = timing_of(schedule)
    if kind == "every":
        first = next_due(schedule, after, tz)
        if first > until:
            return 0, None
        count = int((until - first) // value) + 1
        return count, first + (count - 1) * value
    count, last, at = 0, None, after
    while count < FIRES_LIMIT:
        due = next_due(schedule, at, tz)
        if due is None or due > until:
            break
        count, last, at = count + 1, due, due
    return count, last


def timing_text(schedule):
    timing = schedule.get("timing") or {}
    kind, value = timing.get("kind"), timing.get("value")
    parts = []
    if kind == "every":
        parts.append(f"every {value}")
    elif kind == "at":
        parts.append(f"daily at {value}" if re.fullmatch(r"[0-9]{1,2}:[0-9]{2}", str(value)) else f"once at {value}")
    elif kind == "cron":
        parts.append(f"cron {value}")
    if schedule.get("on_push"):
        parts.append(f"on push to {schedule['on_push']}")
    return ", ".join(parts) or "-"


# -- the schedule file ------------------------------------------------------------------------------------------------

KEYS = {"version", "name", "created", "timing", "on_push", "agent", "options", "agent_args", "prompt", "preset",
        "preset_values", "board", "workdir", "ask", "limit", "missed", "keep_starts", "commit", "push", "push_url",
        "notify", "paused"}


def check_schedule(document):
    """DOCUMENT (a schedule file's) as it may be: its fields checked; ValueError says what is wrong."""
    if not isinstance(document, dict) or document.get("version") != VERSION:
        raise ValueError("not a schedule (version 1)")
    unknown = set(document) - KEYS
    if unknown:
        raise ValueError(f"unknown fields {', '.join(sorted(unknown))}")
    checked_name(document.get("name"))
    timing = document.get("timing")
    if timing is not None:
        if not isinstance(timing, dict) or timing.get("kind") not in ("every", "at", "cron"):
            raise ValueError("timing: every, at or cron")
        timing_of(document)
    if timing is None and not document.get("on_push"):
        raise ValueError("when it runs: --every, --at, --cron or --on-push")
    for key in ("options", "agent_args", "preset_values"):
        value = document.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value) or len(value) > 2000:
            raise ValueError(f"{key}: a list of texts")
    for key in ("prompt", "preset", "board", "on_push", "commit", "push", "push_url", "workdir", "agent"):
        if document.get(key) is not None and not isinstance(document[key], str):
            raise ValueError(f"{key}: a text")
    if document.get("agent") not in ONESHOT_HINT and not document.get("board"):
        raise ValueError(f"no agent {document.get('agent')!r}")
    if not isinstance(document.get("workdir"), str) or not os.path.isabs(document["workdir"]):
        raise ValueError("workdir: an absolute path")
    if document.get("ask") not in ASK_MODES:
        raise ValueError("ask: off, allow or on")
    if document.get("missed") not in MISSED:
        raise ValueError("missed: once or skip")
    limit = document.get("limit")
    if not isinstance(limit, int) or not LIMIT_RANGE[0] <= limit <= LIMIT_RANGE[1]:
        raise ValueError(f"limit: {LIMIT_RANGE[0]}-{LIMIT_RANGE[1]} seconds")
    keep = document.get("keep_starts")
    if not isinstance(keep, int) or not 0 <= keep <= 1000:
        raise ValueError("keep_starts: 0-1000")
    if document.get("prompt") is not None and len(document["prompt"]) > PROMPT_LIMIT:
        raise ValueError(f"the task is longer than {PROMPT_LIMIT >> 10} KiB")
    if document.get("push") and not document.get("commit"):
        raise ValueError("--push needs --commit BRANCH")
    return document


def load(name):
    """The schedule NAME; ValueError when there is none or its file is not as it should be."""
    path = schedule_path(name)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise ValueError(f"no schedule {name} (sandbox-ai schedule list)") from None
    if info.st_mode & 0o077:
        raise ValueError(f"{path} must have mode 0600")
    try:
        document = json.loads(read_private(path))
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: {exc}") from None
    try:
        check_schedule(document)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{path}: {exc}") from None
    if document["name"] != name:
        raise ValueError(f"{path}: its name is not {name}")
    return document


def names():
    try:
        return sorted(path.stem for path in config_dir().glob("*.json") if NAME.fullmatch(path.stem))
    except OSError:
        return []


def save(document):
    check_schedule(document)
    write_private(schedule_path(document["name"]), json.dumps(document, indent=2) + "\n")


def load_state(name):
    try:
        data = json.loads(read_private(state_dir(name) / "state.json"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def save_state(name, state):
    write_private(state_dir(name) / "state.json", json.dumps(state, indent=2) + "\n")


# -- the start --------------------------------------------------------------------------------------------------------

def split_start(tokens):
    """(agent or None, options, agent args) of the start's words: [AGENT] [OPTIONS] [-- AGENT ARGS]."""
    tokens = list(tokens)
    agent = None
    if tokens and not tokens[0].startswith("-"):
        agent = tokens.pop(0)
    if "--" in tokens:
        position = tokens.index("--")
        return agent, tokens[:position], tokens[position + 1:]
    return agent, tokens, []


def board_document(name):
    """The board kept here as NAME (sandbox-ai board list) - never a file of the folder a run starts in, which an
    agent may have written."""
    return load_module("board_bundle").kept_board(name)


def check_options(agent, options):
    """OPTIONS as the launcher reads them (its own parser): canonical long forms. ValueError: an option the schedule
    sets itself, or one the launcher does not take."""
    sandbox = launcher()
    canonical = sandbox.canonical_options(list(options))
    for token in canonical:
        name = token.split("=", 1)[0]
        if name in OWN_OPTIONS:
            raise ValueError(f"{name} is not for a schedule's start: {OWN_OPTIONS[name]}")
    import io
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            sandbox.build_parser(agent).parse_args([agent, *canonical])
    except SystemExit:
        said = [line for line in errors.getvalue().strip().splitlines() if line.strip()]
        raise ValueError(f"the start's options: {said[-1] if said else 'not as the launcher takes them'}") from None
    return canonical


def task_of(schedule):
    """The task's text: --prompt, else the preset's first task (its values filled in), else None."""
    if schedule.get("prompt"):
        return schedule["prompt"]
    if schedule.get("preset"):
        presets = load_module("presets")
        text = presets.load(schedule["preset"]).get("prompt")
        if text:
            values = dict(pair.split("=", 1) for pair in schedule.get("preset_values", []) if "=" in pair)
            return presets.PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    return None


def agent_args_of(schedule):
    agent = schedule["agent"]
    task = task_of(schedule)
    if task:
        if agent not in ONESHOT:
            raise ValueError(f"{agent} has no one-shot mode sandbox-ai knows: it cannot run on a schedule")
        return list(schedule.get("agent_args") or []) + ONESHOT[agent](task)
    args = list(schedule.get("agent_args") or [])
    if agent not in ONESHOT:
        raise ValueError(f"{agent} has no one-shot mode sandbox-ai knows: it cannot run on a schedule")
    if not oneshot_given(agent, args):
        raise ValueError(f"a scheduled run needs a task: --prompt TEXT, or {agent}'s one-shot form after the start's "
                         f"-- ({agent} {ONESHOT_HINT[agent]})")
    return args


def start_argv(schedule, start_name):
    """The launcher's words of one run of SCHEDULE: AGENT [the board's options] [--preset NAME ...] OPTIONS --name
    START --no-tmux --ask MODE -- AGENT ARGS."""
    agent = schedule["agent"]
    head = []
    if schedule.get("board"):
        document = board_document(schedule["board"])
        agent = document["agent"]
        head = ["--no-config", *document["options"]]
    if schedule.get("preset"):
        head += ["--preset", schedule["preset"]]
        for value in schedule.get("preset_values", []):
            head += ["--preset-value", value]
    args = agent_args_of({**schedule, "agent": agent})
    return [agent, *head, *schedule.get("options", []), "--name", start_name, "--no-tmux", "--ask",
            schedule["ask"], "--", *args]


def safe_path(text, folders):
    """The PATH TEXT without what a schedule's host-side programs must never be looked for in: relative entries (a
    run's launcher starts in the workspace) and entries that are or lie in one of FOLDERS (the workspace: an
    activated virtualenv, node_modules/.bin - the agent writes them, and nobody watches a scheduled run)."""
    tops = []
    for folder in folders:
        with contextlib.suppress(OSError, ValueError):
            tops.append(os.path.realpath(folder))
    kept = []
    for entry in str(text or "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        try:
            real = os.path.realpath(entry)
        except (OSError, ValueError):
            continue
        if any(inside(top, real) for top in tops):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def run_environment(schedule):
    """The environment of a run's own launcher (and of what the run's process starts on the host): this one's, its
    PATH without the workspace's folders (safe_path), SANDBOX_AI_SCHEDULE the schedule's name."""
    env = {**os.environ, "SANDBOX_AI_SCHEDULE": schedule["name"],
           "PATH": safe_path(os.environ.get("PATH", os.defpath), [schedule["workdir"]])}
    env.pop("SANDBOX_AI_SCHEDULE_NOW", None)
    return env


def main_command():
    """sandbox-ai itself: this Python with main.py beside this file."""
    return [sys.executable, "-I", str(HERE / "main.py")]


# -- git: what the agent changed --------------------------------------------------------------------------------------

DANGEROUS_CONFIG = re.compile(r"(credential\..*|core\.sshcommand|core\.gitproxy|core\.askpass|url\..*\.(push)?insteadof"
                              r"|remote\..*\.(receivepack|uploadpack|vcs|proxy)|include\.path|includeif\..*"
                              r"|http\..*|ssh\..*|protocol\..*|push\..*|gpg\..*|submodule\..*)", re.IGNORECASE)


class Git:
    """Git plumbing for the workspace on the host: no program the repository's own configuration names ever runs (no
    hooks, fsmonitor, filters, textconv, external diff, transport); objects are hashed as the files are."""

    def __init__(self, repo, index=None):
        self.repo, self.index = Path(repo), index

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C",
                    "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "sandbox-ai-none",
                    "GIT_CONFIG_NOSYSTEM": "1"})
        if self.index:
            env["GIT_INDEX_FILE"] = str(self.index)
        env["PATH"] = safe_path(env.get("PATH", os.defpath), [self.repo])  # (git itself: never the repository's own)
        return env

    def command(self):
        return ["git", "--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", "-c", "core.sshCommand=false",
                "-c", "diff.external=", "-c", "commit.gpgSign=false", "-c", "core.alternateRefsCommand=",
                "-C", str(self.repo)]

    def __call__(self, *args, data=None, check=True):
        result = subprocess.run([*self.command(), *args], input=data, capture_output=True, env=self.env(),
                                timeout=GIT_TIMEOUT, stdin=None if data is not None else subprocess.DEVNULL)
        if check and result.returncode != 0:
            said = result.stderr.decode(errors="replace").strip().splitlines()
            raise ValueError(f"git {args[0]}: {said[-1] if said else result.returncode}")
        return result

    def out(self, *args, **kwargs):
        return self(*args, **kwargs).stdout.decode(errors="replace").strip()


def is_repository(path):
    return shutil.which("git") is not None and os.path.lexists(Path(path) / ".git")


def head_of(git):
    result = git("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    return result.stdout.decode().strip() or None if result.returncode == 0 else None


OUTSIDE = ("the workspace's .git leads outside the workspace ({what}): Git is not run on it from the host - no "
           "summary of the changes, nothing committed or pushed")


def inside(top, path):
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def git_folder(repo, branch=None):
    """The repository's own folder of the workspace REPO (absolute), checked: it, the folder its worktrees share,
    its objects and its branches (BRANCH's file too) lie inside the workspace - a .git that is a file or a link to
    another repository of this machine (or whose objects or refs are links out of it) would have the host write
    that one. ValueError otherwise."""
    top = os.path.realpath(repo)
    git = Git(repo)
    found = {}
    for key, args in (("git", ("--absolute-git-dir",)), ("common", ("--git-common-dir",)),
                      ("objects", ("--git-path", "objects")), ("heads", ("--git-path", "refs/heads")),
                      *((("branch", ("--git-path", f"refs/heads/{branch}")),) if branch else ())):
        said = git("rev-parse", *args).stdout.decode("utf-8", "surrogateescape").rstrip("\n")
        found[key] = os.path.realpath(os.path.join(top, said))
        if not said or not inside(top, found[key]):
            raise ValueError(OUTSIDE.format(what=printable(found[key], 200)))
    return found


def object_format(git):
    """The hash of the repository's objects: sha1, or sha256."""
    said = git("rev-parse", "--show-object-format", check=False)
    name = said.stdout.decode(errors="replace").strip() if said.returncode == 0 else "sha1"
    if name not in ("sha1", "sha256"):
        raise ValueError(f"an object format sandbox-ai does not know ({printable(name, 40)}): no summary")
    return name


FOLDER_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
BLOB_CHUNK = 1 << 20


class WorkTree:
    """The files of a work tree read for a snapshot: every folder on the way to a path is opened relative to its
    parent without following a link, the file itself too - a folder the agent replaced by a link to a folder of
    this machine is never read through (the path is gone, as Git itself sees a path beyond a link)."""

    def __init__(self, repo):
        self.root = os.open(repo, FOLDER_FLAGS)
        self.head, self.fd = None, None  # the folder opened last (paths come folder by folder)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
        os.close(self.root)
        self.head = self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def folder(self, head):
        """A descriptor of the folder HEAD (relative; '': the work tree itself), or None: not a folder now."""
        if not head:
            return self.root
        if head == self.head:
            return self.fd
        if self.fd is not None:
            os.close(self.fd)
        self.head = self.fd = None
        current = self.root
        try:
            for part in head.split("/"):
                opened = os.open(part, FOLDER_FLAGS, dir_fd=current)
                if current != self.root:
                    os.close(current)
                current = opened
        except OSError:
            if current != self.root:
                os.close(current)
            return None
        self.head, self.fd = head, current
        return current

    def entry(self, path):
        """What PATH is now: ("file", descriptor, its stat), ("link", its target's bytes), ("other",) - a folder (a
        nested repository), a special file - or None: gone, or beyond a link."""
        parts = path.rstrip("/").split("/")  # (an untracked nested repository is listed as "folder/")
        if not parts[0] or any(part in ("", ".", "..", ".git") for part in parts):
            return None
        folder = self.folder("/".join(parts[:-1]))
        if folder is None:
            return None
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if info.st_mode & 0o170000 == 0o120000:
                return "link", os.fsencode(os.readlink(name, dir_fd=folder))
            if info.st_mode & 0o170000 != 0o100000:
                return ("other",)
            opened = os.open(name, READ_FLAGS, dir_fd=folder)
        except OSError:
            return None
        info = os.fstat(opened)
        if info.st_mode & 0o170000 != 0o100000:
            os.close(opened)
            return ("other",)
        return "file", opened, info


def blob_id(algorithm, size):
    """A hash begun for a blob of SIZE bytes (Git's object id: the hash of "blob SIZE\\0" and the content)."""
    digest = hashlib.new(algorithm)
    digest.update(b"blob %d\0" % size)
    return digest


def read_blob(opened, size, digest, write=None):
    """The SIZE bytes of the open file into DIGEST (and WRITE); ValueError when it is no longer that long."""
    left = size
    while left:
        chunk = os.read(opened, min(BLOB_CHUNK, left))
        if not chunk:
            break
        digest.update(chunk)
        if write is not None:
            write(chunk)
        left -= len(chunk)
    if left or os.read(opened, 1):
        raise ValueError("the work tree changes while it is read: no summary of the changes")


def store_blobs(git, tree, algorithm, missing):
    """The blobs MISSING ({object id: path, or the bytes of a link's target}) written into the repository by one
    `git fast-import` (its objects only: no ref, no program of the repository's), each read again without following
    a link and checked against its id. ValueError when a file changed meanwhile or Git failed."""
    if not missing:
        return
    with tempfile.TemporaryFile() as said:
        process = subprocess.Popen([*git.command(), "fast-import", "--quiet", "--done"], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=said, env=git.env())
        timer = threading.Timer(GIT_TIMEOUT, process.kill)
        timer.start()
        try:
            try:
                for wanted, source in missing.items():
                    if isinstance(source, bytes):
                        process.stdin.write(b"blob\ndata %d\n" % len(source) + source + b"\n")
                        continue
                    found = tree.entry(source)
                    if found is None or found[0] != "file":
                        raise ValueError("the work tree changes while it is read: no summary of the changes")
                    try:
                        size = found[2].st_size
                        digest = blob_id(algorithm, size)
                        process.stdin.write(b"blob\ndata %d\n" % size)
                        read_blob(found[1], size, digest, process.stdin.write)
                        process.stdin.write(b"\n")
                    finally:
                        os.close(found[1])
                    if digest.hexdigest() != wanted:
                        raise ValueError("the work tree changes while it is read: no summary of the changes")
                process.stdin.write(b"done\n")
                process.stdin.close()
            except BaseException:
                process.kill()
                raise
            finally:
                with contextlib.suppress(OSError):
                    process.stdin.close()
                status = process.wait()
        except BrokenPipeError:
            status = process.wait()
        finally:
            timer.cancel()
        if status != 0:
            said.seek(0)
            lines = said.read()[-2000:].decode(errors="replace").strip().splitlines()
            raise ValueError(f"git fast-import: {lines[-1] if lines else status}")


def snapshot(repo, scratch):
    """{"tree", "head", "files"} of REPO as it is now - its HEAD with every tracked and untracked (not ignored) file
    of the work tree - hashed without any filter into the repository's objects, through an index of its own in
    SCRATCH (the repository's index is never touched). The files are read by sandbox-ai itself, no link followed on
    the way to one (WorkTree): what a link of the agent's leads to on this machine never becomes an object of the
    workspace. None: not a repository; ValueError: too big, a .git that leads outside the workspace, git failed."""
    if not is_repository(repo):
        return None
    git_folder(repo)
    index = Path(scratch) / f"index-{os.urandom(4).hex()}"
    git = Git(repo, index)
    try:
        head = head_of(git)
        git("read-tree", head) if head else git("read-tree", "--empty")
        listed = [p for p in git("ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
                  .decode("utf-8", "surrogateescape").split("\0") if p]
        listed = list(dict.fromkeys(listed))
        if len(listed) > SNAPSHOT_FILES:
            raise ValueError(f"more than {SNAPSHOT_FILES} files: no summary of the changes")
        algorithm = object_format(git)
        lines, sources, total = [], {}, 0
        with WorkTree(repo) as tree:
            for path in listed:
                found = tree.entry(path)
                if found is None:
                    lines.append(f"0 {'0' * (64 if algorithm == 'sha256' else 40)}\t{path}")
                elif found[0] == "link":
                    digest = blob_id(algorithm, len(found[1]))
                    digest.update(found[1])
                    sources.setdefault(digest.hexdigest(), found[1])
                    lines.append(f"120000 {digest.hexdigest()}\t{path}")
                elif found[0] == "file":
                    try:
                        info = found[2]
                        total += info.st_size
                        if total > SNAPSHOT_BYTES:
                            raise ValueError(f"the work tree's files are larger than {SNAPSHOT_BYTES >> 30} GiB: no "
                                             "summary")
                        digest = blob_id(algorithm, info.st_size)
                        read_blob(found[1], info.st_size, digest)
                    finally:
                        os.close(found[1])
                    sources.setdefault(digest.hexdigest(), path)
                    lines.append(f"{'100755' if info.st_mode & 0o111 else '100644'} {digest.hexdigest()}\t{path}")
                # (a folder: a nested repository - its entry stays as HEAD has it)
            if sources:
                checked = git("cat-file", "--batch-check", data="\n".join(sources).encode() + b"\n").stdout.decode()
                missing = {line.split()[0]: sources[line.split()[0]] for line in checked.splitlines()
                           if line.endswith(" missing") and line.split()[0] in sources}
                store_blobs(git, tree, algorithm, missing)
        if lines:
            git("update-index", "-z", "--index-info",
                data="\0".join(lines).encode("utf-8", "surrogateescape") + b"\0")
        tree = git.out("write-tree")
        return {"tree": tree, "head": head, "files": len(listed)}
    finally:
        with contextlib.suppress(OSError):
            index.unlink()
        with contextlib.suppress(OSError):
            Path(str(index) + ".lock").unlink()


def changes(repo, before, after):
    """{"stat" (git's diff stat), "files", "insertions", "deletions", "commits"} between two snapshots."""
    git = Git(repo)
    found = {"stat": "", "files": 0, "insertions": 0, "deletions": 0, "commits": 0}
    if before["tree"] != after["tree"]:
        found["stat"] = git.out("diff-tree", "-r", "--no-ext-diff", "--no-textconv", "--no-renames",
                                "--stat=120,100", before["tree"], after["tree"])
        for line in git.out("diff-tree", "-r", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat",
                            before["tree"], after["tree"]).splitlines():
            added, deleted, _path = (line.split("\t", 2) + ["", ""])[:3]
            found["files"] += 1
            found["insertions"] += int(added) if added.isdigit() else 0
            found["deletions"] += int(deleted) if deleted.isdigit() else 0
    if after.get("head") and after["head"] != before.get("head"):
        span = f"{before['head']}..{after['head']}" if before.get("head") else after["head"]
        count = git("rev-list", "--count", span, check=False).stdout.decode().strip()
        found["commits"] = int(count) if count.isdigit() else 0
    return found


def check_branch(branch):
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or branch.startswith(
            ("-", "/", ".")) or branch.endswith(("/", ".", ".lock")) or ".." in branch or "//" in branch or \
            "/." in branch or branch == "HEAD":
        raise ValueError(f"--commit {printable(branch, 80)!r}: a branch name (letters, digits, . _ - /)")
    return branch


def run_tree(repo, before, after, scratch):
    """The tree of AFTER's HEAD with what changed between the snapshots BEFORE and AFTER (the run's changes) as AFTER
    has them - never what was uncommitted in the work tree before the run and stayed as it was (the human's own
    work). Through an index of its own in SCRATCH."""
    index = Path(scratch) / f"index-{os.urandom(4).hex()}"
    git = Git(repo, index)
    try:
        head = after.get("head")
        git("read-tree", head) if head else git("read-tree", "--empty")
        raw = git("diff-tree", "-r", "-z", "--no-renames", "--no-ext-diff", before["tree"], after["tree"]).stdout
        fields, lines = raw.split(b"\0"), []
        for meta, path in zip(fields[0::2], fields[1::2]):
            parts = meta.decode().lstrip(":").split()
            if len(parts) < 5:
                continue
            new_mode, new_id, status = parts[1], parts[3], parts[4]
            lines.append((f"0 {'0' * len(new_id)}\t" if status.startswith("D") else f"{new_mode} {new_id}\t").encode() + path)
        if lines:
            git("update-index", "-z", "--index-info", data=b"\0".join(lines) + b"\0")
        return git.out("write-tree")
    finally:
        for leftover in (index, Path(str(index) + ".lock")):
            with contextlib.suppress(OSError):
                leftover.unlink()


def commit_changes(repo, after, branch, message, before=None, scratch=None):
    """The changes of the run (BEFORE and AFTER: its snapshots; run_tree in SCRATCH) as one commit on BRANCH, on top
    of the work tree's HEAD; the work tree and the branch checked out stay as they are. Without BEFORE: the work tree
    as AFTER has it. The commit, or ValueError (why not)."""
    git = Git(repo)
    git_folder(repo, check_branch(branch))
    current = git("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
    if current == f"refs/heads/{branch}":
        raise ValueError(f"{branch} is the branch checked out in the workspace: not committed")
    head = after.get("head")
    tree = run_tree(repo, before, after, scratch) if before is not None and scratch is not None else after["tree"]
    if head and git.out("rev-parse", f"{head}^{{tree}}") == tree:
        commit = head  # nothing beyond the agent's own commits
    else:
        identity = {"GIT_AUTHOR_NAME": "sandbox-ai schedule", "GIT_AUTHOR_EMAIL": "schedule@sandbox-ai.invalid",
                    "GIT_COMMITTER_NAME": "sandbox-ai schedule", "GIT_COMMITTER_EMAIL": "schedule@sandbox-ai.invalid"}
        result = subprocess.run([*git.command(), "commit-tree", tree, *(["-p", head] if head else []),
                                 "-m", message], capture_output=True, env={**git.env(), **identity},
                                timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise ValueError(f"git commit-tree: {result.stderr.decode(errors='replace').strip()[-300:]}")
        commit = result.stdout.decode().strip()
    git("update-ref", "-m", "sandbox-ai schedule", f"refs/heads/{branch}", commit)
    return commit


def remote_url(repo, remote):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", remote or ""):
        raise ValueError(f"--push {printable(remote, 80)!r}: a remote's name")
    if not is_repository(repo):
        raise ValueError(f"--push: {repo} is not a Git repository")
    url = Git(repo)("config", "--get", f"remote.{remote}.url", check=False).stdout.decode().strip()
    if not url:
        raise ValueError(f"--push: the workspace has no remote {remote}")
    return url


def push_folder():
    """Where a push's own repository is made (and removed again): sandbox-ai's state, never a workspace."""
    return state_root() / ".push"


def push_branch(repo, remote, recorded_url, branch, run=subprocess.run):
    """Push BRANCH to REMOTE after the privacy check: only to the address recorded when the schedule was added, and
    from a repository of sandbox-ai's own that borrows the workspace's objects (objects/info/alternates) - Git reads
    your configuration and that repository's, never the workspace's (.git/config, config.worktree, an include: a
    credential helper, proxy, SSH command, hook or URL rewrite the agent wrote there is not there for it). A
    workspace configuration that names such things is refused all the same. "pushed", or ValueError (why not)."""
    git = Git(repo)
    check_branch(branch)
    folders = git_folder(repo, branch)
    if remote_url(repo, remote) != recorded_url:
        raise ValueError(f"the remote {remote} points elsewhere than when the schedule was added: not pushed")
    local_config = git("config", "--local", "--list", "-z", check=False).stdout.decode(errors="replace")
    risky = sorted({entry.split("\n", 1)[0] for entry in local_config.split("\0")
                    if entry and DANGEROUS_CONFIG.fullmatch(entry.split("\n", 1)[0])})
    if risky:
        raise ValueError(f"the workspace's .git/config sets {', '.join(risky[:5])}: not pushed from the host")
    commit = git("rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}", check=False).stdout.decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
        raise ValueError(f"the workspace has no branch {branch}: not pushed")
    algorithm = object_format(git)
    if "\n" in folders["objects"] or "\r" in folders["objects"]:
        raise ValueError("the workspace's path holds a line break: not pushed from the host")
    checked = run([*main_command(), "privacy-check", str(repo)], capture_output=True, text=True, timeout=600,
                  stdin=subprocess.DEVNULL)
    if checked.returncode != 0:
        said = (checked.stdout + checked.stderr).strip().splitlines()
        raise ValueError("the privacy check stopped the push" + (f": {said[-1]}" if said else ""))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PATH"] = safe_path(env.get("PATH", os.defpath), [repo])
    scratch = tempfile.mkdtemp(prefix="push-", dir=private_dir(push_folder()))
    try:
        own = os.path.join(scratch, "push.git")
        made = run(["git", "init", "-q", "--bare", "--template=",
                    *([f"--object-format={algorithm}"] if algorithm != "sha1" else []), own], capture_output=True,
                   text=True, timeout=GIT_TIMEOUT, env=env, stdin=subprocess.DEVNULL, cwd=scratch)
        if made.returncode != 0:
            said = made.stderr.strip().splitlines()
            raise ValueError(f"git init failed: {said[-1] if said else made.returncode}")
        os.makedirs(os.path.join(own, "objects", "info"), exist_ok=True)
        with open(os.path.join(own, "objects", "info", "alternates"), "w", encoding="utf-8",
                  errors="surrogateescape") as stream:
            stream.write(folders["objects"] + "\n")
        shallow = os.path.join(folders["common"], "shallow")  # (a shallow clone: its boundary goes along)
        if os.path.isfile(shallow) and not os.path.islink(shallow):
            shutil.copyfile(shallow, os.path.join(own, "shallow"))
        # (run in the workspace's folder: a remote that is a relative path means the same as for the human; with
        # --git-dir nothing of that folder's own .git is looked for)
        result = run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                      "-c", "protocol.ext.allow=never", "--git-dir", own, "push", "--force", recorded_url,
                      f"{commit}:refs/heads/{branch}"], capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env,
                     stdin=subprocess.DEVNULL, cwd=str(repo))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result.returncode != 0:
        said = result.stderr.strip().splitlines()
        raise ValueError(f"git push failed: {said[-1] if said else result.returncode}")
    return "pushed"


def refs_digest(repo):
    """A digest of the branches and tags of REPO (a work tree or a bare repository); None: not readable."""
    path = Path(repo)
    if not (path / ".git").exists() and not (path / "HEAD").is_file():
        return None
    git = Git(repo)
    result = git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags", check=False)
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


# -- locks ------------------------------------------------------------------------------------------------------------

class Lock:
    """An exclusive lock on PATH (flock), taken without waiting: held() says whether it was got."""

    def __init__(self, path):
        import fcntl
        private_dir(Path(path).parent)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.got = True
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(self.fd)
                raise
            self.got = False

    def release(self):
        if self.fd is not None:
            os.close(self.fd)  # (the lock goes with it)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def running(name):
    """Whether a run of NAME holds its lock now."""
    path = state_dir(name) / "lock"
    if not path.exists():
        return False
    lock = Lock(path)
    try:
        return not lock.got
    finally:
        lock.release()


# -- history ----------------------------------------------------------------------------------------------------------

def history(name, limit=None):
    """The runs of NAME, the newest last."""
    path = state_dir(name) / "history.jsonl"
    try:
        lines = read_private(path, 64 << 20).splitlines()
    except (OSError, ValueError):
        return []
    entries = []
    for line in lines:
        with contextlib.suppress(ValueError, RecursionError):
            entry = json.loads(line)
            if isinstance(entry, dict):
                entries.append(entry)
    return entries[-limit:] if limit else entries


def record(name, entry):
    """ENTRY at the end of NAME's history; the oldest beyond HISTORY_KEEP go, with their files."""
    entries = [*history(name), entry]
    dropped, entries = entries[:-HISTORY_KEEP], entries[-HISTORY_KEEP:]
    write_private(state_dir(name) / "history.jsonl", "".join(json.dumps(e) + "\n" for e in entries))
    for old in dropped:
        if RUN_ID.fullmatch(str(old.get("run", ""))):
            shutil.rmtree(state_dir(name) / "runs" / old["run"], ignore_errors=True)


def new_run_id(at):
    return datetime.fromtimestamp(at).strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


# -- one run ----------------------------------------------------------------------------------------------------------

def notify_end(schedule, entry):
    """The hook point at a run's end: a desktop notification, and the phone channels of `sandbox-ai notify` when this
    sandbox-ai has it (notify.py beside this file). Never an error."""
    if not schedule.get("notify", True):
        return []
    told = []
    ok = entry["status"] == "ok"
    what = f"scheduled run {schedule['name']}: {entry['status']}" + (
        f", {entry['changes']['files']} file(s) changed" if (entry.get("changes") or {}).get("files") else "")
    with contextlib.suppress(Exception):
        approvals = load_module("approvals")
        approvals.desktop_notification(f"sandbox-ai: {what}",
                                       f"sandbox-ai schedule report {schedule['name']} {entry['run']}",
                                       urgency="normal" if ok else "critical")
        told.append("desktop")
    if (HERE / "notify.py").is_file():
        with contextlib.suppress(Exception):
            notify = load_module("notify")
            notify.tell("run-ended" if ok else "run-failed", entry.get("start"), what)
            notify.finish(5)
            told.append("notify")
    return told


def stop_process(process, start_name, run=subprocess.run):
    """A run past its time: its start stopped (sandbox-ai stop), then its launcher's process group ended."""
    with contextlib.suppress(Exception):
        run([*main_command(), "stop", start_name], capture_output=True, timeout=STOP_WAIT + 30,
            stdin=subprocess.DEVNULL)
    for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 15)):
        if process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            os.killpg(process.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(wait)


def tail(path, lines=OUTPUT_TAIL):
    try:
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - (256 << 10)))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return redact(printable("\n".join(text.splitlines()[-lines:]), 64 << 10))


def summary_markdown(schedule, entry, said):
    reason = {"due": "due", "missed": "missed while the machine was off or asleep: run once on wake",
              "push": "a push to " + str(schedule.get("on_push")), "now": "run now"}.get(entry["reason"],
                                                                                          entry["reason"])
    ask = {"off": "off: what is not allowed was refused, nothing asked",
           "allow": "allow: every question was allowed (and recorded)",
           "on": "on: questions waited for an answer"}[schedule["ask"]]
    rows = [("Schedule", schedule["name"]), ("When", timing_text(schedule)), ("Why this run", reason),
            ("Start", entry.get("start") or "-"), ("Agent", entry.get("agent") or "-"), ("Ask", ask),
            ("Status", entry["status"] + (f" (exit status {entry['exit']})" if entry.get("exit") is not None
                                          else "")),
            ("Started", shown_time(entry.get("started"))), ("Duration", f"{round(entry.get('seconds') or 0)} s"),
            ("Workspace", schedule["workdir"])]
    if entry.get("due"):
        rows.insert(3, ("Due", shown_time(entry["due"])))
    out = [f"# Scheduled run {schedule['name']} {entry['run']}", "", "|   |   |", "|---|---|"]
    out += [f"| {key} | {printable(str(value), 300).replace('|', '/')} |" for key, value in rows]
    out += ["", "## What the agent changed", ""]
    found = entry.get("changes")
    if found is None:
        out.append(said.get("changes") or "The workspace is no Git repository: no summary of the changes.")
    elif not (found["files"] or found["commits"]):
        out.append("Nothing changed in the workspace.")
    else:
        out.append(f"{found['files']} file(s) changed, {found['insertions']} insertion(s), "
                   f"{found['deletions']} deletion(s); {found['commits']} new commit(s).")
        if found.get("stat"):
            out += ["", "```", printable(found["stat"], 20000).replace("```", "'''"), "```"]
    if schedule.get("commit"):
        out += ["", f"Branch {schedule['commit']}: " + (entry.get("branch") or said.get("branch") or "not committed")]
    if schedule.get("push"):
        out.append(f"Push to {schedule['push']}: " + (entry.get("pushed") or said.get("push") or "not pushed"))
    out += ["", "## Report", ""]
    out.append("The start's report: [report.md](report.md) (`sandbox-ai report " + str(entry.get("start")) + "`)"
               if entry.get("report") else "No report: " + (said.get("report") or "the run did not start"))
    output = said.get("output")
    if output:
        out += ["", "## The end of its output", "", "```", output.replace("```", "'''"), "```"]
    return "\n".join(out) + "\n"


def run_once(name, reason="now", due=None, clock=now, run=subprocess.run, popen=subprocess.Popen, quiet=True):
    """One run of schedule NAME, in this process: the lock, the start (with its time limit), the summary, the report,
    the history, the notifications. The history entry; status "skipped" when a run of NAME runs already."""
    schedule = load(name)
    started = clock()
    run_id = new_run_id(started)
    lock = Lock(state_dir(name) / "lock")
    if not lock.got:
        entry = {"run": run_id, "reason": reason, "due": due, "started": started, "ended": started, "seconds": 0,
                 "status": "skipped", "note": "the previous run still runs"}
        record(name, entry)
        return entry
    with lock:
        ended_mid_way(name, clock(), schedule)
        directory = private_dir(state_dir(name) / "runs" / run_id)
        stamp = datetime.fromtimestamp(started).strftime("%m%d%H%M%S")
        start_name = f"{name}-{stamp}"
        entry = {"run": run_id, "schedule": name, "reason": reason, "due": due, "started": started, "start": start_name,
                 "agent": schedule.get("agent"), "ask": schedule["ask"], "status": "error", "exit": None,
                 "report": None, "summary": None, "changes": None, "branch": None, "pushed": None}
        said = {}
        write_private(state_dir(name) / "current.json", json.dumps({"run": run_id, "pid": os.getpid(),
                                                                     "start": start_name, "started": started}))
        try:
            argv = start_argv(schedule, start_name)
            entry["agent"] = argv[0]
            workdir = schedule["workdir"]
            if not os.path.isdir(workdir):
                raise ValueError(f"the workspace {workdir} is not there")
            before = None
            try:
                before = snapshot(workdir, directory)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                said["changes"] = f"No summary of the changes: {printable(exc, 300)}"
            # Named before it starts: a run killed mid-way (the machine shut down) leaves a kept start that the next
            # runs' prune_starts deletes with the other old ones.
            state = load_state(name)
            state["starts"] = [s for s in state.get("starts", []) if isinstance(s, str) and s != start_name] + \
                [start_name]
            save_state(name, state)
            output = directory / "output.log"
            env = run_environment(schedule)
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                LOG.info("schedule", f"{name}: run {run_id} starts {start_name} ({reason})")
                process = popen([*main_command(), *argv], cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                stdout=fd, stderr=fd, start_new_session=True)
            finally:
                os.close(fd)
            begun = time.monotonic()
            try:
                status = process.wait(schedule["limit"])
                entry["status"] = "ok" if status == 0 else "failed"
            except subprocess.TimeoutExpired:
                stop_process(process, start_name, run)
                status = process.poll()
                entry["status"] = "timeout"
                entry["note"] = f"stopped after its time limit of {round(schedule['limit'] / 60)} min"
            entry["exit"] = status
            entry["seconds"] = round(time.monotonic() - begun, 1)
            said["output"] = tail(output)
            if before is not None:
                try:
                    after = snapshot(workdir, directory)
                    entry["changes"] = changes(workdir, before, after)
                    if schedule.get("commit") and (entry["changes"]["files"] or entry["changes"]["commits"]):
                        commit = commit_changes(workdir, after, schedule["commit"],
                                                f"sandbox-ai schedule {name}: run {run_id}\n\nStart {start_name}, "
                                                f"status {entry['status']}.", before=before, scratch=directory)
                        entry["branch"] = f"{schedule['commit']} at {commit[:12]}"
                        if schedule.get("push"):
                            try:
                                entry["pushed"] = push_branch(workdir, schedule["push"], schedule.get("push_url"),
                                                              schedule["commit"], run)
                            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                                said["push"] = f"not pushed: {printable(exc, 300)}"
                    elif schedule.get("commit"):
                        said["branch"] = "nothing to commit"
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    said.setdefault("changes", f"No summary of the changes: {printable(exc, 300)}")
                    said["branch"] = f"not committed: {printable(exc, 300)}"
            report = directory / "report.md"
            try:
                made = run([*main_command(), "report", start_name, "-o", str(report), "--format", "md"],
                           capture_output=True, text=True, timeout=REPORT_TIMEOUT, stdin=subprocess.DEVNULL)
                if made.returncode == 0 and report.is_file():
                    entry["report"] = f"runs/{run_id}/report.md"
                else:
                    lines = (made.stderr or made.stdout).strip().splitlines()
                    said["report"] = printable(lines[-1] if lines else f"exit status {made.returncode}", 300)
            except (OSError, subprocess.SubprocessError) as exc:
                said["report"] = printable(exc, 300)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            entry["status"] = "error"
            entry["note"] = printable(exc, 500)
            said["report"] = "the run did not start: " + entry["note"]
        finally:
            entry["ended"] = clock()
            entry.setdefault("seconds", round(entry["ended"] - started, 1))
            with contextlib.suppress(OSError, ValueError):
                write_private(directory / "summary.md", summary_markdown(schedule, entry, said))
                entry["summary"] = f"runs/{run_id}/summary.md"
            entry["said"] = {key: value for key, value in said.items() if key != "output"}
            record(name, entry)
            with contextlib.suppress(OSError):
                (state_dir(name) / "current.json").unlink()
            LOG.info("schedule", f"{name}: run {run_id} ended: {entry['status']}")
        prune_starts(name, schedule, start_name, run)
        entry["told"] = notify_end(schedule, entry)
        return entry


def ended_mid_way(name, at, schedule=None):
    """A run of NAME whose process was killed (SIGKILL, the machine off) left its mark (current.json) and no history
    entry: recorded now (the caller holds the lock: that run is over), with a summary of what its output says."""
    path = state_dir(name) / "current.json"
    try:
        mark = json.loads(read_private(path))
    except (OSError, ValueError, RecursionError):
        return None
    with contextlib.suppress(OSError):
        path.unlink()
    if not isinstance(mark, dict) or not RUN_ID.fullmatch(str(mark.get("run", ""))) or \
            any(e.get("run") == mark["run"] for e in history(name)):
        return None
    started = mark.get("started") if isinstance(mark.get("started"), (int, float)) else at
    ended = at  # (the last its output was written, when there is any: about when it was killed)
    with contextlib.suppress(OSError):
        ended = min(at, max(started, os.stat(state_dir(name) / "runs" / mark["run"] / "output.log").st_mtime))
    entry = {"run": mark["run"], "schedule": name, "reason": "due", "due": None, "started": started, "ended": ended,
             "seconds": round(max(0.0, ended - started), 1),
             "start": printable(str(mark.get("start") or ""), 80) or None, "status": "error",
             "agent": (schedule or {}).get("agent"), "ask": (schedule or {}).get("ask"), "exit": None,
             "report": None, "summary": None, "changes": None,
             "note": "ended mid-way: its process was killed (the machine shut down or slept, the session ended)"}
    directory = state_dir(name) / "runs" / mark["run"]
    if schedule is not None and directory.is_dir() and not directory.is_symlink():
        with contextlib.suppress(OSError, ValueError, KeyError):
            said = {"report": "the run " + entry["note"], "output": tail(directory / "output.log"),
                    "changes": "No summary of the changes: the run " + entry["note"]}
            write_private(directory / "summary.md", summary_markdown(schedule, entry, said))
            entry["summary"] = f"runs/{mark['run']}/summary.md"
    record(name, entry)
    return entry


def prune_starts(name, schedule, start_name, run=subprocess.run):
    """The kept starts of NAME beyond its newest keep_starts are deleted (their reports stay in the history)."""
    state = load_state(name)
    starts = [s for s in state.get("starts", []) if isinstance(s, str) and s != start_name] + [start_name]
    keep = schedule.get("keep_starts", DEFAULT_KEEP_STARTS)
    old, starts = (starts[:-keep], starts[-keep:]) if keep else (starts, [])
    for start in old:
        with contextlib.suppress(Exception):
            run([*main_command(), "delete", start, "--yes"], capture_output=True, timeout=300,
                stdin=subprocess.DEVNULL)
    state = load_state(name)
    state["starts"] = starts
    save_state(name, state)


def spawn(name, reason, due=None):
    """A run of NAME in a process of its own (it outlives the tick that starts it)."""
    argv = [sys.executable, "-I", str(HERE / "schedule.py"), "_run", name, "--reason", reason,
            *(["--due", str(due)] if due is not None else [])]
    env = dict(os.environ)
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, env=env, close_fds=True)
    return process.pid


# -- the tick ---------------------------------------------------------------------------------------------------------

def decide(schedule, state, at, tz=None, busy=None):
    """What a tick at AT does for SCHEDULE with its STATE: (reason or None, due, notes, new state). BUSY(): whether a
    run of it holds its lock now."""
    tz = tz or local_zone()
    state = dict(state)
    notes = []
    if schedule.get("paused"):
        return None, None, notes, state
    busy = busy or (lambda: running(schedule["name"]))
    reason, due = None, None
    if schedule.get("timing"):
        last = state.get("last_due")
        last = float(schedule.get("created") or at) if last is None else float(last)
        count, latest = fires_between(schedule, last, at, tz)
        if count:
            state["last_due"] = latest
            late = at - latest > GRACE
            if late and schedule["missed"] == "skip":
                notes.append(f"{count} run(s) missed while the machine was off or asleep: skipped (--missed skip)")
            elif busy():
                notes.append("the previous run still runs: this one is skipped")
            else:
                reason, due = ("missed" if late else "due"), latest
                if count > 1:
                    notes.append(f"{count} times due since the last tick: one run")
    if schedule.get("on_push"):
        digest = refs_digest(schedule["on_push"])
        if digest is not None:
            if state.get("push_seen") is None:
                state["push_seen"] = digest
            elif digest != state["push_seen"] and reason is None and not busy():
                reason, due = "push", at
                state["push_seen"] = digest
            elif digest != state["push_seen"] and reason is not None:
                state["push_seen"] = digest  # (one run for both)
    return reason, due, notes, state


def tick(clock=now, starter=None, tz=None):
    """Start what is due now (each in a process of its own) and note what was skipped. [(name, reason)] started."""
    starter = starter or spawn
    root = private_dir(state_root())
    started = []
    with Lock(root / "tick.lock") as lock:
        if not lock.got:
            return started
        at = clock()
        for name in names():
            try:
                schedule = load(name)
                state = load_state(name)
                reason, due, notes, state = decide(schedule, state, at, tz)
                for note in notes:
                    record(name, {"run": new_run_id(at), "reason": "due", "due": state.get("last_due"),
                                  "started": at, "ended": at, "seconds": 0, "status": "skipped", "note": note})
                save_state(name, state)
                if reason:
                    starter(name, reason, due)
                    started.append((name, reason))
            except Exception as exc:  # noqa: BLE001 - one schedule never holds up the others
                LOG.warning("schedule", f"{name}: not looked at", exc)
        write_private(root / "tick.json", json.dumps({"last": at}))
    return started


def last_tick():
    try:
        return float(json.loads(read_private(state_root() / "tick.json")).get("last"))
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None


# -- the timer --------------------------------------------------------------------------------------------------------

def timer_kind():
    return "launchd" if sys.platform == "darwin" else "systemd"


def systemd_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


# The first line of each systemd unit sandbox-ai writes: a file of that name without it is not ours, never touched.
UNIT_MARKER = "# Written by sandbox-ai schedule enable; removed by schedule disable and sandbox-ai uninstall."


def schedule_timer():
    """The schedules' timer: what its files are called, what they run and how often. The update timer (updates.py)
    is another such description, made, checked and removed by the same functions below."""
    return {"name": TIMER_NAME, "label": LAUNCHD_LABEL, "marker": UNIT_MARKER, "args": ["schedule", "tick"],
            "command": tick_command(), "description": "sandbox-ai schedules: start what is due (sandbox-ai schedule tick)",
            "every": "every minute", "calendar": "*-*-* *:*:00", "accuracy": "5s", "interval": 60,
            "log": state_root() / "tick.log", "said": "sandbox-ai schedule", "on": "the timer runs every minute",
            "off": "the timer is off (the schedules stay; sandbox-ai schedule enable starts it again)",
            "none": "no timer of sandbox-ai's was on",
            "no_systemctl": "no systemctl here: start `sandbox-ai schedule tick` every minute yourself (cron, a loop)"}


def launchd_path(timer=None):
    return Path.home() / "Library" / "LaunchAgents" / f"{(timer or {}).get('label', LAUNCHD_LABEL)}.plist"


def ours(path, timer=None):
    """Whether PATH is a timer file sandbox-ai wrote: a systemd unit that starts with the timer's marker, or the
    LaunchAgent with its label that runs its command (`schedule tick`). A symlink, someone else's file of that name:
    not ours."""
    timer = timer or schedule_timer()
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    try:
        if path.suffix == ".plist":
            data = plistlib.loads(path.read_bytes())
            return isinstance(data, dict) and data.get("Label") == timer["label"] and \
                list(data.get("ProgramArguments") or [])[-len(timer["args"]):] == timer["args"]
        return path.read_text(errors="replace").startswith(timer["marker"] + "\n")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def timer_files(platform=None, timer=None):
    """The timer files sandbox-ai wrote that are there now (only ours: see ours())."""
    timer = timer or schedule_timer()
    if (platform or sys.platform) == "darwin":
        candidates = [launchd_path(timer)]
    else:
        candidates = [systemd_dir() / f"{timer['name']}{suffix}" for suffix in (".timer", ".service")]
    return [str(path) for path in candidates if ours(path, timer)]


def systemd_quote(word):
    """WORD as one argument of ExecStart= or a value of Environment= (systemd's quoting: % and $ doubled)."""
    escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def tick_command():
    return [sys.executable, "-I", str(HERE / "main.py"), "schedule", "tick"]


def systemd_units(command=None, path=None, timer=None):
    """{file name: text} of the systemd user service and timer that run the tick every minute (TIMER: another
    timer's description, with schedule_timer's keys)."""
    timer = timer or schedule_timer()
    command = command or timer["command"]
    path = path if path is not None else os.environ.get("PATH", "/usr/bin:/bin")
    service = "\n".join([
        timer["marker"], "[Unit]", f"Description={timer['description']}", "",
        "[Service]", "Type=oneshot", "ExecStart=" + " ".join(systemd_quote(word) for word in command),
        "Environment=" + systemd_quote(f"PATH={path}"),
        "# The runs it starts outlive the tick: only the tick itself ends with it.", "KillMode=process", ""])
    units = "\n".join([
        timer["marker"], "[Unit]", f"Description={timer['description'].split(':')[0]}: {timer['every']}", "",
        "[Timer]", f"OnCalendar={timer['calendar']}", f"AccuracySec={timer['accuracy']}", "Persistent=true", "",
        "[Install]", "WantedBy=timers.target", ""])
    return {f"{timer['name']}.service": service, f"{timer['name']}.timer": units}


def launchd_plist(command=None, path=None, log=None, timer=None):
    """The LaunchAgent's plist (bytes) that runs the tick every minute and once at load (after a wake too)."""
    timer = timer or schedule_timer()
    command = command or timer["command"]
    path = path if path is not None else os.environ.get("PATH", "/usr/bin:/bin")
    log = str(log or timer["log"])
    return plistlib.dumps({"Label": timer["label"], "ProgramArguments": list(command),
                           "StartInterval": timer["interval"], "RunAtLoad": True, "AbandonProcessGroup": True,
                           "ProcessType": "Background", "EnvironmentVariables": {"PATH": path},
                           "StandardOutPath": log, "StandardErrorPath": log})


def timer_state(run=subprocess.run, timer=None):
    """{"kind", "installed", "active" (None: not known), "files", "last_tick"} (last_tick: the schedules' only)."""
    given = timer is not None
    timer = timer or schedule_timer()
    kind = timer_kind()
    files = [str(launchd_path(timer))] if kind == "launchd" else \
        [str(systemd_dir() / name) for name in systemd_units(timer=timer)]
    installed = all(os.path.isfile(path) for path in files)
    active = None
    if installed:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if kind == "systemd" and shutil.which("systemctl"):
                active = run(["systemctl", "--user", "is-active", "--quiet", f"{timer['name']}.timer"],
                             capture_output=True, timeout=10, stdin=subprocess.DEVNULL).returncode == 0
            elif kind == "launchd" and shutil.which("launchctl"):
                active = run(["launchctl", "print", f"gui/{os.getuid()}/{timer['label']}"], capture_output=True,
                             timeout=10, stdin=subprocess.DEVNULL).returncode == 0
    return {"kind": kind, "installed": installed, "active": active, "files": files,
            "last_tick": None if given else last_tick()}


def enable(dry_run=False, run=subprocess.run, out=print, timer=None):
    """Install and start the user-level timer (DRY_RUN: only show what would be written)."""
    timer = timer or schedule_timer()
    kind = timer_kind()
    if kind == "launchd":
        path = launchd_path(timer)
        data = launchd_plist(timer=timer)
        if dry_run:
            out(f"# {path}\n{data.decode()}")
            return 0
        private_dir(Path(timer["log"]).parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{timer['label']}"], capture_output=True, timeout=30,
            stdin=subprocess.DEVNULL)
        done = run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True, timeout=30,
                   stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise ValueError(f"launchctl bootstrap: {(done.stderr or '').strip()[-300:]}")
        out(f"{timer['said']}: {timer['on']} (LaunchAgent {path})")
        return 0
    units = systemd_units(timer=timer)
    if dry_run:
        for file, text in units.items():
            out(f"# {systemd_dir() / file}\n{text}")
        return 0
    if not shutil.which("systemctl"):
        raise ValueError(timer["no_systemctl"])
    private_dir(systemd_dir())
    for file, text in units.items():
        (systemd_dir() / file).write_text(text)
    for argv in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "--now", f"{timer['name']}.timer"]):
        done = run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise ValueError(f"{' '.join(argv[:3])}: {(done.stderr or '').strip()[-300:]}")
    out(f"{timer['said']}: {timer['on']} ({timer['name']}.timer, systemd --user)")
    out("  note: a user's timers run while that user is logged in; to run them without a login: "
        "loginctl enable-linger (yours to decide)")
    return 0


def disable(run=subprocess.run, out=print, timer=None):
    """The timer off and its files gone - only those sandbox-ai wrote (ours()): a unit or LaunchAgent of that name
    that is someone else's stays as it is. The files removed."""
    timer = timer or schedule_timer()
    kind = timer_kind()
    found = timer_files("darwin" if kind == "launchd" else "linux", timer)
    if kind == "launchd":
        if found:
            run(["launchctl", "bootout", f"gui/{os.getuid()}/{timer['label']}"], capture_output=True, timeout=30,
                stdin=subprocess.DEVNULL)
    elif found and shutil.which("systemctl"):
        run(["systemctl", "--user", "disable", "--now", f"{timer['name']}.timer"], capture_output=True, timeout=60,
            stdin=subprocess.DEVNULL)
    for file in found:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(file)
    if found and kind == "systemd" and shutil.which("systemctl"):
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    others = [str(path) for path in ([launchd_path(timer)] if kind == "launchd" else
                                     [systemd_dir() / name for name in systemd_units(timer=timer)])
              if os.path.lexists(path) and str(path) not in found]
    out(f"{timer['said']}: {timer['off'] if found else timer['none']}")
    for path in others:
        out(f"  left as it is: {path} (not written by sandbox-ai)")
    return found


# -- add --------------------------------------------------------------------------------------------------------------

def build(a, start, at=None, previous=None):
    """The schedule document of `schedule add`'s options A and its start words START."""
    at = now() if at is None else at
    name = checked_name(a.name)
    timings = [(kind, value) for kind, value in (("every", a.every), ("at", a.at), ("cron", a.cron)) if value]
    if len(timings) > 1:
        raise ValueError("one of --every, --at and --cron")
    timing = None
    if timings:
        kind, value = timings[0]
        value = " ".join(str(value).split())
        timing = {"kind": kind, "value": value}
        if kind == "at" and "once" in parse_at(value) and next_due({"timing": timing}, at) is None:
            raise ValueError(f"--at {printable(value, 40)}: that time has passed (a schedule that would never run)")
    if timing is None and not a.on_push:
        raise ValueError("when it runs: --every SPEC, --at TIME, --cron \"M H DOM MON DOW\" or --on-push REPO")
    agent, options, agent_args = split_start(start)
    board = None
    if a.board:
        document = board_document(a.board)
        if agent and agent != document["agent"]:
            raise ValueError(f"the board {a.board} starts {document['agent']}, not {agent}")
        agent, board = document["agent"], a.board
    if not agent:
        raise ValueError("the agent to start: -- AGENT [OPTIONS] [-- AGENT ARGS] (e.g. -- claude -w ~/src/app)")
    if agent not in ONESHOT_HINT:
        raise ValueError(f"no agent {agent!r} (one of {', '.join(ONESHOT_HINT)})")
    prompt = a.prompt
    if a.prompt_file:
        if prompt:
            raise ValueError("--prompt or --prompt-file, not both")
        prompt = read_private(Path(a.prompt_file).expanduser(), PROMPT_LIMIT) if str(a.prompt_file) != "-" else \
            sys.stdin.read(PROMPT_LIMIT + 1)
    if prompt is not None:
        prompt = prompt.strip("\n")
        if not prompt.strip():
            raise ValueError("--prompt: an empty task")
    workdir = os.path.abspath(os.path.expanduser(a.workdir or os.getcwd()))
    if not os.path.isdir(workdir):
        raise ValueError(f"--workdir {workdir}: no such folder")
    on_push = os.path.abspath(os.path.expanduser(a.on_push)) if a.on_push else None
    if on_push and refs_digest(on_push) is None:
        raise ValueError(f"--on-push {on_push}: not a Git repository")
    limit = parse_duration(a.limit) if a.limit else DEFAULT_LIMIT
    if not LIMIT_RANGE[0] <= limit <= LIMIT_RANGE[1]:
        raise ValueError("--limit: from 10s to 24h")
    commit = check_branch(a.commit) if a.commit else None
    if a.push and not commit:
        raise ValueError("--push REMOTE needs --commit BRANCH (the branch it pushes)")
    if commit and not is_repository(workdir):
        raise ValueError(f"--commit: the workspace {workdir} is no Git repository")
    if commit:
        git_folder(workdir, commit)  # (a linked worktree, a submodule: said now, not at the first run)
    push_url = remote_url(workdir, a.push) if a.push else None
    if a.preset:
        preset = load_module("presets").load(a.preset)
        if agent not in preset.get("agents", list(ONESHOT_HINT)):
            raise ValueError(f"preset {a.preset} is for {', '.join(preset.get('agents', []))}, not {agent}")
    keep = a.keep_starts if a.keep_starts is not None else DEFAULT_KEEP_STARTS
    document = {"version": VERSION, "name": name,
                "created": previous["created"] if previous and previous.get("timing") == timing else at,
                "timing": timing, "on_push": on_push, "agent": agent, "options": list(options),
                "agent_args": list(agent_args), "prompt": prompt, "preset": a.preset,
                "preset_values": list(a.preset_value or []), "board": board, "workdir": workdir, "ask": a.ask,
                "limit": limit, "missed": a.missed, "keep_starts": keep, "commit": commit, "push": a.push,
                "push_url": push_url, "notify": not a.no_notify,
                "paused": bool(previous.get("paused")) if previous else False}
    check_schedule(document)
    if options:
        document["options"] = check_options(agent, options)
    agent_args_of(document if not board else {**document, "agent": agent})  # a task it can run
    return document


def add_main(a, start, out=print):
    previous = None
    with contextlib.suppress(ValueError):
        previous = load(a.name)
    if previous is not None and not a.replace:
        raise ValueError(f"there is a schedule {a.name} already: --replace to change it")
    at = now()
    document = build(a, start, at, previous)
    save(document)
    state = load_state(a.name)
    if previous is None or previous.get("timing") != document["timing"]:
        state["last_due"] = at  # (the first run: the first time due from now on)
    save_state(a.name, state)
    following = next_due(document, at) if document.get("timing") else None
    out(f"sandbox-ai schedule: {a.name} {'changed' if previous else 'added'} - {timing_text(document)}; "
        f"next run {shown_time(following) if following else '-'}; questions: --ask {document['ask']}"
        + (" (refused, nobody answers)" if document["ask"] == "off" else ""))
    timer = timer_state()
    if not timer["installed"]:
        out("  the timer is not on yet: sandbox-ai schedule enable")
    return 0


def add_arguments(fields):
    """`sandbox-ai schedule add` words of FIELDS (the app's and the web interface's form: name, every|at|cron,
    on_push, agent, options (a list or a shell-quoted text), agent_args, prompt, preset, preset_values, board,
    workdir, ask, limit, missed, keep_starts, commit, push, notify, replace), checked as the command checks them."""
    def text(key):
        value = fields.get(key)
        if value in (None, ""):
            return None
        if not isinstance(value, (str, int)):
            raise ValueError(f"{key}: a text")
        value = str(value)
        if len(value) > (PROMPT_LIMIT if key == "prompt" else 4096) or any(
                not (c.isprintable() or (key == "prompt" and c in "\n\t")) for c in value):
            raise ValueError(f"{key}: not a text sandbox-ai takes")
        return value

    def words(key):
        value = fields.get(key)
        if value in (None, ""):
            return []
        if isinstance(value, str):
            try:
                value = shlex.split(value)
            except ValueError as exc:
                raise ValueError(f"{key}: {exc}") from None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key}: a list of texts")
        return value
    name = checked_name(text("name"))
    args = ["schedule", "add"]
    timings = [(key, text(key)) for key in ("every", "at", "cron") if text(key)]
    if len(timings) > 1:
        raise ValueError("one of every, at and cron")
    for key, value in timings:
        {"every": parse_every, "at": parse_at, "cron": Cron}[key](value)
        args.append(f"--{key}={value}")
    if text("on_push"):
        args.append(f"--on-push={text('on_push')}")
    if not timings and not text("on_push"):
        raise ValueError("when it runs: every, at, cron or on_push")
    for key in ("prompt", "preset", "board", "workdir", "commit", "push"):
        if text(key):
            args.append(f"--{key.replace('_', '-')}={text(key)}")
    for value in words("preset_values"):
        args.append(f"--preset-value={value}")
    ask = text("ask") or "off"
    if ask not in ASK_MODES:
        raise ValueError("ask: off, allow or on")
    args.append(f"--ask={ask}")
    if text("limit"):
        parse_duration(text("limit"))
        args.append(f"--limit={text('limit')}")
    missed = text("missed") or "once"
    if missed not in MISSED:
        raise ValueError("missed: once or skip")
    args.append(f"--missed={missed}")
    if fields.get("keep_starts") not in (None, ""):
        keep = fields["keep_starts"]
        if isinstance(keep, bool) or not str(keep).isdigit() or int(keep) > 1000:
            raise ValueError("keep_starts: 0-1000")
        args.append(f"--keep-starts={int(keep)}")
    if fields.get("notify") is False:
        args.append("--no-notify")
    if fields.get("replace"):
        args.append("--replace")
    agent = text("agent")
    if agent is None and not text("board"):
        raise ValueError("agent: the agent to start")
    if agent is not None and agent not in ONESHOT_HINT:
        raise ValueError(f"no agent {agent!r}")
    options, agent_args = words("options"), words("agent_args")
    if "--" in options:
        raise ValueError("options: the start's options, without --")
    return [*args, name, "--", *([agent] if agent else []), *options, *(["--", *agent_args] if agent_args else [])]


# -- the commands -----------------------------------------------------------------------------------------------------

def summary_of(name, at=None, timer=None):
    """The row of NAME for list --json: its settings, the next and the last run."""
    at = now() if at is None else at
    schedule = load(name)
    entries = history(name)
    last = next((e for e in reversed(entries) if e.get("status") != "skipped"), None)
    following = None
    state = load_state(name)
    if schedule.get("timing") and not schedule.get("paused"):
        with contextlib.suppress(ValueError):
            following = next_due(schedule, max(at, float(state.get("last_due") or 0)))
    busy = running(name)
    return {"name": name, "when": timing_text(schedule), "timing": schedule.get("timing"),
            "on_push": schedule.get("on_push"), "agent": schedule.get("agent"), "board": schedule.get("board"),
            "preset": schedule.get("preset"), "prompt": schedule.get("prompt"), "options": schedule.get("options"),
            "agent_args": schedule.get("agent_args"), "workdir": schedule["workdir"], "ask": schedule["ask"],
            "limit": schedule["limit"], "missed": schedule["missed"], "keep_starts": schedule["keep_starts"],
            "commit": schedule.get("commit"), "push": schedule.get("push"), "notify": schedule.get("notify", True),
            "paused": schedule.get("paused", False), "running": busy,
            "state": "running" if busy else "paused" if schedule.get("paused") else
            "done" if schedule.get("timing", {}) and following is None and not schedule.get("on_push") else "active",
            "next": following, "last": last, "runs": len([e for e in entries if e.get("status") != "skipped"])}


def list_main(a, out=print):
    rows, problems = [], []
    for name in names():
        try:
            rows.append(summary_of(name))
        except ValueError as exc:
            problems.append(printable(exc, 300))
    timer = timer_state()
    if a.json:
        print(json.dumps({"schedules": rows, "problems": problems, "timer": timer}, indent=2))
        return 0
    t = load_module("textout")
    t.table(["NAME", "WHEN", "AGENT", "ASK", "NEXT", "LAST", "STATE"],
            [[row["name"], row["when"], row["agent"] or "-", row["ask"], shown_time(row["next"]),
              (f"{row['last']['status']} {shown_time(row['last'].get('started'))}" if row["last"] else "-"),
              row["state"]] for row in rows], out, indent=0, empty="no schedules (sandbox-ai schedule add)")
    for problem in problems:
        out(f"  problem: {problem}")
    out("")
    out("timer: " + ("on" if timer["installed"] and timer["active"] is not False else
                     "installed but not active" if timer["installed"] else
                     "off - sandbox-ai schedule enable") + (
        f"; last tick {shown_time(timer['last_tick'])}" if timer["last_tick"] else ""))
    return 0


def show_main(a, out=print):
    schedule = load(a.name)
    row = summary_of(a.name)
    try:
        command = shlex.join(["sandbox-ai", *start_argv(schedule, f"{a.name}-MMDDhhmmss")])
    except ValueError as exc:
        command = f"(cannot start: {printable(exc, 300)})"
    upcoming, at = [], max(now(), float(load_state(a.name).get("last_due") or 0))
    if schedule.get("timing") and not schedule.get("paused"):
        for _ in range(5):
            due = next_due(schedule, at)
            if due is None:
                break
            upcoming.append(due)
            at = due
    if a.json:
        print(json.dumps({**row, "command": command, "upcoming": upcoming,
                          "history": history(a.name, 20)}, indent=2))
        return 0
    t = load_module("textout")
    t.pairs([("Name", a.name), ("When", row["when"]), ("State", row["state"]),
             ("Agent", row["agent"] + (f" (board {row['board']})" if row["board"] else "")),
             ("Preset", row["preset"] or "-"), ("Task", printable(row["prompt"] or "(in the agent arguments)", 300)),
             ("Workspace", row["workdir"]),
             ("Ask", row["ask"] + (" - what is not allowed is refused, nobody answers" if row["ask"] == "off" else
                                   " - every question is allowed and recorded" if row["ask"] == "allow" else
                                   " - questions wait for an answer (sandbox-ai approvals)")),
             ("Time limit", f"{round(row['limit'] / 60, 1):g} min"), ("Missed runs", row["missed"]),
             ("Kept starts", str(row["keep_starts"])),
             ("Commit to", row["commit"] or "-"), ("Push to", row["push"] or "-"),
             ("Notify", "yes" if row["notify"] else "no"),
             ("Next", ", ".join(shown_time(due) for due in upcoming) or "-"), ("Start", command)], out, indent=0)
    return 0


def history_main(a, out=print):
    load(a.name)
    entries = history(a.name, a.limit)
    if a.json:
        print(json.dumps({"name": a.name, "runs": entries}, indent=2))
        return 0
    t = load_module("textout")
    t.table(["RUN", "WHY", "STARTED", "STATUS", "DURATION", "CHANGED", "REPORT"],
            [[e.get("run", "?"), e.get("reason", "-"), shown_time(e.get("started")), e.get("status", "?"),
              f"{round(e.get('seconds') or 0)} s",
              (f"{e['changes']['files']} file(s), {e['changes']['commits']} commit(s)" if e.get("changes") else "-"),
              str(state_dir(a.name) / e["summary"]) if e.get("summary") else (e.get("note") or "-")]
             for e in entries], out, indent=0, empty="no runs yet")
    return 0


def run_entry(name, run=None):
    entries = [e for e in history(name) if e.get("summary") or e.get("report")]
    if run:
        found = [e for e in entries if e.get("run") == run]
        if not found:
            raise ValueError(f"no run {run} of {name} (sandbox-ai schedule history {name})")
        return found[-1]
    if not entries:
        raise ValueError(f"{name} has no run with a report yet")
    return entries[-1]


def report_text(name, run=None):
    """The Markdown of a run: its summary, then the start's report."""
    load(name)
    entry = run_entry(name, run)
    parts = []
    for key in ("summary", "report"):
        relative = entry.get(key)
        if relative and re.fullmatch(r"runs/[0-9]{8}-[0-9]{6}-[0-9a-f]{4}/(summary|report)\.md", relative):
            with contextlib.suppress(OSError, ValueError):
                parts.append(read_private(state_dir(name) / relative, 32 << 20))
    return entry, "\n\n---\n\n".join(parts)


def report_main(a, out=print):
    _entry, text = report_text(a.name, a.run)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def set_paused(name, paused, out=print):
    schedule = load(name)
    schedule["paused"] = paused
    save(schedule)
    if not paused:
        state = load_state(name)
        state["last_due"] = now()  # what was due while paused is not made up
        if schedule.get("on_push"):  # nor a push while paused: the branches as they are now are the ones seen
            digest = refs_digest(schedule["on_push"])
            if digest is not None:
                state["push_seen"] = digest
        save_state(name, state)
    out(f"sandbox-ai schedule: {name} {'paused' if paused else 'goes on'}")
    return 0


def remove_main(a, out=print):
    path = schedule_path(a.name)
    if not path.exists():
        raise ValueError(f"no schedule {a.name} (sandbox-ai schedule list)")
    if running(a.name):
        raise ValueError(f"a run of {a.name} runs now: let it end (or stop its start), then remove it")
    path.unlink()
    if not a.keep_history:
        shutil.rmtree(state_dir(a.name), ignore_errors=True)
    out(f"sandbox-ai schedule: {a.name} removed" + (" (its history stays)" if a.keep_history else ""))
    return 0


def run_now_main(a, out=print):
    load(a.name)
    if running(a.name):
        raise ValueError(f"a run of {a.name} runs already")
    if a.wait:
        entry = run_once(a.name, "now")
        out(f"sandbox-ai schedule: {a.name} run {entry['run']}: {entry['status']}"
            + (f" - {entry['note']}" if entry.get("note") else ""))
        if entry.get("summary"):
            out(f"  summary: {state_dir(a.name) / entry['summary']}")
        return 0 if entry["status"] == "ok" else 1
    spawn(a.name, "now")
    out(f"sandbox-ai schedule: {a.name} started; see sandbox-ai schedule history {a.name}")
    return 0


def help_style(prog):
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai schedule", formatter_class=help_style, allow_abbrev=False,
                                description="Start an agent on a timetable or a trigger, with a report afterwards: "
                                            "each run a normal start under its rules, its questions answered by the "
                                            "schedule's --ask (default off: refused, nobody answers at night).")
    commands = p.add_subparsers(dest="command", required=True)
    add = commands.add_parser(
        "add", help="Add a schedule (or change one: --replace)", formatter_class=help_style,
        usage="sandbox-ai schedule add NAME --every|--at|--cron WHEN [OPTIONS] -- AGENT [OPTIONS] [-- ARGS]",
        description="A schedule NAME: when it runs, and the start - an agent with its options as `sandbox-ai AGENT` "
                    "takes them - after --. The task: --prompt (put into the agent's one-shot mode: claude -p, codex "
                    "exec, opencode run ...), or the agent's one-shot form after the start's own --.",
        epilog="Examples:\n"
               "  sandbox-ai schedule add nightly --at 02:30 --prompt \"Review the commits\" -- claude\n"
               "  sandbox-ai schedule add deps --cron \"0 6 * * 1\" --commit deps -- codex -- exec \"Update\"\n"
               "  sandbox-ai schedule add triage --on-push ~/src/app --prompt \"Triage\" -- qwen")
    add.add_argument("name", metavar="NAME", help="The schedule's name (1-24 of a-z 0-9 _ -)")
    when = add.add_argument_group("When")
    when.add_argument("--every", metavar="SPEC", help="Every 30m, 2h, 1d, 1w, 1h30m (real time, from now)")
    when.add_argument("--at", metavar="TIME", help="Every day at HH:MM, or once at \"YYYY-MM-DD HH:MM\" (local time)")
    when.add_argument("--cron", metavar="SPEC", help="Five cron fields \"M H DOM MON DOW\" (or @daily, @hourly ...)")
    when.add_argument("--on-push", type=Path, metavar="REPO", help="Also (or only) when a branch or tag of this local "
                                                        "repository moves")
    when.add_argument("--missed", choices=MISSED, default="once",
                      help="Runs missed while the machine was off or asleep: once on wake (default), or skip")
    what = add.add_argument_group("What")
    what.add_argument("--prompt", metavar="TEXT", help="The task, given to the agent's one-shot mode")
    what.add_argument("--prompt-file", type=Path, metavar="FILE", help="The task from FILE (- : standard input)")
    what.add_argument("--preset", metavar="NAME", help="Start with this preset (sandbox-ai presets list); its first "
                                                      "task is the task unless --prompt gives one")
    what.add_argument("--preset-value", action="append", metavar="KEY=VALUE", help="A value the preset asks for "
                                                                                    "(repeatable)")
    what.add_argument("--board", metavar="NAME", help="Start a board kept here (sandbox-ai board list): its agent "
                                                     "and options")
    what.add_argument("--workdir", type=Path, metavar="DIR", help="The workspace the start runs in (default: this folder)")
    rules = add.add_argument_group("Rules and afterwards")
    rules.add_argument("--ask", choices=ASK_MODES, default="off",
                       help="The run's questions: off - refused, nobody answers (default); allow - every one "
                            "allowed and recorded; on - they wait for an answer")
    rules.add_argument("--limit", metavar="DURATION", help="Time limit of a run, then it is stopped (default 60m)")
    rules.add_argument("--keep-starts", type=int, metavar="N",
                       help=f"Keep the newest N starts (their HOME) and delete older ones (default "
                            f"{DEFAULT_KEEP_STARTS}; reports stay)")
    rules.add_argument("--commit", metavar="BRANCH", help="Put what the agent changed on BRANCH (not the one checked "
                                                         "out; the workspace stays as it is)")
    rules.add_argument("--push", metavar="REMOTE", help="Push BRANCH to REMOTE after the privacy check (its address "
                                                       "as now)")
    rules.add_argument("--no-notify", action="store_true", help="No desktop or phone notification when a run ends")
    rules.add_argument("--replace", action="store_true", help="Change the schedule NAME (its history stays)")
    add.add_argument("start", nargs="*", metavar="-- AGENT", help="The start: AGENT [OPTIONS] [-- AGENT ARGS]")
    listing = commands.add_parser("list", help="Every schedule: when, agent, next and last run, the timer",
                                  formatter_class=help_style)
    listing.add_argument("--json", action="store_true", help="As JSON")
    show = commands.add_parser("show", help="One schedule: settings, start command, next times",
                               formatter_class=help_style)
    show.add_argument("name", metavar="NAME")
    show.add_argument("--json", action="store_true", help="As JSON (with the last runs)")
    hist = commands.add_parser("history", help="A schedule's runs: status, duration, changes, report",
                               formatter_class=help_style)
    hist.add_argument("name", metavar="NAME")
    hist.add_argument("--limit", type=int, metavar="N", help="Only the newest N")
    hist.add_argument("--json", action="store_true", help="As JSON")
    report = commands.add_parser("report", help="A run's summary and report (Markdown)", formatter_class=help_style)
    report.add_argument("name", metavar="NAME")
    report.add_argument("run", nargs="?", metavar="RUN", help="A run of its history (default: the last)")
    for command, text in (("pause", "Pause a schedule"), ("resume", "Go on with a paused schedule")):
        sub = commands.add_parser(command, help=text, formatter_class=help_style)
        sub.add_argument("name", metavar="NAME")
    now_ = commands.add_parser("run-now", help="One run now, as the timer would start it", formatter_class=help_style)
    now_.add_argument("name", metavar="NAME")
    now_.add_argument("--wait", action="store_true", help="Wait for its end and say how it went")
    remove = commands.add_parser("remove", help="Remove a schedule", formatter_class=help_style)
    remove.add_argument("name", metavar="NAME")
    remove.add_argument("--keep-history", action="store_true", help="Keep its history and reports")
    enable_ = commands.add_parser("enable", help="Install the user-level timer (systemd --user, launchd)",
                                  formatter_class=help_style)
    enable_.add_argument("--dry-run", action="store_true", help="Only show the files it would write")
    commands.add_parser("disable", help="Remove the timer (the schedules stay)", formatter_class=help_style)
    commands.add_parser("tick", help="Start what is due now (what the timer runs)", formatter_class=help_style)
    return p


def internal_run(argv):
    """`schedule.py _run NAME --reason R [--due T]`: one run, in the process a tick or run-now started."""
    p = argparse.ArgumentParser(prog="schedule.py _run")
    p.add_argument("name", help="The schedule")
    p.add_argument("--reason", default="now", choices=("due", "missed", "push", "now"), help="Why it runs")
    p.add_argument("--due", type=float, help="The time it was due (seconds)")
    a = p.parse_args(argv)
    with contextlib.suppress(ValueError, OSError, KeyError):  # (a schedule that cannot be read: run_once says so)
        os.environ["PATH"] = safe_path(os.environ.get("PATH", os.defpath), [load(a.name)["workdir"]])
    try:
        entry = run_once(a.name, a.reason, a.due)
    except Exception as exc:  # noqa: BLE001 - said in launcher.log; nobody watches this process
        LOG.error("schedule", f"{a.name}: the run failed to run", exc)
        return 1
    return 0 if entry["status"] == "ok" else 1


def main(argv, out=print):
    if argv[:1] == ["_run"]:
        return internal_run(argv[1:])
    start = []
    if argv[:1] == ["add"] and "--" in argv:
        position = argv.index("--")
        argv, start = argv[:position], argv[position + 1:]
    a = parser().parse_args(argv)
    try:
        if a.command == "add":
            return add_main(a, [*a.start, *start], out)
        if a.command == "list":
            return list_main(a, out)
        if a.command == "show":
            return show_main(a, out)
        if a.command == "history":
            return history_main(a, out)
        if a.command == "report":
            return report_main(a, out)
        if a.command in ("pause", "resume"):
            return set_paused(a.name, a.command == "pause", out)
        if a.command == "run-now":
            return run_now_main(a, out)
        if a.command == "remove":
            return remove_main(a, out)
        if a.command == "enable":
            return enable(a.dry_run, out=out)
        if a.command == "disable":
            disable(out=out)
            return 0
        tick()
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"sandbox-ai schedule {a.command}: {printable(exc, 1000)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
