"""Independent feature review 1 of sandbox-ai 0.1.95 (the app's part): each test failed before its fix
(docs/releases/v0.1.95.md, "Feature review 1")."""
import json
import os
import time
import types

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from sandbox_ai_app import backend, pages, widgets  # noqa: E402

# A post of about 60 KiB outside ASCII: its question file holds it escaped once more (ü: six bytes for two).
POST = {"text": "Grüße aus dem Sandkasten, äöü " * 2000, "langs": ["de"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def fake_run(env, name="claude-abc"):
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    (env / "project").mkdir(exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": "claude", "backend": "podman", "workspace": str(env / "project"), "pid": os.getpid(),
        "session": None, "session_dir": str(env / "s"), "started": "2026-11-13T10:00:00Z", "proxy": True}))
    return directory


def ask_call(run, ident, arguments, short):
    asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = run / "approvals" / f"{ident}.json"
    # As the supervisor writes it (json.dumps: every character outside ASCII escaped).
    path.write_text(json.dumps({
        "id": ident, "time": asked, "timeout": 60, "kind": "mcp", "server": "bsky", "tool": "post", "rule": "posts",
        "ruleset": "rules.json", "arguments": short,
        "arguments_full": json.dumps(arguments, indent=2, ensure_ascii=False)}))
    return path


def test_a_call_with_long_arguments_is_asked_on_the_approvals_page(qapp, env):
    """Its question file holds the call's arguments in full (up to 64 KiB, escaped once more): the app's question
    cache read at most 64 KiB of a file and took a longer one for nothing - the question never showed on the page,
    while the app's heartbeat kept every launcher from notifying of it, so it was refused unanswered."""
    run = fake_run(env)
    path = ask_call(run, "1700000000-1-1", POST, json.dumps({"text": POST["text"][:500] + "..."}))
    assert path.stat().st_size > (64 << 10)
    page = pages.ApprovalsPage(backend.SandboxAI(), interval=0, remote_interval=0)
    try:
        page.poll()
        assert page.open.rowCount() == 1
        page.open.selectRow(0)
        assert page.current()[1].get("id") == "1700000000-1-1"
        assert "Sandkasten" in page.arguments.text.toPlainText()
    finally:
        page.timer.stop()
        page.blocked.timer.stop()
        page.deleteLater()


def test_a_notification_offers_no_allow_for_a_call_it_cannot_show_whole(qapp, monkeypatch):
    """sandbox-ai's own desktop notification offers no Allow for a tool call whose arguments it cannot show whole
    (release notes); the app's notification of a new question offered Allow (and Allow always…) for it all the same -
    one click let a call through whose arguments were never seen. Block and the way to the page stay."""
    from sandbox_ai_app import ui
    monkeypatch.setattr(backend, "setting", lambda key, *a: True if key in ("notify", "notifications") else
                        backend.SETTINGS.get(key))
    shown = []
    window = types.SimpleNamespace(question_toasts={}, pending_count=0, close_settled_toasts=lambda: None,
                                   show_question_count=lambda: None, show_asking=lambda: None,
                                   notify=lambda title, text, level="warning", action=None: shown.append(action))
    window.question_actions = lambda *args, **kwargs: ui.MainWindow.question_actions(window, *args, **kwargs)
    long_call = {"id": "1700000000-1-1", "kind": "mcp", "server": "bsky", "tool": "post", "rule": "r",
                 "ruleset": "rules.json", "arguments": json.dumps({"text": "Anfang..."}),
                 "arguments_full": json.dumps(POST, indent=2)}
    short_call = {**long_call, "id": "1700000000-1-2", "arguments": json.dumps({"text": "hi"}),
                  "arguments_full": json.dumps({"text": "hi"}, indent=2)}
    ui.MainWindow.questions_changed(window, 2, [("claude-abc", long_call, ("local", "claude-abc", "1700000000-1-1")),
                                                ("claude-abc", short_call, ("local", "claude-abc", "1700000000-1-2"))])
    labels = [[label for label, _ in actions] for actions in shown]
    assert labels[0] == ["Block", "Block always…", "Open approvals"], labels
    assert labels[1] == ["Allow", "Allow always…", "Block", "Block always…", "Open approvals"], labels
