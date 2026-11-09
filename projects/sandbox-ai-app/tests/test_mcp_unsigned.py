"""Adding an MCP server from an unsigned catalog (offscreen): `sandbox-ai mcp add` refuses it unless --unsigned; the
MCP page shows that refusal wrapped, says nothing was added, and offers - asked first (mcp-unsigned) - to add it
anyway; other refusals are never offered. The list above shows the catalog each server came from. Stand-ins record
what was asked; one test runs the real `sandbox-ai mcp add` against a fake catalog file (no network, no pull)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

URL = "https://raw.githubusercontent.com/c0decave/axross-mcp-hub/main/catalog.json"
REFUSAL = (f"sandbox-ai mcp: crtsh: its catalog is not signed by a trusted key ({URL}.sig cannot be read), so nobody "
           "vouched that it is built from its pinned sources and works with sandbox-ai; --unsigned adds it anyway\n")
ENTRY = {"name": "crtsh", "network": "none", "hosts": [], "title": "crt.sh lookups", "digest": "published",
         "_signature": {"state": "unsigned"}, "_catalog_name": "axross-mcp-hub (github.com/c0decave)"}


# -- the refusal, as sandbox-ai words it (no Qt needed) --


@needs_sai
def test_the_refusal_for_an_unsigned_catalog_is_recognized_and_no_other():
    mcp = backend.SandboxAI().module("mcp")
    with pytest.raises(ValueError) as unsigned:
        mcp.check_signature({"name": "crtsh", "_signature": {"state": "unsigned", "reason": f"{URL}.sig cannot be read"}})
    assert backend.mcp_unsigned_refusal(f"sandbox-ai mcp: {unsigned.value}") == {"catalog": URL}
    with pytest.raises(ValueError) as keyless:
        mcp.check_signature({"name": "crtsh", "_signature": {"state": "unsigned", "reason": "no key is trusted yet"}})
    assert backend.mcp_unsigned_refusal(str(keyless.value)) == {"catalog": ""}
    with pytest.raises(ValueError) as bad:
        mcp.check_signature({"name": "crtsh", "_signature": {"state": "bad", "reason": "wrong key"}})
    assert backend.mcp_unsigned_refusal(str(bad.value)) is None, "a signature that does not verify: never offered"
    for other in ("sandbox-ai mcp: no catalog entry crtsh; see sandbox-ai mcp catalog", "", None,
                  "crtsh: the catalog has no digest for it yet (not published)"):
        assert backend.mcp_unsigned_refusal(other) is None


def test_the_web_and_the_app_recognize_the_same_words():
    from pathlib import Path
    webui = (Path(__file__).resolve().parents[3] / "sandbox" / "webui.py").read_text()
    assert f'MCP_UNSIGNED_HINT = "{backend.MCP_UNSIGNED_HINT}"' in webui


# -- the MCP page with stand-ins --


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
    sai.ran, sai.answers, sai.servers = [], [], [("demo", "stopped", "", "python3 demo", "built-in")]

    def host_run(host, *args, timeout=120, input=None):
        sai.ran.append(args)
        return sai.answers.pop(0) if sai.answers else (0, "", "")
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_mcp_servers", lambda host: list(sai.servers))
    monkeypatch.setattr(sai, "host_mcp_catalog", lambda host, catalogs=(): ([("hub", dict(ENTRY))], []))
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        asked.append((action, text, details))
        return page_answer[0]
    page_answer = [True]
    monkeypatch.setattr(ui, "ask_first", ask_first)
    monkeypatch.setattr(ui.AddServerDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    shown = ui.McpPage(sai)
    shown.sai_, shown.asked, shown.answer, shown.ui = sai, asked, page_answer, ui
    shown.load_catalog()
    yield shown
    shown.close()


def catalog_rows(page):
    return [[page.catalog.item(r, c).text() for c in range(page.catalog.columnCount())]
            for r in range(page.catalog.rowCount())]


def add_crtsh(page):
    page.catalog.selectRow(0)
    page.add()


def test_the_catalog_says_unsigned_and_which_catalog_lists_it(page):
    rows = catalog_rows(page)
    assert rows == [["hub:crtsh", "none", "crt.sh lookups", "yes", "unsigned", "axross-mcp-hub (github.com/c0decave)"]]


def test_refused_for_its_unsigned_catalog_it_is_offered_and_added_with_unsigned(page):
    sai = page.sai_
    sai.answers = [(1, "", REFUSAL), (0, "sandbox-mcp: crtsh added (…); use it with --mcp crtsh\n", "")]
    sai.servers.append(("crtsh", "stopped", "", "ghcr.io/x@sha256:…", "axross-mcp-hub (github.com/c0decave), unsigned"))
    add_crtsh(page)
    assert [args[:2] for args in sai.ran] == [("mcp", "add"), ("mcp", "add")]
    assert "--unsigned" not in sai.ran[0] and "--unsigned" in sai.ran[1] and sai.ran[1][-1] == "hub:crtsh"
    (action, question, details), = page.asked
    assert action == "mcp-unsigned" and "crtsh was not added" in question and URL in question
    assert "nobody vouched" in question and "sandbox of its own" in question and "--unsigned" in question
    assert details.strip() == REFUSAL.strip()
    assert "crtsh added" in page.catalog_status.text()
    names = {page.servers.item(r, 0).text(): page.servers.item(r, 4).text() for r in range(page.servers.rowCount())}
    assert names["crtsh"] == "axross-mcp-hub (github.com/c0decave), unsigned", "the list above shows it, read again"


def test_declined_it_is_said_on_the_page_that_nothing_was_added(page):
    page.answer[0] = False
    page.sai_.answers = [(1, "", REFUSAL)]
    add_crtsh(page)
    assert len(page.sai_.ran) == 1 and page.asked[0][0] == "mcp-unsigned"
    assert page.catalog_status.text().startswith("hub:crtsh was not added: ") and "in the list above" not in page.catalog_status.text()
    assert not page.catalog_status.isHidden()


@pytest.mark.parametrize("said", [
    "sandbox-ai mcp: crtsh: the catalog's signature does not verify (wrong key); nothing of it is used\n",
    "sandbox-ai mcp: crtsh: the catalog has no digest for it yet (not published)\n"])
def test_another_refusal_is_shown_and_never_offered(page, said):
    page.sai_.answers = [(1, "", said)]
    add_crtsh(page)
    assert len(page.sai_.ran) == 1 and page.asked == []
    title, text, _details = widgets.SHOWN[-1]
    assert "not added" in title and text == said.strip()
    assert page.catalog_status.text() == "hub:crtsh was not added.\n" + said.strip()


def test_already_asked_for_unsigned_a_refusal_is_not_offered_again(page, monkeypatch):
    monkeypatch.setattr(page.ui.AddServerDialog, "exec", lambda self: (self.unsigned.setChecked(True),
                                                                       QtWidgets.QDialog.DialogCode.Accepted)[1])
    page.sai_.answers = [(1, "", REFUSAL)]
    add_crtsh(page)
    assert len(page.sai_.ran) == 1 and "--unsigned" in page.sai_.ran[0]
    assert [action for action, *_ in page.asked] == ["mcp-unsigned"], "only the question before the add"


def test_the_servers_are_read_again_when_the_page_is_shown_again(page):
    page.show()
    page.hide()
    page.sai_.servers.append(("fetch", "stopped", "", "uvx", "own (mcp.json)"))  # added with sandbox-ai mcp add
    page.show()
    assert "fetch" in [page.servers.item(r, 0).text() for r in range(page.servers.rowCount())]


# -- the long refusal: wrapped, selectable, plain text, the window not widened --


def test_a_long_refusal_wraps_in_its_dialog_and_in_the_page(qapp):
    box = widgets._box(None, widgets.QMessageBox.Icon.Warning, "mcp add", REFUSAL.strip())
    assert isinstance(box, widgets.MessageDialog)
    box.show()
    qapp.processEvents()
    text = box.text
    assert text.toPlainText() == REFUSAL.strip() and text.isReadOnly()
    assert text.textInteractionFlags() & widgets.Qt.TextInteractionFlag.TextSelectableByMouse
    assert text.wordWrapMode() == widgets.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
    assert text.lineWrapMode() == widgets.QPlainTextEdit.LineWrapMode.WidgetWidth
    assert box.width() <= widgets.MESSAGE_SIZE[0] + 200, "never as wide as the one line it is"
    assert not text.verticalScrollBar().isVisible(), "a few lines: all of them shown"
    yes = box.addButton("Add unsigned", widgets.QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton("Cancel", widgets.QMessageBox.ButtonRole.RejectRole)
    box.setEscapeButton(no)
    yes.click()
    assert box.clickedButton() is yes and box.result() == QtWidgets.QDialog.DialogCode.Accepted
    box.deleteLater()
    url_only = "x " + "https://example.org/" + "a" * 200
    assert widgets.long_message(url_only) and not widgets.long_message("short one")
    # markup is shown as it is
    marked = widgets.MessageDialog(None, widgets.QMessageBox.Icon.Warning, "t", "<b>bold</b> " * 40)
    assert marked.text.toPlainText().startswith("<b>bold</b>")
    marked.deleteLater()


def test_the_page_keeps_the_window_shrinkable_with_the_long_refusal(qapp, page):
    window = QtWidgets.QMainWindow()
    window.setCentralWidget(page)
    page.catalog_status.setText("crtsh was not added.\n" + REFUSAL.strip())
    window.show()
    for _ in range(3):
        qapp.processEvents()
        window.resize(window.minimumSizeHint())
    qapp.processEvents()
    status = page.catalog_status
    assert window.minimumSizeHint().width() < 700, "the one-line refusal does not set the window's width"
    assert status.width() <= page.width() and status.height() >= status.needed_height(), "all of it shown"
    assert status.toPlainText().endswith("--unsigned adds it anyway")
    window.takeCentralWidget()
    window.deleteLater()


# -- the real sandbox-ai mcp add against a fake catalog file: refused, offered, added, listed --


@needs_sai
def test_the_real_command_adds_it_unsigned_and_the_list_shows_its_catalog(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [
        {"id": "hub:crtsh", "name": "crtsh", "title": "crt.sh lookups", "kind": "oci",
         "image": "ghcr.io/c0decave/axross-mcp-crtsh", "digest": "sha256:" + "a" * 64, "network": "none"}]}))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "background", lambda function, *args, done, **kw: done(function(*args), None))
    asked = []
    monkeypatch.setattr(ui, "ask_first", lambda parent, action, *rest, **kw: asked.append(action) or True)
    monkeypatch.setattr(ui.AddServerDialog, "exec", lambda self: (self.no_pull.setChecked(True),
                                                                  QtWidgets.QDialog.DialogCode.Accepted)[1])
    sai = backend.SandboxAI()
    page = ui.McpPage(sai)
    page.catalog_source.setText(str(catalog))
    page.load_catalog()
    assert catalog_rows(page)[0][0] == "c0:crtsh" and catalog_rows(page)[0][4:] == ["unsigned", str(catalog)]
    add_crtsh(page)
    assert asked == ["mcp-unsigned"]
    rows = {page.servers.item(r, 0).text(): page.servers.item(r, 4).text() for r in range(page.servers.rowCount())}
    assert rows.get("crtsh") == f"{catalog}, unsigned", rows
    listed = json.loads(sai.host_run("local", "mcp", "list", "--json", timeout=60)[1])
    crtsh, = [entry for entry in listed if entry["name"] == "crtsh"]
    assert crtsh["catalog_source"] == str(catalog) and crtsh["signed"] is False
    page.close()
