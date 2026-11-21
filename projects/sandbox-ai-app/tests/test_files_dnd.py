"""Drag and drop on the Files page everywhere it makes sense (user 2026-11-18: "im dateimanager drag + drop
supporten"): pane to pane and within one pane for every pairing of this machine, a sandbox's folder, a remote system
over SSH and a sandbox's folder on one of sandbox-ai's hosts (a drop copies, with Shift it moves); onto either pane's
chooser (its places and its recent folders) and onto the path bar; out to other programs (a remote system's files:
refused with a reason); other programs' files into a remote pane (an upload); onto the editor (opens the file); a drop
that cannot be made is not taken (the "no" cursor) and the status line says why; a drag ended without a drop (Esc)
leaves nothing behind. Against the fake SFTP server (fake_sftp_server.py); everything changed is below tmp_path."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent  # noqa: E402

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_host_sandboxes import ENTRIES, Hosts  # noqa: E402
from test_files_manager import drop_at, dragged, editor_of, row_point  # noqa: E402
from test_files_page import finish, select, settle, setup  # noqa: E402,F401
from test_files_remote import FAKE  # noqa: E402

KINDS = ("machine", "sandbox", "remote", "hostbox")
ACTIONS = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction


class World:
    """The page with this machine's folder HOST, the sandbox's folder WS, the remote system carol@other (SSH…, not
    one of sandbox-ai's hosts: FOLDERS["remote"]) and sandbox-ai's host lab (alice@fake) with its sandbox's folder
    FOLDERS["hostbox"] - both remote systems are the fake server, HOME tmp_path/remote."""

    def __init__(self, page, ws, host, home, wait):
        self.page, self.ws, self.host, self.home, self.wait = page, ws, host, home, wait
        self.folders = {"machine": host, "sandbox": ws, "remote": home / "plain", "hostbox": home / "lab" / "proj"}
        self.ssh = None  # the chooser's data of carol@other

    def choose(self, pane, data):
        index = pane.roots.findData(data)
        assert index >= 0, [pane.roots.itemData(i) for i in range(pane.roots.count())]
        if pane.roots.currentIndex() != index:
            pane.roots.setCurrentIndex(index)
        assert self.wait(lambda: pane.roots.currentData() == data and pane.root is not None and pane.listing is None,
                         15), self.page.message.text()
        settle(pane)

    def show(self, pane, kind, sub=""):
        """PANE shows the folder of KIND (SUB in it): chosen in its chooser, then typed in its path bar."""
        page, folder = self.page, self.folders[kind] / sub if sub else self.folders[kind]
        if kind == "machine" or kind == "sandbox" and pane is page.right:
            self.choose(pane, files_page.LOCAL)  # (the right pane: the sandbox's folder as a folder of this machine)
        elif kind == "sandbox":
            self.choose(pane, str(self.ws))
        elif kind == "remote":
            self.choose(pane, self.ssh)
        elif pane is page.left:
            self.choose(pane, files_page.host_folder("lab", str(self.folders["hostbox"])))
        else:
            self.choose(pane, "host:lab")
        pane.go_path(str(folder))
        settle(pane)
        assert pane.path_text() == str(folder), page.message.text()


@pytest.fixture
def world(setup, tmp_path, monkeypatch, wait):
    page, ws, host, _outside = setup
    home = tmp_path / "remote"
    for folder in ("plain/in", "lab/proj/in"):
        (home / folder).mkdir(parents=True)
    (host / "in").mkdir()
    (ws / "in").mkdir()
    home = home.resolve()
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    reader = Hosts()
    reader.answers = {"lab": [("run", "claude-abc", str(home / "lab" / "proj"))]}
    page.sai.host_entries = lambda: ENTRIES[:2]
    page.sai.hosts = {"lab": {"version": "0.1.99"}}
    page.sai.host_file_roots = reader
    page.ssh_program = FAKE
    page.hosts_changed()
    page.resize(1100, 700)
    page.show()
    lab = files_page.host_folder("lab", str(home / "lab" / "proj"))
    assert wait(lambda: page.left.roots.findData(lab) >= 0, 10)
    assert page.connect_remote("carol@other", page.right)
    assert wait(lambda: page.right.remote() and page.right.listing is None, 15), page.message.text()
    found = World(page, ws, host, home, wait)
    found.ssh = page.right.roots.currentData()
    yield found
    for root in page.remotes.values():
        root.close()
    page.hide()


def events(widget, mime, point=None, modifiers=Qt.KeyboardModifier.NoModifier, drop=True):
    """MIME dragged over WIDGET (at POINT: default its middle) and, with DROP, dropped there: (whether the move over it
    was taken - else the "no" cursor shows -, whether the drop was taken)."""
    point = QPointF(point if point is not None else widget.rect().center())
    enter = QDragEnterEvent(point.toPoint(), ACTIONS, mime, Qt.MouseButton.LeftButton, modifiers)
    QtWidgets.QApplication.sendEvent(widget, enter)
    move = QDragMoveEvent(point.toPoint(), ACTIONS, mime, Qt.MouseButton.LeftButton, modifiers)
    move.setAccepted(False)
    QtWidgets.QApplication.sendEvent(widget, move)
    if not drop:
        return move.isAccepted(), None
    dropped = QDropEvent(point, ACTIONS, mime, Qt.MouseButton.LeftButton, modifiers)
    dropped.setAccepted(False)
    QtWidgets.QApplication.sendEvent(widget, dropped)
    QtWidgets.QApplication.processEvents()
    return move.isAccepted(), dropped.isAccepted()


def files_of(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


# -- 1. pane to pane and within one pane, every pairing --

@pytest.mark.parametrize("source", KINDS)
def test_every_pairing_copies_and_with_shift_moves_pane_to_pane_and_within_a_pane(world, source):
    page, wait = world.page, world.wait
    left, right = page.left, page.right
    for target in KINDS:
        sub = "in" if target == source else ""  # (the same place in both panes: into a folder of it)
        into = world.folders[target] / sub if sub else world.folders[target]
        for move in (False, True):
            name = f"{source}-to-{target}{'-moved' if move else ''}.txt"
            (world.folders[source] / name).write_text(name)
            world.show(left, source)
            world.show(right, target, sub)
            taken = []
            modifiers = Qt.KeyboardModifier.ShiftModifier if move else Qt.KeyboardModifier.NoModifier
            dragged(page, left, [name], lambda mime: taken.append(drop_at(right, mime, row_point(right, "."),
                                                                          modifiers)))
            report = finish(page, wait)
            assert taken == [True], (source, target, page.message.text())
            assert wait(lambda: (into / name).exists(), 10), (source, target, report)
            assert (into / name).read_text() == name
            assert (world.folders[source] / name).exists() is not move, (source, target, move, report)
    # Within one pane: onto a folder of the folder shown.
    for move in (False, True):
        name = f"{source}-within{'-moved' if move else ''}.txt"
        (world.folders[source] / name).write_text(name)
        world.show(left, source)
        modifiers = Qt.KeyboardModifier.ShiftModifier if move else Qt.KeyboardModifier.NoModifier
        taken = []
        dragged(page, left, [name], lambda mime: taken.append(drop_at(left, mime, row_point(left, "in"), modifiers)))
        report = finish(page, wait)
        assert taken == [True] and wait(lambda: (world.folders[source] / "in" / name).exists(), 10), (source, report)
        assert (world.folders[source] / name).exists() is not move, (source, move, report)


def test_between_two_remote_systems_through_this_machine_with_progress_and_the_replace_question(world, monkeypatch):
    page, wait = world.page, world.wait
    (world.folders["remote"] / "both.txt").write_text("new")
    (world.folders["hostbox"] / "both.txt").write_text("old")
    world.show(page.left, "remote")
    world.show(page.right, "hostbox")
    page.replace.setChecked(True)
    texts = []
    real = page.start_transfer

    def started(*args, **kwargs):
        result = real(*args, **kwargs)
        texts.append(page.task.label.text())
        return result
    monkeypatch.setattr(page, "start_transfer", started)
    widgets.SHOWN.clear()
    dragged(page, page.left, ["both.txt"], lambda mime: drop_at(page.right, mime, row_point(page.right, ".")))
    assert wait(lambda: texts and not page.task.running, 15)
    assert any(title == "Replace existing files?" for title, *_ in widgets.SHOWN), widgets.SHOWN
    assert "through this machine" in texts[0], texts
    assert (world.folders["hostbox"] / "both.txt").read_text() == "new"


# -- 2. onto either chooser: its places and its recent folders --

def chooser_menu(page, monkeypatch, pick):
    """Every menu the page shows is answered by PICK(menu) (the action triggered, or None: closed with Esc)."""
    shown = []

    def answer(menu, where):
        shown.append(menu)
        action = pick(menu)
        if action is not None:
            action.trigger()
        return action
    monkeypatch.setattr(page, "exec_menu", answer)
    return shown


def entries(menu):
    found = []
    for action in menu.actions():
        if action.menu() is not None:
            found += [(action.text(), *entry) for entry in entries(action.menu())]
        elif action.text():
            found.append((action.text(), action.isEnabled()))
    return found


def by_text(menu, start):
    for action in menu.actions():
        if action.menu() is not None:
            found = by_text(action.menu(), start)
            if found is not None:
                return found
        elif action.text().startswith(start) and action.isEnabled():
            return action
    return None


def test_a_drop_onto_a_chooser_offers_its_places_and_recent_folders_and_goes_there(world, monkeypatch):
    page, wait, home = world.page, world.wait, world.home
    left, right = page.left, page.right
    world.show(left, "sandbox")
    world.show(right, "machine", "in")
    world.show(right, "machine")  # (host/in: a recent folder of the right pane now)
    widgets.SHOWN.clear()
    picks = iter(["Host lab", str(world.host / "in"), None])
    shown = chooser_menu(page, monkeypatch, lambda menu: (lambda wanted: wanted and by_text(menu, wanted))(next(picks)))
    # Dragged from the left pane onto the right pane's chooser: a menu of where it goes; Host lab is not connected yet -
    # it is, the pane shows its HOME, and the file goes there (asked first: one of sandbox-ai's hosts).
    taken = []

    def onto_chooser(mime):
        moved, _ = events(right.roots, mime, drop=False)
        assert moved and "choose where" in page.message.text().lower(), page.message.text()
        taken.append(events(right.roots, mime)[1])
    dragged(page, left, ["a.txt"], onto_chooser)
    assert taken == [True] and shown
    texts = [text for text, *_ in entries(shown[0])]
    assert any(text.startswith("This machine") for text in texts), texts
    assert any(text.startswith("Host lab") for text in texts), texts
    assert any(text.startswith("Remote system") for text in texts), texts
    assert any(str(world.host / "in") in text for text in texts), "a recent folder of the pane"
    assert wait(lambda: (home / "a.txt").exists() and not page.task.running, 15), page.message.text()
    assert right.remote() and right.path_text() == str(home)
    assert any(title.startswith("Copy to lab") for title, *_ in widgets.SHOWN), widgets.SHOWN
    assert (world.ws / "a.txt").exists(), "a drop copies"
    # With Shift onto the chooser again, a recent folder chosen: moved there, and the pane shows it.
    dragged(page, left, ["a.txt"], lambda mime: events(right.roots, mime, modifiers=Qt.KeyboardModifier.ShiftModifier))
    assert wait(lambda: (world.host / "in" / "a.txt").exists() and not page.task.running, 15), page.message.text()
    assert not (world.ws / "a.txt").exists() and right.path_text() == str(world.host / "in")
    # The menu closed with Esc: nothing done, nothing waits for a place either.
    (world.ws / "c.txt").write_text("c")
    left.list()
    dragged(page, left, ["c.txt"], lambda mime: events(left.roots, mime))
    assert len(shown) == 3 and not page.task.running and left.pending_drop is None
    assert "nothing" in page.message.text().lower(), page.message.text()


def test_a_drop_onto_the_path_bar_goes_into_the_folder_shown(world):
    page, wait = world.page, world.wait
    left, right = page.left, page.right
    world.show(left, "sandbox")
    world.show(right, "remote", "in")
    taken = []

    def onto_path(mime):
        moved, dropped = events(right.path, mime)
        taken.append((moved, dropped))
    dragged(page, left, ["a.txt"], onto_path)
    assert taken == [(True, True)]
    assert wait(lambda: (world.folders["remote"] / "in" / "a.txt").exists() and not page.task.running, 15)
    assert right.path.text() == str(world.folders["remote"] / "in"), "the text of the path bar stays a path"
    # Text dragged onto it (not files): typed in, as before.
    mime = QMimeData()
    mime.setText("/tmp")
    moved, _ = events(right.path, mime, drop=False)
    assert moved and not page.message.text().startswith("Drop:")


# -- 4. out to other programs; 5. other programs into a remote pane --

def test_a_remote_systems_files_go_to_no_other_program_and_the_status_line_says_why(world):
    page = world.page
    world.show(page.left, "remote")
    (world.folders["remote"] / "r.txt").write_text("r")
    page.left.list()
    mime = dragged(page, page.left, ["r.txt"], lambda mime: None)
    assert not mime.hasUrls()
    assert "remote" in page.message.text().lower() and "copy" in page.message.text().lower(), page.message.text()


def test_other_programs_files_dropped_into_a_remote_pane_are_uploaded(world, tmp_path):
    page, wait = world.page, world.wait
    mine = tmp_path / "desktop"
    mine.mkdir()
    (mine / "up.txt").write_text("uploaded")
    (mine / "dir").mkdir()
    (mine / "dir" / "inner.txt").write_text("inner")
    world.show(page.right, "remote")
    widgets.SHOWN.clear()
    moved, dropped = events(page.right.table.viewport(), files_of(mine / "up.txt", mine / "dir"))
    assert moved and dropped
    assert wait(lambda: (world.folders["remote"] / "dir" / "inner.txt").exists() and not page.task.running, 15)
    assert (world.folders["remote"] / "up.txt").read_text() == "uploaded"
    assert widgets.SHOWN == [], "a remote system of the human's: nothing to ask"
    # Into one of sandbox-ai's hosts: asked first, naming them.
    world.show(page.right, "hostbox")
    moved, dropped = events(page.right.table.viewport(), files_of(mine / "up.txt"))
    assert dropped and wait(lambda: (world.folders["hostbox"] / "up.txt").exists() and not page.task.running, 15)
    assert any(title == "Copy files of another program to alice@fake?" for title, *_ in widgets.SHOWN)


def test_out_of_a_sandboxs_folder_the_status_line_says_what_other_programs_do_not_get(world):
    page = world.page
    world.show(page.left, "sandbox")
    mime = dragged(page, page.left, ["a.txt", "run.sh", "sub"], lambda mime: None)
    assert [os.path.basename(url.toLocalFile()) for url in mime.urls()] == ["a.txt"]
    assert "1 of the 3" in page.message.text(), page.message.text()


# -- 6. onto the editor --

def test_a_file_dropped_onto_the_editor_opens_in_one_of_its_own(world, tmp_path):
    page, wait = world.page, world.wait
    left = page.left
    world.show(left, "sandbox")
    window = page.open_editor(left.root, "a.txt", str(world.ws / "a.txt"))
    window = editor_of(page, wait)
    before = len(page.editors)
    # From the pane: sub/b.txt dragged onto the text.
    left.go("sub")
    settle(left)
    taken = []
    dragged(page, left, ["b.txt"], lambda mime: taken.append(events(window.text.viewport(), mime)))
    assert taken == [(True, True)] and len(page.editors) == before + 1
    opened = editor_of(page, wait)
    assert opened.relative == "sub/b.txt" and opened.text.toPlainText() == "beta"
    assert window.text.toPlainText() == "alpha", "nothing typed into the file it was dropped on"
    # From another program: a local file.
    (tmp_path / "other.txt").write_text("other program's")
    assert events(window.text.viewport(), files_of(tmp_path / "other.txt")) == (True, True)
    assert wait(lambda: len(page.editors) == before + 2, 10)
    assert editor_of(page, wait).text.toPlainText() == "other program's"
    # A folder: not opened, the editor says why.
    left.up()
    settle(left)
    dragged(page, left, ["sub"], lambda mime: taken.append(events(window.text.viewport(), mime)))
    assert taken[-1] == (False, False) and len(page.editors) == before + 2
    assert "folder" in window.notice.text().lower(), window.notice.text()
    for each in list(page.editors):
        each.close()


# -- 7. a drop that cannot be made: not taken, and the status line says why --

def test_a_refused_drop_is_not_taken_and_the_status_line_says_why(world, monkeypatch):
    page = world.page
    left, right = page.left, page.right
    world.show(left, "sandbox")
    world.show(right, "machine")
    said = []

    def try_drop(pane, point):
        def drop(mime):
            moved, dropped = events(pane.table.viewport(), mime, point(pane))
            said.append((moved, dropped, page.message.text()))
        return drop
    dragged(page, left, ["a.txt"], try_drop(left, lambda pane: row_point(pane, ".")))
    dragged(page, left, ["sub"], try_drop(left, lambda pane: row_point(pane, "sub")))
    assert [s[:2] for s in said] == [(False, False), (False, False)], said
    assert "already in this folder" in said[0][2] and "into itself" in said[1][2], said
    forged = QMimeData()
    forged.setData(files_page.MIME, b"0" * 32)
    assert events(right.table.viewport(), forged)[:2] == (False, False)
    assert "another window" in page.message.text(), page.message.text()
    web = QMimeData()
    web.setUrls([QUrl("https://example.org/x")])
    assert events(right.table.viewport(), web) == (False, False)
    assert "files of this machine" in page.message.text(), page.message.text()
    monkeypatch.setattr(page.task, "call", object())  # a job runs
    assert events(right.table.viewport(), files_of(world.ws / "a.txt")) == (False, False)
    assert "running" in page.message.text(), page.message.text()
    assert right.table.drop_row is None, "no frame left behind"


def test_the_drop_target_is_framed_while_dragged_over(world):
    page = world.page
    world.show(page.left, "sandbox")
    world.show(page.right, "machine")
    seen = []

    def over(mime):
        events(page.right.table.viewport(), mime, row_point(page.right, "in"), drop=False)
        seen.append(page.right.table.drop_row)
        events(page.right.path, mime, drop=False)
        seen.append(page.right.path.property("dropTarget"))
        QtWidgets.QApplication.sendEvent(page.right.path, QDragLeaveEvent())
        seen.append(page.right.path.property("dropTarget"))
        events(page.right.roots, mime, drop=False)
        seen.append(page.right.roots.property("dropTarget"))
        QtWidgets.QApplication.sendEvent(page.right.roots, QDragLeaveEvent())
        seen.append(page.right.roots.property("dropTarget"))
    dragged(page, page.left, ["a.txt"], over)
    assert seen[0] == widgets.find_row(page.right.table, "in")
    assert seen[1:] == [True, False, True, False], seen


# -- 8. Esc ends a drag: nothing is left behind --

def test_a_drag_ended_with_esc_leaves_nothing_behind(world, monkeypatch):
    page = world.page
    left, right = page.left, page.right
    world.show(left, "sandbox")
    world.show(right, "machine")
    select(left, "a.txt")

    def escaped(drag):
        # As the window system does on Esc: over the other pane, then left - no drop.
        events(right.table.viewport(), drag.mimeData(), row_point(right, "in"), drop=False)
        assert right.table.drop_row is not None
        QtWidgets.QApplication.sendEvent(right.table.viewport(), QDragLeaveEvent())
        return Qt.DropAction.IgnoreAction
    monkeypatch.setattr(page, "exec_drag", escaped)
    assert page.start_drag(left) == Qt.DropAction.IgnoreAction
    QtWidgets.QApplication.processEvents()
    assert page.drag is None and right.table.drop_row is None and not page.task.running
    assert not (world.host / "a.txt").exists() and not (world.host / "in" / "a.txt").exists()
    assert "nothing was copied or moved" in page.message.text().lower(), page.message.text()


# -- 9. the keys stay --

def test_copy_and_paste_f5_and_f6_still_work_beside_drag_and_drop(world):
    from test_files_page import press
    page, wait = world.page, world.wait
    world.show(page.left, "sandbox")
    world.show(page.right, "machine")
    select(page.left, "a.txt")
    press(page.left, Qt.Key.Key_F5)
    finish(page, wait)
    assert (world.host / "a.txt").read_text() == "alpha"
    select(page.right, "a.txt")
    press(page.right, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert page.paste(page.right, page.right.relative + "/in")
    finish(page, wait)
    assert (world.host / "in" / "a.txt").exists()
    (world.ws / "a.txt").unlink()
    page.left.list()
    select(page.right, "a.txt")
    press(page.right, Qt.Key.Key_F6)
    finish(page, wait)
    assert not (world.host / "a.txt").exists() and (world.ws / "a.txt").read_text() == "alpha"


# -- 10. the help --

def test_the_help_says_where_a_drop_goes_in_english_and_german():
    from sandbox_ai_app import help_content
    body = help_content.BY_ID["page-files"].body
    english, german = body.en, body.de
    for text, words in ((english, ("onto a pane's chooser a menu asks where", "Onto the path field", "Dropped onto "
                                   "the editor", "Esc ends a drag", "a remote system's files not at all",
                                   "also onto a remote system (an upload)", "the status line says why")),
                        (german, ("auf der Auswahl eines Bereichs fragt ein Menü", "Auf dem Pfadfeld", "Auf dem "
                                  "Editor abgelegt", "Esc beendet ein Ziehen", "eines entfernten Systems gar nicht",
                                  "auch auf ein entferntes System (ein Hochladen)", "die Statuszeile sagt, warum"))):
        flat = " ".join(text.split())
        for words_ in words:
            assert words_ in flat, words_


# -- e2e: the real window, the mouse --

def test_end_to_end_a_drag_with_the_mouse_copies_and_moves_both_ways_in_the_window(qapp, tmp_path, monkeypatch, wait):
    """The real window: a press on a selected row and a move further than the drag distance start the drag (the window
    system's drag is replaced by its drop on the other pane's list); copied to a host's folder, then moved back."""
    from PySide6.QtTest import QTest
    from test_ui import env as _env, fake_run  # noqa: F401
    from test_files_hosts import HOSTS
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
        page.right.roots.setCurrentIndex(page.right.roots.findData("host:lab"))
        assert wait(lambda: page.right.remote() and page.right.listing is None, 15), page.message.text()
        settle(page.left, page.right)

        def drag_with_mouse(source, target, name, modifiers=Qt.KeyboardModifier.NoModifier):
            table = source.table
            settle(source)
            widgets.select_keys(table, [name])
            point = row_point(source, name)

            def delivered(drag):
                events(target.table.viewport(), drag.mimeData(), row_point(target, "."), modifiers)
                return Qt.DropAction.CopyAction
            monkeypatch.setattr(page, "exec_drag", delivered)
            QTest.mousePress(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
            QTest.mouseMove(table.viewport(), point + QPoint(0, QtWidgets.QApplication.startDragDistance() + 20))
            QTest.mouseRelease(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        drag_with_mouse(page.left, page.right, "notes.txt")
        assert wait(lambda: (remote_home / "notes.txt").exists() and not page.task.running, 15), page.message.text()
        assert any(title == "Copy to lab (alice@fake)?" for title, *_ in widgets.SHOWN)
        (project / "notes.txt").unlink()
        page.left.list()
        page.right.list()
        drag_with_mouse(page.right, page.left, "notes.txt", Qt.KeyboardModifier.ShiftModifier)
        assert wait(lambda: (project / "notes.txt").exists() and not page.task.running, 15), page.message.text()
        assert not (remote_home / "notes.txt").exists() and (project / "notes.txt").read_text() == "project data"
    finally:
        for root in window.files.remotes.values():
            root.close()
        window.close()
