"""The wizard: a new (or existing) project in a few pages, building the same board model as the Board page.

Project folder -> agent -> preset (the Board page's library) or custom -> network -> connection proxy and TLS ->
Git and tokens -> MCP servers -> devices and limits -> review (command line, security badges, problems, the file and what
changes, the launcher's verdict) -> write and trust. Finishing opens the board.

The safety rules of sandbox-ai's own wizard hold: a token typed here goes to a private file (mode 0600)
under ~/.config/sandbox-ai/tokens, never into the project file, which only names that file; the project's
origin remote is never offered as a default (its .git/config is the agent's to write); an existing project
file is shown, never applied, until it is written and trusted again.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QWizard, QWizardPage,
)

from . import board as B
from . import presets as PR
from . import widgets
from .board_view import LinesEdit, PathEdit, RulesEdit, read_small
from .preset_gallery import PlaceholderForm
from .files import printable
from .i18n import N_, tr
from .widgets import MONO, background, confirm, plain_label, warn

DEFAULT_MEMORY = "8g"  # the launcher's default, as a placeholder
TOKEN_KINDS = (("env", "--git-token-env"), ("secret", "--git-token-secret"), ("typed", "--git-token-file"),
               ("file", "--git-token-file"))


class Page(QWizardPage):
    def __init__(self, wizard, title, subtitle=""):
        super().__init__()
        self.wizard_ = wizard
        self.setTitle(title)
        if subtitle:
            self.setSubTitle(subtitle)
        self.form = QFormLayout(self)

    @property
    def board(self):
        return self.wizard_.board

    def node(self, kind):
        node = self.board.first(kind)
        return node if node is not None else self.board.add(kind)

    def forbid(self, widget, option, value=None):
        """Grey out WIDGET when the administrator's policy forbids OPTION (or OPTION=VALUE)."""
        forbidden = self.wizard_.forbidden.get(option, set())
        if None in forbidden or (value is not None and value in forbidden):
            widget.setEnabled(False)
            widget.setToolTip(tr("The administrator's policy forbids {option}", option=option +
                                 (f"={value}" if value is not None else "")))


class FolderPage(Page):
    def __init__(self, wizard):
        super().__init__(wizard, tr("Project folder"), tr("The folder the agent works in (/work in the sandbox); "
                                                          "the project file is written there."))
        self.path = PathEdit()
        self.form.addRow(tr("Folder"), self.path)
        self.note = plain_label(role="secondary")
        self.form.addRow(self.note)

    def validatePage(self):  # noqa: N802
        directory = self.path.edit.text().strip()
        if not directory or not os.path.isdir(os.path.expanduser(directory)):
            self.note.setText(tr("Choose a folder that exists."))
            return False
        self.wizard_.directory = os.path.realpath(os.path.expanduser(directory))
        self.wizard_.existing = None
        try:
            review = self.wizard_.sai.review_project(self.wizard_.directory)
        except FileNotFoundError:
            review = None
        except (OSError, ValueError) as exc:
            self.note.setText(tr("The project file there cannot be read: {error}", error=printable(exc)))
            return False
        if review is not None and review.document is not None:
            self.wizard_.existing = review
        return True


