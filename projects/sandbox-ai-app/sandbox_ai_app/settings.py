"""The Settings dialog: every setting of the app in one place, and the administrator's policy (read-only).

Settings are kept as before in ~/.config/sandbox-ai/app/settings.json (mode 0600), one key each
(backend.SETTINGS lists them with their defaults); the dialog writes a key when its field changes, and the
window applies what can change at once (theme, polling intervals, notifications). The language and the
sandbox-ai location apply at the next start. The policy tab shows /etc/sandbox-ai/policy.json as it is, why
it stops every start if it does, and what it forbids; the app greys those options out everywhere (the Start
page's form, the board, the wizard).

The Notifications tab holds the phone channels of this machine or a host (sandbox-ai notify: notify_view.py).

The sandbox-ai tab also lists the extra preset directories (sandbox-ai presets dirs) and the trusted project
configurations (sandbox-ai wizard trust --list; Forget… runs `sandbox-ai wizard forget PATH`, asked first:
project-forget; Review and trust… shows a project file of that machine with every key, what weakens marked and the
changes against the version trusted last, and trusts exactly that after a yes: host_trust), each of this machine or of
a host of `sandbox-ai hosts` (over SSH).
"""
from __future__ import annotations

import os

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStyle, QStyleOptionButton, QStylePainter,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import (
    backend, fonts, help_texts, help_view, intro_choice, notifications, presets as PR, project_trust as PT, reading_fonts,
    theme,
)
from .board_view import PathEdit
from .files import printable, printable_text
from .i18n import tr
from .short_notices import short_notice
from .widgets import (
    MONO, arrow_tabs, ask_first, background, explanation, outcome, plain_label, safe_tooltip, set_explanations_shown,
    tame_combos, warn,
)

LOCAL = "local"
SIZE = QSize(760, 600)  # the dialog's size at first, when the screen has the room
LOWEST = 420  # the lowest it may become (each tab scrolls)


def screen_room(widget):
    """The room of the screen WIDGET is on (tests: a small laptop's)."""
    screen = widget.screen() or QGuiApplication.primaryScreen()
    return screen.availableGeometry().size() if screen is not None else QSize(1366, 728)


def breakable(path):
    """PATH (shown in a wrapping label) that may break after each "/": a long path does not widen the dialog."""
    return str(path).replace("/", "/\u200b")


class WrapCheckBox(QCheckBox):
    """A check box whose text wraps to the room it has (a QCheckBox keeps its text on one line and makes the dialog
    as wide as that line). Its text, accessible name and click stay those of a check box: the whole box toggles."""
    wraps = True  # tests/test_layout.py: measured as a wrapping text
    FLAGS = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap

    def __init__(self, text="", parent=None, about=60):
        super().__init__(text, parent)
        self.about = about  # characters on one line at most when there is room
        self.laid_width = 0  # the width it was given last: its height is the one for that width (resizeEvent)
        self.width_before = 0  # the width it had before that one
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # (its height: for its width)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def option(self):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        return option

    def text_width(self, width):
        """The room for the text in a box WIDTH wide (what the indicator and the spacing leave)."""
        lead = super().sizeHint().width() - self.fontMetrics().horizontalAdvance(self.text())
        return max(1, width - lead)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        line = super().sizeHint().height()
        rect = self.fontMetrics().boundingRect(QRect(0, 0, self.text_width(width), 100000), int(self.FLAGS),
                                               self.text())
        return max(line, line - self.fontMetrics().height() + rect.height())

    def sizeHint(self):  # noqa: N802
        whole = super().sizeHint().width()
        width = min(whole, whole - self.fontMetrics().horizontalAdvance(self.text())
                    + self.fontMetrics().averageCharWidth() * self.about)
        return QSize(width, self.heightForWidth(self.laid_width or width))  # (a form asks this, not heightForWidth)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        width = event.size().width()
        if width != self.laid_width:
            before = self.heightForWidth(self.laid_width) if self.laid_width else None
            # Back to the width it had before the last one: its tab's scroll bar comes and goes (its hint, taller at
            # the narrower width, asked for the bar; shorter at the wider one, it let it go - without end, at some
            # widths, until the app crashed: the settings 818 px wide with a large font). It keeps the narrower
            # width's height then, which the bar's room holds either way.
            flipping = width == self.width_before
            self.width_before = self.laid_width
            self.laid_width = min(width, self.width_before) if flipping else width
            if self.heightForWidth(self.laid_width) != before:
                self.updateGeometry()  # the lines it needs at this width

    def minimumSizeHint(self):  # noqa: N802
        words = self.text().split() or [""]
        widest = max(self.fontMetrics().horizontalAdvance(word) for word in words)
        lead = super().sizeHint().width() - self.fontMetrics().horizontalAdvance(self.text())
        width = lead + max(widest, self.fontMetrics().averageCharWidth() * 12)
        return QSize(width, super().minimumSizeHint().height())  # its height for a width: heightForWidth

    def hitButton(self, pos):  # noqa: N802
        return self.rect().contains(pos)

    def paintEvent(self, _event):  # noqa: N802
        painter = QStylePainter(self)
        option = self.option()
        text, option.text = option.text, ""
        painter.drawControl(QStyle.ControlElement.CE_CheckBox, option)  # the indicator (and the focus)
        option.text = text
        room = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxContents, option, self)
        self.style().drawItemText(painter, room, int(self.FLAGS), option.palette, self.isEnabled(), text,
                                  QPalette.ColorRole.WindowText)


def host_row(sai, owner):
    """A host chooser for a box of the dialog (this machine, or a host of sandbox-ai hosts); OWNER.refresh() when
    another is chosen."""
    from .host_choice import HostChooser
    chooser = HostChooser(sai)
    chooser.changed.connect(lambda _host: owner.refresh())
    return chooser


