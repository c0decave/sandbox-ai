"""Native crashes the app must never have, offscreen: Qt objects used after Qt deleted them (the board's scene made
anew under a gesture, widgets deleted inside their own signal), which end the process without a Python traceback."""
import gc
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from sandbox_ai_app import backend, help_view, widgets  # noqa: E402
from conftest import open_tab  # noqa: E402
from test_projects import ask, close, env, fake_run, project  # noqa: E402,F401
from test_terminal import stub_web  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


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
    yield view
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def test_the_scene_made_anew_while_a_connection_is_dragged(page, qapp, tmp_path):
    """A step that makes the scene anew (Delete on a selected part, a new theme, another project) while a
    connection is being dragged: the dashed line went with the scene, and the next move of the mouse drew on it."""
    page.add_node("mount", source=str(tmp_path), dest="/mnt/x", mode="ro")
    mount, agent = page.board.first("mount"), page.board.first("agent")
    page.change("disconnect", lambda: page.board.disconnect(mount.id, agent.id))
    page.fit()
    qapp.processEvents()
    view = page.view.viewport()
    start = page.view.mapFromScene(page.items[mount.id].outport.scenePos())
    body = page.view.mapFromScene(page.items[agent.id].sceneBoundingRect().center() + QPointF(0, 12))
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view, body)
    assert page.scene.line is not None
    page.rebuild()
    QTest.mouseMove(view, body + QPoint(5, 5))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, body)
    assert page.scene.line is None and page.scene.dragging is None
    assert (mount.id, agent.id) not in page.board.edges, "the drag ended with the scene: nothing connected"



def test_no_filter_on_the_whole_application_and_a_terminal_tab_opens(qapp, env, stub_web, monkeypatch, wait):
    """Soak (stability round 3): a filter of the whole application, in Python, sees every event of every object -
    Chromium's of the terminal tabs too - and with one installed the next terminal tab ended the process (a
    segmentation fault, every time). The app installed two: the keys of the answers while the open questions are shown
    (the Approvals page, or their panel floating: often beside terminal tabs), and the watch of the mouse and keyboard
    while the pages are made after the start. Neither is on the application now; the keys still answer (test_ui)."""
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    installed = []
    original = QApplication.installEventFilter
    monkeypatch.setattr(QApplication, "installEventFilter",
                        lambda self, watcher: installed.append(watcher) or original(self, watcher))
    ask(fake_run(env, "claude-p", project(env, "p")), "1700000000-1-1", kind="dns", name="pypi.org")
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0, lazy=True)
    w.page_makers["terminals"] = lambda: terminal.TerminalsPage(stub_web, sai=w.sai, in_terminal=w.in_terminal)
    try:
        w.resize(1200, 800)
        w.show()
        w.show_page("approvals")
        w.approvals.poll()
        assert wait(lambda: w.approvals.keys_on, 5), "the keys of the answers are on"
        w.build_later()  # the watch of the mouse and keyboard while the other pages are made
        assert w.input_watch.app is not None and w.input_watch.windows
        assert installed == [], "nothing filters the whole application"
        assert open_tab(w.terminals, ["sh", "-c", "sleep 30"], "t") is not None
        wait(lambda: False, 1.0)  # (the process ended here, at the latest)
        w.terminals.close_all()
    finally:
        close(w)
