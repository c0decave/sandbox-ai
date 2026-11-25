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
import json
import re
import types

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton, QRadioButton, QTabWidget, QVBoxLayout, QWidget,
)

from . import applog
from . import theme, widgets
from .backend import SandboxAI, valid_run
from .files import printable, printable_text
from .credential_tab import NOT_WHILE_RUNNING, CredentialTab, even_out, menu_entry
from .i18n import N_, tr
from .host_choice import on_host
from .name_fields import NameField, alive, publish as publish_names
from .widgets import (
    ask_first, background, delete_key, explanation, fill, inform, multi_select, one_only, outcome, plain_label,
    report_each, run_each, safe_tooltip, selected, selected_keys, several, show_text, table,
)

LOCAL = "local"  # the host of this machine's own secrets (sandbox-ai hosts' name for it)
GIT_URL_EXAMPLE, GIT_USER_DEFAULT = "https://git.example.org", "x-access-token"  # the same in every language
GIVE_KEY, GIVE_LOGIN = "secrets give", "git-logins give"  # the commands a host's older sandbox-ai may not know
# The overview's kinds, in the order of the tabs.
KINDS = (("logins", N_("Agent login")), ("secrets", N_("Secret")), ("git", N_("Git login")),
         ("publish", N_("Publish login")), ("identities", N_("Identity")), ("vpn", N_("VPN profile")))


LOGIN_GIVE = "login give"

# The primary password's button on the Secrets tab, named by the step that is due (CredentialsPage.protection_step).
PROTECT_NAMES = {"new": N_("Set password…"), "unlock": N_("Unlock…"), "lock": N_("Lock")}


class LoginsBox(CredentialTab):
    """Base of the Git and publish logins: a table, Add, Remove and Check, all through sandbox-ai's commands (laid
    out as every tab of the page: credential_tab.py)."""
    command = ""
    title = ""
    headers = ()
    stretch, compact = 0, ()  # the column of long text; the short ones (tables.py)

    def __init__(self, sai, changed):
        super().__init__(tr(self.title))
        self.sai, self.changed, self.entries, self.secret_names, self.host = sai, changed, [], [], LOCAL
        self.secrets_known = False  # whether secret_names are what the host said (else a replace is asked about)
        self.add_about(self.about())
        self.list = self.set_list(multi_select(table([tr(h) for h in self.headers], stretch=self.stretch,
                                                     compact=self.compact)))
        self.add_button, self.edit_button, self.remove_button, self.check_button = \
            QPushButton(tr("Add…")), QPushButton(tr("Edit…")), QPushButton(tr("Remove…")), QPushButton(tr("Check"))
        self.edit_button.setToolTip(safe_tooltip(tr(
            "Change the selected login: its server, its user, which secret holds the token (sandbox-ai {command} "
            "edit); Replace value… takes a new token - the one kept is never shown", command=self.command)))
        self.add_button.clicked.connect(lambda _=False: self.add())
        self.edit_button.clicked.connect(lambda _=False: self.edit())
        self.remove_button.clicked.connect(lambda _=False: self.remove())
        self.check_button.clicked.connect(lambda _=False: self.check())
        self.add_buttons(self.add_button, self.edit_button, self.remove_button, self.check_button,
                         *self.more_buttons())
        # several at once (Ctrl, Shift): Remove… and the Delete key take them all; what acts on one login waits
        delete_key(self.list, self.remove)
        one_only(self.list, [self.edit_button, *self.single_buttons()])
        # a double click (or Enter) on a login: Edit…
        self.list.cellDoubleClicked.connect(lambda _row, _column: self.edit())

    def more_buttons(self):
        return []

    def single_buttons(self):
        """The buttons that act on one login (greyed out while several are selected)."""
        return []

    def chosen(self):
        """The login selected in the list (its entry), or None."""
        rows = sorted({index.row() for index in self.list.selectedIndexes()})
        return self.entries[rows[0]] if rows and rows[0] < len(self.entries) else None

    def edit(self):
        """Edit…: the selected login's dialog with its fields filled in (never its token)."""

    def edit_with(self, args, values, entry, seen, then=None):
        """`sandbox-ai COMMAND edit ARGS` for the login ENTRY on the host of SEEN, asked first (login-edit); a token
        typed under Replace value… is kept first as the secret named (`secrets set`, on standard input). THEN: what
        follows a change that worked."""
        host = seen["host"]
        name = entry.get("url") or entry.get("kind") or ""
        lines = [tr("Change the login for {name}?", name=printable(name)),
                 tr("It is for the starts from now on: a sandbox that runs keeps the login it was started with.")]
        if values.get("value"):
            lines.append(tr("The token typed replaces the value of the secret {secret}, for everything that names it.",
                            secret=printable(values["secret"])))
        if not ask_first(self, "login-edit", tr(self.title), on_host(host, "\n\n".join(lines)), tr("Change")):
            self.message.setText(tr("Not changed: the login for {name} stays as it is.", name=printable(name)))
            return False
        return self.add_with_token(args, values, what="edit", seen=seen, replacing=False, then=then)

    # what Remove… says and runs, for one login (KEY: its first column) and for several
    def remove_text(self, key):
        return ""

    def remove_args(self, key):
        return []

    def remove(self):
        """Remove… (and the Delete key): the selected login, or every one of several after one question - on the host
        the question named (taken before it is asked: the chooser may show another one by the answer)."""
        if widgets.held(self.remove_button):  # (the Delete key while a removal runs)
            self.message.setText(tr("A removal is running: wait for it."))
            return
        keys = [key for key in selected_keys(self.list) if key]
        host, command = self.host, self.command
        if len(keys) <= 1:
            key = selected(self.list)
            if key and ask_first(self, "login-remove", tr("Remove"), on_host(host, self.remove_text(key)),
                                 tr("Remove")):
                self.run(*self.remove_args(key), what="remove", host=host)
            return
        if not keys or not ask_first(self, "login-remove", tr("Remove"), on_host(host, several(tr(
                "Remove these {count} logins? Their secrets stay.", count=len(keys)), [printable(k) for k in keys])),
                tr("Remove")):
            return
        self.message.setText(tr("Working…"))

        def work(key):
            if host == LOCAL:
                return self.sai.call(command, *self.remove_args(key), timeout=60)
            return self.sai.host_run(host, command, *self.remove_args(key), timeout=90)

        def done(outcomes):
            failures = report_each(self, tr(self.title), outcomes, printable)
            self.message.setText(tr("Removed {count} of {total}.", count=len(keys) - len(failures), total=len(keys)))
            self.changed()
        run_each(work, keys, owner=self, source=self.buttons(), timeout=120 * len(keys), done=done)

    def buttons(self):
        return [self.add_button, self.edit_button, self.remove_button, self.check_button, *self.more_buttons()]

    def replaces(self, values, entries=()):
        """The server or kind of an entry (of ENTRIES, the logins listed) the login of VALUES replaces, or None."""
        return None

    def seen(self):
        """What the box shows now - the host, its logins and secret names - taken before a dialog or a question: the
        answer acts on that host with what was known of it, whatever the chooser shows by then."""
        return {"host": self.host, "entries": self.entries, "secret_names": self.secret_names,
                "known": self.secrets_known}

    def show_entries(self, entries, secret_names, known=True):
        self.entries, self.secret_names, self.secrets_known = entries, secret_names, bool(known)
        fill(self.list, [self.row(e) for e in entries], ["ok" if e.get("secret") in secret_names else "warn"
                                                         for e in entries])

    def state(self, entry):
        return tr("ready") if entry.get("secret") in self.secret_names else \
            tr("secret {name} missing", name=printable(entry.get("secret", "")))

    def run(self, *args, what, host=None, then=None):
        """`sandbox-ai COMMAND ARGS` on HOST (default: the one chosen), in the background."""
        self.message.setText(tr("Working…"))
        host = self.host if host is None else host

        def work():
            if host == LOCAL:
                return self.sai.call(self.command, *args, timeout=60)
            return self.sai.host_run(host, self.command, *args, timeout=90)
        background(work, owner=self, work=True, timeout=120, source=self.buttons(),
                   done=lambda result, error: self.ran(what, result, error, host=host, then=then))

    def ran(self, what, result, error, draft=None, host=None, then=None):
        """What a command of the box said; an add taken: the DRAFT of its dialog is done with (drafts.py). THEN: what
        follows a change that worked (an edit: the offer to give a running sandbox the login as it is now)."""
        status, out, err = outcome(result, error)
        if what == "edit" and status != 0:
            old = self.sai.too_old_text(host, result, f"{self.command} edit")
            if old:
                self.message.setText(old)
                return
        if draft is not None and status == 0 and what == "add":
            from . import drafts
            drafts.drop(draft)
        said = printable_text((out + err).strip())[-1500:]
        if what == "kinds":
            show_text(self, tr("Publish login kinds"), "\n".join(printable(line) for line in out.strip().splitlines())
                      if status == 0 else said, modal=False)
            return
        self.message.setText(said if status == 0 or what == "check" else tr("Not done: {error}", error=said))
        if what != "check":
            self.changed()
        if status == 0 and then is not None:
            then()

    def check(self):
        self.run("check", what="check")

    def add_with_token(self, add_args, values, what="add", seen=None, replacing=True, then=None):
        """ADD_ARGS (after the command; they name --secret) on the host chosen (SEEN: what seen() said before the
        dialog - its host); a token typed in the dialog is kept first as that secret (`secrets set NAME`, the value on
        standard input), asked first when a secret of that name is there or may be (the host's names not read).
        REPLACING False: an edit of the login itself (it replaces no other one). THEN: what follows when it worked."""
        seen = seen or self.seen()
        host, command = seen["host"], self.command
        secret, value = values["secret"], values.get("value")
        replaced = self.replaces(values, seen["entries"]) if replacing else None
        if replaced and not ask_first(self, "login-replace", tr(self.title), on_host(host, tr(
                "A login for {name} is there already: its token is replaced by the secret {secret}'s for every later "
                "start that uses it. Replace it?", name=printable(replaced), secret=printable(secret))),
                tr("Replace")):
            self.message.setText(tr("Not added: the login for {name} stays as it is.", name=printable(replaced)))
            return False
        if not value:  # a secret kept already: the add alone
            self.run(*add_args, what=what, host=host, **({"then": then} if then is not None else {}))
            return True
        if value:
            exists = (secret in seen["secret_names"]) if seen["known"] else None
            if not ask_first(self, "secret-replace", tr(self.title), on_host(host, tr(
                    "A secret {name} is kept already (or may be): the token typed replaces its value, for everything "
                    "that names it. Replace it?", name=printable(secret))), tr("Replace"), detail={"exists": exists}):
                self.message.setText(tr("Not added: the secret {name} stays as it is.", name=printable(secret)))
                return False
        self.message.setText(tr("Working…"))
        known = exists

        def work():
            existed = known
            if existed is None:  # the host's names not read: asked now, so a refused login leaves no new secret
                try:
                    existed = self.sai.secret_exists(secret, host)
                except Exception as _exc:  # noqa: BLE001 - not answered: kept as it is whatever happens
                    applog.swallowed("credentials_page", "add_with_token.work: not answered: kept as it is what…", _exc)
                    existed = None
            return self.sai.add_login(host, command, add_args, secret, value, existed=existed)
        background(work, owner=self, work=True, timeout=150, source=self.buttons(),
                   done=lambda result, error: self.ran(what, result, error, draft=(command, host), host=host,
                                                       then=then))
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

    def replaces(self, values, entries=()):
        url = (values.get("url") or "").strip().rstrip("/").lower()
        return next((e.get("url") for e in entries if str(e.get("url", "")).rstrip("/").lower() == url), None)

    def more_buttons(self):
        if not hasattr(self, "give_button"):
            self.give_button = QPushButton(tr("Give to a running sandbox…"))
            self.give_button.setToolTip(safe_tooltip(tr(
                "A sandbox started before this login was added gets it now (sandbox-ai git-logins give RUN URL "
                "--secret NAME): its connection proxy logs in with it, no token enters the sandbox; every sandbox "
                "started later has it anyway. With no login chosen: a login whose token is in a variable, a secret "
                "or a file, not stored here")))
            self.give_button.clicked.connect(lambda _=False: self.give())
            # (Not in the row, which must fit a small window: on the list's right click, and from the Give dialog.)
            self.give_far_button = QPushButton(tr("Give to a sandbox on another host…"), self)
            self.give_far_button.hide()
            self.give_far_button.clicked.connect(lambda _=False: self.give_far())
            show_far(self.give_far_button, LOCAL)
            self.menu_only = [menu_entry(self.give_far_button)]
        return [self.give_button]

    def single_buttons(self):
        return [self.give_button]

    def set_host(self, host):
        self.host = host
        show_far(self.give_far_button, host)

    def give_far(self):
        """This machine's login for a sandbox on another host: its secret is sent there first (asked first: sandbox-ai
        secrets send NAME HOST, the value over SSH on standard input), then the login is given there as any login of
        that host."""
        entry = self.chosen()
        if entry is None or not entry.get("secret"):
            self.message.setText(tr("Choose a login in the list first."))
            return
        send_then(self, self.sai, entry["secret"], self.message, self.give_far_button,
                  lambda host: background(give_targets(self.sai, host), owner=self, source=self.give_far_button,
                                          timeout=90, done=lambda found, error: self.give_to(entry, found, error,
                                                                                             host)))

    def edit(self):
        seen, entry = self.seen(), self.chosen()
        if entry is None:
            self.message.setText(tr("Choose a login in the list first."))
            return None
        dialog = LoginDialog(self, tr("Edit Git login"), seen["secret_names"],
                             ((tr("Git server"), "url", "https://git.example.org"),
                              (tr("User"), "user", "x-access-token")), host=seen["host"], edit=entry)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        dialog.forget_value()
        user = values["user"] or "x-access-token"
        args = ["edit"]
        if values["url"].rstrip("/") != str(entry.get("url", "")).rstrip("/"):
            args.append(f"--url={values['url']}")
        if user != entry.get("user"):
            args.append(f"--user={user}")
        if values["secret"] != entry.get("secret"):
            args.append(f"--secret={values['secret']}")
        if len(args) == 1 and not values.get("value"):
            self.message.setText(tr("Nothing changed."))
            return None
        if len(args) == 1:  # a new token alone: the login itself stays (the command says so)
            args.append(f"--user={user}")
        args += ["--", str(entry.get("url", ""))]  # the URL after "--": never read as an option
        new = {"url": values["url"], "user": user, "secret": values["secret"]}
        host = seen["host"]
        self.edit_with(args, values, entry, seen, then=lambda: self.offer_give(new, host))
        return new

    def offer_give(self, entry, host):
        """After a login changed: it is for the starts from now on - the sandboxes that run on HOST keep what they
        were started with, so Give to a running sandbox… is offered for them (asked: nothing is given unasked)."""
        def found_runs(found, error):
            runs = running_on(host, found, error)
            if not runs:
                return
            names = ", ".join(run_label(run) for run in runs[:6])
            text = tr("The login for {url} is changed for the starts from now on. {count} sandbox(es) run with what "
                      "they were started with: {names}.\n\nGive the login as it is now to one of them?",
                      url=printable(entry["url"]), count=len(runs), names=names)
            if widgets.confirm(self, tr("Give to a running sandbox"), where_given(host, text), tr("Give…")):
                self.give_to(entry, found, error, host)
        background(give_targets(self.sai, host), owner=self, timeout=90, done=found_runs)

    def give(self):
        """Give the chosen login to a sandbox that runs on the host chosen (this machine or a host of sandbox-ai hosts):
        its starts are read in the background, then one is chosen, asked about (the app's one policy for widening a
        run) and `git-logins give` sent there."""
        host = self.host
        entry = self.chosen()
        if entry is None:
            # A login not stored here (what the Allow… dialog took until 2026-11-24): its server and where its token
            # is - a variable, a secret or a file of that host, never the token.
            dialog = OtherLoginDialog(self, host)
            if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted or dialog.entry() is None:
                return
            entry = dialog.entry()
            background(give_targets(self.sai, host), owner=self, source=self.give_button, timeout=90,
                       done=lambda found, error: self.give_to(entry, found, error, host))
            return
        if not (entry.get("secret") or entry.get("env") or entry.get("file")):
            self.message.setText(tr("This login names no secret: only a login with its token in a secret can be given."))
            return
        background(give_targets(self.sai, host), owner=self, source=self.give_button, timeout=90,
                   done=lambda found, error: self.give_to(entry, found, error, host))

    def give_to(self, entry, found, error, host=LOCAL):
        runs = running_on(host, found, error)
        if not runs:
            self.message.setText(nobody_runs(host) if error is None else printable_text(error))
            return
        dialog = GiveDialog(self, runs, host=host, far=host == LOCAL)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            if dialog.wants_far:  # A sandbox on another host…: this machine's login sent there first
                self.give_far()
            return
        run, keep, called = dialog.run(), dialog.keep.isChecked(), dialog.run_label()
        target = git_login_url(entry)
        question = give_login_question(target, token_place(entry), called, keep)
        if not ask_first(self, "net-allow", tr("Give the login"), where_given(host, question), tr("Give")):
            return
        self.message.setText(tr("Giving the login…"))
        give_login(self, self.sai, host, run, entry, keep, called, self.message, self.give_button)

    def add(self):
        seen = self.seen()
        dialog = LoginDialog(self, tr("New Git login"), seen["secret_names"],
                             ((tr("Git server"), "url", "https://git.example.org"),
                              (tr("User"), "user", "x-access-token")), host=seen["host"],
                             draft=(self.command, seen["host"]))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        dialog.forget_value()
        # Every sandbox's proxy logs in there from now on: asked first (git-login-add).
        if not ask_first(self, "git-login-add", tr("New Git login"), on_host(seen["host"], "\n\n".join(
                [printable(values["url"]), self.about()])), tr("Add")):
            return None
        args = ["add", "--secret", values["secret"], *(["--user", values["user"]] if values["user"] else []),
                "--", values["url"]]  # the URL after "--": never read as an option
        self.add_with_token(args, values, seen=seen)
        return {k: v for k, v in values.items() if k != "value"}

    def remove_text(self, key):
        return tr("Remove the Git login for {name}? Its secret stays.", name=printable(key))

    def remove_args(self, key):
        return ["remove", "--", key]  # the URL after "--": never read as an option


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
            # There, greyed out with the reason: a publish login cannot be given to a sandbox that runs.
            self.give_button = QPushButton(tr("Give to a running sandbox…"))
            widgets.set_enabled(self.give_button, False, why=tr(NOT_WHILE_RUNNING["publish"]))
        return [self.kinds_button, self.give_button]

    def edit(self):
        seen, entry = self.seen(), self.chosen()
        if entry is None:
            self.message.setText(tr("Choose a login in the list first."))
            return None
        kind, host = entry.get("kind", ""), entry.get("host", "")
        own = bool(host) and host != SandboxAI.PUBLISH_HOSTS.get(kind)
        dialog = LoginDialog(self, tr("Edit publish login"), seen["secret_names"],
                             ((tr("Own server (optional)"), "url", "https://gitlab.example.org"),),
                             kinds=(kind,), host=seen["host"],
                             edit={"url": f"https://{host}" if own else "", "secret": entry.get("secret", "")})
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        dialog.forget_value()
        args = ["edit", kind]
        if values["secret"] != entry.get("secret"):
            args.append(f"--secret={values['secret']}")
        if values["url"].rstrip("/") != (f"https://{host}" if own else ""):
            args.append(f"--url={values['url'] or 'default'}")
        if len(args) == 2 and not values.get("value"):
            self.message.setText(tr("Nothing changed."))
            return None
        if len(args) == 2:  # a new token alone: the login itself stays (the command says so)
            args.append(f"--secret={values['secret']}")
        self.edit_with(args, values, entry, seen)
        return {k: v for k, v in values.items() if k != "value"}

    def row(self, entry):
        return [entry.get("kind", ""), entry.get("host", ""), entry.get("secret", ""), self.state(entry)]

    def replaces(self, values, entries=()):
        return values.get("kind") if any(e.get("kind") == values.get("kind") for e in entries) else None

    def add(self):
        seen = self.seen()
        dialog = LoginDialog(self, tr("New publish login"), seen["secret_names"],
                             ((tr("Own server (optional)"), "url", "https://gitlab.example.org"),),
                             kinds=tuple(SandboxAI.PUBLISH_HOSTS), host=seen["host"], draft=(self.command, seen["host"]))
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
        dialog.forget_value()
        self.add_with_token(["add", values["kind"], "--secret", values["secret"],
                             *(["--url", values["url"]] if values["url"] else [])], values, seen=seen)
        return {k: v for k, v in values.items() if k != "value"}

    def remove_text(self, key):
        return tr("Remove the publish login for {name}? Its secret stays.", name=printable(key))

    def remove_args(self, key):
        return ["remove", key]


