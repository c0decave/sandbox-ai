"""Terminal tabs inside the app: xterm.js in QtWebEngine, connected to a pseudo-terminal (pty_bridge).

The page and its scripts are local files (sandbox_ai_app/web, xterm.js vendored by scripts/vendor_xterm.py);
the view loads nothing else: a request interceptor blocks every URL except the files below
sandbox_ai_app/web and Qt's qwebchannel.js, navigation away from the page is refused, local content may not
reach remote URLs, and the profile keeps nothing on disk. Without
QtWebEngine or the vendored files the app uses a terminal window instead (SandboxAI.in_terminal), and so it does for
a new terminal while Chromium's sandbox cannot get its namespaces (chromium.py: Chromium would end the whole app).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import weakref

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, QUrl, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSplitter, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import chromium, terminal_text, theme
from .backend import setting as backend_setting
from .files import printable
from .i18n import N_, tr
from .pty_bridge import PtyBridge
from .widgets import (
    ask_first, background, disclosure, fill, fit_columns, inform, on_screen, plain_label, row_key, safe_tooltip, table,
    warn,
)

WEB = Path(__file__).resolve().with_name("web")
QWIDGETSIZE_MAX = (1 << 24) - 1  # Qt's "no maximum" of a widget's size
TABS_MIN_HEIGHT = 160  # px: the terminal tabs' least height
# The vendoring script next to the package (a checkout or sandbox-ai's installation; not in a wheel).
VENDOR_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "vendor_xterm.py"
VENDOR_FILES = ("xterm.js", "xterm.css", "addon-fit.js")
_ending = set()  # bridges of closed tabs whose command has not ended yet (kept until it has)
_views = weakref.WeakSet()
CHANNEL_SCRIPT = "qrc:///qtwebchannel/qwebchannel.js"
LOCAL = "local"  # this machine, as sandbox-ai hosts names it
# The variable each terminal tab of a start gets: the start records it (sandbox-ai's sessions.launch, list --json
# "app_tab"), so that after a restart the app knows which running session its tab showed.
TAB_TAG = "SANDBOX_AI_APP_TAB"
SAVED_TABS = "terminal-tabs"  # the app's state: the terminal tabs of running starts, in their order
SAVED_LIMIT = 40  # tabs remembered at most

try:
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                         QWebEngineUrlRequestInterceptor)
    from PySide6.QtWebEngineWidgets import QWebEngineView
    MISSING = None
except ImportError as exc:  # PySide6-Addons / PySide6-WebEngine not installed
    MISSING = str(exc)


def available(web=WEB):
    """(True, "") if terminals can run inside the app, else (False, why)."""
    if os.environ.get("SANDBOX_AI_APP_NO_WEBENGINE"):
        return False, tr("switched off by SANDBOX_AI_APP_NO_WEBENGINE")
    if MISSING:
        return False, tr("QtWebEngine is not installed ({error})", error=MISSING)
    missing = [name for name in VENDOR_FILES if not (Path(web) / "vendor" / name).is_file()]
    if missing:
        return False, tr("xterm.js is not vendored ({files}); run scripts/vendor_xterm.py", files=", ".join(missing))
    return True, ""


def prepare_window(window, wanted=True, environ=None, platform=None):
    """The main WINDOW made an OpenGL window from the start when terminal tabs are WANTED and can run. A web view
    (QtWebEngine draws through OpenGL) put into a window made without it makes Qt destroy the window and make it
    again as an OpenGL one: at the first terminal tab the whole window vanished and came back (a tiling window
    manager laid out every window anew). A hidden OpenGL widget of no size, in the window before it is first shown,
    gives it that kind at once. None (nothing done) without terminal tabs, on Qt's offscreen platform or with
    SANDBOX_AI_APP_NO_GL_WINDOW set."""
    environ = os.environ if environ is None else environ
    if not wanted or environ.get("SANDBOX_AI_APP_NO_GL_WINDOW") or not available()[0]:
        return None
    if platform is None:
        from PySide6.QtGui import QGuiApplication
        platform = QGuiApplication.platformName()
    if platform in ("offscreen", "minimal", "vnc"):
        return None
    try:
        from PySide6.QtOpenGLWidgets import QOpenGLWidget
    except ImportError:
        return None
    surface = QOpenGLWidget(window)
    surface.setObjectName("openglSurface")
    surface.setFixedSize(0, 0)
    surface.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    surface.hide()
    return surface


if not MISSING:
    class LocalOnly(QWebEngineUrlRequestInterceptor):
        """Blocks every request except the page's own files and Qt's qwebchannel.js."""

        def __init__(self, web, parent=None):
            super().__init__(parent)
            self.web = os.path.realpath(web)

        def allowed(self, url):
            if url.toString() == CHANNEL_SCRIPT:
                return True
            if url.scheme() != "file" or url.host():
                return False
            path = os.path.realpath(url.toLocalFile())
            return path.startswith(self.web + os.sep)

        def interceptRequest(self, info):  # noqa: N802 - Qt's name
            if not self.allowed(info.requestUrl()):
                info.block(True)

    class TerminalPage(QWebEnginePage):
        def __init__(self, profile, start, parent=None):
            super().__init__(profile, parent)
            self.start = start

        def acceptNavigationRequest(self, url, kind, main_frame):  # noqa: N802
            return url == self.start

        def createWindow(self, kind):  # noqa: N802
            return None

    _profiles = {}

    def profile(web=WEB):
        """An off-the-record profile per page directory: nothing is stored, nothing remote is loaded."""
        key = os.path.realpath(web)
        if key not in _profiles:
            created = QWebEngineProfile()  # no storage name: off the record
            created.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            created.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            created.interceptor = LocalOnly(key, created)
            created.setUrlRequestInterceptor(created.interceptor)
            _profiles[key] = created
        return _profiles[key]

    def lock_down(settings):
        A = QWebEngineSettings.WebAttribute
        # A file:// page needs LocalContentCanAccessFileUrls for its own scripts and styles; which files it may
        # load is decided by LocalOnly (only those below sandbox_ai_app/web) and the page's CSP.
        for attribute, value in ((A.LocalContentCanAccessRemoteUrls, False), (A.LocalContentCanAccessFileUrls, True),
                                 (A.JavascriptCanOpenWindows, False), (A.JavascriptCanAccessClipboard, False),
                                 (A.PluginsEnabled, False), (A.PdfViewerEnabled, False),
                                 (A.AllowRunningInsecureContent, False), (A.AllowGeolocationOnInsecureOrigins, False),
                                 (A.NavigateOnDropEnabled, False), (A.LocalStorageEnabled, False),
                                 (A.WebGLEnabled, False), (A.ScreenCaptureEnabled, False),
                                 (A.AutoLoadIconsForPage, False), (A.JavascriptEnabled, True)):
            settings.setAttribute(attribute, value)


    class TerminalWebView(QWebEngineView):
        """The web view of a terminal: its context menu is the terminal's (TerminalView.menu), not Chromium's."""

        def contextMenuEvent(self, event):  # noqa: N802 - Qt's name
            owner = self.parent()
            if hasattr(owner, "menu"):
                owner.menu(event.globalPos())
            event.accept()


def login_of(argv):
    """The agent of a terminal that runs `sandbox-ai login AGENT` (here, or on a host over SSH), else None."""
    argv = [str(arg) for arg in argv]
    if argv and os.path.basename(argv[0]) == "ssh":
        words = remote_words(argv[-1])
        return terminal_text.login_agent(words) if words else None
    return terminal_text.login_agent(argv)


class TerminalView(QWidget):
    """One terminal: the web view and the command's pseudo-terminal, the bar it asks its questions in (a paste, a
    program's OSC 52, a sign-in link), and, for `sandbox-ai login AGENT`, what watches the login for its sign-in
    link and one-time code (terminal_text.LoginWatch) and opens the login browser (login_browser.py)."""
    exited = Signal(int)

    def __init__(self, argv, web=WEB, cwd=None, parent=None, env=None):
        super().__init__(parent)
        from .terminal_tools import TerminalBar, TerminalHelper
        self.argv = list(argv)
        self.tab_title = " ".join(os.path.basename(str(a)) for a in self.argv[:3])
        # What the tab shows, for its reopening after a restart (TerminalsPage.remembered): the tag the start in it
        # records (SANDBOX_AI_APP_TAB in ENV), and the host and start it attaches to.
        self.tab_tag = (env or {}).get(TAB_TAG)
        self.start_host = self.start_name = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        page_url = QUrl.fromLocalFile(str(Path(web) / "terminal.html"))
        self.bridge = PtyBridge(self.argv, cwd=cwd, env=env, theme=theme.terminal_theme(),
                                exit_text=tr("[sandbox-ai: exit status {status}; close the tab with ×]"), parent=self)
        self.bridge.exited.connect(self.exited)
        self.bar = TerminalBar(self)
        layout.addWidget(self.bar)
        self.helper = TerminalHelper(self, self)
        self.view = TerminalWebView(self)
        self.page = TerminalPage(profile(web), page_url, self.view)
        lock_down(self.page.settings())
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("pty", self.bridge)
        self.channel.registerObject("helper", self.helper)
        self.page.setWebChannel(self.channel)
        self.view.setPage(self.page)
        layout.addWidget(self.view)
        # A login: its output is read for the sign-in link (and a device code), its end closes the login browser.
        self.login_agent = login_of(self.argv)
        self.watch, self.offered, self.code_shown = None, None, False
        if self.login_agent:
            self.watch = terminal_text.LoginWatch()
            self.bridge.output.connect(self.login_output)
            self.exited.connect(self.login_exited)
        # Chromium's page process may end on its own (out of memory, a crash in it): the app goes on, the tab says
        # so, and the log window and app.log keep why (it was silent before: an empty tab).
        self.page.renderProcessTerminated.connect(self.page_ended)
        self.page.load(page_url)
        _views.add(self)

    def page_ended(self, status, code):
        from . import applog
        name = getattr(status, "name", str(status))
        if name == "NormalTerminationStatus":
            return
        applog.log("ERROR", "terminal", f"the terminal page of {' '.join(self.argv[:3])} ended: {name}, exit code "
                                        f"{code} (Chromium's page process; the command itself may still run)")

    @property
    def running(self):
        return self.bridge.running

    # -- what the terminal's tools need of it --

    def title(self):
        tabs = self.tabs_widget()
        if tabs is not None and tabs.indexOf(self) >= 0:
            return tabs.tabText(tabs.indexOf(self))
        return self.tab_title

    def tabs_widget(self):
        widget = self.parentWidget()
        while widget is not None and not isinstance(widget, QTabWidget):
            widget = widget.parentWidget()
        return widget

    def status(self, text):
        window = self.window()
        if window is not self and hasattr(window, "statusBar"):
            window.statusBar().showMessage(text, 5000)
        else:
            self.bar.say(text, seconds=3)

    def focus_terminal(self):
        self.view.setFocus()

    def reveal(self):
        """This tab, shown and focused (the login browser has pasted into it)."""
        window = self.window()
        if hasattr(window, "show_page"):
            window.show_page("terminals")
        tabs = self.tabs_widget()
        if tabs is not None:
            tabs.setCurrentWidget(self)
        window.raise_()
        window.activateWindow()
        self.view.setFocus()

    def paste_line(self, text):
        """TEXT pasted as the program asked for (bracketed or not), then Enter (a sign-in's code)."""
        self.run_js(f"window.sandboxTerminal && window.sandboxTerminal.pasteLine({json.dumps(str(text))})",
                    lambda _result: None)

    def launcher(self):
        page = self.parentWidget()
        while page is not None and not hasattr(page, "launcher"):
            page = page.parentWidget()
        return page.launcher() if page is not None else None

    def hosts_for(self, url):
        """(agent, hosts): the hosts the login browser opens without asking for URL from this terminal - those of
        its login's agent, else those of the provider URL belongs to (no agent: none)."""
        launcher = self.launcher()
        agent = self.login_agent or terminal_text.agent_of_url(url, launcher)
        return agent, terminal_text.login_hosts(agent, launcher) if agent else ()

    def open_link(self, url, where=None):
        """Open URL (a link of the terminal): a provider's sign-in page in the login browser when Settings say so
        (WHERE "app" or "system" decides instead), everything else in the system browser."""
        from .terminal_tools import system_browser
        if not terminal_text.plausible(url):
            return
        agent, hosts = self.hosts_for(url)
        in_scope = terminal_text.host_in(terminal_text.host_of(url), hosts)
        if where == "app" or (where is None and in_scope and backend_setting("login_links") == "app"):
            self.open_login(url, agent, hosts)
        else:
            system_browser(url)

    def open_login(self, url, agent=None, hosts=None):
        from . import login_browser
        if hosts is None:
            agent, hosts = self.hosts_for(url)

        def done(window, why):
            if window is None and why:
                self.bar.say(tr("Opened in the system browser instead: {why}", why=why), seconds=0, level="warn")
        login_browser.open_login(url, hosts, target=self, code=self.watch.code if self.watch else None,
                                 title=agent or "", parent=self, done=done)

    # -- a login --

    def login_output(self, encoded):
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return
        for url in self.watch.feed(data):
            if self.offered is None and terminal_text.host_in(terminal_text.host_of(url), self.hosts_for(url)[1]):
                self.offered = url
                self.offer_signin(url, auto=True)
        if self.offered and self.watch.code and not self.code_shown:
            self.code_shown = True
            from . import login_browser
            window = login_browser.window_for(self)
            if window is not None:
                window.show_code(self.watch.code)
            self.offer_signin(self.offered)

    def offer_signin(self, url, auto=False):
        """The login printed its sign-in link: said above the terminal, with Open and Copy; opened in the login
        browser at once when Settings say sign-in links open in the app (AUTO: the first time)."""
        from .terminal_tools import system_browser
        code = self.watch.code if self.watch else None
        text = tr("Sign-in link: {url}", url=terminal_text.shown_url(url, 160))
        if code:
            text += " " + tr("One-time code: {code}", code=code)
        choices = [(tr("Open in the app's browser"), lambda: self.open_login(url)),
                   (tr("Open in the system browser"), lambda: system_browser(url)),
                   (tr("Copy link"), lambda: self.helper.copy_text(url))]
        if backend_setting("login_links") == "system":
            choices = [choices[1], choices[0], choices[2]]
        self.bar.ask(text, choices, level="info")
        if auto and backend_setting("login_links") == "app":
            self.open_login(url)

    def login_exited(self, status):
        from . import login_browser
        window = login_browser.window_for(self)
        if window is not None:
            window.login_ended(status)
        if status == 0 and self.offered:
            self.bar.dismiss()

    # -- the context menu --

    def menu(self, position):
        """Right click: what the page says is selected and under the mouse, then the menu at POSITION."""
        self.run_js("window.sandboxTerminal ? window.sandboxTerminal.menuState() : ''",
                    lambda state: self.show_menu(state, position))

    def menu_actions(self, state):
        """[(text, enabled, callback or None for a separator)] of the context menu for STATE (the page's
        menuState(): whether something is selected, the link under the mouse)."""
        from .terminal_tools import shortcut_names
        link = state.get("link") if isinstance(state.get("link"), str) else None
        selected = bool(state.get("selection"))
        copy_keys, paste_keys = shortcut_names()
        actions = [(tr("Copy") + "\t" + copy_keys, selected, lambda: self.run_js("window.sandboxTerminal.copy()",
                                                                                  lambda _r: None)),
                   (tr("Paste") + "\t" + paste_keys, True, lambda: self.run_js("window.sandboxTerminal.paste()",
                                                                                lambda _r: None))]
        if link and terminal_text.plausible(link):
            actions += [("", False, None), (tr("Copy link"), True, lambda: self.helper.copy_text(link)),
                        (tr("Open link"), True, lambda: self.open_link(link))]
            agent, hosts = self.hosts_for(link)
            if terminal_text.host_in(terminal_text.host_of(link), hosts):
                if backend_setting("login_links") == "app":
                    actions.append((tr("Open in the system browser"), True,
                                    lambda: self.open_link(link, where="system")))
                else:
                    actions.append((tr("Open in the app's browser"), True, lambda: self.open_link(link, where="app")))
        actions += [("", False, None),
                    (tr("Select all"), True, lambda: self.run_js("window.sandboxTerminal.selectAll()",
                                                                 lambda _r: None)),
                    (tr("Clear selection"), selected, lambda: self.run_js("window.sandboxTerminal.clearSelection()",
                                                                          lambda _r: None))]
        return actions

    def show_menu(self, state, position):
        from PySide6.QtWidgets import QMenu
        try:
            state = json.loads(state) if state else {}
        except ValueError:
            state = {}
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        for text, enabled, callback in self.menu_actions(state if isinstance(state, dict) else {}):
            if callback is None:
                menu.addSeparator()
                continue
            action = menu.addAction(text)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _=False, f=callback: f())
        menu.popup(position)
        return menu

    def run_js(self, script, callback):
        self.page.runJavaScript(script, 0, callback)

    def close_terminal(self):
        """End the command; the bridge outlives the tab until the command is gone (SIGKILL after a while)."""
        bridge = self.bridge
        if bridge.running:
            _ending.add(bridge)
            self.channel.deregisterObject(bridge)  # (the page goes with the tab; the bridge, once deleted, from it)
            bridge.setParent(None)
            bridge.exited.connect(lambda _status, b=bridge: ended(b))
        bridge.close()


