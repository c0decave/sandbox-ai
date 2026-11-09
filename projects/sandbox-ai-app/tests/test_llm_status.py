"""What an LLM server has loaded and uses now (sandbox-ai llm status, wizard models --status) in the app: the LLM
page's line per server, read in the background after the list and every STATUS_REFRESH seconds while the page is
shown (never on the GUI thread, one read at a time), the dialog's Test connection, and the Sandboxes page's line for
a start with --llm NAME. Also: every model a server lists is offered (70, 120 or more), in choosers whose popup
scrolls and whose typing filters. Stand-ins record what they were asked; nothing reaches a server or a host."""
import json
import os
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app import llm_page  # noqa: E402
from sandbox_ai_app import wizard_llm as WL  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

SERVER = {"name": "box", "kind": "ollama", "url": "http://192.168.1.20:11434", "api_key_secret": None, "headers": {},
          "ca": None, "vpn": None, "model": None, "anthropic": False, "insecure_http": False, "models": [],
          "checked": None, "agents": ["claude", "codex", "opencode"]}
VLLM = dict(SERVER, name="gpu", kind="vllm", url="http://100.64.0.7:8000")
STATUS = [{"name": "box", "kind": "ollama", "url": SERVER["url"], "reachable": True, "error": None,
           "read_at": "2026-11-09T12:00:00Z", "models": 12,
           "loaded": [{"name": "qwen2.5-coder:7b", "size": 4_683_087_332, "memory_bytes": 6_000_000_000,
                       "vram_bytes": 5_000_000_000, "ram_bytes": 1_000_000_000, "context": 8192,
                       "expires_at": "2026-11-09T12:05:00Z"}],
           "usage": {"vram_bytes": 5_000_000_000, "ram_bytes": 1_000_000_000}, "reported_by": ["Ollama /api/ps"],
           "not_reported": ["kv_cache", "requests", "totals"]},
          {"name": "gpu", "kind": "vllm", "url": VLLM["url"], "reachable": False, "error": "timed out",
           "read_at": "2026-11-09T12:00:01Z", "models": None, "loaded": None, "usage": {}, "reported_by": [],
           "not_reported": ["loaded", "memory", "kv_cache", "requests", "totals"]}]


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    """host_run records (host, args) and answers from sai.answers; sai.slow: seconds `llm status` takes."""
    sai = backend.SandboxAI()
    sai.asked, sai.slow, sai.threads = [], 0, set()
    sai.answers = {("local", ("llm", "list", "--json")): (0, json.dumps([SERVER, VLLM]), ""),
                   ("local", ("llm", "status", "--json")): (1, json.dumps(STATUS), "")}
    monkeypatch.setattr(sai, "hosts_supported", lambda: False)

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, args))
        sai.threads.add(threading.current_thread() is threading.main_thread())
        if args[:2] == ("llm", "status") and sai.slow:
            time.sleep(sai.slow)
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    return sai


