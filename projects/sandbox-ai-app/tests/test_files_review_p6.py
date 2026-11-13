"""Review pass 6 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
Cancel of "Take ownership" (and its time limit) waited for what podman unshare had started - the helper went on changing
owners, and the job did not end until it had finished. Files of another program pasted to a sandbox-ai host reached by
another name or without its port (an alias of ~/.ssh/config) went there unasked. ".." typed in the sandbox pane's path
bar was refused, even below its top. A remote file another program wrote right after a save was replaced unasked by the
next save. Paste was offered with nothing to paste. The report of a job went when the panes were listed again (a slow
listing's note, the other pane's folder deleted). Reload went back to the view the window was opened with, and a Reload
that failed left the window "read-only" for good. Everything changed is below tmp_path."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from sandbox_ai_app import files
from sandbox_ai_app.files import Cancelled

HERE = Path(__file__).resolve().parent

from test_files_ops import fake_podman, places  # noqa: E402,F401


# -- taking ownership: podman unshare and its helper --

def podman_that_forks(tmp_path, before="sleep 30"):
    """`podman unshare CMD...` as the real one runs it: CMD a child of podman's (podman unshare re-executes itself in
    the user namespace and runs the command there), here after BEFORE, another child that holds the output pipe."""
    script = tmp_path / "podman-forks"
    script.write_text(f'#!/bin/sh\n[ "$1" = unshare ] || exit 9\nshift\n{before}\n"$@"\n')
    script.chmod(0o755)
    return str(script)


def test_cancel_of_take_ownership_stops_what_podman_started(places, tmp_path):
    """podman was killed, its child went on (holding the output pipe): the job waited for it - and the helper changed
    owners after Cancel."""
    sandbox, *_ = places
    started = time.monotonic()
    with pytest.raises(Cancelled):
        files.take_ownership(sandbox, "sub", cancelled=lambda: time.monotonic() - started > 0.5,
                             podman=podman_that_forks(tmp_path), owner=(os.getuid(), os.getgid()))
    assert time.monotonic() - started < 10, "the job ended at Cancel"


def test_take_ownership_ends_at_its_time_limit(places, tmp_path):
    sandbox, *_ = places
    started = time.monotonic()
    with pytest.raises(Cancelled):
        files.take_ownership(sandbox, "sub", podman=podman_that_forks(tmp_path), owner=(os.getuid(), os.getgid()),
                             timeout=0.5)
    assert time.monotonic() - started < 10


# The helper's own mount check (it runs inside podman unshare, with no files._mount_of to stand in for): a folder taken
# for a mount point where FAKE_MOUNT says, else the helper's real mount().
FAKE_MOUNT = '''
real_mount = mount

def mount(fd):
    where = os.readlink("/proc/self/fd/%d" % fd)
    marked = os.environ.get("FAKE_MOUNT", "")
    if marked and (where == marked or where.startswith(marked + "/")):
        return ("mount", -1)
    return real_mount(fd)
'''


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="podman unshare: Linux")
def test_the_ownership_helper_skips_a_mount_point_in_a_folder(places, fake_podman, monkeypatch):
    """Keeps it so (the helper had no test): neither the mount point's top nor what it holds is changed or walked."""
    sandbox, _, ws, _, _ = places
    monkeypatch.setattr(files, "OWNERSHIP_HELPER", files.OWNERSHIP_HELPER.replace("\ndef own(", FAKE_MOUNT + "\ndef own(", 1))
    monkeypatch.setenv("FAKE_MOUNT", str(ws / "sub" / "deeper"))
    foreign = max(files.user_groups()) + 54321  # (the helper tries every entry it walks: each one is reported)
    result = files.take_ownership(sandbox, "sub", podman=fake_podman, owner=(os.getuid(), foreign))
    reasons = dict(result.skipped)
    assert sorted(reasons) == ["sub", "sub/b.txt", "sub/deeper"]
    assert "mount point" in reasons["sub/deeper"]
    monkeypatch.setenv("FAKE_MOUNT", str(ws / "sub"))  # (the folder chosen itself is changed and walked)
    assert sorted(dict(files.take_ownership(sandbox, "sub", podman=fake_podman, owner=(os.getuid(), foreign))
                       .skipped)) == ["sub", "sub/b.txt", "sub/deeper", "sub/deeper/c.txt"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's mount IDs")
def test_the_ownership_helpers_mount_check_tells_mounts_apart(tmp_path):
    """Keeps it so: the helper's mount() (/proc/self/fdinfo's mnt_id) - "/" and /proc are two mounts, a folder and the
    one in it are on one."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    head = files.OWNERSHIP_HELPER.split("\ndef own(", 1)[0]
    probe = head + '''
D2 = os.O_RDONLY | os.O_DIRECTORY
one, two, three, four = (mount(os.open(path, D2)) for path in ("/", "/proc", sys.argv[7], sys.argv[8]))
print(json.dumps([one[0], one != two, three == four]))
'''
    done = subprocess.run([sys.executable, "-I", "-c", probe, "/", "x", "0", "0", "0", "0", str(tmp_path / "a"),
                           str(tmp_path / "a" / "b")], capture_output=True, text=True, timeout=30, check=True)
    assert done.stdout.strip() == '["mount", true, true]'


# -- files of another program pasted to a sandbox-ai host: the host as ssh reaches it --

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files_page, remote, widgets  # noqa: E402
from sandbox_ai_app.files_page import host_relative  # noqa: E402
from test_files_page import finish, names, select, settle, setup  # noqa: E402,F401
from test_files_remote import served  # noqa: E402,F401


def test_a_sandbox_host_reached_without_its_port_asks_first(served, monkeypatch, tmp_path):
    """A host entry with port 2222, connected as its name alone (an alias of ~/.ssh/config that has the Port, or a
    name whose port ssh could not tell): taken for another host, its files copied there unasked."""
    page, home, ws, host = served
    page.sai.host_entries = lambda: [{"name": "lab", "kind": "ssh", "ssh": "alice@fake", "port": 2222}]
    secret = tmp_path / "id_ed25519"
    secret.write_text("a key")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert page.copy_paths([str(secret)], page.left, page.left.relative, foreign=True) is False
    assert any(title == "Copy files of another program to alice@fake?" for title, *_ in widgets.SHOWN)
    assert not (home / "id_ed25519").exists()


def test_a_sandbox_host_is_found_where_ssh_connects(served):
    """An alias and the address it stands for (ssh -G: HostName and Port), either way round."""
    page, *_ = served
    root = page.left.root
    page.sai.host_entries = lambda: [{"name": "lab", "kind": "ssh", "ssh": "me@build", "port": 2222}]
    root.endpoint, root.sandbox_endpoints = ("10.0.0.5", 2222), {("10.0.0.5", 2222)}
    assert page.sandbox_host(root)
    page.sai.host_entries = lambda: [{"name": "lab", "kind": "ssh", "ssh": "me@10.0.0.5", "port": 2222}]
    root.sandbox_endpoints = set()
    assert page.sandbox_host(root), "the entry names the address the alias stands for"
    page.sai.host_entries = lambda: [{"name": "lab", "kind": "ssh", "ssh": "me@10.0.0.5", "port": 2223}]
    assert not page.sandbox_host(root), "another port of that address, as ssh says: another sshd"


def test_dot_dot_typed_in_the_sandbox_pane_goes_up_but_never_above_its_top(setup):
    """".." in the path bar went up in the host pane and was refused in the sandbox pane ("“..” is not followed")."""
    page, ws, _, _ = setup
    (ws / "sub" / "deeper").mkdir()
    left = page.left
    assert left.go_path(str(ws / "sub" / "deeper")) and left.relative == "sub/deeper"
    settle(left)
    assert left.go_path("..") and left.relative == "sub"
    settle(left)
    assert left.go_path("deeper/../../sub/./deeper") and left.relative == "sub/deeper"
    settle(left)
    assert left.go_path(str(ws / "sub" / "deeper") + "/../..") and left.relative == ""
    settle(left)
    assert not left.go_path("..") and not left.go_path("sub/../..") and left.relative == ""
    assert not left.go_path(str(ws) + "/../outside") and left.relative == ""
    assert "not followed" in page.message.text()


@pytest.mark.parametrize("in_place", [False, True])
def test_a_remote_file_written_right_after_a_save_is_not_replaced_unasked(tmp_path, monkeypatch, in_place):
    """The identity a save returned was read after it: a write of another program right after the save was taken for
    the save's own, and the next save replaced it without a question (a local save keeps what it wrote)."""
    from test_files_review_p1 import connect
    (tmp_path / "f.txt").write_text("old")
    root = connect(tmp_path)
    try:
        first = root.read_file("f.txt")
        step = "close_written" if in_place else "rename"  # (the save's last step on the server)
        writer = getattr(root.sftp, step)

        def then_the_agent(*args, **kwargs):
            done = writer(*args, **kwargs)
            with open(tmp_path / "f.txt", "a") as stream:
                stream.write(" and the agent's line")
            return done
        monkeypatch.setattr(root.sftp, step, then_the_agent)
        saved = root.write_file("f.txt", b"mine", first.identity, in_place=in_place)
        monkeypatch.undo()
        with pytest.raises(files.Changed):
            root.write_file("f.txt", b"mine again", saved, in_place=in_place)
        assert (tmp_path / "f.txt").read_text() == "mine and the agent's line"
        assert root.write_file("f.txt", b"as it was", root.read_file("f.txt").identity, in_place=in_place)
    finally:
        root.close()


def test_paste_is_offered_only_when_it_has_something(setup):
    """Files copied here, then text copied in another program: Paste was offered, and said "Nothing to paste"."""
    from PySide6.QtGui import QGuiApplication
    page, ws, _, _ = setup
    select(page.left, "a.txt")
    assert page.copy_items(page.left)

    def offered():
        menu = page.menu(page.right, at_row=False)
        action = next(action for action in menu.actions() if action.text() == "Paste")
        enabled = action.isEnabled()
        menu.deleteLater()
        return enabled
    assert offered() and page.can_paste()
    QGuiApplication.clipboard().setText("some text of another program")
    assert not offered() and not page.can_paste()
    assert not page.paste(page.right) and "Nothing to paste" in page.message.text()


def test_the_report_of_a_delete_stays_when_the_other_pane_showed_what_was_deleted(setup, wait):
    page, ws, _, _ = setup
    page.right.go_path(str(ws / "sub"))
    settle(page.right)
    select(page.left, "sub")
    assert page.delete_selected(page.left)
    assert wait(lambda: not page.task.running, 10)
    settle(page.left, page.right)
    text = page.message.text()
    assert "2 file(s) and folder(s) deleted." in text, text
    assert page.right.relative != host_relative(str(ws / "sub"))


def test_the_report_of_a_copy_stays_when_a_listing_takes_long(setup, wait, monkeypatch):
    """The "Listing…" note of a slow listing took the report's place, and went with the listing: the report was
    gone (what was skipped, what lost its executable bit)."""
    page, ws, host, _ = setup
    monkeypatch.setattr(files_page, "LIST_WAIT", 50)
    real = files.Root.list

    def slow(root, relative=""):
        time.sleep(0.3)
        return real(root, relative)
    monkeypatch.setattr(files.Root, "list", slow)
    select(page.left, "run.sh")
    assert page.copy_selected(page.left)
    assert wait(lambda: not page.task.running, 10)
    assert wait(lambda: "Listing" in page.message.text(), 10)
    settle(page.left, page.right)
    assert "copied" in page.message.text() and "run.sh" in page.message.text(), page.message.text()


@pytest.mark.parametrize("opened, then", [("auto", "hex"), ("markdown", "text"), ("auto", "markdown")])
def test_reload_keeps_the_view_shown(tmp_path, qapp, wait, opened, then):
    """Reload showed the view the window was opened with: the text of a Markdown file being edited went back to the
    rendering, the hex view to the text."""
    from sandbox_ai_app.file_editor import EditorWindow
    (tmp_path / "a.md").write_text("# A\n\ntext\n")
    window = EditorWindow(None, files.Root(tmp_path), "a.md", str(tmp_path / "a.md"), opened)
    window.show()
    assert wait(lambda: not window.loading, 10)
    assert window.switch(then) and window.view == then
    (tmp_path / "a.md").write_text("# A\n\nnew text\n")
    assert window.reload()
    assert wait(lambda: not window.loading, 10)
    assert window.view == then and window.original == b"# A\n\nnew text\n"
    window.close()
    window.deleteLater()


def test_a_reload_that_fails_leaves_the_editor_as_it_was(tmp_path, qapp, wait):
    """The file gone for a moment (a program that writes it anew): the window said "read-only" from then on and could
    not save any more - while its text still took what was typed."""
    from sandbox_ai_app.file_editor import EditorWindow
    (tmp_path / "a.txt").write_text("old")
    window = EditorWindow(None, files.Root(tmp_path), "a.txt", str(tmp_path / "a.txt"))
    window.show()
    assert wait(lambda: not window.loading, 10)
    (tmp_path / "a.txt").unlink()
    assert window.reload()
    assert wait(lambda: not window.loading, 10)
    assert "Cannot open a.txt" in window.notice.text()
    assert not window.read_only and window.save_button.isEnabled() and not window.text.isReadOnly()
    assert "read-only" not in window.windowTitle()
    window.text.setPlainText("mine")
    assert window.save(replace=True)
    assert wait(lambda: not window.saving, 10)
    assert (tmp_path / "a.txt").read_text() == "mine"
    window.close()
    window.deleteLater()


def test_endpoint_of_reads_what_ssh_makes_of_a_target(tmp_path):
    program = tmp_path / "fake-ssh"
    program.write_text('#!/bin/sh\n[ "$1" = -G ] || exit 9\nprintf "user me\\nhostname Build.Example.org\\nport 2222\\n"\n')
    program.chmod(0o755)
    assert remote.endpoint_of(remote.Target("build"), ssh=str(program)) == ("build.example.org", 2222)
    assert remote.endpoint_of(remote.Target("build"), ssh=str(tmp_path / "missing")) is None
    assert remote.endpoint_of(remote.Target("build"), ssh=["a", "list"]) is None
