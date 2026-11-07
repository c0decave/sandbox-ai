"""The Settings dialog: every setting of the app in one place, and the administrator's policy (read-only).

Settings are kept as before in ~/.config/sandbox-ai/app/settings.json (mode 0600), one key each
(backend.SETTINGS lists them with their defaults); the dialog writes a key when its field changes, and the
window applies what can change at once (theme, polling intervals, notifications). The language and the
sandbox-ai location apply at the next start. The policy tab shows /etc/sandbox-ai/policy.json as it is, why
it stops every start if it does, and what it forbids; the app greys those options out everywhere (the Start
page's form, the board, the wizard).
"""
from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from . import backend, notifications, presets as PR, theme
from .board_view import PathEdit
from .files import printable
from .i18n import tr
from .widgets import MONO, background, inform, outcome, plain_label, warn


class PresetDirs(QWidget):
    """The extra preset directories (sandbox-ai presets dirs): listed in order with what each gives or why it is
    skipped; Add folder… and Remove run the command, whose checks apply (never a folder sandboxes can write).
    The preset gallery shows the presets found there."""
    changed = Signal()

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        self.sai, self.entries = sai, []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
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
        self.status = plain_label(role="secondary")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        if not sai.has_preset_dirs():
            self.status.setText(tr("This sandbox-ai has no extra preset directories yet; update it."))
            self.add_button.setEnabled(False)
            self.remove_button.setEnabled(False)
        else:
            self.refresh()

    def refresh(self):
        background(self.sai.preset_dirs, owner=self, source=[self.add_button, self.remove_button], timeout=90,
                   done=self.loaded)

    def loaded(self, result, error):
        self.list.clear()
        if error is not None:
            self.status.setText(tr("Cannot list the preset directories: {error}", error=printable(str(error))))
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
                               yours=printable(str(result.get("yours") or ""))))
        self.update_buttons()

    def update_buttons(self):
        if not self.sai.has_preset_dirs():
            return
        self.add_button.setEnabled(True)
        self.remove_button.setEnabled(bool(self.list.selectedItems()))

    def add(self):
        from .state import choose_folder
        path = choose_folder(self, tr("Folder with presets (*.json)"), "presets")
        if path:
            self.run("add", path)

    def remove(self):
        items = self.list.selectedItems()
        if items:
            self.run("remove", items[0].data(256))

    def run(self, action, path):
        background(lambda: self.sai.preset_dir_command(action, path), owner=self,
                   source=[self.add_button, self.remove_button], timeout=90,
                   done=lambda result, error: self.done(result, error))

    def done(self, result, error):
        status, out, err = outcome(result, error)
        text = (out + err).strip()
        if status != 0:
            warn(self, tr("Preset directories"), text or tr("Failed (exit status {status}).", status=status))
        elif text:
            inform(self, tr("Preset directories"), text, modal=False)
        self.changed.emit()
        self.refresh()


