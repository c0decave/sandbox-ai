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
_TEST_HOME = home_guard.ensure()  # (None: a HOME moved before this session, by whoever moved it)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ["SANDBOX_AI_APP_LANGUAGE"] = "en"
# The launcher's dry runs ask Podman for no image (the tests' starts name images nobody built here; as the core
# tests' conftest does).
os.environ.setdefault("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
# Secrets the tests keep in private files need no primary password (test_primary_password.py turns that off).
os.environ.setdefault("SANDBOX_AI_SECRETS_PLAIN", "1")
# A start compacts no logs in the background (the core's tests check that).
os.environ.setdefault("SANDBOX_AI_AUTO_COMPACT", "0")

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


_REAL_HOST_LOGGED_IN = []  # backend.SandboxAI.host_logged_in as the app has it (kept at the first test)


@pytest.fixture(autouse=True)
def agents_logged_in(monkeypatch):
    """A start in the app asks to log in first when its host has no login of the start's agent (prerequisites.py). The
    tests' HOMEs hold no agent's login, and a test that starts something is not about that question: here every
    agent counts as logged in. The tests of the question itself (test_remote_session_start.py) take the real answer
    back (real_logins)."""
    try:
        from sandbox_ai_app import backend
    except ImportError:
        return
    if not _REAL_HOST_LOGGED_IN:
        _REAL_HOST_LOGGED_IN.extend([backend.SandboxAI.host_logged_in, backend.SandboxAI.logged_in_here])
    monkeypatch.setattr(backend.SandboxAI, "host_logged_in",
                        lambda self, host, agents, session=None: set(agents))
    monkeypatch.setattr(backend.SandboxAI, "logged_in_here", lambda self, agents, session=None: set(agents))


@pytest.fixture
def real_logins(agents_logged_in, monkeypatch):
    """The logins a host really has (see agents_logged_in)."""
    from sandbox_ai_app import backend
    monkeypatch.setattr(backend.SandboxAI, "host_logged_in", _REAL_HOST_LOGGED_IN[0])
    monkeypatch.setattr(backend.SandboxAI, "logged_in_here", _REAL_HOST_LOGGED_IN[1])


@pytest.fixture(scope="session")
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from sandbox_ai_app import widgets
    widgets.collect_on_gui_thread()  # as the app does: the cycle collector never deletes a Qt object in a worker
    widgets.nonblocking_dialogs()  # as the app does (main.py): dialogs block no window
    watch_unanswered_dialogs()
    # (not fitting_dialogs: offscreen's screen is 800x600, and the layout tests size dialogs as laptops of their own;
    # test_logview tests the fitting itself)
    yield app
    if "sandbox_ai_app.terminal" in sys.modules:  # web pages before their profile, or Chromium aborts
        sys.modules["sandbox_ai_app.terminal"].shutdown()


# A dialog run with exec() that no test answers (one AUTO_ANSWER does not cover: QMessageBox's statics, a dialog of
# its own) waits in its event loop for a click that never comes: the test, and the CI job with it, hung until the
# job's timeout killed it with no word of where (0.1.187's app job, at 80 %). Past UNANSWERED_LIMIT seconds it is
# cancelled (rejected) instead, and the test that left it fails with its title and text (unanswered_dialogs).
UNANSWERED_LIMIT = 120.0
UNANSWERED = []


def dialog_words(dialog):
    """"Type 'title': text" of DIALOG, for the failure of the test that left it unanswered."""
    try:
        text = dialog.text() if callable(getattr(dialog, "text", None)) else ""
        return f"{type(dialog).__name__} {dialog.windowTitle()!r}: {' '.join(str(text).split())[:300]}"
    except RuntimeError:  # deleted meanwhile
        return "a dialog (deleted)"


def watch_unanswered_dialogs():
    """QDialog.exec (nonblocking_dialogs' own) with a watch: a dialog still open after UNANSWERED_LIMIT seconds is
    noted in UNANSWERED and rejected, so that exec() returns (Cancel) and the test goes on to fail."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog
    inner = QDialog.exec
    if getattr(inner, "watched", False):
        return

    def watched_exec(dialog, *args):
        timer = QTimer()
        timer.setSingleShot(True)

        def overdue():
            UNANSWERED.append(dialog_words(dialog))
            try:
                dialog.reject()
            except RuntimeError:
                pass
        timer.timeout.connect(overdue)
        timer.start(int(UNANSWERED_LIMIT * 1000))
        try:
            return inner(dialog, *args)
        finally:
            timer.stop()
    watched_exec.watched = True
    watched_exec.nonblocking = getattr(inner, "nonblocking", False)
    QDialog.exec = watched_exec


@pytest.fixture(autouse=True)
def unanswered_dialogs():
    """A test that left a dialog open without an answer fails, naming it (watch_unanswered_dialogs)."""
    del UNANSWERED[:]
    yield
    if UNANSWERED:
        found = list(UNANSWERED)
        del UNANSWERED[:]
        pytest.fail(f"a dialog nobody answered for {UNANSWERED_LIMIT:g} s was cancelled (answer it in the test - "
                    "widgets.AUTO_ANSWER covers widgets' own questions only): " + "; ".join(found), pytrace=False)


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
def no_modifiers_held():
    """QTest's key clicks with Ctrl or Shift leave them as the application's keyboard modifiers (offscreen, until the
    next key event): a later selectRow() in a list that takes several rows (Ctrl, Shift) would add to the selection
    or take a range instead of choosing one row. A plain key release after each test lets go of them."""
    yield
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6 import QtWidgets
    from PySide6.QtCore import Qt
    if QtWidgets.QApplication.instance() is None or \
            QtWidgets.QApplication.keyboardModifiers() == Qt.KeyboardModifier.NoModifier:
        return
    from PySide6.QtTest import QTest
    widget = QtWidgets.QWidget()
    QTest.keyRelease(widget, Qt.Key.Key_Shift)
    widget.deleteLater()


def started_here(pid):
    """Whether process PID was started by this test session: it has the session's HOME (home_guard moved it to a
    temporary folder of its own; what the tests start inherits it, detached or not). False if its environment cannot
    be read (gone, another user's)."""
    try:
        environ = Path(f"/proc/{int(pid)}/environ").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return False
    return ("HOME=" + os.environ.get("HOME", "")).encode() in environ


@pytest.fixture(autouse=True)
def only_this_sessions_launchers(monkeypatch):
    """The Sandboxes page lists every sandbox-ai launcher process of the account it does not know ("Not registered"):
    run beside another test session (the core suite's launchers, another checkout's app tests) or a real sandbox,
    a test that expects no row, or one, found theirs too. The tests see the launchers they started themselves."""
    if "sandbox_ai_app.backend" not in sys.modules or sys.platform != "linux":
        yield
        return
    backend = sys.modules["sandbox_ai_app.backend"]
    original = backend.launcher_processes

    def ours(agents=backend.AGENTS, proc=Path("/proc"), info=None):
        found = original(agents, proc, info)
        if info is not None or Path(proc) != Path("/proc"):
            return found  # a process table of the test's own (procinfo on a Mac, a fake /proc)
        return [entry for entry in found if started_here(entry[0])]
    monkeypatch.setattr(backend, "launcher_processes", ours)
    yield


@pytest.fixture(autouse=True)
def no_mouse_buttons_held():
    """QTest's double click on a widget (mouseDClick) leaves its button as the application's mouse buttons (offscreen,
    for the rest of the process): later code that waits while a button is held - a floating panel given back to the
    window manager once no drag runs (MainWindow.settle_floating) - waited for a drag that never ends, and a test of it
    failed or not by the order the tests ran in. A release through the window system after each test lets go."""
    yield
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6 import QtWidgets
    from PySide6.QtCore import QPoint, Qt
    app = QtWidgets.QApplication.instance()
    if app is None or QtWidgets.QApplication.mouseButtons() == Qt.MouseButton.NoButton:
        return
    from PySide6.QtGui import QWindow
    from PySide6.QtTest import QTest
    window = QWindow()
    window.resize(4, 4)
    for button in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
        if QtWidgets.QApplication.mouseButtons() & button:
            QTest.mouseRelease(window, button, Qt.KeyboardModifier.NoModifier, QPoint(1, 1))
    app.processEvents()
    window.destroy()


@pytest.fixture(autouse=True)
def no_drafts_left():
    """What a dialog kept of what was typed (drafts.py, this app session's memory) goes with the test that typed it."""
    yield
    drafts = sys.modules.get("sandbox_ai_app.drafts")
    if drafts is not None:
        drafts.DRAFTS.clear()


@pytest.fixture(autouse=True)
def short_notices_on():
    """Short notices are shown in every test (a window made from settings with them off switched them off for the
    whole process: short_notices.set_enabled is the app's, not a window's)."""
    yield
    if "sandbox_ai_app.short_notices" in sys.modules:
        sys.modules["sandbox_ai_app.short_notices"].set_enabled(True)


@pytest.fixture(autouse=True)
def explanations_off():
    """The info texts are off again after every test, as by default (a window made from settings with them on
    switched them on for the whole process: widgets.set_explanations_shown is the app's, not a window's)."""
    yield
    if "sandbox_ai_app.widgets" in sys.modules:
        sys.modules["sandbox_ai_app.widgets"].set_explanations_shown(False)


@pytest.fixture(autouse=True)
def button_texts_off():
    """The icon buttons show their icons alone again after every test, as by default (a window made from settings with
    button_texts on switched the texts on for the whole process: button_icons.set_texts_shown is the app's)."""
    yield
    if "sandbox_ai_app.button_icons" in sys.modules:
        sys.modules["sandbox_ai_app.button_icons"].set_texts_shown(False)


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



@pytest.fixture(autouse=True)
def no_fake_host_process_outlives_the_test():
    """Nothing of a fake host (tests/live_fakes.Host) a test made runs on after it: ended by its exact PID, and the
    test fails - it stops what it started (`sandbox-ai mcp stop NAME`; the same guard as sandbox-ai's own tests)."""
    fakes = sys.modules.get("live_fakes")
    before = len(fakes.HOSTS) if fakes is not None else 0
    yield
    fakes = sys.modules.get("live_fakes")
    problems = fakes.guard_since(before) if fakes is not None else []
    if problems:
        pytest.fail("the test left processes of its fake host running (ended now; stop what the test started, e.g. "
                    "`sandbox-ai mcp stop NAME`):\n" + "\n".join(problems), pytrace=False)


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
        if _TEST_HOME is not None:  # (os._exit runs no atexit: the session's HOME stayed behind in TMPDIR)
            home_guard.remove(_TEST_HOME)
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
