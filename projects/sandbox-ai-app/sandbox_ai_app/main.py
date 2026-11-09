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


def chromium_flags(environ):
    """QTWEBENGINE_CHROMIUM_FLAGS for the terminal tabs: without the GPU (xterm.js needs none, and a machine without
    usable acceleration filled the log with Vulkan, GLX and command-buffer errors), Chromium's own messages from
    errors up (all with --debug). None: leave the variable as it is (yours wins; SANDBOX_AI_APP_GPU=1 keeps the GPU)."""
    if "QTWEBENGINE_CHROMIUM_FLAGS" in environ:
        return None
    flags = [] if environ.get("SANDBOX_AI_APP_GPU") == "1" else ["--disable-gpu", "--disable-vulkan"]
    if not environ.get("SANDBOX_AI_APP_DEBUG"):
        flags.append("--log-level=2")
    return " ".join(flags) or None


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
    # Set by the AppImage's or the macOS app's start for this process only (see backend.installed_launcher).
    backend.PACKAGE_LAUNCHER = os.environ.pop("SANDBOX_AI_APP_PACKAGE_LAUNCHER", None) or backend.PACKAGE_LAUNCHER
    problem = broken_pyside()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    flags = chromium_flags(os.environ)
    if flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags  # read when QtWebEngine starts its first page
    try:  # QtWebEngine must be loaded before the QApplication exists (terminal tabs)
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        pass
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import applog, i18n, terminal, theme
    applog.install()  # every later entry of the log says when, where and in which versions
    applog.write(f"{applog.header('INFO', 'webengine')}\n  QTWEBENGINE_CHROMIUM_FLAGS="
                 f"{os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')!r}\n")

    app = QApplication.instance() or QApplication([arg for arg in arguments if arg != "--self-test"])
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
    theme.apply(settings.get("theme", "system"), app)
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
    sheet = app.styleSheet()
    app.setStyleSheet("")
    try:
        window = MainWindow(sai, risk=risk)
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
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
