"""Projects, the navigation's order and every sandbox-ai instance of the account, offscreen against fake
records (run registry, sessions, MCP state, launcher processes that are harmless sleepers): no sandbox runs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, theme, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def make_window(monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    w.sai.in_terminal = lambda argv, title="": ["term", *argv]
    return w


def close(w):
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.fixture
def window(qapp, env, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    started = []
    monkeypatch.setattr("sandbox_ai_app.ui.QProcess.startDetached",
                        lambda program, args: started.append([program, *args]) or True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    w = make_window(monkeypatch)
    w.started = started
    yield w
    close(w)


@pytest.fixture
def sleepers():
    """Harmless processes that look like sandbox-ai launchers or supervisors (their arguments do)."""
    started = []

    def start(*arguments):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *map(str, arguments)])
        started.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b"time.sleep" not in Path(f"/proc/{process.pid}/cmdline").read_bytes():
            time.sleep(0.01)
        return process
    yield start
    for process in started:
        process.kill()
        process.wait()


def project(env, name):
    folder = env / name
    folder.mkdir(exist_ok=True)
    return folder


def fake_run(env, name, workspace, session=None, pid=None, agent="claude"):
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": agent, "backend": "bwrap", "workspace": str(workspace), "pid": pid or os.getpid(),
        "session": session, "session_dir": str(env / "s"), "started": "2026-11-03T10:00:00Z", "proxy": True}))
    return directory


def fake_session(env, name, workspace):
    folder = env / "state" / "sandbox-ai" / "sessions" / name
    (folder / "home").mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"agent": "codex", "options": [], "backend": "podman",
                                                    "workspace": str(workspace), "cwd": str(workspace),
                                                    "created": "2026-11-02T09:00:00Z"}))
    return folder


def ask(run, ident, **question):
    asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (run / "approvals" / f"{ident}.json").write_text(json.dumps({"id": ident, "time": asked, "timeout": 60, **question}))


def bold(table, row):
    return table.item(row, 0).font().bold()


# -- the navigation --


def test_navigation_has_sections_shortcuts_and_a_glyph_per_page(window, qapp):
    from sandbox_ai_app.ui import PAGES, SECTIONS
    headers = [window.nav.item(r) for r in range(window.nav.count()) if window.nav.item(r).data(QtCore.Qt.ItemDataRole.UserRole)
               is None]
    assert [h.text() for h in headers] == ["PROJECT", "RUNNING", "INSIGHT", "MANAGE", "SYSTEM"]
    assert all(not (h.flags() & QtCore.Qt.ItemFlag.ItemIsSelectable) for h in headers), "headers cannot be chosen"
    assert [key for _, pages in SECTIONS for key, _ in pages] == [key for key, _ in PAGES] == window.keys
    for key in window.keys:
        assert key in theme.GLYPHS and not window.nav_item(key).icon().isNull(), key
    # Ctrl+1 ... Ctrl+9: the first nine pages in the navigation's order.
    assert [s.shortcut().toString() for s in window.shortcuts] == [f"Ctrl+{n}" for n in range(1, 10)]
    window.shortcuts[5].trigger()
    assert window.pages.currentWidget() is getattr(window, window.keys[5]) and window.current_page() == "connections"
    assert "Ctrl+6" in window.nav_item("connections").toolTip()
    window.nav.setCurrentRow(0)  # a header: the page stays
    assert window.current_page() == "connections"
    window.show_page("kits")
    assert window.pages.currentWidget() is window.kits


# -- projects --


def test_known_projects_come_from_sandbox_ais_records_and_the_apps_list(window, env, wait):
    sai = window.sai
    trusted, ran, sess, recent, gone = (project(env, n) for n in ("trusted", "ran", "sess", "recent", "gone"))
    (trusted / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
    sai.launcher.trust_config(trusted / ".sandbox-ai.json")
    fake_run(env, "claude-1", ran)
    fake_session(env, "work", sess)
    sai.remember_project(recent)
    sai.remember_project(gone)
    gone.rmdir()
    known = dict(sai.known_projects())
    assert known[str(trusted)] == {"trusted"} and known[str(ran)] == {"running"} and known[str(sess)] == {"session"}
    assert known[str(recent)] == {"recent"} and str(gone) not in known, "only folders that exist"
    assert [path for path, _ in sai.known_projects()][0] == str(recent), "the app's own list first, newest first"
    window.project_bar.fill()  # read in the background: the list changes when it is known
    combo = window.project_bar.combo
    assert wait(lambda: str(trusted) in [combo.itemData(i) for i in range(combo.count())])
    items = [combo.itemData(i) for i in range(combo.count())]
    assert str(trusted) in items and str(ran) in items and items[-2:] == [":open", ":new"]
    tip = combo.itemData(items.index(str(trusted)), QtCore.Qt.ItemDataRole.ToolTipRole)
    assert "trusted project configuration" in tip


def test_switching_the_project_drives_the_pages_and_is_kept(window, env, monkeypatch, qapp, wait):
    one, two = project(env, "one"), project(env, "two")
    (two / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--offline"]}))
    assert window.set_project(one)
    assert window.project == str(one) and window.project_bar.name.text() == "one"
    assert wait(lambda: "no .sandbox-ai.json yet" in window.project_bar.state.text())  # read in a thread
    assert window.launch.form.values()["--workspace"] == [str(one)]
    assert window.board.directory == str(one) and window.board.notice.isVisibleTo(window.board)
    assert wait(lambda: window.files.roots.currentData() == str(one))  # read in a thread
    log_dir = str(window.sai.launcher.default_log_dir(one))
    assert wait(lambda: window.connections.directory.currentData() == log_dir)  # listed in a thread
    assert window.statistics.directory.currentData() == log_dir
    assert "one" in window.windowTitle()
    # The project bar's list switches too (two is known: a sandbox runs there).
    fake_run(env, "codex-two", two)
    bar = window.project_bar
    bar.fill()
    assert wait(lambda: bar.combo.findData(str(two)) >= 0)
    bar.activated(bar.combo.findData(str(two)))
    assert window.project == str(two) and window.board.board.first("agent").props["agent"] == "codex"
    assert wait(lambda: "not trusted yet" in bar.state.text() and window.launch.review_button.isVisibleTo(window.launch))
    assert window.sai.settings()["project"] == str(two)
    assert window.sai.recent_projects()[:2] == [str(two), str(one)]
    # The next start opens it again.
    window.state.flush()
    again = make_window(monkeypatch)
    try:
        assert again.project == str(two) and again.board.directory == str(two)
    finally:
        close(again)


def test_switching_keeps_each_projects_board_and_reopening_asks(window, env, monkeypatch):
    one, two = project(env, "one"), project(env, "two")
    window.set_project(one)
    assert not window.board.dirty()
    window.board.add_node("limits", **{"--memory": "4g"})
    assert window.board.dirty()
    assert window.set_project(two)  # another tab: one's board is kept as it was
    assert window.board.directory == str(two) and window.board.board.first("limits") is None
    assert window.board.dirty_for(one) and not window.board.dirty()
    window.set_project(one)
    assert window.board.board.first("limits") is not None and window.board.dirty()
    # Reading the same project again on the Board drops its changes: only after asking.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep them"
    assert window.board.open_project(str(one)) is False and window.board.board.first("limits") is not None
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Changes not applied" and "one" in text


def test_open_folder_and_new_project_from_the_switcher(window, env, monkeypatch):
    folder = project(env, "opened")
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [str(folder)])
    bar = window.project_bar
    bar.activated(bar.combo.findData(bar.OPEN))
    assert window.project == str(folder)
    made = project(env, "made")
    opened = []

    def wizard(ask=True):
        opened.append(ask)
        window.board.load(window.board.board, str(made))  # what the wizard does when it has written the project
    monkeypatch.setattr(window.board, "open_wizard", wizard)
    bar.activated(bar.combo.findData(bar.NEW))
    assert opened == [False] and window.project == str(made) and window.current_page() == "board"
    assert window.project_bar.name.text() == "made"


def test_applying_the_board_trusts_and_shows_the_project_state(window, env, wait):
    folder = project(env, "applied")
    window.set_project(folder)
    window.board.add_node("limits", **{"--memory": "4g"})
    assert window.board.apply()
    assert wait(lambda: (folder / ".sandbox-ai.json").exists() and not window.board.dirty(), 20)
    assert wait(lambda: ".sandbox-ai.json trusted" in window.project_bar.state.text(), 10)


def test_approvals_of_the_current_project_stand_out(window, env):
    mine, other = project(env, "mine"), project(env, "other")
    run_mine = fake_run(env, "claude-mine", mine)
    run_other = fake_run(env, "claude-other", other)
    ask(run_mine, "1700000000-1-1", kind="dns", name="pypi.org")
    ask(run_other, "1700000000-1-2", kind="dns", name="example.org")
    window.set_project(mine)
    page = window.approvals
    assert page.open.rowCount() == 2 and page.project_pending == 1
    rows = {widgets.row_key(page.open, r)[1]: r for r in range(page.open.rowCount())}  # (host, run, id)
    assert bold(page.open, rows["claude-mine"]) and not bold(page.open, rows["claude-other"])
    assert "(1 of the current project)" in page.open_box.title()
    assert "open questions: 1" in window.project_bar.activity.text()
    window.set_project(other)
    rows = {widgets.row_key(page.open, r)[1]: r for r in range(page.open.rowCount())}  # (host, run, id)
    assert bold(page.open, rows["claude-other"]) and not bold(page.open, rows["claude-mine"])


# -- every sandbox-ai instance --


def test_every_instance_of_the_account_is_listed_with_its_kind(window, env, sleepers):
    sai = window.sai
    one, two = project(env, "one"), project(env, "two")
    fake_run(env, "claude-abc", one)  # a one-off run (this test process stands in for its launcher)
    fake_session(env, "work", two)  # a stopped session
    # A running MCP server: its supervisor's arguments name its socket folder.
    mcp = sai.module("mcp")
    server = sleepers(f"--mcp-socket-dir={sai.launcher.mcp_socket_dir('demo')}", sai.launcher.mcp_socket_dir("demo"))
    mcp.save_state("demo", {"pid": server.pid, "pid_start": sai.launcher.process_start(server.pid),
                            "backend": "bwrap", "started": "2026-11-03T11:00:00Z"})
    # A launcher that has not registered (yet), and a dry run, which starts nothing.
    unregistered = sleepers("/opt/sandbox-ai/agent_sandbox.py", "codex", "--workspace", one, "--backend", "bwrap")
    sleepers("/opt/sandbox-ai/agent_sandbox.py", "claude", "--dry-run")
    found, problems = sai.instances()
    by = {i.key: i for i in found}
    assert problems == []
    assert by["run:claude-abc"].kind == "run" and by["run:claude-abc"].project == str(one)
    assert by["session:work"].kind == "session" and by["session:work"].state == "stopped" and not by["session:work"].run
    assert by["mcp:demo"].kind == "mcp" and by["mcp:demo"].backend == "bwrap" and by["mcp:demo"].state == "running"
    loose = by[f"pid:{unregistered.pid}"]
    assert loose.kind == "unregistered" and loose.agent == "codex" and loose.project == str(one)
    assert loose.backend == "bwrap" and loose.started.endswith("Z")
    assert not [i for i in found if i.kind == "unregistered" and i.agent == "claude"], "dry runs are left out"
    page = window.sandboxes
    window.set_project(one)
    page.refresh()
    kinds = {widgets.row_key(page.table, r): page.table.item(r, 0).text() for r in range(page.table.rowCount())}
    assert kinds["run:claude-abc"] == "Run" and kinds["session:work"] == "Session"
    assert kinds["mcp:demo"] == "MCP server" and kinds[f"pid:{unregistered.pid}"] == "Not registered"
    assert bold(page.table, widgets.find_row(page.table, "run:claude-abc")), "the current project's rows stand out"
    assert "in the current project" in page.summary.text()
    page.only_project.setChecked(True)
    assert {widgets.row_key(page.table, r) for r in range(page.table.rowCount())} == {"run:claude-abc",
                                                                                      f"pid:{unregistered.pid}"}


def test_a_session_that_runs_is_one_row_with_its_run(window, env):
    folder = project(env, "s")
    fake_session(env, "work", folder)
    fake_run(env, "session-work", folder, session="agent-session-work", agent="codex")
    found, _ = window.sai.instances()
    assert [(i.kind, i.key, i.run) for i in found] == [("session", "session:work", "session-work")]


def test_buttons_follow_the_kind_and_say_why_not(window, env, sleepers):
    page = window.sandboxes
    folder = project(env, "p")
    fake_run(env, "claude-abc", folder)
    fake_session(env, "work", folder)
    loose = sleepers("/x/agent_sandbox.py", "claude", "--workspace", folder)
    page.refresh()
    page.table.selectRow(widgets.find_row(page.table, "run:claude-abc"))
    live_mcp = [page.run_named[key] for key in ("add-mcp", "remove-mcp")]
    assert all(b.isEnabled() for b in page.run_buttons if b not in live_mcp)
    assert not any(b.isEnabled() for b in live_mcp) and "Only for kept starts" in live_mcp[0].toolTip(), \
        "a run without a kept start takes no MCP server while it runs"
    assert not page.session_buttons["attach"].isEnabled() and "Only for sessions" in page.session_buttons["attach"].toolTip()
    assert page.stop_button.isEnabled() == page.stoppable
    page.table.selectRow(widgets.find_row(page.table, "session:work"))
    assert page.session_buttons["resume"].isEnabled() and page.session_buttons["delete"].isEnabled()
    assert not page.session_buttons["attach"].isEnabled() and "stopped" in page.session_buttons["attach"].toolTip()
    # Review 5 (A3): an MCP server can be taken out of a stopped session's options; nothing else runs on it.
    assert not page.stop_button.isEnabled() and page.run_named["remove-mcp"].isEnabled()
    assert not any(b.isEnabled() for b in page.run_buttons if b is not page.run_named["remove-mcp"])
    page.table.selectRow(widgets.find_row(page.table, f"pid:{loose.pid}"))
    assert not page.stop_button.isEnabled() and "not registered" in page.stop_button.toolTip()
    assert not page.run_buttons[0].isEnabled() and "terminal" in page.run_buttons[0].toolTip()


def test_stop_runs_the_command_of_its_kind(window, env, monkeypatch, sleepers, wait):
    page = window.sandboxes
    folder = project(env, "p")
    fake_run(env, "claude-abc", folder)
    fake_session(env, "work", folder)
    calls = []
    monkeypatch.setattr(window.sai, "call", lambda *args, timeout=120, input=None: calls.append(args) or (0, "", ""))
    page.stoppable = True
    page.refresh()
    page.table.selectRow(widgets.find_row(page.table, "run:claude-abc"))
    page.stop()
    assert wait(lambda: ("net", "stop", "claude-abc") in calls, 10)
    assert widgets.SHOWN[-1][0] == "Stop" and "run claude-abc" in widgets.SHOWN[-1][1], "asked first"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.table.selectRow(widgets.find_row(page.table, "run:claude-abc"))
    page.stop()
    assert len([c for c in calls if c[:2] == ("net", "stop")]) == 1, "Cancel stops nothing"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    # A running session: sandbox-ai session stop (its state is kept).
    fake_run(env, "session-work", folder, session="agent-session-work")
    session_dir = env / "state" / "sandbox-ai" / "sessions" / "work"
    supervisor = sleepers("/x/agent_sandbox.py", "codex", "--tmux-socket-dir", session_dir / "tmux")
    data = json.loads((session_dir / "session.json").read_text())
    data.update(pid=supervisor.pid, pid_start=window.sai.launcher.process_start(supervisor.pid))
    (session_dir / "session.json").write_text(json.dumps(data))
    assert wait(lambda: not widgets.busy(page), 10)  # the stop before has finished (its button waits until then)
    page.refresh()
    page.table.selectRow(widgets.find_row(page.table, "session:work"))
    assert page.current().state == "running" and page.stop_button.isEnabled(), page.stop_button.toolTip()
    page.stop()
    assert wait(lambda: ("session", "stop", "work") in calls, 10)


def test_a_foreground_start_has_nothing_to_attach_to(window, env, sleepers):
    """A kept start without tmux (--no-tmux, a task) is a session that runs, but Attach would fail: it says why."""
    page = window.sandboxes
    folder = project(env, "p")
    fake_session(env, "work", folder)
    session_dir = env / "state" / "sandbox-ai" / "sessions" / "work"
    supervisor = sleepers("/x/agent_sandbox.py", "codex", "--workspace", folder)
    data = json.loads((session_dir / "session.json").read_text())
    data.update(mode="foreground", pid=supervisor.pid, pid_start=window.sai.launcher.process_start(supervisor.pid))
    (session_dir / "session.json").write_text(json.dumps(data))
    found, _ = window.sai.instances()
    assert [(i.key, i.foreground) for i in found if i.kind == "session"] == [("session:work", True)]
    page.refresh()
    page.table.selectRow(widgets.find_row(page.table, "session:work"))
    if page.current().state != "running":
        pytest.skip("this sandbox-ai does not see the fake supervisor as running")
    for action in ("attach", "attach-app"):
        assert not page.session_buttons[action].isEnabled()
        assert "--no-tmux" in page.session_buttons[action].toolTip()
    assert page.session_buttons["pause"].isEnabled()  # the rest as for any running session


def test_the_run_registry_of_another_runtime_folder_is_found(window, env, monkeypatch):
    """A run started where XDG_RUNTIME_DIR differed registered under the state folder: still listed (with a
    sandbox-ai that looks there; older ones do not)."""
    if "stop" not in window.sai.net_commands():
        pytest.skip("this sandbox-ai reads only one run registry")
    folder = project(env, "elsewhere")
    directory = env / "state" / "sandbox-ai" / "runs" / "codex-other"
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({"run": "codex-other", "agent": "codex", "backend": "podman",
                                                    "workspace": str(folder), "pid": os.getpid(), "session": None,
                                                    "started": "2026-11-03T10:00:00Z", "proxy": True}))
    found, _ = window.sai.instances()
    assert "run:codex-other" in {i.key for i in found}


def test_helpers_read_processes_and_options():
    assert backend.option_value(["--backend", "bwrap", "--backend=podman", "--", "--backend", "x"], "--backend") == "podman"
    assert backend.option_value(["-w", "x"], "--backend") is None
    started = backend.process_started(os.getpid())
    assert started and abs(backend.parse_time(started) - time.time()) < 24 * 3600
    assert backend.process_started(10 ** 9) is None
    assert all(pid != os.getpid() for pid, *_ in backend.launcher_processes())
