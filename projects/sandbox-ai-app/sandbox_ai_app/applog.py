"""The app's log (app.log when it runs in the background, else the terminal): every entry says when, where and in
which versions. An exception in a slot or a thread is written as one entry with the component that raised it
(module.Class.function, file and line of the innermost frame of the app's own code) and the place Qt or the thread
delivered it; Qt's own warnings get the same header. The app goes on after an error, as before."""
import datetime
import faulthandler
import os
from pathlib import Path
import platform
import sys
import threading
import traceback

APP_PACKAGE = "sandbox_ai_app"
LOG_LIMIT, LOG_KEEP = 5 << 20, 3  # app.log is rotated at 5 MiB, app.log.1 ... app.log.3 kept
_versions = {}
_installed = False
_debug = False
_file = None  # the app's own log file, when standard error is not it already


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def versions():
    """{name: version} of the app, sandbox-ai, Python and Qt (what is known; read once)."""
    if "app" not in _versions:  # sandbox-ai's may be known first
        from . import __version__
        _versions["app"] = __version__
        try:
            import PySide6
            from PySide6 import QtCore
            _versions["pyside"], _versions["qt"] = PySide6.__version__, QtCore.qVersion()
        except Exception:  # noqa: BLE001 - a log line must never fail
            pass
        _versions["python"] = platform.python_version()
    return dict(_versions)


def set_sandbox_ai(version, home=None):
    """sandbox-ai's version (and its folder) once the app knows it."""
    _versions["sandbox-ai"] = str(version)
    if home:
        _versions["sandbox-ai home"] = str(home)


def frame_name(frame_summary, frame=None):
    """module.Class.function of one frame (the class from the frame's self or cls, where there is one)."""
    module = os.path.splitext(os.path.basename(frame_summary.filename))[0]
    owner = None
    if frame is not None:
        holder = frame.f_locals.get("self", frame.f_locals.get("cls"))
        if holder is not None:
            owner = holder.__name__ if isinstance(holder, type) else type(holder).__name__
    return ".".join(part for part in (module, owner, frame_summary.name) if part)


def component(tb):
    """(name, "file:line") of the innermost frame of the app's own code in TB, else of the innermost frame."""
    frames = list(traceback.walk_tb(tb))
    if not frames:
        return "unknown", "?"
    chosen = frames[-1]
    for frame, line in reversed(frames):
        if f"{os.sep}{APP_PACKAGE}{os.sep}" in frame.f_code.co_filename:
            chosen = (frame, line)
            break
    frame, line = chosen
    summary = traceback.FrameSummary(frame.f_code.co_filename, line, frame.f_code.co_name, lookup_line=False)
    return frame_name(summary, frame), f"{os.path.basename(frame.f_code.co_filename)}:{line}"


def header(level, where, thread=None):
    known = versions()
    tail = ", ".join(f"{name} {value}" for name, value in known.items() if name != "sandbox-ai home")
    return (f"{now()} {level} [{where}] pid {os.getpid()}, thread {thread or threading.current_thread().name}"
            f"{', ' + tail if tail else ''}")


def entry(exc_type, exc, tb, delivered=None, thread=None):
    """The log entry of one exception: a header line, the exception, the traceback, indented."""
    name, place = component(tb)
    lines = [header("ERROR", name, thread) + f" at {place}" + (f" (delivered by {delivered})" if delivered else "")]
    lines.append(f"  {exc_type.__name__}: {exc}")
    for text in traceback.format_exception(exc_type, exc, tb):
        lines += ["  | " + line for line in text.rstrip("\n").split("\n")]
    return "\n".join(lines) + "\n"


