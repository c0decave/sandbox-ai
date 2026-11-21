"""The Files page's viewer and editor: a file opened by a double click (Enter, F3 view, F4 edit) shows in a window of its
own, as text when it is text (valid UTF-8 without NUL bytes, up to files.TEXT_LIMIT), else in a hex editor (up to
files.EDIT_LIMIT; a bigger file shows its first part, read-only).

The file is read and written through its pane's files.Root (Root.read_file, Root.write_file), as every other
operation of the file manager: relative to open directories, never through a symbolic link, a FIFO or a device, and
the file is never run. Saving writes a temporary file beside it and renames it over the old one; when the file changed
on disk since it was opened (the agent wrote it meanwhile), saving asks before it replaces it; when it is no longer
there (removed, renamed or moved meanwhile), before it saves it anew where it was (files.Gone). Text that cannot be
written back exactly as it was read (mixed line endings, a paragraph separator) opens read-only: the hex editor
changes it byte by byte. Invisible and direction-changing characters (Trojan Source) are named above the text.

The buttons read Save (Ctrl+S), Save as… (Ctrl+Shift+S: what is shown as a new file in the same folder), Reload and Cancel: Cancel (Escape, Ctrl+W - macOS: Cmd+W) closes the window without
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
import stat
import sys
import unicodedata

from PySide6.QtCore import QEvent, QRect, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFontMetrics, QKeySequence, QPainter, QPalette, QShortcut, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea, QCheckBox, QDialog, QHBoxLayout, QInputDialog, QLineEdit, QPlainTextEdit, QPushButton,
    QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from . import applog, files, theme
from .files import printable, printable_text
from .i18n import tr
from .markdown_view import LONG_RUN, LONG_WORD, ImageSource, MarkdownView, is_markdown
from .widgets import (
    MONO, FlowLayout, ask_first, background, button, confirm, plain_label, safe_tooltip, set_enabled, shown_as_text,
)

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


def save_as_problem(name):
    """What is wrong with NAME as the name of the file Save as writes ("": nothing): one name in this file's folder
    (no "/"), not "." or "..", no control or invisible characters, at most 255 bytes."""
    name = str(name or "")
    if not name.strip():
        return tr("Type a name for the new file.")
    if "/" in name:
        return tr("A file's name has no '/': Save as writes into this file's folder.")
    if name in (".", ".."):
        return tr("'.' and '..' are not names of a file.")
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in name):
        return tr("A name with control characters (a line break, a tab, NUL) or invisible ones is not used: {name}",
                  name=printable(name))
    if len(name.encode("utf-8", "surrogatepass")) > 255:
        return tr("That name is too long: at most 255 bytes.")
    return ""


class ChangedBytes:
    """The offsets of the bytes the hex view marks as changed: one byte each in a mask (a set of them took about 70
    bytes an offset - a character typed at the start of a text of 5 MiB, then the hex view, marked every byte after it:
    350 MB, and half a second in Python). In, add, len, iteration and == (with another, or a set of offsets) as a
    set's."""

    def __init__(self, mask=b""):
        self.mask = bytearray(mask)

    def add(self, offset):
        if offset >= len(self.mask):
            self.mask.extend(bytes(offset + 1 - len(self.mask)))
        self.mask[offset] = 1

    def __contains__(self, offset):
        return isinstance(offset, int) and 0 <= offset < len(self.mask) and self.mask[offset] == 1

    def __len__(self):
        return self.mask.count(1)

    def __bool__(self):
        return 1 in self.mask

    def __iter__(self):
        at = self.mask.find(1)
        while at >= 0:
            yield at
            at = self.mask.find(1, at + 1)

    def __eq__(self, other):
        if isinstance(other, ChangedBytes):
            return self.mask.rstrip(bytes(1)) == other.mask.rstrip(bytes(1))
        if isinstance(other, (set, frozenset)):
            return len(self) == len(other) and all(offset in self for offset in other)
        return NotImplemented

    __hash__ = None

    def copy(self):
        return ChangedBytes(self.mask)

    def __repr__(self):
        return f"ChangedBytes({sorted(self)[:20]}{'...' if len(self) > 20 else ''})"


