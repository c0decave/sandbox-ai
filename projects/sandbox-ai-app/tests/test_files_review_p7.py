"""Review pass 7 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A delete of a folder that held one file mounted there (a bind mount of a single file) deleted everything before it and
then stopped at that file ("busy"); a move removed such a file from its source (or failed half way), a recursive owner
change and Take ownership changed the mounted file's owner. A sandbox's folder reached under another path (a bind mount
of it) counted as the host's own: the agent's links were followed there, its files copied as the human's. The editor
had no Ctrl+W (Cmd+W). The Name column sorted by lower() and code points (accents after "z") and, clicked, put folders
among the files. A relisting replaced what was being typed in the path bar, and a path typed wrong gave the bar the
folder's path with its bytes that are no UTF-8 dropped (Enter then went to another folder). Files of another program
dropped or pasted into the agent's folder went, after the question, into whatever folder of that name the pane showed
by the answer. What was typed into an editor while its file was read (just opened, or Reload) was taken, then replaced.
Take ownership's time limit said "Cancelled." to whoever had cancelled nothing. Everything changed is below tmp_path."""
import os
from pathlib import Path
import sys
import unicodedata

import pytest

from sandbox_ai_app import files

HERE = Path(__file__).resolve().parent

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_ops import fake_podman, places  # noqa: E402,F401
from test_files_page import settle, setup  # noqa: E402,F401
from test_files_review_p5 import mounts  # noqa: E402,F401
from test_files_review_p6 import FAKE_MOUNT, podman_that_forks  # noqa: E402


# -- one file mounted in a folder --

@pytest.fixture
def bound(tmp_path, mounts):
    """ws/data holds notes.txt and bound.txt - a single file mounted there (mount --bind of one file, as containers
    have them)."""
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "notes.txt").write_text("notes")
    (ws / "data" / "bound.txt").write_text("the host's file, mounted here")
    mounts(ws / "data" / "bound.txt")
    return ws


def test_a_folder_that_holds_one_mounted_file_is_not_deleted(bound):
    """Counted first, only folders were looked at: the delete took notes.txt, then stopped at bound.txt ("Device or
    resource busy") - half the folder gone. Refused now before anything is deleted."""
    root = files.Root(bound)
    with pytest.raises(files.Refused, match="mount point"):
        root.delete_entry("data")
    assert (bound / "data" / "notes.txt").exists() and (bound / "data" / "bound.txt").exists()
    with pytest.raises(files.Refused, match="mount point"):
        root.delete_entry("data/bound.txt")  # (the file itself: said so, not "busy")
    root.delete_entry("data/notes.txt")  # (a file that is no mount point: deleted as asked)
    assert not (bound / "data" / "notes.txt").exists()


def test_a_file_that_is_no_mount_point_is_not_taken_for_one(tmp_path):
    """Keeps it so: files, links (also to a folder), a FIFO in an ordinary folder are deleted."""
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "a.txt").write_text("a")
    (ws / "data" / "to-dir").symlink_to(tmp_path)
    os.mkfifo(ws / "data" / "pipe")
    assert files.Root(ws).delete_entry("data").removed == 4
    assert not (ws / "data").exists() and tmp_path.exists()


def test_a_move_keeps_a_mounted_file_in_its_source(bound, tmp_path):
    """The move copied it, then removed it from the source as one of its own (or, on a real mount, failed there with
    "busy", half way through the removal); it stays now, and the move says so."""
    host = tmp_path / "host"
    host.mkdir()
    moved = files.move_entry(files.Root(bound), "data", files.Root(host, sandbox=False), "")
    assert (host / "data" / "bound.txt").read_text() == "the host's file, mounted here", "copied"
    assert not moved.moved and moved.left == ["data/bound.txt"] and "bound.txt" in moved.kept
    assert (bound / "data" / "bound.txt").exists() and not (bound / "data" / "notes.txt").exists()


