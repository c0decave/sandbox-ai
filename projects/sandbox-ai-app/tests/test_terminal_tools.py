"""Copy, paste, links and OSC 52 in the terminal tabs (terminal_tools.py, web/terminal.js), the login tab that
finds its sign-in link, and the login browser (login_browser.py): with the stand-in for xterm.js, and with the
vendored xterm.js where scripts/vendor_xterm.py has been run."""
import base64
import json
import os
from pathlib import Path
import shutil
import stat
import sys

import pytest

HERE = Path(__file__).resolve().parent
CLAUDE_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code"
              "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&code_challenge=abc"
              "&code_challenge_method=S256&state=S7")


@pytest.fixture
def terminal(qapp):
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    return terminal


@pytest.fixture(params=["stand-in", "vendored"])
def web(request, tmp_path, terminal):
    if request.param == "vendored":
        ready, why = terminal.available()
        if not ready:
            pytest.skip(f"needs xterm.js vendored by scripts/vendor_xterm.py: {why}")
        return terminal.WEB
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, web / "vendor" / name)
    return web


@pytest.fixture
def settings(monkeypatch):
    """The terminal settings, as a test wants them (backend.setting reads the app's settings file otherwise)."""
    from sandbox_ai_app import backend, terminal, terminal_tools
    chosen = {}

    def setting(name, settings=None):
        return chosen.get(name, backend.SETTINGS[name])
    monkeypatch.setattr(terminal_tools, "setting", setting)
    monkeypatch.setattr(terminal, "backend_setting", setting)
    return chosen


def js(view, wait, script, timeout=20):
    result = {}
    view.run_js(script, lambda value: result.setdefault("value", value))
    wait(lambda: "value" in result, timeout)
    return result.get("value")


def screen(view, wait, needle, timeout=20):
    def poll():
        return needle in (js(view, wait, "window.sandboxTerminal ? window.sandboxTerminal.text() : ''", 2) or "")
    return wait(poll, timeout)


def open_view(terminal, web, wait, argv):
    view = terminal.TerminalView(argv, web=web)
    view.resize(760, 420)
    view.show()
    assert wait(lambda: view.bridge.ready_seen, 30), "the page never connected"
    assert wait(lambda: js(view, wait, "typeof window.sandboxTerminal") == "object", 20)
    return view


def clipboard():
    from PySide6.QtGui import QGuiApplication
    return QGuiApplication.clipboard()


READER = ["sh", "-c", "stty -echo; while read -r x; do printf 'got[%s]\\n' \"$x\"; done"]


def test_copy_puts_the_selection_on_qts_clipboard(terminal, web, wait, settings):
    view = open_view(terminal, web, wait, ["sh", "-c", "echo HELLOWORLD; sleep 30"])
    try:
        assert screen(view, wait, "HELLOWORLD")
        clipboard().setText("before")
        js(view, wait, "window.sandboxTerminal.select(0, 0, 5)")
        js(view, wait, "window.sandboxTerminal.copy()")
        assert wait(lambda: clipboard().text() == "HELLO"), clipboard().text()
    finally:
        view.close_terminal()


def test_a_plain_line_is_pasted_at_once(terminal, web, wait, settings):
    view = open_view(terminal, web, wait, READER)
    try:
        clipboard().setText("abc def")
        js(view, wait, "window.sandboxTerminal.paste()")
        js(view, wait, "window.sandboxTerminal.type('\\r')")
        assert screen(view, wait, "got[abc def]")
        assert view.bar.isHidden()
    finally:
        view.close_terminal()


def test_several_lines_are_asked_about_and_may_become_one(terminal, web, wait, settings):
    view = open_view(terminal, web, wait, READER)
    try:
        clipboard().setText("first\nsecond\n")
        js(view, wait, "window.sandboxTerminal.paste()")
        assert wait(lambda: not view.bar.isHidden())
        assert "2 lines" in view.bar.text() and view.bar.choices() == ["Paste", "Paste as one line", "Cancel"]
        assert view.bar.choose("Paste as one line")
        js(view, wait, "window.sandboxTerminal.type('\\r')")
        assert screen(view, wait, "got[first second]")
        settings["paste_warn"] = False  # off: several lines go as they are, no question
        clipboard().setText("third\nfourth\n")
        js(view, wait, "window.sandboxTerminal.paste()")
        assert screen(view, wait, "got[fourth]") and view.bar.isHidden()
    finally:
        view.close_terminal()


