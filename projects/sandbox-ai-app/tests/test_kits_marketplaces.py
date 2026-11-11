"""The marketplace browser of the Kits & plugins page (sandbox-ai plugins marketplaces --json, version 1): the UI flows
against a fake document with the kit commands recorded; a host of `sandbox-ai hosts` (a stand-in host_run that
answers from a table, as tests/test_host_pages.py has it), an older one told to update; every argv the page builds
read by the real parsers of sandbox-ai; and end to end against the real command in a throwaway HOME with Claude's,
Codex's and OpenCode's own configuration faked (sandbox-ai's tests/fake_agents.py): the agents' files are never
written. Nothing here reads or writes the real HOME (conftest.py moves it for the session, and the end-to-end test
moves it again)."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, kits_page, widgets  # noqa: E402

from test_projects import env, window  # noqa: E402,F401,F811 - shared fixtures


@pytest.fixture
def wait(qapp, window):
    """conftest's wait, and the Kits page's change (a Task: one at a time) has ended too: the next action may go."""
    from conftest import wait_until
    return lambda condition, timeout=10.0: wait_until(qapp, lambda: condition() and not window.kits.task.running,
                                                      timeout)

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

CODEX_ABOUT = ("sandbox-ai's kits give Codex no plugins yet: Codex's marketplaces and plugins are shown as Codex has "
               "them on this machine; one in Claude's format can be imported for Claude")
OPENCODE_ABOUT = "OpenCode has no marketplaces: its plugins are npm packages"


def market(name, agent="claude", **values):
    entry = {"name": name, "agent": agent, "builtin": False, "origins": [agent], "description": None, "source": None,
             "source_value": None, "importable": True, "problem": None, "import": None, "kits": [], "store": None,
             "agent_config": None, "listed_from": agent, "plugins": []}
    entry.update(values)
    return entry


def plugin(ident, kits=(), agent=None, **values):
    name = ident.split("@")[0]
    entry = {"name": name, "id": ident, "listed": True, "description": None, "version": None, "category": None,
             "homepage": None, "source": "local", "kits": list(kits), "store": {"installed": False, "version": None},
             "agent": agent or {"installed": False, "enabled": None}}
    entry.update(values)
    return entry


def on(kit, kind="user", enabled=True):
    return {"kit": kit, "kind": kind, "enabled": enabled}


MARKETS = {
    "version": 1, "store": "/home/u/.local/share/sandbox-ai/plugins/claude",
    "kits": [{"name": "default", "kind": "default", "editable": True, "agents": ["claude"]},
             {"name": "egm", "kind": "builtin", "editable": False, "agents": ["claude"]},
             {"name": "mine", "kind": "user", "editable": True, "agents": ["claude", "opencode"]}],
    "agents": {
        "claude": {"marketplaces": True, "kits": True, "config": "/home/u/.claude", "about": None,
                   "notes": ["settings.json: not JSON (line 3)"]},
        "codex": {"marketplaces": True, "kits": False, "config": "/home/u/.codex/config.toml", "about": CODEX_ABOUT,
                  "notes": []},
        "opencode": {"marketplaces": False, "kits": True, "config": "/home/u/.config/opencode/opencode.jsonc",
                     "about": OPENCODE_ABOUT, "notes": [],
                     "plugins": [{"name": "opencode-foo@1.0", "id": "opencode-foo@1.0", "importable": True, "kits": [],
                                  "agent": {"configured": True}},
                                 {"name": "opencode-bar", "id": "opencode-bar", "importable": True,
                                  "kits": [on("mine")], "agent": {"configured": False}}],
                     "modules": ["/home/u/.config/opencode/plugin/guard.js"]}},
    "marketplaces": [
        market("sandbox-ai", builtin=True, origins=["sandbox-ai", "kit"], source_value="sandbox-ai",
               listed_from="sandbox-ai", kits=[{"kit": "egm", "kind": "builtin", "source": "sandbox-ai"}],
               plugins=[plugin("egm@sandbox-ai", [on("egm", "builtin")], description="Elephant-Goldfish Model"),
                        plugin("sandcastle@sandbox-ai", [on("default", "default")], version="1.0.0",
                               store={"installed": True, "version": "1.0.0"})]),
        market("acme", description="Acme plugins", source_value="acme/plugins", listed_from="claude",
               agent_config={"known": True, "settings": False, "fetched": True},
               plugins=[plugin("lint@acme", agent={"installed": True, "enabled": True}, description="Lints",
                               version="1.2"),
                        plugin("fmt@acme", agent={"installed": False, "enabled": False}, description="Formats")]),
        market("team", origins=["kit", "claude"], source_value="https://git.example.org/team/plugins.git",
               listed_from=None, kits=[{"kit": "mine", "kind": "user", "source": "https://git.example.org/team"}],
               plugins=[plugin("deploy@team", [on("mine")], listed=False),
                        plugin("audit@team", [on("mine", enabled=False)], listed=False)]),
        market("localdir", source_value="/opt/plugins", importable=False, listed_from=None,
               problem="it is a folder or file on this machine; sandboxes get only sandbox-ai's own marketplace as a "
                       "folder: publish it on a Git server or GitHub"),
        market("cx", "codex", codex_source={"source_type": "git"}, source_value="https://x/cx.git",
               plugins=[plugin("tool@cx", agent={"installed": True, "enabled": True})],
               **{"import": {"name": "cx-claude", "source": {"source": "git"}, "source_value": "https://x/cx.git"}}),
    ],
    "errors": [{"kit": "broken", "agent": "claude", "error": "marketplaces.json: not JSON"}],
}
INVENTORY = {"version": 1, "kits_dir": "/home/u/.local/share/sandbox-ai/kits", "store": "/s", "installed": {},
             "kits": [{"name": "default", "kind": "default", "path": "/home/u/.local/share/sandbox-ai/home",
                       "agents": {"claude": {"skills": ["example"], "plugins": ["sandcastle@sandbox-ai"],
                                             "disabled": ["fmt@acme"]}}},
                      {"name": "egm", "kind": "builtin", "path": "/sai/kits/egm", "agents": {"claude": {}}},
                      {"name": "mine", "kind": "user", "path": "/home/u/.local/share/sandbox-ai/kits/mine",
                       "agents": {"claude": {"plugins": ["deploy@team"], "disabled": ["audit@team"]}}}]}


@pytest.fixture
def browser(window, monkeypatch, wait):
    """The window's Kits page with the fake inventory and marketplaces read; kit commands are recorded."""
    calls = []
    monkeypatch.setattr(window.sai, "has_kits_command", lambda: True)
    monkeypatch.setattr(window.sai, "has_marketplaces", lambda: True)
    monkeypatch.setattr(window.sai, "kits_inventory", lambda: copy.deepcopy(INVENTORY))
    monkeypatch.setattr(window.sai, "marketplaces", lambda: copy.deepcopy(MARKETS))
    monkeypatch.setattr(window.sai, "kit_command",
                        lambda action, name, *args, options=(): calls.append((action, name, *args, *options)) or
                        (0, "", ""))
    page = window.kits
    page.refresh()
    assert wait(lambda: page.market.rowCount() == 6 and page.table.rowCount() == 3 and not widgets.busy(page)
                and not widgets.busy(page.market_split), 10)
    page.calls, page.window_ = calls, window
    page.wait = wait
    return page


