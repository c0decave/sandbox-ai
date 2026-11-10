"""Column widths (tables.py): every table of the pages, filled with long names, URLs and paths, in English and
German. No column is narrower than its title, a short (compact) column is not wider than its content needs, the long
column takes the rest, the view scrolls sideways only when the titles and short columns alone are wider than it;
widths the human dragged come back with the page (and only they: a splitter or the window moving the long column
is not the human sizing it); 5000 rows are fitted in well under 50 ms."""
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from sandbox_ai_app import backend, i18n, state, tables, theme, widgets  # noqa: E402

from test_ui import env  # noqa: E402,F401 - the isolated home of the UI tests

LONG = "/home/alexandra-musterfrau/c0de/projects/very-long-project-name/sandbox-ai-workspace/src/module"
URL = "https://git.internal.example-company.com:3080/organisation/repository-with-a-long-name.git"
T = "2026-11-09 14:23:51"
# (page, attribute path, rows): every page's table that fill() fills
DATA = (
    ("credentials", "overview", [["Secret", "GITHUB_PERSONAL_ACCESS_TOKEN_FOR_PUSHING", "keyring · used by git-logins "
                                  "github.com, identities work", "ok"], ["Git login", URL, "x-access-token", "missing secret"]]),
    ("credentials", "logins", [["claude", "store", "logged in", "~/.local/share/sandbox-ai/auth/claude/.credentials.json"],
                               ["opencode", "host", "missing", ""]]),
    ("credentials", "secrets", [["GITHUB_PERSONAL_ACCESS_TOKEN_FOR_PUSHING", "keyring", "git-logins " + URL]]),
    ("credentials", "git.list", [[URL, "x-access-token", "GITEA_PERSONAL_ACCESS_TOKEN", "ok"]]),
    ("credentials", "publish.list", [["npm", "registry.npmjs.org", "NPM_PUBLISH_TOKEN_FOR_ORGANISATION", "missing secret"]]),
    ("credentials", "vpn.profiles", [["home-wireguard-fritzbox", "wireguard", "Example Company IT"]]),
    ("approvals", "open", [["claude-abc123-very-long-sandbox-name", "connection", URL, T, "proxy", "this machine"]]),
    ("approvals", "history", [[T, "claude-abc123", URL, "allowed", "once", "asked by the human in the dialog"]]),
    ("approvals", "always", [[LONG, "pypi.org", "host"]]),
    ("connections", "table", [[T, "allowed", "CONNECT", "200", f"{URL}?page={i}", 1234567 + i] for i in range(300)]),
    ("kits", "table", [["python-dev-full", "kit", "claude, codex", "skills: 4 · plugins: 2 · packages: apt python3-dev",
                        "in use"]]),
    ("mcp", "servers", [["telegram-bridge-server", "running", "tools: send_message, read_messages", "podman", "mcp:x"]]),
    ("mcp", "catalog", [["hub:axross/filesystem-readonly", "none", "Read-only access to a folder", "2026-10-01",
                         "signed by axross (ed25519)", "axross-mcp-hub"]]),
    ("images", "images", [["localhost/sandbox-ai-claude:latest", "claude", "2026-11-08 10:00", "python-dev", "2.0.14",
                           "2026-11-08", "2.3 GB", "sandbox-ai-claude-python-dev-variant"]]),
    ("team", "board", [["Implement the login page", "lead", "claude-1", "open", "What to do and why it matters " * 3]]),
    ("llm", "servers", [["ollama-workstation", "ollama", "http://192.168.178.42:11434/v1", "qwen2.5-coder:32b-instruct",
                         "OLLAMA_KEY", "home", "claude, codex"]]),
    ("files", "left.table", [[f"a-rather-long-file-name-number-{i}.py", "file", 12345 * i, T, "", "alexandra:users",
                              "-rw-r--r--"] for i in range(300)]),
)
PAD = 30  # px a compact column may have beyond its widest cell or title (margins, grid)


def get(obj, path):
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def show_tab_of(view):
    parent = view.parentWidget()
    while parent is not None:
        if isinstance(parent, QtWidgets.QTabWidget):
            for index in range(parent.count()):
                if parent.widget(index).isAncestorOf(view):
                    parent.setCurrentIndex(index)
        parent = parent.parentWidget()


