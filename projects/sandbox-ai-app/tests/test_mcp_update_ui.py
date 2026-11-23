"""The MCP page's Update and Update all (the user, 2026-11-23: "add mcp update"), offscreen: a server whose catalog
pins a newer image says so in its status (its tooltip: the digests, signed or not, the command); Update takes the
servers selected and Update all every server of yours - both on a button and on the servers' right-click menu, asked
first (mcp-update), then `sandbox-ai mcp update [NAME...] --yes --json` on the host shown; a server of an unsigned
catalog is named in the question and passed --unsigned. The texts are there in German."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, kits_page, mcp_fields as MF, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
IMAGE = "ghcr.io/c0decave/sandbox-ai-mcp-radare2"


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
        "radare2": {"image": f"{IMAGE}@{OLD}", "network": "none", "source": "mcp:radare2", "catalog": "CAT",
                    "signed_by": None},
        "time": {"image": f"ghcr.io/c0decave/sandbox-ai-mcp-time@{OLD}", "network": "none", "source": "mcp:time",
                 "catalog": "CAT", "signed_by": None}}))
    cache = tmp_path / "cache" / "sandbox-ai"
    cache.mkdir(parents=True)
    (cache / "mcp-catalog-env.json").write_text(json.dumps({"CAT": {
        "mcp:radare2": {"signature": "good", "env": {}, "network": "none", "hosts": [], "image": f"{IMAGE}@{NEW}"},
        "mcp:time": {"signature": "good", "env": {}, "network": "none", "hosts": [],
                     "image": f"ghcr.io/c0decave/sandbox-ai-mcp-time@{OLD}"}}}))
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
        if args[:2] == ("mcp", "update"):
            names = [a for a in args[2:] if not a.startswith("--")]
            return 0, json.dumps({"servers": [{"server": n, "state": "newer"} for n in names],
                                  "updated": names}), ""
        return 0, "", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    asked = []
    monkeypatch.setattr(ui, "ask_first", lambda parent, action, title, text, accept, **kw: asked.append(
        (action, text)) or True)
    shown = ui.McpPage(sai)
    shown.asked = asked
    yield shown
    shown.close()


def row_of(table, name):
    return next(r for r in range(table.rowCount()) if table.item(r, 0).text() == name)


def test_the_real_notes_say_a_newer_image(homes):
    notes = backend.SandboxAI().host_mcp_notes("local")
    assert notes["radare2"]["newer"] == {"image": [f"{IMAGE}@{OLD}", f"{IMAGE}@{NEW}"], "signed": True}
    assert "time" not in notes, "its catalog pins the image it runs"


def test_the_status_says_it_and_its_tooltip_names_the_digests(page):
    row = row_of(page.servers, "radare2")
    assert page.servers.item(row, 1).text().endswith("· a newer image")
    tip = page.servers.item(row, 0).toolTip()
    assert "111111111111 → 222222222222" in tip and "sandbox-ai mcp update radare2" in tip and "signed" in tip
    assert "newer" not in page.servers.item(row_of(page.servers, "time"), 1).text()


def test_update_takes_the_servers_selected_asked_first(page):
    page.servers.selectRow(row_of(page.servers, "radare2"))
    page.update_button.click()
    assert page.asked and page.asked[-1][0] == "mcp-update" and "radare2" in page.asked[-1][1]
    assert ("mcp", "update", "radare2", "--yes", "--json") in page.sai.ran
    assert "Updated: radare2." in page.catalog_status.toPlainText()


def test_update_all_names_no_server_and_both_are_on_the_menu(page):
    page.update_all_button.click()
    assert ("mcp", "update", "--yes", "--json") in page.sai.ran
    page.servers.selectRow(row_of(page.servers, "radare2"))
    texts = [a.text() for a in kits_page.context_menu(page.servers, page.server_buttons).actions()]
    assert "Update" in texts and "Update all" in texts


def test_an_unsigned_catalog_is_named_and_passed_unsigned(page, homes):
    cache = homes / "cache" / "sandbox-ai" / "mcp-catalog-env.json"
    known = json.loads(cache.read_text())
    known["CAT"]["mcp:radare2"]["signature"] = "unsigned"
    cache.write_text(json.dumps(known))
    page.refresh()
    page.servers.selectRow(row_of(page.servers, "radare2"))
    page.update_button.click()
    assert "is not signed: nobody vouched for its new image" in page.asked[-1][1]
    assert ("mcp", "update", "radare2", "--yes", "--json", "--unsigned") in page.sai.ran


def test_the_update_question_is_one_the_app_asks(qapp):
    assert backend.needs_confirm("mcp-update") and "signature checked again" in backend.needs_confirm("mcp-update")


def test_the_german_texts_are_there(qapp):
    from sandbox_ai_app import i18n
    i18n.set_language("de")
    try:
        assert i18n.tr("a newer image") == "ein neueres Image"
        assert i18n.tr("catalog|signed") == "signierter"
        assert i18n.tr("Updated: {names}.", names="radare2") == "Aktualisiert: radare2."
        assert i18n.tr("Update") == "Aktualisieren" and i18n.tr("Update all") == "Alle aktualisieren"
    finally:
        i18n.set_language("en")