class AgentPage(Page):
    def __init__(self, wizard):
        super().__init__(wizard, tr("Agent"), tr("Which coding agent runs in the sandbox."))
        self.agent = QComboBox()
        self.agent.addItems(list(wizard.sai.agents))
        self.agent.setCurrentText(wizard.settings().get("default_agent", "claude"))
        self.danger = QCheckBox(tr("without the agent's own approval prompts (-D; the sandbox stays in force)"))
        self.forbid(self.danger, "--dangerously-skip-permissions")
        # The model's and the effort's choices are the agent's own (guide.AGENT_MODELS / AGENT_EFFORTS); a model
        # it does not list can be typed.
        self.model = QComboBox()
        self.model.setEditable(True)
        self.effort = QComboBox()
        self.form.addRow(tr("Agent"), self.agent)
        self.form.addRow("", self.danger)
        self.form.addRow(tr("Model"), self.model)
        self.form.addRow(tr("Effort"), self.effort)
        self.agent.currentTextChanged.connect(self.agent_changed)
        self.agent_changed(self.agent.currentText())

    def agent_changed(self, agent):
        from .guide import AGENT_EFFORTS, AGENT_MODELS
        model, effort = self.model_name(), self.effort.currentData() or ""
        self.model.clear()
        self.model.addItem(tr("the agent's default"), "")
        for name in AGENT_MODELS.get(agent, ()):
            self.model.addItem(name, name)
        index = self.model.findData(model)
        if index >= 0:
            self.model.setCurrentIndex(index)
        elif model:
            self.model.setEditText(model)
        self.effort.clear()
        self.effort.addItem(tr("the agent's default"), "")
        for level in AGENT_EFFORTS.get(agent, ()):
            self.effort.addItem(level, level)
        self.effort.setCurrentIndex(max(0, self.effort.findData(effort)))
        self.effort.setEnabled(self.effort.count() > 1)
        self.effort.setToolTip("" if self.effort.count() > 1 else
                               tr("{agent} sets its effort per model in its own configuration", agent=agent))

    def model_name(self):
        """The model chosen or typed ("" for the agent's default)."""
        text = self.model.currentText().strip()
        index = self.model.findText(text)
        data = self.model.itemData(index) if index >= 0 else None
        return data if data is not None else text

    def initializePage(self):  # noqa: N802
        existing = self.wizard_.existing
        if existing is not None and existing.agent:
            self.agent.setCurrentText(existing.agent)


class StartPage(Page):
    """The project's configuration, a preset of the library (the Board page's gallery: built-in ones and your
    own) or the defaults. A preset's placeholders are asked for when the page is left."""

    def __init__(self, wizard):
        super().__init__(wizard, tr("Start from"), tr("A preset fills in the next pages; everything can be changed "
                                                      "there and on the board."))
        self.group = QButtonGroup(self)
        self.buttons = {}
        for key, title in (("existing", tr("The project's current configuration")), ("preset", tr("A preset")),
                           ("custom", tr("Custom (the defaults)"))):
            button = QRadioButton(title)
            self.group.addButton(button)
            self.buttons[key] = button
        self.preset = QComboBox()
        self.preset.currentIndexChanged.connect(lambda _: self.preset_chosen())
        gallery = QPushButton(tr("Gallery…"))
        gallery.setToolTip(tr("The presets with a picture of each board, their scores and what they need"))
        gallery.clicked.connect(lambda: self.open_gallery())
        row = QHBoxLayout()
        row.addWidget(self.preset, 1)
        row.addWidget(gallery)
        self.about = plain_label(role="secondary")
        self.form.addRow(self.buttons["existing"])
        self.form.addRow(self.buttons["preset"])
        self.form.addRow("", row)
        self.form.addRow("", self.about)
        self.form.addRow(self.buttons["custom"])
        self.mine, self.values, self.values_for = [], None, None

    def fill_presets(self):
        """The presets by category (the headings cannot be chosen), your own last."""
        self.mine = PR.load_mine(self.wizard_.sai)[0]
        self.preset.blockSignals(True)
        self.preset.clear()
        groups = [(tr(title), [p for p in PR.PRESETS if p.category == key]) for key, title in PR.CATEGORIES]
        groups.append((tr(PR.MINE), self.mine))
        for heading, presets in groups:
            if not presets:
                continue
            self.preset.addItem(heading)
            self.preset.model().item(self.preset.count() - 1).setEnabled(False)
            for preset in presets:
                self.preset.addItem("   " + preset.text(preset.title), preset.id)
        self.preset.blockSignals(False)

    def select_preset(self, name):
        index = self.preset.findData(name)
        if index >= 0:
            self.preset.setCurrentIndex(index)
        return index >= 0

    def chosen_preset(self):
        name = self.preset.currentData()
        try:
            return PR.find(name, self.mine) if name else None
        except ValueError:
            return None

    def preset_chosen(self):
        self.buttons["preset"].setChecked(True)
        preset = self.chosen_preset()
        self.about.setText(preset.text(preset.description) if preset else "")
        if preset is None or preset.id != self.values_for:
            self.values, self.values_for = None, None

    def open_gallery(self):
        from .preset_gallery import PresetGallery
        wizard = self.wizard_
        gallery = PresetGallery(wizard.sai, wizard.directory, wizard.agent_page.agent.currentText(), wizard.policy,
                                None, self, wizard.settings().get("default_backend"))
        if gallery.exec() == gallery.DialogCode.Accepted and gallery.result_ is not None:
            preset, values = gallery.result_
            self.fill_presets()
            self.select_preset(preset.id)
            self.values, self.values_for = values, preset.id

    def initializePage(self):  # noqa: N802
        has_existing = self.wizard_.existing is not None
        self.buttons["existing"].setVisible(has_existing)
        self.fill_presets()
        wanted = self.wizard_.settings().get("default_template", "development")
        if not self.select_preset(wanted):
            self.select_preset("development")
        self.preset_chosen()
        self.buttons["existing" if has_existing else "preset"].setChecked(True)

    def choice(self):
        key = next((key for key, button in self.buttons.items() if button.isChecked()), "custom")
        if key == "preset":
            return self.preset.currentData() or "custom"
        return key

    def validatePage(self):  # noqa: N802
        wizard, choice = self.wizard_, self.choice()
        agent_page = wizard.agent_page
        agent = agent_page.agent.currentText()
        preset = self.chosen_preset() if choice not in ("existing", "custom") else None
        if choice == "existing" and wizard.existing is not None:
            board = B.from_project(wizard.sai, wizard.existing.document, wizard.directory)
        elif preset is not None:
            values = self.values if self.values_for == preset.id else None
            if values is None and preset.placeholders and self.values_for != preset.id:
                form = PlaceholderForm(wizard.sai, preset, self)
                if widgets.AUTO_ANSWER is None and form.exec() != QDialog.DialogCode.Accepted:
                    return False
                values = form.values()
            board = PR.build(wizard.sai, preset, wizard.directory, agent=agent, values=values,
                             backend=wizard.settings().get("default_backend"))
        else:
            board = B.with_backend(B.from_argv(wizard.sai, agent, [], workspaces=[wizard.directory]),
                                   wizard.settings().get("default_backend"))
        node = board.first("agent")
        if preset is None or preset.agent is None:
            node.props["agent"] = agent
        if agent_page.danger.isChecked():
            node.props["--dangerously-skip-permissions"] = True
        if agent_page.model_name():
            node.props["model"] = agent_page.model_name()
        if agent_page.effort.currentData():
            node.props["effort"] = agent_page.effort.currentData()
        wizard.board = board
        return True


