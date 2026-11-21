"""With an Ollama --llm every agent apart from Hermes was told the model's own maximum window (/api/show's model_info
<arch>.context_length, 131072 here), the model profile's num_ctx, or your num_ctx - but Ollama serves an
OpenAI-compatible request with the window it loaded the model with: its Modelfile's num_ctx, else
OLLAMA_CONTEXT_LENGTH (4096 by default); that API takes no num_ctx, and sandbox-ai's start sends none. The agents
planned their compaction for a window Ollama did not serve, and Ollama cut the prompt without a word.

Now every agent that takes a window is told what Ollama serves (llm.ollama_served_context, as Hermes was:
llm.start_context): /api/ps of a loaded model, else the Modelfile's num_ctx, else the model loaded as the agent's
first request would load it and /api/ps read - a larger window of yours, the profile's or the model's own gives way
to it, a smaller one of yours stays. What cannot be learnt (an old Ollama, a server that does not answer, behind a
VPN, a dry run) leaves the window as before. Against the Ollama-like mock server (tests/devtools/model_mock.py), and
end to end through the real ./sandbox-ai (tests/live_fakes.py)."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402

SPEC = importlib.util.spec_from_file_location("sandbox_llm_ollama_served", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
MODEL = "qwen2.5-coder:7b"
OTHER = "llama3.1:8b"
PROFILED = "qwen3-coder-next:latest"  # the qwen3-coder-next profile: num_ctx 131072


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    monkeypatch.setattr(llm, "running_runs", lambda: {})


@pytest.fixture
def ollama(tmp_path):
    started = []

    def serve(models=(MODEL,), **options):
        log = tmp_path / f"requests{len(started)}.jsonl"
        server, port = model_mock.start(models=models, ollama_ps=True, log=str(log), log_bodies=True, **options)
        started.append(server)
        lines = []
        assert llm.main(["add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}", "--model", models[0],
                         "--replace"], out=lines.append) == 0, lines
        return log
    yield serve
    for server in started:
        server.shutdown()


def requests(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def loads(log):
    return [r["body"]["model"] for r in requests(log) if r["path"] == "/api/generate"]


def start(agent, model=MODEL, *params, **options):
    defaults = {"llm": f"ol:{model}", "agent": agent, "env": [], "secret_env": [], "llm_param": list(params),
                "vpn": None, "no_provider_defaults": True, "llm_models": None, "offline": False, "no_inspect": [],
                "agent_args": [], "local_config": False, "dangerously_skip_permissions": False}
    return argparse.Namespace(**{**defaults, **options})


def written_home(a):
    written = {}
    llm.prepare_home(a, Path("/h"), lambda data, home, relative: written.update({relative: data.decode()}), None, None)
    return written


def told(agent, a):
    """The window AGENT is told for the start's model, read from the configuration sandbox-ai writes for it."""
    model = llm.selection(a)[1]
    if agent == "claude":
        env = llm.agent_environment(a)
        assert env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
        return int(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"]) if "CLAUDE_CODE_MAX_CONTEXT_TOKENS" in env else None
    if agent == "codex":
        text = written_home(a)[f".codex/{llm.CODEX_PROFILE}.config.toml"]
        found = [line for line in text.splitlines() if line.startswith("model_context_window = ")]
        return int(found[0].split(" = ")[1]) if found else None
    if agent == "opencode":
        config = json.loads(written_home(a)[llm.OPENCODE_CONFIG])
        return next(iter(config["provider"].values()))["models"][model].get("limit", {}).get("context")
    if agent == "opencode2":
        config = json.loads(llm.opencode2_files(a)["sandbox-ai-llm.json"])
        return next(iter(config["provider"].values()))["models"][model].get("limit", {}).get("context")
    if agent == "qwen":
        entry = llm.qwen_settings(a)["modelProviders"]["openai"][0]
        return entry["generationConfig"].get("contextWindowSize")
    if agent == "goose":
        value = llm.goose_environment(a, *llm.selection(a)).get("GOOSE_CONTEXT_LIMIT")
        return int(value) if value else None
    if agent == "dsh":
        return llm.dsh_config(a)["route"]["models"][0].get("contextWindow")
    if agent == "pi":
        return llm.pi_llm(a)["provider"]["config"]["models"][0].get("contextWindow")
    if agent == "openhands":
        return llm.openhands_llm(a).get("max_input_tokens")
    if agent == "nanobot":
        return llm.nanobot_config(a)["agents"]["defaults"].get("contextWindowTokens")
    if agent == "hermes":
        return llm.hermes_config(a)["model"].get("context_length")
    raise AssertionError(agent)


AGENTS = ("claude", "codex", "opencode", "opencode2", "qwen", "goose", "dsh", "pi", "openhands", "nanobot")


@pytest.mark.parametrize("agent", AGENTS)
def test_regression_each_agent_is_told_the_window_ollama_serves_not_the_models_own(ollama, agent):
    """OLLAMA_CONTEXT_LENGTH=32768 on the server, the model not loaded, no Modelfile num_ctx: before, the agent was
    told 131072 (the model's own) and Ollama cut every prompt beyond 32768 tokens."""
    log = ollama(default_context=32768)
    assert told(agent, start(agent)) == 32768
    assert loads(log) == [MODEL], "loaded once, as the agent's first request would load it"
    assert requests(log)[[r["path"] for r in requests(log)].index("/api/generate")]["body"] == \
        {"model": MODEL, "stream": False}


@pytest.mark.parametrize("agent", AGENTS)
def test_ollamas_default_window_is_told_and_said(ollama, agent, capsys):
    ollama()
    assert told(agent, start(agent)) == 4096
    err = capsys.readouterr().err
    assert "Ollama serves" in err and "4096" in err and "OLLAMA_CONTEXT_LENGTH" in err, err


@pytest.mark.parametrize("agent", ("opencode", "qwen", "claude"))
def test_a_loaded_model_is_read_not_loaded_again(ollama, agent):
    log = ollama(running={MODEL: {"context": 16384, "expires_at": "2318-01-01T00:00:00Z"}})
    assert told(agent, start(agent)) == 16384
    assert loads(log) == []


@pytest.mark.parametrize("agent", ("opencode", "goose"))
def test_a_modelfiles_num_ctx_is_told_and_nothing_is_loaded(ollama, agent):
    log = ollama(modelfile_context=65536)
    assert told(agent, start(agent)) == 65536
    assert loads(log) == []


@pytest.mark.parametrize("agent", ("opencode", "codex", "claude"))
def test_your_larger_num_ctx_gives_way_to_what_ollama_serves(ollama, agent):
    """--llm-param num_ctx is never sent to Ollama (its OpenAI-compatible API takes none)."""
    ollama(default_context=32768)
    assert told(agent, start(agent, MODEL, "num_ctx=131072")) == 32768


@pytest.mark.parametrize("agent", ("opencode", "qwen"))
def test_your_smaller_num_ctx_stays(ollama, agent):
    ollama(default_context=65536)
    assert told(agent, start(agent, MODEL, "num_ctx=16384")) == 16384


@pytest.mark.parametrize("agent", ("opencode", "codex", "claude"))
def test_the_model_profiles_window_gives_way_to_what_ollama_serves(ollama, agent):
    """The qwen3-coder-next profile sets num_ctx 131072; Ollama serves 32768."""
    ollama(models=(PROFILED,), default_context=32768)
    a = start(agent, PROFILED)
    assert llm.profile_params(llm.selection(a)[0], PROFILED).get("num_ctx") == 131072
    assert told(agent, a) == 32768


def test_the_servers_other_models_are_not_told_more_than_ollamas_default(ollama):
    """The other models the agent offers (OpenCode's /models): not loaded (only the start's model is), each told at
    most the window Ollama loaded the start's model with by default - the one it loads them with too."""
    log = ollama(models=(MODEL, OTHER), default_context=32768)
    config = json.loads(written_home(start("opencode"))[llm.OPENCODE_CONFIG])
    models = next(iter(config["provider"].values()))["models"]
    assert models[MODEL]["limit"]["context"] == 32768 and models[OTHER]["limit"]["context"] == 32768
    assert loads(log) == [MODEL]


def test_another_model_loaded_with_its_own_window_is_told_that(ollama):
    log = ollama(models=(MODEL, OTHER), default_context=32768,
                 running={OTHER: {"context": 8192, "expires_at": "2318-01-01T00:00:00Z"}})
    contexts = llm.qwen_settings(start("qwen"))["modelProviders"]["openai"]
    assert [e["generationConfig"]["contextWindowSize"] for e in contexts] == [32768, 8192]
    assert loads(log) == [MODEL]


def test_what_cannot_be_learnt_leaves_the_window_as_before(ollama, monkeypatch):
    ollama(default_context=32768)
    monkeypatch.setattr(llm, "ollama_served_context", lambda *args, **kw: (None, None))
    assert told("opencode", start("opencode")) == 131072
    assert told("claude", start("claude")) == 131072


def test_behind_a_vpn_nothing_is_asked_nor_loaded(ollama):
    log = ollama(default_context=32768)
    a = start("opencode", vpn="tor")
    assert llm.served_context(a, *llm.selection(a)) == (None, None)
    assert loads(log) == []


def test_the_served_window_is_learnt_once_per_start(ollama):
    log = ollama(default_context=32768)
    a = start("qwen")
    llm.qwen_settings(a)
    llm.agent_environment(a)
    shown = [r for r in requests(log) if r["path"] in ("/api/show", "/api/generate")]
    assert llm.served_context(a, *llm.selection(a)) == (32768, "loaded now")
    assert [r for r in requests(log) if r["path"] in ("/api/show", "/api/generate")] == shown


def test_hermes_keeps_its_check(ollama):
    ollama(default_context=65536)
    assert told("hermes", start("hermes")) == 65536


def test_another_kind_is_never_asked(monkeypatch):
    endpoint = {"name": "x", "kind": "lmstudio", "url": "http://127.0.0.1:9", "parsed": llm.parse_url(
        "http://127.0.0.1:9")}
    assert llm.served_context(start("opencode"), endpoint, MODEL) == (None, None)


def test_a_tagless_model_name_matches_ollamas_latest(ollama):
    """Ollama lists `llama3` as llama3:latest in /api/ps."""
    log = ollama(models=("llama3:latest",), running={"llama3:latest": {"context": 8192,
                                                                         "expires_at": "2318-01-01T00:00:00Z"}})
    assert llm.ollama_served_context(llm.endpoint("ol"), "llama3") == (8192, "loaded")
    assert loads(log) == []


# --- end to end: the real ./sandbox-ai -------------------------------------------------------------------------------

def test_e2e_opencode_and_claude_on_ollama_are_told_the_served_window(tmp_path):
    """`opencode --llm ol:MODEL` and `claude --llm ol:MODEL` against an Ollama-like server with
    OLLAMA_CONTEXT_LENGTH=32768: OpenCode's limit.context and Claude Code's window variables in the sandbox are 32768,
    not the model's 131072. A dry run asks the server nothing and loads nothing."""
    from live_fakes import Host
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=(MODEL,), ollama_ps=True, default_context=32768, log=str(log),
                                    log_bodies=True)
    try:
        host = Host(tmp_path / "host")
        host.add_image("localhost/opencode-sandbox:latest")
        host.add_image("localhost/claude-sandbox:latest")
        env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "", "SANDBOX_AI_SECRETS": "file"}
        added = host.run("llm", "add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}", "--model",
                         MODEL, env=env)
        assert added.returncode == 0, added.stderr
        dry = host.run("opencode", "--llm", f"ol:{MODEL}", "--dry-run", env=env)
        assert dry.returncode == 0, dry.stderr
        assert json.loads(dry.stdout)["llm"]["kind"] == "ollama"
        assert loads(log) == [], "a dry run loads no model"

        done = host.run("opencode", "--no-tmux", "--llm", f"ol:{MODEL}",
                        env={**env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / "kept")})
        assert done.returncode == 0, done.stderr
        assert "Ollama serves" in done.stderr and "32768" in done.stderr and "Traceback" not in done.stderr
        configs = list(host.directory.rglob("sandbox-ai-llm.json"))
        assert configs, "OpenCode's configuration was not written"
        config = json.loads(configs[0].read_text())
        assert next(iter(config["provider"].values()))["models"][MODEL]["limit"]["context"] == 32768
        assert loads(log) == [MODEL]

        claude = host.run("claude", "--no-tmux", "--no-auth", "--llm", f"ol:{MODEL}",
                          env={**env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / "kc")})
        assert claude.returncode == 0, claude.stderr
        given = json.loads((tmp_path / "kc/session/config.json").read_text())["env"]
        assert given["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "32768"
        assert given["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "32768"
        assert loads(log) == [MODEL], "loaded already: read from /api/ps"
    finally:
        server.shutdown()
