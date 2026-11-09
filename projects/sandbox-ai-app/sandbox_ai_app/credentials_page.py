"""The Credentials page: an overview of everything configured (agent logins, secrets, Git logins, publish logins,
identities, VPN profiles) with its state, and a tab per kind to look at and change it. Names only: no value of a
secret is ever shown, read back or passed as an argument (sandbox-ai secrets set reads it from standard input).

A click on a row of the overview opens its tab with the row selected. Every change goes through sandbox-ai's own
commands (login, secrets, git-logins, publish-logins, identities, vpn).

The host chooser at the top shows and changes all of it on this machine or on a host of `sandbox-ai hosts` (over SSH:
`sandbox-ai hosts run HOST -- ...`, a secret's value on standard input there too; a login in a terminal tab with
ssh -tt)."""
from __future__ import annotations

import functools

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QMessageBox, QPushButton, QRadioButton, QTabWidget, QVBoxLayout, QWidget,
)

from . import theme, widgets
from .backend import SandboxAI
from .files import printable
from .i18n import N_, tr
from .host_choice import on_host
from .name_fields import NameField, publish as publish_names
from .widgets import ask_first, background, fill, outcome, plain_label, selected, show_text, table

LOCAL = "local"  # the host of this machine's own secrets (sandbox-ai hosts' name for it)
# The overview's kinds, in the order of the tabs.
KINDS = (("logins", N_("Agent login")), ("secrets", N_("Secret")), ("git", N_("Git login")),
         ("publish", N_("Publish login")), ("identities", N_("Identity")), ("vpn", N_("VPN profile")))


class LoginsBox(QGroupBox):
    """Base of the Git and publish logins: a table, Add, Remove and Check, all through sandbox-ai's commands."""
    command = ""
    title = ""
    headers = ()
    stretch, compact = 0, ()  # the column of long text; the short ones (tables.py)

    def __init__(self, sai, changed):
        super().__init__(tr(self.title))
        self.sai, self.changed, self.entries, self.secret_names, self.host = sai, changed, [], [], LOCAL
        self.secrets_known = False  # whether secret_names are what the host said (else a replace is asked about)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(self.about(), role="secondary"))
        self.list = table([tr(h) for h in self.headers], stretch=self.stretch, compact=self.compact)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.add_button, self.remove_button, self.check_button = \
            QPushButton(tr("Add…")), QPushButton(tr("Remove…")), QPushButton(tr("Check"))
        self.add_button.clicked.connect(lambda _=False: self.add())
        self.remove_button.clicked.connect(lambda _=False: self.remove())
        self.check_button.clicked.connect(lambda _=False: self.check())
        for widget in (self.add_button, self.remove_button, self.check_button, *self.more_buttons()):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        self.message = plain_label()
        layout.addWidget(self.message)

    def more_buttons(self):
        return []

    def buttons(self):
        return [self.add_button, self.remove_button, self.check_button, *self.more_buttons()]

    def replaces(self, values):
        """The server or kind of an entry the login of VALUES replaces, or None."""
        return None

    def show_entries(self, entries, secret_names, known=True):
        self.entries, self.secret_names, self.secrets_known = entries, secret_names, bool(known)
        fill(self.list, [self.row(e) for e in entries], ["ok" if e.get("secret") in secret_names else "warn"
                                                         for e in entries])

    def state(self, entry):
        return tr("ready") if entry.get("secret") in self.secret_names else \
            tr("secret {name} missing", name=printable(entry.get("secret", "")))

    def run(self, *args, what):
        """`sandbox-ai COMMAND ARGS` on the host chosen, in the background."""
        self.message.setText(tr("Working…"))
        host = self.host

        def work():
            if host == LOCAL:
                return self.sai.call(self.command, *args, timeout=60)
            return self.sai.host_run(host, self.command, *args, timeout=90)
        background(work, owner=self, work=True, timeout=120, source=self.buttons(),
                   done=lambda result, error: self.ran(what, result, error))

    def ran(self, what, result, error):
        status, out, err = outcome(result, error)
        said = printable((out + err).strip())[-1500:]
        if what == "kinds":
            show_text(self, tr("Publish login kinds"), "\n".join(printable(line) for line in out.strip().splitlines())
                      if status == 0 else said, modal=False)
            return
        self.message.setText(said if status == 0 or what == "check" else tr("Not done: {error}", error=said))
        if what != "check":
            self.changed()

    def check(self):
        self.run("check", what="check")

    def add_with_token(self, add_args, values, what="add"):
        """ADD_ARGS (after the command; they name --secret) on the host chosen; a token typed in the dialog is kept
        first as that secret (`secrets set NAME`, the value on standard input), asked first when a secret of that name
        is there or may be (the host's names not read)."""
        secret, value = values["secret"], values.get("value")
        replaced = self.replaces(values)
        if replaced and not ask_first(self, "login-replace", tr(self.title), on_host(self.host, tr(
                "A login for {name} is there already: its token is replaced by the secret {secret}'s for every later "
                "start that uses it. Replace it?", name=printable(replaced), secret=printable(secret))),
                tr("Replace")):
            self.message.setText(tr("Not added: the login for {name} stays as it is.", name=printable(replaced)))
            return False
        if not value:  # a secret kept already: the add alone
            self.run(*add_args, what=what)
            return True
        if value:
            exists = (secret in self.secret_names) if self.secrets_known else None
            if not ask_first(self, "secret-replace", tr(self.title), on_host(self.host, tr(
                    "A secret {name} is kept already (or may be): the token typed replaces its value, for everything "
                    "that names it. Replace it?", name=printable(secret))), tr("Replace"), detail={"exists": exists}):
                self.message.setText(tr("Not added: the secret {name} stays as it is.", name=printable(secret)))
                return False
        self.message.setText(tr("Working…"))
        host, command = self.host, self.command
        known = (secret in self.secret_names) if self.secrets_known else None

        def work():
            existed = known
            if existed is None:  # the host's names not read: asked now, so a refused login leaves no new secret
                try:
                    existed = self.sai.secret_exists(secret, host)
                except Exception:  # noqa: BLE001 - not answered: kept as it is whatever happens
                    existed = None
            return self.sai.add_login(host, command, add_args, secret, value, existed=existed)
        background(work, owner=self, work=True, timeout=150, source=self.buttons(),
                   done=lambda result, error: self.ran(what, result, error))
        return True


