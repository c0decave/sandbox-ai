"""The Sandboxes page's tree: the hosts at the top (this machine, each host of `sandbox-ai hosts`), under each host its
starts, under each start what belongs to it (the MCP servers it reaches, its helpers, its pod with Podman; the
starts an MCP server serves). Only the starts are rows to act on; the hosts and what hangs below a start only show.

StartTree offers what the page and its tests know of a table: rowCount(), item(row, column), selectRow(row) and
row_keys count the start rows only (in the order shown, wherever they hang), so widgets.find_row() and row_key()
work on it as on a table filled by widgets.fill()."""
from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem

from . import tables, theme
from .widgets import CELL_LIMIT, SORT_ROLE, elide, safe_tooltip

ID_ROLE = Qt.ItemDataRole.UserRole + 20  # row_id(host, id) of a row: what its selection and its fold are kept by
KIND_ROLE = Qt.ItemDataRole.UserRole + 21  # "host", "start" or "part" (what hangs below a start)
KEY_ROLE = Qt.ItemDataRole.UserRole + 22  # a start row's Instance.key; a part's: the key of the row it stands for
HELPER_PREFIX = "helper-"  # agent_sandbox.HELPER_PREFIX: an MCP server that is another agent, a helper of the start


def server_name(instance):
    """The name of the MCP server definition the MCP server sandbox INSTANCE runs (what a start's --mcp names), or
    None for another kind. This machine's own carry it (Instance.mcp); `sandbox-ai list` gives "mcp-NAME"."""
    if instance.kind != "mcp":
        return None
    if instance.host == "local" and not instance.key.startswith("ended:") and instance.mcp:
        return instance.mcp
    ident = str(getattr(instance, "ident", None) or instance.name)
    return ident[4:] if ident.startswith("mcp-") else ident


def names(value):
    """The names in a list (of strings) of a registry entry: other values give none."""
    if isinstance(value, str):
        value = [value]
    return [name for name in dict.fromkeys(value) if isinstance(name, str) and name] if isinstance(value, list) else []


def reached(instance):
    """[(server name, now)] of the MCP servers the start INSTANCE reaches: NOW when it reaches it while it runs
    (run.json's "mcp_servers", `sandbox-ai list`'s "mcp_running"), else at its next start (its options' --mcp)."""
    if instance.kind == "mcp":
        return []
    record = instance.record if isinstance(instance.record, dict) else {}
    now = names(record.get("mcp_servers")) + names(record.get("mcp_running"))
    later = [name for name in names(record.get("mcp")) if name not in now]
    return [(name, True) for name in dict.fromkeys(now)] + [(name, False) for name in later]


def related(instances):
    """{Instance.key: [part]} of what belongs to each start of INSTANCES (one host's): a part is a dict {"kind":
    "mcp", "helper", "pod", "companion", "team" or "serves", "name", "now" (reached now, not only at its next start),
    "key": the key of the start or MCP server it stands for, if listed, else None}."""
    servers = {server_name(i): i for i in instances if i.kind == "mcp"}
    starts = [i for i in instances if i.kind != "mcp"]
    by_name = {}
    for i in starts:
        by_name.setdefault(i.name, i)
    found, serves = {}, {}
    for i in starts:
        record = i.record if isinstance(i.record, dict) else {}
        parts = []
        for name, now in reached(i):
            server = servers.get(name)
            parts.append({"kind": "helper" if name.startswith(HELPER_PREFIX) else "mcp", "name": name, "now": now,
                          "key": server.key if server is not None else None})
            serves.setdefault(name, {})[i.name] = i.key
        pod = record.get("pod")
        if isinstance(pod, str) and pod:
            parts.append({"kind": "pod", "name": pod, "now": True, "key": None})
        parts += [{"kind": "companion", "name": name, "now": True, "key": None}
                  for name in names(record.get("companions"))]
        parts += [{"kind": "team", "name": name, "now": True, "key": None}
                  for name in dict.fromkeys(names(record.get("team")) + names(record.get("teams")))]
        found[i.key] = parts
    for name, server in servers.items():
        record = server.record if isinstance(server.record, dict) else {}
        served = dict(serves.get(name, {}))
        for start in names(record.get("serves")):
            served.setdefault(start, by_name[start].key if start in by_name else None)
        found[server.key] = [{"kind": "serves", "name": start, "now": True, "key": key}
                             for start, key in served.items()]
    return found


def row_id(host, ident=""):
    """The id of a row: its HOST and IDENT (a start's Instance.key, "" for the host's own row, more for a part), as one
    string (Qt keeps a string as it is, a tuple as a list)."""
    return f"{host}\n{ident}"


def id_host(row):
    """The host of a row_id()."""
    return str(row).partition("\n")[0]


class TreeItem(QTreeWidgetItem):
    """A row of the tree that sorts by its SORT_ROLE values (numbers as numbers), like widgets.SortItem."""

    def __lt__(self, other):
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        mine, theirs = self.data(column, SORT_ROLE), other.data(column, SORT_ROLE)
        if mine is not None and theirs is not None:
            try:
                return mine < theirs
            except TypeError:
                pass
        return self.text(column).lower() < other.text(column).lower()


