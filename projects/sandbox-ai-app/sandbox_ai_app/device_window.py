"""A device sign-in's one-time code, large, beside the page that opened in the system browser.

Codex's sign-in in a sandbox is its device sign-in: the terminal prints a page (https://auth.openai.com/codex/device)
and a one-time code, and waits until the code was entered there. When a terminal tab of the app shows one - `sandbox-ai
login codex`, or a Codex start in a sandbox that has no login - the page opens by itself in the system browser (the
setting device_signin; never for a run behind Tor or a VPN) and this small window shows what is to be entered there:
the code, large, with Copy; the page's address with Open again and Copy link. The code comes from the terminal - of
another host too -, the browser is this computer's. When the terminal's sign-in ends well the window closes by
itself; the code is never written to the log.
"""
from __future__ import annotations

import weakref

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout

from . import applog, terminal_text as T
from .files import printable
from .i18n import tr
from .terminal_tools import put, system_browser
from .widgets import plain_label, safe_tooltip  # (plain_label: plain text, never read as markup)

AGENT_TITLES = {"codex": "Codex", "hermes": "Hermes", "qwen": "Qwen Code", "claude": "Claude Code",
                "opencode": "OpenCode", "pi": "Pi"}
EXPIRES_MS = 15 * 60 * 1000  # a device code holds for 15 minutes (Codex says so): the window goes with it
_windows = []  # weak references to the windows shown


def agent_title(agent):
    return AGENT_TITLES.get(agent, printable(agent or "") or tr("the agent"))