def test_control_characters_are_always_asked_about_and_stripped(terminal, web, wait, settings):
    settings["paste_warn"] = False
    view = open_view(terminal, web, wait, READER)
    try:
        clipboard().setText("a\x1b[31mb\x03c")
        js(view, wait, "window.sandboxTerminal.paste()")
        assert wait(lambda: not view.bar.isHidden())
        assert "ESC" in view.bar.text() and "Ctrl-C" in view.bar.text() and "␛" in view.bar.text()
        assert view.bar.choices() == ["Paste without them", "Cancel"]
        view.bar.choose("Paste without them")
        js(view, wait, "window.sandboxTerminal.type('\\r')")
        assert screen(view, wait, "got[a[31mbc]")
        clipboard().setText("x\x1by")
        js(view, wait, "window.sandboxTerminal.paste()")
        assert wait(lambda: not view.bar.isHidden())
        view.bar.choose("Cancel")
        js(view, wait, "window.sandboxTerminal.type('z\\r')")
        assert screen(view, wait, "got[z]")
    finally:
        view.close_terminal()


def test_bracketed_paste_needs_no_question_for_lines(terminal, web, wait, settings):
    view = open_view(terminal, web, wait, ["sh", "-c", "printf '\\033[?2004h'; stty -echo raw; "
                                                       "dd bs=1 count=24 2>/dev/null | od -c; sleep 30"])
    try:
        assert wait(lambda: js(view, wait, "window.sandboxTerminal.bracketed()") is True, 20)
        clipboard().setText("one\ntwo")
        js(view, wait, "window.sandboxTerminal.paste()")
        assert screen(view, wait, "2   0   0   ~") and view.bar.isHidden()  # ESC [ 2 0 0 ~ one \r two ESC [ 2 0 1 ~
    finally:
        view.close_terminal()


def osc52(text):
    return b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07"


def test_osc52_writes_only_after_a_yes_and_is_never_read(terminal, web, wait, settings):
    view = open_view(terminal, web, wait, ["sh", "-c", "sleep 30"])
    try:
        clipboard().setText("mine")
        view.bridge._send(b"\x1b]52;c;?\x07")  # a read: never answered, nothing asked
        view.bridge._send(osc52("copied by tmux"))
        assert wait(lambda: not view.bar.isHidden())
        assert "14 characters" in view.bar.text() and "copied by tmux" in view.bar.text()
        assert clipboard().text() == "mine"
        view.bar.choose("Copy to the clipboard")
        assert clipboard().text() == "copied by tmux"
        view.bridge._send(osc52("again"))
        assert wait(lambda: "Always for this tab" in view.bar.choices())
        assert view.bar.choose("Always for this tab"), view.bar.choices()
        assert view.helper.osc52_allowed and clipboard().text() == "again", clipboard().text()
        view.bridge._send(osc52("third"))
        assert wait(lambda: clipboard().text() == "third") and not view.bar.choices()
    finally:
        view.close_terminal()


def test_osc52_off_and_allow(terminal, web, wait, settings):
    settings["terminal_osc52"] = "off"
    view = open_view(terminal, web, wait, ["sh", "-c", "sleep 30"])
    try:
        clipboard().setText("mine")
        view.bridge._send(osc52("nope") + b"marker\r\n")
        assert screen(view, wait, "marker")
        assert clipboard().text() == "mine" and view.bar.isHidden()
        settings["terminal_osc52"] = "allow"
        view.bridge._send(osc52("yes"))
        assert wait(lambda: clipboard().text() == "yes") and not view.bar.choices()
        assert "copied 3 characters" in view.bar.text()  # (said: in the window's status bar, here in the bar)
    finally:
        view.close_terminal()


def test_a_url_tmux_drew_over_rows_of_their_own_is_one_link(terminal, web, wait, settings):
    view = open_view(terminal, web, wait, ["sh", "-c", "sleep 30"])
    try:
        cols = int(js(view, wait, "window.sandboxTerminal.size()[0]"))
        rows = [CLAUDE_URL[i:i + cols] for i in range(0, len(CLAUDE_URL), cols)]
        view.bridge._send("\r\n".join(rows).encode() + b"\r\n\r\nPaste code here > ")
        assert screen(view, wait, "Paste code here")
        js(view, wait, "window.sandboxLinks = null; window.sandboxTerminal.links(2)")
        assert wait(lambda: js(view, wait, "window.sandboxLinks") is not None)
        found = json.loads(js(view, wait, "JSON.stringify(window.sandboxLinks)"))
        assert [link[0] for link in found] == [CLAUDE_URL], found
        assert found[0][1]["start"] == {"x": 1, "y": 1} and found[0][1]["end"]["y"] == len(rows)
    finally:
        view.close_terminal()


