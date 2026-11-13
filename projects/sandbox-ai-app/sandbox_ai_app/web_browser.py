"""The app's browser: Chromium (QtWebEngine) for what is done in a browser - signing in to a cloud subscription
(claude.ai, ChatGPT, OpenCode), reading a provider's page - opened from File > Browser, the header's button or the
Credentials page ("Sign in to a cloud subscription…", with the providers of sandbox-ai's PROVIDERS).

It is the login browser's profile ("sandbox-ai login", login_browser.py), so a sign-in made here is the one the next
`sandbox-ai login` finds, and Settings > Forget the login browser's data forgets both. Its rules are the login
browser's, for every page and every request of a page (login_browser.LocalRefused): https only, never this machine
or the local network nor a name that resolves there, a certificate error always refused, every permission (camera,
microphone, location, notifications, clipboard ...) denied, no plugins, the clipboard out of the pages' reach. Unlike
a sign-in window any public https host opens, the address bar can be typed into, and there are tabs (a page's new
window is a tab), Back, Forward, Reload and the pages visited in this run (History, kept in memory only).

Downloads are asked about: a file a page sends is held (accepted into a private folder of the profile, paused), the
bar names it with its size and host, and only Save to a folder… lets it go on - into the folder chosen, under its own
name (a free one), never opened or run (its execute bits are taken away). Cancel, or closing the tab, drops it.

Sign-in links of the terminal tabs still open in sign-in windows of their own (login_browser.open_login), which hand
the code back to their terminal.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QTimer, QUrl, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .widgets import safe_tooltip
from . import chromium, login_browser as L, terminal_text as T
from .i18n import N_, tr
from .terminal_tools import TerminalBar, system_browser

MAX_TABS = 12
VISITED_KEPT = 50  # pages in History (this run only)
TITLE_CHARS = 28
AGENT_NAMES = {"claude": N_("Claude Code"), "codex": N_("Codex"), "opencode": N_("OpenCode")}
# The page a provider's host signs in on; a host of PROVIDERS not here opens at its root. API hosts (api.*) are none.
SIGN_IN_PAGES = {"claude.ai": "https://claude.ai/login", "chatgpt.com": "https://chatgpt.com/auth/login",
                 "auth.openai.com": "https://auth.openai.com/log-in", "opencode.ai": "https://opencode.ai/auth",
                 "platform.claude.com": "https://platform.claude.com/login",
                 "console.anthropic.com": "https://console.anthropic.com/login"}
_open = []  # the one browser window (more tabs, not more windows)


def quick_links(launcher=None):
    """[(agent, host, url)]: the sign-in pages of each agent's provider (sandbox-ai's PROVIDERS, OpenCode's login
    with OpenAI's and Anthropic's as sandbox-ai login gives it --profile frontier); no API host."""
    links = []
    for agent, hosts in T.provider_hosts(launcher).items():
        listed = list(hosts)
        if agent in T.LOGIN_FRONTIER:
            listed += [h for other in ("claude", "codex") for h in T.provider_hosts(launcher).get(other, [])]
        for host in dict.fromkeys(h.lower() for h in listed):
            if host.startswith("api.") or not host:
                continue
            links.append((agent, host, SIGN_IN_PAGES.get(host, f"https://{host}/")))
    return links


def quick_menu(menu, opened, launcher=None):
    """MENU filled with the providers' sign-in pages, a section per agent; a choice calls OPENED(url)."""
    menu.clear()
    agent = None
    for name, host, url in quick_links(launcher):
        if name != agent:
            menu.addSection(tr(AGENT_NAMES.get(name, name)))
            agent = name
        menu.addAction(host, lambda u=url: opened(u)).setToolTip(safe_tooltip(url))
    menu.setToolTipsVisible(True)
    return menu


def address(text):
    """(url, "") the address bar's TEXT opens - https; a bare name gets https:// - or (None, why not)."""
    text = (text or "").strip()
    if not text:
        return None, tr("Type an address, such as claude.ai.")
    if re.search(r"\s", text):
        return None, tr("Not an address: {text}. The browser opens https addresses, such as claude.ai.",
                        text=T.shown_url(text, 120))
    if "://" not in text and not text.startswith(("about:", "data:", "javascript:", "file:")):
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None, tr("Not an address: {text}. The browser opens https addresses, such as claude.ai.",
                        text=T.shown_url(text, 120))
    if parts.scheme.lower() in ("http", "https", "ws", "wss") and host and T.private_address(host):
        return None, tr("Not opened: {address} is on this machine or the local network, which the browser never "
                        "reaches.", address=T.shown_url(T.without_query(text), 200))
    if parts.scheme.lower() != "https" or not host or ("." not in host and ":" not in host):
        return None, tr("Not opened: {address} (the browser opens https pages only).", address=T.shown_url(text, 200))
    return text, ""


