"""Review pass 1 of the Files page's operations: each test failed before its fix. A move with a remote side that
deleted what it had not copied, a staging folder an agent could swap for a link to a host folder, an SFTP server that
follows links in its listings, Properties' Apply on several items, a paste that took the host's "/" for a remote "/",
the executable bits of copies inside a sandbox's folder, paths through a link in a sandbox's folder reached by
another path, a drop while a job runs, a cut pasted twice, a read-only file saved over, and folders compared by case
on macOS. Everything changed is below tmp_path."""
import os
from pathlib import Path
import stat
import sys
import unicodedata

import pytest

from sandbox_ai_app import files, remote

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]


def connect(path, **env):
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        return remote.RemoteRoot(remote.Target("fake"), str(path), ssh=FAKE, timeout=5)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def sides(tmp_path):
    served, host, ws, outside = (tmp_path / name for name in ("served", "host", "ws", "outside"))
    for path in (served, host, ws, outside):
        path.mkdir()
    (outside / "precious.txt").write_text("the host's own")
    return served, files.Root(host, sandbox=False), host, files.Root(ws), ws, outside


# -- moves with a remote side: only what was copied goes --

def test_a_move_to_a_remote_folder_keeps_what_it_could_not_copy(sides):
    """The source was deleted as a whole after the copy: a link in it (skipped, never copied) was lost."""
    served, host_root, host, ws_root, ws, outside = sides
    (ws / "proj").mkdir()
    (ws / "proj" / "a.txt").write_text("alpha")
    (ws / "proj" / "link").symlink_to(outside)
    root = connect(served)
    try:
        result = files.move_entry(ws_root, "proj", root, "")
        assert (served / "proj" / "a.txt").read_text() == "alpha"
        assert [path for path, _ in result.skipped] == ["proj/link"]
        assert not result.moved and result.kept, "something was not copied: the source stays"
        assert os.path.islink(ws / "proj" / "link") and (ws / "proj" / "a.txt").exists()
    finally:
        root.close()


def test_a_move_from_a_remote_folder_keeps_what_it_could_not_copy(sides):
    served, host_root, host, ws_root, ws, outside = sides
    (served / "docs").mkdir()
    (served / "docs" / "a.txt").write_text("alpha")
    (served / "docs" / "link").symlink_to(outside)
    root = connect(served)
    try:
        result = files.move_entry(root, "docs", host_root, "")
        assert (host / "docs" / "a.txt").read_text() == "alpha" and not result.moved and result.kept
        assert os.path.islink(served / "docs" / "link") and (served / "docs" / "a.txt").exists()
    finally:
        root.close()


def test_a_move_with_a_remote_side_removes_only_what_it_copied_unchanged(sides, monkeypatch):
    """What appeared in the source while it was copied stayed unknown to the copy - and was deleted with the rest."""
    served, host_root, host, ws_root, ws, outside = sides
    (ws / "proj").mkdir()
    (ws / "proj" / "a.txt").write_text("alpha")
    root = connect(served)
    real_put = remote.RemoteRoot.put

    def put(self, folder, name, *args, **kwargs):
        written = real_put(self, folder, name, *args, **kwargs)
        (ws / "proj" / "late.txt").write_text("written by the agent meanwhile")
        return written
    monkeypatch.setattr(remote.RemoteRoot, "put", put)
    try:
        result = files.move_entry(ws_root, "proj", root, "")
        assert (ws / "proj" / "late.txt").read_text() == "written by the agent meanwhile"
        assert not (ws / "proj" / "a.txt").exists(), "what was copied goes"
        assert not result.moved and result.left == ["proj/late.txt"]
    finally:
        root.close()
    monkeypatch.setattr(remote.RemoteRoot, "put", real_put)
    # And the other way: a remote file that changed after it was read stays.
    (served / "docs").mkdir()
    (served / "docs" / "a.txt").write_text("alpha")
    (served / "docs" / "b.txt").write_text("beta")
    root = connect(served)
    real_fetch = remote._fetch

    def fetch(source, path, info, dir_fd, name, shown, *args):
        real_fetch(source, path, info, dir_fd, name, shown, *args)
        if shown == "docs/a.txt":
            (served / "docs" / "a.txt").write_text("alpha, longer now")
    monkeypatch.setattr(remote, "_fetch", fetch)
    try:
        result = files.move_entry(root, "docs", host_root, "")
        assert (served / "docs" / "a.txt").read_text() == "alpha, longer now", "changed meanwhile: kept"
        assert not (served / "docs" / "b.txt").exists() and (host / "docs" / "b.txt").read_text() == "beta"
        assert not result.moved and result.kept
    finally:
        root.close()