NONZERO = bytes([0] + [1] * 255)  # (bytes.translate: a byte that differs becomes 1)


def changed_bytes(data, original):
    """The offsets where DATA differs from ORIGINAL (as far as both go), as ChangedBytes: the two XORed as numbers, each
    byte that is not 0 a change (byte by byte in Python, 4 MiB took 150 ms, and a set of them 70 bytes each)."""
    end = min(len(data), len(original))
    if not end:
        return ChangedBytes()
    differ = int.from_bytes(data[:end], "big") ^ int.from_bytes(original[:end], "big")
    return ChangedBytes(differ.to_bytes(end, "big").translate(NONZERO))


ASTRAL = re.compile("[\U00010000-\U0010ffff]")


class DocumentText:
    """The text of a QTextDocument as the find bar searches it: what was typed is found as it was typed (Qt's own
    QTextDocument.find reads every non-breaking space of the text as a space - one typed or pasted into the find field
    was never found, and a space found one), and positions go both ways between Python's characters and the
    document's (UTF-16 units: a character beyond U+FFFF is two). TEXT: the document's raw text, a position of it a
    position of the document (blocks apart by U+2029, an image or a table's frame a character each)."""

    def __init__(self, document):
        self.text = document.toRawText()
        self.astral = [found.start() for found in ASTRAL.finditer(self.text)]  # (as characters)
        self.astral_units = [at + count for count, at in enumerate(self.astral)]  # (as the document's positions)

    def unit(self, index):
        """The document's position of the character INDEX."""
        return index + bisect.bisect_left(self.astral, index) if self.astral else index

    def index(self, unit):
        """The character at the document's position UNIT."""
        return unit - bisect.bisect_left(self.astral_units, unit) if self.astral else unit

    def find(self, pattern, at, backward=False, length=1):
        """(start, end) characters of the first match of PATTERN that starts at AT or later (BACKWARD: the last one that
        starts before AT; LENGTH: how many characters a match has), or None."""
        if not backward:
            found = pattern.search(self.text, at)
            return found.span() if found is not None else None
        end, size = min(len(self.text), at - 1 + length), 4096
        while at > 0:  # (a window before AT, wider each time: never every match of the text in Python)
            begin, last = max(0, at - size), None
            for last in pattern.finditer(self.text, begin, end):
                pass
            if last is not None or begin == 0:
                return last.span() if last is not None else None
            size *= 4
        return None


