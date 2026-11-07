"""What the app keeps for the next start (sandbox_ai_app/state.py): the file itself (round trip, 0600, atomic,
debounced, damaged or partial), and every kind of value through a restart - a second MainWindow in the same
test reads what the first one left. Secrets never reach the file."""
import time
import json
import os
import stat

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, state, widgets  # noqa: E402

from test_projects import close, env, fake_run, make_window, project, window  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def restart(window, monkeypatch):
    """Close WINDOW as the app does (its state is written) and start a new one."""
    window.shutdown()
    return make_window(monkeypatch)


# -- the file --


def test_round_trip_private_atomic_and_debounced(qapp, env, wait):
    s = state.State(delay=50)
    assert s.data == {} and s.problem is None
    s.set("page", "board")
    s.section("tables")["x"] = {"sort": 1}
    s.set("count", 3)
    assert s.writes == 0, "not written at once"
    assert wait(lambda: s.writes == 1, 5)
    path = state.state_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == [], "no temporary file left"
    again = state.State()
    assert again.get("page", "") == "board" and again.get("count", 0) == 3 and again.section("tables")["x"]["sort"] == 1
    # A value of the wrong type falls back to the default.
    assert again.get("page", 0) == 0 and again.get("count", "") == "" and again.get("missing", []) == []
    again.data["flag"] = True
    assert again.get("flag", 1) == 1, "a bool is not a number"


def test_a_damaged_file_is_put_aside_and_defaults_apply(qapp, env):
    path = state.state_file()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    s = state.State()
    assert s.data == {} and "state.json" in s.problem and "kept as state.json.damaged-" in s.problem
    assert not path.exists() and list(path.parent.glob("state.json.damaged-*"))
    path.write_text(json.dumps(["a", "list"]))
    assert state.State().problem.endswith(")") and state.State().data == {}
    # Partial: the right shape, wrong types inside - every value falls back on its own.
    path.write_text(json.dumps({"page": 7, "tabs": "x", "window": [], "launch": {"forms": "x"}, "boards": {"/p": 3}}))
    s = state.State()
    assert s.problem is None and s.get("page", "") == "" and s.get("tabs", []) == [] and s.get("window", {}) == {}


def test_a_damaged_file_at_start_says_so_and_the_window_works(qapp, env, monkeypatch):
    state.state_file().parent.mkdir(parents=True)
    state.state_file().write_text("\0garbage")
    backend.settings_file().write_text("{")
    w = make_window(monkeypatch)
    try:
        assert w.notice_bar.isVisibleTo(w)
        text = w.notice_bar.text()
        assert "state.json" in text and "settings.json" in text and "defaults" in text
        assert not backend.settings_file().exists() and list(backend.settings_file().parent.glob("settings.json.damaged-*"))
        assert w.current_page() == "launch" and w.tabs.count() == 0
    finally:
        close(w)


def test_secrets_never_reach_a_file(window, env, monkeypatch, wait):
    secret = "sk-" + "live-0123456789abcdefSECRETvalue"  # made up; split so no scanner takes it for real
    token = "ghp" + "_AAAAbbbbCCCCdddd1111222233334444"
    folder = project(env, "p")
    window.set_project(folder)
    window.launch.agent_args.setText(f"--model sonnet --api-key {secret} --token={token} {token}")
    for edit in window.findChildren(QtWidgets.QLineEdit):  # every password field holds the secret
        if edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password:
            edit.setText(secret)
    window.board.set_prop(window.board.board.first("agent").id, "arguments", f"--verbose --api-key {secret}")
    window.connections.filter.setText("example.org")
    window.state.flush()
    window.shutdown()
    written = state.state_file().read_text()
    settings = backend.settings_file().read_text() if backend.settings_file().exists() else ""
    assert secret not in written and token not in written and secret not in settings and token not in settings
    saved = json.loads(written)
    assert saved["launch"]["forms"]["claude"]["arguments"] == ["--model", "sonnet"]
    draft = saved["boards"][str(folder)]["draft"]
    agent = next(n for n in draft["nodes"] if n["kind"] == "agent")
    assert agent["props"]["arguments"] == "--verbose", "the board's draft is kept without the secret"
    assert saved["texts"]["connections-filter"] == "example.org"