def ended(bridge):
    """The command of a closed tab has ended: its bridge goes. It has no parent any more, and its signal's connection
    to the function that knows it held it alive inside Qt, where Python's collector does not look: every closed tab
    left its bridge, its Pty and their notifiers and timers for the rest of the run (stability round 3, the soak)."""
    _ending.discard(bridge)
    bridge.deleteLater()


def shutdown():
    """End every terminal and release QtWebEngine in the order it needs, while the application still runs: the pages
    first, then the web views, then the profiles (else Chromium aborts at exit, or Python's own cleanup at the end of
    the interpreter deletes a web view whose QtWebEngine is gone: "shared QObject was deleted directly", a crash).
    Called when the application quits; a second call does nothing."""
    from shiboken6 import isValid
    if not MISSING:
        from . import login_browser
        login_browser.shutdown()  # (its pages, then its profile: before QtWebEngine goes)
    views = [view for view in list(_views) if isValid(view)]
    for view in views:
        view.close_terminal()
        if isValid(view.view):
            view.view.setPage(None)
        if isValid(view.page):
            view.page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for view in views:  # the web views before their profile (no page of theirs is made again meanwhile)
        if isValid(view):
            view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _views.clear()
    for bridge in list(_ending):
        if isValid(bridge):
            bridge.pty.kill()
    if not MISSING and _profiles:
        for created in _profiles.values():
            if isValid(created):
                created.deleteLater()
        _profiles.clear()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# Why a start of `sandbox-ai list --json` has no terminal to attach to: its "attach_why" (guessed from its kind and
