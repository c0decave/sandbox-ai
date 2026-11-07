"""Drag and drop on the Board, offscreen: parts from the palette onto the canvas (where they are dropped, one step
of Undo, a drop indicator in the theme's colours), folders and files from a file manager (workspace, mount, the
folder of a part, a project, a preset), connecting by dragging from a port onto a node, and the (?) of the parts
and nodes. The keyboard keeps working: Enter on a part adds it."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from sandbox_ai_app import backend, board as B, help_view, presets, theme, widgets  # noqa: E402
from sandbox_ai_app.board_view import MIME_PART, NODE_H, NODE_W  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(help_view, "OPENED", [])
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    yield view
    view.close()
    view.deleteLater()


def palette_item(page, kind):
    return next(page.palette.item(r) for r in range(page.palette.count())
                if page.palette.item(r).data(Qt.ItemDataRole.UserRole) == kind)


def part_mime(kind):
    mime = QMimeData()
    mime.setData(MIME_PART, kind.encode())
    return mime


def drag(page, qapp, mime, point, drop=True):
    """A drag of MIME over the board's viewport to POINT (viewport coordinates), dropped there if DROP."""
    viewport = page.view.viewport()
    args = (Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    enter = QDragEnterEvent(point, *args)
    qapp.sendEvent(viewport, enter)
    move = QDragMoveEvent(point, *args)
    qapp.sendEvent(viewport, move)
    ghost = page.ghost
    accepted = move.isAccepted()
    if drop:
        qapp.sendEvent(viewport, QDropEvent(QPointF(point), *args))
        qapp.processEvents()  # the drop is handled once its event has returned
    return ghost, accepted


def test_a_part_dragged_from_the_palette_lands_where_it_is_dropped(page, qapp):
    mime = page.palette.mimeData([palette_item(page, "mount")])
    assert page.drop_what(mime) == ("part", "mount")
    point = QPoint(500, 600)
    ghost, accepted = drag(page, qapp, mime, point, drop=False)
    assert accepted and ghost is not None and ghost.label.text() == "Drop to add Mount" and not ghost.refused
    assert ghost.pen().color() == QColor(theme.colors()["accent"]), "the indicator is in the theme's accent colour"
    scene = page.view.mapToScene(point)
    assert ghost.pos() == QPointF(B.snap(scene.x() - NODE_W / 2, 20), B.snap(scene.y() - NODE_H / 2, 20))
    qapp.sendEvent(page.view.viewport(), QDragLeaveEvent())
    assert page.ghost is None and ghost.scene() is None, "leaving the board removes the indicator"
    steps = page.undo.count()
    drag(page, qapp, mime, point)
    mount = page.board.first("mount")
    assert mount is not None and page.ghost is None
    assert (mount.x, mount.y) == (B.snap(scene.x() - NODE_W / 2, 20), B.snap(scene.y() - NODE_H / 2, 20))
    assert page.undo.count() == steps + 1
    page.undo.undo()
    assert page.board.first("mount") is None, "one drop is one step of Undo"


def test_a_part_the_board_has_once_is_greyed_out_and_refused(page, qapp):
    assert palette_item(page, "limits").flags() & Qt.ItemFlag.ItemIsEnabled
    page.drop(("part", "limits"), QPointF(100, 100))
    item = palette_item(page, "limits")
    assert not item.flags() & Qt.ItemFlag.ItemIsEnabled and "On the board already" in item.toolTip()
    ghost, accepted = drag(page, qapp, part_mime("limits"), QPoint(300, 300), drop=False)
    assert ghost.refused and not accepted and ghost.pen().color() == QColor(theme.colors()["danger"])
    page.hide_ghost()
    assert page.drop(("part", "limits"), QPointF(400, 400)) is None and len(page.board.of_kind("limits")) == 1
    assert page.drop_what(part_mime("agent")) is None, "the parts every board has cannot be dropped"
    assert page.drop_what(part_mime("nonsense")) is None
    page.undo.undo()
    assert palette_item(page, "limits").flags() & Qt.ItemFlag.ItemIsEnabled


def test_the_keyboard_still_adds_a_part(page, qapp):
    page.palette.setCurrentItem(palette_item(page, "display"))
    QTest.keyClick(page.palette, Qt.Key.Key_Return)
    assert page.board.first("display") is not None


def test_folders_and_files_from_a_file_manager(page, qapp, tmp_path, monkeypatch):
    folder, other, data = tmp_path / "project", tmp_path / "data set", tmp_path / "notes.txt"
    folder.mkdir()
    other.mkdir()
    data.write_text("x")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(folder))])
    assert page.drop_what(mime) == ("paths", [str(folder)])
    assert [key for key, _ in page.drop_choices([str(folder)])] == ["workspace", "mount-ro", "mount-rw"]
    assert [key for key, _ in page.drop_choices([str(data)])] == ["mount-ro", "mount-rw"], "a file is no workspace"
    chosen = []
    monkeypatch.setattr(page, "choose_drop", lambda choices, point=None: chosen.append(choices) or answer[0])
    answer = ["workspace"]
    ghost, _ = drag(page, qapp, mime, QPoint(300, 300))
    assert ghost.label.text() == "Drop: project"
    workspace = page.board.first("workspace")
    assert workspace.props["path"] == str(folder) and page.board.connected(workspace)
    answer[0] = "mount-ro"
    assert page.drop_paths([str(other), str(data)], QPointF(600, 200)) == "mount-ro"
    mounts = page.board.of_kind("mount")
    assert [(m.props["source"], m.props["dest"], m.props["mode"]) for m in mounts] == [
        (str(other), "/mnt/data-set", "ro"), (str(data), "/mnt/notes.txt", "ro")]
    assert mounts[1].y > mounts[0].y, "several are stacked below the drop"
    page.undo.undo()
    assert page.board.of_kind("mount") == [], "several paths are one step of Undo"
    answer[0] = "mount-rw"
    page.drop_paths([str(other)], QPointF(600, 200))
    page.drop_paths([str(other)], QPointF(600, 400))
    assert [m.props["dest"] for m in page.board.of_kind("mount")] == ["/mnt/data-set", "/mnt/data-set-2"]
    # onto a mount: its folder
    mount = page.board.of_kind("mount")[0]
    item = page.items[mount.id]
    choices = page.drop_choices([str(folder)], mount)
    assert choices[0] == ("use", f"Use it for {mount.title()}")
    answer[0] = "use"
    page.drop_paths([str(folder)], item.sceneBoundingRect().center())
    assert page.board.node(mount.id).props["source"] == str(folder)
    answer[0] = None
    assert page.drop_paths([str(folder)], QPointF(0, 0)) is None, "a closed menu changes nothing"
    assert page.drop_paths([str(tmp_path / "missing")], QPointF(0, 0)) is None


