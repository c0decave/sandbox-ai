"""The Teams page (sandbox-ai team): teams of agents, one sandbox per agent on a shared workspace, started and stopped
together, with a task board between them - on this machine or on a host of `sandbox-ai hosts` (over SSH).

  - the list is `sandbox-ai team list`, a team's members and its board `sandbox-ai team show --json NAME`;
  - Start runs `sandbox-ai team start --agents A,B[,...] --workspace DIR [--vpn PROFILE] [--member-vpn AGENT=PROFILE]
    [--allow-shared-openvpn] NAME [-- OPTIONS]` in a terminal tab on that host (each member starts there, one after
    the other, and says so). The dialog offers that host's VPN profiles (with their type) for every member and, under
    "Per member (advanced)", another one for a member; one profile in several members is checked as team.py checks
    it (vpn_sharing: Tor said, WireGuard/Mullvad and a persistent Tailscale node refused with the reason, OpenVPN only
    with its box ticked);
  - Post a task is `sandbox-ai team post NAME AGENT TASK` (a task for a member, from you);
  - Talk to the lead opens `sandbox-ai attach NAME-LEAD` in a terminal tab;
  - Stop (`team stop`) and Delete (`team delete --yes`: with the members' HOMEs and the board) are asked first
    (start-stop, start-delete).

What a team's board holds comes from its members: shown through printable(), never as markup. Names that go back as
arguments are checked first (a team's name, an agent of AGENTS), and passed after "--"."""
from __future__ import annotations

import re
import shlex

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from .backend import TEAM_AGENTS as AGENTS  # sandbox-ai's team.AGENTS, not every agent of the app (backend.AGENTS)
from .files import printable, printable_text
from .i18n import N_, tr
from . import widgets
from .widgets import (
    ask_first, background, explanation, fill, flow, multi_select, outcome, plain_label, report_each, run_each,
    safe_tooltip, selected_keys, set_enabled, several, table, warn,
)

LOCAL = "local"
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


def vpn_sharing(entry):
    """"shared", "once" or "ask" for a profile ENTRY of `vpn list --json` (profile, type, provider, ephemeral): whether
    it may run in several members at once - sandbox-ai's own rule (vpn.sharing): Tor and an ephemeral Tailscale node
    each their own, WireGuard (Mullvad's too) and a persistent Tailscale node once, OpenVPN only when you confirm."""
    kind = entry.get("type")
    if kind == "tor" or kind == "tailscale" and entry.get("ephemeral"):
        return "shared"
    return "ask" if kind == "openvpn" else "once"


def vpn_label(entry):
    """"NAME (TYPE)", Mullvad's as mullvad: what the picker and the checks name."""
    provider = entry.get("provider")
    kind = provider if provider not in (None, "", "generic", "?") else entry.get("type") or "?"
    return f"{printable(entry.get('profile'))} ({printable(kind)})"


def vpn_check(profiles, chosen, shared_openvpn):
    """([note], problem or None) of the members' VPNs CHOSEN ({agent: profile or None}) with PROFILES ({name: entry}):
    one profile in several members as vpn_sharing says (team.py checks the same again)."""
    groups = {}
    for agent, name in chosen.items():
        if name:
            groups.setdefault(name, []).append(agent)
    notes = []
    for name, members in groups.items():
        entry = profiles.get(name) or {"profile": name, "type": "?"}
        if len(members) < 2:
            continue
        values = {"profile": vpn_label(entry), "count": len(members), "members": ", ".join(members)}
        kind = vpn_sharing(entry)
        if entry.get("type") == "tor":
            notes.append(tr("{profile}: each member gets a Tor of its own (its own circuits and its own exit).",
                            **values))
        elif kind == "shared":
            notes.append(tr("{profile}: each member gets a new ephemeral Tailscale node.", **values))
        elif entry.get("type") == "tailscale":
            return notes, tr("{profile} would run in {count} members at once ({members}): a Tailscale node runs once "
                             "at a time. Use an ephemeral profile (vpn add … --ephemeral) or one per member.", **values)
        elif kind == "once":
            return notes, tr("{profile} would run in {count} members at once ({members}): a WireGuard key works in one "
                             "sandbox at a time - the same key twice gets the same tunnel address, and the server sends "
                             "to whichever tunnel spoke last, so both break. Give each member a profile of its own "
                             "(Mullvad allows several devices per account, each with its own key) or use Tor.",
                             **values)
        elif not shared_openvpn:
            return notes, tr("{profile} would run in {count} members at once ({members}): many OpenVPN servers take "
                             "one account or certificate once, and a second connection ends the first. Tick the box "
                             "below if your server allows it (duplicate-cn), or give each member a profile of its own.",
                             **values)
        else:
            notes.append(tr("{profile} runs in {count} members at once ({members}), as you confirmed: if a tunnel "
                            "drops, give each member a profile of its own.", **values))
    return notes, None