def title_text(view, column):
    """px of the title's text alone (no margins): no column may be narrower."""
    header = tables.header_of(view)
    text = view.model().headerData(column, QtCore.Qt.Orientation.Horizontal) or ""
    return QtGui.QFontMetrics(header.font()).horizontalAdvance(str(text))


def needed(view, column):
    """px the widest cell or the title of COLUMN asks for, as Qt measures it."""
    return max(view.sizeHintForColumn(column), tables.header_of(view).sectionSizeHint(column))


def findings(view, where):
    header = tables.header_of(view)
    stretch = tables.stretch_of(view)
    compact = getattr(view, "fit_compact", set())
    found = []
    for column in range(header.count()):
        if header.isSectionHidden(column):
            continue  # a column hidden while it has nothing to say (the Kits table's "This project": review 6)
        width = header.sectionSize(column)
        if width < title_text(view, column):
            found.append(f"{where}: column {column} {width} px, its title needs {title_text(view, column)}")
        if column in compact and column != stretch and width > needed(view, column) + PAD:
            found.append(f"{where}: compact column {column} {width} px, needs {needed(view, column)}")
    total = sum(header.sectionSize(column) for column in range(header.count()))
    room = view.viewport().width() - header.viewportMargins().right()  # less a (?)'s corner (review 6 L2)
    if view.horizontalScrollBar().isVisible():
        least = sum(header.sectionSize(c) for c in range(header.count()) if c != stretch) + tables.STRETCH_MIN
        # (a bar still shown while the columns take exactly the room is Qt's update to come, seen under load)
        if least <= room and stretch is not None and total > room:
            found.append(f"{where}: scrolls sideways ({total} px in {room}) though the columns fit")
    elif stretch is not None and abs(total - room) > 2:
        found.append(f"{where}: the columns take {total} px of {room}: the stretch column does not take the rest")
    return found


@pytest.fixture
def window_for(qapp, env, monkeypatch):  # noqa: F811
    from sandbox_ai_app import terminal, ui
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    made = []

    def make(language):
        i18n.set_language(language)
        w = ui.MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
        if w.tray is not None:
            w.tray.hide()
            w.tray = None
        w.resize(1240, 840)  # the window's first size
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


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
@pytest.mark.parametrize("language", ["en", "de"])
def test_every_table_of_the_pages_fits_its_titles_and_content(qapp, window_for, language):
    window = window_for(language)
    found = []
    for page, path, rows in DATA:
        window.show_page(page, raise_window=False)
        qapp.processEvents()
        view = get(getattr(window, page), path)
        show_tab_of(view)
        width = view.columnCount()
        widgets.fill(view, [[*row, *[""] * width][:width] for row in rows])
        for _ in range(3):
            qapp.processEvents()
        # The rows shown alone (a page may have read its own before or after: a later fill keeps columns wider).
        tables.fit_columns(view, force=True)
        assert getattr(view, "fit_watcher", None) is not None, f"{page} {path}: not fitted by tables.py"
        found += findings(view, f"{language} {page} {path}")
    assert found == [], "\n".join(found)


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
def test_every_table_of_the_pages_is_kept_for_the_next_start(qapp, window_for):
    window = window_for("en")
    kept = window.state.section("tables")
    keys = set()
    for page, _ in __import__("sandbox_ai_app.ui", fromlist=["PAGES"]).PAGES:
        for view in getattr(window, page).findChildren(QtWidgets.QAbstractItemView):
            if getattr(view, "fit_watcher", None) is not None:
                key = getattr(view, "state_key", None)
                assert key, f"{page}: {view.accessibleName()} is not registered with the state"
                keys.add(key)
    assert {"credentials-git-list", "credentials-publish-list", "credentials-overview", "hosts-table",
            "llm-servers"} <= keys
    assert isinstance(kept, dict)


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
def test_every_table_with_columns_is_fitted_by_tables_py(qapp, window_for):
    from sandbox_ai_app import ui
    window = window_for("en")
    missing, fitted = [], []
    for page, _ in ui.PAGES:
        for view in getattr(window, page).findChildren(QtWidgets.QAbstractItemView):
            if not isinstance(view, (QtWidgets.QTableView, QtWidgets.QTreeView)) or view.model() is None:
                continue
            if tables.header_of(view).isHidden() or view.model().columnCount() < 2:
                continue  # a list (a tree without titles)
            if view.wordWrap():
                continue  # the doctor's checks: two columns of wrapped text share the width on purpose
            if getattr(view, "fit_watcher", None) is None:
                missing.append(f"{page}: {view.accessibleName() or type(view).__name__}")
            else:
                fitted.append(view)
    assert missing == [] and len(fitted) >= 25