class PresetDirs(QWidget):
    """The extra preset directories (sandbox-ai presets dirs): listed in order with what each gives or why it is
    skipped; Add folder… and Remove run the command, whose checks apply (never a folder sandboxes can write).
    The preset gallery shows the presets found there."""
    changed = Signal()

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        self.sai, self.entries, self.token = sai, [], 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.hosts = host_row(sai, self)
        layout.addWidget(self.hosts)
        self.list = QListWidget()
        self.list.setMaximumHeight(120)
        self.list.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.add_button = QPushButton(tr("Add folder…"))
        self.add_button.clicked.connect(lambda _=False: self.add())
        self.remove_button = QPushButton(tr("Remove"))
        self.remove_button.clicked.connect(lambda _=False: self.remove())
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch()
        layout.addLayout(row)
        from .widgets import list_menu
        list_menu(self.list, lambda: [self.remove_button, None, self.add_button])
        self.status = plain_label(role="secondary", selectable=True, log="settings")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        if not sai.has_preset_dirs():
            self.status.setText(tr("This sandbox-ai has no extra preset directories yet; update it."))
            self.add_button.setEnabled(False)
            self.remove_button.setEnabled(False)
        else:
            self.refresh()

    def host(self):
        return self.hosts.host() or LOCAL

    def refresh(self):
        host = self.host()
        self.token += 1
        token = self.token
        self.list.clear()
        if host == LOCAL:
            background(self.sai.preset_dirs, owner=self, source=[self.add_button, self.remove_button], timeout=90,
                       done=lambda result, error: self.loaded(result, error, token))
        else:
            self.status.setText(tr("Asking {host}…", host=self.hosts.label(host)))
            background(lambda: self.sai.host_preset_dirs(host), owner=self, source=[self.add_button,
                                                                                   self.remove_button],
                       timeout=120, done=lambda result, error: self.loaded(result, error, token))

    def loaded(self, result, error, token=None):
        if token is not None and token != self.token:
            return
        self.list.clear()
        if error is not None:
            self.status.setText(tr("Cannot list the preset directories: {error}", error=printable_text(str(error))))
            self.entries = []
            self.update_buttons()
            return
        self.entries = [e for e in result.get("dirs", []) if isinstance(e, dict) and isinstance(e.get("path"), str)]
        for entry in self.entries:
            text = (tr("{path}  ({count} presets)", path=printable(entry["path"]), count=entry.get("presets", 0))
                    if entry.get("state") == "used" else
                    tr("{path}  (skipped: {why})", path=printable(entry["path"]), why=printable(entry.get("why") or "")))
            item = QListWidgetItem(text)
            item.setData(256, entry["path"])
            self.list.addItem(item)
        self.status.setText(tr("Presets are read from your folder {yours} first, then from these in their order, then "
                               "the built-in ones. A folder sandboxes can write is refused.",
                               yours=breakable(printable(str(result.get("yours") or "")))))
        self.update_buttons()

    def update_buttons(self):
        if not self.sai.has_preset_dirs():
            return
        self.add_button.setEnabled(True)
        self.remove_button.setEnabled(bool(self.list.selectedItems()))

    def add(self):
        from . import remote_chooser as RC  # (another host's: its folders, listed over SSH)
        path = RC.choose_folder(self, self.host(), tr("Folder with presets (*.json)"), "presets", sai=self.sai)
        if path:
            self.add_path(path)

    def add_path(self, path):
        """Add PATH to the preset directories of the host chosen, asked first as the web interface asks it
        (defaults-lower): every later start there reads presets from it too."""
        host = self.host()
        listed = [entry["path"] for entry in self.entries]
        where = tr("this computer") if host == LOCAL else printable(host)
        if not ask_first(self, "defaults-lower", tr("Preset directories"),
                         tr("Add {path} to the preset directories of {host}? Every later start there reads presets "
                            "from it too: whoever can write that folder chooses what a preset there sets.",
                            path=printable(path), host=where), tr("Add"),
                         detail={"old": {"preset_dirs": listed}, "new": {"preset_dirs": listed + [path]}}):
            return False
        self.run("add", path)
        return True

    def remove(self):
        items = self.list.selectedItems()
        if items:
            self.run("remove", items[0].data(256))

    def run(self, action, path):
        host = self.host()
        PR.HOST_DOCUMENTS.pop(host, None)  # the presets that host offers change: read again where they are shown
        if host == LOCAL:
            background(lambda: self.sai.preset_dir_command(action, path), owner=self, work=True,
                       source=[self.add_button, self.remove_button], timeout=90,
                       done=lambda result, error: self.done(result, error))
        else:
            background(lambda: self.sai.host_preset_dir_command(host, action, path), owner=self, work=True,
                       source=[self.add_button, self.remove_button], timeout=120,
                       done=lambda result, error: self.done(result, error))

    def done(self, result, error):
        status, out, err = outcome(result, error)
        text = printable_text((out + err).strip())[-1500:]  # (what sandbox-ai there wrote: escaped, bounded)
        if status != 0:
            warn(self, tr("Preset directories"), text or tr("Failed (exit status {status}).", status=status))
        elif text:
            short_notice(self, tr("Preset directories"), text)
        self.changed.emit()
        self.refresh()


