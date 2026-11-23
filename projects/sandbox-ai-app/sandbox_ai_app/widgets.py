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
import weakref

from PySide6.QtCore import (
    QAbstractItemModel, QAbstractTableModel, QEvent, QItemSelection, QItemSelectionModel, QModelIndex, QObject, QPoint,
    QProcess, QRect, QSize, Qt, QTimer, Signal, Slot,
)
from PySide6 import QtGui
from PySide6.QtGui import (
    QAction, QBrush, QColor, QFont, QFontMetrics, QPainter, QPalette, QTextDocument, QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QSpacerItem, QStyle, QStyledItemDelegate, QTabBar,
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


def ago(seconds):
    """How long ago SECONDS were, in the largest unit that fits: "55 s ago", "3 min ago", "2 h ago", "4 days ago"
    (German "vor 55 Sek.", "vor 3 Min.", "vor 2 Std.", "vor 4 Tagen"): every relative time of the app."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        return "?"
    if seconds < 60:
        return tr("{seconds} s ago", seconds=seconds)
    if seconds < 3600:
        return tr("{minutes} min ago", minutes=seconds // 60)
    if seconds < 86400:
        return tr("{hours} h ago", hours=seconds // 3600)
    days = seconds // 86400
    return tr("1 day ago") if days == 1 else tr("{days} days ago", days=days)


def local_time(text, seconds=False):
    """A time as sandbox-ai writes it (UTC, 2026-11-03T10:00:00Z) in local time, or the text itself."""
    from .backend import parse_time
    moment = parse_time(text)
    if moment is None:
        return str(text or "")
    pattern = "%d.%m.%Y %H:%M" if i18n.language() == "de" else "%Y-%m-%d %H:%M"
    return time.strftime(pattern + (":%S" if seconds else ""), time.localtime(moment))


class LoggedLabel(QLabel):
    """A page's status line whose every new text goes into the log (once per change): at INFO - DEBUG for progress
    (a text ending in "…") or a QUIET one, WARNING while it is coloured as an error - from LOG_WHERE (the page).
    What the human read there ("Select files first.", "Cannot open …: Permission denied") can be followed in the
    log.
    FITS: as high as its text wraps to at its width, also at the window's smallest size (a word-wrapped label's own
    minimum is one line: the LLM traffic page's two-line "No LLM traffic recorded yet: ..." was cut to one, CI's
    fonts 2026-11-14)."""
    log_where, quiet, fits = "app", False, False

    def setText(self, text):  # noqa: N802
        text = str(text)
        if text and text != self.text() and not getattr(self, "_noticed", False):
            role = self.property("role") or ""
            # ("Reading…", "Asking box…": progress at DEBUG - it alternates with the outcome at every refresh)
            level = "WARNING" if role == "notice-error" else "DEBUG" if self.quiet or \
                text.rstrip().endswith(("…", "...")) else "INFO"
            applog.log(level, self.log_where, f"status: {said(text)}")
        super().setText(text)
        self.fit()

    def fit(self):
        if not self.fits or not self.isVisible() or self.width() < 50:
            return  # (laid out yet: a hidden label's width is not its page's, its height for it far too high)
        need = self.heightForWidth(self.width()) if self.text() and self.wordWrap() else 0
        if need >= 0 and need != self.minimumHeight():
            self.setMinimumHeight(need)

    def resizeEvent(self, event):  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self.fit()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.fit()


def plain_label(text="", word_wrap=True, role=None, selectable=False, log=None, fit=False):
    """A label that never interprets its text as HTML (texts from logs and questions come from the agent).
    SELECTABLE: a message, a status or a refusal - its text can be marked and copied (Ctrl+C, its context menu).
    LOG: the page's name in the log - each new text is logged (LoggedLabel). FIT (with LOG): always as high as its
    wrapped text (LoggedLabel.fits) - a status line below a page's part that takes all the room."""
    label = LoggedLabel() if log else QLabel()
    if log:
        label.log_where = str(log)
        label.fits = bool(fit)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(word_wrap)
    label.setText(str(text))
    if role:
        label.setProperty("role", role)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                      | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    return label


# The setting explanations ("Show explanations", off by default): the purely explanatory texts of the pages and
# dialogs - a page's intro line, a hint below a form or a table, a "how this works" paragraph - are shown only while
# it is on (user 2026-11-18: "such info texts we don't need in the app"). Never one of them: a warning, an error, a
# refusal, a status or result line, an empty list's line, a note that a choice is dangerous. The (?) buttons and the
# tooltips stay: they are the way to the explanations while the setting is off.
_EXPLAINED = [False]
_EXPLANATIONS = weakref.WeakSet()  # every Explanation made, shown or hidden when the setting changes
_EXPLANATION_LISTENERS = []  # weakref.WeakMethod of callbacks (an explanation that is no label: a wizard's subtitle)


def explanations_shown():
    return _EXPLAINED[0]


def set_explanations_shown(on):
    """Show or hide every explanation of the app at once, live: on every page and in every dialog open now (and in
    those made later, as they are made). A hidden one takes no room: its layout closes the space."""
    on = bool(on)
    if on == _EXPLAINED[0]:
        return
    _EXPLAINED[0] = on
    for label in list(_EXPLANATIONS):
        try:
            label.apply_shown()
        except RuntimeError:  # its C++ side is gone
            _EXPLANATIONS.discard(label)
    for ref in list(_EXPLANATION_LISTENERS):
        method = ref()
        try:
            if method is None:
                raise RuntimeError
            method(on)
        except RuntimeError:
            _EXPLANATION_LISTENERS.remove(ref)


def on_explanations_changed(method):
    """Call the bound METHOD(shown) whenever the setting explanations changes (kept weakly: a page that is gone is
    dropped) - for an explanation that is no label (a wizard page's subtitle)."""
    _EXPLANATION_LISTENERS.append(weakref.WeakMethod(method))


class Explanation(QLabel):
    """A purely explanatory text (widgets.explanation): shown only while the setting explanations is on. What the
    page asks of it (setVisible, show, hide) is kept and applied with the setting: a hint the page hides for a while
    stays hidden when the setting is switched on, and comes back when the page shows it again."""

    def __init__(self, text="", word_wrap=True, selectable=False):
        super().__init__()
        self.wanted = None  # what setVisible asked for (None: as Qt decides, with its parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(word_wrap)
        self.setText(str(text))
        self.setProperty("role", "secondary")
        self.setProperty("explanation", True)
        if selectable:
            self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                         | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        _EXPLANATIONS.add(self)
        if not _EXPLAINED[0]:
            QLabel.setVisible(self, False)

    def setVisible(self, visible):  # noqa: N802 - Qt's name; the setting explanations wins
        self.wanted = bool(visible)
        QLabel.setVisible(self, self.wanted and _EXPLAINED[0])
        self.follow_row()

    def apply_shown(self):
        if not _EXPLAINED[0]:
            QLabel.setVisible(self, False)
        elif self.wanted is not False:
            if self.parentWidget() is None:  # not placed yet: shown with its parent later, never as a window
                self.setAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide, False)
            else:
                QLabel.setVisible(self, True)
        self.follow_row()

    def follow_row(self):
        """In a form, the empty label of its row (form.addRow("", explanation)) goes and comes with it: no gap."""
        parent = self.parentWidget()
        form = parent.layout() if parent is not None else None
        if not isinstance(form, QFormLayout):
            return
        buddy = form.labelForField(self)
        if isinstance(buddy, QLabel) and not buddy.text():
            buddy.setVisible(not self.isHidden())


def explanation(text="", word_wrap=True, selectable=False):
    """A purely explanatory text of a page or dialog: a page's intro line, a hint below a form or a table, a "how this
    works" paragraph. Shown only while the setting explanations ("Show explanations") is on; never for a warning, an
    error, a status line or an empty list's line (those are plain_label's, always shown). Plain text, never HTML."""
    return Explanation(text, word_wrap=word_wrap, selectable=selectable)


NOTICE_ROLES = {"ok": "notice-ok", "warn": "notice-warn", "error": "notice-error"}


NOTICE_LEVELS = {"error": "WARNING", "warn": "INFO", "ok": "INFO"}  # a status line's level in the log (else DEBUG)


def log_notice(widget, text, level=None, details=None):
    """A page's status line or notice, as it changes, into the log: an error at WARNING, a warning or a success at
    INFO, any other note at DEBUG."""
    try:
        where = where_of(widget)
    except Exception:  # noqa: BLE001
        where = "app"
    applog.log(NOTICE_LEVELS.get(level, "DEBUG"), where, f"{level or 'note'}: {said(text)}" +
               (f"\n{applog.tail(details)}" if details else ""))


def set_notice(label, text, level=None):
    """TEXT in LABEL, a page's status line, in the colour of LEVEL ("ok", "warn", "error"; None: the label's own,
    e.g. secondary): what went wrong does not look like any other note (design review 0.1.95)."""
    if label.property("plain_role") is None:
        label.setProperty("plain_role", label.property("role") or "")
    role = NOTICE_ROLES.get(level) or label.property("plain_role") or None
    if text and text != label.text():  # (once per change: a page that sets the same line at every poll says it once)
        log_notice(label, text, level)
    label._noticed = True  # (a LoggedLabel: said here already, with its level)
    try:
        label.setText(text)
    finally:
        label._noticed = False
    if label.property("role") != role:
        label.setProperty("role", role)
        label.style().unpolish(label)
        label.style().polish(label)
    return label


def show_text_window(parent, title_text, text):
    """TEXT whole in a window of its own, to mark and copy (a refusal cut short in a status line)."""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout
    dialog = QDialog(parent)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.setWindowTitle(title_text)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit(str(text))
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    layout.addWidget(view)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy = buttons.addButton(tr("Copy all"), QDialogButtonBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(str(text)))
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    dialog.resize(640, 260)
    dialog.show()
    return dialog


class ElidedLabel(QLabel):
    """A one-line label that never asks for more room than ABOUT characters: a longer text is cut with "…" where the
    room ends, whole in its tooltip (a status line under a canvas that does not scroll)."""

    def __init__(self, about=40, role=None):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.about = about
        self.elides = True  # cut on purpose (tests/test_layout.py: not a clipped text)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Ctrl+C after a click copies the whole text
        if role:
            self.setProperty("role", role)

    def sizeHint(self):  # noqa: N802
        hint = super().sizeHint()
        return QSize(min(hint.width(), self.fontMetrics().averageCharWidth() * self.about), hint.height())

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def setText(self, text, whole=None):  # noqa: N802
        """TEXT on the one line; WHOLE (default TEXT: a message of several lines, its line breaks kept) in the
        tooltip, the copy and the window of the whole text."""
        super().setText(str(text))
        self.whole = str(whole) if whole else str(text)
        self.setToolTip(safe_tooltip(self.whole) if text else "")

    def whole_text(self):
        return getattr(self, "whole", None) or self.text() if self.text() else ""

    def copy_text(self):
        QApplication.clipboard().setText(self.whole_text())

    def show_whole(self):
        if self.text():
            show_text_window(self, tr("Whole text"), self.whole_text())

    def contextMenuEvent(self, event):  # noqa: N802
        """Copy the whole text (also Ctrl+C after a click), or show it whole in a window to mark a part."""
        if not self.text():
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction(tr("Copy"), self.copy_text)
        menu.addAction(tr("Show the whole text…"), self.show_whole)
        exec_dropped(menu, event.globalPos())

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.show_whole()

    def keyPressEvent(self, event):  # noqa: N802
        from PySide6.QtGui import QKeySequence
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_text()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):  # noqa: N802
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), text)


def title(text, subtitle=None):
    """The heading of a page: a title and an optional line of explanation (an explanation: shown only while the
    setting explanations is on)."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(2)
    heading = plain_label(text, word_wrap=False, role="title")
    heading.setAccessibleName(text)
    layout.addWidget(heading)
    if subtitle:
        layout.addWidget(explanation(subtitle))
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


class MoreHeader(QToolButton):
    """The header line of rows folded away (with disclosure(): an arrow, right folded, down open): Space and also
    Enter or Return show or fold them, as on a link; reached with Tab."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)  # its own width, not the row's

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.isAutoRepeat():
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


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


def nav_button(text, tooltip, slot, glyph=None):
    """A button that goes somewhere or does one small thing beside a list (Home, Up, Refresh, New folder): an action,
    never a toggle, and it takes no focus from a click (a focused button looks pressed in some themes, as if Home
    stayed on); Tab still reaches it. GLYPH (a key of theme.GLYPHS): an icon instead of TEXT, which stays its
    accessible name (screen readers, the list's context menu: button_text) - TOOLTIP says what it does; paint_glyph
    draws it in the look's text colour, repaint_glyphs again after a change of theme. The Files page's panes and the
    remote folder chooser use it."""
    widget = button("" if glyph else text, tooltip, slot)
    widget.setCheckable(False)
    widget.setAutoDefault(False)
    widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    widget.setAccessibleName(text)
    if glyph:
        widget.glyph = glyph
        paint_glyph(widget)
    return widget


GLYPH_SIZE = 16


