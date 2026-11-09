"""The windows of sandbox-ai-app (PySide6)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shlex
import textwrap
import time

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QProcess, QRect, QSize, Qt, QTimer, Signal, qVersion
from PySide6.QtGui import QAction, QActionGroup, QBrush, QCloseEvent, QColor, QFont, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar,
    QPushButton,
    QDockWidget, QFrame, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QStyle, QSystemTrayIcon, QTabBar,
    QTabWidget, QToolButton, QTreeView, QVBoxLayout, QWidget,
)

from . import applog, backend, i18n, notifications, terminal, theme, widgets
from .files import printable
from .host_choice import on_host
from .name_fields import NameField
from .state import choose_file, choose_folder
from .i18n import N_, tr
from .activity_page import ActivityPage
from .board_view import BoardPage, HostCombo
from . import dock_fold, help_view, labels
from . import presets as PR
from . import project_trust as PT
from .hosts_page import HostsPage
from .kits_page import KitsPage
from .state import State, scrub_arguments
from .pages import ApprovalsPage, CredentialsPage, ImagesPage, StatisticsPage
from .files_page import FilesPage
from .reviews import home_review, review_refusal
from .start_tree import ID_ROLE, KEY_ROLE, KIND_ROLE, StartTree, id_host, related, row_id
from . import wizard_llm as WL
from .widgets import (
    MONO, AsyncCommand, MessageText, ThreadCall, append_rows, ask_first, background, busy, fill, find_row, fit_columns,
    flow, inform, local_time, outcome, plain_label, row_key, safe_tooltip, selected, selected_rows, set_enabled,
    show_text, table, warn, shown_as_text, tame_combos, working,
)

CONNECTIONS_LIMIT = 5000  # records the Connections page keeps (backend.read_connections reads as many)
FILTER_PAUSE = 200  # ms after the last key before a filter of the Connections page is applied
REMOTE_LOG_SECONDS = 5  # between two reads of another host's connections while the Connections page follows
STATE_WAIT = 30  # seconds a look at a project folder (its state, whether it exists) may take
QUIT_WAIT = 10  # seconds the window waits, when it is closed, for the work still running
SESSION_GRACE = 60  # seconds after a logout request after which the logout counts as cancelled
TRAY_WAIT = 30  # seconds a start at login waits for the system tray to appear


def report(parent, title, result, error):
    """The outcome of a background sandbox-ai command, as plain text (its output may quote the agent)."""
    status, out, err = outcome(result, error)
    # Neither blocks the window: a question may wait meanwhile, and it must stay answerable.
    if status == 0:
        inform(parent, title, (out + err).strip(), modal=False)
    else:
        warn(parent, title, (out + err).strip(), modal=False)
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
        browse = self.browse_button = QToolButton()
        browse.setText("…")
        browse.setAccessibleName(tr("Choose…"))
        browse.clicked.connect(self.browse)
        layout.addWidget(self.edit)
        layout.addWidget(browse)

    def set_local(self, local):
        """LOCAL False: the path is another host's, typed in (this computer's chooser is off)."""
        self.browse_button.setEnabled(local)

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

    def set_local(self, local):
        """LOCAL False: the devices are another host's, typed in (this computer's are not offered)."""
        self.pick.setEnabled(local)

    def add(self, path):
        if path not in self.value():
            self.edit.appendPlainText(path)

    def value(self):
        return [line.strip() for line in self.edit.toPlainText().splitlines() if line.strip()]

    def set_value(self, values):
        self.edit.setPlainText("\n".join(values or []))


class RulesField(QWidget):
    """The Start page's ruleset files (--rules): one per line (a ListField, read like one: value, set_value), with
    New from example… and Check below (rules_tools)."""
    changed = Signal()

    def __init__(self, form):
        super().__init__()
        from . import rules_tools
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        self.field = ListField(path=False)
        self.field.edit.setPlaceholderText(rules_tools.placeholder())
        self.field.edit.setToolTip(rules_tools.tooltip())
        self.field.changed.connect(self.changed)
        column.addWidget(self.field)
        values = lambda: form.values() if len(form.widgets) >= len(form.options) else {}  # noqa: E731
        self.tools = rules_tools.RulesetTools(
            backend.SandboxAI(), self.field.value, lambda path: self.field.add(path),
            lambda: values().get("--workspace") or [],
            local=lambda: getattr(form, "host", LOCAL) in (None, "", LOCAL))
        column.addWidget(self.tools)

    def value(self):
        return self.field.value()

    def set_value(self, values):
        self.field.set_value(values)


class ListField(QWidget):
    """One value per line; for path options with a button that adds a folder; with CHOICES a button that adds one of
    the names the chosen host has (set_choices: its MCP servers for --mcp, its kits for --kit)."""
    changed = Signal()

    def __init__(self, path=False, choices=False, option=None):
        super().__init__()
        self.option = option
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setFixedHeight(52)
        self.edit.textChanged.connect(self.changed)
        layout.addWidget(self.edit)
        self.add_button = self.pick = None
        if path:
            add = self.add_button = QToolButton()
            add.setText("+")
            add.setToolTip(tr("Add a folder"))
            add.clicked.connect(self.add_folder)
            layout.addWidget(add, alignment=Qt.AlignmentFlag.AlignTop)
        if choices:
            self.pick = QToolButton()
            self.pick.setText("+")
            self.pick.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self.pick.setMenu(QMenu(self.pick))
            layout.addWidget(self.pick, alignment=Qt.AlignmentFlag.AlignTop)
            self.set_choices(None)

    def set_choices(self, names, where="", problem=None):
        """The names the pick button offers (None: not read yet), those of WHERE ("this computer" or a host)."""
        if self.pick is None:
            return
        menu = self.pick.menu()
        menu.clear()
        for name in names or []:
            menu.addAction(name, lambda n=name: self.add(n))
        if not names:
            text = (tr("Could not be read: {error}", error=problem) if problem else
                    tr("Reading…") if names is None else tr("None on {host}", host=where))
            menu.addAction(text).setEnabled(False)
        self.pick.setToolTip(safe_tooltip(tr("Add one that {host} has", host=where) if where else tr("Add one")))

    def add(self, value):
        if self.option in OptionsForm.SECRET_CHOICES:  # a secret's name: HOST=NAME, URL=NAME or NAME (name_fields)
            from .name_fields import add_line
            self.edit.setPlainText(add_line(self.edit.toPlainText(), value, self.option))
        elif value not in self.value():
            self.edit.appendPlainText(value)

    def set_local(self, local):
        """LOCAL False: the paths are another host's, typed in (this computer's chooser is off)."""
        if self.add_button is not None:
            self.add_button.setEnabled(local)

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


def buddy(text, widget):
    """A label for WIDGET: its buddy (a screen reader names the field by it), and WIDGET's accessible name."""
    label = QLabel(text)
    label.setBuddy(widget)
    if not widget.accessibleName():
        widget.setAccessibleName(text.replace("&", ""))
    return label


def set_accessible(widget, name):
    """NAME as WIDGET's accessible name, and of the field inside a compound one (a path or list field)."""
    widget.setAccessibleName(name)
    for kind in (QLineEdit, QComboBox, QPlainTextEdit):
        for child in widget.findChildren(kind):
            if not child.accessibleName():
                child.setAccessibleName(name)


class OptionLabel(QWidget):
    """A row label of the options form: the option's name in the language chosen, and the command-line option in
    small text below it while the setting show_flags is on; in the warning colour while an option that weakens the
    sandbox is set."""

    def __init__(self, text, flag):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.title = plain_label(text, word_wrap=False)
        self.flag = plain_label(flag, word_wrap=False, role="secondary")
        small = self.flag.font()
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1))
        self.flag.setFont(small)
        layout.addWidget(self.title)
        layout.addWidget(self.flag)
        self.flag.setVisible(labels.flags_shown())

    def text(self):
        return self.title.text()

    def set_warning(self, on):
        role = "notice-warn" if on else None
        if self.title.property("role") != role:
            self.title.setProperty("role", role)
            self.title.style().unpolish(self.title)
            self.title.style().polish(self.title)


class OptionsForm(QTabWidget):
    """Every launcher option, grouped; built from the launcher's own parser (its help texts are sandbox-ai's)."""
    changed = Signal()

    def __init__(self, options):
        super().__init__()
        self.options, self.widgets, self.labels = options, {}, {}
        self.changed.connect(self.mark_dangerous)
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
                # The option's name in the language chosen (labels.py), the command-line option below it in small
                # text, sandbox-ai's own help (English) as the tooltip of both.
                flag = option.long + (f" ({option.short})" if option.short else "")
                label = OptionLabel(labels.label(option.long), flag)
                tip = safe_tooltip(labels.tip(flag, option.help))
                label.setToolTip(tip)
                widget.setToolTip(tip)
                set_accessible(widget, labels.label(option.long))
                form.addRow(label, widget)
                self.widgets[option.long] = widget
                self.labels[option.long] = label
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.addTab(scroll, tr(GROUP_TITLES.get(title, title)).replace("&", "&&"))  # & would mark a shortcut

    def show_flags(self):
        """The setting show_flags changed: each row's option (--…) below its name and in its tooltip, or not."""
        for option in self.options:
            label, widget = self.labels.get(option.long), self.widgets.get(option.long)
            if label is None or widget is None:
                continue
            label.flag.setVisible(labels.flags_shown())
            tip = safe_tooltip(labels.tip(label.flag.text(), option.help))
            label.setToolTip(tip)
            widget.setToolTip(tip)

    def mark_dangerous(self):
        """The options that weaken the sandbox (labels.DANGEROUS) in the warning colour while they are set."""
        if len(self.widgets) < len(self.options):
            return  # still being built
        from . import ask_choice as AC
        values = self.values()
        for long, label in self.labels.items():
            label.set_warning((long in labels.DANGEROUS and bool(values.get(long))) or AC.warned(long, values.get(long)))

    # The options whose values the chosen host lists (backend.host_start_choices): a chooser filled from its lists,
    # the value still typed in where it has none (or the list could not be read).
    HOST_CHOICES = ("--vpn", "--kit", "--identity", "--mcp", "--image-variant", "--secret-env", "--api-key",
                    "--git-token-secret")
    SECRET_CHOICES = ("--secret-env", "--api-key", "--git-token-secret")  # their values name a secret of that host

    def widget_for(self, option):
        from . import hardware
        if option.long == "--gpu" and option.kind == "choice":
            # This computer: only the kinds it has (a kind it lacks would be refused at the start); another host:
            # every kind (set_host), its launcher checks it there.
            widget = QComboBox()
            widget.currentTextChanged.connect(self.changed)
            self.fill_gpu(widget, option, "local")
            return widget
        if option.long == "--device" and option.kind == "list":
            widget = DeviceField(hardware.devices())
            widget.changed.connect(self.changed)
            return widget
        if option.long in ("--memory", "--cpus") and option.kind not in ("flag", "choice", "multi", "list"):
            widget = QLineEdit()
            widget.textChanged.connect(self.changed)
            self.limit_hint(widget, option, "local")
            return widget
        if option.long in self.HOST_CHOICES and option.kind == "list":
            widget = ListField(choices=True, option=option.long)
            widget.changed.connect(self.changed)
            return widget
        if option.long in self.HOST_CHOICES and option.kind == "value":
            widget = QComboBox()
            widget.setEditable(True)
            widget.addItem("")
            widget.lineEdit().setPlaceholderText(option.metavar or "")
            widget.currentTextChanged.connect(self.changed)
            return widget
        if option.kind == "flag":
            # What the box does in the language chosen (its row is labelled with the same words; the help's first
            # words, in English, only for an option labels.py does not know yet).
            known = option.long in labels.OPTION_LABELS
            widget = QCheckBox(labels.label(option.long) if known else
                               (option.help.split(";")[0][:90] if option.help else labels.label(option.long)))
            widget.toggled.connect(self.changed)
        elif option.kind == "choice" and option.long == "--ask":
            from . import ask_choice as AC  # its values in words, "allow" marked (sandbox-ai 0.1.80)
            widget = AC.AskChoice(option.choices, option.default)
            widget.currentTextChanged.connect(self.changed)
        elif option.kind == "choice":
            widget = QComboBox()
            widget.addItems(([""] if option.default is None else []) + [str(c) for c in option.choices])
            if option.default is not None:
                widget.setCurrentText(str(option.default))
            widget.currentTextChanged.connect(self.changed)
        elif option.kind == "multi":
            widget = MultiChoice(option.choices)
            widget.changed.connect(self.changed)
        elif option.kind == "list" and option.long == "--rules":  # what to enter, a starter, the launcher's check
            widget = RulesField(self)
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
            # A start without a name gets a codename from the launcher (sandbox-ai list shows it).
            widget.setPlaceholderText(tr("empty: a codename like brisk-otter-lantern") if option.long == "--name"
                                      else option.metavar or "")
            widget.textChanged.connect(self.changed)
        return widget

    def fill_gpu(self, widget, option, host):
        """--gpu's kinds: those this computer has (none: greyed out), every kind for another HOST."""
        from . import hardware
        current = widget.currentText()
        found = [kind for kind in option.choices if kind in hardware.gpu_kinds()] if host == "local" else \
            [str(kind) for kind in option.choices]
        widget.blockSignals(True)
        try:
            widget.clear()
            widget.addItems([""] + found)
            if current and widget.findText(current) < 0:  # kept, not dropped (set_values does the same)
                widget.addItem(current)
            widget.setCurrentText(current)
        finally:
            widget.blockSignals(False)
        widget.setEnabled(bool(found) or bool(current))
        widget.setProperty("detected", (", ".join(found) or tr("none")) if host == "local" else "")
        widget.setProperty("host", host)
        widget.setToolTip(safe_tooltip(option.help if host == "local" else
                          tr("Every kind: whether {host} has that GPU is checked by its launcher at the start.",
                             host=printable(host)) + "\n" + option.help))

    def limit_hint(self, widget, option, host):
        """--memory and --cpus: what this computer has, or for another HOST only where it starts."""
        from . import hardware
        if host == "local":
            memory, cpus = hardware.limits_hint()
            have = memory if option.long == "--memory" else tr("{count} CPUs", count=cpus)
            widget.setPlaceholderText(tr("{example} (this computer: {have})", example=option.metavar or "",
                                         have=have) if have else option.metavar or "")
        else:
            have = ""
            widget.setPlaceholderText(tr("{example} (on {host})", example=option.metavar or "", host=printable(host)))
        widget.setProperty("detected", have)

    def set_host(self, host):
        """The fields that depend on where it starts: --gpu's kinds, the limits' hints, the choosers (emptied until
        set_choices gives that host's lists)."""
        self.host = host
        for option in self.options:
            widget = self.widgets.get(option.long)
            if option.long == "--gpu" and isinstance(widget, QComboBox):
                self.fill_gpu(widget, option, host)
            elif option.long in ("--memory", "--cpus") and isinstance(widget, QLineEdit):
                self.limit_hint(widget, option, host)
        self.set_choices(host, None, {})

    def set_choices(self, host, found, problems):
        """The choosers' entries: FOUND ({option: [name]} of host_start_choices; None: being read), PROBLEMS ({option:
        why a list could not be read})."""
        where = tr("this computer") if host == "local" else printable(host)
        for long in self.HOST_CHOICES:
            widget = self.widgets.get(long)
            names = None if found is None else found.get(long, [] if long not in problems else None)
            if isinstance(widget, ListField):
                widget.set_choices(names if names is not None or long not in problems else [], where,
                                   problems.get(long))
            elif isinstance(widget, QComboBox) and widget.isEditable():
                current = widget.currentText()
                widget.blockSignals(True)
                try:
                    widget.clear()
                    widget.addItem("")
                    for name in names or []:
                        widget.addItem(name)
                    widget.setCurrentText(current)  # what was typed or chosen stays, also a name it lacks
                finally:
                    widget.blockSignals(False)
                option = next(o for o in self.options if o.long == long)
                note = (tr("Reading what {host} has…", host=where) if names is None and long not in problems else
                        tr("Could not be read: {error}", error=problems[long]) if long in problems else
                        tr("{count} on {host}", count=len(names), host=where))
                widget.setToolTip(safe_tooltip(f"{option.help}\n{note}" if option.help else note))

    def values(self):
        result = {}
        for option in self.options:
            widget = self.widgets[option.long]
            if isinstance(widget, QCheckBox):
                result[option.long] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                result[option.long] = widget.currentText()
            elif isinstance(widget, (ListField, RulesField, DeviceField, PathField, MultiChoice)):
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
                widget.setToolTip(safe_tooltip(why))
            elif isinstance(widget, QComboBox):
                for index in range(widget.count()):
                    if widget.itemText(index) in values:
                        widget.model().item(index).setEnabled(False)
            elif isinstance(widget, MultiChoice):
                for value in values:
                    if value in widget.boxes:
                        widget.boxes[value].setEnabled(False)
                        widget.boxes[value].setToolTip(safe_tooltip(why))
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
                if text and widget.findText(text) < 0 and not widget.isEditable():
                    # e.g. a GPU kind this computer lacks: kept, not dropped
                    widget.addItem(text)
                    if option.long == "--gpu" and widget.property("host") in (None, "local"):
                        widget.setItemData(widget.count() - 1, tr("not found on this computer"),
                                           Qt.ItemDataRole.ToolTipRole)
                    widget.setEnabled(True)
                widget.setCurrentText(text)
            elif isinstance(widget, (ListField, RulesField, DeviceField, MultiChoice)):
                widget.set_value(value or [])
            elif isinstance(widget, PathField):
                widget.set_value(value)
            else:
                widget.setText("" if value is None else str(value))
        self.mark_dangerous()


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
        # Right beside the agent: the model it works with, its own provider or a server of yours (--llm).
        self.model_choice = QComboBox()
        self.model_choice.setMinimumWidth(200)
        self.model_choice.setToolTip(tr("The agent's own provider (cloud), or a model server of yours (sandbox-ai "
                                        "llm): --llm NAME[:MODEL], without --model"))
        self.model_only = QCheckBox(tr("only this server"))
        self.model_only.setToolTip(tr("--no-provider-defaults: the agent reaches the model server and nothing else"))
        self.llm_servers, self.llm_loading, self.llm_read_at, self.model_seen = [], False, 0.0, None
        self.llm_again, self.llm_host = False, LOCAL  # the host whose servers llm_servers are
        # Where it starts: this machine, or a host of sandbox-ai hosts (its launcher there checks and starts it).
        self.host_choice = HostCombo(sai)
        self.host_known = {}  # {(host, agent): that host's launcher options, or None: could not be read}
        layout.addWidget(flow(self.host_choice, buddy(tr("Agent"), self.agent), self.agent,
                              buddy(tr("Model"), self.model_choice), self.model_choice, self.model_only,
                              buddy(tr("Profile"), self.profile), self.profile, load, save, project))
        self.host_note = plain_label(role="secondary")
        self.host_note.hide()
        layout.addWidget(self.host_note)
        # A preset of the chosen host's catalogue (its sandbox-ai presets, the app's own, yours): sandbox-ai's becomes
        # --preset NAME (with --preset-value for what it asks), the launcher there applies it; the app's own and
        # yours fill in the form's options.
        self.preset_choice = QComboBox()
        self.preset_choice.setMinimumWidth(260)
        self.preset_choice.setToolTip(tr("A preset of the chosen host: sandbox-ai's own there (sandbox-ai presets list), "
                                         "the app's own and yours. What it leaves to you is asked for."))
        self.preset_choice.activated.connect(lambda _index: self.preset_picked())
        self.preset_gallery_button = QPushButton(tr("Gallery…"))
        self.preset_gallery_button.setToolTip(tr("The presets of the chosen host with a picture of each board, their "
                                                 "scores and what they need"))
        self.preset_gallery_button.clicked.connect(lambda _=False: self.open_preset_gallery())
        self.preset_note = plain_label(role="secondary")
        self.preset_note.hide()
        layout.addWidget(flow(buddy(tr("Preset"), self.preset_choice), self.preset_choice, self.preset_gallery_button))
        layout.addWidget(self.preset_note)
        self.preset_catalogue, self.presets_host, self.presets_reading = PR.Catalogue(), None, None
        self.choices_host, self.choices_read_at, self.choices_token = None, 0.0, 0
        # A secret or VPN profile added or removed (Credentials page): the choosers are read again when shown next.
        from .name_fields import hub
        hub().changed.connect(self.names_changed)
        self.form = OptionsForm(self.options)
        try:
            self.form.apply_policy(sai.forbidden())
        except Exception:  # noqa: BLE001 - a broken policy is shown on the System page
            pass
        # {option: (enabled, tooltip) before} of the options greyed out because the chosen host does not know them:
        # given back as they were (the policy's greying, a GPU this computer lacks) when a host knows them again.
        self.host_greyed = {}
        default = backend.setting("default_agent")
        if default in sai.agents:
            self.agent.setCurrentText(default)
        layout.addWidget(self.form, 1)
        arguments = QHBoxLayout()
        self.agent_args = QLineEdit()
        self.agent_args.setPlaceholderText(tr("arguments for the agent itself, e.g. --model sonnet"))
        arguments.addWidget(buddy(tr("Agent arguments"), self.agent_args))
        arguments.addWidget(self.agent_args)
        layout.addLayout(arguments)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(MONO)
        self.preview.setFixedHeight(70)
        self.preview.setAccessibleName(tr("Command line"))
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
        self.session.setAccessibleName(tr("Session name"))
        self.session.setMaximumWidth(160)
        session = QPushButton(tr("Start as session"))
        session.setToolTip(tr("A detachable session: attach and detach from the Sandboxes page"))
        session.clicked.connect(self.start_session)
        write = self.write_button = QPushButton(tr("Save as project configuration…"))
        write.setToolTip(tr("Write .sandbox-ai.json into the first workspace and trust it"))
        write.clicked.connect(self.write_project)
        layout.addWidget(flow(dry, in_app, start, self.session, session, write))
        self.host_choice.chosen.connect(lambda _host: self.host_changed())
        self.agent.currentTextChanged.connect(lambda _agent: self.read_host_options())
        self.form.changed.connect(self.update_preview)
        self.agent.currentTextChanged.connect(self.update_preview)
        self.agent_args.textChanged.connect(self.update_preview)
        self.form.changed.connect(self.show_model)
        self.agent.currentTextChanged.connect(lambda _text: self.show_model(force=True))
        self.model_choice.activated.connect(lambda _index: self.model_chosen())
        self.model_only.toggled.connect(lambda _on: self.model_chosen())
        self.refresh_profiles()
        self.update_preview()
        self.show_model(force=True)
        self.read_llm_servers()

    # -- the model: the agent's provider or a server of yours (sandbox-ai llm) --

    def read_llm_servers(self):
        """The model servers registered on the host it starts on (sandbox-ai llm list there, and for a host's servers
        their models: llm models NAME), read in the background; again when the page is shown after a while (a server
        added on the Model servers page meanwhile) and when another host is chosen."""
        if "--llm" not in self.form.widgets:
            return
        host = self.host()
        if self.llm_loading:
            self.llm_again = self.llm_loading != host  # the host it is read for is no longer the one chosen
            return
        self.llm_loading, self.llm_again = host, False
        # A read, not work: no owner, so it neither greys out the page's buttons (busy()) nor holds up quitting.
        background(lambda: self.sai.host_llm_servers(host), timeout=180,
                   done=lambda result, error: alive(self) and self.llm_servers_read(host, result, error))

    def llm_servers_read(self, host, result, error):
        self.llm_loading = False
        if host != self.host() or self.llm_again:
            self.read_llm_servers()  # another host was chosen meanwhile: its servers, never these
            return
        self.llm_read_at, self.llm_host = time.monotonic(), host
        self.llm_servers = result if error is None and isinstance(result, list) else []
        self.show_model(force=True)

    def showEvent(self, event):  # noqa: N802
        self.host_choice.reload()  # a host added on the Hosts page meanwhile
        super().showEvent(event)
        if time.monotonic() - self.llm_read_at > 30:
            self.read_llm_servers()
        if self.presets_host != self.host():
            self.refresh_presets()
        if self.choices_host != self.host() or time.monotonic() - self.choices_read_at > 60:
            self.read_host_choices()

    # -- presets of the chosen host --

    def refresh_presets(self):
        """The preset chooser: the chosen host's catalogue (presets.catalogue), the same as the gallery's and the
        wizard's, made in the background (each of sandbox-ai's presets is checked against its options: tenths of a
        second, at the first show of the page too). Another host's sandbox-ai presets are read in the background as
        well (meanwhile what it said last)."""
        host = self.host()
        self.presets_host = host
        self.presets_token = token = getattr(self, "presets_token", 0) + 1

        def make():
            try:
                return PR.catalogue(self.sai, host)
            except Exception as exc:  # noqa: BLE001 - a broken store of yours: the app's own still offered
                return PR.catalogue(self.sai, host, mine=[], problems=[str(exc)])
        background(make, timeout=90, done=lambda catalogue, error: alive(self) and token == self.presets_token
                   and error is None and self.show_presets(host, catalogue))

    def show_presets(self, host, catalogue):
        from .preset_gallery import fill_preset_combo
        self.preset_catalogue = catalogue
        current = self.preset_choice.currentData()
        fill_preset_combo(self.preset_choice, self.preset_catalogue, current)
        self.preset_choice.insertItem(0, tr("(none)"), "")
        if not current or self.preset_choice.findData(current) < 0:
            self.preset_choice.setCurrentIndex(0)
        if host != LOCAL and not self.preset_catalogue.complete and self.presets_reading != host:
            self.presets_reading = host
            background(PR.host_documents, self.sai, host, timeout=90,
                       done=lambda _result, error: alive(self) and self.host_presets_read(host, error))

    def host_presets_read(self, host, error):
        self.presets_reading = None
        if host != self.host():
            return
        if error is not None:
            self.show_preset_note(tr("The presets of {host} could not be read: {error}", host=printable(host),
                                     error=printable(error)[-300:]), "notice-warn")
            return
        self.refresh_presets()

    def show_preset_note(self, text, role="secondary"):
        self.preset_note.setText(text)
        self.preset_note.setProperty("role", role)
        self.preset_note.style().unpolish(self.preset_note)
        self.preset_note.style().polish(self.preset_note)
        self.preset_note.setVisible(bool(text))

    def open_preset_gallery(self):
        """The gallery of the chosen host's presets; the one chosen there (and what was filled in) comes here."""
        from .preset_gallery import PresetGallery
        gallery = PresetGallery(self.sai, None, self.agent.currentText(), self.policy_or_empty(), None, self,
                                backend.setting("default_backend"), host=self.host())
        if gallery.exec() != gallery.DialogCode.Accepted or gallery.result_ is None:
            return None
        preset, values = gallery.result_
        self.refresh_presets()
        return self.apply_preset(preset, values or {})

    def policy_or_empty(self):
        try:
            return self.sai.policy()[0] or {}
        except Exception:  # noqa: BLE001 - a broken policy is shown on the System page
            return {}

    def preset_picked(self):
        """A preset chosen in the chooser: sandbox-ai's of another host as it is there now (`presets show NAME
        --json`), then what it leaves to you asked for and applied (apply_preset)."""
        key = self.preset_choice.currentData()
        if not key:
            self.set_option("--preset", "")
            self.set_option("--preset-value", [])
            self.show_preset_note("")
            return None
        try:
            preset = self.preset_catalogue.find(key)
        except ValueError:
            return None
        host = self.host()
        if preset.source == "sandbox-ai" and host != LOCAL:
            self.show_preset_note(tr("Reading {name} on {host}…", name=printable(preset.name), host=printable(host)))
            background(self.sai.host_preset, host, preset.name, owner=self, timeout=60,
                       done=lambda document, error: alive(self) and self.preset_shown(host, preset, document, error))
            return None
        return self.apply_preset(preset)

    def preset_shown(self, host, preset, document, error):
        if host != self.host():
            return None  # another host was chosen meanwhile: never this one's preset there
        if error is None:
            try:
                preset = PR.from_sandbox_ai(self.sai, document, host=host)
            except ValueError as exc:
                self.show_preset_note(printable(exc), "notice-warn")
                return None
        return self.apply_preset(preset)

    def set_option(self, long, value):
        widget = self.form.widgets.get(long)
        if widget is None:
            return
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QComboBox):
            widget.setCurrentText(str(value or ""))
        elif isinstance(widget, (ListField, DeviceField, MultiChoice)):
            widget.set_value(value or [])
        elif isinstance(widget, PathField):
            widget.set_value(value)
        else:
            widget.setText(str(value or ""))

    def apply_preset(self, preset, values=None):
        """PRESET into the form, its placeholders asked for first (the fill-in form, with what the chosen host has).
        sandbox-ai's: --preset NAME and --preset-value NAME=VALUE (the launcher there applies the preset itself, its
        ruleset and MCP servers too). The app's own and yours: their options replace the form's (the workspace stays);
        MCP servers they define and a ruleset of the project need its file (the Board). True if applied."""
        from .preset_gallery import PlaceholderForm
        from . import board as B
        from . import widgets
        host = self.host()
        if values is None and preset.placeholders:
            form = PlaceholderForm(self.sai, preset, self, agent=self.agent.currentText(), host=host)
            if widgets.AUTO_ANSWER is None and form.exec() != QDialog.DialogCode.Accepted:
                return False
            values = form.values() or {}
        values = values or {}
        notes = []
        if preset.agents and self.agent.currentText() not in preset.agents:
            self.agent.setCurrentText(preset.agents[0])
            notes.append(tr("It is for {agents}: {agent} chosen.", agents=", ".join(preset.agents),
                            agent=preset.agents[0]))
        elif preset.agent and preset.agent in self.sai.agents:
            self.agent.setCurrentText(preset.agent)
        if preset.source == "sandbox-ai":
            pairs = [f"{name}={values[marker]}" for marker, name in preset.value_names if values.get(marker)]
            self.set_option("--preset", preset.name)
            self.set_option("--preset-value", pairs)
            left = [name for marker, name in preset.value_names if not values.get(marker)
                    and not next((p.optional for p in preset.placeholders if p.marker == marker), False)]
            notes.insert(0, tr("--preset {name}: the sandbox-ai on {host} applies it at the start (its options, MCP "
                               "servers and ruleset); the options here come on top.", name=printable(preset.name),
                               host=tr("this computer") if host == LOCAL else printable(host)))
        else:
            board = PR.build(self.sai, preset, None, agent=self.agent.currentText(), values=values)
            kept = self.form.values().get("--workspace") or []
            parsed, _ = backend.values_from_argv(self.options, B.to_argv(self.sai, board, workspaces=False),
                                                 backend.canonical_of(self.sai))
            parsed["--workspace"] = kept
            self.form.set_values(parsed)
            left = [marker for marker, _ in B.placeholders(board)]
            if preset.servers or preset.rules is not None:
                notes.append(tr("Its own MCP servers and ruleset belong in the project's file: create it on the Board "
                                "(New from preset) to have them; only its options are here."))
        if left:
            notes.append(tr("Still to fill in: {list}", list=", ".join(printable(m) for m in left)))
        self.show_preset_note(" ".join(notes), "notice-warn" if left else "secondary")
        index = self.preset_choice.findData(preset.key)
        if index >= 0:
            self.preset_choice.setCurrentIndex(index)
        self.form.changed.emit()
        return True

    # -- what the chosen host has, for the choosers (--vpn, --kit, --identity, --mcp, --image-variant) --

    def read_host_choices(self):
        """The chosen host's VPN profiles, kits, identities, MCP servers and image variants (backend.host_start_choices),
        read in the background; a late answer for another host is dropped (a token per read)."""
        host = self.host()
        self.choices_token += 1
        token = self.choices_token
        self.choices_host = host
        self.form.set_choices(host, None, {})
        background(self.sai.host_start_choices, host, timeout=180,
                   done=lambda result, error: alive(self) and self.host_choices_read(token, host, result, error))

    def names_changed(self, _kind, host):
        if host == self.choices_host:
            self.choices_read_at = 0.0

    def host_choices_read(self, token, host, result, error):
        if token != self.choices_token or host != self.host():
            return
        self.choices_read_at = time.monotonic()
        if error is not None:
            found, problems = {}, {long: printable(error)[-200:] for long in OptionsForm.HOST_CHOICES}
        else:
            found, problems = result
        self.form.set_choices(host, found, problems)

    # -- the host it starts on --

    def host(self):
        return self.host_choice.current()

    def known_options(self):
        """The options the chosen host's launcher takes (None: this machine, or not known: every option)."""
        host = self.host()
        return None if host == LOCAL else self.host_known.get((host, self.agent.currentText()))

    def host_changed(self):
        """Another host: its launcher's options (read in the background) decide what is offered; paths are that
        host's, typed in; the project's file of this machine is not that host's."""
        host = self.host()
        remote = host != LOCAL
        workspace = self.form.widgets.get("--workspace")
        if workspace is not None and self.project:
            if remote and workspace.value() == [self.project]:
                workspace.set_value([])  # this machine's folder is not a folder there
            elif not remote and not workspace.value():
                workspace.set_value([self.project])
        for widget in self.form.widgets.values():
            if hasattr(widget, "set_local"):
                widget.set_local(not remote)
        self.form.set_host(host)  # --gpu's kinds and the limits' hints: that host's, not this computer's
        if self.presets_host is not None and self.presets_host != host:
            self.refresh_presets()
        if self.choices_host is not None and self.choices_host != host:
            self.read_host_choices()
        set_enabled(self.write_button, not remote, tr("It writes the project file on this machine; on {host} a "
                                                      "project's file is written there (sandbox-ai wizard).",
                                                      host=printable(host)))
        self.host_note.setVisible(remote)
        self.show_host_note()
        if self.llm_host != host:  # the Model choice: that host's model servers, not this machine's
            self.llm_servers = []
            self.show_model(force=True)
            self.read_llm_servers()
        self.read_host_options()
        self.apply_host_options()
        self.show_project_note()
        self.update_preview()

    def show_host_note(self, problem=None):
        host = self.host()
        text = tr("Starts on {host} (over SSH): workspace and mount paths are {host}'s - type them in, nothing of this "
                  "computer is chosen. What its sandbox-ai does not know is greyed out and left out.",
                  host=printable(host))
        if problem:
            text += " " + tr("Its options could not be read: {error}", error=printable(problem)[-200:])
        self.host_note.setText(text)

    def read_host_options(self):
        host, agent = self.host(), self.agent.currentText()
        if host == LOCAL or (host, agent) in self.host_known:
            self.apply_host_options()
            return
        self.host_known[(host, agent)] = None  # asked once; until it answers every option is offered
        background(self.sai.host_launch_options, host, agent, timeout=90,
                   done=lambda found, error: alive(self) and self.host_options_read(host, agent, found, error))

    def host_options_read(self, host, agent, found, error):
        self.host_known[(host, agent)] = set(found) if error is None and found else None
        if (host, agent) == (self.host(), self.agent.currentText()):
            if error is not None:
                self.show_host_note(str(error))
            self.apply_host_options()
            self.update_preview()

    def apply_host_options(self):
        """Grey out the options the chosen host's launcher does not know; the others are as they were (what the
        policy greyed out stays so)."""
        known, host = self.known_options(), self.host()
        for long, widget in self.form.widgets.items():
            missing = known is not None and long not in known
            if missing:
                self.host_greyed.setdefault(long, (widget.isEnabled(), widget.toolTip()))
                widget.setEnabled(False)
                widget.setToolTip(safe_tooltip(tr("The sandbox-ai on {host} does not know {option}", host=printable(host),
                                     option=long)))
            elif long in self.host_greyed:
                enabled, tip = self.host_greyed.pop(long)
                widget.setEnabled(enabled)
                widget.setToolTip(tip)  # as it was (escaped when it was set)

    def show_model(self, force=False):
        """The Model choice as the options say (--llm, --no-provider-defaults): after a change of the options, the
        agent (which servers it can use) or the servers."""
        from .wizard import fill_model_combo
        values = self.form.values()
        seen = (str(values.get("--llm") or "").strip(), bool(values.get("--no-provider-defaults")))
        if not force and seen == self.model_seen:
            return
        self.model_seen = seen
        self.model_choice.setEnabled("--llm" in self.form.widgets)
        fill_model_combo(self.model_choice, self.model_only, self.llm_servers, self.agent.currentText(), self.sai,
                         seen[0], seen[1])

    def model_chosen(self):
        """A model chosen beside the agent: a server sets --llm NAME[:MODEL] (and --no-provider-defaults with "only
        this server") and empties --model (the launcher refuses it with --llm); the agent's provider takes both away."""
        value = self.model_choice.currentData() or ""
        choice = WL.choice_of(value, self.model_only.isChecked())
        values = WL.form_values(self.form.values(), choice)
        for option in ("--llm", "--no-provider-defaults", "--model"):
            widget = self.form.widgets.get(option)
            if widget is None or option not in values:
                continue
            widget.blockSignals(True)
            try:
                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(values[option]))
                elif isinstance(widget, QComboBox):
                    widget.setCurrentText(str(values[option] or ""))
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(values[option] or ""))
            finally:
                widget.blockSignals(False)
        self.form.changed.emit()  # the preview, the state kept, and this choice once more

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
        if isinstance(saved.get("host"), str):
            self.host_choice.choose(saved["host"])
        self.current_agent = self.agent.currentText()
        self.load_agent_form(self.current_agent)
        self.agent.currentTextChanged.connect(self.agent_switched)
        self.form.changed.connect(state.schedule)
        self.agent_args.textChanged.connect(state.schedule)
        self.profile.currentTextChanged.connect(state.schedule)
        self.host_choice.chosen.connect(lambda _host: state.schedule())
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
        saved["host"] = self.host()
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
        values, _ = backend.values_from_argv(self.options, options, backend.canonical_of(self.sai))
        values["--workspace"] = self.form.values().get("--workspace") or []
        self.form.set_values(values)
        self.agent_args.setText(shlex.join(scrub_arguments(arguments)))

    def set_project(self, path):
        """Start in the project PATH: its folder becomes the first (and only) workspace. Its .sandbox-ai.json is
        never copied into the form: the launcher applies a trusted one itself."""
        self.project = path
        widget = self.form.widgets.get("--workspace")
        if widget is not None and self.host() == LOCAL:  # on another host the workspace is a folder there
            widget.set_value([path])
        self.show_project_note()

    def show_project_note(self):
        """The note on the project's .sandbox-ai.json; its state is read in a thread (a slow disk)."""
        self.note_token = getattr(self, "note_token", 0) + 1
        if not self.project:
            self.project_note.setText("")
            self.review_button.hide()
            return
        if self.host() != LOCAL:  # this machine's project file does not apply there
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
        """The launcher's options of the form; for another host without the ones its launcher does not know (a start
        or dry run refuses those by name first: refused_options)."""
        return backend.argv_for_host(self.options, self.form.values(), self.known_options())

    def refused_options(self, title):
        """True (and said) when options set in the form are unknown to the chosen host's launcher: they would be left
        out, and leaving out a restrictive one (--workspace-ro, --no-provider-defaults) starts a weaker sandbox."""
        dropped = backend.dropped_options(self.options, self.form.values(), self.known_options())
        if not dropped:
            return False
        warn(self, title, tr("The sandbox-ai on {host} does not know these options: {list}. Clear them, or update "
                             "sandbox-ai there; none is left out without a word.", host=printable(self.host()),
                             list=", ".join(dropped)))
        return True

    def require_workspace(self, title):
        """True when the start names its workspace. An empty field would make the launcher take the directory the
        app (or, on another host, the SSH login) runs in - often HOME - as the writable /work: on this machine the
        project folder chosen above fills it in (said), otherwise nothing starts until one is chosen."""
        if self.workspace():
            return True
        widget = self.form.widgets.get("--workspace")
        if self.host() == LOCAL and self.project and widget is not None and os.path.isdir(self.project):
            widget.set_value([self.project])
            self.update_preview()
            self.project_note.setText(tr("The workspace was empty: the project folder {path} is the workspace.",
                                         path=printable(self.project)))
            return True
        where = tr("a folder on {host}, typed in", host=printable(self.host())) if self.host() != LOCAL else \
            tr("a folder of this machine, or open a project folder above")
        warn(self, title, tr("Choose the workspace first (the tab Workspace of the options: {where}). Without one "
                             "the sandbox would get the folder the app runs in - often your HOME - as its writable "
                             "/work.", where=where))
        return False

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
        host = self.host()
        where = ["hosts", "run", host, "--"] if host != LOCAL else []  # the same start from this machine's shell
        line = ["sandbox-ai", *where, self.agent.currentText(), *self.argv(), *(["--", *args] if args else [])]
        self.preview.setPlainText(shlex.join(line))

    def refresh_profiles(self):
        self.profile.clear()
        self.profile.addItems(sorted(self.sai.profiles()))

    def set_from_argv(self, agent, argv, agent_args=()):
        values, unknown = backend.values_from_argv(self.options, argv, backend.canonical_of(self.sai))
        self.agent.setCurrentText(agent)
        self.form.set_values(values)
        self.agent_args.setText(shlex.join(agent_args))
        if unknown:
            QMessageBox.warning(self, tr("Options not shown"), shown_as_text(tr(
                "These are not options of this sandbox-ai version and were left out:") + "\n" + " ".join(unknown)))

    def load_profile(self):
        name = self.profile.currentText()
        profile = self.sai.profiles().get(name)
        if profile:
            host = profile.get("host") if isinstance(profile.get("host"), str) else LOCAL
            if not self.host_choice.choose(host):
                warn(self, tr("Profile"), tr("The profile {name} starts on {host}, which is not a host of sandbox-ai "
                                             "hosts (any more): the host stays as chosen.", name=printable(name),
                                             host=printable(host)))
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
            self.sai.save_profile(name.strip(), self.agent.currentText(), self.argv(), self.agent_arguments(),
                                  host=self.host())
            self.refresh_profiles()
            self.profile.setCurrentText(name.strip())

    def load_project(self, directory=None):
        """Review a folder's .sandbox-ai.json (written there by whoever could write the folder, the agent
        too) and trust exactly the bytes shown if the human agrees. The file's options are never copied into
        the form: the launcher applies a trusted file itself when the folder is the first workspace, and
        refuses it if it changes afterwards. With another host chosen: a folder there, reviewed and trusted by that
        host's sandbox-ai (load_project_on_host)."""
        if self.host() != LOCAL:
            self.load_project_on_host(directory)
            return None
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
            intro = state + "\n\n" + tr("Trust exactly this content (see the details: the changes against the version "
                                         "you trusted last, and the whole file)? The launcher refuses the file if it "
                                         "changes afterwards.")
            # Every key is named and what weakens marked; the details diff against the version trusted LAST.
            question, details = PT.question(self.sai, directory, review.document, intro,
                                            new_text=review.data.decode("utf-8", "replace"))
            # Trusting outlives this start (every later start of the project gets the file's options): asked first.
            if not ask_first(self, "project-trust", tr("Review the project configuration"), question, tr("Trust"),
                             details=details):
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

    def load_project_on_host(self, directory=None):
        """A project folder on the chosen host (typed in: the first workspace of the form offered): its
        .sandbox-ai.json reviewed through that host's sandbox-ai as a file of this machine is (every key, what weakens,
        the changes against the version trusted last; host_trust) and trusted exactly as shown after a yes; then the
        form starts in that folder there."""
        host = self.host()
        if not directory:
            workspaces = self.form.values().get("--workspace") or []
            directory, ok = QInputDialog.getText(self, tr("Project folder with .sandbox-ai.json"), shown_as_text(tr(
                "The project folder on {host}, an absolute path:", host=printable(host))),
                text=str(workspaces[0]) if workspaces else "")
            if not ok:
                return
        directory = (directory or "").strip()
        path = PT.config_path(directory)
        if path is None:
            warn(self, tr("No project configuration"), tr("Give the absolute path of a folder on {host}.",
                                                          host=printable(host)))
            return
        folder = path[:-len("/.sandbox-ai.json")] or "/"
        from .host_trust import review_and_trust

        def done(trusted, document):
            if not trusted or host != self.host():
                return
            agent = (document or {}).get("agent")
            self.set_from_argv(agent if agent in self.sai.agents else self.agent.currentText(),
                               ["--workspace", folder])
            inform(self, tr("Project configuration"), on_host(host, tr(
                "Trusted: {path}. The form starts in that folder; the launcher applies the file's options itself.",
                path=printable(path))), modal=False)
        review_and_trust(self, self.sai, host, path, done=done, source=self.open_button)

    def dry_run(self):
        """The launcher's plan, nothing started: on the chosen host by its own launcher."""
        if self.refused_options(tr("Dry run")) or not self.require_workspace(tr("Dry run")):
            return
        background(self.sai.host_dry_run, self.host(), self.agent.currentText(), self.argv(), owner=self,
                   source=self.dry_button, timeout=120, done=self.dry_run_done)

    def dry_run_done(self, result, error):
        plan, message = result if error is None else (None, str(error))
        if plan is None:
            warn(self, tr("The launcher refused these options"), message)
            return
        show_text(self, tr("Plan (dry run)"), (message + "\n\n" if message else "") + json.dumps(plan, indent=2))

    def start(self, session=None, embedded=False):
        if self.refused_options(tr("Not started")) or not self.require_workspace(tr("Not started")):
            return False
        try:
            host = self.host()
            if host != LOCAL:  # ssh -tt there: its tmux keeps an interactive start when the tab closes
                argv = self.sai.host_launch_argv(host, self.agent.currentText(), self.argv(), self.agent_arguments(),
                                                 session)
                title = f"{printable(host)}: {self.agent.currentText()}" + (f" ({session})" if session else "")
                self.terminal(argv, title, embedded=embedded, cwd=None)
                return True
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
        if self.host() != LOCAL:
            return
        document, main = self.sai.project_document(self.agent.currentText(), self.options, self.form.values(),
                                                   self.agent_arguments())
        if not main:
            QMessageBox.information(self, tr("Project configuration"),
                                    tr("Choose a workspace first: the file is written there."))
            return
        intro = tr("Write {path} with this content and trust exactly it? The details show the changes against the "
                   "version you trusted last, and the whole file.", path=f"{main}/.sandbox-ai.json")
        question, details = PT.question(self.sai, main, document, intro)
        # Trusted, the file's options apply at every start there without a question: asked first (the policy of
        # project-trust), Cancel the default.
        if ask_first(self, "project-trust", tr("Write and trust"), question, tr("Write and trust"),
                     details=details):  # the folder may be slow (a network file system): in a thread
            background(self.sai.write_project, main, document, owner=self, work=True, timeout=60,
                       done=self.project_written)

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
        self.hosts.setPlaceholderText("api.example.org *.example.net pypi.org")
        self.hosts.setToolTip(safe_tooltip(tr("Names separated by spaces. *.DOMAIN opens every name below DOMAIN, each "
                                              "as it is looked up (not for shared hosting such as *.github.io, nor "
                                              "public suffixes).")))
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
        self.nameservers = QLineEdit()
        self.nameservers.setPlaceholderText("192.0.2.53 192.0.2.54")
        self.nameservers.setToolTip(safe_tooltip(tr("Resolvers instead of the defaults (--nameserver; implies real "
                                                    "DNS), separated by spaces")))
        self.keep = QCheckBox(tr("Keep it for later starts (into the project's .sandbox-ai.json)"))
        self.keep.setToolTip(safe_tooltip(tr("net allow --keep: the options go into the project's .sandbox-ai.json, "
                                             "only while that file is exactly what you trusted (the new file is "
                                             "trusted at once); logins are kept as references, never as tokens")))
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
        if "--nameserver" in self.supported:
            form.addRow(tr("Nameservers"), self.nameservers)
        if "--git-token-env" in self.supported:
            form.addRow(tr("Git login (the token, never shown)"), git_row)
        self.presets = {}
        for name, hosts in presets.items():
            box = QCheckBox(", ".join(hosts)[:80])
            form.addRow(f"--allow-{name}", box)
            self.presets[name] = box
        if "--keep" in self.supported:  # net allow only (a revoke keeps nothing)
            form.addRow(tr("Later starts"), self.keep)
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
        if "--nameserver" in self.supported:
            args += [t for server in self.nameservers.text().split() for t in ("--nameserver", server)]
        url, ref = self.git_url.text().strip(), self.git_ref.text().strip()
        if "--git-token-env" in self.supported and url and ref:
            args += [f"--git-token-{self.git_kind.currentData()}", f"{url}={ref}"]
        return args

    def kept(self):
        """Whether what is allowed is also kept for later starts (net allow --keep)."""
        return "--keep" in self.supported and self.keep.isChecked()


