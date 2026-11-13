"""Review pass 3 of the Files page's operations: each test failed before its fix. Files of other programs pasted or
dropped into an agent's folder unasked, a file with several names counted more than once, a save that took a file
from its owner (and its extended attributes), a local step on a hung file system without Cancel, a listing that timed
out under the path of the folder before, case-only renames and line breaks in names on a remote system (a delete that
went half way), a remote move onto a folder, files dragged out with the bits they had when they were listed, the note
about other hosts under a remote system, a Markdown file of countless remote images, a remote item's owners in this
machine's names, and the Credentials page's Git note cut short. Everything changed is below tmp_path."""
import errno
import os
from pathlib import Path
import stat
import sys
import threading
import time

import pytest

from sandbox_ai_app import files, remote

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QMimeData, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from sandbox_ai_app import file_editor, files_page, widgets  # noqa: E402
from test_files_page import names, select, settle, setup  # noqa: E402,F401
from test_files_remote import served  # noqa: E402,F401
from test_files_review_p1 import connect  # noqa: E402

# -- other programs' files into an agent's folder --

def foreign_clipboard(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    QGuiApplication.clipboard().setMimeData(mime)


def test_files_of_other_programs_go_into_an_agents_folder_only_after_a_question(setup, wait, monkeypatch):
    """With --gui-wayland an agent may put file URLs of the user's files on the clipboard (or drag them over): Ctrl+V or a
    drop in the sandbox pane copied them into the agent's folder, unasked."""
    page, ws, host, outside = setup
    secret = outside / "precious.txt"
    page.clipboard = None
    foreign_clipboard(secret)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert not page.paste(page.left)
    time.sleep(0.2)
    assert not (ws / "precious.txt").exists()
    title, text, details = widgets.SHOWN[-1]
    assert str(secret) in (details or "") and "another program" in text, "what and from where"
    plan = files_page.DropPlan(page.left, "", -1, Qt.DropAction.CopyAction, paths=[str(secret)])
    assert not page.run_drop(plan)
    assert not (ws / "precious.txt").exists()
    # Said yes: copied.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert page.paste(page.left)
    assert wait(lambda: (ws / "precious.txt").exists() and not page.task.running, 10), page.message.text()
    # Into a folder of this machine (not an agent's): nothing to ask.
    asked = len(widgets.SHOWN)
    assert page.paste(page.right)
    assert wait(lambda: (host / "precious.txt").exists() and not page.task.running, 10), page.message.text()
    assert len(widgets.SHOWN) == asked


# -- Properties: a file with several names --

def test_a_file_with_several_names_is_counted_once(tmp_path, qapp, wait):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_bytes(b"x" * 1000)
    os.link(tmp_path / "d" / "x", tmp_path / "d" / "y")
    os.link(tmp_path / "d" / "x", tmp_path / "top")
    root = files.Root(tmp_path, sandbox=False)
    counted = root.measure("d")
    assert (counted.bytes, counted.files) == (1000, 2)
    from sandbox_ai_app.file_properties import PropertiesDialog
    dialog = PropertiesDialog(None, root, [("top", "top"), ("d", "d")], str(tmp_path), False)
    assert wait(lambda: not dialog.counting, 10)
    assert "(1,000 bytes)" in dialog.size.text(), dialog.size.text()
    dialog.close()


# -- a save keeps what the file had --

class Theirs:
    """The lstat of a file of another user (UID 4242, group 4343): no chown is needed to make one (none may be done
    here - no root, or a user namespace that forbids it)."""
    st_uid, st_gid = 4242, 4343

    def __init__(self, info):
        self.info = info

    def __getattr__(self, name):
        return getattr(self.info, name)


def theirs(monkeypatch, name):
    real = os.stat

    def looked_at(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        return Theirs(info) if os.fspath(path) == name else info
    monkeypatch.setattr(files.os, "stat", looked_at)


def test_a_save_keeps_the_owner_and_group_of_the_file(tmp_path, monkeypatch):
    """Renamed over a file of another user that you may write (its group's), the new file was yours: its owner and its
    group were lost, unasked."""
    path = tmp_path / "theirs.conf"
    path.write_text("old")
    root = files.Root(tmp_path, sandbox=False)
    read = root.read_file("theirs.conf")
    theirs(monkeypatch, "theirs.conf")
    given = []
    monkeypatch.setattr(files.os, "fchown", lambda fd, uid, gid: given.append((uid, gid)))
    root.write_file("theirs.conf", b"new", read.identity, mode=read.mode)
    assert path.read_text() == "new" and given == [(4242, 4343)], "the new file given to them"
    # Where it cannot be given to them (only root may): asked first - written in place, it stays theirs.
    read = root.read_file("theirs.conf")

    def refused(*_args):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(files.os, "fchown", refused)
    with pytest.raises(files.NotKept) as caught:
        root.write_file("theirs.conf", b"newer", read.identity, mode=read.mode)
    assert "4242" in str(caught.value) and path.read_text() == "new"
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".part")]
    root.write_file("theirs.conf", b"newer", read.identity, in_place=True)
    assert path.read_text() == "newer"


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="no extended attributes in Python here")
def test_a_save_keeps_the_extended_attributes_of_the_file(tmp_path):
    path = tmp_path / "tagged.txt"
    path.write_text("old")
    try:
        os.setxattr(path, "user.origin", b"kept")
    except OSError:
        pytest.skip("this file system has no user extended attributes")
    root = files.Root(tmp_path, sandbox=False)
    read = root.read_file("tagged.txt")
    root.write_file("tagged.txt", b"new", read.identity, mode=read.mode)
    assert path.read_text() == "new" and os.getxattr(path, "user.origin") == b"kept"


