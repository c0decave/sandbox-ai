"""Safeguards in the app: the Safeguards tab of the Activity page, and popups while sandboxes run.

Whenever a safeguard of a sandbox refuses or ends something (the firewall, a refused lookup, the proxy, a rule, a
question nobody answered, the privacy check of a push, the security mode, the administrator's policy, the run's
taint, a device, the memory watch, the kernel's out-of-memory killer, a failed check, a canary), sandbox-ai's
launcher writes one record (safeguards.jsonl; `sandbox-ai safeguards`): what, why, the process when known, and
the fix, the exact command or option that would allow it, or that it stays refused.

The tab lists them for every run, newest first, warnings in the theme's warning colour and errors in its error
colour, with the whole record and the fix (which can be copied) below. The popups come from the running
sandboxes' new records, grouped and rate-limited as the launcher's desktop notifications are (safeguards.py's
Notifier): the first of a kind and place at once, the ones that follow within a minute as one ("12 connections
to x.example dropped by the firewall in the last minute"). The setting notify_safeguards turns them off; the
kinds muted in sandbox-ai's defaults (safeguard_mute, Settings > Window) never pop up and are still listed.
Everything in a record may quote the agent: plain text, control characters escaped; records are redacted by
sandbox-ai and hold no secret values."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import backend, tables, theme
from .files import printable
from .i18n import N_, tr
from .widgets import MONO, background, plain_label, safe_tooltip

COLUMNS = (N_("Time"), N_("Run"), N_("Kind"), N_("What"), N_("Why"), N_("To allow it"))
RECORD_ROLE = Qt.ItemDataRole.UserRole + 31
LIMIT = 2000  # records shown at most (the newest)
# The title of each kind, as sandbox-ai's KINDS name them (shown translated).
TITLES = {
    "firewall": N_("connection dropped by the firewall"), "dns": N_("name lookup refused"),
    "network": N_("connection refused by the proxy"), "rule": N_("refused by a rule"),
    "approval-timeout": N_("refused without an answer"), "push": N_("push refused by the privacy check"),
    "publish": N_("publishing refused"), "mode": N_("refused by the security mode"),
    "policy": N_("refused by the administrator's policy"), "taint": N_("held: the run is tainted"),
    "device": N_("device refused"), "memory": N_("process ended: memory nearly full"),
    "supervisor-memory": N_("the supervisor dropped its caches"), "guard": N_("a safety check failed"),
    "canary": N_("a canary went off"), "oom": N_("out of memory: the kernel ended a process"),
}
# The grouped popup of each kind: {count} in the last minute, {target} where.
GROUPED = {
    "firewall": N_("{count} connections to {target} dropped by the firewall in the last minute"),
    "dns": N_("{count} lookups of {target} refused in the last minute"),
    "network": N_("{count} connections to {target} refused in the last minute"),
    "rule": N_("{count} actions refused by {target} in the last minute"),
    "approval-timeout": N_("{count} questions about {target} refused without an answer in the last minute"),
    "push": N_("{count} pushes to {target} refused in the last minute"),
    "publish": N_("{count} publishing requests to {target} refused in the last minute"),
}


def kind_title(kind):
    return tr(TITLES[kind]) if kind in TITLES else printable(kind)


def fix_label(record):
    return tr("Stays refused") if record.get("stays") else tr("To allow it")


def short_time(value):
    text = str(value or "")
    return text[:19].replace("T", " ") if len(text) >= 19 else text


def detail_text(record):
    """Everything a record says, one field per line, plain text."""
    rows = [(tr("Time"), short_time(record.get("time")) + " UTC"), (tr("Run"), record.get("run") or "-"),
            (tr("Kind"), f"{record.get('kind')} ({kind_title(record.get('kind'))})"),
            (tr("What"), record.get("what")), (tr("Process"), record.get("process")),
            (tr("Why"), record.get("why")), (fix_label(record), record.get("fix")),
            (tr("Rule"), record.get("rule")), (tr("Count"), record.get("count") if (record.get("count") or 1) > 1 else None),
            (tr("Seen by"), record.get("source")), ("id", record.get("id"))]
    width = max(len(name) for name, _ in rows)
    return "\n".join(f"{name.ljust(width)}  {printable(value)}" for name, value in rows if value not in (None, ""))


def popup(notification):
    """(level, title, text) of a Notifier notification, in the app's language: the title says the run and what
    happened (grouped: how often in the last minute), the text what, why and what to do."""
    run = printable(notification.get("run") or "sandbox-ai")
    kind, count = notification.get("kind"), notification.get("count") or 1
    if kind == "summary":
        return ("warning", tr("{run}: {count} more safeguard events", run=run, count=count),
                tr("Kinds: {kinds}. All of them: Activity > Safeguards", kinds=", ".join(notification.get("kinds") or [])))
    entry = notification.get("entry") or {}
    if count > 1:
        if kind in GROUPED:
            what = tr(GROUPED[kind], count=count, target=printable(entry.get("target") or "?"))
        else:
            what = tr("{title} ({count} times in the last minute)", title=kind_title(kind), count=count)
        title = f"{run}: {what}"
    else:
        title = f"{run}: {kind_title(kind)}"
    lines = [] if count > 1 else [printable(entry.get("what") or "")]
    if entry.get("why"):
        lines.append(tr("Why: {why}", why=printable(entry["why"])))
    if entry.get("fix"):
        lines.append(f"{fix_label(entry)}: {printable(entry['fix'])}")
    level = notification.get("level") if notification.get("level") in ("warning", "error") else "warning"
    return level, title, "\n".join(line for line in lines if line)


class SafeguardsView(QWidget):
    """Every safeguard record of every run (newest first); a row unfolds into the whole record below."""
    counted = Signal(int)  # records shown, for the tab's title

    def __init__(self, sai):
        super().__init__()
        self.sai, self.loaded_once, self.token, self.records = sai, False, 0, []
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.kind = QComboBox()
        self.kind.setAccessibleName(tr("Kind"))
        self.kind.addItem(tr("Every kind"), "")
        for kind in TITLES:
            self.kind.addItem(f"{kind_title(kind)} ({kind})", kind)
        self.kind.currentIndexChanged.connect(lambda _: self.show_records(self.records))
        self.status = plain_label(role="secondary", selectable=True)
        top.addWidget(self.refresh_button)
        top.addWidget(self.kind)
        top.addWidget(self.status, 1)
        layout.addLayout(top)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName(tr("Safeguards"))
        self.tree.setHeaderLabels([tr(title) for title in COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        tables.setup(self.tree, 3, (0, 1, 2))  # What takes the rest; time, run and kind their content
        self.tree.currentItemChanged.connect(lambda *_: self.show_details())
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        self.detail.setAccessibleName(tr("Details"))
        self.detail.setPlaceholderText(tr("Choose a record to see what was refused or ended, why, and what to do."))
        row = QHBoxLayout()
        self.copy_button = QPushButton(tr("Copy the fix"))
        self.copy_button.setToolTip(tr("The command or option that would allow it, for a terminal"))
        self.copy_button.clicked.connect(lambda _=False: self.copy_fix())
        self.copy_button.setEnabled(False)
        row.addWidget(self.copy_button)
        row.addWidget(plain_label(tr("Mute kinds: Settings > Window (they are still listed here)."), role="secondary"), 1)
        lower_layout.addWidget(self.detail, 1)
        lower_layout.addLayout(row)
        split.addWidget(self.tree)
        split.addWidget(lower)
        split.setSizes([420, 180])
        layout.addWidget(split, 1)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once:
            self.refresh()

    def refresh(self):
        """`sandbox-ai safeguards --all --json`, in the background."""
        self.loaded_once = True
        if not getattr(self.sai, "has_safeguards", lambda: False)():
            self.status.setText(tr("This sandbox-ai records no safeguards yet; update it with: sandbox-ai update"))
            return
        self.token += 1
        token = self.token
        self.status.setText(tr("Reading the safeguard records…"))
        background(self.sai.safeguards, owner=self, source=self.refresh_button, timeout=90,
                   done=lambda result, error: self.loaded(token, result, error))

    def loaded(self, token, result, error):
        if token != self.token:
            return
        if error is not None:
            self.status.setText(tr("Cannot read the safeguard records: {error}", error=printable(error)))
            return
        self.show_records([r for r in result or [] if isinstance(r, dict)])

    def add_live(self, records):
        """New records of running sandboxes (the popups' feed): shown at once, without a refresh."""
        if not records or not self.loaded_once:
            return
        known = {r.get("id") for r in self.records}
        fresh = [r for r in records if isinstance(r, dict) and r.get("id") not in known]
        if fresh:
            self.show_records(self.records + fresh)

    def show_records(self, records):
        self.records = sorted(records, key=lambda r: str(r.get("time") or ""))[-LIMIT:]
        wanted = self.kind.currentData()
        shown = [r for r in reversed(self.records) if not wanted or r.get("kind") == wanted]
        current = self.tree.currentItem()
        selected = current.data(0, RECORD_ROLE).get("id") if current is not None and \
            isinstance(current.data(0, RECORD_ROLE), dict) else None
        self.tree.clear()
        warn, bad = QBrush(theme.color("warn")), QBrush(theme.color("bad"))
        for record in shown:
            count = record.get("count") if isinstance(record.get("count"), int) and record["count"] > 1 else None
            item = QTreeWidgetItem([short_time(record.get("time")), printable(record.get("run") or "-"),
                                    kind_title(record.get("kind")),
                                    printable(record.get("what") or "") + (f"  ×{count}" if count else ""),
                                    printable(record.get("why") or ""),
                                    (tr("stays refused") + ": " if record.get("stays") else "")
                                    + printable(record.get("fix") or "")])
            item.setData(0, RECORD_ROLE, record)
            for column in (3, 4, 5):
                item.setToolTip(column, safe_tooltip(item.text(column)))  # (the sandbox's words: plain text)
            item.setForeground(2, bad if record.get("level") == "error" else warn)
            self.tree.addTopLevelItem(item)
            if selected and record.get("id") == selected:
                self.tree.setCurrentItem(item)
        tables.fit_columns(self.tree)
        runs = len({r.get("run") for r in shown})
        errors = sum(1 for r in shown if r.get("level") == "error")
        if shown:
            text = tr("{count} record(s) in {runs} run(s)", count=len(shown), runs=runs)
            if errors:
                text += " · " + tr("{count} error(s)", count=errors)
        else:
            text = tr("No safeguard refused or ended anything.")
        self.status.setText(text)
        self.counted.emit(len(self.records))

    def current(self):
        item = self.tree.currentItem()
        record = item.data(0, RECORD_ROLE) if item is not None else None
        return record if isinstance(record, dict) else None

    def show_details(self):
        record = self.current()
        self.copy_button.setEnabled(bool(record and record.get("fix") and not record.get("stays")))
        if record is None:
            self.detail.clear()
            return
        self.detail.setPlainText(detail_text(record))

    def copy_fix(self):
        record = self.current()
        if record and record.get("fix"):
            QGuiApplication.clipboard().setText(printable(record["fix"]))

    def recolor(self):
        if self.records:
            self.show_records(self.records)


class SafeguardMute(QWidget):
    """Which kinds of safeguard warnings are announced: a box per kind, ticked = announced. Unticked kinds go to
    "safeguard_mute" in sandbox-ai's defaults file, which the launcher's desktop notifications and the app's popups
    both keep to (the records are written all the same). PROBLEM(text) reports a defaults file that cannot be
    changed."""
    changed = Signal()

    def __init__(self, sai, problem=None, parent=None):
        super().__init__(parent)
        self.sai, self.problem, self.boxes = sai, problem, {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        kinds = sai.safeguard_kinds()
        muted = sai.muted_safeguards() if kinds else set()
        for index, (kind, (level, _title)) in enumerate(kinds.items()):
            box = QCheckBox(kind_title(kind))
            box.setToolTip(safe_tooltip(f"{kind} ({tr('error') if level == 'error' else tr('warning')})"))
            box.setChecked(kind not in muted)
            box.toggled.connect(lambda _on: self.save())
            grid.addWidget(box, index // 2, index % 2)
            self.boxes[kind] = box
        if not kinds:
            grid.addWidget(plain_label(tr("This sandbox-ai records no safeguards yet; update it."), role="secondary"),
                           0, 0)

    def muted(self):
        return [kind for kind, box in self.boxes.items() if not box.isChecked()]

    def save(self):
        muted = self.muted()
        try:
            self.sai.save_sandbox_default("safeguard_mute", muted or None)
        except (OSError, ValueError) as exc:
            if self.problem is not None:
                self.problem(printable(exc))
            return
        self.changed.emit()


class SafeguardAlerts(QObject):
    """The running sandboxes' new safeguard records: read in the background every INTERVAL milliseconds, each
    handed to safeguards.py's Notifier (grouping, rate limit, muted kinds), whose notifications NOTIFY(level,
    title, text) shows as the app's popups (setting notify_safeguards)."""
    arrived = Signal(list)  # the new records (the Safeguards tab adds them)

    def __init__(self, sai, notify, interval=3000, parent=None, clock=None):
        super().__init__(parent)
        self.sai, self.notify, self.reading, self.muted = sai, notify, None, set()
        self.notifier = None
        if getattr(sai, "has_safeguards", lambda: False)():
            module = sai.module("safeguards")
            self.tail = module.Tail()
            options = {"clock": clock} if clock is not None else {}
            self.notifier = module.Notifier(self.send, muted=lambda: self.muted, **options)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        if self.notifier is not None and interval:
            self.timer.start(interval)

    def read(self):
        """In a thread: the muted kinds and the new records of what runs now."""
        return self.sai.muted_safeguards(), self.tail.running(self.sai.runs())

    def tick(self):
        if self.reading is not None or self.notifier is None:
            return
        self.reading = background(self.read, timeout=60, done=self.read_done)

    def read_done(self, result, error):
        self.reading = None
        if error is not None or not result:
            return
        self.muted, records = result
        self.take(records)

    def take(self, records):
        for record in records:
            if isinstance(record, dict):
                self.notifier.add(record)
        self.notifier.flush()
        if records:
            self.arrived.emit(list(records))

    def send(self, notification):
        if not backend.setting("notify_safeguards"):
            return
        level, title, text = popup(notification)
        self.notify(level, title, text)
