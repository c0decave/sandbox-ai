"""The Teams page (sandbox-ai team): teams of agents, one sandbox per agent on a shared workspace, started and stopped
together, with a task board between them - on this machine or on a host of `sandbox-ai hosts` (over SSH).

  - the list is `sandbox-ai team list`, a team's members and its board `sandbox-ai team show --json NAME`;
  - Start runs `sandbox-ai team start --agents A,B[,C] --workspace DIR NAME [-- OPTIONS]` in a terminal tab on that host
    (each member starts there, one after the other, and says so);
  - Post a task is `sandbox-ai team post NAME AGENT TASK` (a task for a member, from you);
  - Talk to the lead opens `sandbox-ai attach NAME-LEAD` in a terminal tab;
  - Stop (`team stop`) and Delete (`team delete --yes`: with the members' HOMEs and the board) are asked first
    (start-stop, start-delete).

What a team's board holds comes from its members: shown through printable(), never as markup. Names that go back as
arguments are checked first (a team's name, an agent of the three), and passed after "--"."""
from __future__ import annotations

import re
import shlex

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from .files import printable
from .i18n import N_, tr
from .widgets import (
    ask_first, background, fill, flow, outcome, plain_label, safe_tooltip, selected_keys, set_enabled, table, warn,
)

LOCAL = "local"
AGENTS = ("claude", "codex", "opencode")
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")  # team.py's TEAM_NAME
NAME_EXAMPLE = "review"  # the same in every language
OPTIONS_EXAMPLE = "--allow-pypi --kit egm"
STATES = {"open": N_("open"), "working": N_("working"), "done": N_("done")}
ROLES = {"lead": N_("leads"), "member": N_("member")}
STATE_COLORS = {"running": "ok", "paused": "warn", "stopped": "muted", "gone": "bad"}
LIST_SECONDS, SHOW_SECONDS, POST_SECONDS, STOP_SECONDS = 60, 60, 60, 300
TASK_LIMIT = 20000


def valid_team(name):
    return name if isinstance(name, str) and TEAM_NAME.fullmatch(name) else None


def member_name(team, agent):
    return f"{team}-{agent}"


