"""The Canaries tab of the Activity page, offscreen: events in the theme's "bad" colour with their process and,
unfolded, the tool call, prompt turn and model requests linked to them; once from a fake log, once from the real
`sandbox-ai canaries log --json` of the checkout in SANDBOX_AI_HOME (state written here, no sandbox runs)."""
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, canaries_view, i18n, theme  # noqa: E402
from sandbox_ai_app.activity_page import ActivityPage  # noqa: E402
from sandbox_ai_app.canaries_view import CanariesView, process_text  # noqa: E402

LOG = {"run": "claude-1", "agent": "claude", "status": "ended", "activity": True, "events": [
    {"time": "2026-11-07T10:00:05Z", "run": "claude-1", "canary": "aws", "path": "/root/.aws/credentials",
     "event": "read", "action": "report", "summary": "something in the sandbox opened the decoy file",
     "process": {"pid": 41, "ns_pid": 21, "comm": "cat", "argv": ["cat", "/root/.aws/credentials"],
                 "parents": ["bash", "claude"]},
     "processes": [{"pid": 41, "comm": "cat"}, {"pid": 42, "comm": "python3"}],
     "tool_calls": [{"id": "t2", "time": "2026-11-07T10:00:04Z", "match": "names the file",
                     "summary": "cat ~/.aws/credentials"}],
     "turn": {"id": "p1", "time": "2026-11-07T10:00:00Z", "summary": "tidy up the scripts"},
     "requests": [{"position": "before", "time": "2026-11-07T10:00:03Z", "method": "POST",
                   "url": "https://api.anthropic.com/v1/messages", "status": 200, "activity_id": "n1"},
                  {"position": "carried the token", "time": "2026-11-07T10:00:07Z", "method": "POST",
                   "url": "https://api.anthropic.com/v1/messages", "status": 200, "activity_id": None,
                   "log": "connections-1-claude-1.jsonl", "offset": 812}]},
    {"time": "2026-11-07T10:01:30Z", "run": "claude-1", "canary": "netrc", "path": "/root/.netrc", "event": "read",
     "action": "log", "process": {"unknown": "no process held the file open when it was looked up (closed already)"},
     "tool_calls": [], "turn": None, "requests": []}]}


class FakeSai:
    def __init__(self, logs=None, error=None, has=True):
        self.logs, self.error, self.has, self.calls = logs, error, has, 0

    def has_canary_log(self):
        return self.has

    def canary_log(self, run=None):
        self.calls += 1
        if self.error:
            raise ValueError(self.error)
        return self.logs


def texts(item):
    return [item.child(i).text(0) for i in range(item.childCount())]


def test_events_are_shown_in_the_bad_colour_with_their_links(qapp, wait):
    view = CanariesView(FakeSai([LOG]))
    view.refresh()
    assert wait(lambda: view.tree.topLevelItemCount() == 2)
    newest, first = view.tree.topLevelItem(0), view.tree.topLevelItem(1)
    assert newest.text(2).startswith("netrc") and first.text(2) == "aws  /root/.aws/credentials"
    assert first.text(1) == "read (report)"
    assert first.text(3) == "cat (pid 41, 21 in the sandbox): cat /root/.aws/credentials ← bash ← claude"
    assert newest.text(3).startswith("unknown (no process held the file open")
    bad = theme.color("bad")
    assert all(first.foreground(column).color() == bad for column in range(5))
    lines = texts(first)
    assert lines[0].startswith("Tool call (names the file): cat ~/.aws/credentials") and "activity t2" in lines[0]
    assert lines[1].startswith("Prompt turn: tidy up the scripts") and "activity p1" in lines[1]
    assert "Model request (before): POST https://api.anthropic.com/v1/messages → 200" in lines[2]
    assert "activity n1" in lines[2]
    assert lines[3].startswith("Model request (carried the token)") and "connections-1-claude-1.jsonl @812" in lines[3]
    assert first.child(3).foreground(0).color() == bad
    assert lines[4] == "Also held open by: python3 (pid 42)"
    assert texts(newest) == ["Nothing of the agent's in that time window."]
    assert view.status.text() == "2 canary event(s) in 1 run(s)"
    view.tree.setCurrentItem(first.child(0))
    assert '"tool_calls"' in view.detail.toPlainText() and "t2" in view.detail.toPlainText()