def text_pattern(needle, case=True):
    """NEEDLE as a regular expression of itself (nothing in it is a pattern), CASE: in its letter case only."""
    return re.compile(re.escape(needle), 0 if case else re.IGNORECASE)


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
        self.buffer, self.edited = bytearray(), ChangedBytes()
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
        self.buffer, self.edited, self.found = bytearray(data), ChangedBytes(), None
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
    changes a file's size, so nothing is replaced there. Escape closes the bar. What was typed is found as it was typed
    (DocumentText: a non-breaking space is no space)."""

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

    def pattern(self, needle):
        """What the text is searched for: NEEDLE as it was typed (text_pattern), in its case with Match case."""
        return text_pattern(needle, self.case.isChecked())

    @staticmethod
    def selecting(document, known, span):
        """A QTextCursor of DOCUMENT that selects SPAN, (start, end) characters of KNOWN (its DocumentText)."""
        cursor = QTextCursor(document)
        cursor.setPosition(known.unit(span[0]))
        cursor.setPosition(known.unit(span[1]), QTextCursor.MoveMode.KeepAnchor)
        return cursor

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
        known, pattern = DocumentText(document), self.pattern(needle)
        # (typing on: the match grows where it is; backward: the one before the match shown)
        at = known.index(cursor.selectionStart() if incremental or backward else cursor.selectionEnd())
        span = known.find(pattern, at, backward, len(needle))
        if span is None:  # round the end
            span = known.find(pattern, len(known.text) + 1 if backward else 0, backward, len(needle))
        if span is None:
            edit.setExtraSelections([])
            self.say(tr("No match"))
            return False
        found = self.selecting(document, known, span)
        edit.setTextCursor(found)
        edit.ensureCursorVisible()
        mark = QTextEdit.ExtraSelection()
        mark.cursor = found
        mark.format.setBackground(edit.palette().color(QPalette.ColorRole.Highlight))
        mark.format.setForeground(edit.palette().color(QPalette.ColorRole.HighlightedText))
        edit.setExtraSelections([mark])
        starts = self.text_matches(known, pattern)
        self.say_count(bisect.bisect_left(starts, span[0]), starts, span[0])
        return True

    @staticmethod
    def text_matches(known, pattern):
        """Where the matches of PATTERN start in KNOWN (a DocumentText; at most MATCH_LIMIT of them), as characters."""
        starts = []
        for found in pattern.finditer(known.text):
            starts.append(found.start())
            if len(starts) >= MATCH_LIMIT:
                break
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
            known = DocumentText(edit.document())
            same = self.pattern(needle).match(known.text, known.index(cursor.selectionStart()))
            if same is not None and known.unit(same.end()) == cursor.selectionEnd():
                cursor.insertText(self.replacement.text())
                edit.setTextCursor(cursor)
        return self.search()

    def replace_all(self):
        """Replace every match, as one step to undo; returns how many. The text from the first match on is given anew
        in one change (each match replaced on its own laid its whole line out again: on the long line of a minified
        file Replace all took minutes, the window hung)."""
        if not self.can_replace():
            return 0
        edit, needle, new = self.editor.text, self.find.text(), self.replacement.text()
        document = edit.document()
        known, pattern = DocumentText(document), self.pattern(needle)
        first = pattern.search(known.text)
        count = 0
        if first is not None:
            # (the replacement as it was typed: a backslash in it is no reference to a group)
            rest, count = pattern.subn(lambda _found: new, known.text[first.start():])
            scrolled = edit.verticalScrollBar().value(), edit.horizontalScrollBar().value()
            cursor = QTextCursor(document)
            cursor.beginEditBlock()
            cursor.setPosition(known.unit(first.start()))
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(rest)
            cursor.endEditBlock()
            edit.verticalScrollBar().setValue(scrolled[0])
            edit.horizontalScrollBar().setValue(scrolled[1])
        edit.setExtraSelections([])
        self.say(tr("Replaced {count}", count=count) if count else tr("No match"))
        return count


def open_url(url):
    """URL (http or https, the human said yes) in the system's browser."""
    QDesktopServices.openUrl(url)


class BarLayout(FlowLayout):
    """The editor's bar: its groups on one line where there is room (its size hint), wrapped where there is not."""

    def sizeHint(self):  # noqa: N802
        shown = [item for item in self.items if item.widget() is None or not item.widget().isHidden()]
        width = sum(item.sizeHint().width() for item in shown) + self.gap * max(0, len(shown) - 1)
        return QSize(width, max((item.sizeHint().height() for item in shown), default=0))


