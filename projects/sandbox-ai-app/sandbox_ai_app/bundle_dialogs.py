"""Export… and Import… of the Board page and the Sessions page: a board or a session as one self-sufficient file (a
bundle), and such a file taken in (`sandbox-ai board export|import`).

ExportDialog writes the bundle of a session (of this machine or a host) or of the board shown: where to (a file of this
machine), and whether the images go along (--images: a larger file that works offline). It says what the file holds:
this machine's folder paths and names, never a secret's value - logins and secrets are named only.

ImportDialog takes a bundle in as passed on, not as yours: Check first (`board import --check --json`: nothing is
written), which shows the plan - each folder with the bundle's path as its default, to change here (--map), the values
it withheld to give (--value), what is created, reused, replaced, what this machine lacks (secrets, identities, VPN
profiles, images), what the bundle left out - then Import, asked first (board-import), which writes its parts and keeps
the board (never started): IMPORTED(board document, host) shows it on the Board page. The commands run in the
background; the window never waits for them."""
from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from . import applog
from . import backend
from .files import printable, printable_text
from .i18n import N_, tr
from .name_fields import alive
from .widgets import (
    ask_first, background, explanation, output_text, plain_label, safe_tooltip, secret_value, set_enabled,
)

PRIVACY = N_("The file holds this machine's folder paths and names (servers, hosts, the session's): pass it on "
             "knowingly. Never in it: a secret's value, a token, a login, the agent's login files, cookies or a HOME's "
             "contents - secrets and logins are named only, to give where it is imported.")
OUTPUT_LINES = 400


