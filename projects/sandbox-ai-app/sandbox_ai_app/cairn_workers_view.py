"""The Board's field of Cairn's workers (--cairn-workers AGENT[=MODEL],...): the value as typed, and a list to add a
worker from - every harness of sandbox-ai's with its traffic light, as the model lists show theirs
(model_profile_view.light_icon: the theme's colour with a shape of its own), its words and evidence in the tooltip
and the accessible text. The lights come from sandbox-ai's cairn_workers.py (`sandbox-ai cairn workers`); a sandbox-ai
without it shows the plain field.

The user (2026-11-22): "why workers only for claude and not all harnesses?" and "shouldnt all harnesses generally be
support? and if not the harnesses supported shown by ampel or desc?" """
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QWidget

from . import applog
from .i18n import N_, tr
from .files import printable
from .model_profile import LIGHT_WORDS, agent_name
from .model_profile_view import LIGHT_ROLE, light_icon
from .widgets import safe_tooltip

AGENT_ROLE = Qt.ItemDataRole.UserRole + 42  # an entry's harness (claude, codex, ...)
ADD = N_("Add a worker…")


def rows(sai):
    """[{agent, light, words, evidence, limits, run, ...}] of sandbox-ai's cairn_workers.rows(), or [] (a sandbox-ai
    without it, or a table it cannot read: no lights then)."""
    try:
        module = sai.module("cairn_workers") if sai is not None else None
        return list(module.rows()) if module is not None and hasattr(module, "rows") else []
    except Exception as _exc:  # noqa: BLE001 - an older or damaged sandbox-ai: the plain field
        applog.swallowed("cairn_workers_view", "rows: no traffic light of Cairn's workers", _exc)
        return []


def summary(row):
    """In words, for a tooltip: "Claude Code as Cairn's worker: partly working", then what it comes from, what it
    runs and what it cannot give (sandbox-ai's own words: the evidence, the command line, the limits)."""
    words = tr(LIGHT_WORDS.get(row.get("light"), LIGHT_WORDS["grey"]))
    lines = [tr("{agent} as Cairn's worker: {words}", agent=agent_name(row.get("agent")), words=words)]
    if row.get("reason"):
        lines.append(tr("Why: {reason}", reason=printable(str(row["reason"]))))
    lines.append(tr("Evidence: {evidence}", evidence=printable(str(row.get("evidence") or tr("no data yet")))))
    if row.get("run"):
        lines.append(tr("A task runs: {command}", command=printable(str(row["run"]))))
    for limit in row.get("limits") or []:
        lines.append("- " + printable(str(limit)))
    return "\n".join(lines)


def given(text):
    """The harnesses TEXT names (AGENT[=MODEL],...), in its order."""
    return [item.split("=", 1)[0].strip() for item in str(text or "").split(",") if item.split("=", 1)[0].strip()]


class CairnWorkersField(QWidget):
    """The value (AGENT[=MODEL],...) as a line, and "Add a worker…": each harness with its light. CHANGED(text) when
    the value changes by a pick or when editing ends."""

    changed = Signal(str)

    def __init__(self, sai, value="", placeholder="", parent=None):
        super().__init__(parent)
        self.sai = sai
        self.rows = {row["agent"]: row for row in rows(sai) if isinstance(row, dict) and row.get("agent")}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(str(value or ""))
        self.edit.setPlaceholderText(placeholder or tr("empty: every agent of the session"))
        self.edit.setAccessibleName(tr("Cairn's workers"))
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.text()))
        self.edit.textChanged.connect(lambda _text: self.mark())
        layout.addWidget(self.edit, 1)
        self.add = QComboBox()
        self.add.setAccessibleName(tr(ADD))
        self.add.addItem(tr(ADD), None)
        for agent, row in self.rows.items():
            self.add.addItem(light_icon(row.get("light", "grey")), f"{agent_name(agent)} ({agent})", agent)
            index = self.add.count() - 1
            tip = summary(row)
            self.add.setItemData(index, agent, AGENT_ROLE)
            self.add.setItemData(index, row.get("light", "grey"), LIGHT_ROLE)
            self.add.setItemData(index, safe_tooltip(tip), Qt.ItemDataRole.ToolTipRole)
            self.add.setItemData(index, f"{agent_name(agent)}, {tip.splitlines()[0]}",
                                 Qt.ItemDataRole.AccessibleTextRole)
        self.add.activated.connect(self.picked)
        layout.addWidget(self.add)
        self.add.setVisible(bool(self.rows))  # (in its place first: shown without a parent, it flashed as a window)
        self.mark()

    def text(self):
        return self.edit.text().strip()

    def setText(self, value):  # noqa: N802 - Qt's name, as QLineEdit's
        self.edit.setText(str(value or ""))

    def picked(self, index):
        """A harness chosen in the list: added to the value (once), the list back on "Add a worker…"."""
        agent = self.add.itemData(index, AGENT_ROLE)
        self.add.setCurrentIndex(0)
        if not agent or agent in given(self.text()):
            return
        self.edit.setText(",".join([item for item in self.text().split(",") if item.strip()] + [agent]))
        self.changed.emit(self.text())

    def mark(self):
        """The line's tooltip: the light of each harness it names (or how the default is chosen)."""
        named = [agent for agent in given(self.text()) if agent in self.rows]
        if not self.rows:
            return
        if not named:
            tip = tr("Empty: every agent of the session is a worker, the best supported first. The list beside "
                     "shows each harness with its traffic light.")
        else:
            tip = "\n\n".join(summary(self.rows[agent]) for agent in named)
        self.edit.setToolTip(safe_tooltip(tip))
