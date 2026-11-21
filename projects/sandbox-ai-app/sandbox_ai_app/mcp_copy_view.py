"""From another host…: MCP servers of another host copied to this one (sandbox-ai mcp copy), for "Add MCP server…" of a
running session (then attached to it at once) and the MCP servers page.

Reported live on coding-101: that host had no MCP servers yet - "it would be practical to copy MCP servers from other
hosts to an active session". The servers of every other host are read in the background; Check runs `mcp copy
--dry-run` (on this machine, which reaches both hosts) and shows what is copied and how - a catalog server added from
its catalog there, never copied raw - what changes, and what the target cannot take as it is, each with its field: a
folder of the target for each folder of the source (… chooses one there), or left out; a missing secret kept there
(typed here, sent only there, never read from the source); the image pulled there or sent; another name, or the one
there replaced; without its VPN; the catalog's own pin. OK copies only once a check found nothing left to resolve with
the same choices; the page that opened the dialog asks first and runs the copy (mcp-copy)."""
from __future__ import annotations

import json

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QWidget)

from . import backend, widgets
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import ask_first, background, explanation, plain_label, safe_tooltip

LOCAL = "local"
FLAGS = (("secrets_later", N_("Copy although a secret it uses is missing there (keep it before it starts)")),
         ("no_vpn", N_("Without the VPN profile it is behind")),
         ("current_pin", N_("A catalog server: the image the catalog there pins, where it pins another")),
         ("unsigned", N_("A catalog server its host took unsigned: take it unsigned there too")),
         ("replace", N_("Replace a server of that name there (asked first)")))


def alive(widget):
    try:
        widget.objectName()
        return True
    except RuntimeError:
        return False


def where(host):
    return tr("this machine") if host == LOCAL else printable(host)


def plan_lines(result):
    """What a check (or a copy) of `mcp copy --json` says, as lines for people: per server how it is copied, what is
    copied as defined, what changes, the notes and warnings, and what must be resolved with its fix (sandbox-ai's own
    words, as it said them)."""
    lines = []
    for plan in result.get("servers") or []:
        if not isinstance(plan, dict):
            continue
        server = printable(str(plan.get("server") or "?"))
        lines.append(tr("{server}: added from its catalog there (the same pinned image) - never copied as it is.",
                        server=server) if plan.get("how") == "catalog" else
                     tr("{server}: its definition, checked there as a server of your own is.", server=server))
        for text in plan.get("copied") or []:
            lines.append("  " + tr("copied: {what}", what=printable_text(str(text))))
        for path in plan.get("paths") or []:
            if isinstance(path, dict):
                lines.append("  " + tr("folder {path}: {state}", path=printable(str(path.get("path"))), state=(
                    tr("left out") if path.get("dropped") else printable(str(path["to"])) if path.get("to") else
                    tr("not resolved"))))
        secrets = plan.get("secrets") if isinstance(plan.get("secrets"), dict) else {}
        needed = sorted({str(s.get("secret")) for s in secrets.get("needed") or [] if isinstance(s, dict)})
        if needed:
            lines.append("  " + tr("secrets (names only, never a value): {names}", names=printable(", ".join(needed))))
        image = plan.get("image") if isinstance(plan.get("image"), dict) else None
        if image:
            lines.append("  " + tr("image {ref}: {action}", ref=printable(str(image.get("ref"))), action={
                "pull": tr("pulled there by its digest"), "none": tr("there already"), "missing": tr("not there"),
                "copy": tr("sent from the other host")}.get(str(image.get("action")), printable(str(image.get("action"))))))
        for text in plan.get("changes") or []:
            lines.append("  " + tr("changed: {what}", what=printable_text(str(text))))
        for text in plan.get("notes") or []:
            lines.append("  " + tr("note: {what}", what=printable_text(str(text))))
        for text in plan.get("warnings") or []:
            lines.append("  " + tr("warning: {what}", what=printable_text(str(text))))
        for problem in plan.get("problems") or []:
            if isinstance(problem, dict):
                lines.append("  " + tr("to resolve: {what}", what=printable_text(str(problem.get("text"))))
                             + (" - " + tr("how: {fix}", fix=printable_text(str(problem["fix"])))
                                if problem.get("fix") else ""))
    return lines


