"""Hosts in either pane of the Files page (user request 2026-11-16: "switch a listing to another host, e.g. to copy
project data to another host"): each pane chooses where it lists - this machine, one of sandbox-ai's hosts by name
(`sandbox-ai hosts`, connected over SSH as SSH… connects), or any other system with SSH… -, both panes may show remote
systems at once, and copies and moves go every way: this machine -> host, host -> this machine, host -> host (through
this machine: remote.relay). Against the fake SFTP server (fake_sftp_server.py; every "host" is served from tmp_path);
everything changed is below tmp_path."""
import os
import stat

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import files, files_page, remote, state as app_state, widgets  # noqa: E402
from test_files_manager import drop_at, dragged, row_point, shown  # noqa: E402
from test_files_page import finish, names, press, select, settle, setup  # noqa: E402,F401
from test_files_remote import FAKE, failing  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402

HOSTS = [{"name": "local", "kind": "local"},
         {"name": "lab", "kind": "ssh", "ssh": "alice@fake"},
         {"name": "farm", "kind": "ssh", "ssh": "bob@farm"}]


def choose(pane, data, wait=None):
    """Choose DATA in PANE's chooser (a host by name connects first: waited for with WAIT)."""
    index = pane.roots.findData(data)
    assert index >= 0, [pane.roots.itemData(i) for i in range(pane.roots.count())]
    pane.roots.setCurrentIndex(index)
    if wait is not None:
        assert wait(lambda: pane.roots.currentData() == data and pane.root is not None and pane.listing is None,
                    15), pane.page.message.text()
    settle(pane)


@pytest.fixture
def hosts(setup, tmp_path, monkeypatch, wait):
    """The page with two of sandbox-ai's hosts (lab and farm: both the fake server, HOME tmp_path/remote) known."""
    page, ws, host, outside = setup
    home = tmp_path / "remote"
    for folder in ("lab", "farm"):
        (home / folder).mkdir(parents=True)
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    page.sai.host_entries = lambda: HOSTS
    page.sai.hosts = {"lab": {"version": "0.1.97"}, "farm": {"version": "0.1.97"}}
    page.ssh_program = FAKE
    page.refresh_roots()
    page.right.refresh_roots()
    assert wait(lambda: not page.roots_reading and page.left.roots.findData("host:lab") >= 0, 10)
    settle(page.left, page.right)
    yield page, home.resolve(), ws, host
    for root in page.remotes.values():
        root.close()


def places(pane):
    return [(pane.roots.itemText(i), pane.roots.itemData(i)) for i in range(pane.roots.count())
            if pane.roots.itemData(i) in (files_page.LOCAL, "host:lab", "host:farm")]


# -- where a pane lists --

def test_both_panes_offer_this_machine_and_sandbox_ais_hosts_by_name(hosts):
    page, home, ws, host = hosts
    for pane in (page.left, page.right):
        assert places(pane) == [("This machine", files_page.LOCAL), ("Host farm (bob@farm)", "host:farm"),
                                ("Host lab (alice@fake)", "host:lab")]
        model = pane.roots.model()
        assert all(model.item(pane.roots.findData(data)).isEnabled() for _text, data in places(pane))
        texts = [button.text() for button in pane.findChildren(QtWidgets.QPushButton)]
        assert "SSH…" in texts, texts
    assert page.right.roots.currentData() == files_page.LOCAL and page.right.mode() == "host"
    assert page.left.mode() == "sandbox" and "not available here yet" not in " ".join(
        page.left.roots.itemText(i) for i in range(page.left.roots.count()))
    # The pane's buttons on its context menu too (the project's rule): where it lists, SSH….
    for pane in (page.left, page.right):
        menu = page.menu(pane, at_row=False)
        entries = [text for text, _ in widgets.menu_entries(menu)]
        assert "Show in this pane" in entries and "  This machine" in entries and "  Host lab (alice@fake)" in \
            entries and "  SSH…" in entries, entries


