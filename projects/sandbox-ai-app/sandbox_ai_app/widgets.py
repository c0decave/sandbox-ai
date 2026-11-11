"""Small widgets and helpers shared by the pages.

Everything that shows text from sandbox-ai or from a sandbox (host names, URLs, file names, command output)
shows it as plain text: message boxes, labels and form rows force Qt.PlainText, tooltips are escaped. Long
work never runs on the GUI thread: AsyncCommand runs a command (in its own process group, so Cancel ends it
and its children), ThreadCall runs a function, Task shows either inline with a Cancel button."""
from __future__ import annotations

import html
import os
import re
import signal
import sys
import threading
import time

from PySide6.QtCore import (
    QAbstractTableModel, QItemSelection, QItemSelectionModel, QModelIndex, QObject, QProcess, QRect,
    QSize, Qt, QTimer, Signal, Slot,
)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPalette, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QLayout, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate,
    QTableView, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QWidgetItem,
)

from . import i18n, tables, theme
from . import applog
from .i18n import tr

MONO = QFont("monospace")
MONO.setStyleHint(QFont.StyleHint.Monospace)
CELL_LIMIT = 300  # characters shown in a table cell; the rest is in its tooltip
TOOLTIP_LIMIT = 2000
SORT_ROLE = Qt.ItemDataRole.UserRole + 7
BAR_ROLE = Qt.ItemDataRole.UserRole + 8
BADGE_ROLE = Qt.ItemDataRole.UserRole + 9
SAMPLED_ROWS = 300  # rows a ModelTable's column widths are measured on
# Tests set this to True or False: confirm() then answers at once, and messages are collected in SHOWN.
AUTO_ANSWER = None
SHOWN = []


# -- text --

def safe_tooltip(text, limit=TOOLTIP_LIMIT):
    """TEXT for setToolTip: escaped and marked as rich text, so markup in it is shown, never interpreted."""
    if not text:
        return ""
    text = str(text)
    if len(text) > limit:
        text = text[:limit] + "…"
    return "<p style='white-space:pre-wrap'>" + html.escape(text, quote=False) + "</p>"


def shown_as_text(text):
    """TEXT for a place Qt reads as rich text when it looks like markup (Qt.AutoText: the labels of QInputDialog and of
    the static QMessageBox calls): escaped and marked as rich text, so a name an agent or a host chose (a file
    "<b>Allow</b>", a URL) is shown as it is, never interpreted."""
    return "<p style='white-space:pre-wrap'>" + html.escape(str(text), quote=False) + "</p>"


def elide(text, limit=CELL_LIMIT):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def number(value):
    """12,345 or 12.345 (German): thousands separated."""
    text = f"{value:,.1f}" if isinstance(value, float) and not value.is_integer() else f"{int(value):,}"
    if i18n.language() == "de":
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def local_time(text, seconds=False):
    """A time as sandbox-ai writes it (UTC, 2026-11-03T10:00:00Z) in local time, or the text itself."""
    from .backend import parse_time
    moment = parse_time(text)
    if moment is None:
        return str(text or "")
    pattern = "%d.%m.%Y %H:%M" if i18n.language() == "de" else "%Y-%m-%d %H:%M"
    return time.strftime(pattern + (":%S" if seconds else ""), time.localtime(moment))


def plain_label(text="", word_wrap=True, role=None):
    """A label that never interprets its text as HTML (texts from logs and questions come from the agent)."""
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(word_wrap)
    label.setText(str(text))
    if role:
        label.setProperty("role", role)
    return label


class ElidedLabel(QLabel):
    """A one-line label that never asks for more room than ABOUT characters: a longer text is cut with "…" where the
    room ends, whole in its tooltip (a status line under a canvas that does not scroll)."""

    def __init__(self, about=40, role=None):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.about = about
        self.elides = True  # cut on purpose (tests/test_layout.py: not a clipped text)
        if role:
            self.setProperty("role", role)

    def sizeHint(self):  # noqa: N802
        hint = super().sizeHint()
        return QSize(min(hint.width(), self.fontMetrics().averageCharWidth() * self.about), hint.height())

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def setText(self, text):  # noqa: N802
        super().setText(str(text))
        self.setToolTip(safe_tooltip(text) if text else "")

    def paintEvent(self, event):  # noqa: N802
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), text)


