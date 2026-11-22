"""Session bridges in the app (sandbox-ai bridge): the words of `bridge add` built from the dialog's choices, the paths
checked as the launcher checks them, the bridges marked on both sessions' rows of the Sandboxes page, New bridge…
with the launcher's dry run and its egress warning confirmed first, Show, Revoke and Resume in the background, and the
bridge events of the activity log in German too."""
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, bridges as BR, i18n, widgets  # noqa: E402
from conftest import read_now  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the fixtures

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


# -- no Qt: the words, the paths, the marks --

def test_paths_are_taken_relative_to_the_peers_workspace_as_the_launcher_takes_them(tmp_path):
    workspace = tmp_path / "beta"
    assert BR.relative_path("src", str(workspace)) == "src"
    assert BR.relative_path(" ", str(workspace)) == "." and BR.relative_path("./a//b/", None) == "a/b"
    assert BR.relative_path(str(workspace / "docs" / "out"), str(workspace)) == "docs/out"
    assert BR.relative_path(str(workspace), str(workspace)) == "."
    for bad, words in ((str(tmp_path / "elsewhere"), "not in the workspace"), ("a/../b", "'..'"),
                       ("x/.git/hooks", ".git"), ("a,b", "comma"), ("a\nb", "control")):
        with pytest.raises(ValueError, match=re.escape(words)):
            BR.relative_path(bad, str(workspace))


def test_the_choices_become_the_capabilities_and_the_add_arguments(tmp_path):
    caps = BR.capabilities(True, True, ["src", str(tmp_path / "docs")], ["out", "src"], True, str(tmp_path))
    assert caps == ["message", "ask", "files:rw:out", "files:rw:src", "files:ro:docs", "watch"], \
        "a path both read and written is written (once)"
    with pytest.raises(ValueError, match="at least one capability"):
        BR.capabilities()
    values = BR.add_values("alpha", "beta", caps, both=True, until="2h", ask="each", watch=True,
                           actions=["pause", "alert", "bogus"], limits={"rate": "30", "size": "", "daily": "1g"})
    assert values == {"a": "alpha", "b": "beta", "allow": caps, "both": True, "until": "2h", "ask": "each",
                      "observer_actions": ["alert", "pause"], "limits": [("rate", "30"), ("daily", "1g")]}
    assert BR.add_values("alpha", "beta", ["message"], watch=False, actions=["alert"])["observer_actions"] is None, \
        "observer actions only with watch"
    args = backend.SandboxAI.bridge_add_args(**values)
    assert args == ["bridge", "add", "alpha", "beta", "--allow", ",".join(caps), "--both", "--until=2h", "--ask=each",
                    "--observer-actions=alert,pause", "--limit=rate=30", "--limit=daily=1g"]
    for bad in ({"a": "alpha", "b": "alpha"}, {"a": "", "b": "beta"}, {"a": "alpha", "b": "beta", "until": "soon"},
                {"a": "alpha", "b": "beta", "limits": {"rate": "fast"}}):
        with pytest.raises(ValueError):
            BR.add_values(**{"caps": ["message"], **bad})
    with pytest.raises(ValueError):
        backend.SandboxAI.bridge_add_args("-x", "beta", ["message"])
    with pytest.raises(ValueError):
        backend.SandboxAI.bridge_add_args("alpha", "beta", ["--all"])