def keys(view):
    return [widgets.row_key(view, r) for r in range(view.rowCount())]


def cells(view, key):
    row = widgets.find_row(view, key)
    return [view.item(row, c).text() for c in range(view.columnCount())]


def pick(page, key, plugin_id=None):
    page.market.clearSelection()
    widgets.select_keys(page.market, [key])
    if plugin_id is not None:
        widgets.select_keys(page.plugins, [plugin_id])


def choose_kit(page, key):
    page.market_kit.setCurrentIndex(page.market_kit.findData(key))


def act(page, function, *args):
    """FUNCTION(*ARGS) of the page, then until its kit command ran and the page read everything again."""
    before = len(page.calls)
    function(*args)
    assert page.wait(lambda: len(page.calls) > before and not widgets.busy(page)
                     and not widgets.busy(page.market_split) and page.market.rowCount(), 10)
    return page.calls[-1]


# -- what the browser shows --


def test_the_browser_lists_kits_marketplaces_and_every_state(browser):
    page = browser
    combo = [page.market_kit.itemText(i) for i in range(page.market_kit.count())]
    assert combo == ["the default kit (every sandbox)", "egm (built-in, read-only)", "mine"]
    assert page.market_kit.currentData() == "user:mine", "a kit of yours first"
    assert keys(page.market) == ["claude:sandbox-ai", "claude:acme", "claude:team", "claude:localdir", "codex:cx",
                                 kits_page.OPENCODE]
    # review 6: the count is the marketplaces', the OpenCode row named apart
    assert page.market_status.text().startswith("5 marketplaces on ")
    assert page.market_status.text().endswith(", and OpenCode's npm plugins")
    team = cells(page.market, "claude:team")
    assert "declared in mine" in team[2] and team[3] == "2 (list not fetched)"
    assert cells(page.market, "claude:acme")[2] == "Claude on that host" and cells(page.market, "claude:acme")[3] == "2"
    assert "not for a kit: it is a folder" in page.market.item(widgets.find_row(page.market, "claude:localdir"),
                                                               0).toolTip()
    # each agent's problems and the kits' errors, inline
    assert "! claude: settings.json: not JSON (line 3)" in page.agent_notes.text()
    assert "✘ kit broken (claude): marketplaces.json: not JSON" in page.agent_notes.text()
    pick(page, "claude:acme")
    assert page.plugins_title.text() == "Plugins of acme (claude)"
    lint = cells(page.plugins, "lint@acme")
    assert lint[0] == "lint 1.2" and lint[1] == "–"
    assert lint[2] == "the agent has it installed · the agent enables it" and lint[3] == "Lints"
    assert cells(page.plugins, "fmt@acme")[2] == "the agent has it off"
    page.filter.setText("FORMAT")
    assert keys(page.plugins) == ["fmt@acme"], "the filter: name or description, any case"
    page.filter.clear()
    pick(page, "claude:sandbox-ai")
    assert cells(page.plugins, "sandcastle@sandbox-ai")[2] == "on in default · installed (1.0.0)"
    assert cells(page.plugins, "egm@sandbox-ai")[2] == "on in egm"
    pick(page, "claude:team")
    assert cells(page.plugins, "deploy@team")[1] == "on" and cells(page.plugins, "audit@team")[1] == "off"
    assert "not fetched yet" in page.plugins_note.text()
    assert cells(page.plugins, "deploy@team")[3] == "not in its plugin list (named by a kit or the agent)"
    # Codex: its about said, its plugins shown as Codex has them, nothing to change
    pick(page, "codex:cx", "tool@cx")
    assert CODEX_ABOUT in page.plugins_note.text()
    assert not any(b.isEnabled() for b in page.plugin_buttons)
    assert "give Codex no plugins yet" in page.plugin_add_button.toolTip()
    # OpenCode: npm plugins, its about and its modules
    pick(page, kits_page.OPENCODE)
    assert page.plugins_title.text() == "OpenCode's npm plugins"
    assert OPENCODE_ABOUT in page.plugins_note.text() and "guard.js" in page.plugins_note.text()
    assert cells(page.plugins, "opencode-foo@1.0")[2] == "in opencode.json"
    assert cells(page.plugins, "opencode-bar")[1] == "on"


