"""The Credentials page after 2026-11-19 (user: "wieso kann man bei 'Zugänge' eingetragene Creds nicht editieren??",
"check in-depth if ... creds can really be added to sessions, also remote sessions"):

  Edit…     on every tab (button, right click, double click): the entry's dialog with its fields filled in - never a
            secret's value; Replace value… takes a new one. Asked first; the commands are sandbox-ai's own (git-logins
            edit, publish-logins edit, secrets rename / set, vpn set).
  Give…     agent logins (the stored one, a session's) for a sandbox that runs; this machine's secret for a sandbox
            on another host (sent there first, asked first); greyed out with the reason where a kind cannot be given
            to a sandbox that runs (publish logins, identities, VPN profiles).
  Revoke…   Given to sandboxes: what each running sandbox was given during its run, taken back after a question.

The window's page with a backend that records what it is asked to run (what each command then does is the core's:
tests/test_credentials_edit.py, test_credentials_give.py; the real commands through the window: the app tests of the
two verifications)."""
import json
import re
import types

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import credentials_page as C, vpn_page as V, widgets  # noqa: E402

SECRET = "ghp_" + "N3wT0k3nV4lu3" * 2 + "abcdefgh"


class Sai:
    """What the page asks of sandbox-ai, recorded: (host, *args) of every command, the standard input of those that
    got one. RUNS: the sandboxes that run ({host: [(run, name, agent)]}); ANSWERS: {first words: (status, out, err)}."""
    agents = ("claude", "codex", "opencode")
    PUBLISH_HOSTS = {"pypi": "upload.pypi.org", "gitlab": "gitlab.com"}

    def __init__(self):
        self.ran, self.inputs, self.answers = [], [], {}
        self.runs = {"local": [("codex-1a2b3c4d", "api", "codex"), ("claude-5e6f7a8b", "web", "claude")],
                     "ct1": [("far", "far", "codex")]}

    # -- reading --
    def login_status(self):
        return [("claude", "store", None, "~/auth/claude"), ("codex", "store", None, "~/auth/codex")]

    def host_login_status(self, host):
        return self.login_status()

    def secrets(self):
        return "file", [("GH_PUSH", "file"), ("PYPI_TOKEN", "file")]

    def host_secrets(self, host):
        return "keyring", [("FAR_KEY", "keyring")]

    def git_logins(self):
        return [{"url": "https://git.example.org", "user": "alice", "secret": "GH_PUSH"}]

    def host_git_logins(self, host):
        return self.git_logins()

    def publish_logins(self):
        return [{"kind": "gitlab", "host": "gitlab.example.org", "secret": "PYPI_TOKEN"}]

    def host_publish_logins(self, host):
        return self.publish_logins()

    def has_identities(self):
        return False

    def vpn_profiles(self):
        return [{"profile": "tail", "type": "tailscale", "provider": "tailscale"}]

    def host_vpn_profiles(self, host):
        return self.vpn_profiles()

    def session_logins(self, host):
        return {"sessions": [{"id": "s1", "name": "donor", "agent": "codex", "state": "stopped", "login": True,
                              "source": "file", "info": {"fingerprint": "1a2b3c4d", "kind": "subscription",
                                                         "account": {"email": "a@example.org"}}}], "central": {}}

    def remote_hosts(self):
        return ["ct1"]

    def instances(self):
        return [types.SimpleNamespace(run=run, name=name, agent=agent, kind="session", state="running", host="local")
                for run, name, agent in self.runs["local"]], None

    def host_instances(self, host):
        return [types.SimpleNamespace(run=run, name=name, agent=agent, kind="session", state="running", host=host)
                for run, name, agent in self.runs.get(host, [])]

    def valid_secret(self, name, value=None):
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name or ""))

    def secret_exists(self, name, host):
        return True

    def login_url_problem(self, command, values):
        return None

    def too_old_text(self, host, result, command):
        return None

    def command(self, *args):
        return ["sandbox-ai", *args]

    @staticmethod
    def token_secret_name(kind=None, url=None):
        return "OFFERED_NAME"

    # -- running --
    def answer(self, args):
        for words, result in self.answers.items():
            if tuple(args[:len(words)]) == words:
                return result(args) if callable(result) else result
        return 0, "done\n", ""

    def call(self, *args, timeout=60, input=None):
        return self.host_run("local", *args, input=input)

    def host_run(self, host, *args, timeout=120, input=None):
        self.ran.append((host, *args))
        if input is not None:
            self.inputs.append((args, input))
        return self.answer(args)

    def set_secret(self, name, value, host="local"):
        self.ran.append((host, "secrets", "set", name))
        self.inputs.append((("secrets", "set", name), value + "\n"))
        return 0, f"{name} kept", ""

    def delete_secret(self, name, host="local"):
        return 0, "", ""

    def add_login(self, host, command, add_args, secret, value=None, existed=None):
        if value:
            self.set_secret(secret, value, host)
        return self.host_run(host, command, *add_args)