def test_the_long_column_takes_the_rest_and_short_ones_their_content(qapp):
    view = widgets.table(["Kind", "Name", "Path", "State"], stretch=2, compact=(0, 3))
    view.resize(900, 300)
    view.show()
    widgets.fill(view, [["Secret", "GITHUB_TOKEN", LONG, "ok"], ["Git login", "x", URL, "missing secret"]])
    qapp.processEvents()
    header = view.horizontalHeader()
    assert findings(view, "table") == []
    assert header.sectionResizeMode(2) == QtWidgets.QHeaderView.ResizeMode.Stretch
    assert header.sectionSize(2) > header.sectionSize(1) > header.sectionSize(0)
    # A narrow view: the others give way first, then the long column keeps its least and the view scrolls.
    view.resize(260, 300)
    qapp.processEvents()
    assert header.sectionSize(2) >= min(tables.STRETCH_MIN, needed(view, 2))
    assert view.horizontalScrollBar().maximum() > 0
    view.resize(900, 300)
    qapp.processEvents()
    assert findings(view, "table again wide") == []
    view.close()


def test_a_cut_cell_shows_its_full_text_on_hover(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(tables, "QToolTip", type("Tip", (), {"showText": staticmethod(lambda *a: shown.append(a[1])),
                                                              "hideText": staticmethod(lambda: shown.append(None))}))
    view = widgets.table(["Name", "Where"], stretch=1)
    view.resize(400, 200)
    view.show()
    widgets.fill(view, [["a-very-long-name-" * 6, "x"], ["short", "y"]])
    qapp.processEvents()
    for row in (0, 1):
        rect = view.visualRect(view.model().index(row, 0))
        event = QtGui.QHelpEvent(QtCore.QEvent.Type.ToolTip, rect.center(), view.viewport().mapToGlobal(rect.center()))
        QtWidgets.QApplication.sendEvent(view.viewport(), event)
    assert len(shown) == 2 and "a-very-long-name-" in shown[0] and shown[1] is None
    view.close()


def test_later_fills_only_widen_and_force_fits_again(qapp):
    view = widgets.table(["Name", "State", "Path"], stretch=2, compact=(1,))
    view.resize(900, 300)
    view.show()
    widgets.fill(view, [["x", "running, asking the human", "/"]])
    qapp.processEvents()
    wide = view.horizontalHeader().sectionSize(1)
    widgets.fill(view, [["x", "ok", "/"]])  # a refresh while the human reads: nothing jumps
    assert view.horizontalHeader().sectionSize(1) == wide
    widgets.fill(view, [["x", "running, asking the human, and paused for a while", "/"]])
    assert view.horizontalHeader().sectionSize(1) > wide
    wider = view.horizontalHeader().sectionSize(1)
    widgets.fill(view, [["x", "ok", "/"]])
    assert view.horizontalHeader().sectionSize(1) == wider
    widgets.fit_columns(view, force=True)  # from scratch: as wide as "ok" (or the title) needs
    assert view.horizontalHeader().sectionSize(1) < wide
    view.close()


@pytest.mark.parametrize("many", [False, True])
def test_5000_rows_are_fitted_in_well_under_50_ms(qapp, many):
    if many and not hasattr(widgets, "ModelTable"):
        pytest.skip("no ModelTable (a model for many rows) in this version")
    view = widgets.table(["Time", "Event", "Method", "Status", "Target", "Bytes"], stretch=4, compact=(0, 1, 2, 3, 5),
                         numeric=(5,), **({"many": True} if many else {}))
    view.resize(1000, 400)
    view.show()
    widgets.fill(view, [[T, "allowed", "CONNECT", "200", f"{URL}/{i}" * (1 + i % 3), i * 1000] for i in range(5000)])
    qapp.processEvents()
    took = min(tables.fit_columns(view, force=True) for _ in range(3))
    assert took < 0.05, f"{took * 1000:.1f} ms for 5000 rows"
    view.close()


def test_the_widths_the_human_drags_come_back_and_only_they(qapp, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    rows = [["claude", "store", "logged in", LONG]]

    def page(saved):
        view = widgets.table(["Agent", "Login from", "State", "File"], stretch=3, compact=(0, 1, 2))
        view.resize(900, 300)
        saved.table("credentials-logins", view)
        view.show()
        widgets.fill(view, rows)
        qapp.processEvents()
        return view

    first = state.State(path, delay=10)
    view = page(first)
    header = view.horizontalHeader()
    monkeypatch.setattr(state, "dragging", lambda: True)  # a mouse button is down from here on
    view.resize(700, 300)  # a splitter or the window: the long column follows, the human sized nothing
    qapp.processEvents()
    assert not getattr(view, "user_sized", False)
    header.resizeSection(0, 230)  # the human drags the first column's edge
    assert view.user_sized
    assert first.flush()
    view.close()
    monkeypatch.setattr(state, "dragging", lambda: False)
    again = page(state.State(path, delay=10))
    assert again.user_sized and again.horizontalHeader().sectionSize(0) == 230
    widgets.fill(again, rows + [["codex", "a much longer place to log in from", "expired", "x"]])
    assert again.horizontalHeader().sectionSize(0) == 230  # still the human's
    again.close()


def test_a_kept_width_narrower_than_a_title_in_another_language_is_widened(qapp, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    saved = state.State(path, delay=10)
    view = widgets.table(["Agent", "Login from", "File"], stretch=2)
    saved.table("t", view)
    view.show()
    widgets.fill(view, [["claude", "store", LONG]])
    monkeypatch.setattr(state, "dragging", lambda: True)
    view.horizontalHeader().resizeSection(1, 40)
    monkeypatch.setattr(state, "dragging", lambda: False)
    saved.flush()
    view.close()
    i18n.set_language("de")
    try:
        other = widgets.table([i18n.tr("Agent"), "Angemeldet von (ein langer Titel)", i18n.tr("File")], stretch=2)
        state.State(path, delay=10).table("t", other)
        other.show()
        widgets.fill(other, [["claude", "store", LONG]])
        assert other.user_sized
        assert other.horizontalHeader().sectionSize(1) >= title_text(other, 1)
        other.close()
    finally:
        i18n.set_language("en")


def test_trees_are_fitted_too(qapp):
    from sandbox_ai_app.start_tree import StartTree
    tree = StartTree(["Kind", "Name", "Project", "State"], stretch=2, compact=(0, 3))
    tree.resize(800, 300)
    tree.show()
    host = tree.add_row(None, "h", "host", ["host", "build-server.internal.example-company.com", "", ""])
    tree.add_row(host, "s", "start", ["start", "claude-abc", LONG, "running, asking"], key="claude-abc")
    tree.expandAll()
    widgets.fit_columns(tree)
    qapp.processEvents()
    assert findings(tree, "tree") == []
    header = tree.header()
    # the indented start row's kind fits
    assert header.sectionSize(0) >= QtGui.QFontMetrics(tree.font()).horizontalAdvance("start") + tree.indentation()
    tree.close()


def test_keep_tables_names_them_by_page_and_attribute(qapp, tmp_path):
    page = QtWidgets.QWidget()
    page.box = QtWidgets.QGroupBox(page)
    page.box.list = widgets.table(["A", "B"], stretch=1)
    page.box.list.setParent(page.box)
    page.own = widgets.table(["A"], stretch=0)
    page.own.setParent(page)
    saved = state.State(tmp_path / "state.json", delay=10)
    assert tables.keep_tables(saved, {"demo": page}) == 2
    assert {page.box.list.state_key, page.own.state_key} == {"demo-box-list", "demo-own"}
    assert tables.keep_tables(saved, {"demo": page}) == 0  # once


def test_the_themes_keep_titles_whole(qapp):
    for mode in ("light", "dark", "win311"):
        theme.apply(mode, qapp)
        view = widgets.table(["Agent version", "Created", "Name"], stretch=2, compact=(0, 1))
        view.resize(700, 200)
        view.show()
        widgets.fill(view, [["2.0.14", "2026-11-08", "x"]])
        qapp.processEvents()
        assert findings(view, mode) == []
        view.close()
    theme.apply("system", qapp)
