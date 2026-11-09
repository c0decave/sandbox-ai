"""The Kits & plugins page: sandbox-ai's kits (its own, the default kit, built-in kits such as egm and yours),
what each brings per agent (skills, agents, commands, prompts, the memory template, Claude plugins and
marketplaces, hooks), and the marketplace browser: every plugin marketplace of the host (sandbox-ai's own, the
kits', the plugin store's, Claude's and Codex's) with its plugins and their state, and OpenCode's npm plugins.

Everything here runs `sandbox-ai kits ...` (read: `kits list --json`; change: new, add-skill, new-skill, add-plugin,
remove-plugin, enable-plugin, disable-plugin, add-marketplace, remove-marketplace, import-marketplace, add-hook,
remove-skill, remove-hook, delete), `sandbox-ai plugins marketplaces --json` (read only: nothing fetched or run, the
agents' own configuration only read, never written) or `sandbox-ai plugins claude [--marketplaces-only]` in a
terminal: the command's checks apply (no links followed, built-in kits read-only, folder marketplaces refused). The
plugins and marketplaces of the default kit change like those of a kit of yours. Using a kit in the current project
goes through the Board's Apply (shown, then written and trusted). What widens every later start with a kit is asked
first (kit-add), what narrows it too (kit-remove), naming the host and the kit.

A kit's hooks run only once approved. Approving is `sandbox-ai kits trust-hooks KIT` in a terminal: it shows every
hook and the full text of every file, and the human types yes there. The app never approves on its own (no
--yes); taking an approval back is `sandbox-ai kits trust-hooks --revoke KIT`.

The host chooser at the top shows and changes the kits of this machine or of a host of `sandbox-ai hosts` (over SSH:
`sandbox-ai hosts run HOST -- kits ...`; the review of hooks and the plugin installs in a terminal tab with ssh -tt).
A skill folder or a hook file is then a path on that host. A host whose sandbox-ai predates the marketplace browser
(0.1.78) is told to update (Hosts › Update); its kits are shown and changed as before.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from . import backend, theme
from .files import printable
from .host_choice import on_host
from .state import choose_file, choose_folder
from .i18n import N_, tr
from .widgets import (
    MessageText, Task, ask_first, background, busy, compact_combo, fill, flow, inform, outcome, plain_label, row_key,
    safe_tooltip, select_keys, selected_keys, selected_rows, set_enabled, show_text, table, warn,
)

LOCAL = "local"

KIND_TITLES = {"always": N_("always (sandbox-ai)"), "default": N_("always (default kit)"), "builtin": N_("built-in"),
               "user": N_("yours")}
ENTRY_TITLES = (("skills", N_("Skills")), ("agents", N_("Agents")), ("commands", N_("Commands")),
                ("prompts", N_("Prompts")))
AGENTS = backend.AGENTS
NEW_KIT = "my-kit"  # the name proposed for a first kit of yours (a name, not a text)
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart")
# What a kit change does to every later start with that kit (webui.KIT_WIDENS, KIT_NARROWS): widens it (asked first:
# kit-add) or narrows it (asked first: kit-remove). A new skill is sandbox-ai's own template text, written as the web
# writes it, without a question (review 6).
WIDENS = ("add-skill", "add-plugin", "add-marketplace", "add-hook", "enable-plugin", "import-marketplace")
NARROWS = ("remove-skill", "remove-plugin", "remove-hook", "disable-plugin", "remove-marketplace")
# Where a marketplace was found (`plugins marketplaces --json` "origins"), as the web's Kits page says it.
MARKET_ORIGINS = {"sandbox-ai": N_("sandbox-ai's own"), "kit": N_("a kit"), "store": N_("plugin store"),
                  "claude": N_("Claude on that host"), "codex": N_("Codex on that host"),
                  "codex-curated": N_("Codex (curated)"), "suggested": N_("suggested")}
OPENCODE = "opencode:"  # the marketplace table's row of OpenCode's npm plugins (OpenCode has no marketplaces)


def names(values):
    return [str(v) for v in values] if isinstance(values, list) else []


def kit_agents(kit):
    agents = kit.get("agents")
    return {name: info for name, info in agents.items() if isinstance(info, dict)} if isinstance(agents, dict) else {}


def hooks_of(info):
    """The kit's hooks for one agent (the inventory's "hooks"), or None."""
    hooks = info.get("hooks")
    return hooks if isinstance(hooks, dict) else None


def hook_text(hook):
    """One hook for display: an OpenCode plugin module, or event, matcher and command."""
    if not isinstance(hook, dict):
        return printable(hook)
    if hook.get("plugin"):
        return tr("plugin {name}", name=printable(hook["plugin"]))
    matcher = hook.get("matcher") or tr("every tool")
    return printable(f"{hook.get('event', '?')} · {matcher}: {hook.get('command', '')}")


def hook_files(info):
    """The files of a kit's hooks folder that remove-hook takes: those at its top, not hooks.json."""
    hooks = hooks_of(info) or {}
    files = hooks.get("files") if isinstance(hooks.get("files"), list) else []
    return [f["path"] for f in files if isinstance(f, dict) and isinstance(f.get("path"), str)
            and "/" not in f["path"] and f["path"] != "hooks.json"]


def unapproved(kit):
    """The agents whose hooks in KIT are there but not approved (errors or not)."""
    return sorted(agent for agent, info in kit_agents(kit).items()
                  if hooks_of(info) and not hooks_of(info).get("trusted"))


def source_text(source):
    """A marketplace source for display: OWNER/REPO, a URL, or sandbox-ai's own folder."""
    if not isinstance(source, dict):
        return printable(source)
    value = source.get("repo") or source.get("url") or source.get("path") or ""
    return printable(f"{source.get('source', '?')}: {value}")


class AgentsBox(QWidget):
    """One check box per agent."""

    def __init__(self, agents, checked=None):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.boxes = {}
        for agent in agents:
            box = QCheckBox(agent)
            box.setChecked(checked is None or agent in checked)
            row.addWidget(box)
            self.boxes[agent] = box
        row.addStretch()

    def chosen(self):
        return [agent for agent, box in self.boxes.items() if box.isChecked()]


class FormDialog(QDialog):
    """A small form with OK and Cancel."""

    def __init__(self, parent, title, note=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.form = QFormLayout(self)
        if note:
            self.form.addRow(plain_label(note, role="secondary"))
        self.setMinimumWidth(480)

    def finish(self):
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.form.addRow(buttons)


class NewKitDialog(FormDialog):
    def __init__(self, parent):
        super().__init__(parent, tr("New kit"), tr("A kit is a folder per agent with skills, agents, commands, prompts, "
                                                   "a memory template and plugin declarations; a project uses it with "
                                                   "--kit NAME."))
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("my-kit"))
        self.agents = AgentsBox(AGENTS)
        self.form.addRow(tr("Name"), self.name)
        self.form.addRow(tr("For"), self.agents)
        self.finish()


class NewSkillDialog(FormDialog):
    def __init__(self, parent, agents):
        super().__init__(parent, tr("New skill"), tr("Writes SKILL.md from a template; describe when the agent should "
                                                     "use it, then fill it in its folder."))
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("review-checklist"))
        self.description = QLineEdit()
        self.agents = AgentsBox(agents)
        self.form.addRow(tr("Name (a-z, 0-9, -)"), self.name)
        self.form.addRow(tr("Description"), self.description)
        self.form.addRow(tr("For"), self.agents)
        self.finish()


class PluginDialog(FormDialog):
    def __init__(self, parent, known):
        super().__init__(parent, tr("Add plugin"), tr("Claude: NAME@MARKETPLACE (a plugin of sandbox-ai's own "
                                                      "marketplace needs no source); OpenCode: an npm package name."))
        self.plugin = QComboBox()
        self.plugin.setEditable(True)
        self.plugin.addItems(known)
        self.agent = QComboBox()
        self.agent.addItems(["claude", "opencode"])
        self.source = QLineEdit()
        self.source.setPlaceholderText(tr("only for a new marketplace: OWNER/REPO or https://…"))
        self.form.addRow(tr("Plugin"), self.plugin)
        self.form.addRow(tr("Agent"), self.agent)
        self.form.addRow(tr("Marketplace source"), self.source)
        self.finish()


class MarketplaceDialog(FormDialog):
    def __init__(self, parent):
        super().__init__(parent, tr("Add marketplace"), tr("A Claude plugin marketplace from GitHub (OWNER/REPO) or a "
                                                           "Git server (https://…). Sandboxes get only sandbox-ai's "
                                                           "own marketplace as a folder; sandbox-ai refuses others "
                                                           "and says why."))
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("my-marketplace"))
        # No folder: sandbox-ai kits refuses one for every marketplace but its own (review 6 N2)
        self.source = QLineEdit()
        self.source.setPlaceholderText(tr("OWNER/REPO or https://git.example.org/plugins.git"))
        self.form.addRow(tr("Name"), self.name)
        self.form.addRow(tr("Source"), self.source)
        self.finish()


