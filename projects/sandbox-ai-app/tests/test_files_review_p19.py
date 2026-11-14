"""Review pass 19 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A save to a remote file nobody may write replaced it (a local save refuses: change its permissions first); a save of a
file removed or renamed meanwhile asked whether to replace "the changed file" and pointed to Reload, which can only
fail - the save made it anew where it was; a move whose source was kept (a link in it, not copied) said the items were
moved. A cut was dropped before the question about replacing (answered No: gone, nothing moved); Recent gave up on a
sandbox's folder that opened slowly; changes undone to the saved text still had to be discarded; a folder's copy beside
it was named as a file's ("src (copy).d"). All of them low: no medium or high issue was found in this pass. Everything
changed is below tmp_path."""
import os
import stat
import time
from pathlib import Path

import pytest

from sandbox_ai_app import files, remote  # noqa: F401

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402,F401

from sandbox_ai_app import file_editor, files_page, widgets  # noqa: E402
from test_files_page import FakeSai, finish, names, press, select, settle, setup  # noqa: E402,F401
from test_files_review_p1 import connect  # noqa: E402

HERE = Path(__file__).resolve().parent


# -- a remote file nobody may write --

def test_a_remote_file_nobody_may_write_is_not_replaced_by_a_save(tmp_path):
    """A local save refuses a file you may not write ("read-only for you ... Change its permissions first"); a remote
    one renamed the new contents over a file of mode 0444 - its read-only bits were no protection there."""
    served = tmp_path / "srv"
    served.mkdir()
    (served / "locked.txt").write_text("keep me")
    (served / "locked.txt").chmod(0o444)
    root = connect(served)
    try:
        read = root.read_file("locked.txt")
        with pytest.raises(files.Refused, match="read-only"):
            root.write_file("locked.txt", b"changed", read.identity)
        with pytest.raises(files.Refused, match="read-only"):
            root.write_file("locked.txt", b"changed")  # (Replace after a question: still refused)
        assert (served / "locked.txt").read_text() == "keep me"
        assert os.listdir(served) == ["locked.txt"], "no temporary file left"
        # (kept so) a file its owner may write is saved; its bits stay
        (served / "locked.txt").chmod(0o644)
        root.write_file("locked.txt", b"changed")
        assert (served / "locked.txt").read_text() == "changed"
        assert stat.S_IMODE((served / "locked.txt").stat().st_mode) == 0o644
    finally:
        root.close()


# -- a file removed or renamed while it was edited --

def editor(tmp_path, wait, name="notes.txt", text="one\n"):
    (tmp_path / name).write_text(text)
    window = file_editor.EditorWindow(None, files.Root(tmp_path), name, str(tmp_path / name))
    assert wait(lambda: not window.loading, 10)
    return window


def test_a_save_of_a_file_removed_meanwhile_says_it_is_gone(tmp_path, qapp, wait, monkeypatch):
    """The file was removed (or renamed, or moved to the other pane) while it was edited: Save asked "Replace the
    changed file? ... changed on disk since it was opened (the agent or another program wrote it) ... Reload shows what
    is on disk now" - there is nothing on disk to replace or reload, and Replace made it anew at its old place. The
    question now says it is gone, and what Save does then."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # (No: nothing is made)
    monkeypatch.setattr(widgets, "SHOWN", [])
    window = editor(tmp_path, wait)
    window.text.selectAll()
    window.text.insertPlainText("two\n")
    os.rename(tmp_path / "notes.txt", tmp_path / "renamed.txt")
    window.save()
    assert wait(lambda: not window.saving, 10)
    assert widgets.SHOWN, "asked first"
    title, text, _details = widgets.SHOWN[-1]
    assert "no longer there" in text and "changed on disk" not in text and "Reload" not in text, text
    assert not (tmp_path / "notes.txt").exists() and window.dirty
    assert "no longer there" in window.notice.text()
    # Yes: saved anew where it was (its own bits)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    window.save()
    assert wait(lambda: not window.saving and (tmp_path / "notes.txt").exists(), 10)
    assert (tmp_path / "notes.txt").read_text() == "two\n" and not window.dirty
    assert (tmp_path / "renamed.txt").read_text() == "one\n"
    window.close()


def test_a_save_of_a_file_changed_meanwhile_still_asks_to_replace_it(tmp_path, qapp, wait, monkeypatch):
    """(kept so) a file written meanwhile is still asked about as changed - Replace, or Reload to see it."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    monkeypatch.setattr(widgets, "SHOWN", [])
    window = editor(tmp_path, wait)
    window.text.selectAll()
    window.text.insertPlainText("two\n")
    (tmp_path / "notes.txt").write_text("the agent's, longer\n")
    window.save()
    assert wait(lambda: not window.saving, 10)
    assert "changed on disk" in widgets.SHOWN[-1][1] or widgets.SHOWN[-1][0] == "Replace the changed file?"
    assert (tmp_path / "notes.txt").read_text() == "the agent's, longer\n"
    window.dirty = False
    window.close()


