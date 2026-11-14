"""Short notices ("toasts"): what only says that something is done or how it is ("Written and trusted: …",
"Forgotten.", "Nothing to remove.") shows for a moment near the bottom of the window and goes by itself - no box,
no OK to click.

A notice is a small frame inside the window (never a window of its own: it takes no focus, the window stays active,
the keyboard stays where it was). It stays VISIBLE_MS (a longer text a little longer, LONGEST_MS at most), then fades
out over FADE_MS; while the pointer is over it, it stays (and goes HOVER_MS after the pointer left); a click takes it
away at once. Several stack upwards from the bottom, MAX_SHOWN at a time (the oldest goes first). Screen readers get
it as an alert (its title as the accessible name, its text as the description).

Every notice is also written to the app's log (INFO, component "notice": app.log and the log window) and to the
status bar of the main window - also with the setting short_notices off ("Show short notices (toasts)"), which
shows none at all.

What needs reading (an error, a warning with a decision, a question) stays a dialog (widgets.inform, warn, confirm).
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QColor, QPen
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QLabel, QMainWindow, QVBoxLayout, QWidget

from . import applog, theme
from .files import printable_text
from .widgets import safe_tooltip

SETTING = "short_notices"
VISIBLE_MS = 1000  # a short text
FREE_CHARS = 60  # characters read within VISIBLE_MS
PER_CHAR_MS = 25  # each character beyond those
LONGEST_MS = 4000
FADE_MS = 300
HOVER_MS = 1000  # after the pointer left it
MAX_SHOWN = 3
TEXT_LIMIT = 600  # characters shown (the log gets all of it)
WIDTH = 520  # at most, in pixels (less in a narrow window)
MARGIN = 16  # from the window's edges and between notices
STATUS_MS = 8000  # the status bar keeps it that long

_enabled = [True]
SHOWN = []  # tests: (title, text) of every notice shown


def set_enabled(on):
    """The setting short_notices: off, nothing is shown (the log and the status bar still get every notice)."""
    _enabled[0] = bool(on)


def enabled():
    return _enabled[0]


def lifetime(text):
    """How long (ms) TEXT stays before it fades: VISIBLE_MS, a little longer for a longer text, LONGEST_MS at most."""
    return min(LONGEST_MS, VISIBLE_MS + max(0, len(str(text)) - FREE_CHARS) * PER_CHAR_MS)


def _alive(widget):
    try:
        return widget is not None and widget.objectName() is not None
    except RuntimeError:  # deleted
        return False


def _main_window():
    windows = [w for w in QApplication.topLevelWidgets() if isinstance(w, QMainWindow) and _alive(w)]
    return next((w for w in windows if w.isVisible()), windows[0] if windows else None)


def target_window(parent):
    """The window a notice for PARENT shows in: PARENT's window; when that is hidden, the active window or the
    main window (a dialog closed meanwhile)."""
    window = None
    if isinstance(parent, QWidget) and _alive(parent):
        window = parent.window()
    if window is None or not window.isVisible():
        for other in (QApplication.activeWindow(), _main_window()):
            if _alive(other) and other.isVisible():
                return other
    return window


def one_line(title, text):
    text = " ".join(str(text).split())
    return f"{title}: {text}" if title else text


def _status(window, title, text):
    main = window if isinstance(window, QMainWindow) else _main_window()
    if main is None:
        return
    try:
        main.statusBar().showMessage(one_line(title, text)[:300], STATUS_MS)
    except RuntimeError:
        pass


def short_notice(parent, title_text, text, details=None, modal=None, warning=False):
    """Say TEXT (TITLE_TEXT: what it is about) briefly in PARENT's window; it goes by itself. Takes what inform()
    takes (MODAL means nothing here: a notice never waits). Returns the Notice shown, or None (setting off, or no
    window)."""
    title_text, text = str(title_text or ""), printable_text(str(text or "")).strip()
    applog.log("WARNING" if warning else "INFO", "notice",
               (f"{title_text}: {text}" if title_text else text)  # (its lines kept: a list of what was removed)
               + (f"\n{printable_text(details)}" if details else ""))
    window = target_window(parent)
    _status(window, title_text, text)
    if not text or not enabled() or window is None:
        return None
    SHOWN.append((title_text, text))
    return Stack.of(window).add(title_text, text)


class Notice(QFrame):
    """One notice inside WINDOW: the text, the theme's colours, a fade at its end; never focused."""

    def __init__(self, window, title, text, stack):
        super().__init__(window)
        self.setObjectName("short_notice")
        self.stack, self.title, self.text = stack, title, text
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        shown = text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT - 1] + "…"
        self.label = QLabel(shown)
        self.label.setObjectName("short_notice_text")
        self.label.setTextFormat(Qt.TextFormat.PlainText)  # what a command or an agent wrote is never markup
        self.label.setWordWrap(True)
        self.label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        self.setAccessibleName(title or text)
        self.setAccessibleDescription(text)
        if title:
            self.setToolTip(safe_tooltip(title))  # (a title may name a start or a path)
        colors = theme.data()["colors"]
        self.background, self.border = QColor(colors["base"]), QColor(colors["accent"])
        self.label.setStyleSheet(f"QLabel {{ color: {colors['text']}; background: transparent; }}")
        self.opacity = QGraphicsOpacityEffect(self)
        self.opacity.setOpacity(1.0)
        self.setGraphicsEffect(self.opacity)
        self.fade = QPropertyAnimation(self.opacity, b"opacity", self)
        self.fade.setDuration(FADE_MS)
        self.fade.setStartValue(1.0)
        self.fade.setEndValue(0.0)
        self.fade.setEasingCurve(QEasingCurve.Type.InQuad)
        self.fade.finished.connect(self.dismiss)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fade.start)
        self.ms = lifetime(text)
        self.gone = False

    def start(self):
        self.show()
        self.raise_()
        self.timer.start(self.ms)
        self.announce()

    def announce(self):
        """Screen readers: an alert with the notice's text."""
        try:
            from PySide6.QtGui import QAccessible, QAccessibleEvent
            QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.Event.Alert))
        except Exception:  # noqa: BLE001 - no accessibility bridge: the log and the status bar have it
            pass

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
        painter.fillPath(path, self.background)
        painter.setPen(QPen(self.border, 1.2))
        painter.drawPath(path)

    def enterEvent(self, event):  # noqa: N802 - the pointer over it: it stays
        self.timer.stop()
        self.fade.stop()
        self.opacity.setOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        if not self.gone:
            self.timer.start(HOVER_MS)
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 - a click takes it away
        self.dismiss()
        event.accept()

    def dismiss(self):
        if self.gone:
            return
        self.gone = True
        self.timer.stop()
        self.fade.stop()
        self.hide()
        self.stack.remove(self)
        self.deleteLater()


