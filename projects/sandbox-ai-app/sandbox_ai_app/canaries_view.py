"""The Canaries tab of the Activity page: every decoy file opened and every canary token that left a sandbox, in
the theme's "bad" colour, with who did it and what the agent did around it.

It shows `sandbox-ai canaries log --json`: per event its time, run, canary, path and action, the process (for a
file, the one the supervisor found holding it open right after inotify reported it; "unknown" when none was
found), and, unfolded, the agent's tool call in that time window, its prompt turn and the model requests before
and after it, each with the id of its activity record. Everything in it may quote the agent: plain text, control
characters escaped; it is redacted by the command and holds none of the run's tokens."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import theme
from .activity_page import detail_text, short_time
from .files import printable
from .i18n import N_, tr
from .widgets import MONO, background, plain_label

COLUMNS = (N_("Time"), N_("Event"), N_("Canary"), N_("Process"), N_("What"))
EVENT_ROLE = Qt.ItemDataRole.UserRole + 30
POSITIONS = {"before": N_("before"), "after": N_("after"), "same second": N_("same second"),
             "carried the token": N_("carried the token")}
MATCHES = {"names the file": N_("names the file"), "same time": N_("same time")}


def process_text(process):
    """One line about the process of an event: name, PIDs, command line, parents; or why it is unknown."""
    if not isinstance(process, dict) or not process or process.get("unknown") is not None:
        why = process.get("unknown") if isinstance(process, dict) else None
        return tr("unknown ({why})", why=printable(why)) if why else tr("unknown")
    text = f"{printable(process.get('comm') or '?')} (pid {printable(process.get('pid', '?'))}"
    if process.get("ns_pid") not in (None, process.get("pid")):
        text += tr(", {pid} in the sandbox", pid=printable(process["ns_pid"]))
    text += ")"
    argv = process.get("argv") if isinstance(process.get("argv"), list) else []
    if argv:
        text += ": " + printable(" ".join(str(a) for a in argv))
    parents = process.get("parents") if isinstance(process.get("parents"), list) else []
    if parents:
        text += " ← " + " ← ".join(printable(p) for p in parents[:3])
    return text


def link_lines(event):
    """[(text, ident)] of what EVENT is linked to: tool calls, the prompt turn, model requests."""
    lines = []
    for call in event.get("tool_calls") or []:
        if isinstance(call, dict):
            lines.append((tr("Tool call ({match}): {summary}  [{time}, activity {id}]",
                             match=tr(MATCHES.get(call.get("match"), str(call.get("match")))),
                             summary=printable(call.get("summary") or ""), time=short_time(call.get("time")),
                             id=printable(call.get("id") or "")), "tool"))
    turn = event.get("turn")
    if isinstance(turn, dict):
        lines.append((tr("Prompt turn: {summary}  [{time}, activity {id}]", summary=printable(turn.get("summary") or ""),
                         time=short_time(turn.get("time")), id=printable(turn.get("id") or "")), "turn"))
    for request in event.get("requests") or []:
        if not isinstance(request, dict):
            continue
        where = tr("activity {id}", id=printable(request["activity_id"])) if request.get("activity_id") else \
            f"{printable(request.get('log') or '')} @{printable(request.get('offset'))}"
        what = f"{printable(request.get('method') or '')} {printable(request.get('url') or request.get('host') or '')}"
        if request.get("status"):
            what += f" → {printable(request['status'])}"
        lines.append((tr("Model request ({position}): {request}  [{time}, {where}]",
                         position=tr(POSITIONS.get(request.get("position"), str(request.get("position")))),
                         request=what.strip(), time=short_time(request.get("time")), where=where),
                      "carried" if request.get("position") == "carried the token" else "request"))
    for other in (event.get("processes") or [])[1:]:
        lines.append((tr("Also held open by: {process}", process=process_text(other)), "process"))
    return lines


def events_of(logs):
    """Every event of LOGS (canaries log --json: one run's log, or a list of them), the newest first."""
    logs = logs if isinstance(logs, list) else [logs] if isinstance(logs, dict) else []
    found = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        for event in log.get("events") or []:
            if isinstance(event, dict):
                found.append({"agent": log.get("agent"), "linked": bool(log.get("activity")), **event})
    return sorted(found, key=lambda e: str(e.get("time") or ""), reverse=True)


class CanariesView(QWidget):
    """The canary log of every run (newest first): one row per event, unfolded to its links."""
    counted = Signal(int)  # events shown, for the tab's title

    def __init__(self, sai):
        super().__init__()
        self.sai, self.loaded_once, self.token = sai, False, 0
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.status = plain_label(role="secondary")
        top.addWidget(self.refresh_button)
        top.addWidget(self.status, 1)
        layout.addLayout(top)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr(title) for title in COLUMNS])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        for column, width in enumerate((130, 120, 260, 300)):
            self.tree.setColumnWidth(column, width)
        self.tree.currentItemChanged.connect(lambda *_: self.show_details())
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        self.detail.setPlaceholderText(tr("Choose an event to see everything recorded about it."))
        split.addWidget(self.tree)
        split.addWidget(self.detail)
        split.setSizes([420, 160])
        layout.addWidget(split, 1)
        self.events = []

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once:
            self.refresh()

    def refresh(self):
        """`sandbox-ai canaries log --json`, in the background."""
        self.loaded_once = True
        if not self.sai.has_canary_log():
            self.status.setText(tr("This sandbox-ai has no canary log yet; update it with: sandbox-ai update"))
            return
        self.token += 1
        token = self.token
        self.status.setText(tr("Reading the canary log…"))
        background(self.sai.canary_log, owner=self, source=self.refresh_button, timeout=90,
                   done=lambda result, error: self.loaded(token, result, error))

    def loaded(self, token, result, error):
        if token != self.token:
            return
        if error is not None:
            self.status.setText(tr("Cannot read the canary log: {error}", error=printable(error)))
            return
        self.show_events(events_of(result))

    def show_events(self, events):
        self.events = events
        self.tree.clear()
        self.detail.clear()
        bad, muted = QBrush(theme.color("bad")), QBrush(theme.color("muted"))
        bold = QFont()
        bold.setBold(True)
        for event in events:
            what = event.get("event") or "?"
            label = f"{printable(what)} ({printable(event['action'])})" if event.get("action") else printable(what)
            item = QTreeWidgetItem([short_time(event.get("time")), label,
                                    f"{printable(event.get('canary') or '-')}  {printable(event.get('path') or '')}",
                                    process_text(event.get("process")),
                                    printable(event.get("summary") or event.get("error") or "")])
            item.setData(0, EVENT_ROLE, event)
            item.setToolTip(4, printable(event.get("summary") or event.get("error") or ""))
            for column in range(len(COLUMNS)):
                item.setForeground(column, bad)
            item.setFont(1, bold)
            lines = link_lines(event)
            if not lines and event.get("canary"):
                lines = [(tr("Nothing of the agent's in that time window.") if event.get("linked") else
                          tr("No activity timeline for this run: nothing to link."), "none")]
            for text, kind in lines:
                child = QTreeWidgetItem([text])
                child.setFirstColumnSpanned(True)
                child.setData(0, EVENT_ROLE, event)
                if kind == "carried":
                    child.setForeground(0, bad)
                elif kind == "none":
                    child.setForeground(0, muted)
                item.addChild(child)
            self.tree.addTopLevelItem(item)
            for index in range(item.childCount()):
                item.child(index).setFirstColumnSpanned(True)
        runs = len({e.get("run") for e in events})
        self.status.setText(tr("{count} canary event(s) in {runs} run(s)", count=len(events), runs=runs) if events
                            else tr("No canary was touched: no decoy file opened, no token sent."))
        self.counted.emit(len(events))

    def show_details(self):
        item = self.tree.currentItem()
        event = item.data(0, EVENT_ROLE) if item is not None else None
        if not isinstance(event, dict):
            self.detail.clear()
            return
        self.detail.setPlainText(detail_text(event))

    def recolor(self):
        if self.events:
            self.show_events(self.events)
