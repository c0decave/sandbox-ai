"""The Images page: the agent images and your variants of them (sandbox-ai images), what is in them, and an
editor for their extensions - package lists, files and a Containerfile fragment with syntax highlighting,
line numbers and the validator's findings inline, and a preview of the final Containerfile.

Reading and validating use sandbox-ai's own images module (loaded from its installation); every change goes
through `sandbox-ai images extend|new|remove|recipe`, every build through `sandbox-ai build` in a terminal. The app
never writes an extension itself.

The host chooser at the top shows and changes the images of this machine or of a host of `sandbox-ai hosts` (over
SSH: `sandbox-ai hosts run HOST -- images ...`, the fragment on standard input there too; a build in a terminal tab
with ssh -tt). A file added to an extension is then a path on that host."""
from __future__ import annotations

import json
import os
import re

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTextEdit,
    QToolTip, QVBoxLayout, QWidget,
)

from . import theme
from .files import printable
from .host_choice import on_host
from .i18n import tr
from .widgets import (
    MONO, AsyncCommand, Task, ask_first, button, confirm, fill, inform, plain_label, safe_tooltip, select_keys,
    selected_keys, show_text, table, warn, shown_as_text,
)

KINDS = ("apt", "npm", "pip")
# Every Containerfile instruction (the validator decides which an extension may use).
LOCAL = "local"
PIN_EXAMPLE = "2.1.0"  # the same in every language
UV_EXAMPLE = "0.9.5"
MEDIA, TOR = "media", "tor"  # the recipes of sandbox-ai images recipe
INSTRUCTIONS = ("ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM", "HEALTHCHECK", "LABEL",
                "MAINTAINER", "ONBUILD", "RUN", "SHELL", "STOPSIGNAL", "USER", "VOLUME", "WORKDIR")


def status_text(entry):
    """The extension column of an image: whether it is current with its extension."""
    if not entry.get("extended") and entry.get("status") in (None, "current"):
        return tr("no extension")
    return {"current": tr("current"), "outdated": tr("changed: rebuild"), "base-changed": tr("new base: rebuild"),
            "invalid": tr("has errors"), "not-built": tr("not built")}.get(entry.get("status"),
                                                                         printable(entry.get("status") or ""))


def status_color(entry):
    return {"current": None, "outdated": "warn", "base-changed": "warn", "invalid": "bad",
            "not-built": "warn"}.get(entry.get("status"), None)


def image_entries(document):
    """The rows of `sandbox-ai images --json` as dicts with printable texts; an older sandbox-ai without
    variants or extensions gives one row per agent."""
    entries = []
    for image in document if isinstance(document, list) else []:
        if not isinstance(image, dict):
            continue
        size = image.get("size") or 0
        agent = printable(image.get("agent", ""))
        entries.append({
            "target": printable(image.get("target") or agent), "agent": agent, "variant": bool(image.get("variant")),
            "present": bool(image.get("present")), "version": printable(image.get("agent_version") or ""),
            "created": printable(str(image.get("created") or "").replace("T", " ")),
            "size": f"{size / 1e9:.2f} GB" if isinstance(size, (int, float)) and size else "",
            "image": printable(image.get("image", "")), "extended": bool(image.get("extended")),
            "status": image.get("status") if isinstance(image.get("status"), str) else None})
    return entries


# -- the editor's parts --

class ContainerfileHighlighter(QSyntaxHighlighter):
    """Containerfile syntax: instructions, comments, strings, variables, options and line continuations, in
    the theme's colours (refreshed when the theme changes)."""

    KEYWORD = re.compile(r"^\s*([A-Za-z]+)\b")
    STRING = re.compile(r"\"(?:[^\"\\]|\\.)*\"?|'[^']*'?")
    VARIABLE = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*(?::?[-+?][^}]*)?\}|[A-Za-z_][A-Za-z0-9_]*)")
    OPTION = re.compile(r"(?<=\s)--[a-z][a-z-]*(?:=\S*)?")

    def __init__(self, document):
        super().__init__(document)
        self.restyle(rehighlight=False)

    def restyle(self, rehighlight=True):
        c = theme.colors()

        def form(color, bold=False, italic=False):
            result = QTextCharFormat()
            result.setForeground(QColor(color))
            if bold:
                result.setFontWeight(QFont.Weight.Bold)
            result.setFontItalic(italic)
            return result
        self.formats = {"keyword": form(c["accent"], bold=True), "comment": form(c["secondary"], italic=True),
                        "string": form(theme.color("ok")), "variable": form(theme.color("warn")),
                        "option": form(c["title"], italic=True), "continuation": form(c["secondary"], bold=True)}
        if rehighlight:
            self.rehighlight()

    def highlightBlock(self, text):  # noqa: N802
        continued = self.previousBlockState() == 1
        stripped = text.strip()
        if stripped.startswith("#"):
            self.setFormat(0, len(text), self.formats["comment"])
            self.setCurrentBlockState(1 if continued else 0)
            return
        if not continued:
            match = self.KEYWORD.match(text)
            if match and match.group(1).upper() in INSTRUCTIONS:
                self.setFormat(match.start(1), len(match.group(1)), self.formats["keyword"])
        for name, pattern in (("option", self.OPTION), ("variable", self.VARIABLE), ("string", self.STRING)):
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), self.formats[name])
        for match in self.STRING.finditer(text):  # variables inside double quotes stay visible
            if match.group(0).startswith('"'):
                for inner in self.VARIABLE.finditer(match.group(0)):
                    self.setFormat(match.start() + inner.start(), len(inner.group(0)), self.formats["variable"])
        if text.rstrip().endswith("\\"):
            self.setFormat(len(text.rstrip()) - 1, 1, self.formats["continuation"])
            self.setCurrentBlockState(1)
        else:
            self.setCurrentBlockState(1 if continued and not stripped else 0)


class LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):  # noqa: N802
        return QSize(self.editor.number_width(), 0)

    def paintEvent(self, event):  # noqa: N802
        self.editor.paint_numbers(event)


