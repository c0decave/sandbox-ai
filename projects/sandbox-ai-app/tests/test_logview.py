"""The log window: every entry of applog (any thread) in one panel at the bottom of the window, on every page, with
a level to show from and a filter; debug entries only when asked for, and never on a terminal unless --debug=all."""
import io
import os
import threading

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import applog, backend, logview  # noqa: E402

from test_projects import env, make_window, window  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_entries_from_any_thread_reach_the_panel_with_their_level(qapp, monkeypatch):
    monkeypatch.setattr(applog, "_listeners", [])
    monkeypatch.setattr(logview, "_hub", None)
    panel = logview.LogPanel("INFO")
    applog.log("INFO", "board", "a note")
    applog.log("WARNING", "board", "a warning\nwith a second line")
    worker = threading.Thread(target=lambda: applog.log("ERROR", "hosts", "from a thread"))
    worker.start()
    worker.join()
    qapp.processEvents()
    text = panel.text.toPlainText()
    assert "INFO    board: a note" in text and "WARNING board: a warning\n    with a second line" in text
    assert "ERROR   hosts: from a thread" in text
    panel.level.setCurrentIndex(panel.level.findData("WARNING"))
    assert "a note" not in panel.text.toPlainText() and "a warning" in panel.text.toPlainText()
    panel.filter.setText("thread")
    assert panel.text.toPlainText().count("\n") == 0 and "from a thread" in panel.text.toPlainText()
    panel.clear()
    assert panel.text.toPlainText() == "" and logview.hub().snapshot() == []


def colours_of(panel):
    from PySide6.QtGui import QTextCursor
    found, block = [], panel.text.document().begin()
    while block.isValid():
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        found.append(cursor.charFormat().foreground().color().name())
        block = block.next()
    return found


def test_all_entries_are_shown_again_in_one_insertion_per_run_of_a_level(qapp, monkeypatch):
    """Soak (stability round 3): the entries were put in one by one when the level, the filter or the theme changed -
    half a second for the MAX_ENTRIES at Debug, a stall of the window at every new theme and every key typed into the
    filter. A run of entries of one level goes in at once; the text and its colours are as before."""
    monkeypatch.setattr(applog, "_listeners", [])
    monkeypatch.setattr(logview, "_hub", None)
    hub = logview.hub()
    levels = ["DEBUG"] * 3 + ["INFO"] * 4 + ["WARNING"] + ["DEBUG"] * 2 + ["ERROR"]
    for index, level in enumerate(levels):
        hub._arrived({"time": "2026-11-10T10:00:00.000", "level": level, "where": "soak",
                      "message": f"entry {index}" + ("\nits second line" if index % 4 == 0 else "")})
    panel = logview.LogPanel("DEBUG")
    insertions = []
    insert = panel.insert_lines
    monkeypatch.setattr(panel, "insert_lines", lambda *args: insertions.append(1) or insert(*args))
    panel.show_all()
    assert len(insertions) == 5, "one per run of a level"
    text, colours = panel.text.toPlainText(), colours_of(panel)
    panel.text.clear()
    for entry in hub.snapshot():  # one by one, as a new entry is shown
        panel.append(entry, panel.formats())
    assert panel.text.toPlainText() == text and colours_of(panel) == colours
    assert len(set(colours)) == 4  # (debug, info, warning, error)


def test_unread_warnings_are_counted_until_the_panel_is_watched(qapp, monkeypatch):
    monkeypatch.setattr(applog, "_listeners", [])
    monkeypatch.setattr(logview, "_hub", None)
    panel = logview.LogPanel()
    counts = []
    panel.unread.connect(counts.append)
    applog.log("INFO", "x", "not counted")
    applog.log("WARNING", "x", "one")
    applog.log("ERROR", "x", "two")
    qapp.processEvents()  # (entries arrive through the event loop)
    assert counts == [1, 2]
    panel.set_watching(True)
    applog.log("WARNING", "x", "seen")
    qapp.processEvents()
    assert counts == [1, 2, 0]


