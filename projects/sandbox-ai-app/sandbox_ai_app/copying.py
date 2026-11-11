"""Copy out of every list, table and tree of the app: Ctrl+C (the platform's Copy) copies the selected rows as text -
the columns separated by tabs, one line per row, in the order shown; a single cell is copied as it is - and the
context menu has Copy and, where a row has several columns, Copy cell (the cell under the pointer).

copyable(VIEW) gives a view both: tables.setup() (every table and tree of the app) does it as each is made,
copyable_all(ROOT) for every view inside ROOT (the window does it for each page as it is made), and watch_focus() for
any view that gets the focus later (a dialog's list): never with a filter on the whole application (see
ui.InputWatch). A view with a context menu of its own adds the entries with copy_entries(MENU, VIEW, POINT).
What is copied is the text shown (Qt's DisplayRole), never what a row stands for inside the app.
"""
from __future__ import annotations

from PySide6.QtCore import SIGNAL, QModelIndex, Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QApplication, QComboBox, QMenu, QTableView, QTreeView

from .i18n import tr

PROPERTY = "copyable"  # True: copyable() was here; False: left out on purpose (the window's navigation)


def copy_keys():
    return [key for key in QKeySequence.keyBindings(QKeySequence.StandardKey.Copy) if not key.isEmpty()] or \
        [QKeySequence("Ctrl+C")]


def copyable(view, menu=True):
    """Ctrl+C on VIEW copies its selected rows (selection_text); MENU: a right click shows Copy and Copy cell, unless
    the view shows a menu of its own (that one adds them: copy_entries). Once per view; returns VIEW."""
    if view is None or view.property(PROPERTY) is not None:
        return view
    view.setProperty(PROPERTY, True)
    action = QAction(tr("Copy"), view)
    action.setObjectName("copyAction")
    action.setShortcuts(copy_keys())
    # in the view and its cell editor (an editor's own Ctrl+C wins: it takes the key before any shortcut)
    action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    action.triggered.connect(lambda _=False, v=view: copy_rows(v))
    view.addAction(action)
    if menu and view.contextMenuPolicy() == Qt.ContextMenuPolicy.DefaultContextMenu and not own_menu(view):
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda point, v=view: copy_menu(v, point))
    return view


def own_menu(view):
    """True if VIEW's class (one of the app's) shows a context menu of its own."""
    return any("contextMenuEvent" in vars(kind) for kind in type(view).__mro__
               if kind.__module__.startswith("sandbox_ai_app"))


def copyable_all(root):
    """Every list, table and tree inside ROOT (and ROOT itself) copyable."""
    if isinstance(root, QAbstractItemView):
        copyable(root)
    for view in root.findChildren(QAbstractItemView):
        if not in_combo(view):
            copyable(view)
    return root


def in_combo(view):
    """True for the list a combo box pops up (its choices: nothing to copy, and no menu of its own)."""
    parent = view.parentWidget()
    while parent is not None:
        if isinstance(parent, QComboBox):
            return True
        parent = parent.parentWidget()
    return False


_focus = []


def watch_focus(app=None):
    """A view that gets the focus (a dialog's, one made after its page) becomes copyable then: before its Ctrl+C or
    its context menu (a click gives it the focus first). Through the focusChanged signal, never an event filter."""
    app = app or QApplication.instance()
    if app is None or _focus:
        return
    _focus.append(app)
    app.focusChanged.connect(_focused)


def _focused(_old, new):
    window = new.window() if new is not None else None
    if window is not None and not window.property("copyWalked"):  # a dialog: its lists, once
        window.setProperty("copyWalked", True)
        copyable_all(window)
    for widget in (new, new.parent() if new is not None else None):
        if isinstance(widget, QAbstractItemView):
            if not in_combo(widget):
                copyable(widget)
            return


