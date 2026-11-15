"""--llm: the agents wait for a slow local model instead of giving up during its prefill and sending everything again.

User: with local models "some took ages before they finished". The local-model research (phase 2, a mock server that
keeps the first byte back; evidence/timeouts_mock.txt, runs/delayfix_*, delayfix2_*) proved with the real harness CLIs:
a fresh 50-100k prompt on a 120B local model takes 2-6 minutes before its first token, and the harnesses gave up long
before - Qwen Code after 120 s (its request timeout) and 240 s (stream idle), OpenCode after 300 s (response headers,
chunks), Codex after 300 s (stream_idle_timeout_ms), Goose after 600 s (OPENAI_TIMEOUT) - and sent the whole request
again, for ever. With the settings below each waited through 420 s of silence and finished.

One value for all of them: the start's or the server's stream_idle_timeout, else llm.LOCAL_WAIT (an hour, the proxy's
own idle limit) for a model server of your own; an OpenAI-compatible service at a public host keeps each agent's own.

Unit tests of each generated configuration, then the real ./sandbox-ai --dry-run (tests/live_fakes.py)."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests/devtools"))
import home_guard  # noqa: E402

home_guard.ensure()
from live_fakes import Host  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


llm = load("sandbox_llm_waits", "sandbox/llm.py")
runtime = load("sandbox_runtime_llm_waits", "sandbox/runtime.py")
HOUR_MS = 3_600_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")
    monkeypatch.setattr(llm, "keep_loaded", lambda *args, **kw: None)


LOCAL = {"kind": "lmstudio", "url": "http://127.0.0.1:1234", "model": "qwen3-coder-next"}
PUBLIC = {"kind": "openai-compatible", "url": "https://openrouter.ai/api/v1", "model": "qwen/qwen3-coder"}


def start(agent, *params, record=LOCAL, env=()):
    """A --llm start of AGENT at the server RECORD (stored as "srv"), with --llm-param PARAMS."""
    store = llm.load_store()
    store["endpoints"]["srv"] = dict(record)
    llm.save_store(store)
    return argparse.Namespace(agent=agent, llm="srv", llm_param=list(params), llm_models=None, vpn=None,
                              env=list(env), secret_env=[], no_provider_defaults=False,
                              dangerously_skip_permissions=False, qwen_enable=[], goose_enable=[], allow_host=[],
                              offline=False, agent_args=[])


def chosen(a):
    return llm.selection(a)


# --- one value: stream_idle_timeout, else an hour for a model server of your own -------------------------------------

def test_a_local_server_is_waited_for_an_hour_unless_stream_idle_timeout_says_otherwise():
    a = start("codex")
    assert llm.LOCAL_WAIT == 3600
    assert llm.agent_wait(a, *chosen(a)) == llm.LOCAL_WAIT
    a = start("codex", "stream_idle_timeout=900")
    assert llm.agent_wait(a, *chosen(a)) == 900
    a = start("codex", record={**LOCAL, "params": {"*": {"stream_idle_timeout": 7200}}})
    assert llm.agent_wait(a, *chosen(a)) == 7200


def test_a_public_service_keeps_each_agents_own_wait():
    a = start("codex", record=PUBLIC)
    assert llm.agent_wait(a, *chosen(a)) is None
    a = start("codex", "stream_idle_timeout=900", record=PUBLIC)
    assert llm.agent_wait(a, *chosen(a)) == 900


def test_the_proxy_never_cuts_a_stream_the_agents_still_wait_for():
    """sandbox-ai's own path (the connection proxy, its LLM relay) ends a connection quiet both ways only after
    IDLE_TIMEOUT: never before the agents' default wait."""
    assert runtime.IDLE_TIMEOUT >= llm.LOCAL_WAIT


# --- T3 Codex: stream_idle_timeout_ms of the provider it reads ------------------------------------------------------