def title(text, subtitle=None):
    """The heading of a page: a title and an optional line of explanation."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(2)
    heading = plain_label(text, word_wrap=False, role="title")
    heading.setAccessibleName(text)
    layout.addWidget(heading)
    if subtitle:
        layout.addWidget(plain_label(subtitle, role="secondary"))
    return box


def section(text):
    return plain_label(text, word_wrap=False, role="section")


def disclosure(widget):
    """WIDGET (a checkable QToolButton that shows or hides something) drawn as a disclosure: an arrow beside its
    text (right: folded, down: open), flat, so its checked state never looks like a button stuck pressed. The
    property "disclosure" marks it (tests/test_buttons.py: the only checkable buttons besides check boxes)."""
    widget.setCheckable(True)
    widget.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    widget.setAutoRaise(True)
    widget.setProperty("disclosure", True)

    def arrow(on):
        widget.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
    widget.toggled.connect(arrow)
    arrow(widget.isChecked())
    return widget


def button(text, tooltip, slot=None, role=None, shortcut=None):
    """A push button with a tooltip (every button explains itself) and an optional role (primary, danger)."""
    widget = QPushButton(text)
    widget.setToolTip(safe_tooltip(tooltip))
    if role:
        widget.setProperty("role", role)
    if shortcut:
        widget.setShortcut(shortcut)
    if slot:
        widget.clicked.connect(lambda _=False: slot())
    return widget


def set_enabled(widget, enabled, why=None, tooltip=None):
    """Enable WIDGET; when disabled, its tooltip says why."""
    widget.setEnabled(bool(enabled))
    if not hasattr(widget, "_tooltip"):
        widget._tooltip = tooltip if tooltip is not None else widget.toolTip()
    widget.setToolTip(widget._tooltip if enabled or not why else safe_tooltip(why))


# -- dialogs (plain text only) --

LONG_MESSAGE = (24, 160)  # lines, characters in a line: a message beyond either goes to a MessageDialog
LONG_WORD = 60  # characters without a space (a URL, a path): a message with one goes to a MessageDialog too
MESSAGE_SIZE = (640, 360)  # the most a MessageDialog's text takes before it scrolls (less on a smaller screen)


def long_message(text):
    """Whether TEXT is too long for a plain message box: a line of more than LONG_MESSAGE characters, more lines
    than it, or a word a line cannot hold (a URL). Such a message is shown in a MessageDialog: wrapped (inside the
    word too), selectable, scrolling beyond a screenful, never cut and never wider than the screen."""
    lines = str(text).split("\n")
    return len(lines) > LONG_MESSAGE[0] or any(len(line) > LONG_MESSAGE[1] for line in lines) or \
        any(len(word) > LONG_WORD for word in str(text).split())


class WrapText(QPlainTextEdit):
    """wrapping_text's field: its size hint is the one given (HINT), not QPlainTextEdit's fixed 256x192."""
    hint = None

    def sizeHint(self):  # noqa: N802
        return self.hint if self.hint is not None else super().sizeHint()


def wrapping_text(text="", role=None):
    """A read-only plain-text field for a message (a refusal of sandbox-ai, a URL in it): it wraps at word
    boundaries and inside a word longer than a line, so it never asks for more width than it gets; it is
    selectable, and Qt never reads its text as rich text. MessageDialog's text; MessageText is the one that
    grows with its text in a page."""
    field = WrapText()
    field.setReadOnly(True)
    field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                  Qt.TextInteractionFlag.TextSelectableByKeyboard)
    field.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    field.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    field.setFrameShape(QFrame.Shape.NoFrame)
    field.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # it wraps: never wider than it is
    field.setStyleSheet("QPlainTextEdit { background: transparent; border: none; }")
    field.document().setDocumentMargin(0)
    if role:
        field.setProperty("role", role)
    field.setPlainText(str(text))
    return field


def text_height(text, font, width):
    """The height TEXT takes in FONT at WIDTH pixels, wrapped as wrapping_text wraps it."""
    document = QTextDocument()
    document.setDefaultFont(font)
    option = document.defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    document.setDefaultTextOption(option)
    document.setDocumentMargin(0)
    document.setPlainText(str(text))
    document.setTextWidth(max(1, width))
    return int(document.size().height()) + 2


class MessageText(QPlainTextEdit):
    """A page's message area for what sandbox-ai said (its refusals, a catalog's problems): wrapping_text that is
    as high as its text at the width it gets, without a scroll bar, so a long one-line refusal with a URL wraps in
    the page instead of widening the window; empty, it takes no room."""

    def __init__(self, text="", role=None):
        super().__init__()
        field = wrapping_text("", role)  # its settings, taken over
        self.setReadOnly(True)
        self.setTextInteractionFlags(field.textInteractionFlags())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(field.styleSheet())
        self.document().setDocumentMargin(0)
        field.deleteLater()
        if role:
            self.setProperty("role", role)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text):  # noqa: N802 - QLabel's name: the pages set it as they set a label
        self.setPlainText(str(text or ""))
        self.setHidden(not str(text or "").strip())
        self.fit()

    def text(self):
        return self.toPlainText()

    def needed_height(self, width=None):
        width = self.viewport().width() if width is None else width
        margins = self.contentsMargins()
        return text_height(self.toPlainText(), self.font(), width) + margins.top() + margins.bottom()

    def fit(self):
        """As high as its text needs at its width; while it has no real width yet (not laid out), one line: a height
        for a width of a few pixels would make the window as high as the text is long."""
        if not self.toPlainText():
            height = 0
        elif self.viewport().width() < 8 * max(1, self.fontMetrics().averageCharWidth()):
            height = self.fontMetrics().lineSpacing() + self.contentsMargins().top() + self.contentsMargins().bottom()
        else:
            height = self.needed_height()
        if height != self.height() or self.minimumHeight() != height:
            self.setFixedHeight(height)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, self.height())

    def sizeHint(self):  # noqa: N802
        return QSize(0, self.height())

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.fit()


class MessageDialog(QDialog):
    """A message box for a long message (long_message): its icon, the text in wrapping_text (wrapped inside a URL
    too, selectable, scrolling beyond MESSAGE_SIZE), the details behind a button, the buttons below. It offers
    what inform() and confirm() use of QMessageBox (addButton, setDefaultButton, setEscapeButton, clickedButton,
    setDetailedText), so a QMessageBox, whose own sizing would cut such a message or shift it out of its place,
    is never asked to show one."""

    def __init__(self, parent, icon, title_text, text, details=None):
        super().__init__(parent)
        self.setWindowTitle(str(title_text))
        self.setObjectName("message_dialog")
        self._clicked, self._escape = None, None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        pixmap = QLabel()
        standard = {QMessageBox.Icon.Warning: QStyle.StandardPixmap.SP_MessageBoxWarning,
                    QMessageBox.Icon.Question: QStyle.StandardPixmap.SP_MessageBoxQuestion,
                    QMessageBox.Icon.Critical: QStyle.StandardPixmap.SP_MessageBoxCritical}.get(
            icon, QStyle.StandardPixmap.SP_MessageBoxInformation)
        pixmap.setPixmap(self.style().standardIcon(standard).pixmap(32, 32))
        row.addWidget(pixmap, 0, Qt.AlignmentFlag.AlignTop)
        self.text = wrapping_text(text)
        self.text.setObjectName("message_text")
        self.text.setAccessibleName(tr("Message"))
        row.addWidget(self.text, 1)
        layout.addLayout(row, 1)
        self.details = None
        self.details_button = QPushButton(tr("Show details…"))
        self.details_button.setVisible(False)
        self.details_button.clicked.connect(self._toggle_details)
        self.buttons = QDialogButtonBox()
        bottom = QHBoxLayout()
        bottom.addWidget(self.details_button)
        bottom.addWidget(self.buttons, 1)
        layout.addLayout(bottom)
        self._fit(str(text))
        if details:
            self.setDetailedText(details)

    def _fit(self, text):
        """The text's room: as wide as its longest line (at most MESSAGE_SIZE and the screen less a margin), as
        high as it needs there (at most MESSAGE_SIZE); beyond that it scrolls."""
        screen = (self.screen() or QApplication.primaryScreen())
        room = screen.availableGeometry().size() if screen is not None else QSize(*MESSAGE_SIZE) * 2
        metrics = self.text.fontMetrics()
        widest = max((metrics.horizontalAdvance(line) for line in text.split("\n")), default=0) + 8
        width = max(min(widest, MESSAGE_SIZE[0], room.width() - 160), min(320, room.width() - 160), 120)
        # (measured a scroll bar narrower, with a line to spare: QPlainTextEdit lays out a little differently)
        height = min(text_height(text, self.text.font(), width - 24) + metrics.lineSpacing(), MESSAGE_SIZE[1],
                     max(120, room.height() // 2))
        self.text.setMinimumSize(width, max(height, metrics.lineSpacing()))
        self.text.hint = QSize(width, max(height, metrics.lineSpacing()))

    def addButton(self, text, role):  # noqa: N802
        accepting = role == QMessageBox.ButtonRole.AcceptRole
        found = self.buttons.addButton(str(text), QDialogButtonBox.ButtonRole.AcceptRole if accepting else
                                       QDialogButtonBox.ButtonRole.RejectRole)
        found.clicked.connect(lambda _=False, b=found, a=accepting: self._chosen(b, a))
        return found

    def _chosen(self, chosen, accepting):
        self._clicked = chosen
        (self.accept if accepting else super().reject)()

    def setDefaultButton(self, chosen):  # noqa: N802
        chosen.setDefault(True)
        chosen.setFocus()

    def setEscapeButton(self, chosen):  # noqa: N802
        self._escape = chosen

    def reject(self):
        """Escape or the window closed: the escape button counts as chosen (as in a QMessageBox)."""
        self._clicked = self._escape
        super().reject()

    def clickedButton(self):  # noqa: N802
        return self._clicked

    def setDetailedText(self, details):  # noqa: N802
        if self.details is None:
            self.details = wrapping_text("")
            self.details.setObjectName("message_details")
            self.details.setFont(MONO)
            self.details.setMinimumHeight(120)
            self.details.setVisible(False)
            self.layout().insertWidget(1, self.details, 1)
        self.details.setPlainText(str(details))
        self.details_button.setVisible(True)

    def _toggle_details(self):
        """Show or hide the details: the dialog grows by their room (within the screen) and shrinks back, so the
        message, the details and the buttons never lie over each other and the button stays where it can be hit."""
        shown = not self.details.isVisible()
        if shown:
            self._closed_size = self.size()
        self.details.setVisible(shown)
        self.details_button.setText(tr("Hide details…") if shown else tr("Show details…"))
        self.layout().activate()
        if shown:
            screen = self.screen() or QApplication.primaryScreen()
            room = screen.availableGeometry().size() if screen is not None else QSize(*MESSAGE_SIZE) * 2
            wanted = self.sizeHint().expandedTo(self.minimumSizeHint())
            extra = max(self.details.minimumHeight(), min(MESSAGE_SIZE[1], room.height() // 3))
            self.resize(min(max(self.width(), wanted.width()), room.width() - 40),
                        min(max(self.height() + extra, wanted.height()), room.height() - 40))
        else:
            self.resize(getattr(self, "_closed_size", self.sizeHint()))


def _box(parent, icon, title_text, text, details=None):
    if long_message(text):
        return MessageDialog(parent, icon, title_text, text, details)
    box = QMessageBox(icon, str(title_text), str(text), QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    label = box.findChild(QLabel, "qt_msgbox_label")
    if label is not None:
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if details:
        box.setDetailedText(str(details))  # always plain text
    return box


def inform(parent, title_text, text, details=None, warning=False, modal=True):
    """A message with OK; inform() returns at once - no one waits for an OK (an event loop waiting for it would hold
    back every answer given meanwhile: nested loops return last-in, first-out). MODAL: the page PARENT is on waits
    behind a veil until OK (show_veiled); not MODAL (the outcome of a finished background job): nothing is veiled,
    the Approvals page stays usable while it is open."""
    if AUTO_ANSWER is not None:
        SHOWN.append((title_text, text, details))
        return
    box = _box(parent, QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information, title_text, text, details)
    box.addButton(tr("OK"), QMessageBox.ButtonRole.AcceptRole)
    (show_veiled if modal else show_modeless)(box)


def warn(parent, title_text, text, details=None, modal=True):
    inform(parent, title_text, text, details, warning=True, modal=modal)


# The log in a window of its own (set by the main window): Ctrl+Shift+L opens it from any dialog too.
LOG_WINDOW_OPENER = None
LOG_WINDOW_KEYS = "Ctrl+Shift+L"


def page_of(widget):
    """The page WIDGET is on, else WIDGET itself when it is no window; None for a window (a dialog of the main window
    veils nothing). The page: the widget directly in a QStackedWidget (a page of the window, of a wizard...) - not in
    a QTabWidget's own: a tab's dialog veils the page the tabs are on (the other tabs would change what it asks
    about), or the tabs as a whole where they lie in a window directly. A widget kept elsewhere for its page (the
    Board's panels in docks of the window) names that page: its attribute veil_page."""
    from PySide6.QtWidgets import QStackedWidget, QTabWidget
    current, tabs, seen = widget, None, set()
    while current is not None and not current.isWindow():
        owner = getattr(current, "veil_page", None)
        if owner is not None and id(owner) not in seen:
            seen.add(id(owner))
            current = owner
            continue
        parent = current.parentWidget()
        if isinstance(parent, QTabWidget):
            tabs = parent  # (the outermost counts)
        elif isinstance(parent, QStackedWidget) and not isinstance(parent.parentWidget(), QTabWidget):
            return current
        current = parent
    if tabs is not None:
        return tabs
    return widget if widget is not None and not widget.isWindow() else None


def veiled_parts(widget):
    """What a dialog of WIDGET veils: its page (page_of) and what the page names besides with veil_also() (the
    Board: its panels in docks of the window), none twice and none inside another. Empty for a window."""
    page = page_of(widget)
    if page is None:
        return []
    also, current, seen = [], widget, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        named = getattr(current, "veil_also", None)
        if callable(named):
            also = list(named())
            break
        if current is page:
            break
        current = getattr(current, "veil_page", None) or current.parentWidget()
    parts = [page]
    for part in also:
        try:
            if part is not None and not any(p is part or p.isAncestorOf(part) for p in parts):
                parts.append(part)
        except RuntimeError:  # (deleted)
            continue
    return parts


def _alive(widget):
    try:
        from shiboken6 import isValid
    except ImportError:  # pragma: no cover
        return widget is not None
    return widget is not None and isValid(widget)


class PageVeil(QWidget):
    """A translucent cover over the page a dialog came from, while the dialog is open: clicks on the page bring
    the dialog up instead (the page's folder, host or board stay as the question saw them). Disabling the page
    would disable the dialog too (Qt disables child windows with their parent). Nested dialogs of one page share
    one veil. The keyboard does not reach the page either: the focus moves from the page to the veil (and back when
    it goes), Tab stays there, and the page's own shortcuts do nothing meanwhile (the window's own still work)."""

    @classmethod
    def over(cls, page, dialog):
        if page is None:
            return None
        veil = page.findChild(PageVeil, "pageVeil", Qt.FindChildOption.FindDirectChildrenOnly)
        if veil is None:
            veil = cls(page)
        veil.dialogs.append(dialog)
        veil.setGeometry(page.rect())
        veil.show()
        veil.raise_()
        veil.guard_shortcuts()
        veil.take_focus()
        return veil

    def __init__(self, page):
        super().__init__(page)
        self.setObjectName("pageVeil")
        self.dialogs = []
        self.previous = None  # where the focus was in the page: it goes back there
        self.guarded = []  # the page's QActions and QShortcuts held back meanwhile
        self.setCursor(Qt.CursorShape.ForbiddenCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(tr("A dialog of this page is open: answer it first"))
        page.installEventFilter(self)  # (its own page only: follows its size)
        QApplication.instance().focusChanged.connect(self.focus_moved)

    def guard_shortcuts(self):
        """The page's shortcuts (its QActions and QShortcuts with a key, whatever their context) do nothing while it
        is veiled: their shortcut events are held back here (a filter on each of them, never on the application)."""
        from PySide6.QtGui import QAction, QShortcut
        page = self.parentWidget()
        for found in [*page.findChildren(QAction), *page.findChildren(QShortcut)]:
            keyed = bool(found.shortcuts()) if isinstance(found, QAction) else not found.key().isEmpty()
            if keyed and not any(found is g for g in self.guarded):
                found.installEventFilter(self)
                self.guarded.append(found)

    def take_focus(self):
        """The focus of the page's window, if it is in the page, to the veil (the window gives it back to the veil
        when it is active again)."""
        page = self.parentWidget()
        focus = page.window().focusWidget()
        if focus is not None and focus is not self and page.isAncestorOf(focus):
            if self.previous is None:
                self.previous = focus
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def focus_moved(self, _old, now):
        """The focus went into the veiled page some other way (Tab from outside it, a widget's setFocus): to the
        veil instead."""
        try:
            page = self.parentWidget()
            if now is not None and now is not self and self.isVisible() and page is not None and \
                    page.isAncestorOf(now):
                self.setFocus(Qt.FocusReason.OtherFocusReason)
        except RuntimeError:  # (deleted meanwhile)
            pass

    def focusNextPrevChild(self, _next):  # noqa: N802
        return True  # Tab stays on the veil

    def eventFilter(self, watched, event):  # noqa: N802
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.Shortcut and watched is not self.parentWidget():
            self.bring_up()  # one of the page's shortcuts: held back
            return True
        if event.type() == QEvent.Type.Resize and watched is self.parentWidget():
            self.setGeometry(watched.rect())
        return False

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        color = QColor(self.palette().color(QPalette.ColorRole.Window))
        color.setAlpha(150)
        painter.fillRect(self.rect(), color)

    def mousePressEvent(self, event):  # noqa: N802
        self.bring_up()
        event.accept()

    def keyPressEvent(self, event):  # noqa: N802
        self.bring_up()
        event.accept()

    def bring_up(self):
        for dialog in reversed(self.dialogs):
            try:
                if dialog.isVisible():
                    dialog.raise_()
                    dialog.activateWindow()
                    return
            except RuntimeError:
                continue

    def release(self, dialog):
        """DIALOG ended; the veil goes with the page's last one (the focus back where it was in the page)."""
        self.dialogs = [d for d in self.dialogs if d is not dialog]
        if self.dialogs:
            return
        try:
            QApplication.instance().focusChanged.disconnect(self.focus_moved)
        except (RuntimeError, TypeError):
            pass
        for guarded in self.guarded:
            if _alive(guarded):
                guarded.removeEventFilter(self)
        self.guarded = []
        try:
            self.setObjectName("")  # (deleted later: a dialog asked meanwhile gets a veil of its own)
            page = self.parentWidget()
            if page is not None:
                page.removeEventFilter(self)
                previous = self.previous
                if page.window().focusWidget() is self and _alive(previous) and previous.isVisible() and \
                        page.isAncestorOf(previous):
                    previous.setFocus(Qt.FocusReason.OtherFocusReason)
            self.previous = None
            self.hide()
            self.deleteLater()
        except RuntimeError:  # the page went first
            pass


def veil(parent, dialog):
    """Veil what a dialog of PARENT veils (veiled_parts) while DIALOG is open; returns the veils."""
    return [PageVeil.over(part, dialog) for part in veiled_parts(parent)] if parent is not None else []


RUNNING_DIALOGS = {}  # (class name, the window it belongs to) -> the dialog of nonblocking_dialogs() waiting for it


def open_veiled(dialog, key=None):
    """DIALOG (about to be shown) veils its page (veil) until it ends - when it is finished or deleted, at once (not
    when the event loop its caller waits in returns: nested loops return last-in, first-out, and an answer given while
    another dialog waits would keep its page veiled until that one is answered); its entry KEY of RUNNING_DIALOGS goes
    then too. Returns the function that ends it (once; the caller calls it as well when it stops waiting)."""
    veils = veil(dialog.parentWidget(), dialog)
    done = []

    def end(*_):
        if done:
            return
        done.append(True)
        if key is not None and RUNNING_DIALOGS.get(key) is dialog:
            del RUNNING_DIALOGS[key]
        for each in veils:
            each.release(dialog)
        for signal_ in (dialog.finished, dialog.destroyed):
            try:
                signal_.disconnect(end)
            except (RuntimeError, TypeError):
                pass
    dialog.finished.connect(end)
    dialog.destroyed.connect(end)
    return end


def show_veiled(dialog):
    """Show DIALOG (a message with OK only, a text to read) without anyone waiting for it: it blocks nothing, its page
    waits behind a veil until it is closed; deleted when it is closed. Returns DIALOG."""
    log_window_shortcut(dialog)
    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.setModal(False)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    open_veiled(dialog)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def nonblocking_dialogs(platform=sys.platform):
    """Every dialog run with exec() is a window of its own that blocks nothing: the main window, the log window and
    the other dialogs stay usable while it is open (Qt's exec() made it application modal: one dialog left open
    blocked the whole app). The caller still waits for the answer - in an event loop of its own, while the app goes
    on. A second exec() of a dialog of the same kind for the same window brings the open one to the front and is
    cancelled (no two of the same stacked on each other). File dialogs block their window only (a native one
    needs it), a dialog given a modality of its own keeps it, and on macOS a file dialog is a sheet as before.
    Ctrl+Shift+L opens the log window from any of them. Once per process."""
    if getattr(QDialog.exec, "nonblocking", False):
        return
    original = QDialog.exec
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QFileDialog, QInputDialog

    def exec_nonblocking(dialog, *args):
        log_window_shortcut(dialog)
        if isinstance(dialog, QMessageBox):  # (Qt makes every message box application modal itself)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
        if dialog.windowModality() != Qt.WindowModality.NonModal:  # a modality chosen for it (the open questions
            return original(dialog, *args)  # above a dialog)
        if isinstance(dialog, QFileDialog):
            if platform != "darwin" and dialog.parentWidget() is not None:
                dialog.setWindowModality(Qt.WindowModality.WindowModal)
            return original(dialog, *args)
        parent = dialog.parentWidget()
        # Only a dialog class of the app's own is kept to one per parent (Settings, the wizard...): a message box,
        # a plain QDialog and a second question of another part are all shown - none answered for the human.
        own = type(dialog) not in (QDialog, QMessageBox, MessageDialog, QInputDialog)
        key = (type(dialog).__name__, id(parent)) if own and parent is not None else None
        running = RUNNING_DIALOGS.get(key) if key is not None else None
        if running is not None and running is not dialog:
            try:
                if running.isVisible():
                    running.raise_()
                    running.activateWindow()
                    dialog.setResult(QDialog.DialogCode.Rejected)
                    return QDialog.DialogCode.Rejected
            except RuntimeError:  # deleted meanwhile
                pass
        if key is not None:
            RUNNING_DIALOGS[key] = dialog
        dialog.setModal(False)  # (a message box makes itself modal)
        # The part of the window that asked waits behind a veil: what the answer acts on (its folder, its host,
        # its board) cannot change under the question. The rest of the app stays usable. The veil goes as soon as
        # the dialog ends, even while the caller still waits for another dialog's loop to return (open_veiled).
        close = open_veiled(dialog, key)
        loop = QEventLoop()
        ended = []

        def end(*_):
            ended.append(True)
            loop.quit()

        def finished(code):
            results.append(code)  # (a dialog deleted on close cannot be asked afterwards)
            end()
        results = []
        dialog.finished.connect(finished)
        dialog.destroyed.connect(end)  # (deleted with its parent: the caller is not left waiting for ever)
        try:
            dialog.setResult(QDialog.DialogCode.Rejected)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            if not ended:
                loop.exec()
        finally:
            close()
            for signal, slot in ((dialog.finished, finished), (dialog.destroyed, end)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        try:
            ending = QApplication.closingDown() or bool(getattr(parent.window(), "shut_down", False)) \
                if parent is not None else QApplication.closingDown()
        except RuntimeError:  # its window is gone
            ending = True
        if ending:  # the window ended while it was open: what the answer would start does not run any more
            return QDialog.DialogCode.Rejected
        if results:
            return results[-1]
        try:
            return dialog.result()
        except RuntimeError:  # deleted: cancelled
            return QDialog.DialogCode.Rejected
    exec_nonblocking.nonblocking = True
    exec_nonblocking.__doc__ = original.__doc__
    QDialog.exec = exec_nonblocking
    # Qt's own QMessageBox.information/warning/critical/question run their box in C++ (application modal): the same
    # box, made here and run with the exec() above.
    icons = {"information": QMessageBox.Icon.Information, "warning": QMessageBox.Icon.Warning,
             "critical": QMessageBox.Icon.Critical, "question": QMessageBox.Icon.Question}
    for name, icon in icons.items():
        def static(parent, title, text, buttons=None, defaultButton=QMessageBox.StandardButton.NoButton,  # noqa: N803
                   icon=icon, name=name):
            if buttons is None:
                buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No if name == "question" \
                    else QMessageBox.StandardButton.Ok
            box = QMessageBox(icon, title, text, buttons, parent)
            if defaultButton != QMessageBox.StandardButton.NoButton:
                box.setDefaultButton(defaultButton)
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            answer = box.exec()
            try:
                return QMessageBox.StandardButton(answer)
            except ValueError:
                return QMessageBox.StandardButton.NoButton
        setattr(QMessageBox, name, staticmethod(static))
    input_statics()


INT_LIMIT = 2147483647  # Qt's own defaults of QInputDialog.getInt/getDouble


def input_statics():
    """Qt's QInputDialog.getText/getMultiLineText/getItem/getInt/getDouble run their dialog in C++ (application
    modal, the whole app blocked): the same dialog, with the same arguments and defaults, made here and run with the
    exec() of nonblocking_dialogs (its page veiled, nothing blocked). Each returns what Qt's does: (value, ok), the
    value when cancelled as Qt's (getText: "", getItem: the current item, getInt/getDouble: the value given)."""
    from PySide6.QtWidgets import QInputDialog

    def run(dialog, read, cancelled):
        try:
            code = dialog.exec()
            ok = int(getattr(code, "value", code)) != 0
            value = read() if ok else cancelled
        except RuntimeError:  # deleted with its parent meanwhile: cancelled
            return cancelled, False
        try:
            dialog.deleteLater()
        except RuntimeError:
            pass
        return value, ok

    def made(parent, title, label, flags):
        dialog = QInputDialog(parent) if flags is None else QInputDialog(parent, flags)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        return dialog

    def get_text(parent, title, label, echo=QLineEdit.EchoMode.Normal, text="", flags=None,
                 inputMethodHints=Qt.InputMethodHint.ImhNone):  # noqa: N803
        dialog = made(parent, title, label, flags)
        dialog.setTextEchoMode(echo)
        dialog.setTextValue(text)
        dialog.setInputMethodHints(inputMethodHints)
        return run(dialog, dialog.textValue, "")

    def get_multi_line_text(parent, title, label, text="", flags=None,
                            inputMethodHints=Qt.InputMethodHint.ImhNone):  # noqa: N803
        dialog = made(parent, title, label, flags)
        dialog.setOption(QInputDialog.InputDialogOption.UsePlainTextEditForTextInput, True)
        dialog.setTextValue(text)
        dialog.setInputMethodHints(inputMethodHints)
        return run(dialog, dialog.textValue, "")

    def get_item(parent, title, label, items, current=0, editable=True, flags=None,
                 inputMethodHints=Qt.InputMethodHint.ImhNone):  # noqa: N803
        items = [str(item) for item in items]
        text = items[current] if 0 <= current < len(items) else ""
        dialog = made(parent, title, label, flags)
        dialog.setComboBoxItems(items)
        dialog.setTextValue(text)
        dialog.setComboBoxEditable(editable)
        dialog.setInputMethodHints(inputMethodHints)
        return run(dialog, dialog.textValue, text)

    def get_int(parent, title, label, value=0, minValue=-INT_LIMIT, maxValue=INT_LIMIT, step=1,  # noqa: N803
                flags=None):
        dialog = made(parent, title, label, flags)
        dialog.setIntRange(minValue, maxValue)
        dialog.setIntValue(value)
        dialog.setIntStep(step)
        return run(dialog, dialog.intValue, value)

    def get_double(parent, title, label, value=0.0, minValue=-INT_LIMIT, maxValue=INT_LIMIT, decimals=1,  # noqa: N803
                   flags=None, step=1.0):
        dialog = made(parent, title, label, flags)
        dialog.setDoubleDecimals(decimals)
        dialog.setDoubleRange(minValue, maxValue)
        dialog.setDoubleStep(step)
        dialog.setDoubleValue(value)
        return run(dialog, dialog.doubleValue, value)
    for name, function in (("getText", get_text), ("getMultiLineText", get_multi_line_text), ("getItem", get_item),
                           ("getInt", get_int), ("getDouble", get_double)):
        setattr(QInputDialog, name, staticmethod(function))


TITLE_BAR = 32  # what a window manager's frame adds above a window, about (unknown before the window is shown)
SCREEN_MARGIN = 8


def fit_on_screen(widget):
    """WIDGET (a dialog about to be shown) no larger than the free area of its screen (the screen of the window it
    belongs to) and inside it: a dialog sized for its content reached past the screen's edge, its buttons out of
    reach. Its minimum size gives way where it must."""
    parent = widget.parentWidget()
    screen = (parent.window().screen() if parent is not None else None) or widget.screen() or \
        QApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry().adjusted(SCREEN_MARGIN, SCREEN_MARGIN + TITLE_BAR, -SCREEN_MARGIN,
                                                -SCREEN_MARGIN)
    if area.width() < 200 or area.height() < 150:
        return
    width, height = min(widget.width(), area.width()), min(widget.height(), area.height())
    if widget.minimumWidth() > width:
        widget.setMinimumWidth(width)
    if widget.minimumHeight() > height:
        widget.setMinimumHeight(height)
    if (width, height) != (widget.width(), widget.height()):
        widget.resize(width, height)
    x = min(max(widget.x(), area.left()), area.right() - width + 1)
    y = min(max(widget.y(), area.top()), area.bottom() - height + 1)
    if (x, y) != (widget.x(), widget.y()):
        widget.move(x, y)


def fitting_dialogs():
    """Every dialog and message box fits on its screen when it is shown (fit_on_screen). Once per process."""
    from PySide6.QtWidgets import QMessageBox as Box
    for cls in (QDialog, Box):
        original = cls.showEvent
        if getattr(original, "fits", False):
            continue

        def show_event(dialog, event, original=original):
            if not event.spontaneous():
                try:
                    fit_on_screen(dialog)
                except RuntimeError:  # (deleted meanwhile)
                    pass
            return original(dialog, event)
        show_event.fits = True
        cls.showEvent = show_event


def log_window_shortcut(dialog):
    """Ctrl+Shift+L in DIALOG opens the log window of its own (once per dialog; nothing before the window set
    LOG_WINDOW_OPENER)."""
    if LOG_WINDOW_OPENER is None or dialog.property("logWindowKeys"):
        return
    from PySide6.QtGui import QKeySequence, QShortcut
    shortcut = QShortcut(QKeySequence(LOG_WINDOW_KEYS), dialog)
    shortcut.activated.connect(lambda: LOG_WINDOW_OPENER is not None and LOG_WINDOW_OPENER())
    dialog.setProperty("logWindowKeys", True)


def show_modeless(dialog):
    """Show DIALOG without blocking any window; it is deleted when it is closed."""
    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.show()
    return dialog


def confirm(parent, title_text, text, accept, destructive=False, details=None, reject=None):
    """True if the human chose ACCEPT (a verb, e.g. "Delete"); the safe choice is the default."""
    if AUTO_ANSWER is not None:
        SHOWN.append((title_text, text, details))
        return bool(AUTO_ANSWER)
    box = _box(parent, QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question, title_text, text,
               details)
    yes = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(reject or tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
    if destructive:
        yes.setProperty("role", "danger")
    box.setDefaultButton(no if destructive else yes)
    box.setEscapeButton(no)
    # The answer as it is clicked: the box is gone afterwards when its parent went while it was open (an editor
    # closed meanwhile) - not chosen then.
    chosen = []
    yes.clicked.connect(lambda *_: chosen.append(True))
    box.exec()
    return bool(chosen)


def ask_first(parent, action, title_text, text, accept, detail=None, details=None, reject=None):
    """The app's one confirmation policy: True when ACTION (a key of backend.NEEDS_CONFIRM or APP_NEEDS_CONFIRM) may
    go ahead - at once when backend.needs_confirm(ACTION, DETAIL) says it needs no confirmation, else only when the
    human chose ACCEPT in a destructive confirmation (Cancel the default)."""
    from . import backend  # here: backend never imports Qt, and the widgets need no backend otherwise
    if backend.needs_confirm(action, detail) is None:
        return True
    return confirm(parent, title_text, text, accept, destructive=True, details=details, reject=reject)


# -- fields where a secret's value is typed: a "Show value" toggle beside each, on by default --
# What is typed is shown while typing unless the human turns it off; the choice is one for every such field, kept in the
# app's state (state.json: SHOW_VALUES). A value already stored is never read back into any field.
SHOW_VALUES = "show_secret_values"
_values_shown = [True]  # without a State (tests, a dialog before the window): this run's choice
_toggles = []  # the toggles shown now: a change in one is the change of all


def values_shown():
    from . import state
    kept = state.current()
    return kept.get(SHOW_VALUES, True, bool) if kept is not None else _values_shown[0]


def set_values_shown(on):
    from . import state
    _values_shown[0] = bool(on)
    kept = state.current()
    if kept is not None:
        kept.set(SHOW_VALUES, bool(on))
    try:
        from shiboken6 import isValid
    except ImportError:  # pragma: no cover
        isValid = lambda _w: True  # noqa: E731
    _toggles[:] = [t for t in _toggles if isValid(t)]
    for toggle in _toggles:
        toggle.blockSignals(True)
        toggle.setChecked(bool(on))
        toggle.blockSignals(False)
        apply_echo(toggle.edit)


def apply_echo(edit):
    """EDIT's echo mode: hidden (Password) only for a secret's value while "Show value" is off."""
    from PySide6.QtWidgets import QLineEdit
    hidden = bool(edit.property("secret_value")) and not values_shown()
    edit.setEchoMode(QLineEdit.EchoMode.Password if hidden else QLineEdit.EchoMode.Normal)


def secret_value(edit, secret=True):
    """EDIT (a QLineEdit for a secret's value) with its "Show value" toggle beside it: the row's widget to lay out.
    SECRET False: not a secret's value now (e.g. a token field that names a variable): shown as typed, the toggle
    greyed out. The toggle is edit.show_toggle."""
    from PySide6.QtWidgets import QCheckBox
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    toggle = QCheckBox(tr("Show value"))
    toggle.setAccessibleName(tr("Show value"))
    toggle.setToolTip(tr("Show what is typed here (a value kept already is never read back)"))
    toggle.edit = edit
    toggle.setChecked(values_shown())
    toggle.toggled.connect(set_values_shown)
    edit.show_toggle = toggle
    _toggles.append(toggle)
    row.addWidget(edit, 1)
    row.addWidget(toggle)
    mark_secret(edit, secret)
    return box


def mark_secret(edit, secret=True):
    """Whether EDIT holds a secret's value now (its echo and toggle follow)."""
    edit.setProperty("secret_value", bool(secret))
    toggle = getattr(edit, "show_toggle", None)
    if toggle is not None:
        toggle.setEnabled(bool(secret))
    apply_echo(edit)


def show_text(parent, title_text, text, modal=True):
    """TEXT in a read-only view; returns at once, MODAL: its page veiled until it is closed (see inform)."""
    if AUTO_ANSWER is not None:
        SHOWN.append((title_text, text, None))
        return
    dialog = QDialog(parent)
    dialog.setWindowTitle(str(title_text))
    dialog.resize(760, 520)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit(str(text))
    view.setReadOnly(True)
    view.setFont(MONO)
    layout.addWidget(view)
    buttons = QDialogButtonBox()
    buttons.addButton(tr("Close"), QDialogButtonBox.ButtonRole.RejectRole)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    (show_veiled if modal else show_modeless)(dialog)


class Notice(QWidget):
    """An inline message of a page: a sentence in plain words, the raw details expandable (plain text)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.label = plain_label()
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.toggle = QToolButton()
        self.toggle.setText(tr("Details"))
        self.toggle.setToolTip(tr("Show or hide what sandbox-ai printed"))
        disclosure(self.toggle)
        self.toggle.toggled.connect(lambda on: self.details.setVisible(on))
        close = QToolButton()
        close.setText("×")
        close.setToolTip(tr("Hide this message"))
        close.clicked.connect(self.clear)
        row.addWidget(self.label, 1)
        row.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(MONO)
        self.details.setMaximumHeight(160)
        self.details.hide()
        layout.addWidget(self.details)
        self.hide()

    def show_message(self, text, level="info", details=None):
        self.label.setText(str(text))
        self.label.setProperty("role", {"warn": "notice-warn", "error": "notice-error", "ok": "notice-ok"}.get(level))
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)
        details = (details or "").strip()
        self.details.setPlainText(details)
        self.toggle.setVisible(bool(details))
        self.toggle.setChecked(False)
        self.details.hide()
        self.show()

    def text(self):
        return self.label.text() if self.isVisibleTo(self.parentWidget() or self) or not self.isHidden() else ""

    def clear(self):
        self.label.setText("")
        self.details.setPlainText("")
        self.hide()


# -- background work --

# The command becomes the leader of a process group of its own, so that Cancel reaches its children too.
SETSID = ("import os, sys\n"
          "os.setsid()\n"
          "try:\n"
          "    os.execvp(sys.argv[1], sys.argv[1:])\n"
          "except OSError as exc:\n"
          "    sys.stderr.write(f'cannot run {sys.argv[1]!r}: {exc.strerror}\\n')\n"
          "    sys.exit(127)\n")
CANNOT_RUN = "cannot run "  # the wrapper's own message: the command did not start (status -1, not 127)


class AsyncCommand(QObject):
    """A command run with QProcess, without blocking the window: finished(exit status, stdout, stderr).
    A command that cannot start, crashes, is cancelled or takes longer than TIMEOUT seconds finishes with
    status -1. INPUT (bytes) is written to its standard input, which is then closed (secrets go this way)."""
    finished = Signal(int, str, str)
    KILL_AFTER = 1500  # ms between SIGTERM and SIGKILL when a command is stopped
    stopped = {}  # {QProcess: pid} of every stopped command still running (end_stopped at the app's end)

    def __init__(self, timeout=120, parent=None):
        super().__init__(parent)
        self.timeout, self.process, self.cancelled = timeout, None, False
        self.stopping = set()  # stopped commands that have not ended yet (kept until they have)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.stop)

    @property
    def running(self):
        return self.process is not None

    def start(self, argv, input=None):
        if self.process is not None:
            return False
        # Without a parent: a QProcess child that deleteLater() was called on crashes PySide6 when its parent
        # is garbage-collected first. The process is kept in self.process while it runs.
        process = QProcess()
        process.setProgram(sys.executable)
        process.setArguments(["-I", "-c", SETSID, *argv])
        if input is None:
            process.setStandardInputFile(QProcess.nullDevice())
        process.finished.connect(lambda code, status, p=process: self._done(
            p, code if status == QProcess.ExitStatus.NormalExit and not self.cancelled else -1))
        process.errorOccurred.connect(lambda error, p=process: error == QProcess.ProcessError.FailedToStart
                                      and self._done(p, -1, p.errorString()))
        self.process, self.cancelled = process, False
        self.timer.start(int(self.timeout * 1000))
        self.started_at = time.monotonic()
        applog.debug("command", argv=[str(a) for a in argv][-6:], timeout=self.timeout)
        process.start()
        if input is not None:
            process.write(input if isinstance(input, bytes) else str(input).encode())
            process.closeWriteChannel()
        return True

    def _done(self, process, status, error=""):
        try:
            from shiboken6 import isValid
            if not isValid(process):  # destroyed while it still ran (the app quit): nothing left to read
                return
        except ImportError:  # pragma: no cover
            pass
        if process is not self.process:
            if process in self.stopping:  # a stopped command has ended: no SIGKILL for it any more
                self.stopping.discard(process)
                AsyncCommand.stopped.pop(process, None)
                process.deleteLater()
            return
        self.process = None
        self.timer.stop()
        out = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(process.readAllStandardError()).decode("utf-8", "replace") + error
        if status == 127 and err.startswith(CANNOT_RUN):
            status = -1
        applog.debug("command done", status=status, seconds=round(time.monotonic() - getattr(self, "started_at", 0), 2),
                     error=err.strip()[-300:] if status else "")
        if process.state() == QProcess.ProcessState.NotRunning:
            process.deleteLater()
        else:  # stopped: deleted once it has ended (deleting a running QProcess waits for it)
            self.stopping.add(process)
            AsyncCommand.stopped[process] = process.processId()
        self.finished.emit(status, out, err)

    def stop(self):
        """End the command and every process it started (its process group): SIGTERM now, SIGKILL after
        KILL_AFTER ms if it still runs. Nothing is waited for (this runs on the GUI thread): the command counts
        as stopped at once (finished(-1, ...)), a new one may start right away."""
        process = self.process
        if process is None:
            return
        self.cancelled = True
        pid = process.processId()
        if pid:
            self._terminate(process, pid)
        else:  # still starting: ended as soon as it runs
            process.started.connect(lambda p=process: self._terminate(p, p.processId()))
        self._done(process, -1, tr("stopped"))

    def _terminate(self, process, pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        QTimer.singleShot(self.KILL_AFTER, self, lambda p=process, n=pid: self._kill(p, n))

    @classmethod
    def end_stopped(cls, grace=1.5):
        """When the app quits (no timer fires any more): one wait of at most GRACE seconds for every stopped
        command still running, then SIGKILL to the groups of those still there."""
        deadline = time.monotonic() + grace
        for process, pid in list(cls.stopped.items()):
            if process.state() != QProcess.ProcessState.NotRunning and \
                    not process.waitForFinished(max(0, int((deadline - time.monotonic()) * 1000))):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _kill(self, process, pid):
        """SIGKILL to the group of a stopped command whose lead has not ended yet (while it has not, the group
        number cannot have been reused)."""
        if process in self.stopping and process.state() != QProcess.ProcessState.NotRunning:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


_keepers = []


class _Run:
    """One call's run on a worker thread, as a thread is asked about it: is_alive() until the call has ended (its
    result handed on), join(timeout) waits for that. The thread itself goes on to the next call."""
    __slots__ = ("ended",)

    def __init__(self):
        self.ended = threading.Event()

    def is_alive(self):
        return not self.ended.is_set()

    def join(self, timeout=None):
        self.ended.wait(timeout)


class _Workers:
    """The daemon threads the ThreadCalls run on: a thread that has finished a call waits up to IDLE seconds for the
    next one, so a page that polls every second (the questions) or every few seconds (the starts, the hosts) does not
    start a new thread each time. Never a queue: a call starts at once on a waiting thread or on a new one, however
    many run (a host that hangs for its whole timeout holds up no other read). At most KEPT threads wait."""
    IDLE = 30.0
    KEPT = 8

    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = []  # [_Worker] with nothing to do
        self.made = 0
        self.held = None  # [function] while calls are held (hold_background), else None

    def hold(self):
        with self.lock:
            if self.held is None:
                self.held = []

    def release(self):
        with self.lock:
            held, self.held = self.held or [], None
        for function in held:
            self.run(function)

    def run(self, function):
        with self.lock:
            if self.held is not None:
                self.held.append(function)
                return
            worker = self.waiting.pop() if self.waiting else None
            if worker is None:
                self.made += 1
                number = self.made
        if worker is None:
            worker = _Worker(self, number)
            worker.job = function
            worker.thread.start()
        else:
            worker.give(function)

    def done(self, worker):
        """WORKER finished a call: True if it waits for the next one, False if it ends (enough wait already)."""
        with self.lock:
            if len(self.waiting) >= self.KEPT:
                return False
            self.waiting.append(worker)
            return True

    def retire(self, worker):
        """WORKER waited IDLE seconds for nothing: True if it may end (no call was given to it meanwhile)."""
        with self.lock:
            if worker in self.waiting:
                self.waiting.remove(worker)
                return True
            return False

    def count(self):
        with self.lock:
            return len(self.waiting)


class _Worker:
    def __init__(self, pool, number):
        self.pool, self.job, self.given, self.taken = pool, None, threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.loop, name=f"worker-{number}", daemon=True)

    def give(self, function):
        """FUNCTION to run, returning once the thread has taken it - as Thread.start() returns once the thread runs:
        the call's function starts before its caller goes on (a closure the caller changes right after, say)."""
        self.taken.clear()
        self.job = function
        self.given.set()
        self.taken.wait(5)

    def loop(self):
        while True:
            job, self.job = self.job, None
            self.taken.set()
            job()
            del job  # (nothing of the call stays referenced by a waiting thread)
            if not self.pool.done(self):
                return
            if not self.given.wait(self.pool.IDLE):
                if self.pool.retire(self):
                    return
                self.given.wait()  # taken from the waiting ones just now: its call comes
            self.given.clear()


_workers = _Workers()


def hold_background():
    """The calls started from now on wait, in their order, until release_background(): while the window makes itself
    and paints for the first time, the first pages' reads (sandbox-ai's modules loaded and run in their threads, a
    preset catalogue read) would share Python with it - about a seventh of the time to the first paint."""
    _workers.hold()


def release_background():
    """The calls held since hold_background() start now, in their order; those started later at once. (Again: nothing.)"""
    _workers.release()


def _keeper():
    """The parent of every ThreadCall (made on the GUI thread with the first call)."""
    if not _keepers:
        _keepers.append(QObject())
    return _keepers[0]


class ThreadCall(QObject):
    """FUNCTION(*args) in a daemon thread (one of _workers, kept for the next call); finished(result, error) is
    emitted on the GUI thread (the thread that made the ThreadCall), so whatever is connected to it, a lambda too,
    may touch widgets. Whatever the function raises - SystemExit and KeyboardInterrupt too - comes back as ERROR: a
    call always reports.

    An abandoned call (abandon_all() when the window closes) emits nothing: the check and the emit happen under
    one lock, so no result reaches a receiver after it was given up. Once it has reported (or was abandoned and
    its thread has ended), its connections are cut and the Qt object is deleted, so nothing it held stays alive:
    a page that polls makes thousands of calls a day. WORK: what a human started (an install, a copy), not a read;
    wait_all(work_only=True) waits for those alone."""
    finished = Signal(object, object)
    _result = Signal(object, object)  # from the worker thread to this object's (the GUI) thread: queued
    _disposed = Signal()  # from the worker thread, for an abandoned call: let go of it on the GUI thread
    _alive = set()
    _lock = threading.Lock()

    def __init__(self, function, *args, work=True):
        # A child of the keeper: Qt owns it, so the Python object going (on whatever thread drops it last) never
        # deletes it - deleteLater() does, on the GUI thread, once it has reported (never inside its own event).
        super().__init__(_keeper())
        self.function, self.args, self.abandoned, self.thread, self.work = function, args, False, None, work
        self.reported, self.state = False, None  # state: what background() keeps for the call (see _dispose)
        with ThreadCall._lock:
            ThreadCall._alive.add(self)
        self._result.connect(self._deliver)
        self._disposed.connect(self._dispose)

    def start(self):
        """Run it on a worker thread (one waiting from an earlier call, or a new one). self.thread answers
        is_alive() and join() for this call, as a thread of its own did."""
        self.thread = _Run()
        _workers.run(self._run)
        return self

    def _run(self):
        try:
            self._call()
        finally:
            self.thread.ended.set()

    def _call(self):
        _worker.call = self
        try:
            result, error = self.function(*self.args), None
        except BaseException as exc:  # noqa: BLE001 - reported to the page; SystemExit too (argparse, sys.exit)
            result, error = None, exc
        finally:
            _worker.call = None
            if _worker_processes:
                _forget_reaped()
        with ThreadCall._lock:
            if self.abandoned:
                ThreadCall._alive.discard(self)
                try:
                    self._disposed.emit()
                except RuntimeError:  # the Qt object is gone already (the app is closing)
                    pass
                return
            try:
                self._result.emit(result, error)
            except RuntimeError:  # the Qt object is gone already (the app is closing)
                ThreadCall._alive.discard(self)

    @Slot(object, object)
    def _deliver(self, result, error):
        with ThreadCall._lock:
            ThreadCall._alive.discard(self)
            abandoned = self.abandoned
        if not abandoned:
            self.reported = True
            self.finished.emit(result, error)
        self._dispose()

    @Slot()
    def _dispose(self):
        """On the GUI thread, once the call has reported or was abandoned and its thread is done: everything it holds
        is let go of here (the function, background()'s state: pages, buttons, timers - Qt objects must never be
        deleted by the worker thread dropping the last reference), every connection is cut (a receiver that refers to
        this call would keep both alive for good: the connection is held by the Qt object), and the Qt object is
        deleted later, on this thread. Python attributes (abandoned, thread, work) stay readable for whoever still
        holds the call."""
        self.function, self.args, self.state = None, (), None
        import warnings
        with warnings.catch_warnings():  # PySide warns (RuntimeWarning) about a signal with nothing connected
            warnings.simplefilter("ignore", RuntimeWarning)
            for signal_ in (self.finished, self._result, self._disposed):
                try:
                    signal_.disconnect()
                except (RuntimeError, TypeError):  # nothing connected
                    pass
        try:
            self.deleteLater()
        except RuntimeError:
            pass

    def abandon(self):
        with ThreadCall._lock:
            self.abandoned = True

    @classmethod
    def running(cls, work_only=False):
        with cls._lock:
            return [call for call in cls._alive if call.thread is not None and call.thread.is_alive()
                    and (call.work or not work_only)]

    @classmethod
    def wait_all(cls, timeout, work_only=False):
        """Wait up to TIMEOUT seconds for every running call (WORK_ONLY: every running call that is work) to
        end; the calls still running."""
        deadline = time.monotonic() + timeout
        for call in cls.running(work_only):
            call.thread.join(max(0.0, deadline - time.monotonic()))
        return cls.running(work_only)

    @classmethod
    def abandon_all(cls):
        with cls._lock:
            for call in cls._alive:
                call.abandoned = True


# -- garbage collection on the GUI thread only --

_collectors = []


def collect_on_gui_thread(interval=250, idle_interval=2000):
    """Python's cycle collector runs in whatever thread happens to allocate: in a worker thread it may delete a Qt
    object (a dialog or page that went, held in a reference cycle), which crashes. From this call on it runs only
    here, on the GUI thread (a QTimer every INTERVAL ms, as often as Python's own thresholds would); the app's start
    and the tests call it once. While little is allocated (the app idles, in the tray say) the look comes less often,
    up to every IDLE_INTERVAL ms (doubled each time nothing was due): fewer wakeups of an idle app."""
    if _collectors:
        return _collectors[0]
    import gc
    thresholds = gc.get_threshold()
    gc.disable()
    timer = QTimer()

    def check():
        counts = gc.get_count()
        if counts[0] > thresholds[0]:
            generation = 2 if counts[1] > thresholds[1] and counts[2] > thresholds[2] else \
                1 if counts[1] > thresholds[1] else 0
            gc.collect(generation)
            if timer.interval() != interval:
                timer.setInterval(interval)
        elif counts[0] < thresholds[0] // 2 and timer.interval() < idle_interval:
            timer.setInterval(min(idle_interval, timer.interval() * 2))
    timer.timeout.connect(check)
    timer.start(interval)
    _collectors.append(timer)
    return timer


# -- the processes background work starts --

_worker = threading.local()  # .call: the ThreadCall whose function runs on this thread
_worker_processes = set()  # subprocess.Popen objects started on a ThreadCall's thread (track_worker_processes)
_worker_lock = threading.Lock()


def track_worker_processes():
    """From now on every subprocess.Popen started on a ThreadCall's thread - also deep inside sandbox-ai's modules
    (`podman pod ps` of the registry read, ssh) - is remembered until it is reaped, so that end_worker_processes()
    can end what still runs when the app quits (otherwise it outlives the app). The app's start calls it once (it
    changes subprocess.Popen for the whole process); returns a function that undoes it (tests)."""
    import subprocess
    original = subprocess.Popen.__init__
    if getattr(original, "_tracks_workers", False):
        return lambda: None

    def init(popen, *args, **kwargs):
        original(popen, *args, **kwargs)
        if getattr(_worker, "call", None) is not None:
            with _worker_lock:
                _worker_processes.add(popen)
    init._tracks_workers = True
    subprocess.Popen.__init__ = init

    def undo():
        subprocess.Popen.__init__ = original
    return undo


def end_worker_processes():
    """SIGKILL to every process a ThreadCall's thread started that has not been reaped yet (an unreaped process keeps
    its number: no other process can be hit), to its whole process group when it leads one (a command and the ssh it
    started); the number signalled. When the app quits: what reads still run is ended without a question (the work
    was asked about and stopped before)."""
    with _worker_lock:
        found = [popen for popen in _worker_processes if popen.returncode is None]
        _worker_processes.clear()
    ended = 0
    for popen in found:
        try:
            if os.getpgid(popen.pid) == popen.pid:
                os.killpg(popen.pid, signal.SIGKILL)
            else:
                os.kill(popen.pid, signal.SIGKILL)
            ended += 1
        except (ProcessLookupError, PermissionError):
            pass
    return ended


def _forget_reaped():
    with _worker_lock:
        for popen in [p for p in _worker_processes if p.returncode is not None]:
            _worker_processes.discard(popen)


def _hold(widget):
    """Disable WIDGET while a background() call that started from it runs; calls with the same widget are counted,
    so the first one to end does not enable it while another still runs. What it was before the first comes back."""
    try:
        held = widget.property("_background_held") or 0
        if not held:
            widget.setProperty("_background_enabled", widget.isEnabled())
        widget.setProperty("_background_held", held + 1)
        widget.setEnabled(False)
    except RuntimeError:  # deleted meanwhile
        pass


def _release(widget):
    try:
        held = (widget.property("_background_held") or 1) - 1
        widget.setProperty("_background_held", held)
        if not held:
            widget.setEnabled(widget.property("_background_enabled") is not False)
    except RuntimeError:  # deleted meanwhile
        pass


def held(widget):
    """True while a background() call started from WIDGET runs."""
    return bool(widget.property("_background_held"))


class _Call:
    """What one background() call keeps: never the ThreadCall itself, so nothing connected to the call refers
    back to it (see ThreadCall._dispose)."""

    def __init__(self, done, sources, owner, counted, work, name, timeout):
        self.done, self.sources, self.owner, self.counted, self.work = done, sources, owner, counted, work
        self.name, self.began, self.answered, self.timer, self.timeout = name, time.monotonic(), False, None, timeout

    def owner_alive(self):
        if self.owner is None:
            return True
        try:
            from shiboken6 import isValid
            return isValid(self.owner)
        except ImportError:  # pragma: no cover
            return True

    def ended(self, result, error):
        """The thread's result (on the GUI thread): the sources work again, the owner's count goes down, and DONE
        gets it - unless DONE got a TimeoutError already (the late result is dropped)."""
        if self.timer is not None:  # (dropped here, not in expired(): never inside its own signal)
            self.timer.stop()
            self.timer = None
        applog.debug("background done", call=self.name, seconds=round(time.monotonic() - self.began, 2),
                     error=f"{type(error).__name__}: {error}"[:300] if error is not None else "")
        for widget in self.sources:
            _release(widget)
        alive = self.owner_alive()
        if self.counted and alive:  # a late result (after the window's end) never reaches a deleted page
            self.owner._background -= 1
            if self.work:
                self.owner._work -= 1
            activity().changed.emit()
        if alive:
            self.answer(result, error)

    def expired(self):
        """No result within TIMEOUT: DONE gets a TimeoutError now. The thread cannot be stopped: its buttons stay
        disabled and its owner busy until it really ends (a second start of the same action meanwhile would run
        it twice)."""
        if self.owner_alive():
            self.answer(None, TimeoutError(tr("No answer within {seconds} s.", seconds=int(self.timeout))))

    def answer(self, result, error):
        if self.answered:
            return
        self.answered = True
        done, self.done = self.done, None
        done(result, error)


def background(function, *args, done, source=None, owner=None, timeout=None, work=False):
    """Run FUNCTION(*args) in a thread, never on the GUI thread (which must keep answering approvals);
    DONE(result, error) runs on the GUI thread, once. SOURCE (a widget or a list of them, e.g. the button
    that started it) is disabled until the thread ends; OWNER (a page) counts its running calls for busy(), and
    a late result never reaches it once it is deleted. WORK (True for what a human started that changes
    something: an install, a start, a write) also counts for working(): the window's busy bar and the question
    when it is closed. A read or a poll (the default) shows as neither, and quitting ends it without a question.
    After TIMEOUT seconds without a result, DONE gets a TimeoutError (a late result is dropped). Returns the
    ThreadCall."""
    sources = [w for w in (source if isinstance(source, (list, tuple)) else [source]) if w is not None]
    for widget in sources:
        _hold(widget)
    counted = owner is not None
    if counted:
        owner._background = getattr(owner, "_background", 0) + 1
        owner._work = getattr(owner, "_work", 0) + (1 if work else 0)
        activity().changed.emit()  # the window's busy indicator: which pages have work running
    name = getattr(function, "__qualname__", None) or repr(function)
    state = _Call(done, sources, owner, counted, work, name, timeout)
    call = ThreadCall(function, *args, work=work)
    applog.debug("background", call=name, timeout=timeout)
    # PySide holds a connected method of a plain Python object only weakly: the call keeps STATE (STATE never
    # refers to the call, so nothing is held in a circle; both go once the call has reported).
    call.state = state
    call.finished.connect(state.ended)
    if timeout:
        state.timer = QTimer()
        state.timer.setSingleShot(True)
        state.timer.timeout.connect(state.expired)
        state.timer.start(int(timeout * 1000))
    return call.start()


# -- several rows chosen at once (Ctrl+click, Shift+click): one question, then each in turn, the failures together --

LISTED = 12  # names a question about several names in full (the rest: "and N more")


def listing(names, limit=LISTED):
    """NAMES one per line for a question about several (only the first LIMIT, then how many more)."""
    names = [str(name) for name in names]
    lines = ["- " + name for name in names[:limit]]
    if len(names) > limit:
        lines.append(tr("… and {count} more", count=len(names) - limit))
    return "\n".join(lines)


def several(text, names):
    """A question's TEXT (it names how many) and below it the NAMES it is about."""
    return text + "\n\n" + listing(names)


def multi_select(view):
    """VIEW takes several rows: Ctrl+click adds or drops one, Shift+click a range from the last one (and Ctrl+A)."""
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    return view


def delete_key(view, slot, text=None):
    """The Delete key in VIEW does SLOT (what its Remove button does, to the selected rows)."""
    from PySide6.QtGui import QAction, QKeySequence
    action = QAction(text or tr("Remove…"), view)
    action.setObjectName("deleteAction")
    action.setShortcuts([key for key in QKeySequence.keyBindings(QKeySequence.StandardKey.Delete) if not key.isEmpty()]
                        or [QKeySequence("Del")])
    action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    action.triggered.connect(lambda _=False: slot())
    view.addAction(action)
    return action


def one_only(view, buttons):
    """BUTTONS act on one row of VIEW: greyed out (saying why) while several are selected."""
    def update():
        count = len(selected_rows(view))
        for button in buttons:
            if button is not None and not held(button):
                set_enabled(button, count <= 1, tr("Acts on one row: {count} are selected.", count=count)
                            if count > 1 else None)
    view.itemSelectionChanged.connect(update) if hasattr(view, "itemSelectionChanged") else \
        view.selectionModel().selectionChanged.connect(lambda *_: update())
    return update


def run_each(function, items, *, done, owner=None, source=None, timeout=None, work=True):
    """FUNCTION(ITEM) for each of ITEMS, one after the other in one background() thread (never at once: the same
    sandbox-ai, the same files); DONE([(item, result, error), ...]) on the GUI thread, in the order of ITEMS - an
    exception of one is its error, the others go on. TIMEOUT is for all of them together."""
    items = list(items)

    def each():
        outcomes = []
        for item in items:
            try:
                outcomes.append((item, function(item), None))
            except Exception as exc:  # noqa: BLE001 - the next one still runs; said in DONE
                outcomes.append((item, None, exc))
        return outcomes
    return background(each, owner=owner, source=source, work=work, timeout=timeout,
                      done=lambda result, error: done(result if error is None else [(i, None, error) for i in items]))


def failed(outcomes):
    """(item, why) of each of run_each's OUTCOMES that failed: an exception, or a (status, out, err) result with a
    status other than 0, or a result of False."""
    found = []
    for item, result, error in outcomes:
        if error is not None:
            found.append((item, str(error)))
        elif isinstance(result, tuple) and len(result) == 3 and result[0] != 0:
            found.append((item, (str(result[2] or "") + str(result[1] or "")).strip() or
                          tr("exit status {status}", status=result[0])))
        elif result is False:
            found.append((item, tr("not done")))
    return found


def report_each(parent, title, outcomes, name_of=str, modal=False):
    """What run_each's OUTCOMES did, in one message: nothing when all went well, else which failed and why (each by
    NAME_OF(item), its last lines) and how many went well. Returns the items that failed."""
    failures = failed(outcomes)
    if not failures:
        return []
    lines = []
    for item, why in failures:
        last = [line for line in why.strip().splitlines() if line.strip()][-3:]
        lines.append(f"- {name_of(item)}: " + " / ".join(last))
    text = tr("{failed} of {count} failed:", failed=len(failures), count=len(outcomes)) + "\n" + "\n".join(lines)
    warn(parent, title, text, modal=modal)
    return [item for item, _ in failures]


def busy(owner):
    """True while background() calls of OWNER run (reads too), or its Task runs."""
    task = getattr(owner, "task", None)
    return getattr(owner, "_background", 0) > 0 or (isinstance(task, Task) and task.running)


def working(owner):
    """True while OWNER has work running that a human started (background(work=True), a Task's job that is work):
    what the window's busy bar shows and closing the window asks about. Reads and polls never count."""
    task = getattr(owner, "task", None)
    return getattr(owner, "_work", 0) > 0 or (isinstance(task, Task) and task.running and task.work)


class Activity(QObject):
    """changed: a page's background() call or Task began or ended (the window shows what runs, with a busy bar)."""
    changed = Signal()


_activity = []


def activity():
    if not _activity:
        _activity.append(Activity())
    return _activity[0]


ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")


def output_text(text):
    """What a command wrote, for a plain-text output area: terminal escapes (colours, cursor moves) left out, a
    carriage return (a progress line drawn again) as a new line, other control characters dropped."""
    text = ANSI.sub("", str(text)).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127 and not 0x80 <= ord(c) < 0xa0))


def outcome(result, error):
    """(exit status, stdout, stderr) of a background SandboxAI.call: an exception counts as a failure."""
    return result if error is None else (1, "", str(error))


class Task(QWidget):
    """Inline progress of a page's background job: a busy bar, what runs, Cancel, and (OUTPUT True: an output area of
    its own, kept after the end until the next job; or the page's own QPlainTextEdit) what the job's commands write as
    they come (feed(), call_watched()). While it runs, the buttons that started it are disabled and no second job
    starts (run/thread/call_watched return False); results go to DONE(status, out, err) or DONE(result, error), on
    the GUI thread."""
    changed = Signal(bool)
    _fed = Signal(str)  # from the worker thread: queued to the GUI thread
    OUTPUT_LINES = 2000  # lines the output area keeps (the oldest go)

    def __init__(self, parent=None, output=False):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.row = QWidget()
        layout = QHBoxLayout(self.row)
        layout.setContentsMargins(0, 0, 0, 0)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setMaximumWidth(140)
        self.bar.setMaximumHeight(12)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName(tr("Running"))
        self.label = plain_label(role="secondary")
        self.cancel_button = button(tr("Cancel"), tr("Stop what runs now"), self.cancel)
        layout.addWidget(self.bar)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.cancel_button)
        outer.addWidget(self.row)
        self.output, self.own_output = (output, False) if isinstance(output, QPlainTextEdit) else (None, True)
        if output is True:
            self.output = QPlainTextEdit()
            self.output.setReadOnly(True)
            self.output.setFont(MONO)
            self.output.setMaximumBlockCount(self.OUTPUT_LINES)
            self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            self.output.setMaximumHeight(160)
            self.output.setAccessibleName(tr("Output"))
            self.output.hide()
            outer.addWidget(self.output)
        self._fed.connect(self._append)
        self.command, self.call, self.source, self.done = None, None, [], None
        self.work = True  # the job that runs is work (an install, a copy), not a read: see working()
        self.cancel_event = threading.Event()
        self.cancelled = False
        self.hide()

    @property
    def running(self):
        return self.command is not None or self.call is not None

    def _begin(self, text, source, cancellable):
        self.label.setText(text)
        self.cancel_button.setVisible(cancellable)
        self.cancel_button.setEnabled(True)
        self.cancelled = False
        self.source = [w for w in (source if isinstance(source, (list, tuple)) else [source]) if w is not None]
        for widget in self.source:
            widget.setEnabled(False)
        if self.output is not None and self.own_output:
            self.output.clear()
            self.output.hide()
        self.row.show()
        self.show()
        self.changed.emit(True)
        activity().changed.emit()

    def _end(self):
        self.command, self.call = None, None
        for widget in self.source:
            try:
                widget.setEnabled(True)
            except RuntimeError:
                pass
        self.source = []
        self.row.hide()
        if not self.own_output or self.output is None or not self.output.toPlainText():
            self.hide()  # (what it wrote in its own area stays readable until the next job)
        self.changed.emit(False)
        activity().changed.emit()

    def feed(self, text):
        """Output of the job (any thread): shown in the output area, the last line beside the busy bar."""
        try:
            self._fed.emit(str(text))
        except RuntimeError:  # the page is gone (the window closed)
            pass

    @Slot(str)
    def _append(self, text):
        text = output_text(text)
        if not text or self.output is None:
            return
        if self.own_output:
            self.output.show()
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def run(self, argv, done, text, source=None, timeout=120, input=None, work=True):
        """ARGV as an AsyncCommand; WORK False: a read (a history, a check), which the window's busy bar and its
        question when it is closed leave out."""
        if self.running:
            return False
        self.work = work
        self.command = AsyncCommand(timeout, self)
        self.done = done
        self.command.finished.connect(self._command_done)
        self._begin(text, source, True)
        self.command.start(argv, input=input)
        return True

    def _command_done(self, status, out, err):
        done, cancelled = self.done, self.command is not None and self.command.cancelled
        self._end()
        if done:
            done(-2 if cancelled else status, out, err)

    def thread(self, function, done, text, source=None, cancellable=False, work=True):
        """FUNCTION(cancelled) in a thread; Cancel sets the event it gets (copies stop at the next chunk)."""
        if self.running:
            return False
        self.work = work
        self.cancel_event = threading.Event()
        self.call = ThreadCall(function, self.cancel_event.is_set, work=work)
        self.done = done
        self.call.finished.connect(self._thread_done)
        self._begin(text, source, cancellable)
        self.call.start()
        return True

    def call_watched(self, sai, function, done, text, source=None, cancellable=True, errors_only=False):
        """FUNCTION() in a thread, its sandbox-ai commands watched (SandboxAI.watching): what they write (ERRORS_ONLY:
        stderr, beside a --json stdout) comes into the output area as it comes, and Cancel ends the command that runs
        (with everything it started), so FUNCTION gets status backend.CANCELLED from it. DONE(result, error);
        self.cancelled tells a cancelled job."""
        def work(cancelled):
            with sai.watching(output=self.feed, cancelled=cancelled, errors_only=errors_only):
                return function()
        return self.thread(work, done, text, source, cancellable)

    def _thread_done(self, result, error):
        done = self.done
        self._end()
        if done:
            done(result, error)

    def cancel(self):
        if self.command is not None:
            self.cancelled = True
            self.command.stop()
        elif self.call is not None:
            self.cancelled = True
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)  # once: the job ends at its next look
            self.label.setText(tr("Cancelling…"))


