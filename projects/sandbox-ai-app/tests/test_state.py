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
from conftest import read_now  # noqa: E402

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
    # Compact (stability round 3): a third of an indented text's size, made in a tenth of the time on the GUI thread.
    assert "\n" not in path.read_text() and ": " not in path.read_text()


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


@pytest.mark.parametrize("case", ["values", "draft-kind", "draft-values", "draft-places"])
def test_a_state_file_of_valid_json_but_foreign_values_never_stops_the_app(case, qapp, env, monkeypatch):
    """Stability review 2: anyone with the user's rights can edit state.json (another version of the app may have
    written it). Values of the wrong shape - lists where names belong, numbers too large for Qt, a board draft with
    parts or settings this app does not know - are left out: the window is made (it was not: a TypeError at every
    start), and no slot raises when the pages and the board are shown."""
    import sys
    import traceback
    folder = project(env, "p")
    (folder / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
    p = str(folder)
    tab = [{"kind": "project", "path": p}]
    node = {"id": 1, "kind": "agent", "props": {"agent": "claude"}, "x": 0, "y": 0}
    drafts = {"draft-kind": [{"id": 1, "kind": "from-a-newer-app", "props": "s", "x": "a", "y": None}],
              "draft-values": [{"id": 1, "kind": "limits", "props": {"--memory": 5}, "x": 0, "y": 0}],
              "draft-places": [{**node, "x": 1e300}]}
    if case == "values":
        data = {"nav_collapsed": [[1], {}, "zz"], "tabs": [5, {"kind": [1]}, *tab], "tab_current": [1],
                "docks": {"state": "x", "closed": [[1]], "known": [{}], "folded": [[]], "sizes": {"log": []}},
                "splitters": {"approvals": [10 ** 20, 5], "kits": [1, 10 ** 30], "connections": "x"},
                "launch": {"agent": ["x"], "profile": 5, "forms": {"claude": "x"}, "projects": {p: {"agent": {}}}},
                "boards": {p: {"zoom": 1.0, "center": [10 ** 400, 0]}}, "page": "board"}
    else:
        data = {"tabs": tab, "page": "board",
                "boards": {p: {"draft": {"nodes": drafts[case], "edges": [[1, 2]], "extra": [], "document_extra": {}}}}}
    state.state_file().parent.mkdir(parents=True)
    state.state_file().write_text(json.dumps(data))
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: errors.append("".join(traceback.format_exception(*a))))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    w = make_window(monkeypatch)
    try:
        for key in ("board", "approvals", "kits", "connections", "launch"):
            w.show_page(key)
            qapp.processEvents()
        w.board.grab()
        assert not errors, "\n".join(errors)
        assert not w.board.dirty(), "a draft this app cannot show is left out"
    finally:
        close(w)


def test_secrets_never_reach_a_file(window, env, monkeypatch, wait):
    secret = "sk-" + "live-0123456789abcdefSECRETvalue"  # made up; split so no scanner takes it for real
    token = "ghp" + "_AAAAbbbbCCCCdddd1111222233334444"
    folder = project(env, "p")
    window.set_project(folder)
    window.launch.agent_args.setText(f"--model sonnet --api-key {secret} --token={token} {token}")
    for edit in window.findChildren(QtWidgets.QLineEdit):  # every password field holds the secret
        if edit.property("secret_value") or edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password:
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
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog",
                        lambda dialog: asked.append(dialog.directory().absolutePath()) or [str(picked)])
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
        from sandbox_ai_app import logview
        qapp.processEvents()  # (the log window's entries arrive through the event loop)
        assert board.dirty() and not board.notice.isVisibleTo(board), "said in the log window, not above the board"
        assert any("Restored the changes not applied" in e["message"] for e in logview.hub().snapshot())
        assert board.view.transform().m11() == pytest.approx(zoom, rel=1e-3)
        board.undo_group.undo()
        assert board.board.first("limits") is None and not board.dirty(), "Undo goes back to the file"
        board.undo_group.redo()
        board.discard_changes()  # (the canvas menu's Discard the changes not applied; asked first)
        assert board.board.first("limits") is None and not board.dirty()
    finally:
        again.shutdown()
    third = make_window(monkeypatch)
    try:
        assert third.board.board.first("limits") is None and not third.board.dirty(), "discarded stays discarded"
    finally:
        close(third)