def written(a, tmp_path):
    """{HOME-relative path: text} that prepare_home writes for A."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    files = {}

    def replace_private(data, where, relative):
        files[relative] = data.decode()
    llm.prepare_home(a, home, replace_private, lambda *args: None, None)
    return files


def test_codex_waits_an_hour_for_a_local_server_in_the_provider_section_it_reads(tmp_path):
    files = written(start("codex"), tmp_path)
    profile = tomllib.loads(files[f".codex/{llm.CODEX_PROFILE}.config.toml"])
    provider = profile["model_providers"][profile["model_provider"]]
    assert provider["stream_idle_timeout_ms"] == HOUR_MS
    assert "stream_idle_timeout_ms" not in {key for key, value in profile.items() if not isinstance(value, dict)}


def test_codex_takes_stream_idle_timeout_and_keeps_its_own_for_a_public_service(tmp_path):
    files = written(start("codex", "stream_idle_timeout=900"), tmp_path)
    profile = tomllib.loads(files[f".codex/{llm.CODEX_PROFILE}.config.toml"])
    assert profile["model_providers"][profile["model_provider"]]["stream_idle_timeout_ms"] == 900_000
    files = written(start("codex", record=PUBLIC), tmp_path)
    profile = tomllib.loads(files[f".codex/{llm.CODEX_PROFILE}.config.toml"])
    assert "stream_idle_timeout_ms" not in profile["model_providers"][profile["model_provider"]]


def test_claude_code_is_told_the_same_wait():
    """Claude Code gives up after 360 s without a byte whatever its variables say (research runs/delayfix2_claude_*);
    its two waits still get the same value, so nothing of sandbox-ai's makes it shorter."""
    a = start("claude")
    assert llm.claude_wait_environment(a, *chosen(a)) == {"CLAUDE_STREAM_IDLE_TIMEOUT_MS": str(HOUR_MS),
                                                          "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": str(HOUR_MS)}
    a = start("claude", record=PUBLIC)
    assert llm.claude_wait_environment(a, *chosen(a)) == {}


# --- T2 OpenCode: its provider's headerTimeout and chunkTimeout, no whole-request timeout ---------------------------

def opencode_options(a, tmp_path):
    config = json.loads(written(a, tmp_path)[llm.OPENCODE_CONFIG])
    return config["provider"][llm.provider_id(chosen(a)[0])]["options"]


def test_opencode_waits_an_hour_for_the_response_headers_and_each_chunk(tmp_path):
    """OpenCode 1.19.5 (and OpenCode 2) wrap the provider's fetch: headerTimeout and chunkTimeout default to 300000
    ms each, timeout (the whole request) to none; false turns a wait off. A silent prefill ended with "Provider
    response headers timed out after 300000ms" (runs/delay_silent_opencode) - chunkTimeout and timeout alone did not
    reach the headers' wait."""
    options = opencode_options(start("opencode"), tmp_path)
    assert options["headerTimeout"] == options["chunkTimeout"] == HOUR_MS
    assert options["timeout"] is False, "a long answer is never cut while its chunks still come"


def test_opencode_takes_stream_idle_timeout_and_keeps_its_own_for_a_public_service(tmp_path):
    options = opencode_options(start("opencode", "stream_idle_timeout=900"), tmp_path)
    assert options["headerTimeout"] == options["chunkTimeout"] == 900_000 and options["timeout"] is False
    options = opencode_options(start("opencode", record=PUBLIC), tmp_path)
    assert not {"timeout", "headerTimeout", "chunkTimeout"} & set(options)


# --- T1 Qwen Code: its provider entry's generationConfig and its variables -----------------------------------------

def launched(agent, *options):
    """The launcher's whole environment of the agent (agent_sandbox.agent_environment) for a --llm srv start."""
    a = launcher.parse_args([agent, "--workspace", "/tmp", "--llm", "srv", *options])
    return launcher.agent_environment(a)


launcher = load("sandbox_launcher_llm_waits", "sandbox/agent_sandbox.py")
qwen = load("sandbox_qwen_llm_waits", "sandbox/qwen.py")
QWEN_WAITS = ("QWEN_STREAM_IDLE_TIMEOUT_MS", "QWEN_CODE_API_TIMEOUT_MS")


def test_qwen_waits_an_hour_per_request_and_for_the_stream_of_every_model_it_offers():
    """Qwen Code 0.26.4 takes a modelProviders model's timeout (120 s by default) and streamIdleTimeoutMs (240 s)
    from that entry's generationConfig - model.generationConfig is not used for it (runs/delayfix_silent_qwen: still
    120 s); the variables say the same for any other model."""
    a = start("qwen", record={**LOCAL, "models": ["qwen3-coder-next", "gpt-oss-120b"]})
    entries = llm.qwen_settings(a)["modelProviders"]["openai"]
    assert len(entries) == 2
    for entry in entries:
        assert {key: entry["generationConfig"][key] for key in ("timeout", "streamIdleTimeoutMs", "maxRetries")} == \
            {"timeout": HOUR_MS, "streamIdleTimeoutMs": HOUR_MS, "maxRetries": 2}
    env = llm.agent_environment(a)
    assert {name: env[name] for name in QWEN_WAITS} == {name: str(HOUR_MS) for name in QWEN_WAITS}
    assert env["QWEN_STREAM_MAX_LIFETIME_MS"] == str(HOUR_MS)


