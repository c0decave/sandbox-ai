"""Logging, round 2 (by scenario): what a user does in the app, driven offscreen, then app.log read as someone
debugging their report would - what was asked, what the app decided and why, what it ran and how it ended can be
told from the log alone, a secret never reaches it. The log window narrows the entries to one area and copies a bug
report (the end of app.log and launcher.log with the versions, secrets masked)."""
import json
import os
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import applog, backend, files, logview, prerequisites, widgets  # noqa: E402
from test_ui import ask, env, fake_run, window  # noqa: E402,F401 - the isolated home and window of the UI tests
from test_terminal_reopen import sai, stub_web, terminal  # noqa: E402,F401 - real terminal tabs, a stand-in sandbox-ai
from test_live_regressions import board_page, user  # noqa: E402,F401 - the real sandbox-ai, Podman faked

TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def logged(monkeypatch):
    """Every log entry (DEBUG too) as (level, where, message), as the log window gets them."""
    entries = []
    monkeypatch.setattr(applog, "_listeners", [])
    monkeypatch.setattr(applog, "_debug_wanted", True)
    monkeypatch.setattr(applog, "_repeat", {"key": None, "count": 0, "since": 0.0})
    monkeypatch.setattr(applog, "_changes", {})
    applog.add_listener(lambda entry: entries.append((entry["level"], entry["where"], entry["message"])))
    return entries


def lines(entries, level=None, where=None):
    return [f"{lv} {at}: {message}" for lv, at, message in entries
            if (level is None or lv == level) and (where is None or at == where)]


def said(entries, text, level=None, where=None):
    """True when an entry (of LEVEL, from WHERE) contains TEXT."""
    return any(text in line for line in lines(entries, level, where))


@pytest.fixture
def answers(window, monkeypatch):
    """The window's sandbox-ai with the commands in the table faked ({args prefix: (status, out, err)}) at the level
    of its process runner - so call() logs them as it logs every command; the rest run the real sandbox-ai."""
    table = {}
    real = window.sai.run

    def run(argv, **kw):
        args = tuple(argv[3:])
        for prefix, result in table.items():
            if args[:len(prefix)] == prefix:
                return subprocess.CompletedProcess(argv, *result)
        return real(argv, **kw)
    monkeypatch.setattr(window.sai, "run", run)
    return table


def start(name, state="running", kind="session", host="local"):
    return backend.Instance(kind, f"{kind}:{host}:{name}", name, run=f"r-{name}", session=name if kind == "session"
                            else None, agent="claude", state=state, host=host)


def settle(wait, entries, text, timeout=10):
    assert wait(lambda: said(entries, text), timeout), "\n".join(lines(entries))


# -- Sandboxes & sessions: stop, resume, delete, rename, stop all, a refusal --

