"""The app's login browser: a Chromium window (QtWebEngine) for an agent's sign-in, which hands the result back to
the terminal tab that asked for it.

Opened for a sign-in link a login prints (`sandbox-ai login AGENT` in a terminal tab: the link is found in its
output, terminal_text.LoginWatch) or a link Ctrl+clicked in any terminal tab, when Settings say sign-in links open
in the app (login_links: app, the default; system: the system browser).

- One profile, "sandbox-ai login", kept in ~/.local/share/sandbox-ai/app/login-browser (mode 0700): a sign-in at a
  provider is remembered for the next login, as in a browser - also one made in the app's browser (web_browser.py),
  which shares this profile and its rules. Settings > Window forgets it. No extensions, no plugins, downloads
  cancelled (the app's browser asks, then saves into a folder chosen), every permission (camera, notifications, location ...) refused, a certificate error
  refused (never accepted), the clipboard out of the page's reach.
- The address bar shows the real address, read-only, and whether TLS was verified.
- Pages: https only. The hosts of the agent's provider (sandbox-ai's own lists, terminal_text.login_hosts) open at
  once; another host (a sign-in with Google, Apple, a company's SSO) is asked about, for this sign-in, or opened in the
  system browser. Nothing of this machine or the local network is reached (localhost, 127.0.0.0/8, private,
  link-local and shared addresses, a cloud's metadata service: refused for every request of every page) - nor a name
  that resolves to one (localtest.me, nas.lan: terminal_text.NameChecks). Qt's interceptor cannot wait, so a name is
  looked up in the background and its requests refused until it is known to be public; a page goes on then (it is
  loaded again, a few times at most). Popups only for the same sign-in, under the same rules.
- The end of the sign-in: for Claude Code (and OpenCode's Anthropic login) the provider's page with the code
  (redirect_uri .../oauth/code/callback?code=...&state=...): "CODE#STATE" is shown and, after a click, pasted into the
  terminal with Enter - its state must be the one the sign-in began with. For a sign-in that returns to a program on
  localhost (inside the sandbox, which this browser cannot reach) that address is caught, never opened here: it can
  be pasted into the terminal or copied. A device code sign-in (Codex's --device-auth, which sandbox-ai login uses)
  shows its one-time code to copy; the terminal finishes by itself, and the window closes when the login ends well.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import threading
import weakref

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import chromium, terminal_text as T
from .i18n import tr
from .terminal_tools import TerminalBar, preview, put, system_browser

STORAGE_NAME = "sandbox-ai-login"
POPUPS = 3  # popups one sign-in may open at once
_windows = weakref.WeakSet()
BROWSERS = weakref.WeakSet()  # the windows of the app's browser (web_browser.py): the same profile and rules
_profile = []  # the one profile, once made
# Origins ("http://127.0.0.1:PORT") the local and https rules do not hold for: the end-to-end tests' fake provider
# alone. Nothing in the app adds one - no setting, no page, no command line.
EXEMPT = set()
SHOW_CODE_PAGE = True  # the provider's code page loads after its code was taken (tests: nothing remote)
NAMES = T.NameChecks()  # which names resolve to this machine or the local network (looked up in the background)
AUTO_RELOADS = 3  # times a page is loaded again because a name it asked for became known as public
_pending = {}  # name -> the hosts of the pages that asked for it before it was known (their windows load again)
_pending_lock = threading.Lock()


class _GuiCall(QObject):
    """Hands a callable from a lookup's thread to the GUI thread (a queued signal)."""
    call = Signal(object)


_gui = []


def gui_call():
    """The one _GuiCall, made in the GUI thread (by the first window); None before."""
    return _gui[0] if _gui else None


def make_gui_call():
    if not _gui:
        made = _GuiCall()
        made.call.connect(lambda function: function())
        _gui.append(made)
    return _gui[0]


def in_gui(function):
    """FUNCTION, called in the GUI thread (from a lookup's thread: queued)."""
    target = gui_call()
    if target is None:
        return
    target.call.emit(function)


