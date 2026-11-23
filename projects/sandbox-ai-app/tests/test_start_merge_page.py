"""The Start page: Board and Expert are two views of one start (through the real window, offscreen).

One entry in the navigation, a switch above the page (Board by default, the view chosen last kept), and whatever is
changed in one view is in the other at once: a part or a setting on the Board in the Expert view's fields and command
line; a field or a command line typed there on the Board. A line that cannot be read changes nothing and says why.
Undo and Redo are one history for both, and a start runs the same command whichever view starts it."""
import json
import os
import shlex

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from sandbox_ai_app import backend, board as B, i18n, start_link, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")  # (the launcher's check without a workspace: not the app's own tree)
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def make_window(monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    started = []
    w.sai.in_terminal = lambda argv, title="": ["term", *argv]
    monkeypatch.setattr("sandbox_ai_app.ui.QProcess.startDetached",
                        lambda program, args: started.append([program, *args]) or True)
    w.started = started
    return w


def end_window(w):
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.fixture
def window(qapp, env, monkeypatch):
    w = make_window(monkeypatch)
    yield w
    end_window(w)


def start_of(window):
    """(agent, options, the agent's arguments) of the board both views show."""
    return B.start_argv(window.sai, window.board.board)


def ordered(window, argv):
    """ARGV in the order of the launcher's parser, long options (the form of every command line the page shows)."""
    options = window.sai.options()
    values, unknown = backend.values_from_argv(options, list(argv), backend.canonical_of(window.sai))
    parts = {name: value for name, value in values.items()
             if name in B.OWNER or name in ("--model", "--effort", "--show-thinking")}
    other = {name: value for name, value in values.items() if name not in parts}  # (Other options: after the parts')
    return backend.argv_from_values(options, parts) + backend.argv_from_values(options, other) + unknown


def line(window, agent, argv, agent_args=(), host=None):
    where = ["hosts", "run", host, "--"] if host else []
    return shlex.join(["sandbox-ai", *where, agent, *ordered(window, argv), *(["--", *agent_args] if agent_args else [])])


def typed(window, text):
    """TEXT typed into the Expert view's command line (as the human's edit: its signal)."""
    edit = window.launch.preview
    edit.setPlainText(text)
    return edit


def flush(window, qapp):
    qapp.processEvents()
    window.start_link.flush()
    qapp.processEvents()


# -- one page, one entry, a switch --

def test_the_navigation_has_one_entry_and_the_page_opens_as_the_board(window, qapp):
    """A new installation: Start is one entry (no Board entry beside it), shown as the Board, with the switch above
    the page saying so."""
    titles = [window.nav.item(row).text() for row in range(window.nav.count())]
    assert titles[:3] == ["PROJECT", "Start", "RUNNING"] and "Board" not in titles
    assert window.current_page() == "board" and window.start_view == "board"
    window.show()
    qapp.processEvents()
    switch = window.view_switch
    assert switch.isVisible() and switch.view() == "board"
    assert [switch.buttons[key].text() for key in ("board", "launch")] == ["Board", "Expert"]
    assert window.nav.currentRow() == window.nav_rows["launch"] == window.nav_rows["board"]
    assert window.board.isVisible() and not window.launch.isVisible()


def test_the_switch_shows_the_other_view_and_only_on_the_start_page(window, qapp):
    window.show()
    qapp.processEvents()
    window.view_switch.buttons["launch"].click()
    qapp.processEvents()
    assert window.current_page() == "launch" and window.launch.isVisible() and not window.board.isVisible()
    assert window.view_switch.view() == "launch" and window.nav.currentRow() == window.nav_rows["launch"]
    assert not window.docks["parts"].isVisible(), "the Board's panels belong to the Board view"
    window.view_switch.buttons["board"].click()
    qapp.processEvents()
    assert window.current_page() == "board" and window.docks["parts"].isVisible()
    window.show_page("sandboxes")
    qapp.processEvents()
    assert not window.view_switch.isVisible()
    assert not window.actions["switch-view"].isEnabled() and not window.actions["switch-view"].shortcuts()
    window.show_page("board")
    qapp.processEvents()
    assert window.view_switch.isVisible() and window.actions["switch-view"].isEnabled()


def test_the_key_of_the_switch_and_the_old_shortcuts_land_on_the_page(window, qapp):
    """F6 switches the view on the page; Ctrl+1 is Start in the view chosen last, Ctrl+2 (the Board's shortcut) the
    page in its Board view; the other pages keep their numbers."""
    window.show()
    qapp.processEvents()
    switch = window.actions["switch-view"]
    assert [key.toString() for key in switch.shortcuts()] == [start_link.SWITCH_KEYS]
    switch.trigger()
    assert window.current_page() == "launch"
    switch.trigger()
    assert window.current_page() == "board"
    switch.trigger()
    window.show_page("sandboxes")
    assert [key.toString() for key in window.actions["page:launch"].shortcuts()] == ["Ctrl+1"]
    assert [key.toString() for key in window.actions["page:board"].shortcuts()] == ["Ctrl+2"]
    assert [key.toString() for key in window.actions["page:sandboxes"].shortcuts()] == ["Ctrl+3"]
    window.actions["page:launch"].trigger()
    assert window.current_page() == "launch", "Start: the view chosen last (Expert)"
    window.show_page("sandboxes")
    window.actions["page:board"].trigger()
    assert window.current_page() == "board"
    window.show_page("sandboxes")
    window.actions["page:launch"].trigger()
    assert window.current_page() == "board", "... which is the Board now"
    assert "Ctrl+1" in window.nav_item("launch").toolTip() and window.nav_item("board") is window.nav_item("launch")
    # Back and Forward go through the views as through pages
    window.view_switch.buttons["launch"].click()
    assert window.go_back() and window.current_page() == "board"


def test_the_view_chosen_is_kept_for_the_next_start(qapp, env, monkeypatch):
    first = make_window(monkeypatch)
    try:
        assert first.current_page() == "board", "a new installation: the Board"
        first.view_switch.buttons["launch"].click()
        assert first.current_page() == "launch"
        first.show_page("sandboxes")  # (left on another page: the view is kept all the same)
        first.state.flush()
        kept = json.loads(first.state.path.read_text())
        assert kept["start_view"] == "launch"
    finally:
        end_window(first)
    second = make_window(monkeypatch)
    try:
        assert second.start_view == "launch"
        second.show_start()
        assert second.current_page() == "launch"
        second.show_page("board")  # a link or a tour shows the other view: no choice of the human
        assert second.current_page() == "board" and second.start_view == "launch"
        second.show_page("sandboxes")
        second.show_start()
        assert second.current_page() == "launch"
        second.actions["switch-view"].trigger()  # the human's: the Board from now on
        assert second.current_page() == "board" and second.start_view == "board"
        second.state.flush()
    finally:
        end_window(second)
    third = make_window(monkeypatch)
    try:
        assert third.current_page() == "board" and third.start_view == "board"
    finally:
        end_window(third)


@pytest.mark.parametrize("page", ["launch", "board"])
def test_a_state_of_before_the_merge_opens_the_page(qapp, env, monkeypatch, page):
    """The app was left on the Start page or on the Board by a version that had both as pages of their own: it opens
    on the one Start page (as the Board: no view was chosen yet); and what shows either by its key - a link, a tour,
    the Go menu - lands on that page, in that view."""
    from sandbox_ai_app import state
    state.state_file().parent.mkdir(parents=True, exist_ok=True)
    state.state_file().write_text(json.dumps({"page": page}))
    w = make_window(monkeypatch)
    try:
        w.show()
        qapp.processEvents()
        assert w.current_page() == "board" and w.view_switch.isVisible()
        assert w.nav.currentRow() == w.nav_rows["launch"]
        for key in ("launch", "board", "launch"):
            w.show_page("sandboxes")
            w.show_page(key)
            qapp.processEvents()
            assert w.current_page() == key and w.nav.currentRow() == w.nav_rows["launch"]
            assert w.view_switch.isVisible() and w.view_switch.view() == key
            assert w.start_view == "board", "shown by its key, not chosen"
        assert w.section_of("board") == w.section_of("launch") == "Project"
    finally:
        end_window(w)


# -- one start: what one view changes, the other shows --

def test_a_change_on_the_board_is_in_the_expert_view_at_once(window, qapp):
    board, expert = window.board, window.launch
    board.add_node("limits")
    limits = board.board.first("limits")
    board.set_prop(limits.id, "--memory", "4g")
    assert expert.form.widgets["--memory"].text() == "4g"
    assert expert.preview.toPlainText() == "sandbox-ai claude --memory 4g"
    network = board.board.first("network")
    board.set_prop(network.id, "--offline", True)
    board.add_node("workspace", path="/tmp/proj")
    agent = board.board.first("agent")
    board.change("agent", lambda: B.switch_agent(agent.props, "codex"))
    board.set_prop(board.board.first("agent").id, "arguments", "exec 'fix it'")
    assert expert.form.widgets["--offline"].isChecked() and expert.agent.currentText() == "codex"
    assert expert.form.widgets["--workspace"].value() == ["/tmp/proj"]
    assert shlex.split(expert.agent_args.text()) == ["exec", "fix it"]
    assert expert.preview.toPlainText() == line(window, "codex", ["--workspace", "/tmp/proj", "--offline", "--memory",
                                                                 "4g"], ["exec", "fix it"])
    board.remove_node(board.board.first("limits").id)
    assert expert.form.widgets["--memory"].text() == "" and "--memory" not in expert.preview.toPlainText()


def test_a_field_of_the_expert_view_is_on_the_board_at_once(window, qapp):
    board, expert = window.board, window.launch
    window.show_page("launch")
    places = {node.id: (node.x, node.y) for node in board.board.nodes}
    expert.form.widgets["--memory"].setText("4g")
    expert.form.widgets["--offline"].setChecked(True)
    expert.form.widgets["--allow-host"].set_value(["example.org"])
    expert.form.widgets["--mode"].setCurrentText("high")
    expert.agent_args.setText("--verbose")
    flush(window, qapp)
    assert board.board.first("limits").props == {"--memory": "4g"}
    assert board.board.first("network").props == {"--offline": True, "--allow-host": ["example.org"]}
    assert board.board.first("other").props == {"tokens": ["--mode", "high"]} and board.board.extra == ["--mode", "high"]
    assert board.board.first("agent").props["arguments"] == "--verbose"
    assert {node.id: (node.x, node.y) for node in board.board.nodes if node.id in places} == places
    assert set(board.items) == {node.id for node in board.board.nodes}, "the scene shows the parts made for it"
    assert expert.preview.toPlainText() == line(window, "claude", ["--offline", "--allow-host", "example.org",
                                                                  "--memory", "4g", "--mode", "high"], ["--verbose"])
    expert.agent.setCurrentText("codex")
    assert board.board.first("agent").props["agent"] == "codex"
    expert.form.widgets["--memory"].setText("")
    flush(window, qapp)
    assert board.board.first("limits").props == {}, "the part stays where it is, with nothing set"


def test_a_command_line_typed_in_is_on_the_board_and_in_the_fields(window, qapp):
    board, expert = window.board, window.launch
    window.show_page("launch")
    typed(window, "sandbox-ai codex -o --memory 4g -w '/tmp/my proj' --name night -- exec 'fix it'")
    assert start_of(window) == ("codex", ordered(window, ["--workspace", "/tmp/my proj", "--offline", "--memory", "4g",
                                                          "--name", "night"]), ["exec", "fix it"])
    assert [n.props["path"] for n in board.board.of_kind("workspace")] == ["/tmp/my proj"]
    assert board.board.first("limits").props == {"--memory": "4g"} and board.board.extra == ["--name", "night"]
    assert expert.agent.currentText() == "codex" and expert.form.widgets["--offline"].isChecked()
    assert expert.form.widgets["--memory"].text() == "4g" and expert.form.widgets["--name"].text() == "night"
    assert shlex.split(expert.agent_args.text()) == ["exec", "fix it"] and not expert.line_note.text().startswith("The command")
    expert.preview.left.emit()  # the keyboard leaves the line: shown as the start has it (long options, in order)
    assert expert.preview.toPlainText() == line(window, "codex", ["--workspace", "/tmp/my proj", "--offline",
                                                                 "--memory", "4g", "--name", "night"],
                                                ["exec", "fix it"])


@pytest.mark.parametrize("line, said", [
    ("sandbox-ai claude --memory '4g", "quotation mark"),
    ("sandbox-ai claude --no-such-option", "--no-such-option"),
    ("sandbox-ai claude --memory", "--memory needs a value"),
    ("sandbox-ai nobody --offline", "nobody is not an agent"),
    ("sandbox-ai hosts run nowhere -- claude", "nowhere is not a host"),
    ("", "Name the agent first"),
])
def test_a_line_that_cannot_be_read_changes_nothing_and_says_why(window, qapp, line, said):
    board, expert = window.board, window.launch
    window.show()
    window.show_page("launch")
    typed(window, "sandbox-ai claude --offline --memory 2g")
    before, steps = board.board.state(), board.undo.count()
    typed(window, line)
    qapp.processEvents()
    assert board.board.state() == before and board.undo.count() == steps, "nothing of it is applied"
    assert said in expert.line_note.text() and expert.line_note.isVisible()
    assert expert.line_note.property("role") == "notice-error"
    assert not expert.board_button.isVisible(), "about the line as typed: the Board has nothing more to say of it"
    assert expert.form.widgets["--memory"].text() == "2g" and expert.form.widgets["--offline"].isChecked()
    assert expert.preview.toPlainText() == line, "what was typed stays, to be mended"
    expert.preview.left.emit()
    assert expert.preview.toPlainText() == line
    typed(window, "sandbox-ai claude --offline --memory 3g")
    assert said not in expert.line_note.text() and board.board.first("limits").props == {"--memory": "3g"}


def test_what_the_launcher_refuses_is_said_below_the_line(window, qapp, wait):
    """A value the launcher's own check refuses (the board's dry run in the background): applied - it can be read -
    and said below the command line, with the way to the Board."""
    expert = window.launch
    window.show()
    window.show_page("launch")
    typed(window, "sandbox-ai claude --memory lots")
    assert window.board.board.first("limits").props == {"--memory": "lots"}
    assert wait(lambda: expert.line_note.property("role") == "notice-error" and "lots" in expert.line_note.text(), 60)
    assert expert.board_button.isVisible()
    expert.board_button.click()
    assert window.current_page() == "board"
    window.show_page("launch")
    typed(window, "sandbox-ai claude --memory 4g")
    assert wait(lambda: expert.line_note.property("role") != "notice-error", 60), expert.line_note.text()
    assert not expert.board_button.isVisible() or expert.line_note.property("role") == "notice-warn"


def test_a_problem_of_the_board_is_said_in_the_expert_view(window, qapp):
    """What the board itself finds wrong (an agent sandbox-ai does not have) is below the Expert view's line too."""
    expert = window.launch
    window.board.add_node("mount", source="/tmp/data", dest="relative/path", mode="ro")
    assert expert.line_note.property("role") == "notice-error" and "✘" in expert.line_note.text()
    assert "relative/path" in expert.line_note.toolTip() or "/mnt" in expert.line_note.toolTip()


def test_switching_views_loses_nothing(window, qapp):
    board, expert = window.board, window.launch
    window.show()
    typed(window, "sandbox-ai codex --offline --memory 4g -w /tmp/a -m /tmp/d:/mnt/d:rw --mcp files --name n -- exec")
    board.add_node("bridge", peer="other", direction="out")  # (a part of the app alone: no option of the line)
    node = board.board.first("limits")
    board.items[node.id].setPos(900.0, 700.0)
    before = board.board.state()
    board.moved(before)
    state, start, steps = board.board.state(), start_of(window), board.undo.count()

    def unplaced(found):
        """The board's state without where its parts sit: showing the Board arranges it (the user 2026-11-23)."""
        return {**found, "nodes": [{k: v for k, v in node.items() if k not in ("x", "y")} for node in found["nodes"]]}
    for view in ("launch", "board", "launch", "board"):
        window.view_switch.buttons[view].click()
        qapp.processEvents()
        assert window.current_page() == view
        assert unplaced(board.board.state()) == unplaced(state) and start_of(window) == start
        assert board.undo.count() == steps, "arranging on the way is no step of the Undo history"
        assert not board.dirty() or steps, "nor a change to apply"
        assert expert.preview.toPlainText() == shlex.join(["sandbox-ai", start[0], *start[1], "--", *start[2]])
    # ... also what was typed into a field a moment ago: it is on the board before the Board is looked at
    window.show_page("launch")
    expert.form.widgets["--cpus"].setText("3")
    window.view_switch.buttons["board"].click()
    assert board.board.first("limits").props == {"--memory": "4g", "--cpus": "3"}


def test_the_board_is_arranged_and_centred_whenever_it_is_shown_or_a_board_arrives(window, qapp):
    """The user 2026-11-23: "wenn man aufs Board wechselt oder ein neues Board lädt, egal woher, immer erst anordnen und
    zentrieren": a part moved aside is back on the grid when the Board is switched to, every part is in view, and
    neither is a step of the Undo history."""
    board = window.board
    window.show()
    window.show_page("board")
    qapp.processEvents()
    node = board.board.first("agent")
    board.items[node.id].setPos(5000.0, 4000.0)
    board.moved(board.board.state())
    steps = board.undo.count()
    window.view_switch.buttons["launch"].click()
    window.view_switch.buttons["board"].click()
    qapp.processEvents()
    qapp.processEvents()
    assert (board.board.first("agent").x, board.board.first("agent").y) != (5000.0, 4000.0), "arranged"
    shown = board.view.mapToScene(board.view.viewport().rect()).boundingRect()
    assert all(shown.contains(item.sceneBoundingRect().center()) for item in board.items.values()), "all in view"
    assert board.undo.count() == steps
    # a board that arrives (here: the same start loaded again as a new board) is arranged too
    from sandbox_ai_app import board as B
    fresh = B.from_argv(board.sai, "claude", [])
    for each in fresh.nodes:
        each.x, each.y = 7000.0, 7000.0
    board.set_board(fresh, None)
    qapp.processEvents()
    assert all((each.x, each.y) != (7000.0, 7000.0) for each in board.board.nodes)


def test_undo_and_redo_are_one_history_for_both_views(window, qapp):
    board, expert = window.board, window.launch
    window.show()
    window.show_page("launch")
    assert expert.undo_redo.isVisible() and not expert.undo_button.isEnabled()
    expert.form.widgets["--memory"].setText("4g")
    flush(window, qapp)
    board.set_prop(board.board.first("network").id, "--offline", True)
    typed(window, "sandbox-ai claude --offline --memory 4g --cpus 2")
    assert board.undo.count() == 3 and expert.undo_button.isEnabled() and not expert.redo_button.isEnabled()
    expert.undo_button.click()  # the line's edit
    assert "--cpus" not in board.board.first("limits").props and expert.form.widgets["--cpus"].text() == ""
    board.undo_group.undo()  # the Board's edit, undone from the Board
    assert not expert.form.widgets["--offline"].isChecked()
    expert.undo_button.click()  # the field's edit
    assert board.board.first("limits") is None or not board.board.first("limits").props
    assert expert.form.widgets["--memory"].text() == "" and expert.preview.toPlainText() == "sandbox-ai claude"
    assert not expert.undo_button.isEnabled() and expert.redo_button.isEnabled()
    expert.redo_button.click()
    board.undo_group.redo()
    expert.redo_button.click()
    assert expert.preview.toPlainText() == line(window, "claude", ["--offline", "--memory", "4g", "--cpus", "2"])
    assert board.board.first("limits").props == {"--memory": "4g", "--cpus": "2"}


def test_a_value_typed_letter_by_letter_is_one_step_of_undo(window, qapp):
    board, expert = window.board, window.launch
    window.show()
    window.show_page("launch")
    field = expert.form.widgets["--memory"]
    field.setFocus()
    for letter in "12g":
        QTest.keyClicks(field, letter)
        flush(window, qapp)
    assert board.board.first("limits").props == {"--memory": "12g"} and board.undo.count() == 1
    assert field.text() == "12g" and field.cursorPosition() == 3, "the field typed into is left alone"
    board.set_prop(board.board.first("limits").id, "--cpus", "2")
    QTest.keyClicks(field, "x")
    flush(window, qapp)
    assert board.undo.count() == 3, "after another change it is a step of its own"
    board.undo_group.undo()
    board.undo_group.undo()
    board.undo_group.undo()
    assert expert.form.widgets["--memory"].text() == ""


def test_starting_from_either_view_runs_the_same_command(window, qapp, env):
    board, expert = window.board, window.launch
    project = env / "project"
    project.mkdir()
    window.show()
    window.show_page("launch")
    typed(window, f"sandbox-ai codex --offline --memory 4g -w {project} --name night -- exec")
    expert.form.widgets["--cpus"].setText("2")  # typed a moment ago, not left yet: part of the start all the same
    assert expert.start(embedded=False) is True
    from_expert = window.started[-1]
    window.show_page("board")
    assert board.start(embedded=False) is True
    from_board = window.started[-1]
    assert len(window.started) == 2 and from_expert == from_board
    argv = from_expert[from_expert.index("codex"):]
    assert argv == ["codex", *ordered(window, ["--workspace", str(project), "--offline", "--memory", "4g", "--cpus",
                                               "2", "--name", "night"]), "--no-config", "--workspaces-as-given", "--",
                    "exec"]
    # ... and the dry run of either view is the board's check of that very start
    asked = []
    window.sai.host_dry_run = lambda host, agent, argv, **_: asked.append((host, agent, list(argv))) or ({}, "")
    expert.dry_button.click()
    board.dry_button.click()
    assert qapp.processEvents() or True
    from conftest import wait_until
    assert wait_until(qapp, lambda: len([a for a in asked if "--cpus" in a[2]]) >= 2, 30)
    runs = [a for a in asked if "--cpus" in a[2]][-2:]
    assert runs[0] == runs[1] == ("local", "codex", B.check_argv(window.sai, board.board))


def test_copy_command_copies_the_same_line_in_both_views(window, qapp):
    board, expert = window.board, window.launch
    typed(window, "sandbox-ai claude --offline -- 'fix it'")
    clipboard = QtWidgets.QApplication.clipboard()
    clipboard.setText("")
    expert.copy_button.click()
    assert clipboard.text() == "sandbox-ai claude --offline -- 'fix it'" == expert.preview.toPlainText()
    clipboard.setText("")
    board.copy_button.click()
    assert clipboard.text() == "sandbox-ai claude --offline -- 'fix it'"


def test_a_preset_chosen_in_the_expert_view_is_the_boards(window, qapp, env):
    """The preset chooser of the Expert view makes the start of both views, as the Board's New from preset does."""
    board, expert = window.board, window.launch
    from sandbox_ai_app import presets as PR
    preset = PR.catalogue(window.sai, "local", mine=[]).find("locked")
    assert expert.apply_preset(preset, {}) is True
    assert board.board.first("network").props.get("--offline") is True
    assert expert.form.widgets["--offline"].isChecked() and "--offline" in expert.preview.toPlainText()
    assert start_of(window)[1] == B.start_argv(window.sai, PR.build(
        window.sai, preset, board.preset_folder(), agent=board.default_agent(), values={}))[1]


def test_a_profile_loaded_in_the_expert_view_is_one_step_on_the_board(window, qapp, env):
    board, expert = window.board, window.launch
    project = env / "project"
    project.mkdir()
    board.add_node("workspace", path=str(project))
    window.sai.save_profile("p", "opencode", ["-o", "--gpu", "dri", "--from-the-future"], ["--verbose"])
    expert.refresh_profiles()
    expert.profile.setCurrentText("p")
    steps = board.undo.count()
    expert.load_profile()
    assert board.undo.count() == steps + 1
    assert start_of(window) == ("opencode", ordered(window, ["--workspace", str(project), "--gpu", "dri", "--offline",
                                                             "--from-the-future"]), ["--verbose"])
    assert board.board.extra == ["--from-the-future"], "what this version does not know is kept on Other options"
    # ... and stays there when a field is changed afterwards
    expert.form.widgets["--cpus"].setText("2")
    flush(window, qapp)
    assert board.board.extra == ["--from-the-future"] and "--from-the-future" in expert.preview.toPlainText()
    typed(window, expert.preview.toPlainText().replace("--cpus 2", "--cpus 3"))
    assert board.board.first("limits").props == {"--cpus": "3"} and board.board.extra == ["--from-the-future"]


def test_the_host_is_the_same_in_both_views(window, qapp, monkeypatch):
    board, expert = window.board, window.launch
    for combo in (board.host_choice, expert.host_choice):
        combo.addItem("build1", "build1")
    typed(window, "sandbox-ai claude --offline")
    expert.host_choice.choose("build1")
    assert board.host() == "build1" == expert.host()
    assert expert.preview.toPlainText() == "sandbox-ai hosts run build1 -- claude --offline"
    board.host_choice.choose("local")
    assert expert.host() == "local" and expert.preview.toPlainText() == "sandbox-ai claude --offline"
    typed(window, "sandbox-ai hosts run build1 -- claude --offline --cpus 2")
    assert board.host() == "build1" == expert.host() and board.board.first("limits").props == {"--cpus": "2"}
    typed(window, "sandbox-ai claude --offline --cpus 2")
    assert board.host() == "local"


def test_each_project_has_its_start_in_both_views(window, qapp, env):
    """The project's tab chosen: the Expert view shows that project's board, as the Board view does."""
    one, two = env / "one", env / "two"
    for folder in (one, two):
        folder.mkdir()
    (two / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--offline"],
                                                      "mode": "high"}))
    board, expert = window.board, window.launch
    window.set_project(str(one))
    assert expert.form.widgets["--workspace"].value() == [str(one)] and expert.agent.currentText() == "claude"
    expert.form.widgets["--memory"].setText("4g")
    flush(window, qapp)
    window.set_project(str(two))
    assert board.directory == str(two)
    assert start_of(window) == ("codex", ["--mode", "high", "--workspace", str(two), "--offline"], [])  # (the file's own first)
    assert expert.agent.currentText() == "codex" and expert.form.widgets["--offline"].isChecked()
    assert expert.form.widgets["--mode"].currentText() == "high" and expert.form.widgets["--memory"].text() == ""
    assert expert.preview.toPlainText() == shlex.join(["sandbox-ai", "codex", "--mode", "high", "--workspace",
                                                       str(two), "--offline"])
    # the file's own key, changed in the Expert view, stays the file's key (not a second --mode)
    expert.form.widgets["--mode"].setCurrentText("paranoid")
    flush(window, qapp)
    assert board.board.document_extra == {"mode": "paranoid"} and not board.board.extra
    window.set_project(str(one))
    assert expert.form.widgets["--memory"].text() == "4g" and expert.agent.currentText() == "claude"
    assert expert.form.widgets["--mode"].currentText() in ("", "medium")