def test_a_pane_switched_to_a_host_starts_in_its_home_and_comes_back(hosts, wait):
    page, home, ws, host = hosts
    left, right = page.left, page.right
    before = (left.root, left.relative)
    choose(right, "host:lab", wait)
    assert right.remote() and right.mode() == "remote" and right.path_text() == str(home)
    assert right.root.agents and right.untrusted(), "one of sandbox-ai's hosts: its files are an agent's"
    assert not right.agents_box.isHidden() and right.agents_box.isChecked()
    assert " free of " in right.space.text()
    assert (left.root, left.relative) == before, "the other pane stays where it was"
    assert page.host_keys["lab"] in page.remotes
    # Back to this machine: the folder shown before.
    choose(right, files_page.LOCAL)
    assert not right.remote() and right.path_text() == str(host) and right.agents_box.isHidden()
    # Chosen again: the same connection, no second one.
    root = page.remotes[page.host_keys["lab"]]
    choose(right, "host:lab", wait)
    assert right.root is root and len(page.remotes) == 1
    # The left pane too - both panes on remote systems at once, the same connection shared.
    choose(left, "host:lab", wait)
    assert left.root is right.root and left.path_text() == str(home)
    choose(left, "host:farm", wait)
    assert left.root is not right.root and len(page.remotes) == 2 and left.root.label == "bob@farm"
    # This machine in the left pane: the human's own (not an agent's), from HOME.
    choose(left, files_page.LOCAL)
    assert left.mode() == "host" and not left.untrusted() and left.root is page.local
    assert left.path_text() == os.path.realpath(os.path.expanduser("~"))


def test_free_space_a_remote_folder_cannot_tell_is_said_unknown(hosts, wait, monkeypatch):
    page, home, ws, host = hosts
    monkeypatch.setenv("FAKE_SFTP_NO_EXTENSIONS", "1")
    choose(page.right, "host:lab", wait)
    assert page.right.space.text() == "Free space: not known (the SFTP server does not tell)"


def test_an_unknown_host_key_is_trusted_only_after_a_yes_also_for_the_right_pane(setup, tmp_path, monkeypatch, wait):
    page, ws, host, _ = setup
    known = tmp_path / "known_hosts"
    serve = " ".join(FAKE)
    page.ssh_program = failing("Host key verification failed.",
                               f"grep -q fake '{known}' 2>/dev/null && exec {serve}", tmp_path)
    page.known_hosts = str(known)
    page.sai.host_entries = lambda: HOSTS
    page.sai.hosts = {"lab": {}}
    page.right.refresh_roots()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(tmp_path))
    monkeypatch.setattr(remote, "scan_host", lambda target: ["fake ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA"])
    monkeypatch.setattr(remote, "fingerprints", lambda lines: ["ED25519 SHA256:abcdef"])
    widgets.AUTO_ANSWER = False
    widgets.SHOWN.clear()
    page.right.roots.setCurrentIndex(page.right.roots.findData("host:lab"))
    assert wait(lambda: widgets.SHOWN, 10)
    assert widgets.SHOWN[0][0] == "Trust this host?"
    assert wait(lambda: "not trusted" in page.message.text(), 5) and not known.exists() and not page.remotes
    assert page.right.roots.currentData() == files_page.LOCAL and not page.right.remote(), "it stays where it was"
    widgets.AUTO_ANSWER = True
    page.right.roots.setCurrentIndex(page.right.roots.findData("host:lab"))
    assert wait(lambda: page.right.remote() and page.right.listing is None, 10), page.message.text()
    assert known.exists() and page.right.path_text() == str(tmp_path.resolve())
    for root in page.remotes.values():
        root.close()


