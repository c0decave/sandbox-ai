"""The sampling parameters of an LLM server (offscreen): the LLM servers page's Parameters… dialog (sandbox-ai llm set,
the names, types and ranges from `llm params --json`) checks every value before it closes, saves a scope at once
(--reset and the full set), keeps a refusal in the dialog and is disabled for a sandbox-ai without `llm params`; the
board's LLM node keeps this board's own values (--llm-param KEY=VALUE) through the command line and the project
file, checks them and says when the launcher lacks the option; the loop guard's layers are edited per server (its own
over yours), per board (loop_guard.LAYER) and in the Settings (defaults.json "loop_guard"); the safeguard kind "loop"
has its titles. Every argv is parsed with sandbox-ai's own argparse."""
import argparse
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, llm_page, llm_params as LP, llm_params_view as LPV  # noqa: E402
from sandbox_ai_app import safeguards_view, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

SERVER = {"name": "lm", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "api_key_secret": None,
          "headers": {}, "ca": None, "vpn": None, "model": "qwen3:8b", "anthropic": False, "insecure_http": False,
          "models": ["qwen3:8b", "llama3.1:8b"], "checked": None, "agents": ["claude", "codex", "opencode"]}
VIEW = dict(SERVER, params={"*": {"temperature": 0.7}, "qwen3:8b": {"repeat_penalty": 1.1, "top_k": 40}},
            forced={"qwen3:8b": ["repeat_penalty"]}, contexts={"qwen3:8b": 32768}, evict="never",
            loop_guard={"enabled": True, "steps": 40, "compaction": True, "instructions": True, "sampling": True,
                        "context": True, "warn": 4, "block": 6, "text": 10, "watchdog_requests": 0,
                        "watchdog_minutes": 0},
            loop_guard_own={"warn": 4})


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


@pytest.fixture
def catalogue(homes):
    return LP.local_catalogue(backend.SandboxAI(), "lmstudio")


