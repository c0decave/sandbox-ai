"""The Approvals page's parts besides the open questions: which sandbox it is about and how that one runs
(ContextHeader), what it refused and why, with what may be done about it (BlockedSection), and everything it may reach
now - what its start opened, each with its source, and what was allowed during the run (AllowedSection).

The page follows the active tab (its run, its session's run, or the newest run of its project); the header's box
chooses another running sandbox, or all of them. Everything is read in threads, bounded (sandbox-ai's
approvals.blocked reads the newest part of each log only); every change goes through sandbox-ai's own commands
(`sandbox-ai approvals allow|deny|ask-again`, `sandbox-ai net allow|revoke`), which keep to the run's security mode,
the administrator's policy, Tor and the kill switch - the supervisor checks again what reaches it."""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import applog
from . import theme
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import (
    ask_first, background, compact_combo, confirm, explanation, fill, flow, list_menu, listing, on_screen, plain_label,
    row_key, selected_rows, set_enabled, table, warn,
)

FOLLOW, ALL = "follow", "all"
LOCAL = "local"  # this machine, as sandbox-ai hosts names it
BLOCKED_INTERVAL = 5000  # ms between two reads of the Blocked list while it is shown
REMOTE_BLOCKED_SECONDS = 30  # a start of another host: its list read over SSH at most this often (unless asked)
ACTION_WAIT = 150        # seconds the commands of one action may take together


def kind_label(kind):
    return {"network": tr("Not allowed by the network settings"), "rule": tr("Denied by a ruleset"),
            "unanswered": tr("Not answered in time"), "denied": tr("Denied by you"),
            "mode": tr("The security mode"), "policy": tr("The administrator's policy"),
            "cut": tr("The kill switch"), "blocked": tr("Blocked always"), "taint": tr("Untrusted content"),
            }.get(kind, tr("Other"))


# The Show box of the Blocked list: (kinds, label); None: every kind.
def blocked_filters():
    return [(None, tr("All reasons")), (("network",), tr("Not allowed by the network settings")),
            (("rule",), tr("Denied by a ruleset")), (("unanswered",), tr("Not answered in time")),
            (("denied",), tr("Denied by you")), (("mode", "policy"), tr("Security mode or policy")),
            (("cut",), tr("The kill switch")), (("blocked", "taint", "other"), tr("Other reasons"))]


def why_text(entry):
    """Why ENTRY (of sandbox-ai's approvals.blocked) was refused and what to do, in the app's language."""
    kind = entry.get("kind")
    rule, ruleset = printable(entry.get("rule") or "?"), printable(entry.get("ruleset") or "?")
    if kind == "network":
        return tr("The sandbox's network settings do not allow it. Allow it for this run, or always for this project.")
    if kind == "rule":
        return tr("Rule {rule} of the ruleset {ruleset} denies it. The network settings do not change what a ruleset "
                  "decides; “Allow always” adds an exception to a ruleset of yours.", rule=rule, ruleset=ruleset)
    if kind == "unanswered":
        if entry.get("ruleset"):
            return tr("Rule {rule} of the ruleset {ruleset} asks before it, and nobody answered in time. That try "
                      "failed; the question stays open, and its answer counts for the next try. Allowing its host in "
                      "the network settings does not change that: allow it here.", rule=rule, ruleset=ruleset)
        return tr("It was asked about, and nobody answered in time. That try failed; the question stays open, and its "
                  "answer counts for the next try.")
    if kind == "denied" and entry.get("detail"):  # an allow that closed it: the name does not exist
        return tr("Denied for this run: {detail}. “Ask again” asks again, e.g. once the name exists.",
                  detail=printable(entry["detail"]))
    if kind == "denied":
        return tr("You denied it for this run. “Ask again” takes that back; “Always allow” allows it for the rest of "
                  "the run.")
    if kind == "other" and str(entry.get("what") or "").startswith("lookup of ") and \
            "reserved name" in str(entry.get("reason") or ""):
        return tr("A reserved name (RFC 2606/6761): it never exists, so its lookup is refused without a question. "
                  "There is nothing to allow.")
    if kind == "mode":
        return tr("The run's security mode refuses it. Start again with a laxer mode if you mean it.")
    if kind == "policy":
        return tr("The administrator's policy refuses it. Only your administrator can change that.")
    if kind == "cut":
        return tr("The kill switch cuts it. What you allow meanwhile stays cut until the cut is lifted (Sessions › "
                  "Kill switch).")
    if kind == "blocked":
        return tr("You blocked it always for this project (Inspection › Remembered › Forget lifts it).")
    if kind == "taint":
        return tr("Untrusted content entered the run, and the rules for tainted runs hold. `sandbox-ai net untaint` "
                  "takes that back, if you trust what entered.")
    return printable(entry.get("reason") or tr("Refused."))


