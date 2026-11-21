"""The recommended pairs of a use case - model x harness - for the Board, the Start page and the new-project wizard.

sandbox-ai's model_usecases ranks every model a server HAS by the use case's table, each with its best harness (the
agent it was measured best in), then the models the table does not have ("not measured for this use case"), then the
ones it marks unsafe for the use case (KAT, Flint, Gemma 4 or Nex with untrusted input) - shown with the reason,
never hidden (use_case.pairs_of). PairList shows them as a table: the first one is the recommended pair; "Use this
pair" (the button, a double click, the row's right-click menu) hands the pair to the page, which sets the agent and the
model (an unsafe one only after a question that says why). Below the table: the pair selected in words - why, the
evidence, what fits, and when the page's agent is not its harness, what is known of the model there ("... is
measured best in Claude Code; in OpenCode: partial").
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from . import use_case as UC
from . import widgets
from .i18n import tr
from .files import printable
from .widgets import plain_label, safe_tooltip


def fill_use_cases(combo, found, current=""):
    """COMBO with "none" and every use case of FOUND (use_case.recommendations) with its status, CURRENT chosen (a
    value the table does not know is kept)."""
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(tr("none: choose the agent and the model yourself"), "")
    for entry in found or []:
        combo.addItem(f"{UC.label(entry['label'])} — {UC.status_text(entry['status'])}", entry["use_case"])
        combo.setItemData(combo.count() - 1, safe_tooltip(printable(str(entry.get("about") or ""))),
                          Qt.ItemDataRole.ToolTipRole)
    if current and combo.findData(current) < 0:
        combo.addItem(printable(current), current)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    combo.blockSignals(False)
    return combo


class PairList(QWidget):
    """The pairs of one use case for a server (set), best first; picked(pair) when one is to be used."""
    picked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        self.intro = widgets.explanation(tr(
            "The server's models ranked by the use case's measurements, each in the agent it was measured best in; "
            "the first is the recommended pair. Not measured and unsafe ones are listed too, with why."))
        column.addWidget(self.intro)
        self.table = widgets.table([tr("Model"), tr("Agent"), tr("Measured"), tr("Fits"), tr("Why")], stretch=4,
                                   compact=(1, 2, 3), sortable=False)
        self.table.setAccessibleName(tr("Recommended pairs"))
        self.table.setMaximumHeight(190)
        column.addWidget(self.table)
        row = QHBoxLayout()
        self.use_button = QPushButton(tr("Use this pair"))
        self.use_button.setToolTip(safe_tooltip(tr("Sets the agent and the model of the pair selected (the use case's "
                                                   "settings for them go over the model profile)")))
        self.use_button.clicked.connect(lambda _=False: self.use())
        row.addWidget(self.use_button)
        row.addStretch()
        column.addLayout(row)
        self.note = plain_label(role="secondary", selectable=True)
        self.note.setAccessibleName(tr("The pair selected"))
        column.addWidget(self.note)
        self.empty = plain_label(role="secondary")
        column.addWidget(self.empty)
        self.pairs, self.one_server = [], True
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self.use())
        widgets.list_menu(self.table, lambda: [
            self.use_button,
            (tr("Copy the start command"), self.chosen() is not None, tr("Select a pair first"),
             lambda: self.copy_command())])
        self.set(None)

    def set(self, found, server=None, current=None):
        """The pairs of FOUND (one use case's recommend()) - SERVER's only, when given -, CURRENT ((server, model)
        the page has) selected if it is one of them, else the first (the recommended pair)."""
        self.pairs = UC.pairs_of(found, server)
        self.one_server = len({pair["server"] for pair in self.pairs}) <= 1
        widgets.fill(self.table, [UC.pair_row(pair, self.one_server) for pair in self.pairs],
                     colors=["bad" if pair.get("unsafe") else "muted" if pair.get("source") != "table" else None
                             for pair in self.pairs],
                     tooltips=[UC.pair_text(pair) for pair in self.pairs],
                     keys=[(pair["server"], pair["model"]) for pair in self.pairs])
        keys = [(pair["server"], pair["model"]) for pair in self.pairs]
        widgets.select_keys(self.table, [current] if current in keys else keys[:1])
        shown = found is not None
        if shown and self.parentWidget() is None:  # not placed yet: shown with its parent later, never as a window
            self.setAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide, False)
        else:
            self.setVisible(shown)
        self.table.setVisible(bool(self.pairs))
        self.use_button.setVisible(bool(self.pairs))
        self.intro.setVisible(bool(self.pairs))  # (an explanation: shown only with the setting on)
        self.empty.setText("" if self.pairs or not shown else
                           tr("No model of this server to pair: add or load one on the server (LLM servers page)."))
        self.empty.setVisible(bool(self.empty.text()))
        self.selection_changed()

    def chosen(self):
        """The pair selected, or None."""
        keys = widgets.selected_keys(self.table)
        return next((pair for pair in self.pairs if keys and (pair["server"], pair["model"]) == keys[0]), None)

    def selection_changed(self):
        pair = self.chosen()
        self.use_button.setEnabled(pair is not None)
        self.note.setText(UC.pair_text(pair) if pair else "")
        role = "notice-warn" if pair and (pair.get("unsafe") or pair.get("agent")) else "secondary"
        if self.note.property("role") != role:
            self.note.setProperty("role", role)
            self.note.style().unpolish(self.note)
            self.note.style().polish(self.note)
        self.note.setVisible(pair is not None)

    def use(self):
        """The pair selected to the page (picked) - one the use case marks unsafe only after a question that says
        why (Cancel the default)."""
        pair = self.chosen()
        if pair is None:
            return
        if pair.get("unsafe") and not widgets.ask_first(
                self, "unsafe-pair", tr("Use an unsafe pair?"),
                tr("The research marks {model} unsafe for this use case: {reason}\n\nWith untrusted input (web "
                   "pages, tickets, other tools' results) it may follow a planted instruction again. Use it anyway?",
                   model=printable(pair["model"]), reason=printable(str(pair["unsafe"]))),
                tr("Use it anyway")):
            return
        self.picked.emit(pair)

    def copy_command(self):
        from . import copying
        pair = self.chosen()
        if pair is not None and pair.get("command"):
            copying.copy_text(str(pair["command"]))
