"""Review pass 18 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A list sorted with thousands of entries selected held up the window for seconds (Qt asked every cell's flags), and the
selected rows were found by asking every cell's flags again (each Copy, F5 or drag slow). The hex view kept a set of
every changed offset (350 MB for a text of 5 MiB typed into at its start). A code block's colours were made anew for each of its lines, and the live preview rendered a big text at every pause of the typing. F3 and F4 entered a
link to a folder (on the folder itself they say it is one); on macOS the texts said Ctrl where the key is Cmd, and
Cmd+Backspace went up a folder instead of deleting, as in the Finder. Everything changed is below tmp_path."""
import cProfile
import html
import pstats
import tracemalloc

import pytest

from sandbox_ai_app import files

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import file_editor, files_page, markdown_view, widgets  # noqa: E402
from sandbox_ai_app.file_editor import EditorWindow  # noqa: E402
from test_files_page import FakeSai, finish, names, press, select, settle, setup  # noqa: E402,F401


def editor(tmp_path, wait, data, name="t.txt", mode="auto"):
    (tmp_path / name).write_bytes(data)
    window = EditorWindow(None, files.Root(tmp_path), name, str(tmp_path / name), mode)
    assert wait(lambda: not window.loading, 10)
    return window


def big_folder(page, host, count):
    folder = host / "big"
    folder.mkdir()
    for number in range(count):
        (folder / f"file-{number:05d}.txt").write_bytes(b"x" * (number % 7))
    page.right.go_path(str(folder))
    settle(page.right)
    return page.right


# -- thousands selected --

def flag_calls(work):
    """How often WORK() made Qt ask a pane's model for a cell's flags (a call into Python each: counted by cProfile -
    PySide calls the method it found first, a monkeypatched one is never seen)."""
    profile = cProfile.Profile()
    profile.enable()
    try:
        work()
    finally:
        profile.disable()
    return sum(counts[1] for (_file, _line, name), counts in pstats.Stats(profile).stats.items() if name == "flags")


def test_sorting_with_thousands_selected_asks_no_cell(setup):
    """Every entry selected, then a click on Size and on Name: Qt kept the selection one index a cell while it sorted
    and asked every cell's flags twice over (56 calls into Python a row: 20,000 rows took 2 to 5 s a click). One index
    a row now (VerticalSortHint), the selection still whole."""
    page, ws, host, _ = setup
    pane = big_folder(page, host, 2000)
    pane.select_all()
    asked = flag_calls(lambda: (pane.table.sortByColumn(2, Qt.SortOrder.DescendingOrder),
                                pane.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)))
    assert asked < 100, asked
    assert len(pane.table.selectionModel().selection()) <= 2 and len(pane.chosen()) == 2000
    assert [entry.name for entry in pane.chosen()][:2] == ["file-00000.txt", "file-00001.txt"]


def test_the_selected_rows_are_found_without_asking_every_cell(setup):
    """The selected rows came from Qt's selectedRows(), which asks for the flags of every cell - 14 calls into Python a
    row, 280,000 for 20,000 rows: more than half a second at every Copy, F5, drag or Delete. The first cell of each row
    now; "." and ".." (never selectable) are still never among them."""
    page, ws, host, _ = setup
    pane = big_folder(page, host, 2000)
    pane.select_all()
    found = []
    asked = flag_calls(lambda: found.extend(widgets.selected_rows(pane.table)))
    assert len(found) == 2000 and 0 not in found and 1 not in found
    assert asked <= 2 * len(found) + 10, asked
    # (kept so) a range over "." and ".." still leaves them out; a row deselected is left out
    model, selection = pane.table.model(), pane.table.selectionModel()
    selection.select(model.index(0, 0), selection.SelectionFlag.Select | selection.SelectionFlag.Rows)
    selection.select(model.index(5, 0), selection.SelectionFlag.Deselect | selection.SelectionFlag.Rows)
    rows = widgets.selected_rows(pane.table)
    assert 0 not in rows and 1 not in rows and 5 not in rows and len(rows) == 1999


# -- the hex view's marks --

def test_bytes_shifted_by_a_character_typed_are_marked_without_a_set_of_millions(tmp_path, wait, qapp):
    """A character typed at the start of a text, then Hex: every byte after it differs, and each offset was kept in a
    set (about 70 bytes an offset: 350 MB for a text of 5 MiB, and half a second in Python). One byte each now."""
    data = b"0123456789abcde\n" * (1 << 16)  # 1 MiB
    window = editor(tmp_path, wait, data)
    cursor = window.text.textCursor()
    cursor.setPosition(0)
    cursor.insertText("x")
    tracemalloc.start()
    try:
        assert window.switch("hex")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 24 << 20, f"{peak >> 20} MiB"
    edited = window.hex.edited
    assert 0 in edited and len(edited) == len(data), "(kept so) every byte after the one typed is marked"
    assert window.hex.data()[:2] == b"x0"
    window.dirty = False
    window.close()


def test_the_changed_bytes_are_those_that_differ():
    """(kept so) the offsets marked are exactly those that differ, as far as both go; typing marks one more."""
    original = bytes(range(256)) * 4
    data = bytearray(original)
    for at in (0, 5, 255, 1023):
        data[at] ^= 0xFF
    found = file_editor.changed_bytes(bytes(data) + b"more", original)
    assert found == {0, 5, 255, 1023} and sorted(found) == [0, 5, 255, 1023] and len(found) == 4
    assert 6 not in found and -1 not in found and 5000 not in found
    copy = found.copy()
    copy.add(2000)
    assert 2000 in copy and 2000 not in found and copy != found
    assert not file_editor.changed_bytes(b"", original) and file_editor.changed_bytes(original, original) == set()


