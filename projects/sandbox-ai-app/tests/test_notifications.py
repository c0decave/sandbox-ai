"""Notifications in the app's theme: the level's colour in every theme, plain text only, stacking, closing on
their own (not errors), following a theme change, and the desktop's notification where a toast cannot be
placed or is not wanted."""
import time

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import notifications as N, theme  # noqa: E402

NAMES = list(theme.THEMES_DATA)


class FakeTray:
    def __init__(self):
        self.messages = []

    def showMessage(self, *args):  # noqa: N802
        self.messages.append(args)


@pytest.fixture
def notifier(qapp):
    theme.apply("light")
    shown = N.Notifier(None)
    yield shown
    shown.close_all()
    qapp.processEvents()
    theme.apply("system")


def run_for(qapp, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


@pytest.mark.parametrize("name", NAMES)
def test_each_level_has_the_theme_colour_and_is_readable(name):
    t = theme.THEMES_DATA[name]
    c, s = t["colors"], t["status"]
    assert N.accent("warning", t) == s["warn"] and N.accent("error", t) == s["bad"]
    assert N.accent("success", t) == s["ok"] and N.accent("info", t) == c["accent"]
    assert N.accent("nonsense", t) == c["accent"], "an unknown level is information"
    for level in N.LEVELS:  # the bar, border and glyph on the toast's background: 3:1 at least
        assert theme.contrast(N.accent(level, t), c["base"]) >= 3.0, (name, level)
    assert len({N.accent(level, t) for level in ("info", "warning", "error")}) == 3, "levels look different"
    for text in ("text", "secondary"):  # the title and the text
        assert theme.contrast(c[text], c["base"]) >= 4.5, (name, text)
    assert theme.contrast(c["text"], c["button"]) >= 4.5, "the action button"


@pytest.mark.parametrize("name", NAMES)
def test_a_toast_is_painted_in_the_theme(qapp, notifier, name):
    theme.apply(name)
    toast = notifier.show("warning", "Title", "Body", seconds=60)
    sheet = toast.styleSheet()
    c, s = theme.THEMES_DATA[name]["colors"], theme.THEMES_DATA[name]["status"]
    assert f"background: {c['base']}" in sheet and f"border: 1px solid {s['warn']}" in sheet
    assert f"QFrame#toastBar {{ background: {s['warn']}" in sheet and f"color: {c['text']}" in sheet
    image = toast.icon.pixmap().toImage()
    painted = {image.pixelColor(x, y).name() for x in range(image.width()) for y in range(image.height())
               if image.pixelColor(x, y).alpha() == 255}
    assert painted == {s["warn"]}, "the glyph is drawn in the level's colour"
    error = notifier.show("error", "Bad", "", seconds=60)
    assert f"border: 1px solid {s['bad']}" in error.styleSheet() and error.text.isHidden(), "no empty text line"


def test_title_and_text_are_plain_text_escaped_and_shortened(qapp, notifier):
    from PySide6.QtCore import Qt
    toast = notifier.show("info", "<b>bold</b>\x1b[31m", "<a href='https://evil.example'>x</a>\x00" + "y" * 900)
    assert toast.title.textFormat() == Qt.TextFormat.PlainText == toast.text.textFormat()
    assert toast.title.text() == "<b>bold</b>\\x1b[31m", "control characters shown escaped, markup as it is"
    assert toast.text.text().startswith("<a href='https://evil.example'>x</a>\\x00")
    assert len(toast.text.text()) == N.TEXT_LIMIT and toast.text.text().endswith("…")
    assert not toast.title.openExternalLinks() and not toast.text.openExternalLinks()


def test_toasts_stack_in_the_corner_newest_at_the_bottom(qapp, notifier):
    from PySide6.QtGui import QGuiApplication
    area = QGuiApplication.primaryScreen().availableGeometry()
    first = notifier.show("info", "one", "first", seconds=60)
    second = notifier.show("warning", "two", "second\nwith two lines", seconds=60)
    third = notifier.show("error", "three", "third", seconds=60)
    qapp.processEvents()
    assert all(t.isVisible() for t in (first, second, third))
    assert third.geometry().bottom() == area.bottom() - N.MARGIN, "the newest is in the corner"
    assert all(t.geometry().right() == area.right() - N.MARGIN for t in (first, second, third))
    assert first.geometry().bottom() < second.geometry().top() and second.geometry().bottom() < third.geometry().top()
    assert not first.geometry().intersects(second.geometry()) and not second.geometry().intersects(third.geometry())
    # A closed one leaves no gap: the others move down.
    third.close()
    qapp.processEvents()
    assert notifier.toasts == [first, second] and second.geometry().bottom() == area.bottom() - N.MARGIN
    # No more than MAX_SHOWN at a time: the oldest goes.
    more = [notifier.show("info", f"n{i}", "", seconds=60) for i in range(N.MAX_SHOWN)]
    qapp.processEvents()
    assert notifier.toasts == more and first not in notifier.toasts


def test_toast_windows_take_no_focus(qapp, notifier):
    from PySide6.QtCore import Qt
    toast = notifier.show("info", "t", "x", seconds=60)
    flags = toast.windowFlags()
    assert flags & Qt.WindowType.ToolTip == Qt.WindowType.ToolTip
    assert flags & Qt.WindowType.FramelessWindowHint and flags & Qt.WindowType.WindowStaysOnTopHint
    assert toast.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_toasts_close_on_their_own_but_errors_stay(qapp, notifier):
    info = notifier.show("info", "i", "", seconds=0.2)
    warning = notifier.show("warning", "w", "", seconds=0.2)
    error = notifier.show("error", "e", "", seconds=0.2)
    assert info.timer.isActive() and warning.timer.isActive() and not error.timer.isActive()
    run_for(qapp, 0.6)
    assert notifier.toasts == [error] and error.isVisible(), "an error stays until it is closed"
    error.close_button.click()
    qapp.processEvents()
    assert notifier.toasts == []


def test_the_pointer_over_a_toast_keeps_it(qapp, notifier):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QEnterEvent
    toast = notifier.show("warning", "w", "", seconds=0.2)
    point = QPointF(5, 5)
    toast.enterEvent(QEnterEvent(point, point, point))
    run_for(qapp, 0.4)
    assert notifier.toasts == [toast]
    toast.leaveEvent(QEvent(QEvent.Type.Leave))
    run_for(qapp, 0.5)
    assert notifier.toasts == []


def test_the_action_runs_and_closes_the_toast(qapp, notifier):
    ran = []
    toast = notifier.show("warning", "asks", "q", seconds=60, action=("Open approvals", lambda: ran.append(1)))
    assert toast.action.text() == "Open approvals"
    toast.action.click()
    qapp.processEvents()
    assert ran == [1] and notifier.toasts == []


def test_toasts_follow_a_theme_change(qapp, notifier):
    toast = notifier.show("error", "e", "x", seconds=60)
    assert theme.THEMES_DATA["light"]["status"]["bad"] in toast.styleSheet()
    theme.apply("hacker")
    qapp.processEvents()
    hacker = theme.THEMES_DATA["hacker"]
    assert hacker["status"]["bad"] in toast.styleSheet() and hacker["colors"]["base"] in toast.styleSheet()
    assert theme.THEMES_DATA["light"]["colors"]["base"] not in toast.styleSheet()


def test_the_desktop_notification_where_asked_or_where_a_toast_cannot_be_placed(qapp, notifier, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon
    tray = FakeTray()
    notifier._tray = tray
    assert notifier.show("error", "<b>t</b>", "<a href=x>y</a> &", style="system") is None
    title, text, icon, msecs = tray.messages[-1]
    assert title == "<b>t</b>" and text == "&lt;a href=x&gt;y&lt;/a&gt; &amp;", "the body may hold markup: escaped"
    assert icon == QSystemTrayIcon.MessageIcon.Critical and msecs == 0
    notifier.show("warning", "w", "", seconds=7, style="system")
    assert tray.messages[-1][2:] == (QSystemTrayIcon.MessageIcon.Warning, 7000)
    notifier.show("info", "i", "", style="system")
    assert tray.messages[-1][2] == QSystemTrayIcon.MessageIcon.Information
    assert notifier.toasts == []
    # Wayland: a window cannot place itself; the tray's notification, with the level's icon.
    monkeypatch.setattr(N, "can_place_windows", lambda: False)
    assert notifier.show("warning", "w", "x") is None and tray.messages[-1][2] == QSystemTrayIcon.MessageIcon.Warning
    # ... and without a tray: inside the window while it is visible, else nothing.
    from PySide6.QtWidgets import QMainWindow
    window = QMainWindow()
    window.resize(800, 600)
    window.tray = None
    inside = N.Notifier(window)
    assert inside.show("warning", "w", "x") is None
    window.show()
    toast = inside.show("warning", "w", "x", seconds=60)
    qapp.processEvents()
    assert toast.parent() is window and toast.overlay and toast.isVisible()
    assert window.rect().contains(toast.geometry())
    window.close()
    # Themed style without a tray (or with one): a toast.
    monkeypatch.setattr(N, "can_place_windows", lambda: True)
    notifier._tray = None
    assert notifier.show("info", "i", "x", style="system") is not None


def test_an_action_that_outlives_its_toast_raises_nothing(qapp):
    """Seen in the user's log (0.1.88): an answer asked first in a dialog, the toast's time ran out meanwhile and it was
    deleted; closing it after the answer raised "Internal C++ object (Toast) already deleted". Now the toast closes
    first and the answer runs from the event loop."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from sandbox_ai_app import notifications
    ran = []

    def answer():
        ran.append(True)
        toast.deleteLater()  # (as if its time ran out while the answer's question was open)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    toast = notifications.Toast("warning", "sandbox-ai asks", "x.example", 15, action=[("Allow", answer)])
    toast.show()
    toast.actions[0].click()
    for _ in range(5):
        qapp.processEvents()
    assert ran == [True]


def test_a_toast_shows_no_window_of_its_own_but_itself(qapp, notifier):
    """Its text label was shown before it had a parent: for a moment a window of its own (a tiling window manager laid
    out every window anew around it - the app flickered at its start)."""
    from PySide6.QtCore import QEvent, QObject

    class Spy(QObject):
        shown = []

        def eventFilter(self, obj, event):  # noqa: N802
            if event.type() == QEvent.Type.Show and obj.isWidgetType() and obj.isWindow():
                self.shown.append(type(obj).__name__ + (":" + obj.objectName() if type(obj).__name__ != "Toast" else ""))
            return False
    spy = Spy()
    qapp.installEventFilter(spy)
    try:
        notifier.show("warning", "A title", "some text")
        qapp.processEvents()
    finally:
        qapp.removeEventFilter(spy)
    assert spy.shown and set(spy.shown) <= {"Toast"}, spy.shown


def test_a_toasts_text_can_be_marked_and_copied(qapp, notifier):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    toast = notifier.show("error", "Session failed", "agent-sandbox: [Errno 13] Permission denied: '/mnt/ext'")
    if toast is None:  # the desktop's notification was used instead
        pytest.skip("no themed toast here")
    for label in (toast.title, toast.text):
        assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert toast.message_text() == "Session failed\nagent-sandbox: [Errno 13] Permission denied: '/mnt/ext'"
    toast.text.setSelection(0, 13)
    assert toast.selected_text() == "agent-sandbox"
    QGuiApplication.clipboard().setText(toast.message_text())
    assert "Permission denied" in QGuiApplication.clipboard().text()