def test_scrub_arguments():
    assert state.scrub_arguments(["--model", "x", "--api-key", "v", "--auth-token=t", "-p", "hi"]) == [
        "--model", "x", "-p", "hi"]
    assert state.scrub_arguments(["sk-abc", "xoxb-1", "AKIA" + "IOSFODNN7EXAMPLE", "plain words"]) == ["plain words"]
    assert state.scrub_arguments(["0123456789abcdef0123456789abcdef01"]) == []
    assert state.scrub_arguments(["/srv/data/a/very/long/path/that/is/fine/1234567890"]) == [
        "/srv/data/a/very/long/path/that/is/fine/1234567890"]


# -- through a restart --


def test_window_page_navigation_and_panels_come_back(window, monkeypatch, qapp):
    from sandbox_ai_app import ui
    window.show()
    window.resize(1200, 760)
    qapp.processEvents()
    left = bytes(window.saveGeometry())
    restored = []
    real = ui.MainWindow.restoreGeometry
    monkeypatch.setattr(ui.MainWindow, "restoreGeometry", lambda self, data: restored.append(bytes(data)) or
                        real(self, data))
    again = restart(window, monkeypatch)
    try:
        assert restored == [left], "the window's geometry as it was left (the screen may still shrink it)"
    finally:
        close(again)
    window = make_window(monkeypatch)
    window.show()
    window.show_page("statistics")
    window.toggle_section("Insight")
    window.toggle_section("Manage")
    window.toggle_section("Manage")
    insight = window.section_rows["Insight"][1]
    assert all(window.nav.isRowHidden(r) for r in insight)
    window.showMaximized()
    qapp.processEvents()
    again = restart(window, monkeypatch)
    try:
        again.show()
        qapp.processEvents()
        assert again.current_page() == "statistics" and again.isMaximized()
        assert again.collapsed == {"Insight"} and all(again.nav.isRowHidden(r) for r in insight)
        assert not any(again.nav.isRowHidden(r) for r in again.section_rows["Manage"][1])
        # A click on a section's header folds it (and unfolds it).
        header = again.nav.item(again.section_rows["Running"][0])
        position = again.nav.visualItemRect(header).center()
        from PySide6.QtGui import QMouseEvent
        release = QMouseEvent(QtCore.QEvent.Type.MouseButtonRelease, QtCore.QPointF(position),
                              QtCore.QPointF(again.nav.viewport().mapToGlobal(position)),
                              QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.NoButton,
                              QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(again.nav.viewport(), release)
        assert "Running" in again.collapsed
    finally:
        close(again)
        close(window)


def test_tables_splitters_filters_and_folders_come_back(window, env, monkeypatch, qapp):
    logs = env / "state" / "sandbox-ai" / "logs" / "proj-1"
    logs.mkdir(parents=True)
    (logs / "connections-1.jsonl").write_text(json.dumps({"time": "t", "event": "http", "url": "https://a/"}) + "\n")
    window.connections.refresh_directories(select=str(logs))
    table = window.connections.table
    table.sortByColumn(1, QtCore.Qt.SortOrder.DescendingOrder)
    monkeypatch.setattr(state, "dragging", lambda: True)  # the human drags a column wider
    table.horizontalHeader().resizeSection(0, 333)
    monkeypatch.setattr(state, "dragging", lambda: False)
    window.connections.split.setSizes([300, 100])
    window.connections.split.splitterMoved.emit(300, 1)
    window.connections.filter.setText("pypi")
    window.connections.refused.setChecked(True)
    window.connections.follow.setChecked(False)
    picked = env / "picked"
    picked.mkdir()
    asked = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        lambda parent, title, start="": asked.append(start) or str(picked))
    window.files.add_other()
    again = restart(window, monkeypatch)
    try:
        table = again.connections.table
        header = table.horizontalHeader()
        assert header.sortIndicatorSection() == 1 and header.sortIndicatorOrder() == QtCore.Qt.SortOrder.DescendingOrder
        assert header.sectionSize(0) == 333 and table.user_sized
        widgets.fill(table, [["x", "y", "", "", "", ""]])
        assert header.sectionSize(0) == 333, "a width the human gave is not fitted away"
        sizes = again.connections.split.sizes()
        assert sizes[0] > sizes[1]
        assert again.connections.filter.text() == "pypi" and again.connections.refused.isChecked()
        assert not again.connections.follow.isChecked()
        deadline = time.monotonic() + 10  # the log folders are listed in the background
        while again.connections.directory.currentData() != str(logs) and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert again.connections.directory.currentData() == str(logs)
        again.files.add_other()
        assert asked[-1] == str(picked), "the file dialog starts where it ended last time"
    finally:
        close(again)


