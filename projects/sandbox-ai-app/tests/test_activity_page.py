"""The Activity page, offscreen: the timeline comes from the real `sandbox-ai activity --json --follow` of the
checkout in SANDBOX_AI_HOME, read from a log folder written here (no sandbox runs)."""
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import activity_page, backend, i18n, theme, widgets  # noqa: E402
from sandbox_ai_app.activity_page import RECORD_ROLE, ActivityFilter, ActivityModel  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def record(kind, summary, ident, time="2026-11-04T10:00:00.000Z", run="claude-1", **details):
    source = details.pop("source", "claude")
    return {"v": 1, "time": time, "run": run, "session": None, "agent": "claude", "kind": kind, "summary": summary,
            "details": details, "source": source, "id": ident}


def timeline(workspace, logs):
    return [
        record("session", "claude started", "s1", source="sandbox-ai", event="start", log_dir=str(logs),
               workspaces=[{"host": str(workspace), "sandbox": "/work"}]),
        record("prompt", "fix the tests", "p1", time="2026-11-04T10:00:01.000Z", text="fix the tests"),
        record("command", "pytest · exit 1", "c1", time="2026-11-04T10:00:02.000Z", command="pytest",
               status="error", exit_code=1),
        record("file", "modified /work/src/a.py (Edit)", "f1", time="2026-11-04T10:00:03.000Z", path="/work/src/a.py",
               action="modified"),
        record("network", "denied: lookup of evil.example", "n1", time="2026-11-04T10:00:04.000Z", source="egress",
               verdict="denied", host="evil.example", target="evil.example"),
        record("reply", "\x1b[31mred\x07 <b>bold</b>", "r1", time="2026-11-04T10:00:05.000Z", text="x\u202ey"),
        record("tokens", "1 model call(s)", "t1", time="2026-11-04T10:00:06.000Z", usage={"input_tokens": 3}),
    ]


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


def project(env):
    logs = env / "state" / "sandbox-ai" / "logs" / "app-1234"
    logs.mkdir(parents=True)
    workspace = env / "work"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("x\n")
    (logs / "activity.jsonl").write_text("".join(json.dumps(r) + "\n" for r in timeline(workspace, logs)) + "junk\n")
    return logs, workspace


def texts(table, column):
    model = table.model()
    return [model.data(model.index(row, column)) for row in range(model.rowCount())]


def select(page, ident):
    proxy = page.proxy
    for row in range(proxy.rowCount()):
        if proxy.data(proxy.index(row, 0), RECORD_ROLE)["id"] == ident:
            page.table.selectRow(row)
            page.table.setCurrentIndex(proxy.index(row, 0))
            return True
    return False


def test_model_updates_trims_and_filters(qapp, monkeypatch):
    model = ActivityModel()
    model.add([record("command", "make · running", "c1", status="running"), record("prompt", "hi", "p1")])
    model.add([{**record("command", "make · exit 0", "c1", status="ok"), "update": True}])
    assert model.rowCount() == 2 and model.data(model.index(0, 3)) == "make · exit 0"
    monkeypatch.setattr(activity_page, "KEEP", 3)
    model.add([record("prompt", f"p{i}", f"x{i}") for i in range(4)])
    assert model.rowCount() == 3 and model.data(model.index(0, 3)) == "p1"
    model.add([{**record("prompt", "p3 again", "x3"), "update": True}])
    assert model.data(model.index(2, 3)) == "p3 again"  # the index of ids survives the trim
    proxy = ActivityFilter()
    proxy.setSourceModel(model)
    proxy.set_filter(kinds=["command"])
    assert proxy.rowCount() == 0
    proxy.set_filter(kinds=list(activity_page.KINDS), text="P2")
    assert proxy.rowCount() == 1


def test_kind_colours_follow_the_theme(qapp):
    for mode in ("light", "dark"):
        theme.apply(mode)
        assert activity_page.kind_color(record("command", "x", "1")) == theme.color("warn")
        assert activity_page.kind_color(record("prompt", "x", "1")) == theme.colors()["accent"]
        assert activity_page.kind_color(record("network", "x", "1", verdict="denied")) == theme.color("bad")
    theme.apply("light")


def test_the_page_follows_filters_and_shows_details(window, env, wait):
    logs, workspace = project(env)
    page = window.activity
    window.show_page("activity")
    assert wait(lambda: page.model.rowCount() == 7, timeout=20)
    assert page.source.currentData() == ["--log-dir", str(logs)]
    assert texts(page.table, 1)[:3] == ["session", "prompt", "command"]
    assert texts(page.table, 2)[:2] == ["sandbox", "claude"]  # sandbox-ai's own record, then the agent's
    # Untrusted text: escaped in the table, the tooltip and the details.
    row = texts(page.table, 3).index("\\x1b[31mred\\x07 <b>bold</b>")
    tip = page.proxy.data(page.proxy.index(row, 3), QtCore.Qt.ItemDataRole.ToolTipRole)
    assert "&lt;b&gt;" in tip and "\x1b" not in tip
    assert select(page, "r1")
    assert "\\u202e" in page.detail.toPlainText() and "\u202e" not in page.detail.toPlainText()
    assert page.detail.toPlainText().split("\n")[1].strip() == '"v": 1,'  # the record's own order
    # New events arrive while following.
    with open(logs / "activity.jsonl", "a") as stream:
        stream.write(json.dumps(record("command", "make", "c2", time="2026-11-04T10:00:07.000Z")) + "\n")
    assert wait(lambda: page.model.rowCount() == 8, timeout=20)
    # Kinds and text.
    page.set_kinds(["command"])
    assert texts(page.table, 3) == ["pytest · exit 1", "make"] and page.kinds_button.text() == "Kinds (1)"
    page.filter.setText("pytest")
    assert texts(page.table, 3) == ["pytest · exit 1"] and "1 of 8" in page.status.text()
    page.filter.setText("")
    page.set_kinds(activity_page.KINDS)
    # The command's colour is the theme's.
    command_row = texts(page.table, 1).index("command")
    brush = page.proxy.data(page.proxy.index(command_row, 1), QtCore.Qt.ItemDataRole.ForegroundRole)
    assert brush.name() == QtWidgets.QApplication.palette().text().color().name() or brush.name() == theme.color("bad")


