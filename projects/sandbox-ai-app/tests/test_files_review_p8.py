"""Review pass 8 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
A sandbox's folder that was an entry of the host pane (~/projects/ws listed in ~/projects), or lay inside the folder
copied (~/projects with ws in it), was copied to another host folder or a remote system as the human's own - the
agent's executables executable, its hooks unnamed - and handed to other programs by a drag or the clipboard as it is.
A remote file larger than a copy may be was read up to the limit and put in place cut short; a file changed while it
was copied was reported as copied, nothing more. A remote save checked the file once, before its contents went up, and
replaced what another program wrote meanwhile. A server that answered READDIR without names and never with EOF kept a
listing asking for good (and a move's removal read a listing without bound). An upload cut short by the connection
left the folder it made there, part filled, unsaid. A paste beside the original on a slow file system went into the
folder of another sandbox chosen meanwhile. Closing an editor while it saved asked whether to discard "changes that are
not saved" - they were being saved. Everything changed is below tmp_path."""
import os
from pathlib import Path
import stat
import sys

import pytest

from sandbox_ai_app import files, remote

HERE = Path(__file__).resolve().parent

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import finish, names, select, settle, setup  # noqa: E402,F401
from test_files_review_p1 import connect  # noqa: E402


# -- a sandbox's folder as an entry of the host pane --

def agents_folder_in_host_pane(page, ws, tmp_path):
    """The host pane at tmp_path, the folder that holds the sandbox's workspace ws (an entry there, not the folder
    shown: the pane itself is the host's own)."""
    (ws / ".envrc").write_text("export PATH=/tmp/agent:$PATH\n")
    right = page.right
    assert right.go_path(str(tmp_path))
    settle(right)
    assert not right.untrusted() and "ws" in names(right)


def test_the_agents_folder_copied_from_the_folder_that_holds_it_is_the_agents(setup, tmp_path, wait):
    """The host pane showing ~/projects, the agent's workspace ~/projects/ws one of its entries: Ctrl+C, Ctrl+V into
    another host folder copied it as the human's own - run.sh still executable, its .envrc not named. Copied out of
    the pane's own folder or from inside ws, it was the agent's."""
    page, ws, host, _ = setup
    agents_folder_in_host_pane(page, ws, tmp_path)
    right = page.right
    select(right, "ws")
    assert page.copy_items(right)
    assert right.go_path(str(host))
    settle(right)
    assert page.paste(right)
    report = "\n".join(finish(page, wait))
    copied = host / "ws" / "run.sh"
    assert copied.read_text() == "#!/bin/sh\n" and not copied.stat().st_mode & 0o111, report
    assert "run.sh" in report and ".envrc" in report and "the agent could have written" in report


def test_a_folder_that_holds_the_agents_folder_copies_only_the_agents_part_as_the_agents(setup, tmp_path, wait):
    """A folder of the human's that holds a sandbox's folder (~/projects with ~/projects/ws in it), copied to another
    host folder: the agent's files in it lose their executable bits and its risky files are named; the human's own
    beside them keep theirs."""
    page, ws, host, _ = setup
    agents_folder_in_host_pane(page, ws, tmp_path)
    projects = tmp_path / "projects"
    (projects / "inner").mkdir(parents=True)
    (projects / "inner" / "agent.sh").write_text("agent")
    (projects / "inner" / "agent.sh").chmod(0o755)
    (projects / "inner" / ".envrc").write_text("x")
    (projects / "mine.sh").write_text("mine")
    (projects / "mine.sh").chmod(0o755)
    page.others.append(("other", "", str(projects / "inner")))
    page.refresh_roots()
    assert wait(lambda: not page.roots_reading, 10)
    right = page.right
    right.list()
    settle(page.left, right)
    select(right, "projects")
    assert page.copy_items(right)
    assert right.go_path(str(host))
    settle(right)
    assert page.paste(right)
    report = "\n".join(finish(page, wait))
    assert not (host / "projects" / "inner" / "agent.sh").stat().st_mode & 0o111, report
    assert (host / "projects" / "mine.sh").stat().st_mode & 0o111, "the human's own file keeps its bit"
    assert "agent.sh" in report and "inner/.envrc" in report and "mine.sh" not in report


def test_the_agents_folder_is_not_handed_to_other_programs_from_the_folder_that_holds_it(setup, tmp_path):
    """Dragged out (or copied to the clipboard) from the host pane, a folder is handed to other programs as it is -
    hooks, executables and all: never the agent's folder, nor one that holds it."""
    page, ws, host, _ = setup
    agents_folder_in_host_pane(page, ws, tmp_path)
    right = page.right
    entries = [right.by_name[name] for name in ("ws", "host")]
    urls = [url.toLocalFile() for url in page.export_urls(right, entries)]
    assert urls == [str(host)]