def test_a_read_only_kit_is_marked_and_changes_nothing(browser):
    page = browser
    choose_kit(page, "builtin:egm")
    pick(page, "claude:acme", "lint@acme")
    buttons = page.plugin_buttons + [page.market_add_button, page.import_button, page.market_remove_button,
                                     page.market_new_button]
    assert not any(b.isEnabled() for b in buttons)
    assert all("read-only" in b.toolTip() for b in buttons)
    page.plugin_action("add-plugin")
    page.add_market()
    assert page.calls == [], "nothing runs for a read-only kit"


def test_the_kits_table_moves_the_browsers_kit_and_shows_disabled_plugins(browser, monkeypatch):
    page = browser
    page.table.selectRow(widgets.find_row(page.table, "default:default"))
    assert page.market_kit.currentData() == "default:default"
    row = widgets.find_row(page.table, "default:default")
    assert "off: fmt@acme" in page.table.item(row, 3).text()
    claude = page.tree.topLevelItem(0)
    plugins = next(claude.child(i) for i in range(claude.childCount()) if claude.child(i).text(0).startswith("Plugins"))
    assert [plugins.child(i).text(0) for i in range(plugins.childCount())] == [
        "sandcastle@sandbox-ai  (not installed)", "fmt@acme  (off)"]
    seen = {}

    class Remove:
        def __init__(self, parent, entries):
            seen["entries"] = entries

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def chosen(self):
            return ("plugin", "claude", "fmt@acme")
    monkeypatch.setattr(kits_page, "RemoveDialog", Remove)
    assert act(page, page.remove) == ("remove-plugin", "default", "fmt@acme", "--agent=claude")
    assert seen["entries"] == [("plugin", "claude", "sandcastle@sandbox-ai"), ("plugin", "claude", "fmt@acme")], \
        "of the default kit only its plugins (its skills are edited in its folder)"
    assert "fmt@acme" in widgets.SHOWN[-1][1] and "On this computer" in widgets.SHOWN[-1][1]


# -- what it changes: every change asked first, naming the host and the kit --


