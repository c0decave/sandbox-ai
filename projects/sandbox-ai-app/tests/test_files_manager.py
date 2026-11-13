"""The file manager's own features (user feedback): "." and ".." in every folder of both panes, columns that stay put
when the folder changes, drag and drop (between the panes, onto a folder, from and to other programs), a built-in text
and hex editor (double click), the context menu with the clipboard (Copy, Cut, Paste), Copy path and Properties.
files.py's read_file, write_file and measure first (no Qt). Everything the tests change is below tmp_path."""
import os
import stat
import sys

import pytest

from sandbox_ai_app import files


# -- files.py: the editor's reads and writes, a folder's size --

@pytest.fixture
def root(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("alpha\n")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.bin").write_bytes(b"\0\1\2")
    (ws / "outside.txt").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("the host's own")
    return files.Root(ws), ws


def test_read_file_reads_regular_files_only_and_says_when_it_stopped(root, tmp_path):
    r, ws = root
    data = r.read_file("a.txt")
    assert data.data == b"alpha\n" and not data.truncated and data.size == 6
    assert r.read_file("a.txt", limit=3).data == b"alp" and r.read_file("a.txt", limit=3).truncated
    with pytest.raises(files.Refused, match="symbolic link"):
        r.read_file("outside.txt")
    os.mkfifo(ws / "pipe")
    with pytest.raises(files.Refused, match="not a regular file"):
        r.read_file("pipe")  # (opened without blocking: never hangs)
    with pytest.raises(ValueError):
        r.read_file("../secret")
    assert files.text_of(b"caf\xc3\xa9") == "café" and files.text_of(b"a\0b") is None
    assert files.text_of(b"\xff\xfe") is None
    assert files.text_of(b"ok \xc3", truncated=True) == "ok ", "a character cut at the limit"


def test_a_file_written_while_it_is_read_is_read_again_or_never_saved_over_unasked(root, monkeypatch):
    """Review: the identity a save checks was taken after the read - a file the agent wrote in place meanwhile was
    read half old, half new, and a save replaced the agent's write with that mix without a question."""
    r, ws = root
    (ws / "log.txt").write_bytes(b"old " * 100)
    real_read, writes = os.read, {"left": 1}

    def read(fd, size):
        chunk = real_read(fd, min(size, 16))
        if writes["left"]:  # the agent appends while the first chunk is read
            writes["left"] -= 1
            with open(ws / "log.txt", "ab") as stream:
                stream.write(b"new!")
        return chunk
    monkeypatch.setattr(files.os, "read", read)
    data = r.read_file("log.txt")
    monkeypatch.setattr(files.os, "read", real_read)
    assert data.data == b"old " * 100 + b"new!", "read again once it held still"
    assert data.identity == files._identity((ws / "log.txt").stat())
    # Never still: the identity of before the read - a save asks first.
    writes["left"] = 10 ** 9  # (a 1000 bytes read at most: every attempt sees it change)
    monkeypatch.setattr(files.os, "read", read)
    data = r.read_file("log.txt", limit=1000)
    monkeypatch.setattr(files.os, "read", real_read)
    with pytest.raises(files.Changed):
        r.write_file("log.txt", data.data, expected=data.identity)


def test_write_file_replaces_never_writes_through_and_keeps_the_mode(root, tmp_path):
    r, ws = root
    (ws / "run.sh").write_text("#!/bin/sh\n")
    (ws / "run.sh").chmod(0o4755 if os.getuid() else 0o755)
    first = r.read_file("run.sh")
    os.link(ws / "run.sh", tmp_path / "hardlink")  # the agent's hard link to a file of the host's
    identity = r.write_file("run.sh", b"#!/bin/sh\necho new\n", expected=first.identity)
    assert (ws / "run.sh").read_text() == "#!/bin/sh\necho new\n"
    assert stat.S_IMODE((ws / "run.sh").stat().st_mode) == 0o755, "the bits stay, set-ID ones do not"
    assert (tmp_path / "hardlink").read_text() == "#!/bin/sh\n", "never written through a hard link"
    assert identity == files._identity((ws / "run.sh").stat())
    # Changed on disk since it was read: refused, unless asked (expected None).
    (ws / "run.sh").write_text("the agent wrote this")
    with pytest.raises(files.Changed):
        r.write_file("run.sh", b"mine", expected=identity)
    assert (ws / "run.sh").read_text() == "the agent wrote this"
    r.write_file("run.sh", b"mine")
    assert (ws / "run.sh").read_bytes() == b"mine"
    # A link there is never written through; nothing is left behind.
    with pytest.raises(files.Refused, match="symbolic link"):
        r.write_file("outside.txt", b"x")
    assert (tmp_path / "secret").read_text() == "the host's own"
    with pytest.raises(files.Refused):
        r.write_file("sub", b"x")
    r.write_file("new.txt", b"fresh")
    assert (ws / "new.txt").read_bytes() == b"fresh"
    assert not [name for name in os.listdir(ws) if name.endswith(".part")]


def test_measure_counts_without_following_links_and_stops_at_its_budget(root):
    r, ws = root
    total = r.measure("")
    assert (total.files, total.directories, total.bytes) == (3, 2, 9), "a.txt, b.bin and the link; ws and sub"
    assert r.measure("sub").bytes == 3 and r.measure("a.txt").files == 1
    with pytest.raises(files.Refused) as caught:
        r.measure("", entries=2)
    assert caught.value.partial.files + caught.value.partial.directories <= 3


# -- the page --

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QGuiApplication, QKeyEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
import shiboken6  # noqa: E402

from sandbox_ai_app import file_editor, files_page, widgets  # noqa: E402
from test_files_page import finish, names, press, select, settle, setup  # noqa: E402,F401


def rows(pane):
    settle(pane)
    return [pane.table.item(r, 0).text() for r in range(pane.table.rowCount())]


def shown(page):
    page.resize(1300, 700)
    page.show()
    QtWidgets.QApplication.processEvents()
    settle(page.left, page.right)
    return page


def test_dot_and_dotdot_lead_every_folder_and_are_never_entries(setup, wait):
    """User report: "." and ".." were missing - ".." showed only below the top of a pane, and the sandbox pane opens
    at its top. Both lead every folder of both panes now; at the top ".." is greyed out and says why."""
    page, ws, host, _ = setup
    left, right = page.left, page.right
    model = left.table.model()
    assert rows(left)[:2] == [".", ".."] and rows(right)[:2] == [".", ".."] and right.path_text() == str(host)
    up = model.index(1, 0)
    assert not model.flags(up) & Qt.ItemFlag.ItemIsEnabled, "the top of the sandbox's folder: greyed out"
    assert "is the top of this pane" in model.data(up, Qt.ItemDataRole.ToolTipRole)
    left.open_row(1)
    assert left.relative == "" and "is the top of this pane" in page.message.text(), "never above the top"
    left.go("sub")
    assert rows(left) == [".", "..", "b.txt"]
    assert model.flags(model.index(1, 0)) & Qt.ItemFlag.ItemIsEnabled
    assert not model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsSelectable
    assert not model.flags(model.index(1, 0)) & Qt.ItemFlag.ItemIsSelectable
    left.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    assert rows(left)[:2] == [".", ".."], "first however the list is sorted"
    press(left, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert [entry.name for entry in left.chosen()] == ["b.txt"] and \
        [widgets.row_key(left.table, row) for row in widgets.selected_rows(left.table)] == ["b.txt"], \
        "Ctrl+A: every entry, never . or .."
    left.table.selectAll()  # (even Qt's own select all: neither is ever chosen)
    assert [entry.name for entry in left.chosen()] == ["b.txt"]
    left.table.clearSelection()
    left.open_row(0)  # a double click on ".": listed again, still here
    settle(left)
    assert left.relative == "sub"
    left.open_row(1)  # a double click on ".."
    settle(left)
    assert left.relative == "" and [entry.name for entry in left.chosen()] == ["sub"], "the folder come from"
    assert left.table.currentRow() == widgets.find_row(left.table, "sub")
    left.go("sub")
    press(left, Qt.Key.Key_Backspace)
    assert left.relative == ""
    left.go("sub")
    settle(left)
    left.table.setCurrentCell(1, 0)
    press(left, Qt.Key.Key_Return)
    assert left.relative == ""
    right.go("")
    assert right.path_text() == "/" and rows(right)[:2] == [".", ".."], "at / too, .. greyed out"
    assert not right.table.model().flags(right.table.model().index(1, 0)) & Qt.ItemFlag.ItemIsEnabled


def test_the_columns_and_the_list_stay_put_when_the_folder_changes(setup, wait):
    """User report: the panes seemed to shift for a moment at every change of folder. The vertical scroll bar is
    always there (its coming and going changed the list's width and so the columns), the columns start at widths
    fitted to typical rows, a new folder is shown from its top in one paint."""
    page, ws, host, _ = setup
    for index in range(120):
        (ws / "sub" / f"file-{index:03}.txt").write_text("x" * index)
    shown(page)
    left = page.left
    header = left.table.horizontalHeader()
    assert left.table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
    before = [header.sectionSize(c) for c in range(header.count())], left.table.viewport().width()
    seen = set()
    for folder in ("sub", "", "sub", ""):
        left.go(folder)
        settle(left)
        QtWidgets.QApplication.processEvents()
        seen.add(((tuple(header.sectionSize(c) for c in range(header.count()))), left.table.viewport().width()))
    assert seen == {(tuple(before[0]), before[1])}, "no column and no width changed"
    left.go("sub")
    settle(left)
    left.table.verticalScrollBar().setValue(40)
    left.list()  # the same folder again (after a copy, Refresh): where it was
    settle(left)
    assert left.table.verticalScrollBar().value() == 40
    left.up()
    settle(left)
    left.go("sub")
    settle(left)
    assert left.table.verticalScrollBar().value() == 0, "another folder: from its top"
    page.hide()


# -- drag and drop --

def drop_at(pane, mime, point, modifiers=Qt.KeyboardModifier.NoModifier):
    """Drag MIME over PANE's list and drop it at POINT; whether the drop was taken."""
    viewport = pane.table.viewport()
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    enter = QDragEnterEvent(QPointF(point), actions, mime, Qt.MouseButton.LeftButton, modifiers)
    QtWidgets.QApplication.sendEvent(viewport, enter)
    move = QDragMoveEvent(QPointF(point), actions, mime, Qt.MouseButton.LeftButton, modifiers)
    QtWidgets.QApplication.sendEvent(viewport, move)
    drop = QDropEvent(QPointF(point), actions, mime, Qt.MouseButton.LeftButton, modifiers)
    QtWidgets.QApplication.sendEvent(viewport, drop)
    return drop.isAccepted()


def row_point(pane, name):
    return pane.table.visualRect(pane.table.model().index(widgets.find_row(pane.table, name), 0)).center()


def empty_point(pane):
    return QPoint(20, pane.table.viewport().height() - 5)


def dragged(page, pane, names_, then):
    """Select NAMES_ in PANE and drag them; THEN(mime) drops them (instead of the window system's drag)."""
    select(pane, *names_)
    got = []
    page.exec_drag = lambda drag: got.append(drag.mimeData()) or then(drag.mimeData())
    page.start_drag(pane)
    QtWidgets.QApplication.processEvents()
    settle(page.left, page.right)
    return got[0]


def test_drag_between_the_panes_copies_and_with_shift_moves(setup, wait):
    page, ws, host, _ = setup
    shown(page)
    taken = []
    mime = dragged(page, page.left, ["a.txt", "sub"], lambda m: taken.append(drop_at(page.right, m, empty_point(
        page.right))))
    finish(page, wait)
    assert taken == [True] and (host / "a.txt").read_text() == "alpha" and (host / "sub" / "b.txt").exists()
    assert (ws / "a.txt").exists(), "a drop copies"
    assert mime.hasFormat(files_page.MIME)
    (host / "tool").write_text("#!/bin/sh\n")
    page.right.list()
    dragged(page, page.right, ["tool"], lambda m: drop_at(page.left, m, empty_point(page.left),
                                                         Qt.KeyboardModifier.ShiftModifier))
    finish(page, wait)
    assert (ws / "tool").exists() and not (host / "tool").exists(), "with Shift it moves"


def test_a_drop_onto_a_folder_goes_into_it_and_nowhere_it_makes_no_sense(setup, wait):
    page, ws, host, _ = setup
    shown(page)
    taken = []
    dragged(page, page.left, ["a.txt"], lambda m: taken.append(drop_at(page.left, m, row_point(page.left, "sub"))))
    finish(page, wait)
    assert taken == [True] and (ws / "sub" / "a.txt").read_text() == "alpha" and (ws / "a.txt").exists()
    # Onto its own folder, or a folder into itself: not taken, nothing done.
    dragged(page, page.left, ["a.txt"], lambda m: taken.append(drop_at(page.left, m, empty_point(page.left))))
    dragged(page, page.left, ["sub"], lambda m: taken.append(drop_at(page.left, m, row_point(page.left, "sub"))))
    assert taken[1:] == [False, False] and not page.task.running
    # ".." takes a drop for the folder above.
    page.left.go("sub")
    settle(page.left)
    dragged(page, page.left, ["b.txt"], lambda m: taken.append(drop_at(page.left, m, row_point(page.left, ".."),
                                                                       Qt.KeyboardModifier.ShiftModifier)))
    finish(page, wait)
    assert (ws / "b.txt").read_text() == "beta" and not (ws / "sub" / "b.txt").exists()
    # The app's format with another token (another window, or forged): not taken.
    forged = QMimeData()
    forged.setData(files_page.MIME, b"0" * 32)
    assert drop_at(page.right, forged, empty_point(page.right)) is False


def test_files_from_other_programs_are_copied_in_and_links_in_a_sandbox_are_not_followed(setup, wait):
    page, ws, host, outside = setup
    shown(page)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(host.parent / "outside" / "precious.txt")), QUrl("https://example.org/x")])
    assert drop_at(page.left, mime, empty_point(page.left))
    QtWidgets.QApplication.processEvents()
    finish(page, wait)
    assert (ws / "precious.txt").read_text() == "the host's own"
    # Through the agent's link "escape" (ws/escape -> outside): refused, as in the path bar.
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(ws / "escape" / "precious.txt"))])
    assert drop_at(page.right, mime, empty_point(page.right))
    QtWidgets.QApplication.processEvents()
    settle(page.right)
    assert "symbolic link" in page.message.text() and not (host / "precious.txt").exists()
    remote = QMimeData()
    remote.setUrls([QUrl("https://example.org/x")])
    assert drop_at(page.left, remote, empty_point(page.left)) is False, "only local files"