# state for an older sandbox-ai without it). The web interface (sandbox-ai web) says the same.
ATTACH_WHY = {
    "foreground": N_("Runs in the foreground of its own terminal (started with --no-tmux, as a task or without a "
                     "terminal): nothing to attach. Start it with tmux (the default for interactive starts) to "
                     "attach from here."),
    "paused": N_("Paused: resume it to attach."),
    "mcp": N_("An MCP server sandbox: it has no terminal of its own."),
    "stopped": N_("It does not run."),
    "damaged": N_("Damaged: see sandbox-ai list."),
}
STATES = {"running": N_("running"), "paused": N_("paused")}


def attach_why(entry):
    """Why the start ENTRY (of list --json) cannot be attached, in the app's language; "" when it can."""
    if entry.get("attachable"):
        return ""
    why = entry.get("attach_why")
    if why not in ATTACH_WHY:
        why = ("mcp" if entry.get("kind") == "mcp" else "foreground" if entry.get("kind") == "run" else
               "paused" if entry.get("state") == "paused" else "stopped")
    return tr(ATTACH_WHY[why])


OWNER = "\0owner"  # what grouped() adds to an entry: the name of the start it belongs to, "" for none (an MCP server)
ORPHAN_TIP = N_("An MCP server that belongs to no start that runs: started on its own (sandbox-ai mcp start), or the "
                "start it was started for ended.")