class SettingsDialog(QDialog):
    changed = Signal(str)  # the key of a setting that was saved
    intro_requested = Signal()  # "Watch the intro again": the dialog closes, the window plays it

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        self.sai = sai
        self.setWindowTitle(tr("Settings"))
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        # A (?) beside the tabs explains the tab shown (help_texts.SETTINGS_TABS, in the tabs' order).
        from . import help_texts, help_view
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

        # appearance
        page, form = self.tab(tr("Appearance"))
        self.combo(form, "theme", tr("Theme"), [(key, tr(text)) for key, text in theme.choices()], current)
        self.combo(form, "language", tr("Language"), (("system", tr("as the system")), ("en", "English"),
                                                      ("de", "Deutsch")), current)
        form.addRow(plain_label(tr("A new language applies when the app is started again."), role="secondary"))
        self.check(form, "guide_nodes", tr("Help set up each new part of the board, one question at a time"), current)
        self.check(form, "guide_tour", tr("Offer the guided tour at the first start"), current)
        self.check(form, "intro", tr("Play the intro animation at start (Esc skips it)"), current)
        self.replay = QPushButton(tr("Watch the intro again"))
        self.replay.clicked.connect(self.watch_intro)
        form.addRow("", self.replay)

        # window and notifications
        page, form = self.tab(tr("Window"))
        self.check(form, "tray", tr("Keep answering in the system tray when the window is closed"), current)
        self.autostart = QCheckBox(tr("Start at login, in the tray"))
        self.autostart.setChecked(backend.autostart_enabled())
        self.autostart.toggled.connect(self.choose_autostart)
        form.addRow("", self.autostart)
        self.combo(form, "terminal", tr("Commands such as builds and logins run"),
                   (("app", tr("in a tab of the app (when available)")), ("window", tr("in a terminal window"))), current)
        self.check(form, "notify", tr("Desktop notification for each new question"), current)
        self.check(form, "auto_tabs", tr("Open a tab for each session or sandbox that starts"), current)
        self.spin(form, "notify_seconds", tr("Notifications stay (seconds)"), 3, 120, current)
        self.combo(form, "notify_style", tr("Notifications are shown"),
                   [(key, tr(text)) for key, text in notifications.STYLES], current)
        form.addRow(plain_label(tr("In the app's theme: a popup at the corner of the screen in the theme's colours "
                                   "(warnings in its warning colour, errors in its error colour; errors stay until "
                                   "closed). Where a window cannot place itself (Wayland), the desktop's notification "
                                   "is used."), role="secondary"))
        self.check(form, "debug", tr("Debug mode: log every step (pages, commands, their times and results)"), current)
        from . import applog
        form.addRow(plain_label(tr("The log: {path} (Help > Show the log)", path=str(applog.log_path())),
                                role="secondary"))

        # polling
        page, form = self.tab(tr("Polling"))
        self.spin(form, "approvals_interval", tr("Open questions (milliseconds)"), 250, 10000, current)
        self.spin(form, "sandboxes_interval", tr("Running sandboxes (milliseconds)"), 1000, 60000, current)
        self.spin(form, "connections_interval", tr("Connection log (milliseconds)"), 500, 60000, current)
        form.addRow(plain_label(tr("Questions are refused after {seconds} seconds without an answer: keep the question "
                                   "interval short.", seconds=sai.approval_seconds), role="secondary"))

        # sandbox-ai
        page, form = self.tab(tr("sandbox-ai"))
        form.addRow(tr("In use"), plain_label(printable(sai.directory)))
        home = PathEdit(backend.setting("sandbox_ai_home", current))
        home.changed.connect(lambda text: self.save("sandbox_ai_home", text.strip()))
        self.fields["sandbox_ai_home"] = home
        form.addRow(tr("Use the checkout at"), home)
        form.addRow(plain_label(tr("Empty: the sandbox-ai command, or $SANDBOX_AI_HOME when it is set (it wins). "
                                   "Applies at the next start; a folder a sandbox can write is refused then."),
                                role="secondary"))
        self.preset_dirs = PresetDirs(sai)
        form.addRow(tr("Preset directories"), self.preset_dirs)

        # new projects and the board
        page, form = self.tab(tr("New projects"))
        self.combo(form, "default_agent", tr("Agent"), [(a, a) for a in sai.agents], current)
        backends = [("podman", "Podman"), ("bwrap", "Bubblewrap")]
        self.combo(form, "default_backend", tr("Backend"), backends, current)
        self.combo(form, "default_template", tr("Preset"), [(p.id, tr(p.title)) for p in PR.PRESETS], current)
        self.spin(form, "board_grid", tr("Board grid (0: no snapping)"), 0, 200, current)

        # privacy: sandbox-ai's own defaults, for every start (the app's and the command line's)
        page, form = self.tab(tr("Privacy"))
        self.telemetry = QComboBox()
        levels = sai.telemetry_levels()
        defaults, problem = sai.sandbox_defaults()
        texts = {"off": tr("off: no usage statistics, error reports, feedback or update checks (recommended)"),
                 "strict": tr("strict: also no other calls than the model's"),
                 "on": tr("on: the agents' own settings")}
        for level in levels:
            self.telemetry.addItem(texts.get(level, level), level)
        self.telemetry.setCurrentIndex(max(0, self.telemetry.findData(defaults.get("telemetry", "off"))))
        self.telemetry.setEnabled(bool(levels) and not problem)
        self.telemetry.currentIndexChanged.connect(lambda _: self.save_telemetry(self.telemetry.currentData()))
        self.fields["telemetry"] = self.telemetry
        form.addRow(tr("Telemetry of Claude Code, Codex and OpenCode"), self.telemetry)
        if not levels:
            form.addRow(plain_label(tr("This sandbox-ai has no telemetry setting yet: update it."), role="notice-error"))
        elif problem:
            form.addRow(plain_label(tr("It stops every start: {problem}", problem=printable(problem)),
                                    role="notice-error"))
        form.addRow(plain_label(tr("For every start, in {path}; --telemetry on the command line wins, the security "
                                   "modes high and paranoid allow no less than off and strict.",
                                   path=printable(str(sai.sandbox_defaults_file()))), role="secondary"))

        # policy
        self.policy_page, form = self.tab(tr("Administrator policy"))
        policy, problem, text, path = sai.policy()
        form.addRow(tr("File"), plain_label(printable(path)))
        if text is None:
            form.addRow(plain_label(tr("There is no administrator policy on this machine.")))
        else:
            if problem:
                form.addRow(plain_label(tr("It stops every start: {problem}", problem=printable(problem)),
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
        form.addRow(plain_label(tr("Only root can change it. What it forbids is greyed out in the app."),
                                role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    # -- fields --

    def watch_intro(self):
        """Close the dialog first: the intro plays over the window, not under a modal dialog."""
        self.accept()
        self.intro_requested.emit()

    def tab(self, title):
        page = QWidget()
        form = QFormLayout(page)
        self.tabs.addTab(page, title)
        return page, form

    def save(self, key, value):
        self.sai.save_setting(key, value)
        if key == "debug":
            from . import applog
            applog.set_debug(bool(value))
            if not value:
                os.environ.pop("SANDBOX_AI_APP_DEBUG", None)
        if key == "theme":
            theme.apply_soon(value, after=lambda _mode: self.changed.emit(key))  # after the combo box's event
            return
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

    def check(self, form, key, label, current):
        widget = QCheckBox(label)
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

    def save_telemetry(self, level):
        try:
            self.sai.save_sandbox_default("telemetry", None if level == "off" else level)
        except (OSError, ValueError) as exc:
            warn(self, tr("Privacy"), printable(exc))
            return
        self.changed.emit("telemetry")

    def choose_autostart(self, on):
        """Written or removed only here or on the System page, when the human ticks the box."""
        try:
            backend.set_autostart(bool(on), forbidden=self.sai.sandbox_folders() if on else (),
                                  also=[str(self.sai.directory)])
        except (OSError, ValueError) as exc:
            warn(self, tr("Start at login"), printable(exc))
            self.autostart.blockSignals(True)
            self.autostart.setChecked(backend.autostart_enabled())
            self.autostart.blockSignals(False)

    def show_policy(self):
        self.tabs.setCurrentWidget(self.policy_page)