class AllowedDialog(QDialog):
    """What a running sandbox was given since its start (sandbox-ai net show), Untaint… for its taint, and - where
    its sandbox-ai can (net show --keep) - Keep for later starts… and Keep everything for later starts…: what it was
    given, into its project's .sandbox-ai.json (KEEP(every) does it, asked first)."""

    def __init__(self, parent, title, text, untaint, keep=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        self.view = QPlainTextEdit(text)
        self.view.setReadOnly(True)
        self.view.setFont(MONO)
        self.view.setAccessibleName(tr("Allowed during the run"))
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.untaint_button = QPushButton(tr("Untaint…"))
        self.untaint_button.setAccessibleName(tr("Untaint"))
        self.untaint_button.setToolTip(tr("Take back the run's taint (sandbox-ai net untaint): only once you checked "
                                          "the untrusted content that entered it"))
        self.untaint_button.clicked.connect(lambda _=False: untaint())
        self.keep_button = QPushButton(tr("Keep for later starts…"))
        self.keep_button.setToolTip(tr("sandbox-ai net show --keep: the names and addresses you allowed (or approved "
                                       "always), wildcards, networks, DNS, host ports, devices and workspaces added go "
                                       "into the project's trusted .sandbox-ai.json; no Git logins"))
        self.keep_all_button = QPushButton(tr("Keep everything for later starts…"))
        self.keep_all_button.setToolTip(tr("sandbox-ai net show --keep-all: also what approvals opened once or for "
                                           "the run"))
        for button, every in ((self.keep_button, False), (self.keep_all_button, True)):
            button.clicked.connect(lambda _=False, e=every: keep(e))
            button.setVisible(keep is not None)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        row.addWidget(self.untaint_button)
        row.addWidget(self.keep_button)
        row.addWidget(self.keep_all_button)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self.resize(720, 460)


DEVICE_EXAMPLE = "/dev/..."  # the same in every language
LOCAL = "local"  # Instance.host of this machine's own sandboxes


class DeviceDialog(QDialog):
    """A device for a running sandbox: the devices under /dev, refused ones greyed out with the launcher's
    reason, warned ones with what they expose; or a path typed in."""

    def __init__(self, parent, candidates, title=None):
        super().__init__(parent)
        self.setWindowTitle(title or tr("Add device"))
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(DEVICE_EXAMPLE)
        for path, why, note in candidates:
            item = QListWidgetItem(path + (f"  ({tr('refused')}: {why})" if why else f"  ({note})" if note else ""))
            item.setData(Qt.ItemDataRole.UserRole, path)
            if why:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(safe_tooltip(tr("sandbox-ai refuses it: {why}", why=why)))
            self.list.addItem(item)
        self.list.itemSelectionChanged.connect(lambda: self.edit.setText(
            self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else self.edit.text()))
        self.list.setVisible(bool(candidates))  # another host's devices are typed in: its /dev is not listed here
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


class WorkspaceDialog(QDialog):
    """A host folder or a single file for a running sandbox (sandbox-ai workspace add): the path, a name under
    /workspaces (optional; checked as sandbox-ai checks --name), read-only or not. For a sandbox on another HOST the
    path is that host's: typed in, never chosen from this computer's."""

    def __init__(self, parent, run, host="local"):
        super().__init__(parent)
        remote = host != "local"
        self.setWindowTitle(tr("Add workspace to {run} on {host}", run=printable(run), host=printable(host)) if remote
                            else tr("Add workspace to {run}", run=printable(run)))
        form = QFormLayout(self)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(tr("/path/to/a/folder or /path/to/a/file"))
        self.edit.setAccessibleName(tr("Folder or file"))
        row = QHBoxLayout()
        row.addWidget(self.edit, 1)
        self.browse_buttons = []
        if not remote:
            for text, slot in ((tr("Folder…"), self.browse), (tr("File…"), self.browse_file)):
                button = QPushButton(text)
                button.clicked.connect(lambda _=False, s=slot: s())
                row.addWidget(button)
                self.browse_buttons.append(button)
        form.addRow(tr("Folder or file on {host}", host=printable(host)) if remote else tr("Folder or file"), row)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("its own name"))
        self.name_edit.setToolTip(tr("At /workspaces/NAME: 1-64 letters, digits, _ . -, starting with a letter or "
                                     "digit (sandbox-ai workspace add --name); empty: the folder's or file's name"))
        self.name_edit.setAccessibleName(tr("Name under /workspaces"))
        self.name_edit.textChanged.connect(lambda _="": self.check())
        form.addRow(tr("Name under /workspaces"), self.name_edit)
        self.read_only = QCheckBox(tr("Read-only (the agent cannot change it)"))
        form.addRow("", self.read_only)
        self.problem = plain_label(role="notice-warn")
        self.problem.hide()
        form.addRow(self.problem)
        form.addRow(plain_label(tr("It appears at /workspaces/NAME until the sandbox ends; a session keeps a folder "
                                   "for a restart (with its name) unless it is read-only, a file only for this run. "
                                   "sandbox-ai checks it as at a start: private places, package stores and the "
                                   "policy are refused."), role="secondary"))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        self.resize(560, 0)

    def browse(self):
        path = choose_folder(self, tr("Folder for the sandbox"), key="workspace")
        if path:
            self.edit.setText(path)

    def browse_file(self):
        path = choose_file(self, tr("File for the sandbox"), key="workspace-file")
        if path:
            self.edit.setText(path)

    def check(self):
        """A name sandbox-ai would refuse is said at once, and OK waits for a good one."""
        name = self.name()
        bad = bool(name) and not backend.valid_workspace_name(name)
        self.problem.setText(tr("A name is 1-64 letters, digits, _ . -, starting with a letter or digit.") if bad
                             else "")
        self.problem.setVisible(bad)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not bad)

    def directory(self):
        return self.edit.text().strip()

    def name(self):
        return self.name_edit.text().strip()


HELPER_TITLES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}