# -- the part Other options --

def test_other_options_is_a_part_of_the_board_with_a_field(window, qapp):
    board, expert = window.board, window.launch
    typed(window, "sandbox-ai claude --mode high --name night")
    other = board.board.first("other")
    assert other is not None and other.id in board.items
    assert "Other options" in other.title()
    from sandbox_ai_app.board_view import summary
    assert summary(other) == "--mode high --name night"
    board.inspect(other)
    assert board.other_edit.text() == "--mode high --name night"
    # an option another part owns moves to that part; the rest stays here
    assert board.set_other("--mode paranoid --memory 4g --canaries aws") is None
    assert board.board.extra == ["--mode", "paranoid", "--canaries", "aws"]
    assert board.board.first("limits").props == {"--memory": "4g"}
    assert expert.form.widgets["--mode"].currentText() == "paranoid" and expert.form.widgets["--memory"].text() == "4g"
    before = board.board.state()
    assert "quotation mark" in board.set_other("--name 'x")
    assert "--nope" in board.set_other("--nope")
    assert board.board.state() == before
    board.remove_node(other.id)
    assert board.board.extra == [] and "--mode" not in expert.preview.toPlainText()
    assert expert.form.widgets["--mode"].currentText() in ("", "medium")
    kinds = [board.palette.item(row).data(Qt.ItemDataRole.UserRole) for row in range(board.palette.count())]
    assert "other" in kinds


