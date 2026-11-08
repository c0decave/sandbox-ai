"""The Kits & plugins page: sandbox-ai's kits (its own, the default kit, built-in kits such as egm and yours),
what each brings per agent (skills, agents, commands, prompts, the memory template, Claude plugins and
marketplaces), sandbox-ai's own plugin marketplace and the plugin store.

Everything here runs `sandbox-ai kits ...` (read: `kits list --json`; change: new, add-skill, new-skill,
add-plugin, add-marketplace, add-hook, remove-skill, remove-plugin, remove-hook, delete) or `sandbox-ai plugins
claude` in a terminal: the command's checks apply (no links followed, built-in kits read-only, folder marketplaces
refused). Using a kit in the current project goes through the Board's Apply (shown, then written and trusted).

A kit's hooks run only once approved. Approving is `sandbox-ai kits trust-hooks KIT` in a terminal: it shows every
hook and the full text of every file, and the human types yes there. The app never approves on its own (no
--yes); taking an approval back is `sandbox-ai kits trust-hooks --revoke KIT`.

The host chooser at the top shows and changes the kits of this machine or of a host of `sandbox-ai hosts` (over SSH:
`sandbox-ai hosts run HOST -- kits ...`; the review of hooks and the plugin installs in a terminal tab with ssh -tt).
A skill folder or a hook file is then a path on that host.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import backend
from .files import printable
from .host_choice import on_host
from .state import choose_file, choose_folder
from .i18n import N_, tr
from .widgets import (
    ask_first, background, busy, fill, flow, inform, outcome, plain_label, row_key, selected_rows, set_enabled,
    show_text, table, warn,
)

LOCAL = "local"

KIND_TITLES = {"always": N_("always (sandbox-ai)"), "default": N_("always (default kit)"), "builtin": N_("built-in"),
               "user": N_("yours")}
ENTRY_TITLES = (("skills", N_("Skills")), ("agents", N_("Agents")), ("commands", N_("Commands")),
                ("prompts", N_("Prompts")))
AGENTS = backend.AGENTS
NEW_KIT = "my-kit"  # the name proposed for a first kit of yours (a name, not a text)
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart")


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
        self.source = QLineEdit()
        self.source.setPlaceholderText(tr("OWNER/REPO or https://git.example.org/plugins.git"))
        folder = QPushButton(tr("Folder…"))
        folder.clicked.connect(self.choose_folder)
        row = QHBoxLayout()
        row.addWidget(self.source, 1)
        row.addWidget(folder)
        self.form.addRow(tr("Name"), self.name)
        self.form.addRow(tr("Source"), row)
        self.finish()

    def choose_folder(self):
        path = choose_folder(self, tr("Marketplace folder"), "marketplace")
        if path:
            self.source.setText(path)


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


class KitsPage(QWidget):
    enable_kit = Signal(str)  # use this kit in the current project (the window passes it to the Board)
    inventory_loaded = Signal(object)  # the inventory, for the Board's kits node

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.inventory, self.kits, self.project, self.project_kits, self.token = None, {}, None, [], 0
        self.installed, self.market_entries, self.kits_host = set(), {}, None  # kits_host: whose rows are shown
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
        split = self.split = QSplitter(Qt.Orientation.Horizontal)
        kits_box = QGroupBox(tr("Kits"))
        kits_layout = QVBoxLayout(kits_box)
        self.table = table([tr("Kit"), tr("Kind"), tr("Agents"), tr("Brings"), tr("This project")], stretch=3)
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
        # Adding works whatever kit is selected: into the selected kit when it is yours, else into a kit of yours
        # chosen (or created) first. Removing and deleting are for your own kits only.
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
        details_layout.addWidget(self.where)
        details_layout.addWidget(self.tree, 1)
        split.addWidget(kits_box)
        split.addWidget(details_box)
        split.setSizes([700, 340])
        layout.addWidget(split, 3)
        market_box = QGroupBox(tr("sandbox-ai's plugin marketplace (sandbox-ai)"))
        market_layout = QVBoxLayout(market_box)
        self.market = table([tr("Plugin"), tr("Description"), tr("Comes from"), tr("Installed"), tr("In kits")], stretch=1)
        self.market.setMinimumHeight(110)
        self.market.itemSelectionChanged.connect(self.update_buttons)
        market_layout.addWidget(self.market, 1)
        row = QHBoxLayout()
        self.add_to_kit_button = QPushButton(tr("Add to a kit…"))
        self.add_to_kit_button.setToolTip(tr("Declare the selected plugin in one of your kits (sandbox-ai kits "
                                             "add-plugin)"))
        self.add_to_kit_button.clicked.connect(lambda _=False: self.add_to_kit())
        self.store = plain_label(role="secondary")
        row.addWidget(self.add_to_kit_button)
        row.addWidget(self.store, 1)
        market_layout.addLayout(row)
        layout.addWidget(market_box, 2)
        self.loaded_once = False
        self.update_buttons()

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
        self.clear_rows()
        if self.loaded_once:
            self.refresh()

    def clear_rows(self):
        self.inventory, self.kits, self.kits_host, self.market_entries = None, {}, None, {}
        self.installed = set()
        fill(self.table, [])
        fill(self.market, [])
        self.tree.clear()
        self.store.setText("")
        self.selection_changed()

    def refresh(self):
        """`sandbox-ai kits list --json`, in the background (on the host chosen)."""
        self.loaded_once = True
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
            problem = any(info.get("error") for info in agents.values())
            replaced = kit.get("kind") == "builtin" and set(agents) <= set(names(kit.get("replaced")))
            brings = [tr("skills: {list}", list=", ".join(skills))] if skills else []
            brings += [tr("plugins: {list}", list=", ".join(plugins))] if plugins else []
            hooks = sum(len(names(hooks_of(info).get("hooks"))) or 1 for info in agents.values() if hooks_of(info))
            waiting = unapproved(kit)
            brings += [tr("hooks: {count} (not approved)", count=hooks) if waiting else
                       tr("hooks: {count}", count=hooks)] if hooks else []
            rows.append([printable(kit["name"]), tr(KIND_TITLES.get(kit.get("kind"), str(kit.get("kind")))),
                         ", ".join(sorted(agents)), printable(" · ".join(brings)) or "–", self.use_text(kit)])
            colors.append("bad" if problem else "muted" if replaced else "warn" if waiting else None)
            keys.append(key)
        fill(self.table, rows, colors, keys=keys)
        market = inventory.get("marketplace") if isinstance(inventory.get("marketplace"), dict) else {}
        entries = [e for e in market.get("plugins", []) if isinstance(e, dict)] if isinstance(market.get("plugins"),
                                                                                                list) else []
        self.market_entries = {str(e.get("id") or e.get("name")): e for e in entries}
        fill(self.market, [[printable(key), printable(e.get("description") or ""), printable(e.get("source") or ""),
                            tr("yes") if key in self.installed else tr("no"), printable(", ".join(names(e.get("kits"))))]
                           for key, e in self.market_entries.items()],
             ["ok" if key in self.installed else None for key in self.market_entries], keys=list(self.market_entries))
        store = printable(inventory.get("store") or "")
        listed = ", ".join(sorted(self.installed)) or tr("none")
        self.store.setText(tr("Plugin store {path}: {list}", path=store, list=printable(listed)))
        user = sum(1 for kit in self.kits.values() if kit.get("kind") == "user")
        self.summary.setText(tr("{count} kits ({user} of yours) in {path}", count=len(self.kits), user=user,
                                path=printable(inventory.get("kits_dir") or "")) +
                             ("" if self.host() == LOCAL else f" · {self.hosts.label()}"))
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
            plugins = names(info.get("plugins"))
            if plugins:
                child = QTreeWidgetItem([tr("Plugins: {count}", count=len(plugins))])
                for plugin in plugins:
                    state = tr("installed") if plugin in self.installed or agent != "claude" else tr("not installed")
                    child.addChild(QTreeWidgetItem([f"{printable(plugin)}  ({state})"]))
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
        why = (none if kit is None else None if kind == "user" else
               tr("The default kit is edited in its folder: {path}", path=printable(kit.get("path") or ""))
               if kind == "default" else
               tr("sandbox-ai's own kits are read-only: create a kit of your own (New kit…)."))
        for button in self.change_buttons:
            set_enabled(button, why is None, why)
        for button in self.add_buttons:
            set_enabled(button, True)
        self.add_hint.setText("" if why is None or kit is None else
                              tr("{why} What you add goes to a kit of yours: you choose it (or a new one) first.",
                                 why=why))
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
        rows = selected_rows(self.market)  # the kit of yours is chosen (or created) when adding
        market_why = tr("Select a plugin of the marketplace first.") if not rows else None
        set_enabled(self.add_to_kit_button, market_why is None, market_why)

    # -- actions --

    def run(self, action, name, *arguments, options=(), title=None, create=False):
        """`sandbox-ai kits ACTION ... NAME ARGUMENTS` in the background; CREATE: `sandbox-ai kits new NAME` first
        (nothing more runs when that fails). What is added to a kit reaches every later start with it: asked first."""
        if action.startswith("add-") or action == "new-skill":
            what = " ".join(printable(a) for a in arguments) or printable(name)
            if not ask_first(self, "kit-add", title or tr("Kits"), on_host(self.host(), tr(
                    "Add {what} to the kit {name}? Every later start with this kit gets it; a plugin or a hook runs "
                    "code there.", what=what, name=printable(name))), tr("Add")):
                return

        host = self.host()
        if self.kits_host not in (None, host):  # rows of another host: never acted on here
            warn(self, title or tr("Kits"), tr("The kits shown are {old}'s, not {host}'s: Refresh first.",
                                               old=self.hosts.label(self.kits_host), host=self.hosts.label(host)))
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
        background(work, owner=self,
                   source=[self.refresh_button, self.use_button, self.add_to_kit_button, *self.edit_buttons],
                   timeout=600, done=lambda result, error: self.done(self.on_host(title or f"sandbox-ai kits {action}"),
                                                                     result, error))

    def user_kits(self):
        return sorted(kit["name"] for kit in self.kits.values() if kit.get("kind") == "user")

    def target(self, why=None):
        """(kit name, create it first?, its inventory entry or None) that receives an addition: the selected kit
        when it is yours, else the kit of yours the human chooses (or names, to be created first). None when
        cancelled."""
        kit = self.current()
        if kit is not None and kit.get("kind") == "user" and why is None:
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

    def done(self, title, result, error):
        status, out, err = outcome(result, error)
        text = (out + err).strip()
        if status != 0:
            warn(self, title, text or tr("Failed (exit status {status}).", status=status))
        elif text:
            inform(self, title, text, modal=False)  # a result: never blocks the window
        self.update_buttons()
        self.refresh()

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
        return list(self.market_entries)

    def add_plugin(self, plugin=None):
        target = self.target()
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
        target = self.target()
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
        kit = self.current()
        if kit is None:
            return
        entries = [("skill", agent, skill) for agent, info in sorted(kit_agents(kit).items())
                   for skill in names(info.get("skills"))]
        entries += [("plugin", agent, plugin) for agent, info in sorted(kit_agents(kit).items())
                    for plugin in names(info.get("plugins"))]
        entries += [("hook", agent, name) for agent, info in sorted(kit_agents(kit).items())
                    for name in hook_files(info)]
        if not entries:
            inform(self, tr("Remove from the kit"), tr("The kit has no skills, plugins or hooks."))
            return
        dialog = RemoveDialog(self, entries)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.chosen() is None:
            return
        kind, agent, value = dialog.chosen()
        action = {"skill": "remove-skill", "hook": "remove-hook"}.get(kind, "remove-plugin")
        if not ask_first(self, "kit-remove", tr("Remove from the kit"), on_host(self.host(), tr(
                "Take {what} ({agent}) out of the kit {name}? Every later start with this kit is without it.",
                what=printable(value), agent=printable(agent), name=printable(kit["name"]))), tr("Remove")):
            return
        self.run(action, kit["name"], value, options=[f"--agent={agent}"], title=tr("Remove from the kit"))

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
        background(lambda: self.sai.revoke_hooks(host, name), owner=self, source=[self.revoke_button],
                   timeout=150, done=lambda result, error: self.done(self.on_host(tr("Take the approval back")), result,
                                                                     error))

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

    def add_to_kit(self):
        rows = selected_rows(self.market)
        plugin = row_key(self.market, rows[0]) if rows else None
        if not plugin:
            return
        target = self.target(why=tr("Declare {plugin} in a kit of yours:", plugin=printable(plugin)))
        if target is not None:
            self.run("add-plugin", target[0], plugin, options=["--agent=claude"], title=tr("Add plugin"),
                     create=target[1])

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