def synchronous(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as ThreadCall hands it on
        done(None, exc)
        return
    done(result, None)


def wait(qapp, condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    return condition()


def status_reads(sai):
    return [entry for entry in sai.asked if entry[1][:2] == ("llm", "status")]


def test_the_page_shows_what_each_server_has_loaded_and_uses(qapp, sai, monkeypatch):
    monkeypatch.setattr(llm_page, "background", synchronous)
    page = llm_page.LlmPage(sai)
    assert status_reads(sai) == [], "a page nobody opened reads nothing"
    page.show()
    assert sai.asked == [("local", ("llm", "list", "--json")), ("local", ("llm", "status", "--json"))]
    lines = page.server_status.text().splitlines()
    assert lines == ["box: loaded: qwen2.5-coder:7b (6.0 GB in memory: VRAM 5.0 GB, RAM 1.0 GB; context 8192) · "
                     "VRAM 5.0 GB · RAM 1.0 GB · not reported: KV cache, requests",
                     "gpu: not reachable: timed out"], "llm status says 1 when a server is down: the JSON counts"
    tip = page.server_status.toolTip()
    assert "unloaded at 2026-11-09T12:05:00Z" in tip and "read at 12:00:00 (UTC)" in tip
    assert "Not reported by its API: KV cache, requests, the host's total RAM/VRAM" in tip
    assert 15 <= page.status_timer.interval() / 1000 <= 30 and page.status_timer.isActive()
    page.status_timer.timeout.emit()
    assert len(status_reads(sai)) == 2, "read again while shown"
    page.hide()
    assert not page.status_timer.isActive(), "a hidden page reads nothing"
    page.deleteLater()


def test_an_older_host_is_told_to_update_and_not_asked_every_time(qapp, sai, monkeypatch):
    monkeypatch.setattr(llm_page, "background", synchronous)
    sai.answers[("local", ("llm", "status", "--json"))] = (
        2, "", "usage: sandbox-ai llm ...\nsandbox-ai llm: error: argument command: invalid choice: 'status'\n")
    page = llm_page.LlmPage(sai)
    page.show()
    assert "does not know llm status yet: update the host" in page.server_status.text()
    page.status_timer.timeout.emit()
    assert len(status_reads(sai)) == 1, "not asked again every few seconds"
    page.refresh_button.click()
    assert len(status_reads(sai)) == 2, "Refresh asks again (updated meanwhile?)"
    page.deleteLater()


def test_the_periodic_read_never_blocks_the_window(qapp, sai):
    """With the real background helper and a status read that takes 1.5 s: the timer's read returns at once, the
    command runs off the GUI thread, a second tick meanwhile starts no second read, and the line fills once it is
    done."""
    page = llm_page.LlmPage(sai)
    page.show()
    assert wait(qapp, lambda: page.server_status.text().startswith("box:")), page.server_status.text()
    sai.slow = 1.5
    before = len(status_reads(sai))
    began = time.monotonic()
    page.status_timer.timeout.emit()
    page.status_timer.timeout.emit()
    assert time.monotonic() - began < 0.5, "the GUI thread does not wait for the server"
    assert wait(qapp, lambda: len(status_reads(sai)) == before + 1)
    assert sai.threads == {False}, "every command off the GUI thread"
    sai.answers[("local", ("llm", "status", "--json"))] = (0, json.dumps(STATUS[:1]), "")
    time.sleep(1.6)
    assert wait(qapp, lambda: page.status_reading is None)
    assert len(status_reads(sai)) == before + 1, "one read at a time"
    page.hide()
    page.deleteLater()


def test_test_connection_of_a_saved_server_shows_its_status(qapp, sai, monkeypatch):
    """An edited server reached as it was saved: `llm models NAME`, then `llm status NAME` for what it has loaded."""
    monkeypatch.setattr(llm_page, "background", synchronous)
    sai.answers[("local", ("llm", "models", "box", "--sort", "loaded", "--json"))] = (0, json.dumps([
        {"name": "qwen2.5-coder:7b", "size": 4_683_087_332, "loaded": True, "details": ""},
        {"name": "llama3.2:3b", "size": 2_019_393_189, "loaded": False, "details": ""}]), "")
    sai.answers[("local", ("llm", "status", "box", "--json"))] = (0, json.dumps(STATUS[:1]), "")
    page = llm_page.LlmPage(sai)
    existing = WL.fields_of(SERVER)
    dialog = llm_page.LlmDialog(page, "local", existing=existing, run=page.runner("local"))
    dialog.test()
    assert sai.asked[-2:] == [("local", ("llm", "models", "box", "--sort", "loaded", "--json")),
                              ("local", ("llm", "status", "box", "--json"))]
    text = dialog.test_result.text()
    assert text.startswith("Reachable: 2 models, 1 loaded.") and "\nNow: loaded: qwen2.5-coder:7b (6.0 GB in memory" in text
    dialog.deleteLater()
    page.deleteLater()


def test_a_start_with_llm_shows_its_servers_status_while_selected(qapp, sai, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(llm_page, "background", synchronous)
    monkeypatch.setattr(ui, "background", synchronous)
    sai.answers[("ct1", ("llm", "status", "box", "--json"))] = (0, json.dumps(STATUS[:1]), "")
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    page.show()
    running = backend.Instance("session", "remote:ct1:api", "api", host="ct1", state="running", agent="codex",
                               record={"name": "api", "llm": "box:qwen2.5-coder:7b"})
    plain = backend.Instance("session", "remote:ct1:web", "web", host="ct1", state="running", agent="codex",
                             record={"name": "web"})
    monkeypatch.setattr(page, "current", lambda: running)
    page.selection_changed()
    assert ("ct1", ("llm", "status", "box", "--json")) in sai.asked, "on the start's host"
    assert page.llm_status.isVisible() and page.llm_status.text().startswith(
        "LLM server box:qwen2.5-coder:7b: loaded: qwen2.5-coder:7b (6.0 GB in memory: VRAM 5.0 GB, RAM 1.0 GB")
    assert page.llm_status.timer.isActive() and 15 <= page.llm_status.timer.interval() / 1000 <= 30
    page.llm_status.timer.timeout.emit()
    assert len(status_reads(sai)) == 2, "read again while it is shown"
    monkeypatch.setattr(page, "current", lambda: plain)
    page.selection_changed()
    assert not page.llm_status.isVisible() and not page.llm_status.timer.isActive(), "a start without --llm: none"
    page.timer.stop()
    page.remote_timer.stop()
    page.deleteLater()


# -- every model a server lists, in choosers that scroll and filter --

MANY = [f"model-{i:03d}" for i in range(120)]


def test_every_model_is_offered_and_the_chooser_scrolls_and_filters(qapp, sai, monkeypatch):
    from sandbox_ai_app.wizard import fill_model_combo
    endpoint = dict(SERVER, models=MANY)
    assert len(WL.placeholder_choices(endpoint)) == 120, "no cut at 30"
    combo, only = QtWidgets.QComboBox(), QtWidgets.QCheckBox()
    fill_model_combo(combo, only, [endpoint], "codex", sai, "")
    values = [combo.itemData(i) for i in range(combo.count())]
    assert values == ["", *[f"box:{m}" for m in MANY]], "the agent's provider, then all 120"
    assert combo.maxVisibleItems() == 20 and combo.view().verticalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAsNeeded and "combobox-popup: 0" in combo.styleSheet()
    combo.show()
    combo.showPopup()
    assert combo.view().verticalScrollBar().maximum() > 0, "the popup scrolls"
    combo.hidePopup()
    completer = combo.completer()
    assert completer is not None and completer.filterMode() == Qt.MatchFlag.MatchContains
    completer.setCompletionPrefix("MODEL-11")
    assert completer.completionCount() == 10, "typing filters (any part, any case)"
    chosen = []
    combo.activated.connect(lambda index: chosen.append(combo.itemData(index)))
    combo.setEditText("box:model-117")
    combo.lineEdit().editingFinished.emit()
    assert combo.currentData() == "box:model-117" and chosen == ["box:model-117"], "a typed entry is chosen"
    combo.setEditText("no such model")
    combo.lineEdit().editingFinished.emit()
    assert combo.currentText() == "box:model-117", "typed text that names none: the choice stays"
    fill_model_combo(combo, only, [endpoint], "codex", sai, "box:model-005")
    assert combo.currentData() == "box:model-005" and combo.count() == 121
    combo.deleteLater()


def test_a_hosts_servers_bring_all_their_models(qapp, sai):
    listed = [{"name": m, "size": 1_000_000_000 + i, "loaded": i == 3, "details": ""} for i, m in enumerate(MANY)]
    sai.answers[("ct1", ("llm", "list", "--json"))] = (0, json.dumps([SERVER]), "")
    sai.answers[("ct1", ("llm", "models", "box", "--json"))] = (0, json.dumps(listed), "")
    servers = sai.host_llm_servers("ct1")
    assert servers[0]["models"] == MANY


def test_the_dialogs_default_model_and_the_wizards_model_choose_from_all(qapp, sai, monkeypatch):
    monkeypatch.setattr(llm_page, "background", synchronous)
    page = llm_page.LlmPage(sai)
    dialog = llm_page.LlmDialog(page, "local", run=page.runner("local"))
    probe = tuple(WL.probe_argv("ollama", "http://192.168.1.20:11434", status=True))
    sai.answers[("local", probe)] = (0, json.dumps({"models": [{"name": m, "size": 5, "loaded": False}
                                                               for m in MANY], "status": None}), "")
    dialog.kind.setCurrentIndex(dialog.kind.findData("ollama"))
    dialog.url.setText("http://192.168.1.20:11434")
    dialog.test()
    box = dialog.model_box
    assert box.count() == 120 and box.maxVisibleItems() == 20 and box.completer().filterMode() == \
        Qt.MatchFlag.MatchContains
    dialog.deleteLater()
    page.deleteLater()
