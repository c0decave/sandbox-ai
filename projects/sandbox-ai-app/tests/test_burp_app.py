"""Burp Suite as an MCP server in the app: the MCP page and the board's MCP part show its mode and edition ("Burp Suite
Professional, host mode (127.0.0.1:9876)", in German too), and Burp extension… writes the extension jar for your own
Burp on the host shown (sandbox-ai mcp burp extension) and shows its path and SHA-256 to copy - a button of the list
and an entry of its right-click menu."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, board as B, i18n, widgets  # noqa: E402

DIGEST = "sha256:" + "d" * 64
HOST_MODE = {"image": "ghcr.io/c0decave/sandbox-ai-mcp-burp@" + DIGEST, "network": "none",
             "endpoint": "http://127.0.0.1:9876/mcp", "burp": {"mode": "host", "edition": "professional"}}
CONTAINER_MODE = {"image": "ghcr.io/c0decave/sandbox-ai-mcp-burp@" + DIGEST, "network": "full",
                  "mounts": [{"target": "/burp", "host": "/home/u/.local/share/sandbox-ai/mcp-folders/burp/burp",
                              "mode": "ro"}],
                  "burp": {"mode": "container", "edition": "community",
                           "jar": {"name": "burpsuite_community_v2026.10.jar", "sha256": "e" * 64, "size": 1}}}


@pytest.fixture
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_the_mode_and_the_edition_in_both_languages(english):
    assert backend.burp_text(HOST_MODE) == "Burp Suite Professional, host mode (127.0.0.1:9876)"
    assert backend.burp_text(CONTAINER_MODE) == \
        "Burp Suite Community, container mode (burpsuite_community_v2026.10.jar)"
    unknown = {**HOST_MODE, "burp": {"mode": "host", "edition": None}}
    assert backend.burp_text(unknown) == "Burp Suite (edition not known yet), host mode (127.0.0.1:9876)"
    i18n.set_language("de")
    assert backend.burp_text(HOST_MODE) == "Burp Suite Professional, Host-Modus (127.0.0.1:9876)"
    assert backend.burp_text(unknown).startswith("Burp Suite (Edition noch nicht bekannt), Host-Modus")
    assert not backend.is_burp({"image": "x"}) and not backend.is_burp({"burp": {"mode": "other"}})


def test_the_boards_mcp_part_says_how_burp_runs(english):
    rows = dict(B.mcp_summary(HOST_MODE))
    assert rows["Burp"] == "Burp Suite Professional, host mode (127.0.0.1:9876)"
    assert rows["Runs"] == "your Burp on this host, reached at http://127.0.0.1:9876/mcp"
    assert rows["Network"] == "offline"  # (the network words since 0.1.247)
    rows = dict(B.mcp_summary(CONTAINER_MODE))
    assert rows["Burp"].startswith("Burp Suite Community, container mode")
    assert rows["Runs"] == CONTAINER_MODE["image"]


def test_the_extension_jar_through_the_host(monkeypatch):
    sai = backend.SandboxAI()
    ran = []

    def host_run(host, *args, timeout=120, input=None):
        ran.append((host, *args))
        return 0, json.dumps({"name": "burp", "path": "/home/u/.local/share/sandbox-ai/burp/burp-mcp-all.jar",
                              "sha256": "f" * 64, "image": HOST_MODE["image"]}), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    found = sai.host_burp_extension("lab", "burp")
    assert ran == [("lab", "mcp", "burp", "extension", "--as", "burp", "--json")]
    assert found["sha256"] == "f" * 64 and found["path"].endswith("burp-mcp-all.jar")
    monkeypatch.setattr(sai, "host_run", lambda host, *args, **kw: (1, "", "no Burp server burp: sandbox-ai mcp add burp"))
    with pytest.raises(ValueError, match="no Burp server"):
        sai.host_burp_extension("local", "burp")
    with pytest.raises(ValueError):
        sai.host_burp_extension("local", "../x")


@pytest.fixture
def page(qapp, tmp_path, monkeypatch, english):
    from sandbox_ai_app import ui
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    monkeypatch.setattr(ui, "background", lambda function, *args, done, **kw: done(function(*args), None))
    rows = [("burp", "stopped", "", backend.burp_text(HOST_MODE), "sandbox-ai-mcp (signed)"),
            ("fetch", "stopped", "", "ghcr.io/x/web-fetch@" + DIGEST, "sandbox-ai-mcp (signed)")]
    monkeypatch.setattr(sai, "host_mcp_servers", lambda host: rows)
    monkeypatch.setattr(sai, "host_mcp_notes", lambda host: {})
    sai.extension_asked = []
    monkeypatch.setattr(sai, "host_burp_extension", lambda host, name: sai.extension_asked.append((host, name)) or {
        "path": "/home/u/.local/share/sandbox-ai/burp/burp-mcp-all.jar", "sha256": "f" * 64,
        "image": HOST_MODE["image"]})
    shown = []
    monkeypatch.setattr(widgets, "show_text_window", lambda parent, title, text: shown.append((title, text)))
    monkeypatch.setattr(ui.widgets, "show_text_window", lambda parent, title, text: shown.append((title, text)))
    found = ui.McpPage(sai)
    found.shown_windows = shown
    yield found
    found.close()
    found.deleteLater()


def select(page, name):
    for row in range(page.servers.rowCount()):
        if page.servers.item(row, 0).text() == name:
            page.servers.selectRow(row)
            return
    raise AssertionError(name)


def test_the_mcp_page_shows_burps_mode_and_offers_its_extension(page):
    texts = [page.servers.item(row, 3).text() for row in range(page.servers.rowCount())]
    assert "Burp Suite Professional, host mode (127.0.0.1:9876)" in texts
    select(page, "burp")
    assert page.burp_button.isEnabled()
    page.burp_button.click()
    assert page.sai.extension_asked == [("local", "burp")]
    (title, text), = page.shown_windows
    assert title == "Burp extension"
    assert text.startswith("/home/u/.local/share/sandbox-ai/burp/burp-mcp-all.jar") and "f" * 64 in text
    assert "Extensions, Add, Extension type Java" in text


def test_burp_extension_is_for_the_burp_server_only(page):
    select(page, "fetch")
    page.burp_extension()
    assert page.sai.extension_asked == []
    assert any("Select the Burp server first" in shown[1] for shown in widgets.SHOWN)


def test_its_right_click_menu_has_it_too_for_the_burp_server(page):
    assert not page.burp_button.isHidden(), "shown: the list has a Burp server"
    select(page, "burp")
    assert page.burp_button in page.servers.menu_entries()
    assert "Burp extension…" in [action.text() for action in widgets.menu_of(page.servers).actions()]
    select(page, "fetch")
    assert page.burp_button not in page.servers.menu_entries(), "another server's menu stays as it was"


def test_without_a_burp_server_the_button_is_not_shown(page):
    page.servers_read([("fetch", "stopped", "", "ghcr.io/x/web-fetch@" + DIGEST, "")], None)
    assert page.burp_button.isHidden()


def test_this_machines_servers_read_from_sandbox_ai(tmp_path, monkeypatch, english):
    """This machine's list comes from sandbox-ai's own mcp module: a Burp definition in mcp.json is shown by its mode
    and edition, not by its image."""
    if not os.environ.get("SANDBOX_AI_HOME"):
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "config" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "config" / "sandbox-ai" / "mcp.json").write_text(json.dumps({"burp": HOST_MODE}))
    sai = backend.SandboxAI()
    (row,) = [r for r in sai.mcp_servers() if r[0] == "burp"]
    assert row[2] == "Burp Suite Professional, host mode (127.0.0.1:9876)"
