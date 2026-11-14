"""Review pass 15 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A remote permission change could still be sent through a link: an agent that swapped a link and a file in turn had
lsetstat@openssh.com fail for the link and the look after it find the file - SETSTAT followed the link put back. A
remote pane's listings of folders gone through quickly on a slow link were refused as if the file system hung. An
Apply of Properties pressed in the moment between the late end of the one before and the dialog learning it took the
items' bits for unchanged, and its result was said to be the one before's. Ctrl+C of an agent's files given up half
way named only those copied by then. A folder moved out of the root while it was opened left its descriptor open.
Everything changed is below tmp_path."""
import os
import time

import pytest

from sandbox_ai_app import files, remote

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401
from test_files_review_p10 import lab  # noqa: E402,F401
from test_files_review_p14 import hung  # noqa: E402,F401


def open_descriptors():
    folder = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    return len(os.listdir(folder))


# -- a remote permission change: never through a link, also when the agent swaps link and file in turn --

def test_a_remote_permission_change_never_follows_a_link_swapped_in_and_out(tmp_path):
    """lsetstat@openssh.com failed for a link the agent had put at the item's path; the LSTAT after it found the file
    put back, and that was taken for a server whose lsetstat fails for every path (an older C library): SETSTAT came
    next, and went through the link the agent had put back in time - the human's key got 0666. Whether lsetstat
    works for what is no link is asked of "." (the remote HOME, no agent's): where it does, the item's failure is
    said, and nothing is sent through a link."""
    from test_remote import connect
    served = tmp_path / "served"
    served.mkdir()
    (served / "a.txt").write_text("a")
    os.chmod(served / "a.txt", 0o644)
    (served / "key").write_text("secret")
    os.chmod(served / "key", 0o600)

    def to_link():
        os.unlink(served / "a.txt")
        os.symlink(served / "key", served / "a.txt")

    def to_file():
        os.unlink(served / "a.txt")
        (served / "a.txt").write_text("a")
        os.chmod(served / "a.txt", 0o644)

    root = connect(served)
    try:
        real, state, sent = root.sftp.ask, {"failed": False}, []

        def racing(kind, payload, *args, **kwargs):
            item = b"a.txt" in payload
            sent.append((kind, item))
            if kind == remote.EXTENDED and b"lsetstat@openssh.com" in payload and item and not state["failed"]:
                to_link()  # (the agent: a link there as the change comes)
                try:
                    return real(kind, payload, *args, **kwargs)
                finally:
                    state["failed"] = True
                    to_file()  # (... and the file back for the look after it)
            if kind == remote.LSTAT and item and state["failed"]:
                try:
                    return real(kind, payload, *args, **kwargs)
                finally:
                    to_link()  # (... and the link again for what comes next)
            return real(kind, payload, *args, **kwargs)
        root.sftp.ask = racing
        with pytest.raises(OSError):
            root.chmod_entry("a.txt", 0o666)
        assert os.stat(served / "key").st_mode & 0o777 == 0o600, "the human's file kept its bits"
        assert (remote.SETSTAT, True) not in sent, "no SETSTAT of the item, which the server applies through a link"
    finally:
        root.close()


def test_a_server_whose_lsetstat_fails_everywhere_still_gets_setstat(tmp_path):
    """Keeps it so: OpenSSH's sftp-server on a C library older than glibc 2.32 (or in a chroot without /proc) fails
    lsetstat for every path - "." too: SETSTAT as before, asked of "." once per connection."""
    from test_remote import connect
    served = tmp_path / "served"
    served.mkdir()
    for name in ("a.txt", "b.txt"):
        (served / name).write_text(name)
        os.chmod(served / name, 0o600)
    root = connect(served)
    try:
        real, probes = root.sftp.ask, []

        def unsupported(kind, payload, *args, **kwargs):
            if kind == remote.EXTENDED and b"lsetstat@openssh.com" in payload:
                if payload.startswith(remote.pack_string("lsetstat@openssh.com") + remote.pack_string(".")):
                    probes.append(payload)
                raise OSError(95, "Operation not supported")
            return real(kind, payload, *args, **kwargs)
        root.sftp.ask = unsupported
        root.chmod_entry("a.txt", 0o644)
        root.chmod_entry("b.txt", 0o640)
        assert os.stat(served / "a.txt").st_mode & 0o777 == 0o644
        assert os.stat(served / "b.txt").st_mode & 0o777 == 0o640
        assert root.sftp.lsetstat_works is False and len(probes) == 1, "asked once"
    finally:
        root.close()


