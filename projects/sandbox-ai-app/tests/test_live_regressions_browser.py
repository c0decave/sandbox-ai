"""Regressions from live use of the sandbox's own browser (--browser, its live view vnc_view.py): each test quotes what
the user reported. The real app (MainWindow offscreen, its Sandboxes & sessions page reading this account's sessions
and runs with sandbox-ai's own modules), a session and its run as sandbox-ai records them (session.json, run.json with
the "browser" record the launcher writes - browser.record of SANDBOX_AI_HOME), and the browser's container faked by
its VNC socket: a server on a Unix socket in a folder only this user can open, speaking RFB as x11vnc does."""
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import types

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import applog, backend, start_tree, vnc_view as V, widgets  # noqa: E402

HOME = os.environ.get("SANDBOX_AI_HOME")
pytestmark = pytest.mark.skipif(not HOME and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

from test_ui import window  # noqa: E402,F401 - the page's fixtures
from conftest import read_now  # noqa: E402


# -- the user's host: a session that runs, its run with a browser, the browser's display --------------------------

def live_fakes():
    """sandbox-ai's tests/live_fakes.py: a user's host with only Podman, tmux, ssh and npm faked on PATH."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("live_fakes_for_app", Path(HOME) / "tests/live_fakes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The user's host (live_fakes.Host: their HOME, XDG folders, Podman's image store, the fakes on PATH) that the app
    and the sandbox-ai it calls run in; the runtime folder short (a Unix socket's path is at most 107 bytes)."""
    host = live_fakes().Host(tmp_path / "host") if HOME else None
    if host is not None:
        for name in ("SANDBOX_AI_NO_AUTO_BUILD", "SANDBOX_AI_MCP_NO_IMAGE_CHECK"):
            monkeypatch.delenv(name, raising=False)
        for name, value in host.env.items():
            if name.startswith(("FAKE_", "XDG_", "SANDBOX_AI_BUILD")) or name in ("HOME", "PATH"):
                monkeypatch.setenv(name, value)
    else:
        for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
            monkeypatch.setenv(name, str(tmp_path / sub))
    runtime = Path(tempfile.mkdtemp(prefix="sai-br-"))
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    yield tmp_path
    shutil.rmtree(runtime, ignore_errors=True)


@pytest.fixture
def logged():
    """The app's log entries (what goes to app.log) while the test runs."""
    found = []
    listener = applog.add_listener(found.append)
    yield found
    applog.remove_listener(listener)


@pytest.fixture(autouse=True)
def no_windows_left():
    yield
    for window_ in list(V._open.values()):
        try:
            window_.close()
        except RuntimeError:
            pass
    V._open.clear()


def browser_module():
    """sandbox-ai's browser.py (SANDBOX_AI_HOME): what the launcher records of the browser in run.json."""
    import importlib.util
    root = Path(HOME) if HOME else Path(backend.shutil.which("sandbox-ai")).resolve().parent
    spec = importlib.util.spec_from_file_location("sandbox_browser_for_app", root / "sandbox" / "browser.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Session:
    """A kept session NAME that runs (its supervisor: a process of this test), its run RUN registered as the launcher
    registers it, with a browser of CONTROL when BROWSER (--browser, OPTIONS its session's options)."""

    def __init__(self, env, name="web-shop", run="claude-5e55a0b1", browser=True, control=None, options=None):
        self.env, self.name, self.run = env, name, run
        workspace = env / "ws"
        directory = Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai" / "sessions" / name
        (directory / "tmux").mkdir(parents=True)
        (directory / "home").mkdir()
        self.supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                            str(directory / "tmux")])
        options = (["--backend", "podman", "--browser", "chromium"] + (["--browser-control", control] if control
                                                                         else [])) if options is None else options
        (directory / "session.json").write_text(json.dumps({
            "agent": "claude", "options": options, "agent_args": [], "backend": "podman", "workspace": str(workspace),
            "cwd": str(workspace), "created": "2026-11-14T10:00:00Z", "started": "2026-11-14T10:00:00Z", "name": name,
            "ready": True, "keep": True, "mode": "tmux", "pid": self.supervisor.pid}))
        self.run_dir = Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs" / run
        (self.run_dir / "approvals").mkdir(parents=True)
        self.run_dir.parent.chmod(0o700)
        self.run_dir.chmod(0o700)
        pod = f"agent-session-{name}"
        record = {"run": run, "agent": "claude", "backend": "podman", "workspace": str(workspace), "pid": os.getpid(),
                  "session": pod, "session_dir": str(env / "s"), "name": name, "start": name, "pod": pod,
                  "started": "2026-11-14T10:00:05Z", "proxy": True, "browser": None}
        if browser:  # as agent_sandbox.register_run records it (browser.record), with the run's directory
            a = types.SimpleNamespace(browser="chromium", browser_control=control, browser_profile=None,
                                      browser_container={"name": "browser"}, control_dir=self.run_dir, run_id=run)
            record["browser"] = browser_module().record(a, pod)
        (self.run_dir / "run.json").write_text(json.dumps(record))
        self.record = record

    def end_run(self):
        """Its run ends (the session stops: its supervisor and its run's directory go)."""
        self.supervisor.kill()
        self.supervisor.wait()
        shutil.rmtree(self.run_dir, ignore_errors=True)

    @property
    def vnc(self):
        return Path(self.record["browser"]["vnc"])

    def end(self):
        self.end_run()


