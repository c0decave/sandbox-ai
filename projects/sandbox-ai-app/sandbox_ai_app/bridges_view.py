"""Session bridges on the Sandboxes page: the bridges between running sessions (also marked on both sessions' rows),
New bridge… (a dialog: the two sessions, the capabilities with path pickers, both directions, until, the ask mode,
the observer's actions each on its own, the limits under Advanced), Show, Revoke and Resume - each through
`sandbox-ai bridge` in the background, never on the GUI thread. Before a bridge is made the launcher's dry run says
what it warns about (data that can flow from the more restricted session to the less restricted one above all) and
the human confirms exactly that."""
from __future__ import annotations

import json
import os
import re
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from . import bridges as BR
from . import widgets
from .files import printable, printable_text
from .i18n import tr
from .widgets import (
    ask_first, background, explanation, flow, inform, plain_label, safe_tooltip, set_enabled, show_text, warn,
)

LOCAL = "local"
MAX_ROWS = 6  # rows the list of bridges shows before it scrolls


REMOTE_WAIT = 60  # seconds one host may take to list its starts (as the Sandboxes page waits)


def alive(widget):
    """Whether WIDGET's C++ object still exists (a host's late answer must not touch a dialog closed meanwhile)."""
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:  # pragma: no cover - PySide6 always has it
        return True


def candidates(instances, remote=False):
    """[{name, run, agent, workspace, host}] of the agent sessions that run (what a bridge may join): no MCP server's
    sandbox, no helper, nothing paused or stopped. This machine's (host None); with REMOTE also those of the hosts of
    `sandbox-ai hosts` (as `sandbox-ai hosts run HOST -- list --json` lists them), named HOST:NAME as `bridge add`
    takes them - the launcher bridges one of this machine with one of another host."""
    found = []
    for instance in instances:
        record = instance.record if isinstance(getattr(instance, "record", None), dict) else {}
        host = getattr(instance, "host", LOCAL) or LOCAL
        if host != LOCAL and (not remote or not BR.HOST.fullmatch(str(host))):
            continue
        # (this machine's record is the run's, kind "agent"; a host's is its `list --json` entry, kind "session")
        if instance.state != "running" or (host == LOCAL and not instance.run) or \
                instance.kind not in ("session", "run") or \
                record.get("kind") not in (None, "agent", "session", "run") or \
                record.get("helper_of") or not BR.NAME.fullmatch(str(instance.name)):
            continue
        found.append({"name": instance.name if host == LOCAL else f"{host}:{instance.name}",
                      "run": instance.run or record.get("run") or "", "host": None if host == LOCAL else host,
                      "agent": instance.agent or record.get("agent") or "",
                      "workspace": record.get("workspace") or instance.project or ""})
    return sorted(found, key=lambda entry: (entry["host"] is not None, entry["name"]))


class PathList(QWidget):
    """Paths of the peer's workspace, one per line, and Choose folder… (the peer's workspace opened - on its host,
    listed over SSH (remote_chooser), for a session of another host -, the folder taken relative to it)."""

    def __init__(self, workspace_of, title, host_of=None):
        super().__init__()
        self.workspace_of = workspace_of  # () -> the peer's workspace now
        self.host_of = host_of or (lambda: None)  # () -> the peer's host (None: this machine)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(tr("one path per line, relative to its workspace (. for all of it)"))
        self.edit.setAccessibleName(title)
        self.edit.setFixedHeight(58)
        self.edit.setTabChangesFocus(True)
        self.choose = QPushButton(tr("Choose folder…"))
        self.choose.setToolTip(tr("A folder of the other session's workspace (the path is taken relative to it)"))
        self.choose.clicked.connect(lambda _=False: self.pick())
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.choose, 0, Qt.AlignmentFlag.AlignTop)

    def paths(self):
        return [line.strip() for line in self.edit.toPlainText().splitlines() if line.strip()]

    def add(self, path):
        if path not in self.paths():
            self.edit.setPlainText("\n".join([*self.paths(), path]))

    def pick(self):
        workspace = self.workspace_of()
        host = self.host_of()
        if host:  # a session of another host: its folder is there - listed there, never this machine's of that name
            if not workspace or not str(workspace).startswith("/"):
                warn(self, tr("New bridge"), tr("The workspace of the other session is not known here: type the "
                                                "path."))
                return
            from . import remote_chooser as RC
            chosen = RC.choose_folder(self, host, tr("A folder of the other session's workspace"), "bridge",
                                      start=workspace)
        elif not workspace or not os.path.isdir(workspace):
            warn(self, tr("New bridge"), tr("The workspace of the other session is not known here: type the path."))
            return
        else:
            chosen = QFileDialog.getExistingDirectory(self, tr("A folder of the other session's workspace"),
                                                      workspace)
        if not chosen:
            return
        try:
            self.add(BR.relative_path(chosen, workspace))
        except ValueError as exc:
            warn(self, tr("New bridge"), printable_text(exc))


