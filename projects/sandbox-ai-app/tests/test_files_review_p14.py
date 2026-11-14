"""Review pass 14 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
On a file system that hangs, a listing asked for again (Ctrl+R, Refresh, the folder typed again) started one more
thread each time that waited for good, and so did every drag or Ctrl+C of entries there, every Properties of them and
every Apply cancelled and pressed again (the late Apply's bits were unknown to the dialog). A folder swapped for a link
while it was copied failed the whole copy; a remote system's permission bits were set through a link put in the item's
place after the look. Everything changed is below tmp_path."""
import os
import threading
import time

import pytest

from sandbox_ai_app import files

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401


class Hung:
    """Calls that hang (a hard NFS mount, sshfs whose server went away) until GATE is set: how many wait now, and how
    many waited at most at the same time."""

    def __init__(self):
        self.gate, self.lock, self.now, self.most, self.calls = threading.Event(), threading.Lock(), 0, 0, 0

    def wait(self):
        with self.lock:
            self.now += 1
            self.calls += 1
            self.most = max(self.most, self.now)
        try:
            self.gate.wait(60)
        finally:
            with self.lock:
                self.now -= 1


@pytest.fixture
def hung():
    found = Hung()
    yield found
    found.gate.set()


def hang_listing(monkeypatch, hung, marker="hung"):
    """Root.list of a folder whose path holds MARKER hangs (in the listing's thread)."""
    real = files.Root.list

    def listing(self, relative=""):
        if marker in relative.split("/") or any(part.startswith(marker) for part in relative.split("/")):
            hung.wait()
        return real(self, relative)
    monkeypatch.setattr(files.Root, "list", listing)


def ctrl(pane, key):
    return pane.page.key(pane, key, Qt.KeyboardModifier.ControlModifier)


# -- (a) a listing asked for again --

def test_a_folder_that_hangs_listed_again_and_again_waits_in_one_thread(setup, wait, monkeypatch, hung, qapp):
    """Ctrl+R (or Refresh, or "." opened) on a folder whose file system hangs started a new listing thread each time:
    each waited for good, and after two minutes said "No answer" while it went on. A listing of that folder still on
    its way is waited for again, never started anew - and once it answers, the pane shows it."""
    page, _ws, host, _ = setup
    (host / "hung").mkdir()
    (host / "hung" / "x.txt").write_text("x")
    right = page.right
    hang_listing(monkeypatch, hung)
    assert right.go_path(str(host / "hung"))
    assert wait(lambda: hung.now == 1, 5)
    for _ in range(6):
        assert ctrl(right, Qt.Key.Key_R)
        qapp.processEvents()
    time.sleep(0.2)
    assert hung.calls == 1, f"{hung.calls} listings of the folder that hangs"
    assert right.listing is not None, "still on its way"
    hung.gate.set()
    settle(right)
    assert names(right) == ["x.txt"] and right.relative == right.shown


def test_a_folder_listed_again_while_its_listing_is_on_its_way_shows_what_is_there_now(setup, wait, monkeypatch):
    """Keeps it so: Ctrl+R while a listing of the folder is still on its way (a slow file system) shows the folder as it
    is after the change made meanwhile - once that listing has come the folder is listed again, once for every Ctrl+R
    of the meantime."""
    page, _ws, host, _ = setup
    right = page.right
    right.list()
    settle(right)
    real, calls = files.Root.list, []

    def slow(self, relative=""):
        calls.append(relative)
        time.sleep(0.4)
        return real(self, relative)
    monkeypatch.setattr(files.Root, "list", slow)
    right.list()
    time.sleep(0.1)
    (host / "new.txt").write_text("n")
    for _ in range(4):
        right.list()
    settle(right)
    assert "new.txt" in names(right)
    assert len(calls) == 2, calls


