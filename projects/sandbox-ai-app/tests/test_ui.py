"""The windows, offscreen: every page builds, the form drives the command line, the new pages work against
a fake run registry, log directory and session (no sandbox runs in these tests)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from sandbox_ai_app import backend, i18n, theme, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
HERE = Path(__file__).resolve().parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


@pytest.fixture
def window(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # confirm() says yes, messages are collected
    monkeypatch.setattr(widgets, "SHOWN", [])
    started = []
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:  # tests choose their tray (see FakeTray)
        w.tray.hide()
        w.tray = None
    w.sai.in_terminal = lambda argv, title="": ["term", *argv]
    monkeypatch.setattr("sandbox_ai_app.ui.QProcess.startDetached", lambda program, args: started.append([program, *args]) or True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    w.started = started
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    # Without an event loop deleteLater() never runs: every test's window would stay alive (and be restyled,
    # polled and repainted by all later tests).
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def fake_run(env, name="claude-abc", session=None):
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    workspace = env / "project"
    workspace.mkdir(exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": "claude", "backend": "podman", "workspace": str(workspace), "pid": os.getpid(),
        "session": session, "session_dir": str(env / "s"), "started": "2026-11-03T10:00:00Z", "proxy": True}))
    return directory


def ask(run, ident, seconds_ago=0, **question):
    asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))
    (run / "approvals" / f"{ident}.json").write_text(json.dumps({"id": ident, "time": asked, "timeout": 60, **question}))


def texts(widget):
    """Every text the user can see in WIDGET (labels, buttons, line edits, table cells)."""
    found = []
    for child in widget.findChildren(QtWidgets.QWidget):
        for getter in ("text", "toPlainText", "title", "placeholderText"):
            if hasattr(child, getter) and not isinstance(child, QtWidgets.QLineEdit) or getter == "text" and \
                    isinstance(child, QtWidgets.QLineEdit) and child.echoMode() == QtWidgets.QLineEdit.EchoMode.Normal:
                try:
                    found.append(str(getattr(child, getter)()))
                except TypeError:
                    pass
        if isinstance(child, QtWidgets.QTableWidget):
            found += [child.item(r, c).text() for r in range(child.rowCount()) for c in range(child.columnCount())
                      if child.item(r, c)]
    return found


def row_of(table, text, column=0):
    """The visual row whose COLUMN shows TEXT."""
    return next(r for r in range(table.rowCount()) if table.item(r, column).text() == text)


def test_fill_keeps_row_keys_and_the_selection_across_sorting(qapp):
    t = widgets.table(["Name", "Count"], numeric=(1,))
    widgets.fill(t, [["b", 2], ["a", 1], ["c", 3]], keys=["kb", "ka", "kc"])
    assert [t.item(r, 0).text() for r in range(3)] == ["b", "a", "c"], "the order given, until a header is clicked"
    t.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert [t.item(r, 0).text() for r in range(3)] == ["a", "b", "c"]
    assert [widgets.row_key(t, r) for r in range(3)] == ["ka", "kb", "kc"]
    t.selectRow(widgets.find_row(t, "kc"))
    widgets.fill(t, [["c", 3], ["d", 4], ["a", 1]], keys=["kc", "kd", "ka"])
    assert widgets.selected_keys(t) == ["kc"] and t.item(widgets.selected_rows(t)[0], 0).text() == "c"
    widgets.fill(t, [["d", 4], ["a", 1]], keys=["kd", "ka"])
    assert widgets.selected_keys(t) == [], "a selected row that is gone leaves nothing selected, not its successor"
    widgets.bars(t, 1, [4, 1])
    assert t.item(widgets.find_row(t, "kd"), 1).data(widgets.BAR_ROLE) == 1.0
    assert t.item(widgets.find_row(t, "ka"), 1).data(widgets.BAR_ROLE) == 0.25


# -- the existing pages --


def test_pages_and_preview(window):
    assert [window.nav.item(i).text() for i in range(window.nav.count())] == [
        "PROJECT", "Start", "Board", "RUNNING", "Sandboxes & sessions", "Approvals", "Terminals", "INSIGHT",
        "Connections", "Activity", "Statistics", "Files", "MANAGE", "Kits & plugins", "MCP servers", "Images", "Credentials",
        "SYSTEM", "System", "Hosts", "Help"]
    launch = window.launch
    launch.agent.setCurrentText("codex")
    launch.form.widgets["--dangerously-skip-permissions"].setChecked(True)
    launch.form.widgets["--allow-host"].set_value(["example.org"])
    launch.form.widgets["--profile"].boxes["development"].setChecked(True)
    launch.agent_args.setText("--model gpt")
    assert launch.preview.toPlainText() == ("sandbox-ai codex --profile development --allow-host example.org "
                                            "--dangerously-skip-permissions -- --model gpt")
    launch.start()
    started = window.started[-1]
    assert started[0] == "term" and started[2:4] == ["-I", str(window.sai.directory / "main.py")]
    assert started[4:] == ["codex", "--profile", "development", "--allow-host", "example.org",
                           "--dangerously-skip-permissions", "--", "--model", "gpt"]


def test_the_intro_can_be_watched_again(window, qapp):
    """Help > Watch the intro again and the button in Settings play it over the window, skippable as at the
    start; no setting changes and the guided tour is not offered for it."""
    before, offered = window.sai.settings(), window.state.get("tour_offered", False)
    window.actions["intro"].trigger()
    played = window.intro
    assert played is not None and not played.isHidden() and played.geometry() == window.rect()
    window.actions["intro"].trigger()
    assert window.intro is played, "one at a time"
    played.skip()
    dialog = window.settings_dialog()
    dialog.replay.click()
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted, "the dialog makes way for it"
    assert window.intro is not played and not window.intro.isHidden()
    window.intro.skip()
    assert window.sai.settings() == before and window.state.get("tour_offered", False) == offered


def test_profile_round_trip_through_the_form(window):
    launch = window.launch
    window.sai.save_profile("p", "opencode", ["-o", "--gpu", "dri", "-w", "/tmp/x"])
    launch.refresh_profiles()
    launch.profile.setCurrentText("p")
    launch.load_profile()
    assert launch.agent.currentText() == "opencode"
    assert launch.form.widgets["--offline"].isChecked() and launch.form.widgets["--gpu"].currentText() == "dri"
    assert launch.argv() == ["--workspace", "/tmp/x", "--gpu", "dri", "--offline"]
    assert launch.workspace() == "/tmp/x"


def test_loading_a_profile_keeps_the_projects_workspace(window, env):
    """Regression: a profile without --workspace emptied the form's workspace, so the start ran in whatever folder
    the terminal opened in (the launcher's default workspace) instead of the current project."""
    project = env / "project"
    project.mkdir()
    launch = window.launch
    launch.set_project(str(project))
    window.sai.save_profile("pypi", "codex", ["--allow-pypi"])
    window.sai.save_profile("elsewhere", "codex", ["-w", "/tmp/x"])
    launch.refresh_profiles()
    launch.profile.setCurrentText("pypi")
    launch.load_profile()
    assert launch.argv() == ["--workspace", str(project), "--allow-pypi"] and launch.workspace() == str(project)
    launch.start()
    assert window.started[-1][4:] == ["codex", "--workspace", str(project), "--allow-pypi"]
    launch.profile.setCurrentText("elsewhere")
    launch.load_profile()
    assert launch.workspace() == "/tmp/x", "a profile's own workspace wins"


def test_connections_page_shows_a_log(window, env, wait):
    logs = env / "state" / "sandbox-ai" / "logs" / "app-123"
    logs.mkdir(parents=True)
    (logs / "connections-20261103-x.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"time": "t1", "event": "http", "method": "GET", "url": "https://a/x", "status": 200},
        {"time": "t2", "event": "denied", "destination": "192.0.2.1:22", "reason": "SSH"}]) + "\n")
    page = window.connections
    page.refresh_directories()  # read in a thread
    assert wait(lambda: page.table.rowCount() == 2)
    page.refused.setChecked(True)
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "denied"
    page.table.selectRow(0)
    assert '"reason": "SSH"' in page.detail.toPlainText()
    # Sorted, a row still shows its own record (here: "denied" moves above "http").
    page.refused.setChecked(False)
    page.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    assert '"reason": "SSH"' in page.detail.toPlainText(), "the selection stays on its record across a refill"
    page.detail.clear()
    page.table.clearSelection()
    page.table.selectRow(row_of(page.table, "denied", 1))
    assert '"reason": "SSH"' in page.detail.toPlainText()
    page.table.selectRow(row_of(page.table, "http", 1))
    assert "https://a/x" in page.detail.toPlainText()


def test_connections_follow_past_5000_records_and_only_while_shown(window, env, monkeypatch, wait):
    logs = env / "state" / "sandbox-ai" / "logs" / "app-9"
    logs.mkdir(parents=True)
    log = logs / "connections-1.jsonl"
    log.write_text("".join(json.dumps({"time": f"t{i}", "event": "http", "url": f"https://pypi.org/{i}"}) + "\n"
                           for i in range(5000)))
    page = window.connections
    page.refresh_directories()
    assert wait(lambda: page.table.rowCount() == 5000, 30)

    def append(when):
        with open(log, "a") as stream:
            stream.write(json.dumps({"time": when, "event": "denied", "destination": "203.0.113.66:443"}) + "\n")
    append("t-new")
    page.load()
    assert wait(lambda: not widgets.busy(page), 30)
    assert page.table.rowCount() == 5000 and page.table.item(4999, 0).text() == "t-new"
    page.table.selectRow(4999)
    reads = []
    real = backend.read_connections
    monkeypatch.setattr(backend, "read_connections", lambda *a, **k: reads.append(a) or real(*a, **k))
    page.load()
    assert wait(lambda: not widgets.busy(page))
    assert reads == [], "an unchanged log is not read again"
    append("t-newer")
    assert not page.isVisible()
    page.tick()
    assert reads == [], "a hidden page does not read the log"
    page.load()
    assert wait(lambda: not widgets.busy(page), 30)
    assert len(reads) == 1 and page.table.item(4999, 0).text() == "t-newer"
    selected = widgets.selected_rows(page.table)
    assert len(selected) == 1 and page.record_of(selected[0])["time"] == "t-new", "the selection stays on its record"



def test_connections_follow_reads_at_its_interval_and_appends_what_came(window, env, monkeypatch, wait):
    """Regression: with follow on, every finished read started the next at once - up to 16 MiB read and 5000
    rows refilled again and again (about a second of GUI thread each time). The next read waits for the
    interval, and records appended to the log are added to the table, not refilled."""
    from sandbox_ai_app import ui
    logs = env / "state" / "sandbox-ai" / "logs" / "app-follow"
    logs.mkdir(parents=True)
    log = logs / "connections-1.jsonl"
    log.write_text("".join(json.dumps({"time": f"t{i}", "event": "http", "url": f"https://a/{i}"}) + "\n"
                           for i in range(300)))
    page = window.connections
    page.timer.setInterval(60000)  # the test drives the ticks
    window.show()
    window.show_page("connections")
    assert page.isVisible() and page.follow.isChecked()
    page.refresh_directories()
    assert wait(lambda: page.table.rowCount() == 300 and not widgets.busy(page), 30)
    loads, fills = [], []
    real_load, real_fill = page.load, ui.fill
    monkeypatch.setattr(page, "load", lambda: loads.append(1) or real_load())
    monkeypatch.setattr(ui, "fill", lambda *a, **k: fills.append(1) or real_fill(*a, **k))
    with open(log, "a") as stream:
        stream.write("".join(json.dumps({"time": f"n{i}", "event": "denied", "destination": "192.0.2.9:22"}) + "\n"
                             for i in range(3)))
        stream.write('{"time": "half a line')  # being written: read once it is complete
    page.tick()
    assert wait(lambda: page.table.rowCount() == 303 and not widgets.busy(page), 30)
    wait(lambda: False, 0.5)
    assert len(loads) == 1, "no read right after a read: the next one waits for the interval"
    assert fills == [], "appended records are added to the table, it is not filled again"
    assert [page.table.item(r, 0).text() for r in (299, 300, 302)] == ["t299", "n0", "n2"]
    assert page.table.item(302, 1).text() == "denied"
    page.table.selectRow(301)
    assert page.record_of(301)["time"] == "n1"
    with open(log, "a") as stream:
        stream.write('"}\n')
    page.tick()
    assert wait(lambda: page.table.rowCount() == 304, 30) and fills == []
    assert page.record_of(303) == {"time": "half a line"}
    assert page.record_of(widgets.selected_rows(page.table)[0])["time"] == "n1", "the selection stays"
    page.filter.setText("n1")
    assert page.table.rowCount() == 1, "a filter change fills the table again"