# -- tables --

class _Row:
    """One row of a RowModel: the values as given (formatted only when Qt shows them) and what fill() gave with it."""
    __slots__ = ("values", "color", "tip", "icon", "sort", "index")

    def __init__(self, values, color, tip, icon, sort, index):
        self.values, self.color, self.tip, self.icon, self.sort, self.index = values, color, tip, icon, sort, index


class RowModel(QAbstractTableModel):
    """The rows of a ModelTable as Python lists: Qt asks only for the cells it paints, so 20,000 rows cost one model
    reset instead of 140,000 QTableWidgetItems made on the GUI thread (seconds). The same roles as fill() gives
    a QTableWidget's items: the text (cut at CELL_LIMIT), the tooltip, the colour, the dot, UserRole (the row's
    index in row_keys) and SORT_ROLE."""

    def __init__(self, headers, numeric=(), stretch=None, parent=None):
        super().__init__(parent)
        self.headers, self.numeric, self.stretch, self.rows = list(headers), set(numeric), stretch, []
        self.header_tips = {}  # section: the column title's tooltip (help_view.explain_columns sets them)
        self.sorted_by = (-1, Qt.SortOrder.AscendingOrder)

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(self.headers):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.header_tips.get(section)
        return None

    def setHeaderData(self, section, orientation, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        """The column titles' tooltips (help_view.explain_columns: a term explains itself on hover)."""
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(self.headers) or \
                role != Qt.ItemDataRole.ToolTipRole:
            return False
        self.header_tips[section] = value
        self.headerDataChanged.emit(orientation, section, section)
        return True

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def text(self, row, column):
        value = row.values[column] if column < len(row.values) else None
        if column in self.numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
            return number(value)
        return "" if value is None else str(value)

    def sort_value(self, row, column):
        value = row.values[column] if column < len(row.values) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if row.sort and column < len(row.sort) and row.sort[column] is not None:
            return row.sort[column]
        return self.text(row, column).lower()

    def display_text(self, row, column):
        """What row ROW shows in COLUMN (data's DisplayRole, without an index: tables.fit_columns measures with it)."""
        return elide(self.text(self.rows[row], column)) if 0 <= row < len(self.rows) else None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row, column = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return elide(self.text(row, column))
        if role == Qt.ItemDataRole.ToolTipRole:
            text = self.text(row, column)
            if len(text) > CELL_LIMIT or column == self.stretch or row.tip:
                return safe_tooltip(row.tip if row.tip and column == 0 else text)
            return None
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(theme.color(row.color)) if row.color else None
        if role == Qt.ItemDataRole.DecorationRole:
            return theme.dot(row.icon) if column == 0 and row.icon else None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) if column in self.numeric else None
        if role == Qt.ItemDataRole.UserRole:
            return row.index if column == 0 else None
        if role == SORT_ROLE:
            return self.sort_value(row, column)
        return None

    @staticmethod
    def make_rows(rows, colors=None, tooltips=None, icons=None, sort=None, first=0):
        return [_Row(values, colors[r] if colors else None, tooltips[r] if tooltips else None,
                     icons[r] if icons else None, sort[r] if sort else None, first + r) for r, values in enumerate(rows)]

    def set_rows(self, rows):
        """All rows new (one reset: the selection is gone, fill() selects again by keys), in the order sorted by."""
        self.beginResetModel()
        self.rows = self.ordered(rows, *self.sorted_by)
        self.endResetModel()

    def ordered(self, rows, column, order):
        if column < 0 or column >= len(self.headers):
            return sorted(rows, key=lambda row: row.index)  # the order given to fill()

        def key(row):
            value = self.sort_value(row, column)
            return (0, value, "") if isinstance(value, (int, float)) else (1, 0, str(value))
        return sorted(rows, key=key, reverse=order == Qt.SortOrder.DescendingOrder)

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        """Sorted in Python (a header click, sortByColumn): the selection and the current row stay on their rows."""
        self.sorted_by = (column, order)
        self.layoutAboutToBeChanged.emit()
        before = self.rows
        self.rows = self.ordered(before, column, order)
        self._move_persistent(before)
        self.layoutChanged.emit()

    def _move_persistent(self, before):
        where = {id(row): position for position, row in enumerate(self.rows)}
        old = self.persistentIndexList()
        new = []
        for index in old:
            position = where.get(id(before[index.row()])) if 0 <= index.row() < len(before) else None
            new.append(self.index(position, index.column()) if position is not None else QModelIndex())
        self.changePersistentIndexList(old, new)

    def remove_where(self, gone):
        """Remove the rows for which GONE(row) is true, block by block (the others keep selection and place)."""
        positions = [position for position, row in enumerate(self.rows) if gone(row)]
        while positions:
            last = positions.pop()
            first = last
            while positions and positions[-1] == first - 1:
                first = positions.pop()
            self.beginRemoveRows(QModelIndex(), first, last)
            del self.rows[first:last + 1]
            self.endRemoveRows()

    def append(self, rows):
        """ROWS at the end (then in the order sorted by, if a column was chosen)."""
        if not rows:
            return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()
        if self.sorted_by[0] >= 0:
            self.sort(*self.sorted_by)


