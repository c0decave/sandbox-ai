"""Hotfix 0.1.96: what the user asked for at the Board's configuration of an agent.

User: "Mir scheint, dass nicht in allen Terminals beim Config im Board abgefragt wird, ob Thinking angezeigt werden
soll, bitte hinzufügen." - every agent gets `--show-thinking on|off`, mapped to that agent's own switch as its pinned
release has it (checked in each release: Claude Code 2.1.322 showThinkingSummaries, Codex 0.169.0
hide_agent_reasoning and show_raw_agent_reasoning, OpenCode 1.19.5 thinking_mode in its kv.json, Hermes v0.22.5
display.show_reasoning, Goose 1.57.0 GOOSE_CLI_SHOW_THINKING, DSH sandbox-ai's own chat); Qwen Code 0.26.4 has none
(Ctrl+O unfolds its thinking), so the option is refused for it with that reason.

User: "Auch brauchen wir dort bei lokalen LLMs Advanced Settings wie KV-Cache, Temp, Context…" and "Advanced Settings
auch einbauen für die allgemeine LLM-Config im LLM-Server-Bereich." - what each kind of server really takes, per
request or at the model's load (Ollama's keep_alive on its OpenAI-compatible chat API, LM Studio's ttl per request and
flash_attention and offload_kv_cache_to_gpu at its REST load), the agents' wait for a slow server
(stream_idle_timeout), and what only the server's own start decides (KV cache type, context window), shown as
information.

Unit tests of each mapping, then the real ./sandbox-ai (tests/live_fakes.py: only Podman stood in for)."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests/devtools"))
import home_guard  # noqa: E402

home_guard.ensure()
from live_fakes import Host  # noqa: E402
import model_mock  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("sandbox_hotfix_board_llm", "sandbox/agent_sandbox.py")
llm = load("sandbox_llm_hotfix_board_llm", "sandbox/llm.py")
rt = load("sandbox_runtime_hotfix_board_llm", "sandbox/runtime.py")
chat = load("sandbox_dsh_chat_hotfix_board_llm", "sandbox/dsh_chat.py")
hermes = load("sandbox_hermes_hotfix_board_llm", "sandbox/hermes.py")
goose = load("sandbox_goose_hotfix_board_llm", "sandbox/goose.py")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def parsed(agent, *options, args=()):
    return s.parse_args([agent, "--workspace", "/tmp", *options, *(["--", *args] if args else [])])


# --- "ob Thinking angezeigt werden soll": every agent's own switch ---------------------------------------------------

def test_every_agent_has_a_thinking_switch_or_says_why_not():
    """Each of the launcher's agents is in exactly one of the two tables; the app's copy is the same."""
    assert set(s.AGENT_THINKING) | set(s.THINKING_NONE) == set(s.DANGER_ARGS)
    assert not set(s.AGENT_THINKING) & set(s.THINKING_NONE)
    sys.path.insert(0, str(ROOT / "projects/sandbox-ai-app"))
    from sandbox_ai_app import thinking as app  # (no Qt)
    assert {agent: default for agent, (default, _switch) in s.AGENT_THINKING.items()} == app.AGENT_DEFAULTS
    assert set(app.NO_SWITCH) == set(s.THINKING_NONE)


@pytest.mark.parametrize("value, shown", [("on", True), ("off", False)])
def test_claude_code_gets_show_thinking_summaries_for_this_run(value, shown):
    """Claude Code 2.1.322: the setting showThinkingSummaries, given with --settings JSON before your own arguments
    (your own --settings after it wins)."""
    command = s.agent_command(parsed("claude", "--show-thinking", value, args=["--verbose"]), "claude")
    index = command.index("--settings")
    assert json.loads(command[index + 1]) == {"showThinkingSummaries": shown}
    assert index < command.index("--verbose")


@pytest.mark.parametrize("value, hide, raw", [("on", "false", "true"), ("off", "true", "false")])
def test_codex_gets_hide_agent_reasoning_and_show_raw_agent_reasoning(value, hide, raw):
    command = s.agent_command(parsed("codex", "--show-thinking", value), "codex")
    pairs = [command[i + 1] for i, arg in enumerate(command) if arg == "-c"]
    assert f"hide_agent_reasoning={hide}" in pairs and f"show_raw_agent_reasoning={raw}" in pairs


def test_codex_refuses_the_switch_given_twice():
    with pytest.raises(ValueError, match="given twice"):
        s.validate(parsed("codex", "--show-thinking", "on", args=["-c", "hide_agent_reasoning=true"]))


@pytest.mark.parametrize("value, mode", [("on", "show"), ("off", "hide")])
def test_opencode_gets_thinking_mode_in_its_kv_json_and_keeps_the_rest(tmp_path, value, mode):
    """OpenCode 1.19.5 keeps the TUI's thinking_mode (show|hide, default hide) in XDG_STATE_HOME/opencode/kv.json."""
    home = tmp_path / "home"
    (home / ".local/state/opencode").mkdir(parents=True)
    (home / ".local/state/opencode/kv.json").write_text(json.dumps({"theme": "nord", "thinking_mode": "x"}))
    s.opencode_thinking(home, value == "on")
    assert json.loads((home / s.OPENCODE_KV).read_text()) == {"theme": "nord", "thinking_mode": mode}


