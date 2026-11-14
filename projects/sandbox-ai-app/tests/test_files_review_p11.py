"""Review pass 11 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A host folder that held a sandbox's folder moved there (or one mounted deep inside it) went to other programs by a drag
or the clipboard; a sandbox's folder moved on this machine became the human's own once the app was started again; a
rename, a new folder or file and a permission change answered while a copy ran went ahead in the middle of it; the box
"An agent's files" ticked again while its question was open showed ticked while the files counted as the human's; a host
folder holding a sandbox's folder sent to a remote system named none of the agent's files that tools run; a remote
system's files copied before that box was ticked came executable; a project on a file system that hangs stopped every
listing of the host pane, every copy and the list of folders. Everything changed is below tmp_path."""
import os
import threading
import time

import pytest

from sandbox_ai_app import files

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_find_recent import FakeState  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401
from test_files_review_p10 import connected  # noqa: E402


def moved_into(page, wait, ws, folder):
    """The sandbox's folder WS moved (F6: a rename) into the host FOLDER through the host pane."""
    right = page.right
    assert right.go_path(str(ws.parent))
    settle(right)
    clip = files_page.Clip(right.root, [("ws", right.path_of(right.by_name["ws"]), None)], right.untrusted(),
                           right.path_text())
    assert page.transfer_items(clip, right, files_page.host_relative(str(folder)), move=True)
    finish(page, wait)
    assert (folder / "ws" / "run.sh").exists() and not ws.exists()
    return folder / "ws"


def urls_of(page, pane, *wanted):
    select(pane, *wanted)
    return sorted(os.path.basename(files_page.local_path(url)) for url in page.export_urls(pane, pane.chosen()))


# -- a host folder that holds a sandbox's folder under another name --

def test_a_folder_holding_a_moved_sandbox_folder_is_not_handed_out(setup, tmp_path, wait):
    """The workspace moved into ~/projects (F6) stays the agent's (pass 10) - but ~/projects itself, dragged to a mail
    program or copied with Ctrl+C, went out as the human's folder: the agent's hooks and programs in it, executable."""
    page, ws, host, _ = setup
    (host / "projects").mkdir()
    (host / "plain" / "deeper").mkdir(parents=True)
    moved_into(page, wait, ws, host / "projects")
    right = page.right
    assert right.go_path(str(host))
    settle(right)
    assert not right.untrusted()
    assert urls_of(page, right, "projects", "plain") == ["plain"]


def test_a_folder_with_a_sandbox_folder_mounted_deep_inside_is_not_handed_out(setup, wait):
    """A bind mount of a workspace (mount --bind ~/ws ~/data/a/b/ws) is that folder itself - the same device and inode:
    a folder that held one deep inside went out by a drag (only an entry that was one, or held one by its path, was
    kept back)."""
    page, ws, host, _ = setup
    mounted = host / "data" / "a" / "b" / "ws"
    mounted.mkdir(parents=True)
    page.keep_identities({files_page.folder_identity(str(mounted)): str(ws)})  # (as a bind mount of WS would be)
    right = page.right
    right.list()
    settle(right)
    assert urls_of(page, right, "data") == []


def test_a_folder_too_big_to_look_through_in_time_is_not_handed_out(setup, wait, monkeypatch):
    """What could not be looked through within the bound is not known to hold no sandbox's folder: not handed out."""
    page, _ws, host, _ = setup
    for index in range(6):
        (host / "big" / f"d{index}").mkdir(parents=True)
    (host / "small" / "one").mkdir(parents=True)
    (host / "note.txt").write_text("mine")
    monkeypatch.setattr(files_page, "EXPORT_FOLDERS", 4)
    right = page.right
    right.list()
    settle(right)
    assert urls_of(page, right, "big", "small", "note.txt") == ["note.txt", "small"]