def test_the_words_are_what_the_launcher_parses():
    """The arguments the app builds are taken by sandbox-ai bridge's own parser, unchanged."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("bridge_for_app_tests", ROOT / "sandbox" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = backend.SandboxAI.bridge_add_args("alpha", "beta", ["message", "files:ro:src", "watch"], both=True,
                                             until="30m", ask="never", observer_actions=["alert", "net-cut"],
                                             limits=[("ask-timeout", "60")])
    parsed = module.parser().parse_args(args[1:] + ["--dry-run", "--json"])
    assert (parsed.a, parsed.b, parsed.allow, parsed.both, parsed.until, parsed.ask, parsed.observer_actions,
            parsed.limit, parsed.dry_run, parsed.json) == ("alpha", "beta", "message,files:ro:src,watch", True, "30m",
                                                           "never", "alert,net-cut", ["ask-timeout=60"], True, True)
    assert module.parse_caps(parsed.allow)["files"] == [{"path": "src", "mode": "ro"}]
    assert BR.OBSERVER_ACTIONS == module.OBSERVER_ACTIONS and [k for k, _ in BR.ASK_MODES] == ["first", "each", "never"]
    assert {k for k, *_ in BR.LIMITS} == set(module.DEFAULT_LIMITS) and set(BR.LIVE_STATES) == set(module.LIVE_STATES)


ROWS = [{"id": "1a2b3c4d", "state": "active", "a": "alpha", "b": "beta", "a_run": "session-alpha",
         "b_run": "session-beta", "a_to_b": "message,files:ro:src", "b_to_a": "-", "observer_actions": [],
         "ask": "first", "until": None, "calls": 3, "warnings": 1},
        {"id": "5e6f7a8b", "state": "suspended", "a": "guard", "b": "beta", "a_run": "session-guard",
         "b_run": "session-beta", "a_to_b": "watch", "b_to_a": "-", "observer_actions": ["alert"], "ask": "first",
         "until": "2026-11-15T10:00:00Z", "calls": 0, "warnings": 0},
        {"id": "9c0d1e2f", "state": "ended", "a": "alpha", "b": "gamma", "a_run": "session-alpha",
         "b_run": "session-gamma", "a_to_b": "ask", "b_to_a": "ask"}]


def test_both_sessions_are_marked_with_their_peer():
    assert BR.marker(ROWS, "session-alpha") == "bridged with beta", "an ended bridge marks nothing"
    assert BR.marker(ROWS, "session-guard") == "bridged with beta (suspended)"
    assert BR.marker(ROWS, "session-beta") == "bridged (2)" and BR.marker(ROWS, "session-gamma") == ""
    assert BR.marker(ROWS, None) == ""
    tip = BR.tooltip(ROWS, "session-beta").splitlines()
    assert tip == ["Bridge 1a2b3c4d (active): alpha may reach it: message,files:ro:src",
                   "Bridge 5e6f7a8b (suspended): guard may reach it: watch"]
    assert BR.tooltip(ROWS, "session-alpha") == "Bridge 1a2b3c4d (active): it may reach beta: message,files:ro:src"
    i18n.set_language("de")
    assert BR.marker(ROWS, "session-alpha") == "verbunden mit beta"
    assert BR.marker(ROWS, "session-guard") == "verbunden mit beta (ausgesetzt)"
    assert BR.mcp_at_start("codex") and not BR.mcp_at_start("claude") and not BR.mcp_at_start("")


def test_a_bridge_across_hosts_marks_only_this_machines_session():
    """bridge list --json of a bridge across hosts: the side on another host has a_host/b_host (its run id may be a
    local run's too: that one is not marked); its name is HOST:NAME."""
    rows = [{"id": "0a1b2c3d", "state": "active", "a": "alpha", "b": "lab1:beta", "a_run": "session-alpha",
             "b_run": "session-beta", "a_host": None, "b_host": "lab1", "remote_half": False, "a_to_b": "message",
             "b_to_a": "-"},
            {"id": "4e5f6a7b", "state": "active", "a": "laptop:gamma", "b": "delta", "a_run": "session-delta",
             "b_run": "session-delta", "a_host": "laptop", "b_host": None, "remote_half": True, "a_to_b": "watch",
             "b_to_a": "-"}]
    assert BR.marker(rows, "session-alpha") == "bridged with lab1:beta"
    assert BR.marker(rows, "session-beta") == "", "lab1's session-beta is not this machine's"
    assert BR.marker(rows, "session-delta") == "bridged with laptop:gamma"
    assert BR.tooltip(rows, "session-delta") == "Bridge 4e5f6a7b (active): laptop:gamma may reach it: watch"


# -- the activity log's bridge events, in German too --

SUMMARIES = {
    "created": ("bridge 1a2b3c4d alpha -> beta: message,files:ro:src; back: message",
                "Brücke 1a2b3c4d alpha -> beta: message,files:ro:src; zurück: message"),
    "revoked": ("bridge 1a2b3c4d revoked", "Brücke 1a2b3c4d zurückgenommen"),
    "resumed": ("bridge 1a2b3c4d resumed by the human", "Brücke 1a2b3c4d von Ihnen fortgesetzt"),
    "suspended": ("bridge 1a2b3c4d suspended: the kill switch of beta holds (api)",
                  "Brücke 1a2b3c4d ausgesetzt: der Kill-Switch von beta hält (api)"),
    "ended": ("bridge 1a2b3c4d ended: alpha has ended", "Brücke 1a2b3c4d beendet: alpha hat geendet"),
    "message": ("bridge 1a2b3c4d: message from alpha to beta (12 bytes)",
                "Brücke 1a2b3c4d: Nachricht von alpha an beta (12 Bytes)"),
    "ask": ("bridge 1a2b3c4d: question from alpha to beta", "Brücke 1a2b3c4d: Frage von alpha an beta"),
    "answer": ("bridge 1a2b3c4d: beta answered a question of alpha",
               "Brücke 1a2b3c4d: beta hat eine Frage von alpha beantwortet"),
    "read": ("bridge 1a2b3c4d: alpha read src/a b.py of beta (40 bytes)",
             "Brücke 1a2b3c4d: alpha hat src/a b.py von beta gelesen (40 Bytes)"),
    "write-asked": ("bridge 1a2b3c4d: alpha asks to write out/x.txt of beta",
                    "Brücke 1a2b3c4d: alpha möchte out/x.txt von beta schreiben"),
    "write-denied": ("bridge 1a2b3c4d: write of out/x.txt refused", "Brücke 1a2b3c4d: Schreiben von out/x.txt abgelehnt"),
    "write": ("bridge 1a2b3c4d: alpha wrote out/x.txt of beta (5 bytes, allowed by the human)",
              "Brücke 1a2b3c4d: alpha hat out/x.txt von beta geschrieben (5 Bytes, von Ihnen erlaubt)"),
    "alert": ("bridge 1a2b3c4d: alert (critical) by guard about beta: it reads ~/.ssh",
              "Brücke 1a2b3c4d: Alarm (kritisch) von guard über beta: it reads ~/.ssh"),
    "hint": ("bridge 1a2b3c4d: hint from observer guard to beta", "Brücke 1a2b3c4d: Hinweis von Beobachter guard an beta"),
    "net-cut": ("bridge 1a2b3c4d: observer guard cut the network of beta",
                "Brücke 1a2b3c4d: Beobachter guard hat das Netz von beta gekappt"),
    "pause": ("bridge 1a2b3c4d: observer guard paused beta", "Brücke 1a2b3c4d: Beobachter guard hat beta pausiert"),
}


def test_every_bridge_event_of_the_launcher_is_shown_in_german_too():
    """Each event bridge.py writes into a run's activity log ({"event": ...}) has its words here; in English the
    summary stays exactly as the launcher wrote it, in German it is translated with the same names and paths."""
    import ast
    written = set()
    for node in ast.walk(ast.parse((ROOT / "sandbox" / "bridge.py").read_text())):
        func = getattr(node, "func", None)
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if isinstance(node, ast.Call) and name in ("event", "both_events", "activity_event"):
            for item in ast.walk(node):
                if isinstance(item, ast.Dict):
                    written |= {v.value for k, v in zip(item.keys, item.values) if isinstance(k, ast.Constant)
                                and k.value == "event" and isinstance(v, ast.Constant)}
    assert len(written) >= 15 and written <= set(SUMMARIES), written ^ set(SUMMARIES)
    assert set(SUMMARIES) == {event for event, _, _ in BR.EVENTS}
    for event, (english, german) in SUMMARIES.items():
        record = {"kind": "bridge", "summary": english, "details": {"bridge": "1a2b3c4d", "event": event}}
        i18n.set_language("en")
        assert BR.event_summary(record) == english, event
        i18n.set_language("de")
        assert BR.event_summary(record) == german, event
    # What it does not know stays as it is.
    assert BR.event_summary({"kind": "bridge", "summary": "bridge x: something new", "details": {"event": "new"}}) == \
        "bridge x: something new"
    assert BR.event_summary({"kind": "tool", "summary": "bridge 1a2b3c4d revoked", "details": {"event": "revoked"}}) \
        == "bridge 1a2b3c4d revoked"
    assert BR.reason_text("its time ran out (2026-11-15T10:00:00Z)") == "seine Zeit ist abgelaufen (2026-11-15T10:00:00Z)"


def test_the_activity_page_shows_bridge_events_in_german(qapp):
    from sandbox_ai_app import activity_page
    record = {"time": "2026-11-14T10:00:00Z", "kind": "bridge", "source": "sandbox-ai",
              "summary": SUMMARIES["message"][0], "details": {"bridge": "1a2b3c4d", "event": "message"}}
    i18n.set_language("de")
    entry = activity_page.Entry(record)
    assert entry.cells[1] == "Brücke" and entry.cells[3] == SUMMARIES["message"][1]
    assert "message from alpha" in entry.search, "the launcher's own words are found too"
    assert "bridge" in activity_page.KINDS
    i18n.set_language("en")
    assert activity_page.Entry(record).cells[1:4:2] == ["bridge", SUMMARIES["message"][0]]


# -- the backend: sandbox-ai bridge, in the background --

class Runner:
    def __init__(self, out="{}", status=0, err=""):
        self.calls, self.out, self.status, self.err = [], out, status, err

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[3:])
        return subprocess.CompletedProcess(argv, self.status, self.out, self.err)


