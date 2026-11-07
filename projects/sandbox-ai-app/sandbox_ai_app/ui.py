"""The windows of sandbox-ai-app (PySide6)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import time

from PySide6.QtCore import QByteArray, QEvent, QPoint, QProcess, QRect, QSize, Qt, QTimer, Signal, qVersion
from PySide6.QtGui import QAction, QActionGroup, QBrush, QCloseEvent, QColor, QFont, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QDockWidget, QFrame, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QStyle, QSystemTrayIcon, QTabBar,
    QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import applog, backend, i18n, notifications, terminal, theme
from .files import printable
from .state import choose_file, choose_folder
from .i18n import N_, tr
from .activity_page import ActivityPage
from .board_view import BoardPage
from . import help_view
from .hosts_page import HostsPage
from .kits_page import KitsPage
from .state import State, scrub_arguments
from .pages import ApprovalsPage, CredentialsPage, FilesPage, ImagesPage, StatisticsPage
from .widgets import (
    MONO, AsyncCommand, ThreadCall, append_rows, background, busy, confirm, fill, find_row, fit_columns, flow, inform,
    local_time,
    outcome, plain_label, row_key, safe_tooltip, selected, selected_rows, set_enabled, show_text, table, warn,
)

CONNECTIONS_LIMIT = 5000  # records the Connections page keeps (backend.read_connections reads as many)
STATE_WAIT = 30  # seconds a look at a project folder (its state, whether it exists) may take
QUIT_WAIT = 10  # seconds the window waits, when it is closed, for the work still running
SESSION_GRACE = 60  # seconds after a logout request after which the logout counts as cancelled
TRAY_WAIT = 30  # seconds a start at login waits for the system tray to appear


def report(parent, title, result, error):
    """The outcome of a background sandbox-ai command, as plain text (its output may quote the agent)."""
    status, out, err = outcome(result, error)
    if status == 0:  # a success says so without blocking the window (approvals stay answerable)
        inform(parent, title, (out + err).strip(), modal=False)
    else:
        warn(parent, title, (out + err).strip())
    return status

GROUP_TITLES = {  # the launcher's option groups (backend.GROUPS), translated
    "Workspace": N_("Workspace"), "Network": N_("Network"), "Proxy & privacy": N_("Proxy & privacy"), "Git": N_("Git"),
    "Agent": N_("Agent"), "Login": N_("Login"), "Display & terminal": N_("Display & terminal"),
    "Hardware": N_("Hardware"), "Limits": N_("Limits"), "Engine": N_("Engine"), "More": N_("More"),
}


class PathField(QWidget):
    changed = Signal()

    def __init__(self, directory=True):
        super().__init__()
        self.directory = directory
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.textChanged.connect(self.changed)
        browse = QToolButton()
        browse.setText("…")
        browse.clicked.connect(self.browse)
        layout.addWidget(self.edit)
        layout.addWidget(browse)

    def browse(self):
        path = (choose_folder(self, tr("Choose a folder"), "folder") if self.directory
                else choose_file(self, tr("Choose a file"), "file"))
        if path:
            self.edit.setText(path)

    def value(self):
        return self.edit.text()

    def set_value(self, value):
        self.edit.setText("" if value is None else str(value))


class DeviceField(QWidget):
    """--device: one device per line, typed, or picked from the devices this computer has (path, what it is, its
    name from sysfs)."""
    changed = Signal()

    def __init__(self, found):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setFixedHeight(52)
        self.edit.setPlaceholderText(tr("/dev/ttyUSB0, /dev/kvm, usb:VENDOR:PRODUCT … one per line"))
        self.edit.textChanged.connect(self.changed)
        layout.addWidget(self.edit)
        self.pick = QToolButton()
        self.pick.setText("+")
        self.pick.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.pick)
        for path, kind, label in found:
            menu.addAction(f"{path}  ·  {tr(kind)}" + (f"  ·  {label}" if label else ""),
                           lambda p=path: self.add(p))
        if not found:
            menu.addAction(tr("No devices found on this computer")).setEnabled(False)
        self.pick.setMenu(menu)
        self.pick.setToolTip(tr("Add a device this computer has"))
        layout.addWidget(self.pick, alignment=Qt.AlignmentFlag.AlignTop)

    def add(self, path):
        if path not in self.value():
            self.edit.appendPlainText(path)

    def value(self):
        return [line.strip() for line in self.edit.toPlainText().splitlines() if line.strip()]

    def set_value(self, values):
        self.edit.setPlainText("\n".join(values or []))


class ListField(QWidget):
    """One value per line; for path options with a button that adds a folder."""
    changed = Signal()

    def __init__(self, path=False):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setFixedHeight(52)
        self.edit.textChanged.connect(self.changed)
        layout.addWidget(self.edit)
        if path:
            add = QToolButton()
            add.setText("+")
            add.setToolTip(tr("Add a folder"))
            add.clicked.connect(self.add_folder)
            layout.addWidget(add, alignment=Qt.AlignmentFlag.AlignTop)

    def add_folder(self):
        path = choose_folder(self, tr("Choose a folder"), "folder")
        if path:
            self.edit.appendPlainText(path)

    def value(self):
        return [line.strip() for line in self.edit.toPlainText().splitlines() if line.strip()]

    def set_value(self, values):
        self.edit.setPlainText("\n".join(values or []))


class MultiChoice(QWidget):
    """Several of a fixed set of values (e.g. --profile): one check box each."""
    changed = Signal()

    def __init__(self, choices):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.boxes = {}
        for choice in choices:
            box = QCheckBox(str(choice))
            box.toggled.connect(self.changed)
            layout.addWidget(box)
            self.boxes[str(choice)] = box
        layout.addStretch()

    def value(self):
        return [name for name, box in self.boxes.items() if box.isChecked()]

    def set_value(self, values):
        for name, box in self.boxes.items():
            box.setChecked(name in (values or []))


class OptionsForm(QTabWidget):
    """Every launcher option, grouped; built from the launcher's own parser (its help texts are sandbox-ai's)."""
    changed = Signal()

    def __init__(self, options):
        super().__init__()
        self.options, self.widgets = options, {}
        groups = {}
        for option in options:
            groups.setdefault(option.group, []).append(option)
        order = list(backend.GROUPS) + ["More"]
        for title in sorted(groups, key=lambda g: order.index(g) if g in order else len(order)):
            page = QWidget()
            outer = QVBoxLayout(page)
            form = QFormLayout()
            outer.addLayout(form)
            outer.addStretch()  # rows keep their height; the space goes below them
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for option in groups[title]:
                widget = self.widget_for(option)
                label = QLabel(f"{option.long}" + (f"  ({option.short})" if option.short else ""))
                label.setToolTip(option.help)
                widget.setToolTip(option.help)
                form.addRow(label, widget)
                self.widgets[option.long] = widget
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.addTab(scroll, tr(GROUP_TITLES.get(title, title)).replace("&", "&&"))  # & would mark a shortcut

    def widget_for(self, option):
        from . import hardware
        if option.long == "--gpu" and option.kind == "choice":
            # Only the kinds this computer has: a kind it lacks would be refused at the start.
            found = [kind for kind in option.choices if kind in hardware.gpu_kinds()]
            widget = QComboBox()
            widget.addItems([""] + found)
            widget.setEnabled(bool(found))
            widget.currentTextChanged.connect(self.changed)
            widget.setProperty("detected", ", ".join(found) or tr("none"))
            return widget
        if option.long == "--device" and option.kind == "list":
            widget = DeviceField(hardware.devices())
            widget.changed.connect(self.changed)
            return widget
        if option.long in ("--memory", "--cpus") and option.kind not in ("flag", "choice", "multi", "list"):
            memory, cpus = hardware.limits_hint()
            widget = QLineEdit()
            have = memory if option.long == "--memory" else tr("{count} CPUs", count=cpus)
            widget.setPlaceholderText(tr("{example} (this computer: {have})", example=option.metavar or "",
                                         have=have) if have else option.metavar or "")
            widget.setProperty("detected", have)
            widget.textChanged.connect(self.changed)
            return widget
        if option.kind == "flag":
            widget = QCheckBox(option.help.split(";")[0][:90] if option.help else "")
            widget.toggled.connect(self.changed)
        elif option.kind == "choice":
            widget = QComboBox()
            widget.addItems(([""] if option.default is None else []) + [str(c) for c in option.choices])
            if option.default is not None:
                widget.setCurrentText(str(option.default))
            widget.currentTextChanged.connect(self.changed)
        elif option.kind == "multi":
            widget = MultiChoice(option.choices)
            widget.changed.connect(self.changed)
        elif option.kind == "list":
            widget = ListField(path=option.is_path)
            widget.changed.connect(self.changed)
        elif option.is_path:
            widget = PathField(directory=option.long not in ("--git-credentials", "--local-config", "--auth-file",
                                                             "--import-codex-auth", "--config"))
            widget.changed.connect(self.changed)
        else:
            widget = QLineEdit()
            widget.setPlaceholderText(option.metavar or "")
            widget.textChanged.connect(self.changed)
        return widget

    def values(self):
        result = {}
        for option in self.options:
            widget = self.widgets[option.long]
            if isinstance(widget, QCheckBox):
                result[option.long] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                result[option.long] = widget.currentText()
            elif isinstance(widget, (ListField, DeviceField, PathField, MultiChoice)):
                result[option.long] = widget.value()
            else:
                result[option.long] = widget.text()
        return result

    def apply_policy(self, forbidden):
        """Grey out what the administrator's policy forbids: whole options, or single values of a choice."""
        for long, values in forbidden.items():
            widget = self.widgets.get(long)
            if widget is None:
                continue
            why = tr("The administrator's policy forbids {option}", option=long)
            if None in values:
                widget.setEnabled(False)
                widget.setToolTip(why)
            elif isinstance(widget, QComboBox):
                for index in range(widget.count()):
                    if widget.itemText(index) in values:
                        widget.model().item(index).setEnabled(False)
            elif isinstance(widget, MultiChoice):
                for value in values:
                    if value in widget.boxes:
                        widget.boxes[value].setEnabled(False)
                        widget.boxes[value].setToolTip(why)
        if "--egress-all-allow" in forbidden and isinstance(self.widgets.get("--profile"), MultiChoice):
            box = self.widgets["--profile"].boxes.get("egress-all")
            if box is not None:
                box.setEnabled(False)

    def set_values(self, values):
        for option in self.options:
            widget, value = self.widgets[option.long], values.get(option.long)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                text = "" if value is None and option.default is None else str(value or option.default)
                if text and widget.findText(text) < 0:  # e.g. a GPU kind this computer lacks: kept, not dropped
                    widget.addItem(text)
                    widget.setItemData(widget.count() - 1, tr("not found on this computer"), Qt.ItemDataRole.ToolTipRole)
                    widget.setEnabled(True)
                widget.setCurrentText(text)
            elif isinstance(widget, (ListField, DeviceField, MultiChoice)):
                widget.set_value(value or [])
            elif isinstance(widget, PathField):
                widget.set_value(value)
            else:
                widget.setText("" if value is None else str(value))


class LaunchPage(QWidget):
    """Choose the agent and every option, see the command, start."""
    opened = Signal(str)  # a project folder was reviewed and trusted here

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.options = sai.options()
        self.project, self.state = None, None
        layout = QVBoxLayout(self)
        note = QHBoxLayout()
        self.project_note = plain_label(role="secondary")
        self.review_button = QPushButton(tr("Review and trust…"))
        self.review_button.setToolTip(tr("Show the project's .sandbox-ai.json and trust exactly that content"))
        self.review_button.clicked.connect(lambda _=False: self.load_project(self.project))
        self.review_button.hide()
        note.addWidget(self.project_note, 1)
        note.addWidget(self.review_button)
        layout.addLayout(note)
        self.agent = QComboBox()
        self.agent.addItems(list(sai.agents))
        self.profile = QComboBox()
        self.profile.setMinimumWidth(180)
        load = QPushButton(tr("Load"))
        load.clicked.connect(self.load_profile)
        save = QPushButton(tr("Save as profile…"))
        save.clicked.connect(self.save_profile)
        project = self.open_button = QPushButton(tr("Open project folder…"))
        project.setToolTip(tr("Review a folder's .sandbox-ai.json, trust it if you agree, and start in that folder"))
        project.clicked.connect(lambda _=False: self.load_project())
        layout.addWidget(flow(QLabel(tr("Agent")), self.agent, QLabel(tr("Profile")), self.profile, load, save, project))
        self.form = OptionsForm(self.options)
        try:
            self.form.apply_policy(sai.forbidden())
        except Exception:  # noqa: BLE001 - a broken policy is shown on the System page
            pass
        default = backend.setting("default_agent")
        if default in sai.agents:
            self.agent.setCurrentText(default)
        layout.addWidget(self.form, 1)
        arguments = QHBoxLayout()
        self.agent_args = QLineEdit()
        self.agent_args.setPlaceholderText(tr("arguments for the agent itself, e.g. --model sonnet"))
        arguments.addWidget(QLabel(tr("Agent arguments")))
        arguments.addWidget(self.agent_args)
        layout.addLayout(arguments)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(MONO)
        self.preview.setFixedHeight(70)
        layout.addWidget(self.preview)
        dry = QPushButton(tr("Dry run"))
        dry.setToolTip(tr("Check the options and show the plan; nothing is started"))
        dry.clicked.connect(self.dry_run)
        self.dry_button = dry
        in_app = QPushButton(tr("Start in the app"))
        in_app.setProperty("role", "primary")  # the main action of the page
        in_app.setToolTip(tr("In a terminal tab of this window (Terminals)"))
        in_app.clicked.connect(self.start_in_app)
        start = QPushButton(tr("Start in a terminal window"))
        start.clicked.connect(lambda: self.start())
        self.session = QLineEdit()
        self.session.setPlaceholderText(tr("session name"))
        self.session.setMaximumWidth(160)
        session = QPushButton(tr("Start as session"))
        session.setToolTip(tr("A detachable session: attach and detach from the Sandboxes page"))
        session.clicked.connect(self.start_session)
        write = QPushButton(tr("Save as project configuration…"))
        write.setToolTip(tr("Write .sandbox-ai.json into the first workspace and trust it"))
        write.clicked.connect(self.write_project)
        layout.addWidget(flow(dry, in_app, start, self.session, session, write))
        self.form.changed.connect(self.update_preview)
        self.agent.currentTextChanged.connect(self.update_preview)
        self.agent_args.textChanged.connect(self.update_preview)
        self.refresh_profiles()
        self.update_preview()

    # -- kept for the next start --

    def attach_state(self, state):
        """The agent and profile chosen, and per agent its last options and agent arguments, come back at the
        next start (the workspace follows the project; agent arguments that may be secrets are left out)."""
        self.state = state
        saved = state.section("launch")
        if saved.get("agent") in self.sai.agents:
            self.agent.setCurrentText(saved["agent"])
        if isinstance(saved.get("profile"), str) and self.profile.findText(saved["profile"]) >= 0:
            self.profile.setCurrentText(saved["profile"])
        self.current_agent = self.agent.currentText()
        self.load_agent_form(self.current_agent)
        self.agent.currentTextChanged.connect(self.agent_switched)
        self.form.changed.connect(state.schedule)
        self.agent_args.textChanged.connect(state.schedule)
        self.profile.currentTextChanged.connect(state.schedule)
        state.collect(self.keep_form)

    def forms(self):
        saved = self.state.section("launch")
        if not isinstance(saved.get("forms"), dict):
            saved["forms"] = {}
        return saved["forms"]

    def form_state(self):
        values = self.form.values()
        values.pop("--workspace", None)  # the project's
        return {"options": backend.argv_from_values(self.options, values),
                "arguments": scrub_arguments(self.agent_arguments())}

    def keep_form(self):
        saved = self.state.section("launch")
        saved["agent"], saved["profile"] = self.agent.currentText(), self.profile.currentText()
        self.forms()[self.agent.currentText()] = self.form_state()

    def agent_switched(self, agent):
        """Another agent: the form keeps this agent's options and shows the other's last ones (if any)."""
        self.forms()[self.current_agent] = self.form_state()
        self.current_agent = agent
        self.load_agent_form(agent)

    def load_agent_form(self, agent):
        saved = self.forms().get(agent)
        if not isinstance(saved, dict):
            return
        options = [str(o) for o in saved.get("options", []) if isinstance(o, str)] if isinstance(
            saved.get("options"), list) else []
        arguments = [str(a) for a in saved.get("arguments", []) if isinstance(a, str)] if isinstance(
            saved.get("arguments"), list) else []
        values, _ = backend.values_from_argv(self.options, options, self.sai.launcher.canonical_options)
        values["--workspace"] = self.form.values().get("--workspace") or []
        self.form.set_values(values)
        self.agent_args.setText(shlex.join(scrub_arguments(arguments)))

    def set_project(self, path):
        """Start in the project PATH: its folder becomes the first (and only) workspace. Its .sandbox-ai.json is
        never copied into the form: the launcher applies a trusted one itself."""
        self.project = path
        widget = self.form.widgets.get("--workspace")
        if widget is not None:
            widget.set_value([path])
        self.show_project_note()

    def show_project_note(self):
        """The note on the project's .sandbox-ai.json; its state is read in a thread (a slow disk)."""
        self.note_token = getattr(self, "note_token", 0) + 1
        if not self.project:
            self.project_note.setText("")
            self.review_button.hide()
            return
        token, project = self.note_token, self.project
        background(self.sai.project_state, project, timeout=STATE_WAIT,
                   done=lambda result, error: self.project_note_read(token, project, result, error))

    def project_note_read(self, token, project, result, error):
        if token != self.note_token or project != self.project:
            return
        state = result[0] if error is None else "unreadable"
        texts = {"none": tr("No .sandbox-ai.json in this project: the options below are all there is."),
                 "trusted": tr("The project's trusted .sandbox-ai.json is applied by the launcher on top of these "
                               "options."),
                 "changed": tr("The project's .sandbox-ai.json changed since you trusted it: the launcher refuses "
                               "to start until you review it."),
                 "untrusted": tr("The project's .sandbox-ai.json is not trusted yet: the launcher refuses to start "
                                 "until you review it."),
                 "unreadable": tr("The project's .sandbox-ai.json cannot be read.")}
        self.project_note.setText(tr("Project {path}", path=printable(self.project)) + " · " + texts[state])
        self.project_note.setProperty("role", "notice-warn" if state in ("changed", "untrusted", "unreadable")
                                      else "secondary")
        self.project_note.style().unpolish(self.project_note)
        self.project_note.style().polish(self.project_note)
        self.review_button.setVisible(state in ("changed", "untrusted"))

    def argv(self):
        return backend.argv_from_values(self.options, self.form.values())

    def workspace(self):
        """The first workspace of the form, or None."""
        values = self.form.values().get("--workspace") or []
        first = next((w.strip() for value in values for w in str(value).split(",") if w.strip()), None)
        return first

    def agent_arguments(self):
        try:
            return shlex.split(self.agent_args.text())
        except ValueError:
            return []

    def update_preview(self):
        args = self.agent_arguments()
        line = ["sandbox-ai", self.agent.currentText(), *self.argv(), *(["--", *args] if args else [])]
        self.preview.setPlainText(shlex.join(line))

    def refresh_profiles(self):
        self.profile.clear()
        self.profile.addItems(sorted(self.sai.profiles()))

    def set_from_argv(self, agent, argv, agent_args=()):
        values, unknown = backend.values_from_argv(self.options, argv, self.sai.launcher.canonical_options)
        self.agent.setCurrentText(agent)
        self.form.set_values(values)
        self.agent_args.setText(shlex.join(agent_args))
        if unknown:
            QMessageBox.warning(self, tr("Options not shown"), tr("These are not options of this sandbox-ai version "
                                                                  "and were left out:") + "\n" + " ".join(unknown))

    def load_profile(self):
        name = self.profile.currentText()
        profile = self.sai.profiles().get(name)
        if profile:
            # A profile without a workspace keeps the project's: else the start would run in whatever folder the
            # terminal opens in (the launcher's default workspace is the current directory).
            kept = self.form.values().get("--workspace") or []
            self.set_from_argv(profile.get("agent", "claude"), profile.get("options", []), profile.get("agent_args", []))
            if kept and not self.form.values().get("--workspace"):
                self.form.widgets["--workspace"].set_value(kept)

    def save_profile(self):
        name, ok = QInputDialog.getText(self, tr("Save profile"), tr("Name of the profile:"),
                                        text=self.profile.currentText())
        if ok and name.strip():
            self.sai.save_profile(name.strip(), self.agent.currentText(), self.argv(), self.agent_arguments())
            self.refresh_profiles()
            self.profile.setCurrentText(name.strip())

    def load_project(self, directory=None):
        """Review a folder's .sandbox-ai.json (written there by whoever could write the folder, the agent
        too) and trust exactly the bytes shown if the human agrees. The file's options are never copied into
        the form: the launcher applies a trusted file itself when the folder is the first workspace, and
        refuses it if it changes afterwards."""
        directory = directory or choose_folder(self, tr("Project folder with .sandbox-ai.json"), "project")
        if not directory:
            return False
        try:
            review = self.sai.review_project(directory)
        except FileNotFoundError:
            warn(self, tr("No project configuration"), tr("{path} has no .sandbox-ai.json.", path=printable(directory)))
            return False
        except (OSError, ValueError) as exc:
            warn(self, tr("No project configuration"), printable(exc))
            return False
        lines = [tr("Agent: {agent}", agent=printable(review.agent or "?"))]
        if review.widening:
            lines.append(tr("It widens what the sandbox may do or see:"))
            lines += [f"  {name} {value}".rstrip() for name, value in review.widening]
        if review.workspaces:
            lines.append(tr("Further workspaces: {list}", list=", ".join(printable(w) for w in review.workspaces)))
        if review.agent_args:
            lines.append(tr("Agent arguments: {list}", list=" ".join(printable(a) for a in review.agent_args)))
        if not review.usable:
            warn(self, tr("This project configuration cannot be used"),
                 "\n".join([*(printable(problem) for problem in review.problems), "", *lines]), details=review.text())
            return False
        if not review.trusted:
            state = (tr("{path} changed since you trusted it.", path=printable(review.path)) if review.changed else
                     tr("{path} is not trusted yet.", path=printable(review.path)))
            question = "\n".join([state, "", *lines, "", tr("Trust exactly this content (see the details)? The "
                                                            "launcher refuses the file if it changes afterwards.")])
            if not confirm(self, tr("Review the project configuration"), question, tr("Trust"),
                           destructive=bool(review.widening), details=review.text()):
                return False
            try:
                self.sai.trust_project(review)
            except (OSError, ValueError) as exc:
                warn(self, tr("Not trusted"), printable(exc))
                return False
        self.set_from_argv(review.agent, ["--workspace", str(directory)])
        inform(self, tr("Project configuration"), tr("Trusted: {path}. The form starts in that folder; the launcher "
                                                     "applies the file's options itself.", path=printable(review.path)))
        self.opened.emit(str(directory))
        self.show_project_note()
        return True

    def dry_run(self):
        background(self.sai.dry_run, self.agent.currentText(), self.argv(), owner=self, source=self.dry_button,
                   timeout=90, done=self.dry_run_done)

    def dry_run_done(self, result, error):
        plan, message = result if error is None else (None, str(error))
        if plan is None:
            warn(self, tr("The launcher refused these options"), message)
            return
        show_text(self, tr("Plan (dry run)"), (message + "\n\n" if message else "") + json.dumps(plan, indent=2))

    def start(self, session=None, embedded=False):
        try:
            argv = self.sai.launch_argv(self.agent.currentText(), self.argv(), self.agent_arguments(), session)
            title = f"{self.agent.currentText()}" + (f" ({session})" if session else "")
            self.terminal(argv, title, embedded=embedded, cwd=self.workspace())
        except Exception as exc:  # noqa: BLE001 - a start that cannot be run is a message, the window stays
            warn(self, tr("Not started"), printable(exc))
            return False
        return True

    def start_in_app(self):
        self.start(embedded=True)

    def start_session(self):
        name = self.session.text().strip()
        if not name:
            QMessageBox.information(self, tr("Session"), tr("Give the session a name first."))
            return
        self.start(session=name, embedded=None)

    def write_project(self):
        document, main = self.sai.project_document(self.agent.currentText(), self.options, self.form.values(),
                                                   self.agent_arguments())
        if not main:
            QMessageBox.information(self, tr("Project configuration"),
                                    tr("Choose a workspace first: the file is written there."))
            return
        text = json.dumps(document, indent=2)
        answer = QMessageBox.question(self, tr("Write and trust"), tr("Write {path} with this content and trust it?",
                                                                      path=f"{main}/.sandbox-ai.json") + f"\n\n{text}")
        if answer == QMessageBox.StandardButton.Yes:  # the folder may be slow (a network file system): in a thread
            background(self.sai.write_project, main, document, owner=self, timeout=60, done=self.project_written)

    def project_written(self, path, error):
        if error is not None:
            warn(self, tr("Not written"), printable(error))
            return
        inform(self, tr("Project configuration"), tr("Written and trusted: {path}", path=printable(path)))


