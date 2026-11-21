"""Bridges of the user's hosts in the app (0.1.98): two sessions of other hosts (of one host: made there; of two:
relayed by this machine), the hosts' bridges listed beside this machine's (each once, HOST:ID), and Show, Revoke and
Resume of one of another host through `sandbox-ai bridge ... HOST:ID`."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, bridges as BR, i18n, widgets  # noqa: E402
from conftest import read_now  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the fixtures
from test_bridges_app import sessions  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


# -- no Qt --

def test_the_peers_are_grouped_by_host_and_named_as_the_boards_host_sees_them():
    local = [{"name": "docs", "agent": "claude"}]
    hosts = {"ct2": [{"name": "ct2:api", "agent": "codex"}], "ct1": [{"name": "ct1:web", "agent": "claude"}],
             "bad host": [{"name": "x"}], "local": [{"name": "y"}], "ct3": []}
    here = [(host, [value for value, _ in entries]) for host, entries in BR.peer_groups(local, hosts)]
    assert here == [(None, ["docs"]), ("ct1", ["ct1:web"]), ("ct2", ["ct2:api"])]
    there = [(host, [value for value, _ in entries]) for host, entries in BR.peer_groups(local, hosts, "ct2")]
    assert there == [("ct2", ["api"]), (None, ["local:docs"]), ("ct1", ["ct1:web"])]
    assert BR.on_host("api", "ct2") == "ct2:api" and BR.on_host("local:docs", "ct2") == "local:docs"
    assert BR.on_host("local:docs", "local") == "docs" and BR.on_host("ct1:web", None) == "ct1:web"
    assert BR.on_host("", "ct2") == ""


def test_the_hosts_bridges_are_shown_once_each_with_their_host():
    local = [{"id": "aaaa0001", "state": "active", "a": "alpha", "b": "ct1:beta", "b_host": "ct1"},
             {"id": "aaaa0005", "state": "active", "a": "ct1:x", "b": "ct2:y", "a_host": "ct1", "b_host": "ct2",
              "relay": True}]
    ct1 = BR.from_host([{"id": "aaaa0001", "state": "active", "a": "beta", "b": "laptop:alpha", "b_host": "laptop",
                         "remote_half": True},
                        {"id": "bbbb0002", "state": "active", "a": "p", "b": "q"},
                        {"id": "aaaa0005", "state": "active", "a": "x", "b": "ct2:y", "b_host": "ct2",
                         "remote_half": True, "relayed_by": "laptop"},
                        {"id": "nope"}, "junk"], "ct1")
    rows = BR.merged(local, ct1)
    assert [(BR.qualified(row), row["a"], row["b"]) for row in rows] == [
        ("aaaa0001", "alpha", "ct1:beta"), ("aaaa0005", "ct1:x", "ct2:y"), ("ct1:bbbb0002", "ct1:p", "ct1:q")]
    assert BR.local_run(rows[2], "a") is None, "a session of ct1 is never marked on this machine's rows"


def test_the_backend_takes_a_hosts_bridge_by_host_and_id(monkeypatch):
    calls = []

    class Runner:
        def __call__(self, argv, **kwargs):
            import subprocess
            calls.append(argv[3:])
            return subprocess.CompletedProcess(argv, 0, '{"id": "1a2b3c4d", "host": "ct1"}', "")
    sai = backend.SandboxAI(runner=Runner())
    sai.bridge_show("ct1:1a2b3c4d")
    sai.bridge_revoke("ct1:1a2b3c4d")
    sai.bridge_resume("ct1:1a2b3c4d")
    assert calls == [["bridge", "show", "ct1:1a2b3c4d", "--json"], ["bridge", "revoke", "ct1:1a2b3c4d"],
                     ["bridge", "resume", "ct1:1a2b3c4d"]]
    for bad in ("CT1:1a2b3c4d", "ct1:", "me@ct1:1a2b3c4d", "ct1:1a2b3c4d:x", "-o:1a2b3c4d"):
        with pytest.raises(ValueError):
            sai.bridge_revoke(bad)
    monkeypatch.setattr(sai, "host_json", lambda name, *args, timeout=120: calls.append((name, args)) or [
        {"id": "bbbb0002", "state": "active", "a": "p", "b": "q"}])
    rows = sai.host_bridges("ct1")
    assert calls[-1] == ("ct1", ("bridge", "list", "--json")) and rows[0]["host"] == "ct1" and rows[0]["a"] == "ct1:p"
    monkeypatch.setattr(sai, "host_json", lambda name, *args, timeout=120: {"error": "x"})
    with pytest.raises(ValueError, match="not a list"):
        sai.host_bridges("ct1")


# -- the Sandboxes page --

def test_the_bridges_box_lists_the_hosts_bridges_and_acts_on_them_there(window, env, monkeypatch, wait):
    found = sessions(env)
    monkeypatch.setattr(window.sai, "instances", lambda: (found, []))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: ["ct1", "ct2"])
    monkeypatch.setattr(window.sai, "host_instances", lambda name, *a, **k: [])
    monkeypatch.setattr(window.sai, "bridges", lambda include_all=False: [
        {"id": "aaaa0005", "state": "active", "a": "ct1:x", "b": "ct2:y", "a_host": "ct1", "b_host": "ct2",
         "a_to_b": "message", "b_to_a": "-", "relay": True, "calls": 2}])
    asked = []

    def host_bridges(host, timeout=60):
        asked.append(host)
        if host == "ct2":
            raise ValueError("ssh: connect to host ct2 port 22: Connection refused")
        return BR.from_host([{"id": "aaaa0005", "state": "active", "a": "x", "b": "ct2:y", "b_host": "ct2",
                              "remote_half": True, "a_to_b": "message", "b_to_a": "-"},
                             {"id": "bbbb0002", "state": "suspended", "a": "p", "b": "q", "a_to_b": "watch",
                              "b_to_a": "-"}], host)
    monkeypatch.setattr(window.sai, "host_bridges", host_bridges)
    page = window.sandboxes
    read_now(page)
    box = page.bridges_box
    assert wait(lambda: len(box.rows) == 2, 15)
    cells = [[box.table.item(r, c).text() for c in range(3)] for r in range(box.table.rowCount())]
    assert cells == [["aaaa0005", "ct1:x → ct2:y", "message"], ["ct1:bbbb0002", "ct1:p → ct1:q", "watch"]]
    assert sorted(set(asked)) == ["ct1", "ct2"]
    count = len(asked)
    box.refresh()
    assert len(asked) == count, "the hosts are asked again only after a while"
    done = []
    monkeypatch.setattr(window.sai, "bridge_show", lambda ident: done.append(("show", ident)) or {"id": "bbbb0002"})
    monkeypatch.setattr(window.sai, "bridge_resume", lambda ident: done.append(("resume", ident)) or "")
    monkeypatch.setattr(window.sai, "bridge_revoke", lambda ident: done.append(("revoke", ident)) or "")
    widgets.SHOWN.clear()
    box.table.selectRow(widgets.find_row(box.table, "ct1:bbbb0002"))
    box.update_buttons()
    assert box.buttons["resume"].isEnabled(), "a suspended bridge of a host is resumed there"
    box.show_bridge()
    box.resume()
    assert wait(lambda: ("resume", "ct1:bbbb0002") in done and not widgets.busy(box), 10)
    assert ("show", "ct1:bbbb0002") in done and len(asked) > count, "acted on: the hosts read again at once"
    assert any(item[0] == "Resume" and "ct1:bbbb0002" in item[1] for item in widgets.SHOWN)
    box.table.selectRow(widgets.find_row(box.table, "aaaa0005"))
    box.revoke()
    assert wait(lambda: ("revoke", "aaaa0005") in done and not widgets.busy(box), 10), "relayed here: revoked here"


def test_new_bridge_joins_two_sessions_of_two_other_hosts(window, env, monkeypatch, wait):
    monkeypatch.setattr(window.sai, "instances", lambda: ([], []))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: ["ct1", "ct2"])
    monkeypatch.setattr(window.sai, "host_bridges", lambda host, timeout=60: [])

    def host_instances(name, *args, **kwargs):
        return [backend.listed_instance(name, {"name": "web", "id": "web", "kind": "session", "agent": "claude",
                                               "state": "running", "workspace": "/w"})]
    monkeypatch.setattr(window.sai, "host_instances", host_instances)
    monkeypatch.setattr(window.sai, "bridges", lambda include_all=False: [])
    page = window.sandboxes
    read_now(page)
    box = page.bridges_box
    assert wait(lambda: len(page.bridge_remote_sessions()) == 2, 15)
    box.update_buttons()
    assert box.buttons["new"].isEnabled(), "no session here, but your hosts': two of them can be bridged"

    def fill(dialog):
        dialog.a.setCurrentIndex(dialog.a.findData("ct1:web"))
        dialog.b.setCurrentIndex(dialog.b.findData("ct2:web"))
        assert "this machine passes their messages on" in dialog.note.text()
        dialog.message.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(widgets, "exec_dropped", fill)
    added = []
    monkeypatch.setattr(window.sai, "bridge_plan", lambda args: {"id": None, "state": "planned", "grants": {},
                                                                 "warnings": [], "notes": ["relayed"]})
    monkeypatch.setattr(window.sai, "bridge_add", lambda args: added.append(args) or {
        "id": "abcdef04", "state": "active", "a": "ct1:web", "b": "ct2:web", "relay": True, "notes": []})
    box.new_bridge()
    assert wait(lambda: added and not widgets.busy(box), 10)
    assert added == [["bridge", "add", "ct1:web", "ct2:web", "--allow", "message", "--ask=first"]]
