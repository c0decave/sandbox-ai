"""`sandbox-ai doctor` for a human: the checks grouped by area (System, Engine, Agents, Project), each with a status
icon in the theme's colours, what was found in plain words and how to fix it, the fix command copyable.

The results come from sandbox-ai's doctor module in this process ((status, check, detail, hint) tuples), from
`sandbox-ai doctor --json` (another host: a list of {status, check, detail, hint, group}), or, from an older
sandbox-ai, as its text output (parse_text). No result is ever run: a fix command is only copied.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QMenu, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import theme
from .files import printable
from .i18n import N_, tr
from .widgets import plain_label, safe_tooltip

STATUSES = ("fail", "warn", "ok", "skip")
SYMBOLS = {"ok": "✔", "warn": "!", "fail": "✘", "skip": "–"}
COLOURS = {"ok": "ok", "warn": "warn", "fail": "bad", "skip": "muted"}
WORDS = {"ok": N_("Fine"), "warn": N_("Warning"), "fail": N_("Problem"), "skip": N_("Not checked")}
# The doctor's own areas (doctor.GROUPS), in its order; a check that fits none is in the last.
GROUPS = (("System", N_("System")), ("Engine", N_("Engine (Podman, Bubblewrap, images)")),
          ("Agents", N_("Agents (logins, kits, plugins)")), ("Project", N_("Project and security mode")))
# The doctor's titles by area, for results without a group (an older sandbox-ai's --json or text).
PREFIXES = {"System": ("Ordinary user", "Python", "sandbox-ai", "User namespaces", "systemd user session",
                       "Connection proxy"),
            "Engine": ("Podman", "podman machine", "pasta", "Delegated UID/GID ranges", "newuidmap", "Image ", "VPN",
                       "Bubblewrap", "TIOCSTI", "Firejail", "nft", "setpriv", "slirp"),
            "Agents": ("Login ", "Kit ", "Kits", "Plugins")}
COMMAND_WORDS = ("sandbox-ai", "sudo", "podman", "systemctl", "loginctl", "modprobe", "sysctl", "apt", "apt-get", "dnf",
                 "pacman", "zypper", "brew", "usermod", "git", "echo", "chmod", "mkdir")


def group_of(check, groups=None):
    """The area of the check titled CHECK (the doctor module's group_of when given as GROUPS)."""
    if groups is not None:
        try:
            return groups(check)
        except Exception:  # noqa: BLE001 - the app's own table below
            pass
    for name, prefixes in PREFIXES.items():
        if any(check == p or check.startswith(p) for p in prefixes):
            return name
    return "Project"


def fix_command(hint):
    """The command in a doctor's hint, to copy (`podman info`, "sandbox-ai build claude", "sudo modprobe tun ..."),
    or "" when the hint is advice in words."""
    hint = (hint or "").strip()
    quoted = re.search(r"`([^`]+)`", hint)
    if quoted:
        return quoted.group(1).strip()
    text = re.sub(r"^(?:run|try|then run)\s*:\s+", "", hint, flags=re.IGNORECASE)
    if text.split(" ", 1)[0] not in COMMAND_WORDS:
        return ""
    return re.split(r"\s+\(|;\s|,\s+(?:or|then|and)\s", text)[0].strip().rstrip(".")


def normalize(results, groups=None):
    """[{status, check, detail, hint, group, command}] of RESULTS: tuples of the doctor module or dicts of --json."""
    found = []
    for result in results or []:
        if isinstance(result, dict):
            status, check, detail, hint = (result.get(k, "") for k in ("status", "check", "detail", "hint"))
            group = result.get("group")
        else:
            status, check, detail, hint = (list(result) + ["", "", "", ""])[:4]
            group = None
        status = str(status) if str(status) in STATUSES else "skip"
        check, detail, hint = (printable(str(v or "")) for v in (check, detail, hint))
        group = group if group in dict(GROUPS) else group_of(check, groups)
        found.append({"status": status, "check": check, "detail": detail, "hint": hint, "group": group,
                      "command": fix_command(hint)})
    return found


LINE = re.compile(r"^\s*(ok|WARN|FAIL|--)\s+(.+?)\s{2,}(.*)$")


def parse_text(text):
    """The checks of doctor's text output (the table or --plain): [{status, check, detail, hint}]; a "-> hint" line
    belongs to the check above it."""
    found = []
    for line in (text or "").splitlines():
        match = LINE.match(line)
        if match:
            status = {"ok": "ok", "WARN": "warn", "FAIL": "fail", "--": "skip"}[match.group(1)]
            found.append({"status": status, "check": match.group(2).strip(), "detail": match.group(3).strip(),
                          "hint": ""})
        elif found and line.strip().startswith("->"):
            found[-1]["hint"] = line.strip()[2:].strip()
        elif found and found[-1]["hint"] and line.startswith("   ") and line.strip():
            found[-1]["hint"] += " " + line.strip()
    return found


def summary(checks):
    failed = sum(c["status"] == "fail" for c in checks)
    warned = sum(c["status"] == "warn" for c in checks)
    if not checks:
        return tr("The doctor found nothing to check."), "muted"
    if failed or warned:
        return tr("{failed} problem(s), {warned} warning(s): each says how to fix it.", failed=failed,
                  warned=warned), "bad" if failed else "warn"
    return tr("Everything needed is in place."), "ok"


CHECK_COLUMN = 240  # px the check's title gets at most; what was found and how to fix it share the rest, wrapped


class DoctorView(QWidget):
    """The checks as a list by area: problems first in each area, an area where all is fine folded; a click on a row
    shows it whole below (wrapped, the fix command selectable) and selects its fix command for copying. A narrow pane
    (the Hosts page) stays readable: the columns wrap, the rows grow when the columns change. An answer it cannot
    read (an older or unknown sandbox-ai's text) is shown as it came."""
    copied = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.summary = plain_label(tr("Not checked yet: press Check."), role="secondary")
        self.only_problems = QCheckBox(tr("Only problems and warnings"))
        self.only_problems.toggled.connect(lambda _=False: self.show_checks())
        self.copy_button = QPushButton(tr("Copy the fix command"))
        self.copy_button.setToolTip(tr("Copies the command of the selected check; run it in a terminal yourself"))
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(lambda _=False: self.copy_selected())
        top.addWidget(self.summary, 1)
        top.addWidget(self.only_problems)
        top.addWidget(self.copy_button)
        layout.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Check"), tr("What was found"), tr("How to fix")])
        self.tree.setAccessibleName(tr("The doctor's checks"))
        self.tree.setWordWrap(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setRootIsDecorated(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # Wrapped rows take their height from the column widths: laid out again when a column changes.
        self.relayout = QTimer(self)
        self.relayout.setSingleShot(True)
        self.relayout.setInterval(50)
        self.relayout.timeout.connect(self.tree.doItemsLayout)
        header.sectionResized.connect(lambda *_: self.relayout.start())
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self.copy_item(item))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.menu)
        # The selected check whole (status, what was found, how to fix, the command), or a text that could not be
        # read as checks: selectable, wrapped, never cut.
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setAccessibleName(tr("The check chosen, whole"))
        self.detail.setPlaceholderText(tr("Select a check to read it whole here."))
        self.detail.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.checks, self.raw = [], ""

    def set_results(self, results, groups=None):
        """Show RESULTS (doctor tuples or --json dicts); GROUPS: the doctor module's group_of, if any."""
        self.checks, self.raw = normalize(results, groups), ""
        self.show_checks()

    def set_text(self, text):
        """Show doctor's text output (an older sandbox-ai without --json); a text that holds no check it can read
        is shown as it came (escaped)."""
        self.set_results(parse_text(text))
        if not self.checks and (text or "").strip():
            self.raw = "\n".join(printable(line) for line in str(text).strip().splitlines())
            self.summary.setText(tr("The doctor answered in a form this app does not read: its text is below, as it "
                                    "came."))
            self.summary.setStyleSheet("")
            self.detail.setPlainText(self.raw)

    def count(self):
        return len(self.checks)

    def rowCount(self):  # noqa: N802 - as the table it replaces
        return self.count()

    def show_checks(self):
        text, kind = summary(self.checks)
        self.summary.setText(text)
        self.summary.setStyleSheet(f"color: {theme.color(kind)}; font-weight: 600;" if kind != "muted" else "")
        self.tree.clear()
        only = self.only_problems.isChecked()
        order = {s: i for i, s in enumerate(STATUSES)}
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for key, title in GROUPS:
            rows = sorted((c for c in self.checks if c["group"] == key and (not only or c["status"] in ("fail", "warn"))),
                          key=lambda c: order[c["status"]])
            if not rows:
                continue
            failed = sum(c["status"] == "fail" for c in rows)
            warned = sum(c["status"] == "warn" for c in rows)
            worst = "fail" if failed else "warn" if warned else "ok"
            label = tr(title) + ("  " + tr("{failed} problem(s), {warned} warning(s)", failed=failed, warned=warned)
                                 if failed or warned else "  " + tr("all fine"))
            group = QTreeWidgetItem([label, "", ""])
            group.setFont(0, bold)
            group.setIcon(0, theme.dot(COLOURS[worst], 12))
            group.setFirstColumnSpanned(True)
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(group)
            for check in rows:
                fix = check["hint"] if check["status"] in ("fail", "warn") else ""
                item = QTreeWidgetItem([f"{SYMBOLS[check['status']]}  {check['check']}", check["detail"], fix])
                item.setIcon(0, theme.dot(COLOURS[check["status"]], 12))
                item.setForeground(0, theme_brush(check["status"]))
                item.setToolTip(0, tr(WORDS[check["status"]]))
                for column, value in ((1, check["detail"]), (2, fix)):
                    if value:
                        item.setToolTip(column, safe_tooltip(value))
                if fix and check["command"]:
                    item.setToolTip(2, safe_tooltip(tr("Command: {command} (double-click copies it)",
                                                       command=check["command"])))
                item.setData(0, Qt.ItemDataRole.UserRole, check["command"] if fix else "")
                item.setData(1, Qt.ItemDataRole.UserRole, check)
                group.addChild(item)
            group.setExpanded(worst != "ok")  # an area where all is fine is folded: the problems stand out
        # The check's title gets what it needs up to CHECK_COLUMN; the rest is for what was found and the fix.
        self.tree.resizeColumnToContents(0)
        self.tree.setColumnWidth(0, min(self.tree.columnWidth(0), CHECK_COLUMN))
        self.detail.setPlainText("")
        self.selection_changed()

    def items(self):
        return [self.tree.topLevelItem(i).child(j) for i in range(self.tree.topLevelItemCount())
                for j in range(self.tree.topLevelItem(i).childCount())]

    def selected_command(self):
        items = self.tree.selectedItems()
        return (items[0].data(0, Qt.ItemDataRole.UserRole) or "") if items else ""

    def selection_changed(self):
        self.copy_button.setEnabled(bool(self.selected_command()))
        items = self.tree.selectedItems()
        check = items[0].data(1, Qt.ItemDataRole.UserRole) if items else None
        if isinstance(check, dict):
            lines = [f"{tr(WORDS[check['status']])}: {check['check']}"]
            if check["detail"]:
                lines.append(tr("What was found: {detail}", detail=check["detail"]))
            if check["status"] in ("fail", "warn") and check["hint"]:
                lines.append(tr("How to fix: {hint}", hint=check["hint"]))
            if check["status"] in ("fail", "warn") and check["command"]:
                lines.append(tr("Command (double-click the row or Copy copies it): {command}",
                                command=check["command"]))
            self.detail.setPlainText("\n".join(lines))
        elif not self.raw:
            self.detail.setPlainText("")

    def copy_item(self, item):
        command = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        if command:
            QGuiApplication.clipboard().setText(command)
            self.copied.emit(command)
        return command

    def copy_selected(self):
        items = self.tree.selectedItems()
        return self.copy_item(items[0]) if items else ""

    def menu(self, point):
        item = self.tree.itemAt(point)
        command = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        if not command:
            return
        menu = QMenu(self)
        menu.addAction(tr("Copy the fix command"), lambda: self.copy_item(item))
        menu.exec(self.tree.viewport().mapToGlobal(point))


def theme_brush(status):
    from PySide6.QtGui import QBrush, QColor
    return QBrush(QColor(theme.color(COLOURS[status])))
