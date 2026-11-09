"""The Canaries tab of the Activity page: every decoy file opened and every canary token that left a sandbox, in
the theme's "bad" colour, with who did it and what the agent did around it.

It shows `sandbox-ai canaries log --json`: per event its time, run, canary, path and action, the process (for a
file, the one the supervisor found holding it open right after inotify reported it; "unknown" when none was
found), and, unfolded, the agent's tool call in that time window, its prompt turn and the model requests before
and after it, each with the id of its activity record. Everything in it may quote the agent: plain text, control
characters escaped; it is redacted by the command and holds none of the run's tokens.

Below the log, one run's canaries (`sandbox-ai canaries list RUN --json`: the decoys with their tokens masked; in full
only after a question, canary-tokens) and Resume for a sandbox a canary paused (`sandbox-ai canaries resume RUN`,
asked first: canary-resume). Everything on this machine or the host the Activity page has chosen."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QFont
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import backend, tables, theme
from .activity_page import detail_text, short_time
from .files import printable
from .i18n import N_, tr
from .widgets import MONO, ask_first, background, outcome, plain_label, safe_tooltip, show_text

COLUMNS = (N_("Time"), N_("Event"), N_("Canary"), N_("Process"), N_("What"))
EVENT_ROLE = Qt.ItemDataRole.UserRole + 30
POSITIONS = {"before": N_("before"), "after": N_("after"), "same second": N_("same second"),
             "carried the token": N_("carried the token")}
MATCHES = {"names the file": N_("names the file"), "same time": N_("same time")}
LOCAL = "local"
RUN_SECONDS, RESUME_SECONDS = 90, 150


def run_text(data, tokens=False):
    """`sandbox-ai canaries list RUN --json` for the screen: the run, its decisions, each canary with its tokens
    (masked unless TOKENS), and the events. Everything escaped (a sandbox chose some of it)."""
    if not isinstance(data, dict):
        return ""
    decisions = data.get("decisions") if isinstance(data.get("decisions"), dict) else {}
    lines = [tr("Run {run} ({status}), started {started}", run=printable(data.get("run") or "?"),
                status=printable(data.get("status") or "?"), started=printable(data.get("started") or "?")),
             tr("On a read: {read}; when a token leaves: {send}", read=printable(decisions.get("on_read") or "?"),
                send=printable(decisions.get("on_send") or "?")), ""]
    for canary in data.get("canaries") or []:
        if not isinstance(canary, dict):
            continue
        values = canary.get("tokens") if isinstance(canary.get("tokens"), dict) else {}
        lines.append(f"{printable(canary.get('id') or '?')}  {printable(canary.get('sandbox_path') or '')}  "
                     f"({printable(canary.get('describe') or canary.get('category') or '')})")
        lines += [f"    {printable(key)} = {printable(value)}" for key, value in values.items()]
    if not data.get("canaries"):
        lines.append(tr("No canaries were planted."))
    lines.append("")
    events = [e for e in data.get("events") or [] if isinstance(e, dict)]
    lines += [f"{printable(e.get('time') or '')}  {printable(e.get('canary') or '-')}  {printable(e.get('event') or '')}"
              f"  {printable(e.get('action') or '')}  {printable(e.get('summary') or e.get('error') or '')}"
              for e in events] or [tr("Nothing happened to them.")]
    if tokens:
        lines += ["", tr("These are the run's decoy tokens in full: worthless, but planted elsewhere they could "
                         "raise false alarms.")]
    return "\n".join(lines)


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
        self.sai, self.loaded_once, self.token, self.host = sai, False, 0, LOCAL
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
        tables.setup(self.tree, 4, (0, 1))
        self.tree.currentItemChanged.connect(lambda *_: self.show_details())
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        self.detail.setPlaceholderText(tr("Choose an event to see everything recorded about it."))
        split.addWidget(self.tree)
        split.addWidget(self.detail)
        split.setSizes([420, 160])
        layout.addWidget(split, 1)
        # One run's canaries, their tokens (asked first) and Resume.
        runs = QHBoxLayout()
        self.run_box = QComboBox()
        self.run_box.setAccessibleName(tr("Run"))
        self.run_box.setToolTip(tr("The runs with canaries (sandbox-ai canaries list)"))
        self.run_box.setMinimumWidth(240)
        self.show_button = QPushButton(tr("Canaries of the run"))
        self.show_button.setToolTip(tr("sandbox-ai canaries list RUN: its decoys, tokens masked, and its events"))
        self.tokens_button = QPushButton(tr("Show the tokens…"))
        self.tokens_button.setToolTip(tr("sandbox-ai canaries list RUN --tokens: the decoy tokens in full"))
        self.resume_button = QPushButton(tr("Resume…"))
        self.resume_button.setToolTip(tr("sandbox-ai canaries resume RUN: continue a sandbox a canary paused"))
        self.show_button.clicked.connect(lambda _=False: self.show_run(False))
        self.tokens_button.clicked.connect(lambda _=False: self.show_run(True))
        self.resume_button.clicked.connect(lambda _=False: self.resume())
        runs.addWidget(plain_label(tr("Run"), word_wrap=False))
        for widget in (self.run_box, self.show_button, self.tokens_button, self.resume_button):
            runs.addWidget(widget)
        runs.addStretch(1)
        layout.addLayout(runs)
        self.events = []
        self.update_run_buttons()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once:
            self.refresh()

    def set_host(self, host):
        """The log of HOST from now on (the Activity page's host chooser)."""
        if host != self.host:
            self.host = host
            self.tree.clear()
            self.detail.clear()
            self.run_box.clear()
            self.update_run_buttons()
            if self.loaded_once:
                self.refresh()

    def refresh(self):
        """`sandbox-ai canaries log --json` and the runs with canaries, in the background (on the host chosen)."""
        self.loaded_once = True
        host = self.host
        if host == LOCAL and not self.sai.has_canary_log():
            self.status.setText(tr("This sandbox-ai has no canary log yet; update it with: sandbox-ai update"))
            return
        self.token += 1
        token = self.token
        self.status.setText(tr("Reading the canary log…"))
        if host == LOCAL:
            background(self.sai.canary_log, owner=self, source=self.refresh_button, timeout=90,
                       done=lambda result, error: self.loaded(token, result, error))
        else:
            background(lambda: self.sai.host_canary_log(host), owner=self, source=self.refresh_button, timeout=120,
                       done=lambda result, error: self.loaded(token, result, error))
        background(lambda: self.sai.canary_runs(host), owner=self, timeout=RUN_SECONDS,
                   done=lambda result, error: self.runs_listed(token, result, error))

    def runs_listed(self, token, result, error):
        if token != self.token:
            return
        current = self.run_box.currentData()
        self.run_box.clear()
        for entry in result if error is None and isinstance(result, list) else []:
            run = entry.get("run")
            if not backend.valid_run(run):
                continue
            self.run_box.addItem(tr("{run} ({status}, {events} events, {alerts} alerts)", run=printable(run),
                                    status=printable(entry.get("status") or "?"), events=entry.get("events", 0),
                                    alerts=entry.get("alerts", 0)), run)
        index = self.run_box.findData(current)
        self.run_box.setCurrentIndex(max(index, 0))
        self.update_run_buttons()

    def update_run_buttons(self):
        for button in (self.show_button, self.tokens_button, self.resume_button):
            button.setEnabled(self.run_box.count() > 0)

    def chosen_run(self):
        run = self.run_box.currentData()
        return run if backend.valid_run(run) else None

    def show_run(self, tokens):
        """One run's canaries; with TOKENS the decoy tokens in full, after a question (canary-tokens)."""
        run, host = self.chosen_run(), self.host
        if run is None:
            return
        if tokens and not ask_first(self, "canary-tokens", tr("Show the tokens…").rstrip("…"), tr(
                "Show the canary tokens of {run} in full? They are decoys made for the run, but shown here they can be "
                "copied and planted somewhere else.", run=printable(run)), tr("Show")):
            return
        background(lambda: self.sai.canary_run(host, run, tokens=tokens), owner=self,
                   source=[self.show_button, self.tokens_button], timeout=RUN_SECONDS + 15,
                   done=lambda result, error: self.run_shown(run, tokens, result, error))

    def run_shown(self, run, tokens, result, error):
        if error is not None:
            self.status.setText(tr("Cannot read the canaries of {run}: {error}", run=printable(run),
                                   error=printable(error)[:500]))
            return
        show_text(self, tr("Canaries of {run}", run=printable(run)), run_text(result, tokens), modal=False)

    def resume(self):
        """`sandbox-ai canaries resume RUN`: a sandbox a canary paused goes on, after a question (canary-resume)."""
        run, host = self.chosen_run(), self.host
        if run is None:
            return
        if not ask_first(self, "canary-resume", tr("Resume…").rstrip("…"), tr(
                "Resume {run}? A canary paused it: something opened a decoy or sent a token away. Look at the log "
                "first; resumed, the sandbox goes on with what it holds.", run=printable(run)), tr("Resume")):
            return
        background(lambda: self.sai.canary_resume(host, run), owner=self, work=True, source=self.resume_button,
                   timeout=RESUME_SECONDS + 15, done=lambda result, error: self.resumed(run, result, error))

    def resumed(self, run, result, error):
        status, out, err = outcome(result, error)
        said = printable((out + err).strip()[-500:])
        self.status.setText(tr("{run} resumed.", run=printable(run)) + (f" {said}" if said else "") if status == 0
                            else tr("Not resumed: {error}", error=said or status))

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
            item.setToolTip(4, safe_tooltip(printable(event.get("summary") or event.get("error") or "")))
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
        tables.fit_columns(self.tree)
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
