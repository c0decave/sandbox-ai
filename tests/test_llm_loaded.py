"""The models an LLM server has loaded, and loading and unloading them (sandbox-ai llm loaded|load|unload), against
the mock model server (tests/devtools/model_mock.py: LM Studio's REST API with JIT loading and its instances, Ollama's
/api/ps with keep_alive) and fake llama.cpp router and vLLM servers on this machine's loopback: instances with their
context, memory and how they stay loaded (JIT with an idle TTL, explicit), the running sandboxes using a model (run
registry: --llm NAME:MODEL), unload and reloads refused while one does unless --force, a JIT instance replaced and
never held twice; keep_loaded at a start (the JIT warning, the explicit reload when nobody else uses it); the same
through an endpoint's VPN; end to end as a command against the mock as a process of its own."""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_llm_loaded_test", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
sys.path.insert(0, str(ROOT / "tests/devtools"))
import model_mock  # noqa: E402

KEY = "sk-llm-fixture-0123456789abcdef"  # shaped like a key, valid nowhere


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    RUNS.clear()
    monkeypatch.setattr(llm, "running_runs", lambda: dict(RUNS))


RUNS = {}  # the run registry the tests set: {run: run.json}


def running(run, llm_value, name=None):
    RUNS[run] = {"run": run, "name": name or run, "llm": llm_value, "pid": 1}


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    return status, "\n".join(lines)


def run_err(capsys, *argv):
    status, out = run(*argv)
    return status, out, capsys.readouterr().err