@pytest.fixture
def world(qapp, monkeypatch):
    """The page with its lists read, every question answered yes and kept (ASKED: (policy key, text))."""
    asked = []
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])

    def ask(parent, action, title, text, accept, **kw):
        asked.append((action, text))
        return True
    for module in (C, V):
        monkeypatch.setattr(module, "ask_first", ask)
    sai = Sai()
    page = C.CredentialsPage(sai, lambda *a, **k: None)
    yield types.SimpleNamespace(page=page, sai=sai, asked=asked)
    # What a step started in the background (the offer after an edit, a list read again) ends here, while the
    # dialogs and questions are still the test's: nothing of it reaches a later test.
    for _ in range(5):
        widgets.ThreadCall.wait_all(30)
        qapp.processEvents()
    page.deleteLater()


def loaded(world, wait):
    page = world.page
    page.refresh_all()
    assert wait(lambda: not widgets.busy(page) and page.git.list.rowCount() == 1 and page.secrets.rowCount() == 2
                and page.vpn.profiles.rowCount() == 1 and page.session_logins.list.rowCount() == 1, 30)
    return page


def dialog_of(monkeypatch, cls, fill, seen=None):
    """The next dialogs of CLS: filled by FILL(dialog), accepted through their own check."""
    def run(dialog):
        if seen is not None:
            seen.append(dialog)
        fill(dialog)
        if hasattr(dialog, "check_and_accept"):
            dialog.check_and_accept()
            assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted, dialog.problem.text()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(cls, "exec", run)


def said(wait, label, part):
    """LABEL's text once it holds PART (what the step waited for says: an earlier step's message, or a list's own
    "N sessions.", is not it)."""
    assert wait(lambda: part in label.text(), 30), label.text()
    return label.text()


def ran(wait, world, *words):
    """Waits until a command starting with WORDS (after its host) ran; returns every such command."""
    found = lambda: [args for args in world.sai.ran if args[1:1 + len(words)] == words]  # noqa: E731
    assert wait(lambda: bool(found()), 30), world.sai.ran
    return found()


def every_text(widget):
    """Every text a dialog shows or holds: its labels, fields, buttons and combo boxes."""
    found = []
    for kind in (QtWidgets.QLabel, QtWidgets.QLineEdit, QtWidgets.QAbstractButton):
        found += [child.text() for child in widget.findChildren(kind)]
    for combo in widget.findChildren(QtWidgets.QComboBox):
        found += [combo.currentText(), *[combo.itemText(i) for i in range(combo.count())]]
    return " ".join(found)


# --- Edit… ------------------------------------------------------------------------------------------------------------

def test_edit_is_on_every_tab_as_a_button_on_the_right_click_and_on_a_double_click(world, wait, monkeypatch):
    page = loaded(world, wait)
    for view, button in ((page.git.list, page.git.edit_button), (page.publish.list, page.publish.edit_button),
                         (page.secrets, page.edit_button), (page.vpn.profiles, page.vpn.edit_button)):
        assert button.text() == "Edit…"
        menu = [action.text().replace("&", "") for action in widgets.menu_of(view).actions()]
        assert "Edit…" in menu, menu
    opened = []
    for box, name in ((page.git, "git"), (page.publish, "publish")):
        monkeypatch.setattr(box, "edit", lambda name=name: opened.append(name))
    monkeypatch.setattr(page, "edit_secret", lambda: opened.append("secret"))
    monkeypatch.setattr(page.vpn, "edit", lambda: opened.append("vpn"))
    for view in (page.git.list, page.publish.list, page.secrets, page.vpn.profiles):
        view.selectRow(0)
        view.cellDoubleClicked.emit(0, 0)
    assert opened == ["git", "publish", "secret", "vpn"]