class NetworkPage(Page):
    PROFILES = ("development", "frontier", "dns", "egress-all", "offline")

    def __init__(self, wizard):
        super().__init__(wizard, tr("Network"), tr("Where the agent's connections may go (its provider is always "
                                                   "allowed unless switched off)."))
        row = QHBoxLayout()
        self.profiles = {}
        for name in self.PROFILES:
            box = QCheckBox(name)
            self.forbid(box, "--profile", name)
            if name == "egress-all":
                self.forbid(box, "--egress-all-allow")
            row.addWidget(box)
            self.profiles[name] = box
        self.form.addRow(tr("Profiles"), row)
        row = QHBoxLayout()
        self.presets = {}
        for option in B.PRESET_OPTIONS:
            box = QCheckBox(option[len("--allow-"):])
            self.forbid(box, option)
            row.addWidget(box)
            self.presets[option] = box
        self.form.addRow(tr("Presets"), row)
        self.hosts = LinesEdit([])
        self.form.addRow(tr("Further hosts (HTTPS)"), self.hosts)
        self.ports = LinesEdit([])
        self.form.addRow(tr("Ports of services on this host"), self.ports)
        self.ask = QCheckBox(tr("ask before anything else (approvals)"))
        self.forbid(self.ask, "--ask", "off")
        self.form.addRow("", self.ask)

    def initializePage(self):  # noqa: N802
        props = self.node("network").props
        for name, box in self.profiles.items():
            box.setChecked(name in props.get("--profile", []) or (name == "offline" and bool(props.get("--offline")))
                           or (name == "egress-all" and bool(props.get("--egress-all-allow"))))
        self.shown_profiles = self.checked_profiles()
        for option, box in self.presets.items():
            box.setChecked(bool(props.get(option)))
        self.hosts.setPlainText("\n".join(props.get("--allow-host", [])))
        self.ports.setPlainText("\n".join(props.get("--allow-host-port", [])))
        approvals = self.board.first("approvals")
        self.ask.setChecked(not approvals or approvals.props.get("--ask", "on") != "off")

    def checked_profiles(self):
        return [name for name, box in self.profiles.items() if box.isChecked()]

    def validatePage(self):  # noqa: N802
        props = self.node("network").props
        profiles = self.checked_profiles()
        # Unchanged boxes keep the options as written (an existing project's --offline stays --offline).
        if profiles != getattr(self, "shown_profiles", None):
            props.pop("--offline", None)
            props.pop("--egress-all-allow", None)
            set_or_drop(props, "--profile", profiles)
        for option, box in self.presets.items():
            set_or_drop(props, option, box.isChecked())
        set_or_drop(props, "--allow-host", self.hosts.values())
        set_or_drop(props, "--allow-host-port", self.ports.values())
        if self.ask.isChecked():
            if self.board.first("approvals"):
                self.board.first("approvals").props.pop("--ask", None)
        else:
            self.node("approvals").props["--ask"] = "off"
        return True


