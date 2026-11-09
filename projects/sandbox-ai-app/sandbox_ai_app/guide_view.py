"""The guide on screen (guide.py has what it says): the tour's panel beside the pages, and the dialog that sets up
a new node of the board one question at a time."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import board as B
from . import guide, tours
from .board_view import PathEdit
from .files import printable
from .i18n import tr
from .widgets import plain_label


class TourPanel(QFrame):
    """One step of the tour: where you are, what the page is for, what to try; Back, Next, Close; and the way into
    the tour of the page's own area."""
    page_requested = Signal(str)  # the window shows this page
    step_changed = Signal(int)    # remembered for the next time
    area_requested = Signal(str)  # the tour of this page's area
    closed = Signal()

    def __init__(self, index=0, parent=None):
        super().__init__(parent)
        self.setObjectName("tourPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        self.where = plain_label(role="secondary")
        self.title = plain_label(role="section")
        self.text = plain_label()
        self.try_ = plain_label()
        self.back = QPushButton(tr("Back"))
        self.next = QPushButton(tr("Next"))
        self.next.setProperty("role", "primary")
        close = QPushButton(tr("Close the tour"))
        self.area = QPushButton(tr("Learn this area"))
        self.area.setToolTip(tr("A tour of this page: each control and how to use it"))
        self.area.clicked.connect(lambda: self.area_requested.emit(guide.TOUR[self.index].page))
        self.back.clicked.connect(lambda: self.go(self.index - 1))
        self.next.clicked.connect(lambda: self.close_tour() if self.index == len(guide.TOUR) - 1
                                  else self.go(self.index + 1))
        close.clicked.connect(self.close_tour)
        row = QHBoxLayout()
        row.addWidget(self.back)
        row.addWidget(self.next)
        row.addWidget(self.area)
        row.addStretch(1)
        row.addWidget(close)
        for widget in (self.where, self.title, self.text, self.try_):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addLayout(row)
        self.index = -1
        self.go(index)

    def go(self, index):
        index = max(0, min(len(guide.TOUR) - 1, int(index)))
        step = guide.TOUR[index]
        changed = index != self.index
        self.index = index
        self.where.setText(tr("Guided tour · {number} of {count}", number=index + 1, count=len(guide.TOUR)))
        self.title.setText(tr(step.title))
        self.text.setText(tr(step.text))
        self.try_.setText(tr("Try: {what}", what=tr(step.try_)))
        self.back.setEnabled(index > 0)
        self.area.setVisible(step.page in tours.AREA_TOURS)
        self.next.setText(tr("Finish") if index == len(guide.TOUR) - 1 else tr("Next"))
        if changed:
            self.page_requested.emit(step.page)
            self.step_changed.emit(index)

    def close_tour(self):
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_tour()
        else:
            super().keyPressEvent(event)


class AreaTourPanel(QFrame):
    """The tour of one area (tours.py): a step per control, the control outlined in the window; Back, Next,
    Close."""
    page_requested = Signal(str)
    target_requested = Signal(str, str)  # page, the page's attribute to outline ("" for none)
    closed = Signal()

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.setObjectName("tourPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.page, self.steps = page, tours.tour_of(page)
        layout = QVBoxLayout(self)
        self.where = plain_label(role="secondary")
        self.title = plain_label(role="section")
        self.text = plain_label()
        self.try_ = plain_label()
        for label in (self.text, self.try_):
            label.setWordWrap(True)
        self.back = QPushButton(tr("Back"))
        self.next = QPushButton(tr("Next"))
        self.next.setProperty("role", "primary")
        close = QPushButton(tr("Close the tour"))
        self.back.clicked.connect(lambda: self.go(self.index - 1))
        self.next.clicked.connect(lambda: self.close_tour() if self.index == len(self.steps) - 1
                                  else self.go(self.index + 1))
        close.clicked.connect(self.close_tour)
        row = QHBoxLayout()
        row.addWidget(self.back)
        row.addWidget(self.next)
        row.addStretch(1)
        row.addWidget(close)
        for widget in (self.where, self.title, self.text, self.try_):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addLayout(row)
        self.index = -1

    def start(self):
        self.page_requested.emit(self.page)
        self.go(0)

    def go(self, index):
        index = max(0, min(len(self.steps) - 1, int(index)))
        step = self.steps[index]
        self.index = index
        page_title = tr(dict(guide_pages()).get(self.page, self.page))
        self.where.setText(tr("Tour of {area} · {number} of {count}", area=page_title, number=index + 1,
                              count=len(self.steps)))
        self.title.setText(tours.say(step.title))
        self.text.setText(tours.say(step.text))
        self.try_.setText(tr("Try: {what}", what=tours.say(step.try_)))
        self.back.setEnabled(index > 0)
        self.next.setText(tr("Finish") if index == len(self.steps) - 1 else tr("Next"))
        self.target_requested.emit(self.page, step.target or "")

    def close_tour(self):
        self.hide()
        self.target_requested.emit(self.page, "")
        self.closed.emit()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_tour()
        else:
            super().keyPressEvent(event)


class TaskTourPanel(QFrame):
    """A tour through a whole task (tours.TASK_TOURS): each step shows its page and outlines its control; a step
    with a check is ticked off as soon as the window sees it done (CHECK(name) -> bool, asked twice a second)."""
    page_requested = Signal(str)
    target_requested = Signal(str, str)  # page, the page's attribute to outline ("" for none)
    closed = Signal()

    def __init__(self, ident, check=None, parent=None):
        super().__init__(parent)
        self.setObjectName("tourPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.tour, self.check = tours.task_tour(ident), check
        self.steps = self.tour.steps
        layout = QVBoxLayout(self)
        self.where = plain_label(role="secondary")
        self.title = plain_label(role="section")
        self.text = plain_label()
        self.try_ = plain_label()
        self.done = plain_label(role="notice-ok")
        self.done.hide()
        self.back = QPushButton(tr("Back"))
        self.next = QPushButton(tr("Next"))
        self.next.setProperty("role", "primary")
        close = QPushButton(tr("Close the tour"))
        self.back.clicked.connect(lambda: self.go(self.index - 1))
        self.next.clicked.connect(lambda: self.close_tour() if self.index == len(self.steps) - 1
                                  else self.go(self.index + 1))
        close.clicked.connect(self.close_tour)
        row = QHBoxLayout()
        row.addWidget(self.back)
        row.addWidget(self.next)
        row.addStretch(1)
        row.addWidget(close)
        for widget in (self.where, self.title, self.text, self.try_, self.done):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addLayout(row)
        self.index = -1
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.look)

    def start(self):
        self.go(0)
        if self.check is not None:
            self.timer.start()

    def go(self, index):
        index = max(0, min(len(self.steps) - 1, int(index)))
        step = self.steps[index]
        self.index = index
        self.where.setText(tr("{tour} · {number} of {count}", tour=tours.say(self.tour.title), number=index + 1,
                              count=len(self.steps)))
        self.title.setText(tours.say(step.title))
        self.text.setText(tours.say(step.text))
        self.try_.setText(tr("Try: {what}", what=tours.say(step.try_)))
        self.back.setEnabled(index > 0)
        self.next.setText(tr("Finish") if index == len(self.steps) - 1 else tr("Next"))
        self.page_requested.emit(step.page)
        self.target_requested.emit(step.page, step.target or "")
        self.look()

    def is_done(self):
        step = self.steps[self.index]
        if not step.check or self.check is None:
            return False
        try:
            return bool(self.check(step.check))
        except Exception:  # noqa: BLE001 - a check never stops the tour
            return False

    def look(self):
        """Tick the step off when its check holds."""
        done = self.is_done()
        self.done.setText(tr("✔ Done: go on with Next.") if done else "")
        self.done.setVisible(done)

    def close_tour(self):
        self.timer.stop()
        self.hide()
        self.target_requested.emit(self.steps[self.index].page if self.index >= 0 else "", "")
        self.closed.emit()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_tour()
        else:
            super().keyPressEvent(event)


def guide_pages():
    """(page key, title) of the navigation, for the tour's heading."""
    return [(step.page, step.title) for step in guide.TOUR]


class NodeGuide(QDialog):
    """Set up NODE (a new one, or one of the board) question by question; then what is still wrong with it.
    answers() is what the dialog leaves: {question key: answer}, for guide.props_of."""

    def __init__(self, node, problems=(), parent=None):
        super().__init__(parent)
        self.node, self.problems = node, list(problems)
        self.asked = list(guide.questions(node.kind))
        self.setWindowTitle(tr("Set up: {name}", name=tr(B.KINDS[node.kind].title)))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.where = plain_label(role="secondary")
        layout.addWidget(self.where)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.inputs = {}
        intro = QWidget()
        intro_layout = QVBoxLayout(intro)
        intro_layout.addWidget(plain_label(tr(B.KINDS[node.kind].title), role="section"))
        intro_layout.addWidget(plain_label(tr(B.KINDS[node.kind].help)))
        intro_layout.addWidget(plain_label(tr("{count} question(s); each can be changed later in the inspector.",
                                              count=len(self.asked)), role="secondary"))
        intro_layout.addStretch(1)
        self.pages.addWidget(intro)
        for question in self.asked:
            self.pages.addWidget(self.question_page(question))
        self.summary = QListWidget()
        end = QWidget()
        end_layout = QVBoxLayout(end)
        end_layout.addWidget(plain_label(tr("What the board still finds"), role="section"))
        end_layout.addWidget(self.summary, 1)
        self.pages.addWidget(end)
        row = QHBoxLayout()
        self.back = QPushButton(tr("Back"))
        self.next = QPushButton(tr("Next"))
        self.next.setProperty("role", "primary")
        cancel = QPushButton(tr("Cancel"))
        self.back.clicked.connect(lambda: self.show_page(self.pages.currentIndex() - 1))
        self.next.clicked.connect(self.forward)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addStretch(1)
        row.addWidget(self.back)
        row.addWidget(self.next)
        layout.addLayout(row)
        self.show_page(0)

    def question_page(self, question):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(plain_label(tr(question.label) + (" *" if question.needed else ""), role="section"))
        layout.addWidget(plain_label(tr(question.help)))
        current = guide.answer_of(self.node, question)
        if question.form == "flag":
            widget = QCheckBox(tr(question.label))
            widget.setChecked(bool(current))
        elif question.form == "list":
            widget = QPlainTextEdit("\n".join(current))
            widget.setPlaceholderText(question.placeholder)
        elif question.form in ("choice", "switches"):
            widget = QComboBox()
            choices = question.choices
            if question.key == "--gpu":  # only the kinds this computer has (and the node's own, kept)
                from . import hardware
                found = hardware.gpu_kinds()
                choices = [(v, l) for v, l in choices if not v or v in found or v == current]
            for value, label in choices:
                widget.addItem(tr(label), value)
            widget.setCurrentIndex(max(0, widget.findData(current)))
        elif question.form in ("model", "effort"):
            widget = QComboBox()
            widget.setEditable(question.form == "model")
            self.fill_agent_choices(question, widget, self.node.props.get("agent", "claude"), current)
        elif question.key == "--vpn":  # the board's host's VPN profiles to choose from, any other name typed
            from .name_fields import NameField
            board = self.parent()
            widget = NameField("vpn", getattr(board, "sai", None), board.host() if hasattr(board, "host") else "local",
                               placeholder=question.placeholder)
            widget.setText(current or "")
            widget.setAccessibleName(tr(question.label))
        elif question.form == "path":
            widget = PathEdit(current, directory=question.key != "source")
            if question.placeholder:
                widget.edit.setPlaceholderText(question.placeholder)
        else:
            widget = QLineEdit(current)
            widget.setPlaceholderText(question.placeholder)
            if question.key in ("--memory", "--cpus"):
                from . import hardware
                memory, cpus = hardware.limits_hint()
                have = memory if question.key == "--memory" else tr("{count} CPUs", count=cpus)
                widget.setPlaceholderText(tr("{example} (this computer: {have})", example=question.placeholder,
                                             have=have))
        if question.key == "--device" and isinstance(widget, QPlainTextEdit):
            from . import hardware
            found = hardware.devices()
            if found:
                widget.setPlaceholderText(tr("This computer has: {devices}",
                                             devices=", ".join(path for path, _, _ in found[:8])))
        self.inputs[question.key] = (question, widget)
        if question.key == "agent":  # the model's and the effort's choices follow the agent
            widget.currentIndexChanged.connect(lambda _i, w=widget: self.agent_changed(w.currentData()))
        layout.addWidget(widget)
        layout.addStretch(1)
        return page

    @staticmethod
    def fill_agent_choices(question, widget, agent, current):
        """The model names or effort levels AGENT knows; the empty choice is its default."""
        if question.form == "model":  # each model with its version (models.py says where the lists come from)
            from . import models
            models.fill_combo(widget, agent, current)
            widget.setEnabled(True)
            return
        widget.blockSignals(True)
        widget.clear()
        widget.addItem(tr("the agent's default"), "")
        for value in guide.AGENT_EFFORTS.get(agent, ()):
            widget.addItem(value, value)
        index = widget.findData(current)
        if index >= 0:
            widget.setCurrentIndex(index)
        elif question.form == "model" and current:
            widget.setEditText(current)  # a name it does not list, kept as typed
        else:
            widget.setCurrentIndex(0)
        widget.setEnabled(question.form == "model" or widget.count() > 1)
        widget.blockSignals(False)

    def agent_changed(self, agent):
        for key in ("model", "effort"):
            if key in self.inputs:
                question, widget = self.inputs[key]
                value = widget.currentData() if widget.currentText() == widget.itemText(widget.currentIndex()) \
                    else widget.currentText()
                self.fill_agent_choices(question, widget, agent, value)

    def answers(self):
        found = {}
        for key, (question, widget) in self.inputs.items():
            if isinstance(widget, QCheckBox):
                found[key] = widget.isChecked()
            elif isinstance(widget, QPlainTextEdit):
                found[key] = widget.toPlainText()
            elif isinstance(widget, QComboBox) and widget.isEditable():
                index = widget.findText(widget.currentText())
                data = widget.itemData(index) if index >= 0 else None
                found[key] = data if data is not None else widget.currentText().strip()
            elif isinstance(widget, QComboBox):
                found[key] = widget.currentData() if widget.currentData() is not None else ""
            elif isinstance(widget, PathEdit):
                found[key] = widget.edit.text()
            else:
                found[key] = widget.text()
        return found

    def preview(self):
        """The node as the answers would leave it (a copy: the board changes only when the dialog is accepted)."""
        node = B.Node(self.node.id, self.node.kind, dict(self.node.props))
        for key, value in guide.props_of(node.kind, self.answers()).items():
            if value is None:
                node.props.pop(key, None)
            else:
                node.props[key] = value
        return node

    def show_page(self, index):
        index = max(0, min(self.pages.count() - 1, index))
        self.pages.setCurrentIndex(index)
        last = index == self.pages.count() - 1
        if last:
            self.summary.clear()
            items = guide.checklist(self.problems, self.preview())
            for level, text in items or [("ok", tr("Nothing: the node is ready."))]:
                symbol = {"error": "✘", "warning": "!"}.get(level, "✔")
                self.summary.addItem(QListWidgetItem(f"{symbol} {printable(text)}"))
        self.where.setText(tr("Step {number} of {count}", number=index + 1, count=self.pages.count()))
        self.back.setEnabled(index > 0)
        self.next.setText(tr("Done") if last else tr("Next"))

    def forward(self):
        if self.pages.currentIndex() == self.pages.count() - 1:
            self.accept()
        else:
            self.show_page(self.pages.currentIndex() + 1)


def apply(page, node_id, answers):
    """The ANSWERS of the dialog on node NODE_ID of the BoardPage PAGE, as one undo step."""
    node = page.board.node(node_id)
    if node is None:
        return
    updates = guide.props_of(node.kind, answers)

    def change():
        for key, value in updates.items():
            if value is None:
                node.props.pop(key, None)
            else:
                node.props[key] = value
    page.change(tr("Set up {name}", name=tr(B.KINDS[node.kind].title)), change)