class GitLoginsBox(LoginsBox):
    command = "git-logins"
    title = N_("Git logins (sandbox-ai git-logins)")
    headers = (N_("Git server"), N_("User"), N_("Secret"), N_("State"))
    compact = (3,)

    def about(self):
        return tr("The connection proxy of every sandbox logs in to these Git servers on the wire (clone, fetch, push); "
                  "no token enters a sandbox. The token is the secret named.")

    def row(self, entry):
        return [entry.get("url", ""), entry.get("user", ""), entry.get("secret", ""), self.state(entry)]

    def replaces(self, values):
        url = (values.get("url") or "").strip().rstrip("/").lower()
        return next((e.get("url") for e in self.entries if str(e.get("url", "")).rstrip("/").lower() == url), None)

    def add(self):
        dialog = LoginDialog(self, tr("New Git login"), self.secret_names,
                             ((tr("Git server"), "url", "https://git.example.org"),
                              (tr("User"), "user", "x-access-token")), host=self.host)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        dialog.forget_value()
        # Every sandbox's proxy logs in there from now on: asked first (git-login-add).
        if not ask_first(self, "git-login-add", tr("New Git login"), on_host(self.host, "\n\n".join(
                [printable(values["url"]), self.about()])), tr("Add")):
            return None
        args = ["add", "--secret", values["secret"], *(["--user", values["user"]] if values["user"] else []),
                "--", values["url"]]  # the URL after "--": never read as an option
        self.add_with_token(args, values)
        return {k: v for k, v in values.items() if k != "value"}

    def remove(self):
        url = selected(self.list)
        if not url or not ask_first(self, "login-remove", tr("Remove"), on_host(self.host, tr(
                "Remove the Git login for {name}? Its secret stays.", name=printable(url))), tr("Remove")):
            return
        self.run("remove", "--", url, what="remove")


class PublishLoginsBox(LoginsBox):
    command = "publish-logins"
    title = N_("Publish logins (sandbox-ai publish-logins)")
    headers = (N_("Kind"), N_("Server"), N_("Secret"), N_("State"))
    stretch, compact = 1, (0, 3)

    def about(self):
        return tr("Package registries and forges a start may publish to with --publish KIND; the proxy adds the token "
                  "on the wire and asks before every upload.")

    def more_buttons(self):
        if not hasattr(self, "kinds_button"):
            self.kinds_button = QPushButton(tr("Kinds"))
            self.kinds_button.setToolTip(tr("sandbox-ai publish-logins kinds: what each kind opens, sets and asks "
                                            "about"))
            self.kinds_button.clicked.connect(lambda _=False: self.run("kinds", what="kinds"))
        return [self.kinds_button]

    def row(self, entry):
        return [entry.get("kind", ""), entry.get("host", ""), entry.get("secret", ""), self.state(entry)]

    def replaces(self, values):
        return values.get("kind") if any(e.get("kind") == values.get("kind") for e in self.entries) else None

    def add(self):
        dialog = LoginDialog(self, tr("New publish login"), self.secret_names,
                             ((tr("Own server (optional)"), "url", "https://gitlab.example.org"),),
                             kinds=tuple(SandboxAI.PUBLISH_HOSTS), host=self.host)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        dialog.forget_value()
        self.add_with_token(["add", values["kind"], "--secret", values["secret"],
                             *(["--url", values["url"]] if values["url"] else [])], values)
        return {k: v for k, v in values.items() if k != "value"}

    def remove(self):
        kind = selected(self.list)
        if not kind or not ask_first(self, "login-remove", tr("Remove"), on_host(self.host, tr(
                "Remove the publish login for {name}? Its secret stays.", name=printable(kind))), tr("Remove")):
            return
        self.run("remove", kind, what="remove")