def test_the_sandboxes_page_logs_what_was_asked_what_ran_and_how_it_ended(window, logged, answers, wait,
                                                                          monkeypatch):
    page = window.sandboxes
    monkeypatch.setattr(page, "refresh_soon", lambda again=False: None)
    monkeypatch.setattr(page, "refresh_remote", lambda hosts=None, polled=False: None)
    web = start("web")
    page.instances = {web.key: web}
    monkeypatch.setattr(page, "current", lambda: web)
    monkeypatch.setattr(page, "several", lambda: False)
    answers[("session", "stop")] = (0, "stopped web\n", "")
    page.stop()
    settle(wait, logged, "exit status 0 after")
    assert said(logged, "stop: asked for session web on this machine (state running), wait", "INFO", "sandboxes")
    assert said(logged, "asked: Stop: Stop session web?", "INFO", "SandboxesPage"), "the page, not its scroll area"
    assert said(logged, "started: sandbox-ai command: session stop web for SandboxesPage", "INFO", "background")
    assert said(logged, "sandbox-ai session stop web", "INFO", "command")
    assert not said(logged, "(exit status, stdout, stderr)"), "a command is named by itself, not by call()'s docstring"
    del logged[:]
    answers[("session", "stop")] = (1, "", "Error: session web: not running\n")
    page.stop()
    settle(wait, logged, "warning shown: Stop")
    assert said(logged, "exit status 1 after", "WARNING", "command")
    assert said(logged, "Error: session web: not running", "WARNING", "command"), "the tail of its error output"
    del logged[:]
    answers[("session", "delete")] = (0, "deleted\n", "")
    page.session("delete")
    settle(wait, logged, "session delete web --yes")
    assert said(logged, "delete: asked for session web on this machine", "INFO", "sandboxes")
    assert said(logged, "asked: Delete: Delete session web", "INFO")
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("web2", True))
    answers[("rename",)] = (0, "renamed\n", "")
    page.rename()
    settle(wait, logged, "sandbox-ai rename web web2")
    assert said(logged, "rename: asked for session web on this machine (state running), new name web2", "INFO")
    # A start that is no session: the refusal is in the log, not only in the box.
    run = start("one-off", kind="run")
    monkeypatch.setattr(page, "current", lambda: run)
    page.session("attach")
    assert said(logged, "attach: not done - run one-off is not a session", "INFO", "sandboxes")
    # Stop all: what it stops, and how each host's stop ended.
    page.instances = {web.key: web, run.key: run}
    page.remote = {}
    answers[("stop", "--all")] = (0, "stopped 2\n", "")
    assert page.stop_all() is True
    settle(wait, logged, "stop all ended")
    assert said(logged, "stop all: asked (this machine: web, one-off)", "INFO", "sandboxes")
    assert said(logged, "stop all ended: this machine exit status 0", "INFO", "sandboxes")


