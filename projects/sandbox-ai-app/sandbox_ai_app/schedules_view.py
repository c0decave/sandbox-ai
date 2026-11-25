"""Schedules on the Sessions page: the scheduled runs of this machine or of a host (`sandbox-ai schedule` there).

A schedule starts an agent on a timetable (--every, --at, --cron) or when a repository is pushed to (--on-push), each
run a normal start under its rules with the agent's one-shot mode; nobody answers questions at night, so its ask mode
(off by default: refused) says what happens to them. The table lists each schedule with when it runs, the agent, the
ask mode, the next and the last run and its state; Add…, Edit…, Run now, Pause, Resume, History…, Remove…, Refresh and
the timer (on or off) are beside it and on its right-click menu (Delete removes). History… lists a schedule's runs -
why, status, duration, what changed - and shows each run's report (its summary and the start's `sandbox-ai report`).
Adding (or changing) a schedule, removing it and the timer are asked first (schedule-add, schedule-remove,
schedule-timer); every command runs in the background, on another host through `sandbox-ai hosts run`.
"""
from __future__ import annotations

import shlex
import time

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from .files import printable, printable_text
from .host_choice import on_host
from .i18n import N_, tr
from .widgets import (
    ask_first, background, delete_key, explanation, fill, flow, list_menu, outcome, plain_label, selected_keys,
    set_enabled, show_text, table, warn,
)

LOCAL = "local"
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta",
          "pi")
WHEN = (("every", N_("every (30m, 2h, 1d)")), ("at", N_("at (HH:MM daily, or YYYY-MM-DD HH:MM once)")),
        ("cron", N_("cron (M H DOM MON DOW)")), ("", N_("only on push")))
ASK = (("off", N_("off: refused, nobody answers (the default)")),
       ("allow", N_("allow: every question allowed and recorded")), ("on", N_("on: questions wait for an answer")))
MISSED = (("once", N_("once on wake")), ("skip", N_("skip them")))
# Examples in the fields (names, paths, a duration, an agent's one-shot form: the same in every language)
EXAMPLE_NAME, EXAMPLE_PATH, EXAMPLE_ARGS, EXAMPLE_LIMIT = "nightly", "/home/you/src/app", 'exec "review"', "60m"
EXAMPLE_BRANCH, EXAMPLE_REMOTE, EXAMPLE_WHEN = "nightly-result", "origin", "02:30"
EXAMPLE_OPTIONS = "--offline -w /home/you/src/app"
STATES = {"active": N_("active"), "paused": N_("paused"), "running": N_("running"), "done": N_("done")}


def when_of(seconds):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds)) if isinstance(seconds, (int, float)) else "-"


def words(values):
    """A list of arguments as one line to edit (quoted where needed)."""
    return shlex.join(values) if isinstance(values, list) and all(isinstance(v, str) for v in values) else ""


