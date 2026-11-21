"""What preparing a start does right now, and how far it is (user report 2026-11-14: the log said only "Was der Start
braucht, wird noch vorbereitet…" - "da sollte stehen was noch vorbereitet wird und Progress"): the items it makes in
their order (an identity, an image variant from its recipe, a build, a Tor profile, an MCP server), each waiting,
running, done or failed, with the stage and step of a build as sandbox-ai prints them (its progress.py: "building
the image localhost/qwen-sandbox:latest (stage 2/2, step 9/14): RUN ..."), and one line that says it all: "Preparing
(2 of 4): building the image variant tor (step 9/14, 1:12) — next: MCP server exiftool, Tor profile tor-net".

The slow items run in the terminal tab Preparing the start (prerequisites.CHAIN), which writes what it sees of their
output to a status file (read() here); the quick ones in the background (start() and finish() from that thread).
PreparingPanel shows it on the Board: the line (a click opens where it runs), a bar for the whole and one per item,
and, when a start is held because something is missing, what is missing with the button that makes it."""
from __future__ import annotations

import json
import re
import time

from .files import printable
from .i18n import tr

# The progress of a build as sandbox-ai's progress.py (Output) says it: "(step 9/14)" or "(stage 2/2, step 9/14)".
STEP = re.compile(r"\((?:stage (\d+)/(\d+), )?step (\d+)/(\d+)\)")
# "sandbox-ai build: ", "agent-sandbox: ", "sandbox-ai mcp: " - the prefix of a progress line.
PREFIX = re.compile(r"^[A-Za-z][\w .-]{0,40}: ")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def doing(kind, name):
    """What the item KIND NAME is while it runs."""
    return {"identity": tr("making the identity {name}", name=name),
            "recipe": tr("making the image variant {name} from its recipe", name=name),
            "build": tr("building the image variant {name}", name=name),
            "image": tr("building the image of {name}", name=name),
            "vpn": tr("making the Tor profile {name}", name=name),
            "mcp": tr("adding the MCP server {name}", name=name),
            "service": tr("building the service image {name}", name=name)}.get(kind, name)


def thing(kind, name):
    """The item KIND NAME as a thing (the list of what comes next, what is missing)."""
    return {"identity": tr("identity {name}", name=name),
            "recipe": tr("image variant {name}", name=name),
            "build": tr("image variant {name}", name=name),
            "image": tr("the image of {name}", name=name),
            "vpn": tr("Tor profile {name}", name=name),
            "mcp": tr("MCP server {name}", name=name),
            "service": tr("service image {name}", name=name)}.get(kind, name)