def test_plugins_are_added_enabled_disabled_and_removed(browser, monkeypatch):
    page = browser
    pick(page, "claude:acme", "lint@acme")
    assert page.plugin_add_button.isEnabled() and not page.enable_button.isEnabled()
    assert not page.plugin_remove_button.isEnabled() and page.plugin_remove_button.toolTip() == widgets.safe_tooltip("Not in this kit.")
    # Cancel: nothing runs
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.plugin_action("add-plugin")
    assert page.calls == [] and widgets.SHOWN[-1][0] == "Plugin added to mine"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert act(page, page.plugin_action, "add-plugin") == (
        "add-plugin", "mine", "lint@acme", "--agent=claude", "--source=acme/plugins"), \
        "a marketplace the kit does not declare yet comes with its source"
    title, text, _ = widgets.SHOWN[-1]
    assert text == "Add lint@acme to the kit mine on local (this machine)? Every later start with that kit gets it."
    # a plugin on in the kit: disable (it stays), remove; one off: enable
    pick(page, "claude:team", "deploy@team")
    assert not page.plugin_add_button.isEnabled() and page.plugin_add_button.toolTip() == widgets.safe_tooltip("It is in the kit already.")
    assert not page.enable_button.isEnabled()
    assert act(page, page.plugin_action, "disable-plugin") == ("disable-plugin", "mine", "deploy@team",
                                                               "--agent=claude")
    assert "It stays in the kit" in widgets.SHOWN[-1][1] and "mine on local" in widgets.SHOWN[-1][1]
    pick(page, "claude:team", "audit@team")
    assert act(page, page.plugin_action, "enable-plugin") == ("enable-plugin", "mine", "audit@team", "--agent=claude")
    pick(page, "claude:team", "audit@team")
    assert act(page, page.plugin_action, "remove-plugin") == ("remove-plugin", "mine", "audit@team", "--agent=claude")
    assert widgets.SHOWN[-1][1] == "Remove the plugin audit@team for claude from the kit mine on local (this machine)?"
    # the default kit enables sandcastle: a kit of yours can turn it off
    pick(page, "claude:sandbox-ai", "sandcastle@sandbox-ai")
    assert act(page, page.plugin_action, "disable-plugin") == ("disable-plugin", "mine", "sandcastle@sandbox-ai",
                                                               "--agent=claude")
    assert "though the default kit enables it" in widgets.SHOWN[-1][1]
    # the default kit itself
    choose_kit(page, "default:default")
    pick(page, "claude:sandbox-ai", "sandcastle@sandbox-ai")
    assert act(page, page.plugin_action, "disable-plugin") == ("disable-plugin", "default", "sandcastle@sandbox-ai",
                                                               "--agent=claude")
    assert "the default kit on local" in widgets.SHOWN[-1][1]
    pick(page, "claude:sandbox-ai", "egm@sandbox-ai")
    assert act(page, page.plugin_action, "add-plugin") == ("add-plugin", "default", "egm@sandbox-ai", "--agent=claude",
                                                           "--source=sandbox-ai")
    assert page.market_kit.currentData() == "default:default", "the kit chosen stays after the page read again"


def test_opencodes_npm_plugins_switch_the_same_way(browser):
    page = browser
    pick(page, kits_page.OPENCODE, "opencode-foo@1.0")
    assert not page.market_add_button.isEnabled() and "OpenCode has no marketplaces" in page.market_add_button.toolTip()
    assert act(page, page.plugin_action, "add-plugin") == ("add-plugin", "mine", "opencode-foo@1.0",
                                                           "--agent=opencode")
    pick(page, kits_page.OPENCODE, "opencode-bar")
    assert act(page, page.plugin_action, "disable-plugin") == ("disable-plugin", "mine", "opencode-bar",
                                                               "--agent=opencode")
    pick(page, kits_page.OPENCODE, "opencode-bar")
    assert act(page, page.plugin_action, "remove-plugin") == ("remove-plugin", "mine", "opencode-bar",
                                                              "--agent=opencode")