def grouped(host, entries):
    """ENTRIES (of `sandbox-ai list --json` on HOST) in the order of their groups: each start that belongs to no other
    one, followed by those that belong to it (start_tree.owners: its MCP servers and helpers, the members of a team
    it leads), marked with the name of that start (OWNER); then the MCP servers that belong to no start (OWNER "").
    The entries are copies: what came back is not changed."""
    from . import backend, start_tree
    instances = []
    for entry in entries:
        instance = backend.listed_instance(host, entry)
        if instance is not None:
            instances.append((instance, entry))
    starts = [i for i, _ in instances]
    owned = start_tree.owners(starts)
    alone = set(start_tree.orphans(starts, owned))
    names = {i.key: i.name for i in starts}
    below = {}
    for instance, entry in instances:
        if instance.key in owned:
            below.setdefault(owned[instance.key][0], []).append({**entry, OWNER: names[owned[instance.key][0]]})
    found = []
    for instance, entry in instances:
        if instance.key not in owned and instance.key not in alone:
            found.append(dict(entry))
            found += below.get(instance.key, [])
    return found + [{**entry, OWNER: ""} for instance, entry in instances if instance.key in alone]


def attach_key(argv):
    """What a terminal's command line stands for when it attaches to a start (one tab per start):
    `sandbox-ai [session] attach NAME` on this machine, or the same over SSH; None for every other command."""
    argv = [str(arg) for arg in argv]
    if len(argv) >= 2 and argv[-2] == "attach":
        base = argv[:-2]
        if base and base[-1] == "session":
            base = base[:-1]
        if base and (base[-1].endswith("main.py") or os.path.basename(base[-1]) == "sandbox-ai"):
            return ("here", argv[-1])
    if len(argv) >= 2 and os.path.basename(argv[0]) == "ssh" and re.search(r"(?:^|\s)attach\s", argv[-1]):
        return ("ssh", tuple(argv))
    return None


AGENT_NAMES = ("claude", "codex", "opencode")


