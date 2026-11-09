"""The LLM servers page as the user asked for it: the models in a table where one becomes the server's default model
(or is copied as NAME:MODEL), Edit… prefilled from `llm show NAME --json` and saved with --replace, Test connection in
the dialog (`sandbox-ai wizard models` for a server not saved yet), and a name checked as it is typed - a dialog that
never closes or loses what was typed on an error.

The argv tests need no Qt; each argv is parsed by sandbox-ai's own argparse (llm.py, wizard.py), so the page cannot
drift from the CLI. Qt tests run offscreen with a SandboxAI whose host_run records what it was asked."""
import argparse
import json
import os

import pytest

from sandbox_ai_app import backend
from sandbox_ai_app import wizard_llm as WL

needs_cli = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

SERVER = {"name": "lm", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "api_key_secret": "LLM_KEY",
          "headers": {"X-Team": "TEAM_ID"}, "ca": None, "vpn": "home", "model": "qwen2.5-coder:7b",
          "anthropic": False, "insecure_http": False, "models": [], "checked": None,
          "agents": ["claude", "codex", "opencode"]}
GATEWAY = {"name": "gw", "kind": "openai-compatible", "url": "https://gw.example.org/llm", "api_key_secret": "GW_KEY",
           "headers": {}, "ca": "gw.pem", "vpn": None, "model": None, "anthropic": True, "insecure_http": False,
           "models": [], "checked": None, "agents": ["claude", "codex", "opencode"]}
LONG = "lmstudio-community/" + "x" * 60 + "-GGUF"
MODELS = [{"name": "llama3.1:8b", "size": 8_000_000_000, "loaded": False, "details": "Q8_0", "type": "llm",
           "quantization": "Q8_0", "context": 131072},
          {"name": "qwen2.5-coder:7b", "size": 4_500_000_000, "loaded": True, "details": "Q4_K_M", "type": "llm",
           "quantization": "Q4_K_M", "context": 32768},
          {"name": LONG, "size": None, "loaded": False, "details": "", "type": "vlm"}]


class Parsed(Exception):
    pass


