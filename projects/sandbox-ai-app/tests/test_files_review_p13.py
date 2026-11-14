"""Review pass 13 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
On a file system that hangs, every try (F5 again, Properties again, the path typed again) left one more thread waiting
for good and held up the window for a moment each time; Apply in the Properties dialog on a slow or remote file system
went on beside the page's jobs (a copy or move started meanwhile worked on what it changed); an agent's file copied
with Ctrl+C was gone for other programs after a few drags (its copy handed out was removed); a copy to or from a remote
system failed as a whole when something in it was removed meanwhile (a local one names it and copies the rest), and a
local one when that happened right after the look; a job refused while another ran spoke of "another copy". Everything
changed is below tmp_path."""
import os
import threading
import time

import pytest

from sandbox_ai_app import files

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401
from test_files_review_p11 import hanging  # noqa: E402,F401


def answer_threads():
    return [thread for thread in threading.enumerate() if thread.name == "files-answer" and thread.is_alive()]


# -- a file system that hangs, tried again and again --

def test_a_file_system_that_hangs_is_waited_for_once_not_at_every_try(qapp, tmp_path, monkeypatch, wait, hanging):
    """Pass 12 looked at a host folder in a thread and waited half a second for it - but each look started a thread of
    its own: on a hard NFS mount every F5, Properties, paste or path typed again left one more thread waiting for good,
    and held up the window for half a second again. A look of the same thing still on its way is waited for a moment,
    not started anew."""
    gate, place, _calls = hanging
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    ws, hung, host = tmp_path / "ws", tmp_path / "hung", tmp_path / "host"
    for path in (ws, hung / "sub", host):
        path.mkdir(parents=True)
    (ws / "a.txt").write_text("a")
    (hung / "sub" / "x.txt").write_text("x")
    page = files_page.FilesPage(FakeSai([("run", "claude-abc", str(ws))]))
    try:
        assert wait(lambda: page.roots.count() > 0 and not page.roots_reading, 10)
        right, left = page.right, page.left
        assert wait(lambda: left.root is not None and left.listing is None, 10)
        assert right.go_path(str(host))
        settle(right)
        assert page.identities, "the sandboxes' folders' identities are known: a host folder is looked at"
        place["hung"] = str(hung)
        before = len(answer_threads())
        target = files_page.host_relative(str(hung / "sub"))
        select(left, "a.txt")
        started = time.monotonic()
        for _ in range(6):
            assert right.untrusted_at(target, unknown=None) is None
            clip = files_page.Clip(left.root, [("a.txt", "a.txt", None)], True, str(ws))
            assert page.transfer_items(clip, right, target) is False
            assert "does not answer" in page.message.text()
            found, problems = page.host_items([str(hung / "sub" / "x.txt")])
            assert not found.items and problems
            assert page.through_sandbox_link(str(hung / "sub"))
        spent = time.monotonic() - started
        assert len(answer_threads()) - before <= 3, len(answer_threads()) - before
        assert spent < 4, f"the window was held up {spent:.1f} s"
    finally:
        gate.set()
        page.deleteLater()
    assert wait(lambda: not answer_threads(), 10), "they end once it answers"


def test_a_look_that_answered_is_made_anew_the_next_time(qapp, monkeypatch):
    """Keeps it so: only a look still on its way is shared - one that answered is asked again (the folder may be
    another by then), and a look of something else is made beside it."""
    calls = []

    def work():
        calls.append(1)
        return len(calls)
    assert files_page.answered(work, key=("k",)) == (True, 1)
    assert files_page.answered(work, key=("k",)) == (True, 2)
    gate = threading.Event()
    assert files_page.answered(lambda: gate.wait(10), wait=0.05, key=("slow",)) == (False, None)
    assert files_page.answered(lambda: "other", key=("other",)) == (True, "other")
    gate.set()
    time.sleep(0.3)
    assert files_page.answered(lambda: "again", key=("slow",)) == (True, "again")


# -- Apply in the Properties dialog: a job of the page --

