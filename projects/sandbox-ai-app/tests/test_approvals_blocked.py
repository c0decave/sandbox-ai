"""The Approvals page's header (which sandbox: the active tab's), Blocked (what was refused, why, newest first, with
its actions) and Allowed now (what the run may reach, Revoke), and the keys of the questions."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from sandbox_ai_app import approvals_sections as sections, backend, pages, widgets  # noqa: E402

RULESET = "--rules tor-researcher.rules.json"
URL = "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def fake_run(env, name, workspace="project", **fields):
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    (env / workspace).mkdir(exist_ok=True)
    logs = env / f"logs-{workspace}"
    logs.mkdir(exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": "opencode", "backend": "podman", "workspace": str(env / workspace), "pid": os.getpid(),
        "session": fields.pop("session", None), "session_dir": str(env / "s" / name),
        "started": "2026-11-11T10:00:00Z", "proxy": True, "log_dir": str(logs), "ask": "on", **fields}))
    return directory, logs


def lines(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def refusals(directory, logs, run):
    """One refusal of every kind in RUN's logs (newest last)."""
    (directory / "approvals" / "1762855200-7-1.json").write_text(json.dumps({
        "id": "1762855200-7-1", "kind": "http", "time": "2026-11-11T10:00:01Z", "method": "POST", "url": URL,
        "host": "registry.npmjs.org", "path": "/-/npm/v1/security/advisories/bulk", "rule": "writes",
        "ruleset": RULESET, "process": {"comm": "opencode", "pid": 80, "exe": "/usr/bin/opencode"}}))
    (directory / "approvals" / "1762855200-7-1.done").write_text(json.dumps({
        "decision": "deny", "scope": "once", "reason": "no answer within 60 s", "question": "1762855200-7-1"}))
    (directory / "approvals" / "1762855200-7-2.json").write_text(json.dumps({
        "id": "1762855200-7-2", "kind": "connect", "time": "2026-11-11T10:00:02Z", "destination": "192.0.2.7:8443",
        "address": "192.0.2.7", "port": 8443}))
    (directory / "approvals" / "1762855200-7-2.done").write_text(json.dumps({"decision": "deny", "scope": "run",
                                                                             "reason": "answered"}))
    lines(logs / f"connections-20261111-100000-{run}.jsonl", [
        {"time": "2026-11-11T10:00:03Z", "event": "denied", "method": "DELETE", "url": "https://api.example/x",
         "rule": "no-deletes", "ruleset": "rules.json", "reason": "denied by rule no-deletes"},
        {"time": "2026-11-11T10:00:04Z", "event": "denied", "destination": "192.0.2.50:443",
         "reason": "cut by the kill switch (all: everything is cut); sandbox-ai net cut RUN off lifts it"},
        {"time": "2026-11-11T10:00:06Z", "event": "denied", "destination": "198.51.100.3:443",
         "reason": "not allowed by the network options", "process": {"comm": "curl", "pid": 9}}])
    lines(logs / "egress-denied.jsonl", [
        {"time": "2026-11-11T10:00:05Z", "host": "pypi.org", "reason": "not in allowed hosts", "sandbox": {"run": run}}])
    lines(logs / "mcp-calls.jsonl", [
        {"time": "2026-11-11T10:00:07Z", "event": "call", "server": "files", "tool": "rm", "decision": "deny",
         "rule": "x", "ruleset": "policy", "reason": "the administrator's policy refuses it"}])
    lines(directory / "safeguard-events.jsonl", [
        {"time": "2026-11-11T10:00:08Z", "kind": "mode", "what": "sandbox-ai net allow --allow", "why": "refused"}])


@pytest.fixture
def page(qapp, env, monkeypatch):
    sai = backend.SandboxAI()
    made = pages.ApprovalsPage(sai, interval=0, remote_interval=0)
    yield made
    made.timer.stop()
    made.blocked.timer.stop()
    made.deleteLater()