def test_sandboxes_and_system_pages_build(window, wait):
    window.sandboxes.refresh()
    assert window.sandboxes.table.rowCount() == 0
    window.mcp.refresh()  # read in the background
    assert wait(lambda: window.mcp.servers.rowCount() >= 1, 15)


def test_sandboxes_lists_and_manages_the_other_hosts_starts(window, wait, monkeypatch):
    """Beside this account's own: the starts of the hosts of `sandbox-ai hosts` (read over SSH in the background),
    each row naming its host; pause, resume, stop, delete and attach run on that host (sandbox-ai there), each
    confirmation naming it; what a run may reach stays with that host; a host that does not answer is said."""
    page = window.sandboxes
    there = backend.Instance("session", "ct1:web", "web", project="/srv/app", agent="codex", state="running",
                             record={"attachable": True}, host="ct1")
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: ["ct1", "ct2"])
    monkeypatch.setattr(window.sai, "host_instances",
                        lambda name, timeout=60: [there] if name == "ct1" else (_ for _ in ()).throw(
                            ValueError("ssh: connection timed out")))
    ran, opened = [], []
    monkeypatch.setattr(window.sai, "host_run", lambda name, *args, timeout=120: ran.append((name, args)) or (0, "", ""))
    monkeypatch.setattr(window.sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, *args])
    monkeypatch.setattr(page, "terminal", lambda argv, title, embedded=False: opened.append((argv, title)))
    page.refresh_remote()
    assert wait(lambda: page.table.rowCount() == 1, 15)
    assert page.table.item(0, 1).text() == "web" and page.table.item(0, 8).text() == "ct1"
    assert "1 on other hosts" in page.summary.text() and "ct2: no answer" in page.summary.text()
    page.table.selectRow(0)
    assert "On ct1" in page.detail.text()
    assert page.stop_button.isEnabled() and page.session_buttons["pause"].isEnabled()
    assert not page.session_buttons["resume"].isEnabled() and not any(b.isEnabled() for b in page.run_buttons)
    page.session("attach")
    assert opened == [(["ssh", "-tt", "ct1", "attach", "web"], "ct1: web")]
    page.session("pause")
    assert wait(lambda: ("ct1", ("pause", "web")) in ran, 15)
    page.stop()
    assert wait(lambda: ("ct1", ("stop", "web")) in ran, 15)
    page.refresh()  # this account's own read again: the other hosts' rows stay
    assert page.table.rowCount() == 1 and page.table.item(0, 8).text() == "ct1"


# -- approvals --


def test_approvals_page_lists_counts_down_and_answers(window, env, wait):
    run = fake_run(env)
    ask(run, "1700000000-1-1", seconds_ago=15, kind="connect", destination="192.0.2.7:5432", address="192.0.2.7",
        port=5432, host="db.example")
    ask(run, "1700000000-1-2", seconds_ago=5, kind="browser", url="https://login.example/device?code=\x1b[31mX")
    notes = []
    window.notify = lambda title, text, **_: notes.append((title, text))
    page = window.approvals
    page.poll()
    assert page.open.rowCount() == 2
    # Several sandboxes may ask at once: the row names run and workspace.
    assert page.open.item(0, 0).text() == f"claude-abc ({env / 'project'})" and page.open.item(0, 1).text() == "connection"
    assert "192.0.2.7:5432" in page.open.item(0, 2).text() and "not verified" in page.open.item(0, 2).text()
    left = int(page.open.item(0, 4).text().split()[0])
    assert 40 <= left <= 45, page.open.item(0, 4).text()
    assert page.open.item(1, 4).text() == "no time limit" and page.open.item(1, 2).font().bold()
    assert page.current() is None and not page.allow_button.isEnabled(), "two questions: nothing chosen for you"
    assert any(f"within {window.sai.approval_seconds} seconds" in text for text in texts(page.open_box))
    nav = window.nav_item("approvals")
    assert nav.text() == "Approvals  (2)" and nav.font().bold()
    assert len(notes) == 1 and notes[0][0] == f"sandbox-ai asks: claude-abc ({env / 'project'})"
    page.poll()
    assert len(notes) == 1, "a question is announced once"
    # A browser question shows its URL prominently, escaped.
    page.open.selectRow(1)
    assert page.url.isVisible() or not page.isVisible()
    assert page.url.text() == "Open in your browser: https://login.example/device?code=\\x1b[31mX"
    assert page.url.textFormat() == QtWidgets.QLabel().textFormat().PlainText
    page.answer("deny")  # in the background: the window is not blocked while sandbox-ai answers
    assert not page.allow_button.isEnabled() and not page.deny_button.isEnabled()
    assert wait(lambda: page.answering is None, 90)
    assert json.loads((run / "approvals" / "1700000000-1-2.answer").read_text())["decision"] == "deny"
    # Allow once through the command line.
    page.open.selectRow(0)
    page.scope.setCurrentIndex(page.scope.findData("once"))
    widgets.SHOWN.clear()
    page.answer("allow")
    title, text, _ = widgets.SHOWN[-1]  # Allow asks first: which sandbox, what, which scope
    assert title == "Allow this request?" and f"claude-abc ({env / 'project'})" in text
    assert "192.0.2.7:5432" in text and "Scope: once" in text
    assert wait(lambda: page.answering is None, 90)
    assert json.loads((run / "approvals" / "1700000000-1-1.answer").read_text()) == {"decision": "allow", "scope": "once"}
    assert page.open.rowCount() == 0 and nav.text() == "Approvals"
    (run / "approvals" / "1700000000-1-1.done").write_text(json.dumps({"decision": "allow", "scope": "once",
                                                                       "reason": "answered"}))
    page.poll()
    answers = {page.history.item(r, 3).text() for r in range(page.history.rowCount())}
    assert "allowed" in answers and "denied" in answers


def test_block_always_and_the_blocked_list(window, env, wait):
    run = fake_run(env)
    ask(run, "1700000000-1-9", kind="connect", destination="192.0.2.66:443", address="192.0.2.66", port=443)
    page = window.approvals
    page.poll()
    assert page.current() is not None and page.block_button.isEnabled()
    page.answer("deny", "always")  # what the Block always button does: no question first
    assert wait(lambda: page.answering is None, 90)
    answer = json.loads((run / "approvals" / "1700000000-1-9.answer").read_text())
    # In this run a block counts for the run; sandbox-ai remembers it for the project once the sandbox applied it.
    assert answer["decision"] == "deny" and answer["scope"] == "run"


def test_the_policy_caps_the_scope_box(qapp, env, monkeypatch):
    from sandbox_ai_app import pages
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "max_scope", lambda: "run")
    page = pages.ApprovalsPage(sai, interval=0)
    always = page.scope.findData("always")
    assert not page.scope.model().item(always).isEnabled() and page.scope.model().item(page.scope.findData("run")).isEnabled()
    page.timer.stop()


def test_approvals_rows_keep_their_question_when_sorted(window, env, wait):
    first, second = fake_run(env, "claude-aaa"), fake_run(env, "codex-bbb")
    ask(first, "1700000000-1-1", kind="dns", name="a.example")
    ask(second, "1700000000-2-1", kind="dns", name="b.example")
    page = window.approvals
    page.poll()
    page.open.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    row = next(r for r in range(2) if "b.example" in page.open.item(r, 2).text())
    assert row == 0, "sorted: codex-bbb first, unlike the order the questions were read in"
    page.open.selectRow(row)
    page.poll()  # the countdown is updated in its own row; the selection stays
    assert page.current()[1]["name"] == "b.example"
    page.answer("deny")  # allowing a name resolves it first; .example names never resolve
    assert wait(lambda: page.answering is None, 90)
    assert (second / "approvals" / "1700000000-2-1.answer").exists()
    assert not (first / "approvals" / "1700000000-1-1.answer").exists(), "the answer went to another sandbox's question"


def test_approvals_selection_is_cleared_when_its_question_goes(window, env, wait):
    run = fake_run(env)
    ask(run, "1700000000-1-1", kind="dns", name="first.example")
    ask(run, "1700000000-1-2", kind="dns", name="second.example")
    page = window.approvals
    page.poll()
    page.open.selectRow(0)
    assert page.current()[1]["name"] == "first.example" and page.allow_button.isEnabled()
    (run / "approvals" / "1700000000-1-1.answer").write_text(json.dumps({"decision": "deny", "scope": "once"}))
    page.poll()
    assert page.open.rowCount() == 1 and page.current() is None, "the selection does not jump to another question"
    assert not page.open.selectionModel().selectedRows()
    assert not page.allow_button.isEnabled() and not page.deny_button.isEnabled()
    # A wider scope is for one answer only.
    page.open.selectRow(0)
    page.scope.setCurrentIndex(page.scope.findData("always"))
    page.answer("deny")
    assert wait(lambda: page.answering is None, 90)
    assert json.loads((run / "approvals" / "1700000000-1-2.answer").read_text())["decision"] == "deny"
    assert page.scope.currentData() == "once"


def test_allow_asks_first_with_cancel_as_default_for_always(window, env, monkeypatch, wait):
    from sandbox_ai_app import pages
    run = fake_run(env)
    ask(run, "1700000000-1-5", kind="connect", destination="192.0.2.9:443")
    page = window.approvals
    page.poll()
    assert page.current() is not None, "the one open question is selected, ready to answer"
    asked = []
    monkeypatch.setattr(pages, "confirm", lambda parent, title, text, accept, destructive=False, **k:
                        asked.append((text, destructive)) or False)
    page.scope.setCurrentIndex(page.scope.findData("always"))
    page.answer("allow")
    assert asked and asked[-1][1] is True and "always (this project)" in asked[-1][0], "Cancel is the default"
    assert page.answering is None and not (run / "approvals" / "1700000000-1-5.answer").exists(), "declined"
    page.scope.setCurrentIndex(page.scope.findData("run"))
    page.answer("allow")
    assert asked[-1][1] is False
    count = len(asked)
    page.answer("deny")  # one click
    assert len(asked) == count
    assert wait(lambda: page.answering is None, 90)
    assert json.loads((run / "approvals" / "1700000000-1-5.answer").read_text())["decision"] == "deny"


def test_approvals_are_read_in_the_background(window, env, wait):
    run = fake_run(env)
    page = window.approvals
    ask(run, "1700000000-1-6", kind="dns", name="pypi.org")
    page.poll_soon()
    assert page.open.rowCount() == 0, "the scan runs in a thread; its result arrives through the event loop"
    assert wait(lambda: page.open.rowCount() == 1 or page.poll_soon())


def test_approvals_countdown_reaches_refused(window, env):
    run = fake_run(env)
    ask(run, "1700000000-1-3", seconds_ago=0, kind="dns", name="pypi.org")
    page = window.approvals
    page.poll(now=time.time() + 61)
    assert page.open.item(0, 4).text() == "refused" and page.open.item(0, 2).text() == "look up the name pypi.org"