class LoginDialog(QDialog):
    """A Git or publish login on HOST: its fields and its token - typed here (kept as the secret named, prefilled:
    PYPI_TOKEN ... for a publish kind, GIT_TOKEN_<HOST> for a Git server; `secrets set NAME`, the value on standard
    input) or a secret kept there already (a list of that host's secret names)."""

    def __init__(self, parent, title, secret_names, fields, kinds=None, host=LOCAL):
        super().__init__(parent)
        self.setWindowTitle(title if host == LOCAL else tr("{title} (on {host})", title=title, host=printable(host)))
        form = self.form = QFormLayout(self)
        self.kind = None
        if kinds:
            self.kind = QComboBox()
            self.kind.addItems(list(kinds))
            form.addRow(tr("Kind"), self.kind)
        self.edits = {}
        for label, key, placeholder in fields:
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            form.addRow(label, edit)
            self.edits[key] = edit
        self.typed = QRadioButton(tr("Enter the token"))
        self.stored = QRadioButton(tr("Use a stored secret"))
        self.typed.setChecked(True)
        choice = QHBoxLayout()
        choice.addWidget(self.typed)
        choice.addWidget(self.stored)
        choice.addStretch()
        form.addRow(tr("Token"), choice)
        self.value = QLineEdit()
        self.value.setMaxLength(1 << 16)  # a token, not a name: long ones fit
        self.value.setPlaceholderText(tr("the token (kept as the secret below, on standard input)"))
        self.value_row = widgets.secret_value(self.value)
        form.addRow(tr("The token"), self.value_row)
        self.secret = QLineEdit()  # the name a typed token is kept as
        self.secret.setMaxLength(64)
        self.secret.textEdited.connect(lambda _text: setattr(self, "name_typed", True))
        self.name_typed = False
        form.addRow(tr("Kept as the secret"), self.secret)
        # a secret that host keeps already: its names to choose from (read again when one is added or removed there)
        self.secret_choice = NameField("secret", names=list(secret_names), host=host)
        self.secret_choice.setAccessibleName(tr("Stored secret"))
        if getattr(parent, "sai", None) is not None:  # the page read them just now: again when one is added or removed
            self.secret_choice.set_source(parent.sai, host, read=False)
        form.addRow(tr("Stored secret"), self.secret_choice)
        form.addRow(plain_label(tr("A secret name (e.g. PYPI_TOKEN) is how sandbox-ai keeps a token: the login only "
                                   "names it, the token itself stays in sandbox-ai secrets."), role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.problem = plain_label(role="notice-error")
        form.addRow(self.problem)
        self.typed.toggled.connect(lambda _on: self.show_choice())
        if self.kind is not None:
            self.kind.currentTextChanged.connect(lambda _text: self.offer_name())
        if "url" in self.edits:
            self.edits["url"].textChanged.connect(lambda _text: self.offer_name())
        self.offer_name()
        self.show_choice()

    def offer_name(self):
        """The secret name offered for a typed token, until one is typed by hand."""
        if self.name_typed:
            return
        url = self.edits["url"].text().strip() if "url" in self.edits else ""
        kind = self.kind.currentText() if self.kind is not None else None
        self.secret.setText(SandboxAI.token_secret_name(kind=kind, url=None if kind else url))

    def show_choice(self):
        typed = self.typed.isChecked()
        if not typed and not self.secret_choice.text() and self.secret_choice.offered():
            self.secret_choice.setCurrentIndex(1)  # the first stored secret, as before (another chosen or typed)
        for widget, shown in ((self.value_row, typed), (self.secret, typed), (self.secret_choice, not typed)):
            self.form.setRowVisible(widget, shown)

    def values(self):
        found = {key: edit.text().strip() for key, edit in self.edits.items()}
        if self.typed.isChecked():
            found["secret"], found["value"] = self.secret.text().strip(), self.value.text().strip() or None
        else:
            found["secret"], found["value"] = self.secret_choice.text().strip(), None
        if self.kind is not None:
            found["kind"] = self.kind.currentText()
        return found

    def forget_value(self):
        """The typed token is passed on and kept nowhere else (not in a dialog that lives on)."""
        self.value.clear()

    def check_and_accept(self):
        values = self.values()
        if not self.parent().sai.valid_secret(values["secret"]):
            self.problem.setText(tr("A name is letters, digits and _ (up to 64), not starting with a digit."))
            return
        if self.typed.isChecked() and not values["value"]:
            self.problem.setText(tr("Enter the token, or use a stored secret."))
            return
        if "url" in self.edits and self.kind is None and not values["url"]:
            self.problem.setText(tr("Give the server's address."))
            return
        # The address as sandbox-ai checks it, before anything is kept: refused here, the dialog stays open with
        # everything typed (the token too) and no secret is stored for a login that would be refused.
        command = getattr(self.parent(), "command", None)
        check = getattr(getattr(self.parent(), "sai", None), "login_url_problem", None)
        problem = check(command, values) if command and check else None
        if problem:
            self.problem.setText(tr("Not accepted: {reason}", reason=problem))
            return
        self.accept()


class CredentialsPage(QWidget):
    """Everything configured at a glance, a tab per kind for the details. Names only, values never shown."""

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal, self.loaded = sai, terminal, False
        self.state = {"logins": [], "secrets": [], "store": "?", "git": [], "publish": []}
        layout = QVBoxLayout(self)
        # Whose credentials: this machine's, or a host's of `sandbox-ai hosts` (over SSH; a value goes on standard
        # input there too, never as an argument). One choice for every tab.
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("This machine's credentials, or a host's (over SSH)"))
        self.hosts.changed.connect(self.host_changed)
        self.secret_host = self.hosts.box  # (the Secrets tab had a chooser of its own: the page's now)
        layout.addWidget(self.hosts)
        self.summary = plain_label(role="secondary")
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # the overview
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.addWidget(plain_label(tr("Everything configured, with its state. Click a row to open its "
                                                 "tab."), role="secondary"))
        self.overview = table([tr("Kind"), tr("Name"), tr("Where / used by"), tr("State")], stretch=2, compact=(0, 3))
        self.overview.setAccessibleName(tr("Overview"))
        self.overview.cellClicked.connect(lambda row, _column: self.open_row(row))
        self.overview.cellActivated.connect(lambda row, _column: self.open_row(row))
        overview_layout.addWidget(self.overview, 1)
        self.tabs.addTab(overview, tr("Overview"))

        # agent logins
        logins = QGroupBox(tr("Agent logins for sandboxes"))
        logins_layout = QVBoxLayout(logins)
        self.logins = table([tr("Agent"), tr("Login from"), tr("State"), tr("File")], stretch=3, compact=(0, 1, 2))
        logins_layout.addWidget(self.logins)
        row = QHBoxLayout()
        login = QPushButton(tr("Log in…"))
        login.setToolTip(tr("Runs the agent's own login inside a sandbox and keeps it for all sandboxes"))
        login.clicked.connect(lambda: self.login(False))
        logout = QPushButton(tr("Log out…"))
        logout.clicked.connect(lambda: self.login(True))
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(self.refresh)
        for widget in (login, logout, refresh):
            row.addWidget(widget)
        row.addStretch()
        logins_layout.addLayout(row)
        self.add_tab("logins", logins)

        # secrets
        secrets = QGroupBox(tr("Secrets (API keys and tokens for --secret-env and MCP servers)"))
        secrets_layout = QVBoxLayout(secrets)
        # {host: [(name, where)]} of the secret names read there; a host missing: not known (not read yet, or the
        # read failed), and a replace there is asked about (an unknown secret may exist).
        self.remote_secrets, self.secrets_token = {}, 0
        self.backend_label = plain_label()
        secrets_layout.addWidget(self.backend_label)
        # Where a secret kept now goes on the chosen host (`sandbox-ai secrets backend` there: keyring or file).
        self.store_label, self.store_token = plain_label(role="secondary"), 0
        secrets_layout.addWidget(self.store_label)
        self.secrets = table([tr("Name"), tr("Kept in"), tr("Used by")], stretch=2, compact=(1,))
        self.secrets.itemSelectionChanged.connect(lambda: self.name.setText(selected(self.secrets) or self.name.text()))
        secrets_layout.addWidget(self.secrets, 1)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("API_TOKEN")
        self.value = QLineEdit()
        self.value.setPlaceholderText(tr("the value (passed on standard input; a kept one is never read back)"))
        self.value.setAccessibleName(tr("Value"))  # its form label names the field's row, not the field itself
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Value"), widgets.secret_value(self.value))
        secrets_layout.addLayout(form)
        row = QHBoxLayout()
        keep = self.add_button = QPushButton(tr("Add"))
        keep.setToolTip(tr("Keep the value under that name for good (sandbox-ai secrets set, on standard input); a "
                           "name kept already is replaced only after a yes"))
        keep.clicked.connect(self.set_secret)
        delete = self.remove_button = QPushButton(tr("Remove…"))
        delete.setToolTip(tr("Remove the selected secret (asked first): every start, VPN profile and Git login that "
                             "names it goes without it"))
        delete.clicked.connect(self.delete_secret)
        self.secret_buttons = [keep, delete]
        self.generation = 0
        for widget in (keep, delete):
            row.addWidget(widget)
        row.addStretch()
        secrets_layout.addLayout(row)
        self.message = plain_label()
        secrets_layout.addWidget(self.message)
        self.add_tab("secrets", secrets)

        # Git and publish logins
        self.git = GitLoginsBox(sai, self.refresh)
        self.add_tab("git", self.git)
        self.publish = PublishLoginsBox(sai, self.refresh)
        self.add_tab("publish", self.publish)

        # identities and VPN profiles: their own boxes, which tell the overview when they read their lists
        self.identities = None
        if sai.has_identities():
            from .identities_page import IdentitiesBox
            self.identities = IdentitiesBox(sai)
            self.identities.on_change = self.show_overview
            self.add_tab("identities", self.identities)
        from .vpn_page import VpnBox
        self.vpn = VpnBox(sai)
        self.vpn.on_change = self.show_overview
        self.add_tab("vpn", self.vpn)

    TAB_TITLES = {"logins": N_("Agent logins"), "secrets": N_("Secrets"), "git": N_("Git logins"),
                  "publish": N_("Publish logins"), "identities": N_("Identities"), "vpn": N_("VPN")}

    def add_tab(self, key, widget):
        widget.setProperty("credential_kind", key)
        self.tabs.addTab(widget, tr(self.TAB_TITLES[key]))

    def tab_of(self, key):
        for index in range(self.tabs.count()):
            if self.tabs.widget(index).property("credential_kind") == key:
                return index
        return -1

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded:
            self.loaded = True
            self.refresh()
            self.vpn.refresh()
            if self.identities is not None:
                self.identities.refresh()

    def host(self):
        return self.hosts.host() or LOCAL

    def host_changed(self, host):
        """Every tab shows HOST's from now on."""
        for box in (self.git, self.publish):
            box.host = host
            box.show_entries([], [], known=False)
        self.state.update(logins=[], secrets=[], store="?", git=[], publish=[])
        self.remote_secrets.pop(host, None)  # read again before a replace there counts as known
        fill(self.logins, [])
        self.vpn.set_host(host)
        if self.identities is not None:
            self.identities.set_host(host)
        self.refresh()
        self.load_secrets()

    def read_store(self):
        """`sandbox-ai secrets backend` on the chosen host, in the background (a late answer for another host is
        dropped)."""
        self.store_token += 1
        token, host = self.store_token, self.host()
        read = getattr(self.sai, "secrets_backend", None)
        self.store_label.setVisible(read is not None)
        if read is None:
            return
        self.store_label.setText(tr("Secrets backend: asking…"))
        background(read, host, owner=self, timeout=90,
                   done=lambda result, error: self.store_read(token, host, result, error))

    def store_read(self, token, host, result, error):
        if token != self.store_token or host != self.host():
            return
        stores = {"keyring": tr("your system keyring (Secret Service)"), "file": tr("private files (mode 0600)")}
        if error is not None or not isinstance(result, str):
            text = tr("Secrets backend: not known ({error})", error=printable(error)[:200])
        else:
            text = tr("Secrets backend (sandbox-ai secrets backend): {store}",
                      store=stores.get(result, printable(result)[:40]))
        self.store_label.setText(text if host == LOCAL else tr("On {host}: {text}", host=printable(host), text=text))

    def refresh(self):
        """Read the logins, the secrets' names and the Git and publish logins in a thread (asking the keyring may
        take seconds); on another host over SSH."""
        self.generation += 1
        token = self.generation
        host = self.host()

        def read_remote():
            found, problems = [], []
            for function in (self.sai.host_login_status, self.sai.host_git_logins, self.sai.host_publish_logins):
                try:
                    found.append(function(host))
                except Exception as exc:  # noqa: BLE001 - shown on the page; the other tabs still fill
                    found.append([])
                    problems.append(printable(exc)[:300])
            try:
                listed = self.sai.host_secrets(host)
            except Exception as exc:  # noqa: BLE001
                listed = None  # not known: a replace there is asked about
                problems.append(printable(exc)[:300])
            rows, git, publish = found
            return rows, listed, "; ".join(problems) or None, git, publish, None
        if host != LOCAL:
            self.read_store()
            self.summary.setText(tr("Asking {host}…", host=self.hosts.label(host)))
            background(read_remote, owner=self, timeout=300,
                       done=lambda result, error: self.refreshed(token, result, error, host))
            return

        def read():
            rows = self.sai.login_status()
            git, publish = [], []
            try:
                git, publish = self.sai.git_logins(), self.sai.publish_logins()
            except Exception:  # noqa: BLE001 - an older sandbox-ai: those tabs stay empty
                pass
            try:
                hosts = list(self.sai.remote_hosts())
            except Exception:  # noqa: BLE001 - an older sandbox-ai, or no list of hosts: this machine's only
                hosts = []
            try:
                return rows, self.sai.secrets(), None, git, publish, hosts
            except Exception as exc:  # noqa: BLE001 - shown on the page
                return rows, None, exc, git, publish, hosts  # not known: a replace is asked about
        # The keyring is asked with timeouts of its own (secret-tool: 10 s and 30 s); this one is the last word.
        self.read_store()
        background(read, owner=self, timeout=90,
                   done=lambda result, error: self.refreshed(token, result, error, LOCAL))

    def refreshed(self, token, result, error, host=LOCAL):
        if token != self.generation or host != self.host():
            return
        if error is not None:
            self.remote_secrets.pop(host, None)
            self.message.setText(printable(error))
            return
        rows, listed, problem, git, publish, hosts = result
        store, names = listed if listed is not None else ("?", [])
        self.state.update(logins=rows, secrets=names, store=store, git=git, publish=publish)
        if listed is None:
            self.remote_secrets.pop(host, None)
        else:
            self.remote_secrets[host] = list(names)
            publish_names(self.sai, "secret", host, [name for name, _ in names])  # for every name field
        if hosts is not None:
            self.fill_secret_hosts(hosts)
        states = {None: tr("logged in"), "missing": tr("no login"), "expired": tr("expired, log in again"),
                  "error": tr("cannot be read")}
        fill(self.logins, [[agent, self.login_source(where), states.get(problem_, problem_), source]
                           for agent, where, problem_, source in rows],
             ["ok" if problem_ is None else "warn" for _, _, problem_, _ in rows])
        if problem is not None:
            self.message.setText(printable(problem))
        self.show_secrets(host, store, names)
        secret_names = [name for name, _ in names]
        self.git.show_entries(git, secret_names, known=listed is not None)
        self.publish.show_entries(publish, secret_names, known=listed is not None)
        self.show_overview()

    def fill_secret_hosts(self, hosts):
        """The chooser as HOSTS (`sandbox-ai hosts` as just read) has it: a host removed leaves it, one added comes in;
        the choice stays while its host does (one that left: this machine, said by the chooser)."""
        from .host_choice import valid_host
        wanted = [h for h in hosts if isinstance(h, str) and h != LOCAL and valid_host(h)]
        box = self.secret_host
        listed = [box.itemData(i) for i in range(box.count())]
        if listed[1:] == wanted:
            return
        current = box.currentData()
        box.blockSignals(True)
        for index in reversed(range(1, box.count())):
            if box.itemData(index) not in wanted:
                box.removeItem(index)
        for host in wanted:
            if box.findData(host) < 0:
                box.addItem(printable(host), host)
        index = box.findData(current)
        box.setCurrentIndex(max(index, 0))
        box.blockSignals(False)
        if index < 0:
            self.hosts.changed.emit(self.hosts.host())  # its host left the list: this machine's credentials

    def secret_host_of(self):
        return self.host()

    def show_secrets(self, host, store, names):
        stores = {"keyring": tr("your system keyring (Secret Service)"), "file": tr("private files (mode 0600)")}
        text = tr("Kept in: {store}", store=stores.get(store, printable(store)))
        self.backend_label.setText(text if host == "local" else tr("On {host}: {text}", host=printable(host),
                                                                    text=text))
        uses = self.secret_uses() if host == self.host() else {}  # what uses them: that host's logins
        fill(self.secrets, [[name, where, ", ".join(uses.get(name, [])) or "—"] for name, where in names])

    def load_secrets(self):
        """The secrets of the host chosen: this machine's from the last refresh; a host's read there (names only)."""
        host = self.secret_host_of()
        self.secrets_token += 1
        token = self.secrets_token
        if host == "local":
            self.show_secrets("local", self.state.get("store", "?"), self.state.get("secrets", []))
            return
        self.remote_secrets.pop(host, None)  # not known until it answered: a replace is asked about
        fill(self.secrets, [])
        self.backend_label.setText(tr("Asking {host}…", host=printable(host)))
        background(self.sai.host_secrets, host, owner=self, timeout=90,
                   done=lambda result, error: self.secrets_read(token, host, result, error))

    def secrets_read(self, token, host, result, error):
        if token != self.secrets_token or host != self.secret_host_of():
            return
        if error is not None:
            self.remote_secrets.pop(host, None)
            self.backend_label.setText(tr("{host}: no answer", host=printable(host)) + f" ({printable(error)[:300]})")
            return
        store, names = result
        self.remote_secrets[host] = list(names)
        publish_names(self.sai, "secret", host, [name for name, _ in names])
        self.show_secrets(host, store, names)

    @staticmethod
    def login_source(where):
        return tr("sandbox-ai login store") if where == "store" else tr("the host's login") if where == "host" else where

    def secret_uses(self):
        """{secret name: [what uses it]}: Git logins, publish logins and identities (by name)."""
        uses = {}
        for entry in self.state["git"]:
            uses.setdefault(entry.get("secret"), []).append(tr("Git login {name}", name=entry.get("url", "")))
        for entry in self.state["publish"]:
            uses.setdefault(entry.get("secret"), []).append(tr("publish login {name}", name=entry.get("kind", "")))
        known = getattr(self.identities, "known", {}) if self.identities is not None else {}
        for name, identity in sorted(known.items()):
            for entry in list(identity.get("git", [])) + list(identity.get("publish", [])):
                if isinstance(entry, dict) and entry.get("secret"):
                    uses.setdefault(entry["secret"], []).append(tr("identity {name}", name=name))
        return {key: list(dict.fromkeys(value)) for key, value in uses.items()}

    def overview_rows(self):
        """[(kind key, name, where / used by, state text, colour)] of everything configured."""
        rows = []
        secret_names = [name for name, _ in self.state["secrets"]]
        states = {None: tr("logged in"), "missing": tr("no login"), "expired": tr("expired, log in again"),
                  "error": tr("cannot be read")}
        for agent, where, problem, _source in self.state["logins"]:
            rows.append(("logins", agent, self.login_source(where), states.get(problem, str(problem)),
                         "ok" if problem is None else "warn"))
        uses = self.secret_uses()
        for name, where in self.state["secrets"]:
            used = ", ".join(uses.get(name, []))
            rows.append(("secrets", name, f"{where}" + (f"; {used}" if used else ""), tr("kept"), "ok"))
        for box, key in ((self.git, "git"), (self.publish, "publish")):
            for entry in self.state[key]:
                name = entry.get("url") if key == "git" else entry.get("kind")
                where = entry.get("user", "") if key == "git" else entry.get("host", "")
                ok = entry.get("secret") in secret_names
                rows.append((key, name or "", tr("{where}, secret {secret}", where=where, secret=entry.get("secret", "")),
                             box.state(entry), "ok" if ok else "warn"))
        if self.identities is not None:
            from .identities_page import summary
            for name, identity in sorted(getattr(self.identities, "known", {}).items()):
                who, servers, publishes = summary(identity)
                missing = [e.get("secret") for e in list(identity.get("git", [])) + list(identity.get("publish", []))
                           if isinstance(e, dict) and e.get("secret") and e.get("secret") not in secret_names]
                rows.append(("identities", name, ", ".join(p for p in (who, servers, publishes) if p and p != "—"),
                             tr("secret {name} missing", name=", ".join(missing)) if missing else tr("ready"),
                             "warn" if missing else "ok"))
        for entry in getattr(self.vpn, "entries", []) or []:
            rows.append(("vpn", entry.get("profile", ""), " ".join(v for v in (entry.get("type"), entry.get("provider"))
                                                                   if v), tr("configured"), "ok"))
        return rows

    def show_overview(self):
        rows = self.overview_rows()
        kinds = dict(KINDS)
        fill(self.overview, [[tr(kinds[key]), name, where, state] for key, name, where, state, _ in rows],
             [colour for *_, colour in rows], keys=[f"{key}\t{name}" for key, name, *_ in rows])
        self.overview_keys = [(key, name) for key, name, *_ in rows]
        problems = sum(colour != "ok" for *_, colour in rows)
        self.summary.setText(tr("{count} configured, {problems} need attention.", count=len(rows), problems=problems)
                             if rows else tr("Nothing configured yet: log in to an agent, keep a secret."))
        for key, _ in KINDS:
            index = self.tab_of(key)
            if index < 0:
                continue
            count = sum(r[0] == key for r in rows)
            warn_ = any(r[0] == key and r[4] != "ok" for r in rows)
            self.tabs.setTabText(index, f"{tr(self.TAB_TITLES[key])} ({count})")
            self.tabs.setTabIcon(index, theme.dot("warn") if warn_ else theme.dot("ok") if count else theme.dot("muted"))

    def open_row(self, row):
        """Open the tab of the overview's row ROW, with its entry selected."""
        key = self.overview.item(row, 0).data(Qt.ItemDataRole.UserRole) if self.overview.item(row, 0) else None
        keys = getattr(self, "overview_keys", [])
        if not isinstance(key, int) or not 0 <= key < len(keys):
            return None
        kind, name = keys[key]
        index = self.tab_of(kind)
        if index < 0:
            return None
        self.tabs.setCurrentIndex(index)
        target = {"logins": self.logins, "secrets": self.secrets, "git": self.git.list, "publish": self.publish.list,
                  "identities": getattr(self.identities, "list", None), "vpn": self.vpn.profiles}.get(kind)
        if target is not None:
            found = next((r for r in range(target.rowCount()) if target.item(r, 0) is not None
                          and target.item(r, 0).text() == name), -1)
            if found >= 0:
                target.selectRow(found)
        return kind

    def login(self, logout):
        """`sandbox-ai login AGENT [--logout]` in a terminal tab on the host chosen (ssh -tt for another host)."""
        agent, host = selected(self.logins), self.host()
        if agent not in self.sai.agents:
            QMessageBox.information(self, tr("Logins"), tr("Select an agent first."))
            return
        where = "" if host == LOCAL else f" ({host})"
        if logout:
            text = tr("Forget the stored {agent} login? Sandboxes then use the host's login again.", agent=agent)
            if not ask_first(self, "login-logout", tr("Log out"), text if host == LOCAL else
                             tr("On {host}: {text}", host=printable(host), text=text), tr("Log out")):
                return
            argv = self.sai.command("login", agent, "--logout") if host == LOCAL else \
                self.sai.login_argv(host, agent, logout=True)
            self.terminal(argv, f"sandbox-ai login {agent} --logout{where}")
        else:
            argv = self.sai.command("login", agent) if host == LOCAL else self.sai.login_argv(host, agent)
            self.terminal(argv, f"sandbox-ai login {agent}{where}")

    def set_secret(self):
        name, value, host = self.name.text().strip(), self.value.text(), self.secret_host_of()
        if not self.sai.valid_secret(name):
            QMessageBox.warning(self, tr("Secret"), tr("A name is letters, digits and _ (up to 64), not starting "
                                                       "with a digit."))
            return
        if not value:
            QMessageBox.warning(self, tr("Secret"), tr("Enter the value first."))
            return
        # A secret of that name: its value is replaced, for everything that names it - asked first (not known: too).
        listed = self.remote_secrets.get(host)  # None: not read (yet), or the read failed
        exists = None if listed is None else any(found == name for found, _ in listed)
        text = tr("A secret {name} is kept already: replace its value? Everything that names it gets the new one.",
                  name=printable(name)) if exists else             tr("Whether a secret {name} is kept there could not be told: a value it has would be replaced. Go on?",
               name=printable(name))
        if host != "local":
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "secret-replace", tr("Secret"), text, tr("Replace"), detail={"exists": exists}):
            # Cancel keeps what was typed (it is still the human's to send or clear).
            self.message.setText(tr("Not kept: the secret {name} stays as it is.", name=printable(name)))
            return
        self.value.clear()  # after the yes: the value goes on standard input of the command, and nowhere else
        if host == "local":
            background(self.sai.set_secret, name, value, owner=self, work=True, source=self.secret_buttons, timeout=90,
                       done=self.secret_kept)
        else:
            # (Handed over, not referred to by a closure: the line below would empty it before the thread reads it.)
            background(functools.partial(self.sai.set_secret, name, value, host=host), owner=self,
                       source=self.secret_buttons, work=True, timeout=120, done=self.secret_kept)
        value = None  # noqa: F841 - not kept here (the call lets go of it once it has reported)

    def secret_kept(self, result, error):
        status, out, err = outcome(result, error)
        self.message.setText(printable((out + err).strip()) if status == 0 else
                             tr("Not kept: {error}", error=printable(err.strip() or out.strip())))
        self.secrets_changed()

    def secrets_changed(self):
        self.refresh()

    def delete_secret(self):
        name, host = self.name.text().strip() or selected(self.secrets), self.secret_host_of()
        if not name or not self.sai.valid_secret(name):
            QMessageBox.information(self, tr("Secret"), tr("Select a secret first."))
            return
        text = tr("Remove the secret {name}? Every start, VPN profile and Git login that names it goes without "
                  "it.", name=printable(name))
        if host != "local":
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "secret-delete", tr("Remove secret"), text, tr("Remove")):
            return
        if host == "local":
            background(self.sai.delete_secret, name, owner=self, work=True, source=self.secret_buttons, timeout=90,
                       done=self.secret_deleted)
        else:
            background(lambda: self.sai.delete_secret(name, host=host), owner=self, source=self.secret_buttons,
                       work=True, timeout=120, done=self.secret_deleted)

    def secret_deleted(self, result, error):
        status, out, err = outcome(result, error)
        self.message.setText(printable((out + err).strip()))
        self.secrets_changed()


__all__ = ["CredentialsPage"]