def settle(qapp, page, wait):
    assert wait(lambda: page.blocked.reading is None and page.allowed.reading is None, 20)
    qapp.processEvents()


def rows(table):
    return [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]


def test_the_page_follows_the_active_tab(env, qapp, page, wait):
    one, logs_one = fake_run(env, "opencode-1", "alpha")
    two, logs_two = fake_run(env, "opencode-2", "beta", session="night")
    refusals(one, logs_one, "opencode-1")
    lines(logs_two / "connections-20261111-100000-opencode-2.jsonl", [
        {"time": "2026-11-11T10:00:09Z", "event": "denied", "destination": "198.51.100.9:443",
         "reason": "not allowed by the network options"}])
    page.poll()
    assert sections.run_for_tab({"kind": "run", "id": "opencode-1"}, page.last_runs) == "opencode-1"
    assert sections.run_for_tab({"kind": "session", "name": "night", "id": "x"}, page.last_runs) == "opencode-2"
    assert sections.run_for_tab({"kind": "project", "path": str(env / "alpha")}, page.last_runs) == "opencode-1"
    assert sections.run_for_tab({"kind": "project", "path": str(env / "gamma")}, page.last_runs) is None
    page.set_active_tab({"kind": "project", "path": str(env / "alpha")})
    settle(qapp, page, wait)
    assert page.header.run() == "opencode-1"
    shown = rows(page.blocked.table)
    assert shown and all(row[1] == "opencode-1" for row in shown), "the active tab's run only"
    assert "198.51.100.9" not in " ".join(" ".join(row) for row in shown)
    page.set_active_tab({"kind": "session", "name": "night", "id": "x"})
    settle(qapp, page, wait)
    assert [row[1] for row in rows(page.blocked.table)] == ["opencode-2"]
    # The box chooses another one, or all of them.
    page.header.choice.setCurrentIndex(page.header.choice.findData(sections.ALL))
    page.header.changed.emit()
    settle(qapp, page, wait)
    assert {row[1] for row in rows(page.blocked.table)} == {"opencode-1", "opencode-2"}
    assert "Blocked (" in page.sections.tabText(page.sections.indexOf(page.blocked))


def test_blocked_lists_every_kind_newest_first_with_why(env, qapp, page, wait):
    directory, logs = fake_run(env, "opencode-1", "alpha", mode={"name": "high", "approval_scope": "run"},
                               vpn_type="tor", vpn_profile="tor")
    refusals(directory, logs, "opencode-1")
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    settle(qapp, page, wait)
    assert "security mode high" in page.header.state.text() and "through Tor" in page.header.state.text()
    entries = page.blocked.visible_entries()
    assert [e["time"] for e in entries] == sorted((e["time"] for e in entries), reverse=True)
    reasons = [row[2] for row in rows(page.blocked.table)]
    for label in ("Not allowed by the network settings", "Denied by a ruleset", "Not answered in time",
                  "Denied by you", "The security mode", "The administrator's policy", "The kill switch"):
        assert label in reasons, label
    # The filter: only what nobody answered.
    page.blocked.filter.setCurrentIndex(next(i for i in range(page.blocked.filter.count())
                                             if page.blocked.filter.itemData(i) == ("unanswered",)))
    assert [row[3] for row in rows(page.blocked.table)] == [f"POST {URL} (rule writes of {RULESET})"]
    page.blocked.table.selectRow(0)
    detail = page.blocked.detail.text()
    assert "Rule writes of the ruleset --rules tor-researcher.rules.json asks before it" in detail
    assert "does not change that" in detail
    assert rows(page.blocked.table)[0][4].startswith("opencode (pid 80")
    # Its actions: high approves for the run, not always - Allow always is not offered.
    assert page.blocked.allow_run.isEnabled() and page.blocked.ask_again.isEnabled()
    assert page.blocked.deny_run.isEnabled() and not page.blocked.allow_always.isEnabled()
    # The kill switch's refusal: nothing to do from here.
    page.blocked.filter.setCurrentIndex(next(i for i in range(page.blocked.filter.count())
                                             if page.blocked.filter.itemData(i) == ("cut",)))
    page.blocked.table.selectRow(0)
    assert not any(button.isEnabled() for button in page.blocked.buttons.values())
    assert "kill switch" in page.blocked.detail.text()