def paint_glyph(widget):
    """WIDGET's glyph in the text colour of the look now (again after a change of theme: readable in the dark and
    high-contrast looks), drawn sharp at twice the size too (a HiDPI screen takes that one)."""
    tint = theme.colors()["text"]
    icon = theme.glyph(widget.glyph, tint, GLYPH_SIZE)
    icon.addPixmap(theme.glyph(widget.glyph, tint, 2 * GLYPH_SIZE).pixmap(2 * GLYPH_SIZE, 2 * GLYPH_SIZE))
    widget.setIcon(icon)
    widget.setIconSize(QSize(GLYPH_SIZE, GLYPH_SIZE))


def repaint_glyphs(holder, event):
    """In HOLDER's changeEvent: after another look (EVENT a palette or style change), every nav_button's glyph in
    HOLDER drawn again in its text colour."""
    if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
        for widget in holder.findChildren(QAbstractButton):
            if getattr(widget, "glyph", None):
                paint_glyph(widget)


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
        self.details_button.setAutoDefault(False)  # (Qt made it, hidden, the default: Enter then pressed nothing)
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
        area = screen_area(self.parentWidget() or self)
        room = area.size() if area is not None else QSize(*MESSAGE_SIZE) * 2
        metrics = self.text.fontMetrics()
        widest = max((metrics.horizontalAdvance(line) for line in text.split("\n")), default=0) + 8
        # (the icon and the margins beside the text: about 80 px; a phone's screen leaves the text the rest)
        width = max(min(widest, MESSAGE_SIZE[0], room.width() - 160), min(320, room.width() - 100), 120)
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


def where_of(parent):
    """The page (or window) a message is shown on, for its log line: its class name - the page's own (SandboxesPage),
    never the scroll area the window keeps it in (every message of a page said "PageScroll")."""
    try:
        page = page_of(parent) if parent is not None else None
        shown = page or parent
        if shown is None:
            return "app"
        inner = shown.widget() if type(shown).__name__ == "PageScroll" and hasattr(shown, "widget") else None
        if inner is None and type(shown).__name__ == "PageScroll":  # (a widget of the page: the page it is on)
            inner = next((w for w in _parents(parent) if type(w).__name__.endswith(("Page", "Dialog"))), None)
        return type(inner or shown).__name__
    except Exception:  # noqa: BLE001 - a log line must never fail
        return "app"


def _parents(widget):
    while widget is not None:
        yield widget
        widget = widget.parentWidget()


def said(text, limit=1200):
    """A message's text for the log (redacted, at most LIMIT characters)."""
    text = applog.redact(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def log_message(parent, title_text, text, details=None, warning=False):
    """What a message told the human, into the log: a warning at WARNING (a refusal, a failure: "Not started"), else
    INFO; with the tail of its details."""
    applog.log("WARNING" if warning else "INFO", where_of(parent), f"{'warning' if warning else 'message'} shown: "
               f"{said(title_text, 200)}: {said(text)}" + (f"\n{applog.tail(details)}" if details else ""))


def log_answer(parent, title_text, text, answer):
    """A question and the human's answer, into the log."""
    applog.log("INFO", where_of(parent), f"asked: {said(title_text, 200)}: {said(text, 600)} -> answer: "
               f"{said(answer, 200)}")


def inform(parent, title_text, text, details=None, warning=False, modal=True):
    """A message with OK; inform() returns at once - no one waits for an OK (an event loop waiting for it would hold
    back every answer given meanwhile: nested loops return last-in, first-out). MODAL: the page PARENT is on waits
    behind a veil until OK (show_veiled); not MODAL (the outcome of a finished background job): nothing is veiled,
    the Approvals page stays usable while it is open."""
    from .files import printable_text
    # (what sandbox-ai, an agent or a host wrote is in many messages: its control characters shown escaped, whoever
    # put the text together - its line breaks and tabs kept)
    text = printable_text(str(text if text is not None else "").replace("\r\n", "\n").replace("\r", "\n"))
    log_message(parent, title_text, text, details, warning)
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
# The window's emergency stop, its QAction (set by the main window): Ctrl+Shift+X also in a dialog that blocks the
# window (emergency_shortcut).
EMERGENCY_ACTION = None
EMERGENCY_KEYS = "Ctrl+Shift+X"


def on_screen(widget):
    """True while WIDGET is shown and its window is not minimised (in the tray the window is hidden: not shown either).
    What a page only shows (a list, a log, a timeline) is read again meanwhile, every few seconds, for nobody: a
    minimised window's pages count as shown (isVisible); their timers ask this, and read again at their next tick once
    the window is back. The questions are read whatever this says."""
    return widget.isVisible() and not widget.window().isMinimized()


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


def drop_after_exec(dialog):
    """Delete DIALOG (run with exec(), its answer read) once the caller is back in the event loop, unless it is still
    shown: a dialog made anew for each use is a child of its window and otherwise stayed, closed, for as long as the
    window lived (Settings… about 4 MB each time). Anything not a widget (a test's stand-in) is left alone."""
    try:
        if isinstance(dialog, QWidget) and not dialog.isVisible():
            dialog.deleteLater()
    except RuntimeError:  # deleted already
        pass


def exec_dropped(dialog, *args):
    """DIALOG.exec(*ARGS) for a dialog, message box or menu made for this one use (a child of its window), dropped
    once the caller is back in the event loop (drop_after_exec): its answer and fields can be read until then, also
    across another question asked meanwhile (a deferred delete waits for the loop it was posted in)."""
    try:
        return dialog.exec(*args)
    finally:
        drop_after_exec(dialog)


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
        emergency_shortcut(dialog)
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
            ending = QApplication.closingDown() or bool(getattr(QWidget.window(parent), "shut_down", False)) \
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
# What a window manager's title bar takes beside the title: its buttons (close, maximise, minimise), the window's icon
# and the margins, about. Qt's message box counts 50 px and the title font of its own MDI windows: under GNOME, KDE
# or Xfce a short message's box was narrower than its title, which the title bar cut ("Rename auburn-kak…").
TITLE_ROOM = 150


def screen_area(widget):
    """The free area of the screen WIDGET is shown on (the screen of the window it belongs to), less a margin and
    the window manager's title bar; None without a screen."""
    parent = widget.parentWidget()
    screen = (parent.window().screen() if parent is not None else None) or widget.screen() or \
        QApplication.primaryScreen()
    if screen is None:
        return None
    return screen.availableGeometry().adjusted(SCREEN_MARGIN, SCREEN_MARGIN + TITLE_BAR, -SCREEN_MARGIN,
                                                -SCREEN_MARGIN)


def title_width(widget):
    """The width a window needs for its title WIDGET.windowTitle() to be read whole in a window manager's title bar:
    the title in a bold font a little larger than the window's own, and TITLE_ROOM beside it."""
    title = widget.windowTitle()
    if not title:
        return 0
    font = QFont(widget.font())
    font.setBold(True)
    if font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() * 1.1)
    elif font.pixelSize() > 0:
        font.setPixelSize(round(font.pixelSize() * 1.1))
    return QFontMetrics(font).horizontalAdvance(title) + TITLE_ROOM


