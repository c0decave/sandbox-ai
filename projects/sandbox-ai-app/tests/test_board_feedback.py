"""The board as asked for in the feedback of 2026-11-10: properties on a double click with OK, Cancel and Remove; an [x]
on each part that may go (asked first unless the setting says otherwise); the CLI command behind a button; a
codename for a session without a name; Ctrl+S applies; the board kept at once when it is left, with its steps of
Undo; every MCP server there is offered; the project folder and the extra folder named and explained."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import backend, board as B, i18n, widgets  # noqa: E402
from sandbox_ai_app.board_view import NodeCloseMark, history_of  # noqa: E402

from test_board_view import page  # noqa: E402,F401
from test_projects import env, make_window, project, window  # noqa: E402,F401
from test_state import restart  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
LEFT, NONE = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier


def center(view, item):
    return view.mapFromScene(item.sceneBoundingRect().center())


def buttons(page):
    box = page.edit_box
    return {b.text(): b for b in box.buttons()}


def remove_button(page):
    return next(b for b in page.inspector_holder.findChildren(QtWidgets.QPushButton) if b.text() == "Remove")


def test_a_part_opens_on_a_double_click_and_moves_while_held(page, qapp):
    opened = []
    page.inspector_request = lambda: opened.append(True)
    page.view.resize(900, 600)
    page.show()
    qapp.processEvents()
    agent = page.items[page.board.first("agent").id]
    QTest.mouseClick(page.view.viewport(), LEFT, NONE, center(page.view, agent))
    qapp.processEvents()
    assert agent.isSelected() and opened == [], "a click selects, nothing opens"
    QTest.mouseDClick(page.view.viewport(), LEFT, NONE, center(page.view, agent))
    qapp.processEvents()
    assert opened, "a double click opens the properties"
    agent = page.items[page.board.first("agent").id]  # (made again after a change of the board)
    before = (page.board.first("agent").x, page.board.first("agent").y)
    start = center(page.view, agent)
    end = start + QPoint(60, 40)
    QTest.mousePress(page.view.viewport(), LEFT, NONE, start)
    QTest.mouseMove(page.view.viewport(), start + QPoint(10, 5))
    QTest.mouseMove(page.view.viewport(), end)
    QTest.mouseRelease(page.view.viewport(), LEFT, NONE, end)
    qapp.processEvents()
    assert (page.board.first("agent").x, page.board.first("agent").y) != before, "pressed and held, the part moves"


def test_cancel_goes_back_to_the_part_as_it_was_and_ok_keeps(page, qapp):
    page.add_node("limits")
    node = page.board.first("limits")
    page.inspect(node)
    assert not buttons(page)["Cancel"].isEnabled(), "nothing to undo yet"
    page.set_prop(node.id, "--memory", "6g")
    page.set_prop(node.id, "--cpus", "2")
    page.inspect(page.board.node(node.id))
    assert buttons(page)["Cancel"].isEnabled()
    buttons(page)["Cancel"].click()
    qapp.processEvents()
    kept = page.board.node(node.id).props
    assert "--memory" not in kept and "--cpus" not in kept, "Cancel undoes what changed since it opened"
    page.inspect(page.board.node(node.id))
    page.set_prop(node.id, "--memory", "8g")
    page.inspect(page.board.node(node.id))
    buttons(page)["OK"].click()
    qapp.processEvents()
    assert page.board.node(node.id).props["--memory"] == "8g"
    page.inspect(page.board.node(node.id))
    assert not buttons(page)["Cancel"].isEnabled(), "after OK, Cancel has nothing to go back to"


def test_remove_in_the_properties_and_never_for_the_agent(page, qapp):
    page.add_node("devices")
    node = page.board.first("devices")
    page.inspect(node)
    remove_button(page).click()
    qapp.processEvents()
    assert page.board.first("devices") is None
    page.undo_group.undo()
    assert page.board.first("devices") is not None, "Undo brings it back"
    page.inspect(page.board.first("agent"))
    assert not [b for b in page.inspector_holder.findChildren(QtWidgets.QPushButton) if b.text() == "Remove"]


@pytest.mark.parametrize("confirm", [True, False])
def test_the_x_of_a_part_asks_first_unless_the_setting_says_not(page, qapp, monkeypatch, confirm):
    page.settings = lambda: {"board_grid": 20, "board_confirm_remove": confirm}
    asked = []
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: asked.append(a[1]) or False)  # Keep
    page.add_node("devices")
    node = page.board.first("devices")
    item = page.items[node.id]
    assert isinstance(item.close_mark, NodeCloseMark)
    assert page.items[page.board.first("agent").id].close_mark is None, "the agent stays on the board"
    page.ask_remove(node.id)
    if confirm:
        assert asked == ["Remove the part"] and page.board.first("devices") is not None, "Keep keeps it"
    else:
        assert asked == [] and page.board.first("devices") is None, "removed at once"
    assert backend.needs_confirm("board-remove-part", {"ask": True})
    assert backend.needs_confirm("board-remove-part", {"ask": False}) is None


def test_the_cli_command_is_behind_its_button(page, qapp):
    page.show()
    qapp.processEvents()
    assert not page.preview.isVisible() and page.preview.toPlainText().startswith("sandbox-ai claude")
    page.cli_button.click()
    qapp.processEvents()
    assert page.preview.isVisible()
    page.cli_button.click()
    assert not page.preview.isVisible()


def test_a_session_without_a_name_gets_a_codename(page, monkeypatch):
    name = page.session_suggestion
    assert name.count("-") >= 2 and page.session.placeholderText() == name
    started = []
    monkeypatch.setattr(page, "start", lambda **kw: started.append(kw) or True)
    page.start_session()
    assert started == [{"embedded": True, "session": name}] and page.session_suggestion != ""
    assert page.session.placeholderText() and page.session_suggestion != name, "the next one gets another"
    page.session.setText("mine")
    page.start_controls.where.setCurrentIndex(page.start_controls.where.findData("window"))
    page.start_controls.start()
    assert started[-1] == {"embedded": False, "session": "mine"}
    page.start_controls.as_session.setChecked(False)  # not as a session: it ends with its terminal
    page.start_controls.start()
    assert started[-1] == {"embedded": False, "session": None} and not page.session.isEnabled()


def test_ctrl_s_applies_the_board(page, qapp, monkeypatch):
    applied = []
    monkeypatch.setattr(page, "apply", lambda *a, **k: applied.append(True))
    page.show()
    page.view.setFocus()
    qapp.processEvents()
    QTest.keyClick(page.view, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert applied == [True]


def test_every_mcp_server_is_offered(page, monkeypatch):
    monkeypatch.setattr(page.sai, "mcp_definitions", lambda: {"fetch": {}, "github": {}})
    page.mcp_catalog_names = ["duckduckgo", "fetch"]
    page.add_node("mcp", name="")
    node = page.board.first("mcp")
    page.inspect(node)
    combo = next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox) if c.isEditable())
    values = [combo.itemData(i) for i in range(combo.count())]
    assert values == ["fetch", "github", "duckduckgo"]
    assert "catalog" in combo.itemText(2)
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    assert page.board.node(node.id).props["name"] == "github"


def test_the_folders_say_what_they_are():
    i18n.set_language("de")
    try:
        assert i18n.tr(B.KINDS["workspace"].title) == "Projektordner"
        assert i18n.tr(B.KINDS["mount"].title) == "Zusatzordner"
        assert "nicht der Ort, an dem der Agent arbeitet" in i18n.tr(B.KINDS["mount"].help)
        assert i18n.tr("Fit") == "Zentrieren" and i18n.tr("CLI command") == "Cli-Befehl"
    finally:
        i18n.set_language("en")


def test_the_board_is_kept_when_it_is_left_with_its_undo(window, env, monkeypatch, qapp):
    folder = project(env, "kept")
    (folder / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
    window.set_project(folder)
    board = window.board
    window.show_page("board")
    board.add_node("limits", **{"--memory": "6g"})
    board.add_node("devices")
    writes = window.state.writes
    window.show_page("launch")  # leaving the page writes at once (no waiting for the delay), in the writer thread
    qapp.processEvents()
    import time
    deadline = time.monotonic() + 10
    while window.state.writes <= writes and time.monotonic() < deadline:
        time.sleep(0.02)
    assert window.state.writes > writes
    kept = json.loads(window.state.path.read_text())["boards"][board.key_of(folder)]
    assert kept["history"]["index"] == len(kept["history"]["steps"]) == 2
    again = restart(window, monkeypatch)
    try:
        board = again.board
        assert board.board.first("devices") is not None and board.undo.count() == 2
        board.undo_group.undo()
        assert board.board.first("devices") is None and board.board.first("limits") is not None, "step by step"
        board.undo_group.undo()
        assert board.board.first("limits") is None and not board.dirty(), "back to the project's file"
    finally:
        again.shutdown()


def test_history_of_keeps_the_last_steps(page):
    for index in range(25):
        page.add_node("limits") if index == 0 else page.set_prop(page.board.first("limits").id, "--pids", str(index))
    history = history_of(page.undo)
    assert len(history["steps"]) == 20 and history["index"] == 20
    page.undo_group.undo()
    assert history_of(page.undo)["index"] == 19


def test_undo_is_bounded_and_the_state_copies_only_the_steps_it_keeps(page, monkeypatch):
    """Soak (stability round 3): every step of Undo stayed for the whole run (some KB each), and every write of the
    state file - one after each change of the board - copied every one of them to keep the last 20: the app grew and
    each change took longer than the one before."""
    from sandbox_ai_app import board_view as BV
    monkeypatch.setattr(BV, "UNDO_LIMIT", 30)
    page.new_stack()
    page.add_node("limits")
    for index in range(40):
        page.set_prop(page.board.first("limits").id, "--pids", str(index + 1))
    assert page.undo.count() == 30 and page.board.first("limits").props["--pids"] == "40"
    copied = []
    scrub = BV.scrub_board
    monkeypatch.setattr(BV, "scrub_board", lambda state: copied.append(1) or scrub(state))
    history = history_of(page.undo)
    assert len(history["steps"]) == BV.HISTORY_STEPS and history["index"] == BV.HISTORY_STEPS
    assert len(copied) == 2 * BV.HISTORY_STEPS, "only the steps kept are copied"


def test_the_boards_kept_stay_within_their_budget_and_keep_every_draft():
    """Soak (stability round 3): a draft with its 20 steps of Undo is some 10-60 KB of the state file, and the boards of
    projects not opened for months are kept too: a few dozen made the file larger than state.LIMIT, and the next start
    read none of it (every draft lost). Over BOARDS_BUDGET the steps go - the largest first, the board shown last -
    and every draft stays."""
    from sandbox_ai_app.board_view import within_budget
    step = {"text": "Change", "before": {"nodes": ["x" * 400]}, "after": {"nodes": ["y" * 400]}}

    def entry(steps):
        return {"zoom": 1.0, "draft": {"nodes": [f"draft {steps}"]},
                "history": {"steps": [step] * steps, "index": steps}}
    boards = {"/shown": entry(10), "/big": entry(12), "/small": entry(2), "/plain": {"zoom": 1.0}}
    unchanged = json.loads(json.dumps(boards))
    assert within_budget(boards, "/shown", budget=10 ** 6) == unchanged, "within the budget nothing goes"
    within_budget(boards, "/shown", budget=len(json.dumps(unchanged, separators=(",", ":"))) - 100)
    assert "history" not in boards["/big"] and "history" in boards["/small"] and "history" in boards["/shown"]
    within_budget(boards, "/shown", budget=2000)
    assert [key for key in boards if "history" in boards[key]] == []
    assert {key: entry.get("draft") for key, entry in boards.items()} == {
        key: entry.get("draft") for key, entry in unchanged.items()}, "every draft stays"


def test_a_property_step_updates_the_parts_in_place_and_a_new_part_makes_the_scene_again(page):
    """Performance round 1: a step that changes only properties or places keeps the scene's items (it made every item
    anew before); a step that adds or removes a part makes the scene again."""
    page.add_node("limits")
    node = page.board.first("limits")
    item = page.items[node.id]
    page.set_prop(node.id, "--memory", "6g")
    assert page.items[node.id] is item and "6g" in " ".join(item.details)
    page.undo_group.undo()
    assert page.items[node.id] is item and "6g" not in " ".join(item.details)
    page.undo_group.undo()  # the part goes again
    assert page.board.first("limits") is None and node.id not in page.items


def test_printable_text_keeps_lines_and_escapes_the_rest():
    from sandbox_ai_app.files import printable_text
    plain = "a line\n\tand another"
    assert printable_text(plain) is plain or printable_text(plain) == plain
    assert printable_text("bell\x07 and\nnew") == "bell\\x07 and\nnew"


@pytest.mark.parametrize("meanwhile", ["edited", "other project"])
def test_a_change_made_while_apply_writes_stays_unapplied(page, tmp_path, monkeypatch, meanwhile):
    """Stability review 2: Apply writes the project file in a thread. What was changed on the board meanwhile was
    marked as applied when the write came back (setClean on the stack as it was then): never written, it was then no
    draft either and lost at the next start. And with another project shown by then, that one's board was marked
    applied and took the first project's folder."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert page.open_project(str(first))
    page.set_prop(page.board.first("network").id, "--allow-pypi", True)
    written = []  # the write's done callback, called when the test says (the thread's result)
    monkeypatch.setattr("sandbox_ai_app.board_view.background", lambda *a, done=None, **k: written.append(done))
    assert page.apply()
    if meanwhile == "edited":
        page.set_prop(page.board.first("network").id, "--allow-github", True)  # after Apply, before it is written
    else:
        assert page.open_project(str(second))
        page.set_prop(page.board.first("network").id, "--allow-github", True)
    written[0](first / ".sandbox-ai.json", None)
    assert page.dirty(), "the change made meanwhile is not written: still unapplied"
    if meanwhile == "other project":
        assert page.directory == str(second)
        assert not page.dirty_for(first), "the first project's board is applied"


def test_a_list_typed_into_belongs_to_its_part_when_another_is_selected_at_once(page, qapp):
    """Stability round 1 found it: a list field takes what was typed 600 ms after the typing stops; selecting another
    part sooner lost the text. Now it is taken for the part it was typed for."""
    from sandbox_ai_app.board_view import LinesEdit
    network = page.board.first("network")
    page.items[network.id].setSelected(True)
    page.inspect(network)
    edits = page.inspector_holder.findChildren(LinesEdit)
    assert edits, "the network has list fields"
    edit = edits[0]
    edit.setPlainText("pypi.org")
    assert edit.timer.isActive()
    page.scene.clearSelection()
    page.items[page.board.first("agent").id].setSelected(True)  # at once, before the 600 ms
    values = {key: value for key, value in page.board.node(network.id).props.items() if isinstance(value, list)}
    assert ["pypi.org"] in values.values(), values
