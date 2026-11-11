"""What belongs to a start is shown with it (user report: MCP servers started with an agent stood on their own in the
list, as if they belonged to no agent), and a start opens one tab, not one per part (user report: every MCP server
of a start opened a tab of its own). start_tree.owners decides; the Sandboxes page, the Terminals page's Sessions
and the tabs follow it. Offscreen, against fake records."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, start_tree, widgets  # noqa: E402
from sandbox_ai_app.backend import Instance  # noqa: E402

from conftest import read_now  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - shared fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def session(name, started="2026-11-10T10:00:00Z", host="local", **record):
    key = f"session:{name}" if host == "local" else backend.remote_key(host, name)
    return Instance("session", key, name, run=f"claude-{name}" if host == "local" else None, session=name,
                    agent="claude", started=started, state="running", host=host,
                    record={"run": f"claude-{name}", **record})


def server(name, started="2026-11-10T10:00:00Z", host="local", **record):
    key = f"mcp:{name}" if host == "local" else backend.remote_key(host, f"mcp-{name}")
    return Instance("mcp", key, name if host == "local" else f"mcp-{name}", run=f"mcp-{name}", mcp=name,
                    agent="mcp", started=started, state="running", host=host, record=record,
                    ident=None if host == "local" else f"mcp-{name}")


# -- start_tree.owners --


def test_an_mcp_server_belongs_to_the_start_it_was_started_for():
    work = session("work", mcp_servers=["fetch", "search"])
    other = session("other", mcp_servers=["fetch"])
    fetch = server("fetch", started_for={"start": None, "run": "claude-work"}, owner_recorded=True)
    search = server("search", started_for=None, owner_recorded=False)  # an older one: the start that uses it
    owned = start_tree.owners([other, work, fetch, search])
    assert owned == {"mcp:fetch": ("session:work", "started"), "mcp:search": ("session:work", "uses")}
    # sandbox-ai list --json of another host names the start (by its name)
    remote = [session("web", host="ct1"), server("docs", host="ct1", started_for="web", owner_recorded=True)]
    assert start_tree.owners(remote) == {backend.remote_key("ct1", "mcp-docs"): (backend.remote_key("ct1", "web"),
                                                                                  "started")}


def test_a_server_several_starts_use_belongs_to_the_one_that_reaches_it_now():
    later = session("later", started="2026-11-10T09:00:00Z", mcp=["fetch"])  # at its next start only
    now = session("now", started="2026-11-10T11:00:00Z", mcp_servers=["fetch"])
    fetch = server("fetch", owner_recorded=True, started_for=None)
    assert start_tree.owners([later, now, fetch]) == {"mcp:fetch": ("session:now", "uses")}


def test_an_older_server_nobody_uses_is_placed_by_its_start_time_and_said_to_be_a_guess():
    early = session("early", started="2026-11-10T08:00:00Z")
    close = session("close", started="2026-11-10T10:00:30Z")
    old = server("old", started="2026-11-10T10:00:00Z")  # no started_for at all: before 0.1.78
    far = server("far", started="2026-11-10T13:00:00Z")
    alone = server("alone", started="2026-11-10T10:00:05Z", started_for=None, owner_recorded=True)  # mcp start
    gone = server("gone", started="2026-11-10T10:00:05Z", owner_recorded=True,
                  started_for={"start": "ended-one", "run": None})
    found = [early, close, old, far, alone, gone]
    owned = start_tree.owners(found)
    assert owned == {"mcp:old": ("session:close", "time")}
    assert start_tree.orphans(found, owned) == ["mcp:far", "mcp:alone", "mcp:gone"]
    # A launcher of an MCP server that has not registered yet: with the start that is starting it.
    starting = Instance("unregistered", "pid:42", "pid 42", agent="codex", started="2026-11-10T10:00:31Z",
                        state="starting", pid=42, record={"mcp_server": True})
    agent = Instance("unregistered", "pid:41", "pid 41", agent="claude", started="2026-11-10T10:00:29Z",
                     state="starting", pid=41)
    assert start_tree.owners([close, starting, agent]) == {"pid:42": ("session:close", "time")}


def test_a_team_stands_below_its_lead_and_a_helper_below_whom_it_helps():
    lead = session("rev-claude", mcp_servers=["team-rev"])
    member = session("rev-codex", mcp_servers=["team-rev"], lead="rev-claude")
    board = server("team-rev", started_for={"start": None, "run": "claude-rev-claude"}, owner_recorded=True)
    # A member of an older listing (no "lead"): by the start its team's board was started for.
    other = session("rev-opencode", mcp_servers=["team-rev"])
    helper = Instance("run", "run:codex-help", "codex-help", run="codex-help", agent="codex", state="running",
                      record={"helper_of": "claude-rev-codex", "mcp_servers": ["notes"]})
    notes = server("notes", started_for={"start": None, "run": "codex-help"}, owner_recorded=True)
    owned = start_tree.owners([lead, member, board, other, helper, notes])
    assert owned == {"session:rev-codex": ("session:rev-claude", "team"),
                     "mcp:team-rev": ("session:rev-claude", "started"),
                     "session:rev-opencode": ("session:rev-claude", "team"),
                     # below the top of the chain: the helper's server below the lead too
                     "run:codex-help": ("session:rev-claude", "helper"),
                     "mcp:notes": ("session:rev-claude", "started")}
    a = Instance("run", "run:a", "a", run="a", state="running", record={"helper_of": "b"})
    b = Instance("run", "run:b", "b", run="b", state="running", record={"helper_of": "a"})
    assert start_tree.owners([a, b]) == {}, "a circle: each stands on its own"


# -- the Sandboxes page --


def tree_item(page, host, ident=""):
    return page.table.find_item(start_tree.row_id(host, ident))


def children(item, column=0):
    return [item.child(i).text(column) for i in range(item.childCount())]


def test_the_sandboxes_page_shows_each_start_with_everything_that_belongs_to_it(window, monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    lead = session("rev-claude", mcp_servers=["team-rev", "fetch"])
    lead.project = str(project)
    member = session("rev-codex", mcp_servers=["team-rev"], lead="rev-claude")
    board = server("team-rev", started_for={"start": None, "run": "claude-rev-claude"}, owner_recorded=True)
    fetch = server("fetch", started_for={"start": None, "run": "claude-rev-claude"}, owner_recorded=True)
    old = server("old", started="2026-11-10T10:00:10Z")  # before 0.1.78: placed by the time
    alone = server("demo", started="2026-11-10T15:00:00Z", started_for=None, owner_recorded=True)
    monkeypatch.setattr(window.sai, "instances", lambda: ([lead, member, board, fetch, old, alone], []))
    page = window.sandboxes
    read_now(page)
    tree = page.table
    host = tree_item(page, "local")
    assert children(host) == ["Session", "Belongs to no start"]
    top = tree_item(page, "local", "session:rev-claude")
    assert children(top, 1) == ["rev-codex", "fetch", "old", "team-rev"]  # its own starts first (by kind and name)
    below = {top.child(i).data(0, start_tree.KEY_ROLE): top.child(i) for i in range(top.childCount())}
    assert all(item.data(0, start_tree.KIND_ROLE) == "start" for item in below.values())
    assert "A member of the team rev-claude leads" in below["session:rev-codex"].toolTip(0)
    assert "Started for rev-claude" in below["mcp:fetch"].toolTip(0)
    assert "this is a guess" in below["mcp:old"].toolTip(0)
    # The board server both reach is in the group already: no second line for it below the member.
    assert children(below["session:rev-codex"]) == []
    orphans = tree_item(page, "local", start_tree.ORPHANS)
    assert orphans.data(0, start_tree.KIND_ROLE) == "group" and orphans.isExpanded()
    assert children(orphans, 1) == ["demo"] and "belong to no start" in orphans.toolTip(0)
    assert "belong to no start" in orphans.child(0).toolTip(0)
    assert host.text(1) == "Starts: 6, running: 6"
    # The orphans' row is no start: the buttons say what to select.
    tree.select_item(orphans)
    assert page.current() is None and "belong to no start" in page.stop_button.toolTip()
    tree.selectRow(widgets.find_row(tree, "mcp:demo"))
    assert page.current().key == "mcp:demo" and page.stop_button.isEnabled()
    # Only the current project: the lead's group stays whole, the server of no project goes.
    page.set_project(str(project))
    page.only_project.setChecked(True)
    assert [widgets.row_key(tree, r) for r in range(tree.rowCount())] == [
        "session:rev-claude", "session:rev-codex", "mcp:fetch", "mcp:old", "mcp:team-rev"]
    i18n.set_language("de")
    try:
        page.only_project.setChecked(False)
        assert tree_item(page, "local", start_tree.ORPHANS).text(0) == "Gehört zu keinem Start"
        assert "Vermutung" in tree_item(page, "local", "mcp:old").toolTip(0)
    finally:
        i18n.set_language("en")


# -- one tab per start --


def test_a_start_opens_one_tab_not_one_per_part(window, monkeypatch):
    """A team's lead opens its tab; its other members, its helper (a run) and an MCP server whose run the app took for
    a run of its own (Bubblewrap: no mcp- name) open none."""
    read_now(window.sandboxes)  # the first read: what runs then gets no tab of its own
    real_setting = backend.setting
    monkeypatch.setattr(backend, "setting", lambda name, *a, **k: True if name == "auto_tabs" else
                        real_setting(name, *a, **k))
    window.sync_tabs({})
    before = window.tabs.count()
    lead = session("rev-claude", mcp_servers=["team-rev"])
    member = session("rev-codex", mcp_servers=["team-rev"], lead="rev-claude")
    helper = Instance("run", "run:codex-help", "codex-help", run="codex-help", agent="codex", state="running",
                      record={"helper_of": "claude-rev-claude"})
    board = server("team-rev", started_for={"start": None, "run": "claude-rev-claude"}, owner_recorded=True)
    window.sync_tabs({i.key: i for i in (lead, member, helper, board)})
    opened = [window.tabs.entry(i)["key"] for i in range(before, window.tabs.count())]
    assert opened == ["session:rev-claude"]
    # A start of its own that comes later still opens its tab.
    work = session("work")
    window.sync_tabs({i.key: i for i in (lead, member, helper, board, work)})
    assert [window.tabs.entry(i)["key"] for i in range(before, window.tabs.count())] == [
        "session:rev-claude", "session:work"]


# -- what the app reads --


def test_an_mcp_server_is_never_a_run_of_its_own_and_carries_whom_it_was_started_for(tmp_path, monkeypatch):
    """With Bubblewrap an MCP server's launcher registers as AGENT-xxxx (no pod name, no mcp- run): it was listed as a
    run beside the server (and opened a tab). It is found by its PID now, as sandbox-ai list finds it."""
    s = backend.SandboxAI()
    runs = {"claude-work-1": {"run": "claude-work-1", "start": "work-1", "agent": "claude", "workspace": "/w",
                              "pid": 100, "started": "2026-11-10T10:00:00Z", "mcp_servers": ["fetch", "team-rev"]},
            "codex-5f5f5f5f": {"run": "codex-5f5f5f5f", "kind": "mcp", "agent": "codex", "pid": 200,
                               "workspace": "/s", "started": "2026-11-10T10:00:01Z"},
            "codex-6a6a6a6a": {"run": "codex-6a6a6a6a", "kind": "mcp", "agent": "codex", "pid": 300}}
    states = {"fetch": {"pid": 200, "backend": "bwrap", "started": "2026-11-10T09:59:59Z",
                        "started_for": {"start": "work-1", "run": "claude-work-1"}}}

    class Mcp:
        @staticmethod
        def definitions():
            return {"fetch": {}, "idle": {}}

        @staticmethod
        def status(name):
            return "running" if name in states else "stopped"

        @staticmethod
        def load_state(name):
            return states[name]

    class Team:
        @staticmethod
        def load(name):
            if name != "rev":
                raise ValueError("no team")
            return {"lead": "claude"}

    monkeypatch.setattr(s, "runs", lambda: runs)
    monkeypatch.setattr(s, "sessions", lambda: [["rev-codex", "codex", "podman", "stopped", "/w", "2026-11-09"],
                                                ["work", "claude", "podman", "running", "/w", "2026-11-10"]])
    monkeypatch.setattr(s, "session_ids", lambda: {"work": "work-1", "rev-codex": "rev-codex"})
    monkeypatch.setattr(s, "session_servers", lambda ident: ["team-rev"] if ident == "rev-codex" else [])
    monkeypatch.setattr(s, "foreground_start", lambda name, ident=None: False)
    real_module = s.module
    monkeypatch.setattr(s, "module", lambda name: Mcp if name == "mcp" else Team if name == "team" else
                        real_module(name))
    (tmp_path / "proc").mkdir()
    found, problems = s.instances(proc=tmp_path / "proc")
    assert problems == []
    by_key = {i.key: i for i in found}
    assert "run:codex-5f5f5f5f" not in by_key, "the server's run is the server's row"
    fetch = by_key["mcp:fetch"]
    assert fetch.run == "codex-5f5f5f5f" and fetch.record["owner_recorded"] is True
    assert fetch.record["started_for"] == {"start": "work-1", "run": "claude-work-1"}
    assert by_key["run:codex-6a6a6a6a"].kind == "mcp", "an MCP server's run, whatever its name"
    assert by_key["session:rev-codex"].record.get("lead") == "rev-claude"
    assert "lead" not in by_key["session:work-1"].record, "team-rev is no team of its name"
    assert start_tree.owners(found)["mcp:fetch"] == ("session:work-1", "started")


# -- the Terminals page's Sessions --


def test_the_terminals_sessions_list_shows_what_belongs_to_a_start_below_it(window):
    from sandbox_ai_app import terminal
    page = window.terminals
    listing = [{"name": "mcp-demo", "id": "mcp-demo", "kind": "mcp", "state": "running", "started_for": None,
                "owner_recorded": True, "serves": []},
               {"name": "mcp-fetch", "id": "mcp-fetch", "kind": "mcp", "state": "running", "started_for": "work",
                "owner_recorded": True, "serves": ["work"]},
               {"name": "work", "id": "work-1", "kind": "session", "state": "running", "attachable": True,
                "mcp_running": ["fetch"], "agent": "claude"},
               {"name": "old", "kind": "run", "state": "stopped"}]
    grouped = terminal.grouped("local", [e for e in listing if e["state"] == "running"])
    assert [(e["name"], e.get(terminal.OWNER)) for e in grouped] == [
        ("work", None), ("mcp-fetch", "work"), ("mcp-demo", "")]
    assert terminal.OWNER not in listing[1], "copies"
    page.starts_read("local", listing, None)
    assert [page.list.item(r, 0).text() for r in range(page.list.rowCount())] == [
        "work", "    ↳ mcp-fetch", "mcp-demo"]
    assert widgets.row_key(page.list, 1) == "mcp-fetch"
    assert "Belongs to work" in page.list.item(1, 0).toolTip()
    assert "belongs to no start" in page.list.item(2, 4).text()
    assert "belongs to no start" in page.list.item(2, 0).toolTip()


def test_the_new_texts_are_translated():
    from sandbox_ai_app import terminal, ui
    for text in [*ui.BELONGS_HELP.values(), ui.ORPHANS_HELP, terminal.ORPHAN_TIP, "Belongs to no start",
                 "MCP servers: {count}", "belongs to no start", "Belongs to {name}",
                 "MCP servers that belong to no start: select one of them below to act on it."]:
        assert i18n.TEXTS[text]["de"] and i18n.TEXTS[text]["de"] != text, text


def test_grouped_leaves_out_what_is_no_start():
    """grouped() takes what list --json gives; entries it cannot use are left out as host_instances leaves them."""
    from sandbox_ai_app import terminal
    assert terminal.grouped("ct1", [{"name": "--all", "state": "running"}, {"name": "ok", "state": "running"}]) == [
        {"name": "ok", "state": "running"}]
    assert json.dumps(terminal.grouped("ct1", [])) == "[]"
