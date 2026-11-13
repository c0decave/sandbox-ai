"""The Files page's viewer and editor: a file opened by a double click (Enter, F3 view, F4 edit) shows in a window of its
own, as text when it is text (valid UTF-8 without NUL bytes, up to files.TEXT_LIMIT), else in a hex editor (up to
files.EDIT_LIMIT; a bigger file shows its first part, read-only).

The file is read and written through its pane's files.Root (Root.read_file, Root.write_file), as every other
operation of the file manager: relative to open directories, never through a symbolic link, a FIFO or a device, and
the file is never run. Saving writes a temporary file beside it and renames it over the old one; when the file changed
on disk since it was opened (the agent wrote it meanwhile), saving asks before it replaces it. Text that cannot be
written back exactly as it was read (mixed line endings, a paragraph separator) opens read-only: the hex editor
changes it byte by byte. Invisible and direction-changing characters (Trojan Source) are named above the text.

The buttons read Save (Ctrl+S), Reload and Cancel: Cancel (Escape, Ctrl+W - macOS: Cmd+W) closes the window without
saving, asking first about changes not saved (the "editor-discard" question); a file only viewed has Close. The find bar (FindBar: Ctrl+F;
replace Ctrl+H, on macOS Cmd+Option+F) finds and replaces in the text view and finds text or hex bytes in the hex
view; Escape closes it before it cancels the window.

A Markdown file (markdown_view.is_markdown) opens rendered (markdown_view.MarkdownView: nothing in it runs or is
loaded from the network, images come through the pane's Root); Edit shows its text, Markdown the rendering again,
and Preview the rendering beside the text as it is typed. A link clicked there is never followed in the window:
open_link asks before a web link goes to the browser and opens a Markdown file of the same root in a viewer of
its own; anything else is named, not opened.

The hex editor's layout - offset, hex and ASCII columns painted line by line, typing over a byte nibble by nibble, Tab
between the two columns - follows the idea of axross's ui/hex_editor.py (github.com/c0decave/axross, Apache-2.0);
the code here is this app's own.
"""
from __future__ import annotations

import bisect
import posixpath
import re
import sys

from PySide6.QtCore import QEvent, QRect, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFontMetrics, QKeySequence, QPainter, QPalette, QShortcut, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea, QCheckBox, QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from . import files, theme
from .files import printable
from .i18n import tr
from .markdown_view import ImageSource, MarkdownView, is_markdown
from .widgets import MONO, ask_first, background, button, confirm, plain_label, safe_tooltip, set_enabled

BYTES_PER_LINE = 16
MATCH_LIMIT = 1000  # matches the find bar counts ("1000+" beyond)
# Replace: Ctrl+H; on macOS Cmd+H hides the app (the system's), so there Cmd+Option+F, as in its editors.
REPLACE_KEY = "Ctrl+Alt+F" if sys.platform == "darwin" else "Ctrl+H"
# Characters that do not show or turn the direction of what follows (zero width, bidi controls, a BOM in the middle):
# what is shown may then differ from what a compiler or a shell reads.
INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u061c\ufeff]")
CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")


def split_text(text):
    """(text for the editor, BOM, line ending) of the decoded file TEXT, or None when the editor could not give it
    back exactly (mixed line endings, a lone CR, a paragraph or line separator, U+FDD0 or U+FDD1)."""
    bom = text.startswith("\ufeff")
    if bom:
        text = text[1:]
    crlf = text.count("\r\n")
    if crlf and crlf == text.count("\n") == text.count("\r"):
        text, newline = text.replace("\r\n", "\n"), "\r\n"
    elif "\r" in text:
        return None
    else:
        newline = "\n"
    # (the editor's own paragraph separator, and the line separator Shift+Enter types: both come back as a line break;
    # so do U+FDD0 and U+FDD1, with which Qt's text document marks the beginning and the end of a frame)
    if any(mark in text for mark in ("\u2029", "\u2028", "\ufdd0", "\ufdd1")):
        return None
    return text, bom, newline


def join_text(text, bom, newline):
    """The bytes of the editor's TEXT as the file had them (its BOM and line ending)."""
    if newline != "\n":
        text = text.replace("\n", newline)
    return (("\ufeff" if bom else "") + text).encode("utf-8")


def text_warning(text):
    """What the human should know about TEXT before trusting what is shown, or ""."""
    found = INVISIBLE.search(text) or CONTROL.search(text)
    if not found:
        return ""
    return tr("Careful: this text holds invisible, direction-changing or control characters (the first: U+{code} "
              "at character {at}); what is shown may differ from what tools read. The hex editor shows every byte.",
              code=f"{ord(found.group()):04X}", at=found.start() + 1)


def editor_text(edit):
    """The text of the QPlainTextEdit EDIT as typed (toPlainText would turn non-breaking spaces into spaces). Shift+Enter
    types a line separator (U+2028): shown as a line break, so saved as one (the file got the bytes E2 80 A8 and one
    long line; a file that holds one is never opened as text, split_text)."""
    return edit.document().toRawText().replace("\u2029", "\n").replace("\u2028", "\n")


def changed_bytes(data, original, block=4096):
    """The offsets where DATA differs from ORIGINAL (as far as both go). Compared a BLOCK at a time, byte by byte only
    in the blocks that differ: a few changes in a big file are found at once (byte by byte in Python, 4 MiB took
    150 ms)."""
    end = min(len(data), len(original))
    found = set()
    for start in range(0, end, block):
        stop = min(start + block, end)
        if data[start:stop] != original[start:stop]:
            found.update(at for at in range(start, stop) if data[at] != original[at])
    return found


def hex_needle(text):
    """The bytes typed as hex digits in TEXT ("7f 45 4c 46", "7f454c46"; spaces, colons and commas between bytes are
    ignored), or None when it is not two hex digits per byte."""
    digits = re.sub(r"[\s:,]", "", text)
    if not digits or len(digits) % 2 or not re.fullmatch("[0-9A-Fa-f]+", digits):
        return None
    return bytes.fromhex(digits)


