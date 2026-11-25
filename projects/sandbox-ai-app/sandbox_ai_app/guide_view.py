"""The guide on screen (guide.py has what it says): the tour's panel beside the pages, and the dialog that sets up
a new node of the board one question at a time."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import applog
from . import board as B
from . import board_icons, theme
from . import guide, tours
from . import list_fields as LF
from .board_view import VALUE_NAMES, PathEdit
from .files import printable
from .widgets import safe_tooltip
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
        except Exception as _exc:  # noqa: BLE001 - a check never stops the tour
            applog.swallowed("guide_view", "TaskTourPanel.is_done: a check never stops the tour", _exc)
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
    answers() is what the dialog leaves: {question key: answer}, for guide.props_of. Its first step introduces the
    kind of part; SKIP_INTRO (the board shows it once per kind: intro_seen) starts at the first question instead,
    and "Show the introduction" goes back to it."""

    def __init__(self, node, problems=(), parent=None, skip_intro=False):
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
        heading = QHBoxLayout()  # the kind's icon (decoration) before its name
        mark = QLabel()
        mark.setPixmap(board_icons.pixmap(node.kind, board_icons.SIZE, theme.colors()["title"],
                                          self.devicePixelRatioF()))
        mark.setFixedSize(board_icons.SIZE, board_icons.SIZE)
        heading.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(plain_label(tr(B.KINDS[node.kind].title), role="section"), 1)
        intro_layout.addLayout(heading)
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
        self.intro = QPushButton(tr("Show the introduction"))
        self.intro.setToolTip(tr("What this part is, again (the first step, shown once per kind of part)"))
        self.intro.clicked.connect(lambda: self.show_page(0))
        row.addWidget(cancel)
        row.addWidget(self.intro)
        row.addStretch(1)
        row.addWidget(self.back)
        row.addWidget(self.next)
        layout.addLayout(row)
        self.show_page(1 if skip_intro and self.asked else 0)

    def question_page(self, question):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(plain_label(tr(question.label) + (" *" if question.needed else ""), role="section"))
        layout.addWidget(plain_label(tr(question.help)))
        current = guide.answer_of(self.node, question)
        if question.form == "flag":
            widget = QCheckBox(tr(question.label))
            widget.setChecked(bool(current))
            if question.key == "--toolchain-offline":  # (what the toolchains chosen need: closed then, said there)
                widget.toggled.connect(lambda _on: self.show_needs())
        elif question.form == "list" and self.provider_of(question.key) is not None:
            # What the board's host has to choose from, as in the part's properties (list_fields: a drop-down, the
            # chosen ones as chips): its toolchains - the store's and the signed catalog's, every version - and its
            # devices. (It was a box to type names into: "in der Erstsetup KEIN Dropdown", user 2026-11-24.)
            widget = self.list_field(question, current)
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
        elif question.form in ("model", "effort", "thinking"):
            widget = QComboBox()
            widget.setEditable(question.form == "model")
            self.fill_agent_choices(question, widget, self.node.props.get("agent", "claude"), current,
                                    self.parent(), model=self.chosen_model())
        elif self.node.kind == "service" and question.key == "image" and hasattr(self.parent(), "image_combo"):
            # Your images to choose from (sandbox-ai images list), "Other…" for a name typed in.
            widget = self.parent().image_combo(current)
            widget.setAccessibleName(tr(question.label))
        elif self.node.kind == "mcp" and question.key == "name" and hasattr(self.parent(), "mcp_combo"):
            # The servers there are to choose from (defined, the project's own, the catalogs'), any other typed.
            widget = self.parent().mcp_combo(current)
            widget.setAccessibleName(tr(question.label))
        elif question.key in VALUE_NAMES:
            # what the board's host has to choose from (VPN profiles, image variants, model servers), any other typed
            from .name_fields import NameField
            board = self.parent()
            widget = NameField(VALUE_NAMES[question.key], getattr(board, "sai", None),
                               board.host() if hasattr(board, "host") else "local", placeholder=question.placeholder)
            widget.setText(current or "")
            widget.setAccessibleName(tr(question.label))
        elif question.form == "path":
            # a folder or file of the host the board starts on: chosen from that host's folders (remote_chooser)
            board = self.parent()
            widget = PathEdit(current, directory=question.key != "source",
                              host=board.host() if hasattr(board, "host") else "local")
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
        if question.key == "model":  # the effort's choices follow the model (the levels it takes)
            widget.currentIndexChanged.connect(lambda _i: self.model_changed())
            widget.lineEdit().editingFinished.connect(self.model_changed)
        layout.addWidget(widget)
        if question.key == "--toolchain" and isinstance(widget, LF.ListFields):
            # What each chosen toolchain needs of the network (its package registries), as the properties show it.
            self.needs_note = plain_label("", role="secondary", selectable=True)
            layout.addWidget(self.needs_note)
            widget.changed.connect(lambda _key, _values: self.show_needs())
            self.show_needs()
        if question.key == "effort":  # an effort the model chosen does not take was reset: said here
            self.effort_note = plain_label(role="secondary")
            self.effort_note.hide()
            layout.addWidget(self.effort_note)
        layout.addStretch(1)
        return page

    def provider_of(self, key):
        """What the board offers for the list question KEY (BoardPage.value_provider: provider(fill)), or None."""
        ask = getattr(self.parent(), "value_provider", None)
        if key not in ("--toolchain", "--device") or ask is None:
            return None
        provider = ask(key)
        if provider is None:
            return None
        if key == "--toolchain":
            def with_needs(fill):  # (once the host's toolchains are read: what the chosen ones need, below)
                provider(lambda items, note="": (fill(items, note), self.show_needs()))
            return with_needs
        from . import device_picker as DP

        def devices_only(fill):  # (a GPU kind is the part's own question, GPU: never a --device)
            provider(lambda items, note="": fill([item for item in items or [] if not DP.gpu_of(
                item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else item)], note))
        return devices_only

    def list_field(self, question, current):
        """The list question as one field with the host's values to choose from (list_fields.ListFields)."""
        kind = LF.ListKind(question.key, tr(question.label), tip=tr(question.help),
                           placeholder=LF.EXAMPLES.get(question.key) or question.placeholder)
        if question.key == "--device":
            from . import device_picker as DP
            kind.check = DP.check_board_value
        field = LF.ListFields([kind], {question.key: current}, name=tr(question.label))
        field.set_provider(question.key, self.provider_of(question.key))
        return field

    def show_needs(self):
        """Below the toolchains: what each chosen one needs of the network and what the start does about it."""
        note, board = getattr(self, "needs_note", None), self.parent()
        if note is None or not hasattr(board, "toolchains_found"):
            return
        question, field = self.inputs.get("--toolchain", (None, None))
        values = field.values("--toolchain") if isinstance(field, LF.ListFields) else []
        found = board.toolchains_found.get(board.host())
        offline = self.inputs.get("--toolchain-offline")
        rows = B.needs_rows(values, found, offline=bool(offline and offline[1].isChecked()),
                            mode=B.security_mode(board.board)) if found else []
        try:
            note.setText("\n".join(B.toolchain_needs_lines(rows)))
            note.setVisible(bool(rows))
        except RuntimeError:  # (a late answer for a dialog already gone)
            pass

    def chosen_model(self):
        """The model answered so far (the model page's choice, else the node's)."""
        if "model" in getattr(self, "inputs", {}):
            question, widget = self.inputs["model"]
            if not widget.property("server"):
                from . import models
                return models.combo_value(widget)
            return ""
        return str(self.node.props.get("model") or "")

    def model_changed(self):
        """The effort's choices for the model now chosen; an effort it does not take is reset, with a note."""
        if "effort" not in self.inputs:
            return
        question, widget = self.inputs["effort"]
        before = widget.currentData() or ""
        agent = self.inputs["agent"][1].currentData() if "agent" in self.inputs else \
            self.node.props.get("agent", "claude")
        self.fill_agent_choices(question, widget, agent, before, self.parent(), model=self.chosen_model())
        after = widget.currentData() or ""
        note = getattr(self, "effort_note", None)
        if note is not None:
            if before and not after:
                note.setText(tr("The effort {level} was reset: {model} does not take it.", level=before,
                                model=self.chosen_model() or tr("the agent's default model")))
                note.setVisible(True)
            elif after:
                note.setVisible(False)

    @staticmethod
    def fill_agent_choices(question, widget, agent, current, page=None, model=""):
        """The model names or effort levels AGENT knows (the effort's: those MODEL takes, models.efforts); the empty
        choice is its default. With a model server on the board of PAGE (--llm), the models are that server's
        (NAME:MODEL; the launcher refuses --model with --llm)."""
        if question.form == "thinking":  # shown, hidden or the agent's default; none to choose without a switch
            from . import thinking
            widget.blockSignals(True)
            widget.clear()
            for value, label in thinking.choices(agent):
                widget.addItem(label, value)
            widget.setCurrentIndex(max(0, widget.findData(current or "")) if thinking.available(agent) else 0)
            widget.setEnabled(thinking.available(agent))
            widget.setToolTip(safe_tooltip(thinking.tooltip(agent)))
            widget.blockSignals(False)
            return
        if question.form == "model":  # each model with its version (models.py says where the lists come from)
            server = server_models(page)
            widget.setProperty("server", server is not None)
            if server is not None:
                fill_server_models(widget, *server)
                return
            from . import models
            models.fill_combo(widget, agent, current, getattr(page, "sai", None))
            widget.setEnabled(True)
            return
        from . import models
        sai = getattr(page, "sai", None)
        widget.blockSignals(True)
        widget.clear()
        default = models.default_effort(agent, model, sai)
        widget.addItem(tr("the agent's default ({level} for this model)", level=default) if default
                       else tr("the agent's default"), "")
        for value in (models.efforts(agent, model, sai) if guide.AGENT_EFFORTS.get(agent) else ()):
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
        for key in ("model", "effort", "thinking"):
            if key in self.inputs:
                question, widget = self.inputs[key]
                value = widget.currentData() if widget.currentText() == widget.itemText(widget.currentIndex()) \
                    else widget.currentText()
                self.fill_agent_choices(question, widget, agent, value, self.parent(), model=self.chosen_model())

    def answers(self):
        found = {}
        for key, (question, widget) in self.inputs.items():
            if isinstance(widget, QCheckBox):
                found[key] = widget.isChecked()
            elif isinstance(widget, LF.ListFields):  # the chips, and what was typed and not added yet
                widget.take_typed()
                found[key] = widget.values(key)
            elif isinstance(widget, QPlainTextEdit):
                found[key] = widget.toPlainText()
            elif isinstance(widget, QComboBox) and widget.isEditable():
                index = widget.findText(widget.currentText())
                data = widget.itemData(index) if index >= 0 else None
                found[key] = data if isinstance(data, str) and not data.startswith("\x00") else \
                    widget.currentText().strip()
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
        self.intro.setVisible(index > 0)
        self.next.setText(tr("Done") if last or self.ends_here(index) else tr("Next"))

    def ends_here(self, index):
        """INDEX is the last question and the board finds nothing wrong with the part: the guide ends there - the
        step "What the board still finds" is shown only when it has something to say."""
        return index == self.pages.count() - 2 and index > 0 and \
            not guide.checklist(self.problems, self.preview())

    def forward(self):
        index = self.pages.currentIndex()
        if index == self.pages.count() - 1 or self.ends_here(index):
            self.accept()
        else:
            self.show_page(index + 1)


