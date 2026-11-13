"""The Sandboxes page's tree: the hosts at the top (this machine, each host of `sandbox-ai hosts`), under each host its
starts, under each start the starts that belong to it (owners(): the MCP servers and helpers started for it or that
only it uses, the members of a team it leads) and what else belongs to it (the MCP servers it reaches, its helpers,
its pod with Podman; the starts an MCP server serves). MCP servers that belong to no start stand together below
their host, said so (orphans()). Only the starts are rows to act on; the hosts and what hangs below a start only show.

StartTree offers what the page and its tests know of a table: rowCount(), item(row, column), selectRow(row) and
row_keys count the start rows only (in the order shown, wherever they hang), so widgets.find_row() and row_key()
work on it as on a table filled by widgets.fill()."""
from __future__ import annotations

import calendar
import functools
import time

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem

from . import tables, theme
from .widgets import CELL_LIMIT, SORT_ROLE, elide, safe_tooltip

ID_ROLE = Qt.ItemDataRole.UserRole + 20  # row_id(host, id) of a row: what its selection and its fold are kept by
KIND_ROLE = Qt.ItemDataRole.UserRole + 21  # "host", "start" or "part" (what hangs below a start)
KEY_ROLE = Qt.ItemDataRole.UserRole + 22  # a start row's Instance.key; a part's: the key of the row it stands for
HELPER_PREFIX = "helper-"  # agent_sandbox.HELPER_PREFIX: an MCP server that is another agent, a helper of the start
TEAM_PREFIX = "team-"  # team.server_name: the board of a team (sandbox-ai team), an MCP server


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


GUESS_SECONDS = 120  # an MCP server of an older sandbox-ai belongs to the start of its host started this close to it
ORPHANS = "\norphans"  # the row id (below a host) of the MCP servers that belong to no start


