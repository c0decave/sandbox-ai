"""Duplicate…, Export with state… and Import… of the Sessions page (and Import a session… of the Board): a session's
state - its configuration, HOME with the agent's conversation, memory, checkpoints and network approvals - as a second
session here (`sandbox-ai session duplicate NAME NEW`), as one archive (`sandbox-ai session export NAME OUT`), or an
archive taken as a session of this machine (`sandbox-ai session import FILE`).

Every command runs in the background; what it says is shown in the dialog. What carries a risk is asked first, with
the app's confirmation policy (ask_first): logins and keys along (session-export-secrets: whoever has them can log in
as you), the network approvals granted again (session-grant-approvals), and an import whose configuration gives the
sandbox secrets or folders of this machine (session-import: the list it said at its check). An import is always
checked first (its --check: the archive's checksums and the launcher's dry run, nothing kept) and shows what it starts
with before Import is offered."""
from __future__ import annotations

import os
import shlex

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QToolButton,
    QVBoxLayout, QWidget,
)

from . import applog
from . import backend
from .files import printable, printable_text
from .i18n import tr
from .name_fields import alive
from .widgets import ask_first, background, explanation, output_text, plain_label, safe_tooltip, set_enabled

OUTPUT_LINES = 400


def size_words(value):
    size = int(value or 0)
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if size >= 1 << shift:
            return f"{size / (1 << shift):.1f} {unit}"
    return f"{size} B"


def result_lines(result):
    """What an import (or a duplicate: its import) says it made or would make, in the app's words."""
    lines = []
    if result.get("imported"):
        lines.append(tr("Made: {name}. Continue it with Resume (the agent resumes its conversation).",
                        name=printable(result.get("name"))))
    elif result.get("checked"):
        lines.append(tr("It can start here as {name} (checked: nothing was kept).", name=printable(result.get("name"))))
    origin = result.get("from") if isinstance(result.get("from"), dict) else {}
    if origin:
        lines.append(tr("From: {name} (sandbox-ai {version}, {created})", name=printable(origin.get("name")),
                        version=printable(origin.get("sandbox_ai")), created=printable(origin.get("created"))))
    options = [str(word) for word in result.get("options") or [] if isinstance(word, str)]
    lines.append(tr("Starts with: {options}", options=printable(shlex.join(options)) or "-"))
    if result.get("workspace"):
        lines.append(tr("Workspace: {path}", path=printable(result.get("workspace")))
                     + (" " + tr("(its files from the archive)") if result.get("workspace_files") else ""))
    if result.get("include_secrets"):
        lines.append(tr("The archive holds logins and keys."))
    for item in result.get("asked") or []:
        lines.append(tr("Asked first: {item}", item=printable(item)))
    for item in result.get("warnings") or []:
        lines.append(tr("Note: {item}", item=printable(item)))
    approvals = [str(word) for word in result.get("approvals") or [] if isinstance(word, str)]
    if approvals:
        words = printable(" ".join(approvals))
        lines.append(tr("Network approvals granted again: {list}", list=words) if result.get("granted") else
                     tr("Network approvals not granted (tick Grant its network approvals again): {list}", list=words))
    memory = result.get("memory") if isinstance(result.get("memory"), dict) else None
    if memory:
        lines.append(tr("Memory: {count} files added to the project's memory here", count=memory.get("added") or 0))
        for name in memory.get("kept_here") or []:
            lines.append(tr("Kept as it is here (the project's own): {file}", file=printable(name)))
    checkpoints = result.get("checkpoints") if isinstance(result.get("checkpoints"), dict) else None
    if checkpoints:
        lines.append(tr("Checkpoints: {count}", count=len(checkpoints.get("ids") or [])))
    return [line for line in lines if line]


