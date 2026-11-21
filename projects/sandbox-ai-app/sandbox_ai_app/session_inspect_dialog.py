"""Memory… and Session files… of the Sandboxes page (buttons and a start's right-click menu): a start's agent memory and
the agents' own session files, read only (`sandbox-ai session memory|files` on the start's host).

The files are listed on the left (name, size, when changed), the chosen one's text on the right; Find looks for a
text in it (Enter: the next one); Export… writes the start's folder - its records and its kept HOME - as a new tar.gz
of this machine (`sandbox-ai session export`), logins and keys left out and listed. Memory: the live copy in the HOME
while the start runs, else what went back to the project's memory store (the dialog says which; Which copy chooses).
Session files: one shown as a conversation (user, assistant, tool calls and results with their times; long tool
output shortened unless In full).

Logins and keys (user 2026-11-19: always visible by default): a file that looks like one is listed like the others,
its column Login or key saying why; selecting it asks first (show-secret-file) and only then reads it with
--show-secret, a warning above its text - once per file while the dialog is open. Settings > Privacy: Hide logins and
keys in file views leaves them out (the status line says how many). Export… leaves them out and names them; With
logins and keys takes them in, asked first (session-export-secrets), unticked again after each export.

Everything shown was written by the agent: names through printable(), texts through printable_text() into plain-text
widgets (never rich text). Every command runs in the background; the window never waits for one."""
from __future__ import annotations

import os

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QAbstractButton, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QPushButton, QSplitter, QVBoxLayout,
)

from . import applog
from . import widgets
from .files import printable, printable_text
from .i18n import N_, tr
from .name_fields import alive
from .widgets import ask_first, background, plain_label, safe_tooltip, set_enabled

# What the marks of a login or key say (session_inspect.py's reasons), translated where they are shown.
SECRET_WHY = {"an agent's login": N_("an agent's login"),
              "a login, token or key (by its name)": N_("a login, token or key (by its name)"),
              "an agent's provider keys": N_("an agent's provider keys"),
              "holds a private key": N_("holds a private key"),
              "holds a placeholder of a secret the launcher gave": N_("holds a placeholder of a secret the launcher gave")}


def secret_why(why):
    """WHY (a file's "secret" mark) as shown: translated when it is one of SECRET_WHY, else as it came, printable."""
    return tr(SECRET_WHY[why]) if why in SECRET_WHY else printable_text(str(why or "")).replace("\n", " ")


ROLES = {"user": N_("user"), "assistant": N_("assistant"), "system": N_("system"), "reasoning": N_("reasoning"),
         "tool-call": N_("tool call"), "tool-result": N_("tool result"), "event": N_("event")}
SOURCES = (("auto", N_("automatic: the HOME while it runs, else the store")), ("home", N_("the copy in its HOME")),
           ("store", N_("the project's memory store")))


def size_text(value):
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def when(text):
    return printable(str(text or "")).replace("T", " ").replace("Z", "")[:19]


def conversation_text(found):
    """The messages of `session files --file F --json` as plain text: a line per message's head (time, role, tool),
    then its text."""
    messages = found.get("messages") if isinstance(found.get("messages"), list) else []
    blocks = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = tr(ROLES.get(message.get("role"), str(message.get("role") or "")))
        tool_name = message.get("tool")  # (a name on the head line: one line, printable)
        tool = f": {printable(tool_name)}" if isinstance(tool_name, str) else ""
        error = f" ({tr('error')})" if message.get("error") else ""
        blocks.append(f"== {when(message.get('time')) or '-'}  {role}{tool}{error}\n"
                      f"{printable_text(message.get('text') or '')}")
    return "\n\n".join(blocks) if blocks else tr("Nothing to show as a conversation.")


