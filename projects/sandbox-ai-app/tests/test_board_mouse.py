"""The Board's canvas and the mouse, offscreen: the right and the middle button drag the view (nothing on the board
moves, nothing is selected, no step of Undo), a right click opens a context menu (the canvas's or the node's), the
left button selects, moves and draws the rubber band; a node's text wraps to at most three lines inside the node;
the inspector slides out when a part is selected (or, as a window of its own, opens again); the score and the
findings are shown only on request."""
import gc
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QFontMetricsF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from sandbox_ai_app import backend, help_view, i18n, widgets  # noqa: E402
from sandbox_ai_app.board_view import NODE_H, NODE_PAD, NODE_W, TEXT_LINES  # noqa: E402

from test_projects import close, env, make_window, window  # noqa: E402,F401 - shared fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

RIGHT, MIDDLE, LEFT = Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton
NONE = Qt.KeyboardModifier.NoModifier


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(help_view, "OPENED", [])
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    view.menus = []  # the context menus opened (instead of running them)
    monkeypatch.setattr(view.view, "open_menu", lambda menu, point: view.menus.append(menu))
    yield view
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def zoomed(page, qapp, off_grid=True):
    """Zoomed in (the view can scroll both ways) and scrolled to the middle; the nodes off the grid (a project file
    written by hand, another grid setting): a click must not snap them."""
    if off_grid:
        for node in page.board.nodes:
            node.x, node.y = node.x + 7, node.y + 3
        page.rebuild()
    page.view.zoom(3.0)
    qapp.processEvents()
    for bar in (page.view.horizontalScrollBar(), page.view.verticalScrollBar()):
        assert bar.maximum() > 200, "the view must be able to scroll for this test"
        bar.setValue(bar.maximum() // 2)
    qapp.processEvents()


def empty_point(page):
    viewport = page.view.viewport()
    for x in range(30, viewport.width() - 30, 15):
        for y in range(30, viewport.height() - 30, 15):
            if not page.view.items(QPoint(x, y)) and all(not page.view.items(QPoint(x + dx, y + dy))
                                                         for dx in (-20, 20) for dy in (-20, 20)):
                return QPoint(x, y)
    raise AssertionError("no empty place on the canvas")


def looks(page):
    view = page.view
    return {"scroll": (view.horizontalScrollBar().value(), view.verticalScrollBar().value()),
            "zoom": view.transform().m11(), "nodes": [(n.id, n.x, n.y) for n in page.board.nodes],
            "items": {k: (i.pos().x(), i.pos().y()) for k, i in page.items.items()},
            "undo": page.undo.count(), "selected": sorted(map(id, page.scene.selectedItems()))}


def drag(page, qapp, button, start, delta, steps=6):
    viewport = page.view.viewport()
    QTest.mousePress(viewport, button, NONE, start)
    for step in range(1, steps + 1):
        QTest.mouseMove(viewport, start + QPoint(delta.x() * step // steps, delta.y() * step // steps))
        qapp.processEvents()
    QTest.mouseRelease(viewport, button, NONE, start + delta)
    qapp.processEvents()


def texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def action(menu, text):
    return next(a for a in menu.actions() if a.text() == text)


@pytest.mark.parametrize("button", [RIGHT, MIDDLE])
def test_the_right_and_middle_button_drag_the_view_and_move_nothing(page, qapp, button):
    zoomed(page, qapp)
    page.items[page.board.first("agent").id].setSelected(True)
    before = looks(page)
    start = empty_point(page)
    drag(page, qapp, button, start, QPoint(-60, -40))
    after = looks(page)
    assert after["scroll"] == (before["scroll"][0] + 60, before["scroll"][1] + 40), "the view follows the pointer"
    assert {k: v for k, v in after.items() if k != "scroll"} == {k: v for k, v in before.items() if k != "scroll"}
    assert page.menus == [], "a drag is not a click: no menu"
    assert page.view.pan is None and page.scene.pressed is None


def test_a_right_drag_that_starts_on_a_node_pans_too(page, qapp):
    zoomed(page, qapp)
    agent = page.items[page.board.first("agent").id]
    before = looks(page)
    drag(page, qapp, RIGHT, page.view.mapFromScene(agent.sceneBoundingRect().center()), QPoint(50, 30))
    after = looks(page)
    assert after["scroll"] == (before["scroll"][0] - 50, before["scroll"][1] - 30)
    assert after["nodes"] == before["nodes"] and after["undo"] == before["undo"] and page.menus == []


def test_a_right_click_on_the_canvas_opens_its_menu_and_moves_nothing(page, qapp):
    zoomed(page, qapp)
    before = looks(page)
    point = empty_point(page)
    QTest.mouseClick(page.view.viewport(), RIGHT, NONE, point)
    qapp.processEvents()
    assert looks(page) == before, "a right click moves, snaps and selects nothing"
    assert len(page.menus) == 1
    menu = page.menus[0]
    assert texts(menu) == ["Add a part here", "Select all", "Fit the board", "Zoom 100 %", "Arrange", "Undo", "Redo",
                           "What is this?"]
    assert not action(menu, "Undo").isEnabled()
    parts = action(menu, "Add a part here").menu()
    limits = next(a for a in parts.actions() if a.text() == "Limits")
    limits.trigger()
    node = page.board.first("limits")
    assert node is not None and page.undo.count() == 1
    where = page.view.mapToScene(point)
    assert abs(node.x + NODE_W / 2 - where.x()) <= 20 and abs(node.y + NODE_H / 2 - where.y()) <= 20, "added there"
    page.menus.clear()
    QTest.mouseClick(page.view.viewport(), RIGHT, NONE, empty_point(page))
    menu = page.menus[0]
    assert action(menu, "Undo").isEnabled()
    action(menu, "Select all").trigger()
    assert all(item.isSelected() for item in page.items.values())
    action(menu, "Zoom 100 %").trigger()
    assert page.view.transform().m11() == 1.0
    action(menu, "Fit the board").trigger()
    assert page.view.transform().m11() != 1.0


def test_a_right_click_on_a_node_selects_it_and_offers_its_actions(page, qapp):
    page.add_node("limits", **{"--memory": "4g"})
    page.fit()
    qapp.processEvents()
    limits, agent = page.items[page.board.first("limits").id], page.items[page.board.first("agent").id]
    page.scene.clearSelection()
    agent.setSelected(True)
    undo = page.undo.count()
    QTest.mouseClick(page.view.viewport(), RIGHT, NONE, page.view.mapFromScene(limits.sceneBoundingRect().center()))
    qapp.processEvents()
    assert limits.isSelected() and not agent.isSelected(), "the node right-clicked is the selection"
    menu = page.menus[-1]
    assert "What is this?" in texts(menu) and "Delete" in texts(menu) and action(menu, "Delete").isEnabled()
    assert page.undo.count() == undo
    action(menu, "Delete").trigger()
    assert page.board.first("limits") is None
    agent = page.items[page.board.first("agent").id]  # the board is drawn anew after a change
    QTest.mouseClick(page.view.viewport(), RIGHT, NONE, page.view.mapFromScene(agent.sceneBoundingRect().center()))
    assert not action(page.menus[-1], "Delete").isEnabled(), "the agent cannot be removed"


def menu_event(reason, viewport):
    return QContextMenuEvent(reason, QPoint(5, 5), viewport.mapToGlobal(QPoint(5, 5)))


def test_the_menu_key_opens_the_menu_too(page, qapp):
    page.items[page.board.first("agent").id].setSelected(True)
    viewport = page.view.viewport()
    QApplication.sendEvent(viewport, menu_event(QContextMenuEvent.Reason.Keyboard, viewport))
    assert len(page.menus) == 1 and "Delete" in texts(page.menus[0])
    # The mouse's own context menu event (at the press, on X11 and Wayland) opens nothing: the release decides.
    QApplication.sendEvent(viewport, menu_event(QContextMenuEvent.Reason.Mouse, viewport))
    assert len(page.menus) == 1


def test_the_left_button_still_selects_moves_and_draws_the_rubber_band(page, qapp):
    zoomed(page, qapp)
    page.view.zoom(1 / 3.0)
    page.fit()
    qapp.processEvents()
    before = looks(page)
    QTest.mouseClick(page.view.viewport(), LEFT, NONE, empty_point(page))
    qapp.processEvents()
    assert looks(page)["nodes"] == before["nodes"] and page.undo.count() == before["undo"], \
        "a click snaps nothing to the grid"
    # The rubber band from above the left of every node to below the right of every node selects them all.
    rect = page.scene.itemsBoundingRect()
    top_left = page.view.mapFromScene(rect.topLeft() - QPointF(15, 15))
    bottom_right = page.view.mapFromScene(rect.bottomRight() + QPointF(15, 15))
    drag(page, qapp, LEFT, top_left, bottom_right - top_left)
    assert all(item.isSelected() for item in page.items.values())
    page.scene.clearSelection()
    # Dragging a node moves it (on the grid) as one step of Undo; the others stay off the grid.
    agent = page.board.first("agent")
    network = page.board.first("network")
    was = (network.x, network.y)
    item = page.items[agent.id]
    start = page.view.mapFromScene(item.sceneBoundingRect().center() + QPointF(-40, 10))
    drag(page, qapp, LEFT, start, QPoint(0, 80))
    assert agent.y % 20 == 0 and agent.y > before["nodes"][[n[0] for n in before["nodes"]].index(agent.id)][2]
    assert (network.x, network.y) == was and page.undo.count() == before["undo"] + 1
    page.undo.undo()
    assert looks(page)["nodes"] == before["nodes"]


# -- a node's text --


def text_fits(item):
    """Every line shown is no wider than the node's text area, and the lines end inside the node."""
    title_metrics, detail_metrics = QFontMetricsF(item.title_font), QFontMetricsF(item.detail_font)
    widths = [title_metrics.horizontalAdvance(line) for line in item.titles] + \
        [detail_metrics.horizontalAdvance(line) for line in item.details]
    bottom = item.header_h + 9.0 + len(item.details) * detail_metrics.lineSpacing()
    return max(widths) <= NODE_W - 2 * NODE_PAD and 5.0 + len(item.titles) * title_metrics.lineSpacing() \
        <= item.header_h and bottom <= item.rect().height()


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_long_text_wraps_to_three_lines_inside_the_node_and_is_whole_in_the_tooltip(page, qapp, tmp_path,
                                                                                      language):
    i18n.set_language(language)
    long_path = tmp_path / ("a-folder-with-a-rather-long-name-" * 4) / "and-deeper-still" / "project"
    long_path.mkdir(parents=True)
    page.add_node("workspace", path=str(long_path))
    page.add_node("limits", **{"--memory": "4g"})
    item = page.items[page.board.first("workspace").id]
    title = page.board.first("workspace").title()
    assert len(item.titles) + len(item.details) <= TEXT_LINES and len(item.titles) == 2
    assert item.titles[-1].endswith("…") and text_fits(item)
    line = QFontMetricsF(item.detail_font).lineSpacing()
    assert NODE_H < item.rect().height() <= NODE_H + TEXT_LINES * line, "the node grows, not without end"
    assert str(long_path) in item.toolTip() and title.split(":")[0] in item.toolTip()
    assert item.inport is None or item.inport.pos().y() == item.rect().height() / 2
    assert item.outport.pos().y() == item.rect().height() / 2, "the ports at the middle of the taller node"
    agent = page.items[page.board.first("agent").id]
    edge = next(e for e in page.edges if e.source is item and e.target is agent)
    assert edge.path().elementAt(0).y == pytest.approx(item.outport.scenePos().y()), "the edge starts at the port"
    page.inspect(page.board.first("workspace"))
    assert page.inspector_title.text() == title, "the inspector shows it whole"
    short = page.items[page.board.first("limits").id]
    assert len(short.titles) == 1 and len(short.details) == 1 and short.rect().height() == NODE_H
    assert not short.titles[0].endswith("…") and text_fits(short)
    # A long detail (many hosts) gets the lines the title leaves, the last one ends in "…".
    network = page.board.first("network")
    page.set_prop(network.id, "--allow-host", [f"host-number-{i}.example.org" for i in range(3)] + ["x.org"])
    page.set_prop(network.id, "--profile", ["python", "node", "rust", "go", "ruby", "java", "dotnet", "php"])
    item = page.items[network.id]
    assert len(item.titles) == 1 and len(item.details) == 2 and item.details[-1].endswith("…") and text_fits(item)
    assert "host-number-2.example.org" in item.toolTip()


def test_a_node_paints_nothing_outside_itself(page, qapp, tmp_path):
    """Rendered on its own: no pixel of its text outside the node (the frame of a hint and the shadow aside)."""
    from PySide6.QtGui import QImage, QPainter
    long_path = tmp_path / ("x" * 200)
    long_path.mkdir()
    page.add_node("workspace", path=str(long_path))
    item = page.items[page.board.first("workspace").id]
    rect = item.rect()
    image = QImage(int(NODE_W) + 200, int(rect.height()) + 200, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.translate(100, 100)
    item.paint(painter, QtWidgets.QStyleOptionGraphicsItem(), None)
    painter.end()
    outside = [(x, y) for x in range(image.width()) for y in range(0, image.height(), 2)
               if (x < 96 or x > 100 + NODE_W + 4 or y < 96) and image.pixelColor(x, y).alpha() > 0]
    assert outside == []


# -- the inspector and the findings --


def click(view, item, button=LEFT):
    QTest.mouseClick(view.viewport(), button, NONE, view.mapFromScene(item.sceneBoundingRect().center()))


def test_the_inspector_slides_out_on_a_click_and_back_on_esc_or_the_empty_canvas(page, qapp, wait):
    slide = page.slide
    assert not slide.expanded and slide.width() == slide.strip_width() and not slide.holder.isVisible()
    assert page.side.parentWidget() is slide.holder and slide.arrow.accessibleName() == "Show the properties"
    agent = page.items[page.board.first("agent").id]
    click(page.view, agent)
    assert slide.expanded and slide.arrow.accessibleName() == "Hide the properties"
    assert wait(lambda: slide.width() == slide.WIDE, 3) and page.side.isVisible(), "it slides out"
    QTest.keyClick(page.view, Qt.Key.Key_Escape)
    assert not slide.expanded and wait(lambda: slide.width() == slide.strip_width() and not slide.holder.isVisible(), 3)
    click(page.view, agent)
    assert wait(lambda: slide.width() == slide.WIDE, 3)
    QTest.mouseClick(page.view.viewport(), LEFT, NONE, empty_point(page))  # the empty canvas
    assert not slide.expanded and wait(lambda: slide.width() == slide.strip_width(), 3)
    slide.arrow.click()  # its arrow, both ways
    assert slide.expanded
    slide.arrow.click()
    assert not slide.expanded
    # The right button still pans and opens the menu; the strip does not move for it.
    page.menus.clear()
    QTest.mouseClick(page.view.viewport(), RIGHT, NONE, empty_point(page))
    assert len(page.menus) == 1 and not slide.expanded


def test_the_score_and_findings_are_shown_only_on_request_and_alone(page, qapp):
    assert not page.findings_panel.isVisible() and not page.score_button.isChecked()
    assert page.score_button.text().startswith("Security score:") and page.score_button.accessibleName()
    page.score_button.click()
    assert page.findings_panel.isVisible() and page.score_button.isChecked() and not page.slide.expanded
    click(page.view, page.items[page.board.first("agent").id])
    assert page.slide.expanded and page.findings_panel.isVisible()
    QTest.keyClick(page.view, Qt.Key.Key_Escape)
    assert not page.slide.expanded and page.findings_panel.isVisible(), "the inspector does not take the findings"
    page.score_button.click()
    assert not page.findings_panel.isVisible()


def test_in_the_window_the_score_is_a_panel_of_its_own_closed_at_first_and_kept(window, qapp, monkeypatch):
    window.show()
    window.show_page("board")
    qapp.processEvents()
    board, findings = window.board, window.docks["findings"]
    assert not findings.isVisible() and not board.score_button.isChecked()
    assert not window.panel_actions["findings"].isChecked()
    assert window.workspace.dockWidgetArea(findings) != window.workspace.dockWidgetArea(window.docks["inspector"]) \
        or window.docks["inspector"].isFloating()
    board.score_button.click()
    qapp.processEvents()
    assert findings.isVisible() and board.score_button.isChecked() and window.panel_actions["findings"].isChecked()
    assert findings.widget() is board.findings_panel and not board.slide.expanded
    click(board.view, board.items[board.board.first("agent").id])
    QTest.keyClick(board.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert findings.isVisible(), "the inspector going back leaves the findings alone"
    window.state.flush()
    again = make_window(monkeypatch)
    try:
        again.show()
        again.show_page("board")
        qapp.processEvents()
        assert again.docks["findings"].isVisible() and again.board.score_button.isChecked(), "kept open"
    finally:
        close(again)


@pytest.mark.parametrize("closed", [False, True])
def test_the_inspector_as_a_window_of_its_own_opens_on_a_click_and_is_kept(window, qapp, wait, monkeypatch,
                                                                            closed):
    window.show()
    window.show_page("board")
    qapp.processEvents()
    board, dock = window.board, window.docks["inspector"]
    assert board.inspector_mode == "slide" and not window.actions["inspector-window"].isChecked()
    window.actions["inspector-window"].trigger()
    qapp.processEvents()
    assert board.inspector_mode == "window" and dock.widget() is board.side and dock.isFloating()
    assert not board.slide.isVisible() and dock.allowedAreas() == Qt.DockWidgetArea.NoDockWidgetArea
    if closed:
        dock.close()
        qapp.processEvents()
        assert not dock.isVisible() and not window.panel_actions["inspector"].isChecked()
    window.activateWindow()
    board.view.setFocus()
    qapp.processEvents()
    focused = QApplication.focusWidget()
    agent = board.items[board.board.first("agent").id]
    click(board.view, agent)
    qapp.processEvents()
    assert agent.isSelected() and dock.isVisible() and dock.isFloating()
    assert window.panel_actions["inspector"].isChecked() and "inspector" not in window.closed_docks
    assert wait(lambda: QApplication.focusWidget() is focused, 2), "the keyboard stays where it was"
    window.state.flush()
    again = make_window(monkeypatch)
    try:
        assert again.board.inspector_mode == "window" and again.actions["inspector-window"].isChecked()
        assert again.docks["inspector"].widget() is again.board.side, "the mode is kept"
        again.actions["inspector-window"].trigger()  # back to the strip
        assert again.board.inspector_mode == "slide" and again.board.side.parentWidget() is again.board.slide.holder
        assert not again.docks["inspector"].isVisible()
    finally:
        close(again)


def test_a_right_click_on_a_connection_offers_delete(page, qapp, tmp_path):
    page.add_node("workspace", path=str(tmp_path))
    page.fit()
    qapp.processEvents()
    workspace, agent = page.board.first("workspace"), page.board.first("agent")
    edge = next(e for e in page.edges if e.ids == (workspace.id, agent.id))
    QTest.mouseClick(page.view.viewport(), RIGHT, NONE, page.view.mapFromScene(edge.path().pointAtPercent(0.5)))
    assert edge.isSelected() and texts(page.menus[-1]) == ["Delete"]
    action(page.menus[-1], "Delete").trigger()
    assert (workspace.id, agent.id) not in page.board.edges
