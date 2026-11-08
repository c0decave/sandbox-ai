"""Shared test setup: offscreen Qt, one QApplication for all tests, English texts.

QtWebEngine must be imported before the QApplication exists; Chromium's own sandbox and GPU are switched
off for tests only (containers and CI runners have neither user namespaces for it nor a GPU)."""
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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


@pytest.fixture(autouse=True)
def widgets_freed_on_the_gui_thread(request):
    """After every test that used Qt: running background calls are waited for, the widgets a fixture deleteLater()ed
    are deleted now (without an event loop they never would be), and the garbage collector runs here, on the GUI
    thread. A page left behind keeps its timers firing at every processEvents of later tests, and a QWidget the
    collector frees on a worker thread crashes the process (the app job's segfault in test_board_dnd)."""
    yield
    if "qapp" not in request.fixturenames and "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6 import QtCore, QtWidgets
    if QtWidgets.QApplication.instance() is None:
        return
    widgets = sys.modules.get("sandbox_ai_app.widgets")
    if widgets is not None and hasattr(widgets, "ThreadCall"):
        widgets.ThreadCall.wait_all(5)  # bounded: a test's own long call must not hold the whole job
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    import gc
    gc.collect()