def test_links_open_where_settings_say(terminal, web, wait, settings, monkeypatch):
    from conftest import OPENED_URLS
    from sandbox_ai_app import login_browser
    opened = []
    monkeypatch.setattr(login_browser, "open_login", lambda url, hosts, **kw: opened.append((url, hosts, kw)))
    view = open_view(terminal, web, wait, ["sh", "-c", "sleep 30"])
    try:
        before = len(OPENED_URLS)
        view.open_link(CLAUDE_URL)
        assert opened and opened[0][0] == CLAUDE_URL and "claude.ai" in opened[0][1]
        assert opened[0][2]["target"] is view
        view.open_link("https://example.org/page")
        assert OPENED_URLS[before].toString() == "https://example.org/page" and len(opened) == 1
        settings["login_links"] = "system"
        view.open_link(CLAUDE_URL)
        assert len(opened) == 1 and len(OPENED_URLS) == before + 2
        view.open_link("javascript:alert(1)")
        assert len(OPENED_URLS) == before + 2
        texts = [t for t, _e, f in view.menu_actions({"selection": True, "link": CLAUDE_URL}) if f is not None]
        assert texts[2:] == ["Copy link", "Open link", "Open in the app's browser", "Select all", "Clear selection"]
        texts = [t for t, e, f in view.menu_actions({"selection": False, "link": None}) if f is not None]
        assert [t.split("\t")[0] for t in texts] == ["Copy", "Paste", "Select all", "Clear selection"]
    finally:
        view.close_terminal()


def fake_login(tmp_path, body):
    script = tmp_path / "main.py"
    script.write_text(body)
    return [sys.executable, "-I", str(script), "login", "claude"]


def test_a_login_tab_offers_its_sign_in_link_and_takes_the_code(terminal, web, wait, settings, monkeypatch, tmp_path):
    from sandbox_ai_app import login_browser
    opened = []
    monkeypatch.setattr(login_browser, "open_login", lambda url, hosts, **kw: opened.append((url, hosts, kw)))
    argv = fake_login(tmp_path, "import sys, termios\n"
                                "a = termios.tcgetattr(0); a[3] &= ~termios.ECHO; termios.tcsetattr(0, termios.TCSANOW, a)\n"
                                f"url = {CLAUDE_URL!r}\n"
                                "sys.stdout.write('Opening browser to sign in\\nIf the browser did not open, visit: '\n"
                                "  '\\x1b]8;;' + url + '\\x07' + url + '\\x1b]8;;\\x07\\nPaste code here if prompted > ')\n"
                                "sys.stdout.flush()\n"
                                "line = sys.stdin.readline().strip()\n"
                                "print('got[' + line + ']')\n")
    assert terminal.login_of(argv) == "claude"
    view = open_view(terminal, web, wait, argv)
    try:
        assert wait(lambda: opened, 20), view.bar.text()
        assert opened[0][0] == CLAUDE_URL and opened[0][2]["target"] is view
        assert "Sign-in link" in view.bar.text()
        assert view.bar.choices() == ["Open in the app's browser", "Open in the system browser", "Copy link"]
        view.paste_line("C0DE#S7")
        assert screen(view, wait, "got[C0DE#S7]")
        assert wait(lambda: not view.running, 20)
        assert view.bar.isHidden()  # the login ended well: nothing left to say
    finally:
        view.close_terminal()


def test_which_terminals_are_logins(terminal):
    assert terminal.login_of(["ssh", "-tt", "host", "if command -v sandbox-ai; then exec sandbox-ai login codex; fi"]) \
        == "codex"
    assert terminal.login_of(["ssh", "-tt", "host", "exec sandbox-ai attach x"]) is None
    assert terminal.login_of(["sh", "-c", "sandbox-ai login claude"]) is None


# -- the login browser --

class Target:
    """A terminal tab, as the login browser sees it."""

    def __init__(self):
        self.running, self.pasted, self.revealed = True, [], 0

    def title(self):
        return "sandbox-ai login claude"

    def paste_line(self, text):
        self.pasted.append(text)

    def reveal(self):
        self.revealed += 1