def test_a_git_login_is_edited_in_its_dialog_and_no_token_is_ever_shown(world, wait, monkeypatch):
    page = loaded(world, wait)
    box, seen = page.git, []
    box.list.selectRow(0)

    def change(dialog):
        # filled in with what the login is; its secret chosen, kept as it is - the token nowhere
        assert dialog.edits["url"].text() == "https://git.example.org" and dialog.edits["user"].text() == "alice"
        assert dialog.stored.isChecked() and dialog.secret_choice.text() == "GH_PUSH" and dialog.value.text() == ""
        assert dialog.typed.text() == "Replace value…"
        dialog.edits["url"].setText("https://code.example.org")
        dialog.edits["user"].setText("bob")
    dialog_of(monkeypatch, C.LoginDialog, change, seen)
    dialog_of(monkeypatch, C.GiveDialog, lambda dialog: None)
    box.edit_button.click()
    assert ran(wait, world, "git-logins", "edit") == [("local", "git-logins", "edit", "--url=https://code.example.org",
                                                       "--user=bob", "--", "https://git.example.org")]
    assert world.sai.inputs == [], "no value was sent: the secret stays as it is"
    question = next(text for action, text in world.asked if action == "login-edit")
    assert "https://git.example.org" in question and "a sandbox that runs keeps the login it was started with" in question
    assert seen[0].windowTitle() == "Edit Git login"
    # A login used by running sandboxes: Give to a running sandbox… is offered for them (the login as it is now).
    assert wait(lambda: any(args[1:3] == ("git-logins", "give") for args in world.sai.ran), 30), world.sai.ran
    offer = next(text for _title, text, _details in widgets.SHOWN if "Give the login as it is now" in text)
    assert "2 sandbox(es) run with what they were started with" in offer
    given = next(args for args in world.sai.ran if args[1:3] == ("git-logins", "give"))
    assert given[3:] == ("--secret", "GH_PUSH", "--user", "bob", "--", "codex-1a2b3c4d", "https://code.example.org")


def test_replace_value_takes_a_new_token_on_standard_input_and_changes_nothing_else(world, wait, monkeypatch):
    page = loaded(world, wait)
    box = page.git
    box.list.selectRow(0)

    def replace(dialog):
        dialog.typed.setChecked(True)  # Replace value…
        assert dialog.secret.text() == "GH_PUSH", "kept as the secret the login names (not a name offered anew)"
        assert dialog.value.text() == "", "the value kept is never filled in"
        dialog.value.setText(SECRET)
    dialog_of(monkeypatch, C.LoginDialog, replace)
    dialog_of(monkeypatch, C.GiveDialog, lambda dialog: None)
    box.edit()
    assert ran(wait, world, "git-logins", "edit") == [("local", "git-logins", "edit", "--user=alice", "--",
                                                       "https://git.example.org")]
    assert world.sai.inputs == [(("secrets", "set", "GH_PUSH"), SECRET + "\n")]
    ran(wait, world, "git-logins", "give")  # (the offer for the running sandboxes, taken: over before the test is)
    assert not any(SECRET in json.dumps(args) for args in world.sai.ran), "never an argument"
    assert [action for action, _ in world.asked][:2] == ["login-edit", "secret-replace"]
    assert SECRET not in box.message.text() and SECRET not in json.dumps(world.asked)


def test_an_unchanged_login_runs_nothing(world, wait, monkeypatch):
    page = loaded(world, wait)
    page.git.list.selectRow(0)
    dialog_of(monkeypatch, C.LoginDialog, lambda dialog: None)
    before = list(world.sai.ran)
    page.git.edit()
    assert page.git.message.text() == "Nothing changed." and world.sai.ran == before and world.asked == []