def test_properties_apply_on_a_slow_file_system_is_a_job_of_the_page(setup, wait, monkeypatch):
    """Apply waits for a job of the page (pass 12) - but Apply itself was none: on a file system that hangs (or a remote
    system, always in a thread) it went on in the background, the page's buttons and keys stayed as they were, and a
    copy, move or delete started meanwhile worked on what it was changing. It runs as the page's job now (its busy bar,
    Cancel), as the page's own Permissions."""
    page, _ws, host, _ = setup
    (host / "a.txt").write_text("a")
    os.chmod(host / "a.txt", 0o644)
    right, left = page.right, page.left
    right.list()
    settle(right)
    dialog = page.properties(right, [right.by_name["a.txt"]])
    assert dialog is not None
    gate, real = threading.Event(), files.Root.chmod_entry

    def slow(self, relative, *args, **kwargs):
        gate.wait(10)
        return real(self, relative, *args, **kwargs)
    monkeypatch.setattr(files.Root, "chmod_entry", slow)
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.1)
    try:
        dialog.checks[4].setChecked(True)  # (the group's write)
        assert dialog.apply() is not False
        assert page.task.running, "a job of the page: its buttons greyed out, Cancel"
        select(left, "a.txt")
        assert page.copy_selected(left) is False and "Wait until" in page.message.text()
        assert not (host / "a.txt").stat().st_mode & 0o020
    finally:
        gate.set()
        finish(page, wait)
    assert wait(lambda: os.stat(host / "a.txt").st_mode & 0o777 == 0o664, 10)
    assert wait(lambda: "Permissions of 1 item(s) changed." in dialog.message.text(), 10)
    dialog.close()


def test_properties_apply_keeps_working_without_a_job_running(setup, wait):
    """Keeps it so: on a file system that answers, Apply is done at once and says so; closed meanwhile is fine."""
    page, _ws, host, _ = setup
    (host / "b.txt").write_text("b")
    os.chmod(host / "b.txt", 0o600)
    right = page.right
    right.list()
    settle(right)
    dialog = page.properties(right, [right.by_name["b.txt"]])
    dialog.checks[3].setChecked(True)  # (the group's read)
    assert dialog.apply() is not False
    assert wait(lambda: os.stat(host / "b.txt").st_mode & 0o777 == 0o640, 10)
    assert "Permissions of 1 item(s) changed." in dialog.message.text()
    assert not page.task.running and not dialog.applying
    dialog.close()


# -- an agent's file on the clipboard, handed out to other programs --

def test_an_agents_file_on_the_clipboard_stays_there_for_other_programs_after_drags(setup, wait, monkeypatch):
    """An agent's file goes to other programs as a copy in a folder of the app's own, of which the last EXPORT_KEPT
    stay - but a drag (also one between the panes) made one more each time: after a few, the copy the clipboard named
    was removed, and a paste in another program found no file. The copy on the clipboard stays while it is there."""
    page, ws, _host, _ = setup
    left = page.left
    select(left, "a.txt")
    clipboard = QtWidgets.QApplication.clipboard()
    assert page.copy_items(left)
    assert wait(lambda: len(clipboard.mimeData().urls()) == 1, 10)
    handed = clipboard.mimeData().urls()[0].toLocalFile()
    assert os.path.isfile(handed) and handed != str(ws / "a.txt")
    entry = left.by_name["a.txt"]
    for _ in range(files_page.EXPORT_KEPT + 2):
        assert page.export_urls(left, [entry])  # (what each drag hands out)
    assert os.path.isfile(handed), "the copy the clipboard names is still there"
    assert open(handed).read() == "alpha"
    # once the clipboard holds another one, the old copy goes with the others
    select(left, "sub")
    page.copy_items(left)
    for _ in range(files_page.EXPORT_KEPT + 2):
        page.export_urls(left, [entry])
    assert not os.path.exists(handed)


# -- copies with a remote side: what goes away while they are made --