def name_known(host, verdict, address):
    """A name looked up for a request (LocalRefused): the windows whose pages asked for it load again when it is
    public (the GUI thread)."""
    with _pending_lock:
        parties = _pending.pop(host, set())
    if verdict != "open":
        return
    for window in windows() + browsers():
        if _alive(window):
            window.name_resolved(host, parties)


def browsers():
    """The open windows of the app's browser (web_browser.py)."""
    from shiboken6 import isValid
    return [w for w in list(BROWSERS) if isValid(w)]


def download_requested(item):
    """A page asks to download a file: a page of the app's browser hands it to its window (which asks, and saves it
    into a folder chosen); a sign-in's page gets none."""
    try:
        page = item.page()
    except (AttributeError, RuntimeError):
        page = None
    for window in browsers():
        if page is not None and window.owns(page):
            window.download_requested(item)
            return
    item.cancel()


def exempt(url):
    try:
        parsed = QUrl(url)
        return f"{parsed.scheme()}://{parsed.host()}:{parsed.port()}" in EXEMPT
    except (TypeError, ValueError):
        return False

try:
    from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                         QWebEngineUrlRequestInterceptor)
    from PySide6.QtWebEngineWidgets import QWebEngineView
    MISSING = None
except ImportError as exc:  # pragma: no cover - without QtWebEngine the system browser opens sign-ins
    MISSING = str(exc)