def test_the_blocked_reading_is_bounded(env, qapp, page, wait, monkeypatch):
    """The page reads through sandbox-ai's approvals.blocked (bounded: the newest part of each log), in a thread,
    for each run shown."""
    fake_run(env, "opencode-1", "alpha")
    calls = []
    approvals = page.sai.module("approvals")
    monkeypatch.setattr(approvals, "blocked", lambda record, runs: calls.append(record["run"]) or [])
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    settle(qapp, page, wait)
    assert calls[-1] == "opencode-1" and page.blocked.empty.text() == "Nothing was refused."
    assert approvals.BLOCKED_TAIL <= 8 << 20 and approvals.BLOCKED_LIMIT <= 1000


def act(qapp, page, wait, action, select):
    page.blocked.filter.setCurrentIndex(0)
    page.blocked.table.clearSelection()
    for row in range(page.blocked.table.rowCount()):
        if select(page.blocked.table.item(row, 3).text()):
            page.blocked.table.selectRow(row)
            break
    assert page.blocked.selected(), "a row to act on"
    page.blocked.act(action)
    assert wait(lambda: page.blocked.acting is None, 20)


def test_each_action_calls_its_command(env, qapp, page, wait, monkeypatch):
    directory, logs = fake_run(env, "opencode-1", "alpha")
    refusals(directory, logs, "opencode-1")
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    settle(qapp, page, wait)
    called, exceptions = [], []
    monkeypatch.setattr(page.sai, "call", lambda *argv, **kwargs: called.append(list(argv)) or (0, "{}", ""))
    approvals = page.sai.module("approvals")
    monkeypatch.setattr(approvals, "rule_exception", lambda record, entry, write=False: exceptions.append(
        (record.get("run"), entry.get("question"), write)) or {"rule": {"id": "allowed-1"}, "where": "other",
                                                               "path": "/c/x.json", "original": "/p/x.json"})
    http = lambda what: what.startswith("POST")  # noqa: E731
    act(qapp, page, wait, "allow-run", http)
    assert called[-1] == ["approvals", "allow", "1762855200-7-1", "--scope", "run"]
    act(qapp, page, wait, "ask-again", http)
    assert called[-1] == ["approvals", "ask-again", "1762855200-7-1"]
    act(qapp, page, wait, "deny-run", http)
    assert called[-1] == ["approvals", "deny", "1762855200-7-1", "--scope", "run"]
    widgets.SHOWN.clear()
    act(qapp, page, wait, "allow-always", http)
    assert exceptions == [("opencode-1", "1762855200-7-1", False), ("opencode-1", "1762855200-7-1", True)]
    assert called[-1] == ["approvals", "allow", "1762855200-7-1", "--scope", "run"]
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Allow always for this project?" and "stays as it is" in text and "/c/x.json" in text
    # A destination the network settings did not allow: as sandbox-ai net allow, --keep for always.
    lookup = lambda what: what == "lookup of pypi.org"  # noqa: E731
    act(qapp, page, wait, "allow-run", lookup)
    assert called[-1] == ["net", "allow", "opencode-1", "--allow-host", "pypi.org"]
    act(qapp, page, wait, "allow-always", lookup)
    assert called[-1] == ["net", "allow", "opencode-1", "--allow-host", "pypi.org", "--keep"]
    act(qapp, page, wait, "allow-run", lambda what: what.startswith("connection to 198.51.100.3"))
    assert called[-1] == ["net", "allow", "opencode-1", "--allow-tcp", "198.51.100.3:443"]
    # A connection you denied: taken back through the supervisor (it opens the firewall itself).
    act(qapp, page, wait, "allow-run", lambda what: what.startswith("connect to 192.0.2.7"))
    assert called[-1] == ["approvals", "allow", "1762855200-7-2", "--scope", "run"]
    assert page.blocked.message.text() == "Done."