@pytest.mark.parametrize("value, shown", [("on", True), ("off", False)])
def test_hermes_gets_display_show_reasoning_in_its_managed_configuration(value, shown):
    """Hermes v0.22.5 reads display.show_reasoning (default true) and the CLI's own key, which /reasoning saves and
    which wins over it: both pinned."""
    config = hermes.managed_config(parsed("hermes", "--show-thinking", value))
    assert config["display"]["show_reasoning"] is shown
    assert config["display"]["platforms"]["cli"]["show_reasoning"] is shown
    assert "display" not in hermes.managed_config(parsed("hermes"))


def test_goose_gets_its_variable_only_for_on_and_the_configuration_for_both():
    """Goose 1.57.0: any value of GOOSE_CLI_SHOW_THINKING turns it on, so off is the configuration's false and no
    variable at all."""
    on, off = parsed("goose", "--show-thinking", "on"), parsed("goose", "--show-thinking", "off")
    assert goose.environment(on)["GOOSE_CLI_SHOW_THINKING"] == "1"
    assert "GOOSE_CLI_SHOW_THINKING" not in goose.environment(off)
    assert goose.managed_config(on)["GOOSE_CLI_SHOW_THINKING"] is True
    assert goose.managed_config(off)["GOOSE_CLI_SHOW_THINKING"] is False
    assert "GOOSE_CLI_SHOW_THINKING" not in goose.managed_config(parsed("goose"))


def test_dsh_chat_gets_thinking_hide():
    command = s.agent_command(parsed("dsh", "--show-thinking", "off"), "dsh")
    assert command[command.index("--thinking") + 1] == "hide"
    assert "--thinking" not in s.agent_command(parsed("dsh"), "dsh")


def test_qwen_code_is_refused_with_what_it_offers_instead():
    with pytest.raises(ValueError, match=r"Qwen Code 0\.26\.4 has no setting for it.*Ctrl\+O"):
        s.validate(parsed("qwen", "--show-thinking", "on"))


def test_without_the_option_every_agent_keeps_its_own_default():
    for agent in ("claude", "codex"):
        command = s.agent_command(parsed(agent), agent)
        assert "--settings" not in command and not any("agent_reasoning" in arg for arg in command)


class Out:
    def __init__(self):
        self.lines = []

    def write(self, text, code=None):
        self.lines.append((text, code))

    def note(self, text):
        self.lines.append((text, "note"))


def test_the_dsh_chat_shows_thinking_dimmed_or_leaves_it_out():
    """sandbox-ai's chat for DSH: thinking dimmed by default, left out with --thinking hide."""
    event = json.dumps({"type": "thinking", "text": "pondering the files"}).encode() + b"\n"
    shown, hidden = Out(), Out()
    chat.Turn(shown).render(event)
    chat.Turn(hidden, thinking=False).render(event)
    assert shown.lines == [("  pondering the files", "2")] and hidden.lines == []
    assert chat.parse(["--thinking", "hide", "--", "dsh", "headless"])[0]["thinking"] == "hide"
    with pytest.raises(SystemExit):
        chat.parse(["--thinking", "maybe", "--", "dsh"])