@pytest.fixture
def sessions(env):
    made = []

    def make(**kwargs):
        made.append(Session(env, **kwargs))
        return made[-1]
    yield make
    for session in made:
        session.end()


class FakeDisplay:
    """The browser container's x11vnc on its Unix socket (RUN.browser-vnc/vnc.sock, its folder 0700): RFB 3.8,
    security None, a display of WIDTH x HEIGHT; every connection is answered in a thread of its own."""

    def __init__(self, path, width=4, height=2):
        self.path, self.width, self.height, self.connections = Path(path), width, height, 0
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        self.server.listen(4)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.answer, args=(connection,), daemon=True).start()

    def answer(self, connection):
        try:
            connection.sendall(b"RFB 003.008\n")
            connection.recv(12)
            connection.sendall(b"\x01\x01")  # one security type: None
            connection.recv(1)
            connection.sendall(struct.pack(">I", 0))
            connection.recv(1)  # ClientInit
            connection.sendall(struct.pack(">HH", self.width, self.height) + b"\0" * 16 + struct.pack(">I", 7) +
                               b"browser")
            while connection.recv(4096):
                pass
        except OSError:
            pass
        finally:
            connection.close()

    def close(self):
        self.server.close()


@pytest.fixture
def display():
    made = []

    def make(path, **kwargs):
        made.append(FakeDisplay(path, **kwargs))
        return made[-1]
    yield make
    for server in made:
        server.close()


def select(page, key):
    page.only_project.setChecked(False)
    read_now(page)
    item = page.table.find_item(start_tree.row_id("local", key))
    assert item is not None, f"{key} is not listed: {list(page.instances)}"
    page.table.select_item(item)
    return page.current()


def browser_entry(page):
    """The Browser entry of the right click on the selected row (Sandboxes & sessions)."""
    menu = widgets.menu_of(page.table)
    return next(action for action in menu.actions() if action.text() == "Browser")


def browser_tabs(window):  # noqa: F811
    terminals = window.built("terminals")
    if terminals is None:
        return []
    return [(index, terminals.tabs.tabText(index), terminals.tabs.widget(index))
            for index in range(terminals.tabs.count()) if terminals.tabs.tabText(index).startswith("Browser")]


def own_windows():
    """The browser views shown in a window of their own (on the desktop, outside the app's window)."""
    return [w for w in QtWidgets.QApplication.topLevelWidgets() if isinstance(w, V.BrowserWindow) and w.isVisible()]


# == "auf Browser geklickt, aber es passiert nichts" (2026-11-14) ======================================================

