"""Review pass 2 of the Files page's operations: each test failed before its fix. A paste beside the original on a slow
file system, Cancel of a stalled remote copy or delete, characters the text editor turned into line breaks, a remote
save whose CLOSE failed, a save that took the agent's write right after it for its own, a move onto another name of the
same file, names that are no UTF-8 given to other programs and to the rename dialog, Enter in a path bar that shows an
escaped name, and permissions of a file nobody may read on macOS. Everything changed is below tmp_path."""
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from sandbox_ai_app import files, remote

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QMimeData, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from sandbox_ai_app import file_editor, files_page, widgets  # noqa: E402
from test_files_page import finish, names, select, settle, setup  # noqa: E402,F401
from test_files_remote import served  # noqa: E402,F401
from test_files_remote_stall import stall_file, stalling  # noqa: E402,F401


# -- the page --

def test_a_paste_beside_the_original_waits_for_a_slow_file_system(setup, wait, monkeypatch):
    """The names taken beside the original were read with on_root and its result unpacked at once: on a file system
    slower than LOCAL_GRACE on_root returns True (the step goes on in a thread) - a TypeError, nothing pasted."""
    page, ws, host, _ = setup
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.05)
    real = files.exists_in

    def slow(root, relative_dir, wanted):
        time.sleep(0.4)
        return real(root, relative_dir, wanted)
    monkeypatch.setattr(files, "exists_in", slow)
    select(page.left, "a.txt")
    assert page.copy_items(page.left)
    assert page.paste(page.left)
    assert wait(lambda: (ws / "a (copy).txt").exists() and not page.task.running, 10), page.message.text()
    assert (ws / "a (copy).txt").read_text() == "alpha"


def jobs(page):
    def copy_in():
        widgets.select_keys(page.right.table, ["in.txt"])
        return page.copy_selected(page.right)

    def delete():
        return page.delete_items(files_page.Chosen.of(page.left, [page.left.by_name["a.txt"]]))
    return {"WRITE": copy_in, "REMOVE": delete}


@pytest.mark.parametrize("request_name", ["WRITE", "REMOVE"])
def test_cancel_interrupts_a_stalled_remote_copy_or_delete(stalling, qapp, wait, request_name):
    """Cancel interrupts a stalled request only for the roots root_job named (job_roots): a copy, a move, a delete
    started their job themselves and left job_roots empty (or another job's) - Cancel waited for the request timeout."""
    page, home, ws, host, stall = stalling
    page.left.root.sftp.timeout = 60
    page.replace.setChecked(False)
    (host / "in.txt").write_text("in")
    page.right.list()
    settle(page.right)
    stall.write_text(request_name)
    assert jobs(page)[request_name]()
    assert wait(lambda: page.task.running, 5)
    time.sleep(0.5)
    page.task.cancel_button.click()
    began = time.monotonic()
    assert wait(lambda: not page.task.running, 8), "Cancel ended it"
    assert time.monotonic() - began < 8 and not page.left.root.alive, "the stalled request was interrupted"
    stall.unlink()


def test_the_text_editor_never_turns_characters_into_line_breaks(tmp_path, qapp, wait):
    """U+FDD0 and U+FDD1 are what Qt's text document marks frames with: QPlainTextEdit gave them back as line breaks,
    so a file holding one opened as text and was saved with a line break in its place."""
    assert file_editor.split_text("a\ufdd0b") is None and file_editor.split_text("a\ufdd1b") is None
    (tmp_path / "t.txt").write_bytes("one\ufdd0two\n".encode())
    window = file_editor.EditorWindow(None, files.Root(tmp_path), "t.txt", str(tmp_path / "t.txt"))
    assert wait(lambda: not window.loading, 10)
    assert window.text.isReadOnly() and window.contents() == "one\ufdd0two\n".encode()
    window.dirty = False
    window.close()


