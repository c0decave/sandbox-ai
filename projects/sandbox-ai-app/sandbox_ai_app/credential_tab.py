"""One layout for every tab of the Credentials page (agent logins, secrets, Git logins, publish logins, identities,
VPN, and the overview), so that switching tabs moves nothing:

    the header     its title and what it is for - as high as the highest header of the page (even_out: the table
                   of every tab starts at the same height, whatever the length of its text or the language)
    the list       takes the free room
    below it       what a tab adds (the Secrets tab's form: the list is lower there, nothing else moves)
    the buttons    a row of one height, always at the same place
    the message    a message area of a fixed height (MESSAGE_LINES lines; a longer message scrolls), so a message
                   moves nothing either

The same margins and spacing in every tab; no size of a tab depends on its content."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from .widgets import plain_label

MARGINS = (10, 10, 10, 10)
SPACING = 8
MESSAGE_LINES = 2


class MessageArea(QScrollArea):
    """A selectable, wrapping message of a fixed height (MESSAGE_LINES lines): a longer one scrolls instead of
    pushing the buttons above it; text(), setText() and clear() as a label's."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.label = plain_label(selectable=True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.label.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.setWidget(self.label)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.fit()

    def fit(self):
        self.setFixedHeight(self.label.fontMetrics().lineSpacing() * MESSAGE_LINES + 4)

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.fit()

    def text(self):
        return self.label.text()

    def setText(self, text):  # noqa: N802
        self.label.setText(str(text))
        self.verticalScrollBar().setValue(0)

    def clear(self):
        self.label.clear()

    def setWordWrap(self, on):  # noqa: N802 - (it always wraps)
        self.label.setWordWrap(True)

    def setProperty(self, name, value):  # noqa: N802 - a role ("notice-error" ...) is the label's
        self.label.setProperty(name, value)
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)
        return super().setProperty(name, value)


class CredentialTab(QWidget):
    """A tab of the Credentials page: header (title, about, more labels), list, what goes below it, button row,
    message area - always in that order, with the same margins (see the module)."""
    resized = None  # the page's even_out (CredentialsPage.add_tab), told when the tab got another width

    def __init__(self, title="", parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*MARGINS)
        outer.setSpacing(SPACING)
        self.header = QWidget()
        self.header.setObjectName("credentialHeader")
        self.header_layout = QVBoxLayout(self.header)
        self.header_layout.setContentsMargins(0, 0, 0, 0)
        self.header_layout.setSpacing(4)
        self.title_label = plain_label(title, role="section")
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addStretch(1)  # a header made higher by even_out keeps its labels at the top
        self.header.installEventFilter(self)  # a header text that changed (a state read late): evened out again
        outer.addWidget(self.header)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(SPACING)
        outer.addLayout(self.body, 1)
        self.button_bar = QWidget()
        self.button_bar.setObjectName("credentialButtons")
        self.button_row = QHBoxLayout(self.button_bar)
        self.button_row.setContentsMargins(0, 0, 0, 0)
        self.button_row.addStretch(1)
        self.button_bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        outer.addWidget(self.button_bar)
        self.message = MessageArea()
        outer.addWidget(self.message)

    def title(self):
        return self.title_label.text()

    def setTitle(self, text):  # noqa: N802 - (it was a group box's title)
        self.title_label.setText(text)

    def add_header(self, widget):
        """A label (an about text, a state line) under the title, before the stretch."""
        self.header_layout.insertWidget(self.header_layout.count() - 1, widget)
        return widget

    def add_about(self, text):
        return self.add_header(plain_label(text, role="secondary"))

    def set_list(self, widget):
        if not widget.accessibleName():  # (a group box's title named it before)
            widget.setAccessibleName(self.title_label.text())
        self.body.insertWidget(0, widget, 1)
        return widget

    def add_below(self, item):
        """A widget or layout under the list (the list gives it the room)."""
        if isinstance(item, QWidget):
            self.body.addWidget(item)
        else:
            self.body.addLayout(item)
        return item

    def add_buttons(self, *buttons):
        for button in buttons:
            self.button_row.insertWidget(self.button_row.count() - 1, button)

    def header_height(self, width):
        """The height the header needs at WIDTH (its labels wrapped there)."""
        self.ensurePolished()  # (a tab never shown yet: its labels' fonts of the style sheet first)
        layout = self.header_layout
        layout.invalidate()
        return layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()

    def header_width(self):
        left, _top, right, _bottom = MARGINS
        return max(1, self.width() - left - right)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.header and event.type() == QEvent.Type.LayoutRequest and self.resized is not None:
            QTimer.singleShot(0, self.resized)
        return False

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self.resized is not None and event.oldSize().width() != event.size().width():
            self.resized()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if self.resized is not None and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange,
                                                         QEvent.Type.LanguageChange):
            self.resized()


def even_out(tabs):
    """Every tab's header as high as the highest needs at the current tab's width: their lists start at the same
    height (TABS: the CredentialTabs of one QTabWidget, the current one first)."""
    tabs = [tab for tab in tabs if isinstance(tab, CredentialTab)]
    if not tabs:
        return
    width = tabs[0].header_width()
    height = max(tab.header_height(width) for tab in tabs)
    for tab in tabs:
        if tab.header.minimumHeight() != height or tab.header.maximumHeight() != height:
            tab.header.setFixedHeight(height)
