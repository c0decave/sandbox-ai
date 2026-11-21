"""The Agent logins tab's second list, "Logins of the sessions" (the Credentials page): which login each running or kept
session of the host chosen uses (`sandbox-ai login --status --sessions --json`), refreshed in the background and never
on the GUI thread.

Each row: the session, its agent, its state, where its login came from (the central login, this computer's own, a
login file, from session X, none), the account as its login file says, the kind (subscription or API key), when the
access token expires and the login's fingerprint (the first 8 hex characters of the SHA-256 of its tokens: never a
token). A double click (or Enter, or Details…) opens a dialog with every field. Its buttons, also on the right-click
menu (the app's rule for every list):
- Import as central login… - `sandbox-ai login AGENT --from-session NAME --expect FINGERPRINT` on that host, after a
  confirmation that names the account and what it replaces (login-import);
- Use for a new start… - the Board with that host, the session's agent and "Login from: a session" set to it.

Everything a login file says was written by the agent in that sandbox: shown through printable() as plain text."""
from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QPushButton, QVBoxLayout, QWidget,
)

from . import applog
from .files import printable, printable_text
from .i18n import N_, tr
from .name_fields import alive
from .widgets import (
    ask_first, background, explanation, fill, flow, inform, list_menu, plain_label, row_key, selected_rows, set_enabled,
    table,
)

LOCAL = "local"
FINGERPRINT = re.compile(r"[0-9a-f]{8}")
SOURCES = {"store": N_("central login"), "host": N_("this computer's login"), "file": N_("a login file"),
           "none": N_("no login")}
KINDS = {"subscription": N_("subscription"), "api-key": N_("API key"),
         "subscription+api-key": N_("subscription and API key")}
REFRESH_WAIT = 150  # seconds a host may take to list its sessions' logins (over SSH)


def when(value):
    return printable(str(value or "")).replace("T", " ").replace("Z", "")[:19]


def source_text(row):
    """Where the session's login came from, in words."""
    if row.get("source") == "session":
        return tr("from session {name}", name=printable(str(row.get("source_session") or "?")))
    key = row.get("source")
    return tr(SOURCES[key]) if key in SOURCES else printable(str(key or "?"))


def account_text(info):
    """The account of a login (describe's "account") in one line, as its file says."""
    account = info.get("account") if isinstance(info, dict) and isinstance(info.get("account"), dict) else {}
    providers = account.get("providers") if isinstance(account.get("providers"), list) else []
    parts = [account.get(key) for key in ("email", "organization", "account_id")] + providers
    return printable(", ".join(str(part) for part in parts if isinstance(part, (str, int)) and str(part)))


def kind_text(info):
    kind = info.get("kind") if isinstance(info, dict) else None
    return tr(KINDS[kind]) if kind in KINDS else printable(str(kind or ""))


def info_of(row):
    return row.get("info") if isinstance(row.get("info"), dict) else {}


def detail_rows(row):
    """[(label, value)] of a session's login for its details dialog: what its file says, never a token."""
    info = info_of(row)
    account = info.get("account") if isinstance(info.get("account"), dict) else {}
    providers = account.get("providers") if isinstance(account.get("providers"), list) else []
    rows = [(tr("Session"), printable(str(row.get("name") or ""))), (tr("Agent"), printable(str(row.get("agent") or ""))),
            (tr("State"), printable(str(row.get("state") or ""))), (tr("Login from"), source_text(row)),
            (tr("Account (as the file says)"), printable(str(account.get("email") or ""))),
            (tr("Organization"), printable(str(account.get("organization") or ""))),
            (tr("Plan"), printable(str(account.get("plan") or ""))),
            (tr("Account id"), printable(str(account.get("account_id") or ""))),
            (tr("Providers"), printable(", ".join(str(p) for p in providers))), (tr("Kind"), kind_text(info)),
            (tr("Expires"), when(info.get("expires"))), (tr("Renewable to"), when(info.get("refresh_expires"))),
            (tr("Last refresh"), when(info.get("last_refresh"))),
            (tr("Fingerprint"), printable(str(info.get("fingerprint") or ""))),
            (tr("The same login as the central one"), (tr("yes") if row.get("shares_central") else tr("no"))
             if row.get("login") else ""),
            (tr("Problem"), printable(str(row.get("problem") or "")))]
    return [(label, value) for label, value in rows if value]