def test_the_dsh_chat_switches_thinking_with_its_command(tmp_path, monkeypatch, capsys):
    """/thinking off and /thinking on in the chat: the next answers follow."""
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    lines = iter(["/thinking off", "/thinking", "/thinking on", "/exit"])
    monkeypatch.setattr(chat, "read_message", lambda out, history, path: next(lines))
    seen = []
    monkeypatch.setattr(chat.Chat, "send", lambda self, message, retry=True: seen.append(self.thinking))
    assert chat.main(["--", "dsh", "headless"]) == 0
    said = capsys.readouterr().out
    assert "thinking is hidden" in said and "thinking is shown" in said


# --- "Advanced Settings wie KV-Cache, Temp, Context": what each server really takes ----------------------------------

def test_each_kind_offers_only_what_its_server_takes():
    assert "keep_alive" in llm.kind_params("ollama") and "keep_alive" not in llm.kind_params("lmstudio")
    assert "ttl" in llm.kind_params("lmstudio") and "ttl" not in llm.kind_params("ollama")
    for key in ("flash_attention", "offload_kv_cache_to_gpu"):
        assert [k for k in llm.KINDS if key in llm.kind_params(k)] == ["lmstudio"]
    for kind in llm.KINDS:
        assert {"temperature", "num_ctx", "max_tokens", "stream_idle_timeout"} <= set(llm.kind_params(kind))
        assert llm.server_only(kind), kind  # what only the server's own start decides, said
    assert any("cache-type-k" in item["where"] for item in llm.server_only("llamacpp"))
    assert any("OLLAMA_KV_CACHE_TYPE" in item["where"] for item in llm.server_only("ollama"))
    assert any("kv-cache-dtype" in item["where"] for item in llm.server_only("vllm"))


@pytest.mark.parametrize("kind, text, match", [
    ("ollama", "keep_alive=forever", "a whole number"),
    ("ollama", "keep_alive=-2", "from -1 to"),
    ("lmstudio", "ttl=0", "from 1 to"),
    ("ollama", "ttl=60", "Ollama does not take it"),
    ("lmstudio", "flash_attention=maybe", "true or false"),
    ("vllm", "offload_kv_cache_to_gpu=true", "does not take it"),
    ("llamacpp", "stream_idle_timeout=5", "from 10 to 86400"),
])
def test_an_invalid_value_is_refused_with_a_clear_message(kind, text, match):
    with pytest.raises(ValueError, match=match):
        llm.parse_param(kind, text, "--llm-param")


def entry_for(kind, params):
    endpoint = {"kind": kind, "name": "x", "params": {"*": params}, "models": ["m"], "model": "m"}
    # the endpoint is not in a store: hand selection() its cached answer and offer the one model
    a = argparse.Namespace(llm_param=[], llm="x", llm_models="m", _llm=("x", (endpoint, "m")))
    return {**llm.proxy_params(a, endpoint, "m"), "name": "x"}


def test_ollama_keep_alive_goes_on_its_chat_requests_only():
    entry = entry_for("ollama", {"keep_alive": -1, "stream_idle_timeout": 900})
    chat_request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert rt.llm_apply_params(chat_request, entry, "chat") == {"keep_alive": -1}
    assert chat_request["keep_alive"] == -1 and "stream_idle_timeout" not in chat_request
    messages = {"model": "m", "messages": []}
    rt.llm_apply_params(messages, entry, "messages")
    assert "keep_alive" not in messages  # its Anthropic API has no keep_alive


def test_lm_studio_ttl_is_sent_and_its_load_settings_never_are():
    entry = entry_for("lmstudio", {"ttl": 600, "flash_attention": True, "offload_kv_cache_to_gpu": False,
                                   "num_ctx": 32768})
    request = {"model": "m", "messages": []}
    assert rt.llm_apply_params(request, entry, "chat") == {"ttl": 600}
    assert llm.lmstudio_load_body("m", 32768, {"flash_attention": True, "offload_kv_cache_to_gpu": False}) == {
        "model": "m", "context_length": 32768, "parallel": 1, "flash_attention": True,
        "offload_kv_cache_to_gpu": False}


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    assert status == 0, lines
    return "\n".join(lines)