def test_several_selected_act_together_and_a_failure_is_said(env, qapp, page, wait, monkeypatch):
    directory, logs = fake_run(env, "opencode-1", "alpha")
    refusals(directory, logs, "opencode-1")
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    settle(qapp, page, wait)
    called = []
    monkeypatch.setattr(page.sai, "call", lambda *argv, **kwargs: called.append(list(argv)) or (
        (1, "", "sandbox-ai approvals: the security mode paranoid approves at most once") if argv[1] == "allow"
        else (0, "", "")))
    page.blocked.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.MultiSelection)
    for row in range(page.blocked.table.rowCount()):
        if page.blocked.table.item(row, 2).text() in ("Not answered in time", "Denied by you"):
            page.blocked.table.selectRow(row)
    assert len(page.blocked.selected()) == 2 and page.blocked.ask_again.isEnabled()
    page.blocked.act("ask-again")
    assert wait(lambda: page.blocked.acting is None, 20)
    assert sorted(argv[2] for argv in called) == ["1762855200-7-1", "1762855200-7-2"]
    page.blocked.act("allow-run")
    assert wait(lambda: page.blocked.acting is None, 20)
    assert "approves at most once" in page.blocked.message.text()


def test_allowed_now_lists_and_revokes(env, qapp, page, wait, monkeypatch):
    fake_run(env, "opencode-1", "alpha")
    shown = {"run": "opencode-1", "cut": None, "all_ports": ["192.0.2.0/24"], "dns": ["192.0.2.53"],
             "host_wildcards": ["*.example.org"], "git": [], "host_ports": [8080],
             "https": [{"address": "192.0.2.10", "names": ["pypi.org"]}, {"address": "192.0.2.11", "names": []}],
             "tcp": [{"address": "198.51.100.3", "port": 443, "names": []}]}
    called = []

    def call(*argv, **kwargs):
        called.append(list(argv))
        return (0, json.dumps(shown), "") if argv[:2] == ("net", "show") else (0, "", "")
    monkeypatch.setattr(page.sai, "call", call)
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    settle(qapp, page, wait)
    page.sections.setCurrentWidget(page.allowed)
    page.allowed.refresh()
    settle(qapp, page, wait)
    destinations = [row[0] for row in rows(page.allowed.table)]
    assert destinations == ["192.0.2.11", "pypi.org", "198.51.100.3:443", "192.0.2.0/24", "192.0.2.53",
                            "127.0.0.1:8080", "*.example.org"]
    for row in range(page.allowed.table.rowCount()):
        if page.allowed.table.item(row, 0).text() in ("pypi.org", "198.51.100.3:443"):
            page.allowed.table.selectRow(row)
    page.allowed.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.MultiSelection)
    page.allowed.table.selectRow(next(r for r in range(page.allowed.table.rowCount())
                                      if page.allowed.table.item(r, 0).text() == "pypi.org"))
    page.allowed.revoke_selected()
    assert wait(lambda: any(argv[:2] == ["net", "revoke"] for argv in called), 20)
    revoke = next(argv for argv in called if argv[:2] == ["net", "revoke"])
    assert revoke[2] == "opencode-1" and "--allow-host" in revoke and "pypi.org" in revoke
    # The wildcard is listed, not revoked from here.
    assert next(r for r in sections.allowed_rows(shown) if r["destination"] == "*.example.org")["revoke"] is None


