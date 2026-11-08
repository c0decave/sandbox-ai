"""The Credentials page: one overview of everything configured with its state, a tab per kind; a click on a row
opens its tab with the entry selected; Git and publish logins through their commands; names only."""
import re

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import widgets  # noqa: E402


class FakeSai:
    agents = ("claude", "codex", "opencode")

    def __init__(self):
        self.calls = []

    def login_status(self):
        return [("claude", "store", None, "~/.local/share/sandbox-ai/auth/claude"),
                ("codex", "host", "expired", "~/.codex/auth.json"), ("opencode", "host", "missing", "")]

    def secrets(self):
        return "file", [("GH_PUSH", "file"), ("PYPI_TOKEN", "file")]

    def git_logins(self):
        return [{"url": "https://github.com", "user": "x-access-token", "secret": "GH_PUSH"},
                {"url": "http://localhost:3080", "user": "me", "secret": "GITEA_TOKEN"}]

    def publish_logins(self):
        return [{"kind": "pypi", "host": "upload.pypi.org", "secret": "PYPI_TOKEN"}]

    def has_identities(self):
        return False

    def vpn_profiles(self):
        return [{"profile": "home", "type": "wireguard", "provider": ""}]

    def valid_secret(self, name, value=None):
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name or ""))

    def call(self, *args, timeout=60, input=None):
        self.calls.append(args)
        return 0, "done", ""

    def command(self, *args):
        return ["sandbox-ai", *args]

    # another host (sandbox-ai hosts): its secrets over SSH, names only; a value on standard input there
    def remote_hosts(self):
        return ["ct1"]

    def host_secrets(self, host):
        self.calls.append(("host_secrets", host))
        return "keyring", [("LLM_KEY", "keyring")]

    def set_secret(self, name, value, host="local"):
        self.calls.append(("set", name, value, host))
        return 0, f"{name} kept", ""

    def delete_secret(self, name, host="local"):
        self.calls.append(("delete", name, host))
        return 0, f"{name} deleted", ""