PUBLIC = "151.101.1.69"


def resolver(local=()):
    """A resolver for the login browser's name checks: every name public, those of LOCAL 127.0.0.1."""
    return lambda host: ["127.0.0.1"] if host in local else [PUBLIC]


@pytest.fixture
def browser(terminal, monkeypatch):
    from sandbox_ai_app import login_browser, terminal_text as T
    monkeypatch.setattr(login_browser, "NAMES", T.NameChecks(resolve=resolver(), start=lambda work: work()))
    target = Target()
    window = login_browser.LoginBrowser(login_browser.Sign(CLAUDE_URL, T.login_hosts("claude"), target,
                                                           title="claude"))
    yield login_browser, window, target
    window.close()


def test_the_login_browser_decides_where_it_goes(browser):
    login_browser, window, _target = browser
    assert window.decide(CLAUDE_URL) and window.decide("https://claude.ai/login") and window.decide("about:blank")
    assert not window.decide("data:text/html,x") and not window.decide("file:///etc/passwd")
    assert not window.decide("http://claude.ai/login") and "https pages only" in window.bar.text()
    assert not window.decide("http://127.0.0.1:631/") and "local network" in window.bar.text()
    assert not window.decide("https://accounts.google.com/signin")
    assert window.bar.choices() == ["Open accounts.google.com here", "Open in the system browser", "Cancel"]
    loaded = []
    window.page.load = loaded.append  # (nothing remote in tests)
    window.bar.choose("Open accounts.google.com here")
    assert "accounts.google.com" in window.sign.allowed and window.decide("https://accounts.google.com/x")
    assert [u.toString() for u in loaded] == ["https://accounts.google.com/signin"]
    rule = login_browser.LocalRefused()
    from PySide6.QtCore import QUrl
    assert rule.allowed(QUrl("https://cdn.example/x.js")) and rule.allowed(QUrl("data:image/png;base64,AA=="))
    for url in ("http://cdn.example/x.js", "https://127.0.0.1/", "wss://[::1]/", "https://192.168.0.2/",
                "file:///etc/passwd", "ws://example.org/"):
        assert not rule.allowed(QUrl(url)), url


def test_the_login_browser_never_reaches_a_name_of_this_machine_or_the_lan(browser, monkeypatch):
    """Not only literals: a name that resolves to this machine, the LAN or a metadata service (localtest.me,
    127.0.0.1.nip.io, nas.lan) is refused - as a page (the bar says why) and as any request of a page."""
    login_browser, window, _target = browser
    from PySide6.QtCore import QUrl
    from sandbox_ai_app import terminal_text as T
    monkeypatch.setattr(login_browser, "NAMES", T.NameChecks(resolve=resolver({"localtest.me", "sso.example"}),
                                                             start=lambda work: work()))
    window.sign.allowed.add("sso.example")  # even one the human allowed for this sign-in
    assert not window.decide("https://sso.example/login")
    assert "sso.example" in window.bar.text() and "127.0.0.1" in window.bar.text()
    rule = login_browser.LocalRefused()
    assert not rule.allowed(QUrl("https://localtest.me/x.js")) and rule.allowed(QUrl("https://cdn.example/x.js"))


def test_a_name_not_known_yet_is_refused_until_it_is_then_the_page_goes_on(browser, monkeypatch, qapp):
    """Qt's interceptor cannot wait for a lookup: an unknown name is refused at first and looked up in the
    background; a page goes on (loaded again) once its names are known to be public - never before."""
    login_browser, window, _target = browser
    from PySide6.QtCore import QUrl
    from sandbox_ai_app import terminal_text as T
    works = []
    monkeypatch.setattr(login_browser, "NAMES", T.NameChecks(resolve=resolver({"evil.example"}), start=works.append))
    loaded = []
    window.page.load = loaded.append
    assert not window.decide("https://claude.ai/login"), "not before its name is known"
    rule = login_browser.LocalRefused()
    assert not rule.allowed(QUrl("https://cdn.example/app.js")) and not rule.allowed(QUrl("https://evil.example/"))
    for work in list(works):
        work()  # the lookups end (in a thread of their own; their answers reach the window through Qt's loop)
    deadline = __import__("time").monotonic() + 5
    while not loaded and __import__("time").monotonic() < deadline:
        qapp.processEvents()
    assert [u.toString() for u in loaded] == ["https://claude.ai/login"]
    assert window.decide("https://claude.ai/login")
    assert rule.allowed(QUrl("https://cdn.example/app.js")) and not rule.allowed(QUrl("https://evil.example/"))