def safe_name(name):
    """A download's file name as it is saved: no folder, no control characters, not hidden, not empty."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = "".join(c for c in name if c.isprintable() and c not in '/\\:*?"<>|')
    name = name.strip().lstrip(".").strip()
    return name[:200] or "download"


def free_path(folder, name):
    """FOLDER/NAME, or NAME (2), NAME (3) ... where one of that name is there already."""
    folder, name = Path(folder), safe_name(name)
    stem, suffix = (name.rsplit(".", 1)[0], "." + name.rsplit(".", 1)[1]) if "." in name[1:] else (name, "")
    candidate, number = folder / name, 2
    while candidate.exists() or candidate.is_symlink():
        candidate = folder / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


def size_text(count):
    if count is None or count < 0:
        return tr("size not known")
    if count < 1000:
        return tr("{count} bytes", count=count)
    if count < 1_000_000:
        return tr("{size} kB", size=f"{count / 1000:.0f}")
    return tr("{size} MB", size=f"{count / 1e6:.1f}")


def choose_folder(parent, title):
    from .state import choose_folder as choose
    return choose(parent, title, "downloads")


if not L.MISSING:
    from PySide6.QtWebEngineCore import QWebEngineDownloadRequest
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class BrowserTab(QWidget):
        """One tab: its view and page (login_browser.LoginPage: the profile's rules; it asks this tab where its main
        frame may go - decide)."""

        def __init__(self, window):
            super().__init__()
            self.window_ref = window
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            self.view = QWebEngineView(self)
            self.page = L.LoginPage(self, self.view)
            self.view.setPage(self.page)
            layout.addWidget(self.view)
            self.certificate_problem = None
            self.auto_reloads = 0
            self.page.windowCloseRequested.connect(lambda: window.close_tab(self))

        def current_url(self):
            return self.page.url().toString()

        def load(self, url):
            self.auto_reloads = 0
            self.page.load(QUrl(url))  # (decide, as for every other navigation)

        # what LoginPage asks its "window"

        def decide(self, url):
            """Whether the main frame may go to URL: https to a public host (its name looked up first); never this
            machine, the local network or anything but https."""
            if url in ("", "about:blank"):
                return True
            if L.exempt(url):
                return True
            decision = T.navigation(url, (), ())
            window = self.window_ref
            if decision == "local":
                window.bar.say(tr("Not opened: {address} is on this machine or the local network, which the browser "
                                  "never reaches.", address=T.shown_url(T.without_query(url), 200)), seconds=0,
                               level="warn")
                return False
            if decision == "refuse":
                window.bar.say(tr("Not opened: {address} (the browser opens https pages only).",
                                  address=T.shown_url(url, 200)), seconds=0, level="warn")
                return False
            return self.name_checked(url)

        def name_checked(self, url):
            host = T.host_of(url)

            def known(name, verdict, found, url=url):
                L.in_gui(lambda: self.name_answer(url, verdict, found))
            answer = L.NAMES.lookup(host, known)
            if answer is None:
                self.window_ref.bar.say(tr("Looking up {host}…", host=host), seconds=0, level="info")
                return False
            return self.name_answer(url, *answer, load=False)

        def name_answer(self, url, verdict, found, load=True):
            from shiboken6 import isValid
            if not isValid(self):
                return False
            window, host = self.window_ref, T.host_of(url)
            if verdict == "open":
                if load:
                    window.bar.dismiss()
                    self.page.load(QUrl(url))
                return True
            if verdict == "local":
                window.bar.say(tr("Not opened: {host} is {address}, on this machine or the local network, which the "
                                  "browser never reaches.", host=host, address=found or "?"), seconds=0, level="warn")
            else:
                window.bar.say(tr("Not opened: the name {host} cannot be found.", host=host), seconds=0, level="warn")
            return False

        def popup(self):
            """A page's new window: a tab of this window (none beyond MAX_TABS)."""
            tab = self.window_ref.new_tab()
            return tab.page if tab is not None else None

        def certificate_refused(self, url, description):
            self.certificate_problem = (url, description)
            self.window_ref.bar.say(tr("Not opened: the certificate of {host} is not valid ({why}).",
                                       host=T.host_of(url), why=description), seconds=0, level="error")
            self.window_ref.show_address()

    class Browser(QWidget):
        """The browser's window: its tabs, the address bar, the bar for what it says and asks."""

        def __init__(self):
            super().__init__(None, Qt.WindowType.Window)
            from .widgets import close_controls
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.setObjectName("appBrowser")
            self.setWindowTitle(tr("Browser"))
            self.visited, self.downloads = [], []
            layout = QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)
            top = QHBoxLayout()
            self.back = QToolButton()
            self.back.setText("‹")
            self.back.setToolTip(tr("Back (Alt+Left)"))
            self.back.setAccessibleName(tr("Back"))
            self.forward = QToolButton()
            self.forward.setText("›")
            self.forward.setToolTip(tr("Forward (Alt+Right)"))
            self.forward.setAccessibleName(tr("Forward"))
            self.reload = QToolButton()
            self.reload.setText("⟳")
            self.reload.setToolTip(tr("Reload (F5)"))
            self.reload.setAccessibleName(tr("Reload"))
            self.address = QLineEdit()
            self.address.setAccessibleName(tr("Address"))
            self.address.setPlaceholderText(tr("An https address, such as claude.ai"))
            self.address.setToolTip(tr("Type an address and press Enter (Ctrl+L): https only, never this machine "
                                       "or the local network"))
            self.address.returnPressed.connect(self.go)
            self.address.installEventFilter(self)  # Esc: the page's address back (never on to the window: it closes)
            self.tls = QLabel()
            self.tls.setTextFormat(Qt.TextFormat.PlainText)
            self.history_button = QToolButton()
            self.history_button.setText(tr("History"))
            self.history_button.setToolTip(tr("The pages visited in this run (not kept)"))
            self.history_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self.history_menu = QMenu(self)
            self.history_menu.aboutToShow.connect(self.fill_history)
            self.history_button.setMenu(self.history_menu)
            self.sign_in_button = QToolButton()
            self.sign_in_button.setText(tr("Sign in…"))
            self.sign_in_button.setToolTip(tr("The sign-in pages of the agents' providers"))
            self.sign_in_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self.sign_in_menu = quick_menu(QMenu(self), self.open_url)
            self.sign_in_button.setMenu(self.sign_in_menu)
            self.external = QPushButton(tr("Open in the system browser"))
            self.external.setToolTip(tr("This page in your usual browser"))
            self.external.clicked.connect(lambda _=False: self.open_externally())
            for widget in (self.back, self.forward, self.reload):
                top.addWidget(widget)
            top.addWidget(self.address, 1)
            for widget in (self.tls, self.history_button, self.sign_in_button, self.external):
                top.addWidget(widget)
            layout.addLayout(top)
            self.bar = TerminalBar(self)
            layout.addWidget(self.bar)
            self.tabs = QTabWidget()
            self.tabs.setDocumentMode(True)
            self.tabs.setTabsClosable(True)
            self.tabs.setMovable(True)
            self.tabs.tabCloseRequested.connect(lambda index: self.close_tab(self.tabs.widget(index)))
            self.tabs.currentChanged.connect(lambda _index: self.tab_changed())
            plus = QToolButton()
            plus.setText("+")
            plus.setToolTip(tr("New tab (Ctrl+T)"))
            plus.clicked.connect(lambda _=False: self.new_tab(focus=True))
            self.tabs.setCornerWidget(plus, Qt.Corner.TopRightCorner)
            layout.addWidget(self.tabs, 1)
            self.back.clicked.connect(lambda _=False: self.current() and self.current().view.back())
            self.forward.clicked.connect(lambda _=False: self.current() and self.current().view.forward())
            self.reload.clicked.connect(lambda _=False: self.reload_or_stop())
            for keys, slot in ((QKeySequence.StandardKey.AddTab, lambda: self.new_tab(focus=True)),
                               ("Ctrl+L", self.focus_address), ("F5", self.reload_or_stop),
                               (QKeySequence.StandardKey.Back, lambda: self.current() and self.current().view.back()),
                               (QKeySequence.StandardKey.Forward,
                                lambda: self.current() and self.current().view.forward()),
                               ("Ctrl+W", lambda: self.close_tab(self.current()))):
                shortcut = QShortcut(QKeySequence(keys), self)
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
                shortcut.activated.connect(slot)
            self.reload_timer = QTimer(self)
            self.reload_timer.setSingleShot(True)
            self.reload_timer.setInterval(300)
            self.reload_timer.timeout.connect(self.load_again)
            self.waiting_reload = set()
            close_controls(self)  # an [x] at its top right, Esc closes it
            self.resize(1100, 800)
            L.BROWSERS.add(self)
            L.make_gui_call()
            for _agent, host, _url in quick_links():  # the providers' names, looked up before a page asks
                L.NAMES.lookup(host)

        # -- tabs --

        def tab_list(self):
            return [self.tabs.widget(index) for index in range(self.tabs.count())]

        def current(self):
            return self.tabs.currentWidget()

        def owns(self, page):
            return any(tab.page is page for tab in self.tab_list())

        def new_tab(self, url=None, focus=False):
            """A new tab (at URL), or None beyond MAX_TABS (the bar says so)."""
            if self.tabs.count() >= MAX_TABS:
                self.bar.say(tr("Not opened: {count} tabs are open already.", count=MAX_TABS), level="warn")
                return None
            tab = BrowserTab(self)
            index = self.tabs.addTab(tab, tr("New tab"))
            page = tab.page
            page.titleChanged.connect(lambda title, t=tab: self.titled(t, title))
            page.urlChanged.connect(lambda qurl, t=tab: self.url_changed(t, qurl.toString()))
            page.loadStarted.connect(lambda t=tab: self.show_loading(t, True))
            page.loadFinished.connect(lambda ok, t=tab: self.loaded(t, ok))
            self.tabs.setCurrentIndex(index)
            if url:
                tab.load(url)
            if focus:
                self.focus_address()
            return tab

        def close_tab(self, tab):
            if tab is None:
                return
            for held in [d for d in self.downloads if d.get("tab") is tab and not d.get("folder")]:
                self.drop_download(held)
            index = self.tabs.indexOf(tab)
            if index >= 0:
                self.tabs.removeTab(index)
            tab.view.setPage(None)
            tab.page.deleteLater()
            tab.deleteLater()
            if self.tabs.count() == 0:
                self.close()

        def titled(self, tab, title):
            index = self.tabs.indexOf(tab)
            if index < 0:
                return
            shown = title or T.host_of(tab.current_url()) or tr("New tab")
            self.tabs.setTabText(index, shown if len(shown) <= TITLE_CHARS else shown[:TITLE_CHARS - 1] + "…")
            self.tabs.setTabToolTip(index, safe_tooltip(T.shown_url(title, 300)))  # (the page's own words: text)
            if tab is self.current():
                self.setWindowTitle(tr("Browser: {title}", title=T.shown_url(title, 120)) if title else tr("Browser"))
            url = tab.current_url()
            if title and self.visited and self.visited[0][1] == url:
                self.visited[0] = (title, url)

        def url_changed(self, tab, url):
            if url.startswith("https://") and (not self.visited or self.visited[0][1] != url):
                self.visited = ([(tab.page.title() or url, url)] + [v for v in self.visited if v[1] != url])[
                    :VISITED_KEPT]
            if tab is self.current():
                self.show_address()

        def loaded(self, tab, ok):
            self.show_loading(tab, False)
            if tab is self.current():
                self.show_address()

        def show_loading(self, tab, loading):
            tab.loading = loading
            if tab is self.current():
                self.reload.setText("✕" if loading else "⟳")
                self.reload.setToolTip(tr("Stop") if loading else tr("Reload (F5)"))

        def tab_changed(self):
            tab = self.current()
            if tab is None:
                return
            self.show_address(force=True)
            self.show_loading(tab, getattr(tab, "loading", False))
            self.titled(tab, tab.page.title())

        def show_address(self, force=False):
            tab = self.current()
            if tab is None:
                return
            url = tab.current_url()
            if force or not (self.address.hasFocus() and self.address.isModified()):
                self.address.setText("" if url in ("", "about:blank") else T.shown_url(url, 2000))
                self.address.setModified(False)
                self.address.setCursorPosition(0)
            problem = tab.certificate_problem
            if problem and problem[0] == url:
                self.tls.setText(tr("TLS error: refused"))
            elif url.startswith("https://"):
                self.tls.setText(tr("TLS verified"))
            else:
                self.tls.setText("")
            self.tls.setToolTip(tr("Chromium checked the server's certificate; a certificate error is always refused"))
            history = tab.view.history()
            self.back.setEnabled(history.canGoBack())
            self.forward.setEnabled(history.canGoForward())

        def eventFilter(self, watched, event):  # noqa: N802 - Qt's name
            """Esc in the address bar puts the page's address back, as in every browser: the address bar does not
            take it, and the window, which Esc closes, closed with every tab."""
            if watched is self.address and event.type() == QEvent.Type.KeyPress and \
                    event.key() == Qt.Key.Key_Escape and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                self.show_address(force=True)
                self.address.selectAll()
                return True
            return super().eventFilter(watched, event)

        def focus_address(self):
            self.address.setFocus()
            self.address.selectAll()

        def reload_or_stop(self):
            tab = self.current()
            if tab is None:
                return
            if getattr(tab, "loading", False):
                tab.view.stop()
            else:
                tab.view.reload()

        # -- where it goes --

        def go(self):
            """The address typed: opened in this tab (a new one when there is none), or why not."""
            url, why = address(self.address.text())
            if url is None:
                self.bar.say(why, seconds=0, level="warn")
                return False
            tab = self.current() or self.new_tab()
            if tab is None:
                return False
            self.address.setModified(False)
            tab.load(url)
            tab.view.setFocus()
            return True

        def open_url(self, url):
            """URL in the current tab when it shows nothing yet, else in a new one."""
            tab = self.current()
            if tab is None or tab.current_url() not in ("", "about:blank"):
                tab = self.new_tab()
            if tab is not None:
                tab.load(url)
            return tab

        def fill_history(self):
            self.history_menu.clear()
            if not self.visited:
                self.history_menu.addAction(tr("No page visited yet")).setEnabled(False)
                return
            for title, url in self.visited:
                shown = title if len(title) <= 60 else title[:59] + "…"
                self.history_menu.addAction(shown, lambda u=url: self.open_url(u)).setToolTip(safe_tooltip(T.shown_url(url, 300)))
            self.history_menu.setToolTipsVisible(True)

        def open_externally(self):
            tab = self.current()
            url = tab.current_url() if tab is not None else ""
            if url.startswith("https://"):
                system_browser(url)

        def name_resolved(self, host, parties):
            """HOST is public now: the tabs that wait for it load again (a few times at most)."""
            for tab in self.tab_list():
                shown = T.NameChecks.name(T.host_of(tab.current_url()))
                if (shown == host or shown in parties) and tab.auto_reloads < L.AUTO_RELOADS:
                    self.waiting_reload.add(tab)
                    self.reload_timer.start()

        def load_again(self):
            tabs, self.waiting_reload = self.waiting_reload, set()
            for tab in tabs:
                if tab in self.tab_list() and tab.auto_reloads < L.AUTO_RELOADS:
                    tab.auto_reloads += 1
                    tab.view.reload()

        # -- downloads: asked about, saved into a folder chosen, never opened --

        def download_requested(self, item):
            """A page of this window sends a file: held in the profile's private folder (paused) until the human
            says where it goes."""
            folder = L.private_folder(L.data_dir() / "downloads")
            name = safe_name(item.suggestedFileName() or item.downloadFileName())
            held = free_path(folder, name)
            item.setDownloadDirectory(str(folder))
            item.setDownloadFileName(held.name)
            item.accept()
            item.pause()
            tab = next((t for t in self.tab_list() if t.page is item.page()), None)
            entry = {"item": item, "name": name, "held": held, "tab": tab, "folder": None, "done": False}
            self.downloads.append(entry)
            item.stateChanged.connect(lambda state, e=entry: self.download_state(e, state))
            host = T.host_of(item.url().toString()) or "?"
            self.bar.ask(tr("{host} sends the file “{name}” ({size}). Save it?", host=host, name=name,
                            size=size_text(item.totalBytes())),
                         [(tr("Save to a folder…"), lambda e=entry: self.save_download(e)),
                          (tr("Cancel"), lambda e=entry: self.drop_download(e))],
                         cancelled=lambda e=entry: self.drop_download(e), level="info")
            return entry

        def save_download(self, entry, folder=None):
            folder = folder or choose_folder(self, tr("Save “{name}” in", name=entry["name"]))
            if not folder:
                self.drop_download(entry)
                return False
            entry["folder"] = Path(folder)
            self.bar.say(tr("Downloading “{name}”…", name=entry["name"]), seconds=0, level="info")
            item = entry["item"]
            item.resume()
            if item.state() == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                self.download_state(entry, item.state())
            return True

        def drop_download(self, entry):
            if entry.get("done"):
                return
            entry["done"] = True
            try:
                entry["item"].cancel()
            except RuntimeError:
                pass
            for path in (entry["held"], Path(str(entry["held"]) + ".download")):
                try:
                    path.unlink()
                except OSError:
                    pass
            if entry in self.downloads:
                self.downloads.remove(entry)

        def download_state(self, entry, state):
            S = QWebEngineDownloadRequest.DownloadState
            if entry.get("done"):
                return
            if state == S.DownloadCompleted and entry.get("folder"):
                entry["done"] = True
                try:
                    target = free_path(entry["folder"], entry["name"])
                    shutil.move(str(entry["held"]), str(target))
                    mode = stat.S_IMODE(os.stat(target).st_mode)
                    os.chmod(target, mode & ~0o111)  # never run (nor opened) by the app
                except OSError as exc:
                    self.bar.say(tr("Not saved: {why}", why=exc), seconds=0, level="error")
                    return
                entry["saved"] = target
                if entry in self.downloads:
                    self.downloads.remove(entry)
                self.bar.say(tr("Saved: {path}", path=str(target)), seconds=0, level="info")
            elif state in (S.DownloadCancelled, S.DownloadInterrupted):
                why = entry["item"].interruptReasonString() if state == S.DownloadInterrupted else tr("cancelled")
                if entry.get("folder"):
                    self.bar.say(tr("“{name}” was not downloaded: {why}", name=entry["name"], why=why), seconds=0,
                                 level="warn")
                self.drop_download(entry)

        # -- closing --

        def drop_pages(self):
            """Before the profile goes (login_browser.shutdown): every tab's page."""
            for tab in self.tab_list():
                tab.view.setPage(None)
                tab.page.deleteLater()

        def closeEvent(self, event):  # noqa: N802 - Qt's name
            for entry in list(self.downloads):
                if not entry.get("folder"):
                    self.drop_download(entry)
            self.drop_pages()
            L.BROWSERS.discard(self)
            super().closeEvent(event)