def test_marketplaces_are_added_imported_and_removed(browser, monkeypatch):
    page = browser
    pick(page, "claude:acme")
    assert not page.market_remove_button.isEnabled() and "does not declare" in page.market_remove_button.toolTip()
    assert act(page, page.add_market) == ("add-marketplace", "mine", "acme", "acme/plugins", "--agent=claude")
    assert widgets.SHOWN[-1][1] == ("Declare the marketplace acme (acme/plugins) in mine on local (this machine)? Its "
                                    "plugins can then be added there.")
    pick(page, "claude:acme")
    assert page.import_button.text() == "Import with Claude's plugins"
    assert act(page, page.import_market) == ("import-marketplace", "mine", "acme", "--from=claude", "--with-plugins")
    # declared in mine: removed, with its plugins or without
    pick(page, "claude:team")
    assert not page.market_add_button.isEnabled() and page.market_add_button.toolTip() == widgets.safe_tooltip("The kit declares it already.")
    assert page.with_plugins.isChecked() and page.with_plugins.isEnabled()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.remove_market()
    assert page.calls[-1][0] != "remove-marketplace", "Cancel: nothing taken out"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert act(page, page.remove_market) == ("remove-marketplace", "mine", "team", "--agent=claude", "--with-plugins")
    assert "with its plugins out of mine on local" in widgets.SHOWN[-1][1]
    pick(page, "claude:team")
    page.with_plugins.setChecked(False)
    assert act(page, page.remove_market) == ("remove-marketplace", "mine", "team", "--agent=claude")
    assert "must be removed first" in widgets.SHOWN[-1][1]
    # a folder marketplace cannot go into a kit: said why
    pick(page, "claude:localdir")
    assert not page.market_add_button.isEnabled() and not page.import_button.isEnabled()
    assert "not for a kit: it is a folder" in page.market_add_button.toolTip()
    # Codex's, in Claude's format: imported for Claude
    pick(page, "codex:cx")
    assert page.import_button.text() == "Import for Claude" and not page.market_add_button.isEnabled()
    assert act(page, page.import_market) == ("import-marketplace", "mine", "cx", "--from=codex")
    assert "for Claude, as cx-claude, in mine on local" in widgets.SHOWN[-1][1]
    # one by its source, into the kit chosen

    class Dialog:
        def __init__(self, parent):
            self.name, self.source = QtWidgets.QLineEdit("team2"), QtWidgets.QLineEdit("https://git.example.org/p.git")

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(kits_page, "MarketplaceDialog", Dialog)
    choose_kit(page, "default:default")
    assert act(page, page.new_marketplace) == ("add-marketplace", "default", "team2", "https://git.example.org/p.git")


def test_the_plugin_lists_are_fetched_in_the_background_installing_nothing(browser, monkeypatch):
    page = browser
    assert "installs nothing" in page.fetch_button.toolTip()
    ran = []
    call = page.sai.call
    monkeypatch.setattr(page.sai, "call", lambda *args, **kw: (ran.append(args) or (0, "", ""))
                        if args[:2] == ("plugins", "claude") else call(*args, **kw))
    started = len(page.window_.started)
    page.fetch_lists()
    assert "nothing is installed" in page.market_status.text()
    assert page.wait(lambda: ran and not page.task.running, 10)
    assert ran == [("plugins", "claude", "--marketplaces-only")] and len(page.window_.started) == started
    assert page.calls == []


def test_a_late_answer_is_dropped_and_an_old_sandbox_ai_said(browser, monkeypatch):
    page = browser
    other = copy.deepcopy(MARKETS)
    other["marketplaces"] = other["marketplaces"][:1]
    page.markets_loaded(page.market_token - 1, "local", other, None)
    page.markets_loaded(page.market_token, "ct9", other, None)
    assert page.market.rowCount() == 6, "an answer asked before, or for another host, is never shown"
    monkeypatch.setattr(page.sai, "has_marketplaces", lambda: False)
    page.refresh()
    assert page.wait(lambda: not widgets.busy(page), 10)
    assert "sandbox-ai update" in page.market_status.text() and page.market.rowCount() == 0
    assert not page.fetch_button.isEnabled() and page.table.rowCount() == 3, "the kits are shown as before"


# -- on a host of sandbox-ai hosts: the same, over SSH; an older host is told to update --


@pytest.fixture
def remote(env, monkeypatch):
    """A SandboxAI with the host ct1; host_run records what it was asked and answers from sai.answers (its kit
    commands too), background calls run at once."""
    def synchronous(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001 - as ThreadCall hands it on
            done(None, exc)
            return None
        done(result, None)
    monkeypatch.setattr(kits_page, "background", synchronous)
    monkeypatch.setattr(kits_page.Task, "call_watched", lambda task, sai, function, done, *a, **k: synchronous(
        function, done=done) or True)  # a kit change too
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    sai.asked, sai.answers = [], {}
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}])
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {"ct1": {"version": "0.1.78"}})
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, "sandbox-ai", *args])

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, args))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    sai.answers[("ct1", ("kits", "list", "--json"))] = (0, json.dumps(INVENTORY), "")
    sai.answers[("ct1", ("plugins", "marketplaces", "--json"))] = (0, json.dumps(MARKETS), "")
    sai.answers[("ct1", ("plugins", "--help"))] = (0, "  --kit NAME\n  --marketplaces-only  fetch\n", "")
    return sai


