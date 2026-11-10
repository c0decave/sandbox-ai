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
_path = None  # where _file was opened (rotated there again while the app runs: keep_rotated)
_rotating = threading.Lock()
# The log window (logview.LogHub) and anything else that wants every entry: called with one dict per entry
# ({"time", "level", "where", "message"}), from whichever thread logged it (the hub hands it to the GUI thread).
_listeners = []
_debug_wanted = False  # the log window shows debug entries: debug() hands them to the listeners, not to the files
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "FATAL")


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


def add_listener(function):
    """FUNCTION(entry) for every later entry (see _listeners); returns FUNCTION (to remove it again)."""
    if function not in _listeners:
        _listeners.append(function)
    return function


def remove_listener(function):
    if function in _listeners:
        _listeners.remove(function)


def want_debug(on):
    """The log window shows debug entries (ON) or not: debug() is a no-op unless someone wants them."""
    global _debug_wanted
    _debug_wanted = bool(on)


def notify(level, where, message):
    """Hand one entry to the listeners; never fails (a log line must never end the app)."""
    if not _listeners:
        return
    entry = {"time": now(), "level": level, "where": where, "message": str(message)}
    for function in list(_listeners):
        try:
            function(entry)
        except Exception:  # noqa: BLE001
            pass


def to_terminal():
    """Debug lines go to a terminal (standard error a TTY) only with SANDBOX_AI_APP_DEBUG=all: in the terminal they
    drowned everything else (every background read of every page, twice a second). The log file and the log window
    always get them in debug mode."""
    if os.environ.get("SANDBOX_AI_APP_DEBUG") == "all":
        return True
    try:
        return not sys.stderr.isatty()
    except (AttributeError, ValueError, OSError):
        return True


def log(level, where, message):
    """One entry of LEVEL (INFO, WARNING, ERROR) from WHERE (a component, e.g. "board"): into the log (standard error
    and the app's log file) and the log window."""
    level = level if level in LEVELS else "INFO"
    text = f"{header(level, where)}\n  " + "\n  ".join(str(message).split("\n")) + "\n"
    if level == "DEBUG":
        if debugging():
            write_debug(text)
    else:
        write(text)
    notify(level, where, message)


def write_debug(text):
    """A debug line: into app.log, and onto standard error only where that is no terminal (or --debug=all)."""
    if to_terminal():
        write(text)
    elif _file is not None:
        write(text, _file)


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
    if _file is not None and (stream is None or stream is _file):
        keep_rotated()


def keep_rotated():
    """app.log rotated again once the file this app writes is larger than LOG_LIMIT, as at a start: an app that runs
    for weeks (in the tray; debug lines, an error repeated every second) never fills the disk with it. Each app
    rotates by the size of its own file, so a second app writing the same log is bounded too. Never raises."""
    global _file
    stream = _file
    try:
        if stream is None or _path is None or os.fstat(stream.fileno()).st_size <= LOG_LIMIT:
            return
    except (OSError, ValueError):
        return
    with _rotating:
        if _file is not stream:  # rotated by another thread meanwhile
            return
        try:
            rotate(_path, LOG_LIMIT, LOG_KEEP)
            fresh = open_log(_path)
        except Exception:  # noqa: BLE001 - a log must never end the app
            return
        if fresh is None:
            return
        _file = fresh
        if _installed:
            try:
                faulthandler.enable(file=fresh, all_threads=True)
            except (ValueError, OSError, AttributeError):
                pass
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def set_debug(on):
    """Debug mode: page switches, theme changes, every background command with its time and outcome - into app.log
    and the log window. Also on with --debug or SANDBOX_AI_APP_DEBUG=1; SANDBOX_AI_APP_DEBUG=all (--debug=all) also
    on the terminal, with Qt's own debug messages and Chromium's."""
    global _debug
    _debug = bool(on)
    if on:
        os.environ.setdefault("SANDBOX_AI_APP_DEBUG", "1")


