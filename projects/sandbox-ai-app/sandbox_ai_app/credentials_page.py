"""The Credentials page: an overview of everything configured (agent logins, secrets, Git logins, publish logins,
identities, VPN profiles) with its state, and a tab per kind to look at and change it. Names only: no value of a
secret is ever shown, read back or passed as an argument (sandbox-ai secrets set reads it from standard input).

A click on a row of the overview opens its tab with the row selected. Every change goes through sandbox-ai's own
commands (login, secrets, git-logins, publish-logins, identities, vpn).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from . import theme
from .backend import SandboxAI
from .files import printable
from .i18n import N_, tr
from .widgets import ask_first, background, fill, outcome, plain_label, selected, table

LOCAL = "local"  # the host of this machine's own secrets (sandbox-ai hosts' name for it)
# The overview's kinds, in the order of the tabs.
KINDS = (("logins", N_("Agent login")), ("secrets", N_("Secret")), ("git", N_("Git login")),
         ("publish", N_("Publish login")), ("identities", N_("Identity")), ("vpn", N_("VPN profile")))


class LoginsBox(QGroupBox):
    """Base of the Git and publish logins: a table, Add, Remove and Check, all through sandbox-ai's commands."""
    command = ""
    title = ""
    headers = ()

    def __init__(self, sai, changed):
        super().__init__(tr(self.title))
        self.sai, self.changed, self.entries, self.secret_names = sai, changed, [], []
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(self.about(), role="secondary"))
        self.list = table([tr(h) for h in self.headers])
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.add_button, self.remove_button, self.check_button = \
            QPushButton(tr("Add…")), QPushButton(tr("Remove…")), QPushButton(tr("Check"))
        self.add_button.clicked.connect(lambda _=False: self.add())
        self.remove_button.clicked.connect(lambda _=False: self.remove())
        self.check_button.clicked.connect(lambda _=False: self.check())
        for widget in (self.add_button, self.remove_button, self.check_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        self.message = plain_label()
        layout.addWidget(self.message)

    def buttons(self):
        return [self.add_button, self.remove_button, self.check_button]

    def show_entries(self, entries, secret_names):
        self.entries, self.secret_names = entries, secret_names
        fill(self.list, [self.row(e) for e in entries], ["ok" if e.get("secret") in secret_names else "warn"
                                                         for e in entries])

    def state(self, entry):
        return tr("ready") if entry.get("secret") in self.secret_names else \
            tr("secret {name} missing", name=printable(entry.get("secret", "")))

    def run(self, *args, what):
        self.message.setText(tr("Working…"))
        background(lambda: self.sai.call(self.command, *args, timeout=60), owner=self, timeout=90,
                   source=self.buttons(), done=lambda result, error: self.ran(what, result, error))

    def ran(self, what, result, error):
        status, out, err = outcome(result, error)
        said = printable((out + err).strip())[-1500:]
        self.message.setText(said if status == 0 or what == "check" else tr("Not done: {error}", error=said))
        if what != "check":
            self.changed()

    def check(self):
        self.run("check", what="check")


class GitLoginsBox(LoginsBox):
    command = "git-logins"
    title = N_("Git logins (sandbox-ai git-logins)")
    headers = (N_("Git server"), N_("User"), N_("Secret"), N_("State"))

    def about(self):
        return tr("The connection proxy of every sandbox logs in to these Git servers on the wire (clone, fetch, push); "
                  "no token enters a sandbox. The token is the secret named.")

    def row(self, entry):
        return [entry.get("url", ""), entry.get("user", ""), entry.get("secret", ""), self.state(entry)]

    def add(self):
        dialog = LoginDialog(self, tr("New Git login"), self.secret_names,
                             ((tr("Git server"), "url", "https://git.example.org"),
                              (tr("User"), "user", "x-access-token")))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        # Every sandbox's proxy logs in there from now on: asked first (git-login-add).
        if not ask_first(self, "git-login-add", tr("New Git login"), "\n\n".join([printable(values["url"]),
                                                                                  self.about()]), tr("Add")):
            return None
        args = ["add", values["url"], "--secret", values["secret"]] + (["--user", values["user"]]
                                                                      if values["user"] else [])
        self.run(*args, what="add")
        return values

    def remove(self):
        url = selected(self.list)
        if not url or not ask_first(self, "login-remove", tr("Remove"),
                                    tr("Remove the Git login for {name}? Its secret stays.", name=url), tr("Remove")):
            return
        self.run("remove", url, what="remove")


class PublishLoginsBox(LoginsBox):
    command = "publish-logins"
    title = N_("Publish logins (sandbox-ai publish-logins)")
    headers = (N_("Kind"), N_("Server"), N_("Secret"), N_("State"))

    def about(self):
        return tr("Package registries and forges a start may publish to with --publish KIND; the proxy adds the token "
                  "on the wire and asks before every upload.")

    def row(self, entry):
        return [entry.get("kind", ""), entry.get("host", ""), entry.get("secret", ""), self.state(entry)]

    def add(self):
        dialog = LoginDialog(self, tr("New publish login"), self.secret_names,
                             ((tr("Own server (optional)"), "url", "https://gitlab.example.org"),),
                             kinds=tuple(SandboxAI.PUBLISH_HOSTS))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        self.run("add", values["kind"], "--secret", values["secret"],
                 *(["--url", values["url"]] if values["url"] else []), what="add")
        return values

    def remove(self):
        kind = selected(self.list)
        if not kind or not ask_first(self, "login-remove", tr("Remove"),
                                     tr("Remove the publish login for {name}? Its secret stays.", name=kind),
                                     tr("Remove")):
            return
        self.run("remove", kind, what="remove")


class LoginDialog(QDialog):
    """A Git or publish login: its fields and the NAME of the secret holding the token (offered as typed)."""

    def __init__(self, parent, title, secret_names, fields, kinds=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QFormLayout(self)
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
        self.secret = QLineEdit()
        self.secret.setPlaceholderText("GIT_TOKEN")
        self.secret.setCompleter(QCompleter(list(secret_names), self.secret))
        form.addRow(tr("Secret (its name)"), self.secret)
        form.addRow(plain_label(tr("Keep the token first on the Secrets tab; only its name is written here."),
                                role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.problem = plain_label(role="notice-error")
        form.addRow(self.problem)

    def values(self):
        found = {key: edit.text().strip() for key, edit in self.edits.items()}
        found["secret"] = self.secret.text().strip()
        if self.kind is not None:
            found["kind"] = self.kind.currentText()
        return found

    def check_and_accept(self):
        values = self.values()
        if not self.parent().sai.valid_secret(values["secret"]):
            self.problem.setText(tr("A name is letters, digits and _ (up to 64), not starting with a digit."))
            return
        if "url" in self.edits and self.kind is None and not values["url"]:
            self.problem.setText(tr("Give the server's address."))
            return
        self.accept()


class CredentialsPage(QWidget):
    """Everything configured at a glance, a tab per kind for the details. Names only, values never shown."""

    def __init__(self, sai, terminal):
        super().__init__()
        self.sai, self.terminal, self.loaded = sai, terminal, False
        self.state = {"logins": [], "secrets": [], "store": "?", "git": [], "publish": []}
        layout = QVBoxLayout(self)
        self.summary = plain_label(role="secondary")
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # the overview
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.addWidget(plain_label(tr("Everything configured, with its state. Click a row to open its "
                                                 "tab."), role="secondary"))
        self.overview = table([tr("Kind"), tr("Name"), tr("Where / used by"), tr("State")])
        self.overview.cellClicked.connect(lambda row, _column: self.open_row(row))
        self.overview.cellActivated.connect(lambda row, _column: self.open_row(row))
        overview_layout.addWidget(self.overview, 1)
        self.tabs.addTab(overview, tr("Overview"))

        # agent logins
        logins = QGroupBox(tr("Agent logins for sandboxes"))
        logins_layout = QVBoxLayout(logins)
        self.logins = table([tr("Agent"), tr("Login from"), tr("State"), tr("File")])
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
        # Whose secrets: this machine's, or a host's of `sandbox-ai hosts` (secrets list, set and delete there, over
        # SSH; a value goes on standard input there too, never as an argument).
        host_row = QHBoxLayout()
        self.secret_host = QComboBox()
        self.secret_host.setAccessibleName(tr("Secrets on"))
        self.secret_host.setToolTip(tr("This machine's secrets, or a host's (over SSH)"))
        self.secret_host.addItem(tr("This machine"), LOCAL)
        self.secret_host.activated.connect(lambda _=0: self.load_secrets())
        host_row.addWidget(QLabel(tr("Secrets on")))
        host_row.addWidget(self.secret_host)
        host_row.addStretch()
        secrets_layout.addLayout(host_row)
        self.remote_secrets, self.secrets_token = {}, 0  # {host: [(name, where)]} as last read there
        self.backend_label = plain_label()
        secrets_layout.addWidget(self.backend_label)
        self.secrets = table([tr("Name"), tr("Kept in"), tr("Used by")])
        self.secrets.itemSelectionChanged.connect(lambda: self.name.setText(selected(self.secrets) or self.name.text()))
        secrets_layout.addWidget(self.secrets, 1)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("API_TOKEN")
        self.value = QLineEdit()
        self.value.setEchoMode(QLineEdit.EchoMode.Password)
        self.value.setPlaceholderText(tr("the value (never shown, passed on standard input)"))
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Value"), self.value)
        secrets_layout.addLayout(form)
        row = QHBoxLayout()
        keep = QPushButton(tr("Keep secret"))
        keep.clicked.connect(self.set_secret)
        delete = QPushButton(tr("Delete…"))
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

    def refresh(self):
        """Read the logins, the secrets' names and the Git and publish logins in a thread (asking the keyring may
        take seconds)."""
        self.generation += 1
        token = self.generation

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
                return rows, ("?", []), exc, git, publish, hosts
        # The keyring is asked with timeouts of its own (secret-tool: 10 s and 30 s); this one is the last word.
        background(read, owner=self, timeout=90, done=lambda result, error: self.refreshed(token, result, error))

    def refreshed(self, token, result, error):
        if token != self.generation:
            return
        if error is not None:
            self.message.setText(printable(error))
            return
        rows, (store, names), problem, git, publish, hosts = result
        self.state.update(logins=rows, secrets=names, store=store, git=git, publish=publish)
        self.fill_secret_hosts(hosts)
        states = {None: tr("logged in"), "missing": tr("no login"), "expired": tr("expired, log in again"),
                  "error": tr("cannot be read")}
        fill(self.logins, [[agent, self.login_source(where), states.get(problem_, problem_), source]
                           for agent, where, problem_, source in rows],
             ["ok" if problem_ is None else "warn" for _, _, problem_, _ in rows])
        if problem is not None:
            self.message.setText(printable(problem))
        if self.secret_host.currentData() in (None, "local"):
            self.show_secrets("local", store, names)
        secret_names = [name for name, _ in names]
        self.git.show_entries(git, secret_names)
        self.publish.show_entries(publish, secret_names)
        self.show_overview()

    def fill_secret_hosts(self, hosts):
        """This machine and each host of `sandbox-ai hosts`; the choice stays while its host does."""
        chosen = self.secret_host.currentData()
        self.secret_host.blockSignals(True)
        self.secret_host.clear()
        self.secret_host.addItem(tr("This machine"), LOCAL)
        for host in hosts:
            if isinstance(host, str) and host != "local":
                self.secret_host.addItem(printable(host), host)
        index = self.secret_host.findData(chosen)
        self.secret_host.setCurrentIndex(max(0, index))
        self.secret_host.blockSignals(False)
        if index < 0 and chosen not in (None, "local"):
            self.load_secrets()  # its host is gone: this machine's, not that host's rows under another name

    def secret_host_of(self):
        return self.secret_host.currentData() or "local"

    def show_secrets(self, host, store, names):
        stores = {"keyring": tr("your system keyring (Secret Service)"), "file": tr("private files (mode 0600)")}
        text = tr("Kept in: {store}", store=stores.get(store, printable(store)))
        self.backend_label.setText(text if host == "local" else tr("On {host}: {text}", host=printable(host),
                                                                    text=text))
        uses = self.secret_uses() if host == "local" else {}  # what uses them: this machine's logins
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
            self.backend_label.setText(tr("{host}: no answer", host=printable(host)) + f" ({printable(error)[:300]})")
            return
        store, names = result
        self.remote_secrets[host] = list(names)
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
        agent = selected(self.logins)
        if agent not in self.sai.agents:
            QMessageBox.information(self, tr("Logins"), tr("Select an agent first."))
            return
        if logout:
            if not ask_first(self, "login-logout", tr("Log out"), tr("Forget the stored {agent} login? Sandboxes then "
                                                                     "use the host's login again.", agent=agent),
                             tr("Log out")):
                return
            self.terminal(self.sai.command("login", agent, "--logout"), f"sandbox-ai login {agent} --logout")
        else:
            self.terminal(self.sai.command("login", agent), f"sandbox-ai login {agent}")

    def set_secret(self):
        name, value, host = self.name.text().strip(), self.value.text(), self.secret_host_of()
        if not self.sai.valid_secret(name):
            QMessageBox.warning(self, tr("Secret"), tr("A name is letters, digits and _ (up to 64), not starting "
                                                       "with a digit."))
            return
        if not value:
            QMessageBox.warning(self, tr("Secret"), tr("Enter the value first."))
            return
        self.value.clear()  # at once: the value goes on standard input of the command, and nowhere else
        # A secret of that name: its value is replaced, for everything that names it - asked first (not known: too).
        listed = self.state.get("secrets") if host == "local" else self.remote_secrets.get(host)
        exists = None if listed is None else any(found == name for found, _ in listed)
        text = tr("A secret {name} is kept already: replace its value? Everything that names it gets the new one.",
                  name=printable(name)) if exists else             tr("Whether a secret {name} is kept there could not be told: a value it has would be replaced. Go on?",
               name=printable(name))
        if host != "local":
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "secret-replace", tr("Secret"), text, tr("Replace"), detail={"exists": exists}):
            self.message.setText(tr("Not kept: the secret {name} stays as it is.", name=printable(name)))
            return
        if host == "local":
            background(self.sai.set_secret, name, value, owner=self, source=self.secret_buttons, timeout=90,
                       done=self.secret_kept)
        else:
            background(lambda: self.sai.set_secret(name, value, host=host), owner=self, source=self.secret_buttons,
                       timeout=120, done=self.secret_kept)
        value = None  # noqa: F841 - not kept

    def secret_kept(self, result, error):
        status, out, err = outcome(result, error)
        self.message.setText(printable((out + err).strip()) if status == 0 else
                             tr("Not kept: {error}", error=printable(err.strip() or out.strip())))
        self.secrets_changed()

    def secrets_changed(self):
        if self.secret_host_of() == "local":
            self.refresh()
        else:
            self.load_secrets()

    def delete_secret(self):
        name, host = self.name.text().strip() or selected(self.secrets), self.secret_host_of()
        if not name or not self.sai.valid_secret(name):
            QMessageBox.information(self, tr("Secret"), tr("Select a secret first."))
            return
        text = tr("Delete the secret {name}?", name=printable(name))
        if host != "local":
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "secret-delete", tr("Delete secret"), text, tr("Delete")):
            return
        if host == "local":
            background(self.sai.delete_secret, name, owner=self, source=self.secret_buttons, timeout=90,
                       done=self.secret_deleted)
        else:
            background(lambda: self.sai.delete_secret(name, host=host), owner=self, source=self.secret_buttons,
                       timeout=120, done=self.secret_deleted)

    def secret_deleted(self, result, error):
        status, out, err = outcome(result, error)
        self.message.setText(printable((out + err).strip()))
        self.secrets_changed()


__all__ = ["CredentialsPage"]
