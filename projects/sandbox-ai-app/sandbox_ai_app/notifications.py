"""Notifications in the app's theme: small popups ("toasts") at the corner of the screen.

A toast has a level - info, success, warning or error - shown by a coloured bar on its left, a coloured
border and a glyph, in the theme's own colours: the accent for info, the status colours ok, warn and bad for
the others (tests/test_theme.py checks that every status colour is readable on the theme's base colour, the
toast's background; the accent at 3:1, enough for a bar and a glyph). Its title and text are plain text,
always: what an agent chose is never read as markup, and control characters are shown escaped.

Toasts stack upwards from the bottom right corner of the screen, at most MAX_SHOWN at a time (the oldest goes
first); info, success and warnings close after the setting notify_seconds (not while the pointer is over
them), errors stay until they are closed. They follow a theme change at once.

Where a window cannot place itself on the screen (Wayland), or with the setting notify_style "system", the
desktop's own notification is shown through the tray icon instead, with the icon of the level; on Wayland
without a tray, a toast is shown inside the window while it is visible.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSystemTrayIcon, QToolButton, QVBoxLayout

from . import theme
from .files import printable
from .i18n import N_, tr

LEVELS = ("info", "success", "warning", "error")
STYLES = (("themed", N_("in the app's theme")), ("system", N_("as the desktop's notifications")))
TITLE_LIMIT = 120
TEXT_LIMIT = 500
MAX_SHOWN = 4
MARGIN = 16  # pixels from the screen's edge and between toasts
WIDTH = 380
LEVEL_NAMES = {"info": N_("Information"), "success": N_("Done"), "warning": N_("Warning"), "error": N_("Error")}


def level_of(level):
    return level if level in LEVELS else "info"


def accent(level, data=None):
    """The colour of LEVEL in the theme DATA (default: the one in use)."""
    t = data or theme.data()
    c, s = t["colors"], t["status"]
    return {"info": c["accent"], "success": s["ok"], "warning": s["warn"], "error": s["bad"]}[level_of(level)]


def tray_icon(level):
    """The desktop notification's icon for LEVEL."""
    icons = QSystemTrayIcon.MessageIcon
    return {"info": icons.Information, "success": icons.Information, "warning": icons.Warning,
            "error": icons.Critical}[level_of(level)]