class DeviceCodeWindow(QDialog):
    """The window (see the module's docstring). URL and CODE are what the terminal TARGET printed; WHERE names the
    host whose terminal it is (None: this machine); OPENED: the page was opened in the system browser already;
    REFUSED: why it was not (a run behind Tor or a VPN), said instead."""

    def __init__(self, url, code, agent=None, where=None, opened=False, refused="", target=None, parent=None,
                 opener=None):
        super().__init__(parent)
        self.url, self.code, self.agent = str(url), code, agent
        self.target = weakref.ref(target) if target is not None else (lambda: None)
        self.opener = opener or system_browser
        self.setObjectName("deviceCodeWindow")
        self.setWindowTitle(tr("Sign in to {agent}", agent=agent_title(agent)))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        layout = QVBoxLayout(self)
        self.lead = plain_label(self.lead_text(where, opened, refused))
        layout.addWidget(self.lead)
        self.code_label = plain_label("", word_wrap=False, selectable=True)
        self.code_label.setObjectName("deviceCode")
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_label.setAccessibleName(tr("One-time code"))
        font = self.code_label.font()
        font.setPointSizeF(max(font.pointSizeF(), 10.0) * 2.4)
        font.setBold(True)
        font.setFamily("monospace")
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 2.0)
        self.code_label.setFont(font)
        layout.addWidget(self.code_label)
        row = QHBoxLayout()
        self.copy_button = QPushButton(tr("Copy the code"))
        self.copy_button.setProperty("role", "primary")
        self.copy_button.setToolTip(safe_tooltip(tr("Puts the one-time code on the clipboard, to paste it into the "
                                                    "page in your browser")))
        self.copy_button.clicked.connect(lambda _=False: self.copy_code())
        row.addStretch(1)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.url_label = plain_label(T.shown_url(self.url, 200), selectable=True, role="secondary")
        self.url_label.setAccessibleName(tr("Sign-in page"))
        self.url_label.setToolTip(safe_tooltip(T.shown_url(self.url, 400)))
        layout.addWidget(self.url_label)
        # (a warning, always shown: device codes are a common phishing target)
        self.note = plain_label(tr("Continue only if you started this sign-in yourself. Never enter a code that a "
                                   "website or another person gave you."), role="notice-warn")
        layout.addWidget(self.note)
        self.state = plain_label("", role="secondary")
        self.state.hide()
        layout.addWidget(self.state)
        buttons = QHBoxLayout()
        self.open_button = QPushButton(tr("Open again") if opened else tr("Open in the system browser"))
        self.open_button.setToolTip(safe_tooltip(tr("Opens the sign-in page in this computer's browser")))
        self.open_button.clicked.connect(lambda _=False: self.open_again())
        self.link_button = QPushButton(tr("Copy link"))
        self.link_button.setToolTip(safe_tooltip(tr("Puts the sign-in page's address on the clipboard")))
        self.link_button.clicked.connect(lambda _=False: self.copy_link())
        self.close_button = QPushButton(tr("Close"))
        self.close_button.setToolTip(safe_tooltip(tr("Closes this window; the sign-in in the terminal goes on")))
        self.close_button.clicked.connect(lambda _=False: self.close())
        for button in (self.open_button, self.link_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        if refused:
            self.open_button.setEnabled(False)
            self.open_button.setToolTip(safe_tooltip(refused))
        self.expiry = QTimer(self)
        self.expiry.setSingleShot(True)
        self.expiry.timeout.connect(self.expired)
        self.expiry.start(EXPIRES_MS)
        self.show_code(code)
        _windows.append(weakref.ref(self))

    def lead_text(self, where, opened, refused):
        agent = agent_title(self.agent)
        if refused:
            first = refused
        elif opened:
            first = tr("The sign-in page of {agent} opened in your browser. Enter this one-time code there:",
                       agent=agent)
        else:
            first = tr("Open the sign-in page of {agent} in a browser and enter this one-time code there:",
                       agent=agent)
        if where:
            first += "\n" + tr("The code comes from the terminal on {host}; the browser is this computer's.",
                               host=printable(where))
        return first

    def show_code(self, code):
        """The one-time CODE (None: the terminal has not shown one that could be read: it is to be taken from there)."""
        self.code = code
        self.code_label.setText(printable(code) if code else tr("(see the terminal)"))
        self.copy_button.setEnabled(bool(code))

    def copy_code(self):
        if self.code:
            put(self.code)
            self.say(tr("Copied: paste it into the page in your browser."))

    def copy_link(self):
        put(self.url)
        self.say(tr("The link is on the clipboard."))

    def open_again(self):
        if T.plausible(self.url):
            self.opener(self.url)
            self.open_button.setText(tr("Open again"))
            self.say(tr("Opened in your browser."))

    def say(self, text):
        self.state.setText(str(text))
        self.state.show()

    def expired(self):
        self.say(tr("The code has expired (15 minutes): start the sign-in again."))
        self.copy_button.setEnabled(False)

    def login_ended(self, status):
        """The terminal's sign-in ended: well (0) - the window has done its part and closes; else it says so."""
        applog.log("INFO", "sign-in", f"the device sign-in in the terminal ended: exit status {status}")
        if status == 0:
            self.close()
        else:
            self.say(tr("The login in the terminal ended (exit status {status}).", status=status))
            self.copy_button.setEnabled(False)


def windows():
    """The device sign-in windows that are there."""
    from shiboken6 import isValid
    alive = []
    for reference in list(_windows):
        window = reference()
        if window is None or not isValid(window):
            _windows.remove(reference)
        else:
            alive.append(window)
    return alive


def window_for(target):
    """The window of the terminal TARGET's sign-in, or None."""
    for window in windows():
        if window.target() is target:
            return window
    return None


def show(target, url, code, agent=None, where=None, opened=False, refused="", parent=None, opener=None):
    """The window for the device sign-in the terminal TARGET shows (one per terminal: an open one is updated)."""
    window = window_for(target)
    if window is not None and window.url == str(url):
        window.show_code(code)
    else:
        if window is not None:
            window.close()
        window = DeviceCodeWindow(url, code, agent, where, opened, refused, target, parent, opener)
    # (the code is never logged; that a sign-in is shown, and where its page goes, is)
    applog.log("INFO", "sign-in", f"device sign-in of {agent or '?'}: its page {T.without_query(str(url))} "
               + ("opened in the system browser" if opened else "not opened here") + "; its one-time code is shown "
               "in a window" + (f" (the terminal is on {printable(where)})" if where else ""))
    window.show()
    window.raise_()
    return window


def close_all():
    for window in windows():
        window.close()
