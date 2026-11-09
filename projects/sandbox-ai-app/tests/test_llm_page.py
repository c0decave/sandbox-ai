"""The LLM servers page (offscreen): the endpoints of sandbox-ai llm on this machine or a host of sandbox-ai hosts,
each command through SandboxAI.host_run (here a stand-in that records what it was asked). Every argv is also
parsed with sandbox-ai llm's own argparse, so the page cannot drift from the options the CLI has. A typed API key
goes to set_secret (standard input) and never into an argv; for another host only a secret's name is asked."""
import argparse
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app import llm_page  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

KEY = "sk-typed-into-the-app-only"
LOCAL_SERVER = {"name": "lm", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "api_key_secret": "LLM_KEY",
                "headers": {}, "ca": None, "vpn": None, "model": "qwen2.5-coder:7b", "anthropic": False,
                "insecure_http": False, "models": [], "checked": None, "agents": ["claude", "codex", "opencode"]}
REMOTE_SERVER = {"name": "gpu", "kind": "vllm", "url": "http://100.64.0.7:8000", "api_key_secret": None,
                 "headers": {}, "ca": None, "vpn": "ts", "model": None, "anthropic": False, "insecure_http": False,
                 "models": [], "checked": None, "agents": ["claude", "codex", "opencode"]}
# `llm status --json` of LOCAL_SERVER: LM Studio reports what is loaded, not what it uses.
STATUS = {"name": "lm", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "reachable": True, "error": None,
          "read_at": "2026-11-09T12:00:00Z", "models": 2,
          "loaded": [{"name": "qwen2.5-coder:7b", "size": 4_500_000_000, "context": 8192}], "usage": {},
          "reported_by": ["LM Studio /api/v1/models"], "not_reported": ["memory", "kv_cache", "requests", "totals"]}
MODELS = [{"name": "qwen2.5-coder:7b", "size": 4_500_000_000, "loaded": True, "details": "7B Q4_K_M"},
          {"name": "llama3.1:8b", "size": None, "loaded": None, "details": ""}]


class Parsed(Exception):
    pass


def parsed(sai, argv):
    """ARGV (["llm", ...]) as `sandbox-ai llm` parses it: its argparse Namespace, or SystemExit for an argv it
    refuses. Nothing of the command runs: parsing ends it."""
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
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
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
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    """A SandboxAI with this machine and one host (ct1), whose host_run records (host, args) and answers from
    sai.answers ({(host, args): (status, out, err)}); set_secret records (name, value)."""
    sai = backend.SandboxAI()
    sai.asked, sai.secrets_set, sai.status_asked = [], [], []
    sai.answers = {("local", ("llm", "list", "--json")): (0, json.dumps([LOCAL_SERVER]), ""),
                   ("ct1", ("llm", "list", "--json")): (0, "old: unusable (url: bad)\n" + json.dumps([REMOTE_SERVER]),
                                                        "")}
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}])

    def host_run(name, *args, timeout=120):
        # What the servers have loaded now (llm status) is read beside every list: kept apart from the commands
        (sai.status_asked if args[:2] == ("llm", "status") else sai.asked).append((name, args))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "set_secret", lambda name, value, host="local": sai.secrets_set.append(
        (name, value) if host == "local" else (name, value, host)) or (0, "", ""))
    return sai


def cells(page, row=0):
    return [page.servers.item(row, c).text() for c in range(page.servers.columnCount())]