def test_an_upload_skips_what_was_removed_while_it_was_made(tmp_path, monkeypatch):
    """A local copy names a file removed while its folder is copied ("removed while it was copied") and copies the rest
    - a copy to a remote system failed as a whole ("No such file or directory"), everything it had sent removed again:
    an agent's folder that changes while it is copied (a build, an editor's temporary file) could not be sent at all."""
    from test_remote import connect
    served, ws = tmp_path / "served", tmp_path / "ws"
    served.mkdir()
    (ws / "proj" / "deep").mkdir(parents=True)
    for name in ("a.txt", "gone.txt", "z.txt"):
        (ws / "proj" / name).write_text(name)
    (ws / "proj" / "deep" / "d.txt").write_text("d")
    (ws / "proj" / "gone-folder").mkdir()
    real = files._names

    def names_then_removed(fd, budget=None):
        found = real(fd, budget)
        if "gone.txt" in found:
            os.unlink(ws / "proj" / "gone.txt")
            os.rmdir(ws / "proj" / "gone-folder")
        return found
    monkeypatch.setattr(files, "_names", names_then_removed)
    root = connect(served)
    try:
        result = files.copy_entry(files.Root(ws), "proj", root, "")
    finally:
        root.close()
    assert sorted(path for path, _ in result.skipped) == ["proj/gone-folder", "proj/gone.txt"]
    assert all(why == "removed while it was copied" for _, why in result.skipped)
    assert (served / "proj" / "a.txt").read_text() == "a.txt" and (served / "proj" / "z.txt").exists()
    assert (served / "proj" / "deep" / "d.txt").read_text() == "d"


def test_a_download_skips_what_was_removed_while_it_was_made(tmp_path, monkeypatch):
    """The same from a remote system - one of sandbox-ai's hosts, where agents change their files as they work: a file
    or folder listed, then removed before it was read, failed the whole copy (and a move kept nothing of it)."""
    from sandbox_ai_app import remote
    from test_remote import connect
    served, host = tmp_path / "served", tmp_path / "host"
    host.mkdir()
    (served / "proj" / "deep").mkdir(parents=True)
    for name in ("a.txt", "gone.txt", "z.txt"):
        (served / "proj" / name).write_text(name)
    (served / "proj" / "deep" / "d.txt").write_text("d")
    (served / "proj" / "gone-folder" / "inner").mkdir(parents=True)
    real = remote.RemoteRoot.listed

    def listed_then_removed(self, path, budget):
        found = real(self, path, budget)
        if path.endswith("/proj") and (served / "proj" / "gone.txt").exists():
            os.unlink(served / "proj" / "gone.txt")
            os.rmdir(served / "proj" / "gone-folder" / "inner")
            os.rmdir(served / "proj" / "gone-folder")
        return found
    monkeypatch.setattr(remote.RemoteRoot, "listed", listed_then_removed)
    root = connect(served)
    try:
        result = files.copy_entry(root, "proj", files.Root(host, sandbox=False), "")
        assert sorted(path for path, _ in result.skipped) == ["proj/gone-folder", "proj/gone.txt"]
        assert all(why == "removed while it was copied" for _, why in result.skipped)
        assert (host / "proj" / "a.txt").read_text() == "a.txt" and (host / "proj" / "z.txt").exists()
        assert (host / "proj" / "deep" / "d.txt").read_text() == "d"
        assert not (host / "proj" / "gone-folder").exists(), "nothing of what went away"
        assert not [p for p in host.iterdir() if p.name.endswith(".part")]
        # a move keeps its source then (something was skipped)
        (served / "proj" / "gone.txt").write_text("again")
        (served / "proj" / "gone-folder" / "inner").mkdir(parents=True)
        moved = files.move_entry(root, "proj", files.Root(host, sandbox=False), "", overwrite=True)
        assert moved.kept and (served / "proj" / "a.txt").exists()
    finally:
        root.close()


