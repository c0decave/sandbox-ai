"""Review pass 12 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A host folder on a file system that hangs held up the whole window: a copy into the host pane while it was still
listing it, its Properties, the path typed in the path bar, a link opened that points there and files of another
program pasted from there were looked at on the GUI thread; Apply in the Properties dialog changed permissions while a
copy ran (the page's own Permissions waits for it). Everything changed is below tmp_path."""
import os
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401
from test_files_review_p11 import hanging  # noqa: E402,F401


# -- a host folder on a file system that hangs, looked at on the GUI thread --

def test_a_host_folder_that_hangs_holds_up_no_window(qapp, tmp_path, monkeypatch, wait, hanging):
    """Pass 11 kept a project that hangs off the GUI thread - but a host folder of one (sshfs whose server went away,
    NFS) was looked at there still: whether it lies in a sandbox's folder (each folder on its way, by its identity) when
    something was copied into the host pane while it listed it, or its Properties were asked for; its real path when it
    was typed; where a link to it points; the files another program put on the clipboard from there. Each held up the
    whole window until the file system answered - for a hard NFS mount, for good. What does not answer within a moment
    counts as an agent's where that is the safe side (refused, never executable); a copy into it is refused (into an
    agent's folder, the agent's files keep their executable bits)."""
    gate, place, calls = hanging
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    ws, hung, host = tmp_path / "ws", tmp_path / "hung", tmp_path / "host"
    for path in (ws, hung / "sub", host):
        path.mkdir(parents=True)
    (ws / "a.txt").write_text("a")
    (hung / "sub" / "x.txt").write_text("x")
    (host / "to-hung").symlink_to(hung / "sub")
    page = files_page.FilesPage(FakeSai([("run", "claude-abc", str(ws))]))
    try:
        assert wait(lambda: page.roots.count() > 0 and not page.roots_reading, 10)
        right, left = page.right, page.left
        assert wait(lambda: left.root is not None and left.listing is None, 10)
        assert right.go_path(str(host))
        settle(right)
        place["hung"] = str(hung)
        started = time.monotonic()
        # the path typed: not resolved on the GUI thread (as typed when it does not answer: nothing is followed then)
        right.go_path(str(hung / "sub"))
        assert right.relative == files_page.host_relative(str(hung / "sub"))
        assert right.untrusted(), "not known to lie outside every sandbox's folder: as an agent's"
        props = page.properties(right, [])
        assert props is not None
        # a copy into it: refused - as an agent's folder, what came from the sandbox's folder would keep its executable
        # bits there (no copy "out"); as the human's, it is not known to be
        select(left, "a.txt")
        assert right.untrusted_at(right.relative, unknown=None) is None
        assert page.copy_selected(left) is False and "does not answer" in page.message.text()
        assert not page.task.running
        clip, problems = page.host_items([str(hung / "sub" / "x.txt")])
        assert not clip.items and problems
        assert time.monotonic() - started < 10
        assert not calls, calls
    finally:
        gate.set()
        wait(lambda: not page.task.running, 30)
        page.deleteLater()


def test_a_link_into_a_folder_that_hangs_holds_up_no_window(qapp, tmp_path, monkeypatch, wait, hanging):
    """A link of the host's opened (a double click): where it points was read on the GUI thread."""
    gate, place, calls = hanging
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    ws, hung, host = tmp_path / "ws", tmp_path / "hung", tmp_path / "host"
    for path in (ws, hung / "sub", host):
        path.mkdir(parents=True)
    (host / "to-hung").symlink_to(hung / "sub")
    page = files_page.FilesPage(FakeSai([("run", "claude-abc", str(ws))]))
    try:
        assert wait(lambda: page.roots.count() > 0 and not page.roots_reading, 10)
        right = page.right
        assert right.go_path(str(host))
        settle(right)
        place["hung"] = str(hung)
        right.open_link(right.by_name["to-hung"])
        assert "answer" in page.message.text() or "not followed" in page.message.text()
        assert not calls, calls
    finally:
        gate.set()
        page.deleteLater()


def test_a_host_folder_that_answers_is_still_looked_at_as_before(setup, tmp_path, wait):
    """Keeps it so: a path typed through a link of the host's goes to the folder it leads to (into a sandbox's folder:
    shown as the agent's), one through a link of the agent's is refused; a link to a sandbox's folder opened is not
    followed; a folder in a sandbox's is the agent's."""
    page, ws, host, _ = setup
    (host / "real").mkdir()
    (host / "via").symlink_to(host / "real")
    (host / "to-ws").symlink_to(ws)
    right = page.right
    assert right.go_path(str(host / "via"))
    settle(right)
    assert right.path_text() == str(host / "real") and not right.untrusted()
    assert right.go_path(str(host / "to-ws" / "sub"))
    settle(right)
    assert right.path_text() == str(ws / "sub") and right.untrusted()
    assert not right.go_path(str(ws / "escape"))
    assert "not followed" in page.message.text()
    assert right.go_path(str(host))
    settle(right)
    right.open_link(right.by_name["to-ws"])
    assert "not followed" in page.message.text() and right.path_text() == str(host)
    right.open_link(right.by_name["via"])
    settle(right)
    assert right.path_text() == str(host / "real")
    assert page.below_sandbox(str(ws / "sub")) and not page.below_sandbox(str(host / "real"))


