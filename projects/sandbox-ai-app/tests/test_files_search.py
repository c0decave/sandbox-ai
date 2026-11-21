"""The file manager's search window (file_search_view.py; user 2026-11-19: "brauchen im Dateimanager auch noch eine
generelle Suche mit Wildcard-Support"): Ctrl+Shift+F, the pane's search button and its context menu open it for the
folder shown; it searches below it in the background (this machine, a sandbox's folder, a remote system - the fake
SFTP server), lists path, size, date and kind, says every limit and what was not searched; a double click shows the
entry selected in its pane; the right click has Open, Show in folder, Copy path, copy to the other pane and Delete
(asked first, full paths). Offscreen, through the page's own entry points."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from sandbox_ai_app import file_search, widgets  # noqa: E402
from test_files_page import finish, names, press, select, settle, setup  # noqa: E402,F401
from test_files_remote import served  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME")


def searched(dialog, wait, pattern="", text=""):
    dialog.names.setText(pattern)
    dialog.text.setText(text)
    assert dialog.begin(), dialog.state.text()
    assert wait(lambda: not dialog.running, 30), "the search ends"
    return rows(dialog)


def rows(dialog):
    return sorted(widgets.row_key(dialog.table, row) for row in range(dialog.table.rowCount()))


def row_of(dialog, relative):
    return widgets.find_row(dialog.table, relative)


def pick(dialog, *relatives):
    dialog.table.clearSelection()
    widgets.select_keys(dialog.table, list(relatives))


def test_ctrl_shift_f_the_button_and_the_menu_open_it_for_the_folder_shown(setup, qapp):
    page, ws, host, _ = setup
    page.show()
    left = page.left
    names(left)
    left.table.setFocus()
    from PySide6.QtTest import QTest
    QTest.keyClick(left.table, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qapp.processEvents()
    dialog = page.searches.get("sandbox")
    assert dialog is not None and dialog.isVisible() and str(ws) in dialog.where.text()
    assert dialog.names.hasFocus() or qapp.focusWidget() is dialog.names
    dialog.close()
    qapp.processEvents()
    button = next(b for b in page.right.findChildren(QtWidgets.QPushButton) if b.accessibleName() == "Search…")
    assert "Ctrl+Shift+F" in button.toolTip()
    button.click()
    qapp.processEvents()
    assert str(host) in page.searches["host"].where.text(), "the right pane's folder"
    page.searches["host"].close()
    menu = page.menu(left, at_row=False)
    texts = [action.text() for action in menu.actions() if not action.isSeparator()]
    assert "Search below this folder…" in texts and texts.index("Search below this folder…") > texts.index("Refresh")
    page.hide()


def test_wildcards_below_the_folder_never_through_a_link(setup, wait):
    page, ws, _host, outside = setup
    (ws / "sub" / "deep").mkdir()
    (ws / "sub" / "deep" / "c.TXT").write_text("gamma")
    dialog = page.search_dialog(page.left)
    assert searched(dialog, wait, "*.txt") == ["a.txt", "sub/b.txt", "sub/deep/c.TXT"]
    assert "precious" not in str(rows(dialog)), "the link escape/ is never followed"
    assert searched(dialog, wait, "sub/**/*.txt") == ["sub/b.txt", "sub/deep/c.TXT"]
    assert searched(dialog, wait, "esc") == ["escape"], "a part of the name, the link itself listed"
    row = row_of(dialog, "escape")
    assert dialog.table.item(row, 3).text() == "symbolic link"
    assert searched(dialog, wait, "", "beta") == ["sub/b.txt"]
    assert "line 1" in dialog.table.item(row_of(dialog, "sub/b.txt"), 0).toolTip()
    dialog.case.setChecked(True)
    assert searched(dialog, wait, "*.TXT") == ["sub/deep/c.TXT"]
    assert "1 symbolic link" in dialog.summary.text() or "found" in dialog.state.text()
    dialog.close()


def test_a_double_click_shows_the_entry_selected_in_its_pane(setup, wait, qapp):
    page, ws, _host, _ = setup
    left = page.left
    left.quick.setText("a.txt")
    dialog = page.search_dialog(left)
    searched(dialog, wait, "b.txt")
    dialog.table.cellDoubleClicked.emit(row_of(dialog, "sub/b.txt"), 0)
    settle(left)
    assert left.relative == "sub" and [entry.name for entry in left.chosen()] == ["b.txt"]
    assert left.current().name == "b.txt" and left.quick.text() == ""
    # Enter in the list: the same.
    left.go("")
    settle(left)
    dialog.table.setCurrentCell(row_of(dialog, "sub/b.txt"), 0)
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    QtWidgets.QApplication.sendEvent(dialog.table, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                                             Qt.KeyboardModifier.NoModifier))
    settle(left)
    assert left.relative == "sub" and [entry.name for entry in left.chosen()] == ["b.txt"]
    assert dialog.isVisible() or not dialog.isHidden(), "Enter does not search again or close the window"
    dialog.close()


def test_the_right_click_has_the_panes_actions(setup, wait, qapp):
    page, ws, host, _ = setup
    dialog = page.search_dialog(page.left)
    searched(dialog, wait, "*")
    pick(dialog, "run.sh")
    texts = [text for text, _enabled in widgets.menu_entries(widgets.menu_of(dialog.table))]
    for wanted in ("Open", "Show in folder", "Copy path", "Copy to the other pane", "Delete…"):
        assert wanted in texts, texts
    assert texts.index("Open") < texts.index("Show in folder") < texts.index("Delete…")
    # Copy path: the full path, as the pane shows it.
    pick(dialog, "sub/b.txt", "a.txt")
    dialog.copy_paths()
    assert QGuiApplication.clipboard().text().splitlines() == [str(ws / "a.txt"), str(ws / "sub" / "b.txt")]
    # Copy to the other pane: out of a sandbox's folder as the agent's (never executable on the host).
    pick(dialog, "run.sh", "sub/b.txt")
    assert dialog.copy_to_other()
    finish(page, wait)
    assert (host / "b.txt").read_text() == "beta" and (host / "run.sh").exists()
    assert not os.access(host / "run.sh", os.X_OK), "an agent's file stays the agent's"
    dialog.close()


def test_delete_asks_with_the_full_paths_first(setup, wait, monkeypatch):
    page, ws, _host, _ = setup
    dialog = page.search_dialog(page.left)
    searched(dialog, wait, "*.txt")
    pick(dialog, "sub/b.txt")
    widgets.SHOWN.clear()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert not dialog.delete_hits()
    assert (ws / "sub" / "b.txt").exists() and str(ws / "sub" / "b.txt") in widgets.SHOWN[-1][1]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    pick(dialog, "sub/b.txt", "a.txt")
    assert dialog.delete_hits()
    finish(page, wait)
    assert not (ws / "sub" / "b.txt").exists() and not (ws / "a.txt").exists()
    assert str(ws / "a.txt") in widgets.SHOWN[-1][1] and str(ws / "sub" / "b.txt") in widgets.SHOWN[-1][1]
    assert wait(lambda: rows(dialog) == []), "the rows of what is gone go"
    dialog.close()


def test_open_a_file_in_the_editor_and_a_folder_in_the_pane(setup, wait):
    page, ws, _host, _ = setup
    dialog = page.search_dialog(page.left)
    searched(dialog, wait, "*")
    pick(dialog, "sub/b.txt")
    dialog.open_hit()
    assert page.editors and page.editors[-1].relative == "sub/b.txt"
    pick(dialog, "sub")
    dialog.open_hit()
    settle(page.left)
    assert page.left.relative == "sub"
    for window in list(page.editors):
        window.close()
    dialog.close()


def test_limits_and_what_was_not_searched_are_said(setup, wait, monkeypatch):
    page, ws, _host, _ = setup
    monkeypatch.setattr(file_search, "MAX_RESULTS", 2)
    dialog = page.search_dialog(page.left)
    assert len(searched(dialog, wait, "*")) == 2
    assert "Stopped at 2 results" in dialog.summary.text()
    monkeypatch.setattr(file_search, "MAX_RESULTS", 100)
    from sandbox_ai_app import files
    opened = files.Root._open_child_dir

    def refuse(self, parent, name):
        if name == "sub":
            raise PermissionError(13, "Permission denied")
        return opened(self, parent, name)
    monkeypatch.setattr(files.Root, "_open_child_dir", refuse)
    searched(dialog, wait, "*")
    assert "could not be read: sub" in dialog.summary.text()
    assert dialog.not_searched_button.isVisibleTo(dialog)
    assert "sub: Permission denied" in "\n".join(dialog.search.not_searched())
    dialog.close()


def test_stop_and_closing_cancel_it(setup, wait, monkeypatch):
    page, ws, _host, _ = setup
    real = file_search.Search.check

    def slow(self):
        import time
        time.sleep(0.02)
        return real(self)
    monkeypatch.setattr(file_search.Search, "check", slow)
    for i in range(100):
        (ws / f"f{i}.txt").write_text("x")
    dialog = page.search_dialog(page.left)
    dialog.names.setText("*")
    assert dialog.begin()
    assert dialog.running and not dialog.go_button.isEnabled()
    dialog.stop()
    assert wait(lambda: not dialog.running, 10)
    assert "Cancelled" in dialog.summary.text() and dialog.go_button.isEnabled()
    assert dialog.begin()
    search = dialog.search
    dialog.close()
    assert wait(lambda: search.done, 10) and search.stopped == "cancelled"


def test_a_broken_pattern_is_said(setup):
    page = setup[0]
    dialog = page.search_dialog(page.left)
    dialog.names.setText("[z-a]")
    assert not dialog.begin() and "Not a pattern" in dialog.state.text()
    dialog.close()


def test_a_remote_system_is_searched_over_its_connection(served, wait):
    page, home, ws, host = served
    left = page.left
    (home / "docs" / "sub").mkdir()
    (home / "docs" / "sub" / "deep.txt").write_text("deep alpha")
    dialog = page.search_dialog(left)
    assert str(home) in dialog.where.text() and "alice@fake" in dialog.where.text()
    assert searched(dialog, wait, "*.txt") == ["docs/a.txt", "docs/sub/deep.txt"]
    assert searched(dialog, wait, "", "alpha") == ["docs/a.txt", "docs/sub/deep.txt"]
    dialog.table.cellDoubleClicked.emit(row_of(dialog, "docs/sub/deep.txt"), 0)
    settle(left)
    assert left.path_text() == str(home / "docs" / "sub") and [e.name for e in left.chosen()] == ["deep.txt"]
    pick(dialog, "docs/a.txt")
    dialog.copy_paths()
    assert QGuiApplication.clipboard().text() == str(home / "docs" / "a.txt")
    dialog.close()


def test_another_place_in_the_pane_is_said_not_acted_on(setup, wait):
    page, ws, _host, _ = setup
    dialog = page.search_dialog(page.left)
    searched(dialog, wait, "b.txt")
    page.left.roots.setCurrentIndex(page.left.roots.findData("local:"))
    settle(page.left)
    dialog.table.cellDoubleClicked.emit(row_of(dialog, "sub/b.txt"), 0)
    assert "another place" in dialog.state.text()
    dialog.close()


def test_german(setup, wait):
    from sandbox_ai_app import i18n
    page = setup[0]
    i18n.set_language("de")
    try:
        dialog = page.search_dialog(page.left)
        assert dialog.go_button.text() == "Suchen" and dialog.windowTitle().startswith("Suchen")
        searched(dialog, wait, "*.txt")
        assert "gefunden" in dialog.state.text()
        dialog.close()
    finally:
        i18n.set_language("en")