@pytest.fixture
def sai(homes, monkeypatch):
    """host_run records (host, args) and answers from sai.answers; background runs at once."""
    sai = backend.SandboxAI()
    sai.asked = []
    module = sai.module("llm")
    sai.answers = {("local", ("llm", "list", "--json")): (0, json.dumps([SERVER]), ""),
                   ("local", ("llm", "show", "lm", "--json")): (0, json.dumps(VIEW), ""),
                   ("local", ("llm", "params", "--kind", "lmstudio", "--json")):
                       (0, json.dumps(module.params_catalogue("lmstudio")), "")}
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}])

    def host_run(name, *args, timeout=120):
        if args[:2] != ("llm", "status"):
            sai.asked.append((name, args))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)

    def synchronous(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            done(None, exc)
            return
        done(result, None)
    monkeypatch.setattr(llm_page, "background", synchronous)
    return sai


# -- the checks and the argument lists --

def test_the_catalogue_is_sandbox_ais_and_each_value_is_checked_by_its_type_and_range(catalogue):
    entries = {e["name"]: e for e in catalogue}
    assert {"temperature", "top_k", "repeat_penalty", "max_tokens", "num_ctx"} <= set(entries)
    assert "min_p" not in entries, "a parameter LM Studio does not take is not offered for it"
    assert LP.check_value(entries["temperature"], "0.7") == 0.7
    assert LP.check_value(entries["top_k"], "40") == 40
    for key, text in (("temperature", "3"), ("temperature", "warm"), ("top_k", "4.5"), ("temperature", "nan"),
                      ("num_ctx", "100")):
        with pytest.raises(ValueError):
            LP.check_value(entries[key], text)
    spec = LP.loop_spec(backend.SandboxAI())
    assert spec is not None and "warn" in spec[0] and isinstance(spec[0]["sampling"], bool)
    assert LP.check_layer("sampling", "OFF", spec) is False and LP.check_layer("warn", "5", spec) == 5
    assert LP.check_layer("watchdog_minutes", "off", spec) == 0
    for key, text in (("warn", "1"), ("warn", "many"), ("sampling", "5"), ("nonsense", "on")):
        with pytest.raises(ValueError):
            LP.check_layer(key, text, spec)
    assert LP.loop_changes({"warn": 4, "text": 12}, {"warn": 5, "sampling": False}) == \
        ["warn=5", "sampling=off", "text=default"]
    with pytest.raises(ValueError):
        LP.parse_catalogue("not json")
    assert LP.parse_catalogue(json.dumps([{"name": "x y", "type": "float"}, {"name": "ok", "type": "int",
                                                                             "min": 0, "max": 5}]))[0]["name"] == "ok"


def test_set_argv_replaces_a_scope_at_once_and_fits_sandbox_ai_llm(sai, catalogue):
    argv = LP.set_argv("lm", "qwen3:8b", {"temperature": 0.5, "top_k": 40}, {"top_k"},
                       guard=["warn=5", "text=default"], evict="allow", catalogue=catalogue)
    assert argv == ["llm", "set", "lm", "--model", "qwen3:8b", "--reset", "--param", "temperature=0.5",
                    "--force-param", "top_k=40", "--loop-guard", "warn=5", "--loop-guard", "text=default",
                    "--evict", "allow"]
    args = parsed(sai, argv)
    assert (args.command, args.model, args.reset, args.param, args.force_param, args.loop_guard, args.evict) == \
        ("set", "qwen3:8b", True, ["temperature=0.5"], ["top_k=40"], ["warn=5", "text=default"], "allow")
    assert LP.set_argv("lm", LP.SCOPE_ALL, {}, set()) == ["llm", "set", "lm", "--reset"]
    with pytest.raises(ValueError):
        LP.set_argv("lm", "--evil", {}, set())
    with pytest.raises(ValueError):
        LP.set_argv("-x", LP.SCOPE_ALL, {}, set())


# -- the LLM servers page's Parameters… dialog --

def test_the_dialog_checks_before_it_closes_and_saves_the_scope_with_llm_set(qapp, sai, monkeypatch):
    shown = []

    def run(dialog):
        shown.append(dialog)
        fields = dialog.fields
        assert dialog.scope.currentData() == "qwen3:8b", "the model selected in the models table"
        assert fields.text("repeat_penalty") == "1.1" and fields.forced() == {"repeat_penalty"}
        assert fields.fields["temperature"].placeholderText() == "default: 0.7", "every model's value shown"
        assert "32768" in dialog.context.text()
        fields.fields["temperature"].setText("9")
        assert not fields.valid() and not dialog.buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled()
        dialog.accept()
        assert not dialog.saved and dialog.message.isVisibleTo(dialog), "not closed: the rule is said"
        fields.fields["temperature"].setText("0.4")
        fields.forces["temperature"].setChecked(True)
        fields.fields["top_k"].clear()
        assert dialog.guard.text("warn") == "4", "the server's own layer"
        assert dialog.guard.fields["block"].placeholderText() == "default: 6", "the others: what holds without it"
        dialog.guard.fields["warn"].setText("1")
        assert not dialog.validate(), "a layer out of its range: OK stays off"
        dialog.guard.fields["warn"].clear()
        dialog.guard.fields["text"].setText("20")
        dialog.evict.setCurrentIndex(dialog.evict.findData("allow"))
        dialog.accept()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(LPV.ParamsDialog, "exec", run)
    page = llm_page.LlmPage(sai)
    page.show()
    page.servers.selectRow(0)
    page.models_of, page.model_list = ("local", "lm"), [{"name": "qwen3:8b"}, {"name": "llama3.1:8b"}]
    page.show_models()
    page.model_table.selectRow(next(r for r in range(page.model_table.rowCount())
                                    if page.model_table.item(r, 0).text() == "qwen3:8b"))
    assert page.params_button.isEnabled()
    page.params_button.click()
    assert shown and shown[0].saved
    saved = [args for host, args in sai.asked if args[:2] == ("llm", "set")]
    assert saved == [("llm", "set", "lm", "--model", "qwen3:8b", "--reset", "--force-param", "temperature=0.4",
                      "--force-param", "repeat_penalty=1.1", "--loop-guard", "text=20", "--loop-guard",
                      "warn=default", "--evict", "allow")]
    parsed(sai, list(saved[0]))
    assert "saved" in page.status.text()
    page.close()
    page.deleteLater()


def test_a_refusal_keeps_the_dialog_open_and_another_scope_is_saved_too(qapp, sai, monkeypatch):
    answers = []

    def run(dialog):
        assert dialog.scope.currentData() == LP.SCOPE_ALL, "no model selected: every model's"
        dialog.fields.fields["temperature"].setText("0.2")
        dialog.scope.setCurrentIndex(dialog.scope.findData("llama3.1:8b"))
        dialog.fields.fields["max_tokens"].setText("4096")
        sai.answers[("local", ("llm", "set", "lm", "--reset", "--param", "temperature=0.2"))] = \
            (1, "", "sandbox-ai llm: --param temperature: no")
        dialog.accept()
        answers.append((dialog.saved, dialog.message.text()))
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(LPV.ParamsDialog, "exec", run)
    page = llm_page.LlmPage(sai)
    page.show()
    page.servers.selectRow(0)
    page.parameters()
    assert answers and answers[0][0] is False and "temperature: no" in answers[0][1]
    sets = [args for host, args in sai.asked if args[:2] == ("llm", "set")]
    assert sets == [("llm", "set", "lm", "--reset", "--param", "temperature=0.2")], "the first refusal stops"
    page.close()
    page.deleteLater()


def test_a_sandbox_ai_without_llm_params_disables_the_button(qapp, sai, monkeypatch):
    page = llm_page.LlmPage(sai)
    page.show()
    page.host_box.setCurrentIndex(page.host_box.findData("ct1"))
    sai.answers[("ct1", ("llm", "list", "--json"))] = (0, json.dumps([SERVER]), "")
    sai.answers[("ct1", ("llm", "show", "lm", "--json"))] = (0, json.dumps(SERVER), "")
    sai.answers[("ct1", ("llm", "params", "--kind", "lmstudio", "--json"))] = \
        (2, "", "sandbox-ai llm: error: argument command: invalid choice: 'params'")
    page.refresh()
    page.servers.selectRow(0)
    page.parameters()
    assert not page.params_button.isEnabled() and "update it there" in page.params_button.toolTip()
    page.close()
    page.deleteLater()


# -- the board's LLM node --

def test_a_boards_llm_params_round_trip_through_the_command_line_and_the_project_file(homes, tmp_path):
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "opencode", ["--workspace", str(tmp_path), "--llm", "lm:qwen3:8b", "--llm-param",
                                          "temperature=0.6", "--llm-param", "loop_guard=off", "--llm-param",
                                          "loop_guard.warn=4"])
    node = board.first("llm")
    assert node.props["params"] == {"temperature": "0.6", "loop_guard.enabled": "off", "loop_guard.warn": "4"}
    argv = B.to_argv(sai, board)
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--llm-param"] == \
        ["temperature=0.6", "loop_guard.enabled=off", "loop_guard.warn=4"]
    document, _main = B.project_document(sai, board)
    assert "temperature=0.6" in document["options"]
    again = B.from_project(sai, document, tmp_path)
    assert again.first("llm").props["params"] == node.props["params"]
    node.props["params"] = {}
    assert "--llm-param" not in B.to_argv(sai, board)
    lone = B.from_argv(sai, "claude", ["--llm-param", "temperature=0.6"])
    assert lone.extra == ["--llm-param", "temperature=0.6"], "without an LLM server node: kept as it was"


