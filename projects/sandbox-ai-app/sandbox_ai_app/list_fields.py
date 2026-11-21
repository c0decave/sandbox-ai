"""Several mostly-empty list fields as one compact field (user 2026-11-19: "Dropdown mit Klassen -> Add -> Werte
eintragen"): a drop-down of the KINDS of value (Allowed host names, TCP address:port, Own name servers ...), the value
field beside it and Add. What was added shows below as small chips grouped by kind, each with its remove button (x);
a kind without values takes no room at all. The values are the same lists the separate fields held - a board, a
wizard page saves exactly what it saved before.

Keyboard: the drop-down, the value, Enter adds (Add too); a chip takes the keyboard (Tab), Delete or Backspace removes
it, its right-click menu has Remove, Edit (back into the field) and Copy. Each kind's tooltip is the old field's help.

A kind may check what is added (CHECK: the value -> why it is refused, or None) and split it into items (SPLIT: a
launcher list option's "a, b c" - backend.list_items); a value refused stays in the field with why below it.

Suggestions per kind: set_provider(key, provider) - PROVIDER(fill) is called when that kind is chosen and when the
value's list is opened, and calls fill(items, note="") at once or later (from a background read): ITEMS are values or
(label, value) pairs, offered in the editable value field's list (any other value may still be typed); NOTE is one
line below the field (nothing found, the host does not answer). A device picker, the host's secrets plug in so. An item
may be (label, value, group): the items of one GROUP follow its name, a line of the list that cannot be chosen (the
devices of a host by kind: USB devices, Serial ports ...)."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from . import backend
from .files import printable
from .i18n import tr
from .widgets import FlowLayout, exec_dropped, plain_label, safe_tooltip

PLACEHOLDER_HOST = "HOST"  # a suggestion's part to be typed over (HOST=SECRET of --api-key): selected when chosen


@dataclass
class ListKind:
    """One kind of value of a ListFields."""
    key: str  # what its values are saved as (a launcher option, a key of the part)
    label: str  # in the drop-down and over its chips (translated)
    tip: str = ""  # the kind's help (the old field's tooltip)
    placeholder: str = ""  # what a value looks like
    split: bool = False  # several in one value, separated by commas or spaces (backend.list_items)
    check: Optional[Callable[[str], Optional[str]]] = None  # value -> why it is refused, or None
    offered: bool = True  # in the drop-down (False: its values are shown, none can be added - another agent's feature)
    enabled: bool = True  # False: greyed out in the drop-down (the administrator's policy), WHY its tooltip
    why: str = ""


# -- what the launcher takes (the same refusals as the Board's checks, board.problems, and the launcher's own) --

def check_ip(value):
    try:
        ipaddress.ip_address(str(value))
    except ValueError:
        return tr("A resolver is one IP address: {value}", value=printable(value))
    return None


def check_network(value):
    try:
        ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return tr("Not an address or network: {value}", value=printable(value))
    return None


def check_port(value):
    if not str(value).isdigit() or not 1 <= int(value) <= 65535:
        return tr("Not a port: {value}", value=printable(value))
    return None


TCP = re.compile(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([0-9.]+):(\d{1,5})")  # agent_sandbox.tcp_targets


def check_tcp(value):
    match = TCP.fullmatch(str(value))
    try:
        if match and ipaddress.ip_address(match[1] or match[3]) and 1 <= int(match[2] or match[4]) <= 65535:
            return None
    except ValueError:
        pass
    return tr("Not IP:PORT or [IPv6]:PORT: {value}", value=printable(value))


def check_host(value):
    """A host name (or *.DOMAIN): no scheme, path or port - those the launcher would refuse at the start."""
    if re.fullmatch(r"(\*\.)?[A-Za-z0-9_.-]+\.?", str(value)) and ".." not in str(value):
        return None
    return tr("Not a host name: {value}", value=printable(value))


def check_wildcard(value):
    """A host wildcard *.DOMAIN (--allow-shared-wildcard): the launcher checks the rest."""
    if str(value).startswith("*.") and check_host(value) is None:
        return None
    return tr("Not a wildcard *.DOMAIN: {value}", value=printable(value))


OPTION_CHECKS = {"--nameserver": check_ip, "--allow": check_network, "--allow-host-port": check_port,
                 "--allow-tcp": check_tcp, "--vpn-bypass-tcp": check_tcp, "--allow-host": check_host,
                 "--allow-domain": check_host, "--no-inspect": check_host, "--allow-shared-wildcard": check_wildcard}


# What a value looks like, as the empty field's placeholder (documentation addresses: RFC 5737, RFC 3849).
EXAMPLES = {"--allow-host": "pypi.org, *.example.com", "--allow-domain": "example.org",
            "--allow-shared-wildcard": "*.blob.core.windows.net", "--allow": "192.0.2.0/24",
            "--allow-tcp": "192.0.2.10:5432", "--allow-host-port": "11434", "--nameserver": "192.0.2.53",
            "--no-inspect": "*.example.com", "--vpn-bypass-tcp": "192.168.1.20:11434", "--env": "NAME",
            "--secret-env": "NAME[=SECRET]", "--api-key": "HOST=NAME[=SECRET]",
            "--git-token-env": "https://git.example.org=NAME", "--git-token-file": "https://git.example.org=FILE",
            "--git-token-secret": "https://git.example.org=SECRET", "--device": "/dev/ttyUSB0"}


def option_kind(option, label, tip="", offered=True):
    """The ListKind of the launcher's list option OPTION (an Option of backend.SandboxAI.options(), or its long name):
    split and checked as the launcher takes it."""
    long = getattr(option, "long", option)
    example = EXAMPLES.get(long) or getattr(option, "metavar", None) or ""
    return ListKind(long, label, tip=tip, placeholder=example, split=long in backend.LIST_OPTIONS,
                    check=OPTION_CHECKS.get(long), offered=offered)


class SuggestBox(QComboBox):
    """The value field: editable, its list the current kind's suggestions (none: it opens no empty list)."""
    opening = Signal()

    def showPopup(self):  # noqa: N802
        self.opening.emit()
        if self.count():
            super().showPopup()


