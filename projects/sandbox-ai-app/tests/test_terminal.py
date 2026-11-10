"""Terminals inside the app: xterm.js in QtWebEngine over the PTY bridge.

The page is tested with a small stand-in for xterm.js (tests/fake_xterm) wherever QtWebEngine runs;
the end-to-end test with the real, vendored xterm.js runs when scripts/vendor_xterm.py has been run."""
import http.server
import os
from pathlib import Path
import shutil
import threading

import pytest
from conftest import open_tab  # noqa: E402

HERE = Path(__file__).resolve().parent


@pytest.fixture
def terminal(qapp):
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    return terminal


@pytest.fixture
def stub_web(tmp_path):
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, web / "vendor" / name)
    return web


def js(view, wait, script, timeout=20):
    result = {}
    view.run_js(script, lambda value: result.setdefault("value", value))
    wait(lambda: "value" in result, timeout)
    return result.get("value")


def screen_text(view, wait, needle, timeout=30):
    text = {"value": ""}

    def poll():
        value = js(view, wait, "window.sandboxTerminal ? window.sandboxTerminal.text() : ''", 2)
        text["value"] = value or ""
        return needle in text["value"]
    wait(poll, timeout)
    return text["value"]


def test_available_needs_webengine_and_the_vendored_files(tmp_path, monkeypatch, qapp):
    from sandbox_ai_app import terminal
    ok, why = terminal.available(tmp_path)
    assert not ok and ("xterm.js" in why or "QtWebEngine" in why)
    monkeypatch.setenv("SANDBOX_AI_APP_NO_WEBENGINE", "1")
    assert terminal.available(terminal.WEB)[0] is False


def test_only_local_files_may_load(terminal, stub_web, tmp_path):
    from PySide6.QtCore import QUrl
    rule = terminal.LocalOnly(str(stub_web))
    allowed = [QUrl.fromLocalFile(str(stub_web / "terminal.html")), QUrl.fromLocalFile(str(stub_web / "vendor/xterm.js")),
               QUrl("qrc:///qtwebchannel/qwebchannel.js")]
    refused = [QUrl("https://cdn.jsdelivr.net/npm/xterm"), QUrl("http://127.0.0.1:8000/x.js"),
               QUrl.fromLocalFile(str(tmp_path / "elsewhere.js")), QUrl("file:///etc/passwd"),
               QUrl("file://" + str(stub_web) + "/../elsewhere.js"), QUrl("qrc:///other.js"), QUrl("data:text/html,x"),
               QUrl("file://otherhost" + str(stub_web / "terminal.js"))]
    (tmp_path / "link.js").symlink_to("/etc/passwd")
    (stub_web / "vendor" / "link.js").symlink_to("/etc/passwd")
    refused.append(QUrl.fromLocalFile(str(stub_web / "vendor" / "link.js")))
    assert all(rule.allowed(u) for u in allowed)
    assert not any(rule.allowed(u) for u in refused), [u.toString() for u in refused if rule.allowed(u)]


def run_terminal(terminal, web, wait, tmp_path):
    view = terminal.TerminalView(["sh", "-c", "echo hi; read x; echo got $x"], web=web)
    view.resize(700, 420)
    view.show()
    try:
        text = screen_text(view, wait, "hi")
        assert "hi" in text, text
        js(view, wait, "window.sandboxTerminal.type('abc\\r')")
        text = screen_text(view, wait, "exit status 0")
        assert "got abc" in text and "exit status 0" in text, text
        assert not view.running and view.bridge.status == 0
        return view
    finally:
        view.close_terminal()


def test_terminal_page_with_a_stand_in_for_xterm(terminal, stub_web, wait, tmp_path):
    view = terminal.TerminalView(["sh", "-c", "read x; stty size; echo got $x"], web=stub_web)
    view.show()
    try:
        assert wait(lambda: view.bridge.ready_seen, 30), "the page never connected to the bridge"
        # The stand-in's fit() makes it 100x30: the pseudo-terminal follows the page's size.
        assert wait(lambda: view.bridge.pty.fd is None or view.bridge.pty.size() == (100, 30))
        js(view, wait, "window.sandboxTerminal.type('abc\\r')")
        text = screen_text(view, wait, "got abc")
        assert "30 100" in text and "got abc" in text, text
    finally:
        view.close_terminal()
    run_terminal(terminal, stub_web, wait, tmp_path)