def byte_matches(data, needle, case=True, limit=None):
    """The offsets where NEEDLE starts in DATA (not overlapping), at most LIMIT of them. CASE False: ASCII letters match
    either case (one byte per character: the hex editor's ASCII column)."""
    if not needle:
        return []
    if not case:
        data, needle = bytes(data).lower(), needle.lower()
    found, at = [], data.find(needle)
    while at >= 0 and (limit is None or len(found) < limit):
        found.append(at)
        at = data.find(needle, at + len(needle))
    return found


class HexView(QAbstractScrollArea):
    """Bytes as offset | hex | ASCII, 16 a line. Typing a hex digit (in the hex column) or a character (in the ASCII
    column) changes the byte under the cursor in place - the size never changes; Tab switches the columns. Changed
    bytes are marked. READ_ONLY: moves only."""
    changed = Signal()
    moved = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.buffer, self.edited = bytearray(), set()
        self.read_only, self.cursor, self.low, self.in_ascii = True, 0, False, False
        self.found = None  # (offset, length) of the match the find bar shows, marked
        self.setFont(MONO)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr("Bytes of the file"))
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.fit_font()

    # -- contents --

    def set_data(self, data, read_only=False):
        self.buffer, self.edited, self.found = bytearray(data), set(), None
        self.read_only, self.cursor, self.low = read_only, 0, False
        self.verticalScrollBar().setValue(0)
        self.update_scroll()
        self.viewport().update()

    def data(self):
        return bytes(self.buffer)

    @property
    def modified(self):
        return bool(self.edited)

    def set_byte(self, offset, value):
        """Byte OFFSET becomes VALUE (in place); False when read-only or there is no such byte."""
        if self.read_only or not 0 <= offset < len(self.buffer):
            return False
        if self.buffer[offset] != value:
            self.buffer[offset] = value
            self.edited.add(offset)
            self.changed.emit()
        self.viewport().update()
        return True

    # -- geometry --

    def fit_font(self):
        metrics = QFontMetrics(self.font())
        self.cw, self.ch, self.ascent = max(1, metrics.horizontalAdvance("0")), max(1, metrics.height()), \
            metrics.ascent()
        self.hex_x = 10 * self.cw  # "00000000  "
        self.ascii_x = self.hex_x + (BYTES_PER_LINE * 3 + 2) * self.cw
        self.line_width = self.ascii_x + (BYTES_PER_LINE + 1) * self.cw
        self.update_scroll()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.FontChange:
            self.fit_font()

    def lines(self):
        return max(1, (len(self.buffer) + BYTES_PER_LINE - 1) // BYTES_PER_LINE)

    def visible(self):
        return max(1, self.viewport().height() // self.ch)

    def update_scroll(self):
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, self.lines() - self.visible()))
        bar.setPageStep(self.visible())
        across = self.horizontalScrollBar()
        across.setRange(0, max(0, self.line_width - self.viewport().width()))
        across.setPageStep(max(1, self.viewport().width()))
        across.setSingleStep(self.cw)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.update_scroll()

    def scrollContentsBy(self, dx, dy):  # noqa: N802
        self.viewport().update()

    def hex_column(self, column):
        """x of the hex pair of COLUMN (a gap after the eighth byte)."""
        return self.hex_x + (column * 3 + (1 if column >= BYTES_PER_LINE // 2 else 0)) * self.cw

    def offset_at(self, point):
        """(offset, in the ASCII column) of the byte at POINT of the viewport, or None."""
        if not self.buffer:
            return None
        x = point.x() + self.horizontalScrollBar().value()
        line = self.verticalScrollBar().value() + point.y() // self.ch
        if self.hex_x <= x < self.ascii_x - self.cw:
            inner = (x - self.hex_x) // self.cw
            if inner >= BYTES_PER_LINE // 2 * 3:
                inner -= 1
            column, ascii_side = inner // 3, False
        elif self.ascii_x <= x < self.ascii_x + BYTES_PER_LINE * self.cw:
            column, ascii_side = (x - self.ascii_x) // self.cw, True
        else:
            return None
        column = max(0, min(BYTES_PER_LINE - 1, column))
        return min(len(self.buffer) - 1, max(0, line * BYTES_PER_LINE + column)), ascii_side

    # -- painting --

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self.viewport())
        palette = self.palette()
        painter.fillRect(event.rect(), palette.color(QPalette.ColorRole.Base))
        painter.setFont(self.font())
        text, muted = palette.color(QPalette.ColorRole.Text), QColor(theme.color("muted"))
        edited, mark = QColor(theme.color("bad")), palette.color(QPalette.ColorRole.Highlight)
        marked = palette.color(QPalette.ColorRole.HighlightedText)
        match = QColor(mark)
        match.setAlpha(80)
        found_from, found_to = (self.found[0], self.found[0] + self.found[1]) if self.found else (0, 0)
        first, left = self.verticalScrollBar().value(), -self.horizontalScrollBar().value()
        for line in range(first, min(self.lines(), first + self.visible() + 1)):
            top = (line - first) * self.ch
            base = top + self.ascent
            start = line * BYTES_PER_LINE
            painter.setPen(muted)
            painter.drawText(left, base, f"{start:08x}")
            for column, value in enumerate(self.buffer[start:start + BYTES_PER_LINE]):
                at = start + column
                hex_x, ascii_x = left + self.hex_column(column), left + self.ascii_x + column * self.cw
                if found_from <= at < found_to:  # the find bar's match
                    painter.fillRect(QRect(hex_x, top, 2 * self.cw, self.ch), match)
                    painter.fillRect(QRect(ascii_x, top, self.cw, self.ch), match)
                color = edited if at in self.edited else text
                if at == self.cursor:
                    full = QRect(hex_x, top, 2 * self.cw, self.ch)
                    single = QRect(ascii_x, top, self.cw, self.ch)
                    active, other = (single, full) if self.in_ascii else (full, single)
                    painter.fillRect(active, mark)
                    painter.setPen(mark)
                    painter.drawRect(other.adjusted(0, 0, -1, -1))
                painter.setPen(marked if at == self.cursor and not self.in_ascii else color)
                painter.drawText(hex_x, base, f"{value:02x}")
                painter.setPen(marked if at == self.cursor and self.in_ascii else color)
                painter.drawText(ascii_x, base, chr(value) if 32 <= value < 127 else ".")
        painter.end()

    # -- moving and typing --

    def go(self, offset, low=False):
        if not self.buffer:
            return
        self.cursor, self.low = max(0, min(len(self.buffer) - 1, offset)), low
        line, bar = self.cursor // BYTES_PER_LINE, self.verticalScrollBar()
        if line < bar.value():
            bar.setValue(line)
        elif line >= bar.value() + self.visible():
            bar.setValue(line - self.visible() + 1)
        self.moved.emit(self.cursor)
        self.viewport().update()

    def focusNextPrevChild(self, forward):  # noqa: N802
        return False if forward else super().focusNextPrevChild(forward)  # Tab: the other column (keyPressEvent)

    def keyPressEvent(self, event):  # noqa: N802
        key, modifiers, Key = event.key(), event.modifiers(), Qt.Key
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        page = self.visible() * BYTES_PER_LINE
        moves = {Key.Key_Left: -1, Key.Key_Right: 1, Key.Key_Up: -BYTES_PER_LINE, Key.Key_Down: BYTES_PER_LINE,
                 Key.Key_PageUp: -page, Key.Key_PageDown: page}
        if key == Key.Key_Tab:
            self.in_ascii, self.low = not self.in_ascii, False
            self.viewport().update()
        elif key in moves:
            self.go(self.cursor + moves[key])
        elif key == Key.Key_Home:
            self.go(0 if control else self.cursor - self.cursor % BYTES_PER_LINE)
        elif key == Key.Key_End:
            self.go(len(self.buffer) - 1 if control else self.cursor - self.cursor % BYTES_PER_LINE + BYTES_PER_LINE - 1)
        elif not control and self.type_text(event.text()):
            pass
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def type_text(self, typed):
        """TYPED (a key's text) over the byte at the cursor; True if it changed (or was refused as) a byte."""
        if len(typed) != 1 or not typed.isprintable() or not self.buffer:
            return False
        if self.read_only:
            return True
        old = self.buffer[self.cursor]
        if self.in_ascii:
            if ord(typed) > 126:
                return True  # one byte per character: only ASCII here
            self.set_byte(self.cursor, ord(typed))
            self.go(self.cursor + 1)
            return True
        if typed.lower() not in "0123456789abcdef":
            return True
        digit = int(typed, 16)
        if self.low:
            self.set_byte(self.cursor, (old & 0xF0) | digit)
            self.go(self.cursor + 1) if self.cursor + 1 < len(self.buffer) else self.go(self.cursor)
        else:
            self.set_byte(self.cursor, (digit << 4) | (old & 0x0F))
            self.go(self.cursor, low=True)
        return True

    def mousePressEvent(self, event):  # noqa: N802
        found = self.offset_at(event.position().toPoint())
        if found is not None and event.button() == Qt.MouseButton.LeftButton:
            self.in_ascii = found[1]
            self.go(found[0])
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()


def key_text(key):
    """KEY (a standard key or "Ctrl+H") as this system writes it (macOS: ⌘F)."""
    return QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText)


