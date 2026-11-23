"""The traffic light per local model in the app (offscreen, 0.1.98). User: "farbliche Ampel zu den Modellen, die auf
einem lokalen LLM-Server sind: wie weit wir sie supporten und erfolgreich in welchem Harness hatten. Detail-Infos, was
funktioniert hat und was nicht, pro Modell, vorausgesetzt wir haben solche Infos."

Every list of a model server's models - the LLM servers page's table, the Board's and the Start page's Model choice
and its picker, the setup wizard's model list, the Test connection list - shows each model's light: the theme's status
colour with a shape of its own (readable without the colour) and words (tooltip, accessible text); a detail panel
says per agent what worked and what failed, the recommended settings, the speed, the round and its date, and "no data
yet" for a model nothing is known about. Read from this machine's sandbox-ai (model_profiles.support) at run time."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from sandbox_ai_app import backend, i18n, model_profile as MP, widgets, wizard_llm as WL  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
QCN = "qwen/qwen3-coder-next"
Q38 = "mistralai/devstral-small-2-2512"   # red where it was run (Claude Code, OpenCode, Codex), no data elsewhere
OTHER = "meta/llama3-8b"
MODELS = [{"name": QCN, "size": 45_000_000_000, "loaded": True, "details": "", "type": "llm"},
          {"name": Q38, "size": 16_000_000_000, "loaded": False, "details": "", "type": "llm"},
          {"name": OTHER, "size": 4_000_000_000, "loaded": False, "details": "", "type": "llm"}]
SERVER = {"name": "studio", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "api_key_secret": None,
          "headers": {}, "ca": None, "vpn": None, "model": QCN, "anthropic": False, "insecure_http": False,
          "models": [m["name"] for m in MODELS], "checked": None, "agents": ["claude", "codex", "opencode"]}


@pytest.fixture
def sai(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    answers = {("llm", "list", "--json"): (0, json.dumps([SERVER]), ""),
               ("llm", "models", "studio", "--sort", "loaded", "--json"): (0, json.dumps(MODELS), "")}
    monkeypatch.setattr(sai, "hosts_supported", lambda: False)
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: answers.get(args, (0, "", "")))
    return sai


@pytest.fixture
def german():
    i18n.set_language("de")
    yield
    i18n.set_language("en")


def table_of(sai):
    module = sai.module("model_profiles")
    return module, module.load()


def family(sai, family_id):
    return next(f for f in table_of(sai)[1]["families"] if f["id"] == family_id)


# --- the words (no Qt) ----------------------------------------------------------------------------------------------

def test_the_light_in_words_from_the_table(sai):
    found = MP.support_of(sai, QCN)
    entries = family(sai, "qwen3-coder-next")["harnesses"]
    green = [a for a, e in entries.items() if e["status"] == "verified"]
    assert found["light"] == "green"
    assert MP.light_label(found).startswith("✓ verified: " + ", ".join(MP.agent_name(a) for a in green[:3]))
    assert MP.light_summary(found).startswith("verified in " + ", ".join(MP.agent_name(a) for a in green))
    assert "; partly working in " in MP.light_summary(found)
    assert MP.light_accessible(found, QCN).startswith(f"Support of {QCN}: verified in ")
    red = MP.support_of(sai, Q38)
    assert MP.light_label(red) == "✗ does not work: Claude Code, OpenCode, Codex"
    assert MP.light_summary(red) == "does not work in Claude Code, OpenCode, Codex"   # never "every agent"
    grey = MP.support_of(sai, OTHER)
    assert MP.light_label(grey) == "? no data yet" and MP.light_summary(grey) == "no data yet"
    assert MP.support_of(sai, "") is None and MP.support_of(None, QCN) is None


def test_the_details_per_agent(sai):
    found = MP.support_of(sai, QCN)
    details = MP.support_details(found)
    entry = family(sai, "qwen3-coder-next")["harnesses"]["claude"]
    pair = next(pair for pair in found["harnesses"] if pair["agent"] == "claude")
    claude = next(pair for pair in details["harnesses"] if pair["title"].startswith("Claude Code:"))
    # where the status is shown: the derived evidence's runs, use cases and rounds
    runs, cases = entry["evidence"]["runs"], len(entry["evidence"]["use_cases"])
    assert claude["title"] == f"Claude Code: verified ({runs} runs, {cases} use cases, phase 2, rounds 1, 5)"
    assert claude["symbol"] == "✓" and claude["light"] == "green"
    assert claude["works"] == entry["works"] and claude["fails"] == entry.get("fails", [])
    rounds = table_of(sai)[1]["rounds"]
    assert f"Evidence of phase 2, round 1, round 5, {rounds['round 5']}" in claude["lines"]
    assert f"Measured: {pair['evidence']}" in claude["lines"] and "By hand: " in pair["evidence"]
    assert any(line.startswith("Recommended settings: temperature 0.7") for line in claude["lines"])
    assert any(line.startswith("Measured speed: ") and "tokens/s" in line for line in claude["lines"])
    codex = next(pair for pair in details["harnesses"] if pair["title"].startswith("Codex:"))   # round 8: OpenCode verified
    assert codex["title"].startswith("Codex: partly working (") and codex["light"] == "yellow"
    assert details["untested"].startswith("No data yet in: ")
    text = MP.support_text(details)
    assert text.startswith(f"Support of {QCN}\nverified in ") and f"✓ {claude['title']}" in text
    one = MP.measured_text({"runs": 1, "use_cases": 1, "rounds": "round 3"})
    assert one == "1 run, 1 use case, round 3" and MP.measured_text(None) == ""
    none = MP.support_details(MP.support_of(sai, OTHER))
    assert none["lines"] == ["No data yet: no model family of sandbox-ai's research matches this name."]
    assert none["harnesses"] == [] and none["light"] == "grey"
    bad = MP.support_details(MP.support_of(sai, Q38))
    assert not any(line.startswith("Known not to work") for line in bad["lines"]), "no verdict for every agent"
    assert [pair["title"].split(":")[0] for pair in bad["harnesses"]] == ["Claude Code", "OpenCode", "Codex"]
    assert bad["untested"].startswith("No data yet in: Codex") is False and "Qwen Code" in bad["untested"]


def test_in_german(sai, german):
    found = MP.support_of(sai, QCN)
    assert MP.light_label(found).startswith("✓ verifiziert: ")
    assert MP.light_summary(MP.support_of(sai, OTHER)) == "noch keine Daten"
    details = MP.support_details(found)
    assert details["title"] == f"Unterstützung von {QCN}"
    assert any(pair["title"].startswith("Claude Code: verifiziert (") and " Läufe, " in pair["title"] and
               " Anwendungsfälle, " in pair["title"] for pair in details["harnesses"])
    assert MP.support_details(MP.support_of(sai, OTHER))["lines"][0].startswith("Noch keine Daten")


def test_every_text_is_in_both_languages():
    import ast
    import inspect
    source = inspect.getsource(MP)
    keys = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("tr", "N_") and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            keys.add(node.args[0].value)
    from sandbox_ai_app import model_profile_view
    for node in ast.walk(ast.parse(inspect.getsource(model_profile_view))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr" and node.args and \
                isinstance(node.args[0], ast.Constant):
            keys.add(node.args[0].value)
    missing = sorted(key for key in keys if key not in i18n.TEXTS or not i18n.TEXTS[key].get("de"))
    assert missing == []


# --- the icons ------------------------------------------------------------------------------------------------------

def test_each_colour_has_an_icon_with_a_shape_of_its_own(qapp):
    from sandbox_ai_app.model_profile_view import light_icon
    images = {colour: light_icon(colour).pixmap(14, 14).toImage() for colour in ("green", "yellow", "red", "grey")}
    assert all(not image.isNull() for image in images.values())
    gray = {colour: [QtWidgets.QApplication.instance() and (image.pixelColor(x, y).lightness() > 200 and
                                                            image.pixelColor(x, y).alpha() > 200)
                     for x in range(14) for y in range(14)] for colour, image in images.items()}
    assert len({tuple(mask) for mask in gray.values()}) == 4, "the white shapes differ: readable without colour"
    assert light_icon("nonsense").cacheKey() == light_icon("grey").cacheKey()


# --- the LLM servers page -------------------------------------------------------------------------------------------

def test_the_llm_servers_page_shows_the_light_and_the_details(sai):
    from sandbox_ai_app import llm_page
    monkey = pytest.MonkeyPatch()
    monkey.setattr(llm_page, "background", lambda function, *a, done, **kw: done(function(*a), None))
    try:
        page = llm_page.LlmPage(sai)
        page.show()
        page.servers.selectRow(0)
        page.models_button.click()
        widget = page.model_table
        headers = [widget.horizontalHeaderItem(c).text() for c in range(widget.columnCount())]
        assert headers[-1] == "Support"
        column = len(headers) - 1
        rows = {widget.row_keys[widgets.row_index(widget, r)]: r for r in range(widget.rowCount())}
        cell = widget.item(rows[QCN], column)
        assert cell.text().startswith("✓ verified: ") and not cell.icon().isNull()
        assert "verified in " in cell.toolTip() and "Claude Code" in cell.toolTip().split(";")[0]
        assert cell.data(Qt.ItemDataRole.AccessibleTextRole).startswith(f"Support of {QCN}: verified in ")
        assert widget.item(rows[Q38], column).text() == "✗ does not work: Claude Code, OpenCode, Codex"
        assert widget.item(rows[OTHER], column).text() == "? no data yet"
        widget.clearSelection()
        assert page.support_panel.isHidden() and page.support_area.isHidden()
        widget.selectRow(rows[QCN])
        texts = [label.text() for label in page.support_panel.findChildren(QtWidgets.QLabel)]
        assert page.support_panel.isVisible() and any(t.startswith("✓ Claude Code: verified (") for t in texts)
        assert any("Recommended settings: temperature 0.7" in t for t in texts)
        widget.selectRow(rows[OTHER])
        texts = [label.text() for label in page.support_panel.findChildren(QtWidgets.QLabel)]
        assert any(t.startswith("No data yet") for t in texts)
        page.support_panel.copy()
        assert QtWidgets.QApplication.clipboard().text().startswith(f"Support of {OTHER}")
        widget.clearSelection()
        assert page.support_panel.isHidden()
        page.close()
        page.deleteLater()
    finally:
        monkey.undo()


# --- the Model choices and the picker -------------------------------------------------------------------------------

def endpoints():
    return [{**SERVER, "answered": True, "model_info": {m["name"]: m for m in MODELS}}]


def test_the_model_choice_of_the_board_and_the_start_page_is_marked(sai):
    from sandbox_ai_app.wizard import fill_model_combo
    combo, only = QtWidgets.QComboBox(), QtWidgets.QCheckBox()
    fill_model_combo(combo, only, endpoints(), "opencode", sai, f"studio:{QCN}")
    index = combo.findData(f"studio:{QCN}")
    assert not combo.itemIcon(index).isNull()
    tip = combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
    assert "verified in " in tip and "Claude Code" in tip.split("verified in ", 1)[1].split(";")[0]
    assert f"Support of {QCN}: verified in " in combo.itemData(index, Qt.ItemDataRole.AccessibleTextRole)
    grey = combo.findData(f"studio:{OTHER}")
    assert combo.itemData(grey, Qt.ItemDataRole.ToolTipRole).endswith("In OpenCode: no data yet</p>")
    assert combo.itemIcon(combo.findData("")).isNull(), "the agent's own provider: no light"


def test_the_picker_has_the_support_column_and_the_panel(sai):
    from sandbox_ai_app.model_picker import ModelPicker
    rows = WL.picker_rows(endpoints(), "opencode", sai)
    assert next(r for r in rows if r["model"] == QCN)["support"]["light"] == "green"
    picker = ModelPicker(None, rows, f"studio:{QCN}")
    headers = [picker.table.horizontalHeaderItem(c).text() for c in range(picker.table.columnCount())]
    assert headers[-1] == "Support"
    shown = {picker.shown_rows[r]["model"]: r for r in range(picker.table.rowCount())}
    assert picker.table.item(shown[Q38], len(headers) - 1).text() == "✗ does not work: Claude Code, OpenCode, Codex"
    assert not picker.table.item(shown[QCN], 0).icon().isNull()
    assert picker.support_panel.isVisible() or not picker.isVisible()
    assert picker.support_panel.details["title"] == f"Support of {QCN}"
    picker.table.selectRow(shown[OTHER])
    assert picker.support_panel.details["lines"][0].startswith("No data yet")
    picker.deleteLater()
    plain = ModelPicker(None, WL.picker_rows(endpoints(), "opencode"), "")
    assert [plain.table.horizontalHeaderItem(c).text() for c in range(plain.table.columnCount())][-1] != "Support"
    plain.deleteLater()


def test_the_wizards_model_list_and_test_connection_are_marked(sai):
    from sandbox_ai_app import llm_page
    dialog = llm_page.LlmDialog(None, "local", run=lambda *args, **kw: (0, json.dumps(MODELS), ""), sai=sai)
    dialog.fill_model_box(MODELS)
    index = dialog.model_box.findData(QCN)
    assert index >= 0 and not dialog.model_box.itemIcon(index).isNull()
    assert "verified in" in dialog.model_box.itemData(index, Qt.ItemDataRole.ToolTipRole)
    dialog.deleteLater()


def test_the_setup_wizards_model_list_is_marked(sai, monkeypatch):
    from sandbox_ai_app.wizard import SandboxWizard
    w = SandboxWizard(sai, lambda: {"default_agent": "opencode", "default_template": "development"})
    page = w.model_page
    page.fill_models(SERVER, MODELS)
    combo = page.server_model
    index = combo.findData(QCN)
    assert index >= 0 and not combo.itemIcon(index).isNull()
    assert "verified in" in combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
    assert combo.itemIcon(combo.findData(OTHER)).isNull() is False
    w.deleteLater()