def test_a_recursive_owner_change_skips_a_mounted_file(bound):
    owned = files.Root(bound).chown_entry("data", gid=os.getgid(), recursive=True)
    assert [path for path, _ in owned.skipped] == ["data/bound.txt"] and owned.files == 2  # data, notes.txt


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="podman unshare: Linux")
def test_take_ownership_skips_a_mounted_file(places, fake_podman, monkeypatch):
    sandbox, _, ws, _, _ = places
    monkeypatch.setattr(files, "OWNERSHIP_HELPER", files.OWNERSHIP_HELPER.replace("\ndef own(", FAKE_MOUNT + "\ndef own(", 1))
    monkeypatch.setenv("FAKE_MOUNT", str(ws / "sub" / "b.txt"))
    foreign = max(files.user_groups()) + 54321  # (every entry it changes fails, and is reported: each one named)
    reasons = dict(files.take_ownership(sandbox, "sub", podman=fake_podman, owner=(os.getuid(), foreign)).skipped)
    assert "mount point" in reasons["sub/b.txt"]
    assert "mount point" not in reasons["sub/deeper/c.txt"], "the other files: changed (here: tried)"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="podman unshare: Linux")
def test_take_ownerships_time_limit_says_what_happened(places, tmp_path):
    sandbox, *_ = places
    with pytest.raises(files.Cancelled, match="took longer than 0.5 s"):
        files.take_ownership(sandbox, "sub", podman=podman_that_forks(tmp_path), owner=(os.getuid(), os.getgid()),
                             timeout=0.5)


# -- a sandbox's folder under another path --

def test_a_sandboxs_folder_reached_under_another_path_is_the_agents(setup, tmp_path, monkeypatch):
    """mount --bind ~/ws /mnt/ws: the host pane at /mnt/ws compared paths only - the agent's link was followed there,
    and what was copied from there kept its executable bits. (No mount in a test: the folder's identity stands in.)"""
    page, ws, host, outside = setup
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    (mnt / "escape").symlink_to(outside)
    (mnt / "sub").mkdir()
    real = files_page.folder_identity

    def identity(path):
        return real(str(ws)) if os.path.realpath(str(path)) == str(mnt.resolve()) else real(path)
    monkeypatch.setattr(files_page, "folder_identity", identity)
    right = page.right
    assert right.go_path(str(mnt))
    settle(right)
    assert right.untrusted() and right.untrusted_at(right.relative + "/sub") and "belongs to a sandbox" in right.note.text()
    assert page.below_sandbox(str(mnt / "sub" / "x")) and page.sandbox_holding(str(mnt / "sub")) == str(mnt.resolve())
    right.open_row(widgets.find_row(right.table, "escape"))
    assert "not followed" in page.message.text() and right.path_text() == str(mnt.resolve())
    assert page.markdown_top(right.root, files_page.host_relative(str(mnt / "sub" / "a.md"))) == \
        files_page.host_relative(str(mnt.resolve())), "a Markdown file there shows images of that folder only"
    assert not page.below_sandbox(str(host)) and not page.below_sandbox(str(tmp_path))


# -- the editor: Ctrl+W --

@pytest.fixture
def editor(tmp_path, qapp, wait, monkeypatch):
    from sandbox_ai_app.file_editor import EditorWindow
    monkeypatch.setattr(widgets, "SHOWN", [])
    (tmp_path / "a.txt").write_text("old")
    window = EditorWindow(None, files.Root(tmp_path), "a.txt", str(tmp_path / "a.txt"))
    window.show()
    window.activateWindow()
    assert wait(lambda: not window.loading, 10)
    window.text.setFocus()
    qapp.processEvents()
    yield window
    window.dirty = False
    window.close()
    window.deleteLater()


