"""The log window: every entry of applog (any thread) in one panel at the bottom of the window, on every page, with
a level to show from and a filter; debug entries only when asked for, and never on a terminal unless --debug=all."""
import io
import os
import threading

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
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
    """One prompt per question: the app's, with Allow, Allow always, Block and Block always (the Approvals page's
    answer with its caps and questions) and the way to its page; a canary only leads to its page."""
    actions = window.question_actions(("local", "claude-1", "q1"))
    assert [label for label, _ in actions] == ["Allow", "Allow always", "Block", "Block always", "Open approvals"]
    sent = []
    monkeypatch.setattr(window.approvals, "answer", lambda decision, scope, entry=None, parent=None:
                        sent.append((decision, scope, entry)))
    window.approvals.questions = {("local", "claude-1", "q1"): ("claude-1", {"id": "q1"}, "local")}
    dict(actions)["Allow always"]()
    dict(actions)["Block always"]()
    assert sent == [("allow", "always", ("claude-1", {"id": "q1"}, "local")),
                    ("deny", "always", ("claude-1", {"id": "q1"}, "local"))]
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
