"""Shared test setup: offscreen Qt, one QApplication for all tests, English texts.

QtWebEngine must be imported before the QApplication exists; Chromium's own sandbox and GPU are switched
off for tests only (containers and CI runners have neither user namespaces for it nor a GPU)."""
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before anything else (Qt and QtWebEngine write below HOME too): the real logins are fingerprinted and HOME and the
# XDG folders move to a temporary directory for the whole session (home_guard, the same as sandbox-ai's own tests).
# The app's tests call sandbox-ai's commands and modules: run in the real HOME, one of them could write a login over
# the real one and log out the agent running them. pytest_sessionfinish fails the session if a real login changed.
import home_guard  # noqa: E402

_REAL_LOGINS = home_guard.fingerprint(home_guard.login_files()) if not os.environ.get(home_guard.MARKER) else {}
home_guard.ensure()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ["SANDBOX_AI_APP_LANGUAGE"] = "en"

try:  # noqa: SIM105 - must happen before a QApplication is created
    import PySide6.QtWebEngineWidgets  # noqa: F401
except ImportError:
    pass

# No test opens a real browser or file manager (see home_guard.no_browser): what the app asks Qt to open is recorded.
OPENED_URLS = []
try:
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl = staticmethod(lambda url: OPENED_URLS.append(url) or True)
except (ImportError, TypeError, AttributeError):
    pass


# CI sets SANDBOX_AI_APP_REQUIRE_QT=1: a test skipped because Qt, QtWebEngine or the sandbox-ai checkout is
# missing then fails, so a broken Qt installation cannot turn the Qt tests into silent skips.
REQUIRE_QT = os.environ.get("SANDBOX_AI_APP_REQUIRE_QT") == "1"
QT_SKIPS = ("PySide6", "QtWebEngine", "QtCore", "QtWidgets", "SANDBOX_AI_HOME")


def skipped_for_qt(report):
    return REQUIRE_QT and report.skipped and any(word in str(report.longrepr) for word in QT_SKIPS)


def fail_instead(report):
    report.outcome = "failed"
    report.longrepr = f"SANDBOX_AI_APP_REQUIRE_QT=1, but this was skipped: {report.longrepr}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if skipped_for_qt(report):
        fail_instead(report)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    report = outcome.get_result()
    if skipped_for_qt(report):  # a module-level importorskip (tests/test_ui.py)
        fail_instead(report)


@pytest.fixture(scope="session")
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from sandbox_ai_app import widgets
    widgets.collect_on_gui_thread()  # as the app does: the cycle collector never deletes a Qt object in a worker
    yield app
    if "sandbox_ai_app.terminal" in sys.modules:  # web pages before their profile, or Chromium aborts
        sys.modules["sandbox_ai_app.terminal"].shutdown()


def sweep_widgets(before=()):
    """Every top-level widget not in BEFORE ends now: a main window shut down first (its timers, reads and commands),
    each hidden and deleted (deleteLater() alone needs an event loop at the level it was called from, which a test
    never returns to: processEvents() leaves it undone), then Python's cycle collector, which runs on a timer of the
    GUI thread only (widgets.collect_on_gui_thread) and so hardly ever between tests. Returns how many went."""
    if "PySide6.QtWidgets" not in sys.modules:
        return 0
    import gc
    from PySide6 import QtCore, QtWidgets
    app = QtWidgets.QApplication.instance()
    if app is None:
        return 0
    # The app's own windows, pages and dialogs only. Windows that have a parent (a dialog of a window, a combo box's
    # list) go with it; one of Qt's own may belong to an object that deletes it itself (a completer's list: deleted
    # here too, it crashed), and a plain widget a test made is Python's, which the collection below lets go of.
    left = [widget for widget in app.topLevelWidgets() if widget not in before and widget.parentWidget() is None
            and (type(widget).__module__.startswith("sandbox_ai_app")
                 or isinstance(widget, (QtWidgets.QDialog, QtWidgets.QMainWindow)))]
    for widget in left:
        try:
            if callable(getattr(type(widget), "shutdown", None)) and hasattr(widget, "sai"):
                widget.shutdown()  # (a MainWindow: what closing it does, without its questions)
            widget.hide()
            widget.deleteLater()
        except RuntimeError:  # deleted with its parent meanwhile
            pass
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    gc.collect()  # (some ms; without it a closed window's Python side stays until the collector's timer runs)
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)  # what it let go of
    return len(left)


@pytest.fixture(autouse=True)
def no_widgets_left():
    """What a test made of windows, pages and dialogs goes with it (sweep_widgets): kept, the whole suite grew to
    several GB in one process and the CI runner killed it (exit 137)."""
    before = ()
    if "PySide6.QtWidgets" in sys.modules:
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication.instance()
        before = set(app.topLevelWidgets()) if app is not None else ()
    yield
    sweep_widgets(before)


@pytest.fixture(autouse=True)
def english():
    from sandbox_ai_app import i18n
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def wait_until(qapp, condition, timeout=10.0):
    """Run the event loop until CONDITION() is true; False after TIMEOUT seconds."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return bool(condition())


@pytest.fixture
def wait(qapp):
    return lambda condition, timeout=10.0: wait_until(qapp, condition, timeout)



def pytest_sessionfinish(session, exitstatus):
    """The real logins must be as they were (see home_guard at the top); the exit status is kept for
    pytest_unconfigure, which ends the process with it when Qt is loaded."""
    touched = home_guard.changed(_REAL_LOGINS) if _REAL_LOGINS else []
    if touched:
        message = ("tests changed the real login files: " + ", ".join(touched) + " (a test wrote into the real HOME)")
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(message, red=True, bold=True)
        else:
            print(message, file=sys.stderr)
        session.exitstatus = exitstatus = 1
    session.config.qt_exit_status = int(exitstatus)


def pytest_unconfigure(config):
    """Once everything is reported: with Qt loaded the process ends here, without Python's finalization. Qt's and
    QtWebEngine's own objects destroyed after it crash now and then (SIGSEGV on the main thread, "<no Python
    frame>"), which turned runs whose every test had passed into exit 139 on CI runners."""
    if "PySide6.QtWidgets" in sys.modules and hasattr(config, "qt_exit_status"):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(config.qt_exit_status)



def read_now(page, timeout=60):
    """The Sandboxes page's read (in the background in the app, the first one too) done and shown at once: after the
    read already running, if any, has come back (its late result would otherwise replace this one)."""
    import time
    from PySide6 import QtWidgets
    deadline = time.monotonic() + timeout
    while page.reading is not None and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    try:
        result, error = page.sai.instances(), None
    except Exception as exc:  # noqa: BLE001 - shown on the page, as the app does
        result, error = None, exc
    page.apply(result, error)


def open_tab(page, argv, title, cwd=None, timeout=30):
    """TerminalsPage.open(), waited for: the tab (or None when Chromium's sandbox cannot start now). A new tab
    waits for Chromium's sandbox check, which runs in a thread."""
    from PySide6 import QtWidgets
    got = []
    page.open(argv, title, cwd, done=got.append)
    assert wait_until(QtWidgets.QApplication.instance(), lambda: got, timeout), "the terminal tab never came"
    return got[0]


def settle_files(page, timeout=30):
    """The Files page lists its folders in a thread: until every listing on its way is shown."""
    from PySide6 import QtWidgets
    panes = [pane for pane in (getattr(page, "left", None), getattr(page, "right", None)) if pane is not None]
    assert wait_until(QtWidgets.QApplication.instance(), lambda: all(pane.listing is None for pane in panes or [page]),
                      timeout), "a folder's listing never came"