def test_a_publish_login_is_edited_and_cannot_be_given_to_a_running_sandbox(world, wait, monkeypatch):
    page = loaded(world, wait)
    box = page.publish
    box.list.selectRow(0)

    def change(dialog):
        assert dialog.kind.currentText() == "gitlab" and not dialog.kind.isEnabled(), "the kind names the login"
        assert dialog.edits["url"].text() == "https://gitlab.example.org"
        assert dialog.secret_choice.text() == "PYPI_TOKEN"
        dialog.edits["url"].setText("")
        dialog.secret_choice.setText("GL_TOKEN")
    dialog_of(monkeypatch, C.LoginDialog, change)
    box.edit_button.click()
    assert ran(wait, world, "publish-logins", "edit") == [("local", "publish-logins", "edit", "gitlab",
                                                           "--secret=GL_TOKEN", "--url=default")]
    # Give to a running sandbox…: there, greyed out, saying why and what to do instead.
    assert box.give_button.text() == "Give to a running sandbox…" and not box.give_button.isEnabled()
    assert "cannot be given to a sandbox that runs" in box.give_button.toolTip()
    assert "--publish KIND" in box.give_button.toolTip()
    assert "Give to a running sandbox…" in [a.text().replace("&", "") for a in widgets.menu_of(box.list).actions()]
    vpn = page.vpn.give_button
    assert not vpn.isEnabled() and "--vpn PROFILE" in vpn.toolTip()


def test_a_secret_is_renamed_and_its_value_replaced_without_ever_being_shown(world, wait, monkeypatch):
    page = loaded(world, wait)
    page.tabs.setCurrentIndex(page.tab_of("secrets"))
    page.secrets.selectRow(0)
    seen = []

    def change(dialog):
        assert dialog.name.text() == "GH_PUSH" and dialog.value.text() == ""
        assert not dialog.form.isRowVisible(dialog.value_row), "the value's field: only with Replace value…"
        assert "Git login https://git.example.org" in every_text(dialog), "what uses it is named"
        dialog.name.setText("GH_TOKEN")
        dialog.replace.setChecked(True)
        dialog.value.setText(SECRET)
    dialog_of(monkeypatch, C.SecretDialog, change, seen)
    page.edit_button.click()
    text = said(wait, page.message, "a sandbox that runs keeps the one it was given")
    # the value first, under the name it has, on standard input; then the rename
    assert world.sai.inputs == [(("secrets", "set", "GH_PUSH"), SECRET + "\n")]
    assert [args for args in world.sai.ran if args[1] == "secrets"][-2:] == [
        ("local", "secrets", "set", "GH_PUSH"), ("local", "secrets", "rename", "GH_PUSH", "GH_TOKEN")]
    assert [action for action, _ in world.asked] == ["secret-replace", "secret-rename"]
    rename = world.asked[1][1]
    assert "GH_PUSH" in rename and "GH_TOKEN" in rename and "trusted project files" in rename
    assert "a sandbox that runs keeps the one it was given" in text
    assert SECRET not in text and SECRET not in json.dumps(world.asked) and SECRET not in json.dumps(world.sai.ran)
    assert seen[0].value.text() == "", "the dialog keeps no value"


def test_a_secret_dialog_refuses_a_name_that_is_none_and_an_empty_new_value(world, wait):
    page = loaded(world, wait)
    dialog = C.SecretDialog(page, "GH_PUSH")
    dialog.name.setText("9bad name")
    dialog.check_and_accept()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted and "letters, digits and _" in dialog.problem.text()
    dialog.name.setText("GH_PUSH")
    dialog.replace.setChecked(True)
    dialog.check_and_accept()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted and "Enter the new value" in dialog.problem.text()
    dialog.deleteLater()


