"""The file manager's quick filter (user 2026-11-19: "für Dateimanager: Schnellsuchfilter"): a field above each pane's
list narrows the rows to the names that hold the text typed (case-insensitive; * and ? as wildcards); "." and ".."
stay; what is not shown is never selected, so Select all, Copy, Move and Delete act only on the rows shown. Ctrl+F puts
the keyboard into the field, Escape empties it and gives the list the keyboard back; another folder starts unfiltered.
The pane says how many of how many it shows."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from test_files_page import names, press, select, settle, setup  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME")


def test_typing_narrows_the_rows_and_says_how_many(setup):
    page, ws, _host, _ = setup
    pane = page.left
    assert names(pane) == ["a.txt", "escape", "run.sh", "sub"]
    pane.quick.setText("TXT")
    assert names(pane) == ["a.txt"]
    rows = [pane.table.item(r, 0).text() for r in range(pane.table.rowCount())]
    assert rows[:2] == [".", ".."], "the folder and the one above stay"
    assert "1" in pane.quick_count.text() and "4" in pane.quick_count.text()
    pane.quick.setText("r*.sh")
    assert names(pane) == ["run.sh"]
    pane.quick.setText("")
    assert names(pane) == ["a.txt", "escape", "run.sh", "sub"]
    assert not pane.quick_count.isVisibleTo(pane)


def test_select_all_and_chosen_take_only_the_rows_shown(setup):
    page, _ws, _host, _ = setup
    pane = page.left
    names(pane)
    pane.quick.setText("s")  # run.sh, sub, escape ("s" in each), not a.txt? a.txt has no s
    shown = names(pane)
    assert "a.txt" not in shown and shown
    pane.select_all()
    assert sorted(e.name for e in pane.chosen()) == shown


def test_another_folder_starts_unfiltered_and_a_relisting_keeps_the_filter(setup):
    page, _ws, _host, _ = setup
    pane = page.left
    names(pane)
    pane.quick.setText("a.txt")
    pane.list()  # listed again (a Refresh, after a copy): the filter stays
    assert names(pane) == ["a.txt"]
    pane.quick.setText("sub")
    select(pane, "sub")
    pane.go("sub")
    settle(pane)
    assert pane.quick.text() == "" and names(pane) == ["b.txt"]


def test_ctrl_f_and_escape(setup, qapp):
    page, _ws, _host, _ = setup
    pane = page.left
    page.show()
    names(pane)
    pane.table.setFocus()
    press(pane, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert pane.quick.hasFocus() or qapp.focusWidget() is pane.quick
    pane.quick.setText("run")
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    QtWidgets.QApplication.sendEvent(pane.quick, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                           Qt.KeyboardModifier.NoModifier))
    qapp.processEvents()
    assert pane.quick.text() == "" and names(pane) == ["a.txt", "escape", "run.sh", "sub"]
    page.hide()