def test_listings_of_folders_that_hang_are_bounded_and_the_pane_stays_where_it_was(setup, wait, monkeypatch, hung,
                                                                                    qapp):
    """Each folder of a file system that hangs, typed one after the other, left a listing waiting for good - without
    any bound. At most LIST_STUCK of a pane wait so; past them the pane stays on the folder it shows and says why."""
    page, _ws, host, _ = setup
    (host / "a.txt").write_text("a")
    for number in range(12):
        (host / f"hung{number}").mkdir()
    right = page.right
    right.list()
    settle(right)
    shown = right.relative
    hang_listing(monkeypatch, hung)
    monkeypatch.setattr(files_page, "LIST_SLOW", 0.05)
    for number in range(12):
        right.go_path(str(host / f"hung{number}"))
        assert wait(lambda: hung.now >= min(number + 1, files_page.LIST_STUCK), 5)
        time.sleep(0.08)
        qapp.processEvents()
    assert hung.most <= files_page.LIST_STUCK, f"{hung.most} listings waiting at once"
    assert right.relative == shown and right.listing is None, "the pane stays on the folder shown"
    assert "a.txt" in names(right)
    assert "still wait" in page.message.text()
    hung.gate.set()
    assert wait(lambda: hung.now == 0, 10)
    right.go_path(str(host / "hung0"))
    settle(right)
    assert right.relative.endswith("hung0"), "once they answered, the pane lists again"


# -- (b) a drag or Ctrl+C of entries on a file system that hangs --

def test_drags_and_copies_of_folders_that_hang_leave_no_thread_each(setup, wait, monkeypatch, hung, qapp):
    """A host folder dragged out or copied with Ctrl+C is looked through for a sandbox's folder in a thread, given up
    after EXPORT_WAIT - but on a file system that hangs that thread never ended, and every drag or Ctrl+C left one
    more. A look of the same entries still on its way is not started again, and at most EXPORT_STUCK wait at once."""
    page, _ws, host, _ = setup
    for number in range(8):
        (host / f"mnt{number}").mkdir()
    right = page.right
    right.list()
    settle(right)
    monkeypatch.setattr(files_page, "EXPORT_WAIT", 0.4)
    real = files_page.holds_agents

    def holds(path, *args, **kwargs):
        if os.path.basename(path).startswith("mnt"):
            hung.wait()
        return real(path, *args, **kwargs)
    monkeypatch.setattr(files_page, "holds_agents", holds)
    entry = right.by_name["mnt0"]
    started = time.monotonic()
    for _ in range(6):
        assert page.export_urls(right, [entry]) == []
    spent = time.monotonic() - started
    assert spent < 1.2, f"the window was held up {spent:.1f} s (EXPORT_WAIT again at every drag)"
    for _ in range(6):
        select(right, "mnt0")
        assert page.copy_items(right)
        qapp.processEvents()
    time.sleep(0.3)
    assert hung.calls == 1, f"{hung.calls} looks at the same folder that hangs"
    for number in range(1, 8):
        page.export_urls(right, [right.by_name[f"mnt{number}"]])
    assert hung.most <= files_page.EXPORT_STUCK, f"{hung.most} looks waiting at once"
    hung.gate.set()
    assert wait(lambda: hung.now == 0, 10)
    assert page.export_urls(right, [entry]), "once it answers, the folder goes out again"


# -- (c) Properties and its Apply on a file system that hangs --