def test_a_start_from_elsewhere_lands_on_the_page(window, qapp):
    """show_start_line (what leads to a start from another page): one step of the page's Undo, the page shown in the
    view chosen last."""
    board = window.board
    window.show_page("sandboxes")
    steps = board.undo.count()
    window.show_start_line("codex", ["--offline", "--name", "night"], ["exec"], raise_window=False)
    assert window.current_page() == "board" and board.undo.count() == steps + 1
    assert start_of(window) == ("codex", ordered(window, ["--offline", "--name", "night"]), ["exec"])
    assert window.launch.agent.currentText() == "codex"


def test_resume_with_shows_the_sessions_whole_start_on_the_page(window, qapp):
    """Resume with… (Sandboxes & sessions): "On the Start page…" shows the session's start with what the dialog adds
    on the Start page - nothing is resumed -, when the options it was created with are known."""
    from sandbox_ai_app.ui import ResumeDialog
    dialog = ResumeDialog(window.sandboxes, "work", start=("codex", ["--offline", "--memory", "4g"], ["exec"]))
    assert dialog.start_button.isEnabled() and dialog.start_button.text() == "On the Start page…"
    dialog.workspaces.set_value(["/tmp/extra"])
    dialog.servers.setText("files")
    dialog.options.setText("--cpus 2")
    assert dialog.start_line() == ("codex", ["--offline", "--memory", "4g", "--workspace", "/tmp/extra", "--mcp",
                                             "files", "--cpus", "2"], ["exec"])
    dialog.start_button.click()
    assert dialog.to_start and dialog.result() == QtWidgets.QDialog.DialogCode.Rejected
    line = dialog.start_line()
    unknown = ResumeDialog(window.sandboxes, "other")
    assert not unknown.start_button.isEnabled() and unknown.start_button.toolTip()
    for made in (dialog, unknown):
        made.deleteLater()
    # the Sessions page hands it to the window: the page, in the view chosen last, one step of its Undo
    window.show_page("sandboxes")
    steps = window.board.undo.count()
    window.sandboxes.start_wanted.emit(*line, "local")
    assert window.current_page() == "board" and window.board.undo.count() == steps + 1
    assert start_of(window) == ("codex", ordered(window, ["--workspace", "/tmp/extra", "--offline", "--memory", "4g",
                                                          "--cpus", "2", "--mcp", "files"]), ["exec"])
    assert window.launch.form.widgets["--cpus"].text() == "2"


