"""A question about an MCP tool call shows the call's arguments in full before Allow (the review of the bluesky
server: a long post was approved from its first 300 characters): on the Approvals page and in the Questions window,
this machine's and another host's, escaped, opened by itself where the question's line shortens them."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from sandbox_ai_app import backend, pages, widgets  # noqa: E402

POST = {"text": "Anfang des Threads " + "lang " * 300 + "und das ENDE\u202e", "langs": ["de"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def page(qapp, env):
    made = pages.ApprovalsPage(backend.SandboxAI(), interval=0, remote_interval=0)
    yield made
    made.timer.stop()
    made.blocked.timer.stop()
    made.deleteLater()


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
    (run / "approvals" / f"{ident}.json").write_text(json.dumps({
        "id": ident, "time": asked, "timeout": 60, "kind": "mcp", "server": "bsky", "tool": "post", "rule": "posts",
        "ruleset": "rules.json", "arguments": short,
        "arguments_full": json.dumps(arguments, indent=2, ensure_ascii=False)}))


def select(page, ident):
    for row in range(page.open.rowCount()):
        page.open.selectRow(row)
        if page.current() and page.current()[1].get("id") == ident:
            return page.current()
    raise AssertionError(f"no row for {ident}")


def test_the_page_shows_a_long_call_whole_before_allow(page, env):
    run = fake_run(env)
    ask_call(run, "1700000000-1-1", POST, json.dumps({"text": POST["text"][:500] + "..."}))
    ask_call(run, "1700000000-1-2", {"text": "hi"}, json.dumps({"text": "hi"}))
    page.poll()
    assert page.open.rowCount() == 2
    select(page, "1700000000-1-1")
    box = page.arguments
    assert not box.isHidden() and box.toggle.isChecked() and not box.text.isHidden()  # opened by itself
    text = box.text.toPlainText()
    assert "und das ENDE" in text and "\u202e" not in text and "\\u202e" in text  # whole, a bidi override escaped
    assert json.loads(text) == POST and box.text.isReadOnly() and box.text.font().styleHint() == widgets.MONO.styleHint()
    assert "shortened" in box.note.text() and "credentials redacted" in box.note.text()
    assert "arguments shortened here: in full below" in page.open.item(page.open.currentRow(), 2).text()
    box.toggle.setChecked(False)  # closed by hand: the next poll keeps it closed
    page.poll()
    select(page, "1700000000-1-1")
    assert box.text.isHidden() and box.toggle.text() == "Show the arguments in full"
    select(page, "1700000000-1-2")  # whole in its line: there, but closed
    assert not box.isHidden() and not box.toggle.isChecked() and box.text.isHidden()
    page.open.clearSelection()
    assert box.isHidden()


def test_the_questions_window_shows_them_too(page, env):
    run = fake_run(env)
    ask_call(run, "1700000000-1-1", POST, json.dumps({"text": "Anfang..."}))
    page.poll()
    window = pages.QuestionsWindow(page)
    try:
        window.show_questions()  # one question: chosen for you
        assert not window.arguments.isHidden() and "und das ENDE" in window.arguments.text.toPlainText()
    finally:
        window.deleteLater()


def test_another_host_s_details_are_kept_escaped_and_bounded():
    question = backend.RemoteQuestion("ct1", {"id": "1700000000-1-1", "run": "claude-r", "kind": "mcp",
                                              "question": "call the MCP tool post", "details": '{\n  "t": "\x1b[2J"\n}',
                                              "details_whole": False})
    text, whole = backend.question_details(question)
    assert text == '{\n  "t": "\\x1b[2J"\n}' and whole is False
    big = backend.RemoteQuestion("ct1", {"id": "1700000000-1-2", "run": "claude-r", "kind": "mcp",
                                         "details": "x" * (backend.DETAILS_LIMIT + 5)})
    assert big["details"].endswith("\n… 5 more characters not shown") and big["details_whole"] is False
    assert backend.question_details(backend.RemoteQuestion("ct1", {"id": "1700000000-1-3", "run": "r"})) == ("", True)


def ask_tool(run, ident, arguments, short):
    asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (run / "approvals" / f"{ident}.json").write_text(json.dumps({
        "id": ident, "time": asked, "timeout": 60, "kind": "tool", "agent": "claude", "tool": "Bash",
        "rule": "bash-asks", "ruleset": "rules.json", "arguments": short, "call_digest": "0" * 32,
        "arguments_full": json.dumps(arguments, indent=2, ensure_ascii=False)}))


def test_the_agents_own_tool_shows_its_arguments_whole_too(page, env):
    """A question about the agent's own tool (kind "tool": Bash, Edit, ...) as an MCP call's: whole before Allow."""
    run = fake_run(env)
    command = {"command": "echo start && " + "true && " * 120 + "rm -rf /work/x # ENDE‮"}
    ask_tool(run, "1700000000-1-4", command, json.dumps({"command": command["command"][:500] + "..."}))
    ask_tool(run, "1700000000-1-5", {"command": "ls"}, json.dumps({"command": "ls"}))
    page.poll()
    select(page, "1700000000-1-4")
    box = page.arguments
    assert not box.isHidden() and box.toggle.isChecked()
    assert json.loads(box.text.toPlainText()) == command and "\\u202e" in box.text.toPlainText()
    assert "(arguments shortened here: in full below)" in page.open.item(page.open.currentRow(), 2).text()
    select(page, "1700000000-1-5")
    assert not box.isHidden() and not box.toggle.isChecked()
    assert "shortened" not in page.open.item(page.open.currentRow(), 2).text()


def test_a_write_across_a_session_bridge_is_answered_here_with_its_diff(page, env):
    """sandbox-ai bridge asks from the bridge's own folder (not a run's): its questions are on the page all the same,
    a write with its diff in full before Allow."""
    fake_run(env)  # (the page reads questions while something runs)
    folder = env / "state" / "sandbox-ai" / "bridges" / "0badcafe" / "approvals"
    folder.mkdir(parents=True)
    # Only a living bridge's questions are listed (deep test round 1): its record active, its broker running.
    (folder.parent / "bridge.json").write_text(json.dumps({"id": "0badcafe", "state": "active",
                                                           "broker_pid": os.getpid()}))
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('one')\n+print('two')\n"
    (folder / "1700000000-7-1.json").write_text(json.dumps({
        "id": "1700000000-7-1", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "timeout": 300,
        "kind": "bridge", "bridge": "0badcafe", "from": "alpha", "to": "beta", "tool": "write_file",
        "arguments": "write src/app.py (13 bytes)", "arguments_full": diff, "write": True}))
    page.poll()
    question = select(page, "1700000000-7-1")[1]
    assert question["kind"] == "bridge"
    assert "alpha wants to write_file on beta" in page.open.item(page.open.currentRow(), 2).text()
    assert "this one write only" in page.open.item(page.open.currentRow(), 2).text()
    page.arguments.toggle.setChecked(True)
    assert "+print('two')" in page.arguments.text.toPlainText()