def test_the_login_browser_pastes_code_hash_state_after_a_click(browser):
    _module, window, target = browser
    done = "https://platform.claude.com/oauth/code/callback?code=AbC_1&state=S7"
    assert window.decide(done)  # the provider's page may show its code too
    assert "AbC_1#S7" in window.bar.text()
    assert window.bar.choices() == ["Paste into “sandbox-ai login claude” and press Enter", "Copy"]
    assert target.pasted == []
    window.bar.choose("Paste into “sandbox-ai login claude” and press Enter")
    assert target.pasted == ["AbC_1#S7"] and target.revealed == 1


def test_the_login_browser_refuses_a_code_of_another_sign_in(browser):
    _module, window, target = browser
    window.decide("https://platform.claude.com/oauth/code/callback?code=AbC_1&state=other")
    assert "does not belong" in window.bar.text() and window.bar.choices() == [] and target.pasted == []


def test_a_return_to_localhost_is_caught_and_never_opened(terminal, monkeypatch):
    from sandbox_ai_app import login_browser, terminal_text as T
    target = Target()
    url = "https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&state=q"
    window = login_browser.LoginBrowser(login_browser.Sign(url, T.login_hosts("codex"), target, title="codex"))
    try:
        back = "http://localhost:1455/auth/callback?code=xyz&state=q"
        assert window.decide(back) is False
        assert "port 1455" in window.bar.text()
        target.running = False  # the terminal ended meanwhile: only Copy
        window.finished(T.callback_result(window.sign.flow, back), back)
        assert window.bar.choices() == ["Copy"]
        window.bar.choose("Copy")
        from PySide6.QtGui import QGuiApplication
        assert QGuiApplication.clipboard().text() == back
    finally:
        window.close()


def test_a_device_code_is_shown_and_the_window_closes_when_the_login_ends(terminal):
    from sandbox_ai_app import login_browser, terminal_text as T
    target = Target()
    window = login_browser.LoginBrowser(login_browser.Sign("https://auth.openai.com/codex/device",
                                                           T.login_hosts("codex"), target, code="ABCD-12345"))
    window.show()
    assert "ABCD-12345" in window.bar.text() and window.bar.choices() == ["Copy the code"]
    window.login_ended(1)
    assert window.isVisible() and "exit status 1" in window.bar.text()
    window.login_ended(0)
    assert not window.isVisible()


def test_the_login_profile_is_private_persistent_and_locked_down(browser):
    login_browser, window, _target = browser
    from PySide6.QtWebEngineCore import QWebEngineSettings
    profile = login_browser.profile()
    assert not profile.isOffTheRecord()
    folder = login_browser.data_dir()
    assert Path(profile.persistentStoragePath()).resolve().is_relative_to(folder.resolve())
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    attributes = window.page.settings()
    A = QWebEngineSettings.WebAttribute
    assert not attributes.testAttribute(A.JavascriptCanAccessClipboard)
    assert not attributes.testAttribute(A.PluginsEnabled)
    assert not attributes.testAttribute(A.AllowRunningInsecureContent)
    assert window.address.isReadOnly()
    with pytest.raises(ValueError):
        login_browser.forget()  # a window is open


def test_open_login_falls_back_to_the_system_browser_without_chromiums_sandbox(terminal, monkeypatch):
    from conftest import OPENED_URLS
    from sandbox_ai_app import chromium, login_browser
    said, before = [], len(OPENED_URLS)
    monkeypatch.setattr(login_browser.LoginBrowser, "load", lambda self, url: None)  # (nothing remote in tests)
    login_browser.open_login(CLAUDE_URL, ("claude.ai",), check=lambda: chromium.Probe(False, True, "ENOSPC", "x"),
                             done=lambda window, why: said.append((window, why)))
    assert said and said[0][0] is None and said[0][1]
    assert OPENED_URLS[before].toString() == CLAUDE_URL
    login_browser.open_login(CLAUDE_URL, ("claude.ai",), check=lambda: chromium.Probe(True, True, None, ""),
                             done=lambda window, why: said.append((window, why)))
    window = said[-1][0]
    try:
        assert window is not None and window.isVisible() and window.sign.flow.kind == "code"
    finally:
        window.close()