def test_a_local_copy_skips_a_file_or_folder_gone_between_the_look_and_the_opening(tmp_path, monkeypatch):
    """A local copy named what was gone when it was looked at - one gone right after (between its lstat and its
    opening) failed the whole copy."""
    ws, host = tmp_path / "ws", tmp_path / "host"
    (ws / "proj" / "sub").mkdir(parents=True)
    host.mkdir()
    (ws / "proj" / "a.txt").write_text("a")
    (ws / "proj" / "b.txt").write_text("b")
    real_open, real_dir = files._open_regular, files.Root._open_child_dir

    def gone_file(name, dir_fd=None):
        if name == "a.txt":
            raise FileNotFoundError(2, "No such file or directory", name)
        return real_open(name, dir_fd)

    def gone_folder(self, parent, name):
        if name == "sub":
            raise FileNotFoundError(2, "No such file or directory", name)
        return real_dir(self, parent, name)
    monkeypatch.setattr(files, "_open_regular", gone_file)
    monkeypatch.setattr(files.Root, "_open_child_dir", gone_folder)
    result = files.copy_entry(files.Root(ws), "proj", files.Root(host, sandbox=False), "")
    assert sorted(result.skipped) == [("proj/a.txt", "removed while it was copied"),
                                      ("proj/sub", "removed while it was copied")]
    assert (host / "proj" / "b.txt").read_text() == "b"


def test_a_job_refused_while_another_runs_says_which_kind_waits(setup, wait):
    """A delete or an owner change confirmed while another job ran (its question blocks nothing: a Properties Apply, a
    delete began meanwhile) said "Another copy still runs." - about a copy there was none of. It says what waits."""
    page, _ws, host, _ = setup
    (host / "c.txt").write_text("c")
    right = page.right
    right.list()
    settle(right)
    gate = threading.Event()
    assert page.run_job([right.root], lambda cancelled: gate.wait(10), lambda *_: None, "a job")
    try:
        chosen = files_page.Chosen.of(right, [right.by_name["c.txt"]])
        assert not page.delete_items(chosen)
        assert "Another copy" not in page.message.text() and "Wait until" in page.message.text()
        assert (host / "c.txt").exists()
    finally:
        gate.set()
        finish(page, wait)


# -- the file manager's own dialogs and menus made for one use: freed once answered --

def test_the_context_menu_and_the_owner_dialog_go_once_answered(setup, qapp, monkeypatch):
    """Stability round 2 frees every dialog and menu made anew for one use (widgets.exec_dropped) and left the file
    manager's to its review: each Owner… left its OwnerDialog behind as a child of the page, for as long as the window
    lived; the context menu (with its submenu) goes the same way."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QDialog, QMenu
    from test_stability_round2 import close_soon, settle as drop
    page, _ws, host, _ = setup
    (host / "d.txt").write_text("d")
    right = page.right
    right.list()
    settle(right)
    select(right, "d.txt")
    drop(qapp)
    menus = len(page.findChildren(QMenu))
    for _ in range(3):
        close_soon(qapp, QMenu, lambda menu: menu.close())
        page.context_menu(right, QPoint(4, 4))
    drop(qapp)
    assert len(page.findChildren(QMenu)) == menus
    monkeypatch.setattr(files_page.OwnerDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    owners = len(page.findChildren(files_page.OwnerDialog))
    for _ in range(3):
        assert page.chown_dialog(right, [right.by_name["d.txt"]]) is False
    drop(qapp)
    assert len(page.findChildren(files_page.OwnerDialog)) == owners


def test_every_dialog_and_menu_of_the_file_manager_made_for_one_use_is_freed():
    """Stability round 2's guard, for the file manager's own files too (it left them to this review)."""
    from pathlib import Path
    from test_stability_round2 import made_and_run
    app = Path(files_page.__file__).resolve().parent
    left = {name: found for name in ("files_page.py", "file_editor.py", "file_properties.py", "markdown_view.py",
                                      "remote.py", "files.py") for found in [made_and_run(app / name)] if found}
    assert left == {}