class _Cell:
    """What QTableWidget.item() gives, for a ModelTable's cell: its text and roles (read-only)."""

    def __init__(self, model, row, column):
        self.model, self.index = model, model.index(row, column)

    def text(self):
        return self.model.data(self.index) or ""

    def data(self, role):
        return self.model.data(self.index, role)

    def toolTip(self):  # noqa: N802
        return self.model.data(self.index, Qt.ItemDataRole.ToolTipRole) or ""

    def foreground(self):
        color = self.model.data(self.index, Qt.ItemDataRole.ForegroundRole)
        return QBrush(color) if color is not None else QBrush()


class ModelTable(QTableView):
    """A read-only table for many rows (a folder's 20,000 entries, 5,000 log records): what table() makes, on a
    RowModel. fill(), append_rows(), row_key(), selected_rows(), select_keys() and find_row() work on it as on a
    QTableWidget, and the parts of QTableWidget's interface the pages use (rowCount, item, currentRow,
    setCurrentCell, cellDoubleClicked, itemSelectionChanged) are here too."""
    cellDoubleClicked = Signal(int, int)
    itemSelectionChanged = Signal()
    model_class = RowModel  # (a subclass may keep rows of its own in place: the Files page's "..")

    def __init__(self, headers, numeric=(), stretch=None):
        super().__init__()
        self.rows_model = self.model_class(headers, numeric, stretch, self)
        self.setModel(self.rows_model)
        self.doubleClicked.connect(lambda index: self.cellDoubleClicked.emit(index.row(), index.column()))
        self.selectionModel().selectionChanged.connect(lambda *_: self.itemSelectionChanged.emit())
        # Column widths from the first rows only (resizeColumnToContents asks the model for every row it samples).
        self.verticalHeader().setResizeContentsPrecision(SAMPLED_ROWS)
        self.horizontalHeader().setResizeContentsPrecision(SAMPLED_ROWS)

    def rowCount(self):  # noqa: N802
        return self.rows_model.rowCount()

    def columnCount(self):  # noqa: N802
        return self.rows_model.columnCount()

    def item(self, row, column):
        if 0 <= row < self.rowCount() and 0 <= column < self.columnCount():
            return _Cell(self.rows_model, row, column)
        return None

    def currentRow(self):  # noqa: N802
        index = self.currentIndex()
        return index.row() if index.isValid() else -1

    def setCurrentCell(self, row, column):  # noqa: N802
        self.setCurrentIndex(self.rows_model.index(row, column))

    def setSortingEnabled(self, enabled):  # noqa: N802
        """As QTableView's, which sorts again at every call with True: only when it is switched on."""
        if enabled != self.isSortingEnabled():
            super().setSortingEnabled(enabled)