def test_the_browser_of_the_context_menu_opens_as_a_tab_in_the_app(window, env, sessions, display, logged,  # noqa: F811
                                                                    wait):
    """User: "Bei einer Session, die auch im Board einen Browser hat, auf Browser geklickt (Sitzung & Sessions → rechte
    Maustaste → Browser), aber es passiert nichts. Wo sollte der Browser dann überhaupt aufgehen? Brauchen wir nicht
    eine Kategorie wie Browser links und dann so etwas wie die Terminal-Fenster? Oder dass die Browser bei Terminals
    aufgehen?" -
    the cause: Browser opened the live view in a separate top-level window of its own (no
    parent: placed by the desktop wherever it liked - behind the app's window, on another screen -, never raised or
    activated, and a click again on one that was minimised or covered only "raised" it, which shows nothing). Now the
    browser opens where the terminals are: a tab "Browser: NAME" in the Terminals' tab row, shown at once, its display
    connected; no window of its own on the desktop."""
    session = sessions()
    server = display(session.vnc)
    page = window.sandboxes
    instance = select(page, f"session:{session.name}")
    assert V.browser_record(instance) is not None, "the session's run carries its browser (run.json)"
    entry = browser_entry(page)
    assert entry.isEnabled(), entry.toolTip()
    entry.trigger()
    assert own_windows() == [], "no window of its own, somewhere on the desktop"
    tabs = browser_tabs(window)
    assert [text for _, text, _ in tabs] == [f"Browser: {session.name}"], tabs
    index, _, panel = tabs[0]
    terminals = window.terminals
    assert window.current_page() == "terminals" and terminals.tabs.currentIndex() == index, "shown at once"
    assert not terminals.tabs.tabIcon(index).isNull(), "a browser icon"
    assert wait(lambda: "Connected" in panel.status.text(), 10), panel.status.text()
    assert server.connections == 1
    said = [e["message"] for e in logged if e["where"] == "browser"]
    assert any("tab" in line and session.name in line for line in said), said
    # Asked again (the button, the menu, the tab of its session): the same tab is shown, no second connection.
    terminals.tabs.setCurrentIndex(-1 if terminals.tabs.count() == 1 else 0)
    page.open_browser(instance)
    assert len(browser_tabs(window)) == 1 and terminals.tabs.currentWidget() is panel
    assert server.connections == 1


def test_every_reason_it_does_not_open_is_a_dialog_and_in_the_log(window, env, sessions, logged):  # noqa: F811
    """User: "Bei einer Session, die auch im Board einen Browser hat, auf Browser geklickt (Sitzung & Sessions → rechte
    Maustaste → Browser), aber es passiert nichts. Wo sollte der Browser dann überhaupt aufgehen? Brauchen wir nicht
    eine Kategorie wie Browser links und dann so etwas wie die Terminal-Fenster? Oder dass die Browser bei Terminals
    aufgehen?" -
    when the browser cannot be shown, the Browser entry was greyed out (its reason only in a
    tooltip) or nothing came: a click did nothing. Now the entry can be clicked for every start that has a browser or has it in its options, and
    the reason is a real dialog (not a notice that goes by itself) and a line in app.log: a session whose options
    have the browser but whose run was started without it (the Board's browser part added later), a run whose
    browser only the agent drives, and a display socket that is not this user's alone."""
    later = sessions(name="added-later", run="claude-0a0a0a0a", browser=False)
    agent = sessions(name="agent-only", run="claude-0b0b0b0b", control="agent")
    foreign = sessions(name="open-folder", run="claude-0c0c0c0c")
    foreign.vnc.parent.mkdir(mode=0o755)
    foreign.vnc.parent.chmod(0o755)
    page = window.sandboxes
    expected = {"added-later": "started without it", "agent-only": "Only the agent drives",
                "open-folder": "not yours alone"}
    for session in (later, agent, foreign):
        widgets.SHOWN.clear()
        select(page, f"session:{session.name}")
        entry = browser_entry(page)
        assert entry.isEnabled(), f"{session.name}: a click must answer ({entry.toolTip()})"
        entry.trigger()
        assert widgets.SHOWN, f"{session.name}: a dialog says why"
        title, text, _ = widgets.SHOWN[-1]
        assert title == "Browser" and expected[session.name] in text, (session.name, text)
        assert browser_tabs(window) == [] and own_windows() == []
        said = [e for e in logged if e["where"] == "browser" and session.name in e["message"]]
        assert said and expected[session.name] in said[-1]["message"], said


def terminal_tab(window, env, name):  # noqa: F811
    """A terminal tab attached to the start NAME (`sandbox-ai attach NAME`; a stand-in that waits), as Attach in the
    app opens it."""
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    script = env / "bin" / "sandbox-ai"
    if not script.exists():
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/sh\nexec sleep 60\n")
        script.chmod(0o755)
    return window.terminals.make_tab([str(script), "attach", name], name)


