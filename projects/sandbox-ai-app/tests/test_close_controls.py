"""Every extra window of the app has an [x] at its top right and closes with Esc (widgets.window_close_controls);
the main window, menus, tooltips and toasts get none."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMainWindow, QMenu, QMessageBox, QVBoxLayout, QWidget  # noqa: E402

from sandbox_ai_app import widgets  # noqa: E402

app = QApplication.instance() or QApplication([])
widgets.window_close_controls(app)


def corner(window):
    return window.findChild(widgets.CloseCorner, "closeCorner", Qt.FindChildOption.FindDirectChildrenOnly)


def press_escape(target):
    QApplication.sendEvent(target, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))


def test_a_dialog_gets_the_x_at_its_top_right_and_room_for_it():
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLineEdit())
    dialog.resize(400, 200)
    dialog.show()
    app.processEvents()
    button = corner(dialog)
    assert button is not None and button.accessibleName() == "Close" and "Esc" in button.toolTip()
    assert button.geometry().right() <= dialog.width() and button.geometry().top() >= 0
    assert button.x() > dialog.width() - 2 * widgets.CLOSE_CORNER - widgets.CLOSE_MARGIN, "at the right"
    assert layout.contentsMargins().top() >= widgets.CLOSE_CORNER + widgets.CLOSE_MARGIN, "it covers no control"
    dialog.resize(600, 300)
    app.processEvents()
    assert corner(dialog).x() == 600 - corner(dialog).width() - widgets.CLOSE_MARGIN, "it follows the window's size"
    corner(dialog).click()
    assert dialog.result() == QDialog.DialogCode.Rejected and not dialog.isVisible()


def test_esc_closes_a_plain_window_and_a_main_window_like_one_but_a_field_takes_it_first():
    window = QWidget()
    field = QLineEdit(window)
    widgets.close_controls(window)  # (what the log window and the login browser do)
    window.show()
    app.processEvents()
    assert corner(window) is not None
    press_escape(window)
    assert not window.isVisible()
    log = QMainWindow()
    log.setCentralWidget(QLineEdit())
    widgets.close_controls(log)
    log.show()
    app.processEvents()
    assert corner(log) is not None and log.contentsMargins().top() >= widgets.CLOSE_CORNER
    press_escape(log)
    assert not log.isVisible()
    del field


def test_a_message_box_without_an_escape_button_still_closes():
    box = QMessageBox(QMessageBox.Icon.Information, "t", "text")
    box.show()
    app.processEvents()
    assert corner(box) is not None
    corner(box).click()
    app.processEvents()
    assert not box.isVisible()


def test_menus_tooltips_and_frameless_overlays_never_take_one():
    menu = QMenu()
    toast = QWidget(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
    for widget in (menu, toast):
        widgets.close_controls(widget)
        assert corner(widget) is None


def test_the_log_window_has_one():
    from sandbox_ai_app import logview
    window = logview.LogWindow()
    window.show()
    app.processEvents()
    assert corner(window) is not None
    press_escape(window)
    assert not window.isVisible()
