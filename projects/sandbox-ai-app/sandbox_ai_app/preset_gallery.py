"""The preset gallery of the Board page: cards by category, each with a small picture of its graph, its
security score and what is left to fill in; a search box; Enter or a double-click creates the board. The
placeholders of a preset (a VPN profile, an LLM server, an MCP server, a folder) are asked for in a short form
that offers what `sandbox-ai vpn list`, `sandbox-ai llm list` and the MCP servers page know, or are left to be
filled in on the board. The user's own presets (the current board saved under a name) are listed under
"My presets", and can be exported and imported as JSON files (checked: never a secret).
"""
from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGraphicsScene, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from . import board as B
from . import presets as PR
from . import theme, widgets
from .board_view import NODE_H, NODE_W, PathEdit, elide
from .files import printable
from .i18n import tr
from .state import choose_file
from .widgets import MONO, FlowLayout, background, confirm, inform, plain_label, safe_tooltip, warn

THUMB = QSize(248, 124)


def thumbnail(board, size=THUMB, ratio=1.0):
    """A small picture of BOARD in the current theme's colours: the canvas, the nodes as cards with their
    title band (a part with a placeholder outlined in the warning colour), the edges."""
    colors, data = theme.colors(), theme.data()
    radius = float(min(data["radius"], 12)) * 1.6
    scene = QGraphicsScene()
    marked = {node_id for _, node_id in B.placeholders(board)}
    nodes = {n.id: n for n in board.nodes}
    edge_pen = QPen(QColor(colors["secondary"]), 7.0)
    for source, target in sorted(board.edges):
        a, b = nodes.get(source), nodes.get(target)
        if a is None or b is None:
            continue
        start, end = QPointF(a.x + NODE_W, a.y + NODE_H / 2), QPointF(b.x, b.y + NODE_H / 2)
        bend = max(60.0, abs(end.x() - start.x()) / 2)
        path = QPainterPath(start)
        path.cubicTo(QPointF(start.x() + bend, start.y()), QPointF(end.x() - bend, end.y()), end)
        scene.addPath(path, edge_pen)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(30)
    for node in board.nodes:
        rect = QRectF(node.x, node.y, NODE_W, NODE_H)
        body = QPainterPath()
        body.addRoundedRect(rect, radius, radius)
        outline = theme.color("warn") if node.id in marked else colors["border"]
        item = scene.addPath(body, QPen(QColor(outline), 7.0 if node.id in marked else 3.0),
                             QBrush(QColor(colors["node"])))
        item.setZValue(1)
        header = QPainterPath()
        header.addRect(QRectF(rect.left(), rect.top(), rect.width(), 40.0))
        band = QColor(colors["accent"] if node.kind == "agent" else colors["node_header"])
        scene.addPath(header.intersected(body), QPen(Qt.PenStyle.NoPen), QBrush(band)).setZValue(2)
        text = scene.addSimpleText(elide(tr(B.KINDS[node.kind].title), 14), font)
        text.setBrush(QBrush(QColor(colors["accent_text"] if node.kind == "agent"
                                    else colors.get("node_title", colors["text"]))))
        text.setPos(rect.left() + 12, rect.top() + 3)
        text.setZValue(3)
        bar = QPainterPath()  # the detail line, as a bar
        bar.addRoundedRect(QRectF(rect.left() + 12, rect.top() + 48, rect.width() * 0.6, 8), 4, 4)
        scene.addPath(bar, QPen(Qt.PenStyle.NoPen), QBrush(QColor(colors["secondary"]))).setZValue(2)
    pixmap = QPixmap(int(size.width() * ratio), int(size.height() * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(QColor(colors["canvas"]))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    source = scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
    width, height = size.width() - 8.0, size.height() - 8.0
    scale = min(width / max(source.width(), 1.0), height / max(source.height(), 1.0))
    target = QRectF(4 + (width - source.width() * scale) / 2, 4 + (height - source.height() * scale) / 2,
                    source.width() * scale, source.height() * scale)  # centred
    scene.render(painter, target, source, Qt.AspectRatioMode.KeepAspectRatio)
    painter.end()
    return pixmap


class PresetCard(QFrame):
    """One preset: its picture, title, description, difficulty, score and what is left to fill in."""
    chosen = Signal(object)  # clicked: selected
    opened = Signal(object)  # double-clicked: created

    def __init__(self, preset, board, score):
        super().__init__()
        self.preset = preset
        self.setProperty("role", "card")
        self.setFixedWidth(THUMB.width() + 22)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)
        self.picture = QLabel()
        self.picture.setPixmap(thumbnail(board, ratio=self.devicePixelRatioF() or 1.0))
        self.picture.setFixedSize(THUMB)
        layout.addWidget(self.picture)
        layout.addWidget(plain_label(preset.text(preset.title), role="section"))
        description = plain_label(preset.text(preset.description), role="secondary")
        description.setMinimumHeight(description.fontMetrics().lineSpacing() * 3)
        description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(description)
        to_fill = len(preset.placeholders)
        meta = [tr(PR.DIFFICULTIES.get(preset.difficulty, preset.difficulty)),
                tr("score {score}", score=score)]
        if to_fill:
            meta.append(tr("{count} to fill in", count=to_fill))
        self.meta = plain_label(" · ".join(meta), role="secondary")
        layout.addWidget(self.meta)
        layout.addStretch()
        self.setToolTip(safe_tooltip("\n".join(preset.text(n) for n in preset.notes)))
        self.setAccessibleName(preset.text(preset.title))
        self.select(False)

    def select(self, on):
        self.selected = on
        accent = theme.colors()["accent"]
        self.setStyleSheet(f'QFrame[role="card"] {{ border: 2px solid {accent}; }}' if on else "")

    def mousePressEvent(self, event):  # noqa: N802
        self.chosen.emit(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.chosen.emit(self)
        self.opened.emit(self)

    def focusInEvent(self, event):  # noqa: N802
        self.chosen.emit(self)
        super().focusInEvent(event)


class PresetGallery(QDialog):
    """Choose a preset; created() gives (preset, values or None: fill in later on the board)."""

    def __init__(self, sai, directory=None, agent=None, policy=None, board=None, parent=None, backend_name=None):
        super().__init__(parent)
        self.sai, self.directory, self.agent, self.policy = sai, directory, agent, policy or {}
        self.current_board, self.backend_name = board, backend_name
        self.result_ = None  # (preset, values) once created
        self.setWindowTitle(tr("New from preset"))
        self.resize(1180, 760)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search: vpn, mcp, offline, llm, gpu…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter)
        from . import help_view
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        search_row.addWidget(help_view.HelpButton("gallery.cards"))
        layout.addLayout(search_row)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        split.addWidget(self.scroll)
        details = QWidget()
        side = QVBoxLayout(details)
        side.setContentsMargins(8, 0, 0, 0)
        self.title = plain_label(role="title")
        self.description = plain_label()
        self.meta = plain_label(role="secondary")
        self.notes = plain_label(role="secondary")
        self.fill_in = plain_label()
        self.command = QPlainTextEdit()
        self.command.setReadOnly(True)
        self.command.setFont(MONO)
        self.command.setMaximumHeight(110)
        for widget in (self.title, self.description, self.meta, self.fill_in, self.notes):
            side.addWidget(widget)
        side.addWidget(plain_label(tr("Command line"), role="section"))
        side.addWidget(self.command)
        side.addStretch()
        split.addWidget(details)
        split.setSizes([840, 340])
        layout.addWidget(split, 1)
        self.problem = plain_label(role="notice-warn")
        self.problem.hide()
        layout.addWidget(self.problem)
        row = QHBoxLayout()
        save = QPushButton(tr("Save the board as a preset…"))
        save.setToolTip(tr("Keep the board shown now as a preset of your own (no workspaces, no secrets)"))
        save.clicked.connect(lambda: self.save_board())
        save.setEnabled(board is not None)
        load = QPushButton(tr("Import…"))
        load.setToolTip(tr("Add a preset from a JSON file (checked: options, servers, rules; never a secret)"))
        load.clicked.connect(lambda: self.import_preset())
        self.export_button = QPushButton(tr("Export…"))
        self.export_button.setToolTip(tr("Write the selected preset to a JSON file to pass on"))
        self.export_button.clicked.connect(lambda: self.export_preset())
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setProperty("role", "danger")
        self.delete_button.clicked.connect(lambda: self.delete_preset())
        for widget in (save, load, self.export_button, self.delete_button):
            row.addWidget(widget)
        row.addWidget(help_view.HelpButton("gallery.mine"))
        row.addStretch()
        buttons = QDialogButtonBox()
        self.create_button = buttons.addButton(tr("Create"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.create_button.setProperty("role", "primary")
        self.create_button.setDefault(True)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(lambda: self.create())
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)
        self.cards, self.sections, self.selected = [], [], None
        self.populate()

    # -- the cards --

    def presets(self):
        mine, problems = PR.load_mine(self.sai)
        own, own_problems = PR.sandbox_ai_presets(self.sai)
        problems = problems + own_problems
        self.mine = mine
        if problems:
            self.problem.setText(tr("Some of your presets were left out: {problems}", problems="; ".join(problems)))
        self.problem.setVisible(bool(problems))
        return [(key, title, own if key == PR.SAI_CATEGORY else [p for p in PR.PRESETS if p.category == key])
                for key, title in PR.CATEGORIES] + [("mine", PR.MINE, mine)]

    def populate(self, select=None):
        holder = QWidget()
        column = QVBoxLayout(holder)
        self.cards, self.sections, self.selected = [], [], None  # the cards shown until now go
        for key, title, presets in self.presets():
            if key == PR.SAI_CATEGORY and not presets:
                continue  # a sandbox-ai without presets of its own: no empty section
            heading = plain_label(tr(title), role="section")
            box = QWidget()
            flow = FlowLayout(box, spacing=10)
            cards = []
            for preset in presets:
                board = self.build(preset)
                card_ = PresetCard(preset, board, B.badges(board, self.policy)[0])
                card_.chosen.connect(self.choose)
                card_.opened.connect(lambda _card: self.create())
                flow.addWidget(card_)
                cards.append(card_)
            if key == "mine" and not presets:
                empty = plain_label(tr("None yet: “Save the board as a preset…” keeps the board shown now."),
                                    role="secondary")
                flow.addWidget(empty)
            column.addWidget(heading)
            column.addWidget(box)
            self.sections.append((heading, box, cards, key))
            self.cards += cards
        column.addStretch()
        self.scroll.setWidget(holder)
        wanted = next((c for c in self.cards if c.preset.id == select), None) or (self.cards[0] if self.cards else None)
        if wanted is not None:
            self.choose(wanted)
        self.filter(self.search.text())
        if select is None:
            self.scroll.verticalScrollBar().setValue(0)

    def build(self, preset, values=None):
        return PR.build(self.sai, preset, self.directory, agent=self.agent, values=values,
                        backend=self.backend_name)

    def visible_cards(self):
        return [c for c in self.cards if not c.isHidden()]

    def filter(self, text):
        for heading, box, cards, key in self.sections:
            shown = 0
            for card_ in cards:
                card_.setVisible(PR.matches(card_.preset, text))
                shown += not card_.isHidden()
            visible = bool(shown) or (key == "mine" and not text.strip())
            heading.setVisible(visible)
            box.setVisible(visible)
        if self.selected is None or self.selected.isHidden():
            visible = self.visible_cards()
            self.choose(visible[0] if visible else None)

    def choose(self, card_):
        if self.selected is not None and self.selected is not card_:
            self.selected.select(False)
        self.selected = card_
        self.create_button.setEnabled(card_ is not None)
        self.export_button.setEnabled(card_ is not None)
        self.delete_button.setEnabled(card_ is not None and card_.preset.user)
        if card_ is None:
            self.title.setText(tr("No preset matches"))
            for widget in (self.description, self.meta, self.notes, self.fill_in):
                widget.setText("")
            self.command.setPlainText("")
            return
        card_.select(True)
        preset = card_.preset
        board = self.build(preset)
        score = B.badges(board, self.policy)[0]
        category = dict(PR.CATEGORIES).get(preset.category, preset.category)
        self.title.setText(preset.text(preset.title))
        self.description.setText(preset.text(preset.description))
        self.meta.setText(" · ".join([tr(category), tr(PR.DIFFICULTIES.get(preset.difficulty, preset.difficulty)),
                                      tr("Security score: {score} of 100", score=score)]))
        lines = [f"{p.marker}: {preset.text(p.label)}" + (" " + tr("(optional)") if p.optional else "")
                 for p in preset.placeholders]
        self.fill_in.setText(tr("To fill in:") + "\n" + "\n".join(lines) if lines else tr("Nothing to fill in."))
        self.notes.setText("\n\n".join(preset.text(n) for n in preset.notes))
        self.command.setPlainText(B.command_line(self.sai, board))
        self.scroll.ensureWidgetVisible(card_)

    def keyPressEvent(self, event):  # noqa: N802
        """Arrows move between the cards shown (also from the search box); Enter creates."""
        keys = {Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1, Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1}
        visible = self.visible_cards()
        if event.key() in keys and visible:
            index = visible.index(self.selected) if self.selected in visible else -1
            self.choose(visible[max(0, min(len(visible) - 1, index + keys[event.key()]))])
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.selected is not None:
            self.create()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- creating --

    def create(self, fill_later=False):
        """The selected preset: its placeholders asked for (unless FILL_LATER), then the dialog closes."""
        if self.selected is None:
            return False
        preset, values = self.selected.preset, {}
        if preset.placeholders and not fill_later:
            form = PlaceholderForm(self.sai, preset, self)
            if widgets.AUTO_ANSWER is None and form.exec() != QDialog.DialogCode.Accepted:
                return False
            values = form.values()
        self.result_ = (preset, None if fill_later or values is None else values)
        self.accept()
        return True

    # -- the user's own --

    def save_board(self, title=None, description="", category="simple"):
        if self.current_board is None:
            return None
        if title is None:
            dialog = SavePresetDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            title, description, category = dialog.values()
        try:
            preset = PR.from_board(self.sai, self.current_board, title.strip(), description.strip(), category)
            PR.add_mine(self.sai, preset)
        except (OSError, ValueError) as exc:
            warn(self, tr("Not saved"), printable(exc))
            return None
        self.populate(select=preset.id)
        return preset

    def import_preset(self, path=None):
        path = path or choose_file(self, tr("Import a preset"), "preset")
        if not path:
            return None
        try:
            preset = PR.import_file(self.sai, path)
            PR.add_mine(self.sai, preset)
        except (OSError, ValueError) as exc:
            warn(self, tr("Not imported"), printable(exc))
            return None
        self.populate(select=preset.id)
        return preset

    def export_preset(self, path=None):
        if self.selected is None:
            return None
        preset = self.selected.preset
        if path is None:
            path = QFileDialog.getSaveFileName(self, tr("Export the preset"), f"{preset.id}.json",
                                               tr("Presets (*.json)"))[0]
        if not path:
            return None
        try:
            PR.export(self.sai, preset, path)
        except (OSError, ValueError) as exc:
            warn(self, tr("Not exported"), printable(exc))
            return None
        inform(self, tr("Export the preset"), tr("Written: {path}", path=printable(path)))
        return path

    def delete_preset(self):
        if self.selected is None or not self.selected.preset.user:
            return False
        preset = self.selected.preset
        if not confirm(self, tr("Delete"), tr("Delete your preset {name}?", name=printable(preset.title)),
                       tr("Delete"), destructive=True):
            return False
        try:
            PR.remove_mine(self.sai, preset.id)
        except OSError as exc:
            warn(self, tr("Delete"), printable(exc))
            return False
        self.selected = None
        self.populate()
        return True


class PlaceholderForm(QDialog):
    """What a preset leaves to the user, one row each, with what sandbox-ai knows offered: VPN profiles
    (sandbox-ai vpn list --json), LLM servers (sandbox-ai llm list --json), MCP servers (the MCP servers
    page's definitions). Empty rows stay placeholders on the board ("Fill in later" leaves them all)."""

    def __init__(self, sai, preset, parent=None):
        super().__init__(parent)
        self.sai, self.preset, self.fields, self.later = sai, preset, {}, False
        self.endpoints = {}
        self.setWindowTitle(tr("Fill in: {name}", name=preset.text(preset.title)))
        self.resize(620, 120 + 70 * len(preset.placeholders))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("What only you know. Left empty, a value stays a placeholder on the board "
                                        "(shown as a problem until it is filled in); an optional part is left "
                                        "out."), role="secondary"))
        form = QFormLayout()
        layout.addLayout(form)
        kinds = {p.kind for p in preset.placeholders}
        for placeholder in preset.placeholders:
            widget = self.field(placeholder)
            label = preset.text(placeholder.label) + (" " + tr("(optional)") if placeholder.optional else "")
            box = QWidget()
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            column.addWidget(widget)
            if placeholder.kind == "vpn":
                # No profile yet, or another one: made here, without leaving the preset (sandbox-ai vpn add).
                new = QPushButton(tr("New VPN profile…"))
                new.clicked.connect(lambda _=False, marker=placeholder.marker: self.new_vpn(marker))
                column.addWidget(new)
            if placeholder.hint:
                column.addWidget(plain_label(preset.text(placeholder.hint), role="secondary"))
            form.addRow(plain_label(label), box)
            self.fields[placeholder.marker] = widget
        buttons = QDialogButtonBox()
        create = buttons.addButton(tr("Create"), QDialogButtonBox.ButtonRole.AcceptRole)
        create.setProperty("role", "primary")
        create.setDefault(True)
        later = buttons.addButton(tr("Fill in later"), QDialogButtonBox.ButtonRole.ActionRole)
        later.setToolTip(tr("Create the board with every placeholder; fill them in on the board"))
        later.clicked.connect(self.fill_later)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if "vpn" in kinds:
            background(sai.vpn_profiles, owner=self, timeout=90,
                       done=lambda result, error: self.offer("vpn", result if error is None else []))
        if "llm" in kinds:
            background(sai.llm_endpoints, owner=self, timeout=90,
                       done=lambda result, error: self.offer("llm", result if error is None else []))
        if "mcp" in kinds:
            try:
                names = sorted(sai.mcp_definitions())
            except Exception:  # noqa: BLE001 - an mcp.json that does not load: typed in by hand
                names = []
            self.offer("mcp", [{"name": n} for n in names])

    def field(self, placeholder):
        if placeholder.kind == "folder":
            return PathEdit("")
        combo = QComboBox()
        combo.setEditable(placeholder.kind != "gpu")
        combo.addItem("")
        if placeholder.kind == "gpu":
            combo.addItems(["nvidia", "dri", "rocm"])
        if combo.isEditable():
            combo.lineEdit().setPlaceholderText(tr("empty: fill in later"))
        if placeholder.kind == "llm":
            combo.currentTextChanged.connect(lambda text, marker=placeholder.marker: self.llm_chosen(marker, text))
        return combo

    def offer(self, kind, entries):
        """Fill the rows of KIND with ENTRIES (from sandbox-ai's listings); a likely one is chosen."""
        for placeholder in self.preset.placeholders:
            if placeholder.kind != kind:
                continue
            combo = self.fields[placeholder.marker]
            for entry in entries:
                name = entry.get("profile") or entry.get("name")
                if not isinstance(name, str) or combo.findText(name) >= 0:
                    continue
                detail = {"vpn": " · ".join(str(entry.get(k)) for k in ("type", "provider") if entry.get(k)),
                          "llm": " · ".join(str(entry.get(k)) for k in ("kind", "url", "vpn") if entry.get(k)),
                          }.get(kind, "")
                combo.addItem(name)
                combo.setItemData(combo.count() - 1, safe_tooltip(printable(detail)), Qt.ItemDataRole.ToolTipRole)
                if kind == "llm":
                    self.endpoints[name] = entry
            if kind == "mcp" and not combo.currentText() and placeholder.marker == "<mcp:browser>":
                browser = next((e["name"] for e in entries if e["name"].lower() in PR.BROWSER_SERVERS), "")
                combo.setCurrentText(browser)
            if not combo.currentText() and combo.count() == 2 and not placeholder.optional:
                combo.setCurrentIndex(1)  # the only one there is

    def new_vpn(self, marker):
        from .vpn_page import add_profile

        def added(name):
            if name:
                background(self.sai.vpn_profiles, owner=self, timeout=90,
                           done=lambda result, error: (self.offer("vpn", result if error is None else []),
                                                       self.fields[marker].setCurrentText(name)))
        add_profile(self, self.sai, added)

    def llm_chosen(self, marker, text):
        """An LLM server registered with a VPN brings it along: the VPN row that follows it takes it."""
        vpn = (self.endpoints.get(text.split(":", 1)[0]) or {}).get("vpn")
        for placeholder in self.preset.placeholders:
            if placeholder.follows == marker and vpn:
                self.fields[placeholder.marker].setCurrentText(str(vpn))

    def set_value(self, marker, value):
        widget = self.fields[marker]
        if isinstance(widget, PathEdit):
            widget.edit.setText(value)
        else:
            widget.setCurrentText(value)

    def values(self):
        """{placeholder: value} of the rows filled in, or None for "Fill in later"."""
        if self.later:
            return None
        found = {}
        for marker, widget in self.fields.items():
            value = widget.edit.text() if isinstance(widget, PathEdit) else widget.currentText()
            if value.strip():
                found[marker] = os.path.expanduser(value.strip()) if isinstance(widget, PathEdit) else value.strip()
        return found

    def fill_later(self):
        self.later = True
        self.accept()


class SavePresetDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Save the board as a preset"))
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.description = QLineEdit()
        self.description.setMaxLength(400)
        self.category = QComboBox()
        for key, title in PR.CATEGORIES:
            self.category.addItem(tr(title), key)
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Description"), self.description)
        form.addRow(tr("Category"), self.category)
        form.addRow(plain_label(tr("The workspaces are not kept (a preset is for any project); secrets never are: "
                                   "a board with one is not saved."), role="secondary"))
        buttons = QDialogButtonBox()
        buttons.addButton(tr("Save"), QDialogButtonBox.ButtonRole.AcceptRole).setDefault(True)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(lambda: self.accept() if self.name.text().strip() else self.name.setFocus())
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.name.text(), self.description.text(), self.category.currentData()