def test_a_field_typed_a_moment_ago_is_never_put_back_by_the_board(window, qapp):
    """The board is shown again for a reason of its own (a late answer makes it look at itself) while a field of the
    Expert view was typed into and is not on the board yet: the field wins, and is on the board."""
    board, expert = window.board, window.launch
    window.show_page("launch")
    expert.form.widgets["--memory"].setText("6g")  # (not on the board yet: it goes there in a moment)
    assert board.board.first("limits") is None
    board.refresh()
    assert expert.form.widgets["--memory"].text() == "6g"
    assert board.board.first("limits").props == {"--memory": "6g"}
    # ... and another agent chosen right after a field: the field is the start's, then the agent changes as on the
    # Board (what only the agent before took goes)
    typed(window, "sandbox-ai claude --memory 6g --effort max")
    assert board.board.first("agent").props.get("effort") == "max"
    expert.form.widgets["--cpus"].setText("2")
    expert.agent.setCurrentText("opencode")
    agent = board.board.first("agent").props
    assert agent["agent"] == "opencode" and not agent.get("effort"), "an effort OpenCode does not take goes"
    assert board.board.first("limits").props == {"--memory": "6g", "--cpus": "2"}
    assert expert.agent.currentText() == "opencode" and expert.form.widgets["--cpus"].text() == "2"
    flush(window, qapp)
    assert board.board.first("limits").props == {"--memory": "6g", "--cpus": "2"}