class EditorWindow(QDialog):
    """One file of a pane in a window of its own: text, hex or rendered Markdown (MODE "auto", "text", "hex",
    "markdown"), editable unless READ_ONLY (F3: a viewer). SAVED() after it was saved (the page lists its panes again);
    OPEN_FILE(relative, shown): a Markdown file of the same root a link of this one leads to, in a viewer of its own.
    DROPPED(mime): what files dragged onto the window do (FilesPage.editor_drop) - None: nothing of its (text dragged
    within the text is the text's own), else (why they cannot be opened or None, OPEN(window) after the drop): each
    file opens in an editor of its own, nothing is typed into this one."""

    def __init__(self, parent, root, relative, shown, mode="auto", read_only=False, saved=None, open_file=None, top="",
                 dropped=None):
        super().__init__(parent)
        self.on_dropped = dropped
        self.root, self.relative, self.shown, self.mode = root, relative, shown, mode
        self.top = "/".join(part for part in top.split("/") if part)  # (images and Markdown links: below it only)
        self.viewer, self.read_only, self.on_saved, self.on_open_file = read_only, read_only, saved, open_file
        self.markdown_like = is_markdown(relative) or mode == "markdown"
        self.decoded = False  # the file is text (UTF-8): it can be shown as Markdown
        self.original, self.identity, self.bom, self.newline, self.view = b"", None, False, "\n", None
        self.loading, self.saving, self.truncated, self.dirty, self.text_ok = False, False, False, False, False
        self.text_bytes = None  # the bytes the text widget was given (read-only text: what it shows still)
        self.name = relative.rsplit("/", 1)[-1]
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        self.where = plain_label(printable(shown), role="secondary")
        self.where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.where)
        self.notice = plain_label(selectable=True, log="editor")
        self.notice.hide()
        layout.addWidget(self.notice)
        self.stack = QStackedWidget()
        self.text = QPlainTextEdit()
        self.text.setFont(MONO)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setAccessibleName(tr("Text of the file"))
        self.text.setTabStopDistance(QFontMetrics(MONO).horizontalAdvance(" ") * 4)
        self.text.document().modificationChanged.connect(self.modification_changed)
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
        self.wrap.toggled.connect(self.wrap_lines)
        self.position = plain_label(role="secondary", word_wrap=False)
        self.find_button = button(tr("Find…"), tr("Find and replace ({find}, {replace})", find=key_text(
            QKeySequence.StandardKey.Find), replace=key_text(REPLACE_KEY)), lambda: self.find_bar.open())
        # Save, Reload, Cancel: Cancel (Escape) closes without saving - asking first about changes not saved; a file
        # that is only viewed has Close.
        # (the key as the system writes it: on macOS Cmd+S - the button said Ctrl+S there)
        self.save_button = button(tr("Save ({shortcut})", shortcut=key_text(QKeySequence.StandardKey.Save)),
                                  tr("Write the file back (asks first if it changed on disk meanwhile)"),
                                  lambda: self.save())
        # Save as…: what is shown (the text, the Markdown's text, the hex view's bytes) as a new file in the same folder
        # - also from a viewer (F3); the window shows the new file then.
        self.save_as_button = button(tr("Save as…"), tr("Write what is shown as a new file in this folder ({shortcut})",
                                                       shortcut=key_text(QKeySequence.StandardKey.SaveAs)),
                                     lambda: self.save_as())
        self.reload_button = button(tr("Reload"), tr("Read the file again from disk"), lambda: self.reload())
        self.cancel_button = button(tr("Cancel"), "", self.cancel)
        # The views and the position, then the actions: on one line where the window has the room, else the actions on
        # a line of their own (German with a large font on a laptop: one line was wider than the screen).
        self.bar = QWidget()
        bar = BarLayout(self.bar)
        for group in ((self.as_text, self.as_hex, self.as_markdown, self.edit_button, self.preview_check, self.wrap,
                       self.position),
                      (self.find_button, self.save_button, self.save_as_button, self.reload_button,
                       self.cancel_button)):
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            for widget in group:
                row.addWidget(widget)
            bar.addWidget(holder)
        layout.addWidget(self.bar)
        self.save_button.setVisible(not read_only)
        for widget in self.findChildren(QPushButton):  # Enter (in the find field, the hex view) presses no button
            widget.setAutoDefault(False)
            widget.setDefault(False)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self, activated=self.save)
        if not QKeySequence.keyBindings(QKeySequence.StandardKey.SaveAs):  # (no system key for it: Ctrl+Shift+S)
            QShortcut(QKeySequence("Ctrl+Shift+S"), self, activated=self.save_as)
        else:
            QShortcut(QKeySequence(QKeySequence.StandardKey.SaveAs), self, activated=self.save_as)
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
        if dropped is not None:
            # (every part that may get a drag - the text, the hex view, the renderings - and the window around them: a
            # drag one of them does not take goes on to the next one out)
            self.setAcceptDrops(True)
            for area in self.findChildren(QAbstractScrollArea):
                for widget in (area, area.viewport()):
                    widget.setAcceptDrops(True)
                    widget.installEventFilter(self)
            self.installEventFilter(self)
        self.title()
        self.load()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop) and \
                self.on_dropped is not None:
            return self.dragged_over(event)
        return super().eventFilter(watched, event)

    def dragged_over(self, event):
        """A drag over the window: files open in editors of their own once dropped (on_dropped); one that cannot be
        dropped is not taken (the "no" cursor) and the window says why. False: text, the widget's own."""
        found = self.on_dropped(event.mimeData())
        if found is None:
            return False
        why, opener = found
        kind = event.type()
        if why:
            if kind != QEvent.Type.DragEnter:
                self.say(tr("Cannot drop here: {why}", why=why), True)
            if kind == QEvent.Type.DragEnter:
                event.accept()  # (the moves over it follow: each is refused)
            else:
                event.ignore()
            return True
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if kind == QEvent.Type.Drop:
            # (after the drop has ended: what it opens may ask, and a question inside a drop handler holds up the
            # program the drag came from)
            QTimer.singleShot(0, self, lambda: opener(self))
        return True

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

    def place(self):
        """Which file this window shows, for the log: its root (this machine's folder, a sandbox, HOST over SFTP) and
        its path there."""
        root = getattr(self, "root", None)
        return f"{printable(getattr(root, 'label', None) or '?')}: {printable(getattr(self, 'relative', '') or '')}"

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
        applog.log("INFO", "files", f"editor: opening {self.place()}" + (" (remote, over SFTP)" if getattr(
            root, "remote", False) else "") + (" read-only" if self.read_only else ""))
        # (a remote file: no time limit for the read as a whole - its requests have theirs (remote.REQUEST_TIMEOUT); a
        # big file far away took longer than 120 s and was said not to open, while the read went on. A local one on a
        # file system that hangs: given up after 120 s.)
        background(lambda: root.read_file(relative), owner=self, done=self.loaded,
                   timeout=None if getattr(root, "remote", False) else 120)

    def loaded(self, result, error):
        self.loading = False
        if error is not None:
            applog.log("WARNING", "files", f"editor: {self.place()} cannot be opened: {applog.error_text(error)}"
                       + (" (a Reload: the window keeps what it showed)" if self.view is not None else ""))
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
                notes += self.fit_wrap(decoded)
                self.text.setPlainText(decoded)
                self.text_bytes = result.data
            else:
                notes += self.fit_wrap(split[0])
                self.text.setPlainText(split[0])
                self.bom, self.newline = split[1], split[2]
                self.text_bytes = None
            warning = text_warning(decoded)
            if warning and want in ("text", "markdown"):
                notes.append(warning)
        else:
            self.text.setPlainText("")
            self.text_bytes = None
        self.text.document().setModified(False)
        self.dirty = False
        self.show_view(want)
        applog.log("INFO", "files", f"editor: {self.place()} opened ({files.size_text(len(result.data))}"
                   + (f" of {files.size_text(result.size)}" if result.truncated else "") + f", shown as {want}"
                   + (", read-only" if self.read_only else "") + ")" + (
                       "\n" + applog.tail("\n".join(notes), lines=4) if notes else ""))
        self.say("\n".join(notes), warning=bool(notes))
        self.title()

    def show_view(self, view):
        self.view = view
        self.stack.setCurrentWidget({"text": self.text_page, "markdown": self.markdown}.get(view, self.hex))
        self.text.setReadOnly(self.read_only or not self.text_ok)
        self.wrap.setVisible(view == "text")
        # (a text it cannot give back exactly is shown read-only, also again after the hex view)
        set_enabled(self.as_text, view != "text" and (self.text_ok or self.decoded))
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

    def wrap_lines(self, on):
        """Wrap lines: the text wrapped at the window's width - not when a line holds more than LONG_RUN characters
        without a space (Qt wraps such a line in time that grows with its square: 1 MB of a minified file held up the
        window for about 40 s): the box goes off again, and the window says why."""
        if on:
            notes = self.fit_wrap(editor_text(self.text))
            if notes:
                self.say(notes[0], True)
                return
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if on else QPlainTextEdit.LineWrapMode.NoWrap)

    def fit_wrap(self, text):
        """Before TEXT is shown: Wrap lines off where TEXT has a line it could not wrap in time (wrap_lines); what the
        window says then ([] when nothing)."""
        if not self.wrap.isChecked() and self.text.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap:
            return []
        if not LONG_WORD.search(text):
            return []
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.wrap.blockSignals(True)
        self.wrap.setChecked(False)
        self.wrap.blockSignals(False)
        return [tr("Lines are not wrapped: a line of more than {count} characters without a space would hold up the "
                   "window.", count=LONG_RUN)]

    def mark_dirty(self):
        if not self.dirty:
            self.dirty = True
            self.title()

    def modification_changed(self, changed):
        """The text was changed - or undone to where it was last read or saved (Qt's own clean state): then nothing is
        left to save if it is what the file holds (it kept the "* " and asked to discard changes there were none of)."""
        if changed:
            self.mark_dirty()
        elif self.dirty and self.view in ("text", "markdown") and not (self.loading or self.saving) and \
                self.contents() == self.original:
            self.dirty = False
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
            self.hex.edited = edited.copy()
        else:
            data = self.hex.data()
            decoded = files.text_of(data)
            split = split_text(decoded) if decoded is not None else None
            if split is None:
                if decoded is not None and data == self.original:
                    # The file's text as it was read or saved (mixed line endings, a lone CR ...): read-only, as after
                    # a Reload; the hex view changes it byte by byte. (The text widget showed the text as it was
                    # opened - also after other bytes were saved from the hex view; a text that could be edited and
                    # was saved there with a lone CR was refused from then on.)
                    if self.text_bytes != data:
                        dirty = self.dirty
                        notes = self.fit_wrap(decoded)
                        if notes:
                            self.say(notes[0], True)
                        self.text.setPlainText(decoded)
                        self.text.document().setModified(False)
                        self.text_bytes, self.dirty = data, dirty
                    self.text_ok = False
                    self.show_view(view)
                    return True
                self.say(tr("These bytes are no text the editor can show unchanged (UTF-8 without NUL bytes, one kind "
                            "of line ending)."), True)
                return False
            dirty = self.dirty
            notes = self.fit_wrap(split[0])
            if notes:
                self.say(notes[0], True)
            self.text.setPlainText(split[0])
            self.bom, self.newline, self.text_bytes = split[1], split[2], None
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
        """Ctrl+Plus, Ctrl+Minus, Ctrl+0: the rendering larger, smaller, as it was (the preview in the text view) - only
        one that is shown (in the hex view, or the text view without Preview, the rendering not shown was zoomed: nothing
        changed on the screen, and it came back larger later)."""
        if self.view == "markdown":
            self.markdown.zoom_by(steps)
        elif self.view == "text" and not self.preview.isHidden():
            self.preview.zoom_by(steps)

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
        applog.log("INFO", "files", f"editor: saving {self.place()} ({files.size_text(len(data))}"
                   + (", replacing a file changed on disk" if replace else "") + (", in place" if in_place else "")
                   + ")")
        self.busy(True)
        # No time limit: a save that took longer (a big file to a slow remote system) said "Not saved: No answer within
        # 120 s" and gave Save and Reload back while it went on and replaced the file. It ends by itself - a remote
        # request without an answer ends its connection (remote.REQUEST_TIMEOUT) - and closing the window meanwhile
        # says it is still saving (may_close).
        background(lambda: root.write_file(relative, data, expected, in_place=in_place, mode=mode), owner=self,
                   work=True, done=lambda result, error: self.saved(result, error, data, replace, in_place))
        return True

    def save_as(self):
        """Save as…: what is shown written as a new file of a name asked for, in this file's folder (through the pane's
        Root: never through a link, O_EXCL-like - an existing name is asked about first); the window shows that file
        then. Not for a file shown only in part (a big one: its first part only)."""
        if self.loading or self.saving:
            return False
        if self.truncated:
            self.say(tr("Only the first part of {name} is shown: Save as would write only that part.",
                        name=printable(self.name)), True)
            return False
        data = self.contents()
        folder = self.relative.rpartition("/")[0]
        name, ok = QInputDialog.getText(self, tr("Save as"), shown_as_text(tr(
            "Name of the new file in the folder of {name}:", name=printable(self.name))), text=self.name)
        if not ok:
            return False
        problem = save_as_problem(name)
        if problem:
            self.say(problem, True)
            return False
        relative = f"{folder}/{name}" if folder else name
        if relative == self.relative:
            return self.save()
        root, mode = self.root, getattr(self, "file_mode", None)
        self.busy(True)

        def looked(found, error):
            if not isValid(self):
                return False
            if error is not None and not isinstance(error, FileNotFoundError):
                self.busy(False)
                self.say(tr("Not saved: {error}", error=printable_text(str(error))), True)
                return False
            if error is None and found is not None:
                if stat.S_ISLNK(found.st_mode) or not stat.S_ISREG(found.st_mode):
                    self.busy(False)
                    self.say(tr("{name} is there already and is no plain file (a link, a folder): not written.",
                                name=printable(name)), True)
                    return False
                if not confirm(self, tr("Replace {name}?", name=printable(name)), tr(
                        "{name} exists in this folder. Replace it with what is shown here?", name=printable(name)),
                        tr("Replace")):
                    if isValid(self):
                        self.busy(False)
                        self.say(tr("Not saved: {name} exists.", name=printable(name)), True)
                    return False
            applog.log("INFO", "files", f"editor: saving {self.place()} as {printable(name)} "
                                        f"({files.size_text(len(data))})")
            background(lambda: root.write_file(relative, data, None, mode=mode), owner=self, work=True,
                       done=lambda result, err: self.saved_as(result, err, data, relative, name))
            return True

        def look():
            try:
                return root.lstat(relative)
            except FileNotFoundError:
                return None
        background(look, owner=self, timeout=60, done=looked)
        return True

    def saved_as(self, result, error, data, relative, name):
        if not isValid(self):
            return False
        self.busy(False)
        if error is not None:
            applog.log("WARNING", "files", f"editor: {self.place()} not saved as {printable(name)}: "
                                           f"{applog.error_text(error)}")
            self.say(tr("Not saved: {error}", error=printable_text(str(error))), True)
            return False
        # The window is the new file's now (its folder, its path shown); the file it showed stays as it was.
        self.shown = posixpath.join(posixpath.dirname(str(self.shown)), name)
        self.relative, self.name, self.identity, self.original = relative, name, result, data
        self.where.setText(printable(self.shown))
        if self.hex.data() == data:
            self.hex.edited = ChangedBytes()
            self.hex.viewport().update()
        self.text.document().setModified(False)
        self.dirty = False
        self.say(tr("Saved as {name} ({size}).", name=printable(name), size=files.size_text(len(data))))
        self.title()
        if self.on_saved is not None:
            self.on_saved()
        return True

    def busy(self, saving):
        """SAVING: a save runs or asks - Save (Ctrl+S) and Reload wait for it (the window is one of its own: its
        question blocks neither)."""
        self.saving = saving
        set_enabled(self.save_button, not saving and not self.read_only and not self.loading)
        set_enabled(self.save_as_button, not saving and not self.loading)
        set_enabled(self.reload_button, not saving)

    def saved(self, result, error, data, replaced=False, in_place=False):
        name = printable(self.name)
        if error is not None:
            applog.log("INFO" if isinstance(error, (files.NoTemporary, files.Changed)) else "WARNING", "files",
                       f"editor: {self.place()} not saved: {applog.error_text(error)}" + (
                           " - asked what to do" if isinstance(error, (files.NoTemporary, files.Changed)) else ""))
        else:
            applog.log("INFO", "files", f"editor: {self.place()} saved ({files.size_text(len(data))})")
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
        if isinstance(error, files.Gone):
            # (removed, renamed or moved - also from the Files page - since it was opened: it asked to replace "the
            # changed file" and to Reload, which can only fail; Save makes a new file there - said so, asked each time)
            again = confirm(self, tr("Save as a new file?"), tr(
                "{name} is no longer there: it was removed, renamed or moved since it was opened here. Save what is "
                "shown as a new file {name} where it was?", name=name), tr("Save as a new file"))
            if not isValid(self):
                return False
            self.busy(False)
            if again:  # (a new file: never in place - there is none to write into)
                return self.save(replace=True, data=data)
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
        if self.hex.data() == data:  # (also saved from another view: the bytes changed in the hex view are saved)
            self.hex.edited = ChangedBytes()
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