def test_the_terminal_takes_the_monospace_font_of_the_settings(terminal, stub_web, wait, monkeypatch):
    """A new terminal tab: xterm.js's fontFamily is the chosen font, then the theme's, then monospace; a chosen font
    that is not installed is left out (the theme's fonts)."""
    from sandbox_ai_app import fonts, theme
    try:
        for families, setting, first in ((["Hack"], "Hack", "Hack, "), ([], "Hack", '"JetBrains Mono", ')):
            monkeypatch.setattr(fonts, "_families", families)
            fonts.use({"mono_font": setting})
            view = terminal.TerminalView(["sh", "-c", "sleep 30"], web=stub_web)
            view.show()
            try:
                assert wait(lambda: view.bridge.ready_seen, 30)
                family = js(view, wait, "window.sandboxTerminal.fontFamily()")
                assert family.startswith(first) and family.endswith(", monospace"), family
            finally:
                view.close_terminal()
    finally:
        monkeypatch.setattr(fonts, "_families", None)
        fonts.use({})
        theme.apply("system")


def test_the_page_reaches_nothing_remote(terminal, stub_web, wait):
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    view = terminal.TerminalView(["sh", "-c", "sleep 30"], web=stub_web)
    view.show()
    try:
        assert wait(lambda: view.bridge.ready_seen, 30)
        js(view, wait, f"""(function () {{
            var s = document.createElement('script'); s.src = '{url}/script.js'; document.head.appendChild(s);
            var i = new Image(); i.src = '{url}/image.png';
            try {{ fetch('{url}/fetch'); }} catch (e) {{}}
            try {{ var x = new XMLHttpRequest(); x.open('GET', '{url}/xhr'); x.send(); }} catch (e) {{}}
            window.location = '{url}/navigate';
            return 1; }})()""")
        wait(lambda: False, 2)
        assert hits == []
        assert view.page.url().scheme() == "file"
    finally:
        view.close_terminal()
        server.shutdown()


def test_terminals_page_tabs_and_close(terminal, stub_web, wait, monkeypatch):
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    page = terminal.TerminalsPage(stub_web)
    view = open_tab(page, ["sh", "-c", "trap '' HUP; sleep 60"], "agent")
    assert page.tabs.count() == 1 and page.tabs.tabText(0) == "agent" and page.running() == [view]
    pid = view.bridge.pty.pid
    view.bridge.killer.setInterval(300)
    page.close_all()
    assert page.tabs.count() == 0
    import os

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    assert wait(lambda: gone() or terminal._ending == set(), 10)
    page.deleteLater()


def test_a_closed_tab_leaves_nothing_once_its_command_ended(terminal, stub_web, wait, monkeypatch):
    """Soak (stability round 3): the bridge of a closed tab whose command still ran stayed for the rest of the run,
    with its Pty, notifiers and timers (its signal's connection held it alive inside Qt): one more for every tab."""
    import gc
    from PySide6.QtCore import QCoreApplication, QEvent
    from sandbox_ai_app.pty_bridge import PtyBridge
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))

    def bridges():
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()
        return len([o for o in gc.get_objects() if isinstance(o, PtyBridge)])
    page = terminal.TerminalsPage(stub_web)
    before = bridges()
    for index in range(3):
        view = open_tab(page, ["sh", "-c", "sleep 60"], f"t{index}")
        view.bridge.kill_after = 300
        page.close_tab(0, ask=False)
        assert wait(lambda: not terminal._ending, 10), "the command ended"
    del view
    assert wait(lambda: bridges() == before, 5), "every bridge went"
    page.deleteLater()


def test_one_tab_per_attached_start(terminal, stub_web, tmp_path, monkeypatch):
    """Attaching to a start whose tab still runs shows that tab (from the list, Sandboxes & sessions or Hosts)."""
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    command = tmp_path / "sandbox-ai"  # a stand-in that stays attached
    command.write_text("#!/bin/sh\nsleep 60\n")
    command.chmod(0o755)
    page = terminal.TerminalsPage(stub_web)
    view = open_tab(page, [str(command), "attach", "web"], "web")
    open_tab(page, ["sh", "-c", "sleep 60"], "other")
    assert page.open([str(command), "session", "attach", "web"], "session web") is view, "at once: its tab"
    assert page.tabs.count() == 2 and page.tabs.currentWidget() is view
    assert open_tab(page, [str(command), "attach", "db"], "db") is not view and page.tabs.count() == 3
    page.close_all()
    page.deleteLater()


def test_end_to_end_with_the_vendored_xterm(terminal, wait, tmp_path):
    ready, why = terminal.available()
    if not ready:
        pytest.skip(f"needs QtWebEngine and xterm.js vendored by scripts/vendor_xterm.py: {why}")
    run_terminal(terminal, terminal.WEB, wait, tmp_path)