def safe_name(text):
    """A file name of TEXT (a session's or board's name): letters, digits, '-', '_' and '.'."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text or "board")).strip("-.") or "board"
    return name[:60]


class ExportDialog(QDialog):
    """The bundle of session SESSION (of HOST) or of the board document BOARD, written where the human chooses."""

    def __init__(self, parent, sai, title_name, session=None, board=None, host="local", owner=None):
        super().__init__(parent)
        self.sai, self.session, self.board, self.host = sai, session, board, host or "local"
        self.owner, self.running, self.written = owner, False, None
        self.setWindowTitle(tr("Export {name}", name=printable(title_name)))
        layout = QVBoxLayout(self)
        layout.addWidget(explanation(
            tr("One file with everything its start needs that exists only here: its options, the board, the MCP and "
               "LLM servers, rulesets, kits, presets and image recipes it uses. Folders become placeholders, asked "
               "for where it is imported (their contents are not in it).")))
        self.privacy = plain_label(tr(PRIVACY), role="secondary")
        layout.addWidget(self.privacy)
        form = QFormLayout()
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        default = os.path.join(os.path.expanduser("~"), f"{safe_name(title_name)}.sandbox-ai-bundle.json")
        self.path = QLineEdit(default)
        self.path.setAccessibleName(tr("Bundle file"))
        self.path.setToolTip(safe_tooltip(tr("Where the bundle is written (a file of this machine)")))
        self.path.textChanged.connect(lambda _text: self.update_buttons())
        line.addWidget(self.path, 1)
        browse = QToolButton()
        browse.setText("…")
        browse.setAccessibleName(tr("Choose…"))
        browse.setToolTip(tr("Choose where the bundle is written"))
        browse.clicked.connect(lambda _=False: self.choose())
        line.addWidget(browse)
        form.addRow(tr("Bundle file"), row)
        layout.addLayout(form)
        self.images = QCheckBox(tr("Include images"))
        self.images.setToolTip(safe_tooltip(tr(
            "Also the images of its MCP servers and services (podman save): the bundle works offline, as a tar "
            "archive - a much larger file. The agents' own images never: they are built on each machine (--images)")))
        self.images.toggled.connect(lambda on: self.images_toggled(on))
        layout.addWidget(self.images)
        self.status = plain_label("", role="secondary", selectable=True)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setAccessibleName(tr("What sandbox-ai said"))
        self.output.setMaximumBlockCount(OUTPUT_LINES)
        self.output.hide()
        layout.addWidget(self.output, 1)
        buttons = QDialogButtonBox()
        self.export_button = QPushButton(tr("Export"))
        self.export_button.setAccessibleName(tr("Export"))
        self.export_button.setToolTip(safe_tooltip(tr("Write the bundle ({command})",
                                                      command="sandbox-ai board export")))
        self.export_button.clicked.connect(lambda _=False: self.run())
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(lambda _=False: self.reject())
        buttons.addButton(self.export_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.resize(620, 360)
        self.update_buttons()

    def images_toggled(self, on):
        text = self.path.text().strip()
        if on and text.endswith(".json"):
            self.path.setText(text[:-len(".json")] + ".tar")
        elif not on and text.endswith(".tar"):
            self.path.setText(text[:-len(".tar")] + ".json")

    def choose(self):
        from .state import choose_save
        path = choose_save(self, tr("Export"), os.path.basename(self.path.text().strip()) or "board.json",
                           tr("Bundles (*.json *.tar)"), "bundle")
        if path:
            self.path.setText(path)

    def update_buttons(self):
        if not alive(self):
            return
        path = os.path.expanduser(self.path.text().strip())
        why = (tr("A command runs.") if self.running else
               tr("Choose where the bundle is written.") if not path or not os.path.isabs(path) else
               tr("That is a folder: give a file's name.") if os.path.isdir(path) else None)
        set_enabled(self.export_button, why is None, why)

    def run(self):
        path = os.path.expanduser(self.path.text().strip())
        try:
            backend.board_export_args(path, self.session, self.board is not None, self.host, self.images.isChecked())
        except ValueError as exc:
            self.show_output(tr("Not exported: {error}", error=printable_text(exc)))
            return
        if os.path.exists(path) and not ask_first(self, "files-replace", tr("Export"),
                                                  tr("{path} exists. Replace it with the bundle?",
                                                     path=printable(path)), tr("Replace")):
            return
        what = f"session {self.session}" if self.session else "the board"
        applog.log("INFO", "bundle", f"export of {printable(what)}"
                   + (f" of {printable(self.host)}" if self.host != "local" else "")
                   + f" to {printable(path)}" + (" with its images" if self.images.isChecked() else ""))
        self.running = True
        self.status.setText(tr("Exporting… (with images this can take minutes)"))
        self.update_buttons()
        background(self.sai.board_export, path, self.session, self.board, self.host, self.images.isChecked(),
                   owner=self.owner, work=True, what=f"sandbox-ai board export of {what}",
                   source=[self.export_button], timeout=backend.BUNDLE_SECONDS + 60,
                   done=lambda result, error: self.ran(path, result, error))

    def ran(self, path, result, error):
        status, out, err = result if error is None else (1, "", str(error))
        applog.log("INFO" if status == 0 else "WARNING", "bundle", f"export to {printable(path)}: exit status {status}"
                   + (f"\n{applog.tail(err or out)}" if status != 0 else ""))
        if not alive(self):
            return
        self.running = False
        self.update_buttons()
        if status != 0:
            self.status.setText(tr("Not exported (exit {status}): see what sandbox-ai said below.", status=status))
            self.show_output((err or out).strip())
            return
        self.written = path
        lines = [tr("Written: {path}", path=printable(path))]
        try:
            import json
            summary = json.loads(out)
            if summary.get("secrets"):
                lines.append(tr("Secrets named (never their values): {names}",
                                names=", ".join(printable(s) for s in summary["secrets"])))
            for text in summary.get("withheld") or []:
                lines.append(tr("Not in it: {what}", what=printable(text)))
            if summary.get("folders"):
                lines.append(tr("Folders asked for where it is imported: {count}", count=len(summary["folders"])))
        except (ValueError, TypeError, AttributeError, RecursionError):
            pass
        self.status.setText(tr("Exported."))
        self.show_output("\n".join(lines))

    def show_output(self, text):
        self.output.show()
        self.output.setPlainText(output_text(text))


class ImportDialog(QDialog):
    """A bundle taken in on HOST: Check shows the plan, Import (asked first) writes it; IMPORTED(document, host) after
    an import that succeeded."""

    def __init__(self, parent, sai, host="local", path=None, owner=None, imported=None):
        super().__init__(parent)
        self.sai, self.host, self.owner, self.imported = sai, host or "local", owner, imported
        # PLAN: the last check's, until a choice changes (Import follows a plan checked as chosen); LAST_PLAN stays
        # (what it named to replace is still offered)
        self.plan, self.last_plan, self.running, self.done_with = None, None, False, None
        self.folder_edits, self.value_edits = {}, {}
        self.setWindowTitle(tr("Import a board"))
        layout = QVBoxLayout(self)
        layout.addWidget(explanation(
            tr("A bundle from elsewhere is passed on, not yours: what a passed-on preset may not carry is refused, and "
               "nothing is written before you see its plan and confirm it. The board is not started.")))
        if self.host != "local":
            layout.addWidget(plain_label(tr("It is imported on {host}.", host=printable(self.host)), role="secondary"))
        form = QFormLayout()
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit(path or "")
        self.path.setAccessibleName(tr("Bundle file"))
        self.path.textChanged.connect(lambda _text: self.forget_plan())
        line.addWidget(self.path, 1)
        browse = QToolButton()
        browse.setText("…")
        browse.setAccessibleName(tr("Choose…"))
        browse.setToolTip(tr("Choose the bundle"))
        browse.clicked.connect(lambda _=False: self.choose())
        line.addWidget(browse)
        form.addRow(tr("Bundle file"), row)
        self.name = QLineEdit()
        self.name.setAccessibleName(tr("Board's name"))
        self.name.setPlaceholderText(tr("the bundle's"))
        self.name.setToolTip(safe_tooltip(tr("The board's name here (--as; lower-case letters, digits, _ and -)")))
        self.name.setMaxLength(40)
        self.name.textChanged.connect(lambda _text: self.forget_plan())
        form.addRow(tr("Board's name"), self.name)
        layout.addLayout(form)
        self.images = QCheckBox(tr("Load its images"))
        self.images.toggled.connect(lambda _on: self.forget_plan())
        self.images.setToolTip(safe_tooltip(tr("Load the images the bundle carries into Podman here, each archive "
                                               "checked first (--images)")))
        layout.addWidget(self.images)
        self.replace = QCheckBox(tr("Replace the parts of the same name here"))
        self.replace.toggled.connect(lambda _on: self.forget_plan())
        layout.addWidget(self.replace)
        self.unsigned = QCheckBox(tr("Take MCP servers of an unsigned catalog"))
        self.unsigned.setToolTip(safe_tooltip(tr(
            "An MCP server the bundle names from a catalog whose signature is not checked is refused unless chosen "
            "here (--unsigned, as sandbox-ai mcp import takes it)")))
        self.unsigned.toggled.connect(lambda _on: self.forget_plan())
        self.replace.setToolTip(safe_tooltip(tr(
            "A part of that name here that is defined otherwise (an MCP or LLM server, a kit, a preset, the board) is "
            "replaced by the bundle's (--replace, asked first); without, the import is refused")))
        layout.addWidget(self.unsigned)
        self.inputs = QWidget()
        self.inputs_form = QFormLayout(self.inputs)
        self.inputs_form.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.inputs)
        scroll.setMaximumHeight(220)
        scroll.setAccessibleName(tr("Folders and values of the bundle"))
        layout.addWidget(scroll)
        self.status = plain_label(tr("Choose a bundle, then Check."), role="secondary", selectable=True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setAccessibleName(tr("The import's plan"))
        self.output.setMaximumBlockCount(OUTPUT_LINES)
        self.output.setMinimumHeight(140)
        layout.addWidget(self.output, 1)
        buttons = QDialogButtonBox()
        self.check_button = QPushButton(tr("Check"))
        self.check_button.setAccessibleName(tr("Check"))
        self.check_button.setToolTip(safe_tooltip(tr("Show the import's plan: nothing is written (--check)")))
        self.check_button.clicked.connect(lambda _=False: self.run(check=True))
        self.import_button = QPushButton(tr("Import"))
        self.import_button.setAccessibleName(tr("Import"))
        self.import_button.setToolTip(safe_tooltip(tr("Write its parts and keep the board, after asking ({command})",
                                                      command="sandbox-ai board import")))
        self.import_button.clicked.connect(lambda _=False: self.run(check=False))
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(lambda _=False: self.reject())
        buttons.addButton(self.check_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.import_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.resize(700, 600)
        self.update_buttons()

    def choose(self):
        from .state import choose_file
        path = choose_file(self, tr("Import a board"), "bundle", tr("Bundles (*.json *.tar)"))
        if path:
            self.path.setText(path)

    def forget_plan(self):
        self.plan = None
        self.update_buttons()

    def update_buttons(self):
        if not alive(self):
            return
        path = os.path.expanduser(self.path.text().strip())
        why = (tr("A command runs.") if self.running else
               tr("Choose the bundle.") if not path or not os.path.isfile(path) else None)
        set_enabled(self.check_button, why is None, why)
        import_why = why or (tr("Done: imported as {name}.", name=printable(self.done_with)) if self.done_with else
                             tr("Check first: Import follows its plan.") if self.plan is None else None)
        set_enabled(self.import_button, import_why is None, import_why)

    # -- what is chosen --

    def choices(self):
        """board_import_args's keywords of what is chosen in the dialog."""
        maps = []
        for marker, (default, edit) in self.folder_edits.items():
            value = edit.text().strip()
            if value and value != default:
                maps.append((marker, value))
        values = [(name, edit.text()) for name, edit in self.value_edits.items() if edit.text()]
        replaces = []
        if self.replace.isChecked() and self.last_plan is not None:
            for item in self.last_plan.get("problems") or []:
                for kind, name in re.findall(r"--replace ([a-z]+):([A-Za-z0-9][A-Za-z0-9_.@-]{0,63})",
                                             str(item.get("fix") or "")):
                    replaces.append((kind, name))
            replaces += [(item.get("kind"), item.get("name")) for item in self.last_plan.get("replace") or []
                         if isinstance(item, dict) and item.get("kind") in backend.BUNDLE_KINDS
                         and isinstance(item.get("name"), str)]
        name = self.name.text().strip() or None
        return {"as_name": name, "maps": maps, "values": values, "replaces": list(dict.fromkeys(
            (kind, str(item).partition("@")[0]) for kind, item in replaces)), "images": self.images.isChecked(),
                "unsigned": self.unsigned.isChecked()}

    # -- the command --

    def run(self, check=True):
        path = os.path.expanduser(self.path.text().strip())
        choices = self.choices()
        try:
            backend.board_import_args(path, self.host, check=check, **choices)
        except ValueError as exc:
            self.status.setText(tr("Not checked: {error}", error=printable_text(exc)))
            return
        if not check:
            text = tr("Import the board of {path}: write its parts here (MCP and LLM servers, kits, presets, rulesets, "
                      "image recipes) and keep the board? It is not started.", path=printable(path))
            if choices["replaces"]:
                text += "\n" + tr("Replaced: {names}", names=", ".join(f"{kind} {printable(name)}"
                                                                         for kind, name in choices["replaces"]))
            if not ask_first(self, "board-import", tr("Import a board"), text, tr("Import")):
                return
        applog.log("INFO", "bundle", f"{'check of the ' if check else ''}import of {printable(path)}"
                   + (f" on {printable(self.host)}" if self.host != "local" else "")
                   + (f", folders changed: {len(choices['maps'])}" if choices["maps"] else "")
                   + (f", replacing {len(choices['replaces'])}" if choices["replaces"] else ""))
        self.running = True
        self.status.setText(tr("Checking…") if check else tr("Importing…"))
        self.update_buttons()
        host = self.host
        background(lambda: self.sai.board_import(path, host, check, **choices), owner=self.owner, work=not check,
                   what="sandbox-ai board import" + (" --check" if check else ""),
                   source=[self.check_button, self.import_button], timeout=backend.BUNDLE_SECONDS + 60,
                   done=lambda result, error: self.ran(check, result, error))

    def ran(self, check, result, error):
        if error is None and isinstance(result, dict):
            applog.log("INFO" if not result.get("problems") else "WARNING", "bundle",
                       f"{'check of the ' if check else ''}import: {len(result.get('problems') or [])} problem(s)"
                       + ("" if check or result.get("problems") else f", written as {printable(result.get('board'))}"))
        else:
            applog.log("WARNING", "bundle", f"import: {applog.error_text(error)}")
        if not check and error is None and isinstance(result, dict) and result.get("written") and \
                self.imported is not None and isinstance(result.get("board_document"), dict):
            self.imported(result["board_document"], self.host)  # (the Board page shows it, also when closed)
        if not alive(self):
            return
        self.running = False
        if error is not None or not isinstance(result, dict):
            self.status.setText(tr("It failed: {error}", error=printable_text(error)[:600]))
            self.update_buttons()
            return
        self.plan = self.last_plan = result
        if not check and result.get("written"):
            self.done_with = result.get("board")
            self.status.setText(tr("Imported as {name}: shown on the Board page (not started).",
                                   name=printable(result.get("board"))))
        elif result.get("problems"):
            self.status.setText(tr("Not importable as it is: resolve what is listed under Problems (a folder, a "
                                   "value, a name), then Check again."))
        else:
            self.status.setText(tr("It can be imported: Import writes what is listed."))
        self.fill_inputs(result)
        self.output.setPlainText(output_text(plan_text(result)))
        self.update_buttons()

    def fill_inputs(self, plan):
        """A field for each folder (its default the bundle's path) and each withheld value; what was typed stays."""
        typed = {marker: edit.text() for marker, (_default, edit) in self.folder_edits.items()}
        given = {name: edit.text() for name, edit in self.value_edits.items()}
        while self.inputs_form.rowCount():
            self.inputs_form.removeRow(0)
        self.folder_edits, self.value_edits = {}, {}
        for item in plan.get("folders") or []:
            if not isinstance(item, dict) or not isinstance(item.get("marker"), str):
                continue
            default = str(item.get("default") or "")
            edit = QLineEdit(typed.get(item["marker"], str(item.get("to") or default)))
            edit.setAccessibleName(tr("Folder {marker}", marker=printable(item["marker"])))
            edit.setToolTip(safe_tooltip(tr("{label}: the folder here (the bundle's: {path})",
                                            label=printable(item.get("label") or ""), path=printable(default))))
            edit.textChanged.connect(lambda _text: self.forget_plan())
            self.inputs_form.addRow(printable(f"{item['marker']} ({item.get('label') or ''})"), edit)
            self.folder_edits[item["marker"]] = (default, edit)
        for item in plan.get("values") or []:
            match = re.fullmatch(r"<text:([a-z0-9][a-z0-9-]{0,31})>", str(item.get("marker") or ""))
            if not match:
                continue
            edit = QLineEdit(given.get(match[1], ""))
            edit.setAccessibleName(tr("Value {marker}", marker=printable(item["marker"])))
            edit.setToolTip(safe_tooltip(tr("Withheld from the bundle (it looked like a secret): {label}",
                                            label=printable(item.get("label") or ""))))
            edit.textChanged.connect(lambda _text: self.forget_plan())
            # (hidden as typed, with its "Show value" toggle: widgets.secret_value, the one way a field hides a value)
            self.inputs_form.addRow(printable(f"{item['marker']} ({item.get('label') or ''})"), secret_value(edit))
            self.value_edits[match[1]] = edit