GIT_URL_EXAMPLE = "https://git.example.org"  # the same in every language


class NetworkDialog(QDialog):
    def __init__(self, parent, presets, title, supported=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.supported = set(supported or ())
        form = QFormLayout(self)
        self.hosts = QLineEdit()
        self.hosts.setPlaceholderText("api.example.org pypi.org")
        self.tcp = QLineEdit()
        self.tcp.setPlaceholderText("192.0.2.10:5432")
        self.nets = QLineEdit()
        self.nets.setPlaceholderText("192.0.2.0/24")
        form.addRow(tr("Host names (TCP/443)"), self.hosts)
        form.addRow(tr("IP:PORT (TCP)"), self.tcp)
        form.addRow(tr("Addresses or networks (every port)"), self.nets)
        # What later versions of sandbox-ai can change during the run: shown only where the installed one can.
        self.host_ports = QLineEdit()
        self.host_ports.setPlaceholderText("3000 11434")
        self.host_ports.setToolTip(safe_tooltip(tr("Ports on this computer's loopback (a local Git server, an LLM "
                                                   "server), reached through a relay that only you start")))
        self.dns = QCheckBox(tr("Real DNS lookups (each one logged; addresses stay closed)"))
        self.git_url = QLineEdit()
        self.git_url.setPlaceholderText(GIT_URL_EXAMPLE)
        self.git_kind = QComboBox()
        for kind, label in (("env", tr("variable")), ("secret", tr("secret")), ("file", tr("file"))):
            self.git_kind.addItem(label, kind)
        self.git_ref = QLineEdit()
        self.git_ref.setPlaceholderText(tr("NAME of the variable or secret, or the file"))
        git_row = QHBoxLayout()
        for widget in (self.git_url, self.git_kind, self.git_ref):
            git_row.addWidget(widget, 2 if widget is not self.git_kind else 0)
        if "--allow-host-port" in self.supported:
            form.addRow(tr("Ports on this computer"), self.host_ports)
        if "--dns" in self.supported:
            form.addRow(tr("DNS"), self.dns)
        if "--git-token-env" in self.supported:
            form.addRow(tr("Git login (the token, never shown)"), git_row)
        self.presets = {}
        for name, hosts in presets.items():
            box = QCheckBox(", ".join(hosts)[:80])
            form.addRow(f"--allow-{name}", box)
            self.presets[name] = box
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def arguments(self):
        args = []
        args += [t for host in self.hosts.text().split() for t in ("--allow-host", host)]
        args += [t for target in self.tcp.text().split() for t in ("--allow-tcp", target)]
        args += [t for net in self.nets.text().split() for t in ("--allow", net)]
        args += [f"--allow-{name}" for name, box in self.presets.items() if box.isChecked()]
        if "--allow-host-port" in self.supported:
            args += [t for port in self.host_ports.text().split() for t in ("--allow-host-port", port)]
        if "--dns" in self.supported and self.dns.isChecked():
            args.append("--dns")
        url, ref = self.git_url.text().strip(), self.git_ref.text().strip()
        if "--git-token-env" in self.supported and url and ref:
            args += [f"--git-token-{self.git_kind.currentData()}", f"{url}={ref}"]
        return args


DEVICE_EXAMPLE = "/dev/..."  # the same in every language


class DeviceDialog(QDialog):
    """A device for a running sandbox: the devices under /dev, refused ones greyed out with the launcher's
    reason, warned ones with what they expose; or a path typed in."""

    def __init__(self, parent, candidates):
        super().__init__(parent)
        self.setWindowTitle(tr("Add device"))
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(DEVICE_EXAMPLE)
        for path, why, note in candidates:
            item = QListWidgetItem(path + (f"  ({tr('refused')}: {why})" if why else f"  ({note})" if note else ""))
            item.setData(Qt.ItemDataRole.UserRole, path)
            if why:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(tr("sandbox-ai refuses it: {why}", why=why))
            self.list.addItem(item)
        self.list.itemSelectionChanged.connect(lambda: self.edit.setText(
            self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else self.edit.text()))
        layout.addWidget(plain_label(tr("Devices weaken the isolation: a service on the host reached through a host "
                                        "port is safer. Refused devices are greyed out.")))
        layout.addWidget(self.list, 1)
        layout.addWidget(self.edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 480)

    def path(self):
        return self.edit.text().strip()


class LimitsDialog(QDialog):
    def __init__(self, parent, run):
        super().__init__(parent)
        self.setWindowTitle(tr("Limits of {run}", run=printable(run)))
        form = QFormLayout(self)
        form.addRow(plain_label(tr("For the rest of the run; empty: unchanged, none: no limit.")))
        self.memory, self.cpus, self.pids = QLineEdit(), QLineEdit(), QLineEdit()
        for label, edit in ((tr("Memory (e.g. 8g)"), self.memory), (tr("CPUs (e.g. 2 or 1.5)"), self.cpus),
                            (tr("Processes"), self.pids)):
            form.addRow(label, edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.memory.text().strip(), self.cpus.text().strip(), self.pids.text().strip()


KIND_TITLES = {"run": N_("Run"), "session": N_("Session"), "mcp": N_("MCP server"), "unregistered": N_("Not registered")}
STATE_TITLES = {"running": N_("running"), "paused": N_("paused"), "stopped": N_("stopped"), "damaged": N_("damaged"),
                "starting": N_("starting")}
SESSION_ACTIONS = (("attach", N_("Attach")), ("attach-app", N_("Attach in the app")), ("pause", N_("Pause")),
                   ("resume", N_("Resume")), ("delete", N_("Delete")))


def same_folder(a, b):
    """True if the folders A and B are the same (as resolved paths); False when either is empty."""
    if not a or not b:
        return False
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))