def test_the_start_form_per_agent_comes_back_without_secrets(window, env, monkeypatch):
    launch = window.launch
    launch.form.widgets["--allow-host"].set_value(["example.org"])
    launch.agent_args.setText("--model opus --api-key " + "sk-" + "secret-abcdefghijklmnop0123")
    launch.agent.setCurrentText("codex")
    assert launch.form.values()["--allow-host"] == ["example.org"], "an agent without a saved form keeps the form"
    launch.form.widgets["--allow-host"].set_value(["codex.example"])
    launch.agent_args.setText("--model gpt")
    launch.agent.setCurrentText("claude")
    assert launch.form.values()["--allow-host"] == ["example.org"] and launch.agent_args.text() == "--model opus"
    launch.agent.setCurrentText("codex")
    again = restart(window, monkeypatch)
    try:
        assert again.launch.agent.currentText() == "codex"
        assert again.launch.form.values()["--allow-host"] == ["codex.example"]
        assert again.launch.agent_args.text() == "--model gpt"
        again.launch.agent.setCurrentText("claude")
        assert again.launch.form.values()["--allow-host"] == ["example.org"]
        assert again.launch.agent_args.text() == "--model opus"
    finally:
        close(again)


def test_an_unapplied_board_draft_comes_back_with_undo_and_discard(window, env, monkeypatch, qapp):
    folder = project(env, "p")
    (folder / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": ["--offline"]}))
    window.set_project(folder)
    window.board.add_node("limits", **{"--memory": "6g"})
    window.board.view.zoom(1.7)
    zoom = window.board.view.transform().m11()
    again = restart(window, monkeypatch)
    try:
        board = again.board
        assert board.directory == str(folder) and board.board.first("limits").props["--memory"] == "6g"
        assert board.dirty() and board.notice.isVisibleTo(board) and board.discard_button.isVisibleTo(board)
        assert "Restored the changes not applied" in board.notice.text()
        assert board.view.transform().m11() == pytest.approx(zoom, rel=1e-3)
        board.undo_group.undo()
        assert board.board.first("limits") is None and not board.dirty(), "Undo goes back to the file"
        board.undo_group.redo()
        board.discard_button.click()
        assert board.board.first("limits") is None and not board.dirty() and not board.discard_button.isVisibleTo(board)
    finally:
        again.shutdown()
    third = make_window(monkeypatch)
    try:
        assert third.board.board.first("limits") is None and not third.board.dirty(), "discarded stays discarded"
    finally:
        close(third)


def test_the_selected_sandbox_comes_back(window, env, monkeypatch):
    folder = project(env, "p")
    fake_run(env, "claude-one", folder)
    fake_run(env, "claude-two", folder)
    page = window.sandboxes
    page.refresh()
    page.table.selectRow(widgets.find_row(page.table, "run:claude-two"))
    page.only_project.setChecked(True)
    again = restart(window, monkeypatch)
    try:
        assert again.sandboxes.current().key == "run:claude-two" and again.sandboxes.only_project.isChecked()
    finally:
        close(again)