class Stack(QObject):
    """The notices of one window, stacked upwards from its bottom (above its status bar); laid out again when the
    window's size changes."""

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("short_notices")
        self.window = window
        self.notices = []
        window.installEventFilter(self)

    @classmethod
    def of(cls, window):
        found = window.findChild(Stack, "short_notices", Qt.FindChildOption.FindDirectChildrenOnly)
        return found if found is not None else cls(window)

    def add(self, title, text):
        while len(self.notices) >= MAX_SHOWN:
            self.notices[0].dismiss()
        notice = Notice(self.window, title, text, self)
        self.notices.append(notice)
        self.layout()
        notice.start()
        return notice

    def remove(self, notice):
        if notice in self.notices:
            self.notices.remove(notice)
            self.layout()

    def bottom(self):
        """Where the lowest notice ends: above the window's status bar, if it shows one."""
        bottom = self.window.height() - MARGIN
        if isinstance(self.window, QMainWindow):
            bar = self.window.statusBar() if self.window.statusBar().isVisible() else None
            if bar is not None:
                bottom = min(bottom, bar.geometry().top() - MARGIN // 2)
        return bottom

    def layout(self):
        if not _alive(self.window):
            return
        room = max(120, min(WIDTH, self.window.width() - 2 * MARGIN))
        bottom = self.bottom()
        for notice in reversed(self.notices):  # the newest lowest
            margins = notice.layout().contentsMargins()
            inner = room - margins.left() - margins.right()
            metrics = notice.label.fontMetrics()
            wanted = min(inner, max(metrics.horizontalAdvance(line) for line in notice.label.text().split("\n")) + 4)
            width = wanted + margins.left() + margins.right()
            height = notice.label.heightForWidth(wanted) + margins.top() + margins.bottom()
            height = max(height, notice.label.sizeHint().height() + margins.top() + margins.bottom())
            notice.setGeometry((self.window.width() - width) // 2, bottom - height, width, height)
            notice.raise_()
            bottom -= height + MARGIN // 2

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.window and event.type() == QEvent.Type.Resize:
            self.layout()
        return False
