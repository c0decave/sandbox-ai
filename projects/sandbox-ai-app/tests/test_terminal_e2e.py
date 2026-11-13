"""End to end, offscreen: a real terminal tab (the stand-in for xterm.js and, where vendored, the real xterm.js in
QtWebEngine) and the real login browser against a local fake provider - a sign-in that ends at the provider's code
page (Claude Code's flow) and one that returns to localhost (caught, never opened); the keys, the browser's own
paste event and OSC 8 links as regressions of what did not work before."""
import http.server
import json
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from test_terminal_tools import READER, clipboard, js, open_view, screen, settings, terminal, web  # noqa: F401

CODE_CALLBACK = "https://platform.claude.com/oauth/code/callback"


class Provider:
    """A fake OAuth provider on 127.0.0.1: /authorize sends the browser on to its redirect_uri with a code (a 302,
    or a page whose script does it - /authorize-js) - as claude.ai does after a sign-in."""

    def __init__(self):
        self.hits = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                query = parse_qs(urlsplit(self.path).query)
                target = f"{query['redirect_uri'][0]}?code=XYZ_42&state={query['state'][0]}"
                if self.path.startswith("/authorize-js"):
                    body = f"<!doctype html><script>location.href = {json.dumps(target)};</script>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, redirect, state, path="/authorize"):
        from urllib.parse import quote
        return f"{self.origin}{path}?response_type=code&redirect_uri={quote(redirect, safe='')}&state={state}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider(monkeypatch, terminal):
    from sandbox_ai_app import login_browser, terminal_text as T
    made = Provider()
    monkeypatch.setattr(login_browser, "EXEMPT", {made.origin})  # the fake provider alone
    monkeypatch.setattr(login_browser, "SHOW_CODE_PAGE", False)  # (nothing remote: platform.claude.com not loaded)
    monkeypatch.setitem(T.PROVIDERS, "claude", T.PROVIDERS["claude"] + ["127.0.0.1"])
    yield made
    made.close()
    for window in login_browser.windows():
        window.close()


def choose_starting(bar, prefix):
    for label in bar.choices():
        if label.startswith(prefix):
            return bar.choose(label)
    return False


def test_a_login_tab_signs_in_through_the_login_browser_and_gets_the_code(terminal, web, wait, settings, provider,
                                                                          tmp_path):
    """sandbox-ai login claude, as the Credentials page starts it (a stand-in main.py prints what Claude Code 2.1.322
    prints): the tab finds the OSC 8 link, the login browser opens it, the provider sends it to the code page, the
    code and state go into the terminal with Enter after one click, the login ends well and the window closes."""
    from sandbox_ai_app import login_browser
    url = provider.url(CODE_CALLBACK, "ST8")
    script = tmp_path / "main.py"
    script.write_text("import sys, termios\n"
                      "a = termios.tcgetattr(0); a[3] &= ~termios.ECHO; termios.tcsetattr(0, termios.TCSANOW, a)\n"
                      f"url = {url!r}\n"
                      "sys.stdout.write('If the browser did not open, visit: \\x1b]8;;' + url + '\\x07' + url\n"
                      "                 + '\\x1b]8;;\\x07\\nPaste code here if prompted > ')\n"
                      "sys.stdout.flush()\n"
                      "print('got[' + sys.stdin.readline().strip() + ']')\n")
    view = open_view(terminal, web, wait, [sys.executable, "-I", str(script), "login", "claude"])
    try:
        assert wait(lambda: login_browser.window_for(view) is not None, 30), view.bar.text()
        window = login_browser.window_for(view)
        assert wait(lambda: "XYZ_42#ST8" in window.bar.text(), 30), (window.bar.text(), provider.hits)
        assert provider.hits and provider.hits[0].startswith("/authorize")
        assert window.address.isReadOnly()
        assert choose_starting(window.bar, "Paste into")
        assert screen(view, wait, "got[XYZ_42#ST8]")
        assert wait(lambda: not view.running, 20) and view.bridge.status == 0
        assert wait(lambda: not login_browser.windows(), 10)
    finally:
        view.close_terminal()


def test_a_sign_in_back_to_localhost_is_caught_and_never_reaches_this_machine(terminal, web, wait, settings,
                                                                              provider):
    """Codex's browser login returns to http://localhost:1455 - inside the sandbox. The login browser never opens
    that address here (a listener of this machine on that port gets nothing); it offers to paste it."""
    from sandbox_ai_app import login_browser, terminal_text as T
    listener = Provider()  # stands for whatever listens on this machine at that port
    try:
        back = f"http://localhost:{listener.port}/auth/callback"
        view = open_view(terminal, web, wait, READER)
        try:
            opened = []
            login_browser.open_login(provider.url(back, "q", "/authorize-js"), T.login_hosts("codex"), target=view,
                                     done=lambda window, why: opened.append((window, why)))
            assert wait(lambda: opened, 30) and opened[0][0] is not None, opened
            window = opened[0][0]
            assert wait(lambda: "port " + str(listener.port) in window.bar.text(), 30), (window.bar.text(),
                                                                                          provider.hits)
            assert listener.hits == []
            assert choose_starting(window.bar, "Paste into")
            assert screen(view, wait, f"got[{back}?code=XYZ_42&state=q]")
            assert listener.hits == []
        finally:
            view.close_terminal()
    finally:
        listener.close()