class Cell:
    """One cell of a start row as a QTableWidgetItem offers it: what the table's helpers and the tests read."""

    def __init__(self, item, column, row):
        self.tree_item, self.column, self.row = item, column, row

    def text(self):
        return self.tree_item.text(self.column)

    def font(self):
        return self.tree_item.font(self.column)

    def setFont(self, font):  # noqa: N802
        self.tree_item.setFont(self.column, font)

    def foreground(self):
        return self.tree_item.foreground(self.column)

    def toolTip(self):  # noqa: N802
        return self.tree_item.toolTip(self.column)

    def data(self, role):
        if role == Qt.ItemDataRole.UserRole and self.column == 0:
            return self.row  # widgets.row_index(): its place among the start rows, whose keys are in row_keys
        return self.tree_item.data(self.column, role)


class StartTree(QTreeWidget):
    """The tree of the Sandboxes page; see the module. FOLDED(bool) after View › Fold all / Unfold all."""
    folded = Signal(bool)

    def __init__(self, headers, stretch=None, compact=()):
        super().__init__()
        self.setColumnCount(len(headers))
        self.setHeaderLabels(headers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setAllColumnsShowFocus(True)
        header = self.header()
        tables.setup(self, stretch, compact)  # widgets.fit_columns fits it
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)  # the order given, until a header is clicked
        self.setSortingEnabled(True)

    # -- what a table offers (widgets.find_row, row_key, fit_columns, state.State.table) --

    def horizontalHeader(self):  # noqa: N802
        return self.header()

    def every_item(self):
        """Every row of the tree, top to bottom as shown (folded ones too)."""
        found = []

        def walk(item):
            found.append(item)
            for index in range(item.childCount()):
                walk(item.child(index))
        for index in range(self.topLevelItemCount()):
            walk(self.topLevelItem(index))
        return found

    def start_items(self):
        return [item for item in self.every_item() if item.data(0, KIND_ROLE) == "start"]

    @property
    def row_keys(self):
        return [item.data(0, KEY_ROLE) for item in self.start_items()]

    def rowCount(self):  # noqa: N802
        return len(self.start_items())

    def item(self, row, column):
        items = self.start_items()
        if not (0 <= row < len(items) and 0 <= column < self.columnCount()):
            return None
        return Cell(items[row], column, row)

    def selectRow(self, row):  # noqa: N802
        """Select the ROW-th start row; the groups it is in open."""
        items = self.start_items()
        if 0 <= row < len(items):
            self.select_item(items[row])

    def select_item(self, item, open_groups=True):
        """Select ITEM; OPEN_GROUPS: the groups it is in open (shown)."""
        parent = item.parent()
        while open_groups and parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        flags = QItemSelectionModel.SelectionFlag
        scrolls = self.hasAutoScroll()
        self.setAutoScroll(open_groups)  # scrolling to it would open its groups too (QTreeView.scrollTo)
        try:
            self.setCurrentItem(item, 0, flags.ClearAndSelect | flags.Rows)
        finally:
            self.setAutoScroll(scrolls)

    def find_item(self, row_id):
        return next((item for item in self.every_item() if item.data(0, ID_ROLE) == row_id), None)

    def selected_item(self):
        items = self.selectedItems()
        return items[0] if items else None

    # -- View › Fold all / Unfold all (MainWindow.fold_all) --

    def collapseAll(self):  # noqa: N802
        super().collapseAll()
        self.folded.emit(True)

    def expandAll(self):  # noqa: N802
        super().expandAll()
        self.folded.emit(False)

    # -- filling --

    def add_row(self, parent, row_id, kind, cells, key=None, color=None, tip=None, sort=None, bold=False):
        """A row of CELLS below PARENT (None: at the top): ROW_ID (row_id()) keeps its selection and fold, KIND says
        what it is, KEY the start it is (or stands for). COLOR: a theme colour name; TIP: the first cell's tooltip;
        SORT: values to sort by, column by column (None: the text)."""
        item = TreeItem()
        for column, value in enumerate(cells):
            text = "" if value is None else str(value)
            item.setText(column, elide(text))
            item.setData(column, SORT_ROLE, sort[column] if sort and column < len(sort) and sort[column] is not None
                         else text.lower())
            if len(text) > CELL_LIMIT or column == self.stretch or (tip and column == 0):
                item.setToolTip(column, safe_tooltip(tip if tip and column == 0 else text))
            if color:
                item.setForeground(column, QBrush(QColor(theme.color(color))))
            if bold:
                font = item.font(column)
                font.setBold(True)
                item.setFont(column, font)
        item.setData(0, ID_ROLE, row_id)
        item.setData(0, KIND_ROLE, kind)
        item.setData(0, KEY_ROLE, key)
        if parent is None:
            self.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item
