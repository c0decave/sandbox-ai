"""Back and Forward through the pages shown (the mouse's side buttons, Alt+Left/Alt+Right, Go > Back/Forward) and
the next side button for the settings; the presses are taken on the window's QWindow, before any list, table or
board keeps them."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from sandbox_ai_app import backend  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def press(target, button, kind=QtCore.QEvent.Type.MouseButtonPress, pos=QtCore.QPointF(5, 5)):
    event = QtGui.QMouseEvent(kind, pos, pos, button, button, Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(target, event)
    return event


def test_back_and_forward_go_through_the_pages_shown(window):
    window.show_page("board", raise_window=False)
    window.show_page("images", raise_window=False)
    window.show_page("hosts", raise_window=False)
    assert window.go_back() and window.current_page() == "images"
    assert window.go_back() and window.current_page() == "board"
    assert window.go_forward() and window.current_page() == "images"
    # a page chosen now: what was ahead goes (as in a browser)
    window.show_page("files", raise_window=False)
    assert not window.go_forward() and window.current_page() == "files"
    assert not window.actions["forward"].isEnabled() and window.actions["back"].isEnabled()
    assert window.go_back() and window.current_page() == "images"
    assert window.actions["forward"].isEnabled()
    # the same page twice in a row is one step
    steps = len(window.page_history)
    window.show_page("images", raise_window=False)
    assert len(window.page_history) == steps


def test_back_returns_to_the_help_section_shown(window):
    window.show_page("board", raise_window=False)
    window.show_help("page-launch")
    window.help.show_section("page-sandboxes")
    assert window.help.shown == "page-sandboxes"
    assert window.go_back() and window.current_page() == "help" and window.help.shown == "page-launch"
    assert window.go_back() and window.current_page() == "board"
    assert window.go_forward() and window.current_page() == "help" and window.help.shown == "page-launch"
    assert window.go_forward() and window.help.shown == "page-sandboxes"


def test_the_go_menu_has_back_and_forward_with_the_platforms_keys(window):
    go = next(a.menu() for a in window.menuBar().actions() if a.text() == "&Go")
    assert {"Back", "Forward"} <= {a.text() for a in go.actions()}
    back = [k.toString() for k in window.actions["back"].shortcuts()]
    forward = [k.toString() for k in window.actions["forward"].shortcuts()]
    assert back and forward
    if not QtCore.QSysInfo.productType() == "macos":
        assert "Alt+Left" in back and "Alt+Right" in forward
    window.show_page("board", raise_window=False)
    window.show_page("images", raise_window=False)
    window.actions["back"].trigger()
    assert window.current_page() == "board"
    window.actions["forward"].trigger()
    assert window.current_page() == "images"
    # the terminals keep Alt+Left/Alt+Right (a word back and on in a shell) while they are shown
    window.show_page("terminals", raise_window=False)
    assert window.actions["back"].shortcuts() == []
    window.show_page("board", raise_window=False)
    assert [k.toString() for k in window.actions["back"].shortcuts()] == back


def test_the_side_buttons_over_a_table_or_the_board_go_back_and_forward(window, monkeypatch):
    window.show()
    QtWidgets.QApplication.processEvents()
    handle = window.windowHandle()
    assert handle is not None
    window.show_page("board", raise_window=False)
    window.show_page("hosts", raise_window=False)
    QtWidgets.QApplication.processEvents()
    # a press over the hosts table (a table takes every button it gets, so the window watches its QWindow)
    table = window.hosts.table
    point = QtCore.QPointF(table.viewport().mapTo(window, QtCore.QPoint(5, 5)))
    seen = []
    monkeypatch.setattr(table, "mousePressEvent", lambda event: seen.append(event.button()))
    press(handle, Qt.MouseButton.BackButton, pos=point)
    press(handle, Qt.MouseButton.BackButton, QtCore.QEvent.Type.MouseButtonRelease, pos=point)
    assert window.current_page() == "board" and seen == []
    press(handle, Qt.MouseButton.ForwardButton, pos=point)
    assert window.current_page() == "hosts"
    # an ordinary click still reaches the widgets
    press(handle, Qt.MouseButton.LeftButton, pos=point)
    press(handle, Qt.MouseButton.LeftButton, QtCore.QEvent.Type.MouseButtonRelease, pos=point)
    assert window.current_page() == "hosts"


def test_the_next_side_button_opens_the_settings(window, monkeypatch):
    opened = []
    monkeypatch.setattr(window, "open_settings", lambda policy=False: opened.append(policy))
    window.show()
    QtWidgets.QApplication.processEvents()
    press(window.windowHandle(), Qt.MouseButton.TaskButton)
    QtWidgets.QApplication.processEvents()
    assert opened == [False]


def test_a_side_button_no_widget_took_reaches_the_window(window):
    """Presses sent to the window itself (no QWindow in between): mousePressEvent does the same."""
    window.show_page("board", raise_window=False)
    window.show_page("images", raise_window=False)
    event = press(window, Qt.MouseButton.BackButton)
    assert event.isAccepted() and window.current_page() == "board"