def test_the_backend_runs_bridge_add_dry_run_show_revoke_and_resume():
    plan = {"id": None, "state": "planned", "dry_run": True, "a": "alpha", "b": "beta", "warnings": ["w"]}
    runner = Runner(json.dumps(plan))
    sai = backend.SandboxAI(runner=runner)
    args = sai.bridge_add_args("alpha", "beta", ["message"])
    assert sai.bridge_plan(args) == plan and runner.calls[-1] == [*args, "--dry-run", "--json"]
    sai.bridge_add(args)
    assert runner.calls[-1] == [*args, "--json"]
    sai.bridge_show("1a2b3c4d")
    assert runner.calls[-1] == ["bridge", "show", "1a2b3c4d", "--json"]
    sai.bridge_revoke("1a2b3c4d")
    sai.bridge_resume("1a2b3c4d")
    assert runner.calls[-2:] == [["bridge", "revoke", "1a2b3c4d"], ["bridge", "resume", "1a2b3c4d"]]
    for bad in ("--all", "1A2B3C4D", "1a2b"):
        with pytest.raises(ValueError):
            sai.bridge_revoke(bad)
    refused = backend.SandboxAI(runner=Runner("", 1, "sandbox-ai bridge: nothing named gamma runs (see sandbox-ai "
                                                     "list)\n"))
    with pytest.raises(ValueError, match="nothing named gamma runs"):
        refused.bridge_plan(args)