def test_the_folders_handed_out_are_looked_through_off_the_gui_thread(setup, wait, monkeypatch):
    """The look through the folders runs in a thread (a big folder, a file system that hangs: the window waits
    EXPORT_WAIT at most, and nothing is handed out then)."""
    page, _ws, host, _ = setup
    (host / "slow").mkdir()
    gate, real = threading.Event(), files_page.holds_agents

    def slow(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(files_page, "holds_agents", slow)
    monkeypatch.setattr(files_page, "EXPORT_WAIT", 0.2)
    right = page.right
    right.list()
    settle(right)
    started = time.monotonic()
    try:
        assert urls_of(page, right, "slow") == []
        assert time.monotonic() - started < 5
    finally:
        gate.set()


# -- a moved sandbox's folder across restarts --

def test_a_moved_sandbox_folder_stays_the_agents_after_a_restart(setup, tmp_path, wait, monkeypatch):
    """Its device and inode were kept while the app ran: started again, the moved workspace (sandbox-ai no longer knows
    it there) was the human's own folder - its links followed, its programs copied executable."""
    page, ws, host, _ = setup
    kept = FakeState()
    monkeypatch.setattr(files_page.app_state, "current", lambda: kept)
    moved = moved_into(page, wait, ws, host)
    page.right.list()
    settle(page.right)
    assert kept.data.get(files_page.IDENTITIES_KEY), "kept in the app's state"
    again = files_page.FilesPage(FakeSai([]))
    try:
        settle(again.left, again.right)
        assert again.right.go_path(str(moved))
        settle(again.right)
        assert again.right.untrusted() and again.below_sandbox(str(moved / "sub"))
        again.right.open_row(widgets.find_row(again.right.table, "escape"))
        assert "not followed" in again.message.text()
    finally:
        again.deleteLater()


def test_the_kept_identities_are_pruned(setup, monkeypatch):
    """Not seen for KEPT_DAYS (the folder is gone, or never visited since): dropped - an inode used again for a folder of
    the human's made it an agent's. At most KEPT_IDENTITIES, those seen last kept. Anything else in the state is
    dropped."""
    page, *_ = setup
    today = files_page.today()
    old = today - files_page.KEPT_DAYS - 1
    data = [[1, 2, "/gone", old], [3, 4, "/recent", today], "junk", [5, "x", "/bad", today], [7, 8, 9, today],
            *([10 + n, 1, f"/many{n}", today - 1] for n in range(files_page.KEPT_IDENTITIES + 5))]
    kept = FakeState({files_page.IDENTITIES_KEY: data})
    monkeypatch.setattr(files_page.app_state, "current", lambda: kept)
    page2 = files_page.FilesPage(FakeSai([]))
    try:
        assert (3, 4) in page2.identities and (1, 2) not in page2.identities
        assert (5, "x") not in page2.identities and (7, 8) not in page2.identities
        assert len(page2.identities) == files_page.KEPT_IDENTITIES
    finally:
        page2.deleteLater()


# -- what was asked before a job began, answered while it runs --

def test_changes_answered_while_a_job_runs_wait_for_it(setup, wait):
    """Rename, New folder, New file and Permissions ask first (a dialog that blocks nothing): a copy started meanwhile
    was then renamed under, or had a folder made in what it copied - their keys and buttons are greyed out while a job
    runs for that reason, but the answer of a question asked before went ahead."""
    page, ws, host, _ = setup
    (host / "a.txt").write_text("a")
    right = page.right
    right.list()
    settle(right)
    gate = threading.Event()
    assert page.run_job([right.root], lambda cancelled: gate.wait(10), lambda *_: None, "a job")
    try:
        entry = right.by_name["a.txt"]
        assert not page.rename(right, entry, "b.txt")
        assert "Wait until" in page.message.text()
        assert not page.make_folder(right, "new")
        assert not page.make_file(right, "new.txt", edit=False)
        assert not page.chmod(right, [entry], 0o600)
        assert (host / "a.txt").exists() and not (host / "b.txt").exists()
        assert not (host / "new").exists() and not (host / "new.txt").exists()
        assert os.stat(host / "a.txt").st_mode & 0o777 != 0o600
    finally:
        gate.set()
        finish(page, wait)
    assert page.rename(right, right.by_name["a.txt"], "b.txt"), "once it has ended"
    assert (host / "b.txt").exists()


# -- "An agent's files" ticked again while its question is open --

def test_the_agents_box_ticked_again_while_asked_stays_true(setup, tmp_path, monkeypatch, wait):
    """The question before the files of a sandbox-ai host count as the human's blocks nothing: the box ticked again while
    it was open, then the question answered with "Treat as my own" - the box showed the files as an agent's, and they
    counted as the human's (copied executable)."""
    page, *_ = setup
    home = tmp_path / "lab"
    home.mkdir()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    root = connected(page, wait, [{"name": "lab", "kind": "ssh", "ssh": "alice@fake"}])
    try:
        box = page.left.agents_box
        assert root.agents and box.isChecked()

        def meanwhile(*_args, **_kwargs):
            box.setChecked(True)  # (the human, while the question is open)
            return True
        monkeypatch.setattr(files_page, "confirm", meanwhile)
        box.setChecked(False)
        assert box.isChecked() and root.agents and page.left.untrusted()
    finally:
        root.close()


# -- a host folder holding a sandbox's folder, sent to a remote system --

def test_a_sandbox_folder_sent_to_a_remote_system_names_its_risky_files(setup, tmp_path, monkeypatch, wait):
    """~/projects with the workspace in it, copied to the human's own remote system: the workspace's files arrive never
    executable (pass 7) - but its .envrc and git hooks went unnamed (a copy on this machine names them)."""
    page, ws, host, _ = setup
    (ws / ".envrc").write_text("export X=1\n")
    served = tmp_path / "srv"
    served.mkdir()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(served))
    root = connected(page, wait, [{"name": "other", "kind": "ssh", "ssh": "bob@elsewhere"}])
    try:
        assert not root.agents
        right = page.right
        assert right.go_path(str(ws.parent))
        settle(right)
        select(right, "ws")
        assert page.copy_selected(right)
        report = "\n".join(finish(page, wait))
        assert (served / "ws" / ".envrc").exists()
        assert not os.stat(served / "ws" / "run.sh").st_mode & 0o111
        assert "Careful" in report and ".envrc" in report
    finally:
        root.close()