def test_ssh_in_the_right_pane_shows_the_system_there(setup, tmp_path, monkeypatch, wait):
    page, ws, host, _ = setup
    monkeypatch.setenv("FAKE_SFTP_HOME", str(tmp_path))
    page.ssh_program = FAKE
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem", lambda *a, **k: ("carol@plain", True))
    assert page.remote_dialog(page.right)
    assert wait(lambda: page.right.remote() and page.right.listing is None, 10), page.message.text()
    assert page.right.roots.currentText() == "Remote system carol@plain" and not page.right.root.agents
    assert page.left.root.path == str(ws), "the left pane stays"
    assert page.left.roots.findData(page.right.roots.currentData()) >= 0, "offered in the other pane too"
    for root in page.remotes.values():
        root.close()


# -- copies and moves every way --

def test_this_machine_to_a_host_asks_first_naming_what_goes_there(hosts, wait):
    """The use case: a project folder of this machine copied to one of sandbox-ai's hosts."""
    page, home, ws, host = hosts
    left, right = page.left, page.right
    project = host / "proj"
    project.mkdir()
    (project / "a.txt").write_text("alpha")
    (project / "build.sh").write_text("#!/bin/sh\n")
    (project / "build.sh").chmod(0o755)
    right.list()
    choose(left, "host:lab", wait)
    left.go_path(str(home / "lab"))
    settle(left)
    select(right, "proj")
    widgets.AUTO_ANSWER = False
    widgets.SHOWN.clear()
    page.copy_selected(right)
    assert [title for title, *_ in widgets.SHOWN] == ["Copy to lab (alice@fake)?"]
    title, text, details = widgets.SHOWN[0]
    assert "sandboxes run" in text and str(project) in details
    assert wait(lambda: not page.task.running, 5) and not (home / "lab" / "proj").exists(), "No: nothing copied"
    widgets.AUTO_ANSWER = True
    assert page.copy_selected(right)
    finish(page, wait)
    assert (home / "lab" / "proj" / "a.txt").read_text() == "alpha"
    assert os.access(home / "lab" / "proj" / "build.sh", os.X_OK), "the human's own files keep their bits"
    assert names(left) == ["proj"]


def test_a_host_to_this_machine_comes_as_an_agents_files(hosts, wait):
    page, home, ws, host = hosts
    right = page.right
    (home / "lab" / "tool.sh").write_text("#!/bin/sh\n")
    (home / "lab" / "tool.sh").chmod(0o4755)
    (home / "lab" / ".envrc").write_text("export X=1\n")
    choose(right, "host:lab", wait)
    right.go_path(str(home / "lab"))
    choose(page.left, files_page.LOCAL)
    page.left.go_path(str(host))
    settle(page.left, right)
    select(right, "tool.sh", ".envrc")
    widgets.SHOWN.clear()
    assert page.copy_selected(right)
    report = "\n".join(finish(page, wait))
    mode = os.stat(host / "tool.sh").st_mode
    assert not mode & 0o111 and not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    assert "Copied without the executable bit" in report and "Careful" in report and ".envrc" in report
    assert not [title for title, *_ in widgets.SHOWN if title.startswith("Copy to")], "nothing goes to a host"


