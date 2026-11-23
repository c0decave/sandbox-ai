"""From another host…: MCP servers of another host copied to this one (sandbox-ai mcp copy), for "Add MCP server…" of a
running session (then attached to it at once) and the MCP servers page; and Copy NAME to another host… of that page: the
server selected there copied to a host chosen in the dialog.

Reported live on coding-101: that host had no MCP servers yet - "it would be practical to copy MCP servers from other
hosts to an active session". The servers of every other host are read in the background; Check runs `mcp copy
--dry-run` (on this machine, which reaches both hosts) and shows what is copied and how - a catalog server added from
its catalog there, never copied raw - what changes, and what the target cannot take as it is, each with its field: a
folder of the target for each folder of the source (… chooses one there), or its default folder there (a path of the
source means nothing there: sandbox-ai mcp copy --drop-path; a command server's option: left out); a missing secret
kept there (typed here, sent only there, never read from the source); the image pulled there or sent; another name, or
the one there replaced; without its VPN; the catalog's own pin. OK (Copy) checks the choices first and copies only
once a check found nothing left to resolve with the same choices - greyed out only while the very choices checked are
blocked (its tooltip says so); the page that opened the dialog asks first and runs the copy (mcp-copy).

Reported live (0.1.211): "kopieren ist immer ausgegraut" - Copy was greyed out until a clean check of the very same
choices, a folder typed after a check greyed it again; "prüfen funktioniert manchmal" - a check's late answer was shown
for the server (or choices) chosen since, its folder fields with it. Only the answer of the last check counts now."""
from __future__ import annotations

import json

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QWidget)

from . import backend, widgets
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import ask_first, background, explanation, plain_label, safe_tooltip