def on_ct1(sai, qapp):
    opened = []
    page = kits_page.KitsPage(sai, lambda argv, title, **kw: opened.append((argv, title)))
    page.opened = opened
    page.hosts.set_host("ct1")
    page.refresh()
    qapp.processEvents()
    return page


def test_the_browser_on_a_host(qapp, remote):
    sai = remote
    page = on_ct1(sai, qapp)
    asked = [args for host, args in sai.asked if host == "ct1"]
    assert ("plugins", "marketplaces", "--json") in asked and page.market.rowCount() == 6
    assert "on ct1" in page.market_hint.text()
    pick(page, "claude:team", "audit@team")
    page.plugin_action("enable-plugin")
    assert ("ct1", ("kits", "enable-plugin", "--agent=claude", "--", "mine", "audit@team")) in sai.asked
    assert widgets.SHOWN[-1][1] == "Turn audit@team on in every start with mine on ct1?"
    choose_kit(page, "default:default")
    pick(page, "claude:acme", "fmt@acme")
    page.plugin_action("add-plugin")
    assert sai.asked[-3][1] == ("kits", "add-plugin", "--agent=claude", "--source=acme/plugins", "--", "default",
                                "fmt@acme"), "then the kits and marketplaces read again"
    page.fetch_lists()
    assert sai.asked[-3] == ("ct1", ("plugins", "claude", "--marketplaces-only")), "there, in the background"
    assert page.opened == [], "no terminal"
    page.deleteLater()


def test_an_older_host_is_told_to_update_and_its_kits_work_as_before(qapp, remote):
    sai = remote
    sai.answers[("ct1", ("plugins", "--help"))] = (0, "  --kit NAME\n  --update\n", "")  # sandbox-ai 0.1.77
    page = on_ct1(sai, qapp)
    asked = [args for host, args in sai.asked if host == "ct1"]
    assert ("plugins", "marketplaces", "--json") not in asked, "said, not sent"
    assert "Hosts › Update" in page.market_status.text() and page.market.rowCount() == 0
    assert page.table.rowCount() == 3, "the kits of that host are shown as before"
    assert not page.fetch_button.isEnabled() and "Hosts › Update" in page.fetch_button.toolTip()
    assert not page.plugin_add_button.isEnabled() and "Hosts › Update" in page.plugin_add_button.toolTip()
    # what came with 0.1.78 is refused before it is sent; what an older host knows goes as before
    status, _, err = sai.host_kit_command("ct1", "add-plugin", "default", "x@acme", options=["--agent=claude"])
    assert status == 2 and "Hosts › Update" in err
    status, _, err = sai.host_kit_command("ct1", "disable-plugin", "mine", "x@acme")
    assert status == 2 and "--marketplaces-only" in err
    assert sai.host_kit_command("ct1", "add-plugin", "mine", "x@acme")[0] == 0
    sent = [args for host, args in sai.asked if host == "ct1" and args[:1] == ("kits",)]
    assert ("kits", "add-plugin", "--", "mine", "x@acme") in sent
    assert not [a for a in sent if a[1] in ("disable-plugin",) or "default" in a], "nothing of 0.1.78 sent"
    page.deleteLater()


# -- every argv the page builds, read by the real parsers --