def run_for_tab(entry, runs):
    """The running sandbox the tab ENTRY (ui.project_entry, instance_entry) stands for, among RUNS ({run: record}):
    a run's tab its run, a session's tab its session's run, a project's tab the newest run of the project; else
    None."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind")
    if kind == "run" and entry.get("id") in runs:
        return entry["id"]
    if kind == "session":
        for name, record in runs.items():
            session_dir = str(record.get("session_dir") or "")
            if (entry.get("name") and record.get("session") == entry.get("name")) or \
                    (entry.get("id") and session_dir and Path(session_dir).name == entry.get("id")):
                return name
    path = entry.get("path")
    if path:
        here = os.path.realpath(str(path))
        mine = [(str(record.get("started") or ""), name) for name, record in runs.items()
                if record.get("workspace") and os.path.realpath(str(record["workspace"])) == here]
        if mine:
            return max(mine)[1]
    return None


def run_label(name, record):
    workspace = Path(str(record.get("workspace") or "")).name
    label = printable(record.get("name") or record.get("session") or name)
    return f"{label} ({printable(workspace)})" if workspace else label


def state_text(record, seconds, cut=None):
    """How the sandbox RECORD runs, in one line: its security mode, VPN or Tor, the kill switch, how questions wait."""
    parts = []
    mode = record.get("mode") if isinstance(record.get("mode"), dict) else {}
    if mode.get("name"):
        parts.append(tr("security mode {mode}", mode=printable(mode["name"])))
    if record.get("vpn_type") == "tor":
        parts.append(tr("through Tor"))
    elif record.get("vpn_profile"):
        parts.append(tr("through the VPN {name}", name=printable(record["vpn_profile"])))
    if cut:
        parts.append(tr("kill switch: cut ({mode})", mode=printable(cut)))
    ask = record.get("ask")
    if ask == "off":
        parts.append(tr("asks nothing (--ask off): what is not allowed is refused"))
    elif ask == "allow":
        parts.append(tr("allows every question by itself (--ask allow)"))
    else:
        parts.append(tr("a question waits {seconds} s for your answer", seconds=seconds))
    return " · ".join(parts)


class ContextHeader(QWidget):
    """Which sandbox the page is about (the active tab's, one chosen, or all) and how it runs, in one line."""
    changed = Signal()

    def __init__(self, sai, seconds):
        super().__init__()
        self.sai, self.seconds = sai, seconds
        self.runs, self.active = {}, None
        # The running starts of the hosts of sandbox-ai hosts (as the Sessions page last read them): {"HOST:NAME":
        # {"host", "name", "agent", "workspace"}} - chosen one at a time, read and changed there over SSH.
        self.remote = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.purpose = explanation(tr("What your sandboxes ask, what they were refused and why, and what they may "
                                      "reach. What you change here holds for the running sandbox at once."))
        layout.addWidget(self.purpose)
        row = QHBoxLayout()
        label = QLabel(tr("Sandbox"))
        self.choice = QComboBox()
        self.choice.setAccessibleName(tr("Sandbox shown"))
        self.choice.setToolTip(tr("Blocked and Allowed now show this sandbox: the active tab's (it follows the tabs), "
                                  "another one that runs, or all of them"))
        compact_combo(self.choice, 28)
        label.setBuddy(self.choice)
        self.choice.activated.connect(lambda _=0: self.changed.emit())
        self.state = plain_label(role="secondary", selectable=True)
        self.state.setAccessibleName(tr("How the sandbox runs"))
        row.addWidget(label)
        row.addWidget(self.choice)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(self.state)
        self.fill()

    def fill(self):
        chosen = self.choice.currentData() if self.choice.count() else FOLLOW
        self.choice.blockSignals(True)
        self.choice.clear()
        active = self.runs.get(self.active) if self.active else None
        self.choice.addItem(tr("The active tab's: {name}", name=run_label(self.active, active)) if active else
                            tr("The active tab's (none runs)"), FOLLOW)
        for name, record in sorted(self.runs.items()):
            self.choice.addItem(run_label(name, record), name)
        for key, record in sorted(self.remote.items()):
            self.choice.addItem(tr("{sandbox} on {host}", sandbox=printable(record.get("name")),
                                   host=printable(record.get("host"))), key)
        self.choice.addItem(tr("All running sandboxes"), ALL)
        index = self.choice.findData(chosen)
        self.choice.setCurrentIndex(index if index >= 0 else 0)
        self.choice.blockSignals(False)
        self.show_state()

    def set_runs(self, runs, active):
        """The running sandboxes ({run: record}) and the active tab's run (or None)."""
        if runs == self.runs and active == self.active:
            return False
        self.runs, self.active = dict(runs), active
        self.fill()
        return True

    def set_remote(self, starts):
        """The running starts of the other hosts ({host: [(name, record)]}): each one can be chosen (read there)."""
        remote = {f"{host}:{name}": {**dict(record or {}), "host": host, "name": name}
                  for host, found in (starts or {}).items() if host != LOCAL for name, record in found}
        if remote == self.remote:
            return False
        self.remote = remote
        self.fill()
        return True

    def choose(self, host, name):
        """Show NAME of HOST (Sessions › What it may reach…): this machine's run, or a start of another host (listed
        now when the last read of that host's starts did not have it yet)."""
        key = name if host == LOCAL else f"{host}:{name}"
        if host != LOCAL and key not in self.remote:
            self.remote[key] = {"host": host, "name": name}
            self.fill()
        index = self.choice.findData(key)
        if index < 0:
            return False
        self.choice.setCurrentIndex(index)
        self.show_state()
        return True

    def run(self):
        """The run shown: a run's name, a start of another host ("HOST:NAME"), ALL (this machine's), or None (the
        active tab has none running)."""
        chosen = self.choice.currentData()
        if chosen == FOLLOW:  # the active tab's; when it has none running, the only one that runs, or all of them
            if self.active in self.runs:
                return self.active
            return next(iter(self.runs)) if len(self.runs) == 1 else ALL if self.runs else None
        return chosen if chosen == ALL or chosen in self.runs or chosen in self.remote else None

    def remote_target(self):
        """(host, name) of the start of another host shown, or None (this machine's)."""
        record = self.remote.get(self.run())
        return (record["host"], record["name"]) if record else None

    def records(self):
        """[(run, record)] shown."""
        run = self.run()
        if run == ALL:
            return sorted(self.runs.items())
        if run in self.remote:
            return [(run, self.remote[run])]
        return [(run, self.runs[run])] if run else []

    def show_state(self):
        run = self.run()
        if run in self.remote:
            record = self.remote[run]
            self.state.setText(tr("{sandbox} on {host}", sandbox=printable(record.get("name")), host=printable(
                record.get("host"))) + " · " + tr("read there over SSH; what you change here is done there"))
            return
        if run == ALL:
            self.state.setText(tr("All running sandboxes ({count})", count=len(self.runs)) if
                               self.choice.currentData() == ALL else
                               tr("The active tab has no running sandbox: all running ones are shown ({count}).",
                                  count=len(self.runs)))
        elif run is None:
            self.state.setText(tr("No sandbox runs."))
        else:
            record = self.runs[run]
            prefix = "" if run == self.active or self.choice.currentData() != FOLLOW else \
                tr("The active tab has no running sandbox; the only one that runs:") + " " + run_label(run, record) + " · "
            cut = None
            try:
                cut = self.sai.module("net").cut_state(record["directory"])
            except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai, an unreadable state: not said
                applog.swallowed("approvals_sections", "ContextHeader.show_state: an older sandbox-ai, an unrea…", _exc)
                cut = None
            self.state.setText(prefix + state_text(record, self.seconds, cut))


def local_time(stamp):
    """An ISO time of the logs (UTC, "...Z") as this computer's local time, "MM-DD HH:MM:SS"; as it came otherwise."""
    import calendar
    import time
    try:
        seconds = calendar.timegm(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return printable(str(stamp or ""))[:19]
    return time.strftime("%m-%d %H:%M:%S", time.localtime(seconds))


def remote_blocked(sai, host, result, error):
    """(entries, error) of `sandbox-ai approvals blocked NAME --json` on HOST (RESULT, ERROR as background gives
    them): each entry marked with its host; "Allow always" of a request or tool call is left out (its exception rule
    would go into a ruleset of this machine). An older sandbox-ai there: the update to do."""
    if error is not None:
        return None, error
    status, out, err = result
    if status != 0:
        return None, ValueError(sai.too_old_text(host, result, "approvals blocked") or (err or out).strip()[-500:]
                                or f"exit status {status}")
    try:
        data = json.loads(out)
    except (ValueError, RecursionError):
        return None, ValueError((err or out).strip()[-300:] or "no answer")
    entries = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        entry = {**entry, "host": host}
        dns = entry.get("question") and entry.get("question_kind") == "dns" and entry.get("kind") != "network"
        if not dns and not target_option(entry.get("target")):
            entry["actions"] = [a for a in entry.get("actions") or [] if a != "allow-always"]
        entries.append(entry)
    return entries, None


def target_option(target):
    """The `sandbox-ai net allow|revoke` option for a destination ({"host"}, {"tcp"} or {"address"})."""
    if not isinstance(target, dict):
        return None
    if target.get("host"):
        return ["--allow-host", target["host"]]
    if target.get("tcp"):
        return ["--allow-tcp", target["tcp"]]
    if target.get("address"):
        return ["--allow", target["address"]]
    return None


class BlockedSection(QWidget):
    """What the sandbox shown refused (sandbox-ai approvals.blocked), newest first, filtered by reason; Allow for this
    run, Allow always for this project, Ask again and Deny for this run on the selected entries."""
    counted = Signal(int)

    def __init__(self, sai, header):
        super().__init__()
        self.sai, self.header = sai, header
        self.entries, self.reading, self.acting, self.generation = [], None, None, 0
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        show = QLabel(tr("Show"))
        self.filter = QComboBox()
        self.filter.setAccessibleName(tr("Show the refusals of"))
        compact_combo(self.filter, 24)
        for kinds, label in blocked_filters():
            self.filter.addItem(label, kinds)
        show.setBuddy(self.filter)
        self.filter.currentIndexChanged.connect(lambda _=0: self.show_entries())
        self.allow_run = QPushButton(tr("Allow for this run"))
        self.allow_run.setToolTip(tr("The sandbox may do it until it ends: a destination is opened, a request or tool "
                                     "call no longer asked"))
        # (the label of this scope everywhere: the questions' buttons say it so; "Always allow" there is for the run)
        self.allow_always = QPushButton(tr("Always, also for later sessions…"))
        self.allow_always.setToolTip(tr("Kept for this project: a destination in its .sandbox-ai.json, a request as an "
                                        "exception in a ruleset of yours (shown before it is written)"))
        self.ask_again = QPushButton(tr("Ask again"))
        self.ask_again.setToolTip(tr("Forget what this run decided: its next try is asked again"))
        self.deny_run = QPushButton(tr("Deny for this run"))
        self.deny_run.setToolTip(tr("Refused until the sandbox ends, without a question"))
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setAccessibleName(tr("Refresh the blocked list"))
        self.refresh_button.clicked.connect(lambda: self.refresh(force=True))
        self.buttons = {"allow-run": self.allow_run, "allow-always": self.allow_always, "ask-again": self.ask_again,
                        "deny-run": self.deny_run}
        for action, button in self.buttons.items():
            button.setAccessibleName(button.text().rstrip("…"))
            button.clicked.connect(lambda _=False, a=action: self.act(a))
        top.addWidget(show)
        top.addWidget(self.filter)
        top.addStretch()
        layout.addLayout(top)
        # The actions above the list, in a row that wraps when the window is narrow.
        layout.addWidget(flow(*self.buttons.values(), self.refresh_button))
        self.note = explanation(tr("Opening a host in the network or VPN settings does not change what a ruleset "
                                   "decides: a request a ruleset asks about is asked even when its host is allowed."))
        layout.addWidget(self.note)
        self.table = table([tr("Time"), tr("Sandbox"), tr("Reason"), tr("What"), tr("Tried by"), tr("Times")],
                           multi=True, stretch=3, compact=(0, 1, 2, 5), numeric=(5,))
        self.table.setAccessibleName(tr("Blocked"))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        list_menu(self.table, lambda: [*self.buttons.values(), None, self.refresh_button])
        layout.addWidget(self.table, 1)
        self.empty = plain_label(role="secondary")
        layout.addWidget(self.empty)
        self.detail = plain_label(selectable=True)
        self.detail.setAccessibleName(tr("Why it was refused"))
        layout.addWidget(self.detail)
        self.message = plain_label(selectable=True, log="approvals")
        layout.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: on_screen(self) and self.refresh())  # (not in a minimised window)
        self.timer.start(BLOCKED_INTERVAL)
        self.update_buttons()

    # -- reading --

    def refresh(self, force=False):
        """Read the list anew in a thread (one read at a time; FORCE: also when one runs, its result is dropped). A
        start of another host: `sandbox-ai approvals blocked NAME --json` there, at most every
        REMOTE_BLOCKED_SECONDS unless FORCE."""
        if self.reading is not None and not force:
            return
        target = self.header.remote_target()
        if target is not None and not force and (target, self.generation) == getattr(self, "remote_read", (None, -1)) \
                and time.monotonic() - getattr(self, "remote_at", 0) < REMOTE_BLOCKED_SECONDS:
            return
        records = self.header.records()
        runs = dict(self.header.runs)
        self.generation += 1
        token = self.generation
        if not records:
            self.reading = None
            self.apply(token, [], None)
            return
        if target is not None:
            self.remote_read, self.remote_at = (target, token), time.monotonic()
            self.reading = background(self.sai.host_run, target[0], "approvals", "blocked", target[1], "--json",
                                      timeout=90, done=lambda result, error: self.apply(
                                          token, *remote_blocked(self.sai, target[0], result, error)))
            return
        self.reading = background(self.read, records, runs, timeout=60,
                                  done=lambda result, error: self.apply(token, result, error))

    def read(self, records, runs):
        approvals = self.sai.module("approvals")
        if not hasattr(approvals, "blocked"):
            raise ValueError(tr("This sandbox-ai has no list of what was refused (an older version)."))
        found = []
        for _, record in records:
            found += approvals.blocked(record, runs)
        return sorted(found, key=lambda e: str(e.get("time")), reverse=True)

    def apply(self, token, result, error):
        if token != self.generation:
            return
        self.reading = None
        if error is not None:
            self.message.setText(tr("Cannot read what was refused: {error}", error=printable_text(error)))
            return
        self.entries = list(result or [])
        self.show_entries()

    def visible_entries(self):
        kinds = self.filter.currentData()
        return [entry for entry in self.entries if kinds is None or entry.get("kind") in kinds]

    def show_entries(self):
        entries = self.visible_entries()
        keys = [(e.get("run"), e.get("question") or (e.get("kind"), e.get("what"))) for e in entries]
        rows = [[local_time(e.get("time")), printable(e.get("run") or ""), kind_label(e.get("kind")),
                 printable(e.get("what") or ""), printable(e.get("process") or ""), int(e.get("count") or 1)]
                for e in entries]
        colors = ["muted" if e.get("kind") in ("mode", "policy", "cut") else "warn" for e in entries]
        # The read every BLOCKED_INTERVAL mostly finds what is shown: the table is filled again only when a row
        # differs (300 rows took ~20 ms of the window's time every 5 s, and the list under the mouse was rebuilt).
        # The entries themselves (their actions, why) are always the newest: by_key and the buttons below.
        filled = (rows, colors, keys, theme.color("warn"), theme.color("muted"))  # (another theme: its colours)
        if filled != getattr(self, "filled", None):
            fill(self.table, rows, colors=colors, keys=keys)
            self.filled = filled
        self.by_key = dict(zip(keys, entries))
        run = self.header.run()
        self.empty.setVisible(not entries)
        self.empty.setText(tr("Nothing was refused.") if self.entries or run is not None else
                           tr("No sandbox runs: nothing to show."))
        self.counted.emit(len(self.entries))
        self.selection_changed()

    # -- the selection and the actions --

    def selected(self):
        return [self.by_key[key] for key in (row_key(self.table, row) for row in selected_rows(self.table))
                if key in getattr(self, "by_key", {})]

    def selection_changed(self):
        chosen = self.selected()
        if len(chosen) == 1:
            entry = chosen[0]
            lines = [why_text(entry)]
            if entry.get("reason"):
                lines.append(tr("sandbox-ai said: {reason}", reason=printable(entry["reason"])))
            # sandbox-ai offers nothing for some (a packet of another protocol: --allow would open every port of
            # it; a name that carries a canary token: never looked up): said, not left to buttons that are off.
            if not entry.get("actions") and entry.get("kind") in ("network", "rule", "unanswered", "denied"):
                lines.append(tr("Nothing can be allowed here: sandbox-ai would have to open more than was refused "
                                "(or the name carries a canary token, which is never looked up)."))
            self.detail.setText("\n".join(lines))
        else:
            self.detail.setText(tr("{count} selected", count=len(chosen)) if chosen else "")
        self.update_buttons()

    def update_buttons(self):
        chosen = self.selected()
        busy = self.acting is not None
        for action, button in self.buttons.items():
            possible = bool(chosen) and all(action in (entry.get("actions") or []) for entry in chosen)
            why = None if possible else (tr("Choose what was refused first.") if not chosen else
                                         tr("Not possible for everything selected (the reason, the security mode or "
                                            "the administrator's policy)."))
            set_enabled(button, possible and not busy, why)

    def act(self, action, chosen=None):
        """ACTION on the entries CHOSEN (default: those selected here; the Approvals table hands its Refused rows)."""
        chosen = [entry for entry in (self.selected() if chosen is None else chosen)
                  if action in (entry.get("actions") or [])]
        if not chosen or self.acting is not None:
            return
        host = next((entry["host"] for entry in chosen if entry.get("host")), LOCAL)
        where = (lambda text: text) if host == LOCAL else (lambda text: tr("On {host}: {text}", host=printable(host),
                                                                             text=text))
        # Every one it acts on is named, or counted past the first ones (it named 12 and allowed them all).
        what = listing([printable(entry.get("what") or "") for entry in chosen])
        if action == "allow-run":
            if not ask_first(self, "net-allow", tr("Allow for this run?"),
                             where(tr("The sandbox may do this until it ends:") + "\n" + what),
                             tr("Allow for this run")):
                return
        elif action == "allow-always":
            text = self.always_text(chosen)
            if text is None or not ask_first(self, "net-allow", tr("Allow always for this project?"), where(text),
                                             tr("Always, also for later sessions")):
                return
        # sandbox-ai there (a start of another host: over SSH), as `sandbox-ai ARGS` here.
        call = self.sai.call if host == LOCAL else functools.partial(self.sai.host_run, host)

        # In the thread, after the question above (ask_first): what each entry needs.
        def run_actions(action, chosen, records):
            """Each entry's commands (in the thread): [(entry, (status, out, err))]."""
            done = []
            for entry in chosen:
                run, question = str(entry.get("run") or ""), entry.get("question")
                option = target_option(entry.get("target"))
                try:
                    if action == "ask-again":
                        result = call("approvals", "ask-again", question, timeout=60)
                    elif action == "deny-run":
                        result = call("approvals", "deny", question, "--scope", "run", timeout=60)
                    elif action in ("allow-run", "allow-always") and question and \
                            entry.get("question_kind") == "dns" and entry.get("kind") != "network":
                        # A lookup the agent asked about: as an answer to it, only the name's public addresses are
                        # opened (the agent chose the name; it may point at this host or the LAN), "always" kept as
                        # such an answer is (approvals allow ID: decide_again), never as the project's --allow-host.
                        scope = "always" if action == "allow-always" else "run"
                        result = call("approvals", "allow", question, "--scope", scope, timeout=90)
                    elif action == "allow-run" and question and entry.get("question_kind") != "dns" and not (
                            entry.get("kind") == "network" and option):
                        result = call("approvals", "allow", question, "--scope", "run", timeout=60)
                    elif option:  # a destination: as sandbox-ai net allow (resolved through the run's VPN or Tor)
                        keep = ["--keep"] if action == "allow-always" else []
                        # A name the agent looked up: only its public addresses (--keep: remembered as an approval).
                        public = ["--public-only"] if option[0] == "--allow-host" else []
                        result = call("net", "allow", run, *option, *public, *keep, timeout=90)
                        if question and result[0] == 0:
                            call("approvals", "ask-again", question, timeout=60)  # no remembered deny left
                    elif host != LOCAL:  # an exception rule is written into a ruleset of this machine: not for there
                        result = (1, "", tr("An exception rule is written into a ruleset of this machine: for a "
                                            "start of another host, allow it for the run, or add the rule there."))
                    else:  # allow-always for a request or tool call: an exception rule, then allowed for this run
                        plan = self.sai.module("approvals").rule_exception(records.get(run) or {}, entry, write=True)
                        result = (0, json.dumps(plan), "")
                        if question:
                            result = call("approvals", "allow", question, "--scope", "run", timeout=60)
                except (ValueError, OSError) as exc:
                    result = (1, "", str(exc))
                done.append((entry, result))
            return done

        records = dict(self.header.runs)
        self.acting = background(run_actions, action, chosen, records, timeout=ACTION_WAIT, work=True,
                                 done=lambda result, error: self.acted(action, result, error))
        self.message.setText(tr("Working…"))
        self.update_buttons()

    def always_text(self, chosen):
        """What "Allow always" changes for CHOSEN, said before it is done (None: refused, said)."""
        lines = []
        approvals = self.sai.module("approvals")
        for entry in chosen:
            option = target_option(entry.get("target"))
            if (entry.get("question") and entry.get("question_kind") == "dns" and entry.get("kind") != "network") or \
                    (option and option[0] == "--allow-host"):
                lines.append(tr("{what}: allowed always for this project, as an answer to the lookup is: only the "
                                "name's public addresses are opened, now and at every later start.",
                                what=printable(entry.get("what"))))
                continue
            if option:
                lines.append(tr("{what}: {option} goes into the project's .sandbox-ai.json (trusted at once); every "
                                "later start of the project may reach it.", what=printable(entry.get("what")),
                                option=" ".join(option)))
                continue
            record = self.header.runs.get(entry.get("run"))
            try:
                plan = approvals.rule_exception(record or {}, entry)
            except (ValueError, OSError, AttributeError) as exc:
                warn(self, tr("Allow always"), tr("{what}: {error}", what=printable(entry.get("what")),
                                                  error=printable_text(exc)), modal=False)
                return None
            rule = json.dumps(plan["rule"], ensure_ascii=False)
            if plan["where"] == "own":
                lines.append(tr("{what}: the rule {rule} goes first into your own ruleset {path}; the running sandbox "
                                "reads it again at once.", what=printable(entry.get("what")), rule=printable(rule),
                                path=printable(plan["path"])))
            elif plan["where"] == "project":
                lines.append(tr("{what}: the rule {rule} goes first into the project's own ruleset (its "
                                ".sandbox-ai.json, trusted at once); it holds from the next start, and this run "
                                "allows it now.", what=printable(entry.get("what")), rule=printable(rule)))
            else:
                lines.append(tr("{what}: {original} is a preset's or someone else's ruleset and stays as it is. A "
                                "copy of it with the rule {rule} first is written to {path}; a start uses it only in "
                                "place of the original (Board › Approvals and rules). This run allows it now.",
                                what=printable(entry.get("what")), original=printable(plan.get("original")),
                                rule=printable(rule), path=printable(plan["path"])))
        return "\n\n".join(lines)

    def acted(self, action, result, error):
        self.acting = None
        if error is not None:
            self.message.setText(tr("Not done: {error}", error=printable_text(error)))
        else:
            failed = [(entry, err or out) for entry, (status, out, err) in result if status != 0]
            if failed:
                self.message.setText("\n".join(tr("{what}: {error}", what=printable(entry.get("what")),
                                                  error=printable_text(str(why).strip().splitlines()[-1] if str(why).strip()
                                                                  else "?")) for entry, why in failed[:6]))
            else:
                cut = [entry for entry, _ in result if self.cut_of(entry.get("run"))]
                self.message.setText(tr("Done.") + ((" " + tr("The kill switch holds: what you allowed stays cut until "
                                                              "the cut is lifted.")) if cut and action.startswith(
                                                                  "allow") else ""))
        self.update_buttons()
        self.refresh(force=True)

    def cut_of(self, run):
        if self.header.remote_target() is not None:
            return False  # (another host's kill switch: not known here)
        record = self.header.runs.get(run)
        try:
            return bool(record) and bool(self.sai.module("net").cut_state(record["directory"]))
        except Exception as _exc:  # noqa: BLE001 - not known: not said
            applog.swallowed("approvals_sections", "BlockedSection.cut_of: not known: not said", _exc)
            return False


# What a start opened, by the source sandbox-ai names (net show --json "started": agent_sandbox.reach), and the kind.
REACH_SOURCES = {"provider": N_("the agent's provider"), "frontier": N_("--frontier"), "profile": N_("the profile"),
                 "project": N_("the project file"), "option": N_("the start's own options"),
                 "approved": N_("approved always"), "toolchain": N_("a toolchain's registries"),
                 "git-login": N_("a Git login"), "api-key": N_("an API key"), "publish": N_("a publish login"),
                 "llm": N_("--llm"), "feature": N_("an agent feature"), "policy": N_("the administrator's policy")}
REACH_KINDS = {"preset": N_("HTTPS (port 443)"), "https": N_("HTTPS (port 443)"), "wildcard": N_("host wildcard"),
               "tcp": N_("TCP port"), "network": N_("every port and protocol"), "host-port": N_("a port of this computer"),
               "dns": N_("DNS (lookups)"), "everything": N_("every destination (--egress-all-allow)"),
               "nothing": N_("no network (--offline)")}
REACH_HOLDS = {"every start of the project": N_("every start of the project")}


def started_rows(shown):
    """The rows of what its start opened (SHOWN["started"], of runs whose sandbox-ai records it): destination, names,
    kind, why (its source) and how long it holds; never revoked from here (they are its start's: its options or its
    project file)."""
    rows = []
    for entry in shown.get("started") if isinstance(shown.get("started"), list) else []:
        if not isinstance(entry, dict):
            continue
        what = str(entry.get("what") or "")
        hosts = [str(h) for h in entry.get("hosts") or [] if isinstance(h, str)]
        source = entry.get("source")
        why = tr(REACH_SOURCES[source]) if source in REACH_SOURCES else str(source or "")
        if entry.get("detail"):
            why += f" ({entry['detail']})"
        holds = entry.get("scope")
        rows.append({"destination": ", ".join(hosts) if hosts else what,
                     "names": "" if source == "provider" or not hosts else what,
                     "kind": tr(REACH_KINDS[entry.get("kind")]) if entry.get("kind") in REACH_KINDS else
                     str(entry.get("kind") or ""), "why": why,
                     "holds": tr(REACH_HOLDS[holds]) if holds in REACH_HOLDS else str(holds) if holds else
                     tr("this start"), "revoke": None, "start": True})
    return rows


class AllowedSection(QWidget):
    """Everything the sandbox shown may reach now, and why: what its start opened, each with its source and how long it
    holds (net show's "started"), and what was allowed during the run (sandbox-ai net allow, this page, answers for the
    run or always), from `sandbox-ai net show --json`; Revoke takes back what was allowed during the run."""
    counted = Signal(int)

    def __init__(self, sai, header):
        super().__init__()
        self.sai, self.header = sai, header
        self.rows, self.reading, self.generation = [], None, 0
        self.rows_run = None  # the run the rows are of (the header may show another one while it is read)
        layout = QVBoxLayout(self)
        self.revoke = QPushButton(tr("Revoke…"))
        self.revoke.setAccessibleName(tr("Revoke"))
        self.revoke.setToolTip(tr("The sandbox may no longer reach it (a connection open already stays until it ends)"))
        self.revoke.clicked.connect(self.revoke_selected)
        # What Sessions › What it may reach… offered in a window of its own until 2026-11-24 (one view now, user's
        # proposal E): Untaint…, Keep for later starts… (everything: also what approvals opened once or for the run)
        # and the plain text of `sandbox-ai net show` to copy.
        self.untaint = QPushButton(tr("Untaint…"))
        self.untaint.setAccessibleName(tr("Untaint"))
        self.untaint.clicked.connect(lambda _=False: self.untaint_run())
        self.keep = QPushButton(tr("Keep for later starts…"))
        self.keep.setToolTip(tr("sandbox-ai net show --keep: the names and addresses you allowed (or approved "
                                "always), wildcards, networks, DNS, host ports, devices and workspaces added go "
                                "into the project's trusted .sandbox-ai.json; no Git logins"))
        self.keep.clicked.connect(lambda _=False: self.keep_run(False))
        self.keep_all = QPushButton(tr("Keep everything for later starts…"))
        self.keep_all.setToolTip(tr("sandbox-ai net show --keep-all: also what approvals opened once or for the run"))
        self.keep_all.clicked.connect(lambda _=False: self.keep_run(True))
        self.copy = QPushButton(tr("Copy as text"))
        self.copy.setAccessibleName(tr("Copy what it may reach as text"))
        self.copy.setToolTip(tr("The whole list as sandbox-ai net show prints it, to the clipboard"))
        self.copy.clicked.connect(lambda _=False: self.copy_text())
        refresh = QPushButton(tr("Refresh"))
        refresh.setAccessibleName(tr("Refresh what it may reach"))
        refresh.clicked.connect(lambda: self.refresh())
        self.summary = plain_label(role="secondary", selectable=True)
        layout.addWidget(flow(self.revoke, self.untaint, self.keep, self.keep_all, self.copy, refresh))
        layout.addWidget(self.summary)
        self.table = table([tr("Destination"), tr("Names"), tr("Kind"), tr("Why"), tr("Holds for")], multi=True,
                           stretch=1, compact=(0, 2))
        self.table.setAccessibleName(tr("Allowed now"))
        self.table.itemSelectionChanged.connect(self.update_buttons)
        list_menu(self.table, lambda: [self.revoke, None, self.untaint, self.keep, self.keep_all, None, self.copy,
                                       refresh])
        layout.addWidget(self.table, 1)
        layout.addWidget(explanation(tr("Everything it may reach now. Why says where each comes from: its start "
                                        "(provider, options, project file, approvals kept always) or this run "
                                        "(Allow…, an answer). Revoke takes back what this run added; Keep for later "
                                        "starts writes it into the project file.")))
        self.message = plain_label(selectable=True, log="approvals")
        layout.addWidget(self.message)
        self.taint = None  # the run's taint as net show says it ({"tainted", "since", "source"}), None: not said
        self.update_buttons()

    def refresh(self):
        run = self.header.run()
        self.generation += 1
        token = self.generation
        if run in (None, ALL):
            self.apply(token, None, None)
            return
        target = self.header.remote_target()
        if target is not None:  # a start of another host: what it may reach, read there
            self.reading = background(self.sai.host_run, target[0], "net", "show", target[1], "--json", timeout=90,
                                      done=lambda result, error: self.apply(token, result, error, run))
            return
        self.reading = background(self.sai.call, "net", "show", run, "--json", timeout=60,
                                  done=lambda result, error: self.apply(token, result, error, run))

    def apply(self, token, result, error, run=None):
        if token != self.generation:
            return
        self.reading = None
        self.rows_run = run if result is not None and error is None and result[0] == 0 else None
        run_shown = self.header.run()
        self.taint = None
        if result is None and error is None:
            self.rows = []
            self.summary.setText(tr("Choose one running sandbox above to see what it may reach.") if run_shown == ALL
                                 else tr("No sandbox runs: nothing to show."))
        elif error is not None or result[0] != 0:
            self.rows = []
            self.summary.setText(tr("Cannot read it: {error}", error=printable_text(error or result[2] or result[1])))
        else:
            try:
                shown = json.loads(result[1])
            except (ValueError, RecursionError):
                shown = {}
            self.rows = allowed_rows(shown)
            added = len(self.rows)
            self.rows += started_rows(shown)
            self.taint = shown.get("taint") if isinstance(shown.get("taint"), dict) else None
            self.summary.setText(tr("{count} destinations allowed during this run", count=added) + (
                ", " + tr("{count} from its start", count=len(self.rows) - added) if len(self.rows) > added else "") + (
                "  " + tr("(the kill switch cuts them now: {mode})", mode=printable(shown["cut"]))
                if shown.get("cut") else "") + taint_line(self.taint))
        # A row's key is its kind and destination: an HTTPS address without names and a network allowed on every
        # port read the same (as may a DNS server), and one key for both selected - and revoked - the two at once.
        fill(self.table, [[printable(r["destination"]), printable(r["names"]), r["kind"],
                           printable(r.get("why") or tr("allowed during this run")),
                           printable(r.get("holds") or tr("until it ends"))] for r in self.rows],
             keys=[row_identity(r) for r in self.rows])
        self.counted.emit(len(self.rows))
        self.update_buttons()

    def selected(self):
        keys = [row_key(self.table, row) for row in selected_rows(self.table)]
        return [r for r in self.rows if row_identity(r) in keys and r["revoke"]]

    def update_buttons(self):
        set_enabled(self.revoke, bool(self.selected()), tr("Choose what to revoke first."))
        one = None if self.rows_run not in (None, ALL) else tr("Choose one running sandbox above first.")
        for button in (self.keep, self.keep_all, self.copy):
            set_enabled(button, one is None, one)
        tainted = isinstance(self.taint, dict) and bool(self.taint.get("tainted"))
        why = one or (None if tainted else tr("It is not tainted: no untrusted content entered it (or its taint was "
                                               "taken back)."))
        set_enabled(self.untaint, why is None, why)
        if why is None:
            self.untaint.setToolTip(tr("Take back the run's taint (sandbox-ai net untaint): only once you checked "
                                       "the untrusted content that entered it"))

    def target(self):
        """(host, name) of the run the rows were read of, or None."""
        run = self.rows_run
        if run in (None, ALL):
            return None
        record = self.header.remote.get(run)
        return (record["host"], record["name"]) if record else (LOCAL, run)

    def run_command(self, *argv, done, timeout=90):
        """`sandbox-ai ARGV` for the run shown, on its host (this machine: sandbox-ai's own call)."""
        target = self.target()
        if target is None:
            return
        if target[0] == LOCAL:
            background(self.sai.call, *argv, timeout=timeout, work=True, done=done)
        else:
            background(self.sai.host_run, target[0], *argv, timeout=timeout, work=True, done=done)

    def on_host(self, text):
        target = self.target()
        return tr("On {host}: {text}", host=printable(target[0]), text=text) if target and target[0] != LOCAL else text

    def untaint_run(self):
        """`sandbox-ai net untaint RUN` (on its host): the rules for tainted runs no longer hold - asked first."""
        target = self.target()
        if target is None:
            return
        run = target[1]
        if not ask_first(self, "net-untaint", tr("Untaint {run}", run=printable(run)), self.on_host(tr(
                "Take back the taint of {run}? Untrusted content entered it; the rules for tainted runs (what it may "
                "reach, what is asked) no longer hold until such content enters it again. Only when you checked what "
                "came in.", run=printable(run))), tr("Untaint")):
            return
        self.message.setText(tr("Working…"))
        self.run_command("net", "untaint", run, done=lambda result, error: self.done(result, error))

    def keep_run(self, every):
        """`sandbox-ai net show RUN --keep` (EVERY: --keep-all) on its host: what the run was given goes into its
        project's .sandbox-ai.json, for every later start of that project - asked first."""
        target = self.target()
        if target is None:
            return
        host, run = target
        text = tr("Keep what you gave {run} (allowed names and addresses, networks, DNS, ports, devices, workspaces) "
                  "in its project's .sandbox-ai.json? Every later start of that project gets it.", run=printable(run))
        if every:
            text += " " + tr("Also what approvals opened once or for the run.")
        if not ask_first(self, "net-keep", tr("Keep for later starts: {run}", run=printable(run)), self.on_host(text),
                         tr("Keep")):
            return
        self.message.setText(tr("Working…"))
        background(self.sai.net_keep, host, run, every, timeout=120, work=True,
                   done=lambda result, error: self.done(result, error))

    def copy_text(self):
        """The plain text of `sandbox-ai net show RUN` (on its host) to the clipboard."""
        target = self.target()
        if target is None:
            return

        def copied(result, error):
            status, out, err = result if error is None else (1, "", str(error))
            if status != 0:
                self.message.setText(tr("Not copied: {error}", error=printable_text((err or out).strip())))
                return
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(out)
            self.message.setText(tr("Copied: what {run} may reach, as sandbox-ai net show prints it.",
                                    run=printable(target[1])))
        self.run_command("net", "show", target[1], done=copied, timeout=60)

    def done(self, result, error):
        status, out, err = result if error is None else (1, "", str(error))
        self.message.setText(printable_text((out or "").strip())[-400:] or tr("Done.") if status == 0 else
                             tr("Not done: {error}", error=printable_text((err or out).strip())[-400:]))
        self.refresh()

    def revoke_selected(self):
        # The run the rows were read of: another sandbox chosen above while its rows are read still shows these.
        chosen = self.selected()
        run = self.rows_run
        if not chosen or run in (None, ALL):
            return
        record = self.header.remote.get(run)
        text = tr("The sandbox may no longer reach or use:") + "\n" + "\n".join(
            f"- {printable(r['destination'])}" + (f" ({r['kind']}: {printable(r['names'])})"
                                                  if r["revoke"][0] in ("--api-key", "--git-token-secret") else "")
            for r in chosen)
        if record:
            text = tr("On {host}: {text}", host=printable(record["host"]), text=text)
        if not ask_first(self, "net-revoke", tr("Revoke?"), text, tr("Revoke")):
            return
        options = [token for r in chosen for token in r["revoke"]]
        self.message.setText(tr("Working…"))
        if record:
            background(self.sai.host_run, record["host"], "net", "revoke", record["name"], *options, timeout=90,
                       work=True, source=self.revoke, done=lambda result, error: self.revoked(result, error))
            return
        background(self.sai.call, "net", "revoke", run, *options, timeout=90, work=True, source=self.revoke,
                   done=lambda result, error: self.revoked(result, error))

    def revoked(self, result, error):
        status, out, err = result if error is None else (1, "", str(error))
        self.message.setText(tr("Revoked.") if status == 0 else tr("Not revoked: {error}",
                                                                   error=printable_text((err or out).strip())))
        self.refresh()


def taint_line(taint):
    """What net show says of the run's taint, for the summary ("" when not said or not tainted)."""
    if not isinstance(taint, dict) or not taint.get("tainted"):
        return ""
    source = taint.get("source") if isinstance(taint.get("source"), dict) else {}
    return "  " + tr("Untrusted content: tainted since {since} by {what}", since=printable(taint.get("since") or "?"),
                     what=printable(source.get("what") or "?"))


def row_identity(row):
    """The key of a row of AllowedSection (allowed_rows, started_rows): its kind and its destination (marked for one of
    its start's)."""
    return ("start", row["kind"], row["destination"]) if row.get("start") else (row["kind"], row["destination"])


def allowed_rows(shown):
    """The rows of AllowedSection for `sandbox-ai net show --json` SHOWN: destination, names, kind, and the options
    `net revoke` takes it back with (None: not from here)."""
    rows = []
    hosts = {}
    for entry in shown.get("https") or []:
        names = [n for n in entry.get("names") or [] if isinstance(n, str)]
        key = ", ".join(names)
        if names:
            hosts.setdefault(key, []).append(str(entry.get("address")))
        else:
            rows.append({"destination": str(entry.get("address")), "names": "", "kind": tr("HTTPS (port 443)"),
                         "revoke": ["--allow", str(entry.get("address"))]})
    for names, addresses in hosts.items():
        rows.append({"destination": names, "names": ", ".join(addresses), "kind": tr("HTTPS (port 443)"),
                     "revoke": [token for name in names.split(", ") for token in ("--allow-host", name)]})
    for entry in shown.get("tcp") or []:
        address, port = str(entry.get("address")), entry.get("port")
        target = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        rows.append({"destination": target, "names": ", ".join(entry.get("names") or []), "kind": tr("TCP port"),
                     "revoke": ["--allow-tcp", target]})
    for network in shown.get("all_ports") or []:
        rows.append({"destination": str(network), "names": "", "kind": tr("every port and protocol"),
                     "revoke": ["--allow", str(network)]})
    for server in shown.get("dns") or []:
        rows.append({"destination": str(server), "names": "", "kind": tr("DNS (lookups)"), "revoke": ["--dns"]})
    for port in shown.get("host_ports") or []:
        rows.append({"destination": f"127.0.0.1:{port}", "names": "", "kind": tr("a port of this computer"),
                     "revoke": ["--allow-host-port", str(port)]})
    shared = shown.get("shared_wildcards") if isinstance(shown.get("shared_wildcards"), dict) else {}
    for wildcard in shown.get("host_wildcards") or []:
        # Below a shared service (net show's shared_wildcards): marked as the risk you confirmed.
        kind = tr("host wildcard below the shared service {suffix} (anyone can rent a name there)",
                  suffix=str(shared[wildcard])) if wildcard in shared else tr("host wildcard")
        rows.append({"destination": str(wildcard), "names": "", "kind": kind, "revoke": None})
    # What the run was given during the run beside destinations: taken back here too (git-logins revoke, secrets
    # revoke - the reference after "=" is not read by a revoke).
    for login in shown.get("git") or []:
        if isinstance(login, dict) and isinstance(login.get("url"), str):
            rows.append({"destination": login["url"], "names": str(login.get("user") or ""),
                         "kind": tr("Git login (token in the sandbox)") if login.get("in_sandbox") else
                         tr("Git login (given during the run)"),
                         "revoke": ["--git-token-secret", f"{login['url']}=REVOKE"]})
    for key in shown.get("api_keys") or []:
        if isinstance(key, dict) and isinstance(key.get("name"), str):
            rows.append({"destination": str(key.get("host") or ""), "names": key["name"],
                         "kind": tr("API key (given during the run)"), "revoke": ["--api-key", key["name"]]})
    return rows