class TrustedProjects(QWidget):
    """The trusted project configurations of this machine or a host (`sandbox-ai wizard trust --list --json`):
    each with its state (trusted, changed since, missing, unreadable); Forget… takes one off the list (`sandbox-ai
    wizard forget PATH`, asked first: its project's starts stop until it is trusted again); Review and trust…
    reviews the selected one, or a project folder typed in, on that host (host_trust.review_and_trust)."""
    changed = Signal()
    STATES = {"trusted": "ok", "changed": "warn", "missing": "muted", "unreadable": "bad"}

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        self.sai, self.entries, self.token = sai, [], 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.hosts = host_row(sai, self)
        layout.addWidget(self.hosts)
        self.list = QListWidget()
        self.list.setMaximumHeight(140)
        self.list.setAccessibleName(tr("Trusted project configurations"))
        self.list.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.forget_button = QPushButton(tr("Forget…"))
        self.forget_button.setToolTip(tr("sandbox-ai wizard forget: the configuration is no longer trusted; its "
                                         "project's starts stop until it is trusted again"))
        self.forget_button.clicked.connect(lambda _=False: self.forget())
        self.review_button = QPushButton(tr("Review and trust…"))
        self.review_button.setToolTip(tr("Show a project's .sandbox-ai.json on that host (the selected one, or a "
                                         "folder you name) with every key and the changes against the version "
                                         "trusted last, and trust exactly that content"))
        self.review_button.clicked.connect(lambda _=False: self.review())
        row.addWidget(self.refresh_button)
        row.addWidget(self.review_button)
        row.addWidget(self.forget_button)
        row.addStretch()
        layout.addLayout(row)
        from .widgets import list_menu
        list_menu(self.list, lambda: [self.review_button, self.forget_button, None, self.refresh_button])
        self.status = plain_label(role="secondary", selectable=True, log="settings")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.update_buttons()
        self.refresh()

    def host(self):
        return self.hosts.host() or LOCAL

    def refresh(self):
        host = self.host()
        self.token += 1
        token = self.token
        self.list.clear()
        self.entries = []
        self.status.setText(tr("Asking {host}…", host=self.hosts.label(host)))
        background(lambda: self.sai.trusted_projects(host), owner=self, source=[self.refresh_button,
                                                                                self.forget_button,
                                                                                self.review_button],
                   timeout=90, done=lambda result, error: self.loaded(token, host, result, error))

    def loaded(self, token, host, result, error):
        if token != self.token:
            return
        self.list.clear()
        if error is not None:
            self.status.setText(tr("Cannot list the trusted configurations: {error}", error=printable_text(str(error))[:400]))
            self.entries = []
            self.update_buttons()
            return
        self.entries = [e for e in result if backend.valid_directory(e.get("path"))]
        states = {"trusted": tr("trusted"), "changed": tr("changed since it was trusted"), "missing": tr("missing"),
                  "unreadable": tr("unreadable")}
        for entry in self.entries:
            state = entry.get("state")
            item = QListWidgetItem(f"{printable(entry['path'])}  ({states.get(state, printable(state or '?'))})")
            item.setData(256, entry["path"])
            item.setIcon(theme.dot(self.STATES.get(state, "muted")))
            self.list.addItem(item)
        self.status.setText(tr("{count} trusted on {host}: a start of such a project gets the options of its "
                               ".sandbox-ai.json.", count=len(self.entries), host=self.hosts.label(host))
                            if self.entries else tr("No trusted project configuration on {host}.",
                                                    host=self.hosts.label(host)))
        self.update_buttons()

    def update_buttons(self):
        self.forget_button.setEnabled(bool(self.list.selectedItems()))

    def review(self, path=None):
        """Review and trust PATH (a project folder or its .sandbox-ai.json on the chosen host): the selected entry,
        else one asked for."""
        host = self.host()
        items = self.list.selectedItems()
        if path is None and items:
            path = items[0].data(256)
        if path is None:  # a project folder of that host, chosen from its folders (this machine's dialog here)
            from . import remote_chooser as RC
            path = RC.choose_folder(self, host, tr("Review and trust…").rstrip("…"), "project", sai=self.sai)
            if not path:
                return
        target = PT.config_path(path)
        if target is None:
            warn(self, tr("Trusted project configurations"), tr("Give the absolute path of a folder on {host}.",
                                                               host=self.hosts.label(host)))
            return
        from .host_trust import review_and_trust
        review_and_trust(self, self.sai, host, target, source=[self.review_button, self.forget_button],
                         done=lambda trusted, _document: self.reviewed(trusted), say_trusted=True)

    def reviewed(self, trusted):
        if trusted:
            self.changed.emit()
        self.refresh()

    def forget(self):
        items, host = self.list.selectedItems(), self.host()
        if not items:
            return
        path = items[0].data(256)
        if not backend.valid_directory(path):
            return
        if not ask_first(self, "project-forget", tr("Forget…").rstrip("…"), tr(
                "No longer trust {path} on {host}? Starts of that project stop until its configuration is trusted "
                "again.", path=printable(path), host=self.hosts.label(host)), tr("Forget")):
            return
        background(lambda: self.sai.forget_trusted(host, path), owner=self, work=True,
                   source=[self.refresh_button, self.forget_button], timeout=90,
                   done=lambda result, error: self.forgotten(result, error))

    def forgotten(self, result, error):
        status, out, err = outcome(result, error)
        text = printable_text((out + err).strip())[-1500:]
        if status != 0:
            warn(self, tr("Trusted project configurations"), text or tr("Failed (exit status {status}).",
                                                                      status=status))
        else:
            self.status.setText(text)
        self.changed.emit()
        self.refresh()