class FindBar(QWidget):
    """Find (Ctrl+F) and replace (REPLACE_KEY) in an EditorWindow: Next (Enter, F3) and Previous (Shift+Enter,
    Shift+F3) go round the end of the file, Match case, how many there are (counted up to MATCH_LIMIT). In the text view
    Replace replaces the match shown and goes on to the next one, Replace all every one (one step to undo); in the hex
    view it finds the text typed (UTF-8) or, with Hex bytes, the bytes typed as hex digits - the hex editor never
    changes a file's size, so nothing is replaced there. Escape closes the bar."""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        first, second = QHBoxLayout(), QHBoxLayout()
        self.find = QLineEdit()
        self.find.setPlaceholderText(tr("Find"))
        self.find.setAccessibleName(tr("Find"))
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda _text: self.search(incremental=True))
        self.find.installEventFilter(self)
        self.previous_button = button(tr("Previous"), tr("The match before (Shift+Enter, Shift+F3)"),
                                      lambda: self.search(backward=True))
        self.next_button = button(tr("Next"), tr("The next match (Enter, F3)"), lambda: self.search())
        self.case = QCheckBox(tr("Match case"))
        self.case.toggled.connect(lambda _on: self.search(incremental=True))
        self.hex_bytes = QCheckBox(tr("Hex bytes"))
        self.hex_bytes.setToolTip(tr("Find the bytes typed as hex digits (e.g. 7f 45 4c 46); off: the text typed, as "
                                     "UTF-8"))
        self.hex_bytes.toggled.connect(lambda _on: self.search(incremental=True))
        self.replace_toggle = QCheckBox(tr("Replace"))
        self.replace_toggle.setToolTip(safe_tooltip(tr("Show the fields to replace what is found ({shortcut})",
                                                       shortcut=key_text(REPLACE_KEY))))
        self.replace_toggle.toggled.connect(self.show_replace)
        self.status = plain_label(role="secondary", word_wrap=False)
        self.close_button = button(tr("Close"), tr("Close the find bar (Escape)"), lambda: self.close_bar())
        for widget in (self.find, self.previous_button, self.next_button, self.case, self.hex_bytes,
                       self.replace_toggle, self.status, self.close_button):
            first.addWidget(widget, 1 if widget is self.find else 0)
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText(tr("Replace with"))
        self.replacement.setAccessibleName(tr("Replace with"))
        self.replacement.returnPressed.connect(lambda: self.replace())
        self.replace_button = button(tr("Replace"), tr("Replace the match shown and find the next one"),
                                     lambda: self.replace())
        self.replace_all_button = button(tr("Replace all"), tr("Replace every match (one step to undo)"),
                                         lambda: self.replace_all())
        self.replace_row = QWidget()
        second.setContentsMargins(0, 0, 0, 0)
        self.replace_row.setLayout(second)
        for widget in (self.replacement, self.replace_button, self.replace_all_button):
            second.addWidget(widget, 1 if widget is self.replacement else 0)
        layout.addLayout(first)
        layout.addWidget(self.replace_row)
        self.replace_row.hide()
        self.hide()

    # -- shown, hidden --

    def open(self, replace=False):
        """Show the bar (REPLACE: with the replace fields) and put the keyboard in the find field; a selection of one
        line in the text becomes what is found."""
        editor = self.editor
        if editor.view in ("text", "markdown"):
            selected = self.edit().textCursor().selectedText()
            if selected and "\u2029" not in selected and "\u2028" not in selected:  # (one line)
                self.find.blockSignals(True)
                self.find.setText(selected)
                self.find.blockSignals(False)
        if replace:
            self.replace_toggle.setChecked(True)
        self.fit_view()
        self.show()
        self.find.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.find.selectAll()
        self.search(incremental=True)

    def close_bar(self):
        """Hide the bar; the keyboard goes back to the file."""
        self.hide()
        self.editor.hex.found = None
        self.editor.hex.viewport().update()
        self.editor.text.setExtraSelections([])
        self.editor.markdown.browser.setExtraSelections([])
        (self.edit() if self.editor.view != "hex" else self.editor.hex).setFocus(Qt.FocusReason.OtherFocusReason)

    def edit(self):
        """The text widget searched: the rendering in the Markdown view, else the text."""
        return self.editor.markdown.browser if self.editor.view == "markdown" else self.editor.text

    def show_replace(self, on):
        self.replace_row.setVisible(bool(on) and self.editor.view == "text")

    def fit_view(self):
        """What the view shown offers: hex bytes in the hex view, replacing in the text view (unless read-only)."""
        text = self.editor.view != "hex"
        self.hex_bytes.setVisible(not text)
        if self.editor.view == "markdown":
            set_enabled(self.replace_toggle, False, tr("Replace in the text: Edit."))
        else:
            set_enabled(self.replace_toggle, text, tr("The hex editor never changes a file's size: replace in the "
                                                      "text view."))
        self.show_replace(self.replace_toggle.isChecked())
        writable = self.editor.view in ("text", None) and not self.editor.text.isReadOnly()
        for widget in (self.replace_button, self.replace_all_button, self.replacement):
            set_enabled(widget, writable, tr("Read-only: nothing is replaced."))
        set_enabled(self.case, not (not text and self.hex_bytes.isChecked()))

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.find and event.type() == QEvent.Type.KeyPress and \
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.search(backward=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            return True
        return super().eventFilter(watched, event)

    # -- finding --

    def flags(self, backward=False):
        flags = QTextDocument.FindFlag(0)
        if self.case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def say(self, text):
        self.status.setText(text)

    def search(self, backward=False, incremental=False):
        """Find the next match (BACKWARD: the one before; INCREMENTAL: the find field changed, so the match shown may
        stay) and show it; True if there is one."""
        if self.isHidden() and not incremental:  # (F3 with the bar closed: it opens, then finds)
            self.open()
            return self.search(backward)
        if self.editor.view == "hex":
            return self.search_bytes(backward, incremental)
        if self.editor.view not in ("text", "markdown"):
            return False
        edit, needle = self.edit(), self.find.text()
        if not needle:
            edit.setExtraSelections([])
            self.say("")
            return False
        document, cursor = edit.document(), edit.textCursor()
        start = QTextCursor(cursor)
        if incremental:  # (typing on: the match grows where it is)
            start.setPosition(cursor.selectionStart())
        elif backward:
            start.setPosition(cursor.selectionStart())
        else:
            start.setPosition(cursor.selectionEnd())
        found = document.find(needle, start, self.flags(backward))
        if found.isNull():  # round the end
            around = QTextCursor(document)
            around.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
            found = document.find(needle, around, self.flags(backward))
        if found.isNull():
            edit.setExtraSelections([])
            self.say(tr("No match"))
            return False
        edit.setTextCursor(found)
        edit.ensureCursorVisible()
        mark = QTextEdit.ExtraSelection()
        mark.cursor = found
        mark.format.setBackground(edit.palette().color(QPalette.ColorRole.Highlight))
        mark.format.setForeground(edit.palette().color(QPalette.ColorRole.HighlightedText))
        edit.setExtraSelections([mark])
        starts = self.text_matches(needle)
        self.say_count(bisect.bisect_left(starts, found.selectionStart()), starts, found.selectionStart())
        return True

    def text_matches(self, needle):
        """Where the matches of NEEDLE start in the text (at most MATCH_LIMIT of them)."""
        document, starts = self.edit().document(), []
        found = document.find(needle, 0, self.flags())
        while not found.isNull() and len(starts) < MATCH_LIMIT:
            starts.append(found.selectionStart())
            found = document.find(needle, found.selectionEnd(), self.flags())
        return starts

    def say_count(self, index, starts, at):
        """"3 of 12" (index of the match at AT among STARTS), or how many there are when it is not among them."""
        count = f"{len(starts)}+" if len(starts) >= MATCH_LIMIT else str(len(starts))
        if index < len(starts) and starts[index] == at:
            self.say(tr("{index} of {count}", index=index + 1, count=count))
        else:
            self.say(tr("{count} matches", count=count))

    def needle_bytes(self):
        """The bytes to find in the hex view, or None (said) when the hex digits typed are none."""
        text = self.find.text()
        if not self.hex_bytes.isChecked():
            return text.encode("utf-8")
        needle = hex_needle(text)
        if needle is None:
            self.say(tr("Not hex bytes: two hex digits per byte (e.g. 7f 45 4c 46)"))
        return needle

    def search_bytes(self, backward=False, incremental=False):
        self.fit_view()
        view = self.editor.hex
        needle = self.needle_bytes() if self.find.text() else b""
        if not needle:
            view.found = None
            view.viewport().update()
            if not self.find.text():
                self.say("")
            return False
        case = self.case.isChecked() or self.hex_bytes.isChecked()
        data = bytes(view.buffer) if case else bytes(view.buffer).lower()
        needle = needle if case else needle.lower()
        here = view.cursor
        if backward:
            at = data.rfind(needle, 0, here - 1 + len(needle)) if here > 0 else -1
            at = at if at >= 0 else data.rfind(needle)
        else:
            begin = here + 1 if not incremental and view.found is not None and view.found[0] == here else here
            at = data.find(needle, begin)
            at = at if at >= 0 else data.find(needle)
        if at < 0:
            view.found = None
            view.viewport().update()
            self.say(tr("No match"))
            return False
        view.found = (at, len(needle))
        view.go(at)
        view.viewport().update()
        starts = byte_matches(data, needle, True, MATCH_LIMIT)
        self.say_count(bisect.bisect_left(starts, at), starts, at)
        return True

    # -- replacing (the text view) --

    def can_replace(self):
        return self.editor.view == "text" and not self.editor.text.isReadOnly() and bool(self.find.text())

    def replace(self):
        """Replace the match shown (if the selection is one) and find the next one."""
        if not self.can_replace():
            return False
        edit, needle = self.editor.text, self.find.text()
        cursor = edit.textCursor()
        if cursor.hasSelection():
            same = edit.document().find(needle, cursor.selectionStart(), self.flags())
            if not same.isNull() and (same.selectionStart(), same.selectionEnd()) == \
                    (cursor.selectionStart(), cursor.selectionEnd()):
                cursor.insertText(self.replacement.text())
                edit.setTextCursor(cursor)
        return self.search()

    def replace_all(self):
        """Replace every match, as one step to undo; returns how many."""
        if not self.can_replace():
            return 0
        edit, needle, new = self.editor.text, self.find.text(), self.replacement.text()
        document, count = edit.document(), 0
        block = QTextCursor(document)
        block.beginEditBlock()
        try:
            found = document.find(needle, 0, self.flags())
            while not found.isNull():
                found.insertText(new)
                count += 1
                found = document.find(needle, found.position(), self.flags())
        finally:
            block.endEditBlock()
        edit.setExtraSelections([])
        self.say(tr("Replaced {count}", count=count) if count else tr("No match"))
        return count


def open_url(url):
    """URL (http or https, the human said yes) in the system's browser."""
    QDesktopServices.openUrl(url)


class EditorWindow(QDialog):
    """One file of a pane in a window of its own: text, hex or rendered Markdown (MODE "auto", "text", "hex",
    "markdown"), editable unless READ_ONLY (F3: a viewer). SAVED() after it was saved (the page lists its panes again);
    OPEN_FILE(relative, shown): a Markdown file of the same root a link of this one leads to, in a viewer of its own."""

    def __init__(self, parent, root, relative, shown, mode="auto", read_only=False, saved=None, open_file=None, top=""):
        super().__init__(parent)
        self.root, self.relative, self.shown, self.mode = root, relative, shown, mode
        self.top = "/".join(part for part in top.split("/") if part)  # (images and Markdown links: below it only)
        self.viewer, self.read_only, self.on_saved, self.on_open_file = read_only, read_only, saved, open_file
        self.markdown_like = is_markdown(relative) or mode == "markdown"
        self.decoded = False  # the file is text (UTF-8): it can be shown as Markdown
        self.original, self.identity, self.bom, self.newline, self.view = b"", None, False, "\n", None
        self.loading, self.saving, self.truncated, self.dirty, self.text_ok = False, False, False, False, False
        self.name = relative.rsplit("/", 1)[-1]
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        self.where = plain_label(printable(shown), role="secondary")
        self.where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.where)
        self.notice = plain_label(selectable=True)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.stack = QStackedWidget()
        self.text = QPlainTextEdit()
        self.text.setFont(MONO)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setAccessibleName(tr("Text of the file"))
        self.text.setTabStopDistance(QFontMetrics(MONO).horizontalAdvance(" ") * 4)
        self.text.document().modificationChanged.connect(lambda changed: changed and self.mark_dirty())
        self.text.cursorPositionChanged.connect(self.show_position)
        self.hex = HexView()
        self.hex.changed.connect(self.mark_dirty)
        self.hex.moved.connect(lambda _offset: self.show_position())
        # The text, with the live preview of Markdown beside it (Preview); the rendered view (Markdown: the outline,
        # the rendering); both through markdown_view, never loading anything themselves.
        source = ImageSource(root, relative, self.top)
        self.preview = MarkdownView(source, outline=False)
        self.preview.hide()
        self.preview.link.connect(self.open_link)
        self.text_page = QSplitter(Qt.Orientation.Horizontal)
        self.text_page.addWidget(self.text)
        self.text_page.addWidget(self.preview)
        self.text.textChanged.connect(self.follow_typing)
        self.markdown = MarkdownView(source)
        self.markdown.link.connect(self.open_link)
        self.stack.addWidget(self.text_page)
        self.stack.addWidget(self.hex)
        self.stack.addWidget(self.markdown)
        layout.addWidget(self.stack, 1)
        self.find_bar = FindBar(self)
        layout.addWidget(self.find_bar)
        bottom = QHBoxLayout()
        self.as_text = button(tr("Text"), tr("Show the file as text (UTF-8)"), lambda: self.switch("text"))
        self.as_hex = button(tr("Hex"), tr("Show the file's bytes (hex editor)"), lambda: self.switch("hex"))
        self.as_markdown = button(tr("Markdown"), tr("Show the file rendered as Markdown"),
                                  lambda: self.switch("markdown"))
        self.edit_button = button(tr("Edit"), tr("Edit the Markdown text (Markdown shows it rendered again)"),
                                  lambda: self.edit_markdown())
        self.preview_check = QCheckBox(tr("Preview"))
        self.preview_check.setToolTip(tr("The Markdown rendered beside the text, as you type"))
        self.preview_check.toggled.connect(self.show_preview)
        self.wrap = QCheckBox(tr("Wrap lines"))
        self.wrap.toggled.connect(lambda on: self.text.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if on else QPlainTextEdit.LineWrapMode.NoWrap))
        self.position = plain_label(role="secondary", word_wrap=False)
        self.find_button = button(tr("Find…"), tr("Find and replace ({find}, {replace})", find=key_text(
            QKeySequence.StandardKey.Find), replace=key_text(REPLACE_KEY)), lambda: self.find_bar.open())
        # Save, Reload, Cancel: Cancel (Escape) closes without saving - asking first about changes not saved; a file
        # that is only viewed has Close.
        self.save_button = button(tr("Save (Ctrl+S)"), tr("Write the file back (asks first if it changed on disk "
                                                          "meanwhile)"), lambda: self.save())
        self.reload_button = button(tr("Reload"), tr("Read the file again from disk"), lambda: self.reload())
        self.cancel_button = button(tr("Cancel"), "", self.cancel)
        for widget in (self.as_text, self.as_hex, self.as_markdown, self.edit_button, self.preview_check, self.wrap):
            bottom.addWidget(widget)
        bottom.addWidget(self.position, 1)
        for widget in (self.find_button, self.save_button, self.reload_button, self.cancel_button):
            bottom.addWidget(widget)
        layout.addLayout(bottom)
        self.save_button.setVisible(not read_only)
        for widget in self.findChildren(QPushButton):  # Enter (in the find field, the hex view) presses no button
            widget.setAutoDefault(False)
            widget.setDefault(False)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self, activated=self.save)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self, activated=lambda: self.find_bar.open())
        QShortcut(QKeySequence(REPLACE_KEY), self, activated=lambda: self.find_bar.open(replace=True))
        for standard, steps in ((QKeySequence.StandardKey.ZoomIn, 1), (QKeySequence.StandardKey.ZoomOut, -1)):
            QShortcut(QKeySequence(standard), self, activated=lambda steps=steps: self.zoom(steps))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=lambda: self.zoom(0))
        # Close the window: Ctrl+W (macOS: Cmd+W) and the system's own Close keys - as Cancel, asking first about changes
        # not saved. Each key once (two shortcuts of one key would both stay silent).
        closing = {key.toString(): key for key in QKeySequence.keyBindings(QKeySequence.StandardKey.Close)}
        closing.setdefault(QKeySequence("Ctrl+W").toString(), QKeySequence("Ctrl+W"))
        for key in closing.values():
            QShortcut(key, self, activated=self.cancel)
        # F3 and Shift+F3 everywhere (macOS's own Find Next is Cmd+G only), each key once (two shortcuts of one key
        # would both stay silent).
        for standard, extra, backward in ((QKeySequence.StandardKey.FindNext, "F3", False),
                                          (QKeySequence.StandardKey.FindPrevious, "Shift+F3", True)):
            keys = {key.toString(): key for key in QKeySequence.keyBindings(standard)}
            keys.setdefault(QKeySequence(extra).toString(), QKeySequence(extra))
            for key in keys.values():
                QShortcut(key, self, activated=lambda backward=backward: self.find_bar.search(backward=backward))
        self.title()
        self.load()

    # -- reading --

    def title(self):
        name = printable(self.name)
        if self.read_only:
            self.setWindowTitle(tr("{name} (read-only)", name=name))
            self.cancel_button.setText(tr("Close"))
            self.cancel_button.setToolTip(safe_tooltip(tr("Close the window ({shortcut})", shortcut=key_text("Ctrl+W"))))
        else:
            self.setWindowTitle(("* " if self.dirty else "") + tr("{name} – editor", name=name))
            self.cancel_button.setText(tr("Cancel"))
            self.cancel_button.setToolTip(safe_tooltip(tr(
                "Close without saving (asks first about changes not saved; Escape, {shortcut})",
                shortcut=key_text("Ctrl+W"))))

    def say(self, text, warning=False):
        self.notice.setText(text)
        self.notice.setStyleSheet(f"color: {theme.color('warn')};" if warning else "")
        self.notice.setVisible(bool(text))

    def load(self):
        self.loading = True
        set_enabled(self.save_button, False)
        # (what was typed while it reads - the window just opened, or a Reload - was replaced by what it read, unasked)
        self.text.setReadOnly(True)
        self.hex.read_only = True
        self.say(tr("Reading {name}…", name=printable(self.name)))
        root, relative = self.root, self.relative
        background(lambda: root.read_file(relative), owner=self, timeout=120, done=self.loaded)

    def loaded(self, result, error):
        self.loading = False
        if error is not None:
            self.say(tr("Cannot open {name}: {error}", name=printable(self.name), error=printable(error)), True)
            if self.view is not None:  # (a Reload: the window stays as it was - it said "read-only" from then on and
                self.reload_view = None  # could not save any more, while its text still took what was typed)
                set_enabled(self.save_button, not self.read_only and not self.saving)
                self.text.setReadOnly(self.read_only or not self.text_ok)
                self.hex.read_only = self.read_only
                return
            self.read_only = True
            self.title()
            return
        self.original, self.identity, self.truncated = result.data, result.identity, result.truncated
        self.file_mode = result.mode or None  # (a file removed meanwhile is saved anew with its own bits)
        self.read_only = self.viewer or result.truncated
        notes = []
        if result.truncated:
            notes.append(tr("Only the first {shown} of {size} are shown; a file this big is not changed here.",
                            shown=files.size_text(len(result.data)), size=files.size_text(result.size)))
        decoded = files.text_of(result.data, result.truncated) if len(result.data) <= files.TEXT_LIMIT else None
        split = split_text(decoded) if decoded is not None else None
        self.text_ok = split is not None
        self.decoded = decoded is not None
        # (Reload: the view shown before - it showed the one the window was opened with: the text of a Markdown file
        # being edited went back to the rendering, the hex view to the text)
        want, self.reload_view = getattr(self, "reload_view", None) or self.mode, None
        if want == "auto":
            want = "text" if decoded is not None else "hex"
        if want == "markdown" and decoded is None:
            notes.append(tr("Not text (UTF-8 without NUL bytes, at most {size}): it cannot be shown as Markdown; "
                            "shown in the hex editor.", size=files.size_text(files.TEXT_LIMIT)))
            want = "hex"
        if want == "text" and decoded is None:
            notes.append(tr("Not text (UTF-8 without NUL bytes, at most {size}): shown in the hex editor.",
                            size=files.size_text(files.TEXT_LIMIT)))
            want = "hex"
        self.hex.set_data(result.data, self.read_only)
        if decoded is not None:
            if split is None:
                notes.append(tr("This text has mixed line endings or characters the editor would change: it is shown "
                                "read-only; the hex editor changes it byte by byte."))
                self.text.setPlainText(decoded)
            else:
                self.text.setPlainText(split[0])
                self.bom, self.newline = split[1], split[2]
            warning = text_warning(decoded)
            if warning and want in ("text", "markdown"):
                notes.append(warning)
        else:
            self.text.setPlainText("")
        self.text.document().setModified(False)
        self.dirty = False
        self.show_view(want)
        self.say("\n".join(notes), warning=bool(notes))
        self.title()

    def show_view(self, view):
        self.view = view
        self.stack.setCurrentWidget({"text": self.text_page, "markdown": self.markdown}.get(view, self.hex))
        self.text.setReadOnly(self.read_only or not self.text_ok)
        self.wrap.setVisible(view == "text")
        set_enabled(self.as_text, view != "text" and self.text_ok)
        set_enabled(self.as_hex, view != "hex")
        set_enabled(self.as_markdown, view != "markdown" and self.decoded)
        # Markdown: the rendering, and Edit (the text) beside it; the text of a Markdown file: Preview.
        self.edit_button.setVisible(view == "markdown" and self.text_ok and not self.truncated)
        self.preview_check.setVisible(view == "text" and self.markdown_like)
        self.preview.setVisible(view == "text" and self.markdown_like and self.preview_check.isChecked())
        if view == "markdown":
            self.markdown.render(editor_text(self.text))
        elif self.preview.isVisible():
            self.preview.render(editor_text(self.text))
        set_enabled(self.save_button, not self.read_only and not self.loading)
        self.show_position()
        # The find bar: what this view offers; a match marked in the other view is no longer shown.
        self.hex.found = None
        self.text.setExtraSelections([])
        self.markdown.browser.setExtraSelections([])
        self.find_bar.fit_view()
        if not self.find_bar.isHidden():
            self.find_bar.search(incremental=True)

    def show_position(self):
        if self.view == "hex":
            size = len(self.hex.buffer)
            self.position.setText(tr("Offset {offset} of {size} bytes", offset=f"0x{self.hex.cursor:x}", size=size)
                                  if size else tr("Empty file"))
        else:
            cursor = self.text.textCursor()
            self.position.setText(tr("Line {line}, column {column}", line=cursor.blockNumber() + 1,
                                     column=cursor.positionInBlock() + 1))

    def mark_dirty(self):
        if not self.dirty:
            self.dirty = True
            self.title()

    # -- the views --

    def contents(self):
        """The bytes shown now (what Save writes)."""
        if self.view == "hex":
            return self.hex.data()
        if not self.text_ok:
            return self.original
        return join_text(editor_text(self.text), self.bom, self.newline)

    def switch(self, view):
        """Show the other view; what was changed goes along."""
        if self.loading or self.view is None or view == self.view:
            return False
        if view == "markdown" or (view == "text" and self.view == "markdown"):
            # (the text widget holds the text in both: from the hex view it is made text first)
            if self.view == "hex" and not self.switch("text"):
                return False
            if view == "markdown" and not self.decoded:
                return False
            self.show_view(view)
            return True
        if view == "hex":
            data = self.contents()
            edited = self.hex.edited if data == self.hex.data() else changed_bytes(data, self.original)
            self.hex.set_data(data, self.read_only)
            self.hex.edited = set(edited)
        else:
            data = self.hex.data()
            decoded = files.text_of(data)
            split = split_text(decoded) if decoded is not None else None
            if split is None:
                self.say(tr("These bytes are no text the editor can show unchanged (UTF-8 without NUL bytes, one kind "
                            "of line ending)."), True)
                return False
            dirty = self.dirty
            self.text.setPlainText(split[0])
            self.bom, self.newline = split[1], split[2]
            # (text that can be given back exactly now - the hex editor mended a text that could not: contents() gave
            # the file as it was read for it, and a Save from the text or Markdown view lost what was mended)
            self.text_ok = self.decoded = True
            self.text.document().setModified(False)
            self.dirty = dirty
        self.show_view(view)
        return True

    # -- Markdown --

    def show_preview(self, on):
        """Preview: the Markdown rendered beside the text (the text view of a Markdown file)."""
        shown = bool(on) and self.view == "text"
        self.preview.setVisible(shown)
        if shown:
            self.preview.render(editor_text(self.text))

    def follow_typing(self):
        if self.preview.isVisible():
            self.preview.show_later(editor_text(self.text))

    def edit_markdown(self):
        """Edit: from the rendering to the text - a file only viewed (F3) becomes one being edited."""
        if self.viewer and not self.truncated and not self.loading:
            self.viewer = self.read_only = False
            self.hex.read_only = False
            self.save_button.setVisible(True)
            self.title()
        return self.switch("text")

    def zoom(self, steps):
        """Ctrl+Plus, Ctrl+Minus, Ctrl+0: the rendering larger, smaller, as it was (the preview in the text view)."""
        (self.preview if self.view == "text" else self.markdown).zoom_by(steps)

    def open_link(self, url):
        """A link clicked in the rendering (never followed there): http(s) opens in the browser once the human said
        yes; a relative link to a Markdown file of the same root opens it in a viewer of its own; anything else is
        named, not opened."""
        target = url.toString()
        scheme = url.scheme().lower()
        if scheme in ("http", "https"):
            if confirm(self, tr("Open the link?"), tr("Open {url} in your web browser? Links in a file can lead "
                                                      "anywhere; this one is in {name}.", url=printable(target),
                                                      name=printable(self.name)), tr("Open")):
                open_url(url)
            return True
        path = QUrl.fromPercentEncoding(url.path(QUrl.ComponentFormattingOption.FullyEncoded).encode())
        if not scheme and path and not path.startswith("/"):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(self.relative), path))
            if joined != ".." and not joined.startswith("../") and is_markdown(joined) and \
                    self.on_open_file is not None and (not self.top or joined.startswith(self.top + "/")):
                self.on_open_file(joined, posixpath.normpath(posixpath.join(posixpath.dirname(self.shown), path)))
                return True
        self.say(tr("Not opened: {target} (web links open after a question, Markdown files of this folder in a "
                    "viewer of their own).", target=printable(target)), True)
        return False

    # -- writing --

    def save(self, replace=False, data=None, in_place=False):
        """Write what is shown (DATA: those bytes) back to the file (in a thread). REPLACE: also when it changed on
        disk meanwhile. IN_PLACE: into the file itself (its folder may not be written; asked first: saved())."""
        if self.read_only or self.loading or self.saving:
            return False
        data = self.contents() if data is None else data
        root, relative, expected = self.root, self.relative, None if replace else self.identity
        mode = getattr(self, "file_mode", None)
        self.busy(True)
        background(lambda: root.write_file(relative, data, expected, in_place=in_place, mode=mode), owner=self,
                   timeout=120,
                   work=True, done=lambda result, error: self.saved(result, error, data, replace, in_place))
        return True

    def busy(self, saving):
        """SAVING: a save runs or asks - Save (Ctrl+S) and Reload wait for it (the window is one of its own: its
        question blocks neither)."""
        self.saving = saving
        set_enabled(self.save_button, not saving and not self.read_only and not self.loading)
        set_enabled(self.reload_button, not saving)

    def saved(self, result, error, data, replaced=False, in_place=False):
        name = printable(self.name)
        if isinstance(error, files.NoTemporary):
            # (its folder may not be written: no temporary file beside it - or a new file renamed over it would not
            # keep its owner, group or ACL (files.NotKept, which says which): the file itself, if the human says so)
            if type(error) is not files.NoTemporary:  # (NotKept, Busy: they say why)
                question = tr("{error}\n\nWrite the file itself? That is not done in one step - if it is cut short (a "
                              "crash, a full disk), the file is left half written.", error=printable(error))
            else:
                question = tr("{name} is in a folder you may not write: it cannot be saved through a temporary file "
                              "renamed over it. Write the file itself? That is not done in one step - if it is cut "
                              "short (a crash, a full disk), the file is left half written.", name=name)
            in_place = confirm(self, tr("Write the file in place?"), question, tr("Write in place"))
            if not isValid(self):
                return False
            self.busy(False)
            if in_place:
                return self.save(replace=replaced, data=data, in_place=True)
            self.say(tr("Not saved: {error}", error=printable(error)), True)
            return False
        if isinstance(error, files.Changed):
            # Still saving while it asks: a second Save would ask again, a Reload would show what is not written.
            replace = ask_first(self, "files-replace", tr("Replace the changed file?"),
                                tr("{name} changed on disk since it was opened here (the agent or another program "
                                   "wrote it). Replace it with what is shown here? Reload shows what is on disk now.",
                                   name=name), tr("Replace"))
            if not isValid(self):
                return False  # closed while it asked (its changes discarded)
            self.busy(False)
            if replace:  # (what the question was about; in place again when that was asked for)
                return self.save(replace=True, data=data, in_place=in_place)
            self.say(tr("Not saved: {name} changed on disk meanwhile.", name=name), True)
            return False
        self.busy(False)
        if error is not None:
            self.say(tr("Not saved: {error}", error=printable(error)), True)
            return False
        self.identity, self.original = result, data
        if self.contents() != data:  # typed while the save ran (or while it asked): not saved, still to be
            self.dirty = True
            self.say(tr("Saved {name} ({size}); what was changed while it was saved is not saved yet.", name=name,
                        size=files.size_text(len(data))), True)
            self.title()
            if self.on_saved is not None:
                self.on_saved()
            return True
        if self.view == "hex":
            self.hex.edited = set()
            self.hex.viewport().update()
        self.text.document().setModified(False)
        self.dirty = False
        self.say(tr("Saved {name} ({size}).", name=name, size=files.size_text(len(data))))
        self.title()
        if self.on_saved is not None:
            self.on_saved()
        return True

    def reload(self):
        if self.saving:  # (its button is greyed out meanwhile)
            return False
        if self.dirty and not ask_first(self, "editor-discard", tr("Discard changes?"), tr(
                "{name} has changes that are not saved. Read it again from disk and discard them?",
                name=printable(self.name)), tr("Discard")):
            return False
        for view in (self.markdown, self.preview):  # (its images too: one changed on disk kept its old picture)
            view.forget_images()
        self.reload_view = self.view
        self.load()
        return True

    # -- closing --

    def may_close(self):
        if getattr(self, "dropped", False):  # (the app quits for good: FilesPage.drop_changes - nobody to answer)
            return True
        if self.saving:
            # (asked "has changes that are not saved - discard them?" while they were being saved, and saved they were;
            # a save that then failed or asked - the file changed on disk meanwhile - went unsaid, the window gone)
            return ask_first(self, "editor-discard", tr("Still saving"), tr(
                "{name} is being saved right now. Close the window anyway? The save goes on, but whether it succeeds "
                "is not shown then, and what was changed after Save is not saved.", name=printable(self.name)),
                tr("Close"), reject=tr("Keep editing"))
        return not self.dirty or ask_first(self, "editor-discard", tr("Discard changes?"), tr(
            "{name} has changes that are not saved. Close and discard them?", name=printable(self.name)),
            tr("Discard"), reject=tr("Keep editing"))

    def closeEvent(self, event):  # noqa: N802
        if self.may_close():
            self.dirty = False
            event.accept()
        else:
            event.ignore()

    def keyPressEvent(self, event):  # noqa: N802
        """Escape closes the find bar first; then it is Cancel (reject: asks first about changes not saved)."""
        if event.key() == Qt.Key.Key_Escape and not self.find_bar.isHidden():
            self.find_bar.close_bar()
            event.accept()
            return
        super().keyPressEvent(event)

    def cancel(self):
        """Cancel: close without saving (asks first about changes not saved, the "editor-discard" question)."""
        return self.reject()

    def reject(self):
        if self.may_close():
            self.dirty = False
            super().reject()