def test_debug_entries_only_when_wanted_and_not_on_a_terminal(qapp, monkeypatch):
    monkeypatch.setattr(applog, "_listeners", [])
    monkeypatch.setattr(logview, "_hub", None)
    monkeypatch.setattr(applog, "_debug", False)
    monkeypatch.delenv("SANDBOX_AI_APP_DEBUG", raising=False)
    tty = Tty()
    monkeypatch.setattr(applog.sys, "stderr", tty)
    monkeypatch.setattr(applog, "_file", None)
    panel = logview.LogPanel("INFO")
    applog.debug("not wanted")
    qapp.processEvents()
    assert "not wanted" not in panel.text.toPlainText() and tty.getvalue() == ""
    panel.level.setCurrentIndex(panel.level.findData("DEBUG"))  # the log window asks for them
    applog.debug("shown in the window", key=1)
    qapp.processEvents()
    assert "shown in the window key=1" in panel.text.toPlainText() and tty.getvalue() == "", "not on the terminal"
    monkeypatch.setenv("SANDBOX_AI_APP_DEBUG", "1")
    applog.debug("debug mode")
    assert "debug mode" not in tty.getvalue(), "--debug: the terminal keeps warnings and errors"
    applog.log("WARNING", "x", "a warning")
    assert "a warning" in tty.getvalue()
    monkeypatch.setenv("SANDBOX_AI_APP_DEBUG", "all")
    applog.debug("everything")
    assert "everything" in tty.getvalue(), "--debug=all: on the terminal too"
    panel.level.setCurrentIndex(panel.level.findData("INFO"))
    assert applog._debug_wanted is False


def test_the_log_window_is_on_every_page_folded_at_first(window, qapp):
    dock = window.docks["log"]
    window.show()
    qapp.processEvents()
    for key in ("board", "launch", "sandboxes", "help"):
        window.show_page(key)
        qapp.processEvents()
        assert dock.isVisible(), key
    assert window.log_fold.is_folded()
    applog.log("WARNING", "test", "something to read")
    qapp.processEvents()
    assert dock.windowTitle().startswith("Log (")
    window.set_panel("log", True)
    qapp.processEvents()
    assert not window.log_fold.is_folded() and dock.windowTitle() == "Log"
    window.board.show_notice("a note of the board", "notice-warn")
    qapp.processEvents()
    assert "a note of the board" in window.log_panel.text.toPlainText()


def test_a_question_is_answered_from_its_notification(window, monkeypatch):
    """One prompt per question: the app's, with Allow, Allow always…, Block and Block always… (the Approvals page's
    answer with its caps; asked once more only for "always") and the way to its page; a canary only leads to its
    page."""
    actions = window.question_actions(("local", "claude-1", "q1"))
    assert [label for label, _ in actions] == ["Allow", "Allow always…", "Block", "Block always…", "Open approvals"]
    sent = []
    monkeypatch.setattr(window.approvals, "answer", lambda decision, scope, entry=None, parent=None, asked=False:
                        sent.append((decision, scope, entry, asked)))
    question = {"id": "q1", "kind": "dns", "name": "a.example"}
    window.approvals.questions = {("local", "claude-1", "q1"): ("claude-1", question, "local")}
    dict(actions)["Allow always…"]()
    dict(actions)["Block always…"]()
    assert sent == [("allow", "always", ("claude-1", question, "local"), True),
                    ("deny", "always", ("claude-1", question, "local"), True)]
    window.approvals.questions = {}
    dict(actions)["Block"]()
    assert len(sent) == 2, "a question answered meanwhile is left alone"
    canary = window.question_actions(("local", "claude-1", "q2"), canary=True)
    assert isinstance(canary, tuple) and canary[0] == "Open approvals"


def test_the_app_is_the_one_prompt_while_it_notifies(window, monkeypatch):
    beats = []
    monkeypatch.setattr(window.sai, "watcher_heartbeat", lambda on=True: beats.append(on) or True)
    monkeypatch.setattr(backend, "setting", lambda key, *a: True if key == "notify" else backend.SETTINGS.get(key))
    window.watcher_heartbeat()
    assert beats == [True] and window.heartbeat_on
    monkeypatch.setattr(backend, "setting", lambda key, *a: False if key == "notify" else backend.SETTINGS.get(key))
    window.watcher_heartbeat()
    assert beats[-1] is False and not window.heartbeat_on, "notifications off: the launchers notify again"