def log_path(environ=None):
    """~/.local/state/sandbox-ai/app.log (XDG_STATE_HOME), the same file the launcher script writes to."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sandbox-ai" / "app.log"


def rotate(path, limit=LOG_LIMIT, keep=LOG_KEEP):
    """app.log -> app.log.1 ... (the oldest dropped) once it is larger than LIMIT."""
    try:
        if path.stat().st_size <= limit:
            return
    except OSError:
        return
    for index in range(keep, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index - 1}") if index > 1 else path
        try:
            os.replace(newer, older)
        except OSError:
            pass


def open_log(path=None):
    """The app's own log file (append, mode 0600, never through a link), or None. Not opened when standard error
    already is that file (the launcher script started the app in the background into it)."""
    path = path or log_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        rotate(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return None
    try:
        mine, err = os.fstat(fd), os.fstat(sys.stderr.fileno())
        if (mine.st_dev, mine.st_ino) == (err.st_dev, err.st_ino):
            os.close(fd)
            return None
    except (OSError, ValueError, AttributeError):
        pass
    return os.fdopen(fd, "a", buffering=1, encoding="utf-8", errors="replace")


def write(text, stream=None):
    targets = [stream] if stream is not None else [sys.stderr, _file]
    for target in targets:
        if target is None:
            continue
        try:
            target.write(text)
            target.flush()
        except Exception:  # noqa: BLE001 - nowhere left to report it
            pass


def set_debug(on):
    """Debug mode: page switches, theme changes, every background command with its time and outcome, Qt's debug
    messages. Also on with --debug or SANDBOX_AI_APP_DEBUG=1."""
    global _debug
    _debug = bool(on)
    if on:
        os.environ["SANDBOX_AI_APP_DEBUG"] = "1"


def debugging():
    return _debug or bool(os.environ.get("SANDBOX_AI_APP_DEBUG"))


def debug(event, **fields):
    """One DEBUG line (only in debug mode): EVENT and its FIELDS, the component that called it."""
    if not debugging():
        return
    caller = sys._getframe(1)
    where = f"{os.path.splitext(os.path.basename(caller.f_code.co_filename))[0]}.{caller.f_code.co_name}"
    details = " ".join(f"{key}={value!r}" for key, value in fields.items())
    write(f"{header('DEBUG', where)}\n  {event}{' ' + details if details else ''}\n")


def delivered_by():
    """Where Qt delivered a slot's exception: the sender's class and object name, when there is one."""
    try:
        from PySide6.QtCore import QObject  # noqa: F401
        from PySide6.QtWidgets import QApplication
        widget = QApplication.focusWidget() if QApplication.instance() else None
        if widget is not None:
            window = widget.window()
            page = next((w for w in _parents(widget) if type(w).__name__.endswith(("Page", "Dialog"))), None)
            return " / ".join(part for part in (type(window).__name__, type(page).__name__ if page else None,
                                                 type(widget).__name__) if part)
    except Exception:  # noqa: BLE001
        pass
    return None


def _parents(widget):
    while widget is not None:
        yield widget
        widget = widget.parentWidget()


def excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    write(entry(exc_type, exc, tb, delivered=delivered_by()))


def thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    write(entry(args.exc_type, args.exc_value, args.exc_traceback, delivered="a thread",
                thread=args.thread.name if args.thread else None))


QT_LEVELS = {"QtDebugMsg": "DEBUG", "QtInfoMsg": "INFO", "QtWarningMsg": "WARNING", "QtCriticalMsg": "ERROR",
             "QtFatalMsg": "FATAL"}


def qt_message(mode, context, message):
    level = QT_LEVELS.get(getattr(mode, "name", str(mode)).split(".")[-1], "WARNING")
    if level == "DEBUG" and not os.environ.get("SANDBOX_AI_APP_DEBUG"):
        return
    category = getattr(context, "category", None) or "qt"
    where = f"{category}" + (f" {context.file}:{context.line}" if getattr(context, "file", None) else "")
    write(f"{header(level, where)}\n  {message}\n")


def install(debug_mode=False):
    """Take over the app's error output: exceptions of slots and threads, Qt's messages, a crash of the process
    itself (faulthandler: the Python stack of every thread on a segfault), and a first line; into the app's own
    log as well when standard error is not it (a start from the desktop menu had no log at all)."""
    global _installed, _file
    if _installed:
        return
    _installed = True
    if debug_mode:
        set_debug(True)
    _file = open_log()
    try:
        faulthandler.enable(file=_file if _file is not None else sys.stderr, all_threads=True)
    except (ValueError, OSError, AttributeError):
        pass
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(qt_message)
    except Exception:  # noqa: BLE001
        pass
    write(f"{header('INFO', 'start')}, {platform.platform()}, argv {sys.argv[1:]}, log {log_path()}"
          f"{', debug mode' if debugging() else ''}\n")