class ProxyPage(Page):
    def __init__(self, wizard):
        super().__init__(wizard, tr("Connection proxy and TLS"), tr("Every connection passes the proxy: TLS is opened "
                                                                    "with a CA for this run, every request is logged, "
                                                                    "pushes are checked."))
        self.enabled = QCheckBox(tr("connection proxy on"))
        self.forbid(self.enabled, "--no-proxy")
        self.form.addRow("", self.enabled)
        self.passthrough = LinesEdit([])
        self.passthrough.setToolTip(tr("Hosts (or *.domain) whose TLS passes unopened: only their names are logged"))
        self.form.addRow(tr("TLS not opened for"), self.passthrough)
        self.rules = RulesEdit([])
        self.form.addRow(tr("Requests per host (--allow-http)"), self.rules)
        self.privacy = QComboBox()
        for value in ("public", "always", "off"):
            self.privacy.addItem(value, value)
            if value in wizard.forbidden.get("--privacy-check", set()):
                self.privacy.model().item(self.privacy.count() - 1).setEnabled(False)
        self.form.addRow(tr("Privacy check of pushes"), self.privacy)

    def initializePage(self):  # noqa: N802
        props = self.node("proxy").props
        self.enabled.setChecked(props.get("enabled", True) is not False)
        if None in self.wizard_.forbidden.get("--no-proxy", set()):
            self.enabled.setChecked(True)
        self.passthrough.setPlainText("\n".join(props.get("--no-inspect", [])))
        self.rules.table.setRowCount(0)
        for rule in props.get("--allow-http", []):
            parts = str(rule).split(None, 2)
            self.rules.add_row(*(parts + ["", "", ""])[:3])
        self.privacy.setCurrentIndex(max(0, self.privacy.findData(props.get("--privacy-check", "public"))))

    def validatePage(self):  # noqa: N802
        props = self.node("proxy").props
        props["enabled"] = self.enabled.isChecked()
        set_or_drop(props, "--no-inspect", self.passthrough.values())
        set_or_drop(props, "--allow-http", self.rules.rules())
        value = self.privacy.currentData()
        set_or_drop(props, "--privacy-check", "" if value == "public" else value)
        return True