def test_copying_a_url_tmux_drew_over_rows_gives_the_whole_url(terminal, web, wait, settings):
    from sandbox_ai_app import terminal as T
    if web != T.WEB:
        pytest.skip("the stand-in has no rows to select across")
    from test_terminal_tools import CLAUDE_URL
    view = open_view(terminal, web, wait, ["sh", "-c", "sleep 30"])
    try:
        cols = int(js(view, wait, "window.sandboxTerminal.size()[0]"))
        rows = [CLAUDE_URL[i:i + cols] for i in range(0, len(CLAUDE_URL), cols)]
        assert len(rows) >= 3
        view.bridge._send("\r\n".join(rows).encode() + b"\r\nnext line\r\n")
        assert screen(view, wait, "next line")
        js(view, wait, f"window.sandboxTerminal.select(0, 0, {len(CLAUDE_URL)})")
        js(view, wait, "window.sandboxTerminal.copy()")
        assert wait(lambda: clipboard().text() == CLAUDE_URL), clipboard().text()
        # (the note "Copied ..." shows above the terminal here - the app has its status bar: xterm.js fits itself to
        # the smaller room, and a resize ends a selection)
        assert wait(lambda: not view.bar.isHidden())
        wait(lambda: False, 0.5)
        # a row that is not full keeps its line break
        js(view, wait, f"window.sandboxTerminal.select(0, {len(rows) - 1}, {cols + 4})")
        js(view, wait, "window.sandboxTerminal.copy()")
        assert wait(lambda: clipboard().text() == rows[-1] + "\nnext"), repr(clipboard().text())
    finally:
        view.close_terminal()


def key(view, wait, name, **mods):
    """A key pressed in xterm.js's text area (as Chromium delivers it)."""
    options = json.dumps({"key": name, "bubbles": True, "cancelable": True, **mods})
    js(view, wait, f"document.querySelector('.xterm-helper-textarea').dispatchEvent(new KeyboardEvent('keydown', "
                   f"{options})); true")


def test_the_copy_and_paste_keys_and_the_browsers_own_paste(terminal, web, wait, settings):
    """Regression: Ctrl+Shift+C and Ctrl+Shift+V did nothing (the page had no clipboard), and the browser's own paste
    event went to xterm.js unchecked. Now both go through the app, which reads Qt's clipboard - never what the page
    hands it."""
    from sandbox_ai_app import terminal as T
    if web != T.WEB:
        pytest.skip("the stand-in has no text area")
    view = open_view(terminal, web, wait, READER)
    try:
        clipboard().setText("from-keys")
        key(view, wait, "V", ctrlKey=True, shiftKey=True)
        js(view, wait, "window.sandboxTerminal.type('\\r')")
        assert screen(view, wait, "got[from-keys]")
        clipboard().setText("qt-clipboard")
        js(view, wait, "const d = new DataTransfer(); d.setData('text/plain', 'page-data');"
                       "document.querySelector('.xterm-helper-textarea').dispatchEvent(new ClipboardEvent('paste', "
                       "{clipboardData: d, bubbles: true, cancelable: true})); true")
        js(view, wait, "window.sandboxTerminal.type('\\r')")
        assert screen(view, wait, "got[qt-clipboard]")
        assert "page-data" not in js(view, wait, "window.sandboxTerminal.text()")
        js(view, wait, "window.sandboxTerminal.select(0, 0, 3)")
        key(view, wait, "C", ctrlKey=True, shiftKey=True)
        assert wait(lambda: clipboard().text() == "got"), clipboard().text()
        key(view, wait, "Insert", shiftKey=True)  # Shift+Insert pastes too
        js(view, wait, "window.sandboxTerminal.type('\\r')")
        assert screen(view, wait, "got[got]")
    finally:
        view.close_terminal()


def test_an_osc8_link_opens_through_the_app_with_ctrl_only(terminal, web, wait, settings):
    """Regression: a click on Claude Code's login link (an OSC 8 hyperlink) went to xterm.js's own handler - a
    confirm() and window.open, which the app's page refuses: nothing opened. Now Ctrl+click (Cmd+click) opens it
    through the app; a plain click only shows its target."""
    from conftest import OPENED_URLS
    settings["login_links"] = "system"
    view = open_view(terminal, web, wait, ["sh", "-c", "sleep 30"])
    try:
        before = len(OPENED_URLS)
        js(view, wait, "window.sandboxTerminal.activateLink('https://claude.ai/oauth/x', false); true")
        assert js(view, wait, "document.getElementById('hover').hidden") is False
        assert "claude.ai/oauth/x" in js(view, wait, "document.getElementById('hover').textContent")
        assert len(OPENED_URLS) == before
        js(view, wait, "window.sandboxTerminal.activateLink('https://claude.ai/oauth/x', true); true")
        assert wait(lambda: len(OPENED_URLS) == before + 1)
        assert OPENED_URLS[-1].toString() == "https://claude.ai/oauth/x"
        js(view, wait, "window.sandboxTerminal.activateLink('javascript:alert(1)', true); true")
        assert len(OPENED_URLS) == before + 1
        assert json.loads(js(view, wait, "window.sandboxTerminal.menuState()"))["link"] == "https://claude.ai/oauth/x"
    finally:
        view.close_terminal()
