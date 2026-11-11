"""The Files page's viewer and editor: a file opened by a double click (Enter, F3 view, F4 edit) shows in a window of its
own, as text when it is text (valid UTF-8 without NUL bytes, up to files.TEXT_LIMIT), else in a hex editor (up to
files.EDIT_LIMIT; a bigger file shows its first part, read-only).

The file is read and written through its pane's files.Root (Root.read_file, Root.write_file), as every other
operation of the file manager: relative to open directories, never through a symbolic link, a FIFO or a device, and
the file is never run. Saving writes a temporary file beside it and renames it over the old one; when the file changed
on disk since it was opened (the agent wrote it meanwhile), saving asks before it replaces it. Text that cannot be
written back exactly as it was read (mixed line endings, a paragraph separator) opens read-only: the hex editor
changes it byte by byte. Invisible and direction-changing characters (Trojan Source) are named above the text.

The hex editor's layout - offset, hex and ASCII columns painted line by line, typing over a byte nibble by nibble, Tab
between the two columns - follows the idea of axross's ui/hex_editor.py (github.com/c0decave/axross, Apache-2.0);
the code here is this app's own.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractScrollArea, QCheckBox, QDialog, QHBoxLayout, QPlainTextEdit, QStackedWidget, QVBoxLayout,
)
from shiboken6 import isValid

from . import files, theme
from .files import printable
from .i18n import tr
from .widgets import MONO, ask_first, background, button, plain_label, set_enabled

BYTES_PER_LINE = 16
# Characters that do not show or turn the direction of what follows (zero width, bidi controls, a BOM in the middle):
# what is shown may then differ from what a compiler or a shell reads.
INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u061c\ufeff]")
CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")


def split_text(text):
    """(text for the editor, BOM, line ending) of the decoded file TEXT, or None when the editor could not give it
    back exactly (mixed line endings, a lone CR, a paragraph separator)."""
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
    if "\u2029" in text:  # (the editor's own paragraph separator: it would come back as a line break)
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
    """The text of the QPlainTextEdit EDIT as typed (toPlainText would turn non-breaking spaces into spaces)."""
    return edit.document().toRawText().replace("\u2029", "\n")


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
        self.setFont(MONO)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr("Bytes of the file"))
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.fit_font()

    # -- contents --

    def set_data(self, data, read_only=False):
        self.buffer, self.edited = bytearray(data), set()
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


class EditorWindow(QDialog):
    """One file of a pane in a window of its own: text or hex (MODE "auto", "text", "hex"), editable unless READ_ONLY
    (F3: a viewer). SAVED() after it was saved (the page lists its panes again)."""

    def __init__(self, parent, root, relative, shown, mode="auto", read_only=False, saved=None):
        super().__init__(parent)
        self.root, self.relative, self.shown, self.mode = root, relative, shown, mode
        self.viewer, self.read_only, self.on_saved = read_only, read_only, saved
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
        self.notice = plain_label()
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
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.hex)
        layout.addWidget(self.stack, 1)
        bottom = QHBoxLayout()
        self.as_text = button(tr("Text"), tr("Show the file as text (UTF-8)"), lambda: self.switch("text"))
        self.as_hex = button(tr("Hex"), tr("Show the file's bytes (hex editor)"), lambda: self.switch("hex"))
        self.wrap = QCheckBox(tr("Wrap lines"))
        self.wrap.toggled.connect(lambda on: self.text.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if on else QPlainTextEdit.LineWrapMode.NoWrap))
        self.position = plain_label(role="secondary", word_wrap=False)
        self.save_button = button(tr("Save (Ctrl+S)"), tr("Write the file back (asks first if it changed on disk "
                                                          "meanwhile)"), lambda: self.save())
        self.reload_button = button(tr("Reload"), tr("Read the file again from disk"), lambda: self.reload())
        close = button(tr("Close"), tr("Close the window (asks first about changes not saved)"), self.close)
        for widget in (self.as_text, self.as_hex, self.wrap):
            bottom.addWidget(widget)
        bottom.addWidget(self.position, 1)
        for widget in (self.save_button, self.reload_button, close):
            bottom.addWidget(widget)
        layout.addLayout(bottom)
        self.save_button.setVisible(not read_only)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self, activated=self.save)
        self.title()
        self.load()

    # -- reading --

    def title(self):
        name = printable(self.name)
        if self.read_only:
            self.setWindowTitle(tr("{name} (read-only)", name=name))
        else:
            self.setWindowTitle(("* " if self.dirty else "") + tr("{name} – editor", name=name))

    def say(self, text, warning=False):
        self.notice.setText(text)
        self.notice.setStyleSheet(f"color: {theme.color('warn')};" if warning else "")
        self.notice.setVisible(bool(text))

    def load(self):
        self.loading = True
        set_enabled(self.save_button, False)
        self.say(tr("Reading {name}…", name=printable(self.name)))
        root, relative = self.root, self.relative
        background(lambda: root.read_file(relative), owner=self, timeout=120, done=self.loaded)

    def loaded(self, result, error):
        self.loading = False
        if error is not None:
            self.say(tr("Cannot open {name}: {error}", name=printable(self.name), error=printable(error)), True)
            self.read_only = True
            self.title()
            return
        self.original, self.identity, self.truncated = result.data, result.identity, result.truncated
        self.read_only = self.viewer or result.truncated
        notes = []
        if result.truncated:
            notes.append(tr("Only the first {shown} of {size} are shown; a file this big is not changed here.",
                            shown=files.size_text(len(result.data)), size=files.size_text(result.size)))
        decoded = files.text_of(result.data, result.truncated) if len(result.data) <= files.TEXT_LIMIT else None
        split = split_text(decoded) if decoded is not None else None
        self.text_ok = split is not None
        want = self.mode
        if want == "auto":
            want = "text" if decoded is not None else "hex"
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
            if warning and want == "text":
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
        self.stack.setCurrentWidget(self.text if view == "text" else self.hex)
        self.text.setReadOnly(self.read_only or not self.text_ok)
        self.wrap.setVisible(view == "text")
        set_enabled(self.as_text, view != "text" and self.text_ok)
        set_enabled(self.as_hex, view != "hex")
        set_enabled(self.save_button, not self.read_only and not self.loading)
        self.show_position()

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
        if view == "hex":
            data = self.contents()
            edited = self.hex.edited if data == self.hex.data() else {
                at for at in range(min(len(data), len(self.original))) if data[at] != self.original[at]}
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
            self.text.document().setModified(False)
            self.dirty = dirty
        self.show_view(view)
        return True

    # -- writing --

    def save(self, replace=False, data=None):
        """Write what is shown (DATA: those bytes) back to the file (in a thread). REPLACE: also when it changed on
        disk meanwhile."""
        if self.read_only or self.loading or self.saving:
            return False
        data = self.contents() if data is None else data
        root, relative, expected = self.root, self.relative, None if replace else self.identity
        self.busy(True)
        background(lambda: root.write_file(relative, data, expected), owner=self, timeout=120, work=True,
                   done=lambda result, error: self.saved(result, error, data))
        return True

    def busy(self, saving):
        """SAVING: a save runs or asks - Save (Ctrl+S) and Reload wait for it (the window is one of its own: its
        question blocks neither)."""
        self.saving = saving
        set_enabled(self.save_button, not saving and not self.read_only and not self.loading)
        set_enabled(self.reload_button, not saving)

    def saved(self, result, error, data):
        name = printable(self.name)
        if isinstance(error, files.Changed):
            # Still saving while it asks: a second Save would ask again, a Reload would show what is not written.
            replace = ask_first(self, "files-replace", tr("Replace the changed file?"),
                                tr("{name} changed on disk since it was opened here (the agent or another program "
                                   "wrote it). Replace it with what is shown here? Reload shows what is on disk now.",
                                   name=name), tr("Replace"))
            if not isValid(self):
                return False  # closed while it asked (its changes discarded)
            self.busy(False)
            if replace:
                return self.save(replace=True, data=data)  # what the question was about
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
        self.load()
        return True

    # -- closing --

    def may_close(self):
        return not self.dirty or ask_first(self, "editor-discard", tr("Discard changes?"), tr(
            "{name} has changes that are not saved. Close and discard them?", name=printable(self.name)),
            tr("Discard"), reject=tr("Keep editing"))

    def closeEvent(self, event):  # noqa: N802
        if self.may_close():
            self.dirty = False
            event.accept()
        else:
            event.ignore()

    def reject(self):
        if self.may_close():
            self.dirty = False
            super().reject()