def test_export_json_and_text(window, env, wait, monkeypatch):
    logs, _ = project(env)
    page = window.activity
    window.show_page("activity")
    assert wait(lambda: page.model.rowCount() == 7, timeout=20)
    page.set_kinds(["prompt", "command"])
    target = env / "out.jsonl"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert page.export("json") == str(target)
    assert [json.loads(line)["id"] for line in target.read_text().splitlines()] == ["p1", "c1"]
    assert target.stat().st_mode & 0o777 == 0o600
    text = env / "out.txt"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(text), ""))
    page.export("text")
    lines = text.read_text().splitlines()
    assert len(lines) == 2 and lines[1].split()[2:4] == ["command", "claude"] and lines[1].endswith("pytest · exit 1")
    (env / "link.txt").symlink_to(env / "elsewhere.txt")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(env / "link.txt"), ""))
    assert page.export("text") is None and not (env / "elsewhere.txt").exists()


def test_jumps_to_connections_and_files(window, env, wait):
    logs, workspace = project(env)
    (logs / "connections-20261104-100000-claude-1.jsonl").write_text(json.dumps(
        {"time": "t", "event": "denied", "host": "evil.example"}) + "\n")
    (logs / "connections-20261104-090000-other.jsonl").write_text("")
    page = window.activity
    window.show_page("activity")
    assert wait(lambda: page.model.rowCount() == 7, timeout=20)
    assert select(page, "c1") and not page.connections_button.isEnabled() and not page.files_button.isEnabled()
    assert select(page, "n1") and page.connections_button.isEnabled()
    assert page.show_connections()
    assert window.pages.currentWidget() is window.connections
    assert wait(lambda: window.connections.file.currentText().endswith("-claude-1.jsonl"))  # listed in a thread
    assert window.connections.filter.text() == "evil.example"
    window.show_page("activity")
    assert select(page, "f1") and page.files_button.isEnabled()
    assert page.host_path(page.current()) == (str(workspace), "src/a.py")
    assert page.show_file()
    files = window.files
    assert window.pages.currentWidget() is files and files.relative == "src"
    assert [files.entry_at(r).name for r in widgets.selected_rows(files.table)] == ["a.py"]
    # A path outside the workspaces, or one that climbs out, has no place in the file browser.
    outside = record("file", "x", "f9", path="/etc/passwd")
    climbing = record("file", "x", "f8", path="/work/../../etc/passwd")
    assert page.host_path(outside) is None and page.host_path(climbing) is None


def test_without_logs_and_with_an_old_sandbox_ai(window, env, monkeypatch, tmp_path):
    page = window.activity
    window.show_page("activity")
    assert page.source.count() == 0 and "No sandbox has recorded activity" in page.status.text()
    old = tmp_path / "old-sandbox-ai"
    old.mkdir()
    monkeypatch.setattr(page.sai, "directory", old)
    page.source.addItem("x", ["--log-dir", str(tmp_path)])
    page.restart()
    assert "sandbox-ai update" in page.status.text()


def test_the_page_in_german(window, env, wait):
    project(env)
    i18n.set_language("de")
    try:
        from sandbox_ai_app.activity_page import ActivityPage
        page = ActivityPage(window.sai, window)
        page.show()
        assert wait(lambda: page.model.rowCount() == 7, timeout=20)
        assert texts(page.table, 1)[:3] == ["Sitzung", "Eingabe", "Befehl"]
        assert page.model.headerData(3, QtCore.Qt.Orientation.Horizontal) == "Was"
        page.shutdown()
        page.deleteLater()
    finally:
        i18n.set_language("en")


def test_stopping_the_timeline_does_not_wait_on_the_gui_thread(window, monkeypatch, wait):
    """Regression: ActivityPage.stop() waited up to 2 s for the killed command (every source change, follow
    toggle and shutdown). The command is killed and deleted once it has ended, without waiting."""
    import sys
    waits = []
    monkeypatch.setattr(activity_page.QProcess, "waitForFinished", lambda self, *a: waits.append(a) or True)
    page = window.activity
    process = activity_page.QProcess(page)
    process.setProgram(sys.executable)
    process.setArguments(["-c", "import time; time.sleep(60)"])
    process.finished.connect(lambda code, _status, p=process: page.finished(p, code))
    page.process = process
    process.start()
    assert wait(lambda: process.state() == activity_page.QProcess.ProcessState.Running, 10)
    destroyed = []
    process.destroyed.connect(lambda *_: destroyed.append(True))
    page.stop()
    assert waits == [] and page.process is None, "nothing waited for"
    assert wait(lambda: destroyed, 10), "deleted once it has ended"