@pytest.fixture
def page(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.credentials_page import CredentialsPage
    view = CredentialsPage(FakeSai(), lambda *a, **k: None)
    yield view
    view.deleteLater()


def cells(table, column=0):
    return [table.item(r, column).text() for r in range(table.rowCount())]


def test_the_overview_lists_everything_with_its_state(page, wait):
    page.refresh()
    page.vpn.refresh()
    assert wait(lambda: not widgets.busy(page) and not widgets.busy(page.vpn) and page.overview.rowCount() == 9)
    rows = {(page.overview.item(r, 0).text(), page.overview.item(r, 1).text()): page.overview.item(r, 3).text()
            for r in range(page.overview.rowCount())}
    assert rows[("Agent login", "claude")] == "logged in" and rows[("Agent login", "codex")] == "expired, log in again"
    assert rows[("Secret", "GH_PUSH")] == "kept"
    assert rows[("Git login", "http://localhost:3080")] == "secret GITEA_TOKEN missing"
    assert rows[("Git login", "https://github.com")] == "ready" and rows[("Publish login", "pypi")] == "ready"
    assert rows[("VPN profile", "home")] == "configured"
    assert page.summary.text() == "9 configured, 3 need attention."
    tabs = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert tabs == ["Overview", "Agent logins (3)", "Secrets (2)", "Git logins (2)", "Publish logins (1)", "VPN (1)"]
    used = dict(zip(cells(page.secrets), cells(page.secrets, 2)))
    assert used == {"GH_PUSH": "Git login https://github.com", "PYPI_TOKEN": "publish login pypi"}


def test_a_click_on_a_row_opens_its_tab_with_the_entry_selected(page, wait):
    page.refresh()
    assert wait(lambda: not widgets.busy(page) and page.overview.rowCount() >= 8)
    row = next(r for r in range(page.overview.rowCount()) if page.overview.item(r, 1).text() == "http://localhost:3080")
    assert page.open_row(row) == "git"
    assert page.tabs.currentWidget() is page.git and widgets.selected(page.git.list) == "http://localhost:3080"
    row = next(r for r in range(page.overview.rowCount()) if page.overview.item(r, 1).text() == "PYPI_TOKEN")
    page.open_row(row)
    assert page.tabs.currentWidget().property("credential_kind") == "secrets" and page.name.text() == "PYPI_TOKEN"


def test_git_and_publish_logins_go_through_their_commands(page, wait, monkeypatch):
    from sandbox_ai_app import credentials_page as C
    page.refresh()
    assert wait(lambda: not widgets.busy(page) and page.git.list.rowCount() == 2)

    def filled(dialog):
        if dialog.kind is not None:
            dialog.kind.setCurrentText("npm")
        else:
            dialog.edits["url"].setText("https://git.example.org")
            dialog.edits["user"].setText("ada")
        dialog.secret.setText("GH_PUSH")
        dialog.check_and_accept()
        return dialog.result()
    monkeypatch.setattr(C.LoginDialog, "exec", filled)
    page.git.add()
    assert wait(lambda: not widgets.busy(page.git) and not widgets.busy(page))
    page.publish.add()
    assert wait(lambda: not widgets.busy(page.publish) and not widgets.busy(page))
    page.git.list.selectRow(0)
    page.git.remove()
    assert wait(lambda: not widgets.busy(page.git))
    calls = page.sai.calls
    assert ("git-logins", "add", "https://git.example.org", "--secret", "GH_PUSH", "--user", "ada") in calls
    assert ("publish-logins", "add", "npm", "--secret", "GH_PUSH") in calls
    assert any(c[:2] == ("git-logins", "remove") for c in calls)


def test_a_login_needs_a_valid_secret_name(page):
    from sandbox_ai_app import credentials_page as C
    page.git.secret_names = ["GH_PUSH"]
    dialog = C.LoginDialog(page.git, "x", ["GH_PUSH"], (("Git server", "url", ""), ("User", "user", "")))
    dialog.edits["url"].setText("https://git.example.org")
    dialog.secret.setText("not a name")
    dialog.check_and_accept()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted and "letters, digits" in dialog.problem.text()
    dialog.deleteLater()


def test_deleting_a_secret_and_a_stored_login_ask_with_cancel_as_the_default(page, monkeypatch, wait):
    """Regression: both asked with QMessageBox.question, whose default is Yes: Enter deleted."""
    page.refresh()
    assert wait(lambda: not widgets.busy(page) and page.logins.rowCount() == 3)
    opened = []
    page.terminal = lambda argv, title: opened.append(argv)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    page.name.setText("PYPI_TOKEN")
    page.delete_secret()
    assert widgets.SHOWN[-1][:2] == ("Delete secret", "Delete the secret PYPI_TOKEN?")
    page.logins.selectRow(0)
    page.login(True)
    assert widgets.SHOWN[-1][0] == "Log out" and opened == []
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.login(True)
    assert opened and opened[-1][-1] == "--logout"


def test_the_secrets_of_another_host_and_a_replace_asked_first(page, monkeypatch, wait):
    """Review round 3 (A3, A8): a secret of the name typed had its value replaced without a word; another host's
    secrets could not be reached from the app."""
    page.refresh()
    assert wait(lambda: not widgets.busy(page) and page.secret_host.findData("ct1") >= 0)
    # This machine: a secret kept already is replaced only after a yes, a new one is kept at once.
    page.name.setText("GH_PUSH")
    page.value.setText("ghp-new")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    page.set_secret()
    assert widgets.SHOWN[-1][0] == "Secret" and "GH_PUSH is kept already" in widgets.SHOWN[-1][1]
    assert page.value.text() == "" and not [c for c in page.sai.calls if c[0] == "set"], "declined: nothing kept"
    count = len(widgets.SHOWN)
    page.name.setText("NEW_TOKEN")
    page.value.setText("tok")
    page.set_secret()
    assert wait(lambda: ("set", "NEW_TOKEN", "tok", "local") in page.sai.calls) and len(widgets.SHOWN) == count
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    # Another host: its own list, set and delete there.
    assert wait(lambda: not widgets.busy(page))
    page.secret_host.setCurrentIndex(page.secret_host.findData("ct1"))
    page.load_secrets()
    assert wait(lambda: not widgets.busy(page) and cells(page.secrets) == ["LLM_KEY"])
    assert page.backend_label.text().startswith("On ct1: ")
    widgets.SHOWN.clear()
    page.name.setText("LLM_KEY")
    page.value.setText("sk-new")
    page.set_secret()
    assert widgets.SHOWN[-1][1].startswith("On ct1: ") and "LLM_KEY is kept already" in widgets.SHOWN[-1][1]
    assert wait(lambda: ("set", "LLM_KEY", "sk-new", "ct1") in page.sai.calls)
    assert wait(lambda: not widgets.busy(page))
    page.delete_secret()
    assert widgets.SHOWN[-1][1] == "On ct1: Delete the secret LLM_KEY?"
    assert wait(lambda: ("delete", "LLM_KEY", "ct1") in page.sai.calls)