class BridgeDialog(QDialog):
    """New bridge…: the two sessions (A reaches B), what A may do to B, both directions, until, the ask mode, the
    observer's actions each on its own, and the limits under Advanced. values() says what was chosen (ValueError: why
    not), in the form backend.SandboxAI.bridge_add_args takes.

    SESSIONS: this machine's sessions that run (candidates), and those of other hosts already known (HOST:NAME). With
    SAI the sessions that run on the user's hosts (HOSTS: their names; None: `sandbox-ai hosts` read first) are listed
    afresh in the background, each host apart (`sandbox-ai hosts run HOST -- list --json`), and offered as HOST:NAME
    once they answer (bridge add takes HOST:NAME): with one of this machine, two of one host (made there) or two of
    two hosts (relayed by this machine) - the dialog says which."""

    def __init__(self, parent, sessions, first=None, second=None, sai=None, hosts=None, draft=None):
        super().__init__(parent)
        self.setWindowTitle(tr("New bridge"))
        self.sessions = {entry["name"]: entry for entry in sessions}
        self.host_problems, self.asking = {}, set()
        form = QFormLayout(self)
        form.addRow(explanation(tr("A bridge lets session A reach session B through a broker on their host (never a "
                                   "path between the sandboxes): each gets an MCP server bridge-PEER. Only you make, "
                                   "widen or end one.")))
        self.a, self.b = QComboBox(), QComboBox()
        for combo, name in ((self.a, tr("Session A")), (self.b, tr("Session B"))):
            combo.setAccessibleName(name)
        self.fill_sessions()
        names = [self.a.itemData(index) for index in range(self.a.count())]
        if first in names:
            self.a.setCurrentIndex(names.index(first))
        local = [name for name in names if not BR.peer_host(name)]
        other = second if second in names and second != self.a.currentData() else \
            next((name for name in local + names if name != self.a.currentData()), None)
        if other is not None:
            self.b.setCurrentIndex(names.index(other))
        form.addRow(tr("Session A (reaches B)"), self.a)
        form.addRow(tr("Session B"), self.b)
        self.hosts_note = plain_label(role="secondary")
        self.hosts_note.setVisible(False)
        form.addRow(self.hosts_note)
        caps = QGroupBox(tr("What A may do to B"))
        caps_form = QFormLayout(caps)
        self.message = QCheckBox(tr("Send messages (message)"))
        self.message.setToolTip(tr("A sends B's agent a note; B reads it with its bridge's inbox tool"))
        self.ask = QCheckBox(tr("Ask questions and wait for the answer (ask)"))
        self.ask.setToolTip(tr("A asks B's agent and waits for its answer (with a timeout and size limits)"))
        caps_form.addRow(flow(self.message, self.ask))
        self.read = PathList(self.peer_workspace, tr("Read files"), self.peer_host)
        self.write = PathList(self.peer_workspace, tr("Write files"), self.peer_host)
        caps_form.addRow(tr("Read files (files:ro)"), self.read)
        caps_form.addRow(tr("Write files (files:rw)"), self.write)
        caps_form.addRow(explanation(tr("Every write is asked on the Approvals page, with its diff, whatever the ask "
                                        "mode says. .git is never reached.")))
        self.watch = QCheckBox(tr("Observe B (watch)"))
        self.watch.setToolTip(tr("A sees B's activity, a summary of its LLM traffic, the changes in its workspace and "
                                 "its terminal, and may alert you"))
        caps_form.addRow(self.watch)
        self.actions = {}
        for action, text, tip in (
                ("alert", tr("Alert you"), tr("The observer raises an alert: a notification and a line on the bridge")),
                ("hint", tr("Hint"), tr("The observer puts a note into B's inbox")),
                ("net-cut", tr("Cut B's network"), tr("The observer pulls B's kill switch: only its model API stays "
                                                       "reachable; the bridge is suspended")),
                ("pause", tr("Pause B"), tr("The observer freezes B; only you resume it"))):
            box = QCheckBox(text)
            box.setToolTip(tip)
            box.setChecked(action in BR.DEFAULT_ACTIONS)
            self.actions[action] = box
        self.actions_row = flow(*self.actions.values())
        caps_form.addRow(tr("The observer may"), self.actions_row)
        self.watch.toggled.connect(lambda _=False: self.update_state())
        form.addRow(caps)
        self.both = QCheckBox(tr("Both directions (B may do the same to A)"))
        self.both.setToolTip(tr("--both: B gets the same capabilities over A; its paths are in A's workspace"))
        form.addRow(self.both)
        self.until = QLineEdit()
        self.until.setPlaceholderText(tr("30m, 2h, 1d, HH:MM or an ISO time; empty: until either session ends"))
        self.until.setAccessibleName(tr("Until"))
        form.addRow(tr("Until"), self.until)
        self.ask_mode = QComboBox()
        self.ask_mode.setAccessibleName(tr("Ask you"))
        for key, text in BR.ASK_MODES:
            self.ask_mode.addItem(tr(text), key)
        self.ask_mode.setToolTip(tr("Which calls that move content across are asked on the Approvals page (--ask)"))
        form.addRow(tr("Ask you about"), self.ask_mode)
        self.advanced = widgets.disclosure(QToolButton())
        self.advanced.setText(tr("Advanced: limits"))
        form.addRow(self.advanced)
        self.limits_box = QWidget()
        limits_form = QFormLayout(self.limits_box)
        limits_form.setContentsMargins(0, 0, 0, 0)
        self.limits = {}
        for key, title, default, tip in BR.LIMITS:
            edit = QLineEdit()
            edit.setPlaceholderText(tr("default: {value}", value=default))
            edit.setToolTip(tr(tip))
            edit.setAccessibleName(tr(title))
            self.limits[key] = edit
            limits_form.addRow(tr(title), edit)
        self.limits_box.setVisible(False)
        self.advanced.toggled.connect(self.limits_box.setVisible)
        form.addRow(self.limits_box)
        self.note = plain_label(role="secondary")
        form.addRow(self.note)
        self.problem = plain_label(role="bad")
        form.addRow(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.addButton(tr("Check and create…"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setToolTip(tr("The launcher checks the bridge and says its warnings first (sandbox-ai bridge add "
                              "--dry-run); then you confirm"))
        buttons.accepted.connect(self.try_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        for combo in (self.a, self.b):
            combo.currentIndexChanged.connect(lambda _=0: self.update_state())
        for box in (self.message, self.ask, self.both, self.actions["hint"]):
            box.toggled.connect(lambda _=False: self.update_state())
        self.update_state()
        # What was chosen and not made (Cancel, a "no", a refusal): filled in again for the same pair (drafts.py).
        from . import drafts
        self.draft = None if draft is None else drafts.Draft(self, draft, {
            "a": self.a, "b": self.b, "message": self.message, "ask": self.ask, "read": self.read.edit,
            "write": self.write.edit, "watch": self.watch, **{f"action:{k}": box for k, box in self.actions.items()},
            "both": self.both, "until": self.until, "ask_mode": self.ask_mode,
            **{f"limit:{k}": edit for k, edit in self.limits.items()}})
        if self.draft is not None:
            self.update_state()
        if sai is not None:
            self.read_hosts(sai, hosts)

    # -- the sessions: this machine's, and those of the user's hosts as they answer --

    def fill_sessions(self):
        """Both lists from self.sessions (this machine's first, then each host's), each keeping what was chosen."""
        entries = sorted(self.sessions.values(), key=lambda e: (BR.peer_host(e["name"]) is not None, e["name"]))
        for combo in (self.a, self.b):
            chosen = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for entry in entries:
                combo.addItem(f"{printable(entry['name'])} ({printable(entry.get('agent') or '?')})", entry["name"])
            index = combo.findData(chosen) if chosen is not None else -1
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def read_hosts(self, sai, hosts=None):
        """The user's hosts (HOSTS, or `sandbox-ai hosts` read in the background), then each host's sessions that
        run, each in a read of its own (a host that does not answer holds up none of the others)."""
        if hosts is None:
            background(sai.remote_hosts, timeout=30,
                       done=lambda found, error: alive(self) and self.hosts_listed(sai, found, error))
        else:
            self.hosts_listed(sai, hosts, None)

    def hosts_listed(self, sai, hosts, error):
        if error is not None:
            self.host_problems["sandbox-ai hosts"] = printable_text(error)[:200]
            self.show_hosts()
            return
        for host in [h for h in hosts or () if isinstance(h, str) and BR.HOST.fullmatch(h) and h != LOCAL]:
            self.asking.add(host)
            background(sai.host_instances, host, REMOTE_WAIT, timeout=REMOTE_WAIT + 15,
                       done=lambda found, error_, h=host: alive(self) and self.host_listed(h, found, error_))
        self.show_hosts()

    def host_listed(self, host, found, error):
        """HOST's sessions that run (or why it did not answer): offered as HOST:NAME; those it no longer lists go."""
        self.asking.discard(host)
        if error is not None:
            self.host_problems[host] = printable_text(error)[:200]
        else:
            self.host_problems.pop(host, None)
            fresh = {entry["name"]: entry for entry in candidates(
                [i for i in found or () if getattr(i, "host", None) == host], remote=True)}
            self.sessions = {**{name: entry for name, entry in self.sessions.items() if BR.peer_host(name) != host},
                             **fresh}
            self.fill_sessions()
            self.update_state()
        self.show_hosts()

    def show_hosts(self):
        lines = []
        if self.asking:
            lines.append(tr("Asking your hosts for their running sessions: {hosts}…",
                            hosts=", ".join(printable(h) for h in sorted(self.asking))))
        lines += [tr("{host}: no answer", host=printable(host)) + f" ({problem})"
                  for host, problem in sorted(self.host_problems.items())]
        self.hosts_note.setText("\n".join(lines))
        self.hosts_note.setVisible(bool(lines))

    def peer_workspace(self):
        return (self.sessions.get(self.b.currentData()) or {}).get("workspace") or ""

    def peer_local(self):
        return not BR.peer_host(self.b.currentData())

    def peer_host(self):
        """The host of the session chosen as B (None: this machine)."""
        return BR.peer_host(self.b.currentData())

    def update_state(self):
        for box in self.actions.values():
            box.setEnabled(self.watch.isChecked())
        notes = []
        where = BR.placement_note(self.a.currentData(), self.b.currentData())
        if where:
            notes.append(where)
        sees = BR.peer_sees_tools(self.message.isChecked(), self.ask.isChecked(),
                                  self.watch.isChecked() and self.actions["hint"].isChecked(), self.both.isChecked())
        for combo in (self.a, self.b) if sees else (self.a,):  # (B only watched or read: it needs no tool)
            entry = self.sessions.get(combo.currentData()) or {}
            if BR.mcp_at_start(entry.get("agent")):
                notes.append(tr("{name} runs {agent}, which reads its MCP servers only when it starts: its relay opens "
                                "now, but its agent does not see the bridge (a restart does not keep a bridge).",
                                name=printable(entry.get("name")), agent=printable(entry.get("agent"))))
        self.note.setText("\n".join(notes))
        self.note.setVisible(bool(notes))

    def values(self):
        """{a, b, allow, both, until, ask, observer_actions, limits} (ValueError: what is missing or wrong)."""
        caps = BR.capabilities(self.message.isChecked(), self.ask.isChecked(), self.read.paths(), self.write.paths(),
                               self.watch.isChecked(), self.peer_workspace())
        return BR.add_values(self.a.currentData(), self.b.currentData(), caps, both=self.both.isChecked(),
                             until=self.until.text(), ask=self.ask_mode.currentData(), watch=self.watch.isChecked(),
                             actions=[key for key, box in self.actions.items() if box.isChecked()],
                             limits={key: edit.text() for key, edit in self.limits.items()})

    def try_accept(self):
        try:
            self.values()
        except ValueError as exc:
            self.problem.setText(printable_text(exc))
            return
        self.accept()


def plan_text(plan, values):
    """The question before a bridge is made: who reaches whom with what, the ask mode, until, and every warning the
    launcher's dry run gave (the egress direction first)."""
    grants = plan.get("grants") if isinstance(plan.get("grants"), dict) else {}
    a, b = printable(values["a"]), printable(values["b"])
    lines = [tr("{a} may reach {b}: {caps}", a=a, b=b, caps=printable(",".join(values["allow"])))]
    if values.get("both"):
        lines.append(tr("{a} may reach {b}: {caps}", a=b, b=a, caps=printable(",".join(values["allow"]))))
    actions = (grants.get("a") or {}).get("actions") if isinstance(grants.get("a"), dict) else None
    if actions:
        lines.append(tr("The observer may: {actions}", actions=", ".join(printable(x) for x in actions)))
    lines.append(tr("Asked: {mode}; every write is asked with its diff.",
                    mode=tr(dict(BR.ASK_MODES).get(values.get("ask"), "the first call of each tool"))))
    lines.append(tr("Until {time}", time=printable(plan["until"])) if plan.get("until") else
                 tr("Until either session ends (it is not restored on resume)."))
    warnings = [printable(w) for w in plan.get("warnings") or [] if isinstance(w, str)]
    egress = [w for w in warnings if "data can flow from" in w]
    if egress:
        lines += ["", tr("Data can flow from a more restricted session to a less restricted one:")]
        lines += [f"  ⚠ {w}" for w in egress]
    others = [w for w in warnings if w not in egress]
    if others:
        lines += ["", tr("The launcher also warns:")] + [f"  • {w}" for w in others]
    return "\n".join(lines)


def create(parent, sai, values, args, plan, then=None, source=None):
    """Ask (bridge-add, Cancel the default) whether the bridge of VALUES may be made, with what the launcher's dry run
    PLAN warns about; then `sandbox-ai bridge add ARGS` in the background, its outcome said. THEN(result or None, error
    or None) once it is made, refused or declined. True when it is being made."""
    text = tr("Create this bridge?") + "\n\n" + plan_text(plan if isinstance(plan, dict) else {}, values)
    if not ask_first(parent, "bridge-add", tr("New bridge"), text, tr("Create bridge")):
        if then is not None:
            then(None, None)
        return False

    def added(result, error):
        if then is not None:
            then(result, error)
        if error is not None:
            warn(parent, tr("Not bridged"), printable_text(error), modal=False)
            return
        data = result if isinstance(result, dict) else {}
        notes = [printable(n) for n in data.get("notes") or [] if isinstance(n, str)]
        said = tr("Bridge {id} is active: {a} and {b} see each other as MCP servers bridge-….",
                  id=printable(data.get("id") or "?"), a=printable(data.get("a") or values["a"]),
                  b=printable(data.get("b") or values["b"]))
        inform(parent, tr("New bridge"), said + ("\n\n" + "\n".join(notes) if notes else ""), modal=False)
    background(sai.bridge_add, args, owner=parent, source=source, timeout=120, work=True, done=added)
    return True


# The launcher's words while a session of a bridge does not run yet (bridge.find_side): a Board's start waits.
NOT_YET = re.compile(r"nothing named \S+ runs|does not run \(|\S+ is paused")
WAIT_SECONDS = 600   # how long a Board's bridge waits for both sessions to run
WAIT_EVERY = 3000    # ms between two looks (each a dry run of the bridge, in the background)


def remote_sides(values):
    """[(host, name)] of the sessions of other hosts a bridge's VALUES join (bridge add's HOST:NAME)."""
    found = []
    for side in ("a", "b"):
        host = BR.peer_host(values.get(side))
        if host:
            found.append((host, str(values[side]).split(":", 1)[1]))
    return found


class BridgeWaiter(QObject):
    """The Bridge parts of a Board's start: each made once both its sessions run - a session of another host once that
    host lists it as running (asked every WAIT_EVERY ms in the background; the launcher's dry run does not ask the
    other host), one of this machine once the dry run no longer says it does not run (at most WAIT_SECONDS) - then its
    warnings confirmed (create) and the bridge made. What the launcher refuses for another reason is said and that
    bridge dropped. What still waits is kept in the app's state (kept()): the next start of the app waits on until
    the same time (R2-21: a quit ended the wait without a word)."""
    finished = Signal()
    changed = Signal()  # an entry is done (made, refused, given up): what kept() says changed

    def __init__(self, parent, sai, entries, every=None, seconds=None, clock=time.monotonic):
        super().__init__(parent)
        self.page, self.sai, self.clock = parent, sai, clock
        self.pending = list(entries)  # [(values, args)]
        seconds = WAIT_SECONDS if seconds is None else seconds
        self.flight, self.deadline, self.made = None, clock() + seconds, []
        self.until = time.time() + seconds  # (the same end as a time of the clock on the wall, for the next start)
        self.running = set()  # (host, name) of the sessions of other hosts seen running
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.look)
        self.timer.start(every or WAIT_EVERY)
        QTimer.singleShot(0, self.look)

    def look(self):
        if self.flight is not None:
            return
        if not self.pending:
            self.stop()
            return
        values, args = self.pending[0]
        missing = [side for side in remote_sides(values) if side not in self.running]
        if missing:
            host, name = missing[0]
            self.flight = background(self.sai.host_instances, host, REMOTE_WAIT, owner=self.page,
                                     timeout=REMOTE_WAIT + 15,
                                     done=lambda found, error: self.host_seen(values, args, host, name, found, error))
            return
        self.flight = background(self.sai.bridge_plan, args, owner=self.page, timeout=120,
                                 done=lambda plan, error: self.planned(values, args, plan, error))

    def host_seen(self, values, args, host, name, found, error):
        """What HOST listed (or why not): NAME runs there - the bridge is planned at once -, or it is asked again at
        the next look until WAIT_SECONDS have passed."""
        self.flight = None
        names = {entry["name"] for entry in candidates([i for i in found or () if getattr(i, "host", None) == host],
                                                       remote=True)} if error is None else set()
        if f"{host}:{name}" in names:
            self.running.add((host, name))
            QTimer.singleShot(0, self.look)
            return
        if self.clock() < self.deadline:
            return
        self.pending = [entry for entry in self.pending if entry[1] != args]
        self.changed.emit()
        warn(self.page, tr("Not bridged"), tr("The board's bridge {a} → {b} was not made: {error}",
                                              a=printable(values["a"]), b=printable(values["b"]),
                                              error=tr("{name} did not run on {host}", name=printable(name),
                                                       host=printable(host)) +
                                              (f" ({printable_text(error)[:200]})" if error is not None else "")),
             modal=False)
        if not self.pending:
            self.stop()

    def planned(self, values, args, plan, error):
        self.flight = None
        if error is not None and NOT_YET.search(str(error)) and self.clock() < self.deadline:
            return  # not both running yet: asked again at the next look
        self.pending = [entry for entry in self.pending if entry[1] != args]
        self.changed.emit()
        if error is not None:
            warn(self.page, tr("Not bridged"), tr("The board's bridge {a} → {b} was not made: {error}",
                                                  a=printable(values["a"]), b=printable(values["b"]),
                                                  error=printable_text(error)), modal=False)
        else:
            create(self.page, self.sai, values, args, plan,
                   then=lambda result, error_: self.made.append(result) if result else None)
        if not self.pending:
            self.stop()

    def stop(self):
        self.timer.stop()
        self.pending = []
        self.finished.emit()

    def kept(self):
        """[{"values", "until"}] of the bridges that still wait (values as bridge_add_args takes them; until: the end
        of the wait, seconds since the epoch), for the app's state."""
        return [{"values": json.loads(json.dumps(values)), "until": round(self.until, 1)}
                for values, _args in self.pending]


def kept_waits(sai, rows):
    """(entries, problems) of the waits the app's state kept (BridgeWaiter.kept): [(until, values, args)] of those
    whose values a bridge takes (checked as bridge_add_args checks them: names, capabilities), and a line for each
    one that cannot be used."""
    entries, problems = [], []
    keys = {"a", "b", "allow", "both", "until", "ask", "observer_actions", "limits"}
    for row in rows if isinstance(rows, list) else []:
        values = row.get("values") if isinstance(row, dict) else None
        until = row.get("until") if isinstance(row, dict) else None
        try:
            if not isinstance(values, dict) or not set(values) <= keys or not isinstance(until, (int, float)) or \
                    isinstance(until, bool):
                raise ValueError(tr("not a bridge's values"))
            values = dict(values)
            limits = values.get("limits") or []
            if not isinstance(limits, list) or not all(isinstance(pair, list) and len(pair) == 2 and
                                                        all(isinstance(v, str) for v in pair) for pair in limits):
                raise ValueError(tr("not a bridge's values"))
            values["limits"] = [tuple(pair) for pair in limits]
            if not isinstance(values.get("allow"), list) or not all(isinstance(cap, str) for cap in values["allow"]):
                raise ValueError(tr("choose at least one capability"))
            entries.append((float(until), values, sai.bridge_add_args(**values)))
        except (TypeError, ValueError) as exc:
            a = values.get("a") if isinstance(values, dict) else None
            b = values.get("b") if isinstance(values, dict) else None
            problems.append(tr("{a} → {b}: {error}", a=printable(a or "?"), b=printable(b or "?"),
                               error=printable_text(str(exc))[:200]))
    return entries, problems


def show_lines(document):
    """`bridge show --json` as lines for a window: grant both ways, state, limits, counters, warnings, alerts, calls."""
    data = document if isinstance(document, dict) else {}
    a, b = data.get("a") or {}, data.get("b") or {}
    grants = data.get("grants") or {}

    def grant_text(grant):
        if not isinstance(grant, dict):
            return "-"
        caps = [name for name in ("message", "ask", "watch") if grant.get(name)]
        caps += [f"files:{e.get('mode')}:{e.get('path')}" for e in grant.get("files") or [] if isinstance(e, dict)]
        return ",".join(printable(c) for c in caps) or "-"
    state = tr(BR.STATE_TITLES.get(data.get("state"), "?")) if data.get("state") in BR.STATE_TITLES else \
        printable(data.get("state"))
    lines = [tr("Bridge {id}: {state}", id=printable(data.get("id")), state=state)]
    if data.get("reason"):
        lines.append("  " + printable(BR.reason_text(data["reason"]))[:400])
    lines.append(tr("Made {time}", time=printable(data.get("created") or "?")) + "; " +
                 (tr("until {time}", time=printable(data["until"])) if data.get("until") else
                  tr("until either session ends")))
    for actor, peer in ((a, b), (b, a)):
        side_grant = grants.get("a" if actor is a else "b")
        line = tr("{a} -> {b}: {caps}", a=printable(actor.get("name") or "?"), b=printable(peer.get("name") or "?"),
                  caps=grant_text(side_grant))
        if isinstance(side_grant, dict) and side_grant.get("watch"):
            line += "; " + tr("observer actions: {actions}",
                              actions=", ".join(printable(x) for x in side_grant.get("actions") or []) or "-")
        if actor.get("server"):
            line += "; " + tr("sees MCP server {name}", name=printable(actor["server"]))
        lines.append(line)
    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
    lines.append(tr("Ask: {mode}; limits: {limits}", mode=printable(data.get("ask") or "?"),
                    limits=", ".join(f"{printable(k)}={printable(v)}" for k, v in limits.items()) or "-"))
    lines.append(tr("Calls: {calls}; today {bytes} bytes", calls=printable(data.get("calls", 0)),
                    bytes=printable((data.get("volume") or {}).get("bytes", 0))))
    for warning in data.get("warnings") or []:
        lines.append("  ⚠ " + printable(warning))
    alerts = [x for x in data.get("alerts") or [] if isinstance(x, dict)]
    if alerts:
        lines += ["", tr("Alerts:")]
        for alert in alerts[-20:]:
            lines.append(f"  {printable(alert.get('time'))} {printable(alert.get('from'))} → "
                         f"{printable(alert.get('about'))} ({tr(BR.SEVERITIES.get(alert.get('severity'), '?'))}): "
                         f"{printable(alert.get('text'))[:500]}")
    recent = [x for x in data.get("recent") or [] if isinstance(x, dict)]
    if recent:
        lines += ["", tr("Newest calls and events:")]
        for call in recent[-20:]:
            lines.append(f"  {printable(call.get('time'))} {printable(call.get('side', '-'))} "
                         f"{printable(call.get('tool') or call.get('event'))} {printable(call.get('result', ''))[:120]}")
    return lines


class BridgesBox(QGroupBox):
    """The bridges between running sessions of this machine (sandbox-ai bridge list), with New bridge…, Show,
    Revoke… and Resume…. Read in the background after each look of the Sandboxes page; CHANGED is said when what is
    listed changed (the page marks both sessions' rows)."""
    changed = Signal()

    HOSTS_EVERY = 30  # seconds between two reads of the hosts' bridges (each host asked over SSH)

    def __init__(self, sai, sessions_of, selected_of=None, remote_of=None, hosts_of=None, clock=time.monotonic):
        super().__init__(tr("Bridges"))
        self.sai, self.sessions_of, self.selected_of = sai, sessions_of, selected_of or (lambda: None)
        # The sessions of the user's hosts the page has seen (HOST:NAME), and the names of those hosts: New bridge…
        # offers them at once and asks each host afresh in the background.
        self.remote_of, self.hosts_of = remote_of or (lambda: []), hosts_of or (lambda: [])
        self.rows, self.reading, self.read_again, self.error = [], None, False, None
        # The bridges of the user's hosts ({host: rows}, bridges.from_host), read in the background every HOSTS_EVERY
        # seconds at most, each host apart: made there, or a half there of one made elsewhere (listed once)
        self.local_rows, self.host_rows, self.host_reading, self.hosts_read, self.clock = [], {}, set(), None, clock
        layout = QVBoxLayout(self)
        self.empty = plain_label(tr("No bridge between running sessions. New bridge… lets one session reach "
                                    "another (message, ask, files, watch)."), role="secondary")
        layout.addWidget(self.empty)
        self.table = widgets.table([tr("Bridge"), tr("Between"), tr("A may"), tr("B may"), tr("State"), tr("Until"),
                                    tr("Calls")], stretch=1, numeric=(6,), compact=(0, 4, 5))
        self.table.setAccessibleName(tr("Bridges"))
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self.show_bridge())
        layout.addWidget(self.table)
        self.buttons = {}
        for key, text, tip, slot in (
                ("new", tr("New bridge…"), tr("Let one running session reach another: messages, questions, files, "
                                               "observing (sandbox-ai bridge add); checked and warned about first"),
                 self.new_bridge),
                ("show", tr("Show"), tr("The selected bridge: its grant both ways, state, limits, counters, alerts and "
                                        "newest calls (sandbox-ai bridge show)"), self.show_bridge),
                ("revoke", tr("Revoke…"), tr("End the selected bridge now: both sessions lose it at once (sandbox-ai "
                                             "bridge revoke)"), self.revoke),
                ("resume", tr("Resume…"), tr("Lift the suspension of the selected bridge once its cause (a kill switch, "
                                             "a canary alarm) is gone (sandbox-ai bridge resume)"), self.resume)):
            button = QPushButton(text)
            button.setToolTip(safe_tooltip(tip))
            button.setAccessibleName(text.rstrip("…"))
            button.clicked.connect(lambda _=False, s=slot: s())
            self.buttons[key] = button
        self.buttons["revoke"].setProperty("role", "danger")
        layout.addWidget(flow(*self.buttons.values()))
        # A right click on a bridge: the buttons' commands, enabled as they are (a disabled one with its reason).
        widgets.list_menu(self.table, lambda: list(self.buttons.values()))
        self.show_rows()

    # -- reading --

    def refresh(self, hosts_now=False):
        """Read the bridges in a thread, one read at a time (another once it is done when asked meanwhile); the hosts'
        bridges too when HOSTS_EVERY seconds have passed since they were read (HOSTS_NOW: now)."""
        self.refresh_hosts(hosts_now)
        if self.reading is not None:
            self.read_again = True
            return
        self.read_again = False
        self.reading = background(self.sai.bridges, timeout=60, owner=self, done=self.read_done)

    def refresh_hosts(self, now=False):
        hosts = [h for h in self.hosts_of() or () if isinstance(h, str) and BR.HOST.fullmatch(h) and h != LOCAL]
        if self.host_reading or not hosts or not hasattr(self.sai, "host_bridges") or \
                (not now and self.hosts_read is not None and self.clock() - self.hosts_read < self.HOSTS_EVERY):
            return
        self.hosts_read = self.clock()
        self.host_rows = {host: rows for host, rows in self.host_rows.items() if host in hosts}
        for host in hosts:
            self.host_reading.add(host)
            background(self.sai.host_bridges, host, REMOTE_WAIT, owner=self, timeout=REMOTE_WAIT + 15,
                       done=lambda rows, error, h=host: self.host_read(h, rows, error))

    def host_read(self, host, rows, error):
        """HOST's bridges (a host that does not answer keeps what it listed last)."""
        self.host_reading.discard(host)
        if error is None:
            self.host_rows[host] = BR.live(rows)
        self.update_rows()

    def read_done(self, rows, error):
        self.reading = None
        self.local_rows = [] if error is not None else BR.live(rows)
        self.error = None if error is None else printable_text(str(error))[:300]
        self.update_rows()
        if self.read_again:
            self.refresh()

    def update_rows(self):
        rows = BR.merged(self.local_rows, [row for host in sorted(self.host_rows) for row in self.host_rows[host]])
        if rows != self.rows:
            self.rows = rows
            self.show_rows()
            self.changed.emit()

    def show_rows(self):
        rows = self.rows
        cells, colors, keys = [], [], []
        for row in rows:
            both = row.get("b_to_a") not in (None, "", "-")
            cells.append([printable(BR.qualified(row)),
                          f"{printable(row.get('a'))} {'⇄' if both else '→'} {printable(row.get('b'))}",
                          printable(row.get("a_to_b") or "-"), printable(row.get("b_to_a") or "-"),
                          tr(BR.STATE_TITLES.get(row.get("state"), "?"))
                          + (" " + tr("(reconnecting)") if row.get("reconnecting") is True else ""),
                          printable(row.get("until") or "-"),
                          row.get("calls", 0) if isinstance(row.get("calls"), int) else 0])
            colors.append("warn" if row.get("state") == "suspended" or row.get("warnings") or
                          row.get("reconnecting") is True else None)
            keys.append(BR.qualified(row))
        widgets.fill(self.table, cells, colors=colors, keys=keys)
        self.table.setVisible(bool(rows))
        self.table.setMaximumHeight(self.table.horizontalHeader().height() + 4 +
                                    self.table.verticalHeader().defaultSectionSize() * max(1, min(len(rows), MAX_ROWS)))
        self.empty.setText(tr("The bridges could not be read: {error}", error=self.error) if self.error else
                           tr("No bridge between running sessions. New bridge… lets one session reach another "
                              "(message, ask, files, watch)."))
        self.empty.setVisible(not rows)
        self.update_buttons()

    def selected(self):
        keys = widgets.selected_keys(self.table)
        return next((row for row in self.rows if keys and BR.qualified(row) == keys[0]), None)

    def update_buttons(self):
        row = self.selected()
        none = tr("Select a bridge first.")
        sessions = self.sessions_of()
        set_enabled(self.buttons["new"], self.may_bridge(sessions), self.why_not(sessions))
        set_enabled(self.buttons["show"], row is not None, none)
        set_enabled(self.buttons["revoke"], row is not None, none)
        set_enabled(self.buttons["resume"], row is not None and row.get("state") == "suspended",
                    none if row is None else tr("Only a suspended bridge is resumed."))

    def may_bridge(self, sessions):
        """Two sessions of this machine run, or the user has hosts (whose sessions the dialog lists: a bridge may join
        one of them with one here or with another of a host)."""
        return len(sessions) >= 2 or bool(self.hosts_of())

    def why_not(self, sessions):
        if self.hosts_of():
            return tr("A bridge joins two running sessions, here or on your hosts; {count} run here now.",
                      count=len(sessions))
        return tr("A bridge joins two agent sessions of this machine that run; {count} run now.", count=len(sessions))

    # -- actions --

    def new_bridge(self, first=None, second=None):
        """The dialog, then the launcher's dry run in the background, then the question with its warnings, then the
        add in the background."""
        sessions = self.sessions_of()
        if not self.may_bridge(sessions):
            inform(self, tr("New bridge"), self.why_not(sessions))
            return
        selected = self.selected_of()
        known = {entry["name"]: entry for entry in [*sessions, *self.remote_of()]}
        key = ("bridge-new", first or selected, second)  # what was chosen and not made: filled in again (drafts.py)
        dialog = BridgeDialog(self, list(known.values()), first=first or selected, second=second, sai=self.sai,
                              hosts=self.hosts_of() or None, draft=key)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.values()
            args = self.sai.bridge_add_args(**values)
        except ValueError as exc:
            warn(self, tr("New bridge"), printable_text(exc))
            return

        def made(result, error):
            if error is None and result is not None:
                from . import drafts
                drafts.drop(key)  # made: what was chosen is done with
        self.check(values, args, then=made)

    def check(self, values, args, then=None):
        """The dry run of ARGS in the background; its plan is asked about (bridge-add), then the bridge is made.
        THEN(result or None, error or None) once it is made or refused (the Board's Bridge part)."""
        background(self.sai.bridge_plan, args, owner=self, source=self.buttons["new"], timeout=120,
                   done=lambda plan, error: self.planned(values, args, plan, error, then))

    def planned(self, values, args, plan, error, then=None):
        if error is not None:
            warn(self, tr("Not bridged"), printable_text(error))
            if then is not None:
                then(None, error)
            return

        def made(result, error):
            self.refresh(hosts_now=bool(BR.peer_host(values["a"]) and BR.peer_host(values["b"])))
            if then is not None:
                then(result, error)
        create(self, self.sai, values, args, plan, made, source=self.buttons["new"])

    def show_bridge(self):
        row = self.selected()
        if row is None:
            return
        ident = BR.qualified(row)
        background(self.sai.bridge_show, ident, owner=self, source=self.buttons["show"], timeout=150,
                   done=lambda document, error: self.shown(ident, document, error))

    def shown(self, ident, document, error):
        if error is not None:
            warn(self, tr("Bridge {id}", id=printable(ident)), printable_text(error), modal=False)
            return
        show_text(self, tr("Bridge {id}", id=printable(ident)), "\n".join(show_lines(document)), modal=False)

    def revoke(self):
        row = self.selected()
        if row is None:
            return
        if not ask_first(self, "bridge-revoke", tr("Revoke"),
                         tr("Revoke bridge {id} between {a} and {b}? Both lose it at once.",
                            id=printable(BR.qualified(row)), a=printable(row.get("a")), b=printable(row.get("b"))),
                         tr("Revoke")):
            return
        background(self.sai.bridge_revoke, BR.qualified(row), owner=self, source=self.buttons["revoke"], timeout=150,
                   work=True, done=lambda result, error: self.done(tr("Revoke"), error, row))

    def resume(self):
        row = self.selected()
        if row is None:
            return
        if not ask_first(self, "bridge-resume", tr("Resume"),
                         tr("Resume bridge {id} between {a} and {b}? They reach each other again as it allows.",
                            id=printable(BR.qualified(row)), a=printable(row.get("a")), b=printable(row.get("b"))),
                         tr("Resume")):
            return
        background(self.sai.bridge_resume, BR.qualified(row), owner=self, source=self.buttons["resume"], timeout=150,
                   work=True, done=lambda result, error: self.done(tr("Resume"), error, row))

    def done(self, title, error, row=None):
        self.refresh(hosts_now=bool(row and (row.get("host") or row.get("relay"))))
        if error is not None:
            warn(self, title, printable_text(error), modal=False)

