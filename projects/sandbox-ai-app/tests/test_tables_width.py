"""Column widths when there is room (user report 2026-11-18: "On Credentials the table's default width is not sane.
When there is enough room everything should be readable. Check the other tables too."): every table and tree of every
page, filled with realistic rows (long names, long paths and URLs, German texts), in the real window at 1920x1080 and
1280x800 and at 800 px:

- when the sum of what each column's widest cell and title need fits, no cell is cut and the long column takes the
  rest;
- when it does not fit, the long column gives way first: a short (status, kind) column stays whole, a column other
  than the long one is cut only once the long one is down to its least;
- the rows arriving after the table was shown (fitted to its titles), a later wider window, widths the app (not the
  human) chose in a small window and kept in the state: none of them leaves a cut cell behind when there is room;
- widths the human dragged stay the human's, column by column.

The generic test walks every page, so a new table is checked without being named here."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, i18n, state, tables, widgets  # noqa: E402

from test_tables import get, show_tab_of  # noqa: E402
from test_ui import env  # noqa: E402,F401 - the isolated home of the UI tests

HOME = "/home/alexandra-musterfrau"
PATH = f"{HOME}/.local/share/sandbox-ai/auth/claude/.credentials.json"
URL = "https://git.internal.example-company.com:3080/organisation/repository-with-a-long-name.git"
TOKEN = "GITHUB_PERSONAL_ACCESS_TOKEN_FOR_PUSHING"
T = "2026-11-09 14:23:51"
# Realistic rows of every page's tables (German texts where a page translates them: they are the longer ones).
CREDENTIALS = (
    ("overview", [["Secret", TOKEN, "Schlüsselbund; Git-Login https://github.com, Identität arbeit", "gespeichert"],
                  ["Git-Login", "https://gitea.example-company.com", "alexandra, Secret GITEA_TOKEN", "bereit"],
                  ["Agent-Login", "claude", "sandbox-ai-Login-Speicher", "angemeldet"],
                  ["Identität", "arbeit", "Alexandra Musterfrau <alexandra@example-company.com>", "bereit"],
                  ["VPN", "home-wireguard", "wireguard Example Company IT", "eingerichtet"]]),
    ("logins", [["claude", "sandbox-ai-Login-Speicher", "angemeldet", PATH],
                ["codex", "die Anmeldung des Hosts", "abgelaufen, neu anmelden", f"{HOME}/.codex/auth.json"],
                ["opencode", "sandbox-ai-Login-Speicher", "keine Anmeldung", ""]]),
    ("secrets", [[TOKEN, "Schlüsselbund", "Git-Login https://github.com, Identität arbeit"],
                 ["GITEA_TOKEN", "Schlüsselbund", "Git-Login https://gitea.example-company.com"],
                 ["NPM_PUBLISH_TOKEN", "Datei", "Veröffentlichungs-Login npm"]]),
    ("git.list", [["https://gitea.example-company.com:3000", "alexandra", "GITEA_TOKEN", "bereit"],
                  ["https://github.com", "x-access-token", TOKEN, "Secret fehlt"]]),
    ("publish.list", [["npm", "registry.npmjs.org", "NPM_PUBLISH_TOKEN", "bereit"],
                      ["pypi", "upload.pypi.org", "PYPI_API_TOKEN_FOR_THE_ORGANISATION", "Secret fehlt"]]),
    ("identities.list", [["arbeit", "Alexandra Musterfrau <alexandra@example-company.com>",
                          "gitea.example-company.com, github.com", "npm, pypi"],
                         ["privat", "alex <alex@example.org>", "codeberg.org", "—"]]),
    ("vpn.profiles", [["home-wireguard-fritzbox", "wireguard", "Example Company IT"],
                      ["büro-openvpn", "openvpn", "—"]]),
)
OTHERS = (
    ("approvals", "always", [[f"{HOME}/projekte/sandbox-ai-workspace", "pypi.org, files.pythonhosted.org", "Host"]]),
    ("connections", "table", [[T, "erlaubt", "CONNECT", "200", f"{URL}?page={i}", 1234567 + i] for i in range(40)]),
    ("kits", "table", [["python-dev-full", "Kit", "claude, codex", "Skills: 4 · Plugins: 2 · Pakete: python3-dev",
                        "benutzt"]]),
    ("mcp", "servers", [["telegram-bridge-server", "läuft", "Werkzeuge: send_message, read_messages", "podman", "–"]]),
    ("images", "images", [["localhost/sandbox-ai-claude:latest", "claude", "2026-11-08 10:00", "python-dev",
                           "2.0.14", "2026-11-08", "2,3 GB", "sandbox-ai-claude-python-dev"]]),
    ("llm", "servers", [["ollama-workstation", "ollama", "http://192.168.178.42:11434/v1", "qwen2.5-coder:32b",
                         "OLLAMA_KEY", "home", "claude, codex"]]),
    ("team", "members", [["lead", "Leitung", "arbeitet"], ["claude-1", "Mitglied", "wartet auf Freigabe"]]),
    ("hosts", "table", [["build-server.internal.example-company.com", "ja", "ja", "0.1.99", "2", "0"]]),
)
SIZES = ((1920, 1080), (1280, 800))
# Pages whose tables are not checked by the walk, and why (none yet).
NOT_WALKED = {}
with_home = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


# -- what a column needs, what it got --

def cells(view):
    """[index] of every cell shown (a tree's folded rows and one-text rows left out)."""
    model, found, stack = view.model(), [], [QtCore.QModelIndex()]
    tree = isinstance(view, QtWidgets.QTreeView)
    while stack:
        parent = stack.pop()
        for row in range(model.rowCount(parent)):
            first = model.index(row, 0, parent)
            if tree and view.isFirstColumnSpanned(row, parent):
                continue
            if tree and parent.isValid() and not view.isExpanded(parent):
                continue
            for column in range(model.columnCount(parent)):
                if not tables.header_of(view).isSectionHidden(column):
                    found.append(first.siblingAtColumn(column))
            if tree:
                stack.append(first)
    return found


def hint(view, index):
    """px the cell's content needs (the delegate's size hint; a widget's in a cell); 0 for an empty cell."""
    widget = view.indexWidget(index)
    if widget is not None:
        return widget.sizeHint().width()
    if not view.model().data(index, QtCore.Qt.ItemDataRole.DisplayRole):
        return 0
    return view.sizeHintForIndex(index).width()


def cut(view, index):
    """px a cell lacks (0: shown whole): what it needs against its rectangle."""
    return max(0, hint(view, index) - view.visualRect(index).width())


def needs(view):
    """{column: px} each shown column needs: its title or its widest cell (with what the cell's rectangle lacks of
    its column: a grid line, a tree's indentation)."""
    header = tables.header_of(view)
    found = {column: tables._title(view, header, column) for column in range(header.count())
             if not header.isSectionHidden(column)}
    for index in cells(view):
        column = index.column()
        if hint(view, index):
            outside = header.sectionSize(column) - view.visualRect(index).width()
            found[column] = max(found[column], hint(view, index) + outside)
    return found


def room_of(view):
    return view.viewport().width() - tables.header_of(view).viewportMargins().right()


def check(view, where):
    """What is wrong with VIEW's widths (see the module), as lines."""
    header = tables.header_of(view)
    stretch = tables.stretch_of(view)
    compact = getattr(view, "fit_compact", set())
    human = tables.user_columns(view, range(header.count()))
    need = needs(view)
    room = room_of(view)
    found = []
    width = {column: header.sectionSize(column) for column in need}
    for column in need:
        if width[column] < tables._title(view, header, column) - (tables.SORT_ROOM if column == stretch else 0):
            found.append(f"{where}: column {column} {width[column]} px: its title needs "
                         f"{tables._title(view, header, column)}")
    if sum(need.values()) <= room:
        lacking = [(i.row(), i.column(), cut(view, i)) for i in cells(view) if cut(view, i) and i.column() not in human]
        if lacking:
            found.append(f"{where}: {sum(need.values())} px needed in {room}, yet cut (row, column, px): {lacking[:6]}")
        if stretch in need and abs(sum(width.values()) - room) > 2:
            found.append(f"{where}: the columns take {sum(width.values())} px of {room}: the rest is not the long "
                         "column's")
        return found
    cap = max(tables.CAP_MIN, int(tables.CAP * room))
    for column in need:
        if column in human or column == stretch:
            continue
        if column in compact and width[column] < min(need[column], cap):
            found.append(f"{where}: short column {column} cut ({width[column]} px of {need[column]}) in {room} px")
    if stretch in need and stretch not in human:
        least = min(need[stretch], max(tables.STRETCH_MIN, tables._title(view, header, stretch)))
        others_cut = [c for c in need if c not in (stretch, *human) and width[c] < need[c]]
        if others_cut and width[stretch] > least + 2:
            found.append(f"{where}: columns {others_cut} cut while the long column {stretch} has {width[stretch]} px "
                         f"(it gives way first, down to {least})")
    return found


def settle(qapp, rounds=4):
    for _ in range(rounds):
        qapp.processEvents()


@pytest.fixture
def window_for(qapp, env, monkeypatch):  # noqa: F811
    from sandbox_ai_app import terminal, ui
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    made = []

    def make(language, size):
        i18n.set_language(language)
        w = ui.MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
        if w.tray is not None:
            w.tray.hide()
            w.tray = None
        w.resize(*size)
        w.show()
        made.append(w)
        return w
    yield make
    for w in made:
        w.shutdown()
        w.close()
        w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    i18n.set_language("en")


def shown_view(qapp, window, page, path):
    window.show_page(page, raise_window=False)
    settle(qapp)
    view = get(getattr(window, page), path)
    show_tab_of(view)
    settle(qapp)
    return view


def fill(qapp, view, rows):
    """ROWS arrive (from the background, after the table was shown): fill() fits the columns."""
    count = view.model().columnCount()
    widgets.fill(view, [[*row, *[""] * count][:count] for row in rows])
    settle(qapp)


# -- the pages' tables, filled as they are --

@with_home
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("language", ["de", "en"])
def test_every_credentials_tab_is_readable_when_there_is_room(qapp, window_for, size, language):
    window = window_for(language, size)
    found, fitting = [], 0
    for path, rows in CREDENTIALS:
        view = shown_view(qapp, window, "credentials", path)
        assert view.isVisible(), path
        fill(qapp, view, rows)
        found += check(view, f"{size} {language} credentials {path}")
        fitting += sum(needs(view).values()) <= room_of(view)
    assert found == [], "\n".join(found)
    if size[0] >= 1920:
        assert fitting == len(CREDENTIALS), "at 1920 px every credentials table has room for all of its content"


@with_home
@pytest.mark.parametrize("size", [(1240, 840), (1920, 1080)], ids=lambda s: f"{s[0]}x{s[1]}")
def test_credentials_filled_while_their_tabs_are_hidden_are_whole_when_shown(qapp, window_for, size):
    """As the page's refresh does: every tab's rows arrive while the Overview is shown (the window's first size
    first); each tab shown later is fitted for its width - the Overview's Kind no longer kept at the width the rows
    read before needed, cutting the long column."""
    window = window_for("de", size)
    window.show_page("credentials", raise_window=False)
    settle(qapp)
    window.credentials.tabs.setCurrentIndex(0)
    settle(qapp)
    for path, rows in CREDENTIALS:
        view = get(window.credentials, path)
        count = view.model().columnCount()
        widgets.fill(view, [[*row, *[""] * count][:count] for row in rows])
    settle(qapp)
    found = []
    for path, _rows in CREDENTIALS:
        view = shown_view(qapp, window, "credentials", path)
        found += check(view, f"{size} credentials {path} filled hidden")
    assert found == [], "\n".join(found)


@with_home
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_the_other_pages_tables_are_readable_when_there_is_room(qapp, window_for, size):
    window = window_for("de", size)
    found = []
    for page, path, rows in OTHERS:
        view = shown_view(qapp, window, page, path)
        if not view.isVisible():
            found.append(f"{page} {path}: not shown")
            continue
        fill(qapp, view, rows)
        found += check(view, f"{size} {page} {path}")
    assert found == [], "\n".join(found)


@with_home
def test_at_800_px_the_short_columns_stay_whole_and_the_long_one_is_cut(qapp, window_for):
    window = window_for("de", (800, 600))
    settle(qapp)
    # (800 px, or the window's least width when its parts need more: 850 with some fonts)
    assert window.width() <= max(800, window.minimumSizeHint().width(), window.minimumWidth()) <= 900
    found, long_cut = [], []
    for path, rows in CREDENTIALS:
        view = shown_view(qapp, window, "credentials", path)
        fill(qapp, view, rows)
        found += check(view, f"800 credentials {path}")
        stretch = tables.stretch_of(view)
        if sum(needs(view).values()) > room_of(view):
            long_cut.append(tables.header_of(view).sectionSize(stretch) < needs(view)[stretch])
    assert found == [], "\n".join(found)
    assert long_cut and all(long_cut), "a table without room cuts its long column"
    # Wide again: everything whole again (nothing kept from the narrow window).
    window.resize(1920, 1080)
    settle(qapp, 8)
    for path, _rows in CREDENTIALS:
        view = shown_view(qapp, window, "credentials", path)
        found += check(view, f"800 then 1920 credentials {path}")
    assert found == [], "\n".join(found)


# -- every table of every page, new ones too --

def generic_rows(view, count=4):
    """Rows of realistic texts for VIEW's columns: a short German state in a short column, a long path in the long
    one, a long name in the others."""
    header = tables.header_of(view)
    stretch, compact = tables.stretch_of(view), getattr(view, "fit_compact", set())
    rows = []
    for row in range(count):
        cells_ = []
        for column in range(header.count()):
            if column in compact:
                cells_.append(["läuft", "wartet auf Freigabe", "2026-11-09 14:23", "1.234.567"][(row + column) % 4])
            elif column == stretch:
                cells_.append(f"{HOME}/projekte/sandbox-ai-workspace/src/modul-{row}/eine-längere-datei.py")
            else:
                cells_.append(f"ein-recht-langer-name-nummer-{row}-{column}")
        rows.append(cells_)
    return rows


def generic_fill(qapp, view):
    """Fill VIEW (a table widget, a table of many rows, a tree widget) with generic_rows; False for another model."""
    rows = generic_rows(view)
    if isinstance(view, (QtWidgets.QTableWidget, widgets.ModelTable)):
        widgets.fill(view, rows)
    elif isinstance(view, QtWidgets.QTreeWidget):
        view.clear()
        for row in rows:
            item = QtWidgets.QTreeWidgetItem(row)
            item.addChild(QtWidgets.QTreeWidgetItem(row))
            view.addTopLevelItem(item)
        view.expandAll()
        tables.fit_columns(view, force=True)
    else:
        return False
    settle(qapp)
    return True


def fitted_views(window, page):
    for view in getattr(window, page).findChildren(QtWidgets.QAbstractItemView):
        if getattr(view, "fit_watcher", None) is None or view.model() is None:
            continue
        if tables.header_of(view).isHidden() or view.model().columnCount() < 2:
            continue
        yield view


@with_home
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_every_table_of_every_page_is_readable_when_there_is_room(qapp, window_for, size):
    from sandbox_ai_app import ui
    window = window_for("de", size)
    found, checked, filled = [], [], 0
    for page, _ in ui.PAGES:
        if page in NOT_WALKED:
            continue
        window.show_page(page, raise_window=False)
        settle(qapp)
        for view in fitted_views(window, page):
            show_tab_of(view)
            shown = reveal(view)  # a part of the page shown only with something to show (bridges, terminals)
            settle(qapp)
            if view.isVisible() and view.viewport().width() >= 100:
                filled += generic_fill(qapp, view)
                found += check(view, f"{size} {page} {view.accessibleName() or type(view).__name__}")
                checked.append(f"{page}:{getattr(view, 'state_key', None) or type(view).__name__}")
            for widget in reversed(shown):
                widget.hide()
            settle(qapp)
    assert found == [], "\n".join(found)
    assert len(checked) >= 50 and filled >= 45, checked


def reveal(view):
    """Show the hidden parts of the page VIEW is in (and VIEW): [what was shown], to hide again."""
    shown, widget = [], view
    while widget is not None and not widget.isWindow():
        if widget.isHidden():
            widget.show()
            shown.append(widget)
        widget = widget.parentWidget()
    return shown


def test_the_model_pickers_table_is_fitted_too(qapp, monkeypatch):
    """The model picker's table sized its columns with Qt's resizeColumnsToContents and stretched the model's name:
    long tags left the name a sliver. Now fitted as every table (tables.py)."""
    from sandbox_ai_app import wizard_llm as WL
    from sandbox_ai_app.model_picker import ModelPicker
    from test_model_picker import servers
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    picker = ModelPicker(None, WL.picker_rows(servers(), "codex"), "home:llava:13b")
    picker.resize(1200, 520)
    picker.show()
    settle(qapp)
    assert getattr(picker.table, "fit_watcher", None) is not None
    assert check(picker.table, "model picker") == [] and sum(needs(picker.table).values()) <= room_of(picker.table)
    picker.resize(520, 520)
    settle(qapp)
    assert check(picker.table, "model picker narrow") == []
    picker.close()


# -- the flows that froze or cut a layout --

def test_rows_arriving_after_the_first_show_are_whole(qapp):
    """Shown before its rows (fitted to its titles), filled from the background later: whole when there is room."""
    view = widgets.table(["Art", "Name", "Wo / benutzt von", "Zustand"], stretch=2, compact=(0, 3))
    view.resize(1400, 300)
    view.show()
    settle(qapp)
    fill(qapp, view, CREDENTIALS[0][1])
    assert sum(needs(view).values()) <= room_of(view)
    assert check(view, "late rows") == []
    view.close()


def test_rows_measured_before_the_font_changed_are_measured_again(qapp):
    """A dialog fills its table before its first show; the theme's style sheet (or a font size chosen later) brings
    another font: the cells measured with the old one were cut by the difference (the model picker, full suite)."""
    view = widgets.table(["Model", "Tags", "Parameters", "Size"], stretch=0, compact=(2, 3))
    fill(qapp, view, [["home:qwen2.5-coder:32b", "[text] [code] [reasoning] [vision]", "32.8B", "19.0 GB"]])
    font = view.font()
    font.setPointSizeF(font.pointSizeF() * 1.4)
    view.setFont(font)
    view.horizontalHeader().setFont(font)
    view.resize(1200, 300)
    view.show()
    settle(qapp)
    assert sum(needs(view).values()) <= room_of(view) and check(view, "bigger font") == []
    font.setPointSizeF(font.pointSizeF() * 1.3)  # and while shown
    view.setFont(font)
    settle(qapp)
    assert check(view, "bigger still") == []
    view.close()


def test_a_long_column_beside_the_long_one_is_not_capped_when_there_is_room(qapp):
    """The Overview's Name (a URL) beside its long "Where" column: once capped at 40 % of the view and cut while the
    long column had room to spare."""
    view = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    view.resize(1000, 300)
    view.show()
    fill(qapp, view, [["Git login", URL, "x-access-token, secret T", "ok"]])
    header = view.horizontalHeader()
    assert check(view, "url") == []
    assert header.sectionSize(1) > 0.4 * room_of(view)
    view.close()


def test_without_room_the_long_column_gives_way_first(qapp):
    view = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    view.resize(700, 300)
    view.show()
    fill(qapp, view, [["Git login", URL, f"{PATH} and more", "expired, log in again"]])
    header, need = view.horizontalHeader(), needs(view)
    assert sum(need.values()) > room_of(view)
    assert check(view, "narrow") == []
    assert header.sectionSize(0) >= need[0] and header.sectionSize(3) >= need[3]  # the short ones whole
    assert header.sectionSize(2) < need[2]  # the long one cut
    view.resize(260, 300)  # narrower than the short ones and titles: it scrolls, the short ones stay whole
    settle(qapp)
    assert header.sectionSize(0) >= need[0] and header.sectionSize(3) >= need[3]
    assert view.horizontalScrollBar().maximum() > 0
    view.resize(1600, 300)
    settle(qapp)
    assert check(view, "wide again") == [] and sum(needs(view).values()) <= room_of(view)
    view.close()


def test_the_plan(qapp):
    titles = {0: 40, 1: 40, 2: 60, 3: 40}
    content = {0: 80, 1: 500, 2: 400, 3: 120}
    widths, squeezed = tables.plan(titles, content, 1500, stretch=2, compact={0, 3})
    assert widths == {0: 80, 1: 500, 2: 800, 3: 120} and not squeezed  # all whole, the rest to the long one
    widths, squeezed = tables.plan(titles, content, 1000, stretch=2, compact={0, 3})
    assert widths == {0: 80, 1: 500, 2: 300, 3: 120} and not squeezed  # the long one gives way first
    widths, squeezed = tables.plan(titles, content, 700, stretch=2, compact={0, 3})
    assert widths[2] == tables.STRETCH_MIN and widths[0] == 80 and widths[3] == 120 and widths[1] < 500
    assert sum(widths.values()) == 700 and not squeezed
    widths, squeezed = tables.plan(titles, content, 250, stretch=2, compact={0, 3})
    assert widths == {0: 80, 1: 40, 2: tables.STRETCH_MIN, 3: 120} and squeezed  # titles and short ones: it scrolls
    widths, _ = tables.plan(titles, content, 1500, stretch=2, compact={0, 3}, fixed={1: 90})
    assert widths[1] == 90 and widths[2] == 1500 - 80 - 90 - 120  # the human's width kept


def test_widths_the_app_kept_from_a_small_window_are_fitted_again(qapp, tmp_path, monkeypatch):
    """The state keeps the header (its sort order) without the human sizing anything: the next start, in a wide
    window, fits the columns afresh - a narrow layout is never replayed."""
    path = tmp_path / "state.json"
    rows = CREDENTIALS[0][1]
    first = state.State(path, delay=10)
    view = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    first.table("credentials-overview", view)
    view.resize(420, 300)
    view.show()
    fill(qapp, view, rows)
    view.sortByColumn(1, QtCore.Qt.SortOrder.AscendingOrder)  # saves the header: by the app
    assert first.flush() and not first.data["tables"]["credentials-overview"]["sized"]
    view.close()
    again = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    state.State(path, delay=10).table("credentials-overview", again)
    again.resize(1500, 300)
    again.show()
    fill(qapp, again, rows)
    assert not again.user_sized and check(again, "wide again") == []
    again.close()


def test_a_column_the_human_dragged_is_kept_and_only_it(qapp, tmp_path, monkeypatch):
    """The human drags one column in a small window: that width comes back; the others are fitted for the window the
    app has now (they once all came back as the small window had them)."""
    path = tmp_path / "state.json"
    rows = CREDENTIALS[0][1]
    first = state.State(path, delay=10)
    view = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    first.table("credentials-overview", view)
    view.resize(500, 300)
    view.show()
    fill(qapp, view, rows)
    monkeypatch.setattr(state, "dragging", lambda: True)
    view.horizontalHeader().resizeSection(0, 150)  # the human
    monkeypatch.setattr(state, "dragging", lambda: False)
    assert view.user_columns == {0}
    assert first.flush() and first.data["tables"]["credentials-overview"]["columns"] == [0]
    view.close()
    again = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    state.State(path, delay=10).table("credentials-overview", again)
    again.resize(1500, 300)
    again.show()
    fill(qapp, again, rows)
    header = again.horizontalHeader()
    assert again.user_sized and again.user_columns == {0} and header.sectionSize(0) == 150
    assert check(again, "the human's column and the fitted ones") == []
    again.resize(1600, 300)  # the window: the human's column stays, the others are fitted again
    settle(qapp)
    assert header.sectionSize(0) == 150 and check(again, "wider") == []
    again.close()


def test_a_small_layout_an_older_version_froze_is_fitted_again(qapp, tmp_path):
    """"sized" without "columns" (a version before 0.1.99; before 0.1.79 a splitter or the window moving set it too):
    every width of a small window came back for good - the likely cause of the Credentials tables' report. Such a
    table is fitted again; a column the human drags from now on is kept."""
    path = tmp_path / "state.json"
    view = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    for column, width in enumerate((40, 60, 120, 40)):
        view.horizontalHeader().resizeSection(column, width)
    old = {"header": bytes(view.horizontalHeader().saveState().toBase64()).decode(), "sized": True, "sort": 1,
           "order": 0}
    path.write_text(__import__("json").dumps({"tables": {"credentials-overview": old}}))
    kept = state.State(path, delay=10)
    again = widgets.table(["Kind", "Name", "Where / used by", "State"], stretch=2, compact=(0, 3))
    kept.table("credentials-overview", again)
    again.resize(1500, 300)
    again.show()
    fill(qapp, again, CREDENTIALS[0][1])
    assert not again.user_sized and check(again, "an older version's state") == []
    assert again.horizontalHeader().sortIndicatorSection() == 1  # the sort order is still kept
    again.remember_state()
    assert "columns" not in kept.data["tables"]["credentials-overview"] or \
        not kept.data["tables"]["credentials-overview"]["sized"]
    view.deleteLater()
    again.close()