def test_the_browser_tab_sits_beside_its_terminal_and_moves_to_a_window_and_back(window, env, sessions,  # noqa: F811
                                                                                 display, wait):
    """User: "... Oder dass die Browser bei Terminals aufgehen?" - the browser's tab comes right after its session's
    terminal tab (not at the end of the row); its right click opens it in its own window (raised on the app's screen,
    the same connection) and its Back into the tabs puts it back beside the terminal; Focus in Sandboxes & sessions
    selects its start; Close tab ends its connection."""
    session = sessions()
    other = sessions(name="other", run="claude-0d0d0d0d", browser=False, options=[])
    server = display(session.vnc)
    page = window.sandboxes
    read_now(page)
    terminal_tab(window, env, session.name)
    terminal_tab(window, env, other.name)
    terminals = window.terminals
    page.open_browser(page.instances[f"session:{session.name}"])
    tabs = terminals.tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == [session.name, f"Browser: {session.name}", other.name]
    panel = tabs.widget(1)
    assert wait(lambda: "Connected" in panel.status.text(), 10), panel.status.text()
    menu = [entry[0] if entry else None for entry in terminals.tab_actions(1)]
    assert menu == ["Open in its own window", "Focus in Sandboxes & sessions", None, "Close tab", "Close other tabs"]
    assert terminals.tab_actions(0)[0][0] == "Rename…", "a terminal tab keeps its own menu"
    actions = {entry[0]: entry for entry in terminals.tab_actions(1) if entry}
    actions["Open in its own window"][3]()
    assert [tabs.tabText(i) for i in range(tabs.count())] == [session.name, other.name]
    frame = panel.window()
    assert frame is not window and frame.isVisible() and frame.windowTitle() == f"Browser: {session.name}"
    area = (window.screen() or QtWidgets.QApplication.primaryScreen()).availableGeometry()
    assert area.intersects(frame.frameGeometry()), "on the app's screen"
    assert not panel.back.isHidden() and "Esc closes this window" in panel.note.text()
    assert server.connections == 1 and "Connected" in panel.status.text(), "the same connection"
    page.open_browser(page.instances[f"session:{session.name}"])  # asked again: its window, no tab
    assert tabs.count() == 2 and server.connections == 1
    panel.back.click()
    assert [tabs.tabText(i) for i in range(tabs.count())] == [session.name, f"Browser: {session.name}", other.name]
    assert tabs.widget(1) is panel and panel.back.isHidden() and not frame.isVisible()
    assert "Esc closes" not in panel.note.text(), "in a tab every key is the browser's"
    actions = {entry[0]: entry for entry in terminals.tab_actions(1) if entry}
    actions["Focus in Sandboxes & sessions"][3]()
    assert window.current_page() == "sandboxes" and page.current().key == f"session:{session.name}"
    actions["Close tab"][3]()
    assert [tabs.tabText(i) for i in range(tabs.count())] == [session.name, other.name]
    assert panel.down and not panel.client.socket.isOpen()


def test_the_browser_tab_closes_when_its_session_ends(window, env, sessions, display, logged, wait,  # noqa: F811
                                                     monkeypatch):
    """User: "... so etwas wie die Terminal-Fenster?" - as a terminal tab of a session that ended: the browser's tab
    says so and closes AUTO_CLOSE_MS later; one opened in its own window too."""
    from sandbox_ai_app import browser_tabs
    monkeypatch.setattr(browser_tabs, "AUTO_CLOSE_MS", 200)
    first = sessions()
    second = sessions(name="docs", run="claude-0e0e0e0e")
    display(first.vnc)
    display(second.vnc)
    page = window.sandboxes
    read_now(page)
    page.open_browser(page.instances[f"session:{first.name}"])
    page.open_browser(page.instances[f"session:{second.name}"])
    terminals = window.terminals
    browsers = terminals.browsers
    browsers.to_window(browsers.find(f"session:{second.name}"))
    assert len(browsers.panels()) == 2 and len(browsers.frames) == 1
    first.end_run()
    second.end_run()
    read_now(page)
    titles = [terminals.tabs.tabText(i) for i in range(terminals.tabs.count())]
    assert titles == [f"Browser: {first.name} (ended)"], titles
    assert browsers.frames[0].windowTitle() == f"Browser: {second.name} (ended)"
    assert wait(lambda: not browsers.panels(), 5), "closed by itself, the window too"
    assert terminals.tabs.count() == 0
    said = [e["message"] for e in logged if e["where"] == "browser"]
    assert any("closed by itself" in line for line in said), said