def test_the_editor_asks_before_it_writes_a_file_of_someone_else_in_place(tmp_path, qapp, wait, monkeypatch):
    (tmp_path / "f.txt").write_text("old")
    root = files.Root(tmp_path, sandbox=False)
    real = root.write_file

    def theirs(relative, data, expected=None, in_place=False, mode=None):
        if not in_place:
            raise files.NotKept("f.txt belongs to UID 4242: saved through a new file it became yours.")
        return real(relative, data, expected, in_place=in_place, mode=mode)
    monkeypatch.setattr(root, "write_file", theirs)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    window = file_editor.EditorWindow(None, root, "f.txt", str(tmp_path / "f.txt"), "text")
    assert wait(lambda: not window.loading, 10)
    window.text.setPlainText("new")
    assert window.save()
    assert wait(lambda: (tmp_path / "f.txt").read_text() == "new" and not window.saving, 10), window.notice.text()
    title, text, _ = widgets.SHOWN[-1]
    assert "4242" in text and "folder you may not write" not in text, text
    window.dirty = False
    window.close()


# -- a local step on a file system that hangs --

def test_a_local_step_that_hangs_runs_as_the_pages_job_with_cancel(setup, wait, monkeypatch):
    """A local step not done within LOCAL_GRACE went on in a thread of its own: no Cancel, and every button stayed
    enabled - a copy or a delete started meanwhile worked on what the hung step was changing."""
    page, ws, host, _ = setup
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.05)
    release = threading.Event()
    real = files.Root.rename_entry

    def hung(self, relative, new_name):
        release.wait(20)
        return real(self, relative, new_name)
    monkeypatch.setattr(files.Root, "rename_entry", hung)
    try:
        assert page.rename(page.left, page.left.by_name["a.txt"], "b.txt")
        assert page.task.running and not page.buttons["copy"].isEnabled() and not page.task.cancel_button.isHidden()
        page.task.cancel_button.click()
        assert wait(lambda: not page.task.running, 5), "Cancel gave the page back"
        assert "Cancelled" in page.message.text() and page.buttons["copy"].isEnabled()
    finally:
        release.set()
    # A step that is only slow: done as the job, renamed.
    monkeypatch.setattr(files.Root, "rename_entry", lambda self, relative, new: (time.sleep(0.3), real(
        self, relative, new))[1])
    assert wait(lambda: not (ws / "a.txt").exists() or (ws / "b.txt").exists(), 5)  # (the one cancelled went on)
    target = "c.txt"
    source = "b.txt" if (ws / "b.txt").exists() else "a.txt"
    page.left.list()
    settle(page.left)
    assert page.rename(page.left, page.left.by_name[source], target)
    assert wait(lambda: (ws / target).exists() and not page.task.running, 10), page.message.text()


