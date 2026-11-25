"""One start, two views: the Board page shows a start as a Board (parts and connections) or as Expert (every option
a field, and the whole command line as text to edit), with a switch between them.

Both views show one start - the board of the Board view (board.Board, with its Undo). StartLink keeps them the same:
what changes on the board is shown in the Expert view's fields and command line at once, and a field or a command line
typed there is put onto the board (board.with_start: the parts that are there stay where they are) as a step of the
same Undo. A command line that cannot be read changes nothing and says why below the field.

ViewSwitch is the switch itself (the window puts it above the page); CommandEdit the command line's field."""
from __future__ import annotations

import shlex

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QPlainTextEdit, QRadioButton, QWidget

from . import applog, backend, board as B
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import safe_tooltip

LOCAL = "local"
# The two views of the Start page: (the page's key in the window, its title on the switch, what it shows).
VIEWS = (("board", N_("Board"), N_("The start as parts and connections: drag parts onto the board, connect them, set "
                                  "them in their properties")),
         ("launch", N_("Expert"), N_("The same start as text: every option a field, and the whole command line to "
                                    "read, edit or paste")))
VIEW_KEYS = tuple(key for key, _, _ in VIEWS)
DEFAULT_VIEW = "board"
SWITCH_KEYS = "F6"  # switches between the two views while the Board page is shown


def view_title(key):
    return tr(next(title for view, title, _ in VIEWS if view == key))


class ViewSwitch(QWidget):
    """Board | Expert: the view of the Board page that is shown, as two radio buttons (one of two: never a button
    that looks stuck pressed, in any theme)."""
    chosen = Signal(str)  # the view's key, when the human chose another one

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("viewSwitch")
        self.setAccessibleName(tr("View of the Board page"))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key, title, what in VIEWS:
            button = QRadioButton(tr(title))
            button.setObjectName(f"viewSwitch-{key}")
            button.setToolTip(safe_tooltip(tr("{view}: {what} ({keys} switches)", view=tr(title), what=tr(what),
                                              keys=SWITCH_KEYS)))
            button.setAccessibleName(tr("{view} view", view=tr(title)))
            button.clicked.connect(lambda _=False, k=key: self.chosen.emit(k))
            self.group.addButton(button)
            layout.addWidget(button)
            self.buttons[key] = button
        self.buttons[DEFAULT_VIEW].setChecked(True)

    def view(self):
        return next((key for key, button in self.buttons.items() if button.isChecked()), DEFAULT_VIEW)

    def set_view(self, key):
        """The button of view KEY chosen (no signal: the window shows the view, this follows)."""
        if key in self.buttons and not self.buttons[key].isChecked():
            self.buttons[key].setChecked(True)


class CommandEdit(QPlainTextEdit):
    """The command line of the start as text: shown (show_text), and typed into (edited: only what the human typed,
    pasted or cut; Enter takes the line as it is and never breaks it; left: the field lost the keyboard)."""
    edited = Signal(str)
    entered = Signal()
    left = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.showing = False
        self.setTabChangesFocus(True)
        self.textChanged.connect(self.changed)

    def changed(self):
        if not self.showing:
            self.edited.emit(self.toPlainText())

    def show_text(self, text):
        """TEXT in the field, by the app (no edit of the human); the same text again changes nothing (laid out and
        scrolled to the top otherwise)."""
        if text == self.toPlainText():
            return
        self.showing = True
        try:
            self.setPlainText(text)
        finally:
            self.showing = False

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.isReadOnly():
            self.entered.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):  # noqa: N802
        super().focusOutEvent(event)
        self.left.emit()


def command_text(agent, argv, agent_args, host=LOCAL):
    """The command line of a start as the page shows it: on another host as this machine's shell starts it there."""
    where = ["hosts", "run", host, "--"] if host != LOCAL else []
    return shlex.join(["sandbox-ai", *where, agent, *argv, *(["--", *agent_args] if agent_args else [])])


