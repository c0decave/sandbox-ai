"""The Board page offscreen: nodes, edges, undo/redo, the inspector, the launcher's verdict, Apply and Start."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, theme, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    # Without a workspace node the launcher's verdict takes the working directory as the workspace: a project of
    # the test's own, not the app's directory (which lies in sandbox-ai's own tree and is refused for that).
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage
    started = []
    view = BoardPage(backend.SandboxAI(), lambda argv, title, embedded=None, cwd=None:
                     started.append((argv, title, embedded, cwd)), lambda: {"board_grid": 20})
    view.started = started
    yield view
    view.deleteLater()


def kinds(page):
    return sorted(page.board.node(i).kind for i in page.items)


def test_the_board_starts_with_agent_proxy_and_network(page):
    assert kinds(page) == ["agent", "network", "proxy"]
    assert page.preview.toPlainText().startswith("sandbox-ai claude")
    assert len(page.edges) == 2 and page.score.text().startswith("Security score:")


def test_nodes_and_edges_with_undo_and_redo(page, tmp_path):
    page.add_node("workspace", path=str(tmp_path))
    assert kinds(page).count("workspace") == 1 and f"--workspace {tmp_path}" in page.preview.toPlainText()
    page.undo.undo()
    assert "workspace" not in kinds(page)
    page.undo.redo()
    workspace = page.board.first("workspace")
    agent = page.board.first("agent")
    edge = next(e for e in page.edges if e.ids == (workspace.id, agent.id))
    page.scene.clearSelection()
    edge.setSelected(True)
    page.delete_selected()  # disconnected: the workspace no longer counts
    assert "--workspace" not in page.preview.toPlainText()
    assert page.items[workspace.id].opacity() < 1.0
    page.connect_nodes(workspace.id, agent.id)
    assert "--workspace" in page.preview.toPlainText()
    page.connect_nodes(workspace.id, page.board.first("network").id)  # not a flow: ignored
    assert (workspace.id, page.board.first("network").id) not in page.board.edges


def test_properties_problems_and_badges(page, wait):
    network = page.board.first("network")
    page.set_prop(network.id, "--allow-host", ["pypi.org"])
    assert "--allow-host pypi.org" in page.preview.toPlainText()
    page.set_prop(network.id, "--offline", True)
    texts = [page.findings.item(i).text() for i in range(page.findings.count())]
    assert any("Offline contradicts" in t for t in texts)
    assert page.items[network.id].pen().color().name() == theme.color("bad")
    assert wait(lambda: "refuses" in page.verdict.text(), 60), page.verdict.text()
    page.undo.undo()
    assert wait(lambda: "accepts" in page.verdict.text(), 60), page.verdict.text()


def test_the_inspector_builds_fields_from_the_launcher_and_honours_the_policy(page, wait):
    proxy = page.board.first("proxy")
    page.forbidden = {"--no-proxy": {None}, "--privacy-check": {"off"}}
    page.inspect(proxy)
    from sandbox_ai_app.board_view import RulesEdit
    boxes = page.inspector_holder.findChildren(QtWidgets.QCheckBox)
    enabled = next(b for b in boxes if "every connection passes it" in b.text())  # (--no-proxy: when shown)
    assert not enabled.isEnabled() and enabled.isChecked()
    combo = next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox) if c.findText("off") >= 0)
    assert not combo.model().item(combo.findText("off")).isEnabled()
    rules = page.inspector_holder.findChildren(RulesEdit)[0]
    rules.add_row("api.github.com", "GET,HEAD", "/**", emit=True)
    assert page.board.first("proxy").props["--allow-http"] == ["api.github.com GET,HEAD /**"]
    assert "--allow-http 'api.github.com GET,HEAD /**'" in page.preview.toPlainText()


def test_moving_snaps_to_the_grid_as_one_step(page):
    before = page.board.state()
    agent = page.board.first("agent")
    page.items[agent.id].setPos(123.0, 77.0)
    page.moved(before)
    assert (agent.x, agent.y) == (120.0, 80.0) and page.undo.count() == 1
    page.undo.undo()
    assert page.board.first("agent").x != 120.0 or page.board.first("agent").y != 80.0


def test_templates_open_and_start_runs_exactly_the_board(page, tmp_path):
    page.directory = str(tmp_path)
    page.new_from_template("locked")
    assert "--offline" in page.preview.toPlainText()
    assert page.start(embedded=True)
    argv, title, embedded, cwd = page.started[-1]
    assert argv[3] == "claude" and "--offline" in argv and "--no-config" in argv and embedded is True
    assert cwd == str(tmp_path)


def test_apply_writes_and_trusts_the_project_file_and_the_layout(page, tmp_path, wait):
    project = tmp_path / "project"
    project.mkdir()
    assert page.open_project(str(project))
    page.set_prop(page.board.first("network").id, "--allow-pypi", True)
    assert page.apply()
    title, text, details = widgets.SHOWN[-1]
    assert title == "Write and trust" and '"--allow-pypi"' in details
    assert wait(lambda: not widgets.busy(page), 60)
    document = json.loads((project / ".sandbox-ai.json").read_text())
    assert document["options"] == ["--allow-pypi"] and document["agent"] == "claude"
    assert page.sai.review_project(project).trusted and (project / B.LAYOUT_NAME).is_file()
    again = B.from_project(page.sai, document, project)
    assert B.to_argv(page.sai, again) == B.to_argv(page.sai, page.board)


def test_errors_stop_apply_and_start(page, tmp_path):
    page.add_node("workspace", path=str(tmp_path))
    page.set_prop(page.board.first("network").id, "--offline", True)
    page.set_prop(page.board.first("network").id, "--allow-pypi", True)
    assert not page.apply() and not page.start()
    assert "Offline contradicts" in widgets.SHOWN[-1][1] and page.started == []


def test_a_project_file_nobody_trusted_is_shown_never_started_until_applied(page, tmp_path, wait):
    """The agent can write the project's .sandbox-ai.json (it lies in the workspace) and Start passes --no-config:
    a board loaded from an untrusted file starts only after Apply wrote and trusted what the human saw."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude",
                                                          "options": ["--egress-all-allow"]}))
    assert page.open_project(str(project))
    started = len(page.started)
    assert page.start(embedded=True) is False and len(page.started) == started
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Not started" and "is not trusted" in text
    assert page.apply()
    assert wait(lambda: not widgets.busy(page), 60)
    assert page.sai.review_project(project).trusted
    assert page.start(embedded=True) and len(page.started) == started + 1


