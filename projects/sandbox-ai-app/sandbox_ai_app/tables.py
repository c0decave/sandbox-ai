"""Column widths of the app's tables and trees: as wide as their titles and content, the long column takes the rest.

Every table (widgets.table(), the Sandboxes tree, the Activity timeline, the dialogs' tables) is set up once with
setup(VIEW, STRETCH, COMPACT) and fitted with fit_columns(VIEW) when its rows are set:

- STRETCH, the column of long text (a name, a path, a URL, a description), takes the width the others leave;
- COMPACT columns (a state, a kind, a time, a size, a count, a check mark) are as wide as their widest cell or their
  title, whichever is wider, and never shrink below that;
- every other column is as wide as its content too, but at most CAP of the view, and gives way (down to its title)
  when the view is too narrow for the stretch column to keep STRETCH_SHARE of it: a horizontal scroll bar only when
  the titles and compact columns alone are wider than the view;
- no column is ever narrower than its title (with room for the sort arrow).

Widths are measured with the font's metrics on at most SAMPLE rows (the first ones and some spread over the rest),
never with ResizeToContents on every row: a folder of 20,000 files is fitted in a few milliseconds. A table is
fitted in full when its first rows arrive; later fills only widen a column whose content became wider (nothing
jumps while the human reads), fit_columns(VIEW, force=True) fits it again from scratch. A text cut to fit its
column shows itself in full on hover.

Widths the human drags are the human's: state.State.table keeps them (per page and table, keep_tables() registers
every page's tables) and they come back at the next start; only a column narrower than its title (another language)
is widened then.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QModelIndex, QObject, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QToolTip, QTreeView, QWidget

SAMPLE = 200  # rows measured: the first half of them, the rest spread evenly over the other rows
LONGEST = 12  # texts of a column measured with the font: its longest ones
CAP = 0.4  # a column other than the stretch column: at most this share of the view's width
CAP_MIN = 140  # px: ... but a cap never below this
STRETCH_SHARE = 0.3  # the stretch column keeps this share of the view (or less, if its content is shorter) ...
STRETCH_MIN = 180  # px: ... and at least this much of its content: in a narrow view the others scroll
FALLBACK_WIDTH = 900  # px: the width assumed for a view not laid out yet (fitted again when it is)
MAXIMUM = 4000  # px: the widest a column may be dragged (the stretch column takes any width)
MINIMUM = 32  # px
SORT_ROOM = 22  # px: the sort arrow in a column title (QHeaderView adds it to every title while one is shown)


def header_of(view):
    return view.horizontalHeader() if isinstance(view, QTableView) else view.header()


def setup(view, stretch=None, compact=()):
    """How VIEW's columns are fitted: STRETCH (a column number, or None: the one with the widest content) takes the
    rest of the width; COMPACT columns are as wide as their content. Columns may be dragged; a cell cut short shows
    its full text on hover."""
    view.stretch = stretch
    view.fit_compact = set(compact)
    view.fit_done = False  # fitted with rows (not only titles)
    view.fit_titles, view.fit_content = {}, {}
    view.fitting = False  # True while fit_columns resizes (not the human: state.State.table)
    header = header_of(view)
    header.setMaximumSectionSize(MAXIMUM)
    header.setMinimumSectionSize(MINIMUM)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(False)
    if not view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    if stretch is not None:
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    if getattr(view, "fit_watcher", None) is None:
        view.fit_watcher = _Watcher(view)
    return view


def stretch_of(view):
    stretch = getattr(view, "stretch", None)
    if stretch is None:
        stretch = getattr(view, "fit_auto_stretch", None)
    return stretch


def _modes(view, header, stretch, squeezed=False):
    """The resize modes setup() chose (a header state restored from an older version may have others); SQUEEZED:
    the stretch column has a width of its own too (the view is too narrow for all columns: it scrolls)."""
    if header.stretchLastSection():
        header.setStretchLastSection(False)
    for column in range(header.count()):
        mode = QHeaderView.ResizeMode.Stretch if column == stretch and not squeezed else \
            QHeaderView.ResizeMode.Interactive
        if header.sectionResizeMode(column) != mode:
            header.setSectionResizeMode(column, mode)


def _sample(model, tree, limit=SAMPLE):
    """[(index of column 0, depth)] of at most LIMIT rows: all of a short table; of a long one the first half and
    the rest spread evenly; a tree's rows depth first (folded ones too: they are shown when unfolded)."""
    count = model.rowCount()
    if count == 0:
        return []
    if not tree or not any(model.rowCount(model.index(row, 0)) for row in range(min(count, 50))):
        if count <= limit:
            rows = range(count)
        else:
            head = limit // 2
            step = (count - head) / (limit - head)
            rows = list(range(head)) + [head + int(i * step) for i in range(limit - head)]
        return [(model.index(row, 0), 0) for row in rows]
    found, stack = [], [(QModelIndex(), -1)]
    while stack and len(found) < limit:
        parent, depth = stack.pop()
        children = [model.index(row, 0, parent) for row in range(model.rowCount(parent))]
        for child in reversed(children):
            stack.append((child, depth + 1))
        if parent.isValid():
            found.append((parent, depth))
    return found