def test_an_open_dialog_blocks_no_window(window, qapp, monkeypatch):
    """A dialog run with exec() blocks nothing: the main window and the log window of its own stay usable (its lines
    can be copied while the dialog is open); the caller still gets the answer; Ctrl+Shift+L opens the log from the
    dialog; a second dialog of the same kind for the window brings the first to the front instead."""
    from PySide6.QtCore import QEvent, QTimer, Qt
    from PySide6.QtWidgets import QDialog, QMessageBox
    from sandbox_ai_app import widgets
    monkeypatch.setattr(QDialog, "exec", QDialog.exec)  # (the patch below is undone after the test)
    widgets.nonblocking_dialogs(platform="linux")
    widgets.nonblocking_dialogs(platform="linux")  # once
    assert QDialog.exec.nonblocking
    window.show()
    log_window = window.open_log_window()
    assert log_window.parent() is None and log_window.isVisible() and log_window.isWindow()
    blocked = []

    class Watch(QtCore.QObject):
        def eventFilter(self, obj, e):  # noqa: N802
            if e.type() == QEvent.Type.WindowBlocked:
                blocked.append(obj)
            return False
    watch = Watch()
    window.installEventFilter(watch)
    log_window.installEventFilter(watch)
    dialog = QDialog(window)
    seen = {}

    def inside():
        qapp.processEvents()
        seen["modal"] = QtWidgets.QApplication.activeModalWidget()
        seen["keys"] = bool(dialog.property("logWindowKeys"))
        applog.log("ERROR", "test", "TypeError: 'bool' object is not callable")
        qapp.processEvents()
        seen["text"] = log_window.panel.text.toPlainText()
        class Own(QDialog):
            pass
        first = Own(window)
        first.show()
        widgets.RUNNING_DIALOGS[("Own", id(window))] = first  # (as if it were open through exec())
        seen["second"] = Own(window).exec()  # the same dialog of the app again: the open one comes up instead
        widgets.RUNNING_DIALOGS.pop(("Own", id(window)), None)
        first.close()
        dialog.accept()
    QTimer.singleShot(50, inside)
    assert dialog.exec() == QDialog.DialogCode.Accepted
    assert seen["modal"] is None and seen["keys"] and seen["second"] == QDialog.DialogCode.Rejected
    assert "not callable" in seen["text"]
    assert not blocked
    # A message box answers with its button; one deleted on close still answers.
    box = QMessageBox(QMessageBox.Icon.Question, "t", "x", QMessageBox.StandardButton.Yes |
                      QMessageBox.StandardButton.No, window)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    QTimer.singleShot(30, lambda: box.button(QMessageBox.StandardButton.Yes).click())
    assert box.exec() == QMessageBox.StandardButton.Yes
    # A dialog deleted while it is open (with its parent) leaves the caller cancelled, not waiting for ever.
    parent = QtWidgets.QWidget()
    doomed = QDialog(parent)
    QTimer.singleShot(30, parent.deleteLater)
    assert doomed.exec() == QDialog.DialogCode.Rejected
    assert window.open_log_window() is log_window  # one window, brought up again
    window.shutdown()
    assert getattr(window, "log_window", None) is None and widgets.LOG_WINDOW_OPENER is None


def test_file_dialogs_and_chosen_modalities_keep_blocking(qapp, monkeypatch):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QDialog, QFileDialog, QWidget
    from sandbox_ai_app import widgets
    monkeypatch.setattr(QDialog, "exec", QDialog.exec)
    widgets.nonblocking_dialogs(platform="linux")
    parent = QWidget()
    files = QFileDialog(parent)
    files.setOption(QFileDialog.Option.DontUseNativeDialog)
    seen = []
    QTimer.singleShot(30, lambda: (seen.append(QtWidgets.QApplication.activeModalWidget()), files.reject()))
    files.exec()
    assert files.windowModality() == Qt.WindowModality.WindowModal and seen == [files]
    chosen = QDialog(parent)
    chosen.setWindowModality(Qt.WindowModality.ApplicationModal)
    QTimer.singleShot(30, lambda: (seen.append(QtWidgets.QApplication.activeModalWidget()), chosen.reject()))
    chosen.exec()
    assert seen[-1] is chosen
    parent.deleteLater()


