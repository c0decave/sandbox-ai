"""The Approvals page's parts besides the open questions: which sandbox it is about and how that one runs
(ContextHeader), what it refused and why, with what may be done about it (BlockedSection), and what it may reach now
beyond its start options (AllowedSection).

The page follows the active tab (its run, its session's run, or the newest run of its project); the header's box
chooses another running sandbox, or all of them. Everything is read in threads, bounded (sandbox-ai's
approvals.blocked reads the newest part of each log only); every change goes through sandbox-ai's own commands
(`sandbox-ai approvals allow|deny|ask-again`, `sandbox-ai net allow|revoke`), which keep to the run's security mode,
the administrator's policy, Tor and the kill switch - the supervisor checks again what reaches it."""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .files import printable
from .i18n import tr
from .widgets import (
    background, compact_combo, confirm, fill, flow, plain_label, row_key, selected_rows, set_enabled, table, warn,
)

FOLLOW, ALL = "follow", "all"
BLOCKED_INTERVAL = 5000  # ms between two reads of the Blocked list while it is shown
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
            return tr("Rule {rule} of the ruleset {ruleset} asks before it, and nobody answered in time. It is "
                      "refused for 2 minutes, then asked again. Allowing its host in the network settings does not "
                      "change that: allow it here.", rule=rule, ruleset=ruleset)
        return tr("It was asked about, and nobody answered in time. It is refused for 2 minutes, then asked again.")
    if kind == "denied":
        return tr("You denied it for this run. “Ask again” takes that back; “Allow for this run” allows it.")
    if kind == "mode":
        return tr("The run's security mode refuses it. Start again with a laxer mode if you mean it.")
    if kind == "policy":
        return tr("The administrator's policy refuses it. Only your administrator can change that.")
    if kind == "cut":
        return tr("The kill switch cuts it. What you allow meanwhile stays cut until the cut is lifted (Sandboxes › "
                  "Kill switch).")
    if kind == "blocked":
        return tr("You blocked it always for this project (Remembered › Forget lifts it).")
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.purpose = plain_label(tr("What your sandboxes ask, what they were refused and why, and what they may "
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

    def run(self):
        """The run shown: a run's name, ALL, or None (the active tab has none running)."""
        chosen = self.choice.currentData()
        if chosen == FOLLOW:  # the active tab's; when it has none running, the only one that runs, or all of them
            if self.active in self.runs:
                return self.active
            return next(iter(self.runs)) if len(self.runs) == 1 else ALL if self.runs else None
        return chosen if chosen == ALL or chosen in self.runs else None

    def records(self):
        """[(run, record)] shown."""
        run = self.run()
        if run == ALL:
            return sorted(self.runs.items())
        return [(run, self.runs[run])] if run else []

    def show_state(self):
        run = self.run()
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
            except Exception:  # noqa: BLE001 - an older sandbox-ai, an unreadable state: not said
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
        self.allow_always = QPushButton(tr("Allow always…"))
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
        self.note = plain_label(tr("Opening a host in the network or VPN settings does not change what a ruleset "
                                   "decides: a request a ruleset asks about is asked even when its host is allowed."),
                                role="secondary")
        layout.addWidget(self.note)
        self.table = table([tr("Time"), tr("Sandbox"), tr("Reason"), tr("What"), tr("Tried by"), tr("Times")],
                           multi=True, stretch=3, compact=(0, 1, 2, 5), numeric=(5,))
        self.table.setAccessibleName(tr("Blocked"))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, 1)
        self.empty = plain_label(role="secondary")
        layout.addWidget(self.empty)
        self.detail = plain_label(selectable=True)
        self.detail.setAccessibleName(tr("Why it was refused"))
        layout.addWidget(self.detail)
        self.message = plain_label(selectable=True)
        layout.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.isVisible() and self.refresh())
        self.timer.start(BLOCKED_INTERVAL)
        self.update_buttons()

    # -- reading --

    def refresh(self, force=False):
        """Read the list anew in a thread (one read at a time; FORCE: also when one runs, its result is dropped)."""
        if self.reading is not None and not force:
            return
        records = self.header.records()
        runs = dict(self.header.runs)
        self.generation += 1
        token = self.generation
        if not records:
            self.reading = None
            self.apply(token, [], None)
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
            self.message.setText(tr("Cannot read what was refused: {error}", error=printable(error)))
            return
        self.entries = list(result or [])
        self.show_entries()

    def visible_entries(self):
        kinds = self.filter.currentData()
        return [entry for entry in self.entries if kinds is None or entry.get("kind") in kinds]

    def show_entries(self):
        entries = self.visible_entries()
        keys = [(e.get("run"), e.get("question") or (e.get("kind"), e.get("what"))) for e in entries]
        fill(self.table, [[local_time(e.get("time")), printable(e.get("run") or ""),
                           kind_label(e.get("kind")), printable(e.get("what") or ""), printable(e.get("process") or ""),
                           int(e.get("count") or 1)] for e in entries],
             colors=["muted" if e.get("kind") in ("mode", "policy", "cut") else "warn" for e in entries], keys=keys)
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

    def act(self, action):
        chosen = [entry for entry in self.selected() if action in (entry.get("actions") or [])]
        if not chosen or self.acting is not None:
            return
        what = "\n".join(f"- {printable(entry.get('what') or '')}" for entry in chosen[:12])
        if action == "allow-run":
            if not confirm(self, tr("Allow for this run?"), tr("The sandbox may do this until it ends:") + "\n" + what,
                           tr("Allow for this run")):
                return
        elif action == "allow-always":
            text = self.always_text(chosen)
            if text is None or not confirm(self, tr("Allow always for this project?"), text, tr("Allow always")):
                return
        records = dict(self.header.runs)
        self.acting = background(self.run_actions, action, chosen, records, timeout=ACTION_WAIT, work=True,
                                 done=lambda result, error: self.acted(action, result, error))
        self.message.setText(tr("Working…"))
        self.update_buttons()

    def always_text(self, chosen):
        """What "Allow always" changes for CHOSEN, said before it is done (None: refused, said)."""
        lines = []
        approvals = self.sai.module("approvals")
        for entry in chosen:
            option = target_option(entry.get("target"))
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
                                                  error=printable(exc)), modal=False)
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

    def run_actions(self, action, chosen, records):
        """Each entry's commands (in the thread): [(entry, (status, out, err))]."""
        done = []
        for entry in chosen:
            run, question = str(entry.get("run") or ""), entry.get("question")
            option = target_option(entry.get("target"))
            try:
                if action == "ask-again":
                    result = self.sai.call("approvals", "ask-again", question, timeout=60)
                elif action == "deny-run":
                    result = self.sai.call("approvals", "deny", question, "--scope", "run", timeout=60)
                elif action == "allow-run" and question and entry.get("question_kind") != "dns" and not (
                        entry.get("kind") == "network" and option):
                    result = self.sai.call("approvals", "allow", question, "--scope", "run", timeout=60)
                elif option:  # a destination: as sandbox-ai net allow (resolved through the run's VPN or Tor)
                    keep = ["--keep"] if action == "allow-always" else []
                    result = self.sai.call("net", "allow", run, *option, *keep, timeout=90)
                    if question and result[0] == 0:
                        self.sai.call("approvals", "ask-again", question, timeout=60)  # no remembered deny left
                else:  # allow-always for a request or tool call: an exception rule, then allowed for this run
                    plan = self.sai.module("approvals").rule_exception(records.get(run) or {}, entry, write=True)
                    result = (0, json.dumps(plan), "")
                    if question:
                        result = self.sai.call("approvals", "allow", question, "--scope", "run", timeout=60)
            except (ValueError, OSError) as exc:
                result = (1, "", str(exc))
            done.append((entry, result))
        return done

    def acted(self, action, result, error):
        self.acting = None
        if error is not None:
            self.message.setText(tr("Not done: {error}", error=printable(error)))
        else:
            failed = [(entry, err or out) for entry, (status, out, err) in result if status != 0]
            if failed:
                self.message.setText("\n".join(tr("{what}: {error}", what=printable(entry.get("what")),
                                                  error=printable(str(why).strip().splitlines()[-1] if str(why).strip()
                                                                  else "?")) for entry, why in failed[:6]))
            else:
                cut = [entry for entry, _ in result if self.cut_of(entry.get("run"))]
                self.message.setText(tr("Done.") + ((" " + tr("The kill switch holds: what you allowed stays cut until "
                                                              "the cut is lifted.")) if cut and action.startswith(
                                                                  "allow") else ""))
        self.update_buttons()
        self.refresh(force=True)

    def cut_of(self, run):
        record = self.header.runs.get(run)
        try:
            return bool(record) and bool(self.sai.module("net").cut_state(record["directory"]))
        except Exception:  # noqa: BLE001 - not known: not said
            return False


