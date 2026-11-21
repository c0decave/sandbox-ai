"""Column widths of the app's tables and trees: every column as wide as its title and content, the long one the rest.

Every table (widgets.table(), the Sandboxes tree, the Activity timeline, the dialogs' tables) is set up once with
setup(VIEW, STRETCH, COMPACT) and fitted with fit_columns(VIEW) when its rows are set, again when its width changes:

- every column is first given what its widest cell or its title needs (the sort arrow counted in the column sorted
  by), whichever is wider;
- when all of that fits, nothing is cut: STRETCH, the column of long text (a name, a path, a URL, a description),
  takes the room that is left over;
- only when the view is narrower than the sum, columns give way, in this order: the STRETCH column (down to
  STRETCH_MIN, or its content if shorter), then the other long columns (first down to CAP of the view, then down to
  their titles), at last a COMPACT column (a state, a kind, a time, a size, a count, a check mark) wider than CAP; a
  horizontal scroll bar only when even that is wider than the view. A short column is never cut for a long one;
- no column is ever narrower than its title (with room for the sort arrow).

A view with stretch_interactive True (the Files page's panes) keeps every column Interactive, the stretch column too
(its edge can be dragged): it is given the room the others leave each time the columns are fitted (also when the
view's width changes). Nothing is stretched by Qt: what Qt's last-section stretching gives depends on the width the
view had when it was first fitted, and it moved columns at a change of folder.

Widths are measured on at most SAMPLE rows (the first ones and some spread over the rest), never with
ResizeToContents on every row: a folder of 20,000 files is fitted in a few milliseconds. A table is
fitted in full when its first rows arrive; later fills only widen a column whose content became wider (nothing
jumps while the human reads) - unless the widths earlier rows needed would cut a cell of the rows shown now that
would be whole without them (a view with fit_steady keeps them even then); fit_columns(VIEW, force=True) fits it again from scratch. A text cut to fit its
column is cut in the middle (a path keeps its start and its file name) and shows itself in full on hover.

Widths the human drags are the human's, column by column (view.user_columns): state.State.table keeps them (per
page and table, keep_tables() registers every page's tables) and they come back at the next start - only a column
narrower than its title (another language) is widened then. The other columns of that table are still fitted: a
width the app chose in a small window is never kept.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QModelIndex, QObject, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QToolTip, QTreeView, QWidget

SAMPLE = 200  # rows measured: the first half of them, the rest spread evenly over the other rows
LONGEST = 12  # texts of a column measured (the delegate's size hint): its longest ones
CAP = 0.4  # a view too narrow for every column's content: a column other than the stretch column gives way to
CAP_MIN = 140  # px: ... this share of the view's width, but a cap never below this
STRETCH_MIN = 180  # px: the stretch column gives way first, down to this much of its content: then the others
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
    if view.textElideMode() == Qt.TextElideMode.ElideRight:  # (Qt's default) a path keeps its start and its end
        view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    if not view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    if stretch is not None and not getattr(view, "stretch_interactive", False):
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    if getattr(view, "fit_watcher", None) is None:
        view.fit_watcher = _Watcher(view)
    from .copying import copyable
    copyable(view)  # Ctrl+C and the context menu's Copy
    return view


def stretch_of(view):
    stretch = getattr(view, "stretch", None)
    if stretch is None:
        stretch = getattr(view, "fit_auto_stretch", None)
    return stretch


def _modes(view, header, stretch, squeezed=False):
    """The resize modes setup() chose (a header state restored from an older version may have others); SQUEEZED:
    the stretch column has a width of its own too (the view is too narrow for all columns: it scrolls). A view with
    STRETCH_INTERACTIVE (the Files page's panes) has every column Interactive - the stretch column's edge can be
    dragged too (apply gives it the room the others leave)."""
    last = bool(getattr(view, "stretch_interactive", False))
    if header.stretchLastSection():
        header.setStretchLastSection(False)
    for column in range(header.count()):
        mode = QHeaderView.ResizeMode.Stretch if column == stretch and not squeezed and not last else \
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
    """{column: px} the widest sampled cell of each column needs (text, icon, margins, a tree's indentation): what
    the view's delegate asks for, so a cell given it is never cut. Only the LONGEST texts of a column (by characters)
    are measured: the widest is among them."""
    tree = isinstance(view, QTreeView)
    grid = 1 if isinstance(view, QTableView) and view.showGrid() else 0
    indent = view.indentation() if tree else 0
    first_column = header.logicalIndex(0)
    display = Qt.ItemDataRole.DisplayRole
    found = {column: {} for column in columns}  # column: {text: (index, depth)}
    holders = set()  # columns with a widget in their cells (a combo box, a button)
    widths = {column: 0 for column in columns}
    sample = _sample(model, tree)
    # A model of Python rows (widgets.RowModel) says a cell's text without an index made for it (an index of a Python
    # model asks it for its size twice: most of the time of a long table's fit); the index is made for the longest.
    shown = getattr(model, "display_text", None) if not tree else None
    for position, (first, depth) in enumerate(sample):
        if tree and view.isFirstColumnSpanned(first.row(), first.parent()):
            continue  # one text over the whole row (a canary's lines): no column's width
        for column in columns:
            if position and shown is not None and column not in holders:
                text = shown(first.row(), column)
            else:
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
            if text and (text not in found[column] or depth > found[column][text][1]):
                found[column][text] = (first, depth)  # (a tree: the deepest row with the text, the most indented)
    for column, texts in found.items():
        if not texts:
            continue
        indented = tree and column == first_column
        longest = sorted(texts, key=lambda t: len(t) + (texts[t][1] * 4 if indented else 0), reverse=True)[:LONGEST]
        for text in longest:
            first, depth = texts[text]
            index = first if column == 0 else first.siblingAtColumn(column)
            width = view.sizeHintForIndex(index).width() + grid  # its text, font, icon and margins
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
    view.fit_measured = content  # the rows shown now
    grow = view.fit_done and not force
    if grow:
        content = {column: max(content[column], view.fit_content.get(column, 0)) for column in columns}
    view.fit_titles, view.fit_content = titles, content
    view.fit_font = _fonts(view)
    view.fit_done = view.fit_done or rows
    apply(view)
    return time.perf_counter() - began


def _fonts(view):
    """The fonts the cells and titles are measured with (a style sheet's font comes with the view's first show)."""
    return view.font().key(), header_of(view).font().key()


def user_columns(view, titles=()):
    """The columns of VIEW (of TITLES) whose width the human set (dragged an edge, double-clicked one): kept."""
    if not getattr(view, "user_sized", False):
        return set()
    return set(getattr(view, "user_columns", None) or ()) & set(titles)


def _give(widths, floors, over):
    """Take OVER px from WIDTHS (in place), from each column in FLOORS ({column: px}) in proportion to what it has
    above its floor, never below it. Returns what could not be taken."""
    spare = {column: widths[column] - floor for column, floor in floors.items() if widths[column] > floor}
    total = sum(spare.values())
    if over <= 0 or total <= 0:
        return over
    share = min(1.0, over / total)
    for column, extra in spare.items():
        taken = min(extra, over, int(extra * share + 0.999))
        widths[column] -= taken
        over -= taken
    return max(0, over)


def plan(titles, content, room, stretch=None, compact=(), fixed=None):
    """({column: px}, squeezed) for a view of ROOM px: what each column gets (see the module). TITLES and CONTENT:
    {column: px} its title and its widest cell need; FIXED: {column: px} the human's widths, kept. SQUEEZED: even
    cut as far as they may be, the columns are wider than ROOM (the view scrolls sideways)."""
    fixed = dict(fixed or {})
    widths = {column: fixed.get(column, max(titles[column], content.get(column, 0))) for column in titles}
    free = [column for column in titles if column not in fixed]
    over = sum(widths.values()) - room
    if over <= 0:
        if stretch in free:
            widths[stretch] -= over  # the room left over
        return widths, False
    if stretch in free:  # 1. the long column first
        over = _give(widths, {stretch: max(titles[stretch], min(widths[stretch], STRETCH_MIN))}, over)
    cap = max(CAP_MIN, int(CAP * room))
    others = [column for column in free if column != stretch and column not in compact]
    # 2. the other long columns: down to a share of the view, then down to their titles
    over = _give(widths, {column: max(titles[column], min(widths[column], cap)) for column in others}, over)
    over = _give(widths, {column: titles[column] for column in others}, over)
    # 3. a short column only when its content is longer than a long column may be
    over = _give(widths, {column: max(titles[column], cap) for column in free if column in compact
                          and column != stretch}, over)
    return widths, over > 0


def apply(view):
    """The widths of VIEW's columns from what fit_columns measured, for the width the view has now."""
    header = header_of(view)
    stretch = stretch_of(view)
    titles, content = getattr(view, "fit_titles", {}), getattr(view, "fit_content", {})
    if not titles:
        _modes(view, header, stretch)
        return
    view.fitting = True
    try:
        human = user_columns(view, titles)
        laid_out = view.isVisible() and view.viewport().width() > 60
        # less the corner a (?) keeps free in the header (help_view.Corner.make_room)
        room = view.viewport().width() - header.viewportMargins().right() if laid_out else FALLBACK_WIDTH
        interactive = getattr(view, "stretch_interactive", False)
        fixed = {column: max(titles[column], header.sectionSize(column)) for column in human
                 if column != stretch or interactive}
        measured = getattr(view, "fit_measured", None) or content
        kept, now = (sum(fixed.get(c, need.get(c, 0)) for c in titles) for need in (content, measured))
        if kept > room >= now and not getattr(view, "fit_steady", False):
            # Wider than earlier rows needed only: the rows shown now are whole when those widths are given up (not
            # in a view with fit_steady: the Files page's panes are fitted to typical rows first, so that a change of
            # folder moves no column).
            content = measured
        widths, squeezed = plan(titles, content, room, stretch, getattr(view, "fit_compact", set()), fixed)
        squeezed = squeezed and laid_out
        _modes(view, header, stretch, squeezed)
        stretched = stretch in titles and not squeezed and not interactive  # Qt gives it the rest
        for column, width in widths.items():
            if column == stretch and stretched:
                continue
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
        if kind == QEvent.Type.Show and watched is view and not getattr(view, "fit_titles", None) \
                and view.model() is not None:
            # Shown before its first fill (or never filled: a listing that failed): fitted to its titles now that
            # the theme's font is on them, not left at Qt's 100 px each ("gent versio").
            fit_columns(view)
        elif kind in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.FontChange, QEvent.Type.StyleChange) \
                and getattr(view, "fit_titles", None) and not view.fitting:
            if getattr(view, "fit_font", None) != _fonts(view):
                # Measured with another font (filled before its first show, before the theme's style sheet, a
                # change of the font size): measured again, else its cells would be cut by the difference (widths
                # earlier rows needed are kept: a pane fitted to typical rows stays put).
                fit_columns(view)
            elif view.viewport().width() != getattr(view, "fit_room", None):
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
