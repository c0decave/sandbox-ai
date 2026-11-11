"""Dialogs that block nothing (widgets.nonblocking_dialogs) and the veil over the page they came from (PageVeil): the
page takes no keys while veiled, an answer acts (and lifts its veil) at once even while another dialog waits, Qt's
own input dialogs veil instead of blocking the app, a question whose parent went answers "no", and every part of
the page is veiled (the Board's panels in docks, the page around a tab)."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QKeySequence, QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QInputDialog, QLineEdit, QListWidget, QMainWindow, QMessageBox, QStackedWidget, QTabWidget,
    QVBoxLayout, QWidget,
)
from shiboken6 import isValid  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402

from test_projects import env, window  # noqa: E402,F401


def boxes(kind=QMessageBox):
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, kind) and w.isVisible()]


def veil_of(widget):
    veil = widget.findChild(widgets.PageVeil, "pageVeil", Qt.FindChildOption.FindDirectChildrenOnly)
    return veil if veil is not None and isValid(veil) and not veil.isHidden() else None


def a_window(pages=1):
    win = QMainWindow()
    stack = QStackedWidget()
    win.setCentralWidget(stack)
    made = []
    for _ in range(pages):
        page = QWidget()
        QVBoxLayout(page)
        stack.addWidget(page)
        made.append(page)
    return win, made


def test_the_veiled_page_takes_no_keys_and_gets_its_focus_back(qapp):
    """Review: the window kept its focus widget inside the veiled page - Delete deleted, Tab moved on, the page's
    shortcuts fired while its question was open."""
    win, (page,) = a_window()
    lst = QListWidget()
    lst.addItems(["a", "b"])
    edit = QLineEdit()
    page.layout().addWidget(lst)
    page.layout().addWidget(edit)
    hits = []
    widgets.delete_key(lst, lambda: hits.append("delete"))
    save = QShortcut(QKeySequence("Ctrl+S"), page)
    save.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    save.activated.connect(lambda: hits.append("save"))
    refresh = QShortcut(QKeySequence("F5"), page)  # (the window's context: from anywhere in it)
    refresh.activated.connect(lambda: hits.append("refresh"))
    other = QShortcut(QKeySequence("F9"), win)  # the window's own: not the page's, it still works
    other.activated.connect(lambda: hits.append("window"))
    win.show()
    QApplication.setActiveWindow(win)
    QTest.qWaitForWindowExposed(win)
    lst.setFocus()
    lst.setCurrentRow(0)
    seen = {}

    def during():
        veil = veil_of(page)
        QApplication.setActiveWindow(win)  # (back to the window, as with Alt+Tab)
        seen["focus"] = win.focusWidget() is veil and veil is not None
        target = QApplication.focusWidget() or win
        QTest.keyClick(target, Qt.Key.Key_Delete)
        QTest.keyClick(target, Qt.Key.Key_Down)
        QTest.keySequence(target, QKeySequence("Ctrl+S"))
        QTest.keyClick(target, Qt.Key.Key_F5)
        QTest.keyClick(target, Qt.Key.Key_F9)
        QTest.keyClick(target, Qt.Key.Key_Tab)
        seen["after tab"] = QApplication.focusWidget() is veil
        edit.setFocus()  # (focus moved into the page some other way)
        seen["after setFocus"] = QApplication.focusWidget() is veil
        seen["row"] = lst.currentRow()
        boxes()[0].done(0)
    QTimer.singleShot(50, during)
    QMessageBox.question(page, "t", "q")
    qapp.processEvents()
    assert seen == {"focus": True, "after tab": True, "after setFocus": True, "row": 0}, seen
    assert hits == ["window"], hits
    assert win.focusWidget() is lst, "the focus goes back where it was"
    QTest.keyClick(lst, Qt.Key.Key_Delete)
    assert hits == ["window", "delete"]
    win.close()


def test_an_answer_acts_at_once_while_another_dialog_waits(qapp):
    """Review: a dialog answered while another dialog's loop ran kept its page veiled until that one closed (nested
    loops end last-in, first-out); a message of a finished background job (warn) ran a loop of its own too."""
    win, (p, q) = a_window(2)
    win.show()
    seen, order = {}, []

    def background_done():  # a background job's outcome on page Q while A's question is open
        widgets.warn(q, "B", "failed")
        order.append("warn returned")
        seen["B veils its page"] = veil_of(q) is not None

    def question_b():  # a second question, on page Q
        order.append("B asked")
        QMessageBox.question(q, "C", "replace?")
        order.append("C answered")

    def answer_a():
        [b for b in boxes() if b.windowTitle() == "A"][0].done(QMessageBox.StandardButton.Yes)
        QTimer.singleShot(30, check)

    def check():
        seen["A's veil after its answer"] = veil_of(p) is not None
        seen["open"] = sorted(b.windowTitle() for b in boxes())
        for box in boxes():
            box.done(0)
    QTimer.singleShot(20, background_done)
    QTimer.singleShot(30, question_b)
    QTimer.singleShot(60, answer_a)
    assert QMessageBox.question(p, "A", "delete?") == QMessageBox.StandardButton.Yes
    order.append("A continued")
    qapp.processEvents()
    assert seen == {"B veils its page": True, "A's veil after its answer": False, "open": ["B", "C"]}, seen
    assert order[:2] == ["warn returned", "B asked"], order
    assert veil_of(q) is None, "B's OK lifted its veil"
    win.close()


def test_a_message_veils_its_page_until_ok_and_never_waits(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    win, (page,) = a_window()
    win.show()
    widgets.inform(page, "Note", "done")  # returns at once
    box = boxes()[0]
    assert box.windowModality() == Qt.WindowModality.NonModal and veil_of(page) is not None
    widgets.show_text(page, "Text", "lines")
    assert veil_of(page) is not None
    box.buttons()[0].click()
    qapp.processEvents()
    assert veil_of(page) is not None, "the text is still open"
    next(d for d in QApplication.topLevelWidgets() if isinstance(d, QDialog) and d.isVisible()
         and d.windowTitle() == "Text").reject()
    qapp.processEvents()
    assert veil_of(page) is None
    widgets.inform(page, "Note", "x", modal=False)  # not modal: no veil
    assert veil_of(page) is None
    for b in boxes():
        b.done(0)
    win.close()


def test_a_second_dialog_right_after_the_first_gets_its_veil(qapp):
    """Review: the first dialog's veil, still waiting for its deletion, was taken for the second - and deleted under
    it."""
    win, (page,) = a_window()
    win.show()
    seen = []

    def answer(tag):
        def later():
            seen.append((tag, veil_of(page) is not None))
            boxes()[0].done(0)
        QTimer.singleShot(30, later)
    QTimer.singleShot(20, lambda: answer("first"))
    QMessageBox.question(page, "A", "a")
    QTimer.singleShot(20, lambda: answer("second"))
    QMessageBox.warning(page, "B", "b")
    assert seen == [("first", True), ("second", True)], seen
    win.close()


def test_qts_input_dialogs_veil_their_page_and_block_nothing(qapp):
    win, (page,) = a_window()
    win.show()
    seen = []

    def answer(value=None, accept=True, check=None):
        def later():
            dialog = boxes(QInputDialog)[0]
            try:
                seen.append((QApplication.activeModalWidget(), veil_of(page) is not None))
                if check is not None:
                    seen.append(check(dialog))
                if value is not None:
                    (dialog.setIntValue if isinstance(value, int) else dialog.setDoubleValue
                     if isinstance(value, float) else dialog.setTextValue)(value)
            finally:  # (never left open: the test would wait for ever)
                dialog.done(1 if accept else 0)
        QTimer.singleShot(30, later)
    answer("typed", check=lambda d: (d.textEchoMode(), d.textValue(), d.windowTitle(), d.labelText()))
    assert QInputDialog.getText(page, "Name", "Its name:", QLineEdit.EchoMode.Password, "old") == ("typed", True)
    answer(accept=False)
    assert QInputDialog.getText(page, "Name", "Its name:", text="old") == ("", False)
    answer("b", check=lambda d: (d.comboBoxItems(), d.textValue(), d.isComboBoxEditable()))
    assert QInputDialog.getItem(page, "Pick", "One:", ["a", "b", "c"], 2, False) == ("b", True)
    answer(accept=False)
    assert QInputDialog.getItem(page, "Pick", "One:", ["a", "b"], 1) == ("b", False)  # (as Qt's: the current one)
    answer(7, check=lambda d: (d.intMinimum(), d.intMaximum(), d.intStep(), d.intValue()))
    assert QInputDialog.getInt(page, "Count", "How many:", 3, 1, 9, 2) == (7, True)
    answer(accept=False)
    assert QInputDialog.getInt(page, "Count", "How many:", 3) == (3, False)
    answer(2.5, check=lambda d: d.doubleDecimals())
    assert QInputDialog.getDouble(page, "Size", "GB:", 1.0, 0.0, 10.0, 2) == (2.5, True)
    answer("one\ntwo", check=lambda d: d.testOption(QInputDialog.InputDialogOption.UsePlainTextEditForTextInput))
    assert QInputDialog.getMultiLineText(page, "Text", "Lines:", "x") == ("one\ntwo", True)
    modal = [entry for entry in seen if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], bool)
             and (entry[0] is None or isinstance(entry[0], QWidget))]
    assert modal and all(entry == (None, True) for entry in modal), seen
    assert (QLineEdit.EchoMode.Password, "old", "Name", "Its name:") in seen
    assert (["a", "b", "c"], "c", False) in seen and (1, 9, 2, 3) in seen and 2 in seen and True in seen
    assert veil_of(page) is None
    # Its parent deleted while it is open: cancelled.
    doomed = QWidget(page)
    QTimer.singleShot(30, doomed.deleteLater)
    assert QInputDialog.getText(doomed, "Name", "x") == ("", False)
    win.close()


@pytest.mark.parametrize("long", [False, True])
def test_a_question_whose_parent_went_answers_no(qapp, monkeypatch, long):
    """Review: confirm() asked the deleted box for its clicked button (RuntimeError, an error notification) when the
    question's parent was closed meanwhile (an editor closed during its question)."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    monkeypatch.setattr(backend, "needs_confirm", lambda action, detail=None: "ask")
    win, (page,) = a_window()
    win.show()
    text = "Delete it?" + (" and more" * 400 if long else "")
    for ask in (lambda parent: widgets.confirm(parent, "t", text, "Delete", destructive=True),
                lambda parent: widgets.ask_first(parent, "file-delete", "t", text, "Delete")):
        editor = QWidget(page)
        QTimer.singleShot(30, editor.deleteLater)
        assert ask(editor) is False
        # still answered by its buttons
        QTimer.singleShot(30, lambda: next(b for b in next(d for d in boxes(QDialog) if d.windowTitle() == "t")
                                           .findChildren(QtWidgets.QPushButton) if b.text() == "Delete").click())
        assert ask(page) is True
    win.close()


