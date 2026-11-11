"""Reports in the app (sandbox-ai report): Report… on the Sandboxes page for the selected starts (one or several, this
machine's), and on the Statistics page for the log folder shown; the dialog (format, file, period, parts), the report
written in the background by sandbox-ai and opened afterwards; the backend's command line."""
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, reports, widgets  # noqa: E402
from conftest import read_now  # noqa: E402
from test_ui import env, sandboxes_tree, window  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


class Accepting:
    """ReportDialog as a human fills it in: VALUES given, accepted at once."""

    made = []

    def __init__(self, parent, title, what, label, parts=True):
        self.title, self.what, self.label, self.parts = title, what, label, parts
        Accepting.made.append(self)

    def exec(self):
        return QtWidgets.QDialog.DialogCode.Accepted

    def values(self):
        return dict(self.answer)

    def deleteLater(self):
        pass


def accept_with(monkeypatch, **answer):
    Accepting.made = []
    Accepting.answer = {"kind": "pdf", "path": "", "since": None, "parts": True, "open": True, **answer}
    monkeypatch.setattr(reports, "ReportDialog", Accepting)


def test_the_backend_runs_sandbox_ai_report(window, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(window.sai, "call", lambda *args, timeout=120, **kw: calls.append(args) or (0, "", ""))
    path = str(tmp_path / "r.pdf")
    assert window.sai.report(path, "pdf", ["work", "mcp-fetch"], since="2h", parts=False) == path
    assert calls[-1] == ("report", "--format", "pdf", "--output", path, "--no-parts", "--since", "2h", "--",
                         "work", "mcp-fetch")
    window.sai.report(path, "html", log_dir="/logs/web")
    assert calls[-1] == ("report", "--format", "html", "--output", path, "--log-dir", "/logs/web", "--")
    for bad in (dict(names=["-x"]), dict(names=["a b"]), dict(kind="docx", names=["a"]), dict(names=[]),
                dict(names=["a"], since="--all"), dict(log_dir="relative")):
        kind = bad.pop("kind", "pdf")
        with pytest.raises(ValueError):
            window.sai.report(path, kind, **bad)
    with pytest.raises(ValueError):
        window.sai.report("relative.pdf", "pdf", ["a"])
    monkeypatch.setattr(window.sai, "call", lambda *args, timeout=120, **kw: (1, "", "sandbox-ai report: no run"))
    with pytest.raises(ValueError, match="no run"):
        window.sai.report(path, "md", ["a"])
    assert window.sai.has_report()  # this checkout writes reports


def test_the_backend_writes_a_real_report(window, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "connections-20261111-100000-claude-1.jsonl").write_text(json.dumps(
        {"time": "2026-11-11T10:00:00Z", "event": "denied", "host": "evil.example", "destination": "192.0.2.6:443",
         "reason": "not allowed by the network options"}) + "\n")
    for kind, start in (("html", b"<!DOCTYPE html>"), ("pdf", b"%PDF-1.4"), ("md", b"<a id=")):
        path = tmp_path / f"report.{kind}"
        window.sai.report(str(path), kind, log_dir=str(logs))
        assert path.read_bytes().startswith(start)
    assert b"evil.example" in (tmp_path / "report.md").read_bytes()


def test_report_on_the_sandboxes_page(window, monkeypatch, wait, tmp_path):
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    button = page.manage_buttons["report"]
    from sandbox_ai_app import start_tree
    found = {item.data(0, start_tree.KEY_ROLE): item for item in page.table.start_items()}
    page.table.select_item(found["session:work"])
    assert button.isEnabled()
    page.table.select_item(found[backend.remote_key("ct1", "web")])
    assert not button.isEnabled() and "sandbox-ai report there" in button.toolTip()
    written, opened, notices = [], [], []
    monkeypatch.setattr(window.sai, "report", lambda *args: written.append(args) or args[0])
    monkeypatch.setattr(reports, "opener", opened.append)
    page.notice.connect(lambda level, title, text: notices.append((level, title, text)))
    target = str(tmp_path / "work.pdf")
    accept_with(monkeypatch, path=target)
    page.table.select_item(found["session:work"])
    page.report()
    assert wait(lambda: opened, 15) and opened == [target]
    assert written[-1] == (target, "pdf", ["work"], None, True, None)
    assert notices and notices[-1][0] == "success" and target in notices[-1][2]
    assert "work" in Accepting.made[-1].what
    # Several rows: one report of those of this machine (the host's row is left out); an MCP server by its run.
    from PySide6.QtCore import QItemSelectionModel
    flags = QItemSelectionModel.SelectionFlag
    page.table.clearSelection()
    for key in ("session:work", "mcp:fetch", backend.remote_key("ct1", "web")):
        item = found.get(key) or page.table.find_item(start_tree.row_id("local", key))
        if item is not None:
            page.table.selectionModel().select(page.table.indexFromItem(item), flags.Select | flags.Rows)
    assert page.several() and button.isEnabled()
    accept_with(monkeypatch, path=str(tmp_path / "two.html"), kind="html", since="2h", open=False, parts=False)
    page.report()
    assert wait(lambda: len(written) == 2, 15)
    path, kind, names, log_dir, parts, since = written[-1]
    assert kind == "html" and set(names) <= {"work", "mcp-fetch"} and "work" in names and since == "2h"
    assert parts is False and log_dir is None and len(opened) == 1, "not opened when the human said so"
    # A file in a folder that does not exist: said, nothing written.
    accept_with(monkeypatch, path=str(tmp_path / "missing" / "r.pdf"))
    widgets.SHOWN.clear()
    page.table.clearSelection()
    page.table.select_item(found["session:work"])
    page.report()
    assert any("Not a file in an existing folder" in text for _, text, _ in widgets.SHOWN) and len(written) == 2
    # It failed: said, nothing opened.
    monkeypatch.setattr(window.sai, "report", lambda *args: (_ for _ in ()).throw(ValueError("no run named work")))
    accept_with(monkeypatch, path=target)
    widgets.SHOWN.clear()
    page.report()
    assert wait(lambda: any("no run named work" in text for _, text, _ in widgets.SHOWN), 15) and len(opened) == 1


def test_report_on_the_statistics_page(window, env, monkeypatch, wait, tmp_path):
    page = window.statistics
    page.refresh_directories()
    assert not page.report_button.isEnabled(), "no logs yet"
    logs = env / "state" / "sandbox-ai" / "logs" / "proj-1"
    logs.mkdir(parents=True)
    (logs / "connections-20261111-100000-claude-1.jsonl").write_text(json.dumps({"event": "http",
                                                                                 "host": "pypi.org"}) + "\n")
    page.refresh_directories()
    assert wait(lambda: not widgets.busy(page)) and page.report_button.isEnabled()
    written, opened = [], []
    monkeypatch.setattr(window.sai, "report", lambda *args: written.append(args) or args[0])
    monkeypatch.setattr(reports, "opener", opened.append)
    accept_with(monkeypatch, path=str(tmp_path / "logs.md"), kind="md")
    page.report()
    assert wait(lambda: opened, 15)
    assert written[-1] == (str(tmp_path / "logs.md"), "md", [], str(logs), True, None)
    assert Accepting.made[-1].parts is False, "a log folder's report has no parts to choose"


def test_the_dialog_follows_the_format(qapp, env, tmp_path):
    dialog = reports.ReportDialog(None, "Report of work", "A report of work", "work/../x y")
    try:
        path = dialog.path.text()
        assert path.endswith(".pdf") and "sandbox-ai-report-work-..-x-y-" in os.path.basename(path)
        assert os.path.isabs(path)
        dialog.format.setCurrentIndex(dialog.format.findData("html"))
        assert dialog.path.text().endswith(".html") and dialog.path.text()[:-5] == path[:-4]
        dialog.path.setText("~/report")
        dialog.format.setCurrentIndex(dialog.format.findData("md"))
        values = dialog.values()
        assert values["path"] == str(Path.home() / "report.md") and values["kind"] == "md"
        assert values["parts"] and values["open"] and values["since"] is None
        dialog.since.setText(" 3d ")
        dialog.parts.setChecked(False)
        assert dialog.values()["since"] == "3d" and dialog.values()["parts"] is False
    finally:
        dialog.deleteLater()
    hidden = reports.ReportDialog(None, "t", "w", "logs", parts=False)
    try:
        hidden.parts.setChecked(False)
        assert hidden.values()["parts"] is True  # (not asked: every run of the folder)
    finally:
        hidden.deleteLater()