def test_a_remote_file_removed_meanwhile_is_said_to_be_gone(tmp_path):
    """As a local one: files.Gone (a Changed), not "changed on disk"."""
    served = tmp_path / "srv"
    served.mkdir()
    (served / "notes.txt").write_text("old")
    root = connect(served)
    try:
        read = root.read_file("notes.txt")
        (served / "notes.txt").unlink()
        with pytest.raises(files.Gone):
            root.write_file("notes.txt", b"mine", read.identity)
        assert os.listdir(served) == []
    finally:
        root.close()


def test_a_local_file_removed_meanwhile_is_said_to_be_gone(tmp_path):
    root = files.Root(tmp_path)
    (tmp_path / "notes.txt").write_text("old")
    read = root.read_file("notes.txt")
    (tmp_path / "notes.txt").unlink()
    with pytest.raises(files.Gone):
        root.write_file("notes.txt", b"mine", read.identity)
    assert issubclass(files.Gone, files.Changed)
    assert os.listdir(tmp_path) == []


# -- a move whose source was kept --

def test_a_move_whose_source_was_kept_does_not_say_moved(setup, wait):
    """A folder with a link in it moved out of the sandbox's folder: copied (the link skipped), its source kept - and
    the report began "2 file(s) in 1 folder(s), 9 B moved". It says copied now, the source kept."""
    page, ws, host, outside = setup
    (ws / "proj").mkdir()
    (ws / "proj" / "a.txt").write_text("alpha")
    (ws / "proj" / "link").symlink_to(outside)
    page.left.list()
    select(page.left, "proj")
    page.activate(page.left)
    page.copy_selected(page.left, move=True)
    report = "\n".join(finish(page, wait))
    assert (ws / "proj" / "a.txt").exists() and (host / "proj" / "a.txt").exists(), "copied, the source kept"
    assert "moved" not in report.split("\n")[0], report
    assert "source" in report.split("\n")[0] and "copied" in report.split("\n")[0], report


def test_a_move_that_moved_still_says_moved(setup, wait):
    """(kept so) a move that removed its source says moved."""
    page, ws, host, _ = setup
    select(page.left, "sub")
    page.copy_selected(page.left, move=True)
    report = finish(page, wait)
    assert not (ws / "sub").exists() and (host / "sub" / "b.txt").exists()
    assert "moved" in report[0], report


def test_a_report_of_moved_and_kept_items_names_both(qapp, setup):
    page, *_ = setup
    moved = files.Result(files=2, directories=1, bytes=10, moved=True)
    kept = files.Result(files=1, directories=1, bytes=5, kept="The source was kept: 1 item(s) were not copied.")
    lines = page.report([moved, kept], [], moved=True)
    assert "moved" in lines[0] and "2 file" in lines[0], lines
    assert "1 file" in lines[1] and "copied" in lines[1] and "moved" not in lines[1], lines


# -- a cut whose paste was not done --

def test_a_cut_stays_on_the_clipboard_until_its_move_begins(setup, wait, monkeypatch):
    """Ctrl+X, then Ctrl+V where a name exists ("Replace existing files" ticked): on a file system slower than a moment
    (or a remote target) the question came after the paste had returned - and the cut was dropped from the page and
    the system clipboard already. Answered No, nothing was moved, and a second Ctrl+V had nothing to paste."""
    from PySide6.QtGui import QGuiApplication
    page, ws, host, _ = setup
    (host / "a.txt").write_text("the host's a")
    page.right.list()
    page.replace.setChecked(True)
    select(page.left, "a.txt")
    assert page.copy_items(page.left, cut=True)
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.01)
    real = files.exists_in

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)
    monkeypatch.setattr(files, "exists_in", slow)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # (No: leave it)
    page.paste(page.right)
    assert wait(lambda: any("Replace" in str(title) for title, *_ in widgets.SHOWN), 10), "asked"
    assert wait(lambda: not page.task.running, 10)
    assert (ws / "a.txt").exists() and (host / "a.txt").read_text() == "the host's a"
    assert page.clipboard is not None and page.clipboard.cut, "the cut is still there"
    assert page.clipboard.owns(QGuiApplication.clipboard().mimeData())
    # (kept so) once the move has begun, the cut is gone: a second paste has nothing to move
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.paste(page.right)
    assert wait(lambda: not (ws / "a.txt").exists(), 10)
    finish(page, wait)
    assert (host / "a.txt").read_text() == "alpha" and page.clipboard is None