def test_a_tabs_dialog_veils_the_page_around_the_tabs(qapp):
    win, (page,) = a_window()
    tabs = QTabWidget()
    tab = QWidget()
    tabs.addTab(tab, "t")
    page.layout().addWidget(tabs)
    win.show()
    assert widgets.page_of(tab) is page and widgets.page_of(QLineEdit(tab)) is page
    alone = QTabWidget()  # tabs directly in a window: the tabs as a whole
    inner = QWidget()
    alone.addTab(inner, "x")
    holder = QDialog()
    QVBoxLayout(holder).addWidget(alone)
    assert widgets.page_of(inner) is alone
    seen = {}

    def during():
        seen["page"] = veil_of(page) is not None
        seen["tab"] = veil_of(tab) is not None
        tab.deleteLater()  # the tab closed meanwhile: its question goes, the page's veil with it
    QTimer.singleShot(30, during)
    assert QMessageBox.question(tab, "t", "q") == QMessageBox.StandardButton.NoButton
    qapp.processEvents()
    assert seen == {"page": True, "tab": False} and veil_of(page) is None
    holder.deleteLater()
    win.close()


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
def test_a_board_dialog_veils_its_panels_in_the_docks_too(window, qapp):  # noqa: F811
    """Review: the Board's parts and properties panels (docks of the window) stayed usable under a Board dialog."""
    window.show()
    window.show_page("board")
    board = window.board
    parts, side = board.parts_panel, board.side
    assert not board.isAncestorOf(parts) and not board.isAncestorOf(side), "in docks of the window"
    page = widgets.page_of(board)
    for parent in (board, board.palette, side):  # from the page, from a widget in a dock
        assert widgets.page_of(parent) is page
        seen = {}

        def during():
            seen["veiled"] = [veil_of(w) is not None for w in (page, parts, side)]
            boxes()[0].done(0)
        QTimer.singleShot(30, during)
        QMessageBox(QMessageBox.Icon.Question, "Board", "q", QMessageBox.StandardButton.Yes, parent).exec()
        qapp.processEvents()
        assert seen == {"veiled": [True, True, True]}, (parent, seen)
        assert [veil_of(w) for w in (page, parts, side)] == [None, None, None]