def clock(seconds):
    """SECONDS as m:ss (h:mm:ss from an hour on)."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def parse(line):
    """{"stage": (a, b) or None, "step": (c, d), "text": what the step does} of a progress line LINE of sandbox-ai
    (a build's step), else None."""
    text = ANSI.sub("", str(line)).strip()
    found = STEP.search(text)
    if not found:
        return None
    stage = (int(found[1]), int(found[2])) if found[1] else None
    rest = text[found.end():].lstrip(": ").rstrip("…").strip()
    return {"stage": stage, "step": (int(found[3]), int(found[4])), "text": rest}


def fraction(stage, step):
    """How far a build is at STEP (c, d) of STAGE (a, b) (or none): the steps before the one that runs, done."""
    if not step or step[1] <= 0:
        return None
    within = (max(1, step[0]) - 1) / step[1]
    if stage and stage[1] > 0:
        return min(1.0, ((max(1, stage[0]) - 1) + within) / stage[1])
    return min(1.0, within)


class Item:
    """One thing made for a start (KIND: identity, recipe, build, image, vpn, mcp), IN_TAB when it runs in the
    terminal tab."""

    def __init__(self, kind, name, in_tab=False):
        self.kind, self.name, self.in_tab = kind, str(name), in_tab
        self.state = "waiting"  # running, done, failed
        self.begun = self.took = self.status = None
        self.stage = self.step = None
        self.detail = ""  # the last line of its output (what it does now)

    def fraction(self):
        if self.state in ("done", "failed"):
            return 1.0
        if self.state != "running":
            return 0.0
        return fraction(self.stage, self.step)

    def progress(self, now):
        """" (stage 2/2, step 9/14, 1:12)" of the running item (its time when nothing more is known)."""
        parts = []
        if self.stage and self.stage[1] > 1:
            parts.append(tr("stage {stage}/{stages}", stage=self.stage[0], stages=self.stage[1]))
        if self.step:
            parts.append(tr("step {step}/{steps}", step=self.step[0], steps=self.step[1]))
        if self.begun is not None:
            parts.append(clock(now - self.begun))
        return f" ({', '.join(parts)})" if parts else ""


class Preparation:
    """The items made for one start, in the order they are made (see the module's docstring). CLOCK: monotonic
    seconds (time of the items); TAB_ARGV: the command of the terminal tab the slow ones run in, STATUS: its status
    file (None: no tab)."""

    def __init__(self, items, clock_=time.monotonic):
        self.items, self.clock = list(items), clock_
        self.tab_argv, self.status = None, None
        self.begun = clock_()

    def __bool__(self):
        return bool(self.items)

    def running(self):
        return next((item for item in self.items if item.state == "running"), None)

    def failed(self):
        return next((item for item in self.items if item.state == "failed"), None)

    def finished(self):
        return all(item.state in ("done", "failed") for item in self.items) or self.failed() is not None

    def current(self):
        """The item that runs, else the next one to run (else None)."""
        return self.running() or next((item for item in self.items if item.state == "waiting"), None)

    def overall(self):
        """How far the whole is (0 to 1): the items done, and the part of the one that runs that is known."""
        if not self.items:
            return 1.0
        return sum(item.fraction() or 0.0 for item in self.items) / len(self.items)

    def start(self, index, now=None):
        item = self.items[index]
        item.state, item.begun = "running", self.clock() if now is None else now

    def finish(self, index, ok, status=None, now=None):
        item = self.items[index]
        if item.begun is None:
            item.begun = self.clock() if now is None else now
        item.state, item.status = ("done" if ok else "failed"), status
        item.took = (self.clock() if now is None else now) - item.begun

    def line(self, index, text):
        """A line of the output of the item at INDEX: its stage and step, when it says them; what it does now."""
        item = self.items[index]
        text = PREFIX.sub("", ANSI.sub("", str(text)).strip())
        if not text:
            return
        found = parse(text)
        if found:
            item.stage, item.step = found["stage"], found["step"]
        item.detail = text[-300:]

    def tab_items(self):
        return [index for index, item in enumerate(self.items) if item.in_tab]

    def merge(self, data, now=None):
        """The terminal tab's status (CHAIN's file: {"steps": [{"state", "begun", "took", "status", "mark",
        "last"}]}) into the items that run there, in their order."""
        now = self.clock() if now is None else now
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            return
        for index, step in zip(self.tab_items(), steps):
            if not isinstance(step, dict):
                continue
            item, state = self.items[index], step.get("state")
            if state in ("running", "done", "failed") and item.state == "waiting":
                begun = step.get("begun")  # when it began (the time of day: the tab is another process)
                item.begun = now - max(0.0, time.time() - begun) if isinstance(begun, (int, float)) else now
            if state in ("running", "done", "failed"):
                item.state = state
            if isinstance(step.get("took"), (int, float)):
                item.took = step["took"]
            if isinstance(step.get("status"), int):
                item.status = step["status"]
            for key in ("mark", "last"):
                if isinstance(step.get(key), str) and step[key]:
                    self.line(index, step[key])

    def read(self):
        """The terminal tab's status file read again (nothing when it is not there yet or half written)."""
        if not self.status:
            return False
        try:
            with open(self.status, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError, RecursionError):  # (RecursionError: nested deeper than Python reads)
            return False
        self.merge(data)
        return True

    def number(self):
        """The 1-based number of the current item (the last one when all are done)."""
        current = self.failed() or self.current()
        return self.items.index(current) + 1 if current is not None else len(self.items)

    def summary(self, now=None, timed=True, still=False):
        """The line that says what is prepared now and how far (TIMED: with the step and the time of the item that
        runs; without, for the log: once per change of item; STILL: the answer to a Start pressed meanwhile)."""
        now = self.clock() if now is None else now
        total, failed = len(self.items), self.failed()
        if failed is not None:
            status = tr(" (exit status {status})", status=failed.status) \
                if isinstance(failed.status, int) and failed.status else ""
            return tr("Not prepared ({number} of {total}): {doing} failed{status}.", number=self.number(),
                      total=total, doing=printable(doing(failed.kind, failed.name)), status=status)
        current = self.current()
        if current is None:
            return tr("Prepared: {list}.", list=printable(", ".join(thing(i.kind, i.name) for i in self.items)))
        values = {"number": self.number(), "total": total, "doing": printable(doing(current.kind, current.name)),
                  "progress": current.progress(now) if timed and current.state == "running" else ""}
        text = tr("Still preparing ({number} of {total}): {doing}{progress}", **values) if still else \
            tr("Preparing ({number} of {total}): {doing}{progress}", **values)
        later = [item for item in self.items[self.items.index(current) + 1:] if item.state == "waiting"]
        if later:
            text += tr(" — next: {list}", list=printable(", ".join(thing(i.kind, i.name) for i in later)))
        return text

    def changed_key(self):
        """What changes when another item runs (the log says the summary once per such change)."""
        return tuple(item.state for item in self.items)


def chain_status_argv(argv, status):
    """The terminal tab's command ARGV (CHAIN's: [python, -I, -c, CHAIN, STEPS]) told to write its status to
    STATUS: the steps stay its last argument."""
    return [*argv[:-1], str(status), argv[-1]]


# -- the panel on the Board --

def make_panel():
    """A PreparingPanel (Qt imported only here: the model above is Qt-free)."""
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                                   QToolButton, QVBoxLayout, QWidget)
    from .widgets import safe_tooltip

    class ClickLabel(QLabel):
        clicked = Signal()

        def mouseReleaseEvent(self, event):  # noqa: N802
            if event.button() == Qt.MouseButton.LeftButton and not self.hasSelectedText():
                self.clicked.emit()
            super().mouseReleaseEvent(event)

    class PreparingPanel(QFrame):
        """What the start's preparation does (show_preparation) or what keeps a start from going on
        (show_missing): above the Board's verdict, hidden otherwise. clicked: its line was clicked (open where it
        runs)."""
        clicked = Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("preparing")
            self.setFrameShape(QFrame.Shape.StyledPanel)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(6, 4, 6, 4)
            layout.setSpacing(3)
            top = QHBoxLayout()
            self.line = ClickLabel()
            self.line.setTextFormat(Qt.TextFormat.PlainText)  # names come from boards and presets
            self.line.setWordWrap(True)
            self.line.setCursor(Qt.CursorShape.PointingHandCursor)
            self.line.setAccessibleName(tr("Preparing the start"))
            self.line.clicked.connect(self.clicked.emit)
            top.addWidget(self.line, 1)
            self.close_button = QToolButton()
            self.close_button.setText("×")
            self.close_button.setAccessibleName(tr("Close"))
            self.close_button.setToolTip(tr("Hide this note"))
            self.close_button.clicked.connect(lambda _=False: self.dismiss())
            top.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(top)
            self.overall = QProgressBar()
            self.overall.setRange(0, 1000)
            self.overall.setTextVisible(True)
            self.overall.setAccessibleName(tr("Preparing the start: all of it"))
            layout.addWidget(self.overall)
            self.rows_widget = QWidget()
            self.rows = QGridLayout(self.rows_widget)
            self.rows.setContentsMargins(0, 0, 0, 0)
            self.rows.setHorizontalSpacing(8)
            self.rows.setVerticalSpacing(2)
            layout.addWidget(self.rows_widget)
            self.labels, self.bars, self.buttons, self.shown_items = [], [], [], None
            self.mode, self.dismissed = None, False
            self.hide()

        def dismiss(self):
            """Closed by its x: hidden until the next start or Start pressed again."""
            self.dismissed = True
            self.hide()

        def clear_rows(self):
            while self.rows.count():
                widget = self.rows.takeAt(0).widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.labels, self.bars, self.buttons, self.shown_items = [], [], [], None

        def show_preparation(self, preparation, now=None):
            """PREPARATION's line, its bar and one bar per item."""
            now = preparation.clock() if now is None else now
            if self.mode != "preparing" or self.shown_items != [id(i) for i in preparation.items]:
                self.clear_rows()
                self.mode = "preparing"
                for row, item in enumerate(preparation.items):
                    label = QLabel()
                    label.setTextFormat(Qt.TextFormat.PlainText)
                    bar = QProgressBar()
                    bar.setTextVisible(True)
                    bar.setAccessibleName(thing(item.kind, item.name))
                    self.rows.addWidget(label, row, 0)
                    self.rows.addWidget(bar, row, 1)
                    self.labels.append(label)
                    self.bars.append(bar)
                self.rows.setColumnStretch(1, 1)
                self.shown_items = [id(i) for i in preparation.items]
            failed = preparation.failed() is not None
            self.line.setText(preparation.summary(now))
            self.line.setProperty("role", "notice-error" if failed else "")
            self.line.setToolTip(tr("Click: open where it runs (its terminal tab, else the log window)"))
            self.overall.show()
            self.overall.setValue(int(1000 * preparation.overall()))
            self.overall.setFormat(f"{preparation.number()}/{len(preparation.items)}  %p%")
            for item, label, bar in zip(preparation.items, self.labels, self.bars):
                state = {"waiting": tr("waiting"), "running": tr("running"), "done": tr("done"),
                         "failed": tr("failed")}[item.state]
                label.setText(f"{printable(thing(item.kind, item.name))}: {state}")
                share = item.fraction()
                if share is None:  # running, nothing known of how far: a busy bar
                    bar.setRange(0, 0)
                else:
                    bar.setRange(0, 1000)
                    bar.setValue(int(1000 * share))
                bar.setFormat(item.progress(now).strip(" ()") if item.state == "running" else
                              (clock(item.took) if item.took is not None and item.state != "waiting" else ""))
                bar.setToolTip(safe_tooltip(printable(item.detail)))  # (a line of a build's output)
            self.style().unpolish(self.line)
            self.style().polish(self.line)
            self.show()

        def show_missing(self, text, rows, role="notice-warn"):
            """TEXT (why nothing started) and ROWS [(what is missing, [(button text, function)])]: each with the
            buttons that make it."""
            self.clear_rows()
            self.mode = "missing"
            self.line.setText(text)
            self.line.setProperty("role", role)
            self.line.setToolTip("")
            self.overall.hide()
            for row, (what, actions) in enumerate(rows):
                label = QLabel(what)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                self.rows.addWidget(label, row, 0)
                self.labels.append(label)
                for column, (title, function) in enumerate(actions, 1):
                    button = QPushButton(title)
                    button.clicked.connect(lambda _=False, f=function: f())
                    self.rows.addWidget(button, row, column)
                    self.buttons.append(button)
            self.rows.setColumnStretch(1, 0)
            self.rows.setColumnStretch(0, 1)
            self.style().unpolish(self.line)
            self.style().polish(self.line)
            self.show()

        def done(self):
            """Nothing prepared or missing any more."""
            self.clear_rows()
            self.mode = None
            self.hide()

    return PreparingPanel()
