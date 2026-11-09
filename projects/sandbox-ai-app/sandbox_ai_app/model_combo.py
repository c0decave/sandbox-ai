"""Model choosers that hold every model a server lists (70, 120 or more): the popup is a list that scrolls (at most
VISIBLE rows at a time, a scroll bar as needed - also with styles whose native popup would grow to the screen's
height), and typing filters it (a completer that matches any part of an entry, whatever the case).

searchable(combo) turns a QComboBox into such a chooser; STRICT ones (the Model choice beside the agent: only what
is listed may be chosen) go back to the entry chosen when what was typed names none.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QCompleter

VISIBLE = 20  # rows the popup shows at a time; the rest scroll


def searchable(combo, visible=VISIBLE, strict=False):
    """COMBO (a QComboBox) as a chooser for long lists: VISIBLE rows, a scroll bar, typing filters (contains, any
    case). STRICT: typed text that names no entry is replaced by the entry chosen when editing ends. Returns COMBO."""
    combo.setMaxVisibleItems(visible)
    view = combo.view()
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    # Styles with a "native" popup (macOS, Fusion's in-place popup) ignore maxVisibleItems and show every row they
    # can fit, without a scroll bar: a list popup instead.
    if "combobox-popup" not in combo.styleSheet():
        combo.setStyleSheet(combo.styleSheet() + "QComboBox { combobox-popup: 0; }")
    if not combo.isEditable():
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    completer = QCompleter(combo.model(), combo)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    completer.setMaxVisibleItems(visible)
    combo.setCompleter(completer)
    if strict and not combo.property("model_combo_strict"):
        combo.setProperty("model_combo_strict", True)
        combo.lineEdit().editingFinished.connect(lambda: settle(combo))
    return combo


def settle(combo):
    """A strict chooser after typing: an entry whose text was typed is chosen (and activated, as a click would);
    other text goes, the entry chosen before stays."""
    try:
        text = combo.currentText()
    except RuntimeError:  # the combo is gone (a page rebuilt)
        return
    index = combo.findText(text, Qt.MatchFlag.MatchFixedString)
    item = combo.model().item(index) if index >= 0 and hasattr(combo.model(), "item") else None
    if index >= 0 and (item is None or item.isEnabled()):
        if index != combo.currentIndex():
            combo.setCurrentIndex(index)
            combo.activated.emit(index)
        return
    combo.setEditText(combo.itemText(combo.currentIndex()) if combo.currentIndex() >= 0 else "")