class SandboxesPage(QWidget):
    """Every sandbox-ai instance of this account in one table: one-off runs (started here, in a terminal, by a
    script or another copy of the app), sessions (also paused and stopped ones), MCP server sandboxes, and
    launchers that have not registered yet. The actions follow the kind of the selected row; what does not
    apply is greyed out with the reason in its tooltip. Read again every few seconds in the background."""
    open_log = Signal(str)
    counted = Signal(int, int)  # instances that run, of them in the current project
    updated = Signal(object)  # {key: Instance} after each read (the window's tabs follow it)

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.project, self.instances, self.problems, self.stoppable = None, {}, [], None
        self.reading, self.added, self.devices_token = None, [], 0
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.summary = plain_label(role="secondary")
        self.only_project = QCheckBox(tr("Only the current project"))
        self.only_project.toggled.connect(lambda _=False: self.show_instances())
        refresh = QPushButton(tr("Refresh"))
        refresh.setToolTip(tr("Read again now (it is read every few seconds anyway)"))
        refresh.clicked.connect(lambda _=False: self.refresh())
        top.addWidget(self.summary, 1)
        top.addWidget(self.only_project)
        top.addWidget(refresh)
        layout.addLayout(top)
        self.table = table([tr("Kind"), tr("Name"), tr("Project"), tr("Agent"), tr("Backend"), tr("Started"),
                            tr("State"), tr("Proxy")], stretch=2)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, 1)
        below = QHBoxLayout()
        self.detail = plain_label(role="secondary")
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.stop_button = QPushButton(tr("Stop…"))
        self.stop_button.setProperty("role", "danger")
        self.stop_button.clicked.connect(lambda _=False: self.stop())
        below.addWidget(self.detail, 1)
        below.addWidget(self.stop_button)
        layout.addLayout(below)
        # The sessions' own actions (sandbox-ai session ...).
        session_box = QGroupBox(tr("Session"))
        session_row = QVBoxLayout(session_box)
        self.session_buttons = {}
        for action, text in SESSION_ACTIONS:
            button = QPushButton(tr(text))
            button.clicked.connect(lambda _=False, a=action: self.session(a))
            self.session_buttons[action] = button
        session_row.addWidget(flow(*self.session_buttons.values()))
        # What a running sandbox may reach (sandbox-ai net), its devices and limits (until it ends).
        run_box = QGroupBox(tr("While it runs"))
        run_layout = QVBoxLayout(run_box)
        self.run_buttons = []
        for text, tip, slot in ((tr("Allow…"), tr("Allow more destinations until the sandbox ends (sandbox-ai net allow)"),
                                 lambda: self.network("allow")),
                                (tr("Revoke…"), tr("Take back what was allowed during the run (sandbox-ai net revoke)"),
                                 lambda: self.network("revoke")),
                                (tr("Allowed during the run"), tr("What was allowed since the start (sandbox-ai net show)"),
                                 self.show_allowed),
                                (tr("Connection log"), tr("The connection proxy's log of its project"), self.log),
                                (tr("Add device…"), tr("Hand a host device to the selected sandbox until it ends"),
                                 self.add_device),
                                (tr("Remove device"), tr("Take back a device added during the run"), self.remove_device),
                                (tr("Limits…"), tr("Change memory, CPUs and processes of the selected sandbox"),
                                 self.limits)):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, s=slot: s())
            self.run_buttons.append(button)
        run_layout.addWidget(flow(*self.run_buttons))
        self.devices_label = plain_label(role="secondary")
        run_layout.addWidget(self.devices_label)
        boxes = QHBoxLayout()
        boxes.addWidget(session_box, 2)
        boxes.addWidget(run_box, 5)
        layout.addLayout(boxes)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_soon)
        self.timer.start(backend.setting("sandboxes_interval"))
        self.refresh()

    # -- reading --

    def refresh(self):
        """Read now, on this thread (the registry and /proc of this user: small files)."""
        try:
            result, error = self.sai.instances(), None
        except Exception as exc:  # noqa: BLE001 - shown, read again
            result, error = None, exc
        self.apply(result, error)

    def refresh_soon(self):
        """The timer's way: read in a thread, one read at a time."""
        if self.reading is not None:
            return
        self.reading = background(self.sai.instances, timeout=60, done=self.read_done)

    def read_done(self, result, error):
        self.reading = None
        self.apply(result, error)

    def apply(self, result, error):
        if self.stoppable is None:
            try:
                self.stoppable = "stop" in self.sai.net_commands()
            except Exception:  # noqa: BLE001
                self.stoppable = False
        if error is not None:
            found, self.problems = [], [str(error)]
        else:
            found, self.problems = result
        self.instances = {instance.key: instance for instance in found}
        self.show_instances()
        self.updated.emit(self.instances)

    def set_project(self, path):
        self.project = path
        self.show_instances()

    def attach_state(self, state):
        """The row selected last is selected again once it is there (kept for the next start)."""
        self.state = state
        self.wanted = state.get("sandboxes-selected", "")
        self.show_instances()

    def mine(self, instance):
        return same_folder(instance.project, self.project)

    def show_instances(self):
        shown = [i for i in self.instances.values() if not self.only_project.isChecked() or self.mine(i)]
        order = {"run": 0, "session": 1, "mcp": 2, "unregistered": 3}
        shown.sort(key=lambda i: (not self.mine(i), order.get(i.kind, 9), str(i.name)))
        rows, colors, tips = [], [], []
        for i in shown:
            rows.append([tr(KIND_TITLES.get(i.kind, i.kind)), printable(i.name), printable(i.project), printable(i.agent),
                         printable(i.backend), local_time(i.started), tr(STATE_TITLES.get(i.state, i.state)) if i.state
                         else "", "" if i.proxy is None else tr("on") if i.proxy else tr("off")])
            colors.append({"running": "ok", "paused": "warn", "damaged": "bad", "starting": "warn"}.get(i.state))
            tips.append(tr("Current project") if self.mine(i) else None)
        fill(self.table, rows, colors, tooltips=tips, keys=[i.key for i in shown])
        if getattr(self, "wanted", "") and not selected_rows(self.table) and find_row(self.table, self.wanted) >= 0:
            self.table.selectRow(find_row(self.table, self.wanted))
        for row in range(self.table.rowCount()):
            instance = self.instances.get(row_key(self.table, row))
            if instance is not None and self.mine(instance):
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
        fit_columns(self.table)  # bold text is wider
        running = [i for i in self.instances.values() if i.state in ("running", "paused", "starting")]
        here = [i for i in running if self.mine(i)]
        text = tr("{count} running on this account", count=len(running))
        if self.project:
            text += " · " + tr("{count} in the current project", count=len(here))
        if self.problems:
            text += " · " + tr("not all could be read: {problems}", problems=printable("; ".join(self.problems))[:300])
        self.summary.setText(text)
        self.counted.emit(len(running), len(here))
        self.selection_changed()

    def current(self):
        rows = selected_rows(self.table)
        return self.instances.get(row_key(self.table, rows[0])) if rows else None

    # -- buttons --

    def selection_changed(self):
        instance = self.current()
        if instance is not None and getattr(self, "state", None) is not None:
            self.wanted = instance.key
            self.state.set("sandboxes-selected", instance.key)
        if instance is None:
            self.detail.setText(tr("Select a row: the buttons below act on it."))
        else:
            parts = [tr("Run {run}", run=printable(instance.run)) if instance.run else tr("not in the run registry")]
            if instance.pid:
                parts.append(f"PID {instance.pid}")
            if instance.project:
                parts.append(printable(instance.project))
            self.detail.setText(" · ".join(parts))
        self.update_buttons()
        self.load_devices()

    def update_buttons(self):
        if busy(self):
            return  # a command runs: its buttons stay greyed out until it is done
        instance = self.current()
        none = tr("Select a row first.")
        kind = instance.kind if instance else None
        state = instance.state if instance else None
        run_why = (none if instance is None else
                   tr("It has not registered with sandbox-ai (yet): starting, or started by an older sandbox-ai. "
                      "End it in its terminal.") if kind == "unregistered" else
                   tr("It does not run.") if not instance.run else None)
        for button in self.run_buttons:
            set_enabled(button, run_why is None, run_why)
        rules = {"attach": state == "running", "attach-app": state == "running", "pause": state == "running",
                 "resume": state in ("paused", "stopped"), "delete": state == "stopped"}
        foreground = bool(instance is not None and getattr(instance, "foreground", False))
        for action, button in self.session_buttons.items():
            why = (none if instance is None else tr("Only for sessions.") if kind != "session" else
                   tr("It runs in the foreground of its own terminal (started with --no-tmux or without a terminal): "
                      "there is nothing to attach to.") if foreground and action in ("attach", "attach-app") else
                   None if rules[action] else tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state))))
            set_enabled(button, why is None, why)
        if instance is None:
            why = none
        elif kind == "unregistered":
            why = run_why
        elif kind == "session":
            why = None if state in ("running", "paused") else tr("The session does not run.")
        elif kind == "mcp":
            why = None
        elif not self.stoppable:
            why = tr("This sandbox-ai cannot stop a run from outside (sandbox-ai net stop is missing; update it). End "
                     "the run in its terminal.")
        else:
            why = None
        set_enabled(self.stop_button, why is None, why, tooltip=tr("End the selected sandbox: a session (its state is "
                                                                  "kept), an MCP server, or a run (sandbox-ai net stop)"))

    def stop(self, instance=None):
        instance = instance or self.current()
        if instance is None:
            return
        what = {"session": tr("session {name}", name=printable(instance.name)),
                "mcp": tr("MCP server {name}", name=printable(instance.name))}.get(instance.kind,
                                                                                  tr("run {name}", name=printable(instance.name)))
        text = tr("Stop {what}? Its agent ends; what it did not save is lost.", what=what)
        if instance.kind == "session":
            text = tr("Stop {what}? It ends; its HOME is kept and Resume starts it again.", what=what)
        elif instance.kind == "mcp":
            text = tr("Stop {what}? Agents that use it lose it until it is started again.", what=what)
        if not confirm(self, tr("Stop"), text, tr("Stop"), destructive=True):
            return
        if instance.kind == "session":
            call = (self.sai.call, "session", "stop", instance.name)
        elif instance.kind == "mcp":
            call = (self.sai.stop_mcp, instance.mcp)
        else:
            call = (self.sai.stop_run, instance.run)
        background(*call, owner=self, source=[self.stop_button, *self.session_buttons.values(), *self.run_buttons],
                   timeout=330, done=lambda result, error: self.command_done(tr("Stop"), result, error))

    def command_done(self, title, result, error, quiet_success=True):
        status, out, err = outcome(result, error)
        if status != 0 or not quiet_success:
            (inform if status == 0 else warn)(self, title, (out + err).strip())
        self.refresh()

    # -- devices and limits --

    def load_devices(self):
        """The devices added to the selected sandbox during its run (in the background)."""
        instance = self.current()
        run = instance.run if instance else None
        self.devices_token += 1
        token = self.devices_token
        if not run:
            self.added = []
            self.devices_label.setText("")
            return
        background(self.sai.devices_added, run, timeout=30,
                   done=lambda found, error: self.devices_loaded(token, found, error))

    def devices_loaded(self, token, found, error):
        if token != self.devices_token:
            return
        self.added = list(found or []) if error is None else []
        if isinstance(error, AttributeError):
            self.devices_label.setText(tr("This sandbox-ai version cannot change devices during a run."))
        else:
            self.devices_label.setText(tr("Added during the run: {list}", list=", ".join(map(printable, self.added)))
                                       if self.added else tr("No device added during the run."))

    def add_device(self):
        run = self.current_run()
        if run:
            background(self.sai.device_candidates, owner=self, source=self.run_buttons, timeout=30,
                       done=lambda found, error: self.choose_device(run, found or []))

    def choose_device(self, run, candidates):
        self.update_buttons()
        dialog = DeviceDialog(self, candidates)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.path():
            return
        path = dialog.path()
        background(self.sai.device_check, path, owner=self, source=self.run_buttons, timeout=30,
                   done=lambda result, error: self.confirm_device(run, path, result, error))

    def confirm_device(self, run, path, result, error):
        """The launcher's own notes on the device (what it exposes, udev, Bluetooth) before it is added."""
        self.update_buttons()
        if error is not None:
            warn(self, tr("Not added"), printable(error))
            return
        resolved, notes = result
        text = "\n".join([tr("Give {path} to {run} until it ends? Devices weaken the isolation.",
                             path=printable(resolved), run=printable(run)), ""] + [printable(n) for n in notes])
        if confirm(self, tr("Add device"), text, tr("Add"), destructive=True):
            background(self.sai.add_device, run, path, owner=self, source=self.run_buttons, timeout=150,
                       done=lambda result, error: self.device_done(tr("Add device"), result, error))

    def remove_device(self):
        run = self.current_run()
        if not run:
            return
        if not self.added:
            inform(self, tr("Remove device"), tr("No device was added to {run} during its run (devices given at the "
                                                 "start stay).", run=printable(run)))
            return
        path = self.added[0]
        if len(self.added) > 1:
            path, ok = QInputDialog.getItem(self, tr("Remove device"), tr("Device"), self.added, 0, False)
            if not ok:
                return
        background(self.sai.remove_device, run, path, owner=self, source=self.run_buttons, timeout=150,
                   done=lambda result, error: self.device_done(tr("Remove device"), result, error))

    def device_done(self, title, result, error):
        report(self, title, result, error)
        self.update_buttons()
        self.load_devices()

    def limits(self):
        run = self.current_run()
        if not run:
            return
        dialog = LimitsDialog(self, run)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        memory, cpus, pids = dialog.values()
        background(self.sai.set_limits, run, memory, cpus, pids, owner=self, source=self.run_buttons, timeout=150,
                   done=lambda result, error: self.reported(tr("Limits"), result, error))

    def reported(self, title, result, error):
        report(self, title, result, error)
        self.update_buttons()

    def current_run(self):
        instance = self.current()
        run = instance.run if instance else None
        if not run:
            QMessageBox.information(self, tr("Sandboxes"), tr("Select a running sandbox first."))
        return run

    def network(self, op):
        run = self.current_run()
        if not run:
            return
        title = tr("Allow for {run}", run=run) if op == "allow" else tr("Revoke for {run}", run=run)
        dialog = NetworkDialog(self, self.sai.presets(), title, self.sai.net_options(op))
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.arguments():
            return
        background(self.sai.call, "net", op, run, *dialog.arguments(), owner=self, source=self.run_buttons, timeout=150,
                   done=lambda result, error: self.reported(f"net {op}", result, error))

    def show_allowed(self):
        run = self.current_run()
        if run:
            background(self.sai.call, "net", "show", run, owner=self, source=self.run_buttons, timeout=150,
                       done=lambda result, error: self.allowed_shown(run, result, error))

    def allowed_shown(self, run, result, error):
        inform(self, run, "\n".join(outcome(result, error)[1:]).strip())
        self.update_buttons()

    def log(self):
        instance = self.current()
        if instance is not None and instance.project:
            self.open_log.emit(instance.project)

    # -- sessions --

    def session(self, action, instance=None):
        instance = instance or self.current()
        if instance is None or instance.kind != "session":
            QMessageBox.information(self, tr("Sessions"), tr("Select a session first."))
            return
        name = instance.session
        if action in ("attach", "attach-app"):
            try:
                self.terminal(self.sai.command("session", "attach", name), tr("session {name}", name=name),
                              embedded=action == "attach-app")
            except Exception as exc:  # noqa: BLE001 - attaching that cannot be run is a message, the window stays
                warn(self, tr("Not attached"), printable(exc))
            return
        if action == "delete" and QMessageBox.question(
                self, tr("Delete"), tr("Delete session {name} and its HOME?", name=name)) != QMessageBox.StandardButton.Yes:
            return
        background(self.sai.call, "session", action, name, *(["--yes"] if action == "delete" else []), owner=self,
                   source=[self.stop_button, *self.session_buttons.values()], timeout=150,
                   done=lambda result, error: self.command_done(f"session {action}", result, error))


class ConnectionsPage(QWidget):
    """The connection proxy's log: every request, push and refusal. While "follow" is on and the page is
    shown, the log is read again when it changed (log_state: inode, size, time), never more often."""

    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        self.directory = QComboBox()
        self.directory.setMinimumWidth(320)
        self.directory.currentIndexChanged.connect(self.list_files)
        browse = QPushButton(tr("Other folder…"))
        browse.clicked.connect(self.browse)
        self.file = QComboBox()
        self.file.setMinimumWidth(280)
        self.file.currentIndexChanged.connect(lambda _=0: self.load())
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("filter (host, URL, event)"))
        self.filter.setMinimumWidth(220)
        self.filter.textChanged.connect(lambda _="": self.show_records())
        self.refused = QCheckBox(tr("only refused and errors"))
        self.refused.toggled.connect(lambda _=False: self.show_records())
        self.follow = QCheckBox(tr("follow"))
        self.follow.setChecked(True)
        layout.addWidget(flow(QLabel(tr("Logs")), self.directory, browse, QLabel(tr("Run")), self.file, self.filter,
                              self.refused, self.follow))
        split = self.split = QSplitter(Qt.Orientation.Vertical)
        self.table = table([tr("Time"), tr("Event"), tr("Method"), tr("Status"), tr("Target"), tr("Bytes")])
        self.table.itemSelectionChanged.connect(self.details)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([400, 220])
        layout.addWidget(split, 1)
        # base: the key of records[0]; keys stay with their records when old ones drop out of the window read.
        self.records, self.shown, self.state, self.base = [], [], None, 0
        self.position = None  # (path, (inode, offset)) where the last read ended: a follow reads only what came
        self.reading = None  # the path being looked at in a thread (log_state, then up to 16 MiB of log)
        self.listing = 0  # the newest listing of folders or logs (an older one that comes back late is dropped)
        self.wanted_log = None  # the end of the name of the log to choose once the logs are listed
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(backend.setting("connections_interval"))
        self.refresh_directories()

    def refresh_directories(self, select=None, log_suffix=None):
        """The log folders, listed in a thread (a slow disk must not hold up a tab switch); SELECT: the folder
        to choose, LOG_SUFFIX: the end of the name of the log to choose there (e.g. "-RUN.jsonl")."""
        self.wanted_log = log_suffix
        current = select or self.directory.currentData()
        self.listing += 1
        token = self.listing
        background(self.sai.log_directories, timeout=STATE_WAIT,  # a look, not work quitting would interrupt
                   done=lambda found, error: self.directories_listed(token, current, found or [], error))

    def directories_listed(self, token, current, found, error):
        if token != self.listing:
            return
        self.directory.blockSignals(True)
        self.directory.clear()
        for path in found:
            self.directory.addItem(path.name, str(path))
        if current and self.directory.findData(str(current)) < 0:
            self.directory.addItem(Path(current).name, str(current))
        if current:
            self.directory.setCurrentIndex(self.directory.findData(str(current)))
        self.directory.blockSignals(False)
        self.list_files()

    def browse(self):
        path = choose_folder(self, tr("Log folder"), "logs")
        if path:
            self.refresh_directories(select=path)

    def open_for_workspace(self, workspace):
        if workspace:
            self.refresh_directories(select=str(self.sai.launcher.default_log_dir(Path(workspace))))

    def list_files(self):
        """The logs of the chosen folder, listed in a thread."""
        directory = self.directory.currentData()
        self.listing += 1
        token = self.listing

        def look():
            return sorted(Path(directory).glob("connections-*.jsonl"), reverse=True) if directory else []
        background(look, timeout=STATE_WAIT, done=lambda found, error: self.files_listed(token, found or [], error))

    def files_listed(self, token, found, error):
        if token != self.listing:
            return
        self.file.blockSignals(True)
        self.file.clear()
        for path in found:
            self.file.addItem(path.name, str(path))
        wanted, self.wanted_log = self.wanted_log, None
        if wanted:
            index = next((i for i in range(self.file.count()) if self.file.itemText(i).endswith(wanted)), -1)
            if index >= 0:
                self.file.setCurrentIndex(index)
        self.file.blockSignals(False)
        self.load()

    def tick(self):
        if self.follow.isChecked() and self.isVisible():
            self.load()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.load()

    def load(self):
        """Look at the chosen log in a thread - its state (inode, size, time: a stat may hang on a network file
        system, so not on the GUI thread) and, if that changed since it was last read, its records. At most
        one look at a time: while one has not come back (even a stuck one), no other starts."""
        if self.reading is not None:
            return
        path = self.file.currentData()
        if path is None:
            if self.state is not None or self.records:
                self.apply(None, [])
            return
        known = self.state
        since = self.position[1] if self.position and self.position[0] == path and self.records else None

        def look():
            state = (path, backend.log_state(path))
            if state == known:
                return state, None  # unchanged: nothing read
            return state, backend.read_connections(path, strict=True, since=since, track=True)
        self.reading = path
        background(look, owner=self, timeout=60, done=lambda result, error: self.loaded(path, result, error))

    def loaded(self, path, result, error):
        self.reading = None
        if path != self.file.currentData():
            self.load()  # another log was chosen meanwhile
            return
        if error is not None:
            # Not recorded as read: the next tick (or load) tries again.
            self.detail.setPlainText(tr("Cannot read {path}: {error}", path=printable(path), error=printable(error)))
            return
        state, read = result
        if read is None:
            return
        records, position, appended = read
        self.position = (path, position) if position else None
        # What was appended meanwhile is read at the next tick (connections_interval), not at once: a log that
        # grows all the time would otherwise be read and shown again and again, on the GUI thread too.
        if appended:
            self.append(state, records)
        else:
            self.apply(state, records)

    def append(self, state, records):
        """RECORDS were appended to the log shown: added to the table, the oldest beyond LIMIT dropped."""
        self.state = state
        if not records:
            return
        start = self.base + len(self.records)
        self.records = self.records + records
        dropped = max(0, len(self.records) - CONNECTIONS_LIMIT)
        if dropped:
            self.records = self.records[dropped:]
            self.base += dropped
        rows = self.rows(records, start)
        base = self.base
        if not append_rows(self.table, [row for row, _, _, _ in rows],
                           ["bad" if bad else None for _, _, bad, _ in rows], keys=[key for _, _, _, key in rows],
                           drop=lambda key: key is None or key < base):
            self.show_records()
            return
        self.shown = [entry for entry in self.shown if entry[3] >= base] + rows
        if self.follow.isChecked() and rows:
            self.table.scrollToBottom()

    def apply(self, state, records):
        path = state[0] if state else None
        if self.state is not None and state is not None and self.state[0] == path and self.records:
            last = self.records[-1]
            at = next((i for i in range(len(records) - 1, -1, -1) if records[i] == last), None)
            self.base += len(self.records) if at is None else len(self.records) - 1 - at
        else:
            self.base += len(self.records)
        self.state, self.records = state, records
        self.show_records()

    def rows(self, records, first):
        """[(row, record, bad, key)] of the RECORDS the filters let through; FIRST: the key of records[0]."""
        text = self.filter.text().lower()
        shown = []
        for index, record in enumerate(records):
            row = tuple(printable(value) for value in backend.summary(record))
            bad = row[1] in ("denied", "error", "tls-refused", "tls-upstream-failed", "push REFUSED")
            if self.refused.isChecked() and not bad:
                continue
            if text and text not in " ".join(row).lower():
                continue
            shown.append((row, record, bad, first + index))
        return shown

    def show_records(self):
        shown = self.shown = self.rows(self.records, self.base)
        fill(self.table, [row for row, _, _, _ in shown], ["bad" if bad else None for _, _, bad, _ in shown],
             keys=[key for _, _, _, key in shown])
        if self.follow.isChecked() and shown:
            self.table.scrollToBottom()

    def record_of(self, row):
        """The log record shown in the visual ROW (found by its key: the table may be sorted), or None."""
        key = row_key(self.table, row)
        index = None if key is None else key - self.base
        return self.records[index] if index is not None and 0 <= index < len(self.records) else None

    def details(self):
        rows = selected_rows(self.table)
        record = self.record_of(rows[0]) if rows else None
        if record is not None:
            self.detail.setPlainText(json.dumps(record, indent=2, ensure_ascii=True))


class AddServerDialog(QDialog):
    def __init__(self, parent, entry):
        super().__init__(parent)
        self.setWindowTitle(tr("Add {name}", name=entry.get("name")))
        form = QFormLayout(self)
        description = plain_label(entry.get("description", ""))
        form.addRow(description)
        self.local = QLineEdit(entry.get("name", ""))
        form.addRow(tr("Name for --mcp"), self.local)
        self.mounts = {}
        for mount in entry.get("mounts", []):
            field = PathField()
            writable = QCheckBox(tr("writable"))
            row = QHBoxLayout()
            row.addWidget(field)
            row.addWidget(writable)
            holder = QWidget()
            holder.setLayout(row)
            form.addRow(f"{mount.get('target')}" + (f" ({tr('required')})" if mount.get("required") else ""), holder)
            self.mounts[mount.get("target")] = (field, writable)
        self.env = [v.get("name") for v in entry.get("env", []) if isinstance(v, dict)]
        if self.env:
            form.addRow(tr("Variables from your environment"), plain_label(", ".join(self.env)))
        self.network = QComboBox()
        wanted = entry.get("network", "none")
        self.network.addItems(["full", "none"] if wanted != "none" else ["none", "full"])
        hosts = ", ".join(entry.get("hosts", []))
        form.addRow(tr("Network") + (" " + tr("(needs {hosts})", hosts=hosts) if hosts else ""), self.network)
        self.memory = QLineEdit("512m")
        form.addRow(tr("Memory"), self.memory)
        self.pids = QSpinBox()
        self.pids.setRange(16, 4096)
        self.pids.setValue(128)
        form.addRow(tr("Processes"), self.pids)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.prefix_name = None

    def arguments(self, name):
        args = [name, "--as", self.local.text().strip() or name.split(":")[-1], "--network",
                self.network.currentText(), "--memory", self.memory.text().strip() or "512m",
                "--pids", str(self.pids.value())]
        for target, (field, writable) in self.mounts.items():
            if field.value():
                args += ["--mount", f"{target}={field.value()}:{'rw' if writable.isChecked() else 'ro'}"]
        for variable in self.env:
            args += ["--env", variable]
        return args