def test_the_path_bar_shows_an_escaped_name_and_enter_stays_there(setup):
    """The path bar shows names escaped (a line break as \\x0a): Enter in it went to the folder of that escaped text,
    which does not exist - an error, and the pane jumped to HOME."""
    page, ws, host, _ = setup
    folder = host / "two\nlines"
    folder.mkdir()
    (folder / "inside.txt").write_text("x")
    page.right.go_path(str(folder))
    settle(page.right)
    assert names(page.right) == ["inside.txt"] and "\\x0a" in page.right.path.text()
    page.right.go_path()  # (Enter, the text as shown)
    settle(page.right)
    assert page.right.path_text() == str(folder) and names(page.right) == ["inside.txt"], page.message.text()
    page.right.path.setText(page.right.path.text() + "/deeper")
    (folder / "deeper").mkdir()
    page.right.go_path()
    settle(page.right)
    assert page.right.path_text() == str(folder / "deeper"), page.message.text()
    spaced = host / "ends in a space "
    spaced.mkdir()
    (spaced / "here.txt").write_text("x")
    (host / "ends in a space").mkdir()
    page.right.go(files_page.host_relative(str(spaced)))
    settle(page.right)
    page.right.go_path()
    settle(page.right)
    assert page.right.path_text() == str(spaced) and names(page.right) == ["here.txt"]
    page.right.go_path(str(host))
    settle(page.right)
    page.right.go_recent(str(spaced))  # (Recent: the path as it is)
    settle(page.right)
    assert page.right.path_text() == str(spaced), page.message.text()


# -- names that are no UTF-8 --

@pytest.fixture
def odd_name(setup):
    """A host file whose name holds the byte 0xff (no UTF-8) beside a file with the name Qt would make of it."""
    page, ws, host, _ = setup
    os.close(os.open(os.path.join(os.fsencode(host), b"a\xffb.txt"), os.O_WRONLY | os.O_CREAT, 0o644))
    with open(os.path.join(os.fsencode(host), b"a\xffb.txt"), "wb") as stream:
        stream.write(b"the odd one")
    (host / "ab.txt").write_text("another file")
    page.right.list()
    settle(page.right)
    return page, ws, host, os.fsdecode(b"a\xffb.txt")


def test_a_name_that_is_no_utf8_goes_to_other_programs_as_itself(odd_name):
    """Qt drops what is no UTF-8 from a str (a lone surrogate): the file URL another program got for a\\xffb.txt named
    ab.txt - another file."""
    page, ws, host, odd = odd_name
    entry = page.right.by_name[odd]
    urls = page.export_urls(page.right, [entry])
    assert all(files_page.local_path(url) == str(host / odd) for url in urls)
    assert all(not url.toLocalFile().endswith("/ab.txt") for url in urls)


def test_a_dropped_or_pasted_url_of_a_name_that_is_no_utf8_copies_that_file(odd_name, wait):
    page, ws, host, odd = odd_name
    mime = QMimeData()
    mime.setUrls([QUrl.fromEncoded(f"file://{host}/a%FFb.txt".encode())])
    QGuiApplication.clipboard().setMimeData(mime)
    page.clipboard = None
    assert page.paste(page.left)
    finish(page, wait)
    copied = os.path.join(os.fsencode(ws), b"a\xffb.txt")
    assert os.path.exists(copied) and open(copied, "rb").read() == b"the odd one", page.message.text()
    assert not (ws / "ab.txt").exists()


def test_the_rename_dialog_left_as_it_was_renames_nothing(odd_name, monkeypatch, wait):
    """The rename dialog showed a\\xffb.txt as "ab.txt" (Qt dropped the byte); OK without a change renamed the file to
    ab.txt."""
    page, ws, host, odd = odd_name
    (host / "ab.txt").unlink()
    page.right.list()
    settle(page.right)
    shown = []

    def get_text(_parent, _title, _label, _echo, text):
        shown.append(text)
        return QtWidgets.QLineEdit(text).text(), True  # (what Qt makes of it, unchanged)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", get_text)
    page.rename_dialog(page.right, page.right.by_name[odd])
    settle(page.right)
    assert shown and os.path.exists(os.path.join(os.fsencode(host), b"a\xffb.txt")) and not (host / "ab.txt").exists()