def test_a_vpn_profile_is_edited_with_its_settings_filled_in(world, wait, monkeypatch):
    page = loaded(world, wait)
    world.sai.answers[("vpn", "show")] = (0, json.dumps({"profile": "tail", "type": "tailscale", "settings": {
        "provider": "tailscale", "auth-key-secret": "TS_KEY", "exit-node": None, "login-server": None,
        "hostname": "box"}}), "")
    box = page.vpn
    box.profiles.selectRow(0)

    def change(dialog):
        assert {key: edit.text() for key, edit in dialog.edits.items()} == {
            "provider": "tailscale", "auth-key-secret": "TS_KEY", "exit-node": "", "login-server": "",
            "hostname": "box"}
        dialog.edits["exit-node"].setText("exit1")
        dialog.edits["auth-key-secret"].setText("")
    dialog_of(monkeypatch, V.VpnEditDialog, change)
    box.edit_button.click()
    assert ran(wait, world, "vpn", "set") == [("local", "vpn", "set", "--auth-key-secret=none", "--exit-node=exit1",
                                               "--", "tail")]
    said(wait, box.message, "done")  # (what the command said: the edit is over before the next one starts)
    assert wait(lambda: not widgets.busy(box), 30)
    question = next(text for action, text in world.asked if action == "vpn-replace")
    assert "tail" in question and "--exit-node exit1" in question
    # an older sandbox-ai there (no "settings"): said, nothing opened
    world.sai.answers[("vpn", "show")] = (0, json.dumps({"profile": "tail", "type": "tailscale"}), "")
    box.edit()
    said(wait, box.message, "older and cannot change a profile in place")
    assert len(ran(wait, world, "vpn", "set")) == 1, "nothing more was sent"


# --- Give…: agent logins ----------------------------------------------------------------------------------------------

PREVIEW = {"agent": "codex", "run": {"name": "api", "id": "s9", "state": "running"}, "source": "the central login",
           "login": {"fingerprint": "abcdef12", "kind": "subscription", "account": {"email": "me@example.org"}},
           "current": {"fingerprint": "00000000"}, "same": False, "same_account": False, "reads_again": False,
           "restart": True}


def test_the_stored_agent_login_is_given_to_a_running_sandbox_shown_first_then_asked(world, wait, monkeypatch):
    page = loaded(world, wait)
    page.tabs.setCurrentIndex(page.tab_of("logins"))
    world.sai.answers[("login", "give")] = lambda args: (0, json.dumps(
        PREVIEW if "--dry-run" in args else {"given": True, "restarted": True, "notes": ["shared from now on"]}), "")
    page.logins.selectRow(1)  # codex
    offered = []

    def choose(dialog):
        offered.extend(dialog.choice.itemData(i) for i in range(dialog.choice.count()))
        assert dialog.restart.isVisibleTo(dialog) or not dialog.isVisible()
        dialog.restart.setChecked(True)
    dialog_of(monkeypatch, C.LoginGiveDialog, choose)
    assert page.give_login_button.text() == "Give to a running sandbox…"
    assert "Give to a running sandbox…" in [a.text().replace("&", "") for a in widgets.menu_of(page.logins).actions()]
    page.give_login_button.click()
    text = said(wait, page.logins_message, "has the codex login now")
    assert offered == ["api"], "the running sandboxes of that agent, and only they"
    runs = [args for args in world.sai.ran if args[1:3] == ("login", "give")]
    assert runs == [("local", "login", "give", "api", "codex", "--dry-run", "--json"),
                    ("local", "login", "give", "api", "codex", "--yes", "--expect=abcdef12", "--restart", "--json")]
    question = next(text_ for action, text_ in world.asked if action == "login-give")
    assert "the central login" in question and "me@example.org" in question and "abcdef12" in question
    assert "ANOTHER account" in question and "Its agent is started again inside the sandbox" in question
    assert "has the codex login now" in text and "Its agent was started again." in text and "shared from now on" in text


def test_a_sessions_login_is_given_to_another_running_sandbox_of_its_agent(world, wait, monkeypatch):
    page = loaded(world, wait)
    page.tabs.setCurrentIndex(page.tab_of("logins"))
    world.sai.answers[("login", "give")] = lambda args: (0, json.dumps(
        {**PREVIEW, "source": "the login of session donor"} if "--dry-run" in args else
        {"given": True, "restarted": False, "notes": []}), "")
    box = page.session_logins
    box.list.selectRow(0)
    dialog_of(monkeypatch, C.LoginGiveDialog, lambda dialog: None)
    assert box.give_button.isEnabled()
    widgets.menu_of(box.list)  # (on the right click too: the list menu test names it)
    box.give_button.click()
    said(wait, box.message, "has the codex login now")
    runs = [args for args in world.sai.ran if args[1:3] == ("login", "give")]
    assert runs[0] == ("local", "login", "give", "api", "codex", "--from-session=donor", "--dry-run", "--json")
    assert runs[1] == ("local", "login", "give", "api", "codex", "--from-session=donor", "--yes",
                       "--expect=abcdef12", "--json")
    question = next(text for action, text in world.asked if action == "login-give")
    assert "the login of session donor" in question and "codex reads its login when it starts" in question


