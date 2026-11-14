"""Review pass 10 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
Files copied from one of sandbox-ai's own hosts (where sandboxes run and agents write) to this machine kept their
executable bits and went unnamed; a sandbox's folder moved within this machine became the host's own; a sandbox's folder
on a file system that hangs held up the whole window (its folder opened, its identity and path read on the GUI thread);
a remote HOME with ".." in it broke the pane; closing Properties did not stop an Apply; a file the agent turned into a
folder while it was copied aside was handed out as a folder. Everything changed is below tmp_path."""
import os
import stat
import threading

import pytest

from sandbox_ai_app import files, remote

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401
from test_files_review_p1 import FAKE, connect  # noqa: E402


# -- files of a host of sandbox-ai's, copied to this machine --

def connected(page, wait, entries):
    page.sai.host_entries = lambda: entries
    page.ssh_program = FAKE
    assert page.connect_remote("alice@fake")
    assert wait(lambda: page.remotes and page.left.remote() and page.left.listing is None, 10), page.message.text()
    settle(page.left)
    return page.left.root


@pytest.fixture
def lab(setup, tmp_path, monkeypatch, wait):
    """The page connected to the fake remote system as one of sandbox-ai's hosts (alice@fake), its HOME tmp_path/lab with
    an executable and an .envrc the agents there may have written."""
    page, ws, host, outside = setup
    home = tmp_path / "lab"
    home.mkdir()
    (home / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (home / "run.sh").chmod(0o755)
    (home / "tool.sh").write_text("#!/bin/sh\n")
    (home / "tool.sh").chmod(0o755)
    (home / ".envrc").write_text("export X=1\n")
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    root = connected(page, wait, [{"name": "lab", "kind": "ssh", "ssh": "alice@fake"}])
    yield page, root, home.resolve(), host
    for each in page.remotes.values():
        each.close()


def test_files_copied_from_a_sandbox_ai_host_are_the_agents_on_this_machine(lab, wait):
    """Sandboxes run on sandbox-ai's hosts and their agents may have written any file there: copied to this machine
    (into a folder of the human's), a program kept its executable bit and an .envrc went unnamed - as if the human had
    written them. Now they come as an agent's files do, unless the human says otherwise for that connection."""
    page, root, home, host = lab
    left = page.left
    assert root.agents and left.untrusted(), "a host of sandbox-ai's: its files count as an agent's"
    assert not left.agents_box.isHidden() and left.agents_box.isChecked()
    select(left, "run.sh", ".envrc")
    assert page.copy_selected(left)
    report = "\n".join(finish(page, wait))
    assert not os.stat(host / "run.sh").st_mode & 0o111, "never executable on this machine"
    assert "Copied without the executable bit" in report and "run.sh" in report
    assert "Careful" in report and ".envrc" in report
    assert any(title == "Files that run or configure tools" for title, *_ in widgets.SHOWN)
    # A move too (the copy it makes; the source goes).
    select(left, "tool.sh")
    assert page.copy_selected(left, move=True)
    finish(page, wait)
    assert not os.stat(host / "tool.sh").st_mode & 0o111 and not (home / "tool.sh").exists()
    # The human's explicit choice: not an agent's (asked first) - copies keep their bits then.
    left.agents_box.setChecked(False)
    assert not root.agents and not left.untrusted()
    assert any(title == "Not an agent's files?" for title, *_ in widgets.SHOWN)
    page.right.go_path(str(host / ".."))
    settle(page.right)
    (host / ".." / "mine").mkdir()
    assert page.right.go_path(str(host / ".." / "mine"))
    settle(page.right)
    select(left, "run.sh")
    assert page.copy_selected(left)
    finish(page, wait)
    assert os.stat(host / ".." / "mine" / "run.sh").st_mode & 0o111


def test_permissions_on_a_sandbox_hosts_files_keep_the_special_bits_they_have(lab, wait):
    """There, as in a sandbox's folder here: no new set-ID or sticky bit - and one a file or folder has already stays
    (Permissions… and Properties sent the bits without them: a shared set-group-ID folder lost it at a tick of the
    group's write)."""
    page, root, home, _host = lab
    (home / "shared").mkdir()
    (home / "shared").chmod(0o2755)
    page.left.list()
    settle(page.left)
    page.chmod(page.left, [page.left.by_name["shared"]], 0o2775)
    assert wait(lambda: not page.task.running, 10)
    assert stat.S_IMODE((home / "shared").stat().st_mode) == 0o2775, oct((home / "shared").stat().st_mode)
    page.chmod(page.left, [page.left.by_name["run.sh"]], 0o4755)
    assert wait(lambda: not page.task.running, 10)
    assert not (home / "run.sh").stat().st_mode & files.SPECIAL_BITS
    assert "Set-user-ID" in "\n".join(page.last_report)


def test_turning_a_sandbox_hosts_files_into_the_humans_is_asked_first(lab, monkeypatch):
    page, root, _home, _host = lab
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.left.agents_box.setChecked(False)
    assert root.agents and page.left.agents_box.isChecked(), "said no: they stay an agent's"


def test_another_remote_system_is_the_humans_own_unless_said_otherwise(setup, tmp_path, monkeypatch, wait):
    page, _ws, host, _ = setup
    home = tmp_path / "srv"
    home.mkdir()
    (home / "run.sh").write_text("#!/bin/sh\n")
    (home / "run.sh").chmod(0o755)
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    root = connected(page, wait, [{"name": "other", "kind": "ssh", "ssh": "bob@elsewhere"}])
    try:
        assert not root.agents and not page.left.agents_box.isChecked() and not page.left.untrusted()
        select(page.left, "run.sh")
        assert page.copy_selected(page.left)
        finish(page, wait)
        assert os.stat(host / "run.sh").st_mode & 0o111, "as before: the human's own system"
        page.left.agents_box.setChecked(True)  # (asks nothing: the safe direction)
        assert root.agents and page.left.untrusted()
    finally:
        root.close()


def test_the_choice_stays_with_a_connection_made_again(lab, wait):
    page, root, _home, _host = lab
    page.left.agents_box.setChecked(False)
    assert not root.agents
    assert page.connect_remote("alice@fake")
    assert wait(lambda: page.left.root is not None and page.left.root is not root and page.left.listing is None, 10)
    assert not page.left.root.agents and not page.left.agents_box.isChecked()


# -- a remote HOME that is no clean path --

def test_a_remote_home_with_dotdot_breaks_no_pane(setup, tmp_path, monkeypatch, wait):
    """REALPATH answers whatever the server likes: "/x/../y" as the HOME made the pane's choose_root and every listing
    raise (".." is not followed) - the remote system was never shown, the pane stayed empty."""
    page, *_ = setup
    home = tmp_path / "srv"
    (home / "y").mkdir(parents=True)
    root = connect(home, FAKE_SFTP_HOME_ANSWER=f"{home}/x/../y")
    try:
        assert ".." not in root.home.split("/") and root.home.startswith("/")
        files.parts(root.home.lstrip("/"))
    finally:
        root.close()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    monkeypatch.setenv("FAKE_SFTP_HOME_ANSWER", "../../etc\nx")
    root = connected(page, wait, [])
    try:
        assert page.left.root is root and root.home == "/"
    finally:
        root.close()


# -- a sandbox's folder moved or renamed on this machine --

def test_a_sandboxs_folder_moved_within_this_machine_stays_the_agents(setup, tmp_path, wait):
    """A move within this machine is a rename: the workspace ~/projects/ws moved to ~/old/ws was no longer at the path
    sandbox-ai knows - the host pane took it for the human's own folder, followed the agent's links there and copied its
    programs with their executable bits."""
    page, ws, host, _ = setup
    right = page.right
    assert right.go_path(str(tmp_path))
    settle(right)
    clip = files_page.Clip(right.root, [("ws", right.path_of(right.by_name["ws"]), None)], right.untrusted(),
                           right.path_text())
    assert page.transfer_items(clip, right, files_page.host_relative(str(host)), move=True)
    finish(page, wait)
    assert (host / "ws" / "run.sh").exists() and not ws.exists()
    assert right.go_path(str(host / "ws"))
    settle(right)
    assert right.untrusted() and page.below_sandbox(str(host / "ws" / "sub"))
    right.open_row(widgets.find_row(right.table, "escape"))
    assert "not followed" in page.message.text()
    # Renamed in place (F2) too.
    assert right.go_path(str(host))
    settle(right)
    page.rename(right, right.by_name["ws"], "ws2")
    settle(right)
    assert page.below_sandbox(str(host / "ws2" / "sub"))


# -- a sandbox's folder on a file system that hangs --

@pytest.fixture
def recorded(monkeypatch):
    """The file-system calls on HUNG (a folder below tmp_path, set by the test) made on the GUI thread: on a mount that
    hangs (sshfs gone, NFS) each one holds up the whole window."""
    calls, place = [], {}

    def watch(name, real):
        def call(path, *args, **kwargs):
            hung = place.get("hung")
            if hung and isinstance(path, str) and (path == hung or path.startswith(hung + "/")) and \
                    threading.current_thread() is threading.main_thread():
                calls.append((name, path))
            return real(path, *args, **kwargs)
        return call
    for module, name in ((os, "stat"), (os, "lstat"), (os, "open"), (os.path, "realpath"), (os.path, "isdir"),
                         (os.path, "exists"), (os.path, "islink")):
        monkeypatch.setattr(module, name, watch(name, getattr(module, name)))
    return calls, place


def test_a_sandboxs_folder_that_hangs_holds_up_no_window(qapp, tmp_path, monkeypatch, wait, recorded):
    """A folder chosen with Other folder… (or a project) on sshfs whose server went away: every listing of the host pane,
    every copy, Permissions, Properties, a drag out and the Markdown viewer read its path (realpath) and its identity
    (stat) on the GUI thread - the window hung at each, in folders that had nothing to do with it."""
    calls, place = recorded
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    ws, hung, host = tmp_path / "ws", tmp_path / "hung", tmp_path / "host"
    for path in (ws, hung, host):
        path.mkdir()
    (host / "a.txt").write_text("a")
    (host / "dir").mkdir()

    class Failing(FakeSai):
        def file_roots(self, extra=()):
            raise RuntimeError("sandbox-ai did not answer")
    place["hung"] = str(hung)
    page = files_page.FilesPage(Failing([]), projects=lambda: [("project", "", str(ws)), ("other", "", str(hung))])
    try:
        assert wait(lambda: page.roots.count() > 0 and not page.roots_reading, 10)
        right = page.right
        assert right.go_path(str(host))
        settle(page.left, right)
        assert page.below_sandbox(str(ws / "x")) and not right.untrusted()
        select(right, "a.txt", "dir")
        page.export_urls(right, right.chosen())
        page.markdown_top(right.root, files_page.host_relative(str(host / "a.md")))
        page.chmod(right, [right.by_name["a.txt"]], 0o640)
        assert wait(lambda: not page.task.running, 10)
        assert stat.S_IMODE((host / "a.txt").stat().st_mode) == 0o640
        assert page.properties(right, [right.by_name["a.txt"]])
        assert wait(lambda: page.dialogs, 10)
        page.dialogs[-1].close()
        assert page.copy_selected(right)
        finish(page, wait)
        right.list()
        settle(right)
        assert not calls, calls
    finally:
        page.deleteLater()


def test_choosing_a_folder_that_hangs_holds_up_no_window(setup, tmp_path, wait, recorded):
    calls, place = recorded
    page, ws, _host, _ = setup
    hung = tmp_path / "hung"
    hung.mkdir()
    (hung / "inside.txt").write_text("x")
    page.others.append(("other", "", str(hung)))
    page.refresh_roots()
    assert wait(lambda: not page.roots_reading and page.roots.findData(str(hung)) >= 0, 10)
    place["hung"] = str(hung)
    page.roots.setCurrentIndex(page.roots.findData(str(hung)))
    assert wait(lambda: page.left.root is not None and page.left.root.path == str(hung) and page.left.listing is None,
                10)
    assert names(page.left) == ["inside.txt"]
    assert not calls, calls


def test_a_folder_chosen_that_is_slow_to_open_waits_for_its_listing(setup, tmp_path, wait, monkeypatch):
    """While a folder chosen opens in the background, the rows of the folder before stand for no entry of it (an Enter
    on one opened the file of that name in the folder before)."""
    page, ws, _host, _ = setup
    slow = tmp_path / "slow"
    slow.mkdir()
    (slow / "a.txt").write_text("the slow one's")
    page.others.append(("other", "", str(slow)))
    page.refresh_roots()
    assert wait(lambda: not page.roots_reading and page.roots.findData(str(slow)) >= 0, 10)
    gate, real = threading.Event(), files.Root

    class Slow(real):
        def __init__(self, path, *args, **kwargs):
            if os.fspath(path) == str(slow):
                gate.wait(10)
            super().__init__(path, *args, **kwargs)
    monkeypatch.setattr(files, "Root", Slow)
    try:
        assert "a.txt" in names(page.left)
        page.roots.setCurrentIndex(page.roots.findData(str(slow)))
        assert page.left.root is None and page.left.changing()
        select(page.left, "a.txt")
        assert page.left.chosen() == [] and page.left.entry_at(widgets.find_row(page.left.table, "a.txt")) is None
        page.open_current(page.left)
        assert "Wait until" in page.message.text() and not page.editors
        gate.set()
        assert wait(lambda: page.left.root is not None and page.left.listing is None, 10)
        assert page.left.root.path == str(slow) and names(page.left) == ["a.txt"]
    finally:
        gate.set()


# -- Properties closed while Apply runs --

def test_closing_properties_stops_its_apply(tmp_path, qapp, wait):
    """Apply on many items of a remote system far away goes item by item, without a time limit: closing the dialog did
    not stop it - the bits of every item were changed after the human had closed it (and nothing said so)."""
    from sandbox_ai_app.file_properties import PropertiesDialog
    served = tmp_path / "srv"
    served.mkdir()
    items = []
    for index in range(5):
        (served / f"{index}.txt").write_text("x")
        (served / f"{index}.txt").chmod(0o644)
        items.append((f"{index}.txt", f"{index}.txt"))
    root = connect(served)
    go, first = threading.Event(), threading.Event()
    chmod = root.sftp.chmod

    def slow(*args, **kwargs):
        first.set()
        go.wait(10)
        return chmod(*args, **kwargs)
    root.sftp.chmod = slow
    dialog = PropertiesDialog(None, root, items, "/srv", False, infos=files.entry_infos(root, items))
    try:
        dialog.checks[4].setChecked(True)  # (the group's write)
        assert dialog.apply() and wait(lambda: first.is_set(), 10)
        assert "Changing the permissions" in dialog.message.text()
        dialog.close()
        go.set()
        wait(lambda: False, 1.0)
        changed = [name for name, _ in items if stat.S_IMODE((served / name).stat().st_mode) != 0o644]
        assert len(changed) <= 1, changed
    finally:
        go.set()
        root.close()


# -- a file turned into a folder while it is copied aside --

def test_only_files_are_handed_out_when_one_turns_into_a_folder(setup, monkeypatch):
    """copy_aside looked at the names first, then copied each: a file the agent replaced by a folder in between went out
    as that folder (empty, or refused only when it held something) - a folder handed to the other program from the
    agent's."""
    page, ws, _host, _ = setup
    real = files.copy_entry

    def swapped(source, relative, *args, **kwargs):
        if relative == "a.txt" and (ws / "a.txt").is_file():
            os.unlink(ws / "a.txt")
            os.mkdir(ws / "a.txt")
        return real(source, relative, *args, **kwargs)
    monkeypatch.setattr(files, "copy_entry", swapped)
    select(page.left, "a.txt")
    entries = page.left.chosen()
    assert page.export_urls(page.left, entries) == []


# -- reads of a remote system that take long --

def hurried(module, monkeypatch):
    """MODULE's background calls with every time limit as if it had passed already."""
    real = module.background

    def call(*args, timeout=None, **kwargs):
        return real(*args, timeout=None if timeout is None else 0.05, **kwargs)
    monkeypatch.setattr(module, "background", call)


def test_a_remote_file_that_takes_long_to_read_is_not_said_to_be_unreadable(tmp_path, qapp, wait, monkeypatch):
    """A big file of a remote system far away (16 MiB at 1 Mbit/s: over two minutes): after 120 s the editor said
    "Cannot open: No answer" and stayed empty and read-only - while the read went on (each of its requests has a time
    limit of its own)."""
    from sandbox_ai_app import file_editor
    from sandbox_ai_app.file_editor import EditorWindow
    monkeypatch.setattr(widgets, "SHOWN", [])
    served = tmp_path / "srv"
    served.mkdir()
    (served / "a.txt").write_text("remote text")
    root = connect(served)
    go, real = threading.Event(), root.read_file

    def slow(*args, **kwargs):
        go.wait(10)
        return real(*args, **kwargs)
    root.read_file = slow
    hurried(file_editor, monkeypatch)
    window = EditorWindow(None, root, "a.txt", "fake:/srv/a.txt")
    try:
        wait(lambda: False, 0.5)
        assert window.loading and "Cannot open" not in window.notice.text(), window.notice.text()
        go.set()
        assert wait(lambda: not window.loading, 10)
        assert window.text.toPlainText() == "remote text" and not window.read_only
    finally:
        go.set()
        window.dropped = True
        window.close()
        window.deleteLater()
        root.close()


def test_a_remote_image_that_takes_long_is_not_said_to_be_missing(tmp_path, qapp, wait, monkeypatch):
    """The same for the images of a Markdown file of a remote system: after 60 s "Image not shown: No answer"."""
    from sandbox_ai_app import markdown_view
    served = tmp_path / "srv"
    served.mkdir()
    import base64
    (served / "dot.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    root = connect(served)
    go, real = threading.Event(), root.read_file

    def slow(*args, **kwargs):
        go.wait(10)
        return real(*args, **kwargs)
    root.read_file = slow
    hurried(markdown_view, monkeypatch)
    found = []
    try:
        markdown_view.ImageSource(root, "a.md").read_later(["dot.png"], done=lambda result, error: found.append(
            (result, error)))
        wait(lambda: False, 0.5)
        assert not found, found
        go.set()
        assert wait(lambda: found, 10)
        result, error = found[0]
        assert error is None and not isinstance(result["dot.png"], str), found
    finally:
        go.set()
        root.close()


def test_a_long_count_in_properties_is_not_given_up_while_it_goes_on(tmp_path, qapp, wait, monkeypatch):
    """Properties counts a folder's size until Stop (or closing the dialog): after 600 s it said "No answer" and offered
    Compute size again - while the first count went on, and a second one ran beside it on the same connection."""
    from sandbox_ai_app import file_properties
    from sandbox_ai_app.file_properties import PropertiesDialog
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "f").write_text("12345")
    go = threading.Event()

    class Slow(files.Root):
        def measure(self, *args, **kwargs):
            go.wait(10)
            return super().measure(*args, **kwargs)
    root = Slow(tmp_path)
    items = [("dir", "dir")]
    hurried(file_properties, monkeypatch)
    dialog = PropertiesDialog(None, root, items, str(tmp_path), False, infos=files.entry_infos(root, items))
    try:
        wait(lambda: False, 0.5)
        assert dialog.counting and "No answer" not in dialog.size.text(), dialog.size.text()
        go.set()
        assert wait(lambda: not dialog.counting, 10)
        assert "5 B" in dialog.size.text(), dialog.size.text()
    finally:
        go.set()
        dialog.close()