class GitPage(Page):
    """Identity and push tokens: a host variable, a stored secret or a token typed here (to a private file)."""

    def __init__(self, wizard):
        super().__init__(wizard, tr("Git and tokens"), tr("Push from the sandbox: a typed token goes to a private "
                                                          "file, never into the project file. The project's own "
                                                          "remote is not offered: the agent can rewrite .git/config."))
        self.identity = QCheckBox(tr("commit as my global user.name and user.email (--git-identity)"))
        self.form.addRow("", self.identity)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("Git server"), tr("Token from"), tr("Name, file or token")])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMaximumHeight(170)
        self.form.addRow(self.table)
        row = QHBoxLayout()
        for title, url, name in (("GitHub", "https://github.com", "GH_PUSH"), ("GitLab", "https://gitlab.com", "GITLAB_TOKEN"),
                                 (tr("Local Gitea"), "http://localhost:3000", "GITEA_TOKEN")):
            quick = QPushButton(title)
            quick.setToolTip(tr("A row for {url}, its token from the stored secret {name}", url=url, name=name))
            quick.clicked.connect(lambda _=False, u=url, n=name: self.add_row(u, "secret", n))
            row.addWidget(quick)
        add = QPushButton(tr("Add token"))
        add.clicked.connect(lambda: self.add_row("", "secret", ""))
        remove = QPushButton(tr("Remove token"))
        remove.clicked.connect(lambda: [self.table.removeRow(r) for r in
                                        sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)])
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        self.form.addRow(row)
        self.token_proxy = QCheckBox(tr("keep the tokens out of the sandbox: the connection proxy adds them to Git's "
                                        "requests (--token-proxy)"))
        self.forbid(self.token_proxy, "--token-proxy")
        self.form.addRow("", self.token_proxy)
        self.note = plain_label(role="secondary")
        self.form.addRow(self.note)
        self.help = plain_label(role="secondary")
        self.help.setWordWrap(True)
        self.help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.help.setText(tr(
            "How a token gets here: keep it once with  sandbox-ai secrets set GH_PUSH  (it reads it from standard "
            "input; on a desktop without a keyring: SANDBOX_AI_SECRETS=file sandbox-ai secrets set GH_PUSH), then "
            "choose \"stored secret\" and its name. Better still for every project: sandbox-ai git-logins add "
            "https://github.com --secret GH_PUSH (the proxy adds it, no token in any sandbox). Publishing to PyPI, "
            "npm, crates.io: sandbox-ai publish-logins add pypi --secret PYPI_TOKEN, then start with --publish pypi. "
            "Several accounts: sandbox-ai identities, then --identity NAME."))
        self.form.addRow(self.help)
        self.stored = []  # the names of the kept secrets (read in the background), offered as you type

    PLACEHOLDERS = {"env": N_("GH_TOKEN  (a variable of the shell the app was started from)"),
                    "secret": N_("GH_PUSH  (sandbox-ai secrets set GH_PUSH)"),
                    "typed": N_("ghp_…  (kept in a private file, never in the project file)"),
                    "file": N_("/home/you/.config/tokens/github  (one line, mode 0600)")}

    def add_row(self, url, kind, value):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(url))
        combo = QComboBox()
        for key, text in (("env", tr("host variable")), ("secret", tr("stored secret")),
                          ("typed", tr("typed here (kept in a private file)")), ("file", tr("token file"))):
            combo.addItem(text, key)
        combo.setCurrentIndex(max(0, combo.findData(kind)))
        self.table.setCellWidget(row, 1, combo)
        edit = QLineEdit(value)
        edit.setEchoMode(QLineEdit.EchoMode.Password if kind == "typed" else QLineEdit.EchoMode.Normal)

        def kind_changed(_=None, e=edit, c=combo):
            e.setEchoMode(QLineEdit.EchoMode.Password if c.currentData() == "typed" else QLineEdit.EchoMode.Normal)
            e.setPlaceholderText(tr(self.PLACEHOLDERS[c.currentData()]))
            e.setCompleter(self.completer() if c.currentData() == "secret" else None)
            e.setToolTip(tr("Kept secrets: {names}", names=", ".join(self.stored) or tr("none yet"))
                         if c.currentData() == "secret" else "")
        combo.currentIndexChanged.connect(kind_changed)
        kind_changed()
        self.table.setCellWidget(row, 2, edit)

    def entries(self):
        found = []
        for row in range(self.table.rowCount()):
            url = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            kind = self.table.cellWidget(row, 1).currentData()
            value = self.table.cellWidget(row, 2).text()
            if url or value:
                found.append((url, kind, value))
        return found

    def completer(self):
        from PySide6.QtWidgets import QCompleter
        found = QCompleter(self.stored, self)
        found.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        found.setFilterMode(Qt.MatchFlag.MatchContains)
        return found

    def secrets_read(self, result, error):
        self.stored = [name for name, _ in result[1]] if error is None and result else []
        for row in range(self.table.rowCount()):
            combo, edit = self.table.cellWidget(row, 1), self.table.cellWidget(row, 2)
            if combo is not None and combo.currentData() == "secret":
                edit.setCompleter(self.completer())
                edit.setToolTip(tr("Kept secrets: {names}", names=", ".join(self.stored) or tr("none yet")))

    def initializePage(self):  # noqa: N802
        background(self.wizard_.sai.secrets, owner=self, timeout=60, done=self.secrets_read)  # names only, never values
        props = self.node("git").props
        self.identity.setChecked(bool(props.get("--git-identity")))
        self.token_proxy.setChecked(bool(props.get("--token-proxy")))
        self.table.setRowCount(0)
        for kind, option in (("env", "--git-token-env"), ("secret", "--git-token-secret"), ("file", "--git-token-file")):
            for value in props.get(option, []):
                url, _, name = str(value).partition("=")
                self.add_row(url, kind, name)

    def validatePage(self):  # noqa: N802
        props = self.node("git").props
        values = {"--git-token-env": [], "--git-token-secret": [], "--git-token-file": []}
        for row in range(self.table.rowCount()):
            url = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            kind_box, edit = self.table.cellWidget(row, 1), self.table.cellWidget(row, 2)
            kind, value = kind_box.currentData(), edit.text()
            if not url and not value:
                continue
            if not url.startswith(("https://", "http://")) or not value.strip():
                self.note.setText(tr("Each token needs a server (https://HOST) and a name, file or token."))
                return False
            if kind == "typed":
                try:
                    path = self.wizard_.sai.save_token(url, value)
                except (OSError, ValueError) as exc:
                    self.note.setText(tr("The token was not kept: {error}", error=printable(exc)))
                    return False
                kind, value = "file", str(path)
                kind_box.setCurrentIndex(kind_box.findData("file"))
                edit.setText(value)  # the token itself is no longer held here, only where it is kept
            option = dict(TOKEN_KINDS)[kind]
            values[option].append(f"{url}={value.strip()}")
        for option, entries in values.items():
            set_or_drop(props, option, entries)
        set_or_drop(props, "--git-identity", self.identity.isChecked())
        set_or_drop(props, "--token-proxy", self.token_proxy.isChecked())
        return True