class LoginDialog(QDialog):
    """A Git or publish login on HOST: its fields and its token - typed here (kept as the secret named, prefilled:
    PYPI_TOKEN ... for a publish kind, GIT_TOKEN_<HOST> for a Git server; `secrets set NAME`, the value on standard
    input) or a secret kept there already (a list of that host's secret names)."""

    def __init__(self, parent, title, secret_names, fields, kinds=None, host=LOCAL, draft=None, edit=None):
        """DRAFT: the key of what was typed and not sent (drafts.py): the server, user, kind and the secret's name
        are filled in again, the token never. EDIT: the login that is edited ({url, user, secret}): its fields are
        filled in, its secret is the one chosen - its token is never read, shown or filled in; Replace value… takes
        a new one (typed, kept as that secret)."""
        super().__init__(parent)
        self.editing = edit
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
        form.addRow(explanation(tr("A secret name (e.g. PYPI_TOKEN) is how sandbox-ai keeps a token: the login only "
                                   "names it, the token itself stays in sandbox-ai secrets.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.problem = plain_label(role="notice-error", selectable=True)
        form.addRow(self.problem)
        self.typed.toggled.connect(lambda _on: self.show_choice())
        if self.kind is not None:
            self.kind.currentTextChanged.connect(lambda _text: self.offer_name())
        if "url" in self.edits:
            self.edits["url"].textChanged.connect(lambda _text: self.offer_name())
        self.offer_name()
        if self.editing is not None:
            self.fill_from(self.editing)
        self.show_choice()
        from . import drafts
        fields = {**{f"field:{key}": edit for key, edit in self.edits.items()}, "kind": self.kind,
                  "stored": self.stored, "secret": self.secret, "secret_choice": self.secret_choice}
        self.draft = None if draft is None else drafts.Draft(self, draft, {k: w for k, w in fields.items()
                                                                          if w is not None})
        # a name kept that is not the one offered was typed by hand: it stays when the server or kind changes
        self.name_typed = self.secret.text() != self.offered_name()

    def fill_from(self, entry):
        """The dialog of the login ENTRY that is edited: its server and user filled in, its secret chosen (kept as it
        is unless Replace value… is chosen: then a new token is typed, never the old one shown)."""
        for key, field in self.edits.items():
            field.setText(str(entry.get(key) or ""))
        if self.kind is not None:
            self.kind.setEnabled(False)  # (the kind names the login: another kind is another login)
        secret = str(entry.get("secret") or "")
        self.typed.setText(tr("Replace value…"))
        self.typed.setToolTip(tr("Type a new token: it replaces the value of the secret named below (the one kept is "
                                 "never shown)"))
        self.stored.setText(tr("Keep its secret (or choose another)"))
        self.stored.setChecked(True)
        self.secret_choice.setText(secret)
        self.secret.setText(secret)
        self.name_typed = True  # (the name a new token is kept as: this login's secret, not one offered anew)

    def offer_name(self):
        """The secret name offered for a typed token, until one is typed by hand."""
        if self.name_typed:
            return
        self.secret.setText(self.offered_name())

    def offered_name(self):
        url = self.edits["url"].text().strip() if "url" in self.edits else ""
        kind = self.kind.currentText() if self.kind is not None else None
        return SandboxAI.token_secret_name(kind=kind, url=None if kind else url)

    def show_choice(self):
        typed = self.typed.isChecked()
        if not typed and not self.secret_choice.text() and self.secret_choice.offered() and self.editing is None:
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
        # one line (cut with "…", whole in its tooltip): a longer summary never moves the tabs below it
        self.summary = widgets.ElidedLabel(about=120, role="secondary")
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        widgets.arrow_tabs(self.tabs)  # (the app's tab row: arrows at its end when the tabs do not fit)
        self.tabs.currentChanged.connect(lambda _index: self.even_out())
        layout.addWidget(self.tabs, 1)

        # the overview
        overview = CredentialTab(tr("Everything configured"))
        overview.add_about(tr("Everything configured, with its state. Click a row to open its tab."))
        self.overview = overview.set_list(table([tr("Kind"), tr("Name"), tr("Where / used by"), tr("State")],
                                                stretch=2, compact=(0, 3)))
        self.overview.setAccessibleName(tr("Overview"))
        self.overview.cellClicked.connect(lambda row, _column: self.open_row(row))
        self.overview.cellActivated.connect(lambda row, _column: self.open_row(row))
        refresh_all = QPushButton(tr("Refresh"))
        refresh_all.setToolTip(tr("Read everything again (on the host chosen)"))
        refresh_all.clicked.connect(lambda _=False: self.refresh_all())
        overview.add_buttons(refresh_all)
        self.overview_tab = overview
        self.add_tab(None, overview, tr("Overview"))

        # agent logins
        logins = CredentialTab(tr("Agent logins for sandboxes"))
        logins.add_about(tr("The agents' own logins (sandbox-ai login AGENT): kept once, used by every sandbox of "
                            "that agent; without one a sandbox uses the host's login."))
        self.logins = logins.set_list(table([tr("Agent"), tr("Login from"), tr("State"), tr("File")], stretch=3,
                                            compact=(0, 1, 2)))
        login = QPushButton(tr("Log in…"))
        login.setToolTip(tr("Runs the agent's own login inside a sandbox and keeps it for all sandboxes"))
        login.clicked.connect(lambda: self.login(False))
        logout = QPushButton(tr("Log out…"))
        logout.clicked.connect(lambda: self.login(True))
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(self.refresh)
        # The app's browser at a provider's sign-in page (web_browser.py): the login browser's profile, so a
        # subscription signed in to there makes the agent's Log in… one click.
        self.cloud_button = QPushButton(tr("Sign in to a cloud subscription…"))
        self.cloud_button.setToolTip(safe_tooltip(tr(
            "Opens the app's browser at a provider's sign-in page (claude.ai, ChatGPT, OpenCode ...): the sign-in "
            "stays in the login browser's profile, so the next Log in… finds it")))
        self.cloud_menu = QMenu(self.cloud_button)
        self.cloud_menu.aboutToShow.connect(self.fill_cloud_menu)
        self.cloud_button.setMenu(self.cloud_menu)
        self.give_login_button = QPushButton(tr("Give to a running sandbox…"))
        self.give_login_button.setToolTip(safe_tooltip(tr(
            "The stored login of the selected agent for a sandbox that runs already (sandbox-ai login give RUN): put "
            "into its HOME as a start does; Claude Code reads it again by itself, the other agents are started "
            "again inside the sandbox (asked)")))
        self.give_login_button.clicked.connect(lambda _=False: self.give_agent_login())
        # (Refresh is not in this row - with Give… it would not fit a small window in German: the Refresh of the
        # sessions' logins below reads this list too, and the list's right click has it.)
        logins.add_buttons(login, logout, self.give_login_button, self.cloud_button)
        refresh.setParent(logins)
        refresh.hide()
        logins.menu_only = [menu_entry(refresh)]
        self.logins_message = logins.message
        # the second list: which login each running or kept session uses (sandbox-ai login --status --sessions)
        from .session_logins_view import SessionLoginsBox
        self.session_logins = SessionLoginsBox(sai, changed=self.refresh, use=self.use_session_login,
                                               give=self.give_session_login)
        self.session_logins.refresh_button.clicked.connect(lambda _=False: self.refresh())  # (this tab's one Refresh)
        self.session_logins.refresh_button.setToolTip(tr("Read the agents' and the sessions' logins again (on the host "
                                                         "chosen)"))
        logins.body.addWidget(self.session_logins, 1)
        self.add_tab("logins", logins)

        # secrets
        secrets = CredentialTab(tr("Secrets (API keys and tokens for --secret-env and MCP servers)"))
        # {host: [(name, where)]} of the secret names read there; a host missing: not known (not read yet, or the
        # read failed), and a replace there is asked about (an unknown secret may exist).
        self.remote_secrets, self.secrets_token = {}, 0
        self.backend_label = secrets.add_header(plain_label(role="secondary"))
        # Where a secret kept now goes on the chosen host (`sandbox-ai secrets backend` there: keyring or file).
        self.store_label, self.store_token = secrets.add_header(plain_label(role="secondary")), 0
        self.secrets = secrets.set_list(multi_select(table([tr("Name"), tr("Kept in"), tr("Used by")], stretch=2,
                                                           compact=(1,))))
        self.secrets.itemSelectionChanged.connect(lambda: self.name.setText(selected(self.secrets) or self.name.text()))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.name = QLineEdit()
        self.name.setPlaceholderText("API_TOKEN")
        self.value = QLineEdit()
        self.value.setPlaceholderText(tr("the value (passed on standard input; a kept one is never read back)"))
        self.value.setAccessibleName(tr("Value"))  # its form label names the field's row, not the field itself
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Value"), widgets.secret_value(self.value))
        secrets.add_below(form)
        keep = self.add_button = QPushButton(tr("Add"))
        keep.setToolTip(tr("Keep the value under that name for good (sandbox-ai secrets set, on standard input); a "
                           "name kept already is replaced only after a yes"))
        keep.clicked.connect(self.set_secret)
        delete = self.remove_button = QPushButton(tr("Remove…"))
        delete.setToolTip(tr("Remove the selected secret (asked first): every start, VPN profile and Git login that "
                             "names it goes without it"))
        delete.clicked.connect(self.delete_secret)
        give = self.give_key_button = QPushButton(tr("Give to a running sandbox…"))
        give.setToolTip(safe_tooltip(tr(
            "The selected secret for a sandbox that runs: as an API key (sandbox-ai secrets give RUN NAME --api-key "
            "HOST; its proxy puts the key in on the wire, the agent only sees a placeholder) or, for a Git server, as "
            "a Git login (sandbox-ai git-logins give RUN URL --secret NAME)")))
        give.clicked.connect(lambda _=False: self.give_secret())
        edit = self.edit_button = QPushButton(tr("Edit…"))
        edit.setToolTip(safe_tooltip(tr(
            "The selected secret: another name (sandbox-ai secrets rename, carried through every login, VPN profile, "
            "project file and kept start that names it) or Replace value… (a new value typed; the one kept is "
            "never shown)")))
        edit.clicked.connect(lambda _=False: self.edit_secret())
        # (Not in the row, which must fit a small window: on the list's right click, and from the Give dialog.)
        far = self.give_far_button = QPushButton(tr("Give to a sandbox on another host…"), secrets)
        far.hide()
        far.clicked.connect(lambda _=False: self.give_secret_far())
        show_far(far, LOCAL)
        secrets.menu_only = [menu_entry(far)]
        # a double click on a secret: Edit…
        self.secrets.cellDoubleClicked.connect(lambda _row, _column: self.edit_secret())
        # The primary password of the private files on the chosen host (sandbox-ai secrets password set, unlock,
        # lock): one button, named by what it does now; hidden where the system's keyring keeps the secrets, and
        # for a sandbox-ai that knows none. {host: secrets_protection's answer} as last read.
        self.protections = {}
        protect = self.protect_button = QPushButton(tr("Unlock…"))
        protect.clicked.connect(lambda _=False: self.protect())
        # (always in its place, as wide as its longest name: a state read later moves and resizes nothing)
        protect.setMinimumWidth(max(protect.fontMetrics().horizontalAdvance(tr(text)) for text in PROTECT_NAMES.values())
                                + 28)
        self.show_protection(LOCAL)
        # several at once (Ctrl, Shift): Remove… and the Delete key take them all; Give… one
        delete_key(self.secrets, self.delete_secret)
        one_only(self.secrets, [give, edit])
        self.secret_buttons = [keep, delete, edit]
        self.generation = 0
        secrets.add_buttons(keep, edit, delete, give, protect)
        self.message = secrets.message
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
        # what the sandboxes that run on the chosen host were given during their run, each taken back with Revoke…
        self.given = GivenBox(sai)
        self.given.counted = self.show_given_count
        self.add_tab("given", self.given)

    def fill_cloud_menu(self):
        """The providers' sign-in pages (sandbox-ai's PROVIDERS), a section per agent, and the browser itself."""
        from . import web_browser
        try:
            launcher = self.sai.launcher  # (sandbox-ai's own PROVIDERS)
        except Exception as _exc:  # noqa: BLE001 - not loadable: terminal_text's copy of the lists
            applog.swallowed("credentials_page", "CredentialsPage.fill_cloud_menu: not loadable: terminal_text'…", _exc)
            launcher = None
        web_browser.quick_menu(self.cloud_menu, self.open_browser, launcher)
        self.cloud_menu.addSeparator()
        self.cloud_menu.addAction(tr("Open the browser"), lambda: self.open_browser(None))

    def open_browser(self, url):
        from . import web_browser
        return web_browser.open_browser(url, parent=self)

    def give_secret(self):
        """The chosen secret as an API key (or a Git login) of a sandbox that runs on the host chosen (this machine or
        a host of sandbox-ai hosts, where the secret is kept): its starts are read in the background, then one is
        chosen with the server and variable, asked about, and `secrets give` (`git-logins give`) sent there."""
        name, host = selected(self.secrets), self.host()
        if not name:
            self.message.setText(tr("Choose a secret in the list first."))
            return
        background(give_targets(self.sai, host), owner=self, source=self.give_key_button, timeout=90,
                   done=lambda found, error: self.give_secret_to(name, found, error, host))

    def give_secret_to(self, secret, found, error, where=LOCAL):
        runs = running_on(where, found, error)
        if not runs:
            self.message.setText(nobody_runs(where) if error is None else printable_text(error))
            return
        dialog = GiveDialog(self, runs, secret=secret, git_hosts=self.git_hosts(), host=where, far=where == LOCAL)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            if dialog.wants_far:  # A sandbox on another host…: this machine's secret sent there first
                self.give_secret_far()
            return
        run, keep, called = dialog.run(), dialog.keep.isChecked(), dialog.run_label()
        host, variable = dialog.key_host(), dialog.variable.text().strip()
        if dialog.as_git_login():
            user = dialog.user.text().strip() or "x-access-token"
            entry = {"url": f"https://{host}", "user": user, "secret": secret}
            question = tr("Give the secret {secret} to {run} as a Git login for https://{host} (user {user}), until "
                          "it ends? Its connection proxy logs in with it on the wire from now on (git push); the "
                          "token does not enter the sandbox.", secret=printable(secret),
                          run=called, host=printable(host), user=printable(user))
            if keep:
                question += "\n\n" + tr("It is also kept for the later starts of its project: written into its "
                                         "trusted .sandbox-ai.json.")
            if not ask_first(self, "net-allow", tr("Give the login"), where_given(where, question), tr("Give")):
                return
            self.message.setText(tr("Giving the login…"))
            give_login(self, self.sai, where, run, entry, keep, called, self.message, self.give_key_button)
            return
        question = tr("Give the secret {secret} to {run} as the API key {name} for {host}, until it ends? Its proxy "
                      "puts the key in on the wire for HTTPS requests to {host}; the agent sends the placeholder "
                      "and never sees the key. {host} is allowed too.", secret=printable(secret), run=called,
                      name=printable(variable), host=printable(host))
        if keep:
            question += "\n\n" + tr("It is also kept for the later starts of its project: written into its trusted "
                                     ".sandbox-ai.json.")
        if not ask_first(self, "net-allow", tr("Give the key"), where_given(where, question), tr("Give")):
            return
        self.message.setText(tr("Giving the key…"))
        background(self.sai.host_run, where, "secrets", "give", run, secret, "--api-key", host, "--as", variable,
                   *(["--keep"] if keep else []), owner=self, work=True, source=self.give_key_button, timeout=150,
                   done=lambda result, error_: self.secret_given(called, run, host, variable, result, error_, where))

    def give_secret_far(self):
        """This machine's secret for a sandbox on another host: sent there first (asked first), then given there as
        any secret of that host (the same dialog and question as Give to a running sandbox…)."""
        name = selected(self.secrets)
        if not name or self.host() != LOCAL:
            self.message.setText(tr("Choose a secret in the list first."))
            return
        send_then(self, self.sai, name, self.message, self.give_far_button,
                  lambda host: background(give_targets(self.sai, host), owner=self, source=self.give_far_button,
                                          timeout=90, done=lambda found, error: self.give_secret_to(name, found,
                                                                                                    error, host)))

    def edit_secret(self):
        """Edit…: the selected secret's name (renamed everywhere sandbox-ai names it) and, with Replace value…, a
        new value typed here - the one kept is never read, shown or filled in."""
        name, host = selected(self.secrets), self.secret_host_of()
        if not name or not self.sai.valid_secret(name):
            self.message.setText(tr("Choose a secret in the list first."))
            return None
        uses = self.secret_uses().get(name, []) if host == self.host() else []
        dialog = SecretDialog(self, name, uses, host)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return None
        new, value = dialog.name.text().strip(), dialog.value.text() if dialog.replace.isChecked() else ""
        dialog.value.clear()
        if new == name and not value:
            self.message.setText(tr("Nothing changed."))
            return None
        if value and not ask_first(self, "secret-replace", tr("Secret"), on_host(host, tr(
                "A secret {name} is kept already: replace its value? Everything that names it gets the new one.",
                name=printable(name))), tr("Replace"), detail={"exists": True}):
            self.message.setText(tr("Not kept: the secret {name} stays as it is.", name=printable(name)))
            return None
        if new != name and not ask_first(self, "secret-rename", tr("Secret"), on_host(host, tr(
                "Rename the secret {old} to {new}?\n\nIts name is changed in everything sandbox-ai keeps that names "
                "it: Git and publish logins, identities, VPN profiles, the options of trusted project files and of "
                "kept starts. What still names {old} afterwards is said (an LLM server, an MCP server, a project "
                "file changed since you trusted it). A sandbox that runs has the value already: nothing changes for "
                "it.", old=printable(name), new=printable(new))), tr("Rename")):
            self.message.setText(tr("Not renamed: the secret {name} keeps its name.", name=printable(name)))
            return None
        self.message.setText(tr("Working…"))

        def work(name=name, new=new, value=value, host=host):
            said, result = [], (0, "", "")
            if value:  # the value first, under the name it has (on standard input, never an argument)
                result = self.sai.set_secret(name, value, host) if host != LOCAL else self.sai.set_secret(name, value)
                said.append(((result[1] or "") + (result[2] or "")).strip())
            if result[0] == 0 and new != name:
                result = self.sai.host_run(host, "secrets", "rename", name, new, timeout=120)
                said.append(((result[1] or "") + (result[2] or "")).strip())
            return result[0], "\n".join(part for part in said if part), "", result
        value = None  # noqa: F841 - held by the call alone
        background(work, owner=self, work=True, source=self.secret_buttons, timeout=240,
                   done=lambda result, error: self.secret_edited(host, bool(dialog.replace.isChecked()), result,
                                                                 error))
        return new

    def secret_edited(self, host, replaced, result, error):
        if error is not None or not isinstance(result, tuple):
            self.message.setText(tr("Not done: {error}", error=printable_text(error)))
        else:
            status, said, _, last = result
            text = self.sai.too_old_text(host, last, "secrets rename") if status != 0 else None
            self.message.setText(text or (printable_text(said) if status == 0 else
                                          tr("Not done: {error}", error=printable_text(said))) + (
                "\n" + tr("The new value is for the starts from now on: a sandbox that runs keeps the one it was "
                          "given (Give to a running sandbox… gives it the new one).")
                if status == 0 and replaced else ""))
        self.secrets_changed()

    def give_agent_login(self):
        """Give to a running sandbox… of the agent logins: the stored (central) login of the selected agent for a
        sandbox of that agent that runs on the host chosen."""
        agent, host = selected(self.logins), self.host()
        if agent not in self.sai.agents:
            self.logins_message.setText(tr("Select an agent first."))
            return
        give_agent_login(self, self.sai, host, agent, None, self.logins_message, self.give_login_button)

    def give_session_login(self, host, agent, name, message, source):
        """Give to a running sandbox… of the logins of the sessions: session NAME's login for another sandbox of
        AGENT that runs on HOST."""
        give_agent_login(self, self.sai, host, agent, name, message, source)

    def secret_given(self, called, run, host, variable, result, error, where=LOCAL):
        status, out, err = given_outcome(result, error)
        if status != 0:
            self.message.setText(self.sai.too_old_text(where, result, GIVE_KEY) or
                                 where_given(where, not_given_text(called, out, err)))
            return
        self.message.setText(where_given(where, with_notes(tr(
            "{run} has the key {name} now, for HTTPS requests to {host} only: in the sandbox, export "
            "{name}={placeholder} and send that. To check: sandbox-ai net show {id}", run=called,
            name=printable(variable), host=printable(host), placeholder=f"sandbox-ai-api-key-placeholder-{variable}",
            id=printable(str(run))), out)))

    def git_hosts(self):
        """The hosts a secret given to is most likely meant as a Git login: the well-known forges and the servers
        of the Git logins (and identities) configured."""
        hosts = set(GIT_FORGES)
        entries = list(self.state.get("git", []))
        for identity in (getattr(self.identities, "known", {}) or {}).values() if self.identities is not None else []:
            entries += [e for e in identity.get("git", []) if isinstance(e, dict)]
        for entry in entries:
            host = git_host(entry.get("url"))
            if host:
                hosts.add(host)
        return hosts

    TAB_TITLES = {"logins": N_("Agent logins"), "secrets": N_("Secrets"), "git": N_("Git logins"),
                  "publish": N_("Publish logins"), "identities": N_("Identities"), "vpn": N_("VPN"),
                  "given": N_("Given to sandboxes")}

    def show_given_count(self, count):
        index = self.tab_of("given")
        if index >= 0:
            self.tabs.setTabText(index, f"{tr(self.TAB_TITLES['given'])} ({count})")
            self.tabs.setTabIcon(index, theme.dot("ok") if count else theme.dot("muted"))

    def show_given(self, host, run=None):
        """The Given to sandboxes tab for HOST, the rows of RUN selected (Sessions › Secrets and logins…)."""
        self.hosts.set_host(host)
        index = self.tab_of("given")
        if index >= 0:
            self.tabs.setCurrentIndex(index)
        self.given.wanted = run
        self.given.set_host(self.host())
        self.given.refresh()

    def add_tab(self, key, widget, title=None):
        widget.setProperty("credential_kind", key)
        widget.resized = self.even_out
        self.tabs.addTab(widget, title or tr(self.TAB_TITLES[key]))

    def even_out(self):
        """Every tab's header as high as the highest at this width (credential_tab.even_out): switching tabs moves
        no list, no button and no text. (Told a moment after a tab's header changed: the window may be gone by then.)"""
        if not alive(self.tabs):
            return
        current = self.tabs.currentWidget()
        even_out([current] + [self.tabs.widget(i) for i in range(self.tabs.count())
                              if self.tabs.widget(i) is not current])

    def refresh_all(self):
        """The overview's Refresh: every tab read again."""
        self.refresh()
        self.session_logins.refresh()
        self.vpn.refresh()
        self.given.refresh()
        if self.identities is not None:
            self.identities.refresh()

    def tab_of(self, key):
        for index in range(self.tabs.count()):
            if self.tabs.widget(index).property("credential_kind") == key:
                return index
        return -1

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.even_out()
        if not self.loaded:
            self.loaded = True
            self.refresh_all()

    def host(self):
        return self.hosts.host() or LOCAL

    def host_changed(self, host):
        """Every tab shows HOST's from now on: nothing of the host before stays (its late answers are dropped), and
        HOST's are read at once, each once - the secrets' names with the rest (refresh); a page not shown before reads
        nothing more when it is (the window gives a hidden page another host as it is about to be shown)."""
        self.git.set_host(host)
        self.publish.host = host
        show_far(self.give_far_button, host)
        for box in (self.git, self.publish):
            box.show_entries([], [], known=False)
        self.state.update(logins=[], secrets=[], store="?", git=[], publish=[])
        self.remote_secrets.pop(host, None)  # read again before a replace there counts as known
        fill(self.logins, [])
        fill(self.secrets, [])
        self.backend_label.setText("")
        self.generation += 1  # what is on its way for the host before
        self.secrets_token += 1
        self.store_token += 1
        self.vpn.set_host(host)
        self.given.set_host(host)
        self.session_logins.set_host(host)
        if self.identities is not None:
            self.identities.set_host(host)
        self.show_overview()
        self.loaded = True  # (what its first show would read: read now)
        self.refresh()

    def read_store(self):
        """What keeps and protects the secrets on the chosen host (`sandbox-ai secrets status` there; a sandbox-ai
        before 0.1.98: `secrets backend`), in the background (a late answer for another host is dropped)."""
        self.store_token += 1
        token, host = self.store_token, self.host()
        backend_of = getattr(self.sai, "secrets_backend", None)
        protection_of = getattr(self.sai, "secrets_protection", None)
        self.store_label.setVisible(backend_of is not None or protection_of is not None)
        if backend_of is None and protection_of is None:
            return

        def read():
            info = protection_of(host) if protection_of is not None else None
            return info if info is not None or backend_of is None else backend_of(host)
        self.store_label.setText(tr("Secrets backend: asking…"))
        background(read, owner=self, timeout=90,
                   done=lambda result, error: self.store_read(token, host, result, error))

    def store_read(self, token, host, result, error):
        if token != self.store_token or host != self.host():
            return
        stores = {"keyring": tr("your system keyring (Secret Service)"), "keychain": tr("the macOS Keychain"),
                  "file": tr("private files (mode 0600)")}
        self.protections.pop(host, None)
        if error is not None or not isinstance(result, (str, dict)):
            text = tr("Secrets backend: not known ({error})", error=printable_text(error)[:200])
        elif isinstance(result, dict):
            self.protections[host] = result
            store = result.get("backend")
            text = tr("Secrets backend (sandbox-ai secrets backend): {store}",
                      store=stores.get(store, printable(str(store))[:40]))
            text += " — " + self.protection_text(result)
        else:
            text = tr("Secrets backend (sandbox-ai secrets backend): {store}",
                      store=stores.get(result, printable(result)[:40]))
        self.store_label.setText(text if host == LOCAL else tr("On {host}: {text}", host=printable(host), text=text))
        self.show_protection(host)

    @staticmethod
    def protection_text(info):
        """What protects the secrets (secrets_protection's answer), in words."""
        if info.get("backend") != "file" and not info.get("password") and not info.get("plain"):
            return tr("encrypted by the system")
        if not info.get("password"):
            return tr("not encrypted ({count} in plain files): set a primary password", count=info.get("plain", 0)) \
                if info.get("plain") else tr("the first secret needs a primary password, which encrypts them")
        text = tr("encrypted with the primary password, locked") if info.get("locked") else \
            tr("encrypted with the primary password, unlocked")
        if info.get("plain"):
            text += "; " + tr("{count} still in plain files (encrypted at the next unlock)", count=info["plain"])
        return text

    def protection_step(self, host):
        """What the primary password's button does on HOST now: "new" (set one), "unlock", "lock" or None (the
        system's keyring keeps the secrets, or what protects them is not known)."""
        info = self.protections.get(host)
        if not isinstance(info, dict):
            return None
        if info.get("password"):
            return "unlock" if info.get("locked") else "lock"
        return "new" if info.get("backend") == "file" or info.get("plain") else None

    def show_protection(self, host):
        step = self.protection_step(host)
        texts = {"new": (tr(PROTECT_NAMES["new"]),
                         tr("Encrypt the secrets kept in private files on this host with a primary password "
                            "(sandbox-ai secrets password set): asked once per session, never stored")),
                 "unlock": (tr(PROTECT_NAMES["unlock"]), tr("Type the primary password: the secrets stay unlocked for 8 hours, or "
                                             "until Lock (sandbox-ai secrets unlock)")),
                 "lock": (tr(PROTECT_NAMES["lock"]), tr("Forget the key now: the next secret needed asks for the primary password "
                                         "again (sandbox-ai secrets lock)"))}
        if step is None:
            known = isinstance(self.protections.get(host), dict)
            widgets.set_enabled(self.protect_button, False, why=tr(
                "The system's keyring encrypts these secrets: they need no primary password") if known else tr(
                "What protects the secrets on this host is not known (yet)"))
            return
        self.protect_button.setText(texts[step][0])
        self.protect_button._tooltip = texts[step][1]  # (set_enabled keeps the first one it saw otherwise)
        widgets.set_enabled(self.protect_button, True)

    def new_primary_password(self, host):
        """A new primary password typed twice (hidden), or None: cancelled, too short or not the same twice."""
        where = "" if host == LOCAL else " " + tr("(on {host})", host=printable(host))
        first = widgets.ask_password(
            self, tr("Primary password"),
            tr("The secrets{where} are kept in private files. A primary password encrypts them: without it nobody "
               "reads them from the disk or a backup - you neither, there is no way to recover it. It is asked once "
               "per session.\n\nNew primary password (8 characters or more):", where=where), tr("Next"))
        if not first:
            return None
        if len(first) < 8:
            widgets.warn(self, tr("Primary password"), tr("A primary password has 8 characters or more."))
            return None
        again = widgets.ask_password(self, tr("Primary password"), tr("The same primary password again:"),
                                     tr("Set primary password"))
        if again != first:
            if again:
                widgets.warn(self, tr("Primary password"), tr("The two passwords are not the same: nothing was set."))
            return None
        return first

    def protect(self, then=None):
        """The primary password's step on the chosen host (protection_step): set one (typed twice), unlock (typed
        once) or lock - each `sandbox-ai secrets ...` there, the password on its standard input. THEN: what to do
        once the secrets are unlocked (the secret that waited for it)."""
        host = self.host()
        step = self.protection_step(host)
        where = "" if host == LOCAL else " " + tr("(on {host})", host=printable(host))
        if step == "lock":
            call = functools.partial(self.sai.secrets_lock, host)
        elif step == "unlock":
            password = widgets.ask_password(
                self, tr("Primary password"),
                tr("The secrets{where} are encrypted. Type their primary password: they stay unlocked for 8 hours, "
                   "or until Lock.", where=where), tr("Unlock"))
            if not password:
                return
            call = functools.partial(self.sai.secrets_unlock, password, host)
        elif step == "new":
            password = self.new_primary_password(host)
            if not password:
                return
            call = functools.partial(self.sai.secrets_new_password, password, host)
        else:
            return
        password = None  # noqa: F841 - held by the call alone, which lets go of it once it has reported
        background(call, owner=self, work=True, source=[self.protect_button, *self.secret_buttons], timeout=300,
                   what=f"sandbox-ai secrets: the primary password ({step}) on {host}",
                   done=lambda result, error: self.protected(host, step, result, error, then))

    def protected(self, host, step, result, error, then):
        status, out, err = outcome(result, error)
        if status == 0:
            self.message.setText(printable_text((out + err).strip()))
            info = dict(self.protections.get(host) or {})
            if info:  # (as it is now, until the next read says so itself)
                info.update(password=True, locked=step == "lock", **({"plain": 0} if step != "lock" else {}))
                self.protections[host] = info
                self.show_protection(host)
        else:
            self.message.setText(tr("Not done: {error}", error=printable_text(err.strip() or out.strip())))
        self.read_store()
        if status == 0 and then is not None and step != "lock":
            then()

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
                    applog.swallowed("credentials_page", "refresh.read_remote: shown on the page; the other tabs…", exc)
                    found.append([])
                    problems.append(printable_text(exc)[:300])
            try:
                listed = self.sai.host_secrets(host)
            except Exception as exc:  # noqa: BLE001
                applog.swallowed("credentials_page", "refresh.read_remote failed (went on without it)", exc)
                listed = None  # not known: a replace there is asked about
                problems.append(printable_text(exc)[:300])
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
            except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: those tabs stay empty
                applog.swallowed("credentials_page", "refresh.read: an older sandbox-ai: those tabs stay empty", _exc)
            try:
                hosts = list(self.sai.remote_hosts())
            except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai, or no list of hosts: this machine's only
                applog.swallowed("credentials_page", "refresh.read: an older sandbox-ai, or no list of hosts: t…", _exc)
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
            self.message.setText(printable_text(error))
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
            self.message.setText(printable_text(problem))
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
        stores = {"keyring": tr("your system keyring (Secret Service)"), "keychain": tr("the macOS Keychain"),
                  "file": tr("private files (mode 0600)")}
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
            self.backend_label.setText(tr("{host}: no answer", host=printable(host)) + f" ({printable_text(error)[:300]})")
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

    def use_session_login(self, host, agent, name):
        """Use for a new start… of the logins of the sessions: the Board with HOST, AGENT and "Login from: a session"
        NAME (--auth-from-session), shown."""
        window = self.window()
        board = getattr(window, "board", None)
        if board is None or not hasattr(board, "use_session_login"):
            inform(self, tr("Use for a new start"), tr("The Board is not there in this window."))
            return False
        if not board.use_session_login(host, agent, name):
            return False
        if hasattr(window, "show_page"):
            window.show_page("board")
        return True

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
        # (Handed over as its own arguments, not referred to by a closure: the line below would empty it first.)
        def keep(name=name, value=value, host=host):
            if host == "local":
                background(self.sai.set_secret, name, value, owner=self, work=True, source=self.secret_buttons,
                           timeout=90, done=self.secret_kept)
            else:
                background(functools.partial(self.sai.set_secret, name, value, host=host), owner=self,
                           source=self.secret_buttons, work=True, timeout=120, done=self.secret_kept)
        value = None  # noqa: F841 - not kept here (the call lets go of it once it has reported)
        step = self.protection_step(host)
        if step == "unlock" or (step == "new" and not (self.protections.get(host) or {}).get("plain_choice")):
            # private files there, without a primary password yet or locked: that first (typed here), then the
            # secret (plain files that are the host's own choice, SANDBOX_AI_SECRETS_PLAIN=1, are kept as they are)
            self.protect(then=keep)
            return
        keep()

    def secret_kept(self, result, error):
        status, out, err = outcome(result, error)
        self.message.setText(printable_text((out + err).strip()) if status == 0 else
                             tr("Not kept: {error}", error=printable_text(err.strip() or out.strip())))
        self.secrets_changed()

    def secrets_changed(self):
        self.refresh()

    def delete_secret(self):
        names = [name for name in selected_keys(self.secrets) if name]
        if len(names) > 1:
            self.delete_secrets(names)
            return
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

    def delete_secrets(self, names):
        """Remove… with several secrets selected: one question naming them, then each (secrets delete NAME)."""
        host = self.secret_host_of()
        names = [name for name in names if self.sai.valid_secret(name)]
        text = several(tr("Remove these {count} secrets? Every start, VPN profile and Git login that names one goes "
                          "without it.", count=len(names)), [printable(name) for name in names])
        if host != "local":
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not names or not ask_first(self, "secret-delete", tr("Remove secret"), text, tr("Remove")):
            return

        def done(outcomes):
            failures = report_each(self, tr("Remove secret"), outcomes, printable)
            self.message.setText(tr("Removed {count} of {total}.", count=len(names) - len(failures), total=len(names)))
            self.secrets_changed()
        run_each(lambda name: self.sai.delete_secret(name) if host == "local" else
                 self.sai.delete_secret(name, host=host), names, owner=self, source=self.secret_buttons,
                 timeout=120 * len(names), done=done)

    def secret_deleted(self, result, error):
        status, out, err = outcome(result, error)
        self.message.setText(printable_text((out + err).strip()))
        self.secrets_changed()


__all__ = ["CredentialsPage"]

def git_login_url(entry):
    """http(s)://[USER@]HOST[:PORT] of a Git login, as `net allow --git-token-*` takes it."""
    url, user = str(entry.get("url", "")).rstrip("/"), str(entry.get("user") or "")
    if user and "://" in url and "@" not in url.split("://", 1)[1]:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://{user}@{rest}"
    return url


def token_source(entry):
    """--secret NAME, --env VARIABLE or --file PATH: where the token of the login ENTRY is."""
    for key in ("secret", "env", "file"):
        if entry.get(key):
            return [f"--{key}", str(entry[key])]
    raise ValueError("a Git login names where its token is: a secret, a variable or a file")


def token_place(entry):
    """Where the token of ENTRY is, for people: "secret NAME", "variable NAME", "file PATH"."""
    for key, word in (("secret", N_("secret {name}")), ("env", N_("variable {name}")), ("file", N_("file {name}"))):
        if entry.get(key):
            return tr(word, name=printable(str(entry[key])))
    return ""


def give_login_args(run, entry, keep, in_sandbox=False):
    """What follows `sandbox-ai git-logins give` for the login ENTRY ({url, user, and secret, env or file}): --secret
    NAME (--env VARIABLE, --file PATH) [--user USER] [--keep] [--in-sandbox] -- RUN URL (the URL after "--" is never
    read as an option)."""
    user = str(entry.get("user") or "")
    return [*token_source(entry), *(["--user", user] if user else []),
            *(["--keep"] if keep else []), *(["--in-sandbox"] if in_sandbox else []), "--", run,
            str(entry.get("url", "")).rstrip("/")]


def given_outcome(result, error):
    """(status, out, err) of a give's `sandbox-ai ...` (an exception, a timeout: a failure saying so)."""
    return result if error is None and isinstance(result, tuple) else (1, "", str(error or ""))


def show_far(button, host):
    """The button that gives THIS machine's credential to a sandbox on another host: usable while the page shows this
    machine's (another host's are given there with Give to a running sandbox…)."""
    button.setToolTip(safe_tooltip(tr(
        "For a sandbox on another host of sandbox-ai hosts: the secret is sent there first (asked first; its value "
        "goes over SSH on standard input into that host's secret store, never in a command line or a file - sandbox-ai "
        "secrets send NAME HOST), then given there")))
    widgets.set_enabled(button, host in (None, LOCAL), why=tr(
        "This is for this machine's credentials (choose This machine above). What is kept on {host} is given to a "
        "sandbox there with Give to a running sandbox….", host=printable(host)))


def send_then(parent, sai, secret, message, source, then):
    """This machine's SECRET for another host: which host (chosen from sandbox-ai hosts), asked first (secret-send),
    sent there (`sandbox-ai secrets send SECRET HOST --yes`: the value goes from this machine's store over SSH on
    standard input into `secrets set` there - never through the app), then THEN(host). A secret of that name kept
    there already is replaced only after a second yes (secret-replace)."""
    def hosts_read(hosts, error):
        hosts = [h for h in (hosts or []) if isinstance(h, str) and h != LOCAL] if error is None else []
        if not hosts:
            message.setText(printable_text(error) if error is not None else tr(
                "No other host is known: add one on the Hosts page (sandbox-ai hosts add NAME USER@HOST)."))
            return
        dialog = HostDialog(parent, hosts, secret)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        host = dialog.host()
        if not ask_first(parent, "secret-send", tr("Send the secret"), tr(
                "Send the secret {secret} of this machine to {host}?\n\nIts value goes over the SSH connection to "
                "{host}, on standard input, into that host's secret store (sandbox-ai secrets) and is kept there from "
                "then on: whoever controls {host} can use it. It is never in a command line, a log or a file on the "
                "way. To take it away there later: the Secrets tab with {host} chosen, Remove….",
                secret=printable(secret), host=printable(host)), tr("Send")):
            return
        send(host, False)

    def send(host, replace):
        message.setText(tr("Sending the secret to {host}…", host=printable(host)))
        background(sai.call, "secrets", "send", secret, host, "--yes", *(["--replace"] if replace else []),
                   owner=parent, work=True, source=source, timeout=180,
                   done=lambda result, error: sent(host, replace, result, error))

    def sent(host, replaced, result, error):
        status, out, err = given_outcome(result, error)
        if status != 0 and not replaced and "already" in err and "--replace" in err:
            if ask_first(parent, "secret-replace", tr("Send the secret"), on_host(host, tr(
                    "A secret {name} is kept already: replace its value? Everything that names it gets the new one.",
                    name=printable(secret))), tr("Replace"), detail={"exists": True}):
                send(host, True)
            else:
                message.setText(tr("Not sent: the secret {name} on {host} stays as it is.", name=printable(secret),
                                   host=printable(host)))
            return
        if status != 0:
            lines = (err or out).strip().splitlines()
            message.setText(tr("Not sent to {host}: {error}", host=printable(host),
                               error=printable(lines[-1]) if lines else tr("the command said nothing")))
            return
        message.setText(tr("The secret {name} is kept on {host} now.", name=printable(secret), host=printable(host)))
        then(host)
    background(sai.remote_hosts, owner=parent, source=source, timeout=60, done=hosts_read)


def give_login_question(target, secret, called, keep):
    """What is asked before a Git login is given to the running sandbox CALLED: what it does, and where the token
    is (with the run's connection proxy - never in the sandbox)."""
    question = tr("Give the Git login for {url} ({where}) to {run} until it ends, at once?\n\nIts connection "
                  "proxy logs in with it on the wire from now on (clone, fetch, push); the token does not enter the "
                  "sandbox, and where the run already has a login for that server, this one is used.",
                  url=printable(target), where=secret, run=called)
    if keep:
        question += "\n\n" + tr("It is also kept for the later starts of its project: written into its trusted "
                                 ".sandbox-ai.json.")
    return question


def give_login(parent, sai, host, run, entry, keep, called, message, source, in_sandbox=False):
    """`sandbox-ai git-logins give` for ENTRY to RUN on HOST, in the background; its outcome in MESSAGE. A run whose
    connection proxy cannot log in (no proxy, a server it does not inspect, started by an older sandbox-ai) refuses
    it: the only other way puts the token into the sandbox, where the agent can read it - asked about by itself
    (said plainly), and given so only after that yes (--in-sandbox)."""
    args = ("git-logins", "give", *give_login_args(run, entry, keep, in_sandbox=in_sandbox))

    def done(result, error):
        status, out, err = given_outcome(result, error)
        if status != 0 and not in_sandbox and "--in-sandbox" in err:
            why = printable_text((err.strip().splitlines() or [""])[-1])
            if ask_first(parent, "net-allow", tr("Give the login"), where_given(host, tr(
                    "{run} cannot be given the Git login for {url} through its connection proxy.\n\nThe only other "
                    "way puts the token into the sandbox's session directory for its Git: the agent can read it "
                    "there. Give it so?\n\n{why}", run=called, url=printable(git_login_url(entry)), why=why)),
                    tr("Give into the sandbox")):
                message.setText(tr("Giving the login…"))
                give_login(parent, sai, host, run, entry, keep, called, message, source, in_sandbox=True)
            else:
                message.setText(where_given(host, tr("Not given to {run}: its proxy cannot log in there, and the "
                                                      "token was not put into the sandbox.", run=called)))
            return
        message.setText(sai.too_old_text(host, result, GIVE_LOGIN) or
                        where_given(host, login_given_text(called, run, entry, result, error, in_sandbox)))
    background(sai.host_run, host, *args, owner=parent, work=True, source=source, timeout=150, done=done)


def with_notes(text, out):
    """TEXT, then the notes the command said ("note: ..." lines of OUT: a server on the host's loopback, a port that
    needs --allow-tcp), one per line."""
    notes = [printable(line.strip()) for line in (out or "").splitlines() if line.strip().startswith("note:")]
    return "\n".join([text, *notes])


def not_given_text(called, out, err):
    """What a give that failed says: which run did not get it, and the command's reason (its last line)."""
    lines = (err or out or "").strip().splitlines()
    return tr("Not given to {run}: {error}", run=called,
              error=printable(lines[-1]) if lines else tr("the command said nothing"))


def login_given_text(called, run, entry, result, error, in_sandbox=False):
    """What a Git login given to RUN (CALLED: "RUN (NAME)") says: the server and user it got and where its token is
    (IN_SANDBOX: in the sandbox, readable by the agent - else with the run's proxy), or why not."""
    status, out, err = given_outcome(result, error)
    if status != 0:
        return not_given_text(called, out, err)
    url = str(entry.get("url", "")).rstrip("/")
    user = str(entry.get("user") or "") or (url.split("://", 1)[-1].split("@", 1)[0] if "@" in url else
                                             "x-access-token")
    if in_sandbox:
        return with_notes(tr("{run} has the Git login for {url} (user {user}) now, in the sandbox: its git logs in "
                             "with it until the run ends, and the agent can read the token. To check: sandbox-ai net "
                             "show {id}", run=called, url=printable(url), user=printable(user),
                             id=printable(str(run))), out)
    return with_notes(tr("{run} has the Git login for {url} (user {user}) now: its connection proxy logs in with it "
                         "until the run ends; the token is not in the sandbox. To check: sandbox-ai net show {id}",
                         run=called, url=printable(url), user=printable(user), id=printable(str(run))), out)


# The forges whose host a secret is most likely given to for git push (besides the Git logins configured).
GIT_FORGES = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "gitea.com")
# Forges whose API is not on the Git server's own host: a key for github.com is never put into requests to
# api.github.com (the proxy puts a key in for its very host only), so the key goes to the API's host.
FORGE_API_HOSTS = {"github.com": "api.github.com", "bitbucket.org": "api.bitbucket.org"}


def git_host(url):
    """The host of a Git login's URL (http(s)://[USER@]HOST[:PORT]), lower case; None for another form."""
    match = re.fullmatch(r"https?://(?:[^@/]*@)?([A-Za-z0-9.-]+)(?::\d+)?/?", str(url or ""))
    return match[1].lower().rstrip(".") if match else None


def run_label(instance):
    """"RUN (NAME)": the run id and the start's name, as the messages of a credential given name them."""
    run, name = str(getattr(instance, "run", "") or ""), str(getattr(instance, "name", "") or "")
    return printable(f"{run} ({name})" if name and name != run else run)


def running_here(found, error):
    """The runs and sessions that run on this machine now, of `instances` (FOUND, ERROR as background gives them)."""
    instances = (found[0] if isinstance(found, tuple) else found) if error is None else []
    return [i for i in instances or [] if getattr(i, "run", None) and getattr(i, "state", "") == "running"
            and getattr(i, "kind", "") in ("run", "session") and getattr(i, "host", LOCAL) == LOCAL]


def give_targets(sai, host):
    """What reads the starts a credential may be given to, to run in the background: this machine's instances, or the
    starts of HOST (`sandbox-ai list --json` there)."""
    return sai.instances if host in (None, LOCAL) else functools.partial(sai.host_instances, host)


def running_on(host, found, error):
    """The runs and sessions that run on HOST now (FOUND, ERROR of give_targets as background gives them); a start of
    another host by its name (what `secrets give` and `git-logins give` take there: run is its name)."""
    if host in (None, LOCAL):
        return running_here(found, error)
    return [types.SimpleNamespace(run=i.name, name=i.name, agent=getattr(i, "agent", ""), kind=i.kind, host=host,
                                  state=i.state)
            for i in (found or [] if error is None else []) if getattr(i, "state", "") == "running"
            and getattr(i, "kind", "") in ("run", "session") and valid_run(i.name)]


def nobody_runs(host):
    return tr("No sandbox runs on this machine now. For one on another host: right click, Give to a sandbox on "
              "another host….") if host in (None, LOCAL) else \
        tr("No sandbox runs on {host} now.", host=printable(host))


def where_given(host, text):
    """TEXT of a give (its question, its outcome), "On HOST: …" for another host (this machine's as it was)."""
    return text if host in (None, LOCAL) else on_host(host, text)


API_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")
API_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class NoteLabel(QLabel):
    """A wrapped note in a row of a form: at least as high as its text at its width. A QFormLayout gave the row's
    widget the height of its size hint - the note's lines at another width - and the note was cut short (the Git
    server's note of GiveDialog to one line)."""

    def __init__(self, text, role=None):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)  # (as plain_label: never HTML)
        self.setWordWrap(True)
        self.setText(str(text))
        if role:
            self.setProperty("role", role)

    def fit(self):
        need = self.heightForWidth(self.width())
        if need > 0 and need != self.minimumHeight():
            self.setMinimumHeight(need)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.fit()


class OtherLoginDialog(QDialog):
    """A Git login that is not stored here, for Give to a running sandbox…: its server, and where its token is on
    HOST - a variable, a secret or a file (git-logins give --env/--secret/--file), never the token."""

    def __init__(self, parent, host=LOCAL):
        super().__init__(parent)
        self.setWindowTitle(tr("Give a Git login") if host in (None, LOCAL) else
                            tr("Give a Git login on {host}", host=printable(host)))
        form = QFormLayout(self)
        form.addRow(explanation(tr("A login not in the list: name its server and where its token is - never the token. "
                                   "The sandbox to give it to is chosen next.")))
        self.url = QLineEdit()
        self.url.setPlaceholderText(GIT_URL_EXAMPLE)
        self.user = QLineEdit()
        self.user.setPlaceholderText(GIT_USER_DEFAULT)
        self.kind = QComboBox()
        for kind, label in (("secret", tr("secret")), ("env", tr("variable")), ("file", tr("file"))):
            self.kind.addItem(label, kind)
        self.ref = QLineEdit()
        self.ref.setPlaceholderText(tr("NAME of the variable or secret, or the file"))
        row = QHBoxLayout()
        row.addWidget(self.kind)
        row.addWidget(self.ref, 1)
        form.addRow(tr("Git server"), self.url)
        form.addRow(tr("User"), self.user)
        form.addRow(tr("Token in"), row)
        self.problem = plain_label("", role="notice-error")
        self.problem.hide()
        form.addRow(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.checked_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def entry(self):
        """{url, user, and secret, env or file}, or None while the server or the place of its token is missing."""
        url, ref = self.url.text().strip(), self.ref.text().strip()
        if not url or not ref:
            return None
        return {"url": url, self.kind.currentData(): ref, "user": self.user.text().strip()}

    def checked_accept(self):
        if self.entry() is None:
            self.problem.setText(tr("A Git login needs both: the server's URL and the variable, secret or file of its "
                                    "token."))
            self.problem.show()
            return
        self.accept()


class GiveDialog(QDialog):
    """Which running sandbox gets the login (or, with SECRET, the secret as an API key: for which host, under which
    variable), and whether its project keeps it."""

    def __init__(self, parent, runs, secret=None, git_hosts=(), host=LOCAL, far=False):
        """FAR: the dialog also offers "A sandbox on another host…" (this machine's credential: sent there first);
        chosen, the dialog closes with wants_far set."""
        super().__init__(parent)
        self.wants_far = False
        self.setWindowTitle(tr("Give to a running sandbox") if host in (None, LOCAL) else
                            tr("Give to a running sandbox on {host}", host=printable(host)))
        layout = QVBoxLayout(self)
        self.choice = QComboBox()
        self.choice.setAccessibleName(tr("Sandbox"))
        self.labels = {}
        for instance in runs:
            # Its run id beside the name: the run that gets it, as every message and sandbox-ai net show name it.
            what = ", ".join(str(part) for part in (getattr(instance, "agent", ""), instance.run if instance.run !=
                                                     instance.name else "") if part)
            label = f"{instance.name}  ({what})" if what else instance.name
            self.choice.addItem(printable(label), instance.run)
            self.labels[instance.run] = run_label(instance)
        self.keep = QCheckBox(tr("Also for the later starts of its project"))
        layout.addWidget(plain_label(tr("The sandbox that gets the key:") if secret else
                                     tr("The sandbox that gets the login:")))
        layout.addWidget(self.choice)
        self.host = self.variable = None
        if secret:
            form = QFormLayout()
            self.host = QLineEdit()
            self.host.setPlaceholderText(tr("api.example.com"))
            self.host.setToolTip(tr("The HTTPS server the key is for: the sandbox's proxy puts it in on the wire for "
                                    "requests there only, and the sandbox may reach it from now on"))
            self.variable = QLineEdit(re.sub(r"[^A-Za-z0-9_]", "_", secret)[:64])
            self.variable.setToolTip(tr("The variable whose placeholder the agent sends (export NAME=… in the "
                                        "sandbox: the app says the line)"))
            form.addRow(tr("Server"), self.host)
            # A Git server's host: the secret is offered as a Git login (git push), not silently as an API key.
            self.git_hosts = {h.lower() for h in git_hosts}
            self.as_key = QRadioButton(tr("As an API key (the agent sends a placeholder)"))
            self.as_login = QRadioButton(tr("As a Git login (for git push)"))
            self.as_key.setChecked(True)
            self.kinds = QWidget()
            kinds = QVBoxLayout(self.kinds)
            kinds.setContentsMargins(0, 0, 0, 0)
            self.git_note = NoteLabel(tr("This is a Git server: give the secret as a Git login to push there, or as an "
                                         "API key for its API."), role="secondary")
            kinds.addWidget(self.git_note)
            kinds.addWidget(self.as_login)
            kinds.addWidget(self.as_key)
            form.addRow(self.kinds)
            self.user = QLineEdit("x-access-token")
            self.user.setToolTip(tr("The user Git logs in as (GitHub: x-access-token, GitLab: oauth2 or your user "
                                    "name)"))
            form.addRow(tr("User"), self.user)
            form.addRow(tr("Variable"), self.variable)
            self.form = form
            self.offered = False
            layout.addLayout(form)
            layout.addStretch(1)  # (the room kept for the Git server's choice, while it is not shown: below the form)
            self.host.textChanged.connect(lambda _text: self.show_kind())
            self.as_login.toggled.connect(lambda _on: self.show_kind())
            self.show_kind()
        self.reserved = False
        layout.addWidget(self.keep)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if far:
            # The sandbox meant is not in the list because it runs on another host: the way there, from here.
            self.far_button = buttons.addButton(tr("A sandbox on another host…"),
                                                QDialogButtonBox.ButtonRole.ActionRole)
            self.far_button.setToolTip(safe_tooltip(tr(
                "The sandboxes listed run on this machine. For one on another host of sandbox-ai hosts, the secret is "
                "sent there first (asked first), then given there")))
            self.far_button.clicked.connect(lambda _=False: self.choose_far())
        layout.addWidget(buttons)

    def choose_far(self):
        self.wants_far = True
        self.reject()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)  # (first: the app's [x] in the corner makes room at the top as the dialog is shown)
        if self.host is not None and not self.reserved:
            self.reserved = True
            self.reserve_room()

    def reserve_room(self):
        """The dialog as large as it is with the Git server's choice shown, from the start: when the choice appeared,
        the window had to grow, and where the window manager keeps a dialog's size once it is shown (tiling, some
        Wayland compositors) the note above the choice was cut to one line."""
        self.ensurePolished()  # (its style sheet's padding: sizes before it are smaller - the window still grew)
        rows = (self.kinds, self.user, self.variable)
        shown = [self.form.isRowVisible(widget) for widget in rows]
        for widget in rows:
            widget.ensurePolished()
        for widget in rows:
            self.form.setRowVisible(widget, True)
        self.layout().activate()
        self.resize(self.layout().totalSizeHint())  # (laid out once: the note's width, then its lines at that width)
        self.layout().activate()
        self.git_note.fit()
        self.layout().invalidate()
        self.layout().activate()
        hint = self.layout().totalSizeHint()
        height = self.layout().totalHeightForWidth(hint.width()) if self.layout().hasHeightForWidth() else -1
        for widget, visible in zip(rows, shown):
            self.form.setRowVisible(widget, visible)
        size = QSize(hint.width(), max(hint.height(), height))
        self.setMinimumSize(size)
        self.resize(size.expandedTo(self.size()))

    def run(self):
        return self.choice.currentData()

    def run_label(self):
        """"RUN (NAME)" of the sandbox chosen."""
        return self.labels.get(self.run(), printable(str(self.run())))

    def git_server(self):
        """Whether the server typed is a Git server's host (a forge, a Git login's)."""
        return self.host is not None and self.host.text().strip().lower().rstrip(".") in self.git_hosts

    def as_git_login(self):
        return self.git_server() and self.as_login.isChecked()

    def key_host(self):
        """The host the secret is given for: the one typed - as an API key for a forge whose API has a host of its own,
        the API's (github.com: api.github.com - a key for github.com would never be put into its API's requests)."""
        host = self.host.text().strip().lower().rstrip(".") if self.host is not None else ""
        return FORGE_API_HOSTS.get(host, host) if self.git_server() and not self.as_git_login() else host

    def show_kind(self):
        """The choice of a Git login or an API key for a Git server's host (a Git login offered first, once), the
        user for a login, the variable for a key."""
        git = self.git_server()
        typed = self.host.text().strip().lower().rstrip(".")
        api = FORGE_API_HOSTS.get(typed, typed)
        self.as_key.setText(tr("As an API key for {host} (the agent sends a placeholder)", host=printable(api))
                            if git and api != typed else
                            tr("As an API key (the agent sends a placeholder)"))
        if git and not self.offered:
            self.offered = True
            self.as_login.setChecked(True)
        self.form.setRowVisible(self.kinds, git)
        self.form.setRowVisible(self.user, git and self.as_login.isChecked())
        self.form.setRowVisible(self.variable, not (git and self.as_login.isChecked()))

    def problem(self):
        """Why the host or variable typed cannot be taken (None: they can)."""
        if self.host is None:
            return None
        if not API_HOST.fullmatch(self.host.text().strip().lower().rstrip(".")):
            return tr("Name the server as a host name, such as api.example.com (no address, no https://).")
        if self.as_git_login():
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.user.text().strip()):
                return tr("The user: letters, digits and . _ - only.")
            return None
        if not API_VARIABLE.fullmatch(self.variable.text().strip()):
            return tr("The variable: letters, digits and _, not starting with a digit.")
        return None

    def accept(self):
        problem = self.problem()
        if problem:
            inform(self, tr("Give to a running sandbox"), problem)
            return
        super().accept()