def test_every_argv_the_page_builds_is_what_the_real_parsers_read(window, monkeypatch, wait):
    sai = window.sai
    if not sai.has_marketplaces():
        pytest.skip("this sandbox-ai has no marketplace browser")
    sent = []

    def call(*args, timeout=120, input=None):
        if args[:1] == ("kits",) and args[1] not in ("list",):
            sent.append(args[1:])
            return 0, "", ""
        if args[:2] == ("plugins", "claude"):
            sent.append(args)
            return 0, "", ""
        return backend.SandboxAI.call(sai, *args, timeout=timeout, input=input)
    monkeypatch.setattr(sai, "call", call)
    monkeypatch.setattr(sai, "kits_inventory", lambda: copy.deepcopy(INVENTORY))
    monkeypatch.setattr(sai, "marketplaces", lambda: copy.deepcopy(MARKETS))
    page = window.kits
    page.refresh()
    assert wait(lambda: page.market.rowCount() == 6 and not widgets.busy(page), 10)

    def run(function, *args):
        before = len(sent)
        function(*args)
        assert wait(lambda: len(sent) > before and not widgets.busy(page) and not widgets.busy(page.market_split)
                    and page.market.rowCount(), 10)
        return list(sent[-1])
    kitctl = sai.module("kitctl")
    parse = kitctl.parser().parse_args
    pick(page, "claude:acme", "lint@acme")
    a = parse(run(page.plugin_action, "add-plugin"))
    assert (a.command, a.name, a.plugin, a.agent, a.source) == ("add-plugin", "mine", "lint@acme", "claude",
                                                                 "acme/plugins")
    pick(page, "claude:team", "deploy@team")
    a = parse(run(page.plugin_action, "disable-plugin"))
    assert (a.command, a.name, a.plugin, a.agent) == ("disable-plugin", "mine", "deploy@team", "claude")
    pick(page, "claude:team", "audit@team")
    a = parse(run(page.plugin_action, "enable-plugin"))
    assert (a.command, a.name, a.plugin, a.agent) == ("enable-plugin", "mine", "audit@team", "claude")
    pick(page, "claude:team", "audit@team")
    a = parse(run(page.plugin_action, "remove-plugin"))
    assert (a.command, a.name, a.plugin, a.agent) == ("remove-plugin", "mine", "audit@team", "claude")
    pick(page, "claude:team")
    a = parse(run(page.remove_market))
    assert (a.command, a.name, a.marketplace, a.with_plugins) == ("remove-marketplace", "mine", "team", True)
    pick(page, "claude:acme")
    a = parse(run(page.add_market))
    assert (a.command, a.name, a.marketplace, a.source, a.agent) == ("add-marketplace", "mine", "acme",
                                                                     "acme/plugins", "claude")
    pick(page, "claude:acme")
    a = parse(run(page.import_market))
    assert (a.command, a.name, a.marketplace, a.from_agent, a.with_plugins) == ("import-marketplace", "mine", "acme",
                                                                                "claude", True)
    pick(page, "codex:cx")
    a = parse(run(page.import_market))
    assert (a.command, a.marketplace, a.from_agent, a.with_plugins) == ("import-marketplace", "cx", "codex", False)
    choose_kit(page, "default:default")
    pick(page, kits_page.OPENCODE, "opencode-foo@1.0")
    a = parse(run(page.plugin_action, "add-plugin"))
    assert (a.command, a.name, a.plugin, a.agent) == ("add-plugin", "default", "opencode-foo@1.0", "opencode")
    # the background's command: plugins claude --marketplaces-only, as plugins.cli reads it
    before = len(sent)
    page.fetch_lists()
    assert wait(lambda: len(sent) > before and not page.task.running, 10)
    argv = sent[-1][1:]
    seen = []
    original = argparse.ArgumentParser.parse_known_args

    class Parsed(Exception):
        pass

    def capture(self, args=None, namespace=None):
        seen.append(original(self, args, namespace)[0])
        raise Parsed
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_known_args", capture)
        with pytest.raises(Parsed):
            sai.module("plugins").cli(list(argv))
    assert seen[0].agent == "claude" and seen[0].marketplaces_only and not seen[0].update
    listing = sai.module("plugins").marketplaces_parser().parse_args(["--json"])
    assert listing.json and listing.name is None


# -- end to end: the real sandbox-ai in a throwaway HOME with the agents' own configuration faked --


