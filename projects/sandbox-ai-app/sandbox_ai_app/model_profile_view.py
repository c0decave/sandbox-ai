"""The "What we know" box (Qt): what sandbox-ai's research knows of a model in an agent, beside the model profile on
the Board (the LLM part's and the agent's properties) and on the Start page.

User: "please also add a box to the app with agent + harness what we know and what we dont with short bullet points
and what is working and what not." model_profile.knowledge() gives the words; this box shows them compactly: a title
line that folds the rest away (folded stays folded for every box of this run of the app), the pair's status, then
✓ works, ✗ does not work, ? not known yet. Its header's commands (fold, copy) are on its context menu too.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QApplication, QFrame, QToolButton, QVBoxLayout, QWidget

from . import widgets
from .i18n import tr
from .widgets import context_menu, disclosure, exec_dropped, plain_label, safe_tooltip

# Folded or not, for every box (the Board's properties are built anew for each part shown).
STATE = {"open": True}
ROLES = {"✓": "notice-ok", "✗": "notice-warn", "?": "secondary"}


class KnowledgeBox(QFrame):
    """The box; set(found) fills it from model_profile.knowledge() (None: hidden)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "card")
        self.found = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        self.header = disclosure(QToolButton())  # flat with its arrow, never a button stuck pressed
        self.header.setAccessibleName(tr("What we know"))
        self.header.setToolTip(safe_tooltip(tr("Click to fold or unfold it; right-click to copy it")))
        self.header.toggled.connect(self.fold)
        for widget in (self.header, self):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda point, w=widget: exec_dropped(self.menu(), w.mapToGlobal(point)))
        layout.addWidget(self.header)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(4, 0, 0, 0)
        self.rows.setSpacing(1)
        layout.addWidget(self.body)
        self.hide()

    def title(self):
        return self.found["title"] if self.found else ""

    def text(self):
        from .model_profile import knowledge_text
        return knowledge_text(self.found) if self.found else ""

    def set(self, found):
        """Show FOUND (model_profile.knowledge()), or hide the box (None)."""
        self.found = found
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not found:
            self.hide()
            return
        self.header.setText(found["title"])
        for line in found["lines"]:  # a pair or a model known not to work: that line stands out
            self.rows.addWidget(plain_label(line, role="notice-warn" if line == found["bad"] else None,
                                            selectable=True))
        for sign, heading, bullets in found["groups"]:
            self.rows.addWidget(plain_label(f"{sign} {heading}", role="section"))
            self.rows.addWidget(plain_label("\n".join(f"  • {text}" for text in bullets), role=ROLES.get(sign),
                                            selectable=True))
        self.fold(STATE["open"], remember=False)
        self.show()

    def fold(self, opened, remember=True):
        """Unfold (OPENED) or fold the box to its title line; REMEMBER: every box from now on alike."""
        opened = bool(opened)
        if remember:
            STATE["open"] = opened
        if self.header.isChecked() != opened:
            self.header.blockSignals(True)
            self.header.setChecked(opened)
            self.header.blockSignals(False)
        self.header.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self.body.setVisible(opened)

    def copy(self):
        QApplication.clipboard().setText(self.text())

    def menu(self):
        """Its context menu: fold or unfold (the header's click), copy."""
        opened = self.body.isVisibleTo(self)
        return context_menu(self, [(tr("Hide details") if opened else tr("Show details"), True, None,
                                    lambda: self.fold(not opened)),
                                   (tr("Copy what we know"), bool(self.found), None, self.copy)])


# --- the traffic light (Qt): an icon per colour, the light on a model's list entry, the detail panel ------------------
# User: "farbliche Ampel zu den Modellen ... Detail-Infos, was funktioniert hat und was nicht, pro Modell". The colour
# is the theme's status colour (ok, warn, bad, muted: readable on every theme) and each colour has a shape of its own
# drawn into it - a check (verified), a bar (partly working), a cross (does not work), a ring (no data yet) - so it reads
# without the colour; its words are in the entry's tooltip and accessible text (model_profile.light_summary).

_ICONS = {}