def test_the_bridges_are_read_with_sandbox_ais_own_module(env):
    """bridge list --json, read from its files in the app's process (no command, no Podman): an ended one only with
    ALL."""
    sai = backend.SandboxAI()
    assert sai.bridge_supported() and sai.bridges() == []
    directory = env / "state" / "sandbox-ai" / "bridges" / "1a2b3c4d"
    directory.mkdir(parents=True)
    side = {"name": "alpha", "run": "session-alpha"}
    (directory / "bridge.json").write_text(json.dumps({
        "id": "1a2b3c4d", "state": "revoked", "created": "2026-11-14T10:00:00Z", "a": side,
        "b": {"name": "beta", "run": "session-beta"}, "grants": {"a": {"message": True, "files": []},
                                                                 "b": {"files": []}}}))
    assert sai.bridges() == []
    rows = sai.bridges(True)
    assert [(r["id"], r["state"], r["a"], r["b"], r["a_to_b"], r["b_to_a"]) for r in rows] == [
        ("1a2b3c4d", "revoked", "alpha", "beta", "message", "-")]


def test_making_resuming_and_revoking_a_bridge_is_asked_first():
    for action in ("bridge-add", "bridge-revoke", "bridge-resume"):
        assert backend.needs_confirm(action, {}), action


# -- the Sandboxes page --

def sessions(env):
    def session(name, agent="claude"):
        workspace = env / "projects" / name
        workspace.mkdir(parents=True, exist_ok=True)
        return backend.Instance("session", f"session:{name}", name, session=name, state="running",
                                run=f"session-{name}", agent=agent, project=str(workspace),
                                record={"kind": "agent", "agent": agent, "workspace": str(workspace)})
    helper = backend.Instance("run", "run:helper", "helper-codex", run="helper-codex", state="running",
                              record={"kind": "agent", "helper_of": "alpha"})
    paused = backend.Instance("session", "session:sleepy", "sleepy", run="session-sleepy", state="paused",
                              record={"kind": "agent"})
    return [session("alpha"), session("beta", "codex"), session("guard"), helper, paused]


def bridge_page(window, env, monkeypatch, wait, rows=ROWS):
    found = sessions(env)
    monkeypatch.setattr(window.sai, "instances", lambda: (found, []))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: [])
    monkeypatch.setattr(window.sai, "bridges", lambda include_all=False: [dict(r) for r in rows])
    page = window.sandboxes
    read_now(page)
    assert wait(lambda: page.bridges_box.rows == BR.live(rows), 15)
    return page


def state_of(page, name):
    from sandbox_ai_app import start_tree
    item = page.table.find_item(start_tree.row_id("local", f"session:{name}"))
    return item.text(6), item.toolTip(6) or item.toolTip(0)