def bodies(log, path):
    return [json.loads(line)["body"] for line in log.read_text().splitlines() if json.loads(line)["path"] == path]


def test_a_start_loads_lm_studio_s_model_with_its_load_settings(tmp_path, monkeypatch):
    """LM Studio-like server (its REST load refuses keys it does not know): the start's explicit load carries the
    context window, Flash Attention and the KV cache's place, the server's values with the start's over them."""
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "")
    log = tmp_path / "server.log"
    server, port = model_mock.start(models=("m",), lmstudio=True, log=str(log), log_bodies=True)
    try:
        run("add", "lms", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")
        run("set", "lms", "--param", "flash_attention=true", "--param", "offload_kv_cache_to_gpu=true")
        a = argparse.Namespace(llm_param=["num_ctx=16384", "offload_kv_cache_to_gpu=false"], run_id="r1")
        note = llm.keep_loaded(llm.endpoint("lms"), "m", a, runs={})
        assert note and "loaded m for this start" in note, note
        assert bodies(log, "/api/v1/models/load") == [{"model": "m", "context_length": 16384, "parallel": 1,
                                                        "flash_attention": True, "offload_kv_cache_to_gpu": False}]
    finally:
        server.shutdown()


def test_llm_load_of_ollama_takes_the_server_s_keep_alive(tmp_path, monkeypatch):
    log = tmp_path / "server.log"
    server, port = model_mock.start(models=("m",), ollama_ps=True, log=str(log), log_bodies=True)
    try:
        run("add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}")
        run("set", "ol", "--param", "keep_alive=1800")
        said = run("load", "ol", "m")
        assert "unloaded after 1800 s idle" in said
        assert bodies(log, "/api/generate")[-1]["keep_alive"] == 1800
    finally:
        server.shutdown()


def test_the_agents_wait_for_a_slow_server():
    """stream_idle_timeout: Claude Code's two waits, Codex's provider, OpenCode's provider - in milliseconds."""
    endpoint = {"name": "x", "kind": "llamacpp", "url": "http://127.0.0.1:8080",
                "parsed": llm.parse_url("http://127.0.0.1:8080"), "models": ["m"]}
    a = argparse.Namespace(llm_param=["stream_idle_timeout=1200"], env=[], secret_env=[])
    assert llm.agent_wait(a, endpoint, "m") == 1200
    assert llm.claude_wait_environment(a, endpoint, "m") == {"CLAUDE_STREAM_IDLE_TIMEOUT_MS": "1200000",
                                                             "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": "1200000"}
    assert "stream_idle_timeout_ms = 1200000" in llm.codex_profile(endpoint, "m", wait=1200)
    options = llm.opencode_config(endpoint, "m", wait=1200)["provider"][llm.provider_id(endpoint)]["options"]
    assert options["chunkTimeout"] == options["timeout"] == 1200000
    mine = argparse.Namespace(llm_param=["stream_idle_timeout=1200"], env=["CLAUDE_STREAM_IDLE_TIMEOUT_MS"],
                              secret_env=[])
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in llm.claude_wait_environment(mine, endpoint, "m")


def test_the_catalogue_says_where_each_setting_goes():
    found = {entry["name"]: entry for entry in llm.params_catalogue("lmstudio")}
    assert found["flash_attention"]["at_load"] == ["lmstudio"] and found["flash_attention"]["never_sent"] is False
    assert found["stream_idle_timeout"]["never_sent"] is True
    assert llm.where_sent("lmstudio", "flash_attention", []) == "at the model's load"


# --- the real ./sandbox-ai -------------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    return Host(tmp_path / "host")


@pytest.mark.parametrize("agent, default", [("claude", "off"), ("codex", "on"), ("opencode", "off"),
                                            ("hermes", "on"), ("dsh", "on"), ("goose", "off")])
def test_e2e_the_plan_shows_the_thinking_switch(host, agent, default):
    done = host.run(agent, "--show-thinking", "off", "--dry-run")
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)["show_thinking"]
    assert plan["value"] == "off" and plan["agent_default"] == default and plan["switch"]