def light_icon(colour, size=14):
    """The icon of COLOUR (green, yellow, red, grey): a disc in the theme's status colour with its own shape."""
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
    from . import theme
    from .model_profile import LIGHT_THEME
    colour = colour if colour in LIGHT_THEME else "grey"
    fill = theme.color(LIGHT_THEME[colour])
    key = (colour, size, fill)
    if key in _ICONS:
        return _ICONS[key]
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    if colour == "grey":  # a ring: nothing known
        painter.setPen(QPen(QColor(fill), s / 7))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(s * .15, s * .15, s * .7, s * .7))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawEllipse(QRectF(0.5, 0.5, s - 1, s - 1))
        pen = QPen(QColor("#ffffff"), s / 7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if colour == "green":
            painter.drawPolyline([QPointF(s * .27, s * .52), QPointF(s * .44, s * .69), QPointF(s * .74, s * .33)])
        elif colour == "yellow":
            painter.drawLine(QPointF(s * .28, s * .5), QPointF(s * .72, s * .5))
        else:
            painter.drawLine(QPointF(s * .32, s * .32), QPointF(s * .68, s * .68))
            painter.drawLine(QPointF(s * .68, s * .32), QPointF(s * .32, s * .68))
    painter.end()
    icon = _ICONS[key] = QIcon(pixmap)
    return icon


def mark_combo(combo, sai, model_of=None, agent=None):
    """Each entry of COMBO (a list of a server's models) with its light: the icon, the summary in its tooltip and its
    accessible text. MODEL_OF(index) is the entry's model (default: the MODEL of its data NAME:MODEL); an entry
    without a model (the agent's provider, a server's name alone) is left as it is. Nothing without a sandbox-ai that
    has the traffic light."""
    from .model_profile import light_accessible, light_summary, support_of, table
    if table(sai) is None:
        return
    from . import wizard_llm as WL
    from .widgets import safe_tooltip

    def default(index):
        value = combo.itemData(index)
        if not isinstance(value, str) or ":" not in value:
            return None
        return WL.parse_value(value)[1].split(",")[0] or None  # NAME:MODEL,MODEL...: the agent's (the first)
    model_of = model_of or default
    lights = {}
    for index in range(combo.count()):
        model = model_of(index)
        found = support_of(sai, model) if model else None
        if found is None:
            continue
        colour, summary = agent_light(found, agent)
        combo.setItemIcon(index, light_icon(colour))
        combo.setItemData(index, colour, LIGHT_ROLE)
        tip = combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
        combo.setItemData(index, safe_tooltip(f"{tip}\n{summary}" if tip else summary), Qt.ItemDataRole.ToolTipRole)
        combo.setItemData(index, f"{combo.itemText(index)}, {light_accessible(found, model)}",
                          Qt.ItemDataRole.AccessibleTextRole)
        lights[index] = colour
    order_by_light(combo, lights, model_of)


LIGHT_ROLE = Qt.ItemDataRole.UserRole + 41  # an entry's light colour (what its icon shows)


def agent_light(found, agent):
    """(colour, words) of a model's light (FOUND: support_of) for AGENT: that agent's own measured light - grey (no
    data yet) for an agent the model was never run in, whatever the other agents or its family say - named first,
    then the others; without AGENT the best of its measured harnesses (the LLM servers' pages: no agent chosen
    there). The user 2026-11-22 saw "verified" for a model that was only partly working in their agent, then
    "opencode verifiziert" for one never run in OpenCode: only that agent's own runs."""
    from .model_profile import LIGHT_WORDS, agent_name, light_summary
    summary = light_summary(found)
    if not agent:
        return found["light"], summary
    measured = {h.get("agent"): h.get("light") for h in found.get("harnesses") or []}
    colour = measured.get(agent) or "grey"
    own = tr("In {agent}: {words}", agent=agent_name(agent), words=tr(LIGHT_WORDS.get(colour, LIGHT_WORDS["grey"])))
    return colour, own if not found.get("harnesses") else f"{own}. {summary}"  # (nothing measured: said once)


LIGHT_ORDER = {"green": 0, "yellow": 1, "red": 2, "grey": 3}  # (the user 2026-11-22: "green first, then orange...")


def order_by_light(combo, lights, model_of):
    """COMBO's models in the order of their lights (LIGHTS: index -> colour): green, yellow, red, grey, then models
    without one, each group in its order. Leading entries without a model (the agent's provider, "none") stay first;
    the chosen entry stays chosen and nothing is said about a change (no signal: only the order changed).
    The rows are rewritten in place from what each had (its text, icon, every data role and its flags), never moved
    with takeRow/insertRow: those keep a wrapper of each QStandardItem that outlives it (see widgets.ITEM_FLAGS_ROLE).
    """
    model = combo.model()
    if not isinstance(model, QStandardItemModel):  # (a combo box with a model of its own: left as it is)
        return
    first = 0
    while first < combo.count() and not model_of(first):
        first += 1
    rank = {index: LIGHT_ORDER.get(lights.get(index), len(LIGHT_ORDER)) for index in range(first, combo.count())}
    wanted = sorted(rank, key=lambda index: (rank[index], index))
    if wanted == list(range(first, combo.count())):
        return
    current = combo.currentIndex()
    column = combo.modelColumn()
    rows = {index: (model.itemData(model.index(index, column)), widgets.row_flags(combo, index)) for index in wanted}
    blocked = combo.blockSignals(True)
    try:
        for position, index in enumerate(wanted):
            row = first + position
            roles, flags = rows[index]
            model.clearItemData(model.index(row, column))
            model.setItemData(model.index(row, column), roles)
            widgets.set_row_flags(combo, row, flags)
        if current >= first:
            combo.setCurrentIndex(first + wanted.index(current))
    finally:
        combo.blockSignals(blocked)


class SupportPanel(QFrame):
    """The detail panel of one model's light: how far the research supports it per agent - the status, the round and
    date of the evidence, what worked (✓) and what failed (✗), the recommended settings, the measured speed - and
    "no data yet" when nothing is known. set(found) fills it from sandbox-ai's support() (None: hidden). Copy is on
    its context menu."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "card")
        self.details = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        self.setAccessibleName(tr("Support of the model"))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda point: exec_dropped(self.menu(), self.mapToGlobal(point)))
        self.rows = layout
        self.hide()

    def text(self):
        from .model_profile import support_text
        return support_text(self.details) if self.details else ""

    def set(self, found):
        """Show FOUND (sandbox-ai's support() of a model), or hide the panel (None)."""
        from PySide6.QtWidgets import QHBoxLayout, QLabel
        from .model_profile import support_details
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                while item.layout().count():
                    inner = item.layout().takeAt(0)
                    if inner.widget() is not None:
                        inner.widget().deleteLater()
        self.details = support_details(found) if found else None
        if self.details is None:
            self.hide()
            return
        details = self.details

        def headed(colour, text, role):
            row = QHBoxLayout()
            icon = QLabel()
            icon.setPixmap(light_icon(colour).pixmap(14, 14))
            icon.setAccessibleName(text)
            row.addWidget(icon)
            label = plain_label(text, role=role, selectable=True)
            row.addWidget(label, 1)
            self.rows.addLayout(row)
            return label
        headed(details["light"], f"{details['title']}: {details['summary']}", "section")
        for line in details["lines"]:
            self.rows.addWidget(plain_label(line, role="secondary", selectable=True))
        for pair in details["harnesses"]:
            headed(pair["light"], f"{pair['symbol']} {pair['title']}", None)
            body = [f"    {line}" for line in pair["lines"]]
            if body:
                self.rows.addWidget(plain_label("\n".join(body), role="secondary", selectable=True))
            for sign, texts in (("✓", pair["works"]), ("✗", pair["fails"])):
                if texts:
                    self.rows.addWidget(plain_label("\n".join(f"    {sign} {text}" for text in texts),
                                                    role=ROLES.get(sign), selectable=True))
        if details["untested"]:
            self.rows.addWidget(plain_label(details["untested"], role="secondary", selectable=True))
        self.setAccessibleDescription(details["summary"])
        self.show()

    def copy(self):
        QApplication.clipboard().setText(self.text())

    def menu(self):
        return context_menu(self, [(tr("Copy the support details"), bool(self.details), None, self.copy)])


def scrolled(panel, height=240):
    """PANEL in an area that scrolls when its details are longer than HEIGHT pixels (a list of models stays in
    view)."""
    from PySide6.QtWidgets import QScrollArea
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(panel)
    panel.setVisible(bool(getattr(panel, "details", None)))  # setWidget shows it: empty, it stays hidden
    area.setMaximumHeight(height)
    area.setAccessibleName(tr("Support of the model"))
    return area