# -- statistics --


def test_statistics_page(window, env, wait):
    logs = env / "state" / "sandbox-ai" / "logs" / "proj-1"
    logs.mkdir(parents=True)
    (logs / "connections-1.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"event": "http", "host": "api.anthropic.com", "model": {"model": "claude-x", "usage": {"input_tokens": 100,
                                                                                             "output_tokens": 40}},
         "credentials": ["x-api-key"]},
        {"event": "http", "host": "api.anthropic.com", "model": {"model": "claude-x", "usage": {"input_tokens": 50}}},
        {"event": "http", "host": "github.com", "push": {"ok": True}, "credentials": ["Authorization"]},
        {"event": "denied", "destination": "192.0.2.1:22"}]) + "\n")
    (logs / "processes.jsonl").write_text("\n".join(json.dumps({"command": c}) for c in
                                                     ["/usr/bin/git status", "git push", "python3 x.py"]) + "\n")
    mcp = env / "state" / "sandbox-ai" / "mcp" / "files"
    mcp.mkdir(parents=True)
    (mcp / "supervisor.log").write_text("".join(f'sandbox-ai-mcp-call {{"time": "t", "tool": "{t}"}}\n'
                                                for t in ("read", "read", "write")))
    page = window.statistics
    page.refresh_directories()  # read in a thread
    assert wait(lambda: not widgets.busy(page))
    assert "3 HTTP requests, 3 processes; pushes: 1 checked, 0 refused" in page.summary.text()
    models = page.tables["models"]
    assert (models.item(0, 0).text(), models.item(0, 1).text()) == ("claude-x", "2")
    tokens = {page.tables["tokens"].item(r, 0).text(): page.tables["tokens"].item(r, 1).text()
              for r in range(page.tables["tokens"].rowCount())}
    assert tokens == {"input_tokens": "150", "output_tokens": "40"}
    hosts = page.tables["hosts"]
    bar = lambda row: hosts.item(row, 2).data(widgets.BAR_ROLE)  # noqa: E731
    assert hosts.item(0, 0).text() == "api.anthropic.com" and bar(0) == 1.0 and bar(1) == 0.5
    assert isinstance(hosts.itemDelegateForColumn(2), widgets.BarDelegate)
    hosts.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    page.load()  # sorted by name, each bar stays with its host
    assert wait(lambda: not widgets.busy(page))
    assert hosts.item(0, 0).text() == "github.com" and bar(0) == 0.5 and bar(1) == 1.0
    hosts.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    page.load()
    assert wait(lambda: not widgets.busy(page))
    credentials = {(page.tables["credentials"].item(r, 0).text(), page.tables["credentials"].item(r, 1).text())
                   for r in range(page.tables["credentials"].rowCount())}
    assert credentials == {("x-api-key", "api.anthropic.com"), ("Authorization", "github.com")}
    assert page.tables["refused"].item(0, 0).text() == "denied 192.0.2.1:22"
    assert page.tables["programs"].item(0, 0).text() == "git" and page.tables["programs"].item(0, 1).text() == "2"
    tools = page.tables["tools"]
    assert [tools.item(0, c).text() for c in range(3)] == ["files", "read", "2"]


def test_statistics_page_without_logs(window):
    window.statistics.refresh_directories()
    assert "No logs yet" in window.statistics.summary.text()


# -- files --


def copy(page, wait, start):
    """Start a copy of the Files page and wait for its report."""
    assert start() is True
    assert wait(lambda: not page.task.running, 60)
    return page.last_report


def test_files_page_browses_copies_and_never_follows_links(window, env, tmp_path, wait):
    fake_run(env)
    project = env / "project"
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("print(1)\n")
    (project / "notes.txt").write_text("hello")
    (project / "outside").symlink_to("/etc")
    page = window.files
    page.refresh_roots()
    titles = [page.roots.itemText(i) for i in range(page.roots.count())]
    assert titles[0].startswith("Running sandbox claude-abc: workspace")
    assert any(t.startswith("Current project") for t in titles)
    page.roots.setCurrentIndex(0)
    names = {page.table.item(r, 0).text(): page.table.item(r, 1).text() for r in range(page.table.rowCount())}
    assert names == {"src": "folder", "notes.txt": "file", "outside": "symbolic link"}
    link_row = row_of(page.table, "outside")
    assert page.table.item(link_row, 4).text() == "→ /etc"
    page.open_row(link_row)
    assert "not followed" in page.message.text() and page.relative == ""
    page.open_row(row_of(page.table, "src"))
    assert page.relative == "src" and page.table.item(0, 0).text() == "main.py"
    page.up()
    assert page.relative == ""
    host = tmp_path / "download"
    host.mkdir()
    rows = [e for e in page.entries if e.name in ("src", "outside", "notes.txt")]
    report = copy(page, wait, lambda: page.download(rows, host))
    assert (host / "src" / "main.py").read_text() == "print(1)\n" and (host / "notes.txt").read_text() == "hello"
    assert not (host / "outside").exists() and any("symbolic link" in line for line in report)
    upload = tmp_path / "upload.txt"
    upload.write_text("from the host")
    page.go("src")
    copy(page, wait, lambda: page.upload([str(upload)]))
    assert (project / "src" / "upload.txt").read_text() == "from the host"
    assert "upload.txt" in [page.table.item(r, 0).text() for r in range(page.table.rowCount())]
    report = copy(page, wait, lambda: page.upload([str(upload)]))
    assert any("already exists" in line for line in report)
    page.replace.setChecked(True)
    upload.write_text("newer")
    copy(page, wait, lambda: page.upload([str(upload)]))
    assert (project / "src" / "upload.txt").read_text() == "newer"
    with pytest.raises(ValueError):
        page.go("../..")
    # Sorted by name (descending), rows still stand for their own entries.
    page.go("")
    page.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    assert [page.table.item(r, 0).text() for r in range(3)] == ["src", "outside", "notes.txt"]
    assert [e.name for e in page.entries][1] == "notes.txt"
    page.message.clear()
    page.open_row(1)
    assert "not followed" in page.message.text() and page.relative == ""
    page.table.selectRow(row_of(page.table, "notes.txt"))
    assert [e.name for e in page.chosen()] == ["notes.txt"]


def test_files_page_warns_about_copied_files_that_tools_run(window, env, tmp_path, wait):
    fake_run(env)
    repo = env / "project" / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n")
    (repo / ".git" / "hooks" / "post-checkout").chmod(0o755)
    (repo / "README").write_text("x")
    page = window.files
    page.refresh_roots()
    page.roots.setCurrentIndex(0)
    host = tmp_path / "out"
    host.mkdir()
    report = copy(page, wait, lambda: page.download([e for e in page.entries if e.name == "repo"], host))
    assert (host / "repo" / ".git" / "config").exists()
    assert not os.access(host / "repo" / ".git" / "hooks" / "post-checkout", os.X_OK), "never executable on the host"
    assert any("repo/.git/config" in line and "repo/.git/hooks/post-checkout" in line for line in report)
    assert any("never executable" in line for line in report)
    assert any(line.startswith("Copied without the executable bit") and "post-checkout" in line for line in report)
    assert [title for title, *_ in widgets.SHOWN] == ["Files that run or configure tools"]
    widgets.SHOWN.clear()
    envrc = tmp_path / ".envrc"
    envrc.write_text("export X=1\n")
    copy(page, wait, lambda: page.upload([str(envrc)]))  # into the sandbox: nothing on the host runs it
    assert widgets.SHOWN == []


def test_files_selection_does_not_follow_a_name_into_a_subfolder(window, env):
    fake_run(env)
    project = env / "project"
    (project / "src" / "src").mkdir(parents=True)
    (project / "src" / "src" / "x.py").write_text("1")
    (project / "src" / "other.txt").write_text("2")
    page = window.files
    page.refresh_roots()
    page.roots.setCurrentIndex(0)
    page.table.selectRow(row_of(page.table, "src"))
    page.open_row(row_of(page.table, "src"))
    assert page.relative == "src" and page.chosen() == [], "src/src must not be selected (and copied) by its name"
    page.table.selectRow(row_of(page.table, "other.txt"))
    page.list()  # a refresh of the same folder keeps the selection
    assert [e.name for e in page.chosen()] == ["other.txt"]
    page.up()
    assert page.chosen() == []


# -- images --


def test_images_page(window):
    page = window.images
    page.show_images([{"agent": "claude", "target": "claude", "present": True, "agent_version": "2.1.3",
                       "created": "2026-11-01T10:00:00", "size": 2_000_000_000, "image": "localhost/claude-sandbox:latest",
                       "extended": True, "status": "outdated"},
                      {"agent": "codex", "present": False, "image": "localhost/codex-sandbox:latest"},
                      {"agent": "claude", "target": "gpu", "variant": True, "present": True, "extended": True,
                       "status": "current", "image": "localhost/claude-sandbox-gpu:latest", "size": 1},
                      "junk"])
    assert page.images.rowCount() == 3
    assert [page.images.item(0, c).text() for c in (0, 1, 2, 3)] == ["claude", "claude", "yes", "changed: rebuild"]
    assert page.images.item(1, 2).text() == "not built" and page.images.item(1, 3).text() == "no extension"
    assert page.images.item(2, 3).text() == "current" and page.images.item(0, 6).text() == "2.00 GB"
    assert "2 of 3 images built" in page.status.text() and "1 to rebuild" in page.status.text()
    asked = []
    page.package_command.start = lambda argv: asked.append(argv) or True
    page.show_command.start = lambda argv: asked.append(argv) or True
    page.images.selectRow(0)
    assert ["images", "packages", "claude", "--json"] == asked[0][-4:]
    assert ["images", "extend", "claude", "--show", "--json"] == asked[1][-5:]
    assert not page.delete_button.isEnabled()
    page.packages_listed(0, json.dumps([{"kind": "deb", "name": "git", "version": "1:2.47"},
                                        {"kind": "npm", "name": "@anthropic-ai/claude-code", "version": "2.1.3"},
                                        {"kind": "tool", "name": "uv", "version": "uv 0.9"}]), "")
    assert page.packages.rowCount() == 3
    page.filter.setText("claude")
    assert page.packages.rowCount() == 1
    page.rebuild()
    assert window.started[-1][-2:] == ["build", "claude"]
    page.build(extension_only=True)
    assert window.started[-1][-3:] == ["build", "--extension-only", "claude"]
    page.images.selectRow(2)
    assert page.delete_button.isEnabled() and asked[-1][-5:] == ["images", "extend", "gpu", "--show", "--json"]
    page.images_listed(1, "", "podman: not found")
    assert "podman: not found" in page.status.text()



def test_a_refresh_during_the_discard_question_asks_no_second_one(window, monkeypatch):
    """Regression: selection_changed asked "Discard the changes?" (a modal confirm) in the selection-change
    slot; a refresh of the list that came back meanwhile refilled the table, which changed the selection
    again and stacked a second question on the first."""
    from sandbox_ai_app import images_page
    page = window.images
    claude = {"agent": "claude", "target": "claude", "present": True, "image": "localhost/claude-sandbox:latest"}
    gpu = {"agent": "claude", "target": "gpu", "variant": True, "present": True, "image": "localhost/claude-gpu:latest"}
    page.show_images([gpu, claude])
    page.package_command.start = lambda argv: True
    page.show_command.start = lambda argv: True
    page.target = "gpu"
    monkeypatch.setattr(page, "dirty", lambda: True)  # the extension of gpu has unsaved changes
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)  # real questions
    open_now, most = [0], [0]
    real = images_page.confirm

    def confirm(*args, **kwargs):
        open_now[0] += 1
        most[0] = max(most[0], open_now[0])
        try:
            return real(*args, **kwargs)
        finally:
            open_now[0] -= 1
    monkeypatch.setattr(images_page, "confirm", confirm)
    window.show()

    def refresh():  # the list comes back while the question is open: in another order
        try:
            page.show_images([claude, gpu])
        finally:
            modal = QtWidgets.QApplication.activeModalWidget()
            if modal is not None:
                modal.reject()
    net = close_modals_after(5)
    QtCore.QTimer.singleShot(200, refresh)
    page.images.selectRow(widgets.find_row(page.images, "claude"))
    widgets.AUTO_ANSWER = True
    net.stop()
    assert most[0] == 1, "one question at a time"
    assert widgets.selected_keys(page.images) == ["gpu"], "kept the changes: the selection went back"