def test_e2e_qwen_is_refused_with_the_reason(host):
    done = host.run("qwen", "--show-thinking", "on", "--dry-run")
    assert done.returncode != 0 and "Ctrl+O" in done.stderr, done.stderr


def kept_start(host, tmp_path, agent, *options):
    host.add_image(f"localhost/{agent}-sandbox:latest")
    kept = tmp_path / f"kept-{agent}"
    done = host.run(agent, "--no-tmux", "--no-auth", *options,
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
    assert done.returncode == 0, done.stderr
    return kept / "session"


def test_e2e_claude_and_codex_starts_carry_their_switches(host, tmp_path):
    session = kept_start(host, tmp_path, "claude", "--show-thinking", "on")
    command = json.loads((session / "config.json").read_text())["command"]
    assert json.loads(command[command.index("--settings") + 1]) == {"showThinkingSummaries": True}
    session = kept_start(host, tmp_path, "codex", "--show-thinking", "off")
    command = json.loads((session / "config.json").read_text())["command"]
    assert "hide_agent_reasoning=true" in command and "show_raw_agent_reasoning=false" in command


def test_e2e_hermes_goose_and_dsh_starts_carry_their_switches(host, tmp_path):
    session = kept_start(host, tmp_path, "hermes", "--show-thinking", "off")
    config = json.loads((session / "hermes/config.yaml").read_text())
    assert config["display"]["show_reasoning"] is False
    session = kept_start(host, tmp_path, "goose", "--show-thinking", "on")
    assert json.loads((session / "config.json").read_text())["env"]["GOOSE_CLI_SHOW_THINKING"] == "1"
    assert json.loads((session / "goose/config.yaml").read_text())["GOOSE_CLI_SHOW_THINKING"] is True
    session = kept_start(host, tmp_path, "dsh", "--show-thinking", "off")
    command = json.loads((session / "config.json").read_text())["command"]
    assert command[command.index("--thinking") + 1] == "hide"


def test_e2e_opencode_start_writes_thinking_mode_into_its_home(host, tmp_path):
    home = tmp_path / "oc-home"
    kept_start(host, tmp_path, "opencode", "--state-dir", str(home), "--show-thinking", "on")
    assert json.loads((home / ".local/state/opencode/kv.json").read_text())["thinking_mode"] == "show"


def test_e2e_the_plan_shows_the_effective_values_of_a_local_server(host):
    """A server's own values, the start's over them (the Board's), where each goes, and what only the server's start
    decides; an invalid value of the start is refused by name."""
    env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"}
    added = host.run("llm", "add", "lms", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--model", "m",
                     env=env)
    assert added.returncode == 0, added.stderr
    saved = host.run("llm", "set", "lms", "--param", "temperature=0.4", "--param", "flash_attention=true",
                     "--param", "ttl=900", env=env)
    assert saved.returncode == 0, saved.stderr
    done = host.run("codex", "--llm", "lms:m", "--llm-param", "temperature=0.7", "--llm-param",
                    "stream_idle_timeout=1200", "--llm-param", "num_ctx=32768", "--dry-run", env=env)
    assert done.returncode == 0, done.stderr
    params = json.loads(done.stdout)["llm"]["params"]
    assert params["values"] == {"temperature": 0.7, "flash_attention": True, "ttl": 900,
                                "stream_idle_timeout": 1200, "num_ctx": 32768}
    assert params["at_load"] == {"flash_attention": True, "num_ctx": 32768}
    assert params["agents_told"] == {"stream_idle_timeout": 1200, "num_ctx": 32768}
    assert any(item["setting"] == "KV cache quantization" for item in params["server_only"])
    refused = host.run("codex", "--llm", "lms:m", "--llm-param", "keep_alive=60", "--dry-run", env=env)
    assert refused.returncode != 0 and "--llm-param keep_alive: LM Studio does not take it" in refused.stderr


def test_e2e_llm_params_names_what_only_the_server_sets(host):
    done = host.run("llm", "params", "--kind", "llamacpp")
    assert done.returncode == 0, done.stderr
    assert "set on the server only: KV cache type: llama-server --cache-type-k" in done.stdout