class SortItem(QTableWidgetItem):
    """A cell that sorts by its SORT_ROLE value (numbers as numbers, times as times)."""

    def __lt__(self, other):
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is not None and theirs is not None:
            try:
                return mine < theirs
            except TypeError:
                pass
        return super().__lt__(other)


def table(headers, multi=False, stretch=None, numeric=(), sortable=True, compact=(), many=False, view=None):
    """A read-only table; STRETCH is the column that takes the free width, NUMERIC columns are right-aligned;
    NUMERIC and COMPACT columns are as wide as their content (tables.py fits the columns at every fill()).
    A SORTABLE table keeps the order fill() was given until the user clicks a column header; rows may then
    be in any order, so pages find what a row stands for with row_key(), never from its position. MANY: for
    thousands of rows (a ModelTable: filled in one step, whatever the number; VIEW: a subclass of it to make)."""
    if many:
        widget = (view or ModelTable)(headers, numeric, stretch)
    else:
        widget = QTableWidget(0, len(headers))
        widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if multi
                            else QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.verticalHeader().setVisible(False)
    widget.setWordWrap(False)
    widget.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    header = widget.horizontalHeader()
    tables.setup(widget, stretch, set(compact) | set(numeric))
    widget.numeric = set(numeric)
    widget.setSortingEnabled(False)
    header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)  # no column yet: the order given to fill()
    widget.sortable = sortable
    widget.row_keys = []
    widget.setAlternatingRowColors(True)
    widget.verticalHeader().setDefaultSectionSize(28)
    return widget


