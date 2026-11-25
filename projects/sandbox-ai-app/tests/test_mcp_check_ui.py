"""The MCP page's Check and Check all (the user, 2026-11-25: "Bitte auch prüfen, ob wir mit anderen MCP-Servern solche
Probleme haben"), offscreen: `sandbox-ai mcp check [NAME...|--all] --json` on the host shown, in the background,
nothing asked first (it changes nothing); the result in the Check column - ok with its tools, or "failed: WHY" with
the reason and the fix in its tooltip; a server's last failure names its log. The texts are there in German."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, i18n, kits_page, mcp_fields as MF, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
DIGEST = "sha256:" + "1" * 64
ROWS = [{"server": "gowitness", "result": "failed", "tools": None, "seconds": 3.2,
         "reason": "the MCP server ended before it answered (exit status 126: Error: crun: the requested cgroup "
                   "controller `pids` is not available); the human sees more: sandbox-ai mcp show gowitness",
         "fix": "sandbox-ai doctor shows the cgroup delegation", "note": None, "log": "/x/supervisor.log"},
        {"server": "time", "result": "ok", "tools": 2, "seconds": 1.5, "reason": None, "fix": None,
         "note": None, "log": "/y/supervisor.log"}]


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    path = tmp_path / "config" / "sandbox-ai" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "gowitness": {"image": f"ghcr.io/c0decave/sandbox-ai-mcp-gowitness@{DIGEST}", "network": "full"},
        "time": {"image": f"ghcr.io/c0decave/sandbox-ai-mcp-time@{DIGEST}", "network": "none"}}))
    return tmp_path


@pytest.fixture
def page(qapp, homes, monkeypatch):
    from sandbox_ai_app import ui
    sai = backend.SandboxAI()
    monkeypatch.setattr(ui, "background", lambda function, *args, done, **kw: done(function(*args), None))
    monkeypatch.setattr(MF, "background", lambda function, *args, done, **kw: done(function(*args), None))
    sai.ran = []

    def host_run(host, *args, timeout=120, input=None):
        sai.ran.append(args)
        if args[:2] == ("mcp", "check"):
            names = [a for a in args[2:] if not a.startswith("--")]
            return 1, json.dumps([row for row in ROWS if not names or row["server"] in names]), "checking..."
        return 0, "", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    asked = []
    monkeypatch.setattr(ui, "ask_first", lambda *a, **kw: asked.append(a) or True)
    shown = ui.McpPage(sai)
    shown.asked = asked
    yield shown
    shown.close()


def row_of(table, name):
    return next(r for r in range(table.rowCount()) if table.item(r, 0).text() == name)


def test_check_all_fills_the_check_column_without_asking(page):
    headers = [page.servers.horizontalHeaderItem(c).text() for c in range(page.servers.columnCount())]
    assert headers[-1] == "Check"
    page.check_all_button.click()
    assert ("mcp", "check", "--all", "--json") in page.sai.ran and not page.asked
    bad = row_of(page.servers, "gowitness")
    assert page.servers.item(bad, 5).text().startswith("failed: the MCP server ended before it answered (exit status")
    tip = page.servers.item(bad, 0).toolTip()  # (the row's tooltip)
    assert "the requested cgroup controller" in tip and "Fix: sandbox-ai doctor shows the cgroup delegation" in tip
    assert page.servers.item(row_of(page.servers, "time"), 5).text() == "ok, 2 tools, 1.5 s"
    assert "1 of 2 ok; failed: gowitness" in page.catalog_status.toPlainText()


def test_check_takes_the_servers_selected_and_is_on_the_menu(page):
    page.servers.selectRow(row_of(page.servers, "time"))
    page.check_button.click()
    assert ("mcp", "check", "time", "--json") in page.sai.ran
    texts = [a.text() for a in kits_page.context_menu(page.servers, page.server_buttons).actions()]
    assert "Check" in texts and "Check all" in texts


def test_a_last_failure_names_its_log(page, homes, monkeypatch):
    problem = {"time": "2026-11-25T10:00:00Z", "what": "MCP server gowitness's process ended while it was used",
               "why": "exit status 1", "fix": "read the server's log", "log": "/x/supervisor.log"}
    monkeypatch.setattr(page.sai, "host_mcp_notes", lambda host: {"gowitness": {"problem": problem, "below": {},
                                                                                "newer": None}})
    page.refresh()
    tip = page.servers.item(row_of(page.servers, "gowitness"), 0).toolTip()
    assert "Its log: /x/supervisor.log" in tip, tip


def test_the_backend_keeps_only_well_formed_rows(page):
    def host_run(host, *args, timeout=120, input=None):
        return 1, json.dumps([{"server": "bad name", "result": "ok"}, {"server": "x", "result": "weird",
                                                                         "reason": "a\x1b[31mred"}]), ""
    page.sai.host_run = host_run
    rows = page.sai.host_mcp_check("local")
    assert [row["server"] for row in rows] == ["x"] and rows[0]["result"] == "failed"
    assert "\x1b" not in rows[0]["reason"]
    page.sai.host_run = lambda host, *args, **kw: (2, "", "sandbox-ai mcp: no such command")
    with pytest.raises(ValueError, match="no such command"):
        page.sai.host_mcp_check("local")
    with pytest.raises(ValueError):
        page.sai.host_mcp_check("local", ["bad name"])


def test_the_texts_are_there_in_german():
    for text in ("Check", "Check all", "ok, {count} tools, {seconds} s", "failed: {reason}", "Why: {reason}",
                 "{ok} of {count} ok", "Its log: {path}"):
        assert i18n.TEXTS[text]["de"], text