def test_a_refusal_sandbox_ai_offers_nothing_for_says_so_and_does_nothing(env, qapp, page, monkeypatch):
    """sandbox-ai offers no action for some refusals (a packet of another protocol: --allow would open every port of
    the address; a name carrying a canary token): the buttons are off, the detail says why, an action does nothing."""
    called = []
    monkeypatch.setattr(page.sai, "call", lambda *argv, **kwargs: called.append(argv) or (0, "", ""))
    blocked = page.blocked
    blocked.entries = [{"time": "2026-11-11T10:00:00Z", "run": "opencode-1", "kind": "network", "what":
                        "udp to 192.0.2.9:5353", "target": None, "actions": [], "count": 1},
                       {"time": "2026-11-11T10:00:01Z", "run": "opencode-1", "kind": "network",
                        "what": "lookup of x.canary.example", "target": None, "count": 1}]
    blocked.show_entries()
    for row in range(2):
        blocked.table.clearSelection()
        blocked.table.selectRow(row)
        assert "Nothing can be allowed here" in blocked.detail.text()
        assert not any(button.isEnabled() for button in blocked.buttons.values())
        for action in blocked.buttons:
            blocked.act(action)
    assert called == [] and blocked.acting is None


def test_enter_allows_for_the_run_and_delete_denies_in_the_list(env, qapp, page, monkeypatch):
    run, _ = fake_run(env, "opencode-1", "alpha")
    (run / "approvals" / "1762855200-7-9.json").write_text(json.dumps({
        "id": "1762855200-7-9", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "timeout": 60,
        "kind": "http", "method": "POST", "url": URL, "rule": "writes", "ruleset": RULESET}))
    page.poll()
    page.open.selectRow(0)
    assert "asks before it" in page.why.text() and "network settings do not change that" in page.why.text()
    answers = []
    monkeypatch.setattr(page, "answer", lambda decision, scope=None, **kwargs: answers.append((decision, scope)))
    enter = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    delete = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier)
    assert pages.shortcut_of(enter) == "allow-run" and pages.shortcut_of(delete) == "deny-key"
    assert pages.in_question_list(page.open.viewport()) and not pages.in_question_list(page.scope)
    page.shortcut("allow-run")
    assert answers[-1] == ("allow", "run")
    monkeypatch.setattr(page.deny_button, "click", lambda: answers.append(("deny", "run")))
    page.shortcut("deny-key")
    assert answers[-1] == ("deny", "run")
    # The Allow button says how long; nothing waiting says so.
    page.scope.setCurrentIndex(page.scope.findData("run"))
    assert page.allow_button.text() == "Allow for this run"
    (run / "approvals" / "1762855200-7-9.done").write_text(json.dumps({"decision": "deny", "reason": "answered"}))
    page.poll()
    assert page.none_waiting.text() == "Nothing is waiting for an answer." and not page.none_waiting.isHidden()


def test_enter_in_the_questions_window_does_what_its_allow_button_says(env, qapp, page, monkeypatch):
    """Review: the Questions window has no scope box and its button says Allow once, yet Enter in its list allowed
    for the whole run (and the confirmation's default is Allow: Enter twice). Enter is its Allow button now."""
    run, _ = fake_run(env, "opencode-1", "alpha")
    (run / "approvals" / "1762855200-7-9.json").write_text(json.dumps({
        "id": "1762855200-7-9", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "timeout": 60,
        "kind": "connect", "destination": "192.0.2.7:5432", "address": "192.0.2.7", "port": 5432}))
    page.poll()
    answers = []
    monkeypatch.setattr(page, "answer", lambda decision, scope=None, **kwargs: answers.append((decision, scope)))
    window = pages.QuestionsWindow(page)
    try:
        window.show_questions()
        assert window.current() is not None and window.allow_button.text() == "Allow once"
        window.shortcut("allow-run")
        assert answers == [("allow", "once")]
    finally:
        window.deleteLater()


