"""The server's other models in the agent (live use, hotfix 0.1.96). User: "Auch supporten, wenn aktiviert ist, dass
in z.B. OpenCode andere auf dem LLM-Server vorhandene Modelle geladen/ausgewählt werden können."

A start with --llm NAME:MODEL wired that one model into the agent. Now the agent offers every model the server lists
at the start (--llm-models all, the default; --llm-models MODEL,... limits them), the start's model stays the default,
and the connection proxy takes requests for any model of that same server - applying each model's own parameters,
recording which model each request asked for - and never for another server; with a limit it refuses the others.

What each agent can do, checked in its pinned source (see llm.py, "the server's other models"):
  OpenCode     the provider's models, each with limit.context/output: /models
  Qwen Code    0.26.4 modelProviders.openai, one entry per model (its timeouts and contextWindowSize): /model
  DSH          0.2.1-rc.5 the route's models: its web UI's model selection
  Codex        0.169 /model offers its own catalog only: a profile per model (codex --profile sandbox-ai-llm-SLUG)
  Hermes, Goose, Claude Code: their own /model (nothing to configure); the start's note says how.

Here: each agent's generated configuration, the relay accepting and refusing models and applying per-model
parameters (the real proxy, tests/devtools/model_mock.py), and the real ./sandbox-ai start (tests/live_fakes.py)."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = "studio"
MODEL = "qwen/qwen3-coder-next"
OTHERS = ("google/gemma-3-27b", "openai/gpt-oss-20b", "qwen2.5-coder:7b")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


llm = load("sandbox_llm_hotfix_models", "sandbox/llm.py")
rt = load("sandbox_runtime_hotfix_models", "sandbox/runtime.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    assert status == 0, lines
    return "\n".join(lines)


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """LM Studio with four models (the start's one loaded at 65536 tokens), registered as the user did."""
    log = tmp_path / "mock.jsonl"
    server, port = model_mock.start(models=(MODEL, *OTHERS), lmstudio=True, max_context=131072,
                                    loaded={MODEL: 65536}, log=str(log), log_bodies=True)
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    run("add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model", MODEL)
    yield port, log
    server.shutdown()


def start(agent="opencode", **options):
    defaults = {"llm": f"{NAME}:{MODEL}", "agent": agent, "env": [], "secret_env": [], "llm_param": [], "vpn": None,
                "no_provider_defaults": True, "llm_models": None, "offline": False, "no_inspect": [],
                "agent_args": [], "local_config": False, "dangerously_skip_permissions": False}
    return argparse.Namespace(**{**defaults, **options})


def written_home(a):
    written = {}
    llm.prepare_home(a, Path("/h"), lambda data, home, relative: written.update({relative: data.decode()}), None, None)
    return written


def requests_of(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# --- each agent's configuration -------------------------------------------------------------------------------------

def test_regression_opencode_offers_the_other_models_of_the_server(studio):
    """User: "dass in z.B. OpenCode andere auf dem LLM-Server vorhandene Modelle geladen/ausgewählt werden können".
    --llm studio:MODEL: OpenCode's provider lists all four models of the server (asked at the start), each with the
    limits the server says, MODEL stays the default."""
    config = json.loads(written_home(start())[llm.OPENCODE_CONFIG])
    models = config["provider"]["llm-studio"]["models"]
    assert list(models) == [MODEL, *OTHERS]
    assert config["model"] == f"llm-studio/{MODEL}" and config["small_model"] == f"llm-studio/{MODEL}"
    assert models[MODEL]["limit"] == {"context": 65536, "output": 16384}  # as loaded
    assert models["google/gemma-3-27b"]["limit"] == {"context": 131072, "output": 32768}  # its maximum
    # The list is remembered for later starts (as `llm models` does).
    assert llm.endpoint(NAME)["models"] == [MODEL, *OTHERS]


def test_per_model_parameters_reach_the_agents_limits(studio):
    run("set", NAME, "--model", "google/gemma-3-27b", "--param", "num_ctx=32768", "--param", "max_tokens=4096")
    models = json.loads(written_home(start())[llm.OPENCODE_CONFIG])["provider"]["llm-studio"]["models"]
    assert models["google/gemma-3-27b"]["limit"] == {"context": 32768, "output": 4096}
    assert models["openai/gpt-oss-20b"]["limit"]["context"] == 131072


def test_qwen_code_lists_every_model_with_its_timeouts_and_window(studio):
    settings = llm.qwen_settings(start("qwen"))
    entries = settings["modelProviders"]["openai"]
    assert [entry["id"] for entry in entries] == [MODEL, *OTHERS] and settings["model"]["name"] == MODEL
    for entry in entries:  # the structure of the slow-model timeouts kept for each
        assert entry["baseUrl"].endswith("/v1") and entry["envKey"] == "OPENAI_API_KEY"
        assert {k: entry["generationConfig"][k] for k in llm.QWEN_GENERATION} == llm.QWEN_GENERATION
    assert entries[0]["generationConfig"]["contextWindowSize"] == 65536
    assert entries[1]["generationConfig"]["contextWindowSize"] == 131072


def test_dsh_routes_every_model(studio):
    route = llm.dsh_config(start("dsh"))["route"]
    assert [m["id"] for m in route["models"]] == [MODEL, *OTHERS]
    assert route["models"][0]["contextWindow"] == 65536


def test_codex_gets_a_profile_per_model(studio):
    """Codex 0.169's /model offers only its catalog (a catalog entry must carry Codex's whole system prompt): a
    profile per model, a name Codex takes (letters, digits, - and _), the provider the same."""
    written = written_home(start("codex"))
    default = written[f".codex/{llm.CODEX_PROFILE}.config.toml"]
    assert f'model = "{MODEL}"' in default and "model_context_window = 65536" in default
    profiles = {path: text for path, text in written.items() if path != f".codex/{llm.CODEX_PROFILE}.config.toml"}
    assert sorted(profiles) == [".codex/sandbox-ai-llm-google-gemma-3-27b.config.toml",
                                ".codex/sandbox-ai-llm-openai-gpt-oss-20b.config.toml",
                                ".codex/sandbox-ai-llm-qwen2-5-coder-7b.config.toml"]
    text = profiles[".codex/sandbox-ai-llm-qwen2-5-coder-7b.config.toml"]
    assert 'model = "qwen2.5-coder:7b"' in text and 'model_provider = "llm-studio"' in text
    assert "codex --profile sandbox-ai-llm-qwen2-5-coder-7b" in text and "model_context_window = 131072" in text
    assert llm.codex_profiles(["a/b", "a:b", "a.b"]) == {"a/b": "sandbox-ai-llm-a-b", "a:b": "sandbox-ai-llm-a-b-2",
                                                        "a.b": "sandbox-ai-llm-a-b-3"}


@pytest.mark.parametrize("agent, says", [
    ("opencode", "in OpenCode with /models"), ("qwen", "in Qwen Code with /model"),
    ("hermes", "in Hermes with /model"), ("goose", "in Goose with /model MODEL-ID"),
    ("dsh", "in DSH's web UI"), ("claude", "in Claude Code with /model MODEL-ID"),
    ("codex", "codex --profile sandbox-ai-llm-google-gemma-3-27b")])
def test_the_start_says_how_each_agent_switches_and_that_loading_waits(studio, agent, says):
    note = llm.switch_note(start(agent))
    assert f"{MODEL} and 3 other models (the server lists" in note and says in note
    assert "loaded by the first request to it, which waits" in note and "LM Studio: JIT loading" in note
    assert f"evict never keeps {MODEL} loaded" in note


def test_llm_models_limits_the_offer_and_the_relay(studio):
    a = start(llm_models="openai/gpt-oss-20b")
    assert llm.offered_models(a) == ([MODEL, "openai/gpt-oss-20b"], True)
    assert list(json.loads(written_home(a)[llm.OPENCODE_CONFIG])["provider"]["llm-studio"]["models"]) == \
        [MODEL, "openai/gpt-oss-20b"]
    entry = llm.proxy_entry(a)[0]
    assert entry["models"] == [MODEL, "openai/gpt-oss-20b"]
    assert "the proxy refuses requests for any other model" in llm.switch_note(a)
    # all (the default): any model of the server.
    assert llm.proxy_entry(start(llm_models="all"))[0]["models"] is None
    # One model only: no note.
    assert llm.switch_note(start(llm_models=MODEL)) is None
    for bad in ("a,,b", "bad model", ""):
        with pytest.raises(ValueError, match="--llm-models expects all or MODEL"):
            llm.parse_offer(bad)


def test_models_named_twice_are_refused(studio):
    a = start(llm=f"{NAME}:{MODEL},google/gemma-3-27b", llm_models="openai/gpt-oss-20b")
    with pytest.raises(ValueError, match="name them in one place"):
        llm.validate(a, True, True, lambda **kw: True)
    # The models --llm names are the offer, as before (limited).
    a = start(llm=f"{NAME}:{MODEL},google/gemma-3-27b")
    assert llm.offered_models(a) == ([MODEL, "google/gemma-3-27b"], True)


def test_without_the_server_the_remembered_models_are_offered(monkeypatch):
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")
    run("add", NAME, "--kind", "ollama", "--url", "http://127.0.0.1:9", "--model", MODEL)
    llm.remember_models(NAME, [MODEL, "llama3:8b", "nomic-embed-text:latest"])
    names, limited = llm.offered_models(start())
    assert names == [MODEL, "llama3:8b", "nomic-embed-text:latest"] and not limited
    note = llm.switch_note(start())
    assert "remembered for it (sandbox-ai llm models studio)" in note and "OLLAMA_MAX_LOADED_MODELS" in note
    # Embedding models are no chat models: never offered from a fresh listing.
    assert not llm.chat_model({"name": "nomic-embed-text:latest", "families": ["nomic-bert"]})
    assert llm.chat_model({"name": "llama3:8b", "families": ["llama"]})


# --- the relay: any model of that server, each with its parameters --------------------------------------------------

@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_relay_takes_any_model_of_the_server_with_its_own_parameters(studio, tmp_path):
    from test_llm_loops import Relay, dechunk
    port, log = studio
    run("set", NAME, "--model", "google/gemma-3-27b", "--param", "temperature=0.3", "--param", "max_tokens=2048")
    relay = Relay(tmp_path, port, start())
    try:
        for model in (MODEL, "google/gemma-3-27b", "openai/gpt-oss-20b", "not-listed-at-the-start/qwen3-coder-x"):
            head, _ = dechunk(relay.post("/v1/chat/completions", {
                "model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8192}))
            # (the last one the server itself does not have: its own 404 comes back, the relay passed it on)
            assert (b" 200 " if "/qwen3-coder-x" not in model else b" 404 ") in head.split(b"\r\n", 1)[0], head
        sent = {r["body"]["model"]: r["body"] for r in requests_of(log) if r["path"] == "/v1/chat/completions"}
        # "geladen": LM Studio loaded the other model by its first request (JIT), the start's one stays loaded.
        assert llm.loaded_context(llm.endpoint(NAME), "google/gemma-3-27b") is not None
        assert llm.loaded_context(llm.endpoint(NAME), MODEL) == 65536
        assert sent["google/gemma-3-27b"]["temperature"] == 0.3 and sent["google/gemma-3-27b"]["max_tokens"] == 2048
        assert sent["openai/gpt-oss-20b"]["temperature"] == 1.0  # its family's preset (listed at the start)
        assert sent["not-listed-at-the-start/qwen3-coder-x"]["top_k"] == 20  # the family preset of any model
        assert sent[MODEL]["max_tokens"] == 8192 and "temperature" in sent[MODEL]
        records = relay.records()
        assert [r.get("llm_model") for r in records if r.get("llm")] == \
            [MODEL, "google/gemma-3-27b", "openai/gpt-oss-20b", "not-listed-at-the-start/qwen3-coder-x"]
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_relay_refuses_a_model_the_start_does_not_offer(studio, tmp_path):
    from test_llm_loops import Relay, dechunk
    port, log = studio
    relay = Relay(tmp_path, port, start(llm_models="openai/gpt-oss-20b"))
    try:
        head, body = dechunk(relay.post("/v1/chat/completions", {
            "model": "google/gemma-3-27b", "messages": [{"role": "user", "content": "hi"}]}))
        assert b" 403 " in head.split(b"\r\n", 1)[0]
        error = json.loads(body)["error"]
        assert error["code"] == "model_not_offered" and "google/gemma-3-27b" in error["message"]
        assert "--llm-models" in error["message"]
        assert not [r for r in requests_of(log) if r["path"] == "/v1/chat/completions"]  # nothing reached the server
        head, _ = dechunk(relay.post("/v1/chat/completions", {
            "model": "openai/gpt-oss-20b", "messages": [{"role": "user", "content": "hi"}]}))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
        denied = [r for r in relay.records() if r.get("event") == "denied"]
        assert denied and denied[0]["rule"] == "(llm-models)" and denied[0]["llm_model"] == "google/gemma-3-27b"
    finally:
        relay.close()


def test_the_relay_rules_in_short():
    entry = {"name": "s", "models": ["a", "b"], "presets": {"a": {"top_k": 5}},
             "families": [[pattern.pattern, values] for pattern, values in llm.FAMILY_PRESETS]}
    assert rt.llm_model_refusal(entry, {"model": "a"}) is None
    assert rt.llm_model_refusal(entry, {"messages": []}) is None  # a server of one model: no name, no refusal
    assert "not among the models" in rt.llm_model_refusal(entry, {"model": "c"})
    assert rt.llm_model_refusal({**entry, "models": None}, {"model": "c"}) is None
    assert rt.llm_preset(entry, "a") == {"top_k": 5}
    assert rt.llm_preset(entry, "Qwen3-Coder-30B")["top_k"] == 20
    assert rt.llm_preset({"families": [["(", {}]]}, "x") == {}  # a broken pattern is ignored


def test_the_llm_traffic_log_names_each_requests_model():
    traffic = rt.LlmTraffic.__new__(rt.LlmTraffic)
    traffic.run, traffic.session = "r", None
    record = traffic.record({"api": "chat", "document": {"model": "google/gemma-3-27b", "messages": []},
                             "before": None, "done": {}})
    assert record["model"] == "google/gemma-3-27b"


# --- end to end: the real ./sandbox-ai ------------------------------------------------------------------------------

def e2e_host(tmp_path, image):
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image(image)
    return host


def test_e2e_opencode_and_qwen_starts_offer_the_servers_models(tmp_path):
    """The real ./sandbox-ai (only Podman stood in for) starts opencode and qwen with --llm studio:MODEL against an LM
    Studio-like server: the configuration in the sandbox lists all of its models, the start says how to switch, and
    the dry run says which models the proxy takes."""
    server, port = model_mock.start(models=(MODEL, *OTHERS), lmstudio=True, max_context=131072, loaded={MODEL: 65536})
    try:
        host = e2e_host(tmp_path, "localhost/opencode-sandbox:latest")
        host.add_image("localhost/qwen-sandbox:latest")
        env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "", "SANDBOX_AI_SECRETS": "file"}
        added = host.run("llm", "add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model",
                         MODEL, env=env)
        assert added.returncode == 0, added.stderr
        kept = tmp_path / "kept"
        done = host.run("opencode", "--no-tmux", "--llm", f"{NAME}:{MODEL}",
                        env={**env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
        assert done.returncode == 0, done.stderr
        assert "3 other models (the server lists" in done.stderr and "in OpenCode with /models" in done.stderr
        configs = list(host.directory.rglob("sandbox-ai-llm.json"))
        assert configs, "OpenCode's configuration was not written"
        config = json.loads(configs[0].read_text())
        assert list(config["provider"]["llm-studio"]["models"]) == [MODEL, *OTHERS]
        assert config["model"] == f"llm-studio/{MODEL}"

        qwen = host.run("qwen", "--no-tmux", "--llm", f"{NAME}:{MODEL}", "--llm-models", "google/gemma-3-27b",
                        env={**env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept / "q")})
        assert qwen.returncode == 0, qwen.stderr
        assert "1 other model (named with --llm-models)" in qwen.stderr and "in Qwen Code with /model" in qwen.stderr
        settings = json.loads(next((kept / "q/session").rglob("settings.json")).read_text())
        assert [e["id"] for e in settings["modelProviders"]["openai"]] == [MODEL, "google/gemma-3-27b"]
    finally:
        server.shutdown()


def test_e2e_dry_run_and_refusals(tmp_path):
    host = e2e_host(tmp_path, "localhost/opencode-sandbox:latest")
    env = {"SANDBOX_AI_SECRETS": "file"}
    assert host.run("llm", "add", NAME, "--kind", "ollama", "--url", "http://127.0.0.1:9", "--model", MODEL,
                    env=env).returncode == 0
    dry = host.run("opencode", "--llm", f"{NAME}:{MODEL}", "--llm-models", "llama3:8b", "--dry-run",
                   env=env)
    assert dry.returncode == 0, dry.stderr
    plan = json.loads(dry.stdout)["llm"]
    assert plan["models"] == [MODEL, "llama3:8b"] and plan["models_limited"] is True
    twice = host.run("opencode", "--llm", f"{NAME}:{MODEL},llama3:8b", "--llm-models", "x", "--dry-run", env=env)
    assert twice.returncode != 0 and "name them in one place" in twice.stderr
    alone = host.run("opencode", "--llm-models", "x", "--dry-run", env=env)
    assert alone.returncode != 0 and "add --llm NAME" in alone.stderr
    bad = host.run("opencode", "--llm", NAME, "--llm-models", "a,,b", "--dry-run", env=env)
    assert bad.returncode != 0 and "--llm-models expects all or MODEL" in bad.stderr
