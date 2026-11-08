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
    yield app
    if "sandbox_ai_app.terminal" in sys.modules:  # web pages before their profile, or Chromium aborts
        sys.modules["sandbox_ai_app.terminal"].shutdown()


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

