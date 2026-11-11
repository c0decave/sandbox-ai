"""The kill switch in the app (sandbox-ai net cut): per sandbox from the Sandboxes page (also several at once, here
and on a host), what holds shown in the State column, and the window's emergency stop - instant, without a question,
a notification afterwards that says what was cut and how to lift it."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app.ui import CUT_CHOICES, EMERGENCY_KEYS  # noqa: E402
from conftest import read_now  # noqa: E402
from test_ui import env, sandboxes_tree, window  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def report(*runs, mode="api"):
    """What `sandbox-ai net cut ... --json` prints: RUNS are (run, ok[, error])."""
    return json.dumps({"mode": mode, "runs": [
        {"run": run, "name": run, "for": None, "ok": ok, "cut": mode if ok and mode != "off" else None,
         "error": None if ok else (rest[0] if rest else "refused")} for run, ok, *rest in runs]})


def select_items(tree, items):
    flags = QItemSelectionModel.SelectionFlag
    tree.clearSelection()
    for item in items:
        tree.selectionModel().select(tree.indexFromItem(item), flags.Select | flags.Rows)


def items(page):
    from sandbox_ai_app import start_tree
    return {item.data(0, start_tree.KEY_ROLE): item for item in page.table.start_items()}


def test_the_backend_reads_the_report_and_asks_every_host_at_once(window, monkeypatch):
    calls = []

    def host_run(host, *args, **kwargs):
        calls.append((host, *args))
        return (0, report(("claude-1", True), mode="all"), "") if host == "local" else (255, "", "ssh: gone")
    monkeypatch.setattr(window.sai, "host_run", host_run)
    found = window.sai.cut_everywhere("all", ["local", "ct1", "local"])
    assert sorted(calls) == [("ct1", "net", "cut", "--all-runs", "all", "--json"),
                             ("local", "net", "cut", "--all-runs", "all", "--json")]
    assert backend.cut_report(found["local"]) == [{"run": "claude-1", "name": "claude-1", "for": None, "ok": True,
                                                  "cut": "all", "error": ""}]
    assert backend.cut_report(found["ct1"]) is None
    assert window.sai.cut_run("bad name;", "all") == (2, "", "invalid run or mode")
    assert window.sai.cut_run("claude-1", "everything") == (2, "", "invalid run or mode")
    with pytest.raises(ValueError):
        window.sai.cut_everywhere("everything")
    assert window.sai.cut_supported()  # this checkout has `sandbox-ai net cut`


def test_the_state_column_shows_the_cut(window, monkeypatch, wait):
    work = backend.Instance("session", "session:work", "work", session="work", state="running", run="claude-abc",
                            record={"cut": "api", "net_cut": 1})
    other = backend.Instance("run", "run:codex-1", "codex-1", run="codex-1", state="running",
                             record={"cut": "all", "net_cut": 1})
    plain = backend.Instance("run", "run:codex-2", "codex-2", run="codex-2", state="running", record={"net_cut": 1})
    monkeypatch.setattr(window.sai, "instances", lambda: ([work, other, plain], []))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: [])
    page = window.sandboxes
    read_now(page)
    assert wait(lambda: "session:work" in items(page))
    found = items(page)
    assert found["session:work"].text(6) == "running, only the model API"
    assert "kill switch (api)" in found["session:work"].toolTip(0) and "Lift the cut" in found["session:work"].toolTip(0)
    assert found["run:codex-1"].text(6) == "running, network cut"
    assert found["run:codex-2"].text(6) == "running"


def test_the_kill_switch_cuts_the_selected_sandboxes_at_once(window, monkeypatch, wait):
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    found = items(page)
    work, fetch, web = found["session:work"], found["mcp:fetch"], found[backend.remote_key("ct1", "web")]
    notices = []
    monkeypatch.setattr(window, "notify", lambda title, text, level="warning", action=None: notices.append(
        (title, text, level)))
    calls = []

    def cut_run(run, mode, host="local"):
        calls.append((host, run, mode))
        if host == "ct1":
            return 1, report((run, False, "web was started by a sandbox-ai older than 0.1.94"), mode=mode), ""
        return 0, report((run, True), ("mcp-fetch", True), mode=mode), ""
    monkeypatch.setattr(window.sai, "cut_run", cut_run)
    # One start: the menu offers every mode; the cut runs at once, without a question.
    select_items(page.table, [work])
    assert page.cut_button.isEnabled() and page.cut_button.menu() is page.cut_menu
    assert [action.text() for action in page.cut_menu.actions() if not action.isSeparator()] == \
        [text for _, text, _ in CUT_CHOICES]
    widgets.SHOWN.clear()
    page.cut_actions["api"].trigger()
    assert wait(lambda: calls and not widgets.busy(page))
    assert calls == [("local", "claude-abc", "api")] and widgets.SHOWN == []  # nobody was asked
    assert notices and notices[-1][0] == "Kill switch: Only the model API" and notices[-1][2] == "warning"
    assert "claude-abc, mcp-fetch: Only its model API stays reachable" in notices[-1][1]
    assert "Lift the cut gives it back" in notices[-1][1]
    # Several, here and on a host: each one's host and run; what failed is said together.
    calls.clear()
    select_items(page.table, [work, fetch, web])
    assert page.cut_button.isEnabled()
    page.cut("all")
    assert wait(lambda: len(calls) == 3 and not widgets.busy(page))
    assert sorted(calls) == [("ct1", "web", "all"), ("local", "claude-abc", "all"), ("local", "mcp-fetch", "all")]
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Kill switch: Cut everything" and text.startswith("1 of 3 failed:") and "0.1.94" in text
    # Lifted: a success, without "lift it" advice.
    calls.clear()
    select_items(page.table, [work])
    page.cut("off")
    assert wait(lambda: calls and not widgets.busy(page))
    assert notices[-1][2] == "success" and "Lift the cut" not in notices[-1][1]


def test_the_kill_switch_waits_for_a_running_start_and_a_sandbox_ai_that_has_it(window, monkeypatch, wait):
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    page.table.clearSelection()
    page.update_buttons()
    assert not page.cut_button.isEnabled()
    stopped = backend.Instance("session", "session:old", "old", session="old", state="stopped")
    assert page.cut_why(stopped) == "It does not run."
    select_items(page.table, [items(page)["session:work"]])
    page.cuttable = False  # an older sandbox-ai: no `net cut`
    page.update_buttons()
    assert not page.cut_button.isEnabled() and "0.1.94" in page.cut_button.toolTip()


def test_the_emergency_stop_cuts_everything_at_once_and_says_how_to_lift_it(window, monkeypatch, wait):
    """The real path (cut_every_host, cut_everywhere): the hosts read afresh, every one sent the cut at once, the one
    that does not answer named."""
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    action = window.actions["emergency-cut"]
    assert QKeySequence(EMERGENCY_KEYS) in action.shortcuts()
    assert action.shortcutContext() == Qt.ShortcutContext.ApplicationShortcut  # from every window of the app
    notices, calls = [], []
    monkeypatch.setattr(window, "notify", lambda title, text, level="warning", action=None: notices.append(
        (title, text, level, action)))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: ["ct1", "ct2", "ct3"])  # ct3: added since the last look

    def host_run(host, *args, **kwargs):
        calls.append((host, *args))
        mode = args[3]
        if host == "ct2":
            return 255, "", "ssh: connect to host ct2: Connection timed out"
        if host == "ct1":
            return 1, report(("web", True), ("old-1", False, "started by a sandbox-ai older than 0.1.94"), mode=mode), ""
        return 0, report(("claude-abc", True), ("mcp-fetch", True), mode=mode) if host == "local" else \
            report(("api-9", True), mode=mode), ""
    monkeypatch.setattr(window.sai, "host_run", host_run)
    widgets.SHOWN.clear()
    action.trigger()
    assert wait(lambda: notices)
    assert sorted(calls) == [(host, "net", "cut", "--all-runs", "all", "--json") for host in ("ct1", "ct2", "ct3",
                                                                                              "local")]
    assert widgets.SHOWN == []  # no question: it must be instant
    title, text, level, button = notices[-1]
    assert title == "Emergency stop: every sandbox's network is cut" and level == "error"
    assert text.startswith("Cut: claude-abc, mcp-fetch, ") and "web on ct1" in text and "api-9 on ct3" in text
    assert "old-1 on ct1: started by a sandbox-ai older than 0.1.94" in text
    assert "ct2: ssh: connect to host ct2: Connection timed out" in text
    assert "File › Lift every cut" in text and "sandbox-ai net cut --all-runs off" in text
    assert button is not None and button[0] == "Show the sandboxes"
    # The header's button does the same; File › Lift every cut asks first (a question nothing waits for).
    notices.clear()
    window.emergency_button.click()
    assert wait(lambda: notices) and calls[-1][4] == "all"
    notices.clear()
    calls.clear()
    window.actions["lift-cut"].trigger()
    assert widgets.SHOWN[-1][0] == "Lift every cut" and "also those you cut one by one" in widgets.SHOWN[-1][1]
    assert wait(lambda: notices) and {call[4] for call in calls} == {"off"}
    assert notices[-1][0] == "Every cut lifted" and notices[-1][1].startswith("Lifted: claude-abc")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # not lifted when the question is not answered yes
    calls.clear()
    window.actions["lift-cut"].trigger()
    assert calls == []


def test_the_emergency_stop_right_after_the_start_reaches_every_host(window, monkeypatch, wait):
    """Right after the start the Sandboxes page has seen no host yet: the hosts are read for the cut itself."""
    page = window.sandboxes
    page.remote_names = []
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: ["ct1"])
    calls, notices = [], []
    monkeypatch.setattr(window, "notify", lambda title, text, level="warning", action=None: notices.append(text))
    monkeypatch.setattr(window.sai, "host_run", lambda host, *args, **kwargs: calls.append(host) or (
        0, report(("r-" + host, True), mode="all"), ""))
    window.emergency_cut()
    assert wait(lambda: notices)
    assert sorted(calls) == ["ct1", "local"] and "r-ct1 on ct1" in notices[-1]
    # A list of hosts that cannot be read: this machine is cut all the same, and that is said.
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: (_ for _ in ()).throw(OSError("hosts.json: damaged")))
    notices.clear()
    window.emergency_cut()
    assert wait(lambda: notices) and "sandbox-ai hosts: the list of hosts could not be read: hosts.json: damaged" \
        in notices[-1]


def test_an_emergency_stop_wins_over_a_lift_still_on_its_way(window, monkeypatch, wait):
    import threading
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: [])
    release, calls, notices = threading.Event(), [], []
    monkeypatch.setattr(window, "notify", lambda title, text, level="warning", action=None: notices.append(title))

    def host_run(host, *args, **kwargs):
        calls.append(args[3])
        if args[3] == "off":
            release.wait(10)  # the lift is slow (a host that takes its time)
        return 0, report(("claude-abc", True), mode=args[3]), ""
    monkeypatch.setattr(window.sai, "host_run", host_run)
    window.lift_every_cut()
    assert wait(lambda: calls == ["off"])
    window.emergency_cut()
    assert wait(lambda: calls == ["off", "all"])  # at once: it never waits for the lift
    release.set()
    # Once the lift is done, the emergency stop runs once more (the lift may have reached a host after it): cut wins.
    assert wait(lambda: calls == ["off", "all", "all"] and not window.cut_jobs)
    # A lift asked for while an emergency stop is on its way goes after it: the later choice holds.
    calls.clear()
    release.clear()
    gate = threading.Event()

    def slow_cut(host, *args, **kwargs):
        calls.append(args[3])
        if args[3] == "all":
            gate.wait(10)
        return 0, report(("claude-abc", True), mode=args[3]), ""
    monkeypatch.setattr(window.sai, "host_run", slow_cut)
    window.emergency_cut()
    assert wait(lambda: calls == ["all"])
    window.lift_every_cut()
    assert calls == ["all"]  # waits
    gate.set()
    assert wait(lambda: calls == ["all", "off"] and not window.cut_jobs)


def test_ctrl_shift_x_is_the_emergency_stop_not_the_file_lists_cut():
    from PySide6.QtCore import Qt as Q
    from sandbox_ai_app.files_page import FilesPage
    control, shift = Q.KeyboardModifier.ControlModifier, Q.KeyboardModifier.ShiftModifier
    assert FilesPage.is_key(None, Q.Key.Key_X, control)  # Ctrl+X: the list's cut of files
    assert not FilesPage.is_key(None, Q.Key.Key_X, control | shift)  # Ctrl+Shift+X: the window's
    assert not FilesPage.is_key(None, Q.Key.Key_C, control | Q.KeyboardModifier.AltModifier)
    assert FilesPage.key(None, None, Q.Key.Key_X, control | shift) is False


def test_the_selected_sandboxes_are_cut_at_once_and_a_later_choice_follows(window, monkeypatch, wait):
    """A host that does not answer holds up no other sandbox; the button stays usable, and a mode chosen meanwhile
    for a sandbox whose cut is on its way follows it (the later choice holds)."""
    import threading
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    found = items(page)
    work, web = found["session:work"], found[backend.remote_key("ct1", "web")]
    monkeypatch.setattr(window, "notify", lambda *a, **k: None)
    gate, calls = threading.Event(), []

    def cut_run(run, mode, host="local"):
        calls.append((host, run, mode))
        if host == "ct1":
            gate.wait(10)  # a host that takes its time
        return 0, report((run, True), mode=mode), ""
    monkeypatch.setattr(window.sai, "cut_run", cut_run)
    select_items(page.table, [work, web])
    page.cut("remote")
    assert wait(lambda: ("local", "claude-abc", "remote") in calls and ("ct1", "web", "remote") in calls)
    assert page.cut_button.isEnabled()  # never held while a cut is on its way
    select_items(page.table, [web])
    page.cut("api")
    page.cut("all")  # chosen before "api" was sent: only the latest follows
    assert ("ct1", "web", "api") not in calls and ("ct1", "web", "all") not in calls
    gate.set()
    assert wait(lambda: ("ct1", "web", "all") in calls and not widgets.busy(page))
    assert ("ct1", "web", "api") not in calls and [c for c in calls if c[0] == "ct1"][-1][2] == "all"