def test_the_sandboxes_page_lists_the_bridges_and_marks_both_sessions(window, env, monkeypatch, wait):
    page = bridge_page(window, env, monkeypatch, wait)
    box = page.bridges_box
    assert box.isVisibleTo(page) and box.title() == "Bridges"
    assert box.table.rowCount() == 2 and not box.empty.isVisibleTo(box)
    cells = [[box.table.item(0, c).text() for c in range(box.table.columnCount())]]
    assert cells[0][:5] == ["1a2b3c4d", "alpha → beta", "message,files:ro:src", "-", "active"]
    text, tip = state_of(page, "alpha")
    assert text == "running, bridged with beta" and "Bridge 1a2b3c4d (active): it may reach beta" in tip
    assert state_of(page, "beta")[0] == "running, bridged (2)"
    assert state_of(page, "guard")[0] == "running, bridged with beta (suspended)"
    # The buttons follow the selection: Resume only for a suspended one.
    assert box.buttons["new"].isEnabled()
    assert not box.buttons["show"].isEnabled() and not box.buttons["revoke"].isEnabled()
    box.table.selectRow(widgets.find_row(box.table, "1a2b3c4d"))
    assert box.buttons["show"].isEnabled() and box.buttons["revoke"].isEnabled()
    assert not box.buttons["resume"].isEnabled() and "suspended" in box.buttons["resume"].toolTip()
    box.table.selectRow(widgets.find_row(box.table, "5e6f7a8b"))
    assert box.buttons["resume"].isEnabled()
    # Gone: the marks go too.
    monkeypatch.setattr(window.sai, "bridges", lambda include_all=False: [])
    box.refresh()
    assert wait(lambda: box.rows == [], 10)
    assert state_of(page, "alpha")[0] == "running" and box.empty.isVisibleTo(box) and not box.table.isVisibleTo(box)


def test_new_bridge_offers_the_running_agent_sessions_and_confirms_the_egress_warning(window, env, monkeypatch, wait):
    page = bridge_page(window, env, monkeypatch, wait)
    box = page.bridges_box
    assert [e["name"] for e in page.bridge_sessions()] == ["alpha", "beta", "guard"], \
        "no helper, nothing paused, nothing of another host"
    page.table.select_item(page.table.find_item(__import__("sandbox_ai_app.start_tree", fromlist=["row_id"]).row_id(
        "local", "session:guard")))
    assert page.bridge_selected() == "guard"
    shown_dialogs = []

    def fill(dialog):
        shown_dialogs.append(dialog)
        assert dialog.a.currentData() == "guard" and dialog.b.currentData() == "alpha", "the selected one is A"
        dialog.b.setCurrentIndex(dialog.b.findData("beta"))
        assert not dialog.actions["hint"].isEnabled(), "observer actions only with watch"
        dialog.watch.setChecked(True)
        assert dialog.actions["alert"].isChecked() and dialog.actions["pause"].isEnabled()
        dialog.actions["pause"].setChecked(True)
        assert "beta runs codex" not in dialog.note.text(), "only watched: it needs no tool of the bridge"
        dialog.message.setChecked(True)
        assert "beta runs codex, which reads its MCP servers only when it starts" in dialog.note.text()
        dialog.read.add(BR.relative_path(str(env / "projects" / "beta" / "src"), dialog.peer_workspace()))
        dialog.until.setText("2h")
        dialog.ask_mode.setCurrentIndex(dialog.ask_mode.findData("each"))
        dialog.advanced.setChecked(True)
        dialog.limits["rate"].setText("20")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(widgets, "exec_dropped", fill)
    warning = ("data can flow from guard (more restricted) to beta, which reaches what it cannot: host example.org")
    plans, added = [], []
    monkeypatch.setattr(window.sai, "bridge_plan", lambda args: plans.append(args) or {
        "id": None, "state": "planned", "dry_run": True, "a": "guard", "b": "beta",
        "grants": {"a": {"actions": ["alert", "pause"]}}, "until": "2026-11-14T12:00:00Z",
        "warnings": [warning, "beta runs codex, which reads its MCP servers only when it starts"]})
    monkeypatch.setattr(window.sai, "bridge_add", lambda args: added.append(args) or {
        "id": "abcdef01", "state": "active", "a": "guard", "b": "beta", "notes": []})
    widgets.SHOWN.clear()
    box.new_bridge()
    assert wait(lambda: added and not widgets.busy(box), 10)
    expected = ["bridge", "add", "guard", "beta", "--allow", "message,files:ro:src,watch", "--until=2h", "--ask=each",
                "--observer-actions=alert,pause", "--limit=rate=20"]
    assert plans == [expected] and added == [expected]
    title, question, _ = next(item for item in widgets.SHOWN if item[0] == "New bridge")
    assert question.startswith("Create this bridge?") and "guard may reach beta: message,files:ro:src,watch" in question
    assert "Data can flow from a more restricted session to a less restricted one:" in question
    assert f"⚠ {warning}" in question and "The observer may: alert, pause" in question
    assert "reads its MCP servers only when it starts" in question
    assert any("Bridge abcdef01 is active" in item[1] for item in widgets.SHOWN)
    # Declined: nothing is made.
    added.clear()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    box.new_bridge()
    assert wait(lambda: len(plans) == 2 and not widgets.busy(box), 10)
    assert added == []