def test_browser_tabs_come_back_at_the_next_start_only_while_their_session_runs(window, env, sessions,  # noqa: F811
                                                                                 display, logged):
    """User: "... so etwas wie die Terminal-Fenster?" - like the terminal tabs, the browser tabs open when the app ended
    come back at its next start: only those whose session still runs with a browser; the others are left out, and
    app.log says which and why."""
    from sandbox_ai_app import browser_tabs
    running = sessions()
    display(running.vnc)
    read_now(window.sandboxes)  # (the read the window began before)
    terminals = window.terminals
    store = terminals.store
    assert store is not None, "the Terminals page keeps its tabs in the app's state"
    store.set(browser_tabs.SAVED, [{"key": f"session:{running.name}", "name": running.name},
                                   {"key": "session:gone", "name": "gone"}])
    terminals.browsers.restored = False  # (the app's next start)
    shown = window.current_page()
    read_now(window.sandboxes)
    titles = [terminals.tabs.tabText(i) for i in range(terminals.tabs.count())]
    assert titles == [f"Browser: {running.name}"], titles
    assert window.current_page() == shown, "reopened in the background, not shown over the page chosen"
    said = [e["message"] for e in logged if e["where"] == "browser"]
    assert any("reopened web-shop" in line and "not reopened: gone (It does not run.)" in line for line in said), said
    assert store.get(browser_tabs.SAVED) == [{"key": f"session:{running.name}", "name": running.name}]
    # Renamed (sandbox-ai rename: its id, so its key, stays): the tab follows the name.
    renamed = backend.Instance("session", f"session:{running.name}", "shop", session="shop", state="running",
                               record=running.record)
    terminals.browsers.sync({renamed.key: renamed})
    assert [terminals.tabs.tabText(i) for i in range(terminals.tabs.count())] == ["Browser: shop"]
    assert store.get(browser_tabs.SAVED) == [{"key": f"session:{running.name}", "name": "shop"}]


def at_once(function, *args, done, **kwargs):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        return done(None, exc)
    return done(result, None)


def test_the_boards_browser_part_to_a_session_whose_browser_opens_in_its_tab(window, env, display, wait,  # noqa: F811
                                                                            monkeypatch, sessions):
    """User: "Bei einer Session, die auch im Board einen Browser hat, auf Browser geklickt (Sitzung & Sessions → rechte
    Maustaste → Browser), aber es passiert nichts." - end to end: the Board's Browser part, the launcher's check of the
    board (the real sandbox-ai's dry run, Podman faked) accepts it and plans the browser's display socket, the start
    as a session goes to a terminal with --browser; the session it starts (registered as the launcher registers it,
    its browser's container faked by its display) is listed with its browser, and Browser in its right click opens the
    tab with the live view."""
    from sandbox_ai_app import board_view, prerequisites
    from sandbox_ai_app.board_view import BoardPage
    from sandbox_ai_app import terminal
    if not HOME:
        pytest.skip("needs SANDBOX_AI_HOME (its tests/live_fakes.py)")
    monkeypatch.setattr(board_view, "background", at_once)
    monkeypatch.setattr(prerequisites, "background", at_once)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    live_fakes().Host(env / "host").add_image("localhost/claude-sandbox:latest", "localhost/sandbox-ai-browser:latest")
    workspace = env / "ws"
    started = []
    board_page = BoardPage(backend.SandboxAI(), lambda argv, title, **kw: started.append(argv) or True,
                           lambda: {"board_grid": 20})
    board_page.prerequisite_terminal = lambda: (lambda argv, title, ended: False)
    try:
        from sandbox_ai_app import board as B
        board = B.from_argv(board_page.sai, "claude", ["--backend", "podman"], [], workspaces=[str(workspace)])
        board_page.set_board(board, workspace)
        board_page.add_node("browser")
        board_page.check()
        assert board_page.verdict.text() == "The launcher accepts these options.", board_page.verdict.text()
        options = B.launcher_argv(board_page.sai, board_page.board)
        plan, message = board_page.sai.dry_run("claude", options)
        assert plan is not None, message
        assert plan["browser"]["control"] == "human" and plan["browser"]["display"]["vnc_socket"].endswith(
            ".browser-vnc/vnc.sock"), plan["browser"]
        assert board_page.start(session="web-shop") is not False, widgets.SHOWN
        argv = started[-1]
        assert argv[argv.index("--browser") + 1] == "chromium" and argv[argv.index("--session") + 1] == "web-shop"
    finally:
        board_page.deleteLater()
    # The session runs: registered as the launcher registers it (run.json "browser": browser.record), the browser's
    # container serving its display on the socket the record names.
    session = sessions(name="web-shop", options=argv[argv.index("claude") + 1:argv.index("--session")])
    server = display(session.vnc)
    page = window.sandboxes
    select(page, "session:web-shop")
    entry = browser_entry(page)
    assert entry.isEnabled()
    entry.trigger()
    tabs = browser_tabs(window)
    assert [text for _, text, _ in tabs] == ["Browser: web-shop"] and own_windows() == []
    panel = tabs[0][2]
    assert wait(lambda: "Connected" in panel.status.text(), 10), panel.status.text()
    assert server.connections == 1 and panel.control == "human"