class StateDialog(QDialog):
    """The frame of the three: an explanation, the fields of each, a status line and an output area, the buttons."""

    def __init__(self, parent, sai, title, owner=None, succeeded=None):
        super().__init__(parent)
        self.sai, self.owner, self.succeeded, self.running = sai, owner, succeeded, False
        self.setWindowTitle(title)
        self.layout_ = QVBoxLayout(self)
        self.status = plain_label("", role="secondary", selectable=True)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setAccessibleName(tr("What sandbox-ai said"))
        self.output.setMaximumBlockCount(OUTPUT_LINES)
        self.output.setMinimumHeight(110)
        self.output.hide()
        self.buttons = QDialogButtonBox()
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(lambda _=False: self.reject())
        self.buttons.addButton(self.close_button, QDialogButtonBox.ButtonRole.RejectRole)

    def finish_layout(self):
        self.layout_.addWidget(self.status)
        self.layout_.addWidget(self.output, 1)
        self.layout_.addWidget(self.buttons)
        self.resize(640, 460)

    def check_box(self, text, tip):
        box = QCheckBox(text)
        box.setToolTip(safe_tooltip(tip))
        self.layout_.addWidget(box)
        return box

    def show_output(self, text):
        self.output.show()
        self.output.setPlainText(output_text(text))

    def secrets_asked(self, secrets, what):
        """The question of logins and keys along (SECRETS), as the app's policy asks it; True when it may go on."""
        return not secrets or ask_first(self, "session-export-secrets", tr("With logins and keys"), tr(
            "Take the logins, keys and secrets of its HOME along ({what})? Whoever has them can log in as you.",
            what=what), tr("Yes, with logins and keys"))


def grant_text(what):
    return tr("Grant its network approvals again, as options of that session ({what})? It reaches those destinations "
              "at every start.", what=what)


