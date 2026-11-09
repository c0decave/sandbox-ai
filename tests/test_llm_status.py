"""What an LLM server has loaded and uses now (sandbox-ai llm status, wizard models --status), against fake servers of
every kind on this machine's loopback (no real network): the loaded models with their sizes, VRAM/RAM, KV cache and
requests as each server's API reports them, "not reported" for the rest, the key sent as for the listing, short
timeouts (a server that hangs or is down holds up no other), the JSON and the text, --watch; and the --llm a start
records for the app and the web (sandbox-ai list --json: "llm")."""
import http.server
import importlib.util
import json
from pathlib import Path
import socket
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_llm_status_test", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
KEY = "sk-llm-fixture-0123456789abcdef"  # shaped like a key, valid nowhere
GB = 1_000_000_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def secret(name, value):
    llm.load_secret_store_module().FileStore().set(name, value)


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    return status, "\n".join(lines)


VLLM_METRICS = """# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3-Coder-30B"} 2.0
vllm:num_requests_running{engine="1",model_name="Qwen/Qwen3-Coder-30B"} 1.0
vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen3-Coder-30B"} 4.0
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen/Qwen3-Coder-30B"} 0.125
vllm:kv_cache_usage_perc{engine="1",model_name="Qwen/Qwen3-Coder-30B"} 0.375
vllm:prompt_tokens_total{engine="0",model_name="Qwen/Qwen3-Coder-30B"} 12345.0
"""
# vLLM before V1: gpu_cache_usage_perc and cpu_cache_usage_perc; a NaN is no number
VLLM_V0_METRICS = """vllm:num_requests_running{model_name="m"} 0.0
vllm:num_requests_waiting{model_name="m"} 0.0
vllm:gpu_cache_usage_perc{model_name="m"} 0.5
vllm:cpu_cache_usage_perc{model_name="m"} NaN
"""
LLAMACPP_METRICS = """# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 1
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 3
llamacpp:n_tokens_max 4096
"""

ROUTES = {
    "ollama": {
        "/api/tags": {"models": [
            {"name": "qwen2.5-coder:7b", "size": 4683087332, "modified_at": "2026-10-01T10:00:00Z",
             "details": {"family": "qwen2", "parameter_size": "7.6B", "quantization_level": "Q4_K_M"}},
            {"name": "llama3.2:3b", "size": 2019393189, "details": {}}]},
        "/api/ps": {"models": [{"name": "qwen2.5-coder:7b", "model": "qwen2.5-coder:7b", "size": 6 * GB,
                                "size_vram": 5 * GB, "context_length": 8192,
                                "expires_at": "2026-11-09T13:05:00.123+01:00", "details": {}}]}},
    "lmstudio": {
        "/api/v0/models": {"data": [{"id": "qwen/qwen3-8b", "type": "llm", "state": "loaded", "quantization": "Q4_K_M",
                                     "arch": "qwen3", "max_context_length": 32768},
                                    {"id": "google/gemma-3-12b", "type": "vlm", "state": "not-loaded",
                                     "quantization": "Q8_0", "max_context_length": 131072},
                                    {"id": "nomic-embed", "type": "embeddings", "state": "not-loaded"}]},
        "/api/v1/models": {"models": [
            {"type": "llm", "key": "qwen/qwen3-8b", "size_bytes": 5 * GB, "params_string": "8B",
             "quantization": {"name": "Q4_K_M", "bits_per_weight": 4}, "max_context_length": 32768,
             "loaded_instances": [{"id": "qwen/qwen3-8b", "config": {"context_length": 4096}}]},
            {"type": "llm", "key": "google/gemma-3-12b", "size_bytes": 13 * GB, "params_string": "12B",
             "loaded_instances": []},
            {"type": "embedding", "key": "nomic-embed", "size_bytes": 84 * 1_000_000, "loaded_instances": []}]}},
    "llamacpp": {
        "/v1/models": {"object": "list", "data": [{"id": "Qwen3-Coder-30B-A3B-Q4_K_M.gguf",
                                                   "meta": {"n_ctx_train": 262144, "size": 18556689568}}]},
        "/props": {"total_slots": 4, "model_path": "/m/q.gguf", "default_generation_settings": {"n_ctx": 32768}},
        "/slots": [{"id": 0, "n_ctx": 8192, "is_processing": True}, {"id": 1, "n_ctx": 8192, "is_processing": False},
                   {"id": 2, "n_ctx": 8192, "is_processing": True}, {"id": 3, "n_ctx": 8192, "is_processing": False}],
        "/metrics": LLAMACPP_METRICS},
    # llama.cpp in router mode: a status per model, and the memory a loaded instance allocated per device
    "llamacpp-router": {
        "/v1/models": {"object": "list", "data": [
            {"id": "big", "status": {"value": "loaded", "args": ["llama-server", "-ctx", "4096"]},
             "meta": {"size": 9 * GB},
             "memory": {"status": "measured", "effective": {"n_ctx": 4096, "devices": [
                 {"kind": "device", "name": "CUDA0", "total": 24 * GB, "free": 10 * GB, "model": 8 * GB,
                  "context": 1 * GB, "compute": 500_000_000},
                 {"kind": "host", "name": "CPU", "model": 1 * GB, "context": 0, "compute": 200_000_000}]}}},
            {"id": "small", "status": {"value": "unloaded"}, "meta": None,
             "memory": {"status": "unmeasured", "effective": None}}]}},
    "vllm": {
        "/v1/models": {"data": [{"id": "Qwen/Qwen3-Coder-30B", "max_model_len": 65536}]},
        "/metrics": VLLM_METRICS},
    "vllm-v0": {"/v1/models": {"data": [{"id": "m", "max_model_len": 4096}]}, "/metrics": VLLM_V0_METRICS},
    "openai-compatible": {"/v1/models": {"data": [{"id": "gpt-oss-20b"}]}},
}