def test_qwen_takes_stream_idle_timeout_and_keeps_its_own_for_a_public_service():
    a = start("qwen", "stream_idle_timeout=900")
    entry = llm.qwen_settings(a)["modelProviders"]["openai"][0]
    assert entry["generationConfig"]["timeout"] == entry["generationConfig"]["streamIdleTimeoutMs"] == 900_000
    env = llm.agent_environment(a)
    assert {name: env[name] for name in QWEN_WAITS} == {name: "900000" for name in QWEN_WAITS}
    assert env["QWEN_STREAM_MAX_LIFETIME_MS"] == str(HOUR_MS), "a stream's whole life is never shorter than an hour"
    a = start("qwen", "stream_idle_timeout=7200")
    assert llm.agent_environment(a)["QWEN_STREAM_MAX_LIFETIME_MS"] == "7200000"
    a = start("qwen", record=PUBLIC)
    entry = llm.qwen_settings(a)["modelProviders"]["openai"][0]
    assert not {"timeout", "streamIdleTimeoutMs"} & set(entry["generationConfig"])
    assert entry["generationConfig"]["maxRetries"] == 2
    assert not set(QWEN_WAITS) & set(llm.agent_environment(a))


def test_qwen_your_own_variables_win_and_the_launcher_passes_the_same_value():
    start("qwen")
    env = launched("qwen")
    assert {name: env[name] for name in QWEN_WAITS} == {name: str(HOUR_MS) for name in QWEN_WAITS}
    assert not set(QWEN_WAITS) & set(qwen.environment(argparse.Namespace(agent="qwen", qwen_enable=[], llm="srv"))), \
        "one source: llm.agent_wait, not a constant of qwen.py"
    a = start("qwen", env=["QWEN_STREAM_IDLE_TIMEOUT_MS"])
    assert "QWEN_STREAM_IDLE_TIMEOUT_MS" not in llm.agent_environment(a)


# --- T4 Goose: OPENAI_TIMEOUT and GOOSE_STREAM_TIMEOUT (seconds) ----------------------------------------------------

GOOSE_WAITS = ("OPENAI_TIMEOUT", "GOOSE_STREAM_TIMEOUT")


def test_goose_waits_an_hour_for_its_openai_provider():
    """Goose 1.57.0's openai provider gives up after OPENAI_TIMEOUT seconds (600 by default) and sends the request
    again (runs/delay_silent_goose: three times 600 s); with both variables at 3600 it waited through 420 s of silence
    (runs/delayfix_silent_goose)."""
    a = start("goose")
    env = llm.goose_environment(a, *chosen(a))
    assert env["GOOSE_PROVIDER"] == "openai", "--llm uses only Goose's openai provider"
    assert {name: env[name] for name in GOOSE_WAITS} == {"OPENAI_TIMEOUT": "3600", "GOOSE_STREAM_TIMEOUT": "3600"}


def test_goose_takes_stream_idle_timeout_and_keeps_its_own_for_a_public_service():
    a = start("goose", "stream_idle_timeout=900")
    env = llm.goose_environment(a, *chosen(a))
    assert {name: env[name] for name in GOOSE_WAITS} == {"OPENAI_TIMEOUT": "900", "GOOSE_STREAM_TIMEOUT": "900"}
    a = start("goose", record=PUBLIC)
    assert not set(GOOSE_WAITS) & set(llm.goose_environment(a, *chosen(a)))
    a = start("goose", env=["OPENAI_TIMEOUT"])
    assert "OPENAI_TIMEOUT" not in llm.goose_environment(a, *chosen(a)), "your own --env OPENAI_TIMEOUT wins"
    start("goose")
    env = launched("goose")
    assert {name: env[name] for name in GOOSE_WAITS} == {"OPENAI_TIMEOUT": "3600", "GOOSE_STREAM_TIMEOUT": "3600"}


# --- e2e: the real ./sandbox-ai --dry-run ---------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    return Host(tmp_path / "host")


@pytest.mark.parametrize("agent", ["codex", "opencode", "qwen", "goose", "claude"])
def test_e2e_the_plan_names_the_agents_wait(host, agent):
    env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"}
    added = host.run("llm", "add", "lms", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--model", "m",
                     env=env)
    assert added.returncode == 0, added.stderr
    done = host.run(agent, "--llm", "lms:m", "--dry-run", env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["llm"]["params"]["agent_wait"] == 3600
    done = host.run(agent, "--llm", "lms:m", "--llm-param", "stream_idle_timeout=900", "--dry-run", env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["llm"]["params"]["agent_wait"] == 900
