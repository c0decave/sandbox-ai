"""The Kits & plugins page's tab of what the agents on this machine (or on the host chosen above) have set up
themselves: their skills, commands, prompts, subagents, memory and instructions files, auto memory, hooks, plugins,
marketplaces and MCP servers (`sandbox-ai kits host-list --json`, read in the background), grouped by agent and kind.

Read only until the human acts: a part selected is previewed (`kits host-show --json`: its files, the scripts in it,
the secret-looking lines masked, its text, what it becomes in each agent's kit); several are selected with Ctrl and
Shift. "Import into a kit…" copies the parts selected into a kit of yours there (new or existing:
`kits host-import --kit KIT`, asked first: kit-add); "Add to a project's memory…" puts memory parts into the memory
sandbox-ai keeps for a project there (`--project DIR`, asked first: memory-add), which every later start and resumed
session of that project gets. Nothing is imported without a selection. Hooks, plugins, marketplaces and MCP servers
are shown only; a part with secret-looking lines goes only when its box is ticked in the dialog; a plugin module goes
into the kit's hooks, which run only once approved (Review and approve hooks…); a skill's own hooks are taken out by
the command. Everything runs as the page's other changes do (its busy bar, Cancel, the message area).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
    QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import remote_chooser as RC, tables, widgets
from .files import printable, printable_text
from .host_choice import on_host
from .i18n import N_, tr
from .widgets import ask_first, background, explanation, flow, plain_label, safe_tooltip, warn

LOCAL = "local"
PART = Qt.ItemDataRole.UserRole
KIND_TITLES = {"skill": N_("Skills"), "command": N_("Commands"), "prompt": N_("Prompts"), "agent": N_("Subagents"),
               "output-style": N_("Output styles"), "memory-file": N_("Memory and instructions files"),
               "memory": N_("Memory"), "hooks": N_("Hooks (shown only)"), "plugin": N_("Installed plugins (shown only)"),
               "marketplace": N_("Marketplaces (shown only)"), "mcp": N_("MCP servers (shown only)"),
               "plugin-module": N_("Plugin modules (code)"), "extension": N_("Extensions (code)")}
MEMORY_KINDS = ("memory-file", "memory")


def flags(part):
    """The short words beside a part: its size, scripts, secret-looking lines, private, own hooks, shown only."""
    words = []
    if part.get("kind") == "skill" and part.get("importable"):
        words.append(tr("{count} file(s)", count=part.get("files") or 0))
    scripts = [str(s) for s in part.get("scripts") or []]
    if scripts:
        words.append(tr("scripts: {names}", names=", ".join(printable(s) for s in scripts[:3])))
    if part.get("secrets"):
        words.append(tr("{count} secret-looking line(s)", count=part["secrets"]))
    if part.get("findings"):
        words.append(tr("{count} hidden-text finding(s)", count=part["findings"]))
    if part.get("private"):
        words.append(tr("private"))
    if part.get("hooks"):
        words.append(tr("own hooks: taken out on import"))
    if not part.get("importable"):
        words.append(tr("shown only") if part.get("kind") not in (
            "skill", "command", "prompt", "agent", "memory-file", "memory", "plugin-module") else tr("refused"))
    return words


def preview_text(shown):
    """The text of the preview pane for SHOWN (`kits host-show --json`)."""
    lines = [f"{printable(shown.get('id', ''))}  ({printable(shown.get('path', ''))})"]
    if shown.get("description"):
        lines.append(printable_text(shown["description"]))
    if not shown.get("importable"):
        lines.append(tr("Not imported: {why}", why=printable_text(shown.get("why") or "")))
    for item in shown.get("file_list") or []:
        if isinstance(item, dict):
            lines.append(tr("file {path} ({bytes} bytes)", path=printable(item.get("path", "")), bytes=item.get("bytes", 0))
                         + (tr(" - a script: an agent may run it") if item.get("script") else ""))
    for found in shown.get("secrets") or []:
        if isinstance(found, dict):
            lines.append(tr("secret-looking line {file}:{line} ({rule}): {text}", file=printable(found.get("file", "")),
                            line=found.get("line", "?"), rule=printable(found.get("rule", "")),
                            text=printable(found.get("text", ""))))
    for found in shown.get("findings") or []:
        if isinstance(found, dict):
            lines.append(tr("hidden-text finding {file}:{line} ({rule}): {text}", file=printable(found.get("file", "")),
                            line=found.get("line", "?"), rule=printable(found.get("rule", "")),
                            text=printable(found.get("hidden") or found.get("text", ""))))
    for item in shown.get("conversions") or []:
        if isinstance(item, dict):
            lines.append(tr("into a kit for {agent}: {where}", agent=printable(item.get("agent", "")),
                            where=printable(item.get("where") or "")) if item.get("ok") else
                         tr("into a kit for {agent}: no - {why}", agent=printable(item.get("agent", "")),
                            why=printable_text(item.get("why") or "")))
            lines += ["   " + tr("note: {text}", text=printable_text(note)) for note in item.get("notes") or []]
    lines += [tr("note: {text}", text=printable_text(note)) for note in shown.get("notes") or []]
    if shown.get("text"):
        lines += ["", printable_text(shown["text"])]
    return "\n".join(lines)


def flagged(parts):
    """How many of PARTS the import may refuse for secret-looking lines: those the list counted some in, and those it
    did not check whole ("secrets": null - a file longer than the list reads, or past its budget; the import checks
    every byte)."""
    return sum(1 for p in parts if p.get("secrets") or (p.get("secrets") is None and p.get("importable")))


def hidden_text(parts):
    """How many of PARTS the import may refuse for findings of the hidden-text checker (invisible or bidi characters,
    instruction phrasing aimed at the model): those the list counted some in, and those it did not check whole."""
    return sum(1 for p in parts if p.get("findings") or (p.get("findings") is None and p.get("importable")))


class ImportDialog(QDialog):
    """Where the parts chosen go: a kit of yours (or a new one), or a project's memory; for which agents; with
    secret-looking lines or not."""

    def __init__(self, parent, title, note, kits=None, project=None, host=LOCAL, secrets=0, sai=None, findings=0):
        super().__init__(parent)
        from .kits_page import AGENTS, AgentsBox, NEW_KIT  # (the page's own widgets; no import cycle at load)
        self.setWindowTitle(title)
        self.host, self.sai = host, sai
        form = QFormLayout(self)
        text = plain_label(note, role="secondary")
        text.setWordWrap(True)
        form.addRow(text)
        self.kit = self.name = self.project = None
        if kits is not None:
            self.kit = QComboBox()
            for name in kits:
                self.kit.addItem(printable(name), name)
            self.kit.addItem(tr("A new kit…"), "")
            self.name = QLineEdit()
            self.name.setPlaceholderText(tr("my-kit"))
            if not kits:
                self.name.setText(NEW_KIT)
            self.kit.currentIndexChanged.connect(lambda _=0: self.name.setEnabled(self.kit.currentData() == ""))
            self.name.setEnabled(self.kit.currentData() == "")
            form.addRow(tr("Kit"), self.kit)
            form.addRow(tr("New kit's name"), self.name)
        else:
            self.project = QLineEdit(project or "")
            self.project.setPlaceholderText(tr("the project's folder (its first workspace)"))
            browse = QPushButton(tr("Folder…"))
            browse.clicked.connect(lambda _=False: self.choose())
            row = QHBoxLayout()
            row.addWidget(self.project, 1)
            row.addWidget(browse)
            form.addRow(tr("Project"), row)
        self.agents = AgentsBox(AGENTS, checked=[])
        self.agents.setToolTip(tr("None ticked: each part goes to its own agent (OpenCode's to OpenCode and OpenCode "
                                  "2); a part of the shared skills folder to the kit's agents"))
        form.addRow(tr("For"), self.agents)
        self.secrets = QCheckBox(tr("Also the parts with secret-looking lines, or not checked whole yet ({count})",
                                    count=secrets))
        self.secrets.setToolTip(tr("A token or password in a kit reaches every sandbox that uses the kit; better take "
                                   "it out of the file first"))
        form.addRow(self.secrets)
        self.secrets.setVisible(bool(secrets))  # (after addRow: never a window of its own for a moment)
        self.findings = QCheckBox(tr("Also the parts with hidden text, or not checked whole yet ({count})",
                                     count=findings))
        self.findings.setToolTip(tr("Invisible or bidirectional characters and instructions aimed at the model can "
                                    "steer every sandbox that uses the kit without you seeing it; look at the preview "
                                    "first (escapes show what is hidden)"))
        form.addRow(self.findings)
        self.findings.setVisible(bool(findings))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setMinimumWidth(520)

    def choose(self):
        path = RC.choose_folder(self, self.host, tr("Project folder"), "project", start=self.project.text().strip(),
                                sai=self.sai)
        if path:
            self.project.setText(path)

    def kit_choice(self):
        """(name, whether it is new)."""
        current = self.kit.currentData()
        return (current, False) if current else (self.name.text().strip(), True)


class AgentPartsTab(QWidget):
    """The tab: the agents' own parts of the page's host, a preview, and the imports."""

    def __init__(self, page):
        super().__init__()
        self.page, self.sai = page, page.sai
        self.data, self.data_host, self.token, self.preview_token, self.loaded_once = None, None, 0, 0, False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.hint = explanation(tr(
            "What the agents on this machine have set up themselves, read only: skills, commands, prompts, subagents, "
            "memory and instructions files, auto memory - and their hooks, plugins, marketplaces and MCP servers, "
            "shown only. Select parts (Ctrl, Shift for several) and import them into a kit of yours or into a "
            "project's memory; nothing is imported otherwise."))
        layout.addWidget(self.hint)
        # (what they may hold, before anything is imported: always shown)
        layout.addWidget(plain_label(tr("Memory and instructions files may hold private notes."), role="secondary"))
        self.refresh_button = QPushButton(tr("Read again"))
        self.refresh_button.setToolTip(tr("sandbox-ai kits host-list: read the agents' folders again"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.import_button = QPushButton(tr("Import into a kit…"))
        self.import_button.setProperty("role", "primary")
        self.import_button.setToolTip(tr("sandbox-ai kits host-import --kit: the parts selected into a kit of yours "
                                         "(new or existing), converted where the agents' formats map"))
        self.import_button.clicked.connect(lambda _=False: self.import_into_kit())
        self.memory_button = QPushButton(tr("Add to a project's memory…"))
        self.memory_button.setToolTip(tr("sandbox-ai kits host-import --project: the memory parts selected into the "
                                         "memory sandbox-ai keeps for a project; every later start and resumed session "
                                         "of it has them"))
        self.memory_button.clicked.connect(lambda _=False: self.import_into_memory())
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter by name or description"))
        self.filter.setAccessibleName(tr("Filter the agents' parts"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda _text: self.show_parts())
        layout.addWidget(flow(self.import_button, self.memory_button, self.refresh_button))
        layout.addWidget(self.filter)
        self.status = plain_label(role="secondary", selectable=True)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        split = self.split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([tr("Part"), tr("About")])
        self.tree.setAccessibleName(tr("The agents' own parts"))
        tables.setup(self.tree, stretch=1, compact=(0,))  # the part's name whole, "About" takes the rest
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        from .kits_page import with_context_menu
        with_context_menu(self.tree, lambda: [self.import_button, self.memory_button, None, self.refresh_button])
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName(tr("Preview of the part"))
        self.preview.setFont(QFont("monospace"))
        self.preview.setPlaceholderText(tr("Select a part to see its files and text."))
        split.addWidget(self.tree)
        split.addWidget(self.preview)
        split.setSizes([560, 440])
        layout.addWidget(split, 1)
        self.update_buttons()

    # -- reading --

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once:
            self.refresh()

    def host(self):
        return self.page.host()

    def host_changed(self):
        """Another host: its rows go at once, a late answer of the one before is dropped."""
        self.token += 1
        self.preview_token += 1
        self.data, self.data_host = None, None
        self.tree.clear()
        self.preview.clear()
        self.hint_for_host()
        self.update_buttons()
        if self.loaded_once and self.isVisible():
            self.refresh()
        else:
            self.loaded_once = False

    def hint_for_host(self):
        host = self.host()
        self.status.setText("" if host == LOCAL else tr("The agents of {host} (over SSH); imports go into its kits.",
                                                         host=self.page.hosts.label(host)))

    def refresh(self):
        self.loaded_once = True
        host = self.host()
        self.token += 1
        token = self.token
        self.status.setText(tr("Reading the agents' folders…") if host == LOCAL else
                            tr("Asking {host}…", host=self.page.hosts.label(host)))
        background(lambda: self.sai.host_agent_parts(host), owner=self, source=self.refresh_button, timeout=200,
                   done=lambda result, error: self.loaded(token, host, result, error))

    def loaded(self, token, host, result, error):
        if token != self.token or host != self.host():
            return
        if error is not None:
            self.data, self.data_host = None, None
            self.tree.clear()
            self.status.setText(tr("Cannot list the agents' parts: {error}", error=printable_text(error)))
            self.update_buttons()
            return
        self.data, self.data_host = result, host
        parts = [p for p in result.get("parts", []) if isinstance(p, dict)]
        self.status.setText(tr("{count} part(s) of the agents on {host}; {importable} can be imported.",
                               count=len(parts), host=self.page.hosts.label(host),
                               importable=sum(1 for p in parts if p.get("importable"))))
        self.show_parts()

    def show_parts(self):
        """The tree: agent > kind > part, filtered; the selection kept where the same parts are still there."""
        chosen = {p["id"] for p in self.selected(importable_only=False)}
        self.tree.clear()
        if not self.data:
            self.update_buttons()
            return
        words = self.filter.text().strip().lower()
        parts = [p for p in self.data.get("parts", []) if isinstance(p, dict) and isinstance(p.get("id"), str)]
        for agent in self.data.get("agents", []):
            if not isinstance(agent, dict):
                continue
            mine = [p for p in parts if p.get("agent") == agent.get("agent") and (
                not words or words in f"{p.get('id')} {p.get('description') or ''}".lower())]
            if not mine:
                continue
            top = QTreeWidgetItem([f"{printable(agent.get('title') or agent.get('agent'))}",
                                   printable(agent.get("path") or "")])
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(top)
            for kind in KIND_TITLES:
                of_kind = [p for p in mine if p.get("kind") == kind]
                if not of_kind:
                    continue
                group = QTreeWidgetItem([tr(KIND_TITLES[kind]), ""])
                group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                top.addChild(group)
                for part in of_kind:
                    about = "; ".join([*([printable_text(part["description"])] if part.get("description") else []),
                                       *flags(part)])
                    item = QTreeWidgetItem([printable(part.get("name", "")), about])
                    item.setData(0, PART, part)
                    item.setToolTip(0, safe_tooltip(printable(part["id"])))
                    item.setToolTip(1, safe_tooltip(printable_text(part.get("why") or about)))
                    if not part.get("importable"):
                        font = item.font(0)
                        font.setItalic(True)
                        item.setFont(0, font)
                    group.addChild(item)
                    item.setSelected(part["id"] in chosen)
            top.setExpanded(True)
            for index in range(top.childCount()):
                top.child(index).setExpanded(True)
        tables.fit_columns(self.tree, force=True)
        self.update_buttons()

    # -- selection and preview --

    def selected(self, importable_only=True):
        parts = []
        for item in self.tree.selectedItems():
            part = item.data(0, PART)
            if isinstance(part, dict) and (part.get("importable") or not importable_only):
                parts.append(part)
        return parts

    def update_buttons(self):
        ready = self.data is not None and self.data_host == self.host() and not self.page.task.running
        chosen = self.selected()
        self.import_button.setEnabled(ready and any(p.get("kind") not in ("output-style",) for p in chosen))
        self.memory_button.setEnabled(ready and any(p.get("kind") in MEMORY_KINDS for p in chosen))
        why = (tr("Select parts that can be imported (Ctrl, Shift for several)") if ready and not chosen else "")
        for button in (self.import_button, self.memory_button):
            button.setStatusTip(why)

    def selection_changed(self):
        self.update_buttons()
        items = self.tree.selectedItems()
        part = items[-1].data(0, PART) if items else None
        if not isinstance(part, dict):
            return
        host = self.host()
        self.preview_token += 1
        token = self.preview_token
        self.preview.setPlainText(tr("Reading {part}…", part=printable(part["id"])))
        background(lambda: self.sai.host_agent_part(host, part["id"]), owner=self, timeout=200,
                   done=lambda result, error: self.previewed(token, host, result, error))

    def previewed(self, token, host, result, error):
        if token != self.preview_token or host != self.host():
            return
        if error is not None:
            self.preview.setPlainText(tr("Cannot show the part: {error}", error=printable_text(error)))
            return
        self.preview.setPlainText(preview_text(result))

    # -- imports --

    def chosen_or_say(self, memory=False):
        """The importable parts selected (memory parts only with MEMORY), or None (said)."""
        parts = [p for p in self.selected() if not memory or p.get("kind") in MEMORY_KINDS]
        if not parts:
            warn(self, tr("Kits & plugins"), tr("Select the memory parts to add first.") if memory else
                 tr("Select the parts to import first: nothing is imported without a choice."))
            return None
        if self.data_host != self.host():
            warn(self, tr("Kits & plugins"), tr("The parts shown are not those of the host chosen: Read again first."))
            return None
        return parts

    def note_for(self, parts):
        lines = [tr("{count} part(s): {names}", count=len(parts), names=", ".join(printable(p["id"]) for p in parts[:8])
                    + (" …" if len(parts) > 8 else ""))]
        if any(p.get("private") for p in parts):
            lines.append(tr("Memory and instructions files may hold private notes about you and your projects."))
        if any(p.get("kind") == "plugin-module" for p in parts):
            lines.append(tr("A plugin module goes into the kit's hooks: it runs only once you have reviewed and "
                            "approved them (Review and approve hooks…)."))
        if any(p.get("scripts") for p in parts):
            lines.append(tr("Some skills hold scripts the agent may run when it uses them: look at them in the "
                            "preview first."))
        return "\n".join(lines)

    def import_into_kit(self):
        if self.page.busy_change():
            return
        parts = self.chosen_or_say()
        if parts is None:
            return
        host = self.host()
        secrets = flagged(parts)
        dialog = ImportDialog(self, tr("Import into a kit"), self.note_for(parts), kits=self.page.user_kits(),
                              host=host, secrets=secrets, sai=self.sai, findings=hidden_text(parts))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        kit, new = dialog.kit_choice()
        new = new and kit not in self.page.user_kits()
        if not self.page.valid_name(kit) or kit in ("default", "built-in"):
            return
        ids, agents, allow = [p["id"] for p in parts], dialog.agents.chosen(), dialog.secrets.isChecked()
        findings = dialog.findings.isChecked()
        if not ask_first(self, "kit-add", tr("Import into a kit"), on_host(host, tr(
                "Copy {count} part(s) into the kit {kit}? Every later start with this kit gets them: {names}",
                count=len(ids), kit=printable(kit), names=", ".join(printable(i) for i in ids[:8]))), tr("Import")):
            return
        self.page.start_change(lambda: self.sai.host_import_parts(host, kit, ids, new=new, agents=agents,
                                                                  allow_secrets=allow, allow_findings=findings),
                               "sandbox-ai kits host-import", tr("Importing {count} part(s) into {kit}…",
                                                                  count=len(ids), kit=printable(kit)), host=host)

    def import_into_memory(self):
        if self.page.busy_change():
            return
        parts = self.chosen_or_say(memory=True)
        if parts is None:
            return
        host = self.host()
        project = self.page.project if host == LOCAL and self.page.project else ""
        dialog = ImportDialog(self, tr("Add to a project's memory"), self.note_for(parts) + "\n" + tr(
            "sandbox-ai keeps memory per project (its first workspace): every later start and resumed session of it "
            "gets these files; a session running now keeps its own copy until it ends. Claude's auto memory is "
            "added as files (with a line in MEMORY.md), anything else is appended to the agent's memory file; a file "
            "of that name with other content is never replaced."), project=project, host=host,
            secrets=flagged(parts), sai=self.sai, findings=hidden_text(parts))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        folder = dialog.project.text().strip()
        if not folder.startswith("/"):
            warn(self, tr("Add to a project's memory"), tr("Give the project's folder as an absolute path."))
            return
        ids, agents, allow = [p["id"] for p in parts], dialog.agents.chosen(), dialog.secrets.isChecked()
        findings = dialog.findings.isChecked()
        if not ask_first(self, "memory-add", tr("Add to a project's memory"), on_host(host, tr(
                "Add {count} memory part(s) to the memory of {project}? Every later start of that project gets them: "
                "{names}", count=len(ids), project=printable(folder), names=", ".join(printable(i) for i in ids[:8]))),
                tr("Add")):
            return
        self.page.start_change(lambda: self.sai.host_import_memory(host, folder, ids, agents=agents,
                                                                   allow_secrets=allow, allow_findings=findings),
                               "sandbox-ai kits host-import --project", tr("Adding {count} part(s) to the memory of "
                                                                           "{project}…", count=len(ids),
                                                                           project=printable(folder)), host=host)
