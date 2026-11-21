"""Hermes refuses a context window below 64,000 tokens (llm.HERMES_MIN_CONTEXT): sandbox-ai refuses such a start
before it runs, with the fix. With an Ollama --llm that check never triggered: Hermes was told the model's own
window (/api/show's model_info <arch>.context_length, 131072) or your num_ctx - but Ollama's OpenAI-compatible API
takes no num_ctx, and Ollama serves the agent's requests with the window it loads the model with: its Modelfile's
num_ctx, else OLLAMA_CONTEXT_LENGTH (4096 by default), which its API does not report. Hermes then failed with
"Something went wrong" at its first long turn. Now the start learns what Ollama serves (llm.ollama_served_context):
/api/ps of a loaded model, else the Modelfile's num_ctx, else the model loaded as the agent's first request would
load it (POST /api/generate without a prompt or options) and /api/ps read - and Hermes is told that, and refused
below 64k. Against the Ollama-like mock server (tests/devtools/model_mock.py: /api/ps, /api/show, /api/generate),
and end to end through the real ./sandbox-ai (tests/live_fakes.py)."""
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

SPEC = importlib.util.spec_from_file_location("sandbox_llm_hermes_ollama", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
MODEL = "qwen2.5-coder:7b"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    monkeypatch.setattr(llm, "running_runs", lambda: {})


def serve(tmp_path, **options):
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=(MODEL,), ollama_ps=True, log=str(log), log_bodies=True, **options)
    lines = []
    assert llm.main(["add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}", "--model", MODEL],
                    out=lines.append) == 0, lines
    return server, log


def requests(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def start(agent="hermes", *params):
    return argparse.Namespace(llm=f"ol:{MODEL}", agent=agent, env=[], secret_env=[], llm_param=list(params),
                              vpn=None, no_provider_defaults=False)


def test_ollama_with_its_default_window_is_refused_for_hermes(tmp_path):
    """Not loaded, no Modelfile num_ctx: before, Hermes was told 131072 (the model's own) and the start went on."""
    server, log = serve(tmp_path)
    try:
        with pytest.raises(ValueError) as caught:
            llm.hermes_config(start())
        text = str(caught.value)
        assert "Hermes needs a context window of at least 64,000" in text and "4096" in text, text
        assert "OLLAMA_CONTEXT_LENGTH=65536" in text and "PARAMETER num_ctx" in text
        assert "takes no num_ctx" in text
        loads = [r for r in requests(log) if r["path"] == "/api/generate"]
        assert loads and loads[0]["body"] == {"model": MODEL, "stream": False}, "loaded as the agent's request would"
    finally:
        server.shutdown()


def test_your_num_ctx_does_not_reach_ollama_so_it_does_not_count(tmp_path):
    server, _ = serve(tmp_path)
    try:
        with pytest.raises(ValueError, match="4096"):
            llm.hermes_config(start("hermes", "num_ctx=65536"))
    finally:
        server.shutdown()


def test_ollama_with_a_large_enough_window_starts_and_hermes_is_told_it(tmp_path):
    server, _ = serve(tmp_path, default_context=65536)  # OLLAMA_CONTEXT_LENGTH=65536 on the server
    try:
        assert llm.hermes_config(start())["model"]["context_length"] == 65536
    finally:
        server.shutdown()


def test_a_modelfiles_num_ctx_is_what_ollama_serves_and_nothing_is_loaded(tmp_path):
    server, log = serve(tmp_path, modelfile_context=65536)
    try:
        assert llm.hermes_config(start())["model"]["context_length"] == 65536
        assert not [r for r in requests(log) if r["path"] == "/api/generate"]
    finally:
        server.shutdown()


def test_a_model_loaded_already_is_read_not_loaded_again(tmp_path):
    server, log = serve(tmp_path, running={MODEL: {"context": 8192, "expires_at": "2318-01-01T00:00:00Z"}})
    try:
        with pytest.raises(ValueError, match="8192"):
            llm.hermes_config(start())
        assert not [r for r in requests(log) if r["path"] == "/api/generate"]
        assert llm.ollama_served_context(llm.endpoint("ol"), MODEL) == (8192, "loaded")
    finally:
        server.shutdown()


def test_hermes_as_a_helper_is_warned(tmp_path, capsys):
    server, _ = serve(tmp_path)
    try:
        config = llm.hermes_config(start("claude"))
        assert config["model"]["context_length"] == 4096
        assert "WARNING" in capsys.readouterr().err
    finally:
        server.shutdown()


def test_what_cannot_be_learnt_leaves_the_check_as_it_was(tmp_path, monkeypatch):
    """No answer (an old Ollama without context_length in /api/ps, a server that is down, behind a VPN): unknown."""
    endpoint = {"name": "x", "kind": "ollama", "url": "http://127.0.0.1:9",
                "parsed": llm.parse_url("http://127.0.0.1:9")}
    assert llm.ollama_served_context(endpoint, MODEL) == (None, None)
    assert llm.ollama_served_context(endpoint, MODEL, behind_vpn=True) == (None, None)
    assert llm.ollama_served_context({**endpoint, "kind": "lmstudio"}, MODEL) == (None, None)
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")
    assert llm.ollama_served_context(endpoint, MODEL) == (None, None)


def test_an_answer_that_is_no_window_is_not_taken(tmp_path):
    class Client:
        endpoint = {"url": "http://x"}

        def __init__(self, ps):
            self.ps = ps

        def json(self, path, parse=None, body=None):
            if path == "/api/ps":
                return self.ps
            if path == "/api/show":
                return {"parameters": "num_ctx 12"}
            return {"done": True}
    endpoint = {"name": "x", "kind": "ollama", "url": "http://x", "parsed": llm.parse_url("http://127.0.0.1:9")}
    for ps in ({"models": [{"name": MODEL, "context_length": True}]}, {"models": "x"}, [], None,
               {"models": [{"name": MODEL, "context_length": 1 << 40}]}):
        assert llm.ollama_served_context(endpoint, MODEL, client=Client(ps)) == (None, None), ps


# --- end to end: the real ./sandbox-ai -------------------------------------------------------------------------------

def test_e2e_hermes_on_ollama_with_its_default_window_is_refused(tmp_path):
    from live_fakes import Host
    server, port = model_mock.start(models=(MODEL,), ollama_ps=True)
    try:
        host = Host(tmp_path / "host")
        host.add_image("localhost/hermes-sandbox:latest")
        env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "", "SANDBOX_AI_SECRETS": "file"}
        added = host.run("llm", "add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}", "--model",
                         MODEL, env=env)
        assert added.returncode == 0, added.stderr
        refused = host.run("hermes", "--no-tmux", "--no-auth", "--llm", f"ol:{MODEL}",
                           env={**env, "FAKE_PODMAN_RUN": "0:0"})
        assert refused.returncode != 0, refused.stderr
        assert "Hermes needs a context window of at least 64,000" in refused.stderr and "4096" in refused.stderr
        assert "OLLAMA_CONTEXT_LENGTH" in refused.stderr and "Traceback" not in refused.stderr
    finally:
        server.shutdown()