def debugging():
    return _debug or bool(os.environ.get("SANDBOX_AI_APP_DEBUG"))


def debug(event, **fields):
    """One DEBUG line (only in debug mode): EVENT and its FIELDS, the component that called it."""
    on = debugging()
    if not on and not _debug_wanted:
        return
    caller = sys._getframe(1)
    where = f"{os.path.splitext(os.path.basename(caller.f_code.co_filename))[0]}.{caller.f_code.co_name}"
    details = " ".join(f"{key}={value!r}" for key, value in fields.items())
    message = f"{event}{' ' + details if details else ''}"
    if on:
        write_debug(f"{header('DEBUG', where)}\n  {message}\n")
    notify("DEBUG", where, message)


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
    text = entry(exc_type, exc, tb, delivered=delivered_by())
    write(text)
    notify("ERROR", component(tb)[0], text.split("\n", 1)[1] if "\n" in text else text)


def thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    text = entry(args.exc_type, args.exc_value, args.exc_traceback, delivered="a thread",
                 thread=args.thread.name if args.thread else None)
    write(text)
    notify("ERROR", component(args.exc_traceback)[0], text.split("\n", 1)[1] if "\n" in text else text)


def unraisable_hook(unraisable):
    try:
        where = getattr(unraisable, "object", None)
        text = entry(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback,
                     delivered=f"no caller ({unraisable.err_msg or 'unraisable'}: {where!r:.80})")
        write(text)
        notify("ERROR", "unraisable", text.split("\n", 1)[1] if "\n" in text else text)
    except Exception:  # noqa: BLE001
        pass


QT_LEVELS = {"QtDebugMsg": "DEBUG", "QtInfoMsg": "INFO", "QtWarningMsg": "WARNING", "QtCriticalMsg": "ERROR",
             "QtFatalMsg": "FATAL"}


def qt_message(mode, context, message):
    level = QT_LEVELS.get(getattr(mode, "name", str(mode)).split(".")[-1], "WARNING")
    if level == "DEBUG" and os.environ.get("SANDBOX_AI_APP_DEBUG") != "all":
        return  # Qt's own debug messages only with SANDBOX_AI_APP_DEBUG=all (--debug=all): they flooded the log
    category = getattr(context, "category", None) or "qt"
    where = f"{category}" + (f" {context.file}:{context.line}" if getattr(context, "file", None) else "")
    text = f"{header(level, where)}\n  {message}\n"
    write_debug(text) if level == "DEBUG" else write(text)
    notify(level, where, message)


def install(debug_mode=False):
    """Take over the app's error output: exceptions of slots and threads, Qt's messages, a crash of the process
    itself (faulthandler: the Python stack of every thread on a segfault), and a first line; into the app's own
    log as well when standard error is not it (a start from the desktop menu had no log at all)."""
    global _installed, _file, _path
    if _installed:
        return
    _installed = True
    if debug_mode:
        set_debug(True)
    _file = open_log()
    _path = log_path() if _file is not None else None
    try:
        faulthandler.enable(file=_file if _file is not None else sys.stderr, all_threads=True)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        # Chromium (QtWebEngine, in this process) ends it on a failed check with a trap instruction: SIGTRAP, which
        # enable() does not watch - the app was gone without a line. The stacks are written, then it ends as before.
        import signal
        faulthandler.register(signal.SIGTRAP, file=_file if _file is not None else sys.stderr, all_threads=True,
                              chain=True)
    except (ValueError, OSError, AttributeError, RuntimeError):
        pass
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    sys.unraisablehook = unraisable_hook  # an exception in a destructor or a callback Qt could not raise
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(qt_message)
    except Exception:  # noqa: BLE001
        pass
    write(f"{header('INFO', 'start')}, {platform.platform()}, argv {sys.argv[1:]}, log {log_path()}"
          f"{', debug mode' if debugging() else ''}\n")
