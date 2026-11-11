"""The two-pane Files page (files_page.py): a sandbox's folder on the left, this machine on the right, keys, copies
and moves between the panes, delete, rename, new folder, permissions and owners. Qt (CI: no libEGL everywhere).
Everything the tests change is below tmp_path; the host pane starts in the test session's HOME (home_guard)."""
import os
import stat

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from sandbox_ai_app import files_page, widgets  # noqa: E402


class FakeSai:
    def __init__(self, roots, hosts=None):
        self.roots, self.hosts = roots, hosts or {}

    def file_roots(self, extra=()):
        return [*self.roots, *((kind, label, os.path.realpath(path)) for kind, label, path in extra)]

    def host_statuses(self, refresh=False, cached=True):
        return self.hosts


@pytest.fixture
def setup(qapp, tmp_path, monkeypatch, wait):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # confirm() says yes; what was asked is collected
    monkeypatch.setattr(widgets, "SHOWN", [])
    ws, host, outside = tmp_path / "ws", tmp_path / "host", tmp_path / "outside"
    for path in (ws, host, outside):
        path.mkdir()
    (outside / "precious.txt").write_text("the host's own")
    (ws / "a.txt").write_text("alpha")
    (ws / "run.sh").write_text("#!/bin/sh\n")
    (ws / "run.sh").chmod(0o755)
    (ws / "sub").mkdir()
    (ws / "sub" / "b.txt").write_text("beta")
    (ws / "escape").symlink_to(outside)
    sai = FakeSai([("run", "claude-abc", str(ws.resolve()))], hosts={"lab": {"version": "0.1.76"}})
    page = files_page.FilesPage(sai)
    assert wait(lambda: page.roots.count() > 0 and not page.roots_reading), "the roots are read in the background"
    page.right.go_path(str(host))
    settle(page.left, page.right)
    return page, ws.resolve(), host.resolve(), outside


def settle(*panes):
    """The folders are listed in a thread: until every listing on its way is shown."""
    from conftest import wait_until
    assert wait_until(QtWidgets.QApplication.instance(), lambda: all(pane.listing is None for pane in panes), 30)


def names(pane):
    """The entries listed (not "..", the folder above)."""
    settle(pane)
    return sorted(text for text in (pane.table.item(r, 0).text() for r in range(pane.table.rowCount())) if text != "..")


def select(pane, *wanted):
    settle(pane)
    pane.table.clearSelection()
    widgets.select_keys(pane.table, list(wanted))


def press(pane, key, modifiers=Qt.KeyboardModifier.NoModifier):
    settle(pane.page.left, pane.page.right)
    event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
    QtWidgets.QApplication.sendEvent(pane.table, event)
    settle(pane.page.left, pane.page.right)
    return event


def finish(page, wait):
    assert wait(lambda: not page.task.running, 30)
    return page.last_report


def test_the_panes_show_a_sandbox_and_this_machine(setup):
    page, ws, host, _ = setup
    assert page.left.root.path == str(ws) and page.left.root.sandbox
    assert page.right.root.path == "/" and not page.right.root.sandbox and page.right.path_text() == str(host)
    assert names(page.left) == ["a.txt", "escape", "run.sh", "sub"]
    row = widgets.find_row(page.left.table, "escape")
    assert page.left.table.item(row, files_page.LINK_COLUMN).text().startswith("→ ")
    row = widgets.find_row(page.left.table, "run.sh")
    assert page.left.table.item(row, 6).text().startswith("-rwxr-xr-x") and ":" in page.left.table.item(row, 5).text()
    # Another host's sandboxes: listed, not available.
    texts = [page.roots.itemText(i) for i in range(page.roots.count())]
    assert texts[-1] == "Sandboxes on lab: not available here yet"
    assert not page.roots.model().item(page.roots.count() - 1).isEnabled() and "comes later" in page.left.note.text()
    # The old page's names stand for the sandbox pane.
    assert page.table is page.left.table and page.relative == "" and page.entries is page.left.entries


