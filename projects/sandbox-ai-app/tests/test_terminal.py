"""Terminals inside the app: xterm.js in QtWebEngine over the PTY bridge.

The page is tested with a small stand-in for xterm.js (tests/fake_xterm) wherever QtWebEngine runs;
the end-to-end test with the real, vendored xterm.js runs when scripts/vendor_xterm.py has been run."""
import http.server
from pathlib import Path
import shutil
import threading

import pytest

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
    view = page.open(["sh", "-c", "trap '' HUP; sleep 60"], "agent")
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


def test_one_tab_per_attached_start(terminal, stub_web, tmp_path, monkeypatch):
    """Attaching to a start whose tab still runs shows that tab (from the list, Sandboxes & sessions or Hosts)."""
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    command = tmp_path / "sandbox-ai"  # a stand-in that stays attached
    command.write_text("#!/bin/sh\nsleep 60\n")
    command.chmod(0o755)
    page = terminal.TerminalsPage(stub_web)
    view = page.open([str(command), "attach", "web"], "web")
    page.open(["sh", "-c", "sleep 60"], "other")
    assert page.open([str(command), "session", "attach", "web"], "session web") is view
    assert page.tabs.count() == 2 and page.tabs.currentWidget() is view
    assert page.open([str(command), "attach", "db"], "db") is not view and page.tabs.count() == 3
    page.close_all()
    page.deleteLater()


def test_end_to_end_with_the_vendored_xterm(terminal, wait, tmp_path):
    ready, why = terminal.available()
    if not ready:
        pytest.skip(f"needs QtWebEngine and xterm.js vendored by scripts/vendor_xterm.py: {why}")
    run_terminal(terminal, terminal.WEB, wait, tmp_path)