def test_a_remote_permission_change_where_lsetstat_works_keeps_working(tmp_path):
    """Keeps it so: a server whose lsetstat works sets the bits with it, and a real failure (not the owner) is said as
    it is, not sent again as SETSTAT."""
    from test_remote import connect
    served = tmp_path / "served"
    served.mkdir()
    (served / "a.txt").write_text("a")
    os.chmod(served / "a.txt", 0o600)
    home_mode = os.stat(served).st_mode
    root = connect(served)
    try:
        root.chmod_entry("a.txt", 0o640)
        assert os.stat(served / "a.txt").st_mode & 0o777 == 0o640
        assert root.sftp.lsetstat_works is True
        assert os.stat(served).st_mode == home_mode, "the HOME's bits as they were"
    finally:
        root.close()


def test_a_home_not_the_users_own_still_counts_as_lsetstat_that_works(tmp_path):
    """Keeps it so: "." refused with "Permission denied" (a HOME of root's: a chroot) - lsetstat reached the change
    itself there, so it works for what is no link; the item's failure is said, no SETSTAT follows."""
    from test_remote import connect
    served = tmp_path / "served"
    served.mkdir()
    (served / "a.txt").write_text("a")
    root = connect(served)
    try:
        real, sent = root.sftp.ask, []

        def answers(kind, payload, *args, **kwargs):
            sent.append(kind)
            if kind == remote.EXTENDED and b"lsetstat@openssh.com" in payload:
                if payload.startswith(remote.pack_string("lsetstat@openssh.com") + remote.pack_string(".")):
                    raise PermissionError(13, "Permission denied")
                raise OSError(5, "Failure")
            return real(kind, payload, *args, **kwargs)
        root.sftp.ask = answers
        with pytest.raises(OSError, match="Failure"):
            root.chmod_entry("a.txt", 0o640)
        assert root.sftp.lsetstat_works is True and remote.SETSTAT not in sent
    finally:
        root.close()


# -- a remote pane's listings on a slow link --

def test_a_remote_panes_folders_gone_through_quickly_on_a_slow_link_are_listed(lab, wait, monkeypatch, qapp):
    """Past LIST_STUCK listings still on their way after LIST_SLOW, a pane refused to list another folder: "N listings
    of this pane still wait for a file system that does not answer". A remote system's listings all end (a request
    without an answer ends the connection) - on a slow link the folders gone through one after the other (Backspace,
    a double click on each) were refused as if the file system hung."""
    page, root, home, _host = lab
    for number in range(8):
        (home / f"d{number}").mkdir()
        (home / f"d{number}" / f"in{number}.txt").write_text("x")
    left = page.left
    base = left.relative
    real = remote.RemoteRoot.list

    def slow(self, relative=""):
        time.sleep(0.4)
        return real(self, relative)
    monkeypatch.setattr(remote.RemoteRoot, "list", slow)
    monkeypatch.setattr(files_page, "LIST_SLOW", 0.05)
    for number in range(8):
        left.go(f"{base}/d{number}" if base else f"d{number}")
        time.sleep(0.08)
        qapp.processEvents()
    settle(left)
    assert left.shown.endswith("d7") and names(left) == ["in7.txt"], page.message.text()
    assert "still wait" not in page.message.text()


# -- Properties: an Apply pressed as the one before has just ended --

def test_apply_pressed_as_the_late_apply_before_has_ended_learns_it_first(setup, wait, monkeypatch, hung, qapp):
    """The Apply before, cancelled while the file system did not answer, has ended - and the dialog learns it with its
    next tick (late()). An Apply pressed in between started with the items' bits as before the late one, and late()
    then said of its result "The change of the permissions before took effect", and refreshed the panes again. What
    the one before set is known first; the next Apply is the human's again."""
    page, _ws, host, _ = setup
    (host / "p.txt").write_text("p")
    os.chmod(host / "p.txt", 0o600)
    right = page.right
    right.list()
    settle(right)
    dialog = page.properties(right, [right.by_name["p.txt"]])
    dialog.late_timer.setInterval(3600 * 1000)  # (its tick has not come yet)
    real = files.Root.chmod_entry

    def chmod_entry(self, relative, *args, **kwargs):
        if not hung.gate.is_set():
            hung.wait()
        return real(self, relative, *args, **kwargs)
    monkeypatch.setattr(files.Root, "chmod_entry", chmod_entry)
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.05)
    dialog.checks[3].setChecked(True)  # (the group's read: 0640)
    assert dialog.apply() is not False and page.task.running
    page.task.cancel()
    assert wait(lambda: not page.task.running, 5)
    assert dialog.late_timer.isActive()
    hung.gate.set()
    assert wait(lambda: dialog.pending["ended"].is_set(), 10)
    assert os.stat(host / "p.txt").st_mode & 0o777 == 0o640
    dialog.checks[4].setChecked(True)  # (the group's write as well: 0660)
    assert dialog.apply() is False, "what the one before set is learnt first"
    assert dialog.mode == 0o640 and "took effect" in dialog.message.text()
    assert not dialog.late_timer.isActive()
    assert dialog.apply_button.isEnabled()
    assert dialog.apply() is not False
    assert wait(lambda: os.stat(host / "p.txt").st_mode & 0o777 == 0o660, 10)
    assert wait(lambda: dialog.mode == 0o660, 10)
    assert "took effect" not in dialog.message.text() and not dialog.late_timer.isActive()
    dialog.close()


