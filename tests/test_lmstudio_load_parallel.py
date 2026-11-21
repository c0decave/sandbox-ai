"""LM Studio's explicit load carries "parallel": 1 (llm.lmstudio_load_body). An older LM Studio whose load endpoint
does not know the key answers HTTP 400 "unrecognized_keys": the load is sent once more without it, and the start (or
`llm load`) says so - before, the start fell back to JIT loading (after it had unloaded a JIT instance already).
Against the mock model server (tests/devtools/model_mock.py, load_refuses) and end to end through the real
./sandbox-ai (tests/live_fakes.py)."""
import importlib.util
import json
from pathlib import Path
import sys
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_llm_parallel_test", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    monkeypatch.setattr(llm, "running_runs", lambda: {})


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    return status, "\n".join(lines)


def records(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def loads(log):
    return [r["body"] for r in records(log) if r["path"] == "/api/v1/models/load"]


def paths(log):
    return [r["path"] for r in records(log) if r["path"].startswith("/api/v1/models/")]


def chat(port, model):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        assert answer.status == 200


@pytest.fixture
def old_studio(tmp_path):
    """An LM Studio whose load endpoint does not know "parallel"."""
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=("qwen3-8b",), lmstudio=True, log=str(log), log_bodies=True,
                                    load_refuses=("parallel",))
    assert run("add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")[0] == 0
    yield port, log, server
    server.shutdown()


def instances(server):
    with model_mock.LOCK:
        return [(i["model"], i["ttl"], i["context"]) for i in server.RequestHandlerClass.options.instances]


def test_a_start_loads_again_without_parallel_and_says_so(old_studio):
    port, log, server = old_studio
    run("set", "studio", "--model", "qwen3-8b", "--param", "num_ctx=16384")
    note = llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b", runs={})
    assert "loaded qwen3-8b for this start" in note, note
    assert 'does not know "parallel"' in note and "loaded without it" in note and "update LM Studio" in note
    assert loads(log) == [{"model": "qwen3-8b", "context_length": 16384, "parallel": 1},
                          {"model": "qwen3-8b", "context_length": 16384}]
    assert instances(server) == [("qwen3-8b", None, 16384)]  # explicit: no idle TTL


def test_a_jit_instance_is_replaced_once_never_left_unloaded(old_studio):
    """The JIT instance is unloaded first; the refused load does not leave the model to JIT loading again."""
    port, log, server = old_studio
    chat(port, "qwen3-8b")
    note = llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b", runs={})
    assert "loaded explicitly for this start" in note and "loaded without it" in note, note
    assert paths(log) == ["/api/v1/models/unload", "/api/v1/models/load", "/api/v1/models/load"]
    assert [(m, ttl) for m, ttl, _ in instances(server)] == [("qwen3-8b", None)]


def test_llm_load_loads_again_without_parallel_and_says_so(old_studio):
    _, log, server = old_studio
    status, out = run("load", "studio", "qwen3-8b", "--context", "8192")
    assert status == 0, out
    assert "loaded qwen3-8b explicitly as qwen3-8b, context 8192" in out
    assert 'does not know "parallel"' in out and "loaded without it" in out
    assert loads(log)[-1] == {"model": "qwen3-8b", "context_length": 8192}
    assert instances(server) == [("qwen3-8b", None, 8192)]


def test_another_refused_key_is_not_retried(tmp_path):
    """Only a refusal that names parallel is answered by a load without it: any other is the server's answer."""
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=("m",), lmstudio=True, log=str(log), log_bodies=True,
                                    load_refuses=("flash_attention",))
    try:
        assert run("add", "lms", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")[0] == 0
        run("set", "lms", "--param", "flash_attention=true")
        note = llm.keep_loaded(llm.endpoint("lms"), "m", runs={})
        assert "did not load m (HTTP 400)" in note and "without it" not in note, note
        assert len(loads(log)) == 1
    finally:
        server.shutdown()


def test_a_current_lm_studio_is_sent_one_load(tmp_path):
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=("m",), lmstudio=True, log=str(log), log_bodies=True)
    try:
        assert run("add", "lms", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")[0] == 0
        note = llm.keep_loaded(llm.endpoint("lms"), "m", runs={})
        assert "loaded m for this start" in note and "without it" not in note
        assert loads(log) == [{"model": "m", "parallel": 1}]
    finally:
        server.shutdown()


@pytest.mark.parametrize("status,body,retried", [
    (400, b'{"error": {"message": "Unrecognized key(s) in object: \'parallel\'", "code": "unrecognized_keys"}}', True),
    (422, b'unknown field `parallel`', True),
    (400, b'{"error": {"message": "Unrecognized key(s) in object: \'ttl\'"}}', False),
    (500, b'parallel', False),       # a server error is no refusal of the key
    (401, b'parallel', False),       # nor a refused API key
    (400, b'\xff\xfe parallel', True),
    (400, None, False),
])
def test_which_answers_mean_the_key_is_unknown(status, body, retried):
    sent = []

    class Client:
        def call(self, path, body_):
            sent.append(dict(body_))
            return (status, body) if len(sent) == 1 else (200, b'{"instance_id": "m"}')
    answer = llm.lmstudio_load(Client(), "m", 4096, {})
    assert (len(sent) == 2) is retried
    assert answer[0] == (200 if retried else status) and bool(answer[2]) is retried
    if retried:
        assert sent[1] == {"model": "m", "context_length": 4096}


# --- end to end: the real ./sandbox-ai -------------------------------------------------------------------------------

def test_e2e_a_start_against_an_older_lm_studio_loads_without_parallel(tmp_path):
    from live_fakes import Host
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=("qwen3-8b",), lmstudio=True, log=str(log), log_bodies=True,
                                    load_refuses=("parallel",))
    try:
        host = Host(tmp_path / "host")
        host.add_image("localhost/opencode-sandbox:latest")
        env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "", "SANDBOX_AI_SECRETS": "file"}
        added = host.run("llm", "add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}",
                         "--model", "qwen3-8b", env=env)
        assert added.returncode == 0, added.stderr
        done = host.run("opencode", "--no-tmux", "--llm", "studio:qwen3-8b", env={**env, "FAKE_PODMAN_RUN": "0:0"})
        assert done.returncode == 0, done.stderr
        assert "LM Studio loaded qwen3-8b for this start" in done.stderr, done.stderr
        assert "loaded without it" in done.stderr
        assert [("parallel" in body) for body in loads(log)] == [True, False]
        assert instances(server) and all(ttl is None for _, ttl, _ in instances(server))
    finally:
        server.shutdown()