def fill(widget, rows, colors=None, tooltips=None, icons=None, sort=None, keys=None):
    """Fill WIDGET with ROWS. COLORS per row: None or a theme colour name ("ok", "warn", "bad", "muted");
    ICONS per row: a theme status for a dot in the first column (stays visible when the row is selected);
    SORT per row: values to sort by, column by column (None: the cell). Long texts are cut, the full text is
    in the tooltip.

    KEYS per row: what the row stands for (any hashable value; default: the text of its first column). The
    row's index in ROWS is kept in Qt.UserRole of its first cell and row_key() gives its key, wherever
    sorting moved the row. The selection follows the keys: a row selected before is selected again, and a
    selected row whose key is gone leaves nothing selected (never the row that took its place)."""
    keys = list(keys) if keys is not None else ["" if not row or row[0] is None else str(row[0]) for row in rows]
    if len(keys) != len(rows):
        raise ValueError("fill: one key per row")
    if isinstance(widget, ModelTable):
        previous = selected_keys(widget)
        widget.row_keys = keys
        widget.rows_model.set_rows(RowModel.make_rows(rows, colors, tooltips, icons, sort))
        if getattr(widget, "sortable", False):
            widget.setSortingEnabled(True)
        if previous:
            select_keys(widget, previous)
        fit_columns(widget)
        return
    previous = selected_keys(widget)
    widget.clearSelection()
    sorting = widget.isSortingEnabled()
    widget.setSortingEnabled(False)
    widget.row_keys = keys
    widget.setRowCount(len(rows))
    for r, row in enumerate(rows):
        _set_row(widget, r, r, row, colors[r] if colors else None, tooltips[r] if tooltips else None,
                 icons[r] if icons else None, sort[r] if sort else None)
    if getattr(widget, "sortable", False) or sorting:
        widget.setSortingEnabled(True)
    if previous:
        select_keys(widget, previous)
    fit_columns(widget)