def test_images_editor_validates_saves_and_makes_variants(window, env, wait):
    """The extension editor against the real sandbox-ai images command (no Podman needed for any of this)."""
    from sandbox_ai_app import images_page
    page = window.images
    page.load_extension("claude")
    assert wait(lambda: page.description is not None, 30), page.editor_title.text()
    assert page.description["agent"] == "claude" and not page.dirty() and not page.save_button.isEnabled()
    apt = page.lists["apt"]
    apt.entry.setText("Bad Name; rm -rf /")
    assert not apt.problem.isHidden() and not apt.add_button.isEnabled()
    apt.add()
    assert apt.values() == []
    apt.entry.setText("jq")
    apt.add()
    apt.entry.setText("ripgrep")
    apt.add()
    apt.search.setText("rip")
    assert [apt.list.item(i).isHidden() for i in range(2)] == [True, False]
    page.lists["npm"].entry.setText("@openai/codex")
    assert page.lists["npm"].problem.text(), "the agent itself is not an extension"
    page.fragment.setPlainText("# tools\nFROM alpine\nRUN chmod u+s /opt/x && echo \"$HOME\" \\\n    --flag\n")
    page.revalidate()
    rules = [f["rule"] for f in page.fragment.findings]
    assert rules == ["from", "setuid"] and not page.save_button.isEnabled()
    assert "2 error(s)" in page.verdict.text() and page.editor_tabs.tabText(4).endswith("(2)")
    underlined = [s for s in page.fragment.extraSelections()
                  if s.format.underlineStyle() == QtGui.QTextCharFormat.UnderlineStyle.WaveUnderline]
    assert [s.cursor.selectedText() for s in underlined] == ["FROM", "u+s"]
    assert page.findings.count() == 2 and "line 2" in page.findings.item(0).text()
    page.jump(page.findings.item(1))
    assert page.editor_tabs.currentIndex() == 4 and page.fragment.textCursor().blockNumber() == 2
    # The highlighter: instructions, strings, variables, comments in the theme's colours.
    block = page.fragment.document().findBlockByNumber(2)
    formats = {f.start: f.format.foreground().color().name() for f in block.layout().formats()}
    assert formats[0] == theme.colors()["accent"]
    assert QtGui.QColor(theme.color("warn")).name() in formats.values()
    first = page.fragment.document().firstBlock().layout().formats()
    assert first and first[0].format.fontItalic(), "a comment"
    continued = page.fragment.document().findBlockByNumber(3).layout().formats()
    assert all(f.format.foreground().color().name() != theme.colors()["accent"] for f in continued)
    page.fragment.setPlainText("RUN echo \"$HOME\"\n")
    page.revalidate()
    assert page.fragment.findings == [] and page.save_button.isEnabled() and "not saved" in page.verdict.text()
    preview = page.preview.toPlainText()
    assert "FROM localhost/claude-sandbox:base" in preview and "    jq ripgrep \\" in preview
    argv, stdin = page.save_argv()
    assert argv[-10:] == ["images", "extend", "claude", "--add-apt", "jq", "--add-apt", "ripgrep", "--fragment", "-",
                          "--json"] and stdin == b'RUN echo "$HOME"\n'
    page.save()
    assert wait(lambda: not page.task.running, 30)
    stored = json.loads((env / "config" / "sandbox-ai" / "images" / "claude" / "image.json").read_text())
    assert stored["apt"] == ["jq", "ripgrep"] and not page.dirty()
    assert (env / "config" / "sandbox-ai" / "images" / "claude" / "Containerfile.fragment").read_text() == \
        'RUN echo "$HOME"\n'
    # Removing goes through the command too.
    apt.list.item(0).setSelected(True)
    apt.remove()
    assert page.save_argv()[0][-3:] == ["--remove-apt", "jq", "--json"]
    page.revert()
    assert apt.values() == ["jq", "ripgrep"] and not page.dirty()
    # A file: added with its target and mode, saved by --add-file.
    tool = env / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    assert not page.add_file_entry(str(tool), "relative", "0755")
    assert page.add_file_entry(str(tool), "/usr/local/bin/tool", "755")
    assert page.files.rowCount() == 1 and page.files.item(0, 3).text() == "new"
    page.revalidate()
    assert "COPY --chmod=0755 files/tool.sh /usr/local/bin/tool" in page.preview.toPlainText()
    assert page.save_argv()[0][-3:] == ["--add-file", f"{tool}:/usr/local/bin/tool:0755", "--json"]
    page.save()
    assert wait(lambda: not page.task.running, 30)
    assert page.description["available"] == ["tool.sh"] and page.files.item(0, 3).text() == ""
    # A refused save keeps the editor as it is and says why.
    page.description = dict(page.description, fragment="")
    page.fragment.setPlainText("VOLUME /x\n")
    argv, stdin = page.save_argv()
    page.task.run(argv, page.saved, "test", input=stdin)
    assert wait(lambda: not page.task.running, 30)
    assert widgets.SHOWN[-1][1] == "sandbox-ai did not save the extension." and "VOLUME" in widgets.SHOWN[-1][2]
    assert page.fragment.toPlainText() == "VOLUME /x\n"
    # Variants: new (from the agent's extension) and delete.
    page.create_variant("web", "claude")
    assert wait(lambda: not page.task.running, 30)
    assert page.description["target"] == "web" and page.description["apt"] == ["jq", "ripgrep"]
    assert (env / "config" / "sandbox-ai" / "images" / "web" / "image.json").exists()
    page.create_variant("claude", "codex")
    assert wait(lambda: not page.task.running, 30)
    assert "did not create the variant claude" in widgets.SHOWN[-1][1]
    dialog = images_page.NewVariantDialog(page, ("claude", "codex"), "codex", page.module().valid_name)
    assert dialog.agent.currentData() == "codex" and not dialog.ok.isEnabled()
    dialog.name.setText("Bad")
    assert not dialog.ok.isEnabled() and dialog.problem.text()
    dialog.name.setText("gpu")
    assert dialog.ok.isEnabled()
    page.show_images([{"agent": "claude", "target": "web", "variant": True, "present": False,
                       "image": "localhost/claude-sandbox-web:latest", "extended": True, "status": "not-built"}])
    page.show_command.start = lambda argv: True
    page.images.selectRow(0)
    page.delete_variant()
    assert wait(lambda: not page.task.running, 30)
    assert not (env / "config" / "sandbox-ai" / "images" / "web").exists() and page.description is None
    # The theme changes the colours at once.
    theme.apply("dark")
    try:
        page.fragment.setPlainText("RUN x\n")
        QtWidgets.QApplication.processEvents()
        block = page.fragment.document().firstBlock()
        assert block.layout().formats()[0].format.foreground().color().name() == theme.colors()["accent"]
    finally:
        theme.apply("system")


def test_async_command_does_not_block_and_times_out(qapp, wait):
    from sandbox_ai_app.widgets import AsyncCommand
    results = []
    command = AsyncCommand(timeout=30)
    command.finished.connect(lambda *r: results.append(r))
    began = time.monotonic()
    assert command.start([sys.executable, "-c", "import time; time.sleep(0.5); print('done')"])
    assert time.monotonic() - began < 0.4 and not command.start(["true"])
    assert wait(lambda: results, 10) and results[0][0] == 0 and results[0][1].strip() == "done"
    slow = AsyncCommand(timeout=0.3)
    slow.finished.connect(lambda *r: results.append(r))
    slow.start([sys.executable, "-c", "import time; time.sleep(30)"])
    assert wait(lambda: len(results) == 2, 10) and results[1][0] == -1
    missing = AsyncCommand()
    missing.finished.connect(lambda *r: results.append(r))
    missing.start(["/nonexistent/program"])
    assert wait(lambda: len(results) == 3, 10) and results[2][0] == -1



def test_stopping_a_command_does_not_wait_on_the_gui_thread(qapp, tmp_path, wait):
    """Regression: AsyncCommand.stop() waited up to 1.5 s + 3 s on the GUI thread (on every selection change of
    the Images page, every timeout, at shutdown). It sends SIGTERM, reports the command stopped at once, and
    a timer sends SIGKILL to the group later if it still runs."""
    from sandbox_ai_app.widgets import AsyncCommand
    pid_file = tmp_path / "pid"
    stubborn = ("import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)")
    results = []
    command = AsyncCommand(timeout=60)
    command.finished.connect(lambda *r: results.append(r))
    command.start([sys.executable, "-c", stubborn, str(pid_file)])
    assert wait(lambda: pid_file.exists() and pid_file.read_text(), 20)
    pid = int(pid_file.read_text())
    began = time.monotonic()
    command.stop()
    assert time.monotonic() - began < 0.1, "stop() returns at once"
    assert results and results[0][0] == -1 and not command.running, "reported stopped at once"
    assert command.start([sys.executable, "-c", "print('next')"]), "a new command may start right away"
    assert wait(lambda: len(results) == 2, 20) and results[1][1].strip() == "next"

    def gone():
        try:
            return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] == "Z"
        except OSError:
            return True
    assert not gone(), "it ignores SIGTERM: still there right after stop()"
    assert wait(gone, 10), "killed later by the timer"
    assert len(results) == 2, "the stopped command reports nothing more"


# -- credentials --


def test_credentials_page_logins_and_secrets(window, env, wait):
    page = window.credentials
    page.refresh()  # in a thread: asking the keyring may take seconds
    assert wait(lambda: not widgets.busy(page))
    assert [page.logins.item(r, 0).text() for r in range(page.logins.rowCount())] == list(window.sai.agents)
    assert "private files" in page.backend_label.text()
    page.name.setText("DEMO_TOKEN")
    page.value.setText("very-secret-value")
    assert page.value.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
    page.set_secret()
    assert page.value.text() == "", "cleared at once"
    assert not page.secret_buttons[0].isEnabled() or not widgets.busy(page)
    assert wait(lambda: not widgets.busy(page) and page.secrets.rowCount() == 1, 60)
    assert [page.secrets.item(r, 0).text() for r in range(page.secrets.rowCount())] == ["DEMO_TOKEN"]
    stored = env / "data" / "sandbox-ai" / "secrets" / "DEMO_TOKEN"
    assert stored.read_text() == "very-secret-value"
    assert not any("very-secret-value" in text for text in texts(window)), "a secret's value is never shown"
    page.name.setText("bad name")
    page.value.setText("x")
    page.set_secret()
    assert page.value.text() == "x"  # refused before anything ran
    page.name.setText("DEMO_TOKEN")
    page.delete_secret()
    assert wait(lambda: not widgets.busy(page) and page.secrets.rowCount() == 0, 60)
    assert not stored.exists()
    page.logins.selectRow(row_of(page.logins, "codex"))
    page.login(False)
    assert window.started[-1][-2:] == ["login", "codex"]
    page.login(True)
    assert window.started[-1][-3:] == ["login", "codex", "--logout"]


# -- terminals --