def test_properties_asked_again_and_again_on_a_file_system_that_hangs_are_bounded(setup, wait, monkeypatch, hung,
                                                                                  qapp):
    """Alt+Enter on entries of a file system that hangs read them in a thread that never ended (and a second one waited
    for it, for a day): every try left two more. At most LOCAL_STUCK such steps wait at once; past them the page says
    why, at once."""
    page, _ws, host, _ = setup
    (host / "f.txt").write_text("f")
    right = page.right
    right.list()
    settle(right)
    real = files.Root.stat_entry

    def stat_entry(self, relative):
        if relative.endswith("f.txt"):
            hung.wait()
        return real(self, relative)
    monkeypatch.setattr(files.Root, "stat_entry", stat_entry)
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.05)
    entry = right.by_name["f.txt"]
    for _ in range(files_page.LOCAL_STUCK + 4):
        page.properties(right, [entry])
        qapp.processEvents()
    assert hung.most <= files_page.LOCAL_STUCK, f"{hung.most} reads waiting at once"
    assert "still wait" in page.message.text()
    hung.gate.set()
    assert wait(lambda: hung.now == 0 and page.dialogs, 10)
    for dialog in list(page.dialogs):
        dialog.close()


def test_apply_cancelled_and_pressed_again_waits_for_the_one_before(setup, wait, monkeypatch, hung, qapp):
    """Apply on a file system that hangs runs as the page's job; Cancel gave the dialog back while the change still
    waited - and Apply again started another, every time. The dialog never learnt what the first one set once the file
    system answered: a later Apply set those bits back. Apply waits for the one before; what that one set is known."""
    page, _ws, host, _ = setup
    (host / "p.txt").write_text("p")
    os.chmod(host / "p.txt", 0o600)
    right = page.right
    right.list()
    settle(right)
    dialog = page.properties(right, [right.by_name["p.txt"]])
    real = files.Root.chmod_entry

    def chmod_entry(self, relative, *args, **kwargs):
        if not hung.gate.is_set():
            hung.wait()
        return real(self, relative, *args, **kwargs)
    monkeypatch.setattr(files.Root, "chmod_entry", chmod_entry)
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.05)
    try:
        dialog.checks[3].setChecked(True)  # (the group's read: 0640)
        assert dialog.apply() is not False and page.task.running
        page.task.cancel()
        assert wait(lambda: not page.task.running, 5)
        for _ in range(3):
            assert dialog.apply() is False
            assert "still waits" in dialog.message.text()
        assert hung.calls == 1
    finally:
        hung.gate.set()
    assert wait(lambda: os.stat(host / "p.txt").st_mode & 0o777 == 0o640, 10)
    assert wait(lambda: dialog.mode == 0o640, 10), "what the late Apply set is known"
    assert "took effect" in dialog.message.text()
    # the group's read taken back: the file has it (the late Apply), so Apply takes it away - the dialog thought the
    # file had never got it, and Apply stayed greyed out
    dialog.checks[3].setChecked(False)
    assert dialog.apply_button.isEnabled()
    assert dialog.apply() is not False
    assert wait(lambda: os.stat(host / "p.txt").st_mode & 0o777 == 0o600, 10)
    dialog.close()


# -- a remote system's permission bits: never through a link put in the item's place --

def test_a_remote_permission_change_never_goes_through_a_link_swapped_in(tmp_path):
    """The bits of a remote file were set with SETSTAT, which the server applies through a symbolic link: an agent on a
    host of sandbox-ai's that put a link to a file of the human's in the item's place between the look (LSTAT) and the
    change gave that file the bits chosen - 0666 on a key. With the server's lsetstat@openssh.com (OpenSSH 8.0 and
    later) the item itself is changed, and a link there is refused."""
    from test_remote import connect
    served = tmp_path / "served"
    served.mkdir()
    (served / "a.txt").write_text("a")
    os.chmod(served / "a.txt", 0o644)
    (served / "key").write_text("secret")
    os.chmod(served / "key", 0o600)
    root = connect(served)
    try:
        assert "lsetstat@openssh.com" in root.sftp.extensions
        real = root.sftp.lstat

        def swapped(path):
            found = real(path)
            if path.endswith("/a.txt"):
                os.unlink(served / "a.txt")
                os.symlink(served / "key", served / "a.txt")
            return found
        root.sftp.lstat = swapped
        with pytest.raises(files.Refused, match="symbolic link"):
            root.chmod_entry("a.txt", 0o666)
        assert os.stat(served / "key").st_mode & 0o777 == 0o600, "the human's file kept its bits"
        root.sftp.lstat = real
        os.unlink(served / "a.txt")
        (served / "a.txt").write_text("a")
        os.chmod(served / "a.txt", 0o600)
        root.chmod_entry("a.txt", 0o640)
        assert os.stat(served / "a.txt").st_mode & 0o777 == 0o640
    finally:
        root.close()