def copy_menu(view, point):
    """The context menu of a view without one of its own: Copy, Copy cell. Not when another menu is connected to the
    view (it was set up after copyable(): it adds the entries itself)."""
    if view.receivers(SIGNAL("customContextMenuRequested(QPoint)")) > 1:
        return
    menu = QMenu(view)
    copy_entries(menu, view, point)
    if menu.actions():
        show_menu(menu, view.viewport().mapToGlobal(point))


def show_menu(menu, position):
    menu.exec(position)


def copy_entries(menu, view, point=None):
    """Copy (the selected rows, or the row under POINT) and Copy cell (the cell under POINT, where a row has several
    columns) at the end of MENU, a separator before them when MENU has entries already."""
    index = view.indexAt(point) if point is not None else QModelIndex()
    selected = bool(selected_indexes(view))
    if not selected and not index.isValid():
        return menu
    if menu.actions():
        menu.addSeparator()
    copy = menu.addAction(tr("Copy"), lambda: copy_rows(view) if selected else copy_index(view, index, row=True))
    copy.setShortcuts(copy_keys())
    if index.isValid() and len(visible_columns(view)) > 1:
        menu.addAction(tr("Copy cell"), lambda: copy_index(view, index))
    return menu


def selected_indexes(view):
    model = view.selectionModel()
    return model.selectedIndexes() if model is not None else []


def visible_columns(view):
    """The columns shown, left to right as shown."""
    model = view.model()
    if model is None:
        return []
    if not isinstance(view, (QTableView, QTreeView)):
        return [view.modelColumn()] if hasattr(view, "modelColumn") else [0]  # a list shows one column
    count = model.columnCount(view.rootIndex())
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    columns = [c for c in range(count) if not view.isColumnHidden(c)]
    return sorted(columns, key=header.visualIndex)


def place(view, index):
    """Where INDEX's row is shown, to sort rows top to bottom: its rows up to the top (a tree), or its row as shown."""
    if isinstance(view, QTableView):
        return (view.verticalHeader().visualIndex(index.row()),)
    path = []
    while index.isValid():
        path.append(index.row())
        index = index.parent()
    return tuple(reversed(path))


def cell(index):
    value = index.data(Qt.ItemDataRole.DisplayRole)
    return "" if value is None else str(value)


def selection_text(view, indexes=None):
    """The text of INDEXES (default: the selection; nothing selected: the current cell): a single cell as it is,
    else one line per row (top to bottom as shown) of its cells (left to right as shown) separated by tabs - in a
    tab or a line break inside a cell a space."""
    indexes = [i for i in (selected_indexes(view) if indexes is None else indexes) if i.isValid()]
    if not indexes and view.currentIndex().isValid():
        indexes = [view.currentIndex()]
    if not indexes:
        return ""
    if len(indexes) == 1:
        return cell(indexes[0])
    rows = {}
    for index in indexes:
        key = place(view, index)
        rows.setdefault(key, (index, {}))[1][index.column()] = index
    shown = visible_columns(view)
    used = [c for c in shown if any(c in cells for _, cells in rows.values())] or \
        sorted({c for _, cells in rows.values() for c in cells})
    lines = []
    for key in sorted(rows):
        cells = rows[key][1]
        lines.append("\t".join(" ".join(cell(cells[c]).split()) if c in cells else "" for c in used).rstrip("\t"))
    return "\n".join(lines)


def copy_text(text):
    QGuiApplication.clipboard().setText(text)
    return text


def copy_rows(view):
    """Ctrl+C: the selected rows (or the current cell) to the clipboard; returns the text (None: nothing to copy)."""
    text = selection_text(view)
    return copy_text(text) if text else None


def copy_index(view, index, row=False):
    """The cell INDEX, or (ROW) its whole row, to the clipboard."""
    if not index.isValid():
        return None
    if row:
        model = index.model()
        indexes = [model.index(index.row(), column, index.parent()) for column in visible_columns(view)]
        return copy_text(selection_text(view, indexes or [index]))
    return copy_text(cell(index))