def remote_words(command):
    """The words of `sandbox-ai ...` in the command an ssh terminal hands another host (hosts.remote_command: `exec
    sandbox-ai ARGS`, or the same in an if/else that looks for it; the program is the first one exec runs), or None
    when it is not one."""
    try:
        lexer = shlex.shlex(str(command), posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if index and tokens[index - 1] == "exec":  # sandbox-ai, from PATH, ~/.local/bin or the host's own command
            words = []
            for word in tokens[index + 1:]:
                if word in (";", "&&", "||", "&", "|"):
                    break
                words.append(word)
            return ["sandbox-ai", *words]
    return None


def tab_key(index, platform=None):
    """The key that shows terminal tab INDEX (0-9): Alt+1 ... Alt+9, Alt+0 - on macOS Control+1 ... (Qt's Meta there):
    Option+digit types characters on a Mac - [ ] | { } on a German keyboard (Option+5 ... 9), ¡ ™ £ ... on an
    American one - and as the shortcut it took them from every terminal (no pipe could be typed)."""
    platform = sys.platform if platform is None else platform
    return f"{'Meta' if platform == 'darwin' else 'Alt'}+{(index + 1) % 10}"


def attached_name(words):
    """The start NAME of `sandbox-ai [session] attach NAME` (WORDS), else None."""
    words = [str(w) for w in words or []]
    if len(words) >= 3 and words[-2] == "attach" and words[1:-2] in ([], ["session"]):
        return words[-1]
    return None


def detaches(argv, launcher=None):
    """Whether closing the terminal of ARGV only detaches it, the start running on: attaching to a start (tmux),
    a start with --session, or an interactive agent start, which the launcher runs as a detachable session in tmux
    by default (not with --no-tmux, --probe, --dry-run, --binary, a prompt or subcommand for the agent, or on macOS
    with a sandbox-ai from before attaching reached podman machine; sandbox-ai's detachable() and resolve_tmux()).
    Everything else (a build, doctor, a task) ends with its tab."""
    if attach_key(argv) is not None:
        return True
    argv = [str(arg) for arg in argv]
    if argv and os.path.basename(argv[0]) == "ssh":  # a start on another host: its words there (its tmux is there)
        words = remote_words(argv[-1])
        if words is None:
            return False
        argv, launcher = words, None
    at = next((i + 1 for i, token in enumerate(argv) if os.path.basename(token) in ("main.py", "sandbox-ai")), None)
    if at is None or at >= len(argv) or argv[at] not in AGENT_NAMES:
        return False
    rest = argv[at + 1:]
    options, agent_args = (rest[:rest.index("--")], rest[rest.index("--") + 1:]) if "--" in rest else (rest, [])
    names = {option.split("=", 1)[0] for option in options}
    if names & {"--dry-run", "--probe", "-n"}:
        return False
    if "--session" in names:
        return True
    # macOS: tmux by default since sandbox-ai attaches through podman exec (its on_machine); not before.
    if names & {"--no-tmux", "--binary"} or (sys.platform == "darwin" and not hasattr(launcher, "on_machine")):
        return False
    if "--tmux" in names:
        return True
    if launcher is not None and hasattr(launcher, "interactive_args"):
        try:
            return bool(launcher.interactive_args(argv[at], agent_args))
        except Exception:  # noqa: BLE001 - an older launcher: options only means interactive
            pass
    return all(arg.startswith("-") for arg in agent_args)


class TerminalsPage(QWidget):
    """Tabs with terminals, and below them, folded away at first, Sessions: every start that runs on this machine
    (or a host of `sandbox-ai hosts`), however it was started: the command line, the app, the web interface, another
    machine. One click attaches a start in tmux (a tab per start: clicked again, its tab is shown); the others say
    why they have no terminal to attach. The list is read when the page is shown and every few seconds while it is.
    The line above the tabs says only what is wrong (no QtWebEngine or xterm.js, Chromium's sandbox): what the page
    is for is the guided tour's and the Help's to say, not a sentence that stays."""

    def __init__(self, web=WEB, parent=None, sai=None, in_terminal=None):
        super().__init__(parent)
        self.web, self.sai, self.in_terminal = web, sai, in_terminal
        layout = QVBoxLayout(self)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        bar = QHBoxLayout()
        bar.addWidget(self.hint, 1)
        # Only xterm.js missing (QtWebEngine is there): it can be fetched now, and works without a restart.
        self.fetch = QPushButton(tr("Download xterm.js now"))
        self.fetch.setToolTip(tr("Runs scripts/vendor_xterm.py: the pinned xterm.js from registry.npmjs.org, "
                                 "checked before it is used"))
        self.fetch.clicked.connect(self.vendor)
        bar.addWidget(self.fetch)
        layout.addLayout(bar)
        self.command = None
        self.sandbox_problem = ""  # why the last terminal could not be a tab (Chromium's sandbox), until one can
        self.waiting, self.checking = [], False  # tabs asked for while Chromium's sandbox is checked (open())
        self.show_state()
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        # A terminal needs some rows: the page (and so the window) keeps that much height, Sessions folded or not.
        self.tabs.setMinimumHeight(TABS_MIN_HEIGHT)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        # Tabs move left and right by dragging (every lookup goes by the view, indexOf, never a kept index), and
        # Alt+1 ... Alt+9, Alt+0 show the first ten (while the page or one of its terminals has the focus); on macOS
        # Control+1 ... (tab_key: Option+digit types characters there).
        self.tabs.setMovable(True)
        from PySide6.QtGui import QKeySequence, QShortcut
        self.tab_keys = []
        for number in range(10):
            key = QShortcut(QKeySequence(tab_key(number)), self)
            key.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            key.activated.connect(lambda index=number: self.show_tab(index))
            self.tab_keys.append(key)
        self.tabs.tabBar().tabMoved.connect(lambda *_: self.save_tabs())
        self.store, self.reopening = None, False  # the app's state (MainWindow), and while reopen() opens tabs
        self.entries, self.problem, self.reading, self.shown_rows, self.ticks = [], "", None, None, 0
        self.starts = self.make_starts() if sai is not None else None
        if self.starts is None:
            layout.addWidget(self.tabs, 1)
        else:
            # The terminals first; Sessions below them, folded to one line until opened (then ~30% of the height).
            self.split = QSplitter(Qt.Orientation.Vertical)
            self.split.setChildrenCollapsible(False)
            self.split.addWidget(self.tabs)
            self.split.addWidget(self.starts)
            self.split.setStretchFactor(0, 7)
            self.split.setStretchFactor(1, 3)
            layout.addWidget(self.split, 1)
            self.fold_sessions(self.sessions_fold.isChecked())
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.setInterval(backend_setting("sandboxes_interval"))  # started while the page is shown

    def show_tab(self, index):
        """Alt+1 ... Alt+0: the tab at INDEX (0-9) of the terminals, with the focus in its terminal; none there: nothing."""
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)
            widget = self.tabs.widget(index)
            if widget is not None:
                widget.setFocus(Qt.FocusReason.ShortcutFocusReason)

    # -- the running starts (Sessions) --

    def make_starts(self):
        box = QFrame()
        box.setObjectName("sessions")
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(6, 2, 6, 6)
        top = QHBoxLayout()
        # Folded at first (the terminals are what the page is for); the window keeps whether it was opened.
        self.sessions_fold = disclosure(QToolButton())
        self.sessions_fold.setText(tr("Sessions"))
        self.sessions_fold.setAccessibleName(tr("Sessions"))
        self.sessions_fold.setToolTip(tr("Every start that runs on this machine or the host chosen beside it, however "
                                         "it was started; one in tmux opens in a tab with one click"))
        self.sessions_fold.toggled.connect(self.fold_sessions)
        top.addWidget(self.sessions_fold)
        self.host = QComboBox(self)  # (a parent at once: load_hosts() shows or hides it - no window of its own)
        self.host.setToolTip(tr("This machine, or a host of sandbox-ai hosts (over SSH)"))
        self.host.setAccessibleName(tr("Host"))
        self.load_hosts()
        self.host.currentIndexChanged.connect(lambda _i: self.host_changed())
        from .host_choice import follow_hosts
        follow_hosts(self.host, self.hosts_changed)  # a host added, renamed or removed: here at once
        self.summary = plain_label(role="secondary")
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setToolTip(tr("Read again now (it is read every few seconds anyway)"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh_starts())
        top.addWidget(self.host)
        top.addWidget(self.summary, 1)
        top.addWidget(self.refresh_button)
        box_layout.addLayout(top)
        self.list = table([tr("Name"), tr("Agent"), tr("State"), tr("Workspace"), tr("Terminal")], stretch=3,
                          compact=(1, 2, 4),
                          sortable=False)
        self.list.setAccessibleName(tr("Sessions"))
        self.list.cellDoubleClicked.connect(lambda row, _column: self.attach_row(row))
        box_layout.addWidget(self.list, 1)
        self.top_row = top
        return box

    def fold_sessions(self, opened):
        """Sessions opened (OPENED) or folded: folded, only its first line (the count, the host, Refresh) stays and
        the terminals take the rest; opened, it takes about 30% of the page's height."""
        self.list.setVisible(opened)
        total = sum(self.split.sizes()) or self.split.height()
        if opened:
            self.starts.setMaximumHeight(QWIDGETSIZE_MAX)
            if total > 0:
                self.split.setSizes([total - total * 3 // 10, total * 3 // 10])
        else:
            margins = self.starts.layout().contentsMargins()
            self.starts.setMaximumHeight(self.top_row.sizeHint().height() + margins.top() + margins.bottom()
                                         + 2 * self.starts.frameWidth())
            if total > 0:
                self.split.setSizes([total, 0])  # the terminals take the rest (Sessions keeps its one line)

    def reveal(self, target):
        """A tour points at TARGET (MainWindow.point_at): Sessions opens to be seen."""
        if target == "starts" and self.starts is not None:
            self.sessions_fold.setChecked(True)

    def hosts_changed(self):
        """The hosts file changed: the chooser again; when its host left, this machine's starts."""
        chosen = self.host.currentData()
        self.load_hosts()
        if chosen != self.host.currentData():
            self.host_changed()

    def load_hosts(self):
        """This machine and the hosts of sandbox-ai hosts (read again when the page is shown: one may have been
        added on the Hosts page meanwhile); the chosen one stays chosen while it is there."""
        chosen = self.host.currentData()
        try:
            others = [e for e in self.sai.host_entries() if e.get("kind") != LOCAL] \
                if self.sai.hosts_supported() else []
        except Exception:  # noqa: BLE001 - a damaged hosts file: this machine still counts
            others = []
        self.host.blockSignals(True)
        self.host.clear()
        self.host.addItem(tr("{name} (this machine)", name=LOCAL), LOCAL)
        for entry in others:
            self.host.addItem(printable(f"{entry['name']}  {entry.get('ssh', '')}"), entry["name"])
        self.host.setCurrentIndex(max(0, self.host.findData(chosen)))
        self.host.blockSignals(False)
        self.host.setVisible(self.host.count() > 1)
        if chosen is not None and self.host.currentData() != chosen:
            self.host_changed()  # the chosen host was removed: this machine instead

    def chosen_host(self):
        return self.host.currentData() or LOCAL

    def host_changed(self):
        self.entries, self.problem, self.shown_rows = [], "", None
        self.show_starts()
        self.refresh_starts()

    def showEvent(self, event):  # noqa: N802 - Qt's name
        """Read when the page is shown, not when the window is built (no command, no SSH, for a page nobody
        opened)."""
        super().showEvent(event)
        if self.starts is not None and not self.timer.isActive():
            self.timer.start()  # (it only ticks while the page is shown: no wakeups for a page nobody sees)
        if self.starts is not None and not event.spontaneous():
            self.load_hosts()
            self.refresh_starts()

    def hideEvent(self, event):  # noqa: N802 - Qt's name
        super().hideEvent(event)
        if self.starts is not None:
            self.timer.stop()

    def tick(self):
        """The timer: while the page is shown (not in a minimised window: `sandbox-ai list`, a Python start, every few
        seconds for nobody); another host (SSH) every fifth time only."""
        self.ticks += 1
        if on_screen(self) and (self.chosen_host() == LOCAL or self.ticks % 5 == 0):
            self.refresh_starts()

    def refresh_starts(self):
        if self.starts is None or self.reading is not None:
            return
        host = self.chosen_host()
        self.reading = background(self.sai.host_json, host, "list", "--json", timeout=90, owner=self,
                                  done=lambda result, error: self.starts_read(host, result, error))

    def starts_read(self, host, result, error):
        self.reading = None
        if host != self.chosen_host():
            return
        if error is not None or not isinstance(result, list):
            self.problem = printable(str(error) if error is not None else tr("not a list"))[:300]
        else:
            self.entries = grouped(host, [e for e in result if isinstance(e, dict) and isinstance(e.get("name"), str)
                                          and e.get("state") in ("running", "paused")])
            self.problem = ""
        self.show_starts()

    def attach_argv(self, host, name):
        """`sandbox-ai attach NAME` here, or on HOST over SSH (as the Hosts page attaches)."""
        if host == LOCAL:
            return self.sai.command("attach", name)
        return self.sai.host_terminal(host, "attach", name)

    def start_key(self, host, name):
        """attach_key of the start NAME on HOST; None when HOST is gone (removed on the Hosts page meanwhile)."""
        try:
            return attach_key(self.attach_argv(host, name))
        except (OSError, ValueError):
            return None

    def open_view(self, key):
        """The tab of the start KEY (attach_key) whose command still runs, or None."""
        for view in self.running():
            if key is not None and attach_key(view.argv) == key:
                return view
        return None

    def show_starts(self):
        if self.starts is None:
            return
        host = self.chosen_host()
        rows, colors, open_, tips = [], [], [], []
        for entry in self.entries:
            view = self.open_view(self.start_key(host, entry["name"])) if entry.get("attachable") else None
            owner = entry.get(OWNER)
            # What belongs to a start stands below it, indented; an MCP server of no start says so.
            name = printable(entry["name"])
            rows.append([f"    ↳ {name}" if owner else name,
                         printable(entry.get("agent") or entry.get("kind") or ""),
                         tr(STATES.get(entry.get("state"), "running")), printable(entry.get("workspace") or ""),
                         attach_why(entry) + (" · " + tr("belongs to no start") if owner == "" else "")])
            colors.append({"running": "ok", "paused": "warn"}.get(entry.get("state")))
            tips.append(tr("Belongs to {name}", name=printable(owner)) if owner else
                        tr(ORPHAN_TIP) if owner == "" else None)
            open_.append(view is not None)
        text = tr("{count} running on {host}", count=len(rows), host=host)
        if self.problem:
            text += " · " + tr("could not be read: {problem}", problem=self.problem)
        self.summary.setText(text)
        signature = (host, [tuple(r) for r in rows], open_, [bool(e.get("attachable")) for e in self.entries])
        if signature == self.shown_rows:
            return  # nothing changed: no flicker, the selection and the buttons stay
        self.shown_rows = signature
        for row in range(self.list.rowCount()):
            self.list.removeCellWidget(row, 4)
        fill(self.list, rows, colors, tooltips=tips, keys=[e["name"] for e in self.entries])
        for row, entry in enumerate(self.entries):
            if not entry.get("attachable"):
                continue
            button = QPushButton(tr("Show its tab") if open_[row] else tr("Open terminal"))
            button.setToolTip(safe_tooltip(tr("Attach to {name} in a terminal tab (detach with Ctrl-b d: it keeps running)",
                                 name=printable(entry["name"]))))
            button.clicked.connect(lambda _=False, name=entry["name"]: self.attach_start(name))
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(2, 0, 2, 0)
            cell_layout.addWidget(button)
            cell_layout.addStretch(1)
            self.list.setCellWidget(row, 4, cell)
        fit_columns(self.list)  # the buttons too

    def attach_row(self, row):
        name = row_key(self.list, row)
        entry = next((e for e in self.entries if e["name"] == name), None)
        if entry is not None and entry.get("attachable"):
            self.attach_start(name)

    def attach_start(self, name):
        """A tab attached to the start NAME of the chosen host: the one open already, else a new one (or a
        terminal window, when the app has no terminal tabs)."""
        host = self.chosen_host()
        try:
            argv = self.attach_argv(host, name)
        except (OSError, ValueError) as exc:
            warn(self, tr("Attach"), printable(str(exc)))
            return None
        view = self.open_view(attach_key(argv))
        if view is not None:
            self.tabs.setCurrentWidget(view)
            view.view.setFocus()
            return view
        title = name if host == LOCAL else f"{host}: {name}"
        if self.in_terminal is not None:
            return self.in_terminal(argv, title, embedded=True)
        return self.open(argv, title)

    # -- the terminals --

    def show_state(self, problem=""):
        ok, why = available(self.web)
        if ok:
            text = ""  # nothing wrong: nothing to say (the tour and the Help explain the page)
        elif MISSING:
            text = tr("Terminals inside the app are not available: {why}. Commands open in a terminal window "
                      "instead. Install QtWebEngine with: sandbox-ai-update --force (or pip install PySide6-Addons).",
                      why=why)
        else:
            text = tr("Terminals inside the app are not available: {why}. Commands open in a terminal "
                      "window instead.", why=why)
        if problem:
            text += ("\n" if text else "") + tr("Download failed: {error}", error=problem)
        if ok and self.sandbox_problem:
            text = self.sandbox_problem
        self.say(text)
        self.fetch.setVisible(not ok and not MISSING and VENDOR_SCRIPT.is_file())

    def say(self, text):
        """TEXT above the tabs; no text: no line (the space goes to the terminals)."""
        self.hint.setText(text)
        self.hint.setVisible(bool(text))
        self.fit_hint()

    def fit_hint(self):
        """The line above the tabs as high as its text wraps to at its width: a narrow window gets taller, the
        text is never cut (a word-wrapped label's own minimum is one line)."""
        self.hint.setMinimumHeight(self.hint.heightForWidth(self.hint.width()) if self.hint.text() else 0)

    def resizeEvent(self, event):  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self.fit_hint()

    def vendor(self):
        """Fetch xterm.js into this copy of the app, in the background."""
        from .widgets import AsyncCommand
        if self.command is not None and self.command.running:
            return
        self.command = AsyncCommand(timeout=180, parent=self)
        self.command.finished.connect(self.vendored)
        self.fetch.setEnabled(False)
        self.say(tr("Downloading xterm.js…"))
        self.command.start([sys.executable, "-I", str(VENDOR_SCRIPT), "--into", str(Path(self.web) / "vendor")])

    def vendored(self, status, out, err):
        self.fetch.setEnabled(True)
        lines = [line for line in (err or out).splitlines() if line.strip()]
        self.show_state("" if status == 0 else (lines[-1] if lines else tr("exit status {status}", status=status)))

    told = False  # the dialog about Chromium's sandbox: once per run of the app (the page's text stays)

    def chromium_ready(self, result):
        """Before a new web view: can Chromium's sandbox start now? RESULT: chromium.check() (a short-lived child
        process, run in a thread before every new tab, as namespaces come and go; logged). If not, no web view is made
        (Chromium would end the whole app): why and what to do are shown on this page, in the status bar and, the
        first time, in a message; the caller opens a terminal window instead."""
        if result.ok:
            if self.sandbox_problem:
                self.sandbox_problem = ""
                self.show_state()
            return True
        self.sandbox_problem = chromium.unavailable_text(result)
        self.show_state()
        window = self.window()
        if hasattr(window, "statusBar"):
            window.statusBar().showMessage(tr("A terminal window instead of a tab: Chromium's sandbox cannot start "
                                              "now (see the Terminals page)"), 20000)
        if not TerminalsPage.told:
            TerminalsPage.told = True
            inform(window, tr("A terminal window instead of a tab"), self.sandbox_problem, warning=True, modal=False)
        return False

    def open(self, argv, title, cwd=None, done=None):
        """A tab running ARGV, or the tab attached to that start already (returned, and DONE(it) at once). A new tab
        waits for Chromium's sandbox check (chromium.check, in a thread: no local event loop, the window goes on as
        always); DONE(view) then - None when Chromium's sandbox cannot start now (chromium_ready): the caller uses a
        terminal window. Returns None while it waits. The same command asked for again while it waits (a double
        click) opens one tab."""
        # One tab per start: attaching again (here, from Sandboxes & sessions or the Hosts page) shows its tab.
        view = self.open_view(attach_key(argv))
        if view is not None:
            self.tabs.setCurrentWidget(view)
            view.view.setFocus()
            if done is not None:
                done(view)
            return view
        if any(list(argv) == waiting[0] and title == waiting[1] for waiting in self.waiting):
            return None
        self.waiting.append((list(argv), title, cwd, done))
        if not self.checking:
            self.checking = True
            background(chromium.check, done=self.checked)
        return None

    def checked(self, result, error):
        """Chromium's sandbox check is back: the tabs asked for meanwhile are made (or, if it cannot start, none)."""
        self.checking = False
        waiting, self.waiting = self.waiting, []
        if error is not None:
            result = chromium.Probe(False, False, "error", f"the check failed: {error}")
        ready = self.chromium_ready(result)
        for argv, title, cwd, done in waiting:
            view = self.make_tab(argv, title, cwd) if ready else None
            if done is not None:
                done(view)

    def make_tab(self, argv, title, cwd=None):
        env = None
        start_host, start_name = self.start_of(argv)
        if start_name is None and detaches(argv, self.launcher()):
            # A start in this tab (not an attach): the start records the tag, by which the tab is found again
            env = {**os.environ, TAB_TAG: secrets.token_hex(8)}
        view = TerminalView(argv, self.web, cwd, self, env=env)
        view.start_host, view.start_name = start_host, start_name
        view.tab_title = title
        index = self.tabs.addTab(view, title)
        self.tabs.setTabToolTip(index, shlex.join(argv))
        view.exited.connect(lambda status, v=view: self._ended(v, status))
        self.tabs.setCurrentIndex(index)
        view.view.setFocus()
        self.show_starts()
        self.save_tabs()
        return view

    def _ended(self, view, status):
        index = self.tabs.indexOf(view)
        if index >= 0:
            self.tabs.setTabText(index, tr("{title} (ended)", title=self.tabs.tabText(index)))
        self.show_starts()
        self.save_tabs()

    # -- the tabs of running starts, kept for the next start of the app --

    def start_of(self, argv):
        """(host, start name) a terminal of ARGV attaches to: (LOCAL, NAME) for `sandbox-ai attach NAME` here, (HOST,
        NAME) over SSH; (HOST, None) for another command on a host; (None, None) for everything else."""
        key = attach_key(argv)
        if key is not None and key[0] == "here":
            return LOCAL, key[1]
        argv = [str(arg) for arg in argv]
        if not argv or os.path.basename(argv[0]) != "ssh":
            return None, None
        host = self.host_of(argv)
        return host, (attached_name(remote_words(argv[-1])) if host else None)

    def host_of(self, argv):
        """The host of sandbox-ai hosts an ssh ARGV goes to (as host_terminal makes it), or None."""
        try:
            names = [e["name"] for e in self.sai.host_entries() if e.get("kind") != LOCAL] \
                if self.sai is not None and self.sai.hosts_supported() else []
        except Exception:  # noqa: BLE001 - no hosts file: no host
            return None
        for name in names:
            try:
                prefix = list(self.sai.host_terminal(name, "list"))[:-1]
            except Exception:  # noqa: BLE001
                continue
            if prefix and argv[:len(prefix)] == prefix:
                return name
        return None

    def remembered(self):
        """The tabs of starts that run on when the app ends (sessions in tmux, attached starts), in their order:
        [{"host", "name", "id", "tag", "title"}] - what reopen() attaches again."""
        launcher, found = self.launcher(), []
        for view in self.views():
            if not view.running or not detaches(view.argv, launcher):
                continue
            host, name, tag = view.start_host, view.start_name, view.tab_tag
            if host is None and tag is None:
                continue  # (a start on another host whose host is not one of sandbox-ai hosts)
            listed = next((e for e in self.entries if (tag and e.get("app_tab") == tag) or
                           (name and e.get("name") == name and (host or LOCAL) == self.chosen_host())), None) \
                if self.starts is not None else None
            entry = {"host": host or LOCAL, "name": name or (listed or {}).get("name"),
                     "id": (listed or {}).get("id"), "tag": tag, "title": self.tabs.tabText(self.tabs.indexOf(view))}
            found.append({k: v for k, v in entry.items() if isinstance(v, str) and v and len(v) <= 200})
        return found[:SAVED_LIMIT]

    def save_tabs(self):
        """The tabs of running starts into the app's state (not while they are being reopened)."""
        if self.store is not None and not self.reopening:
            self.store.set(SAVED_TABS, self.remembered())

    def reopen(self, everything=None, done=None):
        """At the app's start: a tab attached again to each start whose tab was open when the app ended, in that
        order - and with EVERYTHING (the setting reopen_terminals; default: it) also to every other start in tmux
        that runs on this machine and on the hosts whose terminals were open. Never starts anything: only what runs
        and can be attached; a start that ended meanwhile is left out and said. DONE(opened, skipped) at the end."""
        if self.store is None or self.sai is None or not available(self.web)[0]:
            return False
        saved = [e for e in self.store.get(SAVED_TABS, []) or [] if isinstance(e, dict)
                 and isinstance(e.get("host"), str)][:SAVED_LIMIT]
        everything = backend_setting("reopen_terminals") if everything is None else everything
        hosts = list(dict.fromkeys(([LOCAL] if everything else []) + [e["host"] for e in saved]))
        if not hosts:
            return False
        self.reopening = True
        listed = {}

        def one(result, error, host):
            listed[host] = result if error is None and isinstance(result, list) else None
            if len(listed) == len(hosts):
                self.reopen_listed(saved, listed, everything, done)
        for host in hosts:
            background(self.sai.host_json, host, "list", "--json", owner=self, timeout=90,
                       done=lambda result, error, host=host: one(result, error, host))
        return True

    def reopen_listed(self, saved, listed, everything, done=None):
        """reopen(), once every host's starts were read (LISTED: {host: entries of list --json, None: not read})."""
        wanted, skipped, seen = [], [], set()
        runs = {host: [e for e in grouped(host, [e for e in entries if isinstance(e, dict)
                                                 and isinstance(e.get("name"), str)])
                       if e.get("state") == "running" and e.get("attachable")]
                for host, entries in listed.items() if entries is not None}
        for entry in saved:
            found = None
            for start in runs.get(entry["host"], []):
                if (entry.get("tag") and start.get("app_tab") == entry["tag"]) or \
                        (entry.get("id") and start.get("id") == entry["id"]) or \
                        (entry.get("name") and start["name"] == entry["name"]):
                    found = start
                    break
            if found is None:
                if entry["host"] in runs:  # (a host that did not answer: nothing is said of its starts)
                    skipped.append(entry.get("name") or entry.get("title") or "?")
                continue
            if (entry["host"], found["name"]) not in seen:
                seen.add((entry["host"], found["name"]))
                wanted.append((entry["host"], found["name"]))
        if everything:
            for host, starts in runs.items():
                for start in starts:
                    # One tab per start: its MCP servers, helpers and a team's members are reached from it.
                    if OWNER not in start and (host, start["name"]) not in seen:
                        seen.add((host, start["name"]))
                        wanted.append((host, start["name"]))
        for host, name in wanted:
            try:
                argv = self.attach_argv(host, name)
            except (OSError, ValueError):
                continue
            self.open(argv, name if host == LOCAL else f"{host}: {name}")
        self.reopening = False
        if skipped:
            from . import applog
            text = tr("Not reopened (ended meanwhile): {names}", names=", ".join(printable(n) for n in skipped))
            applog.log("INFO", "terminal", text)
            self.say(text)
        if wanted:
            QTimer.singleShot(0, self.save_tabs)
        if done is not None:
            done([name for _, name in wanted], skipped)

    def views(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def running(self):
        return [view for view in self.views() if view.running]

    def launcher(self):
        try:
            return self.sai.launcher if self.sai is not None else None
        except Exception:  # noqa: BLE001 - without it the command line alone decides
            return None

    def ending(self):
        """The titles of the tabs whose command still runs and ends with the app (not a session or an attached
        start: closing those only detaches, the sandbox runs on)."""
        launcher = self.launcher()
        return [self.tabs.tabText(self.tabs.indexOf(view)) for view in self.running()
                if not detaches(view.argv, launcher)]

    def detaching(self):
        """The running tabs that only detach when the app quits (sessions, attached starts)."""
        launcher = self.launcher()
        return [view for view in self.running() if detaches(view.argv, launcher)]

    def close_tab(self, index, ask=True):
        view = self.tabs.widget(index)
        if view is None:
            return
        # Ending a command that runs loses what it did not save: asked, with Cancel as the default.
        if ask and view.running and not ask_first(self, "terminal-end", tr("Close terminal"),
                                                  tr("The command in this tab still runs. End it?"), tr("End it")):
            return
        view.close_terminal()
        self.tabs.removeTab(index)
        view.deleteLater()
        self.show_starts()
        self.save_tabs()

    def close_all(self):
        for index in reversed(range(self.tabs.count())):
            self.close_tab(index, ask=False)