class LiveMcpDialog(QDialog):
    """Add MCP server…: a server defined on the start's HOST (SERVERS: [(name, status)] of `sandbox-ai mcp list`
    there) or another agent as a helper (claude, codex, opencode), into the running sandbox of START now (sandbox-ai
    mcp attach … --live) and kept for its later starts. PROBLEM: why the servers could not be read."""

    def __init__(self, parent, start, host=LOCAL, servers=(), agent="", problem=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Add MCP server to {start} on {host}", start=printable(start), host=printable(host))
                            if host != LOCAL else tr("Add MCP server to {start}", start=printable(start)))
        form = QFormLayout(self)
        self.choice = QComboBox()
        self.choice.setAccessibleName(tr("MCP server"))
        for name, status in servers:
            if re.fullmatch(backend.MCP_NAME, str(name)) and not backend.helper_of(name):
                self.choice.addItem(printable(f"{name} ({status})" if status else name), "server:" + name)
        for helper in backend.LIVE_HELPERS:
            self.choice.addItem(tr("{agent} as a helper", agent=HELPER_TITLES[helper]), "helper:" + helper)
        self.choice.setToolTip(tr("A server of sandbox-ai mcp list there, or another agent as a helper (an MCP "
                                  "server in a sandbox of its own on the same main workspace)"))
        form.addRow(tr("MCP server"), self.choice)
        if problem:
            form.addRow(plain_label(tr("The servers there could not be read: {error}", error=printable(problem)[:300]),
                                    role="notice-warn"))
        how = {"claude": tr("Claude Code finds it in /mcp; if it is not listed there, restart the agent."),
               "codex": tr("Codex reads its MCP servers when it starts: restart the agent to use it."),
               "opencode": tr("OpenCode reads its MCP servers when it starts: restart the agent to use it.")}
        form.addRow(plain_label(tr("It is opened in the running sandbox now, without a restart, and kept for its "
                                   "later starts.") + (" " + how[agent] if agent in how else ""), role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(520, 0)

    def value(self):
        """("server", NAME) or ("helper", AGENT)."""
        kind, _, value = str(self.choice.currentData() or "").partition(":")
        return kind, value


def workspace_label(entry):
    """An added workspace in one line: where it is in the sandbox, from where on its host, a file or a folder, named
    with --name, read-only (`sandbox-ai workspace list --json`'s kind, named, mode)."""
    marks = [tr("file") if entry.get("kind") == "file" else tr("folder")]
    if entry.get("named"):
        marks.append(tr("named"))
    if entry.get("mode") == "ro":
        marks.append(tr("read-only"))
    return f"{printable(entry.get('sandbox'))} ← {printable(entry.get('host'))} ({', '.join(marks)})"


def size_text(size):
    """A size in bytes as sandbox-ai's --memory takes it (8g, 512m, ...)."""
    for suffix, shift in (("t", 40), ("g", 30), ("m", 20), ("k", 10)):
        if size >= 1 << shift and size % (1 << shift) == 0:
            return f"{size >> shift}{suffix}"
    return str(size)


def limit_texts(current):
    """{"memory", "cpus", "pids"} as the limits dialog shows them ("" for none) from limits_shown()."""
    current = current or {}
    cpus = current.get("cpus")
    return {"memory": size_text(current["memory"]) if current.get("memory") else "",
            "cpus": "" if not cpus else str(int(cpus)) if float(cpus).is_integer() else f"{cpus:g}",
            "pids": str(current["pids"]) if current.get("pids") else ""}


class LimitsDialog(QDialog):
    """The limits of a running sandbox for the rest of its run, filled with what they are now (CURRENT, from
    `sandbox-ai limits RUN --show --json`; None: not known, NOTE says why)."""

    def __init__(self, parent, run, host="local", current=None, note=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Limits of {run} on {host}", run=printable(run), host=printable(host)) if host != "local"
                            else tr("Limits of {run}", run=printable(run)))
        form = QFormLayout(self)
        form.addRow(plain_label(tr("For the rest of the run; empty: unchanged, none: no limit.")))
        self.memory, self.cpus, self.pids = QLineEdit(), QLineEdit(), QLineEdit()
        for label, edit in ((tr("Memory (e.g. 8g)"), self.memory), (tr("CPUs (e.g. 2 or 1.5)"), self.cpus),
                            (tr("Processes"), self.pids)):
            form.addRow(label, edit)
        self.initial = limit_texts(current) if current is not None else {"memory": "", "cpus": "", "pids": ""}
        for key, edit in (("memory", self.memory), ("cpus", self.cpus), ("pids", self.pids)):
            edit.setText(self.initial[key])
            # A field emptied keeps its limit: the field says so ("none" removes it, asked first).
            edit.setPlaceholderText(tr("none") if current is not None and not self.initial[key] else
                                    tr("unchanged (none: no limit)"))
            edit.textChanged.connect(lambda _="": self.show_unchanged())
        self.unchanged = plain_label(role="secondary")
        form.addRow(self.unchanged)
        if current is not None and current.get("memory_high"):
            form.addRow(plain_label(tr("Memory pressure from {size} on (memory.high): set at the start.",
                                       size=size_text(current["memory_high"])), role="secondary"))
        if note:
            form.addRow(plain_label(note, role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def edits(self):
        return (("memory", tr("memory"), self.memory), ("cpus", tr("CPUs"), self.cpus), ("pids", tr("processes"),
                                                                                           self.pids))

    def values(self):
        """What to change: a value as it was shown is left out (empty: unchanged)."""
        return tuple("" if edit.text().strip() == self.initial[key] else edit.text().strip()
                     for key, _, edit in self.edits())

    def cleared(self):
        """The limits whose field was emptied: they stay as they are (only "none" removes one)."""
        return [label for key, label, edit in self.edits() if self.initial[key] and not edit.text().strip()]

    def show_unchanged(self):
        cleared = self.cleared()
        self.unchanged.setText(tr("Unchanged: {limits} (an empty field keeps its limit; none removes it).",
                                  limits=", ".join(cleared)) if cleared else "")


KIND_TITLES = {"run": N_("Run"), "session": N_("Session"), "mcp": N_("MCP server"), "unregistered": N_("Not registered")}
# What asks a run's supervisor through its control socket (sandbox-ai net allow/revoke/show, workspace add/remove):
# refused while the sandbox is paused.
ASKS_SUPERVISOR = ("allow", "revoke", "allowed", "add-workspace", "remove-workspace")
STATE_TITLES = {"running": N_("running"), "paused": N_("paused"), "stopped": N_("stopped"), "damaged": N_("damaged"),
                "starting": N_("starting")}
DEMO_PORT = 4000  # the emulated device's port unless --port says otherwise (sandbox-ai demo-device)
STOP_WAIT = 30  # seconds sandbox-ai stop waits for a start to end unless --wait says otherwise
REMOTE_SECONDS = 60  # between two looks at the other hosts' starts (over SSH) while the page is hidden
REMOTE_SHOWN_SECONDS = 10  # the same while the page is shown: the fleet at a glance
HIDDEN_EVERY = 4  # the Sandboxes page hidden: this account's starts are read every 4th tick of sandboxes_interval
REMOTE_BACKOFF = 300  # seconds at most between two looks at a host that does not answer (doubled each time)
START_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}"  # a start's name as sandbox-ai takes it (rename NEW)
ENDED_STATES = ("stopped", "ended")
ACTIVE_STATES = ("running", "paused", "starting")
SESSION_ACTIONS = (("attach", N_("Attach")), ("attach-app", N_("Attach in the app")), ("pause", N_("Pause")),
                   ("resume", N_("Resume")), ("delete", N_("Delete")))
ACTION_TITLES = {**dict(SESSION_ACTIONS), "stop": N_("Stop")}
REMOTE_WAIT = 60  # seconds one host may take to list its starts (the hosts are asked at the same time, each apart)
# What hangs below a start on the Sandboxes page (start_tree.related), by its kind.
PART_TITLES = {"mcp": N_("MCP server"), "helper": N_("Helper"), "pod": N_("Pod"), "companion": N_("Companion"),
               "team": N_("Team"), "serves": N_("Serves")}
GROUPS_KEPT = 400  # groups of the Sandboxes page whose fold is kept (the ones changed last)


def history_time(value):
    """An ISO time of a start's history (UTC) as "YYYY-MM-DD HH:MM", or "-" (as sandbox-ai list --history shows it)."""
    return printable(value[:16].replace("T", " ")) if isinstance(value, str) and len(value) >= 16 else "-"


def history_details(entry):
    """What a history entry says besides its time, event and run, in one line (escaped)."""
    event = entry.get("event")
    if event == "exit":
        return tr("status {status}", status=entry["status"]) if isinstance(entry.get("status"), int) else \
            tr("status unknown")
    if event == "rename":
        return tr("renamed to {name}", name=printable(str(entry["to"]))) if entry.get("to") else \
            tr("renamed from {name}", name=printable(str(entry.get("from"))))
    if event in ("start", "rebirth"):
        return " ".join(printable(str(entry[key])) for key in ("agent", "workspace") if entry.get(key))
    return printable(str(entry.get("note") or ""))


def gc_lines(result):
    """What `sandbox-ai gc --json` removes (or would remove), one escaped line each: kept starts with their HOME,
    leftovers, histories; what it leaves alone."""
    result = result if isinstance(result, dict) else {}

    def listed(key):
        value = result.get(key)
        return value if isinstance(value, list) else []
    lines = [tr("the kept start {name} with its HOME (last used {time}, {workspace})", name=printable(str(e.get("name"))),
                time=history_time(e.get("last_used")), workspace=printable(str(e.get("workspace") or "-")))
             for e in listed("removed") if isinstance(e, dict)]
    lines += [tr("the leftovers of {name} (ended; its HOME was not kept)", name=printable(str(name)))
              for name in listed("leftovers")]
    lines += [tr("the history of {name}", name=printable(str(name))) for name in listed("histories")]
    lines += [tr("left alone: {name}: {reason}", name=printable(str(e.get("name"))), reason=printable(str(e.get("reason"))))
              for e in listed("skipped") if isinstance(e, dict)]
    lines += [tr("left alone: {path} is damaged; remove it by hand", path=printable(str(path)))
              for path in listed("damaged")]
    return lines


def same_folder(a, b):
    """True if the folders A and B are the same (as resolved paths); False when either is empty."""
    if not a or not b:
        return False
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))


class ResumeDialog(QDialog):
    """Resume with…: a kept start started again with workspaces, mounts and MCP servers added (kept for its later
    starts), a running one restarted (`sandbox-ai resume --workspace … --mount … --mcp … [--restart] NAME`). The paths
    are those of the start's HOST: typed in there, chosen from this computer's only for this machine."""

    def __init__(self, parent, name, host=LOCAL, running=False, servers=()):
        super().__init__(parent)
        self.setWindowTitle(tr("Resume {name} with…", name=printable(name)) if host == LOCAL else
                            tr("Resume {name} on {host} with…", name=printable(name), host=printable(host)))
        form = QFormLayout(self)
        if host != LOCAL:
            form.addRow(plain_label(tr("Paths on {host}: type them in (this computer's folders are not {host}'s).",
                                       host=printable(host)), role="secondary"))
        self.workspaces = ListField(path=True)
        self.workspaces.set_local(host == LOCAL)
        self.workspaces.setToolTip(tr("Folders added at /workspaces/<name>, one per line (--workspace)"))
        form.addRow(tr("Workspaces"), self.workspaces)
        self.mounts = QPlainTextEdit()
        self.mounts.setFixedHeight(52)
        self.mounts.setPlaceholderText(tr("SOURCE:DESTINATION[:ro|rw], one per line (read-only unless :rw)"))
        form.addRow(tr("Mounts"), self.mounts)
        self.servers = QLineEdit()
        self.servers.setPlaceholderText(tr("names, separated by spaces (sandbox-ai mcp list)"))
        form.addRow(tr("MCP servers"), self.servers)
        if servers:
            form.addRow("", plain_label(tr("Defined: {list}", list=printable(", ".join(servers))), role="secondary"))
        self.restart = QCheckBox(tr("Restart it now if it runs (the agent continues its conversation)"))
        self.restart.setChecked(running)
        form.addRow(self.restart)
        form.addRow(plain_label(tr("What is added here is kept for its later starts."), role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        """(workspaces, [(source, destination, mode)], servers, restart). ValueError: a mount that is not
        SOURCE:DESTINATION[:ro|rw]."""
        mounts = []
        for line in self.mounts.toPlainText().splitlines():
            parts = line.strip().split(":")
            if not line.strip():
                continue
            if len(parts) == 2:
                parts.append("ro")
            if len(parts) != 3 or parts[2] not in ("ro", "rw"):
                raise ValueError(tr("A mount is SOURCE:DESTINATION or SOURCE:DESTINATION:ro|rw: {line}",
                                    line=printable(line.strip())))
            mounts.append(tuple(parts))
        return self.workspaces.value(), mounts, self.servers.text().split(), self.restart.isChecked()


class SandboxesPage(QWidget):
    """Every sandbox-ai instance of this account in one table: one-off runs (started here, in a terminal, by a
    script or another copy of the app), sessions (also paused and stopped ones), MCP server sandboxes, and
    launchers that have not registered yet. The actions follow the kind of the selected row; what does not
    apply is greyed out with the reason in its tooltip. Read again every few seconds in the background."""
    open_log = Signal(str)
    open_remote_log = Signal(str, object)  # (host, sandbox-ai activity's arguments) of a host's start
    counted = Signal(int, int)  # instances that run, of them in the current project
    updated = Signal(object)  # {key: Instance} after each read (the window's tabs follow it)
    fleet_changed = Signal(object)  # {host: {"running", "active", "seen", "problem"}} after each look at a host
    started_on = Signal(str)  # the page started something on that host (Resume): its questions are asked for at once

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.project, self.instances, self.problems, self.stoppable = None, {}, [], None
        self.reading, self.read_again, self.added, self.devices_token = None, False, [], 0
        self.fresh = False  # the next read asks Podman afresh (refresh_soon(again=True): after an action)
        self.loaded = False  # the first read has come back (until then the summary says it reads)
        self.added_workspaces = []
        # (host, run) of the starts with an open question (the Approvals page's poller): marked "asking".
        self.asking = set()
        # The starts that ended, when "Ended" is on: {host: [Instance]} from `sandbox-ai list --all --json`.
        self.ended, self.ended_token = {}, 0
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.summary = plain_label(role="secondary")
        self.only_project = QCheckBox(tr("Only the current project"))
        self.only_project.toggled.connect(lambda _=False: self.show_instances())
        # Which machine's starts are listed: all, this machine's, or one host's (shown once there are hosts).
        self.host_filter = QComboBox()
        self.host_filter.setToolTip(tr("Whose sandboxes are listed: this machine's, a host's, or all of them"))
        self.host_filter.setAccessibleName(tr("Host"))
        self.host_filter.addItem(tr("All hosts"), None)
        self.host_filter.addItem(tr("This machine"), LOCAL)
        self.host_filter.currentIndexChanged.connect(lambda _=0: self.show_instances())
        self.host_filter.hide()
        self.show_ended = QCheckBox(tr("Ended"))
        self.show_ended.setToolTip(tr("Also the starts that ended: stopped MCP servers and what else sandbox-ai list "
                                      "--all keeps, on this machine and every host"))
        self.show_ended.toggled.connect(lambda _=False: self.ended_toggled())
        refresh = QPushButton(tr("Refresh"))
        refresh.setAccessibleName(tr("Refresh the sandboxes"))
        refresh.setToolTip(tr("Read again now (it is read every few seconds anyway)"))
        refresh.clicked.connect(lambda _=False: (self.refresh_soon(again=True), self.refresh_remote()))
        top.addWidget(self.summary, 1)
        top.addWidget(self.host_filter)
        top.addWidget(self.only_project)
        top.addWidget(self.show_ended)
        top.addWidget(refresh)
        layout.addLayout(top)
        # A tree: each host, under it its starts, under each start what belongs to it (start_tree.py); the buttons
        # act on the start rows only. Which groups are open is kept per host and start (open_groups).
        self.table = StartTree([tr("Kind"), tr("Name"), tr("Project"), tr("Agent"), tr("Backend"), tr("Started"),
                                tr("State"), tr("Proxy"), tr("Host"), tr("Memory")], stretch=2,
                               compact=(0, 3, 4, 5, 6, 7, 9))
        self.table.setAccessibleName(tr("Sandboxes and sessions"))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemExpanded.connect(lambda item: self.group_toggled(item, True))
        self.table.itemCollapsed.connect(lambda item: self.group_toggled(item, False))
        self.table.itemDoubleClicked.connect(lambda item, _column: self.open_part(item))
        self.table.folded.connect(self.all_folded)
        # {row_id: open} of the groups the human opened or closed; new ones: a host open, a start closed.
        self.open_groups, self.hosts_open, self.starts_open = {}, True, False
        layout.addWidget(self.table, 1)
        below = QHBoxLayout()
        self.detail = plain_label(role="secondary")
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.stop_button = QPushButton(tr("Stop…"))
        self.stop_button.setAccessibleName(tr("Stop"))
        self.stop_button.setProperty("role", "danger")
        self.stop_button.clicked.connect(lambda _=False: self.stop())
        # sandbox-ai stop --wait: how long it waits for a session or kept start to end (30 s unless said otherwise).
        self.stop_wait = QSpinBox()
        self.stop_wait.setRange(1, 600)
        self.stop_wait.setValue(STOP_WAIT)
        self.stop_wait.setSuffix(" s")
        self.stop_wait.setAccessibleName(tr("Wait for it to end"))
        self.stop_wait.setToolTip(tr("How long Stop waits for a session or kept start to end (sandbox-ai stop --wait)"))
        # A start's name and history, and what is left over (sandbox-ai rename, list --history, gc): on its host.
        self.manage_buttons = {}
        for key, text, tip, slot in (
                ("rename", tr("Rename…"), tr("Another name for the selected start; its run id stays (sandbox-ai "
                                             "rename)"), self.rename),
                ("history", tr("History…"), tr("What happened to the selected start: starts, ends, pauses, renames "
                                               "(sandbox-ai list --history)"), self.history),
                ("clean", tr("Clean up…"), tr("Remove leftovers and the kept starts unused for longer than its "
                                              "keep_days, on this machine or the host chosen above (sandbox-ai gc; "
                                              "shown first)"), self.clean_up)):
            button = QPushButton(text)
            button.setToolTip(safe_tooltip(tip))
            button.setAccessibleName(text.rstrip("…"))
            button.clicked.connect(lambda _=False, s=slot: s())
            self.manage_buttons[key] = button
        # By name too: the (?) and the area tour point at them.
        self.rename_button, self.history_button, self.clean_button = (self.manage_buttons[key] for key in (
            "rename", "history", "clean"))
        below.addWidget(self.detail, 1)
        for button in self.manage_buttons.values():
            below.addWidget(button)
        below.addWidget(self.stop_wait)
        below.addWidget(self.stop_button)
        layout.addLayout(below)
        # The LLM server the selected start works with (--llm NAME): what it has loaded and uses now (llm status on
        # the start's host), read in the background while it is shown.
        from .llm_page import ServerStatusLine
        self.llm_status = ServerStatusLine(sai)
        layout.addWidget(self.llm_status)
        # The sessions' own actions (sandbox-ai session ...).
        session_box = QGroupBox(tr("Session"))
        session_row = QVBoxLayout(session_box)
        self.session_buttons = {}
        for action, text in SESSION_ACTIONS:
            button = QPushButton(tr(text))
            button.setAccessibleName(tr(text))
            button.clicked.connect(lambda _=False, a=action: self.session(a))
            self.session_buttons[action] = button
        self.resume_with_button = QPushButton(tr("Resume with…"))
        self.resume_with_button.setAccessibleName(tr("Resume with"))
        self.resume_with_button.setToolTip(tr("Start the selected kept start again with workspaces, mounts or MCP "
                                              "servers added (kept for its later starts), or restart it now "
                                              "(sandbox-ai resume)"))
        self.resume_with_button.clicked.connect(lambda _=False: self.resume_with())
        session_row.addWidget(flow(*self.session_buttons.values(), self.resume_with_button))
        # What a running sandbox may reach (sandbox-ai net), its devices and limits (until it ends).
        run_box = QGroupBox(tr("While it runs"))
        run_layout = QVBoxLayout(run_box)
        self.run_buttons, self.run_named = [], {}
        for key, text, tip, slot in (
                ("allow", tr("Allow…"), tr("Allow more destinations until the sandbox ends (sandbox-ai net allow)"),
                 lambda: self.network("allow")),
                ("revoke", tr("Revoke…"), tr("Take back what was allowed during the run (sandbox-ai net revoke)"),
                 lambda: self.network("revoke")),
                ("allowed", tr("Allowed during the run"), tr("What was allowed since the start (sandbox-ai net show)"),
                 self.show_allowed),
                ("log", tr("Connection log"), tr("The connection proxy's log of its project"), self.log),
                ("add-device", tr("Add device…"), tr("Hand a host device to the selected sandbox until it ends"),
                 self.add_device),
                ("remove-device", tr("Remove device"), tr("Take back a device added during the run"), self.remove_device),
                ("add-workspace", tr("Add workspace…"), tr("Mount a folder or a file of its host into the selected "
                                                          "sandbox until it ends (sandbox-ai workspace add)"),
                 self.add_workspace),
                ("remove-workspace", tr("Remove workspace…"), tr("Take back a workspace added during the run "
                                                                "(sandbox-ai workspace remove)"), self.remove_workspace),
                ("add-mcp", tr("Add MCP server…"), tr("Give the selected kept start an MCP server or a helper agent "
                                                      "now, without a restart, and at its later starts (sandbox-ai "
                                                      "mcp attach --live)"), self.add_mcp),
                ("remove-mcp", tr("Remove MCP server…"), tr("Take an MCP server from the selected start now and from "
                                                            "its later starts (sandbox-ai mcp detach --live)"),
                 self.remove_mcp),
                ("limits", tr("Limits…"), tr("Change memory, CPUs and processes of the selected sandbox"), self.limits)):
            button = QPushButton(text)
            button.setToolTip(safe_tooltip(tip))
            button.setAccessibleName(text.rstrip("…"))
            button.clicked.connect(lambda _=False, s=slot: s())
            self.run_buttons.append(button)
            self.run_named[key] = button
        run_layout.addWidget(flow(*self.run_buttons))
        self.devices_label = plain_label(role="secondary")
        run_layout.addWidget(self.devices_label)
        self.workspaces_label = plain_label(role="secondary")
        run_layout.addWidget(self.workspaces_label)
        boxes = QHBoxLayout()
        boxes.addWidget(session_box, 2)
        boxes.addWidget(run_box, 5)
        layout.addLayout(boxes)
        # This account's starts every sandboxes_interval while the page is shown; hidden, every HIDDEN_EVERY-th time
        # (the window's tabs and counts follow them); with the window in the tray or minimised, not at all (read again
        # when it is shown).
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(backend.setting("sandboxes_interval"))
        self.ticks = 0
        # The other hosts' starts (sandbox-ai hosts), over SSH: read now and then in the background, never on the
        # GUI thread, each host apart (a slow one holds up no other); a host that does not answer is said in the
        # summary and keeps its last rows, greyed out with when it was seen last, their buttons off.
        # One read per host at a time (as the web interface's FanOut): every look asks each host that has no read
        # open; a host that hangs (it takes its whole timeout) delays no other and is not asked again on top.
        # remote_flight: {host: token of its read running}; remote_again: hosts to read once more when their read is
        # done (after an action there); ended_flight: the same for "Ended" (sandbox-ai list --all there, apart).
        self.remote, self.remote_problems, self.remote_flight, self.remote_again = {}, {}, {}, set()
        self.remote_seen, self.hosts_problem, self.remote_names = {}, None, []
        self.hosts_reading, self.hosts_again, self.ended_flight, self.flights = False, False, {}, 0
        self.hosts_polled = False
        self.remote_timer = QTimer(self)
        self.remote_timer.timeout.connect(self.remote_tick)
        self.remote_timer.start(REMOTE_SECONDS * 1000)
        self.remote_failures, self.remote_next = {}, {}  # a host that does not answer is asked less often (backoff)
        # The first look too runs in the background: what runs here may ask Podman (a run whose launcher is gone, a
        # stopped Podman session), which can take its whole timeout - the window shows at once, "Reading…" until then.
        self.show_instances()
        self.refresh_soon()
        QTimer.singleShot(0, self.refresh_remote)

    # -- reading --

    def window_shown(self):
        """False while the window is in the tray or minimised (its polls pause; the page alone: always True)."""
        window = self.window()
        return window is self or (window.isVisible() and not window.isMinimized())

    def remote_tick(self):
        """The hosts' starts, every REMOTE_SECONDS (shown: REMOTE_SHOWN_SECONDS). With the window in the tray or
        minimised only those of the hosts where nothing ran at the last look: they are not asked for questions
        (ApprovalsPage.set_fleet) until a start shows up here."""
        if self.window_shown():
            self.refresh_remote(polled=True)
            return
        now = time.monotonic()
        for name in self.remote_names:
            if self.fleet_entry(name)["active"] == 0 and name not in self.remote_flight and \
                    now >= self.remote_next.get(name, 0):
                self.read_host(name)

    def tick(self):
        self.ticks += 1
        if self.window_shown() and (self.isVisible() or self.ticks % HIDDEN_EVERY == 0):
            self.refresh_soon()

    def refresh_soon(self, again=False):
        """Read in a thread, one read at a time (the timer's way, the first look too): sandbox-ai's registry read also
        prunes what ended (it removes files), may ask Podman, and a disk that hangs must not hang the window. AGAIN
        (after an action, the Refresh button): when a read runs already, one more once it is done - what the action
        changed is shown."""
        self.fresh = self.fresh or again  # after an action: Podman asked afresh, not the listing kept a while
        if self.reading is not None:
            self.read_again = self.read_again or again
            return
        self.read_again = False
        self.reading = background(self.read_instances, timeout=60, done=self.read_done)

    def read_instances(self):
        """In the reading thread: (instances, problems), and once whether `sandbox-ai net stop` exists."""
        if self.fresh:
            self.fresh = False
            forget = getattr(self.sai, "forget_pods", None)
            if callable(forget):
                forget()
        if self.stoppable is None:
            try:
                self.stoppable = "stop" in self.sai.net_commands()
            except Exception:  # noqa: BLE001
                self.stoppable = False
        return self.sai.instances()

    def read_done(self, result, error):
        self.reading = None
        self.apply(result, error)
        if self.read_again:
            self.refresh_soon()

    def showEvent(self, event):  # noqa: N802
        """Shown: the hosts are looked at every REMOTE_SHOWN_SECONDS (hidden: every REMOTE_SECONDS)."""
        super().showEvent(event)
        self.refresh_soon()  # hidden, it was read less often
        if self.remote_timer.interval() != REMOTE_SHOWN_SECONDS * 1000:
            self.remote_timer.start(REMOTE_SHOWN_SECONDS * 1000)
            self.refresh_remote()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self.remote_timer.start(REMOTE_SECONDS * 1000)

    @property
    def remote_reading(self):
        """The hosts whose read runs ("" for the list of hosts), or None when no read runs."""
        reading = set(self.remote_flight) | ({""} if self.hosts_reading else set())
        return reading or None

    def refresh_remote(self, hosts=None, polled=False):
        """Read the other hosts' starts in the background: the list of hosts, then every host that has no read open,
        each in its own thread with its own timeout; each host's rows are shown as soon as it answered. HOSTS (after
        an action there): those hosts now, or once more when their read is done - what the action changed is shown,
        not only what was read before it. A list of hosts that cannot be read: the hosts known are read again."""
        if hosts is not None:
            for name in hosts:
                if name == LOCAL:
                    continue
                if name in self.remote_flight:
                    self.remote_again.add(name)
                else:
                    self.read_host(name)
            return
        if self.hosts_reading:
            self.hosts_again = True
            return
        self.hosts_reading, self.hosts_polled = True, polled  # POLLED (the timer): hosts that did not answer wait
        # Reads, not work: no owner, so they neither grey out the buttons (busy()) nor hold up closing the window.
        background(self.sai.remote_hosts, timeout=30,
                   done=lambda hosts_, error: alive(self) and self.hosts_listed(hosts_, error))

    def hosts_listed(self, hosts, error):
        self.hosts_reading = False
        if error is None:
            hosts, self.hosts_problem = list(hosts or []), None
            for gone in set(self.remote) | set(self.remote_seen) | set(self.remote_problems) | set(self.ended) | \
                    set(self.remote_flight):
                if gone not in hosts and gone != LOCAL:
                    self.drop_host(gone)
            self.remote_names = list(hosts)
            self.fill_host_filter()
        else:
            self.hosts_problem = printable(error)[:200]
            hosts = list(self.remote_names)
        now = time.monotonic()
        for name in hosts:
            # One that still answers the last look is not asked on top; one that did not answer waits (remote_next).
            if name not in self.remote_flight and (not self.hosts_polled or now >= self.remote_next.get(name, 0)):
                self.read_host(name)
        self.merge_remote()
        if self.hosts_again:
            self.hosts_again = False
            self.refresh_remote()

    def read_host(self, name):
        """[Instance] of host NAME in a thread; with "Ended" its starts that ended too, in a read of their own (each
        with its own time: both are sandbox-ai list there, one may take as long as the other)."""
        self.flights += 1
        token = self.remote_flight[name] = self.flights
        background(self.sai.host_instances, name, REMOTE_WAIT, timeout=REMOTE_WAIT + 15,
                   done=lambda result, error, n=name, t=token: alive(self) and self.host_read(n, result, error, t))
        if self.show_ended.isChecked():
            self.read_ended(name)

    def read_ended(self, name):
        if name in self.ended_flight:
            return
        self.flights += 1
        token = self.ended_flight[name] = self.flights
        background(self.sai.ended_starts, name, timeout=REMOTE_WAIT + 15,
                   done=lambda found, error, n=name, t=token: alive(self) and self.host_ended(n, found, error, t))

    def host_read(self, name, result, error, token=None):
        if self.remote_flight.get(name) != token:
            return  # removed (Hosts › Remove) or dropped from the list meanwhile: its late answer counts no more
        del self.remote_flight[name]
        if name in self.remote_names:
            if error is None:
                self.remote[name] = list(result)
                self.remote_seen[name] = time.time()
                self.remote_problems.pop(name, None)
                self.remote_failures.pop(name, None)
                self.remote_next.pop(name, None)
            else:
                self.remote_problems[name] = printable(error)[:300]
                # Not asked again by the timer before REMOTE_SHOWN_SECONDS * 2^failures (at most REMOTE_BACKOFF): a
                # host that does not answer costs a Python process and an ssh that waits for its timeout each time.
                failures = self.remote_failures[name] = self.remote_failures.get(name, 0) + 1
                self.remote_next[name] = time.monotonic() + min(REMOTE_BACKOFF, REMOTE_SHOWN_SECONDS * 2 ** failures)
        if name in self.remote_again:  # asked again meanwhile (an action there): a fresh read
            self.remote_again.discard(name)
            self.read_host(name)
        self.merge_remote()

    def host_ended(self, name, found, error, token):
        if self.ended_flight.get(name) != token:
            return
        del self.ended_flight[name]
        if name in self.remote_names and error is None and self.show_ended.isChecked():
            self.ended[name] = list(found or [])
            self.show_instances()

    def merge_remote(self):
        self.instances = {**{k: i for k, i in self.instances.items() if i.host == LOCAL},
                          **{i.key: i for found in self.remote.values() for i in found}}
        self.show_instances()
        self.fleet_changed.emit(self.fleet())

    def fleet(self):
        """{host: {"running": starts running or paused, "seen": time.time() of the last answer or None, "problem":
        why it did not answer or None}} of this machine and every host (what the Hosts page lists)."""
        found = {LOCAL: {"running": sum(1 for i in self.instances.values() if i.host == LOCAL
                                        and i.state in ("running", "paused")),
                         "active": sum(1 for i in self.instances.values() if i.host == LOCAL
                                       and i.state in ACTIVE_STATES), "seen": time.time(), "problem": None}}
        for name in self.remote_names:
            found[name] = self.fleet_entry(name)
        return found

    def fleet_entry(self, name):
        """fleet() of host NAME; "active": its starts running, paused or starting at its last look (None: not known -
        not read yet, or its last look got no answer)."""
        seen = name in self.remote_seen
        return {"running": sum(1 for i in self.remote.get(name, []) if i.state in ("running", "paused"))
                if seen else None,
                "active": sum(1 for i in self.remote.get(name, []) if i.state in ACTIVE_STATES)
                if seen and name not in self.remote_problems else None,
                "seen": self.remote_seen.get(name), "problem": self.remote_problems.get(name)}

    def drop_host(self, name):
        """Everything of host NAME goes, its reads running too (their answers, when they come, are ignored)."""
        for store in (self.remote, self.remote_seen, self.remote_problems, self.ended, self.remote_flight,
                      self.ended_flight, self.remote_failures, self.remote_next):
            store.pop(name, None)
        self.remote_again.discard(name)

    def forget_host(self, name):
        """Host NAME was removed from the list (the Hosts page): its rows go at once, not at the next look."""
        self.drop_host(name)
        self.remote_names = [n for n in self.remote_names if n != name]
        self.fill_host_filter()
        self.merge_remote()

    def show_host(self, host):
        """List only HOST's starts (Hosts › Show starts)."""
        index = self.host_filter.findData(host)
        if index < 0 and host not in (None, LOCAL):
            self.remote_names.append(host)
            self.fill_host_filter()
            index = self.host_filter.findData(host)
        self.host_filter.setCurrentIndex(max(0, index))
        self.only_project.setChecked(False)
        self.show_instances()

    def set_asking(self, asking):
        """ASKING: {(host, run)} of the starts with an open question; they are marked in the State column."""
        asking = set(asking)
        if asking != self.asking:
            self.asking = asking
            self.show_instances()

    def is_asking(self, instance):
        if instance.host == LOCAL:
            return bool(instance.run) and (LOCAL, instance.run) in self.asking
        record = instance.record or {}
        return any((instance.host, run) in self.asking for run in (record.get("run"), record.get("id"), instance.name)
                   if isinstance(run, str) and run)

    def stale(self, instance):
        """Seconds since INSTANCE's host answered last when it does not answer now, else None."""
        if instance.host == LOCAL or instance.host not in self.remote_problems:
            return None
        seen = self.remote_seen.get(instance.host)
        return int(time.time() - seen) if seen else 0

    # -- the starts that ended --

    def ended_toggled(self):
        self.load_ended()
        if self.show_ended.isChecked():
            for name in self.remote_names:  # each host's own read, whether a read of its starts runs or not
                self.read_ended(name)
        self.show_instances()

    def load_ended(self):
        """This machine's starts that ended (`sandbox-ai list --all --json`), in the background."""
        self.ended_token += 1
        token = self.ended_token
        if not self.show_ended.isChecked():
            self.ended = {}
            return
        background(self.sai.ended_starts, LOCAL, timeout=60,
                   done=lambda found, error: alive(self) and self.ended_loaded(token, found, error))

    def ended_loaded(self, token, found, error):
        if token != self.ended_token:
            return
        self.ended[LOCAL] = list(found or []) if error is None else []
        self.show_instances()

    def fill_host_filter(self):
        """All hosts, this machine, and each host of `sandbox-ai hosts`; the choice stays while its host does. Filled
        again only when the hosts changed (an open list must not close under the mouse at every look)."""
        if self.host_filter.count() == len(self.remote_names) + 2 and \
                [self.host_filter.itemData(i) for i in range(2, self.host_filter.count())] == list(self.remote_names):
            self.host_filter.setVisible(bool(self.remote_names))
            return
        chosen = self.host_filter.currentData()
        self.host_filter.blockSignals(True)
        self.host_filter.clear()
        self.host_filter.addItem(tr("All hosts"), None)
        self.host_filter.addItem(tr("This machine"), LOCAL)
        for name in self.remote_names:
            self.host_filter.addItem(printable(name), name)
        index = self.host_filter.findData(chosen)
        self.host_filter.setCurrentIndex(index if index >= 0 else 0)
        self.host_filter.blockSignals(False)
        self.host_filter.setVisible(bool(self.remote_names))

    def apply(self, result, error):
        self.loaded = True
        if self.stoppable is None:
            try:
                self.stoppable = "stop" in self.sai.net_commands()
            except Exception:  # noqa: BLE001
                self.stoppable = False
        if error is not None:
            found, self.problems = [], [str(error)]
        else:
            found, self.problems = result
        local = {instance.key: instance for instance in found}
        self.instances = {**local, **{i.key: i for found_ in getattr(self, "remote", {}).values() for i in found_}}
        self.show_instances()
        self.updated.emit(local)  # the other pages act on this account's own
        self.fleet_changed.emit(self.fleet())

    def set_project(self, path):
        self.project = path
        self.show_instances()

    def attach_state(self, state):
        """The row selected last is selected again once it is there (kept for the next start)."""
        self.state = state
        self.wanted = state.get("sandboxes-selected", "")
        self.load_groups(state)
        self.show_instances()

    def mine(self, instance):
        return same_folder(instance.project, self.project)

    def listed(self):
        """{key: Instance} the table may show: every instance, and with "Ended" the starts that ended (those that a
        host lists already as kept are not listed twice)."""
        found = dict(self.instances)
        if self.show_ended.isChecked():
            names = {(i.host, i.name) for i in self.instances.values()} | \
                {(i.host, getattr(i, "ident", i.name)) for i in self.instances.values()}
            for host, ended in self.ended.items():
                if host == LOCAL or host in self.remote_names:
                    found.update({i.key: i for i in ended if (i.host, i.name) not in names
                                  and (i.host, getattr(i, "ident", i.name)) not in names})
        return found

    def start_row(self, i):
        """{cells, color, tip, sort} of the row of the start I."""
        memory = backend.memory_share(i) if i.state in ("running", "paused") else None
        state = tr(STATE_TITLES.get(i.state, i.state)) if i.state else ""
        stale, asking = self.stale(i), self.is_asking(i)
        if stale is not None:  # its host did not answer: what was seen then, not what is now
            state = tr("{state}, last seen {seconds} s ago", state=state or "?", seconds=stale) \
                if i.host in self.remote_seen else tr("{state}, not seen", state=state or "?")
        elif asking:
            state = tr("{state}, asking", state=state or "?")
        from . import ask_choice as AC
        allows_all = AC.allows_all(i.record) and i.state in ("running", "paused")  # --ask allow: nobody is asked
        if allows_all:
            state = f"{state}, {AC.WARN} {tr(AC.BADGE)}"
        cells = [tr(KIND_TITLES.get(i.kind, i.kind)), printable(i.name), printable(i.project), printable(i.agent),
                 printable(i.backend), local_time(i.started), state,
                 "" if i.proxy is None else tr("on") if i.proxy else tr("off"),
                 tr("this machine") if getattr(i, "host", "local") == "local" else printable(i.host),
                 "" if memory is None else f"{memory[0]:.0f} %"]
        color = {"running": "ok", "paused": "warn", "damaged": "bad", "starting": "warn"}.get(i.state)
        if memory is not None and memory[0] >= 95:
            color = "bad"  # the watch is about to end a process in it
        elif memory is not None and memory[0] >= 85 and color != "bad":
            color = "warn"
        if (asking or allows_all) and color != "bad":
            color = "warn"
        if stale is not None or i.state in ENDED_STATES and i.key.startswith("ended:"):
            color = "muted"
        tip = [tr("Current project")] if self.mine(i) else []
        if allows_all:
            tip.append(tr(AC.ALLOW_HELP))
        if asking:
            tip.append(tr("It waits for an answer on the Approvals page."))
        if stale is not None:
            tip.append(tr("{host} does not answer: {problem}", host=printable(i.host),
                          problem=printable(self.remote_problems.get(i.host, ""))[:200]))
        # By the share, not as text ("9 %" > "10 %").
        return {"cells": cells, "color": color, "tip": "\n".join(tip) or None,
                "sort": [None] * 9 + [memory[0] if memory else -1.0]}

    def host_row(self, host, starts):
        """{cells, color, tip, sort} of the row of HOST, above its STARTS: how many, how many run, whether it answers."""
        # Running and paused counted apart: a paused start does not run ("running: 3" with 2 running, 1 paused before).
        running = sum(1 for i in starts if i.state in ("running", "starting"))
        paused = sum(1 for i in starts if i.state == "paused")
        where = tr("this machine") if host == LOCAL else printable(host)
        problem = getattr(self, "remote_problems", {}).get(host)
        seen = getattr(self, "remote_seen", {}).get(host)
        color, tip = None, None
        if host == LOCAL:
            state = ""
        elif problem is not None:
            state = tr("no answer, last seen {seconds} s ago", seconds=int(time.time() - seen)) if seen else \
                tr("no answer, not seen yet")
            color, tip = "muted", tr("{host} does not answer: {problem}", host=where, problem=printable(problem)[:200])
        elif seen:
            state = tr("answers, seen {seconds} s ago", seconds=int(time.time() - seen))
        else:
            state = tr("not read yet")
        if host != LOCAL and tip is None:
            tip = tr("Its starts, read over SSH (sandbox-ai hosts)")
        cells = [tr("This machine") if host == LOCAL else printable(host),
                 tr("Starts: {count}, running: {running}, paused: {paused}", count=len(starts), running=running,
                    paused=paused) if paused else
                 tr("Starts: {count}, running: {running}", count=len(starts), running=running),
                 "", "", "", "", state, "", where, ""]
        order = ("0" if host == LOCAL else "1") + str(host).lower()  # this machine first, by the header too
        return {"cells": cells, "color": color, "tip": tip, "sort": [order] * len(cells)}

    def part_row(self, host, start, part):
        """{cells, color, tip} of the row of PART (start_tree.related), below the start (or MCP server) START."""
        target = self.rows.get(part["key"]) if part["key"] else None
        kind = part["kind"]
        state = tr(STATE_TITLES.get(target.state, target.state)) if target is not None and target.state else ""
        color = None if target is None or target.state in ACTIVE_STATES else "muted"
        backend_name = ""
        if kind in ("mcp", "helper"):
            if target is None:
                state, color = tr("not running"), "muted"
            tip = tr("It reaches this MCP server while it runs.") if part["now"] else \
                tr("It reaches this MCP server at its next start (its --mcp).")
            if kind == "helper":
                tip += "\n" + tr("A helper: another agent in a sandbox of its own, which it asks (--helper).")
        elif kind == "serves":
            tip = tr("This MCP server serves {name}: it reaches it while it runs or at its next start.",
                     name=printable(part["name"]))
        elif kind == "pod":
            state, backend_name = tr(STATE_TITLES.get(start.state, start.state)) if start.state else "", "podman"
            tip = tr("Its Podman pod: Pause and Resume freeze and thaw it.")
        elif kind == "companion":
            tip = tr("Another agent in the same sandbox (--with).")
        else:
            tip = tr("The team it works in (sandbox-ai team).")
        if target is not None:
            tip += "\n" + tr("Double-click: its own row, whose buttons act on it.")
        cells = [tr(PART_TITLES.get(kind, kind)), printable(part["name"]), "", "", backend_name, "", state, "",
                 tr("this machine") if host == LOCAL else printable(host), ""]
        return {"cells": cells, "color": color, "tip": tip}

    def show_instances(self):
        """The tree: each host (the one chosen above, or all), under it its starts, under each what belongs to it.
        The row selected stays selected (by host and id), the groups stay open or closed as they were left."""
        chosen = self.host_filter.currentData()
        self.rows = self.listed()
        shown = [i for i in self.rows.values() if (not self.only_project.isChecked() or self.mine(i))
                 and (chosen is None or getattr(i, "host", "local") == chosen)]
        order = {"run": 0, "session": 1, "mcp": 2, "unregistered": 3}
        shown.sort(key=lambda i: (getattr(i, "host", "local") != "local", not self.mine(i), order.get(i.kind, 9),
                                  str(getattr(i, "host", "")), str(i.name)))
        names = list(getattr(self, "remote_names", []))
        hosts = [LOCAL, *names, *sorted({i.host for i in shown} - {LOCAL} - set(names))] if chosen is None else [chosen]
        tree = self.table
        plan = []  # (parent row id, row id, kind, key, cells...): StartTree.fill
        for host in hosts:
            starts = [i for i in shown if i.host == host]
            host_id = row_id(host)
            plan.append((None, host_id, "host", None, self.host_row(host, starts)))
            parts = related([i for i in self.rows.values() if i.host == host])
            for i in starts:
                start_id = row_id(host, i.key)
                plan.append((host_id, start_id, "start", i.key, {**self.start_row(i), "bold": self.mine(i)}))
                for part in parts.get(i.key, []):
                    plan.append((start_id, row_id(host, f"{i.key}\n{part['kind']}\n{part['name']}"), "part",
                                 part["key"], self.part_row(host, i, part)))
        expanded = (lambda ident, item: self.open_groups.get(
            ident, self.hosts_open if item.data(0, KIND_ROLE) == "host" else self.starts_open))
        same_rows, changed = tree.same_rows(plan), not tree.shows(plan)
        if same_rows and changed:  # the same rows, some cells changed (a state, "seen 12 s ago"): set in place
            tree.blockSignals(True)
            try:
                tree.fill(plan, expanded)
            finally:
                tree.blockSignals(False)
        elif changed:  # other rows: the tree is built again, the selection, folds and scroll kept
            selected_item = tree.selected_item()
            previous = selected_item.data(0, ID_ROLE) if selected_item is not None else None
            scroll = tree.verticalScrollBar().value()
            tree.blockSignals(True)  # rebuilt: no selection or fold changes are said (selection_changed() below)
            try:
                tree.setSortingEnabled(False)
                tree.fill(plan, expanded)
                tree.setSortingEnabled(True)  # the order the human chose by a header, if any
                again = tree.find_item(previous) if previous else None
                if again is not None:
                    tree.select_item(again, open_groups=False)
            finally:
                tree.blockSignals(False)
            tree.verticalScrollBar().setValue(scroll)
        if getattr(self, "wanted", "") and tree.selected_item() is None and find_row(tree, self.wanted) >= 0:
            tree.selectRow(find_row(tree, self.wanted))
        if changed:
            fit_columns(tree)  # bold text is wider
        active = [i for i in self.instances.values() if i.state in ("running", "paused", "starting")]
        running = [i for i in active if getattr(i, "host", "local") == "local"]
        here = [i for i in running if self.mine(i)]
        text = tr("{count} running on this account", count=len(running))
        elsewhere = len(active) - len(running)
        if getattr(self, "remote", None):
            text += " · " + tr("{count} on other hosts", count=elsewhere)
        for name, problem in sorted(getattr(self, "remote_problems", {}).items()):
            text += " · " + tr("{host}: no answer", host=printable(name)) + f" ({printable(problem)[:120]})"
        if getattr(self, "hosts_problem", None):
            text += " · " + tr("the list of hosts could not be read: {error}", error=self.hosts_problem)
        if self.project:
            text += " · " + tr("{count} in the current project", count=len(here))
        if self.problems:
            text += " · " + tr("not all could be read: {problems}", problems=printable("; ".join(self.problems))[:300])
        if not self.loaded:
            text = tr("Reading what runs on this account…")
        self.summary.setText(text)
        self.counted.emit(len(running), len(here))
        self.selection_changed()

    def current(self):
        """The start of the row selected, or None (none selected, or a host's row or a part of a start)."""
        item = self.table.selected_item()
        if item is None or item.data(0, KIND_ROLE) != "start":
            return None
        return getattr(self, "rows", self.instances).get(item.data(0, KEY_ROLE))

    def no_start(self):
        """Why the buttons do not act now: no row selected, or one that is not a start."""
        item = self.table.selected_item()
        kind = item.data(0, KIND_ROLE) if item is not None else None
        if kind == "host":
            return tr("A host's row: select one of its starts below it.")
        if kind == "part":
            return tr("What belongs to the start above it: select the start itself to act on it.")
        return tr("Select a row first.")

    # -- the groups: open or closed, kept per host and start --

    def group_toggled(self, item, opened):
        """The human opened or closed ITEM's group (or a start was selected inside it): kept for the next start."""
        if item.childCount() == 0:
            return
        key = item.data(0, ID_ROLE)
        self.open_groups.pop(key, None)
        self.open_groups[key] = bool(opened)  # the newest last: the oldest go first when there are too many
        while len(self.open_groups) > GROUPS_KEPT:
            self.open_groups.pop(next(iter(self.open_groups)))
        self.keep_groups()

    def all_folded(self, folded):
        """View › Fold all (FOLDED) or Unfold all: every group closes or opens, new ones too, kept."""
        self.open_groups.clear()
        self.hosts_open = self.starts_open = not folded
        self.keep_groups()

    def keep_groups(self):
        if getattr(self, "state", None) is None:
            return
        section = self.state.section("sandboxes-groups")
        section.update({"open": dict(self.open_groups), "hosts": self.hosts_open, "starts": self.starts_open})
        self.state.schedule()

    def load_groups(self, state):
        saved = state.get("sandboxes-groups", {})
        opened = saved.get("open") if isinstance(saved.get("open"), dict) else {}
        self.open_groups = {key: value for key, value in list(opened.items())[-GROUPS_KEPT:]
                            if isinstance(key, str) and len(key) < 500 and isinstance(value, bool)}
        self.hosts_open = saved.get("hosts") if isinstance(saved.get("hosts"), bool) else True
        self.starts_open = saved.get("starts") if isinstance(saved.get("starts"), bool) else False

    def open_part(self, item):
        """Double-click on what belongs to a start: the row of the start or MCP server it stands for, if listed."""
        if item.data(0, KIND_ROLE) != "part" or not item.data(0, KEY_ROLE):
            return
        target = self.table.find_item(row_id(id_host(item.data(0, ID_ROLE)), item.data(0, KEY_ROLE)))
        if target is not None:
            self.table.select_item(target)

    # -- buttons --

    def selection_changed(self):
        instance = self.current()
        if instance is not None and getattr(self, "state", None) is not None:
            self.wanted = instance.key
            self.state.set("sandboxes-selected", instance.key)
        item = self.table.selected_item()
        if instance is None and item is not None:  # a host's row, or what belongs to a start: what it says
            self.detail.setText(" · ".join(text for text in (item.text(0), item.text(1), item.text(6)) if text))
        elif instance is None:
            self.detail.setText(tr("Select a row: the buttons below act on it."))
        elif getattr(instance, "host", "local") != "local":
            self.detail.setText(" · ".join([tr("On {host}", host=printable(instance.host)), printable(instance.name),
                                            *([printable(instance.project)] if instance.project else [])]))
        else:
            parts = [tr("Run {run}", run=printable(instance.run)) if instance.run else tr("not in the run registry")]
            if instance.pid:
                parts.append(f"PID {instance.pid}")
            if instance.project:
                parts.append(printable(instance.project))
            self.detail.setText(" · ".join(parts))
        running = instance is not None and instance.state in ACTIVE_STATES
        self.llm_status.watch(getattr(instance, "host", "local"), (instance.record or {}).get("llm") if running else None)
        self.update_buttons()
        self.load_devices()

    def update_remote_buttons(self, instance):
        """A start on another host: attach, pause, resume, stop and delete it over SSH, and while it runs what it may
        reach, its workspaces, devices and limits (sandbox-ai there does it, with its own checks); its connection log
        is on that host."""
        host = printable(instance.host)
        state, attachable = instance.state, bool((instance.record or {}).get("attachable"))
        running = state in ("running", "paused")
        shown_state = tr(STATE_TITLES.get(state, state))
        stale = self.stale(instance)
        # A host that does not answer: what its rows say is what was seen then; nothing is sent to it from them.
        gone = None if stale is None else tr("{host} does not answer (last seen {seconds} s ago): its state now is not "
                                             "known.", host=host, seconds=stale)
        if gone is None and instance.key.startswith("ended:"):
            gone = tr("It ended: History… shows what happened to it.")
        mcp = instance.kind == "mcp"
        for key, button in self.run_named.items():
            why = gone or (tr("An MCP server's sandbox: it only stops (sandbox-ai stop there).") if mcp else
                           None if running or key == "log" else tr("Not while it is {state} on {host}.",
                                                                   state=shown_state, host=host))
            set_enabled(button, why is None, why)
        self.update_mcp_buttons(instance, gone)
        # As sandbox-ai there takes them (starts.act): an MCP server only stops; a run without a kept start pauses
        # and resumes but keeps nothing to attach to or delete.
        rules = {"attach": state == "running" and attachable, "attach-app": state == "running" and attachable,
                 "pause": state == "running" and not mcp, "resume": state in ("paused", "stopped") and not mcp,
                 "delete": state == "stopped" and instance.kind == "session"}
        for action, button in self.session_buttons.items():
            why = gone or (tr("An MCP server's sandbox: it only stops (sandbox-ai stop there).") if mcp else
                           None if rules.get(action) else tr("Not while it is {state} on {host}.", state=shown_state,
                                                             host=host))
            set_enabled(button, why is None, why)
        stoppable = state in ("running", "paused")
        why = gone or (None if stoppable else tr("It does not run."))
        set_enabled(self.stop_button, why is None, why,
                    tooltip=tr("End the selected start on {host} (sandbox-ai stop there)", host=host))
        why = gone or (None if instance.kind == "session" and state in ("running", "paused", "stopped") else
                       tr("Only for kept starts (sessions)."))
        set_enabled(self.resume_with_button, why is None, why)
        self.update_manage_buttons(instance, gone)

    def remote_action(self, instance, action, wait=None):
        """ACTION (pause, resume, stop, delete) on the start INSTANCE of another host, as `sandbox-ai ACTION NAME`
        there (a stop with --wait WAIT when it is not sandbox-ai's own 30 s); the hosts' starts are read again after
        it."""
        args = [action, instance.name] + (["--yes"] if action == "delete" else [])
        if action == "stop" and wait not in (None, STOP_WAIT):
            args = backend.stop_args(instance.name, wait)
        title = tr("{action} on {host}", action=tr(ACTION_TITLES.get(action, action)), host=printable(instance.host))

        def done(result, error):
            self.command_done(title, result, error)
            self.refresh_remote([instance.host])  # a fresh read of that host, also when one runs already
        if action == "resume":
            self.started_on.emit(instance.host)
        seconds = 180 + (wait or 0)
        background(lambda: self.sai.host_run(instance.host, *args, timeout=seconds), owner=self, work=True,
                   source=[self.stop_button, *self.session_buttons.values()], timeout=seconds + 30, done=done)

    def update_buttons(self):
        instance = self.current()
        if busy(self):
            # A command runs: its buttons stay greyed out until it is done; a row of another host never keeps
            # the buttons of the row chosen before (they would act on this machine). Rename, History and Clean up
            # follow the row chosen, so they wait too (they would act on a row the running command may change).
            if instance is not None and getattr(instance, "host", "local") != "local":
                for button in [*self.run_buttons, *self.session_buttons.values(), self.stop_button,
                               self.resume_with_button]:
                    set_enabled(button, False, tr("A command runs."))
            for button in self.manage_buttons.values():
                set_enabled(button, False, tr("A command runs."))
            return
        none = self.no_start()
        if instance is not None and getattr(instance, "host", "local") != "local":
            self.update_remote_buttons(instance)
            return
        if instance is not None and instance.key.startswith("ended:"):
            why = tr("It ended: History… shows what happened to it.")
            for button in [*self.run_buttons, *self.session_buttons.values(), self.stop_button,
                           self.resume_with_button]:
                set_enabled(button, False, why)
            self.update_manage_buttons(instance, why)
            return
        self.update_manage_buttons(instance)
        kind = instance.kind if instance else None
        state = instance.state if instance else None
        run_why = (none if instance is None else
                   tr("It has not registered with sandbox-ai (yet): starting, or started by an older sandbox-ai. "
                      "End it in its terminal.") if kind == "unregistered" else
                   tr("It does not run.") if not instance.run else None)
        for key, button in self.run_named.items():
            # a paused sandbox's supervisor answers nothing: what asks it waits for the resume (sandbox-ai refuses)
            why = run_why or (tr("It is paused: resume it first.") if state == "paused" and key in ASKS_SUPERVISOR
                              else None)
            set_enabled(button, why is None, why)
        self.update_mcp_buttons(instance, run_why)
        rules = {"attach": state == "running", "attach-app": state == "running", "pause": state == "running",
                 "resume": state in ("paused", "stopped"), "delete": state == "stopped"}
        foreground = bool(instance is not None and getattr(instance, "foreground", False))
        # A run without a kept start pauses and resumes too (sandbox-ai pause/resume NAME, as on another host); it
        # keeps nothing to attach to or delete.
        plain_run = kind == "run" and bool(instance is not None and instance.run)
        for action, button in self.session_buttons.items():
            if plain_run and action in ("pause", "resume"):
                allowed = state == "running" if action == "pause" else state == "paused"
                why = None if allowed else tr("Not while it is {state}.", state=tr(STATE_TITLES.get(state, state)))
                set_enabled(button, why is None, why)
                continue
            why = (none if instance is None else tr("Only for sessions.") if kind != "session" else
                   tr("It runs in the foreground of its own terminal (started with --no-tmux or without a terminal): "
                      "there is nothing to attach to.") if foreground and action in ("attach", "attach-app") else
                   None if rules[action] else tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state))))
            set_enabled(button, why is None, why)
        why = (none if instance is None else tr("Only for kept starts (sessions).") if kind != "session" else
               None if state in ("running", "paused", "stopped") else
               tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state))))
        set_enabled(self.resume_with_button, why is None, why)
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

    def update_mcp_buttons(self, instance, why=None):
        """Add and Remove MCP server: a kept start (a session) that runs now - sandbox-ai mcp attach --live checks it
        against what the start keeps; paused, it is resumed first."""
        if why is None and instance is not None:
            state = tr(STATE_TITLES.get(instance.state, instance.state))
            why = (tr("An MCP server's sandbox: a server is attached to an agent's start.") if instance.kind == "mcp"
                   else tr("Only for kept starts (sessions): a run without one keeps nothing to check the server "
                           "against.") if instance.kind != "session" else
                   tr("It is paused: resume it first.") if instance.state == "paused" else
                   None if instance.state == "running" else tr("Not while it is {state}.", state=state))
        set_enabled(self.run_named["add-mcp"], why is None, why)
        # Remove: a stopped or paused kept start too (mcp detach without --live: out of its later starts).
        remove_why = why
        if instance is not None and instance.kind == "session" and instance.state not in ("damaged",):
            remove_why = None
        set_enabled(self.run_named["remove-mcp"], remove_why is None, remove_why)

    def update_manage_buttons(self, instance, gone=None):
        """Rename… and History… follow the selected start; Clean up… acts on the host chosen above."""
        if instance is None:
            why = self.no_start()
        elif instance.kind in ("mcp", "unregistered"):
            why = tr("Only the starts of agents have a name to change.")
        elif not backend.valid_run(instance.name):
            why = tr("Not a start's name: {ident}", ident=printable(instance.name)[:60])
        else:
            why = None
        set_enabled(self.manage_buttons["rename"], (gone or why) is None, gone or why)
        history_why = self.no_start() if instance is None else None if backend.valid_run(instance.name) \
            and instance.kind != "unregistered" else tr("Not a start's name: {ident}",
                                                        ident=printable(instance.name)[:60])
        if history_why is None and gone and self.stale(instance) is not None:
            history_why = gone
        set_enabled(self.manage_buttons["history"], history_why is None, history_why)
        host = self.clean_host(instance)
        stale_host = host not in (None, LOCAL) and host in self.remote_problems
        why = tr("Choose a host above (or select a start of the host to clean up on).") if host is None else \
            tr("{host} does not answer.", host=printable(host)) if stale_host else None
        set_enabled(self.manage_buttons["clean"], why is None, why)

    def clean_host(self, instance=None):
        """The host Clean up… acts on: the one chosen above, else (All hosts) the selected start's; None: neither."""
        chosen = self.host_filter.currentData()
        if chosen is not None:
            return chosen
        instance = instance if instance is not None else self.current()
        return getattr(instance, "host", LOCAL) if instance is not None else None

    def start_host(self, instance):
        """(host, name) as `sandbox-ai rename`, `list --history` take a start there."""
        host = getattr(instance, "host", LOCAL)
        name = instance.session if host == LOCAL and instance.session else instance.name
        return host, name

    def rename(self):
        instance = self.current()
        if instance is None or instance.kind in ("mcp", "unregistered"):
            return
        host, old = self.start_host(instance)
        title = tr("Rename {name} on {host}", name=printable(old), host=printable(host)) if host != LOCAL else \
            tr("Rename {name}", name=printable(old))
        new, ok = QInputDialog.getText(self, title, tr("New name (1-40 letters, digits, _ . -; its run id stays)"),
                                       text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        if not re.fullmatch(START_NAME, new) or not backend.valid_run(old):
            warn(self, title, tr("A name is 1-40 letters, digits, '_', '.' or '-', beginning with a letter or digit."))
            return

        def done(result, error):
            self.command_done(title, result, error, quiet_success=False)
            if host != LOCAL:
                self.refresh_remote([host])
        background(lambda: self.sai.host_run(host, "rename", old, new, timeout=60), owner=self, work=True,
                   source=list(self.manage_buttons.values()), timeout=90, done=done)

    def history(self):
        instance = self.current()
        if instance is None:
            return
        host, name = self.start_host(instance)
        if not backend.valid_run(name):
            return
        title = tr("History of {name} on {host}", name=printable(name), host=printable(host)) if host != LOCAL else \
            tr("History of {name}", name=printable(name))
        background(self.sai.start_history, name, host, owner=self, source=self.manage_buttons["history"], timeout=90,
                   done=lambda found, error: self.history_shown(title, name, found, error))

    def history_shown(self, title, name, found, error):
        self.update_buttons()
        if error is not None:
            warn(self, title, printable(error), modal=False)
            return
        lines = ["  ".join([history_time(entry.get("time")), printable(str(entry.get("event") or "?")),
                            printable(str(entry.get("run") or "-")), history_details(entry)]).rstrip()
                 for entry in found]
        show_text(self, title, "\n".join(lines) or tr("No history of {name}.", name=printable(name)), modal=False)

    def host_title(self, host):
        return tr("this machine") if host in (None, LOCAL) else printable(host)

    def clean_up(self):
        """`sandbox-ai gc` on the host chosen above (All hosts: the selected start's host): what would go is shown
        first (--dry-run), the host named in the question."""
        host = self.clean_host()
        if host is None:
            inform(self, tr("Clean up"), tr("Choose a host above (or select a start of the host to clean up on)."),
                   modal=False)
            return
        title = tr("Clean up on {host}", host=self.host_title(host))
        background(self.sai.collect, host, True, owner=self, source=list(self.manage_buttons.values()), timeout=150,
                   done=lambda found, error: self.clean_previewed(host, title, found, error))

    def clean_previewed(self, host, title, found, error):
        self.update_buttons()
        if error is not None:
            warn(self, title, printable(error), modal=False)
            return
        lines = gc_lines(found)
        if not lines:
            inform(self, title, tr("Nothing to remove."), modal=False)
            return
        text = on_host(host, tr("Remove these? Kept starts are deleted with their HOME; this cannot be undone.")) + \
            "\n\n" + "\n".join(lines)
        if not ask_first(self, "gc", title, text, tr("Remove")):
            return

        def done(result, error_):
            if isinstance(error_, backend.GcChanged):
                warn(self, title, tr("Nothing was removed: what would go has changed since it was shown. Clean up… "
                                     "shows it again."), modal=False)
            elif error_ is not None:
                warn(self, title, printable(error_), modal=False)
            else:
                lines = gc_lines(result)
                inform(self, title, tr("Removed:") + "\n" + "\n".join(lines) if lines else tr("Nothing to remove."),
                       modal=False)
            self.refresh_soon(again=True)
            if host != LOCAL:
                self.refresh_remote([host])
        # The same --older-than as the list shown, and only if a dry run still lists exactly that (GcChanged).
        background(self.sai.collect_as_shown, host, found, owner=self, source=list(self.manage_buttons.values()),
                   work=True, timeout=630, done=done)

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
        remote = getattr(instance, "host", "local") != "local"
        if remote:  # the host is named: the same name may run on this machine too
            text = tr("On {host}: {text}", host=printable(instance.host), text=text)
        # An MCP server is shared by every start that uses it (mcp-stop); any other start ends alone (start-stop).
        if not (ask_first(self, "mcp-stop", tr("Stop"), text, tr("Stop")) if instance.kind == "mcp" else
                ask_first(self, "start-stop", tr("Stop"), text, tr("Stop"))):
            return
        wait = self.stop_wait.value()
        if remote:
            self.remote_action(instance, "stop", wait if instance.kind == "session" else None)
            return
        sources = [self.stop_button, *self.session_buttons.values(), *self.run_buttons]
        if instance.kind == "session" and wait != STOP_WAIT:  # sandbox-ai stop NAME --wait SECONDS
            stop = backend.stop_args(instance.name, wait)
            background(lambda: self.sai.call(*stop, timeout=wait + 60), owner=self, source=sources, timeout=wait + 90,
                       work=True, done=lambda result, error: self.command_done(tr("Stop"), result, error))
            return
        if instance.kind == "session":
            call = (self.sai.call, "session", "stop", instance.name)
        elif instance.kind == "mcp":
            call = (self.sai.stop_mcp, instance.mcp)
        else:
            call = (self.sai.stop_run, instance.run)
        background(*call, owner=self, work=True, source=sources, timeout=330,
                   done=lambda result, error: self.command_done(tr("Stop"), result, error))

    def command_done(self, title, result, error, quiet_success=True):
        status, out, err = outcome(result, error)
        if status != 0 or not quiet_success:
            # The outcome of a job in the background: shown without blocking (a question may wait meanwhile).
            (inform if status == 0 else warn)(self, title, (out + err).strip(), modal=False)
        self.refresh_soon(again=True)

    # -- devices and limits --

    def load_devices(self):
        """The devices added to the selected sandbox during its run (in the background)."""
        instance = self.current()
        run = instance.run if instance and getattr(instance, "host", "local") == "local" else None
        self.devices_token += 1
        token = self.devices_token
        if not run:
            self.added, self.added_workspaces = [], []
            self.devices_label.setText("")
            self.workspaces_label.setText("")
            return
        background(self.sai.devices_added, run, timeout=30,
                   done=lambda found, error: self.devices_loaded(token, found, error))
        background(self.sai.workspaces, run, timeout=30,
                   done=lambda found, error: self.workspaces_loaded(token, found, error))

    def workspaces_loaded(self, token, found, error):
        if token != self.devices_token:
            return
        entries = list(found or []) if error is None else []
        self.added_workspaces = [e for e in entries if e.get("source") == "added"]
        if isinstance(error, (AttributeError, ImportError)):
            self.workspaces_label.setText(tr("This sandbox-ai version cannot add workspaces during a run."))
            return
        def shown(entry):
            note = entry.get("note") if isinstance(entry.get("note"), str) else ""  # what sandbox-ai says of it
            return workspace_label(entry) + (f": {printable(note)[:300]}" if note else "")
        self.workspaces_label.setText(tr("Workspaces added during the run: {list}",
                                         list=", ".join(map(shown, self.added_workspaces)))
                                      if self.added_workspaces else tr("No workspace added during the run."))

    def devices_loaded(self, token, found, error):
        if token != self.devices_token:
            return
        self.added = list(found or []) if error is None else []
        if isinstance(error, AttributeError):
            self.devices_label.setText(tr("This sandbox-ai version cannot change devices during a run."))
        else:
            self.devices_label.setText(tr("Added during the run: {list}", list=", ".join(map(printable, self.added)))
                                       if self.added else tr("No device added during the run."))

    def on_host(self, host, method, *args):
        """METHOD(*ARGS) for a sandbox of HOST, to run in the background (another host: host=HOST)."""
        return (lambda: method(*args)) if host == "local" else (lambda: method(*args, host=host))

    def add_device(self):
        target = self.target()
        if not target:
            return
        host, run = target
        if host != "local":  # that host's devices: typed in, checked by sandbox-ai there
            self.choose_device(run, [], host)
            return
        background(self.sai.device_candidates, owner=self, source=self.run_buttons, timeout=30,
                   done=lambda found, error: self.choose_device(run, found or []))

    def choose_device(self, run, candidates, host="local"):
        self.update_buttons()
        remote = host != "local"
        dialog = DeviceDialog(self, candidates, tr("Add device to {run} on {host}", run=printable(run),
                                                   host=printable(host))) if remote else DeviceDialog(self, candidates)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.path():
            return
        path = dialog.path()
        if remote:
            if not backend.valid_device(path):
                warn(self, tr("Not added"), tr("A device is a path under /dev/, or usb:VENDOR:PRODUCT[:SERIAL]."))
                return
            title = tr("Add device on {host}", host=printable(host))
            if ask_first(self, "device-add", title, tr("Give {path} of {host} to {run} until it ends? Devices weaken "
                                                       "the isolation; sandbox-ai there checks the device as at a "
                                                       "start.", path=printable(path), host=printable(host),
                                                       run=printable(run)), tr("Add")):
                background(self.on_host(host, self.sai.add_device, run, path), owner=self, source=self.run_buttons,
                           work=True, timeout=150, done=lambda result, error: self.device_done(title, result, error))
            return
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
        if ask_first(self, "device-add", tr("Add device"), text, tr("Add")):
            background(self.sai.add_device, run, path, owner=self, work=True, source=self.run_buttons, timeout=150,
                       done=lambda result, error: self.device_done(tr("Add device"), result, error))

    def remove_device(self):
        target = self.target()
        if not target:
            return
        host, run = target
        if host != "local":  # what was added there, as that host lists it (devices list RUN --json): chosen from
            title = tr("Remove device from {run} on {host}", run=printable(run), host=printable(host))
            background(self.sai.host_devices_added, host, run, owner=self, source=self.run_buttons, timeout=90,
                       done=lambda found, error: self.remote_devices_listed(host, run, title, found, error))
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
        background(self.sai.remove_device, run, path, owner=self, work=True, source=self.run_buttons, timeout=150,
                   done=lambda result, error: self.device_done(tr("Remove device"), result, error))

    def remote_devices_listed(self, host, run, title, found, error):
        """The devices added to RUN on HOST, read there: one is removed by its id (the one there is, or the one
        chosen)."""
        self.update_buttons()
        if error is not None:
            warn(self, title, tr("What was added there could not be read: {error}", error=printable(error)[:500]),
                 modal=False)
            return
        if not found:
            inform(self, title, tr("No device was added to {run} during its run (devices given at the start stay).",
                                   run=printable(run)), modal=False)
            return
        ident = found[0][0]
        if len(found) > 1:
            labels = [label for _, label in found]
            choice, ok = QInputDialog.getItem(self, title, tr("Device"), labels, 0, False)
            if not ok:
                return
            ident = found[labels.index(choice)][0]
        background(self.on_host(host, self.sai.remove_device, run, ident), owner=self, source=self.run_buttons,
                   work=True, timeout=150, done=lambda result, error_: self.device_done(title, result, error_))

    def device_done(self, title, result, error):
        report(self, title, result, error)
        self.update_buttons()
        self.load_devices()

    def add_workspace(self):
        target = self.target()
        if not target:
            return
        host, run = target
        remote = host != "local"
        dialog = WorkspaceDialog(self, run, host=host) if remote else WorkspaceDialog(self, run)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.directory():
            return
        directory, read_only = dialog.directory(), dialog.read_only.isChecked()
        name = dialog.name()
        if not backend.valid_directory(directory):
            warn(self, tr("Not added"), tr("Give the folder or file as an absolute path."))
            return
        if name and not backend.valid_workspace_name(name):
            warn(self, tr("Not added"), tr("A name is 1-64 letters, digits, _ . -, starting with a letter or digit."))
            return
        title = tr("Add workspace on {host}", host=printable(host)) if remote else tr("Add workspace")
        if not ask_first(self, "workspace-writable", title, tr(
                "Let {run} change {path} on {host} until it ends? The agent can write, delete and run there.",
                run=printable(run), path=printable(directory), host=printable(host)) if remote else tr(
                "Let {run} change {path} until it ends? The agent can write, delete and run there.",
                run=printable(run), path=printable(directory)), tr("Add"), detail={"read_only": read_only}):
            return
        background(self.on_host(host, self.sai.add_workspace, run, directory, read_only, *([name] if name else [])),
                   owner=self, work=True, source=self.run_buttons, timeout=150,
                   done=lambda result, error: self.device_done(title, result, error))

    def remove_workspace(self):
        target = self.target()
        if not target:
            return
        host, run = target
        if host != "local":  # what was added there, as that host lists it (workspace list RUN --json): chosen from
            title = tr("Remove workspace from {run} on {host}", run=printable(run), host=printable(host))
            background(self.sai.host_workspaces, host, run, owner=self, source=self.run_buttons, timeout=90,
                       done=lambda found, error: self.choose_workspace(host, run, title, found, error))
            return
        self.choose_workspace("local", run, tr("Remove workspace"), self.added_workspaces, None)

    def choose_workspace(self, host, run, title, entries, error):
        """One of the workspaces added to RUN (on HOST) during its run is taken back: the one there is, or the one
        chosen."""
        self.update_buttons()
        if error is not None:
            warn(self, title, tr("What was added there could not be read: {error}", error=printable(error)[:500]),
                 modal=False)
            return
        added = [e for e in entries or [] if e.get("source") == "added"]
        if not added:
            inform(self, title, tr("No workspace was added to {run} during its run (those of the start stay).",
                                   run=printable(run)), modal=False)
            return
        choices = [workspace_label(e) for e in added]
        index = 0
        if len(choices) > 1:
            choice, ok = QInputDialog.getItem(self, title, tr("Workspace"), choices, 0, False)
            if not ok:
                return
            index = choices.index(choice)
        entry = added[index]
        self.detach_workspace(host, run, entry.get("sandbox") or entry.get("host"), title)

    def detach_workspace(self, host, run, target, title):
        """`sandbox-ai workspace remove RUN TARGET` (on HOST). Refused, sandbox-ai says why (a process uses it, the
        agent took a copy into a namespace of its own, copies cannot be looked for); where it offers --force, its own
        words are in the question whether to detach it anyway."""
        if not backend.valid_directory(target) and not backend.valid_run(str(target)):
            warn(self, title, tr("Give the folder as an absolute path, or its name under /workspaces."))
            return

        def done(result, error):
            status, out, err = outcome(result, error)
            refusal = printable((err or out).strip())[-1500:]
            if status != 0 and "--force" in (err or "") and ask_first(
                    self, "workspace-force", title, tr("sandbox-ai did not remove {path} from {run}:\n\n{reason}\n\n"
                                                       "Detach it anyway? Whatever is open there stays usable until "
                                                       "it is closed; a restart takes it away for sure.",
                                                       path=printable(target), run=printable(run), reason=refusal),
                    tr("Detach")):
                background(self.on_host(host, self.sai.remove_workspace, run, target, True), owner=self, work=True,
                           source=self.run_buttons, timeout=150, done=lambda r, e: self.device_done(title, r, e))
                return
            self.device_done(title, result, error)
        background(self.on_host(host, self.sai.remove_workspace, run, target, False), owner=self, work=True,
                   source=self.run_buttons, timeout=150, done=done)

    # -- MCP servers while it runs (sandbox-ai mcp attach|detach … --live) --

    def live_start(self):
        """(host, start name, instance) of the selected running kept start, as `sandbox-ai mcp attach START` takes
        it there; None, said, when none is selected."""
        instance = self.current()
        if instance is None or instance.kind != "session" or instance.state != "running":
            QMessageBox.information(self, tr("Sandboxes"), tr("Select a running kept start (a session) first."))
            return None
        host, name = self.start_host(instance)
        if not backend.valid_run(name):
            warn(self, tr("Sandboxes"), tr("Not a start's name: {ident}", ident=printable(name)[:60]))
            return None
        return host, name, instance

    def add_mcp(self):
        """Add MCP server…: the servers defined on the start's host (`sandbox-ai mcp list` there, in the background),
        then the dialog, asked first (mcp-attach), then `mcp attach … --live` and what sandbox-ai says of it - with
        the offer to restart the agent where it needs one to see the server."""
        target = self.live_start()
        if not target:
            return
        host, name, instance = target
        background(self.sai.host_mcp_servers, host, owner=self, source=self.run_buttons, timeout=90,
                   done=lambda rows, error: self.choose_live_mcp(host, name, instance.agent, rows, error))

    def choose_live_mcp(self, host, start, agent, rows, error):
        self.update_buttons()
        servers = [(row[0], row[1]) for row in rows or []] if error is None else []
        dialog = LiveMcpDialog(self, start, host, servers, agent, problem=None if error is None else str(error))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        kind, value = dialog.value()
        server, helper = (value, None) if kind == "server" else (None, value)
        what = tr("the MCP server {name}", name=printable(value)) if server else \
            tr("{agent} as a helper", agent=HELPER_TITLES.get(value, printable(value)))
        title = tr("Add MCP server on {host}", host=printable(host)) if host != LOCAL else tr("Add MCP server")
        try:
            backend.mcp_live_args(start, server, helper)
        except ValueError as exc:
            warn(self, title, printable(exc))
            return
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        if not ask_first(self, "mcp-attach", title, where + tr(
                "Give {start} the tools of {what} now, while it runs, and at its later starts?", start=printable(start),
                what=what), tr("Attach")):
            return
        background(lambda: self.sai.host_mcp_attach_live(host, start, server, helper), owner=self, work=True,
                   source=self.run_buttons,
                   timeout=630, done=lambda result, error_: self.live_mcp_done(host, start, title, result, error_))

    def live_mcp_done(self, host, start, title, result, error):
        """What sandbox-ai said of the attach, as it said it; where the agent needs (or may need) a restart to see
        the server, the offer to restart it now (`sandbox-ai resume START --restart`, asked as start-restart)."""
        self.update_buttons()
        self.after_live_change(host)
        if error is not None:
            warn(self, title, tr("Not attached: {reason}", reason=printable(error)[:1500]), modal=False)
            return
        text = "\n\n".join([result.get("message") or "", *result.get("notes", [])]).strip()
        if result.get("agent_restart") in ("required", "maybe"):
            if ask_first(self, "start-restart", title, text + "\n\n" + (
                    tr("Codex and OpenCode see a new server only after a restart.")
                    if result.get("agent_restart") == "required" else
                    tr("If /mcp does not list it, restart the agent.")),
                    tr("Restart the agent now (the conversation goes on)")):
                self.restart_agent(host, start, title)
            return
        inform(self, title, text, modal=False)

    def restart_agent(self, host, start, title):
        args = backend.resume_args(start, restart=True)
        background(lambda: self.sai.host_run(host, *args, timeout=300), owner=self, work=True, source=self.run_buttons,
                   timeout=330, done=lambda result, error: (self.command_done(title, result, error),
                                                            self.after_live_change(host)))

    def after_live_change(self, host):
        """The start's rows are read again: the server it reaches now shows below it."""
        self.refresh_soon(again=True)
        if host != LOCAL:
            self.refresh_remote([host])

    def live_servers(self, instance):
        """The MCP servers INSTANCE reaches now (run.json's mcp_servers, `sandbox-ai list`'s mcp_running)."""
        from .start_tree import reached
        return [name for name, now in reached(instance) if now]

    def remove_mcp(self):
        """Remove MCP server…: one of the servers the start reaches now (as its rows say; else as `sandbox-ai mcp
        list` there says whom each serves), taken out with `mcp detach … --live` - a helper as --helper AGENT."""
        instance = self.current()
        if instance is not None and instance.kind == "session" and instance.state != "running":
            # Stopped or paused: out of its options, for its later starts (mcp detach without --live).
            host, name = self.start_host(instance)
            if not backend.valid_run(name):
                warn(self, tr("Sandboxes"), tr("Not a start's name: {ident}", ident=printable(name)[:60]))
                return
            from .start_tree import reached
            self.choose_detach(host, name, [server for server, _now in reached(instance)], None, live=False)
            return
        target = self.live_start()
        if not target:
            return
        host, name, instance = target
        servers = self.live_servers(instance)
        if servers:
            self.choose_detach(host, name, servers, None)
            return
        background(self.sai.host_mcp_servers, host, owner=self, source=self.run_buttons, timeout=90,
                   done=lambda rows, error: self.choose_detach(host, name, [
                       row[0] for row in rows or [] if name in [part.strip() for part in str(row[2]).split(",")]],
                       error))

    def choose_detach(self, host, start, servers, error, live=True):
        self.update_buttons()
        title = tr("Remove MCP server from {start} on {host}", start=printable(start), host=printable(host)) \
            if host != LOCAL else tr("Remove MCP server from {start}", start=printable(start))
        servers = [s for s in dict.fromkeys(servers) if re.fullmatch(backend.MCP_NAME, str(s))]
        if not servers and not live:
            inform(self, title, tr("{start} has no MCP server in its options.", start=printable(start)), modal=False)
            return
        if not servers:
            inform(self, title, tr("{start} reaches no MCP server now.", start=printable(start)) + (
                " " + tr("The servers there could not be read: {error}", error=printable(error)[:300])
                if error is not None else ""), modal=False)
            return
        labels = [tr("{agent} as a helper ({name})", agent=HELPER_TITLES[backend.helper_of(s)], name=s)
                  if backend.helper_of(s) else s for s in servers]
        choice, ok = QInputDialog.getItem(self, title, tr("MCP server"), labels, 0, False)
        if not ok or choice not in labels:
            return
        server = servers[labels.index(choice)]
        helper = backend.helper_of(server)
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        question = tr("Take {name} from {start} now? Its relay closes at once: the agent's connections to it end and "
                      "its calls fail; its later starts go without it.", name=printable(choice),
                      start=printable(start)) if live else \
            tr("Take {name} out of {start}'s options? Its later starts go without it.", name=printable(choice),
               start=printable(start))
        if not ask_first(self, "mcp-detach", title, where + question, tr("Remove")):
            return
        call = self.sai.host_mcp_detach_live if live else self.sai.host_mcp_detach
        background(lambda: call(host, start, None if helper else server, helper),
                   owner=self, work=True, source=self.run_buttons, timeout=630,
                   done=lambda result, error_: self.detached(host, title, result, error_))

    def detached(self, host, title, result, error):
        self.update_buttons()
        self.after_live_change(host)
        if error is not None:
            reason = "\n".join(printable(line) for line in str(error).splitlines())  # its lines, not \x0a
            warn(self, title, tr("Not removed: {reason}", reason=reason[:1500]), modal=False)
            return
        inform(self, title, "\n\n".join([result.get("message") or "", *result.get("notes", [])]).strip(),
               modal=False)

    def limits(self):
        """The limits dialog, filled with the limits now (read in the background first)."""
        target = self.target()
        if not target:
            return
        host, run = target
        background(self.sai.limits_shown, run, host, owner=self, source=self.run_buttons, timeout=90,
                   done=lambda current, error: self.limits_known(host, run, current, error))

    def limits_known(self, host, run, current, error):
        self.update_buttons()
        note = None if error is None else tr("The limits it has now could not be read: {error}",
                                             error=printable(error)[:300])
        dialog = LimitsDialog(self, run, host=host, current=current if error is None else None, note=note)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        memory, cpus, pids = dialog.values()
        title = tr("Limits of {run} on {host}", run=printable(run), host=printable(host)) if host != "local" else \
            tr("Limits")
        if not (memory or cpus or pids):
            if getattr(dialog, "cleared", lambda: [])():
                inform(self, title, tr("Nothing changed: an empty field leaves its limit as it is. To remove a limit, "
                                       "type none."), modal=False)
            return  # nothing changed
        # More than now (or no limit, or not known what it has now): asked first, saying what grows.
        values = {"memory": memory, "cpus": cpus, "pids": pids}
        known = current if error is None else None
        labels = {"memory": tr("memory"), "cpus": tr("CPUs"), "pids": tr("processes")}
        raised = backend.raised_limits(known, values)
        changes = ", ".join(tr("{limit}: no limit", limit=labels[key]) if str(values[key]).strip().lower() == "none"
                            else tr("{limit}: {value}", limit=labels[key], value=printable(values[key]))
                            for key in raised)
        text = tr("Let {run} use more of its host until it ends: {changes}?", run=printable(run), changes=changes)
        if known is None:
            text += " " + tr("The limits it has now are not known: this may be more than now.")
        if not ask_first(self, "limits-raise", title, text, tr("Raise"), detail={"current": known, "values": values}):
            return
        background(self.on_host(host, self.sai.set_limits, run, memory, cpus, pids), owner=self, work=True,
                   source=self.run_buttons, timeout=150, done=lambda result, error: self.reported(title, result, error))

    def reported(self, title, result, error):
        report(self, title, result, error)
        self.update_buttons()

    def target(self):
        """(host, run) of the selected running sandbox, RUN as `sandbox-ai net`, `devices`, `workspace` and `limits`
        take it (on another host: the start's name there); None, said, when none is selected."""
        instance = self.current()
        if instance is not None and getattr(instance, "host", "local") != "local":
            if instance.state in ("running", "paused") and backend.valid_run(instance.name):
                return instance.host, instance.name
        elif instance is not None and instance.run:
            return "local", instance.run
        QMessageBox.information(self, tr("Sandboxes"), tr("Select a running sandbox first."))
        return None

    def current_run(self):
        target = self.target()
        return target[1] if target else None

    def network(self, op):
        target = self.target()
        if not target:
            return
        host, run = target
        if host != "local":
            title = tr("Allow for {run} on {host}", run=printable(run), host=printable(host)) if op == "allow" else \
                tr("Revoke for {run} on {host}", run=printable(run), host=printable(host))
            # What sandbox-ai there takes (its own `net allow --help`, kept per host and version), not what the one
            # here does; asked in the background.
            background(self.sai.host_net_options, host, op, owner=self, source=self.run_buttons, timeout=90,
                       done=lambda found, error: self.network_dialog(host, run, op, title, found, error))
            return
        title = tr("Allow for {run}", run=run) if op == "allow" else tr("Revoke for {run}", run=run)
        self.network_dialog(host, run, op, title, self.sai.net_options(op), None)

    def network_dialog(self, host, run, op, title, supported, error):
        self.update_buttons()
        presets = self.sai.presets()
        if host != LOCAL:
            # Not known (no answer, an older sandbox-ai): only what every version since 0.1.70 takes. Ports "on this
            # computer" would be that host's loopback: never offered from here.
            supported = set(supported or ()) if error is None else set(backend.BASE_NET_OPTIONS)
            supported -= {"--allow-host-port"}
            presets = {name: hosts for name, hosts in presets.items() if f"--allow-{name}" in supported}
        if op != "allow":
            supported = set(supported or ()) - {"--keep"}
        dialog = NetworkDialog(self, presets, title, supported)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.arguments():
            return
        arguments = dialog.arguments()
        keep = dialog.kept()
        done = lambda result, error_: self.reported(title, result, error_)  # noqa: E731
        if op != "allow":  # a revoke narrows: sent at once
            background(self.sai.host_run, host, "net", "revoke", run, *arguments, owner=self, work=True,
                       source=self.run_buttons, timeout=150, done=done)
            return
        where = tr("on {host}", host=printable(host)) if host != LOCAL else tr("on this machine")
        question = tr("Let {run} ({where}) reach this until it ends, at once?\n\n{what}", run=printable(run),
                      where=where, what=printable(" ".join(arguments)))
        if keep:  # and into the project's file: every later start of that project gets it
            question += "\n\n" + tr("It is also kept for the later starts of its project: written into its trusted "
                                     ".sandbox-ai.json.")
        if not ask_first(self, "net-allow", title, question, tr("Allow")):
            return
        background(self.sai.host_run, host, "net", "allow", run, *arguments, *(["--keep"] if keep else []),
                   owner=self, work=True, source=self.run_buttons, timeout=150, done=done)

    def show_allowed(self):
        target = self.target()
        if not target:
            return
        host, run = target

        def read():  # what it was given, and whether its sandbox-ai keeps it for later starts (net show --keep)
            shown = self.sai.host_run(host, "net", "show", run, timeout=120)
            try:
                keeps = "--keep" in (self.sai.net_options("show") if host == LOCAL else
                                     self.sai.host_net_options(host, "show"))
            except ValueError:  # its --help not answered: not offered
                keeps = False
            return shown, keeps
        background(read, owner=self, source=self.run_buttons, timeout=200,
                   done=lambda result, error: self.allowed_shown(run, result[0] if error is None else None, error,
                                                                 host, keeps=error is None and result[1]))

    def allowed_shown(self, run, result, error, host="local", keeps=False):
        """What the run was given since its start, with Untaint… (sandbox-ai net untaint RUN) and Keep…: not modal,
        so the Approvals page stays usable while it is open."""
        from . import widgets
        title = tr("{run} on {host}", run=printable(run), host=printable(host)) if host != "local" else printable(run)
        text = "\n".join(printable(line) for line in "\n".join(outcome(result, error)[1:]).strip().splitlines())
        self.update_buttons()
        self.allowed_dialog = AllowedDialog(self, title, text, lambda: self.untaint(host, run),
                                            keep=(lambda every: self.keep_network(host, run, every)) if keeps else None)
        if widgets.AUTO_ANSWER is not None:  # tests: what would be shown is collected
            widgets.SHOWN.append((title, text, None))
            return
        widgets.show_modeless(self.allowed_dialog)

    def keep_network(self, host, run, every=False):
        """`sandbox-ai net show RUN --keep` (EVERY: --keep-all) on HOST: what the run was given goes into its project's
        .sandbox-ai.json, for every later start of that project - asked first."""
        title = tr("Keep for later starts: {run} on {host}", run=printable(run), host=printable(host)) \
            if host != LOCAL else tr("Keep for later starts: {run}", run=printable(run))
        text = tr("Keep what you gave {run} (allowed names and addresses, networks, DNS, ports, devices, workspaces) "
                  "in its project's .sandbox-ai.json? Every later start of that project gets it.", run=printable(run))
        if every:
            text += " " + tr("Also what approvals opened once or for the run.")
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not backend.valid_run(run) or not ask_first(self, "net-keep", title, text, tr("Keep")):
            return
        background(self.sai.net_keep, host, run, every, owner=self, work=True, source=self.run_buttons, timeout=120,
                   done=lambda result, error: self.reported(title, result, error))

    def untaint(self, host, run):
        """`sandbox-ai net untaint RUN` (on HOST): the rules for tainted runs no longer hold - asked first."""
        title = tr("Untaint {run} on {host}", run=printable(run), host=printable(host)) if host != LOCAL else \
            tr("Untaint {run}", run=printable(run))
        if not backend.valid_run(run) or not ask_first(self, "net-untaint", title, tr(
                "Take back the taint of {run}? Untrusted content entered it; the rules for tainted runs (what it may "
                "reach, what is asked) no longer hold until such content enters it again. Only when you checked what "
                "came in.", run=printable(run)), tr("Untaint")):
            return
        background(self.sai.host_run, host, "net", "untaint", run, owner=self, source=self.run_buttons, timeout=90,
                   work=True, done=lambda result, error: self.reported(title, result, error))

    def log(self):
        instance = self.current()
        if instance is not None and getattr(instance, "host", LOCAL) != LOCAL:
            # That host's log: its proxy's records through sandbox-ai there (the Connections page's host chooser).
            if backend.valid_run(instance.name):
                self.open_remote_log.emit(instance.host, ["--session", instance.name] if instance.kind == "session"
                                          else [instance.name])
            return
        if instance is not None and instance.project:
            self.open_log.emit(instance.project)

    # -- sessions --

    def session(self, action, instance=None):
        instance = instance or self.current()
        if instance is not None and getattr(instance, "host", "local") != "local":
            host = printable(instance.host)
            if action in ("attach", "attach-app"):
                try:
                    self.terminal(self.sai.host_terminal(instance.host, "attach", instance.name),
                                  f"{host}: {printable(instance.name)}", embedded=action == "attach-app")
                except Exception as exc:  # noqa: BLE001 - shown, the window stays
                    warn(self, tr("Not attached"), printable(exc))
                return
            if action == "delete" and not ask_first(
                    self, "start-delete", tr("Delete"), tr("On {host}: delete session {name} and its HOME?", host=host,
                                                           name=printable(instance.name)), tr("Delete")):
                return
            if action == "resume" and instance.state == "stopped":
                self.resume_checked(instance, lambda: self.remote_action(instance, "resume"))
                return
            self.remote_action(instance, action)
            return
        if instance is not None and instance.kind == "run" and action in ("pause", "resume") and \
                backend.valid_run(instance.name):
            # A run without a kept start: sandbox-ai pause/resume NAME (starts.act), as for another host's.
            title = tr(ACTION_TITLES.get(action, action))
            background(self.sai.call, action, instance.name, owner=self, work=True,
                       source=[self.stop_button, *self.session_buttons.values()], timeout=150,
                       done=lambda result, error: self.command_done(title, result, error))
            return
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
        if action == "delete" and not ask_first(
                self, "start-delete", tr("Delete"), tr("Delete session {name} and its HOME on this machine? This "
                                                       "cannot be undone.", name=printable(name)), tr("Delete")):
            return
        title = tr(ACTION_TITLES.get(action, action))

        def go():
            background(self.sai.call, "session", action, name, *(["--yes"] if action == "delete" else []), owner=self,
                       work=True, source=[self.stop_button, *self.session_buttons.values()], timeout=150,
                       done=lambda result, error: self.command_done(title, result, error))
        if action == "resume" and instance.state == "stopped":
            self.resume_checked(instance, go)
            return
        go()

    def resume_with(self, instance=None):
        """Resume with…: the selected kept start (of this machine or a host) started again with what the dialog adds,
        or restarted - each widening asked first: a restart (start-restart), workspaces and writable mounts
        (workspace-writable), MCP servers (mcp-attach). A stopped one's HOME is reviewed first (resume_checked)."""
        instance = instance or self.current()
        if instance is None or instance.kind != "session":
            QMessageBox.information(self, tr("Sessions"), tr("Select a session first."))
            return
        host, name = self.start_host(instance)
        servers = []
        if host == LOCAL:
            try:
                servers = sorted(self.sai.mcp_definitions())
            except Exception:  # noqa: BLE001 - a damaged mcp.json: typed in then
                servers = []
        dialog = ResumeDialog(self, name, host, instance.state in ("running", "paused"), servers)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title = tr("Resume {name} on {host}", name=printable(name), host=printable(host)) if host != LOCAL else \
            tr("Resume {name}", name=printable(name))
        try:
            workspaces, mounts, mcp, restart = dialog.values()
            args = backend.resume_args(name, restart, workspaces, mounts, mcp)
        except ValueError as exc:
            warn(self, title, printable(exc))
            return
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        if restart and not ask_first(self, "start-restart", title, where + tr(
                "Restart {name} now? What runs in it ends; the agent continues its conversation.", name=printable(name)),
                tr("Restart")):
            return
        widening = [*workspaces, *(f"{source} -> {destination}" for source, destination, mode in mounts if mode == "rw")]
        if widening and not ask_first(self, "workspace-writable", title, where + tr(
                "Let {name} change these on its host, from now on at every start: {list}?", name=printable(name),
                list=printable(", ".join(widening))), tr("Add")):
            return
        if mcp and not ask_first(self, "mcp-attach", title, where + tr(
                "Give {name} the tools of these MCP servers, from now on at every start: {list}?", name=printable(name),
                list=printable(", ".join(mcp))), tr("Attach")):
            return

        def go():
            def done(result, error):
                self.command_done(title, result, error, quiet_success=False)
                if host != LOCAL:
                    self.refresh_remote([host])
            background(lambda: self.sai.host_run(host, *args, timeout=300), owner=self, work=True,
                       source=[self.stop_button, *self.session_buttons.values(), self.resume_with_button],
                       timeout=330, done=done)
        if instance.state == "stopped":
            self.resume_checked(instance, go)
            return
        go()

    def resume_checked(self, instance, go):
        """Before a stopped kept start starts again (GO): what changed in the start files of its HOME since its last
        start (`sandbox-ai resume NAME --review-home --json`, in the background), shown in a question when anything
        did or not every file could be checked. Whatever wrote there - the agent too - runs at the next start."""
        host = getattr(instance, "host", "local")
        name = instance.session if host == "local" and instance.session else instance.name
        background(self.sai.review_home, name, host, owner=self,
                   source=[self.stop_button, *self.session_buttons.values()], timeout=150,
                   done=lambda result, error: self.home_reviewed(host, name, result, error, go))

    def home_reviewed(self, host, name, result, error, go):
        self.update_buttons()
        title = tr("Resume {name} on {host}", name=printable(name), host=printable(host)) if host != "local" else \
            tr("Resume {name}", name=printable(name))
        if error is not None:
            if review_refusal(error):  # nothing kept: nothing to review
                go()
            elif ask_first(self, "home-review-skip", title,
                           tr("What changed in the HOME of {name} since its last start could not be checked:\n"
                              "{error}\n\nResume it anyway?", name=printable(name), error=printable(error)[-800:]),
                           tr("Resume")):
                go()
            return
        found = home_review(name, result, host)
        if found is None:
            go()
            return
        text, details = found
        if ask_first(self, "home-review-skip", title, text + "\n\n" + tr("Resume it anyway?"), tr("Resume"),
                     details=details):
            go()


class ConnectionsPage(QWidget):
    """The connection proxy's log: every request, push and refusal. While "follow" is on and the page is
    shown, the log is read again when it changed (log_state: inode, size, time), never more often."""

    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        # Whose log: this machine's (its files, read here), or a host's (sandbox-ai activity and logs there, over SSH).
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The connection logs of this machine, or of a host's sandboxes (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        self.remote_sources, self.remote_token, self.remote_read_at = [], 0, 0.0
        self.directory = QComboBox()
        self.directory.currentIndexChanged.connect(lambda _=0: self.source_changed())
        browse = QPushButton(tr("Other folder…"))
        browse.clicked.connect(self.browse)
        self.summary_button = QPushButton(tr("Summary…"))
        self.summary_button.setToolTip(tr("What the logs of the chosen folder say in short: refusals, their targets, "
                                          "requests per host (sandbox-ai logs --json)"))
        self.summary_button.clicked.connect(lambda _=False: self.show_summary())
        self.file = QComboBox()
        self.file.setMinimumWidth(280)
        self.file.currentIndexChanged.connect(lambda _=0: self.load())
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("filter (host, URL, event)"))
        self.filter.setMinimumWidth(220)
        # Typing filters after a pause (FILTER_PAUSE ms), not at every key: up to CONNECTIONS_LIMIT records each time.
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(FILTER_PAUSE)
        self.filter_timer.timeout.connect(self.show_records)
        self.filter.textChanged.connect(lambda _="": self.filter_timer.start())
        self.refused = QCheckBox(tr("only refused and errors"))
        self.refused.toggled.connect(lambda _=False: self.show_records())
        self.follow = QCheckBox(tr("follow"))
        self.follow.setChecked(True)
        self.run_label = QLabel(tr("Run"))
        layout.addWidget(self.hosts)
        self.run_label.setBuddy(self.file)
        self.filter.setAccessibleName(tr("Filter"))
        widgets.compact_combo(self.directory, 30)  # long log folders: elided here, whole in the list
        layout.addWidget(flow(buddy(tr("Logs"), self.directory), self.directory, browse, self.run_label, self.file,
                              self.filter, self.refused, self.follow, self.summary_button))
        self.remote_note = plain_label(role="secondary")
        self.remote_note.hide()
        layout.addWidget(self.remote_note)
        split = self.split = QSplitter(Qt.Orientation.Vertical)
        # A ModelTable: thousands of records are shown in one step (a filter applied again, a new log).
        self.table = table([tr("Time"), tr("Event"), tr("Method"), tr("Status"), tr("Target"), tr("Bytes")], many=True,
                           stretch=4, compact=(0, 1, 2, 3, 5))
        self.table.setAccessibleName(tr("Connections"))
        self.table.itemSelectionChanged.connect(self.details)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        self.detail.setAccessibleName(tr("Details"))
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([400, 220])
        layout.addWidget(split, 1)
        # base: the key of records[0]; keys stay with their records when old ones drop out of the window read.
        self.records, self.shown, self.state, self.base = [], [], None, 0
        self.row_cache = {}  # {key: (record, row, bad, its text in lower case)}: rows()
        self.position = None  # (path, (inode, offset)) where the last read ended: a follow reads only what came
        self.reading = None  # the path being looked at in a thread (log_state, then up to 16 MiB of log)
        self.read_token = 0  # the newest read (a host switch makes every older one late: its answer is dropped)
        self.listing = 0  # the newest listing of folders or logs (an older one that comes back late is dropped)
        self.wanted_log = None  # the end of the name of the log to choose once the logs are listed
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.setInterval(backend.setting("connections_interval"))  # started while the page is shown
        self.refresh_directories()

    # -- another host: its sandboxes' connections through sandbox-ai there (activity --kind=network, logs --json) --

    def host(self):
        return self.hosts.host() or LOCAL

    def remote(self):
        return self.host() != LOCAL

    def host_changed(self):
        """Another host: its running starts and sessions (and folders typed in) are the sources, read over SSH;
        this machine: its log folders and files, as before."""
        remote = self.remote()
        self.file.setVisible(not remote)
        self.run_label.setVisible(not remote)
        self.remote_note.setVisible(remote)
        self.remote_note.setText(self.remote_text() if remote else "")
        self.position, self.reading = None, None
        self.read_token += 1  # a read still on its way is the host's before: dropped when it comes back
        self.listing += 1  # so is a listing of this machine's folders or logs
        self.file.blockSignals(True)
        self.file.clear()  # this machine's logs are no source of another host (nor a host's of this one)
        self.file.blockSignals(False)
        self.apply(None, [])
        if remote:
            self.refresh_remote_sources()
        else:
            self.refresh_directories()

    def remote_text(self):
        return tr("The connections the proxy on {host} logged: refusals, pushes and the first request to each host, "
                  "repeats counted (sandbox-ai activity there); Summary… reads its logs in short (sandbox-ai logs).",
                  host=self.hosts.label())

    def open_remote(self, host, source):
        """The connection log of the start SOURCE (sandbox-ai activity's arguments) on HOST (the Sandboxes page's
        Connection log of a host's start)."""
        if self.host() != host:
            self.hosts.set_host(host)
        if self.host() == host and host != LOCAL:
            self.refresh_remote_sources(select=list(source))

    def refresh_remote_sources(self, select=None):
        """That host's running starts and sessions (`sandbox-ai list --json` there, in the background)."""
        host = self.host()
        self.remote_token += 1
        token = self.remote_token
        self.remote_note.setText(tr("Asking {host}…", host=self.hosts.label(host)))
        background(lambda: self.sai.host_instances(host), timeout=90,  # a look, not work quitting would interrupt
                   done=lambda found, error: alive(self) and self.remote_listed(host, token, select, found, error))

    def remote_listed(self, host, token, select, found, error):
        if token != self.remote_token or host != self.host():
            return
        sources = []
        for instance in found if error is None else []:
            project = instance.project if backend.valid_directory(instance.project) else None
            folder = ["--project", project] if project else None
            if instance.kind == "session" and backend.valid_run(instance.name):
                sources.append((tr("Session: {name} ({status})", name=printable(instance.name),
                                   status=printable(instance.state)), ["--session", instance.name], folder))
            elif instance.kind == "run" and backend.valid_run(instance.name):
                sources.append((tr("Running: {run} ({workspace})", run=printable(instance.name),
                                   workspace=printable(instance.project)), [instance.name], folder))
        typed = [entry for entry in self.remote_sources if entry[1][0] in ("--log-dir", "--project")]
        self.remote_sources = sources + [entry for entry in typed if entry[1] not in [e[1] for e in sources]]
        if select and select not in [entry[1] for entry in self.remote_sources]:
            self.remote_sources.append(self.typed_source(select))
        self.remote_note.setText(tr("{host}: no answer", host=self.hosts.label(host)) + f" ({printable(error)[:300]})"
                                 if error is not None else self.remote_text())
        self.fill_remote_sources(select)

    def typed_source(self, source):
        """(title, activity arguments, logs arguments) of a folder typed in, or of a start chosen elsewhere."""
        if source[0] in ("--log-dir", "--project"):
            return tr("Folder: {path}", path=printable(source[-1])), list(source), list(source)
        return printable(" ".join(source)), list(source), None

    def fill_remote_sources(self, select=None):
        current = select or self.directory.currentData()
        self.directory.blockSignals(True)
        self.directory.clear()
        for title, source, _ in self.remote_sources:
            self.directory.addItem(title, source)
        index = self.directory.findData(current) if current else -1
        self.directory.setCurrentIndex(index if index >= 0 else 0)
        self.directory.blockSignals(False)
        self.source_changed()

    def source_changed(self):
        if self.remote():
            self.position = None
            self.apply(None, [])
            self.load(force=True)
        else:
            self.list_files()

    def summary_source(self):
        """The arguments of `sandbox-ai logs` for the folder chosen (here: its log folder; a host's start: its
        project's), or None."""
        if not self.remote():
            directory = self.directory.currentData()
            return ["--log-dir", directory] if isinstance(directory, str) and backend.valid_directory(directory) \
                else None
        chosen = self.directory.currentData()
        return next((folder for _, source, folder in self.remote_sources if source == chosen), None)

    def show_summary(self):
        source, host = self.summary_source(), self.host()
        title = tr("Logs on {host}", host=self.hosts.label(host)) if host != LOCAL else tr("Logs")
        if source is None:
            inform(self, title, tr("Choose a log folder first (a start whose project is not known has none here)."),
                   modal=False)
            return
        background(self.sai.host_log_summary, host, source, owner=self, source=self.summary_button, timeout=150,
                   done=lambda data, error: self.summary_read(title, data, error))

    def summary_read(self, title, data, error):
        if error is not None:
            warn(self, title, printable(error)[:1500], modal=False)
            return
        show_text(self, title, "\n".join(log_summary_lines(data)), modal=False)

    def refresh_directories(self, select=None, log_suffix=None):
        """The log folders, listed in a thread (a slow disk must not hold up a tab switch); SELECT: the folder
        to choose, LOG_SUFFIX: the end of the name of the log to choose there (e.g. "-RUN.jsonl")."""
        if self.remote():  # this machine's folder (a project, a start of this machine): this machine's logs
            self.hosts.set_host(LOCAL)
            if self.remote():
                return
        self.wanted_log = log_suffix
        current = select or self.directory.currentData()
        self.listing += 1
        token = self.listing
        background(self.sai.log_directories, timeout=STATE_WAIT,  # a look, not work quitting would interrupt
                   done=lambda found, error: self.directories_listed(token, current, found or [], error))

    def directories_listed(self, token, current, found, error):
        if token != self.listing or self.remote():
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
        if self.remote():  # a folder on that host: typed (this machine's file dialog cannot see it)
            path, ok = QInputDialog.getText(self, tr("Log folder"), shown_as_text(tr(
                "A project folder or a log folder on {host} (absolute path):", host=self.hosts.label())))
            path = (path or "").strip()
            if ok and backend.valid_directory(path):
                kind = "--log-dir" if "/sandbox-ai/logs/" in path or path.rstrip("/").endswith("/logs") else "--project"
                entry = self.typed_source([kind, path])
                if entry[1] not in [e[1] for e in self.remote_sources]:
                    self.remote_sources.append(entry)
                self.fill_remote_sources(entry[1])
            elif ok and path:
                warn(self, tr("Log folder"), tr("Not an absolute path: {path}", path=printable(path)), modal=False)
            return
        path = choose_folder(self, tr("Log folder"), "logs")
        if path:
            self.refresh_directories(select=path)

    def open_for_workspace(self, workspace):
        if workspace:
            self.refresh_directories(select=str(self.sai.launcher.default_log_dir(Path(workspace))))

    def list_files(self):
        """The logs of the chosen folder, listed in a thread."""
        if self.remote():
            return
        directory = self.directory.currentData()
        self.listing += 1
        token = self.listing

        def look():
            return sorted(Path(directory).glob("connections-*.jsonl"), reverse=True) if directory else []
        background(look, timeout=STATE_WAIT, done=lambda found, error: self.files_listed(token, found or [], error))

    def files_listed(self, token, found, error):
        if token != self.listing or self.remote():
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
        if not self.timer.isActive():
            self.timer.start()  # it ticks only while the page is shown (tick() reads only then anyway)
        self.hosts.load()  # a host added on the Hosts page meanwhile (a small local file)
        self.load()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self.timer.stop()

    def load(self, force=False):
        """Look at the chosen log in a thread - its state (inode, size, time: a stat may hang on a network file
        system, so not on the GUI thread) and, if that changed since it was last read, its records. At most
        one look at a time: while one has not come back (even a stuck one), no other starts. Another host's:
        load_remote."""
        if self.remote():
            self.load_remote(force)
            return
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
        self.read_token += 1
        token = self.read_token
        background(look, owner=self, timeout=60,
                   done=lambda result, error: alive(self) and self.loaded(path, result, error, token))

    def load_remote(self, force=False):
        """The chosen start's connections on that host (`sandbox-ai activity SOURCE --kind=network --json` there),
        at most every REMOTE_LOG_SECONDS while it follows, one read at a time; the table changes only when they
        did."""
        source, host = self.directory.currentData(), self.host()
        if self.reading is not None or not isinstance(source, list) or not source:
            return
        if not force and time.monotonic() - self.remote_read_at < REMOTE_LOG_SECONDS:
            return
        key = ("remote", host, tuple(source))
        self.reading, self.remote_read_at = key, time.monotonic()
        self.read_token += 1
        token = self.read_token
        background(lambda: self.sai.host_connections(host, source, tail=CONNECTIONS_LIMIT), timeout=150,
                   done=lambda records, error: alive(self) and self.remote_loaded(key, records, error, token))

    def remote_loaded(self, key, records, error, token=None):
        if token is not None and token != self.read_token:
            return  # a read of the host before (or of a source before): never shown, and no longer the one running
        self.reading = None
        if key != ("remote", self.host(), tuple(self.directory.currentData() or ())):
            self.load(force=True)  # another source or host was chosen meanwhile
            return
        if error is not None:
            self.detail.setPlainText(tr("{host}: no answer", host=self.hosts.label(key[1])) +
                                     f" ({printable(error)[:500]})")
            return
        if self.state is not None and self.state[0] == key and records == self.records:
            return
        self.apply((key, len(records)), records)

    def loaded(self, path, result, error, token=None):
        if token is not None and token != self.read_token:
            return  # a read of this machine's log from before a host switch: never shown under another host
        self.reading = None
        if self.remote():
            return
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
        """[(row, record, bad, key)] of the RECORDS the filters let through; FIRST: the key of records[0]. Each record's
        row is made once (row_cache, by key): filtering again only compares texts."""
        text = self.filter.text().lower()
        refused = self.refused.isChecked()
        cache = self.row_cache
        if len(cache) > 3 * CONNECTIONS_LIMIT:  # keys of records that dropped out of the window read
            self.row_cache = cache = {key: value for key, value in cache.items() if key >= self.base}
        shown = []
        for index, record in enumerate(records):
            key = first + index
            made = cache.get(key)
            if made is None or made[0] is not record:
                row = tuple(printable(value) for value in backend.summary(record))
                bad = row[1] in ("denied", "error", "tls-refused", "tls-upstream-failed", "push REFUSED")
                made = cache[key] = (record, row, bad, " ".join(row).lower())
            _, row, bad, lowered = made
            if refused and not bad:
                continue
            if text and text not in lowered:
                continue
            shown.append((row, record, bad, key))
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


