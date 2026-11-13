"""The Files page with a remote system (remote.py) in the sandbox pane, against the fake SFTP server
(fake_sftp_server.py): connecting (a host of sandbox-ai or ~/.ssh/config, or user@host typed), an unknown host's keys
trusted only after a yes, a changed key refused, a locked key with a terminal offered, browsing with "." and "..",
links named and never followed, copies and moves both ways, drag and drop, New folder, New file and the editor, the
Markdown viewer, Properties, the free space, Recent and a connection that ended. Everything changed is below
tmp_path."""
import os
from pathlib import Path
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import files, remote, widgets  # noqa: E402
from test_files_manager import drop_at, dragged, editor_of, row_point, shown  # noqa: E402
from test_files_page import finish, names, press, select, settle, setup  # noqa: E402,F401

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]


@pytest.fixture
def served(setup, tmp_path, monkeypatch, wait):
    """The page connected to the fake remote system, whose HOME is tmp_path/remote."""
    page, ws, host, outside = setup
    home = tmp_path / "remote"
    (home / "docs").mkdir(parents=True)
    (home / "docs" / "a.txt").write_text("alpha")
    (home / "docs" / "guide.md").write_text("# Guide\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    (home / "escape").symlink_to(outside)
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    page.ssh_program = FAKE
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem", lambda *a, **k: ("alice@fake", True))
    assert page.remote_dialog()
    assert wait(lambda: page.remotes and page.left.remote() and page.left.listing is None, 10), page.message.text()
    settle(page.left)
    yield page, home.resolve(), ws, host
    for root in page.remotes.values():
        root.close()


def test_connecting_shows_the_remote_home_with_dot_and_dotdot(served):
    page, home, ws, host = served
    left = page.left
    key = next(iter(page.remotes))
    assert key.startswith("ssh://alice@fake") and left.roots.currentData() == key
    assert left.roots.currentText() == "Remote system alice@fake"
    assert left.path_text() == str(home) and names(left) == ["docs", "escape"]
    rows = [left.table.item(r, 0).text() for r in range(2)]
    assert rows == [".", ".."] and left.table.model().flags(left.table.model().index(1, 0)) & Qt.ItemFlag.ItemIsEnabled
    assert " free of " in left.space.text()
    owner = left.table.item(widgets.find_row(left.table, "docs"), 5).text()
    assert owner == f"{os.getuid()}:{os.getgid()}", "a remote system's owners as numbers"
    assert not left.untrusted() and not page.buttons["own"].isEnabled()
    left.go("")
    settle(left)
    assert left.path_text() == "/" and not left.table.model().flags(left.table.model().index(1, 0)) & \
        Qt.ItemFlag.ItemIsEnabled, "the remote / is the top"
    left.go_path(str(home / "docs"))
    settle(left)
    assert names(left) == ["a.txt", "guide.md"] and left.recent[0] == (key, str(home / "docs").lstrip("/"))


def test_links_are_named_and_never_followed(served):
    page, home, ws, host = served
    left = page.left
    left.open_row(widgets.find_row(left.table, "escape"))
    settle(left)
    assert "is a symbolic link to" in page.message.text() and left.path_text() == str(home)
    left.go_path(str(home / "escape"))
    settle(left)
    assert "not followed" in page.message.text() and left.path_text() != str(home / "escape")
    row = widgets.find_row(left.table, "escape") if left.path_text() == str(home) else -1
    assert row < 0 or left.table.item(row, 4).text().startswith("→ ")


def test_copies_and_moves_both_ways_and_drag_and_drop(served, wait):
    page, home, ws, host = served
    left, right = page.left, page.right
    left.go_path(str(home / "docs"))
    select(left, "a.txt")
    assert page.copy_selected(left)
    finish(page, wait)
    assert (host / "a.txt").read_text() == "alpha" and (home / "docs" / "a.txt").exists()
    (host / "up.txt").write_text("from the host")
    right.list()
    select(right, "up.txt")
    assert page.copy_selected(right, move=True)
    finish(page, wait)
    assert (home / "docs" / "up.txt").read_text() == "from the host" and not (host / "up.txt").exists()
    # Into a sandbox's folder: what comes from a remote system counts as the agent's (never executable there).
    (home / "docs" / "tool.sh").write_text("echo")
    (home / "docs" / "tool.sh").chmod(0o755)
    left.list()
    right.go_path(str(ws))
    settle(left, right)
    select(left, "tool.sh")
    assert page.copy_selected(left)
    finish(page, wait)
    assert (ws / "tool.sh").exists() and not os.access(ws / "tool.sh", os.X_OK)
    # Drag and drop: from the host pane onto the remote pane's ".".
    shown(page)
    right.go_path(str(host))
    settle(right)
    (host / "dropped.txt").write_text("dropped")
    right.list()
    settle(right)
    taken = []
    dragged(page, right, ["dropped.txt"], lambda m: taken.append(drop_at(left, m, row_point(left, "."))))
    finish(page, wait)
    assert taken == [True] and (home / "docs" / "dropped.txt").read_text() == "dropped"
    assert page.export_urls(left, left.chosen()) == [], "a remote file is never handed to another program as a URL"


def test_folders_files_the_editor_markdown_properties_and_delete(served, wait):
    page, home, ws, host = served
    left = page.left
    left.go_path(str(home / "docs"))
    settle(left)
    assert page.make_folder(left, "made") and page.task.running, "a remote system's: in the task's thread"
    finish(page, wait)
    assert (home / "docs" / "made").is_dir()
    settle(left)
    assert page.make_file(left, "new.txt")
    finish(page, wait)
    window = editor_of(page, wait)
    assert window.relative.endswith("docs/new.txt") and window.view == "text"
    window.text.insertPlainText("written remotely")
    assert window.save() and wait(lambda: not window.saving, 10)
    assert (home / "docs" / "new.txt").read_text() == "written remotely"
    window.close()
    page.left.open_row(widgets.find_row(left.table, "guide.md"))
    viewer = editor_of(page, wait)
    assert viewer.view == "markdown" and viewer.markdown.outline.item(0).text() == "Guide"
    viewer.close()
    select(left, "made", "a.txt")
    count = len(page.dialogs)
    assert page.properties(left) is True, "read in the background first"
    assert wait(lambda: len(page.dialogs) > count, 10)
    dialog = page.dialogs[-1]
    assert wait(lambda: not dialog.counting) and "in 1 file(s) and 1 folder(s)" in dialog.size.text()
    assert not any(b.text() == "Owner…" for b in dialog.findChildren(QtWidgets.QPushButton)), "no owner change"
    dialog.close()
    select(left, "a.txt")
    assert page.rename(left, left.by_name["a.txt"], "b.txt")
    finish(page, wait)
    assert (home / "docs" / "b.txt").exists()
    settle(left)
    select(left, "b.txt")
    assert page.chmod(left, left.chosen(), 0o600)
    finish(page, wait)
    assert (home / "docs" / "b.txt").stat().st_mode & 0o777 == 0o600
    settle(left)
    select(left, "made", "b.txt")
    widgets.SHOWN.clear()
    assert page.delete_selected(left)
    finish(page, wait)
    assert [title for title, *_ in widgets.SHOWN] == ["Delete?"], "asked first"
    assert not (home / "docs" / "made").exists() and not (home / "docs" / "b.txt").exists()


def test_a_connection_that_ended_is_said_and_connected_again(served, wait):
    page, home, ws, host = served
    left = page.left
    root = left.root
    root.sftp.process.kill()
    root.sftp.process.wait()
    left.list()
    settle(left)
    assert "ended" in page.message.text() or "connection" in page.message.text().lower()
    left.choose_root()
    assert "SSH… connects again" in page.message.text()
    assert page.connect_remote("alice@fake")
    assert wait(lambda: page.left.remote() and page.left.root is not root and page.left.root.alive, 10)


# -- connecting: what is asked, what is refused --

def failing(text, script, tmp_path):
    """A fake ssh that writes TEXT to stderr and fails, until SCRIPT's condition holds (then it serves)."""
    path = tmp_path / "fake-ssh"
    path.write_text(f"#!/bin/sh\n{script}\necho '{text}' >&2\nexit 255\n")
    path.chmod(0o755)
    return [str(path)]


def test_an_unknown_host_is_trusted_only_after_a_yes(setup, tmp_path, monkeypatch, wait):
    page, ws, host, _ = setup
    known = tmp_path / "known_hosts"
    serve = " ".join(FAKE)
    page.ssh_program = failing("Host key verification failed.",
                               f"grep -q fake '{known}' 2>/dev/null && exec {serve}", tmp_path)
    page.known_hosts = str(known)
    monkeypatch.setenv("FAKE_SFTP_HOME", str(tmp_path))
    monkeypatch.setattr(remote, "scan_host", lambda target: ["fake ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA"])
    monkeypatch.setattr(remote, "fingerprints", lambda lines: ["ED25519 SHA256:abcdef"])
    widgets.AUTO_ANSWER = False
    widgets.SHOWN.clear()
    assert page.connect_remote("fake")
    assert wait(lambda: widgets.SHOWN, 10)
    title, text, _ = widgets.SHOWN[0]
    assert title == "Trust this host?" and "ED25519 SHA256:abcdef" in text
    assert wait(lambda: "not trusted" in page.message.text(), 5) and not known.exists() and not page.remotes
    widgets.AUTO_ANSWER = True
    assert page.connect_remote("fake")
    assert wait(lambda: page.remotes, 10), page.message.text()
    assert known.read_text() == "fake ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA\n"


def test_a_changed_host_key_is_refused_and_never_added(setup, tmp_path, monkeypatch, wait):
    page, ws, host, _ = setup
    page.known_hosts = str(tmp_path / "known_hosts")
    page.ssh_program = failing("@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @", "", tmp_path)
    scanned = []
    monkeypatch.setattr(remote, "scan_host", lambda target: scanned.append(target) or [])
    widgets.SHOWN.clear()
    assert page.connect_remote("fake")
    assert wait(lambda: "someone may be in between" in page.message.text(), 10)
    assert scanned == [] and [title for title, *_ in widgets.SHOWN] == ["Host key changed"]
    assert not (tmp_path / "known_hosts").exists()


def test_a_locked_key_offers_a_terminal_with_ssh(setup, tmp_path, monkeypatch, wait):
    page, ws, host, _ = setup
    page.ssh_program = failing("alice@fake: Permission denied (publickey).", "", tmp_path)
    opened = []
    monkeypatch.setattr(page, "open_terminal", lambda argv, title: opened.append(argv))
    assert page.connect_remote("alice@fake:2222")
    assert wait(lambda: opened, 10)
    argv = opened[0]
    assert argv[0] == "ssh" and argv[-2:] == ["--", "fake"] and "BatchMode=yes" not in argv
    assert ["-p", "2222"] == argv[argv.index("-p"):argv.index("-p") + 2] and "passphrase" in page.message.text() or \
        "ssh-agent" in page.message.text()


def test_the_choices_and_what_is_refused_before_anything_runs(setup, tmp_path, monkeypatch):
    page, ws, host, _ = setup
    page.sai.host_entries = lambda: [{"name": "local", "kind": "local"},
                                     {"name": "lab", "kind": "ssh", "ssh": "bob@lab.example", "port": 2200,
                                      "identity": "~/.ssh/lab"}]
    monkeypatch.setattr(remote, "ssh_config_hosts", lambda: ["work"])
    choices = page.remote_choices()
    assert [text for text, _ in choices] == ["lab (sandbox-ai host, bob@lab.example:2200)", "work (~/.ssh/config)"]
    assert choices[0][1] == remote.Target("lab.example", "bob", 2200, "~/.ssh/lab")
    started = []
    monkeypatch.setattr(remote, "RemoteRoot", lambda *a, **k: started.append(a))
    for bad in ("-oProxyCommand=touch /tmp/x", "a b", "host;id"):
        assert page.connect_remote(bad) is False and "Not a remote system" in page.message.text()
    assert started == []
