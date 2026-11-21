"""Models on the server… (offscreen): the LLM servers page's dialog of what one server has loaded (sandbox-ai llm
loaded --json): the table (model, instance, context, memory, JIT or explicit, expires, the running sandboxes using
it), the JIT warning, Load… (a model of llm models, the context from num_ctx of its parameters, Ollama's time; checked
before it closes, a refusal kept in the form), Unload… asked first naming the sandboxes that use the model (then
--force), Keep loaded (an explicit reload of a JIT model, asked first while it is in use), a host of sandbox-ai hosts
(hosts run HOST -- llm ...) and one whose sandbox-ai has no llm loaded yet. Every argv is parsed with sandbox-ai's own
argparse; end to end against the mock model server with the real sandbox-ai; the status line marks a JIT model."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, llm_loaded as LL, llm_loaded_view as LLV, llm_page  # noqa: E402
from sandbox_ai_app import widgets, wizard_llm as WL  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

SERVER = {"name": "lm", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "api_key_secret": None,
          "headers": {}, "ca": None, "vpn": None, "model": "qwen3:8b", "anthropic": False, "insecure_http": False,
          "models": ["qwen3:8b", "llama3.1:8b"], "checked": None, "agents": ["claude", "codex", "opencode"]}
VIEW = dict(SERVER, params={"*": {"temperature": 0.7}, "qwen3:8b": {"num_ctx": 32768}}, forced={}, evict="never")
LOADED = {"name": "lm", "kind": "lmstudio", "url": SERVER["url"], "read_at": "2026-11-12T10:00:00Z",
          "instances": [
              {"model": "qwen3:8b", "instance": "qwen3:8b", "context": 4096, "size": 5_000_000_000, "kept": "jit",
               "ttl_seconds": 3600},
              {"model": "llama3.1:8b", "instance": "llama3.1:8b", "context": 8192, "kept": "explicit"},
              {"model": "../bad name", "instance": "x"},
              {"model": "gemma\x1b[2J", "instance": "g"}],
          "used_by": {"qwen3:8b": ["alpha", "beta"]}, "load": True, "unload": True,
          "kept": {}, "jit_warning": "..."}
MODELS = [{"name": "qwen3:8b", "loaded": True}, {"name": "llama3.1:8b", "loaded": True},
          {"name": "big-70b", "loaded": False}]


class Parsed(Exception):
    pass


def parsed(sai, argv):
    """ARGV (["llm", ...]) as `sandbox-ai llm` parses it (nothing of the command runs)."""
    assert argv[0] == "llm"
    module = sai.module("llm")
    seen = []
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        seen.append(original(self, args, namespace))
        raise Parsed
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Parsed):
            module.main(list(argv[1:]))
    return seen[0]


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def synchronous(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def sai(homes, monkeypatch):
    """host_run records (host, args) and answers from sai.answers; background runs at once."""
    sai = backend.SandboxAI()
    sai.asked = []
    sai.answers = {}
    for host in ("local", "ct1"):
        sai.answers.update({(host, ("llm", "list", "--json")): (0, json.dumps([SERVER]), ""),
                            (host, ("llm", "show", "lm", "--json")): (0, json.dumps(VIEW), ""),
                            (host, ("llm", "loaded", "lm", "--json")): (0, json.dumps(LOADED), ""),
                            (host, ("llm", "models", "lm", "--json")): (0, json.dumps(MODELS), "")})
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}])

    def host_run(name, *args, timeout=120):
        if args[:2] != ("llm", "status"):
            sai.asked.append((name, args))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(llm_page, "background", synchronous)
    monkeypatch.setattr(LLV, "background", synchronous)
    return sai


def commands(sai, word):
    return [(host, args) for host, args in sai.asked if args[:2] == ("llm", word)]


# -- the arguments and the answer --

def test_the_argv_are_checked_and_fit_sandbox_ai_llm(sai):
    argv = LL.load_argv("lm", "qwen3:8b", 16384, 600, force=True)
    assert argv == ["llm", "load", "lm", "qwen3:8b", "--context", "16384", "--ttl", "600", "--force"]
    found = parsed(sai, argv)
    assert (found.command, found.model, found.context, found.ttl, found.force) == ("load", "qwen3:8b", 16384, 600,
                                                                                   True)
    found = parsed(sai, ["llm", "unload", *LL.unload_args("lm", "qwen3:8b:2", force=True)])
    assert (found.command, found.model, found.force) == ("unload", "qwen3:8b:2", True)
    assert parsed(sai, LL.loaded_argv("lm")).json
    for bad in (lambda: LL.load_argv("-x", "m"), lambda: LL.load_argv("lm", "--force"),
                lambda: LL.load_argv("lm", "m", context=100), lambda: LL.load_argv("lm", "m", ttl=0),
                lambda: LL.unload_args("lm", "-rf"), lambda: LL.loaded_argv("a b")):
        with pytest.raises(ValueError):
            bad()
    assert LL.check_context("") is None and LL.check_context(" 8192 ") == 8192 and LL.check_ttl("") is None
    for text in ("4k", "100", "99999999999"):
        with pytest.raises(ValueError):
            LL.check_context(text)


def test_the_answer_is_checked_and_shown(sai):
    entry = LL.parse_loaded(json.dumps(LOADED))
    assert [item["model"] for item in entry["instances"]] == ["qwen3:8b", "llama3.1:8b"], "names that are no model"
    rows = LL.rows(entry)
    cells = rows[0][0]
    assert cells[:3] == ["qwen3:8b", "qwen3:8b", "4096"] and "5.0 GB" in cells[3]
    assert cells[4] == "JIT (unloaded after 3600 s idle)" and cells[6] == "alpha, beta"
    assert "Auto-Evict" in rows[0][2] and "alpha, beta" in rows[0][2]
    assert rows[1][0][4] == "explicit" and rows[1][0][5] == "never (until unloaded)"
    assert LL.default_context(VIEW, "qwen3:8b") == 32768 and LL.default_context(VIEW, "big-70b") is None
    assert LL.default_context(dict(VIEW, params={"*": {"num_ctx": 8192}}), "big-70b") == 8192
    with pytest.raises(ValueError):
        LL.parse_loaded("[]")


def test_the_status_line_marks_a_jit_model():
    status = WL.parse_status({"name": "lm", "kind": "lmstudio", "loaded": [
        {"name": "qwen3:8b", "context": 4096, "jit": True}, {"name": "llama3.1:8b", "jit": "yes"}]})
    text = WL.status_text(status)
    assert "qwen3:8b (context 4096; JIT: LM Studio may unload it" in text and "llama3.1:8b (" not in text


# -- the dialog --

def open_dialog(page, monkeypatch, check):
    seen = []

    def run(dialog):
        seen.append(dialog)
        check(dialog)
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(LLV.LoadedDialog, "exec", run)
    page.servers.selectRow(0)
    assert page.loaded_button.isEnabled()
    page.loaded_button.click()
    assert seen
    return seen[0]


def page_of(sai):
    page = llm_page.LlmPage(sai)
    page.show()
    return page


def select(dialog, instance):
    table = dialog.table
    table.selectRow(next(r for r in range(table.rowCount()) if widgets.row_key(table, r) == instance))


def test_the_table_the_jit_warning_and_keep_loaded_asked_first_while_in_use(qapp, sai, monkeypatch):
    page = page_of(sai)

    def check(dialog):
        assert dialog.table.rowCount() == 2 and dialog.note.isVisibleTo(dialog)
        assert "LM Studio may unload qwen3:8b when another sandbox's model is loaded: Auto-Evict" in dialog.note.text()
        select(dialog, "llama3.1:8b")
        assert not dialog.keep_button.isEnabled() and "explicitly already" in dialog.keep_button.toolTip()
        select(dialog, "qwen3:8b")
        assert dialog.keep_button.isEnabled() and dialog.unload_button.isEnabled() and dialog.load_button.isEnabled()
        dialog.keep_button.click()
    open_dialog(page, monkeypatch, check)
    loads = commands(sai, "load")
    assert loads == [("local", ("llm", "load", "lm", "qwen3:8b", "--context", "4096", "--force"))]
    assert any("alpha, beta" in text for _, text, _ in widgets.SHOWN), "asked first, naming who uses it"
    parsed(sai, list(loads[0][1]))
    page.close()


def test_unload_asks_naming_the_sandboxes_and_forces_only_then(qapp, sai, monkeypatch):
    page = page_of(sai)

    def check(dialog):
        select(dialog, "qwen3:8b")
        dialog.unload_button.click()
        select(dialog, "llama3.1:8b")
        dialog.unload_button.click()
    open_dialog(page, monkeypatch, check)
    assert commands(sai, "unload") == [("local", ("llm", "unload", "lm", "qwen3:8b", "--force")),
                                       ("local", ("llm", "unload", "lm", "llama3.1:8b"))]
    asked = [text for _, text, _ in widgets.SHOWN]
    assert "alpha, beta" in asked[0] and "On this computer" in asked[0] and "alpha" not in asked[1]
    page.close()


def test_unload_answered_no_does_nothing(qapp, sai, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page = page_of(sai)

    def check(dialog):
        select(dialog, "qwen3:8b")
        dialog.unload_button.click()
        assert "Not unloaded" in dialog.status.text()
    open_dialog(page, monkeypatch, check)
    assert not commands(sai, "unload")
    page.close()


def test_load_checks_before_it_closes_and_keeps_a_refusal(qapp, sai, monkeypatch):
    page = page_of(sai)
    forms = []

    def form(dialog):
        forms.append(dialog)
        ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        assert not ok.isEnabled(), "no model chosen yet"
        assert not dialog.ttl.isVisibleTo(dialog), "LM Studio takes no time at a load"
        dialog.model.setCurrentIndex(dialog.model.findData("qwen3:8b"))
        assert dialog.context.placeholderText() == "32768", "num_ctx of its parameters"
        dialog.context.setText("12")
        assert not ok.isEnabled()
        dialog.accept()
        assert not dialog.saved and "512" in dialog.message.text()
        dialog.model.setCurrentIndex(dialog.model.findData("big-70b"))
        dialog.context.setText("16384")
        sai.answers[("local", ("llm", "load", "lm", "big-70b", "--context", "16384"))] = \
            (1, "", "sandbox-ai llm: LM Studio: not enough memory")
        dialog.accept()
        assert not dialog.saved and "not enough memory" in dialog.message.text(), "the refusal kept in the form"
        dialog.context.clear()
        dialog.accept()
        assert dialog.saved
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(LLV.LoadDialog, "exec", form)
    open_dialog(page, monkeypatch, lambda dialog: dialog.load_button.click())
    assert [args for _, args in commands(sai, "load")] == [
        ("llm", "load", "lm", "big-70b", "--context", "16384"), ("llm", "load", "lm", "big-70b")]
    page.close()


def test_ollama_offers_a_time_and_the_context_of_its_parameters(qapp, sai, monkeypatch):
    sai.answers[("local", ("llm", "list", "--json"))] = (0, json.dumps([dict(SERVER, kind="ollama")]), "")
    sai.answers[("local", ("llm", "loaded", "lm", "--json"))] = (0, json.dumps(dict(LOADED, kind="ollama",
                                                                                      instances=[], used_by={})), "")
    page = page_of(sai)
    page.refresh()

    def form(dialog):
        assert dialog.ttl.isVisibleTo(dialog)
        dialog.model.setCurrentIndex(dialog.model.findData("qwen3:8b"))
        dialog.ttl.setText("0")
        assert not dialog.validate()
        dialog.ttl.setText("600")
        dialog.accept()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(LLV.LoadDialog, "exec", form)
    open_dialog(page, monkeypatch, lambda dialog: dialog.load_button.click())
    assert [args for _, args in commands(sai, "load")] == [
        ("llm", "load", "lm", "qwen3:8b", "--context", "32768", "--ttl", "600")]
    page.close()


def test_llama_cpps_router_loads_without_a_context_even_with_num_ctx_set(qapp, sai, monkeypatch):
    sai.answers[("local", ("llm", "list", "--json"))] = (0, json.dumps([dict(SERVER, kind="llamacpp")]), "")
    sai.answers[("local", ("llm", "loaded", "lm", "--json"))] = (0, json.dumps(dict(LOADED, kind="llamacpp",
                                                                                      instances=[], used_by={})), "")
    page = page_of(sai)
    page.refresh()

    def form(dialog):
        assert not dialog.context.isEnabled() and not dialog.ttl.isVisibleTo(dialog)
        dialog.model.setCurrentIndex(dialog.model.findData("qwen3:8b"))  # num_ctx 32768 in its parameters
        dialog.accept()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(LLV.LoadDialog, "exec", form)
    open_dialog(page, monkeypatch, lambda dialog: dialog.load_button.click())
    assert [args for _, args in commands(sai, "load")] == [("llm", "load", "lm", "qwen3:8b")]
    page.close()


def test_a_host_of_sandbox_ai_hosts_and_one_without_llm_loaded(qapp, sai, monkeypatch):
    page = page_of(sai)
    page.host_box.setCurrentIndex(page.host_box.findData("ct1"))
    page.refresh()

    def check(dialog):
        assert dialog.table.rowCount() == 2
    open_dialog(page, monkeypatch, check)
    assert ("ct1", ("llm", "loaded", "lm", "--json")) in sai.asked
    assert not [host for host, args in sai.asked if args[:2] == ("llm", "loaded") and host == "local"]
    sai.answers[("ct1", ("llm", "loaded", "lm", "--json"))] = \
        (2, "", "sandbox-ai llm: error: argument command: invalid choice: 'loaded'")

    def old(dialog):
        assert "has no llm loaded yet" in dialog.status.text() and dialog.table.rowCount() == 0
        assert not dialog.unload_button.isEnabled()
    open_dialog(page, monkeypatch, old)
    page.close()


def test_the_page_never_waits_for_the_dialogs_commands(qapp, sai, monkeypatch):
    """Every command of the dialog runs through background() (never on the GUI thread)."""
    calls = []

    def recording(function, *args, done, **kw):
        calls.append(kw.get("timeout"))
        synchronous(function, *args, done=done)
    monkeypatch.setattr(LLV, "background", recording)
    page = page_of(sai)
    open_dialog(page, monkeypatch, lambda dialog: None)
    assert len(calls) == 2 and all(calls), "loaded, then models and show, each in the background with a timeout"
    page.close()


# -- end to end: the real sandbox-ai against the mock model server --

def test_end_to_end_keep_loaded_replaces_the_jit_instance(qapp, homes, monkeypatch):
    home = Path(os.environ.get("SANDBOX_AI_HOME") or "")
    mock_path = home / "tests/devtools/model_mock.py"
    if not mock_path.exists():
        pytest.skip("needs a sandbox-ai checkout with tests/devtools")
    spec = importlib.util.spec_from_file_location("model_mock_app_e2e", mock_path)
    mock = importlib.util.module_from_spec(spec)
    sys.modules["model_mock_app_e2e"] = mock
    spec.loader.exec_module(mock)
    server, port = mock.start(models=("qwen3-8b", "gemma-3-12b"), lmstudio=True,
                              loaded={"qwen3-8b": {"context": 8192, "ttl": 3600}})
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    try:
        sai = backend.SandboxAI()
        assert sai.host_run("local", "llm", "add", "lm", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}",
                            timeout=60)[0] == 0
        monkeypatch.setattr(llm_page, "background", synchronous)
        monkeypatch.setattr(LLV, "background", synchronous)
        page = page_of(sai)
        page.refresh()

        def check(dialog):
            assert dialog.note.isVisibleTo(dialog), "the JIT warning"
            select(dialog, "qwen3-8b")
            dialog.keep_button.click()
            assert "loaded qwen3-8b explicitly" in dialog.status.text() or dialog.table.rowCount() == 1
            select(dialog, "qwen3-8b")
            assert not dialog.keep_button.isEnabled(), "explicit now"
            assert not dialog.note.isVisibleTo(dialog)
        open_dialog(page, monkeypatch, check)
        instances = server.RequestHandlerClass.options.instances
        assert [(i["id"], i["ttl"], i["context"]) for i in instances] == [("qwen3-8b", None, 8192)], \
            "the JIT instance went first: one explicit instance with the context it had"
        page.close()
    finally:
        server.shutdown()


def test_load_a_model_shows_each_models_traffic_light(qapp, sai, monkeypatch):
    """The traffic light (model_profiles.support) also in Load a model: each entry's icon, its words as tooltip and
    accessible text, and the chosen model's detail panel ("no data yet" for one nothing is known about)."""
    from PySide6.QtCore import Qt
    from sandbox_ai_app import model_profile as MP
    known = "qwen/qwen3-coder-next"
    sai.answers[("local", ("llm", "models", "lm", "--json"))] = (0, json.dumps(MODELS + [{"name": known}]), "")
    page = page_of(sai)
    seen = []

    def form(dialog):
        try:
            check(dialog)
        except BaseException as exc:  # noqa: BLE001 - a slot's exception would be swallowed by Qt
            seen.append(exc)
        else:
            seen.append("checked")
        return QtWidgets.QDialog.DialogCode.Rejected

    def check(dialog):
        index = dialog.model.findData(known)
        assert not dialog.model.itemIcon(index).isNull(), "the light's icon"
        found = MP.support_of(sai, known)
        assert found["light"] == "green"
        assert MP.light_summary(found) in dialog.model.itemData(index, Qt.ItemDataRole.ToolTipRole)
        assert MP.light_accessible(found, known) in dialog.model.itemData(index, Qt.ItemDataRole.AccessibleTextRole)
        assert not dialog.support_area.isVisibleTo(dialog), "nothing chosen yet"
        dialog.model.setCurrentIndex(index)
        assert dialog.support_area.isVisibleTo(dialog)
        assert dialog.support_panel.text().startswith(f"Support of {known}\nverified in ")
        dialog.model.setCurrentIndex(dialog.model.findData("big-70b"))
        assert "no data yet" in dialog.support_panel.text()
        dialog.model.setEditText("")
        assert not dialog.support_area.isVisibleTo(dialog)
    monkeypatch.setattr(LLV.LoadDialog, "exec", form)
    open_dialog(page, monkeypatch, lambda dialog: dialog.load_button.click())
    if seen and isinstance(seen[0], BaseException):
        raise seen[0]
    assert seen == ["checked"]
    page.close()