def test_a_listing_that_times_out_shows_the_folder_it_was_for(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    monkeypatch.setattr(files_page, "LIST_TIMEOUT", 0.3)
    real = files.Root.list

    def slow(self, relative=""):
        if relative == "sub":
            time.sleep(1.5)
        return real(self, relative)
    monkeypatch.setattr(files.Root, "list", slow)
    page.left.go("sub")
    assert wait(lambda: page.left.listing is None, 10)
    assert page.left.path.text() == str(ws / "sub"), "the path bar named the folder before"
    time.sleep(1.5)


# -- a remote system --

@pytest.fixture
def served_here(tmp_path):
    folder = tmp_path / "srv"
    folder.mkdir()
    return folder


def test_a_case_only_rename_on_a_remote_system_that_ignores_case(served_here):
    """OpenSSH's sftp-server renames a file with link() and a folder after a stat() of the new name: on a server whose
    file system ignores the case (macOS) the new name "exists" - it is the file itself - and a.txt -> A.txt failed."""
    (served_here / "a.txt").write_text("alpha")
    (served_here / "dir").mkdir()
    (served_here / "B.txt").write_text("beta")
    root = connect(served_here, FAKE_SFTP_CASELESS="1")
    try:
        root.rename_entry("a.txt", "A.txt")
        root.rename_entry("dir", "Dir")
        assert sorted(os.listdir(served_here)) == ["A.txt", "B.txt", "Dir"]
        with pytest.raises(files.Refused, match="already exists"):  # (another file, of another case: refused)
            root.rename_entry("A.txt", "b.txt")
        assert (served_here / "A.txt").read_text() == "alpha" and (served_here / "B.txt").read_text() == "beta"
    finally:
        root.close()


def test_a_remote_rename_onto_an_existing_name_says_so(served_here):
    (served_here / "a.txt").write_text("alpha")
    (served_here / "b.txt").write_text("beta")
    root = connect(served_here)
    try:
        with pytest.raises(files.Refused, match="already exists"):
            root.rename_entry("a.txt", "b.txt")
    finally:
        root.close()


@pytest.fixture
def broken_names(tmp_path):
    served, host = tmp_path / "srv", tmp_path / "host"
    (served / "d").mkdir(parents=True)
    (served / "d" / "bad\nname").write_text("a line break")  # (first: a tmpfs lists the newest first, this one last)
    for index in range(20):
        (served / "d" / f"ok{index:02}.txt").write_text("fine")
    host.mkdir()
    (host / "up").mkdir()
    (host / "up" / "ok.txt").write_text("fine")
    (host / "up" / "bad\nname").write_text("a line break")
    return served, host


def test_a_remote_name_with_a_line_break_is_skipped_and_said(broken_names):
    served, host = broken_names
    root, local = connect(served), files.Root(host, sandbox=False)
    try:
        done = files.copy_entry(root, "d", local, "")
        assert sorted(os.listdir(host / "d")) == [f"ok{index:02}.txt" for index in range(20)]
        assert [path for path, _why in done.skipped] == ["d/bad\nname"]
        moved = files.move_entry(root, "d", local, "", overwrite=True)
        assert not moved.moved and (served / "d" / "ok00.txt").exists(), "the source was kept"
        up = files.copy_entry(local, "up", root, "")
        assert os.listdir(served / "up") == ["ok.txt"] and [path for path, _ in up.skipped] == ["up/bad\nname"]
    finally:
        root.close()


def test_a_remote_delete_of_a_folder_with_a_line_break_in_a_name_deletes_nothing(broken_names):
    """The count before a delete looked only at folders: a file whose name has a line break passed, and the delete went
    through the folder until it came to it (part of the folder gone, then refused)."""
    served, _ = broken_names
    root = connect(served)
    try:
        with pytest.raises(files.Refused, match="line break"):
            root.delete_entry("d")
        assert len(os.listdir(served / "d")) == 21
    finally:
        root.close()


def test_a_remote_move_onto_an_existing_folder_merges_as_a_local_one(served_here):
    (served_here / "src" / "x").mkdir(parents=True)
    (served_here / "src" / "x" / "one.txt").write_text("1")
    (served_here / "src" / "x" / "same.txt").write_text("new")
    (served_here / "dst" / "x").mkdir(parents=True)
    (served_here / "dst" / "x" / "two.txt").write_text("2")
    (served_here / "dst" / "x" / "same.txt").write_text("old")
    root = connect(served_here)
    try:
        with pytest.raises(files.Refused, match="already exists"):
            files.move_entry(root, "src/x", root, "dst")
        assert (served_here / "src" / "x" / "one.txt").exists()
        result = files.move_entry(root, "src/x", root, "dst", overwrite=True)
        assert result.moved and not (served_here / "src" / "x").exists()
        assert sorted(os.listdir(served_here / "dst" / "x")) == ["one.txt", "same.txt", "two.txt"]
        assert (served_here / "dst" / "x" / "same.txt").read_text() == "new"
    finally:
        root.close()


def test_a_remote_save_keeps_the_owner_or_asks_to_write_in_place(served_here, tmp_path):
    path, log = served_here / "theirs.conf", tmp_path / "requests.log"
    path.write_text("old")
    root = connect(served_here, FAKE_SFTP_FOREIGN="theirs.conf", FAKE_SFTP_LOG=str(log))
    try:
        read = root.read_file("theirs.conf")
        root.write_file("theirs.conf", b"new", read.identity, mode=read.mode)
        assert path.read_text() == "new" and "CHOWN 4242:4343" in log.read_text(), "the new file given to them"
    finally:
        root.close()
    root = connect(served_here, FAKE_SFTP_FOREIGN="theirs.conf", FAKE_SFTP_NO_CHOWN="1")
    try:
        read = root.read_file("theirs.conf")
        with pytest.raises(files.NotKept, match="4242"):
            root.write_file("theirs.conf", b"newer", read.identity, mode=read.mode)
        assert path.read_text() == "new" and not [n for n in os.listdir(served_here) if n.endswith(".part")]
        root.write_file("theirs.conf", b"newer", read.identity, in_place=True)
        assert path.read_text() == "newer"
    finally:
        root.close()


def test_no_note_about_other_hosts_under_a_remote_system(served):
    page, home, ws, host = served
    assert page.left.remote() and page.left.note.isHidden(), page.left.note.text()
    page.left.roots.setCurrentIndex(page.left.roots.findData(str(ws)))
    settle(page.left)
    assert not page.left.remote() and not page.left.note.isHidden()


def test_properties_of_a_remote_item_show_its_owners_as_numbers(served, wait):
    page, home, ws, host = served
    page.left.go(str(home / "docs").lstrip("/"))
    settle(page.left)
    select(page.left, "a.txt")
    assert page.properties(page.left)
    assert wait(lambda: page.dialogs, 10)
    dialog = page.dialogs[-1]
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert f"{os.getuid()}" in texts and "Status changed" not in texts, texts
    dialog.close()


# -- what goes to other programs --

def test_dragged_out_files_are_looked_at_as_they_are_now(setup):
    """The bits and the kind a file had when it was listed decided: made executable (or swapped for a link) by the
    agent since, it went to another program all the same."""
    page, ws, host, outside = setup
    select(page.left, "a.txt")
    entries = page.left.chosen()
    assert page.export_urls(page.left, entries)
    os.chmod(ws / "a.txt", 0o755)
    assert page.export_urls(page.left, entries) == []
    os.unlink(ws / "a.txt")
    os.symlink(outside / "precious.txt", ws / "a.txt")
    assert page.export_urls(page.left, entries) == []


# -- the Markdown viewer --

def test_a_markdown_file_of_countless_remote_images_keeps_few_pictures(tmp_path, qapp):
    """Each remote image's name got a placeholder of its own, kept: a file of a few MiB naming 100,000 of them took
    gigabytes."""
    from sandbox_ai_app.markdown_view import IMAGES, ImageSource, MarkdownView
    view = MarkdownView(ImageSource(files.Root(tmp_path, sandbox=False), "a.md"))
    for index in range(3 * IMAGES):
        view.image(f"https://example.org/{index}.png")
        view.image(f"/abs/{index}.png")
    assert len(view.images) <= 2 * IMAGES + 2


# -- the Credentials page --

@pytest.mark.parametrize("look", ["system", "light", "hacker", "win311", "mac", "fallout", "amber"])
@pytest.mark.parametrize("points", [0, 1, 3])
@pytest.mark.parametrize("language", ["en", "de"])
def test_the_git_server_note_never_needs_the_dialog_to_grow(qapp, wait, language, points, look):
    """When the Git server's choice appears, the dialog had to grow: a window manager that keeps a dialog's size once it
    is shown (tiling, some Wayland compositors) left it as it was, and the note above the choice was cut to one line."""
    from PySide6.QtGui import QFont
    from sandbox_ai_app import credentials_page, i18n, theme
    i18n.set_language(language)
    theme.apply(look)
    font = QFont(qapp.font())
    bigger = QFont(font)
    bigger.setPointSizeF(font.pointSizeF() + points)  # (as a theme of the app's, a larger font of the system's)
    qapp.setFont(bigger)
    try:
        class Run:
            name, agent, run = "claude-abc", "claude", "r1"
        widgets.window_close_controls()  # (as the app: every dialog's [x] makes room at its top as it is shown)
        dialog = credentials_page.GiveDialog(None, [Run()], secret="FORGE_TOKEN", git_hosts=["forge.example"])
        dialog.show()
        for _ in range(5):
            qapp.processEvents()
        shown = dialog.size()
        dialog.host.setText("forge.example")
        for _ in range(5):
            qapp.processEvents()
        note = next(label for label in dialog.kinds.findChildren(QLabel) if "Git" in label.text())
        assert note.isVisible() and note.height() >= note.heightForWidth(note.width()), "the whole note"
        assert dialog.size() == shown, "room for the choice from the start: the window need not grow"
        assert dialog.as_login.isVisible() and dialog.user.isVisible()
        dialog.close()
    finally:
        i18n.set_language("en")
        theme.apply("system")
        qapp.setFont(font)


# -- what an agent can set: times, folder sizes --

def test_a_file_dated_far_in_the_future_never_hides_its_folder(setup, wait):
    """A modification time past what localtime() takes (touch -d @4611686018427387904: tmpfs and btrfs keep it) made
    the listing's rows fail - the whole folder showed empty with "Value too large" - and the Properties dialog
    failed to open."""
    page, ws, host, _ = setup
    os.utime(ws / "a.txt", (2 ** 62, 2 ** 62))
    page.left.list()
    settle(page.left)
    assert names(page.left) == ["a.txt", "escape", "run.sh", "sub"], page.message.text()
    select(page.left, "a.txt")
    assert page.properties(page.left)
    assert wait(lambda: page.dialogs, 10)
    page.dialogs[-1].close()


def test_a_remote_count_reads_no_more_of_a_folder_than_its_budget(served_here, tmp_path):
    """A remote folder was read whole before its entries were counted: a folder of millions of entries was held in
    memory before "More than ..." (a local one stops reading at the budget)."""
    (served_here / "many").mkdir()
    for number in range(600):
        (served_here / "many" / f"f{number:03}").write_text("x")
    log = tmp_path / "requests.log"
    root = connect(served_here, FAKE_SFTP_LOG=str(log))
    try:
        with pytest.raises(files.Refused, match="More than"):
            root.delete_entry("many", entries=10)
        with pytest.raises(files.Refused, match="More than"):
            root.measure("many", entries=10)
        assert log.read_text().count("READDIR") <= 4, "the folder read on past the budget"
        assert len(os.listdir(served_here / "many")) == 600
    finally:
        root.close()


def test_an_upload_onto_a_remote_folder_of_its_name_says_so(served_here, tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "x").write_text("a file")
    (served_here / "x").mkdir()
    (served_here / "x" / "kept.txt").write_text("kept")
    root, local = connect(served_here), files.Root(host, sandbox=False)
    try:
        with pytest.raises(files.Refused, match="is a folder there"):
            files.copy_entry(local, "x", root, "", overwrite=True)
        assert (served_here / "x" / "kept.txt").read_text() == "kept"
        assert not [name for name in os.listdir(served_here) if name.endswith(".part")]
    finally:
        root.close()


def test_a_remote_file_in_a_folder_you_may_not_write_is_saved_in_place_after_a_question(served_here):
    """Locally such a file is offered to be written in place (NoTemporary); over SFTP the save failed with "Permission
    denied"."""
    (served_here / "f.txt").write_text("old")
    root = connect(served_here, FAKE_SFTP_NO_CREATE="1")
    try:
        read = root.read_file("f.txt")
        with pytest.raises(files.NoTemporary):
            root.write_file("f.txt", b"new", read.identity, mode=read.mode)
        assert (served_here / "f.txt").read_text() == "old"
        root.write_file("f.txt", b"new", read.identity, in_place=True)
        assert (served_here / "f.txt").read_text() == "new"
    finally:
        root.close()


def test_properties_apply_runs_once_at_a_time(tmp_path, qapp, wait, monkeypatch):
    """On a slow file system Apply went on in a thread and stayed enabled: a second Apply ran beside the first."""
    (tmp_path / "f.txt").write_text("x")
    os.chmod(tmp_path / "f.txt", 0o644)
    root = files.Root(tmp_path, sandbox=False)
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.05)
    calls, real = [], files.Root.chmod_entry

    def slow(self, relative, mode, special=False):
        calls.append(mode)
        time.sleep(0.4)
        return real(self, relative, mode, special)
    monkeypatch.setattr(files.Root, "chmod_entry", slow)
    from sandbox_ai_app.file_properties import PropertiesDialog
    dialog = PropertiesDialog(None, root, [("f.txt", "f.txt")], str(tmp_path), False)
    dialog.checks[5].setChecked(True)  # (the group may execute)
    dialog.apply()
    assert not dialog.apply_button.isEnabled()
    assert not dialog.apply()
    assert wait(lambda: stat.S_IMODE(os.stat(tmp_path / "f.txt").st_mode) == 0o654 and "changed" in
                dialog.message.text(), 10)
    assert len(calls) == 1
    dialog.close()