def test_no_running_sandbox_of_that_agent_is_said_and_a_refusal_names_the_run(world, wait, monkeypatch):
    page = loaded(world, wait)
    world.sai.runs["local"] = [("claude-5e6f7a8b", "web", "claude")]
    page.logins.selectRow(1)  # codex: none runs
    page.give_login_button.click()
    said(wait, page.logins_message, "No other sandbox of codex runs now")
    world.sai.runs["local"] = [("codex-1a2b3c4d", "api", "codex")]
    world.sai.answers[("login", "give")] = (1, "", "sandbox-ai login: no central codex login is stored (sandbox-ai "
                                                   "login codex)\n")
    dialog_of(monkeypatch, C.LoginGiveDialog, lambda dialog: None)
    page.give_login_button.click()
    text = said(wait, page.logins_message, "Not given to codex-1a2b3c4d (api)")
    assert "no central codex login is stored" in text
    assert not any(action == "login-give" for action, _ in world.asked), "nothing to ask about"


# --- Give…: this machine's secret for a sandbox on another host -------------------------------------------------------

def test_a_secret_of_this_machine_is_sent_to_another_host_asked_first_then_given_there(world, wait, monkeypatch):
    page = loaded(world, wait)
    page.tabs.setCurrentIndex(page.tab_of("secrets"))
    page.secrets.selectRow(0)
    hosts = []
    dialog_of(monkeypatch, C.HostDialog, lambda dialog: hosts.extend(
        dialog.choice.itemData(i) for i in range(dialog.choice.count())))

    def as_key(dialog):
        dialog.host.setText("api.example.com")
        dialog.variable.setText("EXAMPLE_KEY")
    dialog_of(monkeypatch, C.GiveDialog, as_key)
    assert page.give_far_button.isEnabled()
    page.give_far_button.click()
    assert wait(lambda: any(args[1:3] == ("secrets", "give") for args in world.sai.ran), 30), (
        world.sai.ran, page.message.text())
    assert hosts == ["ct1"]
    sent = [args for args in world.sai.ran if args[1:3] == ("secrets", "send")]
    assert sent == [("local", "secrets", "send", "GH_PUSH", "ct1", "--yes")], "sent by this machine's sandbox-ai"
    assert world.sai.inputs == [], "the value never passes through the app"
    assert ("ct1", "secrets", "give", "far", "GH_PUSH", "--api-key", "api.example.com", "--as",
            "EXAMPLE_KEY") in world.sai.ran, "then given on that host, to its running sandbox"
    kinds = [action for action, _ in world.asked]
    assert kinds == ["secret-send", "net-allow"]
    send = world.asked[0][1]
    assert "GH_PUSH" in send and "ct1" in send and "whoever controls ct1 can use it" in send
    assert "never in a command line, a log or a file" in send


def test_a_secret_kept_there_already_is_replaced_only_after_a_second_yes(world, wait, monkeypatch):
    page = loaded(world, wait)
    page.secrets.selectRow(0)
    world.sai.answers[("secrets", "send")] = lambda args: (0, "sent\n", "") if "--replace" in args else (
        1, "", "sandbox-ai secrets: ct1 keeps a secret GH_PUSH already: add --replace to replace its value there\n")
    dialog_of(monkeypatch, C.HostDialog, lambda dialog: None)
    dialog_of(monkeypatch, C.GiveDialog, lambda dialog: dialog.host.setText("api.example.com"))
    page.give_far_button.click()
    assert wait(lambda: any(args[1:3] == ("secrets", "give") for args in world.sai.ran), 30), page.message.text()
    sent = [args[3:] for args in world.sai.ran if args[1:3] == ("secrets", "send")]
    assert sent == [("GH_PUSH", "ct1", "--yes"), ("GH_PUSH", "ct1", "--yes", "--replace")]
    assert [action for action, _ in world.asked][:2] == ["secret-send", "secret-replace"]