def test_a_board_with_the_projects_own_mcp_servers_starts_from_the_written_trusted_file(page, tmp_path, wait):
    """Regression ("Unknown MCP server: search; defined: demo, yara"): the research preset's servers are the
    project's own (.sandbox-ai.json "mcp_servers"); a start with --no-config never told the launcher about them.
    Start now writes and trusts the file first (Apply, shown first), then starts from it."""
    project = tmp_path / "new-test"
    project.mkdir()
    page.directory = str(project)
    page.new_from_preset("research")
    assert sorted(B.project_servers(page.board)) == ["fetch", "search"]
    assert page.start(embedded=True)
    title, text, details = widgets.SHOWN[-1]
    assert title == "Write and trust" and "MCP servers the project defines" in text and '"search"' in details
    assert wait(lambda: page.started, 60), widgets.SHOWN[-3:]
    argv, _, embedded, cwd = page.started[-1]
    document = json.loads((project / ".sandbox-ai.json").read_text())
    assert set(document["mcp_servers"]) == {"search", "fetch"} and page.sai.review_project(project).trusted
    assert "--no-config" not in argv and argv[argv.index("--workspace") + 1] == str(project)
    assert "--mcp" not in argv, "the trusted file names its servers; not given twice"
    assert embedded is True and cwd == str(project)
    # Written and trusted already: the next start goes ahead at once, from the file.
    shown = len(widgets.SHOWN)
    assert page.start(embedded=False) and len(page.started) == 2 and len(widgets.SHOWN) == shown
    assert "--no-config" not in page.started[-1][0]


