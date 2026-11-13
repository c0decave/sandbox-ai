"""Review pass 4 of the Files page's operations: each test failed before its fix. An upload into a remote folder that
was there already changed that folder's permissions, a Markdown file naming countless images beside it kept a picture
for each, the "Listing…" note stayed for good in German, sizes in German written with English separators ("1,000
Bytes" reads as one byte there), an editor's Markdown links after SSH… connected again, "Copy path in the sandbox"
offered for a remote file, Owner… asking for this machine's IDs on a remote system, a remote folder's double slash in
the Recent menu, a folder name with "&" in the context menu, copies beside a long name ("File name too long"), the
Size column sorted by its text, a target folder's name unescaped in the busy line, and Ctrl+Shift+C without a folder.
The error-path tests at the end (a full disk, a quota, a read-only file system, no permission, an I/O error half way
through a file; a move across file systems) found nothing: they keep it so. Everything changed is below tmp_path."""
import errno
import os
from pathlib import Path
import stat
import sys

import pytest

from sandbox_ai_app import files, i18n

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import names, select, settle, setup  # noqa: E402,F401
from test_files_remote import served  # noqa: E402,F401
from test_files_review_p1 import connect  # noqa: E402


@pytest.fixture
def german():
    old = i18n.language()
    i18n.set_language("de")
    yield
    i18n.set_language(old)


# -- a remote system --

def test_an_upload_into_a_remote_folder_that_exists_keeps_its_permissions(tmp_path):
    """Merged into a folder of the same name there, the folder was given the local folder's bits (a 0755 folder of a
    web server's became 0700); a local merge leaves a folder that was there as it was."""
    served, host = tmp_path / "srv", tmp_path / "host"
    (served / "site").mkdir(parents=True)
    (served / "site").chmod(0o755)
    (served / "site" / "old.html").write_text("old")
    (host / "site").mkdir(parents=True)
    (host / "site" / "new.html").write_text("new")
    (host / "site").chmod(0o700)
    (host / "fresh").mkdir()
    (host / "fresh").chmod(0o750)
    root, local = connect(served), files.Root(host, sandbox=False)
    try:
        files.copy_entry(local, "site", root, "", overwrite=True)
        assert sorted(os.listdir(served / "site")) == ["new.html", "old.html"]
        assert stat.S_IMODE((served / "site").stat().st_mode) == 0o755, "the folder that was there keeps its bits"
        files.copy_entry(local, "fresh", root, "")
        assert stat.S_IMODE((served / "fresh").stat().st_mode) == 0o750, "a folder the copy made gets the source's"
    finally:
        root.close()


def test_copy_path_in_the_sandbox_is_not_offered_for_a_remote_file(served, monkeypatch):
    """A remote path that is also the path of a running sandbox's folder here offered "Copy path in the sandbox" - which
    then said there was none."""
    page, home, ws, host = served
    monkeypatch.setattr(page, "sandbox_paths", {str(home): "/work"})
    select(page.left, "docs")
    menu = page.menu(page.left)
    action = next(action for action in menu.actions() if action.text() == "Copy path in the sandbox")
    assert not action.isEnabled()
    menu.deleteLater()


def test_owner_on_a_remote_system_says_at_once_that_it_is_not_done(served, monkeypatch):
    """Owner… showed a dialog of this machine's user and groups for a remote file, and refused only after OK."""
    page, home, ws, host = served
    select(page.left, "docs")
    opened = []
    monkeypatch.setattr(files_page.OwnerDialog, "exec", lambda self: opened.append(self) or 0)
    assert not page.chown_dialog(page.left)
    assert not opened and "remote" in page.message.text(), page.message.text()


def test_a_remote_folder_in_the_recent_menu_has_no_double_slash(served, wait):
    page, home, ws, host = served
    left = page.left
    left.go_path(str(home / "docs"))
    settle(left)
    left.go_path(str(home))
    settle(left)
    texts = [text for text, _key in left.recent_entries()]
    assert texts and not any("//" in text.split("://", 1)[-1] for text in texts), texts


