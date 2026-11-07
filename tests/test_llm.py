"""LLM endpoints (sandbox-ai llm, --llm): the store and its checks, URLs and plain-http rules, the model listing of
each server kind against fake servers (keys, https with an endpoint's own CA, no redirects), `llm check`, and the
configuration each agent gets (golden files in tests/golden/llm; UPDATE_GOLDEN=1 rewrites them)."""
import argparse
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_llm_test", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
RT_SPEC = importlib.util.spec_from_file_location("sandbox_runtime_llm_test", ROOT / "sandbox/runtime.py")
rt = importlib.util.module_from_spec(RT_SPEC)
RT_SPEC.loader.exec_module(rt)
GOLDEN = ROOT / "tests/golden/llm"
KEY = "sk-llm-fixture-0123456789abcdef"  # shaped like a key, valid nowhere


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def secret(name, value):
    llm.load_secret_store_module().FileStore().set(name, value)


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    return status, "\n".join(lines)


# --- URLs and the plain-http rule ----------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://llm.example.org", ("https", "llm.example.org", 443, "", None, False, "https://llm.example.org")),
    ("https://LLM.example.org:8443/team/v1/", ("https", "llm.example.org", 8443, "/team", None, False,
                                                "https://llm.example.org:8443/team")),
    ("http://192.168.1.20:11434", ("http", "192.168.1.20", 11434, "", "192.168.1.20", False,
                                   "http://192.168.1.20:11434")),
    ("http://[fd7a:115c:a1e0::5]:8000", ("http", "fd7a:115c:a1e0::5", 8000, "", "fd7a:115c:a1e0::5", False,
                                         "http://[fd7a:115c:a1e0::5]:8000")),
    ("https://[2001:db8::5]", ("https", "2001:db8::5", 443, "", "2001:db8::5", False, "https://[2001:db8::5]")),
    ("http://localhost:1234", ("http", "localhost", 1234, "", None, True, "http://localhost:1234")),
    ("http://[::1]:8080", ("http", "::1", 8080, "", "::1", True, "http://[::1]:8080")),
    ("http://100.101.102.103:11434/gw", ("http", "100.101.102.103", 11434, "/gw", "100.101.102.103", False,
                                         "http://100.101.102.103:11434/gw")),
])
def test_parse_url(url, expected):
    parsed = llm.parse_url(url)
    assert (parsed["scheme"], parsed["host"], parsed["port"], parsed["prefix"], parsed["address"], parsed["loopback"],
            parsed["url"]) == expected


@pytest.mark.parametrize("url, match", [
    ("ftp://x.example", "expected http"), ("https://user:pw@x.example", "expected http"),
    ("https://x.example/?q=1", "expected http"), ("https://x.example/#f", "expected http"),
    ("https://x.example:0", "port out of range"), ("https://x.example:70000", "port out of range"),
    ("https://x.example/a/../b", "may not contain"), ("https://-x.example", "invalid host name"),
    ("https://[fe80::1%eth0]", "expected http"), ("https://[fe80::1]", "not an address"),
    ("http://127.0.0.2:80", "only 127.0.0.1"), ("http://0.0.0.0:80", "not an address"),
    ("https://999.1.1.1", "invalid IPv4"), ("https://[zz::1]", "expected http"), ("https://x.example\n", "expected"),
])
def test_parse_url_refusals(url, match):
    with pytest.raises(ValueError, match=match):
        llm.parse_url(url)