def test_declining_to_write_the_project_file_refuses_the_start_with_the_reason(page, tmp_path, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    page.directory = str(project)
    page.new_from_preset("research")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert page.start(embedded=True) is False and page.started == []
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Not started" and "search" in text and "Apply" in text
    assert not (project / ".sandbox-ai.json").exists()


def test_the_inspector_lists_the_agents_models_with_their_versions(page):
    agent = page.board.first("agent")
    page.inspect(agent)
    combo = page.model_combo
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert "Opus 5.5 — claude-opus-5-5" in texts and any(t.startswith("Opus 5.5 now") for t in texts)
    combo.setCurrentIndex(combo.findData("claude-opus-5-5"))
    combo.activated.emit(combo.currentIndex())
    assert page.board.first("agent").props["model"] == "claude-opus-5-5"
    assert "--model claude-opus-5-5" in page.preview.toPlainText()


def test_a_start_that_cannot_be_run_is_a_message_not_an_exception(page, tmp_path):
    page.directory = str(tmp_path)
    page.new_from_template("locked")

    def broken(*_args, **_kwargs):
        raise OSError("no terminal")
    page.terminal = broken
    assert page.start(embedded=True) is False
    assert widgets.SHOWN[-1][0] == "Not started" and "no terminal" in widgets.SHOWN[-1][1]


def test_painting_the_canvas_changes_no_view_state(page):
    """Regression: drawBackground set the drag mode and the transformation anchor on every paint - a paint
    during a pan with the middle button switched the pan off. They are set once, when the view is made."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QGraphicsView
    view = page.view
    assert view.dragMode() == QGraphicsView.DragMode.RubberBandDrag
    assert view.transformationAnchor() == QGraphicsView.ViewportAnchor.AnchorUnderMouse
    view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)  # what a press of the middle button does
    image = QImage(200, 200, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    try:
        view.drawBackground(painter, QRectF(0, 0, 200, 200))
    finally:
        painter.end()
    assert view.dragMode() == QGraphicsView.DragMode.ScrollHandDrag, "the pan goes on"


def test_the_wizard_for_the_boards_own_folder_asks_before_dropping_its_changes(page, tmp_path, monkeypatch):
    """new_project opens the wizard without asking first (ask=False); when it then writes the folder of the
    board shown, its unapplied changes would be replaced without a word: may_discard asks then."""
    from sandbox_ai_app import board as B
    from sandbox_ai_app import wizard as W
    folder = tmp_path / "project"
    folder.mkdir()
    page.open_project(str(folder), quiet=True)
    page.add_node("limits", **{"--memory": "4g"})
    assert page.dirty()

    class FakeWizard:
        def __init__(self, sai, settings, parent, board_host="local"):
            from PySide6.QtCore import QObject, Signal

            class Emitter(QObject):
                written = Signal(object, str)
                board_wanted = Signal()
            self.emitter = Emitter()
            self.written, self.board_wanted = self.emitter.written, self.emitter.board_wanted

        def exec(self):
            self.written.emit(B.from_argv(page.sai, "codex", [], workspaces=[str(folder)]), str(folder))
    monkeypatch.setattr(W, "SandboxWizard", FakeWizard)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep them"
    widgets.SHOWN.clear()
    page.open_wizard(ask=False)
    assert widgets.SHOWN and widgets.SHOWN[-1][0] == "Changes not applied"
    assert page.dirty() and page.board.first("limits") is not None, "kept"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # "Drop the changes"
    page.open_wizard(ask=False)
    assert page.board.first("limits") is None and B.values(page.board)[0] == "codex", "the wizard's board"


def test_the_agent_part_says_its_model_and_effort():
    """The agent's card names the model and the effort it works with (the agent's defaults when none is chosen),
    then -D."""
    from sandbox_ai_app import board as B, board_view as BV
    node = B.Node(1, "agent", {"agent": "claude", "--dangerously-skip-permissions": True})
    assert BV.summary(node) == "default model · default effort · -D"
    node.props.update(model="claude-opus-5-5", effort="high")
    assert BV.summary(node) == "claude-opus-5-5 · effort high · -D"
    node.props.pop("--dangerously-skip-permissions")
    assert BV.summary(node) == "claude-opus-5-5 · effort high"
