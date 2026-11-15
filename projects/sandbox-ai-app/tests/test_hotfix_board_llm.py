"""Hotfix 0.1.96, the app (offscreen): what the user asked for at the Board's configuration of an agent.

User: "Mir scheint, dass nicht in allen Terminals beim Config im Board abgefragt wird, ob Thinking angezeigt werden
soll, bitte hinzufügen." - the Board's agent part, its guide, the new-project wizard and the Start page offer "Show
thinking" for every agent with a switch for it (--show-thinking on|off, the agent's own default otherwise); Qwen Code
has none, so the choice is greyed out with the reason.

User: "Auch brauchen wir dort bei lokalen LLMs Advanced Settings wie KV-Cache, Temp, Context…" - the agent part with
a model server of yours folds out the model's advanced settings for this board's start (the LLM part's --llm-param
values), only what that kind of server takes, and what only the server's own start sets is shown.

User: "Advanced Settings auch einbauen für die allgemeine LLM-Config im LLM-Server-Bereich." - the LLM servers page's
Advanced settings… are the server's defaults, per kind, with the same information.

Servers of every kind are registered with the real sandbox-ai and answer as tests/devtools/model_mock.py does; the
board reads them in the background (nothing blocking on the GUI thread: checked); the real ./sandbox-ai checks what
the board passes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, guide, guide_view, llm_page, llm_params as LP  # noqa: E402
from sandbox_ai_app import llm_params_view as LPV, thinking, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
sys.path.insert(0, str(ROOT / "tests/devtools"))
import model_mock  # noqa: E402

AGENTS = ("claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose")
# What each kind of server is offered (some) and not offered, and what only its own start sets.
KINDS = {
    "ollama": ({"keep_alive", "min_p", "repeat_penalty"}, {"ttl", "flash_attention"}, "OLLAMA_KV_CACHE_TYPE"),
    "lmstudio": ({"ttl", "flash_attention", "offload_kv_cache_to_gpu", "top_k"}, {"keep_alive", "min_p"},
                 "KV cache quantization"),
    "llamacpp": ({"min_p", "dry_multiplier", "cache_prompt"}, {"ttl", "keep_alive", "flash_attention"},
                 "--cache-type-k"),
    "vllm": ({"repetition_penalty", "min_p"}, {"repeat_penalty", "ttl", "keep_alive"}, "--kv-cache-dtype"),
    "openai-compatible": ({"presence_penalty"}, {"min_p", "ttl", "keep_alive", "flash_attention"},
                          "the server's own settings"),
}
EVERY_KIND = {"temperature", "top_p", "max_tokens", "num_ctx", "stream_idle_timeout"}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def servers(homes):
    """A model server of every kind, registered with the real sandbox-ai (llm add), each answering as model_mock."""
    module = backend.SandboxAI().module("llm")
    running = []
    for kind in KINDS:
        server, port = model_mock.start(models=("m1",), lmstudio=kind == "lmstudio" or None,
                                        ollama_ps=kind == "ollama" or None)
        running.append(server)
        lines = []
        assert module.main(["add", kind.replace("-", ""), "--kind", kind, "--url", f"http://127.0.0.1:{port}",
                            "--model", "m1"], out=lines.append) == 0, lines
    yield {kind: kind.replace("-", "") for kind in KINDS}
    for server in running:
        server.shutdown()


class GuiThreadGuard:
    """Records a command run on the GUI thread (it would block the window)."""

    def __init__(self, sai, monkeypatch):
        self.blocked = []
        for name in ("call", "host_run", "run"):
            original = getattr(sai, name, None)
            if original is None:
                continue

            def guarded(*args, _original=original, _name=name, **kwargs):
                if threading.current_thread() is threading.main_thread():
                    self.blocked.append((_name, args[:4]))
                return _original(*args, **kwargs)
            monkeypatch.setattr(sai, name, guarded)


@pytest.fixture
def page(qapp, homes, monkeypatch):
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    view = BoardPage(sai, lambda *a, **kw: None, lambda: {"board_grid": 20})
    yield view
    view.deleteLater()


def dry_run(agent, argv, agent_args=()):
    """The real ./sandbox-ai's plan of ARGV (the board's command line)."""
    done = subprocess.run([str(ROOT / "sandbox-ai"), agent, *argv, "--dry-run",
                           *(["--", *agent_args] if agent_args else [])], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --- "ob Thinking angezeigt werden soll" ------------------------------------------------------------------------------

def test_the_agent_part_offers_show_thinking_for_every_agent_with_a_switch(page):
    """Every agent: the choice with its own default named; Qwen Code: greyed out with why."""
    agent = page.board.first("agent")
    for name in AGENTS:
        B.switch_agent(agent.props, name)
        page.inspect(agent)
        combo = page.thinking_combo
        values = [combo.itemData(i) for i in range(combo.count())]
        assert values == ["", "on", "off"], name
        if name == "qwen":
            assert not combo.isEnabled() and "Ctrl+O" in combo.toolTip()
            labels = [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)]
            assert any("Qwen Code has no setting for it" in text for text in labels)
        else:
            assert combo.isEnabled(), name
            default = thinking.AGENT_DEFAULTS[name]
            assert combo.itemText(0) == ("the agent's default (shown)" if default == "on" else
                                         "the agent's default (hidden)")


def test_show_thinking_goes_to_the_launcher_and_the_real_sandbox_ai_takes_it(page):
    agent = page.board.first("agent")
    B.switch_agent(agent.props, "codex")
    page.inspect(agent)
    page.thinking_combo.setCurrentIndex(page.thinking_combo.findData("off"))
    assert page.board.first("agent").props["thinking"] == "off"
    argv = B.to_argv(page.sai, page.board)
    assert argv[argv.index("--show-thinking") + 1] == "off"
    from sandbox_ai_app.board_view import summary
    assert "thinking hidden" in summary(page.board.first("agent"))
    plan = dry_run("codex", argv)
    assert plan["show_thinking"]["value"] == "off"
    again = B.from_argv(page.sai, "codex", argv)
    assert again.first("agent").props["thinking"] == "off", "back from the command line (a project file)"
    B.switch_agent(again.first("agent").props, "qwen")
    assert "thinking" not in again.first("agent").props, "Qwen Code takes none: dropped with the switch"
    assert "--show-thinking" not in B.to_argv(page.sai, again)


def test_the_guide_asks_show_thinking_and_follows_the_agent(qapp, homes):
    board = B.Board()
    node = board.add("agent", agent="hermes")
    dialog = guide_view.NodeGuide(node)
    _question, widget = dialog.inputs["thinking"]
    assert widget.isEnabled() and widget.itemText(0) == "the agent's default (shown)"
    widget.setCurrentIndex(widget.findData("off"))
    assert guide.props_of("agent", dialog.answers())["thinking"] == "off"
    agent_widget = dialog.inputs["agent"][1]
    agent_widget.setCurrentIndex(agent_widget.findData("qwen"))
    assert not widget.isEnabled() and widget.currentData() == "" and "Ctrl+O" in widget.toolTip()
    dialog.deleteLater()


def test_the_new_project_wizard_asks_it_too(qapp, homes):
    from sandbox_ai_app.wizard import SandboxWizard
    wizard = SandboxWizard(backend.SandboxAI(), lambda: {"default_agent": "goose", "default_template": "development"})
    wizard.model_page.agent_changed("goose")
    combo = wizard.model_page.thinking
    assert combo.isEnabled() and combo.itemText(0) == "the agent's default (hidden)"
    combo.setCurrentIndex(combo.findData("on"))
    wizard.model_page.agent_changed("goose")
    assert combo.currentData() == "on", "kept while the agent stays"
    wizard.model_page.agent_changed("qwen")
    assert not combo.isEnabled() and combo.currentData() == ""
    wizard.deleteLater()


def test_the_start_page_greys_it_out_for_qwen_code(qapp, homes, monkeypatch):
    from sandbox_ai_app import ui
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "host_llm_servers", lambda *a, **k: [])
    launch = ui.LaunchPage(sai, lambda *a, **k: None)
    widget = launch.form.widgets["--show-thinking"]
    launch.agent.setCurrentText("claude")
    assert widget.isEnabled() and "showThinkingSummaries" in widget.toolTip()
    widget.setCurrentText("on")
    assert launch.argv()[launch.argv().index("--show-thinking") + 1] == "on"
    launch.agent.setCurrentText("qwen")
    assert not widget.isEnabled() and "Ctrl+O" in widget.toolTip() and "--show-thinking" not in launch.argv()
    launch.deleteLater()


# --- "Advanced Settings wie KV-Cache, Temp, Context…" on the Board ---------------------------------------------------

def choose_server(page, wait, name):
    """NAME as the agent's model server, the servers read in the background as the board does."""
    page.inspect(page.board.first("agent"))
    assert wait(lambda: page.llm_servers is not None and not page.llm_loading, 30), "the servers were not read"
    page.choose_llm_server({"name": name, "model": "m1", "only": True})
    page.inspect(page.board.first("agent"))