def test_markdown_links_of_an_editor_go_through_the_new_connection(setup, tmp_path, monkeypatch, wait):
    """After SSH… connected again, an editor's own root was the new connection, but a link in its Markdown opened the
    file through the old one (closed: "the connection ended")."""
    from test_files_manager import editor_of
    page, ws, host, _ = setup
    home = tmp_path / "remote"
    home.mkdir()
    (home / "a.md").write_text("[next](b.md)\n")
    (home / "b.md").write_text("# B\n")
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    page.ssh_program = FAKE
    assert page.connect_remote("alice@fake")
    assert wait(lambda: page.left.remote() and page.left.listing is None, 10), page.message.text()
    old = page.left.root
    page.left.go_path(str(home.resolve()))
    settle(page.left)
    page.left.open_row(widgets.find_row(page.left.table, "a.md"))
    window = editor_of(page, wait)
    old.sftp.process.kill()
    old.sftp.process.wait()
    assert page.connect_remote("alice@fake")
    assert wait(lambda: page.left.root is not old and page.left.root.alive, 10)
    from PySide6.QtCore import QUrl
    window.open_link(QUrl("b.md"))
    assert wait(lambda: len(page.editors) == 2 and not page.editors[-1].loading, 10)
    other = page.editors[-1]
    assert other.root is page.left.root and "# B" in other.text.toPlainText(), other.notice.text()
    for editor in list(page.editors):
        editor.close()
    for root in page.remotes.values():
        root.close()


# -- the Markdown viewer --

def test_a_markdown_file_of_countless_images_beside_it_keeps_few_pictures(tmp_path, qapp):
    """Images beside the file beyond the IMAGES read each kept a placeholder of its own ("more than ... images"): a
    file of a few MiB naming 100,000 of them took gigabytes (pass 3 bounded only remote and outside ones)."""
    from sandbox_ai_app.markdown_view import IMAGES, ImageSource, MarkdownView
    view = MarkdownView(ImageSource(files.Root(tmp_path, sandbox=False), "a.md"))
    for index in range(5 * IMAGES):
        view.image(f"img/{index}.png")
    assert len(view.asked) == IMAGES, "the first IMAGES are read"
    assert len(view.images) <= 2 * IMAGES + 2


# -- German --

def test_the_listing_note_goes_once_the_folder_is_listed_in_german(setup, german, monkeypatch):
    """"Listing {path}…" is "{path} wird aufgelistet…" in German: the note was looked for by its English beginning and
    stayed after the folder was listed."""
    page, ws, host, _ = setup
    left = page.left
    monkeypatch.setattr(files_page, "LIST_WAIT", 0)
    original = files.Root.list

    def slow(self, relative=""):
        import time
        time.sleep(0.3)
        return original(self, relative)
    monkeypatch.setattr(files.Root, "list", slow)
    seen = []
    left.page.message.setText("")
    left.list(then=lambda listed: seen.append(page.message.text()))
    from conftest import wait_until
    assert wait_until(QtWidgets.QApplication.instance(), lambda: "aufgelistet" in page.message.text(), 5)
    settle(left)
    assert seen and seen[-1] == "", seen


def test_sizes_in_german_have_german_separators(tmp_path, qapp, german):
    """The Properties dialog wrote "1,000 Bytes" in German - one byte there; and "1.5 KiB"."""
    (tmp_path / "f").write_bytes(b"x" * 1536)
    from sandbox_ai_app.file_properties import PropertiesDialog
    dialog = PropertiesDialog(None, files.Root(tmp_path, sandbox=False), [("f", "f")], str(tmp_path), False)
    assert "1,5 KiB (1.536 Bytes)" in dialog.size.text(), dialog.size.text()
    dialog.close()
    assert files.size_text(2048) == "2 KiB" and files.size_text(1536) == "1,5 KiB"
    i18n.set_language("en")
    assert files.size_text(1536) == "1.5 KiB"