@pytest.mark.parametrize("url, addresses, refused", [
    ("http://10.0.0.5:8000", None, False), ("http://172.16.4.4", None, False), ("http://192.168.0.9", None, False),
    ("http://100.64.0.1", None, False), ("http://[fd00::5]", None, False), ("http://169.254.3.3", None, False),
    ("http://localhost:11434", None, False), ("https://203.0.113.5", None, False),
    ("http://203.0.113.5:8000", None, True), ("http://[2001:db8::5]", None, True), ("http://8.8.8.8", None, True),
    ("http://gpu.lan:8000", ["192.168.1.7"], False), ("http://gpu.lan:8000", ["192.168.1.7", "203.0.113.9"], True),
    ("http://gpu.example.org", [], False),  # not resolved yet: checked again at the start
])
def test_plain_http_problem(url, addresses, refused):
    assert bool(llm.plain_http_problem(llm.parse_url(url), addresses)) == refused


# --- the store -------------------------------------------------------------------------------------------------

def test_add_list_show_remove(tmp_path):
    secret("GPU_KEY", KEY)
    status, out = run("add", "gpu", "--kind", "ollama", "--url", "http://10.0.0.5:11434/v1", "--api-key-secret",
                      "GPU_KEY", "--model", "qwen2.5-coder:7b", "--header-secret", "X-Team=TEAM", "--vpn", "office")
    assert status == 0 and "trailing /v1 was dropped" in out and "no secret TEAM yet" in out
    assert "the key crosses your local network unencrypted" in out
    path = llm.store_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    stored = json.loads(path.read_text())["endpoints"]["gpu"]
    assert stored == {"kind": "ollama", "url": "http://10.0.0.5:11434", "api_key_secret": "GPU_KEY",
                      "headers": {"X-Team": "TEAM"}, "vpn": "office", "model": "qwen2.5-coder:7b"}
    assert KEY not in path.read_text()
    status, out = run("list", "--json")
    view = json.loads(out)[0]
    assert view["name"] == "gpu" and view["agents"] == ["claude", "codex", "opencode"] and KEY not in out
    status, out = run("show", "gpu")
    assert "key: secret GPU_KEY" in out and "header X-Team: secret TEAM" in out and "VPN profile: office" in out
    assert run("add", "gpu", "--kind", "vllm", "--url", "https://x.example")[0] == 1  # exists: needs --replace
    assert run("add", "gpu", "--kind", "vllm", "--url", "https://x.example", "--replace")[0] == 0
    assert run("remove", "gpu") == (0, "gpu removed") and run("remove", "gpu")[0] == 1
    assert run("list")[1].startswith("no LLM endpoints")


@pytest.mark.parametrize("argv, match", [
    (["--url", "http://203.0.113.5:8000"], "plain http to 203.0.113.5"),
    (["--url", "https://x.example", "--insecure-http"], "for plain http URLs"),
    (["--url", "https://x.example", "--header-secret", "Host=X"], "cannot set Host"),
    (["--url", "https://x.example", "--header-secret", "nope"], "HEADER=SECRET"),
    (["--url", "https://x.example", "--api-key-secret", "K", "--header-secret", "Authorization=X"],
     "drop that header secret"),
    (["--url", "http://10.0.0.5", "--ca", "/nonexistent.pem"], "No such file"),
])
def test_add_refusals(argv, match, capsys):
    assert run("add", "e", "--kind", "vllm", *argv)[0] == 1
    assert match in capsys.readouterr().err
    assert not llm.store_path().exists()


def test_insecure_http_warns_and_is_kept():
    status, out = run("add", "far", "--kind", "openai-compatible", "--url", "http://203.0.113.5:8000",
                      "--insecure-http")
    assert status == 0 and "WARNING: plain http" in out
    assert llm.endpoint("far")["insecure_http"] is True