def plan_text(plan):
    """The plan of `board import --json` in words (every text of it printable: it comes from the bundle)."""
    lines = []
    source = plan.get("bundle") or {}
    origin = source.get("source") if isinstance(source.get("source"), dict) else {}
    lines.append(tr("Board {name} from {kind} {source} (sandbox-ai {version})", name=printable(plan.get("board")),
                    kind=printable(origin.get("kind") or "?"), source=printable(origin.get("name") or "?"),
                    version=printable(source.get("sandbox_ai") or "?")))
    for item in plan.get("folders") or []:
        lines.append(tr("Folder {marker}: {path}", marker=printable(item.get("marker")),
                        path=printable(item.get("to"))))
    sections = ((tr("Created"), "create"), (tr("Reused (the same is here)"), "reuse"), (tr("Replaced"), "replace"))
    for title, key in sections:
        items = [f"{printable(i.get('kind'))} {printable(i.get('name'))}" for i in plan.get(key) or []
                 if isinstance(i, dict)]
        if items:
            lines.append(f"{title}: " + ", ".join(items))
    missing = plan.get("missing") if isinstance(plan.get("missing"), dict) else {}
    for kind, items in missing.items():
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                lines.append(tr("Missing here ({kind}): {name} - {fix}", kind=printable(kind),
                                name=printable(item.get("name")), fix=printable(item.get("fix"))))
    for text in plan.get("withheld") or []:
        lines.append(tr("Not in the bundle: {what}", what=printable(text)))
    for text in plan.get("notes") or []:
        lines.append(tr("Note: {text}", text=printable(text)))
    for text in plan.get("warnings") or []:
        lines.append(tr("Warning: {text}", text=printable(text)))
    if plan.get("command"):
        lines.append(tr("Starts as: {command}", command=printable(plan["command"])))
    problems = [p for p in plan.get("problems") or [] if isinstance(p, dict)]
    if problems:
        lines.append(tr("Problems:"))
        for item in problems:
            lines.append("  - " + printable(item.get("text")) + (f" ({tr('resolve')}: {printable(item['fix'])})"
                                                                 if item.get("fix") else ""))
    return "\n".join(lines)