def test_a_refused_dry_run_makes_nothing_and_says_why(window, env, monkeypatch, wait):
    page = bridge_page(window, env, monkeypatch, wait)
    box = page.bridges_box

    def fill(dialog):
        dialog.message.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(widgets, "exec_dropped", fill)
    added = []
    monkeypatch.setattr(window.sai, "bridge_plan", lambda args: (_ for _ in ()).throw(
        ValueError("alpha and beta are bridged already (1a2b3c4d)")))
    monkeypatch.setattr(window.sai, "bridge_add", lambda args: added.append(args))
    widgets.SHOWN.clear()
    box.new_bridge()
    assert wait(lambda: any(item[0] == "Not bridged" for item in widgets.SHOWN) and not widgets.busy(box), 10)
    assert added == [] and "bridged already" in next(i[1] for i in widgets.SHOWN if i[0] == "Not bridged")


def test_the_dialog_says_what_is_missing_and_stays_open(window, env, monkeypatch, wait):
    from sandbox_ai_app.bridges_view import BridgeDialog
    page = bridge_page(window, env, monkeypatch, wait)
    dialog = BridgeDialog(page, page.bridge_sessions())
    dialog.try_accept()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted and "at least one capability" in dialog.problem.text()
    dialog.write.edit.setPlainText("../../etc")
    dialog.try_accept()
    assert "'..' is never followed" in dialog.problem.text()
    dialog.write.edit.setPlainText("out")
    dialog.b.setCurrentIndex(dialog.b.findData(dialog.a.currentData()))
    dialog.try_accept()
    assert "two different sessions" in dialog.problem.text()
    dialog.deleteLater()


def test_show_revoke_and_resume_go_through_the_launcher_in_the_background(window, env, monkeypatch, wait):
    page = bridge_page(window, env, monkeypatch, wait)
    box = page.bridges_box
    done = []
    document = {"id": "1a2b3c4d", "state": "active", "created": "2026-11-14T10:00:00Z", "until": None,
                "a": {"name": "alpha", "server": "bridge-beta"}, "b": {"name": "beta", "server": "bridge-alpha"},
                "grants": {"a": {"message": True, "files": [{"path": "src", "mode": "ro"}]},
                           "b": {"watch": True, "actions": ["alert"], "files": []}},
                "ask": "first", "limits": {"rate": 60}, "calls": 3, "volume": {"bytes": 120},
                "warnings": ["data can flow from beta (more restricted) to alpha"],
                "alerts": [{"time": "t1", "from": "beta", "about": "alpha", "severity": "warning", "text": "odd"}],
                "recent": [{"time": "t2", "side": "a", "tool": "send_message", "result": "ok"}]}
    monkeypatch.setattr(window.sai, "bridge_show", lambda ident: done.append(("show", ident)) or document)
    monkeypatch.setattr(window.sai, "bridge_revoke", lambda ident: done.append(("revoke", ident)) or "")
    monkeypatch.setattr(window.sai, "bridge_resume", lambda ident: done.append(("resume", ident)) or "")
    widgets.SHOWN.clear()
    box.table.selectRow(widgets.find_row(box.table, "1a2b3c4d"))
    box.show_bridge()
    assert wait(lambda: any(item[0] == "Bridge 1a2b3c4d" for item in widgets.SHOWN), 10)
    text = next(item[1] for item in widgets.SHOWN if item[0] == "Bridge 1a2b3c4d")
    assert "alpha -> beta: message,files:ro:src; sees MCP server bridge-beta" in text
    assert "beta -> alpha: watch; observer actions: alert" in text and "⚠ data can flow from beta" in text
    assert "beta → alpha (warning): odd" in text and "send_message ok" in text
    box.revoke()
    assert wait(lambda: ("revoke", "1a2b3c4d") in done and not widgets.busy(box), 10)
    assert any(item[0] == "Revoke" and "Both lose it at once" in item[1] for item in widgets.SHOWN)
    box.table.selectRow(widgets.find_row(box.table, "5e6f7a8b"))
    box.resume()
    assert wait(lambda: ("resume", "5e6f7a8b") in done and not widgets.busy(box), 10)
    # Declined: nothing is revoked.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    box.revoke()
    assert done.count(("revoke", "5e6f7a8b")) == 0