def test_terminal_tabs_or_the_terminal_window(window, env, monkeypatch, tmp_path, wait):
    from sandbox_ai_app import terminal
    # Without QtWebEngine or xterm.js: a terminal window.
    window.launch.start_in_app()
    assert window.started[-1][0] == "term" and window.terminals.tabs.count() == 0
    if terminal.MISSING:
        pytest.skip("QtWebEngine is not installed")
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, web)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, web / "vendor")
    monkeypatch.setattr(terminal, "available", lambda w=None: (True, ""))
    window.terminals.web = web
    window.launch.agent.setCurrentText("codex")
    count = len(window.started)
    window.launch.start_in_app()
    assert len(window.started) == count and window.terminals.tabs.count() == 1
    view = window.terminals.views()[0]
    assert view.argv[-1] == "codex" and window.pages.currentWidget() is window.terminals
    assert window.terminals.tabs.tabText(0) == "codex"
    # A session attached in the app.
    session = env / "state" / "sandbox-ai" / "sessions" / "work"
    (session / "home").mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({"agent": "claude", "options": [], "backend": "podman",
                                                      "workspace": str(env), "cwd": str(env), "created": "x"}))
    window.sandboxes.refresh()
    window.sandboxes.table.selectRow(widgets.find_row(window.sandboxes.table, "session:work"))
    window.sandboxes.session("attach-app")
    assert window.terminals.tabs.count() == 2 and window.terminals.views()[1].argv[-3:] == ["session", "attach", "work"]
    # Commands such as builds follow the setting.
    window.sai.save_setting("terminal", "window")
    window.system.tool(("build", "all"))
    assert window.started[-1][-2:] == ["build", "all"] and window.terminals.tabs.count() == 2
    window.terminals.close_all()
    assert window.terminals.tabs.count() == 0


# -- nothing slow on the GUI thread --


def test_slow_commands_do_not_block_the_window(window, env, monkeypatch, wait):
    import threading
    release = threading.Event()

    def slow_call(*args, timeout=120, input=None):
        release.wait(30)
        return 0, "done " + " ".join(args), ""
    monkeypatch.setattr(window.sai, "call", slow_call)
    monkeypatch.setattr(window.sai, "doctor", lambda backend="podman": release.wait(30) and [("ok", "Podman", "5", "")])
    monkeypatch.setattr(window.sai, "dry_run", lambda agent, argv: release.wait(30) and ({"plan": 1}, ""))
    monkeypatch.setattr(window.sai, "mcp_catalog", lambda: release.wait(30) and ([], []))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: str(env))
    run = fake_run(env)
    began = time.monotonic()
    window.system.privacy()
    window.system.check()
    window.launch.dry_run()
    window.mcp.load_catalog()
    window.sandboxes.refresh()
    window.sandboxes.table.selectRow(0)
    window.sandboxes.show_allowed()
    assert time.monotonic() - began < 3, "the commands run in the background"
    buttons = [window.system.privacy_button, window.system.check_button, window.launch.dry_button,
               window.mcp.load_button, *window.sandboxes.run_buttons]
    assert not any(button.isEnabled() for button in buttons), "the buttons that started them wait"
    # Questions are still read and shown while the commands run.
    ask(run, "1700000000-1-7", kind="dns", name="pypi.org")
    window.approvals.poll_soon()
    assert wait(lambda: window.approvals.open.rowCount() == 1)
    release.set()
    pages = (window.system, window.launch, window.mcp, window.sandboxes)
    assert wait(lambda: not any(widgets.busy(page) for page in pages), 30)
    assert all(button.isEnabled() for button in buttons)
    assert window.system.checks.rowCount() == 1
    titles = [title for title, *_ in widgets.SHOWN]
    assert "Plan (dry run)" in titles and any(t.startswith("Privacy check") for t in titles) and "claude-abc" in titles


def test_background_timeout_frees_the_button_and_drops_the_late_result(qapp, wait):
    import threading
    release, results = threading.Event(), []
    button = QtWidgets.QPushButton("x")
    owner = QtWidgets.QWidget()
    widgets.background(lambda: release.wait(30) and "late", owner=owner, source=button, timeout=0.2,
                       done=lambda result, error: results.append((result, error)))
    assert not button.isEnabled() and widgets.busy(owner)
    assert wait(lambda: results, 10)
    assert results[0][0] is None and isinstance(results[0][1], TimeoutError)
    assert button.isEnabled() and not widgets.busy(owner)
    release.set()
    assert not wait(lambda: len(results) > 1, 1), "the late result is dropped, done runs once"


def test_an_abandoned_call_emits_nothing(qapp, wait):
    import threading
    release, results = threading.Event(), []
    call = widgets.ThreadCall(lambda: release.wait(30) and "result")
    call.finished.connect(lambda *r: results.append(r))
    call.start()
    widgets.ThreadCall.abandon_all()  # what the window does when it closes
    release.set()
    call.thread.join(10)
    assert not wait(lambda: results, 0.5) and call not in widgets.ThreadCall._alive


def test_closing_while_work_runs_asks_and_stops_copies(window, monkeypatch, wait):
    import threading
    from PySide6.QtGui import QCloseEvent
    release = threading.Event()
    widgets.background(lambda: release.wait(30), owner=window.system, done=lambda *r: None)
    stopped = []

    def copy(cancelled):  # a long copy: runs until it is cancelled
        while not cancelled():
            time.sleep(0.01)
        stopped.append(True)
    window.files.task.thread(copy, lambda *r: None, "copy", cancellable=True)
    assert window.running_work() == ["Files", "System"]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep working"
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted() and window.files.task.running
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Quit" and "Files, System" in text
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # "Stop and quit"
    monkeypatch.setattr("sandbox_ai_app.ui.QUIT_WAIT", 1)
    release.set()
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() and stopped == [True], "the copy was cancelled and waited for"
    assert widgets.ThreadCall.running() == []


def test_pages_scroll_instead_of_squeezing_when_the_window_is_low(window, qapp):
    """Every page but the board's canvas and the terminals sits in a scroll area: in a low window its sections keep
    the height they need and the page scrolls up and down (never sideways)."""
    from sandbox_ai_app.ui import PageScroll
    assert isinstance(window.pages.frame_of(window.system), PageScroll)
    assert window.pages.frame_of(window.board) is window.board and window.pages.frame_of(window.terminals) is window.terminals
    window.show()
    for key in ("system", "credentials"):
        window.show_page(key, raise_window=False)
        assert window.pages.currentWidget() is getattr(window, key) and window.current_page() == key
    window.show_page("system", raise_window=False)
    window.resize(window.minimumSizeHint())
    qapp.processEvents()
    frame = window.pages.frame_of(window.system)
    page = window.system
    assert frame.viewport().height() < page.minimumSizeHint().height(), "the window is lower than the page"
    assert page.height() >= page.minimumSizeHint().height(), "the page keeps its height"
    assert frame.verticalScrollBar().maximum() > 0 and frame.horizontalScrollBar().maximum() == 0


def test_quitting_asks_only_about_terminals_that_really_end(window, monkeypatch):
    """A session's tab only detaches when the app quits (the sandbox and its agent run on): no question for it.
    Commands that end with the app are named in the question."""
    from PySide6.QtGui import QCloseEvent
    monkeypatch.setattr(window.terminals, "ending", lambda: [])
    monkeypatch.setattr(window.terminals, "detaching", lambda: ["a session"])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() and not [t for t, *_ in widgets.SHOWN if t == "Quit"], "sessions: nobody was asked"


def test_quitting_names_the_commands_that_end_with_the_app(window, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    monkeypatch.setattr(window.terminals, "ending", lambda: ["doctor", "build"])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep the window"
    event = QCloseEvent()
    window.closeEvent(event)
    title, text, _ = widgets.SHOWN[-1]
    assert not event.isAccepted() and title == "Quit" and "doctor, build" in text and "Sessions keep running" in text


def test_an_answer_that_hangs_frees_the_buttons_after_a_while(window, env, monkeypatch, wait):
    """Regression: the answer ran in a ThreadCall without a timeout; a sandbox-ai call that never returned
    kept self.answering set, and Allow and Deny greyed out, for good."""
    import threading
    from sandbox_ai_app import pages
    run = fake_run(env)
    ask(run, "1700000000-1-9", kind="connect", destination="192.0.2.7:5432", address="192.0.2.7", port=5432)
    release = threading.Event()
    monkeypatch.setattr(window.sai, "answer", lambda *a, **k: release.wait(30) and (0, "", ""))
    monkeypatch.setattr(pages, "ANSWER_WAIT", 0.5)
    page = window.approvals
    page.poll()
    page.open.selectRow(0)
    try:
        page.answer("deny")
        assert page.answering is not None and not page.deny_button.isEnabled()
        assert wait(lambda: page.answering is None, 10), "given up after its timeout"
        assert page.deny_button.isEnabled() and page.message.text() == "Not answered"
    finally:
        release.set()



def close_modals_after(seconds):
    """A safety net for tests that open a real modal dialog: whatever is modal after SECONDS is closed, and
    again every second after that (a test that fails must not leave exec() running for ever). The timer."""
    def close():
        timer.setInterval(1000)
        for widget in QtWidgets.QApplication.topLevelWidgets():
            if isinstance(widget, QtWidgets.QDialog) and widget.isVisible() and widget.isModal():
                widget.reject()
    timer = QtCore.QTimer()
    timer.timeout.connect(close)
    timer.start(int(seconds * 1000))
    return timer


def test_a_question_can_be_answered_while_a_modal_dialog_is_open(window, env, monkeypatch, wait):
    """Regression: a modal dialog (a message, confirm(), the wizard, Settings) blocked the main window and with
    it Allow and Deny; a question asked meanwhile was refused unanswered after 60 s. It is offered in a window
    above the dialog, and answered from there through the same path."""
    from PySide6.QtTest import QTest
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)  # real dialogs
    run = fake_run(env)
    page = window.approvals
    window.show()
    seen = {}

    def while_open():
        try:
            seen["modal"] = QtWidgets.QApplication.activeModalWidget()
            ask(run, "1700000000-1-7", kind="connect", destination="192.0.2.7:5432", address="192.0.2.7", port=5432)
            page.poll()
            questions = page.questions_window
            seen["shown"] = questions is not None and questions.isVisible()
            if not seen["shown"]:
                return
            QTest.qWaitForWindowExposed(questions)
            deny = questions.deny_button
            QTest.mouseClick(questions.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             deny.mapTo(questions, deny.rect().center()))
            seen["answering"] = page.answering is not None
            seen["answered"] = wait(lambda: page.answering is None, 60)
            seen["closed"] = wait(lambda: not questions.isVisible(), 5)
        finally:
            if seen.get("modal") is not None:
                seen["modal"].reject()
    net = close_modals_after(90)
    QtCore.QTimer.singleShot(200, while_open)
    widgets.confirm(window, "Something else", "A question of another page", "OK")
    widgets.AUTO_ANSWER = True  # the window's teardown asks no real questions
    net.stop()
    assert seen.get("modal") is not None, "confirm() was open"
    assert seen.get("shown"), "the question was offered above the dialog"
    assert seen.get("answering") and seen.get("answered"), "Deny could be clicked while the dialog was open"
    assert json.loads((run / "approvals" / "1700000000-1-7.answer").read_text())["decision"] == "deny"
    assert seen.get("closed"), "the window goes when nothing is open any more"


def test_results_of_background_jobs_do_not_block(window, monkeypatch, wait):
    """Regression: the outcome of a finished job (ui.report, the Kits page, the privacy check, "Not answered")
    was a modal message box: while it was open, no question could be answered."""
    from sandbox_ai_app import ui
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    window.show()
    net = close_modals_after(3)
    began = time.monotonic()
    ui.report(window.sandboxes, "session stop", (0, "stopped", ""), None)
    window.kits.done("sandbox-ai kits new", (0, "created", ""), None)
    assert time.monotonic() - began < 1.5, "nothing waited for a click"
    widgets.show_text(window.system, "Privacy check", "nothing found", modal=False)
    assert QtWidgets.QApplication.activeModalWidget() is None
    shown = [w for w in QtWidgets.QApplication.topLevelWidgets() if w.isVisible()
             and isinstance(w, (QtWidgets.QMessageBox, QtWidgets.QDialog))]
    assert len(shown) >= 3
    for widget in shown:
        widget.close()
    widgets.AUTO_ANSWER = True
    net.stop()