def fake_agents_module():
    """sandbox-ai's tests/fake_agents.py (the same fake configuration its own tests and the web's use)."""
    path = Path(backend.SandboxAI().directory).parent / "tests" / "fake_agents.py"
    if not path.is_file():
        pytest.skip("sandbox-ai's tests/fake_agents.py is not there")
    spec = importlib.util.spec_from_file_location("sandbox_ai_fake_agents", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fingerprint(home):
    """{path: sha256} of every file of the agents' own configuration below HOME."""
    found = {}
    for top in (".claude", ".codex", ".config/opencode"):
        for path in sorted((home / top).rglob("*")):
            if path.is_file():
                found[str(path.relative_to(home))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


@pytest.fixture
def fake_home(window, tmp_path, monkeypatch):
    """HOME (and the XDG folders) moved below this test's own folder, with Claude's, Codex's and OpenCode's
    configuration faked there; the agents' own variables dropped."""
    home = fake_agents_module().fake_agents(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    assert str(home).startswith(str(tmp_path)) and os.environ["HOME"] == str(home)
    return home


def test_the_browser_end_to_end_with_sandbox_ai(window, fake_home, monkeypatch, wait):
    sai = window.sai
    if not sai.has_marketplaces():
        pytest.skip("this sandbox-ai has no marketplace browser")
    before = fingerprint(fake_home)
    assert ".claude/settings.json" in before and ".codex/config.toml" in before
    document = sai.marketplaces()
    found = {f"{m['agent']}:{m['name']}": m for m in document["marketplaces"]}
    assert {"claude:sandbox-ai", "claude:acme", "claude:team", "claude:localdir", "codex:cx"} <= set(found)
    assert found["codex:cx"]["import"]["name"] == "cx-claude" and not found["claude:localdir"]["importable"]
    assert [p["name"] for p in document["agents"]["opencode"]["plugins"]] == ["opencode-foo@1.0"]
    assert sai.kit_command("new", "mine", options=["--agent=claude", "--agent=opencode"])[0] == 0
    page = window.kits
    page.refresh()
    assert wait(lambda: page.market_kit.findData("user:mine") >= 0 and not widgets.busy(page), 60)
    assert page.market_kit.currentData() == "user:mine"
    assert widgets.find_row(page.market, "claude:acme") >= 0 and widgets.find_row(page.market, "codex:cx") >= 0

    def this_kit(market_key, plugin_id):
        pick(page, market_key)
        row = widgets.find_row(page.plugins, plugin_id)
        return page.plugins.item(row, 1).text() if row >= 0 else None

    def settle(function, *args):
        function(*args)
        assert wait(lambda: not widgets.busy(page) and not widgets.busy(page.market_split), 60)
        assert not [s for s in widgets.SHOWN if "Failed" in s[1] or "error" in s[1].lower()], widgets.SHOWN
    # Claude's marketplace imported with the plugins Claude enables (lint on, fmt off there)
    pick(page, "claude:acme")
    settle(page.import_market)
    assert wait(lambda: this_kit("claude:acme", "lint@acme") == "on", 60)
    assert this_kit("claude:acme", "fmt@acme") == "–"
    assert "declared in mine" in cells(page.market, "claude:acme")[2]
    # off, on again, out
    pick(page, "claude:acme", "lint@acme")
    settle(page.plugin_action, "disable-plugin")
    assert wait(lambda: this_kit("claude:acme", "lint@acme") == "off", 60)
    pick(page, "claude:acme", "lint@acme")
    settle(page.plugin_action, "enable-plugin")
    assert wait(lambda: this_kit("claude:acme", "lint@acme") == "on", 60)
    pick(page, "claude:acme", "fmt@acme")
    settle(page.plugin_action, "add-plugin")
    assert wait(lambda: this_kit("claude:acme", "fmt@acme") == "on", 60)
    # the default kit: a plugin of sandbox-ai's own marketplace, then a kit of yours turns it off
    choose_kit(page, "default:default")
    pick(page, "claude:sandbox-ai", "sandcastle@sandbox-ai")
    settle(page.plugin_action, "add-plugin")
    assert wait(lambda: this_kit("claude:sandbox-ai", "sandcastle@sandbox-ai") == "on", 60)
    choose_kit(page, "user:mine")
    pick(page, "claude:sandbox-ai", "sandcastle@sandbox-ai")
    assert page.disable_button.isEnabled(), "off in mine though the default kit enables it"
    settle(page.plugin_action, "disable-plugin")
    assert wait(lambda: this_kit("claude:sandbox-ai", "sandcastle@sandbox-ai") == "off", 60)
    # OpenCode's npm plugin into the kit
    pick(page, kits_page.OPENCODE, "opencode-foo@1.0")
    settle(page.plugin_action, "add-plugin")
    assert wait(lambda: this_kit(kits_page.OPENCODE, "opencode-foo@1.0") == "on", 60)
    # Codex's marketplace imported for Claude, then acme taken out with its plugins
    pick(page, "codex:cx")
    settle(page.import_market)
    pick(page, "claude:acme")
    page.with_plugins.setChecked(True)
    settle(page.remove_market)
    assert wait(lambda: this_kit("claude:acme", "lint@acme") == "–" and this_kit("claude:acme", "fmt@acme") == "–",
                60)
    final = sai.marketplaces()
    names = {f"{m['agent']}:{m['name']}": m for m in final["marketplaces"]}
    assert not any(k["kit"] == "mine" for k in names["claude:acme"]["kits"])
    assert any(k["kit"] == "mine" for m in final["marketplaces"] if m["agent"] == "claude"
               and m["name"] == "cx-claude" for k in m["kits"]), "Codex's marketplace, for Claude, as cx-claude"
    # a folder marketplace is refused by sandbox-ai with its reason
    status, out, err = sai.kit_command("add-marketplace", "mine", "localdir", "/opt/plugins")
    assert status != 0 and "only sandbox-ai's own marketplace as a directory" in out + err
    assert fingerprint(fake_home) == before, "the agents' own configuration is only read"
    assert sai.kit_command("delete", "mine", options=["--yes"])[0] == 0