def test_new_bridge_needs_two_running_sessions(window, env, monkeypatch, wait):
    found = sessions(env)[:1]
    monkeypatch.setattr(window.sai, "instances", lambda: (found, []))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: [])
    monkeypatch.setattr(window.sai, "bridges", lambda include_all=False: [])
    page = window.sandboxes
    read_now(page)
    box = page.bridges_box
    # Offered all the same: its click says why (disabled, a click did nothing - test_bridge_clicks_app).
    assert box.buttons["new"].isEnabled()
    widgets.SHOWN.clear()
    box.new_bridge()
    assert widgets.SHOWN and "1 run now" in widgets.SHOWN[-1][1]


# -- sessions of the user's hosts: HOST:NAME --

def remote_sessions(host="host1"):
    """What `sandbox-ai hosts run HOST -- list --json` lists there, as the app reads it (listed_instance)."""
    entries = [{"name": "web", "id": "web", "kind": "session", "agent": "claude", "state": "running",
                "workspace": "/home/me/web"},
               {"name": "api", "id": "api", "kind": "session", "agent": "codex", "state": "running",
                "workspace": "/home/me/api"},
               {"name": "old", "id": "old", "kind": "session", "agent": "claude", "state": "paused"},
               {"name": "mcp-fetch", "id": "mcp-fetch", "kind": "mcp", "state": "running"},
               {"name": "helper", "id": "helper", "kind": "run", "agent": "codex", "state": "running",
                "helper_of": "session-web"}]
    return [backend.listed_instance(host, entry) for entry in entries]


def test_host_sessions_become_host_name_peers_of_bridge_add():
    from sandbox_ai_app.bridges_view import candidates
    found = candidates(remote_sessions(), remote=True)
    assert [(e["name"], e["host"], e["agent"]) for e in found] == [("host1:api", "host1", "codex"),
                                                                    ("host1:web", "host1", "claude")], \
        "running agent sessions only: nothing paused, no MCP server, no helper"
    assert candidates(remote_sessions()) == [], "without REMOTE: this machine's only (the Board's Bridge part)"
    assert candidates(remote_sessions("not a host!"), remote=True) == []
    values = BR.add_values("alpha", "host1:web", ["message", "watch"], watch=True)
    assert values["a"] == "alpha" and values["b"] == "host1:web"
    assert backend.SandboxAI.bridge_add_args(**values)[:4] == ["bridge", "add", "alpha", "host1:web"]
    # Both of other hosts: of one host, made there; of two, relayed by this machine (the launcher decides; the
    # dialog says which).
    assert BR.add_values("host1:web", "host2:api", ["message"])["b"] == "host2:api"
    assert BR.placement("host1:web", "host2:api") == "relayed" and BR.placement("host1:web", "host1:api") == "there"
    assert BR.placement("alpha", "host1:web") == "here" and BR.placement("local:alpha", "host1:web") == "here"
    with pytest.raises(ValueError, match="two different sessions"):
        BR.add_values("local:alpha", "alpha", ["message"])
    for bad in ("Host1:web", "host1:-x", "host1:", ":web", "a:b:c"):
        with pytest.raises(ValueError):
            BR.add_values("alpha", bad, ["message"])
        with pytest.raises(ValueError):
            backend.SandboxAI.bridge_add_args("alpha", bad, ["message"])
    assert BR.peer_host("host1:web") == "host1" and BR.peer_host("web") is None and BR.peer_host("local:web") is None


def test_the_launcher_takes_the_host_name_the_app_builds():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bridge_for_app_host_tests", ROOT / "sandbox" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = backend.SandboxAI.bridge_add_args("alpha", "host1:web", ["watch"], observer_actions=["alert"])
    parsed = module.parser().parse_args(args[1:] + ["--dry-run", "--json"])
    assert parsed.b == "host1:web"
    assert module.remote().HOST_NAME.pattern == BR.HOST.pattern, "the app's host names are the launcher's"


class HostsSai:
    """remote_hosts and host_instances as the dialog calls them in the background: host1 answers, host2 does not."""

    def __init__(self):
        self.asked = []

    def remote_hosts(self):
        self.asked.append("hosts")
        return ["host1", "host2"]

    def host_instances(self, name, timeout=60):
        self.asked.append(name)
        if name == "host2":
            raise ValueError("ssh: connect to host host2 port 22: Connection refused")
        return remote_sessions(name)