# -- files.py --

def test_a_save_never_takes_a_write_right_after_it_for_its_own(tmp_path, monkeypatch):
    """Root.write_file stat'ed the file after it had renamed it into place: what the agent wrote in between became the
    identity of "what was saved", and the next save replaced the agent's write without asking."""
    root = files.Root(tmp_path)
    (tmp_path / "f.txt").write_text("old")
    first = root.read_file("f.txt")
    real = files._place

    def place_then_agent(temporary, name, dir_fd, overwrite, identity=None):
        real(temporary, name, dir_fd, overwrite, identity)
        with open(tmp_path / "f.txt", "a") as stream:
            stream.write(" and the agent's line")
    monkeypatch.setattr(files, "_place", place_then_agent)
    saved = root.write_file("f.txt", b"mine", expected=first.identity)
    monkeypatch.setattr(files, "_place", real)
    with pytest.raises(files.Changed):
        root.write_file("f.txt", b"mine again", expected=saved)
    assert (tmp_path / "f.txt").read_text() == "mine and the agent's line"


def test_a_move_onto_another_name_of_the_same_file_moves_it(tmp_path):
    """rename(2) does nothing when both names are hard links of one file: the move said "moved", the source stayed."""
    root = files.Root(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x")
    os.link(tmp_path / "a" / "x.txt", tmp_path / "b" / "x.txt")
    result = files.move_entry(root, "a/x.txt", root, "b", overwrite=True)
    assert result.moved and not (tmp_path / "a" / "x.txt").exists() and (tmp_path / "b" / "x.txt").read_text() == "x"


def test_permissions_of_a_file_nobody_may_read_without_o_path(tmp_path, monkeypatch):
    """macOS has no O_PATH: chmod opened the file to read it (O_RDONLY), which a file of mode 000 (or 200), or a folder
    nobody may list, refuses - its permissions could not be changed at all. (Here as macOS answers: EACCES.)"""
    if os.chmod not in os.supports_follow_symlinks or os.chmod not in os.supports_dir_fd:
        pytest.skip("no fchmodat with AT_SYMLINK_NOFOLLOW here (macOS has it)")
    root = files.Root(tmp_path, sandbox=False)
    (tmp_path / "locked").write_text("x")
    (tmp_path / "closed").mkdir()
    os.chmod(tmp_path / "locked", 0)
    os.chmod(tmp_path / "closed", 0)
    monkeypatch.setattr(files, "O_PATH", 0)
    real_open = os.open

    def macos_open(path, flags, *args, **kwargs):
        if os.fspath(path) in ("locked", "closed") and not flags & (os.O_WRONLY | os.O_RDWR):
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", macos_open)
    try:
        root.chmod_entry("locked", 0o600)
        root.chmod_entry("closed", 0o700)
        assert stat.S_IMODE(os.lstat(tmp_path / "locked").st_mode) == 0o600
        assert stat.S_IMODE(os.lstat(tmp_path / "closed").st_mode) == 0o700
    finally:
        monkeypatch.undo()
        os.chmod(tmp_path / "locked", 0o600)  # (pytest removes tmp_path later)
        os.chmod(tmp_path / "closed", 0o700)


# -- remote.py --

def test_a_remote_save_whose_close_fails_never_replaces_the_file(tmp_path, monkeypatch):
    """put() closed the written file with close_handle, which swallows every error: a CLOSE that failed (a full NFS or
    quota-bound file system: what was written may not be there) was renamed over the file all the same."""
    (tmp_path / "notes.txt").write_text("the file as it was")
    monkeypatch.setenv("FAKE_SFTP_FAIL_CLOSE", "1")
    root = remote.RemoteRoot(remote.Target("fake"), str(tmp_path), ssh=FAKE, timeout=5)
    try:
        with pytest.raises(OSError):
            root.write_file("notes.txt", b"new text")
        assert (tmp_path / "notes.txt").read_text() == "the file as it was"
        assert [name for name in os.listdir(tmp_path) if name.endswith(".part")] == []
    finally:
        root.close()


def test_bytes_mended_in_the_hex_editor_are_saved_from_the_markdown_view(tmp_path, qapp, wait):
    """A text that cannot be given back as it was read (mixed line endings) is shown read-only, the hex editor mends
    it; the Markdown view (through the text view) then showed the mended text - and Save wrote the file as it was
    read (contents(): "not text_ok", the original): the change was lost, the window said Saved."""
    (tmp_path / "a.md").write_bytes(b"# T\r\nline\n")
    window = file_editor.EditorWindow(None, files.Root(tmp_path), "a.md", str(tmp_path / "a.md"), "markdown")
    assert wait(lambda: not window.loading, 10)
    assert window.view == "markdown" and not window.text_ok
    assert window.switch("hex") and window.hex.set_byte(3, ord(" "))
    assert window.switch("markdown")
    assert window.contents() == b"# T \nline\n"
    window.save()
    assert wait(lambda: not window.saving, 10)
    assert (tmp_path / "a.md").read_bytes() == b"# T \nline\n", window.notice.text()
    window.close()


def test_a_file_on_a_read_only_file_system_is_not_said_to_be_read_only_for_you(tmp_path, monkeypatch):
    """Saving on a read-only mount said the file was read-only for you and to change its permissions first - which
    cannot help there."""
    root = files.Root(tmp_path)
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    real = os.fstatvfs

    class ReadOnly:
        def __init__(self, info):
            self.info = info

        def __getattr__(self, name):
            return getattr(self.info, name)

        @property
        def f_flag(self):
            return self.info.f_flag | os.ST_RDONLY
    monkeypatch.setattr(os, "fstatvfs", lambda fd: ReadOnly(real(fd)))
    with pytest.raises(files.Refused) as refused:
        root.write_file("f.txt", b"y")
    assert "read-only file system" in str(refused.value) and "permissions" not in str(refused.value)


def test_properties_read_on_their_own_wait_for_a_slow_file_system(tmp_path, qapp, monkeypatch):
    """PropertiesDialog without INFOS read them with on_root and took its result at once: True (still reading) on a
    file system slower than LOCAL_GRACE - a TypeError. (The page reads them first and hands them over.)"""
    from sandbox_ai_app.file_properties import PropertiesDialog
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.01)
    real = files.entry_infos

    def slow(root, items):
        time.sleep(0.2)
        return real(root, items)
    monkeypatch.setattr(files, "entry_infos", slow)
    dialog = PropertiesDialog(None, files.Root(tmp_path), [("f.txt", "f.txt")], str(tmp_path), False)
    assert not dialog.infos and "Read in the background first" in dialog.note.text()
    dialog.close()