def test_links_are_never_opened_in_the_sandbox_pane_and_paths_stay_below_it(setup):
    page, ws, _, outside = setup
    page.left.open_row(widgets.find_row(page.left.table, "escape"))
    assert "not followed" in page.message.text() and page.left.relative == ""
    listed = []
    page.left.go("escape", then=listed.append)  # (listed in a thread: whether it could be, once it is shown)
    settle(page.left)
    assert listed == [False] and page.left.relative == ""
    assert not page.left.go_path("../") and not page.left.go_path(str(outside))
    assert page.left.go_path(str(ws / "sub")) and page.left.relative == "sub"
    page.left.go("sub", then=listed.append)
    settle(page.left)
    assert listed == [False, True] and names(page.left) == ["b.txt"]


def test_a_host_folder_in_a_sandbox_is_treated_as_the_agents(setup):
    page, ws, host, _ = setup
    assert not page.right.untrusted()
    page.right.go_path(str(ws))
    settle(page.right)
    assert page.right.untrusted() and "belongs to a sandbox" in page.right.note.text()
    page.right.open_row(widgets.find_row(page.right.table, "escape"))
    assert "not followed" in page.message.text() and page.right.path_text() == str(ws)


def test_keys(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    left, right = page.left, page.right
    assert press(left, Qt.Key.Key_Tab).isAccepted() and page.active is right
    press(right, Qt.Key.Key_Tab)
    assert page.active is left
    press(left, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert len(left.chosen()) == 4
    settle(left)
    left.table.setCurrentCell(widgets.find_row(left.table, "sub"), 0)
    press(left, Qt.Key.Key_Return)
    assert left.relative == "sub"
    press(left, Qt.Key.Key_Backspace)
    assert left.relative == ""
    answers = iter([("made", True), ("renamed.txt", True)])
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: next(answers))
    press(left, Qt.Key.Key_F7)
    assert (ws / "made").is_dir()
    select(left, "a.txt")
    left.table.setCurrentCell(widgets.find_row(left.table, "a.txt"), 0)
    press(left, Qt.Key.Key_F2)
    assert (ws / "renamed.txt").read_text() == "alpha" and not (ws / "a.txt").exists()
    select(left, "renamed.txt", "sub")
    press(left, Qt.Key.Key_F5)
    finish(page, wait)
    assert (host / "renamed.txt").read_text() == "alpha" and (host / "sub" / "b.txt").read_text() == "beta"
    select(right, "sub")
    press(right, Qt.Key.Key_F6)  # back into the sandbox's folder, which has a sub already: refused, kept
    report = finish(page, wait)
    assert any("already exists" in line for line in report) and (host / "sub").exists()
    select(left, "made")
    press(left, Qt.Key.Key_Delete)
    finish(page, wait)
    assert not (ws / "made").exists()
    assert [title for title, *_ in widgets.SHOWN] == ["Delete?"]
    select(right, "renamed.txt")
    press(right, Qt.Key.Key_F8)
    finish(page, wait)
    assert not (host / "renamed.txt").exists() and (ws / "renamed.txt").exists()


def test_copy_and_move_between_the_panes(setup, wait):
    page, ws, host, outside = setup
    select(page.left, "run.sh", "escape")
    assert page.copy_selected(page.left) is True
    report = finish(page, wait)
    assert (host / "run.sh").exists() and not os.access(host / "run.sh", os.X_OK), "never executable on the host"
    assert not (host / "escape").exists() and any("symbolic link" in line for line in report)
    assert any(line.startswith("Copied without the executable bit") for line in report)
    assert "run.sh" in names(page.right)
    # Replacing asks first, naming what is replaced.
    (ws / "run.sh").write_text("#!/bin/sh\necho newer\n")
    page.replace.setChecked(True)
    select(page.left, "run.sh")
    page.copy_selected(page.left)
    finish(page, wait)
    title, text, details = widgets.SHOWN[-1]
    assert title == "Replace existing files?" and str(host) in text and details == "run.sh"
    assert "newer" in (host / "run.sh").read_text()
    # A move: copied, then the source is gone; back the other way keeps exec bits off what the agent wrote.
    (host / "tool").write_text("#!/bin/sh\n")
    (host / "tool").chmod(0o755)
    page.right.list()
    select(page.right, "tool")
    page.copy_selected(page.right, move=True)
    report = finish(page, wait)
    assert not (host / "tool").exists() and os.access(ws / "tool", os.X_OK) and "moved" in report[0]
    assert (outside / "precious.txt").read_text() == "the host's own"


def test_cancel_stops_a_copy_and_leaves_nothing(setup, wait, monkeypatch):
    page, ws, host, _ = setup
    from sandbox_ai_app import files
    (ws / "big").mkdir()
    for index in range(50):
        (ws / "big" / f"{index}.bin").write_bytes(b"x" * 1000)
    real = files._copy_data

    def slow(source, target, budget):
        import time
        time.sleep(0.05)
        return real(source, target, budget)
    monkeypatch.setattr(files, "_copy_data", slow)
    page.left.list()
    select(page.left, "big")
    assert page.copy_selected(page.left)
    assert wait(lambda: page.progress is not None and page.progress.count > 2, 10)
    page.task.cancel()
    report = finish(page, wait)
    assert any("Cancelled" in line for line in report) and not (host / "big").exists()


def test_permissions_and_owners(setup, wait):
    page, ws, host, _ = setup
    entries = [e for e in page.left.entries if e.name == "run.sh"]
    assert page.chmod(page.left, entries, 0o700)
    assert stat.S_IMODE((ws / "run.sh").stat().st_mode) == 0o700
    assert not page.chmod(page.left, entries, 0o4755) and "never below a sandbox" in page.message.text()
    assert not (ws / "run.sh").stat().st_mode & stat.S_ISUID
    entries = [e for e in page.left.entries if e.name == "sub"]
    assert page.chown(page.left, entries, os.getuid(), os.getgid(), recursive=True)
    report = finish(page, wait)
    assert report[0] == "2 file(s) and folder(s) changed."
    page.activate(page.right)
    assert not page.buttons["own"].isEnabled()
    assert page.own_selected(page.right) is False and "only below a sandbox" in page.message.text()


def test_the_two_panes_split_half_and_half_and_a_moved_division_stays(setup, qapp, wait):
    """User request: the panes are half and half when the page is shown, and stay where the human dragged them."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    page = setup[0]
    page.resize(1400, 600)  # (the sandbox pane needs some 500 px: its folder list and buttons)
    page.show()
    assert wait(lambda: page.isVisible() and abs(page.split.sizes()[0] - page.split.sizes()[1]) <= 4), \
        page.split.sizes()
    assert page.split.childrenCollapsible() is False and not page.keep_split
    handle = page.split.handle(1)
    middle = QPoint(handle.width() // 2, handle.height() // 2)
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, middle)
    QTest.mouseMove(handle, middle - QPoint(200, 0))
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, middle - QPoint(200, 0))
    moved = page.split.sizes()
    assert moved[0] < moved[1] - 100 and page.keep_split, moved
    page.hide()
    page.show()
    qapp.processEvents()
    assert page.split.sizes() == moved, "a moved division stays"
    page.hide()


def test_home_up_and_refresh_are_actions_not_toggles(setup, qapp, wait):
    """User report: after a click on Home the button looked pressed (it kept the focus, which some themes draw as
    pressed). The pane's buttons go somewhere and stay plain: never checked or down, no focus from a click."""
    from PySide6.QtTest import QTest
    page = setup[0]
    page.show()
    buttons = {b.text(): b for b in page.right.findChildren(QtWidgets.QPushButton)}
    for name in ("Home", "Up", "Refresh"):
        widget = buttons[name]
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert not widget.isCheckable() and not widget.isChecked() and not widget.isDown() and not widget.hasFocus()
        assert widget.focusPolicy() == Qt.FocusPolicy.TabFocus, "still reached with Tab"
    assert page.right.path_text() == os.path.realpath(os.path.expanduser("~")) or page.right.path_text()
    page.hide()
