"""Terminal tabs inside the app: xterm.js in QtWebEngine, connected to a pseudo-terminal (pty_bridge).

The page and its scripts are local files (sandbox_ai_app/web, xterm.js vendored by scripts/vendor_xterm.py);
the view loads nothing else: a request interceptor blocks every URL except the files below
sandbox_ai_app/web and Qt's qwebchannel.js, navigation away from the page is refused, local content may not
reach remote URLs, and the profile keeps nothing on disk. Without
QtWebEngine or the vendored files the app uses a terminal window instead (SandboxAI.in_terminal), and so it does for
a new terminal while Chromium's sandbox cannot get its namespaces (chromium.py: Chromium would end the whole app).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import sys
import weakref

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, QUrl, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from . import chromium, theme
from .backend import setting as backend_setting
from .files import printable
from .i18n import N_, tr
from .pty_bridge import PtyBridge
from .widgets import ask_first, background, fill, fit_columns, inform, plain_label, row_key, safe_tooltip, table, warn

WEB = Path(__file__).resolve().with_name("web")
# The vendoring script next to the package (a checkout or sandbox-ai's installation; not in a wheel).
VENDOR_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "vendor_xterm.py"
VENDOR_FILES = ("xterm.js", "xterm.css", "addon-fit.js")
_ending = set()  # bridges of closed tabs whose command has not ended yet (kept until it has)
_views = weakref.WeakSet()
CHANNEL_SCRIPT = "qrc:///qtwebchannel/qwebchannel.js"
LOCAL = "local"  # this machine, as sandbox-ai hosts names it

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


class TerminalView(QWidget):
    """One terminal: the web view and the command's pseudo-terminal."""
    exited = Signal(int)

    def __init__(self, argv, web=WEB, cwd=None, parent=None):
        super().__init__(parent)
        self.argv = list(argv)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        page_url = QUrl.fromLocalFile(str(Path(web) / "terminal.html"))
        self.bridge = PtyBridge(self.argv, cwd=cwd, theme=theme.terminal_theme(),
                                exit_text=tr("[sandbox-ai: exit status {status}; close the tab with ×]"), parent=self)
        self.bridge.exited.connect(self.exited)
        self.view = QWebEngineView(self)
        self.page = TerminalPage(profile(web), page_url, self.view)
        lock_down(self.page.settings())
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("pty", self.bridge)
        self.page.setWebChannel(self.channel)
        self.view.setPage(self.page)
        layout.addWidget(self.view)
        self.page.load(page_url)
        _views.add(self)

    @property
    def running(self):
        return self.bridge.running

    def run_js(self, script, callback):
        self.page.runJavaScript(script, 0, callback)

    def close_terminal(self):
        """End the command; the bridge outlives the tab until the command is gone (SIGKILL after a while)."""
        bridge = self.bridge
        if bridge.running:
            _ending.add(bridge)
            bridge.setParent(None)
            bridge.exited.connect(lambda _status, b=bridge: _ending.discard(b))
        bridge.close()


def shutdown():
    """End every terminal and release QtWebEngine in the order it needs, while the application still runs: the pages
    first, then the web views, then the profiles (else Chromium aborts at exit, or Python's own cleanup at the end of
    the interpreter deletes a web view whose QtWebEngine is gone: "shared QObject was deleted directly", a crash).
    Called when the application quits; a second call does nothing."""
    from shiboken6 import isValid
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


def attach_key(argv):
    """What a terminal's command line stands for when it attaches to a start (one tab per start):
    `sandbox-ai [session] attach NAME` on this machine, or the same over SSH; None for every other command."""
    argv = [str(arg) for arg in argv]
    if len(argv) >= 2 and argv[-2] == "attach":
        base = argv[:-2]
        if base and base[-1] == "session":
            base = base[:-1]
        if base and (base[-1].endswith("main.py") or os.path.basename(base[-1]) in ("sandbox-ai", "sandbox-session")):
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