def test_a_background_call_of_a_sandbox_ai_command_names_it_and_never_its_secret(qapp, logged, wait):
    sai = backend.SandboxAI(runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    results = []
    widgets.background(sai.call, "secrets", "add", "--token", TOKEN, work=True, done=lambda r, e: results.append(r))
    assert wait(lambda: results, 10)
    text = "\n".join(lines(logged))
    assert "started: sandbox-ai command: secrets add --token ***" in text, text
    assert "done in" in text and "sandbox-ai command: secrets add --token ***" in text.split("done in", 1)[1]
    assert TOKEN not in text


# -- a crash in what handles a background call's result --

def test_a_crash_in_a_background_callback_names_the_call_and_the_app_goes_on(qapp, logged, wait, capfd):
    def look_up_the_sessions():
        """Read the sessions of the project."""
        return {"sessions": []}

    def broken(result, error):
        raise KeyError("state")
    widgets.background(look_up_the_sessions, done=broken)
    assert wait(lambda: said(logged, "KeyError"), 10), lines(logged)
    entry = next(line for line in lines(logged, "ERROR"))
    assert "KeyError: 'state'" in entry and "broken" in entry, entry
    written = capfd.readouterr().err
    assert "(delivered by the result of a background call: " in written, written
    assert "look_up_the_sessions (Read the sessions of the project))" in written, written
    results = []
    widgets.background(lambda: 2, done=lambda r, e: results.append(r))
    assert wait(lambda: results == [2], 10), "the app goes on"


# -- the Start page --

def test_the_start_page_says_what_start_was_asked_and_where_it_went(window, env, logged, monkeypatch):
    """A Start from the Expert view of the Start page is the board's start (one start, two views): the log says what
    it was asked, why it did not start, and where it went."""
    launch = window.launch
    launch.agent.setCurrentText("codex")
    assert launch.start() is False
    assert said(logged, "Start: the board of a new board with codex on local", "INFO", "board")
    assert said(logged, "Not started: Add a workspace first", "WARNING")
    del logged[:]
    window.set_project(str(env))
    launch.agent.setCurrentText("codex")
    launch.form.widgets["--allow-host"].set_value(["example.org"])
    launch.start()
    assert said(logged, f"Start: the board of {env} with codex on local", "INFO", "board")
    assert said(logged, "the start goes to a terminal (codex", "INFO", "board") and \
        said(logged, "--allow-host example.org", "INFO", "board"), lines(logged)
    assert said(logged, f"codex: runs in a terminal window: {window.sai.command()[0]}", "INFO", "terminal")
    assert said(logged, f"codex --workspace {env} --allow-host example.org", "INFO", "terminal"), lines(logged)


def test_the_start_form_on_its_own_says_what_start_was_asked(qapp, env, logged, monkeypatch):
    """The Start form without a Board beside it (no window): its own start says what it was asked and why not."""
    from sandbox_ai_app.ui import LaunchPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    started = []
    launch = LaunchPage(backend.SandboxAI(), lambda argv, title, embedded=None, cwd=None: started.append(argv))
    try:
        launch.agent.setCurrentText("codex")
        assert launch.start() is False
        assert said(logged, "Start: codex on local, workspace -", "INFO", "start")
        assert said(logged, "not started: the options or the workspace are refused", "INFO", "start")
        del logged[:]
        launch.project = str(env)
        launch.form.widgets["--allow-host"].set_value(["example.org"])
        launch.start()
        assert said(logged, "Start: codex on local, workspace", "INFO", "start")
        assert said(logged, "options --allow-host example.org", "INFO", "start")
        assert started and "--allow-host" in started[-1]
    finally:
        launch.deleteLater()


# -- the Board's Start: held for a VPN profile that is missing, an identity made first --

def test_a_board_start_held_or_prepared_says_what_it_needs_and_why(board_page, user, logged, monkeypatch):
    # The agent's image is there (else the start's fix plan lists it too, "agent-image claude", and offers its build):
    # what is held or made here is the VPN profile and the identity alone.
    # (built after the page was made, which looked at it then: looked at again when the board is shown)
    user.add_image("localhost/claude-sandbox:latest")
    monkeypatch.setattr(prerequisites, "IMAGE_UNBUILT_SECONDS", 0)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page = board_page.show_board("claude", ["--vpn", "nosuchvpn"])
    assert said(logged, "agent-sandbox: no VPN profile nosuchvpn (profiles: none yet)", "INFO",
                "board")
    del logged[:]
    assert page.start() is False and not page.started
    assert said(logged, "Start: the board of", "INFO", "board")
    assert said(logged, "The start needs on local: VPN profile nosuchvpn. Missing: VPN profile nosuchvpn.", "INFO")
    assert said(logged, "warning shown: Not started: VPN profile nosuchvpn: make it on the VPN page", "WARNING",
                "BoardPage")
    assert said(logged, "not started, nothing can be made for it here", "INFO", "prerequisites")
    assert said(logged, "the start does not go on now", "INFO", "board")
    # An identity it needs is made first, said with its command and its end; then the start goes on.
    del logged[:]
    page = board_page.show_board("claude", ["--identity", "nobody"])
    page.start()
    assert said(logged, "The start needs on local: identity nobody. Missing: identity nobody.", "INFO", "board")
    assert said(logged, "start on local: making identity nobody", "INFO", "prerequisites")
    assert said(logged, "-- nobody on local: exit 0", "INFO", "board")
    assert said(logged, "Made for the start: identity nobody.", "INFO", "board"), lines(logged)


# -- the Board: Change folder, Save, Save as preset, New from preset, Build now --

@pytest.fixture
def board(qapp, env, monkeypatch, tmp_path):
    from sandbox_ai_app import board_view
    from sandbox_ai_app.board_view import BoardPage

    def at_once(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            return done(None, exc)
        return done(result, None)
    monkeypatch.setattr(board_view, "background", at_once)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    page = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    yield page
    page.deleteLater()


def test_the_board_logs_change_folder_save_as_preset_and_build_now(board, tmp_path, logged, monkeypatch):
    old, new, other = tmp_path / "old", tmp_path / "new", tmp_path / "other"
    for folder in (old, new, other):
        folder.mkdir()
    (other / ".sandbox-ai.json").write_text("{}")
    board.open_project(str(old), quiet=True)
    del logged[:]
    assert board.change_folder(str(new)) is True
    assert said(logged, f"Change folder: from {old} to {new} asked", "INFO", "board")
    assert said(logged, f"Change folder: the board is on {new} now (main workspace moved; no project file there",
                "INFO", "board")
    assert board.change_folder(str(new)) is False
    assert said(logged, f"Change folder: {new} is the board's folder already", "INFO", "board")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep this board"
    board.change_folder(str(other))
    assert said(logged, "asked: Change folder:", "INFO", "BoardPage")
    assert said(logged, "its own project file is left alone: Start waits for Apply", "INFO", "board")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    # Save as preset: what was chosen, and what it was saved as.
    preset = board.save_as_preset({"title": "Mine", "description": "", "category": "testing", "notes": [],
                                   "keep": True})
    assert preset is not None
    assert said(logged, "Save as preset: 'Mine', category testing, folders and values kept True", "INFO", "board")
    assert said(logged, f"Save as preset: saved as {preset.id}", "INFO", "board")
    del logged[:]
    assert board.save_board() == "preset"
    assert said(logged, f"Save: into your preset {preset.id} (Mine)", "INFO", "board")
    board.new_from_preset(preset, {})
    assert said(logged, f"new board from the preset {preset.id} (mine)", "INFO", "board")
    # Build now: what it builds, where, and how it ended.
    tabs, checks = [], []
    board.build_commands = [["build", "qwen"]]
    monkeypatch.setattr(board, "prerequisite_terminal",
                        lambda: lambda argv, title, ended: tabs.append(ended) or True)
    monkeypatch.setattr(board, "check", lambda: checks.append(True))
    board.build_missing()
    assert said(logged, "Build now on local: sandbox-ai build qwen (in a terminal tab)", "INFO", "board")
    tabs[0](2)
    assert said(logged, "Build now ended: exit status 2", "WARNING", "board") and checks


# -- Terminals: a tab that failed, one that closes by itself, one closed, tabs reopened --

def test_terminal_tabs_log_their_end_how_they_close_and_what_was_reopened(terminal, stub_web, sai, logged, wait,
                                                                          monkeypatch, tmp_path):
    from conftest import open_tab
    from sandbox_ai_app import state
    import test_terminal_reopen as R
    web = stub_web
    page = terminal.TerminalsPage(web, sai=sai)
    try:
        view = open_tab(page, ["sh", "-c", "exit 3"], "failing")
        assert wait(lambda: said(logged, "tab failing (failed): its command ended, exit status 3"), 20), lines(logged)
        assert said(logged, "exit status 3 - the tab stays open with its output", "WARNING", "terminal")
        page.close_tab(page.tabs.indexOf(view))
        assert said(logged, "tab failing (failed) closed", "INFO", "terminal")
        # A session's tab whose terminal ended normally: closes by itself, said so.
        monkeypatch.setattr(terminal, "AUTO_CLOSE_MS", 10)
        detached = tmp_path / "quick" / "sandbox-ai"  # (an attach whose terminal ends at once, normally)
        detached.parent.mkdir()
        detached.write_text("#!/bin/sh\nexit 0\n")
        detached.chmod(0o755)
        open_tab(page, [str(detached), "attach", "web"], "web")
        assert wait(lambda: said(logged, "the tab closes by itself in 0.01 s"), 20), lines(logged)
        assert wait(lambda: said(logged, "closed by itself (its session's terminal ended normally)"), 10)
        # Reopened at the app's start: what was kept, a host that did not answer, what was attached again.
        store = state.State(tmp_path / "state.json")
        store.data["terminal-tabs"] = [{"host": "ct1", "name": "far", "title": "ct1: far"},
                                       {"host": "local", "name": "web", "title": "web"}]

        def host_run(name, *args, timeout=120):
            if name == "ct1":
                return 255, "", "ssh: connect to host ct1: Connection refused"
            return 0, json.dumps([R.running("web")]), ""
        monkeypatch.setattr(sai, "host_run", host_run)
        page.store = store
        done = []
        page.reopen(everything=False, done=lambda opened, skipped: done.append(opened))
        assert wait(lambda: done, 20)
        assert said(logged, "reopen: 2 tab(s) kept from the last run of the app (far, web)", "INFO", "terminal")
        assert said(logged, "reopen: the starts of ct1 could not be read", "WARNING", "terminal")
        assert said(logged, "reopen: attached again web", "INFO", "terminal")
    finally:
        page.store = None
        page.close_all()
        page.deleteLater()


# -- the file manager's editor: open, save, and what failed --

def test_the_editor_logs_what_it_opened_saved_and_could_not(qapp, tmp_path, logged, wait, monkeypatch):
    from sandbox_ai_app.file_editor import EditorWindow
    (tmp_path / "notes.txt").write_text("hello\n")
    window = EditorWindow(None, files.Root(tmp_path, label="project"), "notes.txt", str(tmp_path / "notes.txt"))
    assert wait(lambda: not window.loading, 10)
    assert said(logged, "editor: opening project: notes.txt", "INFO", "files")
    assert said(logged, "editor: project: notes.txt opened (6 B, shown as text)", "INFO", "files"), lines(logged)
    window.text.setPlainText(f"token={TOKEN}\n")
    assert window.save()
    assert wait(lambda: said(logged, "editor: project: notes.txt saved"), 10), lines(logged)
    assert said(logged, "editor: saving project: notes.txt (", "INFO", "files")
    # What failed: a file gone before it was read; a save the file system refuses.
    gone = EditorWindow(None, files.Root(tmp_path, label="project"), "gone.txt", str(tmp_path / "gone.txt"))
    assert wait(lambda: not gone.loading, 10)
    assert said(logged, "editor: project: gone.txt cannot be opened:", "WARNING", "files"), lines(logged)

    def refuse(*args, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(window.root, "write_file", refuse)
    window.text.setPlainText("more\n")
    assert window.save()
    assert wait(lambda: said(logged, "not saved"), 10)
    assert said(logged, "editor: project: notes.txt not saved: OSError: [Errno 28] No space left on device",
                "WARNING", "files")
    assert TOKEN not in "\n".join(lines(logged)), "what is in a file never reaches the log"
    for each in (window, gone):
        each.dirty = False
        each.close()
        each.deleteLater()


# -- Approvals: a question that came, one answered, one that went without an answer from here --

def test_approvals_log_questions_that_came_and_went(window, env, logged, wait, monkeypatch):
    run = fake_run(env)
    ask(run, "1700000000-1-1", kind="connect", destination="192.0.2.7:5432", address="192.0.2.7", port=5432)
    ask(run, "1700000000-1-2", kind="dns", name="first.example")
    window.notify = lambda *a, **k: None
    page = window.approvals
    page.poll()
    assert said(logged, "question 1700000000-1-1 from claude-abc", "INFO", "approvals"), lines(logged)
    assert said(logged, "connect to 192.0.2.7:5432", "INFO", "approvals")
    assert said(logged, "s to answer it, else it is refused)", "INFO", "approvals")
    count = len(lines(logged, where="approvals"))
    page.poll()
    assert len(lines(logged, where="approvals")) == count, "a question is said once"
    (run / "approvals" / "1700000000-1-2.json").unlink()  # answered by the CLI, or its time ran out
    page.poll()
    assert said(logged, "question 1700000000-1-2 on this machine is no longer asked (answered elsewhere, its time "
                        "ran out", "INFO", "approvals")
    # A host whose questions cannot be read: once, then again when it answers.
    for _ in range(3):
        page.host_read("box", None, OSError("ssh: connect to host box: Connection refused"))
    assert len([line for line in lines(logged, "WARNING") if "the questions of box cannot be read" in line]) == 1
    page.host_read("box", [], None)
    assert said(logged, "the questions of box are read again", "INFO", "approvals")


# -- MCP servers and LLM servers: the outcome of what was asked --

def test_mcp_and_llm_outcomes_are_logged(window, logged, monkeypatch):
    page = window.mcp
    monkeypatch.setattr(page, "refresh", lambda: None)
    page.added("local", "official:fetch", {"name_as": "fetch", "unsigned": False}, (0, "added fetch", ""), None)
    assert said(logged, "Add: official:fetch on local added", "INFO", "mcp")
    page.added("local", "official:git", {"name_as": "git", "unsigned": True}, (1, "", "Error: no such entry\n"), None)
    assert said(logged, "Add: official:git on local not added (exit status 1)\nError: no such entry", "WARNING", "mcp")
    page.attached("fetch", None, ValueError("no kept start named web"))
    assert said(logged, "Attach to…: fetch not attached: ValueError: no kept start named web", "WARNING", "mcp")
    page.server_done("stop", (0, "", ""), None)
    assert said(logged, "mcp stop: exit status 0", "INFO", "mcp")
    llm = window.llm
    monkeypatch.setattr("sandbox_ai_app.llm_page.show_text", lambda *a, **k: None)
    llm.checked("local", "lm", (1, "", f"Authorization: Bearer {TOKEN}\nmodels: not reachable\n"), None)
    assert said(logged, "llm check lm on local: exit status 1\nAuthorization: ***", "WARNING", "llm")
    assert TOKEN not in "\n".join(lines(logged))


def test_live_mcp_attach_and_remove_outcomes_are_logged(window, logged, monkeypatch):
    page = window.sandboxes
    monkeypatch.setattr(page, "after_live_change", lambda host: None)
    page.live_mcp_done("local", "web", "Add MCP server", {"message": "attached", "agent_restart": None}, None)
    assert said(logged, "add MCP server (live) to web on local: attached, agent restart not needed", "INFO")
    page.detached("local", "Remove MCP server from web", None, ValueError("fetch is not attached"))
    assert said(logged, "Remove MCP server from web: not removed: ValueError: fetch is not attached", "WARNING")


# -- the log window: an area chosen, a bug report copied --

def test_the_log_window_shows_one_area_and_copies_a_bug_report(qapp, tmp_path, monkeypatch, logged):
    from pathlib import Path
    state_dir = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))
    (state_dir / "sandbox-ai").mkdir(parents=True)
    home = str(Path.home())
    (state_dir / "sandbox-ai" / "app.log").write_text(
        "".join(f"line {n}\n" for n in range(1000)) + f"INFO [board] Start in {home}/project with --token {TOKEN}\n")
    (state_dir / "sandbox-ai" / "launcher.log").write_text(f"launcher: GITHUB_TOKEN={TOKEN} start claude\n")
    monkeypatch.setattr(logview, "_hub", None)
    panel = logview.LogPanel()
    applog.log("INFO", "board", "Start: the board of x")
    applog.log("WARNING", "command", "exit status 1 after 0.1 s: sandbox-ai list")
    applog.log("INFO", "qt.qpa.xcb some.cpp:12", "a Qt message")
    qapp.processEvents()
    areas = [panel.area.itemData(i) for i in range(panel.area.count())]
    assert areas[0] is None and {"board", "command", "qt"} <= set(areas[1:]), areas
    panel.area.setCurrentIndex(panel.area.findData("command"))
    shown = panel.text.toPlainText()
    assert "sandbox-ai list" in shown and "Start: the board" not in shown, shown
    panel.area.setCurrentIndex(0)
    assert "Start: the board" in panel.text.toPlainText()
    text = panel.copy_report()
    assert QtWidgets.QApplication.clipboard().text() == text
    assert TOKEN not in text and "--token ***" in text and "GITHUB_TOKEN=***" in text
    assert "~/project" in text and home + "/project" not in text
    assert "--- app.log, its last 400 lines ---" in text and "line 999" in text and "line 500\n" not in text
    assert "--- launcher.log, its last 400 lines ---" in text and "start claude" in text
    assert f"app {applog.versions()['app']}" in text and "python" in text
    assert said(logged, "a bug report was copied to the clipboard", "INFO", "log")
    assert "bug report" in [b.text().lower().split("for a ")[-1] for b in panel.findChildren(QtWidgets.QPushButton)
                            if "bug report" in b.text()][0]
    panel.deleteLater()
