"""Review pass 5 of the Files page's operations: each test failed before its fix. A delete of a folder that held a mount
point emptied the file system mounted there (and a copy, a move, a count and a recursive owner change walked into it),
quitting with an editor of changes not saved and "Keep editing" left that editor in an app already shut down, a file
mounted in place could not be saved (and no write in place was offered), files of another program pasted into a remote
pane of a sandbox-ai host asked nothing, an agent's folder uploaded to a remote system arrived writable by everyone
there, macOS's own attributes (com.apple.provenance) made every save ask to write in place, Copy path in the sandbox on
macOS for a path typed in other letter case, a Markdown file of the agent's in the host pane showed images from anywhere
on the host, and Reload kept the images as they were. Everything changed is below tmp_path."""
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys

import pytest

from sandbox_ai_app import files

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, files_page, widgets  # noqa: E402
from test_files_page import finish, names, select, settle, setup  # noqa: E402,F401
from test_files_remote import served  # noqa: E402,F401
from test_files_review_p1 import connect  # noqa: E402
from test_files_manager import editor_of  # noqa: E402
from test_ui import env, window  # noqa: E402,F401


# -- mount points: a delete, a move's removal, a copy, a count, a recursive chown stay on one file system --

@pytest.fixture
def mounts(monkeypatch):
    """Folders taken for mount points (no real mount in a test): what lies in one of them is on that mount."""
    marked = []
    real = files._mount_of

    def mount_of(fd):
        where = files.fd_location(fd) or ""
        for path in sorted(marked, key=len, reverse=True):
            if where == path or where.startswith(path + "/"):
                return ("mount", path)
        return real(fd)
    monkeypatch.setattr(files, "_mount_of", mount_of)
    return lambda path: marked.append(os.path.realpath(path))


@pytest.fixture
def tree(tmp_path, mounts):
    """ws/data/usb is a mount point: a USB stick, a network share, a bind mount of the host's."""
    ws = tmp_path / "ws"
    (ws / "data" / "usb" / "photos").mkdir(parents=True)
    (ws / "data" / "usb" / "photos" / "one.jpg").write_text("a photo")
    (ws / "data" / "notes.txt").write_text("notes")
    mounts(ws / "data" / "usb")
    return ws


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's mount IDs")
def test_a_mount_point_is_found_by_its_mount_id(tmp_path):
    """/proc is another mount than /; a folder and its subfolder are on one."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    root, proc, a, b = (os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path in
                        ("/", "/proc", tmp_path / "a", tmp_path / "a" / "b"))
    try:
        assert files._mount_of(root)[0] == "mount" and files._other_mount(root, proc)
        assert not files._other_mount(a, b)
    finally:
        for fd in (root, proc, a, b):
            os.close(fd)


def test_a_folder_that_holds_a_mount_point_is_not_deleted(tree):
    """Its files went with it, and what was mounted there was emptied - rm -r's way; nothing is deleted now."""
    root = files.Root(tree)
    with pytest.raises(files.Refused, match="mount point"):
        root.delete_entry("data")
    assert (tree / "data" / "usb" / "photos" / "one.jpg").exists() and (tree / "data" / "notes.txt").exists()
    with pytest.raises(files.Refused, match="mount point"):
        root.delete_entry("data/usb")
    assert (tree / "data" / "usb" / "photos" / "one.jpg").exists()
    root.delete_entry("data/usb/photos/one.jpg")  # (from inside it: what is on it is deleted as asked)
    assert not (tree / "data" / "usb" / "photos" / "one.jpg").exists()


