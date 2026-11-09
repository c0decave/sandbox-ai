"""Tabs for open projects, sessions and sandboxes; the window's own controls (menus, header buttons) and its
panels (docks). Offscreen, against fake records (see test_projects.py)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, theme, widgets  # noqa: E402
from sandbox_ai_app.state import state_file  # noqa: E402

from test_projects import (  # noqa: E402,F401 - shared fixtures and fakes
    close, env, fake_run, fake_session, make_window, project, sleepers, window,
)

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def keys(window):
    return [window.tabs.entry(i)["key"] for i in range(window.tabs.count())]


def current(window):
    return window.tabs.entry(window.tabs.currentIndex())["key"]


# -- tabs --


def test_projects_open_in_tabs_and_the_pages_follow_the_tab(window, env, wait):
    one, two = project(env, "one"), project(env, "two")
    assert window.tabs.count() == 0
    window.set_project(one)
    window.set_project(two)
    assert keys(window) == [f"project:{one}", f"project:{two}"] and current(window) == f"project:{two}"
    assert window.tabs.tabText(0) == "one" and window.tabs.tabToolTip(0)
    window.tabs.setCurrentIndex(0)
    assert window.project == str(one) and window.board.directory == str(one)
    assert window.launch.form.values()["--workspace"] == [str(one)]
    assert wait(lambda: window.files.roots.currentData() == str(one)), "the roots are read in the background"
    assert window.statistics.directory.currentData() == str(window.sai.launcher.default_log_dir(one))
    assert window.project_bar.name.text() == "one"
    window.set_project(two)  # an open project is reused, not opened twice
    assert window.tabs.count() == 2 and window.tabs.currentIndex() == 1
    window.next_tab()
    assert window.project == str(one)
    window.next_tab(-1)
    assert window.project == str(two)


def test_each_tab_keeps_its_board_changes_undo_and_zoom(window, env):
    one, two = project(env, "one"), project(env, "two")
    window.set_project(one)
    board = window.board
    board.add_node("limits", **{"--memory": "4g"})
    board.view.zoom(2.0)
    zoom = board.view.transform().m11()
    window.set_project(two)
    assert board.board.first("limits") is None and not board.dirty()
    assert not board.undo_group.canUndo()
    board.add_node("display")
    window.tabs.setCurrentIndex(0)
    assert board.board.first("limits") is not None and board.board.first("display") is None
    assert board.dirty() and board.undo_group.canUndo() and board.view.transform().m11() == pytest.approx(zoom)
    board.undo_group.undo()
    assert board.board.first("limits") is None, "its own undo stack"
    window.tabs.setCurrentIndex(1)
    assert board.board.first("display") is not None


def test_closing_a_tab_with_unapplied_changes_asks(window, env, monkeypatch):
    one, two = project(env, "one"), project(env, "two")
    window.set_project(one)
    window.board.add_node("limits", **{"--memory": "4g"})
    window.set_project(two)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep the tab"
    assert window.close_tab(0) is False and window.tabs.count() == 2
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Close tab" and "one" in text
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert window.close_tab(0) and keys(window) == [f"project:{two}"]
    assert not window.board.dirty_for(one), "its changes went with it"
    window.set_project(one)
    assert window.board.board.first("limits") is None, "read again from disk"
    # A tab without changes closes at once; Ctrl+W closes the current tab, a middle click the one under it.
    widgets.SHOWN.clear()
    window.actions["close-tab"].trigger()
    assert keys(window) == [f"project:{two}"] and not widgets.SHOWN
    window.tabs.middle_clicked.emit(0)
    assert window.tabs.count() == 0


def test_tabs_are_moved_and_kept_for_the_next_start(window, env, monkeypatch):
    one, two, three = project(env, "one"), project(env, "two"), project(env, "three")
    fake_session(env, "work", two)
    for folder in (one, two, three):
        window.set_project(folder)
    window.sandboxes.refresh()
    from sandbox_ai_app.ui import instance_entry
    window.open_tab(instance_entry(window.sandboxes.instances["session:work"]), activate=False)
    window.tabs.moveTab(2, 0)
    window.tabs.setCurrentIndex(window.tabs.find(f"project:{two}"))
    window.state.flush()
    saved = json.loads(state_file().read_text())
    assert [t["kind"] for t in saved["tabs"]] == ["project", "project", "project", "session"]
    assert saved["tab_current"] == f"project:{two}"
    (three / "gone").mkdir()
    again = make_window(monkeypatch)
    try:
        assert keys(again) == [f"project:{three}", f"project:{one}", f"project:{two}", "session:work"]
        assert current(again) == f"project:{two}" and again.project == str(two)
        assert again.tabs.entry(3)["state"] == "stopped" and again.tabs.entry(3)["agent"] == "codex"
    finally:
        close(again)
    # A project whose folder is gone is left out.
    for path in (three / "gone", three):
        path.rmdir()
    window.state.flush()
    again = make_window(monkeypatch)
    try:
        assert f"project:{three}" not in keys(again)
    finally:
        close(again)


def test_a_session_that_starts_opens_a_tab_and_its_state_follows(window, env, sleepers, monkeypatch):
    folder = project(env, "p")
    window.set_project(folder)
    session_dir = fake_session(env, "night", folder)
    window.sandboxes.refresh()
    assert window.tabs.count() == 1, "a stopped session does not open a tab by itself"
    # It starts: its supervisor runs (a sleeper with the session's arguments).
    supervisor = sleepers("/x/agent_sandbox.py", "codex", "--tmux-socket-dir", session_dir / "tmux")
    data = json.loads((session_dir / "session.json").read_text())
    data.update(pid=supervisor.pid, pid_start=window.sai.launcher.process_start(supervisor.pid))
    (session_dir / "session.json").write_text(json.dumps(data))
    window.sandboxes.refresh()
    index = window.tabs.find("session:night")
    assert index >= 0 and window.tabs.currentIndex() == 0, "opened, not jumped to"
    entry = window.tabs.entry(index)
    assert entry["state"] == "running" and entry["agent"] == "codex" and entry["backend"] == "podman"
    assert "night (codex, podman)" == window.tabs.tabText(index)
    assert "running" in window.tabs.tabToolTip(index)
    # It ends: the tab stays, stopped, until it is closed.
    supervisor.kill()
    supervisor.wait()
    window.sandboxes.refresh()
    assert window.tabs.entry(window.tabs.find("session:night"))["state"] == "stopped"
    # A one-off run too; with the setting off, nothing opens.
    fake_run(env, "claude-new", folder)
    window.sandboxes.refresh()
    assert window.tabs.find("run:claude-new") >= 0
    (env / "runtime" / "sandbox-ai" / "runs" / "claude-new" / "run.json").unlink()
    window.sandboxes.refresh()
    assert window.tabs.entry(window.tabs.find("run:claude-new"))["state"] == "stopped", "a sandbox that ended stays"
    window.sai.save_setting("auto_tabs", False)
    fake_run(env, "claude-other", folder)
    window.sandboxes.refresh()
    assert window.tabs.find("run:claude-other") < 0


def test_the_plus_menu_and_a_tabs_context_menu(window, env, monkeypatch, wait):
    one, two = project(env, "one"), project(env, "two")
    window.set_project(one)
    window.sai.remember_project(two)
    fake_session(env, "work", two)
    window.sandboxes.refresh()
    window.fill_add_tab_menu()
    assert wait(lambda: window.project_bar.known.reading is None)  # the projects are read in the background
    window.fill_add_tab_menu(read=False)
    texts = [a.text() for a in window.add_tab_menu.actions() if a.text()]
    assert any(t.startswith("one") for t in texts) and any(t.startswith("two") for t in texts)
    session = next(a for a in window.add_tab_menu.actions() if a.text().startswith("Session work"))
    assert texts[-2:] == ["Open folder…", "New project (wizard)…"]
    session.trigger()
    index = window.tabs.find("session:work")
    assert index >= 0 and window.tabs.currentIndex() == index and window.project == str(two), \
        "a session's tab is current: its project is"
    window.fill_add_tab_menu()
    assert not [a for a in window.add_tab_menu.actions() if a.text().startswith("Session work")], "open already"
    actions = {key: (enabled, function) for key, _, enabled, function in window.tab_actions(index)}
    assert actions["resume"][0] and not actions["pause"][0] and not actions["attach-app"][0] and not actions["stop"][0]
    calls = []
    monkeypatch.setattr(window.sai, "call", lambda *args, timeout=120, input=None: calls.append(args) or (0, "", ""))
    actions["resume"][1]()
    assert wait(lambda: ("session", "resume", "work") in calls, 10)
    actions["show"][1]()
    assert window.current_page() == "sandboxes" and window.sandboxes.current().key == "session:work"
    project_actions = [key for key, *_ in window.tab_actions(window.tabs.find(f"project:{one}"))]
    assert project_actions == ["close", "close-others"]
    dict((k, f) for k, _, _, f in window.tab_actions(index))["close-others"]()
    assert keys(window) == ["session:work"]


# -- the window's own controls --


def test_header_buttons_and_view_menu_change_the_window(window, qapp):
    window.show()
    qapp.processEvents()
    buttons = window.window_buttons
    assert set(buttons) == {"minimize", "maximize", "fullscreen", "tray", "quit"}
    assert all(name in theme.GLYPHS for name in ("minimize", "maximize", "restore", "fullscreen", "tray", "quit"))
    assert buttons["maximize"].toolTip() == "Maximize"
    buttons["maximize"].click()
    qapp.processEvents()
    assert window.isMaximized() and buttons["maximize"].toolTip() == "Restore"
    assert window.actions["maximize"].text() == "Restore"
    window.actions["maximize"].trigger()
    qapp.processEvents()
    assert not window.isMaximized() and buttons["maximize"].toolTip() == "Maximize"
    window.actions["fullscreen"].trigger()
    qapp.processEvents()
    assert window.isFullScreen() and window.actions["fullscreen"].isChecked()
    assert buttons["fullscreen"].toolTip() == "Leave full screen (F11)"
    assert window.actions["fullscreen"].shortcut().toString() == "F11"
    buttons["fullscreen"].click()
    qapp.processEvents()
    assert not window.isFullScreen()
    # Without a system tray, hiding into it is greyed out and says why.
    assert window.tray is None
    window.update_window_controls()
    assert not buttons["tray"].isEnabled() and "no system tray" in buttons["tray"].toolTip()
    assert not window.actions["tray"].isEnabled()
    buttons["minimize"].click()
    qapp.processEvents()
    assert window.isMinimized() or window.windowState() & QtCore.Qt.WindowState.WindowMinimized


def test_hide_to_the_tray(window, monkeypatch):
    from test_ui import FakeTray
    window.tray = FakeTray()
    window.update_window_controls()
    assert window.window_buttons["tray"].isEnabled()
    window.show()
    window.window_buttons["tray"].click()
    assert not window.isVisible()


def test_quit_goes_through_the_usual_questions_and_shutdown(window, env, monkeypatch):
    closed = []
    window.closed.connect(lambda: closed.append(True))
    assert window.actions["quit"].shortcut().toString() == "Ctrl+Q"
    fake_run(env, "claude-abc", project(env, "p"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep the window": a sandbox still runs
    window.window_buttons["quit"].click()
    assert not window.finished and not closed and widgets.SHOWN[-1][0] == "Quit"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    window.actions["quit"].trigger()
    assert window.finished and getattr(window, "shut_down", False) and closed == [True]
    assert not window.approvals.timer.isActive() and not window.sandboxes.timer.isActive()


def test_the_theme_menu_applies_and_saves(window):
    actions = window.theme_actions
    assert set(actions) == {key for key, _ in theme.choices()}
    actions["dark"].trigger()
    QtWidgets.QApplication.processEvents()  # applied after the menu's own event (theme.apply_soon)
    assert theme.current() == "dark" and window.sai.settings()["theme"] == "dark" and actions["dark"].isChecked()
    assert window.system.theme.currentData() == "dark"
    window.system.theme.setCurrentIndex(window.system.theme.findData("win311"))
    QtWidgets.QApplication.processEvents()
    assert actions["win311"].isChecked() and not actions["dark"].isChecked()
    actions["system"].trigger()
    QtWidgets.QApplication.processEvents()
    assert theme.current() == "system"


def test_go_help_and_zoom(window, qapp):
    window.actions["page:kits"].trigger()
    assert window.current_page() == "kits"
    before = window.board.view.transform().m11()
    window.actions["zoom-in"].trigger()
    assert window.board.view.transform().m11() > before
    window.actions["about"].trigger()
    title, text, _ = widgets.SHOWN[-1]
    import PySide6
    assert title == "About sandbox-ai-app" and PySide6.__version__ in text and window.sai.version() in text
    window.actions["readme"].trigger()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Read the README" and "sandbox-ai-app" in text
    # On the Terminals page, Ctrl+W and Ctrl+Tab belong to the terminal.
    window.show_page("terminals")
    assert window.actions["close-tab"].shortcuts() == [] and window.actions["next-tab"].shortcuts() == []
    window.show_page("launch")
    assert window.actions["close-tab"].shortcut().toString() == "Ctrl+W"


# -- panels --


def test_panels_follow_their_page_float_close_and_are_kept(window, monkeypatch, qapp):
    window.show()
    docks = window.docks
    window.show_page("board")
    qapp.processEvents()
    assert docks["parts"].isVisible() and not docks["questions"].isVisible()
    # The inspector slides out at the canvas's edge (no panel), the findings wait until asked for.
    assert not docks["inspector"].isVisible() and not docks["findings"].isVisible()
    assert docks["parts"].widget() is window.board.parts_panel and docks["questions"].widget() is window.approvals.open_box
    window.show_page("approvals")
    qapp.processEvents()
    assert docks["questions"].isVisible() and not docks["parts"].isVisible()
    # Floated, the questions stay on every page; closed, the parts stay away until View > Panels.
    docks["questions"].setFloating(True)
    window.show_page("board")
    qapp.processEvents()
    assert docks["questions"].isVisible()
    docks["parts"].close()
    qapp.processEvents()
    assert not docks["parts"].isVisible() and not window.panel_actions["parts"].isChecked()
    window.show_page("launch")
    window.show_page("board")
    assert not docks["parts"].isVisible(), "closed stays closed"
    window.state.flush()
    saved = json.loads(state_file().read_text())["docks"]
    assert saved["closed"] == ["findings", "parts"] and saved["state"]
    again = make_window(monkeypatch)
    try:
        again.show()
        again.show_page("launch")
        qapp.processEvents()
        assert again.docks["questions"].isFloating() and again.docks["questions"].isVisible()
        assert "parts" in again.closed_docks and not again.panel_actions["parts"].isChecked()
        again.panel_actions["parts"].trigger()  # View > Panels brings it back, on its page
        qapp.processEvents()
        assert again.current_page() == "board" and again.docks["parts"].isVisible()
        again.actions["panels-reset"].trigger()
        qapp.processEvents()
        assert not again.docks["questions"].isFloating() and again.closed_docks == {"findings"}
    finally:
        close(again)


def test_switching_tabs_does_not_wait_for_a_slow_disk(window, env, monkeypatch, wait):
    """Regression: a tab switch (and the start, through restore_tabs) read the project's state on the GUI
    thread - project_state for the project bar and the Start page's note: a slow or hanging disk (a network
    file system) froze the window. They are read in the background; the switch returns at once."""
    import threading
    import time
    one, two = project(env, "one"), project(env, "two")
    window.set_project(one)
    window.set_project(two)
    assert wait(lambda: "no .sandbox-ai.json yet" in window.project_bar.state.text(), 10)
    real, gui, slow = window.sai.project_state, threading.get_ident(), []

    def project_state(directory):
        slow.append(threading.get_ident())
        time.sleep(2)
        return real(directory)
    monkeypatch.setattr(window.sai, "project_state", project_state)
    began = time.monotonic()
    window.tabs.setCurrentIndex(0)
    assert time.monotonic() - began < 1, "the switch did not wait for the disk"
    assert window.project == str(one) and window.project_bar.name.text() == "one"
    assert wait(lambda: slow, 5) and gui not in slow, "read in a thread"
    assert wait(lambda: "no .sandbox-ai.json yet" in window.project_bar.state.text(), 15)
    assert wait(lambda: "No .sandbox-ai.json in this project" in window.launch.project_note.text(), 15)
    began = time.monotonic()
    window.tabs.setCurrentIndex(1)
    window.tabs.setCurrentIndex(0)
    assert time.monotonic() - began < 1
    assert wait(lambda: not widgets.busy(window) and len(slow) >= 4, 20)
    assert window.project == str(one) and "no .sandbox-ai.json yet" in window.project_bar.state.text()