class StartDialog(QDialog):
    """A new team: its name, its agents (the lead first), the shared workspace, the VPN profile in front of every
    member (PROFILES: `vpn list --json` of that host; one per member under "Per member (advanced)") and the launcher
    options of every member. REMOTE: the workspace is a path on that host (HOST: Folder… lists its folders)."""

    def __init__(self, parent, host_label, remote, folder="", profiles=(), profiles_error=None, host=None, modes=(),
                 draft=None):
        super().__init__(parent)
        self.host = (host or "local") if remote else "local"
        self.profiles = {e["profile"]: e for e in profiles if isinstance(e, dict) and isinstance(e.get("profile"), str)
                         and e["profile"].isprintable()}
        self.setWindowTitle(tr("Start a team on {host}", host=host_label))
        layout = QVBoxLayout(self)
        layout.addWidget(explanation(tr("One sandbox per agent on one workspace; the lead talks to you and hands the "
                                        "others tasks on the team's board.")))
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(NAME_EXAMPLE)
        self.lead = QComboBox()
        for agent in AGENTS:
            self.lead.addItem(agent, agent)
        self.members = {}
        for agent in AGENTS:
            box = QCheckBox(agent)
            box.setChecked(agent == "codex")
            box.toggled.connect(lambda _on=False: self.members_changed())
            self.members[agent] = box
        members = flow(*self.members.values())  # seven agents: the row wraps in a narrow dialog
        self.workspace = QLineEdit(folder)
        self.workspace.setPlaceholderText(tr("the shared workspace (an absolute path on that host)"))
        browse = QPushButton(tr("Folder…"))
        browse.clicked.connect(lambda _=False: self.choose())
        where = QWidget()
        where_row = QHBoxLayout(where)
        where_row.setContentsMargins(0, 0, 0, 0)
        where_row.addWidget(self.workspace, 1)
        where_row.addWidget(browse)
        browse.setVisible(not remote or self.host != "local")  # (in its row first: else a window of its own)
        self.vpn = self.vpn_box(tr("(none)"))
        self.vpn.setAccessibleName(tr("VPN profile"))
        self.vpn.setToolTip(safe_tooltip(tr("The VPN in front of every member: each member is a sandbox of its own "
                                            "with its own VPN container (sandbox-ai team start --vpn). One profile in several "
                                            "members: Tor is fine, WireGuard and Mullvad need one profile per "
                                            "member, OpenVPN only when you confirm")))
        self.options = QLineEdit()
        self.options.setPlaceholderText(OPTIONS_EXAMPLE)
        self.options.setToolTip(tr("Launcher options for every member (as on the command line)"))
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Lead"), self.lead)
        form.addRow(tr("Members"), members)
        form.addRow(tr("Workspace"), where)
        form.addRow(tr("VPN profile"), self.vpn)
        form.addRow(tr("Options"), self.options)
        layout.addLayout(form)
        # Per member (advanced): another profile for one member (sandbox-ai team start --member-vpn AGENT=PROFILE).
        self.per_member = QGroupBox(tr("Per member (advanced)"))
        self.per_member.setCheckable(True)
        self.per_member.setChecked(False)
        self.per_member.setToolTip(safe_tooltip(tr("Another VPN profile for a member (sandbox-ai team start "
                                                   "--member-vpn AGENT=PROFILE)")))
        per_form = QFormLayout(self.per_member)
        self.member_vpn, self.member_own = {}, {}
        for agent in AGENTS:
            box = self.vpn_box(tr("(the team's)"))
            box.setAccessibleName(tr("VPN profile of {agent}", agent=agent))
            label = plain_label(agent, word_wrap=False)
            per_form.addRow(label, box)
            self.member_vpn[agent] = (label, box)
            # ... its role (added to its first prompt: team start --role) and its own model, LLM server and mode
            # (--member-model, --member-llm, --member-mode); team start checks their values
            role, model, llm = QLineEdit(), QLineEdit(), QLineEdit()
            for edit, what, placeholder in ((role, tr("Role"), tr("e.g. reviews every change; writes the tests first")),
                                            (model, tr("Model"), tr("Model (the team's)")),
                                            (llm, tr("LLM server"), tr("LLM server (the team's)"))):
                edit.setPlaceholderText(placeholder)
                edit.setAccessibleName(tr("{what} of {agent}", what=what, agent=agent))
            mode = QComboBox()
            mode.setAccessibleName(tr("{what} of {agent}", what=tr("Mode"), agent=agent))
            mode.addItem(tr("Mode (the team's)"), "")
            for choice in modes:
                mode.addItem(str(choice), str(choice))
            own = flow(role, model, llm, mode)
            per_form.addRow("", own)
            self.member_own[agent] = (own, role, model, llm, mode)
        layout.addWidget(self.per_member)
        self.shared_openvpn = QCheckBox(tr("Allow one OpenVPN profile in several members (your server must allow it: "
                                           "duplicate-cn)"))
        layout.addWidget(self.shared_openvpn)
        self.vpn_notes = plain_label(role="secondary", selectable=True)
        layout.addWidget(self.vpn_notes)
        # (said above the checks' notes for as long as the dialog is open)
        self.listing_note = tr("Could not list the VPN profiles: {error}",
                               error=printable_text(profiles_error)[:300]) if profiles_error else ""
        self.problem = plain_label(role="notice-error", selectable=True)
        layout.addWidget(self.problem)
        self.vpn.currentIndexChanged.connect(lambda _=0: self.vpn_changed())
        self.per_member.toggled.connect(lambda _on=False: self.members_changed())
        self.shared_openvpn.toggled.connect(lambda _on=False: self.vpn_changed())
        for _label, box in self.member_vpn.values():
            box.currentIndexChanged.connect(lambda _=0: self.vpn_changed())
        buttons = QDialogButtonBox()
        buttons.addButton(tr("Start"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.check_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.lead.currentIndexChanged.connect(lambda _=0: self.lead_changed())
        self.lead_changed()
        # What was typed and not started (Cancel, a refusal): filled in again on this host (drafts.py) - the lead
        # before the members (it greys its own box), "Per member" before its rows (greyed while it is off); sharing
        # one OpenVPN profile is confirmed anew each time.
        from . import drafts
        fields = {"name": self.name, "lead": self.lead,
                  **{f"member:{agent}": box for agent, box in self.members.items()},
                  "workspace": self.workspace, "vpn": self.vpn, "options": self.options, "per_member": self.per_member}
        for agent in AGENTS:
            _own, role, model, llm, mode = self.member_own[agent]
            fields.update({f"vpn:{agent}": self.member_vpn[agent][1], f"role:{agent}": role,
                           f"model:{agent}": model, f"llm:{agent}": llm, f"mode:{agent}": mode})
        self.draft = None if draft is None else drafts.Draft(self, draft, fields)

    def vpn_box(self, empty):
        """A picker of the host's VPN profiles, "NAME (TYPE)" each; EMPTY first (no profile, or the team's)."""
        box = QComboBox()
        box.addItem(empty, None)
        for name, entry in sorted(self.profiles.items()):
            box.addItem(vpn_label(entry), name)
        return box

    def lead_changed(self):
        for agent, box in self.members.items():
            box.setEnabled(agent != self.lead.currentData())
            if agent == self.lead.currentData():
                box.setChecked(False)
        self.members_changed()

    def agents(self):
        """The team's agents, the lead first."""
        return [self.lead.currentData()] + [a for a, box in self.members.items()
                                            if box.isChecked() and a != self.lead.currentData()]

    def members_changed(self):
        """Per member: a row for each agent of the team only."""
        agents = self.agents()
        for agent, (label, box) in self.member_vpn.items():
            label.setVisible(agent in agents)
            box.setVisible(agent in agents)
            self.member_own[agent][0].setVisible(agent in agents)
        self.vpn_changed()

    def member_settings(self):
        """({agent: role}, {agent: {--model/--llm/--mode: value}}) of the team's members, as typed under "Per member
        (advanced)" (none while it is not ticked); team start checks them."""
        roles, own = {}, {}
        if not self.per_member.isChecked():
            return roles, own
        for agent in self.agents():
            _row, role, model, llm, mode = self.member_own[agent]
            if role.text().strip():
                roles[agent] = role.text().strip()
            for option, value in (("--model", model.text().strip()), ("--llm", llm.text().strip()),
                                  ("--mode", mode.currentData() or "")):
                if value:
                    own.setdefault(agent, {})[option] = value
        return roles, own

    def chosen_vpns(self):
        """({agent: profile or None} of every member, {agent: profile} of the overrides)."""
        team = self.vpn.currentData()
        own = {agent: self.member_vpn[agent][1].currentData() for agent in self.agents()
               if self.per_member.isChecked() and self.member_vpn[agent][1].currentData()}
        return {agent: own.get(agent) or team for agent in self.agents()}, own

    def vpn_changed(self):
        chosen, _own = self.chosen_vpns()
        notes, problem = vpn_check(self.profiles, chosen, self.shared_openvpn.isChecked())
        openvpn = [n for n in set(chosen.values()) if n and (self.profiles.get(n) or {}).get("type") == "openvpn"
                   and list(chosen.values()).count(n) > 1]
        self.shared_openvpn.setVisible(bool(openvpn))
        self.vpn_notes.setText("\n".join([self.listing_note, *notes] if self.listing_note else notes))
        self.problem.setText(problem or "")

    def choose(self):
        from . import remote_chooser as RC
        path = RC.choose_folder(self, self.host, tr("Workspace"), "team", start=self.workspace.text().strip())
        if path:
            self.workspace.setText(path)

    def request(self):
        """(name, [agents, the lead first], workspace, [options], vpn, {agent: vpn}, shared_openvpn); ValueError says
        what is wrong."""
        name = self.name.text().strip()
        if valid_team(name) is None:
            raise ValueError(tr("A team's name: 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit."))
        agents = self.agents()
        if len(agents) < 2:
            raise ValueError(tr("A team has two or more agents: choose at least one member besides the lead."))
        workspace = self.workspace.text().strip()
        if not workspace.startswith("/") or not workspace.isprintable():
            raise ValueError(tr("The workspace is an absolute path."))
        try:
            options = shlex.split(self.options.text())
        except ValueError as exc:
            raise ValueError(tr("The options cannot be read: {error}", error=printable_text(exc))) from None
        chosen, own = self.chosen_vpns()
        shared = self.shared_openvpn.isChecked() and self.shared_openvpn.isVisibleTo(self)
        _notes, problem = vpn_check(self.profiles, chosen, shared)
        if problem:
            raise ValueError(problem)
        return name, agents, workspace, options, self.vpn.currentData(), own, shared

    def check_and_accept(self):
        try:
            self.request()
        except ValueError as exc:
            self.problem.setText(printable_text(exc))
            return
        self.accept()


class PostDialog(QDialog):
    """A task for one member of a team, from you."""

    def __init__(self, parent, team, members, draft=None):
        """DRAFT: the key of a task typed and not posted (drafts.py): filled in again."""
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
        from . import drafts
        self.draft = None if draft is None else drafts.Draft(self, draft, {"agent": self.agent, "task": self.task})


class TeamPage(QWidget):
    """The teams of `sandbox-ai team` on this machine or a host: members, board, start, post, stop, delete."""

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.teams, self.shown, self.token, self.show_token, self.loaded_once = [], None, 0, 0, False
        self.teams_host = None  # whose teams the rows are
        self.project = None
        layout = QVBoxLayout(self)
        layout.addWidget(explanation(tr("Teams of agents (sandbox-ai team): one sandbox per agent on a shared "
                                        "workspace, started and stopped together; the lead hands the others tasks "
                                        "on the team's board, an MCP server of its own without network.")))
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The teams of this machine, or of a host (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        layout.addWidget(self.hosts)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QGroupBox(tr("Teams"))
        left_layout = QVBoxLayout(left)
        self.table = table([tr("Team"), tr("Members"), tr("Workspace")], stretch=2, compact=(1,))
        self.table.setAccessibleName(tr("Teams"))
        multi_select(self.table)  # several at once (Ctrl, Shift): Stop… and Delete… take them all
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
        from .widgets import list_menu
        list_menu(self.table, lambda: [self.attach_button, self.post_button, None, self.stop_button,
                                       self.delete_button, None, self.start_button, self.refresh_button])
        self.status = plain_label(role="secondary", selectable=True, log="team")
        self.status.setAccessibleName(tr("Status"))
        layout.addWidget(self.status)
        self.update_buttons()

    def host(self):
        return self.hosts.host() or LOCAL

    def show_named(self, host, name):
        """Team NAME of HOST selected (the Board's Team part, Open on the Teams page): that host chosen, its teams
        read, then NAME selected once they are there."""
        if valid_team(name) is None:
            return
        self.wanted = name
        self.hosts.set_host(host)
        if self.hosts.host() != host:
            self.status.setText(tr("{host} is not a host of sandbox-ai hosts here.", host=printable(host)))
            return
        self.refresh()

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
        of one host on another), and its answers still on their way are dropped. Hidden (the Sandboxes page's host,
        given as it is about to be shown): read by its show, once."""
        self.token += 1
        self.show_token += 1
        self.teams, self.teams_host = [], None
        fill(self.table, [], keys=[])
        self.clear_details()
        self.update_buttons()
        if self.loaded_once and self.isVisible():
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
                                   error=printable_text(error)[:500]))
            self.update_buttons()
            return
        self.teams, self.teams_host = [t for t in result if valid_team(t.get("name"))], host
        before, self.wanted = getattr(self, "wanted", None) or self.chosen(), None  # (one the Board asked for first)
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

    def chosen_teams(self):
        """The teams selected (valid names), top to bottom."""
        return [name for name in (valid_team(key) for key in selected_keys(self.table)) if name]

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
            self.where.setText(tr("Could not read team {name}: {error}", name=name, error=printable_text(error)[:400]))
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
        teams = self.chosen_teams()
        if len(teams) > 1:  # several: Stop… and Delete… take them all; Attach and Post one team
            for button in (self.attach_button, self.post_button):
                set_enabled(button, False, tr("Acts on one row: {count} are selected.", count=len(teams)))
            set_enabled(self.stop_button, any(not self.damaged(t) for t in teams),
                        tr("Their records are damaged: only Delete… is left."))
            return
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
        """The host's VPN profiles first (vpn list --json there), then the dialog."""
        host = self.host()
        self.status.setText(tr("Asking {host}…", host=self.hosts.label(host)))
        background(lambda: self.sai.host_vpn_profiles(host), owner=self, source=self.start_button, timeout=90,
                   done=lambda result, error: self.start_with(host, result or [], error))

    def start_with(self, host, profiles, error):
        if host != self.host():
            return
        self.status.setText("")
        folder = self.project if host == LOCAL and self.project else ""
        from . import board as B
        modes = getattr(B._options(self.sai).get("--mode"), "choices", None) or ()
        key = ("team-start", host, folder)  # what was typed and not started: filled in again (drafts.py)
        dialog = StartDialog(self, self.hosts.label(host), host != LOCAL, folder, profiles, error, host=host,
                             modes=modes, draft=key)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        try:
            name, agents, workspace, options, vpn, member_vpn, shared = dialog.request()
            roles, member_options = dialog.member_settings()
            argv = self.sai.team_start_argv(host, name, agents, workspace, options, vpn=vpn, member_vpn=member_vpn,
                                            shared_openvpn=shared, roles=roles, member_options=member_options)
        except ValueError as exc:
            warn(self, tr("Start a team"), printable_text(exc))
            return
        title = f"sandbox-ai team start {name}" + ("" if host == LOCAL else f" ({host})")
        from . import drafts
        drafts.drop(key)  # handed to the terminal, which says how it went: what was typed is done with
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
        key = ("team-post", host, name)  # a task typed and not posted: filled in again (drafts.py)
        dialog = PostDialog(self, name, members, draft=key)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        agent, task = dialog.agent.currentData(), dialog.task.toPlainText().strip()
        if agent not in AGENTS or not task:
            return
        if len(task) > TASK_LIMIT or "\0" in task:
            warn(self, tr("Post a task"), tr("A task is at most {count} characters of text.", count=TASK_LIMIT))
            return
        background(lambda: self.sai.team_post(host, name, agent, task), owner=self, work=True, source=self.post_button,
                   timeout=POST_SECONDS + 15, done=lambda result, error: self.done(name, result, error, draft=key))

    def several_teams(self, action):
        """Stop… or Delete… (ACTION "stop" or "delete") with several teams selected: one question naming them (a
        damaged record is only deleted), then each in turn; what failed is said together."""
        host = self.acting_host()
        teams = [t for t in self.chosen_teams() if action == "delete" or not self.damaged(t)]
        if host is None or not teams:
            return
        label = self.hosts.label(host)
        if action == "stop":
            question, title = tr("Stop every member of these {count} teams on {host} and their boards? What runs in "
                                 "them ends now.", count=len(teams), host=label), tr("Stop…").rstrip("…")
        else:
            question, title = tr("Delete these {count} teams on {host}? Their members are stopped, then their HOMEs "
                                 "and the boards are removed for good; the workspaces stay.", count=len(teams),
                                 host=label), tr("Delete…").rstrip("…")
        accept = tr("Stop") if action == "stop" else tr("Delete")
        text = several(question, teams)
        if not (ask_first(self, "team-stop", title, text, accept) if action == "stop" else
                ask_first(self, "team-delete", title, text, accept)):
            return
        self.status.setText(tr("Working…"))
        call = self.sai.team_stop if action == "stop" else self.sai.team_delete

        def done(outcomes):
            failures = report_each(self, title, outcomes)
            self.status.setText(tr("{done} of {total} done.", done=len(teams) - len(failures), total=len(teams)))
            self.refresh()
        run_each(lambda name: call(host, name), teams, owner=self, source=[self.stop_button, self.delete_button],
                 timeout=(STOP_SECONDS + 30) * len(teams), done=done)

    def stop(self):
        if len(self.chosen_teams()) > 1:
            self.several_teams("stop")
            return
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
        if len(self.chosen_teams()) > 1:
            self.several_teams("delete")
            return
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

    def done(self, name, result, error, draft=None):
        status, out, err = outcome(result, error)
        if draft is not None and status == 0:
            from . import drafts
            drafts.drop(draft)  # posted: the task typed is done with
        said = "\n".join(printable(line) for line in (out + err).strip()[-3000:].splitlines())
        if status != 0:
            warn(self, tr("Team {name}", name=name), said or tr("Failed (exit status {status}).", status=status),
                 modal=False)
        self.status.setText(said.splitlines()[-1] if said else "")
        self.refresh()
        if self.chosen() == name:
            self.show_team(name)