# -- "An agent's files" ticked after the items were copied or cut --

def test_items_copied_before_the_agents_box_was_ticked_come_as_an_agents(setup, tmp_path, monkeypatch, wait):
    """Ctrl+C on a remote system's program, then the box "An agent's files" ticked (the human found out what wrote
    them), then Ctrl+V on this machine: the copy came executable - what was copied kept the choice of the moment of
    Ctrl+C. The choice when it is pasted counts where it is the stricter one; so for Permissions."""
    page, _ws, host, _ = setup
    served = tmp_path / "srv"
    served.mkdir()
    (served / "run.sh").write_text("#!/bin/sh\n")
    (served / "run.sh").chmod(0o755)
    (served / "dir").mkdir()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(served))
    root = connected(page, wait, [{"name": "other", "kind": "ssh", "ssh": "bob@elsewhere"}])
    try:
        left = page.left
        assert not root.agents
        select(left, "run.sh", "dir")
        assert page.copy_items(left)
        chosen = files_page.Chosen.of(left, [left.by_name["dir"]])
        left.agents_box.setChecked(True)
        assert root.agents
        assert page.paste(page.right)
        report = "\n".join(finish(page, wait))
        assert not os.stat(host / "run.sh").st_mode & 0o111, report
        assert page.chmod_items(chosen, 0o2755)
        finish(page, wait)
        assert not os.stat(served / "dir").st_mode & 0o2000, "no new set-ID bit on an agent's files"
    finally:
        root.close()


# -- a folder on a file system that hangs, read in the threads --

@pytest.fixture
def hanging(monkeypatch):
    """The file-system calls on HUNG (a folder below tmp_path, set by the test) hang in every thread but the GUI thread
    until GATE is set (sshfs whose server went away: a stat there never returns); on the GUI thread they are recorded
    (each one would hold up the window)."""
    gate, place, calls = threading.Event(), {}, []

    def watch(name, real):
        def call(path, *args, **kwargs):
            hung = place.get("hung")
            if hung and isinstance(path, str) and (path == hung or path.startswith(hung + "/")):
                if threading.current_thread() is threading.main_thread():
                    calls.append((name, path))
                else:
                    gate.wait(60)
            return real(path, *args, **kwargs)
        return call
    for module, name in ((os, "stat"), (os, "lstat"), (os, "open"), (os, "scandir"), (os.path, "realpath"),
                         (os.path, "isdir"), (os.path, "exists"), (os.path, "islink")):
        monkeypatch.setattr(module, name, watch(name, getattr(module, name)))
    yield gate, place, calls
    gate.set()