class AllowedSection(QWidget):
    """What the sandbox shown may reach beyond its start options: what was allowed during the run (sandbox-ai net
    allow, this page, answers for the run or always), from `sandbox-ai net show --json`; Revoke takes it back."""
    counted = Signal(int)

    def __init__(self, sai, header):
        super().__init__()
        self.sai, self.header = sai, header
        self.rows, self.reading, self.generation = [], None, 0
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.revoke = QPushButton(tr("Revoke…"))
        self.revoke.setAccessibleName(tr("Revoke"))
        self.revoke.setToolTip(tr("The sandbox may no longer reach it (a connection open already stays until it ends)"))
        self.revoke.clicked.connect(self.revoke_selected)
        refresh = QPushButton(tr("Refresh"))
        refresh.setAccessibleName(tr("Refresh what it may reach"))
        refresh.clicked.connect(lambda: self.refresh())
        self.summary = plain_label(role="secondary")
        top.addWidget(self.revoke)
        top.addWidget(refresh)
        top.addStretch()
        layout.addLayout(top)
        layout.addWidget(self.summary)
        self.table = table([tr("Destination"), tr("Names"), tr("Kind")], multi=True, stretch=1, compact=(0, 2))
        self.table.setAccessibleName(tr("Allowed now"))
        self.table.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.table, 1)
        layout.addWidget(plain_label(tr("Listed: what was allowed while it runs (on this page, with sandbox-ai net "
                                        "allow, or by an answer). What its start options allow is on the Board "
                                        "(Network); what is kept for the project, under Remembered."),
                                     role="secondary"))
        self.message = plain_label(selectable=True)
        layout.addWidget(self.message)
        self.update_buttons()

    def refresh(self):
        run = self.header.run()
        self.generation += 1
        token = self.generation
        if run in (None, ALL):
            self.apply(token, None, None)
            return
        self.reading = background(self.sai.call, "net", "show", run, "--json", timeout=60,
                                  done=lambda result, error: self.apply(token, result, error))

    def apply(self, token, result, error):
        if token != self.generation:
            return
        self.reading = None
        run_shown = self.header.run()
        if result is None and error is None:
            self.rows = []
            self.summary.setText(tr("Choose one running sandbox above to see what it may reach.") if run_shown == ALL
                                 else tr("No sandbox runs: nothing to show."))
        elif error is not None or result[0] != 0:
            self.rows = []
            self.summary.setText(tr("Cannot read it: {error}", error=printable(error or result[2] or result[1])))
        else:
            try:
                shown = json.loads(result[1])
            except ValueError:
                shown = {}
            self.rows = allowed_rows(shown)
            self.summary.setText(tr("{count} destinations allowed during this run", count=len(self.rows)) + (
                "  " + tr("(the kill switch cuts them now: {mode})", mode=printable(shown["cut"]))
                if shown.get("cut") else ""))
        fill(self.table, [[printable(r["destination"]), printable(r["names"]), r["kind"]] for r in self.rows],
             keys=[r["destination"] for r in self.rows])
        self.counted.emit(len(self.rows))
        self.update_buttons()

    def selected(self):
        keys = [row_key(self.table, row) for row in selected_rows(self.table)]
        return [r for r in self.rows if r["destination"] in keys and r["revoke"]]

    def update_buttons(self):
        set_enabled(self.revoke, bool(self.selected()), tr("Choose what to revoke first."))

    def revoke_selected(self):
        chosen = self.selected()
        run = self.header.run()
        if not chosen or run in (None, ALL):
            return
        if not confirm(self, tr("Revoke?"), tr("The sandbox may no longer reach:") + "\n" +
                       "\n".join(f"- {printable(r['destination'])}" for r in chosen), tr("Revoke"), destructive=True):
            return
        options = [token for r in chosen for token in r["revoke"]]
        self.message.setText(tr("Working…"))
        background(self.sai.call, "net", "revoke", run, *options, timeout=90, work=True, source=self.revoke,
                   done=lambda result, error: self.revoked(result, error))

    def revoked(self, result, error):
        status, out, err = result if error is None else (1, "", str(error))
        self.message.setText(tr("Revoked.") if status == 0 else tr("Not revoked: {error}",
                                                                   error=printable((err or out).strip())))
        self.refresh()


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
    for wildcard in shown.get("host_wildcards") or []:
        rows.append({"destination": str(wildcard), "names": "", "kind": tr("host wildcard"), "revoke": None})
    return rows