def log_summary_lines(data):
    """`sandbox-ai logs --json` in lines (escaped): the folder, refusals and their targets, the requests per host,
    pushes, the newest refusals with why."""
    data = data if isinstance(data, dict) else {}

    def pairs(value):
        return [(printable(str(entry[0]))[:300], entry[1]) for entry in value or []
                if isinstance(entry, (list, tuple)) and len(entry) == 2 and isinstance(entry[1], int)][:20]
    telemetry = data.get("telemetry") if isinstance(data.get("telemetry"), dict) else {}
    lines = [tr("Logs in {path}", path=printable(str(data.get("directory") or "?")))]
    if isinstance(data.get("denied"), int):
        lines.append(tr("Dropped by the firewall: {count}", count=data["denied"]))
    lines += [f"  {count:>6}  {target}" for target, count in pairs(data.get("targets"))]
    if isinstance(telemetry.get("requests"), int):
        lines += ["", tr("Requests: {count}", count=telemetry["requests"])]
        lines += [f"  {count:>6}  {target}" for target, count in pairs(telemetry.get("hosts"))]
    refused = pairs(telemetry.get("refused"))
    if refused:
        lines += ["", tr("Refused by the proxy:")] + [f"  {count:>6}  {target}" for target, count in refused]
    pushes = telemetry.get("pushes") if isinstance(telemetry.get("pushes"), dict) else {}
    if pushes:
        lines += ["", tr("Pushes: {ok} checked, {refused} refused", ok=int(pushes.get("ok") or 0),
                         refused=int(pushes.get("refused") or 0))]
    newest = [entry for entry in data.get("refusals") or [] if isinstance(entry, dict)][-20:]
    if newest:
        lines += ["", tr("The newest refusals:")]
        for entry in newest:
            when, what = printable(str(entry.get("time") or ""))[:19], printable(str(entry.get("what") or ""))[:200]
            lines.append(f"  {when}  {what}")
            if entry.get("reason"):
                lines.append(f"      {printable(str(entry['reason']))[:300]}")
    return lines