class SettingsDialog(QDialog):
    changed = Signal(str)  # the key of a setting that was saved
    intro_requested = Signal()  # "Watch the intro again": the dialog closes, the window plays it

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        self.sai = sai
        self.setWindowTitle(tr("Settings"))
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        arrow_tabs(self.tabs)  # a screen narrower than the titles: they scroll, whole, with ← → at the row's end
        layout.addWidget(self.tabs, 1)
        # A (?) beside the tabs explains the tab shown (help_texts.SETTINGS_TABS, in the tabs' order).
        self.tab_help = help_view.HelpButton(help_texts.SETTINGS_TABS[0])
        corner = QWidget()
        corner_row = QHBoxLayout(corner)
        corner_row.setContentsMargins(4, 2, 4, 2)
        corner_row.addWidget(self.tab_help)
        self.tabs.setCornerWidget(corner)
        self.tabs.currentChanged.connect(lambda index: self.tab_help.set_tip(
            help_texts.SETTINGS_TABS[max(0, min(index, len(help_texts.SETTINGS_TABS) - 1))]))
        current = sai.settings()
        self.fields = {}
        self.font_hints = {}

        # appearance
        page, form = self.tab(tr("Appearance"))
        self.combo(form, "theme", tr("Theme"), [(key, tr(text)) for key, text in theme.choices()], current)
        self.reading_combo(form, current)
        self.font_combo(form, "ui", tr("Interface font"), current)
        self.font_combo(form, "mono", tr("Monospace font (terminal, code, logs)"), current)
        form.addRow(explanation(tr("The app downloads no fonts: the reading fonts come with it; an interface or "
                                   "monospace font marked not installed says how to get it (point at it in the "
                                   "list). Terminal tabs opened from now on use the monospace font.")))
        self.combo(form, "language", tr("Language"), (("system", tr("as the system")), ("en", "English"),
                                                      ("de", "Deutsch")), current)
        form.addRow(explanation(tr("A new language applies when the app is started again.")))
        self.check(form, "guide_nodes", tr("Help set up each new part of the board, one question at a time"), current)
        self.check(form, "guide_tour", tr("Offer the guided tour at the first start"), current)
        self.check(form, "help_buttons", tr("Show the (?) help buttons (the Help page, the tours and F1 stay)"),
                   current)
        self.check(form, "explanations", tr("Show explanations (the info texts below forms and tables, the pages' "
                                            "intro lines)"), current)
        self.check(form, "button_texts", tr("Show button texts beside the icons"), current)
        self.check(form, "intro", tr("Play the intro animation at start (Esc skips it)"), current)
        self.combo(form, intro_choice.SETTING, tr("Intro animation"),
                   [(intro_choice.ROTATE, tr(intro_choice.ROTATE_TITLE))] +
                   [(which, tr(intro_choice.TITLES[which])) for which in intro_choice.IDS],
                   {intro_choice.SETTING: intro_choice.chosen(current)})
        self.check(form, "show_flags", tr("Show the command-line options (--…) beside the fields' names"), current)
        self.replay = QPushButton(tr("Watch the intro again"))
        self.replay.clicked.connect(self.watch_intro)
        form.addRow("", self.replay)

        # window and notifications
        page, form = self.tab(tr("Window"))
        self.check(form, "tray", tr("Keep answering in the system tray when the window is closed"), current)
        self.autostart = WrapCheckBox(tr("Start at login, in the tray"))
        self.autostart.setChecked(backend.autostart_enabled())
        self.autostart.toggled.connect(self.choose_autostart)
        form.addRow("", self.autostart)
        self.combo(form, "terminal", tr("Commands such as builds and logins run"),
                   (("app", tr("in a tab of the app (when available)")), ("window", tr("in a terminal window"))), current)
        # Chromium's sandbox for the terminal tabs' web view: yours to switch off (off by default), with the risk said.
        self.check(form, "web_without_sandbox",
                   tr("Use web views without Chromium's sandbox (QTWEBENGINE_DISABLE_SANDBOX)"), current)
        form.addRow(plain_label(tr("Terminal tabs show what sandboxes and agents print in a web view (Chromium). "
                                   "Chromium's sandbox keeps a bug in its renderer away from your files and your "
                                   "session; it needs free user namespaces, and every running sandbox holds some. "
                                   "Switched on, terminal tabs work even when none are free, without that "
                                   "protection. Off (the default): the app checks first and opens a terminal window "
                                   "when none are free. Applies when the app is started again."), role="secondary"))
        # copy, paste and links in the terminal tabs, and the login browser (terminal_tools.py, login_browser.py)
        self.combo(form, "login_links", tr("Open sign-in links of the agents in"),
                   (("app", tr("the app's login browser (it hands the result to the terminal)")),
                    ("system", tr("the system browser"))), current)
        self.combo(form, "device_signin", tr("A device sign-in (a one-time code: Codex) opens its page in"),
                   (("system", tr("the system browser, by itself (the code is shown beside it)")),
                    ("app", tr("the app's login browser"))), current)
        self.check(form, "copy_on_select", tr("Copy text selected in a terminal tab to the clipboard at once"), current)
        self.check(form, "paste_warn", tr("Ask before pasting several lines where each line break acts as Enter"),
                   current)
        self.combo(form, "terminal_osc52", tr("A program in a terminal tab may put text on the clipboard (OSC 52)"),
                   (("ask", tr("after asking (the default)")), ("allow", tr("yes, said in the status bar")),
                    ("off", tr("never"))), current)
        forget = QPushButton(tr("Forget the login browser's cookies and data"))
        forget.clicked.connect(self.forget_login_browser)
        form.addRow("", forget)
        form.addRow(explanation(tr("Copy: select with the mouse (double click: a word, triple: a line; Shift - Option "
                                   "on macOS - selects when the program takes the mouse), then Ctrl+Shift+C (Cmd+C), "
                                   "Ctrl+Insert or the right-click menu. Paste: Ctrl+Shift+V (Cmd+V), Shift+Insert "
                                   "or the menu; control characters are always asked about. A link: Ctrl+click "
                                   "(Cmd+click), or Copy link in the menu - also one drawn over several rows. A "
                                   "request to read the clipboard is never answered.")))
        # What only says that something is done ("Written and trusted: …"): a short notice in the window that goes
        # by itself (short_notices.py); off, none is shown (the status bar and the log still have each).
        self.check(form, "short_notices", tr("Show short notices (toasts)"), current)
        form.addRow(explanation(tr("A short notice near the bottom of the window says for a moment that something is "
                                   "done (written, trusted, removed…) and goes by itself; no OK to click. Off: none "
                                   "is shown, the status bar and the log still have each.")))
        self.check(form, "notifications", tr("Show notifications (popups)"), current)
        form.addRow(explanation(tr("Off (the default): nothing pops up. Questions wait on the Approvals page, the "
                                   "number of open ones is shown in the navigation and the tray, and every "
                                   "notification is written to the log (the log window).")))
        self.check(form, "notify", tr("Desktop notification for each new question"), current)
        self.check(form, "auto_tabs", tr("Open a tab for each session or sandbox that starts"), current)
        self.check(form, "reopen_terminals", tr("At start, also open a terminal for every other running session"), current)
        self.spin(form, "notify_seconds", tr("Notifications stay (seconds)"), 3, 120, current)
        self.combo(form, "notify_style", tr("Notifications are shown"),
                   [(key, tr(text)) for key, text in notifications.STYLES], current)

        def follow(on):  # (with notifications off nothing of these pops up: not to be changed as if it did)
            for key in ("notify", "notify_seconds", "notify_style"):
                self.fields[key].setEnabled(bool(on))
        self.fields["notifications"].toggled.connect(follow)
        follow(self.fields["notifications"].isChecked())
        form.addRow(explanation(tr("In the app's theme: a popup at the corner of the screen in the theme's colours "
                                   "(warnings in its warning colour, errors in its error colour; errors stay until "
                                   "closed). Where a window cannot place itself (Wayland), the desktop's notification "
                                   "is used.")))
        # What the sandboxes themselves show on the desktop (sandbox-ai's defaults: desktop_notifications) - while
        # this app runs they show nothing but a canary's alarm anyway; this is for the time it does not run.
        self.desktop = QComboBox()
        levels = sai.desktop_notification_levels()
        texts = {"all": tr("all (questions, safeguards, limits, alarms)"), "alarms": tr("only a canary's alarm"),
                 "none": tr("none")}
        for level in levels:
            self.desktop.addItem(texts.get(level, level), level)
        defaults, problem = sai.sandbox_defaults()
        self.desktop.setCurrentIndex(max(0, self.desktop.findData(defaults.get("desktop_notifications", "all"))))
        self.desktop.setEnabled(bool(levels) and not problem)
        self.desktop.currentIndexChanged.connect(lambda _: self.save_desktop(self.desktop.currentData()))
        self.fields["desktop_notifications"] = self.desktop
        form.addRow(tr("Desktop notifications of the sandboxes"), self.desktop)
        form.addRow(explanation(tr("While this app runs, the sandboxes show none of their own (only a canary's "
                                   "alarm): the app has them. This says what they show when it does not run, for "
                                   "every start (desktop_notifications in {path}).",
                                   path=printable(str(sai.sandbox_defaults_file())))))
        # Safeguards that refuse or end something in a running sandbox (sandbox-ai safeguards)
        self.check(form, "notify_safeguards", tr("A popup when a safeguard refuses or ends something in a running "
                                                 "sandbox"), current)
        from .safeguards_view import SafeguardMute
        self.safeguard_mute = SafeguardMute(sai, problem=lambda text: warn(self, tr("Safeguards"), text))
        self.safeguard_mute.changed.connect(lambda: self.changed.emit("safeguard_mute"))
        form.addRow(tr("Safeguards announced"), self.safeguard_mute)
        form.addRow(explanation(tr("Unticked kinds are never announced, here or on the desktop, for every start ("
                                   "safeguard_mute in {path}); they are still recorded: Activity > Safeguards, "
                                   "sandbox-ai safeguards.", path=printable(str(sai.sandbox_defaults_file())))))
        self.check(form, "debug", tr("Debug mode: log every step (pages, commands, their times and results)"), current)
        from . import applog
        form.addRow(plain_label(tr("The log: {path} (Help > Show the log)", path=breakable(applog.log_path())),
                                role="secondary"))

        # phone notifications (sandbox-ai notify): questions and alarms to ntfy, Telegram, WhatsApp, a webhook
        page, form = self.tab(tr("Notifications"))
        from .notify_view import PhoneChannels
        self.phone_channels = PhoneChannels(sai)
        form.addRow(explanation(tr("Questions and alarms to your phone while you are away (sandbox-ai notify): ntfy, "
                                   "Telegram, WhatsApp or a webhook. Sent by the host the sandboxes run on - never "
                                   "from inside a sandbox - with the tokens as the names of its secrets.")))
        form.addRow(self.phone_channels)

        # polling
        page, form = self.tab(tr("Polling"))
        self.spin(form, "approvals_interval", tr("Open questions (milliseconds)"), 250, 10000, current)
        self.spin(form, "sandboxes_interval", tr("Running sandboxes (milliseconds)"), 1000, 60000, current)
        self.spin(form, "connections_interval", tr("Connection log (milliseconds)"), 500, 60000, current)
        # How long a sandbox's request waits for the answer (sandbox-ai's defaults: ask_timeout; --ask-timeout wins).
        defaults, problem = sai.sandbox_defaults()
        self.ask_timeout = QSpinBox()
        self.ask_timeout.setRange(5, 3600)
        self.ask_timeout.setSuffix(" s")
        value = defaults.get("ask_timeout")
        self.ask_timeout.setValue(value if isinstance(value, int) and not isinstance(value, bool) and
                                  5 <= value <= 3600 else 60)
        self.ask_timeout.setEnabled(not problem)
        self.ask_timeout.setAccessibleName(tr("Wait for an answer"))
        self.ask_timeout.editingFinished.connect(lambda: self.save_ask_timeout(self.ask_timeout.value()))
        self.fields["ask_timeout"] = self.ask_timeout
        form.addRow(tr("Wait for an answer"), self.ask_timeout)
        form.addRow(explanation(tr("A sandbox's request waits this long for your answer to its question, for every "
                                   "start (ask_timeout in {path}; --ask-timeout wins). Unanswered by then, the request "
                                   "fails and the question stays open (Approvals › Timed out): allowed later, the next "
                                   "try goes through. Keep the question interval short.",
                                   path=printable(str(sai.sandbox_defaults_file())))))

        # What a start lacks (its image not built, an MCP server not added): one question for everything, or one per
        # fix (sandbox-ai's defaults: fix_questions; the command line asks the same way, the app's dialog too).
        self.fix_questions = QComboBox()
        for value, text in (("all", tr("one question for everything (Fix all, Choose…, Cancel)")),
                            ("each", tr("one question per fix"))):
            self.fix_questions.addItem(text, value)
        self.fix_questions.setCurrentIndex(max(0, self.fix_questions.findData(defaults.get("fix_questions", "all"))))
        self.fix_questions.setEnabled(not problem and sai.fix_questions_known())
        self.fix_questions.setAccessibleName(tr("What a start lacks"))
        self.fix_questions.currentIndexChanged.connect(
            lambda _: self.save_fix_questions(self.fix_questions.currentData()))
        self.fields["fix_questions"] = self.fix_questions
        form.addRow(tr("What a start lacks"), self.fix_questions)
        form.addRow(explanation(tr("When a start lacks something sandbox-ai can provide - its image not built, an MCP "
                                   "server not added, a toolchain not in the store - it is listed with what would be "
                                   "done, and asked about: once for everything, or each on its own (fix_questions in "
                                   "{path}; a start in a terminal asks the same way, --fix does it without asking). "
                                   "What is unsigned or security-relevant is always named so, and nothing is offered "
                                   "that the security mode refuses.",
                                   path=printable(str(sai.sandbox_defaults_file())))))

        # sandbox-ai
        page, form = self.tab(tr("sandbox-ai"))
        form.addRow(tr("In use"), plain_label(breakable(printable(sai.directory))))
        home = PathEdit(backend.setting("sandbox_ai_home", current))
        home.changed.connect(lambda text: self.save("sandbox_ai_home", text.strip()))
        self.fields["sandbox_ai_home"] = home
        form.addRow(tr("Use the checkout at"), home)
        form.addRow(explanation(tr("Empty: the sandbox-ai command, or $SANDBOX_AI_HOME when it is set (it wins). "
                                   "Applies at the next start; a folder a sandbox can write is refused then.")))
        self.preset_dirs = PresetDirs(sai)
        form.addRow(tr("Preset directories"), self.preset_dirs)
        self.trusted = TrustedProjects(sai)
        form.addRow(tr("Trusted projects"), self.trusted)
        self.loop_rows(form)

        # updates: the update mode of this machine's sandbox-ai (updates_view.py)
        page, form = self.tab(tr("Updates"))
        from .updates_view import UpdatesPanel
        self.updates = UpdatesPanel(sai, window=parent)
        form.addRow(self.updates)

        # new projects and the board
        page, form = self.tab(tr("New projects"))
        self.combo(form, "default_agent", tr("Agent"), [(a, a) for a in sai.agents], current)
        backends = [("podman", "Podman"), ("bwrap", "Bubblewrap")]
        self.combo(form, "default_backend", tr("Backend"), backends, current)
        # The presets of this machine's catalogue (sandbox-ai's, the app's own and yours): an app's own by its id, the
        # others by their key (source:id), as the wizard finds them.
        catalogue = PR.catalogue(sai, LOCAL)
        template = self.combo(form, "default_template", tr("Preset"),
                              [(p.id if p.source == "app" else p.key, p.text(p.title)) for p in catalogue.presets],
                              current)
        try:  # an app's own id that sandbox-ai's preset of that id replaced (development): that one is shown
            chosen = catalogue.find(backend.setting("default_template", current))
            template.blockSignals(True)
            template.setCurrentIndex(max(0, template.findData(chosen.id if chosen.source == "app" else chosen.key)))
            template.blockSignals(False)
        except ValueError:
            pass
        self.spin(form, "board_grid", tr("Board grid (0: no snapping)"), 0, 200, current)
        self.check(form, "board_confirm_remove", tr("Ask before the [x] of a part removes it"), current)
        # The agent's intro (sandbox-ai's defaults: agent_intro, for every start - the app's and the command line's):
        # on unless cleared here; a board's Agent part turns it off or on for its project (--no-intro, --intro).
        defaults, problem = sai.sandbox_defaults()
        self.agent_intro = WrapCheckBox(tr("Tell the agent what its sandbox has"))
        self.agent_intro.setChecked(defaults.get("agent_intro") is not False)
        self.agent_intro.setEnabled(not problem and "agent_intro" in getattr(sai.launcher, "DEFAULTS_KEYS", ()))
        self.agent_intro.toggled.connect(lambda on: self.save_agent_intro(bool(on)))
        self.fields["agent_intro"] = self.agent_intro
        form.addRow("", self.agent_intro)
        form.addRow(explanation(tr("On (the default): every start writes /sandbox/session/INTRO.md - its folders, "
                                   "toolchains, MCP servers, services and devices with where each is and how to use "
                                   "it, no secret, no host path - and tells the agent to read it. Off: no start does "
                                   "(agent_intro in {path}); a board's Agent part decides for its project.",
                                   path=breakable(printable(str(sai.sandbox_defaults_file()))))))

        # privacy: sandbox-ai's own defaults, for every start (the app's and the command line's)
        page, form = self.tab(tr("Privacy"))
        self.telemetry = QComboBox()
        # off and strict only: telemetry is always off (an older sandbox-ai's on is never offered; an old "on" in
        # the file is shown as off, which is how sandbox-ai reads it now)
        levels = [level for level in sai.telemetry_levels() if level in backend.TELEMETRY_ORDER]
        defaults, problem = sai.sandbox_defaults()
        texts = {"off": tr("off: no usage statistics, error reports, feedback or update checks (recommended)"),
                 "strict": tr("strict: also no other calls than the model's")}
        for level in levels:
            self.telemetry.addItem(texts.get(level, level), level)
        self.telemetry.setCurrentIndex(max(0, self.telemetry.findData(defaults.get("telemetry", "off"))))
        self.telemetry.setEnabled(bool(levels) and not problem)
        self.telemetry.currentIndexChanged.connect(lambda _: self.save_telemetry(self.telemetry.currentData()))
        self.fields["telemetry"] = self.telemetry
        form.addRow(tr("Telemetry of the agents (always off)"), self.telemetry)
        if not levels:
            form.addRow(plain_label(tr("This sandbox-ai has no telemetry setting yet: update it."), role="notice-error"))
        elif problem:
            form.addRow(plain_label(tr("It stops every start: {problem}", problem=printable_text(problem)),
                                    role="notice-error"))
        form.addRow(explanation(tr("Telemetry is always off, for every agent; strict also holds back their other "
                                   "calls. For every start, in {path}; a --telemetry option given at a start wins, "
                                   "the security mode paranoid allows no less than strict.",
                                   path=breakable(printable(str(sai.sandbox_defaults_file()))))))
        # The file views (Memory…, Session files…): logins and keys listed and marked by default (user 2026-11-19);
        # ticked, left out (sandbox-ai's defaults: hide_secrets; also asked of other hosts' views: --hide-secrets).
        self.hide_secrets = WrapCheckBox(tr("Hide logins and keys in file views"))
        self.hide_secrets.setChecked(defaults.get("hide_secrets") is True)
        self.hide_secrets.setEnabled(not problem)
        self.hide_secrets.toggled.connect(lambda on: self.save_hide_secrets(bool(on)))
        self.fields["hide_secrets"] = self.hide_secrets
        form.addRow("", self.hide_secrets)
        form.addRow(explanation(tr("Off (the default): files that look like logins or keys (~/.git-credentials, an "
                                   "agent's login, a private key) are listed in Memory… and Session files… and "
                                   "marked; their text is shown only when you ask. On: they are left out of those "
                                   "lists, also of other hosts' (hide_secrets in {path}). An export leaves them out "
                                   "either way unless you take them in.",
                                   path=breakable(printable(str(sai.sandbox_defaults_file()))))))

        # storage: the storage check (sandbox-ai gc --check) and its automatic rules - sandbox-ai's own defaults, also
        # read by the command line's gc (storage.py); each rule off unless set
        page, form = self.tab(tr("Storage"))
        self.storage_fields = {}
        check = QComboBox()
        for value, text in (("off", tr("off")), ("weekly", tr("weekly (the default)")), ("daily", tr("daily"))):
            check.addItem(text, value)
        check.setCurrentIndex(max(0, check.findData(defaults.get("storage_check", "weekly"))))
        check.setEnabled(not problem)
        check.currentIndexChanged.connect(lambda _: self.save_storage("storage_check", None if check.currentData()
                                                                      == "weekly" else check.currentData()))
        self.fields["storage_check"] = check
        form.addRow(tr("Storage check"), check)
        for key, label, low, high, default in (
                ("storage_threshold_gb", tr("Notice when more than this could be freed (GB)"), 1, 10000, 5),
                ("storage_growth_gb", tr("Name a start that grew more than (GB)"), 1, 10000, 2)):
            field = QSpinBox()
            field.setRange(low, high)
            value = defaults.get(key, default)
            field.setValue(int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default)
            field.setEnabled(not problem)
            field.valueChanged.connect(lambda number, k=key, d=default: self.save_storage(
                k, None if number == d else int(number)))
            self.fields[key] = field
            form.addRow(label, field)
        form.addRow(explanation(tr("The app checks what sandbox-ai keeps on this machine at its start and then as "
                                   "chosen, and says when more than the threshold could be freed; Optimize… on the "
                                   "Sessions page shows the plan ({command}).", command="sandbox-ai gc --check")))
        for key, label in (("storage_caches_days", tr("Empty the download caches of starts unused for (days)")),
                           ("storage_compact_days", tr("Compact transcripts older than (days)")),
                           ("storage_pack_days", tr("Pack stopped kept starts unused for (days)")),
                           ("storage_logs_days", tr("Remove log folders not written for (days)"))):
            field = QSpinBox()
            field.setRange(0, 3650)
            field.setSpecialValueText(tr("off"))
            value = defaults.get(key)
            field.setValue(value if isinstance(value, int) and not isinstance(value, bool) else 0)
            field.setEnabled(not problem)
            field.valueChanged.connect(lambda number, k=key: self.save_storage(k, int(number) or None))
            self.fields[key] = field
            form.addRow(label, field)
        form.addRow(explanation(tr("Automatic rules, each off (0) unless set: applied by the storage check and by "
                                   "sandbox-ai gc, never to a start that runs (compacting transcripts excepted: safe "
                                   "while it runs); each action is logged and said in the next notice. In {path}.",
                                   path=breakable(printable(str(sai.sandbox_defaults_file()))))))

        # policy
        self.policy_page, form = self.tab(tr("Administrator policy"))
        policy, problem, text, path = sai.policy()
        form.addRow(tr("File"), plain_label(breakable(printable(path))))
        if text is None:
            form.addRow(plain_label(tr("There is no administrator policy on this machine.")))
        else:
            if problem:
                form.addRow(plain_label(tr("It stops every start: {problem}", problem=printable_text(problem)),
                                        role="notice-error"))
            forbidden = ", ".join(str(v) for v in policy.get("forbid", [])) or tr("nothing")
            form.addRow(tr("Forbidden"), plain_label(printable(forbidden)))
            form.addRow(tr("Backends"), plain_label(printable(", ".join(policy.get("backends", ["podman", "bwrap"])))))
            form.addRow(tr("Widest answer"), plain_label(printable(policy.get("max_approval_scope", "always"))))
            if policy.get("allow_within") is not None:
                form.addRow(tr("Network ceiling"), plain_label(printable(", ".join(policy["allow_within"]))))
            if policy.get("allow_hosts"):
                form.addRow(tr("Named hosts allowed"), plain_label(printable(", ".join(policy["allow_hosts"]))))
            view = QPlainTextEdit(text)
            view.setReadOnly(True)
            view.setFont(MONO)
            form.addRow(view)
        form.addRow(explanation(tr("Only root can change it. What it forbids is greyed out in the app.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("Close"))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        tame_combos(self)  # a long choice (the telemetry's) does not widen the dialog; the list shows it whole
        self.fit()
        self.explain()

    def loop_rows(self, form):
        """The loop guard for local models (sandbox-ai's defaults: loop_guard, for every LLM server and start): each
        layer empty for sandbox-ai's own default (shown in it), a value typed in checked at once and saved only when
        every layer is valid; a server's own layers (its Parameters…) and a start's win over these."""
        from . import llm_params as LP
        from .llm_params_view import LoopFields
        spec = LP.loop_spec(self.sai)
        if spec is None:
            form.addRow(tr("Loop guard for local models"), plain_label(tr(
                "This sandbox-ai has no loop guard layers yet: update it."), role="secondary"))
            return
        defaults, problem = self.sai.sandbox_defaults()
        own = defaults.get("loop_guard") if isinstance(defaults.get("loop_guard"), dict) else {}
        self.loop_guard = LoopFields(spec, {k: v for k, v in own.items() if k in spec[0]})
        self.loop_guard.setEnabled(not problem)
        self.loop_guard.committed.connect(lambda: self.save_loop_guard(self.loop_guard.values()))
        self.fields["loop_guard"] = self.loop_guard
        form.addRow(tr("Loop guard for local models"), self.loop_guard)
        form.addRow(explanation(tr("What sandbox-ai does against a model that repeats itself, for every LLM server "
                                   "(--llm): empty layers keep sandbox-ai's own default; a server's own layers (its "
                                   "Advanced settings… on the LLM servers page) and a start's win (loop_guard in {path}).",
                                   path=breakable(printable(str(self.sai.sandbox_defaults_file()))))))

    def save_loop_guard(self, layers):
        try:
            self.sai.save_sandbox_default("loop_guard", layers or None)
        except (OSError, ValueError) as exc:
            warn(self, tr("Loop guard for local models"), printable_text(exc))
            return
        self.changed.emit("loop_guard")

    def fit(self):
        """Wide enough for every tab title (they scroll only on a screen narrower than that), never wider or
        higher than the screen; each tab scrolls when the dialog is lower than its content."""
        room, bar = screen_room(self), self.tabs.tabBar()
        current, widest = bar.currentIndex(), 0
        bar.blockSignals(True)  # the chosen tab's title is bold in some themes: the widest of the bar with each
        for index in range(bar.count()):
            bar.setCurrentIndex(index)
            widest = max(widest, bar.sizeHint().width())
        bar.setCurrentIndex(current)
        bar.blockSignals(False)
        margins = self.layout().contentsMargins()
        titles = widest + self.tabs.cornerWidget().sizeHint().width() + margins.left() + margins.right() + 4
        width = min(max(titles, self.minimumSizeHint().width()), room.width())
        self.setMinimumSize(width, min(LOWEST, room.height()))
        self.resize(min(max(SIZE.width(), width), room.width()), min(SIZE.height(), room.height()))

    def explain(self):
        """A short explanation on hover for each tab and setting (they stay when the (?)s are off)."""
        for index, ident in enumerate(help_texts.SETTINGS_TABS[:self.tabs.count()]):
            self.tabs.setTabToolTip(index, safe_tooltip(help_view.summary(ident)))
        for key, widget in {**self.fields, "autostart": self.autostart}.items():
            text = help_texts.SETTING_TIPS.get(key)
            if text is not None:
                help_view.hover(widget, help_texts.say(text))
                help_view.hover(help_view.label_of(widget), help_texts.say(text))
        help_view.explain_tree(self)

    # -- fields --

    def watch_intro(self):
        """Close the dialog first: the intro plays over the window, not under a modal dialog."""
        self.accept()
        self.intro_requested.emit()

    def tab(self, title):
        """A tab with a form; it scrolls when the dialog is lower than the form (every setting stays reachable)."""
        page = QWidget()
        form = QFormLayout(page)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)  # a long label above its field, not beside it
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)
        return page, form

    def save(self, key, value):
        try:
            self.sai.save_setting(key, value)
        except (OSError, ValueError) as exc:  # a full disk, a read-only or unreadable settings file: said, not kept
            warn(self, tr("Not saved"), printable_text(exc), modal=False)
            return
        if key == "debug":
            from . import applog
            applog.set_debug(bool(value))
            if not value:
                os.environ.pop("SANDBOX_AI_APP_DEBUG", None)
        if key == "explanations":  # at once, on every page and in this dialog too
            set_explanations_shown(bool(value))
        if key == "theme":
            theme.apply_soon(value, after=lambda _mode: self.changed.emit(key))  # after the combo box's event
            return
        if key in (*fonts.SETTING.values(), reading_fonts.SETTING):  # at once: the app's fonts, new terminal tabs
            fonts.use(self.sai.settings())
            theme.apply_font()
        self.changed.emit(key)

    def combo(self, form, key, label, items, current):
        widget = QComboBox()
        for value, text in items:
            widget.addItem(text, value)
        widget.setCurrentIndex(max(0, widget.findData(backend.setting(key, current))))
        widget.currentIndexChanged.connect(lambda _: self.save(key, widget.currentData()))
        form.addRow(label, widget)
        self.fields[key] = widget
        return widget

    def reading_combo(self, form, current):
        """The reading font: the theme's, or one of the fonts that come with the app (reading_fonts.READING) for all of
        the app's text and the Help; terminals, code and logs keep the monospace font. Applied at once."""
        items = [(value, tr(label)) for value, _family, label, _note in reading_fonts.READING]
        widget = self.combo(form, reading_fonts.SETTING, tr("Reading font"), items, current)
        for index, (_value, _family, _label, note) in enumerate(reading_fonts.READING):
            widget.setItemData(index, tr(note), Qt.ItemDataRole.ToolTipRole)
        hint = plain_label(role="secondary")
        form.addRow("", hint)
        self.font_hints["reading"] = hint
        widget.currentIndexChanged.connect(lambda _: self.show_reading_hint())
        self.show_reading_hint()
        return widget

    def show_reading_hint(self):
        """Under the reading font: what it is for, and - when its files are missing from this copy of the app - that
        the theme's font is used instead."""
        value = self.fields[reading_fonts.SETTING].currentData() or ""
        hint = self.font_hints["reading"]
        entry = next((e for e in reading_fonts.READING if e[0] == value), reading_fonts.READING[0])
        text = tr(entry[3]) if value else ""
        if value and reading_fonts.family_of(value) is None:
            text = tr("{font} is missing from this copy of the app: the theme's font is used.", font=entry[1])
        hint.setText(text)
        hint.setVisible(bool(text))

    def font_combo(self, form, kind, label, current):
        """The font of KIND ("ui" or "mono"): the theme's own, or one of fonts.KINDS[kind]. Installed fonts are
        listed as they are; the others are marked not installed, with how to get them (they can be chosen ahead:
        the theme's font is used until they are installed)."""
        key = fonts.SETTING[kind]
        offered = fonts.choices(kind)
        items = [("", tr("the theme's"))] + [
            (family, family if have else tr("{font} (not installed)", font=family)) for family, have, _ in offered]
        widget = self.combo(form, key, label, items, current)
        for index, (family, have, how) in enumerate(offered, start=1):
            if not have:
                widget.setItemData(index, tr("Not installed. To get it: {how}", how=tr(how)),
                                   Qt.ItemDataRole.ToolTipRole)
        hint = plain_label(role="secondary")
        form.addRow("", hint)
        self.font_hints[kind] = hint
        widget.currentIndexChanged.connect(lambda _: self.show_font_hint(kind))
        self.show_font_hint(kind)
        return widget

    def show_font_hint(self, kind):
        """Under a font that is chosen but not installed: that the theme's font is used, and how to get it."""
        choice = self.fields[fonts.SETTING[kind]].currentData()
        hint = self.font_hints[kind]
        missing = bool(choice) and fonts.pick(kind, choice) is None
        hint.setText(tr("{font} is not installed: the theme's font is used until it is. The app downloads no fonts; "
                        "install it, then start the app again. To get it: {how}",
                        font=choice, how=tr(fonts.how_to_get(kind, choice))) if missing else "")
        hint.setVisible(missing)

    def check(self, form, key, label, current):
        widget = WrapCheckBox(label)
        widget.setChecked(bool(backend.setting(key, current)))
        widget.toggled.connect(lambda on: self.save(key, bool(on)))
        form.addRow("", widget)
        self.fields[key] = widget
        return widget

    def spin(self, form, key, label, low, high, current):
        widget = QSpinBox()
        widget.setRange(low, high)
        widget.setValue(min(high, max(low, int(backend.setting(key, current)))))
        widget.valueChanged.connect(lambda value: self.save(key, int(value)))
        form.addRow(label, widget)
        self.fields[key] = widget
        return widget

    def save_ask_timeout(self, seconds):
        try:
            self.sai.save_sandbox_default("ask_timeout", None if seconds == 60 else int(seconds))
        except (OSError, ValueError) as exc:
            warn(self, tr("Wait for an answer"), printable_text(exc))
            return
        self.changed.emit("ask_timeout")

    def save_fix_questions(self, value):
        try:
            self.sai.save_sandbox_default("fix_questions", None if value == "all" else value)
        except (OSError, ValueError) as exc:
            warn(self, tr("What a start lacks"), printable_text(exc))
            return
        self.changed.emit("fix_questions")

    def save_desktop(self, level):
        try:
            self.sai.save_sandbox_default("desktop_notifications", None if level == "all" else level)
        except (OSError, ValueError) as exc:
            warn(self, tr("Notifications"), printable_text(exc))
            return
        self.changed.emit("desktop_notifications")

    def save_storage(self, key, value):
        """A storage setting (storage_check, its threshold, a rule) into sandbox-ai's defaults; None: its default."""
        try:
            self.sai.save_sandbox_default(key, value)
        except (OSError, ValueError) as exc:
            warn(self, tr("Storage"), printable_text(exc))
            return
        self.changed.emit(key)

    def save_agent_intro(self, on):
        try:
            self.sai.save_sandbox_default("agent_intro", None if on else False)
        except (OSError, ValueError) as exc:
            warn(self, tr("New projects"), printable_text(exc))
            return
        self.changed.emit("agent_intro")

    def save_hide_secrets(self, on):
        try:
            self.sai.save_sandbox_default("hide_secrets", True if on else None)
        except (OSError, ValueError) as exc:
            warn(self, tr("Privacy"), printable_text(exc))
            return
        self.changed.emit("hide_secrets")

    def save_telemetry(self, level):
        try:
            self.sai.save_sandbox_default("telemetry", None if level == "off" else level)
        except (OSError, ValueError) as exc:
            warn(self, tr("Privacy"), printable_text(exc))
            return
        self.changed.emit("telemetry")

    def forget_login_browser(self):
        """The login browser's cookies, storage and cache go (asked first): the next sign-in starts from nothing."""
        if not ask_first(self, "login-browser-forget", tr("Login browser"), tr(
                "Forget every sign-in the login browser keeps (its cookies, its storage and its cache)?"), tr("Forget")):
            return
        from . import login_browser
        try:
            login_browser.forget()
        except (OSError, ValueError) as exc:
            warn(self, tr("Login browser"), printable_text(exc), modal=False)
            return
        short_notice(self, tr("Login browser"), tr("Forgotten."))

    def choose_autostart(self, on):
        """Written or removed only here or on the System page, when the human ticks the box (the folders sandboxes
        can write, which it must not start from, read in the background first)."""
        if not on:
            return self.write_autostart(False, ())
        background(self.sai.sandbox_folders, owner=self, source=self.autostart, timeout=60,
                   done=lambda folders, error: self.write_autostart(True, folders, error))

    def write_autostart(self, on, forbidden, error=None):
        try:
            if error is not None:
                raise OSError(tr("Which folders sandboxes can write is not known ({error}): nothing is written.",
                                 error=printable_text(error)))
            backend.set_autostart(bool(on), forbidden=forbidden, also=[str(self.sai.directory)])
        except (OSError, ValueError) as exc:
            warn(self, tr("Start at login"), printable_text(exc))
            self.autostart.blockSignals(True)
            self.autostart.setChecked(backend.autostart_enabled())
            self.autostart.blockSignals(False)

    def show_policy(self):
        self.tabs.setCurrentWidget(self.policy_page.parentWidget().parentWidget())  # its scroll area (tab())