def test_store_is_checked_on_every_use(tmp_path):
    path = llm.store_path()
    path.parent.mkdir(mode=0o700, parents=True)

    def store(record):
        path.write_text(json.dumps({"version": 1, "endpoints": {"e": record}}))
        path.chmod(0o600)
    store({"kind": "vllm", "url": "http://203.0.113.5:8000"})  # edited by hand: public plain http
    with pytest.raises(ValueError, match="plain http to 203.0.113.5"):
        llm.endpoint("e")
    store({"kind": "vllm", "url": "https://x.example", "evil": 1})
    with pytest.raises(ValueError, match="unknown fields"):
        llm.endpoint("e")
    store({"kind": "vllm", "url": "https://x.example", "ca": "../../etc/passwd"})
    with pytest.raises(ValueError, match="ca is the name of a file"):
        llm.endpoint("e")
    store({"kind": "vllm", "url": "https://x.example"})
    path.chmod(0o620)
    with pytest.raises(ValueError, match="writable by you only"):
        llm.endpoint("e")
    path.chmod(0o600)
    link = tmp_path / "elsewhere.json"
    path.rename(link)
    path.symlink_to(link)
    with pytest.raises(ValueError, match="symlink"):
        llm.endpoint("e")
    path.unlink()
    with pytest.raises(ValueError, match="no LLM endpoint e"):
        llm.endpoint("e")


def test_selection_picks_the_model():
    run("add", "a", "--kind", "ollama", "--url", "http://10.0.0.5:11434", "--model", "m:1")
    run("add", "b", "--kind", "vllm", "--url", "https://b.example")
    ns = lambda value: argparse.Namespace(llm=value)  # noqa: E731
    assert llm.selection(ns("a"))[1] == "m:1" and llm.selection(ns("a:other:7b"))[1] == "other:7b"
    with pytest.raises(ValueError, match="name a model"):
        llm.selection(ns("b"))
    llm.remember_models("b", ["only-one"])
    assert llm.selection(ns("b"))[1] == "only-one"  # the one model it lists
    with pytest.raises(ValueError, match="NAME\\[:MODEL\\]"):
        llm.parse_choice("a:bad model")


# --- servers: fake ones, one per kind --------------------------------------------------------------------------

ROUTES = {
    "ollama": {"/api/tags": {"models": [{"name": "qwen2.5-coder:7b", "size": 4683087332,
                                         "details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"}},
                                        {"name": "llama3.2:3b", "size": 2019393189, "details": {}}]},
               "/api/ps": {"models": [{"name": "llama3.2:3b"}]}},
    "lmstudio": {"/api/v0/models": {"data": [{"id": "qwen3-8b", "type": "llm", "state": "loaded"},
                                             {"id": "nomic-embed", "type": "embeddings", "state": "not-loaded"}]}},
    "llamacpp": {"/v1/models": {"object": "list", "data": [{"id": "Qwen3-Coder-30B-A3B-Q4_K_M.gguf",
                                                            "meta": {"n_ctx_train": 262144, "size": 18556689568}}]}},
    "vllm": {"/v1/models": {"data": [{"id": "Qwen/Qwen3-Coder-30B", "max_model_len": 65536}]}},
    "openai-compatible": {"/v1/models": {"data": [{"id": "gpt-oss-20b"}, {"id": "bad model\u001b[2J"}]}},
}
# Which APIs each fake has (POST {} gets 400 there, 404 elsewhere; LM Studio answers 200 "Unexpected endpoint").
APIS = {"ollama": {"/v1/messages", "/v1/responses", "/v1/chat/completions"},
        "lmstudio": {"/v1/messages", "/v1/responses", "/v1/chat/completions"},
        "llamacpp": {"/v1/messages", "/v1/responses", "/v1/chat/completions"},
        "vllm": {"/v1/messages", "/v1/responses", "/v1/chat/completions"},
        "openai-compatible": {"/v1/chat/completions"}}