def clean(text, limit):
    """TEXT for a toast: control characters escaped, shortened to LIMIT characters."""
    text = printable(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def glyph(level, color, size=22):
    """A small painted sign for LEVEL: i in a circle, a tick, a triangle with !, a circle with a cross."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    level = level_of(level)
    if level == "warning":
        path = QPainterPath(QPointF(12, 3))
        path.lineTo(22, 20.5)
        path.lineTo(2, 20.5)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(12, 9.5), QPointF(12, 14))
        painter.drawPoint(QPointF(12, 17.3))
    else:
        painter.drawEllipse(QRectF(2.5, 2.5, 19, 19))
        if level == "error":
            painter.drawLine(QPointF(8.5, 8.5), QPointF(15.5, 15.5))
            painter.drawLine(QPointF(15.5, 8.5), QPointF(8.5, 15.5))
        elif level == "success":
            path = QPainterPath(QPointF(7.5, 12.5))
            path.lineTo(10.8, 15.6)
            path.lineTo(16.5, 9)
            painter.drawPath(path)
        else:
            painter.drawPoint(QPointF(12, 7.6))
            painter.drawLine(QPointF(12, 11), QPointF(12, 16.5))
    painter.end()
    return pixmap


class Toast(QFrame):
    """One notification. Top level (a tool tip window: no task bar entry, no focus taken) unless OVERLAY, a
    widget inside the window."""
    closed = Signal(object)

    def __init__(self, level, title, text, seconds, action=None, overlay=None):
        super().__init__(overlay)
        self.level = level_of(level)
        self.overlay = overlay is not None
        self.setObjectName("toast")
        self.setProperty("level", self.level)
        if not self.overlay:
            self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedWidth(WIDTH)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = QFrame()
        self.bar.setObjectName("toastBar")
        self.bar.setFixedWidth(5)
        outer.addWidget(self.bar)
        body = QHBoxLayout()
        body.setContentsMargins(10, 10, 6, 10)
        body.setSpacing(10)
        outer.addLayout(body, 1)
        self.icon = QLabel()
        self.icon.setObjectName("toastIcon")
        self.icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.icon.setAccessibleName(tr(LEVEL_NAMES[self.level]))
        body.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        self.title = self._label(clean(title, TITLE_LIMIT), "toastTitle")
        self.text = self._label(clean(text, TEXT_LIMIT), "toastText")
        self.text.setVisible(bool(self.text.text()))
        texts.addWidget(self.title)
        texts.addWidget(self.text)
        self.action = None
        if action is not None:
            label, callback = action
            self.action = QPushButton(clean(label, 60))
            self.action.setObjectName("toastAction")
            self.action.setCursor(Qt.CursorShape.PointingHandCursor)
            self.action.clicked.connect(lambda: (callback(), self.close()))
            row = QHBoxLayout()
            row.setContentsMargins(0, 4, 0, 0)
            row.addWidget(self.action)
            row.addStretch(1)
            texts.addLayout(row)
        texts.addStretch(1)
        body.addLayout(texts, 1)
        self.close_button = QToolButton()
        self.close_button.setObjectName("toastClose")
        self.close_button.setText("×")
        self.close_button.setToolTip(tr("Close"))
        self.close_button.setAccessibleName(tr("Close"))
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close)
        body.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.setAccessibleName(f"{tr(LEVEL_NAMES[self.level])}: {self.title.text()}")
        self.setAccessibleDescription(self.text.text())
        # Errors stay until they are closed; the others close after SECONDS (paused under the pointer).
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.close)
        self.seconds = seconds if self.level != "error" else 0
        self._restyling = False
        # A new theme reaches the toast in the middle of the application's palette and style change: it is
        # restyled once that is over (a style sheet set inside it would re-polish widgets being polished).
        self.restyle_timer = QTimer(self)
        self.restyle_timer.setSingleShot(True)
        self.restyle_timer.setInterval(0)
        self.restyle_timer.timeout.connect(self.restyle)
        self.restyle()

    @staticmethod
    def _label(text, name):
        label = QLabel()
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        label.setWordWrap(True)
        label.setText(text)
        return label

    def color(self):
        return accent(self.level)

    def restyle(self):
        """The colours, font and corners of the theme in use."""
        if self._restyling:
            return
        self._restyling = True
        try:
            t = theme.data()
            c = t["colors"]
            tint = self.color()
            radius = min(int(t.get("radius", 0)), 8) if self.overlay else 0
            weight = 700 if t.get("bevel") else 600
            sheet = (
                f"QFrame#toast {{ background: {c['base']}; border: 1px solid {tint};"
                f" border-radius: {radius}px; }}\n"
                f"QFrame#toastBar {{ background: {tint}; border: none;"
                f" border-top-left-radius: {radius}px; border-bottom-left-radius: {radius}px; }}\n"
                f"QLabel#toastTitle {{ color: {c['text']}; font-weight: {weight}; background: transparent; }}\n"
                f"QLabel#toastText {{ color: {c['secondary']}; background: transparent; }}\n"
                f"QLabel#toastIcon {{ background: transparent; }}\n"
                f"QToolButton#toastClose {{ color: {c['secondary']}; background: transparent; border: none;"
                f" font-size: 13pt; padding: 0 4px; }}\n"
                f"QToolButton#toastClose:hover {{ color: {c['text']}; }}\n"
                f"QPushButton#toastAction {{ color: {c['text']}; background: {c['button']};"
                f" border: 1px solid {tint}; border-radius: {min(int(t.get('radius', 0)), 6)}px;"
                f" padding: 3px 10px; min-height: 0; }}\n"
                f"QPushButton#toastAction:hover {{ background: {c['alternate']}; }}\n")
            if self.styleSheet() != sheet:
                self.setStyleSheet(sheet)
            self.icon.setPixmap(glyph(self.level, tint))
        finally:
            self._restyling = False

    def start(self):
        if self.seconds:
            self.timer.start(int(self.seconds * 1000))

    def changeEvent(self, event):  # noqa: N802
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange) \
                and not self._restyling:
            self.restyle_timer.start()
        super().changeEvent(event)

    def enterEvent(self, event):  # noqa: N802
        self.timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.start()
        super().leaveEvent(event)

    def closeEvent(self, event):  # noqa: N802
        self.timer.stop()
        self.restyle_timer.stop()
        self.closed.emit(self)
        super().closeEvent(event)


def can_place_windows():
    """False where a window cannot choose its place on the screen (Wayland)."""
    return not QGuiApplication.platformName().lower().startswith("wayland")


class Notifier:
    """Shows the app's notifications: themed toasts, or the desktop's own through TRAY (a QSystemTrayIcon or
    None). WINDOW is the main window (for its screen, and for toasts inside it where windows cannot be
    placed)."""

    def __init__(self, window, tray=None):
        self.window = window
        self._tray = tray
        self.toasts = []

    @property
    def tray(self):
        """The tray icon given, else the window's (it may come and go)."""
        return self._tray if self._tray is not None else getattr(self.window, "tray", None)

    def show(self, level, title, text="", *, seconds=15, style="themed", action=None):
        """Notify; returns the Toast shown, or None when the desktop's notification was used (or none could
        be shown). ACTION: (label, callback) for a button on the toast."""
        level = level_of(level)
        overlay = None
        if style == "system" and self.tray is not None:
            return self._desktop(level, title, text, seconds)
        if not can_place_windows():
            if self.tray is not None:
                return self._desktop(level, title, text, seconds)
            if self.window is None or not self.window.isVisible():
                return None
            overlay = self.window
        toast = Toast(level, title, text, seconds, action=action, overlay=overlay)
        toast.closed.connect(self._closed)
        self.toasts.append(toast)
        while len(self.toasts) > MAX_SHOWN:
            self.toasts[0].close()
        toast.adjustSize()
        self.layout()
        toast.show()
        if overlay is not None:
            toast.raise_()
        toast.start()
        return toast

    def _desktop(self, level, title, text, seconds):
        # The notification daemon may read markup in the body: what an agent chose is escaped (after
        # shortening, so no entity is cut).
        import html
        self.tray.showMessage(clean(title, TITLE_LIMIT), html.escape(clean(text, 300)), tray_icon(level),
                              int(seconds * 1000) if level != "error" else 0)
        return None

    def area(self, overlay=False):
        """The rectangle toasts stack in: the screen's free area, or the window's for toasts inside it."""
        if overlay:
            return self.window.rect().adjusted(0, 0, 0, -24)  # above the status bar
        screen = None
        if self.window is not None and self.window.isVisible():
            screen = self.window.screen()
        screen = screen or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)

    def layout(self):
        """Newest at the bottom right corner, older ones above it."""
        bottoms = {}
        for toast in reversed(self.toasts):
            area = self.area(toast.overlay)
            bottom = bottoms.get(toast.overlay, area.bottom() - MARGIN)
            # Wrapped text: the height for the toast's width (a label's size hint guesses another width).
            height = toast.heightForWidth(WIDTH) if toast.hasHeightForWidth() else -1
            height = max(height, toast.minimumSizeHint().height()) if height > 0 else toast.sizeHint().height()
            toast.resize(WIDTH, height)
            toast.move(area.right() - MARGIN - WIDTH + 1, bottom - height + 1)  # QRect's right and bottom: inside
            bottoms[toast.overlay] = bottom - height - MARGIN // 2

    def _closed(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
            self.layout()

    def close_all(self):
        for toast in list(self.toasts):
            toast.close()
