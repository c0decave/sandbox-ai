"""Copy in every list, table and tree: Ctrl+C copies the selected rows as text (tabs between the columns, a line
per row, in the order shown; one cell as it is), the context menu has Copy and Copy cell."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from sandbox_ai_app import backend, copying, widgets  # noqa: E402
from sandbox_ai_app.start_tree import StartTree  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the fixtures


def clipboard():
    return QtGui.QGuiApplication.clipboard().text()


@pytest.fixture
def people(qapp):
    view = widgets.table(["Name", "Role", "Where"], multi=True)
    widgets.fill(view, [["ann", "admin", "here"], ["bob", "user", "there"], ["cy", "user", "far\taway"]])
    view.resize(400, 200)
    view.show()
    QtWidgets.QApplication.processEvents()
    yield view
    view.close()


def test_ctrl_c_copies_the_selected_rows_tab_separated(people):
    people.setFocus()
    widgets.select_keys(people, ["bob", "ann"])
    QtGui.QGuiApplication.clipboard().setText("before")
    QTest.keyClick(people, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert clipboard() == "ann\tadmin\there\nbob\tuser\tthere"


def test_rows_in_the_order_shown_and_a_tab_inside_a_cell_is_a_space(people):
    people.setSortingEnabled(True)
    people.sortItems(0, Qt.SortOrder.DescendingOrder)
    widgets.select_keys(people, ["ann", "cy"])
    assert copying.copy_rows(people) == "cy\tuser\tfar away\nann\tadmin\there"
    # a column moved by the human: copied where it is shown
    people.horizontalHeader().moveSection(2, 0)
    assert copying.selection_text(people) == "far away\tcy\tuser\nhere\tann\tadmin"
    # a hidden column is left out
    people.setColumnHidden(1, True)
    assert copying.selection_text(people) == "far away\tcy\nhere\tann"


def test_a_single_cell_is_copied_as_it_is(qapp):
    view = QtWidgets.QTableWidget(1, 2)
    view.setItem(0, 0, QtWidgets.QTableWidgetItem("a\tb"))
    view.setItem(0, 1, QtWidgets.QTableWidgetItem("c"))
    copying.copyable(view)
    view.setCurrentCell(0, 0, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert copying.copy_rows(view) == "a\tb"


def test_a_list_and_a_tree(qapp):
    listed = QtWidgets.QListWidget()
    listed.addItems(["one", "two", "three"])
    listed.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
    copying.copyable(listed)
    listed.item(2).setSelected(True)
    listed.item(0).setSelected(True)
    assert copying.copy_rows(listed) == "one\nthree"
    tree = StartTree(["Kind", "Name"])
    host = tree.add_row(None, "h", "host", ["host", "here"])
    tree.add_row(host, "s1", "start", ["session", "work"], key="s1")
    tree.add_row(None, "h2", "host", ["host", "ct1"])
    tree.expandAll()
    flags = QtCore.QItemSelectionModel.SelectionFlag
    for item in tree.every_item():
        tree.selectionModel().select(tree.indexFromItem(item), flags.Select | flags.Rows)
    assert copying.copy_rows(tree) == "host\there\nsession\twork\nhost\tct1"
    assert tree.property(copying.PROPERTY) is True  # every table and tree set up by tables.setup


def test_the_context_menu_has_copy_and_copy_cell(people, monkeypatch):
    widgets.select_keys(people, ["bob"])
    point = people.visualRect(people.model().index(1, 1)).center()
    menu = copying.copy_entries(QtWidgets.QMenu(), people, point)
    entries = {a.text(): a for a in menu.actions()}
    assert set(entries) == {"Copy", "Copy cell"}
    entries["Copy cell"].trigger()
    assert clipboard() == "user"
    entries["Copy"].trigger()
    assert clipboard() == "bob\tuser\tthere"
    # a list of one column: Copy alone; an existing menu keeps its entries, a separator before Copy
    listed = QtWidgets.QListWidget()
    listed.addItems(["one"])
    listed.setCurrentRow(0)
    menu = QtWidgets.QMenu()
    menu.addAction("Remove…")
    copying.copy_entries(menu, listed, listed.visualItemRect(listed.item(0)).center())
    assert [a.text() for a in menu.actions() if not a.isSeparator()] == ["Remove…", "Copy"]
    # the view's own menu (it has none of the app's): Copy, Copy cell; once a page connects a menu of its own, that one
    assert people.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    shown = []
    monkeypatch.setattr(copying, "show_menu", lambda menu, _at: shown.append([x.text() for x in menu.actions()]))
    people.customContextMenuRequested.emit(point)
    assert shown == [["Copy", "Copy cell"]]
    people.customContextMenuRequested.connect(lambda _p: None)
    people.customContextMenuRequested.emit(point)
    assert len(shown) == 1


def test_a_view_that_gets_the_focus_becomes_copyable(qapp):
    copying.watch_focus(qapp)
    dialog = QtWidgets.QDialog()
    layout = QtWidgets.QVBoxLayout(dialog)
    first, second = QtWidgets.QListWidget(), QtWidgets.QTreeWidget()
    layout.addWidget(first)
    layout.addWidget(second)
    dialog.show()
    QtWidgets.QApplication.processEvents()
    first.setFocus()
    QtWidgets.QApplication.processEvents()
    if QtWidgets.QApplication.focusWidget() is not first:
        dialog.close()
        pytest.skip("no focus here (offscreen)")
    assert first.property(copying.PROPERTY) is True and second.property(copying.PROPERTY) is True
    dialog.close()


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
def test_every_list_table_and_tree_of_every_page_copies(window):
    from sandbox_ai_app.ui import BUILD_ORDER
    window.build_pages()
    copy = QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Copy).toString()
    missing, checked = [], 0
    for key in BUILD_ORDER:
        for view in window.built(key).findChildren(QtWidgets.QAbstractItemView):
            if isinstance(view.parent(), QtWidgets.QFrame) and isinstance(view.parent().parent(), QtWidgets.QComboBox):
                continue  # a combo box's popup
            if view.window() is not window:
                continue  # a combo box's popup (its own window)
            keys = [k.toString() for a in view.actions() for k in a.shortcuts()]
            checked += 1
            if view.property(copying.PROPERTY) is not True or copy not in keys:
                missing.append(f"{key}: {type(view).__name__} {view.objectName() or view.accessibleName()}")
    assert missing == [] and checked > 30
    assert window.nav.property(copying.PROPERTY) is False