def test_the_servers_of_this_machine_and_of_a_host(qapp, sai):
    page = llm_page.LlmPage(sai)
    assert [page.host_box.itemData(i) for i in range(page.host_box.count())] == ["local", "ct1"]
    assert sai.asked == [], "a page nobody opened asks nothing (no command, no SSH at every start of the app)"
    sai.answers[("local", ("llm", "status", "--json"))] = (0, json.dumps([STATUS]), "")
    page.show()
    assert sai.asked == [("local", ("llm", "list", "--json"))]
    assert sai.status_asked == [("local", ("llm", "status", "--json"))], "then what its servers have loaded now"
    assert cells(page) == ["lm", "lmstudio", "http://192.168.1.20:1234", "qwen2.5-coder:7b", "LLM_KEY", "",
                           "claude, codex, opencode"]
    assert page.server_status.text() == ("lm: loaded: qwen2.5-coder:7b (4.5 GB; context 8192) · not reported: "
                                         "VRAM/RAM use, KV cache, requests")
    assert "LLM servers on local (this machine): 1" in page.status.text()
    page.host_box.setCurrentIndex(page.host_box.findData("ct1"))
    assert sai.asked[-1] == ("ct1", ("llm", "list", "--json"))
    assert page.servers.rowCount() == 1 and cells(page)[:3] == ["gpu", "vllm", "http://100.64.0.7:8000"]
    assert cells(page)[5] == "ts"
    assert "old: unusable (url: bad)" in page.status.text(), "what the CLI found unusable is said, the rest listed"
    sai.answers[("ct1", ("llm", "list", "--json"))] = (0, json.dumps([dict(REMOTE_SERVER, kind="vllm\x1b[2J")]), "")
    page.refresh_button.click()
    assert cells(page)[1] == "vllm\\x1b[2J", "a host's answer is shown printable"
    sai.answers[("ct1", ("llm", "list", "--json"))] = (255, "", "ssh: connect to host 192.0.2.10: timed out")
    page.refresh()
    assert "timed out" in page.status.text()
    for argv in (("llm", "list", "--json"),):
        assert parsed(sai, argv).json is True
    page.close()
    page.deleteLater()


def test_adding_on_this_machine_keeps_the_key_as_a_secret_never_on_a_command_line(qapp, sai, monkeypatch):
    from PySide6.QtWidgets import QDialog, QLineEdit
    page = llm_page.LlmPage(sai)
    page.show()
    dialogs = []

    def run(dialog):
        dialogs.append(dialog)
        assert not dialog.remote and dialog.form.isRowVisible(dialog.value_row)
        assert dialog.value.echoMode() == QLineEdit.EchoMode.Normal and dialog.value.show_toggle.isChecked()
        dialog.name.setText("lm2")
        dialog.kind.setCurrentIndex(dialog.kind.findData("lmstudio"))
        dialog.url.setText("http://192.168.1.21:1234")
        dialog.model.setText("qwen2.5-coder:7b")
        dialog.secret.setText("LLM_KEY")
        dialog.value.setText(KEY)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    sai.asked.clear()
    page.add_button.click()
    assert sai.secrets_set == [("LLM_KEY", KEY)], "the key is kept as a secret once llm add took the server"
    add = ("llm", "add", "lm2", "--kind", "lmstudio", "--url", "http://192.168.1.21:1234", "--model",
           "qwen2.5-coder:7b", "--api-key-secret", "LLM_KEY")
    assert sai.asked[0] == ("local", add)
    assert sai.asked[1] == ("local", ("llm", "list", "--json")), "the list is asked again"
    assert KEY not in json.dumps(sai.asked), "never in an argv"
    assert [d.value.text() for d in dialogs] == [""], "the typed key does not stay behind in the dialog"
    assert not any(e.text() == KEY for e in page.findChildren(QLineEdit))
    args = parsed(sai, list(add))
    assert (args.command, args.name, args.kind, args.url, args.model, args.api_key_secret, args.vpn,
            args.anthropic) == ("add", "lm2", "lmstudio", "http://192.168.1.21:1234", "qwen2.5-coder:7b", "LLM_KEY",
                                None, False)
    page.close()
    page.deleteLater()