def test_allowed_now_keeps_rows_of_the_same_text_apart(env, qapp, page, wait, monkeypatch):
    """Review: an HTTPS address without names and a network allowed on every port read the same; with one key for
    both, selecting one selected - and revoked - the two."""
    fake_run(env, "opencode-1", "alpha")
    shown = {"all_ports": ["192.0.2.10"], "dns": ["192.0.2.10"], "https": [{"address": "192.0.2.10", "names": []}]}
    called = []

    def call(*argv, **kwargs):
        called.append(list(argv))
        return (0, json.dumps(shown), "") if argv[:2] == ("net", "show") else (0, "", "")
    monkeypatch.setattr(page.sai, "call", call)
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    page.allowed.refresh()
    assert wait(lambda: page.allowed.reading is None and page.allowed.table.rowCount() == 3, 20)
    row = next(r for r in range(3) if page.allowed.table.item(r, 2).text() == "DNS (lookups)")
    page.allowed.table.selectRow(row)
    chosen = page.allowed.selected()
    assert [r["revoke"] for r in chosen] == [["--dns"]], chosen


def test_revoke_acts_on_the_run_its_rows_are_of(env, qapp, page, wait, monkeypatch):
    """Another sandbox chosen above: until its rows are read the list still shows the first one's, and Revoke takes
    them back there - never from the sandbox chosen meanwhile, which may not have them at all."""
    import threading
    fake_run(env, "opencode-1", "alpha")
    fake_run(env, "opencode-2", "beta")
    gate, called = threading.Event(), []

    def call(*argv, **kwargs):
        called.append(list(argv))
        if argv[:3] == ("net", "show", "opencode-2"):
            gate.wait(10)
        return (0, json.dumps({"https": [{"address": "192.0.2.10", "names": ["pypi.org"]}]}), "") \
            if argv[:2] == ("net", "show") else (0, "", "")
    monkeypatch.setattr(page.sai, "call", call)
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    page.allowed.refresh()
    assert wait(lambda: page.allowed.reading is None and page.allowed.table.rowCount() == 1, 20)
    page.header.choice.setCurrentIndex(page.header.choice.findData("opencode-2"))
    page.header.changed.emit()
    assert wait(lambda: ["net", "show", "opencode-2", "--json"] in called)
    page.allowed.table.selectRow(0)
    page.allowed.revoke_selected()
    gate.set()
    assert wait(lambda: any(argv[:2] == ["net", "revoke"] for argv in called), 20)
    assert next(argv for argv in called if argv[:2] == ["net", "revoke"])[2] == "opencode-1"
    settle(qapp, page, wait)


def test_allow_for_several_names_or_counts_every_one_it_allows(env, qapp, page, wait, monkeypatch):
    """The question before Allow for this run names what it allows; past the first ones it says how many more (it
    named 12 and allowed them all)."""
    directory, logs = fake_run(env, "opencode-1", "alpha")
    lines(logs / "egress-denied.jsonl", [
        {"time": f"2026-11-11T10:00:{n:02d}Z", "host": f"h{n}.example", "reason": "not in allowed hosts",
         "sandbox": {"run": "opencode-1"}} for n in range(15)])
    page.poll()
    page.set_active_tab({"kind": "run", "id": "opencode-1"})
    settle(qapp, page, wait)
    called = []
    monkeypatch.setattr(page.sai, "call", lambda *argv, **kwargs: called.append(list(argv)) or (0, "", ""))
    page.blocked.table.selectAll()
    assert len(page.blocked.selected()) == 15 and page.blocked.allow_run.isEnabled()
    widgets.SHOWN.clear()
    page.blocked.act("allow-run")
    assert wait(lambda: page.blocked.acting is None, 20)
    assert len([argv for argv in called if argv[:2] == ["net", "allow"]]) == 15
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Allow for this run?" and "lookup of h14.example" in text and "… and 3 more" in text
