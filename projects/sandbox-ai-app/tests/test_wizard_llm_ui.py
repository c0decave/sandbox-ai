"""The wizard's Model step offscreen (Qt): where the model runs - the agent's provider, or a model server of your own
(the raw API of LM Studio, Ollama, llama.cpp, vLLM or an OpenAI-compatible server) for any agent, OpenCode included;
the registered servers and their models read in the background, a server added and checked for the agent, the
choice on the board as --llm NAME:MODEL (and --no-provider-defaults), the cloud model not used meanwhile; the preset
form's "Add a server…"."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, presets as PR, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

ENDPOINTS = [{"name": "lm", "kind": "lmstudio", "url": "http://127.0.0.1:1234", "model": None, "vpn": None,
              "models": [], "anthropic": False, "agents": ["claude", "codex", "opencode"]},
             {"name": "gw", "kind": "openai-compatible", "url": "https://llm.example.org", "model": None, "vpn": None,
              "models": [], "anthropic": False, "agents": ["codex", "opencode"]}]
MODELS = [{"name": "qwen2.5-coder-7b", "size": 4_700_000_000, "loaded": True, "details": "Q4_K_M"},
          {"name": "llama3.1-8b", "size": None, "loaded": False, "details": ""}]


class Calls:
    """sai.call, faked: `llm list`, `llm models`, `llm add` and `llm check` answered from REPLIES; every call kept."""

    def __init__(self, replies=None):
        self.log = []
        self.replies = {("llm", "list"): (0, json.dumps(ENDPOINTS), ""),
                        ("llm", "models"): (0, json.dumps(MODELS), ""), **(replies or {})}

    def __call__(self, *args, timeout=120, input=None):
        self.log.append(args)
        for length in (3, 2):
            if tuple(args[:length]) in self.replies:
                return self.replies[tuple(args[:length])]
        return 0, "", ""


@pytest.fixture
def wizard(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "project").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.wizard import SandboxWizard
    w = SandboxWizard(backend.SandboxAI(), lambda: {"default_agent": "claude", "default_template": "development"})
    w.calls = Calls()
    monkeypatch.setattr(w.sai, "call", w.calls)
    w.folder_page.path.edit.setText(str(tmp_path / "project"))
    assert w.folder_page.validatePage()
    yield w
    w.deleteLater()


def commands(wizard):
    """The calls that add a server, keep its key and check it (the wizard may list folders meanwhile)."""
    return [c for c in wizard.calls.log if c[:2] in (("llm", "add"), ("llm", "check"), ("secrets", "set"))]


def to_model_page(wizard, agent, wait):
    wizard.agent_page.agent.setCurrentText(agent)
    wizard.agent_page.initializePage()
    assert wizard.agent_page.validatePage()
    page = wizard.model_page
    page.initializePage()
    assert wait(lambda: page.loaded and page.servers.rowCount() == 2, 10), "the servers are read in the background"
    return page


def test_opencode_with_an_own_server_and_its_model_on_the_board(wizard, wait):
    """The user's request: OpenCode with a local model. The servers and their models come from sandbox-ai llm; the
    choice is --llm lm:MODEL with --no-provider-defaults, the cloud model is not used."""
    page = to_model_page(wizard, "opencode", wait)
    assert page.cloud.isChecked() and page.model.isEnabled() and not page.servers.isEnabled()
    assert "opencode" in page.servers.item(1, 3).text(), "OpenCode takes any OpenAI-compatible server"
    page.model.setEditText("anthropic/claude-opus-5-5")  # a cloud model chosen first
    page.own.setChecked(True)
    assert not page.model.isEnabled() and not page.effort.isEnabled() and page.servers.isEnabled()
    assert "--llm" in page.model.toolTip()
    assert not page.validatePage() and "Choose a model server" in page.status.text()
    widgets.select_keys(page.servers, ["lm"])
    assert wait(lambda: "lm" in page.listed and page.server_model.count() == 2, 10), "its models, asked for at once"
    assert ("llm", "models", "lm", "--json") in wizard.calls.log
    assert page.server_model_value() == "qwen2.5-coder-7b" and "loaded" in page.server_model.currentText()
    page.server_model.setCurrentIndex(page.server_model.findData("llama3.1-8b"))
    assert page.only.isChecked() and "opencode can use lm" in page.hint.text()
    assert page.validatePage()
    assert wizard.llm_choice == {"name": "lm", "model": "llama3.1-8b", "only": True}
    start = wizard.start_page
    start.initializePage()
    start.buttons["custom"].setChecked(True)
    assert start.validatePage()
    argv = B.to_argv(wizard.sai, wizard.board)
    assert B.values(wizard.board)[0] == "opencode"
    assert argv[argv.index("--llm") + 1] == "lm:llama3.1-8b" and "--no-provider-defaults" in argv
    assert "--model" not in argv, "the launcher refuses --model with --llm"


def test_a_typed_model_and_the_cloud_with_its_provider_too(wizard, wait):
    page = to_model_page(wizard, "codex", wait)
    page.own.setChecked(True)
    widgets.select_keys(page.servers, ["gw"])
    assert wait(lambda: "gw" in page.listed, 10)
    page.server_model.setEditText("gpt-oss-20b")
    page.only.setChecked(False)
    assert page.validatePage() and wizard.llm_choice == {"name": "gw", "model": "gpt-oss-20b", "only": False}
    page.cloud.setChecked(True)
    assert page.model.isEnabled() and not page.server_model.isEnabled()
    assert page.validatePage() and wizard.llm_choice is None, "back to the agent's provider"


def test_claude_is_told_why_a_server_cannot_serve_it(wizard, wait):
    page = to_model_page(wizard, "claude", wait)
    page.own.setChecked(True)
    widgets.select_keys(page.servers, ["gw"])
    assert "Anthropic Messages API" in page.hint.text()
    assert not page.validatePage() and "Anthropic Messages API" in page.status.text()
    widgets.select_keys(page.servers, ["lm"])
    assert wait(lambda: "lm" in page.listed, 10)
    assert page.validatePage() and wizard.llm_choice["name"] == "lm"


def test_a_server_that_does_not_answer_leaves_the_model_to_type(wizard, wait):
    wizard.calls.replies[("llm", "models")] = (1, "", "sandbox-ai llm: lm: connection refused")
    page = to_model_page(wizard, "opencode", wait)
    page.own.setChecked(True)
    widgets.select_keys(page.servers, ["lm"])
    assert wait(lambda: "connection refused" in page.status.text(), 10), "the CLI's error is shown"
    assert not page.validatePage() and "Choose or type a model" in page.status.text()
    page.server_model.setEditText("qwen2.5-coder-7b")
    assert page.validatePage() and wizard.llm_choice["model"] == "qwen2.5-coder-7b"


def test_adding_a_server_adds_keeps_the_key_then_checks_it_for_the_agent(wizard, wait, monkeypatch):
    from sandbox_ai_app.wizard import add_server
    sai = wizard.sai
    wizard.calls.replies[("llm", "check")] = (1, "lm: no answer at http://127.0.0.1:1234 (connection refused)", "")
    monkeypatch.setattr(sai, "secret_exists", lambda name, host="local": False)
    monkeypatch.setattr(sai, "set_secret", lambda name, value, host="local":
                        wizard.calls.log.append(("secrets", "set", name, value)) or (0, "", ""))
    done = []
    fields = {"name": "lm", "kind": "lmstudio", "url": "http://127.0.0.1:1234", "secret": "LLM_KEY", "vpn": "",
              "anthropic": False}
    add_server(wizard.model_page, sai, "opencode", [], lambda name, ok: done.append((name, ok)), fields=fields,
               value="sk-local-0000")
    assert wait(lambda: done, 10)
    assert commands(wizard) == [("llm", "add", "lm", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234",
                                 "--api-key-secret", "LLM_KEY"), ("secrets", "set", "LLM_KEY", "sk-local-0000"),
                                ("llm", "check", "lm", "--agent", "opencode")]
    assert done == [("lm", False)], "added, but the check found problems"
    assert any(title == "Check of lm" and "connection refused" in text for title, text, _ in widgets.SHOWN)
    wizard.calls.log.clear()
    wizard.calls.replies[("llm", "add")] = (1, "", "sandbox-ai llm: an endpoint lm exists (--replace)")
    add_server(wizard.model_page, sai, "opencode", [], lambda name, ok: done.append((name, ok)), fields=fields)
    assert wait(lambda: len(done) == 2, 10)
    assert done[1] == (None, False) and len(commands(wizard)) == 1, "refused: no key kept, nothing checked"
    assert "exists" in widgets.SHOWN[-1][1]


def test_the_page_lists_and_chooses_a_server_it_added(wizard, wait, monkeypatch):
    from sandbox_ai_app import wizard as wizard_module
    page = to_model_page(wizard, "opencode", wait)
    page.own.setChecked(True)
    added = ENDPOINTS + [{"name": "ollama", "kind": "ollama", "url": "http://127.0.0.1:11434", "models": ["qwen3"],
                          "agents": ["claude", "codex", "opencode"]}]

    def fake_add(parent, sai, agent, taken, done, source=None):
        assert agent == "opencode" and taken == ["lm", "gw"]
        wizard.calls.replies[("llm", "list")] = (0, json.dumps(added), "")
        done("ollama", True)
    monkeypatch.setattr(wizard_module, "add_server", fake_add)
    page.add_button.click()
    assert wait(lambda: page.chosen_name() == "ollama", 10), "the new server is chosen"
    assert wait(lambda: "ollama" in page.listed and page.server_model.count() == 2, 10), "its models, asked for"


def test_the_add_dialog_prefills_this_machine_with_the_kinds_port(qapp, wizard, wait, monkeypatch):
    from PySide6.QtTest import QTest
    from sandbox_ai_app.wizard import ServerDialog
    monkeypatch.setattr(wizard.sai, "vpn_profiles", lambda: [{"profile": "home"}, {"profile": "--bad"}])
    dialog = ServerDialog(wizard.sai, ["lmstudio"])
    assert dialog.kind.currentData() == "lmstudio" and dialog.url.text() == "http://127.0.0.1:1234"
    assert dialog.name.text() == "lmstudio-2", "a name not taken"
    assert wait(lambda: dialog.vpn.count() == 2, 10) and dialog.vpn.itemText(1) == "home"
    kinds = [dialog.kind.itemData(i) for i in range(dialog.kind.count())]
    assert kinds == ["lmstudio", "ollama", "llamacpp", "vllm", "openai-compatible"]
    dialog.kind.setCurrentIndex(kinds.index("ollama"))
    assert dialog.url.text() == "http://127.0.0.1:11434" and dialog.name.text() == "ollama"
    assert not dialog.anthropic.isVisibleTo(dialog)
    dialog.kind.setCurrentIndex(kinds.index("vllm"))
    assert dialog.url.text() == "http://127.0.0.1:8000"
    dialog.url.clear()
    QTest.keyClicks(dialog.url, "http://192.168.1.20:8000")
    dialog.kind.setCurrentIndex(kinds.index("openai-compatible"))
    assert dialog.url.text() == "http://192.168.1.20:8000", "a typed URL stays"
    assert dialog.anthropic.isVisibleTo(dialog)
    dialog.name.setText("-x")
    dialog.accept()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted.value and dialog.note.isVisibleTo(dialog)
    dialog.name.setText("gpu")
    dialog.value.setText("sk-typed")
    dialog.accept()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted.value, "a typed key needs a secret's name"
    dialog.secret.setText("GPU_KEY")
    dialog.vpn.setCurrentText("home")
    dialog.accept()
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted.value
    fields, value = dialog.request()
    assert fields == {"name": "gpu", "kind": "openai-compatible", "url": "http://192.168.1.20:8000",
                      "secret": "GPU_KEY", "vpn": "home", "anthropic": False} and value == "sk-typed"
    dialog.deleteLater()


def test_the_add_dialog_finds_servers_and_takes_the_one_chosen(qapp, wizard, wait, monkeypatch):
    """Find servers: `sandbox-ai llm detect --json` at this machine's loopback and the addresses typed (in the
    background); the one chosen fills the kind, the URL and a name not taken; a typed name stays."""
    from sandbox_ai_app.wizard import ServerDialog
    monkeypatch.setattr(wizard.sai, "vpn_profiles", lambda: [])
    found = [{"kind": "ollama", "host": "127.0.0.1", "port": 11434, "url": "http://127.0.0.1:11434",
              "models": [{"name": "qwen2.5-coder:7b"}]},
             {"kind": "vllm", "host": "192.168.1.20", "port": 8000, "url": "http://192.168.1.20:8000", "models": []}]
    wizard.calls.replies[("llm", "detect")] = (0, json.dumps(found), "")
    dialog = ServerDialog(wizard.sai, ["ollama"])
    assert not dialog.found.isVisibleTo(dialog)
    dialog.addresses.setText("127.0.0.1, llm.example.org")
    dialog.find()
    assert dialog.note.isVisibleTo(dialog) and "IP address" in dialog.note.text()
    assert not [c for c in wizard.calls.log if c[:2] == ("llm", "detect")], "nothing runs for a name"
    dialog.addresses.setText("127.0.0.1, 192.168.1.20")
    dialog.find()
    assert wait(lambda: dialog.found.count() == 2, 10)
    assert ("llm", "detect", "--json", "--address", "192.168.1.20") in wizard.calls.log
    assert dialog.kind.currentData() == "ollama" and dialog.url.text() == "http://127.0.0.1:11434"
    assert dialog.name.text() == "ollama-2", "a name not taken"
    dialog.name.setText("lan-gpu")
    dialog.name_typed = True
    dialog.found.setCurrentIndex(1)
    dialog.take_found()
    assert dialog.kind.currentData() == "vllm" and dialog.url.text() == "http://192.168.1.20:8000"
    assert dialog.name.text() == "lan-gpu"
    wizard.calls.replies[("llm", "detect")] = (0, "[]", "")
    dialog.find()
    assert wait(lambda: "No LLM server answered" in dialog.note.text(), 10)
    assert not dialog.found.isVisibleTo(dialog)
    dialog.deleteLater()


def test_a_preset_takes_the_server_of_the_model_page(wizard, wait):
    """local-llm with the Model page's server: nothing left to ask, --llm NAME:MODEL; with the agent's provider
    chosen instead, the placeholder stays and no cloud model is passed along with it."""
    wizard.agent_page.agent.setCurrentText("opencode")
    wizard.llm_choice = {"name": "lm", "model": "qwen2.5-coder-7b", "only": True}
    wizard.llm_endpoint = ENDPOINTS[0]
    start = wizard.start_page
    start.initializePage()
    assert start.select_preset("local-llm")
    assert start.validatePage()
    argv = B.to_argv(wizard.sai, wizard.board)
    assert argv[argv.index("--llm") + 1] == "lm:qwen2.5-coder-7b" and "--model" not in argv
    assert B.values(wizard.board)[0] == "opencode"
    wizard.llm_choice = wizard.llm_endpoint = None
    wizard.model_page.agent_changed("opencode")
    wizard.model_page.model.setEditText("anthropic/claude-opus-5-5")
    assert start.validatePage()
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "<llm:local>" in argv and "--model" not in argv


def test_the_preset_form_offers_servers_with_their_models_and_adds_one(wizard, wait, monkeypatch):
    from sandbox_ai_app import wizard as wizard_module
    from sandbox_ai_app.preset_gallery import PlaceholderForm
    listed = [{**ENDPOINTS[0], "models": ["qwen2.5-coder-7b", "llama3.1-8b"]}]
    monkeypatch.setattr(wizard.sai, "llm_endpoints", lambda: listed)
    form = PlaceholderForm(wizard.sai, PR.find("local-llm"), agent="opencode")
    combo = form.fields["<llm:local>"]
    assert wait(lambda: combo.count() == 3, 10)
    # Several models and no default model: the launcher refuses a bare NAME, so only NAME:MODEL is offered, and none
    # is chosen for the human (which model is theirs to say).
    assert [combo.itemText(i) for i in range(3)] == ["", "lm:qwen2.5-coder-7b", "lm:llama3.1-8b"]
    assert combo.currentText() == "", "no bare NAME preselected"
    buttons = [b for b in form.findChildren(QtWidgets.QPushButton) if b.text() == "Add a server…"]
    assert len(buttons) == 1
    seen = []

    def fake_add(parent, sai, agent, taken, done, source=None):
        seen.append((agent, taken))
        listed.append({"name": "box", "kind": "ollama", "url": "http://192.168.1.5:11434", "models": []})
        done("box", True)
    monkeypatch.setattr(wizard_module, "add_server", fake_add)
    buttons[0].click()
    assert wait(lambda: combo.currentText() == "box", 10) and seen == [("opencode", ["lm"])]
    assert form.values() == {"<llm:local>": "box"}
    mixed = PlaceholderForm(wizard.sai, PR.find("mixed-llm"), agent="claude")
    assert mixed.agent == "opencode", "the preset's own agent"
