"""The model picker beside the Model choice (the Board's agent part, the Start page): every model of the model servers
in a list that a search field filters as it is typed, sorted by name, parameter count (7B, 70B) or size, and
filtered by what a model is for - text, code, vision (VL), image (diffusion), embedding, reasoning - shown as small
tags per model. What a model can do comes from its server (Ollama's families, LM Studio's type, OpenRouter's
modalities: sandbox-ai's llm models --json), else from its name (wizard_llm.model_tags).

Everything shown was read already (the servers' models are asked in the background: backend.host_llm_servers); the
picker itself reads nothing.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout,
)

from . import model_profile as MP, model_profile_view as MPV, tables, wizard_llm as WL
from .files import printable
from .i18n import N_, tr
from .widgets import plain_label, safe_tooltip

SORTS = (("name", N_("Name")), ("params", N_("Parameters")), ("size", N_("Size")))
COLUMNS = (N_("Model"), N_("Tags"), N_("Parameters"), N_("Size"))
SUPPORT = N_("Support")  # the traffic light's column: only when some row has one (model_profile.support_of)


class ModelPicker(QDialog):
    """ROWS (wizard_llm.picker_rows) to choose one from: a search field, the sort, the filter by tag, the list. The
    value chosen: chosen() (None: cancelled); one the agent cannot use is shown greyed, with why."""

    def __init__(self, parent, rows, current=""):
        super().__init__(parent)
        self.setWindowTitle(tr("Choose a model"))
        self.rows, self.shown_rows = list(rows), []
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search the models"))
        self.search.setAccessibleName(tr("Search the models"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self.show_rows())
        top.addWidget(self.search, 1)
        self.sort = QComboBox()
        self.sort.setAccessibleName(tr("Sort by"))
        for key, title in SORTS:
            self.sort.addItem(tr("Sort: {by}", by=tr(title)), key)
        self.sort.currentIndexChanged.connect(lambda _i: self.show_rows())
        top.addWidget(self.sort)
        self.tag = QComboBox()
        self.tag.setAccessibleName(tr("What it is for"))
        self.tag.addItem(tr("Every kind"), "")
        for key, title in WL.TAGS:
            self.tag.addItem(tr(title), key)
        self.tag.currentIndexChanged.connect(lambda _i: self.show_rows())
        top.addWidget(self.tag)
        layout.addLayout(top)
        self.lights = any(row.get("support") for row in self.rows)
        columns = [*COLUMNS, SUPPORT] if self.lights else list(COLUMNS)
        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels([tr(c) for c in columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        # the model's name takes the rest; parameters, size and the light their content (tables.py)
        tables.keep(tables.setup(self.table, 0, range(2, len(columns))), "dialog-model-picker")
        self.table.setAccessibleName(tr("Models"))
        self.table.cellDoubleClicked.connect(lambda row, _column: self.take(row))
        self.table.itemSelectionChanged.connect(self.update_ok)
        layout.addWidget(self.table, 1)
        # the selected model's traffic light in detail: per agent what worked and failed, the settings, the speed
        self.support_panel = MPV.SupportPanel()
        self.support_area = MPV.scrolled(self.support_panel, 200)
        self.support_area.setVisible(False)
        layout.addWidget(self.support_area)
        self.table.itemSelectionChanged.connect(self.show_support)
        self.count = plain_label(role="secondary")
        layout.addWidget(self.count)
        buttons = QDialogButtonBox()
        self.ok = buttons.addButton(tr("Choose"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(lambda: self.take(self.table.currentRow()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.value = None
        self.resize(760, 520)
        self.show_rows(current)
        self.search.setFocus()

    def show_rows(self, select=None):
        """The rows the search, the tag and the sort let through, the one chosen kept (or SELECT)."""
        keep = select if select is not None else self.current_value()
        rows = WL.filter_rows(WL.sort_rows(self.rows, self.sort.currentData() or "name"), self.search.text(),
                              self.tag.currentData() or "")
        self.shown_rows = rows
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            cells = [printable(row["label"]), " ".join(f"[{tr(dict(WL.TAGS)[t])}]" for t in row["tags"]),
                     WL.params_label(row["params"]), WL.size_text(row["size"]) if row["size"] else ""]
            found = row.get("support")
            if self.lights:
                cells.append(MP.light_label(found) if found else "")
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if found and column in (0, len(COLUMNS)):  # the light's icon beside the name and in its column
                    item.setIcon(MPV.light_icon(found["light"]))
                    item.setData(Qt.ItemDataRole.AccessibleTextRole,
                                 f"{text}, {MP.light_accessible(found, row['model'])}")
                if row["hint"]:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
                    item.setToolTip(safe_tooltip(row["hint"]))
                elif column == 0:
                    item.setToolTip(safe_tooltip((f"--llm {row['value']}" if row["value"] else
                                                  tr("The agent's own provider and login (no --llm)"))
                                                 + (f"\n{MP.light_summary(found)}" if found else "")))
                elif column == len(COLUMNS) and found:
                    item.setToolTip(safe_tooltip(MP.light_summary(found)))
                self.table.setItem(index, column, item)
        tables.fit_columns(self.table)
        found = next((i for i, r in enumerate(rows) if r["value"] == keep and not r["hint"]), None)
        if found is not None:
            self.table.selectRow(found)
        self.count.setText(tr("{shown} of {total} models", shown=len(rows), total=len(self.rows)))
        self.update_ok()

    def show_support(self):
        """The detail panel of the selected row's light (hidden for a row without one)."""
        row = self.table.currentRow()
        selected = 0 <= row < len(self.shown_rows) and bool(self.table.selectedItems())
        found = self.shown_rows[row].get("support") if selected else None
        self.support_panel.set(found)
        self.support_area.setVisible(found is not None)

    def current_value(self):
        row = self.table.currentRow()
        return self.shown_rows[row]["value"] if 0 <= row < len(self.shown_rows) and self.table.selectedItems() \
            else None

    def update_ok(self):
        row = self.table.currentRow()
        self.ok.setEnabled(0 <= row < len(self.shown_rows) and bool(self.table.selectedItems())
                           and not self.shown_rows[row]["hint"])

    def take(self, row):
        if 0 <= row < len(self.shown_rows) and not self.shown_rows[row]["hint"]:
            self.value = self.shown_rows[row]["value"]
            self.accept()

    def chosen(self):
        return self.value


def picker_button(combo, rows, chosen):
    """A "…" button beside COMBO (a Model choice of wizard.fill_model_combo) that opens the picker with ROWS() and
    passes the value chosen to CHOSEN(value) - after setting COMBO to it (added when it is not listed)."""
    from .widgets import exec_dropped
    button = QToolButton()
    button.setText("…")
    button.setAccessibleName(tr("Choose a model"))
    button.setToolTip(tr("Choose a model: search, sort by name, parameters or size, filter by what it is for (text, "
                         "code, vision, image, embedding, reasoning)"))

    def open_picker():
        dialog = ModelPicker(button.window(), rows(), combo.currentData() or "")
        if exec_dropped(dialog) != QDialog.DialogCode.Accepted or dialog.chosen() is None:
            return
        value = dialog.chosen()
        index = combo.findData(value)
        if index < 0:
            combo.addItem(value, value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
        chosen(value)
    button.clicked.connect(lambda _=False: open_picker())
    return button

