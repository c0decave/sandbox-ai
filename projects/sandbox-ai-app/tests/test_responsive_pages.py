"""Every page, every button: nothing slow runs on the GUI thread (user report: "the app seems to have problems in many
places"), from the window's start to its end. Every slow backend method (test_references.BLOCKING_SAI: commands here
and over SSH, reads that may ask Podman) is replaced - before the window is built, so its start counts too - by one
that notes the thread it was called on, waits a moment and fails; the reads of what runs here (REAL) run for real on a
fake run, a kept Podman session and a fake `podman` that takes PODMAN_SLEEP seconds (the start must not wait for it).
Then each page is shown, the first row of every list selected and every enabled push button clicked (dialogs
answered at once: confirmations yes, file and input dialogs cancelled, other dialogs closed), the event loop run a
little after each click. No slow method may have been called on the GUI thread, no click may hold the window
(MAX_CLICK seconds: building a dialog or a page included). Then the window idles: the polls never stop the event loop
for MAX_IDLE_GAP and never count as work (no busy bar). Then it is closed: nothing is asked about work (only reads ran)
and no process it started outlives it."""
import os
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402

from test_projects import ask, close, env, fake_run, fake_session, make_window, project  # noqa: E402,F401
from test_references import BLOCKING_SAI  # noqa: E402

MAX_CLICK = 0.5  # seconds a click may take on the GUI thread (a dialog or a page built on the spot)
MAX_BUILD = 8.0  # seconds building the window may take (it must not wait for PODMAN_SLEEP: that alone is more)
MAX_IDLE_GAP = 0.25  # seconds the event loop may stand still while the window idles (polls run in threads)
IDLE = 3.0  # seconds of idling
PODMAN_SLEEP = 10  # seconds the fake podman takes
# Reads that run for real (in their threads) on the fake run and session: the tables have rows to select.
REAL = {"instances", "runs", "sessions", "known_projects", "file_roots", "recent_folders"}
# Buttons that end the app or hide its window.
SKIP = {"Quit", "Quit sandbox-ai", "Hide to tray"}


def children():
    """The processes this one started that still run (zombies not counted)."""
    found = set()
    for task in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{task}/children") as stream:
                found |= {int(pid) for pid in stream.read().split()}
        except OSError:
            pass
    alive = set()
    for pid in found:
        try:
            with open(f"/proc/{pid}/stat") as stream:
                if stream.read().rpartition(")")[2].split()[0] != "Z":
                    alive.add(pid)
        except OSError:
            pass
    return alive


