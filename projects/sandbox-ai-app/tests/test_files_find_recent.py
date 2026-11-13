"""The Files page's additions of 0.1.94: find and replace in the editor (and finding bytes in the hex view), the
editor's Save / Reload / Cancel, recently visited folders per pane, the free space of each pane's folder, and a folder's
size counted at once in Properties (progress, Stop, cancelled when the dialog closes). files.py first (no Qt).
Everything the tests change is below tmp_path."""
import os
import threading
import time

import pytest

from sandbox_ai_app import files


@pytest.fixture
def root(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("alpha\n")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.bin").write_bytes(b"\0\1\2")
    (ws / "outside").symlink_to(tmp_path)
    return files.Root(ws), ws


def test_free_space_is_of_the_open_folder_and_never_through_a_link(root):
    r, ws = root
    free, total = r.free_space("sub")
    info = os.statvfs(ws)
    unit = info.f_frsize or info.f_bsize
    assert total == info.f_blocks * unit and 0 <= free <= total
    assert r.free_space("")[1] == total
    with pytest.raises(files.Refused, match="symbolic links are not followed"):
        r.free_space("outside")
    with pytest.raises(ValueError):
        r.free_space("../..")


def test_measure_shows_its_progress_while_it_counts(root):
    r, ws = root
    progress = files.Progress()
    total = r.measure("", progress=progress)
    assert (progress.count, progress.bytes) == (total.files + total.directories - 1, total.bytes) == (4, 9), \
        "a.txt, sub, b.bin and the link counted as they come (the root's folder itself is no entry)"
    stop = threading.Event()
    stop.set()
    with pytest.raises(files.Cancelled) as caught:
        r.measure("", cancelled=stop.is_set, progress=files.Progress())
    assert caught.value.partial.bytes == 0


# -- the hex view's search, without a window --

def test_hex_needles_and_byte_matches():
    from sandbox_ai_app.file_editor import byte_matches, hex_needle
    assert hex_needle("7f 45 4c 46") == b"\x7fELF" == hex_needle("7F454c46") == hex_needle("7f:45,4c 46")
    assert hex_needle("7f4") is None and hex_needle("zz") is None and hex_needle("  ") is None
    data = b"ELF elf ElF\x00elf"
    assert byte_matches(data, b"elf") == [4, 12]
    assert byte_matches(data, b"elf", case=False) == [0, 4, 8, 12]
    assert byte_matches(data, b"elf", case=False, limit=2) == [0, 4]
    assert byte_matches(b"aaaa", b"aa") == [0, 2] and byte_matches(data, b"") == []


# -- the page --

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import file_editor, file_properties, files_page, widgets  # noqa: E402
from test_files_manager import editor_of  # noqa: E402
from test_files_page import finish, names, select, settle, setup  # noqa: E402,F401


def open_text(page, ws, wait, text="alpha beta Alpha alpha\nend\n", read_only=False):
    (ws / "a.txt").write_text(text)
    window = page.open_editor(page.left.root, "a.txt", str(ws / "a.txt"), read_only=read_only)
    editor_of(page, wait)
    return window


def find(window, text):
    bar = window.find_bar
    bar.open()
    bar.find.setText(text)  # (found as it is typed)
    return bar


def selected(window):
    cursor = window.text.textCursor()
    return cursor.selectionStart(), cursor.selectedText()


def test_find_goes_round_the_text_and_counts_the_matches(setup, wait):
    page, ws, host, _ = setup
    window = open_text(page, ws, wait)
    assert window.find_bar.isHidden()
    bar = find(window, "alpha")
    assert not bar.isHidden() and selected(window) == (0, "alpha") and bar.status.text() == "1 of 3"
    assert bar.search() and selected(window) == (11, "Alpha") and bar.status.text() == "2 of 3"
    assert bar.search() and selected(window) == (17, "alpha")
    assert bar.search() and selected(window) == (0, "alpha"), "round the end, to the top"
    assert bar.search(backward=True) and selected(window) == (17, "alpha"), "and back round"
    bar.case.setChecked(True)
    assert bar.status.text().endswith("of 2")
    assert bar.search() and selected(window) == (0, "alpha")
    bar.find.setText("gamma")
    assert bar.status.text() == "No match"
    assert window.text.toPlainText().startswith("alpha beta"), "finding changes nothing"
    assert not window.dirty
    # Enter in the find field finds the next one (Shift+Enter the one before) and presses no button of the window.
    bar.find.setText("alpha")
    QTest.keyClick(bar.find, Qt.Key.Key_Return)
    assert selected(window)[0] == 17 and window.isVisible() and not window.dirty
    QTest.keyClick(bar.find, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert selected(window)[0] == 0
    window.close()


def test_replace_one_and_all_in_one_step_to_undo(setup, wait):
    page, ws, host, _ = setup
    window = open_text(page, ws, wait)
    bar = find(window, "alpha")
    assert bar.replace_row.isHidden()
    bar.replace_toggle.setChecked(True)
    assert not bar.replace_row.isHidden()
    bar.replacement.setText("omega")
    assert bar.replace() and window.text.toPlainText() == "omega beta Alpha alpha\nend\n"
    assert selected(window) == (11, "Alpha"), "on to the next one"
    assert window.dirty and window.windowTitle().startswith("*")
    window.text.textCursor().clearSelection()
    assert bar.replace_all() == 2 and window.text.toPlainText() == "omega beta omega omega\nend\n"
    assert bar.status.text() == "Replaced 2"
    window.text.document().undo()
    assert window.text.toPlainText() == "omega beta Alpha alpha\nend\n", "Replace all: one step to undo"
    # A replacement that holds what is found is not found again.
    bar.find.setText("beta")
    bar.replacement.setText("beta beta")
    assert bar.replace_all() == 1 and "beta beta Alpha" in window.text.toPlainText()
    window.text.document().setModified(False)
    window.dirty = False
    window.close()


def test_nothing_is_replaced_where_it_cannot_be(setup, wait):
    page, ws, host, _ = setup
    window = open_text(page, ws, wait, read_only=True)  # F3: a viewer
    bar = find(window, "alpha")
    bar.replace_toggle.setChecked(True)
    bar.replacement.setText("x")
    assert not bar.replace_button.isEnabled() and not bar.replace_all_button.isEnabled()
    assert bar.replace() is False and bar.replace_all() == 0
    assert (ws / "a.txt").read_text().startswith("alpha") and window.text.toPlainText().startswith("alpha")
    assert window.cancel_button.text() == "Close", "a viewer is closed, not cancelled"
    window.close()


def test_the_hex_view_finds_text_and_hex_bytes(setup, wait):
    page, ws, host, _ = setup
    (ws / "prog").write_bytes(b"\x7fELF\x02\x01\x00\x00elf ELF")
    window = page.open_editor(page.left.root, "prog", str(ws / "prog"))
    editor_of(page, wait)
    assert window.view == "hex"
    bar = find(window, "elf")
    assert not bar.hex_bytes.isHidden() and not bar.replace_toggle.isEnabled(), "nothing replaced in the hex view"
    assert window.hex.found == (1, 3) and window.hex.cursor == 1 and bar.status.text() == "1 of 3"
    assert bar.search() and window.hex.found == (8, 3)
    bar.case.setChecked(True)
    assert window.hex.found == (8, 3) and bar.status.text() == "1 of 1"
    bar.hex_bytes.setChecked(True)
    bar.find.setText("02 01 00")
    assert window.hex.found == (4, 3) and window.hex.cursor == 4
    bar.find.setText("0")
    assert window.hex.found is None and bar.status.text().startswith("Not hex bytes")
    bar.find.setText("ff ff")
    assert bar.status.text() == "No match"
    assert not window.dirty and (ws / "prog").read_bytes().startswith(b"\x7fELF")
    window.close()


def test_the_keys_of_the_find_bar_and_escape_closes_it_before_the_window(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    window = open_text(page, ws, wait)
    window.activateWindow()
    window.text.setFocus()
    cursor = window.text.textCursor()
    cursor.setPosition(6)
    cursor.setPosition(10, cursor.MoveMode.KeepAnchor)  # "beta" selected: what Ctrl+F finds
    window.text.setTextCursor(cursor)
    QTest.keyClick(window.text, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    bar = window.find_bar
    assert not bar.isHidden() and bar.find.text() == "beta" and bar.find.hasFocus()
    bar.find.setText("alpha")  # (found from "beta" on)
    assert bar.status.text() == "2 of 3"
    QTest.keyClick(bar.find, Qt.Key.Key_F3)
    assert bar.status.text() == "3 of 3"
    QTest.keyClick(window.text, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
    assert bar.status.text() == "2 of 3"
    QTest.keyClick(bar.find, Qt.Key.Key_Escape)
    assert bar.isHidden() and window.isVisible(), "Escape closed the find bar, not the window"
    QTest.keyClick(window.text, Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
    assert not bar.isHidden() and bar.replace_toggle.isChecked() and not bar.replace_row.isHidden()
    bar.close_bar()
    # Escape in the text: Cancel - asked first about the changes not saved ("editor-discard").
    window.text.insertPlainText("changed ")
    asked = []
    monkeypatch.setattr(file_editor, "ask_first", lambda parent, action, *args, **kwargs: asked.append(action))
    QTest.keyClick(window.text, Qt.Key.Key_Escape)
    assert asked == ["editor-discard"] and window.isVisible(), "kept editing"
    window.close()


def test_save_reload_and_cancel(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    window = open_text(page, ws, wait)
    row = [window.save_button.text(), window.reload_button.text(), window.cancel_button.text()]
    assert row == ["Save (Ctrl+S)", "Reload", "Cancel"]
    assert all(not button.autoDefault() for button in window.findChildren(QtWidgets.QPushButton))
    window.text.selectAll()
    window.text.insertPlainText("not to be saved")
    asked = []

    def answer(parent, action, *args, **kwargs):
        asked.append(action)
        return len(asked) > 1  # no, then yes
    monkeypatch.setattr(file_editor, "ask_first", answer)
    window.cancel_button.click()
    assert asked == ["editor-discard"] and window.isVisible()
    window.cancel_button.click()
    assert asked == ["editor-discard"] * 2 and not window.isVisible()
    assert (ws / "a.txt").read_text() == "alpha beta Alpha alpha\nend\n", "cancelled: nothing saved"
    window = open_text(page, ws, wait)
    asked.clear()
    window.cancel_button.click()  # nothing changed: closed without a question
    assert asked == [] and not window.isVisible()


# -- the free space of each pane's folder --

def test_each_pane_shows_the_free_space_of_its_folder_and_again_after_a_copy(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    assert " free of " in page.left.space.text() and " free of " in page.right.space.text()
    asked = []

    def free_space(root, relative=""):
        asked.append((root.path, relative))
        return (1 << 30) + len(asked), 4 << 30
    monkeypatch.setattr(files.Root, "free_space", free_space)
    page.left.go("sub")
    settle(page.left)
    assert asked[-1] == (str(ws), "sub") and page.left.space.text() == "1 GiB free of 4 GiB"
    select(page.left, "b.txt")
    asked.clear()
    assert page.copy_selected(page.left)
    finish(page, wait)
    settle(page.left, page.right)
    assert (host / "b.txt").exists() and {relative for _, relative in asked} >= {"sub", files_page.host_relative(
        str(host))}, "both panes read it again after the copy"

    def unknown(root, relative=""):
        raise OSError("no statvfs here")
    monkeypatch.setattr(files.Root, "free_space", unknown)
    page.left.list()
    settle(page.left)
    assert page.left.space.text() == "" and names(page.left) == ["b.txt"], "listed all the same"


# -- recently visited folders --

class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None, kind=None):
        value = self.data.get(key, default)
        return value if kind is None or isinstance(value, kind) else default

    def set(self, key, value):
        self.data[key] = value


def test_the_host_pane_remembers_recent_folders_across_restarts(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    kept = FakeState({files_page.RECENT_KEY: ["/nowhere/../x", 7, "relative", str(host), str(host)]})
    monkeypatch.setattr(files_page.app_state, "current", lambda: kept)
    pane = files_page.Pane(page, "host")
    assert pane.recent == ["/nowhere/../x", str(host)], "only absolute paths, each once"
    folders = [host / name for name in ("one", "two", "three")]
    for folder in folders:
        folder.mkdir()
    right = page.right
    right.recent = []
    for folder in [*folders, folders[0]]:
        assert right.go_path(str(folder))
        settle(right)
    assert right.recent == [str(folders[0]), str(folders[2]), str(folders[1])], "most recent first, each once"
    assert kept.data[files_page.RECENT_KEY] == right.recent, "kept in the app's state"
    assert [key for _, key in right.recent_entries()] == [str(folders[2]), str(folders[1])], "not the one shown"
    assert right.recent_action in right.path.actions(), "the arrow at the end of the path bar"
    assert [b.accessibleName() for b in right.path.findChildren(QtWidgets.QToolButton) if b.defaultAction() is
            right.recent_action] == ["Recent"]
    page.show()
    page.activateWindow()
    right.path.setFocus()
    QTest.keyClick(right.path, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)  # (or a click on the arrow)
    assert wait(lambda: right.recent_menu.isVisible())
    right.recent_menu.hide()
    assert [action.text() for action in right.recent_menu.actions()] == [str(folders[2]), str(folders[1])]
    right.recent_menu.actions()[1].trigger()
    settle(right)
    assert right.path_text() == str(folders[1])
    for number in range(files_page.RECENT + 5):
        (host / f"f{number}").mkdir()
        right.go_path(str(host / f"f{number}"))
        settle(right)
    assert len(right.recent) == files_page.RECENT and right.recent[0] == str(host / f"f{files_page.RECENT + 4}")
    # The path bar's own checks: a path through a link in a sandbox's folder is refused.
    assert right.go_recent(str(ws / "escape")) is False
    assert "symbolic link" in page.message.text() and right.path_text() == str(host / f"f{files_page.RECENT + 4}")
    right.recent_menu.clear()
    right.recent = []
    right.fill_recent_menu()
    assert [(a.text(), a.isEnabled()) for a in right.recent_menu.actions()] == [("No folders visited yet", False)]


def test_the_sandbox_pane_offers_recent_folders_only_while_their_folder_is_listed(setup, wait, tmp_path):
    page, ws, host, _ = setup
    left = page.left
    left.go("sub")
    settle(left)
    left.go("")
    settle(left)
    assert left.recent[:2] == [(str(ws), ""), (str(ws), "sub")]
    assert left.recent_entries() == [(f"{ws}/sub", (str(ws), "sub"))]
    assert left.go_recent((str(ws), "sub")) and settle(left) is None and left.relative == "sub"
    # Another folder in the chooser: its recent folders go along, one of the first root's takes it back there.
    other = tmp_path / "other"
    (other / "deep").mkdir(parents=True)
    page.sai.roots.append(("session", "s1", str(other.resolve())))
    done = []
    page.refresh_roots(then=lambda: done.append(True))
    assert wait(lambda: done)
    settle(left)
    left.roots.setCurrentIndex(left.roots.findData(str(other.resolve())))
    left.go("deep")
    settle(left)
    assert left.go_recent((str(ws), "sub")) and settle(left) is None
    assert left.root.path == str(ws) and left.relative == "sub" and left.roots.currentData() == str(ws)
    # A link in a sandbox's folder: refused as when it is typed (the root's own folder is shown).
    assert left.go_recent((str(ws), "escape")) is True
    settle(left)
    assert left.relative == "" and "symbolic links are not followed" in page.message.text()
    # The other folder leaves the chooser: so do its recent folders.
    page.sai.roots.pop()
    done.clear()
    page.refresh_roots(then=lambda: done.append(True))
    assert wait(lambda: done)
    settle(left)
    assert all(root == str(ws) for root, _ in left.recent)
    assert left.go_recent((str(other.resolve()), "deep")) is False and "no longer in the list" in page.message.text()


# -- Properties: a folder's size, counted at once --

def test_properties_count_folders_at_once_and_sum_the_selection(setup, wait):
    page, ws, host, _ = setup
    select(page.left, "sub", "a.txt", "escape")
    dialog = page.properties(page.left)
    assert dialog.counting and not dialog.stop_button.isHidden()
    assert wait(lambda: not dialog.counting)
    assert dialog.size.text() == "9 B (9 bytes) in 3 file(s) and 1 folder(s)", \
        "a.txt (5) and sub/b.txt (4); the link counted, not followed"
    assert dialog.stop_button.isHidden() and dialog.count_button.isHidden()
    dialog.close()


def test_properties_show_the_progress_and_stop_and_closing_cancel_the_count(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    budgets = []

    def slow(self, fd, budget, result, depth, *_shown):
        budgets.append(budget)
        budget.entry(3)
        budget.progress.bytes += 2048
        while not budget.cancelled():
            time.sleep(0.01)
        budget.check()
    monkeypatch.setattr(files.Root, "_measure_folder", slow)
    select(page.left, "sub")
    dialog = page.properties(page.left)
    assert wait(lambda: budgets) and dialog.counting
    dialog.show_progress()
    assert dialog.size.text() == "Counting… 4 file(s) and folder(s), 2 KiB so far"
    assert dialog.stop()
    assert wait(lambda: not dialog.counting)
    assert dialog.size.text().startswith("at least ") and "Cancelled" in dialog.message.text()
    assert not dialog.count_button.isHidden(), "it may be counted again"
    dialog.count_button.click()
    assert dialog.counting and wait(lambda: len(budgets) == 2)
    dialog.close()
    assert wait(lambda: budgets[1].cancelled()), "closing the dialog cancels the count"


# -- New file (Shift+F4) --

def test_make_file_creates_an_empty_file_never_through_a_link(root):
    r, ws = root
    r.make_file("sub", "new.txt")
    made = ws / "sub" / "new.txt"
    assert made.read_bytes() == b"" and made.stat().st_mode & 0o777 == 0o600, "an agent's folder: only yours"
    umask = os.umask(0o022)
    os.umask(umask)
    host = files.Root(ws, sandbox=False)
    host.make_file("", "host.txt")
    assert (ws / "host.txt").stat().st_mode & 0o777 == 0o644 & ~umask, "the host's: 0644 less the umask"
    host.make_file("", "private.txt", untrusted=True)  # (a host folder in a sandbox's)
    assert (ws / "private.txt").stat().st_mode & 0o777 == 0o600
    (ws / "a.txt").write_text("kept")
    for name, why in (("a.txt", "already exists"), ("outside", "already exists"), ("a/b", "Not a name"),
                      ("..", "Not a name"), ("", "Not a name")):
        with pytest.raises(files.Refused, match=why):
            r.make_file("", name)
    assert (ws / "a.txt").read_text() == "kept"
    with pytest.raises(files.Refused, match="symbolic links are not followed"):
        r.make_file("outside", "planted.txt")  # a folder that is a link: never written through
    assert not (ws.parent / "planted.txt").exists()


def test_new_file_in_either_pane_is_selected_and_opened_in_the_editor(setup, wait, monkeypatch):
    from test_files_page import press
    page, ws, host, _ = setup
    answers = iter([("notes.md", True), ("todo.txt", True), ("notes.md", True), ("", False)])
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: next(answers))
    assert page.new_file_dialog(page.left)
    settle(page.left)
    assert (ws / "notes.md").read_bytes() == b"" and (ws / "notes.md").stat().st_mode & 0o777 == 0o600
    assert [entry.name for entry in page.left.chosen()] == ["notes.md"], "selected once listed"
    window = editor_of(page, wait)
    assert window.relative == "notes.md" and window.view == "text" and not window.read_only
    window.text.insertPlainText("# Notes\n")
    window.save()
    assert wait(lambda: not window.saving) and (ws / "notes.md").read_text() == "# Notes\n"
    window.close()
    # The host pane, by its key (Shift+F4; F4 alone still edits the current file).
    press(page.right, Qt.Key.Key_F4, Qt.KeyboardModifier.ShiftModifier)
    assert (host / "todo.txt").exists() and names(page.right) == ["todo.txt"]
    umask = os.umask(0o022)
    os.umask(umask)
    assert (host / "todo.txt").stat().st_mode & 0o777 == 0o644 & ~umask
    editor_of(page, wait).close()
    count = len(page.editors)
    assert page.new_file_dialog(page.left) is False and "already exists" in page.message.text()
    assert (ws / "notes.md").read_text() == "# Notes\n" and len(page.editors) == count, "nothing replaced or opened"
    assert page.new_file_dialog(page.left) is False, "no name: nothing"
    menu = page.menu(page.left, at_row=False)
    action = next(a for a in menu.actions() if a.text() == "New file…")
    assert action.isEnabled() and action.shortcut().toString() == "Shift+F4"
    assert page.buttons["touch"].text() == "New file (Shift+F4)"


# -- "." and ".." --

def test_dot_is_the_folder_shown_as_a_drop_target_and_for_properties(setup, wait, monkeypatch):
    from test_files_manager import drop_at, dragged, row_point, shown
    page, ws, host, _ = setup
    shown(page)
    taken = []
    # Onto the host pane's ".": into the folder it shows.
    dragged(page, page.left, ["a.txt"], lambda m: taken.append(drop_at(page.right, m, row_point(page.right, "."))))
    finish(page, wait)
    assert taken == [True] and (host / "a.txt").read_text() == "alpha"
    # Onto its own "." (where they are), or onto ".." at the top of the pane: nothing.
    dragged(page, page.left, ["run.sh"], lambda m: taken.append(drop_at(page.left, m, row_point(page.left, "."))))
    dragged(page, page.left, ["run.sh"], lambda m: taken.append(drop_at(page.left, m, row_point(page.left, ".."))))
    assert taken[1:] == [False, False] and not page.task.running
    # A right click on "." is the folder's menu: Paste into it, its Properties (its size counted at once).
    monkeypatch.setattr(page, "exec_menu", lambda menu_, where: None)  # (the menu itself, not shown)
    menu = page.context_menu(page.left, row_point(page.left, "."))
    texts = [action.text() for action in menu.actions() if action.text()]
    assert "Paste" in texts and "Delete…" not in texts and "Rename…" not in texts
    page.left.table.setCurrentCell(0, 0)  # "." current, nothing selected: Alt+Enter is the folder's Properties
    from test_files_page import press
    press(page.left, Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
    dialog = page.dialogs[-1]
    assert dialog.windowTitle() == f"Properties of {ws}"  # (its size counted at once: a small folder is done soon)
    assert wait(lambda: not dialog.counting) and " in 4 file(s) and 2 folder(s)" in dialog.size.text()
    dialog.close()


def test_dot_and_dotdot_are_never_copied_moved_deleted_or_dragged(setup, wait, monkeypatch):
    from test_files_manager import dragged
    page, ws, host, _ = setup
    left = page.left
    left.go("sub")
    settle(left)
    # A Shift+click from "." to the last row, as a human selects: only the entries.
    model = left.table.model()
    left.table.selectionModel().select(
        QtCore.QItemSelection(model.index(0, 0), model.index(model.rowCount() - 1, model.columnCount() - 1)),
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert [entry.name for entry in left.chosen()] == ["b.txt"]
    deleted = []
    monkeypatch.setattr(page, "delete_items", lambda chosen: deleted.append([name for name, _ in chosen.items]))
    assert page.delete_selected(left) is not False and deleted == [["b.txt"]]
    carried = []
    dragged(page, left, [".", "..", "b.txt"], lambda m: carried.append([name for name, _, _ in page.drag.items]))
    assert carried == [["b.txt"]], "a drag takes the entries only"
    select(left, ".", "..", "b.txt")
    assert page.copy_items(left) and [name for name, _, _ in page.clipboard.items] == ["b.txt"]
    select(left, ".", "..")
    left.table.setCurrentCell(1, 0)
    assert left.chosen() == [] and page.copy_selected(left) is False and page.copy_items(left) is False
    assert page.rename_dialog(left) is False and page.delete_selected(left) is False
    assert (ws / "sub" / "b.txt").exists() and (ws / "sub").is_dir()


# -- Home, Up and Refresh as icons --

def glyph_buttons(pane):
    return {button.accessibleName(): button for button in pane.findChildren(QtWidgets.QPushButton)
            if getattr(button, "glyph", None)}


def test_home_up_and_refresh_are_icons_named_for_screen_readers_and_follow_the_theme(setup, wait, qapp):
    from sandbox_ai_app import theme
    page, ws, host, _ = setup
    left, right = glyph_buttons(page.left), glyph_buttons(page.right)
    assert set(left) == {"Refresh", "Up"} and len([b for b in page.left.findChildren(QtWidgets.QPushButton)
                                                    if getattr(b, "glyph", None)]) == 2 + 1, "Refresh twice, Up"
    assert set(right) == {"Home", "Up", "Refresh"}
    for button in [*left.values(), *right.values()]:
        assert button.text() == "" and not button.icon().isNull() and button.toolTip(), button.accessibleName()
        assert button.icon().availableSizes(), "drawn"
        assert {size.width() for size in button.icon().availableSizes()} >= {16, 32}, "sharp on a HiDPI screen too"
    assert "Alt+Up" in right["Up"].toolTip() and "Alt+Home" in right["Home"].toolTip()
    assert "Ctrl+R" in right["Refresh"].toolTip()
    # The keys: Alt+Up in either pane, Alt+Home in the host's.
    page.resize(1200, 600)
    page.show()
    page.activateWindow()
    qapp.processEvents()
    page.right.table.setFocus()
    qapp.processEvents()
    QTest.keyClick(page.right.table, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    settle(page.right)
    assert page.right.path_text() == str(host.parent)
    QTest.keyClick(page.right.path, Qt.Key.Key_Home, Qt.KeyboardModifier.AltModifier)
    settle(page.right)
    assert page.right.path_text() == os.path.realpath(os.path.expanduser("~"))
    right["Up"].click()
    settle(page.right)
    assert page.right.path_text() == os.path.dirname(os.path.realpath(os.path.expanduser("~")))
    # Another look: drawn again in its text colour.
    before = right["Home"].icon().pixmap(16, 16).toImage()
    try:
        theme.apply("dark", qapp)
        qapp.processEvents()
        assert wait(lambda: right["Home"].icon().pixmap(16, 16).toImage() != before)
        dark = right["Home"].icon().pixmap(16, 16).toImage()
        theme.apply("light", qapp)
        qapp.processEvents()
        assert wait(lambda: right["Home"].icon().pixmap(16, 16).toImage() != dark)
    finally:
        theme.apply("system", qapp)
    page.hide()


# -- the columns: every edge can be dragged, kept per pane --

def drag_edge(header, column, by):
    """Drag the right edge of COLUMN's title by BY pixels, as the human does."""
    x = header.sectionViewportPosition(column) + header.sectionSize(column) - 1
    y = header.height() // 2
    QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QtCore.QPoint(x, y))
    for step in range(1, 6):
        QTest.mouseMove(header.viewport(), QtCore.QPoint(x + by * step // 5, y))
    QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QtCore.QPoint(x + by, y))


def test_every_column_can_be_dragged_survives_a_folder_change_and_is_kept_per_pane(setup, wait, tmp_path, qapp):
    """User report: the lines between the columns were there, but the columns could not be resized - the Name column
    (the first one) was in Stretch mode, and a column is sized by its right edge."""
    from sandbox_ai_app import state as app_state
    from test_files_manager import shown
    page, ws, host, _ = setup
    shown(page)
    kept = app_state.State(tmp_path / "state.json", delay=10 ** 6)
    kept.table("files", page.left.table)
    kept.table("files-right-table", page.right.table)
    for pane in (page.left, page.right):
        header = pane.table.horizontalHeader()
        assert all(header.sectionResizeMode(column) == QtWidgets.QHeaderView.ResizeMode.Interactive
                   for column in range(header.count())) and not header.stretchLastSection()
    header = page.left.table.horizontalHeader()
    name = header.sectionSize(0)
    drag_edge(header, 0, -60)
    assert header.sectionSize(0) == name - 60 and page.left.table.user_sized
    page.left.go("sub")
    settle(page.left)
    assert header.sectionSize(0) == name - 60, "a folder change leaves the human's widths alone"
    page.left.go("")
    settle(page.left)
    assert header.sectionSize(0) == name - 60
    # A double click on an edge fits that column to its content (and is the human's width too).
    right = page.right.table.horizontalHeader()
    right.resizeSection(5, 300)
    x = right.sectionViewportPosition(5) + right.sectionSize(5) - 1
    QTest.mouseDClick(right.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                      QtCore.QPoint(x, right.height() // 2))
    assert right.sectionSize(5) < 300 and page.right.table.user_sized
    owner = right.sectionSize(5)
    # Kept across restarts, per pane.
    kept.flush()
    again = app_state.State(tmp_path / "state.json")
    assert again.data["tables"]["files"]["sized"] and again.data["tables"]["files-right-table"]["sized"]
    page2 = files_page.FilesPage(page.sai)
    page2.resize(1300, 700)
    page2.show()
    settle(page2.left, page2.right)
    again.table("files", page2.left.table)
    again.table("files-right-table", page2.right.table)
    qapp.processEvents()
    assert page2.left.table.horizontalHeader().sectionSize(0) == name - 60
    assert page2.right.table.horizontalHeader().sectionSize(5) == owner
    page2.hide()
    page.hide()