class HookDialog(FormDialog):
    """A hook for the kit: a script (with event, matcher and timeout), a hooks.json or an OpenCode plugin."""

    def __init__(self, parent, agents, remote=False):
        super().__init__(parent, tr("Add hook"), tr("A script runs at the event for the tools the matcher names; a "
                                                    "hooks.json brings its own definitions; an OpenCode plugin is a "
                                                    ".js, .mjs or .ts module. The hook runs only once you have "
                                                    "reviewed and approved it."))
        self.file = QLineEdit()
        self.file.setPlaceholderText(tr("check.sh, hooks.json or plugin.js"))
        browse = QPushButton(tr("File…"))
        browse.clicked.connect(self.choose)
        browse.setVisible(not remote)  # another host's file: its path there, typed
        if remote:
            self.file.setToolTip(tr("A path on that host"))
        row = QHBoxLayout()
        row.addWidget(self.file, 1)
        row.addWidget(browse)
        self.agents = AgentsBox(agents)
        self.event = QComboBox()
        self.event.setEditable(True)
        self.event.addItems(HOOK_EVENTS)
        self.matcher = QLineEdit()
        self.matcher.setPlaceholderText(tr("every tool; e.g. Bash or Write|Edit"))
        self.form.addRow(tr("File"), row)
        self.form.addRow(tr("For"), self.agents)
        self.form.addRow(tr("Event (a script)"), self.event)
        self.form.addRow(tr("Matcher (a script)"), self.matcher)
        self.finish()

    def choose(self):
        path = choose_file(self, tr("Hook file"), "hook")
        if path:
            self.file.setText(path)

    def options(self):
        """The options of `sandbox-ai kits add-hook` for what the form says."""
        options = [f"--agent={agent}" for agent in self.agents.chosen()]
        name = self.file.text().strip().rsplit("/", 1)[-1]
        if not name.endswith((".json", ".js", ".mjs", ".ts")):  # a script: when and for which tools
            event, matcher = self.event.currentText().strip(), self.matcher.text().strip()
            options += [f"--event={event}"] if event and event != "PreToolUse" else []
            options += [f"--matcher={matcher}"] if matcher else []
        return options


class RemoveDialog(FormDialog):
    """Choose a skill, plugin or hook file of a kit to remove."""

    def __init__(self, parent, entries):
        super().__init__(parent, tr("Remove from the kit"))
        self.list = QListWidget()
        for kind, agent, name in entries:
            text = (tr("skill {name} ({agent})", name=printable(name), agent=agent) if kind == "skill" else
                    tr("hook {name} ({agent})", name=printable(name), agent=agent) if kind == "hook" else
                    tr("plugin {name} ({agent})", name=printable(name), agent=agent))
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, (kind, agent, name))
            self.list.addItem(item)
        self.form.addRow(self.list)
        self.finish()

    def chosen(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class TargetKitDialog(FormDialog):
    """Which kit of yours receives what is added when the selected kit is not one of yours (sandbox-ai's own
    kits are read-only): one of yours, or a new kit created first (sandbox-ai kits new)."""

    def __init__(self, parent, why, kits):
        super().__init__(parent, tr("Add to a kit of yours"), why)
        self.kit = QComboBox()
        for name in kits:
            self.kit.addItem(printable(name), name)
        self.kit.addItem(tr("A new kit…"), "")
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("my-kit"))
        if not kits:
            self.name.setText(NEW_KIT)
        self.form.addRow(tr("Kit"), self.kit)
        self.form.addRow(tr("New kit's name"), self.name)
        self.kit.currentIndexChanged.connect(lambda _=0: self.changed())
        self.changed()
        self.finish()

    def changed(self):
        self.name.setEnabled(self.kit.currentData() == "")

    def chosen(self):
        """(name, whether it is new)."""
        current = self.kit.currentData()
        return (current, False) if current else (self.name.text().strip(), True)


def in_kit(entries, kit):
    """The entry of KIT (a kit of `plugins marketplaces --json`: its name and kind) among ENTRIES (the "kits" of a
    marketplace or a plugin), or None."""
    if not kit or not isinstance(entries, list):
        return None
    return next((e for e in entries if isinstance(e, dict) and e.get("kit") == kit.get("name")
                 and e.get("kind") == kit.get("kind")), None)


def plugin_state(plugin):
    """A plugin's state for display, as the web's Kits page says it: on or off in which kits, installed in the plugin
    store, installed or enabled in the agent's own configuration."""
    words = []
    for entry in plugin.get("kits") if isinstance(plugin.get("kits"), list) else []:
        if isinstance(entry, dict):
            kit = printable(entry.get("kit") or "?")
            words.append(tr("on in {kit}", kit=kit) if entry.get("enabled") else tr("off in {kit}", kit=kit))
    store = plugin.get("store") if isinstance(plugin.get("store"), dict) else {}
    if store.get("installed"):
        words.append(tr("installed ({version})", version=printable(store["version"])) if store.get("version")
                     else tr("installed"))
    agent = plugin.get("agent") if isinstance(plugin.get("agent"), dict) else {}
    if agent.get("installed"):
        words.append(tr("the agent has it installed"))
    if agent.get("enabled") is True:
        words.append(tr("the agent enables it"))
    elif agent.get("enabled") is False:
        words.append(tr("the agent has it off"))
    if agent.get("configured"):
        words.append(tr("in opencode.json"))
    return " · ".join(words)


def plugin_key(plugin):
    """What a plugin is named by in a kit: NAME@MARKETPLACE (Claude) or the npm package (OpenCode)."""
    value = plugin.get("id") if isinstance(plugin.get("id"), str) else plugin.get("name")
    return value if isinstance(value, str) and value else None


