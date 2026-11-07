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