def parsed(sai, module, argv):
    """ARGV as sandbox-ai's MODULE ("llm": `sandbox-ai llm ARGS`, "wizard": `sandbox-ai wizard ARGS`) parses it: its
    argparse Namespace; SystemExit for one it refuses. Nothing runs: parsing ends it."""
    loaded = sai.module(module)
    seen = []
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        seen.append(original(self, args, namespace))
        raise Parsed
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Parsed):
            (loaded.main if module == "llm" else loaded.cli)(list(argv))
    return seen[0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


# -- without Qt: the name, the argv, an edit's settings kept, the models --

def test_a_name_may_start_with_digits_and_what_is_wrong_is_named():
    """The user's name was refused with "not starting with '-'" though it started with digits: what failed was a
    character the name cannot hold ('.' of an address, a space). Now the message names that character."""
    for good in ("4090-box", "192-168-1-20", "lmstudio", "a", "A_b-1", "x" * 32):
        assert WL.name_problem(good) is None and WL.valid_name(good) == good, good
    assert "'.'" in WL.name_problem("192.168.1.20") and "a digit may come first" in WL.name_problem("192.168.1.20")
    assert "'.'" in WL.name_problem("lm.studio") and "a space" in WL.name_problem("lm studio")
    assert "':'" in WL.name_problem("lm:1234") and "'ü'" in WL.name_problem("büro")
    assert "not '-'" in WL.name_problem("-x") and "not '_'" in WL.name_problem("_x")
    assert "33" in WL.name_problem("x" * 33) and "e.g. lmstudio" in WL.name_problem("")
    assert "U+0009" in WL.name_problem("a\tb")
    assert WL.suggest_name("192.168.1.20") == "192-168-1-20" and WL.suggest_name("lm.studio") == "lm-studio"
    assert WL.suggest_name("LM Studio (GPU)") == "LM-Studio-GPU" and WL.suggest_name("Büro") == "Buro"
    assert WL.suggest_name("-x") == "x" and WL.suggest_name("x" * 40) == "x" * 32
    assert WL.suggest_name("lmstudio") is None and WL.suggest_name("東京") is None
    with pytest.raises(ValueError, match="'.'"):
        WL.server_argv({"name": "lm.studio", "kind": "lmstudio", "url": "http://127.0.0.1:1234"})


@needs_cli
def test_the_names_rule_is_the_clis(env):
    llm = backend.SandboxAI().module("llm")
    for name in ("4090-box", "192.168.1.20", "lm studio", "-x", "_x", "x" * 32, "x" * 33, "a:b", "ü", "9"):
        assert (WL.name_problem(name) is None) == bool(llm.NAME.fullmatch(name)), name
        suggestion = WL.suggest_name(name)
        assert suggestion is None or llm.NAME.fullmatch(suggestion), name


@needs_cli
def test_an_edit_keeps_every_setting_of_llm_show_and_replaces(env, tmp_path, monkeypatch):
    """fields_of(`llm show NAME --json`) back through server_argv: llm add --replace with every option the server
    has. The CA is not among them: the store holds only a copy, and `llm add --replace` keeps it (--no-ca drops
    it) - run against the CLI itself here, so an edit cannot start losing it again."""
    sai = backend.SandboxAI()
    llm = sai.module("llm")
    record = {k: v for k, v in GATEWAY.items() if v not in (None, {}, []) and k not in ("name", "agents")}
    view = llm.public_view(llm.check_record("gw", record))
    fields = WL.fields_of(WL.parse_show(json.dumps(view)))
    assert fields["ca"] == "" and fields["has_ca"] is True
    argv = WL.server_argv(dict(fields, model="org/Model-GGUF"))
    assert argv == ["llm", "add", "gw", "--kind", "openai-compatible", "--url", "https://gw.example.org/llm",
                    "--model", "org/Model-GGUF", "--api-key-secret", "GW_KEY", "--anthropic", "--replace"], \
        "no --ca: llm add --replace keeps the CA the endpoint has"
    args = parsed(sai, "llm", argv[1:])
    assert (args.name, args.kind, args.url, args.model, args.api_key_secret, args.anthropic, args.ca, args.replace,
            args.no_ca, args.vpn, args.header_secret, args.insecure_http) == \
        ("gw", "openai-compatible", "https://gw.example.org/llm", "org/Model-GGUF", "GW_KEY", True, None, True, False,
         None, [], False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    llm.save_store({"version": 1, "endpoints": {"gw": dict(record, ca="gw.pem")}})
    llm.write_private(llm.ca_dir() / "gw.pem", b"-----BEGIN CERTIFICATE-----\n")
    assert llm.main(argv[1:], out=lambda *a: None, get=lambda name: "x") == 0
    assert llm.endpoint("gw")["ca"] == "gw.pem" and (llm.ca_dir() / "gw.pem").exists(), "the CA survives the change"
    assert llm.endpoint("gw")["model"] == "org/Model-GGUF"
    lm = WL.fields_of(dict(SERVER, insecure_http=True))
    assert lm["has_ca"] is False
    argv = WL.server_argv(lm)
    assert argv == ["llm", "add", "lm", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234", "--model",
                    "qwen2.5-coder:7b", "--vpn", "home", "--api-key-secret", "LLM_KEY",
                    "--header-secret=X-Team=TEAM_ID", "--insecure-http", "--replace"]
    args = parsed(sai, "llm", argv[1:])
    assert args.header_secret == ["X-Team=TEAM_ID"] and args.vpn == "home" and args.insecure_http
    assert WL.changes(lm, dict(lm, model="llama3.1:8b")) == ["Default model: qwen2.5-coder:7b → llama3.1:8b"]
    assert WL.changes(lm, dict(lm, url="http://192.168.1.21:1234", vpn="", insecure=False)) == [
        "URL: http://192.168.1.20:1234 → http://192.168.1.21:1234", "VPN profile: home → none",
        "Plain http outside the local networks: yes → no"]
    assert WL.changes(lm, dict(lm)) == []
    assert WL.changes(fields, dict(fields)) == [], "the CA it has stays: not a change"
    assert WL.changes(fields, dict(fields, ca="/etc/ssl/new.pem")) == \
        ["CA certificate: the CA kept now \u2192 /etc/ssl/new.pem"]
    with pytest.raises(ValueError):
        WL.parse_show("sandbox-ai llm: no LLM endpoint lm")


@needs_cli
def test_test_connection_asks_wizard_models_as_the_cli_takes_it(env):
    """`sandbox-ai wizard models --kind KIND --url URL --json` asks a server that is registered nowhere: a whole
    URL, https and a path prefix included; nothing is stored and no key is sent."""
    sai = backend.SandboxAI()
    argv = WL.probe_argv("lmstudio", "http://192.168.1.20:1234")
    assert argv == ["wizard", "models", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234", "--sort",
                    "loaded", "--json"]
    args = parsed(sai, "wizard", argv[1:])
    assert (args.command, args.kind, args.url, args.sort, args.json, args.llm, args.host) == \
        ("models", "lmstudio", "http://192.168.1.20:1234", "loaded", True, None, None)
    for url in ("https://gpu.example.org", "http://10.0.0.2:80/gpu/v1", "http://[fd7a:115c:a1e0::7]:8000",
                "http://gpu.lan"):
        assert WL.probe_argv("vllm", url)[5] == url, "the whole URL goes to the CLI, which checks it"
    for kind in WL.PROBE_KINDS:
        assert parsed(sai, "wizard", ["models", "--kind", kind, "--url", "http://h:1"]).kind == kind
    assert set(WL.PROBE_KINDS) == set(sai.module("wizard").SERVERS), "the kinds that command knows"
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        WL.probe_argv("openai-compatible", "http://10.0.0.2:4000")
    for url in ("ftp://a", "http://user@a", "-u", "http://a?q=1", ""):
        with pytest.raises(ValueError, match=r"http\(s\)://"):
            WL.probe_argv("vllm", url)


@needs_cli
def test_what_wizard_models_answers_is_read_as_the_cli_prints_it(env):
    """Its --json is that of `llm models --json`, model for model: parse_models reads both, so a long name comes
    through whole and can be chosen as the default model."""
    llm = backend.SandboxAI().module("llm")
    found = WL.parse_models(json.dumps(llm.sort_models(llm.clean_models(MODELS), "loaded")))
    assert [m["name"] for m in found] == ["qwen2.5-coder:7b", "llama3.1:8b", LONG]
    assert found[0]["loaded"] is True and found[0]["quantization"] == "Q4_K_M"
    with pytest.raises(ValueError, match="Cannot reach"):
        WL.parse_models("sandbox-wizard: Cannot reach lmstudio at 10.0.0.2:1234: timed out")


def test_the_models_table_shows_the_columns_with_values():
    models = WL.parse_models(json.dumps(MODELS + [{"name": "-x"}, {"name": "bad name"}, "x",
                                                  {"name": "z", "context": True, "type": "\x1b[2J"}]))
    assert [m["name"] for m in models] == ["llama3.1:8b", "qwen2.5-coder:7b", LONG, "z"]
    assert models[0]["context"] == 131072 and models[0]["quantization"] == "Q8_0" and "context" not in models[3]
    assert models[3]["type"] == "\\x1b[2J", "a server's text is made printable"
    assert [key for key, _ in WL.model_columns(models)] == ["name", "loaded", "type", "quantization", "context", "size"]
    assert [key for key, _ in WL.model_columns([{"name": "a", "size": None, "loaded": None}])] == ["name"]
    assert WL.model_cell(models[0], "context") == ("128k", 131072)
    assert WL.model_cell(models[1], "size") == ("4.5 GB", 4_500_000_000)
    assert WL.model_cell(models[1], "loaded")[0] == "yes" and WL.model_cell(models[3], "loaded") == ("", 2)
    assert [m["name"] for m in WL.model_order(models)] == ["qwen2.5-coder:7b", "llama3.1:8b", LONG, "z"]


# -- Qt: the dialog and the page --

@pytest.fixture
def qtenv(env, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    if not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"):
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    from sandbox_ai_app import llm_page, widgets
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])

    def synchronous(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001 - as ThreadCall hands it on
            done(None, exc)
            return
        done(result, None)
    monkeypatch.setattr(llm_page, "background", synchronous)
    return env


@pytest.fixture
def sai(qtenv, monkeypatch):
    """host_run records (host, args) and answers from sai.answers ({(host, args): (status, out, err)})."""
    sai = backend.SandboxAI()
    sai.asked, sai.secrets_set, sai.status_asked = [], [], []
    sai.answers = {("local", ("llm", "list", "--json")): (0, json.dumps([SERVER, GATEWAY]), ""),
                   ("local", ("llm", "show", "lm", "--json")): (0, json.dumps(SERVER), ""),
                   ("local", ("llm", "show", "gw", "--json")): (0, json.dumps(GATEWAY), ""),
                           ("local", ("llm", "models", "lm", "--sort", "loaded", "--json")):
                       (0, json.dumps(WL.model_order(MODELS)), "")}
    monkeypatch.setattr(sai, "hosts_supported", lambda: False)

    def host_run(name, *args, timeout=120):
        # What the servers have loaded now (llm status) is read beside every list: kept apart from the commands
        (sai.status_asked if args[:2] == ("llm", "status") else sai.asked).append((name, args))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "set_secret", lambda name, value, host="local": sai.secrets_set.append(
        (name, value) if host == "local" else (name, value, host)) or (0, "", ""))
    monkeypatch.setattr(sai, "secret_exists", lambda name, host="local": False)
    return sai


def adds(sai):
    return [args for _, args in sai.asked if args[:2] == ("llm", "add")]


def test_the_name_is_checked_as_it_is_typed(qapp, sai):
    from sandbox_ai_app import llm_page
    dialog = llm_page.LlmDialog(None, "local", taken=["lm"])
    dialog.show()
    assert not dialog.ok_button.isEnabled() and not dialog.form.isRowVisible(dialog.hint_row), \
        "nothing typed: no red hint yet, OK waits for a name"
    dialog.name.setText("192.168.1.20")
    assert dialog.form.isRowVisible(dialog.hint_row) and "'.'" in dialog.name_hint.text()
    assert not dialog.ok_button.isEnabled() and "'.'" in dialog.ok_button.toolTip()
    assert dialog.suggest_button.isVisible() and dialog.suggest_button.text() == "Use 192-168-1-20"
    dialog.suggest_button.click()
    assert dialog.name.text() == "192-168-1-20" and dialog.ok_button.isEnabled()
    assert not dialog.form.isRowVisible(dialog.hint_row)
    dialog.name.setText("4090-box")
    assert dialog.ok_button.isEnabled() and not dialog.form.isRowVisible(dialog.hint_row), "digits first are fine"
    dialog.name.setText("lm")
    assert dialog.ok_button.isEnabled() and "registered on this host already" in dialog.name_hint.text()
    dialog.replace.setChecked(True)
    assert not dialog.form.isRowVisible(dialog.hint_row)
    dialog.name.setText("-lm")
    assert "not '-'" in dialog.name_hint.text() and dialog.suggest_button.text() == "Use lm"
    dialog.close()
    dialog.deleteLater()


def test_an_error_never_closes_the_dialog_nor_loses_what_was_typed(qapp, sai, monkeypatch):
    from sandbox_ai_app import llm_page, widgets
    page = llm_page.LlmPage(sai)
    page.show()
    add = ("llm", "add", "4090-box", "--kind", "lmstudio", "--url", "http://192.168.1.30:1234", "--model", "m1",
           "--api-key-secret", "BOX_KEY")
    sai.answers[("local", add)] = (1, "", "sandbox-ai llm: there is no route to 192.168.1.30\n")
    seen = []

    def run(dialog):
        dialog.show()
        dialog.name.setText("4090-box")
        dialog.url.setText("http//192.168.1.30:1234")
        dialog.model.setText("m1")
        dialog.secret.setText("BOX_KEY")
        dialog.value.setText("sk-box")
        dialog.ok_button.click()
        seen.append(("url", dialog.isVisible(), dialog.note.text()))
        dialog.url.setText("http://192.168.1.30:1234")
        dialog.ok_button.click()
        seen.append(("cli", dialog.isVisible(), dialog.note.text(), dialog.name.text(), dialog.url.text(),
                     dialog.model.text(), dialog.secret.text(), dialog.value.text(), dialog.ok_button.isEnabled()))
        del sai.answers[("local", add)]
        dialog.ok_button.click()
        seen.append(("saved", dialog.isVisible()))
        return dialog.result()
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    page.add_button.click()
    assert seen[0][:2] == ("url", True) and "http(s)://HOST" in seen[0][2], "a field the CLI would refuse: said inline"
    assert seen[1] == ("cli", True, "sandbox-ai llm add refused it: sandbox-ai llm: there is no route to 192.168.1.30",
                       "4090-box", "http://192.168.1.30:1234", "m1", "BOX_KEY", "sk-box", True)
    assert seen[2] == ("saved", False), "closed once the server is saved"
    assert adds(sai) == [add, add] and sai.secrets_set == [("BOX_KEY", "sk-box")], "the refused one kept no key"
    assert widgets.SHOWN == [], "nothing in a message box: the dialog said it"
    page.close()
    page.deleteLater()


def test_a_question_answered_no_keeps_the_dialog_open(qapp, sai, monkeypatch):
    from sandbox_ai_app import llm_page, widgets
    page = llm_page.LlmPage(sai)
    page.show()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    seen = []

    def run(dialog):
        dialog.show()
        dialog.name.setText("pub")
        dialog.kind.setCurrentIndex(dialog.kind.findData("vllm"))
        dialog.url.setText("http://203.0.113.5:8000")
        dialog.insecure.setChecked(True)
        dialog.ok_button.click()
        seen.append((dialog.isVisible(), dialog.note.text(), dialog.url.text(), dialog.insecure.isChecked()))
        return dialog.result()
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    sai.asked.clear()
    page.add()
    assert seen == [(True, "Not saved: plain http outside the local networks was not allowed.",
                     "http://203.0.113.5:8000", True)]
    assert adds(sai) == [] and "unencrypted" in widgets.SHOWN[-1][1]
    page.close()
    page.deleteLater()


def test_test_connection_lists_the_models_and_fills_the_default_model(qapp, sai):
    from sandbox_ai_app import llm_page
    llm = sai.module("llm")
    listed = llm.sort_models(llm.clean_models(MODELS), "loaded")
    probe = ("wizard", "models", "--kind", "lmstudio", "--url", "http://192.168.1.30:1234", "--status", "--sort",
             "loaded", "--json")
    sai.answers[("local", probe)] = (0, json.dumps(listed), "")
    page = llm_page.LlmPage(sai)
    dialog = llm_page.LlmDialog(page, "local", run=page.runner("local"))
    dialog.show()
    dialog.name.setText("box")
    dialog.url.setText("http://192.168.1.30:1234")
    sai.asked.clear()
    dialog.test_button.click()
    assert sai.asked == [("local", probe)], "the server is asked without being registered"
    assert dialog.test_result.text() == ("Reachable: 3 models, 1 loaded. Choose the default model below (or leave "
                                         "it empty).")
    assert [dialog.model_box.itemText(i) for i in range(dialog.model_box.count())] == \
        ["qwen2.5-coder:7b", "llama3.1:8b", LONG], "a long name whole: --json, not the CLI's table"
    assert dialog.model.text() == "", "nothing chosen for the human: the default model stays optional"
    dialog.model_box.setCurrentIndex(1)
    assert dialog.request()[0]["model"] == "llama3.1:8b"
    dialog.model.setText("typed-model")
    dialog.test()
    assert dialog.model.text() == "typed-model", "a test again keeps what was typed"
    refused = (1, "", "sandbox-wizard: Cannot reach lmstudio at 192.168.1.30:1234: refused\n")
    keyed = probe[:6] + ("--api-key-secret", "BOX_KEY") + probe[6:]
    sai.answers[("local", keyed)] = sai.answers[("local", probe)] = refused
    dialog.secret.setText("BOX_KEY")
    sai.asked.clear()
    dialog.test()
    assert sai.asked == [("local", keyed)], "the secret named goes with the test (its name: the key stays there)"
    assert dialog.test_result.text().startswith("Not reachable: sandbox-wizard: Cannot reach lmstudio")
    assert "Tested without its key" not in dialog.test_result.text()
    dialog.value.setText("sk-new")
    sai.asked.clear()
    dialog.test()
    assert sai.asked == [("local", probe)], "a key typed is not kept yet: the test goes without one"
    assert "Tested without its key" in dialog.test_result.text()
    dialog.value.clear()
    dialog.secret.clear()
    dialog.url.setText("https://gpu.example.org/llm")
    assert dialog.test_result.text() == "", "another URL: the earlier result says nothing about it"
    https = ("wizard", "models", "--kind", "lmstudio", "--url", "https://gpu.example.org/llm", "--status", "--sort",
             "loaded", "--json")
    now = {"loaded": [{"name": "qwen2.5-coder:7b", "size": 4_500_000_000, "memory_bytes": 5_000_000_000,
                       "vram_bytes": 4_000_000_000, "ram_bytes": 1_000_000_000, "context": 8192}],
           "usage": {"vram_bytes": 4_000_000_000, "ram_bytes": 1_000_000_000}, "reported_by": ["Ollama /api/ps"],
           "not_reported": ["kv_cache", "requests", "totals"], "read_at": "2026-11-09T12:00:00Z"}
    sai.answers[("local", https)] = (0, json.dumps({"models": listed[:1], "status": now}), "")
    sai.asked.clear()
    dialog.test()
    assert sai.asked == [("local", https)], "https and a path prefix are asked too"
    assert dialog.test_result.text().startswith("Reachable: 1 models, 1 loaded.")
    assert dialog.test_result.text().splitlines()[-1] == (
        "Now: loaded: qwen2.5-coder:7b (5.0 GB in memory: VRAM 4.0 GB, RAM 1.0 GB; context 8192) · VRAM 4.0 GB · "
        "RAM 1.0 GB · not reported: KV cache, requests"), "what it has loaded now, with sizes and VRAM/RAM"
    assert "the host's total RAM/VRAM" in dialog.test_result.toolTip()
    dialog.kind.setCurrentIndex(dialog.kind.findData("openai-compatible"))
    sai.asked.clear()
    dialog.test()
    assert sai.asked == [] and dialog.check_after
    assert "An OpenAI-compatible server cannot be tested before it is saved." in dialog.test_result.text()
    assert "sandbox-ai llm check" in dialog.test_result.text()
    dialog.close()
    dialog.deleteLater()
    page.deleteLater()


def test_a_server_test_connection_cannot_reach_is_checked_once_saved(qapp, sai, monkeypatch):
    from sandbox_ai_app import llm_page
    page = llm_page.LlmPage(sai)
    page.show()

    def run(dialog):
        dialog.show()
        dialog.name.setText("gw2")
        dialog.kind.setCurrentIndex(dialog.kind.findData("openai-compatible"))
        dialog.url.setText("https://gw2.example.org")
        dialog.test_button.click()
        dialog.ok_button.click()
        return dialog.result()
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    sai.asked.clear()
    page.add()
    assert [a for _, a in sai.asked][:3] == [
        ("llm", "add", "gw2", "--kind", "openai-compatible", "--url", "https://gw2.example.org"),
        ("llm", "list", "--json"), ("llm", "check", "gw2")]
    page.close()
    page.deleteLater()


def test_the_models_table_and_use_as_default(qapp, sai, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from sandbox_ai_app import llm_page, widgets
    page = llm_page.LlmPage(sai)
    page.show()
    assert page.models_box.isHidden() and page.sort.currentData() == "loaded"
    page.servers.selectRow(0)
    page.models_button.click()
    assert page.models_box.isVisible()
    widget = page.model_table
    headers = [widget.horizontalHeaderItem(c).text() for c in range(widget.columnCount())]
    assert headers == ["Name", "Loaded", "Type", "Quant", "Context", "Size"], "only the columns with values"
    names = [widget.row_keys[widgets.row_index(widget, r)] for r in range(widget.rowCount())]
    assert names == ["qwen2.5-coder:7b", "llama3.1:8b", LONG], "loaded first, then by name"
    long_row = names.index(LONG)
    assert LONG in widget.item(long_row, 0).toolTip(), "the whole name in the tooltip"
    assert widget.item(0, 4).text() == "32k" and widget.item(0, 5).text() == "4.5 GB"
    assert "the default model of lm" in widget.item(0, 0).toolTip()
    page.model_filter.setText("LLAMA")
    assert [widget.isRowHidden(r) for r in range(3)] == [True, False, True]
    page.model_filter.clear()
    widget.selectRow(0)
    assert not page.default_button.isEnabled(), "the default model already"
    widget.selectRow(1)
    assert page.default_button.isEnabled() and page.copy_button.isEnabled()
    page.copy_button.click()
    assert QApplication.clipboard().text() == "lm:llama3.1:8b"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    sai.asked.clear()
    page.default_button.click()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Use as default" and text.startswith("On this computer: Change the LLM server lm?")
    assert "Default model: qwen2.5-coder:7b → llama3.1:8b" in text and text.count("→") == 1
    assert sai.asked == [("local", ("llm", "show", "lm", "--json"))] and adds(sai) == [], "declined: nothing changed"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.default_button.click()
    add = ("llm", "add", "lm", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234", "--model", "llama3.1:8b",
           "--vpn", "home", "--api-key-secret", "LLM_KEY", "--header-secret=X-Team=TEAM_ID", "--replace")
    assert adds(sai) == [add], "every other setting of llm show kept"
    args = parsed(sai, "llm", add[1:])
    assert args.model == "llama3.1:8b" and args.replace and args.header_secret == ["X-Team=TEAM_ID"]
    assert "llama3.1:8b is the default model of lm now." in page.status.text()
    row = [widgets.row_index(widget, r) for r in range(widget.rowCount())].index(1)
    assert "the default model of lm" in widget.item(row, 0).toolTip()
    page.servers.selectRow(1)
    assert page.models_box.isHidden(), "another server: the models shown are not its"
    page.close()
    page.deleteLater()


def test_edit_is_prefilled_from_llm_show_and_saved_with_replace(qapp, sai, monkeypatch):
    from sandbox_ai_app import llm_page, widgets
    page = llm_page.LlmPage(sai)
    page.show()
    seen = []

    def run(dialog):
        seen.append(list(sai.asked))
        seen.append(dict(dialog.request()[0], title=dialog.windowTitle(), read_only=dialog.name.isReadOnly(),
                         replace_shown=dialog.form.isRowVisible(dialog.replace),
                         ca_hint=dialog.ca.placeholderText()))
        dialog.show()
        sai.asked.clear()
        dialog.test_button.click()  # reached as it was saved: llm models NAME, with its key, TLS and CA
        seen.append([a for _, a in sai.asked])
        dialog.ok_button.click()  # nothing changed: closes, nothing runs
        return dialog.result()
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    row = [widgets.row_key(page.servers, r) for r in range(page.servers.rowCount())].index("gw")
    sai.asked.clear()
    page.servers.selectRow(row)  # as a double-click does first
    page.servers.cellDoubleClicked.emit(row, 0)
    assert seen[0] == [("local", ("llm", "show", "gw", "--json"))], "one command: --replace keeps the CA itself"
    assert seen[1] == {"name": "gw", "kind": "openai-compatible", "url": "https://gw.example.org/llm", "model": "",
                       "vpn": "", "anthropic": True, "secret": "GW_KEY", "headers": "", "ca": "",
                       "insecure": False, "replace": True, "no_ca": False,
                       "title": "Edit the LLM server gw on local (this machine)",
                       "read_only": True, "replace_shown": False,
                       "ca_hint": "a CA is kept and stays; name a PEM file of that host to replace it"}
    assert seen[2] == [("llm", "models", "gw", "--sort", "loaded", "--json")]
    assert adds(sai) == [] and "gw: nothing changed." in page.status.text()

    seen.clear()

    def change(dialog):
        dialog.show()
        dialog.url.setText("https://gw2.example.org/llm")
        dialog.model.setText("org/Model-GGUF")
        dialog.ok_button.click()
        seen.append((dialog.isVisible(), dialog.note.text()))
        return dialog.result()
    monkeypatch.setattr(llm_page.LlmDialog, "exec", change)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.servers.selectRow(row)
    page.edit_button.click()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Edit LLM server" and "Change the LLM server gw?" in text
    assert "URL: https://gw.example.org/llm → https://gw2.example.org/llm" in text
    assert "Default model: none → org/Model-GGUF" in text and text.count("→") == 2
    assert seen == [(True, "Not saved.")] and adds(sai) == []
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    seen.clear()
    page.edit()
    add = ("llm", "add", "gw", "--kind", "openai-compatible", "--url", "https://gw2.example.org/llm", "--model",
           "org/Model-GGUF", "--api-key-secret", "GW_KEY", "--anthropic", "--replace")
    assert adds(sai) == [add] and seen == [(False, "")]
    args = parsed(sai, "llm", add[1:])
    assert args.replace and args.ca is None and not args.no_ca, "the CA of gw stays: llm add --replace keeps it"
    assert args.anthropic and args.url == "https://gw2.example.org/llm"
    page.close()
    page.deleteLater()


def test_an_edit_that_cannot_be_read_says_so(qapp, sai, monkeypatch):
    from sandbox_ai_app import llm_page
    sai.answers[("local", ("llm", "show", "lm", "--json"))] = (1, "", "sandbox-ai llm: no LLM endpoint lm\n")
    opened = []
    monkeypatch.setattr(llm_page.LlmDialog, "exec", lambda dialog: opened.append(dialog) or 0)
    page = llm_page.LlmPage(sai)
    page.show()
    page.servers.selectRow(0)
    page.edit()
    assert opened == [] and "Could not read lm: sandbox-ai llm: no LLM endpoint lm" in page.status.text()
    page.close()
    page.deleteLater()


# -- Test connection over plain http with a key: refused by sandbox-ai, asked (llm-insecure), tested again --

PUBLIC = "http://203.0.113.5:8000"


@needs_cli
def test_the_key_and_insecure_http_go_to_wizard_models_as_the_cli_takes_them(env):
    """--api-key-secret NAME (a secret's name, never a key) and --insecure-http, parsed by sandbox-ai's own wizard;
    the refusal the app answers with its question is the one llm.discover_url gives, word for word."""
    sai = backend.SandboxAI()
    argv = WL.probe_argv("vllm", PUBLIC, "BOX_KEY", insecure=True)
    assert argv == ["wizard", "models", "--kind", "vllm", "--url", PUBLIC, "--api-key-secret", "BOX_KEY",
                    "--insecure-http", "--sort", "loaded", "--json"]
    args = parsed(sai, "wizard", argv[1:])
    assert (args.kind, args.url, args.api_key_secret, args.insecure_http, args.json) == \
        ("vllm", PUBLIC, "BOX_KEY", True, True)
    plain = parsed(sai, "wizard", WL.probe_argv("vllm", PUBLIC, "BOX_KEY")[1:])
    assert plain.api_key_secret == "BOX_KEY" and plain.insecure_http is False, "never without the question"
    assert "--insecure-http" not in WL.probe_argv("vllm", "https://gpu.example.org", "BOX_KEY", insecure=True), \
        "only for plain http (the CLI refuses it for https)"
    assert "--insecure-http" not in WL.probe_argv("vllm", PUBLIC, None, insecure=True), "no key: nothing to allow"
    with pytest.raises(ValueError, match="secret"):
        WL.probe_argv("vllm", PUBLIC, "-x")
    llm = sai.module("llm")
    with pytest.raises(ValueError) as refusal:
        llm.discover_url("vllm", PUBLIC, api_key_secret="BOX_KEY")
    assert WL.plain_http_refused(f"sandbox-wizard: {refusal.value}\n")
    assert not WL.plain_http_refused("sandbox-wizard: Cannot reach vllm at 203.0.113.5:8000: timed out")
    assert not WL.plain_http_refused("sandbox-wizard: no secret BOX_KEY; keep the key with: sandbox-ai secrets set")
    add = WL.server_argv({"name": "pub", "kind": "vllm", "url": PUBLIC, "secret": "BOX_KEY", "insecure": True})
    added = parsed(sai, "llm", add[1:])
    assert added.insecure_http is True and added.api_key_secret == "BOX_KEY", "Add sends the flag the test asked for"


def refused_then_listed(sai, host="local"):
    # This machine's sandbox-ai is the app's own: the test asks what the server has loaded now too (--status).
    keyed = ("wizard", "models", "--kind", "vllm", "--url", PUBLIC, "--api-key-secret", "BOX_KEY",
             *(("--status",) if host == "local" else ()), "--sort", "loaded", "--json")
    allowed = keyed[:8] + ("--insecure-http",) + keyed[8:]
    sai.answers[(host, keyed)] = (1, "", "sandbox-wizard: plain http to 203.0.113.5 would cross networks you do not "
                                        "control unencrypted; use https, or --insecure-http if you accept that (the API "
                                        "key would go with it)\n")
    sai.answers[(host, allowed)] = (0, json.dumps(WL.model_order(MODELS)), "")
    return keyed, allowed


def plain_http_questions(widgets):
    return [text for _, text, _ in widgets.SHOWN if "over plain http" in text or "plain http to" in text]


def test_a_refused_plain_http_test_asks_then_tests_again_and_add_sends_the_flag(qapp, sai, monkeypatch):
    """The CLI refuses to send the key over plain http to a public address: the dialog asks once (llm-insecure,
    naming the host and the URL), tests again with --insecure-http, ticks the box, and Add sends --insecure-http
    without asking a second time."""
    from sandbox_ai_app import llm_page, widgets
    keyed, allowed = refused_then_listed(sai)
    page = llm_page.LlmPage(sai)
    page.show()
    seen = []

    def run(dialog):
        dialog.show()
        dialog.name.setText("pub")
        dialog.kind.setCurrentIndex(dialog.kind.findData("vllm"))
        dialog.url.setText(PUBLIC)
        dialog.secret.setText("BOX_KEY")
        sai.asked.clear()
        dialog.test_button.click()
        seen.append(([a for _, a in sai.asked], dialog.insecure.isChecked(), dialog.test_result.text()))
        dialog.ok_button.click()
        return dialog.result()
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    page.add()
    asked, ticked, result = seen[0]
    assert asked == [keyed, allowed], "refused, asked, tested again with --insecure-http"
    assert ticked and result.startswith("Reachable: 3 models"), result
    questions = plain_http_questions(widgets)
    assert len(questions) == 1, "asked once: Add does not ask again for the URL the test allowed"
    assert PUBLIC in questions[0] and "from local (this machine)?" in questions[0], "the host and the URL named"
    add = adds(sai)
    assert len(add) == 1 and "--insecure-http" in add[0] and add[0][add[0].index("--url") + 1] == PUBLIC
    page.close()
    page.deleteLater()


def test_a_no_tests_nothing_and_other_errors_ask_nothing(qapp, sai, monkeypatch):
    from sandbox_ai_app import llm_page, widgets
    keyed, allowed = refused_then_listed(sai)
    page = llm_page.LlmPage(sai)
    dialog = llm_page.LlmDialog(page, "local", run=page.runner("local"))
    dialog.show()
    dialog.kind.setCurrentIndex(dialog.kind.findData("vllm"))
    dialog.url.setText(PUBLIC)
    dialog.secret.setText("BOX_KEY")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    sai.asked.clear()
    dialog.test()
    assert [a for _, a in sai.asked] == [keyed], "answered No: not tested again"
    assert dialog.test_result.text().startswith("Not tested: the key is not sent over plain http")
    assert not dialog.insecure.isChecked() and dialog.insecure_confirmed is None
    widgets.SHOWN.clear()
    sai.answers[("local", keyed)] = (1, "", "sandbox-wizard: Cannot reach vllm at 203.0.113.5:8000: timed out\n")
    dialog.test()
    assert plain_http_questions(widgets) == [] and dialog.test_result.text().startswith("Not reachable"), \
        "only that refusal is asked about"
    dialog.close()
    dialog.deleteLater()
    page.deleteLater()


def test_another_host_is_checked_for_the_options_first(qapp, sai, monkeypatch):
    """A host whose `wizard models` lacks --url (0.1.77) is told to update, nothing runs; one that lacks only
    --api-key-secret/--insecure-http is tested without the key, as before; one that knows them asks on its own
    terms (the question names that host)."""
    from sandbox_ai_app import llm_page, widgets
    keyed, allowed = refused_then_listed(sai, "ct1")
    plain = ("wizard", "models", "--kind", "vllm", "--url", PUBLIC, "--sort", "loaded", "--json")
    sai.answers[("ct1", plain)] = (0, json.dumps(WL.model_order(MODELS)), "")
    known = {"ct1": set()}

    def command_options(host, *command):
        assert command == ("wizard", "models")
        return set(known[host])
    monkeypatch.setattr(sai, "host_command_options", command_options)
    page = llm_page.LlmPage(sai)
    dialog = llm_page.LlmDialog(page, "ct1", remote=True, run=page.runner("ct1"), require=page.requirement("ct1"))
    assert page.requirement("local") is None, "this machine's sandbox-ai is the app's own"
    dialog.show()
    dialog.kind.setCurrentIndex(dialog.kind.findData("vllm"))
    dialog.url.setText(PUBLIC)
    dialog.secret.setText("BOX_KEY")
    sai.asked.clear()
    dialog.test()
    assert sai.asked == [] and "too old" in dialog.test_result.text() and "--url" in dialog.test_result.text()
    known["ct1"] = {"--url", "--kind", "--sort", "--json"}
    dialog.test()
    assert sai.asked == [("ct1", plain)] and dialog.test_result.text().startswith("Reachable"), \
        "without the key where the host does not know it yet"
    known["ct1"] |= {"--api-key-secret", "--insecure-http"}
    sai.asked.clear()
    dialog.test()
    assert sai.asked == [("ct1", keyed), ("ct1", allowed)] and dialog.insecure.isChecked()
    assert "from ct1?" in plain_http_questions(widgets)[-1]
    dialog.close()
    dialog.deleteLater()
    page.deleteLater()