def test_a_boards_llm_param_that_is_not_valid_is_an_error_and_an_old_launcher_a_warning(homes, monkeypatch):
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "opencode", ["--llm", "lm:qwen3:8b"])
    node = board.first("llm")
    node.props["params"] = {"temperature": "7", "loop_guard.enabled": "on"}
    problems = [p for p in B.problems(sai, board) if p.node == node.id]
    assert any(p.level == "error" and "temperature" in p.text for p in problems), problems
    node.props["params"] = {"temperature": "0.7", "bogus": "1"}
    assert any(p.level == "error" and "bogus" in p.text for p in B.problems(sai, board) if p.node == node.id)
    node.props["params"] = {"loop_guard.warn": "1"}
    assert any(p.level == "error" and "loop_guard.warn" in p.text for p in B.problems(sai, board)
               if p.node == node.id)
    node.props["params"] = {"temperature": "0.7"}
    assert not [p for p in B.problems(sai, board) if p.node == node.id and p.level == "error"]
    monkeypatch.setattr(B, "launcher_knows", lambda sai_, option: option != "--llm-param")
    assert any(p.level == "warning" and "--llm-param" in p.text for p in B.problems(sai, board) if p.node == node.id)


def test_the_inspector_of_an_llm_node_edits_this_boards_params(qapp, homes, monkeypatch):
    (homes / "cwd").mkdir()
    monkeypatch.chdir(homes / "cwd")
    from sandbox_ai_app.board_view import BoardPage
    page = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    node = page.add_node("llm", **{"--llm": "lm:qwen3:8b"}) or page.board.first("llm")
    page.inspect(node)
    fields = page.inspector_holder.findChildren(LPV.ParamsFields)[0]
    layers = page.inspector_holder.findChildren(LPV.LoopFields)[0]
    assert not fields.forces, "this board's values are forced: no Force box"
    fields.fields["temperature"].setText("5")
    fields.commit()
    assert not page.board.node(node.id).props.get("params"), "nothing that is not valid is kept"
    fields.fields["temperature"].setText("0.3")
    fields.commit()
    assert page.board.node(node.id).props["params"] == {"temperature": "0.3"}
    layers.fields["warn"].setText("5")
    layers.commit()
    assert page.board.node(node.id).props["params"] == {"temperature": "0.3", "loop_guard.warn": "5"}
    fields.reset_button.click()
    layers.reset_button.click()
    assert not page.board.node(node.id).props.get("params")
    page.deleteLater()


def test_the_settings_keep_your_loop_guard_for_every_server(qapp, homes):
    from sandbox_ai_app.settings import SettingsDialog
    sai = backend.SandboxAI()
    dialog = SettingsDialog(sai)
    layers = dialog.fields["loop_guard"]
    assert layers.fields["warn"].placeholderText().startswith("default: "), "sandbox-ai's own shown"
    layers.fields["warn"].setText("1")
    layers.commit()
    assert "loop_guard" not in sai.sandbox_defaults()[0], "nothing that is not valid is saved"
    layers.fields["warn"].setText("4")
    layers.fields["sampling"].setCurrentIndex(layers.fields["sampling"].findData("off"))
    layers.commit()
    assert sai.sandbox_defaults()[0]["loop_guard"] == {"warn": 4, "sampling": False}
    assert sai.module("llm").user_loop() == {"warn": 4, "sampling": False}, "the file sandbox-ai reads"
    layers.reset_button.click()
    assert "loop_guard" not in sai.sandbox_defaults()[0]
    dialog.deleteLater()


def test_the_safeguard_kind_loop_has_its_titles():
    assert safeguards_view.kind_title("loop") == "loop guard intervened"
    assert "loop guard interventions" in safeguards_view.GROUPED["loop"]