def _set_row(widget, r, index, row, color=None, tip=None, icon=None, sort=None):
    """The cells of the visual row R from ROW; INDEX: its index in widget.row_keys."""
    numeric = getattr(widget, "numeric", set())
    for c, value in enumerate(row):
        raw = value
        if c in numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
            text = number(value)
        else:
            text = "" if value is None else str(value)
        item = SortItem(elide(text))
        item.setData(SORT_ROLE, raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else
                     (sort[c] if sort and c < len(sort) and sort[c] is not None else text.lower()))
        if c in numeric:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if len(text) > CELL_LIMIT or c == getattr(widget, "stretch", None) or tip:
            item.setToolTip(safe_tooltip(tip if tip and c == 0 else text))
        if color:
            item.setForeground(QColor(theme.color(color)))
        if c == 0 and icon:
            item.setIcon(theme.dot(icon))
        if c == 0:
            item.setData(Qt.ItemDataRole.UserRole, index)
        widget.setItem(r, c, item)


def append_rows(widget, rows, colors=None, keys=(), drop=None):
    """Add ROWS (with their KEYS) to a table fill() filled, at its end, without touching the rows it has; the
    rows whose key DROP(key) says are gone are removed first. The selection stays on its rows. A table
    that has collected many more keys than rows (dropped ones) is better filled again: False then, and
    nothing is done."""
    keys = list(keys)
    if len(keys) != len(rows):
        raise ValueError("append_rows: one key per row")
    known = getattr(widget, "row_keys", None)
    if known is None or len(known) + len(keys) > 2 * (widget.rowCount() + len(keys)) + 1000:
        return False
    if isinstance(widget, ModelTable):
        model = widget.rows_model
        if drop is not None:
            model.remove_where(lambda row: drop(known[row.index]))
        model.append(RowModel.make_rows(rows, colors, first=len(known)))
        known.extend(keys)
        return True
    sorting = widget.isSortingEnabled()
    widget.setSortingEnabled(False)
    if drop is not None:
        gone = [row for row in range(widget.rowCount()) if drop(row_key(widget, row))]
        for row in reversed(gone):
            widget.removeRow(row)
    first = widget.rowCount()
    widget.setRowCount(first + len(rows))
    for offset, row in enumerate(rows):
        _set_row(widget, first + offset, len(known) + offset, row, colors[offset] if colors else None)
    known.extend(keys)
    if getattr(widget, "sortable", False) or sorting:
        widget.setSortingEnabled(True)
    return True


def fit_columns(widget, force=False):
    """Columns as wide as their titles and content, the stretch column takes the rest (tables.fit_columns): in full
    when the first rows arrive, later only wider. Columns the human sized (state.State.table) keep their widths."""
    return tables.fit_columns(widget, force=force)


def row_texts(widget, row):
    return [widget.item(row, c).text() if widget.item(row, c) else "" for c in range(widget.columnCount())]


