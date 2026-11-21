"""The wizard: a new (or existing) project in a few pages, building the same board model as the Board page.

Project folder -> agent -> where its model runs (the agent's cloud provider and model, or a model server of your
own: wizard_llm.py) -> preset (the Board page's library) or custom -> network -> connection proxy and TLS ->
Git and tokens -> MCP servers -> devices and limits -> review (command line, security badges, problems, the file and what
changes, the launcher's verdict) -> write and trust. Finishing opens the board.

The safety rules of sandbox-ai's own wizard hold: a token typed here goes to a private file (mode 0600)
under ~/.config/sandbox-ai/tokens, never into the project file, which only names that file; the project's
origin remote is never offered as a default (its .git/config is the agent's to write); an existing project
file is shown, never applied, until it is written and trusted again.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWizard, QWizardPage,
)

from . import applog
from . import board as B, labels
from . import presets as PR
from . import project_trust as PT
from . import list_fields as LF
from . import widgets
from . import wizard_llm as WL
from . import model_profile_view as MPV
from .board_view import LinesEdit, PathEdit, RulesEdit, choose_agent, combo_agent, experimental_note, fill_agent_combo
from .preset_gallery import PlaceholderForm
from .files import printable, printable_text
from .model_combo import searchable
from .name_fields import NameField
from .i18n import N_, tr
from .widgets import MONO, ask_first, background, confirm, explanation, plain_label, safe_tooltip, show_text, warn

DEFAULT_MEMORY = "8g"  # the launcher's default, as a placeholder
SECRET_EXAMPLE = "LLM_KEY"  # the same in every language
URL_EXAMPLE = "https://llm.example.org"
LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it
TOKEN_KINDS = (("env", "--git-token-env"), ("secret", "--git-token-secret"), ("typed", "--git-token-file"),
               ("file", "--git-token-file"))


class Page(QWizardPage):
    def __init__(self, wizard, title, subtitle="", explained=True):
        """SUBTITLE: what the page is for - an explanation (shown only with the setting explanations on) unless
        EXPLAINED is False: a page whose line must be read to act safely (a device's risk, what Finish trusts)."""
        super().__init__()
        self.wizard_ = wizard
        self.setTitle(title)
        self.subtitle, self.explained = subtitle, explained
        if subtitle:
            self.show_subtitle(widgets.explanations_shown())
            if explained:
                widgets.on_explanations_changed(self.show_subtitle)
        self.form = QFormLayout(self)

    def show_subtitle(self, shown):
        self.setSubTitle(self.subtitle if shown or not self.explained else "")

    @property
    def board(self):
        return self.wizard_.board

    def node(self, kind):
        node = self.board.first(kind)
        return node if node is not None else self.board.add(kind)

    def keep(self):
        """What the page shows, onto the board as Next would put it, without asking anything (Back: see
        cleanupPage). A page that only writes the board keeps it so; one that asks (or keeps a token) says False."""
        return False

    def cleanupPage(self):  # noqa: N802
        """Back: what was set on this page goes onto the board first - Next fills the page from the board again
        (initializePage), and what was typed here was gone (the bug class of 2026-11-16: set, gone after going
        back). QWizardPage's own resets only registered fields (none here)."""
        super().cleanupPage()
        if self.board is not None:
            self.keep()

    def forbid(self, widget, option, value=None):
        """Grey out WIDGET when the administrator's policy forbids OPTION (or OPTION=VALUE)."""
        forbidden = self.wizard_.forbidden.get(option, set())
        if None in forbidden or (value is not None and value in forbidden):
            widget.setEnabled(False)
            widget.setToolTip(safe_tooltip(tr("The administrator's policy forbids {option}", option=option +
                                 (f"={value}" if value is not None else ""))))


class FolderPage(Page):
    """The project folder: typed, chosen with Browse…, or one of the folders used last (the workspaces of recent
    starts and the folders opened in the app, read in the background): a click takes it, a double-click or Enter
    takes it and goes on."""

    def __init__(self, wizard):
        super().__init__(wizard, tr("Project folder"), tr("The folder the agent works in (/work in the sandbox); "
                                                          "the project file is written there."))
        self.path = PathEdit()
        self.form.addRow(tr("Folder"), self.path)
        self.recent = QListWidget()
        self.recent.setAccessibleName(tr("Folders used last"))
        self.recent.setToolTip(tr("A click takes the folder; a double-click or Enter takes it and goes on"))
        self.recent.setMaximumHeight(220)
        self.recent.currentItemChanged.connect(lambda item, _previous: self.take(item))
        self.recent.itemClicked.connect(self.take)
        self.recent.itemActivated.connect(self.take_and_go_on)
        self.form.addRow(tr("Used last"), self.recent)
        self.note = plain_label(role="secondary")
        self.form.addRow(self.note)
        # The wizard is for this machine: its folders, its model servers (sandbox-ai llm here), its project file
        # (written and trusted here). For another host of sandbox-ai hosts the Board starts there.
        board_host = wizard.board_host
        self.where = plain_label(role="notice-warn" if board_host != LOCAL else "secondary")
        self.where.setText(tr("The Board starts on {host}, but this wizard sets up a project on this machine (its "
                              "folder, model servers and project file). For a project on {host}, use the Board.",
                              host=printable(board_host)) if board_host != LOCAL else
                           tr("This wizard sets up a project on this machine (its folder, model servers and project "
                              "file). For a project on another host (sandbox-ai hosts), use the Board: choose the "
                              "host there."))
        self.board_button = QPushButton(tr("Use the Board for a host"))
        self.board_button.setToolTip(tr("Close the wizard and choose the host on the Board, beside Apply: its "
                                        "launcher checks and starts the board there"))
        self.board_button.clicked.connect(lambda _=False: wizard.use_board())
        self.form.addRow(self.where)
        self.form.addRow("", self.board_button)
        self.recent_loaded = False

    def initializePage(self):  # noqa: N802
        super().initializePage()
        if not self.recent_loaded:
            self.recent_loaded = True
            background(self.wizard_.sai.recent_folders, timeout=60, done=self.recent_read)

    def recent_read(self, found, error):
        try:
            self.recent.blockSignals(True)  # filling it chooses nothing: what was typed meanwhile stays
        except RuntimeError:  # the wizard closed meanwhile
            return
        self.recent.clear()
        for path, when in (found or []) if error is None else []:
            from .backend import home_short
            shown = home_short(printable(path))
            if when:
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
                shown = tr("{folder}   (used {time})", folder=shown, time=stamp)
            item = QListWidgetItem(shown)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(safe_tooltip(printable(path)))
            self.recent.addItem(item)
        self.recent.setCurrentRow(-1)
        self.recent.blockSignals(False)
        self.recent.setVisible(self.recent.count() > 0)
        label = self.form.labelForField(self.recent)
        if label is not None:
            label.setVisible(self.recent.count() > 0)

    def take(self, item):
        if item is not None and item.data(Qt.ItemDataRole.UserRole):
            self.path.edit.setText(item.data(Qt.ItemDataRole.UserRole))

    def take_and_go_on(self, item):
        self.take(item)
        if item is not None and self.wizard() is not None:
            self.wizard().next()

    def validatePage(self):  # noqa: N802
        directory = self.path.edit.text().strip()
        if not directory:
            self.note.setText(tr("Choose a folder (a new one is created)."))
            return False
        directory = os.path.expanduser(directory)
        if not os.path.isdir(directory) and not self.create(directory):
            return False
        self.wizard_.directory = os.path.realpath(directory)
        self.wizard_.existing = None
        try:
            review = self.wizard_.sai.review_project(self.wizard_.directory)
        except FileNotFoundError:
            review = None
        except (OSError, ValueError) as exc:
            self.note.setText(tr("The project file there cannot be read: {error}", error=printable_text(exc)))
            return False
        if review is not None and review.document is not None:
            self.wizard_.existing = review
        return True


    def create(self, directory):
        """Create the new project folder DIRECTORY after the human confirmed the full path. One that is, lies in or
        holds the host's settings, keys or programs (board.sensitive, the launcher's private locations), or lies
        inside sandbox-ai or this app, is yours to choose: the question says what it means (risk) first. False (the
        reason in the note) when not created."""
        if not os.path.isabs(directory):
            self.note.setText(tr("Give the full path of the folder (it starts with / or ~)."))
            return False
        if os.path.lexists(directory):
            self.note.setText(tr("{path} exists and is not a folder.", path=printable(directory)))
            return False
        why = risk(self.wizard_.sai, directory)
        path = os.path.abspath(directory)
        question = tr("{path} does not exist yet. Create it (and the folders above it that are missing) as the new "
                      "project folder?", path=printable(path))
        if why:  # you decide: made after a yes, the question says what it means
            question += "\n\n" + tr("Warning: it is {why}. The agent can read and change what is there; read-only "
                                    "is the safer choice.", why=why)
        if not confirm(self, tr("Create the folder"), question, tr("Create")):
            self.note.setText(tr("Choose a folder that exists, or agree to create it."))
            return False
        try:
            os.makedirs(path, mode=0o755)
        except OSError as exc:
            self.note.setText(tr("{path} could not be created: {error}", path=printable(path),
                                 error=printable_text(exc.strerror or exc)))
            return False
        self.path.edit.setText(path)
        return True


def risk(sai, path):
    """What a new project folder at PATH hands the agent, or None: what board.sensitive calls "bad" (HOME, the file
    system, the host's settings, keys and programs), and the folders of sandbox-ai and of this app. Said before it
    is made, never a refusal: you decide what a sandbox gets."""
    launcher = None
    try:
        launcher = sai.launcher
    except Exception as _exc:  # noqa: BLE001 - board.sensitive's own list still holds
        applog.swallowed("wizard", "risk: board.sensitive's own list still holds", _exc)
    verdict = B.sensitive(path, launcher)
    if verdict and verdict[0] == "bad":
        return verdict[1]
    resolved = Path(os.path.abspath(path)).resolve(strict=False)
    own = [Path(__file__).resolve().parents[1]]
    if getattr(sai, "directory", None):
        own.append(Path(sai.directory).resolve())
    for folder in own:
        if resolved == folder or folder in resolved.parents:
            return tr("inside sandbox-ai's own folder ({path})", path=printable(folder))
    return None


class AgentPage(Page):
    def __init__(self, wizard):
        super().__init__(wizard, tr("Agent"), tr("Which coding agent runs in the sandbox."))
        self.agent = QComboBox()
        self.agent.setAccessibleName(tr("Agent"))
        # Agents not live-tested yet are listed, marked and said so below the choice: board_view.fill_agent_combo.
        fill_agent_combo(self.agent, wizard.sai, wizard.settings().get("default_agent", "claude"))
        self.experimental = plain_label("", role="secondary")
        self.agent.currentIndexChanged.connect(self.agent_chosen)
        self.danger = QCheckBox(tr("without the agent's own approval prompts (-D; the sandbox stays in force)"))
        self.forbid(self.danger, "--dangerously-skip-permissions")
        self.form.addRow(tr("Agent"), self.agent)
        self.form.addRow("", self.experimental)
        self.form.addRow("", self.danger)
        self.agent_chosen()

    def chosen(self):
        """The agent chosen (its name, never the badge shown with it)."""
        return combo_agent(self.agent)

    def agent_chosen(self, _index=None):
        self.experimental.setText(experimental_note(self.wizard_.sai, self.chosen()))
        self.experimental.setVisible(bool(self.experimental.text()))

    def initializePage(self):  # noqa: N802
        existing = self.wizard_.existing
        # The project's agent once per project: entered again (Back, Next), the agent chosen here stays.
        if existing is not getattr(self, "seen_existing", None):
            self.seen_existing = existing
            if existing is not None and existing.agent:
                choose_agent(self.agent, existing.agent)


class ModelPage(Page):
    """Where the model runs: the agent's own provider (its cloud models, each with its version: models.py), or a model
    server of your own - the raw API of LM Studio, Ollama, llama.cpp, vLLM or any OpenAI-compatible server,
    registered with `sandbox-ai llm add` - for any agent (Claude Code needs the Anthropic Messages API, Codex and
    OpenCode take any OpenAI-compatible server: a server the agent cannot use is shown with why, never hidden). The
    servers are read, added, checked and asked for their models in the background (wizard_llm.py). The choice goes
    on the board as --llm NAME[:MODEL] (and --no-provider-defaults for "only this server"); the cloud model and
    effort are not used meanwhile."""

    def __init__(self, wizard):
        super().__init__(wizard, tr("Model"), tr("Where the model runs: the agent's provider in the cloud, or a model "
                                                 "server of your own (its raw API)."))
        self.where = QButtonGroup(self)
        self.cloud = QRadioButton(tr("The agent's provider (cloud)"))
        # A radio button never wraps: its text stays short, the servers it means are said below it (wrapped).
        self.own = QRadioButton(tr("A model server of your own (its raw API)"))
        for button in (self.cloud, self.own):
            self.where.addButton(button)
        self.cloud.setChecked(True)
        self.form.addRow(tr("Where the model runs"), self.cloud)
        self.form.addRow("", self.own)
        self.form.addRow("", widgets.explanation(tr("LM Studio, Ollama, llama.cpp, vLLM or any OpenAI-compatible "
                                                    "server, registered with sandbox-ai llm add.")))
        # The cloud: the agent's own models with their versions, grouped by provider (models.py: sandbox-ai's table
        # of cloud models), the effort's levels those the chosen model takes (models.efforts); a model it does not
        # list can be typed.
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setAccessibleName(tr("Model"))
        self.effort = QComboBox()
        self.effort.setAccessibleName(tr("Effort"))
        self.effort_note = widgets.plain_label(role="secondary")
        self.effort_note.hide()
        self.form.addRow(tr("Model"), self.model)
        self.form.addRow(tr("Effort"), self.effort)
        self.form.addRow("", self.effort_note)
        self.model.currentIndexChanged.connect(lambda _i: self.model_changed())
        self.model.lineEdit().editingFinished.connect(self.model_changed)
        # Whether the agent shows its model's thinking in its terminal (--show-thinking): for the cloud and a server
        # of your own alike; greyed out with the reason for an agent without a switch (thinking.py).
        self.thinking = QComboBox()
        self.thinking.setAccessibleName(tr("Show thinking"))
        self.form.addRow(tr("Show thinking"), self.thinking)
        # An own server.
        self.servers = widgets.table([tr("Server"), tr("Kind"), tr("URL"), tr("Agents")], stretch=2, compact=(1, 3))
        self.servers.setAccessibleName(tr("Model servers"))
        self.servers.setMaximumHeight(170)
        self.servers.itemSelectionChanged.connect(self.server_chosen)
        self.add_button = QPushButton(tr("Add a server…"))
        self.add_button.setToolTip(tr("Register a model server of your own (sandbox-ai llm add), then check it for "
                                      "the agent (sandbox-ai llm check)"))
        self.add_button.clicked.connect(lambda: self.add_server())
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setToolTip(tr("Read the registered servers again (sandbox-ai llm list)"))
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.models_button = QPushButton(tr("List its models"))
        self.models_button.setToolTip(tr("Ask the server for the models it offers now (sandbox-ai llm models)"))
        self.models_button.clicked.connect(lambda: self.list_models())
        row = QHBoxLayout()
        for button in (self.add_button, self.refresh_button, self.models_button):
            row.addWidget(button)
        row.addStretch()
        self.form.addRow(tr("Servers"), self.servers)
        self.form.addRow("", row)
        self.server_model = QComboBox()
        self.server_model.setEditable(True)
        self.server_model.lineEdit().setPlaceholderText(tr("choose one the server lists, or type its name"))
        searchable(self.server_model)  # every model the server lists: a popup that scrolls, typing filters
        self.form.addRow(tr("Its model"), self.server_model)
        self.only = QCheckBox(labels.shown(tr("only this server (no cloud: --no-provider-defaults)")))
        self.only.setToolTip(tr("The agent reaches this server and what you allow on the next pages, not its cloud "
                                "provider"))
        self.only.setChecked(True)
        self.form.addRow("", self.only)
        # What it is for (--use-case): the pair - model x harness - measured best for it among the server's models,
        # and the others below (use_case_view.PairList: not measured and unsafe ones too, with why).
        from .use_case_view import PairList
        self.use_case_choice = QComboBox()
        self.use_case_choice.setAccessibleName(tr("Use case"))
        self.use_case_tip = tr("What the project is for: picks the model of this server and the agent sandbox-ai's "
                               "local-model research found best for it (--use-case); the pairs below list the others, "
                               "unsafe ones with why")
        self.use_case_choice.setToolTip(safe_tooltip(self.use_case_tip))
        self.use_case_choice.activated.connect(lambda _index: self.use_case_chosen())
        self.form.addRow(tr("Use case"), self.use_case_choice)
        self.pair_list = PairList()
        self.pair_list.picked.connect(lambda pair: self.use_pair(pair))
        self.form.addRow(self.pair_list)
        self.use_cases = None
        self.hint = plain_label(role="secondary")
        self.form.addRow(self.hint)
        self.status = plain_label(role="secondary", selectable=True, log="wizard")
        self.form.addRow(self.status)
        self.endpoints, self.notes, self.listed = {}, [], {}
        self.loaded, self.shown_agent, self.existing_choice, self.seen_existing = False, None, None, None
        self.seen_directory = None  # the folder the choice was made for
        self.wanted, self.wanted_model, self.models_for = None, "", None  # the server (and model) to choose once read
        self.ticket = 0
        self.where.buttonToggled.connect(lambda *_: self.where_changed())
        self.own.toggled.connect(lambda on: self.server_chosen() if on else None)
        self.where_changed()

    # -- the agent and the cloud --

    def agent(self):
        return self.wizard_.agent_page.chosen()

    def agent_changed(self, agent):
        from . import models
        same = agent == self.shown_agent
        model, effort = (self.model_name(), self.effort.currentData() or "") if same else ("", "")
        # each with its version, grouped by provider (models.py says where from)
        models.fill_combo(self.model, agent, model, getattr(self.wizard_, "sai", None))
        self.fill_efforts(agent, model, effort)
        from . import thinking
        shown = self.thinking.currentData() or "" if same else ""
        self.thinking.clear()
        for value, label in thinking.choices(agent):
            self.thinking.addItem(label, value)
        self.thinking.setCurrentIndex(max(0, self.thinking.findData(shown)) if thinking.available(agent) else 0)
        self.thinking.setEnabled(thinking.available(agent))
        self.thinking.setToolTip(safe_tooltip(thinking.tooltip(agent)))
        self.shown_agent = agent
        self.where_changed()

    def fill_efforts(self, agent, model, effort):
        """The Effort choice: the agent's default (with the model's own where known), then the levels MODEL takes in
        AGENT (models.efforts); EFFORT stays chosen when the model takes it. Whether it stayed."""
        from .guide import AGENT_EFFORTS
        from . import models
        sai = getattr(self.wizard_, "sai", None)
        self.effort.blockSignals(True)
        self.effort.clear()
        default = models.default_effort(agent, model, sai)
        self.effort.addItem(tr("the agent's default ({level} for this model)", level=default) if default
                            else tr("the agent's default"), "")
        for level in models.efforts(agent, model, sai) if AGENT_EFFORTS.get(agent) else ():
            self.effort.addItem(level, level)
        self.effort.setCurrentIndex(max(0, self.effort.findData(effort)))
        self.effort.blockSignals(False)
        return not effort or self.effort.findData(effort) >= 0

    def model_changed(self):
        """Another model: the effort's levels are its own; an effort it does not take is reset, said below."""
        if self.shown_agent is None:
            return
        effort = self.effort.currentData() or ""
        kept = self.fill_efforts(self.shown_agent, self.model_name(), effort)
        if not kept:
            self.effort_note.setText(tr("The effort {level} was reset: {model} does not take it.", level=effort,
                                        model=self.model_name() or tr("the agent's default model")))
        self.effort_note.setVisible(not kept or (self.effort_note.isVisible() and not self.effort.currentData()))
        self.where_changed()

    def model_name(self):
        """The cloud model chosen or typed ("" for the agent's default)."""
        from . import models
        return models.combo_value(self.model)

    def initializePage(self):  # noqa: N802
        agent = self.agent()
        if agent != self.shown_agent:
            self.agent_changed(agent)
        existing = self.wizard_.existing
        directory = self.wizard_.directory
        if directory != self.seen_directory:
            # Another folder: its own choice, never the one made for the folder before (its --llm, its server).
            if self.seen_directory is not None:
                self.reset_choice()
            self.seen_directory = directory
        if existing is not self.seen_existing:  # an existing project's --llm is shown as chosen
            self.seen_existing, self.existing_choice = existing, None
            if existing is not None and existing.document is not None:
                try:
                    board = B.from_project(self.wizard_.sai, existing.document, self.wizard_.directory)
                    self.existing_choice = WL.from_board(board)
                except Exception as _exc:  # noqa: BLE001 - a project the board cannot read: shown on the next pages
                    applog.swallowed("wizard", "ModelPage.initializePage: a project the board cannot read: show…", _exc)
                    self.existing_choice = None
            if self.existing_choice:
                self.own.setChecked(True)
                self.wanted, self.wanted_model = self.existing_choice["name"], self.existing_choice.get("model") or ""
                self.only.setChecked(bool(self.existing_choice.get("only")))
                # its --use-case chosen (kept until the server's models are read; its pairs show then), the project's
                # model staying selected: nothing else is changed by it
                from .use_case_view import fill_use_cases
                fill_use_cases(self.use_case_choice, self.use_cases or [], self.existing_choice.get("use_case") or "")
        if not self.loaded:
            self.refresh()
        else:
            self.show_endpoints()
        self.where_changed()

    def reset_choice(self):
        """Back to the agent's provider, no server chosen (a folder of its own: wizard.llm_choice too)."""
        self.cloud.setChecked(True)
        self.wanted, self.wanted_model, self.models_for = None, "", None
        self.only.setChecked(True)
        self.servers.blockSignals(True)
        self.servers.clearSelection()
        self.servers.setCurrentItem(None)
        self.servers.blockSignals(False)
        self.server_model.clear()
        self.use_case_choice.setCurrentIndex(0)
        self.wizard_.llm_choice, self.wizard_.llm_endpoint = None, None

    def where_changed(self):
        own = self.own.isChecked()
        why = tr("The model is the server's: --llm NAME:MODEL (the launcher refuses --model with --llm)")
        for widget in (self.model, self.effort):
            widget.setEnabled(not own and (widget is not self.effort or widget.count() > 1))
        self.model.setToolTip(safe_tooltip(why if own else ""))
        self.effort.setToolTip(safe_tooltip(why if own else "" if self.effort.count() > 1 else
                               tr("{agent} sets its effort per model in its own configuration",
                                  agent=self.shown_agent or "")))
        for widget in (self.servers, self.add_button, self.refresh_button, self.models_button, self.server_model,
                       self.only):
            widget.setEnabled(own)
        self.update_hint()
        self.show_use_cases()

    # -- the servers --

    def refresh(self):
        self.status.setText(tr("Reading the model servers (sandbox-ai llm list)…"))
        sai = self.wizard_.sai
        background(lambda: WL.list_endpoints(sai), owner=self, source=self.refresh_button,
                   timeout=WL.LIST_SECONDS + 30, done=self.endpoints_read)

    def endpoints_read(self, result, error):
        try:
            self.status.text()
        except RuntimeError:  # the wizard closed meanwhile
            return
        self.loaded = True
        if error is not None:
            self.endpoints, self.notes = {}, []
            self.status.setText(tr("The model servers could not be read: {error}", error=printable_text(error)))
        else:
            found, self.notes = result
            self.endpoints = {e["name"]: e for e in found}
            self.status.setText("\n".join(self.notes) if self.notes else "" if self.endpoints else
                                tr("No model server yet: “Add a server…” registers one (sandbox-ai llm add)."))
        self.show_endpoints()

    def show_endpoints(self):
        agent = self.agent()
        entries = list(self.endpoints.values())
        chosen = self.wanted or self.chosen_name()
        self.servers.blockSignals(True)
        widgets.fill(self.servers, [WL.endpoint_row(e) for e in entries],
                     colors=[None if WL.usable_by(e, agent) else "muted" for e in entries],
                     tooltips=["\n".join(t for t in (WL.endpoint_detail(e), WL.agent_hint(e, agent, self.wizard_.sai))
                                         if t) for e in entries],
                     keys=[e["name"] for e in entries])
        usable = [e["name"] for e in entries if WL.usable_by(e, agent)]
        if chosen not in self.endpoints and len(usable) == 1:
            chosen = usable[0]  # the only one the agent can use
        widgets.select_keys(self.servers, [chosen] if chosen in self.endpoints else [])
        self.servers.blockSignals(False)
        self.server_chosen()
        if chosen in self.endpoints:
            self.wanted, self.wanted_model = None, ""

    def chosen_name(self):
        keys = widgets.selected_keys(self.servers)
        return keys[0] if keys else None

    def chosen_endpoint(self):
        return self.endpoints.get(self.chosen_name())

    def server_chosen(self):
        endpoint = self.chosen_endpoint()
        name = endpoint["name"] if endpoint is not None else None
        # What was typed stays for the same server; another server starts from its own models.
        typed = self.server_model_value() if name == self.models_for else \
            self.wanted_model if name is not None and name == self.wanted else ""
        self.models_for = name
        self.fill_models(endpoint, self.listed.get(name), typed)
        self.update_hint()
        if endpoint is not None and endpoint["name"] not in self.listed and self.own.isChecked():
            self.list_models()

    def fill_models(self, endpoint, listed=None, typed=""):
        """The models of ENDPOINT: its default (llm add --model), then those it LISTED now (with size and whether
        loaded) or, until it answered, those it listed when it was last checked; TYPED stays."""
        combo = self.server_model
        combo.blockSignals(True)
        combo.clear()
        if endpoint is not None:
            if endpoint.get("model"):
                combo.addItem(tr("the server's default ({model})", model=printable(endpoint["model"])), "")
            rows = listed if listed is not None else \
                [{"name": m} for m in endpoint.get("models") or [] if WL.valid_model(m)]
            for row in rows:
                extra = [tr("loaded")] if row.get("loaded") else []
                if row.get("size"):
                    extra.append(f"{row['size'] / 1e9:.1f} GB")
                combo.addItem(row["name"] + (f"  ({', '.join(extra)})" if extra else ""), row["name"])
            # each model's traffic light (the server's default: its model's)
            MPV.mark_combo(combo, self.wizard_.sai, lambda index: combo.itemData(index) or endpoint.get("model"))
        index = combo.findData(typed) if typed else -1
        if index >= 0:
            combo.setCurrentIndex(index)
        elif typed:
            combo.setEditText(typed)
        elif combo.count():
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def server_model_value(self):
        """The model chosen or typed for the server ("" for its default)."""
        combo = self.server_model
        text = combo.currentText().strip()
        index = combo.findText(text)
        if index >= 0 and combo.itemData(index) is not None:
            return combo.itemData(index)
        return text

    def list_models(self):
        endpoint = self.chosen_endpoint()
        if endpoint is None:
            return
        name, sai = endpoint["name"], self.wizard_.sai
        self.ticket += 1
        ticket = self.ticket
        self.status.setText(tr("Asking {name} for its models (sandbox-ai llm models)…", name=name))
        background(lambda: WL.list_models(sai, name), owner=self, source=self.models_button,
                   timeout=WL.MODELS_SECONDS + 30, done=lambda result, error: self.models_read(ticket, name, result,
                                                                                               error))

    def models_read(self, ticket, name, result, error):
        try:
            self.status.text()
        except RuntimeError:
            return
        if ticket != self.ticket:
            return  # another server was chosen meanwhile
        if error is not None:
            self.status.setText(tr("{name} did not list its models: {error}. Type the model's name, or check that the "
                                   "server runs.", name=name, error=printable_text(error)))
            return
        self.listed[name] = result
        self.status.setText(tr("{name} offers {count} models.", name=name, count=len(result)) if result else
                            tr("{name} offers no model now: load one on the server, or type its name.", name=name))
        if self.chosen_name() == name:
            self.fill_models(self.endpoints.get(name), result, self.server_model_value())
            self.show_use_cases()

    # -- the use case: the pair measured best for it among the server's models --

    def show_use_cases(self):
        """The Use case choice and the pairs of the chosen server's models (what it listed: sizes, what is loaded)
        for the agent chosen; greyed out with why for the agent's provider or no server."""
        from . import use_case as UC
        from .use_case_view import fill_use_cases
        endpoint = self.chosen_endpoint() if self.own.isChecked() else None
        current = str(self.use_case_choice.currentData() or "")
        found = None
        if endpoint is not None:
            listed = self.listed.get(endpoint["name"])
            shown = dict(endpoint)
            if listed:
                shown["models"] = [row["name"] for row in listed if isinstance(row, dict) and row.get("name")]
                shown["model_info"] = {row["name"]: row for row in listed if isinstance(row, dict) and row.get("name")}
            found = UC.recommendations(self.wizard_.sai, [shown], True, note_agent=self.agent())
        self.use_cases = found
        fill_use_cases(self.use_case_choice, found or [], current)
        self.use_case_choice.setEnabled(found is not None)
        self.use_case_choice.setToolTip(safe_tooltip(self.use_case_tip if found is not None else tr(
            "Choose a model server of your own first: a use case picks among its models")))
        chosen = next((entry for entry in found or [] if entry["use_case"] == current), None)
        model = self.server_model_value()
        self.pair_list.set(chosen, current=(endpoint["name"], model) if endpoint is not None and model else None)

    def use_case_chosen(self):
        """A use case chosen: its choice - the pair measured best for it - as the agent and the server's model."""
        name = str(self.use_case_choice.currentData() or "")
        entry = next((e for e in self.use_cases or [] if e["use_case"] == name), None)
        choice = (entry or {}).get("choice")
        if choice:
            self.use_pair(choice)
        else:
            self.show_use_cases()

    def use_pair(self, pair):
        """PAIR's agent (on the Agent page) and model (this server's) chosen; the use case stays chosen."""
        harness = pair.get("harness")
        if harness and harness != self.agent() and choose_agent(self.wizard_.agent_page.agent, harness):
            self.agent_changed(harness)
        combo = self.server_model
        index = combo.findData(pair["model"])
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setEditText(pair["model"])
        self.update_hint()
        self.show_use_cases()

    def update_hint(self):
        endpoint = self.chosen_endpoint() if self.own.isChecked() else None
        if endpoint is None:
            self.hint.setText("")
            return
        agent = self.agent()
        lines = [WL.agent_hint(endpoint, agent, self.wizard_.sai) or
                 tr("{agent} can use {name}.", agent=agent, name=endpoint["name"])]
        if WL.loopback(endpoint) and self.wizard_.settings().get("default_backend") == "bwrap":
            lines.append(tr("{name} is on this machine's loopback: only the Podman backend reaches it, not Bubblewrap.",
                            name=endpoint["name"]))
        self.hint.setText("\n".join(lines))

    def add_server(self):
        def added(name, checked_ok=None):
            if not name:
                return
            self.wanted, self.wanted_model = name, ""
            self.listed.pop(name, None)
            self.status.setText(tr("{name} added and checked for {agent}.", name=name, agent=self.agent())
                                if checked_ok else tr("{name} added.", name=name))
            self.refresh()
        add_server(self, self.wizard_.sai, self.agent(), list(self.endpoints), added, source=self.add_button)

    # -- the choice --

    def validatePage(self):  # noqa: N802
        wizard = self.wizard_
        if not self.own.isChecked():
            wizard.llm_choice, wizard.llm_endpoint = None, None
            return True
        endpoint = self.chosen_endpoint()
        if endpoint is None:
            self.status.setText(tr("Choose a model server, or add one (Add a server…)."))
            return False
        agent = self.agent()
        if not WL.usable_by(endpoint, agent):
            self.status.setText(WL.agent_hint(endpoint, agent, wizard.sai))
            return False
        try:
            model = WL.chosen_model(endpoint, self.server_model_value(), self.listed.get(endpoint["name"], ()))
        except ValueError as exc:
            self.status.setText(printable_text(exc))
            return False
        wizard.llm_choice = {"name": endpoint["name"], "model": model, "only": self.only.isChecked()}
        if self.use_case_choice.currentData():
            wizard.llm_choice["use_case"] = str(self.use_case_choice.currentData())  # --use-case on the board
        elif (self.existing_choice or {}).get("use_case"):
            wizard.llm_choice["use_case"] = ""  # "none" for a project that had one: its --use-case goes
        wizard.llm_endpoint = endpoint
        return True

    def dropped_existing(self):
        """True when the existing project's own server was let go for the agent's provider."""
        return bool(self.existing_choice) and not self.own.isChecked()


class ServerDialog(QDialog):
    """A model server of your own for `sandbox-ai llm add` on this machine: its kind (LM Studio, Ollama, llama.cpp,
    vLLM or an OpenAI-compatible API), its URL (this machine with the kind's usual port, editable), a name, and
    optionally the secret that holds its API key (a key typed here becomes that secret: standard input, after the
    server was added) and the VPN profile it is reached through."""

    def __init__(self, sai, taken=(), parent=None):
        super().__init__(parent)
        self.sai, self.taken = sai, list(taken)
        self.url_typed = self.name_typed = False
        self.setWindowTitle(tr("Add a model server"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.form = form
        layout.addLayout(form)
        self.kind = QComboBox()
        for key, label, _ in WL.KINDS:
            self.kind.addItem(tr(label), key)
        self.url = QLineEdit()
        self.url.setToolTip(tr("The server's root, http(s)://HOST[:PORT][/PREFIX]; each agent gets the API path it "
                               "needs. Plain http only to this host, private or Tailscale addresses."))
        self.url.textEdited.connect(lambda _text: setattr(self, "url_typed", True))
        self.name = QLineEdit()
        self.name.setMaxLength(32)
        self.name.setToolTip(tr("How starts name it: --llm NAME[:MODEL]"))
        self.name.textEdited.connect(lambda _text: setattr(self, "name_typed", True))
        # this machine's secrets to choose from (names only), any other name typed
        self.secret = NameField("secret", sai, LOCAL, placeholder=SECRET_EXAMPLE)
        self.secret.setAccessibleName(tr("Secret for the API key (optional)"))
        self.value = QLineEdit()
        self.value.setPlaceholderText(tr("typed here: kept as that secret (or leave it empty if it is kept already)"))
        self.vpn = QComboBox()
        self.vpn.setEditable(True)
        self.vpn.addItem("")
        self.vpn.lineEdit().setPlaceholderText(tr("none"))
        self.vpn.setToolTip(tr("The VPN profile the server is reached through (optional)"))
        self.anthropic = QCheckBox(tr("It also offers the Anthropic Messages API (Claude Code)"))
        # Finding one: this machine's loopback and the addresses given, one read-only request per usual port.
        self.addresses = QLineEdit("127.0.0.1")
        self.addresses.setAccessibleName(tr("Addresses to look at"))
        self.addresses.setToolTip(tr("Comma-separated IP addresses (this machine's loopback, LAN addresses): one "
                                     "read-only request for the model list per usual port (Ollama 11434, LM Studio "
                                     "1234, llama.cpp 8080, vLLM 8000), short timeouts; no range is scanned"))
        self.find_button = QPushButton(tr("Find servers"))
        self.find_button.setToolTip(tr("Look for LLM servers at these addresses (sandbox-ai llm detect)"))
        self.find_button.clicked.connect(lambda: self.find())
        self.found = QComboBox()
        self.found.setAccessibleName(tr("Servers found"))
        self.found.activated.connect(lambda _index: self.take_found())
        finding = QHBoxLayout()
        finding.addWidget(self.addresses, 1)
        finding.addWidget(self.find_button)
        form.addRow(tr("Look at"), finding)
        form.addRow(tr("Found"), self.found)
        form.addRow(tr("Kind"), self.kind)
        form.addRow(tr("URL"), self.url)
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Secret for the API key (optional)"), self.secret)
        form.addRow(tr("Its value"), widgets.secret_value(self.value))
        form.addRow(tr("VPN profile (optional)"), self.vpn)
        form.addRow("", self.anthropic)
        layout.addWidget(explanation(tr("Most local servers need no key. A key typed here is kept as a secret of "
                                        "sandbox-ai (standard input, never a command line); the server's entry only "
                                        "names that secret, and the proxy adds the key on the wire.")))
        self.note = plain_label(role="notice-warn")
        self.note.hide()
        layout.addWidget(self.note)
        buttons = QDialogButtonBox()
        add = buttons.addButton(tr("Add and check"), QDialogButtonBox.ButtonRole.AcceptRole)
        add.setProperty("role", "primary")
        add.setDefault(True)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(lambda _index: self.kind_changed())
        self.kind_changed()
        self.form.setRowVisible(self.found, False)
        background(sai.vpn_profiles, owner=self, timeout=90, done=self.vpns_read)

    def find(self):
        """Look for servers at the addresses given (in the background); the ones found are offered to choose."""
        try:
            addresses = [a.strip() for a in self.addresses.text().split(",") if a.strip()]
            WL.detect_argv(addresses)  # checked before anything runs
        except ValueError as exc:
            self.note.setText(printable_text(exc))
            self.note.show()
            return
        self.note.hide()
        sai = self.sai
        background(lambda: WL.detect(sai, addresses), owner=self, source=self.find_button,
                   timeout=WL.DETECT_SECONDS + 30, done=self.detected)

    def detected(self, result, error):
        try:
            self.found.clear()
        except RuntimeError:  # closed meanwhile
            return
        if error is not None or not result:
            self.form.setRowVisible(self.found, False)
            self.note.setText(printable_text(error) if error is not None else
                              tr("No LLM server answered there: give its kind and URL."))
            self.note.show()
            return
        for entry in result:
            count = len(entry["models"])
            self.found.addItem(tr("{kind} at {url}, {count} models", kind=WL.kind_label(entry["kind"]),
                                  url=printable(entry["url"]), count=count), entry)
        self.form.setRowVisible(self.found, True)
        self.found.setCurrentIndex(0)
        self.take_found()

    def take_found(self):
        """The server chosen of the ones found: its kind and URL (and a name not taken, unless one was typed)."""
        entry = self.found.currentData()
        if not isinstance(entry, dict):
            return
        self.kind.blockSignals(True)
        self.kind.setCurrentIndex(max(0, self.kind.findData(entry["kind"])))
        self.kind.blockSignals(False)
        self.url.setText(entry["url"])
        self.url_typed = True
        if not self.name_typed:
            self.name.setText(WL.free_name(entry["kind"], self.taken))
        self.form.setRowVisible(self.anthropic, entry["kind"] == "openai-compatible")

    def kind_changed(self):
        """The URL and name follow the kind until they were typed."""
        kind = self.kind.currentData()
        if not self.url_typed:
            self.url.setText(WL.default_url(kind, self.sai))
        self.url.setPlaceholderText(URL_EXAMPLE)
        if not self.name_typed:
            self.name.setText(WL.free_name(kind, self.taken))
        self.form.setRowVisible(self.anthropic, kind == "openai-compatible")

    def vpns_read(self, result, error):
        try:
            for entry in result or [] if error is None else []:
                name = entry.get("profile")
                if isinstance(name, str) and WL.VPN.fullmatch(name) and self.vpn.findText(name) < 0:
                    self.vpn.addItem(name)
        except RuntimeError:  # closed meanwhile
            pass

    def request(self):
        """{name, kind, url, secret, vpn, anthropic} as typed, and the typed key (None: none)."""
        kind = self.kind.currentData()
        return ({"name": self.name.text().strip(), "kind": kind, "url": self.url.text().strip(),
                 "secret": self.secret.text().strip(), "vpn": self.vpn.currentText().strip(),
                 "anthropic": self.anthropic.isChecked() and kind == "openai-compatible"}, self.value.text() or None)

    def accept(self):
        fields, value = self.request()
        try:
            WL.add_argv(fields)  # checked as sandbox-ai llm checks it, before anything runs
            if value and not fields["secret"]:
                raise ValueError(tr("A typed key needs the name of the secret to keep it as."))
            if fields["name"] in self.taken:
                raise ValueError(tr("A server named {name} is registered already: choose another name.",
                                    name=fields["name"]))
        except ValueError as exc:
            self.note.setText(printable_text(exc))
            self.note.show()
            return
        super().accept()


def add_server(parent, sai, agent=None, taken=(), done=None, source=None, fields=None, value=None):
    """Ask for a model server of your own (ServerDialog; FIELDS and VALUE instead: the tests) and add it in the
    background: `sandbox-ai llm add`, a typed key kept as its secret (standard input; a secret of that name is
    replaced only after a yes), then `sandbox-ai llm check NAME --agent AGENT`. The CLI's errors and the check's
    findings are shown; DONE(name or None, whether the check passed) afterwards."""
    title = tr("Add a model server")
    if fields is None:
        dialog = ServerDialog(sai, taken, parent)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        fields, value = dialog.request()
        # The typed key is passed on below and kept nowhere else (not in a hidden dialog of the page).
        dialog.value.clear()
        dialog.deleteLater()
        if not accepted:
            return
    finish = done or (lambda name, checked: None)

    def finished(result, error):
        if error is not None:
            warn(parent, title, printable_text(error), modal=False)
            finish(None, False)
            return
        name, (status, out, err) = result["name"], result["added"]
        if status != 0:
            warn(parent, title, tr("sandbox-ai llm add refused {name}: {error}", name=name,
                                   error=WL.message(status, out, err)), modal=False)
            finish(None, False)
            return
        kept = result.get("kept")
        if kept is not None and kept[0] != 0:
            warn(parent, title, tr("{name} added, but its key was not kept as the secret {secret}: {error}",
                                   name=name, secret=fields.get("secret", ""), error=WL.message(*kept)), modal=False)
        checked = result.get("checked")
        ok = checked is not None and checked[0] == 0
        if checked is not None and not ok:
            show_text(parent, tr("Check of {name}", name=name),
                      tr("{name} was added, but the check for {agent} found problems (does the server run, and at "
                         "this URL?):", name=name, agent=agent or "?") + "\n\n" +
                      "\n".join(printable(line) for line in ((checked[1] or "") + (checked[2] or "")).strip()
                                [-6000:].splitlines()), modal=False)
        finish(name, ok)

    def go():
        def work():
            keep = (lambda: sai.set_secret(fields["secret"], value)) if value else None
            return WL.add_and_check(sai, fields, agent, keep)
        background(work, owner=parent, work=True, source=source, timeout=WL.ADD_SECONDS + WL.CHECK_SECONDS + 90,
                   done=finished)
    if not value:
        go()
        return
    secret = fields["secret"]

    def looked(exists, error):
        exists = exists if error is None else None
        text = tr("A secret {name} is kept already: the key typed replaces its value, for everything that names it. "
                  "Replace it?", name=printable(secret)) if exists else \
            tr("Whether a secret {name} is kept already could not be told: the key typed would replace its value. "
               "Go on?", name=printable(secret))
        if ask_first(parent, "secret-replace", title, text, tr("Replace"), detail={"exists": exists}):
            go()
        else:
            finish(None, False)
    background(lambda: sai.secret_exists(secret), owner=parent, timeout=90, done=looked)


def fill_model_combo(combo, only, endpoints, agent, sai, value="", only_checked=False):
    """COMBO as the Model choice beside AGENT (the launch page, the board's agent node): the agent's own provider,
    then each registered server as NAME and NAME:MODEL (wizard_llm.model_choices); one AGENT cannot use is shown
    greyed, with why. VALUE (--llm's, or a placeholder still to fill in) is chosen, added when it is not listed. ONLY,
    the "only this server" box, is checked as ONLY_CHECKED says and only usable with a server. No signal fires."""
    searchable(combo, strict=True)  # every model listed (70 and more): a popup that scrolls, typing filters
    combo.blockSignals(True)
    only.blockSignals(True)
    try:
        combo.clear()
        tags = {row["value"]: row["tags"] for row in WL.picker_rows(endpoints, agent, sai)}  # (text, code, vision ...)
        for label, item, hint in WL.model_choices(endpoints, agent, sai):
            combo.addItem(label, item)
            index = combo.count() - 1
            tip = (hint or f"--llm {item}") if item else tr("The agent's own provider and login "
                                                                             "(no --llm)")
            if item and tags.get(item) and not hint:
                tip += "\n" + " ".join(f"[{tr(dict(WL.TAGS)[t])}]" for t in tags[item])
            combo.setItemData(index, widgets.safe_tooltip(tip), Qt.ItemDataRole.ToolTipRole)
            if hint:
                combo.model().item(index).setEnabled(False)
        if value and combo.findData(value) < 0:  # typed into the options, a placeholder, or no longer registered
            combo.addItem(value, value)
        MPV.mark_combo(combo, sai)  # each model's traffic light: how far the research supports it, per agent
        combo.setCurrentIndex(max(0, combo.findData(value or "")))
        only.setChecked(bool(value) and bool(only_checked))
        only.setEnabled(bool(value))
    finally:
        combo.blockSignals(False)
        only.blockSignals(False)


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
        self.catalogue, self.reading = PR.Catalogue(), None  # the presets of the wizard's host (fill_presets)

    def host(self):
        """The host the wizard sets the project up for ("local": this machine)."""
        return getattr(self.wizard_, "host", lambda: "local")()

    def fill_presets(self):
        """The catalogue of the wizard's host by category (the headings cannot be chosen): that host's sandbox-ai
        presets, the app's own and yours - the same list as the gallery's. Another host's sandbox-ai presets are read
        in the background (meanwhile the ones it said last)."""
        from .preset_gallery import fill_preset_combo
        host, current = self.host(), self.preset.currentData()
        self.catalogue = PR.catalogue(self.wizard_.sai, host)
        self.mine = self.catalogue.mine()
        fill_preset_combo(self.preset, self.catalogue, current)
        if host != "local" and not self.catalogue.complete and getattr(self, "reading", None) != host:
            self.reading = host
            background(PR.host_documents, self.wizard_.sai, host, owner=self, timeout=90,
                       done=lambda result, error: self.host_presets_read(host, error))

    def host_presets_read(self, host, error):
        self.reading = None
        if error is None and host == self.host():
            self.fill_presets()  # what that host said is remembered (presets.remember): its catalogue now

    def select_preset(self, name):
        from .preset_gallery import select_preset
        return select_preset(self.preset, self.catalogue, name)

    def chosen_preset(self):
        key = self.preset.currentData()
        try:
            return self.catalogue.find(key) if key else None
        except ValueError:
            return None

    def preset_chosen(self):
        self.buttons["preset"].setChecked(True)
        preset = self.chosen_preset()
        self.about.setText(preset.text(preset.description) if preset else "")
        if preset is None or preset.key != self.values_for:
            self.values, self.values_for = None, None

    def open_gallery(self):
        from .preset_gallery import PresetGallery
        wizard = self.wizard_
        gallery = PresetGallery(wizard.sai, wizard.directory, wizard.agent_page.chosen(), wizard.policy,
                                None, self, wizard.settings().get("default_backend"), host=self.host())
        chosen = gallery.result_ if gallery.exec() == gallery.DialogCode.Accepted else None
        widgets.drop_after_exec(gallery)  # (made anew each time)
        if chosen is not None:
            preset, values = chosen
            self.fill_presets()
            if not self.select_preset(preset.key):  # e.g. the host's presets read anew meanwhile: never another one
                from .preset_gallery import fill_preset_combo
                self.catalogue.presets.append(preset)
                fill_preset_combo(self.preset, self.catalogue, preset.key)
            self.values, self.values_for = values, preset.key

    def initializePage(self):  # noqa: N802
        has_existing = self.wizard_.existing is not None
        self.buttons["existing"].setVisible(has_existing)
        self.fill_presets()
        # The default once per folder and project: entered again (Back to the model, Next), the choice made stays.
        seen = (self.wizard_.directory, id(self.wizard_.existing), self.host())
        if seen == getattr(self, "seen", None):
            return
        self.seen = seen
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

    def confirm_cloud(self, existing):
        """True when the human agrees that the project's own model server EXISTING goes for the agent's provider."""
        return confirm(self, tr("Model"), cloud_question(existing), tr("Use the agent's provider"),
                       reject=tr("Back"))

    def basis(self):
        """What the board is built from here: the folder, project and host, this page's choice (its preset's values
        too) and the Agent and Model pages' choices."""
        wizard, choice = self.wizard_, self.choice()
        model_page = wizard.model_page
        preset = self.chosen_preset() if choice not in ("existing", "custom") else None
        values = self.values if preset is not None and self.values_for == preset.key else None
        return (wizard.directory, id(wizard.existing), self.host(), choice, preset.key if preset else None,
                repr(sorted((values or {}).items())), wizard.agent_page.chosen(), wizard.agent_page.danger.isChecked(),
                repr(wizard.llm_choice), model_page.thinking.currentData() if model_page.thinking.isEnabled() else None,
                model_page.model_name(), model_page.effort.currentData(), wizard.settings().get("default_backend"))

    # The places of basis() that only the Model page sets: the server choice, Show thinking, the cloud model, effort.
    MODEL_BASIS = (8, 9, 10, 11)

    def model_only_change(self, basis):
        """Whether BASIS differs from what the board was built from only in the Model page's choices: the board then
        keeps every later page's changes and only what depends on the model changes (keep_model). A preset whose
        placeholders the model server fills (<llm:...>, the VPN that follows it) is built anew: the server is its
        values."""
        wizard, before = self.wizard_, getattr(self, "built_from", None)
        if wizard.board is None or wizard.board is not getattr(self, "built", None) or before is None or \
                len(before) != len(basis):
            return False
        if any(old != new for index, (old, new) in enumerate(zip(before, basis)) if index not in self.MODEL_BASIS):
            return False
        preset = self.chosen_preset() if self.choice() not in ("existing", "custom") else None
        return preset is None or not any(p.kind == "llm" for p in preset.placeholders or ())

    def keep_model(self):
        """The Model page's choices onto the board built before, nothing else changed: --llm and "only this server"
        (or the server taken off again for the agent's provider - asked first for the project's own), the agent's
        cloud model and effort, Show thinking. False when the human keeps the project's server (Back)."""
        wizard, board, model_page = self.wizard_, self.wizard_.board, self.wizard_.model_page
        llm, before = wizard.llm_choice, getattr(self, "built_llm", None)
        node = board.first("agent")
        if llm:
            WL.apply(board, llm)
        elif WL.uses_server(board) and (before or model_page.dropped_existing()):
            if model_page.dropped_existing() and self.choice() == "existing" and not before and \
                    not self.confirm_cloud(model_page.existing_choice):
                return False
            WL.clear(board)
        thinking = model_page.thinking.currentData() if model_page.thinking.isEnabled() else None
        if thinking:
            node.props["thinking"] = thinking
        else:
            node.props.pop("thinking", None)
        for key, value in (("model", model_page.model_name()), ("effort", model_page.effort.currentData())):
            if value and not WL.uses_server(board):
                node.props[key] = value
            else:
                node.props.pop(key, None)
        return True

    def validatePage(self):  # noqa: N802
        wizard, choice = self.wizard_, self.choice()
        # Nothing changed since the board was built (Back to here and Next again): the board stays as the later pages
        # left it - built anew, every change made there was gone.
        basis = self.basis()
        if wizard.board is not None and wizard.board is getattr(self, "built", None) and \
                basis == getattr(self, "built_from", None):
            return True
        # Only the model changed (Back to the Model page): the same board, only what depends on the model changes.
        if self.model_only_change(basis):
            if not self.keep_model():
                return False
            self.built_from, self.built_llm = self.basis(), wizard.llm_choice
            return True
        agent_page, model_page = wizard.agent_page, wizard.model_page
        agent = agent_page.chosen()
        llm = wizard.llm_choice  # an own model server chosen on the Model page, or None: the agent's provider
        preset = self.chosen_preset() if choice not in ("existing", "custom") else None
        if choice == "existing" and wizard.existing is not None:
            board = B.from_project(wizard.sai, wizard.existing.document, wizard.directory)
        elif preset is not None:
            values = self.values if self.values_for == preset.key else None
            # The preset's LLM server (and the VPN that follows it) is the one chosen on the Model page.
            given = WL.prefilled(preset, llm, wizard.llm_endpoint)
            if values is None and preset.placeholders and self.values_for != preset.key:
                if given and not WL.needs_form(preset, given):
                    values = given
                else:
                    form = PlaceholderForm(wizard.sai, preset, self, agent=agent, host=self.host())
                    for marker, value in given.items():
                        form.set_value(marker, value)
                    if widgets.AUTO_ANSWER is None and widgets.exec_dropped(form) != QDialog.DialogCode.Accepted:
                        return False
                    values = form.values()
                    # Kept for this preset: Back and Next again does not ask for them once more
                    self.values, self.values_for = dict(values), preset.key
            elif values is not None:
                values = {**values, **given}
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
        if llm:
            WL.apply(board, llm)  # --llm NAME[:MODEL] for whichever agent, no cloud model
        elif model_page.dropped_existing() and choice == "existing" and WL.uses_server(board):
            # The project's own server let go for the agent's provider: said, and only after a yes (a server-only
            # project reaches its cloud provider from now on). A preset keeps its own server (its form asks for it).
            if not self.confirm_cloud(model_page.existing_choice):
                return False
            WL.clear(board)
        if model_page.thinking.currentData() and model_page.thinking.isEnabled():
            node.props["thinking"] = model_page.thinking.currentData()  # --show-thinking, for any model
        if not WL.uses_server(board):
            if model_page.model_name():
                node.props["model"] = model_page.model_name()
            if model_page.effort.currentData():
                node.props["effort"] = model_page.effort.currentData()
        else:
            node.props.pop("model", None)  # the launcher refuses --model with --llm: the model is the server's
        wizard.board = board
        self.built, self.built_from = board, self.basis()
        self.built_llm = llm
        return True


def cloud_question(existing):
    """The question before the project's own model server EXISTING ({name, model, only}) is taken off the board for
    the agent's provider (the Model page's cloud choice)."""
    value = WL.llm_value(existing["name"], existing.get("model") or "") if existing else "?"
    text = tr("The project uses its own model server ({value}). The Model page says the agent's provider (cloud): "
              "--llm {value} is taken off the board.", value=printable(value))
    if existing and existing.get("only"):
        text += " " + tr("It was the only model the agent reached (--no-provider-defaults): from now on the agent "
                         "reaches its cloud provider, and its login is copied in.")
    return text + "\n\n" + tr("Go back to the Model page to keep the server.")


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
        # Further hosts, whole domains (--allow-domain DOMAIN: DOMAIN and every name below it, sandbox-ai's
        # --allow-host DOMAIN '*.DOMAIN') and ports of this host: one field of kinds (list_fields, 2026-11-19).
        self.lists = LF.ListFields([
            LF.option_kind("--allow-host", tr("Further hosts (HTTPS)"), tip=tr(
                "Names separated by commas or spaces. *.DOMAIN opens every name below DOMAIN, each as it is looked up "
                "(not for shared hosting such as *.github.io, nor public suffixes).")),
            LF.option_kind("--allow-domain", tr("Whole domains (HTTPS)"), tip=tr(
                "Domains, one or more per line, separated by commas or spaces: each one and every name below it "
                "(--allow-domain; not for shared hosting such as github.io, nor public suffixes such as co.uk).")),
            LF.option_kind("--allow-host-port", tr("Ports of services on this host"), tip=tr(
                "Ports on this computer's loopback (a local Git server, an LLM server), reached through a relay that "
                "only you start; separated by commas or spaces"))], {}, name=tr("Hosts, domains, ports"))
        for option in ("--allow-domain", "--allow-host"):  # (a domain is its hosts: greyed out with either)
            forbidden = self.wizard_.forbidden.get(option, set())
            if None in forbidden and self.lists.kinds["--allow-domain"].enabled:
                self.lists.set_kind_enabled("--allow-domain", False,
                                            tr("The administrator's policy forbids {option}", option=option))
        self.form.addRow(tr("Hosts, domains, ports"), self.lists)
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
        for option in ("--allow-host", "--allow-domain", "--allow-host-port"):
            self.lists.set_values(option, props.get(option, []))
        approvals = self.board.first("approvals")
        self.ask.setChecked(not approvals or approvals.props.get("--ask", "on") != "off")
        self.shown_ask = self.ask.isChecked()  # unchanged, --ask stays as written (allow too)

    def checked_profiles(self):
        return [name for name, box in self.profiles.items() if box.isChecked()]

    def keep(self):  # only writes the board: Back keeps it as Next would
        return self.validatePage()

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
        if not self.lists.take_typed():  # (a value typed and not added: added; one it refuses says why)
            return False
        set_or_drop(props, "--allow-host", self.lists.values("--allow-host"))
        if self.lists.kinds["--allow-domain"].enabled:  # greyed out by the policy: what the board has stays as it is
            set_or_drop(props, "--allow-domain", self.lists.values("--allow-domain"))
        set_or_drop(props, "--allow-host-port", self.lists.values("--allow-host-port"))
        if self.ask.isChecked() == getattr(self, "shown_ask", None):
            pass  # as it was: --ask allow (every question allowed, chosen on the board) is not turned into on
        elif self.ask.isChecked():
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
        self.form.addRow(labels.shown(tr("Requests per host (--allow-http)")), self.rules)
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

    def keep(self):  # only writes the board: Back keeps it as Next would
        return self.validatePage()

    def validatePage(self):  # noqa: N802
        props = self.node("proxy").props
        props["enabled"] = self.enabled.isChecked()
        set_or_drop(props, "--no-inspect", self.passthrough.values())
        set_or_drop(props, "--allow-http", self.rules.rules())
        value = self.privacy.currentData()
        set_or_drop(props, "--privacy-check", "" if value == "public" else value)
        return True


class GitPage(Page):
    """Identity and push tokens: a host variable, a stored secret or a token typed here (kept as a secret)."""

    def __init__(self, wizard):
        super().__init__(wizard, tr("Git and tokens"), tr("Push from the sandbox: a typed token is kept in sandbox-ai "
                                                          "secrets, never in the project file. The project's own "
                                                          "remote is not offered: the agent can rewrite .git/config."))
        self.identity = QCheckBox(labels.shown(tr(
            "commit as my global user.name and user.email (--git-identity)")))
        self.form.addRow("", self.identity)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("Git server"), tr("Token from"), tr("Name, file or token")])
        widgets.tables.keep(widgets.tables.setup(self.table, 2, (1,)), "wizard-git")
        self.table.verticalHeader().setVisible(False)
        self.table.setMaximumHeight(170)
        self.form.addRow(self.table)
        row = QHBoxLayout()
        for title, url, name in (("GitHub", "https://github.com", "GH_PUSH"), ("GitLab", "https://gitlab.com", "GITLAB_TOKEN"),
                                 (tr("Local Gitea"), "http://localhost:3000", "GITEA_TOKEN")):
            quick = QPushButton(title)
            quick.setToolTip(safe_tooltip(tr("A row for {url}, its token from the stored secret {name}", url=url, name=name)))
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
        widgets.list_menu(self.table, lambda: [remove, None, add])
        self.form.addRow(row)
        self.token_proxy = QCheckBox(labels.shown(tr("keep the tokens out of the sandbox: the connection proxy adds "
                                                     "them to Git's requests (--token-proxy)")))
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
                    "typed": N_("ghp_…  (kept in sandbox-ai secrets, never in the project file)"),
                    "file": N_("/home/you/.config/tokens/github  (one line, mode 0600)")}

    def add_row(self, url, kind, value):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(url))
        combo = QComboBox()
        for key, text in (("env", tr("host variable")), ("secret", tr("stored secret")),
                          ("typed", tr("typed here (kept as a secret)")), ("file", tr("token file"))):
            combo.addItem(text, key)
        combo.setCurrentIndex(max(0, combo.findData(kind)))
        self.table.setCellWidget(row, 1, combo)
        edit = QLineEdit(value)
        cell = widgets.secret_value(edit, kind == "typed")  # a typed token: its value, "Show value" beside it

        def kind_changed(_=None, e=edit, c=combo):
            widgets.mark_secret(e, c.currentData() == "typed")
            e.setPlaceholderText(tr(self.PLACEHOLDERS[c.currentData()]))
            e.setCompleter(self.completer() if c.currentData() == "secret" else None)
            e.setToolTip(safe_tooltip(tr("Kept secrets: {names}", names=", ".join(self.stored) or tr("none yet"))
                         if c.currentData() == "secret" else ""))
        combo.currentIndexChanged.connect(kind_changed)
        kind_changed()
        self.table.setCellWidget(row, 2, cell)
        widgets.tables.fit_columns(self.table)

    def entries(self):
        found = []
        for row in range(self.table.rowCount()):
            url = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            kind = self.table.cellWidget(row, 1).currentData()
            value = self.token_edit(row).text()
            if url or value:
                found.append((url, kind, value))
        return found

    def token_edit(self, row):
        """The token field of ROW (in its cell beside its "Show value" toggle)."""
        return self.table.cellWidget(row, 2).findChild(QLineEdit)

    def completer(self):
        from PySide6.QtWidgets import QCompleter
        found = QCompleter(self.stored, self)
        found.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        found.setFilterMode(Qt.MatchFlag.MatchContains)
        return found

    def secrets_read(self, result, error):
        self.stored = [name for name, _ in result[1]] if error is None and result else []
        for row in range(self.table.rowCount()):
            combo, edit = self.table.cellWidget(row, 1), self.token_edit(row)
            if combo is not None and combo.currentData() == "secret":
                edit.setCompleter(self.completer())
                edit.setToolTip(safe_tooltip(tr("Kept secrets: {names}", names=", ".join(self.stored) or tr("none yet"))))

    def keep(self):
        """Back: the rows that name where a token is (a variable, a stored secret, a file) and the boxes onto the board
        as Next would; a token typed here is not written anywhere on the way back (it is kept in a private file only
        by Next). The table stays as it is while the board says what was kept (initializePage): a typed row too."""
        props = self.node("git").props
        values = {"--git-token-env": [], "--git-token-secret": [], "--git-token-file": []}
        for row in range(self.table.rowCount()):
            url = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            kind, value = self.table.cellWidget(row, 1).currentData(), self.token_edit(row).text().strip()
            if kind != "typed" and url.startswith(("https://", "http://")) and value:
                values[dict(TOKEN_KINDS)[kind]].append(f"{url}={value}")
        for option, entries in values.items():
            set_or_drop(props, option, entries)
        set_or_drop(props, "--git-identity", self.identity.isChecked())
        set_or_drop(props, "--token-proxy", self.token_proxy.isChecked())
        self.kept = json.dumps(props, sort_keys=True, default=str)
        return True

    def initializePage(self):  # noqa: N802
        background(self.wizard_.sai.secrets, owner=self, timeout=60, done=self.secrets_read)  # names only, never values
        props = self.node("git").props
        if getattr(self, "kept", None) == json.dumps(props, sort_keys=True, default=str):
            return  # as Back left it (keep): the rows as they were, a token typed in one too
        self.kept = None
        self.identity.setChecked(bool(props.get("--git-identity")))
        self.token_proxy.setChecked(bool(props.get("--token-proxy")))
        self.table.setRowCount(0)
        for kind, option in (("env", "--git-token-env"), ("secret", "--git-token-secret"), ("file", "--git-token-file")):
            for value in props.get(option, []):
                url, _, name = str(value).partition("=")
                moved = self.wizard_.sai.moved_token(name) if kind == "file" else None
                # a token file of an older wizard's, moved into sandbox-ai secrets: its secret
                self.add_row(url, "secret" if moved else kind, moved or name)

    def keep_typed(self, row, url, value):
        """A token typed for URL kept as a secret of sandbox-ai (`secrets set GIT_TOKEN_HOST` on standard input), in
        the background: the row then names the secret and the wizard goes on (token_kept). Locked secrets, or private
        files without a primary password yet: the password is asked first (hidden), as the Credentials page asks it."""
        self.keeping = True
        self.note.setText(tr("Keeping the token as a secret of sandbox-ai…"))
        background(self.wizard_.sai.save_token, url, value, owner=self, timeout=90, work=True,
                   done=functools.partial(self.token_kept, row, url, value, False))

    def token_kept(self, row, url, value, retried, result, error):
        self.keeping = False
        if error is None and result:
            kind_box, edit = self.table.cellWidget(row, 1), self.token_edit(row)
            if kind_box is not None and edit is not None:
                kind_box.setCurrentIndex(kind_box.findData("secret"))
                edit.setText(result)  # the token itself is no longer held here, only where it is kept
            self.note.setText("")
            self.wizard().next()
            return
        error = str(error or tr("The token was not kept."))
        if retried:
            self.note.setText(tr("The token was not kept: {error}", error=printable_text(error.strip())))
            return
        go_on = functools.partial(self.unlocked, row, url, value)
        if "sandbox-ai secrets unlock" in error:
            password = widgets.ask_password(self, tr("Primary password"), tr(
                "The secrets{where} are encrypted. Type their primary password: they stay unlocked for 8 hours, or "
                "until Lock.", where=""), tr("Unlock"))
            if not password:
                self.note.setText(tr("The token was not kept: {error}", error=printable_text(error.strip())))
                return
            self.keeping = True
            background(self.wizard_.sai.secrets_unlock, password, owner=self, timeout=120, work=True, done=go_on)
        elif "sandbox-ai secrets password set" in error:
            password = widgets.ask_password(self, tr("Primary password"), tr(
                "The secrets{where} are kept in private files. A primary password encrypts them: without it nobody "
                "reads them from the disk or a backup - you neither, there is no way to recover it. It is asked once "
                "per session.\n\nNew primary password (8 characters or more):", where=""), tr("Next"))
            again = password and widgets.ask_password(self, tr("Primary password"),
                                                      tr("The same primary password again:"),
                                                      tr("Set primary password"))
            if not password or again != password:
                self.note.setText(tr("The two passwords are not the same: nothing was set.") if password else
                                  tr("The token was not kept: {error}", error=printable_text(error.strip())))
                return
            self.keeping = True
            background(self.wizard_.sai.secrets_new_password, password, owner=self, timeout=120, work=True,
                       done=go_on)
        else:
            self.note.setText(tr("The token was not kept: {error}", error=printable_text(error.strip())))
        password = again = None  # noqa: F841 - not held here any longer

    def unlocked(self, row, url, value, result, error):
        self.keeping = False
        status, out, err = widgets.outcome(result, error)
        if status != 0:
            self.note.setText(tr("The token was not kept: {error}", error=printable_text((err or out).strip())))
            return
        self.keeping = True
        background(self.wizard_.sai.save_token, url, value, owner=self, timeout=90, work=True,
                   done=functools.partial(self.token_kept, row, url, value, True))

    def validatePage(self):  # noqa: N802
        props = self.node("git").props
        values = {"--git-token-env": [], "--git-token-secret": [], "--git-token-file": []}
        for row in range(self.table.rowCount()):
            url = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            kind_box, edit = self.table.cellWidget(row, 1), self.token_edit(row)
            kind, value = kind_box.currentData(), edit.text()
            if not url and not value:
                continue
            if not url.startswith(("https://", "http://")) or not value.strip():
                self.note.setText(tr("Each token needs a server (https://HOST) and a name, file or token."))
                return False
            if kind == "typed":
                if not getattr(self, "keeping", False):
                    self.keep_typed(row, url, value)  # kept in the background; the wizard goes on once it is
                return False
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

    def keep(self):  # only writes the board: Back keeps it as Next would
        return self.validatePage()

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
                                                              "reached through a host port is safer."), explained=False)
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

    def keep(self):  # only writes the board: Back keeps it as Next would
        return self.validatePage()

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
                                                  "trusts exactly it and opens the board."), explained=False)
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
        self.verdict = plain_label(role="secondary", selectable=True)
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
        background(sai.dry_run, agent, B.check_argv(sai, board), timeout=90,
                   done=lambda result, error: self.checked(token, result, error))

    def checked(self, token, result, error):
        if token != self.token:
            return
        plan, message = result if error is None else (None, str(error))
        self.verdict_ok = plan is not None
        self.verdict.setText(tr("The launcher accepts these options.") if plan is not None else
                             tr("The launcher refuses: {message}", message=printable_text((message or "").strip()[-400:])))

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

    board_wanted = Signal()  # "Use the Board for a host": the wizard closed for the Board's host choice

    def __init__(self, sai, settings=None, parent=None, board_host=LOCAL):
        super().__init__(parent)
        # The wizard works on this machine only (folders, model servers, the project file it writes and trusts);
        # BOARD_HOST is where the Board that opened it starts (said on the first page).
        self.board_host = board_host if isinstance(board_host, str) and board_host else LOCAL
        self.write_state = None  # None, "writing" (in its thread), "written": what Finish may do
        self.sai, self.settings = sai, settings or (lambda: {})
        self.setWindowTitle(tr("New sandbox for a project"))
        self.board, self.directory, self.existing = B.Board(), None, None
        self.llm_choice, self.llm_endpoint = None, None  # the Model page's own server ({name, model, only}), if any
        try:
            self.policy, self.forbidden = sai.policy()[0], sai.forbidden()
        except Exception as _exc:  # noqa: BLE001 - a broken policy is shown on the System page
            applog.swallowed("wizard", "SandboxWizard.__init__: a broken policy is shown on the System page", _exc)
            self.policy, self.forbidden = {}, {}
        self.folder_page = FolderPage(self)
        self.agent_page = AgentPage(self)
        self.model_page = ModelPage(self)
        self.start_page = StartPage(self)
        self.network_page = NetworkPage(self)
        self.proxy_page = ProxyPage(self)
        self.git_page = GitPage(self)
        self.mcp_page = McpPage(self)
        self.hardware_page = HardwarePage(self)
        self.review_page = ReviewPage(self)
        for page in (self.folder_page, self.agent_page, self.model_page, self.start_page, self.network_page, self.proxy_page,
                     self.git_page, self.mcp_page, self.hardware_page, self.review_page):
            self.addPage(page)
        self.currentIdChanged.connect(lambda _=0: self.lock_buttons())
        self.review_page.completeChanged.connect(self.lock_buttons)  # after QWizard's own update of the buttons
        self.resize(820, 640)

    def host(self):
        """Where the wizard's project is: this machine (the presets, servers and project file are read and written
        here); another host's project is made on the Board."""
        return LOCAL

    def use_board(self):
        """Closed for the Board (when nothing is being written): there the host is chosen."""
        if self.write_state == "writing":
            return
        self.board_wanted.emit()
        self.reject()

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
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("wizard", "SandboxWizard.mcp_names failed (went on without it)", _exc)
            return []

    def document(self):
        return B.project_document(self.sai, self.board)

    def trust_question(self):
        """(question, details) of Write and trust: every key that will be trusted, what weakens the sandbox marked, the
        changes against the version trusted last (never against the file in the folder, which the agent may have
        written) and the whole file."""
        document, main = self.document()
        intro = tr("Write {path} with this content and trust exactly it? The details show the changes against the "
                   "version you trusted last, and the whole file.", path=f"{main}/.sandbox-ai.json")
        return PT.question(self.sai, main or self.directory or ".", document, intro)

    def file_diff(self):
        """What the last page shows: what will be trusted, and the changes against the version trusted last."""
        question, details = self.trust_question()
        return question + "\n\n" + details

    def write(self):
        """Write and trust the project file (after one more explicit yes), keep the layout next to it."""
        document, main = self.document()
        if not main:
            warn(self, tr("Not written"), tr("Add a workspace first: the file is written there."))
            return False
        question, details = self.trust_question()
        # Trusted, the file's options apply at every later start of the project: asked first (Cancel the default).
        if not ask_first(self, "project-trust", tr("Write and trust"), question, tr("Write and trust"), details=details):
            return False
        # The folder may be slow (a network file system): written in a thread; the wizard closes when it is.
        self.set_write_state("writing")
        background(self.sai.write_project, main, document, owner=self, work=True, timeout=60,
                   done=lambda result, error: self.wrote(main, error))
        return False

    def wrote(self, main, error):
        if error is not None:
            self.set_write_state(None)  # Finish asks again
            warn(self, tr("Not written"), printable_text(error))
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

