"""Every page, every button: nothing slow runs on the GUI thread (user report: "the app seems to have problems in many
places"). Every slow backend method (test_references.BLOCKING_SAI: commands here and over SSH, reads that may ask
Podman) is replaced by one that notes the thread it was called on, waits a moment and fails; then each page is shown
and every enabled push button on it clicked (dialogs answered at once: confirmations yes, file and input dialogs
cancelled, other dialogs closed), the event loop run a little after each click. No slow method may have been called
on the GUI thread, and no click may hold the window (MAX_CLICK seconds: building a dialog or a page included)."""
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import widgets  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures
from test_references import BLOCKING_SAI  # noqa: E402

MAX_CLICK = 1.0  # seconds a click may take on the GUI thread (a dialog or a page built on the spot)
# Buttons that end the app or hide its window.
SKIP = {"Quit", "Quit sandbox-ai", "Hide to tray"}


@pytest.fixture
def swept(window, env, monkeypatch, qapp):
    """The window with every slow backend method faked (noting GUI-thread calls) and every dialog answered."""
    on_gui = []
    for name in sorted(BLOCKING_SAI):
        if not hasattr(window.sai, name):
            continue

        def fake(*args, _name=name, **kwargs):
            if threading.current_thread() is threading.main_thread():
                on_gui.append(_name)
            time.sleep(0.05)
            raise RuntimeError(f"{_name}: not in this test")
        monkeypatch.setattr(window.sai, name, fake)
    monkeypatch.setattr(QtWidgets.QDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected)
    monkeypatch.setattr(QtWidgets.QMenu, "exec", lambda self, *a, **k: None)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("", False))
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem", lambda *a, **k: ("", False))
    for box in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, box, lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [])
    window.set_project(project(env, "p"))
    window.on_gui = on_gui
    return window


def run_for(qapp, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


def clickable(page):
    """The push buttons and tool buttons (a "(?)" too) shown and enabled on PAGE; check boxes and radio buttons
    count as well (a setting written, a mode chosen); not one that opens a menu (it waits for a choice)."""
    for widget in page.findChildren(QtWidgets.QAbstractButton):
        if widget.isVisibleTo(page) and widget.isEnabled() and widget.text().strip() not in SKIP \
                and not (isinstance(widget, (QtWidgets.QPushButton, QtWidgets.QToolButton)) and widget.menu()):
            yield widget


def test_every_button_of_every_page_leaves_slow_work_to_the_background(qapp, swept):
    from sandbox_ai_app.ui import PAGES
    window, slow_clicks, clicked = swept, [], 0
    stray = set(QtWidgets.QApplication.topLevelWidgets())
    for key, _title in PAGES:
        page = getattr(window, key, None)
        if page is None:
            continue
        window.show_page(key)
        run_for(qapp, 0.1)
        for widget in list(clickable(page)):
            try:
                if not widget.isVisibleTo(page) or not widget.isEnabled():
                    continue  # a click before closed or disabled it
                text = widget.text()
            except RuntimeError:  # deleted by a click before (a page rebuilt its rows)
                continue
            began = time.monotonic()
            widget.click()
            took = time.monotonic() - began
            clicked += 1
            if took > MAX_CLICK:
                slow_clicks.append(f"{key}: {text!r} {took:.2f} s")
            run_for(qapp, 0.02)
            for top in QtWidgets.QApplication.topLevelWidgets():  # a dialog shown without exec: closed again
                if top not in stray and top is not window and top.isVisible() and isinstance(top, QtWidgets.QDialog):
                    top.close()
            if window.pages.currentWidget() is not page:
                window.show_page(key)
    run_for(qapp, 0.3)
    print(f"clicked {clicked} buttons on {len(PAGES)} pages")
    assert clicked > 100, f"only {clicked} buttons were clicked"
    assert not window.on_gui, "slow backend calls on the GUI thread: " + ", ".join(sorted(set(window.on_gui)))
    assert not slow_clicks, "clicks that held the window:\n" + "\n".join(slow_clicks)


def test_the_sweep_notices_a_call_on_the_gui_thread(qapp, swept):
    window = swept
    try:
        window.sai.call("x")
    except RuntimeError:
        pass
    assert window.on_gui == ["call"]
    widgets.SHOWN.clear()