def test_apply_refused_before_it_began_leaves_apply_working(setup, wait, monkeypatch, qapp):
    """An Apply refused before its change began - LOCAL_STUCK steps still waiting for a file system that hangs (or a
    job of the page begun meanwhile) - was taken for one that still waits: every Apply after it said "The change of the
    permissions before still waits for the file system", for as long as the dialog was open, and late() ticked on."""
    page, _ws, host, _ = setup
    (host / "p.txt").write_text("p")
    os.chmod(host / "p.txt", 0o600)
    right = page.right
    right.list()
    settle(right)
    dialog = page.properties(right, [right.by_name["p.txt"]])
    dialog.checks[3].setChecked(True)
    monkeypatch.setitem(files_page._stuck_steps, "count", files_page.LOCAL_STUCK)
    dialog.apply()
    assert "still wait" in dialog.message.text() and os.stat(host / "p.txt").st_mode & 0o777 == 0o600
    monkeypatch.setitem(files_page._stuck_steps, "count", 0)  # (the file system answered: they ended)
    assert not dialog.late_timer.isActive()
    assert dialog.apply() is not False
    assert wait(lambda: os.stat(host / "p.txt").st_mode & 0o777 == 0o640, 10), dialog.message.text()
    assert wait(lambda: dialog.mode == 0o640, 10)
    dialog.close()


# -- Ctrl+C of an agent's files, given up half way --

def test_copies_aside_given_up_half_way_hand_out_none(tmp_path, monkeypatch):
    """copy_aside (an agent's files for the clipboard: copies, never the agent's own) given up after the first file
    gave the paths of those copied by then: Ctrl+C of three files whose copies took longer than EXPORT_WAIT put one or
    two on the clipboard, unsaid - a paste in another program brought part of them. Given up, none go."""
    ws, aside = tmp_path / "ws", tmp_path / "aside"
    ws.mkdir()
    aside.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (ws / name).write_text(name * 100)
    root = files.Root(ws)
    copied = []
    real = files.copy_entry

    def counting(*args, **kwargs):
        done = real(*args, **kwargs)
        copied.append(done)
        return done
    monkeypatch.setattr(files, "copy_entry", counting)
    with pytest.raises(files.Cancelled):
        files.copy_aside(root, "", ["a.txt", "b.txt", "c.txt"], lambda: str(aside), 1 << 20,
                         cancelled=lambda: len(copied) >= 1)
    (aside / "all").mkdir()
    assert files.copy_aside(root, "", ["a.txt", "b.txt"], lambda: str(aside / "all"), 1 << 20) == \
        [str(aside / "all" / "a.txt"), str(aside / "all" / "b.txt")], "keeps it so: not given up, all of them"


def test_a_clipboard_look_given_up_half_way_leaves_the_clipboard_without_files(setup, wait, monkeypatch, qapp):
    """The same through the page: Ctrl+C of an agent's files whose copies are not done within EXPORT_WAIT leaves the
    clipboard with their paths as text, no file of them (it named those copied by then)."""
    page, ws, _host, _ = setup
    for name in ("p1.txt", "p2.txt", "p3.txt"):
        (ws / name).write_text(name)
    left = page.left
    left.list()
    settle(left)
    monkeypatch.setattr(files_page, "EXPORT_WAIT", 0.3)
    real = files.copy_entry

    def slow(source, relative, *args, **kwargs):
        if relative.endswith("p2.txt"):
            time.sleep(0.6)
        return real(source, relative, *args, **kwargs)
    monkeypatch.setattr(files, "copy_entry", slow)
    select(left, "p1.txt", "p2.txt", "p3.txt")
    assert page.copy_items(left)
    wait(lambda: False, 2.5)  # (the look in its thread: given up after EXPORT_WAIT, done a moment later)
    urls = QtWidgets.QApplication.clipboard().mimeData().urls()
    assert urls == [], [url.toString() for url in urls]


# -- a folder moved out of the root while it is opened --

def test_a_folder_moved_out_of_the_root_while_it_is_opened_leaves_no_descriptor_open(tmp_path, monkeypatch):
    """Root._open_child_dir refused a folder no longer below the root (moved while it was opened) - and left its
    descriptor open: a copy that met many such folders (named and skipped since pass 14) ran out of descriptors."""
    (tmp_path / "ws" / "sub").mkdir(parents=True)
    root = files.Root(tmp_path / "ws")
    parent = root.open_dir("")
    try:
        monkeypatch.setattr(files, "fd_location", lambda fd: "/elsewhere/entirely")
        before = open_descriptors()
        for _ in range(20):
            with pytest.raises(files.Refused, match="no longer below"):
                root._open_child_dir(parent, "sub")
        assert open_descriptors() == before
    finally:
        os.close(parent)