def test_what_other_programs_get_from_a_drag(setup):
    page, ws, host, _ = setup
    (host / "notes.txt").write_text("n")
    (host / "dir").mkdir()
    page.right.list()

    def urls(pane, *wanted):
        return sorted(os.path.basename(url.toLocalFile()) for url in dragged(page, pane, wanted, lambda m: None).urls())
    assert urls(page.right, "notes.txt", "dir") == ["dir", "notes.txt"]
    assert urls(page.left, "a.txt", "run.sh", "sub", "escape") == ["a.txt"], \
        "out of a sandbox's folder: plain files only, never executable ones, folders or links"


def test_a_press_on_a_selected_row_keeps_the_selection_and_dragging_starts_a_drag(setup, monkeypatch):
    page, ws, host, _ = setup
    shown(page)
    left = page.left
    select(left, "a.txt", "sub")
    started = []
    monkeypatch.setattr(page, "start_drag", lambda pane: started.append(sorted(e.name for e in pane.chosen())))
    point = row_point(left, "a.txt")
    QTest.mousePress(left.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert sorted(e.name for e in left.chosen()) == ["a.txt", "sub"]
    QTest.mouseMove(left.table.viewport(), point + QPoint(0, QtWidgets.QApplication.startDragDistance() + 20))
    assert started == [["a.txt", "sub"]]
    QTest.mouseRelease(left.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    # A click without a drag selects that row alone.
    QTest.mouseClick(left.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert [e.name for e in left.chosen()] == ["a.txt"]
    page.hide()


# -- the editor --

def editor_of(page, wait):
    assert page.editors, "an editor window opened"
    window = page.editors[-1]
    assert wait(lambda: not window.loading, 10)
    return window


def saved(window, wait):
    assert window.save() is True
    assert wait(lambda: not window.saving, 10)


def test_a_double_click_opens_text_in_the_editor_and_save_writes_it_back(setup, wait):
    page, ws, host, _ = setup
    page.left.open_row(widgets.find_row(page.left.table, "a.txt"))
    window = editor_of(page, wait)
    assert window.view == "text" and window.text.toPlainText() == "alpha" and not window.read_only
    assert str(ws / "a.txt") in window.where.text() and window.isVisible()
    window.text.selectAll()
    window.text.insertPlainText("beta\nnew line\n")
    assert window.dirty and window.windowTitle().startswith("*")
    saved(window, wait)
    assert (ws / "a.txt").read_text() == "beta\nnew line\n" and not window.dirty
    # Changed on disk meanwhile: asked before it is replaced.
    (ws / "a.txt").write_text("the agent's version")
    window.text.insertPlainText("mine")
    widgets.SHOWN.clear()
    saved(window, wait)
    assert [title for title, *_ in widgets.SHOWN] == ["Replace the changed file?"]
    assert wait(lambda: not window.saving and (ws / "a.txt").read_text().endswith("mine"))
    (ws / "a.txt").write_text("again the agent's")
    window.text.insertPlainText("!")
    widgets.AUTO_ANSWER = False
    saved(window, wait)
    assert (ws / "a.txt").read_text() == "again the agent's" and "Not saved" in window.notice.text()
    window.close()  # unsaved changes: asked (no) - it stays open
    assert window.isVisible()
    widgets.AUTO_ANSWER = True
    window.close()
    assert not window.isVisible()


def test_binary_files_open_in_the_hex_editor_and_are_changed_in_place(setup, wait):
    page, ws, host, _ = setup
    (ws / "blob.bin").write_bytes(bytes(range(40)) + b"\0")
    page.left.list()
    settle(page.left)
    page.left.open_row(widgets.find_row(page.left.table, "blob.bin"))
    window = editor_of(page, wait)
    assert window.view == "hex" and not window.as_text.isEnabled(), "not text: no text view"
    view = window.hex
    view.setFocus()
    QTest.keyClicks(view, "ff")
    assert view.buffer[0] == 0xFF and view.cursor == 1 and view.modified
    QTest.keyClick(view, Qt.Key.Key_Tab)  # the ASCII column
    QTest.keyClicks(view, "AB")
    assert bytes(view.buffer[:3]) == b"\xffAB" and view.cursor == 3
    QTest.keyClick(view, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    assert view.cursor == 40
    QTest.keyClicks(view, "zz")  # one byte per character, the size never changes
    assert len(view.buffer) == 41
    saved(window, wait)
    data = (ws / "blob.bin").read_bytes()
    assert data[:3] == b"\xffAB" and data[40:] == b"zz"[:0] + b"z" and len(data) == 41
    window.close()


def test_view_is_read_only_and_text_keeps_its_line_endings_and_bom(setup, wait):
    page, ws, host, _ = setup
    (ws / "dos.txt").write_bytes("\ufeffone\r\ntwo\r\n".encode())
    (ws / "mixed.txt").write_bytes(b"one\r\ntwo\n")
    (ws / "trojan.py").write_text("ok = True  # \u202e evil\n")
    page.left.list()
    select(page.left, "dos.txt")
    page.left.table.setCurrentCell(widgets.find_row(page.left.table, "dos.txt"), 0)
    press(page.left, Qt.Key.Key_F3)
    viewer = editor_of(page, wait)
    assert viewer.read_only and viewer.text.isReadOnly() and not viewer.save_button.isVisible()
    assert viewer.save() is False and "(read-only)" in viewer.windowTitle()
    viewer.close()
    press(page.left, Qt.Key.Key_F4)
    window = editor_of(page, wait)
    assert window.text.toPlainText() == "one\ntwo\n" and not window.read_only
    window.text.moveCursor(window.text.textCursor().MoveOperation.End)
    window.text.insertPlainText("three\n")
    saved(window, wait)
    assert (ws / "dos.txt").read_bytes() == "\ufeffone\r\ntwo\r\nthree\r\n".encode()
    assert window.switch("hex") and window.hex.data() == (ws / "dos.txt").read_bytes()
    window.close()
    page.left.open_entry(page.left.by_name["mixed.txt"])
    window = editor_of(page, wait)
    assert window.text.isReadOnly() and "mixed line endings" in window.notice.text()
    assert window.switch("hex") and not window.hex.read_only, "byte by byte in the hex editor"
    window.close()
    page.left.open_entry(page.left.by_name["trojan.py"])
    window = editor_of(page, wait)
    assert "U+202E" in window.notice.text()
    window.close()


def test_big_files_links_and_folders_in_the_editor(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    monkeypatch.setattr(files, "TEXT_LIMIT", 10)
    monkeypatch.setattr(files, "EDIT_LIMIT", 20)
    (ws / "big.txt").write_text("y" * 30)
    page.left.list()
    settle(page.left)
    page.left.open_entry(page.left.by_name["big.txt"], "text")
    window = editor_of(page, wait)
    assert window.view == "hex" and window.read_only and len(window.hex.buffer) == 20
    assert "Only the first" in window.notice.text()
    window.close()
    count = len(page.editors)
    page.left.open_entry(page.left.by_name["escape"])
    assert "not followed" in page.message.text() and len(page.editors) == count
    assert page.open_current(page.left) is False  # nothing selected


# -- the context menu, the clipboard, Copy path, Properties --

def actions(menu):
    return {action.text(): action for action in menu.actions() if action.text()}


def test_the_context_menu_and_the_clipboard(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    select(page.left, "a.txt")
    menu = page.menu(page.left)
    found = actions(menu)
    for text in ("Open", "View", "Edit", "Open with the hex editor", "Copy", "Cut", "Paste", "Copy to the other pane",
                 "Move to the other pane", "Rename…", "Delete…", "New folder…", "Copy path", "Properties…"):
        assert text in found, text
    assert found["Copy"].shortcut().toString() == "Ctrl+C"
    empty = actions(page.menu(page.left, at_row=False))
    assert "Copy" not in empty and "Refresh" in empty and "Properties…" in empty
    found["Copy"].trigger()
    text = QGuiApplication.clipboard().text()
    assert text == str(ws / "a.txt")
    assert page.paste(page.right) is True
    finish(page, wait)
    assert (host / "a.txt").read_text() == "alpha" and (ws / "a.txt").exists()
    # Pasted into its own folder: a copy beside it.
    assert page.paste(page.left) is True
    finish(page, wait)
    assert (ws / "a (copy).txt").read_text() == "alpha"
    # Cut and paste moves, once.
    select(page.right, "a.txt")
    press(page.right, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    press(page.left, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    report = finish(page, wait)
    assert (host / "a.txt").exists() and any("already exists" in line for line in report), \
        "a.txt is there: refused, the source stays"
    assert page.clipboard is None or not page.clipboard.cut
    # Files another program put on the clipboard are copied in.
    (host / "from-elsewhere.txt").write_text("e")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(host / "from-elsewhere.txt"))])
    QGuiApplication.clipboard().setMimeData(mime)
    assert page.paste(page.left) is True
    finish(page, wait)
    assert (ws / "from-elsewhere.txt").read_text() == "e"
    # Copy path: control characters in a name are escaped, never put on the clipboard as they are.
    (ws / "bad\x1bname").write_text("x")
    page.left.list()
    settle(page.left)
    select(page.left, "bad\x1bname")
    assert page.copy_path(page.left).endswith("bad\\x1bname") and "\x1b" not in QGuiApplication.clipboard().text()
    assert page.copy_path(page.left, []) == str(ws)
    # Rename and New folder from the menu.
    select(page.left, "a (copy).txt")
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("renamed.txt", True))
    actions(page.menu(page.left))["Rename…"].trigger()
    assert (ws / "renamed.txt").exists()
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("made", True))
    actions(page.menu(page.left, at_row=False))["New folder…"].trigger()
    assert (ws / "made").is_dir()
    settle(page.left)
    select(page.left, "made")
    actions(page.menu(page.left))["Delete…"].trigger()
    finish(page, wait)
    assert not (ws / "made").exists()
    # Review: the folder shown changed while the menu was open - its actions act on nothing there (they found their
    # items by name in the folder shown when they ran: one of the same name elsewhere was deleted).
    (ws / "sub" / "a.txt").write_text("the other a.txt")
    settle(page.left)
    select(page.left, "a.txt")
    menu = page.menu(page.left)
    page.left.go("sub")
    settle(page.left)
    actions(menu)["Delete…"].trigger()
    assert "changed meanwhile" in page.message.text()
    assert (ws / "sub" / "a.txt").exists() and (ws / "a.txt").exists()


def test_the_menu_key_and_the_keys_of_the_list(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    shown(page)
    opened = []
    monkeypatch.setattr(page, "exec_menu", lambda menu, where: opened.append(sorted(actions(menu))))
    select(page.left, "a.txt")
    page.left.focus_name("a.txt")
    press(page.left, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
    assert opened and "Copy" in opened[0]
    override = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    QtWidgets.QApplication.sendEvent(page.left.table, override)
    assert override.isAccepted(), "Ctrl+C in a list copies files, before any shortcut of the window"
    press(page.left, Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
    assert page.dialogs and page.dialogs[-1].windowTitle() == "Properties of a.txt"
    page.dialogs[-1].close()
    press(page.left, Qt.Key.Key_Return)
    assert editor_of(page, wait).view == "text", "Enter opens a file"
    page.editors[-1].close()
    page.hide()


def test_properties_show_and_change_what_may_be_changed(setup, wait):
    page, ws, host, _ = setup
    (ws / "a.txt").chmod(0o640)
    page.left.list()
    select(page.left, "a.txt")
    dialog = page.properties(page.left)
    labels = [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]
    assert "a.txt" in labels and str(ws) in labels and "file" in labels
    assert dialog.octal.text().startswith("Mode 0640")
    assert not dialog.apply_button.isEnabled()
    dialog.checks[5].setChecked(True)  # the group's execute bit
    assert all(not check.isEnabled() for _, check in dialog.specials), "no set-ID or sticky bit in a sandbox's folder"
    assert dialog.apply() and stat.S_IMODE((ws / "a.txt").stat().st_mode) == 0o650
    dialog.close()
    select(page.left, "escape")
    dialog = page.properties(page.left)
    assert not dialog.changeable and str(host.parent / "outside") in [l.text() for l in dialog.findChildren(
        QtWidgets.QLabel)], "a link: where it points, shown; its bits are not changed"
    dialog.close()
    select(page.left, "sub", "a.txt")
    dialog = page.properties(page.left)
    assert dialog.windowTitle() == "Properties of 2 items" and dialog.counting, "a folder's size: counted at once"
    assert wait(lambda: not dialog.counting)
    assert "in 2 file(s) and 1 folder(s)" in dialog.size.text() and not dialog.count_button.isVisible()
    dialog.close()
    page.left.table.clearSelection()
    dialog = page.properties(page.left)  # the folder shown: the sandbox's own folder is changed from above it
    assert not dialog.changeable and dialog.counting
    dialog.close()
    dialog = page.properties(page.right, [])  # a folder of the host: set-ID bits may be set there
    assert dialog.changeable and all(check.isEnabled() for _, check in dialog.specials)
    dialog.close()


# -- the hex view by itself --

def test_hex_view_moves_types_and_stays_read_only_when_asked(qapp):
    view = file_editor.HexView()
    view.resize(700, 300)
    view.set_data(bytes(range(64)), read_only=True)
    assert view.type_text("a") is True and view.buffer[0] == 0, "read-only: nothing changes"
    view.set_data(bytes(range(64)))
    view.go(17)
    assert view.cursor == 17
    QTest.keyClick(view, Qt.Key.Key_Home)
    assert view.cursor == 16
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert view.cursor == 32
    view.type_text("1")
    assert view.buffer[32] == 0x10 | (32 & 0x0F) and view.low
    view.type_text("g")  # not a hex digit: nothing
    assert view.buffer[32] == 0x10 and view.cursor == 32
    view.type_text("2")
    assert view.buffer[32] == 0x12 and view.cursor == 33 and view.edited == {32}
    point = QPoint(view.ascii_x + 3 * view.cw + 1, 2 * view.ch + 1)
    assert view.offset_at(point) == (35, True)
    point = QPoint(view.hex_column(9) + 1, 1)
    assert view.offset_at(point) == (9, False)
    assert file_editor.split_text("a\r\nb") == ("a\nb", False, "\r\n") and file_editor.split_text("a\rb") is None
    assert file_editor.join_text("a\nb", True, "\r\n") == "\ufeffa\r\nb".encode()
    view.deleteLater()
    # Review: Shift+Enter types a line separator (U+2028), shown as a line break and saved as E2 80 A8 - one long
    # line in the file. Saved as a line break; a file that holds one is not opened as text.
    from PySide6.QtWidgets import QPlainTextEdit
    edit = QPlainTextEdit()
    QTest.keyClicks(edit, "ab")
    QTest.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClicks(edit, "cd")
    assert file_editor.editor_text(edit) == "ab\ncd"
    assert file_editor.split_text("a\u2028b") is None
    edit.deleteLater()


def test_copy_names_for_a_copy_beside_the_original():
    assert files_page.copy_name("a.txt") == "a (copy).txt" and files_page.copy_name("a.txt", 2) == "a (copy 2).txt"
    assert files_page.copy_name(".bashrc") == ".bashrc (copy)" and files_page.copy_name("src") == "src (copy)"


def test_nothing_happens_where_there_is_nothing_to_act_on(setup, wait):
    page, ws, host, _ = setup
    page.left.go("sub")
    settle(page.left)
    page.left.table.selectRow(0)  # "." alone (it cannot even be selected)
    assert page.left.chosen() == [] and page.delete_selected(page.left) is False
    assert page.copy_items(page.left) is False and "Select files" in page.message.text()
    page.clipboard = None
    QGuiApplication.clipboard().setText("just some text")
    assert page.paste(page.left) is False and "Nothing to paste" in page.message.text()
    window = page.open_editor(page.left.root, "sub/b.txt", str(ws / "sub" / "b.txt"))
    assert window.switch("hex") is False, "not before it is read"
    assert editor_of(page, wait).text.toPlainText() == "beta"
    window.close()
    missing = page.open_editor(page.left.root, "gone.txt", str(ws / "gone.txt"))
    assert wait(lambda: not missing.loading) and "Cannot open" in missing.notice.text() and missing.save() is False
    missing.close()


def test_what_is_typed_while_a_save_runs_stays_unsaved(setup, wait):
    page, ws, host, _ = setup
    window = page.open_editor(page.left.root, "a.txt", str(ws / "a.txt"))
    editor_of(page, wait)
    window.text.selectAll()
    window.text.insertPlainText("first")
    assert window.save() is True and window.saving
    window.text.insertPlainText(" and more")  # typed before the save has reported
    assert wait(lambda: not window.saving, 10)
    assert (ws / "a.txt").read_text() == "first"
    assert window.dirty and window.windowTitle().startswith("*"), "what was typed meanwhile is not saved yet"
    widgets.AUTO_ANSWER = False
    window.close()  # asked (no): it stays open
    assert window.isVisible()
    saved(window, wait)
    assert (ws / "a.txt").read_text() == "first and more" and not window.dirty
    widgets.AUTO_ANSWER = True
    window.close()


def changed_on_disk(page, ws, wait):
    """An editor of a.txt with "mine" typed in; the agent wrote a.txt meanwhile (a save asks first)."""
    window = page.open_editor(page.left.root, "a.txt", str(ws / "a.txt"))
    editor_of(page, wait)
    window.text.selectAll()
    window.text.insertPlainText("mine")
    (ws / "a.txt").write_text("the agent's, longer")
    return window


def test_the_editor_saves_and_reloads_nothing_while_it_asks_whether_to_replace(setup, wait, monkeypatch):
    """The editor is a window of its own (dialogs block nothing): while its "Replace the changed file?" is open, Save,
    Ctrl+S and Reload wait for the answer - a Reload then Replace had written the old text while the window showed the
    file read again, nothing marked to be saved; Ctrl+S asked a second time."""
    page, ws, host, _ = setup
    window = changed_on_disk(page, ws, wait)
    asked, during = [], {}

    def answer(parent, action, title, text, *args, **kwargs):
        asked.append(action)
        if action == "files-replace":
            during.update(saving=window.saving, save=window.save_button.isEnabled(),
                          reload=window.reload_button.isEnabled())
            window.reload_button.click()  # (greyed out: nothing)
            during.update(reloaded=window.reload(), again=window.save())  # Reload, Ctrl+S
        return True
    monkeypatch.setattr(file_editor, "ask_first", answer)
    saved(window, wait)
    assert during == {"saving": True, "save": False, "reload": False, "reloaded": False, "again": False}
    assert asked == ["files-replace"], "one question, nothing read again"
    assert (ws / "a.txt").read_text() == "mine" == window.text.toPlainText() and not window.dirty
    assert window.save_button.isEnabled() and window.reload_button.isEnabled()
    window.close()


def test_what_is_shown_stays_to_be_saved_when_it_is_not_what_was_written(setup, wait, monkeypatch):
    """Whatever made what is shown differ from what was written while it was asked (here: read again from disk), the
    editor keeps it to be saved - closing it asks."""
    page, ws, host, _ = setup
    window = changed_on_disk(page, ws, wait)

    def answer(parent, action, title, text, *args, **kwargs):
        if action == "files-replace":
            window.text.setPlainText("the agent's, longer")  # as a reload shows it
            window.text.document().setModified(False)
            window.dirty = False
        return True
    monkeypatch.setattr(file_editor, "ask_first", answer)
    saved(window, wait)
    assert (ws / "a.txt").read_text() == "mine" and window.text.toPlainText() == "the agent's, longer"
    assert window.dirty and window.windowTitle().startswith("*")
    widgets.AUTO_ANSWER = True
    window.close()


def test_an_editor_closed_while_it_asks_whether_to_replace_goes_quietly(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    window = changed_on_disk(page, ws, wait)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))

    def answer(parent, action, title, text, *args, **kwargs):
        if action == "files-replace":
            window.close()  # (asked: discard)
            QtWidgets.QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            assert not shiboken6.isValid(window)
        return True
    monkeypatch.setattr(file_editor, "ask_first", answer)
    assert window.save()
    assert wait(lambda: not page.editors, 10)
    wait(lambda: bool(errors), 0.5)
    assert errors == [] and (ws / "a.txt").read_text() == "the agent's, longer"


# -- a question or dialog left open while the panes change: the answer acts on what it was about --
# (Dialogs block nothing: while one is open the panes can go to another folder or another sandbox.)

def moved_meanwhile(page, ws):
    """While the question is open: the sandbox pane goes into sub/, which has an a.txt of its own."""
    (ws / "sub" / "a.txt").write_text("sub's own")
    page.left.go("sub")
    settle(page.left)
    assert page.left.relative == "sub"


def test_delete_acts_on_what_the_question_named_when_the_pane_moved_meanwhile(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    select(page.left, "a.txt")
    asked = []

    def answer(parent, action, title, text, *args, **kwargs):
        asked.append(text)
        moved_meanwhile(page, ws)
        return True
    monkeypatch.setattr(files_page, "ask_first", answer)
    assert page.delete_selected(page.left)
    finish(page, wait)
    assert str(ws) in asked[0] and str(ws / "sub") not in asked[0]
    assert not (ws / "a.txt").exists() and (ws / "sub" / "a.txt").read_text() == "sub's own"


def test_nothing_acts_on_the_rows_of_the_folder_before_while_the_next_is_listed(setup, wait):
    """go() takes the new folder at once, its rows come with the listing (a thread): until then the rows shown are the
    folder's before - an action on them had taken a.txt of the top as sub/a.txt (F2, Delete, Enter on sub went to
    sub/sub, a drop onto sub into sub/sub). They wait, and say so; the selection made meanwhile is not carried over."""
    page, ws, host, _ = setup
    (ws / "sub" / "a.txt").write_text("sub's own")
    (host / "new.txt").write_text("from the host")
    pane = page.left
    select(pane, "a.txt")
    pane.focus_name("a.txt")
    pane.go("sub")

    def still(key, modifiers=Qt.KeyboardModifier.NoModifier):
        assert pane.listing is not None and pane.relative == "sub", "(the listing of sub/ is not shown yet)"
        page.message.setText("")
        assert page.key(pane, key, modifiers)
        assert page.message.text() == f"Wait until {ws / 'sub'} is listed: the list still shows the folder before."
    pane.table.selectAll()
    assert pane.current() is None and pane.chosen() == [] and not pane.is_up(0)
    for key in (Qt.Key.Key_F2, Qt.Key.Key_Delete, Qt.Key.Key_F8, Qt.Key.Key_F3, Qt.Key.Key_F4, Qt.Key.Key_F5,
                Qt.Key.Key_F6):
        still(key)
    still(Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
    still(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    for button in (page.delete_selected, page.rename_dialog, page.copy_selected, page.chmod_dialog):  # (the buttons)
        page.message.setText("")
        assert button(pane) is False and "is listed: the list still shows" in page.message.text()
    pane.table.setCurrentCell(widgets.find_row(pane.table, "sub"), 0)
    still(Qt.Key.Key_Return)
    pane.open_row(widgets.find_row(pane.table, "sub"))  # a double click
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(host / "new.txt"))])
    assert page.drop_plan(pane, mime, row_point(pane, "sub"), Qt.KeyboardModifier.NoModifier) is None
    assert not page.task.running and not page.editors and not page.dialogs
    settle(pane)
    assert pane.relative == "sub" and names(pane) == ["a.txt", "b.txt"]
    assert pane.chosen() == [], "what was selected in the rows before is not selected here"
    assert (ws / "a.txt").read_text() == "alpha" and (ws / "sub" / "a.txt").read_text() == "sub's own"
    assert not (ws / "sub" / "sub").exists() and not (ws / "b.txt").exists() and not (host / "a.txt").exists()
    # Listed: the entries are this folder's.
    pane.focus_name("a.txt")
    assert files_page.Chosen.of(pane, [pane.current()]).items == [("a.txt", "sub/a.txt")]


def test_owners_act_on_what_was_chosen_when_the_pane_moved_meanwhile(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    seen = []
    monkeypatch.setattr(files.Root, "chown_entry", lambda root, path, *a, **k: seen.append((root.path, path)) or
                        files.Result(files=1))
    monkeypatch.setattr(files, "take_ownership", lambda root, path, **k: seen.append((root.path, path)) or
                        files.Result(files=1))

    class Owner:
        def __init__(self, parent, count, folders):
            pass

        def exec(self):
            if page.left.relative != "sub":
                moved_meanwhile(page, ws)
            return QtWidgets.QDialog.DialogCode.Accepted

        def values(self):
            return os.getuid(), -1, True
    monkeypatch.setattr(files_page, "OwnerDialog", Owner)
    select(page.left, "a.txt")
    assert page.chown_dialog(page.left)
    finish(page, wait)
    assert seen == [(str(ws), "a.txt")]
    # Take ownership: the sandbox switched while the question was open.
    other = ws.parent / "other"
    other.mkdir()
    (other / "a.txt").write_text("another sandbox's")
    page.left.go("")
    settle(page.left)
    select(page.left, "a.txt")
    seen.clear()

    def switched(*args, **kwargs):
        page.left.root, page.left.relative = files.Root(other), ""
        return True
    monkeypatch.setattr(files_page, "confirm", switched)
    assert page.own_selected(page.left)
    finish(page, wait)
    assert seen == [(str(ws), "a.txt")]
    # Properties › Owner…: the items the dialog shows, whatever the pane shows by now.
    page.left.root, page.left.relative = files.Root(ws), ""
    page.left.list()
    select(page.left, "a.txt")
    dialog = page.properties(page.left)
    moved_meanwhile(page, ws)
    seen.clear()
    owner = next(b for b in dialog.findChildren(QtWidgets.QPushButton) if b.text() == "Owner…")
    owner.click()
    finish(page, wait)
    assert seen == [(str(ws), "a.txt")]
    dialog.close()


def test_a_cut_out_of_a_sandbox_folder_on_the_host_is_pasted_as_the_agents(setup, wait, monkeypatch):
    """The host pane in a sandbox's folder: what is cut there and pasted into an ordinary host folder is copied as the
    agent's (no executable bit, the warning about files tools run), then removed - never a plain rename."""
    page, ws, host, _ = setup
    (ws / "Makefile").write_text("all:\n")
    page.right.go_path(str(ws))
    settle(page.right)
    select(page.right, "run.sh", "Makefile")
    assert page.copy_items(page.right, cut=True)
    page.right.go_path(str(host))
    settle(page.right)
    shown = []
    monkeypatch.setattr(files_page, "warn", lambda parent, title, text, *a, **k: shown.append(title))
    assert page.paste(page.right)
    report = finish(page, wait)
    assert not (ws / "run.sh").exists() and (host / "run.sh").exists() and not os.access(host / "run.sh", os.X_OK)
    assert any(line.startswith("Copied without the executable bit") for line in report)
    assert shown == ["Files that run or configure tools"]