def test_a_folder_that_hangs_holds_up_no_listing_copy_or_list_of_folders(qapp, tmp_path, monkeypatch, wait, hanging):
    """Pass 10 read a hung project's path and identity in the threads instead of on the GUI thread - but there they were
    waited for: every listing of the host pane never came (after two minutes "No answer", an empty list), a copy never
    ended (Cancel could not end it, the page's buttons stayed greyed out), and the list of folders showed no sandbox
    for a minute, then only the projects."""
    gate, place, calls = hanging
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    ws, hung, host = tmp_path / "ws", tmp_path / "hung", tmp_path / "host"
    for path in (ws, hung, host):
        path.mkdir()
    (host / "a.txt").write_text("a")
    place["hung"] = str(hung)
    page = files_page.FilesPage(FakeSai([("run", "claude-abc", str(ws))]),
                                projects=lambda: [("project", "", str(hung))])
    try:
        assert wait(lambda: page.roots.count() > 0 and not page.roots_reading, 10)
        assert page.roots.findData(str(ws)) >= 0, "the sandboxes are listed"
        right = page.right
        assert right.go_path(str(host))
        assert wait(lambda: right.listing is None, 10), "the host pane is listed"
        assert names(right) == ["a.txt"]
        if page.left.root is None or page.left.root.path != str(ws):
            page.roots.setCurrentIndex(page.roots.findData(str(ws)))
            assert wait(lambda: page.left.root is not None and page.left.listing is None, 10)
        select(right, "a.txt")
        assert page.copy_selected(right)
        assert wait(lambda: not page.task.running, 10), "the copy ends"
        assert (ws / "a.txt").read_text() == "a"
        assert not calls, calls
    finally:
        gate.set()
        page.deleteLater()


# -- the host pane listed before the list of folders came --

def test_the_host_pane_says_it_is_in_a_sandboxs_folder_once_the_folders_are_known(qapp, tmp_path, monkeypatch, wait):
    """The host pane is listed at once, the sandboxes' folders come later (sandbox-ai may ask Podman): a host folder in a
    running sandbox's workspace listed meanwhile kept saying nothing (no note that it belongs to a sandbox, its set-ID
    bits not marked) until it was listed again by hand."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    gate = threading.Event()

    class Slow(FakeSai):
        def file_roots(self, extra=()):
            gate.wait(10)
            return super().file_roots(extra)
    page = files_page.FilesPage(Slow([("run", "claude-abc", str(ws.resolve()))]))
    try:
        right = page.right
        assert right.go_path(str(ws / "sub"))
        settle(right)
        assert page.roots_reading and right.note.isHidden()
        gate.set()
        assert wait(lambda: not page.roots_reading and right.listing is None and not right.note.isHidden(), 10)
        assert right.inside and "belongs to a sandbox" in right.note.text()
    finally:
        gate.set()
        page.deleteLater()


# -- a remote "/" that is answered as another folder --

def test_a_remote_root_answered_as_another_folder_breaks_no_pane(setup, tmp_path, monkeypatch, wait):
    """REALPATH answers whatever the server likes, also for "/": "/srv/x/.." made the pane's root that folder - the HOME
    was looked for below it (nothing shown), and every path shown named another folder than the one acted on."""
    page, *_ = setup
    home = tmp_path / "srv"
    home.mkdir()
    (home / "mine.txt").write_text("x")
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    monkeypatch.setenv("FAKE_SFTP_ROOT_ANSWER", f"{tmp_path}/elsewhere/..")
    root = connected(page, wait, [])
    try:
        assert root.path == "/" and root.key == "ssh://alice@fake/"
        assert page.left.root is root and names(page.left) == ["mine.txt"]
    finally:
        root.close()