# -- a recent folder of a sandbox's folder on a slow file system --

def test_a_recent_folder_of_a_root_opened_slowly_is_shown(qapp, tmp_path, monkeypatch, wait):
    """Recent in the sandbox pane, a folder of another sandbox's folder: that folder is opened in a thread when it
    does not answer at once - Recent gave up then (the pane had no root yet) and showed the folder's top, not the folder
    chosen."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    one, two = tmp_path / "one", tmp_path / "two"
    (one / "a").mkdir(parents=True)
    (two / "deep" / "er").mkdir(parents=True)
    one, two = str(one.resolve()), str(two.resolve())
    page = files_page.FilesPage(FakeSai([("run", "claude-one", one), ("run", "claude-two", two)]))
    assert wait(lambda: page.roots.count() > 1 and not page.roots_reading, 10)
    pane = page.left
    pane.roots.setCurrentIndex(pane.roots.findData(two))
    settle(pane)
    pane.go("deep/er")
    settle(pane)
    pane.roots.setCurrentIndex(pane.roots.findData(one))
    settle(pane)
    assert pane.root.path == one and (two, "deep/er") in pane.recent
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.01)
    real = files.Root

    class Slow(real):
        def __init__(self, path, *args, **kwargs):
            if str(path) == two:
                time.sleep(0.3)
            super().__init__(path, *args, **kwargs)
    monkeypatch.setattr(files, "Root", Slow)
    pane.go_recent((two, "deep/er"))
    assert wait(lambda: pane.root is not None and pane.root.path == two and pane.listing is None, 10)
    assert pane.shown == "deep/er" and pane.relative == "deep/er"


# -- undone to what was saved --

def test_changes_undone_to_what_was_saved_leave_nothing_to_discard(tmp_path, qapp, wait, monkeypatch):
    """Typed, then undone to the text as it was read: the window still had changes not saved ("* " in its title) and
    asked whether to discard them when it was closed."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    monkeypatch.setattr(widgets, "SHOWN", [])
    window = editor(tmp_path, wait)
    window.text.moveCursor(window.text.textCursor().MoveOperation.End)
    window.text.insertPlainText("more")
    assert window.dirty and window.windowTitle().startswith("* ")
    window.text.undo()
    assert window.contents() == b"one\n"
    assert not window.dirty and not window.windowTitle().startswith("* ")
    assert window.may_close() and not widgets.SHOWN, "nothing asked"
    # (kept so) typed again: changes to discard
    window.text.insertPlainText("x")
    assert window.dirty and not window.may_close()
    window.dirty = False
    window.close()


def test_a_save_then_undone_to_the_text_before_it_has_changes(tmp_path, qapp, wait, monkeypatch):
    """(kept so) saved, then undone to the text before the save: that is not what is on disk - changes not saved."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    window = editor(tmp_path, wait)
    window.text.moveCursor(window.text.textCursor().MoveOperation.End)
    window.text.insertPlainText("more")
    window.save()
    assert wait(lambda: not window.saving and not window.dirty, 10)
    window.text.undo()
    assert window.contents() == b"one\n" and window.dirty
    window.dirty = False
    window.close()


# -- a copy of a folder beside it --

def test_a_folder_pasted_beside_itself_keeps_its_whole_name(setup, wait):
    """A folder with a dot in its name pasted into its own folder was named like a file's copy: "src (copy).d" - its
    name cut at the dot as if ".d" were an extension. A folder's copy is "src.d (copy)"; a file's stays "a (copy).txt"."""
    page, ws, host, _ = setup
    (host / "src.d").mkdir()
    (host / "src.d" / "x").write_text("x")
    (host / "b.txt").write_text("b")
    page.right.list()
    select(page.right, "src.d", "b.txt")
    assert page.copy_items(page.right)
    page.paste(page.right)
    finish(page, wait)
    assert (host / "src.d (copy)" / "x").read_text() == "x", sorted(os.listdir(host))
    assert (host / "b (copy).txt").read_text() == "b"
    assert not (host / "src (copy).d").exists()