class McpPage(Page):
    def __init__(self, wizard):
        super().__init__(wizard, tr("MCP servers"), tr("Local MCP servers the agent may use; each runs in its own "
                                                       "sandbox with its own network and folders (MCP servers page)."))
        self.servers = QListWidget()
        self.form.addRow(self.servers)
        self.forbid(self.servers, "--mcp")

    def initializePage(self):  # noqa: N802
        """The servers defined on the MCP servers page, and those of the board: the project's own (defined in
        its file) and placeholders of a preset."""
        nodes = {n.props.get("name"): n for n in self.board.of_kind("mcp") if self.board.connected(n)}
        self.servers.clear()
        defined = self.wizard_.mcp_names()
        for name in defined + [n for n in nodes if n and n not in defined]:
            node = nodes.get(name)
            label = name
            if node is not None and node.props.get("project"):
                label = tr("{name} (defined in the project)", name=name)
            elif name not in defined:
                label = tr("{name} (to fill in on the board)", name=name)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in nodes else Qt.CheckState.Unchecked)
            self.servers.addItem(item)
        if not self.servers.count():
            self.servers.addItem(tr("No MCP server is defined yet."))

    def validatePage(self):  # noqa: N802
        wanted = [self.servers.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.servers.count())
                  if self.servers.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable
                  and self.servers.item(i).checkState() == Qt.CheckState.Checked]
        agent = self.board.first("agent")
        for node in list(self.board.of_kind("mcp")):
            if node.props.get("name") not in wanted and self.board.connected(node):
                if node.props.get("project"):  # the project still defines it: only no longer used
                    self.board.disconnect(agent.id, node.id)
                else:
                    self.board.remove(node.id)
        have = {n.props.get("name"): n for n in self.board.of_kind("mcp")}
        for name in wanted:
            if name not in have:
                self.board.add("mcp", name=name)
            elif not self.board.connected(have[name]):
                self.board.connect(agent.id, have[name].id)
        return True


