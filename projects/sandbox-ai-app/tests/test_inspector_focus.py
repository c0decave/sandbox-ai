"""The properties keep the keyboard (feedback 2026-11-11): a list field takes what was typed after a pause, and the
change makes the inspector's rows anew - the field being typed in keeps the focus, the text as typed (its last empty
line too) and the cursor, also in a floating panel."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import backend  # noqa: E402
from sandbox_ai_app.board_view import LinesEdit  # noqa: E402

from test_board_view import page  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def test_typing_in_a_list_field_keeps_the_focus_across_the_commit(page, qapp):
    network = page.board.first("network")
    holder = QtWidgets.QWidget()
    holder.setLayout(QtWidgets.QVBoxLayout())
    holder.layout().addWidget(page.inspector_holder)
    holder.show()
    holder.activateWindow()
    page.inspected = network.id
    page.inspect(network)
    for _ in range(5):
        qapp.processEvents()
    lists = page.inspector_holder.findChildren(LinesEdit)
    assert lists, "the network part has list fields"
    field = lists[0]
    index = lists.index(field)
    field.setFocus()
    qapp.processEvents()
    QTest.keyClicks(field, "10.0.0.5:22")
    QTest.keyClick(field, "\r")
    field.timer.timeout.emit()  # the pause: the list is taken, the board changed
    for _ in range(5):
        qapp.processEvents()
    page.inspect_timer.timeout.emit()  # the rows made anew
    qapp.processEvents()
    focused = QtWidgets.QApplication.focusWidget()
    assert isinstance(focused, LinesEdit) and focused is not field
    assert page.inspector_holder.findChildren(LinesEdit).index(focused) == index
    assert focused.toPlainText().endswith("10.0.0.5:22\n"), "the new empty line stays"
    assert focused.textCursor().position() == len(focused.toPlainText())
    QTest.keyClicks(focused, "10.0.0.6:22")
    assert focused.values()[-2:] == ["10.0.0.5:22", "10.0.0.6:22"]
    holder.close()
