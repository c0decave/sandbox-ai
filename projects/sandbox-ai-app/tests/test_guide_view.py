"""The guide on screen: the tour panel walks the window's pages, and a part added from the board's list is set up
question by question (guide_view.py, with guide.py behind it)."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from sandbox_ai_app import guide, guide_view  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures


def test_the_tour_walks_the_pages_and_remembers_where_it_was(window):  # noqa: F811
    window.start_tour(0)
    assert window.tour_dock.isVisible() and window.current_page() == "launch"
    assert "1 of" in window.tour.where.text() and window.tour.title.text() == "Start"
    window.tour.next.click()
    assert window.current_page() == "board" and window.state.get("tour_step") == 1
    window.tour.back.click()
    assert window.current_page() == "launch" and not window.tour.back.isEnabled()
    window.tour.go(len(guide.TOUR) - 1)
    assert window.current_page() == guide.TOUR[-1].page and window.tour.next.text() == "Finish"
    window.tour.next.click()
    assert not window.tour_dock.isVisible(), "Finish closes it"
    window.start_tour()  # Help > Guided tour: where it was left
    assert window.tour.index == len(guide.TOUR) - 1 and window.tour_dock.isVisible()
    QtWidgets.QApplication.sendEvent(window.tour, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                            Qt.KeyboardModifier.NoModifier))
    assert not window.tour_dock.isVisible() and window.state.get("tour_offered") is True


def test_a_new_part_is_set_up_question_by_question(window, monkeypatch, tmp_path):  # noqa: F811
    board = window.board
    folder = tmp_path / "mount-source"  # not "data": XDG_DATA_HOME of the window lives there
    folder.mkdir()

    def answer(dialog):
        assert dialog.pages.count() == 1 + len(guide.questions("mount")) + 1, "intro, questions, what is left"
        question, widget = dialog.inputs["source"]
        widget.edit.setText(str(folder))
        dialog.inputs["mode"][1].setCurrentIndex(dialog.inputs["mode"][1].findData("rw"))
        for _ in range(dialog.pages.count() - 1):
            dialog.next.click()
        assert dialog.next.text() == "Done"
        texts = [dialog.summary.item(i).text() for i in range(dialog.summary.count())]
        assert not any("not set yet" in text for text in texts), texts
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(guide_view.NodeGuide, "exec", lambda self: answer(self))
    item = next(board.palette.item(i) for i in range(board.palette.count())
                if board.palette.item(i).data(Qt.ItemDataRole.UserRole) == "mount")
    board.palette.itemDoubleClicked.emit(item)
    mount = board.board.of_kind("mount")[-1]
    assert mount.props["source"] == str(folder) and mount.props["mode"] == "rw"
    board.undo_group.undo()
    assert board.board.of_kind("mount")[-1].props["source"] == "", "the answers are one undo step"


def test_cancel_changes_nothing_and_the_setting_turns_it_off(window, monkeypatch):  # noqa: F811
    board = window.board
    opened = []
    monkeypatch.setattr(guide_view.NodeGuide, "exec",
                        lambda self: opened.append(self.node.kind) or QtWidgets.QDialog.DialogCode.Rejected)
    board.add_node("limits", guided=True)
    assert opened == ["limits"] and "--memory" not in board.board.first("limits").props
    board.settings = lambda: {"guide_nodes": False}
    board.add_node("display", guided=True)
    assert opened == ["limits"], "no guide when the setting says no"
    board.add_node("logs")  # added by code (presets, the wizard): never guided
    assert opened == ["limits"]


def test_the_inspector_offers_the_guide_for_a_part(window, monkeypatch):  # noqa: F811
    board = window.board
    opened = []
    monkeypatch.setattr(guide_view.NodeGuide, "exec",
                        lambda self: opened.append(self.node.kind) or QtWidgets.QDialog.DialogCode.Rejected)
    node = board.board.first("network")
    board.inspect(node)
    button = next(b for b in board.inspector_holder.findChildren(QtWidgets.QPushButton)
                  if b.text() == "Set up step by step…")
    button.click()
    assert opened == ["network"]


def test_a_part_that_is_not_ready_says_what_is_missing(window):  # noqa: F811
    node = window.board.board.add("mcp", name="")
    dialog = guide_view.NodeGuide(node, [], window)
    dialog.show_page(dialog.pages.count() - 1)
    texts = [dialog.summary.item(i).text() for i in range(dialog.summary.count())]
    assert texts == ["✘ Server is not set yet"]
    dialog.inputs["name"][1].setText("demo")
    dialog.show_page(dialog.pages.count() - 1)
    assert dialog.summary.item(0).text() == "✔ Nothing: the node is ready."