def detaches(argv, launcher=None):
    """Whether closing the terminal of ARGV only detaches it, the start running on: attaching to a start (tmux),
    a start with --session, or an interactive agent start, which the launcher runs as a detachable session in tmux
    by default (not with --no-tmux, --probe, --dry-run, --binary, a prompt or subcommand for the agent, or on macOS;
    sandbox-ai's detachable() and resolve_tmux()). Everything else (a build, doctor, a task) ends with its tab."""
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
    if names & {"--no-tmux", "--binary"} or sys.platform == "darwin":
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
    """Tabs with terminals, and above them every start that runs on this machine (or a host of `sandbox-ai
    hosts`), however it was started: the command line, the app, the web interface, another machine. One click
    attaches a start in tmux (a tab per start: clicked again, its tab is shown); the others say why they have
    no terminal to attach. The list is read when the page is shown and every few seconds while it is."""

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
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.entries, self.problem, self.reading, self.shown_rows, self.ticks = [], "", None, None, 0
        self.starts = self.make_starts() if sai is not None else None
        if self.starts is None:
            layout.addWidget(self.tabs, 1)
        else:
            split = QSplitter(Qt.Orientation.Vertical)
            split.setChildrenCollapsible(False)
            split.addWidget(self.starts)
            split.addWidget(self.tabs)
            split.setStretchFactor(1, 3)
            layout.addWidget(split, 1)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(backend_setting("sandboxes_interval"))

    # -- the running starts --

    def make_starts(self):
        box = QGroupBox(tr("Running starts"))
        box_layout = QVBoxLayout(box)
        top = QHBoxLayout()
        self.host = QComboBox()
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
        self.list.setAccessibleName(tr("Running starts"))
        self.list.cellDoubleClicked.connect(lambda row, _column: self.attach_row(row))
        box_layout.addWidget(self.list, 1)
        return box

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
        if self.starts is not None and not event.spontaneous():
            self.load_hosts()
            self.refresh_starts()

    def tick(self):
        """The timer: while the page is shown; another host (SSH) every fifth time only."""
        self.ticks += 1
        if self.isVisible() and (self.chosen_host() == LOCAL or self.ticks % 5 == 0):
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
            self.entries = [e for e in result if isinstance(e, dict) and isinstance(e.get("name"), str)
                            and e.get("state") in ("running", "paused")]
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
        rows, colors, open_ = [], [], []
        for entry in self.entries:
            view = self.open_view(self.start_key(host, entry["name"])) if entry.get("attachable") else None
            rows.append([printable(entry["name"]), printable(entry.get("agent") or entry.get("kind") or ""),
                         tr(STATES.get(entry.get("state"), "running")), printable(entry.get("workspace") or ""),
                         attach_why(entry)])
            colors.append({"running": "ok", "paused": "warn"}.get(entry.get("state")))
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
        fill(self.list, rows, colors, keys=[e["name"] for e in self.entries])
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
            text = tr("Every terminal of the app opens here as a tab: starts in the app, sessions you attach, "
                      "builds and logins. Any start that runs in tmux, wherever it was started, can be attached "
                      "from the list.")
        elif MISSING:
            text = tr("Terminals inside the app are not available: {why}. Commands open in a terminal window "
                      "instead. Install QtWebEngine with: sandbox-ai-update --force (or pip install PySide6-Addons).",
                      why=why)
        else:
            text = tr("Terminals inside the app are not available: {why}. Commands open in a terminal "
                      "window instead.", why=why)
        if problem:
            text += "\n" + tr("Download failed: {error}", error=problem)
        if ok and self.sandbox_problem:
            text = self.sandbox_problem
        self.hint.setText(text)
        self.fetch.setVisible(not ok and not MISSING and VENDOR_SCRIPT.is_file())

    def vendor(self):
        """Fetch xterm.js into this copy of the app, in the background."""
        from .widgets import AsyncCommand
        if self.command is not None and self.command.running:
            return
        self.command = AsyncCommand(timeout=180, parent=self)
        self.command.finished.connect(self.vendored)
        self.fetch.setEnabled(False)
        self.hint.setText(tr("Downloading xterm.js…"))
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
        view = TerminalView(argv, self.web, cwd, self)
        index = self.tabs.addTab(view, title)
        self.tabs.setTabToolTip(index, shlex.join(argv))
        view.exited.connect(lambda status, v=view: self._ended(v, status))
        self.tabs.setCurrentIndex(index)
        view.view.setFocus()
        self.show_starts()
        return view

    def _ended(self, view, status):
        index = self.tabs.indexOf(view)
        if index >= 0:
            self.tabs.setTabText(index, tr("{title} (ended)", title=self.tabs.tabText(index)))
        self.show_starts()

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

    def close_all(self):
        for index in reversed(range(self.tabs.count())):
            self.close_tab(index, ask=False)
