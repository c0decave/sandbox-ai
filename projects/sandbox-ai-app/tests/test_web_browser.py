"""The app's browser (web_browser.py): what the address bar opens, the providers' sign-in pages (sandbox-ai's
PROVIDERS), the login browser's profile and rules for every page (https only, never this machine or the LAN nor a
name resolving there, certificates, permissions, the clipboard), tabs and history, downloads asked about and saved
into a folder chosen (end to end with QtWebEngine against a local server), the Credentials page's "Sign in to a
cloud subscription…", File > Browser and the header's button - and the sign-in windows of the terminals as before
(their downloads still cancelled)."""
import http.server
import importlib.util
import os
from pathlib import Path
import stat
import threading

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from shiboken6 import isValid  # noqa: E402

from sandbox_ai_app import login_browser as L, terminal_text as T, web_browser as W  # noqa: E402

from test_terminal_tools import resolver  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests

ROOT = Path(__file__).resolve().parents[3]


def launcher():
    spec = importlib.util.spec_from_file_location("agent_sandbox_for_browser", ROOT / "sandbox" / "agent_sandbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- no window needed --

def test_what_the_address_bar_opens():
    assert W.address("claude.ai") == ("https://claude.ai", "")
    assert W.address("  https://chatgpt.com/auth/login ") == ("https://chatgpt.com/auth/login", "")
    assert W.address("opencode.ai/auth?x=1") == ("https://opencode.ai/auth?x=1", "")
    for text, words in (("", "Type an address"), ("two words", "Not an address"),
                        ("http://claude.ai", "https pages only"), ("file:///etc/passwd", "https pages only"),
                        ("javascript:alert(1)", "https pages only"), ("data:text/html,x", "https pages only"),
                        ("localhost:8080", "local network"), ("https://127.0.0.1/", "local network"),
                        ("192.168.1.1", "local network"), ("http://[::1]:631/", "local network"),
                        ("169.254.169.254/latest", "local network"), ("intranet", "https pages only")):
        url, why = W.address(text)
        assert url is None and words in why, (text, why)


def test_a_downloads_name_and_where_it_goes(tmp_path):
    assert W.safe_name("../../.bashrc") == "bashrc"
    assert W.safe_name("C:\\Users\\x\\report.pdf") == "report.pdf"
    assert W.safe_name("a\x07b\n.txt") == "ab.txt" and W.safe_name("") == "download" and W.safe_name("...") == \
        "download"
    assert W.free_path(tmp_path, "report.pdf") == tmp_path / "report.pdf"
    (tmp_path / "report.pdf").write_text("x")
    (tmp_path / "report (2).pdf").write_text("x")
    assert W.free_path(tmp_path, "report.pdf") == tmp_path / "report (3).pdf"
    (tmp_path / "notes").write_text("x")
    assert W.free_path(tmp_path, "notes") == tmp_path / "notes (2)"
    assert [W.size_text(n) for n in (None, -1, 12, 4096, 2_500_000)] == ["size not known", "size not known",
                                                                        "12 bytes", "4 kB", "2.5 MB"]


def test_the_sign_in_pages_are_those_of_sandbox_ais_providers():
    real = launcher()
    links = W.quick_links(real)
    providers = {h for hosts in real.PROVIDERS.values() for h in hosts}
    assert links and all(host in providers and not host.startswith("api.") for _agent, host, _url in links)
    assert all(url.startswith(f"https://{host}/") for _agent, host, url in links)
    by_agent = {}
    for agent, host, url in links:
        by_agent.setdefault(agent, []).append(host)
    assert {"claude.ai", "platform.claude.com"} <= set(by_agent["claude"])
    assert {"chatgpt.com", "auth.openai.com"} <= set(by_agent["codex"])
    # OpenCode's login: its own provider and OpenAI's and Anthropic's (sandbox-ai login gives it --profile frontier)
    assert by_agent["opencode"][0] == "opencode.ai" and {"claude.ai", "chatgpt.com"} <= set(by_agent["opencode"])
    assert dict((h, u) for _a, h, u in links)["claude.ai"] == "https://claude.ai/login"
    assert W.quick_links(None) == W.quick_links(real)  # (the app's copy of the lists is the same)


def test_a_sign_in_windows_download_is_cancelled_as_before():
    class Item:
        cancelled = 0

        def page(self):
            return object()

        def cancel(self):
            self.cancelled += 1
    item = Item()
    L.download_requested(item)
    assert item.cancelled == 1


# -- the window --

@pytest.fixture
def browser(qapp, monkeypatch, tmp_path):
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    monkeypatch.setattr(L, "NAMES", T.NameChecks(resolve=resolver({"localtest.me"}), start=lambda work: work()))
    from sandbox_ai_app import chromium
    made = W.open_browser(check=lambda: chromium.Probe(True, True, None, ""))
    yield made
    for window in L.browsers():
        window.close()
    qapp.processEvents()


def test_the_browser_keeps_the_login_browsers_profile_and_rules(browser):
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEngineSettings
    assert isinstance(browser, W.Browser) and browser.isVisible() and browser.tabs.count() == 1
    tab = browser.current()
    # the login browser's one profile, persistent (never page.profile() from Python: PySide 6.12 then deletes the
    # profile with the wrapper it returns)
    assert isinstance(tab.page, L.LoginPage) and len(L._profile) == 1 and not L.profile().isOffTheRecord()
    assert Path(L.profile().persistentStoragePath()).resolve().is_relative_to(L.data_dir().resolve())
    A = QWebEngineSettings.WebAttribute
    attributes = tab.page.settings()
    assert not attributes.testAttribute(A.JavascriptCanAccessClipboard) and not attributes.testAttribute(A.JavascriptCanPaste)
    assert not attributes.testAttribute(A.PluginsEnabled) and not attributes.testAttribute(A.AllowRunningInsecureContent)
    assert not browser.address.isReadOnly()  # (a sign-in window's is)
    assert tab.decide("https://example.org/") and tab.decide("about:blank")
    assert not tab.decide("http://example.org/") and "https pages only" in browser.bar.text()
    assert not tab.decide("https://192.168.0.10/") and "local network" in browser.bar.text()
    assert not tab.decide("https://localtest.me/") and "127.0.0.1" in browser.bar.text()
    assert not tab.decide("file:///etc/passwd")
    rule = L.LocalRefused()  # every request of every page: the profile's interceptor
    assert isinstance(L.profile().interceptor, L.LocalRefused)
    assert rule.allowed(QUrl("https://cdn.example/x.js")) and not rule.allowed(QUrl("http://cdn.example/x.js"))
    assert not rule.allowed(QUrl("https://localtest.me/x.js")) and not rule.allowed(QUrl("https://10.0.0.1/"))
    with pytest.raises(ValueError):
        L.forget()  # the profile is in use
    refused = []
    tab.certificate_refused("https://self-signed.example/", "The certificate is not trusted")
    refused.append(browser.bar.text())
    assert "self-signed.example" in refused[0] and "not valid" in refused[0]


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # (Qt's older feature permissions, still asked by pages)
def test_every_permission_is_denied(browser):
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    tab = browser.current()
    assert isinstance(tab.page, L.LoginPage)  # (its permissionRequested denies every permission: login_browser.py)
    policies = []
    original = tab.page.setFeaturePermission
    tab.page.setFeaturePermission = lambda origin, feature, policy: policies.append(policy) or original(
        origin, feature, policy)
    tab.page.refuse_feature(QUrl("https://example.org"), QWebEnginePage.Feature.MediaAudioCapture)
    assert policies == [QWebEnginePage.PermissionPolicy.PermissionDeniedByUser]


def test_the_address_bar_tabs_and_history(browser, qapp):
    tab = browser.current()
    loaded = []
    tab.page.load = lambda url: loaded.append(url.toString())  # (nothing remote in tests)
    browser.address.setText("http://claude.ai")
    assert not browser.go() and "https pages only" in browser.bar.text() and loaded == []
    browser.address.setText("claude.ai")
    assert browser.go() and loaded == ["https://claude.ai"]
    # a page's new window is a tab; no more than MAX_TABS
    page = tab.popup()
    assert page is not None and browser.tabs.count() == 2 and browser.current().page is page
    while browser.tabs.count() < W.MAX_TABS:
        browser.new_tab()
    assert browser.new_tab() is None and "tabs are open already" in browser.bar.text()
    assert tab.popup() is None
    for extra in browser.tab_list()[1:]:
        browser.close_tab(extra)
    assert browser.tabs.count() == 1
    # what was visited, newest first, in this run only
    browser.url_changed(tab, "https://claude.ai/login")
    browser.url_changed(tab, "https://chatgpt.com/")
    browser.url_changed(tab, "https://claude.ai/login")
    assert [url for _title, url in browser.visited] == ["https://claude.ai/login", "https://chatgpt.com/"]
    browser.fill_history()
    from sandbox_ai_app.widgets import safe_tooltip
    assert [a.toolTip() for a in browser.history_menu.actions()] == [safe_tooltip("https://claude.ai/login"),
                                                                      safe_tooltip("https://chatgpt.com/")]
    browser.sign_in_menu.actions()  # the providers' sign-in pages, a section per agent
    texts = [a.text() for a in browser.sign_in_menu.actions()]
    assert "Claude Code" in texts and "claude.ai" in texts and "opencode.ai" in texts
    # open_browser again: a tab of the same window, not another window
    from sandbox_ai_app import chromium
    again = W.open_browser("https://opencode.ai/auth", check=lambda: chromium.Probe(True, True, None, ""))
    assert again is browser and browser.tabs.count() == 1  # (its tab shows nothing yet: the page goes there)
    assert loaded[-1] == "https://opencode.ai/auth"
    browser.close_tab(browser.current())  # the last tab: the window closes
    qapp.processEvents()
    assert not isValid(browser) or not browser.isVisible()


def test_esc_in_the_address_bar_puts_the_address_back_and_keeps_the_window(browser, qapp):
    """Esc in the address bar (to undo what was typed, as in every browser) went on to the window, which Esc closes -
    with every tab. It puts the page's address back; the window stays."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    browser.activateWindow()
    browser.address.setFocus()
    browser.address.setText("half typed")
    browser.address.setModified(True)
    QTest.keyClick(browser.address, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert isValid(browser) and browser.isVisible(), "the window stays"
    assert browser.address.text() != "half typed"


def test_a_pages_title_is_text_in_its_tabs_tooltip(browser):
    """A page chooses its title: the tab's tooltip showed it as Qt reads text that looks like markup - rich text (an
    image, a table, a fake message of the app in the tooltip of any page)."""
    from sandbox_ai_app.widgets import safe_tooltip
    tab = browser.current()
    title = "<b>Bank</b><img src='file:///etc/hosts'><table><tr><td>sandbox-ai: all safe</td></tr></table>"
    browser.titled(tab, title)
    assert browser.tabs.tabToolTip(browser.tabs.indexOf(tab)) == safe_tooltip(title)
    assert "&lt;b&gt;" in browser.tabs.tabToolTip(0) and "<b>" not in browser.tabs.tabToolTip(0)


def test_without_chromiums_sandbox_or_webengine_the_system_browser_opens(qapp, monkeypatch):
    from conftest import OPENED_URLS
    from sandbox_ai_app import chromium, widgets
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    said, before = [], len(OPENED_URLS)
    W.open_browser("https://claude.ai/login", check=lambda: chromium.Probe(False, True, "ENOSPC", "x"),
                   done=lambda window, why: said.append((window, why)))
    assert said and said[0][0] is None and said[0][1]
    assert OPENED_URLS[before].toString() == "https://claude.ai/login"
    monkeypatch.setattr(L, "MISSING", "No module named 'PySide6.QtWebEngineWidgets'")
    W.open_browser("https://chatgpt.com/", parent=None)
    assert OPENED_URLS[-1].toString() == "https://chatgpt.com/"
    assert any("needs QtWebEngine" in str(text) for _title, text, _details in widgets.SHOWN)


PAGE = b"""<!doctype html><title>start</title><script>
async function probe() {
  let clip = "none", geo = "none", pasted = "none";
  try { clip = "read:" + await navigator.clipboard.readText(); } catch (e) { clip = "refused"; }
  try { pasted = document.execCommand("paste") ? "pasted" : "refused"; } catch (e) { pasted = "refused"; }
  try {
    geo = await new Promise(done => navigator.geolocation.getCurrentPosition(
      () => done("granted"), () => done("refused"), {timeout: 3000}));
  } catch (e) { geo = "refused"; }
  document.title = [clip, pasted, geo].join(" ");
}
probe();
</script>"""


class Files:
    """A local server (an EXEMPT origin, as the login browser's end-to-end tests use) that sends a file as a
    download."""

    def __init__(self):
        outer = self
        self.hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                if self.path == "/page":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(PAGE)))
                    self.end_headers()
                    self.wfile.write(PAGE)
                    return
                body = b"the report\n" * 100
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="../report.sh"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def files(monkeypatch, browser):
    made = Files()
    monkeypatch.setattr(L, "EXEMPT", {made.origin})
    yield made
    made.close()


@pytest.mark.parametrize("answer", ["save", "cancel"])
def test_a_download_is_asked_about_and_saved_where_the_human_says_never_opened(browser, files, monkeypatch,
                                                                               tmp_path, wait, answer):
    from conftest import OPENED_URLS
    target = tmp_path / "chosen"
    target.mkdir()
    (target / "report.sh").write_text("mine")  # a file of that name stays: the download gets a free name
    monkeypatch.setattr(W, "choose_folder", lambda parent, title: str(target))
    opened = len(OPENED_URLS)
    browser.current().load(files.origin + "/report")
    assert wait(lambda: browser.downloads, 30), (browser.bar.text(), files.hits)
    entry = browser.downloads[0]
    assert "report.sh" in browser.bar.text() and "127.0.0.1" in browser.bar.text() and "1 kB" in browser.bar.text()
    assert browser.bar.choices() == ["Save to a folder…", "Cancel"]
    held = entry["held"]
    assert held.parent == L.data_dir() / "downloads" and stat.S_IMODE(os.stat(held.parent).st_mode) == 0o700
    if answer == "cancel":
        browser.bar.choose("Cancel")
        assert wait(lambda: not held.exists(), 10)
        assert browser.downloads == [] and sorted(p.name for p in target.iterdir()) == ["report.sh"]
        return
    browser.bar.choose("Save to a folder…")
    saved = target / "report (2).sh"
    assert wait(lambda: saved.exists(), 30), browser.bar.text()
    assert saved.read_bytes() == b"the report\n" * 100 and (target / "report.sh").read_text() == "mine"
    assert not os.stat(saved).st_mode & 0o111  # never run
    assert "Saved:" in browser.bar.text() and str(saved) in browser.bar.text()
    assert not held.exists() and len(OPENED_URLS) == opened  # never opened


def test_a_page_cannot_read_the_clipboard_nor_get_a_permission(browser, files, wait):
    """End to end: a page asks for the clipboard (the Clipboard API and paste) and for the location - refused, with
    something on the clipboard."""
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.clipboard().setText("secret on the clipboard")
    tab = browser.current()
    tab.load(files.origin + "/page")
    assert wait(lambda: tab.page.title() not in ("", "start") and "127.0.0.1" not in tab.page.title(), 30), \
        tab.page.title()
    assert tab.page.title() == "refused refused refused"


def test_closing_the_tab_drops_a_held_download(browser, files, wait):
    browser.new_tab()
    tab = browser.current()
    tab.load(files.origin + "/report")
    assert wait(lambda: browser.downloads, 30)
    held = browser.downloads[0]["held"]
    browser.close_tab(tab)
    assert browser.downloads == [] and wait(lambda: not held.exists(), 10)


def test_the_credentials_page_signs_in_to_a_cloud_subscription(window, monkeypatch):
    opened = []
    monkeypatch.setattr(W, "open_browser", lambda url=None, **kw: opened.append(url))
    page = window.credentials
    assert page.cloud_button.text() == "Sign in to a cloud subscription…"
    page.fill_cloud_menu()
    actions = [a for a in page.cloud_menu.actions() if a.text()]  # (a section is a separator with its title)
    texts = [a.text() for a in actions]
    assert {"Claude Code", "Codex", "OpenCode"} <= set(texts) and "claude.ai" in texts and "chatgpt.com" in texts and "OpenCode" in texts
    next(a for a in actions if a.text() == "claude.ai").trigger()
    actions[-1].trigger()  # Open the browser
    assert opened == ["https://claude.ai/login", None]


def test_file_browser_and_the_headers_button(window, monkeypatch):
    opened = []
    monkeypatch.setattr(W, "open_browser", lambda url=None, **kw: opened.append(url))
    assert window.actions["browser"].shortcut().toString() == "Ctrl+Shift+O"
    window.actions["browser"].trigger()
    window.browser_button.click()
    assert opened == [None, None]
    assert window.browser_button.accessibleName() == "Browser"