def test_a_refused_server_keeps_no_key_and_an_existing_secret_is_replaced_only_after_a_yes(qapp, sai, monkeypatch):
    """Review round 3 (A3): `secrets set` ran before `llm add`, so a server that llm add refused had replaced an
    existing secret's value anyway, without a word."""
    from PySide6.QtWidgets import QDialog
    page = llm_page.LlmPage(sai)
    page.show()

    def run(dialog):
        dialog.name.setText("lm2")
        dialog.kind.setCurrentIndex(dialog.kind.findData("lmstudio"))
        dialog.url.setText("http://192.168.1.21:1234")
        dialog.secret.setText("LLM_KEY")
        dialog.value.setText(KEY)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    add = ("llm", "add", "lm2", "--kind", "lmstudio", "--url", "http://192.168.1.21:1234", "--api-key-secret",
           "LLM_KEY")
    sai.answers[("local", add)] = (1, "", "sandbox-ai llm: there is an LLM endpoint lm2 already")
    page.add()
    assert ("local", add) in sai.asked and sai.secrets_set == [], "refused: the key was not kept"
    assert "already" in widgets.SHOWN[-1][1]
    del sai.answers[("local", add)]
    monkeypatch.setattr(sai, "secret_exists", lambda name, host="local": True)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    sai.asked.clear()
    page.add()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "New LLM server" and "LLM_KEY is kept already" in text
    assert sai.asked == [] and sai.secrets_set == [], "declined: nothing added, the secret stays"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.add()
    assert sai.asked[0] == ("local", add) and sai.secrets_set == [("LLM_KEY", KEY)]
    page.close()
    page.deleteLater()


def test_the_dialogs_argv_for_every_field_fits_sandbox_ai_llm(qapp, sai):
    argv = llm_page.add_argv({"name": "gw", "kind": "openai-compatible", "url": "https://gw.example.org/llm",
                              "model": "org/Model-GGUF", "vpn": "home", "secret": "GW_KEY", "anthropic": True})
    assert argv == ["llm", "add", "gw", "--kind", "openai-compatible", "--url", "https://gw.example.org/llm",
                    "--model", "org/Model-GGUF", "--vpn", "home", "--api-key-secret", "GW_KEY", "--anthropic"]
    args = parsed(sai, argv)
    assert (args.kind, args.vpn, args.api_key_secret, args.anthropic) == ("openai-compatible", "home", "GW_KEY", True)
    assert llm_page.add_argv({"name": "o", "kind": "ollama", "url": "http://127.0.0.1:11434", "anthropic": True}) == \
        ["llm", "add", "o", "--kind", "ollama", "--url", "http://127.0.0.1:11434"], "Ollama has the API itself"
    for key, _ in llm_page.KINDS:
        assert parsed(sai, ["llm", "add", "x", "--kind", key, "--url", "http://127.0.0.1:1"]).kind == key
    for fields in ({"name": "-x"}, {"name": "--replace"}, {"kind": "ipsec"}, {"url": "ftp://a"},
                   {"url": "http://user@a"}, {"url": "-u"}, {"model": "-m"}, {"model": "a b"}, {"vpn": "-v"},
                   {"secret": "1KEY"}, {"secret": "-s"}):
        with pytest.raises(ValueError):
            llm_page.add_argv({"name": "a", "kind": "ollama", "url": "http://127.0.0.1:11434", **fields})


def test_adding_on_a_host_keeps_the_typed_key_as_a_secret_there(qapp, sai, monkeypatch):
    """Review 5 (L4): the key of a server on another host is typed where the server is added, as on this machine: it is
    kept as a secret of that host (`secrets set` over SSH, standard input) once `llm add` there took the server."""
    from PySide6.QtWidgets import QDialog
    page = llm_page.LlmPage(sai)
    page.show()
    page.host_box.setCurrentIndex(page.host_box.findData("ct1"))
    monkeypatch.setattr(sai, "secret_exists", lambda name, host="local": False)

    def run(dialog):
        assert dialog.remote and dialog.form.isRowVisible(dialog.value_row), "a key is typed for another host too"
        dialog.name.setText("gpu2")
        dialog.kind.setCurrentIndex(dialog.kind.findData("vllm"))
        dialog.url.setText("http://100.64.0.8:8000")
        dialog.vpn.setText("ts")
        dialog.secret.setText("REMOTE_KEY")
        dialog.value.setText(KEY)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    sai.asked.clear()
    page.add()
    add = ("llm", "add", "gpu2", "--kind", "vllm", "--url", "http://100.64.0.8:8000", "--vpn", "ts",
           "--api-key-secret", "REMOTE_KEY")
    deadline = time.monotonic() + 10
    while not sai.secrets_set and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    asked = [a for a in sai.asked if a[1][:2] not in (("vpn", "list"), ("secrets", "list"))]  # the fields' names
    assert asked[0] == ("ct1", add) and sai.secrets_set == [("REMOTE_KEY", KEY, "ct1")]
    assert KEY not in json.dumps(sai.asked)
    assert parsed(sai, list(add)).vpn == "ts"
    page.close()
    page.deleteLater()