def test_the_give_dialog_leads_to_a_sandbox_on_another_host(world, wait, monkeypatch):
    """The sandbox meant is not in the dialog's list because it runs on another host: the dialog's own button "A
    sandbox on another host…" goes the send-then-give way (the same as the right click's entry); the dialog of a
    give on another host has no such button."""
    page = loaded(world, wait)
    page.secrets.selectRow(0)
    seen = []

    def first_local_then_far(dialog):
        seen.append(dialog)
        if len(seen) == 1:
            assert dialog.far_button.text() == "A sandbox on another host…"
            dialog.far_button.click()
            return QtWidgets.QDialog.DialogCode.Rejected
        assert not hasattr(dialog, "far_button"), "on that host: its sandboxes are the ones listed"
        dialog.host.setText("api.example.com")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(C.GiveDialog, "exec", first_local_then_far)
    dialog_of(monkeypatch, C.HostDialog, lambda dialog: None)
    page.give_key_button.click()
    ran(wait, world, "secrets", "send")
    given = ran(wait, world, "secrets", "give")
    assert given[0][0] == "ct1" and len(seen) == 2
    said(wait, page.message, "has the key")
    # no sandbox runs here at all: the message names the way to one on another host
    world.sai.runs["local"] = []
    page.give_key_button.click()
    said(wait, page.message, "For one on another host: right click, Give to a sandbox on another host…")


def test_the_other_host_button_is_for_this_machines_credentials_only(world, wait):
    page = loaded(world, wait)
    assert page.give_far_button.isEnabled() and page.git.give_far_button.isEnabled()
    page.hosts.set_host("ct1")
    assert wait(lambda: page.host() == "ct1", 30)
    for button in (page.give_far_button, page.git.give_far_button):
        assert not button.isEnabled() and "choose This machine above" in button.toolTip()


# --- Given to sandboxes: what a running sandbox was given, taken back -------------------------------------------------

SHOWN = {"git": [{"url": "https://git.example.org", "user": "alice"},
                 {"url": "http://localhost:3080", "user": "x-access-token", "in_sandbox": True}],
         "api_keys": [{"name": "OR_KEY", "host": "openrouter.ai", "port": 443, "secret": "OPENROUTER"}]}


def test_what_a_running_sandbox_was_given_is_listed_and_taken_back_after_a_question(world, wait, monkeypatch):
    page = loaded(world, wait)
    world.sai.answers[("net", "show")] = lambda args: (0, json.dumps(SHOWN if args[2] == "codex-1a2b3c4d" else {}),
                                                       "")
    page.show_given("local", "codex-1a2b3c4d")  # (Sandboxes › Secrets and logins… of that start)
    box = page.given
    assert wait(lambda: box.list.rowCount() == 3, 30), box.message.text()
    rows = [[box.list.item(r, c).text() for c in range(box.list.columnCount())] for r in range(3)]
    assert rows == [["codex-1a2b3c4d (api)", "Git login", "https://git.example.org", "alice", "with its proxy"],
                    ["codex-1a2b3c4d (api)", "Git login", "http://localhost:3080", "x-access-token",
                     "in the sandbox (the agent can read it)"],
                    ["codex-1a2b3c4d (api)", "API key", "openrouter.ai", "OR_KEY (OPENROUTER)", "with its proxy"]]
    assert page.tabs.currentIndex() == page.tab_of("given")
    assert page.tabs.tabText(page.tab_of("given")) == "Given to sandboxes (3)"
    assert [a.text().replace("&", "") for a in widgets.menu_of(box.list).actions()][:2] == ["Revoke…", "Refresh"]
    box.list.selectAll()
    box.revoke_button.click()
    assert wait(lambda: any(args[1:3] == ("net", "revoke") for args in world.sai.ran), 30), box.message.text()
    assert ("local", "net", "revoke", "codex-1a2b3c4d", "--git-token-secret", "https://git.example.org=REVOKE",
            "--git-token-secret", "http://localhost:3080=REVOKE", "--api-key", "OR_KEY") in world.sai.ran
    question = next(text for action, text in world.asked if action == "net-revoke")
    assert "API key OR_KEY (OPENROUTER) for openrouter.ai" in question
    assert "Git login for https://git.example.org" in question and "codex-1a2b3c4d (api)" in question
    said(wait, box.message, "Taken back from 1 of 1 sandboxes.")