# -- a paste beside the original on a slow file system --

def test_a_paste_beside_goes_nowhere_when_another_folder_was_chosen_while_names_were_read(setup, tmp_path, wait,
                                                                                         monkeypatch):
    """Ctrl+V in the folder of what was copied: the names taken there are read first - on a slow file system in the
    background - and the copy then went into whatever the pane showed by then: another sandbox's folder chosen
    meanwhile got sandbox A's files ("a (copy).txt") in its folder of that name."""
    import time
    page, ws, host, _ = setup
    other = tmp_path / "other-ws"
    other.mkdir()
    monkeypatch.setattr(files_page, "LOCAL_GRACE", 0.05)
    real = files.exists_in

    def slow(root, relative_dir, wanted):
        time.sleep(0.4)
        return real(root, relative_dir, wanted)
    monkeypatch.setattr(files, "exists_in", slow)
    select(page.left, "a.txt")
    assert page.copy_items(page.left)
    assert page.paste(page.left)
    page.left.root = files.Root(other)  # (another sandbox's folder chosen while the names were read)
    assert wait(lambda: "changed meanwhile" in page.message.text() or page.task.running or
                (other / "a (copy).txt").exists(), 10)
    assert wait(lambda: not page.task.running, 10)
    assert not (other / "a (copy).txt").exists() and not (other / "a.txt").exists()
    assert "changed meanwhile" in page.message.text()


# -- closing an editor while it saves --

def test_closing_an_editor_while_it_saves_says_so(tmp_path, qapp, wait, monkeypatch):
    """Ctrl+S, then Ctrl+W at once: asked "has changes that are not saved. Close and discard them?" - while they were
    being saved (and saved they were), and a save that then failed or had a question (changed on disk meanwhile) went
    unsaid: the window was gone."""
    import threading
    from sandbox_ai_app.file_editor import EditorWindow
    monkeypatch.setattr(widgets, "SHOWN", [])
    (tmp_path / "a.txt").write_text("old")
    go = threading.Event()

    class Slow(files.Root):
        def write_file(self, *args, **kwargs):
            go.wait(10)
            return super().write_file(*args, **kwargs)
    window = EditorWindow(None, Slow(tmp_path), "a.txt", str(tmp_path / "a.txt"))
    window.show()
    assert wait(lambda: not window.loading, 10)
    window.text.setPlainText("new")
    assert window.save() and window.saving
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep editing"
    assert not window.may_close()
    title, text, _ = widgets.SHOWN[-1]
    assert "being saved" in text and "not saved. Close and discard" not in text, text
    shown = len(widgets.SHOWN)
    window.dropped = True  # (FilesPage.drop_changes: the app quits for good - SIGTERM, nobody to answer)
    assert window.may_close() and len(widgets.SHOWN) == shown
    window.dropped = False
    go.set()
    assert wait(lambda: not window.saving, 10) and not window.dirty
    assert (tmp_path / "a.txt").read_text() == "new"
    assert window.may_close(), "saved: closed without a question"
    window.close()
    window.deleteLater()


# -- a remote file larger than a copy may be --

def test_a_remote_file_larger_than_the_limit_is_refused_not_cut_short(tmp_path, monkeypatch):
    """A remote file of more than files.LIMIT was read up to the limit, taken as copied and put in place cut short
    (a move then kept its source, a copy said nothing); a local one is refused ("More than ...")."""
    served, host = tmp_path / "srv", tmp_path / "host"
    served.mkdir()
    host.mkdir()
    (served / "big.bin").write_bytes(b"x" * 100)
    monkeypatch.setattr(files, "LIMIT", 60)
    root = connect(served)
    try:
        with pytest.raises(files.Refused, match="More than"):
            files.copy_entry(root, "big.bin", files.Root(host, sandbox=False), "", limit=60)
        with pytest.raises(files.Refused, match="More than"):
            files.move_entry(root, "big.bin", files.Root(host, sandbox=False), "", limit=60)
    finally:
        root.close()
    assert os.listdir(host) == [] and (served / "big.bin").stat().st_size == 100


# -- a file changed while it was copied --

def test_a_copy_says_which_files_changed_while_they_were_copied(setup, monkeypatch, wait):
    """The agent wrote a file while it was copied: the copy holds part old, part new (or less than the file has now),
    and the report said only "copied". (A move kept its source and said so.)"""
    page, ws, host, _ = setup
    real = files._copy_data

    def copy_data(source, target, budget):
        real(source, target, budget)
        with open(ws / "a.txt", "a") as stream:
            stream.write(" and more")
    monkeypatch.setattr(files, "_copy_data", copy_data)
    select(page.left, "a.txt")
    assert page.copy_selected(page.left)
    report = "\n".join(finish(page, wait))
    assert (host / "a.txt").read_text() == "alpha"
    assert "Changed while they were copied" in report and "a.txt" in report, report