def test_a_project_file_or_folder_opens_its_project(page, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text("{}")
    opened = []
    page.project_request = opened.append
    assert page.drop_paths([str(project / ".sandbox-ai.json")], QPointF(0, 0)) == "project"
    monkeypatch.setattr(page, "choose_drop", lambda choices, point=None: choices[0][0])
    assert page.drop_choices([str(project)])[0][0] == "project"
    assert page.drop_paths([str(project)], QPointF(0, 0)) == "project"
    assert opened == [str(project), str(project)]


def test_a_preset_file_is_imported(page, tmp_path, monkeypatch):
    sai = page.sai
    path = tmp_path / "offline.json"
    presets.export(sai, presets.find("locked"), str(path))
    monkeypatch.setattr(page, "choose_drop", lambda choices, point=None: "preset")
    assert [key for key, _ in page.drop_choices([str(path)])] == ["preset-board", "preset"]
    assert page.drop_paths([str(path)], QPointF(0, 0)) == "preset"
    mine = presets.load_mine(sai)[0]
    assert [p.id for p in mine] == ["my-locked"] and "Imported as your preset" in widgets.SHOWN[-1][1]
    monkeypatch.setattr(page, "choose_drop", lambda choices, point=None: "preset-board")
    page.drop_paths([str(path)], QPointF(0, 0))
    assert "--offline" in page.preview.toPlainText(), "the board is built from the preset"
    other = tmp_path / "theirs.json"
    other.write_text(json.dumps({"format": "sandbox-ai preset", "version": 1, "id": "x"}))
    assert page.drop_paths([str(other)], QPointF(0, 0)) is None
    assert "preset of sandbox-ai itself" in widgets.SHOWN[-1][1]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"format": presets.FORMAT, "version": 1, "id": "x", "options": ["--copy-auth"]}))
    monkeypatch.setattr(page, "choose_drop", lambda choices, point=None: "preset")
    assert page.drop_paths([str(broken)], QPointF(0, 0)) is None and widgets.SHOWN[-1][0] == "Not imported"


def test_connecting_by_dragging_onto_a_node_with_frames_and_esc(page, qapp, tmp_path):
    page.add_node("mount", source=str(tmp_path), dest="/mnt/x", mode="ro")
    mount, agent, network = page.board.first("mount"), page.board.first("agent"), page.board.first("network")
    page.change("disconnect", lambda: page.board.disconnect(mount.id, agent.id))
    page.fit()
    qapp.processEvents()
    view = page.view.viewport()

    def at(point):
        return page.view.mapFromScene(point)
    start = at(page.items[mount.id].outport.scenePos())
    body = at(page.items[agent.id].sceneBoundingRect().center() + QPointF(0, 12))  # on the node, not its port
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view, body)
    assert page.items[agent.id].hint == "hover" and page.items[network.id].hint is None
    QTest.keyClick(page.view, Qt.Key.Key_Escape)
    assert page.scene.dragging is None and page.items[agent.id].hint is None, "Esc cancels"
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, body)
    assert (mount.id, agent.id) not in page.board.edges
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view, body)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, body)
    assert (mount.id, agent.id) in page.board.edges and page.scene.line is None
    page.undo.undo()
    assert (mount.id, agent.id) not in page.board.edges, "a connection is one step of Undo"


def test_the_parts_and_nodes_explain_themselves(page, qapp):
    row = page.palette.row(palette_item(page, "vpn"))
    page.palette.explain(row).close()
    page.palette.setCurrentRow(page.palette.row(palette_item(page, "git")))
    QTest.keyClick(page.palette, "?")  # the keyboard's way to a part's explanation
    agent = page.items[page.board.first("agent").id]
    popover = agent.help_mark.explain()
    assert popover.title.text() == "Agent" and popover.isVisible()
    popover.close()
    assert help_view.OPENED == ["kind.vpn", "kind.git", "kind.agent"]
    page.scene.clearSelection()
    agent.setSelected(True)
    qapp.processEvents()
    page.inspect_timer.timeout.emit()
    assert page.inspector_help.tip_id == "kind.agent"
    page.scene.clearSelection()
    page.inspect(None)
    assert page.inspector_help.tip_id == "board.inspector"