class HostDialog(QDialog):
    """Which other host of sandbox-ai hosts gets this machine's secret SECRET (and then the sandbox there)."""

    def __init__(self, parent, hosts, secret):
        super().__init__(parent)
        self.setWindowTitle(tr("Give to a sandbox on another host"))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("The host whose sandbox gets the secret {secret}:", secret=printable(secret))))
        self.choice = QComboBox()
        self.choice.setAccessibleName(tr("Host"))
        for host in hosts:
            self.choice.addItem(printable(host), host)
        layout.addWidget(self.choice)
        layout.addWidget(explanation(tr("The secret is kept on that host first (asked next), then given to a sandbox "
                                        "that runs there.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def host(self):
        return self.choice.currentData()


class SecretDialog(QDialog):
    """A secret that is edited: its name (another one renames it wherever sandbox-ai names it) and Replace value… (a
    new value typed, hidden as Add's; the value kept is never read back, shown or filled in). What uses it is
    listed."""

    def __init__(self, parent, name, uses=(), host=LOCAL):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit secret") if host in (None, LOCAL) else
                            tr("{title} (on {host})", title=tr("Edit secret"), host=printable(host)))
        self.sai, self.old = getattr(parent, "sai", None), name
        form = self.form = QFormLayout(self)
        self.name = QLineEdit(name)
        self.name.setMaxLength(64)
        form.addRow(tr("Name"), self.name)
        self.replace = QCheckBox(tr("Replace value…"))
        self.replace.setToolTip(tr("Type a new value: it replaces the one kept, which is never shown"))
        form.addRow(tr("Value"), self.replace)
        self.value = QLineEdit()
        self.value.setMaxLength(8192)
        self.value.setPlaceholderText(tr("the new value (passed on standard input; a kept one is never read back)"))
        self.value.setAccessibleName(tr("New value"))
        self.value_row = widgets.secret_value(self.value)
        form.addRow(tr("New value"), self.value_row)
        form.addRow(explanation(tr("Used by: {uses}", uses=", ".join(printable(use) for use in uses) or "—")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.problem = plain_label(role="notice-error", selectable=True)
        form.addRow(self.problem)
        self.replace.toggled.connect(lambda on: self.form.setRowVisible(self.value_row, on))
        self.form.setRowVisible(self.value_row, False)

    def check_and_accept(self):
        name = self.name.text().strip()
        if self.sai is not None and not self.sai.valid_secret(name):
            self.problem.setText(tr("A name is letters, digits and _ (up to 64), not starting with a digit."))
            return
        if self.replace.isChecked() and not self.value.text():
            self.problem.setText(tr("Enter the new value, or untick Replace value…."))
            return
        if self.replace.isChecked() and ("\n" in self.value.text() or len(self.value.text()) > 8192):
            self.problem.setText(tr("The value must be one line of 1-8192 characters."))
            return
        self.accept()


class LoginGiveDialog(QDialog):
    """Which running sandbox gets the agent login, and - for an agent that reads its login only when it starts -
    whether the agent is started again inside it."""

    def __init__(self, parent, runs, agent, source, host=LOCAL):
        super().__init__(parent)
        self.setWindowTitle(tr("Give to a running sandbox") if host in (None, LOCAL) else
                            tr("Give to a running sandbox on {host}", host=printable(host)))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("The sandbox that gets the {agent} login ({source}):", agent=printable(agent),
                                        source=source)))
        self.choice = QComboBox()
        self.choice.setAccessibleName(tr("Sandbox"))
        self.labels = {}
        for instance in runs:
            what = instance.run if instance.run != instance.name else ""
            self.choice.addItem(printable(f"{instance.name}  ({what})" if what else instance.name), instance.name)
            self.labels[instance.name] = run_label(instance)
        layout.addWidget(self.choice)
        self.restart = QCheckBox(tr("Start its agent again inside the sandbox (its conversation continues)"))
        self.restart.setToolTip(safe_tooltip(tr(
            "This agent reads its login when it starts: without a restart it goes on with the login it has (sandbox-ai "
            "kits restart-agent NAME later does the same). Claude Code reads its login file again by itself.")))
        layout.addWidget(self.restart)
        self.restart.setVisible(agent != "claude")  # (in its place first: shown without a parent it flashes as a window)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def run(self):
        return self.choice.currentData()

    def run_label(self):
        return self.labels.get(self.run(), printable(str(self.run())))


def login_lines(info):
    """A login as `sandbox-ai login` describes it (never a token), one line each: account, kind, fingerprint."""
    info = info if isinstance(info, dict) else {}
    account = info.get("account") if isinstance(info.get("account"), dict) else {}
    lines = [f"{tr(label)}: {printable(str(account[key]))} " + tr("(as the file says)")
             for label, key in ((N_("account"), "email"), (N_("organization"), "organization"), (N_("plan"), "plan"))
             if account.get(key)]
    if info.get("kind"):
        lines.append(f"{tr('kind')}: {printable(str(info['kind']))}")
    lines.append(f"{tr('fingerprint')}: {printable(str(info.get('fingerprint') or '?'))}")
    return lines


def give_agent_login(parent, sai, host, agent, from_session, message, source):
    """An agent login for a sandbox that runs on HOST: the central login of AGENT, or the session FROM_SESSION's.
    The running sandboxes of that agent are read, one is chosen, the login is shown as `sandbox-ai login give RUN
    --dry-run --json` describes it (never a token) and asked about (login-give), then given with the fingerprint that
    was shown (`--yes --expect`), the agent started again inside the sandbox when chosen."""
    words = [agent, *([f"--from-session={from_session}"] if from_session else [])]

    def targets(found, error):
        runs = [run for run in running_on(host, found, error) if getattr(run, "agent", agent) in (agent, "", None)
                and run.name != from_session]
        if not runs:
            message.setText(printable_text(error) if error is not None else where_given(host, tr(
                "No other sandbox of {agent} runs now: a login is given to a sandbox that runs (a stopped one takes "
                "the stored login at its next start).", agent=printable(agent))))
            return
        what = tr("the stored login") if from_session is None else tr("of session {name}", name=printable(from_session))
        dialog = LoginGiveDialog(parent, runs, agent, what, host=host)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        run, called, restart = dialog.run(), dialog.run_label(), dialog.restart.isChecked()
        message.setText(tr("Reading the login…"))
        background(sai.host_run, host, "login", "give", run, *words, "--dry-run", "--json", owner=parent,
                   source=source, timeout=120, done=lambda result, error_: shown(run, called, restart, result, error_))

    def shown(run, called, restart, result, error):
        status, out, err = given_outcome(result, error)
        try:
            preview = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            preview = None
        if not isinstance(preview, dict):
            message.setText(sai.too_old_text(host, result, LOGIN_GIVE) or where_given(host, not_given_text(
                called, out, err)))
            return
        if preview.get("same"):
            message.setText(where_given(host, tr("{run} has this login already: nothing to give.", run=called)))
            return
        login = preview.get("login") if isinstance(preview.get("login"), dict) else {}
        lines = [tr("Give {source} ({agent}) to {run}? Its agent works as that account from now on.",
                    source=printable(str(preview.get("source") or "")), agent=printable(agent), run=called), "",
                 *login_lines(login), ""]
        if preview.get("current"):
            lines.append(tr("It replaces the login the sandbox has now, of the same account.")
                         if preview.get("same_account") else
                         tr("It replaces the login the sandbox has now, of ANOTHER account (or one that cannot be "
                            "told)."))
        lines.append(tr("The two then share the login: subscription logins rotate their refresh tokens, and two "
                        "sandboxes that refresh the same one can log each other out."))
        if not preview.get("reads_again"):
            lines.append(tr("Its agent is started again inside the sandbox: what it does right now ends, its "
                            "conversation continues.") if restart and preview.get("restart") else
                         tr("{agent} reads its login when it starts: until its agent is started again (sandbox-ai "
                            "kits restart-agent), it goes on with the login it has.", agent=printable(agent)))
        if not ask_first(parent, "login-give", tr("Give the login"), where_given(host, "\n".join(lines)), tr("Give")):
            return
        message.setText(tr("Giving the login…"))
        background(sai.host_run, host, "login", "give", run, *words, "--yes",
                   f"--expect={login.get('fingerprint')}", *(["--restart"] if restart else []), "--json",
                   owner=parent, work=True, source=source, timeout=300,
                   done=lambda result_, error_: given(called, result_, error_))

    def given(called, result, error):
        status, out, err = given_outcome(result, error)
        try:
            answer = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            answer = None
        if not isinstance(answer, dict):
            message.setText(sai.too_old_text(host, result, LOGIN_GIVE) or where_given(host, not_given_text(
                called, out, err)))
            return
        notes = "\n".join(printable(str(note)) for note in answer.get("notes") or [])
        head = tr("{run} has the {agent} login now.", run=called, agent=printable(agent)) if answer.get("given") \
            else tr("{run}: nothing was given.", run=called)
        if answer.get("restarted"):
            head += " " + tr("Its agent was started again.")
        message.setText(where_given(host, head + ("\n" + notes if notes else "")))
    background(give_targets(sai, host), owner=parent, source=source, timeout=90, done=targets)


def given_rows(run, name, shown):
    """The rows of GivenBox for the running sandbox RUN (NAME: its start's name) from its `net show --json` SHOWN:
    {"run", "name", "kind" ("git"|"key"), "what", "detail", "where", "revoke": the options `net revoke` takes it back
    with}."""
    rows = []
    for entry in shown.get("git") or []:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            rows.append({"run": run, "name": name, "kind": "git", "what": entry["url"],
                         "detail": str(entry.get("user") or ""),
                         "where": tr("in the sandbox (the agent can read it)") if entry.get("in_sandbox")
                         else tr("with its proxy"),
                         "revoke": ["--git-token-secret", f"{entry['url']}=REVOKE"]})
    for entry in shown.get("api_keys") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            rows.append({"run": run, "name": name, "kind": "key", "what": str(entry.get("host") or ""),
                         "detail": entry["name"] + (f" ({entry['secret']})" if entry.get("secret") and
                                                    entry["secret"] != entry["name"] else ""),
                         "where": tr("with its proxy"), "revoke": ["--api-key", entry["name"]]})
    return rows


class GivenBox(CredentialTab):
    """What the sandboxes that run on the chosen host were given during their run - Git logins (git-logins give) and
    API keys (secrets give) - each taken back with Revoke… (sandbox-ai net revoke, asked first). Names only."""

    def __init__(self, sai):
        super().__init__(tr("Given to running sandboxes"))
        self.sai, self.host, self.rows, self.token, self.wanted, self.counted = sai, LOCAL, [], 0, None, None
        self.add_about(tr("What each sandbox that runs was given while it runs (Give to a running sandbox…, sandbox-ai "
                          "git-logins give, secrets give): until it ends, or until you take it back here."))
        self.list = self.set_list(multi_select(table(
            [tr("Sandbox"), tr("Kind"), tr("For"), tr("As"), tr("Token")], stretch=2, compact=(1, 4))))
        self.revoke_button, self.refresh_button = QPushButton(tr("Revoke…")), QPushButton(tr("Refresh"))
        self.revoke_button.setToolTip(safe_tooltip(tr(
            "Take the selected login or key back from its sandbox, at once (sandbox-ai git-logins revoke, secrets "
            "revoke); a host a key opened stays allowed")))
        self.revoke_button.clicked.connect(lambda _=False: self.revoke())
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.add_buttons(self.revoke_button, self.refresh_button)
        delete_key(self.list, self.revoke)

    def set_host(self, host):
        if host != self.host:
            self.host, self.rows = host, []
            fill(self.list, [])
            self.refresh()

    def refresh(self):
        host = self.host
        self.token += 1
        token = self.token

        def read():
            runs = running_on(host, give_targets(self.sai, host)(), None)
            rows = []
            for run in runs:
                status, out, _err = self.sai.host_run(host, "net", "show", run.run, "--json", timeout=60)
                try:
                    shown = json.loads(out) if status == 0 else {}
                except (ValueError, RecursionError):
                    shown = {}
                rows += given_rows(run.run, run.name, shown if isinstance(shown, dict) else {})
            return rows
        background(read, owner=self, timeout=300, source=self.refresh_button,
                   done=lambda result, error: self.shown(host, token, result, error))

    def shown(self, host, token, result, error):
        if token != self.token or host != self.host:
            return
        if error is not None:
            self.message.setText(printable_text(error))
            result = []
        self.rows = list(result or [])
        kinds = {"git": tr("Git login"), "key": tr("API key")}
        fill(self.list, [[run_label(types.SimpleNamespace(run=r["run"], name=r["name"])), kinds[r["kind"]],
                          r["what"], r["detail"], r["where"]] for r in self.rows],
             ["warn" if "sandbox" in r["where"] and r["kind"] == "git" and r["where"] != tr("with its proxy") else "ok"
              for r in self.rows], keys=[self.key(r) for r in self.rows])
        if self.wanted is not None:  # (opened for one sandbox: its rows selected)
            for index, row in enumerate(self.rows):
                if self.wanted in (row["run"], row["name"]):
                    self.list.selectRow(index)
                    break
            self.wanted = None
        if callable(self.counted):
            self.counted(len(self.rows))

    @staticmethod
    def key(row):
        return "\t".join((row["run"], row["kind"], row["what"], row["detail"]))

    def chosen(self):
        keys = set(selected_keys(self.list))
        return [row for row in self.rows if self.key(row) in keys]

    def revoke(self):
        """Revoke…: the selected logins and keys taken back from their sandboxes, after one question naming them
        (net-revoke), each sandbox with one `net revoke`."""
        chosen, host = self.chosen(), self.host
        if not chosen:
            self.message.setText(tr("Choose what to take back first."))
            return
        names = [f"{run_label(types.SimpleNamespace(run=r['run'], name=r['name']))}: "
                 + (tr("Git login for {url}", url=printable(r["what"])) if r["kind"] == "git" else
                    tr("API key {name} for {host}", name=printable(r["detail"]), host=printable(r["what"])))
                 for r in chosen]
        if not ask_first(self, "net-revoke", tr("Revoke?"), where_given(host, several(tr(
                "Take these back from their sandboxes, at once? What a sandbox does with one right now fails; a host "
                "a key opened stays allowed."), names)), tr("Revoke")):
            return
        by_run = {}
        for row in chosen:
            by_run.setdefault(row["run"], []).extend(row["revoke"])
        self.message.setText(tr("Working…"))

        def done(outcomes):
            failures = report_each(self, tr("Revoke"), outcomes, printable)
            self.message.setText(tr("Taken back from {count} of {total} sandboxes.",
                                    count=len(by_run) - len(failures), total=len(by_run)))
            self.refresh()
        run_each(lambda run: self.sai.host_run(host, "net", "revoke", run, *by_run[run], timeout=90), list(by_run),
                 owner=self, source=self.revoke_button, timeout=120 * len(by_run), done=done)