def test_check_and_models_show_what_the_host_says(qapp, sai):
    page = llm_page.LlmPage(sai)
    page.show()
    check = ("llm", "check", "lm", "--agent", "claude")
    sai.answers[("local", check)] = (1, "lm: LM Studio at http://192.168.1.20:1234\n  ok    address 192.168.1.20\n"
                                        "  FAIL  the server refused the key\x07", "")
    models = ("llm", "models", "lm", "--sort", "loaded", "--json")  # loaded first, then by name: the default
    sai.answers[("local", models)] = (0, json.dumps(MODELS), "")
    page.servers.selectRow(0)
    page.agent.setCurrentIndex(page.agent.findData("claude"))
    page.check_button.click()
    assert sai.asked[-1] == ("local", check)
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Check of lm on local (this machine)" and text.startswith("the check found problems")
    assert "FAIL  the server refused the key\\x07" in text, "shown printable, line by line"
    page.agent.setCurrentIndex(page.agent.findData(""))
    page.check()
    assert sai.asked[-1] == ("local", ("llm", "check", "lm"))
    page.models_button.click()
    assert sai.asked[-1] == ("local", models)
    assert page.models_title.text().startswith("Models of lm (local (this machine)): 2"), "in the page's table"
    cells = [[page.model_table.item(r, c).text() for c in range(page.model_table.columnCount())] for r in range(2)]
    assert cells == [["qwen2.5-coder:7b", "yes", "4.5 GB"], ["llama3.1:8b", "", ""]]
    args = parsed(sai, list(check))
    assert (args.command, args.name, args.agent) == ("check", "lm", "claude")
    args = parsed(sai, list(models))
    assert (args.command, args.name, args.json, args.sort) == ("models", "lm", True, "loaded")
    page.close()
    page.deleteLater()


def test_removing_asks_first_with_cancel_as_the_default(qapp, sai, monkeypatch):
    page = llm_page.LlmPage(sai)
    page.show()
    page.servers.selectRow(0)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    sai.asked.clear()
    page.remove_button.click()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Remove" and text.startswith("Forget the LLM server lm on local (this machine)?")
    assert "--llm lm" in text and sai.asked == [], "declined: nothing runs"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.remove()
    assert sai.asked[0] == ("local", ("llm", "remove", "lm")) and sai.asked[1] == ("local", ("llm", "list", "--json"))
    assert parsed(sai, ["llm", "remove", "lm"]).name == "lm"
    page.close()
    page.deleteLater()


def test_a_name_that_reads_as_an_option_is_never_passed_on(qapp, sai, monkeypatch):
    from PySide6.QtWidgets import QDialog
    sai.answers[("local", ("llm", "list", "--json"))] = (0, json.dumps([dict(LOCAL_SERVER, name="--replace")]), "")
    page = llm_page.LlmPage(sai)
    page.show()
    page.servers.selectRow(0)
    sai.asked.clear()
    for action in (page.check, page.models, page.remove):
        action()
        assert "Refused" in page.status.text()
    assert sai.asked == [] and widgets.SHOWN == [], "nothing ran, nothing was even asked"

    def run(dialog):
        dialog.name.setText("-x")
        dialog.url.setText("http://127.0.0.1:1234")
        dialog.secret.setText("LLM_KEY")
        dialog.value.setText(KEY)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.LlmDialog, "exec", run)
    page.add()
    assert sai.asked == [] and sai.secrets_set == [], "refused before the key is kept"
    assert widgets.SHOWN[-1][0] == "New LLM server" and "not '-'" in widgets.SHOWN[-1][1]
    page.close()
    page.deleteLater()