def test_which_tabs_only_detach_when_the_app_quits(terminal):
    """Quitting ends the commands of the tabs, but a session or an attached start only detaches (tmux): those are
    not asked about. Interactive agent starts are sessions too (the launcher's default); tasks, builds, doctor and
    --no-tmux starts really end."""
    d = terminal.detaches
    assert d(["/x/sandbox-ai", "attach", "web"]) and d(["/x/main.py", "session", "attach", "web"])
    assert d(["ssh", "-t", "host", "sandbox-ai attach web"])
    assert d(["/x/sandbox-ai", "claude", "--workspace", "/p", "--session", "s1"])
    assert not d(["/x/sandbox-ai", "claude", "--dry-run"]) and not d(["/x/sandbox-ai", "doctor"])
    assert not d(["/x/sandbox-ai", "build"]) and not d(["sh", "-c", "sleep 60"])
    assert not d(["/x/sandbox-ai", "codex", "--no-tmux"])
    if terminal.sys.platform != "darwin":
        assert d(["/x/sandbox-ai", "claude", "--workspace", "/p"])
        assert d(["/x/sandbox-ai", "claude", "--tmux"])
        assert not d(["/x/sandbox-ai", "claude", "--", "-p", "a task"]), "a prompt runs and exits: not in tmux"
        assert not d(["/x/sandbox-ai", "codex", "--", "exec", "fix it"])


def test_the_quit_question_names_only_what_really_ends(terminal, stub_web, monkeypatch):
    page = terminal.TerminalsPage(stub_web)

    class View:
        def __init__(self, argv):
            self.argv = argv
    views = {"session s1": View(["/x/sandbox-ai", "attach", "s1"]), "doctor": View(["/x/sandbox-ai", "doctor"])}
    monkeypatch.setattr(page, "running", lambda: list(views.values()))
    monkeypatch.setattr(page.tabs, "indexOf", lambda view: list(views.values()).index(view))
    monkeypatch.setattr(page.tabs, "tabText", lambda index: list(views)[index])
    assert page.ending() == ["doctor"] and page.detaching() == [views["session s1"]]
    page.deleteLater()


def test_quitting_releases_pages_then_views_then_profiles(terminal, stub_web, monkeypatch):
    """User report: a segmentation fault at the very end ("QObject: shared QObject was deleted directly"): Python's
    cleanup deleted web views after QtWebEngine had gone. terminal.shutdown() deletes them first, in QtWebEngine's
    order, while the application runs; a second call does nothing."""
    from shiboken6 import isValid
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    page = terminal.TerminalsPage(stub_web)
    views = [open_tab(page, ["sh", "-c", "sleep 60"], f"t{index}") for index in range(2)]
    pages = [view.page for view in views]
    web_views = [view.view for view in views]
    profiles = list(terminal._profiles.values())
    order = []
    for name, objects in (("page", pages), ("view", web_views), ("profile", profiles)):
        for item in objects:
            item.destroyed.connect(lambda *_, n=name: order.append(n))
    terminal.shutdown()
    assert not any(isValid(item) for item in pages + web_views + profiles), "deleted while the application runs"
    assert order == sorted(order, key=["page", "view", "profile"].index), order
    assert not terminal._profiles and not list(terminal._views)
    terminal.shutdown()  # again (closing, app.aboutToQuit): nothing left, no error
    page.deleteLater()


QUIT_DRIVER = r"""
import os, shutil, sys
sys.path.insert(0, sys.argv[1])
from sandbox_ai_app import main as app_main, intro, terminal, ui, widgets
web = sys.argv[2]
widgets.AUTO_ANSWER = True
intro.play = lambda window, settings: None
show = ui.MainWindow.show

def shown(window):
    from PySide6.QtCore import QTimer
    show(window)
    window.terminals.web = web
    terminal.available = lambda w=None: (True, "")
    opened = []
    for index in range(2):
        window.terminals.open([sys.executable, "-c", "import time; time.sleep(60)"], f"t{index}", done=opened.append)

    def leave():
        if len(opened) < 2:
            QTimer.singleShot(100, leave)
            return
        print(f"tabs: {sum(view is not None for view in opened)}", file=sys.stderr, flush=True)
        window.sai.save_setting("tray", False)
        QTimer.singleShot(500, window.quit_app)
    QTimer.singleShot(200, leave)
ui.MainWindow.show = shown
sys.exit(app_main.main(["sandbox-ai-app"]))
"""


def test_the_app_quits_cleanly_with_terminal_tabs_open(terminal, stub_web, tmp_path):
    """The whole app in a process of its own: two terminal tabs open, Quit; it must end with status 0, without
    "shared QObject was deleted directly" or a fatal error at the end of the interpreter."""
    import subprocess
    import sys
    script = tmp_path / "quit_driver.py"
    script.write_text(QUIT_DRIVER)
    env = {**os.environ, "SANDBOX_AI_APP_NO_TOUR": "1", "QT_QPA_PLATFORM": "offscreen",
           "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu", "QTWEBENGINE_DISABLE_SANDBOX": "1"}
    result = subprocess.run([sys.executable, str(script), str(HERE.parent), str(stub_web)], capture_output=True,
                            text=True, timeout=180, env=env, stdin=subprocess.DEVNULL)
    assert "tabs: 2" in result.stderr, result.stderr[-3000:]
    assert result.returncode == 0, result.stderr[-3000:]
    assert "shared QObject was deleted directly" not in result.stderr and "Fatal Python error" not in result.stderr