class KitsPage(QWidget):
    enable_kit = Signal(str)  # use this kit in the current project (the window passes it to the Board)
    inventory_loaded = Signal(object)  # the inventory, for the Board's kits node

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.inventory, self.kits, self.project, self.project_kits, self.token = None, {}, None, [], 0
        self.installed, self.kits_host = set(), None  # kits_host: whose kits the rows are
        # the marketplace browser: `plugins marketplaces --json` of market_host (None: nothing read)
        self.market_data, self.market_host, self.market_token, self.market_old = None, None, 0, False
        self.markets, self.market_kits = {}, {}
        layout = QVBoxLayout(self)
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The kits of this machine, or of a host (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        layout.addWidget(self.hosts)
        top = QHBoxLayout()
        self.summary = plain_label(role="secondary")
        top.addWidget(self.summary, 1)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        new = QPushButton(tr("New kit…"))
        new.setToolTip(tr("sandbox-ai kits new: a kit of your own in ~/.local/share/sandbox-ai/kits"))
        new.clicked.connect(lambda _=False: self.new_kit())
        self.install_button = QPushButton(tr("Install plugins"))
        self.install_button.setToolTip(tr("sandbox-ai plugins claude, in a terminal: installs what the kits declare "
                                          "into the plugin store (with --kit for the built-in kits this project uses)"))
        self.install_button.clicked.connect(lambda _=False: self.install(False))
        self.update_button = QPushButton(tr("Update plugins"))
        self.update_button.setToolTip(tr("sandbox-ai plugins claude --update, in a terminal"))
        self.update_button.clicked.connect(lambda _=False: self.install(True))
        self.status_button = QPushButton(tr("Plugin status"))
        self.status_button.setToolTip(tr("sandbox-ai plugins --status: the plugins declared and installed"))
        self.status_button.clicked.connect(lambda _=False: self.plugin_status())
        for widget in (self.refresh_button, new, self.install_button, self.update_button, self.status_button):
            top.addWidget(widget)
        layout.addLayout(top)
        # A kit change runs in the background (cloning a marketplace takes a while): a busy bar with what runs and
        # Cancel, what sandbox-ai writes as it comes, then what came of it in the message area (wrapped, plain text).
        self.task = Task(output=True)
        self.task.setAccessibleName(tr("Kit change running"))
        layout.addWidget(self.task)
        self.message = MessageText()
        self.message.setAccessibleName(tr("Message"))
        layout.addWidget(self.message)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName(tr("Kits & plugins"))
        self.tabs.addTab(self.kits_tab(), tr("Kits"))
        self.tabs.addTab(self.market_tab(), tr("Marketplaces"))
        layout.addWidget(self.tabs, 1)
        self.loaded_once = False
        self.update_buttons()
        self.update_market_buttons()

    def kits_tab(self):
        """The kits (a table) and what the one selected brings (a tree), with what changes them."""
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        split = self.split = QSplitter(Qt.Orientation.Horizontal)
        kits_box = QGroupBox(tr("Kits"))
        kits_layout = QVBoxLayout(kits_box)
        self.table = table([tr("Kit"), tr("Kind"), tr("Agents"), tr("Brings"), tr("This project")], stretch=3,
                           compact=(1, 2, 4))
        self.table.setAccessibleName(tr("Kits"))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        kits_layout.addWidget(self.table, 1)
        self.use_button = QPushButton(tr("Use in the current project…"))
        self.use_button.setProperty("role", "primary")
        self.use_button.clicked.connect(lambda _=False: self.use())
        self.skill_button = QPushButton(tr("Add skill from a folder…"))
        self.skill_button.clicked.connect(lambda _=False: self.add_skill())
        self.new_skill_button = QPushButton(tr("New skill…"))
        self.new_skill_button.clicked.connect(lambda _=False: self.new_skill())
        self.plugin_button = QPushButton(tr("Add plugin…"))
        self.plugin_button.clicked.connect(lambda _=False: self.add_plugin())
        self.marketplace_button = QPushButton(tr("Add marketplace…"))
        self.marketplace_button.clicked.connect(lambda _=False: self.add_marketplace())
        self.hook_button = QPushButton(tr("Add hook…"))
        self.hook_button.setToolTip(tr("sandbox-ai kits add-hook: a hook script, a hooks.json or an OpenCode plugin; "
                                       "it runs once approved"))
        self.hook_button.clicked.connect(lambda _=False: self.add_hook())
        self.trust_button = QPushButton(tr("Review and approve hooks…"))
        self.trust_button.clicked.connect(lambda _=False: self.trust_hooks())
        self.revoke_button = QPushButton(tr("Take the approval back"))
        self.revoke_button.setToolTip(tr("sandbox-ai kits trust-hooks --revoke: the kit's hooks no longer run"))
        self.revoke_button.clicked.connect(lambda _=False: self.revoke_hooks())
        self.remove_button = QPushButton(tr("Remove…"))
        self.remove_button.clicked.connect(lambda _=False: self.remove())
        self.delete_button = QPushButton(tr("Delete kit…"))
        self.delete_button.setProperty("role", "danger")
        self.delete_button.clicked.connect(lambda _=False: self.delete())
        # Adding works whatever kit is selected: into the selected kit when it is yours (a plugin or marketplace into
        # the default kit too), else into a kit of yours chosen (or created) first. Removing is for your own kits and
        # the default kit's plugins; deleting for your own kits only.
        self.add_buttons = [self.skill_button, self.new_skill_button, self.plugin_button, self.marketplace_button,
                            self.hook_button]
        self.change_buttons = [self.remove_button, self.delete_button]
        self.edit_buttons = self.add_buttons + self.change_buttons
        kits_layout.addWidget(flow(self.use_button, *self.edit_buttons, self.trust_button, self.revoke_button))
        self.add_hint = plain_label(role="secondary")
        self.add_hint.setWordWrap(True)
        kits_layout.addWidget(self.add_hint)
        details_box = QGroupBox(tr("What it brings"))
        details_layout = QVBoxLayout(details_box)
        self.where = plain_label(role="secondary")
        self.where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName(tr("What it brings"))
        details_layout.addWidget(self.where)
        details_layout.addWidget(self.tree, 1)
        split.addWidget(kits_box)
        split.addWidget(details_box)
        split.setSizes([700, 340])
        holder_layout.addWidget(split, 1)
        return holder

    def market_tab(self):
        """The marketplace browser: a kit chosen (the default kit, one of yours; the built-in ones read-only), every
        marketplace of the host, the plugins of the one selected with their state, and what changes the kit."""
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        kit_label = plain_label(tr("Kit"), word_wrap=False)
        self.market_kit = compact_combo(QComboBox(), 24)
        self.market_kit.setAccessibleName(tr("The kit the marketplace actions change"))
        self.market_kit.setToolTip(tr("What you add, enable, disable or remove below changes this kit: the default "
                                      "kit (every sandbox) or one of yours; sandbox-ai's built-in kits are read-only"))
        kit_label.setBuddy(self.market_kit)
        self.market_kit.currentIndexChanged.connect(lambda _=0: self.market_kit_changed())
        row.addWidget(kit_label)
        row.addWidget(self.market_kit, 1)
        holder_layout.addLayout(row)
        self.fetch_button = QPushButton(tr("Fetch the plugin lists"))
        self.fetch_button.clicked.connect(lambda _=False: self.fetch_lists())
        self.market_new_button = QPushButton(tr("Add marketplace…"))
        self.market_new_button.setAccessibleName(tr("Add a marketplace to the kit by its source"))
        self.market_new_button.clicked.connect(lambda _=False: self.new_marketplace())
        holder_layout.addWidget(flow(self.fetch_button, self.market_new_button))
        self.market_hint = plain_label(role="secondary")
        holder_layout.addWidget(self.market_hint)
        self.market_status = plain_label()
        holder_layout.addWidget(self.market_status)
        split = self.market_split = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.market = table([tr("Marketplace"), tr("Agent"), tr("From"), tr("Plugins"), tr("Source")], stretch=2,
                            compact=(1, 3))
        self.market.setAccessibleName(tr("Marketplaces"))
        self.market.setMinimumHeight(90)
        self.market.itemSelectionChanged.connect(self.market_selected)
        top_layout.addWidget(self.market, 1)
        self.market_add_button = QPushButton(tr("Add to the kit"))
        self.market_add_button.setAccessibleName(tr("Add the marketplace to the kit"))
        self.market_add_button.clicked.connect(lambda _=False: self.add_market())
        self.import_button = QPushButton(tr("Import with Claude's plugins"))
        self.import_button.clicked.connect(lambda _=False: self.import_market())
        self.market_remove_button = QPushButton(tr("Remove from the kit…"))
        self.market_remove_button.setAccessibleName(tr("Remove the marketplace from the kit"))
        self.market_remove_button.setProperty("role", "danger")
        self.market_remove_button.clicked.connect(lambda _=False: self.remove_market())
        self.with_plugins = QCheckBox(tr("With its plugins"))
        self.with_plugins.setChecked(True)
        self.with_plugins.setToolTip(tr("--with-plugins: the kit's plugins of that marketplace go too (else they must "
                                        "be removed first)"))
        top_layout.addWidget(flow(self.market_add_button, self.import_button, self.market_remove_button,
                                  self.with_plugins))
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        self.plugins_title = plain_label(role="section")
        self.plugins_note = plain_label(role="secondary")
        self.plugins_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter by name or description"))
        self.filter.setAccessibleName(tr("Filter the plugins"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda _text: self.show_plugins())
        self.plugins = table([tr("Plugin"), tr("This kit"), tr("State"), tr("Description")], stretch=3,
                             compact=(1, 2))
        self.plugins.setAccessibleName(tr("Plugins of the marketplace"))
        self.plugins.setMinimumHeight(90)
        self.plugins.itemSelectionChanged.connect(self.update_market_buttons)
        self.plugin_add_button = QPushButton(tr("Add to the kit"))
        self.plugin_add_button.setAccessibleName(tr("Add the plugin to the kit"))
        self.plugin_add_button.setProperty("role", "primary")
        self.plugin_add_button.clicked.connect(lambda _=False: self.plugin_action("add-plugin"))
        self.enable_button = QPushButton(tr("Enable"))
        self.enable_button.setAccessibleName(tr("Enable the plugin in the kit"))
        self.enable_button.clicked.connect(lambda _=False: self.plugin_action("enable-plugin"))
        self.disable_button = QPushButton(tr("Disable"))
        self.disable_button.setAccessibleName(tr("Disable the plugin in the kit"))
        self.disable_button.clicked.connect(lambda _=False: self.plugin_action("disable-plugin"))
        self.plugin_remove_button = QPushButton(tr("Remove"))
        self.plugin_remove_button.setAccessibleName(tr("Remove the plugin from the kit"))
        self.plugin_remove_button.setProperty("role", "danger")
        self.plugin_remove_button.clicked.connect(lambda _=False: self.plugin_action("remove-plugin"))
        self.plugin_buttons = [self.plugin_add_button, self.enable_button, self.disable_button,
                               self.plugin_remove_button]
        self.market_buttons = [self.market_add_button, self.import_button, self.market_remove_button,
                               self.market_new_button]
        for widget in (self.plugins_title, self.plugins_note, self.filter):
            bottom_layout.addWidget(widget)
        bottom_layout.addWidget(self.plugins, 1)
        bottom_layout.addWidget(flow(*self.plugin_buttons))
        split.addWidget(top)
        split.addWidget(bottom)
        split.setSizes([220, 300])
        holder_layout.addWidget(split, 1)
        self.agent_notes = plain_label(role="secondary")
        self.agent_notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        holder_layout.addWidget(self.agent_notes)
        return holder

    # -- reading --

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once:
            self.refresh()

    def host(self):
        return self.hosts.host() or LOCAL

    def on_host(self, title):
        """TITLE with the host it runs on (for a terminal tab and a result)."""
        return title if self.host() == LOCAL else f"{title} ({self.host()})"

    def host_changed(self):
        """Another host: the rows of the one before go at once (an action never names a kit of one host on another),
        and a late answer of that host is dropped."""
        self.token += 1
        self.market_token += 1
        self.clear_rows()
        self.clear_markets()
        if self.loaded_once:
            self.refresh()

    def clear_rows(self):
        self.inventory, self.kits, self.kits_host = None, {}, None
        self.installed = set()
        fill(self.table, [])
        self.tree.clear()
        self.selection_changed()

    def refresh(self):
        """`sandbox-ai kits list --json` and `sandbox-ai plugins marketplaces --json`, each in the background (on the
        host chosen)."""
        self.loaded_once = True
        self.refresh_markets()
        host = self.host()
        if host != LOCAL:
            self.token += 1
            token = self.token
            self.summary.setText(tr("Asking {host}…", host=self.hosts.label(host)))
            background(lambda: self.sai.host_kits_inventory(host), owner=self, source=self.refresh_button,
                       timeout=150, done=lambda result, error: self.loaded(token, result, error, host))
            return
        if not self.sai.has_kits_command():
            self.summary.setText(tr("This sandbox-ai has no kits command (sandbox-ai kits); update it with: "
                                    "sandbox-ai update"))
            self.inventory_loaded.emit({})
            return
        self.token += 1
        token = self.token
        self.summary.setText(tr("Reading the kits…"))
        background(self.sai.kits_inventory, owner=self, source=self.refresh_button, timeout=90,
                   done=lambda result, error: self.loaded(token, result, error, LOCAL))

    def loaded(self, token, result, error, host=LOCAL):
        if token != self.token or host != self.host():
            return  # asked for another host (or before another refresh): never shown under this one
        if error is not None:
            self.clear_rows()  # this host's kits are not known: none of another's stay
            self.summary.setText(tr("Cannot list the kits: {error}", error=printable(error)))
            self.update_buttons()
            return
        self.show_inventory(result, host)
        self.update_market_buttons()

    def show_inventory(self, inventory, host=None):
        self.inventory = inventory
        self.kits_host = host or self.kits_host or self.host()  # whose kits the rows are
        kits = [kit for kit in inventory.get("kits", []) if isinstance(kit, dict) and isinstance(kit.get("name"), str)]
        self.kits = {f"{kit.get('kind')}:{kit['name']}": kit for kit in kits}
        installed = inventory.get("installed") if isinstance(inventory.get("installed"), dict) else {}
        self.installed = set(names(installed.get("plugins")))
        rows, colors, keys = [], [], []
        for key, kit in self.kits.items():
            agents = kit_agents(kit)
            skills = sorted({s for info in agents.values() for s in names(info.get("skills"))})
            plugins = sorted({p for info in agents.values() for p in names(info.get("plugins"))})
            off = sorted({p for info in agents.values() for p in names(info.get("disabled"))})
            problem = any(info.get("error") for info in agents.values())
            replaced = kit.get("kind") == "builtin" and set(agents) <= set(names(kit.get("replaced")))
            brings = [tr("skills: {list}", list=", ".join(skills))] if skills else []
            brings += [tr("plugins: {list}", list=", ".join(plugins))] if plugins else []
            brings += [tr("off: {list}", list=", ".join(off))] if off else []
            hooks = sum(len(names(hooks_of(info).get("hooks"))) or 1 for info in agents.values() if hooks_of(info))
            waiting = unapproved(kit)
            brings += [tr("hooks: {count} (not approved)", count=hooks) if waiting else
                       tr("hooks: {count}", count=hooks)] if hooks else []
            rows.append([printable(kit["name"]), tr(KIND_TITLES.get(kit.get("kind"), str(kit.get("kind")))),
                         ", ".join(sorted(agents)), printable(" · ".join(brings)) or "–", self.use_text(kit)])
            colors.append("bad" if problem else "muted" if replaced else "warn" if waiting else None)
            keys.append(key)
        fill(self.table, rows, colors, keys=keys)
        store = printable(inventory.get("store") or "")
        listed = ", ".join(sorted(self.installed)) or tr("none")
        self.store_text = tr("Plugin store {path}: {list}", path=store, list=printable(listed))
        user = sum(1 for kit in self.kits.values() if kit.get("kind") == "user")
        self.summary.setText(tr("{count} kits ({user} of yours) in {path}", count=len(self.kits), user=user,
                                path=printable(inventory.get("kits_dir") or "")) +
                             ("" if self.host() == LOCAL else f" · {self.hosts.label()}"))
        self.summary.setToolTip(safe_tooltip(self.store_text))
        self.inventory_loaded.emit(inventory)
        self.selection_changed()

    def use_text(self, kit):
        if not self.project or self.host() != LOCAL:
            return ""
        return "✔" if kit.get("kind") in ("always", "default") or kit["name"] in self.project_kits else ""

    def set_project(self, path):
        self.project = path
        self.project_kits = self.sai.project_kits(path) if path else []
        if self.inventory is not None:
            self.show_inventory(self.inventory)

    # -- selection --

    def current(self):
        rows = selected_rows(self.table)
        return self.kits.get(row_key(self.table, rows[0])) if rows else None

    def selection_changed(self):
        kit = self.current()
        self.tree.clear()
        if kit is None:
            self.where.setText(tr("Select a kit to see what it brings."))
            self.update_buttons()
            return
        key = f"{kit.get('kind')}:{kit['name']}"  # the marketplace browser follows (when it offers that kit)
        if self.market_kit.findData(key) >= 0:
            self.market_kit.setCurrentIndex(self.market_kit.findData(key))
        lines = [printable(kit.get("path") or "")]
        if kit.get("description"):
            lines.insert(0, printable(kit["description"]))
        if kit.get("replaced"):
            lines.append(tr("Replaced by your kit of the same name for: {list}",
                            list=", ".join(names(kit.get("replaced")))))
        self.where.setText("\n".join(lines))
        for agent, info in sorted(kit_agents(kit).items()):
            top = QTreeWidgetItem([agent])
            for key, title in ENTRY_TITLES:
                values = names(info.get(key))
                if values:
                    child = QTreeWidgetItem([f"{tr(title)}: {len(values)}"])
                    for value in values:
                        child.addChild(QTreeWidgetItem([printable(value)]))
                    top.addChild(child)
            if info.get("memory"):
                top.addChild(QTreeWidgetItem([tr("Memory template: {name}", name=printable(info["memory"]))]))
            markets = info.get("marketplaces") if isinstance(info.get("marketplaces"), dict) else {}
            if markets:
                child = QTreeWidgetItem([tr("Marketplaces: {count}", count=len(markets))])
                for name, source in sorted(markets.items()):
                    child.addChild(QTreeWidgetItem([f"{printable(name)}  ({source_text(source)})"]))
                top.addChild(child)
            plugins, off = names(info.get("plugins")), names(info.get("disabled"))
            if plugins or off:
                child = QTreeWidgetItem([tr("Plugins: {count}", count=len(plugins) + len(off))])
                for plugin in plugins:
                    state = tr("installed") if plugin in self.installed or agent != "claude" else tr("not installed")
                    child.addChild(QTreeWidgetItem([f"{printable(plugin)}  ({state})"]))
                for plugin in off:
                    child.addChild(QTreeWidgetItem([f"{printable(plugin)}  {tr('(off)')}"]))
                top.addChild(child)
            hooks = hooks_of(info)
            if hooks:
                listed = hooks.get("hooks") if isinstance(hooks.get("hooks"), list) else []
                state = (tr("cannot run") if hooks.get("errors") else tr("approved") if hooks.get("trusted") else
                         tr("not approved: they do not run"))
                child = QTreeWidgetItem([tr("Hooks: {count} ({state})", count=len(listed), state=state)])
                for hook in listed:
                    child.addChild(QTreeWidgetItem([hook_text(hook)]))
                for error in names(hooks.get("errors")):
                    child.addChild(QTreeWidgetItem(["✘ " + printable(error)]))
                for note in names(hooks.get("notes")):
                    child.addChild(QTreeWidgetItem(["! " + printable(note)]))
                top.addChild(child)
            for note in names(info.get("notes")):
                top.addChild(QTreeWidgetItem(["! " + printable(note)]))
            if info.get("error"):
                top.addChild(QTreeWidgetItem(["✘ " + printable(info["error"])]))
            if top.childCount() == 0:
                top.addChild(QTreeWidgetItem([tr("(empty)")]))
            self.tree.addTopLevelItem(top)
        self.tree.expandToDepth(1)
        self.update_buttons()

    def update_buttons(self):
        if busy(self):
            return
        kit = self.current()
        kind = kit.get("kind") if kit else None
        none = tr("Select a kit first.")
        folder = kit.get("path") or "" if kit else ""
        read_only = tr("sandbox-ai's own kits are read-only: create a kit of your own (New kit…).")
        remove_why = (none if kit is None else None if kind in ("user", "default") else read_only)
        delete_why = (none if kit is None else None if kind == "user" else
                      tr("The default kit is part of every sandbox: it is not deleted; its skills are edited in its "
                         "folder: {path}", path=printable(folder)) if kind == "default" else read_only)
        set_enabled(self.remove_button, remove_why is None, remove_why,
                    tooltip=tr("Take a skill, plugin or hook out of the kit (of the default kit: a plugin)"))
        set_enabled(self.delete_button, delete_why is None, delete_why)
        for button in self.add_buttons:
            set_enabled(button, True)
        self.add_hint.setText(
            "" if kit is None or kind == "user" else
            tr("The default kit's plugins and marketplaces change here; its skills and hooks are edited in its "
               "folder: {path}. A skill or hook you add goes to a kit of yours: you choose it (or a new one) first.",
               path=printable(folder)) if kind == "default" else
            tr("{why} What you add goes to a kit of yours: you choose it (or a new one) first.", why=read_only))
        use_why = (none if kit is None else
                   tr("The current project is on this machine: choose this machine as the host.")
                   if self.host() != LOCAL else
                   tr("This kit is part of every sandbox already.") if kind in ("always", "default") else
                   tr("Choose a project first (at the top of the window).") if not self.project else
                   tr("The current project uses it already.") if kit["name"] in self.project_kits else None)
        set_enabled(self.use_button, use_why is None, use_why,
                    tooltip=tr("Add --kit NAME to the project's .sandbox-ai.json: shown first, then written and "
                               "trusted"))
        if self.host() == LOCAL and not self.sai.has_kit_hooks():
            set_enabled(self.hook_button, False, tr("This sandbox-ai has no kit hooks yet; update it."))
        with_hooks = kit is not None and any(hooks_of(info) for info in kit_agents(kit).values())
        trust_why = (none if kit is None else
                     tr("The kit has no hooks.") if not with_hooks else
                     tr("sandbox-ai's own skill has no hooks to approve.") if kind == "always" else None)
        set_enabled(self.trust_button, trust_why is None, trust_why,
                    tooltip=tr("sandbox-ai kits trust-hooks, in a terminal: shows every hook and the full text of "
                               "every file; you approve there"))
        approved = kit is not None and any(hooks_of(info) and hooks_of(info).get("trusted")
                                           for info in kit_agents(kit).values())
        set_enabled(self.revoke_button, trust_why is None and approved,
                    trust_why or tr("No hook of this kit is approved."),
                    tooltip=tr("sandbox-ai kits trust-hooks --revoke: the kit's hooks no longer run"))

    # -- the marketplace browser: reading --

    def clear_markets(self, status=""):
        self.market_data, self.market_host, self.markets, self.market_kits = None, None, {}, {}
        self.market_kit.blockSignals(True)
        self.market_kit.clear()
        self.market_kit.blockSignals(False)
        fill(self.market, [])
        self.market_status.setText(status)
        self.market_hint.setText("")
        self.agent_notes.setText("")
        self.agent_notes.setVisible(False)
        self.show_plugins()

    def refresh_markets(self):
        """`sandbox-ai plugins marketplaces --json` on the host chosen, in the background (a late answer for another
        host, or one asked before another refresh, is dropped). A host whose sandbox-ai is older is told to update."""
        host = self.host()
        self.market_token += 1
        token = self.market_token
        if host == LOCAL and not self.sai.has_marketplaces():
            self.market_old = True
            self.clear_markets(tr("This sandbox-ai has no marketplace browser yet (it came with 0.1.78); update it "
                                  "with: sandbox-ai update"))
            self.update_market_buttons()
            return
        self.market_old = False
        self.market_status.setText(tr("Reading the marketplaces of {host}…", host=self.hosts.label(host)))
        # (its owner is the browser, not the page: a read never holds back the kits' buttons, nor they it)
        background(lambda: self.sai.host_marketplaces(host), owner=self.market_split, timeout=150,
                   done=lambda result, error: self.markets_loaded(token, host, result, error))

    def markets_loaded(self, token, host, result, error):
        if token != self.market_token or host != self.host():
            return  # asked for another host (or before another refresh): never shown under this one
        if isinstance(error, backend.HostTooOld):
            self.market_old = True
            self.clear_markets(tr("The sandbox-ai on {host} has no marketplace browser yet (it came with 0.1.78): "
                                  "update the host (Hosts › Update). Its kits on the tab Kits work as before.",
                                  host=self.hosts.label(host)))
        elif error is not None:
            self.clear_markets(tr("Cannot list the marketplaces: {error}", error=printable(error)))
        else:
            self.show_markets(result, host)
        self.update_buttons()
        self.update_market_buttons()

    def show_markets(self, data, host):
        self.market_data, self.market_host, self.market_old = data, host, False
        label = self.hosts.label(host)
        self.market_hint.setText(tr(
            "Every plugin marketplace on {host}: sandbox-ai's own, those the kits declare, and those Claude, Codex and "
            "OpenCode already know there. Pick one to see its plugins; what you add, enable, disable or remove "
            "changes the kit chosen here. The agents' own configuration is only read.", host=label))
        # the kits the browser can change (the default kit, yours) and the built-in ones (shown, read-only)
        previous = self.market_kit.currentData()
        kits = [k for k in data.get("kits", []) if isinstance(k, dict) and isinstance(k.get("name"), str)
                and k.get("kind") in ("default", "builtin", "user")]
        self.market_kits = {f"{k['kind']}:{k['name']}": k for k in kits}
        self.market_kit.blockSignals(True)
        self.market_kit.clear()
        for key, kit in self.market_kits.items():
            text = (tr("the default kit (every sandbox)") if kit["kind"] == "default" else
                    printable(kit["name"]) if kit.get("editable") else
                    tr("{name} (built-in, read-only)", name=printable(kit["name"])))
            self.market_kit.addItem(text, key)
        compact_combo(self.market_kit, 24)
        chosen = previous if previous in self.market_kits else next(
            (key for key, kit in self.market_kits.items() if kit["kind"] == "user" and kit.get("editable")),
            next((key for key, kit in self.market_kits.items() if kit["kind"] == "default"), None))
        if chosen is not None:
            self.market_kit.setCurrentIndex(self.market_kit.findData(chosen))
        self.market_kit.blockSignals(False)
        markets = [m for m in data.get("marketplaces", []) if isinstance(m, dict) and isinstance(m.get("name"), str)
                   and m.get("agent") in ("claude", "codex")]
        self.markets = {f"{m['agent']}:{m['name']}": m for m in markets}
        agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
        opencode = agents.get("opencode") if isinstance(agents.get("opencode"), dict) else None
        if opencode is not None and isinstance(opencode.get("plugins"), list):
            self.markets[OPENCODE] = {"name": tr("OpenCode's npm plugins"), "agent": "opencode", "npm": True,
                                      "plugins": [p for p in opencode["plugins"] if isinstance(p, dict)],
                                      "modules": names(opencode.get("modules"))}
        count = sum(1 for key in self.markets if key != OPENCODE)
        errors = [e for e in data.get("errors", []) if isinstance(e, dict)] if isinstance(data.get("errors"),
                                                                                           list) else []
        # the table's OpenCode row is no marketplace: counted apart, so the count is the rows' (review 6)
        said = tr("1 marketplace on {host}", host=label) if count == 1 else \
            tr("{count} marketplaces on {host}", count=count, host=label)
        if OPENCODE in self.markets:
            said = tr("{marketplaces}, and OpenCode's npm plugins", marketplaces=said)
        self.market_status.setText(said)
        lines = []
        for agent, info in sorted(agents.items()):
            for note in names(info.get("notes")) if isinstance(info, dict) else []:
                lines.append(f"! {agent}: {printable(note)}")
        for error in errors:
            lines.append("✘ " + tr("kit {kit} ({agent}): {error}", kit=printable(error.get("kit") or "?"),
                                   agent=printable(error.get("agent") or "?"),
                                   error=printable(error.get("error") or "?")))
        self.agent_notes.setText("\n".join(lines))
        self.agent_notes.setVisible(bool(lines))
        self.show_market_rows()

    def about(self, agent):
        """The agent's fixed explanation of `plugins marketplaces --json` (what sandbox-ai does with its plugins)."""
        agents = (self.market_data or {}).get("agents")
        info = agents.get(agent) if isinstance(agents, dict) and isinstance(agents.get(agent), dict) else {}
        return printable(info["about"]) if isinstance(info.get("about"), str) else ""

    def show_market_rows(self):
        """The marketplace table for the kit chosen (which ones it declares)."""
        kit = self.chosen_kit()
        rows, colors, tips, keys = [], [], [], []
        for key, market in self.markets.items():
            plugins = market.get("plugins") if isinstance(market.get("plugins"), list) else []
            if key == OPENCODE:
                rows.append([market["name"], "opencode", "opencode.json", str(len(plugins)), "npm"])
                colors.append(None)
                tips.append(self.about("opencode"))
                keys.append(key)
                continue
            declared = in_kit(market.get("kits"), kit)
            origins = ", ".join(tr(MARKET_ORIGINS[o]) if o in MARKET_ORIGINS else printable(o)
                                for o in names(market.get("origins")))
            if declared:
                origins += " · " + tr("declared in {kit}", kit=self.kit_label(kit))
            count = str(len(plugins)) if market.get("listed_from") else tr("{count} (list not fetched)",
                                                                           count=len(plugins))
            rows.append([printable(market["name"]), printable(market.get("agent")), origins, count,
                         printable(market.get("source_value") or "?")])
            colors.append("ok" if declared else "muted" if market.get("agent") == "claude" and
                          not market.get("importable") else None)
            tip = [printable(market.get("description") or "")]
            if market.get("problem"):
                tip.append(tr("not for a kit: {why}", why=printable(market["problem"])))
            if market.get("agent") == "codex":
                tip.append(self.about("codex"))
            tips.append("\n".join(t for t in tip if t))
            keys.append(key)
        fill(self.market, rows, colors, tooltips=tips, keys=keys)
        if not selected_rows(self.market) and keys:
            select_keys(self.market, [keys[0]])
        self.market_selected()

    def chosen_kit(self):
        """The kit chosen in the browser (a kit of `plugins marketplaces --json`), or None."""
        return self.market_kits.get(self.market_kit.currentData()) if self.market_kit.count() else None

    def kit_label(self, kit):
        return tr("the default kit") if kit and kit.get("kind") == "default" else printable((kit or {}).get("name"))

    def market_kit_changed(self):
        if self.market_data is not None:
            self.show_market_rows()

    def picked(self):
        """The marketplace selected (or OpenCode's npm plugins), or None."""
        keys = selected_keys(self.market)
        return self.markets.get(keys[0]) if keys else None

    def picked_plugin(self):
        market, keys = self.picked(), selected_keys(self.plugins)
        if market is None or not keys:
            return None
        return next((p for p in market.get("plugins", []) if isinstance(p, dict) and plugin_key(p) == keys[0]), None)

    def market_selected(self):
        self.show_plugins()

    def show_plugins(self):
        """The plugins of the marketplace selected, with their state in the kit chosen and everywhere; the filter
        keeps those whose name or description holds its words."""
        market, kit = self.picked(), self.chosen_kit()
        notes = []
        if market is None:
            self.plugins_title.setText(tr("Select a marketplace to see its plugins.") if self.markets else "")
            fill(self.plugins, [])
            self.plugins_note.setText("")
            self.plugins_note.setVisible(False)
            self.update_market_buttons()
            return
        if market.get("npm"):
            self.plugins_title.setText(tr("OpenCode's npm plugins"))
            notes.append(self.about("opencode"))
            if market.get("modules"):
                notes.append(tr("OpenCode's plugin modules there (a kit takes one as a hook, below):") + " " +
                             ", ".join(printable(m) for m in market["modules"]))
        else:
            self.plugins_title.setText(tr("Plugins of {marketplace} ({agent})", marketplace=printable(market["name"]),
                                          agent=printable(market.get("agent"))))
            if market.get("agent") == "codex":
                notes.append(self.about("codex") or tr("sandbox-ai's kits give Codex no plugins yet: shown as Codex "
                                                       "has them on that host."))
            elif not in_kit(market.get("kits"), kit) and not market.get("importable"):
                notes.append(tr("Its plugins cannot go into a kit: {why}", why=printable(market.get("problem") or "?")))
            if not market.get("listed_from"):
                notes.append(tr("Its plugin list is not fetched yet: only the plugins a kit or the agent names are "
                                "shown."))
        notes = [n for n in notes if n]
        self.plugins_note.setText("\n".join(notes))
        self.plugins_note.setVisible(bool(notes))
        words = self.filter.text().lower().split()
        rows, colors, keys = [], [], []
        for plugin in market.get("plugins", []):
            key = plugin_key(plugin) if isinstance(plugin, dict) else None
            if key is None or key in keys:
                continue
            description = printable(plugin.get("description") or (
                tr("not in its plugin list (named by a kit or the agent)") if plugin.get("listed") is False else ""))
            if words and not all(w in f"{key} {description}".lower() for w in words):
                continue
            mine = in_kit(plugin.get("kits"), kit)
            name = printable(plugin.get("name") or key) + (f" {printable(plugin['version'])}"
                                                           if plugin.get("version") else "")
            rows.append([name, (tr("on") if mine.get("enabled") else tr("off")) if mine else "–",
                         plugin_state(plugin) or tr("in no kit"), description])
            colors.append(("ok" if mine.get("enabled") else "muted") if mine else None)
            keys.append(key)
        fill(self.plugins, rows, colors, keys=keys)
        self.update_market_buttons()

    # -- the marketplace browser: what may be done, and why not --

    def kit_why(self, kit=None):
        """Why the kit chosen in the browser cannot be changed here, or None."""
        kit = kit or self.chosen_kit()
        if self.market_old:
            return self.market_status.text() or tr("This host has no marketplace browser yet: update it.")
        if self.market_data is None:
            return tr("The marketplaces are not read yet: Refresh.")
        if kit is None:
            return tr("Create a kit of yours first, or use the default kit.")
        if not kit.get("editable") or kit.get("kind") == "builtin":
            return tr("{kit} is one of sandbox-ai's built-in kits and read-only: choose the default kit or one of "
                      "yours (New kit…).", kit=printable(kit["name"]))
        return None

    def market_reasons(self):
        """{button: why it is disabled, or None} for the marketplace buttons."""
        kit, market = self.chosen_kit(), self.picked()
        why = self.kit_why(kit)
        reasons = {self.market_new_button: why}
        if why is None and market is None:
            why = tr("Select a marketplace first.")
        add = importing = remove = why
        if why is None:
            declared = in_kit(market.get("kits"), kit)
            agent = market.get("agent")
            if market.get("npm"):
                add = importing = remove = tr("OpenCode has no marketplaces: add its npm plugins below.")
            elif agent == "codex":
                add = remove = tr("A Codex marketplace does not go into a kit: Import for Claude, if it can.")
                importing = None if market.get("import") else (
                    printable(market.get("problem")) if market.get("problem") else
                    tr("Not in Claude's format: it cannot be imported for Claude."))
            else:
                remove = None if declared else tr("The kit does not declare it.")
                add = (tr("The kit declares it already.") if declared else
                       tr("not for a kit: {why}", why=printable(market.get("problem") or "?"))
                       if not market.get("importable") or not market.get("source_value") else None)
                enabled = any(isinstance(p, dict) and isinstance(p.get("agent"), dict) and p["agent"].get("enabled")
                              is True for p in market.get("plugins", []))
                importing = (add if add else None if market.get("agent_config") and enabled else
                             tr("Claude enables no plugin of it here: add the marketplace to the kit instead."))
        reasons.update({self.market_add_button: add, self.import_button: importing,
                        self.market_remove_button: remove})
        return reasons

    def plugin_reasons(self):
        """{button: why it is disabled, or None} for the plugin buttons of the plugin selected."""
        kit, market, plugin = self.chosen_kit(), self.picked(), self.picked_plugin()
        why = self.kit_why(kit) or (tr("Select a plugin first.") if plugin is None else None)
        if why is None and market.get("agent") == "codex":
            why = tr("sandbox-ai's kits give Codex no plugins yet: shown as Codex has them on that host.")
        if why is not None:
            return {button: why for button in self.plugin_buttons}
        mine = in_kit(plugin.get("kits"), kit)
        if market.get("npm"):
            add = None if plugin.get("importable") else tr("Not an npm package a kit can name.")
        elif in_kit(market.get("kits"), kit) or (market.get("importable") and market.get("source_value")):
            add = None
        else:
            add = tr("Its plugins cannot go into a kit: {why}", why=printable(market.get("problem") or "?"))
        default_on = any(isinstance(e, dict) and e.get("kind") == "default" and e.get("enabled")
                         for e in plugin.get("kits", []))
        return {
            self.plugin_add_button: tr("It is in the kit already.") if mine else add,
            self.enable_button: (tr("Not in this kit: add it first.") if not mine else
                                 tr("It is on in this kit already.") if mine.get("enabled") else None),
            self.disable_button: (None if mine and mine.get("enabled") else
                                  tr("It is off in this kit already.") if mine else
                                  None if default_on and kit.get("kind") != "default" else
                                  tr("Not in this kit (and the default kit does not enable it).")),
            self.plugin_remove_button: None if mine else tr("Not in this kit."),
        }

    def update_market_buttons(self):
        if busy(self):
            return
        market = self.picked()
        self.import_button.setText(tr("Import for Claude") if market and market.get("agent") == "codex" else
                                   tr("Import with Claude's plugins"))
        tips = {self.market_add_button: tr("sandbox-ai kits add-marketplace: the kit declares the marketplace with "
                                           "the source it has here; its plugins can then be added"),
                self.import_button: tr("sandbox-ai kits import-marketplace: the marketplace as Claude has it here "
                                       "(with the plugins Claude enables from it), or a Codex marketplace in "
                                       "Claude's format, for Claude"),
                self.market_remove_button: tr("sandbox-ai kits remove-marketplace: asked first"),
                self.market_new_button: tr("sandbox-ai kits add-marketplace: a Claude plugin marketplace from GitHub "
                                           "(OWNER/REPO) or a Git server (https://…)"),
                self.plugin_add_button: tr("sandbox-ai kits add-plugin: every later start with the kit gets it"),
                self.enable_button: tr("sandbox-ai kits enable-plugin: on again in every start with the kit"),
                self.disable_button: tr("sandbox-ai kits disable-plugin: declared (and installed) but off with the "
                                        "kit; it can turn off a plugin the default kit enables"),
                self.plugin_remove_button: tr("sandbox-ai kits remove-plugin: out of the kit")}
        for reasons in (self.market_reasons(), self.plugin_reasons()):
            for button, why in reasons.items():
                button._tooltip = tips[button]  # the tooltip of the enabled button (set_enabled keeps the first)
                set_enabled(button, why is None, why, tooltip=tips[button])
        remove_why = self.market_reasons()[self.market_remove_button]
        set_enabled(self.with_plugins, remove_why is None, remove_why)
        fetch_why = (self.market_status.text() if self.market_old else None)
        set_enabled(self.fetch_button, fetch_why is None, fetch_why,
                    tooltip=tr("sandbox-ai plugins claude --marketplaces-only, in a terminal: fetches the kits' "
                               "marketplaces (their plugin lists) in the plugin store's sandbox, which reaches only "
                               "their hosts. It installs nothing; Refresh afterwards."))

    # -- the marketplace browser: actions --

    def plugin_action(self, action):
        """add-plugin, enable-plugin, disable-plugin or remove-plugin of the plugin selected in the kit chosen, asked
        first (kit-add widens, kit-remove narrows), naming the host and the kit."""
        reasons = self.plugin_reasons()
        button = {"add-plugin": self.plugin_add_button, "enable-plugin": self.enable_button,
                  "disable-plugin": self.disable_button, "remove-plugin": self.plugin_remove_button}[action]
        if reasons[button] is not None:
            return
        kit, market, plugin = self.chosen_kit(), self.picked(), self.picked_plugin()
        agent = "opencode" if market.get("npm") else "claude"
        value = plugin.get("name") if market.get("npm") else plugin_key(plugin)
        label, host = self.kit_label(kit), self.hosts.label()
        options = [f"--agent={agent}"]
        if action == "add-plugin":
            if agent == "claude" and not in_kit(market.get("kits"), kit) and market.get("source_value"):
                options.append(f"--source={market['source_value']}")  # the marketplace is declared with it
            question = tr("Add {what} to the kit {kit} on {host}? Every later start with that kit gets it.",
                          what=printable(value), kit=label, host=host)
            title = tr("Plugin added to {kit}", kit=label)
        elif action == "enable-plugin":
            question = tr("Turn {plugin} on in every start with {kit} on {host}?", plugin=printable(value), kit=label,
                          host=host)
            title = tr("{kit}: {plugin} enabled", kit=label, plugin=printable(value))
        elif action == "disable-plugin":
            question = (tr("Turn {plugin} off in every start with {kit} on {host}? It stays in the kit (and "
                           "installed).", plugin=printable(value), kit=label, host=host)
                        if in_kit(plugin.get("kits"), kit) else
                        tr("Turn {plugin} off in every start with {kit} on {host}, though the default kit enables it?",
                           plugin=printable(value), kit=label, host=host))
            title = tr("{kit}: {plugin} disabled", kit=label, plugin=printable(value))
        else:
            question = tr("Remove the {kind} {value} for {agent} from the kit {kit} on {host}?", kind="plugin",
                          value=printable(value), agent=agent, kit=label, host=host)
            title = tr("{kit}: removed {value}", kit=label, value=printable(value))
        self.run(action, kit["name"], value, options=options, title=title, question=question, market=True)

    def add_market(self):
        if self.market_reasons()[self.market_add_button] is not None:
            return
        kit, market = self.chosen_kit(), self.picked()
        label = self.kit_label(kit)
        self.run("add-marketplace", kit["name"], market["name"], market["source_value"], options=["--agent=claude"],
                 title=tr("Marketplace added to {kit}", kit=label), market=True,
                 question=tr("Declare the marketplace {marketplace} ({source}) in {kit} on {host}? Its plugins can "
                             "then be added there.", marketplace=printable(market["name"]),
                             source=printable(market["source_value"]), kit=label, host=self.hosts.label()))

    def import_market(self):
        """import-marketplace: Claude's with the plugins Claude enables from it, or Codex's (in Claude's format) for
        Claude, under the name its import names."""
        if self.market_reasons()[self.import_button] is not None:
            return
        kit, market = self.chosen_kit(), self.picked()
        label, host = self.kit_label(kit), self.hosts.label()
        if market.get("agent") == "codex":
            options = ["--from=codex"]
            question = tr("Declare Codex's marketplace {marketplace} for Claude, as {name}, in {kit} on {host}?",
                          marketplace=printable(market["name"]), name=printable((market.get("import") or {}).get(
                              "name") or market["name"]), kit=label, host=host)
        else:
            options = ["--from=claude", "--with-plugins"]
            question = tr("Declare the marketplace {marketplace} in {kit} on {host} with the plugins Claude enables "
                          "from it there? Every later start with that kit gets them.",
                          marketplace=printable(market["name"]), kit=label, host=host)
        self.run("import-marketplace", kit["name"], market["name"], options=options, market=True, question=question,
                 title=tr("Marketplace added to {kit}", kit=label))

    def remove_market(self):
        """remove-marketplace, asked first (kit-remove); with its plugins (--with-plugins) when that is ticked."""
        if self.market_reasons()[self.market_remove_button] is not None:
            return
        kit, market = self.chosen_kit(), self.picked()
        label, host = self.kit_label(kit), self.hosts.label()
        with_plugins = self.with_plugins.isChecked()
        question = (tr("Take the marketplace {marketplace} with its plugins out of {kit} on {host}? Every later "
                       "start with that kit goes without them.", marketplace=printable(market["name"]), kit=label,
                       host=host) if with_plugins else
                    tr("Take the marketplace {marketplace} out of {kit} on {host}? A plugin of it the kit still "
                       "names must be removed first.", marketplace=printable(market["name"]), kit=label, host=host))
        self.run("remove-marketplace", kit["name"], market["name"],
                 options=["--agent=claude"] + (["--with-plugins"] if with_plugins else []), market=True,
                 question=question, title=tr("{kit}: removed {value}", kit=label, value=printable(market["name"])))

    def new_marketplace(self):
        """A marketplace by its source (OWNER/REPO, https://…) into the kit chosen."""
        if self.market_reasons()[self.market_new_button] is not None:
            return
        kit = self.chosen_kit()
        dialog = MarketplaceDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, source = dialog.name.text().strip(), dialog.source.text().strip()
        if not self.valid_name(name) or not source:
            return
        label = self.kit_label(kit)
        self.run("add-marketplace", kit["name"], name, source, title=tr("Marketplace added to {kit}", kit=label),
                 market=True, question=tr("Declare the marketplace {marketplace} ({source}) in {kit} on {host}? Its "
                                          "plugins can then be added there.", marketplace=printable(name),
                                          source=printable(source), kit=label, host=self.hosts.label()))

    def fetch_lists(self):
        """`sandbox-ai plugins claude --marketplaces-only` in a terminal: the kits' marketplaces fetched in the plugin
        store's sandbox (network only to their hosts), nothing installed. Refresh afterwards."""
        if self.market_old:
            return
        host = self.host()
        argv = self.sai.host_plugins_argv(host, marketplaces_only=True)
        self.terminal(argv, self.on_host("sandbox-ai plugins claude --marketplaces-only"))
        self.market_status.setText(tr("The plugin lists are fetched in the terminal (nothing is installed); Refresh "
                                      "afterwards."))

    # -- actions --

    def run(self, action, name, *arguments, options=(), title=None, create=False, question=None, market=False):
        """`sandbox-ai kits ACTION ... NAME ARGUMENTS` in the background; CREATE: `sandbox-ai kits new NAME` first
        (nothing more runs when that fails). What widens every later start with the kit is asked first (kit-add),
        what narrows it too (kit-remove): QUESTION, naming the host and the kit. MARKET: from the marketplace browser
        (whose rows must be the host's chosen)."""
        if self.busy_change():
            return
        host = self.host()
        shown = self.market_host if market else self.kits_host
        if shown not in (None, host):  # rows of another host: never acted on here
            warn(self, title or tr("Kits"), tr("The kits shown are {old}'s, not {host}'s: Refresh first.",
                                               old=self.hosts.label(shown), host=self.hosts.label(host)))
            return
        if action in WIDENS:
            what = " ".join(printable(a) for a in arguments) or printable(name)
            if not ask_first(self, "kit-add", title or tr("Kits"), question or on_host(host, tr(
                    "Add {what} to the kit {name}? Every later start with this kit gets it; a plugin or a hook runs "
                    "code there.", what=what, name=printable(name))), tr("Add")):
                return
        elif action in NARROWS:
            if not ask_first(self, "kit-remove", title or tr("Kits"), question or on_host(host, tr(
                    "Take {what} out of the kit {name}? Every later start with this kit is without it.",
                    what=" ".join(printable(a) for a in arguments), name=printable(name))), tr("Remove")):
                return

        def work():
            first = ""
            if create:
                status, out, err = self.sai.host_kit_command(host, "new", name)
                if status != 0:
                    return status, out, err
                first = out
            status, out, err = self.sai.host_kit_command(host, action, name, *arguments, options=options)
            return status, first + out, err
        self.start_change(work, title or f"sandbox-ai kits {action}", tr("sandbox-ai kits {action} {name} is running…",
                                                                         action=action, name=printable(name)))

    def change_sources(self):
        """The buttons that change kits (or read them): off while a change runs, so two clicks never start two."""
        return [self.refresh_button, self.use_button, *self.edit_buttons, self.trust_button, self.revoke_button,
                *self.market_buttons, *self.plugin_buttons]

    def busy_change(self):
        """True (and said in the message area) while a change runs: one at a time."""
        if self.task.running:
            self.message.setText(tr("A change is running: wait for it, or cancel it."))
            return True
        return False

    def start_change(self, work, title, text):
        """WORK (sandbox-ai commands) in the background with the busy bar, its output as it comes and Cancel; the
        outcome in the message area of this host's page (a late one, after another host was chosen, is dropped)."""
        host, title = self.host(), self.on_host(title)
        self.message.setText("")
        self.task.call_watched(self.sai, work, lambda result, error: self.done(host, title, result, error), text,
                               source=self.change_sources())

    def user_kits(self):
        return sorted(kit["name"] for kit in self.kits.values() if kit.get("kind") == "user")

    def target(self, why=None, default_ok=False):
        """(kit name, create it first?, its inventory entry or None) that receives an addition: the selected kit
        when it is yours (or the default kit, for a plugin or a marketplace: DEFAULT_OK), else the kit of yours the
        human chooses (or names, to be created first). None when cancelled."""
        kit = self.current()
        if kit is not None and why is None and (kit.get("kind") == "user" or (
                default_ok and kit.get("kind") == "default")):
            return kit["name"], False, kit
        if why is None:
            why = (tr("No kit of yours is selected.") if kit is None else
                   tr("The default kit is edited in its folder: {path}", path=printable(kit.get("path") or ""))
                   if kit.get("kind") == "default" else
                   tr("sandbox-ai's own kits are read-only: create a kit of your own (New kit…)."))
        dialog = TargetKitDialog(self, why, self.user_kits())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        name, new = dialog.chosen()
        if not self.valid_name(name) or (new and name in ("default", "built-in")):
            if name in ("default", "built-in"):
                warn(self, tr("Kits & plugins"), tr("{name} is not a valid name.", name=printable(name)))
            return None
        new = new and name not in self.user_kits()
        return name, new, self.kits.get(f"user:{name}")

    def done(self, host, title, result, error):
        """What a change said, in the message area (wrapped, plain text; an error in its colour); the kits are read
        again. For a host no longer chosen: only read again (its rows went when the host changed)."""
        status, out, err = outcome(result, error)
        text = printable((out + err).strip())[-4000:]
        self.update_buttons()
        self.update_market_buttons()
        if host != self.host():
            return
        if self.task.cancelled or status == backend.CANCELLED:
            self.show_message(tr("{title}: cancelled. What sandbox-ai did up to then stays; Refresh shows the kits.",
                                 title=title), "notice-warn")
        elif status != 0:
            self.show_message(title + ": " + (text or tr("Failed (exit status {status}).", status=status)), "bad")
        else:
            self.show_message(title + (": " + text if text else ""), "secondary")
        self.refresh()

    def show_message(self, text, role="secondary"):
        """TEXT in the message area; ROLE "bad" (an error) or "notice-warn" (cancelled) in that status colour."""
        self.message.setProperty("role", role)
        palette = self.message.palette()
        status = {"bad": "bad", "notice-warn": "warn"}.get(role)
        palette.setColor(QPalette.ColorRole.Text, QColor(theme.color(status)) if status else
                         self.palette().color(QPalette.ColorRole.Text))
        self.message.setPalette(palette)
        self.message.setText(text)

    def valid_name(self, name, pattern=backend.KIT_NAME):
        if re.fullmatch(pattern, name or ""):
            return True
        warn(self, tr("Kits & plugins"), tr("{name} is not a valid name.", name=printable(name)))
        return False

    def new_kit(self):
        dialog = NewKitDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name.text().strip()
        if not self.valid_name(name) or not dialog.agents.chosen():
            return
        self.run("new", name, options=[f"--agent={agent}" for agent in dialog.agents.chosen()], title=tr("New kit"))

    def add_skill(self):
        """A skill folder (with SKILL.md) copied into a kit of yours: sandbox-ai kits add-skill checks it (a real
        folder, nothing followed, special files skipped, limits) and notes when it lies in a folder sandboxes
        can write."""
        target = self.target()
        if target is None:
            return
        name, create, _ = target
        path = self.remote_path(tr("Skill folder (with SKILL.md)")) if self.host() != LOCAL else \
            choose_folder(self, tr("Skill folder (with SKILL.md)"), "skill")
        if path:
            self.run("add-skill", name, path, title=tr("Add skill"), create=create)

    def new_skill(self):
        target = self.target()
        if target is None:
            return
        kit_name, create, kit = target
        dialog = NewSkillDialog(self, (sorted(kit_agents(kit)) if kit else []) or list(AGENTS))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name.text().strip()
        if not self.valid_name(name, backend.SKILL_NAME) or not dialog.agents.chosen():
            return
        options = [f"--agent={agent}" for agent in dialog.agents.chosen()]
        if dialog.description.text().strip():
            options.append(f"--description={dialog.description.text().strip()}")
        self.run("new-skill", kit_name, name, options=options, title=tr("New skill"), create=create)

    def remote_path(self, title):
        """A path on the host chosen, typed (this machine's file dialog cannot see that host); None when cancelled
        or not absolute."""
        path, ok = QInputDialog.getText(self, title, tr("The absolute path on {host}:", host=self.hosts.label()))
        path = path.strip() if ok else ""
        if path and not backend.valid_directory(path):
            warn(self, tr("Kits & plugins"), tr("Not an absolute path: {path}", path=printable(path)))
            return None
        return path or None

    def known_plugins(self):
        """The plugin ids of every Claude marketplace the browser read (for the Add plugin dialog)."""
        return sorted({plugin_key(p) for key, market in self.markets.items() if key.startswith("claude:")
                       for p in market.get("plugins", []) if isinstance(p, dict) and plugin_key(p)})

    def add_plugin(self, plugin=None):
        target = self.target(default_ok=True)
        if target is None:
            return
        name, create, _ = target
        dialog = PluginDialog(self, self.known_plugins())
        if plugin:
            dialog.plugin.setCurrentText(plugin)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        plugin = dialog.plugin.currentText().strip()
        if not plugin:
            return
        options = [f"--agent={dialog.agent.currentText()}"]
        if dialog.source.text().strip():
            options.append(f"--source={dialog.source.text().strip()}")
        self.run("add-plugin", name, plugin, options=options, title=tr("Add plugin"), create=create)

    def add_marketplace(self):
        target = self.target(default_ok=True)
        if target is None:
            return
        kit_name, create, _ = target
        dialog = MarketplaceDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, source = dialog.name.text().strip(), dialog.source.text().strip()
        if not self.valid_name(name) or not source:
            return
        self.run("add-marketplace", kit_name, name, source, title=tr("Add marketplace"), create=create)

    def remove(self):
        """A skill, plugin or hook out of a kit of yours; out of the default kit, a plugin (its skills are edited in
        its folder)."""
        kit = self.current()
        if kit is None:
            return
        agents = sorted(kit_agents(kit).items())
        entries = [] if kit.get("kind") == "default" else [
            ("skill", agent, skill) for agent, info in agents for skill in names(info.get("skills"))]
        entries += [("plugin", agent, plugin) for agent, info in agents
                    for plugin in names(info.get("plugins")) + names(info.get("disabled"))]
        if kit.get("kind") != "default":
            entries += [("hook", agent, name) for agent, info in agents for name in hook_files(info)]
        if not entries:
            inform(self, tr("Remove from the kit"), tr("The kit has no skills, plugins or hooks."))
            return
        dialog = RemoveDialog(self, entries)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.chosen() is None:
            return
        kind, agent, value = dialog.chosen()
        action = {"skill": "remove-skill", "hook": "remove-hook"}.get(kind, "remove-plugin")
        self.run(action, kit["name"], value, options=[f"--agent={agent}"], title=tr("Remove from the kit"),
                 question=on_host(self.host(), tr(
                     "Take {what} ({agent}) out of the kit {name}? Every later start with this kit is without it.",
                     what=printable(value), agent=printable(agent), name=printable(kit["name"]))))

    def add_hook(self):
        target = self.target()
        if target is None:
            return
        name, create, kit = target
        agents = (sorted(kit_agents(kit)) if kit else []) or list(AGENTS)
        dialog = HookDialog(self, agents, remote=True) if self.host() != LOCAL else HookDialog(self, agents)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path = dialog.file.text().strip()
        if not path or not dialog.agents.chosen():
            return
        self.run("add-hook", name, path, options=dialog.options(), title=tr("Add hook"), create=create)

    def trust_hooks(self):
        """`sandbox-ai kits trust-hooks KIT` in a terminal: the review is there, and so is the yes (or no). Refresh
        afterwards to see the approval."""
        kit = self.current()
        if kit is None:
            return
        name = kit["name"]
        # Trusted hooks run in every later start with the kit: asked first (kit-hooks-trust), then reviewed and
        # approved hook by hook in the terminal.
        if not ask_first(self, "kit-hooks-trust", tr("Review and approve hooks…"), on_host(self.host(), tr(
                "Hooks of {name} you approve run in every later start with that kit. The terminal shows every hook "
                "and the full text of every file before you approve there.", name=printable(name))),
                tr("Open the review")):
            return
        host = self.host()
        argv = self.sai.trust_hooks_argv(name) if host == LOCAL else self.sai.host_trust_hooks_argv(host, name)
        self.terminal(argv, self.on_host(f"sandbox-ai kits trust-hooks {name}"))
        self.summary.setText(tr("Review the hooks of {name} in the terminal, then Refresh.", name=printable(name)))

    def revoke_hooks(self):
        """`sandbox-ai kits trust-hooks --revoke KIT`: the approval is taken back (nothing widens: no question)."""
        kit, host = self.current(), self.host()
        if kit is None:
            return
        name = kit["name"]
        if self.busy_change():
            return
        self.start_change(lambda: self.sai.revoke_hooks(host, name), tr("Take the approval back"),
                          tr("sandbox-ai kits trust-hooks --revoke {name} is running…", name=printable(name)))

    def plugin_status(self):
        """`sandbox-ai plugins --status` on the host chosen, shown as it says it."""
        host = self.host()
        background(lambda: self.sai.plugins_status(host), owner=self, source=[self.status_button], timeout=150,
                   done=lambda result, error: self.plugin_status_shown(host, result, error))

    def plugin_status_shown(self, host, result, error):
        status, out, err = outcome(result, error)
        text = "\n".join(printable(line) for line in (out if status == 0 else (err or out)).strip()[-8000:]
                         .splitlines())
        show_text(self, tr("Plugins on {host}", host=self.hosts.label(host)), text or tr("Failed (exit status "
                                                                                        "{status}).", status=status),
                  modal=False)

    def delete(self):
        kit = self.current()
        if kit is None:
            return
        if ask_first(self, "kit-delete", tr("Delete kit…").rstrip("…"), on_host(self.host(), tr(
                "Delete the kit {name} with everything in it ({path})? Projects that use it can no longer start until "
                "you remove --kit {name}.", name=printable(kit["name"]), path=printable(kit.get("path") or ""))),
                tr("Delete")):
            self.run("delete", kit["name"], options=["--yes"], title=tr("Delete"))

    def use(self):
        kit = self.current()
        if kit is not None:
            self.enable_kit.emit(kit["name"])

    def install(self, update):
        """`sandbox-ai plugins claude [--update]` in a terminal, with --kit for the built-in kits the current
        project uses (their plugins are installed only on request)."""
        builtin = {kit["name"] for kit in self.kits.values() if kit.get("kind") == "builtin"}
        chosen = [name for name in self.project_kits if name in builtin] if self.host() == LOCAL else []
        kit = self.current()
        if kit is not None and kit.get("kind") == "builtin" and kit["name"] not in chosen:
            chosen.append(kit["name"])
        host = self.host()
        argv = self.sai.plugins_argv(update=update, kits=chosen) if host == LOCAL else \
            self.sai.host_plugins_argv(host, update=update, kits=chosen)
        self.terminal(argv, self.on_host("sandbox-ai plugins claude" + (" --update" if update else "")))