class SessionInspectDialog(QDialog):
    """The memory (KIND "memory") or the session files (KIND "files") of start NAME on HOST. EXPORTABLE: the start
    keeps its folder on this machine (Export… writes it here); else Export… says why not."""

    def __init__(self, parent, sai, name, kind="memory", host="local", exportable=False, export_why=None, owner=None):
        super().__init__(parent)
        self.sai, self.name, self.kind, self.host = sai, name, kind, host or "local"
        self.owner, self.busy, self.rows, self.current, self.source = owner, False, {}, None, "auto"
        self.allowed = set()  # the files marked as a login or key shown after the question, while the dialog is open
        self.exportable, self.export_why = exportable, export_why
        memory = kind == "memory"
        where = "" if self.host == "local" else " " + tr("on {host}", host=printable(self.host))
        self.setWindowTitle((tr("Memory of {name}", name=printable(name)) if memory else
                             tr("Session files of {name}", name=printable(name))) + where)
        layout = QVBoxLayout(self)
        self.where = plain_label("", role="secondary", selectable=True)
        layout.addWidget(self.where)
        row = QHBoxLayout()
        self.which = None
        if memory:
            self.which = QComboBox()
            self.which.setAccessibleName(tr("Which copy"))
            for key, text in SOURCES:
                self.which.addItem(tr(text), key)
            self.which.currentIndexChanged.connect(lambda _index: self.source_changed())
            row.addWidget(self.which)
        self.full = None
        if not memory:
            self.full = QCheckBox(tr("In full"))
            self.full.setToolTip(safe_tooltip(tr("Tool input and output in full, not shortened ({command})",
                                                 command="--full")))
            self.full.toggled.connect(lambda _on: self.reopen())
            row.addWidget(self.full)
        row.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Find in the text"))
        self.search.setAccessibleName(tr("Find in the text"))
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(lambda: self.find())
        self.search.textChanged.connect(lambda _text: self.update_buttons())
        row.addWidget(self.search)
        self.find_button = QPushButton(tr("Find"))
        self.find_button.clicked.connect(lambda _=False: self.find())
        row.addWidget(self.find_button)
        layout.addLayout(row)
        split = QSplitter()
        headers = [tr("File"), tr("Size"), tr("Modified"), tr("Login or key")] if memory else \
            [tr("File"), tr("Agent"), tr("Size"), tr("Modified"), tr("Login or key")]
        self.table = widgets.table(headers, stretch=0, numeric=(len(headers) - 3,), compact=(len(headers) - 2,))
        self.table.setAccessibleName(tr("Files"))
        for corner in self.table.findChildren(QAbstractButton):
            corner.setAccessibleName(tr("Select all"))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        split.addWidget(self.table)
        self.text = QPlainTextEdit()  # plain text: what the agent wrote is never read as markup
        self.text.setReadOnly(True)
        self.text.setAccessibleName(tr("Text"))
        self.text.setPlainText(tr("Choose a file."))
        split.addWidget(self.text)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)
        self.status = plain_label("", role="secondary", selectable=True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.export_button = QPushButton(tr("Export…"))
        self.export_button.setAccessibleName(tr("Export the start's folder"))
        self.export_button.setToolTip(safe_tooltip(tr(
            "Write the start's folder - its records and its HOME as its agent left it - as a new archive; logins and "
            "keys are left out and listed ({command})", command="sandbox-ai session export")))
        self.export_button.clicked.connect(lambda _=False: self.export())
        self.with_secrets = QCheckBox(tr("With logins and keys"))
        self.with_secrets.setAccessibleName(tr("Export with logins and keys"))
        self.with_secrets.setToolTip(safe_tooltip(tr(
            "Take the logins, keys and secrets of its HOME into the archive too (asked first; {command})",
            command="--include-secrets")))
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.load())
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.with_secrets)
        buttons.addStretch(1)
        buttons.addWidget(self.refresh_button)
        layout.addLayout(buttons)
        # A right click on a file: the buttons' commands, as they are enabled.
        widgets.list_menu(self.table, lambda: [self.find_button, None, self.export_button, self.refresh_button])
        box = QDialogButtonBox()
        close = QPushButton(tr("Close"))
        close.clicked.connect(lambda _=False: self.reject())
        box.addButton(close, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(box)
        self.resize(980, 620)
        self.update_buttons()
        self.load()

    # --- state -------------------------------------------------------------------------------------------------

    def update_buttons(self):
        if not alive(self):
            return
        running = tr("A command runs.") if self.busy else None
        export_why = running or (None if self.exportable else self.export_why or tr(
            "Only a kept start of this machine has a folder to export here."))
        set_enabled(self.export_button, export_why is None, export_why)
        set_enabled(self.with_secrets, export_why is None, export_why)
        set_enabled(self.refresh_button, not self.busy, running)
        set_enabled(self.find_button, bool(self.search.text().strip()), tr("Type a text to find."))

    def started(self, text):
        self.busy = True
        self.status.setText(text)
        self.update_buttons()

    def ended(self):
        self.busy = False
        self.update_buttons()

    def selected(self):
        keys = widgets.selected_keys(self.table)
        return self.rows.get(keys[0]) if keys else None

    # --- the list ----------------------------------------------------------------------------------------------

    def source_changed(self):
        self.source = self.which.currentData() or "auto"
        self.load()

    def load(self):
        self.started(tr("Reading…"))
        name, host, source = self.name, self.host, self.source
        if self.kind == "memory":
            background(lambda: self.sai.session_memory(name, host, source), owner=self.owner,
                       what="sandbox-ai session memory", done=self.loaded)
        else:
            background(lambda: self.sai.session_files(name, host), owner=self.owner,
                       what="sandbox-ai session files", done=self.loaded)

    def loaded(self, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not read: {error}", error=printable_text(error)))
            return
        files = [item for item in (result.get("files") or []) if isinstance(item, dict)
                 and isinstance(item.get("name"), str)]
        self.rows = {item["name"]: item for item in files}
        if self.kind == "memory":
            path = printable(str(result.get("path") or ""))
            self.where.setText(tr("Its memory as the agent has it now, in its HOME: {path}", path=path)
                               if result.get("source") == "home" else
                               tr("What went back to the project's memory store: {path}", path=path))
            rows = [[printable(item["name"]), size_text(item.get("size")), when(item.get("modified")),
                     secret_why(item["secret"]) if item.get("secret") else ""] for item in files]
        else:
            self.where.setText(tr("The agents' session files in its HOME: {path}",
                                  path=printable(str(result.get("home") or ""))))
            rows = [[printable(item["name"]), printable(str(item.get("agent") or "")) +
                     ("" if item.get("verified") is not False else " *"),
                     size_text(item.get("size")), when(item.get("modified")),
                     secret_why(item["secret"]) if item.get("secret") else ""] for item in files]
        widgets.fill(self.table, rows, keys=[item["name"] for item in files])
        notes = [printable(str(note)) for note in result.get("notes") or [] if isinstance(note, str)]
        hidden = result.get("hidden")
        if isinstance(hidden, int) and not isinstance(hidden, bool) and hidden > 0:
            notes.append(tr("{count} files that hold a login or key are not listed (Settings: Hide logins and keys in "
                            "file views).", count=hidden))
        if self.kind != "memory" and any(item.get("verified") is False for item in files):
            notes.append(tr("* where this agent keeps its sessions is not verified against its source: its files are "
                            "listed as they look"))
        counted = tr("{count} files.", count=len(files)) if files else (
            tr("No memory files yet.") if self.kind == "memory" else tr("No session files yet."))
        self.status.setText("\n".join([counted, *notes]))
        if self.current in self.rows:
            self.open(self.rows[self.current])

    def selection_changed(self):
        chosen = self.selected()
        if chosen is not None and chosen["name"] != self.current:
            self.open(chosen)

    def reopen(self):
        if self.current in self.rows:
            self.open(self.rows[self.current])

    # --- one file ----------------------------------------------------------------------------------------------

    def open(self, item):
        self.current = item["name"]
        name, host, file = self.name, self.host, item["name"]
        secret = bool(item.get("secret"))
        if secret and file not in self.allowed:
            why = secret_why(item["secret"])
            self.text.setPlainText(tr("{file} is marked as a login or key ({why}): its text is shown only when you "
                                      "ask.", file=printable(file), why=why))
            if not ask_first(self, "show-secret-file", tr("Show a login or key"), tr(
                    "Show the text of {file} ({why})? Anyone who sees this screen can read the login or key in it.",
                    file=printable(file), why=why), tr("Show it")):
                return
            self.allowed.add(file)
        self.text.setPlainText(tr("Reading…"))
        if self.kind == "memory":
            source = self.source
            background(lambda: self.sai.session_memory(name, host, source, file, show_secret=secret), owner=self.owner,
                       what="sandbox-ai session memory --file", done=lambda result, error: self.shown(file, result, error))
        else:
            full = bool(self.full and self.full.isChecked())
            background(lambda: self.sai.session_files(name, host, file, full, show_secret=secret), owner=self.owner,
                       what="sandbox-ai session files --file", done=lambda result, error: self.shown(file, result, error))

    def shown(self, file, result, error):
        if not alive(self) or file != self.current:
            return
        if error is not None:
            self.text.setPlainText(tr("Not read: {error}", error=printable_text(error)))
            return
        if self.kind == "memory":
            entry = next((item for item in result.get("files") or [] if isinstance(item, dict)), {})
            text = tr("(binary: not shown)") if entry.get("binary") else \
                printable_text(entry["text"]) if isinstance(entry.get("text"), str) else tr("(not shown)")
        else:
            text = conversation_text(result)
            notes = [printable(str(note)) for note in result.get("notes") or [] if isinstance(note, str)]
            if notes:
                text += "\n\n" + "\n".join(f"{tr('note')}: {note}" for note in notes)
        item = self.rows.get(file) or {}
        if item.get("secret"):
            text = tr("Warning: it holds a login or key ({why}) - do not pass this on.",
                      why=secret_why(item["secret"])) + "\n\n" + text
        self.text.setPlainText(text)
        if self.search.text().strip():
            self.find(again=False)

    def find(self, again=True):
        """The next place of the search's text (from the start when there is none further, or AGAIN is False)."""
        needle = self.search.text().strip()
        self.update_buttons()
        if not needle:
            return
        if not again:
            self.text.moveCursor(self.text.textCursor().MoveOperation.Start)
        if not self.text.find(needle, QTextDocument.FindFlag(0)):
            self.text.moveCursor(self.text.textCursor().MoveOperation.Start)
            if not self.text.find(needle, QTextDocument.FindFlag(0)):
                self.status.setText(tr("Not found: {text}", text=printable(needle)))

    # --- export ------------------------------------------------------------------------------------------------

    def export(self):
        if not self.exportable:
            return
        from .state import choose_save
        path = choose_save(self, tr("Export"), f"{self.name}.tar.gz", tr("Archives (*.tar.gz)"), "session-export")
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.lexists(path):
            self.status.setText(tr("{path} is there already: choose a new file (nothing is overwritten).",
                                   path=printable(path)))
            return
        include = self.with_secrets.isChecked()
        if include:
            asked = ask_first(self, "session-export-secrets", tr("Export with logins and keys"), tr(
                "Write the folder of {name} - its records and its HOME as its agent left it, WITH its logins, keys "
                "and secrets - into {path}? Whoever has the archive can log in as you with them.",
                name=printable(self.name), path=printable(path)), tr("Export with logins and keys"))
        else:
            asked = ask_first(self, "session-export", tr("Export"), tr(
                "Write the folder of {name} - its records and its HOME as its agent left it (transcripts, memory, "
                "what it kept) - into {path}? Logins and keys are left out.", name=printable(self.name),
                path=printable(path)), tr("Export"))
        if not asked:
            return
        self.with_secrets.setChecked(False)  # (each export with logins and keys is chosen anew)
        applog.log("INFO", "session", f"export of {printable(self.name)} to {printable(path)}"
                                      + (" with its logins and keys" if include else ""))
        self.started(tr("Writing {path}…", path=printable(path)))
        name = self.name
        background(lambda: self.sai.session_export(name, path, include_secrets=include), owner=self.owner, work=True,
                   what="sandbox-ai session export", done=self.exported)

    def exported(self, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not exported: {error}", error=printable_text(error)))
            return
        everything = [item for item in result.get("left_out") or [] if isinstance(item, dict)]
        secrets = [item for item in everything if item.get("secret") is True]
        left = [item for item in everything if item.get("secret") is not True]
        lines = [tr("{count} files ({size}) written to {path}", count=result.get("files") or 0,
                    size=size_text(result.get("bytes")), path=printable(str(result.get("path") or "")))]
        if secrets:
            lines.append(tr("Logins, keys and secrets left out ({count}) - the archive is made to be passed on; tick "
                            "With logins and keys to take them in:", count=len(secrets)))
            lines += [f"  {printable(str(item.get('path')))}: {secret_why(item.get('why'))}" for item in secrets[:50]]
            if len(secrets) > 50:
                lines.append(tr("… and {count} more", count=len(secrets) - 50))
        elif result.get("include_secrets") is True:
            lines.append(tr("Its logins, keys and secrets are in the archive: whoever has it can log in as you."))
        if left:
            lines.append(tr("Left out ({count}):", count=len(left)))
            lines += [f"  {printable(str(item.get('path')))}: {printable(str(item.get('why')))}" for item in left[:50]]
            if len(left) > 50:
                lines.append(tr("… and {count} more", count=len(left) - 50))
        self.status.setText("\n".join(lines))


def open_dialog(parent, sai, name, kind, host="local", exportable=False, export_why=None, owner=None):
    """The dialog for start NAME, shown until it is closed, then dropped (made anew each time)."""
    dialog = SessionInspectDialog(parent, sai, name, kind=kind, host=host, exportable=exportable,
                                  export_why=export_why, owner=owner)
    dialog.exec()
    widgets.drop_after_exec(dialog)
    return dialog