def server_models(page):
    """(name, [NAME:MODEL ...], current --llm value) of the model server on PAGE's board, or None (none used)."""
    from . import wizard_llm as WL
    board = getattr(page, "board", None)
    if board is None or not WL.uses_server(board):
        return None
    current = str(board.first("llm").props.get("--llm") or "")
    name, _ = WL.parse_value(current)
    values = []
    for endpoint in getattr(page, "llm_servers", None) or []:
        if isinstance(endpoint, dict) and endpoint.get("name") == name:
            values = WL.placeholder_choices(endpoint)
    return name, values, current


def fill_server_models(widget, name, values, current):
    """WIDGET (the Model question) with the models of the server NAME: NAME (its default) and NAME:MODEL each."""
    widget.blockSignals(True)
    try:
        widget.clear()
        for value in values or [name]:
            widget.addItem(value, value)
        if current and widget.findData(current) < 0:
            widget.addItem(current, current)
        widget.setCurrentIndex(max(0, widget.findData(current)))
        widget.setEnabled(True)
        widget.setToolTip(safe_tooltip(tr("The model of the model server {name} (--llm {name}:MODEL); the agent's "
                                          "own models are not used with a server", name=printable(name))))
    finally:
        widget.blockSignals(False)


def apply(page, node_id, answers):
    """The ANSWERS of the dialog on node NODE_ID of the BoardPage PAGE, as one undo step. With a model server on the
    board, the model answered is the server's (--llm NAME:MODEL), never --model."""
    node = page.board.node(node_id)
    if node is None:
        return
    models = server_models(page) if node.kind == "agent" and "model" in answers else None
    if models is not None:
        from . import wizard_llm as WL
        value = str(answers.pop("model") or "").strip()
        servers = {endpoint.get("name") for endpoint in getattr(page, "llm_servers", None) or []
                   if isinstance(endpoint, dict)}
        if value and ":" not in value and models[0] and value not in servers | {models[0]}:
            value = f"{models[0]}:{value}"  # a model typed bare: the server's (NAME:MODEL), not a server of that name
        choice = WL.choice_of(value, bool((WL.from_board(page.board) or {}).get("only"))) if value else None
        if choice and value != str(page.board.first("llm").props.get("--llm") or ""):
            page.choose_llm_server(choice)
    updates = guide.props_of(node.kind, answers)

    def change():
        # The part as it is on the board now: the model server chosen above was a step of its own, and every step
        # makes the board's parts anew - the answers were set on the part of the board before it, and lost (the
        # effort, Show thinking and "Agent without approval prompts" answered in the same guide).
        target = page.board.node(node_id)
        if target is None:
            return
        for key, value in updates.items():
            if value is None:
                target.props.pop(key, None)
            else:
                target.props[key] = value
    page.change(tr("Set up {name}", name=tr(B.KINDS[node.kind].title)), change)

