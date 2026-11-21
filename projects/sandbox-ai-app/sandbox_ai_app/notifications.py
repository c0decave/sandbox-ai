"""Notifications in the app's theme: small popups ("toasts") at the corner of the screen.

A toast has a level - info, success, warning or error - shown by a coloured bar on its left, a coloured
border and a glyph, in the theme's own colours: the accent for info, the status colours ok, warn and bad for
the others (tests/test_theme.py checks that every status colour is readable on the theme's base colour, the
toast's background; the accent at 3:1, enough for a bar and a glyph).

What it says is at a glance (rich_notice.py): the session in bold, addresses in the accent colour, allow / deny /
ask and states in their status colours, the command that solves it in bold monospace - from the structure the
code that notifies passes (rich_notice.Rich), else recognised in its text. Every part is escaped: what an agent
chose is never read as markup, and control characters are shown escaped. The text can be marked with the mouse
(and the keyboard once the toast was clicked) and copied: Ctrl+C (the whole message when nothing is marked), the
right click's Copy and Copy all, the copy button below the [x]. Esc closes a toast that has the focus.

Toasts stack upwards from the bottom right corner of the screen, at most MAX_SHOWN at a time (the oldest goes
first); info, success and warnings close after the setting notify_seconds - not while the pointer is over them, the
focus is in them or their menu is open - errors stay until they are closed. They follow a theme change at once.

Where a window cannot place itself on the screen (Wayland): inside the window while it is the active one (the
user works in it, and its toasts can be marked and copied), else the desktop's own notification through the tray
icon, with the icon of the level and the session, the decision and the solution in bold (body markup); without a
tray, a toast is shown inside the window while it is visible. With the setting notify_style "system" always the
desktop's. On macOS, an app run by a Python outside an app bundle shows the desktop's notification through
osascript (Qt's needs a bundle).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QTextCursor, QTextOption,
)
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSystemTrayIcon, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from . import rich_notice, theme
from .files import printable
from .i18n import N_, tr

LEVELS = ("info", "success", "warning", "error")
STYLES = (("themed", N_("in the app's theme")), ("system", N_("as the desktop's notifications")))
TITLE_LIMIT = 120
TEXT_LIMIT = 500
MAX_SHOWN = 4
MARGIN = 16  # pixels from the screen's edge and between toasts
WIDTH = 380
LONG_ACTION = 18  # characters: a toast's button with a longer text takes a row of its own (half the toast cut it)
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


# macOS: Qt posts the tray's notifications through the Notification Center, which takes them only from an app bundle;
# the app run by an unbundled Python (install.sh, pip) shows them through AppleScript. The title and text are the
# script's arguments (on run argv), never part of its source.
OSASCRIPT_NOTIFICATION = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")


def mac_unbundled(executable=None):
    """macOS, and this Python is not inside an app bundle (sandbox-ai.app)."""
    import os
    import sys
    return sys.platform == "darwin" and ".app/Contents/" not in os.path.realpath(executable or sys.executable)


def mac_notification(title, text, popen=None):
    """Show TITLE and TEXT through osascript (macOS), without waiting for it; None, as the tray's notification."""
    import subprocess
    command = ["/usr/bin/osascript", *(part for line in OSASCRIPT_NOTIFICATION for part in ("-e", line)), "--", title,
               text]
    try:
        (popen or subprocess.Popen)(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:  # no notification then, as when the desktop has none
        from . import applog
        applog.swallowed("notifications", "osascript could not show the notification", exc)
    return None


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


def copy_glyph(color, done=False, size=16):
    """The copy button's sign: two sheets, one over the other; DONE: a tick (the text was copied)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 16, size / 16)
    pen = QPen(QColor(color), 1.4)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    if done:
        path = QPainterPath(QPointF(3, 8.5))
        path.lineTo(6.5, 12)
        path.lineTo(13, 4.5)
        painter.drawPath(path)
    else:
        painter.drawRoundedRect(QRectF(5.5, 5.5, 8, 8), 1.5, 1.5)
        path = QPainterPath(QPointF(3.5, 10.5))
        path.lineTo(2.5, 10.5)
        path.lineTo(2.5, 2.5)
        path.lineTo(10.5, 2.5)
        path.lineTo(10.5, 3.5)
        painter.drawPath(path)
    painter.end()
    return pixmap


class NoticeText(QTextEdit):
    """A toast's (or a short notice's) title or text: rich_notice's parts as rich text, read-only, marked with the
    mouse or the keyboard and copied as plain text. Long words wrap too (a URL, a path: a label cut them off at its
    edge); as high as its text at its width (heightForWidth), never a scroll bar. Ctrl+C with nothing marked asks
    for the whole message (copy_all)."""
    copy_all = Signal()

    def __init__(self, name, one_line=False, centered=False, focus=Qt.FocusPolicy.StrongFocus):
        super().__init__()
        self.setObjectName(name)
        self.one_line, self.centered = one_line, centered
        self.rich, self._html = rich_notice.Rich(), ""
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setTabChangesFocus(True)
        self.setFocusPolicy(focus)  # (a toast's: Tab reaches it once the toast has the focus; a notice's: a click)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)  # (the popup's menu: Copy and Copy all)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.document().setDocumentMargin(0)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_message(self, value, limit):
        """VALUE (Rich or text), at most LIMIT characters, shown in the theme's colours."""
        # A title given as text is set semibold; one given as parts (rich_notice) is set as they say - the session
        # in bold, the rest of it in the regular weight, so that the session stands out.
        self.rich = rich_notice.rich(value, one_line=self.one_line, base="heading" if self.one_line else "text") \
            .cut(limit)
        self.render_message()

    def render_message(self):
        """The parts in the colours of the theme in use (again after a theme change)."""
        html = rich_notice.to_html(self.rich, centered=self.centered) if str(self.rich) else ""
        if html != self._html:
            self._html = html
            self.setHtml(html)
            self.updateGeometry()
        self.setAccessibleName(str(self.rich))  # screen readers: the plain text

    def plain(self):
        return str(self.rich)

    def text(self):
        """What it shows, as plain text (as a label's text())."""
        return self.plain()

    def html(self):
        return self._html

    def hasSelectedText(self):  # noqa: N802 - as a label's
        return self.textCursor().hasSelection()

    def selectedText(self):  # noqa: N802
        return self.textCursor().selectedText().replace("\u2029", "\n").replace("\u2028", "\n")

    def setSelection(self, start, length):  # noqa: N802
        cursor = QTextCursor(self.document())
        cursor.setPosition(max(0, start))
        cursor.setPosition(min(start + length, self.document().characterCount() - 1),
                           QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)

    def ideal_width(self):
        """The width of its longest line, unwrapped, in its own fonts (bold and monospaced parts included)."""
        document = self.measured()
        document.setTextWidth(-1)
        return int(document.idealWidth()) + 2 + self.overhead()

    def measured(self):
        """A copy of its document to measure, as it is laid out in it (its style sheet's font: measured before it
        is shown)."""
        self.ensurePolished()
        document = self.document().clone()
        document.setDocumentMargin(self.document().documentMargin())  # (a clone has the default margin)
        return document

    def overhead(self):
        """What its frame and padding take of its width beside the text (a frame's width is in its margins)."""
        margins = self.contentsMargins()
        return margins.left() + margins.right()

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        """Every line of its text wrapped at WIDTH: the document's height there."""
        if not self._html:
            return 0
        margins = self.contentsMargins()
        document = self.measured()
        document.setTextWidth(max(1, width - self.overhead()))
        return int(document.size().height() + 0.999) + margins.top() + margins.bottom()

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        width = self.width() if self.width() > 50 else 300
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(40, self.heightForWidth(max(40, self.width())))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()  # (its height follows its width)

    def keyPressEvent(self, event):  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Copy):
            if self.hasSelectedText():
                QGuiApplication.clipboard().setText(self.selectedText())  # (plain text, never markup)
            else:
                self.copy_all.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()  # (the popup closes)
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):  # noqa: N802 - it never scrolls: all of it is shown
        event.ignore()