class AttachServerDialog(QDialog):
    """Attach to…: the agent start that gets an MCP server (sandbox-ai mcp attach), and whether a running one is
    restarted for it now."""

    def __init__(self, parent, name, targets):
        super().__init__(parent)
        self.setWindowTitle(tr("Attach {name} to…", name=name))
        form = QFormLayout(self)
        self.start = QComboBox()
        for entry in targets:
            used = name in (entry.get("mcp") or [])
            self.start.addItem(printable(f"{entry['name']} ({entry.get('agent') or '?'}, {entry.get('state')})"
                                         + (f" - {tr('uses it')}" if used else "")), entry["name"])
        form.addRow(tr("Agent start"), self.start)
        self.restart = QCheckBox(tr("Restart it now if it runs (the agent continues its conversation)"))
        form.addRow(self.restart)
        form.addRow(plain_label(tr("A running sandbox cannot take a new MCP server while it runs: its agent reads its "
                                   "servers when it starts, and the server's socket is mounted at the start. The "
                                   "server is kept in the start's options: without a restart it is there from its "
                                   "next start on."), role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.start.currentData(), self.restart.isChecked()


class McpPage(QWidget):
    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        servers = QGroupBox(tr("Your MCP servers (use them with --mcp NAME)"))
        servers_layout = QVBoxLayout(servers)
        self.servers = table([tr("Name"), tr("Status"), tr("Serves"), tr("Runs")])
        servers_layout.addWidget(self.servers)
        row = QHBoxLayout()
        self.server_buttons = []
        for action, text in (("start", tr("Start")), ("stop", tr("Stop"))):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action: self.server(a))
            row.addWidget(button)
            self.server_buttons.append(button)
        self.attach_button = QPushButton(tr("Attach to…"))
        self.attach_button.setToolTip(tr("Connect the server to an agent start (sandbox-ai mcp attach)"))
        self.attach_button.clicked.connect(self.attach)
        self.attach_button.setEnabled(sai.mcp_attach_supported())
        row.addWidget(self.attach_button)
        self.server_buttons.append(self.attach_button)
        row.addStretch()
        servers_layout.addLayout(row)
        layout.addWidget(servers)
        catalog = QGroupBox(tr("Catalogs: sandbox-ai-mcp (mcp:) and axross-mcp-hub (hub:)"))
        catalog_layout = QVBoxLayout(catalog)
        self.catalog = table([tr("Entry"), tr("Network"), tr("Title"), tr("Published")])
        catalog_layout.addWidget(self.catalog)
        row = QHBoxLayout()
        load = QPushButton(tr("Reload catalogs"))
        load.setToolTip(tr("The catalogs load by themselves when the page is first shown; this fetches them again"))
        load.clicked.connect(lambda: self.load_catalog())
        add = QPushButton(tr("Add…"))
        add.clicked.connect(self.add)
        self.load_button, self.add_button = load, add
        row.addWidget(load)
        row.addWidget(add)
        row.addStretch()
        catalog_layout.addLayout(row)
        self.catalog_status = plain_label(role="secondary")
        self.catalog_status.setWordWrap(True)
        catalog_layout.addWidget(self.catalog_status)
        layout.addWidget(catalog)
        self.entries, self.catalog_requested = {}, False
        self.refresh()

    def showEvent(self, event):  # noqa: N802
        """The catalogs load by themselves the first time the page is shown (in the background)."""
        super().showEvent(event)
        if not self.catalog_requested:
            self.load_catalog(auto=True)

    def refresh(self):
        """The servers, read in the background (the window is not held up by it)."""
        # A read, not work: no owner, so it never holds up closing the window (busy()).
        background(self.servers_and_starts, timeout=60,
                   done=lambda rows, error: alive(self) and self.servers_read(rows, error))

    def servers_and_starts(self):
        """[(name, status, the starts it serves, what runs)] of the defined MCP servers."""
        try:
            served = self.sai.mcp_serving()
        except Exception:  # noqa: BLE001 - the starts could not be read: the servers are listed anyway
            served = {}
        return [(name, state, ", ".join(served.get(name, [])), what) for name, state, what in self.sai.mcp_servers()]

    def servers_read(self, rows, error):
        if error is not None:
            rows = [(tr("(error)"), "", "", str(error))]
        fill(self.servers, rows, ["ok" if r[1] == "running" else None for r in rows])

    def attach(self):
        name = selected(self.servers)
        if not name:
            inform(self, tr("Attach to…"), tr("Select a server first."))
            return
        try:
            targets = self.sai.mcp_attach_targets()
        except (OSError, ValueError) as exc:
            warn(self, tr("Attach to…"), printable(str(exc)))
            return
        if not targets:
            inform(self, tr("Attach to…"), tr("No kept agent start to attach it to: start one (a run "
                                              "without a kept start takes no new server)."))
            return
        dialog = AttachServerDialog(self, name, targets)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        start, restart = dialog.values()
        background(lambda: self.sai.mcp_attach(name, start, restart), owner=self, source=self.server_buttons,
                   timeout=630, done=lambda result, error: self.attached(name, result, error))

    def attached(self, name, result, error):
        if error is not None:
            warn(self, tr("Attach to…"), printable(str(error)))
        else:
            inform(self, tr("Attach to…"), printable(result.get("message") or ""))
        self.refresh()

    def server(self, action):
        name = selected(self.servers)
        if not name:
            return
        background(lambda: self.sai.call("mcp", action, name, timeout=300), owner=self, source=self.server_buttons,
                   timeout=330, done=lambda result, error: self.server_done(action, result, error))

    def server_done(self, action, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            warn(self, f"mcp {action}", (out + err).strip())
        self.refresh()

    def load_catalog(self, auto=False):
        """The catalogs come over the network: fetched in a thread."""
        self.catalog_requested = True
        self.catalog_status.setText(tr("Loading the catalogs…"))
        background(self.sai.mcp_catalog, owner=self, source=self.load_button, timeout=120,
                   done=lambda result, error: self.catalog_loaded(result, error, auto))

    def catalog_loaded(self, result, error, auto=False):
        entries, problems = result if error is None else ([], [str(error)])
        self.catalog_status.setText(tr("{count} entries", count=len(entries)) if not problems else
                                    "\n".join(printable(problem) for problem in problems))
        if auto:
            problems = []  # loaded by itself: said on the page, not in a dialog
        self.entries = {f"{prefix}:{e.get('name')}": e for prefix, e in entries}
        fill(self.catalog, [[key, e.get("network", "") + (" " + ",".join(e.get("hosts", [])) if e.get("hosts") else ""),
                             e.get("title", ""), tr("yes") if e.get("digest") else tr("not yet")]
                            for key, e in sorted(self.entries.items())])
        if problems:
            warn(self, tr("Catalogs"), "\n".join(printable(problem) for problem in problems))

    def add(self):
        key = selected(self.catalog)
        entry = self.entries.get(key or "")
        if not entry:
            QMessageBox.information(self, tr("Add"), tr("Load the catalogs and select an entry first."))
            return
        dialog = AddServerDialog(self, entry)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        arguments = dialog.arguments(key)
        background(lambda: self.sai.call("mcp", "add", *arguments, timeout=900), owner=self, source=self.add_button,
                   timeout=930, done=lambda result, error: self.added(result, error))

    def added(self, result, error):
        report(self, "mcp add", result, error)
        self.refresh()


class SystemPage(QWidget):
    theme_changed = Signal(str)

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        layout = QVBoxLayout(self)
        about = QGroupBox(tr("sandbox-ai"))
        about_layout = QHBoxLayout(about)
        self.version = plain_label(tr("Version {version}, from {path}", version=sai.version(), path=sai.directory))
        self.update_text = plain_label(tr("Update check not run yet."))
        self.update_button = QPushButton(tr("Check for updates"))
        about_layout.addWidget(self.version, 1)
        about_layout.addWidget(self.update_text, 1)
        about_layout.addWidget(self.update_button)
        layout.addWidget(about)
        # The administrator's policy, in one line; the Settings dialog shows it in full.
        policy_box = QGroupBox(tr("Administrator policy"))
        policy_layout = QHBoxLayout(policy_box)
        self.policy_text = plain_label()
        self.policy_button = QPushButton(tr("Show…"))
        self.settings_button = QPushButton(tr("All settings…"))
        policy_layout.addWidget(self.policy_text, 1)
        policy_layout.addWidget(self.policy_button)
        policy_layout.addWidget(self.settings_button)
        layout.addWidget(policy_box)
        self.show_policy_state()
        doctor = QGroupBox(tr("Host check (sandbox-ai doctor)"))
        doctor_layout = QVBoxLayout(doctor)
        row = QHBoxLayout()
        self.backend = QComboBox()
        self.backend.addItems(["podman", "bwrap", "both"])
        run = QPushButton(tr("Check"))
        run.clicked.connect(self.check)
        self.check_button = run
        row.addWidget(QLabel(tr("Backend")))
        row.addWidget(self.backend)
        row.addWidget(run)
        row.addStretch()
        doctor_layout.addLayout(row)
        from .doctor_view import DoctorView
        self.checks = DoctorView()  # by area, status icons in the theme's colours, the fix command copyable
        doctor_layout.addWidget(self.checks)
        layout.addWidget(doctor, 1)
        tools = QGroupBox(tr("Images, updates, privacy"))
        tools_layout = QHBoxLayout(tools)
        for text, args in ((tr("Build images"), ("build", "all")), (tr("Update sandbox-ai"), ("update",))):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=args: self.tool(a))
            tools_layout.addWidget(button)
        privacy = QPushButton(tr("Privacy check of a repository…"))
        privacy.clicked.connect(self.privacy)
        self.privacy_button = privacy
        tools_layout.addWidget(privacy)
        tools_layout.addStretch()
        layout.addWidget(tools)
        settings = QGroupBox(tr("Settings of the app"))
        form = QFormLayout(settings)
        current = sai.settings()
        self.theme = QComboBox()
        for key, text in theme.choices():
            self.theme.addItem(tr(text), key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(current.get("theme", "system"))))
        self.theme.currentIndexChanged.connect(self.choose_theme)
        self.language = QComboBox()
        for key, text in (("system", tr("as the system")), ("en", "English"), ("de", "Deutsch")):
            self.language.addItem(text, key)
        self.language.setCurrentIndex(max(0, self.language.findData(current.get("language", "system"))))
        self.language.currentIndexChanged.connect(self.choose_language)
        self.terminal_mode = QComboBox()
        for key, text in (("app", tr("in a tab of the app (when available)")), ("window", tr("in a terminal window"))):
            self.terminal_mode.addItem(text, key)
        self.terminal_mode.setCurrentIndex(max(0, self.terminal_mode.findData(current.get("terminal", "app"))))
        self.terminal_mode.currentIndexChanged.connect(
            lambda: self.sai.save_setting("terminal", self.terminal_mode.currentData()))
        form.addRow(tr("Theme"), self.theme)
        form.addRow(tr("Language"), self.language)
        form.addRow(tr("Commands such as builds and logins run"), self.terminal_mode)
        self.tray_box = QCheckBox(tr("Keep answering in the system tray when the window is closed"))
        self.tray_box.setChecked(bool(current.get("tray", True)))
        self.tray_box.toggled.connect(lambda on: self.sai.save_setting("tray", bool(on)))
        form.addRow(tr("Window"), self.tray_box)
        self.autostart_box = QCheckBox(tr("Start at login, in the tray"))
        self.autostart_box.setToolTip(tr("Writes ~/.config/autostart/sandbox-ai-app.desktop; unticked, it is removed"))
        self.autostart_box.setChecked(backend.autostart_enabled())
        self.autostart_box.toggled.connect(self.choose_autostart)
        form.addRow(tr("Login"), self.autostart_box)
        layout.addWidget(settings)

    def choose_autostart(self, on):
        """Written or removed only here, when the human ticks the box."""
        try:
            backend.set_autostart(bool(on), forbidden=self.sai.sandbox_folders() if on else (),
                                  also=[str(self.sai.directory)])
        except backend.InsideSandbox as exc:
            warn(self, tr("Start at login"), tr("Not written: the app runs from {path}, inside {folder}, which a "
                                                "sandbox can write (it could change what starts at your next "
                                                "login). Install the app outside sandbox folders.",
                                                path=printable(exc.path), folder=printable(exc.root)))
            on = None
        except (OSError, ValueError) as exc:
            warn(self, tr("Start at login"), printable(exc))
            on = None
        if on is None:
            self.autostart_box.blockSignals(True)
            self.autostart_box.setChecked(backend.autostart_enabled())
            self.autostart_box.blockSignals(False)

    def show_policy_state(self):
        try:
            policy, problem, text, _ = self.sai.policy()
        except Exception as exc:  # noqa: BLE001
            policy, problem, text = {}, str(exc), ""
        if problem:
            self.policy_text.setText(tr("The administrator's policy stops every start: {problem}",
                                        problem=printable(problem)))
            self.policy_text.setProperty("role", "notice-error")
        elif text is None:
            self.policy_text.setText(tr("No administrator policy on this machine."))
        else:
            self.policy_text.setText(tr("Forbidden: {list}; widest answer: {scope}.",
                                        list=printable(", ".join(policy.get("forbid", []))) or tr("nothing"),
                                        scope=printable(policy.get("max_approval_scope", "always"))))

    def choose_theme(self):
        mode = self.theme.currentData()
        self.sai.save_setting("theme", mode)
        applog.debug("theme", mode=mode)
        # After the combo box's own event: apply() may replace the style that event is still using.
        theme.apply_soon(mode, after=self.theme_changed.emit)

    def choose_language(self):
        self.sai.save_setting("language", self.language.currentData())
        QMessageBox.information(self, tr("Language"), tr("The language changes when the app is started again."))

    def check(self):
        background(self.sai.doctor, self.backend.currentText(), owner=self, source=self.check_button,
                   timeout=300, done=self.checked)

    def checked(self, results, error):
        if error is not None:
            warn(self, tr("Host check (sandbox-ai doctor)"), printable(error))
            return
        try:
            groups = getattr(self.sai.module("doctor"), "group_of", None)
        except Exception:  # noqa: BLE001 - the view's own table of areas
            groups = None
        self.checks.set_results(results, groups)

    def tool(self, args):
        self.terminal(self.sai.command(*args), "sandbox-ai " + " ".join(args))

    def privacy(self):
        directory = choose_folder(self, tr("Git repository"), "repository")
        if directory:
            background(lambda: self.sai.call("privacy-check", directory, timeout=600), owner=self, timeout=630,
                       source=self.privacy_button,
                       done=lambda result, error: show_text(self, tr("Privacy check: {path}", path=directory),
                                                            "\n".join(outcome(result, error)[1:]).strip(),
                                                            modal=False))


def alive(widget):
    """Whether WIDGET's C++ object still exists (a late result of a background call must not touch a deleted one)."""
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:  # pragma: no cover - PySide6 always has it
        return True


def guide_module():
    from . import guide
    return guide


class Spotlight(QWidget):
    """A frame drawn around the control a tour explains; it lets every click through and follows the control
    when the window changes size."""

    def __init__(self, window):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.target = None
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.place)
        self.hide()

    def follow(self, widget):
        self.target = widget
        if widget is None:
            self.timer.stop()
            self.hide()
            return
        self.place()
        self.timer.start()

    def place(self):
        widget = self.target
        try:
            visible = widget is not None and widget.isVisible()
        except RuntimeError:  # deleted with its page
            visible = False
        if not visible:
            self.hide()
            return
        corner = widget.mapTo(self.parentWidget(), QPoint(0, 0))
        self.setGeometry(QRect(corner, widget.size()).adjusted(-4, -4, 4, 4))
        self.raise_()
        self.show()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(theme.colors().get("accent", "#e8590c")), 3)
        painter.setPen(pen)
        painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 6, 6)


def risk_text(risk):
    return tr("Careful: {path} lies in {folder}, which a sandbox can write or could write in an earlier run: an "
              "agent may have changed this code. Install the app and sandbox-ai outside every sandbox folder.",
              path=printable(risk.path), folder=printable(risk.root))


# The navigation: sections with their pages, in this order (Ctrl+1 ... Ctrl+9 open the first nine).
SECTIONS = ((N_("Project"), (("launch", N_("Start")), ("board", N_("Board")))),
            (N_("Running"), (("sandboxes", N_("Sandboxes & sessions")), ("approvals", N_("Approvals")),
                             ("terminals", N_("Terminals")))),
            (N_("Insight"), (("connections", N_("Connections")), ("activity", N_("Activity")),
                             ("statistics", N_("Statistics")), ("files", N_("Files")))),
            (N_("Manage"), (("kits", N_("Kits & plugins")), ("mcp", N_("MCP servers")), ("images", N_("Images")),
                            ("credentials", N_("Credentials")))),
            (N_("System"), (("system", N_("System")), ("hosts", N_("Hosts")), ("help", N_("Help")))))
PAGES = tuple(page for _, pages in SECTIONS for page in pages)
SHORTCUT_PAGES = 9
NAV_WIDTH, COMPACT_NAV_WIDTH = 224, 52  # the navigation with titles, and as a strip of icons
OWN_START_WINDOW = 120  # seconds in which a run that shows up is the one the app just started in its terminal


def started_agent(argv):
    """The agent a sandbox-ai command line starts (sandbox-ai AGENT ...), or None (build, login, images ...)."""
    for index, token in enumerate(argv):
        if os.path.basename(str(token)) in ("main.py", "sandbox-ai"):
            following = argv[index + 1] if index + 1 < len(argv) else None
            return following if following in backend.AGENTS else None
    return None
PROJECT_SOURCES = {"recent": N_("opened in this app"), "trusted": N_("trusted project configuration"),
                   "session": N_("a session"), "running": N_("a running sandbox"),
                   "approvals": N_("answers remembered always"), "earlier": N_("an earlier run")}
PROJECT_STATES = {"none": N_("no .sandbox-ai.json yet"), "trusted": N_(".sandbox-ai.json trusted"),
                  "changed": N_(".sandbox-ai.json changed since you trusted it"),
                  "untrusted": N_(".sandbox-ai.json not trusted yet"), "unreadable": N_(".sandbox-ai.json cannot be read")}


def middle(text, limit=90):
    text = str(text)
    return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]


class ProjectCombo(QComboBox):
    """The switcher: its list is read again each time it opens."""
    opening = Signal()

    def showPopup(self):  # noqa: N802
        self.opening.emit()
        super().showPopup()


