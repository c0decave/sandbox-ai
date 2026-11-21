"""A part added from the Board's parts list opens its setup (the guide); Cancel there means it was never added (user
2026-11-19: "wenn man Abbrechen klickt, wird es trotzdem hinzugefügt"): the board as before, nothing to undo for it, no
Redo left behind. The setup of a part that is on the board already (Wizard…, the inspector's button) changes nothing on
Cancel - and OK still adds or sets as before, as one undo step."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import guide_view  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures

REJECT, ACCEPT = QtWidgets.QDialog.DialogCode.Rejected, QtWidgets.QDialog.DialogCode.Accepted


def answer_with(monkeypatch, code, before=None):
    opened = []

    def run(dialog):
        opened.append(dialog.node.kind)
        if before is not None:
            before(dialog)
        return code
    monkeypatch.setattr(guide_view.NodeGuide, "exec", run)
    return opened


def test_cancel_on_a_new_part_leaves_the_board_as_it_was(window, monkeypatch):  # noqa: F811
    board = window.board
    state, index, count = board.board.state(), board.undo.index(), board.undo.count()
    opened = answer_with(monkeypatch, REJECT)
    board.add_node("limits", guided=True)
    assert opened == ["limits"]
    assert board.board.first("limits") is None, "cancelled: not added"
    assert board.board.state() == state
    assert (board.undo.index(), board.undo.count()) == (index, count), "no undo step and no Redo left behind"
    assert not board.undo.canRedo()


def test_cancel_on_a_new_part_with_answers_typed_also_leaves_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    board = window.board
    state = board.board.state()

    def type_in(dialog):
        dialog.inputs["source"][1].edit.setText(str(tmp_path))
    answer_with(monkeypatch, REJECT, type_in)
    board.add_node("mount", guided=True)
    assert board.board.state() == state


def test_ok_on_a_new_part_adds_it_as_before(window, monkeypatch):  # noqa: F811
    board = window.board
    answer_with(monkeypatch, ACCEPT)
    board.add_node("limits", guided=True)
    assert board.board.first("limits") is not None


def test_cancel_in_the_setup_of_a_part_on_the_board_changes_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    board = window.board
    answer_with(monkeypatch, ACCEPT)
    board.add_node("mount", guided=True)
    mount = board.board.of_kind("mount")[-1]
    state, index, count = board.board.state(), board.undo.index(), board.undo.count()

    def type_in(dialog):
        dialog.inputs["source"][1].edit.setText(str(tmp_path))
        dialog.inputs["mode"][1].setCurrentIndex(dialog.inputs["mode"][1].findData("rw"))
    answer_with(monkeypatch, REJECT, type_in)
    board.guide_node(mount.id)
    assert board.board.state() == state, "Cancel: none of the answers typed is set"
    assert (board.undo.index(), board.undo.count()) == (index, count)
    assert board.board.node(mount.id) is not None, "the part stays: it was there before"