# -- Apply in the Properties dialog while a job runs --

def test_properties_apply_waits_for_the_job_running(setup, wait):
    """Rename, New folder and Permissions answered while a copy runs wait for it (pass 11) - Apply in the Properties
    dialog (a window of its own, its button never greyed out) changed the bits in the middle of it all the same."""
    page, _ws, host, _ = setup
    (host / "a.txt").write_text("a")
    os.chmod(host / "a.txt", 0o644)
    right = page.right
    right.list()
    settle(right)
    dialog = page.properties(right, [right.by_name["a.txt"]])
    assert dialog is not None
    gate = threading.Event()
    assert page.run_job([right.root], lambda cancelled: gate.wait(10), lambda *_: None, "a job")
    try:
        dialog.checks[4].setChecked(True)  # (the group's write)
        assert not dialog.apply()
        assert "Wait until" in dialog.message.text()
        assert os.stat(host / "a.txt").st_mode & 0o777 == 0o644
    finally:
        gate.set()
        finish(page, wait)
    assert dialog.apply() is not False, "once it has ended"
    assert wait(lambda: os.stat(host / "a.txt").st_mode & 0o777 == 0o664, 10)
    dialog.close()


# -- Ctrl+C of a host folder: looked through without holding up the window --

def test_ctrl_c_of_a_host_folder_holds_up_no_window(setup, wait, monkeypatch):
    """Before a host folder goes on the system clipboard as a file, it is looked through for a sandbox's folder (pass
    11) - on the GUI thread's watch: every Ctrl+C or Ctrl+X of a big folder held up the window for up to EXPORT_WAIT,
    also when it was only pasted in the other pane. The paths go on the clipboard at once, the folder as a file once it
    is looked through (if the clipboard still holds what was copied then); what the page pastes is the same."""
    page, _ws, host, _ = setup
    (host / "big" / "deep").mkdir(parents=True)
    (host / "note.txt").write_text("mine")
    gate, real = threading.Event(), files_page.holds_agents

    def slow(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(files_page, "holds_agents", slow)
    monkeypatch.setattr(files_page, "EXPORT_WAIT", 8)
    right = page.right
    right.list()
    settle(right)
    clipboard = QtWidgets.QApplication.clipboard()
    try:
        select(right, "big", "note.txt")
        started = time.monotonic()
        assert page.copy_items(right)
        assert time.monotonic() - started < 2, "the window was held up"
        assert page.pasted()[0] == "clip"
        assert [os.path.basename(url.toLocalFile()) for url in clipboard.mimeData().urls()] == ["note.txt"]
        gate.set()
        assert wait(lambda: len(clipboard.mimeData().urls()) == 2, 10)
        assert sorted(os.path.basename(url.toLocalFile()) for url in clipboard.mimeData().urls()) == \
            ["big", "note.txt"]
        assert page.pasted()[0] == "clip" and page.pasted()[1] is page.clipboard
        assert clipboard.mimeData().text().splitlines() == [str(host / "big"), str(host / "note.txt")]
        # copied again meanwhile: the late look gives the clipboard nothing of the copy before
        gate.clear()
        select(right, "big")
        assert page.copy_items(right)
        select(right, "note.txt")
        assert page.copy_items(right)
        gate.set()
        time.sleep(0.3)
        QtWidgets.QApplication.processEvents()
        assert [os.path.basename(url.toLocalFile()) for url in clipboard.mimeData().urls()] == ["note.txt"]
        assert clipboard.mimeData().text() == str(host / "note.txt")
    finally:
        gate.set()


# -- a kept identity met again by chance: a target --

def test_a_folder_that_only_a_kept_identity_makes_an_agents_gets_the_agents_files_as_they_go_out(setup, wait):
    """The identities of sandboxes' folders are kept for 90 days (pass 11) - a device number is used again (a USB stick
    in place of another, disks numbered anew after a reboot) and an inode number of another file system is the same
    (the first folder of every fresh ext4 is inode 12): a folder of the human's then counts as an agent's. Where
    something is copied from that is the safe side; as a target it was not - a copy out of a sandbox's folder into it
    was no copy "out", and the agent's program arrived executable. A target counts as an agent's only where a
    sandbox's folder of now holds it."""
    page, ws, host, _ = setup
    mine = host / "mine"
    mine.mkdir()
    page.keep_identities({files_page.folder_identity(str(mine)): "/gone/ws"})
    right, left = page.right, page.left
    assert right.go_path(str(mine))
    settle(right)
    assert right.untrusted(), "as a source: the agent's"
    select(left, "run.sh")
    assert page.copy_selected(left)
    report = "\n".join(finish(page, wait))
    assert (mine / "run.sh").exists() and not os.stat(mine / "run.sh").st_mode & 0o111, report
    assert "run.sh" in report


def test_a_copy_into_a_sandboxs_folder_of_now_keeps_the_bits(setup, wait):
    """Keeps it so: into a folder of a sandbox's of now the agent's files go as they are (a copy within the sandboxes'
    folders drops no bit)."""
    page, ws, host, _ = setup
    right, left = page.right, page.left
    assert right.go_path(str(ws / "sub"))
    settle(right)
    select(left, "run.sh")
    assert page.copy_selected(left)
    finish(page, wait)
    assert os.stat(ws / "sub" / "run.sh").st_mode & 0o111