@pytest.fixture
def swept(env, monkeypatch, qapp, tmp_path):
    """The window, built with every slow backend method faked (noting GUI-thread calls) and every dialog answered;
    (window, the slow calls on the GUI thread while it was built, seconds the build took)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(f"#!/bin/sh\nsleep {PODMAN_SLEEP}\necho 'podman: no answer' >&2\nexit 125\n")
    podman.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    workspace = project(env, "p")
    run = fake_run(env, "claude-abc", workspace)
    ask(run, "1-2-3", kind="net", host="example.org", port=443)
    fake_session(env, "kept", workspace)  # a stopped Podman session: reading it asks podman (PODMAN_SLEEP)
    on_gui = []
    for name in sorted(BLOCKING_SAI):
        original = getattr(backend.SandboxAI, name, None)
        if not callable(original):
            continue

        def fake(self, *args, _name=name, _original=original, **kwargs):
            if threading.current_thread() is threading.main_thread():
                on_gui.append(_name)
            time.sleep(0.05)
            if _name in REAL:
                return _original(self, *args, **kwargs)
            raise RuntimeError(f"{_name}: not in this test")
        monkeypatch.setattr(backend.SandboxAI, name, fake)
    monkeypatch.setattr(QtWidgets.QDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected)
    monkeypatch.setattr(QtWidgets.QMenu, "exec", lambda self, *a, **k: None)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("", False))
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem", lambda *a, **k: ("", False))
    for box in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, box, lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [])
    monkeypatch.setattr("sandbox_ai_app.ui.QProcess.startDetached", lambda *a, **k: True)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    undo = widgets.track_worker_processes()  # as the app's start does: what the reads start ends with the app
    before = children()
    began = time.monotonic()
    window = make_window(monkeypatch)
    built = time.monotonic() - began
    window.before_children = before
    window.on_gui = on_gui
    at_build = list(on_gui)
    window.set_project(str(workspace))
    try:
        yield window, at_build, built
    finally:
        close(window)
        undo()


def run_for(qapp, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


def wait_for(qapp, condition, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not condition():
        qapp.processEvents()
        time.sleep(0.005)
    return condition()


def clickable(page):
    """The push buttons and tool buttons (a "(?)" too) shown and enabled on PAGE; check boxes and radio buttons
    count as well (a setting written, a mode chosen); not one that opens a menu (it waits for a choice)."""
    for widget in page.findChildren(QtWidgets.QAbstractButton):
        if widget.isVisibleTo(page) and widget.isEnabled() and widget.text().strip() not in SKIP \
                and not (isinstance(widget, (QtWidgets.QPushButton, QtWidgets.QToolButton)) and widget.menu()):
            yield widget


def select_first_rows(page):
    """The first row of every list shown on PAGE selected: the buttons that act on a row do something."""
    selected = 0
    for view in page.findChildren(QtWidgets.QAbstractItemView):
        try:
            model = view.model()
            if model is None or not model.rowCount() or not view.isVisibleTo(page):
                continue
            view.setCurrentIndex(model.index(0, 0))
            if view.selectionModel() is not None:
                flags = QtCore.QItemSelectionModel.SelectionFlag
                view.selectionModel().select(model.index(0, 0), flags.ClearAndSelect | flags.Rows)
            selected += 1
        except RuntimeError:  # deleted meanwhile
            pass
    return selected


class Heartbeat:
    """A timer that ticks every 10 ms: the longest time the event loop stood still."""

    def __init__(self):
        self.stamps, self.timer = [], QtCore.QTimer()
        self.timer.setInterval(10)
        self.timer.timeout.connect(lambda: self.stamps.append(time.monotonic()))

    def __enter__(self):
        self.began = time.monotonic()
        self.timer.start()
        return self

    def __exit__(self, *exc):
        self.timer.stop()
        points = [self.began, *self.stamps, time.monotonic()]
        self.gap = max(b - a for a, b in zip(points, points[1:]))


def test_every_button_of_every_page_leaves_slow_work_to_the_background(qapp, swept):
    from sandbox_ai_app.ui import PAGES
    window, at_build, built = swept
    assert not at_build, "slow backend calls on the GUI thread while the window was built: " + ", ".join(at_build)
    assert built < MAX_BUILD, f"building the window took {built:.1f} s (it waited for Podman?)"
    window.show()
    assert wait_for(qapp, lambda: window.sandboxes.loaded and window.sandboxes.table.rowCount() > 1, 60), \
        "the Sandboxes page reads in the background and shows the fake run"
    slow_clicks, clicked, selected = [], 0, 0
    stray = set(QtWidgets.QApplication.topLevelWidgets())
    for key, _title in PAGES:
        page = getattr(window, key, None)
        if page is None:
            continue
        window.show_page(key)
        run_for(qapp, 0.1)
        selected += select_first_rows(page)
        run_for(qapp, 0.05)
        for widget in list(clickable(page)):
            try:
                if not widget.isVisibleTo(page) or not widget.isEnabled():
                    continue  # a click before closed or disabled it
                text = widget.text()
            except RuntimeError:  # deleted by a click before (a page rebuilt its rows)
                continue
            began = time.monotonic()
            widget.click()
            took = time.monotonic() - began
            clicked += 1
            if took > MAX_CLICK:
                slow_clicks.append(f"{key}: {text!r} {took:.2f} s")
            run_for(qapp, 0.02)
            for top in QtWidgets.QApplication.topLevelWidgets():  # a dialog shown without exec: closed again
                if top not in stray and top is not window and top.isVisible() and isinstance(top, QtWidgets.QDialog):
                    top.close()
            if window.pages.currentWidget() is not page:
                window.show_page(key)
    print(f"clicked {clicked} buttons on {len(PAGES)} pages, selected a row in {selected} lists")
    assert clicked > 100, f"only {clicked} buttons were clicked"
    assert selected >= 5, f"only {selected} lists had a row to select"
    assert not window.on_gui, "slow backend calls on the GUI thread: " + ", ".join(sorted(set(window.on_gui)))
    assert not slow_clicks, "clicks that held the window:\n" + "\n".join(slow_clicks)

    # Idle: what the clicks started has ended; the polls go on in their threads and are never "work".
    assert wait_for(qapp, lambda: not window.running_work(), 60), f"still at work: {window.running_work()}"
    window.show_page("terminals")  # (its list of starts is polled every few seconds)
    seen = set()
    with Heartbeat() as beat:
        end = time.monotonic() + IDLE
        while time.monotonic() < end:
            qapp.processEvents()
            seen.update(window.running_work())
            seen.update(["busy bar"] if window.busy_bar.isVisible() else [])
            time.sleep(0.005)
    assert not seen, f"reads and polls counted as work while idling: {sorted(seen)}"
    assert beat.gap < MAX_IDLE_GAP, f"the idle window's event loop stood still for {beat.gap:.2f} s"

    # The end: no question about work (only reads run), and nothing it started outlives it.
    window.sai.save_setting("tray", False)
    widgets.SHOWN.clear()
    window.close()
    asked = [text for title, text, *_ in widgets.SHOWN if title == "Quit"]
    assert not [text for text in asked if "Still at work" in text], asked
    assert window.finished, "the window closed (the question about the running sandbox answered: quit)"
    assert wait_for(qapp, lambda: not children() - window.before_children, 5), \
        f"left running after the quit: {sorted(children() - window.before_children)}"


def test_the_sweep_notices_a_call_on_the_gui_thread(qapp, swept):
    window = swept[0]
    try:
        window.sai.call("x")
    except RuntimeError:
        pass
    assert window.on_gui == ["call"]
    widgets.SHOWN.clear()