def test_a_short_read_of_an_sftp_server_is_read_on_not_taken_for_the_end(sides, monkeypatch):
    """A READ answered with fewer bytes than asked ended the read: the copy was cut short, silently - and a move
    removed the whole source after it."""
    served, host_root, host, ws_root, ws, outside = sides
    data = os.urandom(200_000)
    (served / "big.bin").write_bytes(data)
    root = connect(served, FAKE_SFTP_SHORT_READS="1")
    try:
        result = files.move_entry(root, "big.bin", host_root, "")
        assert (host / "big.bin").read_bytes() == data and result.moved
    finally:
        root.close()


def test_the_staging_folder_swapped_for_a_link_never_copies_a_host_folder(sides, monkeypatch):
    """The staging folder in a sandbox's folder was opened again by its path (realpath): an agent that swapped it for
    a link to a host folder had that folder's files copied into its sandbox."""
    served, host_root, host, ws_root, ws, outside = sides
    (served / "precious").mkdir()
    (served / "precious" / "remote.txt").write_text("from the remote system")
    (outside / "precious").mkdir()
    (outside / "precious" / "secret.txt").write_text("a host secret")
    root = connect(served)
    real_fetch = remote._fetch

    def fetch(source, path, info, dir_fd, name, shown, *args):
        real_fetch(source, path, info, dir_fd, name, shown, *args)
        if shown == "precious":  # the agent: the staging folder away, a link to the host's folder in its place
            staging = next(p for p in ws.iterdir() if p.name.endswith(".part"))
            staging.rename(ws / "taken-away")
            staging.symlink_to(outside)
    monkeypatch.setattr(remote, "_fetch", fetch)
    try:
        with pytest.raises(files.Refused):
            files.copy_entry(root, "precious", ws_root, "")
        assert not (ws / "precious").exists(), "nothing of the host folder in the sandbox"
        assert (outside / "precious" / "secret.txt").read_text() == "a host secret"
    finally:
        root.close()


def test_an_sftp_server_that_follows_links_in_its_listing_never_has_them_followed(sides):
    """READDIR's attributes were trusted: a server that gives a link's target's (stat, not lstat) had the delete go
    through the link and empty the folder it points to, and copies and sizes follow it."""
    served, host_root, host, ws_root, ws, outside = sides
    (served / "docs").mkdir()
    (served / "docs" / "a.txt").write_text("alpha")
    (served / "docs" / "inner").symlink_to(outside)
    root = connect(served, FAKE_SFTP_FOLLOW_IN_LISTINGS="1")
    try:
        assert root.measure("docs").files == 2, "the link counted as itself"
        result = files.copy_entry(root, "docs", host_root, "")
        assert [path for path, _ in result.skipped] == ["docs/inner"] and not (host / "docs" / "inner").exists()
        try:
            removed = root.delete_entry("docs").removed
        except OSError:  # (before: the files it points to removed, then the link refused as a folder)
            removed = None
        assert (outside / "precious.txt").read_text() == "the host's own", "never deleted through the link"
        assert removed == 3 and not os.path.lexists(served / "docs"), "the link itself removed"
    finally:
        root.close()


def test_a_long_transfer_lets_other_requests_through_between_its_windows(sides):
    """The SFTP session's lock was held for a whole file: a request of the window (Properties, a rename) waited for
    the end of a copy of up to 1 GiB."""
    served, host_root, host, ws_root, ws, outside = sides
    (served / "big.bin").write_bytes(os.urandom(3 * remote.WINDOW * remote.CHUNK))
    root = connect(served)
    import threading
    answered, chunks, during = [], [], []
    try:
        handle = root.sftp.open(str(served / "big.bin"), remote.READ_FLAG)

        def sink(chunk):
            chunks.append(len(chunk))
            if len(chunks) == 1:  # (in the first window: the other thread asks meanwhile)
                threading.Thread(target=lambda: answered.append(root.sftp.lstat(str(served)).mode),
                                 daemon=True).start()
                import time
                time.sleep(0.2)  # (it waits for the lock now)
            elif answered and not during:
                during.append(len(chunks))
        root.sftp.read_file(handle, 3 * remote.WINDOW * remote.CHUNK, files.LIMIT, sink=sink)
        root.sftp.close_handle(handle)
    finally:
        root.close()
    assert answered and during and during[0] <= 2 * remote.WINDOW + 1, \
        "the other request was answered between two windows of the transfer, not after its end"