def test_host_to_host_goes_through_this_machine_and_leaves_nothing_there(hosts, wait, tmp_path):
    page, home, ws, host = hosts
    left, right = page.left, page.right
    proj = home / "lab" / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("print(1)\n")
    (proj / "run.sh").write_text("#!/bin/sh\n")
    (proj / "run.sh").chmod(0o755)
    (proj / ".envrc").write_text("export X=1\n")
    (proj / "link").symlink_to(host)
    choose(left, "host:lab", wait)
    left.go_path(str(home / "lab"))
    choose(right, "host:farm", wait)
    right.go_path(str(home / "farm"))
    settle(left, right)
    select(left, "proj")
    widgets.SHOWN.clear()
    seen = []
    page.follow = (lambda real: lambda started, progress, text: seen.append(text) or real(started, progress, text))(
        page.follow)
    assert page.copy_selected(left)
    report = "\n".join(finish(page, wait))
    assert [title for title, *_ in widgets.SHOWN if title.startswith("Copy to")] == ["Copy to farm (bob@farm)?"]
    assert "through this machine" in seen[0], seen
    copied = home / "farm" / "proj"
    assert (copied / "src" / "main.py").read_text() == "print(1)\n"
    assert not os.access(copied / "run.sh", os.X_OK), "from a host of sandbox-ai's: an agent's, also on another host"
    assert not (copied / "link").exists() and not (copied / "link").is_symlink(), "a link is never copied"
    assert "symbolic link" in report and "run.sh" in report
    assert not [name for name in os.listdir(home / "farm") if name != "proj"], "no temporary names left there"
    assert not [name for name in os.listdir(os.environ.get("TMPDIR") or "/tmp") if "relay" in name], \
        "the staging folder on this machine is gone"
    # A move: the source goes once the copy is complete (its link stays: it was not copied).
    (home / "lab" / "notes").mkdir()
    (home / "lab" / "notes" / "n.txt").write_text("n")
    left.list()
    select(left, "notes")
    assert page.copy_selected(left, move=True)
    finish(page, wait)
    assert (home / "farm" / "notes" / "n.txt").read_text() == "n" and not (home / "lab" / "notes").exists()


def test_host_to_host_replacing_asks_and_cancel_leaves_nothing(hosts, wait, monkeypatch):
    page, home, ws, host = hosts
    left, right = page.left, page.right
    (home / "lab" / "a.txt").write_text("new")
    (home / "farm" / "a.txt").write_text("old")
    choose(left, "host:lab", wait)
    left.go_path(str(home / "lab"))
    choose(right, "host:farm", wait)
    right.go_path(str(home / "farm"))
    settle(left, right)
    page.replace.setChecked(True)
    select(left, "a.txt")
    answers = iter([True, False])  # yes to "Copy to farm?", no to "Replace existing files?"
    monkeypatch.setattr(files_page, "confirm", lambda *a, **k: next(answers))
    monkeypatch.setattr(files_page, "ask_first", lambda *a, **k: next(answers))
    page.copy_selected(left)
    assert wait(lambda: not page.task.running, 10)
    assert (home / "farm" / "a.txt").read_text() == "old", "not replaced without a yes"
    # Cancelled while it runs: nothing of it on farm, nothing staged here.
    monkeypatch.undo()
    widgets.AUTO_ANSWER = True
    page.replace.setChecked(False)
    (home / "lab" / "big").mkdir()
    for index in range(40):
        (home / "lab" / "big" / f"{index}.bin").write_bytes(b"x" * 3000)
    real = remote._fetch

    def slow(*args, **kwargs):
        import time
        time.sleep(0.03)
        return real(*args, **kwargs)
    monkeypatch.setattr(remote, "_fetch", slow)
    left.list()
    select(left, "big")
    assert page.copy_selected(left)
    assert wait(lambda: page.progress is not None and page.progress.count > 2, 10)
    page.task.cancel()
    report = finish(page, wait)
    assert any("Cancelled" in line for line in report), report
    assert sorted(os.listdir(home / "farm")) == ["a.txt"]
    assert not [name for name in os.listdir(os.environ.get("TMPDIR") or "/tmp") if "relay" in name]


