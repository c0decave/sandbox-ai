"""Copy, paste and links in the terminal tabs: the page's second QWebChannel object ("helper") and the bar a tab
asks its questions in.

The page never touches the clipboard itself (JavascriptCanAccessClipboard stays off): it hands the selection to
copy() and asks paste() for text, and Qt's clipboard (QGuiApplication.clipboard()) is read and written here. A paste
is checked first (terminal_text.check_paste): control characters - an escape sequence among them - are always asked
about, several lines when a line break would act as Enter (no bracketed paste) unless Settings say not to ask. A
program's OSC 52 request only ever writes, and only after a yes by default (Settings: terminal_osc52); one to read the
clipboard is never answered. Links: links() finds them in the rows around the mouse (also a URL tmux or Claude Code's
interface drew over several rows), openLink() opens one - a sign-in page of an agent's provider in the app's login
browser (login_browser.py) or the system browser, as Settings say.
"""
from __future__ import annotations

import json
import sys

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QClipboard, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton

from . import terminal_text as T
from .backend import setting
from .i18n import tr

PREVIEW = 160  # characters of a paste or an OSC 52 text shown in a question
LINKS_LIMIT = 64 << 10  # characters of rows one links() request may hand over


def preview(text, limit=PREVIEW):
    """TEXT for a question: control characters visible (ESC as ␛ ...), line breaks as ⏎, cut at LIMIT."""
    shown = []
    for char in str(text)[:limit]:
        if char in "\r\n":
            shown.append("⏎")
        elif char == "\t":
            shown.append("⇥")
        elif char == "\x1b":
            shown.append("␛")
        elif ord(char) < 0x20:
            shown.append(chr(0x2400 + ord(char)))
        elif 0x7f <= ord(char) <= 0x9f:
            shown.append("�")
        else:
            shown.append(char)
    return "".join(shown) + ("…" if len(str(text)) > limit else "")