def test_an_alias_of_the_ssh_config_is_scanned_where_ssh_connects_and_added_as_ssh_looks_it_up(tmp_path):
    """ssh-keyscan reads no configuration: an unknown host given as an alias of ~/.ssh/config (HostName, Port) was
    looked up as a name in DNS on port 22, and its keys - added under the alias, to ~/.ssh/known_hosts - were never the
    ones ssh looks for there: trusting it never let the connection through."""
    ssh, keyscan = tmp_path / "ssh", tmp_path / "ssh-keyscan"
    ssh.write_text("#!/bin/sh\n[ \"$1\" = -G ] || exit 1\nprintf 'user me\\nhostname 10.1.2.3\\nport 2200\\n"
                   f"userknownhostsfile {tmp_path}/work_hosts ~/.ssh/known_hosts2\\n'\n")
    keyscan.write_text("#!/bin/sh\nhost=\"\"; port=22\nwhile [ $# -gt 0 ]; do case \"$1\" in -p) port=$2; shift;; "
                       "-T) shift;; --) host=$2; shift;; esac; shift; done\n"
                       "if [ \"$port\" = 22 ]; then echo \"$host ssh-ed25519 AAAA\"; "
                       "else echo \"[$host]:$port ssh-ed25519 AAAA\"; fi\n")
    for script in (ssh, keyscan):
        script.chmod(0o755)
    target = remote.Target("work")
    assert remote.scan_host(target, str(keyscan), ssh=str(ssh)) == ["[10.1.2.3]:2200 ssh-ed25519 AAAA"]
    assert remote.known_hosts_file(target, ssh=str(ssh)) == str(tmp_path / "work_hosts")
    # Without ssh's word on it (no ssh -G): as typed.
    assert remote.scan_host(target, str(keyscan), ssh=str(tmp_path / "missing")) == ["work ssh-ed25519 AAAA"]


def test_copies_and_moves_keep_the_modification_time_of_files(sides):
    """Every copy - and so every move between two roots or with a remote side - gave the files the time of the copy as
    their modification time: a move made all of them look changed just now (make, rsync and backups took them so)."""
    served, host_root, host, ws_root, ws, outside = sides
    when = 1_000_000_000  # 2001-09-09
    for folder, name in ((ws, "agent.txt"), (host, "mine.txt"), (served, "far.txt")):
        (folder / name).write_text(name)
        os.utime(folder / name, (when, when))
    files.copy_entry(ws_root, "agent.txt", host_root, "")
    files.move_entry(host_root, "mine.txt", ws_root, "")
    assert (host / "agent.txt").stat().st_mtime == when and (ws / "mine.txt").stat().st_mtime == when
    root = connect(served)
    try:
        files.move_entry(root, "far.txt", host_root, "")
        files.copy_entry(host_root, "agent.txt", root, "")
    finally:
        root.close()
    assert (host / "far.txt").stat().st_mtime == when and (served / "agent.txt").stat().st_mtime == when


# -- files.py --

def test_a_case_only_rename_on_a_linux_file_system_that_ignores_case(tmp_path, monkeypatch):
    """FAT and exFAT (a USB stick on Linux) ignore the case: renameat2(RENAME_NOREPLACE) of a.txt to A.txt finds A.txt
    "existing" - the file itself - and the rename was refused as "already exists" (a plain rename does nothing there).
    The file system is played here: a name exists when one of another case does."""
    import errno
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("alpha")
    (ws / "b.txt").write_text("beta")
    root = files.Root(ws)

    def found(dir_fd, name):
        return next((entry for entry in os.listdir(dir_fd) if entry.casefold() == name.casefold()), None)

    def renameat2(src, name, dst, new):
        if found(dst, new) is not None:
            return errno.EEXIST
        os.rename(name, new, src_dir_fd=src, dst_dir_fd=dst)
        return 0
    monkeypatch.setattr(files, "_renameat2", renameat2)
    monkeypatch.setattr(files, "_same_name_but_case", lambda src, name, dst, new: name.casefold() == new.casefold()
                        and found(dst, new) == name)
    root.rename_entry("a.txt", "A.txt")
    assert sorted(os.listdir(ws)) == ["A.txt", "b.txt"] and (ws / "A.txt").read_text() == "alpha"
    with pytest.raises(files.Refused, match="already exists"):
        root.rename_entry("A.txt", "B.TXT")
    assert sorted(os.listdir(ws)) == ["A.txt", "b.txt"]