def test_connections_retry_after_a_read_error(window, env, monkeypatch, wait):
    logs = env / "state" / "sandbox-ai" / "logs" / "app-err"
    logs.mkdir(parents=True)
    (logs / "connections-1.jsonl").write_text(json.dumps({"time": "t1", "event": "http", "url": "https://a/"}) + "\n")
    real = backend.read_connections
    failing = [True]

    def flaky(*args, **kwargs):
        if failing:
            failing.clear()
            raise OSError("temporarily unreadable")
        return real(*args, **kwargs)
    monkeypatch.setattr(backend, "read_connections", flaky)
    page = window.connections
    page.refresh_directories()
    assert wait(lambda: "temporarily unreadable" in page.detail.toPlainText() and not widgets.busy(page))
    assert page.table.rowCount() == 0 and page.state is None, "a failed read is not recorded as read"
    page.load()  # what the next tick does
    assert wait(lambda: page.table.rowCount() == 1)


def test_connections_look_at_the_log_off_the_gui_thread_one_look_at_a_time(window, env, monkeypatch, wait):
    import threading
    logs = env / "state" / "sandbox-ai" / "logs" / "app-stuck"
    logs.mkdir(parents=True)
    (logs / "connections-1.jsonl").write_text(json.dumps({"time": "t1", "event": "http", "url": "https://a/"}) + "\n")
    release, looks, gui = threading.Event(), [], threading.get_ident()
    real = backend.log_state

    def stuck(path):  # a stat that hangs (a network file system)
        looks.append(threading.get_ident())
        release.wait(30)
        return real(path)
    monkeypatch.setattr(backend, "log_state", stuck)
    page = window.connections
    page.refresh_directories()
    page.load()
    page.load()
    assert wait(lambda: looks, 5) and not wait(lambda: len(looks) > 1, 0.5), "no second look while one is stuck"
    assert looks[0] != gui, "log_state runs in a thread"
    release.set()
    assert wait(lambda: page.table.rowCount() == 1)


SPAWNER = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


def test_quitting_stops_running_commands_with_their_children(window, env, monkeypatch, wait):
    # The window's own reads (the board's dry run, a slow subprocess on a CI runner, started again by its timer
    # whenever events are processed) end first and start no more: what is measured below is the stopped
    # command's thread alone.
    window.board.check_timer.stop()
    window.board.check_timer.blockSignals(True)  # its timeout is wired to check() itself: no new dry run
    widgets.ThreadCall.wait_all(120)
    pid_file = env / "child"
    monkeypatch.setattr(window.sai, "command", lambda *args: [sys.executable, "-c", SPAWNER, str(pid_file)])
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: str(env))
    window.system.privacy()  # sandbox-ai privacy-check, up to 600 s
    assert wait(lambda: pid_file.exists() and pid_file.read_text(), 20)
    child = int(pid_file.read_text())
    assert window.running_work() == ["System"]
    left = window.stop_work(timeout=10)
    assert left == [], ("the command was stopped, so its thread ended; still running: "
                        + ", ".join(getattr(c.function, "__qualname__", repr(c.function)) for c in left))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
            if Path(f"/proc/{child}/stat").read_text().rpartition(")")[2].split()[0] == "Z":
                break
        except (ProcessLookupError, FileNotFoundError):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the command's child process outlived the quit")


def test_sigterm_quits_without_questions(window, monkeypatch):
    import threading
    from PySide6.QtGui import QCloseEvent
    release = threading.Event()
    widgets.background(lambda: release.wait(30), owner=window.system, done=lambda *r: None)
    quit_called = []
    monkeypatch.setattr(QtWidgets.QApplication, "quit", staticmethod(lambda: quit_called.append(True)))
    monkeypatch.setattr("sandbox_ai_app.ui.QUIT_WAIT", 0.5)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # a question would be answered "Keep working"
    window.terminate()
    assert quit_called and window.quitting
    event = QCloseEvent()
    window.closeEvent(event)  # what quit() sends to the window
    assert event.isAccepted() and not [t for t, *_ in widgets.SHOWN if t == "Quit"], "nobody was asked"
    release.set()


# -- the tray, allowed always, what a question means --


class FakeTray:
    def __init__(self):
        self.messages = []

    def showMessage(self, *args):  # noqa: N802
        self.messages.append(args)

    def hide(self):
        pass


def test_closing_hides_to_the_tray_and_answering_goes_on(window, env, monkeypatch, wait):
    from PySide6.QtGui import QCloseEvent
    window.tray = FakeTray()
    window.show()
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted() and not window.isVisible(), "hidden, not closed"
    notices = [t for t in window.notifier.toasts if "tray" in t.text.text()]
    assert len(notices) == 1 and notices[0].level == "info", "a one-line notice in the app's theme, once"
    assert window.tray.messages == [], "not the desktop's notification as well"
    window.show()
    window.closeEvent(QCloseEvent())
    assert len([t for t in window.notifier.toasts if "tray" in t.text.text()]) == 1
    window.notifier.close_all()
    run = fake_run(env)
    ask(run, "1700000000-1-8", kind="dns", name="pypi.org")
    window.approvals.poll_soon()
    assert wait(lambda: window.approvals.open.rowCount() == 1), "the questions are still read while hidden"
    closed = []
    window.closed.connect(lambda: closed.append(True))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert not window.quit_app(), "Quit in the tray menu asks first: a sandbox runs"
    assert "refused after" in widgets.SHOWN[-1][1] and not closed
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    asked = len(widgets.SHOWN)
    assert window.quit_app() and closed == [True] and window.finished and not window.approvals.timer.isActive()
    assert len(widgets.SHOWN) == asked + 1, "asked once"
    event = QCloseEvent()
    window.closeEvent(event)  # app.quit() closing the windows once more
    assert event.isAccepted() and closed == [True] and len(widgets.SHOWN) == asked + 1, "no second question"
    window.tray = None


def test_closing_closes_when_the_tray_setting_is_off(window):
    from PySide6.QtGui import QCloseEvent
    window.tray = FakeTray()
    window.sai.save_setting("tray", False)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() and window.finished
    window.tray = None


def test_logout_stops_the_work_and_asks_nothing(window, env, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    fake_run(env)  # would make a normal close ask
    stopped = []
    monkeypatch.setattr(window, "stop_work", lambda timeout=None: stopped.append(True) or [])
    window.end_of_session(None)
    assert window.quitting and stopped == [True] and window.session_timer.isActive()
    window.session_goes_on()  # the logout was cancelled: back to normal
    assert not window.quitting
    window.end_of_session(None)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() and not [t for t, *_ in widgets.SHOWN if t == "Quit"]
    window.session_goes_on()
    assert window.quitting, "a session that ended stays ended"


def test_a_start_at_login_waits_for_the_tray(window, monkeypatch, wait):
    made = []
    monkeypatch.setattr(window, "make_tray", lambda: made.append(True))
    answers = iter([False, False, True])
    window.start_hidden(seconds=10, interval=20, available=lambda: next(answers))
    assert wait(lambda: made, 5) and not window.isVisible(), "the tray appeared: the window stays hidden"
    window.start_hidden(seconds=0.2, interval=20, available=lambda: False)
    assert wait(lambda: window.isVisible(), 5), "no tray in time: the window is shown"


def test_without_a_tray_closing_warns_that_questions_will_be_refused(window, env, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    fake_run(env)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    event = QCloseEvent()
    window.closeEvent(event)
    title, text, _ = widgets.SHOWN[-1]
    assert not event.isAccepted() and title == "Quit"
    assert f"refused after {window.sai.approval_seconds} seconds" in text


def test_autostart_is_a_setting_written_only_when_ticked(window, env):
    path = env / "config" / "autostart" / "sandbox-ai-app.desktop"
    assert not path.exists() and not window.system.autostart_box.isChecked()
    window.system.autostart_box.setChecked(True)
    assert path.exists() and "--hidden" in path.read_text()
    window.system.autostart_box.setChecked(False)
    assert not path.exists()


def test_allowed_always_lists_and_forgets(window, env, wait):
    approvals = window.sai.module("approvals")
    if not hasattr(approvals, "remembered"):
        pytest.skip("this sandbox-ai has no approvals.remembered")
    project = env / "project"
    project.mkdir(exist_ok=True)
    approvals.remember(project, host="pypi.org")
    approvals.remember(project, tcp="192.0.2.5:5432")
    page = window.approvals
    page.load_always()
    assert wait(lambda: page.always.rowCount() == 2)
    page.always.selectRow(row_of(page.always, "pypi.org", 1))
    page.forget()
    assert "Forget pypi.org for" in widgets.SHOWN[-1][1]
    assert wait(lambda: not widgets.busy(page) and page.always.rowCount() == 1, 60)
    assert page.always.item(0, 1).text() == "192.0.2.5:5432" and page.always_message.text() == "Forgotten."


def test_a_question_shows_what_it_means(window, env, wait):
    run = fake_run(env)
    logs = window.sai.launcher.default_log_dir(env / "project")
    logs.mkdir(parents=True)
    (logs / "connections-20261103-100000-claude-abc.jsonl").write_text(json.dumps(
        {"time": "2026-11-03T09:59:00Z", "event": "denied", "destination": "192.0.2.7:5432"}) + "\n")
    ask(run, "1700000000-1-1", kind="dns", name="localhost")
    ask(run, "1700000000-1-2", kind="connect", destination="192.0.2.7:5432", address="192.0.2.7", port=5432,
        protocol="tls", host="db.example")
    page = window.approvals
    page.poll()
    page.open.selectRow(0)
    assert wait(lambda: "Nothing is looked up before you answer" in page.context.text(), 20), page.context.text()
    assert "127.0.0.1" not in page.context.text(), "the agent's name is never resolved before the answer"
    page.open.selectRow(1)
    assert wait(lambda: "not verified" in page.context.text(), 20), page.context.text()
    text = page.context.text()
    assert "Address 192.0.2.7, port 5432, protocol tls." in text and "db.example" in text
    # The run's log entries are quoted apart: monospace, read-only, labelled as the agent's text.
    assert "2026-11-03T09:59:00Z" not in text
    assert not page.context_log.isHidden() and page.context_log.isReadOnly()
    assert page.context_log.font().styleHint() == widgets.MONO.styleHint()
    assert page.context_log.toPlainText() == "> 2026-11-03T09:59:00Z  denied  192.0.2.7:5432"
    assert "text the agent chose" in page.context_log_label.text()
    page.open.selectRow(0)
    assert page.context_log.isHidden(), "no log entries for this one"


def test_a_risk_found_at_start_stays_on_screen(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "x"))
    sai = backend.SandboxAI()
    risk = backend.InsideSandbox(str(sai.directory), str(Path(sai.directory).parent))
    w = MainWindow(sai, check_updates=False, approvals_interval=0, risk=risk)
    try:
        assert not w.banner.isHidden() and str(sai.directory) in w.banner.text() and "agent may have changed" in w.banner.text()
        assert w.banner.textFormat() == QtWidgets.QLabel().textFormat().PlainText
    finally:
        w.shutdown()
        w.deleteLater()


# -- notifications, project configurations --


def test_notification_bodies_are_escaped(window):
    from PySide6.QtWidgets import QSystemTrayIcon
    shown = []
    window.tray = type("Tray", (), {"showMessage": lambda self, *a: shown.append(a), "hide": lambda self: None})()
    window.sai.save_setting("notify_style", "system")  # the desktop's notifications
    try:
        window.notify("sandbox-ai asks: <b>x</b>\x1b", "open <a href='https://evil.example'>here</a> & more")
    finally:
        window.sai.save_setting("notify_style", "themed")
    title, text, icon = shown[0][:3]
    assert title == "sandbox-ai asks: <b>x</b>\\x1b", "the title is plain text: shortened and printable, not escaped"
    assert "<a" not in text and "&lt;a href=" in text and "&amp; more" in text
    assert icon == QSystemTrayIcon.MessageIcon.Warning, "a question is a warning"
    # In the app's theme (the default): a popup with the text as plain text, nothing sent to the tray.
    window.notify("t <b>bold</b>", "<img src=x>", level="error")
    toast = window.notifier.toasts[-1]
    assert len(shown) == 1 and toast.level == "error" and toast.text.text() == "<img src=x>"
    assert toast.text.textFormat() == toast.text.textFormat().PlainText
    window.notifier.close_all()
    assert window.notifier.toasts == []


def test_open_project_folder_reviews_before_it_trusts(window, env, monkeypatch):
    project = env / "proj"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "opencode",
                                                          "options": ["--allow-pypi"]}))
    launch = window.launch
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # the human says no
    assert not launch.load_project(str(project))
    assert window.sai.launcher.trusted_configs() == {}
    title, text, details = widgets.SHOWN[-1]
    assert title == "Review the project configuration" and "--allow-pypi" in text and '"opencode"' in details
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert launch.load_project(str(project))
    assert window.sai.review_project(project).trusted
    assert launch.agent.currentText() == "opencode" and launch.workspace() == str(project)
    assert "--allow-pypi" not in launch.argv(), "the launcher applies the trusted file; the form does not copy it"
    # A file the launcher would refuse is shown, never trusted.
    evil = env / "evil"
    evil.mkdir()
    (evil / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude",
                                                       "options": ["--config", "/tmp/elsewhere.json"]}))
    assert not launch.load_project(str(evil))
    assert widgets.SHOWN[-1][0] == "This project configuration cannot be used"
    assert not window.sai.review_project(evil).trusted
    assert not launch.load_project(str(env / "nothing-here"))
    assert widgets.SHOWN[-1][0] == "No project configuration"