class CodeEditor(QPlainTextEdit):
    """A plain-text editor for Containerfile text: monospaced, highlighted, with line numbers and the
    validator's findings underlined (errors and warnings in their colours, the message as tooltip)."""

    def __init__(self, read_only=False):
        super().__init__()
        self.setFont(theme.font("hacker") or MONO)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabChangesFocus(True)
        self.setReadOnly(read_only)
        self.numbers = LineNumbers(self)
        self.findings = []
        self.highlighter = ContainerfileHighlighter(self.document())
        self.blockCountChanged.connect(self.update_margin)
        self.updateRequest.connect(self.update_numbers)
        self.cursorPositionChanged.connect(self.refresh_selections)
        self.update_margin()

    def number_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance("9") * max(2, digits)

    def update_margin(self, *_):
        self.setViewportMargins(self.number_width(), 0, 0, 0)

    def update_numbers(self, rect, dy):
        if dy:
            self.numbers.scroll(0, dy)
        else:
            self.numbers.update(0, rect.y(), self.numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_margin()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        area = self.contentsRect()
        self.numbers.setGeometry(QRect(area.left(), area.top(), self.number_width(), area.height()))

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self.highlighter.restyle()
            self.refresh_selections()
            self.numbers.update()

    def paint_numbers(self, event):
        c = theme.colors()
        painter = QPainter(self.numbers)
        painter.fillRect(event.rect(), QColor(c["alternate"]))
        marked = {}
        for item in self.findings:
            if item.get("line"):
                marked[item["line"]] = "bad" if item["severity"] == "error" or marked.get(item["line"]) == "bad" \
                    else "warn"
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        height = self.fontMetrics().height()
        current = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                line = number + 1
                if line in marked:
                    painter.setPen(QColor(theme.color(marked[line])))
                    font = QFont(self.font())
                    font.setBold(True)
                    painter.setFont(font)
                else:
                    painter.setPen(QColor(c["text"] if number == current else c["secondary"]))
                    painter.setFont(self.font())
                painter.drawText(0, top, self.numbers.width() - 6, height, Qt.AlignmentFlag.AlignRight, str(line))
            block = block.next()
            top, bottom = bottom, bottom + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()

    def set_findings(self, findings):
        self.findings = [f for f in findings if isinstance(f.get("line"), int)]
        self.refresh_selections()
        self.numbers.update()

    def finding_range(self, item):
        """(start, end) character positions a finding underlines."""
        block = self.document().findBlockByNumber(item["line"] - 1)
        if not block.isValid():
            return None
        column = max(0, min(item.get("column") or 0, block.length() - 1))
        length = item.get("length") or (block.length() - 1 - column)
        return block.position() + column, block.position() + column + max(1, length)

    def refresh_selections(self):
        selections = []
        if not self.isReadOnly():
            line = QTextEdit.ExtraSelection()
            line.format.setBackground(QColor(theme.colors()["alternate"]))
            line.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            line.cursor = self.textCursor()
            line.cursor.clearSelection()
            selections.append(line)
        for item in sorted(self.findings, key=lambda f: f["severity"] == "error"):  # errors drawn last
            span = self.finding_range(item)
            if span is None:
                continue
            selection = QTextEdit.ExtraSelection()
            selection.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            selection.format.setUnderlineColor(QColor(theme.color("bad" if item["severity"] == "error" else "warn")))
            cursor = QTextCursor(self.document())
            cursor.setPosition(span[0])
            cursor.setPosition(min(span[1], self.document().characterCount() - 1), QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            selections.append(selection)
        self.setExtraSelections(selections)

    def finding_at(self, position):
        for item in self.findings:
            span = self.finding_range(item)
            if span and span[0] <= position <= span[1]:
                return item
        return None

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            point = self.viewport().mapFromGlobal(event.globalPos())
            item = self.finding_at(self.cursorForPosition(point).position())
            if item:
                QToolTip.showText(event.globalPos(), safe_tooltip(finding_text(item)), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def go_to(self, line, column=0):
        block = self.document().findBlockByNumber(max(0, line - 1))
        cursor = self.textCursor()
        cursor.setPosition(block.position() + max(0, min(column, block.length() - 1)))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()


def severity_text(item):
    return tr("error") if item.get("severity") == "error" else tr("warning")


def finding_text(item):
    """One finding in plain words (sandbox-ai's message, in English, escaped)."""
    where = {"apt": tr("Debian packages"), "npm": tr("npm packages"), "pip": tr("Python tools"),
             "files": tr("Files")}.get(item.get("where"))
    place = tr("line {line}", line=item["line"]) if item.get("line") else where or ""
    text = f" ({printable(item['text'])})" if item.get("text") else ""
    return f"{place} · {severity_text(item)}{text}: {printable(item.get('message', ''))}"


class PackageList(QWidget):
    """One package list of an extension: search, add with validation, remove."""
    changed = Signal()

    def __init__(self, kind, page):
        super().__init__()
        self.kind, self.page = kind, page
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("search the list"))
        self.search.textChanged.connect(self.apply_filter)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText({"apt": tr("Debian package, e.g. build-essential or jq=1.6-2.1"),
                                       "npm": tr("npm package, e.g. typescript or @scope/name@1.2"),
                                       "pip": tr("Python tool, e.g. ruff or ruff==0.6.9")}[kind])
        self.entry.textChanged.connect(self.check_entry)
        self.entry.returnPressed.connect(self.add)
        self.add_button = button(tr("Add"), tr("Add it to the list (saved with Save)"), self.add)
        self.remove_button = button(tr("Remove"), tr("Remove the selected packages from the list"), self.remove)
        row.addWidget(self.entry, 1)
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        layout.addLayout(row)
        self.problem = plain_label(role="notice-error")
        self.problem.hide()
        layout.addWidget(self.problem)
        self.add_button.setEnabled(False)

    def values(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def set_values(self, values):
        self.list.clear()
        for value in values:
            item = QListWidgetItem(printable(value))
            item.setData(Qt.ItemDataRole.UserRole, value)
            self.list.addItem(item)
        self.apply_filter()

    def apply_filter(self):
        text = self.search.text().strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def check_entry(self):
        """The reason the typed package would be refused, shown at once; None if it is fine."""
        value = self.entry.text().strip()
        module = self.page.module()
        reason = module.check_package(self.kind, value, self.page.agent_name) if value and module else None
        self.problem.setText(printable(reason) if reason else "")
        self.problem.setVisible(bool(reason))
        self.add_button.setEnabled(bool(value) and not reason)
        return reason

    def add(self):
        value = self.entry.text().strip()
        module = self.page.module()
        if not value or module is None or self.check_entry():
            return
        name = module.package_name(self.kind, value)
        kept = [v for v in self.values() if module.package_name(self.kind, v) != name]
        self.set_values(kept + [value])
        self.entry.clear()
        self.changed.emit()

    def remove(self):
        chosen = {item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()}
        if chosen:
            self.set_values([v for v in self.values() if v not in chosen])
            self.changed.emit()


class FileTargetDialog(QDialog):
    """Where a file goes in the image, and its mode."""

    def __init__(self, parent, source):
        super().__init__(parent)
        self.setWindowTitle(tr("Add a file to the image"))
        layout = QFormLayout(self)
        layout.addRow(tr("File"), plain_label(printable(source)))
        self.target = QLineEdit("/usr/local/bin/" + os.path.basename(source))
        self.mode = QComboBox()
        self.mode.addItems(["0644", "0755", "0600", "0700"])
        self.mode.setEditable(True)
        layout.addRow(tr("Path in the image"), self.target)
        layout.addRow(tr("Mode"), self.mode)
        buttons = QDialogButtonBox()
        buttons.addButton(tr("Add"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)


class BuildDialog(QDialog):
    """`sandbox-ai build` with its options: only the extension layer, a pinned agent version, uv and Node.js."""

    def __init__(self, parent, target):
        super().__init__(parent)
        self.setWindowTitle(tr("Build {name}", name=printable(target)))
        layout = QFormLayout(self)
        self.extension_only = QCheckBox(tr("Only the extension layers, on the base image there is"))
        self.pin = QLineEdit()
        self.pin.setPlaceholderText(tr("newest; e.g. {example}", example=PIN_EXAMPLE))
        self.pin.setToolTip(tr("Install exactly this agent version (one agent only)"))
        self.uv = QLineEdit()
        self.uv.setPlaceholderText(tr("newest; e.g. {example}", example=UV_EXAMPLE))
        self.node = QLineEdit()
        self.node.setPlaceholderText(tr("the current LTS; a tag or an @sha256 digest"))
        layout.addRow("", self.extension_only)
        layout.addRow(tr("Agent version"), self.pin)
        layout.addRow(tr("uv image tag"), self.uv)
        layout.addRow(tr("Node.js image"), self.node)
        buttons = QDialogButtonBox()
        buttons.addButton(tr("Build"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def options(self):
        return {"extension_only": self.extension_only.isChecked(), "pin": self.pin.text().strip() or None,
                "uv": self.uv.text().strip() or None, "node": self.node.text().strip() or None}


class RecipeDialog(QDialog):
    """`sandbox-ai images recipe media|tor --from AGENT [--as NAME]`: a variant with a ready extension."""

    def __init__(self, parent, agents, agent):
        super().__init__(parent)
        self.setWindowTitle(tr("Variant from a recipe"))
        layout = QFormLayout(self)
        self.recipe = QComboBox()
        for key, label in ((MEDIA, tr("media: sound, video and speech (ffmpeg, PulseAudio, SoX, espeak-ng, Piper)")),
                           (TOR, tr("tor: torsocks and torbrowser-launcher, for a sandbox behind a Tor VPN profile"))):
            self.recipe.addItem(label, key)
        self.agent = QComboBox()
        for name in agents:
            self.agent.addItem(name, name)
        self.agent.setCurrentIndex(max(0, self.agent.findData(agent)))
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("the recipe's name"))
        layout.addRow(tr("Recipe"), self.recipe)
        layout.addRow(tr("Agent"), self.agent)
        layout.addRow(tr("Variant's name"), self.name)
        buttons = QDialogButtonBox()
        buttons.addButton(tr("Create"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)


class NewVariantDialog(QDialog):
    def __init__(self, parent, agents, agent, valid):
        super().__init__(parent)
        self.setWindowTitle(tr("New variant"))
        self.valid = valid
        layout = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("e.g. gpu or web"))
        self.agent = QComboBox()
        for name in agents:
            self.agent.addItem(name, name)
        self.agent.setCurrentIndex(max(0, self.agent.findData(agent)))
        self.copy = QCheckBox(tr("Start from the agent's extension"))
        self.copy.setChecked(True)
        layout.addRow(tr("Name"), self.name)
        layout.addRow(tr("Agent"), self.agent)
        layout.addRow("", self.copy)
        self.problem = plain_label(role="notice-error")
        layout.addRow(self.problem)
        buttons = QDialogButtonBox()
        self.ok = buttons.addButton(tr("Create"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.name.textChanged.connect(self.check)
        self.check()

    def check(self):
        name = self.name.text().strip()
        good = self.valid(name)
        self.problem.setText("" if good or not name else
                             tr("Lower-case letters, digits, '.', '_' and '-', up to 30 characters; not an agent's name."))
        self.ok.setEnabled(good)


# -- the page --

class ImagesPage(QWidget):
    """The agent images and variants (sandbox-ai images), their packages, and the editor of their extensions;
    builds run in a terminal."""

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal, self.loaded = sai, terminal, False
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The images of this machine, or of a host (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        self.entries, self.rows, self.package_list, self.packages_for = [], [], [], None
        self.description, self.target, self.agent_name, self.pending_files, self.files_shown = None, None, None, [], []
        self._module, self.document, self.restyle_queued = False, None, False
        # First: the editor built below calls edited() on its first change, which can come while it is built (a
        # theme's re-highlighting); edited() starts this timer.
        self.check_timer = QTimer(self)
        self.check_timer.setSingleShot(True)
        self.check_timer.setInterval(200)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hosts)
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)

        images = QGroupBox(tr("Images and variants"))
        images_layout = QVBoxLayout(images)
        self.images = table([tr("Image"), tr("Agent"), tr("Built"), tr("Extension"), tr("Agent version"),
                             tr("Created"), tr("Size"), tr("Name")])
        self.images.itemSelectionChanged.connect(self.selection_changed)
        self.asking = False  # selection_changed's question is open
        images_layout.addWidget(self.images)
        row = QHBoxLayout()
        self.refresh_button = button(tr("Refresh"), tr("Ask Podman again"), self.refresh)
        self.build_button = button(tr("Build"), tr("sandbox-ai build: the base image, then the extension layer "
                                                   "(where commands run is set on the System page)"), self.build)
        self.more_builds = QPushButton(tr("Other builds"))
        self.more_builds.setToolTip(safe_tooltip(tr("Build in the app or in a window, only the extension layer, "
                                                    "or every image")))
        menu = QMenu(self.more_builds)
        menu.addAction(tr("Build in the app's terminal"), lambda: self.build(embedded=True))
        menu.addAction(tr("Build in a terminal window"), lambda: self.build(embedded=False))
        menu.addAction(tr("Build only the extension layer"), lambda: self.build(extension_only=True))
        menu.addAction(tr("Build with options…"), self.build_with_options)
        menu.addAction(tr("Build all"), self.build_all)
        self.more_builds.setMenu(menu)
        self.new_button = button(tr("New variant…"), tr("A variant of an agent's image with its own extension: "
                                                         "sandbox-ai images new"), self.new_variant)
        self.delete_button = button(tr("Delete variant…"), tr("Remove the selected variant and its image: "
                                                               "sandbox-ai images remove"), self.delete_variant,
                                    role="danger")
        self.more_button = QPushButton(tr("More"))
        self.more_button.setToolTip(safe_tooltip(tr("A variant from a recipe, the history, a check by sandbox-ai, "
                                                    "dropping an extension")))
        more = QMenu(self.more_button)
        more.addAction(tr("Variant from a recipe…"), self.new_from_recipe)
        more.addAction(tr("History of the extensions"), self.show_history)
        more.addAction(tr("Check the extension with sandbox-ai"), self.check_with_sandbox_ai)
        more.addAction(tr("Delete the variant, keep its image…"), lambda: self.delete_variant(keep_image=True))
        more.addAction(tr("Drop the extension of the agent's image…"), self.drop_extension)
        self.more_button.setMenu(more)
        for widget in (self.refresh_button, self.build_button, self.more_builds, self.new_button,
                       self.delete_button, self.more_button):
            row.addWidget(widget)
        row.addStretch()
        images_layout.addLayout(row)
        self.status = plain_label()
        images_layout.addWidget(self.status)
        splitter.addWidget(images)

        self.tabs = QTabWidget()
        packages = QWidget()
        packages_layout = QVBoxLayout(packages)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("filter (name or version)"))
        self.filter.textChanged.connect(self.show_packages)
        packages_layout.addWidget(self.filter)
        self.packages = table([tr("Kind"), tr("Name"), tr("Version")])
        packages_layout.addWidget(self.packages)
        self.tabs.addTab(packages, tr("Installed packages"))
        self.tabs.addTab(self.build_editor(), tr("Extension"))
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.images.setMinimumHeight(150)
        splitter.setSizes([330, 520])

        self.list_command = AsyncCommand(timeout=200, parent=self)
        self.list_command.finished.connect(self.images_listed)
        self.package_command = AsyncCommand(timeout=330, parent=self)
        self.package_command.finished.connect(self.packages_listed)
        self.show_command = AsyncCommand(timeout=60, parent=self)
        self.show_command.finished.connect(self.extension_shown)
        self.check_timer.timeout.connect(self.revalidate)
        self.set_editing(False)

    def build_editor(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        self.editor_title = plain_label(tr("Select an image or a variant to edit its extension."), role="secondary")
        layout.addWidget(self.editor_title)
        self.editor_tabs = QTabWidget()
        self.lists = {}
        for kind, label in (("apt", tr("Debian packages")), ("npm", tr("npm packages")), ("pip", tr("Python tools"))):
            self.lists[kind] = PackageList(kind, self)
            self.lists[kind].changed.connect(self.edited)
            self.editor_tabs.addTab(self.lists[kind], label)
        files = QWidget()
        files_layout = QVBoxLayout(files)
        self.files = table([tr("File"), tr("Path in the image"), tr("Mode"), tr("State")], stretch=1)
        files_layout.addWidget(self.files)
        row = QHBoxLayout()
        row.addWidget(button(tr("Add file…"), tr("Copy a host file into the extension (from a folder no sandbox "
                                                 "can write)"), self.add_file))
        row.addWidget(button(tr("Remove"), tr("Take the selected file out of the image"), self.remove_file))
        row.addStretch()
        files_layout.addLayout(row)
        self.editor_tabs.addTab(files, tr("Files"))
        fragment = QSplitter(Qt.Orientation.Vertical)
        self.fragment = CodeEditor()
        self.fragment.setPlaceholderText(tr("RUN, ENV, COPY files/NAME … lines, added after the packages"))
        self.fragment.textChanged.connect(self.edited)
        fragment.addWidget(self.fragment)
        self.findings = QListWidget()
        self.findings.setWordWrap(True)
        self.findings.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.findings.itemActivated.connect(self.jump)
        self.findings.itemClicked.connect(self.jump)
        fragment.addWidget(self.findings)
        fragment.setStretchFactor(0, 4)
        fragment.setStretchFactor(1, 1)
        self.editor_tabs.addTab(fragment, tr("Containerfile fragment"))
        self.preview = CodeEditor(read_only=True)
        self.editor_tabs.addTab(self.preview, tr("Final Containerfile"))
        layout.addWidget(self.editor_tabs, 1)
        row = QHBoxLayout()
        self.save_button = button(tr("Save"), tr("Save the extension through sandbox-ai images extend"), self.save,
                                  role="primary")
        self.revert_button = button(tr("Revert"), tr("Drop the changes not saved yet"), self.revert)
        self.task = Task()
        self.verdict = plain_label(role="secondary")
        row.addWidget(self.save_button)
        row.addWidget(self.revert_button)
        row.addWidget(self.verdict, 1)
        layout.addLayout(row)
        layout.addWidget(self.task)
        return box

    # -- sandbox-ai's images module --

    def module(self):
        """sandbox-ai's images module when it knows extensions, else None (an older sandbox-ai)."""
        if self._module is False:
            try:
                module = self.sai.module("images")
                self._module = module if hasattr(module, "check_fragment") and hasattr(module, "containerfile") \
                    else None
            except Exception:  # noqa: BLE001 - an installation that cannot be read has no editor
                self._module = None
        return self._module

    # -- the list of images --

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange, QEvent.Type.PaletteChange) and \
                not self.restyle_queued:
            self.restyle_queued = True  # a new theme: its status colours and font, once for all three events
            QTimer.singleShot(0, self.restyle)

    def restyle(self):
        self.restyle_queued = False
        if self.document is not None:
            self.show_images(self.document)
        self.revalidate()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded:
            self.loaded = True
            self.refresh()

    def host(self):
        return self.hosts.host() or LOCAL

    def run_argv(self, *args):
        """argv of `sandbox-ai ARGS` on the host chosen (without a terminal)."""
        return self.sai.host_command(self.host(), *args)

    def host_changed(self):
        """Another host: nothing of the last one stays (its lists, its extension in the editor)."""
        for command in (self.list_command, self.package_command, self.show_command):
            command.stop()
        self.entries, self.rows, self.package_list, self.packages_for, self.document = [], [], [], None, None
        self.description, self.target = None, None
        fill(self.images, [])
        self.show_packages()
        self.set_editing(False, tr("Select an image or a variant to edit its extension."))
        self.update_buttons()
        if self.loaded:
            self.refresh()

    def refresh(self):
        if self.list_command.start(self.run_argv("images", "--json")):
            self.status.setText(tr("Asking Podman…") if self.host() == LOCAL else
                                tr("Asking {host}…", host=self.hosts.label()))

    def images_listed(self, status, out, err):
        try:
            document = json.loads(out) if status == 0 else None
        except ValueError:
            document = None
        if document is None:
            self.status.setText(tr("Cannot list the images: {error}", error=printable((err or out).strip()[-300:])))
            return
        self.show_images(document)

    def show_images(self, document):
        self.document = document
        before = self.agent()
        self.entries = image_entries(document)
        self.rows = [(e["target"], e["present"]) for e in self.entries]
        # Filled without selection signals (a refresh may come back while a question of selection_changed is
        # open); what the refill changed about the selection is handled once, afterwards.
        self.images.blockSignals(True)
        try:
            fill(self.images, [[e["target"], e["agent"], tr("yes") if e["present"] else tr("not built"),
                                status_text(e), e["version"], e["created"], e["size"], e["image"]]
                               for e in self.entries],
                 [status_color(e) if e["present"] else "warn" for e in self.entries],
                 keys=[e["target"] for e in self.entries])
        finally:
            self.images.blockSignals(False)
        if self.agent() != before:
            self.selection_changed()
        built = sum(1 for e in self.entries if e["present"])
        stale = sum(1 for e in self.entries if e["present"] and e["status"] in ("outdated", "base-changed"))
        text = tr("{count} of {total} images built", count=built, total=len(self.entries))
        if stale:
            text += " · " + tr("{count} to rebuild for their extension", count=stale)
        self.status.setText(text)
        self.update_buttons()

    def entry(self):
        keys = selected_keys(self.images)
        return next((e for e in self.entries if keys and e["target"] == keys[0]), None)

    def agent(self):
        entry = self.entry()
        return entry["target"] if entry else None

    def update_buttons(self):
        entry = self.entry()
        self.build_button.setEnabled(entry is not None)
        self.delete_button.setEnabled(bool(entry and entry["variant"]))

    def selection_changed(self):
        if self.asking:  # the question below is open: its answer decides where the selection goes
            return
        self.update_buttons()
        self.load_packages()
        entry = self.entry()
        if entry and entry["target"] != self.target:
            if self.dirty():
                self.asking = True
                try:
                    discard = confirm(self, tr("Images"), tr("Discard the changes to the extension of {name}?",
                                                             name=self.target), tr("Discard"))
                finally:
                    self.asking = False
                if not discard:
                    self.images.blockSignals(True)
                    select_keys(self.images, [self.target])
                    self.images.blockSignals(False)
                    return
            self.load_extension(entry["target"])

    def build(self, embedded=None, extension_only=False):
        target = self.agent()
        if not target:
            warn(self, tr("Images"), tr("Select an image first."))
            return
        if self.dirty() and target == self.target:
            warn(self, tr("Images"), tr("Save the extension first: a build uses what is saved."))
            return
        self.start_build(target, {"extension_only": extension_only}, embedded)

    rebuild = build

    def start_build(self, target, options, embedded=None):
        """`sandbox-ai build [OPTIONS] TARGET` in a terminal on the host chosen (ssh -tt for another host)."""
        host = self.host()
        try:
            argv = self.sai.command("build", *(["--extension-only"] if options.get("extension_only") else []),
                                    target) if host == LOCAL and not any(
                options.get(k) for k in ("pin", "uv", "node")) else self.sai.image_build_argv(host, target, **options)
        except ValueError as exc:
            warn(self, tr("Images"), printable(exc))
            return
        title = "sandbox-ai build " + ("--extension-only " if options.get("extension_only") else "") + target
        title += "" if host == LOCAL else f" ({host})"
        if embedded is None:
            self.terminal(argv, title)
        else:
            self.terminal(argv, title, embedded=embedded)

    def build_with_options(self):
        target = self.agent()
        if not target:
            warn(self, tr("Images"), tr("Select an image first."))
            return
        if self.dirty() and target == self.target:
            warn(self, tr("Images"), tr("Save the extension first: a build uses what is saved."))
            return
        dialog = BuildDialog(self, target)
        if dialog.exec():
            self.start_build(target, dialog.options())

    def build_all(self):
        host = self.host()
        argv = self.sai.command("build", "all") if host == LOCAL else self.sai.host_terminal(host, "build", "all")
        self.terminal(argv, "sandbox-ai build all" + ("" if host == LOCAL else f" ({host})"))

    # -- installed packages --

    def load_packages(self):
        target = self.agent()
        if not target or target == self.packages_for:
            return
        present = any(name == target and built for name, built in self.rows)
        self.packages_for, self.package_list = target, []
        self.show_packages()
        if not present:
            self.status.setText(tr("The {agent} image is not built.", agent=target))
            return
        self.package_command.stop()
        if self.package_command.start(self.run_argv("images", "packages", target, "--json")):
            self.status.setText(tr("Reading the packages of {agent}…", agent=target))

    def packages_listed(self, status, out, err):
        try:
            document = json.loads(out) if status == 0 else None
        except ValueError:
            document = None
        if document is None:
            self.status.setText(tr("Cannot read the packages: {error}", error=printable((err or out).strip()[-300:])))
            return
        self.package_list = [(str(p.get("kind", "")), str(p.get("name", "")), str(p.get("version", "")))
                             for p in (document if isinstance(document, list) else []) if isinstance(p, dict)]
        self.status.setText(tr("{count} packages in {agent}", count=len(self.package_list), agent=self.packages_for))
        self.show_packages()

    def show_packages(self):
        text = self.filter.text().lower()
        fill(self.packages, [[printable(v) for v in row] for row in self.package_list
                             if not text or text in " ".join(row).lower()])

    # -- the extension editor --

    def set_editing(self, enabled, why=None):
        for widget in (self.editor_tabs, self.save_button, self.revert_button):
            widget.setEnabled(enabled)
        if why:
            self.editor_title.setText(why)

    def load_extension(self, target):
        if self.module() is None:
            self.set_editing(False, tr("This sandbox-ai cannot extend images yet; update it (sandbox-ai update)."))
            return
        self.show_command.stop()
        self.target, self.description = target, None
        self.set_editing(False, tr("Reading the extension of {name}…", name=target))
        self.show_command.start(self.run_argv("images", "extend", target, "--show", "--json"))

    def extension_shown(self, status, out, err):
        try:
            document = json.loads(out) if status == 0 else None
        except ValueError:
            document = None
        if not isinstance(document, dict) or "agent" not in document:
            self.set_editing(False, tr("Cannot read the extension: {error}",
                                       error=printable((err or out).strip()[-300:])))
            return
        self.show_description(document)

    def show_description(self, document):
        self.description, self.target, self.agent_name = document, document.get("target"), document.get("agent")
        self.pending_files = []
        for kind in KINDS:
            self.lists[kind].set_values([v for v in document.get(kind, []) if isinstance(v, str)])
        self.files_shown = [dict(e) for e in document.get("files", []) if isinstance(e, dict)]
        self.fragment.blockSignals(True)
        self.fragment.setPlainText(document.get("fragment") or "")
        self.fragment.blockSignals(False)
        what = tr("Variant {name} of {agent}", name=printable(self.target), agent=printable(self.agent_name)) \
            if document.get("variant") else tr("Extension of the {agent} image", agent=printable(self.agent_name))
        self.set_editing(True, f"{what} → {printable(document.get('image', ''))}")
        self.show_files()
        self.revalidate()

    def show_files(self):
        rows = [[printable(e.get("source", "")), printable(e.get("target", "")), printable(e.get("mode", "")), ""]
                for e in self.files_shown]
        rows += [[printable(os.path.basename(source)), printable(target), mode, tr("new")]
                 for source, target, mode in self.pending_files]
        fill(self.files, rows, keys=[e.get("target") for e in self.files_shown] + [t for _, t, _ in self.pending_files])

    def add_file(self):
        if self.description is None:
            return
        if self.host() != LOCAL:
            # A file of that host: its path there, typed (this machine's file dialog cannot see it).
            from PySide6.QtWidgets import QInputDialog
            source, ok = QInputDialog.getText(self, tr("File to copy into the image"), tr(
                "The file's absolute path on {host}:", host=self.hosts.label()))
            source = source.strip() if ok else ""
            if source and (not source.startswith("/") or ":" in source or not source.isprintable()):
                warn(self, tr("Images"), tr("Not an absolute path without ':': {path}", path=printable(source)))
                return
        else:
            from .state import choose_file
            source = choose_file(self, tr("File to copy into the image"), "image-file")
        if not source:
            return
        dialog = FileTargetDialog(self, source)
        if dialog.exec():
            self.add_file_entry(source, dialog.target.text().strip(), dialog.mode.currentText().strip())

    def add_file_entry(self, source, target, mode):
        if not target.startswith("/") or not re.fullmatch(r"0?[0-7]{3}|[0-7]{4}", mode):
            warn(self, tr("Images"), tr("The path in the image is absolute, the mode an octal number such as 0644."))
            return False
        self.pending_files = [p for p in self.pending_files if p[1] != target] + [(source, target, mode.zfill(4))]
        self.files_shown = [e for e in self.files_shown if e.get("target") != target]
        self.show_files()
        self.edited()
        return True

    def remove_file(self):
        chosen = set(selected_keys(self.files))
        if chosen:
            self.files_shown = [e for e in self.files_shown if e.get("target") not in chosen]
            self.pending_files = [p for p in self.pending_files if p[1] not in chosen]
            self.show_files()
            self.edited()

    def state(self):
        """(document, fragment, file names) as edited, in the form sandbox-ai's images module checks."""
        files = list(self.files_shown) + [
            {"source": re.sub(r"[^A-Za-z0-9._+-]", "_", os.path.basename(s)).lstrip("._-+") or "file",
             "target": t, "mode": m} for s, t, m in self.pending_files]
        document = {"version": 1, "agent": self.agent_name, **{k: self.lists[k].values() for k in KINDS},
                    "files": files}
        fragment = self.fragment.toPlainText()
        used = re.findall(r"(?<![\w./-])files/([A-Za-z0-9._+-]+)", fragment)
        available = set((self.description or {}).get("available", [])) | {e["source"] for e in files} | \
            ({u for u in used if u in (self.description or {}).get("available", [])})
        return document, fragment, available

    def dirty(self):
        if self.description is None:
            return False
        d = self.description
        return any(self.lists[k].values() != d.get(k, []) for k in KINDS) or bool(self.pending_files) or \
            [e.get("target") for e in self.files_shown] != [e.get("target") for e in d.get("files", [])] or \
            self.fragment.toPlainText() != (d.get("fragment") or "")

    def edited(self):
        self.check_timer.start()

    def revalidate(self):
        """Check what is in the editor now (sandbox-ai's own validator, no command) and update the findings,
        the underlines and the preview."""
        module = self.module()
        if module is None or self.description is None:
            return
        document, fragment, available = self.state()
        fragment_findings = module.check_fragment(fragment, available)
        findings = module.check_document(document, available) + fragment_findings
        self.fragment.set_findings(fragment_findings)
        self.findings.clear()
        for item in findings:
            entry = QListWidgetItem(theme.dot("bad" if item["severity"] == "error" else "warn"), finding_text(item))
            entry.setToolTip(safe_tooltip(finding_text(item)))
            entry.setData(Qt.ItemDataRole.UserRole, item)
            self.findings.addItem(entry)
        if not findings:
            self.findings.addItem(QListWidgetItem(theme.dot("ok"), tr("No findings.")))
        self.preview.setPlainText(module.containerfile(self.target, document, fragment))
        errors = sum(1 for f in findings if f["severity"] == "error")
        dirty = self.dirty()
        self.save_button.setEnabled(dirty and not errors)
        self.revert_button.setEnabled(dirty)
        verdict = tr("{errors} error(s), {warnings} warning(s)", errors=errors, warnings=len(findings) - errors)
        if dirty:
            verdict += " · " + (tr("errors must be fixed before saving") if errors else tr("not saved"))
        self.verdict.setText(verdict)
        self.verdict.setProperty("role", "notice-error" if errors else "secondary")
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)
        self.editor_tabs.setTabText(4, tr("Containerfile fragment") + (f" ({errors})" if errors else ""))

    def jump(self, item):
        finding = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(finding, dict) and finding.get("line"):
            self.editor_tabs.setCurrentIndex(4)
            self.fragment.go_to(finding["line"], finding.get("column") or 0)
        elif isinstance(finding, dict) and finding.get("where") in KINDS + ("files",):
            self.editor_tabs.setCurrentIndex((*KINDS, "files").index(finding["where"]))

    def save_argv(self):
        """(argv, standard input) of the sandbox-ai command that saves what is in the editor."""
        d, args = self.description, []
        for kind in KINDS:
            old, new = d.get(kind, []), self.lists[kind].values()
            args += [a for value in old if value not in new for a in (f"--remove-{kind}", value)]
            args += [a for value in new if value not in old for a in (f"--add-{kind}", value)]
        kept = {e.get("target") for e in self.files_shown}
        args += [a for e in d.get("files", []) if e.get("target") not in kept for a in ("--remove-file", e["target"])]
        args += [a for s, t, m in self.pending_files for a in ("--add-file", f"{s}:{t}:{m}")]
        fragment = self.fragment.toPlainText()
        stdin = None
        if fragment != (d.get("fragment") or ""):
            args += ["--fragment", "-"] if fragment.strip() else ["--clear-fragment"]
            stdin = fragment.encode() if fragment.strip() else None
        return self.run_argv("images", "extend", self.target, *args, "--json"), stdin

    def save(self):
        if self.description is None or not self.dirty():
            return
        argv, stdin, host = *self.save_argv(), self.host()
        # A host file built into the image reaches every later start of it: asked first (image-file).
        if self.pending_files and not ask_first(self, "image-file", tr("Save the extension"), on_host(host, tr(
                "Build {files} into the image {name}? Every later start of it can read them.",
                files=", ".join(printable(f"{s} -> {t}") for s, t, _m in self.pending_files), name=self.target)),
                tr("Save")):
            return
        self.task.run(argv, lambda status, out, err: self.saved(status, out, err, host),
                      tr("Saving the extension of {name}…", name=self.target), self.save_button, input=stdin)

    def saved(self, status, out, err, host=None):
        if host is not None and host != self.host():
            return  # saved on the host before: its extension is never shown under this one (the lists were read again)
        try:
            document = json.loads(out) if out.strip() else None
        except ValueError:
            document = None
        if status == 0 and isinstance(document, dict) and "agent" in document:
            self.show_description(document)
            self.verdict.setText(tr("Saved. Build it to use it: {command}", command=f"sandbox-ai build {self.target}"))
            self.refresh()
            return
        if status == -2:
            return
        if isinstance(document, dict) and isinstance(document.get("findings"), list):
            self.fragment.set_findings(document["findings"])
        warn(self, tr("Images"), tr("sandbox-ai did not save the extension."),
             details=printable((err or out).strip()[-3000:]))
        self.revalidate()

    def revert(self):
        if self.description is not None:
            self.show_description(self.description)

    # -- variants --

    def new_variant(self):
        module = self.module()
        if module is None:
            warn(self, tr("Images"), tr("This sandbox-ai cannot extend images yet; update it (sandbox-ai update)."))
            return
        entry = self.entry()
        dialog = NewVariantDialog(self, self.sai.agents, entry["agent"] if entry else self.sai.agents[0],
                                  module.valid_name)
        if dialog.exec():
            self.create_variant(dialog.name.text().strip(), dialog.agent.currentData(), not dialog.copy.isChecked())

    def create_variant(self, name, agent, empty=False):
        argv = self.run_argv("images", "new", name, "--from", agent, *(["--empty"] if empty else []), "--json")
        host = self.host()
        self.task.run(argv, lambda status, out, err: self.variant_done(status, out, err, name, host),
                      tr("Creating the variant {name}…", name=name), self.new_button)

    def variant_done(self, status, out, err, name, host=None):
        if status == -2 or (host is not None and host != self.host()):
            return
        if status != 0:
            warn(self, tr("Images"), tr("sandbox-ai did not create the variant {name}.", name=name),
                 details=printable((err or out).strip()[-3000:]))
            return
        self.refresh()
        try:
            self.show_description(json.loads(out))
        except (ValueError, TypeError, AttributeError):
            pass

    def delete_variant(self, keep_image=False):
        entry = self.entry()
        if not entry or not entry["variant"]:
            if keep_image:
                warn(self, tr("Images"), tr("Select a variant first."))
            return
        text = tr("Delete the variant {name}? Its image {image} stays; projects that start it with --image-variant "
                  "stop working.", name=entry["target"], image=entry["image"]) if keep_image else \
            tr("Delete the variant {name} and its image {image}? Projects that start it with --image-variant stop "
               "working.", name=entry["target"], image=entry["image"])
        host = self.host()
        if not ask_first(self, "image-remove", tr("Delete variant"), on_host(host, text), tr("Delete")):
            return
        self.task.run(self.run_argv("images", "remove", entry["target"], *(["--keep-image"] if keep_image else [])),
                      lambda status, out, err: self.deleted(status, out, err, entry["target"], host),
                      tr("Deleting the variant {name}…", name=entry["target"]), self.delete_button)

    def deleted(self, status, out, err, name, host=None):
        if host is not None and host != self.host():
            return  # removed on the host before: this host's lists are read already
        if status not in (0, -2):
            warn(self, tr("Images"), tr("sandbox-ai did not delete the variant {name}.", name=name),
                 details=printable((err or out).strip()[-3000:]))
        if self.target == name:
            self.description, self.target = None, None
            self.set_editing(False, tr("Select an image or a variant to edit its extension."))
        self.refresh()

    # -- more: recipes, history, sandbox-ai's own check, dropping an extension --

    def new_from_recipe(self):
        entry = self.entry()
        dialog = RecipeDialog(self, self.sai.agents, entry["agent"] if entry else self.sai.agents[0])
        if not dialog.exec():
            return
        name = dialog.name.text().strip()
        module = self.module()
        if name and module is not None and not module.valid_name(name):
            warn(self, tr("Images"), tr("Lower-case letters, digits, '.', '_' and '-', up to 30 characters; not an "
                                        "agent's name."))
            return
        recipe, agent = dialog.recipe.currentData(), dialog.agent.currentData()
        argv = self.run_argv("images", "recipe", recipe, "--from", agent, *([f"--as={name}"] if name else []))
        self.task.run(argv, lambda status, out, err: self.recipe_done(status, out, err, recipe),
                      tr("Creating the variant {name}…", name=name or recipe), self.more_button)

    def recipe_done(self, status, out, err, recipe):
        if status == -2:
            return
        if status != 0:
            warn(self, tr("Images"), tr("sandbox-ai did not create the variant {name}.", name=recipe),
                 details=printable((err or out).strip()[-3000:]))
            return
        inform(self, tr("Images"), printable(out.strip()[-3000:]) or tr("Created."), modal=False)
        self.refresh()

    def show_history(self):
        """`sandbox-ai images history [TARGET]`: changes and builds of the extensions (the selected one's, or all)."""
        target = self.agent()
        self.task.run(self.run_argv("images", "history", *([target] if target else [])),
                      lambda status, out, err: self.history_shown(status, out, err, target),
                      tr("Reading the history…"), self.more_button)

    def history_shown(self, status, out, err, target):
        if status == -2:
            return
        text = "\n".join(printable(line) for line in (out if status == 0 else err or out).strip()[-20000:]
                         .splitlines())
        show_text(self, tr("History of {name}", name=target) if target else tr("History of the extensions"),
                  text or tr("Nothing recorded yet."), modal=False)

    def check_with_sandbox_ai(self):
        """`sandbox-ai images check TARGET [--fragment -] --json` on the host chosen: its own verdict and final
        Containerfile (the fragment in the editor on standard input)."""
        if self.description is None or not self.target:
            warn(self, tr("Images"), tr("Select an image first."))
            return
        fragment = self.fragment.toPlainText()
        stdin = fragment.encode() if fragment.strip() else None
        argv = self.run_argv("images", "check", self.target, *(["--fragment", "-"] if stdin else []), "--json")
        self.task.run(argv, self.checked, tr("Checking the extension of {name}…", name=self.target),
                      self.more_button, input=stdin)

    def checked(self, status, out, err):
        if status == -2:
            return
        try:
            document = json.loads(out) if out.strip() else None
        except ValueError:
            document = None
        if not isinstance(document, dict):
            warn(self, tr("Images"), tr("sandbox-ai did not check the extension."),
                 details=printable((err or out).strip()[-3000:]))
            return
        findings = [f for f in document.get("findings") or [] if isinstance(f, dict)]
        lines = [finding_text(f) for f in findings] or [tr("No findings.")]
        if isinstance(document.get("containerfile"), str):
            lines += ["", printable(document["containerfile"])]
        show_text(self, tr("Check of {name}", name=self.target), "\n".join(lines), modal=False)

    def drop_extension(self):
        """`sandbox-ai images remove AGENT`: the agent's image goes back to its base at the next build."""
        entry = self.entry()
        if not entry or entry["variant"] or not entry.get("extended"):
            warn(self, tr("Images"), tr("Select an agent's image with an extension first."))
            return
        host = self.host()
        if not ask_first(self, "image-remove", tr("Drop the extension"), on_host(host, tr(
                "Drop the extension of {name}? Its packages, files and fragment are removed; the next build is the "
                "base image.", name=entry["target"])), tr("Drop")):
            return
        self.task.run(self.run_argv("images", "remove", entry["target"]),
                      lambda status, out, err: self.deleted(status, out, err, entry["target"], host),
                      tr("Dropping the extension of {name}…", name=entry["target"]), self.more_button)