class ScheduleDialog(QDialog):
    """One schedule: its name (fixed when editing), when, the agent (or a board), the task, the start's options, a
    preset, the workspace, the ask mode, the time limit, missed runs, commit and push, notifications. values() gives
    schedule.add_arguments' fields; CHECK(fields) (the command's own check) runs before it closes."""

    def __init__(self, check, schedule=None, parent=None):
        super().__init__(parent)
        self.check = check
        self.setWindowTitle(tr("Edit schedule") if schedule else tr("Add schedule"))
        self.editing = bool(schedule)
        schedule = schedule or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.name = QLineEdit(schedule.get("name", ""))
        self.name.setPlaceholderText(EXAMPLE_NAME)
        self.name.setReadOnly(self.editing)
        form.addRow(tr("Name"), self.name)
        timing = schedule.get("timing") or {}
        self.when = QComboBox()
        for key, text in WHEN:
            self.when.addItem(tr(text), key)
        self.when.setCurrentIndex(max(0, self.when.findData(timing.get("kind", "" if schedule else "every"))))
        self.when.setAccessibleName(tr("When"))
        self.when_value = QLineEdit(str(timing.get("value") or ""))
        self.when_value.setPlaceholderText(EXAMPLE_WHEN)
        self.when_value.setAccessibleName(tr("When"))
        form.addRow(tr("When"), flow(self.when, self.when_value))
        self.on_push = QLineEdit(schedule.get("on_push") or "")
        self.on_push.setPlaceholderText(EXAMPLE_PATH)
        form.addRow(tr("Also on a push to (a repository of that host)"), self.on_push)
        self.agent = QComboBox()
        self.agent.addItem(tr("(the board's)"), "")
        for agent in AGENTS:
            self.agent.addItem(agent, agent)
        self.agent.setCurrentIndex(max(0, self.agent.findData("" if schedule.get("board") else
                                                              schedule.get("agent", "claude"))))
        form.addRow(tr("Agent"), self.agent)
        self.prompt = QPlainTextEdit(schedule.get("prompt") or "")
        self.prompt.setPlaceholderText(tr("Review yesterday's commits"))
        self.prompt.setFixedHeight(70)
        self.prompt.setTabChangesFocus(True)
        self.prompt.setAccessibleName(tr("Task"))
        form.addRow(tr("Task"), self.prompt)
        self.options = QLineEdit(words(schedule.get("options")))
        self.options.setPlaceholderText(EXAMPLE_OPTIONS)
        form.addRow(tr("Start options"), self.options)
        self.agent_args = QLineEdit(words(schedule.get("agent_args")))
        self.agent_args.setPlaceholderText(EXAMPLE_ARGS)
        form.addRow(tr("Agent arguments (its one-shot form, without a task)"), self.agent_args)
        self.preset = QLineEdit(schedule.get("preset") or "")
        form.addRow(tr("Preset"), self.preset)
        self.board = QLineEdit(schedule.get("board") or "")
        form.addRow(tr("Board (kept on that host)"), self.board)
        self.workdir = QLineEdit(schedule.get("workdir") or "")
        self.workdir.setPlaceholderText(EXAMPLE_PATH)
        form.addRow(tr("Workspace"), self.workdir)
        self.ask = QComboBox()
        for key, text in ASK:
            self.ask.addItem(tr(text), key)
        self.ask.setCurrentIndex(max(0, self.ask.findData(schedule.get("ask", "off"))))
        form.addRow(tr("Questions"), self.ask)
        self.limit = QLineEdit(f"{round(schedule['limit'] / 60)}m" if isinstance(schedule.get("limit"), int) else "")
        self.limit.setPlaceholderText(EXAMPLE_LIMIT)
        form.addRow(tr("Time limit"), self.limit)
        self.missed = QComboBox()
        for key, text in MISSED:
            self.missed.addItem(tr(text), key)
        self.missed.setCurrentIndex(max(0, self.missed.findData(schedule.get("missed", "once"))))
        form.addRow(tr("Runs missed while off"), self.missed)
        self.commit = QLineEdit(schedule.get("commit") or "")
        self.commit.setPlaceholderText(EXAMPLE_BRANCH)
        form.addRow(tr("Commit the changes to the branch"), self.commit)
        self.push = QLineEdit(schedule.get("push") or "")
        self.push.setPlaceholderText(EXAMPLE_REMOTE)
        form.addRow(tr("Then push it to the remote"), self.push)
        self.notify = QCheckBox(tr("Notify when a run ends"))
        self.notify.setChecked(schedule.get("notify", True) is not False)
        form.addRow(self.notify)
        form.addRow(explanation(tr("Each run is a normal start under its rules, with the agent's one-shot mode. Nobody "
                                   "answers questions at night: off refuses what is not allowed.")))
        self.problem = plain_label("", role="notice-error", selectable=True)
        self.problem.hide()
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("Save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Cancel"))
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        fields = {"name": self.name.text().strip(), "ask": self.ask.currentData(),
                  "missed": self.missed.currentData(), "notify": self.notify.isChecked(), "replace": self.editing}
        kind, value = self.when.currentData(), self.when_value.text().strip()
        if kind and value:
            fields[kind] = value
        for key, edit in (("on_push", self.on_push), ("options", self.options), ("agent_args", self.agent_args),
                          ("preset", self.preset), ("board", self.board), ("workdir", self.workdir),
                          ("limit", self.limit), ("commit", self.commit), ("push", self.push)):
            if edit.text().strip():
                fields[key] = edit.text().strip()
        if self.agent.currentData():
            fields["agent"] = self.agent.currentData()
        if self.prompt.toPlainText().strip():
            fields["prompt"] = self.prompt.toPlainText().strip()
        return fields

    def save(self):
        try:
            self.check(self.values())
        except (ValueError, TypeError, AttributeError) as exc:
            self.problem.setText(printable_text(str(exc))[:500])
            self.problem.show()
            return
        self.accept()