def test_a_remote_permission_change_on_a_server_without_lsetstat_works_as_before(tmp_path, monkeypatch):
    """Keeps it so: a server without the extension gets SETSTAT, as before - and so does one whose lsetstat fails for
    what is no link (OpenSSH's on an older C library: fchmodat with AT_SYMLINK_NOFOLLOW is not supported there)."""
    from sandbox_ai_app import remote
    from test_remote import connect
    served = tmp_path / "served"
    served.mkdir()
    (served / "a.txt").write_text("a")
    os.chmod(served / "a.txt", 0o600)
    root = connect(served, FAKE_SFTP_NO_EXTENSIONS="1")
    try:
        assert "lsetstat@openssh.com" not in root.sftp.extensions
        root.chmod_entry("a.txt", 0o640)
        assert os.stat(served / "a.txt").st_mode & 0o777 == 0o640
    finally:
        root.close()
    root = connect(served)
    try:
        real = root.sftp.ask

        def unsupported(kind, payload, *args, **kwargs):
            if kind == remote.EXTENDED and b"lsetstat@openssh.com" in payload:
                raise OSError(95, "Operation not supported")
            return real(kind, payload, *args, **kwargs)
        root.sftp.ask = unsupported
        root.chmod_entry("a.txt", 0o644)
        assert os.stat(served / "a.txt").st_mode & 0o777 == 0o644
    finally:
        root.close()


# -- a folder swapped for a link while its folder is copied --

@pytest.mark.parametrize("remote_target", [False, True])
def test_a_folder_swapped_for_a_link_while_it_is_copied_is_named_and_the_rest_copied(tmp_path, monkeypatch,
                                                                                   remote_target):
    """A file swapped for a FIFO or a link between the look and the opening is named and the rest copied (as one
    removed meanwhile: pass 13) - a folder swapped for a link there failed the whole copy ("sub is not a folder"), to
    this machine and to a remote system alike: an agent that renames its folders as it works made a copy of its
    workspace impossible. Named, the rest copied, nothing followed."""
    ws, host, outside = tmp_path / "ws", tmp_path / "host", tmp_path / "outside"
    (ws / "proj" / "sub").mkdir(parents=True)
    host.mkdir()
    outside.mkdir()
    (outside / "precious.txt").write_text("the human's")
    (ws / "proj" / "a.txt").write_text("a")
    (ws / "proj" / "sub" / "s.txt").write_text("s")
    real = files.Root._open_child_dir

    def swapped(self, parent, name):
        if name == "sub" and (ws / "proj" / "sub").is_dir() and not (ws / "proj" / "sub").is_symlink():
            os.unlink(ws / "proj" / "sub" / "s.txt")
            os.rmdir(ws / "proj" / "sub")
            os.symlink(outside, ws / "proj" / "sub")
        return real(self, parent, name)
    monkeypatch.setattr(files.Root, "_open_child_dir", swapped)
    if remote_target:
        from test_remote import connect
        target = connect(host)
    else:
        target = files.Root(host, sandbox=False)
    try:
        result = files.copy_entry(files.Root(ws), "proj", target, "", untrusted=True)
    finally:
        if remote_target:
            target.close()
    assert [path for path, _ in result.skipped] == ["proj/sub"]
    assert (host / "proj" / "a.txt").read_text() == "a"
    assert not (host / "proj" / "sub").exists() or not any((host / "proj" / "sub").iterdir())
    assert not (host / "proj" / "sub" / "precious.txt").exists(), "nothing followed"