class ProjectBar(QWidget):
    """The current project, large, at the top of the window; the switcher lists the projects sandbox-ai and the
    app know (SandboxAI.known_projects), then Open folder… and New project (wizard)…."""
    chosen = Signal(str)
    open_folder = Signal()
    new_project = Signal()
    OPEN, NEW = ":open", ":new"  # never a project: those are absolute paths

    def __init__(self, sai):
        super().__init__()
        self.sai, self.path = sai, None
        self.setObjectName("projectBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 18, 8)
        self.icon = QLabel()
        self.icon.setPixmap(theme.glyph("project", theme.colors()["title"], 26).pixmap(26, 26))
        texts = QVBoxLayout()
        texts.setSpacing(0)
        first = QHBoxLayout()
        self.name = plain_label(tr("No project"), word_wrap=False)
        self.name.setObjectName("projectName")
        self.state = plain_label(word_wrap=False, role="secondary")
        self.where = plain_label(word_wrap=False, role="secondary")
        self.where.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for label in (self.name, self.where):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        first.addWidget(self.name)
        first.addSpacing(10)
        first.addWidget(self.state)
        first.addStretch()
        texts.addLayout(first)
        second = QHBoxLayout()
        self.activity = plain_label(word_wrap=False, role="secondary")
        second.addWidget(self.activity)
        second.addWidget(self.where, 1)
        texts.addLayout(second)
        self.combo = ProjectCombo()
        self.combo.setMinimumWidth(240)
        self.combo.setMaximumWidth(460)
        self.combo.setToolTip(tr("Switch the project: the Start page, the Board, Connections, Statistics, Files and "
                                 "Approvals follow it"))
        self.combo.opening.connect(self.fill)
        self.combo.activated.connect(self.activated)
        layout.addWidget(self.icon)
        layout.addLayout(texts, 1)
        layout.addSpacing(12)
        layout.addWidget(QLabel(tr("Project")))
        layout.addWidget(self.combo)
        self.controls = layout
        self.fill()

    def add_controls(self, buttons):
        """The window's own buttons (minimize, maximize, full screen, tray, quit) at the right end."""
        self.controls.addSpacing(10)
        for button in buttons:
            self.controls.addWidget(button)

    def restyle(self):
        self.icon.setPixmap(theme.glyph("project", theme.colors()["title"], 26).pixmap(26, 26))

    def fill(self):
        """The known projects (read again), the current one selected; then the two actions."""
        try:
            known = self.sai.known_projects()
        except Exception:  # noqa: BLE001 - the actions still work
            known = []
        self.combo.blockSignals(True)
        self.combo.clear()
        if self.path is None:
            self.combo.addItem(tr("No project chosen"), None)
        paths = [path for path, _ in known]
        if self.path and self.path not in paths:
            known = [(self.path, set())] + list(known)
        for path, sources in known:
            self.combo.addItem(f"{printable(Path(path).name or path)}  —  {printable(middle(path, 60))}", path)
            names = ", ".join(tr(PROJECT_SOURCES[s]) for s in sorted(sources) if s in PROJECT_SOURCES)
            self.combo.setItemData(self.combo.count() - 1, safe_tooltip(printable(path) + ("\n" + tr(
                "Known from: {list}", list=names) if names else "")), Qt.ItemDataRole.ToolTipRole)
        self.combo.insertSeparator(self.combo.count())
        self.combo.addItem(tr("Open folder…"), self.OPEN)
        self.combo.addItem(tr("New project (wizard)…"), self.NEW)
        self.select_current()
        self.combo.blockSignals(False)

    def select_current(self):
        self.combo.blockSignals(True)
        index = self.combo.findData(self.path)
        self.combo.setCurrentIndex(max(0, index))
        self.combo.blockSignals(False)

    def activated(self, index):
        data = self.combo.itemData(index)
        self.select_current()  # the window switches, then shows the project; a refusal leaves it as it was
        if data == self.OPEN:
            self.open_folder.emit()
        elif data == self.NEW:
            self.new_project.emit()
        elif isinstance(data, str) and data != self.path:
            self.chosen.emit(data)

    def show_project(self, path, state=None, warning=False):
        self.path = path
        self.name.setText(printable(middle(Path(path).name or path, 40)) if path else tr("No project"))
        self.state.setText(state or "")
        self.state.setProperty("role", "notice-warn" if warning else "secondary")
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)
        self.where.setText(printable(middle(path)) if path else tr("Choose a project folder to work in"))
        self.where.setToolTip(safe_tooltip(path) if path else "")
        self.fill()


SECTION_ROLE = Qt.ItemDataRole.UserRole + 1  # the navigation's section headers
STATE_COLORS = {"running": "ok", "paused": "warn", "starting": "warn", "stopped": "muted", "damaged": "bad"}


def menu_text(text):
    """TEXT for a menu entry or tab: shown as it is (an & would mark a shortcut key)."""
    return printable(text).replace("&", "&&")


def project_entry(path):
    path = os.path.realpath(os.path.expanduser(str(path)))
    return {"key": f"project:{path}", "kind": "project", "path": path}


def instance_entry(instance):
    """The tab of a session or sandbox (backend.Instance)."""
    kind = "session" if instance.kind == "session" else "run"
    name = instance.session if kind == "session" else instance.run
    return {"key": f"{kind}:{name}", "kind": kind, "name": name, "path": instance.project or "",
            "agent": instance.agent, "backend": instance.backend, "state": instance.state}


def tab_entry(value):
    """A saved tab ({"kind", "path", "name"} from the settings) as a tab entry, or None if it is not one (a
    project whose folder is gone, a damaged value)."""
    if not isinstance(value, dict):
        return None
    kind, path, name = value.get("kind"), value.get("path"), value.get("name")
    path = path if isinstance(path, str) and os.path.isabs(path) and "\0" not in path else ""
    if kind == "project":  # kept while the disk does not answer at once (the tab looks again when shown)
        return project_entry(path) if path and backend.is_folder(path) is not False else None
    if kind in ("session", "run") and backend.valid_run(name):
        return {"key": f"{kind}:{name}", "kind": kind, "name": name, "path": path, "agent": "", "backend": "",
                "state": "stopped"}
    return None