def selected(widget, column=0):
    """The text of COLUMN in the selected row (read from the row itself, so sorting does not matter)."""
    rows = widget.selectionModel().selectedRows() if widget.selectionModel() else []
    if not rows:
        return None
    item = widget.item(rows[0].row(), column)
    return item.text() if item else None


def selected_rows(widget):
    """The visual rows selected, top to bottom: use row_key() to know what they stand for."""
    return sorted(index.row() for index in widget.selectionModel().selectedRows()) if widget.selectionModel() else []


def row_data(widget, row, column=0):
    item = widget.item(row, column)
    return item.data(Qt.ItemDataRole.UserRole) if item else None


def row_index(widget, row):
    """The index in the ROWS given to fill() of the visual ROW, or None."""
    if isinstance(widget, ModelTable):
        rows = widget.rows_model.rows
        index = rows[row].index if 0 <= row < len(rows) else None
    else:
        index = row_data(widget, row)
    return index if isinstance(index, int) and 0 <= index < len(getattr(widget, "row_keys", [])) else None


def row_key(widget, row):
    """The key fill() was given for the visual ROW (wherever sorting moved it), or None."""
    index = row_index(widget, row)
    return None if index is None else widget.row_keys[index]


def selected_keys(widget):
    """The keys of the selected rows, top to bottom."""
    keys = (row_key(widget, row) for row in selected_rows(widget))
    return [key for key in keys if key is not None]


def find_row(widget, key):
    """The visual row whose key is KEY, or -1."""
    if isinstance(widget, ModelTable):
        return next((row for row, entry in enumerate(widget.rows_model.rows) if widget.row_keys[entry.index] == key), -1)
    return next((row for row in range(widget.rowCount()) if row_key(widget, row) == key), -1)


def select_keys(widget, keys):
    """Select the rows whose key is in KEYS (only the first of them in a single-selection table)."""
    model, wanted = widget.selectionModel(), list(keys)
    if model is None:
        return
    multi = widget.selectionMode() != QAbstractItemView.SelectionMode.SingleSelection
    flags = QItemSelectionModel.SelectionFlag
    if isinstance(widget, ModelTable):  # one selection of row ranges (selecting 20,000 rows one by one takes long)
        wanted, keys_of, last = set(wanted), widget.row_keys, widget.columnCount() - 1
        rows = [row for row, entry in enumerate(widget.rows_model.rows) if keys_of[entry.index] in wanted]
        rows = rows[:1] if not multi else rows
        if not rows:
            return
        selection, start = QItemSelection(), None
        for position, row in enumerate(rows):
            start = row if start is None else start
            if position + 1 == len(rows) or rows[position + 1] != row + 1:
                selection.select(widget.model().index(start, 0), widget.model().index(row, last))
                start = None
        model.select(selection, flags.ClearAndSelect | flags.Rows)
        return
    first = True
    for row in range(widget.rowCount()):
        if row_key(widget, row) in wanted:
            model.select(widget.model().index(row, 0),
                         (flags.ClearAndSelect if first else flags.Select) | flags.Rows)
            first = False
            if not multi:
                break


class BarDelegate(QStyledItemDelegate):
    """A bar in a table cell, from the fraction (0..1) in BAR_ROLE: drawn by the view, so it scrolls and
    selects with its row."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        fraction = index.data(BAR_ROLE)
        if not isinstance(fraction, (int, float)):
            return
        rect = option.rect.adjusted(6, option.rect.height() // 2 - 4, -6, -(option.rect.height() // 2 - 4))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QColor(option.palette.color(QPalette.ColorRole.Mid))
        track.setAlpha(70)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, 3, 3)
        filled = QRect(rect)
        filled.setWidth(max(2, int(rect.width() * max(0.0, min(1.0, fraction)))))
        painter.setBrush(option.palette.color(QPalette.ColorRole.Highlight))
        painter.drawRoundedRect(filled, 3, 3)
        painter.restore()

    def sizeHint(self, option, index):  # noqa: N802
        hint = super().sizeHint(option, index)
        return QSize(max(hint.width(), 120), hint.height())


def bars(widget, column, values):
    """Bars in COLUMN of WIDGET, relative to the largest value (painted by BarDelegate). VALUES are in the
    order of the rows given to fill(), which sorting may have changed on the screen."""
    if not isinstance(widget.itemDelegateForColumn(column), BarDelegate):
        widget.setItemDelegateForColumn(column, BarDelegate(widget))
    largest = max([v for v in values if isinstance(v, (int, float))] or [1]) or 1
    sorting = widget.isSortingEnabled()
    widget.setSortingEnabled(False)  # no row moves while the bars are set
    for row in range(widget.rowCount()):
        index = row_index(widget, row)
        value = values[index] if index is not None and index < len(values) else 0
        item = widget.item(row, column)
        if item is None:
            item = SortItem("")
            widget.setItem(row, column, item)
        item.setText("")
        item.setData(BAR_ROLE, (value or 0) / largest)
        item.setData(SORT_ROLE, value or 0)
    widget.setSortingEnabled(sorting)


class NavDelegate(QStyledItemDelegate):
    """Navigation entries: icon, text and a count as a pill that stays readable when selected."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        count = index.data(BADGE_ROLE)
        if not count:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = str(count) if count < 100 else "99+"
        font = QFont(option.font)
        font.setBold(True)
        font.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + 12
        height = painter.fontMetrics().height() + 2
        rect = QRect(option.rect.right() - width - 8, option.rect.center().y() - height // 2, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d93025"))
        painter.drawRoundedRect(rect, height / 2, height / 2)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index):  # noqa: N802
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + 40, max(38, hint.height()))


# -- layout --

class FlowLayout(QLayout):
    """Lays out widgets in rows that wrap when the window is narrow (button rows, toolbars)."""

    def __init__(self, parent=None, spacing=6):
        self.items, self.heights, self.minimum = [], {}, None
        super().__init__(parent)
        self.gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def invalidate(self):
        """(Qt says so when an item changed - shown, hidden, another size hint: what was measured is measured again;
        Qt asks a page's layouts for their heights dozens of times when the page is shown.)"""
        self.heights, self.minimum = {}, None
        super().invalidate()

    def addItem(self, item):  # noqa: N802
        self.items.append(item)
        self.heights, self.minimum = {}, None

    def addWidget(self, widget):  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def addStretch(self):  # noqa: N802 - rows wrap; a stretch has no meaning here
        pass

    def count(self):
        return len(self.items)

    def itemAt(self, index):  # noqa: N802
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):  # noqa: N802
        self.heights, self.minimum = {}, None
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        height = self.heights.get(width)
        if height is None:
            if len(self.heights) > 16:
                self.heights.clear()
            height = self.heights[width] = self._layout(QRect(0, 0, width, 0), apply=False)
        return height

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        if self.minimum is None:
            size = QSize()
            for item in self.items:
                if item.widget() is None or not item.widget().isHidden():
                    size = size.expandedTo(item.minimumSize())
            self.minimum = size + QSize(2, 2)
        return QSize(self.minimum)

    def _layout(self, rect, apply):
        x, y, line = rect.x(), rect.y(), 0
        for item in self.items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self.gap, 0
            if apply:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x += hint.width() + self.gap
            line = max(line, hint.height())
        return y + line - rect.y()


def compact_combo(combo, chars=24):
    """COMBO no wider than about CHARS characters, whatever its longest item (Qt sizes a combo to its longest item
    otherwise, and a long one makes the whole window wider than the screen): the popup still shows every item in
    full, and each item's full text is its tooltip."""
    from PySide6.QtWidgets import QComboBox
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    for index in range(combo.count()):
        if not combo.itemData(index, Qt.ItemDataRole.ToolTipRole):
            combo.setItemData(index, safe_tooltip(combo.itemText(index)), Qt.ItemDataRole.ToolTipRole)
    return combo


def tame_combos(root, chars=28):
    """Every combo box below ROOT whose longest item is longer than CHARS characters: compact_combo."""
    from PySide6.QtWidgets import QComboBox
    for combo in root.findChildren(QComboBox):
        if combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon:
            continue
        longest = max((len(combo.itemText(i)) for i in range(combo.count())), default=0)
        if longest > chars:
            compact_combo(combo, chars)


def name_views(root):
    """Every table, tree or list below ROOT without an accessible name gets the title of the group box it sits in
    (a screen reader says "Plugins, table" instead of only "table"); the pages name their own views, this is the
    fallback for a view added without one."""
    from PySide6.QtWidgets import QAbstractItemView, QComboBox, QGroupBox, QHeaderView
    for view in root.findChildren(QAbstractItemView):
        if view.accessibleName() or isinstance(view, QHeaderView):
            continue
        parent = view.parentWidget()
        while parent is not None and parent is not root and not isinstance(parent, (QGroupBox, QComboBox)):
            parent = parent.parentWidget()
        heading = QGroupBox.title(parent) if isinstance(parent, QGroupBox) else ""  # (a box may shadow title())
        if heading:
            view.setAccessibleName(heading.replace("&", ""))


def flow(*widgets):
    holder = QWidget()
    layout = FlowLayout(holder)
    for widget in widgets:
        if widget is not None:
            layout.addWidget(widget)
    holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return holder


def card():
    frame = QFrame()
    frame.setProperty("role", "card")
    return frame


def standard_icon(name):
    return QApplication.style().standardIcon(getattr(QStyle.StandardPixmap, name))


class Busy:
    """Wait cursor around a short call (no subprocess, no network: those run in the background)."""

    def __enter__(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def __exit__(self, *exc):
        QApplication.restoreOverrideCursor()



START_WHERE = (("app", i18n.N_("in the app")), ("window", i18n.N_("in a terminal window")))


class StartControls(QWidget):
    """One Start for the Board and the Start page: where (a terminal tab of the app or a terminal window) and whether
    it starts as a session - kept running in tmux when its terminal or the app closes (or the app crashes), attached
    again from Sandboxes & sessions or Terminals - named by a codename unless a name of its own is typed. Before,
    three buttons (Start in the app, Start in a terminal window, Start as session) said the same less clearly.
    START(embedded, session) is called with True (the app) or False (a window) and the session's name or None."""

    def __init__(self, start, sai=None, settings=None, parent=None):
        super().__init__(parent)
        self.start_function, self.sai = start, sai
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.where = QComboBox()
        self.where.setAccessibleName(tr("Where it starts"))
        for key, title in START_WHERE:
            self.where.addItem(tr(title), key)
        chosen = (settings or {}).get("terminal", "app") if isinstance(settings, dict) else "app"
        self.where.setCurrentIndex(max(0, self.where.findData("window" if chosen == "window" else "app")))
        self.where.setToolTip(safe_tooltip(tr("A terminal tab of this window (Terminals), or your terminal program")))
        self.as_session = QCheckBox(tr("as session"))
        self.as_session.setChecked(True)
        self.as_session.setToolTip(safe_tooltip(tr(
            "Keeps running when its terminal or the app closes; attach it again from Sandboxes & sessions or "
            "Terminals. Off: it ends with its terminal.")))
        self.name = QLineEdit()
        self.name.setAccessibleName(tr("Session name"))
        self.name.setMaximumWidth(220)
        self.suggestion = ""
        self.as_session.toggled.connect(self.name.setEnabled)
        self.button = QPushButton(tr("Start"))
        self.button.setProperty("role", "primary")  # the main action of its page
        self.button.clicked.connect(lambda _=False: self.start())
        for widget in (self.button, self.where, self.as_session, self.name):
            layout.addWidget(widget)
        self.new_name()

    def new_name(self):
        """A codename for the next session (as the launcher names starts), shown in the empty name field."""
        try:
            name = self.sai.module("codenames").codename() if self.sai is not None else ""
        except Exception:  # noqa: BLE001 - an older sandbox-ai without codenames: a name must be typed
            name = ""
        self.suggestion = name
        self.name.setPlaceholderText(name or tr("session name"))
        self.name.setToolTip(safe_tooltip(tr("Empty: the session is called {name}", name=name) if name else
                                          tr("The name of the session")))

    def start(self):
        embedded = self.where.currentData() != "window"
        session = None
        if self.as_session.isChecked():
            session = self.name.text().strip() or self.suggestion
            if not session:
                inform(self, tr("Session"), tr("Give the session a name first."))
                return False
        started = self.start_function(embedded, session)
        if started and session and not self.name.text().strip():
            self.new_name()  # the next one gets another
        return started
