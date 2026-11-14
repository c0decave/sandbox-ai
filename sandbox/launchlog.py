"""The launcher's own log: what a start (or another command of sandbox-ai) was asked, what it decided and why, which
outside commands it ran (podman, ssh, git, a build), how long they took and how they ended.

  $XDG_STATE_HOME/sandbox-ai/launcher.log    (~/.local/state/sandbox-ai/launcher.log)

One line each: "TIME PID LEVEL AREA: MESSAGE". INFO, WARNING and ERROR lines are always written (a start writes a few);
DEBUG lines - every outside command, every background check, every error a fallback takes over - only with
SANDBOX_AI_DEBUG=1 in the environment, and the launcher (agent_sandbox.py) then says each line on its standard error
too, so a session's supervisor.log has them as well. The log is rotated: past LIMIT bytes it becomes launcher.log.1
(KEEP files are kept). The same line said again within REPEAT_WINDOW seconds is written once, then
"(said N more times)". A state (changed(): a session's status, a host reached) is said once per change across
processes - launcher.states.json beside the log keeps the last of each (STATES_KEPT) - since the app polls a command
every few seconds, each a process of its own.

Nothing secret is written: each line passes redact() - progress.scrub's patterns for tokens, keys and passwords (and
in the launcher the proxy's, runtime.redact_text), the values of secret options and NAME=VALUE pairs with a secret
NAME (command_line), and the values given to secret(). A failure to write the log never fails what is logged.

Loaded once per process, as sys.modules["sandbox_ai_launchlog"] (load(); the modules that log have the same few lines,
since each is loaded by its path): every module that logs shares this one. Standard library only.
"""
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
NAME = "sandbox_ai_launchlog"  # its name in sys.modules: one per process
DEBUG_VARIABLE = "SANDBOX_AI_DEBUG"
LIMIT = 2 << 20  # bytes of launcher.log before it is rotated
KEEP = 3         # launcher.log.1 ... launcher.log.KEEP
LINE_LIMIT = 4000  # characters of one message
TAIL_LIMIT = 600   # characters of a command's error output kept in its line
REPEAT_WINDOW = 60  # seconds: the same line said again within them is counted, not written (then once more)
SLOW_SECONDS = 10  # an outside command (run()) still running after them is said once while it runs
STATES_KEPT = 1000  # states changed() keeps across processes (launcher.states.json), the oldest dropped first
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
# Options whose value is secret (the value after them, or after "="), and names of NAME=VALUE pairs that are.
SECRET_OPTIONS = re.compile(r"^--?[a-z0-9-]*(?:token|secret|password|passwd|api-?key|apikey|credential|auth-key"
                            r"|private-key|passphrase)[a-z0-9-]*$", re.I)
SECRET_NAME = re.compile(r"(?:token|secret|passw|api_?key|apikey|credential|auth|cookie|private|signature|session"
                         r"|passphrase)", re.I)