def fake_server(routes, key=None, delay=0):
    """A server on 127.0.0.1 answering ROUTES (a dict as JSON, a str as text); with KEY, only to Bearer KEY. Each
    request waits DELAY seconds first (a server that hangs)."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if delay:
                time.sleep(delay)
            if key and self.headers.get("Authorization") != f"Bearer {key}":
                status, data, kind = 401, b'{"error": "unauthorized"}', "application/json"
            elif self.path in routes:
                value = routes[self.path]
                status = 200
                data, kind = (value.encode(), "text/plain; version=0.0.4") if isinstance(value, str) else \
                    (json.dumps(value).encode(), "application/json")
            else:
                status, data, kind = 404, b'{"error": "not found"}', "application/json"
            try:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def servers():
    started = []

    def start(routes, **kw):
        server, seen = fake_server(routes, **kw)
        started.append(server)
        return f"http://localhost:{server.server_address[1]}", seen
    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def add(name, kind, url, *extra):
    status, out = run("add", name, "--kind", kind, "--url", url, *extra)
    assert status == 0, out


def status_of(name):
    status, out = run("status", name, "--json")
    found = json.loads(out)
    assert [entry["name"] for entry in found] == [name]
    return status, found[0]


def test_ollama_reports_the_loaded_models_with_vram_and_ram(servers):
    secret("K", KEY)
    url, seen = servers(ROUTES["ollama"], key=KEY)
    add("box", "ollama", url, "--api-key-secret", "K")
    status, entry = status_of("box")
    assert status == 0 and entry["reachable"] is True and entry["error"] is None and entry["models"] == 2
    assert entry["loaded"] == [{"name": "qwen2.5-coder:7b", "size": 4683087332, "memory_bytes": 6 * GB,
                                "vram_bytes": 5 * GB, "ram_bytes": 1 * GB, "context": 8192,
                                "expires_at": "2026-11-09T13:05:00.123+01:00"}]
    assert entry["usage"] == {"vram_bytes": 5 * GB, "ram_bytes": 1 * GB}
    assert entry["reported_by"] == ["Ollama /api/ps"]
    assert entry["not_reported"] == ["kv_cache", "requests", "totals"], "never guessed"
    assert entry["read_at"].endswith("Z")
    assert sorted(path for path, _ in seen) == ["/api/ps", "/api/tags"], "each path once"
    assert all(auth == f"Bearer {KEY}" for _, auth in seen), "the key goes as for the listing"
    assert llm.endpoint("box")["models"] == [], "a status read writes nothing (a watch reads often)"
    status, out = run("status", "box")
    assert status == 0
    lines = out.splitlines()
    assert lines[0] == f"box: Ollama at {url}"
    assert lines[1].startswith("  reachable (read 20") and lines[1].endswith("): 2 model(s), 1 loaded")
    assert lines[2] == ("  loaded: qwen2.5-coder:7b  6.0 GB in memory (5.0 GB VRAM, 1.0 GB RAM), context 8192, "
                        "unloaded at 2026-11-09T13:05:00.123+01:00")
    assert lines[3] == "  usage: VRAM 5.0 GB, RAM 1.0 GB"
    assert lines[4] == "  not reported by its API: KV cache, requests, the host's total RAM/VRAM"


def test_ollama_with_nothing_loaded_uses_nothing(servers):
    url, _ = servers({**ROUTES["ollama"], "/api/ps": {"models": []}})
    add("box", "ollama", url)
    _, entry = status_of("box")
    assert entry["loaded"] == [] and entry["usage"] == {"vram_bytes": 0, "ram_bytes": 0}


def test_lm_studio_reports_its_loaded_instances_and_sizes_not_its_memory(servers):
    url, _ = servers(ROUTES["lmstudio"])
    add("lm", "lmstudio", url)
    _, entry = status_of("lm")
    assert entry["loaded"] == [{"name": "qwen/qwen3-8b", "size": 5 * GB, "context": 4096}]
    assert entry["usage"] == {} and entry["models"] == 2, "LLMs only (no embeddings)"
    assert entry["not_reported"] == ["memory", "kv_cache", "requests", "totals"]
    status, out = run("models", "lm", "--json")
    models = {m["name"]: m for m in json.loads(out)}
    assert models["qwen/qwen3-8b"]["size"] == 5 * GB and models["qwen/qwen3-8b"]["loaded"] is True
    assert models["google/gemma-3-12b"]["size"] == 13 * GB and models["google/gemma-3-12b"]["parameters"] == "12B"
    status, out = run("models", "lm")
    assert "SIZE" in out.splitlines()[0] and "LOADED" in out.splitlines()[0] and "13.0 GB" in out


def test_older_lm_studio_has_no_sizes_and_says_what_is_loaded(servers):
    url, _ = servers({"/api/v0/models": ROUTES["lmstudio"]["/api/v0/models"]})
    add("lm", "lmstudio", url)
    _, entry = status_of("lm")
    assert entry["loaded"] == [{"name": "qwen/qwen3-8b"}] and entry["reported_by"] == ["LM Studio /api/v0/models"]
    status, out = run("models", "lm")
    assert "SIZE" not in out.splitlines()[0], "no column for what the server does not report"


def test_lm_studio_with_its_new_api_only(servers):
    url, _ = servers({"/api/v1/models": ROUTES["lmstudio"]["/api/v1/models"]})
    add("lm", "lmstudio", url)
    status, out = run("models", "lm", "--json")
    assert [(m["name"], m["size"], m["loaded"], m.get("quantization")) for m in json.loads(out)] == [
        ("qwen/qwen3-8b", 5 * GB, True, "Q4_K_M"), ("google/gemma-3-12b", 13 * GB, False, None)]


def test_llama_cpp_reports_slots_and_with_metrics_the_requests(servers):
    url, _ = servers(ROUTES["llamacpp"])
    add("cpp", "llamacpp", url)
    _, entry = status_of("cpp")
    assert entry["loaded"] == [{"name": "Qwen3-Coder-30B-A3B-Q4_K_M.gguf", "size": 18556689568, "context": 32768}]
    assert entry["usage"] == {"slots_total": 4, "requests_running": 1, "requests_waiting": 3}, \
        "/metrics (llama-server --metrics) over /slots"
    assert entry["not_reported"] == ["memory", "kv_cache", "totals"]
    url, _ = servers({k: v for k, v in ROUTES["llamacpp"].items() if k != "/metrics"})
    add("plain", "llamacpp", url)
    _, entry = status_of("plain")
    assert entry["usage"] == {"slots_total": 4, "requests_running": 2}, "without --metrics: the slots processing"
    status, out = run("status", "plain")
    assert "(llama-server --metrics also reports the requests waiting)" in out


def test_llama_cpp_router_reports_the_memory_and_the_devices_totals(servers):
    url, _ = servers(ROUTES["llamacpp-router"])
    add("router", "llamacpp", url)
    _, entry = status_of("router")
    assert entry["loaded"] == [{"name": "big", "size": 9 * GB, "memory_bytes": 10_700_000_000,
                                "vram_bytes": 9_500_000_000, "ram_bytes": 1_200_000_000, "context": 4096}]
    assert entry["usage"]["vram_bytes"] == 9_500_000_000 and entry["usage"]["ram_bytes"] == 1_200_000_000
    assert entry["usage"]["vram_total_bytes"] == 24 * GB and entry["usage"]["vram_free_bytes"] == 10 * GB
    assert "totals" not in entry["not_reported"], "its API gives the device's total: shown"


def test_vllm_reports_kv_cache_and_requests_not_memory(servers):
    url, _ = servers(ROUTES["vllm"])
    add("gpu", "vllm", url)
    _, entry = status_of("gpu")
    assert entry["loaded"] == [{"name": "Qwen/Qwen3-Coder-30B", "context": 65536}]
    assert entry["usage"] == {"requests_running": 3, "requests_waiting": 4, "kv_cache_percent": 37.5}, \
        "requests summed over engines; the fullest KV cache"
    assert entry["not_reported"] == ["memory", "totals"]
    url, _ = servers(ROUTES["vllm-v0"])
    add("old", "vllm", url)
    _, entry = status_of("old")
    assert entry["usage"] == {"requests_running": 0, "requests_waiting": 0, "kv_cache_percent": 50.0}, \
        "the names before V1; a NaN is no number"


def test_a_plain_openai_compatible_server_reports_nothing_of_it(servers):
    url, seen = servers(ROUTES["openai-compatible"])
    add("gw", "openai-compatible", url)
    _, entry = status_of("gw")
    assert entry["reachable"] and entry["models"] == 1 and entry["loaded"] is None and entry["usage"] == {}
    assert entry["not_reported"] == ["loaded", "memory", "kv_cache", "requests", "totals"]
    assert [path for path, _ in seen] == ["/v1/models"], "nothing else is asked"
    status, out = run("status", "gw")
    assert "1 model(s), what is loaded is not reported" in out
    assert "not reported by its API: loaded models, VRAM/RAM use, KV cache, requests" in out


def test_every_server_at_once_and_one_down_or_hanging_holds_up_none(servers, monkeypatch):
    monkeypatch.setattr(llm, "STATUS_TIMEOUT", 0.5)
    good, _ = servers(ROUTES["vllm"])
    slow, _ = servers(ROUTES["ollama"], delay=3)
    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        down = f"http://localhost:{sock.getsockname()[1]}"
    add("a-good", "vllm", good)
    add("b-slow", "ollama", slow)
    add("c-down", "lmstudio", down)
    began = time.monotonic()
    status, out = run("status", "--json")
    took = time.monotonic() - began
    found = {entry["name"]: entry for entry in json.loads(out)}
    assert status == 1, "not every server answered"
    assert took < 2.5, f"short timeouts, read at the same time: {took:.1f} s"
    assert found["a-good"]["reachable"] and found["a-good"]["usage"]["kv_cache_percent"] == 37.5
    assert not found["b-slow"]["reachable"] and "timed out" in found["b-slow"]["error"]
    assert not found["c-down"]["reachable"] and found["c-down"]["error"]
    assert found["c-down"]["loaded"] is None and found["c-down"]["not_reported"] == list(llm.REPORTABLE)


def test_a_server_that_answers_the_list_but_hangs_after_is_cut_short(servers, monkeypatch):
    """The optional reads stop at the status budget: what was read by then is shown."""
    monkeypatch.setattr(llm, "STATUS_TIMEOUT", 0.4)
    url, _ = servers(ROUTES["llamacpp"])
    add("cpp", "llamacpp", url)
    found = llm.endpoint("cpp")
    client = llm.Client(found, timeout=0.4)
    calls = []

    def text(path):
        calls.append(path)
        time.sleep(0.3)
        raise ValueError("timed out")

    def fetch(path):
        calls.append(path)
        if path == "/props":
            time.sleep(0.3)  # slow: the budget is spent after it
        return client.json(path)
    began = time.monotonic()
    models, status = llm.discover_status("llamacpp", fetch, text, budget=0.2)
    assert time.monotonic() - began < 2 and models and status["loaded"][0]["name"].startswith("Qwen3")
    assert calls == ["/v1/models", "/props"], "past the budget nothing more is asked (/slots, /metrics)"
    assert status["usage"] == {"slots_total": 4}, "what was read by then is shown"


def test_unusable_entries_and_unknown_names(servers):
    store = {"version": 1, "endpoints": {"bad": {"kind": "ollama", "url": "ftp://x"}}}
    llm.save_store(store)
    status, out = run("status", "--json")
    assert status == 1 and json.loads(out)[0]["name"] == "bad" and "unusable" in json.loads(out)[0]["error"]
    status, out = run("status", "nope")
    assert status == 1 and out == ""
    llm.save_store({"version": 1, "endpoints": {}})
    status, out = run("status")
    assert status == 0 and "no LLM endpoints" in out
    status, out = run("status", "--json")
    assert status == 0 and json.loads(out) == []


def test_watch_reads_again_until_ctrl_c(servers, monkeypatch):
    url, seen = servers(ROUTES["vllm"])
    add("gpu", "vllm", url)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(llm.time, "sleep", sleep)
    status, out = run("status", "gpu", "--json", "--watch", "5")
    lines = out.splitlines()
    assert status == 0 and sleeps == [5, 5] and len(lines) == 2, "one JSON line per reading"
    assert all(json.loads(line)[0]["usage"]["requests_running"] == 3 for line in lines)
    assert run("status", "gpu", "--watch", "1")[0] == 1, "at least 2 seconds between two readings"


def test_wizard_models_status_tests_a_server_not_registered(servers, capsys):
    spec = importlib.util.spec_from_file_location("sandbox_wizard_status_test", ROOT / "sandbox/wizard.py")
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    url, _ = servers(ROUTES["ollama"])
    assert wizard.cli(["models", "--kind", "ollama", "--url", url, "--status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in data["models"]] == ["qwen2.5-coder:7b", "llama3.2:3b"]
    assert data["models"][0]["size"] == 4683087332 and data["models"][0]["loaded"] is True
    assert data["status"]["loaded"][0]["vram_bytes"] == 5 * GB and data["status"]["usage"]["ram_bytes"] == 1 * GB
    assert wizard.cli(["models", "--kind", "ollama", "--url", url, "--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list), "without --status: the list as before"
    assert wizard.cli(["models", "--kind", "ollama", "--host", "localhost", "--port", url.rsplit(":", 1)[1],
                       "--status"]) == 0
    out = capsys.readouterr().out
    assert "LOADED" in out and "SIZE" in out and "usage: VRAM 5.0 GB, RAM 1.0 GB" in out
    add("box", "ollama", url)
    assert wizard.cli(["models", "--llm", "box", "--status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"]["usage"]["vram_bytes"] == 5 * GB


def test_metrics_are_read_by_name_only():
    found = llm.parse_metrics('a{x="1 2"} 3\n# a 9\nb 1e3\nc +Inf\na 4\nbad line\n', {"a", "b", "c"})
    assert found == {"a": [3.0, 4.0], "b": [1000.0]}
    status = llm.clean_status({"loaded": [{"name": "ok"}, {"name": "bad name\x1b"}, "x", {"name": "n", "size": -1,
                                                                                         "context": True}],
                               "usage": {"vram_bytes": "lots", "kv_cache_percent": 250, "requests_running": 2},
                               "reported_by": ["x\x1b[2J"]})
    assert status["loaded"] == [{"name": "ok"}, {"name": "n"}] and status["usage"] == {"requests_running": 2}
    assert status["reported_by"] == ["x\\x1b[2J"], "terminal-safe"


# --- the --llm a start records (sandbox-ai list --json "llm"): the app and the web show that server's status ---

def test_a_start_names_its_llm_server():
    spec = importlib.util.spec_from_file_location("sandbox_starts_llm_test", ROOT / "sandbox/starts.py")
    starts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(starts)
    assert starts.llm_choice(["--mode", "high", "--llm", "box:qwen2.5-coder:7b"]) == "box:qwen2.5-coder:7b"
    assert starts.llm_choice(["-lm", "box", "--llm=gpu:Qwen/Qwen3-Coder-30B"]) == "gpu:Qwen/Qwen3-Coder-30B"
    assert starts.llm_choice(["--llm", "--evil"]) is None and starts.llm_choice(["--llm"]) is None
    assert starts.llm_choice(None) is None and starts.llm_choice(["--mcp", "x"]) is None
    assert starts.record_llm({"llm": "box"}) == "box" and starts.record_llm({"llm": 5}) is None
    assert starts.record_llm({"llm": "bad name"}) is None and starts.record_llm(None) is None
    source = (ROOT / "sandbox/agent_sandbox.py").read_text()
    assert '"llm": getattr(a, "llm", None) or None,' in source, "run.json records --llm"