def _measure(view, header, model, columns):
    """{column: px} the widest sampled cell of each column needs (text, icon, margins, a tree's indentation). Only
    the LONGEST texts of a column (by characters) are measured with the font: the widest is among them."""
    metrics = {None: QFontMetrics(view.font())}
    tree = isinstance(view, QTreeView)
    grid = 1 if isinstance(view, QTableView) and view.showGrid() else 0
    indent = view.indentation() if tree else 0
    first_column = header.logicalIndex(0)
    display = Qt.ItemDataRole.DisplayRole
    found = {column: {} for column in columns}  # column: {text: (index, depth)}
    holders = set()  # columns with a widget in their cells (a combo box, a button)
    widths = {column: 0 for column in columns}
    sample = _sample(model, tree)
    for position, (first, depth) in enumerate(sample):
        if tree and view.isFirstColumnSpanned(first.row(), first.parent()):
            continue  # one text over the whole row (a canary's lines): no column's width
        for column in columns:
            index = first if column == 0 else first.siblingAtColumn(column)
            if position == 0 and view.indexWidget(index) is not None:
                holders.add(column)
            if column in holders:
                widget = view.indexWidget(index)
                if widget is not None:
                    widths[column] = max(widths[column], widget.sizeHint().width())
                continue
            text = model.data(index, display)
            text = "" if text is None else str(text)
            if text and text not in found[column]:
                found[column][text] = (index, depth)
    for column, texts in found.items():
        if not texts:
            continue
        indented = tree and column == first_column
        longest = sorted(texts, key=lambda t: len(t) + (texts[t][1] * 4 if indented else 0), reverse=True)[:LONGEST]
        padding = None
        for text in longest:
            index, depth = texts[text]
            font = model.data(index, Qt.ItemDataRole.FontRole)
            key = font.key() if isinstance(font, QFont) else None
            if key not in metrics:
                metrics[key] = QFontMetrics(font)
            advance = max(metrics[key].horizontalAdvance(line) for line in text.split("\n"))
            if padding is None:
                # The delegate's margins, icon and decoration, from one real cell of the column.
                padding = min(80, max(8, view.sizeHintForIndex(index).width() - advance))
            width = advance + padding + grid
            if indented:
                width += (depth + (1 if view.rootIsDecorated() else 0)) * indent
            widths[column] = max(widths[column], width)
    return widths


def _title(view, header, column):
    """px the title of COLUMN needs (its font and margins; the sort arrow only in the column sorted by: Qt counts it
    in every title once the arrow is shown)."""
    width = header.sectionSizeHint(column)
    if header.isSortIndicatorShown() and header.sortIndicatorSection() != column:
        width -= SORT_ROOM
    return max(MINIMUM, width)


def fit_columns(view, stretch=None, compact=None, force=False):
    """Fit VIEW's columns (see the module): in full the first time it has rows (or with FORCE), else only wider
    where its content became wider. STRETCH and COMPACT, if given, change what setup() chose. Returns the seconds
    it took."""
    began = time.perf_counter()
    if getattr(view, "fit_watcher", None) is None:
        setup(view, getattr(view, "stretch", None), getattr(view, "numeric", ()))
    if stretch is not None:
        view.stretch = stretch
    if compact is not None:
        view.fit_compact = set(compact)
    model, header = view.model(), header_of(view)
    if model is None:
        return 0.0
    columns = [column for column in range(model.columnCount()) if not header.isSectionHidden(column)]
    titles = {column: _title(view, header, column) for column in columns}
    rows = model.rowCount() > 0
    content = _measure(view, header, model, columns) if rows else {column: 0 for column in columns}
    content = {column: max(content[column], titles[column]) for column in columns}
    if view.stretch is None:
        others = [c for c in columns if c not in view.fit_compact] or columns
        view.fit_auto_stretch = max(others, key=lambda c: (content[c], -c)) if rows else \
            getattr(view, "fit_auto_stretch", None) or (others[-1] if others else None)
    grow = view.fit_done and not force
    if grow:
        content = {column: max(content[column], view.fit_content.get(column, 0)) for column in columns}
    view.fit_titles, view.fit_content = titles, content
    view.fit_done = view.fit_done or rows
    apply(view)
    return time.perf_counter() - began