class DuplicateDialog(StateDialog):
    """Duplicate…: a second session of this machine from the state of session NAME (sandbox-ai session duplicate)."""

    def __init__(self, parent, sai, name, owner=None, succeeded=None):
        super().__init__(parent, sai, tr("Duplicate session {name}", name=printable(name)), owner, succeeded)
        self.name = name
        self.layout_.addWidget(explanation(tr(
            "A second session here from its state: its configuration, a copy of its HOME with the agent's "
            "conversation (both continue it on their own), its memory and checkpoints. Both can run; the workspace "
            "is shared unless its files are copied too. A running session is paused for the moment of the snapshot. "
            "Not started: Resume starts it.")))
        form = QFormLayout()
        self.new_name = QLineEdit(f"{name}-2"[:40])
        self.new_name.setMaxLength(40)
        self.new_name.setAccessibleName(tr("Name of the duplicate"))
        form.addRow(tr("Name of the duplicate"), self.new_name)
        self.layout_.addLayout(form)
        self.with_workspace = self.check_box(tr("Copy its workspace too"), tr(
            "The workspace's files are copied beside it as WORKSPACE-NEW (--with-workspace); without, both work in the "
            "same folder"))
        self.with_secrets = self.check_box(tr("With logins and keys"), tr(
            "Its logins, keys and secrets go along into the copy (asked first; --include-secrets); without, the "
            "duplicate gets the login of this machine"))
        self.grant = self.check_box(tr("Grant its network approvals again"), tr(
            "What it was allowed on the network (net allow, approvals for its run, always for its project) is granted "
            "again as options of the duplicate (asked first; --grant-approvals)"))
        self.go_button = QPushButton(tr("Duplicate"))
        self.go_button.setAccessibleName(tr("Duplicate the session"))
        self.go_button.setToolTip(safe_tooltip(tr("Make the duplicate ({command})",
                                                  command="sandbox-ai session duplicate")))
        self.go_button.clicked.connect(lambda _=False: self.run())
        self.buttons.addButton(self.go_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.finish_layout()

    def run(self):
        if self.running:
            return
        new = self.new_name.text().strip()
        if not backend.SESSION_NAME.fullmatch(new):
            self.status.setText(tr("A session's name is 1-40 letters, digits, '_', '.' or '-', beginning with a letter "
                                   "or digit."))
            return
        secrets, grant, files = self.with_secrets.isChecked(), self.grant.isChecked(), self.with_workspace.isChecked()
        what = tr("the duplicate {name}", name=printable(new))
        if not self.secrets_asked(secrets, what):
            return
        if grant and not ask_first(self, "session-grant-approvals", tr("Grant network approvals"), grant_text(what),
                                   tr("Yes, grant them")):
            return
        applog.log("INFO", "session state", f"duplicate of session {printable(self.name)} as {printable(new)}"
                   + (", its workspace copied" if files else "") + (", with its logins and keys" if secrets else "")
                   + (", its approvals granted again" if grant else ""))
        self.running = True
        set_enabled(self.go_button, False, tr("A command runs."))
        self.status.setText(tr("Duplicating {name}… (a large HOME takes a while)", name=printable(self.name)))
        name = self.name
        background(lambda: self.sai.session_duplicate(name, new, with_workspace=files, include_secrets=secrets,
                                                      grant=grant),
                   owner=self.owner, work=True, timeout=backend.TRANSFER_SECONDS + backend.STATE_SECONDS,
                   what=f"sandbox-ai session duplicate {self.name} {new}", done=self.ran)

    def ran(self, result, error):
        if error is None and self.succeeded is not None:
            self.succeeded()
        if not alive(self):
            return
        self.running = False
        set_enabled(self.go_button, error is not None, None if error is not None else tr("Done."))
        if error is not None:
            self.status.setText(tr("Not duplicated: see what sandbox-ai said below."))
            self.show_output(printable_text(error))
            return
        self.status.setText(tr("Done: {name} is made.", name=printable(result.get("name"))))
        self.show_output("\n".join(result_lines(result)))


class StateExportDialog(StateDialog):
    """Export with state…: session NAME of this machine as one archive (sandbox-ai session export) that `session
    import` takes on any machine."""

    def __init__(self, parent, sai, name, owner=None):
        super().__init__(parent, sai, tr("Export session {name} with its state", name=printable(name)), owner)
        self.name = name
        self.layout_.addWidget(explanation(tr(
            "One archive another machine imports (Import… there, or sandbox-ai session import): its configuration, "
            "HOME with the agent's conversation, memory, checkpoints and network approvals, with a manifest of every "
            "file's checksum. A running session is paused for the moment of the snapshot. Logins and keys are left "
            "out unless ticked.")))
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit(os.path.join(os.path.expanduser("~"), f"{name}.tar.gz"))
        self.path.setAccessibleName(tr("Archive"))
        browse = QToolButton()
        browse.setText("…")
        browse.setAccessibleName(tr("Choose…"))
        browse.clicked.connect(lambda _=False: self.browse())
        line.addWidget(self.path, 1)
        line.addWidget(browse)
        form = QFormLayout()
        form.addRow(tr("Archive"), row)
        self.layout_.addLayout(form)
        self.with_workspace = self.check_box(tr("With its workspace's files"), tr(
            "The workspace's files go into the archive too (--with-workspace); without, the import starts on the "
            "folder of that path there, or where it is mapped to"))
        self.with_secrets = self.check_box(tr("With logins and keys"), tr(
            "Its logins, keys and secrets go into the archive too (asked first; --include-secrets)"))
        self.go_button = QPushButton(tr("Export"))
        self.go_button.clicked.connect(lambda _=False: self.run())
        self.buttons.addButton(self.go_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.finish_layout()

    def browse(self):
        from .state import choose_save
        path = choose_save(self, tr("Export"), f"{self.name}.tar.gz", tr("Archives (*.tar.gz)"), "session-export")
        if path:
            self.path.setText(path)

    def run(self):
        if self.running:
            return
        path = os.path.abspath(os.path.expanduser(self.path.text().strip()))
        if not self.path.text().strip():
            self.status.setText(tr("Choose where the archive is written."))
            return
        if os.path.lexists(path):
            self.status.setText(tr("{path} is there already: choose a new file (nothing is overwritten).",
                                   path=printable(path)))
            return
        secrets, files = self.with_secrets.isChecked(), self.with_workspace.isChecked()
        if not self.secrets_asked(secrets, printable(path)):
            return
        self.with_secrets.setChecked(False)  # (each export with logins and keys is chosen anew)
        applog.log("INFO", "session state", f"export of {printable(self.name)} with its state to {printable(path)}"
                   + (", its workspace too" if files else "") + (", with its logins and keys" if secrets else ""))
        self.running = True
        set_enabled(self.go_button, False, tr("A command runs."))
        self.status.setText(tr("Writing {path}…", path=printable(path)))
        name = self.name
        background(lambda: self.sai.session_export(name, path, include_secrets=secrets, with_workspace=files),
                   owner=self.owner, work=True, timeout=backend.STATE_SECONDS, what="sandbox-ai session export",
                   done=self.ran)

    def ran(self, result, error):
        if not alive(self):
            return
        self.running = False
        set_enabled(self.go_button, True)
        if error is not None:
            self.status.setText(tr("Not exported: {error}", error=printable_text(error)))
            return
        left = [item for item in result.get("left_out") or [] if isinstance(item, dict)]
        secrets = [item for item in left if item.get("secret") is True]
        self.status.setText(tr("{count} files ({size}) written to {path}", count=result.get("files") or 0,
                               size=size_words(result.get("bytes")), path=printable(str(result.get("path") or ""))))
        lines = []
        if secrets:
            lines.append(tr("Logins, keys and secrets left out ({count}):", count=len(secrets)))
            lines += [f"  {printable(str(item.get('path')))}" for item in secrets[:100]]
        others = [item for item in left if item.get("secret") is not True]
        if others:
            lines.append(tr("Left out ({count}):", count=len(others)))
            lines += [f"  {printable(str(item.get('path')))}: {printable(str(item.get('why')))}" for item in others[:100]]
        if lines:
            self.show_output("\n".join(lines))


class ImportDialog(StateDialog):
    """Import…: an archive of `sandbox-ai session export` (of this machine or another) as a stopped session here that
    Resume continues; checked first (--check), then imported."""

    def __init__(self, parent, sai, owner=None, succeeded=None, path=""):
        super().__init__(parent, sai, tr("Import a session"), owner, succeeded)
        self.checked = None  # the --check result of the archive and choices it was made with
        self.layout_.addWidget(explanation(tr(
            "A session's archive (Export with state…, or sandbox-ai session export) as a session of this machine that "
            "Resume continues, conversation and all. The archive is checked as untrusted input first - its files "
            "against its manifest, its configuration with the launcher's own dry run - and what it starts with is "
            "shown before anything is kept.")))
        form = QFormLayout()
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit(path)
        self.path.setAccessibleName(tr("Archive"))
        browse = QToolButton()
        browse.setText("…")
        browse.setAccessibleName(tr("Choose…"))
        browse.clicked.connect(lambda _=False: self.browse())
        line.addWidget(self.path, 1)
        line.addWidget(browse)
        form.addRow(tr("Archive"), row)
        self.new_name = QLineEdit()
        self.new_name.setMaxLength(40)
        self.new_name.setPlaceholderText(tr("its name there"))
        self.new_name.setAccessibleName(tr("Name here"))
        form.addRow(tr("Name here"), self.new_name)
        self.maps = QPlainTextEdit()
        self.maps.setAccessibleName(tr("Folders mapped"))
        self.maps.setPlaceholderText(tr("one per line: FOLDER THERE=FOLDER HERE"))
        self.maps.setMaximumHeight(70)
        form.addRow(tr("Folders mapped"), self.maps)
        self.layout_.addLayout(form)
        self.no_workspace = self.check_box(tr("Leave the archive's workspace files out"), tr(
            "It starts on the folder that is here (--no-workspace)"))
        self.replace = self.check_box(tr("Replace a stopped session of that name"), tr(
            "A stopped session of that name here is deleted first, its HOME too (--replace)"))
        self.grant = self.check_box(tr("Grant its network approvals again"), tr(
            "The network approvals it brings are granted again as options of the session (asked first; "
            "--grant-approvals); without, it asks again when it needs them"))
        self.check_button = QPushButton(tr("Check"))
        self.check_button.setToolTip(safe_tooltip(tr("Check the archive and what it starts with: nothing is kept "
                                                     "({command})", command="sandbox-ai session import --check")))
        self.check_button.clicked.connect(lambda _=False: self.run(check=True))
        self.go_button = QPushButton(tr("Import"))
        self.go_button.clicked.connect(lambda _=False: self.run(check=False))
        self.buttons.addButton(self.check_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.buttons.addButton(self.go_button, QDialogButtonBox.ButtonRole.ActionRole)
        for field in (self.path, self.new_name):
            field.textChanged.connect(lambda _text: self.changed())
        self.maps.textChanged.connect(lambda: self.changed())
        for box in (self.no_workspace, self.replace):
            box.toggled.connect(lambda _on: self.changed())
        self.finish_layout()
        self.update_buttons()

    def browse(self):
        from .state import choose_file
        path = choose_file(self, tr("Import a session"), "session-import", tr("Archives (*.tar.gz)"))
        if path:
            self.path.setText(path)

    def choices(self):
        """(path, name, maps, no_workspace, replace) as filled in; ValueError for a map that is no pair."""
        maps = []
        for line in self.maps.toPlainText().splitlines():
            if not line.strip():
                continue
            there, equals, here = line.strip().partition("=")
            if not equals:
                raise ValueError(tr("A folder mapped is FOLDER THERE=FOLDER HERE: {line}", line=printable(line)))
            maps.append((there.strip(), here.strip()))
        path = os.path.abspath(os.path.expanduser(self.path.text().strip())) if self.path.text().strip() else ""
        return (path, self.new_name.text().strip() or None, tuple(maps), self.no_workspace.isChecked(),
                self.replace.isChecked())

    def changed(self):
        self.checked = None  # (another archive or other choices: checked again before Import)
        self.update_buttons()

    def update_buttons(self):
        if not alive(self):
            return
        why = tr("A command runs.") if self.running else (tr("Choose the archive first.")
                                                         if not self.path.text().strip() else None)
        set_enabled(self.check_button, why is None, why)
        go_why = why or (tr("Check it first.") if self.checked is None else None)
        set_enabled(self.go_button, go_why is None, go_why)

    def run(self, check):
        if self.running:
            return
        try:
            path, name, maps, no_workspace, replace = self.choices()
            backend.session_import_args(path, name, maps, check, replace, False, no_workspace)
        except ValueError as exc:
            self.status.setText(tr("Not started: {error}", error=printable_text(exc)))
            return
        grant = self.grant.isChecked() and not check
        yes = False
        if not check:
            result = self.checked or {}
            asked = [str(item) for item in result.get("asked") or []]
            if asked and not ask_first(self, "session-import", tr("Import a session"), tr(
                    "Its configuration gives the sandbox this of this machine:\n{items}\nImport it with these "
                    "options?", items="\n".join(f"  {printable(item)}" for item in asked)), tr("Yes, import")):
                return
            if grant and result.get("approvals") and not ask_first(
                    self, "session-grant-approvals", tr("Grant network approvals"),
                    grant_text(" ".join(printable(word) for word in result.get("approvals") or [])),
                    tr("Yes, grant them")):
                return
            yes = True  # (asked here, as the app's policy says)
        applog.log("INFO", "session state", f"{'check of the ' if check else ''}import of {printable(path)}"
                   + (f" as {printable(name)}" if name else "") + (f", folders mapped: {len(maps)}" if maps else ""))
        self.running = True
        self.update_buttons()
        self.status.setText(tr("Checking {path}…", path=printable(path)) if check else
                            tr("Importing {path}…", path=printable(path)))
        background(self.sai.session_import, path, name, maps, check, replace, grant, no_workspace, yes,
                   owner=self.owner, work=not check, timeout=backend.STATE_SECONDS,
                   what=f"sandbox-ai session import {path}" + (" --check" if check else ""),
                   done=lambda result, error: self.ran(check, result, error))

    def ran(self, check, result, error):
        if error is None and not check and self.succeeded is not None:
            self.succeeded()
        if not alive(self):
            return
        self.running = False
        if error is not None:
            self.checked = None
            self.status.setText(tr("Not checked: see what sandbox-ai said below.") if check else
                                tr("Not imported: see what sandbox-ai said below."))
            self.show_output(printable_text(error))
        else:
            self.checked = result if check else None
            self.status.setText(tr("Checked: it can start here. Import keeps it.") if check else
                                tr("Done: {name} is made.", name=printable(result.get("name"))))
            self.show_output("\n".join(result_lines(result)))
        self.update_buttons()


def open_import(parent, sai, owner=None, succeeded=None):
    dialog = ImportDialog(parent, sai, owner=owner, succeeded=succeeded)
    dialog.exec()
    dialog.deleteLater()
    return dialog
