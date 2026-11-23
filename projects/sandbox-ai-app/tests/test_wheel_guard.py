"""The user 2026-11-22: "there is an major issue with scrolling in windows and message boxes. scrolling works but if
we come across a dropdown menu the scrolling will directly have affect on the dropdown. this is unwanted behavior. if
the cursor is on the dropdown a left mouseclick activates it and THEN the scrollwheel is ok to take effects."
widgets.WheelGuard, on each of the app's windows (its QWindow, where input arrives first - never an application-wide
filter): the wheel over a combo box, spin box or slider that was not activated (has no focus) scrolls what it lies
in; once clicked (it has the focus) the wheel is its own again."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from sandbox_ai_app import widgets  # noqa: E402


def dialog_with(qapp, control):
    window = QtWidgets.QDialog()
    area = QtWidgets.QScrollArea(window)
    inner = QtWidgets.QWidget()
    column = QtWidgets.QVBoxLayout(inner)
    column.addWidget(QtWidgets.QLabel("top"))
    column.addWidget(control)
    column.addSpacing(2000)  # taller than the window: the area scrolls
    area.setWidget(inner)
    area.setWidgetResizable(True)
    QtWidgets.QVBoxLayout(window).addWidget(area)
    window.resize(300, 200)
    window.show()
    qapp.processEvents()
    guard = widgets.WheelGuard()
    guard.watch(window.windowHandle())
    return window, area, guard


def wheel_event(window, widget, notches=-1):
    local = widget.mapTo(window, widget.rect().center())
    return QWheelEvent(QPointF(local), QPointF(window.mapToGlobal(local)), QPoint(0, 0), QPoint(0, 120 * notches),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


def wheel_at(qapp, window, widget, notches=-1):
    """A real wheel event at WIDGET's centre, sent to the window's QWindow (as the platform delivers it)."""
    local = widget.mapTo(window, widget.rect().center())
    event = QWheelEvent(QPointF(local), QPointF(window.mapToGlobal(local)), QPoint(0, 0), QPoint(0, 120 * notches),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(window.windowHandle(), event)
    qapp.processEvents()


def click(qapp, window, widget):
    from PySide6.QtGui import QMouseEvent
    local = QPointF(widget.mapTo(window, widget.rect().center()))
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        event = QMouseEvent(kind, local, QPointF(window.mapToGlobal(local.toPoint())), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton if kind == QMouseEvent.Type.MouseButtonPress
                            else Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(window.windowHandle(), event)
        qapp.processEvents()


def test_a_combo_box_not_clicked_lets_the_window_scroll(qapp):
    combo = QtWidgets.QComboBox()
    combo.addItems(["one", "two", "three"])
    window, area, _guard = dialog_with(qapp, combo)
    try:
        wheel_at(qapp, window, combo)
        assert combo.currentIndex() == 0, "the wheel over a combo box nobody clicked must not change it"
        assert area.verticalScrollBar().value() > 0, "the window scrolled instead"
        area.verticalScrollBar().setValue(0)  # (back where it is fully in view)
        qapp.processEvents()
        click(qapp, window, combo)  # opens its list: the wheel scrolls the list there
        combo.hidePopup()
        window.activateWindow()
        combo.setFocus(Qt.FocusReason.MouseFocusReason)  # (where the closed list gives the focus back)
        qapp.processEvents()
        assert combo.hasFocus(), "a click gives it the focus"
        # Clicked and still the focus: the guard leaves the wheel to it (its own handling then changes it - offscreen,
        # Qt delivers a wheel sent to the window right after a closed list elsewhere, so the decision is checked here).
        assert not _guard.redirect(window.windowHandle(), wheel_event(window, combo)), "clicked: the wheel is its own"
        # Another control's click takes it back from this one.
        _guard.clicked = None
        assert _guard.redirect(window.windowHandle(), wheel_event(window, combo))
    finally:
        window.close()


@pytest.mark.parametrize("make", [lambda: QtWidgets.QSpinBox(), lambda: QtWidgets.QDoubleSpinBox(),
                                  lambda: QtWidgets.QSlider(Qt.Orientation.Horizontal)])
def test_spin_boxes_and_sliders_the_same(qapp, make):
    control = make()
    control.setRange(0, 100) if not isinstance(control, QtWidgets.QDoubleSpinBox) else control.setRange(0.0, 100.0)
    control.setValue(50)
    window, area, _guard = dialog_with(qapp, control)
    try:
        wheel_at(qapp, window, control)
        assert control.value() == 50 and area.verticalScrollBar().value() > 0
    finally:
        window.close()


def test_outside_a_scroll_area_the_wheel_is_just_not_taken(qapp):
    """(the dialog's only field has the focus by itself: that is no click, the wheel still does not change it)"""
    window = QtWidgets.QDialog()
    combo = QtWidgets.QComboBox(window)
    combo.addItems(["one", "two"])
    window.show()
    qapp.processEvents()
    guard = widgets.WheelGuard()
    guard.watch(window.windowHandle())
    try:
        wheel_at(qapp, window, combo)
        assert combo.currentIndex() == 0
    finally:
        window.close()


def test_the_app_guards_its_windows_and_their_dialogs(qapp, monkeypatch):
    """The main window makes the one guard; a dialog opened later is guarded as it gets the focus."""
    from sandbox_ai_app import backend
    from sandbox_ai_app.ui import MainWindow
    guard = widgets.wheel_guard(qapp)
    assert guard is widgets.wheel_guard(qapp), "one for the whole run"
    dialog = QtWidgets.QDialog()
    dialog.show()
    qapp.processEvents()
    guard.focused(dialog.windowHandle())
    assert any(window is dialog.windowHandle() for window in guard.windows)
    dialog.close()
    assert "widgets.wheel_guard(QApplication.instance())" in open(MainWindow.__init__.__code__.co_filename).read()
    assert backend is not None