class AddServerDialog(QDialog):
    """Add a catalog entry (sandbox-ai mcp add): its name for --mcp, folders, variables, network, limits, and how it
    is taken - an extra catalog, without pulling, its backend, unsigned, behind a VPN. An entry this machine's catalog
    module read names its mounts and variables; one listed by another host's `mcp catalog` gets them typed in (paths
    of that HOST)."""

    def __init__(self, parent, entry, host=LOCAL, catalog=""):
        super().__init__(parent)
        self.entry, self.host, self.catalog = entry, host, catalog
        self.setWindowTitle(tr("Add {name}", name=entry.get("name")) if host == LOCAL else
                            tr("Add {name} on {host}", name=entry.get("name"), host=printable(host)))
        form = QFormLayout(self)
        description = plain_label(entry.get("description", "") or entry.get("title", ""))
        form.addRow(description)
        self.local = QLineEdit(entry.get("name", ""))
        form.addRow(tr("Name for --mcp"), self.local)
        self.mounts, self.mount_lines, self.env_line = {}, None, None
        if entry.get("_listed"):  # only its line is known (another host's catalog): folders typed in
            self.mount_lines = QPlainTextEdit()
            self.mount_lines.setFixedHeight(52)
            self.mount_lines.setPlaceholderText(tr("TARGET=HOST_FOLDER[:ro|rw], one per line (read-only unless :rw)"))
            form.addRow(tr("Folders"), self.mount_lines)
            self.env_line = QLineEdit()
            self.env_line.setPlaceholderText(tr("variables of the environment there, separated by spaces"))
            form.addRow(tr("Variables"), self.env_line)
        for mount in entry.get("mounts", []) if not entry.get("_listed") else []:
            field = PathField()
            field.set_local(host == LOCAL)
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
        self.engine = QComboBox()
        for text, key in ((tr("default (Podman when it is there)"), ""), ("podman", "podman"), ("bwrap", "bwrap"),
                          ("both", "both")):
            self.engine.addItem(text, key)
        self.engine.setToolTip(tr("Podman (the image), Bubblewrap (the image's rootfs, checked and unpacked) or both"))
        form.addRow(tr("Backend"), self.engine)
        # that host's VPN profiles to choose from, any other name typed
        self.vpn = NameField("vpn", getattr(parent, "sai", None), host,
                             placeholder=tr("a VPN profile (sandbox-ai vpn list); needs the full network"))
        self.vpn.setAccessibleName(tr("VPN"))
        form.addRow(tr("VPN"), self.vpn)
        self.no_pull = QCheckBox(tr("Do not pull the image (it is there already)"))
        form.addRow(self.no_pull)
        signature = (entry.get("_signature") or {}).get("state", "unsigned")
        self.unsigned = QCheckBox(labels.shown(tr("Take it although its catalog is not signed by a trusted key "
                                                  "(--unsigned)")))
        self.unsigned.setVisible(signature != "good")
        if signature == "bad":
            set_enabled(self.unsigned, False, tr("Its catalog's signature does not verify: sandbox-ai refuses it "
                                                 "anyway."))
        if signature != "good":
            form.addRow(plain_label(tr("Its catalog is not signed by a trusted key (sandbox/mcp-signers): sandbox-ai "
                                       "adds it only with --unsigned.") if signature != "bad" else
                                    tr("Its catalog's signature does not verify: sandbox-ai refuses it."),
                                    role="notice-warn"))
        form.addRow(self.unsigned)
        if catalog:
            form.addRow(tr("Catalog"), plain_label(printable(catalog)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.prefix_name = None

    def mount_values(self):
        """[(target, host folder, "ro"|"rw")]. ValueError: a line that is not TARGET=HOST_FOLDER[:ro|rw]."""
        found = [(target, field.value(), "rw" if writable.isChecked() else "ro")
                 for target, (field, writable) in self.mounts.items() if field.value()]
        for line in (self.mount_lines.toPlainText().splitlines() if self.mount_lines is not None else []):
            if not line.strip():
                continue
            target, equals, rest = line.strip().partition("=")
            source, _, mode = rest.partition(":")
            if not equals or mode not in ("", "ro", "rw"):
                raise ValueError(tr("A folder is TARGET=HOST_FOLDER or TARGET=HOST_FOLDER:ro|rw: {line}",
                                    line=printable(line.strip())))
            found.append((target, source, mode or "ro"))
        return found

    def values(self, name):
        """The keywords of backend.mcp_add_args for the catalog entry NAME."""
        env = list(self.env) + (self.env_line.text().split() if self.env_line is not None else [])
        return {"entry": name, "name_as": self.local.text().strip() or name.split(":")[-1],
                "mounts": self.mount_values(), "env": env, "network": self.network.currentText(),
                "memory": self.memory.text().strip() or "512m", "pids": self.pids.value(),
                "catalogs": [self.catalog] if self.catalog else [], "no_pull": self.no_pull.isChecked(),
                "engine": self.engine.currentData() or None, "unsigned": self.unsigned.isChecked(),
                "vpn": self.vpn.text().strip() or None}

    def arguments(self, name):
        """The words after `sandbox-ai mcp add` (checked: backend.mcp_add_args)."""
        return backend.mcp_add_args(**self.values(name))[0][2:]


class McpStartDialog(QDialog):
    """Start an MCP server (sandbox-ai mcp start): its backend, a VPN in front, and for an agent start - started as
    that start starts it and attached to it, a running one restarted now if wanted."""

    def __init__(self, parent, name, targets, host=LOCAL):
        super().__init__(parent)
        self.setWindowTitle(tr("Start {name}", name=printable(name)) if host == LOCAL else
                            tr("Start {name} on {host}", name=printable(name), host=printable(host)))
        form = QFormLayout(self)
        self.engine = QComboBox()
        for text, key in ((tr("as defined"), ""), ("podman", "podman"), ("bwrap", "bwrap")):
            self.engine.addItem(text, key)
        form.addRow(tr("Backend"), self.engine)
        self.vpn = NameField("vpn", getattr(parent, "sai", None), host,
                             placeholder=tr("a VPN profile in front (sandbox-ai vpn list)"))
        self.vpn.setAccessibleName(tr("VPN"))
        form.addRow(tr("VPN"), self.vpn)
        self.start = QComboBox()
        self.start.addItem(tr("no start: only the server"), "")
        for entry in targets:
            self.start.addItem(printable(f"{entry['name']} ({entry.get('agent') or '?'}, {entry.get('state')})"),
                               entry["name"])
        self.start.setToolTip(tr("For this agent start: started as it starts it (its backend and rules) and attached "
                                 "to it (--for)"))
        form.addRow(tr("For the start"), self.start)
        self.restart = QCheckBox(tr("Restart it now if it runs (the agent continues its conversation)"))
        self.start.currentIndexChanged.connect(lambda _i: self.restart.setEnabled(bool(self.start.currentData())))
        self.restart.setEnabled(False)
        form.addRow(self.restart)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        """The keywords of backend.mcp_start_args (without the server's name)."""
        start = self.start.currentData() or None
        return {"engine": self.engine.currentData() or None, "vpn": self.vpn.text().strip() or None,
                "for_start": start, "restart": bool(start) and self.restart.isChecked()}


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
    """The MCP servers of this machine or a host of `sandbox-ai hosts` (chosen above): list, start (with a backend, a
    VPN, for an agent start), stop, attach to a start, and the catalogs to add from (sandbox-ai mcp there)."""

    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        self.host_choice = HostCombo(sai)
        self.host_choice.chosen.connect(lambda _host: self.host_changed())
        self.host_note = plain_label(role="secondary")
        layout.addWidget(flow(self.host_choice, self.host_note))
        servers = QGroupBox(tr("Your MCP servers (use them with --mcp NAME)"))
        servers_layout = QVBoxLayout(servers)
        self.servers = table([tr("Name"), tr("Status"), tr("Serves"), tr("Runs"), tr("Catalog")], stretch=2, compact=(1,))
        self.servers.setAccessibleName(tr("MCP servers"))
        servers_layout.addWidget(self.servers)
        row = QHBoxLayout()
        self.server_buttons = []
        for action, text in (("start", tr("Start…")), ("stop", tr("Stop"))):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action: self.server(a))
            row.addWidget(button)
            self.server_buttons.append(button)
        self.server_buttons[0].setToolTip(tr("Start the server sandbox: its backend, a VPN in front, or for an agent "
                                             "start (sandbox-ai mcp start)"))
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
        self.catalog = table([tr("Entry"), tr("Network"), tr("Title"), tr("Published"), tr("Signature"),
                              tr("Catalog")], stretch=2, compact=(1, 3, 5))
        self.catalog.setAccessibleName(tr("Catalog"))
        catalog_layout.addWidget(self.catalog)
        self.catalog_source = QLineEdit()
        self.catalog_source.setPlaceholderText(tr("another catalog instead: https://… or a file on the host "
                                                  "(--catalog)"))
        self.catalog_source.setAccessibleName(tr("Catalog"))
        row = QHBoxLayout()
        load = QPushButton(tr("Reload catalogs"))
        load.setToolTip(tr("The catalogs load by themselves when the page is first shown; this fetches them again"))
        load.clicked.connect(lambda: self.load_catalog())
        add = QPushButton(tr("Add…"))
        add.clicked.connect(self.add)
        self.load_button, self.add_button = load, add
        row.addWidget(self.catalog_source, 1)
        row.addWidget(load)
        row.addWidget(add)
        catalog_layout.addLayout(row)
        # The page's message area: what loading the catalogs and adding said (a refusal of sandbox-ai with a URL in
        # it too), wrapped in the page, selectable, never widening the window.
        self.catalog_status = MessageText()
        self.catalog_status.setAccessibleName(tr("Message"))
        catalog_layout.addWidget(self.catalog_status)
        layout.addWidget(catalog)
        self.entries, self.catalog_requested, self.catalog_of, self.shown_before = {}, False, None, False
        self.refresh()

    def showEvent(self, event):  # noqa: N802
        """The catalogs load by themselves the first time the page is shown (in the background); the hosts are read
        again (one added on the Hosts page meanwhile), and so are the servers each time it is shown again (one added
        with sandbox-ai mcp add meanwhile)."""
        self.host_choice.reload()
        super().showEvent(event)
        if self.shown_before:
            self.refresh()
        self.shown_before = True
        if not self.catalog_requested:
            self.load_catalog(auto=True)

    def host(self):
        return self.host_choice.current()

    def host_changed(self):
        host = self.host()
        self.host_note.setText(tr("The MCP servers of {host} (sandbox-ai mcp there); paths are {host}'s.",
                                  host=printable(host)) if host != LOCAL else "")
        fill(self.servers, [])
        fill(self.catalog, [])
        self.entries = {}
        self.refresh()
        if self.catalog_requested:
            self.load_catalog(auto=True)

    def refresh(self):
        """The servers, read in the background (the window is not held up by it)."""
        host = self.host()
        # A read, not work: no owner, so it never holds up closing the window (busy()).
        background(self.sai.host_mcp_servers, host, timeout=90,
                   done=lambda rows, error: alive(self) and host == self.host() and self.servers_read(rows, error))

    def servers_read(self, rows, error):
        if error is not None:
            rows = [(tr("(error)"), "", "", str(error), "")]
        fill(self.servers, rows, ["ok" if r[1] == "running" else None for r in rows])

    def attach(self):
        name = selected(self.servers)
        if not name:
            inform(self, tr("Attach to…"), tr("Select a server first."))
            return
        host = self.host()
        background(self.sai.host_mcp_targets, host, owner=self, source=self.server_buttons, timeout=90,
                   done=lambda targets, error: self.attach_targets_read(host, name, targets, error))

    def attach_targets_read(self, host, name, targets, error):
        if error is not None:
            warn(self, tr("Attach to…"), printable(str(error)))
            return
        if not targets:
            inform(self, tr("Attach to…"), tr("No kept agent start to attach it to: start one (a run "
                                              "without a kept start takes no new server)."))
            return
        dialog = AttachServerDialog(self, name, targets)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        start, restart = dialog.values()
        text = tr("Give {start} the tools of the MCP server {name}?", start=printable(start), name=printable(name))
        if restart:
            text += " " + tr("A running {start} is stopped and started again for it: what runs in it now ends.",
                             start=printable(start))
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "mcp-restart" if restart else "mcp-attach", tr("Attach to…").rstrip("…"), text,
                         tr("Attach")):
            return
        background(lambda: self.sai.host_mcp_attach(host, name, start, restart), owner=self, source=self.server_buttons,
                   work=True, timeout=630, done=lambda result, error: self.attached(name, result, error))

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
        host = self.host()
        if action == "start":  # its options first: the starts it may be for are read in the background
            background(self.sai.host_mcp_targets, host, owner=self, source=self.server_buttons, timeout=90,
                       done=lambda targets, error: self.start_server(host, name, targets if error is None else []))
            return
        text = tr("Stop {what}? Agents that use it lose it until it is started again.",
                  what=tr("MCP server {name}", name=printable(name)))
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if action == "stop" and not ask_first(self, "start-stop", tr("Stop"), text, tr("Stop")):
            return
        background(lambda: self.sai.host_run(host, "mcp", action, name, timeout=300), owner=self, work=True,
                   source=self.server_buttons, timeout=330, done=lambda result, error: self.server_done(action, result,
                                                                                                       error))

    def start_server(self, host, name, targets):
        """`sandbox-ai mcp start` with the dialog's options; for a start it is attached to it too (asked first:
        mcp-attach, and start-restart where that start is restarted now)."""
        dialog = McpStartDialog(self, name, targets, host)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            args = backend.mcp_start_args(name, **values)
        except ValueError as exc:
            warn(self, tr("Start {name}", name=printable(name)), printable(exc))
            return
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        start = values["for_start"]
        if values["restart"] and not ask_first(self, "start-restart", tr("Start {name}", name=printable(name)), where + tr(
                "Start {name} for {start} and restart {start} now? What runs in it ends; the agent continues its "
                "conversation.", name=printable(name), start=printable(start)), tr("Restart")):
            return
        if start and not values["restart"] and not ask_first(
                self, "mcp-attach", tr("Start {name}", name=printable(name)), where + tr(
                    "Give {start} the tools of the MCP server {name}?", start=printable(start), name=printable(name)),
                tr("Attach")):
            return
        background(lambda: self.sai.host_run(host, *args, timeout=300), owner=self, source=self.server_buttons,
                   work=True, timeout=330,
                   done=lambda result, error: self.server_done("start", result, error, quiet=not start))

    def server_done(self, action, result, error, quiet=True):
        status, out, err = outcome(result, error)
        if status != 0:
            warn(self, f"mcp {action}", (out + err).strip())
        elif not quiet:  # for a start: what it says (JSON: its message)
            try:
                said = json.loads(out).get("message") or out
            except (ValueError, AttributeError):
                said = out
            inform(self, f"mcp {action}", printable(str(said)).strip(), modal=False)
        self.refresh()

    def load_catalog(self, auto=False):
        """The catalogs come over the network: fetched in a thread (this machine's catalog module, or `sandbox-ai mcp
        catalog [--catalog SOURCE]` on the host chosen)."""
        self.catalog_requested = True
        host, source = self.host(), self.catalog_source.text().strip()
        try:
            backend.catalog_options([source] if source else [])
        except ValueError as exc:
            self.catalog_status.setText(printable(exc))
            return
        self.catalog_status.setText(tr("Loading the catalogs…"))
        background(self.sai.host_mcp_catalog, host, [source] if source else [], owner=self, source=self.load_button,
                   timeout=180, done=lambda result, error: self.catalog_loaded(result, error, auto, (host, source)))

    def catalog_loaded(self, result, error, auto=False, of=(LOCAL, "")):
        entries, problems = result if error is None else ([], [str(error)])
        self.catalog_of = of
        self.catalog_status.setText(tr("{count} entries", count=len(entries)) if not problems else
                                    "\n".join(printable(problem) for problem in problems))
        if auto:
            problems = []  # loaded by itself: said on the page, not in a dialog
        self.entries = {f"{prefix}:{e.get('name')}": e for prefix, e in entries}
        rows = sorted(self.entries.items())
        signatures = [(e.get("_signature") or {}).get("state", "unsigned") for _key, e in rows]
        fill(self.catalog, [[key, e.get("network", "") + (" " + ",".join(e.get("hosts", [])) if e.get("hosts") else ""),
                             e.get("title", ""), tr("yes") if e.get("digest") else tr("not yet"),
                             tr("signed") if signature == "good" else tr("signature bad") if signature == "bad" else
                             tr("unsigned"), e.get("_catalog_name") or key.partition(":")[0]]
                            for (key, e), signature in zip(rows, signatures)],
             tooltips=[None if signature == "good" else tr(
                 "Its catalog is not signed by a trusted key (sandbox/mcp-signers): sandbox-ai adds it only with "
                 "--unsigned.") if signature != "bad" else tr("Its catalog's signature does not verify: sandbox-ai "
                                                              "refuses it.") for signature in signatures])
        # the signature as a badge: only its cell in the colour of its state
        colors = {tr("signature bad"): "bad", tr("unsigned"): "warn", tr("signed"): "ok"}
        for row in range(self.catalog.rowCount()):
            item = self.catalog.item(row, 4)
            if item is not None and item.text() in colors:
                item.setForeground(QColor(theme.color(colors[item.text()])))
        if problems:
            warn(self, tr("Catalogs"), "\n".join(printable(problem) for problem in problems))

    def add(self):
        key = selected(self.catalog)
        entry = self.entries.get(key or "")
        if not entry:
            QMessageBox.information(self, tr("Add"), tr("Load the catalogs and select an entry first."))
            return
        host, catalog = self.catalog_of or (self.host(), "")
        dialog = AddServerDialog(self, entry, host, catalog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title = tr("Add {name}", name=printable(key)) if host == LOCAL else \
            tr("Add {name} on {host}", name=printable(key), host=printable(host))
        try:
            values = dialog.values(key)
            args, writable = backend.mcp_add_args(**values)
        except ValueError as exc:
            warn(self, title, printable(exc))
            return
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        if values["unsigned"] and not ask_first(self, "mcp-unsigned", title, where + tr(
                "Add {name} although its catalog is not signed by a trusted key? Nothing checked that the catalog is "
                "the one its publisher wrote.", name=printable(key)), tr("Add unsigned")):
            return
        if writable and not ask_first(self, "mcp-writable-mount", title, where + tr(
                "Let the MCP server {name} change these folders of its host: {list}?", name=printable(key),
                list=printable(", ".join(writable))), tr("Add")):
            return
        listed = [self.servers.item(r, 0).text() for r in range(self.servers.rowCount())] if host == self.host() else []
        if values["name_as"] in listed and not ask_first(self, "mcp-replace", title, where + tr(
                "An MCP server {name} is there already: replace its definition for every start that uses it?",
                name=printable(values["name_as"])), tr("Replace")):
            return
        self.run_add(host, key, values, args)

    def run_add(self, host, key, values, args):
        self.catalog_status.setText(tr("Adding {name}…", name=printable(key)))
        background(lambda: self.sai.host_run(host, *args, timeout=900), owner=self, work=True, source=self.add_button,
                   timeout=930, done=lambda result, error: self.added(host, key, values, result, error))

    def added(self, host, key, values, result, error):
        """What `mcp add` said: on success the servers are read again (it is in the list above then); a refusal says
        on the page that nothing was added, and one only for an unsigned catalog offers to add it anyway."""
        status, out, err = outcome(result, error)
        said = printable((out + err).strip())
        self.refresh()
        if status == 0:
            self.catalog_status.setText(tr("{name} added: it is in the list above; use it with --mcp {local}.",
                                           name=printable(key), local=printable(values["name_as"])))
            report(self, "mcp add", result, error)
            return
        refusal = None if values["unsigned"] else backend.mcp_unsigned_refusal(said)
        if refusal is None:
            self.catalog_status.setText(tr("{name} was not added.", name=printable(key)) + "\n" + said)
            warn(self, tr("{name} was not added", name=printable(key)), said, modal=False)
            return
        self.offer_unsigned(host, key, values, refusal, said)

    def offer_unsigned(self, host, key, values, refusal, said):
        """`mcp add` refused KEY only because its catalog is not signed by a trusted key: asked first (mcp-unsigned),
        it is added anyway with --unsigned (the human knows the catalog: one of their own, say)."""
        title = tr("Add {name}", name=printable(key)) if host == LOCAL else \
            tr("Add {name} on {host}", name=printable(key), host=printable(host))
        catalog = refusal.get("catalog") or (self.catalog_of or ("", ""))[1]
        if not catalog and host == LOCAL:
            try:
                catalog = dict(self.sai.module("mcp").CATALOGS).get(key.partition(":")[0] if ":" in key else "", "")
            except Exception:  # noqa: BLE001 - only the address for the question
                catalog = ""
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        question = tr("{name} was not added: its catalog {catalog} is not signed by a trusted key.",
                      name=printable(key), catalog=printable(catalog)) if catalog else \
            tr("{name} was not added: its catalog is not signed by a trusted key.", name=printable(key))
        question += " " + tr("Unsigned means that nobody vouched that the server is built from its pinned sources and "
                             "works with sandbox-ai; it runs in a sandbox of its own all the same.") + " " + \
            tr("Add {name} anyway (--unsigned)?", name=printable(key))
        if not ask_first(self, "mcp-unsigned", title, where + question, tr("Add unsigned"), details=said):
            self.catalog_status.setText(tr("{name} was not added: its catalog is not signed by a trusted key "
                                           "(Add… with --unsigned takes it anyway).", name=printable(key)))
            return
        try:
            args, _writable = backend.mcp_add_args(**{**values, "unsigned": True})
        except ValueError as exc:
            warn(self, title, printable(exc))
            return
        self.run_add(host, key, {**values, "unsigned": True}, args)


class PrivacyDialog(QDialog):
    """Privacy check…: what the next push of repositories would publish that should stay private (sandbox-ai
    privacy-check), on this machine or a host: the repositories (that host's paths), your own rules directory, the
    project's rule file, and whether every file counts (--all), not only the changed ones."""

    def __init__(self, parent, sai):
        super().__init__(parent)
        self.setWindowTitle(tr("Privacy check"))
        form = QFormLayout(self)
        self.host_choice = HostCombo(sai)
        self.host_choice.chosen.connect(lambda host: self.host_changed())
        form.addRow(tr("Host"), self.host_choice)
        self.paths = ListField(path=True)
        self.paths.setToolTip(tr("Git repositories, one per line (absolute paths on the host chosen)"))
        form.addRow(tr("Repositories"), self.paths)
        self.rules = PathField()
        self.rules.setToolTip(tr("Your own rules (default: the project's --privacy-rules, else "
                                 "~/.config/sandbox-ai/privacy if it exists)"))
        form.addRow(tr("Rules directory"), self.rules)
        self.project = QLineEdit()
        self.project.setPlaceholderText(tr("the repository folder's name"))
        self.project.setToolTip(tr("The name of the project rule file (--project)"))
        form.addRow(tr("Project rules"), self.project)
        self.every = QCheckBox(labels.shown(tr("Every tracked and untracked file, also unchanged ones (--all)")))
        form.addRow(self.every)
        self.note = plain_label(role="secondary")
        form.addRow(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.host_changed()

    def host_changed(self):
        host = self.host_choice.current()
        for field in (self.paths, self.rules):
            field.set_local(host == LOCAL)
        self.note.setText(tr("Paths on {host}: type them in (this computer's folders are not {host}'s).",
                             host=printable(host)) if host != LOCAL else "")

    def values(self):
        """(host, repositories, rules directory, project, every file)."""
        return (self.host_choice.current(), self.paths.value(), self.rules.value().strip(), self.project.text().strip(),
                self.every.isChecked())


PRIVACY_COLUMNS = (N_("Repository"), N_("Where"), N_("File"), N_("Rule"), N_("Severity"), N_("Reason"), N_("Line"))


def privacy_rows(results):
    """[[repository, where, file, rule, severity, reason, line]] of privacy-check --json: every finding, and a row
    for a repository without any."""
    rows = []
    for result in results:
        repository = printable(str(result.get("repository") or ""))
        findings = [f for f in result.get("findings") or [] if isinstance(f, dict)]
        if not findings:
            rows.append([repository, "", "", "", "", tr("nothing found ({count} files checked)",
                                                         count=result.get("scanned", 0)), ""])
        for finding in findings:
            rows.append([repository, *(printable(str(finding.get(key) if finding.get(key) is not None else ""))[:400]
                                       for key in ("where", "path", "id", "severity", "reason", "line"))])
    return rows


class PrivacyResults(QDialog):
    """The findings of a privacy check as a table (not modal); the repositories' notes and what was refused below."""

    def __init__(self, parent, title, results, problems):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 520)
        layout = QVBoxLayout(self)
        rows = privacy_rows(results)
        self.table = table([tr(column) for column in PRIVACY_COLUMNS], stretch=5, compact=(3, 4, 6))
        widgets.tables.keep(self.table, "dialog-privacy")
        fill(self.table, rows, ["bad" if row[4] == "block" else "warn" if row[4] else "ok" for row in rows])
        layout.addWidget(self.table, 1)
        notes = [printable(str(note)) for result in results for note in result.get("notes") or []][:20] + \
            [printable(line) for line in problems]
        self.notes = plain_label("\n".join(notes), role="secondary")
        self.notes.setVisible(bool(notes))
        layout.addWidget(self.notes)
        close = QDialogButtonBox()
        close.addButton(tr("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        close.rejected.connect(self.reject)
        layout.addWidget(close)


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
        self.details_button = QPushButton(tr("Details…"))
        self.details_button.setToolTip(tr("Where this sandbox-ai is installed, the keys its updates must be signed by, "
                                          "whether the policy demands it (sandbox-ai version --verbose)"))
        self.details_button.clicked.connect(lambda _=False: self.version_details())
        about_layout.addWidget(self.version, 1)
        about_layout.addWidget(self.update_text, 1)
        about_layout.addWidget(self.details_button)
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
        self.backend.setAccessibleName(tr("Backend"))
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
        build = QPushButton(tr("Build images"))
        build.clicked.connect(lambda _=False: self.tool(("build", "all")))
        update = self.update_sai_button = QPushButton(tr("Update sandbox-ai"))
        update.setToolTip(tr("sandbox-ai update in a terminal: the newest signed release, or the version given"))
        update.clicked.connect(lambda _=False: self.update_sandbox_ai())
        self.update_version = QLineEdit()
        self.update_version.setPlaceholderText(tr("X.Y.Z (empty: the newest)"))
        self.update_version.setToolTip(tr("Update to this version (sandbox-ai update --version), e.g. to go back"))
        self.update_version.setAccessibleName(tr("Version to update to"))
        self.update_version.setMaximumWidth(150)
        privacy = QPushButton(tr("Privacy check of a repository…"))
        privacy.clicked.connect(self.privacy)
        self.privacy_button = privacy
        # A row that wraps when the window is narrow (a page never needs more room than the window has).
        tools_layout.addWidget(flow(build, update, self.update_version, privacy), 1)
        layout.addWidget(tools)
        # The emulated serial device the hardware-analysis preset practises on (sandbox-ai demo-device), on a host too.
        demo = QGroupBox(tr("Emulated device (sandbox-ai demo-device)"))
        demo_layout = QHBoxLayout(demo)
        self.demo_host = HostCombo(sai)
        self.demo_port = QSpinBox()
        self.demo_port.setRange(1024, 65535)
        self.demo_port.setValue(DEMO_PORT)
        self.demo_port.setAccessibleName(tr("Port"))
        self.demo_buttons = {}
        for action, text, tip in (("start", tr("Start"), tr("Start the emulated serial device (on the port given)")),
                                  ("status", tr("Status"), tr("Whether it runs, and where")),
                                  ("stop", tr("Stop"), tr("Stop the emulated device"))):
            button = QPushButton(text)
            button.setToolTip(safe_tooltip(tip))
            button.setAccessibleName(tr("{action} the emulated device", action=text))
            button.clicked.connect(lambda _=False, a=action: self.demo_device(a))
            self.demo_buttons[action] = button
        self.demo_text = plain_label(role="secondary")
        demo_layout.addWidget(self.demo_host)
        demo_layout.addWidget(QLabel(tr("Port")))
        demo_layout.addWidget(self.demo_port)
        for button in self.demo_buttons.values():
            demo_layout.addWidget(button)
        demo_layout.addWidget(self.demo_text, 1)
        layout.addWidget(demo)
        settings = QGroupBox(tr("Settings of the app"))
        form = QFormLayout(settings)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)  # a long label above its field
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
        """Written or removed only here, when the human ticks the box. Before it is written, the folders sandboxes
        can write are read in the background (sandbox-ai's registry may ask Podman)."""
        if not on:
            return self.write_autostart(False, ())
        background(self.sai.sandbox_folders, owner=self, source=self.autostart_box, timeout=60,
                   done=lambda folders, error: self.write_autostart(True, folders, error))

    def write_autostart(self, on, forbidden, error=None):
        try:
            if error is not None:
                raise OSError(tr("Which folders sandboxes can write is not known ({error}): nothing is written.",
                                 error=printable(error)))
            backend.set_autostart(bool(on), forbidden=forbidden, also=[str(self.sai.directory)])
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
        background(self.sai.doctor, self.backend.currentText(), owner=self, work=True, source=self.check_button,
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

    def showEvent(self, event):  # noqa: N802
        self.demo_host.reload()  # a host added on the Hosts page meanwhile
        super().showEvent(event)

    def version_details(self):
        """sandbox-ai version --verbose: where it is installed, its release keys, the policy on signed updates."""
        background(self.sai.version_details, LOCAL, owner=self, source=self.details_button, timeout=90,
                   done=lambda result, error: show_text(self, tr("sandbox-ai version"),
                                                        printable("\n".join(outcome(result, error)[1:]).strip()),
                                                        modal=False))

    def update_sandbox_ai(self):
        """sandbox-ai update [--version X.Y.Z] in a terminal (its signed release; install.sh's installation)."""
        try:
            args = backend.update_args(self.update_version.text().strip() or None)
        except ValueError:
            warn(self, tr("Update sandbox-ai"), tr("A version is X.Y.Z, e.g. 0.1.76."))
            return
        self.tool(tuple(args))

    def demo_device(self, action):
        """sandbox-ai demo-device start|status|stop on the host chosen, in the background."""
        host, port = self.demo_host.current(), self.demo_port.value()
        background(self.sai.demo_device, host, action, port if action == "start" and port != DEMO_PORT else None,
                   owner=self, work=True, source=list(self.demo_buttons.values()), timeout=90,
                   done=lambda result, error: self.demo_done(host, result, error))

    def demo_done(self, host, result, error):
        status, out, err = outcome(result, error)
        said = printable((out + err).strip())[-300:]
        self.demo_text.setText(tr("{host}: {said}", host=printable(host), said=said) if host != LOCAL else said)

    def privacy(self):
        """Privacy check…: the repositories, rules and options asked for, checked on the host chosen (sandbox-ai
        privacy-check --json, in the background), the findings shown as a table."""
        dialog = PrivacyDialog(self, self.sai)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        host, paths, rules, project, every = dialog.values()
        try:
            backend.privacy_check_args(paths, rules or None, project or None, every)
        except ValueError as exc:
            warn(self, tr("Privacy check"), printable(exc))
            return
        title = tr("Privacy check: {path}", path=printable(", ".join(paths))[:200]) if host == LOCAL else \
            tr("Privacy check on {host}: {path}", host=printable(host), path=printable(", ".join(paths))[:200])
        background(self.sai.privacy_check, host, paths, rules or None, project or None, every, owner=self, work=True,
                   timeout=660, source=self.privacy_button,
                   done=lambda result, error: self.privacy_checked(title, result, error))

    def privacy_checked(self, title, result, error):
        from . import widgets
        if error is not None:
            show_text(self, title, printable(str(error)), modal=False)
            return
        results, problems = result
        self.privacy_dialog = PrivacyResults(self, title, results, problems)
        if widgets.AUTO_ANSWER is not None:  # tests: what would be shown is collected
            widgets.SHOWN.append((title, "\n".join("  ".join(row) for row in privacy_rows(results)), None))
            return
        widgets.show_modeless(self.privacy_dialog)


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
            (N_("Manage"), (("kits", N_("Kits & plugins")), ("mcp", N_("MCP servers")), ("llm", N_("LLM servers")),
                            ("images", N_("Images")), ("credentials", N_("Credentials")), ("team", N_("Teams")))),
            (N_("System"), (("system", N_("System")), ("hosts", N_("Hosts")), ("help", N_("Help")))))
PAGES = tuple(page for _, pages in SECTIONS for page in pages)
SHORTCUT_PAGES = 9
NOTIFY_AT_ONCE = 5  # new questions announced one by one at once; more are counted in one notification
NAV_WIDTH, COMPACT_NAV_WIDTH = 224, 52  # the navigation with titles, and as a strip of icons
NAV_MAX_WIDTH = 300  # the navigation with titles at most, however long a title is in the language chosen
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


class KnownProjects(QObject):
    """SandboxAI.known_projects() as last read, read again in the background (it reads sandbox-ai's registry, which
    may ask Podman, and the folders of sessions): the project switcher and the menus show at once what is known and
    change when a newer read differs (changed)."""
    changed = Signal()

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        self.sai, self.value, self.reading, self.again = sai, [], None, False

    def refresh(self):
        if self.reading is not None:
            self.again = True
            return
        self.again = False
        self.reading = background(self.sai.known_projects, timeout=60, done=self.read)

    def read(self, result, error):
        self.reading = None
        if error is None and list(result) != self.value:
            self.value = list(result)
            self.changed.emit()
        if self.again:
            self.refresh()


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
        # A child of the bar: a QObject of Python's own whose signal is connected to a lambda that refers to the bar
        # would keep both alive for good (the connection is held in C++, where the cycle collector does not look).
        self.known = KnownProjects(sai, self)
        self.known.changed.connect(lambda: self.fill(read=False))
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

    def fill(self, read=True):
        """The known projects (as last read; READ: read again, in the background), the current one selected; then
        the two actions."""
        if read:
            self.known.refresh()
        known = self.known.value
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
    # Keyed by its id (a session's directory, a run's registry name): a rename keeps the tab; NAME is what attach takes.
    ident = getattr(instance, "ident", None) if kind == "session" else instance.run
    ident = ident if backend.valid_run(ident) else name
    return {"key": f"{kind}:{ident}", "kind": kind, "name": name, "id": ident, "path": instance.project or "",
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
        ident = value.get("id") if backend.valid_run(value.get("id")) else name
        return {"key": f"{kind}:{ident}", "kind": kind, "name": name, "id": ident, "path": path, "agent": "",
                "backend": "", "state": "stopped"}
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
# The order the pages are made in. A lazy window (the app's start) makes FIRST_PAGES with itself - the first page, the
# Board and the open questions (their panels are the window's), what runs (the tabs follow it) - and the others after
# the first paint (LAZY_START ms after it: the first moments are the human's; in the tray: after LAZY_FALLBACK ms), or
# when they are first asked for: one at a time, LAZY_STEP ms apart, each once the mouse and the keyboard rested LAZY_QUIET ms and the other threads used less than LAZY_BUSY of
# a processor (waited for LAZY_PATIENCE steps at most).
BUILD_ORDER = ("launch", "board", "sandboxes", "activity", "approvals", "connections", "statistics", "files", "kits",
               "mcp", "llm", "team", "images", "credentials", "terminals", "system", "hosts", "help")
FIRST_PAGES = ("launch", "board", "sandboxes", "approvals")
LAZY_START, LAZY_STEP, LAZY_FALLBACK, LAZY_QUIET, LAZY_BUSY, LAZY_PATIENCE = 1000, 50, 3000, 400, 0.25, 20
PROJECT_PAGES = ("launch", "files", "approvals", "sandboxes", "kits", "statistics", "team")  # (set_project)
# The pages the current project (the tab chosen) changes - MainWindow.apply_project: the Start page's workspace, the
# Board, Sandboxes and Approvals (its rows and questions marked), Connections and Statistics (its logs), Files (its
# folder), Kits (what it uses), Teams (the workspace a new team is offered). On the others the tabs are hidden.
PROJECT_TAB_PAGES = ("launch", "board", "sandboxes", "approvals", "connections", "statistics", "files", "kits", "team")
SCROLL_MIN_HEIGHT = 220  # the height a scrolled page may shrink to before the window does
# The width a scrolled page may shrink to before the window does: a wider page scrolls sideways there instead of
# making the window wider than the screen (and Qt never shrinks a window back by itself).
SCROLL_MIN_WIDTH = 640


def make_llm_page(sai):
    from .llm_page import LlmPage
    return LlmPage(sai)


def make_team_page(sai, in_terminal):
    from .team_page import TeamPage
    return TeamPage(sai, in_terminal)


class PageScroll(QScrollArea):
    """A page that scrolls when the window is lower or narrower than the page needs, instead of squeezing its
    sections (Settings, Credentials, long forms) or cutting text off; it asks the window for at most
    SCROLL_MIN_HEIGHT x SCROLL_MIN_WIDTH, so that no page makes the window larger than the screen."""

    def __init__(self, page=None):
        super().__init__()
        self.setObjectName("pageScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        if page is not None:  # (a page made later is put in when it is made: MainWindow.build_page)
            self.setWidget(page)

    def minimumSizeHint(self):  # noqa: N802
        if self.widget() is None:
            return super().minimumSizeHint()
        hint = self.widget().minimumSizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        return QSize(min(hint.width() + bar, SCROLL_MIN_WIDTH), min(hint.height(), SCROLL_MIN_HEIGHT))

    def sizeHint(self):  # noqa: N802
        return self.widget().sizeHint() if self.widget() is not None else super().sizeHint()


class PageStack(QStackedWidget):
    """The pages: as small as the page shown allows (not the largest of all), so that the panels of a page fit
    next to it on a small screen. Most pages sit in a PageScroll; currentWidget() and setCurrentWidget() speak of the
    pages themselves."""

    def __init__(self):
        super().__init__()
        self.currentChanged.connect(self.shown)

    def shown(self, _index=None):
        """A page is shown: its combo boxes with long items (filled meanwhile) no wider than about 28 characters
        (tame_combos), and the window's minimum size that of this page."""
        page = self.currentWidget()
        if page is not None:
            tame_combos(page)
            widgets.name_views(page)
        self.updateGeometry()

    def add_page(self, page, scroll=True):
        return self.addWidget(PageScroll(page) if scroll else page)

    def add_holder(self, key):
        """An empty place for page KEY, made later (MainWindow.build_page): replace_holder puts it there."""
        holder = QWidget()
        holder.setObjectName(f"pageHolder-{key}")
        self.addWidget(holder)
        return holder

    def replace_holder(self, holder, frame):
        """FRAME (a page, or the PageScroll it is put in) in the place of HOLDER."""
        index = self.indexOf(holder)
        self.insertWidget(index, frame)
        if QStackedWidget.currentWidget(self) is holder:
            QStackedWidget.setCurrentWidget(self, frame)
        self.removeWidget(holder)
        holder.deleteLater()

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


class InputWatch(QObject):
    """When the human last used the mouse or the keyboard anywhere in the app (last: time.monotonic()), while it is
    installed (MainWindow.build_later: until every page is made)."""
    KINDS = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseMove, QEvent.Type.Wheel,
             QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate}

    def __init__(self, app):
        super().__init__()
        self.app, self.last = app, 0.0
        app.installEventFilter(self)

    def eventFilter(self, _watched, event):  # noqa: N802
        if event.type() in self.KINDS:
            self.last = time.monotonic()
        return False

    def pause(self):
        if self.app is not None:
            self.app.removeEventFilter(self)

    def resume(self):
        if self.app is not None:
            self.app.installEventFilter(self)

    def stop(self):
        if self.app is not None:
            self.app.removeEventFilter(self)
            self.app = None


class PanelDock(QDockWidget):
    """A panel of a page (View > Panels): closing it by its button is remembered (closed_by_user); hiding
    it because another page is shown is not."""
    closed_by_user = Signal()

    def closeEvent(self, event):  # noqa: N802
        super().closeEvent(event)
        self.closed_by_user.emit()


# (name, title, page, default area) of the panels that can be floated, docked elsewhere and closed.
# The inspector (properties) also folds to a strip while docked and unfolds when a part is clicked (dock_fold); the
# findings are closed until asked for.
DOCKS = (("parts", N_("Board: parts"), "board", Qt.DockWidgetArea.LeftDockWidgetArea),
         ("inspector", N_("Board: properties"), "board", Qt.DockWidgetArea.RightDockWidgetArea),
         ("findings", N_("Board: security score and findings"), "board", Qt.DockWidgetArea.BottomDockWidgetArea),
         ("questions", N_("Approvals: open questions"), "approvals", Qt.DockWidgetArea.TopDockWidgetArea))
CLOSED_AT_FIRST = {"findings"}  # panels closed until asked for (also when the saved state predates them)


class MainWindow(QMainWindow):
    """The window. With a system tray (and the setting, on by default), closing it hides it: the approvals
    are still polled and announced; Quit in the tray menu ends the app (with the usual questions)."""
    closed = Signal()  # the window really closed: the app ends (main connects it to quit)

    def __init__(self, sai, check_updates=True, approvals_interval=1000, risk=None, lazy=False):
        super().__init__()
        labels.set_flags_shown(backend.setting("show_flags"))  # before the forms that show them are made
        # quitting: SIGTERM or logout (no questions); leaving: a confirmed quit from the tray menu (no hiding);
        # finished: the window has closed for good (shutdown ran, exactly once).
        self.sai, self.quitting, self.leaving, self.finished, self.stopped = sai, False, False, False, False
        self.close_seen = False
        # What the window looked like when it was left (state.json); file dialogs remember their folders there.
        self.state = State(parent=self)
        if sai.hosts_supported():
            # hosts added, changed or removed outside the app (sandbox-ai hosts, the web interface): every host
            # chooser follows, as for a change on the Hosts page (review 6 M-H2); a file watch, never a poll
            from .host_choice import watch_hosts_file
            watch_hosts_file(sai.hosts_file())
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
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.compact_nav = False
        self.own_starts = []  # (agent, project folder, when) of sandboxes started in the app's terminal
        self.pages = PageStack()
        self.project = None
        # The pages: made by PAGE_MAKERS. Lazy (the app's start): the first page, the Board and the open questions
        # (their panels) and what runs (the tabs follow it) with the window, every other page after the first paint,
        # one at a time in idle time (build_later) - or at once, when anything asks for it first (__getattr__: the
        # window's self.PAGE builds PAGE). Not lazy (the tests): every page now, as always.
        self.lazy, self.ready, self.page_holders, self.idle_build = bool(lazy), False, {}, None
        self.page_makers = {
            "launch": lambda: LaunchPage(sai, self.in_terminal),
            "board": lambda: BoardPage(sai, self.in_terminal, sai.settings),
            "sandboxes": lambda: SandboxesPage(sai, self.in_terminal),
            "activity": lambda: ActivityPage(sai, self),
            "approvals": lambda: ApprovalsPage(sai, interval=approvals_interval
                                               and backend.setting("approvals_interval")),
            "connections": lambda: ConnectionsPage(sai),
            "statistics": lambda: StatisticsPage(sai),
            "files": lambda: FilesPage(sai, self.projects),
            "kits": lambda: KitsPage(sai, self.in_terminal),
            "mcp": lambda: McpPage(sai),
            "llm": lambda: make_llm_page(sai),  # the LLM servers of `sandbox-ai llm`, on this machine or a host
            "team": lambda: make_team_page(sai, self.in_terminal),  # the teams of `sandbox-ai team`, here or a host
            "images": lambda: ImagesPage(sai, self.in_terminal),
            "credentials": lambda: CredentialsPage(sai, self.in_terminal),
            "terminals": lambda: terminal.TerminalsPage(sai=sai, in_terminal=self.in_terminal),
            "system": lambda: SystemPage(sai, self.in_terminal),
            "hosts": lambda: HostsPage(sai, self.in_terminal),
            "help": lambda: help_view.HelpPage(),  # the Help area; every (?) of the app leads here
        }
        for key in FIRST_PAGES if self.lazy else BUILD_ORDER:
            self.build_page(key)
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
                item.setData(Qt.ItemDataRole.AccessibleTextRole, tr(title))  # also in the strip of icons
                self.nav_rows[key] = self.nav.count()
                self.nav.addItem(item)
                page = self.built(key)
                if page is not None:
                    self.pages.add_page(page, scroll=key not in UNSCROLLED_PAGES)
                else:  # made when it is first shown, or in idle time after the first paint
                    self.page_holders[key] = self.pages.add_holder(key)
                self.keys.append(key)
                if len(self.keys) <= SHORTCUT_PAGES:
                    item.setToolTip(tr("{page} (Ctrl+{number})", page=tr(title), number=len(self.keys)))
        self.nav.currentRowChanged.connect(self.nav_changed)
        self.nav.viewport().installEventFilter(self)  # a click on a section's header folds it
        self.nav.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.nav.customContextMenuRequested.connect(self.nav_menu)
        for key in BUILD_ORDER:  # the pages' signals to the window and to each other
            if self.built(key) is not None:
                self.link_page(key)
        panel = QWidget()
        panel.setObjectName("navPanel")
        side = QVBoxLayout(panel)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        self.brand = brand = QLabel("sandbox-ai")
        brand.setObjectName("brand")
        # The toggle between titles and the strip of icons: at the top, beside the name (alone in the strip).
        self.compact_button = QToolButton()
        self.compact_button.setObjectName("navCompact")
        self.compact_button.setAutoRaise(True)
        self.compact_button.clicked.connect(lambda: self.set_compact_nav(not self.compact_nav))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 4, 0)
        top.setSpacing(0)
        top.addWidget(brand, 1)
        top.addWidget(self.compact_button, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        side.addLayout(top)
        side.addWidget(self.nav, 1)
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
        self.project_bar.known.changed.connect(self.known_projects_changed)
        self.add_tab_button.setMenu(self.add_tab_menu)
        strip_layout.addWidget(self.tabs, 1)
        strip_layout.addWidget(self.add_tab_button)
        # Hidden on the pages the project does not change (PROJECT_TAB_PAGES), keeping their place: nothing below moves.
        for widget in (self.tabs, self.add_tab_button):
            policy = widget.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            widget.setSizePolicy(policy)
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
        # The (?)s of the pages' areas and controls, hover explanations (a page made later gets its own then).
        self.help_buttons = help_view.install(self, {key: page for key in BUILD_ORDER
                                                     if (page := self.built(key)) is not None})
        help_view.explain_windows(QApplication.instance())  # and those of the dialogs, when they are opened
        self.statusBar().showMessage(tr("sandbox-ai {version} from {path}", version=sai.version(), path=sai.directory))
        self.make_busy_indicator()
        self.tray, self.tray_timer = None, None
        self.session_timer = QTimer(self)
        self.session_timer.setSingleShot(True)
        self.session_timer.timeout.connect(self.session_goes_on)
        self.make_window_controls()
        self.make_menus()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.make_tray()
        else:
            self.show_tray_state()
        self.notifier = notifications.Notifier(self, self.tray)
        self.update_window_controls()
        self.update_check = AsyncCommand(timeout=60, parent=self)
        self.update_check.finished.connect(self.update_checked)
        if check_updates:
            QTimer.singleShot(1500, self.check_updates)
        self.sandboxes_here = 0
        self.questions_changed(self.approvals.open.rowCount(), [])
        self.nav.setCurrentRow(self.nav_rows["launch"])
        # What runs is known with the first read of the Sandboxes page (in the background): what starts after that
        # opens a tab of its own (setting auto_tabs); what ran already at the first read does not (sync_tabs).
        self.seen_running = None
        self.register_state()
        self.ready = True  # a page made from now on is linked, explained and kept at once (page_ready)
        self.restore_tabs()
        self.restore_docks()
        self.restore_window()
        self.show_project_tabs(self.current_page())
        if self.lazy:  # the other pages: after the first paint (eventFilter), else (a start in the tray) after a while
            QTimer.singleShot(LAZY_FALLBACK, self.build_later)

    # -- pages --

    def nav_item(self, key):
        return self.nav.item(self.nav_rows[key])

    def built(self, key):
        """Page KEY if it was made already, else None (never makes it: for what only concerns the pages that exist)."""
        return self.__dict__.get(key)

    def __getattr__(self, name):
        """self.PAGE: the page, made now if it was not made yet (lazy start); every other unknown name is an error."""
        makers = self.__dict__.get("page_makers")
        if makers is None or name not in makers:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        return self.build_page(name)

    def build_page(self, key):
        """Make page KEY (once): in its place in the stack of pages, and - once the window is ready - linked with the
        window and the other pages, its (?)s placed, its tables and filters kept, the current project shown."""
        page = self.built(key)
        if page is not None:
            return page
        begin = time.perf_counter()
        holder = self.page_holders.get(key)
        if holder is not None and key not in UNSCROLLED_PAGES and not isinstance(holder, PageScroll):
            # Its frame in its place first: under the app's style sheet a widget is styled again each time it is put
            # into another, so the page is put in once, into its place in the window.
            frame = PageScroll()
            self.pages.replace_holder(holder, frame)
            holder = self.page_holders[key] = frame
        try:
            page = self.page_makers[key]()
        except AttributeError as exc:  # (not "no such page", which __getattr__ would make of it)
            raise RuntimeError(f"page {key}: {exc}") from exc
        setattr(self, key, page)
        self.page_holders.pop(key, None)
        if isinstance(holder, PageScroll):
            holder.setWidget(page)
        elif holder is not None:
            self.pages.replace_holder(holder, page)
        if self.ready:
            self.page_ready(key)
        applog.debug("page built", key=key, ms=round((time.perf_counter() - begin) * 1000))
        return page

    def page_ready(self, key):
        """A page made after the window: what the window did for the pages it was made with."""
        page = getattr(self, key)
        self.link_page(key, late=True)
        self.help_buttons.update(help_view.install(self, {key: page}, tree=page))
        self.register_page_state(key)
        if self.project:
            self.show_project_in(key, self.project)

    def build_later(self):
        """Lazy start: the pages not made yet, one at a time (in BUILD_ORDER), each when the human has not used the
        mouse or the keyboard for LAZY_QUIET ms (a page takes up to about a tenth of a second: never under the hand)."""
        if self.idle_build or not self.lazy or getattr(self, "shut_down", False):
            return
        self.idle_build, self.idle_waits = QTimer(self), 0
        self.idle_build.setSingleShot(True)
        self.idle_build.setInterval(LAZY_STEP)
        self.idle_build.timeout.connect(self.build_next)
        self.input_watch = InputWatch(QApplication.instance())
        self.idle_build.start()

    def build_next(self):
        key = next((key for key in BUILD_ORDER if self.built(key) is None), None)
        if key is None or getattr(self, "shut_down", False):
            self.input_watch.stop()
            return
        now = time.monotonic()
        if now - self.input_watch.last < LAZY_QUIET / 1000:
            self.idle_build.start()
            return
        # And while the rest of the app idles: the reads of the pages made so far (other threads) would share Python
        # with the build and lengthen it; they are waited for LAZY_PATIENCE steps at most.
        others = time.process_time() - time.thread_time()
        mark, self.idle_mark = getattr(self, "idle_mark", None), (others, now)
        if mark is not None and now > mark[1] and (others - mark[0]) / (now - mark[1]) > LAZY_BUSY \
                and self.idle_waits < LAZY_PATIENCE:
            self.idle_waits += 1
            self.idle_build.start()
            return
        self.idle_waits = 0
        # (Not watched while it is made: the thousands of events a page's widgets get as they are made, each through
        # a filter in Python, made it take about twice as long - and no input is handled meanwhile anyway.)
        self.input_watch.pause()
        try:
            self.build_page(key)
        finally:
            self.input_watch.resume()
        if all(self.built(key) is not None for key in BUILD_ORDER):
            self.input_watch.stop()  # (the last one: nothing more to wait for)
            return
        self.idle_mark = (time.process_time() - time.thread_time(), time.monotonic())
        self.idle_build.start()

    def build_pages(self):
        """Every page now (a test of a lazy window that needs them all)."""
        for key in BUILD_ORDER:
            self.build_page(key)

    def link_page(self, key, late=False):
        """The signals of page KEY to the window and to the other pages; LATE (made after the window): what the
        others said meanwhile, too."""
        page = getattr(self, key)
        if key == "help":
            page.tour_requested.connect(self.start_any_tour)
        elif key == "launch":
            page.opened.connect(self.project_opened)
        elif key == "sandboxes":
            page.open_log.connect(self.show_log)
            page.open_remote_log.connect(self.show_remote_log)
            page.counted.connect(lambda running, here: self.update_activity(sandboxes=here))
            page.updated.connect(self.sync_tabs)
            # The fleet: the Hosts page lists what the Sandboxes and Approvals pages last saw of every host; the
            # Approvals page asks only the hosts where something runs for questions.
            page.fleet_changed.connect(self.fleet_changed)
            page.started_on.connect(lambda host: self.approvals.wake_hosts([host]))
        elif key == "approvals":
            page.pending_changed.connect(self.questions_changed)
            sandboxes = self.built("sandboxes")
            if sandboxes is not None and getattr(sandboxes, "loaded", False):
                page.set_fleet(sandboxes.fleet())
        elif key == "hosts":
            # ... and leads to them filtered by a host; a host removed there leaves both at once.
            page.host_removed.connect(lambda name: self.sandboxes.forget_host(name))
            page.host_removed.connect(lambda name: self.approvals.forget_host(name))
            page.show_starts.connect(lambda host: (self.sandboxes.show_host(host), self.show_page("sandboxes")))
            page.show_questions.connect(lambda host: (self.approvals.show_host(host), self.show_page("approvals")))
            page.refresh_requested.connect(lambda: (self.sandboxes.refresh_remote(), self.approvals.poll_remote()))
            if late and getattr(self.sandboxes, "loaded", False):
                page.set_fleet(self.sandboxes.fleet())
            if late:
                self.show_asking()
        elif key == "kits":
            page.enable_kit.connect(self.enable_kit)
            page.inventory_loaded.connect(lambda *found: self.board.set_kit_inventory(*found))
        elif key == "board":
            page.opened.connect(self.project_opened)
            page.open_request = self.choose_project_folder
            page.project_request = self.set_project  # a project folder dropped on the board opens in its tab
            page.inspector_request = lambda: self.reopen_panel("inspector")  # a part selected: its inspector
            page.score_request = lambda shown: self.set_panel("findings", shown)  # the score's button
        elif key == "system":
            page.update_button.clicked.connect(self.check_updates)
            page.settings_button.clicked.connect(lambda: self.open_settings())
            page.policy_button.clicked.connect(lambda: self.open_settings(policy=True))
            page.theme_changed.connect(lambda _mode: self.check_theme_action())
            if late:
                self.show_tray_state()
                self.check_theme_action()
                if getattr(self, "update_said", None):
                    page.update_text.setText(self.update_said)

    def fleet_changed(self, fleet):
        hosts = self.built("hosts")  # (one made later asks the Sandboxes page then)
        if hosts is not None:
            hosts.set_fleet(fleet)
        approvals = self.built("approvals")
        if approvals is not None:
            approvals.set_fleet(fleet)

    def show_project_in(self, key, path):
        """The current project PATH on page KEY (apply_project, and a page made later)."""
        page = self.built(key)
        if page is None:
            return
        if key == "board":
            page.activate(path)
        elif key == "connections":
            page.open_for_workspace(path)
        elif key in PROJECT_PAGES:
            page.set_project(path)

    def nav_changed(self, row):
        item = self.nav.item(row)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if key in self.nav_rows:
            applog.debug("page", key=key)
            if hasattr(self, "docks"):
                # the panels of the page left go first: shown beside the next page's minimum for that moment, they
                # would widen the window for good (Qt grows it at once; review 6 N1, the Board's two panels)
                self.put_away_docks(key)
            self.pages.setCurrentWidget(getattr(self, key))
            if hasattr(self, "docks"):
                self.page_changed(key)

    def current_page(self):
        widget = self.pages.currentWidget()
        return next((key for key in self.keys if self.built(key) is widget and widget is not None), None)

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
        # Settings: the app's own (themes, intervals, notifications, the agents' defaults ...) and the
        # administrator's policy, from the menu bar as well as from the System page.
        settings_menu = bar.addMenu(tr("&Settings"))
        action(settings_menu, "settings", tr("Settings…"), lambda: self.open_settings(), ["Ctrl+,"])
        action(settings_menu, "policy", tr("Administrator policy…"), lambda: self.open_settings(policy=True))
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
        for name, dock in getattr(self, "docks", {}).items():  # a control on a closed panel: the panel opens
            if (name in self.closed_docks or name == "inspector" and self.inspector_fold.is_folded()) \
                    and dock.widget() is not None and (
                    dock.widget() is widget or dock.widget().isAncestorOf(widget)):
                self.set_panel(name, True)
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
        self.show_project_tabs(key)
        if key in help_view.H.PAGE_SECTIONS and hasattr(self, "page_help"):
            self.page_help.set_tip(f"page.{key}")
        if hasattr(self, "help_buttons") and isinstance(getattr(self, key, None), QWidget):
            help_view.explain_tree(getattr(self, key))  # what the page made since (it fills some parts later)
        for name, keys in getattr(self, "terminal_keys", {}).items():
            self.actions[name].setShortcuts([] if key == "terminals" else keys)

    def show_project_tabs(self, key):
        """The project tabs only where the current project changes what the page shows or does (PROJECT_TAB_PAGES)."""
        if hasattr(self, "tabs"):
            shown = key in PROJECT_TAB_PAGES
            self.tabs.setVisible(shown)
            self.add_tab_button.setVisible(shown)

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
            self.actions["tray"].setToolTip(safe_tooltip(tray_why or tr("Hide to the system tray: sandbox-ai keeps answering there")))

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
        system = self.built("system")
        if system is not None:
            current = system.theme.currentData()
        else:  # (as the System page will choose it)
            current, keys = self.sai.settings().get("theme", "system"), [key for key, _ in theme.choices()]
            current = current if current in keys else keys[0]
        for key, item in getattr(self, "theme_actions", {}).items():
            item.setChecked(key == current)

    def known_projects_changed(self):
        """A newer read of the known projects: a menu open now lists them."""
        if self.add_tab_menu.isVisible():
            self.fill_add_tab_menu(read=False)
        if getattr(self, "switch_menu", None) is not None and self.switch_menu.isVisible():
            self.fill_switch_menu(read=False)

    def fill_switch_menu(self, read=True):
        self.switch_menu.clear()
        if read:
            self.project_bar.known.refresh()  # an open menu is filled again when a newer read differs
        known = self.project_bar.known.value
        group = QActionGroup(self.switch_menu)  # one current project: a radio choice
        group.setExclusive(True)
        for path, _ in known:
            item = self.switch_menu.addAction(menu_text(f"{Path(path).name or path}  —  {middle(path, 60)}"))
            item.setCheckable(True)
            item.setChecked(same_folder(path, self.project))
            group.addAction(item)
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
        the window itself: all come back at the next start (a page made later: register_page_state, then)."""
        s = self.state
        for key in BUILD_ORDER:
            if self.built(key) is not None:
                self.register_page_state(key)
        for section in s.get("nav_collapsed", []):
            if section in self.section_rows:
                self.toggle_section(section, True)
        self.set_compact_nav(s.get("nav_compact", False) is True)
        s.collect(self.keep_window)

    def register_page_state(self, key):
        """What register_state keeps of page KEY."""
        s, page = self.state, getattr(self, key)
        tables = {"sandboxes": [("sandboxes", "table")], "connections": [("connections", "table")],
                  "approvals": [("approvals-open", "open"), ("approvals-history", "history"),
                                ("approvals-always", "always")],
                  "files": [("files", "table")],
                  "kits": [("kits", "table"), ("kits-marketplaces", "market"), ("kits-plugins", "plugins")],
                  "mcp": [("mcp", "servers"), ("mcp-catalog", "catalog")],
                  "images": [("images", "images"), ("images-packages", "packages")],
                  "credentials": [("credentials-logins", "logins"), ("credentials-secrets", "secrets")]}
        kept = [(name, getattr(page, attribute)) for name, attribute in tables.get(key, ())]
        if key == "statistics":
            kept += [(f"statistics-{name}", widget) for name, widget in page.tables.items()]
        for name, widget in kept:
            s.table(name, widget)
        from . import tables as fitted
        fitted.keep_tables(s, {key: page})  # every other table, as PAGE-NAME
        if key == "files":
            saved = s.section("splitters").get("files")
            page.keep_split = isinstance(saved, list) and len(saved) == page.split.count()  # kept: it wins
        splitters = {"approvals": [("approvals", "split")], "connections": [("connections", "split")],
                     "kits": [("kits", "split"), ("kits-marketplaces", "market_split")], "files": [("files", "split")]}
        for name, attribute in splitters.get(key, ()):
            s.splitter(name, getattr(page, attribute))
        if key == "connections":
            s.text("connections-filter", page.filter)
            s.check("connections-refused", page.refused)
            s.check("connections-follow", page.follow)
        elif key == "images":
            s.text("images-filter", page.filter)
        elif key == "sandboxes":
            s.check("sandboxes-only-project", page.only_project)
        if key in ("connections", "statistics"):
            name = f"{key}-folder"
            folder = s.get(name, "")
            if folder and os.path.isabs(folder):
                page.refresh_directories(select=folder)
            page.directory.currentIndexChanged.connect(
                lambda _i, k=name, p=page: s.set(k, p.directory.currentData() or ""))
        if key in ("launch", "sandboxes", "board"):
            page.attach_state(s)

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
        if event.type() == QEvent.Type.Paint and self.__dict__.get("idle_build", 0) is None and self.lazy \
                and self.ready:
            # The window's first paint (the navigation is part of it): the other pages are made from a moment on.
            self.idle_build = False
            QTimer.singleShot(LAZY_START, self.build_later)
            QTimer.singleShot(0, lambda: alive(self) and self.approvals.first_scan())  # (see there)
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
        """Fold (FOLDED) or unfold everything that folds: every section of the navigation (the current page's
        section stays open; in the strip of icons, which shows every page, it is kept for when the titles come back)
        and every tree of the page shown (the kits and what they bring, the doctor's checks, the canaries, the hosts and
        starts of the Sandboxes page, which keeps it), which collapse to their top level or open all the way. Said in
        the status bar."""
        current = self.current_page()
        for section, (_, rows) in self.section_rows.items():
            here = any(self.nav.item(row).data(Qt.ItemDataRole.UserRole) == current for row in rows)
            self.toggle_section(section, folded and not here)
        page = getattr(self, current, None) if current else None
        trees = [tree for tree in (page.findChildren(QTreeView) if isinstance(page, QWidget) else [])
                 if tree.isVisibleTo(page)]
        for tree in trees:
            if folded:
                tree.collapseAll()
            else:
                tree.expandAll()
        said = tr("Sections folded") if folded else tr("Sections unfolded")
        if trees:
            said += " · " + tr("the lists of this page too")
        if self.compact_nav:
            said += " · " + tr("the strip of icons shows every page: Only icons off shows the sections")
        self.statusBar().showMessage(said, 5000)

    def section_of(self, key):
        return next((section for section, (_, rows) in self.section_rows.items() if self.nav_rows.get(key) in rows),
                    None)

    def set_compact_nav(self, compact):
        """The navigation as a narrow strip of icons (each page's title in its tooltip), or with titles and
        sections; kept for the next start. Saves room on a small screen."""
        self.compact_nav = bool(compact)
        # The strip's own paddings (theme.COMPACT_NAV): the icons fit in its narrow rows, in every theme.
        self.nav.setProperty("compact", "true" if self.compact_nav else "false")
        self.nav.style().unpolish(self.nav)
        self.nav.style().polish(self.nav)
        # A re-polish reads the ::item rules again but not the list's own padding (its contents margins): the strip
        # kept the titles' side padding, its viewport 40 px wide, the icons cut. A style change reads that too.
        QApplication.sendEvent(self.nav, QEvent(QEvent.Type.StyleChange))
        self.nav.setIconSize(QSize(24, 24) if self.compact_nav else QSize(20, 20))
        for key, row in self.nav_rows.items():
            item = self.nav.item(row)
            item.setText("" if self.compact_nav else self.nav_title(key))
            item.setIcon(theme.glyph(key, size=24 if self.compact_nav else 20))  # never an empty icon
            item.setToolTip(self.nav_tooltip(key))  # the app's own words
            item.setData(Qt.ItemDataRole.AccessibleTextRole, self.nav_title(key))  # a strip of icons has no text
        self.show_question_count()
        for section, (header, rows) in self.section_rows.items():
            self.nav.setRowHidden(header, self.compact_nav)
            for row in rows:
                self.nav.setRowHidden(row, not self.compact_nav and section in self.collapsed)
        self.fit_nav_width()
        self.brand.setVisible(not self.compact_nav)
        self.compact_button.setText("»" if self.compact_nav else "«")
        self.compact_button.setToolTip(safe_tooltip(tr("Show the page titles") if self.compact_nav
                                       else tr("Only icons: more room for the pages")))
        if "nav-compact" in getattr(self, "actions", {}):
            self.actions["nav-compact"].setChecked(self.compact_nav)
        self.state.set("nav_compact", self.compact_nav)

    def fit_nav_width(self):
        """The strip of icons is COMPACT_NAV_WIDTH inside the theme's own frame (a border or margin of the
        navigation, e.g. win311, amber, fallout): its rows keep the room their icons need in every theme."""
        margins = self.nav.contentsMargins()
        if self.compact_nav:
            self.nav.setFixedWidth(COMPACT_NAV_WIDTH + margins.left() + margins.right())
        else:
            # Wide enough for the longest title in the language chosen (German "Sandboxen & Sitzungen" was cut off),
            # at most NAV_MAX_WIDTH.
            needed = self.nav.sizeHintForColumn(0) + margins.left() + margins.right() + 2 * self.nav.frameWidth() \
                + self.nav.verticalScrollBar().sizeHint().width()
            self.nav.setFixedWidth(max(NAV_WIDTH, min(needed, NAV_MAX_WIDTH)))

    def nav_title(self, key):
        return tr(next(title for _, pages in SECTIONS for k, title in pages if k == key))

    def nav_tooltip(self, key):
        """The page's title (in the strip of icons), or its title and shortcut; below it what the page is for, in a
        sentence (its help text: it stays when the (?)s are off)."""
        index = self.keys.index(key)
        if self.compact_nav or index >= SHORTCUT_PAGES:
            title = self.nav_title(key)
        else:
            title = tr("{page} (Ctrl+{number})", page=self.nav_title(key), number=index + 1)
        about = help_view.summary(f"page.{key}") if f"page.{key}" in help_view.H.TIPS else ""
        return "\n".join([title, *textwrap.wrap(about, 60)])

    def nav_menu(self, position):
        menu = QMenu(self)
        compact = menu.addAction(tr("Only icons"), lambda: self.set_compact_nav(not self.compact_nav))
        compact.setCheckable(True)
        compact.setChecked(self.compact_nav)
        menu.addSeparator()
        menu.addAction(tr("Fold all sections"), lambda: self.fold_all(True))
        menu.addAction(tr("Unfold all sections"), lambda: self.fold_all(False))
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
        widgets = {"parts": self.board.parts_panel, "inspector": self.board.side, "findings": self.board.findings_panel,
                   "questions": self.approvals.open_box}
        self.docks, self.closed_docks = {}, set(CLOSED_AT_FIRST)
        for name, title, _, area in DOCKS:
            dock = PanelDock(tr(title), self.workspace)
            dock.setObjectName(f"dock-{name}")
            if name in widgets:
                dock.setWidget(widgets[name])
                tip = widgets[name].toolTip()  # escaped when it was set (safe_tooltip)
                dock.setToolTip(tip)  # its title explains it on hover (the parts: how to add)
            dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                             | QDockWidget.DockWidgetFeature.DockWidgetMovable
                             | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
            self.workspace.addDockWidget(area, dock)
            dock.closed_by_user.connect(lambda n=name: self.panel_closed(n))
            dock.topLevelChanged.connect(lambda _floating: self.docks_changed())
            dock.dockLocationChanged.connect(lambda _area: self.docks_changed())
            self.docks[name] = dock
        self.board.palette.setMinimumWidth(150)
        self.board.side.setMinimumWidth(240)
        # The inspector: folded to a strip at first; a click on a part unfolds it (or shows it, closed), Esc or a
        # click on the empty canvas fold it again while it is docked.
        self.inspector_fold = dock_fold.DockFold(self.docks["inspector"], self.workspace, folded=True)
        self.inspector_fold.changed.connect(lambda _folded: self.docks_changed())
        self.board.fold_request = self.fold_inspector
        self.workspace.resizeDocks([self.docks["parts"]], [170], Qt.Orientation.Horizontal)
        self.default_docks = self.workspace.saveState()

    def fold_inspector(self):
        """Esc or a click on the Board's empty canvas: its inspector folds if it is docked, shown and unfolded (never
        a floating one). True if it folded."""
        dock = self.docks["inspector"]
        if dock.isFloating() or not dock.isVisible() or self.inspector_fold.is_folded():
            return False
        self.inspector_fold.fold()
        return True

    def put_away_docks(self, key):
        """Hide the docked (not floating) panels that are not of the page KEY, before it is shown."""
        for name, _, dock_page, _ in DOCKS:
            dock = self.docks[name]
            if dock_page != key and not dock.isFloating() and not dock.isHidden():
                dock.setVisible(False)

    def update_docks(self):
        page = self.current_page()
        for name, _, dock_page, _ in DOCKS:
            dock = self.docks[name]
            wanted = name not in self.closed_docks and (dock.isFloating() or dock_page == page)
            if dock.isHidden() == wanted:
                dock.setVisible(wanted)
            if hasattr(self, "panel_actions"):
                self.panel_actions[name].setChecked(name not in self.closed_docks)
        self.board.score_button.setChecked("findings" not in self.closed_docks)

    def panel_closed(self, name):
        if self.current_page() == dict((n, p) for n, _, p, _ in DOCKS)[name] or self.docks[name].isFloating():
            self.closed_docks.add(name)
        self.update_docks()
        self.save_docks()

    def toggle_panel(self, name):
        """View > Panels: bring a closed panel back (on its page) or close it."""
        self.set_panel(name, self.panel_actions[name].isChecked())

    def set_panel(self, name, shown):
        """Show the panel NAME (on its page, unfolded) or close it."""
        page = dict((n, p) for n, _, p, _ in DOCKS)[name]
        if shown:
            self.closed_docks.discard(name)
            if name == "inspector":
                self.inspector_fold.unfold()
            if not self.docks[name].isFloating() and self.current_page() != page:
                self.show_page(page)
        else:
            self.closed_docks.add(name)
        self.update_docks()
        self.save_docks()

    def reopen_panel(self, name):
        """Show the panel NAME again where it was (floating or docked) if it was closed, unfold it if it was folded,
        and raise it; the keyboard focus stays where it is (the Board's canvas, when a part was selected there)."""
        if not hasattr(self, "docks") or name not in self.docks:
            return
        dock, focus = self.docks[name], QApplication.focusWidget()
        if name in self.closed_docks:
            self.closed_docks.discard(name)
            dock.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)  # a floating one: not activated
            try:
                self.update_docks()
            finally:
                dock.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            self.save_docks()
            if focus is not None:  # also once the window system has shown (and maybe activated) a floating panel
                self.keep_focus(focus)
                QTimer.singleShot(50, lambda: self.keep_focus(focus))
        if name == "inspector":
            self.inspector_fold.unfold()
        if dock.isVisible():
            dock.raise_()

    @staticmethod
    def keep_focus(widget):
        try:
            if QApplication.focusWidget() is not widget and widget.isVisible():
                widget.activateWindow()
                widget.setFocus(Qt.FocusReason.OtherFocusReason)
        except RuntimeError:  # gone meanwhile
            pass

    def docks_changed(self):
        self.update_docks()
        self.save_docks()

    def reset_docks(self):
        self.closed_docks.clear()
        self.closed_docks.update(CLOSED_AT_FIRST)
        for dock in self.docks.values():
            dock.setFloating(False)
        self.inspector_fold.folded = True  # (as at the first start)
        self.workspace.restoreState(self.default_docks)
        self.inspector_fold.apply()
        self.update_docks()
        self.save_docks()

    def save_docks(self):
        if getattr(self, "restoring_docks", False):
            return
        fold = getattr(self, "inspector_fold", None)
        if fold is None:  # (still being made)
            return
        self.state.set("docks", {"state": bytes(self.workspace.saveState().toBase64()).decode(),
                                 "closed": sorted(self.closed_docks), "known": sorted(self.docks),
                                 "folded": ["inspector"] if fold.folded else [], "sizes": {"inspector": fold.sizes}})

    def restore_docks(self):
        """Where the panels were at the last start (QMainWindow.saveState, kept in the app's state file)."""
        saved = self.state.get("docks", {})
        state, closed = saved.get("state"), saved.get("closed")
        self.restoring_docks = True
        try:
            if isinstance(state, str) and len(state) < 65536:
                self.workspace.restoreState(QByteArray.fromBase64(state.encode()))
            if isinstance(closed, list):
                known = saved.get("known") if isinstance(saved.get("known"), list) else []
                self.closed_docks = {n for n in closed if n in self.docks} | (CLOSED_AT_FIRST - set(known))
            fold, sizes = self.inspector_fold, saved.get("sizes")
            if isinstance(saved.get("folded"), list):  # (else folded, as at the first start)
                fold.folded = "inspector" in saved["folded"] and not self.docks["inspector"].isFloating()
            if isinstance(sizes, dict) and isinstance(sizes.get("inspector"), dict):
                for key in ("width", "height"):
                    value = sizes["inspector"].get(key)
                    if isinstance(value, int) and 80 <= value <= 4000:
                        fold.sizes[key] = value
            fold.apply()
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
        if key == "show_flags":  # the options (--…) beside the fields' names: the Start page's form, the board's
            labels.set_flags_shown(backend.setting("show_flags"))
            for form in self.findChildren(OptionsForm):
                form.show_flags()
            self.board.inspect_timer.start()  # its inspector is made again
            return
        pages = {"approvals_interval": "approvals", "sandboxes_interval": "sandboxes",
                 "connections_interval": "connections"}
        page = self.built(pages.get(key, ""))  # (one made later reads the setting then)
        if page is not None and page.timer.isActive():
            page.timer.setInterval(backend.setting(key))

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
            self.show_project_in("kits", self.project)  # (one made later shows it then)

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
        for key in ("board", *PROJECT_PAGES, "connections"):  # those made yet; one made later shows it then
            self.show_project_in(key, path)
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
            if not ask_first(self, "tab-close", tr("Close tab"), tr("The board of {path} has changes that are not "
                                                                    "applied. Close the tab and drop them?",
                                                                    path=printable(path)),
                             tr("Close and drop them"), reject=tr("Keep the tab")):
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
        self.state.set("tabs", [{k: e[k] for k in ("kind", "path", "name", "id") if e.get(k)} for e in entries])
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
        closed); a session or sandbox that started since the last look opens a tab (setting auto_tabs). Before the
        Sandboxes page's first read (in the background) nothing is known: no tab counts as stopped yet."""
        loaded = getattr(self.sandboxes, "loaded", True)
        for index in range(self.tabs.count()):
            entry = self.tabs.entry(index)
            if entry and entry["kind"] in ("session", "run"):
                instance = instances.get(entry["key"])
                if instance is not None:
                    entry.update(instance_entry(instance))
                elif loaded:
                    entry["state"] = "stopped"
                self.tabs.set_entry(index, entry)
        if not loaded:
            return
        running = {key for key, i in instances.items() if i.kind in ("session", "run") and i.state == "running"}
        seen = getattr(self, "seen_running", set())
        if open_new and seen is not None and backend.setting("auto_tabs"):  # (None: the first read, what ran already)
            for key in sorted(running - seen):
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

    def fill_add_tab_menu(self, read=True):
        """"+": the known projects, the sessions and sandboxes of the account not open yet, Open folder…, New
        project…."""
        menu = self.add_tab_menu
        menu.clear()
        menu.addSection(tr("Projects"))
        if read:
            self.project_bar.known.refresh()
        known = self.project_bar.known.value
        for path, _ in known:
            # An action that opens (or shows) the project's tab, not a toggle: an open one says so in its text.
            shown = self.tabs.find(project_entry(path)["key"]) >= 0
            action = menu.addAction(menu_text(f"{Path(path).name or path}  —  {middle(path, 60)}" +
                                              (" " + tr("(open)") if shown else "")))
            action.triggered.connect(lambda _=False, p=path: self.set_project(p))
        waiting = [i for i in self.sandboxes.instances.values() if i.kind in ("session", "run")
                   and getattr(i, "host", "local") == "local" and self.tabs.find(i.key) < 0]
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

    def show_remote_log(self, host, source):
        self.connections.open_remote(host, source)
        self.show_page("connections")

    def projects(self):
        """The "current project" folders for the file browser: the current project (else the folder the app
        was started in) and the Start page's first workspace when it is another one."""
        project = getattr(self, "project", None)
        # Without a project chosen the folder the app was started in, named as that (not "Current project": the
        # window says it has none).
        found = [("project", "", project) if project else ("started-in", "", os.getcwd())]
        workspace = self.launch.workspace()
        if workspace and not same_folder(workspace, found[0][2]):
            found.append(("project", "", workspace))
        return found

    # -- approvals --

    def show_question_count(self):
        """The open questions of every host on the Approvals entry (its text, or in the strip of icons its tooltip
        and accessible name) and on the tray icon."""
        if not getattr(self, "nav_rows", None):
            return
        count = getattr(self, "pending_count", 0)
        item = self.nav_item("approvals")
        item.setText("" if self.compact_nav else tr("Approvals") + (f"  ({count})" if count else ""))
        # In the strip of icons the count is a badge on the icon (it has no text to put it in).
        size = 24 if self.compact_nav else 20
        item.setIcon(theme.counted_glyph("approvals", count, size) if count and self.compact_nav else
                     theme.glyph("approvals", size=size))
        said = tr("{count} open questions, on this machine and every host", count=count) if count else ""
        item.setToolTip(self.nav_tooltip("approvals") + (f"\n{said}" if said else ""))
        item.setData(Qt.ItemDataRole.AccessibleTextRole, tr("Approvals") + (f": {said}" if said else ""))
        font = QFont(item.font())
        font.setBold(bool(count))
        item.setFont(font)
        if count:
            item.setForeground(QBrush(QColor(theme.color("bad"))))
        else:
            item.setData(Qt.ItemDataRole.ForegroundRole, None)  # the theme's navigation colour
        tooltip = getattr(getattr(self, "tray", None), "setToolTip", None)  # the tray icon, if there is one
        if tooltip is not None:
            tooltip("sandbox-ai" + (f"\n{said}" if said else ""))

    def questions_changed(self, count, new):
        # Said every second (each scan of the questions): the entry and the tray only when the count changed - setting
        # an item's icon again (a new QIcon each time) re-laid out and repainted the navigation every second.
        changed = count != getattr(self, "pending_count", None)
        self.pending_count = count
        if changed:
            self.show_question_count()
        if hasattr(self, "project_bar"):
            self.update_activity()
        self.show_asking()
        if new and backend.setting("notify"):
            from .pages import describe
            entries = [(entry[0], entry[1], entry[2] if len(entry) > 2 else None) for entry in new]
            # One notification per new question, each leading to its own question; a canary that went off first (an
            # alarm: an error toast stays until it is closed), anything else waits for an answer (a warning). A flood
            # of questions at once: the first NOTIFY_AT_ONCE apart, the rest counted in one more.
            entries.sort(key=lambda e: not (isinstance(e[1], dict) and e[1].get("kind") == "canary"))
            for run, question, key in entries[:NOTIFY_AT_ONCE]:
                canary = isinstance(question, dict) and question.get("kind") == "canary"
                self.notify(tr("sandbox-ai asks: {sandbox}", sandbox=run), describe(question),
                            level="error" if canary else "warning",
                            action=(tr("Open approvals"), lambda k=key: self.open_question(k)))
            rest = entries[NOTIFY_AT_ONCE:]
            if rest:
                self.notify(tr("sandbox-ai asks: {count} more questions", count=len(rest)),
                            tr("They wait on the Approvals page."), level="warning",
                            action=(tr("Open approvals"), lambda: self.show_page("approvals")))

    def show_asking(self):
        """Which starts ask (marked on the Sandboxes page), and how many questions wait on each host (Hosts page)."""
        approvals = self.built("approvals")
        keys = list(getattr(approvals, "questions", {}) or {})
        sandboxes, hosts = self.built("sandboxes"), self.built("hosts")
        if sandboxes is not None:
            sandboxes.set_asking({(host, run) for host, run, _ in keys})
        if hosts is not None:
            counts = {}
            for host, _, _ in keys:
                counts[host] = counts.get(host, 0) + 1
            hosts.set_questions(counts)

    def open_question(self, key):
        """A notification's action: the Approvals page with its question selected (when it is still open)."""
        self.show_page("approvals")
        if key is not None:
            self.approvals.select_question(key)

    def changeEvent(self, event):  # noqa: N802
        """A new theme: the navigation's icons and the question count take its colours."""
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "nav_rows", None):
            for key in self.keys:
                self.nav_item(key).setIcon(theme.glyph(key, size=24 if self.compact_nav else 20))
            self.show_question_count()  # the Approvals entry's badge, in the new colours
            QTimer.singleShot(0, lambda: alive(self) and self.fit_nav_width())  # the new theme's frame, once applied
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
            self.show_update_text(tr("Checking for a newer sandbox-ai…"))

    def update_checked(self, status, out, err):
        versions = backend.parse_update_check(status, out)
        if versions is None:  # offline, or not an installation of install.sh: quietly nothing
            self.show_update_text(tr("No update information (offline, or not installed with install.sh)."))
            return
        installed, available = versions
        if backend.newer(available, installed):
            text = tr("sandbox-ai {available} is available (installed: {installed}); update with: sandbox-ai update",
                      available=available, installed=installed)
            self.statusBar().showMessage(text)
        else:
            text = tr("sandbox-ai {installed} is up to date.", installed=installed)
        self.show_update_text(text)

    def show_update_text(self, text):
        """What the update check says, on the System page (one made later shows it then)."""
        self.update_said = text
        system = self.built("system")
        if system is not None:
            system.update_text.setText(text)

    # -- terminals --

    def in_terminal(self, argv, title, embedded=None, cwd=None):
        """Run ARGV in a terminal: a tab of the app (EMBEDDED True, or None and the setting says so, QtWebEngine with
        xterm.js is there and Chromium's sandbox can start now: TerminalsPage.chromium_ready, checked in a thread),
        else a terminal window. True when it runs or will (the tab or, if Chromium's sandbox cannot start, the
        window follows the check), False when no terminal window could be started."""
        if embedded is None:
            embedded = self.sai.settings().get("terminal", "app") == "app"
        directory = cwd if cwd and os.path.isdir(cwd) else None
        approvals = vars(self).get("approvals")  # (made already; and a stand-in window of the tests has none)
        if approvals is not None:  # what it starts may ask (on any host): every host is asked for questions a while
            approvals.wake_hosts()
        if embedded and terminal.available()[0]:
            def opened(view):
                if view is None:  # Chromium's sandbox cannot start now: a terminal window instead
                    self.terminal_window(argv, title)
                    return
                self.show_page("terminals")
                started = started_agent(argv)
                options = argv[:argv.index("--")] if "--" in argv else argv  # not the agent's own arguments
                if started and "--dry-run" not in options and "--session" not in options:
                    # Its terminal tab shows this sandbox: no second tab for its run when it shows up (sync_tabs). A
                    # session is not a one-off run (it gets its session tab): counted, it would swallow the tab of
                    # the next run of the same agent and project started elsewhere.
                    self.own_starts.append((started, os.path.realpath(directory or self.project or ""),
                                            time.monotonic()))
            self.terminals.open(argv, title, directory, done=opened)
            return True
        return self.terminal_window(argv, title)

    def terminal_window(self, argv, title):
        """ARGV in a terminal window of the desktop (SANDBOX_AI_TERMINAL, or one found); False if none starts."""
        command = self.sai.in_terminal(argv, title)
        if command is None:
            QMessageBox.warning(self, tr("No terminal"), shown_as_text(tr(
                "No terminal emulator found. Set SANDBOX_AI_TERMINAL, or run:") + "\n\n" + shlex.join(argv)))
            return False
        started = QProcess.startDetached(command[0], command[1:])
        ok = started[0] if isinstance(started, tuple) else started
        if not ok:
            QMessageBox.warning(self, tr("Not started"), shown_as_text(shlex.join(command)))
        return ok

    def make_busy_indicator(self):
        """At the right of the status bar, while a page has work in the background (a command, a copy, an install): a
        busy bar and which pages ("Running: Kits & plugins…"), so a click that takes a while is seen to be working
        while the window stays usable; gone when nothing runs."""
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setMaximumWidth(90)
        self.busy_bar.setMaximumHeight(10)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setAccessibleName(tr("Running"))
        self.busy_label = widgets.plain_label(word_wrap=False, role="secondary")
        self.statusBar().addPermanentWidget(self.busy_label)
        self.statusBar().addPermanentWidget(self.busy_bar)
        self.version_label = self.make_version_label()
        self.statusBar().addPermanentWidget(self.version_label)
        widgets.activity().changed.connect(self.show_busy)
        self.show_busy()

    def make_version_label(self):
        """Bottom right, always: the app's version, and sandbox-ai's where it differs; where it is installed on hover."""
        from . import __version__
        try:
            launcher = self.sai.version()
        except Exception:  # noqa: BLE001 - a launcher that does not load: the app's version alone
            launcher = None
        text = f"v{__version__}" if launcher in (None, __version__) else \
            tr("App {app} · sandbox-ai {sai}", app=__version__, sai=printable(launcher))
        label = widgets.plain_label(text, word_wrap=False, role="secondary")
        label.setObjectName("version_label")
        label.setAccessibleName(tr("Version"))
        label.setToolTip(widgets.safe_tooltip(tr("sandbox-ai {version} from {path}", version=launcher or "?",
                                                 path=printable(self.sai.directory))))
        return label

    def show_busy(self):
        if getattr(self, "stopping", False):  # "Stop and quit": says that, until the window closes
            return
        try:
            pages = self.running_work()
        except (AttributeError, RuntimeError):  # still being built, or closing
            return
        self.busy_label.setText(tr("Running: {pages}…", pages=", ".join(pages)) if pages else "")
        self.busy_label.setVisible(bool(pages))
        self.busy_bar.setVisible(bool(pages))

    def running_work(self):
        """The titles of the pages with work in the background that a human started (commands, copies, an answer
        being sent). Reads and polls are not work: never shown as running, never asked about when quitting."""
        found = [key for key, _ in PAGES if working(self.built(key))]  # its work=True calls and its Task's job
        if self.approvals.answering is not None:
            found.append("approvals")
        return [tr(title) for key, title in PAGES if key in found]

    def stop_work(self, timeout=None):
        """Cancel the copies (they stop at their next chunk and remove what they created), stop the
        sandbox-ai commands that run (their whole process groups), and wait up to TIMEOUT (QUIT_WAIT)
        seconds for the background work to end (reads are not waited for: shutdown() ends them); the work
        still running. Blocking: for SIGTERM and logout (nobody looks at the window); "Stop and quit" uses
        stop_work_and_quit()."""
        self.stopped = True
        if self.built("files") is not None:
            self.files.task.cancel()
        self.sai.stop_commands()  # on the GUI thread on purpose: quitting, bounded by one grace period
        return ThreadCall.wait_all(QUIT_WAIT if timeout is None else timeout, work_only=True)

    def stop_work_and_quit(self):
        """"Stop and quit": what stop_work() does, in a thread - the window stays painted and says that it stops the
        work (up to a grace period and QUIT_WAIT seconds), takes no more input, and closes when that is done."""
        self.stopped = self.stopping = True
        if self.built("files") is not None:
            self.files.task.cancel()
        self.setEnabled(False)
        self.busy_label.setText(tr("Stopping the work that runs, then quitting…"))
        self.busy_label.setVisible(True)
        self.busy_bar.setVisible(True)

        def stop():
            self.sai.stop_commands()
            return ThreadCall.wait_all(QUIT_WAIT, work_only=True)
        background(stop, owner=self, done=lambda *_: self.quit_now())

    def quit_now(self):
        """Close for good, without another question (everything was asked)."""
        self.quitting, self.leaving, self.close_seen = True, True, False
        self.close()
        if not self.close_seen:  # never shown (in the tray): no close event was sent
            self.closeEvent(QCloseEvent())

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
        self.show_tray_state()
        self.show_question_count()  # the open questions in its tooltip

    def show_tray_state(self):
        """The System page's tray setting: off without a system tray (a System page made later shows it then)."""
        system = self.built("system")
        if system is None:
            return
        tray = getattr(self, "tray", None) is not None
        system.tray_box.setEnabled(tray)
        system.tray_box.setToolTip("" if tray else tr("There is no system tray here"))

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
        """Without a tray, closing ends the answering: True if nothing runs or the human quits anyway. What runs: as the
        registry's files and /proc (live_runs: never Podman, so quitting never waits for it)."""
        try:
            runs = self.sai.live_runs()
        except Exception:  # noqa: BLE001 - nothing known to run
            runs = {}
        if not runs:
            return True
        return ask_first(self, "app-quit", tr("Quit"),
                         tr("{count} sandbox(es) still run. Without this window nobody answers their questions: each "
                            "is refused after {seconds} seconds. Quit anyway?", count=len(runs),
                            seconds=self.sai.approval_seconds), tr("Quit"), reject=tr("Keep the window"))

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
        terminals = self.built("terminals")
        ending = terminals.ending() if terminals is not None else []
        if ending and not ask_first(self, "app-quit", tr("Quit"),
                                    tr("These end with the app: {list}. Sessions keep running (attach again from "
                                       "Terminals or with sandbox-ai attach NAME). Quit?", list=", ".join(ending)),
                                    tr("End them and quit"), reject=tr("Keep the window")):
            event.ignore()
            return
        if terminals is not None and terminals.detaching():
            applog.write(f"{applog.header('INFO', 'quit')}\n  sessions keep running; attach again from Terminals or "
                         f"with sandbox-ai attach NAME\n")
        work = self.running_work()
        if work:
            if not ask_first(self, "app-quit", tr("Quit"),
                             tr("Still at work: {pages}. Quit anyway? Copies are cancelled and what they created is "
                                "removed; running sandbox-ai commands are stopped.", pages=", ".join(work)),
                             tr("Stop and quit"), reject=tr("Keep working")):
                event.ignore()
                return
            event.ignore()  # closed once the work has stopped (in a thread: the window keeps painting)
            self.stop_work_and_quit()
            return
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
        # What still runs is reads and polls (the work was asked about and stopped before): ended without a grace
        # period, with what they started, so that no `sandbox-ai hosts run`, ssh or podman outlives the app.
        self.sai.stop_commands(grace=0)
        widgets.end_worker_processes()
        if self.idle_build:
            self.idle_build.stop()
            self.input_watch.stop()
        self.approvals.timer.stop()
        if self.built("connections") is not None:
            self.connections.timer.stop()
        self.sandboxes.timer.stop()
        self.sandboxes.remote_timer.stop()
        self.update_check.stop()
        if getattr(self, "notifier", None) is not None:
            self.notifier.close_all()  # the popups are windows of their own
        if self.built("images") is not None:
            self.images.list_command.stop()
            self.images.package_command.stop()
        AsyncCommand.end_stopped()  # the timers that would SIGKILL them do not fire any more
        if self.built("activity") is not None:
            self.activity.shutdown()
        if self.built("terminals") is not None:
            self.terminals.close_all()
        if self.tray is not None:
            self.tray.hide()