def test_errors_an_old_sandbox_ai_and_nothing_touched(qapp, wait):
    view = CanariesView(FakeSai(error="boom"))
    view.refresh()
    assert wait(lambda: "boom" in view.status.text())
    old = CanariesView(FakeSai(has=False))
    old.refresh()
    assert "no canary log yet" in old.status.text() and old.sai.calls == 0
    empty = CanariesView(FakeSai([{"run": "x", "events": []}]))
    empty.refresh()
    assert wait(lambda: "No canary was touched" in empty.status.text())
    assert process_text({"unknown": "x\x1b[31m"}) == "unknown (x\\x1b[31m)"  # escaped: the agent names its processes


def test_the_activity_page_has_a_canaries_tab_that_counts_them(qapp):
    page = ActivityPage(FakeSai([LOG]))
    assert [page.tabs.tabText(i) for i in range(page.tabs.count())] == ["Timeline", "Canaries", "Safeguards"]
    page.canaries.show_events(canaries_view.events_of([LOG]))
    index = page.tabs.indexOf(page.canaries)
    assert page.tabs.tabText(index) == "Canaries (2)"
    assert page.tabs.tabBar().tabTextColor(index) == theme.color("bad")


def test_the_tab_in_german(qapp):
    i18n.set_language("de")
    try:
        view = CanariesView(FakeSai([LOG]))
        view.show_events(canaries_view.events_of([LOG]))
        first = view.tree.topLevelItem(1)
        assert texts(first)[0].startswith("Tool-Aufruf (nennt die Datei)")
        assert texts(first)[3].startswith("Modellanfrage (trug das Token)")
        assert view.tree.topLevelItem(0).text(3).startswith("unbekannt (")
        assert view.tree.headerItem().text(3) == "Prozess"
    finally:
        i18n.set_language("en")


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
def test_the_real_canaries_log_command(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run, logs = "claude-c4n4ry", tmp_path / "logs"
    runs = tmp_path / "state" / "sandbox-ai" / "canaries" / "runs"
    runs.mkdir(parents=True)
    logs.mkdir()
    (runs / f"{run}.json").write_text(json.dumps({
        "run": run, "status": "ended", "agent": "claude", "started": "2026-11-07T09:59:00Z", "log_dir": str(logs),
        "model_hosts": ["api.anthropic.com"],
        "canaries": [{"id": "aws", "sandbox_path": "/root/.aws/credentials", "tokens": {"key": "AKIAEXAMPLETOKEN99"}}]}))
    (runs / f"{run}.events.jsonl").write_text(json.dumps({
        "time": "2026-11-07T10:00:05Z", "run": run, "event": "read", "source": "files", "canary": "aws",
        "path": "/root/.aws/credentials", "action": "report",
        "process": {"pid": 41, "comm": "cat", "argv": ["cat", "AKIAEXAMPLETOKEN99"]}}) + "\n")
    (logs / "activity.jsonl").write_text(json.dumps({
        "v": 1, "time": "2026-11-07T10:00:04Z", "run": run, "agent": "claude", "kind": "command", "id": "t2",
        "summary": "cat ~/.aws/credentials", "details": {"command": "cat ~/.aws/credentials"}, "source": "claude"}) + "\n")
    (logs / f"connections-1-{run}.jsonl").write_text(json.dumps({
        "time": "2026-11-07T10:00:08Z", "event": "http", "host": "api.anthropic.com", "method": "POST",
        "url": "https://api.anthropic.com/v1/messages", "status": 200}) + "\n")
    sai = backend.SandboxAI()
    assert sai.has_canary_log()
    found = sai.canary_log(run)
    event = found[0]["events"][0]
    assert event["tool_calls"][0]["id"] == "t2" and event["requests"][0]["position"] == "after"
    assert "AKIAEXAMPLETOKEN99" not in json.dumps(found)
    view = CanariesView(sai)
    view.show_events(canaries_view.events_of(found))
    assert view.tree.topLevelItem(0).text(3) == "cat (pid 41): cat [redacted]"
    assert Path(sai.directory, "canary.py").is_file()