# An HTTP credential (Authorization: Bearer X, Basic X, token X): before progress.scrub, which masks the scheme only.
BEARER = re.compile(r"\b(Bearer|Basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)

_lock = threading.RLock()
_secrets = set()
_redactor = None
_last = {"key": None, "repeats": 0}
_states = {}
_to_stderr = {"on": False}
_local = threading.local()  # .relaying: lines said on another process' behalf (relayed())


def load():
    """This module, once per process: the copy every module of sandbox-ai logs through (its secrets, its collapsing of
    repeated lines and its state changes are shared)."""
    loaded = sys.modules.get(NAME)
    if loaded is None:
        spec = importlib.util.spec_from_file_location(NAME, HERE / "launchlog.py")
        made = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(made)
        with _lock:
            loaded = sys.modules.setdefault(NAME, made)
    return loaded


def path():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "launcher.log"


def states_path():
    """Where changed() keeps the states it logged, for every process: the app's polls are a process each, and a
    state said by one poll is not said again by the next."""
    return path().with_name("launcher.states.json")


def debugging():
    """SANDBOX_AI_DEBUG is on (1, true, yes, on): DEBUG lines are written too."""
    return os.environ.get(DEBUG_VARIABLE, "").strip().lower() in ("1", "true", "yes", "on")


def to_stderr(on=True):
    """With SANDBOX_AI_DEBUG on, every line is said on standard error as well ("agent-sandbox: debug: ..."): the
    launcher turns this on, so its terminal and a session's supervisor.log carry them."""
    _to_stderr["on"] = bool(on)


def set_redactor(function):
    """FUNCTION(text) -> text: the proxy's redaction (runtime.redact_text), handed over by the launcher, which has
    runtime.py loaded already (a listing the app polls never loads it: progress.scrub's patterns hold meanwhile)."""
    global _redactor
    _redactor = function


def secret(*values):
    """VALUES never appear in a line (replaced by [secret]); empty and very short values are ignored."""
    with _lock:
        _secrets.update(str(value) for value in values if value and len(str(value)) >= 4)


_scrub = {}


def scrubber():
    """progress.scrub (credentials in NAME=VALUE and NAME: VALUE, URL passwords, tokens of the usual forms): a small
    module, loaded once."""
    if "function" not in _scrub:
        try:
            spec = importlib.util.spec_from_file_location("sandbox_ai_progress_for_log", HERE / "progress.py")
            progress = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(progress)
            _scrub["function"] = progress.scrub
        except Exception:  # noqa: BLE001 - the values given to secret() and the launcher's redaction still hold
            _scrub["function"] = lambda text: text
    return _scrub["function"]


def redact(text):
    """TEXT without the values given to secret(), progress.scrub's matches and (in the launcher) the proxy's."""
    text = str(text)
    with _lock:
        values = sorted(_secrets, key=len, reverse=True)
    for value in values:
        if value in text:
            text = text.replace(value, "[secret]")
    text = BEARER.sub(r"\1 [secret]", text)
    for function in (_redactor, scrubber()):
        if function is not None:
            try:
                text = function(text)
            except Exception:  # noqa: BLE001 - never fail what is logged
                pass
    return text


def printable(text):
    """TEXT on one line: control and other non-printable characters escaped (a newline as \\n)."""
    return "".join(c if c.isprintable() else "\\n" if c == "\n" else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def command_line(argv):
    """ARGV as a shell would read it, the values of secret options (--git-token-file is a path, kept; --token X,
    --password=X are not) and of NAME=VALUE pairs with a secret NAME replaced by [secret]."""
    shown, hide = [], False
    for token in (str(part) for part in argv):
        if hide:
            shown.append("[secret]")
            hide = False
            continue
        option, equals, value = token.partition("=")
        if token.startswith("-") and SECRET_OPTIONS.match(option) and not option.endswith("-file"):
            if equals:
                shown.append(f"{shlex.quote(option)}=[secret]")
            else:
                shown.append(shlex.quote(token))
                hide = True
            continue
        if equals and not token.startswith("-") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", option) and \
                SECRET_NAME.search(option):
            shown.append(f"{option}=[secret]")
            continue
        shown.append(shlex.quote(token))
    return " ".join(shown)


def tail(text, limit=TAIL_LIMIT):
    """The end of TEXT (a command's error output), at most LIMIT characters, without blank lines."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [line.rstrip() for line in str(text or "").strip().splitlines() if line.strip()]
    joined = " | ".join(lines)
    return joined if len(joined) <= limit else "..." + joined[-limit:]


def _write(line):
    target = path()
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if target.stat().st_size >= LIMIT:
                _rotate(target)
        except FileNotFoundError:
            pass
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        pass  # a full disk, a read-only HOME: what is logged goes on


def _rotate(target):
    """launcher.log -> .1 -> ... -> .KEEP, under a lock (another process may rotate at the same time: the size is
    asked again under it)."""
    lock = os.open(target.with_name(target.name + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if target.stat().st_size < LIMIT:
                return
        except FileNotFoundError:
            return
        for number in range(KEEP - 1, 0, -1):
            with contextlib.suppress(FileNotFoundError):
                os.replace(target.with_name(f"{target.name}.{number}"), target.with_name(f"{target.name}.{number + 1}"))
        os.replace(target, target.with_name(target.name + ".1"))
    finally:
        os.close(lock)


def _line(level, area, message):
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
    return f"{stamp} {os.getpid()} {level:<7} {area}: {message}\n"


def exception_text(exc):
    """EXC as "Type: text". A subprocess error's own text repeats its whole command (secret option values included,
    as a Python list the patterns do not see): only its type, exit status or timeout and the tail of its output."""
    if hasattr(exc, "cmd"):
        status = f"exit status {exc.returncode}" if getattr(exc, "returncode", None) is not None else \
            f"no answer within {exc.timeout} s" if getattr(exc, "timeout", None) is not None else ""
        output = tail(getattr(exc, "stderr", None) or getattr(exc, "output", None) or "")
        return f"{type(exc).__name__}" + (f": {status}" if status else "") + (f"; error output: {output}" if output
                                                                              else "")
    # A text of many lines (a session's start error quotes the end of its supervisor.log): its first line and its
    # last EXCEPTION_LINES.
    lines = [line.rstrip() for line in str(exc).splitlines() if line.strip()]
    if len(lines) > EXCEPTION_LINES + 1:
        lines = [lines[0], "...", *lines[-EXCEPTION_LINES:]]
    return f"{type(exc).__name__}: " + " | ".join(lines)


EXCEPTION_LINES = 3  # lines of a long exception text kept after its first


def log(level, area, message, exc=None, collapse=True):
    """One line at LEVEL (DEBUG, INFO, WARNING, ERROR) from AREA (start, image, session, mcp, ...): MESSAGE, and EXC (an
    exception) as its type and text. DEBUG lines only with SANDBOX_AI_DEBUG on. Never raises. COLLAPSE: the same
    line said again at once is counted, not written (not for changed(): a state said again has changed meanwhile,
    perhaps by another process)."""
    try:
        level = level.upper()
        debug = debugging()
        if LEVELS.get(level, 20) < 20 and not debug:
            return
        if exc is not None:
            message = f"{message}: {exception_text(exc)}"
        message = printable(redact(message))
        if len(message) > LINE_LIMIT:
            message = message[:LINE_LIMIT] + "..."
        with _lock:
            key, now = (level, area, message, str(path())), time.monotonic()
            if collapse and key == _last["key"] and now - _last.get("since", now) < REPEAT_WINDOW:
                _last["repeats"] += 1
                return
            if _last["repeats"]:
                _write(_line(_last["key"][0], _last["key"][1], f"(said {_last['repeats']} more times)"))
            _last.update(key=key, repeats=0, since=now)
            _write(_line(level, area, message))
        if debug and _to_stderr["on"]:
            with contextlib.suppress(Exception):
                print(f"agent-sandbox: debug: {level.lower()} {area}: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - never fail what is logged
        pass


def debug(area, message, exc=None):
    log("DEBUG", area, message, exc)


def info(area, message, exc=None):
    log("INFO", area, message, exc)


def warning(area, message, exc=None):
    log("WARNING", area, message, exc)


def error(area, message, exc=None):
    log("ERROR", area, message, exc)


def forget_states():
    """What changed() remembers, forgotten (a test begins anew)."""
    with _lock:
        _states.clear()
        _last["key"], _last["repeats"] = None, 0
        with contextlib.suppress(OSError):
            states_path().unlink()


def _state_key(key):
    return json.dumps(key, default=str)


def _state_value(state):
    return json.loads(json.dumps(state, default=str))


@contextlib.contextmanager
def _states_file():
    """{key: {"v": state, "t": time}} of launcher.states.json, under its lock; written back when the block changed
    it (the oldest beyond STATES_KEPT dropped). OSError when it cannot be locked (this process' own states then)."""
    target = states_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(target.with_name(target.name + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            known = json.loads(target.read_text())
        except (OSError, ValueError, RecursionError):
            known = {}
        known = known if isinstance(known, dict) else {}
        before = dict(known)
        yield known
        if known != before:
            if len(known) > STATES_KEPT:
                oldest = sorted(known, key=lambda k: known[k].get("t", 0) if isinstance(known[k], dict) else 0)
                for name in oldest[:len(known) - STATES_KEPT]:
                    del known[name]
            fresh = target.with_name(f"{target.name}.{os.getpid()}.new")
            fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, json.dumps(known).encode())
            finally:
                os.close(fd)
            os.replace(fresh, target)
    finally:
        os.close(lock)


def changed(key, state, area, message, level="INFO"):
    """MESSAGE at LEVEL once STATE of KEY (a session's status, an image's presence ...) differs from the one logged
    before - by this process or by another (launcher.states.json: the app polls a command every few seconds, each a
    process of its own, and each said "q1: running" again); True when it was logged. The same state polled again
    says nothing. MESSAGE None: the state is noted only."""
    name, value = _state_key(key), _state_value(state)
    with _lock:
        try:
            with _states_file() as known:
                entry = known.get(name)
                if isinstance(entry, dict) and entry.get("v", object()) == value:
                    _states[key] = value
                    return False
                known[name] = {"v": value, "t": int(time.time())}
        except Exception:  # noqa: BLE001 - no state file (a read-only HOME): this process' own memory
            if _states.get(key, object()) == value:
                return False
        _states[key] = value
    if message is not None:
        log(level, area, message, collapse=False)
    return True


def state(key, default=None):
    """The state of KEY changed() noted last, here or in another process (DEFAULT when none was)."""
    with _lock:
        try:
            with _states_file() as known:
                entry = known.get(_state_key(key))
                if isinstance(entry, dict) and "v" in entry:
                    return entry["v"]
        except Exception:  # noqa: BLE001 - this process' own memory
            pass
        return _states.get(key, default)


@contextlib.contextmanager
def relayed():
    """What this thread says meanwhile is said on another process' behalf (a session's supervisor's lines, which it
    logged itself): the terminal shows it, launcher.log does not get it twice (relaying())."""
    before = getattr(_local, "relaying", False)
    _local.relaying = True
    try:
        yield
    finally:
        _local.relaying = before


def relaying():
    return getattr(_local, "relaying", False)


def flush():
    """The pending "(said N more times)" of the last line, written now."""
    with _lock:
        if _last["repeats"]:
            _write(_line(_last["key"][0], _last["key"][1], f"(said {_last['repeats']} more times)"))
        _last["key"], _last["repeats"] = None, 0


def ran(area, what, argv, started, returncode=None, stderr=None, exc=None, ok=(0,)):
    """The line of an outside command that ran: WHAT it was for, its command line (command_line), how long it took
    since STARTED (time.monotonic()) and how it ended - its exit status and the tail of its error output, or the
    exception EXC that kept it from running or finishing. DEBUG when it succeeded (an exit status in OK), else
    WARNING."""
    seconds = time.monotonic() - started
    text = f"{what}: {command_line(argv)} ({seconds:.1f} s)"
    if exc is not None:
        log("WARNING", area, f"{text} failed", exc)
    elif returncode in ok:
        log("DEBUG", area, f"{text}: exit status {returncode}")
    else:
        end = tail(stderr) if stderr else ""
        log("WARNING", area, f"{text}: exit status {returncode}" + (f"; error output: {end}" if end else ""))


def run(area, what, argv, runner=None, ok=(0,), **kwargs):
    """subprocess.run(ARGV, **KWARGS) - its result or its exception unchanged - with its line (ran()). RUNNER: another
    run function of the same signature (a test's)."""
    import subprocess
    runner = runner or subprocess.run
    started = time.monotonic()
    # A command that hangs (Podman waiting for a lock, a pull, SSH) is said while it runs, once: else nothing in the
    # log tells what a slow start waits for until it ends.
    slow = threading.Timer(SLOW_SECONDS, lambda: log("INFO", area, f"still running after {SLOW_SECONDS} s: {what}: "
                                                                   f"{command_line(argv)}"))
    slow.daemon = True
    slow.start()
    try:
        result = runner(argv, **kwargs)
    except Exception as exc:
        ran(area, what, argv, started, exc=exc)
        raise
    finally:
        slow.cancel()
    ran(area, what, argv, started, returncode=getattr(result, "returncode", None),
        stderr=getattr(result, "stderr", None) if not isinstance(getattr(result, "stderr", None), int) else None,
        ok=ok)
    return result