def test_ctrl_w_closes_the_editor(editor):
    QTest.keyClick(editor.text, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    assert not editor.isVisible() and not widgets.SHOWN, "nothing unsaved: closed at once"


def test_ctrl_w_asks_first_about_changes_not_saved(editor, monkeypatch):
    QTest.keyClicks(editor.text, "new ")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep editing"
    QTest.keyClick(editor.text, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    assert editor.isVisible() and widgets.SHOWN and widgets.SHOWN[-1][0] == "Discard changes?"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # "Discard"
    QTest.keyClick(editor.text, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    assert not editor.isVisible()
    assert (Path(editor.root.path) / "a.txt").read_text() == "old"


# -- the Name column --

NAMES = ["b.txt", "A.txt", "C.txt", "é.txt", "e.txt", "f.txt", unicodedata.normalize("NFD", "è.txt")]
FOLDERS = ["Zeta", "alpha", "Émile"]


def shown_names(pane):
    settle(pane)
    return [pane.table.item(row, 0).text() for row in range(pane.table.rowCount())]


@pytest.fixture
def named(setup):
    page, ws, host, _ = setup
    for name in NAMES:
        (ws / "sub" / name).write_text(name)
        (host / name).write_text(name)
    for name in FOLDERS:
        (ws / "sub" / name).mkdir()
        (host / name).mkdir()
    page.left.go("sub")
    page.right.list()
    settle(page.left, page.right)
    return page


FILES_ASCENDING = ["A.txt", "b.txt", "C.txt", "e.txt", "é.txt", unicodedata.normalize("NFD", "è.txt"), "f.txt"]


def test_names_are_listed_as_people_read_them_folders_first(named):
    """Listed (no column clicked): by lower() and code points - "é.txt" and "è.txt" after "f.txt", "Émile" after
    "Zeta"."""
    for pane in (named.left, named.right):
        assert shown_names(pane) == [".", "..", "alpha", "Émile", "Zeta", *FILES_ASCENDING]


def test_a_click_on_name_keeps_folders_first_both_ways_and_after_a_relisting(named):
    """A click on Name sorted by the shown text alone: folders among the files, accents after "z"."""
    for pane in (named.left, named.right):
        pane.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        listed = shown_names(pane)
        assert listed[:5] == [".", "..", "alpha", "Émile", "Zeta"], listed
        assert listed[5:] == FILES_ASCENDING, listed
        pane.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        listed = shown_names(pane)
        assert listed[:5] == [".", "..", "Zeta", "Émile", "alpha"], "folders first, in reverse"
        assert listed[5:] == FILES_ASCENDING[::-1], listed
        pane.list()  # (after a copy, a save, a Refresh: sorted as it was)
        assert shown_names(pane)[2:5] == ["Zeta", "Émile", "alpha"]
        pane.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)


def test_name_order_takes_nfd_and_nfc_for_one_name():
    entries = [files.Entry(unicodedata.normalize(form, "é") + tail, "file", 0, 0) for form, tail in
               (("NFD", "b"), ("NFC", "a"), ("NFD", "c"))]
    assert [entry.name[-1] for entry in files_page.name_order(entries)] == ["a", "b", "c"]


# -- the path bar --

def test_a_relisting_keeps_what_is_typed_in_the_path_bar(setup, qapp, wait):
    """A copy that ended, a save of an editor, Refresh: the pane listed its folder again and put the folder's path back
    into the bar, in the middle of the typing."""
    page, ws, host, _ = setup
    page.show()
    page.activateWindow()
    right = page.right
    right.path.setFocus()
    assert wait(lambda: right.path.hasFocus(), 5)
    right.path.selectAll()
    QTest.keyClicks(right.path, str(ws) + "/su")
    right.list()
    settle(right)
    assert right.path.text() == str(ws) + "/su"
    QTest.keyClicks(right.path, "b")
    QTest.keyClick(right.path, Qt.Key.Key_Return)
    settle(right)
    assert right.path_text() == str(ws / "sub") and right.path.text() == str(ws / "sub")
    right.list()
    settle(right)
    assert right.path.text() == str(ws / "sub")
    page.hide()


# -- a question blocks nothing --

def test_files_of_another_program_go_only_where_the_question_named(setup, tmp_path, monkeypatch, wait):
    """Dropped or pasted from another program into the agent's folder: asked, and while it asked another sandbox's
    folder (or a remote system) was chosen in the pane - the files went there, into the folder of that name."""
    page, ws, host, outside = setup
    other = tmp_path / "other-ws"
    other.mkdir()

    def answer(*_args, **_kwargs):
        page.left.root = files.Root(other)  # (chosen in the chooser meanwhile)
        return True
    monkeypatch.setattr(files_page, "confirm", answer)
    assert not page.copy_paths([str(outside / "precious.txt")], page.left, "", foreign=True)
    assert wait(lambda: not page.task.running, 10)
    assert not (other / "precious.txt").exists() and not (ws / "precious.txt").exists()
    assert "changed meanwhile" in page.message.text()
    monkeypatch.setattr(files_page, "confirm", lambda *_a, **_k: True)  # (the same folder: copied as asked)
    page.left.root = files.Root(ws)
    assert page.copy_paths([str(outside / "precious.txt")], page.left, "", foreign=True)
    assert wait(lambda: not page.task.running, 10) and (ws / "precious.txt").read_text() == "the host's own"


def test_a_path_typed_wrong_gives_the_folders_path_back_escaped(setup):
    """The bar got the folder's path as it is: Qt drops a byte that is no UTF-8 - a\\xffb showed as ".../ab", and Enter
    went to the folder ab (the agent's, another one)."""
    page, ws, host, _ = setup
    odd = "a" + os.fsdecode(b"\xff") + "b"
    (ws / odd).mkdir()
    (ws / "ab").mkdir()
    left = page.left
    left.go(odd)
    settle(left)
    assert not left.go_path("/not/below/it")
    assert left.path.text() == files.printable(left.path_text()) and "\\udcff" in left.path.text()
    assert left.go_path()  # (Enter on what the bar shows: the folder itself)
    settle(left)
    assert left.relative == odd


def test_what_is_typed_while_a_reload_reads_is_not_taken_and_then_replaced(tmp_path, qapp, wait):
    """Reload (nothing changed: not asked) on a slow file system or a remote system: the text still took what was typed
    while the file was read - and the read replaced it, unasked. The views take nothing while it reads."""
    import threading
    from sandbox_ai_app.file_editor import EditorWindow
    (tmp_path / "a.txt").write_text("old")
    go = threading.Event()

    class Slow(files.Root):
        def read_file(self, relative, limit=None):
            if self.slow:
                go.wait(10)
            return super().read_file(relative, limit)
    root = Slow(tmp_path)
    root.slow = True
    window = EditorWindow(None, root, "a.txt", str(tmp_path / "a.txt"))
    window.show()
    QTest.keyClicks(window.text, "typed before it was read")  # (the window just opened)
    assert window.text.isReadOnly()
    go.set()
    assert wait(lambda: not window.loading, 10)
    assert window.text.toPlainText() == "old" and not window.dirty
    go.clear()
    assert window.reload() and window.loading
    QTest.keyClicks(window.text, "typed meanwhile")
    assert window.text.toPlainText() == "old" and window.text.isReadOnly() and window.hex.read_only
    go.set()
    assert wait(lambda: not window.loading, 10)
    assert not window.text.isReadOnly() and not window.hex.read_only
    QTest.keyClicks(window.text, "new ")
    assert window.dirty and "new " in window.text.toPlainText()
    window.dirty = False
    window.close()
    window.deleteLater()


def test_a_reload_that_fails_gives_the_views_back(tmp_path, qapp, wait):
    """Keeps it so (with the fix above): the text and the hex view take what is typed again."""
    from sandbox_ai_app.file_editor import EditorWindow
    (tmp_path / "a.txt").write_text("old")
    window = EditorWindow(None, files.Root(tmp_path), "a.txt", str(tmp_path / "a.txt"))
    window.show()
    assert wait(lambda: not window.loading, 10)
    (tmp_path / "a.txt").unlink()
    assert window.reload()
    assert wait(lambda: not window.loading, 10)
    assert not window.text.isReadOnly() and not window.hex.read_only
    window.close()
    window.deleteLater()