class SessionLoginDialog(QDialog):
    """Every field of one session's login (detail_rows): what its file says, its fingerprint - no secret."""

    def __init__(self, parent, row, host=LOCAL):
        super().__init__(parent)
        where = "" if host == LOCAL else " " + tr("on {host}", host=printable(host))
        self.setWindowTitle(tr("Login of {name}", name=printable(str(row.get("name") or ""))) + where)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.fields = {}
        for label, value in detail_rows(row):
            field = plain_label(value, selectable=True)
            self.fields[label] = field
            form.addRow(label, field)
        layout.addLayout(form)
        layout.addWidget(plain_label(tr("What a session's login file says about its account was written by the agent "
                                        "in that sandbox: shown as the file says, not checked."), role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SessionLoginsBox(QWidget):
    """The list "Logins of the sessions" with its buttons. CHANGED(): the central logins may have changed (an import):
    the page reads its own list again. USE(host, agent, name): Use for a new start… GIVE(host, agent, name, message,
    source): Give to a running sandbox… (this session's login for another sandbox of its agent that runs there)."""

    def __init__(self, sai, changed=None, use=None, give=None, parent=None):
        super().__init__(parent)
        self.sai, self.changed, self.use, self.give = sai, changed, use, give
        self.host, self.rows, self.central, self.token, self.dialog = LOCAL, [], {}, 0, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(plain_label(tr("Logins of the sessions"), role="section"))
        layout.addWidget(explanation(tr(
            "Which login each running or kept session uses: where it came from, its account as its file says, and a "
            "fingerprint (never a token). Two sessions with the same fingerprint share one login.")))
        self.list = table([tr("Session"), tr("Agent"), tr("State"), tr("Login from"), tr("Account (as the file says)"),
                           tr("Kind"), tr("Expires"), tr("Fingerprint")], stretch=4, compact=(1, 2, 5, 7))
        self.list.setAccessibleName(tr("Logins of the sessions"))
        self.list.cellDoubleClicked.connect(lambda _row, _column: self.details())
        self.list.cellActivated.connect(lambda _row, _column: self.details())
        layout.addWidget(self.list, 1)
        self.details_button = QPushButton(tr("Details…"))
        self.details_button.setToolTip(tr("Every field of the session's login (no secret)"))
        self.details_button.clicked.connect(lambda _=False: self.details())
        self.import_button = QPushButton(tr("Import as central login…"))
        self.import_button.setToolTip(tr("Make this session's login the central login of its agent (sandbox-ai login "
                                         "AGENT --from-session NAME): every sandbox of that agent started from now on "
                                         "uses it; asked first"))
        self.import_button.clicked.connect(lambda _=False: self.import_login())
        self.use_button = QPushButton(tr("Use for a new start…"))
        self.use_button.setToolTip(tr("Open the Board with this session's login for the start (Login from: a "
                                      "session; --auth-from-session)"))
        self.use_button.clicked.connect(lambda _=False: self.use_for_start())
        self.give_button = QPushButton(tr("Give to a running sandbox…"))
        self.give_button.setToolTip(tr("Copy this session's login into another sandbox of its agent that runs on "
                                       "this host (sandbox-ai login give RUN --from-session NAME); shown first, "
                                       "asked first"))
        self.give_button.clicked.connect(lambda _=False: self.give_to_running())
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setToolTip(tr("Read the sessions' logins again (on the host chosen)"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        # (a row that wraps: five buttons with their German names are wider than a small window)
        layout.addWidget(flow(self.details_button, self.import_button, self.use_button, self.give_button,
                              self.refresh_button))
        self.message = plain_label("", role="secondary")
        layout.addWidget(self.message)
        list_menu(self.list, lambda: [self.details_button, self.import_button, self.use_button, self.give_button,
                                      self.refresh_button])
        self.list.itemSelectionChanged.connect(self.update_buttons)
        self.update_buttons()

    # -- reading --

    def set_host(self, host):
        """Show HOST's sessions from now on (what is on its way for the host before is dropped)."""
        self.host = host or LOCAL
        self.rows, self.central = [], {}
        fill(self.list, [])
        self.update_buttons()
        self.refresh()

    def refresh(self):
        """`sandbox-ai login --status --sessions --json` on the host chosen, in the background."""
        self.token += 1
        token, host = self.token, self.host
        self.message.setText(tr("Reading the sessions' logins…"))
        background(lambda: self.sai.session_logins(host), owner=self, timeout=REFRESH_WAIT,
                   done=lambda result, error: self.read(token, host, result, error))

    def read(self, token, host, result, error):
        if not alive(self) or token != self.token or host != self.host:
            return
        if error is not None:
            self.message.setText(tr("Could not read the sessions' logins: {error}", error=printable_text(str(error))[:300]))
            return
        self.rows = [row for row in result.get("sessions") or [] if isinstance(row, dict)]
        self.central = result.get("central") if isinstance(result.get("central"), dict) else {}
        self.show_rows()

    def show_rows(self):
        cells, colours = [], []
        for row in self.rows:
            info = info_of(row)
            cells.append([printable(str(row.get("name") or "")), printable(str(row.get("agent") or "")),
                          printable(str(row.get("state") or "")), source_text(row),
                          account_text(info) if row.get("login") else printable(str(row.get("problem") or
                                                                                    tr("no login"))),
                          kind_text(info) if row.get("login") else "", when(info.get("expires")),
                          printable(str(info.get("fingerprint") or ""))])
            colours.append(None if row.get("login") else "muted")
        fill(self.list, cells, colours, keys=[f"{row.get('id')}\t{row.get('name')}" for row in self.rows])
        self.message.setText(tr("{count} sessions.", count=len(self.rows)) if self.rows else
                             tr("No running or kept session of an agent whose login sandbox-ai keeps."))
        self.update_buttons()

    # -- the selected row --

    def chosen(self):
        rows = selected_rows(self.list)
        if len(rows) != 1:
            return None
        key = row_key(self.list, rows[0])
        return next((row for row in self.rows if f"{row.get('id')}\t{row.get('name')}" == key), None)

    def update_buttons(self):
        row = self.chosen()
        usable = bool(row and row.get("login") and FINGERPRINT.fullmatch(str(info_of(row).get("fingerprint") or "")))
        why = None if usable else tr("Select a session with a login first.")
        set_enabled(self.details_button, row is not None, None if row is not None else tr("Select a session first."))
        set_enabled(self.import_button, usable, why)
        set_enabled(self.use_button, usable and self.use is not None, why)
        set_enabled(self.give_button, usable and self.give is not None, why)

    def details(self):
        row = self.chosen()
        if row is None:
            return None
        self.dialog = SessionLoginDialog(self, row, self.host)
        self.dialog.open()
        return self.dialog

    # -- what it does --

    def import_text(self, row):
        """The confirmation of an import: the account it is and what it replaces."""
        info, agent = info_of(row), printable(str(row.get("agent") or ""))
        account = account_text(info) or printable(str(info.get("fingerprint") or ""))
        stored = self.central.get(row.get("agent")) if isinstance(self.central.get(row.get("agent")), dict) else None
        where = "" if self.host == LOCAL else " " + tr("on {host}", host=printable(self.host))
        if stored and stored.get("fingerprint") == info.get("fingerprint"):
            return None
        if stored:
            old = account_text(stored) or printable(str(stored.get("fingerprint") or ""))
            return tr("Make the {agent} login of {name} ({account}) the central login{where}? It replaces the central "
                      "login ({old}) for every {agent} sandbox started from now on.", agent=agent,
                      name=printable(str(row.get("name"))), account=account, where=where, old=old)
        return tr("Make the {agent} login of {name} ({account}) the central login{where}? Every {agent} sandbox "
                  "started from now on uses it.", agent=agent, name=printable(str(row.get("name"))), account=account,
                  where=where)

    def import_login(self):
        row = self.chosen()
        if row is None or not row.get("login"):
            return False
        text = self.import_text(row)
        if text is None:
            inform(self, tr("Import as central login"), tr("It is the central login already."))
            return False
        if not ask_first(self, "login-import", tr("Import as central login"), text, tr("Import")):
            return False
        host, agent, name = self.host, row.get("agent"), row.get("name")
        fingerprint = info_of(row).get("fingerprint")
        stored = self.central.get(agent)
        replace = bool(isinstance(stored, dict) and stored.get("fingerprint") != fingerprint)
        self.message.setText(tr("Importing the login of {name}…", name=printable(str(name))))
        background(lambda: self.sai.import_session_login(host, agent, name, fingerprint, replace), owner=self,
                   work=True, source=self.import_button, timeout=240,
                   done=lambda result, error: self.imported(host, row, result, error))
        return True

    def imported(self, host, row, result, error):
        if not alive(self):
            return
        if error is not None:
            applog.log("WARNING", "session_logins_view", f"import of a session's login failed: "
                                                         f"{printable_text(str(error))[:300]}")
            self.message.setText(tr("Not imported: {error}", error=printable_text(str(error))[:300]))
            return
        notes = [printable(str(note)) for note in (result.get("notes") or []) if isinstance(note, str)]
        self.message.setText(" ".join([tr("The {agent} login of {name} is the central login now.",
                                          agent=printable(str(row.get("agent"))), name=printable(str(row.get("name")))),
                                       *notes]))
        if host == self.host:
            self.refresh()
        if self.changed is not None:
            self.changed()

    def give_to_running(self):
        """Give to a running sandbox…: this session's login for another sandbox of its agent that runs on the host
        chosen (the page's flow: chosen, shown, asked, given)."""
        row = self.chosen()
        if row is None or not row.get("login") or self.give is None:
            return False
        self.give(self.host, row.get("agent"), row.get("name"), self.message, self.give_button)
        return True

    def use_for_start(self):
        row = self.chosen()
        if row is None or not row.get("login") or self.use is None:
            return False
        self.use(self.host, row.get("agent"), row.get("name"))
        return True