def test_the_folds_of_the_sandboxes_tree_come_back(window, env, monkeypatch):
    """Which hosts and starts of the Sandboxes page are open is kept per host and start."""
    from sandbox_ai_app.start_tree import row_id
    folder = project(env, "p")
    run = fake_run(env, "claude-one", folder)
    record = json.loads((run / "run.json").read_text())
    (run / "run.json").write_text(json.dumps({**record, "pod": "sandbox-claude-one", "mcp_servers": ["fetch"]}))
    page = window.sandboxes
    read_now(page)
    start = page.table.find_item(row_id("local", "run:claude-one"))
    assert start.childCount() == 2 and not start.isExpanded()
    start.setExpanded(True)
    page.table.find_item(row_id("local")).setExpanded(False)
    again = restart(window, monkeypatch)
    read_now(again.sandboxes)
    try:
        tree = again.sandboxes.table
        assert tree.find_item(row_id("local", "run:claude-one")).isExpanded()
        assert not tree.find_item(row_id("local")).isExpanded()
    finally:
        close(again)


def test_the_selected_sandbox_comes_back(window, env, monkeypatch):
    folder = project(env, "p")
    fake_run(env, "claude-one", folder)
    fake_run(env, "claude-two", folder)
    page = window.sandboxes
    read_now(page)
    page.table.selectRow(widgets.find_row(page.table, "run:claude-two"))
    page.only_project.setChecked(True)
    again = restart(window, monkeypatch)
    read_now(again.sandboxes)
    try:
        assert again.sandboxes.current().key == "run:claude-two" and again.sandboxes.only_project.isChecked()
    finally:
        close(again)


def test_the_writer_thread_never_blocks_the_gui_and_quitting_waits_for_it(qapp, env, monkeypatch):
    """Performance round 2: the file is written and synced to the disk in a thread of its own (a slow disk never
    holds the window); flush() - as quitting calls it - waits until it is on the disk; only the newest text is
    written when several wait."""
    import threading
    s = state.State(delay=10_000)
    gate, written = threading.Event(), []
    real = backend.write_private_text

    def slow(path, text):
        gate.wait(5)
        written.append(json.loads(text)["n"])
        real(path, text)
    monkeypatch.setattr(backend, "write_private_text", slow)
    begin = time.monotonic()
    for n in range(5):
        s.set("n", n)
        assert s.flush(wait=False)
    assert time.monotonic() - begin < 1, "the GUI thread went on"
    gate.set()
    assert s.flush() and json.loads(s.path.read_text())["n"] == 4
    # (the first text written is whichever waited when the thread started: on a busy machine any of them)
    assert written == sorted(written) and written[-1] == 4 and len(written) <= 3, "only the newest text waited"
    assert stat.S_IMODE(os.stat(s.path).st_mode) == 0o600


def test_a_failed_write_is_said_and_stops_nothing(qapp, env, monkeypatch):
    s = state.State(delay=10_000)
    real = backend.write_private_text

    def full(path, text):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(backend, "write_private_text", full)
    s.set("x", 1)
    assert s.flush() is False
    monkeypatch.setattr(backend, "write_private_text", real)  # (only this one back: env's variables stay)
    assert s.flush() is True


