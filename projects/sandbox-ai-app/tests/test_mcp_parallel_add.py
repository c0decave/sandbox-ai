"""Several MCP servers can be added at once on the MCP page (user 2026-11-19: "warum kann man nicht weitere MCP-Server
hinzufügen, während bereits ein anderer hinzugefügt wird? man kann das doch im Hintergrund machen"): each `mcp add`
runs in the background of its own (`mcp add` writes the server list under its file lock), the Add button stays
usable, the status names every server still being added, and the same server is not added twice at once."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

ENTRIES = [("hub", {"name": n, "network": "none", "hosts": [], "title": n, "digest": "published",
                    "_signature": {"state": "good"}, "_catalog_name": "hub"}) for n in ("binwalk", "radare2")]


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import ui
    pending = []

    def background(function, *args, done, source=None, **kw):
        if kw.get("work"):  # an `mcp add`: held until the test lets it end
            for widget in (source if isinstance(source, (list, tuple)) else [source]):
                if widget is not None:
                    widget.setEnabled(False)  # as widgets.background does with its source
            pending.append((function, args, done, source))
            return
        done(function(*args), None)
    monkeypatch.setattr(ui, "background", background)
    sai = backend.SandboxAI()
    sai.ran = []

    def host_run(host, *args, timeout=120, input=None):
        sai.ran.append(args)
        return 0, f"{args[-1]} added\n", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_mcp_servers", lambda host: [])
    monkeypatch.setattr(sai, "host_mcp_catalog", lambda host, catalogs=(): ([(p, dict(e)) for p, e in ENTRIES], []))
    monkeypatch.setattr(ui, "ask_first", lambda *a, **k: True)
    monkeypatch.setattr(ui.AddServerDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    shown = ui.McpPage(sai)
    shown.sai_, shown.pending = sai, pending
    shown.load_catalog()
    yield shown
    shown.close()


def add_row(page, row):
    page.catalog.selectRow(row)
    page.add()


def finish(page, index):
    function, args, done, source = page.pending.pop(index)
    done(function(*args), None)


def test_a_second_server_is_added_while_the_first_still_is(page):
    add_row(page, 0)
    assert page.add_button.isEnabled(), "Add stays usable while a server is added"
    add_row(page, 1)
    assert len(page.pending) == 2
    status = page.catalog_status.text()
    assert "binwalk" in status and "radare2" in status
    finish(page, 0)
    assert [args[-1] for args in page.sai_.ran] == ["hub:binwalk"]
    status = page.catalog_status.text()
    assert "radare2" in status and "binwalk" in status  # radare2 still being added; binwalk said as added
    finish(page, 0)
    assert [args[-1] for args in page.sai_.ran] == ["hub:binwalk", "hub:radare2"]
    assert "radare2 added" in page.catalog_status.text() or "radare2" in page.catalog_status.text()


def test_the_same_server_is_not_added_twice_at_once(page):
    add_row(page, 0)
    add_row(page, 0)
    assert len(page.pending) == 1
    assert "binwalk" in page.catalog_status.text() and "already" in page.catalog_status.text()
    finish(page, 0)
    add_row(page, 0)  # once it is done it can be added again (replace)
    assert len(page.pending) == 1
