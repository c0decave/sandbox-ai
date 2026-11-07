"""The MCP page's "Attach to…" (offscreen): an MCP server connected to an agent start through `sandbox-ai mcp attach`
(here stand-ins that record what they were asked), and whom each server serves."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "background", lambda function, *args, done, **kw: done(function(*args), None))
    sai = backend.SandboxAI()
    if not sai.mcp_attach_supported():
        pytest.skip("this sandbox-ai cannot attach MCP servers to starts")
    monkeypatch.setattr(sai, "mcp_servers", lambda: [("demo", "running", "python3 demo"), ("web", "stopped", "x")])
    monkeypatch.setattr(sai, "mcp_serving", lambda: {"demo": ["api"]})
    monkeypatch.setattr(sai, "mcp_attach_targets", lambda: [
        {"name": "api", "agent": "claude", "state": "running", "mcp": ["demo"]},
        {"name": "docs", "agent": "codex", "state": "stopped", "mcp": []}])
    asked = []
    monkeypatch.setattr(sai, "mcp_attach", lambda name, start, restart=False: asked.append((name, start, restart)) or {
        "attached": "next start", "message": f"{start} reaches the MCP server {name} from its next start on"})
    shown = ui.McpPage(sai)
    shown.asked, shown.ui = asked, ui
    yield shown
    shown.close()


def test_the_servers_say_whom_they_serve(page):
    rows = [[page.servers.item(r, c).text() for c in range(4)] for r in range(page.servers.rowCount())]
    assert ["demo", "running", "api", "python3 demo"] in rows and ["web", "stopped", "", "x"] in rows


def test_attach_to_a_start(page, monkeypatch):
    dialogs = []

    def accept(dialog):
        dialogs.append([dialog.start.itemText(i) for i in range(dialog.start.count())])
        dialog.start.setCurrentIndex(1)
        dialog.restart.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(page.ui.AttachServerDialog, "exec", accept)
    row = next(r for r in range(page.servers.rowCount()) if page.servers.item(r, 0).text() == "web")
    page.servers.selectRow(row)
    page.attach()
    assert page.asked == [("web", "docs", True)]
    assert dialogs[0][0].startswith("api (claude, running)") and dialogs[0][1].startswith("docs (codex, stopped)")
    assert any("from its next start on" in text for _, text, *_ in widgets.SHOWN)
    page.servers.clearSelection()
    page.servers.setCurrentItem(None)
    page.asked.clear()
    page.attach()  # nothing chosen: nothing asked
    assert page.asked == []