def test_a_copy_and_a_move_skip_a_mount_point_in_a_folder(tree, tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    source, target = files.Root(tree), files.Root(host, sandbox=False)
    copied = files.copy_entry(source, "data", target, "")
    assert (host / "data" / "notes.txt").exists() and not (host / "data" / "usb").exists()
    assert [path for path, _ in copied.skipped] == ["data/usb"]
    moved = files.move_entry(source, "data", target, "", overwrite=True)
    assert not moved.moved and moved.kept, "something was skipped: the source stays"
    assert (tree / "data" / "usb" / "photos" / "one.jpg").exists() and (tree / "data" / "notes.txt").exists()


def test_a_mount_point_moved_as_a_whole_keeps_what_is_mounted_there(tree, tmp_path):
    """A move between roots copies, then removes what it copied: the mounted file system was emptied."""
    host = tmp_path / "host"
    host.mkdir()
    moved = files.move_entry(files.Root(tree), "data/usb", files.Root(host, sandbox=False), "")
    assert (host / "usb" / "photos" / "one.jpg").exists(), "copied"
    assert not moved.moved and (tree / "data" / "usb" / "photos" / "one.jpg").exists(), "the source stays"


def test_a_count_and_a_recursive_owner_change_skip_a_mount_point(tree):
    root = files.Root(tree)
    counted = root.measure("data")
    assert counted.files == 1 and [path for path, _ in counted.skipped] == ["data/usb"]
    assert root.measure("data/usb").files == 1, "the folder chosen itself is counted"
    owned = root.chown_entry("data", gid=os.getgid(), recursive=True)
    assert [path for path, _ in owned.skipped] == ["data/usb"] and owned.files == 2  # data, notes.txt


def test_an_upload_skips_a_mount_point_in_a_folder(tree, tmp_path):
    served = tmp_path / "srv"
    served.mkdir()
    root = connect(served)
    try:
        result = files.copy_entry(files.Root(tree), "data", root, "")
    finally:
        root.close()
    assert (served / "data" / "notes.txt").exists() and not (served / "data" / "usb").exists()
    assert [path for path, _ in result.skipped] == ["data/usb"]


def test_properties_name_a_mount_point_not_counted(setup, mounts, wait):
    page, ws, host, _ = setup
    (ws / "sub" / "mnt").mkdir()
    (ws / "sub" / "mnt" / "big").write_text("x" * 100)
    mounts(ws / "sub" / "mnt")
    page.left.list()
    select(page.left, "sub")
    dialog = page.properties(page.left)
    assert wait(lambda: not dialog.counting, 10)
    assert "Not counted: sub/mnt" in dialog.message.text() and "1 file(s)" in dialog.size.text()
    dialog.close()


# -- quitting with an editor of changes not saved --

@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="the main window needs sandbox-ai")
def test_keep_editing_keeps_the_whole_app(window, tmp_path, monkeypatch, wait):
    """The main window was shut down and hidden first; Qt's quit asked the editor afterwards, and "Keep editing" left
    it open in an app whose window had gone (its saves never came back)."""
    from PySide6.QtGui import QCloseEvent
    (tmp_path / "f.txt").write_text("old")
    page = window.files
    editor = page.open_editor(files.Root(tmp_path), "f.txt", str(tmp_path / "f.txt"), "text")
    assert wait(lambda: not editor.loading, 10)
    editor.text.setPlainText("new")
    assert editor.dirty
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep editing"
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted() and not getattr(window, "shut_down", False) and not window.finished
    assert editor.dirty and any(title == "Discard changes?" for title, *_ in widgets.SHOWN)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # "Discard"
    widgets.SHOWN.clear()
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() and window.finished and not editor.dirty, "quits; the editor closes without asking again"
    editor.close()
    assert sum(title == "Discard changes?" for title, *_ in widgets.SHOWN) == 1


# -- a file mounted in place (EBUSY) is written in place after a question --

def test_a_file_mounted_in_place_is_offered_a_write_in_place(tmp_path, monkeypatch):
    target = tmp_path / "resolv.conf"
    target.write_text("old")
    real = files._place

    def busy(temporary, name, dir_fd, overwrite, identity=None):
        if name == "resolv.conf":
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))
        return real(temporary, name, dir_fd, overwrite, identity)
    monkeypatch.setattr(files, "_place", busy)
    root = files.Root(tmp_path, sandbox=False)
    read = root.read_file("resolv.conf")
    with pytest.raises(files.NoTemporary, match="mount point"):
        root.write_file("resolv.conf", b"new", read.identity)
    assert target.read_text() == "old" and os.listdir(tmp_path) == ["resolv.conf"], "no temporary file left"
    root.write_file("resolv.conf", b"new", read.identity, in_place=True)
    assert target.read_text() == "new"


def test_the_editor_asks_to_write_a_mounted_file_in_place(setup, monkeypatch, wait):
    page, ws, host, _ = setup
    real = files._place

    def busy(temporary, name, dir_fd, overwrite, identity=None):
        if name == "a.txt":
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))
        return real(temporary, name, dir_fd, overwrite, identity)
    monkeypatch.setattr(files, "_place", busy)
    page.open_editor(page.left.root, "a.txt", str(ws / "a.txt"), "text")
    window = editor_of(page, wait)
    window.text.setPlainText("changed")
    assert window.save()
    assert wait(lambda: not window.saving, 10)
    asked = [text for title, text, *_ in widgets.SHOWN if title == "Write the file in place?"]
    assert asked and "mount point" in asked[-1] and "folder you may not write" not in asked[-1]
    assert (ws / "a.txt").read_text() == "changed"
    window.close()