def make_readable(dialog):
    """DIALOG (about to be shown) as wide as its window title needs (title_width), within its screen and its own
    maximum: a short message's box, an input dialog, were as narrow as their text, and the title bar cut the title.
    An input dialog's question wraps where it would be wider than the screen (a phone's): it was cut off instead; a
    row of buttons too wide stands one below the other. A message box is widened through a spacer across its grid (it
    sizes itself from its layout when it is shown), its text never wider than the screen."""
    area = screen_area(dialog)
    room = area.width() if area is not None and area.width() >= 200 else None
    want = max(title_width(dialog), least_width(dialog))
    if room is not None:
        want = min(want, room - 4)  # (a layout that wraps a label may add a pixel to the minimum it is given)
    from PySide6.QtWidgets import QInputDialog
    if room is not None:
        if isinstance(dialog, QInputDialog):
            for label in dialog.findChildren(QLabel):
                if not label.wordWrap() and label.sizeHint().width() > room - 40:
                    label.setWordWrap(True)
                    label.setMaximumWidth(room - 40)  # (a wrapped label's own width would still be its longest)
        # A row of buttons wider than the screen (three long German ones on a phone): one below the other.
        for row in dialog.findChildren(QDialogButtonBox):
            if row.orientation() == Qt.Orientation.Horizontal and row.sizeHint().width() > room - 40:
                row.setOrientation(Qt.Orientation.Vertical)
    if isinstance(dialog, QMessageBox):
        label = dialog.findChild(QLabel, "qt_msgbox_label")
        if label is None:
            return
        # The text never wider than the screen leaves it beside the icon and the margins (Qt sizes a box by the
        # whole screen: on a phone it reached past the edge). The box at least as wide as its title needs: a spacer
        # across its grid (a minimum on the text instead would replace what its lines need, and Qt then neither
        # wraps nor shows them whole).
        beside = max(0, dialog.sizeHint().width() - label.sizeHint().width())
        if room is not None:
            label.setMaximumWidth(max(80, room - beside))
        layout = dialog.layout()
        spacer = getattr(dialog, "_title_room", None)
        if spacer is None and isinstance(layout, QGridLayout):
            spacer = dialog._title_room = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
            layout.addItem(spacer, layout.rowCount(), 0, 1, max(1, layout.columnCount()))
        if spacer is not None:
            spacer.changeSize(max(0, want - layout.contentsMargins().left() - layout.contentsMargins().right()), 0,
                              QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
            layout.invalidate()
        return
    want = min(want, dialog.maximumWidth())
    if dialog.minimumWidth() < want:
        dialog.setMinimumWidth(want)
    if dialog.width() < want:
        dialog.resize(want, dialog.height())


def fit_height(dialog):
    """DIALOG (shown) as high as its layout needs at its width, within its screen: a dialog sized before its
    wrapping texts (a note, a warning) were laid out at its final width cut their last lines."""
    try:
        layout = dialog.layout()
        if layout is None or not dialog.isVisible() or not layout.hasHeightForWidth():
            return
        need = layout.totalHeightForWidth(dialog.width())
    except RuntimeError:  # (deleted meanwhile)
        return
    area = screen_area(dialog)
    if area is not None:
        need = min(need, area.height())
    if need > dialog.height():
        dialog.resize(dialog.width(), need)


def fit_on_screen(widget):
    """WIDGET (a dialog about to be shown) no larger than the free area of its screen (the screen of the window it
    belongs to) and inside it: a dialog sized for its content reached past the screen's edge, its buttons out of
    reach. Its minimum size gives way where it must."""
    area = screen_area(widget)
    if area is None or area.width() < 200 or area.height() < 150:
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


# -- the size and place of every dialog and extra window (one rule: dialog_rule) --

DIALOG_LEAST = 420  # px: the least width of a dialog, a question (more with a larger font: LEAST_CHARS)
LEAST_CHARS = 52  # characters of the dialog's font: the least width with a larger font
NAME_CHARS = 40  # a text field alone in its row (a name, a path) shows about this many characters
SHARED_CHARS = 16  # a text field that shares its row with others (a host and its port)
READABLE_CHARS = 80  # a long text wraps at about this many characters (Qt's own measure for a wrapping label)
BREAK_WORD = 40  # a word longer than this (a path, a URL) may break after its separators
BREAK_AFTER = "/\\._-?=:,@+"
ZERO_WIDTH_SPACE = "\u200b"
SCREEN_SHARE = 0.9  # the most of its screen's free area a dialog takes; beyond that its content scrolls
SMALL_SCREEN = 800  # px: on a smaller screen (a phone) a dialog may take all of the free area
LARGE_SHARE = 0.7  # a dialog wider or higher than this share of the screen: at its window's top left, not centred
LARGE_INSET = 24  # px: a large dialog's place inside its window's top left corner
MOVED = 24  # px: a dialog this far from where it was placed (or sized this much otherwise) was moved by the human
PLACES = {}  # the dialog's key (place_key) -> QRect: where the human moved or sized it, for this app session
UNLIMITED = (1 << 24) - 1  # QWIDGETSIZE_MAX


def own_rule(dialog):
    """Whether DIALOG takes the whole rule (its texts, fields, size and place): a window of its own, not Qt's file,
    colour or font dialog (a native one is no widget at all) and no popup."""
    from PySide6.QtWidgets import QColorDialog, QFileDialog, QFontDialog
    if isinstance(dialog, (QFileDialog, QColorDialog, QFontDialog)) or not dialog.isWindow():
        return False
    return dialog.windowType() not in (Qt.WindowType.Popup, Qt.WindowType.ToolTip, Qt.WindowType.SplashScreen)


def least_width(dialog):
    """The least width of DIALOG: DIALOG_LEAST, more with a larger font (LEAST_CHARS of it)."""
    return max(DIALOG_LEAST, LEAST_CHARS * max(1, dialog.fontMetrics().averageCharWidth()))


def screen_limit(dialog):
    """The most a dialog may take of its screen: (width, height), SCREEN_SHARE of its free area (all of it on a
    small screen, a phone's); None without a screen."""
    area = screen_area(dialog)
    if area is None or area.width() < 200 or area.height() < 150:
        return None
    if area.width() < SMALL_SCREEN:
        return area.width(), area.height()
    return int(area.width() * SCREEN_SHARE), int(area.height() * SCREEN_SHARE)


def breakable(text, rich=False):
    """TEXT with a zero-width space after the separators (BREAK_AFTER) of each word longer than BREAK_WORD - a path,
    a URL - so that a wrapping label breaks it there instead of growing as wide as the word; RICH: TEXT is markup,
    whose tags and entities are left as they are."""
    def words(part):
        return re.sub(r"\S{%d,}" % (BREAK_WORD + 1),
                      lambda m: "".join(c + ZERO_WIDTH_SPACE if c in BREAK_AFTER else c for c in m.group(0)).rstrip(
                          ZERO_WIDTH_SPACE), part)
    if ZERO_WIDTH_SPACE in text:
        return text
    if not rich:
        return words(text)
    return "".join(part if part.startswith("<") else re.sub(r"[^&]+|&\w+;|&#\d+;|&", lambda m: m.group(0) if
                                                            m.group(0).startswith("&") else words(m.group(0)), part)
                   for part in re.split(r"(<[^>]*>)", text))


def readable_labels(dialog):
    """The labels of DIALOG wrap at a readable width: a text longer than READABLE_CHARS wraps (at about that width:
    Qt's own measure for a wrapping label), and a long word in it (a path, a URL) breaks after its separators instead
    of making the dialog as wide as the word. A label whose text can be selected keeps its words (a copied path must
    stay the path); a message box's text is the box's own."""
    for label in dialog.findChildren(QLabel):
        if label.objectName().startswith("qt_msgbox") or getattr(label, "elides", False) or \
                isinstance(label, ElidedLabel) or label.pixmap() is not None and not label.pixmap().isNull():
            continue
        text = label.text()
        if not text:
            continue
        rich = label.textFormat() == Qt.TextFormat.RichText or \
            label.textFormat() == Qt.TextFormat.AutoText and QtGui.Qt.mightBeRichText(text)
        metrics = label.fontMetrics()
        readable = READABLE_CHARS * max(1, metrics.averageCharWidth())
        if not label.wordWrap() and label.sizeHint().width() > readable:
            label.setWordWrap(True)
        if label.wordWrap() and not label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse \
                and re.search(r"\S{%d,}" % (BREAK_WORD + 1), text):
            shown = breakable(text, rich)
            if shown != text:
                label.setText(shown)


def wrapped_lines(text, metrics, width):
    """TEXT broken at its spaces into lines of at most WIDTH pixels in METRICS (a word longer stays whole)."""
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for word in paragraph.split(" "):
            joined = f"{line} {word}" if line else word
            if line and metrics.horizontalAdvance(joined) > width:
                lines.append(line)
                line = word
            else:
                line = joined
        lines.append(line)
    return "\n".join(lines)


def readable_buttons(dialog):
    """The check boxes and radio buttons of DIALOG with a text longer than READABLE_CHARS: on lines of about that
    width (a QCheckBox keeps its text on one line and made a dialog wider than the screen with a large font). Its
    one-line text stays its accessible name and its property "wholeText"."""
    from PySide6.QtWidgets import QRadioButton
    for box in dialog.findChildren(QAbstractButton):
        if not isinstance(box, (QCheckBox, QRadioButton)) or getattr(box, "wraps", False) or "\n" in box.text():
            continue
        metrics = box.fontMetrics()
        readable = READABLE_CHARS * max(1, metrics.averageCharWidth())
        if metrics.horizontalAdvance(box.text()) <= readable:
            continue
        whole = box.text()
        box.setProperty("wholeText", whole)
        if not box.accessibleName():
            box.setAccessibleName(whole.replace("&&", "\0").replace("&", "").replace("\0", "&"))
        box.setText(wrapped_lines(whole, metrics, readable))


def shares_row(field):
    """Whether FIELD stands in a row with other fields (a host and its port), not alone in it (a name)."""
    from PySide6.QtWidgets import QAbstractSpinBox, QBoxLayout, QFormLayout
    widget, holder = field, None
    while holder is None and widget.parentWidget() is not None and not widget.isWindow():
        parent = widget.parentWidget()
        holder = owning_layout(parent.layout(), widget) if parent.layout() is not None else None
        if holder is None:
            widget = parent  # (FIELD in a widget of its own: a path field with its button)
    if holder is None or isinstance(holder, QFormLayout):
        return False
    inputs = (QLineEdit, QAbstractSpinBox, QComboBox)
    if isinstance(holder, QBoxLayout):
        if holder.direction() not in (QBoxLayout.Direction.LeftToRight, QBoxLayout.Direction.RightToLeft):
            return False
        others = [holder.itemAt(i).widget() for i in range(holder.count())]
    elif isinstance(holder, QGridLayout):
        row = holder.getItemPosition(holder.indexOf(widget))[0]
        others = [item.widget() for item in (holder.itemAtPosition(row, column)
                                             for column in range(holder.columnCount())) if item is not None]
    else:
        return False
    return any(isinstance(other, inputs) and other is not widget and not other.isHidden() for other in others)


def owning_layout(layout, widget):
    """The layout (LAYOUT or one inside it) that holds WIDGET itself; None."""
    if layout.indexOf(widget) >= 0:
        return layout
    for index in range(layout.count()):
        inner = layout.itemAt(index).layout()
        if inner is not None:
            found = owning_layout(inner, widget)
            if found is not None:
                return found
    return None


def field_least(field):
    """The least width of the text field FIELD: NAME_CHARS of its font alone in its row, SHARED_CHARS beside other
    fields; 0 for one that is no field of its own (a spin box's, a combo box's), one given a width of its own (a
    maximum, a fixed size policy) or one in a list."""
    from PySide6.QtWidgets import QAbstractSpinBox
    parent = field.parentWidget()
    if isinstance(parent, (QAbstractSpinBox, QComboBox)) or field.maximumWidth() < UNLIMITED or \
            field.sizePolicy().horizontalPolicy() in (QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum):
        return 0
    while parent is not None and not parent.isWindow():
        if isinstance(parent, QAbstractItemView):
            return 0
        parent = parent.parentWidget()
    metrics = field.fontMetrics()
    frame = max(0, QLineEdit.sizeHint(field).width() - 17 * metrics.horizontalAdvance("x"))
    return (SHARED_CHARS if shares_row(field) else NAME_CHARS) * max(1, metrics.averageCharWidth()) + frame


def wide_fields(dialog):
    """The text fields of DIALOG at least field_least wide (within its screen): a name field shows about 40
    characters - the file manager's New folder showed 17."""
    limit = screen_limit(dialog)
    for field in dialog.findChildren(QLineEdit):
        least = field_least(field)
        if limit is not None:
            least = min(least, limit[0] - 160)
        if least > 0 and field.minimumWidth() < least:
            field.setMinimumWidth(least)


def scroll_contents(dialog):
    """DIALOG's contents (all but the row of buttons at its bottom) in a scroll area of their own: they need more
    height than its screen has, and the buttons stay in reach below. Once."""
    layout = dialog.layout()
    if layout is None or dialog.property("scrolledContents") or isinstance(dialog, QMessageBox):
        return False
    count = layout.count()
    last = layout.itemAt(count - 1) if count else None
    if last is None:
        return False
    holder = last.widget() or last.layout()
    rows = [holder] if isinstance(holder, QDialogButtonBox) else \
        (holder.findChildren(QDialogButtonBox) if isinstance(holder, QWidget) else [])
    in_layout = last.layout() is not None and any(isinstance(last.layout().itemAt(i).widget(),
                                                             (QDialogButtonBox, QAbstractButton))
                                                  for i in range(last.layout().count()))
    if not rows and not in_layout:
        return False
    margins = layout.contentsMargins()
    inner = QWidget()
    inner.setObjectName("scrolledContents")
    inner.setLayout(layout)  # (takes the layout and its widgets from the dialog)
    item = layout.takeAt(layout.count() - 1)
    layout.setContentsMargins(0, 0, 0, 0)
    area = QScrollArea()
    area.setObjectName("dialogScroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setWidget(inner)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(margins)
    outer.addWidget(area, 1)
    if item.widget() is not None:
        outer.addWidget(item.widget())
    elif item.layout() is not None:
        row = item.layout()
        row.setParent(None)
        outer.addLayout(row)
    dialog.setProperty("scrolledContents", True)
    return True


def needed_height(dialog, width):
    """The height DIALOG's layout needs at WIDTH (its wrapping texts laid out there)."""
    layout = dialog.layout()
    if layout is None:
        return dialog.sizeHint().height()
    layout.activate()
    if layout.hasHeightForWidth():
        return max(layout.totalHeightForWidth(width), layout.totalMinimumSize().height())
    return max(layout.totalSizeHint().height(), layout.totalMinimumSize().height())


def fit_size(dialog):
    """DIALOG (about to be shown) as large as its content needs - its size hint, its least width (least_width), its
    title (title_width), its wrapping texts at that width - and at most SCREEN_SHARE of its screen; contents that
    need more height than that scroll (scroll_contents), its buttons stay below. A message box sizes itself (Qt):
    make_readable gives it its least width."""
    if isinstance(dialog, QMessageBox):
        return
    limit = screen_limit(dialog)
    layout = dialog.layout()
    if layout is not None:
        layout.activate()
    hint = dialog.sizeHint().expandedTo(dialog.minimumSizeHint())
    # The size its code gave it is kept (it may be larger than its hint: a list's room); the one Qt gave it before
    # the texts above were wrapped is not (a long line made an input dialog as wide as the screen).
    given = QWidget.size(dialog) if dialog.testAttribute(Qt.WidgetAttribute.WA_Resized) else QSize(0, 0)
    # (its title too: an input dialog's layout sets its minimum size from its contents when it is activated, so the
    # minimum make_readable gave it for its title was gone - "Hardware-Limits von NAME" was cut in its title bar)
    width = max(given.width(), hint.width(), min(max(least_width(dialog), title_width(dialog)), dialog.maximumWidth()))
    if limit is not None:
        width = min(width, limit[0])
    height = max(given.height(), needed_height(dialog, width))
    if limit is not None and height > limit[1]:
        least = layout.totalMinimumSize().height() if layout is not None else 0
        if layout is not None and layout.hasHeightForWidth():
            least = max(least, layout.totalHeightForWidth(width))
        if least > limit[1] and scroll_contents(dialog):
            dialog.layout().activate()
        height = limit[1]
    if limit is not None:
        if dialog.minimumWidth() > limit[0]:
            dialog.setMinimumWidth(limit[0])
        if dialog.minimumHeight() > limit[1]:
            dialog.setMinimumHeight(limit[1])
    width, height = min(width, dialog.maximumWidth()), min(height, dialog.maximumHeight())
    if (width, height) != (dialog.width(), dialog.height()):
        dialog.resize(width, height)


def place_key(window):
    """What a dialog is remembered by (PLACES): its class; a plain dialog, message box or input dialog also by its
    title (each question is another)."""
    from PySide6.QtWidgets import QInputDialog
    kind = type(window)
    key = f"{kind.__module__}.{kind.__qualname__}"
    if kind in (QDialog, QMessageBox, QInputDialog, MessageDialog, QWidget):
        key += "|" + window.windowTitle()
    return key + "|" + window.objectName()


def anchor_of(window):
    """The window WINDOW is placed over: the window of its parent, else the app's active window (the one it was
    opened from); None."""
    parent = window.parentWidget()
    anchor = parent.window() if parent is not None else QApplication.activeWindow()
    if anchor is window or anchor is None or not anchor.isVisible():
        return None
    return anchor


def placement(window, size=None):
    """Where WINDOW (of SIZE, default its own) goes by the rule: a small or medium one centred over the window that
    opened it (anchor_of), a large one (wider or higher than LARGE_SHARE of the screen) at that window's top left,
    LARGE_INSET inside; without such a window, the same on the screen; always inside the screen's free area. Returns
    a QPoint (None without a screen)."""
    area = screen_area(window)
    if area is None:
        return None
    size = size or QWidget.size(window)
    anchor = anchor_of(window)
    over = anchor.frameGeometry() if anchor is not None else area
    if anchor is not None and not over.intersects(area):
        over = area
    large = size.width() > area.width() * LARGE_SHARE or size.height() > area.height() * LARGE_SHARE
    if large:
        x, y = over.left() + LARGE_INSET, over.top() + LARGE_INSET
    else:
        x, y = over.center().x() - size.width() // 2, over.center().y() - size.height() // 2
    return inside(area, QPoint(x, y), size)


def inside(area, point, size):
    """POINT moved so that a window of SIZE there lies inside AREA (its top left corner first)."""
    x = max(area.left(), min(point.x(), area.right() - size.width() + 1))
    y = max(area.top(), min(point.y(), area.bottom() - size.height() + 1))
    return QPoint(x, y)


def place(window):
    """WINDOW (being shown) where the rule puts it (placement) - or, when the human moved or sized it earlier in this
    app session, where they left it (PLACES), its size never below what it needs nor beyond its screen."""
    try:
        area = screen_area(window)
        if area is None:
            return
        kept = PLACES.get(place_key(window))
        if kept is not None:
            limit = screen_limit(window) or (area.width(), area.height())
            size = kept.size().expandedTo(window.minimumSizeHint()).expandedTo(window.minimumSize())
            size = QSize(min(size.width(), limit[0], window.maximumWidth()),
                         min(size.height(), limit[1], window.maximumHeight()))
            if not isinstance(window, QMessageBox) and size != QWidget.size(window):
                window.resize(size)
            point = inside(area, kept.topLeft(), QWidget.size(window))
        else:
            point = placement(window)
        if point is None:
            return
        if point != window.pos():
            window.move(point)
        window.setProperty("placedAt", QRect(window.pos(), QWidget.size(window)))
    except RuntimeError:  # (deleted meanwhile)
        pass


def keep_place(window):
    """WINDOW (being hidden) moved or sized by the human from where the rule placed it: remembered for this app
    session (PLACES), so that it opens there again."""
    try:
        placed = window.property("placedAt")
        if not isinstance(placed, QRect):
            return
        now = QRect(window.pos(), QWidget.size(window))
        moved = (now.topLeft() - placed.topLeft()).manhattanLength() > MOVED
        sized = abs(now.width() - placed.width()) > MOVED or abs(now.height() - placed.height()) > MOVED
        if moved or sized:
            PLACES[place_key(window)] = now
    except RuntimeError:  # (deleted meanwhile)
        pass


def dialog_rule(dialog):
    """The one rule for every dialog, message box and input dialog (DIALOG, about to be shown; fitting_dialogs
    applies it to each, so a new one follows it too): its texts wrap at a readable width (readable_labels), a long
    check box's text is on lines (readable_buttons), a text field shows about 40 characters (wide_fields), it is at
    least DIALOG_LEAST and its title wide (make_readable), as large as its content needs and at most SCREEN_SHARE of
    its screen, scrolling beyond (fit_size). place() then puts it where it belongs once its size is known."""
    if not own_rule(dialog):
        fit_on_screen(dialog)
        return
    dialog.ensurePolished()  # (its theme's fonts and paddings: what it needs is measured with them)
    readable_labels(dialog)
    readable_buttons(dialog)
    wide_fields(dialog)
    make_readable(dialog)
    fit_size(dialog)


def window_rule(window):
    """The rule for an extra window that is no dialog (the log window, a video, a browser): at most SCREEN_SHARE of
    its screen, placed by the rule (place) when it is first shown - afterwards it stays where the human leaves it."""
    try:
        if window.property("placedAt") is not None:
            return
        limit = screen_limit(window)
        if limit is not None:
            width, height = min(window.width(), limit[0]), min(window.height(), limit[1])
            if window.minimumWidth() > width:
                window.setMinimumWidth(width)
            if window.minimumHeight() > height:
                window.setMinimumHeight(height)
            if (width, height) != (window.width(), window.height()):
                window.resize(width, height)
        place(window)
    except RuntimeError:  # (deleted meanwhile)
        pass


def within_limit(dialog):
    """DIALOG (shown) still at most SCREEN_SHARE of its screen: what it got after it was sized (its [x]'s room at the
    top, a row shown, a text read in the background) made it higher than that - its content scrolls then, its
    buttons stay below."""
    limit = screen_limit(dialog)
    layout = dialog.layout()
    if limit is None or layout is None or isinstance(dialog, QMessageBox):
        return
    size = QWidget.size(dialog)
    if size.width() <= limit[0] and size.height() <= limit[1]:
        return
    need = layout.totalMinimumSize().height()
    if layout.hasHeightForWidth():
        need = max(need, layout.totalHeightForWidth(min(size.width(), limit[0])))
    if need > limit[1] and scroll_contents(dialog):
        dialog.layout().activate()
    if dialog.minimumWidth() > limit[0]:
        dialog.setMinimumWidth(limit[0])
    if dialog.minimumHeight() > limit[1]:
        dialog.setMinimumHeight(limit[1])
    dialog.resize(min(size.width(), limit[0]), min(size.height(), limit[1]))


def after_shown(dialog):
    """DIALOG, shown: as high as its wrapping texts need at its final width (fit_height), and placed again when its
    size changed since it was placed (its own code, a text laid out) while it stays where it was put."""
    try:
        if dialog.layout() is not None:
            dialog.layout().activate()  # (a layout request still waiting: the minimum size it sets, now)
        fit_height(dialog)
        within_limit(dialog)
        placed = dialog.property("placedAt")
        if isinstance(placed, QRect) and placed.topLeft() == dialog.pos() and placed.size() != QWidget.size(dialog):
            place(dialog)
    except RuntimeError:  # (deleted meanwhile)
        pass


# The roles of a button box's buttons in the order one becomes the dialog's default when none is (Enter presses it):
# what accepts first, then what closes - a dialog with Close alone closes on Enter as on Esc.
DEFAULT_ROLES = ("AcceptRole", "YesRole", "ApplyRole", "RejectRole", "NoRole")


def ensure_default_button(dialog):
    """DIALOG (shown) has a default button: Qt makes a button box's first accepting button one, but not a Close
    alone, and a dialog of its own may have none - Enter did nothing there (round 2, 2026-11-22: every dialog has a
    default button). A wizard and a message box choose theirs themselves; a disabled button is never made one."""
    from PySide6.QtWidgets import QDialogButtonBox, QMessageBox, QPushButton, QWizard
    if isinstance(dialog, (QWizard, QMessageBox)):
        return None
    shown = [button for button in dialog.findChildren(QPushButton) if button.isVisibleTo(dialog)]
    if not shown or any(button.isDefault() for button in shown):
        return None
    for role in DEFAULT_ROLES:
        wanted = getattr(QDialogButtonBox.ButtonRole, role)
        for box in dialog.findChildren(QDialogButtonBox):
            if not box.isVisibleTo(dialog):
                continue
            for button in box.buttons():
                if isinstance(button, QPushButton) and box.buttonRole(button) == wanted and \
                        button.isVisibleTo(dialog) and button.isEnabled():
                    button.setDefault(True)
                    return button
    return None


def fitting_dialogs():
    """Every dialog and message box follows the one rule when it is shown (dialog_rule, then place), and a dialog
    the human moved or sized opens there again (keep_place). Once per process."""
    from PySide6.QtWidgets import QMessageBox as Box
    for cls in (QDialog, Box):
        original = cls.showEvent
        if getattr(original, "fits", False):
            continue

        def show_event(dialog, event, original=original):
            if event.spontaneous():
                return original(dialog, event)
            try:
                dialog_rule(dialog)
            except RuntimeError:  # (deleted meanwhile)
                pass
            result = original(dialog, event)  # (a message box sizes itself here; Qt centres a dialog here)
            # (after the button boxes chose theirs, as they are shown, and once Qt chose its own after the show: the
            # first button of the focus chain that takes one, a hidden one too)
            QTimer.singleShot(0, dialog, lambda d=dialog: ensure_default_button(d))
            if own_rule(dialog):
                place(dialog)
                if not isinstance(dialog, Box):  # (a message box sizes itself)
                    QTimer.singleShot(0, lambda d=dialog: after_shown(d))
            return result
        show_event.fits = True
        cls.showEvent = show_event
    hidden = QDialog.hideEvent
    if not getattr(hidden, "fits", False):
        def hide_event(dialog, event, original=hidden):
            if not event.spontaneous():
                keep_place(dialog)
            return original(dialog, event)
        hide_event.fits = True
        QDialog.hideEvent = hide_event


def log_window_shortcut(dialog):
    """Ctrl+Shift+L in DIALOG opens the log window of its own (once per dialog; nothing before the window set
    LOG_WINDOW_OPENER)."""
    if LOG_WINDOW_OPENER is None or dialog.property("logWindowKeys"):
        return
    from PySide6.QtGui import QKeySequence, QShortcut
    shortcut = QShortcut(QKeySequence(LOG_WINDOW_KEYS), dialog)
    shortcut.activated.connect(lambda: LOG_WINDOW_OPENER is not None and LOG_WINDOW_OPENER())
    dialog.setProperty("logWindowKeys", True)


def emergency_shortcut(dialog):
    """Ctrl+Shift+X in DIALOG is the emergency stop too: the window's own action (EMERGENCY_ACTION) is added to it
    (nothing before the window set it). That action is an application shortcut, and Qt takes none in a window a modal
    dialog blocks: in a file dialog (window-modal) and in the Questions window (application-modal) the key did
    nothing. The same action, never a second shortcut: two of one key where both count are ambiguous - neither acts
    (a dialog that blocks only a window of its own, the log window's, leaves the main window's counting)."""
    action = EMERGENCY_ACTION
    if action is None:
        return
    try:
        # (QWidget's own: a dialog may have an attribute of that name - the bridge dialog's observer actions hid it,
        # and New bridge… opened nothing)
        if action not in QWidget.actions(dialog):
            QWidget.addAction(dialog, action)
    except RuntimeError:  # the window and its action are gone
        pass


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
        log_answer(parent, title_text, text, accept if AUTO_ANSWER else reject or "Cancel")
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
    exec_dropped(box)  # (made anew for each question)
    log_answer(parent, title_text, text, accept if chosen else reject or tr("Cancel"))
    return bool(chosen)


AUTO_SECRET = NotImplemented  # tests: the value ask_secret_value() answers (None: declined; NotImplemented: ask)


class SecretValueDialog(QDialog):
    """A question for one secret's value: a password field (what is typed is never shown), Keep and Cancel. value() is
    the text typed, or None while nothing is typed. Nothing of it is ever logged."""

    def __init__(self, parent, title_text, text, accept):
        super().__init__(parent)
        self.setWindowTitle(str(title_text))
        self.setObjectName("secret_value_dialog")
        layout = QVBoxLayout(self)
        label = wrapping_text(text)
        label.setObjectName("secret_value_text")
        layout.addWidget(label)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setAccessibleName(tr("Value"))
        self.edit.setPlaceholderText(tr("the value (it is never shown or logged)"))
        # (no clipboard history, no completion: what is typed here goes to the secret store and nowhere else)
        self.edit.setClearButtonEnabled(False)
        layout.addWidget(self.edit)
        self.buttons = QDialogButtonBox()
        self.keep_button = self.buttons.addButton(str(accept), QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.edit.returnPressed.connect(self.accept)
        self.edit.setFocus()

    def value(self):
        return self.edit.text().strip() or None


def ask_secret_value(parent, title_text, text, accept):
    """The value of a secret typed into a password field (the dialog's own text says which secret), or None when the
    human cancelled or typed nothing. The question goes into the log, the answer's word only - never the value."""
    if AUTO_SECRET is not NotImplemented:
        SHOWN.append((title_text, text, None))
        log_answer(parent, title_text, text, accept if AUTO_SECRET else tr("Cancel"))
        return AUTO_SECRET or None
    dialog = SecretValueDialog(parent, title_text, text, accept)
    value = None
    if exec_dropped(dialog) == QDialog.DialogCode.Accepted:
        value = dialog.value()
    dialog.edit.clear()
    log_answer(parent, title_text, text, accept if value else tr("Cancel"))
    return value


def ask_password(parent, title_text, text, accept):
    """A password typed into a hidden field, exactly as typed (the dialog's text says which), or None when the human
    cancelled or typed nothing. The question goes into the log, the answer's word only - never the password."""
    if AUTO_SECRET is not NotImplemented:
        SHOWN.append((title_text, text, None))
        log_answer(parent, title_text, text, accept if AUTO_SECRET else tr("Cancel"))
        return AUTO_SECRET or None
    dialog = SecretValueDialog(parent, title_text, text, accept)
    dialog.setObjectName("password_dialog")
    dialog.edit.setAccessibleName(tr("Password"))
    dialog.edit.setPlaceholderText(tr("the password (it is never shown or logged)"))
    value = None
    if exec_dropped(dialog) == QDialog.DialogCode.Accepted:
        value = dialog.edit.text() or None
    dialog.edit.clear()
    log_answer(parent, title_text, text, accept if value else tr("Cancel"))
    return value


# tests: the key choose() answers while AUTO_ANSWER is set (None, or a key the question does not have: its first
# choice, or none)
AUTO_CHOICE = None


def choose(parent, title_text, text, choices, cancel=None, destructive=()):
    """One of several answers: the key of CHOICES ([(key, text)], in their order) the human chose, or None for CANCEL
    (default "Cancel", also Esc and the default button). Keys in DESTRUCTIVE are marked as such."""
    if AUTO_ANSWER is not None:
        SHOWN.append((title_text, text, None))
        answer = None if not AUTO_ANSWER else AUTO_CHOICE if AUTO_CHOICE in [key for key, _ in choices] else \
            (choices[0][0] if choices else None)
        log_answer(parent, title_text, text, "Cancel" if answer is None else answer)
        return answer
    box = _box(parent, QMessageBox.Icon.Question, title_text, text)
    chosen, buttons = [], []
    for key, label in choices:
        button = box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
        if key in destructive:
            button.setProperty("role", "danger")
        button.clicked.connect(lambda *_, k=key: chosen.append(k))
        buttons.append(button)
    no = box.addButton(cancel or tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    exec_dropped(box)  # (made anew for each question)
    log_answer(parent, title_text, text, chosen[0] if chosen else cancel or tr("Cancel"))
    return chosen[0] if chosen else None


def ask_first(parent, action, title_text, text, accept, detail=None, details=None, reject=None):
    """The app's one confirmation policy: True when ACTION (a key of backend.NEEDS_CONFIRM or APP_NEEDS_CONFIRM) may
    go ahead - at once when backend.needs_confirm(ACTION, DETAIL) says it needs no confirmation, else only when the
    human chose ACCEPT in a destructive confirmation (Cancel the default)."""
    from . import backend  # here: backend never imports Qt, and the widgets need no backend otherwise
    if backend.needs_confirm(action, detail) is None:
        applog.log("INFO", where_of(parent), f"{said(title_text, 200)}: goes ahead without a question (the setting "
                                             f"of {action} needs none)")
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


def dry_run_answer(asked, now):
    """(title suffix, note) for a dry run's answer: ASKED and NOW are (what was asked: "board" or "form", its state,
    host, time) when it was asked and when it answered. ("", "") while both are the same; else the answer is for what
    was asked then - said above it, with the host and the time (R2-20: it was shown as the plan of what is shown)."""
    from .files import printable
    kind, state, host, when = asked
    _kind, state_now, host_now, _when = now
    if host != host_now:
        note = tr("Another host was chosen while the dry run ran: this answer is from {host} (asked at {time}), not "
                  "from {now}. Run Dry run again for {now}.", host=printable(host), time=when, now=printable(host_now))
    elif state != state_now and kind == "board":
        note = tr("The board changed while the dry run ran: this answer is for the board as it was at {time} on "
                  "{host}. Run Dry run again for the board as it is now.", time=when, host=printable(host))
    elif state != state_now:
        note = tr("The form changed while the dry run ran: this answer is for the options as they were at {time} on "
                  "{host}. Run Dry run again for the form as it is now.", time=when, host=printable(host))
    else:
        return "", ""
    return " – " + tr("out of date"), note


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
        if str(text) != self.label.text() or (details or "").strip() != self.details.toPlainText():
            log_notice(self, text, level, details)
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
        self.argv_shown = applog.argv_text(argv)
        applog.log("DEBUG", "command", f"started: {self.argv_shown} (timeout {self.timeout:g} s)")
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
        took = applog.seconds(time.monotonic() - getattr(self, "started_at", time.monotonic()))
        applog.log("INFO" if status else "DEBUG", "command",
                   f"{'stopped' if self.cancelled else f'exit status {status}'} after {took}: "
                   f"{getattr(self, 'argv_shown', '')}" + (f"\n{applog.tail(err)}" if status and err.strip() else ""))
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
        applog.set_work(self.work)
        try:
            result, error = self.function(*self.args), None
        except BaseException as exc:  # noqa: BLE001 - reported to the page; SystemExit too (argparse, sys.exit)
            result, error = None, exc
        finally:
            _worker.call = None
            applog.set_work(None)
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

    def __init__(self, done, sources, owner, counted, work, name, timeout, what=None):
        self.done, self.sources, self.owner, self.counted, self.work = done, sources, owner, counted, work
        self.name, self.began, self.answered, self.timer, self.timeout = name, time.monotonic(), False, None, timeout
        self.what = what or name  # what the call does, in words (background's WHAT, or describe()'s)

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
        ended_log(self.what, self.work, time.monotonic() - self.began, error, self.answered)
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
        applog.log("WARNING", "background", f"no answer within {int(self.timeout)} s: {self.what} - still running "
                   "(it cannot be stopped; its buttons stay disabled until it ends, its result is dropped)")
        if self.owner_alive():
            self.answer(None, TimeoutError(tr("No answer within {seconds} s.", seconds=int(self.timeout))))

    def answer(self, result, error):
        if self.answered:
            return
        self.answered = True
        done, self.done = self.done, None
        try:
            done(result, error)
        except Exception as exc:  # noqa: BLE001 - written as the exception hook writes it, the app goes on as before
            # Raised by what handles the call's result: the entry names the call whose result it was - the hook named
            # only the widget that had the focus then.
            text = applog.entry(type(exc), exc, exc.__traceback__,
                                delivered=f"the result of a background call: {self.what}")
            applog.write(text)
            applog.notify("ERROR", applog.component(exc.__traceback__)[0], text.split("\n", 1)[1])


_described = {}  # describe(): {code object: the words found for it}
_DESCRIBE_SKIP = {"self", "sai", "page", "window", "host", "args", "kwargs", "get", "append", "items", "keys", "values",
                  "isinstance", "len", "list", "dict", "set", "tuple", "str", "int", "bool", "sorted", "any", "all",
                  "min", "max", "print", "range", "enumerate", "zip", "getattr", "hasattr", "json", "os", "path",
                  "Path", "strip", "split", "join", "startswith", "endswith", "partition", "format", "update",
                  "setdefault", "pop", "add", "extend", "lower", "replace", "exists", "time", "monotonic", "Exception",
                  "OSError", "ValueError", "RuntimeError", "TypeError", "KeyError", "type", "iter", "next", "float",
                  "frozenset", "repr", "open", "read", "write", "close", "dumps", "loads", "isfile", "isdir"}


def _called_names(code, depth=0):
    """The names CODE (and the functions and lambdas defined in it) looks up, in order: what it calls."""
    names = list(code.co_names)
    if depth < 3:
        for const in code.co_consts:
            if hasattr(const, "co_names"):
                names += _called_names(const, depth + 1)
    return list(dict.fromkeys(names))


# SandboxAI's methods that run the command their arguments name: described as "sandbox-ai command" (its arguments
# follow), not by their docstring ("(exit status, stdout, stderr) of a sandbox-ai command that does not need…").
RUNNERS = ("call", "host_run", "host_json")


def describe(function):
    """What FUNCTION (handed to background()) does, in words, for the log: its name without the "<locals>" parts, the
    first sentence of its docstring and, for a lambda or an inner function, the sandbox-ai calls it makes - not only
    `Prerequisites.check_prerequisites.<locals>.read`. Worked out once per function's code."""
    import functools
    inner = function
    while isinstance(inner, functools.partial):
        inner = inner.func
    code = getattr(inner, "__code__", None) or getattr(getattr(inner, "__func__", None), "__code__", None)
    key = code if code is not None else getattr(inner, "__qualname__", None) or type(inner).__name__
    found = _described.get(key)
    if found is not None:
        return found
    qualname = str(getattr(inner, "__qualname__", None) or getattr(inner, "__name__", None) or type(inner).__name__)
    if type(getattr(inner, "__self__", None)).__name__ == "SandboxAI":
        if getattr(inner, "__name__", None) in RUNNERS:  # (its arguments are the command: arguments_text)
            _described[key] = "sandbox-ai command" if inner.__name__ == "call" else "sandbox-ai command on a host"
            return _described[key]
        qualname = f"sandbox-ai {getattr(inner, '__name__', qualname)}"
    short = qualname.replace(".<locals>", "")
    doc = (getattr(inner, "__doc__", None) or "").strip() if code is not None else ""
    sentence = " ".join(doc.split("\n\n")[0].split()).split(". ")[0].rstrip(".")
    words = short + (f" ({sentence[:117] + '…' if len(sentence) > 118 else sentence})" if sentence else "")
    if code is not None and ("<lambda>" in qualname or "<locals>" in qualname or not sentence):
        names = _called_names(code)
        try:
            from . import backend
            known = [n for n in names if n not in _DESCRIBE_SKIP and hasattr(backend.SandboxAI, n)]
        except Exception:  # noqa: BLE001 - the names it looks up then
            known = []
        if not known:
            known = [n for n in names if n not in _DESCRIBE_SKIP and not n.startswith("_")][:4]
        if known:
            words += " calling " + ", ".join(known[:5]) + (" …" if len(known) > 5 else "")
    if len(_described) > 4000:
        _described.clear()
    _described[key] = words
    return words


# Parameters whose argument is never logged (set_secret's VALUE, save_token's TOKEN, add_login's SECRET...).
SECRET_PARAMETER = re.compile(r"(?i)value|secret|token|passw|key|cred|auth|code|input|data|content|text")


def arguments_text(function, args, limit=200):
    """ARGS of a background call of FUNCTION for the log: the argument of a parameter whose name says it may be a
    secret (value, secret, token, password, key, ...) masked, and so is one whose parameter is not known; short values
    as they are (one that looks like a token masked: applog.argv_text), a list or a mapping by its size, anything else
    by its type."""
    import inspect
    try:
        bound = inspect.signature(function).bind_partial(*args).arguments
    except (TypeError, ValueError):
        return f"<{len(args)} arguments>"
    shown = []
    for name, value in bound.items():
        for one in (value if isinstance(value, tuple) and name not in ("self",) and
                    inspect.signature(function).parameters[name].kind == inspect.Parameter.VAR_POSITIONAL
                    else (value,)):
            if SECRET_PARAMETER.search(name):
                shown.append("***")
            elif isinstance(one, (str, int, float, bool)) or one is None:
                shown.append(str(one))
            elif isinstance(one, (list, tuple, set, frozenset, dict)):
                shown.append(f"<{type(one).__name__} of {len(one)}>")
            else:
                shown.append(f"<{type(one).__name__}>")
    return applog.argv_text(shown, limit)


def ended_log(what, work, seconds, error, timed_out=False):
    """The end of a background call in the log: work (a human's action) at INFO; a read at DEBUG, its failure at INFO
    once per change of the error (a poll that fails every few seconds is said once, then at DEBUG). TIMED_OUT: its
    caller was told of a timeout already (the late end is said, its result dropped)."""
    took = applog.seconds(seconds)
    late = " (late: its caller had a timeout already, the result is dropped)" if timed_out else ""
    if error is None:
        applog.log("INFO" if work or timed_out else "DEBUG", "background", f"done in {took}: {what}{late}")
        return
    said = f"failed after {took}: {what}: {applog.error_text(error)}{late}"
    if work:
        applog.log("WARNING", "background", said)
    else:
        applog.changed(("background", what), "INFO", "background", said, quiet="DEBUG")


def background(function, *args, done, source=None, owner=None, timeout=None, work=False, what=None):
    """Run FUNCTION(*args) in a thread, never on the GUI thread (which must keep answering approvals);
    DONE(result, error) runs on the GUI thread, once. SOURCE (a widget or a list of them, e.g. the button
    that started it) is disabled until the thread ends; OWNER (a page) counts its running calls for busy(), and
    a late result never reaches it once it is deleted. WORK (True for what a human started that changes
    something: an install, a start, a write) also counts for working(): the window's busy bar and the question
    when it is closed. A read or a poll (the default) shows as neither, and quitting ends it without a question.
    After TIMEOUT seconds without a result, DONE gets a TimeoutError (a late result is dropped). WHAT: what the call
    does, in words, for the log (else describe()d from FUNCTION); its start and end are logged with it (work at
    INFO, a read at DEBUG, a timeout at WARNING). Returns the ThreadCall."""
    sources = [w for w in (source if isinstance(source, (list, tuple)) else [source]) if w is not None]
    for widget in sources:
        _hold(widget)
    counted = owner is not None
    if counted:
        owner._background = getattr(owner, "_background", 0) + 1
        owner._work = getattr(owner, "_work", 0) + (1 if work else 0)
        activity().changed.emit()  # the window's busy indicator: which pages have work running
    name = getattr(function, "__qualname__", None) or repr(function)
    words = str(what) if what else describe(function)
    runner = not what and args and words.startswith("sandbox-ai command")
    if runner:  # the command is what it does: named in its end line too (a failed read is said at INFO)
        words = f"{words}: {arguments_text(function, args)}"
    if work or applog.debugging() or applog._debug_wanted:
        on = f" for {type(owner).__name__}" if owner is not None else ""
        given = f" with {arguments_text(function, args)}" if args and not runner else ""
        applog.log("INFO" if work else "DEBUG", "background",
                   f"started: {words}{given}{on}" + (f" (timeout {timeout:g} s)" if timeout else ""))
    state = _Call(done, sources, owner, counted, work, name, timeout, words)
    call = ThreadCall(function, *args, work=work)
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


# -- context menus: what the buttons beside a list offer, where the item is --

def button_text(button):
    """A button's text as a menu entry: its mnemonic marks (&) taken out, a doubled one kept as one &. An icon-only
    button: its label (button_icons.IconButton: the text it shows when texts are on), else (nav_button) its accessible
    name."""
    label = getattr(button, "menu_text", None)
    if not button.text() and isinstance(label, str) and label:
        return label
    if not button.text() and button.accessibleName():
        return button.accessibleName()
    return button.text().replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def shown_button(button, near=None):
    """Whether BUTTON is offered: not hidden itself or by what holds it below the nearest widget it shares with NEAR
    (the list whose menu offers it; else its window) - an action of another host, of a mode not chosen; whether the
    page itself is shown does not count."""
    if button.isHidden():
        return False
    from shiboken6 import getCppPointer

    def key(widget):
        return getCppPointer(widget)[0]  # (a wrapper made again for the same widget is the same one)
    ancestors, widget = set(), near
    while widget is not None:
        ancestors.add(key(widget))
        widget = widget.parentWidget()
    top = button.parentWidget()
    while top is not None and key(top) not in ancestors and top.parentWidget() is not None:
        top = top.parentWidget()
    return top is None or button.isVisibleTo(top)


# A combo box's entries, through its model's roles only. Never combo.model().item(i), takeRow or insertRow: those
# wrap a QStandardItem that C++ made, and when C++ deletes it (combo.clear(), removeRow, the combo deleted) a wrapper
# that was kept stays in Shiboken's map - the next object at that address (a QAction of the same size) is handed out
# as that QStandardItem: menu.addAction(text).setEnabled(...) ran QStandardItem::setEnabled on a QAction, a segfault
# in context_menu (the user's crash, 0.1.229). tests/test_standard_item_wrappers.py keeps them out of the app.
ITEM_FLAGS_ROLE = int(Qt.ItemDataRole.UserRole) - 1  # (where a QStandardItem keeps its flags: QStandardItem::setFlags)


def combo_index(combo, row):
    """The model index of COMBO's entry ROW (invalid: no such entry)."""
    model = combo.model()
    return model.index(row, combo.modelColumn()) if model is not None and row >= 0 else QModelIndex()


def row_flags(combo, row):
    """The Qt.ItemFlag of COMBO's entry ROW (no such entry: none)."""
    index = combo_index(combo, row)
    return combo.model().flags(index) if index.isValid() else Qt.ItemFlag.NoItemFlags


def set_row_flags(combo, row, flags):
    """COMBO's entry ROW with FLAGS (what QStandardItem.setFlags did, through the model's role; no such entry, or a
    model of the combo's own that keeps no flags: nothing)."""
    index = combo_index(combo, row)
    if index.isValid():
        combo.model().setData(index, int(getattr(flags, "value", flags)), ITEM_FLAGS_ROLE)


def row_enabled(combo, row):
    """Whether COMBO's entry ROW can be chosen (no such entry: False)."""
    return bool(row_flags(combo, row) & Qt.ItemFlag.ItemIsEnabled)


def set_row_enabled(combo, row, on, selectable=None):
    """COMBO's entry ROW enabled (ON) or greyed out, and selectable or not (SELECTABLE; None: as it is)."""
    if not combo_index(combo, row).isValid():
        return
    flags = row_flags(combo, row)
    flags = flags | Qt.ItemFlag.ItemIsEnabled if on else flags & ~Qt.ItemFlag.ItemIsEnabled
    if selectable is not None:
        flags = flags | Qt.ItemFlag.ItemIsSelectable if selectable else flags & ~Qt.ItemFlag.ItemIsSelectable
    set_row_flags(combo, row, flags)


def set_row_tip(combo, row, text):
    """COMBO's entry ROW with the tooltip TEXT (escaped and shortened: safe_tooltip)."""
    if combo_index(combo, row).isValid():
        combo.setItemData(row, safe_tooltip(text or ""), Qt.ItemDataRole.ToolTipRole)


def set_row_heading(combo, row):
    """COMBO's entry ROW a heading: bold, never chosen (no flags)."""
    if not combo_index(combo, row).isValid():
        return
    font = combo.itemData(row, Qt.ItemDataRole.FontRole)
    font = QFont(font) if isinstance(font, QFont) else QFont(combo.font())
    font.setBold(True)
    combo.setItemData(row, font, Qt.ItemDataRole.FontRole)
    set_row_flags(combo, row, Qt.ItemFlag.NoItemFlags)


class MenuGroup:
    """A submenu of a context menu (context_menu): TITLE, opened by pointing at it, with ENTRIES (context_menu's
    kinds). Left out when none of its entries is shown."""

    def __init__(self, title, entries):
        self.title, self.entries = title, list(entries)


def context_menu(parent, entries, owner=None):
    """The QMenu of ENTRIES, in their order: a button (a QPushButton or QToolButton: an entry with its text, enabled
    state and tooltip - the reason it is disabled -, chosen: clicked; a button with a menu: that menu's entries; a
    hidden button is left out), a QAction or a QMenu (as they are), a MenuGroup (a submenu of its entries, left out
    when it has none), (text, enabled, why, callback) (a TEXT entry, WHY its tooltip while disabled), None (a
    separator; never two, never first or last). OWNER: the menu's parent (a submenu's: the menu it is in, so it goes
    with it); PARENT decides which buttons are shown.
    Its own actions and submenus are made here (QAction(text, menu), QMenu(title, menu)) and then added: no method
    runs on what addAction/addMenu hand back, which Shiboken could give as a stale wrapper of another type."""
    menu = QMenu(owner if owner is not None else parent)
    menu.setToolTipsVisible(True)
    pending = False

    def submenu(title, enabled, actions):
        sub = QMenu(title, menu)
        sub.setToolTipsVisible(True)
        sub.setEnabled(enabled)
        for action in actions:
            sub.addAction(action)
        menu.addMenu(sub)

    def own_action(text, enabled, tip, triggered, icon=None):
        action = QAction(text, menu)
        if icon is not None:
            action.setIcon(icon)
        action.setEnabled(enabled)
        if tip:
            action.setToolTip(tip)
        if triggered is not None:
            action.triggered.connect(triggered)
        menu.addAction(action)

    for entry in entries:
        if entry is None:
            pending = not menu.isEmpty()
            continue
        if isinstance(entry, QAbstractButton) and not shown_button(entry, parent):
            continue
        if isinstance(entry, MenuGroup):
            sub = context_menu(parent, entry.entries, owner=menu)
            if sub.isEmpty():
                sub.deleteLater()
                continue
            if pending:
                separator = QAction(menu)
                separator.setSeparator(True)
                menu.addAction(separator)
                pending = False
            sub.setTitle(entry.title)  # (a menu made here, never one handed back by addMenu)
            menu.addMenu(sub)
            continue
        if pending:
            separator = QAction(menu)
            separator.setSeparator(True)
            menu.addAction(separator)
            pending = False
        if isinstance(entry, QMenu):
            # A submenu of this menu's own with the same actions, never the page's menu itself: this menu is dropped
            # after use (exec_dropped), and with it went what it held as a submenu - the page's menu entries were
            # deleted under it ("Internal C++ object (QAction) already deleted": hotfix round 1).
            submenu(entry.title(), entry.isEnabled(), entry.actions())
        elif isinstance(entry, QAction):
            menu.addAction(entry)
        elif isinstance(entry, tuple):
            text, enabled, why, callback = (tuple(entry) + (None, None, None))[:4]
            own_action(str(text), bool(enabled), safe_tooltip(why) if why and not enabled else "",
                       None if callback is None else (lambda _=False, f=callback: f()))
        elif isinstance(entry, QAbstractButton) and entry.menu() is not None and not entry.menu().isEmpty():
            submenu(button_text(entry), entry.isEnabled(), entry.menu().actions())
        elif isinstance(entry, QAbstractButton):
            # (the tooltip as the button has it: escaped when it was set - set_enabled, safe_tooltip)
            own_action(button_text(entry), entry.isEnabled(), entry.toolTip(), lambda _=False, b=entry: b.click(),
                       icon=None if entry.icon().isNull() else entry.icon())
    return menu


def menu_entries(menu):
    """[(text, enabled)] of MENU's entries (separators left out; a submenu's own entries after it, indented): what
    a test reads."""
    found = []
    for action in menu.actions():
        if action.isSeparator():
            continue
        found.append((action.text(), action.isEnabled()))
        if action.menu() is not None:
            found += [("  " + text, enabled) for text, enabled in menu_entries(action.menu())]
    return found


def select_under(view, point):
    """A right click on VIEW at POINT: the row there becomes the selection unless it is part of it already (the
    selected rows stay, for an action on all of them: Ctrl and Shift select several). Its index (invalid: none)."""
    index = view.indexAt(point)
    model = view.selectionModel()
    if not index.isValid() or model is None:
        return index
    if not model.isSelected(index):
        flags = QItemSelectionModel.SelectionFlag.ClearAndSelect
        if view.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows:
            flags |= QItemSelectionModel.SelectionFlag.Rows
        model.setCurrentIndex(index, flags)
    return index


def list_menu(view, entries, copy=True):
    """A context menu on the list, table or tree VIEW: ENTRIES() (see context_menu: mostly the buttons beside the list,
    which stay the one place of each action and its rules) for the row under the pointer (selected first, unless
    it is one of several selected), then Copy and Copy cell (COPY). The menu key and Shift+F10 open it too. Kept
    on the view (VIEW.menu_entries) for the tests."""
    from . import copying
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    view.menu_entries = entries

    def show(point):
        select_under(view, point)
        menu = context_menu(view, entries())
        if copy:
            copying.copy_entries(menu, view, point)
        if menu.isEmpty():
            menu.deleteLater()
            return
        where = view.viewport() if hasattr(view, "viewport") else view
        exec_dropped(menu, where.mapToGlobal(point))
    view.customContextMenuRequested.connect(show)
    return view


def menu_of(view):
    """The context menu list_menu gives VIEW now (not shown): for the tests."""
    return context_menu(view, view.menu_entries())


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
                      what=f"{describe(function)} for each of {len(items)}: " + applog.argv_text(
                          [str(i) for i in items[:6]], 200) + (" …" if len(items) > 6 else ""),
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
        self.began, self.job_text = time.monotonic(), str(text)
        applog.log("INFO" if getattr(self, "work", True) else "DEBUG", "task", f"started: {applog.redact(text)}")
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
        self._ended("cancelled" if cancelled else f"exit status {status}", status != 0 and not cancelled, err)
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

    def _ended(self, outcome, failed, err=""):
        """The job's end in the log: what it was, how long it took, how it ended (the tail of its error output)."""
        took = applog.seconds(time.monotonic() - getattr(self, "began", time.monotonic()))
        text = applog.redact(getattr(self, "job_text", ""))
        level = "WARNING" if failed else "INFO" if getattr(self, "work", True) else "DEBUG"
        applog.log(level, "task", f"ended after {took} ({outcome}): {text}" +
                   (f"\n{applog.tail(err)}" if failed and str(err or "").strip() else ""))

    def _thread_done(self, result, error):
        done = self.done
        status = result[0] if isinstance(result, tuple) and result and isinstance(result[0], int) else None
        self._ended("cancelled" if self.cancelled else f"failed: {applog.error_text(error)}" if error is not None
                    else f"exit status {status}" if status is not None else "done",
                    error is not None or (status not in (None, 0) and not self.cancelled),
                    result[2] if status not in (None, 0) and len(result) > 2 else "")
        self._end()
        if done:
            done(result, error)

    def cancel(self):
        if self.running:
            applog.log("INFO", "task", f"Cancel asked: {applog.redact(getattr(self, 'job_text', ''))}")
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


LAYOUT_SIGNATURE = ("QList<QPersistentModelIndex>", "QAbstractItemModel::LayoutChangeHint")  # (a layout signal's hint)


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
        # (VerticalSortHint: the selection is kept as one index a row, not one a cell - 20,000 rows selected took
        # seconds to sort, and the selection came back cut into a range a row, every later look at it slow)
        hint, signature = QAbstractItemModel.LayoutChangeHint.VerticalSortHint, LAYOUT_SIGNATURE
        self.layoutAboutToBeChanged[signature].emit([], hint)
        before = self.rows
        self.rows = self.ordered(before, column, order)
        self._move_persistent(before)
        self.layoutChanged[signature].emit([], hint)

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
        # Never super().__lt__(): PySide6 6.12 dispatches that back into this override - endless recursion while a
        # sorted table re-sorts (the user's log 2026-11-22, the Approvals page every second). Without comparable
        # sort values the texts are compared, as QTableWidgetItem's own operator< does.
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is not None and theirs is not None:
            try:
                return bool(mine < theirs)
            except TypeError:
                pass
        return self.text() < other.text()


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


def selected(widget, column=0):
    """The text of COLUMN in the selected row (read from the row itself, so sorting does not matter)."""
    rows = widget.selectionModel().selectedRows() if widget.selectionModel() else []
    if not rows:
        return None
    item = widget.item(rows[0].row(), column)
    return item.text() if item else None


def selected_rows(widget):
    """The visual rows selected, top to bottom: use row_key() to know what they stand for."""
    selection = widget.selectionModel()
    if selection is None:
        return []
    if not isinstance(widget, ModelTable):
        return sorted(index.row() for index in selection.selectedRows())
    # A ModelTable (whole rows selected, a row's cells alike): by the ranges - Qt's selectedRows() asked the model for
    # every cell's flags in Python, 140,000 calls for 20,000 rows (more than half a second at every Copy, F5, drag ...).
    model = widget.model()
    last, rows, partly = model.columnCount() - 1, set(), set()
    for part in selection.selection():
        if part.parent().isValid():
            continue
        (rows if part.left() <= 0 and part.right() >= last else partly).update(range(part.top(), part.bottom() + 1))
    rows.update(row for row in partly - rows if selection.isRowSelected(row))
    selectable = Qt.ItemFlag.ItemIsSelectable
    return sorted(row for row in rows if model.flags(model.index(row, 0)) & selectable)


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
    """Lays out widgets in rows that wrap when the window is narrow (button rows, toolbars). An item that wraps itself
    (one of its own, StartControls) and is wider than the row gets the row's width and the height it needs there.
    LINE: its size hint is all in one row (the room it takes where there is enough) - for a flow that is an item of
    another, which places it at its hint; without, its hint is its least size (a page's rows ask no more than that).
    Likewise any item that can be narrower than its hint (a row of a field that shrinks) when the row is narrower."""

    def __init__(self, parent=None, spacing=6, line=False):
        self.items, self.heights, self.minimum, self.line = [], {}, None, line
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
        if self.line:
            hints = [item.sizeHint() for item in self.items
                     if item.widget() is None or not item.widget().isHidden()]
            if hints:
                return QSize(sum(h.width() for h in hints) + self.gap * (len(hints) - 1),
                             max(h.height() for h in hints)).expandedTo(self.minimumSize())
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
        """The rows' height; APPLY: the items placed, each in the middle of its row's height (a label beside a
        field beside a button share one middle line)."""
        x, y, line = rect.x(), rect.y(), 0
        row = []  # (item, x, size) of the row being filled
        for item in self.items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if hint.width() > rect.width() and item.minimumSize().width() < hint.width():
                # Wider than the row, and it can be narrower (it wraps itself, a field shrinks): the row's width, and
                # the height it needs there.
                width = max(rect.width(), item.minimumSize().width())
                hint = QSize(width, item.heightForWidth(width) if item.hasHeightForWidth() else hint.height())
            if x + hint.width() > rect.right() + 1 and line > 0:
                self._place(row, y, line, apply)
                x, y, line, row = rect.x(), y + line + self.gap, 0, []
            row.append((item, x, hint))
            x += hint.width() + self.gap
            line = max(line, hint.height())
        self._place(row, y, line, apply)
        return y + line - rect.y()

    @staticmethod
    def _place(row, y, line, apply):
        if apply:
            for item, x, hint in row:
                item.setGeometry(QRect(x, y + (line - hint.height()) // 2, hint.width(), hint.height()))


class TwoLineToolButton(QToolButton):
    """A tool button whose text goes onto two lines when the room is narrower than its one line (in a flow row: the
    Board's "Security score: 100 of 100", wider than the rest of its row in a large font). Its size hint is the one
    line; its least width the two lines'; the text it shows follows the width it has (show_for)."""
    wraps = True  # tests/test_layout.py: measured by its height for its width

    def __init__(self, parent=None):
        super().__init__(parent)
        self.whole = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # (a tool button's is Fixed)

    def setText(self, text):  # noqa: N802
        self.whole = str(text)
        self.show_for(self.width() if self.isVisible() else None)

    def text(self):
        return self.whole

    def broken(self):
        """The text on two lines: after its first ": ", else at the space nearest its middle (the same text if none)."""
        text = self.whole
        if ": " in text:
            return text.replace(": ", ":\n", 1)
        spaces = [i for i, c in enumerate(text) if c == " "]
        if not spaces:
            return text
        middle = min(spaces, key=lambda i: abs(i - len(text) // 2))
        return text[:middle] + "\n" + text[middle + 1:]

    def hint_for(self, text):
        """Its size hint with TEXT shown: Qt's for the text shown now, less that text's size, plus TEXT's (no setText
        here: Qt asks for hints while it lays out, and a change of text would ask for another layout)."""
        base, metrics = super().sizeHint(), self.fontMetrics()
        shown = metrics.size(Qt.TextFlag.TextShowMnemonic, super().text())
        other = metrics.size(Qt.TextFlag.TextShowMnemonic, text)
        return QSize(base.width() - shown.width() + other.width(), base.height() - shown.height() + other.height())

    def sizeHint(self):  # noqa: N802
        return self.hint_for(self.whole)

    def minimumSizeHint(self):  # noqa: N802
        return self.hint_for(self.broken())

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self.hint_for(self.whole if width >= self.sizeHint().width() - 2 else self.broken()).height()

    def show_for(self, width):
        text = self.whole if width is None or width >= self.sizeHint().width() - 2 else self.broken()
        if super().text() != text:
            QToolButton.setText(self, text)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.show_for(event.size().width())


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


def together(*widgets, spacing=6):
    """WIDGETS side by side as one item of a flow row: when the row wraps they move to the next row together (Undo
    and Redo, zoom out and in) - never the first at the end of one row and the second at the start of the next."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        if widget is not None:
            layout.addWidget(widget)
    holder.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return holder


def card():
    frame = QFrame()
    frame.setProperty("role", "card")
    return frame


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
        # One row where there is room; when narrow they wrap (Board review 2, R2-19: as one row they set the Board's
        # least width, 1049 px with the properties docked - past a 1024 px screen), "as session" with its name.
        layout = FlowLayout(self, line=True)
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
        named = together(self.as_session, self.name)  # (its field shrinks when even that row is too narrow)
        named.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        for widget in (self.button, self.where, named):
            layout.addWidget(widget)
        self.new_name()

    def new_name(self):
        """A codename for the next session (as the launcher names starts), shown in the empty name field."""
        try:
            name = self.sai.module("codenames").codename() if self.sai is not None else ""
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai without codenames: a name must be typed
            applog.swallowed("widgets", "StartControls.new_name: an older sandbox-ai without codenames: a name …", _exc)
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
        if started and session and not self.name.text().strip() and session == self.suggestion:
            self.new_name()  # the next one gets another (unless the page said so already: used)
        return started

    def used(self, session):
        """A start under SESSION went to its terminal - at once, or after a check or a preparation it had waited for
        (start() saw only "not started yet" then, and the codename stayed for the next start: of another agent too,
        which met the session of this one). The codename is used up: the next start gets another. A typed name stays."""
        if session and session == self.suggestion and not self.name.text().strip():
            self.new_name()

    def take_name(self, name):
        """NAME is the session's name from now on (another name for one that is taken): in the field when a name was
        typed there, else as the codename offered."""
        if self.name.text().strip():
            self.name.setText(name)
            return
        self.suggestion = name
        self.name.setPlaceholderText(name)
        self.name.setToolTip(safe_tooltip(tr("Empty: the session is called {name}", name=name)))


# -- every extra window: an [x] at its top right and Esc to close it -------------------------------------------------

CLOSE_CORNER = 22  # pixels: the [x] button's side
CLOSE_MARGIN = 6  # pixels from the window's top and right edge


# -- a tab's close button, inside its tab --
# Qt puts a tab's close button flush with the tab's right edge (a style sheet's padding and margins do not move it):
# its [x] and its hover background reached onto the tab's border, beyond the tab as drawn. The button sits in a
# holder whose right margin keeps it TAB_CLOSE_GAP pixels inside, in every theme and at every font size.
TAB_CLOSE_GAP = 6
TAB_CLOSE_SIZE = 18  # px of the button (its glyph 12 px): it fits the lowest tab of every theme


def tab_close_button(bar, tooltip=None):
    """A close button for a tab of BAR (a QTabBar): (holder to set with setTabButton, the button). A click asks BAR to
    close the tab the holder is on (tabCloseRequested)."""
    holder = QWidget()
    holder.setObjectName("tabCloseHolder")
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, TAB_CLOSE_GAP, 0)
    row.setSpacing(0)
    close = QToolButton()
    close.setObjectName("tabClose")
    close.setAutoRaise(True)
    close.setFixedSize(QSize(TAB_CLOSE_SIZE, TAB_CLOSE_SIZE))
    close.setIconSize(QSize(12, 12))
    close.setIcon(theme.glyph("quit", theme.colors()["text"], 12))
    close.setToolTip(safe_tooltip(tooltip) if tooltip else tr("Close tab"))
    close.setAccessibleName(tooltip or tr("Close tab"))
    close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    row.addWidget(close)
    holder.close_button = close

    def clicked(_=False):
        for index in range(bar.count()):
            if bar.tabButton(index, bar.ButtonPosition.RightSide) is holder:
                bar.tabCloseRequested.emit(index)
                return
    close.clicked.connect(clicked)
    return holder, close


# -- a tab row's arrows --
# Every tab row of the app is an ArrowTabBar (arrow_tabs, inset_close_tabs, the window's ProjectTabs). When its tabs no
# longer fit, ← → and ▾ appear together at the row's right end: ← and → select the tab before or after the current one
# (held, they repeat; each is off at its end), ▾ lists every tab. Qt's own scroll buttons only scrolled without
# choosing, were drawn by the style (hardly visible in some themes) and came hardly ever: with a middle elide Qt shrank
# every title to a few letters first. Now a title keeps its width (one longer than TAB_TITLE_CHARS characters is
# shortened in the middle, whole on hover), Qt scrolls the row, and the arrows lie over the room Qt keeps at the right
# end for its own buttons (the bar's scroller is as wide as the arrows; Qt's buttons stay below them).
TAB_TITLE_CHARS = 32
TAB_ARROW_MIN = 20  # px: the smallest side of an arrow button


class TabArrows(QWidget):
    """The ← → ▾ of an ArrowTabBar, at its right end while its tabs do not fit."""

    def __init__(self, bar):
        super().__init__(bar)
        self.bar = bar
        self.side = TAB_ARROW_MIN
        self.setObjectName("tabArrows")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 0, 0)
        row.setSpacing(1)
        self.left = self.button("tab-left", tr("Previous tab"), lambda: self.step(-1), repeat=True)
        self.right = self.button("tab-right", tr("Next tab"), lambda: self.step(1), repeat=True)
        self.listed = self.button("tab-list", tr("All tabs"), self.show_menu)
        for button in (self.left, self.right, self.listed):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()
        bar.currentChanged.connect(lambda _index: self.update_state())

    def button(self, glyph_name, tip, action, repeat=False):
        button = QToolButton(self)
        button.setObjectName("tabArrow")
        button.glyph_name = glyph_name
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # (the keyboard has Ctrl+Tab and Ctrl+PgUp/PgDn)
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        if repeat:
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(400)
            button.setAutoRepeatInterval(150)
        button.clicked.connect(lambda _=False: action())
        return button

    def width_needed(self):
        """The arrows' width (even: Qt keeps half the scroller's width per button of its own)."""
        margins = self.layout().contentsMargins()
        width = 3 * self.side + 2 * self.layout().spacing() + margins.left() + margins.right()
        return width + width % 2

    def restyle(self):
        """Icons in the theme's text colour, sized to the bar's font; the bar keeps room for them at its right end."""
        height = self.bar.fontMetrics().height()
        self.side = max(TAB_ARROW_MIN, height + 6)
        icon = max(14, round(height * 0.9))
        color_ = theme.colors()["text"]
        for button in (self.left, self.right, self.listed):
            button.setIcon(theme.glyph(button.glyph_name, color_, icon * 2))  # (sharp on a HiDPI screen too)
            button.setIconSize(QSize(icon, icon))
            button.setFixedSize(self.side, self.side)
        sheet = f"QTabBar::scroller {{ width: {self.width_needed()}px; }}"
        if self.bar.styleSheet() != sheet:
            self.bar.setStyleSheet(sheet)
        self.place()

    def place(self):
        """Shown at the bar's right end while Qt scrolls the tabs (its own scroll buttons are shown), else hidden."""
        bar = self.bar
        overflow = bar.scrolls()
        if overflow:
            width = self.width_needed()
            side = min(self.side, max(TAB_ARROW_MIN, bar.height()))
            for button in (self.left, self.right, self.listed):
                if button.width() != side:
                    button.setFixedSize(side, side)
            self.setGeometry(bar.width() - width, 0, width, bar.height())
            self.raise_()  # (above Qt's buttons and the close buttons of tabs scrolled below it)
        self.setVisible(overflow)
        self.update_state()

    def neighbour(self, step, wrap=False):
        """The index of the next enabled tab STEP (-1 or 1) away from the current one (WRAP: past an end, from the
        other end), or -1."""
        count, current = self.bar.count(), self.bar.currentIndex()
        for distance in range(1, count):
            index = current + step * distance
            if wrap:
                index %= count
            elif not 0 <= index < count:
                return -1
            if self.bar.isTabEnabled(index):
                return index
        return -1

    def update_state(self):
        self.left.setEnabled(self.neighbour(-1) >= 0)
        self.right.setEnabled(self.neighbour(1) >= 0)

    def step(self, step, wrap=False):
        index = self.neighbour(step, wrap)
        if index >= 0:
            self.bar.setCurrentIndex(index)  # (Qt scrolls it into view)

    def menu(self):
        """A menu of every tab (the current one checked); choosing one makes it current."""
        menu = QMenu(self)
        current = self.bar.currentIndex()
        for index in range(self.bar.count()):
            action = menu.addAction(self.bar.tabIcon(index), self.bar.tabText(index))
            action.setCheckable(True)
            action.setChecked(index == current)
            action.setEnabled(self.bar.isTabEnabled(index))
            action.triggered.connect(lambda _=False, i=index: self.bar.setCurrentIndex(i))
        return menu

    def show_menu(self):
        menu = self.menu()
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(self.listed.mapToGlobal(QPoint(0, self.listed.height())))


class ArrowTabBar(QTabBar):
    """The app's tab row: whole titles that scroll, with TabArrows at the right end when they do not fit."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideMiddle)  # (a title longer than TAB_TITLE_CHARS only: tabSizeHint)
        self.qt_scroll = [button for button in self.findChildren(QToolButton)
                          if button.objectName() in ("ScrollLeftButton", "ScrollRightButton")]
        self.arrows = TabArrows(self)
        self.arrows.restyle()

    def scrolls(self):
        """Whether the tabs do not fit (Qt shows its scroll buttons, below the arrows)."""
        if self.qt_scroll:
            return any(not button.isHidden() for button in self.qt_scroll)
        return sum(self.tabSizeHint(i).width() for i in range(self.count())) > self.width()  # (another Qt)

    def title_overflow(self, index):
        """How many pixels the title of tab INDEX is wider than TAB_TITLE_CHARS characters (0: it is shown whole)."""
        text = self.tabText(index).replace("&&", "\0").replace("&", "").replace("\0", "&")
        metrics = self.fontMetrics()
        return max(0, metrics.horizontalAdvance(text) - metrics.averageCharWidth() * TAB_TITLE_CHARS)

    def full_title_tip(self, index):
        """The whole title of tab INDEX when it is shortened, else None."""
        if self.title_overflow(index) <= 0:
            return None
        return self.tabText(index).replace("&&", "\0").replace("&", "").replace("\0", "&")

    def tabSizeHint(self, index):  # noqa: N802
        hint = super().tabSizeHint(index)
        over = self.title_overflow(index)
        if over:
            hint.setWidth(hint.width() - over)
        return hint

    def minimumTabSizeHint(self, index):  # noqa: N802
        return self.tabSizeHint(index)  # (never shrunk further: the row scrolls instead)

    def tabLayoutChange(self):  # noqa: N802
        super().tabLayoutChange()
        self.arrows.place()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.arrows.place()

    def tabInserted(self, index):  # noqa: N802
        super().tabInserted(index)
        self.arrows.update_state()

    def tabRemoved(self, index):  # noqa: N802
        super().tabRemoved(index)
        self.arrows.update_state()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self.arrows.restyle()

    def page_key(self, event):
        """-1 or 1 for Ctrl+PgUp or Ctrl+PgDn (as in browsers and editors), else 0."""
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier != Qt.KeyboardModifier.ControlModifier:
            return 0
        return {Qt.Key.Key_PageUp: -1, Qt.Key.Key_PageDown: 1}.get(event.key(), 0)

    def keyPressEvent(self, event):  # noqa: N802
        step = self.page_key(event)
        if step:
            self.arrows.step(step, wrap=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):  # noqa: N802
        """The tab widget's keys (arrow_tabs): a page's widget left Ctrl+PgUp/PgDn to it."""
        if event.type() == QEvent.Type.KeyPress and self.page_key(event):
            self.keyPressEvent(event)
            return True
        return False

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            index = self.tabAt(event.pos())
            tip = self.full_title_tip(index) if index >= 0 and not self.tabToolTip(index) else None
            if tip:
                from PySide6.QtWidgets import QToolTip
                QToolTip.showText(event.globalPos(), safe_tooltip(tip), self)
                return True
        return super().event(event)


def arrow_tabs(tabs):
    """TABS (a QTabWidget, before its first tab) gets the app's tab row (ArrowTabBar); Ctrl+PgUp and Ctrl+PgDn on its
    pages go through its tabs (Ctrl+Tab is Qt's own)."""
    bar = ArrowTabBar()
    tabs.setTabBar(bar)
    tabs.installEventFilter(bar)
    return bar


class InsetCloseTabBar(ArrowTabBar):
    """An ArrowTabBar whose closable tabs get tab_close_button (inside the tab) instead of Qt's own close button."""

    def tabInserted(self, index):  # noqa: N802
        super().tabInserted(index)
        if self.tabsClosable():
            holder, _close = tab_close_button(self)
            self.setTabButton(index, QTabBar.ButtonPosition.RightSide, holder)

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.PaletteChange:  # a new theme
            self.restyle()

    def restyle(self):
        """The close buttons' glyph in the colors of the theme now."""
        for index in range(self.count()):
            holder = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
            close = getattr(holder, "close_button", None)
            if close is not None:
                close.setIcon(theme.glyph("quit", theme.colors()["text"], 12))


def inset_close_tabs(tabs):
    """TABS (a QTabWidget, before its first tab): closable tabs whose close button lies inside the tab."""
    bar = InsetCloseTabBar()
    tabs.setTabBar(bar)
    return bar


class CloseCorner(QToolButton):
    """The [x] at the top right of an extra window (window_close_controls): it closes the window as its own Close
    or Cancel would - a dialog is rejected, any other window closed (its closeEvent asks about unsaved changes)."""

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("closeCorner")
        self.setText("✕")
        self.setAutoRaise(True)
        hint = self.sizeHint()  # (the theme's padding decides: never smaller than its text needs)
        side = max(CLOSE_CORNER, hint.width(), hint.height())
        self.setFixedSize(side, side)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # (Tab goes through the window's own controls)
        self.setToolTip(tr("Close (Esc)"))
        self.setAccessibleName(tr("Close"))
        self.clicked.connect(lambda: close_window(window))
        self.place()

    def place(self):
        window = self.parentWidget()
        if window is not None:
            self.move(window.width() - self.width() - CLOSE_MARGIN, CLOSE_MARGIN)
            self.raise_()


def close_window(window):
    """Close WINDOW as its own Cancel/Close would: a dialog is rejected (the caller gets Rejected), any other window
    closed - its closeEvent may ask first (an editor with unsaved changes)."""
    if isinstance(window, QDialog):
        window.reject()
    else:
        window.close()


def takes_close_controls(widget, main=None):
    """WIDGET is an extra window of the app that gets an [x] and Esc: a window of its own (a dialog, a message box,
    the log window, an editor, the login browser...) - not the main window MAIN, nor popups, menus, tooltips,
    splash screens, toasts, floating panels (their own title bar closes them) or frameless overlays."""
    from PySide6.QtWidgets import QDockWidget, QMainWindow, QMenu
    if not widget.isWindow() or widget is main or isinstance(widget, (QMenu, QDockWidget)):
        return False
    if isinstance(widget, QMainWindow) and widget.objectName() == "mainWindow":
        return False
    kind = widget.windowType()
    if kind in (Qt.WindowType.Popup, Qt.WindowType.ToolTip, Qt.WindowType.SplashScreen, Qt.WindowType.Desktop,
                Qt.WindowType.SubWindow, Qt.WindowType.Drawer):
        return False
    if widget.windowFlags() & Qt.WindowType.FramelessWindowHint:
        return False
    return not widget.property("noCloseCorner")


class WindowCloseKeys(QObject):
    """The filter of ONE extra window (installed on it with its [x]): keeps the [x] at the top right as the window's
    size changes, and Esc that no control of the window took (a field, a find bar, a menu) closes it - a dialog
    rejects Esc itself."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        window.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is not self.window:
            return False
        kind = event.type()
        if kind in (event.Type.Resize, event.Type.Show):
            if kind == event.Type.Show:
                make_room_for_corner(watched)  # (a window given its [x] before its layout was set)
                if not isinstance(watched, QDialog) and not event.spontaneous():
                    window_rule(watched)  # (a dialog follows dialog_rule as it is shown)
            corner = watched.findChild(CloseCorner, "closeCorner", Qt.FindChildOption.FindDirectChildrenOnly)
            if corner is not None:
                corner.place()
        elif kind == event.Type.KeyPress and event.key() == Qt.Key.Key_Escape \
                and event.modifiers() == Qt.KeyboardModifier.NoModifier and not isinstance(watched, QDialog):
            close_window(watched)
            return True
        return False


def close_controls(window):
    """Give WINDOW (an extra window of the app) its [x] at the top right and Esc to close it, once."""
    try:
        if window.property("closeControls") or not takes_close_controls(window, None):
            return
        window.setProperty("closeControls", True)
        CloseCorner(window).show()
        WindowCloseKeys(window)
        make_room_for_corner(window)
    except RuntimeError:  # (deleted meanwhile)
        pass


def make_room_for_corner(window):
    """Room at the top of WINDOW for its [x], so that it covers none of the window's own controls: the window's top
    margin (a main-window-like one: its contents margins; else its layout's) grows to the [x]'s height."""
    from PySide6.QtWidgets import QMainWindow
    corner = window.findChild(CloseCorner, "closeCorner", Qt.FindChildOption.FindDirectChildrenOnly)
    need = (corner.height() if corner is not None else CLOSE_CORNER) + CLOSE_MARGIN
    if isinstance(window, QMainWindow):
        margins = window.contentsMargins()
        if margins.top() < need:
            window.setContentsMargins(margins.left(), need, margins.right(), margins.bottom())
        return
    layout = window.layout()
    if layout is not None:
        margins = layout.contentsMargins()
        if margins.top() < need:
            layout.setContentsMargins(margins.left(), need, margins.right(), margins.bottom())


def window_close_controls(app=None, main=None):
    """Every dialog and message box gets an [x] at its top right as it is first shown, and closes with Esc (Qt's own
    for a dialog); the app's other windows of their own (the log window, the login browser) call close_controls
    themselves. Some window managers (tiling ones) draw no title bar: the window's own [x] is then the only one. Once
    per process. (Not an application-wide event filter: one in Python crashed the app together with QtWebEngine.)"""
    from PySide6.QtWidgets import QMessageBox as Box
    for cls in (QDialog, Box):
        original = cls.showEvent
        if getattr(original, "closeControls", False):
            continue

        def show_event(dialog, event, original=original):
            if not event.spontaneous():
                close_controls(dialog)
            return original(dialog, event)
        show_event.closeControls = True
        cls.showEvent = show_event



# -- the mouse wheel: a page scrolls, a combo box (spin box, slider) nobody clicked keeps its value --

class WheelGuard(QObject):
    """The user 2026-11-22: "if we come across a dropdown menu the scrolling will directly have affect on the dropdown
    ... a left mouseclick activates it and THEN the scrollwheel is ok to take effects". On each of the app's windows
    (their QWindow, where the platform's input arrives first - one filter per window, never one on the whole
    application: InputWatch says why): a wheel over a combo box, spin box or slider without the focus scrolls the
    scroll area it lies in (or does nothing outside one) unless the human clicked it and it still has the focus - the
    focus alone is not enough: a dialog gives its first field the focus by itself. An open combo box's list scrolls
    as always (it is a window of its own)."""
    def __init__(self, app=None):
        super().__init__()
        self.windows = []
        self.app = app
        self.clicked = None  # the control the human clicked last (a weak reference): only it takes the wheel
        if app is not None:
            app.focusWindowChanged.connect(self.focused)
            for widget in QApplication.topLevelWidgets():
                handle = widget.windowHandle() if widget.isVisible() else None
                if handle is not None:
                    self.watch(handle)

    @staticmethod
    def _controls():
        from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox
        return QComboBox, QAbstractSpinBox, QAbstractSlider

    def watch(self, window):
        """Guard WINDOW (a QWindow of the app's; once). Forgotten when it goes."""
        try:
            if window is None or any(seen is window for seen in self.windows):
                return
            window.installEventFilter(self)
            self.windows.append(window)
            window.destroyed.connect(lambda _=None, w=window: self.forget(w))
        except RuntimeError:  # gone meanwhile
            return

    def forget(self, window):
        self.windows = [seen for seen in self.windows if seen is not window]

    def focused(self, window):
        """WINDOW got the focus: guarded if it is one of the app's own windows (a top-level widget's QWindow) - never
        a window of Chromium's (the terminal tabs): a Python filter there ended the process (InputWatch, the soak)."""
        if window is None:
            return
        own = [widget.windowHandle() for widget in QApplication.topLevelWidgets()
               if widget.windowHandle() is not None]
        if any(handle is window for handle in own):
            self.watch(window)

    def eventFilter(self, window, event):  # noqa: N802
        kind = event.type()
        if kind not in (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress):
            return False
        try:
            if kind == QEvent.Type.MouseButtonPress:  # a click activates the control under it (and nothing else)
                control = self.control_at(window, event)
                self.clicked = weakref.ref(control) if control is not None else None
                return False
            return self.redirect(window, event)
        except RuntimeError:  # a widget gone between the lookup and now
            return False

    def control_at(self, window, event):
        """The combo box, spin box or slider at EVENT's position in WINDOW, or None."""
        control = QApplication.widgetAt(window.mapToGlobal(event.position().toPoint()))
        controls = self._controls()
        while control is not None and not isinstance(control, controls):
            if control.isWindow():
                return None
            control = control.parentWidget()
        return control

    def redirect(self, window, event):
        """True (taken here) when the wheel at EVENT's position is over a control without the focus: then the scroll
        area around it scrolls, as the wheel anywhere else on the page would."""
        control = self.control_at(window, event)
        if control is None:
            return False
        clicked = self.clicked() if self.clicked is not None else None
        if clicked is control and control.hasFocus():  # activated by a click, still the focus: its own wheel
            return False
        if isinstance(control, QComboBox) and control.view().isVisible():
            return False
        area = control.parentWidget()
        from PySide6.QtWidgets import QAbstractScrollArea
        while area is not None and not isinstance(area, QAbstractScrollArea):
            if area.isWindow():
                area = None
                break
            area = area.parentWidget()
        if area is not None:
            self.scroll(area, event)
        return True  # (outside any scroll area: not taken by the control either)

    @staticmethod
    def scroll(area, event):
        """AREA scrolled by EVENT's wheel as Qt scrolls it: pixels from a touchpad, else lines (wheelScrollLines a
        notch, a line being the bar's single step)."""
        pixels, angle = event.pixelDelta(), event.angleDelta()
        horizontal = (angle.x() and not angle.y()) or event.modifiers() & Qt.KeyboardModifier.AltModifier
        bar = area.horizontalScrollBar() if horizontal else area.verticalScrollBar()
        if bar is None:
            return
        if not pixels.isNull():
            delta = pixels.x() if horizontal else pixels.y()
        else:
            notches = (angle.x() if horizontal else angle.y()) / 120
            delta = notches * QApplication.wheelScrollLines() * bar.singleStep()
        bar.setValue(int(bar.value() - delta))


_wheel_guard = []


def wheel_guard(app):
    """The app's one WheelGuard (made at the first call; every window shown or focused later is guarded too)."""
    if not _wheel_guard and app is not None:
        _wheel_guard.append(WheelGuard(app))
    return _wheel_guard[0] if _wheel_guard else None
