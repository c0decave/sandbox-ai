"""Start sandbox-ai-app."""
import os
import signal
import sys

# Never import from the current directory: the app is often started from a workspace, which the agent
# writes (a planted PySide6.py or sandbox_ai_app/ there would run with your rights).
sys.path[:] = [entry for entry in sys.path if entry not in ("", ".") and os.path.abspath(entry) != os.getcwd()]

from . import backend


BROKEN = {"6.12.0"}  # PySide6 releases that over-release None: fatal below Python 3.12


def broken_pyside(version=None, python=None):
    """A message when this PySide6 is known to crash this Python, else None."""
    import PySide6
    version = version or PySide6.__version__
    python = python or sys.version_info
    if version in BROKEN and tuple(python[:2]) < (3, 12):
        return (f"sandbox-ai-app: PySide6 {version} crashes on Python {python[0]}.{python[1]} (\"deallocating None\").\n"
                "sandbox-ai-app: install another release, e.g.: pip install --user 'PySide6-Essentials>=6.6,!=6.12.0'\n"
                "sandbox-ai-app: (installed by sandbox-ai's install.sh: sandbox-ai-update --force --app-deps)")
    return None


# Without the GPU, also without Chromium's Vulkan features (its own, and ANGLE's Vulkan backend, a default on Linux):
# the --disable-vulkan used before is no Chromium switch, and the log still showed a Vulkan instance failing ("Failed
# to create Vulkan instance: -9"). Qt WebEngine merges this --disable-features with its own.
NO_GPU = ("--disable-gpu", "--disable-gpu-compositing", "--disable-features=Vulkan,DefaultANGLEVulkan,VulkanFromANGLE")


def chromium_flags(environ):
    """QTWEBENGINE_CHROMIUM_FLAGS for the terminal tabs: without the GPU and Vulkan (xterm.js needs neither, and a
    machine without usable acceleration filled the log with Vulkan, GLX and command-buffer errors), Chromium's own
    messages from errors up (all with --debug=all). None: leave the variable as it is (yours wins; SANDBOX_AI_APP_GPU=1
    keeps the GPU)."""
    if "QTWEBENGINE_CHROMIUM_FLAGS" in environ:
        return None
    flags = [] if environ.get("SANDBOX_AI_APP_GPU") == "1" else list(NO_GPU)
    if environ.get("SANDBOX_AI_APP_DEBUG") != "all":  # Chromium's chatter only when everything is asked for
        flags.append("--log-level=2")
    return " ".join(flags) or None


def prepare_chromium(environ=None, settings=None):
    """Before QtWebEngine is loaded: its flags (chromium_flags) and, when Settings > Window says so, no Chromium
    sandbox (QTWEBENGINE_DISABLE_SANDBOX=1; your own variable stays). Returns whether Chromium runs without it."""
    from . import chromium
    environ = os.environ if environ is None else environ
    flags = chromium_flags(environ)
    if flags:
        environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags  # read when QtWebEngine starts its first page
    return chromium.apply_setting(backend.app_settings() if settings is None else settings, environ)


def self_test_report(sai, window, app):
    """The line --self-test prints: what was built and which sandbox-ai it loaded."""
    from PySide6 import __version__ as pyside_version
    from . import __version__ as app_version
    pages = window.pages.count() if hasattr(window, "pages") else 0
    return (f"sandbox-ai-app self-test: ok (app {app_version}, sandbox-ai {sai.version()} in {sai.directory}, "
            f"PySide6 {pyside_version}, Python {sys.version.split()[0]}, platform {app.platformName()}"
            + (f", {pages} pages" if pages else "") + ")")


