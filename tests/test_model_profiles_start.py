"""Model x harness profiles at a start (0.1.98): --llm NAME:MODEL with agent A applies the profile of MODEL's family
in A (sandbox/model_profiles.json) UNDER everything the user sets - the profile < the endpoint's values (llm set) <
the start's --llm-param < explicit options (--env) -, says it at the start, shows it in the dry run (llm.profile: id,
status, applied, overridden), and --no-llm-profile switches it off. A pair known not to work is a warning, never a
refusal. User: "have perfect configs and settings for the models ready and auto loaded if the board is started".

The relay's side runs the real runtime.llm_values on the entry llm.proxy_params writes; the start's side the real
./sandbox-ai (tests/live_fakes.py: only Podman stood in for) against an LM Studio-like server (tests/devtools)."""
import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = "studio"
QCN = "qwen/qwen3-coder-next"
Q38 = "qwen/qwen3.8-27b"
NEMOTRON = "nvidia/nemotron-3-super"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


llm = load("sandbox_llm_profiles_start", "sandbox/llm.py")
rt = load("sandbox_runtime_profiles_start", "sandbox/runtime.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    assert status == 0, lines
    return "\n".join(lines)


def start(agent="opencode", model=QCN, **options):
    defaults = {"llm": f"{NAME}:{model}", "agent": agent, "env": [], "secret_env": [], "llm_param": [], "vpn": None,
                "no_provider_defaults": True, "llm_models": model, "offline": False, "no_inspect": [],
                "agent_args": [], "local_config": False, "dangerously_skip_permissions": False, "mcp": [],
                "no_llm_profile": False}
    return argparse.Namespace(**{**defaults, **options})


@pytest.fixture
def studio():
    run("add", NAME, "--kind", "lmstudio", "--url", "http://127.0.0.1:9", "--model", QCN)


# --- precedence -----------------------------------------------------------------------------------------------------

def test_the_profile_is_the_lowest_layer_of_the_parameters(studio):
    a = start()
    found, model = llm.selection(a)
    values, forced = llm.effective_params(found, model)
    assert values == {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05, "max_tokens": 16384,
                      "num_ctx": 131072}
    assert forced == set()
    run("set", NAME, "--param", "temperature=0.3")  # every model's value of the endpoint wins
    found, model = llm.selection(start())
    assert llm.effective_params(found, model)[0]["temperature"] == 0.3
    run("set", NAME, "--model", QCN, "--param", "top_k=40")  # the model's own wins
    found, model = llm.selection(start())
    values, _ = llm.effective_params(found, model, {"top_p": 0.5})  # the start's --llm-param wins over all
    assert (values["temperature"], values["top_k"], values["top_p"], values["repeat_penalty"]) == (0.3, 40, 0.5, 1.05)


def test_switched_off_nothing_of_it_applies(studio):
    found, model = llm.selection(start(no_llm_profile=True))
    assert "llm_profile" not in found
    assert llm.effective_params(found, model)[0] == {}
    assert llm.loop_config(found)["steps"] == llm.LOOP_DEFAULTS["steps"]


def test_no_profile_for_a_model_of_no_family(studio):
    found, model = llm.selection(start(model="llama3:8b"))
    assert llm.effective_params(found, model)[0] == {}
    assert llm.profile_report(start(model="llama3:8b"), found, model) is None


def test_the_loop_guard_layers_under_yours(studio, tmp_path):
    found, _ = llm.selection(start())
    assert llm.loop_config(found)["steps"] == 150
    assert llm.loop_config(found, user={"steps": 60})["steps"] == 60  # defaults.json
    run("set", NAME, "--loop-guard", "steps=80")
    found, _ = llm.selection(start())
    assert llm.loop_config(found, user={"steps": 60})["steps"] == 80  # the endpoint's
    assert llm.loop_config(found, {"steps": 90})["steps"] == 90  # the start's
    assert llm.loop_config(found, {"enabled": False})["steps"] == 0  # the guard off: every layer off


def test_the_relay_applies_it_below_the_endpoint_and_the_start(studio):
    """The entry the proxy gets (llm.proxy_params): the profile's sampling as the lowest default of the real
    runtime.llm_values; the endpoint's value and the start's over it; num_ctx is never sent."""
    run("set", NAME, "--param", "top_p=0.9")
    a = start(llm_param=["temperature=0.2"])
    found, model = llm.selection(a)
    entry = llm.proxy_params(a, found, model)
    assert entry["presets"][QCN]["top_k"] == 20 and "num_ctx" not in entry["presets"][QCN]
    values, forced = rt.llm_values(entry, QCN)
    assert (values["temperature"], values["top_p"], values["top_k"], values["repeat_penalty"]) == (0.2, 0.9, 20, 1.05)
    assert forced == {"temperature"}
    assert entry["loop"]["steps"] == 150
    # A model of the server the start did not list: its family's profile through "families".
    assert rt.llm_preset(entry, "google/gemma-4-31b-qat") == {"temperature": 1.0, "top_p": 0.95, "top_k": 64,
                                                             "max_tokens": 16384}
    assert rt.llm_preset(entry, "llama3:8b") == {}
    off = start(no_llm_profile=True)
    found, model = llm.selection(off)
    entry = llm.proxy_params(off, found, model)
    assert rt.llm_preset(entry, "google/gemma-4-31b-qat") == {}
    assert rt.llm_preset(entry, QCN)["top_k"] == 20  # the family preset (the loop guard's sampling layer) stays


def test_the_agents_window_is_the_profiles_at_most_what_the_server_keeps(studio, capsys):
    a = start()
    found, model = llm.selection(a)
    assert llm.agent_context(found, model, a, reported=262144) == 131072
    assert llm.agent_context(found, model, a, reported=262144, loaded=65536) == 65536
    assert llm.agent_context(found, model, a) is None  # nothing known: the agent's own guess, never a blind 131072
    assert "WARNING" not in capsys.readouterr().err
    mine = start(llm_param=["num_ctx=200000"])
    found, model = llm.selection(mine)
    assert llm.agent_context(found, model, mine, reported=262144, loaded=65536) == 200000  # yours, warned
    assert "is told a context window of 200000" in capsys.readouterr().err


def test_qwen38_and_codex_a_smaller_window_and_no_status_without_a_codex_run(studio):
    """Qwen3.8-27B was measured in Claude Code only (round 7): in Codex its settings apply (the smaller window) and
    the start says there is no data yet for Codex - no warning, no status taken from elsewhere (2026-11-22)."""
    a = start("codex", Q38)
    found, model = llm.selection(a)
    assert llm.agent_context(found, model, a, reported=262144) == 49152
    report = llm.profile_report(a, found, model)
    assert report["status"] == "untested" and report["enabled"] is True and report["warnings"] == []
    lines = llm.profile_lines(report, NAME)
    assert lines[0].startswith("agent-sandbox: --llm studio: profile qwen3.8-27b × codex (no data yet in Codex): ")
    assert len(lines) == 1 and not any("WARNING" in line for line in lines)


def test_claude_code_and_nemotron_thinking_off_unless_yours(studio):
    a = start("claude", NEMOTRON)
    found, model = llm.selection(a)
    assert llm.claude_environment(found, model, llm.passed_names(a))["CLAUDE_CODE_DISABLE_THINKING"] == "1"
    mine = start("claude", NEMOTRON, env=["CLAUDE_CODE_DISABLE_THINKING"])
    found, model = llm.selection(mine)
    assert "CLAUDE_CODE_DISABLE_THINKING" not in llm.claude_environment(found, model, llm.passed_names(mine))
    report = llm.profile_report(mine, found, model)
    assert "env.CLAUDE_CODE_DISABLE_THINKING" in report["overridden"]
    other = start("claude", QCN)
    found, model = llm.selection(other)
    assert "CLAUDE_CODE_DISABLE_THINKING" not in llm.claude_environment(found, model, set())


def test_the_report_names_applied_and_overridden_keys(studio):
    run("set", NAME, "--param", "temperature=0.3")
    a = start(llm_param=["loop_guard.steps=60"])
    found, model = llm.selection(a)
    report = llm.profile_report(a, found, model)
    assert report["id"] == "qwen3-coder-next/opencode" and report["status"] == "verified" and report["pair"]
    assert report["applied"] == ["top_p", "top_k", "repeat_penalty", "max_tokens", "num_ctx"]
    assert report["overridden"] == ["temperature", "loop_guard.steps"]
    line = llm.profile_lines(report, NAME)[0]
    assert line == ("agent-sandbox: --llm studio: profile qwen3-coder-next × opencode (verified): top_p 0.8, top_k 20, "
                    "repeat_penalty 1.05, max_tokens 16384, context 131072; yours win over its temperature 0.7, "
                    "loop_guard.steps 150 "
                    "(--no-llm-profile switches it off)")
    off = start(no_llm_profile=True)
    found, model = llm.selection(off)
    report = llm.profile_report(off, found, model)
    assert report["enabled"] is False and report["applied"] == []
    assert llm.profile_lines(report, NAME) == [
        "agent-sandbox: note: --llm studio: profile qwen3-coder-next × opencode (verified) is off (--no-llm-profile)"]


def test_laguna_warns_about_many_mcp_servers(studio):
    a = start("opencode", "poolside/laguna-s-2.1", mcp=["files", "exec", "web-fetch"])
    found, model = llm.selection(a)
    report = llm.profile_report(a, found, model)
    assert any("3 MCP servers are attached" in warning for warning in report["warnings"])
    one = start("opencode", "poolside/laguna-s-2.1", mcp=["files", "exec"])  # round 1: two pass (mcp_max 2)
    found, model = llm.selection(one)
    assert not any("attached" in warning for warning in llm.profile_report(one, found, model)["warnings"])


def test_ollama_keeps_only_what_its_api_takes(tmp_path):
    run("add", "ol", "--kind", "ollama", "--url", "http://127.0.0.1:9", "--model", QCN)
    a = start(llm=f"ol:{QCN}")
    found, model = llm.selection(a)
    assert llm.profile_params(found, model) == {"temperature": 0.7, "top_p": 0.8, "max_tokens": 16384,
                                                "num_ctx": 131072}
    report = llm.profile_report(a, found, model)
    assert report["not_taken"] == ["repeat_penalty", "top_k"]
    assert llm.modelfile_hint(found, model, llm.effective_params(found, model, profile=False)[0]) is None


def test_lmstudios_load_uses_the_profiles_window_at_most_the_models_maximum(studio, monkeypatch):
    """keep_loaded: the model loaded with the profile's 131072 (parallel 1); a model of a smaller maximum with that."""
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH")
    loads = []

    class Client:
        def json(self, path, body=None):
            return {"models": [{"key": QCN, "max_context_length": 262144, "loaded_instances": []},
                               {"key": "poolside/laguna-s-2.1", "max_context_length": 65536,
                                "loaded_instances": []}]}

        def call(self, path, body):
            loads.append(body)
            return 200, {}
    for model in (QCN, "poolside/laguna-s-2.1"):
        a = start(model=model)
        found, chosen = llm.selection(a)
        assert "loaded" in llm.keep_loaded(found, chosen, a, client=Client(), runs={})
    assert loads == [{"model": QCN, "context_length": 131072, "parallel": 1},
                     {"model": "poolside/laguna-s-2.1", "context_length": 65536, "parallel": 1}]


def test_llm_profiles_lists_them(studio):
    text = run("profiles")
    assert "qwen3-coder-next × opencode" in text and "verified" in text and "qwen3.8-27b × *" in text
    shown = json.loads(run("profiles", QCN, "--agent", "opencode", "--json"))
    assert [row["id"] for row in shown["profiles"]] == ["qwen3-coder-next/opencode"]
    detail = run("profiles", NEMOTRON, "--agent", "claude")
    assert "CLAUDE_CODE_DISABLE_THINKING=1" in detail and "reasoning:" in detail
    assert "sandbox-ai sets itself: ENABLE_TOOL_SEARCH=false" in detail
    assert "no profile for llama3:8b" in run("profiles", "llama3:8b")


def test_llm_profiles_says_what_we_know_of_one_pair(studio):
    """User: "what we know and what we dont with short bullet points and what is working and what not" - for one
    model (and agent): ✓ works, ✗ does not work, ? not known yet; a pair without an entry says it was not tested,
    a model of no family that nothing is known about it. The whole listing stays one line per pair."""
    lines = run("profiles", QCN, "--agent", "opencode").splitlines()
    assert "    what we know:" in lines
    assert "      ✓ smoke test: 2 of 2 passed (23-27 s)" in lines
    assert any(line.startswith("      ✗ debug task: 2 of 3 runs unsolved") for line in lines)
    assert "      ? Ollama, llama.cpp and vLLM servers: not measured (LM Studio only)" in lines
    assert lines.index("    what we know:") < lines.index("      ✓ smoke test: 2 of 2 passed (23-27 s)")
    untested = run("profiles", QCN, "--agent", "letta")
    assert "    no data yet for this pair: the model's settings for every agent apply" in untested.splitlines()
    assert "✓ smoke test passed in all nine agents measured (16-45 s in round 1)" in untested  # the family's own
    assert "✓ smoke test: 2 of 2" not in untested
    every = run("profiles", "nvidia-nemotron-3-super-120b-a12b-mlx").splitlines()
    assert "      ✗ Messages API with tools: HTTP 500 (chat template error) for every schema tried" in every
    assert "nothing is known about this model" in run("profiles", "llama3:8b")
    assert not any(line.startswith("      ✓ ") for line in run("profiles").splitlines())
    assert "what we know" not in run("profiles")
    shown = json.loads(run("profiles", QCN, "--agent", "opencode", "--json"))["profiles"][0]
    assert "smoke test: 2 of 2 passed (23-27 s)" in shown["works"] and shown["fails"] and shown["unknown"]


# --- the real ./sandbox-ai ------------------------------------------------------------------------------------------

def e2e_host(tmp_path, *images):
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image(*images)
    return host


def test_e2e_the_start_says_it_and_the_dry_run_shows_it(tmp_path):
    """User: "qwen next coder and opencode 1", "same model other harness", "qwen38-27B and codex": the real
    ./sandbox-ai's dry run says the profile on stderr and has it in llm.profile; yours win; --no-llm-profile and
    a project file's options switch it off."""
    server, port = model_mock.start(models=(QCN, Q38), lmstudio=True, max_context=262144)
    try:
        host = e2e_host(tmp_path, "localhost/opencode-sandbox:latest", "localhost/codex-sandbox:latest",
                        "localhost/goose-sandbox:latest")
        env = {"SANDBOX_AI_SECRETS": "file"}
        added = host.run("llm", "add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model",
                         QCN, env=env)
        assert added.returncode == 0, added.stderr

        dry = host.run("opencode", "--llm", f"{NAME}:{QCN}", "--dry-run", env=env)
        assert dry.returncode == 0, dry.stderr
        assert "--llm studio: profile qwen3-coder-next × opencode (verified): temperature 0.7, top_p 0.8, top_k 20, " \
               "repeat_penalty 1.05, max_tokens 16384, context 131072, loop_guard.steps 150" in dry.stderr
        profile = json.loads(dry.stdout)["llm"]["profile"]
        assert profile["id"] == "qwen3-coder-next/opencode" and profile["status"] == "verified"
        assert profile["applied"] == ["temperature", "top_p", "top_k", "repeat_penalty", "max_tokens", "num_ctx",
                                      "loop_guard.steps"] and profile["overridden"] == []

        mine = host.run("opencode", "--llm", f"{NAME}:{QCN}", "--llm-param", "temperature=0.2", "--dry-run", env=env)
        assert mine.returncode == 0, mine.stderr
        profile = json.loads(mine.stdout)["llm"]["profile"]
        assert profile["overridden"] == ["temperature"] and "temperature" not in profile["applied"]
        assert "yours win over its temperature 0.7" in mine.stderr

        other = host.run("goose", "--llm", f"{NAME}:{QCN}", "--dry-run", env=env)  # same model, another harness
        assert other.returncode == 0, other.stderr
        assert json.loads(other.stdout)["llm"]["profile"]["id"] == "qwen3-coder-next/goose"
        assert "profile qwen3-coder-next × goose (verified)" in other.stderr  # derived: round 8, 10 tier-1 runs

        codex = host.run("codex", "--llm", f"{NAME}:{Q38}", "--dry-run", env=env)  # never run in Codex: no data yet
        assert codex.returncode == 0, codex.stderr
        profile = json.loads(codex.stdout)["llm"]["profile"]
        assert profile["id"] == "qwen3.8-27b/codex" and profile["status"] == "untested"
        assert profile["values"]["num_ctx"] == 49152
        assert "--llm studio: profile qwen3.8-27b × codex (no data yet in Codex)" in codex.stderr
        assert "WARNING: --llm studio: profile" not in codex.stderr

        off = host.run("opencode", "--llm", f"{NAME}:{QCN}", "--no-llm-profile", "--dry-run", env=env)
        assert off.returncode == 0, off.stderr
        profile = json.loads(off.stdout)["llm"]["profile"]
        assert profile["enabled"] is False and profile["applied"] == []
        assert "is off (--no-llm-profile)" in off.stderr and "context 131072" not in off.stderr
        short = host.run("opencode", "--llm", f"{NAME}:{QCN}", "-nl", "--dry-run", env=env)
        assert json.loads(short.stdout)["llm"]["profile"]["enabled"] is False

        project_file = host.workspace / ".sandbox-ai.json"
        project_file.write_text(json.dumps({"version": 1, "agent": "opencode", "options": ["--no-llm-profile"]}))
        trusted = subprocess.run(host.command("wizard", "trust", str(project_file)), input="y\n", text=True,
                                 capture_output=True, env={**host.env, **env}, cwd=host.workspace, timeout=120)
        assert trusted.returncode == 0, trusted.stderr + trusted.stdout
        project = host.run("opencode", "--llm", f"{NAME}:{QCN}", "--dry-run", env=env)
        assert project.returncode == 0, project.stderr
        assert json.loads(project.stdout)["llm"]["profile"]["enabled"] is False
    finally:
        server.shutdown()


def test_e2e_the_written_configuration_carries_the_profile(tmp_path):
    """A real start of opencode (Podman faked) against the LM Studio-like server: the model is loaded with the
    profile's window and parallel 1, OpenCode is told it and its build agent gets the profile's 150 steps."""
    log = tmp_path / "mock.jsonl"
    server, port = model_mock.start(models=(QCN,), lmstudio=True, max_context=262144, log=str(log), log_bodies=True)
    try:
        host = e2e_host(tmp_path, "localhost/opencode-sandbox:latest")
        env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "", "SANDBOX_AI_SECRETS": "file"}
        assert host.run("llm", "add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model",
                        QCN, env=env).returncode == 0
        kept = tmp_path / "kept"
        done = host.run("opencode", "--no-tmux", "--llm", f"{NAME}:{QCN}",
                        env={**env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
        assert done.returncode == 0, done.stderr
        assert "profile qwen3-coder-next × opencode (verified)" in done.stderr
        loads = [json.loads(line) for line in log.read_text().splitlines()]
        bodies = [entry.get("body") for entry in loads if str(entry.get("path", "")).endswith("/models/load")]
        assert bodies and bodies[0]["context_length"] == 131072 and bodies[0]["parallel"] == 1, loads
        config = json.loads(next(host.directory.rglob("sandbox-ai-llm.json")).read_text())
        assert config["provider"]["llm-studio"]["models"][QCN]["limit"]["context"] == 131072
        assert config["agent"]["build"]["steps"] == 150
    finally:
        server.shutdown()


def test_a_damaged_table_never_stops_a_start(studio, tmp_path, monkeypatch):
    """The table unreadable (damaged, missing): the start goes on without profiles, as before 0.1.98."""
    damaged = tmp_path / "profiles.json"
    damaged.write_text('{"schema": 1, "families": [{"id": "x"}]}')
    monkeypatch.setenv("SANDBOX_AI_MODEL_PROFILES", str(damaged))
    a = start()
    found, model = llm.selection(a)
    assert llm.effective_params(found, model)[0] == {} and llm.profile_report(a, found, model) is None
    assert llm.loop_config(found)["steps"] == llm.LOOP_DEFAULTS["steps"]
    monkeypatch.setenv("SANDBOX_AI_MODEL_PROFILES", str(tmp_path / "missing.json"))
    assert llm.proxy_params(a, found, model)["presets"][QCN] == llm.family_preset(QCN)