class HistoryDialog(QDialog):
    """A schedule's runs, the newest first: why, started, status, duration, what changed; Report… (also on the
    right-click menu and a double click) shows a run's summary and the start's report."""

    def __init__(self, sai, host, name, runs, parent=None):
        super().__init__(parent)
        self.sai, self.host, self.name = sai, host, name
        self.runs = list(reversed(runs))
        self.setWindowTitle(tr("History of {name}", name=printable(name)))
        self.resize(820, 420)
        layout = QVBoxLayout(self)
        self.list = table([tr("Run"), tr("Why"), tr("Started"), tr("Status"), tr("Duration"), tr("Changed")],
                          stretch=5, compact=(1, 3, 4))
        self.list.setAccessibleName(tr("Runs"))
        rows, colors = [], []
        for run in self.runs:
            found = run.get("changes") if isinstance(run.get("changes"), dict) else None
            rows.append([printable(run.get("run", "")), printable(run.get("reason", "")), when_of(run.get("started")),
                         printable(run.get("status", "")), f"{round(run.get('seconds') or 0)} s",
                         tr("{files} file(s), {commits} commit(s)", files=found.get("files", 0),
                            commits=found.get("commits", 0)) if found else printable(run.get("note") or "-")])
            colors.append({"ok": "ok", "skipped": None}.get(run.get("status"), "bad"))
        fill(self.list, rows, colors, keys=[str(run.get("run", "")) for run in self.runs])
        self.list.itemDoubleClicked.connect(lambda _item: self.report())
        layout.addWidget(self.list, 1)
        self.report_button = QPushButton(tr("Report…"))
        self.report_button.setToolTip(tr("The run's summary and its start's report (sandbox-ai schedule report)"))
        self.report_button.clicked.connect(lambda _=False: self.report())
        self.status = plain_label(role="secondary", selectable=True)
        row = QHBoxLayout()
        row.addWidget(self.status, 1)
        row.addWidget(self.report_button)
        layout.addLayout(row)
        list_menu(self.list, lambda: [self.report_button])
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("Close"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.runs:
            self.list.selectRow(0)
        else:
            self.status.setText(tr("No runs yet."))
        self.report_button.setEnabled(bool(self.runs))

    def chosen(self):
        keys = selected_keys(self.list)
        return next((run for run in self.runs if keys and str(run.get("run")) == keys[0]), None)

    def report(self):
        run = self.chosen()
        if run is None:
            return
        if not (run.get("summary") or run.get("report")):
            self.status.setText(printable(run.get("note") or tr("No report for this run.")))
            return
        host, name, ident = self.host, self.name, str(run.get("run"))
        self.status.setText(tr("Reading…"))
        background(lambda: self.sai.schedule_report(host, name, ident), owner=self, source=[self.report_button],
                   timeout=120, done=lambda result, error: self.shown(ident, result, error))

    def shown(self, ident, result, error):
        if error is not None:
            self.status.setText(tr("No report: {error}", error=printable_text(str(error))[:400]))
            return
        self.status.setText("")
        show_text(self, tr("Report of {run}", run=printable(ident)), printable_text(result), modal=False)


class SchedulesBox(QGroupBox):
    """The schedules of the host chosen: a table, Add…, Edit…, Run now, Pause, Resume, History…, Remove…, Refresh
    and the timer (also on the right-click menu)."""

    def __init__(self, sai, parent=None):
        super().__init__(tr("Schedules"), parent)
        from .settings import host_row
        self.sai, self.schedules, self.timer, self.token = sai, [], {}, 0
        # (a sandbox-ai without schedule.py, or a stand-in of the tests without it: the box says so, its buttons off)
        has = getattr(sai, "has_schedule", None)
        self.available = bool(callable(has) and has())
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.hosts = host_row(sai, self)
        top.addWidget(self.hosts, 1)
        self.timer_label = plain_label(role="secondary")
        top.addWidget(self.timer_label)
        self.timer_button = QPushButton(tr("Turn the timer on…"))
        self.timer_button.setToolTip(tr("The timer runs sandbox-ai schedule tick every minute as that host's user (a "
                                        "systemd user timer or a LaunchAgent): without it no schedule starts by "
                                        "itself (sandbox-ai schedule enable, disable)"))
        self.timer_button.clicked.connect(lambda _=False: self.toggle_timer())
        top.addWidget(self.timer_button)
        layout.addLayout(top)
        self.list = table([tr("Name"), tr("When"), tr("Agent"), tr("Questions"), tr("Next run"), tr("Last run"),
                           tr("State")], stretch=1, compact=(0, 2, 3, 6))
        self.list.setAccessibleName(tr("Schedules"))
        self.list.setMinimumHeight(110)
        self.list.itemSelectionChanged.connect(self.update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.edit())
        layout.addWidget(self.list)
        self.buttons_by = {}
        for key, text, tip, slot in (
                ("add", tr("Add…"), tr("A new schedule: when, the agent with its task and options, what happens to its "
                                       "questions (sandbox-ai schedule add)"), self.add),
                ("edit", tr("Edit…"), tr("Change the selected schedule; its history stays"), self.edit),
                ("run", tr("Run now"), tr("One run of the selected schedule now, as the timer would start it "
                                          "(sandbox-ai schedule run-now)"), self.run_now),
                ("pause", tr("Pause"), tr("The selected schedule starts nothing until it is resumed"), self.pause),
                ("resume", tr("Resume"), tr("The selected schedule goes on; what was due meanwhile is not made up"),
                 self.resume),
                ("history", tr("History…"), tr("The runs of the selected schedule with their reports (sandbox-ai "
                                               "schedule history)"), self.history),
                ("remove", tr("Remove…"), tr("Remove the selected schedule with its history"), self.remove),
                ("refresh", tr("Refresh"), tr("Read the schedules again"), self.refresh)):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setAccessibleName(text.rstrip("…"))
            button.clicked.connect(lambda _=False, s=slot: s())
            self.buttons_by[key] = button
        self.buttons_by["remove"].setProperty("role", "danger")
        layout.addWidget(flow(*self.buttons_by.values()))
        list_menu(self.list, lambda: [self.buttons_by[key] for key in ("run", "edit", "history", "pause", "resume")]
                  + [None, self.buttons_by["remove"], None, self.buttons_by["add"], self.buttons_by["refresh"],
                     self.timer_button])
        delete_key(self.list, self.remove)
        self.status = plain_label(role="secondary", selectable=True, log="sandboxes")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.read_once = False  # read when the box is first shown (no SSH or process for a page never looked at)
        if not self.available:
            self.status.setText(tr("This sandbox-ai has no schedules yet; update it."))
            for button in self.buttons():
                button.setEnabled(False)
        else:
            self.update_buttons()

    def showEvent(self, event):  # noqa: N802 - Qt's name
        super().showEvent(event)
        if self.available and not self.read_once:
            self.read_once = True
            self.refresh()

    def buttons(self):
        return [*self.buttons_by.values(), self.timer_button]

    def host(self):
        return self.hosts.host() or LOCAL

    def where(self, host=None):
        return self.hosts.label(host or self.host())

    def refresh(self):
        host = self.host()
        self.token += 1
        token = self.token
        self.status.setText(tr("Asking {host}…", host=self.where(host)))
        background(lambda: self.sai.schedules(host), owner=self, source=[self.buttons_by["refresh"]], timeout=120,
                   done=lambda result, error: self.loaded(token, host, result, error))

    def loaded(self, token, host, result, error):
        if token != self.token:
            return
        if error is not None:
            self.schedules, self.timer = [], {}
            fill(self.list, [])
            self.status.setText(
                tr("The sandbox-ai of {host} has no schedules yet; update it (Hosts page).", host=self.where(host))
                if "unknown command 'schedule'" in str(error) else
                tr("Cannot list the schedules: {error}", error=printable_text(str(error))[:400]))
            self.update_buttons()
            return
        self.schedules = result.get("schedules", [])
        self.timer = result.get("timer") if isinstance(result.get("timer"), dict) else {}
        rows, colors = [], []
        for x in self.schedules:
            last = x.get("last") if isinstance(x.get("last"), dict) else None
            agent = printable(x.get("agent") or "") + (f" ({printable(x['board'])})" if x.get("board") else "")
            rows.append([printable(x["name"]), printable(x.get("when", "")), agent, printable(x.get("ask", "")),
                         when_of(x.get("next")),
                         f"{printable(last.get('status', ''))} {when_of(last.get('started'))}" if last else "-",
                         tr(STATES[x["state"]]) if x.get("state") in STATES else printable(x.get("state", ""))])
            colors.append(None if last is None or last.get("status") in ("ok", "skipped") else "bad")
        fill(self.list, rows, colors, keys=[x["name"] for x in self.schedules])
        on = self.timer_on()
        self.timer_label.setText(tr("Timer: on") if on else tr("Timer: off - nothing starts by itself"))
        self.timer_button.setText(tr("Turn the timer off…") if on else tr("Turn the timer on…"))
        self.status.setText(tr("{count} schedules on {host}. Each run is a normal start under its rules; nobody "
                               "answers its questions at night.", count=len(self.schedules), host=self.where(host))
                            if self.schedules else
                            tr("No schedules on {host}. Add one: an agent started on a timetable or a trigger, with a "
                               "report of each run.", host=self.where(host)))
        self.update_buttons()

    def timer_on(self):
        return bool(self.timer.get("installed")) and self.timer.get("active") is not False

    def chosen(self):
        keys = selected_keys(self.list)
        return next((x for x in self.schedules if keys and x["name"] == keys[0]), None)

    def update_buttons(self):
        if not self.available:
            return
        x = self.chosen()
        # (greyed with the reason, also on the right-click menu: the rule of every list)
        none = tr("Select a schedule first.")
        for key in ("edit", "history", "remove"):
            set_enabled(self.buttons_by[key], x is not None, none)
        set_enabled(self.buttons_by["run"], x is not None and not x.get("running"),
                    none if x is None else tr("It runs now."))
        set_enabled(self.buttons_by["pause"], x is not None and not x.get("paused"),
                    none if x is None else tr("It is paused already."))
        set_enabled(self.buttons_by["resume"], x is not None and bool(x.get("paused")),
                    none if x is None else tr("It is not paused."))

    def check(self, fields):
        return self.sai.module("schedule").add_arguments(fields)

    def add(self):
        self.open_dialog(None)

    def edit(self):
        x = self.chosen()
        if x is not None:
            self.open_dialog(x)

    def open_dialog(self, schedule):
        host = self.host()
        dialog = ScheduleDialog(self.check, schedule, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        fields = dialog.values()
        dialog.deleteLater()  # (made for this one use)
        if not accepted:
            return
        if not ask_first(self, "schedule-add", tr("Schedules"), on_host(host, tr(
                "Let this host start {agent} by itself as the schedule {name} says? Each run is a normal start under "
                "its rules; its questions: {ask}.", agent=printable(fields.get("agent") or tr("(the board's)")),
                name=printable(fields["name"]), ask=printable(fields["ask"]))), tr("Save")):
            return
        said = tr("Saved: {name}", name=printable(fields["name"]))
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.schedule_add(host, fields), owner=self, work=True, source=self.buttons(),
                   timeout=180, done=lambda result, error: self.done(said, result, error))

    def act(self, action, said):
        x, host = self.chosen(), self.host()
        if x is None:
            return
        name = x["name"]
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.schedule_action(host, name, action), owner=self, work=True, source=self.buttons(),
                   timeout=180, done=lambda result, error: self.done(said.format(name=printable(name)), result,
                                                                      error))

    def run_now(self):
        self.act("run-now", tr("Started: {name}", name="{name}"))

    def pause(self):
        self.act("pause", tr("Paused: {name}", name="{name}"))

    def resume(self):
        self.act("resume", tr("Resumed: {name}", name="{name}"))

    def remove(self):
        x, host = self.chosen(), self.host()
        if x is None:
            return
        name = x["name"]
        if not ask_first(self, "schedule-remove", tr("Schedules"), on_host(host, tr(
                "Remove the schedule {name}? It starts nothing any more; its history and reports go too.",
                name=printable(name))), tr("Remove")):
            return
        said = tr("Removed: {name}", name=printable(name))
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.schedule_action(host, name, "remove"), owner=self, work=True,
                   source=self.buttons(), timeout=180, done=lambda result, error: self.done(said, result, error))

    def history(self):
        x, host = self.chosen(), self.host()
        if x is None:
            return
        name = x["name"]
        self.status.setText(tr("Reading…"))
        background(lambda: self.sai.schedule_history(host, name), owner=self, source=[self.buttons_by["history"]],
                   timeout=120, done=lambda result, error: self.history_shown(host, name, result, error))

    def history_shown(self, host, name, result, error):
        if error is not None:
            self.status.setText(tr("Cannot read the history: {error}", error=printable_text(str(error))[:400]))
            return
        self.status.setText("")
        self.history_dialog = HistoryDialog(self.sai, host, name, result, self)
        self.history_dialog.open()

    def toggle_timer(self):
        host, on = self.host(), not self.timer_on()
        text = tr("Turn the timer of this host on? It runs sandbox-ai schedule tick every minute as that host's user "
                  "(a systemd user timer or a LaunchAgent).") if on else \
            tr("Turn the timer of this host off? No schedule starts by itself until it is on again.")
        if not ask_first(self, "schedule-timer", tr("Schedules"), on_host(host, text),
                         tr("Turn on") if on else tr("Turn off")):
            return
        said = tr("Timer: on") if on else tr("Timer: off - nothing starts by itself")
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.schedule_timer(host, on), owner=self, work=True, source=self.buttons(),
                   timeout=180, done=lambda result, error: self.done(said, result, error))

    def done(self, said, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            warn(self, tr("Schedules"), printable_text((out + err).strip())[-1500:] or
                 tr("Failed (exit status {status}).", status=status))
            self.status.setText(tr("Not done."))
        else:
            self.status.setText(said)
        self.refresh()