class ProjectTabs(QTabBar):
    """The open projects, sessions and sandboxes. Closable, movable, closed with a middle click too; the
    entry of a tab (its tab data) says what it stands for."""
    middle_clicked = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("projectTabs")
        self.setMovable(True)
        self.setExpanding(False)
        self.setDocumentMode(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.setIconSize(QSize(14, 14))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self.middle_clicked.emit(index)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def entry(self, index):
        data = self.tabData(index) if 0 <= index < self.count() else None
        return dict(data) if isinstance(data, dict) else None

    def find(self, key):
        return next((i for i in range(self.count()) if (self.entry(i) or {}).get("key") == key), -1)

    def add(self, entry, index=None):
        """Insert a tab for ENTRY at INDEX (default: the end); when it is the first, it becomes current and
        currentChanged is sent once its entry is there."""
        first = self.count() == 0
        self.blockSignals(True)
        try:
            index = self.insertTab(self.count() if index is None else index, "")
            close = QToolButton()
            close.setObjectName("tabClose")
            close.setAutoRaise(True)
            close.setFixedSize(QSize(20, 20))
            close.setIconSize(QSize(14, 14))
            close.setToolTip(tr("Close tab"))
            close.clicked.connect(lambda _=False, b=close: self.close_clicked(b))
            self.setTabButton(index, QTabBar.ButtonPosition.RightSide, close)
            self.set_entry(index, entry)
        finally:
            self.blockSignals(False)
        if first:
            self.currentChanged.emit(index)
        return index

    def close_clicked(self, button):
        index = next((i for i in range(self.count()) if self.tabButton(i, QTabBar.ButtonPosition.RightSide) is button),
                     -1)
        if index >= 0:
            self.tabCloseRequested.emit(index)

    def set_entry(self, index, entry):
        self.setTabData(index, dict(entry))
        self.decorate(index)

    def decorate(self, index):
        entry = self.entry(index)
        if entry is None:
            return
        close = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
        if close is not None:
            close.setIcon(theme.glyph("quit", theme.colors()["text"], 14))
        path = entry.get("path") or ""
        if entry["kind"] == "project":
            self.setTabText(index, menu_text(Path(path).name or path))
            self.setTabIcon(index, theme.glyph("project", theme.colors()["text"], 16))
            self.setTabToolTip(index, safe_tooltip(path))
            return
        state = entry.get("state") or "stopped"
        details = ", ".join(filter(None, [entry.get("agent"), entry.get("backend")]))
        self.setTabText(index, menu_text(entry.get("name", "") + (f" ({details})" if details else "")))
        self.setTabIcon(index, theme.dot(STATE_COLORS.get(state, "muted")))
        kind = tr("Session") if entry["kind"] == "session" else tr("Run")
        self.setTabToolTip(index, safe_tooltip(" · ".join(filter(None, [
            f"{kind} {entry.get('name', '')}", details, tr(STATE_TITLES.get(state, state)), path]))))

    def restyle(self):
        for index in range(self.count()):
            self.decorate(index)


# Pages with a canvas or terminal of their own that fills the page: never in a scroll area.
UNSCROLLED_PAGES = ("board", "terminals")
SCROLL_MIN_HEIGHT = 220  # the height a scrolled page may shrink to before the window does


class PageScroll(QScrollArea):
    """A page that scrolls up and down when the window is lower than the page needs, instead of squeezing its
    sections (Settings, Credentials, long forms); as wide as the page needs, so that no text is cut off."""

    def __init__(self, page):
        super().__init__()
        self.setObjectName("pageScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setWidget(page)

    def minimumSizeHint(self):  # noqa: N802
        hint = self.widget().minimumSizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        return QSize(hint.width() + bar, min(hint.height(), SCROLL_MIN_HEIGHT))

    def sizeHint(self):  # noqa: N802
        return self.widget().sizeHint()


class PageStack(QStackedWidget):
    """The pages: as small as the page shown allows (not the largest of all), so that the panels of a page fit
    next to it on a small screen. Most pages sit in a PageScroll; currentWidget() and setCurrentWidget() speak of the
    pages themselves."""

    def __init__(self):
        super().__init__()
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def add_page(self, page, scroll=True):
        return self.addWidget(PageScroll(page) if scroll else page)

    def frame_of(self, page):
        for index in range(self.count()):
            frame = self.widget(index)
            if frame is page or (isinstance(frame, PageScroll) and frame.widget() is page):
                return frame
        return None

    def currentWidget(self):  # noqa: N802
        frame = super().currentWidget()
        return frame.widget() if isinstance(frame, PageScroll) else frame

    def setCurrentWidget(self, page):  # noqa: N802
        frame = self.frame_of(page)
        if frame is not None:
            super().setCurrentWidget(frame)

    def minimumSizeHint(self):  # noqa: N802
        frame = super().currentWidget()
        return frame.minimumSizeHint() if frame is not None else super().minimumSizeHint()

    def sizeHint(self):  # noqa: N802
        frame = super().currentWidget()
        return frame.sizeHint() if frame is not None else super().sizeHint()


class PanelDock(QDockWidget):
    """A panel of a page (View > Panels): closing it by its button is remembered (closed_by_user); hiding
    it because another page is shown is not."""
    closed_by_user = Signal()

    def closeEvent(self, event):  # noqa: N802
        super().closeEvent(event)
        self.closed_by_user.emit()


# (name, title, page, default area) of the panels that can be floated, docked elsewhere and closed.
DOCKS = (("parts", N_("Board: parts"), "board", Qt.DockWidgetArea.LeftDockWidgetArea),
         ("inspector", N_("Board: inspector and findings"), "board", Qt.DockWidgetArea.RightDockWidgetArea),
         ("questions", N_("Approvals: open questions"), "approvals", Qt.DockWidgetArea.TopDockWidgetArea))


class MainWindow(QMainWindow):
    """The window. With a system tray (and the setting, on by default), closing it hides it: the approvals
    are still polled and announced; Quit in the tray menu ends the app (with the usual questions)."""
    closed = Signal()  # the window really closed: the app ends (main connects it to quit)

    def __init__(self, sai, check_updates=True, approvals_interval=1000, risk=None):
        super().__init__()
        # quitting: SIGTERM or logout (no questions); leaving: a confirmed quit from the tray menu (no hiding);
        # finished: the window has closed for good (shutdown ran, exactly once).
        self.sai, self.quitting, self.leaving, self.finished, self.stopped = sai, False, False, False, False
        self.close_seen = False
        # What the window looked like when it was left (state.json); file dialogs remember their folders there.
        self.state = State(parent=self)
        self.setWindowTitle(f"sandbox-ai {sai.version()}")
        self.resize(1240, 840)
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # RISK (backend.InsideSandbox from the start check): stays on screen for as long as the app runs.
        self.banner = plain_label(role="notice-error")
        self.banner.setVisible(risk is not None)
        if risk is not None:
            self.banner.setText(risk_text(risk))
        outer.addWidget(self.banner)
        problems = [p for p in (backend.settings_problem(), self.state.problem) if p]
        self.notice_bar = plain_label(tr("Saved settings could not be read; the app started with defaults: {problems}",
                                         problems=printable("; ".join(problems))) if problems else "",
                                      role="notice-warn")
        self.notice_bar.setVisible(bool(problems))
        outer.addWidget(self.notice_bar)
        layout = QHBoxLayout()
        outer.addLayout(layout, 1)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(20, 20))
        self.nav.setFixedWidth(NAV_WIDTH)
        self.compact_nav = False
        self.own_starts = []  # (agent, project folder, when) of sandboxes started in the app's terminal
        self.pages = PageStack()
        self.project = None
        self.launch = LaunchPage(sai, self.in_terminal)
        self.board = BoardPage(sai, self.in_terminal, sai.settings)
        self.sandboxes = SandboxesPage(sai, self.in_terminal)
        self.activity = ActivityPage(sai, self)
        self.approvals = ApprovalsPage(sai, interval=approvals_interval and backend.setting("approvals_interval"))
        self.connections = ConnectionsPage(sai)
        self.statistics = StatisticsPage(sai)
        self.files = FilesPage(sai, self.projects)
        self.kits = KitsPage(sai, self.in_terminal)
        self.mcp = McpPage(sai)
        self.images = ImagesPage(sai, self.in_terminal)
        self.credentials = CredentialsPage(sai, self.in_terminal)
        self.terminals = terminal.TerminalsPage(sai=sai, in_terminal=self.in_terminal)
        self.system = SystemPage(sai, self.in_terminal)
        self.hosts = HostsPage(sai, self.in_terminal)
        self.help = help_view.HelpPage()  # the Help area; every (?) of the app leads here
        self.help.tour_requested.connect(self.start_any_tour)
        # The navigation: a header per section (not selectable), then its pages; rows are found by page key.
        self.keys, self.nav_rows, self.section_rows, self.collapsed = [], {}, {}, set()
        for section, pages in SECTIONS:
            header = QListWidgetItem(tr(section).upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.AccessibleTextRole, tr(section))
            header.setData(SECTION_ROLE, section)
            header.setIcon(theme.glyph("fold-open", theme.colors()["nav_text"], 14))
            self.nav.addItem(header)
            self.section_rows[section] = (self.nav.count() - 1, [])
            for key, title in pages:
                self.section_rows[section][1].append(self.nav.count())
                item = QListWidgetItem(theme.glyph(key), tr(title))
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.nav_rows[key] = self.nav.count()
                self.nav.addItem(item)
                self.pages.add_page(getattr(self, key), scroll=key not in UNSCROLLED_PAGES)
                self.keys.append(key)
                if len(self.keys) <= SHORTCUT_PAGES:
                    item.setToolTip(tr("{page} (Ctrl+{number})", page=tr(title), number=len(self.keys)))
        self.nav.currentRowChanged.connect(self.nav_changed)
        self.nav.viewport().installEventFilter(self)  # a click on a section's header folds it
        self.nav.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.nav.customContextMenuRequested.connect(self.nav_menu)
        self.sandboxes.open_log.connect(self.show_log)
        self.sandboxes.counted.connect(lambda running, here: self.update_activity(sandboxes=here))
        self.sandboxes.updated.connect(self.sync_tabs)
        self.approvals.pending_changed.connect(self.questions_changed)
        self.kits.enable_kit.connect(self.enable_kit)
        self.kits.inventory_loaded.connect(self.board.set_kit_inventory)
        self.board.opened.connect(self.project_opened)
        self.board.open_request = self.choose_project_folder
        self.board.project_request = self.set_project  # a project folder dropped on the board opens in its tab
        self.launch.opened.connect(self.project_opened)
        panel = QWidget()
        panel.setObjectName("navPanel")
        side = QVBoxLayout(panel)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        self.brand = brand = QLabel("sandbox-ai")
        brand.setObjectName("brand")
        side.addWidget(brand)
        side.addWidget(self.nav, 1)
        self.compact_button = QToolButton()
        self.compact_button.setObjectName("navCompact")
        self.compact_button.setAutoRaise(True)
        self.compact_button.clicked.connect(lambda: self.set_compact_nav(not self.compact_nav))
        side.addWidget(self.compact_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(panel)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.project_bar = ProjectBar(sai)
        self.project_bar.chosen.connect(lambda path: self.set_project(path))
        self.project_bar.open_folder.connect(self.choose_project_folder)
        self.project_bar.new_project.connect(self.new_project)
        content_layout.addWidget(self.project_bar)
        # Open projects, sessions and sandboxes as tabs; "+" opens another.
        strip = QWidget()
        strip.setObjectName("tabStrip")
        strip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(12, 2, 12, 0)
        strip_layout.setSpacing(4)
        self.tabs = ProjectTabs()
        self.tabs.currentChanged.connect(self.tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.middle_clicked.connect(self.close_tab)
        self.tabs.tabMoved.connect(lambda *_: self.save_tabs())
        self.tabs.customContextMenuRequested.connect(self.tab_menu)
        self.add_tab_button = QToolButton()
        self.add_tab_button.setText("+")
        self.add_tab_button.setToolTip(tr("Open a project, session or running sandbox in a new tab"))
        self.add_tab_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_tab_menu = QMenu(self)
        self.add_tab_menu.aboutToShow.connect(self.fill_add_tab_menu)
        self.add_tab_button.setMenu(self.add_tab_menu)
        strip_layout.addWidget(self.tabs, 1)
        strip_layout.addWidget(self.add_tab_button)
        self.page_help = help_view.HelpButton("page.launch")  # explains the page shown (page_changed)
        strip_layout.addWidget(self.page_help, 0, Qt.AlignmentFlag.AlignVCenter)
        content_layout.addWidget(strip)
        # The pages, with panels (the Board's parts and inspector, the open questions) that can be floated,
        # docked elsewhere, closed and brought back from View > Panels.
        self.workspace = QMainWindow()
        self.workspace.setObjectName("workspace")
        self.workspace.setWindowFlags(Qt.WindowType.Widget)
        self.workspace.setDockOptions(QMainWindow.DockOption.AnimatedDocks | QMainWindow.DockOption.AllowTabbedDocks
                                      | QMainWindow.DockOption.AllowNestedDocks)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(18, 14, 18, 10)
        holder_layout.addWidget(self.pages)
        self.workspace.setCentralWidget(holder)
        content_layout.addWidget(self.workspace, 1)
        layout.addWidget(content, 1)
        self.setCentralWidget(central)
        self.make_docks()
        help_view.OPENER = self.show_help
        help_view.set_shown(backend.setting("help_buttons"))  # the setting; the Help page and F1 stay either way
        self.help_buttons = help_view.install(self)  # the (?)s of the pages' areas and controls
        self.statusBar().showMessage(tr("sandbox-ai {version} from {path}", version=sai.version(), path=sai.directory))
        self.tray, self.tray_timer = None, None
        self.session_timer = QTimer(self)
        self.session_timer.setSingleShot(True)
        self.session_timer.timeout.connect(self.session_goes_on)
        self.make_window_controls()
        self.make_menus()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.make_tray()
        else:
            self.system.tray_box.setEnabled(False)
            self.system.tray_box.setToolTip(tr("There is no system tray here"))
        self.notifier = notifications.Notifier(self, self.tray)
        self.update_window_controls()
        self.update_check = AsyncCommand(timeout=60, parent=self)
        self.update_check.finished.connect(self.update_checked)
        self.system.update_button.clicked.connect(self.check_updates)
        self.system.settings_button.clicked.connect(lambda: self.open_settings())
        self.system.policy_button.clicked.connect(lambda: self.open_settings(policy=True))
        self.system.theme_changed.connect(lambda _mode: self.check_theme_action())
        if check_updates:
            QTimer.singleShot(1500, self.check_updates)
        self.sandboxes_here = 0
        self.questions_changed(self.approvals.open.rowCount(), [])
        self.nav.setCurrentRow(self.nav_rows["launch"])
        # What runs now is known; what starts from here on opens a tab of its own (setting auto_tabs).
        self.seen_running = {key for key, i in self.sandboxes.instances.items() if i.state == "running"}
        self.register_state()
        self.restore_tabs()
        self.restore_docks()
        self.restore_window()

    # -- pages --

    def nav_item(self, key):
        return self.nav.item(self.nav_rows[key])

    def nav_changed(self, row):
        item = self.nav.item(row)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if key in self.nav_rows:
            applog.debug("page", key=key)
            self.pages.setCurrentWidget(getattr(self, key))
            if hasattr(self, "docks"):
                self.page_changed(key)

    def current_page(self):
        widget = self.pages.currentWidget()
        return next((key for key in self.keys if getattr(self, key) is widget), None)

    def show_page(self, key, raise_window=True, unfold=True):
        section = self.section_of(key)
        if unfold and section in self.collapsed and not self.compact_nav:
            self.toggle_section(section, False)  # a page reached another way shows where it is
        self.nav.setCurrentRow(self.nav_rows[key])
        if not raise_window:
            return
        if not self.isVisible():  # hidden in the tray
            self.show()
        self.raise_()
        self.activateWindow()

    # -- the window's own controls and menus (for desktops whose window manager shows none) --

    def make_window_controls(self):
        """Minimize, maximize/restore, full screen, to the tray and quit, as buttons in the header bar."""
        self.window_buttons = {}
        for key, slot in (("minimize", self.showMinimized), ("maximize", self.toggle_maximized),
                          ("fullscreen", self.toggle_full_screen), ("tray", self.to_tray), ("quit", self.quit_app)):
            button = QToolButton()
            button.setObjectName("windowControl")
            button.setAutoRaise(True)
            button.setIconSize(QSize(18, 18))
            button.clicked.connect(lambda _=False, s=slot: s())
            self.window_buttons[key] = button
        self.project_bar.add_controls(list(self.window_buttons.values()))

    def make_menus(self):
        """File, View, Go and Help: every action also has a button or a page; shortcuts that a terminal needs
        (Ctrl+W, Ctrl+Tab) are given up while the Terminals page is shown."""
        self.actions = {}

        def action(menu, key, text, slot, shortcuts=(), checkable=False):
            item = QAction(text, self)
            item.setCheckable(checkable)
            if shortcuts:
                item.setShortcuts([QKeySequence(s) for s in shortcuts])
            item.triggered.connect(lambda _=False, s=slot: s())
            menu.addAction(item)
            self.actions[key] = item
            return item
        bar = self.menuBar()
        file_menu = bar.addMenu(tr("&File"))
        action(file_menu, "open", tr("Open project folder…"), self.choose_project_folder)
        action(file_menu, "new", tr("New project…"), self.new_project)
        self.switch_menu = file_menu.addMenu(tr("Switch project"))
        self.switch_menu.aboutToShow.connect(self.fill_switch_menu)
        file_menu.addSeparator()
        action(file_menu, "close-tab", tr("Close tab"), self.close_current_tab, ["Ctrl+W"])
        file_menu.addSeparator()
        action(file_menu, "quit", tr("Quit"), self.quit_app, ["Ctrl+Q"])
        view = bar.addMenu(tr("&View"))
        view.setToolTipsVisible(True)
        view.aboutToShow.connect(self.update_window_controls)
        action(view, "minimize", tr("Minimize"), self.showMinimized)
        action(view, "maximize", tr("Maximize"), self.toggle_maximized)
        action(view, "fullscreen", tr("Full screen"), self.toggle_full_screen, ["F11"], checkable=True)
        action(view, "tray", tr("Hide to the system tray"), self.to_tray)
        view.addSeparator()
        themes = view.addMenu(tr("Theme"))
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_actions = {}
        for key, text in theme.choices():
            item = action(themes, f"theme:{key}", tr(text), lambda k=key: self.choose_theme(k), checkable=True)
            self.theme_group.addAction(item)
            self.theme_actions[key] = item
        self.check_theme_action()
        zoom = view.addMenu(tr("Board zoom"))
        action(zoom, "zoom-in", tr("Zoom in"), lambda: self.board.view.zoom(1.2), [QKeySequence.StandardKey.ZoomIn])
        action(zoom, "zoom-out", tr("Zoom out"), lambda: self.board.view.zoom(1 / 1.2), [QKeySequence.StandardKey.ZoomOut])
        action(zoom, "zoom-fit", tr("Fit the board"), self.board.fit)
        panels = view.addMenu(tr("Panels"))
        self.panel_actions = {}
        for name, title, page, _ in DOCKS:
            item = action(panels, f"panel:{name}", tr(title), lambda n=name: self.toggle_panel(n), checkable=True)
            item.setChecked(True)
            self.panel_actions[name] = item
        panels.addSeparator()
        action(panels, "panels-reset", tr("Reset the panels"), self.reset_docks)
        navigation = view.addMenu(tr("Navigation"))
        action(navigation, "nav-compact", tr("Only icons"), lambda: self.set_compact_nav(not self.compact_nav),
               ["Ctrl+Shift+B"], checkable=True).setChecked(self.compact_nav)
        action(navigation, "nav-fold", tr("Fold all sections"), lambda: self.fold_all(True))
        action(navigation, "nav-unfold", tr("Unfold all sections"), lambda: self.fold_all(False))
        go = bar.addMenu(tr("&Go"))
        self.shortcuts = []
        for number, (key, title) in enumerate(PAGES, 1):
            item = action(go, f"page:{key}", tr(title), lambda k=key: self.show_page(k),
                          [f"Ctrl+{number}"] if number <= SHORTCUT_PAGES else ())
            item.setIcon(theme.glyph(key, theme.colors()["text"], 16))
            if number <= SHORTCUT_PAGES:
                self.shortcuts.append(item)
        go.addSeparator()
        action(go, "next-tab", tr("Next tab"), lambda: self.next_tab(1), ["Ctrl+Tab"])
        action(go, "previous-tab", tr("Previous tab"), lambda: self.next_tab(-1), ["Ctrl+Shift+Tab", "Ctrl+Backtab"])
        help_menu = bar.addMenu(tr("&Help"))
        action(help_menu, "help", tr("Help: every topic, in English and German"), lambda: self.show_help(),
               ["Shift+F1"])
        action(help_menu, "tour", tr("Guided tour"), lambda: self.start_tour())
        action(help_menu, "area-tour", tr("Tour of this page"), lambda: self.start_area_tour(self.current_page()),
               ["F1"])
        action(help_menu, "tour-about", tr("Tour about…"), lambda: self.tour_about(), ["Ctrl+F1"])
        areas = help_menu.addMenu(tr("Area tours"))
        from . import tours
        for key, title in PAGES:
            if key in tours.AREA_TOURS:
                item = action(areas, f"area-tour:{key}", tr(title), lambda k=key: self.start_area_tour(k))
                item.setIcon(theme.glyph(key, theme.colors()["text"], 16))
        tasks = help_menu.addMenu(tr("Tours through a task"))
        for key, tour in tours.TASK_TOURS.items():
            action(tasks, f"task-tour:{key}", tours.say(tour.title), lambda k=key: self.start_task_tour(k))
        action(help_menu, "log", tr("Show the log"), self.show_log_file)
        action(help_menu, "intro", tr("Watch the intro again"), self.play_intro)
        action(help_menu, "about", tr("About sandbox-ai-app"), self.about)
        action(help_menu, "readme", tr("Read the README"), self.readme)
        self.terminal_keys = {key: self.actions[key].shortcuts() for key in ("close-tab", "next-tab", "previous-tab")}

    def show_log_file(self):
        """The end of the app's log (app.log): errors with time, component and versions; in debug mode every step."""
        from . import applog
        path = applog.log_path()
        try:
            with open(path, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - (256 << 10)))
                text = stream.read().decode("utf-8", "replace")
        except OSError as exc:
            text = tr("No log yet: {error}", error=printable(str(exc)))
        show_text(self, tr("Log: {path}", path=printable(str(path))),
                  (tr("Debug mode is on.") if applog.debugging() else
                   tr("Debug mode is off (Settings > Window): only errors and warnings are logged.")) + "\n\n" + text)

    def start_area_tour(self, page):
        """The tour of PAGE's area (tours.py) in the guide's panel: one step per control, outlined on the page."""
        from . import tours
        from .guide_view import AreaTourPanel
        if page not in tours.AREA_TOURS:
            return
        if getattr(self, "tour_dock", None) is None:
            self.tour_dock = QDockWidget(tr("Guide"), self.workspace)
            self.tour_dock.setObjectName("dock-tour")
            self.workspace.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.tour_dock)
        panel = AreaTourPanel(page)
        panel.page_requested.connect(lambda key: self.show_page(key))
        panel.target_requested.connect(self.point_at)
        panel.closed.connect(lambda: self.tour_dock.hide())
        if getattr(self, "tour", None) is not None:
            self.tour.hide()  # the overview tour gives way; Help > Guided tour brings it back
        self.area_tour = panel
        self.tour_dock.setWidget(panel)
        self.tour_dock.show()
        self.tour_dock.raise_()
        panel.start()

    def show_help(self, section=None):
        """The Help page, at SECTION (a help_content id) when given."""
        self.show_page("help")
        if section:
            self.help.show_section(section)
        return self.help

    def tour_about(self, text=""):
        """Tour about…: the human types a topic; the matching tours start, the matching Help sections open on the
        Help page (help_view.TourSearchDialog)."""
        from .help_view import TourSearchDialog
        dialog = TourSearchDialog(self, text)
        dialog.tour_requested.connect(self.start_any_tour)
        dialog.section_requested.connect(lambda ident: self.show_help(ident))
        dialog.show()
        self.tour_search = dialog
        return dialog

    def start_any_tour(self, ident):
        """A tour from the Help page: a task tour by its id, or area:PAGE."""
        if ident.startswith("area:"):
            return self.start_area_tour(ident[len("area:"):])
        return self.start_task_tour(ident)

    def start_task_tour(self, ident):
        """A tour through a whole task (tours.TASK_TOURS) in the guide's panel: each step shows its page, outlines
        its control and is ticked off when the window sees it done."""
        from . import tours
        from .guide_view import TaskTourPanel
        if ident not in tours.TASK_TOURS:
            return None
        if getattr(self, "tour_dock", None) is None:
            self.tour_dock = QDockWidget(tr("Guide"), self.workspace)
            self.tour_dock.setObjectName("dock-tour")
            self.workspace.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.tour_dock)
        panel = TaskTourPanel(ident, self.tour_check)
        panel.page_requested.connect(lambda key: self.show_page(key))
        panel.target_requested.connect(self.point_at)
        panel.closed.connect(lambda: self.tour_dock.hide())
        if getattr(self, "tour", None) is not None:
            self.tour.hide()
        self.task_tour = panel
        self.tour_dock.setWidget(panel)
        self.tour_dock.show()
        self.tour_dock.raise_()
        panel.start()
        return panel

    def tour_check(self, check):
        """Whether the window sees CHECK (a step of a task tour, tours.py) done."""
        from . import presets
        kind, _, value = check.partition(":")
        board = self.board.board
        if kind == "project":
            return bool(self.project)
        if kind == "page":
            return self.current_page() == value
        if kind == "board-has":
            return board.first(value) is not None
        if kind == "board-connected":
            return any(board.connected(n) for n in board.of_kind(value))
        if kind == "board-option":
            node_kind, _, option = value.partition(":")
            return any(n.props.get(option) for n in board.of_kind(node_kind))
        if kind == "board-no-errors":
            return not self.board.errors()
        if kind == "board-applied":
            return bool(self.board.directory) and not self.board.dirty() and \
                os.path.isfile(os.path.join(self.board.directory, ".sandbox-ai.json"))
        if kind == "preset-mine":
            return bool(presets.load_mine(self.sai)[0])
        if kind == "help-search":
            return bool(self.help.search.text().strip())
        if kind == "tip-opened":
            return bool(help_view.OPENED)
        return False

    def point_at(self, page, target):
        """Outline the page's control TARGET (an attribute of the page) while a tour explains it; "" removes it.
        A control on a tab that is not shown brings its tab to the front."""
        if getattr(self, "spotlight", None) is None:
            self.spotlight = Spotlight(self)
        widget = getattr(getattr(self, page, None), target, None) if target else None
        if not isinstance(widget, QWidget):
            self.spotlight.follow(None)
            return
        child = widget
        while child is not None and child is not self:
            parent = child.parentWidget()
            stack = parent.parentWidget() if parent is not None else None
            if isinstance(stack, QTabWidget) and stack.indexOf(child) >= 0:
                stack.setCurrentWidget(child)
            child = parent
        self.spotlight.follow(widget)

    def play_intro(self):
        """The intro once more (Help menu, Settings), skippable as at the start; it changes no setting."""
        from . import intro
        if getattr(self, "intro", None) is None or self.intro.over:
            self.intro = intro.play(self, self.sai.settings(), force=True)
        return self.intro

    def start_tour(self, index=None):
        """The guided tour (guide.py) in a panel below the pages: one step per page, Back and Next show the page.
        It starts where it was left (or at INDEX)."""
        from .guide_view import TourPanel
        if getattr(self, "tour", None) is None:
            self.tour = TourPanel(self.state.get("tour_step", 0) if index is None else index)
            self.tour.page_requested.connect(lambda key: self.show_page(key))
            self.tour.step_changed.connect(lambda step: self.state.set("tour_step", step))
            self.tour.area_requested.connect(self.start_area_tour)
            self.tour.closed.connect(lambda: self.tour_dock.hide())
            self.tour_dock = QDockWidget(tr("Guide"), self.workspace)
            self.tour_dock.setObjectName("dock-tour")
            self.tour_dock.setWidget(self.tour)
            self.workspace.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.tour_dock)
        elif index is not None:
            self.tour.go(index)
        self.tour.show()
        self.tour_dock.show()
        self.tour_dock.raise_()
        self.show_page(guide_module().tour_step(self.tour.index).page)
        self.state.set("tour_offered", True)

    def page_changed(self, key):
        """A page was shown: its panels appear (those of other pages are put away unless floating), and on the
        Terminals page the keys a terminal needs go to the terminal."""
        self.update_docks()
        if key in help_view.H.PAGE_SECTIONS and hasattr(self, "page_help"):
            self.page_help.set_tip(f"page.{key}")
        for name, keys in getattr(self, "terminal_keys", {}).items():
            self.actions[name].setShortcuts([] if key == "terminals" else keys)

    def window_icon(self, name):
        return theme.glyph(name, theme.colors()["title"], 18)

    def update_window_controls(self):
        """The buttons and View entries follow the window's state (maximized, full screen) and the tray."""
        if not getattr(self, "window_buttons", None):
            return
        maximized, full = self.isMaximized(), self.isFullScreen()
        buttons = self.window_buttons
        buttons["minimize"].setIcon(self.window_icon("minimize"))
        buttons["minimize"].setToolTip(tr("Minimize"))
        buttons["maximize"].setIcon(self.window_icon("restore" if maximized else "maximize"))
        buttons["maximize"].setToolTip(tr("Restore") if maximized else tr("Maximize"))
        buttons["fullscreen"].setIcon(self.window_icon("fullscreen"))
        buttons["fullscreen"].setToolTip(tr("Leave full screen (F11)") if full else tr("Full screen (F11)"))
        buttons["quit"].setIcon(self.window_icon("quit"))
        buttons["quit"].setToolTip(tr("Quit sandbox-ai-app (Ctrl+Q)"))
        buttons["tray"].setIcon(self.window_icon("tray"))
        tray_why = None if self.tray is not None else tr("There is no system tray here")
        set_enabled(buttons["tray"], tray_why is None, tray_why,
                    tooltip=tr("Hide to the system tray: sandbox-ai keeps answering there"))
        if hasattr(self, "actions"):
            self.actions["maximize"].setText(tr("Restore") if maximized else tr("Maximize"))
            self.actions["fullscreen"].setChecked(full)
            self.actions["tray"].setEnabled(tray_why is None)
            self.actions["tray"].setToolTip(tray_why or tr("Hide to the system tray: sandbox-ai keeps answering there"))

    def toggle_maximized(self):
        if self.isFullScreen():
            self.showNormal()
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.update_window_controls()

    def toggle_full_screen(self):
        if self.isFullScreen():
            self.showMaximized() if getattr(self, "was_maximized", False) else self.showNormal()
        else:
            self.was_maximized = self.isMaximized()
            self.showFullScreen()
        self.update_window_controls()

    def to_tray(self):
        """Into the system tray (the app keeps answering there); without a tray nothing happens."""
        if self.tray is not None:
            self.hide_to_tray()

    def choose_theme(self, key):
        """A theme from the View menu: as on the System page (applied at once and saved)."""
        index = self.system.theme.findData(key)
        if index >= 0:
            self.system.theme.setCurrentIndex(index)
        self.check_theme_action()

    def check_theme_action(self):
        current = self.system.theme.currentData()
        for key, item in getattr(self, "theme_actions", {}).items():
            item.setChecked(key == current)

    def fill_switch_menu(self):
        self.switch_menu.clear()
        try:
            known = self.sai.known_projects()
        except Exception:  # noqa: BLE001
            known = []
        for path, _ in known:
            item = self.switch_menu.addAction(menu_text(f"{Path(path).name or path}  —  {middle(path, 60)}"))
            item.setCheckable(True)
            item.setChecked(same_folder(path, self.project))
            item.triggered.connect(lambda _=False, p=path: self.set_project(p))
        if not known:
            self.switch_menu.addAction(tr("No project known yet")).setEnabled(False)

    def about(self):
        import platform
        import PySide6
        from . import __version__
        inform(self, tr("About sandbox-ai-app"), tr(
            "sandbox-ai-app {app}\nsandbox-ai {sai} from {path}\nPySide6 {pyside}, Qt {qt}\nPython {python}",
            app=__version__, sai=self.sai.version(), path=printable(self.sai.directory), pyside=PySide6.__version__,
            qt=qVersion(), python=platform.python_version()))

    def readme(self):
        """The app's README, as text (it comes with the app; nothing is opened in another program)."""
        path = backend.PACKAGE_DIR.parent / "README.md"
        try:
            text = path.read_text(errors="replace")[:400_000]
        except OSError:
            inform(self, tr("Read the README"), tr("The README is not installed with this copy of the app: see "
                                                   "https://github.com/c0decave/sandbox-ai"))
            return
        show_text(self, tr("Read the README"), text)

    # -- what is kept for the next start (state.py) --

    def register_state(self):
        """The tables (sort order, widths the human gave), splitters, filters and check boxes of the pages, the
        Start page's form per agent, the Board's drafts and zoom, the Sandboxes page's selection, the log folders,
        the window itself: all come back at the next start."""
        s = self.state
        tables = [("sandboxes", self.sandboxes.table), ("connections", self.connections.table),
                  ("approvals-open", self.approvals.open), ("approvals-history", self.approvals.history),
                  ("approvals-always", self.approvals.always), ("files", self.files.table),
                  ("kits", self.kits.table), ("kits-market", self.kits.market), ("mcp", self.mcp.servers),
                  ("mcp-catalog", self.mcp.catalog), ("images", self.images.images),
                  ("images-packages", self.images.packages), ("credentials-logins", self.credentials.logins),
                  ("credentials-secrets", self.credentials.secrets)]
        tables += [(f"statistics-{key}", widget) for key, widget in self.statistics.tables.items()]
        for key, widget in tables:
            s.table(key, widget)
        for key, splitter in (("approvals", self.approvals.split), ("connections", self.connections.split),
                              ("kits", self.kits.split)):
            s.splitter(key, splitter)
        s.text("connections-filter", self.connections.filter)
        s.check("connections-refused", self.connections.refused)
        s.check("connections-follow", self.connections.follow)
        s.text("images-filter", self.images.filter)
        s.check("sandboxes-only-project", self.sandboxes.only_project)
        for key, page in (("connections-folder", self.connections), ("statistics-folder", self.statistics)):
            folder = s.get(key, "")
            if folder and os.path.isabs(folder):
                page.refresh_directories(select=folder)
            page.directory.currentIndexChanged.connect(
                lambda _i, k=key, p=page: s.set(k, p.directory.currentData() or ""))
        self.launch.attach_state(s)
        self.sandboxes.attach_state(s)
        self.board.attach_state(s)
        for section in s.get("nav_collapsed", []):
            if section in self.section_rows:
                self.toggle_section(section, True)
        self.set_compact_nav(s.get("nav_compact", False) is True)
        s.collect(self.keep_window)

    def keep_window(self):
        full, maximized = self.isFullScreen(), self.isMaximized()
        self.state.data["window"] = {"geometry": bytes(self.saveGeometry().toBase64()).decode(),
                                     "state": "fullscreen" if full else "maximized" if maximized else "normal"}
        self.state.data["page"] = self.current_page()

    def restore_window(self):
        """Size, place and state of the window and the page shown when it was left."""
        saved = self.state.get("window", {})
        if isinstance(saved.get("geometry"), str) and len(saved["geometry"]) < 4096:
            self.restoreGeometry(QByteArray.fromBase64(saved["geometry"].encode()))
        if saved.get("state") == "maximized":
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        elif saved.get("state") == "fullscreen":
            self.setWindowState(self.windowState() | Qt.WindowState.WindowFullScreen)
        page = self.state.get("page", "")
        if page in self.nav_rows:
            self.show_page(page, raise_window=False, unfold=False)  # sections stay as they were left
        self.update_window_controls()

    def eventFilter(self, watched, event):  # noqa: N802
        if (event.type() == QEvent.Type.MouseButtonRelease and getattr(self, "nav", None) is not None
                and watched is self.nav.viewport()):
            item = self.nav.itemAt(event.position().toPoint())
            if item is not None and item.data(SECTION_ROLE):
                self.toggle_section(item.data(SECTION_ROLE))
                return True
        return super().eventFilter(watched, event)

    def toggle_section(self, section, folded=None):
        """Fold or unfold a section of the navigation (kept for the next start)."""
        folded = section not in self.collapsed if folded is None else folded
        (self.collapsed.add if folded else self.collapsed.discard)(section)
        header, rows = self.section_rows[section]
        for row in rows:
            self.nav.setRowHidden(row, folded and not self.compact_nav)  # the icon strip shows every page
        self.nav.item(header).setIcon(theme.glyph("fold-closed" if folded else "fold-open",
                                                  theme.colors()["nav_text"], 14))
        self.state.set("nav_collapsed", sorted(self.collapsed))

    def fold_all(self, folded):
        """Fold (FOLDED) or unfold every section of the navigation; the current page's section stays open."""
        current = self.current_page()
        for section, (_, rows) in self.section_rows.items():
            here = any(self.nav.item(row).data(Qt.ItemDataRole.UserRole) == current for row in rows)
            self.toggle_section(section, folded and not here)

    def section_of(self, key):
        return next((section for section, (_, rows) in self.section_rows.items() if self.nav_rows.get(key) in rows),
                    None)

    def set_compact_nav(self, compact):
        """The navigation as a narrow strip of icons (each page's title in its tooltip), or with titles and
        sections; kept for the next start. Saves room on a small screen."""
        self.compact_nav = bool(compact)
        titles = {key: title for _, pages in SECTIONS for key, title in pages}
        for key, row in self.nav_rows.items():
            item = self.nav.item(row)
            item.setText("" if self.compact_nav else tr(titles[key]))
            item.setToolTip(tr(titles[key]) if self.compact_nav else (item.toolTip() if self.keys.index(key) >=
                            SHORTCUT_PAGES else tr("{page} (Ctrl+{number})", page=tr(titles[key]),
                                                   number=self.keys.index(key) + 1)))
        for section, (header, rows) in self.section_rows.items():
            self.nav.setRowHidden(header, self.compact_nav)
            for row in rows:
                self.nav.setRowHidden(row, not self.compact_nav and section in self.collapsed)
        self.nav.setFixedWidth(COMPACT_NAV_WIDTH if self.compact_nav else NAV_WIDTH)
        self.brand.setVisible(not self.compact_nav)
        self.compact_button.setText("»" if self.compact_nav else "«")
        self.compact_button.setToolTip(tr("Show the page titles") if self.compact_nav
                                       else tr("Only icons: more room for the pages"))
        if "nav-compact" in getattr(self, "actions", {}):
            self.actions["nav-compact"].setChecked(self.compact_nav)
        self.state.set("nav_compact", self.compact_nav)

    def nav_menu(self, position):
        menu = QMenu(self)
        compact = menu.addAction(tr("Only icons"), lambda: self.set_compact_nav(not self.compact_nav))
        compact.setCheckable(True)
        compact.setChecked(self.compact_nav)
        menu.addSeparator()
        menu.addAction(tr("Fold all sections"), lambda: self.fold_all(True)).setEnabled(not self.compact_nav)
        menu.addAction(tr("Unfold all sections"), lambda: self.fold_all(False)).setEnabled(not self.compact_nav)
        item = self.nav.itemAt(position)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if key in self.nav_rows and hasattr(guide_module(), "AREA_TOURS") and key in guide_module().AREA_TOURS:
            menu.addSeparator()
            menu.addAction(tr("Tour of this area"), lambda k=key: self.start_area_tour(k))
        menu.exec(self.nav.viewport().mapToGlobal(position))

    # -- panels (docks) --

    def make_docks(self):
        """The Board's parts and its inspector, and the open questions of the Approvals page, as panels of the
        page area: they can be floated (a floating one stays on every page), docked elsewhere and closed."""
        widgets = {"parts": self.board.parts_panel, "inspector": self.board.side, "questions": self.approvals.open_box}
        self.docks, self.closed_docks = {}, set()
        for name, title, _, area in DOCKS:
            dock = PanelDock(tr(title), self.workspace)
            dock.setObjectName(f"dock-{name}")
            dock.setWidget(widgets[name])
            dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                             | QDockWidget.DockWidgetFeature.DockWidgetMovable
                             | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
            self.workspace.addDockWidget(area, dock)
            dock.closed_by_user.connect(lambda n=name: self.panel_closed(n))
            dock.topLevelChanged.connect(lambda _floating: self.docks_changed())
            dock.dockLocationChanged.connect(lambda _area: self.docks_changed())
            self.docks[name] = dock
        self.board.palette.setMinimumWidth(150)
        self.board.side.setMinimumWidth(300)
        self.workspace.resizeDocks([self.docks["parts"], self.docks["inspector"]], [170, 320], Qt.Orientation.Horizontal)
        self.default_docks = self.workspace.saveState()

    def update_docks(self):
        page = self.current_page()
        for name, _, dock_page, _ in DOCKS:
            dock = self.docks[name]
            wanted = name not in self.closed_docks and (dock.isFloating() or dock_page == page)
            if dock.isHidden() == wanted:
                dock.setVisible(wanted)
            if hasattr(self, "panel_actions"):
                self.panel_actions[name].setChecked(name not in self.closed_docks)

    def panel_closed(self, name):
        if self.current_page() == dict((n, p) for n, _, p, _ in DOCKS)[name] or self.docks[name].isFloating():
            self.closed_docks.add(name)
        self.update_docks()
        self.save_docks()

    def toggle_panel(self, name):
        """View > Panels: bring a closed panel back (on its page) or close it."""
        if self.panel_actions[name].isChecked():
            self.closed_docks.discard(name)
            page = dict((n, p) for n, _, p, _ in DOCKS)[name]
            if not self.docks[name].isFloating() and self.current_page() != page:
                self.show_page(page)
        else:
            self.closed_docks.add(name)
        self.update_docks()
        self.save_docks()

    def docks_changed(self):
        self.update_docks()
        self.save_docks()

    def reset_docks(self):
        self.closed_docks.clear()
        for dock in self.docks.values():
            dock.setFloating(False)
        self.workspace.restoreState(self.default_docks)
        self.update_docks()
        self.save_docks()

    def save_docks(self):
        if getattr(self, "restoring_docks", False):
            return
        self.state.set("docks", {"state": bytes(self.workspace.saveState().toBase64()).decode(),
                                 "closed": sorted(self.closed_docks)})

    def restore_docks(self):
        """Where the panels were at the last start (QMainWindow.saveState, kept in the app's state file)."""
        saved = self.state.get("docks", {})
        state, closed = saved.get("state"), saved.get("closed")
        self.restoring_docks = True
        try:
            if isinstance(state, str) and len(state) < 65536:
                self.workspace.restoreState(QByteArray.fromBase64(state.encode()))
            if isinstance(closed, list):
                self.closed_docks = {n for n in closed if n in self.docks}
        finally:
            self.restoring_docks = False
        self.update_docks()

    def settings_dialog(self):
        from .settings import SettingsDialog
        dialog = SettingsDialog(self.sai, self)
        dialog.changed.connect(self.apply_setting)
        dialog.intro_requested.connect(self.play_intro)
        return dialog

    def open_settings(self, policy=False):
        dialog = self.settings_dialog()
        if policy:
            dialog.show_policy()
        dialog.exec()
        return dialog

    def apply_setting(self, key):
        """What can change at once does (theme: by the dialog; polling intervals and the (?)s here)."""
        if key == "help_buttons":
            help_view.set_shown(backend.setting("help_buttons"))
            return
        timers = {"approvals_interval": self.approvals.timer, "sandboxes_interval": self.sandboxes.timer,
                  "connections_interval": self.connections.timer}
        if key in timers and timers[key].isActive():
            timers[key].setInterval(backend.setting(key))

    def show_board(self, board, directory):
        """Open BOARD for the project DIRECTORY on the Board page (the wizard ends here)."""
        self.board.show_board(board, directory)
        self.show_page("board")

    # -- the current project and its tabs --

    def choose_project_folder(self):
        directory = choose_folder(self, tr("Project folder"), "project")
        return self.set_project(directory) if directory else False

    def new_project(self):
        """The wizard; it ends on the board, whose folder then opens in a tab of its own (the board shown
        before is kept for its project)."""
        self.show_page("board")
        return self.board.open_wizard(ask=False)

    def project_opened(self, directory):
        """The Board or the Start page opened, reviewed or wrote a project folder: its tab is the current one."""
        if not directory:
            return
        if not same_folder(directory, self.project):
            self.set_project(directory)
        else:
            self.show_project_state()
            self.launch.show_project_note()
            self.kits.set_project(self.project)

    def set_project(self, path):
        """Open the project PATH in its tab (made the current tab; an open one is reused). False if PATH is not a
        folder."""
        path = os.path.realpath(os.path.expanduser(str(path)))
        if not os.path.isdir(path):
            warn(self, tr("Project"), tr("{path} is not a folder.", path=printable(path)))
            return False
        self.open_tab(project_entry(path))
        return True

    def apply_project(self, path):
        """Make PATH the current project: the Start page's workspace, the Board (the project's own, as it was
        left), Connections and Statistics (its logs), Files, Approvals and Sandboxes (its rows marked), Kits
        (what it uses). Kept for the next start."""
        try:
            self.project = self.sai.remember_project(path)
        except OSError:  # a read-only configuration folder: the project is used, not kept
            self.project = path
        self.board.activate(path)
        for page in (self.launch, self.files, self.approvals, self.sandboxes, self.kits, self.statistics):
            page.set_project(path)
        self.connections.open_for_workspace(path)
        self.show_project_state()
        self.setWindowTitle(" — ".join([printable(Path(path).name or path), f"sandbox-ai {self.sai.version()}"]))

    def open_tab(self, entry, activate=True):
        """The tab of ENTRY (a project, session or run; see project_entry, instance_entry), added after the
        current one when it is not open yet; made current with ACTIVATE. Its index."""
        index = self.tabs.find(entry["key"])
        if index < 0:
            index = self.tabs.add(entry, self.tabs.currentIndex() + 1 if activate and self.tabs.count() else None)
        if activate:
            if self.tabs.currentIndex() == index:
                self.tab_changed(index)
            else:
                self.tabs.setCurrentIndex(index)
        self.save_tabs()
        return index

    def tab_changed(self, index):
        entry = self.tabs.entry(index)
        if entry is None:
            return
        path = entry.get("path")
        folder = backend.is_folder(path) if path else False
        if folder:
            self.show_tab_project(path)
        elif folder is None:  # the disk does not answer at once (a network file system): looked at in a thread
            key = entry["key"]
            background(os.path.isdir, path, timeout=STATE_WAIT,
                       done=lambda found, error: found and error is None and self.tab_folder_found(key, path))
        self.save_tabs()

    def tab_folder_found(self, key, path):
        current = self.tabs.entry(self.tabs.currentIndex())
        if current is not None and current["key"] == key:
            self.show_tab_project(path)

    def show_tab_project(self, path):
        if same_folder(path, self.project):
            self.board.activate(path)
        else:
            self.apply_project(os.path.realpath(path))

    def close_tab(self, index):
        """Close tab INDEX; when it is the last tab of its project and the project's board has unapplied
        changes, only after the human agrees to drop them. False if the tab stays."""
        entry = self.tabs.entry(index)
        if entry is None:
            return False
        path = entry.get("path")
        others = [i for i in range(self.tabs.count()) if i != index
                  and same_folder((self.tabs.entry(i) or {}).get("path"), path)]
        last = bool(path) and not others
        if last and self.board.dirty_for(path):
            if not confirm(self, tr("Close tab"), tr("The board of {path} has changes that are not applied. Close the "
                                                     "tab and drop them?", path=printable(path)),
                           tr("Close and drop them"), destructive=True, reject=tr("Keep the tab")):
                return False
        self.tabs.removeTab(index)
        if last:
            self.board.forget(path)
            if same_folder(self.board.directory, path) and self.board.dirty():
                self.board.open_project(path, quiet=True)  # its changes were dropped: as it is on disk
        self.save_tabs()
        return True

    def close_current_tab(self):
        if self.tabs.count():
            self.close_tab(self.tabs.currentIndex())

    def next_tab(self, step=1):
        if self.tabs.count():
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    def save_tabs(self):
        """The open tabs, their order and the current one, in the app's settings (restored at the next start)."""
        if getattr(self, "restoring_tabs", False) or getattr(self, "shut_down", False):
            return
        entries = [self.tabs.entry(i) for i in range(self.tabs.count())]
        current = self.tabs.entry(self.tabs.currentIndex())
        self.state.set("tabs", [{k: e[k] for k in ("kind", "path", "name") if e.get(k)} for e in entries])
        self.state.set("tab_current", current["key"] if current else None)

    def restore_tabs(self):
        """The tabs of the last start (projects whose folder is gone are left out), else the last project."""
        saved = self.state.get("tabs", [])
        entries = [entry for entry in (tab_entry(value) for value in saved) if entry is not None]
        if not entries and self.sai.current_project():
            entries = [project_entry(self.sai.current_project())]
        self.restoring_tabs = True
        try:
            for entry in entries:
                if self.tabs.find(entry["key"]) < 0:
                    self.tabs.add(entry)
        finally:
            self.restoring_tabs = False
        if not self.tabs.count():
            return
        self.sync_tabs(self.sandboxes.instances, open_new=False)
        target = max(0, self.tabs.find(self.state.get("tab_current", "")))
        if self.tabs.currentIndex() != target:
            self.tabs.setCurrentIndex(target)
        else:
            self.tab_changed(target)

    def sync_tabs(self, instances, open_new=True):
        """Session and sandbox tabs show their state (a sandbox that ended stays, stopped, until its tab is
        closed); a session or sandbox that started since the last look opens a tab (setting auto_tabs)."""
        for index in range(self.tabs.count()):
            entry = self.tabs.entry(index)
            if entry and entry["kind"] in ("session", "run"):
                instance = instances.get(entry["key"])
                if instance is not None:
                    entry.update(instance_entry(instance))
                else:
                    entry["state"] = "stopped"
                self.tabs.set_entry(index, entry)
        running = {key for key, i in instances.items() if i.kind in ("session", "run") and i.state == "running"}
        if open_new and backend.setting("auto_tabs"):
            for key in sorted(running - getattr(self, "seen_running", set())):
                if self.tabs.find(key) < 0 and not self.own_start(instances[key]):
                    self.open_tab(instance_entry(instances[key]), activate=False)
        self.seen_running = running

    def own_start(self, instance):
        """Whether INSTANCE is a one-off run the app started in its own terminal tab (the same agent and project,
        within OWN_START_WINDOW seconds): that tab shows it, a second one would be the same sandbox. Each start
        accounts for one run."""
        now = time.monotonic()
        self.own_starts = [s for s in self.own_starts if now - s[2] < OWN_START_WINDOW]
        if instance.kind != "run":
            return False
        project = os.path.realpath(instance.project) if instance.project else ""
        for index, (agent, folder, _) in enumerate(self.own_starts):
            if agent == instance.agent and (not folder or not project or folder == project):
                del self.own_starts[index]
                return True
        return False

    def tab_actions(self, index):
        """[(key, text, enabled, function)] of the context menu of tab INDEX."""
        entry = self.tabs.entry(index)
        if entry is None:
            return []
        found = []
        instance = self.sandboxes.instances.get(entry["key"])
        state = instance.state if instance else entry.get("state")
        if entry["kind"] == "session":
            for action, text, ok in (("attach-app", tr("Attach in the app"), state == "running"),
                                     ("pause", tr("Pause"), state == "running"),
                                     ("resume", tr("Resume"), state in ("paused", "stopped"))):
                found.append((action, text, ok and instance is not None,
                              lambda a=action: self.sandboxes.session(a, self.sandboxes.instances.get(entry["key"]))))
        if entry["kind"] in ("session", "run"):
            stoppable = state in ("running", "paused") and instance is not None and (
                entry["kind"] == "session" or self.sandboxes.stoppable)
            found.append(("stop", tr("Stop…"), stoppable,
                          lambda: self.sandboxes.stop(self.sandboxes.instances.get(entry["key"]))))
            found.append(("show", tr("Show in Sandboxes & sessions"), True, lambda: self.show_instance(entry["key"])))
        found.append(("close", tr("Close tab"), True, lambda: self.close_tab(self.tabs.find(entry["key"]))))
        found.append(("close-others", tr("Close other tabs"), self.tabs.count() > 1,
                      lambda: self.close_other_tabs(entry["key"])))
        return found

    def tab_menu(self, position):
        index = self.tabs.tabAt(position)
        actions = self.tab_actions(index)
        if not actions:
            return
        menu = QMenu(self)
        for key, text, enabled, function in actions:
            if key in ("show", "close"):
                menu.addSeparator()
            action = menu.addAction(text)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _=False, f=function: f())
        menu.exec(self.tabs.mapToGlobal(position))

    def close_other_tabs(self, key):
        for index in reversed(range(self.tabs.count())):
            entry = self.tabs.entry(index)
            if entry and entry["key"] != key:
                self.close_tab(index)

    def show_instance(self, key):
        self.show_page("sandboxes")
        self.sandboxes.only_project.setChecked(False)
        row = find_row(self.sandboxes.table, key)
        if row >= 0:
            self.sandboxes.table.selectRow(row)

    def fill_add_tab_menu(self):
        """"+": the known projects, the sessions and sandboxes of the account not open yet, Open folder…, New
        project…."""
        menu = self.add_tab_menu
        menu.clear()
        menu.addSection(tr("Projects"))
        try:
            known = self.sai.known_projects()
        except Exception:  # noqa: BLE001
            known = []
        for path, _ in known:
            action = menu.addAction(menu_text(f"{Path(path).name or path}  —  {middle(path, 60)}"))
            action.setCheckable(True)
            action.setChecked(self.tabs.find(project_entry(path)["key"]) >= 0)
            action.triggered.connect(lambda _=False, p=path: self.set_project(p))
        waiting = [i for i in self.sandboxes.instances.values() if i.kind in ("session", "run")
                   and self.tabs.find(i.key) < 0]
        if waiting:
            menu.addSection(tr("Sessions and sandboxes"))
            for instance in sorted(waiting, key=lambda i: (i.kind, str(i.name))):
                text = " · ".join([f"{tr(KIND_TITLES[instance.kind])} {instance.name}", instance.agent,
                                   tr(STATE_TITLES.get(instance.state, instance.state))])
                action = menu.addAction(theme.dot(STATE_COLORS.get(instance.state, "muted")), menu_text(text))
                action.triggered.connect(lambda _=False, i=instance: self.open_tab(instance_entry(i)))
        menu.addSeparator()
        menu.addAction(tr("Open folder…"), self.choose_project_folder)
        menu.addAction(tr("New project (wizard)…"), self.new_project)

    def show_project_state(self):
        """The project bar: the project at once, the state of its .sandbox-ai.json once it is read (in a
        thread: a slow disk must not hold up a tab switch); until then the state read last time."""
        self.state_token = getattr(self, "state_token", 0) + 1
        if not self.project:
            self.project_bar.show_project(None)
            return
        token, project = self.state_token, self.project
        known = getattr(self, "project_states", {}).get(project)
        self.project_bar.show_project(project, tr(PROJECT_STATES[known]) if known else "",
                                      warning=known in ("changed", "untrusted", "unreadable"))
        self.update_activity()
        background(self.sai.project_state, project, timeout=STATE_WAIT,
                   done=lambda result, error: self.project_state_read(token, project, result, error))

    def project_state_read(self, token, project, result, error):
        state = result[0] if error is None else "unreadable"
        self.project_states = getattr(self, "project_states", {})
        self.project_states[project] = state
        if token != self.state_token or project != self.project:
            return
        self.project_bar.show_project(project, tr(PROJECT_STATES[state]),
                                      warning=state in ("changed", "untrusted", "unreadable"))
        self.update_activity()

    def update_activity(self, sandboxes=None):
        if sandboxes is not None:
            self.sandboxes_here = sandboxes
        if not self.project:
            self.project_bar.activity.setText("")
            return
        questions = self.approvals.project_pending
        parts = [tr("{count} running here", count=self.sandboxes_here)]
        if questions:
            parts.append(tr("open questions: {count}", count=questions))
        self.project_bar.activity.setText(" · ".join(parts) + "  ·")

    def enable_kit(self, name):
        """Kits page: use kit NAME in the current project (--kit in its .sandbox-ai.json, through the
        review-and-trust step of the Board)."""
        if not self.project:
            inform(self, tr("Kits & plugins"), tr("Choose a project first (at the top of the window)."))
            return False
        return self.board.enable_kit(self.project, name)

    def show_log(self, workspace):
        self.connections.open_for_workspace(workspace)
        self.show_page("connections")

    def projects(self):
        """The "current project" folders for the file browser: the current project (else the folder the app
        was started in) and the Start page's first workspace when it is another one."""
        found = [("project", "", getattr(self, "project", None) or os.getcwd())]
        workspace = self.launch.workspace()
        if workspace and not same_folder(workspace, found[0][2]):
            found.append(("project", "", workspace))
        return found

    # -- approvals --

    def questions_changed(self, count, new):
        item = self.nav_item("approvals")
        item.setText(tr("Approvals") + (f"  ({count})" if count else ""))
        font = QFont(item.font())
        font.setBold(bool(count))
        item.setFont(font)
        if count:
            item.setForeground(QBrush(QColor(theme.color("bad"))))
        else:
            item.setData(Qt.ItemDataRole.ForegroundRole, None)  # the theme's navigation colour
        self.pending_count = count
        if hasattr(self, "project_bar"):
            self.update_activity()
        if new and backend.setting("notify"):
            run, question = new[0]
            more = tr(" and {count} more", count=len(new) - 1) if len(new) > 1 else ""
            from .pages import describe
            # A canary that went off is an alarm (an error toast stays until it is closed); anything else
            # waits for an answer (a warning).
            canaries = [(r, q) for r, q in new if isinstance(q, dict) and q.get("kind") == "canary"]
            if canaries:
                run, question = canaries[0]
            self.notify(tr("sandbox-ai asks: {sandbox}", sandbox=run), f"{describe(question)}{more}",
                        level="error" if canaries else "warning",
                        action=(tr("Open approvals"), lambda: self.show_page("approvals")))

    def changeEvent(self, event):  # noqa: N802
        """A new theme: the navigation's icons and the question count take its colours."""
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "nav_rows", None):
            for key in self.keys:
                self.nav_item(key).setIcon(theme.glyph(key))
            for section, (header, _) in getattr(self, "section_rows", {}).items():
                self.nav.item(header).setIcon(theme.glyph("fold-closed" if section in self.collapsed else "fold-open",
                                                          theme.colors()["nav_text"], 14))
            if getattr(self, "pending_count", 0):
                self.nav_item("approvals").setForeground(QBrush(QColor(theme.color("bad"))))
            if getattr(self, "project_bar", None) is not None:
                self.project_bar.restyle()
                self.tabs.restyle()
                self.update_window_controls()
                for key in self.keys:
                    self.actions[f"page:{key}"].setIcon(theme.glyph(key, theme.colors()["text"], 16))
        if event.type() == QEvent.Type.WindowStateChange:
            self.update_window_controls()
        super().changeEvent(event)

    def notify(self, title, text, level="warning", action=None):
        """A notification of LEVEL (info, success, warning, error): a popup in the app's theme at the corner of
        the screen, or the desktop's own (setting notify_style; also where a window cannot place itself, e.g.
        on Wayland). Title and text are plain text: control characters are shown escaped, and for the desktop's
        notification, whose body may hold markup, what the agent chose is HTML-escaped (notifications.py).
        ACTION: (label, callback) for a button on the popup. Warnings and errors also ask for attention in the
        task bar."""
        style = backend.setting("notify_style")
        self.notifier.show(level, title, text, seconds=backend.setting("notify_seconds"),
                           style=style if style in ("themed", "system") else "themed", action=action)
        if level in ("warning", "error"):
            QApplication.alert(self)

    # -- updates --

    def check_updates(self):
        if self.update_check.start(self.sai.command("update", "--check")):
            self.system.update_text.setText(tr("Checking for a newer sandbox-ai…"))

    def update_checked(self, status, out, err):
        versions = backend.parse_update_check(status, out)
        if versions is None:  # offline, or not an installation of install.sh: quietly nothing
            self.system.update_text.setText(tr("No update information (offline, or not installed with install.sh)."))
            return
        installed, available = versions
        if backend.newer(available, installed):
            text = tr("sandbox-ai {available} is available (installed: {installed}); update with: sandbox-ai update",
                      available=available, installed=installed)
            self.statusBar().showMessage(text)
        else:
            text = tr("sandbox-ai {installed} is up to date.", installed=installed)
        self.system.update_text.setText(text)

    # -- terminals --

    def in_terminal(self, argv, title, embedded=None, cwd=None):
        """Run ARGV in a terminal: a tab of the app (EMBEDDED True, or None and the setting says so, and
        QtWebEngine with xterm.js is there), else a terminal window."""
        if embedded is None:
            embedded = self.sai.settings().get("terminal", "app") == "app"
        if embedded and terminal.available()[0]:
            directory = cwd if cwd and os.path.isdir(cwd) else None
            self.terminals.open(argv, title, directory)
            self.show_page("terminals")
            started = started_agent(argv)
            options = argv[:argv.index("--")] if "--" in argv else argv  # not the agent's own arguments
            if started and "--dry-run" not in options and "--session" not in options:
                # Its terminal tab shows this sandbox: no second tab for its run when it shows up (sync_tabs). A
                # session is not a one-off run (it gets its session tab): counted, it would swallow the tab of
                # the next run of the same agent and project started elsewhere.
                self.own_starts.append((started, os.path.realpath(directory or self.project or ""),
                                        time.monotonic()))
            return True
        command = self.sai.in_terminal(argv, title)
        if command is None:
            QMessageBox.warning(self, tr("No terminal"), tr("No terminal emulator found. Set SANDBOX_AI_TERMINAL, "
                                                            "or run:") + "\n\n" + shlex.join(argv))
            return False
        started = QProcess.startDetached(command[0], command[1:])
        ok = started[0] if isinstance(started, tuple) else started
        if not ok:
            QMessageBox.warning(self, tr("Not started"), shlex.join(command))
        return ok

    def running_work(self):
        """The titles of the pages with work in the background (commands, copies, an answer being sent)."""
        found = [key for key, _ in PAGES if busy(getattr(self, key))]
        if self.files.task.running:
            found.append("files")
        if self.approvals.answering is not None:
            found.append("approvals")
        return [tr(title) for key, title in PAGES if key in found]

    def stop_work(self, timeout=None):
        """Cancel the copies (they stop at their next chunk and remove what they created), stop the
        sandbox-ai commands that run (their whole process groups), and wait up to TIMEOUT (QUIT_WAIT)
        seconds for the background threads to end; the calls still running."""
        self.stopped = True
        self.files.task.cancel()
        self.sai.stop_commands()
        return ThreadCall.wait_all(QUIT_WAIT if timeout is None else timeout)

    def terminate(self):
        """SIGTERM (logout, systemctl stop): no question, the work is stopped (bounded), the app quits."""
        self.quitting = True
        self.stop_work()
        QApplication.quit()

    def make_tray(self):
        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion), self)
        self.tray.setToolTip("sandbox-ai")
        self.tray.activated.connect(lambda *_: self.show_page("approvals"))
        self.tray.messageClicked.connect(lambda: self.show_page("approvals"))
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction(tr("Show sandbox-ai"), lambda: self.show_page("approvals"))
        self.tray_menu.addAction(tr("Quit"), self.quit_app)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.show()
        self.system.tray_box.setEnabled(True)
        self.system.tray_box.setToolTip("")

    def start_hidden(self, seconds=TRAY_WAIT, interval=1000, available=QSystemTrayIcon.isSystemTrayAvailable):
        """A start at login (--hidden): stay in the tray. The panel may come up after the app, so without a
        tray yet, look again every INTERVAL ms; after SECONDS the window is shown instead (nobody would
        see the questions otherwise)."""
        if self.tray is not None:
            return
        deadline = time.monotonic() + seconds
        self.tray_timer = QTimer(self)

        def look():
            if available():
                self.tray_timer.stop()
                self.make_tray()
            elif time.monotonic() >= deadline:
                self.tray_timer.stop()
                self.show()
        self.tray_timer.timeout.connect(look)
        self.tray_timer.start(interval)

    def quit_app(self):
        """Quit from the tray menu: a real close (so Qt knows the window is gone), which closeEvent neither
        hides to the tray nor asks about twice."""
        self.leaving, self.close_seen = True, False
        accepted = self.close()
        if not self.close_seen:  # never shown (started in the tray): no close event was sent
            event = QCloseEvent()
            self.closeEvent(event)
            accepted = event.isAccepted()
        if not accepted:
            self.leaving = False
        return accepted

    def end_of_session(self, *_):
        """Logout: no questions and no hiding (an ignored close would cancel the logout); the work is
        stopped within QUIT_WAIT. If the session goes on after all (the logout was cancelled), the window
        behaves as before after SESSION_GRACE seconds."""
        self.quitting = True
        if not self.stopped:
            self.stop_work()
        self.session_timer.start(SESSION_GRACE * 1000)

    def session_goes_on(self):
        if not self.finished:
            self.quitting, self.stopped = False, False

    def keep_in_tray(self):
        return self.tray is not None and bool(self.sai.settings().get("tray", True))

    def hide_to_tray(self):
        self.hide()
        if not self.sai.settings().get("tray_notice_shown"):
            self.notify("sandbox-ai", tr("sandbox-ai keeps answering in the system tray; quit it from the tray menu."),
                        level="info")
            self.sai.save_setting("tray_notice_shown", True)

    def questions_refused_without_window(self):
        """Without a tray, closing ends the answering: True if nothing runs or the human quits anyway."""
        try:
            runs = self.sai.runs()
        except Exception:  # noqa: BLE001 - nothing known to run
            runs = {}
        if not runs:
            return True
        return confirm(self, tr("Quit"), tr("{count} sandbox(es) still run. Without this window nobody answers their "
                                            "questions: each is refused after {seconds} seconds. Quit anyway?",
                                            count=len(runs), seconds=self.sai.approval_seconds),
                       tr("Quit"), destructive=True, reject=tr("Keep the window"))

    def closeEvent(self, event):  # noqa: N802
        self.close_seen = True
        if self.finished:  # closed for good already (e.g. app.quit() closing the windows once more)
            event.accept()
            return
        if self.quitting:  # SIGTERM or logout: nobody is there to answer a question
            self.finish(event)
            return
        if not self.leaving and self.keep_in_tray():
            event.ignore()
            self.hide_to_tray()
            return
        if not self.questions_refused_without_window():  # really closing (no tray, or Quit in its menu)
            event.ignore()
            return
        # Tabs of sessions and attached starts only detach (the sandbox and its agent run on): no question for
        # them. Asked only for commands that really end with the app, by name.
        ending = self.terminals.ending()
        if ending and not confirm(self, tr("Quit"), tr("These end with the app: {list}. Sessions keep running "
                                                       "(attach again from Terminals or with sandbox-ai attach NAME). "
                                                       "Quit?", list=", ".join(ending)),
                                  tr("End them and quit"), destructive=True, reject=tr("Keep the window")):
            event.ignore()
            return
        if self.terminals.detaching():
            applog.write(f"{applog.header('INFO', 'quit')}\n  sessions keep running; attach again from Terminals or "
                         f"with sandbox-ai attach NAME\n")
        work = self.running_work()
        if work:
            if not confirm(self, tr("Quit"), tr("Still at work: {pages}. Quit anyway? Copies are cancelled and what "
                                                "they created is removed; running sandbox-ai commands are stopped.",
                                                pages=", ".join(work)),
                           tr("Stop and quit"), destructive=True, reject=tr("Keep working")):
                event.ignore()
                return
            self.stop_work()
        self.finish(event)

    def finish(self, event):
        self.finished = True
        self.shutdown()
        event.accept()
        self.closed.emit()

    def shutdown(self):
        """Stop timers, commands and terminals; once (closing, app.aboutToQuit and tests may all call it)."""
        if getattr(self, "shut_down", False):
            return
        self.save_docks()
        self.save_tabs()
        self.state.flush()
        self.shut_down = True
        ThreadCall.abandon_all()  # no result reaches a page after this
        self.approvals.timer.stop()
        self.connections.timer.stop()
        self.sandboxes.timer.stop()
        self.update_check.stop()
        if getattr(self, "notifier", None) is not None:
            self.notifier.close_all()  # the popups are windows of their own
        self.images.list_command.stop()
        self.images.package_command.stop()
        AsyncCommand.end_stopped()  # the timers that would SIGKILL them do not fire any more
        self.activity.shutdown()
        self.terminals.close_all()
        if self.tray is not None:
            self.tray.hide()
