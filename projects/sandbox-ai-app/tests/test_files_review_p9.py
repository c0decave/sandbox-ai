"""Review pass 9 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A sandbox's folder listed in the host pane (~/projects/ws seen from ~/projects) got set-ID and sticky bits from
Permissions and from Properties - only a folder shown inside it was the agent's. A file dragged out of a sandbox's
folder (or put on the clipboard) went to the other program by its path in the agent's folder, which the agent could
turn into a link to a file of the human's before that program read it. A remote file rewritten with as many bytes
within the same second (and its bits unchanged) looked unchanged to a save, which replaced it unasked. Everything
changed is below tmp_path."""
import os
import stat

import pytest

from sandbox_ai_app import files, remote

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import names, select, settle, setup  # noqa: E402,F401
from test_files_review_p1 import connect  # noqa: E402


def agents_folder_in_host_pane(page, tmp_path):
    right = page.right
    assert right.go_path(str(tmp_path))
    settle(right)
    assert not right.untrusted() and "ws" in names(right)
    return right


# -- set-ID and sticky bits on a sandbox's folder seen from the folder that holds it --

def test_permissions_never_set_special_bits_on_a_sandboxs_folder_seen_from_its_parent(setup, tmp_path):
    """The host pane at the folder that holds the workspace: Permissions… 2755 on ws made it set-group-ID (every file the
    agent makes there then takes its group), 1777 made it sticky. Inside ws the same was refused."""
    page, ws, _host, _ = setup
    right = agents_folder_in_host_pane(page, tmp_path)
    before = stat.S_IMODE(ws.stat().st_mode)
    for mode in (0o2755, 0o4755, 0o1777):
        page.chmod(right, [right.by_name["ws"]], mode)
        assert not ws.stat().st_mode & files.SPECIAL_BITS, oct(mode)
        assert "set-group-ID" in "\n".join(page.last_report) or "Set-user-ID" in "\n".join(page.last_report)
    assert stat.S_IMODE(ws.stat().st_mode) == before
    # The human's own folder beside it still may get them (the host's own: as before).
    (tmp_path / "mine").mkdir()
    right.list()
    settle(right)
    page.chmod(right, [right.by_name["mine"]], 0o2755)
    assert (tmp_path / "mine").stat().st_mode & stat.S_ISGID


def test_properties_offer_no_special_bits_for_a_sandboxs_folder_seen_from_its_parent(setup, tmp_path, wait):
    page, ws, _host, _ = setup
    right = agents_folder_in_host_pane(page, tmp_path)
    assert page.properties(right, [right.by_name["ws"]])
    assert wait(lambda: page.dialogs, 10)
    dialog = page.dialogs[-1]
    try:
        assert all(not check.isEnabled() for _bit, check in dialog.specials)
        for bit, check in dialog.specials:
            if bit == stat.S_ISGID:
                check.setEnabled(True)
                check.setChecked(True)  # (forced: Apply refuses it all the same)
        dialog.apply()
        assert wait(lambda: not dialog.applying, 10)
        assert not ws.stat().st_mode & files.SPECIAL_BITS
    finally:
        dialog.close()