def data_dir(environ=None):
    """The login browser's folder (its cookies, its storage, its cache)."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "sandbox-ai" / "app" / \
        "login-browser"


def private_folder(path):
    """PATH made (and kept) a folder of mode 0700 - not through a symbolic link."""
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"{path} is a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


if not MISSING:
    class LocalRefused(QWebEngineUrlRequestInterceptor):
        """Every request of every login page: none to this machine or a private network, nothing unencrypted."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.refused = []  # (url) of the last requests refused, for the log and the tests

        @staticmethod
        def allowed(url, party=""):
            """Whether a page may request URL: data and blob, https and wss to a host that is neither an address of
            this machine or the local network nor a name that resolves to one (NAMES); a name not known yet is
            refused and looked up (the page PARTY's window loads again once it is known as public)."""
            scheme = url.scheme().lower()
            if scheme in ("data", "blob") or exempt(url.toString()):
                return True
            if scheme not in ("https", "wss"):
                return False
            host = url.host()
            if T.private_address(host):
                return False
            known = NAMES.lookup(host, lambda name, verdict, address: in_gui(
                lambda: name_known(name, verdict, address)))
            if known is None:
                with _pending_lock:
                    _pending.setdefault(T.NameChecks.name(host), set()).add(T.NameChecks.name(party or host))
                return False
            return known[0] == "open"

        def interceptRequest(self, info):  # noqa: N802 - Qt's name
            url = info.requestUrl()
            try:
                party = info.firstPartyUrl().host()
            except (AttributeError, TypeError):
                party = ""
            if not self.allowed(url, party):
                info.block(True)
                self.refused = (self.refused + [url.toString()])[-20:]

    def profile():
        """The one login profile ("sandbox-ai login"), persistent in data_dir() (mode 0700)."""
        if _profile:
            return _profile[0]
        base = private_folder(data_dir())
        storage, cache = private_folder(base / "storage"), private_folder(base / "cache")
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfileBuilder
            builder = QWebEngineProfileBuilder()
            builder.setPersistentStoragePath(str(storage))
            builder.setCachePath(str(cache))
            builder.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            builder.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
            try:  # no permission is ever granted: none is stored either
                builder.setPersistentPermissionsPolicy(
                    QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)
            except AttributeError:
                pass
            made = builder.createProfile(STORAGE_NAME)
        except ImportError:  # Qt before 6.9
            made = QWebEngineProfile(STORAGE_NAME)
            made.setPersistentStoragePath(str(storage))
            made.setCachePath(str(cache))
            made.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            made.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        made.interceptor = LocalRefused(made)
        made.setUrlRequestInterceptor(made.interceptor)
        made.setSpellCheckEnabled(False)
        made.downloadRequested.connect(download_requested)  # a sign-in's: off; the app's browser asks
        _profile.append(made)
        return made

    def lock_down(settings):
        A = QWebEngineSettings.WebAttribute
        for attribute, value in ((A.JavascriptEnabled, True), (A.JavascriptCanOpenWindows, True),
                                 (A.JavascriptCanAccessClipboard, False), (A.JavascriptCanPaste, False),
                                 (A.LocalContentCanAccessRemoteUrls, False), (A.LocalContentCanAccessFileUrls, False),
                                 (A.PluginsEnabled, False), (A.PdfViewerEnabled, False),
                                 (A.AllowRunningInsecureContent, False), (A.AllowGeolocationOnInsecureOrigins, False),
                                 (A.NavigateOnDropEnabled, False), (A.ScreenCaptureEnabled, False),
                                 (A.FullScreenSupportEnabled, False), (A.WebRTCPublicInterfacesOnly, True),
                                 (A.AllowWindowActivationFromJavaScript, False), (A.PlaybackRequiresUserGesture, True),
                                 (A.LocalStorageEnabled, True)):
            try:
                settings.setAttribute(attribute, value)
            except (AttributeError, TypeError):
                pass

    class LoginPage(QWebEnginePage):
        """A page of a sign-in: every navigation of its main frame is decided by its window (LoginBrowser.decide)."""

        def __init__(self, window, parent=None):
            super().__init__(profile(), parent)
            self.window_ref = weakref.ref(window)
            lock_down(self.settings())
            self.certificateError.connect(self.refuse_certificate)
            if hasattr(self, "permissionRequested"):
                self.permissionRequested.connect(lambda permission: permission.deny())
            if hasattr(self, "featurePermissionRequested"):
                self.featurePermissionRequested.connect(self.refuse_feature)

        def refuse_feature(self, origin, feature):
            try:
                self.setFeaturePermission(origin, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
            except (AttributeError, TypeError):
                pass

        def refuse_certificate(self, error):
            error.rejectCertificate()
            window = self.window_ref()
            if window is not None:
                window.certificate_refused(error.url().toString(), error.description())

        def acceptNavigationRequest(self, url, kind, main_frame):  # noqa: N802 - Qt's name
            window = self.window_ref()
            if not main_frame:
                return True  # (its requests still go through LocalRefused)
            return window.decide(url.toString()) if window is not None else False

        def createWindow(self, kind):  # noqa: N802
            window = self.window_ref()
            return window.popup() if window is not None else None

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 - kept out of the log
            pass


class Sign:
    """What one sign-in shares between its window and its popups: the hosts it opens, those allowed for it, its flow,
    the terminal it hands the result to, the code it shows."""

    def __init__(self, url, hosts, target=None, code=None, title=""):
        self.start, self.hosts, self.allowed = url, tuple(hosts), set()
        self.flow = T.flow_of(url)
        self.target = weakref.ref(target) if target is not None else (lambda: None)
        self.code, self.title, self.result = code, title, None


class LoginBrowser(QWidget):
    """A window of a sign-in (SIGN); a popup of one has OPENER."""

    def __init__(self, sign, url=None, opener=None):
        super().__init__(None, Qt.WindowType.Window)
        from .widgets import close_controls
        close_controls(self)  # an [x] at its top right, Esc closes it
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.sign, self.opener, self.popups = sign, opener, []
        self.setWindowTitle(tr("Sign-in: {title}", title=sign.title) if sign.title else tr("Sign-in"))
        self.setObjectName("loginBrowser")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.back = QPushButton(tr("Back"))
        self.back.setToolTip(tr("The page before"))
        self.reload = QPushButton(tr("Reload"))
        self.address = QLineEdit()
        self.address.setReadOnly(True)
        self.address.setAccessibleName(tr("Address"))
        self.address.setToolTip(tr("The page's real address (it cannot be typed into)"))
        self.tls = QLabel()
        self.tls.setTextFormat(Qt.TextFormat.PlainText)
        self.external = QPushButton(tr("Open in the system browser"))
        self.external.setToolTip(tr("Go on in your usual browser instead (the app then cannot hand the result to "
                                    "the terminal)"))
        self.external.clicked.connect(lambda: self.open_externally(self.current_url()))
        for widget in (self.back, self.reload):
            top.addWidget(widget)
        top.addWidget(self.address, 1)
        top.addWidget(self.tls)
        top.addWidget(self.external)
        layout.addLayout(top)
        self.bar = TerminalBar(self)
        layout.addWidget(self.bar)
        self.view = QWebEngineView(self)
        self.page = LoginPage(self, self.view)
        self.view.setPage(self.page)
        self.back.clicked.connect(self.view.back)
        self.reload.clicked.connect(self.view.reload)
        self.page.urlChanged.connect(lambda url: self.shown(url.toString()))
        self.page.windowCloseRequested.connect(self.close)
        self.page.loadFinished.connect(lambda ok: self.shown(self.current_url()))
        layout.addWidget(self.view, 1)
        self.certificate_problem = None
        self.auto_reloads = 0
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.setInterval(300)  # several names known at once: one load
        self.reload_timer.timeout.connect(self.load_again)
        self.resize(980, 760)
        _windows.add(self)
        make_gui_call()
        for host in sign.hosts:  # the provider's names, looked up before its page asks for them
            NAMES.lookup(host)
        if sign.code:
            self.show_code(sign.code)
        if url:
            self.load(url)

    # -- what is shown --

    def current_url(self):
        return self.page.url().toString() or self.sign.start

    def shown(self, url):
        self.address.setText(T.shown_url(url, 2000))
        self.address.setCursorPosition(0)
        if self.certificate_problem and self.certificate_problem[0] == url:
            self.tls.setText(tr("TLS error: refused"))
        elif url.startswith("https://"):
            self.tls.setText(tr("TLS verified"))
        else:
            self.tls.setText("")
        self.tls.setToolTip(tr("Chromium checked the server's certificate; a certificate error is always refused"))

    def certificate_refused(self, url, description):
        self.certificate_problem = (url, description)
        self.bar.say(tr("Not opened: the certificate of {host} is not valid ({why}).", host=T.host_of(url),
                        why=description), seconds=0, level="error")

    def show_code(self, code):
        """A device code sign-in: the code the terminal printed, to type (or paste) into the page."""
        self.sign.code = code
        self.bar.ask(tr("Enter this one-time code on the page: {code}  (the terminal finishes the login by itself)",
                        code=code), [(tr("Copy the code"), lambda: self.copy_code(code))], level="info")

    def copy_code(self, code):
        put(code)
        self.show_code(code)

    # -- where it may go --

    def load(self, url):
        self.auto_reloads = 0
        self.page.load(QUrl(url))  # (acceptNavigationRequest decides, as for every other navigation)

    def name_resolved(self, host, parties):
        """HOST is public now: this page loads again when it (or one of PARTIES, the pages whose requests waited for
        it) is the one shown - a few times at most (AUTO_RELOADS)."""
        shown = T.NameChecks.name(T.host_of(self.current_url()))
        if (shown == host or shown in parties) and self.auto_reloads < AUTO_RELOADS:
            self.reload_timer.start()

    def load_again(self):
        if self.auto_reloads < AUTO_RELOADS:
            self.auto_reloads += 1
            self.view.reload()

    def name_checked(self, url):
        """Whether URL's host may be opened as far as its name goes: (True) known public; (False) this machine or the
        local network, or no such name (the bar says so); (None) being looked up - URL is loaded once it is known."""
        host = T.host_of(url)
        if not host or exempt(url):
            return True

        def known(name, verdict, address, url=url):
            in_gui(lambda: self.name_answer(url, verdict, address))
        found = NAMES.lookup(host, known)
        if found is None:
            self.bar.say(tr("Looking up {host}…", host=host), seconds=0, level="info")
            return None
        return self.name_answer(url, *found, load=False)

    def name_answer(self, url, verdict, address, load=True):
        if not _alive_or_shown(self):
            return False
        host = T.host_of(url)
        if verdict == "open":
            if load:
                self.bar.dismiss()
                self.page.load(QUrl(url))
            return True
        if verdict == "local":
            self.bar.say(tr("Not opened: {host} is {address}, on this machine or the local network, which the login "
                            "browser never reaches.", host=host, address=address or "?"), seconds=0, level="warn")
        else:
            self.bar.say(tr("Not opened: the name {host} cannot be found.", host=host), seconds=0, level="warn")
        return False

    def decide(self, url):
        """Whether the page may go to URL (its main frame): the end of the sign-in is caught; https on the provider's
        hosts (or those allowed for this sign-in) opens; another https host is asked about; this machine and
        anything not https never."""
        if url in ("", "about:blank"):
            return True  # (a popup's first, empty page)
        result = T.callback_result(self.sign.flow, url)
        if result is not None:
            self.finished(result, url)
            # the provider's code page may show; localhost is never opened
            return self.sign.flow.kind != "loopback" and SHOW_CODE_PAGE
        decision = "open" if exempt(url) else T.navigation(url, self.sign.hosts, self.sign.allowed)
        if decision in ("open", "ask") and not exempt(url):
            checked = self.name_checked(url)  # a name of this machine or the local network: never
            if checked is not True:
                return False
        if decision == "open":
            return True
        host = T.host_of(url)
        if decision == "ask":
            self.bar.ask(tr("The sign-in goes on at {host}, which is not {agent}'s. Open it here for this sign-in?",
                            host=host, agent=self.sign.title or tr("the agent")),
                         [(tr("Open {host} here", host=host), lambda: self.allow(host, url)),
                          (tr("Open in the system browser"), lambda: self.open_externally(url)),
                          (tr("Cancel"), lambda: None)], level="warn")
        elif decision == "local":
            self.bar.say(tr("Not opened: {address} is on this machine or the local network, which the login "
                            "browser never reaches.", address=T.shown_url(T.without_query(url), 200)), seconds=0,
                         level="warn")
        else:
            self.bar.say(tr("Not opened: {address} (the login browser opens https pages only).",
                            address=T.shown_url(url, 200)), seconds=0, level="warn")
        return False

    def allow(self, host, url):
        self.sign.allowed.add(host)
        self.page.load(QUrl(url))

    def open_externally(self, url):
        if url.startswith(("https://", "http://")):
            system_browser(url)

    def popup(self):
        """A page asks for a new window (a sign-in with another provider often does): one of this sign-in, under its
        rules; None beyond POPUPS."""
        self.popups = [p for p in self.popups if _alive(p)]
        if len(self.popups) >= POPUPS:
            return None
        window = LoginBrowser(self.sign, opener=self)
        self.popups.append(window)
        window.show()
        return window.page

    # -- the end of the sign-in --

    def finished(self, result, url):
        """The sign-in reached its end (callback_result): what the terminal gets is shown, and pasted after a
        click; a problem is said."""
        if result.problem:
            why = {"state": tr("it does not belong to the sign-in this window began"),
                   "code": tr("the code is not one a terminal can be given")}.get(result.problem, result.problem)
            self.bar.say(tr("The sign-in did not finish: {why}.", why=why), seconds=0, level="error")
            return
        self.sign.result = result.text
        target = self.sign.target()
        loopback = self.sign.flow.kind == "loopback"
        text = (tr("The sign-in returns to the program in the sandbox (port {port}), which this browser cannot reach. "
                   "Paste this address into its terminal if it asks for it: {text}",
                   port=_port(url), text=preview(result.text, 300)) if loopback else
                tr("Signed in. The code for the terminal: {text}", text=preview(result.text, 300)))
        choices = []
        if target is not None and target.running:
            choices.append((tr("Paste into “{tab}” and press Enter", tab=target.title()),
                            lambda: self.deliver(result.text)))
        choices.append((tr("Copy"), lambda: put(result.text)))
        if target is None or not target.running:
            text += " " + tr("(The terminal it was for is closed or has ended: copy it.)")
        self.bar.ask(text, choices, level="info")
        root = self.opener_root()
        if root is not self:
            root.bar.ask(text, choices, level="info")

    def opener_root(self):
        window = self
        while window.opener is not None and _alive(window.opener):
            window = window.opener
        return window

    def deliver(self, text):
        target = self.sign.target()
        if target is None or not target.running:
            put(text)
            self.bar.say(tr("The terminal is gone: the code is on the clipboard."), seconds=0, level="warn")
            return
        target.paste_line(text)
        target.reveal()
        for window in [self.opener_root(), *self.opener_root().popups]:
            if _alive(window):
                window.close()

    def login_ended(self, status):
        """The terminal's login ended: well (0) - the window has done its part and closes; else it says so."""
        if status == 0:
            for window in [self, *self.popups]:
                if _alive(window):
                    window.close()
        else:
            self.bar.say(tr("The login in the terminal ended (exit status {status}).", status=status), seconds=0,
                         level="warn")

    def closeEvent(self, event):  # noqa: N802 - Qt's name
        for window in self.popups:
            if _alive(window):
                window.close()
        self.view.setPage(None)
        self.page.deleteLater()
        super().closeEvent(event)


def _alive(widget):
    from shiboken6 import isValid
    return widget is not None and isValid(widget) and widget.isVisible()


def _alive_or_shown(widget):
    """A window that still exists (shown or not yet: tests decide before it is shown)."""
    from shiboken6 import isValid
    return widget is not None and isValid(widget)


def _port(url):
    try:
        return QUrl(url).port()
    except (TypeError, ValueError):
        return -1


def windows():
    from shiboken6 import isValid
    return [w for w in list(_windows) if isValid(w)]


def window_for(target):
    """The open login window of the terminal TARGET, or None."""
    for window in windows():
        if window.opener is None and window.sign.target() is target and window.isVisible():
            return window
    return None


def open_login(url, hosts, target=None, code=None, title="", parent=None, check=None, done=None):
    """Open URL in a login window for the terminal TARGET (its sign-in only opens HOSTS without asking). Chromium's
    sandbox is checked first, in a thread (chromium.check; CHECK replaces it in tests): when it cannot start, the
    system browser opens URL instead and DONE(None, why) is called; else DONE(window, "")."""
    if MISSING:
        system_browser(url)
        if done is not None:
            done(None, tr("QtWebEngine is not installed ({error})", error=MISSING))
        return
    existing = window_for(target) if target is not None else None
    if existing is not None and T.flow_of(url) == existing.sign.flow:
        existing.show()
        existing.raise_()
        existing.activateWindow()
        if done is not None:
            done(existing, "")
        return

    def ready(result, error):
        if error is not None or not result.ok:
            system_browser(url)
            if done is not None:
                done(None, chromium.unavailable_text(result) if error is None else str(error))
            return
        if existing is not None:
            existing.close()
        window = LoginBrowser(Sign(url, hosts, target, code, title), url)
        window.show()
        window.raise_()
        if done is not None:
            done(window, "")

    if check is not None:
        ready(check(), None)
        return
    from .widgets import background
    background(chromium.check, done=ready, owner=parent)


def forget():
    """Settings > Forget the login browser's data: its cookies, storage and cache (the app's browser's too: the same
    profile). Refused (ValueError) while a login or browser window is open; with the profile made in this run its
    cookies and cache are cleared, else the folder goes."""
    if windows() or browsers():
        raise ValueError(tr("Close the sign-in windows first."))
    if _profile:
        made = _profile[0]
        made.cookieStore().deleteAllCookies()
        made.clearHttpCache()
        try:
            made.clearAllVisitedLinks()
        except AttributeError:
            pass
        return
    folder = data_dir()
    if folder.is_symlink():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)


def shutdown():
    """Before QtWebEngine goes (terminal.shutdown): the windows' pages first, then the profile."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    for window in browsers():
        window.drop_pages()
    for window in windows():
        if isValid(window.view):
            window.view.setPage(None)
        if isValid(window.page):
            window.page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for window in windows() + browsers():
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _windows.clear()
    BROWSERS.clear()
    for made in _profile:
        if isValid(made):
            made.deleteLater()
    _profile.clear()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