@pytest.mark.parametrize("planted", ["../../../escaped.txt", "ABSOLUTE"])
def test_a_name_a_server_sends_never_writes_outside_the_copy(tmp_path, monkeypatch, planted):
    """A download wrote each name of the server's listing relative to the staging folder (os.open(name, dir_fd=...)):
    a malicious or broken server that lists "../../../x" or "/home/you/x" had the copy create that file anywhere you
    may write (scp's CVE-2019-6111; OpenSSH's sftp refuses such names). A name of a listing is one name, or nothing."""
    served = tmp_path / "srv"
    (served / "docs").mkdir(parents=True)
    (served / "docs" / "a.txt").write_text("alpha")
    target = tmp_path / "host"
    target.mkdir()
    planted = str(tmp_path / "escaped.txt") if planted == "ABSOLUTE" else planted
    monkeypatch.setenv("FAKE_SFTP_EXTRA_NAME", planted)
    root = remote.RemoteRoot(remote.Target("fake"), str(served), ssh=FAKE, timeout=5)
    try:
        try:
            files.copy_entry(root, "docs", files.Root(target, sandbox=False), "")
        except (OSError, ValueError):
            pass
        assert not (tmp_path / "escaped.txt").exists(), "written outside the copy"
        assert sorted(os.listdir(target)) == ["docs"] and sorted(os.listdir(target / "docs")) == ["a.txt"]
        assert [entry.name for entry in root.list("docs")] == ["a.txt"], "no such name is listed"
        assert root.measure("docs").files == 1
    finally:
        root.close()