def requests_of(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def chat(port, model):
    """A request of an agent: LM Studio loads the model JIT when it is not loaded."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        assert answer.status == 200


@pytest.fixture
def studio(tmp_path):
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=("qwen3-8b", "gemma-3-12b", "big-70b"), lmstudio=True, log=str(log),
                                    log_bodies=True, loaded={"gemma-3-12b": 8192})
    assert run("add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")[0] == 0
    yield port, log, server
    server.shutdown()


def loaded(name):
    status, out = run("loaded", name, "--json")
    assert status == 0, out
    return json.loads(out)


# --- llm loaded ------------------------------------------------------------------------------------------------------

def test_lm_studio_instances_jit_or_explicit_with_context_and_users(studio):
    port, log, _ = studio
    chat(port, "qwen3-8b")  # JIT: an instance with an idle TTL
    running("r1", "studio:qwen3-8b", name="alpha")
    running("r2", "other:qwen3-8b")  # another server's model of the same name: not a user
    entry = loaded("studio")
    rows = {i["instance"]: i for i in entry["instances"]}
    assert rows["qwen3-8b"]["kept"] == "jit" and rows["qwen3-8b"]["ttl_seconds"] == 3600
    assert rows["gemma-3-12b"]["kept"] == "explicit" and rows["gemma-3-12b"]["context"] == 8192
    assert rows["gemma-3-12b"]["size"] == 1 << 30 and "ttl_seconds" not in rows["gemma-3-12b"]
    assert entry["used_by"] == {"qwen3-8b": ["alpha"]} and entry["load"] and entry["unload"]
    assert "Auto-Evict" in entry["jit_warning"] and "Keeping models loaded" in entry["jit_warning"]
    status, text = run("loaded", "studio")
    assert status == 0 and "qwen3-8b  context 4096" in text and "JIT, unloaded after 3600 s idle" in text
    assert "used by alpha" in text and "explicit (stays until unloaded)" in text and "Auto-Evict" in text
    # Reading never loads or unloads anything (regression: sandboxes unloading each other's models).
    assert not [r for r in requests_of(log) if r["path"].startswith("/api/v1/models/")]


def test_llm_status_marks_a_jit_instance(studio):
    port, _, _ = studio
    chat(port, "qwen3-8b")
    status, out = run("status", "studio", "--json")
    rows = {m["name"]: m for m in json.loads(out)[0]["loaded"]}
    assert rows["qwen3-8b"].get("jit") is True and "jit" not in rows["gemma-3-12b"]
    assert "JIT: LM Studio may unload it" in run("status", "studio")[1]


def test_a_run_without_a_model_uses_the_endpoints_default(studio):
    run("add", "studio", "--kind", "lmstudio", "--url", llm.endpoint("studio")["url"], "--model", "gemma-3-12b",
        "--replace")
    running("r1", "studio", name="beta")
    running("r2", "studio:qwen3-8b,big-70b", name="gamma")
    running("r3", "not a value", name="broken")
    found = llm.model_users(llm.endpoint("studio"), RUNS)
    assert found == {"gemma-3-12b": ["beta"], "qwen3-8b": ["gamma"], "big-70b": ["gamma"]}
    assert "beta" not in str(llm.model_users(llm.endpoint("studio"), RUNS, exclude="r1"))


# --- llm load ---------------------------------------------------------------------------------------------------------

def test_load_explicitly_with_a_context_and_only_keys_lm_studio_knows(studio):
    port, log, _ = studio
    status, out = run("load", "studio", "qwen3-8b", "--context", "16384")
    assert status == 0 and "loaded qwen3-8b explicitly as qwen3-8b, context 16384" in out
    loads = [r for r in requests_of(log) if r["path"] == "/api/v1/models/load"]
    assert loads[-1]["body"] == {"model": "qwen3-8b", "context_length": 16384}
    rows = {i["instance"]: i for i in loaded("studio")["instances"]}
    assert rows["qwen3-8b"] == {**rows["qwen3-8b"], "kept": "explicit", "context": 16384}
    # Loaded explicitly already: nothing loaded again (a second load would be a second instance, qwen3-8b:2).
    status, out = run("load", "studio", "qwen3-8b")
    assert status == 0 and "loaded explicitly already" in out
    assert len([r for r in requests_of(log) if r["path"] == "/api/v1/models/load"]) == 1
    assert [i["instance"] for i in loaded("studio")["instances"] if i["model"] == "qwen3-8b"] == ["qwen3-8b"]


def test_load_with_another_context_than_the_explicit_one_is_refused(studio, capsys):
    status, out, err = run_err(capsys, "load", "studio", "gemma-3-12b", "--context", "32768")
    assert status == 1 and "loaded explicitly with context 8192" in err and "in memory twice" in err
    assert "sandbox-ai llm unload studio gemma-3-12b" in err


def test_the_context_defaults_to_num_ctx_of_the_models_parameters(studio):
    _, log, _ = studio
    run("set", "studio", "--model", "big-70b", "--param", "num_ctx=24576")
    assert run("load", "studio", "big-70b")[0] == 0
    assert [r for r in requests_of(log) if r["path"] == "/api/v1/models/load"][-1]["body"] == \
        {"model": "big-70b", "context_length": 24576}


def test_a_jit_instance_is_replaced_never_held_twice(studio):
    port, log, _ = studio
    chat(port, "qwen3-8b")
    status, out = run("load", "studio", "qwen3-8b")
    assert status == 0 and "unloaded the JIT instance qwen3-8b" in out and "loaded qwen3-8b explicitly" in out
    paths = [r["path"] for r in requests_of(log) if r["path"].startswith("/api/v1/models/")]
    assert paths == ["/api/v1/models/unload", "/api/v1/models/load"]  # the JIT one goes first
    mine = [i for i in loaded("studio")["instances"] if i["model"] == "qwen3-8b"]
    assert [(i["instance"], i["kept"], i["context"]) for i in mine] == [("qwen3-8b", "explicit", 4096)]


def test_reloading_a_model_a_running_sandbox_uses_needs_force(studio, capsys):
    port, log, _ = studio
    chat(port, "qwen3-8b")
    running("r1", "studio:qwen3-8b", name="alpha")
    status, out, err = run_err(capsys, "load", "studio", "qwen3-8b")
    assert status == 1 and "used by the running sandbox(es) alpha" in err and "--force" in err
    assert not [r for r in requests_of(log) if r["path"].startswith("/api/v1/models/")]
    assert run("load", "studio", "qwen3-8b", "--force")[0] == 0
    assert [i["kept"] for i in loaded("studio")["instances"] if i["model"] == "qwen3-8b"] == ["explicit"]


def test_what_lm_studio_cannot_do_is_refused_before_anything_is_sent(studio, capsys):
    _, log, _ = studio
    status, _, err = run_err(capsys, "load", "studio", "qwen3-8b", "--ttl", "600")
    assert status == 1 and "--ttl" in err and "lms load --ttl" in err
    status, _, err = run_err(capsys, "load", "studio", "no-such-model")
    assert status == 1 and "lists no model no-such-model" in err
    status, _, err = run_err(capsys, "load", "studio", "qwen3-8b", "--context", "12")
    assert status == 1 and "--context" in err
    assert not [r for r in requests_of(log) if r["method"] == "POST"]


# --- llm unload -------------------------------------------------------------------------------------------------------

def test_unload_an_instance_or_every_instance_of_a_model(studio, capsys):
    port, log, server = studio
    with model_mock.LOCK:  # a second instance, as a second explicit load in LM Studio makes it
        server.RequestHandlerClass.options.instances.append(
            {"id": "gemma-3-12b:2", "model": "gemma-3-12b", "context": 4096, "ttl": None})
    status, out = run("unload", "studio", "gemma-3-12b:2")
    assert status == 0 and out == "unloaded gemma-3-12b:2"
    assert [i["instance"] for i in loaded("studio")["instances"]] == ["gemma-3-12b"]
    server.RequestHandlerClass.options.instances.append(
        {"id": "gemma-3-12b:2", "model": "gemma-3-12b", "context": 4096, "ttl": None})
    assert run("unload", "studio", "gemma-3-12b")[0] == 0
    assert loaded("studio")["instances"] == []
    bodies = [r["body"] for r in requests_of(log) if r["path"] == "/api/v1/models/unload"]
    assert bodies == [{"instance_id": "gemma-3-12b:2"}, {"instance_id": "gemma-3-12b"},
                      {"instance_id": "gemma-3-12b:2"}]
    status, out = run("unload", "studio", "gemma-3-12b")
    assert status == 1 and "is not loaded" in out


def test_unload_of_a_model_in_use_is_refused_unless_forced(studio, capsys):
    _, log, _ = studio
    running("r1", "studio:gemma-3-12b", name="alpha")
    running("r2", "studio:gemma-3-12b,qwen3-8b", name="beta")
    status, _, err = run_err(capsys, "unload", "studio", "gemma-3-12b")
    assert status == 1 and "alpha, beta" in err and "--force" in err
    assert not [r for r in requests_of(log) if r["path"] == "/api/v1/models/unload"]
    status, out = run("unload", "studio", "gemma-3-12b", "--force")
    assert status == 0 and "used by alpha, beta" in out and loaded("studio")["instances"] == []


# --- keep_loaded at a start -------------------------------------------------------------------------------------------

def test_a_start_replaces_a_jit_instance_nobody_else_uses(studio):
    port, log, _ = studio
    chat(port, "qwen3-8b")
    run("set", "studio", "--model", "qwen3-8b", "--param", "num_ctx=16384")
    note = llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b", runs={})
    assert "loaded JIT" in note and "loaded explicitly for this start (context 16384)" in note
    assert "never in memory twice" in note
    paths = [r["path"] for r in requests_of(log) if r["path"].startswith("/api/v1/models/")]
    assert paths == ["/api/v1/models/unload", "/api/v1/models/load"]
    mine = [i for i in loaded("studio")["instances"] if i["model"] == "qwen3-8b"]
    assert [(i["kept"], i["context"]) for i in mine] == [("explicit", 16384)]
    assert llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b", runs={}) is None  # explicit now: left as it is


def test_a_start_warns_of_a_jit_instance_another_sandbox_uses(studio, capsys):
    port, log, _ = studio
    chat(port, "qwen3-8b")
    running("r1", "studio:qwen3-8b", name="alpha")
    note = llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b")
    assert note.startswith("WARNING: LM Studio has qwen3-8b loaded JIT")
    assert "LM Studio may unload it when another sandbox's model is loaded: Auto-Evict; see Help > Keeping models " \
           "loaded" in note and "alpha" in note and "sandbox-ai llm load studio qwen3-8b --force" in note
    assert not [r for r in requests_of(log) if r["path"].startswith("/api/v1/models/")]
    # The start's own run is not another sandbox.
    note = llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b", a=type("A", (), {"run_id": "r1", "llm": "studio"})())
    assert note and "loaded explicitly for this start" in note


def test_the_start_prints_the_warning_as_a_warning(studio, capsys, monkeypatch):
    monkeypatch.setattr(llm, "keep_loaded", lambda *a, **k: "WARNING: LM Studio has m loaded JIT (idle TTL): x")
    a = type("A", (), {"llm": "studio:qwen3-8b", "agent": "codex", "vpn": None, "llm_param": [], "loop_guard": []})()
    monkeypatch.setattr(llm, "selection", lambda a: (llm.endpoint("studio"), "qwen3-8b"))
    monkeypatch.setattr(llm, "agent_context", lambda *args, **kw: None)
    llm.prepare_home(a, Path("/nonexistent"), lambda data, home, rel: None, None, None)
    assert "agent-sandbox: WARNING: --llm studio: LM Studio has m loaded JIT" in capsys.readouterr().err


def test_an_older_lm_studio_without_ttls_is_left_as_it_is(studio):
    """LM Studio before remaining_ttl_seconds: every instance reads as explicit, nothing is reloaded (as before)."""
    port, log, server = studio
    server.RequestHandlerClass.options.jit_ttl = None
    chat(port, "qwen3-8b")
    assert llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b", runs={}) is None
    assert not [r for r in requests_of(log) if r["path"].startswith("/api/v1/models/")]


# --- Ollama -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def ollama(tmp_path):
    log = tmp_path / "ollama.jsonl"
    server, port = model_mock.start(models=("qwen2.5-coder:7b", "llama3.2:3b"), ollama_ps=True, log=str(log),
                                    log_bodies=True)
    assert run("add", "box", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}")[0] == 0
    yield port, log
    server.shutdown()


def test_ollama_load_with_context_and_ttl_and_unload(ollama):
    port, log = ollama
    status, out = run("load", "box", "qwen2.5-coder:7b", "--context", "16384", "--ttl", "600")
    assert status == 0 and "unloaded after 600 s idle" in out and "OLLAMA_KEEP_ALIVE=-1" in out
    body = [r["body"] for r in requests_of(log) if r["path"] == "/api/generate"][-1]
    assert body == {"model": "qwen2.5-coder:7b", "keep_alive": 600, "stream": False, "options": {"num_ctx": 16384}}
    item = loaded("box")["instances"][0]
    assert item["kept"] == "timed" and item["context"] == 16384 and item["memory_bytes"] == 4 << 30
    assert item["vram_bytes"] == 4 << 30 and item["ram_bytes"] == 0 and item["expires_at"].startswith("20")
    status, out = run("load", "box", "llama3.2:3b")  # without --ttl: until unloaded (keep_alive -1)
    assert status == 0 and "stays until it is unloaded" in out
    rows = {i["model"]: i for i in loaded("box")["instances"]}
    assert rows["llama3.2:3b"]["kept"] == "explicit" and "expires_at" not in rows["llama3.2:3b"]
    status, out = run("unload", "box", "qwen2.5-coder:7b")
    assert status == 0 and [r["body"] for r in requests_of(log) if r["path"] == "/api/generate"][-1] == \
        {"model": "qwen2.5-coder:7b", "keep_alive": 0, "stream": False}
    assert [i["model"] for i in loaded("box")["instances"]] == ["llama3.2:3b"]


def test_ollama_refuses_a_model_it_does_not_have(ollama, capsys):
    status, _, err = run_err(capsys, "load", "box", "nope:1b")
    assert status == 1 and "ollama pull nope:1b" in err


# --- llama.cpp's router, vLLM -----------------------------------------------------------------------------------------

def fake(routes, posts=None):
    """A server on 127.0.0.1 answering GET ROUTES and POST POSTS ({path: fn(body) -> answer}); [(path, body)] seen."""
    seen, auth = [], []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def parse_request(self):
            ok = super().parse_request()
            if ok:
                auth.append(self.headers.get("Authorization"))
            return ok

        def answer(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append((self.path, None))
            value = routes.get(self.path)
            self.answer(200 if value is not None else 404, value if value is not None else {"error": "no"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append((self.path, body))
            handler = (posts or {}).get(self.path)
            self.answer(200 if handler else 404, handler(body) if handler else {"error": "no"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.auth = auth
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_llama_cpp_router_lists_loads_and_unloads():
    state = {"big": "loaded", "small": "unloaded"}

    def models():
        return {"data": [{"id": name, "status": {"value": value, **({"args": ["llama-server", "-c", "8192"]}
                                                                    if value == "loaded" else {})}}
                         for name, value in state.items()]}

    class Routes(dict):
        def get(self, path, default=None):
            return models() if path == "/v1/models" else default

    def load(body):
        state[body["model"]] = "loaded"
        return {"success": True}

    def unload(body):
        state[body["model"]] = "unloaded"
        return {"success": True}
    server, seen = fake(Routes(), {"/models/load": load, "/models/unload": unload})
    try:
        run("add", "router", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{server.server_address[1]}")
        entry = loaded("router")
        assert [(i["model"], i["kept"], i.get("context")) for i in entry["instances"]] == [("big", "router", 8192)]
        assert run("load", "router", "small")[0] == 0 and ("/models/load", {"model": "small"}) in seen
        assert run("unload", "router", "big")[0] == 0 and ("/models/unload", {"model": "big"}) in seen
        assert [i["model"] for i in loaded("router")["instances"]] == ["small"]
        assert run("load", "router", "small", "--context", "4096")[0] == 1  # the router's preset decides
    finally:
        server.shutdown()


def test_a_single_model_llama_cpp_and_vllm_load_nothing(capsys):
    single, _ = fake({"/v1/models": {"data": [{"id": "q.gguf", "meta": {"size": 5}}]},
                      "/props": {"default_generation_settings": {"n_ctx": 32768}}})
    vllm, seen = fake({"/v1/models": {"data": [{"id": "Qwen/Qwen3", "max_model_len": 65536}]}})
    try:
        run("add", "one", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{single.server_address[1]}")
        run("add", "v", "--kind", "vllm", "--url", f"http://127.0.0.1:{vllm.server_address[1]}")
        assert [(i["model"], i["kept"], i["context"]) for i in loaded("one")["instances"]] == \
            [("q.gguf", "server", 32768)]
        status, _, err = run_err(capsys, "unload", "one", "q.gguf")
        assert status == 1 and "not in router mode" in err
        assert [(i["model"], i["context"]) for i in loaded("v")["instances"]] == [("Qwen/Qwen3", 65536)]
        status, _, err = run_err(capsys, "load", "v", "Qwen/Qwen3")
        assert status == 1 and "serves the model it was started with" in err
        assert not [path for path, body in seen if body is not None]
        assert "loads and unloads no model" in run("loaded", "v")[1]
    finally:
        single.shutdown()
        vllm.shutdown()


# --- the key, the VPN ---------------------------------------------------------------------------------------------

def test_the_key_goes_with_every_request_and_the_answer_is_checked():
    class Routes(dict):
        def get(self, path, default=None):
            return {"models": [{"key": "m", "type": "llm", "loaded_instances": [
                {"id": "m\x1b]0;evil\x07", "config": {"context_length": "lots"}}]},
                {"key": "../../x y", "loaded_instances": [{"id": "z"}]}]} if path == "/api/v1/models" else default
    server, _ = fake(Routes())
    try:
        llm.load_secret_store_module().FileStore().set("STUDIO_KEY", KEY)
        run("add", "keyed", "--kind", "lmstudio", "--url", f"http://localhost:{server.server_address[1]}",
            "--api-key-secret", "STUDIO_KEY")
        entry = loaded("keyed")
        # A name that is no model name and an instance id with control characters are not taken as given.
        assert entry["instances"] == [{"model": "m", "instance": "m", "kept": "explicit", "type": "llm"}]
        assert server.auth and all(value == f"Bearer {KEY}" for value in server.auth)
    finally:
        server.shutdown()


def test_through_the_endpoints_vpn(studio, monkeypatch):
    """An endpoint added with --vpn is read, loaded and unloaded through its tunnel (VPNClient), never from here."""
    port, _, _ = studio
    used = []

    class Tunnel:
        pass

    class FakeVPNClient(llm.Client):
        def __init__(self, endpoint, tunnel, get=None, timeout=llm.TIMEOUT):
            used.append(type(tunnel).__name__)
            super().__init__(endpoint, get, timeout)
    import contextlib

    @contextlib.contextmanager
    def through(endpoint, podman_url=None):
        yield Tunnel() if endpoint.get("vpn") else None
    monkeypatch.setattr(llm, "through_vpn", through)
    monkeypatch.setattr(llm, "VPNClient", FakeVPNClient)
    store = llm.load_store()
    store["endpoints"]["studio"]["vpn"] = "home"
    llm.save_store(store)
    assert loaded("studio")["instances"] and run("load", "studio", "qwen3-8b")[0] == 0
    assert run("unload", "studio", "qwen3-8b")[0] == 0 and used == ["Tunnel"] * 3


# --- end to end: the commands against the mock as a process of its own -------------------------------------------

def test_end_to_end_cli_against_the_mock(tmp_path):
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "e2e-cfg"), XDG_DATA_HOME=str(tmp_path / "e2e-data"),
               XDG_STATE_HOME=str(tmp_path / "e2e-state"), SANDBOX_AI_SECRETS="file")
    env.pop("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", None)
    mock = subprocess.Popen([sys.executable, str(ROOT / "tests/devtools/model_mock.py"), "--port", "0", "--lmstudio",
                             "--model", "qwen3-8b", "--model", "gemma-3-12b"], stdout=subprocess.PIPE, text=True)
    try:
        port = json.loads(mock.stdout.readline())["port"]

        def cli(*argv):
            done = subprocess.run([sys.executable, str(ROOT / "sandbox/llm.py"), *argv], env=env,
                                  capture_output=True, text=True, timeout=60)
            return done.returncode, done.stdout, done.stderr
        assert cli("add", "e2e", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")[0] == 0
        assert json.loads(cli("loaded", "e2e", "--json")[1])["instances"] == []
        chat(port, "qwen3-8b")
        status, out, _ = cli("loaded", "e2e")
        assert status == 0 and "JIT, unloaded after 3600 s idle" in out
        status, out, _ = cli("load", "e2e", "qwen3-8b", "--context", "8192")
        assert status == 0 and "unloaded the JIT instance" in out
        items = json.loads(cli("loaded", "e2e", "--json")[1])["instances"]
        assert [(i["instance"], i["kept"], i["context"]) for i in items] == [("qwen3-8b", "explicit", 8192)]
        status, _, err = cli("load", "e2e", "gemma-3-12b", "--ttl", "5")
        assert status == 1 and "--ttl" in err
        assert cli("unload", "e2e", "qwen3-8b")[0] == 0
        assert json.loads(cli("loaded", "e2e", "--json")[1])["instances"] == []
        status, out, err = cli("--help")
        assert "loaded" in out and "unload" in out
    finally:
        mock.terminate()
        mock.wait(timeout=10)