def apply(view):
    """The widths of VIEW's columns from what fit_columns measured, for the width the view has now."""
    header = header_of(view)
    stretch = stretch_of(view)
    titles, content = getattr(view, "fit_titles", {}), getattr(view, "fit_content", {})
    if not titles or getattr(view, "user_sized", False):
        _modes(view, header, stretch)
    if not titles:
        return
    view.fitting = True
    try:
        if getattr(view, "user_sized", False):
            for column, title in titles.items():  # the human's widths; only a title cut short is made whole
                if column != stretch and header.sectionSize(column) < title:
                    header.resizeSection(column, title)
            return
        laid_out = view.isVisible() and view.viewport().width() > 60
        room = view.viewport().width() if laid_out else FALLBACK_WIDTH
        cap = max(CAP_MIN, int(CAP * room))
        compact = getattr(view, "fit_compact", set())
        widths = {column: max(titles[column], min(content[column], cap))
                  for column in titles if column != stretch}
        need, squeezed = 0, False
        if stretch in titles:
            need = max(titles[stretch], min(content[stretch], max(int(STRETCH_SHARE * room), STRETCH_MIN)))
            over = sum(widths.values()) + need - room
            give = {column: widths[column] - titles[column] for column in widths if column not in compact}
            spare = sum(give.values())
            if over > 0 and spare > 0:
                share = min(1.0, over / spare)
                for column, extra in give.items():
                    widths[column] -= int(extra * share + 0.999)
            # Titles and short columns alone wider than the view: the stretch column keeps its share, the view scrolls.
            squeezed = laid_out and sum(widths.values()) + need > room
        _modes(view, header, stretch, squeezed)
        if squeezed:
            widths[stretch] = need
        for column, width in widths.items():
            if header.sectionSize(column) != width:
                header.resizeSection(column, width)
        view.fit_room = room if laid_out else None
    finally:
        view.fitting = False


class _Watcher(QObject):
    """Fits VIEW again when its width changes (until the human sizes a column) and shows a cell's full text on
    hover when its column cut it short."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)
        header_of(view).sortIndicatorChanged.connect(self.sorted)

    def sorted(self, *_):
        """The column sorted by needs room for the arrow in its title (and the others no longer)."""
        if getattr(self.view, "fit_titles", None):
            fit_columns(self.view)

    def eventFilter(self, watched, event):  # noqa: N802
        view = self.view
        kind = event.type()
        if kind in (QEvent.Type.Resize, QEvent.Type.Show) and getattr(view, "fit_titles", None):
            if not view.fitting and view.viewport().width() != getattr(view, "fit_room", None):
                apply(view)
        elif kind == QEvent.Type.ToolTip and watched is view.viewport():
            return self.tooltip(event)
        return False

    def tooltip(self, event):
        view = self.view
        index = view.indexAt(event.pos())
        model = view.model()
        if not index.isValid() or model is None or model.data(index, Qt.ItemDataRole.ToolTipRole):
            return False  # the cell's own tooltip
        text = model.data(index, Qt.ItemDataRole.DisplayRole)
        text = "" if text is None else str(text)
        rect = view.visualRect(index)
        font = model.data(index, Qt.ItemDataRole.FontRole)
        needed = QFontMetrics(font if isinstance(font, QFont) else view.font()).horizontalAdvance(text) + 8
        if text and needed > rect.width():
            from .widgets import safe_tooltip
            QToolTip.showText(event.globalPos(), safe_tooltip(text), view.viewport(), rect)
        else:
            QToolTip.hideText()
            event.ignore()
        return True


def _name(owner, view, depth=2):
    """The attribute path ("git-list") under which OWNER (a page) holds VIEW, or None."""
    held = vars(owner) if hasattr(owner, "__dict__") else {}
    for name, value in held.items():
        if value is view:
            return name
    if depth > 1:
        for name, value in held.items():
            if isinstance(value, QWidget) and not name.startswith("_") and value.isAncestorOf(view):
                inner = _name(value, view, depth - 1)
                if inner:
                    return f"{name}-{inner}"
    return None


def keep_tables(state, pages):
    """Register with STATE (state.State) every table and tree of PAGES ({page key: page}) fitted here that is not
    registered yet, as "PAGE-ATTRIBUTE": the widths the human gives its columns come back at the next start."""
    count = 0
    for key, page in pages.items():
        if not isinstance(page, QWidget):
            continue
        for view in page.findChildren(QAbstractItemView):
            if getattr(view, "fit_watcher", None) is None or getattr(view, "state_key", None):
                continue
            name = _name(page, view)
            if name:
                state.table(f"{key}-{name}", view)
                count += 1
    return count


def keep(view, key):
    """Register VIEW (a dialog's table) with the running window's state.State as KEY, if there is one."""
    from . import state
    current = state.current()
    if current is not None and not getattr(view, "state_key", None):
        current.table(key, view)
    return view