# -- the context menu --

def test_a_folder_name_with_an_ampersand_is_shown_as_it_is_in_the_menu(setup):
    """"Paste into {name}": an "&" of the name was taken for a shortcut key - a&b showed as "ab", b underlined."""
    page, ws, host, _ = setup
    (ws / "a&b").mkdir()
    page.left.list()
    select(page.left, "a&b")
    page.clipboard = files_page.Clip(page.left.root, [("a.txt", "a.txt", None)], True, page.left.path_text())
    menu = page.menu(page.left)
    paste = next(action for action in menu.actions() if action.text().startswith("Paste into"))
    assert paste.text() == "Paste into a&&b"
    menu.deleteLater()


# -- a copy beside the original --

def test_a_copy_beside_a_file_of_a_long_name_gets_a_name_that_fits(setup, wait):
    """The names for a copy beside the original ("x (copy).txt") were looked up as they were: for a name near the
    255 bytes a name may have, that failed with "File name too long" - and nothing was pasted, not even the items of
    short names."""
    page, ws, host, _ = setup
    long = "é" * 125 + ".txt"  # 254 bytes
    (ws / long).write_text("long")
    page.left.list()
    select(page.left, long, "a.txt")
    assert page.copy_items(page.left)
    assert page.paste(page.left)
    assert wait(lambda: (ws / "a (copy).txt").exists() and not page.task.running, 10), page.message.text()
    copies = [name for name in os.listdir(ws) if name.endswith(" (copy).txt") and name.startswith("é")]
    assert copies and len(os.fsencode(copies[0])) <= 255, (copies, page.message.text())
    assert (ws / copies[0]).read_text() == "long"
    for number in (1, 2, 20):
        name = files_page.copy_name("a" * 251 + ".txt", number)
        assert len(os.fsencode(name)) <= 255 and name.endswith(".txt") and "(copy" in name
    assert files_page.copy_name("\udcff" * 255).endswith("(copy)")
    assert len(os.fsencode(files_page.copy_name("\udcff" * 255))) <= 255


# -- sorting --

def test_the_size_and_modified_columns_sort_by_size_and_time(setup):
    """The Size column was sorted by its text: "1.5 KiB" < "100 B" < "2 KiB" < "512 B"; a time an agent set far ahead
    ("... s after 1970") sorted among the dates by its digits."""
    from PySide6.QtCore import Qt
    page, ws, host, _ = setup
    left = page.left
    for name, size in (("s100", 100), ("s2k", 2048), ("s1k5", 1536), ("s512", 512), ("s3m", 3 << 20)):
        (ws / name).write_bytes(b"x" * size)
    os.utime(ws / "s512", (2 ** 62, 2 ** 62))
    os.utime(ws / "s100", (0, 0))
    left.list()
    settle(left)

    def order(column, descending=False):
        left.table.sortByColumn(column, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
        found = [left.table.item(row, 0).text() for row in range(left.table.rowCount())]
        assert found[:2] == [".", ".."], "the folder shown and the one above stay first"
        return [name for name in found[2:] if name.startswith("s") and name != "sub"]
    assert order(2) == ["s100", "s512", "s1k5", "s2k", "s3m"]
    assert order(2, True) == ["s3m", "s2k", "s1k5", "s512", "s100"]
    assert order(3)[0] == "s100" and order(3)[-1] == "s512"
    left.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)


def test_the_busy_line_shows_a_target_folder_name_escaped(setup, wait):
    """"Copying 1 item(s) to {target}…" named the target folder as it was: a line break or an escape sequence an
    agent put in a folder's name went to the screen as it is (everywhere else names are shown escaped)."""
    page, ws, host, _ = setup
    (ws / "in\nside\x1b[31m").mkdir()
    page.left.list()
    page.left.go("in\nside\x1b[31m")
    settle(page.left)
    (host / "x.txt").write_text("x")
    page.right.list()
    select(page.right, "x.txt")
    assert page.copy_selected(page.right)
    text = page.task.label.text()
    assert "\n" not in text and "\x1b" not in text and "\\x0a" in text, repr(text)
    assert wait(lambda: not page.task.running, 10)
    assert (ws / "in\nside\x1b[31m" / "x.txt").read_text() == "x"


