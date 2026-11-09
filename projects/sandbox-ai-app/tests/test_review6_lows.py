"""Review round 6 (UX), the low findings the release still had: the web logins of a host after a failed refresh, a
project file trusted already, the Kits page's buttons after a failed listing and its columns, the width of the Hosts
list, and two German wordings (the unnamed controls on later tabs: tests/test_accessibility.py walks every tab now).
Nothing here reads or writes the real HOME (conftest.py moves it; the window fixture's XDG directories are the test's)."""
import copy
import hashlib
import json
import time
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, help_content, hosts_page, i18n, kits_page, widgets  # noqa: E402
from sandbox_ai_app.host_trust import review_and_trust  # noqa: E402

from test_projects import env, window  # noqa: E402,F401,F811 - shared fixtures
from test_kits_marketplaces import (  # noqa: E402,F401 - the Kits page with a fake inventory and marketplaces
    INVENTORY, MARKETS, browser, choose_kit, pick, wait,
)


def wait_for(qapp, condition, timeout=10.0):
    from conftest import wait_until
    return wait_until(qapp, condition, timeout)


# -- the host's web interface: a failed refresh leaves no old logins on screen --

class WebSai:
    def __init__(self):
        self.answer = [("alice", "2026-11-01"), ("bob", "2026-11-02")]

    def host_web_users(self, host):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_failed_refresh_of_the_web_logins_clears_them_and_their_buttons(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    sai = WebSai()
    panel = hosts_page.WebPanel(sai, lambda argv, title: None)
    panel.set_host("ct1", load=True)
    assert wait_for(qapp, lambda: panel.users.rowCount() == 2 and not widgets.busy(panel))
    widgets.select_keys(panel.users, ["alice"])
    assert panel.remove_button.isEnabled() and panel.reset_button.isEnabled()
    sai.answer = OSError("ssh: connect to host ct1: Connection refused")
    panel.refresh()
    assert wait_for(qapp, lambda: not widgets.busy(panel) and "no answer" in panel.status.text())
    assert panel.users.rowCount() == 0, "the logins of before are not shown as if they were current"
    assert panel.chosen() is None
    assert not panel.remove_button.isEnabled() and not panel.reset_button.isEnabled()
    assert panel.refresh_button.isEnabled(), "Refresh asks again"
    sai.answer = [("alice", "2026-11-01")]
    panel.refresh()
    assert wait_for(qapp, lambda: panel.users.rowCount() == 1 and not widgets.busy(panel))
    panel.deleteLater()


# -- Review and trust… of a file trusted as it is: said, with its content --

class TrustSai:
    """review_project (this machine) and host_project_show (another host) of a file trusted as it is."""

    def __init__(self, path, document):
        self.path, self.document = path, document

    def review_project(self, directory):
        data = (json.dumps(self.document, indent=2) + "\n").encode()
        review = backend.ProjectReview(self.path, data, hashlib.sha256(data).hexdigest(), trusted=True,
                                       changed=False)
        review.document, review.agent = self.document, self.document["agent"]
        return review

    def host_project_show(self, host, path):
        return {"path": path, "trusted": True, "document": self.document, "digest": "0" * 64}


@pytest.mark.parametrize("host", ["local", "ct1"])
def test_review_and_trust_of_a_trusted_file_says_so(qapp, monkeypatch, host):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    path = "/home/u/src/project/.sandbox-ai.json"
    sai = TrustSai(path, {"version": 1, "agent": "claude", "options": ["--offline"]})
    parent = QtWidgets.QWidget()
    decided = []
    # the Settings' Review and trust… (say_trusted): a message naming the file, its content as the details
    review_and_trust(parent, sai, host, path, done=lambda trusted, document: decided.append(trusted),
                     say_trusted=True)
    assert wait_for(qapp, lambda: decided == [True])
    assert len(widgets.SHOWN) == 1
    _title, text, details = widgets.SHOWN[0]
    assert f"{path} is trusted as it is" in text
    assert '"--offline"' in details
    # the Start page's own message follows a trusted file: nothing said twice there
    widgets.SHOWN.clear()
    review_and_trust(parent, sai, host, path, done=lambda trusted, document: decided.append(trusted))
    assert wait_for(qapp, lambda: decided == [True, True])
    assert widgets.SHOWN == []
    parent.deleteLater()


def test_the_trusted_message_has_its_german_text():
    i18n.set_language("de")
    try:
        text = i18n.tr("{path} is trusted as it is: nothing to review.", path="/p/.sandbox-ai.json")
    finally:
        i18n.set_language("en")
    assert text.startswith("/p/.sandbox-ai.json ist so, wie sie ist, bereits vertrauenswürdig")


# -- the Kits page: buttons after a failed listing, and while the marketplaces are read --

def test_after_a_change_a_failed_kits_listing_still_sets_the_plugin_buttons(browser, monkeypatch):
    page = browser
    choose_kit(page, "user:mine")
    pick(page, "claude:team", "deploy@team")
    assert not page.enable_button.isEnabled() and page.disable_button.isEnabled(), "deploy is on in mine"
    # The change turns deploy off; the marketplaces read afterwards says so, the kits listing then fails (late: the
    # marketplaces' answer comes while the kits are still read).
    after = copy.deepcopy(MARKETS)
    team = next(m for m in after["marketplaces"] if m["name"] == "team")
    next(p for p in team["plugins"] if p["id"] == "deploy@team")["kits"] = [{"kit": "mine", "kind": "user",
                                                                           "enabled": False}]
    monkeypatch.setattr(page.sai, "marketplaces", lambda: copy.deepcopy(after))

    def failing():
        time.sleep(0.5)
        raise OSError("kits list: the kits folder cannot be read")
    monkeypatch.setattr(page.sai, "kits_inventory", failing)
    page.plugin_action("disable-plugin")
    assert page.wait(lambda: "Cannot list the kits" in page.summary.text() and not widgets.busy(page)
                     and not widgets.busy(page.market_split))
    assert page.calls[-1][:3] == ("disable-plugin", "mine", "deploy@team")
    # every button as the marketplaces read now say (before: as they were before the change)
    reasons = {**page.market_reasons(), **page.plugin_reasons()}
    assert {b.text(): b.isEnabled() for b in reasons} == {b.text(): why is None for b, why in reasons.items()}
    pick(page, "claude:team", "deploy@team")
    assert page.enable_button.isEnabled(), "it is off now: Enable"
    assert not page.disable_button.isEnabled() and "off in this kit already" in page.disable_button.toolTip()


def test_while_the_marketplaces_are_read_the_buttons_say_so(window, monkeypatch, wait):
    page = window.kits
    monkeypatch.setattr(window.sai, "has_kits_command", lambda: True)
    monkeypatch.setattr(window.sai, "has_marketplaces", lambda: True)
    monkeypatch.setattr(window.sai, "kits_inventory", lambda: copy.deepcopy(INVENTORY))

    def slow():
        time.sleep(0.6)
        return copy.deepcopy(MARKETS)
    monkeypatch.setattr(window.sai, "marketplaces", slow)
    page.refresh()
    assert wait(lambda: page.table.rowCount() == 3 and not widgets.busy(page))
    assert page.market_data is None and widgets.busy(page.market_split)
    assert "Reading the marketplaces of" in page.market_add_button.toolTip()
    assert "not read yet" not in page.market_add_button.toolTip()
    assert wait(lambda: page.market.rowCount() == 6 and not widgets.busy(page.market_split))
    pick(page, "claude:acme")
    assert page.market_add_button.isEnabled()


# -- the Kits table: names whole, the project column only when it says something --

def test_the_kits_table_keeps_the_names_whole_and_shows_the_project_column_only_with_a_project(browser, tmp_path):
    page = browser
    table, header = page.table, page.table.horizontalHeader()
    assert table.isColumnHidden(kits_page.PROJECT_COLUMN), "no project: the column says nothing"
    assert kits_page.PROJECT_COLUMN in table.fit_compact and 0 in table.fit_compact and 2 not in table.fit_compact
    metrics = table.fontMetrics()
    for row in range(table.rowCount()):
        name = table.item(row, 0).text()
        assert header.sectionSize(0) >= metrics.horizontalAdvance(name), f"{name} cut short"
    project = tmp_path / "project"
    project.mkdir()
    page.set_project(str(project))
    assert not table.isColumnHidden(kits_page.PROJECT_COLUMN)
    assert header.sectionSize(kits_page.PROJECT_COLUMN) >= metrics.horizontalAdvance("This project")
    page.set_project(None)
    assert table.isColumnHidden(kits_page.PROJECT_COLUMN)


# -- the Hosts list: a share of the page, not 255 px of 1280 --

@pytest.mark.parametrize("language", ["en", "de"])
def test_the_hosts_list_gets_a_fair_share_of_the_page(qapp, window, language):
    i18n.set_language(language)
    try:
        window.resize(1280, 800)
        window.show()
        window.show_page("hosts", raise_window=False)
        for _ in range(3):
            qapp.processEvents()
        split = window.hosts.split
        sizes = split.sizes()
        assert sizes[0] >= 0.4 * sum(sizes), sizes
        assert sizes[1] >= split.widget(1).minimumSizeHint().width(), "the tabs keep their minimum"
        assert not split.childrenCollapsible()
    finally:
        i18n.set_language("en")


# -- German: the glossary's words --

def test_the_german_wordings_of_review_6():
    texts = [entry.get("de") or "" for entry in i18n.TEXTS.values()]
    assert not any("ohne es aus" in text for text in texts)
    i18n.set_language("de")
    try:
        tip = i18n.tr("Remove the selected secret (asked first): every start, VPN profile and Git login that names it "
                      "goes without it")
        assert tip.endswith("der es nennt, bekommt es dann nicht mehr")
        # privacy is "Datenschutz" everywhere (the glossary), the presets' category too
        assert i18n.tr("Privacy & VPN") == "Datenschutz und VPN"
    finally:
        i18n.set_language("en")
    german = Path(help_content.__file__).read_text(encoding="utf-8")
    assert "Privatsphäre" not in german and not any("Privatsphäre" in text for text in texts)
    assert "Datenschutz und VPN, Teams und Richtlinien" in german
