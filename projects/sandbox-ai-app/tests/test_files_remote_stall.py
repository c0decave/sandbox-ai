"""A remote system whose server stops answering never holds up the window (files_page.root_job and on_root): for each
operation the Files page offers on a remote folder the fake SFTP server leaves exactly that request unanswered, and
the call returns at once, the event loop keeps running (a 20 ms timer, its gaps measured), and Cancel ends the job -
the stalled request interrupted, the connection ended and said so; what reads in the background ends at the request
timeout. Everything changed is below tmp_path."""
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QTimer  # noqa: E402

from sandbox_ai_app import files_page, widgets  # noqa: E402
from test_files_page import settle, setup  # noqa: E402,F401
from test_files_remote import served  # noqa: E402,F401

MAX_CALL = 0.5  # s: the longest a click may take
MAX_GAP = 0.3  # s: the longest the event loop may stand still


@pytest.fixture
def stall_file(tmp_path, monkeypatch):
    path = tmp_path / "stall"
    monkeypatch.setenv("FAKE_SFTP_HANG_FILE", str(path))  # (before the connection: the server reads it)
    return path


@pytest.fixture
def stalling(stall_file, served, monkeypatch):
    page, home, ws, host = served
    monkeypatch.setattr(files_page, "INTERRUPT_AFTER", 300)
    page.left.go_path(str(home / "docs"))
    settle(page.left, page.right)
    return page, home, ws, host, stall_file


def gaps(qapp, seconds):
    """The longest pause of the event loop within SECONDS (a 20 ms timer's ticks)."""
    ticks, timer = [], QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)
    timer.stop()
    return max((b - a for a, b in zip(ticks, ticks[1:])), default=seconds)


def timed(call):
    began = time.monotonic()
    result = call()
    return result, time.monotonic() - began


def jobs(page):
    """The operations that run as the page's Task (Cancel), with the request each one sends first."""
    left = page.left

    def copy_in():
        page.replace.setChecked(True)
        widgets.select_keys(page.right.table, ["in.txt"])
        return page.copy_selected(page.right)
    return {
        "RENAME": lambda: page.rename(left, left.by_name["a.txt"], "renamed.txt"),
        "MKDIR": lambda: page.make_folder(left, "made"),
        "OPEN": lambda: page.make_file(left, "new.txt", edit=False),
        "SETSTAT": lambda: page.chmod(left, [left.by_name["a.txt"]], 0o600),
        "LSTAT": copy_in,  # (the "already exists?" check before a copy that may replace)
    }


@pytest.mark.parametrize("request_name", ["RENAME", "MKDIR", "OPEN", "SETSTAT", "LSTAT"])
def test_a_stalled_server_never_holds_up_the_window_and_cancel_ends_the_job(stalling, qapp, wait, request_name):
    page, home, ws, host, stall = stalling
    (host / "in.txt").write_text("in")
    page.right.list()
    settle(page.right)
    stall.write_text(request_name)
    started, took = timed(jobs(page)[request_name])
    assert started and took < MAX_CALL, f"returned at once ({took:.2f} s)"
    assert page.task.running and not page.task.cancel_button.isHidden(), "busy, with Cancel"
    assert gaps(qapp, 1.0) < MAX_GAP, "the event loop kept running"
    assert page.task.running, "still waiting for the server"
    page.task.cancel_button.click()
    assert wait(lambda: not page.task.running, 5), "Cancel ended it (the stalled request interrupted)"
    assert "Cancelled" in page.message.text() and not page.left.root.alive
    stall.unlink()


def test_reads_in_the_background_end_at_the_request_timeout(stalling, qapp, wait):
    page, home, ws, host, stall = stalling
    left = page.left
    left.root.sftp.timeout = 1.5
    widgets.select_keys(left.table, ["a.txt"])
    stall.write_text("LSTAT")
    count = len(page.dialogs)
    result, took = timed(lambda: page.properties(left))  # (the first stat of Properties)
    assert result is True and took < MAX_CALL and len(page.dialogs) == count, "the dialog waits for its stat"
    assert gaps(qapp, 0.8) < MAX_GAP
    assert wait(lambda: len(page.dialogs) > count, 5)
    assert "did not answer" in page.dialogs[-1].note.text(), "the dialog names what it could not read"
    page.dialogs[-1].close()


def test_a_listing_is_never_on_the_gui_thread(stalling, qapp, wait):
    page, home, ws, host, stall = stalling
    left = page.left
    left.root.sftp.timeout = 1.5
    stall.write_text("READDIR")
    result, took = timed(lambda: left.list())
    assert result is True and took < MAX_CALL
    assert gaps(qapp, 0.8) < MAX_GAP
    assert wait(lambda: "did not answer" in page.message.text(), 5)


def test_the_editor_reads_off_the_gui_thread(stalling, qapp, wait):
    page, home, ws, host, stall = stalling
    left = page.left
    left.root.sftp.timeout = 1.5
    stall.write_text("OPEN")
    window, took = timed(lambda: page.open_editor(left.root, left.path_of(left.by_name["a.txt"]), "a.txt"))
    assert took < MAX_CALL and window.loading
    assert gaps(qapp, 0.8) < MAX_GAP
    assert wait(lambda: not window.loading, 5) and "did not answer" in window.notice.text()
    window.close()


def test_a_replaced_connection_is_closed_off_the_gui_thread(stalling, qapp, wait, monkeypatch):
    page, home, ws, host, stall = stalling
    old = page.left.root
    slow = []

    def close():
        slow.append(time.monotonic())
        time.sleep(1)
    monkeypatch.setattr(old, "close", close)
    result, took = timed(lambda: page.connect_remote("alice@fake"))
    assert result and took < MAX_CALL
    assert wait(lambda: page.left.root is not old and page.left.root is not None and page.left.root.alive, 10)
    assert slow, "the old connection was closed"
    assert gaps(qapp, 0.5) < MAX_GAP