# -- the Markdown view --

def test_a_code_blocks_colours_are_made_once_a_rendering(qapp, monkeypatch):
    """The colours of a code block were made anew for each of its lines (four formats and their theme look-ups): a code
    block of 150,000 lines held up the window for 4 s, most of it there."""
    view = markdown_view.MarkdownView(None)
    looked = []
    original = markdown_view.theme.color
    monkeypatch.setattr(markdown_view.theme, "color", lambda name: looked.append(name) or original(name))
    view.render("```python\n" + "def f(x): return x + 1  # one\n" * 2000 + "```\n")
    assert 0 < len(looked) < 20, len(looked)
    looked.clear()
    view.render("```\nx = 'a'\n```\n")  # (a new rendering: the look of now)
    assert looked


def test_the_live_preview_waits_longer_after_a_slow_rendering(qapp, monkeypatch):
    """The preview rendered the whole text after every pause of RENDER_DELAY in the typing: a text whose rendering
    takes a second or more left the typing stuck most of the time. It waits at least twice what a rendering took."""
    view = markdown_view.MarkdownView(None, outline=False)
    view.render("# Title\n\ntext\n")
    assert view.timer.interval() == markdown_view.RENDER_DELAY, "(kept so) a quick rendering: the pause as before"
    clock = iter([100.0, 101.5])
    monkeypatch.setattr(markdown_view.time, "monotonic", lambda: next(clock, 101.5))
    view.render("# Title\n\nmore text\n")
    assert view.timer.interval() >= 3000


# -- F3 and F4 on a link to a folder --

def test_view_and_edit_on_a_link_to_a_folder_say_it_is_a_folder(setup, wait):
    """F3 and F4 (and View in the context menu) on a host's link to a folder entered the folder; on the folder itself
    they say "... is a folder". As on the folder now; Enter (Open) still enters it."""
    page, ws, host, outside = setup
    (host / "there").symlink_to(outside)
    (host / "here").mkdir()
    pane = page.right
    pane.list()
    settle(pane)
    for name in ("here", "there"):
        for key in (Qt.Key.Key_F3, Qt.Key.Key_F4):
            pane.table.setCurrentCell(widgets.find_row(pane.table, name), 0)
            select(pane, name)
            page.message.setText("")
            press(pane, key)
            assert pane.path_text() == str(host), (name, key)
            assert "is a folder" in page.message.text(), (name, key, page.message.text())
    page.message.setText("")
    pane.open_entry(pane.by_name["there"], read_only=True, enter=False)  # View in the context menu
    assert pane.path_text() == str(host) and "is a folder" in page.message.text()
    assert not page.editors
    pane.table.setCurrentCell(widgets.find_row(pane.table, "there"), 0)
    press(pane, Qt.Key.Key_Return)
    assert pane.path_text() == str(outside.resolve()), "(kept so) Enter opens it"


# -- macOS --

def test_the_texts_name_the_keys_as_the_system_writes_them(setup, wait, tmp_path, monkeypatch):
    """Save (Ctrl+S), Refresh's "(Ctrl+R)", "Paste (Ctrl+V) ..." and "(Ctrl+C, Ctrl+X)" said Ctrl on macOS too, where
    the keys are Cmd+S, Cmd+R, Cmd+V (Qt's Ctrl is the Cmd key there): they are written as the system writes them."""
    monkeypatch.setattr(file_editor, "key_text", lambda key: "<key>")
    monkeypatch.setattr(files_page, "key_text", lambda key: "<key>")
    page, ws, host, _ = setup
    window = editor(tmp_path, wait, b"text\n", name="s.txt")
    assert window.save_button.text() == "Save (<key>)"
    window.close()
    page.left.list()
    settle(page.left)
    select(page.left, "a.txt")
    assert page.copy_items(page.left) and "Paste (<key>)" in page.message.text()
    page.clipboard = None
    QtWidgets.QApplication.clipboard().clear()
    assert not page.paste(page.left) and "(<key>, <key>)" in page.message.text()
    pane = files_page.Pane(page, "host")
    tips = [html.unescape(widget.toolTip()) for widget in pane.findChildren(QtWidgets.QPushButton)]
    assert any("List the folder again (<key>)" in tip for tip in tips), tips
    assert not any("Ctrl" in tip for tip in tips)
    pane.deleteLater()


def test_cmd_backspace_deletes_on_macos(setup, wait, monkeypatch):
    """macOS: Cmd+Backspace (Qt's Ctrl+Backspace there) deletes what is selected, as in the Finder - asked first, as F8
    and Delete; it went up a folder (as Backspace). Elsewhere Ctrl+Backspace is not Delete."""
    from PySide6.QtGui import QKeySequence
    page, ws, host, _ = setup
    left = page.left
    (ws / "sub" / "gone.txt").write_text("x")
    monkeypatch.setattr(files_page, "MACOS", False)
    left.go("sub")
    select(left, "gone.txt")
    press(left, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
    assert (ws / "sub" / "gone.txt").exists() and widgets.SHOWN == []
    monkeypatch.setattr(files_page, "MACOS", True)
    left.go("sub")
    select(left, "gone.txt")
    assert page.is_key(Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier), "before any shortcut of the window"
    press(left, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
    finish(page, wait)
    assert [title for title, *_ in widgets.SHOWN] == ["Delete?"]
    assert not (ws / "sub" / "gone.txt").exists() and left.relative == "sub", "deleted, not gone up"
    select(left, "b.txt")
    delete = [action for action in page.menu(left).actions() if action.text() == "Delete…"]
    assert delete and delete[0].shortcut() == QKeySequence("Ctrl+Backspace")