class HardwarePage(Page):
    def __init__(self, wizard):
        super().__init__(wizard, tr("Devices and limits"), tr("Devices weaken the isolation: a service on the host "
                                                              "reached through a host port is safer."))
        self.devices = LinesEdit([])
        self.forbid(self.devices, "--device")
        self.form.addRow(tr("Devices (/dev/...)"), self.devices)
        self.gpu = QComboBox()
        for value in ("", "nvidia", "dri", "rocm"):
            self.gpu.addItem(value or tr("none"), value)
        self.forbid(self.gpu, "--gpu")
        self.form.addRow(tr("GPU"), self.gpu)
        self.memory, self.cpus, self.pids = QLineEdit(), QLineEdit(), QLineEdit()
        self.memory.setPlaceholderText(DEFAULT_MEMORY)
        self.cpus.setPlaceholderText(tr("half of the CPUs"))
        self.pids.setPlaceholderText("4096")
        self.form.addRow(tr("Memory"), self.memory)
        self.form.addRow(tr("CPUs"), self.cpus)
        self.form.addRow(tr("Processes"), self.pids)

    def initializePage(self):  # noqa: N802
        devices = self.node("devices").props
        limits = self.node("limits").props
        self.devices.setPlainText("\n".join(devices.get("--device", [])))
        self.gpu.setCurrentIndex(max(0, self.gpu.findData(devices.get("--gpu", ""))))
        self.memory.setText(str(limits.get("--memory", "") or ""))
        self.cpus.setText(str(limits.get("--cpus", "") or ""))
        self.pids.setText(str(limits.get("--pids", "") or ""))

    def validatePage(self):  # noqa: N802
        devices, limits = self.node("devices").props, self.node("limits").props
        set_or_drop(devices, "--device", self.devices.values())
        set_or_drop(devices, "--gpu", self.gpu.currentData())
        for option, edit in (("--memory", self.memory), ("--cpus", self.cpus), ("--pids", self.pids)):
            set_or_drop(limits, option, edit.text().strip())
        for kind in ("devices", "limits"):  # an empty node says nothing
            node = self.board.first(kind)
            if node is not None and not any(k.startswith("--") for k in node.props):
                self.board.remove(node.id)
        return True


class ReviewPage(Page):
    def __init__(self, wizard):
        super().__init__(wizard, tr("Review"), tr("What will be written and started. Finish writes the project file, "
                                                  "trusts exactly it and opens the board."))
        self.command = QPlainTextEdit()
        self.command.setReadOnly(True)
        self.command.setFont(MONO)
        self.command.setMaximumHeight(70)
        self.form.addRow(tr("Command line"), self.command)
        self.score = plain_label(role="section")
        self.form.addRow(self.score)
        self.findings = QListWidget()
        self.findings.setMaximumHeight(160)
        self.form.addRow(self.findings)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(MONO)
        self.form.addRow(tr("The project file and what changes"), self.diff)
        self.verdict = plain_label(role="secondary")
        self.form.addRow(self.verdict)
        self.token = 0
        self.verdict_ok = None

    def initializePage(self):  # noqa: N802
        wizard = self.wizard_
        sai, board = wizard.sai, self.board
        self.command.setPlainText(B.command_line(sai, board))
        score, badges = B.badges(board, wizard.policy)
        problems = B.problems(sai, board, wizard.policy)
        self.score.setText(tr("Security score: {score} of 100", score=score))
        self.findings.clear()
        for problem in problems:
            self.findings.addItem(("✘ " if problem.level == "error" else "! ") + problem.text)
        for badge in badges:
            self.findings.addItem({"bad": "▼ ", "warn": "▽ ", "ok": "✔ ", "info": "· "}[badge.level] + badge.text)
        self.diff.setPlainText(wizard.file_diff())
        self.verdict.setText(tr("The launcher checks the options…"))
        self.verdict_ok = None
        self.token += 1
        token = self.token
        agent, _, _ = B.values(board)
        background(sai.dry_run, agent, B.launcher_argv(sai, board), timeout=90,
                   done=lambda result, error: self.checked(token, result, error))

    def checked(self, token, result, error):
        if token != self.token:
            return
        plan, message = result if error is None else (None, str(error))
        self.verdict_ok = plan is not None
        self.verdict.setText(tr("The launcher accepts these options.") if plan is not None else
                             tr("The launcher refuses: {message}", message=printable((message or "").strip()[-400:])))

    def validatePage(self):  # noqa: N802
        # QWizard.accept() validates the last page again: once the file is written the wizard may close, and
        # while it is being written a second Finish waits instead of asking again (it asked in a loop before).
        if self.wizard_.write_state == "written":
            return True
        if self.wizard_.write_state == "writing":
            return False
        errors = [p.text for p in B.problems(self.wizard_.sai, self.board, self.wizard_.policy) if p.level == "error"]
        if errors:
            warn(self, tr("Not written"), "\n".join(errors))
            return False
        return self.wizard_.write()