# -- files of another program into a remote pane of a sandbox-ai host --

def test_files_of_another_program_pasted_to_a_sandbox_host_ask_first(served, monkeypatch, tmp_path):
    page, home, ws, host = served
    page.sai.host_entries = lambda: [{"name": "lab", "kind": "ssh", "ssh": "alice@fake"}]
    secret = tmp_path / "id_ed25519"
    secret.write_text("a key")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert page.copy_paths([str(secret)], page.left, page.left.relative, foreign=True) is False
    assert any("Copy files of another program to alice@fake?" == title for title, *_ in widgets.SHOWN)
    assert not (home / "id_ed25519").exists() and page.message.text() == "Nothing was copied."
    # Not a sandbox-ai host: copied as before, unasked.
    page.sai.host_entries = lambda: [{"name": "other", "kind": "ssh", "ssh": "bob@elsewhere"}]
    widgets.SHOWN.clear()
    assert not page.sandbox_host(page.left.root) and not widgets.SHOWN


# -- an agent's folder uploaded to a remote system --

def test_an_agents_folder_uploaded_is_not_writable_by_others_there(tmp_path):
    served, ws = tmp_path / "srv", tmp_path / "ws"
    served.mkdir()
    (ws / "open").mkdir(parents=True)
    (ws / "open" / "f").write_text("x")
    (ws / "open").chmod(0o777)
    root = connect(served)
    try:
        files.copy_entry(files.Root(ws), "open", root, "")
    finally:
        root.close()
    assert stat.S_IMODE((served / "open").stat().st_mode) & 0o022 == 0


def test_an_agents_file_uploaded_is_not_writable_by_others_there(tmp_path):
    """A local copy is made with the umask (an agent's 0666 file arrives 0644); the remote one was given its bits as
    they were - one anybody on the remote system could change."""
    served, ws = tmp_path / "srv", tmp_path / "ws"
    served.mkdir()
    ws.mkdir()
    (ws / "f").write_text("x")
    (ws / "f").chmod(0o666)
    root = connect(served)
    try:
        files.copy_entry(files.Root(ws), "f", root, "")
    finally:
        root.close()
    assert stat.S_IMODE((served / "f").stat().st_mode) == 0o644


# -- macOS: the attributes the system keeps for itself --

class FakeMacLibc:
    """fcopyfile and flistxattr of macOS's libc, for the attribute names given."""

    def __init__(self, names, fail_xattr=True, copied=()):
        self.names, self.fail_xattr, self.copied = names, fail_xattr, set(copied)
        self.sources = set()

    def fcopyfile(self, source, target, state, flags):
        self.sources.add(source)
        return -1 if flags == 4 and self.fail_xattr else 0

    def flistxattr(self, fd, buffer, size, options):
        names = self.names if fd in self.sources else self.copied
        data = b"".join(name.encode() + b"\0" for name in sorted(names))
        if buffer is None:
            return len(data)
        ctypes.memmove(buffer, data, len(data))
        return len(data)