def test_saving_never_replaces_a_file_you_may_not_write(tmp_path, monkeypatch):
    """write_file renamed a new file over one you may not write (read-only, another user's): the editor's Save went
    past a read-only file and gave it to you."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ro.txt").write_text("keep me")
    (ws / "ro.txt").chmod(0o444)
    root = files.Root(ws)
    if os.getuid() == 0:  # (root may write every file: here as a user would be answered)
        real_access = os.access

        def access(path, mode, **kwargs):
            info = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
            return real_access(path, mode, **kwargs) and not (mode & os.W_OK and not info.st_mode & 0o200)
        monkeypatch.setattr(files.os, "access", access)
    with pytest.raises(files.Refused, match="read-only|may not write"):
        root.write_file("ro.txt", b"replaced", expected=root.read_file("ro.txt").identity)
    assert (ws / "ro.txt").read_text() == "keep me"
    assert not [p for p in ws.iterdir() if p.name.endswith(".part")]


def test_a_save_is_on_the_disk_before_it_takes_the_files_name(tmp_path, monkeypatch):
    """write_file renamed the new file over the old one without an fsync: after a crash or a power cut soon after a
    save, some file systems had an empty file under the name - the old text and the new both gone."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("old")
    root = files.Root(ws)
    synced, real_fsync, real_replace = [], os.fsync, os.replace
    monkeypatch.setattr(files.os, "fsync", lambda fd: synced.append(os.fstat(fd).st_size) or real_fsync(fd))
    monkeypatch.setattr(files.os, "replace", lambda *a, **k: synced.append("renamed") or real_replace(*a, **k))
    root.write_file("notes.txt", b"the new text", expected=root.read_file("notes.txt").identity)
    assert synced == [len(b"the new text"), "renamed"] and (ws / "notes.txt").read_text() == "the new text"


def test_a_remote_delete_is_counted_first_and_refused_before_anything_goes(sides):
    """Locally a folder too big (or nested too deep) is refused before anything is deleted; on a remote system the
    delete went on until the budget ran out - part of the folder deleted, the rest refused."""
    served, host_root, host, ws_root, ws, outside = sides
    (served / "many").mkdir()
    for number in range(6):
        (served / "many" / f"f{number}").write_text("x")
    root = connect(served)
    try:
        with pytest.raises(files.Refused, match="More than"):
            root.delete_entry("many", entries=4)
        assert len(os.listdir(served / "many")) == 6, "nothing deleted"
        assert root.delete_entry("many").removed == 7
    finally:
        root.close()


def test_without_renameat2_renameatx_np_refuses_an_existing_name_in_one_step(tmp_path, monkeypatch):
    """macOS has no renameat2: the new name was looked at first, then renamed - a name made in between was replaced.
    renameatx_np(RENAME_EXCL) refuses it in one step (played here by a libc with that function only)."""
    import ctypes
    import errno
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("alpha")
    (ws / "b.txt").write_text("beta")
    calls = []

    class Libc:
        @staticmethod
        def renameatx_np(src, name, dst, new, flags):
            calls.append(flags)
            if os.path.lexists(os.path.join(f"/proc/self/fd/{dst}", os.fsdecode(new))):
                ctypes.set_errno(errno.EEXIST)
                return -1
            os.rename(os.fsdecode(name), os.fsdecode(new), src_dir_fd=src, dst_dir_fd=dst)
            return 0
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: Libc())
    root = files.Root(ws)
    with pytest.raises(files.Refused, match="already exists"):
        root.rename_entry("a.txt", "b.txt")
    root.rename_entry("a.txt", "c.txt")
    assert calls and set(calls) == {4} and sorted(os.listdir(ws)) == ["b.txt", "c.txt"]