class Toast(QFrame):
    """One notification. Top level (a tool tip window that bypasses the window manager: no task bar entry, no focus
    taken when it appears; a click into it gives it the keyboard, Esc gives it back) unless OVERLAY, a widget inside
    the window."""
    closed = Signal(object)
    HOLD_MS = 1500  # its time ran out while the pointer, the focus or a menu was in it: looked at again after this
    COPIED_MS = 2500  # the copy button shows its tick that long

    def __init__(self, level, title, text, seconds, action=None, overlay=None):
        super().__init__(overlay)
        self.level = level_of(level)
        self.overlay = overlay is not None
        self.menu_open, self.previous = False, None
        self.setObjectName("toast")
        self.setProperty("level", self.level)
        if not self.overlay:
            self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.X11BypassWindowManagerHint)
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
        self.title = NoticeText("toastTitle", one_line=True)
        self.title.set_message(title, TITLE_LIMIT)
        self.text = NoticeText("toastText")
        self.text.set_message(text, TEXT_LIMIT)
        for label in (self.title, self.text):
            label.copy_all.connect(self.copy_all)
        texts.addWidget(self.title)
        texts.addWidget(self.text)
        # ACTION: (label, callback), or a list of them (a question's answers: Allow once, Always allow, Always, also
        # for later sessions, Block, Block always, then the way to its page), two to a row - a long one (wider than
        # half the toast) in a row of its own, never cut.
        actions = [action] if isinstance(action, tuple) else list(action or [])
        self.action, self.actions = None, []
        if actions:
            grid = QGridLayout()
            grid.setContentsMargins(0, 4, 0, 0)
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(4)
            row, column = 0, 0
            for label, callback in actions:
                button = QPushButton(clean(label, 60))
                button.setObjectName("toastAction")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda _=False, c=callback: self.act(c))
                if len(button.text()) > LONG_ACTION:
                    if column:
                        row, column = row + 1, 0
                    grid.addWidget(button, row, 0, 1, 2)
                    row += 1
                else:
                    grid.addWidget(button, row, column)
                    row, column = (row + 1, 0) if column else (row, 1)
                self.actions.append(button)
            self.action = self.actions[0]
            texts.addLayout(grid)
        texts.addStretch(1)
        body.addLayout(texts, 1)
        # (only now that it is in the toast: a label without a parent shown is a window of its own for a moment - on a
        # tiling window manager the other windows were laid out anew around it, a flicker)
        self.text.setVisible(bool(self.text.plain()))
        side = QVBoxLayout()
        side.setSpacing(2)
        self.close_button = QToolButton()
        self.close_button.setObjectName("toastClose")
        self.close_button.setText("×")
        self.close_button.setToolTip(tr("Close"))
        self.close_button.setAccessibleName(tr("Close"))
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close)
        self.copy_button = QToolButton()
        self.copy_button.setObjectName("toastCopy")
        self.copy_button.setToolTip(tr("Copy the message"))
        self.copy_button.setAccessibleName(tr("Copy the message"))
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.clicked.connect(self.copy_all)
        side.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignHCenter)
        side.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignHCenter)
        side.addStretch(1)
        body.addLayout(side)
        self.copied_timer = QTimer(self)
        self.copied_timer.setSingleShot(True)
        self.copied_timer.timeout.connect(self.show_copy_glyph)
        self.setAccessibleName(f"{tr(LEVEL_NAMES[self.level])}: {self.title.plain()}")
        self.setAccessibleDescription(self.text.plain())
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)  # a click anywhere in it: the keyboard comes along (take_keyboard)
        # Errors stay until they are closed; the others close after SECONDS (not while looked at: keeps()).
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.expire)
        self.seconds = seconds if self.level != "error" else 0
        self._restyling = False
        # A new theme reaches the toast in the middle of the application's palette and style change: it is
        # restyled once that is over (a style sheet set inside it would re-polish widgets being polished).
        self.restyle_timer = QTimer(self)
        self.restyle_timer.setSingleShot(True)
        self.restyle_timer.setInterval(0)
        self.restyle_timer.timeout.connect(self.restyle)
        self.restyle()

    def act(self, callback):
        """A button of the toast: the toast closes first, then CALLBACK runs from the event loop - an answer may ask
        first (a dialog of its own), and meanwhile the toast's time ran out and it was deleted; closing it after the
        callback then raised "Internal C++ object (Toast) already deleted"."""
        self.timer.stop()
        QTimer.singleShot(0, callback)  # (not bound to the toast: it runs whatever happens to the toast)
        self.close()

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
            small = min(int(t.get("radius", 0)), 6)
            sheet = (
                f"QFrame#toast {{ background: {c['base']}; border: 1px solid {tint};"
                f" border-radius: {radius}px; }}\n"
                f"QFrame#toastBar {{ background: {tint}; border: none;"
                f" border-top-left-radius: {radius}px; border-bottom-left-radius: {radius}px; }}\n"
                # The labels: a hairline that is only seen with the focus (the ring: the text colour, instant; its
                # width never changes, so nothing moves).
                f"QTextEdit#toastTitle, QTextEdit#toastText {{ color: {c['text']}; background: transparent;"
                f" border: 1px solid transparent; border-radius: 3px; padding: 0; }}\n"
                f"QTextEdit#toastTitle:focus, QTextEdit#toastText:focus {{ border-color: {c['text']}; }}\n"
                f"QLabel#toastIcon {{ background: transparent; }}\n"
                f"QToolButton#toastClose, QToolButton#toastCopy {{ color: {c['secondary']}; background: transparent;"
                f" border: 1px solid transparent; border-radius: 3px; }}\n"
                f"QToolButton#toastClose {{ font-size: 13pt; padding: 0 4px; }}\n"
                f"QToolButton#toastCopy {{ padding: 3px; }}\n"
                f"QToolButton#toastClose:hover, QToolButton#toastCopy:hover {{ color: {c['text']};"
                f" background: {c['alternate']}; }}\n"
                f"QToolButton#toastClose:focus, QToolButton#toastCopy:focus {{ border-color: {c['text']}; }}\n"
                f"QToolButton#toastClose:pressed, QToolButton#toastCopy:pressed {{ background: {c['border']}; }}\n"
                f"QPushButton#toastAction {{ color: {c['text']}; background: {c['button']};"
                f" border: 1px solid {tint}; border-radius: {small}px;"
                f" padding: 3px 10px; min-height: 0; }}\n"
                f"QPushButton#toastAction:hover {{ background: {c['alternate']}; }}\n"
                f"QPushButton#toastAction:focus {{ border-color: {c['text']}; background: {c['alternate']}; }}\n"
                f"QPushButton#toastAction:pressed {{ background: {c['border']}; }}\n")
            if self.styleSheet() != sheet:
                self.setStyleSheet(sheet)
            self.icon.setPixmap(glyph(self.level, tint))
            self.title.render_message()  # (the roles' colours of this theme)
            self.text.render_message()
            self.show_copy_glyph(self.copied_timer.isActive())
        finally:
            self._restyling = False

    def show_copy_glyph(self, done=False):
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QIcon
        c = theme.colors()
        self.copy_button.setIcon(QIcon(copy_glyph(theme.color("ok") if done else c["secondary"], done)))
        self.copy_button.setIconSize(QSize(16, 16))
        self.copy_button.setToolTip(tr("Copied") if done else tr("Copy the message"))

    # -- copying --

    def message_text(self):
        """The toast's title and text, as shown (plain text)."""
        return "\n".join(t for t in (self.title.plain(), self.text.plain()) if t)

    def selected_text(self):
        return next((label.selectedText() for label in (self.text, self.title) if label.hasSelectedText()), "")

    def copy_all(self):
        """The whole message to the clipboard; the copy button says so for a moment (a tick, "Copied")."""
        QGuiApplication.clipboard().setText(self.message_text())
        self.copied_timer.start(self.COPIED_MS)
        self.show_copy_glyph(True)

    def contextMenuEvent(self, event):  # noqa: N802
        """Copy (what is marked) and Copy all (title and text); the toast stays while its menu is open."""
        from PySide6.QtWidgets import QMenu
        self.timer.stop()
        menu = QMenu(self)
        selected = self.selected_text()
        copy = menu.addAction(tr("Copy"), lambda: QGuiApplication.clipboard().setText(selected))
        copy.setEnabled(bool(selected))
        menu.addAction(tr("Copy all"), self.copy_all)
        self.menu_open = True
        self.run_menu(menu, event.globalPos())
        import shiboken6
        if not shiboken6.isValid(self):  # closed while its menu was open (answered elsewhere, or pushed out)
            return
        self.menu_open = False
        menu.deleteLater()  # (made anew for each right click)
        if not self.underMouse():
            self.start()

    @staticmethod
    def run_menu(menu, position):
        menu.exec(position)

    # -- the keyboard --

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.MouseButtonPress:
            self.take_keyboard()
        return False

    def mousePressEvent(self, event):  # noqa: N802
        self.take_keyboard()
        super().mousePressEvent(event)

    def take_keyboard(self):
        """A click into the toast (a window of its own): it becomes the active window, so Ctrl+C, Tab and Esc reach
        it; the window that was active gets the keyboard back when the toast closes."""
        if self.overlay or self.isActiveWindow():
            return
        from PySide6.QtWidgets import QApplication
        active = QApplication.activeWindow()
        if active is not None and active is not self:
            self.previous = active
        self.activateWindow()

    def focus_inside(self):
        from PySide6.QtWidgets import QApplication
        focus = QApplication.focusWidget()
        return focus is not None and (focus is self or self.isAncestorOf(focus))

    def keeps(self):
        """Not closed by its time now: the pointer is over it, the focus is in it (marking, copying, its buttons)
        or its menu is open."""
        return self.underMouse() or self.focus_inside() or self.menu_open

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.close()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            selected = self.selected_text()
            if selected:
                QGuiApplication.clipboard().setText(selected)
            else:
                self.copy_all()
            return
        super().keyPressEvent(event)

    # -- its time --

    def start(self):
        if self.seconds:
            self.timer.start(int(self.seconds * 1000))

    def expire(self):
        if self.keeps():
            self.timer.start(self.HOLD_MS)
            return
        self.close()

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
        self.copied_timer.stop()
        previous, self.previous = self.previous, None
        if previous is not None and not self.overlay and self.isActiveWindow():
            import shiboken6
            QTimer.singleShot(0, lambda: shiboken6.isValid(previous) and previous.isVisible()
                              and previous.activateWindow())
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
            # Inside the window while the user works in it (its toast can be marked and copied, the desktop's
            # cannot); else the desktop's, which is seen wherever the user is.
            working = self.window is not None and self.window.isVisible() and self.window.isActiveWindow() \
                and not self.window.isMinimized()
            if self.tray is not None and not working:
                return self._desktop(level, title, text, seconds)
            if self.window is None or not self.window.isVisible():
                from . import applog
                applog.log("DEBUG", "notifications", f"not shown (no tray, no window shown): {clean(title, 80)}")
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
        body = rich_notice.rich(text).cut(300)  # (its lines kept, control characters escaped)
        if mac_unbundled():
            return mac_notification(clean(title, TITLE_LIMIT), str(body))
        # The notification daemon may read markup in the body: what an agent chose is escaped (after
        # shortening, so no entity is cut); on Linux (freedesktop's body markup) the session, the decision and the
        # solution in bold.
        import html
        import sys
        shown = rich_notice.desktop_markup(body) if sys.platform.startswith("linux") else html.escape(str(body))
        self.tray.showMessage(clean(title, TITLE_LIMIT), shown, tray_icon(level),
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
        from .widgets import fit_on_screen
        bottoms = {}
        for toast in reversed(self.toasts):
            area = self.area(toast.overlay)
            bottom = bottoms.get(toast.overlay, area.bottom() - MARGIN)
            # As wide as WIDTH, less in a narrow window or screen; every line of its wrapped text shown: the height
            # for that width (a label's size hint guesses another width), never more than the area.
            width = max(220, min(WIDTH, area.width() - 2 * MARGIN))
            if toast.width() != width:
                toast.setFixedWidth(width)
            height = toast.heightForWidth(width) if toast.hasHeightForWidth() else -1
            height = max(height, toast.minimumSizeHint().height()) if height > 0 else toast.sizeHint().height()
            height = min(height, max(80, area.height() - 2 * MARGIN))
            toast.resize(width, height)
            toast.move(area.right() - MARGIN - width + 1, bottom - height + 1)  # QRect's right and bottom: inside
            if not toast.overlay:
                fit_on_screen(toast)  # (a screen smaller than the toasts: inside it all the same)
            bottoms[toast.overlay] = bottom - height - MARGIN // 2

    def _closed(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
            self.layout()

    def close_all(self):
        for toast in list(self.toasts):
            toast.close()