def _moment(value):
    """A time as sandbox-ai writes it (UTC, "YYYY-MM-DDTHH:MM:SSZ") in seconds, or None."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    return _seconds(value[:19])


@functools.lru_cache(maxsize=4096)
def _seconds(value):
    """_moment of the first 19 characters, kept (the same start times at every look at a host)."""
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def refs(instance):
    """The names a start goes by in records of others (its name, its id, its run's id, its start's id)."""
    record = instance.record if isinstance(instance.record, dict) else {}
    found = [instance.name, getattr(instance, "ident", None), instance.run] + \
        [record.get(key) for key in ("run", "id", "start")]
    return {ref for ref in found if isinstance(ref, str) and ref}


def started_for(record, by_ref):
    """The start of BY_REF ({ref: Instance}: refs()) an MCP server's RECORD says it was started for: "started_for"
    as backend.instances gives it ({"start", "run"}) or as sandbox-ai list --json does (the start's name); else None."""
    link = record.get("started_for") if isinstance(record, dict) else None
    wanted = [link] if isinstance(link, str) else [link.get("start"), link.get("run")] if isinstance(link, dict) else []
    return next((by_ref[ref] for ref in wanted if isinstance(ref, str) and ref in by_ref), None)


def is_server(instance):
    """True for an MCP server's sandbox, also one whose launcher has not registered yet (backend.instances)."""
    record = instance.record if isinstance(instance.record, dict) else {}
    return instance.kind == "mcp" or instance.kind == "unregistered" and bool(record.get("mcp_server"))


def owners(instances):
    """{Instance.key: (owner's key, how)} of the starts of INSTANCES (one host's) that belong to another one: what is
    shown below that start, and gets no tab of its own when it starts. HOW says why:

      "started"  an MCP server (or helper) started for that start (its mcp.json's "started_for", since 0.1.78);
      "uses"     an MCP server that start reaches (now first, then at its next start), when none is recorded;
      "listed"   as sandbox-ai list says ("listed_under"), when the fields above say nothing;
      "helper"   a start that is a helper of that start ("helper_of");
      "team"     a member of a team below its lead ("lead": list --json, backend.instances), else below the start
                 its team's board server (team-NAME) was started for;
      "time"     an MCP server of an older sandbox-ai, which did not record whom it was started for, that no start
                 uses: the start of its host started closest to it, within GUESS_SECONDS (a guess, said so).

    Each goes below the top of its chain (a lead's helper's MCP server below the lead); starts that name each other
    round in a circle stay at the top. An MCP server none of this links to belongs to no start: orphans()."""
    starts = [i for i in instances if not is_server(i)]
    by_ref = {}
    for i in starts:
        for ref in refs(i):
            by_ref.setdefault(ref, i)
    servers = {}
    for i in instances:
        if i.kind == "mcp":
            servers.setdefault(server_name(i), i)
    # What each start reaches, worked out once (not once per MCP server: a host with 150 starts and 300 servers spent
    # 60 ms of every look at it here), and the starts that reach each server name, in the order of STARTS.
    reach = {id(s): reached(s) for s in starts}
    users_of = {}
    for s in starts:
        for name in dict.fromkeys(n for n, _ in reach[id(s)]):
            users_of.setdefault(name, []).append(s)
    moments = {id(s): _moment(s.started) for s in starts}
    direct = {}
    for i in instances:
        record = i.record if isinstance(i.record, dict) else {}
        found = None
        if is_server(i):
            owner = started_for(record, by_ref)
            if owner is not None:
                found = (owner, "started")
            name = server_name(i) if i.kind == "mcp" else None
            users = list(users_of.get(name, ())) if name is not None else []
            if found is None and users:
                users.sort(key=lambda s: (not any(n == name and now for n, now in reach[id(s)]), str(s.started)))
                found = (users[0], "uses")
            listed = record.get("listed_under")
            if found is None and isinstance(listed, str) and listed in by_ref:
                found = (by_ref[listed], "listed")
            if found is None and not record.get("owner_recorded"):
                moment = _moment(i.started)
                near = [(abs(moments[id(s)] - moment), str(s.started), s) for s in starts
                        if moment is not None and moments[id(s)] is not None
                        and abs(moments[id(s)] - moment) <= GUESS_SECONDS]
                if near:
                    found = (min(near, key=lambda n: n[:2])[2], "time")
        else:
            helper = record.get("helper_of")
            lead = record.get("lead")
            if isinstance(helper, str) and helper in by_ref:
                found = (by_ref[helper], "helper")
            elif isinstance(lead, str) and lead in by_ref:
                found = (by_ref[lead], "team")
            else:
                for name, _ in reach[id(i)]:
                    board = servers.get(name) if name.startswith(TEAM_PREFIX) else None
                    owner = started_for(board.record, by_ref) if board is not None else None
                    if owner is not None:
                        found = (owner, "team")
                        break
        if found is not None and found[0] is not i:
            direct[i.key] = (found[0].key, found[1])
    top = {}
    for key, (owner, how) in direct.items():
        seen, at = {key}, owner
        while at in direct and at not in seen:
            seen.add(at)
            at = direct[at][0]
        if at not in seen:
            top[key] = (at, how)
    return top


def orphans(instances, owned=None):
    """The keys of the MCP servers of INSTANCES (one host's) that belong to no start (owners()): started on their
    own (sandbox-ai mcp start), or the start they were started for ended and none uses them."""
    owned = owners(instances) if owned is None else owned
    return [i.key for i in instances if is_server(i) and i.key not in owned]


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
        # several at once (Ctrl+click, Shift+click): Stop, Pause, Resume and Delete act on each (SandboxesPage.bulk)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        self.plan = self.items_by_id = self.structure = self.look = None  # what fill() shows

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
        self.set_cells(item, cells, color, tip, sort, bold)
        item.setData(0, ID_ROLE, row_id)
        item.setData(0, KIND_ROLE, kind)
        item.setData(0, KEY_ROLE, key)
        if parent is None:
            self.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def set_cells(self, item, cells, color=None, tip=None, sort=None, bold=False):
        """The cells of ITEM (add_row's CELLS, COLOR, TIP, SORT, BOLD); what an earlier call set and this one does
        not (a tooltip, a colour, bold) goes."""
        brush = QBrush(QColor(theme.color(color))) if color else None
        for column, value in enumerate(cells):
            text = "" if value is None else str(value)
            item.setText(column, elide(text))
            item.setData(column, SORT_ROLE, sort[column] if sort and column < len(sort) and sort[column] is not None
                         else text.lower())
            if len(text) > CELL_LIMIT or column == self.stretch or (tip and column == 0):
                item.setToolTip(column, safe_tooltip(tip if tip and column == 0 else text))
            elif item.data(column, Qt.ItemDataRole.ToolTipRole):
                item.setData(column, Qt.ItemDataRole.ToolTipRole, None)
            if brush is not None:
                item.setForeground(column, brush)
            elif item.data(column, Qt.ItemDataRole.ForegroundRole) is not None:
                item.setData(column, Qt.ItemDataRole.ForegroundRole, None)
            if bold or item.data(column, Qt.ItemDataRole.FontRole) is not None:
                font = item.font(column)
                if font.bold() != bool(bold):
                    font.setBold(bool(bold))
                    item.setFont(column, font)

    def fill(self, plan, expanded):
        """The tree as PLAN says: [(parent row id or None, row id, kind, key, {cells, color, tip, sort, bold})], parents
        before their rows. When the tree holds these very rows already (the same ids under the same parents - what
        the polls mostly find), only the cells that changed are set, in place: no rows taken down and built again (a
        relayout and repaint of the whole tree every few seconds), selection, folds and scroll stay as they are.
        Returns the new items' groups [(item, its row id)] to open or close (EXPANDED(row id) says), or None when the
        rows were updated in place."""
        structure = [(parent, ident, kind, key) for parent, ident, kind, key, _ in plan]
        shown, items = self.plan, self.items_by_id
        if self.same_rows(plan):
            for _, ident, _, _, fields in plan:
                if shown.get(ident) != fields:
                    self.set_cells(items[ident], **fields)
            self.plan = {ident: fields for _, ident, _, _, fields in plan}
            return None
        self.clear()
        items = {}
        for parent, ident, kind, key, fields in plan:
            items[ident] = self.add_row(items.get(parent), ident, kind, key=key, **fields)
        groups = [(items[ident], ident) for parent, ident, kind, _, _ in plan
                  if kind == "host" or items[ident].childCount()]
        for item, ident in groups:
            item.setExpanded(expanded(ident, item))
        self.structure, self.items_by_id, self.look = structure, items, _look()
        self.plan = {ident: fields for _, ident, _, _, fields in plan}
        return groups

    def same_rows(self, plan):
        """True when the tree shows PLAN's rows (ids, parents, order) in the colours of the theme in use."""
        return self.plan is not None and self.look == _look() and len(self.plan) == len(plan) and \
            self.structure == [(parent, ident, kind, key) for parent, ident, kind, key, _ in plan]

    def shows(self, plan):
        """True when the tree shows PLAN as it is: nothing to do."""
        return self.same_rows(plan) and self.plan == {ident: fields for _, ident, _, _, fields in plan}

    def clear(self):
        """(Every row goes: fill() builds them again.)"""
        self.plan = self.items_by_id = None
        super().clear()


def _look():
    """The theme whose colours the cells were given."""
    return theme.current(), theme.is_dark()