def test_a_file_in_a_folder_you_may_not_write_is_saved_in_place_after_a_question(tmp_path, monkeypatch):
    """A save writes a temporary file beside the file and renames it: in a folder you may not write (a file of yours in
    a folder of root's) nothing could be saved. Asked first, the file is written in place (and synced)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "conf.txt").write_text("old and long")
    root = files.Root(ws)
    real_create = files._create

    def create(name, dir_fd, mode):
        if name.startswith(".sandbox-ai-app-"):
            raise PermissionError(13, "Permission denied", name)
        return real_create(name, dir_fd, mode)
    monkeypatch.setattr(files, "_create", create)
    identity = root.read_file("conf.txt").identity
    with pytest.raises(files.NoTemporary):
        root.write_file("conf.txt", b"new", expected=identity)
    assert (ws / "conf.txt").read_text() == "old and long"
    done = root.write_file("conf.txt", b"new", expected=identity, in_place=True)
    assert (ws / "conf.txt").read_bytes() == b"new" and done == files._identity(os.stat(ws / "conf.txt"))
    with pytest.raises(files.Changed):
        root.write_file("conf.txt", b"again", expected=identity, in_place=True)
    os.link(ws / "conf.txt", ws / "other-name")
    with pytest.raises(files.Refused, match="other names"):
        root.write_file("conf.txt", b"through a link", expected=done, in_place=True)
    assert (ws / "conf.txt").read_bytes() == b"new"


# -- the page --

def test_the_editor_asks_before_it_saves_in_place(setup, wait, monkeypatch):
    from test_files_manager import editor_of
    page, ws, host, _ = setup
    real_create = files._create
    monkeypatch.setattr(files, "_create", lambda name, dir_fd, mode: (_ for _ in ()).throw(
        PermissionError(13, "Permission denied", name)) if name.startswith(".sandbox-ai-app-") else
        real_create(name, dir_fd, mode))
    page.left.open_row(widgets.find_row(page.left.table, "a.txt"))
    window = editor_of(page, wait)
    window.text.selectAll()
    window.text.insertPlainText("written in place")
    widgets.SHOWN.clear()
    assert window.save() and wait(lambda: not window.saving, 10)
    assert [title for title, *_ in widgets.SHOWN] == ["Write the file in place?"]
    assert (ws / "a.txt").read_text() == "written in place" and not window.dirty
    window.close()


def test_a_typed_mode_for_files_and_folders_at_once_keeps_the_folders_enterable(setup, wait):
    """Permissions… applied the mode typed to every item: 644 for a file and a folder made the folder 0644, which
    nobody could enter. For files and folders at once a folder also gets the execute (search) bit wherever it may be
    read (chmod's X); one folder alone gets what was typed."""
    page, ws, host, _ = setup
    page.left.list()
    select(page.left, "a.txt", "sub")
    assert page.chmod(page.left, page.left.chosen(), 0o640)
    assert stat.S_IMODE((ws / "a.txt").stat().st_mode) == 0o640
    assert stat.S_IMODE((ws / "sub").stat().st_mode) == 0o750
    assert any("execute" in line for line in page.last_report)
    select(page.left, "sub")
    assert page.chmod(page.left, page.left.chosen(), 0o700)
    assert stat.S_IMODE((ws / "sub").stat().st_mode) == 0o700


def test_a_step_on_a_local_file_system_that_hangs_holds_up_the_window_no_longer_than_a_moment(setup, wait, monkeypatch):
    """root_job and on_root ran a local root's steps on the GUI thread: a rename on an NFS or FUSE mount that hangs
    held up the whole window. A step not done within a moment goes on in a thread; its result comes when it ends."""
    import threading
    import time
    page, ws, host, _ = setup
    release = threading.Event()
    real = files.Root.rename_entry

    def hanging(self, relative, new_name):
        release.wait(20)
        return real(self, relative, new_name)
    monkeypatch.setattr(files.Root, "rename_entry", hanging)
    started = time.monotonic()
    assert page.rename(page.left, page.left.by_name["a.txt"], "b.txt")
    assert time.monotonic() - started < 3, "the window was held up"
    assert not (ws / "b.txt").exists()
    release.set()
    assert wait(lambda: (ws / "b.txt").exists() and "renamed" in page.message.text(), 10)

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtGui import QGuiApplication  # noqa: E402

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import finish, names, select, settle, setup  # noqa: E402,F401


def test_properties_apply_changes_only_the_bits_changed_on_each_item(setup, wait):
    """Apply gave every item the first one's whole mode: a group-write tick on a file and a folder made the folder
    0664 - nobody could enter it any more."""
    page, ws, host, _ = setup
    (ws / "a.txt").chmod(0o644)
    (ws / "sub").chmod(0o755)
    page.left.list()
    select(page.left, "a.txt", "sub")
    dialog = page.properties(page.left)
    dialog.checks[4].setChecked(True)  # the group's write bit
    assert dialog.apply()
    assert stat.S_IMODE((ws / "a.txt").stat().st_mode) == 0o664
    assert stat.S_IMODE((ws / "sub").stat().st_mode) == 0o775, "the folder keeps its own execute bits"
    dialog.checks[4].setChecked(False)
    assert dialog.apply()
    assert stat.S_IMODE((ws / "a.txt").stat().st_mode) == 0o644
    assert stat.S_IMODE((ws / "sub").stat().st_mode) == 0o755
    dialog.close()


def test_a_copy_inside_a_sandboxs_folder_keeps_its_executable_bits(setup, wait):
    """A copy (or a move that merges) from a sandbox's folder into a sandbox's folder dropped every executable bit,
    and said nothing: nothing left the agent's folders, so nothing needed to change."""
    page, ws, host, _ = setup
    page.left.go("sub")
    settle(page.left)
    page.right.go_path(str(ws))
    settle(page.right)
    select(page.right, "run.sh")
    assert page.copy_selected(page.right)
    finish(page, wait)
    assert os.access(ws / "sub" / "run.sh", os.X_OK), page.message.text()


def test_a_paste_into_a_remote_folder_of_the_same_path_is_no_paste_beside_the_original(setup, wait):
    """The host's Root("/") and a remote system's root ("/" too) were taken for one: a cut pasted into the remote
    folder of the same path was refused ("in this folder already"), a copy got a "(copy)" name."""
    page, ws, host, _ = setup

    class Remote:
        remote, sandbox, path = True, False, "/"
    pane = page.left
    pane.root, pane.relative = Remote(), host_relative(str(host))
    clip = files_page.Clip(page.right.root, [("a.txt", files_page.host_relative(str(host / "a.txt")), None)], False,
                           page.right.path_at(files_page.host_relative(str(host))), cut=True)
    started = []
    page.transfer_items = lambda clip, target, target_dir, move=False: started.append((clip, move)) or True
    assert page.paste_clip(clip, pane, pane.relative)
    assert started and started[0][1] and started[0][0].items[0][2] is None, "moved under its own name"
    clip.cut = False
    started.clear()
    assert page.paste_clip(clip, pane, pane.relative)
    assert started[0][0].items[0][2] is None, "copied under its own name, no (copy)"


def host_relative(path):
    return files_page.host_relative(path)


def test_a_path_through_a_link_in_a_sandboxs_folder_is_refused_whatever_path_leads_there(setup, tmp_path):
    """Only the path typed was compared with the sandboxes' folders: through another path to the same folder (a link
    to it), a link of the agent's in it was followed - the host pane showed the folder the agent chose."""
    page, ws, host, outside = setup
    alias = tmp_path / "alias"
    alias.symlink_to(ws)
    page.right.go_path(str(alias / "escape"))
    settle(page.right)
    assert page.right.path_text() != str(outside) and "not followed" in page.message.text()
    clip, problems = page.host_items([str(alias / "escape" / "precious.txt")])
    assert not clip.items and problems, "dropped or pasted from another program: refused as well"


def test_a_drop_while_a_job_runs_asks_nothing_and_says_why(setup, wait, monkeypatch):
    """A drop is no key and no button: it asked whether to replace files (over SFTP: in the window's thread), then
    said that another copy still runs."""
    page, ws, host, _ = setup
    (host / "a.txt").write_text("the host's")
    page.replace.setChecked(True)
    monkeypatch.setattr(page.task, "call", object())  # a job runs
    clip = files_page.Clip(page.left.root, [("a.txt", "a.txt", None)], True, str(ws))
    assert not page.transfer_items(clip, page.right, page.right.relative)
    assert widgets.SHOWN == [] and "Wait until" in page.message.text()


def test_a_cut_pasted_once_is_not_pasted_again_from_the_system_clipboard(setup, wait):
    """After a cut was pasted (moved), the system clipboard still named the files where they had been: a second paste
    copied from there (the files gone: an error; a move that kept them: copied once more)."""
    page, ws, host, _ = setup
    select(page.left, "a.txt")
    assert page.copy_items(page.left, cut=True)
    assert page.paste(page.right)
    finish(page, wait)
    assert (host / "a.txt").exists() and not (ws / "a.txt").exists()
    assert not page.paste(page.right) and "Nothing to paste" in page.message.text()
    mime = QGuiApplication.clipboard().mimeData()
    assert mime is None or not mime.hasFormat(files_page.MIME)


def test_folders_of_sandboxes_are_found_whatever_the_case_and_the_normalisation_on_macos(setup, monkeypatch, tmp_path):
    """macOS' file systems ignore case (and APFS the Unicode normalisation): /users/me/ws is /Users/me/ws there, but
    the comparison with the sandboxes' folders was by the letters - the host pane treated a sandbox's folder reached
    so as the host's own (links followed, executable bits kept)."""
    page, ws, host, _ = setup
    folder = tmp_path / unicodedata.normalize("NFC", "Arbeit-ä")
    folder.mkdir()
    page.others.append(("other", "", str(folder)))
    monkeypatch.setattr(files_page, "FOLDED_PATHS", True)
    assert page.below_sandbox(str(ws).upper() + "/sub")
    assert page.below_sandbox(unicodedata.normalize("NFD", str(folder)) + "/x")
    monkeypatch.setattr(files_page, "FOLDED_PATHS", False)
    assert not page.below_sandbox(str(ws).upper() + "/sub"), "elsewhere: by the letters, as the file system"


def test_a_path_with_control_characters_is_quoted_for_the_shell_so_that_it_names_the_file(setup):
    """"Copy quoted for the shell" quoted the path as shown (control characters escaped): pasted into a shell, a name
    with a tab or a line break named another file. Now quoted as $'...' with those characters as \\x escapes - exact,
    and still no raw control character on the clipboard."""
    import shutil
    import subprocess
    page, ws, host, _ = setup
    (ws / "tab\there.txt").write_text("x")
    (ws / "new\nline.txt").write_text("y")
    page.left.list()
    select(page.left, "tab\there.txt", "new\nline.txt")
    text = page.copy_path(page.left, quoted=True)
    assert not any(ord(c) < 32 and c != "\n" for c in text) and len(text.split("\n")) == 2
    bash = shutil.which("bash")
    if bash:
        for line in text.split("\n"):
            assert subprocess.run([bash, "-c", f"test -f {line}"]).returncode == 0, line
    assert page.copy_path(page.left, [page.left.by_name["tab\there.txt"]], quoted=True).startswith("$'")
    plain = page.copy_path(page.left, [page.left.by_name["tab\there.txt"]])
    assert "\\x09" in plain and "\t" not in plain, "unquoted: shown escaped, as before"


def test_executable_bits_dropped_are_said_wherever_the_copy_went(setup):
    """What comes from a remote system into a sandbox's folder loses its executable bits (it went through a staging
    folder there) - and the report said so only for copies out of a sandbox's folder: the bits vanished unsaid."""
    page, ws, host, _ = setup
    lines = page.report([files.Result(files=1, executable=["tool.sh"])], [], copied_out=False)
    assert any(line.startswith("Copied without the executable bit") and "tool.sh" in line for line in lines)


def test_editors_of_a_remote_system_connected_again_save_through_the_new_connection(setup, tmp_path, monkeypatch,
                                                                                    wait):
    """Connected again (SSH… after the connection ended), the old connection was closed - and an editor still open
    on one of its files could save nothing ("the connection ended"): what was typed in it was lost."""
    from test_files_manager import editor_of
    page, ws, host, _ = setup
    home = tmp_path / "remote"
    home.mkdir()
    (home / "notes.txt").write_text("old")
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    page.ssh_program = FAKE
    assert page.connect_remote("alice@fake")
    assert wait(lambda: page.left.remote() and page.left.listing is None, 10), page.message.text()
    old = page.left.root
    page.left.go_path(str(home.resolve()))
    settle(page.left)
    page.left.open_row(widgets.find_row(page.left.table, "notes.txt"))
    window = editor_of(page, wait)
    window.text.selectAll()
    window.text.insertPlainText("typed before the connection ended")
    old.sftp.process.kill()
    old.sftp.process.wait()
    assert page.connect_remote("alice@fake")
    assert wait(lambda: page.left.root is not old and page.left.root.alive, 10)
    assert window.save() and wait(lambda: not window.saving, 10)
    assert (home / "notes.txt").read_text() == "typed before the connection ended", window.notice.text()
    window.close()
    for root in page.remotes.values():
        root.close()