def main(argv=None):
    arguments = sys.argv if argv is None else argv
    # --self-test: build the main window against the sandbox-ai found, load its launcher, quit with 0 (what the
    # AppImage's, the macOS app's and CI's checks run, e.g. with QT_QPA_PLATFORM=offscreen). No dialog, no intro,
    # no tour; a problem is printed and ends it with 1.
    self_test = "--self-test" in arguments[1:]
    # --wait-for-pid=PID ("Restart the app" after an update): the app before must have ended first - its
    # single-instance guard would take this start for a second one (bounded: backend.RESTART_WAIT).
    waited = [arg for arg in arguments[1:] if arg.startswith(backend.WAIT_FOR_PID)]
    arguments = [arg for arg in arguments if not arg.startswith(backend.WAIT_FOR_PID)]
    if waited and waited[-1][len(backend.WAIT_FOR_PID):].isdigit():
        backend.wait_for_exit(int(waited[-1][len(backend.WAIT_FOR_PID):]))  # before any window: nothing to paint yet
    # Set by the AppImage's or the macOS app's start for this process only (see backend.installed_launcher).
    backend.PACKAGE_LAUNCHER = os.environ.pop("SANDBOX_AI_APP_PACKAGE_LAUNCHER", None) or backend.PACKAGE_LAUNCHER
    problem = broken_pyside()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    no_sandbox = prepare_chromium()
    try:  # QtWebEngine must be loaded before the QApplication exists (terminal tabs)
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        pass
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import applog, i18n, terminal, theme
    applog.install()  # every later entry of the log says when, where and in which versions
    from .widgets import track_worker_processes
    track_worker_processes()  # what the background reads start ends with the app (MainWindow.shutdown)
    applog.write(f"{applog.header('INFO', 'webengine')}\n  QTWEBENGINE_CHROMIUM_FLAGS="
                 f"{os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')!r}, Chromium's sandbox "
                 f"{'off (QTWEBENGINE_DISABLE_SANDBOX)' if no_sandbox else 'on (checked before each terminal tab)'}\n")

    # argv[0] names the windows (X11's WM_CLASS instance): "sandbox-ai-app", not the "__main__.py" of `python -m`.
    app = QApplication.instance() or QApplication(["sandbox-ai-app", *[arg for arg in arguments[1:]
                                                                       if arg != "--self-test"]])
    # One app per user (single_instance.py): a second start shows the first one's window and ends - two would write
    # the same state file, the last to quit overwriting the other's unapplied board changes.
    instance = None
    if not self_test and not os.environ.get("SANDBOX_AI_APP_ANOTHER"):
        from .single_instance import Instance
        instance = Instance()
        if not instance.claim(hidden="--hidden" in arguments[1:]):
            print("sandbox-ai-app: already running - its window is brought to the front", file=sys.stderr)
            return 0
    from . import crashguard, logview
    logview.hub()  # every entry from now on reaches the log window, also those before the window is made
    # The record of this run (app-run.json): the last run's, left by a process that is gone, is an unexpected end
    # (killed from outside: nothing of it is in app.log otherwise), said in the log window at this start.
    guard = None if self_test else crashguard.Guard(wait=False).start()
    from .widgets import collect_on_gui_thread
    collect_on_gui_thread()  # never a Qt object deleted by the cycle collector in a worker thread
    app.setApplicationName("sandbox-ai")
    override = backend.sandbox_ai_home_override()  # the Settings dialog's choice; $SANDBOX_AI_HOME wins
    if override:
        os.environ["SANDBOX_AI_HOME"] = override
    try:
        sai = backend.SandboxAI()
    except backend.Unavailable as exc:
        title = i18n.tr("sandbox-ai in a sandbox folder") if isinstance(exc, backend.UnsafeInstallation) else \
            i18n.tr("sandbox-ai not found")
        if self_test:
            print(f"sandbox-ai-app self-test: {title}: {exc}", file=sys.stderr)
            return 1
        from .widgets import shown_as_text
        QMessageBox.critical(None, title, shown_as_text(str(exc)))
        return 1
    try:
        applog.set_sandbox_ai(sai.version(), sai.directory)
    except Exception:  # noqa: BLE001 - a launcher that does not load is reported below
        applog.set_sandbox_ai("unknown", getattr(sai, "directory", None))
    settings = sai.settings()
    if backend.setting("debug", settings):
        applog.set_debug(True)
    i18n.set_language(settings.get("language", "system"))
    from . import fonts
    fonts.use(settings, app)  # the fonts chosen in the settings, when installed; theme.apply() applies them
    theme.apply(settings.get("theme", "system"), app)
    from .widgets import fitting_dialogs, nonblocking_dialogs, window_close_controls
    nonblocking_dialogs()  # a dialog left open blocks nothing
    fitting_dialogs()  # and never reaches past the screen's edge
    window_close_controls(app)  # every extra window: an [x] at its top right, Esc closes it
    from .ui import MainWindow, risk_text
    from .widgets import ask_first
    # Before any of sandbox-ai's modules runs in this process: are they (or the app) where a sandbox writes?
    risk = sai.startup_risk()
    if risk is not None and self_test:
        print(f"sandbox-ai-app self-test: {risk_text(risk)}", file=sys.stderr)
        return 1
    if risk is not None and not ask_first(None, "startup-risk", i18n.tr("sandbox-ai in a sandbox folder"),
                                          risk_text(risk) + "\n\n" + i18n.tr("Load sandbox-ai's code from there "
                                                                             "anyway?"),
                                          i18n.tr("Load anyway"), reject=i18n.tr("Quit")):
        return 1
    # Built without the style sheet and styled once at the end: Qt polishes each new widget under an
    # application style sheet as it is added, which made building the window about a third slower.
    # Lazy: the first page and what the window needs at once now, the other pages after the first paint (in idle
    # time, or when first shown); the self-test builds every page.
    sheet = app.styleSheet()
    app.setStyleSheet("")
    try:
        window = MainWindow(sai, risk=risk, lazy=not self_test)
    finally:
        app.setStyleSheet(sheet)
    if self_test:
        try:
            problem = sai.check_version()
            app.processEvents()
            report = None if problem else self_test_report(sai, window, app)
        finally:
            window.shutdown()
            terminal.shutdown()
        if problem:
            print(f"sandbox-ai-app self-test: {problem}", file=sys.stderr)
            return 1
        print(report)
        return 0
    app.aboutToQuit.connect(window.shutdown)
    app.aboutToQuit.connect(terminal.shutdown)
    # Closing the window may only hide it (the tray keeps answering, and a tray may appear later): the app
    # ends when the window really closes.
    app.setQuitOnLastWindowClosed(False)
    window.closed.connect(app.quit)
    app.commitDataRequest.connect(window.end_of_session)
    if "--hidden" in arguments[1:]:  # a start at login: into the tray, once there is one
        window.start_hidden()
    else:
        window.show()
        from . import intro

        def offer_tour():
            # The guided tour, offered once (Help > Guided tour brings it back); after the intro, never under it.
            if settings.get("guide_tour", True) and not window.state.get("tour_offered", False) \
                    and not os.environ.get("SANDBOX_AI_APP_NO_TOUR"):
                window.start_tour(0)
        window.intro = intro.play(window, settings)  # the agents into the sandbox; Esc skips it
        if window.intro is not None:
            window.intro.finished.connect(offer_tour)
        else:
            offer_tour()
    # SIGTERM (logout, systemctl stop): no questions, the work is stopped within a bounded time, the event
    # loop ends and Python's exit handlers run (they remove what unfinished copies created). The approvals
    # timer gives Python a moment every second to handle the signal.
    signal.signal(signal.SIGTERM, lambda *_: window.terminate())
    if instance is not None:  # a later start of the app: this window, to the front (a start for the tray: nothing)
        instance.asked.connect(lambda word: word == "show" and bring_to_front(window))
    if guard is not None:
        from PySide6.QtCore import QTimer
        ticks = QTimer(window)
        ticks.timeout.connect(lambda: guard.tick(window.current_page()))
        ticks.start(crashguard.TICK * 1000)
        if guard.last:  # the last run ended without quitting: said once, the details in the log window
            QTimer.singleShot(1500, lambda: window.notify(
                i18n.tr("The app ended unexpectedly last time"),
                i18n.tr("What is known of it is in the log window (and app.log)."), level="warning",
                action=(i18n.tr("Show the log"), lambda: window.set_panel("log", True))))
    code = app.exec()
    end_widgets(app, window)
    if guard is not None:
        guard.stop()  # a clean end: no record left
    if instance is not None:
        instance.release()
    return code


def bring_to_front(window):
    """The window shown (from the tray, minimized or behind others), on the page it shows."""
    if window.isMinimized():
        window.showNormal()
    window.show_page(window.current_page() or "launch")


def end_widgets(app, window):
    """After the event loop, while the QApplication still lives: the terminals (pages, web views, profiles: in
    QtWebEngine's order), then the window and every other top-level widget deleted by Qt. Left to Python's cleanup
    at the end of the interpreter, they were deleted after QtWebEngine had gone: "QObject: shared QObject was deleted
    directly", then a segmentation fault (exit status 139 instead of 0)."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from . import terminal
    window.shutdown()
    terminal.shutdown()
    for widget in [window, *app.topLevelWidgets()]:
        try:
            widget.deleteLater()
        except RuntimeError:  # deleted with the window
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    sys.exit(main())
