"""The browser icon (toolbar, Ctrl+Shift+O) did nothing on the user's desktop (live use, 2026-11-14): a click now
answers at once ("Starting the browser…" in the status bar, a busy cursor while Chromium's sandbox is checked),
a check that hangs ends after CHECK_TIMEOUT with a message saying why, an error while the window is made is said
(never swallowed in the background's callback), a second click meanwhile opens no second check, the window comes
up on the screen of the app's window, and every step is in app.log."""
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import applog, chromium, login_browser as L, web_browser as W, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(bool(L.MISSING), reason="QtWebEngine is not installed")


@pytest.fixture
def host(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    logged = []
    monkeypatch.setattr(applog, "log", lambda level, area, text, *a, **k: logged.append((level, area, text)))
    window = QtWidgets.QMainWindow()
    window.logged = logged
    for made in list(W._open):
        made.close()
    W._open[:] = []
    W._pending[:] = []
    yield window
    for made in list(W._open):
        made.close()
    W._open[:] = []
    while QtWidgets.QApplication.overrideCursor() is not None:
        QtWidgets.QApplication.restoreOverrideCursor()
    window.deleteLater()


def test_a_click_answers_at_once_and_a_hanging_check_ends_in_a_message(host, monkeypatch, wait):
    release = threading.Event()

    def hanging(*args, **kwargs):
        release.wait(30)
        return chromium.Probe(True, True, None, "")
    monkeypatch.setattr(chromium, "check", hanging)
    monkeypatch.setattr(W, "CHECK_TIMEOUT", 0.5)
    assert W.open_browser(parent=host) is None
    assert host.statusBar().currentMessage() == "Starting the browser…", "feedback at once"
    assert QtWidgets.QApplication.overrideCursor() is not None, "a busy cursor while it checks"
    assert W.open_browser(parent=host) is None and len(W._pending) == 2, "a second click: no second check"
    assert wait(lambda: widgets.SHOWN, 10), "the timeout ends in a message"
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Browser" and "No answer within" in text and len(widgets.SHOWN) == 2
    assert QtWidgets.QApplication.overrideCursor() is None and W.window() is None
    assert "did not start" in host.statusBar().currentMessage()
    steps = [text for _, area, text in host.logged if area == "browser"]
    assert steps[0] == "the browser is asked for" and any("checking Chromium" in s for s in steps)
    assert any(s.startswith("no browser window:") for s in steps)
    release.set()
    time.sleep(0.1)


def test_the_window_comes_up_on_the_screen_and_is_said(host, monkeypatch, wait):
    monkeypatch.setattr(chromium, "check", lambda *a, **k: chromium.Probe(True, True, None, ""))
    host.show()
    W.open_browser(parent=host)
    assert wait(lambda: W.window() is not None, 20)
    made = W.window()
    area = (host.screen() or QtWidgets.QApplication.primaryScreen()).availableGeometry()
    assert area.intersects(made.frameGeometry()), "on the screen, not off it"
    assert host.statusBar().currentMessage() == "The browser is open"
    assert any("is shown" in text for _, _, text in host.logged)
    # Minimised and clicked again: shown again.
    made.showMinimized()
    assert W.open_browser(parent=host) is made and not made.isMinimized()


def test_an_error_while_the_window_is_made_is_said_and_logged(host, monkeypatch, wait):
    monkeypatch.setattr(chromium, "check", lambda *a, **k: chromium.Probe(True, True, None, ""))

    def broken():
        raise RuntimeError("no GL context")
    monkeypatch.setattr(W, "Browser", broken)
    told = []
    W.open_browser(parent=host, done=lambda made, why: told.append((made, why)))
    assert wait(lambda: told, 20)
    assert told[0][0] is None and "no GL context" in told[0][1]
    assert any(level == "ERROR" and "could not be made" in text for level, _, text in host.logged)
    assert QtWidgets.QApplication.overrideCursor() is None