def test_drag_and_drop_and_the_clipboard_between_two_hosts(hosts, wait):
    page, home, ws, host = hosts
    left, right = page.left, page.right
    (home / "lab" / "d.txt").write_text("dragged")
    (home / "lab" / "c.txt").write_text("clipped")
    choose(left, "host:lab", wait)
    left.go_path(str(home / "lab"))
    choose(right, "host:farm", wait)
    right.go_path(str(home / "farm"))
    shown(page)
    taken = []
    dragged(page, left, ["d.txt"], lambda mime: taken.append(drop_at(right, mime, row_point(right, "."))))
    finish(page, wait)
    assert taken == [True] and (home / "farm" / "d.txt").read_text() == "dragged"
    select(left, "c.txt")
    press(left, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    page.activate(right)
    assert page.paste(right)
    finish(page, wait)
    assert (home / "farm" / "c.txt").read_text() == "clipped"
    # A copy beside the original on the same host (pasted where it was copied): through this machine too.
    assert page.paste(left)
    assert wait(lambda: (home / "lab" / "c (copy).txt").exists() and not page.task.running, 10), \
        os.listdir(home / "lab")
    assert (home / "lab" / "c (copy).txt").read_text() == "clipped"


def test_within_one_host_across_the_panes_a_move_is_a_rename(hosts, wait):
    page, home, ws, host = hosts
    left, right = page.left, page.right
    (home / "lab" / "in").mkdir()
    (home / "lab" / "m.txt").write_text("m")
    choose(left, "host:lab", wait)
    left.go_path(str(home / "lab"))
    choose(right, "host:lab", wait)
    right.go_path(str(home / "lab" / "in"))
    settle(left, right)
    select(left, "m.txt")
    widgets.SHOWN.clear()
    assert page.copy_selected(left, move=True)
    finish(page, wait)
    assert (home / "lab" / "in" / "m.txt").read_text() == "m" and not (home / "lab" / "m.txt").exists()
    assert not [title for title, *_ in widgets.SHOWN if title.startswith("Copy to")], "within the host: not asked"


# -- Recent: per host --

def test_recent_keeps_folders_per_host_and_goes_back_to_one(hosts, wait, tmp_path):
    page, home, ws, host = hosts
    right = page.right
    choose(right, "host:lab", wait)
    right.go_path(str(home / "lab"))
    settle(right)
    assert right.recent[0] == ("host:lab", str(home / "lab").lstrip("/"))
    texts = [text for text, _key in right.recent_entries()]
    assert f"lab:{home}" in texts and str(host) in texts, texts
    choose(right, files_page.LOCAL)
    entry = next(key for text, key in right.recent_entries() if text == f"lab:{home / 'lab'}")
    right.go_recent(entry)
    assert wait(lambda: right.remote() and right.path_text() == str(home / "lab") and right.listing is None, 10)
    # Kept across restarts (the host pane's list in the app's state), a host's entries with it.
    kept = files_page.Pane.kept_recent()
    if app_state.current() is not None:
        assert ("host:lab", str(home / "lab").lstrip("/")) in kept


# -- remote.relay itself --

def test_relay_copies_between_two_connections_and_cleans_up(tmp_path, monkeypatch):
    from test_files_review_p1 import connect
    source_dir, target_dir = tmp_path / "s", tmp_path / "t"
    (source_dir / "f").mkdir(parents=True)
    target_dir.mkdir()
    (source_dir / "f" / "x.sh").write_text("x")
    (source_dir / "f" / "x.sh").chmod(0o755)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", None)
    one, two = connect(source_dir), connect(target_dir)
    try:
        result = files.copy_entry(one, "f", two, "", untrusted=True)
        assert (target_dir / "f" / "x.sh").read_text() == "x" and not os.access(target_dir / "f" / "x.sh", os.X_OK)
        assert result.files == 1 and any("x.sh" in path for path in result.executable)
        result = files.copy_entry(one, "f", two, "", untrusted=False, name="g")
        assert os.access(target_dir / "g" / "x.sh", os.X_OK), "the human's own: as it is"
        with pytest.raises(files.Refused):
            files.copy_entry(one, "f", two, "")  # (exists: refused, nothing changed)
        with pytest.raises(files.Cancelled):
            files.copy_entry(one, "f", two, "", name="h", cancelled=lambda: True)
        assert sorted(os.listdir(target_dir)) == ["f", "g"] and os.listdir(tmp_path / "tmp") == []
        result = files.move_entry(one, "f", two, "", overwrite=True)
        assert result.moved and not (source_dir / "f").exists()
        # A folder without its write bit: copied (folders keep their owner's bits), and the staging folder here is
        # still removed. (Checked as it should be by a user other than root.)
        (source_dir / "ro" / "in").mkdir(parents=True)
        (source_dir / "ro" / "in" / "x").write_text("x")
        for folder in (source_dir / "ro" / "in", source_dir / "ro"):
            folder.chmod(0o555)
        try:
            files.copy_entry(one, "ro", two, "")
            assert (target_dir / "ro" / "in" / "x").read_text() == "x"
            assert os.listdir(tmp_path / "tmp") == [], "nothing staged left behind"
        finally:
            for folder in (source_dir / "ro", source_dir / "ro" / "in", target_dir / "ro", target_dir / "ro" / "in"):
                if folder.exists():
                    folder.chmod(0o755)
    finally:
        one.close()
        two.close()


# -- end to end: the real page in the app's window, by the menu and the keys --

def test_end_to_end_a_project_copied_to_a_host_from_the_window(qapp, tmp_path, monkeypatch, wait):
    """The user's case in the real window: the right pane switched to one of sandbox-ai's hosts through its context
    menu (Show in this pane), the project's folder copied there with F5 - asked first -, then back with F6."""
    from PySide6.QtTest import QTest
    from test_ui import env as _env, fake_run  # noqa: F401
    from sandbox_ai_app import backend, terminal
    from sandbox_ai_app.ui import MainWindow
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    fake_run(tmp_path)
    project = (tmp_path / "project").resolve()
    (project / "notes.txt").write_text("project data")
    remote_home = tmp_path / "lab-home"
    remote_home.mkdir()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(remote_home))
    window = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if window.tray is not None:
        window.tray.hide()
        window.tray = None
    try:
        window.sai.host_entries = lambda: HOSTS[:2]
        window.sai.host_statuses = lambda refresh=False, cached=True: {"lab": {"version": "0.1.98"}}
        window.resize(1400, 900)
        window.show()
        window.show_page("files")
        page = window.files
        page.ssh_program = FAKE
        page.refresh_roots()
        page.right.refresh_roots()
        assert wait(lambda: not page.roots_reading and page.left.roots.findData(str(project)) >= 0, 10)
        page.left.roots.setCurrentIndex(page.left.roots.findData(str(project)))
        settle(page.left, page.right)
        # The right pane's context menu: Show in this pane > Host lab.
        menu = page.menu(page.right, at_row=False)
        where = next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == "Show in this pane")
        next(a for a in where.actions() if a.text().startswith("Host lab")).trigger()
        assert wait(lambda: page.right.remote() and page.right.listing is None, 15), page.message.text()
        assert page.right.path_text() == str(remote_home.resolve())
        # F5 on the project's notes in the left pane: asked, then copied to the host.
        table = page.left.table
        table.setFocus()
        table.setCurrentCell(widgets.find_row(table, "notes.txt"), 0)
        widgets.select_keys(table, ["notes.txt"])
        QTest.keyClick(table, Qt.Key.Key_F5)
        assert wait(lambda: (remote_home / "notes.txt").exists() and not page.task.running, 15), page.message.text()
        assert any(title == "Copy to lab (alice@fake)?" for title, *_ in widgets.SHOWN)
        assert (remote_home / "notes.txt").read_text() == "project data"
        # And back with F6 from the host into the project (a sandbox's folder: the agent's, as always).
        settle(page.right)
        page.right.table.setFocus()
        widgets.select_keys(page.right.table, ["notes.txt"])
        (project / "notes.txt").unlink()
        page.left.list()
        settle(page.left)
        QTest.keyClick(page.right.table, Qt.Key.Key_F6)
        assert wait(lambda: (project / "notes.txt").exists() and not page.task.running, 15), page.message.text()
        assert not (remote_home / "notes.txt").exists()
    finally:
        for root in window.files.remotes.values():
            root.close()
        window.close()