class StartDialog(QDialog):
    """A new team: its name, its agents (the lead first), the shared workspace and the launcher options of every
    member. REMOTE: the workspace is a path on that host."""

    def __init__(self, parent, host_label, remote, folder=""):
        super().__init__(parent)
        self.setWindowTitle(tr("Start a team on {host}", host=host_label))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("One sandbox per agent on one workspace; the lead talks to you and hands the "
                                        "others tasks on the team's board."), role="secondary"))
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(NAME_EXAMPLE)
        self.lead = QComboBox()
        for agent in AGENTS:
            self.lead.addItem(agent, agent)
        self.members = {}
        members = QWidget()
        row = QHBoxLayout(members)
        row.setContentsMargins(0, 0, 0, 0)
        for agent in AGENTS:
            box = QCheckBox(agent)
            box.setChecked(agent == "codex")
            self.members[agent] = box
            row.addWidget(box)
        row.addStretch()
        self.workspace = QLineEdit(folder)
        self.workspace.setPlaceholderText(tr("the shared workspace (an absolute path on that host)"))
        browse = QPushButton(tr("Folder…"))
        browse.setVisible(not remote)
        browse.clicked.connect(lambda _=False: self.choose())
        where = QWidget()
        where_row = QHBoxLayout(where)
        where_row.setContentsMargins(0, 0, 0, 0)
        where_row.addWidget(self.workspace, 1)
        where_row.addWidget(browse)
        self.options = QLineEdit()
        self.options.setPlaceholderText(OPTIONS_EXAMPLE)
        self.options.setToolTip(tr("Launcher options for every member (as on the command line)"))
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Lead"), self.lead)
        form.addRow(tr("Members"), members)
        form.addRow(tr("Workspace"), where)
        form.addRow(tr("Options"), self.options)
        layout.addLayout(form)
        self.problem = plain_label(role="notice-error")
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox()
        buttons.addButton(tr("Start"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.check_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.lead.currentIndexChanged.connect(lambda _=0: self.lead_changed())
        self.lead_changed()

    def lead_changed(self):
        for agent, box in self.members.items():
            box.setEnabled(agent != self.lead.currentData())
            if agent == self.lead.currentData():
                box.setChecked(False)

    def choose(self):
        from .state import choose_folder
        path = choose_folder(self, tr("Workspace"), "team")
        if path:
            self.workspace.setText(path)

    def request(self):
        """(name, [agents, the lead first], workspace, [options]); ValueError says what is wrong."""
        name = self.name.text().strip()
        if valid_team(name) is None:
            raise ValueError(tr("A team's name: 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit."))
        agents = [self.lead.currentData()] + [a for a, box in self.members.items()
                                              if box.isChecked() and a != self.lead.currentData()]
        if len(agents) < 2:
            raise ValueError(tr("A team has two or three agents: choose at least one member besides the lead."))
        workspace = self.workspace.text().strip()
        if not workspace.startswith("/") or not workspace.isprintable():
            raise ValueError(tr("The workspace is an absolute path."))
        try:
            options = shlex.split(self.options.text())
        except ValueError as exc:
            raise ValueError(tr("The options cannot be read: {error}", error=printable(exc))) from None
        return name, agents, workspace, options

    def check_and_accept(self):
        try:
            self.request()
        except ValueError as exc:
            self.problem.setText(printable(exc))
            return
        self.accept()


class PostDialog(QDialog):
    """A task for one member of a team, from you."""

    def __init__(self, parent, team, members):
        super().__init__(parent)
        self.setWindowTitle(tr("A task for team {name}", name=team))
        form = QFormLayout(self)
        self.agent = QComboBox()
        for agent in members:
            self.agent.addItem(agent, agent)
        self.task = QPlainTextEdit()
        self.task.setPlaceholderText(tr("What the member should do; it reports back on the board."))
        form.addRow(tr("For"), self.agent)
        form.addRow(tr("Task"), self.task)
        buttons = QDialogButtonBox()
        buttons.addButton(tr("Post"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class TeamPage(QWidget):
    """The teams of `sandbox-ai team` on this machine or a host: members, board, start, post, stop, delete."""

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.teams, self.shown, self.token, self.show_token, self.loaded_once = [], None, 0, 0, False
        self.teams_host = None  # whose teams the rows are
        self.project = None
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("Teams of agents (sandbox-ai team): one sandbox per agent on a shared "
                                        "workspace, started and stopped together; the lead hands the others tasks "
                                        "on the team's board, an MCP server of its own without network."),
                                     role="secondary"))
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The teams of this machine, or of a host (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        layout.addWidget(self.hosts)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QGroupBox(tr("Teams"))
        left_layout = QVBoxLayout(left)
        self.table = table([tr("Team"), tr("Members"), tr("Workspace")], stretch=2, compact=(1,))
        self.table.setAccessibleName(tr("Teams"))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        left_layout.addWidget(self.table, 1)
        right = QGroupBox(tr("Members and board"))
        right_layout = QVBoxLayout(right)
        self.members = table([tr("Member"), tr("Role"), tr("State")], stretch=0, compact=(1, 2))
        self.members.setAccessibleName(tr("Members"))
        self.members.setMaximumHeight(140)
        self.board = table([tr("Task"), tr("From"), tr("For"), tr("State"), tr("What")], stretch=4,
                           compact=(1, 2, 3))
        self.board.setAccessibleName(tr("Board"))
        self.where = plain_label(role="secondary")
        right_layout.addWidget(self.where)
        right_layout.addWidget(self.members)
        right_layout.addWidget(self.board, 1)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([420, 620])
        layout.addWidget(split, 1)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.start_button = QPushButton(tr("Start a team…"))
        self.start_button.setProperty("role", "primary")
        self.attach_button = QPushButton(tr("Talk to the lead"))
        self.post_button = QPushButton(tr("Post a task…"))
        self.stop_button = QPushButton(tr("Stop…"))
        self.delete_button = QPushButton(tr("Delete…"))
        self.delete_button.setProperty("role", "danger")
        for button, tip in ((self.refresh_button, tr("Ask the chosen host again")),
                            (self.start_button, tr("sandbox-ai team start, in a terminal: every member starts there")),
                            (self.attach_button, tr("sandbox-ai attach NAME-LEAD, in a terminal")),
                            (self.post_button, tr("sandbox-ai team post: a task for a member, from you")),
                            (self.stop_button, tr("sandbox-ai team stop: every member and the board end")),
                            (self.delete_button, tr("sandbox-ai team delete: stopped, then its members' HOMEs and "
                                                    "the board removed"))):
            button.setToolTip(safe_tooltip(tip))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.start_button.clicked.connect(lambda _=False: self.start())
        self.attach_button.clicked.connect(lambda _=False: self.attach())
        self.post_button.clicked.connect(lambda _=False: self.post())
        self.stop_button.clicked.connect(lambda _=False: self.stop())
        self.delete_button.clicked.connect(lambda _=False: self.delete())
        layout.addWidget(flow(self.start_button, self.attach_button, self.post_button, self.stop_button,
                              self.delete_button, self.refresh_button))  # wraps when the window is narrow
        self.status = plain_label(role="secondary")
        self.status.setAccessibleName(tr("Status"))
        layout.addWidget(self.status)
        self.update_buttons()

    def host(self):
        return self.hosts.host() or LOCAL

    def set_project(self, path):
        """The window's current project: the workspace a new team on this machine is offered."""
        self.project = path

    def showEvent(self, event):  # noqa: N802 - Qt's name
        """Asked when shown, not when the window is built: no command (and no SSH) for a page nobody opened."""
        super().showEvent(event)
        if not event.spontaneous():
            self.hosts.load()
            self.refresh()

    # -- reading --

    def host_changed(self):
        """Another host: the teams of the one before go at once (Talk to the lead, Stop and Delete never name a team
        of one host on another), and its answers still on their way are dropped."""
        self.token += 1
        self.show_token += 1
        self.teams, self.teams_host = [], None
        fill(self.table, [], keys=[])
        self.clear_details()
        self.update_buttons()
        if self.loaded_once:
            self.refresh()

    def acting_host(self):
        """The host chosen, when the teams shown are its own; None (and said) when they are another's."""
        host = self.host()
        if self.teams_host not in (None, host):
            self.status.setText(tr("The teams shown are {old}'s, not {host}'s: Refresh first.",
                                   old=self.hosts.label(self.teams_host), host=self.hosts.label(host)))
            return None
        return host

    def refresh(self):
        self.loaded_once = True
        host = self.host()
        self.token += 1
        token = self.token
        self.status.setText(tr("Asking {host}…", host=self.hosts.label(host)))
        background(lambda: self.sai.teams(host), owner=self, source=self.refresh_button, timeout=LIST_SECONDS + 15,
                   done=lambda result, error: self.listed(host, token, result, error))

    def listed(self, host, token, result, error):
        if token != self.token or host != self.host():
            return
        if error is not None:
            self.teams = []
            fill(self.table, [], keys=[])
            self.clear_details()
            self.status.setText(tr("Could not list the teams on {host}: {error}", host=self.hosts.label(host),
                                   error=printable(error)[:500]))
            self.update_buttons()
            return
        self.teams, self.teams_host = [t for t in result if valid_team(t.get("name"))], host
        before = self.chosen()
        fill(self.table, [[t["name"], tr("damaged: its record cannot be read") if t.get("damaged") else
                           ", ".join(t["members"]), t["workspace"]] for t in self.teams],
             keys=[t["name"] for t in self.teams], colors=["bad" if t.get("damaged") else None for t in self.teams])
        if before:
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0) and self.table.item(row, 0).text() == before:
                    self.table.selectRow(row)
        self.status.setText(tr("Teams on {host}: {count}", host=self.hosts.label(host), count=len(self.teams))
                            if self.teams else tr("No team on {host} yet.", host=self.hosts.label(host)))
        if not self.chosen():
            self.clear_details()
        self.update_buttons()

    def chosen(self):
        keys = selected_keys(self.table)
        return valid_team(keys[0]) if keys else None

    def selection_changed(self):
        self.update_buttons()
        name = self.chosen()
        if name is None:
            self.clear_details()
            return
        self.show_team(name)

    def clear_details(self):
        self.shown = None
        fill(self.members, [])
        fill(self.board, [])
        self.where.setText(tr("Select a team to see its members and its board."))

    def damaged(self, name):
        return any(t.get("name") == name and t.get("damaged") for t in self.teams)

    def show_team(self, name):
        host = self.host()
        if self.damaged(name):  # nothing to show: only Delete… (team delete --yes finds its members by name)
            self.show_token += 1
            self.clear_details()
            self.where.setText(tr("The record of team {name} is damaged: Delete… removes it with every member of its "
                                  "name ({members}).", name=name, members=", ".join(member_name(name, a)
                                                                                   for a in AGENTS)))
            return
        self.show_token += 1
        token = self.show_token
        background(lambda: self.sai.team_show(host, name), owner=self, timeout=SHOW_SECONDS + 15,
                   done=lambda result, error: self.team_shown(host, name, token, result, error))

    def team_shown(self, host, name, token, result, error):
        if token != self.show_token or host != self.host() or name != self.chosen():
            return
        if error is not None:
            self.clear_details()
            self.where.setText(tr("Could not read team {name}: {error}", name=name, error=printable(error)[:400]))
            return
        self.shown = result
        members = [m for m in result.get("members") or [] if m in AGENTS]
        states = result.get("states") if isinstance(result.get("states"), dict) else {}
        lead = result.get("lead")
        fill(self.members, [[member_name(name, agent), tr(ROLES["lead" if agent == lead else "member"]),
                             printable(states.get(agent) or "?")] for agent in members],
             [STATE_COLORS.get(states.get(agent)) for agent in members])
        board = result.get("board") if isinstance(result.get("board"), dict) else {}
        tasks = [t for t in board.get("tasks") or [] if isinstance(t, dict)]
        fill(self.board, [[f"#{printable(t.get('id'))}", printable(t.get("from") or "?")[:64],
                           printable(t.get("to") or "?")[:20], tr(STATES.get(t.get("state"), "open"))
                           if t.get("state") in STATES else printable(t.get("state") or "?"),
                           printable(" ".join(str(t.get("task") or "").split()))[:300]] for t in tasks[-200:]],
             ["ok" if t.get("state") == "done" else "warn" if t.get("state") == "working" else None
              for t in tasks[-200:]])
        text = tr("{workspace} · lead {lead}", workspace=printable(result.get("workspace") or ""),
                  lead=printable(lead or "?"))
        if board.get("error"):
            text += " · " + tr("board: {error}", error=printable(board["error"])[:300])
        self.where.setText(text)
        self.update_buttons()

    def update_buttons(self):
        name = self.chosen()
        why = tr("Select a team first.")
        for button in (self.attach_button, self.post_button, self.stop_button, self.delete_button):
            set_enabled(button, name is not None, why)
        if name is not None and self.damaged(name):
            for button in (self.attach_button, self.post_button, self.stop_button):
                set_enabled(button, False, tr("Its record is damaged: only Delete… is left."))
            return
        if name is not None and self.shown is not None and self.shown.get("name") == name:
            states = self.shown.get("states") if isinstance(self.shown.get("states"), dict) else {}
            lead = self.shown.get("lead")
            set_enabled(self.attach_button, states.get(lead) in ("running", "paused"),
                        tr("The lead does not run: start the team again, or resume its session."))

    # -- actions --

    def start(self):
        host = self.host()
        folder = self.project if host == LOCAL and self.project else ""
        dialog = StartDialog(self, self.hosts.label(host), host != LOCAL, folder)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            name, agents, workspace, options = dialog.request()
            argv = self.sai.team_start_argv(host, name, agents, workspace, options)
        except ValueError as exc:
            warn(self, tr("Start a team"), printable(exc))
            return
        title = f"sandbox-ai team start {name}" + ("" if host == LOCAL else f" ({host})")
        self.terminal(argv, title)
        self.status.setText(tr("Team {name} starts in the terminal; Refresh once it says so.", name=name))

    def attach(self):
        name, host = self.chosen(), self.acting_host()
        if host is None:
            return
        lead = (self.shown or {}).get("lead") if (self.shown or {}).get("name") == name else None
        if name is None or lead not in AGENTS:
            return
        session = member_name(name, lead)
        argv = self.sai.command("attach", session) if host == LOCAL else self.sai.host_terminal(host, "attach", session)
        self.terminal(argv, f"sandbox-ai attach {session}" + ("" if host == LOCAL else f" ({host})"))

    def post(self):
        name, host = self.chosen(), self.acting_host()
        if name is None or host is None:
            return
        members = [m for m in (self.shown or {}).get("members") or [] if m in AGENTS] or \
            next((t["members"] for t in self.teams if t["name"] == name), list(AGENTS))
        dialog = PostDialog(self, name, members)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        agent, task = dialog.agent.currentData(), dialog.task.toPlainText().strip()
        if agent not in AGENTS or not task:
            return
        if len(task) > TASK_LIMIT or "\0" in task:
            warn(self, tr("Post a task"), tr("A task is at most {count} characters of text.", count=TASK_LIMIT))
            return
        background(lambda: self.sai.team_post(host, name, agent, task), owner=self, work=True, source=self.post_button,
                   timeout=POST_SECONDS + 15, done=lambda result, error: self.done(name, result, error))

    def stop(self):
        name, host = self.chosen(), self.acting_host()
        if name is None or host is None:
            return
        if not ask_first(self, "team-stop", tr("Stop…").rstrip("…"), tr(
                "Stop every member of team {name} on {host} and its board? What runs in them ends now.", name=name,
                host=self.hosts.label(host)), tr("Stop")):
            return
        self.status.setText(tr("Stopping {name}…", name=name))
        background(lambda: self.sai.team_stop(host, name), owner=self, source=[self.stop_button, self.delete_button],
                   work=True, timeout=STOP_SECONDS + 30, done=lambda result, error: self.done(name, result, error))

    def delete(self):
        name, host = self.chosen(), self.acting_host()
        if name is None or host is None:
            return
        question = tr("The record of team {name} on {host} is damaged. Delete it with every member of its name "
                      "({members})? They are stopped, then their HOMEs and the board are removed for good; the "
                      "workspace stays.", name=name, host=self.hosts.label(host),
                      members=", ".join(member_name(name, a) for a in AGENTS)) if self.damaged(name) else tr(
            "Delete team {name} on {host}? Its members are stopped, then their HOMEs and the board are removed "
            "for good; the workspace stays.", name=name, host=self.hosts.label(host))
        if not ask_first(self, "team-delete", tr("Delete…").rstrip("…"), question, tr("Delete")):
            return
        self.status.setText(tr("Deleting {name}…", name=name))
        background(lambda: self.sai.team_delete(host, name), owner=self, work=True, source=[self.stop_button,
                                                                                 self.delete_button],
                   timeout=STOP_SECONDS + 30, done=lambda result, error: self.done(name, result, error))

    def done(self, name, result, error):
        status, out, err = outcome(result, error)
        said = "\n".join(printable(line) for line in (out + err).strip()[-3000:].splitlines())
        if status != 0:
            warn(self, tr("Team {name}", name=name), said or tr("Failed (exit status {status}).", status=status),
                 modal=False)
        self.status.setText(said.splitlines()[-1] if said else "")
        self.refresh()
        if self.chosen() == name:
            self.show_team(name)