class SandboxWizard(QWizard):
    """The pages above around one board; done(board, directory) when the file was written."""
    written = Signal(object, str)

    def __init__(self, sai, settings=None, parent=None):
        super().__init__(parent)
        self.write_state = None  # None, "writing" (in its thread), "written": what Finish may do
        self.sai, self.settings = sai, settings or (lambda: {})
        self.setWindowTitle(tr("New sandbox for a project"))
        self.board, self.directory, self.existing = B.Board(), None, None
        try:
            self.policy, self.forbidden = sai.policy()[0], sai.forbidden()
        except Exception:  # noqa: BLE001 - a broken policy is shown on the System page
            self.policy, self.forbidden = {}, {}
        self.folder_page = FolderPage(self)
        self.agent_page = AgentPage(self)
        self.start_page = StartPage(self)
        self.network_page = NetworkPage(self)
        self.proxy_page = ProxyPage(self)
        self.git_page = GitPage(self)
        self.mcp_page = McpPage(self)
        self.hardware_page = HardwarePage(self)
        self.review_page = ReviewPage(self)
        for page in (self.folder_page, self.agent_page, self.start_page, self.network_page, self.proxy_page,
                     self.git_page, self.mcp_page, self.hardware_page, self.review_page):
            self.addPage(page)
        self.currentIdChanged.connect(lambda _=0: self.lock_buttons())
        self.review_page.completeChanged.connect(self.lock_buttons)  # after QWizard's own update of the buttons
        self.resize(820, 640)

    def set_write_state(self, state):
        self.write_state = state
        self.lock_buttons()

    def lock_buttons(self):
        """No Back and no Cancel while the file is written: the wizard closes when it is."""
        writing = self.write_state == "writing"
        self.button(QWizard.WizardButton.CancelButton).setEnabled(not writing)
        back = self.button(QWizard.WizardButton.BackButton)
        back.setEnabled(not writing and len(self.visitedIds()) > 1)

    def reject(self):
        if self.write_state == "writing":  # Escape, the window's close button: not while the file is written
            return
        super().reject()

    def mcp_names(self):
        try:
            return sorted(self.sai.mcp_definitions())
        except Exception:  # noqa: BLE001
            return []

    def document(self):
        return B.project_document(self.sai, self.board)

    def file_diff(self):
        document, main = self.document()
        new = json.dumps(document, indent=2) + "\n"
        old = read_small(Path(main or self.directory or ".") / ".sandbox-ai.json")
        if not old:
            return new
        return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), tr("before"), tr("after"),
                                              lineterm="")) or tr("No change.")

    def write(self):
        """Write and trust the project file (after one more explicit yes), keep the layout next to it."""
        document, main = self.document()
        if not main:
            warn(self, tr("Not written"), tr("Add a workspace first: the file is written there."))
            return False
        if not confirm(self, tr("Write and trust"), tr("Write {path} with this content and trust exactly it? The "
                                                       "details show what changes.", path=f"{main}/.sandbox-ai.json"),
                       tr("Write and trust"), details=self.file_diff()):
            return False
        # The folder may be slow (a network file system): written in a thread; the wizard closes when it is.
        self.set_write_state("writing")
        background(self.sai.write_project, main, document, owner=self, timeout=60,
                   done=lambda result, error: self.wrote(main, error))
        return False

    def wrote(self, main, error):
        if error is not None:
            self.set_write_state(None)  # Finish asks again
            warn(self, tr("Not written"), printable(error))
            return
        self.set_write_state("written")
        self.board.layout()
        try:
            B.write_layout(main, self.board)
        except OSError:
            pass
        self.written.emit(self.board, main)
        # Closed as a QDialog: QWizard.accept() and done() would validate the current page again (any page,
        # had Back been pressed meanwhile: it would change the board just handed on).
        QDialog.done(self, QDialog.DialogCode.Accepted.value)


def set_or_drop(props, option, value):
    """Set OPTION in a node's PROPS, or remove it when VALUE says nothing (empty, False)."""
    if value in (None, "", [], False):
        props.pop(option, None)
    else:
        props[option] = value

