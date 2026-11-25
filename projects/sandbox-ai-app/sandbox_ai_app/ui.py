"""The windows of sandbox-ai-app (PySide6)."""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import sys
import textwrap
import time
import weakref

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QProcess, QRect, QSize, QStringListModel, Qt, QTimer, Signal, qVersion
from PySide6.QtGui import QAction, QActionGroup, QBrush, QCloseEvent, QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QStyleOptionComboBox, QStylePainter,
    QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar,
    QPushButton,
    QDockWidget, QFrame, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QStyle, QSystemTrayIcon, QTabBar,
    QTabWidget, QToolButton, QTreeView, QVBoxLayout, QWidget,
)

from . import applog, backend, button_icons, i18n, notifications, terminal, theme, widgets
from . import ask_choice as AC  # (each row of the Sandboxes tree: not imported again per row)
from . import bridges as BR
from .files import printable, printable_text
from .host_choice import HostFollow, follow_note, on_host
from .name_fields import NameField
from .state import choose_folder
from .i18n import N_, tr
from .short_notices import short_notice
from .activity_page import ActivityPage
from .traffic_page import TrafficPage
from . import mcp_fields as MF
from .mcp_copy_view import McpCopyDialog, copy_question
from . import remote_chooser as RC
from .board_view import BoardPage, HostCombo
from . import copying, dock_fold, drafts, help_view, labels, short_notices, storage_hosts
from . import device_picker as DP
from .prerequisites import Prerequisites
from .session_start import SessionStart, StartTab
from . import board_follow, start_link
from .start_link import CommandEdit, StartLink, ViewSwitch
from . import presets as PR
from . import project_trust as PT
from .hosts_page import HostsPage
from .kits_page import KitsPage
from .state import State, scrub_arguments
from .pages import ApprovalsPage, CredentialsPage, ImagesPage, StatisticsPage
from .files_page import FilesPage
from .reviews import REVIEW_PATHS, home_review, restorable_paths, review_refusal
from .start_tree import (
    ID_ROLE, KEY_ROLE, KIND_ROLE, ORPHANS, StartTree, id_host, orphans, owners, related, row_id,
)
from . import wizard_llm as WL
from .widgets import (
    MONO, AsyncCommand, MessageText, ago, StartControls, ThreadCall, append_rows, ask_first, background, busy, confirm,
    explanation, fill, find_row, fit_columns, flow, inform, local_time, outcome, plain_label, report_each, row_key,
    run_each, safe_tooltip, selected, selected_rows, set_enabled, several, show_text, table, warn, shown_as_text,
    tame_combos, working,
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
        short_notice(parent, title, (out + err).strip())
    else:
        warn(parent, title, (out + err).strip(), modal=False)
    return status

GROUP_TITLES = {  # the launcher's option groups (backend.GROUPS), translated
    "Workspace": N_("Workspace"), "Network": N_("Network"), "Proxy & privacy": N_("Proxy & privacy"), "Git": N_("Git"),
    "Agent": N_("Agent"), "Login": N_("Login"), "Display & terminal": N_("Display & terminal"),
    "Cairn": N_("Cairn"), "ARTEX": N_("ARTEX"), "Hardware": N_("Hardware"), "Limits": N_("Limits"), "Engine": N_("Engine"), "More": N_("More"),
}


class PathField(QWidget):
    """A path with a chooser: this machine's folders, or another host's (set_host: remote_chooser, over SSH)."""
    changed = Signal()

    def __init__(self, directory=True):
        super().__init__()
        self.directory, self.host = directory, LOCAL
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

    def set_host(self, host):
        """The host whose path it is: … lists that host's folders (this machine's for LOCAL)."""
        self.host = host or LOCAL

    def browse(self):
        path = RC.choose(self, self.host, tr("Choose a folder") if self.directory else tr("Choose a file"),
                         "folder" if self.directory else "file", "folder" if self.directory else "file",
                         start=self.edit.text().strip())
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
    """The Board page's ruleset files (--rules): one per line (a ListField, read like one: value, set_value), with
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
    the names the chosen host has (set_choices: its MCP servers for --mcp, its kits for --kit, your images for
    --service: a service of that image); its menu opened asks for them again (opening)."""
    changed = Signal()
    opening = Signal()

    def __init__(self, path=False, choices=False, option=None):
        super().__init__()
        self.option, self.host = option, LOCAL
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
            self.pick.menu().aboutToShow.connect(self.opening)
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
            hint = OptionsForm.NONE_YET.get(self.option)
            text = (tr("Could not be read: {error}", error=problem) if problem else
                    tr("Reading…") if names is None else
                    f"{tr('None on {host}', host=where)} - {tr(hint)}" if hint else tr("None on {host}", host=where))
            menu.addAction(text).setEnabled(False)
        self.pick.setToolTip(safe_tooltip(
            tr("Add a service of one of your images on {host}", host=where) if self.option == "--service" and where else
            tr("Add one that {host} has", host=where) if where else tr("Add one")))

    def add(self, value):
        if self.option in OptionsForm.SECRET_CHOICES:  # a secret's name: HOST=NAME, URL=NAME or NAME (name_fields)
            from .name_fields import add_line
            self.edit.setPlainText(add_line(self.edit.toPlainText(), value, self.option))
        elif self.option == "--service":  # a service of your image VALUE: named after it, its image given
            self.edit.appendPlainText(service_line(value, self.value()))
        elif value not in self.value():
            self.edit.appendPlainText(value)

    def set_host(self, host):
        """The host whose paths these are: + lists that host's folders (this machine's for LOCAL)."""
        self.host = host or LOCAL

    def add_folder(self):
        path = RC.choose_folder(self, self.host, tr("Choose a folder"), "folder")
        if path:
            self.edit.appendPlainText(path)

    def value(self):
        return [line.strip() for line in self.edit.toPlainText().splitlines() if line.strip()]

    def set_value(self, values):
        self.edit.setPlainText("\n".join(values or []))


class MountRow(QWidget):
    """One folder of MountRows: the folder (PathField: the host's folders, or a folder of another machine typed,
    sshfs://...), where the sandbox sees it, and whether the agent may change it; a button that removes the row."""

    def __init__(self, host, remove):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source = PathField()
        self.source.set_host(host)
        self.source.edit.setPlaceholderText(tr("Folder"))
        self.source.edit.setAccessibleName(tr("Folder"))
        self.source.changed.connect(self.offer_destination)
        self.destination = QLineEdit()
        self.destination.setPlaceholderText(tr("In the sandbox at") + " /mnt/…")
        self.destination.setAccessibleName(tr("In the sandbox at"))
        self.destination.textEdited.connect(lambda _text: setattr(self, "destination_typed", True))
        self.destination_typed = False
        self.writable = QCheckBox(tr("The agent may change it"))
        self.remove_button = QToolButton()
        self.remove_button.setText("×")
        self.remove_button.setToolTip(tr("Remove this folder"))
        self.remove_button.setAccessibleName(tr("Remove this folder"))
        self.remove_button.clicked.connect(lambda: remove(self))
        layout.addWidget(self.source, 3)
        layout.addWidget(self.destination, 2)
        layout.addWidget(self.writable)
        layout.addWidget(self.remove_button)

    def offer_destination(self):
        """/mnt/NAME (NAME: the folder's last part) where the sandbox sees it, until one is typed."""
        if self.destination_typed:
            return
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", self.source.value().rstrip("/").rpartition("/")[2]).strip("-.")
        self.destination.setText(f"/mnt/{name}" if name else "")


class MountRows(QWidget):
    """Folders for --mount, one row each (MountRow), and Add a folder…: never a line of SOURCE:DESTINATION:MODE to
    type. value(): [(folder, where the sandbox sees it, "ro" | "rw")]; ValueError names a row that is half filled."""

    def __init__(self, host="local"):
        super().__init__()
        self.host = host or "local"
        self.rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = QVBoxLayout()
        layout.addLayout(self.list)
        self.add_button = QPushButton(tr("Add a folder…"))
        self.add_button.clicked.connect(lambda _=False: self.add_row())
        layout.addWidget(self.add_button, alignment=Qt.AlignmentFlag.AlignLeft)

    def add_row(self, source="", destination="", mode="ro"):
        row = MountRow(self.host, self.remove_row)
        if source:
            row.source.set_value(source)
        if destination:
            row.destination.setText(destination)
            row.destination_typed = True
        row.writable.setChecked(mode == "rw")
        self.rows.append(row)
        self.list.addWidget(row)
        return row

    def remove_row(self, row):
        if row in self.rows:
            self.rows.remove(row)
            self.list.removeWidget(row)
            row.deleteLater()

    def value(self):
        found = []
        for number, row in enumerate(self.rows, 1):
            source, destination = row.source.value().strip(), row.destination.text().strip()
            if not source and not destination:
                continue
            if not source:
                raise ValueError(tr("Folder {number}: which folder (choose it with …)", number=number))
            if not destination.startswith("/"):
                raise ValueError(tr("Folder {number}: where the sandbox sees it (an absolute path such as /mnt/data)",
                                    number=number))
            found.append((source, destination, "rw" if row.writable.isChecked() else "ro"))
        return found

    def set_value(self, mounts):
        for row in list(self.rows):
            self.remove_row(row)
        for source, destination, mode in mounts or []:
            self.add_row(source, destination, mode)

    def draft_value(self):
        """Every row as typed, a half filled one too ([[folder, where, "ro" | "rw"]]): a dialog's draft (drafts.py)."""
        return [[row.source.value(), row.destination.text(), "rw" if row.writable.isChecked() else "ro"]
                for row in self.rows if row.source.value().strip() or row.destination.text().strip()]

    def set_draft_value(self, rows):
        if isinstance(rows, list):
            self.set_value([tuple(row) for row in rows if isinstance(row, list) and len(row) == 3])


def service_line(image, lines=()):
    """The --service value of a service of your image IMAGE: named after it (a service's name: lower-case letters,
    digits and '-', up to 24; a name LINES use already gets a number), image= given (never left to the name)."""
    base = re.sub(r"[^a-z0-9-]+", "-", str(image).lower()).strip("-")[:20].strip("-") or "service"
    taken = {str(line).partition(":")[0].strip() for line in lines}
    name = next(candidate for candidate in (base if i == 0 else f"{base}-{i}" for i in range(100))
                if candidate not in taken)
    return f"{name}:image={image}"


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
    choices_wanted = Signal()  # a chooser of what the host has was opened: its lists are read again

    def __init__(self, options):
        super().__init__()
        widgets.arrow_tabs(self)  # (the app's tab row: arrows at its end when the tabs do not fit)
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
    HOST_CHOICES = ("--vpn", "--kit", "--identity", "--mcp", "--image-variant", "--image", "--service", "--secret-env",
                    "--api-key", "--git-token-secret")
    # Where one is added, for a chooser whose host has none yet.
    NONE_YET = {"--service": N_("the Images page, Your images, adds one"),
                "--image-variant": N_("the Images page, New variant, makes one"),
                "--image": N_("the Images page builds the agents' images"),
                "--vpn": N_("the VPN page adds one"), "--mcp": N_("the MCP servers page adds one"),
                "--kit": N_("the Kits & plugins page adds one"), "--identity": N_("the Identities page adds one")}
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
            widget.opening.connect(self.choices_wanted)
            if option.long == "--service":  # a service of your image: the "+" names it and gives its image
                widget.edit.setPlaceholderText(tr("NAME:image=IMAGE,port=PORT per line - \"+\" adds one of your images"))
            return widget
        if option.long in self.HOST_CHOICES and option.kind == "value":
            from .board_view import OpeningCombo
            widget = OpeningCombo()
            widget.setEditable(True)
            widget.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            widget.addItem("")
            widget.lineEdit().setPlaceholderText(option.metavar or "")
            # what is typed completes to the host's names alone (never to the greyed "None on ..." entry)
            widget.completions = QStringListModel(widget)
            found = QCompleter(widget.completions, widget)
            found.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            found.setFilterMode(Qt.MatchFlag.MatchContains)
            found.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            widget.setCompleter(found)
            widget.currentTextChanged.connect(self.changed)
            widget.opening.connect(self.choices_wanted)
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
        elif option.kind == "list" and option.long == "--hide":  # globs, the usual secret files suggested
            from . import rules_tools
            widget = ListField(path=False)
            widget.edit.setPlaceholderText(rules_tools.hide_placeholder())
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
                    if hasattr(widget, "completions"):
                        widget.completions.setStringList(list(names or []))
                    if names == [] and long in self.NONE_YET:  # none there: where one is added (greyed)
                        widget.addItem(f"{tr('None on {host}', host=where)} - {tr(self.NONE_YET[long])}")
                        widgets.set_row_enabled(widget, widget.count() - 1, False)
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
                        widgets.set_row_enabled(widget, index, False)
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
        """The fields as VALUES says, then one change said: each field set said one (the preview, the warnings and the
        state kept worked out again for every field - each looking at all of them: 25 times on a switch of tabs)."""
        self.blockSignals(True)
        try:
            self._set_values(values)
        finally:
            self.blockSignals(False)
        self.changed.emit()  # (mark_dangerous too)

    def _set_values(self, values):
        for option in self.options:
            widget, value = self.widgets[option.long], values.get(option.long)
            if backend.retired(option.long, value):  # an old --telemetry on: shown as off (not given)
                value = None
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


PROJECTS_KEPT = 40  # the projects whose Board page form is kept (the ones shown last)


def without_file_values(options, values, file_values):
    """VALUES (OptionsForm.values) without what FILE_VALUES (a trusted .sandbox-ai.json's, values_from_argv) sets
    the same: a flag set there, a value equal to the file's, the items of a list the file has."""
    by_name = {option.long: option for option in options}
    result = dict(values)
    for name, given in file_values.items():
        option = by_name.get(name)
        if option is None or name not in result:
            continue
        if option.kind in ("list", "multi"):
            rest = list(result[name] or [])
            for item in given if isinstance(given, list) else [given]:
                if item in rest:
                    rest.remove(item)
            result[name] = rest
        elif option.kind == "flag":
            result[name] = bool(result[name]) and not given
        elif str(result[name] or "").strip() == str(given or "").strip():
            result[name] = ""
    return result


class LaunchPage(Prerequisites, SessionStart, QWidget):
    """Choose the agent and every option, see the command line and edit it, start. In the window this is the Expert
    view of the Board page: the same start as the Board view's board (self.link: start_link.StartLink, which keeps the
    two the same) - what is set here is put onto the board, and Start, Dry run, the project's file and the presets
    are the board's. On its own (no link) it is a form with its own start, as before."""
    opened = Signal(str)  # a project folder was reviewed and trusted here
    images_wanted = Signal()  # a start refused for a service's image: the Images page, Your images
    view_wanted = Signal(str)  # the window: show this view of the Board page ("board": what the Board alone shows)
    link = None  # the Board view's board is this view's start (the window: start_link.StartLink); None: on its own

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.options = sai.options()
        self.project, self.state = None, None
        # {project: values of its trusted .sandbox-ai.json} (argv: the launcher takes those from the file); the
        # project whose form waits for that file to fill it (show_project_form), with the form as it was then.
        self.file_values, self.seeding, self.switching = {}, None, False
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
        # The picker beside it: every model with search, sorts and its tags (text, code, vision ...): model_picker.py
        from .model_picker import picker_button
        self.model_picker = picker_button(
            self.model_choice, lambda: WL.picker_rows(self.llm_servers, self.agent.currentText(), self.sai),
            lambda _value: self.model_chosen())
        self.llm_servers, self.llm_loading, self.llm_read_at, self.model_seen = [], False, 0.0, None
        self.llm_again, self.llm_host = False, LOCAL  # the host whose servers llm_servers are
        # Where it starts: this machine, or a host of sandbox-ai hosts (its launcher there checks and starts it).
        self.host_choice = HostCombo(sai)
        self.host_known = {}  # {(host, agent): that host's launcher options, or None: could not be read}
        # What it is for (--use-case): sandbox-ai's local-model research's use cases; choosing one sets the pair
        # measured best for it among the chosen server's models (the agent and the model) - the pairs below.
        self.use_case_choice = QComboBox()
        # (as wide as a short name, not as its longest entry: the page's minimum width stays what it was)
        self.use_case_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.use_case_choice.setMinimumContentsLength(14)
        self.use_case_choice.setAccessibleName(tr("Use case"))
        self.use_case_tip = tr("What the start is for: picks the model and agent sandbox-ai's local-model research "
                               "found best for it among your server's models (--use-case); the pairs below list the "
                               "others, unsafe ones with why")
        self.use_case_choice.setToolTip(safe_tooltip(self.use_case_tip))
        self.use_case_choice.activated.connect(lambda _index: self.use_case_chosen())
        self.use_cases = None  # use_case.recommendations of the servers read (None: none, or no use cases)
        layout.addWidget(flow(self.host_choice, buddy(tr("Agent"), self.agent), self.agent,
                              buddy(tr("Model"), self.model_choice), self.model_choice, self.model_picker,
                              self.model_only, buddy(tr("Use case"), self.use_case_choice), self.use_case_choice,
                              buddy(tr("Profile"), self.profile), self.profile, load, save, project))
        self.host_note = plain_label(role="secondary")
        self.host_note.hide()
        layout.addWidget(self.host_note)
        # The model profile sandbox-ai applies for the model in this agent (model_profile.py): its line, the whole of
        # it in the tooltip, its warnings; --no-llm-profile in the options switches it off.
        self.model_profile_note = plain_label(role="secondary", selectable=True)
        self.model_profile_note.setAccessibleName(tr("Model profile"))
        self.model_profile_note.hide()
        layout.addWidget(self.model_profile_note)
        # More MCP tools than the model's class copes with (sandbox-ai's mcp_budget.py, this machine's count): said
        # below the model with the servers to leave out - a warning, the start goes on.
        self.tool_budget_note = plain_label(role="notice-warn", selectable=True)
        self.tool_budget_note.setAccessibleName(tr("MCP tools for this model"))
        self.tool_budget_note.hide()
        self.tool_budget_token = 0
        layout.addWidget(self.tool_budget_note)
        # What we know of the model in this agent (model_profile_view.KnowledgeBox): works, does not, not known yet.
        from .model_profile_view import KnowledgeBox
        self.knowledge_box = KnowledgeBox()
        layout.addWidget(self.knowledge_box)
        # The use case's pairs for the chosen server: model x harness, best first (use_case_view.PairList).
        from .use_case_view import PairList
        self.use_case_text = plain_label(role="secondary", selectable=True)
        self.use_case_text.hide()
        layout.addWidget(self.use_case_text)
        self.pair_list = PairList()
        self.pair_list.picked.connect(lambda pair: self.use_pair(str(self.use_case_choice.currentData() or ""),
                                                                 pair["server"], pair["model"], pair.get("harness")))
        layout.addWidget(self.pair_list)
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
        self.form.choices_wanted.connect(self.choices_opened)
        try:
            self.form.apply_policy(sai.forbidden())
        except Exception as _exc:  # noqa: BLE001 - a broken policy is shown on the System page
            applog.swallowed("ui", "LaunchPage.__init__: a broken policy is shown on the System page", _exc)
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
        # The whole start as its command line, to read, edit or paste: taken once it can be read (the fields - and in
        # the window the board - follow at once); a text that cannot be read changes nothing, the reason below it.
        self.preview = self.command = CommandEdit()
        self.preview.setFont(MONO)
        self.preview.setFixedHeight(70)
        self.preview.setAccessibleName(tr("Command line"))
        self.preview.setToolTip(safe_tooltip(tr(
            "The start as its command line: sandbox-ai AGENT [options] [-- the agent's arguments]. Edit or paste a "
            "line here: once it can be read, the fields above (and the Board) show it; a line that cannot be read "
            "changes nothing and says why below.")))
        self.preview.edited.connect(self.line_edited)
        self.preview.entered.connect(self.line_left)
        self.preview.left.connect(self.line_left)
        self.copy_button = QPushButton(tr("Copy command"))
        self.copy_button.setToolTip(tr("Copy the command line of this start (to run it in a terminal)"))
        self.copy_button.clicked.connect(lambda _=False: self.copy_command())
        line = QHBoxLayout()
        line.addWidget(self.preview, 1)
        line.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(line)
        # Below the line: why a line as typed is not taken; in the window also what is wrong with the start (the
        # board's errors, the launcher's verdict of its last check) - with the way to the Board view, which lists all.
        self.line_note = plain_label(role="secondary", selectable=True)
        self.line_note.setAccessibleName(tr("What is wrong with the command line or the start"))
        self.line_note.hide()
        self.line_text = ""  # the line as typed while it cannot be read (update_preview leaves it to the human)
        self.board_button = QPushButton(tr("Show on the Board"))
        self.board_button.setToolTip(tr("The Board view of this start: its parts, every finding, and what a start "
                                        "that waits is preparing"))
        self.board_button.clicked.connect(lambda _=False: self.view_wanted.emit("board"))
        self.board_button.hide()
        note_row = QHBoxLayout()
        note_row.addWidget(self.line_note, 1)
        note_row.addWidget(self.board_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(note_row)
        # Undo and Redo: the board's own (one history for both views), shown in the window only (linked).
        self.undo_button, self.redo_button = QToolButton(), QToolButton()
        self.undo_button.setText(tr("Undo"))
        self.redo_button.setText(tr("Redo"))
        self.undo_button.setToolTip(tr("Undo the last change of this start, wherever it was made (Board or Expert)"))
        self.redo_button.setToolTip(tr("Redo the change undone last"))
        self.undo_redo = widgets.together(self.undo_button, self.redo_button)
        self.undo_redo.hide()
        dry = QPushButton(tr("Dry run"))
        dry.setToolTip(tr("Check the options and show the plan; nothing is started"))
        dry.clicked.connect(self.dry_run)
        self.dry_button = dry
        # One Start: where, and whether as a session (a codename unless one is typed) - widgets.StartControls.
        self.start_controls = StartControls(lambda embedded, session: self.start(session=session, embedded=embedded),
                                            sai, sai.settings() if hasattr(sai, "settings") else None)
        self.session = self.start_controls.name
        write = self.write_button = QPushButton(tr("Save as project configuration…"))
        write.setToolTip(tr("Write .sandbox-ai.json into the first workspace and trust it"))
        write.clicked.connect(self.write_project)
        layout.addWidget(flow(self.undo_redo, dry, self.start_controls, write))
        self.host_choice.chosen.connect(lambda _host: self.host_changed())
        self.agent.currentTextChanged.connect(lambda _agent: self.read_host_options())
        self.form.changed.connect(self.update_preview)
        self.agent.currentTextChanged.connect(self.update_preview)
        self.agent_args.textChanged.connect(self.update_preview)
        self.form.changed.connect(self.show_model)
        self.form.changed.connect(lambda *_: self.show_model_profile())  # its switch, your --llm-param values
        self.form.changed.connect(lambda *_: self.show_use_case())  # --use-case typed in the options
        self.agent.currentTextChanged.connect(lambda _text: self.show_model(force=True))
        self.agent.currentTextChanged.connect(lambda _text: self.apply_agent_options())
        # (the agent's own image looked at in the background as soon as it is shown: Start only reads the answer)
        self.agent.currentTextChanged.connect(lambda _agent: self.look_at_agent_image())
        self.model_choice.activated.connect(lambda _index: self.model_chosen())
        self.model_only.toggled.connect(lambda _on: self.model_chosen())
        self.apply_agent_options()
        self.refresh_profiles()
        self.update_preview()
        self.show_model(force=True)
        self.read_llm_servers()

    # -- the Expert view of the Start page: one start with the Board view (start_link.StartLink) --

    def linked(self):
        """The window linked this view with the Board (self.link): Undo and Redo are the board's, the project's file
        is written by the board's Apply, and what this page kept per agent and project is the board's to keep."""
        board = self.link.board_page
        self.undo_redo.show()
        self.undo_button.clicked.connect(lambda _=False: self.undo_redo_step(False))
        self.redo_button.clicked.connect(lambda _=False: self.undo_redo_step(True))
        board.undo_group.canUndoChanged.connect(self.undo_button.setEnabled)
        board.undo_group.canRedoChanged.connect(self.redo_button.setEnabled)
        self.undo_button.setEnabled(board.undo_group.canUndo())
        self.redo_button.setEnabled(board.undo_group.canRedo())
        self.write_button.setToolTip(tr("Write .sandbox-ai.json into the first workspace and trust it, after showing "
                                        "it and what changes (the Board's Apply)"))
        self.review_button.setToolTip(tr("Show the project's .sandbox-ai.json as this start has it and what changes, "
                                         "then write and trust exactly that (the Board's Apply)"))

    def undo_redo_step(self, redo):
        """Undo or Redo of the one history both views share; what was typed a moment ago is a step of it first."""
        self.link.flush()
        group = self.link.board_page.undo_group
        (group.redo if redo else group.undo)()

    def veil_also(self):
        """What a dialog of this view veils besides it: the Board view and its panels (the same start)."""
        if self.link is None:
            return []
        board = self.link.board_page
        return [board, *board.veil_also()]

    def copy_command(self):
        """Copy command: the command line of the start (as the field shows it) onto the clipboard."""
        if self.link is not None:
            self.link.flush()
            text = self.link.command()
        else:
            text = self.preview.toPlainText()
        QApplication.clipboard().setText(text)
        short_notice(self, tr("Command line"), tr("Copied: {command}", command=printable(text)[:300]))
        return text

    def show_line_note(self, text, role="secondary", whole=None, board=False):
        """TEXT below the command line (ROLE: its colour; WHOLE: all of it, in the tooltip), or nothing; BOARD: with
        the way to the Board view, which shows more about it (its findings, what a start waits for)."""
        self.line_note.setText(text)
        self.line_note.setToolTip(safe_tooltip(whole or text) if text else "")
        if self.line_note.property("role") != role:
            self.line_note.setProperty("role", role)
            self.line_note.style().unpolish(self.line_note)
            self.line_note.style().polish(self.line_note)
        self.line_note.setVisible(bool(text))
        self.board_button.setVisible(bool(text) and bool(board) and self.link is not None)

    def line_edited(self, text):
        """The command line was typed into (this page on its own; in the window the link takes it): read whole and
        shown in the fields, or - a text that cannot be read - nothing changes and the reason is shown below it."""
        if self.link is not None:
            return
        from . import board as B
        try:
            host, agent, argv, agent_args = B.parse_command(self.sai, text)
            if host and host != self.host() and self.host_choice.findData(host) < 0:
                raise ValueError(tr("{host} is not a host of sandbox-ai hosts", host=printable(host)))
        except ValueError as exc:
            self.line_text = text
            self.show_line_note(printable_text(exc), "notice-error")
            return
        self.line_text = text
        self.show_line_note("")
        if (host or LOCAL) != self.host():
            self.host_choice.choose(host or LOCAL)
        values, _ = backend.values_from_argv(self.options, argv, backend.canonical_of(self.sai))
        self.agent.setCurrentText(agent)
        self.form.set_values(values)
        self.agent_args.setText(shlex.join(agent_args))

    def line_left(self):
        """Enter in the command line, or the keyboard left it (this page on its own): a line that was read is shown
        as the fields have it now; one that cannot be read stays, with its reason."""
        if self.link is None and not self.line_note.isVisible():
            self.line_text = ""
            self.update_preview()

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
        # A read, not work: no owner, so it neither greys out the page's buttons (busy()) nor holds up quitting. Two
        # steps: every registered server at once (llm list), then their models (all asked at once, each briefly).
        background(lambda: self.sai.host_llm_endpoints(host), timeout=90,
                   done=lambda result, error: alive(self) and self.llm_servers_read(host, result, error, asking=True))

    def llm_servers_read(self, host, result, error, asking=False):
        """The servers of HOST read (ASKING: only listed so far - shown, and now asked for their models)."""
        if host != self.host() or self.llm_again:
            self.llm_loading = False
            self.read_llm_servers()  # another host was chosen meanwhile: its servers, never these
            return
        self.llm_read_at, self.llm_host = time.monotonic(), host
        self.llm_servers = result if error is None and isinstance(result, list) else []
        self.show_model(force=True)
        if asking and self.llm_servers:
            listed = list(self.llm_servers)
            background(lambda: self.sai.host_llm_servers(host, listed), timeout=150,
                       done=lambda found, failed: alive(self) and self.llm_servers_read(
                           host, found if failed is None else listed, None))
            return
        self.llm_loading = False

    def showEvent(self, event):  # noqa: N802
        self.host_choice.reload()  # a host added on the Hosts page meanwhile
        super().showEvent(event)
        self.look_at_agent_image()
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
                                     error=printable_text(error)[-300:]), "notice-warn")
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
        chosen = gallery.result_ if gallery.exec() == gallery.DialogCode.Accepted else None
        widgets.drop_after_exec(gallery)  # (made anew each time)
        if chosen is None:
            return None
        preset, values = chosen
        self.refresh_presets()
        return self.apply_preset(preset, values or {})

    def policy_or_empty(self):
        try:
            return self.sai.policy()[0] or {}
        except Exception as _exc:  # noqa: BLE001 - a broken policy is shown on the System page
            applog.swallowed("ui", "LaunchPage.policy_or_empty: a broken policy is shown on the System page", _exc)
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
                self.show_preset_note(printable_text(exc), "notice-warn")
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
            if widgets.AUTO_ANSWER is None and widgets.exec_dropped(form) != QDialog.DialogCode.Accepted:
                return False
            values = form.values() or {}
        values = values or {}
        if self.link is not None:
            # In the window: the preset becomes the start of both views, as the Board's New from preset makes it (its
            # parts, its own MCP servers and ruleset, what is left to fill in asked by name at the start).
            self.link.flush()
            board = self.link.board_page.new_from_preset(preset, values)
            if board is None:
                return False
            left = [marker for marker, _ in B.placeholders(board)]
            self.show_preset_note(tr("Still to fill in: {list}", list=", ".join(printable(m) for m in left))
                                  if left else "", "notice-warn" if left else "secondary")
            index = self.preset_choice.findData(preset.key)
            if index >= 0:
                self.preset_choice.setCurrentIndex(index)
            return True
        notes = []
        if preset.agents and self.agent.currentText() not in preset.agents:
            self.agent.setCurrentText(preset.agents[0])
            notes.append(tr("It is for {agents}: {agent} chosen.", agents=", ".join(preset.agents),
                            agent=preset.agents[0]))
        elif preset.agent and preset.agent in self.sai.agents:
            self.agent.setCurrentText(preset.agent)
        # (sandbox-ai's preset of another host only - the presets window lists every host's -: its options taken
        # over, as the app's own; `--preset` there would not find it)
        if preset.source == "sandbox-ai" and host in PR.from_hosts(preset):
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

    def read_host_choices(self, keep=False):
        """The chosen host's VPN profiles, kits, identities, MCP servers, image variants, agents' images and your images
        (backend.host_start_choices), read in the background; a late answer for another host is dropped (a token per
        read). KEEP (a chooser opened): what it offers stays until the answer."""
        host = self.host()
        self.choices_token += 1
        token = self.choices_token
        keep = keep and self.choices_host == host
        self.choices_host = host
        if not keep:
            self.form.set_choices(host, None, {})
        background(self.sai.host_start_choices, host, timeout=180,
                   done=lambda result, error: alive(self) and self.host_choices_read(token, host, result, error))

    def names_changed(self, _kind, host):
        if host == self.choices_host:
            self.choices_read_at = 0.0

    def choices_opened(self):
        """A chooser opened: what the host has, read again unless read a moment ago (or being read)."""
        now = time.monotonic()
        if self.choices_host == self.host() and (now - self.choices_read_at < 3.0
                                                 or now - getattr(self, "choices_asked_at", 0.0) < 3.0):
            return
        self.choices_asked_at = now
        self.read_host_choices(keep=True)

    def host_choices_read(self, token, host, result, error):
        if token != self.choices_token or host != self.host():
            return
        self.choices_read_at = time.monotonic()
        if error is not None:
            found, problems = {}, {long: printable_text(error)[-200:] for long in OptionsForm.HOST_CHOICES}
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
        self.look_at_agent_image()  # (this machine again: its agent's image, in the background)
        workspace = self.form.widgets.get("--workspace")
        if workspace is not None and self.project and self.link is None:  # (linked: the board's folders, as they are)
            if remote and workspace.value() == [self.project]:
                workspace.set_value([])  # this machine's folder is not a folder there
            elif not remote and not workspace.value():
                workspace.set_value([self.project])
        for widget in self.form.widgets.values():
            if hasattr(widget, "set_host"):  # a path's chooser: that host's folders
                widget.set_host(host)
            elif hasattr(widget, "set_local"):  # this computer's devices: offered only here
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
            text += " " + tr("Its options could not be read: {error}", error=printable_text(problem)[-200:])
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

    def apply_agent_options(self):
        """The options that depend on the agent: Show thinking (--show-thinking) is greyed out, with the reason and
        nothing chosen, for an agent without such a switch (Qwen Code); for the others its tooltip names the agent's
        own switch and default."""
        from . import thinking
        widget = self.form.widgets.get(thinking.OPTION)
        if widget is None:
            return
        agent = self.agent.currentText()
        if not thinking.available(agent):
            if isinstance(widget, QComboBox) and widget.currentText():
                widget.setCurrentText("")
            widget.setEnabled(False)
        elif thinking.OPTION not in self.host_greyed:
            widget.setEnabled(True)
        widget.setToolTip(safe_tooltip(thinking.tooltip(agent) + (
            "" if not thinking.available(agent) else "\n" + tr("Empty: the agent's default ({value})",
                                                              value=thinking.AGENT_DEFAULTS[agent]))))

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
        self.model_picker.setEnabled("--llm" in self.form.widgets)
        fill_model_combo(self.model_choice, self.model_only, self.llm_servers, self.agent.currentText(), self.sai,
                         seen[0], seen[1])
        self.show_model_profile(values)
        self.show_use_case(values, force=True)

    # -- the use case: the pair (model x harness) measured best for it among the server's models --

    def show_use_case(self, values=None, force=False):
        """The Use case choice as --use-case says, and below the model its choice and the pairs of the chosen
        server's models (every server's without one), with the notes for the agent chosen (use_case.py). Greyed out
        with why where there is no --use-case or no use cases (an older sandbox-ai)."""
        from . import use_case as UC
        from .use_case_view import fill_use_cases
        values = self.form.values() if values is None else values
        current = str(values.get(UC.OPTION) or "").strip()
        if not force and current == getattr(self, "use_case_seen", None):
            return  # (another option changed: nothing of the use case's)
        self.use_case_seen = current
        name, model = WL.parse_value(values.get("--llm"))
        endpoints = [e for e in self.llm_servers or [] if isinstance(e, dict)]
        own = [e for e in endpoints if e.get("name") == name]
        found = UC.recommendations(self.sai, own or endpoints, self.host() == LOCAL,
                                   note_agent=self.agent.currentText()) if endpoints else None
        self.use_cases = found
        fill_use_cases(self.use_case_choice, found or [], current)
        knows = UC.OPTION in self.form.widgets and found is not None
        self.use_case_choice.setEnabled(knows)
        self.use_case_choice.setToolTip(safe_tooltip(
            self.use_case_tip if knows else
            tr("No model server of yours read yet (Model servers page), or this sandbox-ai has no use cases "
               "(sandbox-ai llm recommend)") if UC.OPTION in self.form.widgets else
            tr("This sandbox-ai has no use cases yet (sandbox-ai llm recommend): update it")))
        chosen = next((entry for entry in found or [] if entry["use_case"] == current), None)
        text = UC.choice_text(chosen) if chosen else ""
        choice = (chosen or {}).get("choice")
        if choice and choice.get("harness") != self.agent.currentText():
            pair = next((p for p in UC.pairs_of(chosen) if (p["server"], p["model"]) ==
                         (choice["server"], choice["model"])), None)
            note = UC.agent_note(pair) if pair else ""
            text = "\n".join(filter(None, [text, note]))
        self.use_case_text.setText(text)
        self.use_case_text.setVisible(bool(text))
        self.pair_list.set(chosen, server=name if own else None, current=(name, model) if model else None)

    def use_case_chosen(self):
        """A use case chosen: --use-case, and its choice - the pair measured best for it among the chosen server's
        models - as the agent and the model (none: only --use-case, the model stays yours)."""
        from . import use_case as UC
        name = str(self.use_case_choice.currentData() or "")
        entry = next((e for e in self.use_cases or [] if e["use_case"] == name), None)
        choice = (entry or {}).get("choice")
        if choice:
            self.use_pair(name, choice["server"], choice["model"], choice.get("harness"))
            return
        widget = self.form.widgets.get(UC.OPTION)
        if widget is not None:
            values = self.form.values()
            values[UC.OPTION] = name
            self.form.set_values(values)
        self.show_use_case()

    def use_pair(self, name, server, model, harness):
        """The pair SERVER:MODEL in HARNESS for the use case NAME on the page: the agent (its form, as a switch of the
        agent brings it), --llm SERVER:MODEL ("only this server" as it was, else on), --use-case NAME."""
        from . import use_case as UC
        if harness and harness in self.sai.agents and harness != self.agent.currentText():
            self.agent.setCurrentText(harness)
        values = self.form.values()
        only = bool(values.get("--no-provider-defaults")) or not str(values.get("--llm") or "").strip()
        try:
            values = WL.form_values(values, WL.choice_of(WL.llm_value(server, model), only))
        except ValueError as exc:
            warn(self, tr("Use case"), printable_text(exc))
            return
        if UC.OPTION in self.form.widgets:
            values[UC.OPTION] = name
        self.form.set_values(values)
        self.show_model(force=True)

    def show_model_profile(self, values=None):
        """The model profile of --llm's model in the chosen agent, below the model (hidden without one): its badge,
        its values (yours marked) in the tooltip, its warnings; off with --no-llm-profile."""
        from . import model_profile as MP
        from . import llm_params as LP
        values = self.form.values() if values is None else values
        name, model = WL.parse_value(values.get("--llm"))
        server = next((e for e in self.llm_servers or [] if isinstance(e, dict) and e.get("name") == name), None)
        model = model.split(",")[0] if model else str((server or {}).get("model") or "")
        profile = MP.lookup(self.sai, model, self.agent.currentText()) if name and model else None
        self.show_tool_budget(model if name else "", values)
        try:  # the box below: what we know of the model in this agent (also: nothing, for a model of no family)
            self.knowledge_box.set(MP.knowledge_of(self.sai, model, self.agent.currentText()) if name else None)
        except Exception as _exc:  # noqa: BLE001 - the page is never broken by it
            applog.swallowed("ui", "the What we know box failed (hidden)", _exc)
            self.knowledge_box.set(None)
        if profile is None:
            self.model_profile_note.hide()
            self.model_profile_note.setText("")
            return
        enabled = not values.get(MP.OPTION)
        params = LP.parse_start(values.get("--llm-param") or [])[0]
        mine = MP.own_keys(params, server, model)
        bad = enabled and profile.get("status") in MP.WARNING_STATUSES
        text = ("! " if bad else "") + MP.badge(profile, enabled)
        shown = MP.warnings(profile)
        self.model_profile_note.setText("\n".join([text, *shown]))
        self.model_profile_note.setToolTip(safe_tooltip("\n".join(MP.details(profile, mine, enabled))))
        self.model_profile_note.setProperty("role", "notice-warn" if bad or shown else "secondary")
        self.model_profile_note.show()

    def show_tool_budget(self, model, values):
        """The MCP tools MODEL would see with the form's --mcp servers against its class's budget, counted by this
        machine's sandbox-ai in the background (its pins for the first workspace, else the catalog's lists): a note
        below the model when they are more (hidden otherwise, and for another host: its launcher says it at the
        start, its Board check too)."""
        self.tool_budget_token += 1
        token = self.tool_budget_token
        servers = [str(s).strip() for s in values.get("--mcp") or [] if str(s).strip()] \
            if isinstance(values.get("--mcp"), (list, tuple)) else []
        if not model or not servers or self.host() != LOCAL:
            self.tool_budget_shown(token, None, None)
            return
        project = self.workspace()
        background(lambda: self.sai.mcp_tool_budget(model, servers, project), owner=self, timeout=60,
                   what="counting the MCP tools of the start's model", done=lambda found, error:
                   self.tool_budget_shown(token, found, error))

    def tool_budget_shown(self, token, found, error):
        from . import model_profile as MP
        if token != self.tool_budget_token:
            return  # (an answer for the form as it was before)
        if error is not None:
            applog.log("DEBUG", "ui", f"the MCP tools of the start are not counted: {printable_text(str(error))[:300]}")
        lines = MP.tool_budget_lines(found) if error is None else []
        self.tool_budget_note.setText("\n".join([*lines, tr(MP.TOOL_TEXTS[5])]) if lines else "")
        self.tool_budget_note.setToolTip(safe_tooltip(MP.tool_budget_details(found)) if lines else "")
        self.tool_budget_note.setVisible(bool(lines))

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
        """The agent and profile chosen, and per project and agent its last options and agent arguments, come back
        at the next start (the workspace follows the project; agent arguments that may be secrets are left out)."""
        self.state = state
        saved = state.section("launch")
        if self.link is not None:
            # The Expert view of the Start page: the start is the board's, kept with the board (its drafts, its host)
            # - only the profile chosen last is this view's own.
            if isinstance(saved.get("profile"), str) and self.profile.findText(saved["profile"]) >= 0:
                self.profile.setCurrentText(saved["profile"])
            self.profile.currentTextChanged.connect(state.schedule)
            state.collect(self.keep_form)
            return
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
        """{agent: its last options and agent arguments}: the current project's (what was entered there last), else
        those of no project."""
        if self.project:
            return self.project_entry(self.project)["forms"]
        return self.agent_forms()

    def agent_forms(self):
        """{agent: the options and agent arguments last entered for it}, whatever the project (the form at the
        start, before a project is shown)."""
        saved = self.state.section("launch")
        if not isinstance(saved.get("forms"), dict):
            saved["forms"] = {}
        return saved["forms"]

    def project_forms(self):
        """{project: {"agent": the agent chosen there last, "forms": {agent: options and agent arguments}}}, the
        PROJECTS_KEPT projects shown last; with the window's state (else for this run of the page only)."""
        if self.state is None:
            return self.__dict__.setdefault("unkept_projects", {})
        saved = self.state.section("launch")
        if not isinstance(saved.get("projects"), dict):
            saved["projects"] = {}
        return saved["projects"]

    def project_entry(self, path):
        projects = self.project_forms()
        entry = projects.get(path)
        if not isinstance(entry, dict) or not isinstance(entry.get("forms"), dict):
            entry = projects[path] = {"agent": self.agent.currentText(), "forms": {}}
        return entry

    def kept_form(self, path):
        """(agent, its options and agent arguments) entered last for project PATH, or None (not shown before)."""
        entry = self.project_forms().get(path)
        if not isinstance(entry, dict) or not isinstance(entry.get("forms"), dict):
            return None
        agent = entry.get("agent")
        saved = entry["forms"].get(agent)
        return (agent, saved) if agent in self.sai.agents and isinstance(saved, dict) else None

    def keep_project(self, path):
        """What the form holds now, as project PATH's (its tab is left): shown again when it comes back."""
        entry = self.project_entry(path)
        entry["agent"] = self.agent.currentText()
        entry["forms"][entry["agent"]] = self.form_state()
        projects = self.project_forms()
        projects[path] = projects.pop(path)  # the one left last, last
        while len(projects) > PROJECTS_KEPT:
            projects.pop(next(iter(projects)))
        if self.state is not None:
            self.state.schedule()

    def form_state(self):
        values = self.form.values()
        values.pop("--workspace", None)  # the project's
        return {"options": backend.argv_from_values(self.options, values),
                "arguments": scrub_arguments(self.agent_arguments())}

    def keep_form(self):
        saved = self.state.section("launch")
        if self.link is not None:
            # (the start itself is the board's, kept with it - what was typed here a moment ago first: this runs
            # before the board's drafts are collected)
            self.link.flush()
            saved["profile"] = self.profile.currentText()
            return
        agent = self.agent.currentText()
        saved["agent"], saved["profile"] = agent, self.profile.currentText()
        saved["host"] = self.host()
        self.agent_forms()[agent] = self.forms()[agent] = self.form_state()
        if self.project:
            self.project_entry(self.project)["agent"] = agent

    def agent_switched(self, agent):
        """Another agent: the form keeps this agent's options and shows the other's last ones (if any)."""
        if self.switching:  # the project's own agent shown (show_form): its form follows
            return
        self.forms()[self.current_agent] = self.form_state()
        self.current_agent = agent
        self.load_agent_form(agent)

    def load_agent_form(self, agent):
        saved = self.forms().get(agent)
        if not isinstance(saved, dict):  # not used in this project yet: the agent's last options elsewhere
            saved = self.agent_forms().get(agent)
        if isinstance(saved, dict):
            self.apply_form(saved)

    def apply_form(self, saved):
        """The form as SAVED says ({"options": launcher options, "arguments": agent arguments}); the workspace stays."""
        options = [str(o) for o in saved.get("options", []) if isinstance(o, str)] if isinstance(
            saved.get("options"), list) else []
        arguments = [str(a) for a in saved.get("arguments", []) if isinstance(a, str)] if isinstance(
            saved.get("arguments"), list) else []
        values, _ = backend.values_from_argv(self.options, options, backend.canonical_of(self.sai))
        values["--workspace"] = self.form.values().get("--workspace") or []
        self.form.set_values(values)
        self.agent_args.setText(shlex.join(scrub_arguments(arguments)))

    def show_form(self, agent, saved):
        """AGENT chosen and the form as SAVED says (apply_form): a project's own, not the agent's last elsewhere."""
        if agent in self.sai.agents and agent != self.agent.currentText():
            self.switching = True
            try:
                self.agent.setCurrentText(agent)
            finally:
                self.switching = False
        self.current_agent = self.agent.currentText()
        self.apply_form(saved)

    def set_project(self, path, running=None):
        """Start in the project PATH: its folder becomes the first (and only) workspace, and the form shows that
        project's values - as the tabs switch, each project keeps what was entered for it last. A project shown the
        first time shows what RUNNING ((agent, launcher options, agent arguments) of its session that runs) was
        started with, else, once read, its trusted .sandbox-ai.json's; else the form stays as it is."""
        previous, self.project = self.project, path
        if self.link is not None:
            # The Expert view: the project's board is its start (the window shows that one first). A project with
            # nothing of its own - no file, no change - shows what its session that runs was started with (RUNNING).
            if path and running:
                self.link.board_page.seed_start(path, *running)
            self.show_project_note()
            return
        waiting = self.seeding is not None and self.seeding[0] == previous and \
            self.seeding[1:] == (self.agent.currentText(), self.form_state())
        if previous and previous != path and not waiting:  # (left before its file was read: read again next time)
            self.keep_project(previous)
        if path and previous != path:
            self.show_project_form(path, running, reset=bool(previous))
        widget = self.form.widgets.get("--workspace")
        if widget is not None and self.host() == LOCAL:  # on another host the workspace is a folder there
            widget.set_value([path])
        self.show_project_note()

    def show_project_form(self, path, running=None, reset=False):
        """The form of project PATH (see set_project). RESET (another project was shown before): a project with
        nothing of its own starts from the defaults, never with the last project's mounts, devices or limits."""
        self.seeding = None
        kept = self.kept_form(path)
        if kept is not None:
            self.show_form(*kept)
        elif running:
            agent, options, arguments = running
            self.show_form(agent, {"options": list(options), "arguments": list(arguments)})
        else:  # its trusted file fills the form once read (project_file_read), unless it was changed meanwhile
            if reset:
                self.apply_form({})
            self.seeding = (path, self.agent.currentText(), self.form_state())

    def applied_file(self):
        """The values of the project's trusted .sandbox-ai.json when the launcher applies that file to this start (on
        this machine, the project the first workspace), else {}."""
        if self.host() != LOCAL or not self.project:
            return {}
        workspaces = self.form.values().get("--workspace") or []
        if not workspaces or not same_folder(workspaces[0], self.project):
            return {}
        return self.file_values.get(self.project) or {}

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
        if state == "trusted":  # its options: shown in the form of a project shown first, left out of argv()
            background(self.sai.review_project, project, timeout=STATE_WAIT,
                       done=lambda review, error: alive(self) and self.project_file_read(project, review, error))
        else:
            self.file_values.pop(project, None)
        texts = {"none": tr("No .sandbox-ai.json in this project: the options below are all there is."),
                 "trusted": tr("The launcher applies the project's trusted .sandbox-ai.json: the form shows its "
                               "options, the command line carries only what you add or change. To drop one of its "
                               "options, save the form as the project configuration."),
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

    def project_file_read(self, project, review, error):
        """The trusted .sandbox-ai.json of PROJECT, read: its values are known (argv), and a project shown the first
        time whose form nobody changed since shows them."""
        if error is not None or review is None or not review.trusted or not review.usable:
            self.file_values.pop(project, None)
            return
        if self.link is not None:  # (the board was read from the file, and its start names every option itself)
            return
        self.file_values[project] = backend.values_from_argv(self.options, review.options,
                                                             backend.canonical_of(self.sai))[0]
        seeding, self.seeding = self.seeding, None
        if seeding is not None and seeding[0] == project == self.project and \
                seeding[1:] == (self.agent.currentText(), self.form_state()):
            self.show_form(review.agent, {"options": review.options, "arguments": review.agent_args})
        self.update_preview()

    def argv(self):
        """The launcher's options of the form; for another host without the ones its launcher does not know (a start
        or dry run refuses those by name first: refused_options). What the project's trusted .sandbox-ai.json sets
        the same is left out: the launcher applies the file itself, and some options given twice are refused (a
        folder mounted twice). In the window (linked): the options of the board's start, which both views show."""
        if self.link is not None:
            from . import board as B
            self.link.flush()
            return B.start_argv(self.sai, self.link.board)[1]
        values = without_file_values(self.options, self.form.values(), self.applied_file())
        return backend.argv_for_host(self.options, values, self.known_options())

    def refused_options(self, title):
        """True (and said) when options set in the form are unknown to the chosen host's launcher: they would be left
        out, and leaving out a restrictive one (--workspace-ro, --no-provider-defaults) starts a weaker sandbox."""
        dropped = backend.dropped_options(self.options, self.form.values(), self.known_options())
        if not dropped:
            return False
        warn(self, title, tr("The sandbox-ai on {host} does not know these options: {list}. Clear them, or update "
                             "sandbox-ai there; none is left out without a word.", host=printable(self.host()),
                             list=", ".join(dropped)) + " " + tr(
            "Its options are read again now: if it was updated meanwhile, try once more."))
        # (what was read may be from before an update there: read again, for the next try)
        self.host_known.pop((self.host(), self.agent.currentText()), None)
        getattr(self.sai, "forget_host_options", lambda name: None)(self.host())
        self.read_host_options()
        return True

    def preset_images(self):
        """[(label, image)] of the sandbox-ai preset the start names (--preset): its values that stand as a service's
        image (presets: names "image"), as --preset-value gives them, else their defaults."""
        name = str(self.form.values().get("--preset") or "").strip()
        key = self.preset_choice.currentData() if hasattr(self, "preset_choice") else None
        try:
            preset = self.preset_catalogue.find(key) if key else None
        except ValueError:
            preset = None
        if not name or preset is None or preset.source != "sandbox-ai" or preset.name != name:
            return []
        given = dict(str(pair).partition("=")[::2] for pair in self.form.values().get("--preset-value") or [])
        names = dict(preset.value_names)
        found = []
        for placeholder in preset.placeholders:
            if getattr(placeholder, "names", "") != "image":
                continue
            value = str(given.get(names.get(placeholder.marker, ""), "") or placeholder.default or "").strip()
            if value:
                found.append((preset.text(placeholder.label), value))
        return found

    def services_unchecked(self, title, then):
        """True when the start must not go on now: its services (--service, the images a sandbox-ai preset's values
        name) are checked against your images on the chosen host first - read in the background, then THEN runs - or
        one names an image that is none of yours (said, naming the service, with the Images page offered)."""
        from . import board as B
        form = getattr(self, "form", None)  # (a page made without its form, as tests do: nothing to check)
        specs = list(form.values().get("--service") or []) if form is not None else []
        images_named = self.preset_images() if form is not None else []
        if not specs and not images_named:
            return False
        host, wanted = self.host(), (self.host(), tuple(specs), tuple(images_named))
        if getattr(self, "services_checked", None) == wanted:
            self.services_checked = None  # (once: the next start reads them again)
            return False

        def images():
            """Your images on HOST and the images of sandbox-ai's own Containerfiles there (built when the start
            needs one: prerequisites)."""
            rows = self.sai.user_images(host)
            try:
                templates = {str(row.get("image")) for row in self.sai.service_templates(host)}
            except Exception as _exc:  # noqa: BLE001 - none of its own known: checked as yours
                applog.swallowed("ui", "LaunchPage.services_unchecked: sandbox-ai's own templates", _exc)
                templates = set()
            return rows, templates

        def read(result, error):
            if not alive(self) or host != self.host():
                return
            rows, templates = result if error is None and isinstance(result, tuple) else (None, set())
            if error is not None or not isinstance(rows, list):
                warn(self, title, tr("Your images on {host} could not be read (sandbox-ai images list), so the "
                                     "services' images are not checked: {error}",
                                     host=tr("this computer") if host == LOCAL else printable(host),
                                     error=printable_text(error)[-300:] if error is not None else "?"))
                return
            mine = {str(row.get("name")) for row in rows if isinstance(row, dict)}
            problems = B.service_image_problems(self.sai, specs, mine, templates)
            problems += [tr("The preset's value \u201c{label}\u201d is {image}, none of your images (your images: "
                            "{mine}): choose one of yours, or add it (Images page, Your images).", label=label,
                            image=printable(image), mine=B.your_images_text(mine))
                         for label, image in images_named if image not in mine and image not in templates]
            if problems:
                applog.log("INFO", "start", "not started: a service's image is none of yours: "
                           + "; ".join(applog.redact(text)[:200] for text in problems))
                if confirm(self, title, "\n".join(problems), tr("Open the Images page"), reject=tr("Close")):
                    self.images_wanted.emit()
                return
            self.services_checked = wanted
            then()
        background(images, owner=self, timeout=120, done=read,
                   what=f"reading the images for services on {host} (sandbox-ai images list, templates)")
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
        """The command line of the start in its field. In the window (linked) it is the board's, shown by the link;
        a line being typed that cannot be read yet, or that means this very start, is left to the human."""
        if self.link is not None:
            return  # (the link shows the board's line when the board changed: show_board)
        args = self.agent_arguments()
        # (the same start from this machine's shell: on another host with `hosts run HOST --`)
        text = start_link.command_text(self.agent.currentText(), self.argv(), args, self.host())
        typed = self.line_text
        if typed and self.preview.hasFocus():
            from . import board as B
            try:
                host, agent, argv, agent_args = B.parse_command(self.sai, typed)
                same = (host or LOCAL, agent, backend.argv_from_values(self.options, backend.values_from_argv(
                    self.options, argv, backend.canonical_of(self.sai))[0]), agent_args) == \
                    (self.host(), self.agent.currentText(), backend.argv_from_values(self.options, self.form.values()),
                     args)
            except ValueError:
                same = True  # (not read yet: the human mends it)
            if same:
                return
        self.preview.show_text(text)

    def refresh_profiles(self):
        self.profile.clear()
        self.profile.addItems(sorted(self.sai.profiles()))

    def set_from_argv(self, agent, argv, agent_args=()):
        values, unknown = backend.values_from_argv(self.options, argv, backend.canonical_of(self.sai))
        if self.link is not None:
            # In the window: the whole start onto the board (one step of Undo); what this version does not know is
            # kept there on the part Other options, not left out.
            self.link.flush()
            self.link.board_page.take_start(agent, list(argv), list(agent_args))
            return
        self.agent.setCurrentText(agent)
        self.form.set_values(values)
        self.agent_args.setText(shlex.join(agent_args))
        if unknown:
            warn(self, tr("Options not shown"), tr(  # (no one waits for its OK: called from callbacks too)
                "These are not options of this sandbox-ai version and were left out:") + "\n" + " ".join(unknown))

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
            options = [str(option) for option in profile.get("options", [])]
            if self.link is not None and kept and "--workspace" not in backend.values_from_argv(
                    self.options, options, backend.canonical_of(self.sai))[0]:
                options += [token for folder in kept for token in ("--workspace", folder)]
            self.set_from_argv(profile.get("agent", "claude"), options, profile.get("agent_args", []))
            if self.link is None and kept and not self.form.values().get("--workspace"):
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
        host's sandbox-ai (load_project_on_host). In the window (linked): the Board's own - a folder opened is opened
        as the project's board (Open project folder…), the project's own file is shown and trusted by Apply."""
        if self.link is not None:
            board = self.link.board_page
            self.link.flush()
            if directory and same_folder(directory, board.directory):
                return board.apply()
            if board.open_request is not None and self.host() == LOCAL and not directory:
                return board.open_request()
            return board.open_project(directory)
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
            warn(self, tr("No project configuration"), printable_text(exc))
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
                 "\n".join([*(printable_text(problem) for problem in review.problems), "", *lines]), details=review.text())
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
                warn(self, tr("Not trusted"), printable_text(exc))
                return False
        # Its tab first (the project left keeps what was entered there), then the form shows the file's values.
        self.opened.emit(str(directory))
        project = self.project if same_folder(self.project, directory) else str(directory)
        self.file_values[project] = backend.values_from_argv(self.options, review.options,
                                                             backend.canonical_of(self.sai))[0]
        self.seeding = None
        self.show_form(review.agent, {"options": review.options, "arguments": review.agent_args})
        self.form.widgets["--workspace"].set_value([project])
        short_notice(self, tr("Project configuration"), tr("Trusted: {path}. The form starts in that folder; the "
                                                           "launcher applies the file's options itself.",
                                                           path=printable(review.path)))
        self.show_project_note()
        return True

    def load_project_on_host(self, directory=None):
        """A project folder on the chosen host (chosen from its folders, from the form's first workspace): its
        .sandbox-ai.json reviewed through that host's sandbox-ai as a file of this machine is (every key, what weakens,
        the changes against the version trusted last; host_trust) and trusted exactly as shown after a yes; then the
        form starts in that folder there."""
        host = self.host()
        if not directory:  # chosen from that host's folders (remote_chooser), starting at the first workspace
            workspaces = self.form.values().get("--workspace") or []
            directory = RC.choose_folder(self, host, tr("Project folder with .sandbox-ai.json"), "project",
                                         start=str(workspaces[0]) if workspaces else "", sai=self.sai)
            if not directory:
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
            short_notice(self, tr("Project configuration"), on_host(host, tr(
                "Trusted: {path}. The form starts in that folder; the launcher applies the file's options itself.",
                path=printable(path))))
        review_and_trust(self, self.sai, host, path, done=done, source=self.open_button)

    def dry_run(self):
        """The launcher's plan, nothing started: on the chosen host by its own launcher. In the window (linked): the
        board's dry run - the same start, the same check."""
        if self.link is not None:
            self.link.flush()
            return self.link.board_page.dry_run(also=self.dry_button)
        if self.refused_options(tr("Dry run")) or not self.require_workspace(tr("Dry run")):
            return
        asked = self.dry_run_asked()
        background(self.sai.host_dry_run, self.host(), self.agent.currentText(), self.argv(), owner=self,
                   source=self.dry_button, timeout=120,
                   done=lambda result, error: self.dry_run_done(result, error, asked))

    def dry_run_asked(self):
        """What a dry run is asked for (widgets.dry_run_answer): the agent and options, the host, the time."""
        return "form", (self.agent.currentText(), self.argv()), self.host(), time.strftime("%H:%M:%S")

    def dry_run_done(self, result, error, asked=None):
        """The launcher's plan (or refusal); one for a form changed meanwhile, or another host chosen, is said to be
        out of date, with the host and time it is from (R2-20)."""
        suffix, note = widgets.dry_run_answer(asked, self.dry_run_asked()) if asked is not None else ("", "")
        lead = note + "\n\n" if note else ""
        plan, message = result if error is None else (None, str(error))
        if isinstance(error, backend.HostUnreachable):  # no answer of that host: no refusal of its launcher
            warn(self, tr("{host} does not answer", host=printable(error.host)), lead + printable_text(message))
            return
        if plan is None:
            warn(self, tr("The launcher refused these options") + suffix, lead + printable_text(message))
            return
        show_text(self, tr("Plan (dry run)") + suffix,
                  lead + (printable_text(message) + "\n\n" if message else "") + json.dumps(plan, indent=2))

    def prerequisite_argv(self):
        return self.argv()

    def prerequisite_agent(self):
        return self.agent.currentText()

    def prerequisite_own_servers(self):
        """The MCP servers the workspace's project file defines (the launcher reads it at the start): nothing to add."""
        folder = self.workspace() if self.host() == LOCAL else None
        try:
            with open(os.path.join(folder, ".sandbox-ai.json"), "rb") as stream:
                document = json.loads(stream.read(1 << 20))
        except (OSError, TypeError, ValueError, RecursionError):
            return set()
        servers = document.get("mcp_servers") if isinstance(document, dict) else None
        return {name for name in servers if isinstance(name, str)} if isinstance(servers, dict) else set()

    def prerequisite_context(self):
        return (self.host(), self.agent.currentText(), str(self.workspace() or ""))

    def folder_made(self, then):
        """True when the workspace is there (or is another host's); else, after a short question, it is made (with
        the folders above it, checked as a start checks a workspace) and THEN runs - False meanwhile. A start used to
        end in "[Errno 2] No such file or directory" for a folder not made yet."""
        folder = self.workspace() if self.host() == LOCAL else None
        folder = os.path.expanduser(folder) if folder else None
        if not folder or os.path.isdir(folder) or os.path.lexists(folder):
            return True  # (a file or a link there: the launcher says why it is refused)
        if not confirm(self, tr("Create the folder"), tr(
                "This folder does not exist yet: {list}\n\nCreate it (with the folders above it) and go on?",
                list=printable(folder)), tr("Create")):
            return False

        said = []  # the launcher's warnings of a risky folder you chose (made all the same: you decide)

        def made(result, error):
            if error is not None:
                warn(self, tr("Not created"), printable_text(error))
                return
            if said:
                from . import board as B
                warn(self, tr("Created, with a warning"), B.path_warnings(said), modal=False)
            then()
        background(self.sai.create_folders, [folder], said, owner=self, work=True, timeout=60, done=made)
        return False

    def log_start(self, session, embedded):
        """What Start was asked: the agent, host, session, workspace, where and the options (secrets masked)."""
        workspace = self.workspace()
        try:
            argv = self.argv()
            options = applog.argv_text(argv, 1500) or "-"
            if not workspace and "--workspace" in argv[:-1]:  # (the project chosen fills an empty field)
                workspace = argv[argv.index("--workspace") + 1]
        except Exception as exc:  # noqa: BLE001 - the start itself says what is wrong with them
            options = f"(not read: {applog.error_text(exc)})"
        applog.log("INFO", "start", f"Start: {self.agent.currentText()} on {printable(self.host())}"
                   + (f" as session {printable(session)}" if session else "") + f", workspace "
                   f"{printable(workspace or '-')}, " + ("in a tab of the app" if embedded else "where the "
                                                         "settings say") + f", options {options}")

    def start(self, session=None, embedded=False):
        if self.link is not None:
            # The Expert view of the Start page: the board's start - one start, whichever view it is started from
            # (what was typed a moment ago is on the board first). What it waits for is shown on the Board.
            return self.link.start(embedded=embedded, session=session)
        self.log_start(session, embedded)
        self.starting_session = session or None  # (the login check asks whether that session has a login of its own)
        if self.refused_options(tr("Not started")) or not self.require_workspace(tr("Not started")):
            applog.log("INFO", "start", "not started: the options or the workspace are refused (the message says why)")
            return False
        if self.services_unchecked(tr("Not started"), lambda: self.start(session, embedded)):
            applog.log("INFO", "start", "the start waits: its services' images are checked against your images first "
                                        "(or one is none of yours: said)")
            return False
        if not self.folder_made(lambda: self.start(session, embedded)):
            applog.log("INFO", "start", "the start waits: its workspace is made first (or was not)")
            return False
        # What the options need on the host (an identity, an image variant, a VPN profile, MCP servers) made first;
        # then the start goes on by itself.
        if self.check_prerequisites(then=lambda: self.start(session, embedded)):
            applog.log("INFO", "start", "the start does not go on now: what it needs on the host is read, made or "
                                        "missing (the prerequisites' lines say which: it goes on by itself once "
                                        "made, not when something is missing)")
            return False
        # Last: is a session of that name there already (here or on the chosen host)? The same agent's that runs: its
        # terminal; anything else in the way is said first, with what can be done (session_start.py).
        context = self.prerequisite_context()

        def unchanged():
            return self.prerequisite_context() == context
        if session and self.session_in_the_way(session, embedded, lambda name: unchanged() and self.start_now(
                name, embedded), same=unchanged):
            applog.log("INFO", "start", f"the start waits: what is there under the session name {printable(session)} "
                                        "is looked up first (or was said)")
            return False
        return self.start_now(session, embedded)

    def start_now(self, session, embedded):
        """The start itself, once every check passed: its command in a terminal (a tab of the app or a window)."""
        try:
            host, agent = self.host(), self.agent.currentText()
            ended = self.start_watch(host, agent, session, embedded,
                                     lambda name: self.start(name, embedded)) if embedded else None
            if host != LOCAL:  # ssh -tt there: its tmux keeps an interactive start when the tab closes
                argv = self.sai.host_launch_argv(host, agent, self.argv(), self.agent_arguments(), session)
                title = f"{printable(host)}: {agent}" + (f" ({session})" if session else "")
                self.start_terminal(argv, title, embedded, None, ended=ended)
                self.session_started(session)
                return True
            argv = self.sai.launch_argv(agent, self.argv(), self.agent_arguments(), session)
            title = f"{agent}" + (f" ({session})" if session else "")
            self.start_terminal(argv, title, embedded, self.workspace(), ended=ended)
            self.session_started(session)
        except Exception as exc:  # noqa: BLE001 - a start that cannot be run is a message, the window stays
            applog.log("WARNING", "start", f"the start failed: {applog.error_text(exc)}")
            warn(self, tr("Not started"), printable_text(exc))
            return False
        return True

    def start_in_app(self):
        """In a terminal tab, not as a session (kept for callers)."""
        return self.start(embedded=True)

    def start_session(self):
        """As a session, where the Start controls say (kept for callers)."""
        self.start_controls.as_session.setChecked(True)
        return self.start_controls.start()

    def write_project(self):
        if self.link is not None:  # the board's Apply: the same file, shown with what changes, written and trusted
            self.link.flush()
            return self.link.board_page.apply()
        if self.host() != LOCAL:
            return
        document, main = self.sai.project_document(self.agent.currentText(), self.options, self.form.values(),
                                                   self.agent_arguments())
        if not main:
            QMessageBox.information(self, tr("Project configuration"),
                                    tr("Choose a workspace first: the file is written there."))
            return
        if not self.folder_made(self.write_project):
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
            warn(self, tr("Not written"), printable_text(error))
            return
        short_notice(self, tr("Project configuration"), tr("Written and trusted: {path}", path=printable(path)))
        self.show_project_note()  # trusted now: its values are read again




def given_network(text):
    """What a run may reach now, from `sandbox-ai net show RUN --json` (TEXT): {"hosts": names and wildcards, "tcp":
    ["ADDRESS:PORT"], "nets": [...], "host_ports": [...]}; None when TEXT is no such answer (an older sandbox-ai)."""
    try:
        shown = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(shown, dict):
        return None
    def items(key):
        value = shown.get(key)
        return value if isinstance(value, list) else []
    hosts = {str(n) for e in items("https") + items("tcp") if isinstance(e, dict)
             for n in (e.get("names") if isinstance(e.get("names"), list) else [])}
    hosts |= {str(w) for w in items("host_wildcards")}
    tcp = [f"{e.get('address')}:{e.get('port')}" for e in items("tcp") if isinstance(e, dict)]
    return {"hosts": hosts, "tcp": tcp, "nets": [str(n) for n in items("all_ports")],
            "host_ports": [str(p) for p in items("host_ports")]}


class NetworkDialog(QDialog):
    def __init__(self, parent, presets, title, supported=None, current=None, op="allow", draft=None):
        """CURRENT (given_network): what the run may reach now - shown above; to allow (OP), its host sets ticked and
        greyed, so a second Erlauben… never looks as if the first was lost, and only what is new is sent; to revoke,
        the host sets it does not have greyed. DRAFT: the key of what was typed and not sent (drafts.py), filled in
        again. A Git login or an API key is given from Credentials (one place for them, 2026-11-24): Git login or API
        key… closes this dialog (what was typed is kept) with WANTS_CREDENTIALS set."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.supported = set(supported or ())
        form = QFormLayout(self)
        self.given = QLabel()
        self.given.setWordWrap(True)
        self.given.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.given.setProperty("role", "notice")
        parts = []
        if current:
            for label, values in ((tr("names"), sorted(current.get("hosts") or ())), ("IP:PORT", current.get("tcp")),
                                  (tr("networks"), current.get("nets")),
                                  (tr("ports on this computer"), current.get("host_ports"))):
                if values:
                    parts.append(f"{label}: {', '.join(values)}")
        self.given.setText(tr("Allowed already during this run (What it may reach… lists everything): {what}",
                              what="; ".join(parts)) if parts else "")
        form.addRow(self.given)
        self.given.setVisible(bool(parts))
        self.hosts = QLineEdit()
        self.hosts.setPlaceholderText("api.example.org *.example.net pypi.org")
        self.hosts.setToolTip(safe_tooltip(tr("Names separated by commas or spaces. *.DOMAIN opens every name below "
                                              "DOMAIN, each as it is looked up; below a shared service such as "
                                              "*.blob.core.windows.net, where anyone can rent a name, only after you "
                                              "confirm that risk (never a public suffix such as *.co.uk).")))
        self.tcp = QLineEdit()
        self.tcp.setPlaceholderText("192.0.2.10:5432 192.0.2.11:22")
        self.tcp.setToolTip(safe_tooltip(tr("One or more, separated by commas or spaces")))
        self.nets = QLineEdit()
        self.nets.setPlaceholderText("192.0.2.0/24 198.51.100.0/24")
        self.nets.setToolTip(safe_tooltip(tr("One or more, separated by commas or spaces")))
        form.addRow(tr("Host names (TCP/443)"), self.hosts)
        form.addRow(tr("IP:PORT (TCP)"), self.tcp)
        form.addRow(tr("Addresses or networks, every port (--allow-net)"), self.nets)
        # What later versions of sandbox-ai can change during the run: shown only where the installed one can.
        self.domains = QLineEdit()
        self.domains.setPlaceholderText("example.org example.net")
        self.domains.setToolTip(safe_tooltip(tr("Domains separated by commas or spaces: each one and every name below it "
                                                "(--allow-domain: the name and *.DOMAIN in one; not for shared "
                                                "hosting such as github.io, nor public suffixes such as co.uk).")))
        if "--allow-domain" in self.supported:
            form.addRow(tr("Domains with every name below (TCP/443)"), self.domains)
        self.host_ports = QLineEdit()
        self.host_ports.setPlaceholderText("3000 11434")
        self.host_ports.setToolTip(safe_tooltip(tr("Ports on this computer's loopback (a local Git server, an LLM "
                                                   "server), reached through a relay that only you start; separated "
                                                   "by commas or spaces")))
        self.dns = QCheckBox(tr("Real DNS lookups (each one logged; addresses stay closed)"))
        self.nameservers = QLineEdit()
        self.nameservers.setPlaceholderText("192.0.2.53 192.0.2.54")
        self.nameservers.setToolTip(safe_tooltip(tr("Resolvers instead of the defaults (--nameserver; implies real "
                                                    "DNS), separated by commas or spaces")))
        self.keep = QCheckBox(tr("Keep it for later starts (into the project's .sandbox-ai.json)"))
        self.keep.setToolTip(safe_tooltip(tr("net allow --keep: the options go into the project's .sandbox-ai.json, "
                                             "only while that file is exactly what you trusted (the new file is "
                                             "trusted at once); logins are kept as references, never as tokens")))
        if "--allow-host-port" in self.supported:
            form.addRow(tr("Ports on this computer"), self.host_ports)
        if "--dns" in self.supported:
            form.addRow(tr("DNS"), self.dns)
        if "--nameserver" in self.supported:
            form.addRow(tr("Nameservers"), self.nameservers)
        self.presets = {}
        if presets:  # --allow-github ...: a fixed set of names each (TCP/443); "preset" is a start template
            form.addRow(plain_label(tr("Host sets (TCP/443)")))
        have = set((current or {}).get("hosts") or ())
        for name, hosts in presets.items():
            box = QCheckBox(", ".join(hosts)[:80])
            if op == "allow" and hosts and have.issuperset(hosts):  # given already: shown so, never sent again
                box.setChecked(True)
                box.setEnabled(False)
                box.setToolTip(safe_tooltip(tr("allowed already during this run")))
            elif op != "allow" and current is not None and not have.intersection(hosts):  # nothing of it to take back
                box.setEnabled(False)
                box.setToolTip(safe_tooltip(tr("not allowed during this run")))
            form.addRow(f"--allow-{name}", box)
            self.presets[name] = box
        if "--keep" in self.supported:  # net allow only (a revoke keeps nothing)
            form.addRow(tr("Later starts"), self.keep)
        # A Git login or an API key for this run: Credentials › Give to a running sandbox… (the one place for them).
        self.wants_credentials = False
        self.credentials_link = QPushButton(tr("Git login or API key…"))
        self.credentials_link.setToolTip(safe_tooltip(tr(
            "Given from Credentials: Git logins › Give to a running sandbox… (a stored login, or one from a variable, "
            "a secret or a file) and Secrets › Give to a running sandbox… (as an API key). Closes this dialog; what you "
            "typed here is kept.")))
        self.credentials_link.clicked.connect(lambda _=False: self.to_credentials())
        if op == "allow":
            form.addRow(tr("Logins"), self.credentials_link)
        # OK with nothing to send, or half a Git login: said here, the dialog stays (it closed without a word, and a
        # Git login without its token's place was dropped from what was sent)
        self.op = op
        self.problem = plain_label("", role="notice-error")
        self.problem.hide()
        form.addRow(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.checked_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.draft = None if draft is None else drafts.Draft(self, draft, {
            "hosts": self.hosts, "tcp": self.tcp, "nets": self.nets, "domains": self.domains,
            "host_ports": self.host_ports, "dns": self.dns, "nameservers": self.nameservers, "keep": self.keep,
            **{f"preset:{name}": box for name, box in self.presets.items()}})

    def to_credentials(self):
        """Git login or API key…: closed (not sent), the window opens Credentials for this host."""
        self.wants_credentials = True
        self.reject()

    def arguments(self):
        """The options of what was typed: each field's items (separated by commas or spaces, each once) an option
        of their own, which every sandbox-ai version takes."""
        def each(option, field):
            return [t for item in backend.list_items(field.text()) for t in (option, item)]
        args = each("--allow-host", self.hosts)
        if "--allow-domain" in self.supported:
            args += each("--allow-domain", self.domains)
        args += each("--allow-tcp", self.tcp) + each("--allow", self.nets)
        args += [f"--allow-{name}" for name, box in self.presets.items() if box.isChecked() and box.isEnabled()]
        if "--allow-host-port" in self.supported:
            args += each("--allow-host-port", self.host_ports)
        if "--dns" in self.supported and self.dns.isChecked():
            args.append("--dns")
        if "--nameserver" in self.supported:
            args += each("--nameserver", self.nameservers)
        return args

    def why_not(self):
        """Why OK cannot send this yet ("": it can): nothing typed or chosen."""
        if not self.arguments():
            return tr("Type or tick what to allow first.") if self.op == "allow" else \
                tr("Type or tick what to take back first.")
        return ""

    def checked_accept(self):
        why = self.why_not()
        if why:
            self.problem.setText(why)
            self.problem.show()
            return
        self.accept()

    def kept(self):
        """Whether what is allowed is also kept for later starts (net allow --keep)."""
        return "--keep" in self.supported and self.keep.isChecked()


DEVICE_EXAMPLE = "/dev/..."  # the same in every language
LOCAL = "local"  # Instance.host of this machine's own sandboxes


class DeviceDialog(QDialog):
    """A device for a running sandbox: the devices its host has (SCANNED: `devices scan` there, device_picker) grouped
    by kind with readable names - those the run has (LISTING: `devices list` of the run), refused ones and a GPU kind
    (--gpu cannot change while it runs) greyed out with why -, then this machine's other devices under /dev
    (CANDIDATES), refused ones greyed out with the launcher's reason, warned ones with what they expose; those ADDED
    during the run already greyed out and listed above (`devices list` of the run's host, read as the dialog opens); or
    a path typed in (DRAFT: kept until sent, drafts.py). PROBLEM: why the host's devices could not be read (said in a
    line: a path may still be typed)."""

    def __init__(self, parent, candidates, title=None, added=None, draft=None, scanned=None, listing=None,
                 problem=None, host="local"):
        super().__init__(parent)
        self.setWindowTitle(title or tr("Add device"))
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(DEVICE_EXAMPLE)
        added = [str(path) for path in added or ()]
        self.given = plain_label(tr("Added during this run already: {list}", list=", ".join(map(printable, added)))
                                 if added else "", role="notice", selectable=True)
        self.given.setVisible(bool(added))
        scanned = list(scanned or [])
        values, nodes = DP.held(listing or [])
        detected = set()
        for kind in DP.KINDS:
            entries = [entry for entry in scanned if entry["kind"] == kind]
            if entries:
                self.header(tr(DP.TITLES[kind]))
            for entry in entries:
                detected.update(entry["nodes"])
                have = DP.has(entry, values, nodes)
                why = entry["refused"] and tr("sandbox-ai refuses it: {why}", why=entry["refused"]) or \
                    (have and tr("added already during this run")) or \
                    (entry["option"] == "--gpu" and tr("--gpu cannot change while it runs: set it on the part and "
                                                       "start again"))
                shown = entry["label"] + (f"  ({tr('refused')})" if entry["refused"] else
                                          f"  ({tr('added already')})" if have else
                                          f"  ({tr('restart needed')})" if entry["option"] == "--gpu" else
                                          f"  ({entry['note']})" if entry.get("note") else "")
                item = QListWidgetItem(shown)
                item.setData(Qt.ItemDataRole.UserRole, entry["value"])
                item.setToolTip(safe_tooltip(why or entry["value"]))
                if why:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
                self.list.addItem(item)
        candidates = [c for c in candidates if c[0] not in detected and c[0] not in values]
        if candidates and scanned:
            self.header(tr("Other devices under /dev"))
        self.problem = plain_label(DP.note(host, scanned, problem) if problem is not None or (scanned == [] and
                                                                                               not candidates) else "",
                                   role="secondary", selectable=True)
        self.problem.setVisible(bool(self.problem.text()))
        for path, why, note in candidates:
            have = path in added
            item = QListWidgetItem(path + (f"  ({tr('refused')}: {why})" if why else
                                           f"  ({tr('added already')})" if have else f"  ({note})" if note else ""))
            item.setData(Qt.ItemDataRole.UserRole, path)
            if why or have:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(safe_tooltip(tr("sandbox-ai refuses it: {why}", why=why) if why else
                                             tr("added already during this run")))
            self.list.addItem(item)
        self.list.itemSelectionChanged.connect(lambda: self.edit.setText(
            self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else self.edit.text()))
        layout.addWidget(plain_label(tr("Devices weaken the isolation: a service on the host reached through a host "
                                        "port is safer. Refused devices are greyed out.")))
        layout.addWidget(self.given)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.problem)
        self.list.setVisible(self.list.count() > 0)  # (nothing detected, the host not read: typed in)
        layout.addWidget(self.edit)
        # OK with nothing chosen or typed: said here, the dialog stays (it closed without a word)
        self.missing = plain_label(tr("Choose a device in the list or type its path first (a path under /dev/, or "
                                      "usb:VENDOR:PRODUCT[:SERIAL])."), role="notice-error")
        self.missing.hide()
        layout.addWidget(self.missing)
        self.edit.textChanged.connect(lambda _="": self.missing.hide())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(lambda: self.accept() if self.path() else self.missing.show())
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 480)
        self.draft = None if draft is None else drafts.Draft(self, draft, {"path": self.edit})

    def header(self, text):
        """A group's name in the list: a line that cannot be chosen."""
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.list.addItem(item)

    def path(self):
        return self.edit.text().strip()


class WorkspaceDialog(QDialog):
    """A host folder or a single file for a running sandbox (sandbox-ai workspace add): the path, a name under
    /workspaces (optional; checked as sandbox-ai checks --name), read-only or not. For a sandbox on another HOST the
    path is that host's: chosen from its folders (remote_chooser), never from this computer's. ADDED: the workspaces
    added during the run already (`workspace list` of its host, read as the dialog opens: listed above). DRAFT: what
    was typed and not sent is kept until sent (drafts.py)."""

    def __init__(self, parent, run, host="local", added=None, draft=None):
        super().__init__(parent)
        self.host = host
        remote = host != "local"
        self.setWindowTitle(tr("Add workspace to {run} on {host}", run=printable(run), host=printable(host)) if remote
                            else tr("Add workspace to {run}", run=printable(run)))
        form = QFormLayout(self)
        added = [entry for entry in added or () if isinstance(entry, dict)]
        self.given = plain_label(tr("Added during this run already: {list}", list=", ".join(map(workspace_label, added)))
                                 if added else "", role="notice", selectable=True)
        self.given.setVisible(bool(added))
        form.addRow(self.given)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(tr("/path/to/a/folder or /path/to/a/file or sshfs://user@machine/path"))
        self.edit.setAccessibleName(tr("Folder or file"))
        row = QHBoxLayout()
        row.addWidget(self.edit, 1)
        self.browse_buttons = []
        for text, slot in ((tr("Folder…"), self.browse), (tr("File…"), self.browse_file)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, s=slot: s())
            if remote:
                button.setToolTip(safe_tooltip(tr("Lists the folders of {host}", host=printable(host))))
            row.addWidget(button)
            self.browse_buttons.append(button)
        # A folder of another machine (sshfs://...), built from its parts: the start's host mounts it with sshfs.
        self.remote_button = QPushButton(tr("Other machine…"))
        self.remote_button.setToolTip(tr("A folder of another machine (sshfs://user@machine/path): the start's host "
                                         "mounts it with sshfs, the agent gets only the files"))
        self.remote_button.clicked.connect(lambda _=False: self.choose_remote())
        row.addWidget(self.remote_button)
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
        self.problem = plain_label(role="notice-warn", selectable=True)
        self.problem.hide()
        form.addRow(self.problem)
        # A folder of another machine (sshfs://): what it means, under the field (its host mounts it with sshfs).
        self.remote_note = plain_label(role="notice-warn", selectable=True)
        self.remote_note.hide()
        form.addRow(self.remote_note)
        self.edit.textChanged.connect(lambda _="": self.check())
        self.read_only.toggled.connect(lambda _=False: self.check())
        form.addRow(explanation(tr("It appears at /workspaces/NAME until the sandbox ends; a session keeps a folder "
                                   "for a restart (with its name) unless it is read-only, a file only for this run. "
                                   "sandbox-ai checks it as at a start: private places, package stores and the "
                                   "policy are refused.")))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.checked_accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        self.resize(560, 0)
        self.draft = None if draft is None else drafts.Draft(self, draft, {
            "path": self.edit, "name": self.name_edit, "read_only": self.read_only})

    def browse(self):
        path = RC.choose_folder(self, self.host, tr("Folder for the sandbox"), key="workspace",
                                start=self.directory())
        if path:
            self.edit.setText(path)

    def browse_file(self):
        path = RC.choose_file(self, self.host, tr("File for the sandbox"), key="workspace-file",
                              start=self.directory())
        if path:
            self.edit.setText(path)

    def choose_remote(self):
        """A folder of another machine (sshfs://...), built from its parts (remote_source.RemoteSourceDialog)."""
        from . import remote_source
        dialog = remote_source.RemoteSourceDialog(self, self.directory())
        if widgets.exec_dropped(dialog) == QDialog.DialogCode.Accepted:
            self.edit.setText(dialog.source())

    def check(self):
        """A name sandbox-ai would refuse is said at once, and OK waits for a good one; a folder of another machine
        is said with what it means (and a source sandbox-ai would refuse with why)."""
        from . import remote_source
        name = self.name()
        bad = bool(name) and not backend.valid_workspace_name(name)
        problem = tr("A name is 1-64 letters, digits, _ . -, starting with a letter or digit.") if bad else ""
        value = self.directory()
        note = ""
        if remote_source.is_remote(value):
            try:
                remote_source.parse(value)
                note = remote_source.warning(value, self.read_only.isChecked())
            except ValueError as exc:
                problem, bad = str(exc), True
        self.problem.setText(problem)
        self.problem.setVisible(bool(problem))
        self.remote_note.setText(note)
        self.remote_note.setVisible(bool(note))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not bad)

    def checked_accept(self):
        """OK: with the folder left empty it says what to choose and stays (it closed without a word)."""
        if not self.directory():
            self.problem.setText(tr("Choose or type a folder or file first (Folder…, File… or Other machine…)."))
            self.problem.show()
            return
        self.accept()

    def directory(self):
        return self.edit.text().strip()

    def name(self):
        return self.name_edit.text().strip()


HELPER_TITLES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "hermes": "Hermes Agent",
                 "dsh": "DeepSeek Harness", "qwen": "Qwen Code", "goose": "Goose", "openhands": "OpenHands", "nanobot": "nanobot", "letta": "Letta Code",
                 "opencode2": "OpenCode 2",
                 "pi": "Pi"}


COPY_CHOICE = "copy:"  # LiveMcpDialog's choice of a server of another host (its data)


def blocked_copy(result):
    """Whether `mcp copy --json` RESULT says something stood in the way (nothing was copied then)."""
    return any(isinstance(plan, dict) and plan.get("problems") for plan in (result or {}).get("servers") or [])


class LiveMcpDialog(QDialog):
    """Add MCP server…: a server defined on the start's HOST (SERVERS: [(name, status)] of `sandbox-ai mcp list`
    there) or another agent as a helper (any agent sandbox-ai starts), into the running sandbox of START now (sandbox-ai
    mcp attach … --live) and kept for its later starts. PROBLEM: why the servers could not be read. ATTACHED: the
    servers the start reaches now (its rows' mcp_servers / mcp_running): listed above, greyed in the choice - opened
    again, the dialog shows what was attached before."""

    def __init__(self, parent, start, host=LOCAL, servers=(), agent="", problem=None, attached=()):
        super().__init__(parent)
        self.setWindowTitle(tr("Add MCP server to {start} on {host}", start=printable(start), host=printable(host))
                            if host != LOCAL else tr("Add MCP server to {start}", start=printable(start)))
        form = QFormLayout(self)
        attached = [str(name) for name in attached or ()]
        self.given = plain_label(tr("It reaches these already: {list}", list=", ".join(
            tr("{agent} as a helper ({name})", agent=HELPER_TITLES[backend.helper_of(name)], name=printable(name))
            if backend.helper_of(name) in HELPER_TITLES else printable(name) for name in attached))
            if attached else "", role="notice", selectable=True)
        self.given.setVisible(bool(attached))
        form.addRow(self.given)
        self.choice = QComboBox()
        self.choice.setAccessibleName(tr("MCP server"))
        for name, status in servers:
            if re.fullmatch(backend.MCP_NAME, str(name)) and not backend.helper_of(name):
                have = name in attached
                self.choice.addItem(printable(f"{name} ({tr('attached already')})" if have else
                                              f"{name} ({status})" if status else name), "server:" + name)
                if have:  # never offered as new
                    widgets.set_row_enabled(self.choice, self.choice.count() - 1, False)
                    widgets.set_row_tip(self.choice, self.choice.count() - 1,
                                        tr("It reaches it already: Remove MCP server… takes it back"))
        # A server of another host, copied here first (sandbox-ai mcp copy: checked, then asked) - what a host without
        # servers of its own offers (reported on coding-101)
        self.choice.addItem(tr("From another host…"), COPY_CHOICE)
        self.choice.setItemData(self.choice.count() - 1, tr("A server of another host, copied to this one first: what "
                                                            "it takes is checked and shown before anything is copied"),
                                Qt.ItemDataRole.ToolTipRole)
        for helper in backend.LIVE_HELPERS:
            self.choice.addItem(tr("{agent} as a helper", agent=HELPER_TITLES[helper]), "helper:" + helper)
        first = next((index for index in range(self.choice.count()) if widgets.row_enabled(self.choice, index)), 0)
        self.choice.setCurrentIndex(first)
        self.choice.setToolTip(tr("A server of sandbox-ai mcp list there, or another agent as a helper (an MCP "
                                  "server in a sandbox of its own on the same main workspace)"))
        form.addRow(tr("MCP server"), self.choice)
        if problem:
            form.addRow(plain_label(tr("The servers there could not be read: {error}", error=printable_text(problem)[:300]),
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
        """("server", NAME), ("helper", AGENT) or ("copy", ""): a server of another host."""
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
    if entry.get("remote"):
        marks.append(tr("sshfs"))
    return f"{printable(entry.get('sandbox'))} ← {printable(entry.get('remote') or entry.get('host'))} " \
           f"({', '.join(marks)})"


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
        form.addRow(explanation(tr("For the rest of the run; empty: unchanged, none: no limit.")))
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
                "starting": N_("starting"), "failed": N_("failed")}
DEMO_PORT = 4000  # the emulated device's port unless --port says otherwise (sandbox-ai demo-device)
STOP_WAIT = 30  # seconds sandbox-ai stop waits for a start to end unless --wait says otherwise
REMOTE_SECONDS = 60  # between two looks at the other hosts' starts (over SSH) while the page is hidden
REMOTE_SHOWN_SECONDS = 10  # the same while the page is shown: the fleet at a glance
HIDDEN_EVERY = 4  # the Sessions page hidden: this account's starts are read every 4th tick of sandboxes_interval
REMOTE_BACKOFF = 300  # seconds at most between two looks at a host that does not answer (doubled each time)
START_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}"  # a start's name as sandbox-ai takes it (rename NEW)
ENDED_STATES = ("stopped", "ended")
ACTIVE_STATES = ("running", "paused", "starting")
STOP_ALL_STATES = ("running", "paused", "orphaned", "starting")  # what Stop all… stops (sandbox-ai stop --all)
SESSION_ACTIONS = (("attach", N_("Attach")), ("attach-app", N_("Attach in the app")), ("shell", N_("Shell")),
                   ("pause", N_("Pause")), ("resume", N_("Resume")), ("delete", N_("Delete")))
# The terminal actions of a running start: the same starts can have them (a session in tmux, running).
TERMINAL_ACTIONS = ("attach", "attach-app", "shell")
ACTION_TITLES = {**dict(SESSION_ACTIONS), "stop": N_("Stop")}
BULK_ACTIONS = ("stop", "pause", "resume", "delete")  # what acts on several starts at once (Ctrl, Shift)
# The kill switch (sandbox-ai net cut): its modes as the Sessions page offers them (mode, the menu's text, what it
# does), and what the State column says while one holds.
CUT_CHOICES = (
    ("remote", N_("Cut the internet"),
     N_("Only the local network and the host's ports the sandbox may reach stay reachable")),
    ("local", N_("Cut the local network and host ports"),
     N_("Only the internet the sandbox may reach stays reachable")),
    ("api", N_("Only the model API"),
     N_("Only its model API stays reachable: its provider's hosts as the start opened them, or its --llm server")),
    ("all", N_("Cut everything"), N_("Everything is cut, the model API too: the emergency stop")),
    ("off", N_("Lift the cut"), N_("It reaches again what it may reach")),
)
CUT_STATES = {"remote": N_("internet cut"), "local": N_("local network cut"), "api": N_("only the model API"),
              "all": N_("network cut")}
EMERGENCY_KEYS = widgets.EMERGENCY_KEYS  # the window's emergency stop: every sandbox's network cut at once
EMERGENCY_HELP = N_("Cut the network of every running sandbox at once, the model API too, on this machine and on every "
                    "host (sandbox-ai net cut --all-runs all); no question. File › Lift every cut gives it back.")
REMOTE_WAIT = 60  # seconds one host may take to list its starts (the hosts are asked at the same time, each apart)
# What hangs below a start on the Sessions page (start_tree.related), by its kind.
PART_TITLES = {"mcp": N_("MCP server"), "helper": N_("Helper"), "pod": N_("Pod"), "companion": N_("Companion"),
               "team": N_("Team"), "serves": N_("Serves")}
GROUPS_KEPT = 400  # groups of the Sessions page whose fold is kept (the ones changed last)
# Why a start is shown below another one (start_tree.owners' HOW), in its tooltip.
BELONGS_HELP = {
    "started": N_("Started for {name}: it belongs to that start and is shown below it."),
    "uses": N_("{name} uses it: it belongs to that start and is shown below it."),
    "listed": N_("It belongs to {name} (as sandbox-ai list shows it) and is shown below it."),
    "helper": N_("A helper of {name}: it belongs to that start and is shown below it."),
    "team": N_("A member of the team {name} leads: shown below the lead."),
    "time": N_("Shown below {name}, which started at the same time: the sandbox-ai that started it did not record "
               "whom it belongs to, so this is a guess."),
}
ORPHANS_HELP = N_("These MCP servers belong to no start listed here: started on their own (sandbox-ai mcp start), or "
                  "the start they were started for ended and none uses them. Stop one when nothing needs it.")


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
    caches = [e for e in listed("caches") if isinstance(e, dict) and isinstance(e.get("bytes"), int)]
    if caches:
        lines.append(tr("the download caches in {count} kept HOME(s), {size}: {where}", count=len(
            {str(e.get("name")) for e in caches}), size=bytes_text(sum(e["bytes"] for e in caches)),
            where=", ".join(f"{printable(str(e.get('name')))} {printable(str(e.get('path')))}" for e in caches[:8])
            + (" …" if len(caches) > 8 else "")))
    compacted = result.get("compacted") if isinstance(result.get("compacted"), dict) else {}
    compressed = [e for e in compacted.get("compressed") or [] if isinstance(e, dict)]
    split = [e for e in compacted.get("split") or [] if isinstance(e, dict)]
    if compressed or split:
        lines.append(tr("compressed, nothing removed: {count} log(s) of ended runs and the older part of {split} large "
                        "shared log(s), {size}", count=len(compressed), split=len(split),
                        size=bytes_text(sum(e.get("bytes") or 0 for e in compressed + split))))
    for e in listed("log_folders"):
        if not isinstance(e, dict):
            continue
        project = e.get("project")
        where = (tr("its project {path} is gone", path=printable(str(project))) if e.get("project_gone") else
                 tr("its project {path}", path=printable(str(project)))) if isinstance(project, str) else ""
        lines.append(tr("the log folder {name} (last written {time}{project}), {size}", name=printable(str(e.get("name"))),
                        time=history_time(e.get("last_written")), project=", " + where if where else "",
                        size=bytes_text(e.get("bytes"))))
    lines += [tr("the MCP root file system {sha} (no MCP server uses it), {size}", sha=printable(str(e.get("sha256")))[:12],
                 size=bytes_text(e.get("bytes"))) for e in listed("rootfs") if isinstance(e, dict)]
    # the storage rules of the host's defaults (Settings › Storage): what they do (or would) with this clean-up
    rules = result.get("rules") if isinstance(result.get("rules"), dict) else {}
    actions = [e for e in rules.get("actions") or [] if isinstance(e, dict)]
    if actions:
        from .storage_view import RULE_TITLES, size_text
        lines += [tr("storage rule: {what}", what=tr(RULE_TITLES[e["rule"]], name=printable(str(e.get("name"))),
                                                         size=size_text(e.get("bytes"))))
                  for e in actions if e.get("rule") in RULE_TITLES]
    lines += [tr("left alone: {name}: {reason}", name=printable(str(e.get("name"))), reason=printable(str(e.get("reason"))))
              for e in listed("skipped") if isinstance(e, dict)]
    lines += [tr("left alone: {path} is damaged; remove it by hand", path=printable(str(path)))
              for path in listed("damaged")]
    return lines


def gc_pending_lines(result):
    """What `sandbox-ai gc --json` does not remove yet, one escaped line each: the kept starts the default expiry
    removes later (no keep_days there: unused for 90 days, 14 days after it was first said), and how to keep them."""
    result = result if isinstance(result, dict) else {}
    expiring = [e for e in result.get("expiring") or [] if isinstance(e, dict)]
    lines = [tr("not yet: the kept start {name} (last used {time}) goes from {date} on", name=printable(str(e.get("name"))),
                time=history_time(e.get("last_used")), date=history_time(e.get("removed_from"))) for e in expiring]
    expiry = result.get("expiry") if isinstance(result.get("expiry"), dict) else {}
    if expiring or any(isinstance(e, dict) and e.get("expiry") == "default" for e in result.get("removed") or []):
        lines.append(tr("Kept starts unused for more than {days} days are removed with their HOME by Clean up… and the "
                        "daily pass (sandbox-ai's default there: no keep_days); to keep them, resume them, or switch it "
                        "off: keep_days 0 in the host's defaults", days=printable(str(expiry.get("days") or 90))))
    return lines


def bytes_text(size):
    """A size in bytes as 1.5 GiB (files.size_text: the language's decimal mark)."""
    from .files import size_text as text
    return text(size if isinstance(size, (int, float)) and not isinstance(size, bool) else 0)


USAGE_PARTS = {"starts": N_("Starts (kept HOMEs)"), "logs": N_("Logs"), "mcp": N_("MCP servers"),
               "history": N_("Histories"), "checkpoints": N_("Workspace checkpoints"),
               "launcher_log": "launcher.log", "state_other": N_("Other state"),
               "data": N_("Data"), "cache": N_("Cache"), "config": N_("Configuration"),
               "runtime": N_("Run directories"), "podman_images": N_("Podman images"),
               "podman_volumes": N_("Podman volumes"), "podman_containers": N_("Podman containers")}
# What is in a kept HOME, by kind (sandbox-ai gc --usage --detail; statedirs.HOME_KINDS)
HOME_KINDS = {"caches": N_("download caches"), "toolchains": N_("toolchains"), "agent": N_("agent state"),
              "build": N_("build output and temp"), "other": N_("other (the agent's work)")}


# Podman's images by what they are to sandbox-ai (statedirs.IMAGE_GROUPS)
IMAGE_GROUPS = {"in_use": N_("a container uses them"), "sandbox_used": N_("sandbox-ai's, kept starts use them"),
                "sandbox_roles": N_("sandbox-ai's browser, VPN, Cairn, team; your services"),
                "sandbox_unused": N_("sandbox-ai's, no kept start uses them (sandbox-ai gc --images=old)"),
                "sandbox_unnamed": N_("sandbox-ai's without a name (sandbox-ai gc --images)"),
                "other_unnamed": N_("other software's without a name (sandbox-ai gc --images=prune)"),
                "intermediate": N_("layers images are built on"),
                "other": N_("other software's (base images, yours)")}


UPDATE_EVERY_SECONDS = 3600  # while the app runs: whether the update mode's daily check is due, asked this often
UPDATE_ANNOUNCED = "update-announced"  # state.json: the newer release the app's notification has told already
STORAGE_FIRST_SECONDS = 120  # the storage check at the app's start: after this long (the window comes first)
STORAGE_EVERY_SECONDS = 3600  # then whether it is due is asked this often


def storage_notice(result, host=LOCAL):
    """The storage check's notice (None: nothing to say): what could be freed above the threshold, the starts that
    grew more than the growth limit, what the storage rules did - numbers and names only, each escaped; HOST's
    (another host's check, storage_hosts.py) named."""
    result = result if isinstance(result, dict) else {}
    parts = []
    total = result.get("total")
    if result.get("above") is True and isinstance(total, int):
        parts.append(tr("{size} could be freed on this machine: Optimize… shows how.", size=bytes_text(total))
                     if host == LOCAL else
                     tr("{size} could be freed on {host}: Optimize… shows how.", size=bytes_text(total),
                        host=printable(host)))
    grew = [entry for entry in result.get("grew") or [] if isinstance(entry, dict)]
    if grew:
        parts.append(tr("Grew since the last check: {starts}", starts=", ".join(
            f"{printable(str(entry.get('name')))} +{bytes_text(entry.get('bytes') or 0)}" for entry in grew[:5])))
    said = [entry for entry in result.get("said") or [] if isinstance(entry, dict)]
    if said:
        from .storage_view import RULE_TITLES, size_text
        parts.append(tr("The storage rules acted {count} time(s): {what}", count=len(said), what="; ".join(
            tr(RULE_TITLES[entry["rule"]], name=printable(str(entry.get("name"))), size=size_text(entry.get("bytes")))
            if entry.get("rule") in RULE_TITLES else printable(str(entry.get("name"))) for entry in said[:6])))
    return "\n".join(parts) or None


def storage_of(instance):
    """{"bytes", "growth", "measured", "since"} of INSTANCE's size at the last storage check (sandbox-ai gc --check;
    list --json's "storage", this machine's from its history), or None - numbers and times only."""
    found = (getattr(instance, "record", None) or {}).get("storage")
    if not isinstance(found, dict) or not isinstance(found.get("bytes"), int) or isinstance(found["bytes"], bool):
        return None
    growth = found.get("growth")
    return {"bytes": found["bytes"],
            "growth": growth if isinstance(growth, int) and not isinstance(growth, bool) else None,
            "measured": found.get("measured") if isinstance(found.get("measured"), str) else None,
            "since": found.get("since") if isinstance(found.get("since"), str) else None}


def storage_cell(instance):
    """The Size column: the start's size at the last storage check ("" before one)."""
    found = storage_of(instance)
    return bytes_text(found["bytes"]) if found else ""


def storage_tip(found):
    """The Size column's words in the row's tooltip: when it was measured, and the growth since the check before."""
    when = local_time(found["measured"]) if found.get("measured") else "?"
    text = tr("Size at the storage check of {time}: {size}", time=when, size=bytes_text(found["bytes"]))
    if found.get("growth") is not None:
        growth = found["growth"]
        text += "; " + tr("{growth} since the check of {time}", time=local_time(found["since"]) if found.get("since")
                          else "?", growth=("+" if growth >= 0 else "-") + bytes_text(abs(growth)))
    return text


def kinds_text(kinds):
    """A HOME's size by kind ({kind: bytes}) as "download caches 1 GiB, agent state 30 MiB", the biggest first."""
    kinds = kinds if isinstance(kinds, dict) else {}
    found = sorted(((kind, value) for kind, value in kinds.items() if kind in HOME_KINDS
                    and isinstance(value, int) and not isinstance(value, bool) and value > 0), key=lambda kv: -kv[1])
    return ", ".join(f"{tr(HOME_KINDS[kind])} {bytes_text(value)}" for kind, value in found)


def detail_lines(result):
    """What `sandbox-ai gc --usage --detail --json` says, one escaped line each: every kept HOME by kind, the biggest
    first, with its biggest items; the total by kind."""
    result = result if isinstance(result, dict) else {}
    homes = [item for item in result.get("starts") or [] if isinstance(item, dict)]
    total = sum(item["bytes"] for item in homes if isinstance(item.get("bytes"), int))
    lines = [tr("The HOMEs by kind: {size} in {count} start(s)", size=bytes_text(total), count=len(homes))]
    scan = result.get("scan") if isinstance(result.get("scan"), dict) else {}
    if scan.get("partial"):
        lines.append(tr("(measured only in part: it took too long)"))
    if kinds_text(result.get("kinds")):
        lines.append(tr("All together: {kinds}", kinds=kinds_text(result.get("kinds"))))
    for item in homes[:20]:
        lines.append("")
        lines.append(f"{printable(str(item.get('name')))} ({printable(str(item.get('state') or '?'))}): "
                     f"{bytes_text(item.get('bytes'))}")
        if item.get("busy"):
            lines.append("  " + tr("not read: another command is starting, stopping or changing it"))
            continue
        if kinds_text(item.get("kinds")):
            lines.append("  " + kinds_text(item.get("kinds")))
        entries = [entry for key in ("items", "other") if isinstance(item.get(key), list) for entry in item[key]
                   if isinstance(entry, dict) and isinstance(entry.get("bytes"), int)]
        entries.sort(key=lambda entry: -entry["bytes"])
        if entries:
            lines.append("  " + tr("biggest: {items}", items=", ".join(
                f"{printable(str(entry.get('path')))} {bytes_text(entry['bytes'])}" for entry in entries[:4])))
    if len(homes) > 20:
        lines.append(tr("… and {count} more", count=len(homes) - 20))
    return lines


def usage_lines(result):
    """What `sandbox-ai gc --usage --json` says, one escaped line each: the total, the starts with their HOME's biggest
    folders, the log folders, the other parts, Podman, and what Clean up… would free."""
    result = result if isinstance(result, dict) else {}
    parts = result.get("parts") if isinstance(result.get("parts"), dict) else {}

    def part(key):
        value = parts.get(key)
        return value if isinstance(value, dict) else {}

    def items(value, limit=8):
        found = value.get("items")
        return [item for item in found if isinstance(item, dict)][:limit] if isinstance(found, list) else []
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    lines = [tr("What sandbox-ai keeps there: {size}", size=bytes_text(summary.get("total") or result.get("total")))]
    scan = result.get("scan") if isinstance(result.get("scan"), dict) else {}
    if scan.get("partial"):
        lines.append(tr("(measured only in part: it took too long)"))
    rows = [row for row in summary.get("rows") or [] if isinstance(row, dict)]
    if rows:  # (sandbox-ai 0.1.98 on: the overview, biggest first)
        lines.append(tr("Overview, biggest first (reclaimable: what Clean up with the caches… frees):"))
        for row in rows[:8]:
            share = row.get("share") if isinstance(row.get("share"), (int, float)) else 0
            lines.append("  " + tr("{part} {size} ({share} %)", part=tr(USAGE_PARTS.get(row.get("key"), ""))
                                   or printable(str(row.get("title"))), size=bytes_text(row.get("bytes")),
                                   share=f"{share:.0f}")
                         + (", " + tr("{size} reclaimable", size=bytes_text(row["reclaimable"]))
                            if row.get("reclaimable") else ""))
    starts = part("starts")
    lines += ["", tr("Starts: {size} in {count} start(s)", size=bytes_text(starts.get("bytes")),
                     count=starts.get("count") or 0)]
    for item in items(starts):
        home = item.get("home") if isinstance(item.get("home"), dict) else {}
        state = printable(str(item.get("state") or "?")) + (", " + tr("kept") if item.get("keep") else "") + \
            (", " + tr("packed (gc --pack; unpacked when resumed)") if item.get("packed") else "")
        lines.append(f"  {bytes_text(item.get('bytes'))}  {printable(str(item.get('name')))} ({state}"
                     + (", " + tr("last used {time}", time=history_time(item.get("last_used")))
                        if item.get("last_used") else "") + ")")
        biggest = [f"{printable(str(e.get('path')))} {bytes_text(e.get('bytes'))}"
                   for e in (home.get("biggest") or [])[:4] if isinstance(e, dict)]
        if biggest:
            lines.append("      " + tr("HOME: {folders}", folders="; ".join(biggest)))
        kinds = kinds_text(home.get("kinds"))
        if kinds:
            lines.append("      " + tr("by kind: {kinds}", kinds=kinds))
    logs = part("logs")
    lines += ["", tr("Logs: {size} in {count} log folder(s)", size=bytes_text(logs.get("bytes")),
                     count=logs.get("count") or 0)]
    for item in items(logs):
        kinds = item.get("kinds") if isinstance(item.get("kinds"), dict) else {}
        shown = ", ".join(f"{printable(str(kind))} {bytes_text(value.get('bytes'))}"
                          for kind, value in list(kinds.items())[:4] if isinstance(value, dict))
        lines.append(f"  {bytes_text(item.get('bytes'))}  {printable(str(item.get('name')))}: {shown}")
    others = [(key, title) for key, title in (("mcp", tr("MCP servers")), ("history", tr("Histories")),
                                              ("checkpoints", tr("Workspace checkpoints")),
                                              ("launcher_log", "launcher.log"), ("state_other", tr("Other state")),
                                              ("data", tr("Data")), ("cache", tr("Cache")),
                                              ("config", tr("Configuration"))) if part(key)]
    lines += ["", "; ".join(f"{title} {bytes_text(part(key).get('bytes'))}" for key, title in others)]
    podman = result.get("podman") if isinstance(result.get("podman"), dict) else None
    if podman:
        lines.append(tr("Podman: {parts}", parts="; ".join(
            tr("{kind} {size} ({reclaimable} reclaimable)", kind=printable(str(kind)), size=bytes_text(value.get("bytes")),
               reclaimable=bytes_text(value.get("reclaimable")))
            for kind, value in podman.items() if kind in ("images", "containers", "volumes")
            and isinstance(value, dict))))
        groups = podman.get("groups") if isinstance(podman.get("groups"), dict) else {}
        found = groups.get("groups") if isinstance(groups.get("groups"), dict) else {}
        shown = sorted(((key, value) for key, value in found.items() if key in IMAGE_GROUPS
                        and isinstance(value, dict) and value.get("count")),
                       key=lambda kv: -(kv[1].get("bytes") if isinstance(kv[1].get("bytes"), int) else 0))
        if shown:
            lines.append(tr("Podman's images by what they are to sandbox-ai:"))
            lines += ["  " + tr("{what}: {count}, {size}", what=tr(IMAGE_GROUPS[key]),
                                count=value.get("count"), size=bytes_text(value.get("bytes"))) for key, value in shown]
    would = result.get("would_free") if isinstance(result.get("would_free"), dict) else {}

    def entry(key):
        value = would.get(key)
        return value if isinstance(value, dict) else {}
    lines += ["", tr("What a clean-up would free:"),
              "  " + tr("Clean up…: the leftovers of {count} ended start(s), {size}",
                        count=entry("leftovers").get("count") or 0, size=bytes_text(entry("leftovers").get("bytes"))),
              "  " + tr("Clean up…: {size} of logs compressed, about {saves} freed (measured on a sample of each "
                        "kind; nothing removed)", size=bytes_text(entry("compress").get("bytes")),
                        saves=bytes_text(entry("compress").get("saves"))),
              "  " + tr("Clean up with the caches…: the download caches in {count} kept HOME(s), {size}",
                        count=entry("caches").get("count") or 0, size=bytes_text(entry("caches").get("bytes")))]
    expiry = entry("expiry")
    if expiry.get("origin") == "default":
        names = ", ".join(printable(str(name)) for name in (expiry.get("names") or [])[:5])
        lines.append("  " + tr("Clean up… and the daily pass: {count} kept start(s) unused for more than {days} days, "
                               "with their HOME, {size}", count=expiry.get("count") or 0,
                               days=printable(str(expiry.get("days"))), size=bytes_text(expiry.get("bytes")))
                     + (f" ({names})" if names else ""))
        lines.append("      " + (tr("sandbox-ai's default (no keep_days there); from {time} on; switch it off: "
                                    "keep_days 0 in the host's defaults", time=history_time(expiry["removed_from"]))
                                 if expiry.get("removed_from") else
                                 tr("sandbox-ai's default (no keep_days there), 14 days after it is first said; switch "
                                    "it off: keep_days 0 in the host's defaults")))
    elif expiry.get("origin") == "off":
        lines.append("  " + tr("Kept starts never expire there (keep_days 0)"))
    for older in would.get("logs_older_than") or []:
        if isinstance(older, dict) and older.get("count"):
            lines.append("  " + tr("sandbox-ai gc --logs-older-than {days}: {count} log folder(s) not written for that "
                                   "long, {size}", days=older.get("days"), count=older.get("count"),
                                   size=bytes_text(older.get("bytes"))))
    for older in would.get("older_than") or []:
        if isinstance(older, dict) and older.get("count"):
            lines.append("  " + tr("keep_days {days}: {count} kept start(s) unused for longer, "
                                   "with their HOME, {size}", days=older.get("days"), count=older.get("count"),
                                   size=bytes_text(older.get("bytes"))))
    if entry("images").get("count"):
        lines.append("  " + tr("sandbox-ai gc --images: {count} image(s) a rebuild left without a name, {size}",
                               count=entry("images")["count"], size=bytes_text(entry("images").get("bytes"))))
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

    def __init__(self, parent, name, host=LOCAL, running=False, servers=(), draft=None, start=None):
        super().__init__(parent)
        # START: (agent, launcher options, agent arguments) the session was created with, when they are known - "On the
        # Start page…" shows them there with what is added here (to_start: chosen instead of OK).
        self.start, self.to_start = start, False
        self.setWindowTitle(tr("Resume {name} with…", name=printable(name)) if host == LOCAL else
                            tr("Resume {name} on {host} with…", name=printable(name), host=printable(host)))
        form = QFormLayout(self)
        if host != LOCAL:
            form.addRow(plain_label(tr("Paths on {host}: chosen from {host}'s folders (over SSH).",
                                       host=printable(host)), role="secondary"))
        self.workspaces = ListField(path=True)
        self.workspaces.set_host(host)
        self.workspaces.setToolTip(tr("Folders added at /workspaces/<name>, one per line (--workspace)"))
        form.addRow(tr("Workspaces"), self.workspaces)
        # Each folder a row (the folder of the start's host, where the sandbox sees it, writable or not): never a
        # line of SOURCE:DESTINATION:MODE to type, and a folder of another machine (sshfs://...) as well.
        self.mounts = MountRows(host)
        self.mounts.setToolTip(tr("Folders the sandbox gets besides its workspaces (--mount), read-only unless the "
                                  "agent may change them; a folder of another machine is typed as sshfs://..."))
        form.addRow(tr("Mounts"), self.mounts)
        self.servers = QLineEdit()
        self.servers.setPlaceholderText(tr("names, separated by spaces (sandbox-ai mcp list)"))
        form.addRow(tr("MCP servers"), self.servers)
        if servers:
            form.addRow("", plain_label(tr("Defined: {list}", list=printable(", ".join(servers))), role="secondary"))
        self.options = QLineEdit()
        self.options.setPlaceholderText(tr("launcher options, e.g. --gui --cpus 4 --allow-host docs.example.org"))
        self.options.setToolTip(tr("An option with one value (--cpus, --memory, --gui, --mode …) replaces the saved "
                                   "one, a list option (--allow-host, --env, --rules …) adds its value; checked as a "
                                   "new start (sandbox-ai resume NAME OPTIONS)"))
        form.addRow(tr("Other options"), self.options)
        self.dropped = QLineEdit()
        self.dropped.setPlaceholderText(tr("options to take out, e.g. gui allow-host=example.org"))
        self.dropped.setToolTip(tr("Every value of an option, or one with =VALUE, taken out of its saved options "
                                   "(--drop-option)"))
        form.addRow(tr("Take out"), self.dropped)
        self.restart = QCheckBox(tr("Restart it now if it runs (the agent continues its conversation)"))
        self.restart.setChecked(running)
        form.addRow(self.restart)
        form.addRow(explanation(tr("What is added or changed here is kept for its later starts.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        # The whole start on the Start page - as a board, or as fields and its command line: every option of the
        # session with what is added here, to look at, change and start as a new start (the session itself is not
        # changed: that is this dialog's OK).
        self.start_button = buttons.addButton(tr("On the Board page…"), QDialogButtonBox.ButtonRole.ActionRole)
        self.start_button.setAutoDefault(False)
        set_enabled(self.start_button, start is not None, tr(
            "The options this session was created with are not known here (a session of another host, or one made "
            "by an older sandbox-ai)"), tr(
            "Show this session's whole start on the Board page, with what is added here: as a board or as fields "
            "and its command line, to look at, change and start as a new start. The session itself is not changed "
            "(that is OK); what Take out names is this dialog's own."))
        self.start_button.clicked.connect(lambda _=False: self.show_on_start())
        form.addRow(buttons)
        # What was typed and not sent (Cancel, a "no", a refusal): filled in again for this start (drafts.py); the
        # restart is chosen anew each time (it follows whether the start runs).
        self.draft = None if draft is None else drafts.Draft(self, draft, {
            "workspaces": self.workspaces.edit, "mounts": self.mounts, "servers": self.servers,
            "options": self.options, "dropped": self.dropped})

    def show_on_start(self):
        """On the Board page…: the dialog closes without resuming; its caller shows start_line() on the Board page."""
        self.to_start = True
        self.reject()

    def start_line(self):
        """(agent, launcher options, agent arguments) of the session's start with what is added here: its options,
        then the workspaces, mounts, MCP servers and other options of the dialog. ValueError: as values() and
        changed_options()."""
        agent, options, arguments = self.start
        workspaces, mounts, servers, _ = self.values()
        added, _ = self.changed_options()
        argv = [str(option) for option in options]
        argv += [token for folder in workspaces for token in ("--workspace", folder)]
        argv += [token for source, destination, mode in mounts for token in ("--mount", f"{source}:{destination}:{mode}")]
        argv += [token for server in servers for token in ("--mcp", server)]
        return agent, argv + added, [str(argument) for argument in arguments]

    def changed_options(self):
        """(options, options taken out) as typed (words as a shell splits them). ValueError: quotes not closed."""
        try:
            return shlex.split(self.options.text()), shlex.split(self.dropped.text())
        except ValueError as exc:
            raise ValueError(tr("Other options: {error}", error=printable_text(exc))) from None

    def values(self):
        """(workspaces, [(source, destination, mode)], servers, restart). ValueError: a folder row half filled
        (MountRows). The other options: changed_options()."""
        return self.workspaces.value(), self.mounts.value(), self.servers.text().split(), self.restart.isChecked()


def log_instance_changes(page, host, instances):
    """What changed on HOST since its last good read, into the log once per change (INFO): a sandbox that appeared,
    whose state changed (running, paused, stopped, failed...), or that is no longer listed. A read that failed changes
    nothing here (said by host_read and apply)."""
    seen = page.__dict__.setdefault("logged_states", {})
    now = {i.key: (i.kind, i.name, i.state) for i in instances if i.host == host}
    for key, (kind, name, state) in now.items():
        before = seen.get(key)
        if before is None or before[2] != state:
            applog.log("INFO", "sandboxes", f"{kind} {printable(name)} on {printable(host)}: "
                       + (f"{before[2] or '?'} -> {state or '?'}" if before is not None
                          else f"{state or '?'} (listed)"))
    for key in [k for k, v in seen.items() if v[3] == host and k not in now]:
        kind, name, state, _ = seen.pop(key)
        applog.log("INFO", "sandboxes", f"{kind} {printable(name)} on {printable(host)}: no longer listed "
                                        f"(was {state or '?'})")
    seen.update({key: (*value, host) for key, value in now.items()})


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
    notice = Signal(str, str, str)  # level, title, text: a notification (what the kill switch cut)
    open_credentials = Signal(str)  # the secrets and logins of that host (the Credentials page with it chosen)
    open_given = Signal(str, str)  # (host, run): what that running start was given during its run, each with Revoke…
    # (host, run): everything it may reach and why - Approvals › Inspection › Allowed now for it (Revoke…, Untaint…,
    # Keep for later starts…, Copy as text): one view of it in the app (2026-11-24, proposal E)
    show_reach = Signal(str, str)
    # Resume with…'s "On the Start page…": (agent, launcher options, agent arguments, host) of a session's start to
    # show on the Start page (the window: show_start_line)
    start_wanted = Signal(str, object, object, str)

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        # Where a start's browser opens (the window sets it: a tab of Terminals); None: a window of its own.
        self.browser_opener = None
        self.project, self.instances, self.problems, self.stoppable = None, {}, [], None
        self.cuttable = None  # this sandbox-ai has the kill switch (sandbox-ai net cut); None: not known yet
        # The cuts on their way ({(host, run): instance}) and the one chosen meanwhile for each (sent once the first
        # is done).
        self.cut_flights, self.cut_waiting = {}, {}
        self.reading, self.read_again, self.added, self.devices_token = None, False, [], 0
        self.fresh = False  # the next read asks Podman afresh (refresh_soon(again=True): after an action)
        self.loaded = False  # the first read has come back (until then the summary says it reads)
        self.added_workspaces = []
        # (host, run) of the starts with an open question (the Approvals page's poller): marked "asking".
        self.asking = set()
        # The starts that ended, when "Ended" is on: {host: [Instance]} from `sandbox-ai list --all --json`.
        self.ended, self.ended_token = {}, 0
        # Two tabs (user 2026-11-19): the starts with their actions, and the scheduled runs; the one shown is kept
        # (State.tab). A start shown from elsewhere (show_instance, Hosts › Show starts, a row selected) brings the
        # first to the front: show_sessions.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)  # (the tab's own margins: the page no wider than before its tabs)
        self.tabs = QTabWidget()
        widgets.arrow_tabs(self.tabs)  # (the app's tab row)
        self.sessions_tab, self.schedules_tab = QWidget(), QWidget()
        layout = QVBoxLayout(self.sessions_tab)
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
        self.show_ended.setToolTip(tr("Also the starts that ended: stopped sessions (to resume or delete them), stopped "
                                      "MCP servers and what else sandbox-ai list --all keeps, on this machine and every "
                                      "host. A session that stops while it is selected stays listed until another row "
                                      "is selected."))
        self.show_ended.toggled.connect(lambda _=False: self.ended_toggled())
        refresh = QPushButton(tr("Refresh"))
        refresh.setAccessibleName(tr("Refresh the sandboxes"))
        refresh.setToolTip(tr("Read again now (it is read every few seconds anyway)"))
        refresh.clicked.connect(lambda _=False: (self.refresh_soon(again=True), self.refresh_remote()))
        self.refresh_button = refresh  # (also on the list's right-click menu)
        top.addWidget(self.summary, 1)
        top.addWidget(self.host_filter)
        top.addWidget(self.only_project)
        top.addWidget(self.show_ended)
        top.addWidget(refresh)
        layout.addLayout(top)
        # Above the tree, the actions on the selected start in two rows of icon buttons (button_icons.py; their texts
        # in the tooltips, beside the icons with Settings › Show button texts): the session's own, and below them
        # what changes a start while it runs. Each row wraps when the page is narrow.
        self.session_row, self.run_row = QWidget(), QWidget()
        for row in (self.session_row, self.run_row):
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(row)
        self.session_label = plain_label(tr("Session"), word_wrap=False, role="section")
        self.session_label.setToolTip(tr("What a session can do: attach, pause, resume, delete, move, export"))
        self.run_label = plain_label(tr("While it runs"), word_wrap=False, role="section")
        self.run_label.setToolTip(tr("What a running start may reach and use, changed until it ends"))
        # A tree: each host, under it its starts, under each start what belongs to it (start_tree.py); the buttons
        # act on the start rows only. Which groups are open is kept per host and start (open_groups).
        self.table = StartTree([tr("Kind"), tr("Name"), tr("Project"), tr("Agent"), tr("Backend"), tr("Started"),
                                tr("State"), tr("Proxy"), tr("Host"), tr("Memory"), tr("Size")], stretch=2,
                               compact=(0, 3, 4, 5, 6, 7, 9, 10))
        self.table.setAccessibleName(tr("Sandboxes and sessions"))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemExpanded.connect(lambda item: self.group_toggled(item, True))
        self.table.itemCollapsed.connect(lambda item: self.group_toggled(item, False))
        self.table.itemDoubleClicked.connect(lambda item, _column: self.open_part(item))
        self.table.folded.connect(self.all_folded)
        self.table.row_chosen.connect(self.show_sessions)  # (a row selected from elsewhere: its tab in front)
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
        # Every start that runs, is paused or orphaned (sandbox-ai stop --all), here and on the host chosen above.
        self.stop_all_button = button_icons.IconButton("stop-all", tr("Stop all…"), tr("Stop all"), role="danger")
        self.stop_all_button.setToolTip(safe_tooltip(tr(
            "Stop every start that runs, is paused or orphaned - sessions, runs, tasks; not the MCP servers - on "
            "this machine, or on the host chosen above (sandbox-ai stop --all); the list is shown first")))
        self.stop_all_button.clicked.connect(lambda _=False: self.stop_all())
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
                ("report", tr("Report…"), tr("One report of the selected starts, running or stopped, with their MCP "
                                             "servers, helpers and team: statistics, every destination and why it "
                                             "was allowed or refused, and a security report of what was not allowed "
                                             "- as PDF, HTML or Markdown (sandbox-ai report)"), self.report),
                ("usage", tr("Disk usage…"), tr("How much space what sandbox-ai keeps takes - the starts with their "
                                                "HOMEs, the logs of each project, Podman's images - and what a "
                                                "clean-up would free, on this machine or the host chosen above "
                                                "(sandbox-ai gc --usage)"), self.disk_usage),
                ("clean", tr("Clean up…"), tr("Remove leftovers and the kept starts unused for longer than its "
                                              "keep_days, and compress the logs of ended runs, on this machine or "
                                              "the host chosen above (sandbox-ai gc; shown first)"), self.clean_up),
                ("optimize", tr("Optimize…"), tr("What could be freed on this machine or the host chosen above - each "
                                                 "with how much, its command and whether it is safe while starts run; "
                                                 "tick and apply, asked first (sandbox-ai gc --check)"),
                 self.optimize)):
            # (Optimize… as an icon above the list, beside Disk usage…: the row has no room for one more text in
            # German - test_layout; its text on hover, and with the setting button_texts)
            button = button_icons.IconButton(key, text) if key == "optimize" else QPushButton(text)
            button.setToolTip(safe_tooltip(tip))
            button.setAccessibleName(text.rstrip("…"))
            button.clicked.connect(lambda _=False, s=slot: s())
            self.manage_buttons[key] = button
        # By name too: the (?) and the area tour point at them.
        self.rename_button, self.history_button, self.clean_button, self.optimize_button = (
            self.manage_buttons[key] for key in ("rename", "history", "clean", "optimize"))
        # The kill switch (sandbox-ai net cut): the selected starts' network cut down at once (also several), with
        # their MCP servers, helpers and team; Lift the cut gives back what they may reach.
        self.cut_button = QPushButton(tr("Kill switch"))
        self.cut_button.setAccessibleName(tr("Kill switch"))
        self.cut_button.setProperty("role", "danger")
        self.cut_menu = QMenu(self.cut_button)
        self.cut_menu.setToolTipsVisible(True)
        self.cut_actions = {}
        for mode, text, tip in CUT_CHOICES:
            if mode == "off":
                self.cut_menu.addSeparator()
            item = self.cut_menu.addAction(tr(text))
            item.setToolTip(tr(tip))
            item.setCheckable(mode != "off")  # the cut that holds is ticked (show_cut_now)
            item.triggered.connect(lambda _=False, m=mode: self.cut(m))
            self.cut_actions[mode] = item
        self.cut_menu.aboutToShow.connect(self.show_cut_now)
        self.cut_button.setMenu(self.cut_menu)
        below.addWidget(self.detail, 1)
        for key, button in self.manage_buttons.items():
            if key not in ("report", "usage", "optimize"):
                below.addWidget(button)
        # Report… above the list, beside Refresh (the row below is full): for the rows selected, running or ended;
        # Disk usage… there too - it is about the whole machine, not the row selected (the row below would need 979 px
        # in German, wider than a small laptop's page).
        top.insertWidget(top.indexOf(refresh), self.manage_buttons["report"])
        top.insertWidget(top.indexOf(refresh), self.manage_buttons["usage"])
        top.insertWidget(top.indexOf(refresh), self.manage_buttons["optimize"])
        below.addWidget(self.cut_button)
        below.addWidget(self.stop_wait)
        below.addWidget(self.stop_button)
        layout.addLayout(below)
        # The LLM server the selected start works with (--llm NAME): what it has loaded and uses now (llm status on
        # the start's host), read in the background while it is shown.
        from .llm_page import ServerStatusLine
        self.llm_status = ServerStatusLine(sai)
        layout.addWidget(self.llm_status)
        # The sessions' own actions (sandbox-ai session ...).
        self.session_buttons = {}
        for action, text in SESSION_ACTIONS:
            button = button_icons.IconButton(action, tr(text))
            button.clicked.connect(lambda _=False, a=action: self.session(a))
            self.session_buttons[action] = button
        self.session_buttons["shell"].setToolTip(tr(
            "A shell of yours in the selected running start, in a terminal tab: under exactly the agent's rules "
            "(its network, proxy, mounts and limits), noted in its activity ({command})",
            command="sandbox-ai shell NAME"))
        self.resume_with_button = button_icons.IconButton("resume-with", tr("Resume with…"), tr("Resume with"))
        self.resume_with_button.setToolTip(tr("Start the selected kept start again with workspaces, mounts or MCP "
                                              "servers added (kept for its later starts), or restart it now "
                                              "(sandbox-ai resume)"))
        self.resume_with_button.clicked.connect(lambda _=False: self.resume_with())
        # A session of this machine to a host of sandbox-ai hosts: its configuration started again there (session
        # copy), or then stopped here too (session move).
        self.transfer_buttons = {}
        for move, text, name, tip in (
                (False, tr("Copy to host…"), tr("Copy to host"),
                 tr("Start the selected session's configuration again on a host as a session of its own, checked "
                    "there first; logins and HOME are not copied ({command})", command="sandbox-ai session copy")),
                (True, tr("Move to host…"), tr("Move to host"),
                 tr("As Copy to host…, then stop the session here; its HOME stays until it is deleted ({command})",
                    command="sandbox-ai session move"))):
            button = button_icons.IconButton("move" if move else "copy", text, name)
            button.setToolTip(safe_tooltip(tip))
            button.clicked.connect(lambda _=False, m=move: self.transfer(m))
            self.transfer_buttons["move" if move else "copy"] = button
        # A session's state - its HOME with the agent's conversation, memory, checkpoints, approvals: as a second
        # session here (session duplicate), as one archive (session export), and an archive taken as a session here
        # (session import: checked first) - session_state_dialogs.
        if self.sai_state_supported():
            for key, text, name, tip in (
                    ("duplicate", tr("Duplicate…"), tr("Duplicate the session"),
                     tr("A second session here from the selected session's state: its own HOME (the agent's "
                        "conversation continued on its own), memory and checkpoints; the workspace shared or copied "
                        "({command})", command="sandbox-ai session duplicate")),
                    ("export-state", tr("Export with state…"), tr("Export the session with its state"),
                     tr("The selected session with its state as one archive another machine imports: configuration, "
                        "HOME with the agent's conversation, memory, checkpoints, approvals ({command})",
                        command="sandbox-ai session export")),
                    ("import", tr("Import…"), tr("Import a session"),
                     tr("A session's archive (Export with state…) as a session of this machine that Resume "
                        "continues - checked as untrusted input first ({command})",
                        command="sandbox-ai session import"))):
                button = button_icons.IconButton(key, text, name)
                button.setToolTip(safe_tooltip(tip))
                button.clicked.connect(lambda _=False, k=key: self.session_state(k))
                self.transfer_buttons[key] = button
        # The session as one self-sufficient file (sandbox-ai board export --session NAME): of this machine or a host,
        # running or not; the file is written here.
        export = button_icons.IconButton("export", tr("Export…"), tr("Export the session"))
        export.setToolTip(safe_tooltip(tr(
            "Write the selected session as one file another machine imports: its options, MCP and LLM servers, "
            "rulesets, kits, presets and image recipes - folders as placeholders, secrets by name only ({command})",
            command="sandbox-ai board export")))
        export.clicked.connect(lambda _=False: self.export_session())
        self.transfer_buttons["export"] = export
        # Its workspaces as they were (sandbox-ai checkpoint): one taken when it began, others every few minutes or
        # by hand; what changed since, put back after a preview (CheckpointsDialog). Of this machine or a host, any
        # start of an agent - also one that ended without keeping its HOME (its checkpoints stay).
        checkpoints = button_icons.IconButton("checkpoints", tr("Checkpoints…"), tr("Checkpoints"))
        checkpoints.setToolTip(safe_tooltip(tr(
            "The selected start's workspaces as they were when it began (and later): what changed since, put back "
            "after a preview - undo what its agent did ({command})", command="sandbox-ai checkpoint")))
        checkpoints.clicked.connect(lambda _=False: self.open_checkpoints())
        self.transfer_buttons["checkpoints"] = checkpoints
        # Its agent's memory and the agents' own session files, read only (sandbox-ai session memory|files): a list
        # and the chosen file's text, its folder exported as one archive (SessionInspectDialog).
        for key, text, name, tip in (
                ("memory", tr("Memory…"), tr("Agent memory"),
                 tr("The selected start's agent memory: its memory file and auto memory - the live copy while it runs, "
                    "else what went back to the project's memory store ({command})", command="sandbox-ai session memory")),
                ("files", tr("Session files…"), tr("Session files"),
                 tr("The agents' own session files in the selected start's HOME, one shown as a conversation; its "
                    "folder exported as one archive ({command})", command="sandbox-ai session files"))):
            button = button_icons.IconButton(key, text, name)
            button.setToolTip(safe_tooltip(tip))
            button.clicked.connect(lambda _=False, k=key: self.open_inspect(k))
            self.transfer_buttons[key] = button
        # Its storage (sandbox-ai gc --check --home NAME on its host): its HOME by kind, its project's logs and
        # checkpoints, its growth, and what can be done with it, each with its saving and command (storage_view).
        storage = button_icons.IconButton("storage", tr("Storage…"), tr("Storage"))
        storage.setToolTip(safe_tooltip(tr(
            "The selected start's storage: its HOME by kind, the logs and checkpoints of its project, its growth since "
            "the last check, and what can be freed - empty caches, compact or remove transcripts, pack - each with "
            "its saving and command ({command})", command="sandbox-ai gc --check --home NAME")))
        storage.clicked.connect(lambda _=False: self.open_storage())
        self.transfer_buttons["storage"] = storage
        self.copy_to_host_button, self.move_to_host_button = self.transfer_buttons["copy"], self.transfer_buttons["move"]
        # Show on the Board (user 2026-11-24): the Board with the selected session's project - its tab, its board, its
        # Agent part selected (the window: board_request; board_follow.show). A button here and the first entry of
        # the right-click menu.
        self.board_request = None
        self.board_button = button_icons.IconButton("board", tr("Show on the Board"))
        self.board_button.setToolTip(tr("Show the Board with this session's project: its tab, its board, its Agent "
                                        "part selected"))
        self.board_button.clicked.connect(lambda _=False: self.show_on_board())
        self.session_row.layout().addWidget(flow(self.session_label, *self.session_buttons.values(),
                                                 self.resume_with_button, *self.transfer_buttons.values(),
                                                 self.board_button))
        # What a running sandbox may reach (sandbox-ai net), its devices and limits (until it ends).
        self.run_buttons, self.run_named = [], {}
        for key, text, tip, slot in (
                ("allow", tr("Allow…"), tr("Allow more destinations until the sandbox ends (sandbox-ai net allow)"),
                 lambda: self.network("allow")),
                ("revoke", tr("Revoke…"), tr("Take back what was allowed during the run (sandbox-ai net revoke)"),
                 lambda: self.network("revoke")),
                ("allowed", tr("What it may reach…"), tr("What the selected sandbox may reach and why: what its start "
                                                          "opened, each with its source, and what was allowed during "
                                                          "the run (sandbox-ai net show)"),
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
                ("add-toolchain", tr("Add toolchain…"), tr("Mount a build toolchain of its host's store (Go, Rust, "
                                                           "a JDK, Node, Python ...) read-only into the selected "
                                                           "sandbox, its commands on the agent's PATH (sandbox-ai "
                                                           "toolchain attach)"), self.add_toolchain),
                ("remove-toolchain", tr("Remove toolchain…"), tr("Take a toolchain added during the run out again "
                                                                 "(sandbox-ai toolchain detach)"),
                 self.remove_toolchain),
                ("add-mcp", tr("Add MCP server…"), tr("Give the selected kept start an MCP server or a helper agent "
                                                      "now, without a restart, and at its later starts (sandbox-ai "
                                                      "mcp attach --live)"), self.add_mcp),
                ("remove-mcp", tr("Remove MCP server…"), tr("Take an MCP server from the selected start now and from "
                                                            "its later starts (sandbox-ai mcp detach --live)"),
                 self.remove_mcp),
                ("add-kits", tr("Add skills/plugins…"), tr("Give the selected running start a kit, a skill, a command, "
                                                           "a subagent or a plugin now, read-only (sandbox-ai kits "
                                                           "attach, plugins attach)"), self.add_kits),
                ("remove-kits", tr("Remove skills/plugins…"), tr("Take what Add skills/plugins… gave the selected start "
                                                                 "out again (sandbox-ai kits detach, plugins detach)"),
                 self.remove_kits),
                ("limits", tr("Limits…"), tr("Change memory, CPUs and processes of the selected sandbox"), self.limits),
                ("rules", tr("Reload rules"), tr("The selected sandbox reads its HTTP rules anew: your rules.json, its "
                                                 "ruleset files and its project's rules as they are now; the "
                                                 "administrator's and the security mode's stay (sandbox-ai rules "
                                                 "reload)"), self.reload_rules),
                ("browser", tr("Browser"), tr("Show the selected sandbox's own browser (--browser) as a tab in "
                                              "Terminals, beside its terminal: its display, your mouse and keyboard; "
                                              "it takes the sandbox's exit"),
                 self.open_browser)):
            button = button_icons.IconButton(key, text)
            button.setToolTip(safe_tooltip(tip))
            button.clicked.connect(lambda _=False, s=slot: s())
            self.run_buttons.append(button)
            self.run_named[key] = button
        self.toolchain_button = self.run_named["add-toolchain"]  # (the tours and the Help point at it: tours.py)
        # The secrets and Git logins of the selected start's host: the Credentials page with that host chosen (its
        # Give to a running sandbox… gives one to this start there).
        self.credentials_button = button_icons.IconButton("credentials", tr("Secrets and logins…"),
                                                          tr("Secrets and logins"))
        self.credentials_button.clicked.connect(lambda _=False: self.credentials_of())
        self.run_row.layout().addWidget(flow(self.run_label, *self.run_buttons, self.credentials_button,
                                             self.stop_all_button))  # (it wraps)
        # The two rows' labels as wide as each other: their icons start on one line.
        wide = max(self.session_label.sizeHint().width(), self.run_label.sizeHint().width())
        for label in (self.session_label, self.run_label):
            label.setMinimumWidth(wide)
        # What was given to the selected start during the run (below the tree, under its detail line).
        self.devices_label = plain_label(role="secondary")
        layout.addWidget(self.devices_label)
        self.workspaces_label = plain_label(role="secondary")
        layout.addWidget(self.workspaces_label)
        self.devices_label.hide()  # (only while something was added: show_added)
        self.workspaces_label.hide()
        # The session bridges between running sessions of this machine (sandbox-ai bridge): listed below, marked on
        # both sessions' rows (State), made, shown, revoked and resumed in the background.
        from .bridges_view import BridgesBox
        self.bridges_box = BridgesBox(sai, self.bridge_sessions, self.bridge_selected,
                                      remote_of=self.bridge_remote_sessions,
                                      hosts_of=lambda: list(getattr(self, "remote_names", None) or []),
                                      loaded_of=lambda: bool(getattr(self, "loaded", True)))
        self.bridges_box.changed.connect(lambda: self.show_instances())
        supported = getattr(sai, "bridge_supported", None)
        self.bridges_box.setVisible(bool(callable(supported) and supported()))
        layout.addWidget(self.bridges_box)
        # The scheduled runs of this machine or a host (sandbox-ai schedule): an agent started on a timetable or a
        # trigger, with a report of each run; listed, added, run, paused and removed in the background.
        from .schedules_view import SchedulesBox
        self.schedules_box = SchedulesBox(sai)
        self.schedules_box.setTitle("")  # (the tab says it; no frame in the tab's frame)
        self.schedules_box.setFlat(True)
        self.schedules_box.setStyleSheet("QGroupBox { border: none; margin-top: 0px; padding-top: 0px; }")
        self.schedules_box.layout().setContentsMargins(0, 0, 0, 0)
        QVBoxLayout(self.schedules_tab).addWidget(self.schedules_box)
        self.tabs.addTab(self.sessions_tab, tr("Sessions").replace("&", "&&"))  # (no mnemonic)
        self.tabs.addTab(self.schedules_tab, tr("Schedules"))
        outer.addWidget(self.tabs)
        # A right click on a start: every command of the buttons around the list, as they are enabled for the
        # selected start(s) - the same rules, a disabled one with its reason.
        widgets.list_menu(self.table, self.table_menu_entries)
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
            self.read_net_commands()
        return self.sai.instances()

    def read_net_commands(self):
        """Once: whether this sandbox-ai has `sandbox-ai net stop` and the kill switch (`net cut`)."""
        try:
            commands = self.sai.net_commands()
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("ui", "SandboxesPage.read_net_commands failed (went on without it)", _exc)
            commands = set()
        self.stoppable, self.cuttable = "stop" in commands, "cut" in commands

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
            self.hosts_problem = printable_text(error)[:200]
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
                log_instance_changes(self, name, self.remote[name])
                applog.changed(("host answers", name), "INFO", "hosts", f"{printable(name)} answers (its sandboxes "
                               "are listed)", quiet=None)
                self.remote_seen[name] = time.time()
                self.remote_problems.pop(name, None)
                self.remote_failures.pop(name, None)
                self.remote_next.pop(name, None)
            else:
                self.remote_problems[name] = printable_text(error)[:300]
                applog.changed(("host answers", name), "WARNING", "hosts", f"{printable(name)} does not answer "
                               f"(its sandboxes are not listed): {applog.error_text(error)}", quiet="DEBUG")
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

    def remote_running(self):
        """{host: [(name, {"agent", "workspace"})]} of the starts that run on the other hosts as last read (not of a
        host that did not answer): what the Approvals page's Blocked and Allowed now may show, read there."""
        found = {}
        for instance in self.instances.values():
            host = getattr(instance, "host", LOCAL)
            if host == LOCAL or instance.state != "running" or instance.kind not in ("run", "session") or \
                    host in self.remote_problems or not backend.valid_run(instance.name):
                continue
            found.setdefault(host, []).append((instance.name, {"agent": instance.agent, "workspace": instance.project}))
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

    def show_sessions(self):
        """The tab of the starts (the tree and its actions) in front."""
        self.tabs.setCurrentWidget(self.sessions_tab)

    def show_schedules(self):
        """The tab of the scheduled runs in front."""
        self.tabs.setCurrentWidget(self.schedules_tab)

    def show_host(self, host):
        """List only HOST's starts (Hosts › Show starts)."""
        self.show_sessions()
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
            self.read_net_commands()
        if error is not None:
            found, self.problems = [], [str(error)]
        else:
            found, self.problems = result
            log_instance_changes(self, LOCAL, found)
        applog.changed(("sandboxes", "problems"), "WARNING" if error is not None else "INFO", "sandboxes",
                       ("this machine's sandboxes could not be read: " if error is not None else
                        "reading this machine's sandboxes: ") + applog.redact("; ".join(map(str, self.problems)))[:600]
                       if self.problems else "this machine's sandboxes are read", quiet=None)
        local = {instance.key: instance for instance in found}
        self.instances = {**local, **{i.key: i for found_ in getattr(self, "remote", {}).values() for i in found_}}
        self.show_instances()
        if self.bridges_box.isVisibleTo(self):  # the bridges after each look (their marks follow when they change)
            self.bridges_box.refresh()
        self.bridges_box.update_buttons()  # (New bridge… follows the sessions that run)
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
        """{key: Instance} the table may show: what runs, is paused, starts or needs a look (orphaned, damaged), and
        with "Ended" also what ended: the kept sessions that are stopped and the starts that ended (those that a host
        lists already as kept are not listed twice)."""
        if not self.show_ended.isChecked():
            # A start that stops while it is selected stays listed (and selected) until another row is selected: it
            # does not vanish under the mouse, its Resume and Delete stay at hand.
            held = self.selected_keys()
            return {key: i for key, i in self.instances.items() if i.state not in ENDED_STATES or key in held}
        found = dict(self.instances)
        names = {(i.host, i.name) for i in self.instances.values()} | \
            {(i.host, getattr(i, "ident", i.name)) for i in self.instances.values()}
        for host, ended in self.ended.items():
            if host == LOCAL or host in self.remote_names:
                found.update({i.key: i for i in ended if (i.host, i.name) not in names
                              and (i.host, getattr(i, "ident", i.name)) not in names})
        return found

    def selected_keys(self):
        """The keys of the starts whose rows are selected now."""
        return {item.data(0, KEY_ROLE) for item in self.table.selectedItems() if item.data(0, KIND_ROLE) == "start"}

    def start_row(self, i, belongs=None):
        """{cells, color, tip, sort} of the row of the start I. BELONGS: (the start it is shown below, how:
        start_tree.owners), "orphan" (an MCP server that belongs to no start), or None (a start of its own)."""
        memory = backend.memory_share(i) if i.state in ("running", "paused") else None
        state = tr(STATE_TITLES.get(i.state, i.state)) if i.state else ""
        terminal = terminal_of(i)
        if terminal == "starting":  # running, its terminal not there yet: its start goes on (and where it is)
            step = str((i.record or {}).get("terminal_step") or "")
            state = tr("starting") + (f": {printable(step)[:60]}" if step else "")
        elif terminal == "missing":
            state = tr("no terminal (see log)")
        elif terminal == "ended":  # its tmux session is gone (the agent and its shell window), its sandbox not
            state = tr("no agent: its tmux session ended (see log)")
        stale, asking = self.stale(i), self.is_asking(i)
        if stale is not None:  # its host did not answer: what was seen then, not what is now
            state = tr("{state}, last seen {ago}", state=state or "?", ago=ago(stale)) \
                if i.host in self.remote_seen else tr("{state}, not seen", state=state or "?")
        elif asking:
            state = tr("{state}, asking", state=state or "?")
        allows_all = AC.allows_all(i.record) and i.state in ("running", "paused")  # --ask allow: nobody is asked
        if allows_all:
            state = f"{state}, {AC.WARN} {tr(AC.BADGE)}"
        # The kill switch holds (sandbox-ai net cut): what is cut, at a glance.
        cut = backend.cut_name((i.record or {}).get("cut")) if i.state in ("running", "paused") else None
        if cut:  # several at once (a shared server): each said
            state = tr("{state}, {cut}", state=state or "?",
                       cut=" + ".join(tr(CUT_STATES[part]) for part in cut.split("+")))
        # A session bridge joins it to another running session (sandbox-ai bridge): said on both sessions' rows.
        bridges = getattr(self, "bridges_box", None)
        bridged = BR.marker(bridges.rows, i.run) if bridges is not None and getattr(i, "host", LOCAL) == LOCAL and \
            i.state in ("running", "paused") else ""
        if bridged:
            state = tr("{state}, {bridged}", state=state or "?", bridged=bridged)
        cells = [tr(KIND_TITLES.get(i.kind, i.kind)), printable(i.name), printable(i.project), printable(i.agent),
                 printable(i.backend), local_time(i.started), state,
                 "" if i.proxy is None else tr("on") if i.proxy else tr("off"),
                 tr("this machine") if getattr(i, "host", "local") == "local" else printable(i.host),
                 "" if memory is None else f"{memory[0]:.0f} %", storage_cell(i)]
        color = {"running": "ok", "paused": "warn", "damaged": "bad", "starting": "warn"}.get(i.state)
        if terminal in ("starting", "missing", "ended"):
            color = "warn" if terminal == "starting" else "bad"
        if memory is not None and memory[0] >= 95:
            color = "bad"  # the watch is about to end a process in it
        elif memory is not None and memory[0] >= 85 and color != "bad":
            color = "warn"
        if (asking or allows_all) and color != "bad":
            color = "warn"
        if cut:
            color = "bad" if cut == "all" else "warn" if color != "bad" else color
        if stale is not None or i.state in ENDED_STATES and i.key.startswith("ended:"):
            color = "muted"
        tip = [tr("Current project")] if self.mine(i) else []
        if cut:
            tip.append(tr("Its network is cut by the kill switch ({mode}): {what}. Kill switch › Lift the cut gives it "
                          "back.", mode=cut, what="; ".join(tr(next(text for key, _, text in CUT_CHOICES if key == part))
                                                            for part in cut.split("+"))))
        if belongs == "orphan":
            tip.append(tr(ORPHANS_HELP))
        elif belongs is not None:
            tip.append(tr(BELONGS_HELP.get(belongs[1], BELONGS_HELP["uses"]), name=printable(belongs[0].name)))
        if allows_all:
            tip.append(tr(AC.ALLOW_HELP))
        if bridged:
            tip.append(BR.tooltip(bridges.rows, i.run))
        if terminal == "starting":
            tip.append(tr("It still starts: its terminal is not there yet (an image, MCP servers or a VPN coming "
                          "up). Attach waits for it."))
        elif terminal == "missing":
            tip.append(tr("It runs without its terminal: its start did not make one. The end of its supervisor.log:")
                       + "\n" + printable_text((i.record or {}).get("terminal_log") or tr("(empty)")))
        elif terminal == "ended":
            tip.append(tr(NO_AGENT) + "\n" + printable_text((i.record or {}).get("terminal_log") or tr("(empty)")))
        if asking:
            tip.append(tr("It waits for an answer on the Approvals page."))
        if stale is not None:
            tip.append(tr("{host} does not answer: {problem}", host=printable(i.host),
                          problem=printable(self.remote_problems.get(i.host, ""))[:200]))
        size = storage_of(i)
        if size is not None:
            tip.append(storage_tip(size))
        # By the share, not as text ("9 %" > "10 %"); the size by its bytes.
        return {"cells": cells, "color": color, "tip": "\n".join(tip) or None,
                "sort": [None] * 9 + [memory[0] if memory else -1.0, size["bytes"] if size else -1]}

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
            state = tr("no answer, last seen {ago}", ago=ago(time.time() - seen)) if seen else \
                tr("no answer, not seen yet")
            color, tip = "muted", tr("{host} does not answer: {problem}", host=where, problem=printable_text(problem)[:200])
        elif seen:
            state = tr("answers, seen {ago}", ago=ago(time.time() - seen))
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

    def host_plan(self, host, starts):
        """The rows of HOST (StartTree.fill's plan) for its STARTS (in the order shown): the host, below it each start
        that belongs to no other one, below that start the starts that belong to it (start_tree.owners: its MCP
        servers and helpers, the members of a team it leads) and what else belongs to it (related), then the MCP
        servers that belong to no start, together below a row that says so. "Only the current project" keeps a
        start with what belongs to it, by the start at its top."""
        owned = owners(starts)
        alone = set(orphans(starts, owned))
        parts = related(starts)
        by_key = {i.key: i for i in starts}

        def top(key):
            return owned[key][0] if key in owned else key
        only = self.only_project.isChecked()
        tops = [i for i in starts if i.key not in owned and i.key not in alone and (not only or self.mine(i))]
        below = {}
        for i in starts:
            if i.key in owned:
                below.setdefault(owned[i.key][0], []).append(i)
        lonely = [i for i in starts if i.key in alone and (not only or self.mine(i))]
        listed = [*tops, *(i for t in tops for i in below.get(t.key, [])), *lonely]
        host_id = row_id(host)
        plan = [(None, host_id, "host", None, self.host_row(host, listed))]

        def add(parent, i):
            start_id = row_id(host, i.key)
            belongs = (by_key[owned[i.key][0]], owned[i.key][1]) if i.key in owned else \
                "orphan" if i.key in alone else None
            plan.append((parent, start_id, "start", i.key, {**self.start_row(i, belongs), "bold": self.mine(i)}))
            return start_id

        def add_parts(start_id, i):
            for part in parts.get(i.key, []):
                if part["key"] and part["key"] in by_key and top(part["key"]) == top(i.key):
                    continue  # its own row is in this group already
                plan.append((start_id, row_id(host, f"{i.key}\n{part['kind']}\n{part['name']}"), "part",
                             part["key"], self.part_row(host, i, part)))
        for i in tops:
            start_id = add(host_id, i)
            for child in below.get(i.key, []):
                add_parts(add(start_id, child), child)
            add_parts(start_id, i)
        if lonely:
            group_id = row_id(host, ORPHANS)
            plan.append((host_id, group_id, "group", None, self.orphans_row(host, lonely)))
            for i in lonely:
                add_parts(add(group_id, i), i)
        return plan

    def orphans_row(self, host, servers):
        """{cells, color, tip, sort} of the row above the MCP servers of HOST that belong to no start."""
        cells = [tr("Belongs to no start"), tr("MCP servers: {count}", count=len(servers)), "", "", "", "", "", "",
                 tr("this machine") if host == LOCAL else printable(host), ""]
        return {"cells": cells, "color": "muted", "tip": tr(ORPHANS_HELP), "sort": ["\uffff"] * len(cells)}

    def show_instances(self):
        """The tree: each host (the one chosen above, or all), under it its starts, under each what belongs to it.
        The row selected stays selected (by host and id), the groups stay open or closed as they were left."""
        chosen = self.host_filter.currentData()
        self.rows = self.listed()
        order = {"run": 0, "session": 1, "mcp": 2, "unregistered": 3}

        def sort_key(i):
            return (getattr(i, "host", "local") != "local", not self.mine(i), order.get(i.kind, 9),
                    str(getattr(i, "host", "")), str(i.name))
        every = sorted((i for i in self.rows.values() if chosen is None or getattr(i, "host", "local") == chosen),
                       key=sort_key)
        names = list(getattr(self, "remote_names", []))
        hosts = [LOCAL, *names, *sorted({i.host for i in every} - {LOCAL} - set(names))] if chosen is None else [chosen]
        tree = self.table
        plan = []  # (parent row id, row id, kind, key, cells...): StartTree.fill
        for host in hosts:
            plan += self.host_plan(host, [i for i in every if i.host == host])
        expanded = (lambda ident, item: self.open_groups.get(
            ident, self.hosts_open if item.data(0, KIND_ROLE) in ("host", "group") else self.starts_open))
        same_rows, changed = tree.same_rows(plan), not tree.shows(plan)
        if same_rows and changed:  # the same rows, some cells changed (a state, "seen 12 s ago"): set in place
            tree.blockSignals(True)
            try:
                tree.fill(plan, expanded)
            finally:
                tree.blockSignals(False)
        elif changed:  # other rows: the tree is built again, the selection, folds and scroll kept
            previous = [item.data(0, ID_ROLE) for item in tree.selectedItems()]  # (several: Ctrl, Shift)
            scroll = tree.verticalScrollBar().value()
            tree.blockSignals(True)  # rebuilt: no selection or fold changes are said (selection_changed() below)
            try:
                tree.setSortingEnabled(False)
                tree.fill(plan, expanded)
                tree.setSortingEnabled(True)  # the order the human chose by a header, if any
                again = [item for item in (tree.find_item(ident) for ident in previous) if item is not None]
                for number, item in enumerate(again):
                    if number == 0:
                        tree.select_item(item, open_groups=False)
                    else:
                        item.setSelected(True)
            finally:
                tree.blockSignals(False)
            tree.verticalScrollBar().setValue(scroll)
        if getattr(self, "wanted", "") and tree.selected_item() is None and find_row(tree, self.wanted) >= 0:
            tree.select_item(tree.start_items()[find_row(tree, self.wanted)])  # (not selectRow: the tab stays)
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
            text += " · " + tr("{host}: no answer", host=printable(name)) + f" ({printable_text(problem)[:120]})"
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

    def bridge_sessions(self):
        """The agent sessions of this machine that run: what a bridge may join (New bridge…)."""
        from .bridges_view import candidates
        return candidates(self.instances.values())

    def bridge_remote_sessions(self):
        """The agent sessions of the user's hosts that ran at their last look, as HOST:NAME (bridge add takes them)."""
        from .bridges_view import candidates
        return [entry for entry in candidates(self.instances.values(), remote=True) if entry["host"]]

    def bridge_selected(self):
        """The name of the selected start when a bridge may join it (New bridge… offers it as session A)."""
        instance = self.current()
        names = {entry["name"] for entry in self.bridge_sessions()}
        return instance.name if instance is not None and instance.name in names else None

    def current(self):
        """The start of the row selected, or None (none selected, several, or a host's row or a part of a start)."""
        if self.several():
            return None
        item = self.table.selected_item()
        if item is None or item.data(0, KIND_ROLE) != "start":
            return None
        return getattr(self, "rows", self.instances).get(item.data(0, KEY_ROLE))

    def no_start(self):
        """Why the buttons do not act now: no row selected, several, or one that is not a start."""
        if self.several():
            return tr("{count} rows are selected: Stop, Pause, Resume and Delete act on each of them; the other "
                      "buttons on one row.", count=len(self.table.selectedItems()))
        item = self.table.selected_item()
        kind = item.data(0, KIND_ROLE) if item is not None else None
        if kind == "host":
            return tr("A host's row: select one of its starts below it.")
        if kind == "part":
            return tr("What belongs to the start above it: select the start itself to act on it.")
        if kind == "group":
            return tr("MCP servers that belong to no start: select one of them below to act on it.")
        return tr("Select a row first.")

    def several(self):
        """True while more than one row is selected (Ctrl+click, Shift+click)."""
        return len(self.table.selectedItems()) > 1

    @staticmethod
    def log_asked(action, instance=None, detail=""):
        """What the human asked of a start on this page, into the log at INFO: the action, the start's kind, name,
        host and state (DETAIL: what else was chosen). The command it runs and how it ends follow (command,
        background); a refusal is said where it is shown."""
        if instance is None:
            applog.log("INFO", "sandboxes", f"{action}: asked" + (f" ({detail})" if detail else ""))
            return
        host = getattr(instance, "host", LOCAL)
        applog.log("INFO", "sandboxes", f"{action}: asked for {instance.kind} {printable(instance.name)} on "
                   f"{'this machine' if host == LOCAL else printable(host)} (state {instance.state})"
                   + (f", {detail}" if detail else ""))

    def selected_starts(self):
        """The starts of the rows selected, top to bottom (hosts' rows and what belongs to a start left out)."""
        rows = getattr(self, "rows", self.instances)
        found = []
        for item in self.table.every_item():
            if item.isSelected() and item.data(0, KIND_ROLE) == "start":
                instance = rows.get(item.data(0, KEY_ROLE))
                if instance is not None and instance not in found:
                    found.append(instance)
        return found

    def bulk_why(self, instance, action):
        """Why ACTION (BULK_ACTIONS) cannot act on INSTANCE as one of several (None: it can) - as the buttons say for
        one; a stopped session is resumed alone (its HOME is reviewed first)."""
        remote = getattr(instance, "host", LOCAL) != LOCAL
        kind, state = instance.kind, instance.state
        shown = tr(STATE_TITLES.get(state, state))
        if instance.key.startswith("ended:"):
            return tr("It ended: History… shows what happened to it.")
        if remote and self.stale(instance) is not None:
            return tr("{host} does not answer.", host=printable(instance.host))
        if kind == "unregistered":
            return tr("It has not registered with sandbox-ai (yet).")
        if action == "stop":
            if kind == "run" and not remote and not (self.stoppable and instance.run):
                return tr("This sandbox-ai cannot stop it from outside: end it in its terminal.")
            ok = state in ("running", "paused") if remote or kind == "session" else state in ACTIVE_STATES
        elif kind == "mcp":
            return tr("An MCP server only stops.")
        elif action == "delete":
            if kind != "session":
                return tr("Only for sessions.")
            ok = state == "stopped"
        elif kind not in ("session", "run") or (kind == "run" and not remote and not backend.valid_run(instance.name)):
            return tr("Only for sessions.")
        elif action == "pause":
            ok = state == "running"
        else:
            if state == "stopped" and kind == "session":
                return tr("Stopped: resume it alone (its HOME is reviewed first).")
            ok = state == "paused"
        return None if ok else tr("Not while it is {state}.", state=shown)

    def update_bulk_buttons(self):
        """Several rows selected: Stop, Pause, Resume and Delete act on those of them they can act on; every other
        button acts on one row and waits."""
        starts, why = self.selected_starts(), self.no_start()
        for button in [*self.run_buttons, *self.transfer_buttons.values(), self.resume_with_button,
                       self.manage_buttons["rename"], self.manage_buttons["history"]]:
            set_enabled(button, False, why)
        for action, button in [("stop", self.stop_button), *self.session_buttons.items()]:
            if action not in BULK_ACTIONS:
                set_enabled(button, False, why)
                continue
            able = [instance for instance in starts if self.bulk_why(instance, action) is None]
            reason = None if able else tr("None of the selected can: {why}", why=self.bulk_why(starts[0], action)) \
                if starts else why
            set_enabled(button, bool(able), reason,
                        tooltip=tr("{action}: {count} of the {total} selected", action=tr(ACTION_TITLES[action]),
                                   count=len(able), total=len(starts)))
        self.update_report_button()
        host = self.host_filter.currentData()
        for key in ("usage", "clean"):
            set_enabled(self.manage_buttons[key], host is not None,
                        None if host is not None else tr("Choose a host above (or select a start of the host to clean "
                                                         "up on)."))

    def bulk(self, action):
        """ACTION (BULK_ACTIONS) on every selected start it can act on: one question naming them (and those left
        out, with why), then one after the other in the background; what failed is said together at the end."""
        starts = self.selected_starts()
        able = [instance for instance in starts if self.bulk_why(instance, action) is None]
        title = tr(ACTION_TITLES[action])
        if not able:
            applog.log("INFO", "sandboxes", f"{action} of {len(starts)} selected: none of them can")
            inform(self, title, tr("None of the selected can: {why}", why=self.bulk_why(starts[0], action))
                   if starts else self.no_start(), modal=False)
            return
        names = [self.bulk_name(instance) for instance in able]
        self.log_asked(f"{action} of {len(able)} selected", detail="for " + ", ".join(names) + (
            "; left out: " + ", ".join(f"{self.bulk_name(i)} ({self.bulk_why(i, action)})" for i in starts
                                       if i not in able) if len(able) < len(starts) else ""))
        text = {"stop": tr("Stop these {count} starts? Their agents end; what they did not save is lost (a session's "
                           "HOME is kept).", count=len(able)),
                "pause": tr("Pause these {count} starts?", count=len(able)),
                "resume": tr("Resume these {count} starts?", count=len(able)),
                "delete": tr("Delete these {count} sessions and their HOME? This cannot be undone.",
                             count=len(able))}[action]
        text = several(text, names)
        left = [instance for instance in starts if instance not in able]
        if left:
            text += "\n\n" + tr("Left out:") + "\n" + "\n".join(
                f"- {self.bulk_name(instance)}: {self.bulk_why(instance, action)}" for instance in left)
        if action == "delete":
            asked = ask_first(self, "start-delete", title, text, title)
        elif action == "stop":  # (MCP servers alone: asked as their stop is; with starts: as a start's)
            asked = ask_first(self, "mcp-stop", title, text, title) if all(i.kind == "mcp" for i in able) else \
                ask_first(self, "start-stop", title, text, title)
        else:
            asked = confirm(self, title, text, title)
        if not asked:
            return
        wait = self.stop_wait.value()
        if action == "resume":
            for host in sorted({getattr(instance, "host", LOCAL) for instance in able} - {LOCAL}):
                self.started_on.emit(host)

        def call(instance):
            host = getattr(instance, "host", LOCAL)
            if host != LOCAL:
                args = [action, instance.name] + (["--yes"] if action == "delete" else [])
                if action == "stop" and wait != STOP_WAIT:
                    args = backend.stop_args(instance.name, wait)
                return self.sai.host_run(host, *args, timeout=180 + wait)
            if action == "stop":
                if instance.kind == "session":
                    return self.sai.call(*backend.stop_args(instance.name, wait), timeout=wait + 60) \
                        if wait != STOP_WAIT else self.sai.call("session", "stop", instance.name, timeout=330)
                if instance.kind == "mcp":
                    return self.sai.stop_mcp(instance.mcp)
                return self.sai.stop_run(instance.run)
            if instance.kind == "run":
                return self.sai.call(action, instance.name, timeout=150)
            return self.sai.call("session", action, instance.session or instance.name,
                                 *(["--yes"] if action == "delete" else []), timeout=150)

        def done(outcomes):
            report_each(self, title, outcomes, self.bulk_name)
            self.refresh_soon(again=True)
            hosts = sorted({getattr(instance, "host", LOCAL) for instance in able} - {LOCAL})
            if hosts:
                self.refresh_remote(hosts)
        run_each(call, able, owner=self, source=[self.stop_button, *self.session_buttons.values()],
                 timeout=len(able) * (330 + wait), done=done)

    def bulk_name(self, instance):
        host = getattr(instance, "host", LOCAL)
        name = printable(instance.name)
        return name if host == LOCAL else tr("{name} on {host}", name=name, host=printable(host))

    # -- the kill switch (sandbox-ai net cut) --

    @staticmethod
    def cut_run_of(instance):
        """The run INSTANCE's kill switch is set for: its run on this machine, the run its host's listing names."""
        if getattr(instance, "host", LOCAL) == LOCAL:
            return instance.run
        record = instance.record or {}
        return record.get("run") if isinstance(record.get("run"), str) else instance.name

    def cut_why(self, instance):
        """Why the kill switch cannot act on INSTANCE (None: it can): only a sandbox that runs has a network to cut."""
        if instance.key.startswith("ended:"):
            return tr("It ended: History… shows what happened to it.")
        if getattr(instance, "host", LOCAL) != LOCAL and self.stale(instance) is not None:
            return tr("{host} does not answer.", host=printable(instance.host))
        if instance.kind == "unregistered":
            return tr("It has not registered with sandbox-ai (yet).")
        # An orphaned start runs too (its supervisor gone, its pod on): net cut reaches it - its control socket, else
        # its pod is stopped.
        if instance.state not in ("running", "paused", "orphaned") or \
                not backend.valid_run(self.cut_run_of(instance) or ""):
            return tr("It does not run.")
        return None

    def cut_targets(self):
        """The starts selected (one or several) whose network the kill switch can cut."""
        chosen = self.selected_starts() if self.several() else [i for i in [self.current()] if i is not None]
        return [instance for instance in chosen if self.cut_why(instance) is None]

    def update_cut_button(self):
        if widgets.held(self.cut_button):
            return  # its own cut runs: enabled again once it is done
        if getattr(self, "cuttable", None) is False:
            set_enabled(self.cut_button, False, tr("This sandbox-ai has no kill switch (sandbox-ai net cut came with "
                                                   "0.1.94): update it."))
            return
        chosen = self.selected_starts() if self.several() else [i for i in [self.current()] if i is not None]
        targets = [instance for instance in chosen if self.cut_why(instance) is None]
        why = None if targets else self.cut_why(chosen[0]) if chosen else self.no_start()
        set_enabled(self.cut_button, bool(targets), why,
                    tooltip=tr("Cut the network of the selected sandboxes at once, also of several, with their MCP "
                               "servers, helpers and team (sandbox-ai net cut); Lift the cut gives back what they "
                               "may reach"))

    def cut_now(self):
        """The cut that holds for the selected starts that run (CUT_CHOICES' modes, "remote+api" for several at
        once): the same for all of them, "" for none; None when they differ (or none is selected)."""
        found = {backend.cut_name((i.record or {}).get("cut")) or "" for i in self.cut_targets()}
        return found.pop() if len(found) == 1 else None

    def show_cut_now(self):
        """The kill switch's menu, opened again, says what holds (the State column says it too): the mode cut now
        ticked, each part of several; Lift the cut only when something is cut."""
        now = self.cut_now()
        parts = set(now.split("+")) if now else set()
        for mode, item in self.cut_actions.items():
            if mode == "off":
                item.setEnabled(now != "")
                item.setToolTip(tr(next(tip for key, _, tip in CUT_CHOICES if key == mode)) if now != "" else
                                tr("Nothing is cut now."))
                continue
            item.setChecked(mode in parts)
            tip = tr(next(tip for key, _, tip in CUT_CHOICES if key == mode))
            item.setToolTip(safe_tooltip(tip + (" " + tr("(holds now)") if mode in parts else "")))

    def cut(self, mode):
        """The kill switch MODE (CUT_CHOICES) for the selected starts that run, at once and without a question (it
        only takes away; Lift the cut gives back): `sandbox-ai net cut RUN MODE` on each one's host, all at the same
        time (a host that does not answer holds up none of the others). The button stays usable: another mode chosen
        meanwhile follows the one still on its way for that sandbox (the later choice wins). What failed is said
        together; what holds shows in the State column and in a notification."""
        targets = self.cut_targets()
        title = tr("Kill switch: {mode}", mode=tr(next(text for key, text, _ in CUT_CHOICES if key == mode)))
        if not targets:
            inform(self, title, self.no_start() if self.current() is None and not self.several() else
                   tr("None of the selected runs: only a running sandbox's network can be cut."), modal=False)
            return
        batch = {"title": title, "mode": mode, "left": len(targets), "outcomes": [], "targets": targets}
        for instance in targets:
            self.cut_start(instance, mode, batch)

    def cut_key(self, instance):
        return getattr(instance, "host", LOCAL), self.cut_run_of(instance)

    def cut_start(self, instance, mode, batch):
        """One sandbox's cut, in a thread of its own; one after the other for the same sandbox (only the latest choice
        waits: one chosen before it and not yet sent is dropped)."""
        key = self.cut_key(instance)
        if key in self.cut_flights:
            dropped = self.cut_waiting.get(key)
            if dropped is not None:
                self.cut_finished(dropped[2], dropped[0], None, None)  # replaced before it was sent
            self.cut_waiting[key] = (instance, mode, batch)
            return
        self.cut_flights[key] = instance

        def call():
            result = self.sai.cut_run(key[1], mode, key[0])
            report = backend.cut_report(result)
            if report is None:
                return result
            failures = [f"{entry['name']}: {entry['error']}" for entry in report if not entry["ok"]]
            done_ = [entry["name"] for entry in report if entry["ok"]]
            return (1 if failures else 0, ", ".join(done_), "\n".join(failures))

        def done(result, error):
            self.cut_flights.pop(key, None)
            self.cut_finished(batch, instance, result, error)
            waiting = self.cut_waiting.pop(key, None)
            if waiting is not None:
                self.cut_start(*waiting)
        background(call, owner=self, work=True, timeout=backend.CUT_WAIT + 30, done=done)

    def cut_finished(self, batch, instance, result, error):
        """One sandbox of BATCH (one choice in the menu) is done; once all are, what failed and what holds is said."""
        batch["outcomes"].append((instance, result, error))
        batch["left"] -= 1
        if batch["left"]:
            return
        mode, outcomes = batch["mode"], batch["outcomes"]
        failures = report_each(self, batch["title"], outcomes, self.bulk_name)
        if batch.get("emergency") and not failures:  # the emergency stop once more (emergency_follows): said already
            self.refresh_soon(again=True)
            return
        reached = []
        for instance_, result_, error_ in outcomes:
            if instance_ not in failures and error_ is None and isinstance(result_, tuple):
                reached.append(printable(result_[1]) if result_[1] else self.bulk_name(instance_))
        if reached:
            text = tr("{names}: {what}.", names=", ".join(reached),
                      what=tr(next(tip for key, _, tip in CUT_CHOICES if key == mode)))
            if mode != "off":
                text += " " + tr("Kill switch › Lift the cut gives it back.")
            self.notice.emit("success" if mode == "off" else "warning", batch["title"], text)
        self.refresh_soon(again=True)
        hosts = sorted({getattr(target, "host", LOCAL) for target in batch["targets"]} - {LOCAL})
        if hosts:
            self.refresh_remote(hosts)

    def emergency_follows(self, title):
        """The emergency stop was sent for every sandbox (`net cut --all-runs all`, each one's cut on its own behalf).
        A sandbox's own cut chosen here before it and still on its way, or waiting, lands after it and - asked on that
        sandbox's behalf too - replaced the stop's (another mode) or took it back (Lift the cut): a sandbox the stop
        had cut reached the network again. The stop is the latest choice for each of them: what waits is dropped, and
        the stop is sent once more for each sandbox whose own cut is on its way, after that one (a later choice here
        still follows it)."""
        if not self.cut_flights:
            return
        batch = {"title": title, "mode": "all", "left": len(self.cut_flights), "outcomes": [],
                 "targets": list(self.cut_flights.values()), "emergency": True}
        for key, instance in list(self.cut_flights.items()):
            dropped = self.cut_waiting.pop(key, None)
            if dropped is not None:
                self.cut_finished(dropped[2], dropped[0], None, None)  # replaced before it was sent
            self.cut_waiting[key] = (instance, "all", batch)

    def lift_follows(self):
        """Lift every cut, asked after the emergency stop: the stop's second send (emergency_follows) not sent yet
        is dropped - the later choice holds."""
        for key, (instance, _mode, batch) in list(self.cut_waiting.items()):
            if batch.get("emergency"):
                del self.cut_waiting[key]
                self.cut_finished(batch, instance, None, None)

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
        if self.several():  # what acts on several, and what waits
            self.detail.setText(self.no_start())
        elif instance is None and item is not None:  # a host's row, or what belongs to a start: what it says
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
        gone = None if stale is None else tr("{host} does not answer (last seen {ago}): its state now is not "
                                             "known.", host=host, ago=ago(stale))
        if gone is None and instance.key.startswith("ended:"):
            gone = tr("It ended: History… shows what happened to it.")
        mcp = instance.kind == "mcp"
        for key, button in self.run_named.items():
            why = gone or (tr("An MCP server's sandbox: it only stops (sandbox-ai stop there).") if mcp else
                           None if running or key == "log" else tr("Not while it is {state} on {host}.",
                                                                   state=shown_state, host=host))
            set_enabled(button, why is None, why)
        self.update_browser_button(instance)
        self.update_mcp_buttons(instance, gone)
        # As sandbox-ai there takes them (starts.act): an MCP server only stops; a run without a kept start pauses
        # and resumes but keeps nothing to attach to or delete.
        rules = {"attach": state == "running" and attachable, "attach-app": state == "running" and attachable,
                 "shell": state == "running" and attachable, "pause": state == "running" and not mcp, "resume": state in ("paused", "stopped") and not mcp,
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
        self.update_transfer_buttons(instance)
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

    def update_credentials_button(self, instance):
        """Secrets and logins…: of the selected start's host, named ("… on HOST…" for another host)."""
        host = getattr(instance, "host", LOCAL) if instance is not None else None
        self.credentials_button.setText(tr("Secrets and logins on {host}…", host=printable(host))
                                        if host not in (None, LOCAL) else tr("Secrets and logins…"))
        self.credentials_button.setEnabled(host is not None)
        self.credentials_button.setToolTip(safe_tooltip(self.no_start() if host is None else tr(
            "The secrets and Git logins of {host}, where the selected start runs: the Credentials page with that host "
            "chosen (Give to a running sandbox… gives one to this start). For a start that runs: what it was given "
            "during its run, each taken back with Revoke…", host=self.host_title(host))))

    def credentials_of(self, instance=None):
        """The Credentials page with the selected start's host chosen."""
        instance = instance or self.current()
        if instance is not None:
            host = getattr(instance, "host", LOCAL) or LOCAL
            self.open_credentials.emit(host)
            # A start that runs: what it was given during its run (Git logins, API keys), each taken back there.
            run = getattr(instance, "run", None) if host == LOCAL else getattr(instance, "name", None)
            if run and getattr(instance, "state", "") == "running" and getattr(instance, "kind", "") in ("run",
                                                                                                         "session"):
                self.open_given.emit(host, str(run))

    # The run actions that add something to a running start or take it out again, grouped in its menus (the user,
    # 2026-11-23: the right-click menu was too long): Add… and Remove…, each opening the kinds - MCP server,
    # skills/plugins, toolchain, workspace, device.
    RUN_GROUPS = (("add", N_("Add…"), ("add-mcp", "add-kits", "add-toolchain", "add-workspace", "add-device")),
                  ("remove", N_("Remove…"), ("remove-mcp", "remove-kits", "remove-toolchain", "remove-workspace",
                                             "remove-device")))

    def table_menu_entries(self):
        """The right click on a start (widgets.context_menu): every command of the buttons around the list, grouped
        (the user 2026-11-24: one long list was overwhelming) - the most used first (to it: Show on the Board, into it;
        its run, the kill switch), then what it may reach and has (Network, Add…, Remove…, Limits…, its secrets), what is inside it,
        copy, move and export, and the rest under Manage; each group between separator lines."""
        session, transfer, manage, run = (self.session_buttons, self.transfer_buttons, self.manage_buttons,
                                          self.run_named)

        def pick(buttons, *keys):
            return [buttons[key] for key in keys if key in buttons]
        group = widgets.MenuGroup
        return [
            self.board_button,  # (the ways to it: first the Board with this session's tab and project)
            *pick(session, "attach", "attach-app", "shell"), *pick(run, "browser"), None,
            *pick(session, "resume"), self.resume_with_button, *pick(session, "pause"), self.stop_button,
            self.cut_button, None,
            group(tr("Network"), pick(run, "allow", "revoke", "allowed", "log", "rules")),
            *[group(tr(title), pick(run, *keys)) for _, title, keys in self.RUN_GROUPS],
            *pick(run, "limits"), self.credentials_button, None,
            group(tr("Look inside"), [*pick(transfer, "checkpoints", "memory", "files", "storage"),
                                      *pick(manage, "history", "report")]),
            group(tr("Copy, move, export"), pick(transfer, "copy", "move", "duplicate", "export", "export-state",
                                                 "import")),
            group(tr("Manage"), [*pick(manage, "rename"), *pick(session, "delete"), self.stop_all_button, None,
                                 *pick(manage, "usage", "clean", "optimize")]),
            None, self.refresh_button]

    def run_menu_entries(self):
        """The run buttons for a context menu (widgets.context_menu): the network's and the rest in their order, the
        add and remove actions as the submenus Add… and Remove… (RUN_GROUPS) where the first of them stood."""
        grouped = {key for _, _, keys in self.RUN_GROUPS for key in keys}
        entries, placed = [], False
        for button in self.run_buttons:
            key = next((k for k, b in self.run_named.items() if b is button), None)
            if key not in grouped:
                entries.append(button)
            elif not placed:
                placed = True
                entries += [widgets.MenuGroup(tr(title), [self.run_named[k] for k in keys if k in self.run_named])
                            for _, title, keys in self.RUN_GROUPS]
        return entries

    def show_on_board(self):
        """Show on the Board: the Board with the selected session's project (the window's board_request)."""
        instance = self.current()
        if self.board_request is not None and board_follow.why_not(instance) is None:
            self.log_asked("show-on-board", instance)
            return self.board_request(instance)
        return False

    def update_board_button(self):
        """Show on the Board: only with one session or sandbox selected (why not, in its tooltip)."""
        why = board_follow.why_not(self.current())
        set_enabled(self.board_button, why is None, why)

    def update_buttons(self):
        instance = self.current()
        self.update_cut_button()  # never held back by another command: the kill switch must act at once
        self.update_board_button()  # (it changes nothing: never held back either)
        self.update_credentials_button(None if self.several() else instance)
        if busy(self):
            # A command runs: its buttons stay greyed out until it is done; a row of another host never keeps
            # the buttons of the row chosen before (they would act on this machine). Rename, History and Clean up
            # follow the row chosen, so they wait too (they would act on a row the running command may change).
            if instance is not None and getattr(instance, "host", "local") != "local":
                for button in [*self.run_buttons, *self.session_buttons.values(), self.stop_button,
                               self.resume_with_button]:
                    set_enabled(button, False, tr("A command runs."))
            for button in [*self.manage_buttons.values(), *self.transfer_buttons.values()]:
                set_enabled(button, False, tr("A command runs."))
            return
        if self.several():
            self.update_bulk_buttons()
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
            self.update_transfer_buttons(instance, why)
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
        self.update_browser_button(instance)
        self.update_mcp_buttons(instance, run_why)
        rules = {"attach": state == "running", "attach-app": state == "running", "shell": state == "running",
                 "pause": state == "running", "resume": state in ("paused", "stopped"), "delete": state == "stopped"}
        no_terminal = instance is not None and terminal_of(instance) in ("missing", "ended")
        no_agent = no_terminal and terminal_of(instance) == "ended"
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
                      "there is nothing to attach to.") if foreground and action in TERMINAL_ACTIONS else
                   (tr(NO_AGENT) if no_agent else
                    tr("It runs without its terminal (its start did not make one): stop it and start it again. The "
                       "end of its log:")) + "\n" + printable_text((instance.record or {}).get("terminal_log") or "")
                   if no_terminal and action in TERMINAL_ACTIONS else
                   None if rules[action] else tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state))))
            set_enabled(button, why is None, why)
        why = (none if instance is None else tr("Only for kept starts (sessions).") if kind != "session" else
               None if state in ("running", "paused", "stopped") else
               tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state))))
        set_enabled(self.resume_with_button, why is None, why)
        self.update_transfer_buttons(instance)
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

    def update_transfer_buttons(self, instance, why=None):
        """Copy to host… and Move to host…: a session of this machine (sandbox-ai session copy|move here); Export…: a
        session of this machine or a host (export_why)."""
        given = why
        if why is None:
            state = getattr(instance, "state", None)
            why = (self.no_start() if instance is None else
                   tr("A session of {host}: only a session of this machine is copied or moved from here ({command} "
                      "runs where the session is).", host=printable(instance.host), command="sandbox-ai session copy")
                   if getattr(instance, "host", LOCAL) != LOCAL else
                   tr("Only for sessions: a run without a kept start has no configuration to start again elsewhere.")
                   if instance.kind != "session" or not instance.session else
                   None if state in ("running", "paused", "stopped") else
                   tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state))))
        for key, button in self.transfer_buttons.items():
            if key not in ("export", "checkpoints", "memory", "files", "import"):
                set_enabled(button, why is None, why)
        if "import" in self.transfer_buttons:  # (no start to select: an archive of anywhere)
            set_enabled(self.transfer_buttons["import"], True)
        export_why = given if given is not None else self.export_why(instance)
        set_enabled(self.transfer_buttons["export"], export_why is None, export_why)
        ended = instance is not None and getattr(instance, "key", "").startswith("ended:")
        checkpoints_why = self.checkpoints_why(instance) if given is None or ended else given
        set_enabled(self.transfer_buttons["checkpoints"], checkpoints_why is None, checkpoints_why)
        inspect_why = self.inspect_why(instance) if given is None or ended else given
        for key in ("memory", "files"):
            set_enabled(self.transfer_buttons[key], inspect_why is None, inspect_why)
        storage_why = self.storage_why(instance) if given is None or ended else given
        set_enabled(self.transfer_buttons["storage"], storage_why is None, storage_why)

    def storage_why(self, instance):
        """Why Storage… cannot open for INSTANCE, else None: a kept start of an agent (a session, of this machine or a
        host) in any state but damaged."""
        if instance is None:
            return self.no_start()
        if instance.kind == "mcp" or getattr(instance, "mcp", None):
            return tr("Only for starts of agents: an MCP server's sandbox keeps no HOME.")
        if getattr(instance, "key", "").startswith("ended:") or instance.kind != "session":
            return tr("Only a kept start (a session) has a HOME to look at.")
        if getattr(instance, "state", None) == "damaged":
            return tr("Not while the session is {state}.", state=tr(STATE_TITLES.get("damaged", "damaged")))
        if getattr(instance, "host", LOCAL) != LOCAL and self.stale(instance) is not None:
            return tr("{host} does not answer.", host=printable(instance.host))
        return None

    def open_storage(self, instance=None):
        """Storage…: the selected start's storage (storage_view.StorageDialog: gc --check --home NAME on its host)."""
        instance = instance or self.current()
        why = self.storage_why(instance)
        if why is not None:
            inform(self, tr("Storage"), why)
            return None
        host = getattr(instance, "host", LOCAL)
        name = instance.session if host == LOCAL and instance.session else instance.name
        self.log_asked("storage", instance)
        from .storage_view import open_dialog
        return open_dialog(self, self.sai, host, name, owner=self, applied=self.storage_applied)

    def optimize(self, host=None):
        """Optimize…: the storage check's plan on the host chosen above (All hosts: the selected start's host)."""
        host = host or self.clean_host()
        if host is None:
            inform(self, tr("Optimize"), tr("Choose a host above (or select a start of the host to clean up on)."),
                   modal=False)
            return None
        from .storage_view import open_dialog
        return open_dialog(self, self.sai, host, None, owner=self, applied=self.storage_applied)

    def storage_applied(self, host):
        """Something of the plan was applied on HOST: its starts read again (a packed HOME, the sizes)."""
        self.refresh_soon(again=True)
        if host != LOCAL:
            self.refresh_remote([host])

    def inspect_why(self, instance):
        """Why Memory… and Session files… cannot open for INSTANCE, else None: a start of an agent (of this machine or
        a host) that runs, is paused, or stopped with its HOME kept - as sandbox-ai session memory|files refuse."""
        if instance is None:
            return self.no_start()
        if instance.kind == "mcp" or getattr(instance, "mcp", None):
            return tr("Only for starts of agents: an MCP server's sandbox has no agent's memory or session files.")
        state = getattr(instance, "state", None)
        if getattr(instance, "key", "").startswith("ended:") or (state == "stopped" and instance.kind != "session"):
            return tr("It ended without keeping its HOME: there is nothing to read.")
        if state not in ("running", "paused", "orphaned", "stopped"):
            return tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state)))
        if not self.sai.session_inspect_supported(getattr(instance, "host", LOCAL)):
            return tr("This sandbox-ai cannot read a start's memory and session files yet ({command}): update it.",
                      command="sandbox-ai session memory")
        return None

    def open_inspect(self, kind, instance=None):
        """Memory… (KIND "memory") and Session files… ("files"): the selected start's agent memory or the agents' own
        session files, read only (session_inspect_dialog: sandbox-ai session memory|files on its host); Export…
        there writes the folder of a kept start of this machine."""
        instance = instance or self.current()
        why = self.inspect_why(instance)
        if why is not None:
            inform(self, tr("Agent memory") if kind == "memory" else tr("Session files"), why)
            return
        host = getattr(instance, "host", LOCAL)
        name = instance.session if host == LOCAL and instance.session else instance.name
        exportable = host == LOCAL and instance.kind == "session" and bool(instance.session)
        export_why = None if exportable else (
            tr("A start of {host}: its archive is written there ({command} on that host, or the web interface).",
               host=printable(host), command="sandbox-ai session export") if host != LOCAL else
            tr("Only a kept start (a session) has a folder to export: a run without one keeps nothing."))
        self.log_asked("memory" if kind == "memory" else "session-files", instance)
        from .session_inspect_dialog import open_dialog
        open_dialog(self, self.sai, name, kind, host=host, exportable=exportable, export_why=export_why, owner=self)

    def checkpoints_why(self, instance):
        """Why Checkpoints… cannot open for INSTANCE, else None: a start of an agent (of this machine or a host, in any
        state but damaged - one that ended keeps its checkpoints too)."""
        if instance is None:
            return self.no_start()
        if instance.kind == "mcp" or getattr(instance, "mcp", None):
            return tr("Only for starts of agents: an MCP server's sandbox has no checkpoints.")
        if getattr(instance, "state", None) == "damaged":
            return tr("Not while the session is {state}.", state=tr(STATE_TITLES.get("damaged", "damaged")))
        if not self.sai.checkpoints_supported():
            return tr("This sandbox-ai has no checkpoints yet ({command}): update it.",
                      command="sandbox-ai checkpoint")
        return None

    def open_checkpoints(self, instance=None):
        """Checkpoints…: the selected start's workspace checkpoints (checkpoints_dialog.CheckpointsDialog: sandbox-ai
        checkpoint on its host)."""
        instance = instance or self.current()
        why = self.checkpoints_why(instance)
        if why is not None:
            inform(self, tr("Checkpoints"), why)
            return
        host = getattr(instance, "host", LOCAL)
        name = instance.session if host == LOCAL and instance.session else instance.name
        self.log_asked("checkpoints", instance)
        from .checkpoints_dialog import CheckpointsDialog
        dialog = CheckpointsDialog(self, self.sai, name, host=host,
                                   running=getattr(instance, "state", None) in ("running", "paused"), owner=self)
        dialog.exec()
        dialog.deleteLater()

    def export_why(self, instance):
        """Why Export… cannot write the bundle of INSTANCE now, else None: a session (of this machine or a host;
        running, paused or stopped)."""
        state = getattr(instance, "state", None)
        if instance is None:
            return self.no_start()
        if getattr(instance, "key", "").startswith("ended:"):
            return tr("It ended: History… shows what happened to it.")
        if instance.kind != "session" or not (instance.session or getattr(instance, "host", LOCAL) != LOCAL):
            return tr("Only for sessions: a run without a kept start has no configuration to export.")
        if state not in ("running", "paused", "stopped"):
            return tr("Not while the session is {state}.", state=tr(STATE_TITLES.get(state, state)))
        if not self.sai.board_bundle_supported():
            return tr("This sandbox-ai cannot export a session yet ({command}): update it.",
                      command="sandbox-ai board export")
        return None

    def export_session(self, instance=None):
        """Export…: the selected session (of this machine or a host) as one file written here
        (bundle_dialogs.ExportDialog: sandbox-ai board export --session NAME, with --host for a host's)."""
        instance = instance or self.current()
        why = self.export_why(instance)
        if why is not None:
            QMessageBox.information(self, tr("Sessions"), shown_as_text(why))
            return
        host = getattr(instance, "host", LOCAL)
        name = instance.session if host == LOCAL and instance.session else instance.name
        self.log_asked("export", instance)
        from .bundle_dialogs import ExportDialog
        dialog = ExportDialog(self, self.sai, name, session=name, host=host, owner=self)
        dialog.exec()
        dialog.deleteLater()

    def update_browser_button(self, instance):
        """Browser: shows the browser of a running start of this machine that has one with a display (--browser,
        --browser-control human or both). It can be clicked for every start selected: when there is nothing to show,
        the click says why (a greyed-out entry of the right click did nothing, its reason only in a tooltip). Greyed
        out only for what has no browser at all: no start selected, an MCP server, a start without --browser."""
        from . import vnc_view
        button = self.run_named["browser"]
        why = self.browser_why(instance) if instance is not None else self.no_start()
        has = instance is not None and (vnc_view.browser_record(instance) is not None or self.wants_browser(instance))
        set_enabled(button, has, why)
        if has and why is not None:
            button.setToolTip(safe_tooltip(tr("Not now: {why}", why=why)))

    def wants_browser(self, instance):
        """Whether the kept start INSTANCE of this machine has the browser in its options (--browser): its run may
        have been started without it (the Board's part added later)."""
        if instance is None or instance.kind != "session" or getattr(instance, "host", LOCAL) != LOCAL:
            return False
        start = self.sai.session_start(getattr(instance, "ident", instance.name)) \
            if hasattr(self.sai, "session_start") else None
        return any(option in ("--browser", "-bw") or option.startswith("--browser=") for option in
                   (start[1] if start else []))

    def browser_why(self, instance):
        """Why INSTANCE's browser cannot be shown now (vnc_view.why_not, its display's socket refused, a session
        whose options have the browser but whose run was started without it), or None."""
        from . import vnc_view
        why = vnc_view.why_not(instance)
        if why is None:
            problem = vnc_view.socket_problem(vnc_view.browser_record(instance)["vnc"])
            if problem and problem != tr("it is not there (yet)"):
                return tr("Its browser's display cannot be opened: {path} is refused ({why}).",
                          path=vnc_view.browser_record(instance)["vnc"], why=problem)
            return None
        if vnc_view.browser_record(instance) is None and self.wants_browser(instance):
            if instance.state not in ("running", "paused"):
                return tr("It does not run: its browser starts with it (Resume).")
            return tr("This session's options have the browser (--browser), but the run that runs now was started "
                      "without it (the browser was added later - on the Board, for example - or an older sandbox-ai "
                      "started it). Restart the session to start it with its browser: Restart in its terminal tab's "
                      "right click, or Stop and Resume.")
        return why

    def open_browser(self, instance=None):
        """The live browser of INSTANCE (default: the selected start): a tab of Terminals beside its terminal
        (browser_opener), else a window of its own. When it cannot be shown, a dialog says why (and app.log)."""
        from . import vnc_view
        instance = instance or self.current()
        why = self.browser_why(instance)
        name = printable(getattr(instance, "name", "") or "?") if instance is not None else "-"
        if why is not None:
            applog.log("WARNING", "browser", f"the browser of {name} is not opened: {applog.redact(why)}")
            inform(self, tr("Browser"), why)
            return None
        applog.log("INFO", "browser", f"the browser of {name} is asked for")
        if self.browser_opener is not None:
            return self.browser_opener(instance)
        return vnc_view.open_browser(instance)

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
        # Skills and plugins for a running kept start (kits attach): the same choice as an MCP server's.
        for key in ("add-kits", "remove-kits"):
            set_enabled(self.run_named[key], why is None, why)
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
        self.update_report_button()
        host = self.clean_host(instance)
        stale_host = host not in (None, LOCAL) and host in self.remote_problems
        why = tr("Choose a host above (or select a start of the host to clean up on).") if host is None else \
            tr("{host} does not answer.", host=printable(host)) if stale_host else None
        set_enabled(self.manage_buttons["clean"], why is None, why)
        set_enabled(self.manage_buttons["usage"], why is None, why)
        set_enabled(self.manage_buttons["optimize"], why is None, why)

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
            applog.log("DEBUG", "sandboxes", f"rename of {printable(old)}: cancelled or unchanged")
            return
        self.log_asked("rename", instance, f"new name {printable(new)}")
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
            warn(self, title, printable_text(error), modal=False)
            return
        lines = ["  ".join([history_time(entry.get("time")), printable(str(entry.get("event") or "?")),
                            printable(str(entry.get("run") or "-")), history_details(entry)]).rstrip()
                 for entry in found]
        show_text(self, title, "\n".join(lines) or tr("No history of {name}.", name=printable(name)), modal=False)

    # -- reports (sandbox-ai report) --

    @staticmethod
    def report_name(instance):
        """The name `sandbox-ai report` takes for INSTANCE: a start's name, an MCP server's run (mcp-NAME)."""
        if instance.kind == "mcp":
            if instance.run:
                return instance.run
            name = instance.mcp or instance.name
            return name if str(name).startswith("mcp-") else f"mcp-{name}"
        return instance.name

    def report_why(self, instance):
        """Why INSTANCE has no report here (None: it has; a start of another host too - made there)."""
        if getattr(instance, "host", LOCAL) != LOCAL and self.stale(instance) is not None:
            return tr("{host} does not answer.", host=printable(instance.host))
        if instance.kind == "unregistered":
            return tr("It has not registered with sandbox-ai (yet).")
        if not backend.valid_run(self.report_name(instance)):
            return tr("Not a start's name: {ident}", ident=printable(instance.name)[:60])
        return None

    def report_targets(self):
        """([instance], why): the selected starts a report covers - one host's: this machine's when any is selected
        (the others are left out), else those of the one host selected; why none can."""
        starts = self.selected_starts() if self.several() else [i for i in [self.current()] if i is not None]
        able = [instance for instance in starts if self.report_why(instance) is None]
        hosts = list(dict.fromkeys(getattr(i, "host", LOCAL) for i in able))
        if len(hosts) > 1:
            if LOCAL not in hosts:
                return [], tr("The selected starts are on several hosts: a report is of one host's starts.")
            able = [i for i in able if getattr(i, "host", LOCAL) == LOCAL]
        why = None if able else self.report_why(starts[0]) if starts else self.no_start()
        return able, why

    def update_report_button(self):
        able, why = self.report_targets()
        if able and getattr(able[0], "host", LOCAL) == LOCAL and not self.sai.has_report():
            able, why = [], tr("This sandbox-ai writes no reports yet: update it (sandbox-ai update).")
        set_enabled(self.manage_buttons["report"], bool(able), why)

    def report(self):
        """Report… for the selected starts of this machine: one report of all of them, with their runs and parts."""
        able, why = self.report_targets()
        if not able:
            inform(self, tr("Report"), why or self.no_start(), modal=False)
            return None
        host = getattr(able[0], "host", LOCAL)
        names = list(dict.fromkeys(self.report_name(instance) for instance in able))
        label = names[0] if len(names) == 1 else tr("{count} starts", count=len(names))
        what = tr("A report of {names}: each with all its runs, running or stopped.", names=", ".join(
            printable(name) for name in names))
        from .reports import make_report
        return make_report(self, self.sai, label, what, names=names, owner=self,
                           source=[self.manage_buttons["report"]], host=host,
                           notice=lambda level, title, text: self.notice.emit(level, title, text))

    def host_title(self, host):
        return tr("this machine") if host in (None, LOCAL) else printable(host)

    def disk_usage(self):
        """`sandbox-ai gc --usage` on the host chosen above (All hosts: the selected start's host): what takes how much
        space there, then Clean up… (with or without the download caches) from there."""
        host = self.clean_host()
        if host is None:
            inform(self, tr("Disk usage"), tr("Choose a host above (or select a start of the host to clean up on)."),
                   modal=False)
            return
        title = tr("Disk usage on {host}", host=self.host_title(host))
        background(self.sai.state_usage, host, owner=self, source=list(self.manage_buttons.values()), timeout=360,
                   done=lambda found, error: self.usage_shown(host, title, found, error))

    def usage_shown(self, host, title, found, error):
        self.update_buttons()
        if error is not None:
            warn(self, title, printable_text(error), modal=False)
            return
        choice = widgets.choose(self, title, on_host(host, "\n".join(usage_lines(found))),
                                [("clean", tr("Clean up…")), ("caches", tr("Clean up with the caches…")),
                                 ("detail", tr("HOMEs in depth…"))],
                                cancel=tr("Close"))
        if choice == "detail":
            self.homes_in_depth(host)
        elif choice is not None and self.clean_host() == host:
            self.clean_up(caches=choice == "caches")

    def homes_in_depth(self, host):
        """`sandbox-ai gc --usage --detail` on HOST: what in each kept HOME is big, by kind (download caches,
        toolchains, agent state, build output, the agent's work) - measured only."""
        title = tr("HOMEs in depth on {host}", host=self.host_title(host))
        background(self.sai.home_details, host, owner=self, source=list(self.manage_buttons.values()), timeout=360,
                   done=lambda found, error: self.details_shown(host, title, found, error))

    def details_shown(self, host, title, found, error):
        self.update_buttons()
        if error is not None:
            warn(self, title, printable_text(error), modal=False)
            return
        inform(self, title, on_host(host, "\n".join(detail_lines(found))), modal=False)

    def clean_up(self, caches=False):
        """`sandbox-ai gc` on the host chosen above (All hosts: the selected start's host): what would go is shown
        first (--dry-run), the host named in the question. CACHES: the download caches of the kept HOMEs too."""
        host = self.clean_host()
        if host is None:
            inform(self, tr("Clean up"), tr("Choose a host above (or select a start of the host to clean up on)."),
                   modal=False)
            return
        title = tr("Clean up on {host}", host=self.host_title(host))
        collect = functools.partial(self.sai.collect, caches=True) if caches else self.sai.collect
        background(collect, host, True, owner=self, source=list(self.manage_buttons.values()), timeout=150,
                   done=lambda found, error: self.clean_previewed(host, title, found, error, caches))

    def clean_previewed(self, host, title, found, error, caches=False):
        self.update_buttons()
        if error is not None:
            warn(self, title, printable_text(error), modal=False)
            return
        lines, pending = gc_lines(found), gc_pending_lines(found)
        if not lines:
            short_notice(self, title, "\n".join([tr("Nothing to remove."), *pending]))
            return
        text = on_host(host, tr("Remove these? Kept starts are deleted with their HOME; this cannot be undone.")) + \
            "\n\n" + "\n".join([*lines, *([""] + pending if pending else [])])
        if not ask_first(self, "gc", title, text, tr("Remove")):
            return

        def done(result, error_):
            if isinstance(error_, backend.GcChanged):
                warn(self, title, tr("Nothing was removed: what would go has changed since it was shown. Clean up… "
                                     "shows it again."), modal=False)
            elif error_ is not None:
                warn(self, title, printable_text(error_), modal=False)
            else:
                lines = gc_lines(result)
                short_notice(self, title, tr("Removed:") + "\n" + "\n".join(lines) if lines else
                             tr("Nothing to remove."))
            self.refresh_soon(again=True)
            if host != LOCAL:
                self.refresh_remote([host])
        # The same --older-than as the list shown, and only if a dry run still lists exactly that (GcChanged).
        remove = functools.partial(self.sai.collect_as_shown, caches=True) if caches else self.sai.collect_as_shown
        background(remove, host, found, owner=self, source=list(self.manage_buttons.values()), work=True,
                   timeout=1830, done=done)

    def stop_all_targets(self):
        """{host: [Instance]} of what Stop all… stops: the starts (not the MCP servers) that run, are paused or
        orphaned - on this machine and every host (All hosts), or on the host chosen above."""
        chosen = self.host_filter.currentData()
        found = {}
        local = [i for i in self.instances.values() if getattr(i, "host", LOCAL) == LOCAL]
        for host, starts in [(LOCAL, local), *self.remote.items()]:
            if chosen is not None and host != chosen:
                continue
            wanted = [i for i in starts if i.kind in ("session", "run") and i.state in STOP_ALL_STATES]
            if wanted:
                found[host] = wanted
        return found

    def stop_all(self):
        """Stop all…: the list asked about first (start-stop; Cancel the default), then `sandbox-ai stop --all --yes`
        on this machine and `hosts run HOST -- stop --all --yes` on each host, in the background; the list is read
        again after it."""
        targets = self.stop_all_targets()
        self.log_asked("stop all", detail="; ".join(
            f"{'this machine' if host == LOCAL else printable(host)}: " + ", ".join(printable(i.name) for i in starts)
            for host, starts in targets.items()) or "nothing runs that could be stopped")
        if not targets:
            short_notice(self, tr("Stop all"), tr("Nothing runs that could be stopped."))
            return False
        lines = [f"{tr('this machine') if host == LOCAL else printable(host)}: "
                 + ", ".join(printable(i.name) for i in starts) for host, starts in targets.items()]
        count = sum(len(starts) for starts in targets.values())
        if not ask_first(self, "start-stop", tr("Stop all"), tr(
                "Stop these {count} starts? Each ends as Stop ends it; what a session keeps stays.", count=count)
                + "\n\n" + "\n".join(lines), tr("Stop all")):
            return False
        hosts = list(targets)

        def run():
            results = []
            for host in hosts:
                try:
                    results.append((host, self.sai.host_run(host, "stop", "--all", "--yes", timeout=900)
                                    if host != LOCAL else self.sai.call("stop", "--all", "--yes", timeout=900)))
                except Exception as exc:  # noqa: BLE001 - said with the others
                    results.append((host, (1, "", str(exc))))
            return results

        def done(results, error):
            if error is not None:
                warn(self, tr("Stop all"), printable_text(error), modal=False)
            else:
                applog.log("INFO" if all(r[0] == 0 for _host, r in results) else "WARNING", "sandboxes",
                           "stop all ended: " + ", ".join(
                               f"{'this machine' if host == LOCAL else printable(host)} exit status {r[0]}"
                               for host, r in results))
                said = [f"{tr('this machine') if host == LOCAL else printable(host)}: "
                        + printable_text(((out or "") + (err or "")).strip())[-1500:]
                        for host, (status, out, err) in results]
                failed = any(status != 0 for _, (status, _out, _err) in results)
                (warn if failed else inform)(self, tr("Stop all"), "\n\n".join(said), modal=False)
            self.refresh_soon(again=True)
            remote = [host for host in hosts if host != LOCAL]
            if remote:
                self.refresh_remote(remote)
        background(run, owner=self, work=True, source=[self.stop_button, self.stop_all_button], timeout=1900,
                   done=done)
        return True

    def stop(self, instance=None, asked=False):
        """Stop the selected start (several: each), or INSTANCE; asked first unless ASKED (the human chose Stop
        session in a question of its own already: a terminal tab's Close)."""
        if instance is None and self.several():
            self.bulk("stop")
            return
        instance = instance or self.current()
        if instance is None:
            return
        self.log_asked("stop", instance, f"wait {self.stop_wait.value()} s" + (" (asked already)" if asked else ""))
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
        if not asked and not (ask_first(self, "mcp-stop", tr("Stop"), text, tr("Stop")) if instance.kind == "mcp" else
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
        """The devices and workspaces added to the selected sandbox during its run (in the background): their lines
        below the list change only with what they say - the page reads them at each refresh, and emptied first, they
        blinked every few seconds (the user 2026-11-24); nothing added: no line (only news is shown)."""
        instance = self.current()
        run = instance.run if instance and getattr(instance, "host", "local") == "local" else None
        self.devices_token += 1
        token = self.devices_token
        host = getattr(instance, "host", LOCAL) if instance is not None else LOCAL
        key = getattr(instance, "key", None)
        if key != getattr(self, "devices_of", None):  # another start: the lines of the one before go at once
            self.devices_of = key
            self.show_added(self.devices_label, "")
            self.show_added(self.workspaces_label, "")
        if host != LOCAL and instance.state in ("running", "paused") and backend.valid_run(instance.name) and \
                self.stale(instance) is None:
            # What was added there during the run, as that host lists it (devices|workspace list NAME --json there).
            self.added, self.added_workspaces = [], []
            name = instance.name
            background(self.sai.host_devices_added, host, name, timeout=90,
                       done=lambda found, error: self.devices_loaded(token, [label for _, label in found or []], None)
                       if error is None else None)
            background(self.sai.host_workspaces, host, name, timeout=90,
                       done=lambda found, error: self.workspaces_loaded(token, found, None) if error is None else None)
            return
        if not run:
            self.added, self.added_workspaces = [], []
            self.show_added(self.devices_label, "")
            self.show_added(self.workspaces_label, "")
            return
        background(self.sai.devices_added, run, timeout=30,
                   done=lambda found, error: self.devices_loaded(token, found, error))
        background(self.sai.workspaces, run, timeout=30,
                   done=lambda found, error: self.workspaces_loaded(token, found, error))

    @staticmethod
    def show_added(label, text):
        """LABEL says TEXT (set only when it changes: no repaint for the same words), hidden while it says nothing."""
        if label.text() != text:
            label.setText(text)
        label.setVisible(bool(text))

    def workspaces_loaded(self, token, found, error):
        if token != self.devices_token:
            return
        entries = list(found or []) if error is None else []
        instance = self.current()
        if getattr(instance, "host", LOCAL) != LOCAL:  # a host's: shown, never what Remove… acts on here
            added = [e for e in entries if e.get("source") == "added"]
            self.show_added(self.workspaces_label, tr(
                "Workspaces added during the run on {host}: {list}", host=printable(instance.host),
                list=", ".join(map(workspace_label, added))) if added else "")
            return
        self.added_workspaces = [e for e in entries if e.get("source") == "added"]
        if isinstance(error, (AttributeError, ImportError)):
            self.show_added(self.workspaces_label, tr("This sandbox-ai version cannot add workspaces during a run."))
            return
        def shown(entry):
            note = entry.get("note") if isinstance(entry.get("note"), str) else ""  # what sandbox-ai says of it
            return workspace_label(entry) + (f": {printable(note)[:300]}" if note else "")
        self.show_added(self.workspaces_label, tr("Workspaces added during the run: {list}",
                                                  list=", ".join(map(shown, self.added_workspaces)))
                        if self.added_workspaces else "")

    def devices_loaded(self, token, found, error):
        if token != self.devices_token:
            return
        instance = self.current()
        if getattr(instance, "host", LOCAL) != LOCAL:  # a host's: shown, never what Remove device acts on here
            found = list(found or [])
            self.show_added(self.devices_label, tr("Added during the run on {host}: {list}",
                                                   host=printable(instance.host), list=", ".join(map(printable, found)))
                            if found else "")
            return
        self.added = list(found or []) if error is None else []
        if isinstance(error, AttributeError):
            self.show_added(self.devices_label, tr("This sandbox-ai version cannot change devices during a run."))
        else:
            self.show_added(self.devices_label, tr("Added during the run: {list}",
                                                   list=", ".join(map(printable, self.added))) if self.added else "")

    def on_host(self, host, method, *args):
        """METHOD(*ARGS) for a sandbox of HOST, to run in the background (another host: host=HOST)."""
        return (lambda: method(*args)) if host == "local" else (lambda: method(*args, host=host))

    def add_device(self):
        target = self.target()
        if not target:
            return
        host, run = target

        def read():  # in the background: the devices of the run's host, this machine's /dev, what the run has already
            candidates = self.sai.device_candidates() if host == LOCAL else []  # (another host's /dev: its scan only)
            try:
                scanned, problem = DP.scan(self.sai, host), None
            except Exception as exc:  # noqa: BLE001 - said in the dialog; a path may still be typed
                scanned, problem = [], exc
            try:
                added = self.sai.devices_added(run) if host == LOCAL else \
                    [label for _, label in self.sai.host_devices_added(host, run)]
            except (AttributeError, ImportError, ValueError, OSError):  # an older sandbox-ai, no answer: not said
                added = None
            try:
                listing = self.sai.devices_listed(run, host)
            except (ValueError, OSError):  # (not read: nothing greyed out for it; devices add says it)
                listing = []
            return candidates, added, {"scanned": scanned, "problem": problem, "listing": listing}
        background(read, owner=self, source=self.run_buttons, timeout=120,
                   done=lambda found, error: self.choose_device(run, *(found if error is None else ([], None, {})),
                                                                host=host))

    def choose_device(self, run, candidates, added=None, found=None, host="local"):
        self.update_buttons()
        remote = host != "local"
        key = ("device", host, run)  # a path typed and not sent: filled in again (drafts.py)
        dialog = DeviceDialog(self, candidates, tr("Add device to {run} on {host}", run=printable(run),
                                                   host=printable(host)) if remote else None, added=added, draft=key,
                              host=host, **(found or {}))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted or not dialog.path():
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
                           work=True, timeout=150,
                           done=lambda result, error: self.device_done(title, result, error, draft=key))
            return
        background(self.sai.device_check, path, owner=self, source=self.run_buttons, timeout=30,
                   done=lambda result, error: self.confirm_device(run, path, result, error))

    def confirm_device(self, run, path, result, error):
        """The launcher's own notes on the device (what it exposes, udev, Bluetooth) before it is added."""
        self.update_buttons()
        if error is not None:
            warn(self, tr("Not added"), printable_text(error))
            return
        resolved, notes = result
        text = "\n".join([tr("Give {path} to {run} until it ends? Devices weaken the isolation.",
                             path=printable(resolved), run=printable(run)), ""] + [printable(n) for n in notes])
        if ask_first(self, "device-add", tr("Add device"), text, tr("Add")):
            background(self.sai.add_device, run, path, owner=self, work=True, source=self.run_buttons, timeout=150,
                       done=lambda result, error: self.device_done(tr("Add device"), result, error,
                                                                   draft=("device", LOCAL, run)))

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
            short_notice(self, tr("Remove device"), tr("No device was added to {run} during its run (devices given "
                                                       "at the start stay).", run=printable(run)))
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
            warn(self, title, tr("What was added there could not be read: {error}", error=printable_text(error)[:500]),
                 modal=False)
            return
        if not found:
            short_notice(self, title, tr("No device was added to {run} during its run (devices given at the start "
                                         "stay).", run=printable(run)))
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

    def device_done(self, title, result, error, draft=None):
        """What a change of the devices or workspaces did; taken, the DRAFT it was typed in is done with."""
        if draft is not None and outcome(result, error)[0] == 0:
            drafts.drop(draft)
        report(self, title, result, error)
        self.update_buttons()
        self.load_devices()

    # -- build toolchains of a running sandbox (sandbox-ai toolchain attach, detach) --

    def add_toolchain(self):
        """Add toolchain…: the store and the signed catalog of the run's host and what the run has already (in the
        background), then the choice."""
        target = self.target()
        if not target:
            return
        host, run = target

        def read():
            found = self.sai.host_toolchains(host)
            try:
                have = self.sai.toolchains_of_run(run, host)
            except (ValueError, OSError):
                have = []
            return found, [row["name"] for row in have]
        background(read, owner=self, source=self.run_buttons, timeout=150,
                   done=lambda result, error: self.choose_toolchain(host, run, result, error))

    def choose_toolchain(self, host, run, result, error):
        self.update_buttons()
        title = tr("Add toolchain to {run} on {host}", run=printable(run), host=printable(host)) if host != "local" \
            else tr("Add toolchain to {run}", run=printable(run))
        if error is not None:
            warn(self, title, printable_text(str(error)))
            return
        found, have = result
        # every version of a toolchain not attached (one version of a toolchain per sandbox): the store's, then the
        # signed catalog's others, each by what `toolchain attach` and `add` take for it (jdk, jdk@17)
        choices = backend.toolchain_choices(found, attached=set(have))
        if not choices:
            inform(self, title, tr("Every toolchain of the store is attached to it already.") if found["installed"]
                   else tr("No toolchain is in the store, and the signed catalog offers none ({problem}). In a "
                           "terminal: sandbox-ai toolchain catalog", problem=found.get("problem") or tr("it is empty")))
            return
        labels = {}
        for choice in choices:
            row = choice["row"]
            label = tr("{name} {version}: {commands}", name=choice["name"], version=printable(row["version"]),
                       commands=printable(", ".join(row.get("commands") or [])[:80])) if choice["stored"] else \
                tr("{name} {version} (built on this machine from its publisher's downloads first: a license to "
                   "accept)", name=choice["name"], version=printable(row["version"])) \
                if backend.license_terms(row) else \
                tr("{name} {version} (downloaded from the signed catalog first)", name=choice["name"],
                   version=printable(row["version"]))
            labels[label] = choice
        chosen, ok = QInputDialog.getItem(self, title, tr("Toolchain"), list(labels), 0, False)
        if not ok or chosen not in labels:
            return
        choice = labels[chosen]
        name, value, row, download = choice["name"], choice["value"], choice["row"], not choice["stored"]
        opens = [*(row.get("hosts") or []), *(f"--allow-{preset}" for preset in row.get("allow") or [])]
        text = tr("Mount {name} read-only into {run} at /opt/toolchains/{name}? Its commands are on the agent's "
                  "PATH from then on.", name=name, run=printable(run))
        if opens:
            text += "\n\n" + tr("Its registries are opened for the run: {hosts} (not in the security modes high and "
                                "paranoid).", hosts=printable(", ".join(opens)))
        terms = backend.license_terms(row) if download else None
        if download and terms:
            text += "\n\n" + tr("It is built in the store first from {by}'s downloads ({size} MiB, each checked "
                                "against the signed catalog's recipe); its license is asked next.", by=terms["by"],
                                size=int((row.get("size") or 0) / (1 << 20)))
        elif download:
            text += "\n\n" + tr("It is downloaded from the signed catalog into the store first ({size} MiB).",
                                size=int((row.get("size") or 0) / (1 << 20)))
        if not ask_first(self, "toolchain-add", title, text, tr("Add")):
            return
        # A license is the human's to accept: a question of its own, No its default; only then its option is given.
        if terms and not widgets.accept_license(self, tr("The toolchain {name}", name=name), terms):
            return

        def work():
            if download:
                status, out, err = self.sai.toolchain_add(value, host=host, accept=terms["option"] if terms else None)
                if status != 0:
                    return status, out, err
            return self.sai.toolchain_attach(run, value, host=host)
        background(work, owner=self, source=self.run_buttons, work=True, timeout=3900,
                   done=lambda result, error: self.device_done(title, result, error))

    def remove_toolchain(self):
        """Remove toolchain…: one attached during the run, chosen from what its host lists."""
        target = self.target()
        if not target:
            return
        host, run = target
        background(lambda: self.sai.toolchains_of_run(run, host), owner=self, source=self.run_buttons, timeout=90,
                   done=lambda found, error: self.choose_toolchain_removal(host, run, found, error))

    def choose_toolchain_removal(self, host, run, found, error, force=False, name=None):
        self.update_buttons()
        title = tr("Remove toolchain from {run}", run=printable(run))
        if error is not None:
            warn(self, title, printable_text(str(error)))
            return
        added = [row["name"] for row in found if row.get("source") == "added"]
        if not added:
            short_notice(self, title, tr("No toolchain was attached to {run} during its run (toolchains given at "
                                         "the start stay).", run=printable(run)))
            return
        name = added[0]
        if len(added) > 1:
            name, ok = QInputDialog.getItem(self, title, tr("Toolchain"), added, 0, False)
            if not ok or name not in added:
                return
        self.detach_toolchain(host, run, name, title)

    def detach_toolchain(self, host, run, name, title, force=False):
        def done(result, error):
            status, out, err = outcome(result, error)
            if status != 0 and not force and "--force" in (out + err) and ask_first(
                    self, "toolchain-force", title, tr("{name} is still in use in {run}: a program of it runs or has "
                                                       "one of its files open. Detach it anyway? What is open stays "
                                                       "usable until it is closed.", name=name, run=printable(run)),
                    tr("Detach anyway")):
                self.detach_toolchain(host, run, name, title, force=True)
                return
            self.device_done(title, result, error)
        background(lambda: self.sai.toolchain_detach(run, name, host=host, force=force), owner=self,
                   source=self.run_buttons, work=True, timeout=180, done=done)

    def add_workspace(self):
        """Add workspace…: what the run has been given already (`workspace list` on its host, in the background),
        then the dialog."""
        target = self.target()
        if not target:
            return
        self.add_workspace_for(*target)

    def target_of(self, instance):
        """(host, run) of the running start INSTANCE as target() names a selected one; None when it does not run."""
        if instance is None:
            return None
        if getattr(instance, "host", "local") != "local":
            ok = instance.state in ("running", "paused") and backend.valid_run(instance.name)
            return (instance.host, instance.name) if ok else None
        return ("local", instance.run) if instance.run and instance.state in ("running", "paused") else None

    def add_workspace_for(self, host, run, path=None):
        """Add workspace… for RUN on HOST (the Terminals tab menu's Add folder…, the Files page's Add to a running
        session…: PATH filled in): what it has been given already read first, then the dialog."""

        def read():
            try:
                found = self.sai.workspaces(run) if host == LOCAL else self.sai.host_workspaces(host, run)
            except (AttributeError, ImportError, ValueError, OSError):  # an older sandbox-ai, no answer: not said
                return None
            return [entry for entry in found or [] if entry.get("source") == "added"]
        background(read, owner=self, source=self.run_buttons, timeout=120,
                   done=lambda added, error: self.workspace_dialog(host, run, added if error is None else None, path))

    def workspace_dialog(self, host, run, added, path=None):
        self.update_buttons()
        remote = host != "local"
        key = ("workspace", host, run)  # what was typed and not sent: filled in again (drafts.py)
        dialog = WorkspaceDialog(self, run, host=host, added=added, draft=key)
        if path:  # (chosen in the Files page: that folder, its name and read-only still to choose)
            dialog.edit.setText(str(path))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted or not dialog.directory():
            return
        directory, read_only = dialog.directory(), dialog.read_only.isChecked()
        name = dialog.name()
        remote_folder = backend.valid_remote(directory)
        if not backend.valid_directory(directory) and not remote_folder:
            warn(self, tr("Not added"), tr("Give the folder or file as an absolute path (or a folder of another "
                                           "machine: sshfs://user@machine/path)."))
            return
        if name and not backend.valid_workspace_name(name):
            warn(self, tr("Not added"), tr("A name is 1-64 letters, digits, _ . -, starting with a letter or digit."))
            return
        title = tr("Add workspace on {host}", host=printable(host)) if remote else tr("Add workspace")
        if not ask_first(self, "workspace-writable", title, tr(
                "Let {run} change the files of {path} until it ends? It is a folder of another machine, mounted with "
                "sshfs; the agent writes and deletes there, on that machine.", run=printable(run),
                path=printable(directory)) if remote_folder else tr(
                "Let {run} change {path} on {host} until it ends? The agent can write, delete and run there.",
                run=printable(run), path=printable(directory), host=printable(host)) if remote else tr(
                "Let {run} change {path} until it ends? The agent can write, delete and run there.",
                run=printable(run), path=printable(directory)), tr("Add"), detail={"read_only": read_only}):
            return
        background(self.on_host(host, self.sai.add_workspace, run, directory, read_only, *([name] if name else [])),
                   owner=self, work=True, source=self.run_buttons, timeout=150,
                   done=lambda result, error: self.workspace_added(title, result, error, draft=key))

    def workspace_added(self, title, result, error, draft=None):
        """The outcome of `sandbox-ai workspace add`: added with a warning (a folder you chose with a risk: HOME,
        sandbox-ai's own folders, a kernel tree...) is shown as a warning that stays, with what the launcher said;
        else as any change of a running sandbox (device_done)."""
        from . import board as B
        status, out, err = outcome(result, error)
        said = B.launcher_warnings(f"{err}\n{out}") if status == 0 else []
        if not said:
            self.device_done(title, result, error, draft=draft)
            return
        drafts.drop(draft)
        warn(self, title, B.path_warnings(said) + "\n\n" + printable_text(out.strip()), modal=False)
        self.update_buttons()
        self.load_devices()

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
            warn(self, title, tr("What was added there could not be read: {error}", error=printable_text(error)[:500]),
                 modal=False)
            return
        added = [e for e in entries or [] if e.get("source") == "added"]
        if not added:
            short_notice(self, title, tr("No workspace was added to {run} during its run (those of the start stay).",
                                         run=printable(run)))
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
            refusal = printable_text((err or out).strip())[-1500:]
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
            QMessageBox.information(self, tr("Sessions"), tr("Select a running kept start (a session) first."))
            return None
        host, name = self.start_host(instance)
        if not backend.valid_run(name):
            warn(self, tr("Sessions"), tr("Not a start's name: {ident}", ident=printable(name)[:60]))
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
        self.log_asked("add MCP server (live)", instance)
        background(self.sai.host_mcp_servers, host, owner=self, source=self.run_buttons, timeout=90,
                   done=lambda rows, error: self.choose_live_mcp(host, name, instance.agent, rows, error,
                                                                 attached=self.live_servers(instance)))

    def choose_live_mcp(self, host, start, agent, rows, error, attached=()):
        self.update_buttons()
        servers = [(row[0], row[1]) for row in rows or []] if error is None else []
        dialog = LiveMcpDialog(self, start, host, servers, agent, problem=None if error is None else str(error),
                               attached=attached)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        kind, value = dialog.value()
        if kind == "copy":
            self.copy_mcp(host, start)
            return
        server, helper = (value, None) if kind == "server" else (None, value)
        applog.log("INFO", "sandboxes", f"add MCP server (live) to {printable(start)} on {printable(host)}: "
                   f"{'server' if server else 'helper'} {printable(value)} chosen")
        what = tr("the MCP server {name}", name=printable(value)) if server else \
            tr("{agent} as a helper", agent=HELPER_TITLES.get(value, printable(value)))
        title = tr("Add MCP server on {host}", host=printable(host)) if host != LOCAL else tr("Add MCP server")
        try:
            backend.mcp_live_args(start, server, helper)
        except ValueError as exc:
            warn(self, title, printable_text(exc))
            return
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        if not ask_first(self, "mcp-attach", title, where + tr(
                "Give {start} the tools of {what} now, while it runs, and at its later starts?", start=printable(start),
                what=what), tr("Attach")):
            return
        background(lambda: self.sai.host_mcp_attach_live(host, start, server, helper), owner=self, work=True,
                   source=self.run_buttons,
                   timeout=630, done=lambda result, error_: self.live_mcp_done(host, start, title, result, error_))

    def copy_mcp(self, host, start):
        """From another host…: a server of another host chosen and checked (McpCopyDialog), asked first (mcp-copy;
        mcp-replace too where one of that name is replaced), then copied to HOST and attached to the running START
        there (`sandbox-ai mcp copy ... --attach START`, run here: it reaches both hosts)."""
        dialog = McpCopyDialog(self, self.sai, host, start)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        source, name, choices = dialog.values()
        title = tr("Add MCP server on {host}", host=printable(host)) if host != LOCAL else tr("Add MCP server")
        applog.log("INFO", "sandboxes", f"add MCP server (live) to {printable(start)} on {printable(host)}: "
                   f"{printable(name)} of {printable(source)} chosen to copy")
        if not ask_first(self, "mcp-copy", title, copy_question(source, name, host, start, choices), tr("Copy and add")):
            return
        if choices.get("replace") and not ask_first(self, "mcp-replace", title, tr(
                "An MCP server {name} is there already: replace its definition for every start that uses it?",
                name=printable(choices.get("as_name") or name)), tr("Replace")):
            return
        background(lambda: self.sai.host_mcp_copy(source, host, [name], dry_run=False, attach=start, **choices),
                   owner=self, work=True, source=self.run_buttons, timeout=3700,
                   done=lambda result, error: self.mcp_copied(host, start, title, result, error))

    def mcp_copied(self, host, start, title, result, error):
        """What the copy said; attached: as an attach says it (the offer to restart the agent where it needs one)."""
        self.update_buttons()
        self.after_live_change(host)
        if error is None and blocked_copy(result):
            from .mcp_copy_view import plan_lines
            warn(self, title, tr("Not copied: {reason}", reason="\n".join(plan_lines(result))[:3000]), modal=False)
            return
        attached = ((result or {}).get("attached") or [{}]) if error is None else [{}]
        applog.log("INFO" if error is None else "WARNING", "sandboxes", f"copy MCP server to {printable(start)} on "
                   f"{printable(host)}: " + ("copied" if error is None else f"not copied: {applog.error_text(error)}"))
        if error is not None:
            warn(self, title, tr("Not copied: {reason}", reason=printable_text(error)[:1500]), modal=False)
            return
        first = attached[0] if attached and isinstance(attached[0], dict) else {}
        if first.get("error"):
            warn(self, title, tr("Copied, but not attached to {start}: {reason}", start=printable(start),
                                 reason=printable_text(first["error"])[:1500]), modal=False)
            return
        self.live_mcp_done(host, start, title, first, None)

    def live_mcp_done(self, host, start, title, result, error):
        """What sandbox-ai said of the attach, as it said it; where the agent needs (or may need) a restart to see
        the server, the offer to restart it now: inside its sandbox where sandbox-ai says that gives it the server
        (restart_in_place: `sandbox-ai kits restart-agent START`, asked as agent-restart - the sandbox goes on), else
        the restart of the whole start that keeps its session (`sandbox-ai resume START --restart`, asked as
        start-restart), said as such before anything is done. Reported live: a Codex session given a server while it
        ran ended with exit status 143 - the only restart offered was resume --restart, and it said "the conversation
        goes on" of a sandbox that ended."""
        self.update_buttons()
        self.after_live_change(host)
        applog.log("INFO" if error is None else "WARNING", "sandboxes", f"add MCP server (live) to {printable(start)} "
                   f"on {printable(host)}: " + (f"attached, agent restart {result.get('agent_restart') or 'not needed'}"
                                               if error is None else f"not attached: {applog.error_text(error)}"))
        if error is not None:
            warn(self, title, tr("Not attached: {reason}", reason=printable_text(error)[:1500]), modal=False)
            return
        text = "\n\n".join([result.get("message") or "", *result.get("notes", [])]).strip()
        if result.get("agent_restart") in ("required", "maybe"):
            hint = tr("Codex and OpenCode see a new server only after a restart.") \
                if result.get("agent_restart") == "required" else tr("If /mcp does not list it, restart the agent.")
            if result.get("restart_in_place") is True:
                if ask_first(self, "agent-restart", title, text + "\n\n" + hint + " " + tr(
                        "Start the agent again inside its sandbox now (sandbox-ai kits restart-agent {start})? What it "
                        "does now ends; its conversation continues, the sandbox, its network and MCP servers stay.",
                        start=printable(start)), tr("Start the agent again")):
                    # (kits restart-agent: its agent again in its tmux window, the sandbox goes on)
                    from .kits_live_view import restarted
                    background(lambda: self.sai.host_agent_restart(host, start), owner=self, work=True,
                               source=self.run_buttons, timeout=150,
                               done=lambda done_, failed: (restarted(self, title, done_, failed),
                                                           self.after_live_change(host)))
                return
            if ask_first(self, "start-restart", title, text + "\n\n" + hint + " " + tr(
                    "The agent of {start} cannot be started again inside its sandbox so that it sees the server: the "
                    "whole start is restarted instead (sandbox-ai resume {start} --restart) - its sandbox ends and "
                    "starts again, its HOME and conversation are kept, the conversation continues.",
                    start=printable(start)), tr("Restart the start (its sandbox starts again)")):
                self.restart_agent(host, start, title)
            return
        short_notice(self, title, text)

    def restart_agent(self, host, start, title):
        """`sandbox-ai resume START --restart` on HOST: the whole start again (its sandbox ends and starts anew, its HOME
        and conversation kept), then attached in the app again - the tab that showed it ended with its sandbox (exit
        status 143), and a restart from here is no tab's own, so nothing else brings it back."""
        args = backend.resume_args(start, restart=True)
        current = self.current()
        instance = current if current is not None and getattr(current, "host", LOCAL) == host and \
            start in (current.name, getattr(current, "session", None)) else None

        def done(result, error):
            self.command_done(title, result, error)
            self.after_live_change(host)
            if instance is not None and outcome(result, error)[0] == 0:
                self.session("attach-app", instance)
        background(lambda: self.sai.host_run(host, *args, timeout=300), owner=self, work=True, source=self.run_buttons,
                   timeout=330, done=done)

    def after_live_change(self, host):
        """The start's rows are read again: the server it reaches now shows below it."""
        self.refresh_soon(again=True)
        if host != LOCAL:
            self.refresh_remote([host])

    def live_servers(self, instance):
        """The MCP servers INSTANCE reaches now (run.json's mcp_servers, `sandbox-ai list`'s mcp_running)."""
        from .start_tree import reached
        return [name for name, now in reached(instance) if now]

    def add_kits(self):
        """Add skills/plugins…: a kit, skill folder, command, subagent or plugin into the selected running start now
        (kits_live_view: asked first, then what the agent needs to see it - a restart inside the sandbox offered)."""
        target = self.live_start()
        if not target:
            return
        host, name, instance = target
        from .kits_live_view import GiveDialog, give
        self.log_asked("add skills/plugins (live)", instance)
        dialog = GiveDialog(self, self.sai, host=host, start=name)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        _, start, kind, value, keep = dialog.values()
        give(self, self.sai, host, start, kind, value, keep, source=self.run_buttons,
             after=lambda: (self.update_buttons(), self.after_live_change(host)))

    def remove_kits(self):
        """Remove skills/plugins…: what Add skills/plugins… gave the selected running start (its name, a kit's name or
        a plugin NAME@MARKETPLACE), taken out again, asked first."""
        target = self.live_start()
        if not target:
            return
        host, name, instance = target
        from .kits_live_view import take_out
        value, ok = QInputDialog.getText(self, tr("Remove skills/plugins"), tr(
            "What was given: a skill, command or subagent (its name), a kit, or a plugin as NAME@MARKETPLACE"))
        if not ok or not value.strip():
            return
        self.log_asked("remove skills/plugins (live)", instance)
        take_out(self, self.sai, host, name, value.strip(), source=self.run_buttons,
                 after=lambda: (self.update_buttons(), self.after_live_change(host)))

    def remove_mcp(self):
        """Remove MCP server…: one of the servers the start reaches now (as its rows say; else as `sandbox-ai mcp
        list` there says whom each serves), taken out with `mcp detach … --live` - a helper as --helper AGENT."""
        instance = self.current()
        if instance is not None and instance.kind == "session" and instance.state != "running":
            # Stopped or paused: out of its options, for its later starts (mcp detach without --live).
            host, name = self.start_host(instance)
            if not backend.valid_run(name):
                warn(self, tr("Sessions"), tr("Not a start's name: {ident}", ident=printable(name)[:60]))
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
            short_notice(self, title, tr("{start} has no MCP server in its options.", start=printable(start)))
            return
        if not servers:
            (inform if error is not None else short_notice)(self, title, tr(
                "{start} reaches no MCP server now.", start=printable(start)) + (
                " " + tr("The servers there could not be read: {error}", error=printable_text(error)[:300])
                if error is not None else ""), modal=False)
            return
        labels = [tr("{agent} as a helper ({name})", agent=HELPER_TITLES[backend.helper_of(s)], name=s)
                  if backend.helper_of(s) else s for s in servers]
        choice, ok = QInputDialog.getItem(self, title, tr("MCP server"), labels, 0, False)
        if not ok or choice not in labels:
            return
        server = servers[labels.index(choice)]
        helper = backend.helper_of(server)
        applog.log("INFO", "sandboxes", f"remove MCP server {'(live) ' if live else ''}from {printable(start)} on "
                   f"{printable(host)}: {printable(server)} chosen")
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
        applog.log("INFO" if error is None else "WARNING", "sandboxes", f"{title}: " + (
            "removed" if error is None else f"not removed: {applog.error_text(error)}"))
        if error is not None:
            reason = "\n".join(printable(line) for line in str(error).splitlines())  # its lines, not \x0a
            warn(self, title, tr("Not removed: {reason}", reason=reason[:1500]), modal=False)
            return
        short_notice(self, title, "\n\n".join([result.get("message") or "", *result.get("notes", [])]).strip())

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
                                             error=printable_text(error)[:300])
        dialog = LimitsDialog(self, run, host=host, current=current if error is None else None, note=note)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
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

    def reported(self, title, result, error, draft=None):
        """What a change of a running sandbox did; once it was taken, the DRAFT it was typed in is done with."""
        if draft is not None and outcome(result, error)[0] == 0:
            drafts.drop(draft)
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
        QMessageBox.information(self, tr("Sessions"), tr("Select a running sandbox first."))
        return None

    def network(self, op):
        target = self.target()
        if not target:
            return
        host, run = target

        def read():  # in the background: what sandbox-ai there takes, and what the run may reach now
            found = self.sai.host_net_options(host, op) if host != "local" else self.sai.net_options(op)
            status, out, _ = self.sai.host_run(host, "net", "show", run, "--json", timeout=60)
            return found, given_network(out) if status == 0 else None
        if host != "local":
            title = tr("Allow for {run} on {host}", run=printable(run), host=printable(host)) if op == "allow" else \
                tr("Revoke for {run} on {host}", run=printable(run), host=printable(host))
        else:
            title = tr("Allow for {run}", run=run) if op == "allow" else tr("Revoke for {run}", run=run)
        # What sandbox-ai there takes (its own `net allow --help`, kept per host and version), not what the one here
        # does; and what the run has already, shown in the dialog (a second Erlauben… never looks as if the first was
        # lost). Asked in the background.
        background(read, owner=self, source=self.run_buttons, timeout=150,
                   done=lambda found, error: self.network_dialog(
                       host, run, op, title, found[0] if error is None else None, error,
                       current=found[1] if error is None else None))

    def network_dialog(self, host, run, op, title, supported, error, current=None):
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
        key = ("net", op, host, run)  # what was typed and not sent: filled in again (drafts.py)
        dialog = NetworkDialog(self, presets, title, supported, current=current, op=op, draft=key)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted or not dialog.arguments():
            if dialog.wants_credentials:  # Git login or API key…: given from Credentials, for this host
                self.open_credentials.emit(host)
            return
        arguments = dialog.arguments()
        keep = dialog.kept()
        done = lambda result, error_: self.reported(title, result, error_, draft=key)  # noqa: E731
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
        wildcards = [value for option, value in zip(arguments, arguments[1:])
                     if option == "--allow-host" and value.startswith("*.")]

        def shared_asked(found):
            """FOUND (sai.shared_wildcards): the wildcards below a shared service, with their risk - asked by itself
            (ask_first "shared-wildcard", naming the risk and the shared suffix); a yes passes each as
            --allow-shared-wildcard, a no sends nothing."""
            if not found:
                send(arguments)
                return
            risks = "\n".join(f"• {printable(risk)}" for risk, _ in found.values())
            if not ask_first(self, "shared-wildcard", title, tr(
                    "{run} ({where}) would get a wildcard below a shared service, where anyone can rent a name:\n\n"
                    "{risks}\n\nAllow it anyway?", run=printable(run), where=where, risks=risks),
                    tr("Allow anyway")):
                return
            # (each --allow-host *.D it names becomes --allow-shared-wildcard *.D; flags such as --dns stay as they are)
            send(["--allow-shared-wildcard" if token == "--allow-host" and index + 1 < len(arguments)
                  and arguments[index + 1].lower() in found else token for index, token in enumerate(arguments)])

        def send(arguments):
            background(self.sai.host_run, host, "net", "allow", run, *arguments, *(["--keep"] if keep else []),
                       owner=self, work=True, source=self.run_buttons, timeout=150, done=done)

        if wildcards and "--allow-shared-wildcard" in (supported or ()):
            # Below a shared service (anyone can rent a name there): its risk read in the background, then asked;
            # else (an older sandbox-ai there) it refuses such a wildcard itself, saying why.
            background(self.sai.shared_wildcards, wildcards, owner=self, source=self.run_buttons, timeout=60,
                       done=lambda found, error_: shared_asked(found if error_ is None else {}))
            return
        send(arguments)

    def show_allowed(self):
        """What it may reach…: the Approvals page's Inspection › Allowed now for the selected running sandbox (the
        window opens it); its plain text (sandbox-ai net show) is copied from there."""
        target = self.target()
        if target:
            self.show_reach.emit(*target)

    def reload_rules(self):
        """`sandbox-ai rules reload RUN` (on its host): the selected running sandbox reads its HTTP rules anew - asked
        first (what you loosened in your rulesets applies at once)."""
        target = self.target()
        if not target:
            return
        host, run = target
        title = tr("Reload the rules of {run} on {host}", run=printable(run), host=printable(host)) if host != LOCAL \
            else tr("Reload the rules of {run}", run=printable(run))
        if not backend.valid_run(run) or not ask_first(self, "rules-reload", title, tr(
                "Let {run} read its HTTP rules anew, as your rulesets and its project's are now? What you loosened in "
                "them applies at once; the administrator's and the security mode's rules stay as they were.",
                run=printable(run)), tr("Reload")):
            return
        background(self.sai.rules_reload, run, host, owner=self, source=self.run_buttons, timeout=120, work=True,
                   done=lambda result, error: self.rules_reloaded(title, result, error))

    def rules_reloaded(self, title, result, error):
        if error is not None:
            warn(self, title, tr("Not reloaded: {error}", error=printable_text(error)[-800:]))
            return
        lines = [tr("Its connection proxy holds these rulesets now:")]
        for source in result.get("rules") or []:
            if isinstance(source, dict):
                lines.append(f"  {printable(source.get('name'))}: " + tr("{count} HTTP rules", count=len(
                    source.get("http") or [])) + (" " + tr("(kept from its start)") if source.get("kept") else ""))
        if result.get("not_reloaded"):
            lines.append(tr("MCP, tool and device rules of {list} apply from its next start.",
                            list=printable(", ".join(result["not_reloaded"]))))
        short_notice(self, title, "\n".join(lines))

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
        if instance is None and action in BULK_ACTIONS and self.several():
            self.bulk(action)
            return
        instance = instance or self.current()
        if instance is not None:
            self.log_asked(action, instance)
        if instance is not None and getattr(instance, "host", "local") != "local":
            host = printable(instance.host)
            if action in ("attach", "attach-app"):
                try:
                    self.terminal(self.sai.host_terminal(instance.host, "attach", instance.name),
                                  f"{host}: {printable(instance.name)}", embedded=action == "attach-app")
                except Exception as exc:  # noqa: BLE001 - shown, the window stays
                    warn(self, tr("Not attached"), printable_text(exc))
                return
            if action == "shell":
                self.open_shell(instance)
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
            applog.log("INFO", "sandboxes", f"{action}: not done - "
                       + ("no start selected" if instance is None else f"{instance.kind} {printable(instance.name)} is "
                                                                       "not a session"))
            QMessageBox.information(self, tr("Sessions"), tr("Select a session first."))
            return
        name = instance.session
        if action == "shell":
            self.open_shell(instance)
            return
        if action in ("attach", "attach-app"):
            try:
                self.terminal(self.sai.command("session", "attach", name), tr("session {name}", name=name),
                              embedded=action == "attach-app")
            except Exception as exc:  # noqa: BLE001 - attaching that cannot be run is a message, the window stays
                warn(self, tr("Not attached"), printable_text(exc))
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

    def open_shell(self, instance):
        """A new terminal tab running `sandbox-ai shell NAME` for the running start INSTANCE (here, or on its host over
        SSH as attach does): a shell of the human under the agent's rules. A tab of its own each time (a shell is
        no session to come back to: closing the tab ends it)."""
        host = getattr(instance, "host", LOCAL)
        title = tr("shell: {name}", name=printable(instance.name))
        older = None
        if host != LOCAL:
            with contextlib.suppress(Exception):  # (its kept status only, no SSH; not known: tried)
                older = self.sai.module("hosts").older_than(host, (0, 1, 97))
        if older:
            # (else its terminal tab would only show that host's usage line: invalid choice 'shell')
            warn(self, tr("No shell opened"), tr("The sandbox-ai on {host} is {version}: sandbox-ai shell needs 0.1.97 "
                                                 "or later. Update it first (Hosts: Update, or sandbox-ai hosts update "
                                                 "{host}).", host=printable(host), version=printable(older)))
            return
        try:
            argv = (self.sai.command("shell", instance.name) if host == LOCAL else
                    self.sai.host_terminal(host, "shell", instance.name))
            self.terminal(argv, title if host == LOCAL else f"{printable(host)}: {title}", embedded=True)
        except Exception as exc:  # noqa: BLE001 - shown, the window stays
            warn(self, tr("No shell opened"), printable_text(exc))

    def resume_with(self, instance=None):
        """Resume with…: the selected kept start (of this machine or a host) started again with what the dialog adds,
        or restarted - each widening asked first: a restart (start-restart), workspaces and writable mounts
        (workspace-writable), MCP servers (mcp-attach). A stopped one's HOME is reviewed first (resume_checked)."""
        instance = instance or self.current()
        if instance is None or instance.kind != "session":
            QMessageBox.information(self, tr("Sessions"), tr("Select a session first."))
            return
        host, name = self.start_host(instance)
        if host != LOCAL:  # the MCP servers defined on that host (sandbox-ai mcp list --json there), then the dialog
            background(self.sai.host_mcp_servers, host, owner=self, source=[self.resume_with_button], timeout=90,
                       done=lambda rows, error: self.resume_dialog(instance, host, name, sorted(
                           row[0] for row in rows or []) if error is None else []))
            return
        servers = []
        try:
            servers = sorted(self.sai.mcp_definitions())
        except Exception as _exc:  # noqa: BLE001 - a damaged mcp.json: typed in then
            applog.swallowed("ui", "SandboxesPage.resume_with: a damaged mcp.json: typed in then", _exc)
            servers = []
        self.resume_dialog(instance, host, name, servers)

    def resume_dialog(self, instance, host, name, servers):
        """Resume with…'s dialog for INSTANCE (NAME on HOST) with the MCP servers SERVERS of its host, then the
        questions and `sandbox-ai resume` there."""
        self.update_buttons()
        key = ("resume", host, name)  # what was typed and not sent: filled in again (drafts.py)
        start = self.sai.session_start(getattr(instance, "ident", None)) if host == LOCAL and \
            hasattr(self.sai, "session_start") else None
        dialog = ResumeDialog(self, name, host, instance.state in ("running", "paused"), servers, draft=key,
                              start=start)
        title = tr("Resume {name} on {host}", name=printable(name), host=printable(host)) if host != LOCAL else \
            tr("Resume {name}", name=printable(name))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            if getattr(dialog, "to_start", False):  # On the Board page…: the whole start there, nothing resumed
                try:
                    agent, argv, arguments = dialog.start_line()
                except ValueError as exc:
                    warn(self, title, printable_text(exc))
                    return
                self.log_asked("resume with: its start shown on the Board page", instance)
                self.start_wanted.emit(agent, argv, arguments, host)
            return
        try:
            workspaces, mounts, mcp, restart = dialog.values()
            options, dropped = dialog.changed_options()
            self.log_asked("resume with", instance, f"restart {restart}, workspaces {len(workspaces)}, mounts "
                           f"{len(mounts)}, MCP {', '.join(map(printable, mcp)) or '-'}, options "
                           f"{applog.argv_text(options, 300) or '-'}, dropped {len(dropped)}")
            args = backend.resume_args(name, restart, workspaces, mounts, mcp, options, dropped)
        except ValueError as exc:
            warn(self, title, printable_text(exc))
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
        if (options or dropped) and not ask_first(self, "start-options", title, where + tr(
                "Change the options of {name}, from now on at every start: {options}? They are checked as a new "
                "start (the administrator's policy and the security mode still hold).", name=printable(name),
                options=printable(" ".join([*options, *(f"--drop-option={value}" for value in dropped)]))),
                tr("Change")):
            return

        def go():
            def done(result, error):
                if outcome(result, error)[0] == 0:
                    drafts.drop(key)  # resumed: what was typed is done with (refused: kept)
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

    def transfer(self, move=False, instance=None):
        """Copy to host… (MOVE: Move to host…): the selected session of this machine to a host of sandbox-ai hosts, in
        SessionTransferDialog (`sandbox-ai session copy|move`; a move is asked first there). The lists are read again
        after it, here and on that host."""
        instance = instance or self.current()
        if instance is None or instance.kind != "session" or not instance.session or \
                getattr(instance, "host", LOCAL) != LOCAL:
            QMessageBox.information(self, tr("Sessions"), tr("Select a session of this machine first."))
            return
        title = tr("Move to host") if move else tr("Copy to host")
        self.log_asked("move to host" if move else "copy to host", instance)
        try:
            supported = self.sai.session_transfer_supported()
        except Exception as _exc:  # noqa: BLE001 - sandbox-ai's sessions module cannot be loaded: said as not supported
            applog.swallowed("ui", "SandboxesPage.transfer: sandbox-ai's sessions module cannot be loaded: said…", _exc)
            supported = False
        if not supported:
            warn(self, title, tr("This sandbox-ai cannot copy a session to another host yet ({command}): update it.",
                                 command="sandbox-ai session copy"))
            return
        from .session_transfer import SessionTransferDialog

        def succeeded(host):
            if alive(self):
                self.refresh_soon(again=True)
                self.refresh_remote([host])
        dialog = SessionTransferDialog(self, self.sai, instance.session, move=move, owner=self, succeeded=succeeded)
        dialog.exec()
        dialog.deleteLater()  # a command still running reports to the page only (succeeded)

    def sai_state_supported(self):
        """Whether this sandbox-ai carries a session's state (duplicate, export with state, import: 0.1.193 on)."""
        try:
            return bool(self.sai.session_state_supported())
        except Exception as _exc:  # noqa: BLE001 - not known: not offered
            applog.swallowed("ui", "SandboxesPage.sai_state_supported: not known: not offered", _exc)
            return False

    def session_state(self, key, instance=None):
        """Duplicate… (KEY duplicate), Export with state… (export-state) of the selected session of this machine, or
        Import… (import): session_state_dialogs, the lists read again after it."""
        from . import session_state_dialogs as SSD

        def made():
            if alive(self):
                self.refresh_soon(again=True)
        if key == "import":
            self.log_asked("import a session", None)
            SSD.open_import(self, self.sai, owner=self, succeeded=made)
            return
        instance = instance or self.current()
        if instance is None or instance.kind != "session" or not instance.session or \
                getattr(instance, "host", LOCAL) != LOCAL:
            QMessageBox.information(self, tr("Sessions"), tr("Select a session of this machine first."))
            return
        self.log_asked("duplicate" if key == "duplicate" else "export with state", instance)
        dialog = (SSD.DuplicateDialog(self, self.sai, instance.session, owner=self, succeeded=made)
                  if key == "duplicate" else SSD.StateExportDialog(self, self.sai, instance.session, owner=self))
        dialog.exec()
        dialog.deleteLater()

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
                              "{error}\n\nResume it anyway?", name=printable(name), error=printable_text(error)[-800:]),
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
            return
        self.offer_restore(host, name, result)

    def offer_restore(self, host, name, review):
        """Not resumed after the HOME review: put back the text its changed start files had at its last start, where
        the record kept it (`sandbox-ai resume NAME --restore-home --yes`), asked first. Starts nothing."""
        paths = restorable_paths(review)
        if not paths:
            return
        title = tr("Restore the HOME of {name} on {host}", name=printable(name), host=printable(host)) \
            if host != LOCAL else tr("Restore the HOME of {name}", name=printable(name))
        text = tr("Put back the text these files had at its last start, as recorded? {list}\nFiles added since, links "
                  "and files without recorded text stay as they are. It does not start.",
                  list=printable(", ".join(paths[:REVIEW_PATHS]) + (" …" if len(paths) > REVIEW_PATHS else "")))
        if not ask_first(self, "home-restore", title, text if host == LOCAL else on_host(host, text),
                         tr("Restore HOME")):
            return
        # Exactly the files asked about: the CLI would also restore what the question did not name.
        background(self.sai.restore_home, name, host, tuple(paths), owner=self, work=True,
                   source=[self.stop_button, *self.session_buttons.values()], timeout=150,
                   done=lambda result, error: self.restored(title, result, error))

    def restored(self, title, result, error):
        if error is not None:
            warn(self, title, tr("Nothing was put back: {error}", error=printable_text(error)[-800:]))
            return
        lines = [tr("Put back: {list}", list=printable(", ".join(result.get("restored") or [])) or "-")]
        for entry in result.get("skipped") or []:
            if isinstance(entry, dict):
                lines.append(tr("Left as it is: {path} ({reason})", path=printable(entry.get("path")),
                                reason=printable(entry.get("reason"))))
        (inform if result.get("skipped") else short_notice)(self, title, "\n".join(lines[:40]))


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
                           stretch=4, compact=(0, 1, 2, 3, 5), multi=True)  # several (Ctrl, Shift): to copy them
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
        """The connection log of the start SOURCE (sandbox-ai activity's arguments) on HOST (the Sessions page's
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
        self.remote_note.setText(tr("{host}: no answer", host=self.hosts.label(host)) + f" ({printable_text(error)[:300]})"
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
            warn(self, title, printable_text(error)[:1500], modal=False)
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
        if self.remote():  # a folder on that host: chosen from its folders (remote_chooser)
            path = RC.choose_folder(self, self.hosts.host(), tr("Log folder"), "logs", sai=self.sai)
            ok = bool(path)
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
            return backend.connection_logs(directory) if directory else []
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
            index = next((i for i in range(self.file.count())  # (or compressed by gc: NAME.gz)
                          if self.file.itemText(i).endswith((wanted, wanted + ".gz"))), -1)
            if index >= 0:
                self.file.setCurrentIndex(index)
        self.file.blockSignals(False)
        self.load()

    def tick(self):
        if self.follow.isChecked() and widgets.on_screen(self):  # (not in a minimised window: read at the next tick)
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
                                     f" ({printable_text(error)[:500]})")
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
            self.detail.setPlainText(tr("Cannot read {path}: {error}", path=printable(path), error=printable_text(error)))
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
        """The chosen record in full; a body logged as a reference to one logged before ("same_as": sandbox-ai writes
        a repeated body once per part of the log) shown with that body - from the records read, else looked for in
        the log file in the background."""
        rows = selected_rows(self.table)
        record = self.record_of(rows[0]) if rows else None
        if record is None:
            return
        shown, left = backend.with_bodies(record, self.records)
        self.detail.setPlainText(json.dumps(shown, indent=2, ensure_ascii=True))
        path = self.file.currentData()
        self.detail_token = getattr(self, "detail_token", 0) + 1
        if not left or not path or self.remote():
            return
        token = self.detail_token

        def found(result, error):
            if alive(self) and token == self.detail_token and error is None and result is not None:
                self.detail.setPlainText(json.dumps(result[0], indent=2, ensure_ascii=True))
        background(lambda: backend.with_bodies(record, (), path), owner=self, timeout=60, done=found)


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
        # Another host's entry comes whole (`mcp catalog --json` there): the same fields as this machine's below. Only
        # a host older than 0.1.95 gives its line alone: then the folders are typed in - said plainly, a folder alone
        # taken for the one folder this machine's catalog names for the entry (KNOWN), if it names exactly one.
        self.known_targets = [str(m.get("target")) for m in entry.get("_known_mounts", []) if isinstance(m, dict)
                              and isinstance(m.get("target"), str)]
        if entry.get("_listed"):
            note = tr("The sandbox-ai on {host} is older than 0.1.95: it does not say which folders and variables this "
                      "entry takes. Type each folder as TARGET=FOLDER - TARGET the path inside the server's container, "
                      "FOLDER one of {host} (read-only; FOLDER:rw writable). Update {host} (Hosts › Update) to get "
                      "the fields instead.", host=printable(host))
            if self.known_targets:
                note += " " + tr("This machine's catalog names its folders: {list}.", list=", ".join(
                    f"{m.get('target')}" + (f" ({tr('required')})" if m.get("required") else "")
                    for m in entry["_known_mounts"]))
            if len(self.known_targets) == 1:
                note += " " + tr("A folder alone is taken for {target}.", target=self.known_targets[0])
            form.addRow(plain_label(note, role="notice-warn", selectable=True))
            self.mount_lines = QPlainTextEdit()
            self.mount_lines.setFixedHeight(52)
            self.mount_lines.setPlaceholderText(tr("one per line: TARGET=FOLDER or TARGET=FOLDER:rw (a folder of "
                                                   "{host})", host=printable(host)))
            form.addRow(tr("Folders"), self.mount_lines)
            self.env_line = QLineEdit()
            self.env_line.setPlaceholderText(tr("variables of the environment there, separated by spaces"))
            form.addRow(tr("Variables"), self.env_line)
        self.mcp = MF.mcp_module(getattr(parent, "sai", None))
        self.mount_reasons = {}
        # Each folder's default (sandbox-ai 0.1.212: `mcp add` needs no --mount): this machine's sandbox-ai says them,
        # another host's in its `mcp catalog --json` ("default", "mode"); an older one has none (a required folder is
        # asked for then, as before).
        self.declared = MF.folder_defaults(self.mcp, entry, host) if not entry.get("_listed") else {}
        if entry.get("mounts") and not entry.get("_listed"):
            form.addRow(explanation((tr("Each folder: the field is the folder on this machine that the server's "
                                        "container sees at the path its label names (read-only unless writable). "
                                        "Left empty, it is its default: /work the workspace of the start that uses "
                                        "the server, any other one a folder of the server's own, made by sandbox-ai.")
                                     if host == LOCAL else
                                     tr("Each folder: the field is the folder on {host} that the server's container "
                                        "sees at the path its label names (… chooses one there; read-only unless "
                                        "writable). Left empty, it is its default: /work the workspace of the start "
                                        "that uses the server, any other one a folder of the server's own, made there "
                                        "by sandbox-ai.", host=printable(host)))
                                    if self.declared else
                                    tr("Each folder: on the left the path inside the server's container, in the field "
                                       "the folder on this machine (read-only unless writable; a writable one that is "
                                       "not there yet is created).") if host == LOCAL else
                                    tr("Each folder: on the left the path inside the server's container, in the field "
                                       "a folder of {host} (… chooses one there; read-only unless writable; a writable "
                                       "one that is not there yet is created).", host=printable(host))))
        for mount in entry.get("mounts", []) if not entry.get("_listed") else []:
            field = PathField()
            field.set_host(host)
            target = mount.get("target")
            field.edit.setPlaceholderText(tr("a folder on this machine") if host == LOCAL else
                                          tr("a folder on {host}", host=printable(host)))
            writable = QCheckBox(tr("writable"))
            writable.setChecked((self.declared.get(target) or {}).get("mode") == "rw")
            row = QHBoxLayout()
            row.addWidget(field)
            row.addWidget(writable)
            holder = QWidget()
            holder.setLayout(row)
            form.addRow(MF.folder_label(target, host) + (f" ({tr('required')})" if mount.get("required") else ""),
                        holder)
            if mount.get("description"):  # what the folder is for, as its catalog entry says
                form.addRow("", plain_label(printable(str(mount["description"]))[:300], role="secondary"))
            reason = plain_label(role="notice-error")
            reason.setVisible(False)
            form.addRow("", reason)
            self.mounts[target], self.mount_reasons[target] = (field, writable), reason
            field.changed.connect(lambda t=target: self.check_mount(t))
            writable.toggled.connect(lambda _on, t=target: self.check_mount(t))
            if mount.get("required"):  # (asked for: OK waits for it)
                self.check_mount(target)
        self.show_defaults()
        self.local.textChanged.connect(lambda _text: self.show_defaults())
        # Its variables as its entry declares them (required, a secret, what for): a value that is no secret, your
        # environment or its secret, or a secret chosen - each one not required left out unless given (every one used
        # to be passed, and required: a flag like ALLOW_PRIVATE_TARGETS failed every start where it was not set).
        rows = MF.variable_rows(entry, {}) if not entry.get("_listed") else []
        self.env = MF.VariablesFields(rows, self.mcp) if rows else None
        if self.env is not None:
            self.env.changed.connect(self.update_state)
            form.addRow(tr("Variables"), self.env)
            sai = getattr(parent, "sai", None)
            if sai is not None:
                background(lambda: [name for name, _where in sai.host_secrets(host)[1]], timeout=60,
                           done=lambda names, failed: alive(self.env) and failed is None and
                           self.env.set_secrets(names))
        # Where it reaches: as its entry says (an entry that declares its hosts: only them), or what you choose.
        wanted = entry.get("network", "none")
        hosts = [str(h) for h in entry.get("hosts", []) if isinstance(h, str)] if wanted == "allowlist" else []
        self.network = MF.network_choice(wanted, hosts)
        form.addRow(tr("Network") + (" " + tr("(needs {hosts})", hosts=", ".join(hosts)) if hosts else ""),
                    self.network)
        form.addRow("", MF.lan_warning(self.network))
        # Empty: what the catalog entry recommends, else the defaults (shown in the fields as such).
        recommended = {}
        if self.mcp is not None and isinstance(entry.get("limits"), dict):
            with contextlib.suppress(Exception):
                recommended = self.mcp.recommended_limits(entry)
        defaults = None
        if self.mcp is not None and host == LOCAL and hasattr(self.mcp, "default_limits"):
            with contextlib.suppress(Exception):
                defaults = self.mcp.default_limits({"image": "", **recommended})
        self.limits = MF.LimitsFields(MF.limit_checks(self.mcp), defaults,
                                      default_text=tr("the entry's, else the default"))
        self.limits.changed.connect(self.update_state)
        form.addRow(tr("Limits") + (" " + tr("(its catalog recommends {limits})", limits=", ".join(
            f"{key} {value}" for key, value in recommended.items())) if recommended else ""), self.limits)
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
        if signature == "bad":
            set_enabled(self.unsigned, False, tr("Its catalog's signature does not verify: sandbox-ai refuses it "
                                                 "anyway."))
        if signature != "good":
            form.addRow(plain_label(tr("Its catalog is not signed by a trusted key (sandbox/mcp-signers): sandbox-ai "
                                       "adds it only with --unsigned.") if signature != "bad" else
                                    tr("Its catalog's signature does not verify: sandbox-ai refuses it."),
                                    role="notice-warn"))
        form.addRow(self.unsigned)
        self.unsigned.setVisible(signature != "good")  # (in the form first: else a window of its own for a moment)
        if catalog:
            form.addRow(tr("Catalog"), plain_label(printable(catalog)))
        self.error = plain_label(role="notice-error", selectable=True)
        self.error.setVisible(False)
        form.addRow(self.error)
        buttons = self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.prefix_name = None
        self.update_state()

    def show_defaults(self):
        """Each empty folder field shows its default (the start's workspace, the folder of the server's own under the
        name for --mcp), as sandbox-ai makes it where it is added."""
        name = self.local.text().strip() or str(self.entry.get("name") or "")
        for target, (field, _writable) in self.mounts.items():
            shown = MF.default_text(self.mcp, self.declared.get(target), name, target, self.host)
            if shown:
                field.edit.setPlaceholderText(shown)

    def check_mount(self, target):
        """The folder of TARGET checked at once (this machine's: mcp.mount_host's rules; another host's at add, there);
        a folder the entry requires and has no default for is asked for - none is filled in for it (never one of this
        machine on a host)."""
        field, writable = self.mounts[target]
        value = field.value().strip()
        required = any(m.get("target") == target and m.get("required") for m in self.entry.get("mounts", [])
                       if isinstance(m, dict)) and target not in self.declared
        if not value:
            reason = (tr("{target} needs a folder of this machine.", target=target) if self.host == LOCAL else
                      tr("{target} needs a folder of {host} (… chooses one there).", target=target,
                         host=printable(self.host))) if required else None
        elif self.host != LOCAL and not (value.startswith("/") or value == "~" or value.startswith("~/")):
            reason = tr("A folder of {host} is an absolute path (/… or ~/…).", host=printable(self.host))
        else:
            reason = MF.mount_reason(self.mcp, target, value, "rw" if writable.isChecked() else "ro") \
                if self.host == LOCAL else None
        self.mount_reasons[target].setText(reason or "")
        self.mount_reasons[target].setVisible(bool(reason))
        MF.mark(field.edit, reason)
        self.update_state()

    def problems(self):
        """Why OK takes nothing yet: a limit or a folder that is not valid."""
        return list(self.limits.problems().values()) + [r.text() for r in self.mount_reasons.values() if r.text()] \
            + (list(self.env.problems().values()) if getattr(self, "env", None) is not None else [])

    def update_state(self):
        if hasattr(self, "buttons"):
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not self.problems())

    def accept(self):
        """OK only with valid values: what is wrong is said here, and the dialog stays open to correct it."""
        problems = self.problems()
        if problems:
            self.error.setText("\n".join(problems))
            self.error.setVisible(True)
            self.limits.focus_first_problem()
            return
        super().accept()

    def mount_values(self):
        """[(target, host folder, "ro"|"rw")]. ValueError: a line that is not TARGET=HOST_FOLDER[:ro|rw]."""
        found = [(target, field.value(), "rw" if writable.isChecked() else "ro")
                 for target, (field, writable) in self.mounts.items() if field.value()]
        # An empty field is its default: named (TARGET=default) only where its mode is not the declared one.
        found += [(target, backend.MCP_DEFAULT_FOLDER, "rw" if writable.isChecked() else "ro")
                  for target, (field, writable) in self.mounts.items() if not field.value() and target in self.declared
                  and ("rw" if writable.isChecked() else "ro") != self.declared[target].get("mode", "ro")]
        for line in (self.mount_lines.toPlainText().splitlines() if self.mount_lines is not None else []):
            line = line.strip()
            if not line:
                continue
            target, equals, rest = line.partition("=")
            if not equals and line.startswith("/") and len(self.known_targets) == 1:
                target, rest = self.known_targets[0], line  # a folder alone: the entry's one folder
            elif not equals:
                raise ValueError(tr("{line}: which folder inside the server's container is it? Give it as "
                                    "TARGET=FOLDER, such as /work={line}.", line=printable(line)))
            source, _, mode = rest.partition(":")
            if mode not in ("", "ro", "rw") or not source.startswith("/") or not target.startswith("/"):
                raise ValueError(tr("{line}: TARGET and FOLDER are absolute paths, and after FOLDER only :ro or :rw "
                                    "may follow.", line=printable(line)))
            found.append((target, source, mode or "ro"))
        return found

    def values(self, name):
        """The keywords of backend.mcp_add_args for the catalog entry NAME (a limit left empty: none given, so the
        entry's recommended one or the default)."""
        limits = self.limits.values()
        env = (self.env.specs() if self.env is not None else []) + \
            (self.env_line.text().split() if self.env_line is not None else [])
        return {"entry": name, "name_as": self.local.text().strip() or name.split(":")[-1],
                "mounts": self.mount_values(), "env": env, "network": self.network.currentData(),
                "memory": limits.get("memory"), "cpus": limits.get("cpus"), "pids": limits.get("pids"),
                "tmp": limits.get("tmp"),
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
        form.addRow(explanation(tr("A running sandbox cannot take a new MCP server while it runs: its agent reads its "
                                   "servers when it starts, and the server's socket is mounted at the start. The "
                                   "server is kept in the start's options: without a restart it is there from its "
                                   "next start on.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.start.currentData(), self.restart.isChecked()


def table_menu(view, entries):
    """A context menu on the table VIEW: ENTRIES() (widgets.list_menu), then Copy and Copy cell."""
    widgets.list_menu(view, entries)


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
        self.follow_note = follow_note()  # whose servers, when the window's pages follow the Sessions page's host
        layout.addWidget(flow(self.host_choice, self.follow_note, self.host_note))
        servers = QGroupBox(tr("Your MCP servers (use them with --mcp NAME)"))
        servers_layout = QVBoxLayout(servers)
        self.servers = table([tr("Name"), tr("Status"), tr("Serves"), tr("Runs"), tr("Catalog"), tr("Check")], stretch=2,
                             compact=(1, 5))
        self.servers.setAccessibleName(tr("MCP servers"))
        widgets.multi_select(self.servers)  # several at once (Ctrl, Shift): Stop takes them all
        servers_layout.addWidget(self.servers)
        self.server_buttons = []
        for action, text in (("start", tr("Start…")), ("stop", tr("Stop"))):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action: self.server(a))
            self.server_buttons.append(button)
        self.server_buttons[0].setToolTip(tr("Start the server sandbox: its backend, a VPN in front, or for an agent "
                                             "start (sandbox-ai mcp start)"))
        self.attach_button = QPushButton(tr("Attach to…"))
        self.attach_button.setToolTip(tr("Connect the server to an agent start (sandbox-ai mcp attach)"))
        self.attach_button.clicked.connect(self.attach)
        self.attach_button.setEnabled(sai.mcp_attach_supported())
        self.server_buttons.append(self.attach_button)
        self.properties_button = QPushButton(tr("Properties…"))
        self.properties_button.setToolTip(tr("Its limits for every start, network, VPN, folders and variables "
                                             "(sandbox-ai mcp set); also a double click or Alt+Enter"))
        self.properties_button.clicked.connect(lambda _=False: self.properties())
        self.server_buttons.append(self.properties_button)
        self.copy_button = QPushButton(tr("From another host…"))
        self.copy_button.setToolTip(tr("Copy a server of another host here (sandbox-ai mcp copy): a catalog server is "
                                       "added from its catalog here, a server of your own checked as mcp.json is; "
                                       "what it takes is shown before anything is copied"))
        self.copy_button.clicked.connect(lambda _=False: self.copy_from_host())
        # The server selected to another host (reported live: "es sollte, wenn man einen MCP markiert hat, dann heißen:
        # zu einem anderen host"): its text names it; greyed out, saying why, without one selected.
        self.copy_to_button = QPushButton(tr("Copy to another host…"))
        self.copy_to_button.setToolTip(tr("Copy the server selected to another host (sandbox-ai mcp copy): checked "
                                          "there first, what it takes is shown before anything is copied"))
        self.copy_to_button.clicked.connect(lambda _=False: self.copy_to_host())
        # Update: the newer image the catalog of each server selected pins (sandbox-ai mcp update NAME...); Update all:
        # every server of yours from a catalog (the user, 2026-11-23: "add mcp update").
        self.update_button = QPushButton(tr("Update"))
        self.update_button.setToolTip(tr("Take the newer image the catalog of each server selected pins (sandbox-ai "
                                         "mcp update): the catalog read again and its signature checked; its folders, "
                                         "variables, limits and network stay"))
        self.update_button.clicked.connect(lambda _=False: self.update_servers())
        self.update_all_button = QPushButton(tr("Update all"))
        self.update_all_button.setToolTip(tr("Every server of yours from a catalog takes the newer image its catalog "
                                             "pins, where there is one (sandbox-ai mcp update)"))
        self.update_all_button.clicked.connect(lambda _=False: self.update_servers(every=True))
        self.server_buttons += [self.update_button, self.update_all_button]
        # The catalog's Burp in host mode: the extension jar your own Burp loads (sandbox-ai mcp burp extension)
        self.burp_button = QPushButton(tr("Burp extension…"))
        self.burp_button.setToolTip(tr("The Burp server selected: write the extension jar of its signed image for your "
                                       "own Burp and show its path and SHA-256 to copy (sandbox-ai mcp burp "
                                       "extension); in Burp: Extensions, Add, Java, that file, then its MCP tab"))
        self.burp_button.clicked.connect(lambda _=False: self.burp_extension())
        self.burp_button.setVisible(False)  # shown once the list has a Burp server (servers_read)
        # Check / Check all: each server started as a start starts it, asked for its tools, stopped again (sandbox-ai
        # mcp check): its result in the Check column - ok, or why and the fix (the user, 2026-11-25: "Bitte auch
        # prüfen, ob wir mit anderen MCP-Servern solche Probleme haben").
        self.check_button = QPushButton(tr("Check"))
        self.check_button.setToolTip(tr("Start each server selected as a start would, ask it for its tools and stop it "
                                        "again (sandbox-ai mcp check): ok, or why it failed and the fix"))
        self.check_button.clicked.connect(lambda _=False: self.check_servers())
        self.check_all_button = QPushButton(tr("Check all"))
        self.check_all_button.setToolTip(tr("Check every server of the host shown (sandbox-ai mcp check --all): one "
                                            "after the other, a few seconds each"))
        self.check_all_button.clicked.connect(lambda _=False: self.check_servers(every=True))
        self.server_buttons += [self.check_button, self.check_all_button]
        self.notes, self.checks = {}, {}
        # Start…, Attach to…, Properties… and Burp extension…: one server
        widgets.one_only(self.servers, [self.server_buttons[0], self.attach_button, self.properties_button,
                                        self.burp_button])
        self.servers.itemSelectionChanged.connect(self.update_copy_to)
        self.servers.doubleClicked.connect(lambda _index: self.properties())
        for keys in ("Alt+Return", "Alt+Enter"):
            shortcut = QShortcut(QKeySequence(keys), self.servers)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda: self.properties())
        table_menu(self.servers, lambda: [*self.server_buttons, *([self.burp_button] if self.burp_selected() else []),
                                          None, self.copy_to_button, self.copy_button])
        # One row that wraps where the window is narrow: the copy buttons' texts are long in German, and Copy NAME to
        # another host… grows with the name selected (the page needed 996 px, more than a small laptop's 900)
        servers_layout.addWidget(flow(*self.server_buttons, self.burp_button, self.copy_to_button, self.copy_button))
        layout.addWidget(servers)
        catalog = QGroupBox(tr("Catalogs: sandbox-ai-mcp (mcp:) and axross-mcp-hub (hub:)"))
        catalog_layout = QVBoxLayout(catalog)
        self.catalog = table([tr("Entry"), tr("Network"), tr("Title"), tr("Published"), tr("Signature"),
                              tr("Catalog")], stretch=2, compact=(1, 3, 5))
        self.catalog.setAccessibleName(tr("Catalog"))
        catalog_layout.addWidget(self.catalog)
        # An entry: Add… by a double click or Enter, and a menu with Add… and Details…
        self.catalog.doubleClicked.connect(lambda _index: self.add())
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.catalog)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda: self.add())
        self.details_button = QPushButton(tr("Details…"))
        self.details_button.setToolTip(tr("What the entry is, needs and recommends (its notes, network, folders, "
                                          "variables, limits)"))
        self.details_button.clicked.connect(lambda _=False: self.details())
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
        row.addWidget(self.details_button)
        row.addWidget(add)
        catalog_layout.addLayout(row)
        table_menu(self.catalog, lambda: [self.add_button, self.details_button, None, self.load_button])
        # The page's message area: what loading the catalogs and adding said (a refusal of sandbox-ai with a URL in
        # it too), wrapped in the page, selectable, never widening the window.
        self.catalog_status = MessageText()
        self.catalog_status.setAccessibleName(tr("Message"))
        catalog_layout.addWidget(self.catalog_status)
        layout.addWidget(catalog)
        self.entries, self.catalog_requested, self.catalog_of, self.shown_before = {}, False, None, False
        # When another host was chosen while the page was hidden (time.monotonic; the window gives a hidden page the
        # Sessions page's host as it is about to be shown): its servers were asked for then, not again by that show.
        self.host_read_at = None
        self.update_copy_to()
        self.refresh()

    def update_copy_to(self):
        """Copy NAME to another host…: the one server selected named; greyed out (saying why) without one, with
        several, or while its copy runs."""
        button = self.copy_to_button
        if widgets.held(button):
            return
        rows = widgets.selected_rows(self.servers)
        name = selected(self.servers) if len(rows) == 1 else None
        name = name if name and re.fullmatch(backend.MCP_NAME, name) else None
        button.setText(tr("Copy {name} to another host…", name=printable(name)) if name else
                       tr("Copy to another host…"))
        widgets.set_enabled(button, name is not None, tr("Acts on one row: {count} are selected.", count=len(rows))
                            if len(rows) > 1 else tr("Select a server first: the one selected is copied to the "
                                                     "host chosen then."))

    def showEvent(self, event):  # noqa: N802
        """The catalogs load by themselves the first time the page is shown (in the background); the hosts are read
        again (one added on the Hosts page meanwhile), and so are the servers each time it is shown again (one added
        with sandbox-ai mcp add meanwhile)."""
        self.host_choice.reload()
        super().showEvent(event)
        just_read = self.host_read_at is not None and time.monotonic() - self.host_read_at < 2
        self.host_read_at = None
        if self.shown_before and not just_read:
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
        self.entries, self.checks = {}, {}
        self.host_read_at = None if self.isVisible() else time.monotonic()
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
        self.server_rows = list(rows)
        checked = [self.check_cell(r[0]) for r in rows]
        fill(self.servers, [(*r, cell[0]) for r, cell in zip(rows, checked)],
             ["ok" if r[1] == "running" else None for r in rows], tooltips=[cell[1] for cell in checked])
        self.burp_button.setVisible(any(self.is_burp_row(row) for row in self.server_rows))
        if error is None:  # then what ended a server's processes, and limits below its catalog's (in the background)
            host = self.host()
            background(self.sai.host_mcp_notes, host, timeout=90,
                       done=lambda notes, failed: alive(self) and host == self.host() and failed is None
                       and self.notes_read(notes))

    def notes_read(self, notes):
        """The status of each server with what ended or failed its processes last (its tooltip: why and the fix) and
        whether its limits are below what its catalog recommends (Properties… applies them)."""
        if not notes:
            return
        self.notes = notes if isinstance(notes, dict) else {}
        rows, colors, tooltips = [], [], []
        for row in getattr(self, "server_rows", []):
            note = notes.get(row[0]) if isinstance(notes, dict) else None
            status, tips = row[1], []
            if note and note.get("problem"):
                problem = note["problem"]
                status = tr("{status}, a problem at {time}", status=row[1], time=printable(problem.get("time") or ""))
                tips.append(printable(f"{problem.get('what')}: {problem.get('why')}. ")
                            + tr("Fix: {fix}", fix=printable_text(problem.get("fix") or ""))
                            + (" " + tr("Its log: {path}", path=printable(problem["log"])) if problem.get("log") else ""))
            if note and note.get("below"):
                status += " · " + tr("below the catalog's limits")
                tips.append(tr("Below the limits its catalog recommends: {limits}. Properties… applies them (sandbox-ai "
                               "mcp set {name} --recommended).", limits=printable(", ".join(
                                   f"{k} {v}" for k, v in note["below"].items())), name=printable(row[0])))
            newer = note.get("newer") if note else None
            if newer:
                status += " · " + tr("a newer image")
                tips.append(tr("Its {signed} catalog pins a newer image: {change}. Update takes it (sandbox-ai mcp "
                               "update {name}); its folders, variables, limits and network stay.",
                               signed=tr("catalog|signed") if newer.get("signed") else tr("catalog|unsigned"),
                               change=", ".join(f"{key} {backend.short_digest(pair[0])} → "
                                                f"{backend.short_digest(pair[1])}"
                                                for key, pair in newer.items() if key in ("image", "rootfs")),
                               name=printable(row[0])))
            cell, tip = self.check_cell(row[0])
            if tip:
                tips.append(tip)
            rows.append((row[0], status, *row[2:5], cell))
            colors.append("bad" if note and note.get("problem") else "warn" if note and (note.get("below") or newer)
                          else "ok" if row[1] == "running" else None)
            tooltips.append("\n".join(tips) or None)
        fill(self.servers, rows, colors, tooltips=tooltips)

    @staticmethod
    def is_burp_row(row):
        """Whether a row of the list is a Burp server (its Runs column: backend.burp_text, in either language)."""
        return len(row) > 3 and str(row[3]).startswith("Burp Suite")

    def burp_selected(self):
        name = selected(self.servers)
        return any(row[0] == name and self.is_burp_row(row) for row in getattr(self, "server_rows", []))

    def burp_extension(self):
        """Burp extension…: the extension jar of the Burp server selected, written on the host shown (sandbox-ai mcp
        burp extension) - its path and SHA-256 in a window to copy from, with how your Burp loads it."""
        name = selected(self.servers)
        if not name or not re.fullmatch(backend.MCP_NAME, name) or not self.burp_selected():
            inform(self, tr("Burp extension"), tr("Select the Burp server first (sandbox-ai mcp add burp)."))
            return
        host = self.host()
        background(self.sai.host_burp_extension, host, name, source=self.burp_button, owner=self, timeout=600,
                   work=True, done=lambda found, error: alive(self) and self.burp_extension_shown(found, error))

    def burp_extension_shown(self, found, error):
        if error is not None:
            inform(self, tr("Burp extension"), str(error), warning=True)
            return
        widgets.show_text_window(self, tr("Burp extension"), tr(
            "{path}\n\nSHA-256 {sha256}\nFrom {image}\n\nIn Burp: Extensions, Add, Extension type Java, this file; "
            "then its MCP tab: Enabled, Server host 127.0.0.1.", path=printable(str(found.get("path") or "")),
            sha256=printable(str(found.get("sha256") or "")), image=printable(str(found.get("image") or ""))))

    def check_cell(self, name):
        """(the Check column's text, its tooltip or None) of server NAME: what the last check of it here said."""
        row = self.checks.get(name)
        if not row:
            return "", None
        if row["result"] == "ok":
            return tr("ok, {count} tools, {seconds} s", count=row.get("tools") or 0,
                      seconds=f"{row.get('seconds') or 0:g}"), row.get("note")
        if row["result"] == "skipped":
            return tr("skipped"), row.get("reason")
        tip = tr("Why: {reason}", reason=row.get("reason") or "") + "\n" + tr("Fix: {fix}", fix=row.get("fix") or "")
        return tr("failed: {reason}", reason=printable(row.get("reason") or "")[:80]), tip

    def check_servers(self, every=False):
        """Check (the servers selected) or Check all (EVERY): `sandbox-ai mcp check` on the host shown, in the
        background - each server started as a start starts it, asked for its tools, stopped again; its result in the
        Check column (the reason and the fix in its tooltip). Nothing is changed, so nothing is asked first."""
        names = [] if every else [name for name in widgets.selected_keys(self.servers)
                                  if name and re.fullmatch(backend.MCP_NAME, name)]
        title = tr("Check all") if every else tr("Check")
        if not every and not names:
            inform(self, title, tr("Select a server first."))
            return
        host = self.host()
        applog.log("INFO", "mcp", f"mcp check: {', '.join(names) or 'all'} on {printable(host)}")
        self.catalog_status.setText(tr("Checking {names}…", names=", ".join(printable(n) for n in names)
                                       if names else tr("every server")))
        background(lambda: self.sai.host_mcp_check(host, names), owner=self, work=True, source=self.server_buttons,
                   timeout=3700, done=lambda rows, error: alive(self) and self.checked(host, title, rows, error))

    def checked(self, host, title, rows, error):
        if error is not None:
            self.catalog_status.setText(f"{title}: {printable_text(error)[:1500]}")
            return
        if host != self.host():
            return
        self.checks.update({row["server"]: row for row in rows or []})
        failed = [row["server"] for row in rows or [] if row["result"] == "failed"]
        ok = sum(1 for row in rows or [] if row["result"] == "ok")
        self.catalog_status.setText(tr("{ok} of {count} ok", ok=ok, count=len(rows or [])) + (
            "; " + tr("failed: {names}", names=", ".join(printable(name) for name in failed)) if failed else ""))
        self.refresh()

    def update_servers(self, every=False):
        """Update (the servers selected) or Update all (EVERY: each server of yours from a catalog): asked first
        (mcp-update), then `sandbox-ai mcp update [NAME...] --yes` on the host shown - the catalogs read again there,
        each signature checked; a server of an unsigned catalog only when the human was told so here (--unsigned)."""
        names = [] if every else [name for name in widgets.selected_keys(self.servers)
                                  if name and re.fullmatch(backend.MCP_NAME, name)]
        title = tr("Update all") if every else tr("Update")
        if not every and not names:
            inform(self, title, tr("Select a server first."))
            return
        host = self.host()
        unsigned = [name for name in names if ((self.notes.get(name) or {}).get("newer") or {}).get("signed") is False]
        text = (tr("Update every MCP server of yours whose catalog pins a newer image? Each catalog is read again and "
                   "its signature checked; each new image is pulled by its digest. Folders, variables, limits and "
                   "network stay; a running one runs on with its old image until it is stopped.") if every else
                several(tr("Update these {count} MCP servers to the newer image their catalog pins? Each catalog is "
                           "read again and its signature checked. Folders, variables, limits and network stay; a "
                           "running one runs on with its old image until it is stopped.", count=len(names)),
                        [printable(name) for name in names]))
        if unsigned:
            text += " " + tr("The catalog of {names} is not signed: nobody vouched for its new image.",
                             names=", ".join(printable(name) for name in unsigned))
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "mcp-update", title, text, tr("Update")):
            return
        applog.log("INFO", "mcp", f"mcp update: {', '.join(names) or 'all'} on {printable(host)} asked")
        self.catalog_status.setText(tr("Updating {names}…", names=", ".join(printable(n) for n in names)
                                       if names else tr("every server of yours")))
        background(lambda: self.sai.host_mcp_update(host, names, unsigned=bool(unsigned)), owner=self, work=True,
                   source=self.server_buttons, timeout=3700,
                   done=lambda result, error: self.updated(title, result, error))

    def updated(self, title, result, error):
        """What `mcp update` said: the servers updated, those that were not (why), or its refusal."""
        self.refresh()
        if error is not None:
            self.catalog_status.setText(tr("Not updated: {reason}", reason=printable_text(str(error))[:1500]))
            warn(self, title, tr("Not updated: {reason}", reason=printable_text(str(error))[:1500]), modal=False)
            return
        updated = [printable(str(name)) for name in result.get("updated") or []]
        others = [f"{printable(str(row.get('server')))}: {printable_text(str(row.get('why') or ''))[:300]}"
                  for row in result.get("servers") or [] if isinstance(row, dict) and row.get("state") == "refused"]
        text = tr("Updated: {names}.", names=", ".join(updated)) if updated else \
            tr("Nothing to update: each runs the image its catalog pins.")
        if others:
            text += "\n" + tr("Not updated: {reason}", reason="; ".join(others))
        self.catalog_status.setText(text)
        short_notice(self, title, text)

    def properties(self, name=None, host=None, values=None):
        """Properties… of the server NAME of HOST, else the one selected (double click, Alt+Enter, its menu): what `mcp
        list --json` says of it read first (in the background), then the dialog (ServerPropertiesDialog), which runs
        `mcp set` itself. VALUES ({limits, variables}: a board's own, "Save as the server's defaults…") are filled in,
        for OK to make them the server's defaults."""
        name = name or selected(self.servers)
        if not name:
            inform(self, tr("Properties…").rstrip("…"), tr("Select a server first."))
            return
        host = host or self.host()
        background(self.sai.host_mcp_list, host, owner=self, source=self.server_buttons, timeout=90,
                   done=lambda servers, error: self.properties_read(host, name, servers, error, values))

    def properties_read(self, host, name, servers, error, values=None):
        title = tr("Properties of {name}", name=printable(name))
        if error is not None:
            warn(self, title, printable_text(str(error)))
            return
        info = next((entry for entry in servers or [] if entry.get("name") == name), None)
        if info is None:
            warn(self, title, tr("{name} is no longer defined (sandbox-ai mcp list).", name=printable(name)))
            return
        if "kind" not in info:
            warn(self, title, tr("The sandbox-ai of {host} cannot change a server's properties yet: update it there.",
                                 host=printable(host)))
            return
        dialog = MF.ServerPropertiesDialog(self, self.sai, info, self.catalog_entry(host, info), host,
                                           MF.run_submit(self.sai, host, owner=self), values=values)
        # The secrets' names for the variables' choosers (never their values), read meanwhile.
        background(lambda: [name for name, _where in self.sai.host_secrets(host)[1]], timeout=60,
                   done=lambda names, failed: alive(dialog) and failed is None and dialog.env.set_secrets(names))
        if widgets.exec_dropped(dialog) == QDialog.DialogCode.Accepted and getattr(dialog, "result_text", ""):
            short_notice(self, title, dialog.result_text)
        self.refresh()

    def catalog_entry(self, host, info):
        """The loaded catalog entry server INFO was added from (its definition's "source"), for the folders it takes;
        None when the catalogs of HOST are not loaded (or only listed: another host's lines name no folders)."""
        source = str((info.get("definition") or {}).get("source") or "")
        if not source or (self.catalog_of or (None, ""))[0] != host:
            return None
        for key, entry in self.entries.items():
            if not entry.get("_listed") and (entry.get("id") == source or key == source):
                return entry
        return None

    def details(self):
        """Details… of the catalog entry selected: its description, notes, network, folders, variables and the
        limits it recommends."""
        key = selected(self.catalog)
        entry = self.entries.get(key or "")
        if not entry:
            inform(self, tr("Details…").rstrip("…"), tr("Load the catalogs and select an entry first."))
            return
        lines = [str(entry.get("title") or key), "", str(entry.get("description") or "")]
        if entry.get("notes"):
            lines += ["", tr("Notes: {text}", text=entry["notes"])]
        network = str(backend.mcp_network_word(entry.get("network", "none"))) + (
            f" ({', '.join(map(str, entry.get('hosts', [])))})"
                                                       if entry.get("hosts") else "")
        lines += ["", tr("Network: {value}", value=network)]
        folders = [str(m.get("target")) + (f" ({tr('required')})" if m.get("required") else "")
                   for m in entry.get("mounts", []) if isinstance(m, dict)]
        if folders:
            lines.append(tr("Folders in the container: {list}", list=", ".join(folders)))
        variables = [str(v.get("name")) for v in entry.get("env", []) if isinstance(v, dict)]
        if variables:
            lines.append(tr("Variables: {list}", list=", ".join(variables)))
        if isinstance(entry.get("limits"), dict) and entry["limits"]:
            lines.append(tr("Recommended limits: {list}", list=", ".join(f"{k} {v}" for k, v in entry["limits"].items())))
        if entry.get("page"):
            lines.append(str(entry["page"]))
        widgets.show_text_window(self, printable(key), printable_text("\n".join(lines)))

    def copy_from_host(self):
        """From another host…: a server of another host copied to the host shown (checked first in the dialog, asked
        first here: mcp-copy, and mcp-replace where one of that name is replaced)."""
        host = self.host()
        dialog = McpCopyDialog(self, self.sai, host)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        source, name, choices = dialog.values()
        title = tr("MCP server from another host to {host}", host=printable(host) if host != LOCAL else
                   tr("this machine"))
        if not ask_first(self, "mcp-copy", title, copy_question(source, name, host, None, choices), tr("Copy")):
            return
        if choices.get("replace") and not ask_first(self, "mcp-replace", title, tr(
                "An MCP server {name} is there already: replace its definition for every start that uses it?",
                name=printable(choices.get("as_name") or name)), tr("Replace")):
            return
        self.catalog_status.setText(tr("Copying {name} from {source}…", name=printable(name),
                                       source=printable(source) if source != LOCAL else tr("this machine")))
        background(lambda: self.sai.host_mcp_copy(source, host, [name], dry_run=False, **choices), owner=self,
                   work=True, source=self.copy_button, timeout=3700,
                   done=lambda result, error: self.copied(title, name, result, error))

    def copy_to_host(self):
        """Copy NAME to another host…: the server selected copied from the host shown to a host chosen in the dialog
        (checked there first, asked first here: mcp-copy, and mcp-replace where one of that name is replaced)."""
        name = selected(self.servers)
        if not name or not re.fullmatch(backend.MCP_NAME, name):
            inform(self, tr("Copy to another host…").rstrip("…"), tr("Select a server first."))
            return
        source = self.host()
        dialog = McpCopyDialog(self, self.sai, server=(source, name))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        source, name, choices = dialog.values()
        target = dialog.target
        title = tr("Copy {name} to another host", name=printable(name))
        if not ask_first(self, "mcp-copy", title, copy_question(source, name, target, None, choices), tr("Copy")):
            return
        if choices.get("replace") and not ask_first(self, "mcp-replace", title, tr(
                "An MCP server {name} is there already: replace its definition for every start that uses it?",
                name=printable(choices.get("as_name") or name)), tr("Replace")):
            return
        there = printable(target) if target != LOCAL else tr("this machine")
        self.catalog_status.setText(tr("Copying {name} to {host}…", name=printable(name), host=there))
        background(lambda: self.sai.host_mcp_copy(source, target, [name], dry_run=False, **choices), owner=self,
                   work=True, source=self.copy_to_button, timeout=3700,
                   done=lambda result, error: (self.copied(title, name, result, error, there), self.update_copy_to()))

    def copied(self, title, name, result, error, there=None):
        """What the copy said: here (THERE None: From another host…) or to the host THERE (Copy NAME to another
        host…)."""
        self.refresh()
        if error is not None or blocked_copy(result):
            from .mcp_copy_view import plan_lines
            reason = printable_text(error)[:1500] if error is not None else "\n".join(plan_lines(result))[:3000]
            self.catalog_status.setText(tr("{name} was not copied.", name=printable(name)) + "\n" + reason)
            warn(self, title, tr("Not copied: {reason}", reason=reason), modal=False)
            return
        servers = ", ".join(printable(str(plan.get("server"))) for plan in result.get("servers") or []
                            if isinstance(plan, dict))
        if there is not None:
            self.catalog_status.setText(tr("{name} copied to {host}: use it there with --mcp {there}.",
                                           name=printable(name), host=there, there=servers or printable(name)))
            return
        self.catalog_status.setText(tr("{name} copied: it is in the list above; use it with --mcp {local}.",
                                       name=printable(name), local=servers or printable(name)))

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
            warn(self, tr("Attach to…"), printable_text(str(error)))
            return
        if not targets:
            inform(self, tr("Attach to…"), tr("No kept agent start to attach it to: start one (a run "
                                              "without a kept start takes no new server)."))
            return
        dialog = AttachServerDialog(self, name, targets)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        start, restart = dialog.values()
        applog.log("INFO", "mcp", f"Attach to…: the MCP server {printable(name)} on {printable(host)} for "
                   f"{printable(start)}" + (" (restarting it now)" if restart else ""))
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
        applog.log("INFO" if error is None else "WARNING", "mcp", f"Attach to…: {printable(name)} " + (
            "attached" if error is None else f"not attached: {applog.error_text(error)}"))
        if error is not None:
            warn(self, tr("Attach to…"), printable_text(str(error)))
        else:
            short_notice(self, tr("Attach to…"), printable(result.get("message") or ""))
        self.refresh()

    def server(self, action):
        names = [name for name in widgets.selected_keys(self.servers) if name]
        if action == "stop" and len(names) > 1:
            self.stop_servers(names)
            return
        name = selected(self.servers)
        if not name:
            applog.log("DEBUG", "mcp", f"mcp {action}: no server selected")
            return
        host = self.host()
        applog.log("INFO", "mcp", f"mcp {action}: {printable(name)} on {printable(host)} asked")
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

    def stop_servers(self, names):
        """Stop with several servers selected: one question naming them, then each (mcp stop NAME)."""
        host = self.host()
        text = several(tr("Stop these {count} MCP servers? Agents that use one lose it until it is started again.",
                          count=len(names)), [printable(name) for name in names])
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "start-stop", tr("Stop"), text, tr("Stop")):
            return

        def done(outcomes):
            report_each(self, tr("Stop"), outcomes, printable)
            self.server_done("stop", (0, "", ""), None)
        run_each(lambda name: self.sai.host_run(host, "mcp", "stop", name, timeout=300), names, owner=self,
                 source=self.server_buttons, timeout=330 * len(names), done=done)

    def start_server(self, host, name, targets):
        """`sandbox-ai mcp start` with the dialog's options; for a start it is attached to it too (asked first:
        mcp-attach, and start-restart where that start is restarted now)."""
        dialog = McpStartDialog(self, name, targets, host)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            args = backend.mcp_start_args(name, **values)
        except ValueError as exc:
            warn(self, tr("Start {name}", name=printable(name)), printable_text(exc))
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
        applog.log("INFO" if status == 0 else "WARNING", "mcp", f"mcp {action}: exit status {status}")
        if status != 0:
            warn(self, f"mcp {action}", (out + err).strip())
        elif not quiet:  # for a start: what it says (JSON: its message)
            try:
                said = json.loads(out).get("message") or out
            except (ValueError, AttributeError, RecursionError):
                said = out
            short_notice(self, f"mcp {action}", printable_text(str(said)).strip())
        self.refresh()

    def load_catalog(self, auto=False):
        """The catalogs come over the network: fetched in a thread (this machine's catalog module, or `sandbox-ai mcp
        catalog [--catalog SOURCE]` on the host chosen)."""
        self.catalog_requested = True
        host, source = self.host(), self.catalog_source.text().strip()
        try:
            backend.catalog_options([source] if source else [])
        except ValueError as exc:
            self.catalog_status.setText(printable_text(exc))
            return
        self.catalog_status.setText(tr("Loading the catalogs…"))
        background(self.sai.host_mcp_catalog, host, [source] if source else [], owner=self, source=self.load_button,
                   timeout=180, done=lambda result, error: self.catalog_loaded(result, error, auto, (host, source)))

    def catalog_loaded(self, result, error, auto=False, of=(LOCAL, "")):
        entries, problems = result if error is None else ([], [str(error)])
        self.catalog_of = of
        self.catalog_status.setText(tr("{count} entries", count=len(entries)) if not problems else
                                    "\n".join(printable_text(problem) for problem in problems))
        if auto:
            problems = []  # loaded by itself: said on the page, not in a dialog
        self.entries = {f"{prefix}:{e.get('name')}": e for prefix, e in entries}
        rows = sorted(self.entries.items())
        signatures = [(e.get("_signature") or {}).get("state", "unsigned") for _key, e in rows]
        fill(self.catalog, [[key, str(backend.mcp_network_word(e.get("network", ""))) + (
                                 " " + ",".join(e.get("hosts", [])) if e.get("hosts") else ""),
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
            warn(self, tr("Catalogs"), "\n".join(printable_text(problem) for problem in problems))

    def add(self):
        key = selected(self.catalog)
        entry = self.entries.get(key or "")
        if not entry:
            QMessageBox.information(self, tr("Add"), tr("Load the catalogs and select an entry first."))
            return
        if entry.get("_listed") and "_known_mounts" not in entry:
            # A host older than 0.1.95 says the entry's line alone: which folders it takes, as this machine's own
            # read of the catalogs says (in the background; nothing known then: typed in all the same).
            background(lambda: self.sai.host_mcp_catalog(LOCAL), owner=self, source=self.add_button, timeout=180,
                       done=lambda result, error: self.add_known(key, entry, result, error))
            return
        self.add_entry(key, entry)

    def add_known(self, key, entry, result, error):
        """The folders this machine's catalog names for KEY (another host's line alone), then Add… for it."""
        found = dict((f"{prefix}:{e.get('name')}", e) for prefix, e in (result[0] if error is None and result else []))
        known = found.get(key) or next((e for e in found.values() if e.get("name") == entry.get("name")), None)
        entry["_known_mounts"] = [m for m in (known or {}).get("mounts", []) if isinstance(m, dict)]
        self.add_entry(key, entry)

    def add_entry(self, key, entry):
        host, catalog = self.catalog_of or (self.host(), "")
        dialog = AddServerDialog(self, entry, host, catalog)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        title = tr("Add {name}", name=printable(key)) if host == LOCAL else \
            tr("Add {name} on {host}", name=printable(key), host=printable(host))
        try:
            values = dialog.values(key)
            args, writable = backend.mcp_add_args(**values)
        except ValueError as exc:
            warn(self, title, printable_text(exc))
            return
        where = tr("On {host}: ", host=printable(host)) if host != LOCAL else ""
        applog.log("INFO", "mcp", f"Add: {printable(key)} from {printable(catalog or 'the default catalogs')} on "
                   f"{printable(host)} as {printable(values.get('name_as') or key)}"
                   + (", unsigned" if values.get("unsigned") else "")
                   + (f", writable folders {printable(', '.join(writable))}" if writable else ""))
        if values["unsigned"] and not ask_first(self, "mcp-unsigned", title, where + tr(
                "Add {name} although its catalog is not signed by a trusted key? Nothing checked that the catalog is "
                "the one its publisher wrote.", name=printable(key)), tr("Add unsigned")):
            return
        if values.get("network") == "lan" and not ask_first(self, "mcp-lan", title, where + tr(
                "Let the MCP server {name} reach your local network and this host, not only the internet (internet + "
                "LAN)?", name=printable(key)), tr("Allow the LAN")):
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
        if host != LOCAL and [m for m in values.get("mounts") or [] if m[1] != backend.MCP_DEFAULT_FOLDER]:
            # Another host's folders (a default is none: sandbox-ai there makes it): looked at there first (over SSH, as its folder chooser does) - one that is not a
            # folder said here, a read-only one that is not there offered to be made there. Before, `mcp add` there
            # refused it with its own words alone ("... is not there on this machine"; user report 2026-11-19).
            self.look_at_host_folders(host, key, values, args)
            return
        self.run_add(host, key, values, args)

    def look_at_host_folders(self, host, key, values, args):
        """The folders of VALUES looked at on HOST over SSH and SFTP in the background, then host_folders_read."""
        paths = list(dict.fromkeys(source for _target, source, _mode in values["mounts"]
                                   if source != backend.MCP_DEFAULT_FOLDER))
        try:
            target = RC.host_target(self.sai, host)
        except Exception as exc:  # noqa: BLE001 - no SSH host of sandbox-ai here: sandbox-ai there checks them
            self.host_folders_read(host, key, values, args, None, exc)
            return
        self.catalog_status.setText(tr("Looking at the folders on {host}…", host=printable(host)))

        def look():
            from . import remote
            root = remote.RemoteRoot(target, "/", ssh=RC.SSH, known_hosts=RC.KNOWN_HOSTS, config=RC.SSH_CONFIG)
            try:
                return remote.folder_states(root, paths)
            finally:
                root.close()
        background(look, owner=self, timeout=120,
                   done=lambda states, error: self.host_folders_read(host, key, values, args, states, error))

    def host_folders_read(self, host, key, values, args, states, error):
        """The folders of an MCP server added on HOST, as that host has them (remote.folder_states): all there - added;
        not a folder (a file, a link: never followed) or not absolute - said, nothing added; a read-only one that is
        not there - made there after a yes (a writable one `mcp add` makes itself), then added. Not looked at (no
        SSH connection of the app's own): added, and sandbox-ai there checks them."""
        title = tr("Add {name} on {host}", name=printable(key), host=printable(host))
        if error is not None:
            applog.log("INFO", "mcp", f"Add: the folders of {printable(key)} could not be looked at on "
                       f"{printable(host)} ({applog.error_text(error)}): sandbox-ai there checks them")
            self.run_add(host, key, values, args)
            return
        modes = {}
        for _target, source, mode in values["mounts"]:
            if source != backend.MCP_DEFAULT_FOLDER:  # (a default is made there by sandbox-ai, never over SSH)
                modes[source] = "rw" if mode == "rw" or modes.get(source) == "rw" else "ro"
        bad = [(path, state) for path, state in states.items() if state in ("file", "link", "relative")]
        if bad:
            words = {"file": tr("is not a folder"), "link": tr("is a symbolic link (not followed)"),
                     "relative": tr("is no absolute path")}
            text = "\n".join(f"{printable(path)} {words[state]}" for path, state in bad)
            self.catalog_status.setText(tr("{name} was not added.", name=printable(key)) + "\n" + text)
            warn(self, title, tr("These folders of {host} cannot be given to {name}:", host=printable(host),
                                 name=printable(key)) + "\n" + text + "\n\n" +
                 tr("Choose folders of {host} (… lists them there), then add it again.", host=printable(host)))
            return
        missing = [path for path, state in states.items() if state == "missing" and modes.get(path) != "rw"]
        if not missing:
            self.run_add(host, key, values, args)
            return
        if not confirm(self, title, tr(
                "Not there on {host}: {list}\n\nCreate these folders there (empty, only you can open them) and add "
                "{name} with them, read-only?", host=printable(host), list="\n".join(printable(p) for p in missing),
                name=printable(key)), tr("Create and add")):
            self.catalog_status.setText(tr("{name} was not added: its folders are not there on {host}.",
                                           name=printable(key), host=printable(host)))
            return
        applog.log("INFO", "mcp", f"Add: {printable(key)} on {printable(host)}: creating its folders there: "
                   + printable(", ".join(missing)))

        def made(result, failed):
            if failed is not None:
                said = printable_text(failed)
                self.catalog_status.setText(tr("{name} was not added.", name=printable(key)) + "\n" + said)
                warn(self, title, tr("The folders could not be created on {host}: {error}", host=printable(host),
                                     error=said))
                return
            self.run_add(host, key, values, args)
        try:
            target = RC.host_target(self.sai, host)
        except Exception as exc:  # noqa: BLE001 - (it was there a moment ago: said as the folders' failure)
            made(None, exc)
            return

        def make():
            from . import remote
            root = remote.RemoteRoot(target, "/", ssh=RC.SSH, known_hosts=RC.KNOWN_HOSTS, config=RC.SSH_CONFIG)
            try:
                return remote.make_folders(root, missing)
            finally:
                root.close()
        background(make, owner=self, work=True, timeout=120, done=made)

    def run_add(self, host, key, values, args):
        """`mcp add` for KEY in the background, each server its own (user 2026-11-19: another one can be added while
        one is: `mcp add` writes the server list under its file lock); the Add button stays usable, the same server
        is not added twice at once."""
        adding = self.__dict__.setdefault("adding", {})
        if (host, key) in adding:
            self.catalog_status.setText(tr("{name} is already being added.", name=printable(key))
                                        + self.still_adding())
            return
        adding[(host, key)] = True
        self.catalog_status.setText(self.adding_text())
        background(lambda: self.sai.host_run(host, *args, timeout=900), owner=self, work=True,
                   timeout=930, done=lambda result, error: self.added(host, key, values, result, error))

    def adding_text(self):
        """"Adding NAME, NAME…": the servers `mcp add` runs for now."""
        names = [key for _host, key in self.__dict__.get("adding", {})]
        return tr("Adding {name}…", name=printable(", ".join(names))) if names else ""

    def still_adding(self):
        text = self.adding_text()
        return "\n" + text if text else ""

    def added(self, host, key, values, result, error):
        """What `mcp add` said: on success the servers are read again (it is in the list above then); a refusal says
        on the page that nothing was added, and one only for an unsigned catalog offers to add it anyway. Servers
        still being added stay named below."""
        self.__dict__.setdefault("adding", {}).pop((host, key), None)
        status, out, err = outcome(result, error)
        said = printable_text((out + err).strip())
        self.refresh()
        applog.log("INFO" if status == 0 else "WARNING", "mcp", f"Add: {printable(key)} on {printable(host)} "
                   + ("added" if status == 0 else f"not added (exit status {status})\n{applog.tail(out + err)}"))
        if status == 0:
            self.catalog_status.setText(tr("{name} added: it is in the list above; use it with --mcp {local}.",
                                           name=printable(key), local=printable(values["name_as"]))
                                        + self.still_adding())
            report(self, "mcp add", result, error)
            return
        refusal = None if values["unsigned"] else backend.mcp_unsigned_refusal(said)
        if refusal is None:
            self.catalog_status.setText(tr("{name} was not added.", name=printable(key)) + "\n" + said
                                        + self.still_adding())
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
            except Exception as _exc:  # noqa: BLE001 - only the address for the question
                applog.swallowed("ui", "McpPage.offer_unsigned: only the address for the question", _exc)
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
            warn(self, title, printable_text(exc))
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
            field.set_host(host)
        self.note.setText(tr("Paths on {host}: chosen from {host}'s folders (over SSH).", host=printable(host))
                          if host != LOCAL else "")

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
        layout.addWidget(self.notes)
        self.notes.setVisible(bool(notes))  # (in the dialog first: else a window of its own for a moment)
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
        self.autostart_box.setToolTip(  # macOS: a LaunchAgent (backend.autostart_file)
            tr("Writes ~/Library/LaunchAgents/org.sandbox-ai.app.plist (a login item); unticked, it is removed")
            if sys.platform == "darwin" else
            tr("Writes ~/.config/autostart/sandbox-ai-app.desktop; unticked, it is removed"))
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
                                 error=printable_text(error)))
            backend.set_autostart(bool(on), forbidden=forbidden, also=[str(self.sai.directory)])
        except backend.InsideSandbox as exc:
            warn(self, tr("Start at login"), tr("Not written: the app runs from {path}, inside {folder}, which a "
                                                "sandbox can write (it could change what starts at your next "
                                                "login). Install the app outside sandbox folders.",
                                                path=printable(exc.path), folder=printable(exc.root)))
            on = None
        except (OSError, ValueError) as exc:
            warn(self, tr("Start at login"), printable_text(exc))
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
                                        problem=printable_text(problem)))
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
        short_notice(self, tr("Language"), tr("The language changes when the app is started again."))

    def check(self):
        background(self.sai.doctor, self.backend.currentText(), owner=self, work=True, source=self.check_button,
                   timeout=300, done=self.checked)

    def checked(self, results, error):
        if error is not None:
            warn(self, tr("Host check (sandbox-ai doctor)"), printable_text(error))
            return
        try:
            groups = getattr(self.sai.module("doctor"), "group_of", None)
        except Exception as _exc:  # noqa: BLE001 - the view's own table of areas
            applog.swallowed("ui", "SystemPage.checked: the view's own table of areas", _exc)
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
                                                        printable_text("\n".join(outcome(result, error)[1:]).strip()),
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
        said = printable_text((out + err).strip())[-300:]
        self.demo_text.setText(tr("{host}: {said}", host=printable(host), said=said) if host != LOCAL else said)

    def privacy(self):
        """Privacy check…: the repositories, rules and options asked for, checked on the host chosen (sandbox-ai
        privacy-check --json, in the background), the findings shown as a table."""
        dialog = PrivacyDialog(self, self.sai)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        host, paths, rules, project, every = dialog.values()
        try:
            backend.privacy_check_args(paths, rules or None, project or None, every)
        except ValueError as exc:
            warn(self, tr("Privacy check"), printable_text(exc))
            return
        title = tr("Privacy check: {path}", path=printable(", ".join(paths))[:200]) if host == LOCAL else \
            tr("Privacy check on {host}: {path}", host=printable(host), path=printable(", ".join(paths))[:200])
        background(self.sai.privacy_check, host, paths, rules or None, project or None, every, owner=self, work=True,
                   timeout=660, source=self.privacy_button,
                   done=lambda result, error: self.privacy_checked(title, result, error))

    def privacy_checked(self, title, result, error):
        from . import widgets
        if error is not None:
            show_text(self, title, printable_text(str(error)), modal=False)
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
# (user 2026-11-24: the page Start is called Board, and it stands in the section of what runs, renamed Project; user
# 2026-11-18: the terminals right after the sessions they belong to, then the approvals)
SECTIONS = ((N_("Project"), (("launch", N_("Board")), ("board", N_("Board")), ("sandboxes", N_("Sessions")),
                             ("terminals", N_("Terminals")), ("approvals", N_("Approvals")))),
            # Manage before Monitoring, the Files page first under Manage (it manages files; it watches nothing).
            (N_("Manage"), (("files", N_("File manager")), ("kits", N_("Kits & plugins")), ("mcp", N_("MCP servers")),
                            ("llm", N_("LLM servers")), ("images", N_("Images")), ("credentials", N_("Credentials")),
                            ("team", N_("Teams")))),
            (N_("Monitoring"), (("connections", N_("Connections")), ("activity", N_("Activity")),
                             ("traffic", N_("LLM traffic")), ("statistics", N_("Statistics")),
                             ("monitor", N_("System monitor")))),
            (N_("System"), (("system", N_("System")), ("hosts", N_("Hosts")), ("help", N_("Help")))))
PAGES = tuple(page for _, pages in SECTIONS for page in pages)
# The Start page has two views of one start, with a switch above them (start_link.py): Board ("board", the default)
# and Expert ("launch": every option a field, and the command line). Each view is a page of the window of its own key -
# its panels, help, video and tour are its own - but the navigation has one entry for both, Start: the row of
# "launch". VIEW_OF: {a view without a row of its own: the page whose row it is shown under}.
VIEW_OF = {"board": "launch"}
# A view of the Board page by its own name, where the page's name alone does not say which (Go, the area tours).
VIEW_TITLES = {"launch": N_("Board: Expert view"), "board": N_("Board: Board view")}
START_VIEWS = start_link.VIEW_KEYS
SHORTCUT_PAGES = 9
HISTORY_KEPT = 50  # the steps Back can go (MainWindow.page_history)
SHOW_ME_SECONDS = 6  # how long the Help's "Show me" outlines a control
NOTIFY_AT_ONCE = 5  # new questions announced one by one at once; more are counted in one notification
NAV_WIDTH, COMPACT_NAV_WIDTH = 224, 52  # the navigation with titles, and as a strip of icons
NAV_MAX_WIDTH = 300  # the navigation with titles at most, however long a title is in the language chosen
OWN_START_WINDOW = 120  # seconds in which a run that shows up is the one the app just started in its terminal
START_MESSAGE_MS = 15000  # the status line's "sandbox-ai VERSION from PATH" at the start (the version stays bottom right)


# A running session whose tmux session has ended (sessions.terminal_state "ended"): what its row and Attach say.
NO_AGENT = N_("Its tmux session has ended - the agent and its shell window - but its sandbox has not: there is no agent "
              "to attach to any more. Stop it, then Resume continues it. The end of its log:")


def terminal_of(instance):
    """"starting", "missing" or "ended" for a running start whose terminal is not there yet, that runs without one,
    or whose tmux session has ended (the record's "terminal": sessions.terminal_state, list --json); None otherwise."""
    record = getattr(instance, "record", None) or {}
    found = record.get("terminal") if isinstance(record, dict) else None
    return found if getattr(instance, "state", None) == "running" and found in ("starting", "missing", "ended") \
        else None


def attach_why_of(instance):
    """Why the running start INSTANCE (of the Sessions page) has no terminal to attach to, in the app's language;
    "" when it has: another host's says it (list --json there: attachable, attach_why), this machine's as the
    Sessions page's Attach decides (a session with tmux, running, whose terminal is there)."""
    record = instance.record if isinstance(instance.record, dict) else {}
    if getattr(instance, "host", LOCAL) != LOCAL:
        return terminal.attach_why({**record, "kind": record.get("kind") or instance.kind,
                                    "state": record.get("state") or instance.state})
    state, found = instance.state, terminal_of(instance)
    if instance.kind == "mcp":
        why = "mcp"
    elif state == "paused":
        why = "paused"
    elif state != "running":
        why = "stopped"
    elif instance.kind != "session" or getattr(instance, "foreground", False):
        why = "foreground"
    elif found == "starting":
        return tr("Its terminal is not there yet (it is starting): try again in a moment.")
    elif found in ("missing", "ended"):
        why = "no-terminal" if found == "missing" else "ended"
    else:
        return ""
    return terminal.attach_why({"attach_why": why})


def running_session(window, argv):
    """The session of this machine a start ARGV (sandbox-ai AGENT ... --session NAME, NAME before --) would start
    again while WINDOW's Sessions page knows it running, of that agent, with a terminal to attach to; else None."""
    agent = started_agent(argv)
    options = list(argv[:argv.index("--")] if "--" in argv else argv)
    if agent is None or "--session" not in options or options.index("--session") + 1 >= len(options):
        return None
    name = options[options.index("--session") + 1]
    built = getattr(window, "built", None)
    page = built("sandboxes") if callable(built) else None
    for instance in (getattr(page, "instances", None) or {}).values():
        if instance.kind == "session" and getattr(instance, "host", LOCAL) == LOCAL and \
                name in (instance.name, instance.session) and instance.state == "running" and \
                instance.agent == agent and not instance.foreground:
            return instance
    return None


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
# What each state means for a start in the project: the project bar's icon says it on hover.
PROJECT_STATE_MEANINGS = {
    "none": N_("This folder has no project configuration: a start here takes only the options you give it. Save as "
               "project configuration… on the Board page writes one."),
    "trusted": N_("You reviewed this file and trusted exactly its content: every start in this folder applies its "
                  "options. If it changes, it is not used until you trust it again."),
    "changed": N_("The file changed since you trusted it: the launcher refuses to start here until you review and "
                  "trust it again (Review and trust… on the Board page)."),
    "untrusted": N_("Nobody has trusted this file yet: the launcher refuses to start here until you review and trust "
                    "it (Review and trust… on the Board page)."),
    "unreadable": N_("The file is there but cannot be read: the launcher cannot use it.")}
PROJECT_WARNINGS = ("changed", "untrusted", "unreadable")  # states that hold up a start in the project


def middle(text, limit=90):
    text = str(text)
    return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]


class ProjectCombo(QComboBox):
    """The switcher, at the top left of the window: the current project's name, large (its path below it and in the
    tooltip); its list, read again each time it opens, names each project with its path."""
    opening = Signal()
    NAME = Qt.ItemDataRole.UserRole + 7  # an entry's short name: what the closed switcher shows

    def showPopup(self):  # noqa: N802
        self.opening.emit()
        # The list as wide as its entries (name and path), however narrow the name above it is.
        view = self.view()
        view.setMinimumWidth(min(max(self.width(), view.sizeHintForColumn(0) + 32), 900))
        super().showPopup()

    def shown_text(self):
        """What the closed switcher shows: the current entry's name (not its path)."""
        name = self.currentData(self.NAME)
        return name if isinstance(name, str) else self.currentText()

    def paintEvent(self, _event):  # noqa: N802
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self.shown_text()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)

    def sizeHint(self):  # noqa: N802 - as wide as the name shown, not as the longest entry of the list
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        metrics = self.fontMetrics()
        size = QSize(metrics.horizontalAdvance(self.shown_text()) + 8, metrics.height())
        return self.style().sizeFromContents(QStyle.ContentsType.CT_ComboBox, option, size, self)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()


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
    """The current project at the top of the window: its name, large, is the switcher (a drop-down: the projects
    sandbox-ai and the app know, SandboxAI.known_projects, then Open folder… and New project (wizard)…); beside it an
    icon for the state of its .sandbox-ai.json (its name and meaning on hover); below, what runs there and its
    path. Shown once: no second "Project: name - path" at the right end."""
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
        self.combo = ProjectCombo()
        self.combo.setObjectName("projectName")  # large, as the project's name
        self.combo.setMaximumWidth(520)
        self.combo.setAccessibleName(tr("Project"))
        self.combo.opening.connect(self.fill)
        self.combo.activated.connect(self.activated)
        # The state of its .sandbox-ai.json as an icon (theme.TRUST_GLYPHS); hovering says which and what it means.
        self.state, self.state_key = QLabel(), None
        self.state.setFixedSize(QSize(20, 20))
        self.where = plain_label(word_wrap=False, role="secondary")
        self.where.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        first.addWidget(self.combo)
        first.addSpacing(6)
        first.addWidget(self.state)
        first.addStretch()
        texts.addLayout(first)
        second = QHBoxLayout()
        self.activity = plain_label(word_wrap=False, role="secondary")
        second.addWidget(self.activity)
        second.addWidget(self.where, 1)
        texts.addLayout(second)
        layout.addWidget(self.icon)
        layout.addLayout(texts, 1)
        self.controls = layout
        self.show_state(None)
        self.fill()

    def add_controls(self, buttons, window_buttons=()):
        """BUTTONS (the browser, the emergency stop) and the window's own WINDOW_BUTTONS (minimize, maximize, full
        screen, tray, quit) at the right end: in one row where there is room; on a narrow window (a 800 px screen)
        the window's buttons go below the others, together (Board review 2, R2-19: as one row the bar set the
        window's least width, 878 px with the light theme's icons)."""
        self.controls.addSpacing(10)
        holder = QWidget()
        row = widgets.FlowLayout(holder, line=True)
        for group in (buttons, window_buttons):
            if group:
                row.addWidget(widgets.together(*group))
        self.controls.addWidget(holder, 0, Qt.AlignmentFlag.AlignVCenter)
        self.control_row = holder

    def restyle(self):
        self.icon.setPixmap(theme.glyph("project", theme.colors()["title"], 26).pixmap(26, 26))
        self.show_state(self.state_key, self.state_detail)

    def fill(self, read=True):
        """The known projects (as last read; READ: read again, in the background), the current one selected; then
        the two actions."""
        if read:
            self.known.refresh()
        known = self.known.value
        self.combo.blockSignals(True)
        self.combo.clear()
        if self.path is None:
            self.combo.addItem(tr("No project"), None)
        paths = [path for path, _ in known]
        if self.path and self.path not in paths:
            known = [(self.path, set())] + list(known)
        for path, sources in known:
            name = printable(Path(path).name or path)
            self.combo.addItem(f"{name}  —  {printable(middle(path, 60))}", path)
            self.combo.setItemData(self.combo.count() - 1, middle(name, 40), ProjectCombo.NAME)
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
        self.combo.updateGeometry()  # as wide as the name it shows now
        self.combo.update()

    def name(self):
        """The project's name as the switcher shows it."""
        return self.combo.shown_text()

    def activated(self, index):
        data = self.combo.itemData(index)
        self.select_current()  # the window switches, then shows the project; a refusal leaves it as it was
        if data == self.OPEN:
            self.open_folder.emit()
        elif data == self.NEW:
            self.new_project.emit()
        elif isinstance(data, str) and data != self.path:
            self.chosen.emit(data)

    def show_project(self, path, state=None, detail=""):
        """PATH as the current project; STATE: of its .sandbox-ai.json (a key of PROJECT_STATES; None: not read
        yet), DETAIL: why it cannot be read."""
        self.path = path
        self.show_state(state if path else None, detail)
        self.where.setText(printable(middle(path)) if path else tr("Choose a project folder to work in"))
        self.where.setToolTip(safe_tooltip(path) if path else "")
        self.combo.setToolTip(safe_tooltip("\n".join(filter(None, [printable(path) if path else "", tr(
            "Switch the project: the Board page, the Board, Connections, Statistics, Files and Approvals follow it")]))))
        self.fill()

    def show_state(self, state, detail=""):
        """The icon of the .sandbox-ai.json's STATE (None: none shown); its tooltip and accessible name say the
        state, the tooltip also what it means (and DETAIL: why it cannot be read)."""
        self.state_key, self.state_detail = (state, detail) if state in PROJECT_STATES else (None, "")
        self.state.setVisible(self.state_key is not None)
        if self.state_key is None:
            self.state.clear()
            self.state.setToolTip("")
            self.state.setAccessibleName("")
            return
        self.state.setPixmap(theme.trust_glyph(state, 20).pixmap(20, 20))
        name = tr(PROJECT_STATES[state])
        meaning = tr(PROJECT_STATE_MEANINGS[state]) + (f" ({printable(detail)[:200]})" if detail else "")
        self.state.setToolTip(safe_tooltip(f"{name}\n{meaning}"))
        self.state.setAccessibleName(name)
        self.state.setAccessibleDescription(meaning)


SECTION_ROLE = Qt.ItemDataRole.UserRole + 1  # the navigation's section headers
STATE_COLORS = {"running": "ok", "paused": "warn", "starting": "warn", "stopped": "muted", "damaged": "bad",
                "failed": "bad"}
# A session's or sandbox's tab in the window's tab row whose start ended normally closes by itself after this moment
# (the human sees it end); one whose run failed (an exit status other than 0) stays, marked as failed.
AUTO_CLOSE_MS = 3000
ACTIVE_TAB_STATES = ("running", "paused", "starting", "orphaned")


def menu_text(text):
    """TEXT for a menu entry or tab: shown as it is (an & would mark a shortcut key)."""
    return printable(text).replace("&", "&&")


def project_entry(path):
    path = os.path.realpath(os.path.expanduser(str(path)))
    return {"key": f"project:{path}", "kind": "project", "path": path}


def parts_of_starts(instances):
    """The keys of INSTANCES (backend.Instance, of any hosts) that belong to another start (start_tree.owners, host by
    host): the starts the Sessions page shows below another one."""
    hosts = {}
    for instance in instances:
        hosts.setdefault(getattr(instance, "host", "local"), []).append(instance)
    return {key for group in hosts.values() for key in owners(group)}


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


class ProjectTabs(widgets.ArrowTabBar):
    """The open projects, sessions and sandboxes. Closable, movable, closed with a middle click too; the
    entry of a tab (its tab data) says what it stands for. The app's tab row: ← → at its end when they do not fit."""
    middle_clicked = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("projectTabs")
        self.setMovable(True)
        self.setExpanding(False)
        self.setDocumentMode(True)
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
            holder, _close = widgets.tab_close_button(self)  # (inside the tab: Qt puts it flush with its edge)
            self.setTabButton(index, QTabBar.ButtonPosition.RightSide, holder)
            self.set_entry(index, entry)
        finally:
            self.blockSignals(False)
        if first:
            self.currentChanged.emit(index)
        return index

    def set_entry(self, index, entry):
        self.setTabData(index, dict(entry))
        self.decorate(index)

    def decorate(self, index):
        entry = self.entry(index)
        if entry is None:
            return
        close = getattr(self.tabButton(index, QTabBar.ButtonPosition.RightSide), "close_button", None)
        if close is not None:
            close.setIcon(theme.glyph("quit", theme.colors()["text"], 12))
        path = entry.get("path") or ""
        if entry["kind"] == "project":
            self.setTabText(index, menu_text(Path(path).name or path))
            self.setTabIcon(index, theme.glyph("project", theme.colors()["text"], 16))
            self.setTabToolTip(index, safe_tooltip(path))
            return
        state = entry.get("state") or "stopped"
        details = ", ".join(filter(None, [entry.get("agent"), entry.get("backend")]))
        text = menu_text(entry.get("name", "") + (f" ({details})" if details else ""))
        if self.tabText(index) != text:  # (a text set again lays the whole row out again, unchanged too)
            self.setTabText(index, text)
        self.setTabIcon(index, theme.dot(STATE_COLORS.get(state, "muted")))
        kind = tr("Session") if entry["kind"] == "session" else tr("Run")
        tip = safe_tooltip(" · ".join(filter(None, [
            f"{kind} {entry.get('name', '')}", details, tr(STATE_TITLES.get(state, state)), path])))
        if self.tabToolTip(index) != tip:
            self.setTabToolTip(index, tip)

    def restyle(self):
        for index in range(self.count()):
            self.decorate(index)


# The pages with a host chooser of their own, which follow the host chosen on the Sessions page (host_choice.HostFollow):
# key -> ([its choosers, the page's own first], the line beside it that says whose host it shows). Not the Start page
# and the Board: they choose where a start runs, kept with the project and the board.
FOLLOWERS = {
    "approvals": lambda page: ([page.host_filter, page.always_host, page.history_host], page.follow_note),
    "terminals": lambda page: ([getattr(page, "host", None)], getattr(page, "follow_note", None)),  # (with sai)
    "mcp": lambda page: ([page.host_choice], page.follow_note),
    "llm": lambda page: ([page.host_box], page.follow_note),
    # (whose sandboxes the left pane offers: that host's, the pane showing one of them)
    "files": lambda page: ([page.host_box], page.follow_note),
    **{key: (lambda page: ([page.hosts.box], page.hosts.note))
       for key in ("kits", "images", "credentials", "team", "connections", "activity", "traffic", "statistics",
                   "monitor")},
}
# Pages with a canvas or terminal of their own that fills the page: never in a scroll area.
UNSCROLLED_PAGES = ("board", "terminals")
# The order the pages are made in. A lazy window (the app's start) makes FIRST_PAGES with itself - the first page, the
# Board and the open questions (their panels are the window's), what runs (the tabs follow it) - and the others after
# the first paint (LAZY_START ms after it: the first moments are the human's; in the tray: after LAZY_FALLBACK ms), or
# when they are first asked for: one at a time, LAZY_STEP ms apart, each once the mouse and the keyboard rested LAZY_QUIET ms and the other threads used less than LAZY_BUSY of
# a processor (waited for LAZY_PATIENCE steps at most).
BUILD_ORDER = ("launch", "board", "sandboxes", "activity", "approvals", "connections", "statistics", "files", "kits",
               "mcp", "llm", "team", "images", "credentials", "terminals", "system", "hosts", "help", "traffic",
               "monitor")
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


def make_monitor_page(sai):
    from .monitor_page import MonitorPage
    return MonitorPage(sai)


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

    # Watched where the platform's input arrives first: the app's own windows (their QWindow), each one shown or given
    # the focus meanwhile (a dialog). Never with a filter on the whole application: a filter in Python sees every event
    # of every object, Chromium's of the terminal tabs too, and with one installed the next terminal tab ended the
    # process (a segmentation fault in PySide; stability round 3, the soak).

    def __init__(self, app):
        super().__init__()
        self.app, self.last, self.windows, self.paused = app, 0.0, [], False
        app.focusWindowChanged.connect(self.focused)
        self.resume()

    def eventFilter(self, _watched, event):  # noqa: N802
        if event.type() in self.KINDS:
            self.last = time.monotonic()
        return False

    def own_windows(self):
        return [handle for widget in QApplication.topLevelWidgets() if alive(widget) and widget.isVisible()
                and (handle := widget.windowHandle()) is not None and alive(handle)]

    def watch(self, window):
        """Watch WINDOW (once). A window that goes is forgotten as it goes: dialogs are deleted when they close, and
        a wrapper kept of one - its address taken by a new window - raised "already deleted" at every focus change."""
        try:
            if not alive(window) or any(seen is window for seen in self.windows):
                return
            window.installEventFilter(self)
            self.windows.append(window)
            window.destroyed.connect(lambda _=None, w=window: self.forget(w))
        except RuntimeError:  # gone meanwhile
            return

    def forget(self, window):
        self.windows = [seen for seen in self.windows if seen is not window]

    def focused(self, window):
        if self.app is None or self.paused or window is None or not alive(window):
            return
        if any(own is window for own in self.own_windows()):
            self.watch(window)

    def pause(self):
        self.paused = True
        for window in self.windows:
            if alive(window):
                window.removeEventFilter(self)
        self.windows = []

    def resume(self):
        if self.app is None:
            return
        self.paused = False
        for window in self.own_windows():
            self.watch(window)

    def stop(self):
        if self.app is not None:
            self.pause()
            try:
                self.app.focusWindowChanged.disconnect(self.focused)
            except (RuntimeError, TypeError):
                pass
            self.app = None


class SideButtons(QObject):
    """The mouse's side buttons (back, forward, and the one after them: the settings) anywhere over the window and its
    floated panels: watched on their QWindow, where the input arrives before any widget gets it - lists, tables,
    text fields, the board and the terminals' web views take a press of any button and never pass it on to the
    window. Never with a filter on the whole application (see InputWatch). The press goes to WINDOW.side_button;
    the press, its release and a double click of those buttons go nowhere else."""
    BUTTONS = (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton, Qt.MouseButton.TaskButton)
    KINDS = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick)

    def __init__(self, window):
        super().__init__(window)
        self.window, self.watched = window, []

    def watch(self, handle):
        try:
            if handle is None or not alive(handle) or any(seen is handle for seen in self.watched):
                return
            handle.installEventFilter(self)
            self.watched.append(handle)
            handle.destroyed.connect(lambda _=None, h=handle: self.forget(h))
        except RuntimeError:  # gone meanwhile (see InputWatch.watch)
            return

    def forget(self, handle):
        self.watched = [seen for seen in self.watched if seen is not handle]

    def eventFilter(self, _watched, event):  # noqa: N802
        if event.type() not in self.KINDS or event.button() not in self.BUTTONS:
            return False
        if event.type() == QEvent.Type.MouseButtonPress:
            self.window.side_button(event.button())
        return True


class PanelDock(QDockWidget):
    """A panel of a page (View > Panels): closing it by its button is remembered (closed_by_user); hiding
    it because another page is shown is not."""
    closed_by_user = Signal()

    def closeEvent(self, event):  # noqa: N802
        super().closeEvent(event)
        self.closed_by_user.emit()


def escape_closes(dock):
    """Esc closes a panel floated out of the window (as it closes the app's dialogs): a shortcut of that panel, on
    only while it floats (docked, Esc keeps its meaning in the window - the board folds its properties). Never an
    application-wide filter (see InputWatch)."""
    shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), dock)
    shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    shortcut.activated.connect(lambda: dock.isFloating() and dock.close())
    shortcut.setEnabled(dock.isFloating())
    dock.topLevelChanged.connect(shortcut.setEnabled)
    return shortcut


class KeepFocusOnActivation(QObject):
    """A panel shown floating without being asked for the keyboard (reopen_panel): when the window system activates
    its window anyway (offscreen, window managers that ignore WA_ShowWithoutActivating), the keyboard goes back to
    WIDGET. Watched on the panel's QWindow (where the platform's events arrive first) until that activation, until
    the human presses a button in the panel (then it is wanted there) or for DEADLINE seconds. Not a fixed delay: a
    busy GUI thread (a loaded machine) handles the window system's activation after any timer due meanwhile (one
    pass of the event loop runs the timers first), and the keyboard stayed in the panel (test_board_mouse)."""
    DEADLINE = 5.0
    PRESSES = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.TouchBegin,
               QEvent.Type.TabletPress}

    def __init__(self, handle, widget, keep):
        super().__init__(handle)
        self.handle, self.widget, self.keep = handle, widget, keep
        self.since, self.pressed, self.activated = time.monotonic(), False, False
        for old in handle.findChildren(KeepFocusOnActivation):  # shown again meanwhile: only the latest watches
            if old is not self:
                old.stop()
        handle.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        kind = event.type()
        if kind in self.PRESSES:
            self.pressed = True
            if not self.activated:
                self.stop()
        elif kind == QEvent.Type.FocusIn and not self.activated:
            self.activated = True
            if time.monotonic() - self.since > self.DEADLINE:
                self.stop()
            else:  # once Qt has handled the activation (it gives the panel's first field the keyboard)
                QTimer.singleShot(0, self, self.give_back)
        return False

    def give_back(self):
        pressed = self.pressed
        self.stop()
        if not pressed:
            self.keep(self.widget)

    def stop(self):
        try:
            self.handle.removeEventFilter(self)
            self.deleteLater()
        except RuntimeError:  # the window is gone meanwhile (docked again)
            pass


# (name, title, page, default area) of the panels that can be floated, docked elsewhere and closed.
# The inspector (properties) also folds to a strip while docked and unfolds when a part is clicked (dock_fold); the
# findings are closed until asked for.
DOCKS = (("parts", N_("Board: parts"), "board", Qt.DockWidgetArea.LeftDockWidgetArea),
         ("inspector", N_("Board: properties"), "board", Qt.DockWidgetArea.RightDockWidgetArea),
         ("findings", N_("Board: security score and findings"), "board", Qt.DockWidgetArea.BottomDockWidgetArea),
         # (The Approvals page's open questions were a panel above it until 2026-11-24: its first tab now.)
         # The log window: on every page (page None), at the bottom, folded to its title bar at first.
         ("log", N_("Log"), None, Qt.DockWidgetArea.BottomDockWidgetArea))
CLOSED_AT_FIRST = {"findings"}  # panels closed until asked for (also when the saved state predates them)


class MainWindow(QMainWindow):
    """The window. With a system tray (and the setting, on by default), closing it hides it: the approvals
    are still polled and announced; Quit in the tray menu ends the app (with the usual questions)."""
    closed = Signal()  # the window really closed: the app ends (main connects it to quit)

    def __init__(self, sai, check_updates=True, approvals_interval=1000, risk=None, lazy=False):
        super().__init__()
        labels.set_flags_shown(backend.setting("show_flags"))  # before the forms that show them are made
        widgets.set_explanations_shown(backend.setting("explanations"))  # the pages' info texts: before they are made
        button_icons.set_texts_shown(backend.setting("button_texts"))  # the icon buttons' texts: before they are made
        # The mouse wheel over a combo box, spin box or slider nobody clicked scrolls the page, on every window of the
        # app (one guard for the whole run: widgets.WheelGuard).
        widgets.wheel_guard(QApplication.instance())
        # quitting: SIGTERM or logout (no questions); leaving: a confirmed quit from the tray menu (no hiding);
        # finished: the window has closed for good (shutdown ran, exactly once).
        self.sai, self.quitting, self.leaving, self.finished, self.stopped = sai, False, False, False, False
        self.close_seen = False
        if lazy:  # the first pages' reads start once the window has painted itself (eventFilter), at the latest after
            widgets.hold_background()  # a while (shown minimized; a window that failed to be made)
            QTimer.singleShot(LAZY_FALLBACK, widgets.release_background)
        # What the window looked like when it was left (state.json); file dialogs remember their folders there.
        self.state = State(parent=self)
        if sai.hosts_supported():
            # hosts added, changed or removed outside the app (sandbox-ai hosts, the web interface): every host
            # chooser follows, as for a change on the Hosts page (review 6 M-H2); a file watch, never a poll
            from .host_choice import watch_hosts_file
            watch_hosts_file(sai.hosts_file())
        self.setWindowTitle(f"sandbox-ai {sai.version()}")
        self.setWindowIcon(theme.app_icon())  # (the taskbar's, X11's window list; macOS and .desktop have their own)
        self.resize(1240, 840)
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # RISK (backend.InsideSandbox from the start check): stays on screen for as long as the app runs.
        self.banner = plain_label(role="notice-error")
        if risk is not None:
            self.banner.setText(risk_text(risk))
        outer.addWidget(self.banner)
        self.banner.setVisible(risk is not None)  # (in the window first: else a window of its own for a moment)
        problems = [p for p in (backend.settings_problem(), self.state.problem) if p]
        self.notice_bar = plain_label(tr("Saved settings could not be read; the app started with defaults: {problems}",
                                         problems=printable("; ".join(problems))) if problems else "",
                                      role="notice-warn", selectable=True)
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
        # Back and Forward (the mouse's side buttons, Alt+Left and Alt+Right, Go > Back and Forward): the pages shown,
        # in order, with the Help section shown; HISTORY_AT is where Back and Forward are now (go_history).
        self.page_history, self.history_at, self.history_moving = [], -1, False
        self.side_buttons = SideButtons(self)
        self.own_starts = []  # (agent, project folder, when) of sandboxes started in the app's terminal
        self.pages = PageStack()
        # The host chosen on the Sessions page: the pages with a host chooser of their own show it too (FOLLOWERS).
        self.host_follow = HostFollow(self)
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
            "traffic": lambda: TrafficPage(sai, self),  # what an agent and its model exchanged (sandbox-ai llm traffic)
            "approvals": lambda: ApprovalsPage(sai, interval=approvals_interval
                                               and backend.setting("approvals_interval")),
            "connections": lambda: ConnectionsPage(sai),
            "statistics": lambda: StatisticsPage(sai),
            "monitor": lambda: make_monitor_page(sai),  # the system monitor: what the runs use (sandbox-ai top)
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
        # The Start page's view: the one chosen last (kept: start_view), Board for a new installation; view_wanted:
        # the view something asked for by its own key (show_page("board")), until the row shows it.
        self.start_view, self.view_wanted, self.start_link = start_link.DEFAULT_VIEW, None, None
        # The Start page's switch between its two views, Board and Expert (shown on that page only; each view names
        # it as its own view_switch: the tours point at it).
        self.view_switch = ViewSwitch()
        self.view_switch.chosen.connect(self.choose_view)
        self.view_switch.hide()
        for section, pages in SECTIONS:
            header = QListWidgetItem(tr(section).upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.AccessibleTextRole, tr(section))
            header.setData(SECTION_ROLE, section)
            header.setIcon(theme.glyph("fold-open", theme.colors()["nav_text"], 14))
            self.nav.addItem(header)
            self.section_rows[section] = (self.nav.count() - 1, [])
            for key, title in pages:
                if key in VIEW_OF:  # a view of the page before it (the Board: of Start): under that page's row
                    self.nav_rows[key] = self.nav_rows[VIEW_OF[key]]
                else:
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
                if len(self.keys) <= SHORTCUT_PAGES and key not in VIEW_OF:
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
        strip_layout.addWidget(self.view_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.page_help = help_view.HelpButton("page.launch")  # explains the page shown (page_changed)
        strip_layout.addWidget(self.page_help, 0, Qt.AlignmentFlag.AlignVCenter)
        # The video about the page shown (video_player.py; the videos are downloaded once when they are not here).
        self.page_video = QToolButton()
        self.page_video.setObjectName("pageVideo")
        self.page_video.setAutoRaise(True)
        self.page_video.setIcon(theme.glyph("video", theme.colors()["accent"], 18))
        self.page_video.setCursor(Qt.CursorShape.PointingHandCursor)
        self.page_video.clicked.connect(lambda _=False: self.watch_video(self.current_page()))
        strip_layout.addWidget(self.page_video, 0, Qt.AlignmentFlag.AlignVCenter)
        self.show_page_video("launch")
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
        # The keyboard starts in the navigation, on the page shown (else on the « that folds it: the first in the tab
        # order, its focus ring the first thing seen once the focus became visible, design review 0.1.95).
        self.nav.setFocus(Qt.FocusReason.OtherFocusReason)
        self.make_docks()
        widgets.LOG_WINDOW_OPENER = self.open_log_window  # Ctrl+Shift+L inside the dialogs too
        # Terminal tabs: an OpenGL window from the start - else the first tab made Qt make the window again.
        settings_now = sai.settings() if hasattr(sai, "settings") else {}
        self.gl_surface = terminal.prepare_window(self, wanted=settings_now.get("terminal", "app") == "app")
        help_view.OPENER = self.show_help
        help_view.WINDOW[:] = [weakref.ref(self)]  # the Help's screenshots show this window's pages
        help_view.SHOW_ME = self.show_me
        help_view.VIDEO_OPENER = self.watch_video  # the videos about the pages (the Help page's list, the (?)s)
        help_view.VIDEO_SAI[:] = [sai]
        help_view.set_shown(backend.setting("help_buttons"))  # the setting; the Help page and F1 stay either way
        short_notices.set_enabled(backend.setting("short_notices"))  # off: only the status bar and the log
        # The (?)s of the pages' areas and controls, hover explanations (a page made later gets its own then).
        self.help_buttons = help_view.install(self, {key: page for key in BUILD_ORDER
                                                     if (page := self.built(key)) is not None})
        help_view.explain_windows(QApplication.instance())  # and those of the dialogs, when they are opened
        # Copy (Ctrl+C, the context menu) in every list, table and tree: the pages' now, a later one's and the
        # dialogs' when they get the focus; not in the navigation (no data in it).
        self.nav.setProperty(copying.PROPERTY, False)
        for page in (self.built(key) for key in BUILD_ORDER):
            if page is not None:
                copying.copyable_all(page)
        copying.watch_focus(QApplication.instance())
        self.statusBar().showMessage(tr("sandbox-ai {version} from {path}", version=sai.version(), path=sai.directory),
                                     START_MESSAGE_MS)
        self.make_busy_indicator()
        self.tray, self.tray_timer = None, None
        # The emergency stop and Lift every cut (request_cut): those on their way, the one to go after them.
        self.cut_jobs, self.cut_next, self.cut_serial, self.lift_question = [], None, 0, None
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
        # While this window watches the questions and notifies of them, the launchers show no notification of their
        # own (one prompt per question: this one, with its answers): a heartbeat every few seconds.
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(5000)
        self.heartbeat_timer.timeout.connect(self.watcher_heartbeat)
        if approvals_interval:
            self.heartbeat_timer.start()
            QTimer.singleShot(0, self.watcher_heartbeat)
        # The running sandboxes' safeguards that refuse or end something: popups (grouped) and the Safeguards tab.
        from .safeguards_view import SafeguardAlerts
        self.safeguard_alerts = SafeguardAlerts(sai, self.safeguard_popup,
                                                interval=approvals_interval and backend.setting("sandboxes_interval"),
                                                parent=self)
        self.safeguard_alerts.arrived.connect(self.safeguards_arrived)
        self.update_window_controls()
        self.update_check = AsyncCommand(timeout=60, parent=self)
        self.update_check.finished.connect(self.update_checked)
        self.make_update_notice()
        # The update check: at the start, then every hour whether the day's check is due (an app that runs for days
        # still sees a new release; the update mode's last check, shared with the command line, says when).
        self.update_clock = time.time
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(UPDATE_EVERY_SECONDS * 1000)
        self.update_timer.timeout.connect(self.start_update_check)
        if check_updates:
            QTimer.singleShot(1500, self.start_update_check)
            self.update_timer.start()
        # The storage check (sandbox-ai gc --check, storage.py): at the start (after a while: the window first) and
        # then as Settings › Storage says - weekly by default, daily, or off; it applies the storage rules, records
        # each start's size (the Size column) and says above the threshold what could be freed (Optimize…). As the
        # update check: not in a window made without it (the tests').
        self.storage_running = False
        self.storage_timer = QTimer(self)  # (the window's own: it ends with it)
        self.storage_timer.timeout.connect(self.storage_timed)
        if check_updates:
            self.storage_timer.start(STORAGE_FIRST_SECONDS * 1000)
        self.sandboxes_here = 0
        self.questions_changed(self.approvals.open.rowCount(), [])
        self.nav.setCurrentRow(self.nav_rows["launch"])
        # What runs is known with the first read of the Sessions page (in the background): what starts after that
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
        copying.copyable_all(page)
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
        if key in START_VIEWS:
            page.view_switch = self.view_switch
        if key in START_VIEWS and self.start_link is None and all(self.built(view) is not None
                                                                  for view in START_VIEWS):
            # The two views of the Start page show one start from now on (before their state is read: attach_state).
            self.start_link = StartLink(self.built("board"), self.built("launch"))
        if key in FOLLOWERS:  # the Sessions page's host (chosen there before it was made: taken when shown)
            combos, note = FOLLOWERS[key](page)
            self.host_follow.attach(key, page, combos, note)
        if key == "help":
            page.tour_requested.connect(self.start_any_tour)
            page.section_shown.connect(self.help_section_shown)
        elif key == "launch":
            page.opened.connect(self.project_opened)
            page.images_wanted.connect(self.show_your_images)
            page.view_wanted.connect(self.choose_view)
        elif key == "sandboxes":
            page.board_request = self.show_on_board  # its "Show on the Board" (button and right-click menu)
            combo = page.host_filter
            combo.currentIndexChanged.connect(lambda _=0: self.host_follow.choose(combo.currentData()))
            page.open_log.connect(self.show_log)
            page.open_remote_log.connect(self.show_remote_log)
            page.open_credentials.connect(self.show_credentials_of)
            page.open_given.connect(self.show_given_of)
            page.show_reach.connect(self.show_reach_of)
            page.start_wanted.connect(lambda agent, argv, arguments, host: self.show_start_line(
                agent, argv, arguments, host))
            page.counted.connect(lambda running, here: self.update_activity(sandboxes=here))
            page.updated.connect(self.sync_tabs)
            # The Board's Bridge parts offer the sessions that run here.
            page.updated.connect(lambda local: self.built("board") is not None and self.board.set_running(local))
            page.browser_opener = self.open_start_browser  # a start's browser: a tab of Terminals
            # The fleet: the Hosts page lists what the Sandboxes and Approvals pages last saw of every host; the
            # Approvals page asks only the hosts where something runs for questions.
            page.fleet_changed.connect(self.fleet_changed)
            page.started_on.connect(lambda host: self.approvals.wake_hosts([host]))
            page.notice.connect(lambda level, title, text: self.notify(title, text, level=level))
        elif key == "approvals":
            page.pending_changed.connect(self.questions_changed)
            sandboxes = self.built("sandboxes")
            if sandboxes is not None and getattr(sandboxes, "loaded", False):
                page.set_fleet(sandboxes.fleet())
                page.set_remote_starts(sandboxes.remote_running())
        elif key == "monitor":  # a run's right click: its terminal tab, its row on the Sessions page
            page.go_terminal = self.go_to_terminal
            page.go_session = self.go_to_session
        elif key == "hosts":
            # ... and leads to them filtered by a host; a host removed there leaves both at once.
            page.host_removed.connect(lambda name: self.sandboxes.forget_host(name))
            page.host_removed.connect(lambda name: self.approvals.forget_host(name))
            page.show_starts.connect(lambda host: (self.sandboxes.show_host(host), self.show_page("sandboxes")))
            page.show_questions.connect(lambda host: (self.approvals.show_host(host), self.show_page("approvals")))
            page.refresh_requested.connect(lambda: (self.sandboxes.refresh_remote(), self.approvals.poll_remote()))
            # Each host's storage check: its last result in the list, its setting beside it (storage_hosts.py)
            page.storage_used = lambda: self.state.get(storage_hosts.USED, [], list)
            page.set_storage(self.storage_hosts_checked())
            if late and getattr(self.sandboxes, "loaded", False):
                page.set_fleet(self.sandboxes.fleet())
            if late:
                self.show_asking()
        elif key == "kits":
            page.enable_kit.connect(self.enable_kit)
            page.inventory_loaded.connect(lambda *found: self.board.set_kit_inventory(*found))
            # How `sandbox-ai plugins claude` went (in the background, no terminal): a failure leads to the log.
            page.notice.connect(lambda level, title, text: self.notify(
                title, text, level=level,
                action=(tr("Show the log"), lambda: self.set_panel("log", True)) if level != "success" else None))
        elif key == "board":
            page.opened.connect(self.project_opened)
            page.images_wanted.connect(self.show_your_images)  # a start refused for a service's image
            # A Team part's "Open on the Teams page": that team on the board's host
            page.team_wanted.connect(lambda name, host: (self.show_page("team"), self.team.show_named(host, name)))
            # An MCP part's "The server's defaults…" (its menu): the server's Properties on the MCP page; "Save as the
            # server's defaults…" (its properties): the same with the board's values filled in
            page.server_properties.connect(lambda name, host: (self.show_page("mcp"),
                                                               self.mcp.properties(name, host)))
            page.server_defaults.connect(lambda name, host, values: (self.show_page("mcp"),
                                                                     self.mcp.properties(name, host, values)))
            page.open_request = self.choose_project_folder
            page.project_request = self.set_project  # a project folder dropped on the board opens in its tab
            page.inspector_request = lambda: self.reopen_panel("inspector")  # a part selected: its inspector
            page.close_request = self.close_inspector  # OK or Cancel in it: the properties close
            page.fit_request = self.fit_inspector  # its rows were made: big enough for them (centred when opened)
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
        self.storage_hosts_used(fleet)
        hosts = self.built("hosts")  # (one made later asks the Sessions page then)
        if hosts is not None:
            hosts.set_fleet(fleet)
        approvals = self.built("approvals")
        if approvals is not None:
            approvals.set_fleet(fleet)
            sandboxes = self.built("sandboxes")
            if sandboxes is not None:  # the hosts' running starts: Blocked and Allowed now of one of them
                approvals.set_remote_starts(sandboxes.remote_running())

    def show_project_in(self, key, path):
        """The current project PATH on page KEY (apply_project, and a page made later)."""
        page = self.built(key)
        if page is None:
            return
        if key == "board":
            page.activate(path)
        elif key == "connections":
            page.open_for_workspace(path)
        elif key == "launch":
            page.set_project(path, running=self.running_start(path))
        elif key in PROJECT_PAGES:
            page.set_project(path)

    def running_start(self, path):
        """(agent, launcher options, agent arguments) a session of project PATH was started with - the current tab's
        session, else one that runs in the project (this machine's, as its session.json says) - or None."""
        entry = self.tabs.entry(self.tabs.currentIndex()) if self.__dict__.get("tabs") is not None else None
        idents = [entry.get("id")] if entry and entry.get("kind") == "session" and \
            same_folder(entry.get("path"), path) else []
        sandboxes = self.built("sandboxes")
        for instance in (getattr(sandboxes, "instances", None) or {}).values():
            if instance.kind == "session" and instance.host == LOCAL and instance.state == "running" and \
                    same_folder(instance.project, path):
                idents.append(instance.ident)
        for ident in idents:
            start = self.sai.session_start(ident)
            if start is not None:
                return start
        return None

    def nav_changed(self, row):
        item = self.nav.item(row)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if key in VIEW_OF.values():
            # The Start page's row: the view asked for by its key (show_page), else the one chosen last.
            wanted, self.view_wanted = self.view_wanted, None
            key = wanted if wanted in START_VIEWS else self.start_view
        if key in self.nav_rows:
            applog.debug("page", key=key)
            self.remember_page(key)
            if hasattr(self, "docks"):
                # the panels of the page left go first: shown beside the next page's minimum for that moment, they
                # would widen the window for good (Qt grows it at once; review 6 N1, the Board's two panels)
                self.put_away_docks(key)
            previous = self.current_page()
            page = getattr(self, key)
            self.host_follow.before_show(key)  # the Sessions page's host, while the page is still hidden
            if key in START_VIEWS and previous in board_follow.SOURCES:
                # From a session on Terminals or Sessions to the Board: its tab and project are the ones shown.
                board_follow.follow(self, previous)
            self.pages.setCurrentWidget(page)
            if hasattr(self, "docks"):
                self.page_changed(key)

    def current_page(self):
        widget = self.pages.currentWidget()
        return next((key for key in self.keys if self.built(key) is widget and widget is not None), None)

    def show_your_images(self):
        """The Images page's Your images (a start was refused for a service's image that is none of yours)."""
        self.show_page("images")
        page = self.built("images") or getattr(self, "images", None)
        if page is not None and hasattr(page, "show_your_images"):
            page.show_your_images()

    def show_page(self, key, raise_window=True, unfold=True):
        """Page KEY shown. A view of the Board page ("board", "launch": the Expert view) is that page in that view."""
        section = self.section_of(key)
        if unfold and section in self.collapsed and not self.compact_nav:
            self.toggle_section(section, False)  # a page reached another way shows where it is
        row = self.nav_rows[key]
        if key in START_VIEWS:
            self.view_wanted = key
            if self.nav.currentRow() == row:  # the page is shown: its other view (the row does not change)
                self.nav_changed(row)
        self.nav.setCurrentRow(row)
        self.view_wanted = None
        if not raise_window:
            return
        if not self.isVisible():  # hidden in the tray
            self.show()
        self.raise_()
        self.activateWindow()

    def show_start(self, raise_window=True):
        """The Board page in the view chosen last (its row of the navigation, Ctrl+1, Go > Start)."""
        return self.show_page(self.start_view, raise_window=raise_window)

    def choose_view(self, key, raise_window=False):
        """The human chose the view KEY of the Board page (the switch, its key, Ctrl+2, Show on the Board): shown, and
        the view the page opens in from now on (kept: start_view). A tour or a link that shows a view by its key
        (show_page) chooses nothing."""
        if key not in START_VIEWS:
            return
        self.show_page(key, raise_window=raise_window)
        if key != self.start_view:
            self.start_view = key
            self.state.set("start_view", key)

    def switch_view(self):
        """The other view of the Board page (the switch's key): Board <-> Expert. Nothing on another page."""
        current = self.current_page()
        if current in START_VIEWS:
            self.choose_view(next(view for view in START_VIEWS if view != current))

    def show_start_line(self, agent, argv, agent_args=(), host=None, raise_window=True):
        """The start AGENT ARGV -- AGENT_ARGS put onto the Board page (one step of its Undo: the parts that are there
        stay where they are) and the page shown in the view chosen last: for what leads to a start from elsewhere."""
        board = self.board
        if host and host != board.host():
            board.host_choice.choose(host)
        if self.start_link is not None:
            self.start_link.flush()
        board.take_start(agent, list(argv), list(agent_args))
        self.show_start(raise_window=raise_window)

    # -- Back and Forward: the mouse's side buttons, Alt+Left and Alt+Right, Go > Back and Forward --

    def page_place(self, key):
        """Where on page KEY the human is, as far as Back and Forward come back to it: the Help page's section."""
        if key == "help":
            help_page = self.built("help")
            return getattr(help_page, "shown", None)
        return None

    def remember_page(self, key):
        """Page KEY is shown now (or another section of it): a step of the history, unless Back or Forward show it.
        What was ahead of the current step (Forward) goes, as in a browser."""
        if self.history_moving:
            return
        entry = (key, self.page_place(key))
        history = self.page_history
        if 0 <= self.history_at < len(history) and history[self.history_at] == entry:
            return
        del history[self.history_at + 1:]
        history.append(entry)
        del history[:-HISTORY_KEPT]
        self.history_at = len(history) - 1
        self.update_history_actions()

    def help_section_shown(self, _ident):
        if self.current_page() == "help":
            self.remember_page("help")

    def can_go(self, step):
        return 0 <= self.history_at + step < len(self.page_history)

    def go_history(self, step):
        """Back (STEP -1) or Forward (+1): the page shown before or after the current one, at the Help section it
        showed; False when there is none."""
        if not self.can_go(step):
            return False
        target = self.history_at + step
        key, place = self.page_history[target]
        self.history_at = target
        self.history_moving = True
        try:
            if key in self.nav_rows:
                self.show_page(key, raise_window=False)
            if key == "help" and place:
                self.help.show_section(place)
        finally:
            self.history_moving = False
        self.update_history_actions()
        return True

    def go_back(self):
        return self.go_history(-1)

    def go_forward(self):
        return self.go_history(1)

    def update_history_actions(self):
        actions = self.__dict__.get("actions") or {}
        for name, step in (("back", -1), ("forward", 1)):
            if name in actions:
                actions[name].setEnabled(self.can_go(step))

    def side_button(self, button):
        """A side button of the mouse over the window: back, forward, or (the next one) the settings. True if it is
        one of them."""
        if button == Qt.MouseButton.BackButton:
            self.go_back()
        elif button == Qt.MouseButton.ForwardButton:
            self.go_forward()
        elif button == Qt.MouseButton.TaskButton:
            # (after the press is handled: the dialog's own loop never runs inside the window's input)
            QTimer.singleShot(0, lambda: alive(self) and not self.finished and self.open_settings())
        else:
            return False
        return True

    def mousePressEvent(self, event):  # noqa: N802
        """What no widget took (a press on a label, a button, a free place): the side buttons too. Most presses of the
        side buttons are taken before any widget sees them (SideButtons: lists and tables would keep them)."""
        if self.side_button(event.button()):
            event.accept()
            return
        super().mousePressEvent(event)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.side_buttons.watch(self.windowHandle())

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
        # The emergency stop, always in sight (File menu and EMERGENCY_KEYS too).
        self.emergency_button = QPushButton(tr("Emergency stop"))
        self.emergency_button.setProperty("role", "danger")
        self.emergency_button.setAccessibleName(tr("Emergency stop: cut every sandbox's network"))
        self.emergency_button.setToolTip(safe_tooltip(tr(EMERGENCY_HELP) + f" ({EMERGENCY_KEYS})"))
        self.emergency_button.clicked.connect(lambda _=False: self.emergency_cut())
        # The app's browser (File > Browser too).
        self.browser_button = QToolButton()
        self.browser_button.setObjectName("windowControl")
        self.browser_button.setAutoRaise(True)
        self.browser_button.setIconSize(QSize(18, 18))
        self.browser_button.setToolTip(tr("Browser (Ctrl+Shift+O): sign in to a cloud subscription in the app"))
        self.browser_button.setAccessibleName(tr("Browser"))
        self.browser_button.clicked.connect(lambda _=False: self.open_browser())
        self.project_bar.add_controls([self.browser_button, self.emergency_button], self.window_buttons.values())

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
        action(file_menu, "save-board", tr("Save the board"), lambda: self.board.save_board())
        action(file_menu, "save-preset", tr("Save the board as a preset…"), lambda: self.board.save_as_preset())
        self.switch_menu = file_menu.addMenu(tr("Switch project"))
        self.switch_menu.aboutToShow.connect(self.fill_switch_menu)
        file_menu.addSeparator()
        # The app's browser: Chromium with the login browser's profile (a sign-in at a cloud subscription stays).
        browser = action(file_menu, "browser", tr("Browser"), lambda: self.open_browser(), ["Ctrl+Shift+O"])
        browser.setToolTip(safe_tooltip(tr("A browser in the app (Chromium): sign in to a cloud subscription, read "
                                           "a provider's page; never this machine or the local network")))
        file_menu.addSeparator()
        action(file_menu, "close-tab", tr("Close tab"), self.close_current_tab, ["Ctrl+W"])
        file_menu.addSeparator()
        # The emergency stop: every sandbox's network cut at once, here and on every host; no question (it must be
        # instant, and it only takes away). Also the header's button.
        emergency = action(file_menu, "emergency-cut", tr("Emergency stop: cut every sandbox's network"),
                           self.emergency_cut, [EMERGENCY_KEYS])
        emergency.setToolTip(safe_tooltip(tr(EMERGENCY_HELP)))
        # From every window of the app (the log, the questions, an editor), as Qt's own shortcut: no event filter.
        emergency.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        widgets.EMERGENCY_ACTION = emergency  # also added to the dialogs: Qt takes none in a window a modal one blocks
        action(file_menu, "lift-cut", tr("Lift every cut"), self.lift_every_cut)
        file_menu.setToolTipsVisible(True)
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
        action(view, "log-window", tr("Log in its own window"), self.open_log_window, [widgets.LOG_WINDOW_KEYS])
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
        # Install: what starts need - images, MCP servers, toolchains, what presets need - all, nothing or a part, on
        # this machine or a host (install_view: a checklist; sandbox-ai install in a terminal tab).
        from . import install_view
        install_menu = bar.addMenu(tr("&Install"))
        install_menu.setToolTipsVisible(True)
        for key, text, kind, every in install_view.MENU:
            if key == "everything":
                install_menu.addSeparator()
            item = action(install_menu, f"install:{key}", tr(text),
                          lambda k=kind, e=every: install_view.open_dialog(self, k, e))
            if key in install_view.MENU_TIPS:
                item.setToolTip(safe_tooltip(tr(install_view.MENU_TIPS[key])))
        go = bar.addMenu(tr("&Go"))
        self.shortcuts = []
        for number, (key, title) in enumerate(PAGES, 1):
            # Board (Ctrl+1): the page in the view chosen last; Board: Board view (Ctrl+2, as ever): in its Board view.
            text = tr(VIEW_TITLES[key]) if key in VIEW_OF else tr(title)
            item = action(go, f"page:{key}", text,
                          (lambda: self.show_start()) if key in VIEW_OF.values() else
                          (lambda k=key: self.choose_view(k, raise_window=True)) if key in VIEW_OF else
                          (lambda k=key: self.show_page(k)),
                          [f"Ctrl+{number}"] if number <= SHORTCUT_PAGES else ())
            item.setIcon(theme.glyph(key, theme.colors()["text"], 16))
            if number <= SHORTCUT_PAGES:
                self.shortcuts.append(item)
            if key in VIEW_OF:  # right after the page's views: the switch between them (on that page only)
                switch = action(go, "switch-view", tr("Board: switch between the Board and Expert views"),
                                self.switch_view)
                switch.setToolTip(safe_tooltip(tr("The other view of the Board page: Board (parts and connections) "
                                                  "or Expert (fields and the command line); both show one start")))
        go.addSeparator()
        # The platform's keys (Alt+Left and Alt+Right; Cmd+[ and Cmd+] on macOS), and the mouse's side buttons.
        for name, text, slot, standard, fallback in (
                ("back", tr("Back"), self.go_back, QKeySequence.StandardKey.Back, "Alt+Left"),
                ("forward", tr("Forward"), self.go_forward, QKeySequence.StandardKey.Forward, "Alt+Right")):
            item = action(go, name, text, slot)
            keys = [key for key in QKeySequence.keyBindings(standard) if not key.isEmpty()] or [QKeySequence(fallback)]
            item.setShortcuts(keys)
            item.setToolTip(safe_tooltip(tr("{what} ({keys}, or the mouse's side button)", what=text,
                                            keys=keys[0].toString(QKeySequence.SequenceFormat.NativeText))))
        self.update_history_actions()
        go.setToolTipsVisible(True)
        go.addSeparator()
        action(go, "next-tab", tr("Next tab"), lambda: self.next_tab(1), ["Ctrl+Tab"])
        action(go, "previous-tab", tr("Previous tab"), lambda: self.next_tab(-1), ["Ctrl+Shift+Tab", "Ctrl+Backtab"])
        # Help: the topics, then the tours and the videos one submenu each (the user 2026-11-24: x times Tour, Help
        # ... in 1-2 entries or submenus); the keys stay (an entry of a submenu takes its key as any other).
        help_menu = bar.addMenu(tr("&Help"))
        action(help_menu, "help", tr("Help: every topic, in English and German"), lambda: self.show_help(),
               ["Shift+F1"])
        tours_menu = help_menu.addMenu(tr("Tours"))
        action(tours_menu, "area-tour", tr("Tour of this page"), lambda: self.start_area_tour(self.current_page()),
               ["F1"])
        action(tours_menu, "tour", tr("Guided tour"), lambda: self.start_tour())
        action(tours_menu, "tour-about", tr("Tour about…"), lambda: self.tour_about(), ["Ctrl+F1"])
        tours_menu.addSeparator()
        areas = tours_menu.addMenu(tr("Area tours"))
        from . import tours
        for key, title in PAGES:
            if key in tours.AREA_TOURS:
                item = action(areas, f"area-tour:{key}", tr(VIEW_TITLES.get(key, title)),
                              lambda k=key: self.start_area_tour(k))
                item.setIcon(theme.glyph(key, theme.colors()["text"], 16))
        tasks = tours_menu.addMenu(tr("Tours through a task"))
        for key, tour in tours.TASK_TOURS.items():
            action(tasks, f"task-tour:{key}", tours.say(tour.title), lambda k=key: self.start_task_tour(k))
        videos_menu = help_menu.addMenu(tr("Videos"))
        action(videos_menu, "page-video", tr("Watch the video about this page"),
               lambda: self.watch_video(self.current_page()), ["Shift+F2"])
        action(videos_menu, "videos", tr("Videos about the pages"), lambda: self.show_help(help_view.VIDEOS))
        action(videos_menu, "intro", tr("Watch the intro again"), self.play_intro)
        help_menu.addSeparator()
        action(help_menu, "check-updates", tr("Check for updates"), self.check_updates)
        action(help_menu, "self-update", tr("Update sandbox-ai…"), self.update_now).setEnabled(False)
        help_menu.addSeparator()
        action(help_menu, "log", tr("Show the log"), self.show_log_file)
        action(help_menu, "readme", tr("Read the README"), self.readme)
        action(help_menu, "about", tr("About sandbox-ai-app"), self.about)
        self.terminal_keys = {key: self.actions[key].shortcuts() for key in ("close-tab", "next-tab", "previous-tab",
                                                                         "back", "forward")}

    # -- the kill switch for every sandbox (sandbox-ai net cut --all-runs) --

    def emergency_cut(self):
        """The emergency stop (File menu, EMERGENCY_KEYS from every window of the app, the header's button): every
        running sandbox's network cut at once, the model API too, here and on every host of `sandbox-ai hosts` (read
        afresh); no question - it must be instant, and it only takes away. A notification says what was cut, which
        host or sandbox was not reached, and how to lift it."""
        # A "Lift every cut?" still open was asked before this stop: answered afterwards it would lift it at once.
        question, self.lift_question = self.lift_question, None
        if question is not None and alive(question):
            question.close()
        self.request_cut("all")

    def lift_every_cut(self):
        """File › Lift every cut: every sandbox reaches again what it may reach (sandbox-ai net cut --all-runs off), also
        what was cut on purpose one by one - so asked first, in a question that blocks nothing."""
        text = tr("Lift every cut, on this machine and on every host? Every sandbox reaches again what it may reach, "
                  "also those you cut one by one.")
        if widgets.AUTO_ANSWER is not None:  # the tests' answer
            widgets.SHOWN.append((tr("Lift every cut"), text, None))
            if widgets.AUTO_ANSWER:
                self.request_cut("off")
            return
        open_already = self.lift_question
        if open_already is not None and alive(open_already) and open_already.isVisible():
            open_already.raise_()  # one at a time: an emergency stop closes the one open (a second stayed open)
            open_already.activateWindow()
            return
        box = QMessageBox(QMessageBox.Icon.Warning, tr("Lift every cut"), text, QMessageBox.StandardButton.NoButton,
                          self)
        lift = box.addButton(tr("Lift every cut"), QMessageBox.ButtonRole.AcceptRole)
        lift.setProperty("role", "danger")
        keep = box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lift.clicked.connect(lambda _=False: self.request_cut("off"))
        # show(), never open(): open() makes it window-modal, and the window it blocked took neither Ctrl+Shift+X nor
        # the header's Emergency stop while the question was open.
        box.show()  # nothing waits for it: an emergency stop meanwhile goes ahead at once
        self.lift_question = box

    def request_cut(self, mode):
        """MODE ("all": the emergency stop, "off": lift every cut) for every sandbox. In the order they were asked for,
        so the later one holds in the end: an emergency stop goes at once, also while a lift is on its way, and runs
        once more after it (that lift may reach a host after it); a lift waits for what is on its way."""
        self.cut_serial += 1
        job = {"serial": self.cut_serial, "mode": mode, "quiet": False}
        # The Sessions page's own cuts on their way would land after this one: the later choice holds there too.
        if mode == "all":
            self.sandboxes.emergency_follows(tr("Emergency stop: every sandbox's network is cut"))
        else:
            self.sandboxes.lift_follows()
        if not self.cut_jobs:
            self.start_every_cut(job)
        elif mode == "all":
            lifting = any(running["mode"] == "off" for running in self.cut_jobs)
            self.start_every_cut(job)
            self.cut_next = {**job, "quiet": True} if lifting else None  # no lift chosen before it goes after it
        else:
            self.cut_next = job

    def start_every_cut(self, job):
        page = self.sandboxes
        mode = job["mode"]
        self.cut_jobs.append(job)
        title = tr("Emergency stop: every sandbox's network is cut") if mode == "all" else tr("Every cut lifted")

        def where(name, host):
            return printable(name) if host == LOCAL else tr("{name} on {host}", name=printable(name),
                                                            host=printable(host))

        def done(result, error):
            self.cut_jobs.remove(job)
            if not self.cut_jobs and self.cut_next is not None:
                following, self.cut_next = self.cut_next, None
                self.start_every_cut(following)
            if error is not None:
                self.notify(title, tr("It could not be done: {error}", error=printable_text(error)[:300]), level="error")
                return
            reached, failures = [], []
            for host, outcome in sorted(result.items(), key=lambda item: item[0] != LOCAL):
                report = backend.cut_report(outcome)
                if report is None:
                    said = [line for line in (str(outcome[2] or "") + str(outcome[1] or "")).splitlines() if line.strip()]
                    failures.append(f"{tr('this machine') if host == LOCAL else printable(host)}: "
                                    f"{printable_text(said[-1] if said else outcome[0])[:200]}")
                    continue
                for entry in report:
                    if entry["ok"]:
                        reached.append(where(entry["name"], host))
                    else:
                        failures.append(f"{where(entry['name'], host)}: {printable(entry['error'])[:200]}")
            if job["quiet"] and not failures:
                return  # the emergency stop once more, after a lift that was on its way: said already
            if mode == "all":
                text = tr("Cut: {names}.", names=", ".join(reached)) if reached else tr("No sandbox runs.")
            else:
                text = tr("Lifted: {names}.", names=", ".join(reached)) if reached else tr("No sandbox runs.")
            if failures:
                text += "\n" + (tr("Not cut:") if mode == "all" else tr("Not lifted:")) + " " + "; ".join(failures)
            if mode == "all":
                text += "\n" + tr("To lift it: File › Lift every cut, Kill switch › Lift the cut on the Sessions page, "
                                  "or sandbox-ai net cut --all-runs off.")
            self.notify(title, text, level="error" if failures else "warning" if mode == "all" else "success",
                        action=(tr("Show the sandboxes"), lambda: (self.show_page("sandboxes"),
                                                                   self.sandboxes.show_sessions())))
            page.refresh_soon(again=True)
            page.refresh_remote()
        background(self.sai.cut_every_host, mode, owner=page, work=True, timeout=backend.CUT_WAIT + 90, done=done)

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
            text = tr("No log yet: {error}", error=printable_text(str(exc)))
        show_text(self, tr("Log: {path}", path=printable(str(path))),
                  (tr("Debug mode is on.") if applog.debugging() else
                   tr("Debug mode is off (Settings > Window): only errors and warnings are logged.")) + "\n\n" + text)

    def start_area_tour(self, page):
        """The tour of PAGE's area (tours.py) in the guide's panel: one step per control, outlined on the page."""
        from . import tours
        from .guide_view import AreaTourPanel
        if page not in tours.AREA_TOURS:
            return
        panel = AreaTourPanel(page)
        panel.page_requested.connect(lambda key: self.show_page(key))
        panel.target_requested.connect(self.point_at)
        panel.closed.connect(lambda: self.tour_dock.hide())
        if getattr(self, "tour", None) is not None:
            self.tour.hide()  # the overview tour gives way; Help > Guided tour brings it back
        self.area_tour = panel
        self.put_in_tour_dock(panel)
        self.tour_dock.show()
        self.tour_dock.raise_()
        panel.start()

    def show_help(self, section=None):
        """The Help page, at SECTION (a help_content id) when given (first: Back leads to the page shown before)."""
        if section:
            self.help.show_section(section)
        self.show_page("help")
        return self.help

    @contextlib.contextmanager
    def showing(self, key):
        """For a picture of page KEY (the Help's screenshots): the page and its panels in the window's workspace for
        the moment, as the navigation shows them - without the navigation (no history, nothing remembered) - and
        the page shown before back right after. Nothing is painted on screen in between (no event loop runs)."""
        before = self.current_page()
        page = getattr(self, key)
        if before == key:
            yield page
            return
        old = self.built(before) if before else None
        if hasattr(self, "docks"):
            self.put_away_docks(key)
        self.pages.setCurrentWidget(page)
        if hasattr(self, "docks"):
            self.update_docks()
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        try:
            yield page
        finally:
            if hasattr(self, "docks"):
                self.put_away_docks(before)
            if old is not None:
                self.pages.setCurrentWidget(old)
            if hasattr(self, "docks"):
                self.update_docks()
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)

    def show_me(self, page, target):
        """The Help's "Show me": page PAGE with its control TARGET outlined - the attribute its tours outline - for a
        few seconds (a tour's own outline is left alone)."""
        from PySide6.QtCore import QTimer
        if page not in self.nav_rows:
            return None
        self.show_page(page)
        self.point_at(page, target)
        widget = getattr(getattr(self, page, None), target, None)
        spotlight = getattr(self, "spotlight", None)

        def done():
            try:
                if spotlight is not None and getattr(spotlight, "target", None) is widget:
                    spotlight.follow(None)
            except RuntimeError:
                pass
        QTimer.singleShot(SHOW_ME_SECONDS * 1000, done)
        return widget

    def show_page_video(self, key):
        """The button of the video about page KEY (beside its (?)): there for every page of the series."""
        from . import videos
        button = getattr(self, "page_video", None)
        if button is None:
            return
        known = key in videos.PAGE_TITLES
        button.setVisible(known)
        if known:
            text = tr("Watch the video about this page: {page}", page=videos.page_title(key))
            button.setToolTip(widgets.safe_tooltip(text))
            button.setAccessibleName(text)

    def watch_video(self, page=None):
        """The video about PAGE (None: the first of the series) in the app's player; when the videos are not on this
        computer, the offer to download them once (video_player.py); an error on the way is said."""
        from . import video_player
        return video_player.watch(page, sai=self.sai, parent=self)

    def open_start_browser(self, instance):
        """The sandbox's own browser of INSTANCE (SandboxesPage.open_browser checked it): a tab of Terminals right
        beside its terminal, shown at once (or its window of its own, raised)."""
        terminals = self.terminals
        panel = terminals.browsers.open(instance)
        if terminals.browsers.frame_of(panel) is None:
            self.show_page("terminals")
            terminals.tabs.setCurrentWidget(panel)
            panel.view.setFocus()
        self.statusBar().showMessage(tr("The browser of {name} is open in Terminals",
                                        name=printable(getattr(instance, "name", "") or "?")), 8000)
        return panel

    def open_browser(self, url=None):
        """The app's browser (web_browser.py: Chromium with the login browser's profile and rules), at URL."""
        from . import web_browser
        return web_browser.open_browser(url, parent=self)

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
        panel = TaskTourPanel(ident, self.tour_check)
        panel.page_requested.connect(lambda key: self.show_page(key))
        panel.target_requested.connect(self.point_at)
        panel.closed.connect(lambda: self.tour_dock.hide())
        if getattr(self, "tour", None) is not None:
            self.tour.hide()
        self.task_tour = panel
        self.put_in_tour_dock(panel)
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
        reveal = getattr(getattr(self, page, None), "reveal", None)
        if callable(reveal):  # a control the page has folded away (Terminals: Sessions) opens
            reveal(target)
        child = widget
        while child is not None and child is not self:
            parent = child.parentWidget()
            stack = parent.parentWidget() if parent is not None else None
            if isinstance(stack, QTabWidget) and stack.indexOf(child) >= 0:
                stack.setCurrentWidget(child)
            child = parent
        self.spotlight.follow(widget)

    def put_in_tour_dock(self, panel):
        """PANEL (a tour) in the guide's dock, made with the first tour. The tour it replaces goes (Qt's setWidget only
        hides it: every tour started stayed, a task tour still checking its step twice a second) - the guided tour
        stays, hidden, for Help > Guided tour."""
        dock = getattr(self, "tour_dock", None)
        if dock is None:
            dock = self.tour_dock = QDockWidget(tr("Guide"), self.workspace)
            dock.setObjectName("dock-tour")
            self.workspace.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        old = dock.widget()
        if old is panel:
            return
        dock.setWidget(panel)
        if old is not None and old is not getattr(self, "tour", None):
            timer = getattr(old, "timer", None)
            if timer is not None:
                timer.stop()
            old.deleteLater()

    def play_intro(self):
        """The intro once more (Help menu, Settings), skippable as at the start; it changes no setting."""
        from . import intro
        if getattr(self, "intro", None) is None or self.intro.over:
            self.intro = intro.play(self, self.sai.settings(), force=True, state=self.state)  # (the next one)
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
        elif index is not None:
            self.tour.go(index)
        self.put_in_tour_dock(self.tour)
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
        self.show_view_switch(key)
        if key in help_view.H.PAGE_SECTIONS and hasattr(self, "page_help"):
            self.page_help.set_tip(f"page.{key}")
        self.show_page_video(key)
        if hasattr(self, "help_buttons") and isinstance(getattr(self, key, None), QWidget):
            help_view.explain_tree(getattr(self, key))  # what the page made since (it fills some parts later)
        for name, keys in getattr(self, "terminal_keys", {}).items():
            self.actions[name].setShortcuts([] if key == "terminals" else keys)
        if key == "terminals" and self.built("terminals") is not None:
            self.terminals.focus_current()  # what one types goes to the selected terminal at once

    def show_view_switch(self, key):
        """The Board page's switch (Board | Expert) on that page only, the view shown down. What was typed in the
        Expert view is on the board before the Board is looked at; the switch's key works on that page only (F6 is
        the Files page's own elsewhere)."""
        on_start = key in START_VIEWS
        if getattr(self, "view_switch", None) is not None:
            self.view_switch.setVisible(on_start)
            if on_start:
                self.view_switch.set_view(key)
        if "switch-view" in getattr(self, "actions", {}):
            self.actions["switch-view"].setShortcuts([QKeySequence(start_link.SWITCH_KEYS)] if on_start else [])
            self.actions["switch-view"].setEnabled(on_start)
        if on_start and self.start_link is not None:
            self.start_link.flush()

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
        if getattr(self, "browser_button", None) is not None:
            self.browser_button.setIcon(self.window_icon("browser"))
        if getattr(self, "page_video", None) is not None:
            self.page_video.setIcon(theme.glyph("video", theme.colors()["accent"], 18))
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
        from . import reading_fonts
        from . import __version__
        inform(self, tr("About sandbox-ai-app"), tr(
            "sandbox-ai-app {app} from {app_path}\nsandbox-ai {sai} from {path}\nPySide6 {pyside}, Qt {qt}\n"
            "Python {python}", app=__version__, app_path=printable(backend.PACKAGE_DIR), sai=self.sai.version(),
            path=printable(self.sai.directory), pyside=PySide6.__version__, qt=qVersion(),
            python=platform.python_version()) + "\n\n" + tr(
            "Fonts that come with the app: {fonts} (SIL Open Font License 1.1; the licences are in {folder})",
            fonts="Atkinson Hyperlegible Next, Atkinson Hyperlegible Mono, OpenDyslexic, Lexend",
            folder=printable(str(reading_fonts.FOLDER))))

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
        Board page's form per agent, the Board's drafts and zoom, the Sessions page's selection, the log folders,
        the window itself: all come back at the next start (a page made later: register_page_state, then)."""
        s = self.state
        for key in BUILD_ORDER:
            if self.built(key) is not None:
                self.register_page_state(key)
        for section in s.get("nav_collapsed", []):
            if isinstance(section, str):
                section = {"Insight": "Monitoring"}.get(section, section)  # (the section's name before 0.1.95)
            if isinstance(section, str) and section in self.section_rows:
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
        if key == "images":  # the results of the package search under each list of the extension (per kind)
            kept += [(f"images-found-{kind}", plist.browser.results) for kind, plist in page.lists.items()]
        for name, widget in kept:
            s.table(name, widget)
        from . import tables as fitted
        fitted.keep_tables(s, {key: page})  # every other table, as PAGE-NAME
        if key == "files":
            page.workspace_request = self.add_workspace_from
            saved = s.section("splitters").get("files")
            page.keep_split = isinstance(saved, list) and len(saved) == page.split.count()  # kept: it wins
        splitters = {"connections": [("connections", "split")],
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
            s.tab("sandboxes", page.tabs)  # its tab (sessions or schedules), as it was left
        elif key == "approvals" and hasattr(page, "keep_details"):
            page.keep_details(s)  # its Details opened or folded, as it was left
        elif key == "terminals" and getattr(page, "starts", None) is not None:
            s.check("terminals-sessions", page.sessions_fold)  # folded or opened, as it was left
        if key == "terminals" and hasattr(page, "reopen"):
            # The terminal tabs of the starts that ran on when the app ended: attached again (never started).
            page.store = s
            page.sessions_page = lambda: self.built("sandboxes")
            page.focus_start = self.show_instance
            page.files_request = self.show_files_for
            page.shell_request = lambda instance: self.sandboxes.open_shell(instance)
            page.workspace_request = self.add_folder_to
            page.run_menu_request = self.run_menu_for
            page.board_request = self.show_on_board  # a tab's "Show on the Board"
            QTimer.singleShot(0, page.reopen)
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
        view = self.state.get("start_view", "")
        if view in START_VIEWS:
            self.start_view = view  # the Board page's view chosen last: the one it opens in
        page = self.state.get("page", "")
        if page in START_VIEWS:
            page = self.start_view  # (one page: left on it, it comes back in the view chosen last)
        elif page not in self.nav_rows:
            page = self.start_view if self.current_page() in START_VIEWS else ""
        if page in self.nav_rows:
            self.show_page(page, raise_window=False, unfold=False)  # sections stay as they were left
        self.update_window_controls()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Paint and self.__dict__.get("idle_build", 0) is None and self.lazy \
                and self.ready:
            # The window's first paint (the navigation is part of it): the other pages are made from a moment on, the
            # first pages' reads start right after it.
            self.idle_build = False
            QTimer.singleShot(0, widgets.release_background)
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
        starts of the Sessions page, which keeps it), which collapse to their top level or open all the way. Said in
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
            if key in VIEW_OF:
                continue  # (a view of another page: that page's row)
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
        lines = [title, *textwrap.wrap(about, 60)]
        for view, page in VIEW_OF.items():  # the page's other view (the Board, of Start): its key and what it is
            if page == key:
                number = self.keys.index(view) + 1
                name = start_link.view_title(view)
                lines.append(name if self.compact_nav or number > SHORTCUT_PAGES else
                             tr("{page} (Ctrl+{number})", page=name, number=number))
                if f"page.{view}" in help_view.H.TIPS:
                    lines += textwrap.wrap(help_view.summary(f"page.{view}"), 60)
        return "\n".join(lines)

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
        widgets.exec_dropped(menu, self.nav.viewport().mapToGlobal(position))

    # -- panels (docks) --

    def make_docks(self):
        """The Board's parts, its inspector and findings, and the log, as panels of the page area: they can be floated
        (a floating one stays on every page), docked elsewhere and closed."""
        from . import logview
        self.log_panel = logview.LogPanel(self.state.get("log_level", logview.DEFAULT_LEVEL)
                                          if isinstance(self.state.get("log_level"), str) else logview.DEFAULT_LEVEL,
                                          detachable=True)
        self.log_panel.own_window.connect(self.open_log_window)
        self.log_panel.level.currentIndexChanged.connect(
            lambda _i: self.state.set("log_level", self.log_panel.chosen()))
        widgets = {"parts": self.board.parts_panel, "inspector": self.board.side, "findings": self.board.findings_panel,
                   "log": self.log_panel}
        self.docks, self.closed_docks = {}, set(CLOSED_AT_FIRST)
        for name, title, _, area in DOCKS:
            dock = PanelDock(tr(title), self.workspace)
            dock.setObjectName(f"dock-{name}")
            # Floating, a panel is a window of its own: it paints its whole area (never what lay on the screen there).
            dock.setAutoFillBackground(True)
            if name in widgets:
                dock.setWidget(widgets[name])
                tip = widgets[name].toolTip()  # escaped when it was set (safe_tooltip)
                dock.setToolTip(tip)  # its title explains it on hover (the parts: how to add)
            dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                             | QDockWidget.DockWidgetFeature.DockWidgetMovable
                             | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
            self.workspace.addDockWidget(area, dock)
            dock.closed_by_user.connect(lambda n=name: self.panel_closed(n))
            escape_closes(dock)  # floating: Esc closes it
            # Undocked by its button or a double click: centred over the window, sized to its content (a drag: where
            # it was dropped, unless partly off the screen; where the user put it before: kept).
            dock.placement = dock_fold.FloatPlacement(dock, self)
            dock.topLevelChanged.connect(lambda _floating: self.docks_changed())
            dock.topLevelChanged.connect(lambda floating, d=dock: floating and self.settle_floating(d))
            # a floated panel is a window of its own: its side buttons too (its QWindow is there once it is shown)
            dock.topLevelChanged.connect(lambda floating, d=dock: floating and QTimer.singleShot(
                0, lambda: alive(d) and self.side_buttons.watch(d.windowHandle())))
            dock.dockLocationChanged.connect(lambda _area: self.docks_changed())
            self.docks[name] = dock
        self.board.palette.setMinimumWidth(150)
        self.board.side.setMinimumWidth(240)
        # The inspector: folded to a strip at first; a click on a part unfolds it (or shows it, closed), Esc or a
        # click on the empty canvas fold it again while it is docked.
        self.inspector_fold = dock_fold.DockFold(self.docks["inspector"], self.workspace, folded=True)
        self.inspector_fold.changed.connect(lambda _folded: self.docks_changed())
        self.board.fold_request = self.fold_inspector
        # The log window: folded to its title bar at first (it counts the warnings and errors that came meanwhile);
        # unfolded, it takes about a fifth of the height.
        self.log_fold = dock_fold.DockFold(self.docks["log"], self.workspace, folded=True, height=180)
        self.log_fold.changed.connect(lambda _folded: (self.log_watch(), self.docks_changed()))
        self.log_panel.unread.connect(self.log_unread)
        self.workspace.resizeDocks([self.docks["parts"]], [170], Qt.Orientation.Horizontal)
        self.default_docks = self.workspace.saveState()

    def open_log_window(self):
        """The log in a window of its own (logview.LogWindow): no dialog blocks it (they are window modal)."""
        from . import logview
        window = getattr(self, "log_window", None)
        if window is None:
            window = self.log_window = logview.LogWindow(self.log_panel.chosen())
            window.panel.level.currentIndexChanged.connect(
                lambda _i: self.state.set("log_level", window.panel.chosen()))
        window.bring_up()
        return window

    def settle_floating(self, dock, tries=20):
        """A panel floated out of the window ends up a window the window manager manages. While it is dragged Qt
        makes it bypass the window manager (X11BypassWindowManagerHint); on spectrwm the drag's end could get lost
        and the panel stayed so - a frameless window over every workspace, not to be moved or resized, painting
        nothing ("Board: Eigenschaften" over a copy of the screen). Once no mouse button is held, the hint goes."""
        def check(left=tries):
            try:
                if not dock.isFloating():
                    return
                if QApplication.mouseButtons() != Qt.MouseButton.NoButton and left > 0:
                    QTimer.singleShot(250, lambda: check(left - 1))  # still dragged
                    return
                if dock.windowFlags() & Qt.WindowType.X11BypassWindowManagerHint:
                    dock.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, False)
                    dock.show()
                    dock.raise_()
                placement = getattr(dock, "placement", None)
                if placement is not None:  # (a drop partly off the screen: brought back, centred)
                    placement.settled()
            except RuntimeError:  # gone meanwhile
                return
        QTimer.singleShot(250, check)

    def fold_inspector(self):
        """Esc or a click on the Board's empty canvas: its inspector folds if it is docked, shown and unfolded (never
        a floating one). True if it folded."""
        dock = self.docks["inspector"]
        if dock.isFloating() or not dock.isVisible() or self.inspector_fold.is_folded():
            return False
        self.inspector_fold.fold()
        return True

    def close_inspector(self):
        """OK or Cancel of the Board's properties: docked, they fold to their strip; floating, the panel closes (the
        next part selected opens it again where it was)."""
        dock = self.docks["inspector"]
        if not dock.isVisible():
            return
        if dock.isFloating():
            self.closed_docks.add("inspector")
            self.update_docks()
            self.save_docks()
        elif not self.inspector_fold.is_folded():
            self.inspector_fold.fold()

    def log_unread(self, count):
        """The log window's title names the warnings and errors that arrived while it was folded or hidden."""
        dock = self.docks["log"]
        title = tr("Log") if not count else tr("Log ({count} new)", count=count)
        dock.setWindowTitle(title)  # (the fold's title bar follows it)

    def log_watch(self):
        """Read while unfolded and shown: nothing counts as new then."""
        fold, dock = getattr(self, "log_fold", None), self.docks.get("log")
        if fold is not None and dock is not None:
            self.log_panel.set_watching(dock.isVisible() and not fold.is_folded())

    def put_away_docks(self, key):
        """Hide the docked (not floating) panels that are not of the page KEY, before it is shown."""
        for name, _, dock_page, _ in DOCKS:
            dock = self.docks[name]
            if dock_page is not None and dock_page != key and not dock.isFloating() and not dock.isHidden():
                dock.setVisible(False)

    def update_docks(self):
        page = self.current_page()
        for name, _, dock_page, _ in DOCKS:
            dock = self.docks[name]
            wanted = name not in self.closed_docks and (dock.isFloating() or dock_page is None or dock_page == page)
            if dock.isHidden() == wanted:
                dock.setVisible(wanted)
            if hasattr(self, "panel_actions"):
                self.panel_actions[name].setChecked(name not in self.closed_docks)
        self.board.score_button.setChecked("findings" not in self.closed_docks)
        self.log_watch()

    def panel_closed(self, name):
        page = dict((n, p) for n, _, p, _ in DOCKS)[name]
        if page is None or self.current_page() == page or self.docks[name].isFloating():
            self.closed_docks.add(name)
        self.update_docks()
        self.save_docks()

    def toggle_panel(self, name):
        """View > Panels: bring a closed panel back (on its page) or close it."""
        self.set_panel(name, self.panel_actions[name].isChecked())

    def set_panel(self, name, shown):
        """Show the panel NAME (on its page, unfolded) or close it. The log while a dialog blocks the window: in its
        own window (the panel could not be reached)."""
        page = dict((n, p) for n, _, p, _ in DOCKS)[name]
        if shown and name == "log" and QApplication.activeModalWidget() is not None:
            self.open_log_window()
            return
        if shown:
            self.closed_docks.discard(name)
            if name == "inspector":
                self.inspector_fold.unfold()
            if name == "log":
                self.log_fold.unfold()
            if page is not None and not self.docks[name].isFloating() and self.current_page() != page:
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
        if name == "inspector" and (name in self.closed_docks or self.inspector_fold.is_folded()):
            self.inspector_opened = True  # (fit_inspector: opened now - centred when it floats)
        if name in self.closed_docks:
            self.closed_docks.discard(name)
            dock.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)  # a floating one: not activated
            try:
                self.update_docks()
            finally:
                dock.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            self.save_docks()
            if focus is not None:  # also once the window system has activated a panel shown floating
                self.keep_focus(focus)
                if dock.isWindow() and dock.windowHandle() is not None:
                    KeepFocusOnActivation(dock.windowHandle(), focus, self.keep_focus)
        if name == "inspector":
            self.inspector_fold.unfold()
        if name == "log":
            self.log_fold.unfold()
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
        self.log_fold.folded = True
        self.workspace.restoreState(self.default_docks)
        self.inspector_fold.apply()
        self.log_fold.apply()
        self.update_docks()
        self.save_docks()

    def save_docks(self):
        if getattr(self, "restoring_docks", False):
            return
        fold = getattr(self, "inspector_fold", None)
        if fold is None:  # (still being made)
            return
        log = getattr(self, "log_fold", None)
        folded = (["inspector"] if fold.folded else []) + (["log"] if log is not None and log.folded else [])
        sizes = {"inspector": fold.sizes, **({"log": log.sizes} if log is not None else {})}
        self.state.set("docks", {"state": bytes(self.workspace.saveState().toBase64()).decode(),
                                 "closed": sorted(self.closed_docks), "known": sorted(self.docks),
                                 "folded": folded, "sizes": sizes})

    def restore_docks(self):
        """Where the panels were at the last start (QMainWindow.saveState, kept in the app's state file)."""
        saved = self.state.get("docks", {})
        state, closed = saved.get("state"), saved.get("closed")
        self.restoring_docks = True
        try:
            if isinstance(state, str) and len(state) < 65536:
                self.workspace.restoreState(QByteArray.fromBase64(state.encode()))
            # A panel restored floating is a window the window manager manages (settle_floating): one saved while
            # Qt's drag flag stuck came up on spectrwm unmanaged - over every workspace, not to be moved or resized,
            # painting nothing ("Board: Eigenschaften" over a copy of the screen).
            for dock in self.docks.values():
                if dock.isFloating():
                    self.settle_floating(dock)
            known = saved.get("known") if isinstance(saved.get("known"), list) else []
            known = [n for n in known if isinstance(n, str)]
            if isinstance(closed, list):
                self.closed_docks = {n for n in closed if isinstance(n, str) and n in self.docks} | \
                    (CLOSED_AT_FIRST - set(known))
            sizes = saved.get("sizes")
            for name, fold in (("inspector", self.inspector_fold), ("log", self.log_fold)):
                # (else folded, as at the first start; also the log window of a state from before it existed)
                if isinstance(saved.get("folded"), list) and name in known:
                    fold.folded = name in saved["folded"] and not self.docks[name].isFloating()
                if isinstance(sizes, dict) and isinstance(sizes.get(name), dict):
                    for key in ("width", "height"):
                        value = sizes[name].get(key)
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
        widgets.drop_after_exec(dialog)  # (made anew each time)
        return dialog

    def apply_setting(self, key):
        """What can change at once does (theme: by the dialog; polling intervals and the (?)s here)."""
        if key == "help_buttons":
            help_view.set_shown(backend.setting("help_buttons"))
            return
        if key == "explanations":  # the info texts of every page and dialog, at once (the dialog did it already)
            widgets.set_explanations_shown(backend.setting("explanations"))
            return
        if key == "short_notices":
            short_notices.set_enabled(backend.setting("short_notices"))
            return
        if key == "button_texts":  # the icon buttons' texts beside their icons, at once
            button_icons.set_texts_shown(backend.setting("button_texts"))
            return
        if key == "board_grid" and self.built("board") is not None:  # its dots drawn again at the new spacing
            self.board.view.resetCachedContent()
            self.board.view.viewport().update()
        if key == "show_flags":  # the options (--…) beside the fields' names: the Board page's form, the board's
            labels.set_flags_shown(backend.setting("show_flags"))
            for form in self.findChildren(OptionsForm):
                form.show_flags()
            self.board.inspect_timer.start()  # its inspector is made again
            return
        if key == "sandboxes_interval" and self.safeguard_alerts.timer.isActive():
            self.safeguard_alerts.timer.setInterval(backend.setting(key))
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
        """New project… of the Board (BoardPage.new_project): this machine's - the wizard; it ends on the board, whose
        folder then opens in a tab of its own (the board shown before is kept for its project) - or a host's."""
        self.show_page("board")
        return self.board.new_project(ask=False)

    def project_opened(self, directory):
        """The Board or the Board page opened, reviewed or wrote a project folder: its tab is the current one."""
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
        """Make PATH the current project: the Board page's workspace, the Board (the project's own, as it was
        left), Connections and Statistics (its logs), Files, Approvals and Sandboxes (its rows marked), Kits
        (what it uses). Kept for the next start."""
        if self.start_link is not None:
            self.start_link.flush()  # what was typed for the project shown until now is its start, not the next one's
        try:
            self.project = self.sai.remember_project(path)
        except (OSError, ValueError):  # a read-only or unreadable configuration: the project is used, not kept
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
        approvals = self.built("approvals")
        if approvals is not None and hasattr(approvals, "set_active_tab"):
            approvals.set_active_tab(entry)  # its Blocked and Allowed now follow the active tab
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
        if self.start_link is not None:
            self.start_link.flush()
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

        def last_of_its_project():
            return bool(path) and not [i for i in range(self.tabs.count()) if i != index
                                       and same_folder((self.tabs.entry(i) or {}).get("path"), path)]
        last = last_of_its_project()
        if last and self.board.dirty_for(path):
            if not ask_first(self, "tab-close", tr("Close tab"), tr("The board of {path} has changes that are not "
                                                                    "applied. Close the tab and drop them?",
                                                                    path=printable(path)),
                             tr("Close and drop them"), reject=tr("Keep the tab")):
                return False
            # The question blocks no window: the tabs may have been closed, opened or moved meanwhile. The tab is
            # found again by its key (by its old index another tab was closed, or none and the board dropped all
            # the same).
            index = self.tabs.find(entry.get("key"))
            if index < 0:
                return False
            last = last_of_its_project()
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
                    if entry["kind"] in ("session", "run"):
                        entry["restored"] = True  # (sync_tabs: ended meanwhile, it goes at once)
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
        """Session and sandbox tabs show their state; a session or sandbox that started since the last look opens a
        tab (setting auto_tabs). One that ran and ended normally loses its tab AUTO_CLOSE_MS later (a tab of an ended
        start makes no sense); one whose run failed (its exit status not 0) keeps it, marked as failed, until the
        human closes it; a tab kept from the last start of the app whose start has ended meanwhile goes at once.
        Before the Sessions page's first read (in the background) nothing is known: no tab counts as stopped yet."""
        loaded = getattr(self.sandboxes, "loaded", True)
        # (a read that failed lists nothing: no tab is taken for ended then - it would be closed under the human)
        trusted = loaded and not getattr(self.sandboxes, "problems", None)
        gone = []
        for index in range(self.tabs.count()):
            entry = self.tabs.entry(index)
            if entry and entry["kind"] in ("session", "run"):
                before = dict(entry)
                instance = instances.get(entry["key"])
                if instance is not None:
                    entry.update(instance_entry(instance))
                elif loaded:
                    entry["state"] = "stopped"
                if entry.get("state") in ACTIVE_TAB_STATES:
                    entry["seen_active"], entry["restored"] = True, False
                    entry.pop("closing", None)
                elif trusted and entry.get("state") in ("stopped", "ended"):
                    status = (getattr(instance, "record", None) or {}).get("status") if instance is not None else None
                    if isinstance(status, int) and not isinstance(status, bool) and status != 0:
                        entry["state"] = "failed"
                    elif entry.get("restored"):
                        gone.append(entry["key"])
                    elif entry.get("seen_active") and not entry.get("closing"):
                        entry["closing"] = True
                        QTimer.singleShot(AUTO_CLOSE_MS, lambda key=entry["key"]: self.close_ended_tab(key))
                if entry != before:  # (each tab's text set again laid out the whole row: seconds per look for
                    self.tabs.set_entry(index, entry)  # hundreds of tabs)
        terminals = self.built("terminals")
        if terminals is not None and hasattr(terminals, "browsers"):  # the browser tabs follow their starts too
            terminals.browsers.sync(instances, trusted)
        for key in gone:  # (kept from the last start, ended meanwhile: never restored)
            index = self.tabs.find(key)
            if index >= 0:
                self.tabs.removeTab(index)
        if gone:
            self.save_tabs()
        if not loaded:
            return
        running = {key for key, i in instances.items() if i.kind in ("session", "run") and i.state == "running"}
        seen = getattr(self, "seen_running", set())
        if open_new and seen is not None and backend.setting("auto_tabs"):  # (None: the first read, what ran already)
            # One tab per start: what belongs to another start (a member of a team below its lead, a helper, an MCP
            # server - start_tree.owners) opens none; Open terminal or the Sessions page reach it.
            owned = parts_of_starts(instances.values()) if running - seen else set()
            for key in sorted(running - seen):
                if key in owned:
                    continue
                if self.tabs.find(key) < 0 and not self.own_start(instances[key]):
                    self.open_tab(instance_entry(instances[key]), activate=False)
        self.seen_running = running

    def close_ended_tab(self, key):
        """The tab of KEY, whose start ended normally AUTO_CLOSE_MS ago, closed - unless it runs again, failed or was
        closed meanwhile. The focus stays where the human has it."""
        try:
            index = self.tabs.find(key)
        except RuntimeError:  # the window is gone
            return
        entry = self.tabs.entry(index) if index >= 0 else None
        if entry is None or not entry.get("closing") or entry.get("state") not in ("stopped", "ended"):
            return
        instance = self.sandboxes.instances.get(key)
        if instance is not None and instance.state in ACTIVE_TAB_STATES:  # runs again (resumed meanwhile)
            return
        focus = QApplication.focusWidget()
        self.close_tab(index)
        try:
            if focus is not None and focus.isVisible() and QApplication.focusWidget() is not focus:
                focus.setFocus()
        except RuntimeError:
            pass

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
                                     ("shell", tr("Shell"), state == "running"),
                                     ("pause", tr("Pause"), state == "running"),
                                     ("resume", tr("Resume"), state in ("paused", "stopped"))):
                found.append((action, text, ok and instance is not None,
                              lambda a=action: self.sandboxes.session(a, self.sandboxes.instances.get(entry["key"]))))
        if entry["kind"] in ("session", "run") and instance is not None and \
                (instance.record or {}).get("browser"):  # --browser: its live view (when not, the click says why)
            found.append(("browser", tr("Browser"), True,
                          lambda: self.sandboxes.open_browser(self.sandboxes.instances.get(entry["key"]))))
        if entry["kind"] in ("session", "run"):
            stoppable = state in ("running", "paused") and instance is not None and (
                entry["kind"] == "session" or self.sandboxes.stoppable)
            found.append(("stop", tr("Stop…"), stoppable,
                          lambda: self.sandboxes.stop(self.sandboxes.instances.get(entry["key"]))))
            record = instance.record if instance is not None and isinstance(instance.record, dict) else {}
            found.append(("files", tr("File manager"), bool(record.get("workspace") or getattr(instance, "project", "")),
                          lambda: self.show_files_for(self.sandboxes.instances.get(entry["key"]))))
            found.append(("show", tr("Show in Sessions"), True, lambda: self.show_instance(entry["key"])))
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
        widgets.exec_dropped(menu, self.tabs.mapToGlobal(position))

    def close_other_tabs(self, key):
        # By key: closing one may ask first (a board's unapplied changes), and the tabs may change meanwhile.
        others = [entry["key"] for entry in map(self.tabs.entry, range(self.tabs.count()))
                  if entry and entry["key"] != key]
        for other in reversed(others):
            index = self.tabs.find(other)
            if index >= 0:
                self.close_tab(index)

    def run_menu_for(self, parent, instance):
        """The "While it runs" submenu of a terminal tab: the Sessions page's run actions (its buttons, as they are
        enabled for INSTANCE, with their reasons) for INSTANCE's row, chosen there first (not shown: the page stays
        where it is). None when the page does not list the start."""
        sandboxes = self.sandboxes
        if not self.select_quietly(instance.key):
            return None
        menu = QMenu(tr("While it runs"), parent)
        menu.setToolTipsVisible(True)

        def add(into, button):
            text = getattr(button, "label", "") or button.accessibleName()  # (its full text: "Allow…")
            action = QAction(text, into)
            action.setEnabled(button.isEnabled())
            tip = button.toolTip()  # (the button's own, made with safe_tooltip: taken as it is)
            action.setToolTip(tip)
            action.triggered.connect(lambda _=False, b=button, key=instance.key:
                                     self.select_quietly(key) and b.click())
            into.addAction(action)
        for entry in sandboxes.run_menu_entries():  # (Add… and Remove… as submenus, as on the page)
            if isinstance(entry, widgets.MenuGroup):
                sub = QMenu(entry.title, menu)
                sub.setToolTipsVisible(True)
                for button in entry.entries:
                    add(sub, button)
                menu.addMenu(sub)
            else:
                add(menu, entry)
        return menu

    def select_quietly(self, key):
        """The start KEY's row selected on the Sessions page without showing the page (its host and Ended as
        show_instance sets them); its buttons follow the row. False when it is not listed."""
        page = self.sandboxes
        instance = page.instances.get(key)
        host = getattr(instance, "host", None)
        if host is not None and page.host_filter.currentData() not in (None, host):
            page.show_host(host)
        row = find_row(page.table, key)
        if row < 0 and instance is not None and instance.state in ENDED_STATES and not page.show_ended.isChecked():
            page.show_ended.setChecked(True)
            row = find_row(page.table, key)
        if row < 0:
            return False
        page.table.selectRow(row)
        page.update_buttons()
        return True

    def add_folder_to(self, instance):
        """A terminal tab's Add folder…: a folder (this machine's, its host's, another machine's over sshfs) for the
        running session INSTANCE - the Sessions page's Add workspace… for it."""
        target = self.sandboxes.target_of(instance)
        if target is None:
            inform(self, tr("Add folder"), tr("Only while the session runs."))
            return False
        self.sandboxes.add_workspace_for(*target)
        return True

    def add_workspace_from(self, host, path):
        """The Files page's Add to a running session…: PATH (a folder or file of HOST: this machine, a host of
        sandbox-ai, or an sshfs:// folder of another machine for this machine's sessions) for one of HOST's running
        sessions - chosen when several run - in the Add workspace dialog, filled in."""
        sandboxes = self.sandboxes
        targets = []
        for instance in sandboxes.instances.values():
            if getattr(instance, "host", "local") != host or instance.kind not in ("run", "session"):
                continue
            target = sandboxes.target_of(instance)
            if target is not None and instance.state == "running" and target not in targets:
                targets.append(target)
        if not targets:
            inform(self, tr("Add to a running session"), tr("No sandbox runs on this machine now.") if host == "local"
                   else tr("No sandbox runs on {host} now.", host=printable(host)))
            return False
        target = targets[0]
        if len(targets) > 1:
            from PySide6.QtWidgets import QInputDialog
            names = [printable(run) for _host, run in targets]
            chosen, ok = QInputDialog.getItem(self, tr("Add to a running session"), widgets.shown_as_text(tr(
                "Which session gets {path}?", path=printable(path))), names, 0, False)
            if not ok or chosen not in names:
                return False
            target = targets[names.index(chosen)]
        sandboxes.add_workspace_for(*target, path=path)
        return True

    def show_files_for(self, instance):
        """A tab's "File manager": the Files page on INSTANCE's folder (its workspace; a start on another host: that
        folder among the host's sandboxes, over SSH)."""
        if instance is None:
            return False
        record = instance.record if isinstance(instance.record, dict) else {}
        folder = record.get("workspace") or instance.project
        if not folder:
            return False
        self.show_page("files")
        page = self.built("files")
        return bool(page is not None and hasattr(page, "show_start")
                    and page.show_start(getattr(instance, "host", "local"), folder))

    def show_instance(self, key):
        """The start KEY's row on the Sessions page selected, scrolled to and focused: its host chosen there first
        when another one is shown, the project filter off, Ended checked for a start that ended. False when it is
        not listed there."""
        self.show_page("sandboxes")
        page = self.sandboxes
        instance = page.instances.get(key)
        host = getattr(instance, "host", None)
        if host is not None and page.host_filter.currentData() not in (None, host):
            page.show_host(host)  # (another host was shown there: the start's own now)
        page.only_project.setChecked(False)
        row = find_row(page.table, key)
        if row < 0 and not page.show_ended.isChecked():
            # A start that ended is listed with "Ended" only: asked to be shown, it is (Ended checked for it).
            if instance is None or instance.state in ENDED_STATES:
                page.show_ended.setChecked(True)
                row = find_row(page.table, key)
        if row < 0:
            return False
        page.table.selectRow(row)
        item = page.table.selected_item() if hasattr(page.table, "selected_item") else None
        if item is not None:
            page.table.scrollToItem(item)
        page.table.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    # -- the System monitor's right click on a run: Go to Terminals, Go to Sessions --

    def run_start(self, host, run, name):
        """The start of the Sessions page (an Instance) that the run RUN (its run id; NAME: its name) of HOST is: by
        its run, else by its name; None when it is not listed there (it ended, or that host was not read yet)."""
        starts = [i for i in self.sandboxes.instances.values()
                  if getattr(i, "host", LOCAL) == host and i.kind in ("session", "run")]
        for instance in starts:
            record = instance.record if isinstance(instance.record, dict) else {}
            if run and run in (instance.run, record.get("run")):
                return instance
        for instance in starts:
            if name and name in (instance.name, instance.session, getattr(instance, "ident", None)):
                return instance
        return None

    def not_listed(self, title, host, name):
        text = tr("{name} is not listed in Sessions: it has ended, or it is not read yet.",
                  name=printable(name))
        short_notices.short_notice(self, title, on_host(host, text), warning=True)

    def go_to_terminal(self, host, run, name):
        """The System monitor's "Go to Terminals" on the run RUN (NAME) of HOST: its terminal tab shown on the
        Terminals page - attached first, as the Sessions page's Attach in the app does (over SSH for a host), when
        no tab shows it yet; never a second tab for it. Why not, briefly, when it has no terminal to attach to. True
        when its tab is shown or being opened."""
        title = tr("Go to Terminals")
        instance = self.run_start(host, run, name)
        if instance is None:
            self.not_listed(title, host, name)
            return False
        terminals = self.terminals
        for view in terminals.running():  # a tab that shows it already: the one it was started in, or attached
            shown = terminals.instance_of(view)
            if shown is not None and shown.key == instance.key:
                self.show_page("terminals")
                terminals.tabs.setCurrentWidget(view)
                view.view.setFocus()
                return True
        why = attach_why_of(instance)
        if why:
            short_notices.short_notice(self, title, on_host(host, tr("{name} has no terminal to attach to: {why}",
                                                                     name=printable(instance.name), why=why)),
                                       warning=True)
            return False
        self.sandboxes.session("attach-app", instance)  # (its tab, if one is open already: shown - TerminalsPage.open)
        return True

    def go_to_session(self, host, run, name):
        """The System monitor's "Go to Sessions" on the run RUN (NAME) of HOST: its row on the Sessions page
        selected and scrolled to (that host chosen there first); not listed there: said briefly."""
        instance = self.run_start(host, run, name)
        if instance is None:
            self.show_page("sandboxes")
            page = self.sandboxes
            if page.host_filter.currentData() not in (None, host):
                page.show_host(host)
            self.not_listed(tr("Go to Sessions"), host, name)
            return False
        if not self.show_instance(instance.key):
            self.not_listed(tr("Go to Sessions"), host, name)
            return False
        return True

    def show_on_board(self, instance, quiet=False):
        """Show on the Board (Sessions, a terminal tab): the Board with the project of the session or sandbox INSTANCE -
        its tab opened or selected, its board shown, its Agent part selected (board_follow.show). QUIET: the Board
        following a session one comes from - nothing asked, nothing said, the page not changed."""
        return board_follow.show(self, instance, quiet=quiet)

    def fit_inspector(self, opened=False):
        """The Board's properties show a part's rows: big enough for all of them - floating: sized to what they need
        and, when the panel was just opened (OPENED), centred over the window; docked at a side: at least as wide as
        they need (at most dock_fold.DOCK_SHARE of the window's width). User 2026-11-24: "Property-Fenster der Items müssen immer groß
        genug gestartet werden, dass alles sichtbar ist, und immer auch zentriert"."""
        dock = self.docks.get("inspector") if hasattr(self, "docks") else None
        if dock is None or not dock.isVisible():
            return
        opened = opened or bool(self.__dict__.pop("inspector_opened", False))
        if dock.isFloating():
            placement = getattr(dock, "placement", None)
            if placement is not None:
                placement.place(force=True) if opened else placement.grow()
        elif not self.inspector_fold.is_folded():
            dock_fold.fit_docked(dock, self.workspace, self.inspector_fold)

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
        self.project_bar.show_project(project, *(known or (None, "")))
        self.update_activity()
        background(self.sai.project_state, project, timeout=STATE_WAIT,
                   done=lambda result, error: self.project_state_read(token, project, result, error))

    def project_state_read(self, token, project, result, error):
        state = result[0] if error is None else "unreadable"
        detail = (result[1] or "" if state == "unreadable" else "") if error is None else str(error)
        self.project_states = getattr(self, "project_states", {})
        self.project_states[project] = (state, detail)
        if token != self.state_token or project != self.project:
            return
        self.project_bar.show_project(project, state, detail)
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

    def show_credentials_of(self, host):
        """The Credentials page with HOST chosen (a start's Secrets and logins on HOST… of the Sessions page)."""
        page = self.credentials
        page.hosts.set_host(host)
        self.show_page("credentials")

    def show_reach_of(self, host, run):
        """Approvals › Inspection › Allowed now for the running start RUN of HOST: everything it may reach and why."""
        self.show_page("approvals")
        self.approvals.show_reach(host, run)

    def show_given_of(self, host, run):
        """The Credentials page's Given to sandboxes tab for HOST with the rows of the running start RUN selected:
        what it was given during its run (a Git login, an API key), each taken back with Revoke…."""
        self.credentials.show_given(host, run)
        self.show_page("credentials")

    def projects(self):
        """The "current project" folders for the file browser: the current project (else the folder the app
        was started in) and the Board page's first workspace when it is another one."""
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
        set_icon = getattr(getattr(self, "tray", None), "setIcon", None)
        if set_icon is not None and getattr(self, "tray_attention", None) != bool(count):
            self.tray_attention = bool(count)
            set_icon(theme.app_icon(attention=bool(count)))  # the shield, with an orange dot while questions wait

    def questions_changed(self, count, new):
        # Said every second (each scan of the questions): the entry and the tray only when the count changed - setting
        # an item's icon again (a new QIcon each time) re-laid out and repainted the navigation every second.
        self.close_settled_toasts()
        changed = count != getattr(self, "pending_count", None)
        self.pending_count = count
        if changed:
            self.show_question_count()
        if hasattr(self, "project_bar"):
            self.update_activity()
        self.show_asking()
        if new and backend.setting("notify"):
            from . import rich_notice
            from .pages import describe_rich, why_asked
            entries = [(entry[0], entry[1], entry[2] if len(entry) > 2 else None) for entry in new]
            # One notification per new question, each leading to its own question; a canary that went off first (an
            # alarm: an error toast stays until it is closed), anything else waits for an answer (a warning). A flood
            # of questions at once: the first NOTIFY_AT_ONCE apart, the rest counted in one more.
            entries.sort(key=lambda e: not (isinstance(e[1], dict) and e[1].get("kind") == "canary"))
            for run, question, key in entries[:NOTIFY_AT_ONCE]:
                canary = isinstance(question, dict) and question.get("kind") == "canary"
                # A tool call whose arguments the notification cannot show whole is allowed only where they are
                # seen whole (the Approvals page, the Questions window), as sandbox-ai's own notification does.
                whole = not isinstance(question, dict) or backend.question_details(question)[1]
                # At a glance: the start in bold, what it asks for (the address in the accent colour), why.
                asks = rich_notice.lines(describe_rich(question), rich_notice.recognise(why_asked(question), "why"))
                toast = self.notify(rich_notice.fmt(tr("sandbox-ai asks: {sandbox}"), sandbox=rich_notice.session(run)),
                                    asks,
                                    level="error" if canary else "warning",
                                    action=self.question_actions(key, canary, whole=whole))
                if toast is not None and key is not None:
                    self.question_toasts[key] = toast
                    toast.closed.connect(lambda _toast=None, k=key: self.question_toasts.pop(k, None))
            rest = entries[NOTIFY_AT_ONCE:]
            if rest:
                self.notify(tr("sandbox-ai asks: {count} more questions", count=len(rest)),
                            tr("They wait on the Approvals page."), level="warning",
                            action=(tr("Open approvals"), self.show_approvals_questions))

    def show_asking(self):
        """Which starts ask (marked on the Sessions page), and how many questions wait on each host (Hosts page): only
        the questions that still wait - the Approvals page also keeps those that timed out or were answered."""
        approvals = self.built("approvals")
        questions = getattr(approvals, "questions", {}) or {}
        keys = [key for key, entry in questions.items()
                if isinstance(entry[1], dict) and backend.question_state(entry[1]) == "open"]
        sandboxes, hosts = self.built("sandboxes"), self.built("hosts")
        if sandboxes is not None:
            sandboxes.set_asking({(host, run) for host, run, _ in keys})
        if hosts is not None:
            counts = {}
            for host, _, _ in keys:
                counts[host] = counts.get(host, 0) + 1
            hosts.set_questions(counts)

    @property
    def question_toasts(self):
        """{question key: its notification (a toast)} while the toast is shown."""
        if "_question_toasts" not in self.__dict__:
            self._question_toasts = {}
        return self._question_toasts

    def close_settled_toasts(self):
        """One answer anywhere settles a question everywhere: the toast of a question answered (here, on the
        Approvals page, in the Questions window, on the command line or the web) or ended closes."""
        approvals = self.built("approvals")
        if approvals is None or not self.question_toasts:
            return
        settled = getattr(approvals, "settled", lambda key: key not in approvals.questions)
        for key, toast in list(self.question_toasts.items()):
            if settled(key):
                self.question_toasts.pop(key, None)
                with contextlib.suppress(RuntimeError):  # deleted meanwhile
                    toast.close()

    def question_actions(self, key, canary=False, whole=True):
        """The buttons of a question's notification: answered right there, at once - the click is the answer: Allow
        once, Always allow (every same request of this run), Always, also for later sessions (remembered for the
        project), Block (for this run), Block always (remembered for the project; user 2026-11-17: no second window
        after a click - the Approvals page offers Undo), as on the Approvals page - and the way to its page. A scope
        the question cannot get there (its caps: the policy, the run's security mode, the kind) is left out here (a
        notification says no why; the page's button does). A canary's alarm only leads to its page. Not WHOLE (a
        tool call whose arguments the notification shows shortened): no Allow, which is given where they are seen
        in full."""
        from .pages import ALLOW_SCOPES
        open_page = (tr("Open approvals"), lambda k=key: self.open_question(k))
        if canary:
            return open_page
        approvals = getattr(self, "approvals", None)
        entry = approvals.questions.get(key) if approvals is not None and key is not None else None
        allow = [(tr(label), lambda k=key, s=scope: self.answer_question(k, "allow", s))
                 for _, scope, label in ALLOW_SCOPES
                 if entry is None or approvals.scope_refused(entry, scope) is None] if whole else []
        return [*allow,
                (tr("Block"), lambda k=key: self.answer_question(k, "deny", "run")),
                (tr("Block always"), lambda k=key: self.answer_question(k, "deny", "always")),
                open_page]

    def answer_question(self, key, decision, scope):
        """A notification's answer: the Approvals page sends it (the same checks as its own buttons) without asking
        again - the button clicked on the notification was the answer, "always" too. A question answered
        or ended meanwhile is left alone, and why an answer cannot be given is said (the page may not be shown)."""
        approvals = self.approvals
        entry = approvals.questions.get(key)
        if entry is None or approvals.settled(key):
            self.notify(tr("Not answered"), tr("The question is not open any more."), level="info")
            return
        rules = approvals.answer_rules(entry)
        refused = rules["allow"] if decision == "allow" else rules["block"] if scope == "always" else rules["deny"]
        if refused is None and decision == "allow":
            refused = approvals.scope_refused(entry, scope)
        if refused:
            self.notify(tr("Not answered"), refused, level="info", action=(tr("Open approvals"),
                                                                             lambda k=key: self.open_question(k)))
            return
        approvals.answer(decision, scope, entry=entry, parent=self, asked=True)
        self.close_settled_toasts()

    def watcher_heartbeat(self):
        """Refresh the app's heartbeat while it runs: the questions wait on its Approvals page (and it notifies of
        them when its settings say so), so no launcher shows a notification of its own meanwhile - not for a
        question, nor a safeguard or a limit (approvals.desktop_notification), only a canary's alarm."""
        self.heartbeat_on = self.sai.watcher_heartbeat(True)
        if not self.heartbeat_on and not getattr(self, "heartbeat_warned", False):
            # Said once: then every question comes twice - the app's notification and the launcher's own.
            self.heartbeat_warned = True
            applog.log("WARNING", "approvals", tr(
                "The sandbox-ai in {path} cannot be told that this app shows the questions (it is older than "
                "0.1.86, or its state folder cannot be written): each question also comes as the launcher's own "
                "desktop notification. Update sandbox-ai; runs started before stay so until they are restarted.",
                path=printable(self.sai.directory)))

    def open_question(self, key):
        """A notification's action: the Approvals page's Questions with its question selected (when it is still
        open)."""
        self.show_approvals_questions()
        if key is not None:
            self.approvals.select_question(key)

    def show_approvals_questions(self):
        """The Approvals page at its Questions tab (a question's notification; the page keeps its tab otherwise)."""
        self.show_page("approvals")
        if hasattr(self.approvals, "show_questions"):
            self.approvals.show_questions()

    def changeEvent(self, event):  # noqa: N802
        """A new theme: the navigation's icons and the question count take its colours."""
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "nav_rows", None):
            for key in self.keys:
                if key not in VIEW_OF:
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

    def safeguard_popup(self, level, title, text):
        """A safeguard refused or ended something in a running sandbox: a warning (an error for a canary, an
        out-of-memory kill, a failed check), with the way to the Safeguards tab."""
        self.notify(title, text, level=level, action=(tr("Show safeguards"), self.show_safeguards))

    def safeguards_arrived(self, records):
        """New safeguard records: into the Safeguards tab when the Activity page was made (else it reads them
        all when it is first shown)."""
        page = self.built("activity")
        if page is not None:
            page.safeguards.add_live(records)

    def show_safeguards(self):
        self.show_page("activity")
        self.activity.show_safeguards()

    def notify(self, title, text, level="warning", action=None):
        """A notification of LEVEL (info, success, warning, error): a popup in the app's theme at the corner of
        the screen, or the desktop's own (setting notify_style; also where a window cannot place itself, e.g.
        on Wayland). Title and text are plain text: control characters are shown escaped, and for the desktop's
        notification, whose body may hold markup, what the agent chose is HTML-escaped (notifications.py).
        ACTION: (label, callback) for a button on the popup. Warnings and errors also ask for attention in the
        task bar.
        With the setting notifications off (the default) nothing pops up: the notification goes into the log only
        (the log window, its level kept), and None is returned. Questions wait on the Approvals page all the same,
        and the app still tells the launchers that it shows them (watcher_heartbeat), so no launcher pops up its own."""
        shown = backend.setting("notifications")
        applog.log({"error": "ERROR", "warning": "WARNING"}.get(level, "INFO"), "notifications",
                   f"{title}: {text}" if text else title)
        if not shown:
            return None
        style = backend.setting("notify_style")
        toast = self.notifier.show(level, title, text, seconds=backend.setting("notify_seconds"),
                                   style=style if style in ("themed", "system") else "themed", action=action)
        if level in ("warning", "error"):
            QApplication.alert(self)
        return toast

    # -- the storage check --

    def storage_due(self, now=None):
        """Whether the storage check is due on this machine: Settings › Storage (storage_check: off, weekly, daily)
        and when it ran last there (by this app, the command line or another app: storage.json)."""
        defaults, problem = self.sai.sandbox_defaults()
        every = {"daily": 86400, "weekly": 7 * 86400, "off": None}.get(defaults.get("storage_check", "weekly"),
                                                                      7 * 86400)
        if problem or every is None:
            return False
        last = self.sai.storage_last_check()
        return last is None or (time.time() if now is None else now) - last >= every

    def storage_timed(self):
        """The storage timer: the first look after STORAGE_FIRST_SECONDS, then every STORAGE_EVERY_SECONDS."""
        self.storage_timer.setInterval(STORAGE_EVERY_SECONDS * 1000)
        self.storage_tick()

    def storage_tick(self, force=False):
        """The storage check in the background when it is due (FORCE: now); its answer: storage_checked. Then the
        configured hosts' (storage_hosts_tick)."""
        self.storage_hosts_tick(force)
        if self.storage_running or not (force or self.storage_due()):
            return False
        self.storage_running = True
        background(lambda: self.sai.storage_check(LOCAL), owner=self, timeout=1800, what="sandbox-ai gc --check",
                   done=self.storage_checked)
        return True

    def storage_checked(self, result, error):
        self.storage_running = False
        if error is not None or not isinstance(result, dict):
            applog.log("INFO", "storage", f"the storage check did not answer: {printable_text(error)}")
            return
        self.sandboxes.refresh_soon(again=True)  # (the Size column: the sizes of this check)
        text = storage_notice(result)
        if text:
            self.notify(tr("Storage"), text, level="warning" if result.get("above") is True else "info",
                        action=(tr("Optimize…"), lambda: (self.show_page("sandboxes"), self.sandboxes.optimize(LOCAL))))

    # -- the storage check of the configured hosts (storage_hosts.py) --

    def storage_hosts_on(self):
        """The hosts of `sandbox-ai hosts` (not this machine) whose storage the app checks."""
        try:
            entries = self.sai.host_entries() if self.sai.hosts_supported() else []
        except (OSError, ValueError) as exc:
            applog.swallowed("ui", "MainWindow.storage_hosts_on: the hosts list", exc)
            return []
        used, settings = self.state.get(storage_hosts.USED, [], list), backend.app_settings()
        return [e["name"] for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)
                and e.get("kind") != "local" and e["name"] != LOCAL and storage_hosts.is_on(e["name"], used, settings)]

    def storage_hosts_checked(self):
        """{host: what its last check said} (storage_hosts.kept), as the app keeps it."""
        found = self.state.get(storage_hosts.CHECKED, {}, dict)
        return {host: entry for host, entry in found.items() if isinstance(entry, dict)}

    def storage_hosts_due(self, now=None):
        """The hosts whose storage check is due now, in the list's order."""
        defaults, problem = self.sai.sandbox_defaults()
        local = "weekly" if problem else defaults.get("storage_check", "weekly")
        checked = self.storage_hosts_checked()
        return [host for host in self.storage_hosts_on() if storage_hosts.due(checked.get(host), local, now)]

    def storage_hosts_tick(self, force=False):
        """The next host whose storage check is due (FORCE: every host that is on), in the background - one host at a
        time; its answer: storage_host_checked, which asks for the next."""
        if getattr(self, "storage_host_running", None):
            return None
        hosts = self.storage_hosts_on() if force else self.storage_hosts_due()
        if force:
            self.storage_hosts_forced = list(hosts[1:])
        if not hosts:
            return None
        applog.log("INFO", "storage", f"the storage check of {printable(hosts[0])} runs (sandbox-ai gc --check there)")
        self.storage_host_now(hosts[0])
        return hosts[0]

    def storage_host_checked(self, host, result, error):
        self.storage_host_running = None
        if error is not None or not isinstance(result, dict):
            applog.log("INFO", "storage", f"the storage check of {printable(host)} did not answer: "
                                          f"{printable_text(error)}")
        checked = self.storage_hosts_checked()
        checked[host] = storage_hosts.kept(result, error)
        self.state.set(storage_hosts.CHECKED, checked)
        hosts = self.built("hosts")
        if hosts is not None:
            hosts.set_storage(checked)
        if error is None and isinstance(result, dict):
            self.sandboxes.refresh_soon(again=True)  # (the Size column of that host's starts)
            text = storage_notice(result, host)
            if text:
                self.notify(tr("Storage of {host}", host=printable(host)), text,
                            level="warning" if result.get("above") is True else "info",
                            action=(tr("Optimize…"), lambda: (self.show_page("sandboxes"),
                                                             self.sandboxes.optimize(host))))
        forced = getattr(self, "storage_hosts_forced", None) or []
        if forced:
            self.storage_host_now(forced.pop(0))
            return
        self.storage_hosts_tick()

    def storage_host_now(self, host):
        """The storage check of HOST now (the Hosts page's Check storage now), unless one of a host runs."""
        if getattr(self, "storage_host_running", None):
            return False
        self.storage_host_running = host
        background(lambda: self.sai.storage_check(host), owner=self, timeout=1800,
                   what=f"sandbox-ai gc --check on {host}",
                   done=lambda result, error: self.storage_host_checked(host, result, error))
        return True

    def storage_hosts_used(self, fleet):
        """The hosts the app uses - those on which the Sessions page found starts - remembered (storage_hosts)."""
        found = {name for name, instances in getattr(self.sandboxes, "remote", {}).items() if instances}
        found |= {name for name, info in (fleet or {}).items() if name != LOCAL and (info or {}).get("running")}
        used = self.state.get(storage_hosts.USED, [], list)
        if not found <= set(used):
            self.state.set(storage_hosts.USED, sorted(set(used) | found))

    # -- updates --

    def start_update_check(self):
        """At the start, as the update mode says (`sandbox-ai update mode --json`, read in the background): off -
        nothing is checked; a newer signed release the last check found - the status line offers it at once; a
        check due (none for a day) - checked now. A sandbox-ai without the update mode: checked as before."""
        if not self.sai.has_update_mode():
            self.check_updates()
            return
        background(self.sai.update_status, owner=self, timeout=90, what="update mode --json",
                   done=lambda data, error: self.update_mode_read(data, error))

    def update_mode_read(self, data, error):
        if error is not None or not isinstance(data, dict):
            return  # (unreadable: nothing unasked; Help > Check for updates still asks)
        if data.get("mode") == "off":
            self.show_update_text(tr("Update checks are off (Settings › Updates)."))
            return
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        if result.get("newer") and isinstance(result.get("available"), str):
            # ("newer": signed by a trusted key - updates.py's verdict, as --check says it)
            self.update_checked(0, f"installed: {data.get('installed')}\navailable: {result['available']}\n"
                                   f"signature: good, by the trusted key {result.get('signer') or ''}\n", "")
        last = data.get("last_check")
        if not isinstance(last, (int, float)) or not 0 <= self.update_clock() - last < 86400:
            self.check_updates()

    def check_updates(self):
        """A check the human asked for (Help > Check for updates, the System page's button): it answers in the
        window's status bar wherever they are - that it runs, what it found (update_checked), or that one still runs
        (the user 2026-11-22: "wenn ich auf updates pruefen klicke passiert nichts")."""
        if self.update_check.start(self.sai.command("update", "--check")):
            self.update_asked = True
            self.show_update_text(tr("Checking for a newer sandbox-ai…"))
            self.statusBar().showMessage(tr("Checking for a newer sandbox-ai…"))
        else:
            self.statusBar().showMessage(tr("A check for updates is still running; its result follows here."), 10000)

    def update_checked(self, status, out, err):
        """What `sandbox-ai update --check` found: a newer release signed by a trusted key - the status line offers
        it (Update now… where this copy replaces itself) and a notification tells it once per version; a newer one
        that is not signed so - the status line says why it is not offered; why a check failed (offline, GitHub's
        rate limit) - on the System page, quietly."""
        asked, self.update_asked = getattr(self, "update_asked", False), False
        versions = backend.parse_update_check(status, out)
        if versions is None:
            self.show_update_text(tr("No update information: {why}", why=printable_text(
                backend.update_check_error(status, out, err) or "")[:300]))
            if asked:
                self.statusBar().showMessage(self.update_said, 20000)
            return
        installed, available = versions
        signature = backend.update_check_signature(out)
        if backend.newer(available, installed) and signature != "good":
            why = {"bad": tr("its signature is BAD"), "none": tr("it is not signed")}.get(
                signature, tr("it is not signed by a trusted key"))
            text = tr("sandbox-ai {available} is there, but {why}: not offered", available=available, why=why)
            if not self.update_running:
                self.offer_update()
            self.update_label.setText(text)
            self.update_label.setToolTip(safe_tooltip(tr("Each update must be signed by a release key this copy "
                                                         "trusts; update --check shows the details.")))
            self.show_update_notice(True)
        elif backend.newer(available, installed):
            text = tr("sandbox-ai {available} is available (installed: {installed}); update with: sandbox-ai update",
                      available=available, installed=installed)
            route, where = backend.update_route(self.sai.directory)
            self.offer_update(installed, available, route, where, hint=text)
            self.announce_update(installed, available)
        else:
            # (a copy newer than the newest release - installed from a checkout, another source or a release not
            # published there yet: said so, not just "up to date")
            text = tr("sandbox-ai {installed} is newer than the newest release {available}: nothing to update",
                      installed=installed, available=available) if backend.newer(installed, available) else \
                tr("sandbox-ai {installed} is up to date.", installed=installed)
            if not self.update_running:
                self.offer_update()
        self.show_update_text(text)
        if asked:  # (a check nobody clicked keeps to the System page and the status line's notice)
            self.statusBar().showMessage(text, 20000)

    def announce_update(self, installed, available):
        """The notification of a newer signed release, once per version (state.json remembers it across starts):
        with "Update now…" where this copy replaces itself, else the way it is updated."""
        if self.state.get(UPDATE_ANNOUNCED, "", str) == available:
            return
        self.state.set(UPDATE_ANNOUNCED, available)
        if self.update_offer is not None:
            self.notify(tr("sandbox-ai {available} is available", available=available),
                        tr("installed: {installed}; Update now… in the status line installs it, signed and checked",
                           installed=installed),
                        level="info", action=(tr("Update now…"), self.update_now))
            return
        way = getattr(self, "update_why", "") or tr("see Settings › Updates")
        self.notify(tr("sandbox-ai {available} is available", available=available),
                    tr("installed: {installed}; {way}", installed=installed, way=way), level="info")

    def make_update_notice(self):
        """In the status line, while a newer sandbox-ai is there: what the check found, and "Update now…" where this
        copy can update itself (install.sh's installation; a system one with sudo) - else the hint stays, and its
        tooltip says why there is no button. After a good update: "Restart the app"."""
        self.update_offer, self.update_running, self.updated_to, self.restart_argv = None, False, None, None
        self.update_notice = QWidget()
        self.update_notice.setObjectName("update_notice")
        row = QHBoxLayout(self.update_notice)
        row.setContentsMargins(0, 0, 0, 0)
        self.update_label = plain_label(word_wrap=False)
        self.update_label.setAccessibleName(tr("Update of sandbox-ai"))
        self.update_now_button = QPushButton(tr("Update now…"))
        self.update_now_button.setAccessibleName(tr("Update sandbox-ai now"))
        self.update_now_button.clicked.connect(lambda _=False: self.update_now())
        self.restart_button = QPushButton(tr("Restart the app"))
        self.restart_button.setAccessibleName(tr("Restart the app"))
        self.restart_button.setToolTip(safe_tooltip(tr("Quit the app the usual way and start it again: the new "
                                                       "version")))
        self.restart_button.clicked.connect(lambda _=False: self.restart_app())
        row.addWidget(self.update_label)
        row.addWidget(self.update_now_button)
        row.addWidget(self.restart_button)
        self.update_now_button.hide()
        self.restart_button.hide()
        self.update_notice.hide()
        self.update_notice_wanted = False
        self.statusBar().addWidget(self.update_notice)
        self.statusBar().messageChanged.connect(self.follow_message)

    def show_update_notice(self, shown):
        """The status line's update notice wanted (SHOWN) or not. QStatusBar paints its message where this notice
        is (a widget of its left part): shown over a message, the two texts overlapped (the user 2026-11-24). So a
        notice that comes up ends the message there (it is the newer news), and a message that comes later hides it
        for its time (follow_message) - explicitly: QStatusBar's own hiding is undone by the next layout of the bar,
        which shows the notice again over the message."""
        self.update_notice_wanted = shown
        bar = self.statusBar()
        if shown and bar.currentMessage():
            bar.clearMessage()
        self.follow_message()

    def follow_message(self, _message=None):
        """The update notice shown while it is wanted and the status line has no message (show_update_notice)."""
        self.update_notice.setVisible(self.update_notice_wanted and not self.statusBar().currentMessage())

    def offer_update(self, installed=None, available=None, route=None, where=None, hint=""):
        """The status line's notice: "Update now…" beside what the check found when ROUTE (backend.update_route; WHERE:
        the installation's folder) is one that `sandbox-ai update` takes; else HINT, its tooltip saying why there is
        no button. Nothing given: no notice (but "Restart the app" after an update). The menu's entry follows."""
        offered = available is not None and route in ("user", "root", "self")
        self.update_offer = (installed, available, route, where) if offered else None
        if offered:
            text = tr("sandbox-ai {available} is available (installed: {installed})", available=available,
                      installed=installed)
            self.update_now_button.setToolTip(safe_tooltip(
                tr("Installed for every user in {path}: sudo sandbox-ai update runs in a terminal, where sudo asks "
                   "for your password (the app never sees it)", path=printable(where)) if route == "root" else
                tr("sandbox-ai update in a terminal: the release's file for this copy, checked (SHA256SUMS and its "
                   "signature), replaces {path}; the version before is kept", path=printable(where))
                if route == "self" else
                tr("sandbox-ai update in a terminal: the newest signed release; the version before is kept")))
            why = ""
        else:
            text = hint if available is not None else ""
            # A package or a pip installation that cannot replace itself: sandbox-ai's own words say the command
            # that updates it (a folder that is not writable: sudo ... update).
            blocker = backend.update_blocker(self.sai.directory) if route in ("package", "other") else None
            why = tr("This copy does not replace itself: {why}", why=printable_text(blocker)) if blocker else \
                  {"package": tr("This sandbox-ai runs from a package (the AppImage, the macOS app or the .pyz): it "
                                 "does not update itself. Download the new one from the release page."),
                   "checkout": tr("This sandbox-ai is a git checkout ({path}): update it with git pull.",
                                  path=printable(where or "")),
                   "other": tr("This sandbox-ai was not installed with install.sh: a pip installation updates "
                               "with pip install -U sandbox-ai-app.")}.get(route, "")
        if not text and self.updated_to is not None:
            text = tr("sandbox-ai updated to {version}: restart the app to use it", version=self.updated_to)
        self.update_label.setText(text)
        self.update_why = why  # (plain: the notification says it too)
        self.update_label.setToolTip(safe_tooltip(why) if why else "")
        self.update_now_button.setVisible(offered)
        self.update_now_button.setEnabled(offered and not self.update_running)
        self.restart_button.setVisible(self.updated_to is not None and not offered)
        self.show_update_notice(bool(text))
        self.actions["self-update"].setEnabled(offered and not self.update_running)

    def update_now(self):
        """"Update now…": asked first (from which version to which), then `sandbox-ai update` - or, for a system
        installation, `sudo sandbox-ai update` - in a terminal tab of the app (or a terminal window, as the setting
        says), where the human answers a password prompt; no password ever passes through the app. Its end:
        update_ended."""
        offer = self.update_offer
        if offer is None or self.update_running:
            return
        installed, available, route, where = offer
        try:
            argv = backend.self_update_command(self.sai, route)
        except backend.NoSudo as exc:
            warn(self, tr("Update sandbox-ai"), tr(
                "sandbox-ai is installed for every user ({path}): only root can update it, and there is no sudo "
                "here. Run as root (e.g. after su -):\n\n{command}", path=printable(where), command=printable_text(exc)))
            return
        except ValueError as exc:
            warn(self, tr("Update sandbox-ai"), printable_text(exc))
            return
        text = " ".join((
            tr("Update sandbox-ai from {installed} to {available}?", installed=installed, available=available),
            tr("It is installed for every user: the update runs as root, sudo asks for your password in the "
               "terminal.") if route == "root" else tr("It runs in a terminal."),
            tr("The app keeps running; restart it afterwards to use the new version.")))
        if not ask_first(self, "self-update", tr("Update sandbox-ai"), text, tr("Update"), details=shlex.join(argv)):
            return
        self.update_running = True
        self.offer_update(*offer)
        title = tr("Update sandbox-ai to {version}", version=available)
        if not self.in_terminal(argv, title, ended=lambda status: self.update_ended(available, status)):
            self.update_running = False
            self.offer_update(*offer)

    def update_ended(self, available, status):
        """The update's command has ended: 0 - a notification with "Restart the app" (the status line keeps that
        button); else the status line says it failed (its terminal shows why) and "Update now…" may try again."""
        self.update_running = False
        if status == 0:
            self.updated_to = available
            self.offer_update()
            text = self.update_label.text()
            self.show_update_text(text)
            self.notify(tr("sandbox-ai updated"), text, level="success",
                        action=(tr("Restart the app"), self.restart_app))
            return
        if self.update_offer is not None:
            self.offer_update(*self.update_offer)
        self.update_label.setText(tr("The update to {version} failed (exit status {status}): its terminal shows why.",
                                     version=available, status=status))
        self.show_update_notice(True)
        self.show_update_text(self.update_label.text())

    def restart_app(self):
        """Quit the usual way (with its questions) and start the app again: the new process waits for this one to end
        (main.py --wait-for-pid), as the single-instance guard is free only then; started once the window has really
        closed (finish)."""
        self.restart_argv = backend.restart_command(os.getpid(), self.sai.directory)
        if not self.quit_app() and not getattr(self, "stopping", False):
            self.restart_argv = None  # the quit was refused: no restart later

    def start_again(self):
        """The new app, detached (finish: the window has closed for good)."""
        argv, self.restart_argv = getattr(self, "restart_argv", None), None
        if not argv:
            return
        started = QProcess.startDetached(argv[0], argv[1:])
        ok = started[0] if isinstance(started, tuple) else started
        applog.write(f"{applog.header('INFO' if ok else 'ERROR', 'restart')}\n  "
                     f"{'started' if ok else 'could not start'}: {shlex.join(argv)}\n")

    def show_update_text(self, text):
        """What the update check says, on the System page (one made later shows it then)."""
        self.update_said = text
        system = self.built("system")
        if system is not None:
            system.update_text.setText(text)

    # -- terminals --

    def in_terminal(self, argv, title, embedded=None, cwd=None, ended=None):
        """Run ARGV in a terminal: a tab of the app (EMBEDDED True, or None and the setting says so, QtWebEngine with
        xterm.js is there and Chromium's sandbox can start now: TerminalsPage.chromium_ready, checked in a thread),
        else a terminal window. True when it runs or will (the tab or, if Chromium's sandbox cannot start, the
        window follows the check), False when no terminal window could be started. ENDED(status): called with the
        command's exit status when it ends (in a window too: terminal_window)."""
        if embedded is None:
            embedded = self.sai.settings().get("terminal", "app") == "app"
        running = running_session(self, argv)
        if running is not None:
            # A start with the name of a session that runs already (the Start page's or the Board's name field): its
            # terminal, as Attach does (its open tab shown), not a start that ends with "is running; attach with".
            argv, title = self.sai.command("session", "attach", running.session), \
                tr("session {name}", name=printable(running.name))
            ended = None
            applog.log("INFO", "terminal", f"{printable(running.name)} runs already: its session is attached instead "
                                           "of a second start")
            self.statusBar().showMessage(tr("{name} runs already: its terminal is opened", name=printable(running.name)),
                                         8000)
        directory = cwd if cwd and os.path.isdir(cwd) else None
        approvals = vars(self).get("approvals")  # (made already; and a stand-in window of the tests has none)
        if approvals is not None:  # what it starts may ask (on any host): every host is asked for questions a while
            approvals.wake_hosts()
        applog.log("INFO", "terminal", f"{printable(title)}: runs in " + (
            "a tab of the app" if embedded and terminal.available()[0] else
            "a terminal window" + (f" (tabs of the app not available: {terminal.available()[1]})" if embedded
                                   else "")) + f": {applog.argv_text(argv, 1500)}")
        if embedded and terminal.available()[0]:
            def opened(view):
                if view is None:  # Chromium's sandbox cannot start now: a terminal window instead
                    applog.log("INFO", "terminal", f"{printable(title)}: Chromium's sandbox cannot start now - a "
                                                   "terminal window instead of a tab")
                    if ended is None:
                        self.terminal_window(argv, title)
                    elif not self.terminal_window(argv, title, ended=ended):
                        ended(-1)
                    return
                told = ended
                if isinstance(ended, StartTab):
                    ended.view = view  # (a start: its page says in this tab why it did not run, start_ended)
                    if not hasattr(getattr(view, "bridge", view), "exited"):
                        told = None  # (no tab whose command's end is told: nothing is said then)
                if told is not None:
                    # From the tab's terminal bridge, which outlives a tab closed while its command runs until the
                    # command is gone: the tab's own signal went with the tab, and the end was never told (a start
                    # being prepared stayed so until the app was restarted).
                    getattr(view, "bridge", view).exited.connect(told)
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
        return self.terminal_window(argv, title) if ended is None else self.terminal_window(argv, title, ended=ended)

    def fresh_starts(self, host, within):
        """The starts of HOST (this machine: "local") as the Sessions page saw them at most WITHIN seconds ago
        ([backend.Instance]), or None when its look is older, failed or was never made: a start under a session's
        name asks the host itself then (session_start.py)."""
        page = self.built("sandboxes")
        if page is None or host == LOCAL:
            return None  # (this machine's own are asked for directly: a moment, and with what `list --json` says)
        seen = getattr(page, "remote_seen", {}).get(host)
        if seen is None or time.time() - seen > within or host in getattr(page, "remote_problems", {}):
            return None
        return list(getattr(page, "remote", {}).get(host) or [])

    def refresh_starts(self, host):
        """The Sessions page looks at HOST again now (a start there was stopped, or started)."""
        page = self.built("sandboxes")
        if page is None:
            return
        if host == LOCAL:
            page.refresh()
        else:
            page.refresh_remote([host])

    def terminal_window(self, argv, title, ended=None):
        """ARGV in a terminal window of the desktop (SANDBOX_AI_TERMINAL, or one found); False if none starts.
        ENDED(status): called with ARGV's exit status, which the window's shell writes to a file of a private folder
        (looked for every second: the window's own process says nothing, a terminal may hand it to a server)."""
        status_file = None
        if ended is not None:
            import tempfile
            status_file = Path(tempfile.mkdtemp(prefix="sandbox-ai-app-status.")) / "status"
            argv = ["sh", "-c", 'f=$1; shift; "$@"; s=$?; echo "$s" > "$f.part" && mv -f "$f.part" "$f"; exit "$s"',
                    "sh", str(status_file), *argv]
        command = self.sai.in_terminal(argv, title)
        if command is None:
            applog.log("WARNING", "terminal", f"{printable(title)}: not started - no terminal emulator found (set "
                                              "SANDBOX_AI_TERMINAL)")
            QMessageBox.warning(self, tr("No terminal"), shown_as_text(tr(
                "No terminal emulator found. Set SANDBOX_AI_TERMINAL, or run:") + "\n\n" + shlex.join(argv)))
            return False
        started = QProcess.startDetached(command[0], command[1:])
        ok = started[0] if isinstance(started, tuple) else started
        applog.log("INFO" if ok else "WARNING", "terminal", f"{printable(title)}: terminal window "
                   f"{'started' if ok else 'did not start'}: {applog.argv_text(command, 1500)}")
        if not ok:
            QMessageBox.warning(self, tr("Not started"), shown_as_text(shlex.join(command)))
        elif status_file is not None:
            self.await_status_file(status_file, ended)
        return ok

    STATUS_LIMIT = 6 * 3600  # seconds a command in a terminal window is waited for (then its end is not reported)

    def await_status_file(self, path, ended):
        """ENDED(status) once PATH holds the exit status (terminal_window); its folder is removed then."""
        timer, began = QTimer(self), time.monotonic()

        def look():
            try:
                said = path.read_text().strip()
            except OSError:
                if time.monotonic() - began < self.STATUS_LIMIT:
                    return
                said = None
            timer.stop()
            timer.deleteLater()
            shutil.rmtree(path.parent, ignore_errors=True)
            applog.log("INFO", "terminal", f"the command of a terminal window ended: exit status {said}"
                       if said is not None else
                       f"a terminal window's command was not seen to end within {self.STATUS_LIMIT} s")
            if said is not None:
                ended(int(said) if said.isdigit() else -1)
        timer.timeout.connect(look)
        timer.start(1000)

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
        """Bottom right, always: the app's version, and sandbox-ai's where it differs (an app left running through an
        update of sandbox-ai: "App 0.1.97 · sandbox-ai 0.1.99"); where both run from on hover."""
        from . import __version__
        try:
            launcher = self.sai.version()
        except Exception as _exc:  # noqa: BLE001 - a launcher that does not load: the app's version alone
            applog.swallowed("ui", "MainWindow.make_version_label: a launcher that does not load: the app's ver…", _exc)
            launcher = None
        text = f"v{__version__}" if launcher in (None, __version__) else \
            tr("App {app} · sandbox-ai {sai}", app=__version__, sai=printable(launcher))
        label = widgets.plain_label(text, word_wrap=False, role="secondary")
        label.setObjectName("version_label")
        label.setAccessibleName(tr("Version"))
        label.setToolTip(widgets.safe_tooltip(tr("sandbox-ai-app {app} from {app_path}\n"
                                                 "sandbox-ai {version} from {path}", app=__version__,
                                                 app_path=printable(backend.PACKAGE_DIR),
                                                 version=printable(launcher or "?"),
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
        self.keep_state_now()
        files_page = self.built("files")
        if files_page is not None and hasattr(files_page, "drop_changes"):
            # (Qt's quit stops at a window that stays open: an editor of changes not saved asked with nobody there to
            # answer, and the app went on running)
            files_page.drop_changes()
        self.stop_work()
        QApplication.quit()

    def make_tray(self):
        self.tray = QSystemTrayIcon(theme.app_icon(), self)  # (with a dot while a question waits)
        self.tray.setToolTip("sandbox-ai")
        self.tray.activated.connect(lambda *_: self.show_page("approvals"))
        self.tray.messageClicked.connect(lambda: self.show_page("approvals"))
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction(tr("Show sandbox-ai"), lambda: self.show_page("approvals"))
        self.tray_menu.addAction(tr("Quit"), self.quit_app)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.show()
        self.show_tray_state()
        self.tray_attention = None  # (a new tray: its dot follows the count)
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
        widgets.release_background()  # (no first paint to wait for)
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

    def quit_for_another_start(self):
        """A newer start of the app (an update, another installation) asked this one to make way: the usual Quit, its
        questions and all (the Board's changes not applied are kept, as at every quit). True when the app quits (or
        stops its work to quit)."""
        applog.log("INFO", "quit", "a newer start of the app asked this one to quit (single_instance.py)")
        return bool(self.quit_app() or getattr(self, "stopping", False))

    def end_of_session(self, *_):
        """Logout: no questions and no hiding (an ignored close would cancel the logout); the work is
        stopped within QUIT_WAIT. If the session goes on after all (the logout was cancelled), the window
        behaves as before after SESSION_GRACE seconds."""
        self.quitting = True
        self.keep_state_now()
        if not self.stopped:
            self.stop_work()
        self.session_timer.start(SESSION_GRACE * 1000)

    def commit_edits(self):
        """What is being typed on the board (its inspector) taken now, before the state is written for good."""
        board = self.built("board")
        if board is not None:
            try:
                board.commit_edits()
            except Exception as exc:  # noqa: BLE001 - never stops the quitting
                applog.log("WARNING", "board", f"what was being typed could not be taken: {exc}")

    def keep_state_now(self):
        """The window's state (panels, tabs, the board's changes not applied) on the disk now, before the work is
        stopped: at a logout or SIGTERM the app may be killed before shutdown() writes it."""
        if getattr(self, "shut_down", False):
            return
        try:
            self.commit_edits()
            self.save_docks()
            self.save_tabs()
            self.state.flush()
        except Exception as exc:  # noqa: BLE001 - never stops the quitting
            applog.log("WARNING", "state", f"not written before quitting: {exc}")

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
        except Exception as _exc:  # noqa: BLE001 - nothing known to run
            applog.swallowed("ui", "MainWindow.questions_refused_without_window: nothing known to run", _exc)
            runs = {}
        if not runs:
            return True
        return ask_first(self, "app-quit", tr("Quit"),
                         tr("{count} sandbox(es) still run. Without this window nobody answers their questions: a "
                            "request that asks fails after {seconds} seconds (its question stays open for sandbox-ai "
                            "approvals and the web interface). Quit anyway?", count=len(runs),
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
        # The file manager's editors with changes not saved: asked before anything is shut down ("Keep editing" keeps
        # the whole app; asked by Qt's quit afterwards, it left an editor open in an app already shut down).
        files_page = self.built("files")
        if files_page is not None and hasattr(files_page, "may_quit") and not files_page.may_quit():
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
        files_page = self.built("files")
        if files_page is not None and hasattr(files_page, "drop_changes"):
            files_page.drop_changes()  # (asked before: may_quit; closed by Qt's quit now without a second question)
        self.shutdown()
        event.accept()
        self.start_again()  # "Restart the app": the new one waits until this process has ended
        self.closed.emit()

    def shutdown(self):
        """Stop timers, commands and terminals; once (closing, app.aboutToQuit and tests may all call it)."""
        if getattr(self, "shut_down", False):
            return
        self.commit_edits()
        self.save_docks()
        self.save_tabs()
        terminals = self.built("terminals")
        if terminals is not None and hasattr(terminals, "save_tabs"):
            terminals.save_tabs()  # the terminals of running starts, reopened at the next start
        self.state.flush()
        board = self.built("board")
        if board is not None and hasattr(board, "end_bridge_waits"):
            board.end_bridge_waits()  # (kept by the write above: the next start waits on)
        if terminals is not None:
            terminals.store = None  # what ends with the app (its tabs) changes the kept list no more
        self.shut_down = True
        if widgets.LOG_WINDOW_OPENER == self.open_log_window:
            widgets.LOG_WINDOW_OPENER = None
        if widgets.EMERGENCY_ACTION is self.actions.get("emergency-cut"):
            widgets.EMERGENCY_ACTION = None
        if getattr(self, "log_window", None) is not None:
            self.log_window.close()
            self.log_window.deleteLater()
            self.log_window = None
        self.heartbeat_timer.stop()
        if getattr(self, "heartbeat_on", False):  # the launchers notify again from now on
            self.sai.watcher_heartbeat(False)
            self.heartbeat_on = False
        widgets.release_background()  # (closed before its first paint: what was held runs, its result abandoned)
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
        self.update_timer.stop()
        if getattr(self, "notifier", None) is not None:
            self.notifier.close_all()  # the popups are windows of their own
        if self.built("images") is not None:
            self.images.list_command.stop()
            self.images.package_command.stop()
        AsyncCommand.end_stopped()  # the timers that would SIGKILL them do not fire any more
        if self.built("activity") is not None:
            self.activity.shutdown()
        if self.built("traffic") is not None:
            self.traffic.shutdown()
        if self.built("terminals") is not None:
            self.terminals.close_all()
        if self.tray is not None:
            self.tray.hide()