# -- updates, themes, languages --


def test_update_notice(window, wait):
    window.update_checked(0, "installed: 0.9.0 (/x)\navailable: 0.10.1 (https://example.org)\n", "")
    assert "0.10.1 is available" in window.system.update_text.text() and "0.10.1" in window.statusBar().currentMessage()
    window.update_checked(0, "installed: 0.10.1 (/x)\navailable: 0.10.1 (https://example.org)\n", "")
    assert "up to date" in window.system.update_text.text()
    window.update_checked(1, "", "sandbox-ai update: this copy was not installed by install.sh")
    assert "No update information" in window.system.update_text.text()
    # The real command, asynchronously: the window is not blocked while it runs.
    began = time.monotonic()
    window.check_updates()
    assert time.monotonic() - began < 0.5
    assert wait(lambda: not window.update_check.running, 90)
    assert window.system.update_text.text() != "Checking for a newer sandbox-ai…"


def test_themes_are_applied_and_saved(window, qapp):
    system = window.system
    system.theme.setCurrentIndex(system.theme.findData("dark"))
    qapp.processEvents()  # applied after the combo box's own event (theme.apply_soon)
    assert theme.is_dark() and theme.current() == "dark" and window.sai.settings()["theme"] == "dark"
    system.theme.setCurrentIndex(system.theme.findData("light"))
    qapp.processEvents()
    assert not theme.is_dark()
    # Every theme of the app is offered, applied at once and saved; the board and the navigation follow it.
    assert [system.theme.itemData(i) for i in range(system.theme.count())] == list(theme.THEMES)
    for name in ("hacker", "cyberpunk", "amber", "unicorn", "win311", "mac", "fallout"):
        system.theme.setCurrentIndex(system.theme.findData(name))
        qapp.processEvents()
        assert theme.current() == name and window.sai.settings()["theme"] == name
        assert theme.is_dark() == theme.THEMES_DATA[name]["dark"]
        qapp.processEvents()
        assert window.board.view.backgroundBrush() is not None
        image = window.nav_item("launch").icon().pixmap(20, 20).toImage()
        tints = {image.pixelColor(x, y).name() for x in range(20) for y in range(20) if image.pixelColor(x, y).alpha() > 200}
        assert theme.colors()["nav_text"] in tints, "the navigation's icons take the theme's colour"
    system.theme.setCurrentIndex(system.theme.findData("system"))
    assert window.sai.settings()["theme"] == "system"
    system.language.setCurrentIndex(system.language.findData("de"))
    assert window.sai.settings()["language"] == "de"


def test_the_window_in_german(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "x"))
    i18n.set_language("de")
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    try:
        assert [w.nav.item(i).text() for i in range(w.nav.count())][:7] == [
            "PROJEKT", "Start", "Tafel", "LAUFEND", "Sandboxen & Sitzungen", "Freigaben", "Terminals"]
        assert w.approvals.allow_button.text() == "Erlauben"
        assert w.launch.form.tabText(1) == "Netzwerk"
    finally:
        w.shutdown()
        w.close()
        w.deleteLater()


def test_main_starts_quietly_without_sandbox_ai(qapp, monkeypatch):
    from sandbox_ai_app import main
    monkeypatch.setattr(backend, "locate", lambda: (_ for _ in ()).throw(backend.Unavailable("missing")))
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a: shown.append(a))
    monkeypatch.setattr(QtWidgets, "QApplication", type("A", (), {"__init__": lambda self, *a: None,
                                                                    "instance": staticmethod(lambda: None),
                                                                    "setApplicationName": lambda self, n: None}))
    assert main.main([]) == 1 and shown


def test_no_shell_with_untrusted_strings():
    for path in (HERE.parent / "sandbox_ai_app").glob("*.py"):
        text = path.read_text()
        assert "shell=True" not in text and "os.system(" not in text, path
    assert subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True).returncode == 0


# -- devices and limits of a running sandbox --


def test_devices_and_limits_of_a_running_sandbox(window, env, monkeypatch, wait):
    from sandbox_ai_app import ui
    run = fake_run(env)
    (run / "devices.json").write_text(json.dumps(["/dev/video0"]))
    page = window.sandboxes
    page.refresh()
    page.table.selectRow(0)
    if not (window.sai.directory / "devices.py").is_file():
        pytest.skip("this sandbox-ai has no devices command")
    assert wait(lambda: "/dev/video0" in page.devices_label.text(), 20), page.devices_label.text()
    calls = []
    monkeypatch.setattr(window.sai, "device_candidates", lambda: [("/dev/video1", None, "a camera"),
                                                                 ("/dev/mem", "raw memory", None)])
    monkeypatch.setattr(window.sai, "device_check", lambda path: (path, ["a camera: the agent can take pictures"]))
    monkeypatch.setattr(window.sai, "add_device", lambda run, path: calls.append(("add", run, path)) or (0, "added", ""))
    monkeypatch.setattr(window.sai, "remove_device", lambda run, path: calls.append(("remove", run, path)) or (0, "", ""))
    monkeypatch.setattr(window.sai, "set_limits", lambda run, *values: calls.append(("limits", run, values)) or (0, "", ""))
    shown = []

    class FakeDevices:
        def __init__(self, parent, candidates):
            shown.append(candidates)

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def path(self):
            return "/dev/video1"

    class FakeLimits:
        def __init__(self, parent, run):
            pass

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def values(self):
            return "4g", "", "none"
    monkeypatch.setattr(ui, "DeviceDialog", FakeDevices)
    monkeypatch.setattr(ui, "LimitsDialog", FakeLimits)
    page.add_device()
    assert wait(lambda: calls, 20) and calls[0] == ("add", "claude-abc", "/dev/video1")
    assert shown[0][1][1] == "raw memory", "refused devices are listed with the launcher's reason"
    assert any("take pictures" in text for _, text, _ in widgets.SHOWN), "the launcher's notes before adding"
    assert wait(lambda: not widgets.busy(page), 20)  # the add has finished (its button is greyed out until then)
    page.remove_device()
    assert wait(lambda: len(calls) == 2, 20) and calls[1] == ("remove", "claude-abc", "/dev/video0")
    page.limits()
    assert wait(lambda: len(calls) == 3, 20) and calls[2] == ("limits", "claude-abc", ("4g", "", "none"))


def test_workspaces_of_a_running_sandbox(window, env, monkeypatch, wait):
    from sandbox_ai_app import ui
    fake_run(env)
    page = window.sandboxes
    entries = [{"host": "/home/me/project", "sandbox": "/work", "mode": "rw", "source": "start"},
               {"host": "/home/me/docs", "sandbox": "/workspaces/docs", "mode": "ro", "source": "added"}]
    monkeypatch.setattr(window.sai, "workspaces", lambda run: entries)
    page.refresh()
    page.table.selectRow(0)
    assert wait(lambda: "/workspaces/docs" in page.workspaces_label.text(), 20), page.workspaces_label.text()
    assert "/work " not in page.workspaces_label.text(), "only the added ones"
    calls = []
    monkeypatch.setattr(window.sai, "add_workspace", lambda run, d, ro: calls.append(("add", run, d, ro)) or (0, "", ""))
    answers = iter([(1, "", "sandbox-ai workspace: busy. Let the agent leave it, or --force"), (0, "removed", "")])
    monkeypatch.setattr(window.sai, "remove_workspace",
                        lambda run, value, force: calls.append(("remove", run, value, force)) or next(answers))

    class FakeWorkspace:
        def __init__(self, parent, run):
            self.read_only = QtWidgets.QCheckBox()

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def directory(self):
            return "/home/me/more"
    monkeypatch.setattr(ui, "WorkspaceDialog", FakeWorkspace)
    shown = len(widgets.SHOWN)
    page.add_workspace()
    assert wait(lambda: calls, 20) and calls[0] == ("add", "claude-abc", "/home/me/more", False)
    assert any("can write" in text for _, text, _ in widgets.SHOWN[shown:]), "a writable add is confirmed first"
    assert wait(lambda: not widgets.busy(page), 20)
    page.remove_workspace()
    assert wait(lambda: len(calls) == 3, 20), calls
    assert calls[1] == ("remove", "claude-abc", "/workspaces/docs", False)
    assert calls[2] == ("remove", "claude-abc", "/workspaces/docs", True), "busy: asked, then forced"