def fake_server(kind, key=None, context=None, prefix=""):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, status, document):
            body = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            seen.append((self.command, self.path, self.headers.get("Authorization"), self.headers.get("X-Team")))
            if key and self.headers.get("Authorization") != f"Bearer {key}":
                self.answer(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            path = self.path[len(prefix):] if self.path.startswith(prefix) else None
            if self.authorized():
                routes = ROUTES[kind]
                self.answer(200, routes[path]) if path in routes else self.answer(404, {"error": "not found"})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = self.path[len(prefix):] if self.path.startswith(prefix) else None
            if not self.authorized():
                return
            if path in APIS[kind]:
                self.answer(400, {"error": "model is required"})
            elif kind == "lmstudio":
                self.answer(200, {"error": f"Unexpected endpoint or method. (POST {self.path})"})
            else:
                self.answer(404, {"detail": "Not Found"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if context:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.mark.parametrize("kind, names", [
    ("ollama", [("llama3.2:3b", True), ("qwen2.5-coder:7b", False)]),
    ("lmstudio", [("qwen3-8b", True)]),
    ("llamacpp", [("Qwen3-Coder-30B-A3B-Q4_K_M.gguf", True)]),
    ("vllm", [("Qwen/Qwen3-Coder-30B", True)]),
    ("openai-compatible", [("gpt-oss-20b", None)]),  # the name with escape codes is dropped
])
def test_models_per_kind_with_the_key(kind, names):
    secret("K", KEY)
    secret("TEAM", "blue team")
    server, seen = fake_server(kind, key=KEY)
    try:
        port = server.server_address[1]
        assert run("add", "e", "--kind", kind, "--url", f"http://localhost:{port}", "--api-key-secret", "K",
                   "--header-secret", "X-Team=TEAM")[0] == 0
        status, out = run("models", "e", "--json")
        assert status == 0
        models = json.loads(out)
        assert [(m["name"], m["loaded"]) for m in models] == names
        assert all(auth == f"Bearer {KEY}" and team == "blue team" for _, _, auth, team in seen)
        assert sorted(llm.endpoint("e")["models"]) == sorted(n for n, _ in names)  # remembered for later starts
        assert KEY not in out and KEY not in llm.store_path().read_text()
    finally:
        server.shutdown()


def test_models_refused_key_and_unreachable(capsys):
    secret("K", "wrong-key-0123456789")
    server, _ = fake_server("vllm", key=KEY)
    try:
        run("add", "e", "--kind", "vllm", "--url", f"http://localhost:{server.server_address[1]}", "--api-key-secret", "K")
        assert run("models", "e")[0] == 1
        assert "refused the key (HTTP 401)" in capsys.readouterr().err
    finally:
        server.shutdown()
    assert run("models", "e")[0] == 1  # nobody listens any more
    assert "sandbox-ai llm:" in capsys.readouterr().err
    run("add", "nokey", "--kind", "vllm", "--url", "http://10.0.0.5:8000", "--api-key-secret", "MISSING")
    assert run("models", "nokey")[0] == 1 and "no secret MISSING" in capsys.readouterr().err


def test_redirects_are_not_followed(capsys):
    """A redirect would carry the key to wherever the server points."""
    secret("K", KEY)
    other, other_seen = fake_server("vllm")

    class Redirect(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{other.server_address[1]}/v1/models")
            self.send_header("Content-Length", "0")
            self.end_headers()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run("add", "e", "--kind", "vllm", "--url", f"http://localhost:{server.server_address[1]}", "--api-key-secret", "K")
        assert run("models", "e")[0] == 1
        assert "not followed" in capsys.readouterr().err and other_seen == []
    finally:
        server.shutdown()
        other.shutdown()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_https_with_the_endpoints_own_ca(tmp_path, capsys):
    """An internal server with its own CA: verified against that CA (copied at add), with the key; refused without."""
    run_dir = tmp_path / "server-ca"
    rt.create_ca(run_dir, tmp_path / "ca-out", bundles=())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(rt.server_certificate(run_dir, "127.0.0.1")), str(run_dir / "leaf.key"))
    secret("K", KEY)
    server, seen = fake_server("vllm", key=KEY, context=context)
    try:
        url = f"https://127.0.0.1:{server.server_address[1]}"
        assert run("add", "plain", "--kind", "vllm", "--url", url, "--api-key-secret", "K")[0] == 0
        assert run("models", "plain")[0] == 1 and "CERTIFICATE_VERIFY_FAILED" in capsys.readouterr().err
        assert run("add", "own", "--kind", "vllm", "--url", url, "--api-key-secret", "K", "--ca",
                   str(run_dir / "ca.pem"))[0] == 0
        copy = llm.ca_dir() / "own.pem"
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600 and copy.read_text() == (run_dir / "ca.pem").read_text()
        (run_dir / "ca.pem").unlink()  # the copy counts, not the original
        assert run("models", "own")[0] == 0 and seen[-1][2] == f"Bearer {KEY}"
        lines = []
        assert llm.check(llm.endpoint("own"), "codex", out=lines.append)
        assert any("certificate verified for 127.0.0.1 with the endpoint's own CA" in line for line in lines)
        run("remove", "own")
        assert not copy.exists()
    finally:
        server.shutdown()
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate")
    assert run("add", "x", "--kind", "vllm", "--url", "https://x.example", "--ca", str(bad))[0] == 1
    assert "no PEM certificate" in capsys.readouterr().err


@pytest.mark.parametrize("kind, agent, ok, text", [
    ("ollama", "claude", True, "the Anthropic Messages API (Claude Code) at /v1/messages"),
    ("lmstudio", "codex", True, "the OpenAI Responses API (Codex) at /v1/responses"),
    ("openai-compatible", "claude", False, "Claude Code speaks only the Anthropic Messages API"),
    ("openai-compatible", "codex", False, "no the OpenAI Responses API (Codex) at /v1/responses"),
    ("openai-compatible", "opencode", True, "the OpenAI Chat Completions API (OpenCode)"),
    ("openai-compatible", None, True, "note  no the Anthropic Messages API"),
])
def test_check(kind, agent, ok, text):
    server, _ = fake_server(kind)
    try:
        run("add", "e", "--kind", kind, "--url", f"http://127.0.0.1:{server.server_address[1]}")
        lines = []
        assert llm.check(llm.endpoint("e"), agent, out=lines.append) is ok
        assert any(text in line for line in lines), lines
        assert any("model(s) listed" in line for line in lines)
    finally:
        server.shutdown()


def test_check_failures():
    lines = []
    run("add", "gone", "--kind", "vllm", "--url", "http://127.0.0.1:9", "--model", "m")
    assert llm.check(llm.endpoint("gone"), out=lines.append) is False and "FAIL" in lines[-1]
    server, _ = fake_server("vllm", key=KEY)
    try:
        secret("K", "wrong-key-0123456789")
        run("add", "e", "--kind", "vllm", "--url", f"http://127.0.0.1:{server.server_address[1]}", "--api-key-secret",
            "K", "--model", "not-there")
        lines = []
        assert llm.check(llm.endpoint("e"), out=lines.append) is False
        assert any("refused the key (HTTP 401)" in line for line in lines)
        secret("K", KEY)
        lines = []
        assert llm.check(llm.endpoint("e"), out=lines.append) is False
        assert any("FAIL  model not-there is not listed" in line for line in lines)
    finally:
        server.shutdown()


def test_claude_refusal_names_the_alternatives():
    run("add", "gw", "--kind", "openai-compatible", "--url", "https://gw.example")
    problem = llm.agent_problem(llm.endpoint("gw"), "claude")
    assert "Use Codex or OpenCode" in problem and "Ollama (0.14.0" in problem and "--anthropic" in problem
    run("add", "lite", "--kind", "openai-compatible", "--url", "https://lite.example", "--anthropic")
    assert llm.agent_problem(llm.endpoint("lite"), "claude") is None
    for kind in ("ollama", "lmstudio", "llamacpp", "vllm"):
        run("add", kind, "--kind", kind, "--url", "https://s.example", "--replace")
        assert llm.agent_problem(llm.endpoint(kind), "claude") is None


# --- what the agents get ---------------------------------------------------------------------------------------

def golden(name, text):
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert text == path.read_text(), f"{name} differs from its golden file (UPDATE_GOLDEN=1 rewrites it)"


@pytest.fixture
def team():
    run("add", "team", "--kind", "vllm", "--url", "https://llm.example.org:8443/gw", "--api-key-secret", "TEAM_KEY",
        "--model", "qwen3-coder-30b")
    llm.remember_models("team", ["qwen3-coder-30b", "llama-3.1-8b"])
    return llm.endpoint("team")


def test_golden_opencode(team):
    golden("opencode.json", json.dumps(llm.opencode_config(team, "qwen3-coder-30b", only=True), indent=2) + "\n")


def test_golden_codex(team):
    text = llm.codex_profile(team, "qwen3-coder-30b")
    golden("codex.config.toml", text)
    import tomllib
    config = tomllib.loads(text)
    provider = config["model_providers"]["llm-team"]
    assert config["model_provider"] == "llm-team" and config["model"] == "qwen3-coder-30b"
    assert provider == {"name": "team (vLLM)", "base_url": "https://llm.example.org:8443/gw/v1",
                        "env_key": "SANDBOX_AI_LLM_KEY", "wire_api": "responses"}
    assert config["analytics"] == {"enabled": False} and config["features"] == {"plugins": False}


def test_golden_claude(team):
    golden("claude.env.json", json.dumps(llm.claude_environment(team, "qwen3-coder-30b"), indent=2) + "\n")


def test_loopback_is_reached_at_127_0_0_1():
    run("add", "here", "--kind", "ollama", "--url", "http://localhost:11434")
    run("add", "v6", "--kind", "ollama", "--url", "http://[fd00::5]:11434/ollama")
    assert llm.agent_url(llm.endpoint("here")) == "http://127.0.0.1:11434/v1"
    assert llm.agent_url(llm.endpoint("here"), "anthropic") == "http://127.0.0.1:11434"
    assert llm.agent_url(llm.endpoint("v6")) == "http://[fd00::5]:11434/ollama/v1"


def namespace(value, **options):
    defaults = {"llm": value, "allow_tcp": [], "allow_host": [], "allow_host_port": [], "allow_http": [],
                "no_proxy": False, "offline": False}
    return argparse.Namespace(**{**defaults, **options})


def test_network_options_and_resolution():
    run("add", "ip", "--kind", "vllm", "--url", "http://[fd00::5]:8000", "--model", "m")
    run("add", "name", "--kind", "vllm", "--url", "https://llm.example.org:8443/gw", "--model", "m")
    run("add", "web", "--kind", "vllm", "--url", "https://llm.example.org", "--model", "m")
    run("add", "lan", "--kind", "vllm", "--url", "http://gpu.lan:8000", "--model", "m")
    run("add", "here", "--kind", "vllm", "--url", "http://localhost:8000", "--model", "m")
    a = namespace("ip")
    llm.network_options(a, True)
    assert a.allow_tcp == ["[fd00::5]:8000"] and a.allow_host == []
    a = namespace("web")
    llm.network_options(a, True)
    assert a.allow_host == ["llm.example.org"] and a.allow_tcp == []
    assert llm.resolved_network(a, {"llm.example.org": ["192.0.2.4"]}, None) == {} and a.allow_tcp == []
    a = namespace("name")
    llm.network_options(a, True)
    assert a.allow_host == [] and a.allow_http == ["llm.example.org * /gw/**"]
    pins = llm.resolved_network(a, {}, lambda names: {n: ["192.0.2.4", "2001:db8::4"] for n in names})
    assert pins == {"llm.example.org": ["192.0.2.4", "2001:db8::4"]}
    assert a.allow_tcp == ["192.0.2.4:8443", "[2001:db8::4]:8443"] and a.llm_addresses == ["192.0.2.4", "2001:db8::4"]
    a = namespace("name", no_proxy=True)
    llm.network_options(a, True)
    assert a.allow_http == []  # the prefix rule needs the proxy
    a = namespace("lan")
    with pytest.raises(ValueError, match="plain http to 203.0.113.7"):
        llm.resolved_network(a, {}, lambda names: {n: ["203.0.113.7"] for n in names})
    assert llm.resolved_network(namespace("lan"), {}, lambda names: {n: ["192.168.1.7"] for n in names})
    a = namespace("here")
    llm.network_options(a, True)
    assert a.allow_host_port == ["8000"]
    a = namespace("here")
    llm.network_options(a, False)
    assert a.allow_host_port == []  # validate() refuses it


def test_proxy_entry_and_plan_keep_secrets_apart(team):
    secret("TEAM_KEY", KEY)
    a = namespace("team", agent="codex")
    a.llm_addresses = ["192.0.2.4"]
    entry = llm.proxy_entry(a)[0]
    assert entry["key"] == KEY and entry["addresses"] == ["192.0.2.4"] and entry["prefix"] == "/gw"
    assert entry["host"] == "llm.example.org" and entry["port"] == 8443 and entry["scheme"] == "https"
    plan = llm.plan(a)
    assert KEY not in json.dumps(plan) and plan["key_secret"] == "TEAM_KEY"
    assert plan["opens"] == "llm.example.org:8443 (pinned at the start)" and plan["api"] == "openai-responses"
    assert KEY not in json.dumps(llm.agent_environment(a)) and KEY not in llm.codex_profile(team, "m")
    secret("TEAM_KEY", "two words")
    with pytest.raises(ValueError, match="does not look like an API key") as error:
        llm.proxy_entry(a)
    assert "two words" not in str(error.value)


def test_the_endpoints_vpn_becomes_the_starts_vpn(capsys):
    run("add", "office", "--kind", "vllm", "--url", "https://llm.corp.example", "--vpn", "corp", "--model", "m")
    run("add", "lan", "--kind", "vllm", "--url", "http://[fd00::5]:8000", "--model", "m")
    a = namespace("office", vpn=None)
    note = llm.apply_vpn(a)
    assert a.vpn == "corp" and a.vpn_origin == "--llm office"
    assert note == "--llm office is reached through the VPN profile corp: the sandbox starts behind it (--vpn corp)"
    a = namespace("office", vpn="corp")  # the same --vpn given as well: nothing to add, no note
    assert llm.apply_vpn(a) is None and a.vpn == "corp" and not hasattr(a, "vpn_origin")
    with pytest.raises(ValueError, match="this start has --vpn home; a sandbox has one VPN: drop --vpn home"):
        llm.apply_vpn(namespace("office", vpn="home"))
    a = namespace("lan", vpn=None)
    assert llm.apply_vpn(a) is None and a.vpn is None  # an endpoint without a VPN leaves --vpn alone
    assert llm.apply_vpn(namespace("missing", vpn=None)) is None  # validate() reports it
    # The name is resolved by the start (through the tunnel with --vpn); an address is not.
    assert llm.pinned_names(namespace("office")) == ["llm.corp.example"] and llm.pinned_names(namespace("lan")) == []
    # Profile names as sandbox-ai vpn takes them.
    capsys.readouterr()
    assert run("add", "dotted", "--kind", "vllm", "--url", "https://x.example", "--vpn", "a.b")[0] == 1
    assert "vpn names a VPN profile" in capsys.readouterr().err


def test_sandbox_ai_dispatches_llm():
    main = importlib.util.spec_from_file_location("sandbox_main_llm_test", ROOT / "sandbox/main.py")
    module = importlib.util.module_from_spec(main)
    main.loader.exec_module(module)
    assert module.PYTHON_COMMANDS["llm"] == ("llm.py",) and "sandbox-ai llm add" in module.__doc__