def test_files_of_another_program_dropped_while_a_job_runs_ask_nothing(setup, monkeypatch):
    """Pass 1 made a drop while a job runs ask nothing ("Wait until..."); pass 3's question before other programs'
    files go into an agent's folder came first again: asked, answered - and then refused."""
    page, ws, host, outside = setup
    monkeypatch.setattr(page.task, "call", object())  # a job runs
    plan = files_page.DropPlan(page.left, "", -1, files_page.Qt.DropAction.CopyAction,
                               paths=[str(outside / "precious.txt")])
    assert not page.run_drop(plan)
    assert widgets.SHOWN == [] and "Wait until" in page.message.text(), page.message.text()
    assert not (ws / "precious.txt").exists()


# -- the active pane --

def test_the_pane_whose_path_field_or_buttons_were_used_is_the_one_acted_on(setup, wait):
    """Only a click into a list made its pane the active one: a folder typed into the right pane's path field, then
    New folder (the toolbar's button), made the folder in the left pane - the one whose list had the keyboard before."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QFocusEvent
    page, ws, host, _ = setup
    page.activate(page.left)
    QtWidgets.QApplication.sendEvent(page.right.path, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason))
    assert page.active is page.right and page.right.title.font().bold()
    up = next(button for button in page.left.findChildren(QtWidgets.QPushButton) if button.accessibleName() == "Up")
    up.click()
    assert page.active is page.left
    QtWidgets.QApplication.sendEvent(page.left.roots, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
    page.activate(page.right)
    QtWidgets.QApplication.sendEvent(page.left.roots, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
    assert page.active is page.left


# -- keys --

def test_copy_relative_path_in_a_pane_without_a_folder_says_so(setup):
    """Ctrl+Shift+C in the sandbox pane while it shows nothing (a remote system whose connection ended, no folder to
    choose) failed with "ValueError: no path specified" (the menu greys it out; the key went on)."""
    from PySide6.QtCore import Qt
    page, ws, host, _ = setup
    page.left.root = None
    page.left.list()
    modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    assert page.key(page.left, Qt.Key.Key_C, modifiers)
    assert page.copy_path(page.left) is None and page.copy_path(page.left, form="sandbox") is None


# -- error paths: a full disk, a quota, a read-only file system, no permission, an I/O error --

ERRORS = (errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EIO)


def failing_writes(monkeypatch, code, marker=".sandbox-ai-app-"):
    """os.write fails with CODE for files whose path holds MARKER (the temporary names of copies and saves, and the
    staging folder of a download) once a first byte is written: a disk that fills up in the middle of a file."""
    real = os.write
    written = {}

    def write(fd, data):
        try:
            where = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            where = ""
        if marker in where:
            if written.get(fd):
                raise OSError(code, os.strerror(code))
            written[fd] = True
            return real(fd, bytes(data)[:1])
        return real(fd, data)
    monkeypatch.setattr(os, "write", write)


def leftovers(*folders):
    return [os.path.join(top, name) for folder in folders for top, dirs, names in os.walk(folder)
            for name in dirs + names if ".sandbox-ai-app-" in name]


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="finds the files written through /proc (Linux)")
@pytest.mark.parametrize("code", ERRORS)
def test_a_write_that_fails_half_way_leaves_nothing_behind(tmp_path, monkeypatch, code):
    """Every operation that writes - a copy of a file or a folder, a move between roots, a save, a download and an
    upload - stopped by an error in the middle of a file: the error is raised, the source is as it was, and nothing
    half written is left in the target (no temporary file, no folder the copy made)."""
    served, ws, host = tmp_path / "srv", tmp_path / "ws", tmp_path / "host"
    for folder in (served, ws, host):
        folder.mkdir()
    (ws / "big.bin").write_bytes(b"a" * 300_000)
    (ws / "tree" / "deep").mkdir(parents=True)
    (ws / "tree" / "deep" / "f.bin").write_bytes(b"b" * 300_000)
    (host / "kept.txt").write_text("as it was")
    (served / "remote.bin").write_bytes(b"c" * 300_000)
    sandbox, local = files.Root(ws), files.Root(host, sandbox=False)
    root = connect(served)
    try:
        failing_writes(monkeypatch, code)
        for step in (lambda: files.copy_entry(sandbox, "big.bin", local, ""),
                     lambda: files.copy_entry(sandbox, "tree", local, ""),
                     lambda: files.move_entry(sandbox, "tree", local, ""),
                     lambda: local.write_file("kept.txt", b"x" * 300_000),
                     lambda: files.copy_entry(root, "remote.bin", local, ""),
                     lambda: files.move_entry(root, "remote.bin", local, "")):
            with pytest.raises(OSError) as raised:
                step()
            assert raised.value.errno == code
        monkeypatch.undo()
        assert sorted(os.listdir(host)) == ["kept.txt"] and (host / "kept.txt").read_text() == "as it was"
        assert (ws / "tree" / "deep" / "f.bin").stat().st_size == 300_000 and (served / "remote.bin").exists()
        assert not leftovers(ws, host, served)
        # Out to the remote system: the local file read fails half way (an I/O error of the source's disk).
        real = os.read

        def read(fd, size):
            if os.readlink(f"/proc/self/fd/{fd}").endswith("big.bin") and os.lseek(fd, 0, os.SEEK_CUR):
                raise OSError(code, os.strerror(code))
            return real(fd, size)
        monkeypatch.setattr(os, "read", read)
        for step in (lambda: files.copy_entry(sandbox, "big.bin", root, ""),
                     lambda: files.move_entry(sandbox, "big.bin", root, "")):
            with pytest.raises(OSError) as raised:
                step()
            assert raised.value.errno == code
        monkeypatch.undo()
        assert sorted(os.listdir(served)) == ["remote.bin"] and (ws / "big.bin").stat().st_size == 300_000
    finally:
        monkeypatch.undo()
        root.close()


@pytest.mark.parametrize("overwrite", [False, True])
def test_a_move_within_a_folder_across_file_systems_is_copied(tmp_path, monkeypatch, overwrite):
    """A rename that crosses file systems (EXDEV: a mount inside the folder) is a copy and a removal - with
    renameat2, without it, and replacing a file that is there."""
    ws = tmp_path / "ws"
    (ws / "mnt").mkdir(parents=True)
    (ws / "a.txt").write_text("alpha")
    (ws / "d" / "e").mkdir(parents=True)
    (ws / "d" / "e" / "f.txt").write_text("deep")
    if overwrite:
        (ws / "mnt" / "a.txt").write_text("old")
    root = files.Root(ws)
    monkeypatch.setattr(files, "_renameat2", lambda *_args: errno.EXDEV)
    real_replace, real_rename = os.replace, os.rename

    def crossing(real):
        def call(src, dst, *args, **kwargs):
            if kwargs.get("src_dir_fd") is not None and ".sandbox-ai-app-" not in str(src):
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            return real(src, dst, *args, **kwargs)
        return call
    monkeypatch.setattr(os, "replace", crossing(real_replace))
    monkeypatch.setattr(os, "rename", crossing(real_rename))
    moved = files.move_entry(root, "a.txt", root, "mnt", overwrite=overwrite)
    assert moved.moved and (ws / "mnt" / "a.txt").read_text() == "alpha" and not (ws / "a.txt").exists()
    moved = files.move_entry(root, "d", root, "mnt", overwrite=overwrite)
    monkeypatch.undo()
    assert moved.moved and (ws / "mnt" / "d" / "e" / "f.txt").read_text() == "deep" and not (ws / "d").exists()
    assert not leftovers(ws)