def test_macos_provenance_not_copied_asks_nothing(tmp_path):
    (tmp_path / "f").write_text("x")
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    target = os.open(tmp_path / "g", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        libc = FakeMacLibc({"com.apple.provenance", "user.tag"}, copied={"user.tag"})
        files._keep_attributes_macos(parent, "f", target, libc)  # (provenance: the system's own, not carried over)
        libc = FakeMacLibc({"com.apple.provenance", "user.tag"}, copied={"com.apple.provenance"})
        with pytest.raises(files.NotKept):
            files._keep_attributes_macos(parent, "f", target, libc)  # (another one lost: asked as before)
    finally:
        os.close(parent)
        os.close(target)


def test_macos_copy_path_in_the_sandbox_for_a_path_of_other_letter_case(setup, monkeypatch):
    page, ws, host, _ = setup
    monkeypatch.setattr(files_page, "FOLDED_PATHS", True)
    page.sandbox_paths = {"/Users/me/WS": "/work"}
    forms = page.path_forms(page.right, "/users/me/ws/src/a.py")
    assert forms["sandbox"] == "/work/src/a.py" and forms["relative"] == "src/a.py"


# -- the Markdown viewer --

def test_markdown_of_a_sandbox_folder_in_the_host_pane_shows_only_its_images(setup, wait):
    page, ws, host, outside = setup
    from PySide6.QtGui import QImage
    picture = QImage(4, 4, QImage.Format.Format_RGB32)
    picture.fill(0xFF0000)
    assert picture.save(str(outside / "private.png")) and picture.save(str(ws / "pic.png"))
    climb = "../" * len(ws.parts)
    (ws / "README.md").write_text(f"![a]({climb}{str(outside / 'private.png').lstrip('/')})\n\n![b](pic.png)\n\n"
                                  f"[c]({climb}{str(outside / 'other.md').lstrip('/')})\n")
    (outside / "other.md").write_text("# host")
    page.right.go_path(str(ws))
    settle(page.right)
    page.open_editor(page.right.root, files_page.host_relative(str(ws / "README.md")), str(ws / "README.md"))
    window = editor_of(page, wait)
    source = window.markdown.source
    private = f"{climb}{str(outside / 'private.png').lstrip('/')}"
    assert source.kind(private)[0] == "outside" and source.kind("pic.png")[0] == "local"
    from PySide6.QtCore import QUrl
    before = len(page.editors)
    window.open_link(QUrl(f"{climb}{str(outside / 'other.md').lstrip('/')}"))
    assert len(page.editors) == before, "a host Markdown file outside the sandbox's folder is not opened"
    window.close()
    # A host folder of the human's own: as before.
    (host / "doc.md").write_text("x")
    assert page.markdown_top(page.right.root, files_page.host_relative(str(host / "doc.md"))) == ""


def test_reload_reads_the_images_again(tmp_path, qapp, wait):
    from PySide6.QtGui import QImage
    picture = QImage(4, 4, QImage.Format.Format_RGB32)
    picture.fill(0xFF0000)
    assert picture.save(str(tmp_path / "p.png"))
    (tmp_path / "a.md").write_text("![p](p.png)\n")
    from sandbox_ai_app.file_editor import EditorWindow
    window = EditorWindow(None, files.Root(tmp_path), "a.md", str(tmp_path / "a.md"), "markdown", True)
    window.show()
    assert wait(lambda: not window.loading and isinstance(window.markdown.images.get("p.png"), QImage) and
                window.markdown.images["p.png"].width() == 4, 10)
    bigger = QImage(8, 8, QImage.Format.Format_RGB32)
    bigger.fill(0x00FF00)
    assert bigger.save(str(tmp_path / "p.png"))
    window.reload()
    assert wait(lambda: not window.loading and isinstance(window.markdown.images.get("p.png"), QImage) and
                window.markdown.images["p.png"].width() == 8, 10), "the image as it is now"
    window.close()
    window.deleteLater()


# -- a fresh look at every operation --

@pytest.mark.skipif(os.geteuid() == 0, reason="root writes any folder")
def test_a_copy_into_a_folder_you_may_not_write_names_the_file_not_a_temporary_one(tmp_path):
    """"Permission denied: '.sandbox-ai-app-1234-....part'" - a name the human never saw."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "a.txt").write_text("a")
    target.chmod(0o555)
    try:
        with pytest.raises(OSError) as raised:
            files.copy_entry(files.Root(source), "a.txt", files.Root(target, sandbox=False), "")
        with pytest.raises(OSError) as saved:
            files.Root(target, sandbox=False).write_file("new.txt", b"x")
    finally:
        target.chmod(0o755)
    for exc in (raised.value, saved.value):
        assert ".sandbox-ai-app-" not in str(exc) and ("a.txt" in str(exc) or "new.txt" in str(exc))


def test_a_temporary_file_refused_is_named_as_the_file(tmp_path, monkeypatch):
    """The same with any error of the temporary file's creation (a full disk, a read-only file system), locally and on
    a remote system."""
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.txt").write_text("a")
    (tmp_path / "out").mkdir()
    real_create = files._create

    def create(name, dir_fd, mode):
        if ".sandbox-ai-app-" in name:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS), name)
        return real_create(name, dir_fd, mode)
    monkeypatch.setattr(files, "_create", create)
    with pytest.raises(OSError) as raised:
        files.copy_entry(files.Root(tmp_path / "ws"), "a.txt", files.Root(tmp_path / "out", sandbox=False), "")
    with pytest.raises(OSError) as saved:
        files.Root(tmp_path / "ws").write_file("a.txt", b"x")
    monkeypatch.undo()
    served = tmp_path / "srv"
    served.mkdir()
    root = connect(served)
    try:
        real = root.sftp.open

        def refused(path, flags, mode=None):
            if ".sandbox-ai-app-" in path:
                raise OSError(errno.EIO, f"{path}: Failure")
            return real(path, flags, mode)
        root.sftp.open = refused
        with pytest.raises(OSError) as sent:
            files.copy_entry(files.Root(tmp_path / "ws"), "a.txt", root, "")
    finally:
        root.close()
    for exc in (raised.value, saved.value, sent.value):
        assert ".sandbox-ai-app-" not in str(exc) and "a.txt" in str(exc), str(exc)


def test_a_write_in_place_asked_for_stays_in_place_after_the_changed_question(setup, monkeypatch, wait):
    """Asked to write in place, then that the file changed meanwhile: Replace saved through a temporary file again,
    which failed again and asked to write in place a second time."""
    page, ws, host, _ = setup
    page.open_editor(page.left.root, "a.txt", str(ws / "a.txt"), "text")
    window = editor_of(page, wait)
    real = files.Root.write_file
    calls = []

    def write_file(self, relative, data, expected=None, in_place=False, mode=None):
        calls.append(in_place)
        if not in_place:
            raise files.NoTemporary("no temporary file here")
        if len(calls) == 2:
            (ws / "a.txt").write_text("the agent's")  # (changed between the question and the write)
        return real(self, relative, data, expected, in_place, mode)
    monkeypatch.setattr(files.Root, "write_file", write_file)
    window.text.setPlainText("mine")
    assert window.save()
    assert wait(lambda: not window.saving, 10)
    assert calls == [False, True, True] and (ws / "a.txt").read_text() == "mine"
    assert sum(title == "Write the file in place?" for title, *_ in widgets.SHOWN) == 1
    window.close()


def test_macos_a_folder_typed_in_other_letter_case_in_the_sandbox_pane(setup, monkeypatch):
    """/users/me/ws/sub typed is the folder /Users/me/ws/sub on macOS: it was refused as "not below the folder"."""
    page, ws, host, _ = setup
    monkeypatch.setattr(files_page, "FOLDED_PATHS", True)
    assert page.left.go_path(str(ws).upper() + "/sub")
    settle(page.left)
    assert page.left.relative == "sub" and names(page.left) == ["b.txt"]
    assert not page.left.go_path(str(ws.parent).upper() + "/outside"), "still nothing above the folder"


def test_a_download_into_a_folder_you_may_not_write_names_the_file(tmp_path, monkeypatch):
    """The staging folder's name (".sandbox-ai-app-....part") was the name the error gave."""
    served, host = tmp_path / "srv", tmp_path / "host"
    served.mkdir()
    host.mkdir()
    (served / "a.txt").write_text("a")
    real = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if ".sandbox-ai-app-" in os.fspath(path):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        return real(path, mode, dir_fd=dir_fd)
    root = connect(served)
    try:
        monkeypatch.setattr(os, "mkdir", mkdir)
        with pytest.raises(OSError) as raised:
            files.copy_entry(root, "a.txt", files.Root(host, sandbox=False), "")
    finally:
        monkeypatch.undo()
        root.close()
    assert ".sandbox-ai-app-" not in str(raised.value) and "a.txt" in str(raised.value)


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="the main window needs sandbox-ai")
def test_sigterm_with_an_editor_of_changes_not_saved_still_quits(window, tmp_path, monkeypatch, wait):
    """Qt's quit closes every window and stops at one that stays open: the editor asked "Discard changes?" with nobody
    there to answer (SIGTERM: a stop of the service, a shutdown), and the app went on running."""
    from PySide6.QtGui import QCloseEvent
    (tmp_path / "f.txt").write_text("old")
    editor = window.files.open_editor(files.Root(tmp_path), "f.txt", str(tmp_path / "f.txt"), "text")
    assert wait(lambda: not editor.loading, 10)
    editor.text.setPlainText("new")
    monkeypatch.setattr(QtWidgets.QApplication, "quit", staticmethod(lambda: None))
    monkeypatch.setattr("sandbox_ai_app.ui.QUIT_WAIT", 0.5)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    window.terminate()
    event = QCloseEvent()
    editor.closeEvent(event)  # (what quit() sends to it, maybe before the main window)
    assert event.isAccepted() and not any(title == "Discard changes?" for title, *_ in widgets.SHOWN)
    editor.close()


@pytest.mark.skipif(not os.path.isfile("/proc/self/status"), reason="a file that says it is empty: Linux's /proc")
def test_a_remote_file_that_says_it_is_empty_but_holds_more_is_read_as_cut_short():
    """A file whose size says 0 (/proc, /sys) was read up to the limit and taken as whole: the editor would have saved
    it cut short."""
    root = connect("/proc/self")
    try:
        read = root.read_file("status", limit=10)
    finally:
        root.close()
    assert len(read.data) == 10 and read.truncated