def test_the_board_applies_devices_and_limits_live(window, env, monkeypatch, wait):
    fake_run(env)
    board = window.board
    board.add_node("workspace", path=str(env / "project"))
    board.add_node("devices", **{"--device": ["/dev/video0"]})
    calls = []
    monkeypatch.setattr(window.sai, "run_for", lambda workspace: "claude-abc")
    monkeypatch.setattr(window.sai, "add_device", lambda run, path: calls.append((run, path)) or (0, "ok", ""))
    board.inspect(board.board.first("devices"))
    labels = [w.text() for w in board.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any("Live, until the sandbox ends" in t for t in labels)
    button = next(b for b in board.inspector_holder.findChildren(QtWidgets.QPushButton) if "now" in b.text())
    button.click()
    assert wait(lambda: calls == [("claude-abc", "/dev/video0")], 20)


def test_the_network_dialog_offers_what_the_installed_sandbox_ai_can(qapp):
    from sandbox_ai_app.ui import NetworkDialog
    new = NetworkDialog(None, {"pypi": ["pypi.org"]}, "Allow", {"--allow-host-port", "--dns", "--git-token-env"})
    new.hosts.setText("example.org")
    new.host_ports.setText("3000 11434")
    new.dns.setChecked(True)
    new.git_url.setText("http://localhost:3080")
    new.git_kind.setCurrentIndex(new.git_kind.findData("secret"))
    new.git_ref.setText("GITEA")
    assert new.arguments() == ["--allow-host", "example.org", "--allow-host-port", "3000", "--allow-host-port",
                               "11434", "--dns", "--git-token-secret", "http://localhost:3080=GITEA"]
    new.git_ref.setText("")  # a login needs both: the server and where its token is
    assert "--git-token-secret" not in new.arguments()
    # An older sandbox-ai: the fields are not offered, nothing of them is passed.
    old = NetworkDialog(None, {}, "Allow", set())
    old.host_ports.setText("3000")
    old.dns.setChecked(True)
    old.git_url.setText("http://localhost:3080")
    old.git_ref.setText("X")
    assert old.arguments() == []
    labels = [old.layout().itemAt(i, old.layout().ItemRole.LabelRole) for i in range(old.layout().rowCount())]
    texts = [item.widget().text() for item in labels if item is not None and item.widget() is not None]
    assert "Ports on this computer" not in texts and "DNS" not in texts
    for dialog in (new, old):
        dialog.deleteLater()


def test_net_options_reads_the_installed_parser(env):
    sai = backend.SandboxAI()
    options = sai.net_options("allow")
    assert "--allow-host" in options and "--allow-tcp" in options
    assert sai.net_options("no-such-command") == set()


def test_an_edit_while_the_images_page_is_built_does_not_crash(qapp, env, monkeypatch):
    """Regression (app.log of 0.1.46): edited() started check_timer before __init__ had made it, when the editor
    changed while it was built (a theme's re-highlighting): AttributeError 'ImagesPage' has no 'check_timer'."""
    from sandbox_ai_app import images_page
    built = images_page.ImagesPage.build_editor

    def build_and_change(self):
        box = built(self)
        self.fragment.setPlainText("RUN true")  # a change before __init__ is done
        return box
    monkeypatch.setattr(images_page.ImagesPage, "build_editor", build_and_change)
    reported = []  # Qt hands an exception in a slot to sys.excepthook instead of raising it
    monkeypatch.setattr(sys, "excepthook", lambda *info: reported.append(info))
    page = images_page.ImagesPage(backend.SandboxAI(), lambda *args: None)
    assert reported == [] and page.check_timer.isSingleShot() and page.check_timer.interval() == 200


def test_the_navigation_folds_and_shrinks_to_save_room(window):
    """Sections fold one by one or all at once, the navigation becomes a strip of icons, a page reached another way
    unfolds its section, and all of it is kept for the next start."""
    from sandbox_ai_app import ui
    nav, rows = window.nav, window.nav_rows
    window.show_page("launch", raise_window=False)
    window.fold_all(True)
    assert nav.isRowHidden(rows["connections"]) and nav.isRowHidden(rows["hosts"])
    assert not nav.isRowHidden(rows["launch"])  # the current page's section stays open
    assert set(window.state.get("nav_collapsed")) == {"Running", "Insight", "Manage", "System"}
    window.show_page("hosts", raise_window=False)  # e.g. Ctrl+number, a tour, a link
    assert not nav.isRowHidden(rows["hosts"]) and window.current_page() == "hosts"
    window.fold_all(False)
    assert window.collapsed == set() and not nav.isRowHidden(rows["connections"])
    window.toggle_section("Insight", True)
    window.set_compact_nav(True)
    assert nav.width() == ui.COMPACT_NAV_WIDTH and not window.brand.isVisibleTo(window)
    assert nav.item(rows["files"]).text() == "" and nav.item(rows["files"]).toolTip() == "Files"
    assert not nav.isRowHidden(rows["files"])  # the strip shows every page, folded or not
    assert all(nav.isRowHidden(header) for header, _ in window.section_rows.values())
    assert window.actions["nav-compact"].isChecked() and window.state.get("nav_compact") is True
    window.set_compact_nav(False)
    assert nav.width() == ui.NAV_WIDTH and nav.item(rows["files"]).text() == "Files"
    assert nav.isRowHidden(rows["files"])  # its section is still folded
    assert nav.item(rows["launch"]).toolTip() == "Start (Ctrl+1)"


def test_a_start_in_the_apps_terminal_opens_one_tab_not_two(window, env, monkeypatch, tmp_path):
    """Regression: a start opened its terminal tab ("claude") and, once its run showed up, a second tab for the
    same sandbox (claude-PROJECT-xxxx, auto_tabs). A run started elsewhere still gets its tab."""
    from sandbox_ai_app import terminal, ui
    project = tmp_path / "not-public"
    project.mkdir()
    opened = []
    monkeypatch.setattr(terminal, "available", lambda: (True, ""))
    monkeypatch.setattr(window.terminals, "open", lambda argv, title, directory=None: opened.append(title))
    real_setting = backend.setting
    monkeypatch.setattr(backend, "setting", lambda name, *a, **k: True if name == "auto_tabs" else real_setting(name, *a, **k))
    assert ui.started_agent(window.sai.launch_argv("claude", ["--allow-pypi"])) == "claude"
    assert ui.started_agent(window.sai.command("build", "claude")) is None
    window.in_terminal(window.sai.launch_argv("claude", []), "claude", embedded=True, cwd=str(project))
    assert opened == ["claude"]
    before = window.tabs.count()

    def instance(run, agent="claude", folder=project):
        return backend.Instance("run", f"run:{run}", run, run=run, project=str(folder), agent=agent, state="running")
    window.sync_tabs({"run:claude-not-public-1a2b": instance("claude-not-public-1a2b")})
    assert window.tabs.count() == before  # its terminal tab shows it
    window.sync_tabs({"run:claude-not-public-1a2b": instance("claude-not-public-1a2b"),
                      "run:claude-not-public-9z9z": instance("claude-not-public-9z9z")})
    assert window.tabs.count() == before + 1  # started elsewhere (the CLI): its own tab


def test_the_mcp_catalogs_load_by_themselves_once_and_quietly(window, monkeypatch, wait):
    """The catalogs load the first time the page is shown (in the background); a problem of that automatic load
    is a line on the page, no dialog; Reload loads again and says problems as before."""
    calls = []
    monkeypatch.setattr(window.sai, "mcp_catalog", lambda: calls.append(1) or (
        [("mcp", {"name": "duckduckgo", "network": "allowlist", "title": "DuckDuckGo", "digest": None})],
        ["hub: its catalog is not signed by a trusted key"]))
    shown = len(widgets.SHOWN)
    window.show()
    window.show_page("mcp", raise_window=False)
    assert wait(lambda: window.mcp.catalog.rowCount() == 1, 10)
    assert calls == [1] and "not signed" in window.mcp.catalog_status.text()
    assert len(widgets.SHOWN) == shown  # no dialog for the automatic load
    window.show_page("launch", raise_window=False)
    window.show_page("mcp", raise_window=False)
    assert calls == [1]  # once
    window.mcp.load_button.click()
    assert wait(lambda: len(calls) == 2 and not widgets.busy(window.mcp), 10)
    assert any(title == "Catalogs" for title, *_ in widgets.SHOWN[shown:])  # asked for: said in a dialog


def test_a_theme_from_its_combo_box_is_applied_after_the_event_and_keeps_the_style(window, qapp, monkeypatch):
    """Regression (a segfault on theme change): the combo box's slot replaced the application's QStyle while the
    combo box was still handling its event. Now the theme is applied after that event, and the QStyle object is
    only replaced when the style really changes."""
    from sandbox_ai_app import theme
    theme.apply("light")
    combo = window.system.theme
    applied, styles = [], []
    monkeypatch.setattr(theme, "apply", lambda mode, app=None, real=theme.apply: applied.append(mode) or real(mode, app))
    real_set_style = QtWidgets.QApplication.setStyle
    monkeypatch.setattr(QtWidgets.QApplication, "setStyle", lambda *args: styles.append(args[-1]) or real_set_style(args[-1]))
    combo.setCurrentIndex(combo.findData("dark"))
    assert applied == []  # not inside the combo box's own signal
    qapp.processEvents()
    assert applied == ["dark"] and theme.current() == "dark"
    assert styles == []  # Fusion to Fusion: the QStyle object is not replaced
    combo.setCurrentIndex(combo.findData("system"))
    qapp.processEvents()
    assert theme.current() == "system"


def test_a_session_started_in_the_app_leaves_the_tab_of_a_run_started_elsewhere(window, env, monkeypatch, tmp_path):
    """Regression: a session started in the app's terminal counted as the app's own one-off run, so the next run of
    the same agent and project started on the command line (within two minutes) got no tab."""
    from sandbox_ai_app import terminal
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(terminal, "available", lambda: (True, ""))
    monkeypatch.setattr(window.terminals, "open", lambda argv, title, directory=None: None)
    real_setting = backend.setting
    monkeypatch.setattr(backend, "setting", lambda name, *a, **k: True if name == "auto_tabs" else real_setting(name, *a, **k))
    window.in_terminal(window.sai.launch_argv("claude", [], session="work"), "claude (work)", embedded=True,
                       cwd=str(project))
    window.in_terminal(window.sai.launch_argv("claude", [], ["--session", "x"]), "claude", embedded=True,
                       cwd=str(project))  # --session among the agent's own arguments: still a one-off run
    assert len(window.own_starts) == 1
    before = window.tabs.count()

    def instance(kind, name):
        return backend.Instance(kind, f"{kind}:{name}" if kind == "session" else f"run:{name}", name,
                                run=name if kind == "run" else "claude-proj-1a2b", session=name if kind == "session"
                                else None, project=str(project), agent="claude", state="running")
    window.sync_tabs({"session:work": instance("session", "work"), "run:claude-proj-1a2b": instance("run", "claude-proj-1a2b"),
                      "run:claude-proj-cli1": instance("run", "claude-proj-cli1")})
    keys = {window.tabs.entry(i)["key"] for i in range(window.tabs.count())}
    assert window.tabs.count() == before + 2 and {"session:work", "run:claude-proj-cli1"} <= keys


@pytest.mark.parametrize("language", ("en", "de"))
def test_rule_questions_say_what_they_ask(language):
    """Regression: a question a project's rules ask (an MCP tool call, an HTTP request, an agent's tool, a device, a
    canary) showed only its kind ("mcp"), in the table and in the notification: it was allowed blind."""
    from sandbox_ai_app import pages
    i18n.set_language(language)
    questions = {"mcp": {"server": "fetch", "tool": "fetch_page", "arguments": {"url": "https://x.example/\x1b"},
                         "rule": "ask-fetch", "ruleset": "research"},
                 "tool": {"agent": "claude", "tool": "Bash", "arguments": "rm -rf build", "rule": "r1", "ruleset": "p"},
                 "http": {"method": "POST", "url": "https://api.github.com/repos/a/b/issues", "rule": "writes",
                          "ruleset": "github"},
                 "device": {"nodes": ["/dev/ttyUSB0"], "value": "usb:1a86:7523", "rule": "d", "ruleset": "lab"},
                 "canary": {"canary": "aws-key", "summary": "the decoy key left the sandbox"}}
    seen = {"mcp": ["fetch_page", "fetch", "https://x.example/", "ask-fetch", "research"],
            "tool": ["claude", "Bash", "rm -rf build"], "http": ["POST", "https://api.github.com/repos/a/b/issues",
                                                               "writes"],
            "device": ["/dev/ttyUSB0", "lab"], "canary": ["aws-key", "the decoy key left the sandbox"]}
    for kind, question in questions.items():
        text = pages.describe({"kind": kind, **question})
        assert all(part in text for part in seen[kind]), (kind, text)
        assert "\x1b" not in text, "escaped"
        assert pages.kind_name(kind) == i18n.tr({"mcp": "MCP tool call", "tool": "agent tool", "http": "HTTP request",
                                                 "device": "device", "canary": "canary"}[kind])
    assert pages.describe({"kind": "mcp", "arguments": "x" * 1000}).count("x") < 400, "shortened"
    i18n.set_language("en")