class StartLink(QObject):
    """The Board view (BOARD_PAGE: board_view.BoardPage) and the Expert view (EXPERT: ui.LaunchPage) show one start:
    the board's. See the module's text."""

    def __init__(self, board_page, expert):
        super().__init__(expert)
        self.board_page, self.expert, self.sai = board_page, expert, board_page.sai
        self.showing = False  # the board is being shown in the Expert view: its fields say no edit of the human
        self.line_problem = ""  # why the command line as typed is not the start (shown below it)
        self.pending = False  # an edit of the Expert view's fields waits to be put onto the board (flush)
        # What the Board says about the start that the Expert view has no place of its own for: its note when it is a
        # warning or an error (a project file nobody trusted, what is left to fill in), and that a start asked for
        # here waits for something the Board shows (start_waits)
        self.notice, self.waiting = ("", "secondary"), ""
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.timeout.connect(self.flush)
        expert.link = self
        board_page.other_view = expert  # (a dialog of the Board veils this view too: the same start)
        board_page.noticed.connect(self.board_noticed)
        board_page.board_changed.connect(self.show_board)
        board_page.findings_changed.connect(self.show_findings)
        board_page.host_choice.chosen.connect(lambda host: self.host_chosen(host, expert.host_choice))
        expert.host_choice.chosen.connect(lambda host: self.host_chosen(host, board_page.host_choice))
        expert.form.changed.connect(self.fields_edited)
        expert.agent_args.textChanged.connect(lambda _text: self.fields_edited())
        expert.agent.currentTextChanged.connect(self.agent_chosen)
        expert.preview.edited.connect(self.line_edited)
        expert.preview.entered.connect(self.line_left)
        expert.preview.left.connect(self.line_left)
        expert.linked()
        self.host_chosen(board_page.host(), expert.host_choice)
        self.show_board()

    @property
    def board(self):
        return self.board_page.board

    # -- the board, shown in the Expert view --

    def show_board(self):
        """The Expert view shows the board's start: the agent, every option's field, the agent's arguments and the
        command line. A field that says the same already is left as it is (the cursor stays where the human types);
        so is a command line being typed that means the same start."""
        if self.showing:
            return
        if self.pending and self.flush():
            # A field typed into a moment ago, not on the board yet, and the board is shown again for another reason
            # (an answer that came late): the field first - else it was put back to what the board had. The board
            # that took it was shown by that very change.
            return
        expert = self.expert
        self.waiting = ""  # (another start now: what the one before waited for is not said of this one)
        try:
            agent, values, agent_args = B.start_values(self.sai, self.board)
        except Exception as exc:  # noqa: BLE001 - a board that cannot be read as a start: said, the fields stay
            applog.log("WARNING", "start", f"the board's start could not be shown as text: {applog.error_text(exc)}")
            return
        self.showing = True
        try:
            if expert.agent.currentText() != agent:
                if expert.agent.findText(agent) < 0:  # an agent this sandbox-ai does not have: shown, not dropped
                    expert.agent.addItem(agent)
                expert.agent.setCurrentText(agent)
            options = expert.options
            if backend.argv_from_values(options, expert.form.values()) != backend.argv_from_values(options, values):
                expert.form.set_values(values)
            if self.typed_arguments() != agent_args:
                expert.agent_args.setText(shlex.join(agent_args))
        finally:
            self.showing = False
        self.show_line()
        self.show_findings()

    def typed_arguments(self):
        """The agent's arguments as the field has them, or None while they cannot be read (a quote not closed)."""
        try:
            return shlex.split(self.expert.agent_args.text())
        except ValueError:
            return None

    def command(self):
        """The board's command line, as the page shows and copies it."""
        agent, argv, agent_args = B.start_argv(self.sai, self.board)
        return command_text(agent, argv, agent_args, self.board_page.host())

    def show_line(self, force=False):
        """The command line of the board in its field - unless the human is typing there and the text means this very
        start (written another way: short options, another order), or cannot be read yet (FORCE: all the same)."""
        edit = self.expert.preview
        if not force and edit.hasFocus() and (self.line_problem or self.same_start(edit.toPlainText())):
            return
        self.line_problem = ""
        edit.show_text(self.command())

    def same_start(self, text):
        try:
            host, agent, argv, agent_args = B.parse_command(self.sai, text, known=self.board.extra)
        except ValueError:
            return False
        if (host or LOCAL) != self.board_page.host():
            return False
        try:
            wanted = B.with_start(self.sai, self.board, agent, argv, agent_args)
        except Exception as _exc:  # noqa: BLE001 - not this start, then
            applog.swallowed("start_link", "same_start: a line the board cannot take is not its start", _exc)
            return False
        return wanted.state() == self.board.state()

    # -- the Expert view's edits, put onto the board --

    def fields_edited(self):
        """A field of the Expert view changed (the human's edit, or what the page itself set: a profile loaded, a
        model chosen): onto the board in a moment - once, however many fields one action set."""
        if self.showing:
            return
        self.pending = self.shown_board()
        self.timer.start()

    def shown_board(self):
        """Which board the Board view shows (its Undo and its project): an edit of the fields is put onto the board it
        was made for, never onto another one shown meanwhile (another project's tab)."""
        return id(self.board_page.undo), self.board_page.directory

    def flush(self, agent=None):
        """What the Expert view's fields say, on the board now (a Start, a dry run, another view and every read of the
        board take what was typed a moment ago). Arguments that cannot be read (a quote not closed) change nothing.
        AGENT: the agent the fields are for, when the chooser shows another one already (agent_chosen)."""
        self.timer.stop()
        pending, self.pending = self.pending, False
        if not pending or self.showing:
            return False
        if pending != self.shown_board():
            applog.log("INFO", "start", "an edit of the Expert view's fields is dropped: the Board shows another "
                                        "board meanwhile")
            self.show_board()
            return False
        expert = self.expert
        agent_args = self.typed_arguments()
        if agent_args is None:
            self.line_problem = tr("The agent's arguments cannot be read: a quotation mark is not closed")
            self.show_findings()
            return False
        argv = backend.argv_from_values(expert.options, expert.form.values())
        # (what the board holds that no field shows - a token of another version - stays)
        _, current, was_args = B.start_argv(self.sai, self.board)
        canonical = backend.canonical_of(self.sai)
        before, unknown = backend.values_from_argv(expert.options, current, canonical)
        after, _ = backend.values_from_argv(expert.options, argv, canonical)
        # Which fields changed: edits of the same ones in a row (a value typed letter by letter) are one step of Undo.
        names = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        source = "fields:" + " ".join(names + (["--"] if agent_args != was_args else []))
        self.line_problem = ""
        try:
            changed = self.board_page.take_start(agent or expert.agent.currentText(), argv + unknown, agent_args,
                                                 source)
        except Exception as exc:  # noqa: BLE001 - said below the line; the board stays
            applog.log("WARNING", "start", f"the fields could not be put onto the board: {applog.error_text(exc)}")
            self.line_problem = printable_text(exc)
            changed = False
        if not changed:
            self.show_line()
            self.show_findings()
        return changed

    def agent_chosen(self, agent):
        """Another agent chosen in the Expert view: as on the board, what only the agent before took goes (its own
        options, an effort or a model alias the other does not know) - one step of Undo."""
        if self.showing:
            return
        before = self.board.first("agent")
        # (the fields typed into a moment ago are the agent's before: on the board first, as its)
        self.flush(agent=before.props.get("agent") if before is not None else None)
        node = self.board.first("agent")
        if node is None or node.props.get("agent") == agent:
            return
        self.board_page.change(tr("Change {name}", name=tr("Agent")), lambda: B.switch_agent(node.props, agent))

    def line_edited(self, text):
        """The command line was typed into: read whole (B.parse_command) and put onto the board, or - a text that
        cannot be read - nothing changes and the reason is shown below it."""
        if self.showing:
            return
        self.timer.stop()
        self.pending = False
        try:
            host, agent, argv, agent_args = B.parse_command(self.sai, text, known=self.board.extra)
            host = host or LOCAL
            if host != self.board_page.host() and not self.known_host(host):
                raise ValueError(tr("{host} is not a host of sandbox-ai hosts", host=printable(host)))
            wanted = B.with_start(self.sai, self.board, agent, argv, agent_args)
        except ValueError as exc:
            self.line_problem = printable_text(exc)
            self.show_findings()
            return False
        self.line_problem = ""
        if host != self.board_page.host():
            self.board_page.host_choice.choose(host)
        if wanted.state() != self.board.state():
            self.board_page.take_start(agent, argv, agent_args, "line")
        else:
            self.show_findings()
        return True

    def line_left(self):
        """Enter in the command line, or the keyboard left it: a line that was read is shown as the board has it now
        (long options, the parser's order); one that cannot be read stays, with its reason, for the human to mend."""
        if not self.line_problem:
            self.show_line(force=True)

    def known_host(self, host):
        combo = self.board_page.host_choice
        return any(combo.itemData(index) == host for index in range(combo.count()))

    def host_chosen(self, host, other):
        """One view's host chosen: the other view's is the same (one start, one host)."""
        if other.current() != host:
            other.choose(host)
        if not self.showing:
            self.show_line()

    # -- what is wrong with the start, below the command line --

    def board_noticed(self, text, role):
        self.notice = (text, role)
        if not self.showing:
            self.show_findings()

    def start(self, embedded=False, session=None):
        """Start from the Expert view: the board's start, of the board as the fields say it now. A start that does not
        go on at once - what it needs is checked, made or missing, shown on the Board - is said below the line."""
        self.flush()
        page = self.board_page
        started = page.start(embedded=embedded, session=session)
        self.waiting = ""
        if not started:
            from .prerequisites import busy_now
            panel = getattr(page, "preparing_panel", None)
            if (panel is not None and not panel.isHidden()) or page.__dict__.get("after_check") is not None \
                    or busy_now(page):
                self.waiting = tr("The start does not go on yet: what it needs is checked, prepared or missing. The "
                                  "Board shows it, with what helps.")
        self.show_findings()
        return started

    def show_findings(self):
        """Below the command line: why the line as typed is not taken, else the board's errors (what the launcher or
        the policy refuses), else the launcher's own verdict of the last check (the board's dry run)."""
        page = self.board_page
        if self.line_problem:
            self.expert.show_line_note(self.line_problem, "notice-error")
            return
        # (what the board's last look at itself found - BoardPage.refresh - and its launcher's last check of this very
        # board: nothing is worked out again here, at every change of the board)
        launcher = getattr(page, "launcher_found", None) or []
        if launcher and getattr(page, "launcher_state", None) != self.board.state():
            launcher = []
        errors = [problem.text for problem in [*getattr(page, "problems", []), *launcher] if problem.level == "error"]
        if errors:
            more = tr(" (and {count} more: the Board view lists them)", count=len(errors) - 2) if len(errors) > 2 \
                else ""
            self.expert.show_line_note(" · ".join("✘ " + text for text in errors[:2]) + more, "notice-error",
                                       whole="\n".join(errors), board=True)
            return
        if self.waiting:
            self.expert.show_line_note(self.waiting, "notice-warn", board=True)
            return
        text, role = self.notice
        if text and role in ("notice-warn", "notice-error"):
            self.expert.show_line_note(text, role, board=True)
            return
        verdict = page.verdict
        said, whole = verdict.text(), getattr(verdict, "whole", None) or verdict.text()
        if B.team_node(self.board) is not None:
            # (the line is the start of one sandbox; with a Team part the board's Start starts one per member)
            team = tr("With its Team part, Start starts a team: this line is each member's sandbox.")
            said, whole = f"{said} {team}".strip(), f"{whole}\n{team}".strip()
        role = verdict.property("role") or "secondary"
        self.expert.show_line_note(said, role, whole=whole, board=role in ("notice-error", "notice-warn"))
