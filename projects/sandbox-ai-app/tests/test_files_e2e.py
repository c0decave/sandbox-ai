"""End to end: the real Files page in the app's main window (offscreen), driven by keys, clicks and the mouse as a human
would - a sandbox's folder opened, "." and "..", the Recent menu, New file, find and replace in the editor and Save, the
Markdown viewer and its links, the free space, a column dragged wider and kept in the app's state file. Everything it
changes is below tmp_path."""
import json

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint, QUrl, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import file_editor, state, widgets  # noqa: E402
from test_files_page import settle  # noqa: E402
from test_ui import env, fake_run, window  # noqa: E402,F401


def press(pane, key, modifiers=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(pane.table, key, modifiers)
    settle(pane.page.left, pane.page.right)


def current_to(pane, name):
    pane.table.setFocus()
    pane.table.setCurrentCell(widgets.find_row(pane.table, name), 0)


def rows(pane):
    settle(pane)
    return [pane.table.item(r, 0).text() for r in range(pane.table.rowCount())]


def newest_editor(page, wait):
    assert wait(lambda: page.editors and not page.editors[-1].loading, 10)
    return page.editors[-1]


def test_the_files_page_end_to_end(window, env, wait, qapp, monkeypatch):
    fake_run(env)
    project = env / "project"
    (project / "docs").mkdir()
    (project / "docs" / "readme.md").write_text("# Read me\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [x] done\n\n"
                                                "[next](next.md) [web](https://example.org/page)\n\n"
                                                "![remote](https://example.org/x.png)\n")
    (project / "docs" / "next.md").write_text("# Next\n")
    opened = []
    monkeypatch.setattr(file_editor, "open_url", lambda url: opened.append(url.toString()))
    window.resize(1400, 900)
    window.show()
    window.show_page("files")
    page = window.files
    left = page.left
    page.refresh_roots()
    assert wait(lambda: not page.roots_reading)
    left.roots.setCurrentIndex(left.roots.findData(str(project.resolve())))
    settle(left, page.right)
    # "." and ".." lead the folder; ".." is greyed out at the top of the sandbox's folder.
    assert rows(left)[:2] == [".", ".."] and "docs" in rows(left)
    assert not left.table.model().flags(left.table.model().index(1, 0)) & Qt.ItemFlag.ItemIsEnabled
    assert " free of " in left.space.text()
    # Into docs/ by Enter, out by Enter on "..", back by the Recent menu.
    current_to(left, "docs")
    press(left, Qt.Key.Key_Return)
    assert left.relative == "docs" and rows(left)[:2] == [".", ".."]
    current_to(left, "..")
    press(left, Qt.Key.Key_Return)
    assert left.relative == ""
    left.fill_recent_menu()
    action = next(a for a in left.recent_menu.actions() if a.text().endswith("/docs"))
    action.trigger()
    settle(left)
    assert left.relative == "docs"
    # New file (Shift+F4): asked for its name, opened in the editor; typed, found, replaced, saved (Ctrl+S).
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("todo.txt", True))
    press(left, Qt.Key.Key_F4, Qt.KeyboardModifier.ShiftModifier)
    editor = newest_editor(page, wait)
    assert editor.relative == "docs/todo.txt" and (project / "docs" / "todo.txt").read_text() == ""
    assert [entry.name for entry in left.chosen()] == ["todo.txt"]
    editor.activateWindow()
    qapp.processEvents()
    editor.text.setFocus()
    QTest.keyClicks(editor.text, "hello world, world")
    QTest.keyClick(editor.text, Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
    bar = editor.find_bar
    bar.find.setText("world")
    assert bar.status.text() == "1 of 2"
    bar.replacement.setText("there")
    bar.replace_all_button.click()
    assert editor.text.toPlainText() == "hello there, there"
    QTest.keyClick(bar.find, Qt.Key.Key_Escape)
    assert bar.isHidden() and editor.isVisible()
    QTest.keyClick(editor.text, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert wait(lambda: not editor.saving and not editor.dirty, 10)
    assert (project / "docs" / "todo.txt").read_text() == "hello there, there"
    editor.cancel_button.click()  # (nothing unsaved: closed at once)
    assert not editor.isVisible()
    # The Markdown viewer: Enter on readme.md shows it rendered; its links are asked, opened in a viewer, never followed.
    current_to(left, "readme.md")
    press(left, Qt.Key.Key_Return)
    viewer = newest_editor(page, wait)
    assert viewer.view == "markdown" and viewer.markdown.outline.item(0).text() == "Read me"
    assert any(type(f).__name__ == "QTextTable" for f in viewer.markdown.browser.document().rootFrame().childFrames())
    assert viewer.markdown.images["https://example.org/x.png"].width() == 420, "a remote image: a placeholder"
    widgets.SHOWN.clear()
    viewer.markdown.clicked(QUrl("https://example.org/page"))
    assert [title for title, *_ in widgets.SHOWN] == ["Open the link?"] and opened == ["https://example.org/page"]
    viewer.markdown.clicked(QUrl("next.md"))
    other = newest_editor(page, wait)
    assert other.relative == "docs/next.md" and other.view == "markdown" and other.read_only
    other.close()
    viewer.close()
    # Paths for a session: relative (Ctrl+Shift+C) and as the agent sees them (the run's workspace is its /work).
    assert wait(lambda: page.sandbox_paths.get(str(project.resolve())) == "/work")
    widgets.select_keys(left.table, ["readme.md", "next.md"])
    press(left, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert sorted(QtWidgets.QApplication.clipboard().text().split("\n")) == ["docs/next.md", "docs/readme.md"]
    menu = page.menu(left)
    next(a for a in menu.actions() if a.text() == "Copy path in the sandbox").trigger()
    assert sorted(QtWidgets.QApplication.clipboard().text().split("\n")) == ["/work/docs/next.md",
                                                                           "/work/docs/readme.md"]
    # A column dragged wider: kept through a change of folder and in the app's state file.
    header = left.table.horizontalHeader()
    width = header.sectionSize(1)
    x = header.sectionViewportPosition(1) + width - 1
    y = header.height() // 2
    QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, y))
    for step in range(1, 6):
        QTest.mouseMove(header.viewport(), QPoint(x + 8 * step, y))
    QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x + 40, y))
    assert header.sectionSize(1) == width + 40
    current_to(left, "..")
    press(left, Qt.Key.Key_Return)
    assert left.relative == "" and header.sectionSize(1) == width + 40
    window.state.flush()
    kept = json.loads(state.state_file().read_text())["tables"]["files"]
    assert kept["sized"] is True