@pytest.mark.parametrize("kind", ["field", "list"])
def test_what_is_being_typed_in_the_inspector_is_kept_when_the_app_quits(window, env, qapp, kind):
    """Stability review 2: a field of the board's inspector is taken when it is left (a list: 600 ms after typing
    stops). Quitting wrote the draft first - what was typed last, in the field still being edited, was lost."""
    from sandbox_ai_app.board_view import LinesEdit
    folder = project(env, "p")
    (folder / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
    window.set_project(folder)
    window.show()
    window.show_page("board")
    page = window.board
    if kind == "field":
        page.add_node("limits")
    node = page.board.first("limits" if kind == "field" else "network")
    for _ in range(3):
        qapp.processEvents()
    page.inspect(node)
    qapp.processEvents()
    if kind == "field":
        edit = [e for e in page.inspector_holder.findChildren(QtWidgets.QLineEdit) if e.isEnabled()][0]
        window.activateWindow()
        edit.setFocus()
        qapp.processEvents()
        assert QtWidgets.QApplication.focusWidget() is edit
        edit.setText("7g")  # typed, the field not left
    else:
        page.inspector_holder.findChildren(LinesEdit)[0].setPlainText("pypi.org")  # typed, the pause not over
    window.shutdown()
    draft = json.loads(state.state_file().read_text())["boards"][str(folder)]["draft"]
    props = next(n["props"] for n in draft["nodes"] if n["id"] == node.id)
    assert ("7g" if kind == "field" else "pypi.org") in json.dumps(props)


@pytest.mark.parametrize("ending", ["logout", "sigterm"])
def test_logout_and_sigterm_write_the_state_before_the_work_is_stopped(window, monkeypatch, ending):
    """Stability review 2: at a logout (commitDataRequest: the moment to save) and at SIGTERM the state - the
    board's changes not applied among it - was written only at the very end, after up to a grace period and
    QUIT_WAIT seconds of stopping the work: a session manager that kills the app meanwhile lost what changed in
    the last moments. Now it is on the disk before the work is stopped."""
    seen = []
    monkeypatch.setattr(QtWidgets.QApplication, "quit", staticmethod(lambda: None))
    monkeypatch.setattr(window, "stop_work", lambda timeout=None: seen.append(
        json.loads(state.state_file().read_text()).get("marker") if state.state_file().exists() else None) or [])
    window.state.set("marker", ending)  # (debounced: not written yet)
    window.end_of_session(None) if ending == "logout" else window.terminate()
    assert seen == [ending]
    window.session_goes_on()


def test_the_writer_survives_any_error_says_a_failure_once_and_quitting_never_waits_forever(qapp, env, monkeypatch):
    """Stability review 2: an error other than OSError ended the writer thread with `writing` left set - no later
    change was written again, and quitting (flush) waited for it for ever. A failed write was said nowhere (the
    board's unapplied changes were silently not kept); a write that hangs (a network home gone) held quitting for
    ever."""
    import threading
    from sandbox_ai_app import applog
    said = []
    monkeypatch.setattr(applog, "log", lambda level, where, message: said.append((level, message)))
    s = state.State(delay=10_000)
    real = backend.write_private_text

    def broken(path, text):
        raise RuntimeError("something unforeseen")
    monkeypatch.setattr(backend, "write_private_text", broken)
    s.set("x", 1)
    assert s.flush() is False
    s.set("x", 2)
    assert s.flush() is False
    assert [level for level, _ in said] == ["WARNING"], "said once, not at every write"
    assert "state.json" in said[0][1] and "something unforeseen" in said[0][1]
    monkeypatch.setattr(backend, "write_private_text", real)
    assert s.flush() is True and json.loads(s.path.read_text())["x"] == 2, "the writer goes on"
    assert said[-1][0] == "INFO", "written again: said too"
    gate = threading.Event()
    monkeypatch.setattr(backend, "write_private_text", lambda path, text: gate.wait(30))
    monkeypatch.setattr(state, "FLUSH_WAIT", 0.3)
    s.set("x", 3)
    begin = time.monotonic()
    assert s.flush() is False and time.monotonic() - begin < 5, "quitting gives up on a write that hangs"
    gate.set()