def blocked(result):
    """Whether a check found something the target cannot take as it is."""
    return any(isinstance(plan, dict) and plan.get("problems") for plan in (result or {}).get("servers") or [])


class McpCopyDialog(QDialog):
    """A server of another host to TARGET (and with START: then given to that running start there); see the module."""

    def __init__(self, parent, sai, target, start=None):
        super().__init__(parent)
        self.sai, self.target, self.start = sai, target, start
        self.setWindowTitle(tr("MCP server from another host for {start} on {host}", start=printable(start),
                               host=where(target)) if start else
                            tr("MCP server from another host to {host}", host=where(target)))
        form = QFormLayout(self)
        form.addRow(explanation(tr("A server of your own of another host, copied to {host}: a catalog server is added "
                                   "there from its catalog (its signature checked there), a server of your own is "
                                   "checked there as mcp.json is. Secrets travel by name only; a folder of the other "
                                   "host is never mounted there unasked.", host=where(target))))
        self.source = QComboBox()
        self.source.setAccessibleName(tr("MCP server"))
        self.source.addItem(tr("Reading the servers of the other hosts…"), None)
        self.source.setEnabled(False)
        form.addRow(tr("MCP server"), self.source)
        self.unread = plain_label(role="notice-warn", selectable=True)
        self.unread.setVisible(False)
        form.addRow(self.unread)
        self.as_name = QLineEdit()
        self.as_name.setPlaceholderText(tr("the same name"))
        self.as_name.setAccessibleName(tr("Name there"))
        form.addRow(tr("Name there"), self.as_name)
        self.image = QComboBox()
        for text, key in ((tr("as needed (a catalog server's is pulled there by its digest)"), ""),
                          (tr("pull it there by its digest"), "pull"),
                          (tr("send it from the other host (podman save | podman load)"), "copy")):
            self.image.addItem(text, key)
        self.image.setAccessibleName(tr("Its image"))
        form.addRow(tr("Its image"), self.image)
        self.flags = {}
        for key, text in FLAGS:
            box = QCheckBox(tr(text))
            self.flags[key] = box
            form.addRow(box)
        self.folders_holder = QWidget()
        self.folders = QFormLayout(self.folders_holder)
        self.folders.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.folders_holder)
        self.folder_fields = {}  # the other host's folder -> (PathField, "leave out" QCheckBox)
        self.secrets_holder = QWidget()
        self.secrets_row = QHBoxLayout(self.secrets_holder)
        self.secrets_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.secrets_holder)
        self.plan = widgets.MessageText()
        self.plan.setAccessibleName(tr("What is copied"))
        self.plan.setPlaceholderText(tr("Check says what is copied, what changes, and what {host} cannot take as it "
                                        "is.", host=where(target)))
        form.addRow(self.plan)
        self.check_button = QPushButton(tr("Check"))
        self.check_button.setToolTip(tr("sandbox-ai mcp copy --dry-run: nothing is copied"))
        self.check_button.clicked.connect(lambda _=False: self.check())
        buttons = self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(self.check_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Copy and add") if start else tr("Copy"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.checked_with, self.result = None, None
        for control in (self.source, self.image):
            control.currentIndexChanged.connect(lambda _=0: self.changed())
        self.as_name.textChanged.connect(lambda _="": self.changed())
        for box in self.flags.values():
            box.toggled.connect(lambda _=False: self.changed())
        self.source.currentIndexChanged.connect(lambda _=0: self.forget_folders())
        self.update_state()
        self.resize(640, 0)
        background(self.sai.host_mcp_elsewhere, target, owner=self, timeout=150,
                   done=lambda rows, error: alive(self) and self.sources_read(rows, error))

    def sources_read(self, rows, error):
        """The other hosts' servers in the choice; the hosts that did not answer said below it."""
        self.source.blockSignals(True)
        self.source.clear()
        unread = []
        for host, servers, problem in (rows or []) if error is None else []:
            if problem:
                unread.append(f"{where(host)}: {printable_text(problem)[:200]}")
            for server in servers:
                kind = server.get("catalog") or server.get("kind") or ""
                self.source.addItem(f"{printable(server['name'])} ({where(host)})" + (f" - {printable(kind)}"
                                                                                      if kind else ""),
                                    (host, server["name"]))
        if error is not None:
            unread.append(printable_text(str(error))[:300])
        if not self.source.count():
            self.source.addItem(tr("No other host has a server of its own."), None)
        self.source.setEnabled(self.source.itemData(0) is not None)
        self.source.blockSignals(False)
        self.unread.setText(tr("Not read: {list}", list="; ".join(unread)) if unread else "")
        self.unread.setVisible(bool(unread))
        self.forget_folders()
        self.changed()

    def chosen(self):
        """(host, server) chosen, or None."""
        data = self.source.currentData()
        return tuple(data) if data else None

    def choices(self):
        """The keywords of SandboxAI.host_mcp_copy for what was chosen and typed (dry_run aside)."""
        paths, dropped = {}, []
        for old, (field, leave) in self.folder_fields.items():
            if leave.isChecked():
                dropped.append(old)
            elif field.value().strip():
                paths[old] = field.value().strip()
        found = {"as_name": self.as_name.text().strip() or None, "paths": paths, "drop_paths": dropped,
                 "pull_image": self.image.currentData() == "pull", "copy_image": self.image.currentData() == "copy"}
        found.update({key: box.isChecked() for key, box in self.flags.items()})
        return found

    def key(self):
        return json.dumps([self.chosen(), self.choices()], sort_keys=True)

    def changed(self):
        """A choice changed: OK waits for a check of the new ones."""
        if self.checked_with is not None and self.checked_with != self.key():
            self.checked_with = None
        self.update_state()

    def update_state(self):
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(self.checked_with is not None and self.checked_with == self.key())
        self.check_button.setEnabled(self.chosen() is not None)

    def forget_folders(self):
        while self.folders.rowCount():
            self.folders.removeRow(0)
        self.folder_fields = {}
        self.plan.setPlainText("")
        self.checked_with, self.result = None, None

    def check(self):
        """mcp copy --dry-run with what is chosen (in the background)."""
        chosen = self.chosen()
        if chosen is None:
            return
        source, name = chosen
        choices = self.choices()
        try:
            backend.mcp_copy_args(source, self.target, [name], dry_run=True, **choices)
        except ValueError as exc:
            self.plan.setPlainText(printable_text(exc))
            return
        key = self.key()
        self.plan.setPlainText(tr("Checking…"))
        background(lambda: self.sai.host_mcp_copy_check(source, self.target, [name], **choices), owner=self,
                   source=self.check_button, timeout=330,
                   done=lambda result, error: alive(self) and self.checked(key, result, error))

    def checked(self, key, result, error):
        """What the check said: its plan; a field for each folder of the source; a button for each secret the target
        lacks; OK when nothing is left to resolve (and the choices are still those checked)."""
        if error is not None:
            self.plan.setPlainText(tr("Not checked: {reason}", reason=printable_text(error)[:1500]))
            self.checked_with = None
            self.update_state()
            return
        self.result = result
        self.show_folders(result)
        self.show_secrets(result)
        self.plan.setPlainText("\n".join(plan_lines(result)))
        self.checked_with = key if not blocked(result) and key == self.key() else None
        self.update_state()

    def show_folders(self, result):
        from .ui import PathField  # (ui.py imports this module)
        source = (self.chosen() or ("", ""))[0]
        for plan in result.get("servers") or []:
            for path in plan.get("paths") or [] if isinstance(plan, dict) else []:
                old = str(path.get("path") or "") if isinstance(path, dict) else ""
                if not old or old in self.folder_fields:
                    continue
                field = PathField()
                field.set_host(self.target)  # … lists the target's folders (remote_chooser over SSH)
                field.edit.setPlaceholderText(tr("a folder of {host} (the same path keeps it)", host=where(self.target)))
                if path.get("to"):
                    field.set_value(path["to"])
                leave = QCheckBox(tr("leave out"))
                leave.setChecked(bool(path.get("dropped")))
                row = QHBoxLayout()
                row.addWidget(field, 1)
                row.addWidget(leave)
                holder = QWidget()
                holder.setLayout(row)
                label = tr("{target} in the container (on {host}: {path})", target=printable(str(path.get("target"))),
                           host=where(source), path=printable(old)) if path.get("kind") == "mount" else \
                    tr("{path} (on {host})", path=printable(old), host=where(source))
                field.setToolTip(safe_tooltip(tr("A folder of {host}; the other host's {path} is never mounted there "
                                                 "unasked (you decide: a folder you name is mounted, with the usual "
                                                 "warnings)", host=where(self.target), path=printable(old))))
                self.folders.addRow(label, holder)
                self.folder_fields[old] = (field, leave)
                field.changed.connect(self.changed)
                leave.toggled.connect(lambda _=False: self.changed())

    def show_secrets(self, result):
        while self.secrets_row.count():
            item = self.secrets_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        missing = sorted({str(name) for plan in result.get("servers") or [] if isinstance(plan, dict)
                          for name in ((plan.get("secrets") or {}).get("missing") or [])})
        for name in missing:
            button = QPushButton(tr("Keep {name} on {host}…", name=printable(name), host=where(self.target)))
            button.setToolTip(safe_tooltip(tr("Its value is typed here and kept on {host} only (sandbox-ai secrets "
                                              "set there); the other host's value is never read",
                                              host=where(self.target))))
            button.clicked.connect(lambda _=False, n=name: self.keep_secret(n))
            self.secrets_row.addWidget(button)
        self.secrets_row.addStretch()

    def keep_secret(self, name):
        """The secret NAME typed here and kept on the target (its value on standard input there), then checked
        again; one there already is replaced only when asked (secret-replace)."""
        title = tr("Keep {name} on {host}", name=printable(name), host=where(self.target))
        # (the app's own dialog for a secret's value: never shown or logged, its text never read as rich text)
        value = widgets.ask_secret_value(self, title, tr("The value of {name} (kept on {host} only):",
                                                         name=printable(name), host=where(self.target)), tr("Keep"))
        if not value:
            return
        present = set(((self.result or {}).get("present_secrets") or []))
        if name in present and not ask_first(self, "secret-replace", title, tr(
                "{host} has a secret {name} already: replace its value?", host=where(self.target),
                name=printable(name)), tr("Replace")):
            return
        background(lambda: self.sai.set_secret(name, value, host=self.target), owner=self, work=True, timeout=120,
                   done=lambda result, error: alive(self) and self.secret_kept(name, result, error))

    def secret_kept(self, name, result, error):
        status = result[0] if error is None and isinstance(result, tuple) else None
        if status != 0:
            reason = printable_text(error if error is not None else (result[2] or result[1]))[:600]
            widgets.warn(self, tr("Keep {name} on {host}", name=printable(name), host=where(self.target)),
                         tr("Not kept: {reason}", reason=reason), modal=False)
            return
        self.check()

    def accept(self):
        if self.checked_with is None or self.checked_with != self.key():
            self.check()
            return
        super().accept()

    def values(self):
        """(source host, server, the keywords of host_mcp_copy) as checked."""
        source, name = self.chosen()
        return source, name, self.choices()


def copy_question(source, name, target, start, choices):
    """The question asked before the copy (mcp-copy)."""
    text = tr("Copy the MCP server {name} from {source} to {host}?", name=printable(name), source=where(source),
              host=where(target))
    if choices.get("as_name"):
        text += " " + tr("There it is called {name}.", name=printable(choices["as_name"]))
    text += " " + tr("It is defined there with the folders, variables and network the check showed.")
    if start:
        text += " " + tr("Then {start} reaches it now, while it runs, and at its later starts.", start=printable(start))
    return text