def test_chmod_entry_refuses_special_bits_on_an_agents_folder_by_its_identity(tmp_path):
    """files.chmod_entry of the host's Root("/"): a folder whose (st_dev, st_ino) is a sandbox's is the agent's, under
    whatever name it is reached (a bind mount)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    info = ws.stat()
    host = files.Root("/", sandbox=False)
    with pytest.raises(files.Refused, match="sticky"):
        host.chmod_entry(str(ws).lstrip("/"), 0o2755, special=True, agents={(info.st_dev, info.st_ino)})
    assert not ws.stat().st_mode & files.SPECIAL_BITS
    host.chmod_entry(str(ws).lstrip("/"), 0o750, special=True, agents={(info.st_dev, info.st_ino)})
    assert stat.S_IMODE(ws.stat().st_mode) == 0o750


# -- files handed to other programs out of a sandbox's folder --

def test_a_file_dragged_out_of_a_sandboxs_folder_goes_as_a_private_copy(setup, wait):
    """The URL handed to another program named the file in the agent's folder: the agent could swap it for a link to
    ~/.ssh/id_ed25519 (or make it executable) between the drop and the other program's read - a mail program attached
    the key. Now the other program gets a copy of the file as it was when it was dragged, in a folder of the app's own,
    never executable."""
    page, ws, _host, outside = setup
    select(page.left, "a.txt")
    urls = page.export_urls(page.left, page.left.chosen())
    assert len(urls) == 1
    given = urls[0].toLocalFile()
    assert not given.startswith(str(ws) + "/"), given
    assert os.path.basename(given) == "a.txt"
    os.unlink(ws / "a.txt")
    os.symlink(outside / "precious.txt", ws / "a.txt")  # (the agent, after the drop)
    info = os.lstat(given)
    assert stat.S_ISREG(info.st_mode) and not info.st_mode & 0o111 and open(given).read() == "alpha"
    folder = os.stat(os.path.dirname(given))
    assert not stat.S_IMODE(folder.st_mode) & 0o077, "a folder only you may read"
    # The clipboard too.
    (ws / "b.txt").write_text("beta")
    page.left.list()
    settle(page.left)
    select(page.left, "b.txt")
    assert page.copy_items(page.left)
    # (the copy is made in a thread, the clipboard gets it once it is done: pass 12)
    assert wait(lambda: QtWidgets.QApplication.clipboard().mimeData().urls(), 10)
    urls = QtWidgets.QApplication.clipboard().mimeData().urls()
    assert [os.path.basename(url.toLocalFile()) for url in urls] == ["b.txt"]
    assert not urls[0].toLocalFile().startswith(str(ws) + "/")


def test_a_file_too_big_to_copy_aside_is_not_handed_out(setup, monkeypatch):
    page, ws, _host, _ = setup
    monkeypatch.setattr(files_page, "EXPORT_LIMIT", 3)
    select(page.left, "a.txt")
    assert page.export_urls(page.left, page.left.chosen()) == []


# -- a remote file rewritten with as many bytes in the same second --

def same_attributes(path, text):
    """PATH rewritten with TEXT (as many bytes), its modification time and bits as they were: what an SFTP server tells
    of it (size, whole seconds, mode) is unchanged."""
    before = os.stat(path)
    path.write_text(text)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.chmod(path, stat.S_IMODE(before.st_mode))


@pytest.mark.parametrize("in_place", [False, True], ids=["renamed over", "in place"])
def test_a_remote_file_rewritten_alike_is_not_replaced_unasked(tmp_path, in_place):
    served = tmp_path / "srv"
    served.mkdir()
    (served / "notes.txt").write_text("old one")
    root = connect(served)
    try:
        read = root.read_file("notes.txt")
        same_attributes(served / "notes.txt", "THEIRS!")
        with pytest.raises(files.Changed):
            root.write_file("notes.txt", b"mine", read.identity, in_place=in_place)
        assert (served / "notes.txt").read_text() == "THEIRS!"
        assert sorted(os.listdir(served)) == ["notes.txt"], "no temporary file left"
        # Unchanged since it was read: saved, and that save's identity finds the next change too.
        read = root.read_file("notes.txt")
        saved = root.write_file("notes.txt", b"my text", read.identity, in_place=in_place)
        assert (served / "notes.txt").read_text() == "my text"
        same_attributes(served / "notes.txt", "MY TEXT")
        with pytest.raises(files.Changed):
            root.write_file("notes.txt", b"mine again", saved, in_place=in_place)
        assert (served / "notes.txt").read_text() == "MY TEXT"
    finally:
        root.close()


def test_a_remote_file_rewritten_alike_while_it_is_moved_stays(tmp_path, monkeypatch):
    """A move from a remote system removes the files it fetched unchanged: one rewritten with as many bytes within the
    same second after it was read looked unchanged, and was removed - the other program's write lost (the copy holds the
    old one)."""
    served, host = tmp_path / "srv", tmp_path / "host"
    served.mkdir()
    host.mkdir()
    (served / "notes.txt").write_text("old one")
    real = files.copy_entry

    def then_rewritten(*args, **kwargs):
        done = real(*args, **kwargs)
        same_attributes(served / "notes.txt", "THEIRS!")
        return done
    root = connect(served)
    try:
        monkeypatch.setattr(files, "copy_entry", then_rewritten)
        done = files.move_entry(root, "notes.txt", files.Root(host, sandbox=False), "")
    finally:
        monkeypatch.undo()
        root.close()
    assert (served / "notes.txt").read_text() == "THEIRS!", "kept: it changed after it was read"
    assert not done.moved and done.left == ["notes.txt"] and "notes.txt" in done.kept
    assert (host / "notes.txt").read_text() == "old one"


# -- a save that takes long --

def test_a_save_that_takes_long_is_not_said_to_have_failed(tmp_path, qapp, wait, monkeypatch):
    """A save of a big file to a slow remote system (16 MiB at 1 Mbit/s: over two minutes) said "Not saved: No answer
    within 120 s" and let Save and Reload go again - while it went on and replaced the file: the human discarded changes
    that were saved, or saved again over them. Now it is "saving" until it ends, however long that takes (a stalled
    server ends its requests by itself; closing the window says it is still saving)."""
    import threading
    from sandbox_ai_app import file_editor
    from sandbox_ai_app.file_editor import EditorWindow
    monkeypatch.setattr(widgets, "SHOWN", [])
    real = file_editor.background

    def hurried(*args, timeout=None, **kwargs):  # (every time limit as if it had passed already)
        return real(*args, timeout=None if timeout is None else 0.05, **kwargs)
    (tmp_path / "a.txt").write_text("old")
    go = threading.Event()

    class Slow(files.Root):
        def write_file(self, *args, **kwargs):
            go.wait(10)
            return super().write_file(*args, **kwargs)
    window = EditorWindow(None, Slow(tmp_path), "a.txt", str(tmp_path / "a.txt"))
    window.show()
    try:
        assert wait(lambda: not window.loading, 10)
        monkeypatch.setattr(file_editor, "background", hurried)
        window.text.setPlainText("new")
        assert window.save()
        wait(lambda: False, 0.5)
        assert window.saving and "Not saved" not in window.notice.text(), window.notice.text()
        go.set()
        assert wait(lambda: not window.saving, 10)
        assert (tmp_path / "a.txt").read_text() == "new" and not window.dirty and "Saved" in window.notice.text()
    finally:
        go.set()
        window.dropped = True
        window.close()
        window.deleteLater()


def test_permissions_that_take_long_on_a_remote_system_are_not_said_to_have_failed(tmp_path, qapp, wait, monkeypatch):
    """Properties' Apply on many items of a remote system far away (a request each): after 120 s it said "No answer"
    and gave Apply back while the changes went on - a second Apply ran beside the first."""
    import threading
    from sandbox_ai_app.file_properties import PropertiesDialog
    served = tmp_path / "srv"
    served.mkdir()
    (served / "a.txt").write_text("a")
    (served / "a.txt").chmod(0o644)
    real = files_page.background

    def hurried(*args, timeout=None, **kwargs):
        return real(*args, timeout=None if timeout is None else 0.05, **kwargs)
    root = connect(served)
    go = threading.Event()
    chmod = root.sftp.chmod

    def slow(*args, **kwargs):
        go.wait(10)
        return chmod(*args, **kwargs)
    root.sftp.chmod = slow
    items = [("a.txt", "a.txt")]
    dialog = PropertiesDialog(None, root, items, "/srv", False, infos=files.entry_infos(root, items))
    try:
        monkeypatch.setattr(files_page, "background", hurried)
        dialog.checks[4].setChecked(True)  # (the group's write)
        assert dialog.apply() and dialog.applying
        wait(lambda: False, 0.5)
        assert dialog.applying and "No answer" not in dialog.message.text(), dialog.message.text()
        go.set()
        assert wait(lambda: not dialog.applying, 10)
        assert stat.S_IMODE((served / "a.txt").stat().st_mode) == 0o664
    finally:
        go.set()
        dialog.close()
        root.close()


def test_a_slow_connection_is_not_given_up_while_it_connects(setup, tmp_path, monkeypatch, wait):
    """SSH… waited 120 s for the connection as a whole, while its steps have time limits of their own (ssh's
    ConnectTimeout, 30 s a request, 10 s for `ssh -G` of each of sandbox-ai's hosts): a connection that took longer said
    "No answer" - and once it was made, nobody took it: its ssh ran on unused until the app quit."""
    import time
    from test_files_review_p1 import FAKE
    page, _ws, _host, _ = setup
    home = tmp_path / "remote"
    home.mkdir()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    page.ssh_program = FAKE
    made, real_root, real_endpoint = [], remote.RemoteRoot, remote.endpoint_of

    def recorded(*args, **kwargs):
        made.append(real_root(*args, **kwargs))
        return made[-1]

    def slow_endpoint(*args, **kwargs):
        time.sleep(0.3)
        return real_endpoint(*args, **kwargs)
    real = files_page.background

    def hurried(*args, timeout=None, **kwargs):
        return real(*args, timeout=None if timeout is None else 0.05, **kwargs)
    monkeypatch.setattr(remote, "RemoteRoot", recorded)
    monkeypatch.setattr(remote, "endpoint_of", slow_endpoint)
    monkeypatch.setattr(files_page, "background", hurried)
    try:
        assert page.connect_remote("alice@fake")
        assert wait(lambda: made and (page.remotes or "No answer" in page.message.text()), 10)
        wait(lambda: False, 0.5)
        assert page.remotes and "No answer" not in page.message.text(), page.message.text()
    finally:
        for root in made:
            root.close()