def test_a_private_file_removed_while_it_was_edited_comes_back_private(tmp_path, qapp, wait, monkeypatch):
    """Saved after it was removed on disk (Replace), a file was made anew with 0666 less the umask: a key of 0600
    came back readable by everyone."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=1\n")
    secret.chmod(0o600)
    window = file_editor.EditorWindow(None, files.Root(tmp_path), ".env", str(secret))
    assert wait(lambda: not window.loading, 10)
    window.text.selectAll()
    window.text.insertPlainText("TOKEN=2\n")
    secret.unlink()
    window.save()
    assert wait(lambda: not window.saving and secret.exists(), 10), window.notice.text()
    assert secret.read_text() == "TOKEN=2\n" and stat.S_IMODE(secret.stat().st_mode) == 0o600
    window.close()


def test_a_download_onto_a_name_that_exists_is_refused_before_anything_is_read(tmp_path, monkeypatch):
    """Without Replace, a remote folder was read whole into the staging folder (up to 1 GiB) - and only then refused,
    because its name exists in the target folder."""
    served = tmp_path / "srv"
    (served / "docs").mkdir(parents=True)
    (served / "docs" / "a.txt").write_text("alpha" * 1000)
    target = tmp_path / "host"
    (target / "docs").mkdir(parents=True)
    log = tmp_path / "log"
    monkeypatch.setenv("FAKE_SFTP_LOG", str(log))
    root = remote.RemoteRoot(remote.Target("fake"), str(served), ssh=FAKE, timeout=5)
    try:
        with pytest.raises(files.Refused, match="already exists"):
            files.copy_entry(root, "docs", files.Root(target, sandbox=False), "")
        assert "READ" not in log.read_text().split() and os.listdir(target) == ["docs"]
    finally:
        root.close()


@pytest.fixture
def big_remote(tmp_path):
    served = tmp_path / "srv"
    served.mkdir()
    (served / "big.bin").write_bytes(os.urandom(64 * remote.CHUNK))  # (four windows of requests)
    (served / "small.txt").write_text("small")
    host = tmp_path / "host"
    host.mkdir()
    (host / "up.bin").write_bytes(os.urandom(64 * remote.CHUNK))
    return served, host


def cancel_after(calls):
    count = [0]

    def cancelled():
        count[0] += 1
        return count[0] > calls
    return cancelled


@pytest.mark.parametrize("how", ["read error", "cancelled download", "cancelled upload"])
def test_a_transfer_cut_short_in_a_window_leaves_the_connection_in_step(big_remote, monkeypatch, how):
    """A READ that failed, or a Cancel (and a budget, a full disk) in the middle of a window of requests, raised at
    once and left the window's other answers in the pipe: the next request read one of them - "the remote system sent
    a damaged answer" - and the connection ended. After Cancel of a download or an upload, nothing worked on it."""
    served, host = big_remote
    if how == "read error":
        monkeypatch.setenv("FAKE_SFTP_READ_FAIL_AT", str(2 * remote.CHUNK))
    root = remote.RemoteRoot(remote.Target("fake"), str(served), ssh=FAKE, timeout=5)
    try:
        with pytest.raises((OSError, ValueError)):
            if how == "cancelled upload":
                files.copy_entry(files.Root(host, sandbox=False), "up.bin", root, "", cancelled=cancel_after(6))
            else:
                files.copy_entry(root, "big.bin", files.Root(host, sandbox=False), "",
                                 cancelled=cancel_after(6) if how.startswith("cancelled") else None)
        assert root.alive
        assert sorted(entry.name for entry in root.list("")) == ["big.bin", "small.txt"]
        assert root.read_file("small.txt").data == b"small"
        assert not [name for name in os.listdir(served) + os.listdir(host) if name.endswith(".part")], "nothing left"
    finally:
        root.close()