@pytest.mark.parametrize("kind", list(KINDS))
def test_the_agent_part_with_a_model_server_offers_what_its_kind_takes(page, servers, wait, monkeypatch, kind):
    guard = GuiThreadGuard(page.sai, monkeypatch)
    choose_server(page, wait, servers[kind])
    assert page.agent_llm_button.isVisible() or not page.agent_llm_button.isChecked()
    page.agent_llm_button.setChecked(True)
    assert not page.agent_llm_box.isHidden()
    fields = page.agent_llm_box.findChildren(LPV.ParamsFields)[0]
    offered, refused, information = KINDS[kind]
    assert EVERY_KIND | offered <= set(fields.fields), (kind, sorted(fields.fields))
    assert not refused & set(fields.fields), (kind, sorted(refused & set(fields.fields)))
    texts = " ".join(w.text() for w in page.agent_llm_box.findChildren(QtWidgets.QLabel))
    assert information in texts and "Set on the server only" in texts
    assert "Temperature (temperature)" in texts and "Context length (num_ctx)" in texts
    assert not guard.blocked, f"a command on the GUI thread: {guard.blocked}"


def test_the_boards_values_override_the_server_and_the_plan_shows_them(page, servers, wait):
    """LM Studio: a server default (llm set), this board's values over it; the real launcher's plan shows the
    effective values and where each goes; a value that is not valid is never kept."""
    module = page.sai.module("llm")
    assert module.main(["set", servers["lmstudio"], "--param", "temperature=0.4", "--param", "ttl=900"],
                       out=lambda line: None) == 0
    choose_server(page, wait, servers["lmstudio"])
    page.agent_llm_button.setChecked(True)
    fields = page.agent_llm_box.findChildren(LPV.ParamsFields)[0]
    fields.fields["ttl"].setText("0")
    fields.commit()
    assert not (page.board.first("llm").props.get("params") or {}), "not valid: not kept"
    assert "Not valid" in fields.reasons["ttl"].text()
    fields.fields["ttl"].clear()
    fields.fields["temperature"].setText("0.7")
    fields.fields["num_ctx"].setText("32768")
    flash = fields.fields["flash_attention"]
    flash.setCurrentIndex(flash.findData("true"))
    fields.fields["stream_idle_timeout"].setText("1200")
    fields.commit()
    argv = B.to_argv(page.sai, page.board)
    given = [argv[i + 1] for i, a in enumerate(argv) if a == "--llm-param"]
    assert {"temperature=0.7", "num_ctx=32768", "flash_attention=true", "stream_idle_timeout=1200"} <= set(given)
    agent, _, agent_args = B.values(page.board, page.sai)
    params = dry_run(agent, argv, agent_args)["llm"]["params"]
    assert params["values"]["temperature"] == 0.7 and params["values"]["ttl"] == 900, "the board's over the server's"
    assert params["at_load"] == {"num_ctx": 32768, "flash_attention": True}
    assert params["agents_told"] == {"num_ctx": 32768, "stream_idle_timeout": 1200}