def test_the_dialog_lists_the_hosts_sessions_in_the_background(window, env, monkeypatch, wait):
    from sandbox_ai_app.bridges_view import BridgeDialog
    page = bridge_page(window, env, monkeypatch, wait)
    sai = HostsSai()
    dialog = BridgeDialog(page, page.bridge_sessions(), first="alpha", sai=sai)
    names = lambda combo: [combo.itemData(i) for i in range(combo.count())]  # noqa: E731
    assert wait(lambda: "host1:web" in names(dialog.b) and "host2" in dialog.hosts_note.text(), 10)
    assert names(dialog.b) == ["alpha", "beta", "guard", "host1:api", "host1:web"], "this machine's first"
    assert names(dialog.a) == names(dialog.b) and dialog.a.currentData() == "alpha"
    assert dialog.b.currentData() == "beta", "chosen before the host answered: kept"
    assert "host2: no answer" in dialog.hosts_note.text() and "Connection refused" in dialog.hosts_note.text()
    assert sorted(sai.asked) == ["host1", "host2", "hosts"]
    dialog.b.setCurrentIndex(dialog.b.findData("host1:web"))
    assert dialog.b.currentText() == "host1:web (claude)" and not dialog.peer_local()
    dialog.watch.setChecked(True)
    dialog.read.edit.setPlainText("src")
    values = dialog.values()
    assert values["a"] == "alpha" and values["b"] == "host1:web" and values["allow"] == ["files:ro:src", "watch"]
    # The folder picker never opens this machine's folder of the same name for a session of another host: that
    # host's folders, listed over SSH (remote_chooser), starting in its workspace there.
    from sandbox_ai_app import remote_chooser
    widgets.SHOWN.clear()
    picked, hosts_asked = [], []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: picked.append(a) or "")
    workspace = dialog.peer_workspace()
    assert workspace == "/home/me/web"

    def choose(parent, host, title, mode="folder", key="", start="", *args, **kw):
        hosts_asked.append((host, start))
        return start + "/docs"
    monkeypatch.setattr(remote_chooser, "choose", choose)
    dialog.read.pick()
    assert picked == [] and hosts_asked == [("host1", workspace)]
    assert dialog.read.paths() == ["src", "docs"], "taken relative to its workspace there"
    # Two sessions of one other host: made there, as the dialog says.
    dialog.a.setCurrentIndex(dialog.a.findData("host1:api"))
    assert "Both sessions run on host1: the bridge is made there" in dialog.note.text()
    assert dialog.values()["a"] == "host1:api" and not dialog.problem.text()
    # A host's next answer replaces what it listed before (a session that ended goes).
    dialog.host_listed("host1", remote_sessions()[:1], None)
    assert names(dialog.b) == ["alpha", "beta", "guard", "host1:web"]
    dialog.deleteLater()


def test_new_bridge_reaches_a_session_of_another_host(window, env, monkeypatch, wait):
    found = sessions(env)[:1]  # alpha only on this machine
    monkeypatch.setattr(window.sai, "instances", lambda: (found, []))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: ["host1"])
    monkeypatch.setattr(window.sai, "host_instances", lambda name, *args, **kwargs: remote_sessions(name))
    monkeypatch.setattr(window.sai, "bridges", lambda include_all=False: [])
    page = window.sandboxes
    read_now(page)
    box = page.bridges_box
    assert wait(lambda: [e["name"] for e in page.bridge_remote_sessions()] == ["host1:api", "host1:web"], 15)
    assert [e["name"] for e in page.bridge_sessions()] == ["alpha"]
    box.update_buttons()
    assert box.buttons["new"].isEnabled(), "one session here and a host: a bridge across hosts can be made"

    def fill(dialog):
        assert dialog.a.currentData() == "alpha" and dialog.b.currentData() == "host1:api", \
            "the host's sessions the page has seen are there at once"
        dialog.b.setCurrentIndex(dialog.b.findData("host1:web"))
        dialog.message.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(widgets, "exec_dropped", fill)
    warning = "data can flow from alpha (more restricted) to host1:web, which reaches what it cannot: any host"
    plans, added = [], []
    monkeypatch.setattr(window.sai, "bridge_plan", lambda args: plans.append(args) or {
        "id": None, "state": "planned", "dry_run": True, "a": "alpha", "b": "host1:web", "grants": {},
        "warnings": [warning]})
    monkeypatch.setattr(window.sai, "bridge_add", lambda args: added.append(args) or {
        "id": "abcdef02", "state": "active", "a": "alpha", "b": "host1:web", "notes": []})
    widgets.SHOWN.clear()
    box.new_bridge()
    assert wait(lambda: added and not widgets.busy(box), 10)
    assert plans == added == [["bridge", "add", "alpha", "host1:web", "--allow", "message", "--ask=first"]]
    question = next(item[1] for item in widgets.SHOWN if item[0] == "New bridge")
    assert "alpha may reach host1:web: message" in question and f"⚠ {warning}" in question