def test_a_folder_that_holds_the_agents_folder_goes_to_a_remote_system_with_that_part_as_the_agents(tmp_path):
    """Uploaded, the same: the agent's executables in it arrive without their executable bit; the human's keep it."""
    served, projects = tmp_path / "srv", tmp_path / "projects"
    served.mkdir()
    (projects / "inner").mkdir(parents=True)
    for path in (projects / "inner" / "agent.sh", projects / "mine.sh"):
        path.write_text("x")
        path.chmod(0o755)
    inner = (projects / "inner").stat()
    root = connect(served)
    try:
        done = files.copy_entry(files.Root(tmp_path, sandbox=False), "projects", root, "",
                                agents={(inner.st_dev, inner.st_ino)})
    finally:
        root.close()
    assert not (served / "projects" / "inner" / "agent.sh").stat().st_mode & 0o111
    assert (served / "projects" / "mine.sh").stat().st_mode & 0o111
    assert done.executable == ["projects/inner/agent.sh"]


# -- a server that never ends a listing --

@pytest.mark.parametrize("names", [[], [".", ".."]], ids=["empty answers", "only dot and dotdot"])
def test_a_listing_that_never_ends_is_given_up(tmp_path, names):
    """READDIR answered with no names (or only "." and "..", which are dropped) again and again, never EOF: the listing
    asked forever - its thread never ended and held the connection, every other request of it waited."""
    import struct
    served = tmp_path / "srv"
    served.mkdir()
    root = connect(served)
    real, asked = root.sftp.ask, []

    def ask(kind, payload):
        if kind == remote.READDIR and len(asked) < 10_000:
            asked.append(kind)
            body = b"".join(remote.pack_string(name) + remote.pack_string("") + struct.pack(">I", 0) for name in names)
            return remote.NAME, struct.pack(">I", len(names)) + body
        return real(kind, payload)
    root.sftp.ask = ask
    try:
        try:
            root.list("")
        except OSError:
            pass
        assert len(asked) < 100
    finally:
        root.close()


# -- an upload cut short by the connection --

def test_an_upload_cut_short_by_the_connection_says_what_stayed_there(tmp_path):
    """The connection ended half way through a folder (Cancel of a stalled request, a network gone): the folder made
    there could not be removed any more and stayed, part filled, under its real name - the error said only that the
    connection ended (and a copy promises to remove what it made unless it completes)."""
    served, local = tmp_path / "srv", tmp_path / "local"
    served.mkdir()
    (local / "proj").mkdir(parents=True)
    for name in ("a.txt", "b.txt"):
        (local / "proj" / name).write_text(name)
    root = connect(served)
    real, calls = root.sftp.write_file, []

    def write_file(handle, source, cancelled=None, budget=None):
        calls.append(handle)
        if len(calls) == 2:
            root.sftp.interrupt()  # (the connection ends)
        return real(handle, source, cancelled, budget)
    root.sftp.write_file = write_file
    try:
        with pytest.raises(OSError) as raised:
            files.copy_entry(files.Root(local, sandbox=False), "proj", root, "")
    finally:
        root.close()
    assert (served / "proj").is_dir(), "(it could not be removed)"
    assert "proj" in str(raised.value) and "stayed" in str(raised.value), str(raised.value)


# -- the editor's save of a remote file --

def test_a_remote_file_written_while_a_save_uploads_is_not_replaced_unasked(tmp_path):
    """A save checked the file once, before its new contents went up (a big file: seconds), and renamed them over it
    without looking again - what another program (an agent on a host of sandbox-ai's) wrote meanwhile was gone. A
    local save looks again right before the rename."""
    served = tmp_path / "srv"
    served.mkdir()
    (served / "notes.txt").write_text("old")
    root = connect(served)
    try:
        read = root.read_file("notes.txt")
        real = root.sftp.write_file

        def write_file(handle, source, cancelled=None, budget=None):
            written = real(handle, source, cancelled, budget)
            (served / "notes.txt").write_text("the other program's, longer")
            return written
        root.sftp.write_file = write_file
        with pytest.raises(files.Changed):
            root.write_file("notes.txt", b"mine", read.identity)
        root.sftp.write_file = real
        assert (served / "notes.txt").read_text() == "the other program's, longer"
        assert [name for name in os.listdir(served) if name != "notes.txt"] == [], "no temporary file left"
        root.write_file("notes.txt", b"mine")  # (asked, and Replace: written)
        assert (served / "notes.txt").read_text() == "mine"
    finally:
        root.close()