class Chip(QFrame):
    """One value: its text and a remove button; Delete or Backspace removes it, right-click: Remove, Edit, Copy."""
    removed = Signal()
    edited = Signal()

    def __init__(self, value, kind_label, editable=True, parent=None):
        super().__init__(parent)
        self.value, self.editable = value, editable
        self.setProperty("role", "chip")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{value} ({kind_label})")
        self.setAccessibleDescription(tr("Delete removes it"))
        self.setToolTip(safe_tooltip(tr("{kind}: {value}\nDelete removes it; right-click: Edit, Copy",
                                        kind=kind_label, value=value)))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 1, 1, 1)
        row.setSpacing(2)
        text = QLabel(value)
        text.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(text)
        close = self.close_button = QToolButton()
        close.setText("✕")
        close.setAutoRaise(True)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # (the chip has the keyboard: Delete)
        close.setAccessibleName(tr("Remove {value}", value=value))
        close.setToolTip(safe_tooltip(tr("Remove {value}", value=value)))
        close.clicked.connect(lambda _=False: self.removed.emit())
        row.addWidget(close)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.removed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2) and self.editable:
            self.edited.emit()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        exec_dropped(self.menu(), event.globalPos())

    def menu(self):
        from .widgets import context_menu
        return context_menu(self, [(tr("Remove"), True, None, self.removed.emit),
                                   (tr("Edit"), self.editable, tr("No more of this kind can be added here"),
                                    self.edited.emit), None,
                                   (tr("Copy"), True, None, lambda: QApplication.clipboard().setText(self.value))])