LOCAL = "local"
WORKSPACE_MARKER = "@workspace"  # (sandbox/mcp.py WORKSPACE_HOST: the start's workspace)
FOLDER_DEFAULTS = (WORKSPACE_MARKER, "default")  # (and DEFAULT_HOST: a folder of the server's own)
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
                # (dropped: a folder's default there - a path of the source means nothing there; an option of a
                # command server has none: left out)
                default = path.get("dropped") and path.get("kind") == "mount" and path.get("to")
                # (a folder default of the source - the start's workspace, a folder of the server's own - is that
                # default there: said in words, never as the definition's marker)
                there = where(str(result.get("to") or ""))
                kept = path.get("kind") == "mount" and not path.get("dropped") and path.get("to") in FOLDER_DEFAULTS
                lines.append("  " + tr("folder {path}: {state}", path=tr("the start's workspace")
                                       if path.get("path") == WORKSPACE_MARKER else printable(str(path.get("path"))),
                                       state=(
                    tr("the default folder on {host}", host=there) if default else
                    tr("the workspace of the start that uses it on {host}", host=there)
                    if kept and path.get("to") == WORKSPACE_MARKER else
                    tr("a folder of its own on {host}, new and empty", host=there) if kept else
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
        for text in plan.get("capped") or []:  # (a folder no more writable than its catalog entry declares)
            lines.append("  " + tr("capped: {what}", what=printable_text(str(text))))
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
    """A server of another host to TARGET (and with START: then given to that running start there); with SERVER
    ((host, name)) that server to another host, chosen here (target_choice); see the module."""

    def __init__(self, parent, sai, target=None, start=None, server=None):
        super().__init__(parent)
        self.sai, self.fixed_target, self.start = sai, target, start
        self.server = tuple(server) if server else None
        self.target_choice = None
        self.serial, self.accepting = 0, False  # the last check started (only its answer counts); OK clicked: copy
        form = QFormLayout(self)
        self.source = QComboBox()
        self.source.setAccessibleName(tr("MCP server"))
        if self.server:
            host, name = self.server
            self.setWindowTitle(tr("Copy {name} to another host", name=printable(name)))
            form.addRow(explanation(tr("The MCP server {name} of {host}, copied to another host: a catalog server is "
                                       "added there from its catalog (its signature checked there), a server of your "
                                       "own is checked there as mcp.json is. Secrets travel by name only; a folder of "
                                       "{host} is never mounted there unasked.", name=printable(name),
                                       host=where(host))))
            self.source.addItem(f"{printable(name)} ({where(host)})", self.server)
            self.source.setEnabled(False)
            form.addRow(tr("MCP server"), self.source)
            self.target_choice = QComboBox()
            self.target_choice.setAccessibleName(tr("To host"))
            for other in [LOCAL, *sai.remote_hosts()]:
                if other != host:
                    self.target_choice.addItem(where(other), other)
            if not self.target_choice.count():
                self.target_choice.addItem(tr("No other host: add one on the Hosts page."), None)
                self.target_choice.setEnabled(False)
            form.addRow(tr("To host"), self.target_choice)
        else:
            self.setWindowTitle(tr("MCP server from another host for {start} on {host}", start=printable(start),
                                   host=where(target)) if start else
                                tr("MCP server from another host to {host}", host=where(target)))
            form.addRow(explanation(tr("A server of your own of another host, copied to {host}: a catalog server is "
                                       "added there from its catalog (its signature checked there), a server of your "
                                       "own is checked there as mcp.json is. Secrets travel by name only; a folder of "
                                       "the other host is never mounted there unasked.", host=where(target))))
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
                                        "is.", host=where(target)) if target else
                                     tr("Check says what is copied, what changes, and what the host chosen cannot "
                                        "take as it is."))
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
        self.checked_with, self.blocked_with, self.check_result = None, None, None
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setToolTip(tr("Checks the choices first (as Check does), then copies when nothing is left to resolve"))
        widgets.set_enabled(ok, True)  # (keeps that tooltip for set_enabled's reasons)
        if self.target_choice is not None:
            self.target_choice.currentIndexChanged.connect(lambda _=0: self.forget_folders())
            self.target_choice.currentIndexChanged.connect(lambda _=0: self.changed())
        for control in (self.source, self.image):
            control.currentIndexChanged.connect(lambda _=0: self.changed())
        self.as_name.textChanged.connect(lambda _="": self.changed())
        for box in self.flags.values():
            box.toggled.connect(lambda _=False: self.changed())
        self.source.currentIndexChanged.connect(lambda _=0: self.forget_folders())
        self.update_state()
        self.resize(640, 0)
        if not self.server:
            background(self.sai.host_mcp_elsewhere, target, owner=self, timeout=150,
                       done=lambda rows, error: alive(self) and self.sources_read(rows, error))

    @property
    def target(self):
        """The host the server is copied to: the one given, or the one chosen (Copy NAME to another host…)."""
        if self.target_choice is not None:
            return self.target_choice.currentData()
        return self.fixed_target

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
        return json.dumps([self.chosen(), self.target, self.choices()], sort_keys=True)

    def changed(self):
        """A choice changed: OK checks the new ones first."""
        if self.checked_with is not None and self.checked_with != self.key():
            self.checked_with = None
        self.update_state()

    def update_state(self):
        """Check: a server and a host chosen. OK (Copy): the same, unless the last check found what is left to resolve
        with exactly these choices (its tooltip says so)."""
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ready = self.chosen() is not None and self.target is not None
        stuck = ready and self.blocked_with is not None and self.blocked_with == self.key()
        if not widgets.held(ok):  # (a check runs: background() gives it back, then its answer sets it)
            widgets.set_enabled(ok, ready and not stuck, tr(
                "Resolve what the check says first (the fields above, or another choice); Copy then checks again")
                if stuck else tr("Choose a server and a host first."))
        if not widgets.held(self.check_button):  # (enabled again by background() when its check ends)
            self.check_button.setEnabled(ready)

    def forget_folders(self):
        while self.folders.rowCount():
            self.folders.removeRow(0)
        self.folder_fields = {}
        self.plan.setPlainText("")
        self.serial += 1  # a check still running answers for what was chosen before: dropped
        self.checked_with, self.blocked_with, self.check_result, self.accepting = None, None, None, False
        self.update_state()

    def check(self, then_accept=False):
        """mcp copy --dry-run with what is chosen (in the background); THEN_ACCEPT (OK clicked): copied once it is
        clean."""
        chosen, target = self.chosen(), self.target
        if chosen is None or target is None:
            return
        source, name = chosen
        choices = self.choices()
        try:
            backend.mcp_copy_args(source, target, [name], dry_run=True, **choices)
        except ValueError as exc:
            self.plan.setPlainText(printable_text(exc))
            return
        key = self.key()
        self.serial += 1
        serial, self.accepting = self.serial, then_accept
        self.plan.setPlainText(tr("Checking…"))
        background(lambda: self.sai.host_mcp_copy_check(source, target, [name], **choices), owner=self,
                   source=[self.check_button, self.buttons.button(QDialogButtonBox.StandardButton.Ok)], timeout=330,
                   done=lambda result, error: alive(self) and self.checked(key, result, error, serial))

    def checked(self, key, result, error, serial=None):
        """What the check said: its plan; a field for each folder of the source; a button for each secret the target
        lacks; OK when nothing is left to resolve (and the choices are still those checked) - and with OK clicked,
        the copy. The answer of a check that is not the last one started, or of other choices, is dropped."""
        if serial is not None and serial != self.serial or key != self.key():
            self.update_state()
            return
        accepting, self.accepting = self.accepting, False
        if error is not None:
            self.plan.setPlainText(tr("Not checked: {reason}", reason=printable_text(error)[:1500]))
            self.checked_with = self.blocked_with = None
            self.update_state()
            return
        self.check_result = result
        self.show_folders(result)
        self.show_secrets(result)
        self.plan.setPlainText("\n".join(plan_lines(result)))
        clean = not blocked(result) and key == self.key()
        self.checked_with = key if clean else None
        self.blocked_with = None if clean else key
        self.update_state()
        if clean and accepting:
            super().accept()

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
                default = path.get("kind") == "mount" and path.get("to") in FOLDER_DEFAULTS
                if path.get("to") and not path.get("dropped") and not default:
                    field.set_value(path["to"])
                if path.get("kind") == "mount":  # (sandbox-ai mcp copy --drop-path: its default folder there)
                    leave = QCheckBox(tr("its default folder there"))
                    leave.setToolTip(safe_tooltip(tr(
                        "{path} is a folder of the other host, which means nothing on {host}: the server gets its "
                        "default folder there instead (/work: the workspace of the start that uses it; any other: a "
                        "folder of its own made there, new and empty)", path=printable(old), host=where(self.target))))
                else:
                    leave = QCheckBox(tr("leave out"))
                leave.setChecked(bool(path.get("dropped")) or default)
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
        present = set(((self.check_result or {}).get("present_secrets") or []))
        if name in present and not ask_first(self, "secret-replace", title, tr(
                "{host} has a secret {name} already: replace its value?", host=where(self.target),
                name=printable(name)), tr("Replace")):
            return
        target = self.target  # (read here: never from the thread)
        background(lambda: self.sai.set_secret(name, value, host=target), owner=self, work=True, timeout=120,
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
        """OK: copied when the last check of these very choices was clean, else they are checked first (and copied
        then, if nothing is left to resolve)."""
        if self.checked_with is None or self.checked_with != self.key():
            self.check(then_accept=True)
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