def window():
    """The browser's window when it is open, else None."""
    from shiboken6 import isValid
    found = [w for w in _open if isValid(w) and w.isVisible()]
    return found[0] if found else None


def open_browser(url=None, parent=None, check=None, done=None):
    """The app's browser at URL (a new tab of its window when it is open). Without QtWebEngine, or when Chromium's
    sandbox cannot start (chromium.check, in a thread; CHECK replaces it in tests), URL opens in the system browser
    and DONE(None, why) is told; else DONE(window, "")."""
    if L.MISSING:
        if url:
            system_browser(url)
        why = tr("QtWebEngine is not installed ({error})", error=L.MISSING)
        if done is not None:
            done(None, why)
        else:
            from .widgets import inform
            inform(parent, tr("Browser"), tr("The app's browser needs QtWebEngine, which is not installed "
                                             "({error}). {opened}", error=L.MISSING,
                                             opened=tr("The page opened in the system browser.") if url else ""),
                   modal=False)
        return None
    existing = window()
    if existing is not None:
        if url:
            existing.open_url(url)
        elif existing.current() is None:
            existing.new_tab(focus=True)
        existing.show()
        existing.raise_()
        existing.activateWindow()
        if done is not None:
            done(existing, "")
        return existing

    def ready(result, error):
        if error is not None or not result.ok:
            why = chromium.unavailable_text(result) if error is None else str(error)
            if url:
                system_browser(url)
            if done is not None:
                done(None, why)
            else:
                from .widgets import inform
                inform(parent, tr("Browser"), why, modal=False)
            return
        made = Browser()
        _open[:] = [made]
        made.new_tab(url, focus=not url)
        made.show()
        made.raise_()
        if done is not None:
            done(made, "")

    if check is not None:
        ready(check(), None)
        return window()
    from .widgets import background
    background(chromium.check, done=ready, owner=parent)
    return None