def test_the_llm_part_offers_the_same_for_its_server(page, servers, wait):
    choose_server(page, wait, servers["ollama"])
    page.inspect(page.board.first("llm"))
    fields = page.inspector_holder.findChildren(LPV.ParamsFields)[0]
    assert "keep_alive" in fields.fields and "ttl" not in fields.fields
    texts = " ".join(w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel))
    assert "OLLAMA_KV_CACHE_TYPE" in texts


# --- "Advanced Settings ... im LLM-Server-Bereich" -----------------------------------------------------------------

@pytest.fixture
def fake_sai(homes, monkeypatch):
    """host_run answers as a server of the kind under test would be shown (fake answers), in the background at once."""
    sai = backend.SandboxAI()
    sai.asked, sai.answers = [], {}
    monkeypatch.setattr(sai, "hosts_supported", lambda: False)

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


@pytest.mark.parametrize("kind", list(KINDS))
def test_the_llm_servers_page_offers_each_kinds_advanced_settings(qapp, fake_sai, monkeypatch, kind):
    module = fake_sai.module("llm")
    server = {"name": "srv", "kind": kind, "url": "http://192.168.1.20:8000", "api_key_secret": None, "headers": {},
              "ca": None, "vpn": None, "model": "m1", "anthropic": False, "insecure_http": False, "models": ["m1"],
              "checked": None, "agents": ["claude", "codex", "opencode"]}
    fake_sai.answers.update({
        ("local", ("llm", "list", "--json")): (0, json.dumps([server]), ""),
        ("local", ("llm", "show", "srv", "--json")): (0, json.dumps(dict(server, params={"*": {"temperature": 0.5}},
                                                                          forced={}, evict="never")), ""),
        ("local", ("llm", "params", "--kind", kind, "--json")): (0, json.dumps(module.params_catalogue(kind)), "")})
    seen = []

    def run(dialog):
        seen.append(dialog)
        offered, refused, information = KINDS[kind]
        assert EVERY_KIND | offered <= set(dialog.fields.fields) and not refused & set(dialog.fields.fields)
        assert dialog.server_only is not None
        texts = " ".join(w.text() for w in dialog.server_only.findChildren(QtWidgets.QLabel))
        assert information in texts
        assert dialog.windowTitle() == "Advanced settings of srv"
        dialog.fields.fields["stream_idle_timeout"].setText("900")
        dialog.accept()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(LPV.ParamsDialog, "exec", run)
    view = llm_page.LlmPage(fake_sai)
    view.show()
    view.servers.selectRow(0)
    assert view.params_button.text() == "Advanced settings…"
    view.params_button.click()
    assert seen and seen[0].saved
    saved = [args for _host, args in fake_sai.asked if args[:2] == ("llm", "set")]
    assert saved == [("llm", "set", "srv", "--reset", "--param", "temperature=0.5", "--param",
                      "stream_idle_timeout=900")]
    view.close()
    view.deleteLater()


def test_every_parameter_has_words_in_both_languages():
    """The form's captions: each parameter sandbox-ai knows in words (the name beside it)."""
    module = backend.SandboxAI().module("llm")
    assert set(module.PARAMS) <= set(LP.LABELS)
    assert LP.caption("num_ctx") == "Context length (num_ctx)"