def test_debug_entries_are_made_while_any_log_panel_shows_them(qapp):
    one, two = logview.LogPanel("INFO"), logview.LogPanel("INFO")
    two.level.setCurrentIndex(two.level.findData("DEBUG"))
    assert applog._debug_wanted is True
    one.level.setCurrentIndex(one.level.findData("ERROR"))
    assert applog._debug_wanted is True, "the other still shows them"
    two.level.setCurrentIndex(two.level.findData("INFO"))
    assert applog._debug_wanted is False


def test_a_dialog_larger_than_the_screen_is_fitted_into_it(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox, QWidget
    from sandbox_ai_app import widgets
    monkeypatch.setattr(QDialog, "showEvent", QDialog.showEvent)
    monkeypatch.setattr(QMessageBox, "showEvent", QMessageBox.showEvent)
    widgets.fitting_dialogs()
    widgets.fitting_dialogs()  # once
    area = qapp.primaryScreen().availableGeometry()
    parent = QWidget()
    dialog = QDialog(parent)
    dialog.setMinimumSize(area.width() + 400, area.height() + 300)
    dialog.move(area.right() - 50, area.bottom() - 50)
    dialog.show()
    try:
        assert dialog.width() <= area.width() and dialog.height() <= area.height()
        assert area.contains(dialog.geometry()), (area, dialog.geometry())
    finally:
        dialog.hide()
        parent.deleteLater()
    small = QDialog()
    small.resize(300, 200)
    small.show()
    assert (small.width(), small.height()) == (300, 200)  # one that fits keeps its size
    small.hide()


def test_qts_own_message_boxes_block_nothing_either(qapp, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog, QMessageBox, QWidget
    from sandbox_ai_app import widgets
    for name in ("exec",):
        monkeypatch.setattr(QDialog, name, QDialog.exec)
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, getattr(QMessageBox, name))
    monkeypatch.setattr(QDialog.exec, "nonblocking", False, raising=False)
    widgets.nonblocking_dialogs(platform="linux")
    parent = QWidget()
    seen = []

    def answer():
        box = next(w for w in QtWidgets.QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible())
        seen.append(QtWidgets.QApplication.activeModalWidget())
        box.button(QMessageBox.StandardButton.No).click()
    QTimer.singleShot(30, answer)
    assert QMessageBox.question(parent, "Delete?", "Really?") == QMessageBox.StandardButton.No
    assert seen == [None]
    QTimer.singleShot(30, lambda: next(w for w in QtWidgets.QApplication.topLevelWidgets() if isinstance(
        w, QMessageBox) and w.isVisible()).button(QMessageBox.StandardButton.Ok).click())
    assert QMessageBox.information(parent, "Note", "text") == QMessageBox.StandardButton.Ok
    parent.deleteLater()



def test_the_asking_page_waits_behind_a_veil_and_messages_are_never_dropped(window, qapp, monkeypatch):
    """Review: with dialogs not blocking, the page that asked could change under the question (another folder, host,
    board) and the answer acted there; a second message box for the same window was dropped unseen."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog, QMessageBox
    from sandbox_ai_app import widgets
    window.show()
    window.show_page("files")
    page = window.files
    holder = widgets.page_of(page)  # the page's place in the window's stack (its scroll area)
    assert holder is page or holder.isAncestorOf(page)
    seen = {}

    def inside():
        veil = holder.findChild(widgets.PageVeil, "pageVeil")
        seen["veil"] = veil is not None and veil.isVisible() and veil.geometry() == holder.rect()
        # A message for the same window while the first is open: shown too (not answered for the human).
        second = QMessageBox(QMessageBox.Icon.Warning, "Second", "shown", QMessageBox.StandardButton.Ok, page)
        QTimer.singleShot(30, lambda: second.button(QMessageBox.StandardButton.Ok).click())
        seen["second"] = second.exec()
        box.button(QMessageBox.StandardButton.Yes).click()
    box = QMessageBox(QMessageBox.Icon.Question, "Delete?", "x", QMessageBox.StandardButton.Yes |
                      QMessageBox.StandardButton.No, page)
    QTimer.singleShot(30, inside)
    assert box.exec() == QMessageBox.StandardButton.Yes
    assert seen["veil"] and seen["second"] == QMessageBox.StandardButton.Ok
    qapp.processEvents()
    left = holder.findChild(widgets.PageVeil, "pageVeil")
    assert left is None or not left.isVisible()
    # The window ended while a dialog was open: its answer starts nothing.
    late = QDialog(page)
    QTimer.singleShot(30, lambda: (setattr(window, "shut_down", True), late.accept()))
    try:
        assert late.exec() == QDialog.DialogCode.Rejected
    finally:
        window.shut_down = False


def test_the_input_watch_forgets_windows_that_go(qapp):
    """0.1.93: a dialog deleted on close left its QWindow's wrapper in InputWatch's list; a new window at the same
    address came back as that dead wrapper and every focus change raised "Internal C++ object (QWindow) already
    deleted"."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QDialog
    from sandbox_ai_app.ui import InputWatch
    watch = InputWatch(qapp)
    try:
        for _ in range(20):
            dialog = QDialog()
            dialog.show()
            qapp.processEvents()
            handle = dialog.windowHandle()
            watch.watch(handle)
            watch.focused(handle)
            assert any(seen is handle for seen in watch.windows)
            dialog.deleteLater()
            QtWidgets.QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()
            assert not any(seen is handle for seen in watch.windows), "forgotten as it went"
            watch.watch(handle)  # a dead one: nothing raised, nothing kept
            watch.focused(handle)
            watch.resume()
        assert all(alive_window.isVisible() is not None for alive_window in watch.windows)
    finally:
        watch.stop()


def test_a_panel_saved_floating_comes_back_managed_and_panels_paint_their_ground(window, qapp):  # noqa: F811
    """spectrwm: the Board's properties, saved floating with Qt's drag flag stuck, came up at every start unmanaged
    over every workspace and painting nothing ("Tafel: Eigenschaften" over an old terminal's text). It still comes
    back floating (the panels keep where they were), but as a window the window manager manages."""
    import time
    from PySide6.QtCore import Qt
    window.show()
    dock = window.docks["inspector"]
    dock.setFloating(True)
    qapp.processEvents()
    window.save_docks()
    window.restore_docks()
    dock.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, True)  # as the stuck drag left it
    dock.show()
    window.restore_docks()
    deadline = time.monotonic() + 3
    while dock.windowFlags() & Qt.WindowType.X11BypassWindowManagerHint and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert dock.isFloating() and not dock.windowFlags() & Qt.WindowType.X11BypassWindowManagerHint
    assert all(d.autoFillBackground() for d in window.docks.values())
    dock.setFloating(False)


def test_a_floating_panel_that_still_bypasses_the_window_manager_is_given_back_to_it(window, qapp):  # noqa: F811
    """spectrwm: xprop showed "Tafel: Eigenschaften" as a window the window manager did not manage (no frame,
    override-redirect: over every workspace, not to be moved or resized) - Qt's flag of a panel being dragged stayed."""
    import time
    from PySide6.QtCore import Qt
    window.show()
    dock = window.docks["inspector"]
    dock.setFloating(True)
    dock.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, True)  # as a drag left it
    dock.show()
    window.settle_floating(dock)
    deadline = time.monotonic() + 3
    while dock.windowFlags() & Qt.WindowType.X11BypassWindowManagerHint and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert not dock.windowFlags() & Qt.WindowType.X11BypassWindowManagerHint and dock.isVisible()
    dock.setFloating(False)