class TerminalBar(QFrame):
    """A line above a terminal: a sentence and its buttons (a question), or a note that goes by itself. One at a
    time: a new one replaces the one shown (a question replaced counts as Cancel)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("terminalBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(6, 2, 2, 2)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)  # what a program printed is never HTML
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.row.addWidget(self.label, 1)
        self.buttons, self.cancelled = [], None
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setToolTip(tr("Hide this message"))
        self.close_button.clicked.connect(self.dismiss)
        self.row.addWidget(self.close_button)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.dismiss)
        self.hide()

    def clear_buttons(self):
        for widget in self.buttons:
            self.row.removeWidget(widget)
            widget.deleteLater()
        self.buttons = []

    def ask(self, text, choices, cancelled=None, level="warn"):
        """TEXT with CHOICES ([(label, callback)]; the first is the default). CANCELLED(): called when it goes
        without a choice (×, Esc, replaced)."""
        self.finish()
        self.timer.stop()
        self.label.setText(str(text))
        self.setProperty("level", level)
        for index, (label, callback) in enumerate(choices):
            button = QPushButton(str(label))
            button.clicked.connect(lambda _=False, f=callback: self.chosen(f))
            if index == 0:
                button.setDefault(True)
            self.row.insertWidget(self.row.count() - 1, button)
            self.buttons.append(button)
        self.cancelled = cancelled
        self.show()
        if self.buttons:
            self.buttons[0].setFocus()

    def say(self, text, seconds=6, level="info"):
        """A note that goes after SECONDS (0: stays until closed)."""
        self.ask(text, [], level=level)
        if seconds:
            self.timer.start(int(seconds * 1000))

    def chosen(self, callback):
        self.cancelled = None
        self.hide_bar()
        callback()

    def finish(self):
        """The question shown goes unanswered: its CANCELLED is called."""
        cancelled, self.cancelled = self.cancelled, None
        self.clear_buttons()
        if cancelled is not None:
            cancelled()

    def dismiss(self):
        self.finish()
        self.hide_bar()

    def hide_bar(self):
        self.clear_buttons()
        self.label.setText("")
        self.hide()

    def keyPressEvent(self, event):  # noqa: N802 - Qt's name
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def text(self):
        return self.label.text() if not self.isHidden() else ""

    def choices(self):
        return [button.text() for button in self.buttons]

    def choose(self, label):
        """Click the button LABEL (tests, the guided tour)."""
        for button in self.buttons:
            if button.text() == label:
                button.click()
                return True
        return False


def clipboard():
    return QGuiApplication.clipboard()


def put(text, selection=False):
    """TEXT on the clipboard (SELECTION: the primary selection of X11 and Wayland, where there is one)."""
    board = clipboard()
    if selection:
        if board.supportsSelection():
            board.setText(text, QClipboard.Mode.Selection)
        return
    board.setText(text, QClipboard.Mode.Clipboard)


def system_browser(url):
    return QDesktopServices.openUrl(QUrl(url))


class TerminalHelper(QObject):
    """The page's "helper": the clipboard, links and OSC 52 for one terminal tab (VIEW, a terminal.TerminalView)."""
    pasteText = Signal(str)  # noqa: N815 - the page's name for it: text for xterm.js's paste() (bracketed if asked)

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.osc52_allowed = False  # "Always for this tab" was chosen
        self.osc52_pending = None

    # -- copying --

    def selection_text(self, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        if isinstance(data.get("segments"), list):
            segments = [s for s in data["segments"] if isinstance(s, dict)][:20000]
            return T.joined_selection(segments, int(data.get("cols") or 0))
        return str(data.get("text") or "")

    @Slot(str)
    def copy(self, payload):
        """Ctrl+Shift+C, Cmd+C, Ctrl+Insert or Copy in the menu: the selection, onto the clipboard."""
        text = self.selection_text(payload)
        if not text:
            return
        put(text)
        put(text, selection=True)
        self.view.status(tr("Copied {count} characters", count=len(text)))

    @Slot(str)
    def selected(self, payload):
        """The selection changed: the primary selection gets it (Linux), and the clipboard too when Settings say
        to copy what is selected."""
        text = self.selection_text(payload)
        if not text:
            return
        put(text, selection=True)
        if setting("copy_on_select"):
            put(text)

    def copy_text(self, text):
        put(text)
        put(text, selection=True)
        self.view.status(tr("Copied {count} characters", count=len(text)))

    # -- pasting --

    @Slot(str, bool)
    def paste(self, source, bracketed):
        """The page wants to paste (Ctrl+Shift+V, Cmd+V, Shift+Insert, a middle click, the menu): SOURCE "clipboard"
        or "selection"; BRACKETED: the program asked for bracketed paste (then a line break is no Enter)."""
        board = clipboard()
        if source == "selection":
            if not board.supportsSelection():
                return
            text = board.text(QClipboard.Mode.Selection)
        else:
            text = board.text(QClipboard.Mode.Clipboard)
        if text:
            self.offer(text, bool(bracketed))

    def offer(self, text, bracketed):
        """Paste TEXT, after a question when check_paste finds something to ask about."""
        check = T.check_paste(text, bracketed)
        if check.too_large:
            self.view.bar.say(tr("Not pasted: the text is larger than {limit} characters.", limit=T.PASTE_LIMIT),
                              level="warn")
            return
        if not check.controls and (not check.runs or not setting("paste_warn")):
            self.send(text)
            return
        lines = [tr("Paste into the terminal: “{text}”", text=preview(text))]
        if check.controls:
            lines.append(tr("It holds control characters ({names}): an escape sequence could make the program do "
                            "something other than what you see.", names=", ".join(check.controls)))
        if check.runs:
            lines.append(tr("It has {count} lines, and the program has not asked for bracketed paste: each line "
                            "break acts as Enter, a shell runs each line at once.", count=check.lines))
        clean = T.without_controls(text)
        choices = []
        if check.controls:
            choices.append((tr("Paste without them"), lambda: self.send(clean)))
            if check.runs:
                choices.append((tr("As one line, without them"), lambda: self.send(T.one_line(clean))))
        else:
            choices.append((tr("Paste"), lambda: self.send(text)))
            choices.append((tr("Paste as one line"), lambda: self.send(T.one_line(text))))
        choices.append((tr("Cancel"), lambda: None))
        self.view.bar.ask(" ".join(lines), choices)

    def send(self, text):
        if text:
            self.pasteText.emit(text)
            self.view.focus_terminal()

    # -- links --

    @Slot(str, result=str)
    def links(self, payload):
        """The links in the rows the page hands over ({"first", "cols", "rows", "wrapped", "target"}) that reach
        row TARGET: [{"url", "start": [row, column], "end": [row, column]}] as JSON."""
        if len(payload) > LINKS_LIMIT:
            return "[]"
        try:
            data = json.loads(payload)
            rows = [str(r) for r in data["rows"]][:200]
            wrapped = [bool(w) for w in data.get("wrapped", [])][:200]
            cols, first, target = int(data["cols"]), int(data["first"]), int(data["target"])
        except (ValueError, KeyError, TypeError):
            return "[]"
        found = T.links_in_rows(rows, wrapped, max(1, min(cols, 1000)), first=first, touching=target)
        return json.dumps([{"url": link.url, "start": list(link.start), "end": list(link.end)} for link in found])

    @Slot(str)
    def openLink(self, url):  # noqa: N802 - the page's name
        """A Ctrl+click (Cmd+click) on a link."""
        self.view.open_link(url)

    # -- OSC 52 --

    @Slot(str)
    def osc52(self, payload):
        """A program asks to set (or read) the clipboard with OSC 52: a read is never answered; a write is done
        after a yes (Settings: ask, the default), at once (allow, said in the status bar) or never (off)."""
        from . import applog
        request = T.parse_osc52(payload)
        policy = setting("terminal_osc52")
        if request.kind == "read":
            applog.log("WARNING", "terminal", f"{self.view.title()}: a program asked to read the clipboard (OSC 52); "
                                              "not answered")
            return
        if request.kind != "set" or not request.text or policy == "off":
            return
        text = request.text
        if policy == "allow" or self.osc52_allowed:
            self.copy_text(text)
            self.view.status(tr("The program in {tab} copied {count} characters to the clipboard",
                                tab=self.view.title(), count=len(text)))
            return

        def always():
            self.osc52_allowed = True
            self.copy_text(text)
        self.view.bar.ask(tr("The program in this tab wants to put {count} characters on the clipboard: “{text}”",
                             count=len(text), text=preview(text)),
                          [(tr("Copy to the clipboard"), lambda: self.copy_text(text)),
                           (tr("Always for this tab"), always), (tr("Ignore"), lambda: None)], level="info")


def shortcut_names(platform=None):
    """The copy and paste keys, as the context menu shows them."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return "⌘C", "⌘V"
    return "Ctrl+Shift+C", "Ctrl+Shift+V"
