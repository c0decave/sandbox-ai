"""Identities (sandbox-ai identities): who a sandbox works as - a commit name and address, Git logins and publish
logins under one name, chosen at a start with --identity NAME. A section of the Credentials page; every change goes
through the command line (identities add/set/login/logout/publish/unpublish), names of secrets only, never tokens.
On this machine or on the host the Credentials page has chosen (over SSH); Check runs `sandbox-ai identities check`."""
from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .backend import SandboxAI
from .files import printable
from .i18n import tr
from .host_choice import on_host
from .name_fields import NameField
from . import tables
from .widgets import (ask_first, background, delete_key, fill, held, multi_select, one_only, outcome, plain_label,
                      report_each, run_each, selected, selected_keys, several, show_text, table)

LOCAL = "local"

PUBLISH_KINDS = tuple(SandboxAI.PUBLISH_HOSTS)


def summary(identity):
    who = " ".join(part for part in (identity.get("name"), f"<{identity['email']}>" if identity.get("email") else None)
                   if part)
    return [who or tr("(no commit name)"), ", ".join(e["url"] for e in identity.get("git", [])) or "—",
            ", ".join(e["kind"] for e in identity.get("publish", [])) or "—"]


class IdentitiesBox(QGroupBox):
    """The identities with Add, Edit and Remove."""

    def __init__(self, sai):
        super().__init__(tr("Identities (sandbox-ai identities)"))
        self.sai, self.known, self.secret_names, self.host, self.token = sai, {}, [], LOCAL, 0
        self.on_change = None  # the Credentials page's overview
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("Who a sandbox works as: commit name and address, Git and publish logins under "
                                        "one name; a start uses one with --identity NAME. Without it: default (your "
                                        "git-logins and publish-logins)."), role="secondary"))
        self.list = table([tr("Identity"), tr("Commits as"), tr("Git servers"), tr("Publishes to")], stretch=2)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.add_button, self.edit_button, self.remove_button = \
            QPushButton(tr("Add…")), QPushButton(tr("Edit…")), QPushButton(tr("Remove…"))
        self.check_button = QPushButton(tr("Check"))
        self.check_button.setToolTip(tr("sandbox-ai identities check: whether the secrets each identity names are "
                                        "kept (the selected one, or every identity)"))
        self.add_button.clicked.connect(lambda: self.edit(None))
        self.edit_button.clicked.connect(lambda: self.edit(selected(self.list)))
        self.remove_button.clicked.connect(self.remove)
        self.check_button.clicked.connect(lambda _=False: self.check())
        for widget in (self.add_button, self.edit_button, self.remove_button, self.check_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        self.message = plain_label()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        # several at once (Ctrl, Shift): Remove… and the Delete key take them all; Edit… one
        multi_select(self.list)
        delete_key(self.list, self.remove)
        one_only(self.list, [self.edit_button])

    def set_host(self, host):
        """HOST's identities from now on (the Credentials page's host chooser)."""
        if host != self.host:
            self.host, self.known = host, {}
            fill(self.list, [])
            self.refresh()

    def buttons(self):
        return [self.add_button, self.edit_button, self.remove_button, self.check_button]

    def refresh(self):
        host = self.host
        self.token += 1
        token = self.token

        def read():
            try:
                names = [name for name, _ in (self.sai.secrets() if host == LOCAL else self.sai.host_secrets(host))[1]]
            except Exception:  # noqa: BLE001 - only an offer while typing
                names = []
            return (self.sai.identities() if host == LOCAL else self.sai.host_identities(host)), names
        background(read, owner=self, timeout=150,
                   done=lambda result, error: self.shown(result, error, host, token))

    def shown(self, result, error, host=LOCAL, token=None):
        if (token is not None and token != self.token) or host != self.host:
            return
        if error is not None:
            self.message.setText(printable(error))
            return
        self.known, self.secret_names = result
        fill(self.list, [[name, *summary(identity)] for name, identity in sorted(self.known.items())])
        if self.on_change is not None:
            self.on_change()

    def edit(self, name):
        # The host and its identities as they were when the dialog opened: the answer is saved there, whatever the
        # chooser shows by then.
        host, known = self.host, self.known
        if name is not None and name not in known:
            return
        dialog = IdentityDialog(self, name, known.get(name), self.secret_names, set(known))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target, new = dialog.result_identity()
        old = known.get(target)
        # Logins taken from the identity are taken from every later start that uses it: asked first (login-remove).
        gone = [step[-1] for step in self.sai.identity_steps(target, old, new) if step[1] in ("logout", "unpublish")]
        if gone and not ask_first(self, "login-remove", tr("Remove"), on_host(host, tr(
                "Remove these logins from the identity {name}: {list}? Their secrets stay in sandbox-ai secrets.",
                name=printable(target), list=", ".join(printable(value) for value in gone))), tr("Remove")):
            return
        self.message.setText(tr("Saving {name}…", name=target))

        def work():
            if host == LOCAL:
                return self.sai.save_identity(target, old, new)
            return self.sai.host_save_identity(host, target, old, new)
        background(work, owner=self, work=True, timeout=300, source=self.buttons(),
                   done=lambda result, error: self.saved(target, result, error))

    def saved(self, name, result, error):
        status, said = (1, str(error)) if error is not None else result
        self.message.setText(tr("{name} saved.", name=name) if status == 0 else printable(said)[-1500:])
        self.refresh()

    def remove_several(self, names, host=None):
        """Remove… with several identities selected: one question naming them, then each (identities remove) on HOST
        (default: the one chosen; taken before the question)."""
        host = self.host if host is None else host
        if not ask_first(self, "login-remove", tr("Remove identity"), on_host(host, several(tr(
                "Remove these {count} identities? Their secrets stay in sandbox-ai secrets.", count=len(names)),
                [printable(name) for name in names])), tr("Remove")):
            return

        def work(name):
            if host == LOCAL:
                return self.sai.call("identities", "remove", "--", name, timeout=60)
            return self.sai.host_run(host, "identities", "remove", "--", name, timeout=90)

        def done(outcomes):
            failures = report_each(self, tr("Remove identity"), outcomes, printable)
            self.message.setText(tr("Removed {count} of {total}.", count=len(names) - len(failures), total=len(names)))
            self.refresh()
        run_each(work, names, owner=self, source=self.buttons(), timeout=120 * len(names), done=done)

    def remove(self):
        if held(self.remove_button):  # (the Delete key while a removal runs)
            self.message.setText(tr("A removal is running: wait for it."))
            return
        names, host = [name for name in selected_keys(self.list) if name], self.host
        if len(names) > 1:
            self.remove_several(names, host)
            return
        name = selected(self.list)
        if not name or not ask_first(self, "login-remove", tr("Remove identity"), on_host(host, tr(
                "Remove the identity {name}? Its secrets stay in sandbox-ai secrets.", name=printable(name))),
                tr("Remove")):
            return

        def work():
            if host == LOCAL:
                return self.sai.call("identities", "remove", "--", name, timeout=60)
            return self.sai.host_run(host, "identities", "remove", "--", name, timeout=90)
        background(work, owner=self, work=True, timeout=120, source=self.buttons(),
                   done=lambda result, error: self.saved(name, (result[0], result[1] + result[2]) if error is None
                                                         else (1, str(error)), None))

    def check(self):
        """`sandbox-ai identities check [NAME]` on the host chosen: whether the secrets they name are there."""
        name, host = selected(self.list), self.host
        if name is not None and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
            return
        background(lambda: self.sai.host_run(host, "identities", "check", *(["--", name] if name else []), timeout=90),
                   owner=self, work=True, source=self.buttons(), timeout=120,
                   done=lambda result, error: self.checked(name, result, error))

    def checked(self, name, result, error):
        status, out, err = outcome(result, error)
        text = "\n".join(printable(line) for line in (out + err).strip()[-6000:].splitlines())
        verdict = tr("every secret is there") if status == 0 else tr("the check found problems")
        self.message.setText(f"{name or tr('every identity')}: {verdict}")
        show_text(self, tr("Check of {name}", name=name or tr("every identity")), verdict + "\n\n" + text,
                  modal=False)


class IdentityDialog(QDialog):
    """One identity: its name (when new), commit name, address, description, Git logins and publish logins."""

    def __init__(self, parent, name, identity, secret_names, taken):
        super().__init__(parent)
        self.setWindowTitle(tr("Identity {name}", name=name) if name else tr("New identity"))
        self.resize(760, 560)
        self.taken, self.original = taken, name
        identity = identity or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(name or "")
        self.name.setPlaceholderText(tr("lower-case letters, digits, - and _, e.g. work or c0decave"))
        self.name.setReadOnly(name is not None)
        self.person = QLineEdit(identity.get("name", ""))
        self.person.setPlaceholderText(tr("the commit name, e.g. Ada Lovelace"))
        self.email = QLineEdit(identity.get("email", ""))
        self.email.setPlaceholderText(tr("the commit address, e.g. you@users.noreply.github.com"))
        self.description = QLineEdit(identity.get("description", ""))
        for label, widget in ((tr("Identity"), self.name), (tr("Commit name"), self.person), (tr("Commit address"), self.email),
                              (tr("Description"), self.description)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.secret_names = list(secret_names)
        layout.addWidget(plain_label(tr("Git logins (the token is a secret of sandbox-ai secrets; the proxy adds it)"),
                                     role="section"))
        self.git = self.make_table([tr("Git server"), tr("User"), tr("Secret")], 0)
        for login in identity.get("git", []):
            self.add_git(login["url"], login["user"], login["secret"])
        layout.addWidget(self.git)
        layout.addLayout(self.buttons_for(self.git, lambda: self.add_git("https://github.com", "", "")))
        layout.addWidget(plain_label(tr("Publish logins (used with --publish KIND)"), role="section"))
        self.publish = self.make_table([tr("Kind"), tr("Secret"), tr("Own server (optional)")], 2, (0,))
        tables.keep(self.git, "identity-git")
        tables.keep(self.publish, "identity-publish")
        for login in identity.get("publish", []):
            default = SandboxAI.PUBLISH_HOSTS.get(login["kind"])
            self.add_publish(login["kind"], login["secret"], "" if login["host"] == default else f"https://{login['host']}")
        layout.addWidget(self.publish)
        layout.addLayout(self.buttons_for(self.publish, lambda: self.add_publish("pypi", "", "")))
        self.note = plain_label(role="secondary")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def make_table(headers, stretch, compact=()):
        widget = QTableWidget(0, len(headers))
        widget.setHorizontalHeaderLabels(headers)
        tables.setup(widget, stretch, compact)
        widget.verticalHeader().setVisible(False)
        widget.setMaximumHeight(150)
        return widget

    def buttons_for(self, widget, add):
        row = QHBoxLayout()
        plus, minus = QPushButton(tr("Add")), QPushButton(tr("Remove"))
        plus.clicked.connect(add)
        minus.clicked.connect(lambda: [widget.removeRow(r) for r in
                                       sorted({i.row() for i in widget.selectedIndexes()}, reverse=True)])
        row.addWidget(plus)
        row.addWidget(minus)
        row.addStretch()
        return row

    def secret_edit(self, value):
        """A secret's name: that host's to choose from (a list, and as you type), any other typed."""
        host = getattr(self.parent(), "host", LOCAL) or LOCAL
        edit = NameField("secret", names=self.secret_names, host=host,
                         placeholder=tr("a secret's name, e.g. GH_PUSH"))
        edit.setAccessibleName(tr("Secret"))
        edit.setText(value)
        if getattr(self.parent(), "sai", None) is not None:
            edit.set_source(self.parent().sai, host, read=False)  # the page read them: again when they change
        return edit

    def add_git(self, url, user, secret):
        row = self.git.rowCount()
        self.git.insertRow(row)
        self.git.setItem(row, 0, QTableWidgetItem(url))
        self.git.setItem(row, 1, QTableWidgetItem(user))
        self.git.setCellWidget(row, 2, self.secret_edit(secret))
        tables.fit_columns(self.git)

    def add_publish(self, kind, secret, url):
        row = self.publish.rowCount()
        self.publish.insertRow(row)
        kinds = QComboBox()
        kinds.addItems(PUBLISH_KINDS)
        kinds.setCurrentText(kind)
        self.publish.setCellWidget(row, 0, kinds)
        self.publish.setCellWidget(row, 1, self.secret_edit(secret))
        self.publish.setItem(row, 2, QTableWidgetItem(url))
        tables.fit_columns(self.publish)

    def result_identity(self):
        git = []
        for row in range(self.git.rowCount()):
            url = (self.git.item(row, 0).text() if self.git.item(row, 0) else "").strip()
            user = (self.git.item(row, 1).text() if self.git.item(row, 1) else "").strip()
            secret = self.git.cellWidget(row, 2).text().strip()
            if url or secret:
                git.append({"url": url, "user": user, "secret": secret})
        publish = []
        for row in range(self.publish.rowCount()):
            kind = self.publish.cellWidget(row, 0).currentText()
            secret = self.publish.cellWidget(row, 1).text().strip()
            url = (self.publish.item(row, 2).text() if self.publish.item(row, 2) else "").strip()
            if secret:
                publish.append({"kind": kind, "secret": secret, **({"url": url} if url else {})})
        return self.name.text().strip(), {"name": self.person.text().strip(), "email": self.email.text().strip(),
                                          "description": self.description.text().strip(), "git": git,
                                          "publish": publish}

    def check_and_accept(self):
        name, identity = self.result_identity()
        problem = None
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) or name == "default":
            problem = tr("The identity's name is lower-case letters, digits, - and _ (not default).")
        elif self.original is None and name in self.taken:
            problem = tr("There is an identity {name} already.", name=name)
        elif any(not e["url"] or not e["secret"] for e in identity["git"]):
            problem = tr("Each Git login needs a server (https://HOST, or http://localhost:PORT) and a secret.")
        elif len({e["kind"] for e in identity["publish"]}) != len(identity["publish"]):
            problem = tr("Each kind of publish login once.")
        if problem:
            self.note.setText(problem)
            return
        self.accept()