def test_the_known_hosts_file_is_found_off_the_gui_thread(setup, tmp_path, monkeypatch, wait):
    """Which known_hosts file a trusted key goes to was asked of `ssh -G` on the GUI thread, after the yes: a
    configuration with Match exec (or a slow ssh) held up the window."""
    from test_files_remote import failing
    page, ws, host, _ = setup
    known = tmp_path / "known_hosts"
    page.ssh_program = failing("Host key verification failed.",
                               f"grep -q fake '{known}' 2>/dev/null && exec {' '.join(FAKE)}", tmp_path)
    page.known_hosts = None
    monkeypatch.setenv("FAKE_SFTP_HOME", str(tmp_path))
    monkeypatch.setattr(remote, "scan_host", lambda target: ["fake ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA"])
    monkeypatch.setattr(remote, "fingerprints", lambda lines: ["ED25519 SHA256:abcdef"])
    asked_on = []
    monkeypatch.setattr(remote, "known_hosts_file", lambda target=None, config=None, ssh="ssh": asked_on.append(
        threading.current_thread() is threading.main_thread()) or str(known))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert page.connect_remote("fake")
    assert wait(lambda: known.exists(), 10), page.message.text()
    assert asked_on and not any(asked_on), "ssh -G ran on the GUI thread"
    assert known.read_text() == "fake ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA\n"