class ListFields(QWidget):
    """The kinds' values as one field (see the module's text). VALUES: {key: [value]} as they are now.
    changed(key, values): a kind's values changed (one Add or one removal: one step of an undo). kind_chosen(key):
    another kind was chosen in the drop-down (a page that makes the field anew keeps it)."""
    changed = Signal(str, list)
    kind_chosen = Signal(str)

    def __init__(self, kinds, values=None, parent=None, name=""):
        super().__init__(parent)
        self.kinds = {kind.key: kind for kind in kinds}
        self.data = {key: list(dict.fromkeys(str(v) for v in (values or {}).get(key) or [] if str(v).strip()))
                     for key in self.kinds}
        self.providers, self.suggestions = {}, {}
        self.chips = {}  # key -> [Chip]
        self.dropped = []  # the chips' groups made anew, until they are deleted
        self.taking = False
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        # (the value field is made first: a page that makes the field anew finds the keyboard's field again as the
        # first combo box and line of this field - BoardPage.restore_focus)
        self.value_box = SuggestBox()
        self.value_box.setEditable(True)
        self.value_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.value_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.value_box.setMinimumContentsLength(12)
        self.value_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.value_box.setAccessibleName(tr("Value"))
        self.value_box.activated.connect(self.suggestion_chosen)
        self.value_box.opening.connect(lambda: self.ask_provider(self.current_kind()))
        self.value_box.installEventFilter(self)
        self.value_box.lineEdit().textEdited.connect(lambda _t: self.say(""))
        self.kind_box = QComboBox()
        self.kind_box.setAccessibleName(tr("Kind of value"))
        self.kind_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.kind_box.setMinimumContentsLength(14)
        for kind in self.kinds.values():
            if kind.offered:
                self.kind_box.addItem(kind.label, kind.key)
                index = self.kind_box.count() - 1
                self.kind_box.setItemData(index, safe_tooltip(kind.tip or kind.label), Qt.ItemDataRole.ToolTipRole)
                if not kind.enabled:
                    self.kind_box.model().item(index).setEnabled(False)
                    self.kind_box.setItemData(index, safe_tooltip(kind.why or kind.tip), Qt.ItemDataRole.ToolTipRole)
        first = next((i for i in range(self.kind_box.count()) if self.kind_box.model().item(i).isEnabled()), 0)
        self.kind_box.setCurrentIndex(first if self.kind_box.count() else -1)
        self.kind_box.currentIndexChanged.connect(self.kind_changed)
        self.add_button = QPushButton(tr("Add"))
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(lambda _=False: self.add())
        self.entry_row = QWidget()
        entry = QHBoxLayout(self.entry_row)
        entry.setContentsMargins(0, 0, 0, 0)
        entry.addWidget(self.value_box, 1)  # (its child first: see above)
        entry.insertWidget(0, self.kind_box)
        entry.addWidget(self.add_button)
        column.addWidget(self.entry_row)
        self.problem = plain_label("", role="notice-error", selectable=True)
        self.problem.hide()
        column.addWidget(self.problem)
        self.note = plain_label("", role="secondary")
        self.note.hide()
        column.addWidget(self.note)
        self.groups = QWidget()
        self.groups_layout = QVBoxLayout(self.groups)
        self.groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups_layout.setSpacing(2)
        column.addWidget(self.groups)
        # (hidden only once they are in this field: a widget without a parent shown is a window of its own, which
        # takes the keyboard from the window the field is made in)
        self.kind_box.setHidden(self.kind_box.count() < 2)  # one kind: the row's label names it (no drop-down of one)
        self.entry_row.setHidden(self.kind_box.count() == 0)
        self.setFocusProxy(self.value_box)
        if name:
            self.setAccessibleName(name)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.show_kind()
        self.show_chips()
        QApplication.instance().focusChanged.connect(self.focus_moved)

    # -- the kinds --

    def current_kind(self):
        return self.kind_box.currentData() if self.kind_box.count() else None

    def choose_kind(self, key):
        """KEY's kind in the drop-down (a finding about it, a page made anew): True when it is offered."""
        index = self.kind_box.findData(key)
        if index < 0 or not self.kind_box.model().item(index).isEnabled():
            return False
        self.kind_box.setCurrentIndex(index)
        return True

    def set_kind_enabled(self, key, enabled, why=""):
        """KEY's kind greyed out in the drop-down (the administrator's policy): its chips stay, removable."""
        kind = self.kinds.get(key)
        if kind is None:
            return
        kind.enabled, kind.why = bool(enabled), why
        index = self.kind_box.findData(key)
        if index >= 0:
            self.kind_box.model().item(index).setEnabled(bool(enabled))
            self.kind_box.setItemData(index, safe_tooltip(why if not enabled and why else kind.tip or kind.label),
                                      Qt.ItemDataRole.ToolTipRole)
            if not enabled and self.kind_box.currentIndex() == index:
                other = next((i for i in range(self.kind_box.count()) if self.kind_box.model().item(i).isEnabled()),
                             None)
                if other is not None:
                    self.kind_box.setCurrentIndex(other)
        self.show_kind()

    def kind_changed(self, _index):
        self.say("")
        self.show_kind()
        self.kind_chosen.emit(self.current_kind() or "")

    def show_kind(self):
        """The value field, Add and the tooltips as the chosen kind wants them; its suggestions asked for."""
        key = self.current_kind()
        kind = self.kinds.get(key)
        usable = kind is not None and kind.enabled
        self.value_box.setEnabled(usable)
        self.add_button.setEnabled(usable)
        if kind is None:
            return
        self.value_box.lineEdit().setPlaceholderText(kind.placeholder)
        tip = kind.tip or kind.label
        if kind.split:
            tip += "\n" + tr("One or more, separated by commas or spaces")
        tip += "\n" + tr("Enter adds it")
        self.value_box.setToolTip(safe_tooltip(tip))
        self.kind_box.setToolTip(safe_tooltip(kind.tip or kind.label))
        self.add_button.setAccessibleName(tr("Add {kind}", kind=kind.label))
        self.add_button.setToolTip(safe_tooltip(tr("Add what the field holds as {kind}", kind=kind.label)))
        self.fill_suggestions()
        self.ask_provider(key)

    # -- suggestions --

    def set_provider(self, key, provider):
        """PROVIDER(fill) offers values of KEY's kind (see the module's text); asked now when that kind is chosen."""
        self.providers[key] = provider
        if key == self.current_kind():
            self.ask_provider(key)

    def ask_provider(self, key):
        provider = self.providers.get(key)
        if provider is not None:
            provider(lambda items, note="", k=key: self.set_suggestions(k, items, note))

    def set_suggestions(self, key, items, note=""):
        """ITEMS (values, (label, value) pairs or (label, value, group)) offered for KEY's kind; NOTE a line below the
        field."""
        try:
            self.value_box.lineEdit()
        except RuntimeError:  # (a late answer for a field already gone)
            return
        pairs = []
        for item in items or []:
            if isinstance(item, (tuple, list)) and len(item) == 3:
                label, value, group = item
            else:
                (label, value), group = (item if isinstance(item, (tuple, list)) and len(item) == 2 else (item, item)), ""
            if str(value).strip():
                pairs.append((str(label), str(value), str(group or "")))
        self.suggestions[key] = (pairs, str(note or ""))
        if key == self.current_kind():
            self.fill_suggestions()

    def fill_suggestions(self):
        pairs, note = self.suggestions.get(self.current_kind(), ([], ""))
        text, cursor = self.value_box.currentText(), self.value_box.lineEdit().cursorPosition()
        modified = self.value_box.lineEdit().isModified()
        self.value_box.blockSignals(True)
        self.value_box.clear()
        group = ""
        for label, value, in_group in pairs:
            if in_group and in_group != group:  # (the group's name: a line that cannot be chosen)
                group = in_group
                self.value_box.addItem(group)
                header = self.value_box.model().item(self.value_box.count() - 1)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                font = header.font()
                font.setBold(True)
                header.setFont(font)
            self.value_box.addItem(label, value)
            self.value_box.setItemData(self.value_box.count() - 1, safe_tooltip(value if value != label else label),
                                       Qt.ItemDataRole.ToolTipRole)
        self.value_box.setCurrentIndex(-1)
        self.value_box.setEditText(text)
        self.value_box.lineEdit().setCursorPosition(cursor)
        self.value_box.lineEdit().setModified(modified)
        self.value_box.blockSignals(False)
        self.note.setText(note)
        self.note.setVisible(bool(note))

    def suggestion_chosen(self, index):
        """A suggestion chosen from the list: its value in the field (its label may say more), HOST selected to be
        typed over."""
        value = self.value_box.itemData(index)
        if value is None:
            return
        value = str(value)
        self.value_box.setEditText(value)
        edit = self.value_box.lineEdit()
        edit.setModified(True)
        at = value.find(PLACEHOLDER_HOST)
        if at >= 0:
            edit.setSelection(at, len(PLACEHOLDER_HOST))

    # -- adding and removing --

    def typed(self):
        """What the value field holds; a suggestion's label (completed as typed, or chosen) stands for its value."""
        text = self.value_box.currentText().strip()
        index = self.value_box.findText(text, Qt.MatchFlag.MatchExactly)
        if text and index >= 0 and self.value_box.itemData(index) is not None:
            return str(self.value_box.itemData(index))
        return text

    def items_of(self, key, text):
        kind = self.kinds[key]
        return backend.list_items(text) if kind.split else ([text.strip()] if text.strip() else [])

    def add(self, text=None, key=None):
        """What the field holds (or TEXT) added to the chosen kind (or KEY's): True when added; refused (a value the
        kind does not take): False, the text left in the field and why said below it."""
        key = key or self.current_kind()
        kind = self.kinds.get(key)
        text = self.typed() if text is None else str(text)
        if kind is None or not kind.enabled or not text.strip():
            return False
        items = self.items_of(key, text)
        for item in items:
            why = kind.check(item) if kind.check is not None else None
            if why:
                self.say(why)
                return False
        values = list(dict.fromkeys([*self.data[key], *items]))
        self.value_box.setEditText("")
        self.value_box.lineEdit().setModified(False)
        self.say("")
        if values != self.data[key]:
            self.data[key] = values
            self.show_chips()
            self.changed.emit(key, list(values))
        return True

    def take_typed(self):
        """What was typed and not added yet, added (the field is left, another part chosen): True when nothing is left
        in the field."""
        if not self.typed():
            return True
        return self.add()

    def remove(self, key, value, focus=False):
        if value not in self.data.get(key, []):
            return
        order = [(k, v) for k in self.kinds for v in self.data[k]]
        at = order.index((key, value))
        self.data[key] = [v for v in self.data[key] if v != value]
        self.show_chips()
        self.changed.emit(key, list(self.data[key]))
        if focus:  # the keyboard to the chip that took its place, else the value field
            after = [chip for k in self.kinds for chip in self.chips.get(k, [])]
            target = after[min(at, len(after) - 1)] if after else self.value_box
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def edit(self, key, value):
        """VALUE back into the field to be changed (taken out: Add puts the changed one back)."""
        if self.kinds[key].offered and self.choose_kind(key):
            self.remove(key, value)
            self.value_box.setEditText(value)
            self.value_box.lineEdit().setModified(True)
            self.value_box.setFocus(Qt.FocusReason.OtherFocusReason)

    def say(self, text):
        self.problem.setText(text)
        self.problem.setVisible(bool(text))

    # -- the values --

    def values(self, key=None):
        return list(self.data.get(key, [])) if key is not None else {k: list(v) for k, v in self.data.items()}

    def set_values(self, key, values):
        self.data[key] = list(dict.fromkeys(str(v) for v in values or [] if str(v).strip()))
        self.show_chips()

    def show_chips(self):
        """The values as chips, grouped by kind under the kind's name; a kind without values: nothing at all."""
        # (the chips before: out of this field at once - gone for findChildren and the keyboard -, deleted once the
        # event loop runs: a chip's own remove button or Delete key may be what made them anew)
        dropped = []
        while self.groups_layout.count():
            widget = self.groups_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
                dropped.append(widget)
        self.dropped = dropped
        self.chips = {}
        several = sum(1 for k in self.kinds if self.data[k]) > 1 or self.kind_box.count() > 1
        for key, kind in self.kinds.items():
            if not self.data[key]:
                continue
            group = QWidget()
            group.setObjectName(f"chips:{key}")
            flow = FlowLayout(group, spacing=4)
            if several:
                title = QLabel(kind.label + ":")
                title.setProperty("role", "secondary")
                title.setToolTip(safe_tooltip(kind.tip or kind.label))
                flow.addWidget(title)
            chips = self.chips[key] = []
            for value in self.data[key]:
                chip = Chip(value, kind.label, editable=kind.offered and kind.enabled)
                chip.removed.connect(lambda k=key, v=value, c=chip: self.remove(k, v, focus=c.hasFocus()))
                chip.edited.connect(lambda k=key, v=value: self.edit(k, v))
                flow.addWidget(chip)
                chips.append(chip)
            self.groups_layout.addWidget(group)
        self.groups.setVisible(bool(self.chips))
        self.updateGeometry()

    # -- the keyboard --

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.value_box and event.type() == QEvent.Type.KeyPress and \
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.typed():
            if not self.value_box.view().isVisible():
                self.add()
                return True  # (never the dialog's default button as well)
        return super().eventFilter(watched, event)

    def release(self):
        """This field is about to go (its page makes it anew): leaving it takes nothing any more."""
        self.taking = True

    def focus_moved(self, old, new):
        """The keyboard left this field for something else (not its own drop-down or Add): what was typed is taken,
        as leaving a line takes it."""
        try:
            if self.taking or old is None or not self.isVisible() or new is None or not (old is self or self.isAncestorOf(old)):
                return
        except RuntimeError:  # (this field is gone)
            return
        if new is self or self.isAncestorOf(new) or self.value_box.view().isAncestorOf(new):
            return
        self.taking = True
        try:
            self.take_typed()
        finally:
            self.taking = False