def test_nothing_is_taken_back_without_a_yes(world, wait, monkeypatch):
    page = loaded(world, wait)
    world.sai.answers[("net", "show")] = (0, json.dumps(SHOWN), "")
    monkeypatch.setattr(C, "ask_first", lambda *a, **k: False)
    page.show_given("local", "api")
    box = page.given
    assert wait(lambda: box.list.rowCount() >= 3, 30)
    box.list.selectRow(0)
    box.revoke()
    assert not any(args[1:3] == ("net", "revoke") for args in world.sai.ran)
    box.list.clearSelection()
    box.revoke()
    assert box.message.text() == "Choose what to take back first."


def test_the_allowed_now_rows_take_back_a_given_login_and_key():
    from sandbox_ai_app import approvals_sections as A
    rows = A.allowed_rows({**SHOWN, "https": [], "tcp": []})
    assert [(row["destination"], row["names"], row["revoke"]) for row in rows] == [
        ("https://git.example.org", "alice", ["--git-token-secret", "https://git.example.org=REVOKE"]),
        ("http://localhost:3080", "x-access-token", ["--git-token-secret", "http://localhost:3080=REVOKE"]),
        ("openrouter.ai", "OR_KEY", ["--api-key", "OR_KEY"])]
    assert rows[1]["kind"] == "Git login (token in the sandbox)"
    assert len({A.row_identity(row) for row in rows}) == 3


# --- Sandboxes › Allow…: a Git login for a run whose proxy cannot log in ------------------------------------------

def test_the_allow_dialog_asks_a_second_time_before_a_token_goes_into_a_sandbox(qapp, monkeypatch, wait):
    """Sandboxes › Allow… with a Git login for a run without the connection proxy: sandbox-ai refuses it (naming the
    only other way), and the window asks a second question that says the agent can read the token - only that yes
    sends --git-token-in-sandbox; a no leaves it refused."""
    from sandbox_ai_app import backend, ui
    refusal = ("sandbox-ai net: web cannot be given the Git login for http://localhost:3080 through its connection "
               "proxy: it runs without the connection proxy (--no-proxy). It can only be put into the sandbox's session "
               "directory for its Git helper, where the agent can read the token: to give it so, add --in-sandbox "
               "(sandbox-ai git-logins give ... --in-sandbox; net allow: --git-token-in-sandbox); or start it again "
               "with the login\n")
    ran, asked, reported = [], [], []

    class Recorder:
        def host_run(self, host, *args, timeout=120, input=None):
            ran.append((host, *args))
            return (0, "ok", "") if "--git-token-in-sandbox" in args else (1, "", refusal)

        def presets(self):
            return {}

    class Dialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def arguments(self):
            return ["--git-token-secret", "http://localhost:3080=GT"]

        def kept(self):
            return False
    monkeypatch.setattr(ui, "NetworkDialog", Dialog)
    monkeypatch.setattr(widgets, "exec_dropped", lambda dialog, *a: dialog.exec())
    page = types.SimpleNamespace(sai=Recorder(), run_buttons=[], update_buttons=lambda: None,
                                 reported=lambda title, result, error, draft=None: reported.append(result))
    answers = iter([True, False, True, True])
    monkeypatch.setattr(ui, "ask_first", lambda parent, action, title, text, accept, **kw: asked.append(
        (action, text, accept)) or next(answers))
    monkeypatch.setattr(ui, "background", lambda function, *args, done=None, **kw: done(function(*args), None))
    # the second question answered no: refused as it was, nothing more sent
    ui.SandboxesPage.network_dialog(page, backend.LOCAL if hasattr(backend, "LOCAL") else "local", "web", "allow",
                                    "Allow for web", {"--git-token-env"}, None)
    assert [args[1:3] for args in ran] == [("net", "allow")] and "--git-token-in-sandbox" not in ran[0]
    assert asked[1][0] == "net-allow" and "the agent can read it" in asked[1][1] and "web" in asked[1][1]
    assert asked[1][2] == "Give into the sandbox" and reported[-1][0] == 1
    # answered yes: sent again with the option that says so
    ui.SandboxesPage.network_dialog(page, "local", "web", "allow", "Allow for web", {"--git-token-env"}, None)
    assert ran[-1] == ("local", "net", "allow", "web", "--git-token-secret", "http://localhost:3080=GT",
                       "--git-token-in-sandbox")
    assert reported[-1][0] == 0
