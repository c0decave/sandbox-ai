"""Spend limits for the cloud model APIs (sandbox-ai spend): the usage of each API read from its answer (streamed SSE
and JSON; cached, cache-written and reasoning tokens), the cost math (prices per million tokens, the cache, long
prompts, a model the table does not know, local models, subscriptions), the proxy's meter (warnings at 80 % and
100 %, the next request refused, the raise, the other runs' spending, a day's limit that starts again), the launcher's
--budget with its defaults and project-file keys, `sandbox-ai spend` itself, and the real proxy end to end with a
fake provider server: counted, refused before the request, raised through the run's control socket."""
import argparse
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_spend", "sandbox/runtime.py")
spend = load("sandbox_spend_test", "sandbox/spend.py")
spend._modules["runtime"] = rt
TABLE = spend.builtin_prices()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("NO_COLOR", "1")


def sse(*events):
    return b"".join(f"event: {e.get('type')}\n".encode() + b"data: " + json.dumps(e).encode() + b"\n\n"
                    for e in events)


def scanned(data, pieces=5):
    scanner = rt.UsageScanner()
    step = max(1, len(data) // pieces)
    for start in range(0, len(data), step):
        scanner.feed(data[start:start + step])
    return scanner.result()


ANTHROPIC_STREAM = sse(
    {"type": "message_start", "message": {"id": "m1", "model": "claude-sonnet-4-5-20250929", "usage": {
        "input_tokens": 12, "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 1000,
        "cache_creation": {"ephemeral_5m_input_tokens": 600, "ephemeral_1h_input_tokens": 400}, "output_tokens": 1}}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 350}},
    {"type": "message_stop"})
CHAT_STREAM = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in (
    {"id": "c1", "model": "gpt-4.1-2025-04-14", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
    {"id": "c1", "model": "gpt-4.1-2025-04-14", "choices": [], "usage": {
        "prompt_tokens": 2000, "completion_tokens": 300, "total_tokens": 2300,
        "prompt_tokens_details": {"cached_tokens": 1500}, "completion_tokens_details": {"reasoning_tokens": 0}}})) \
    + b"data: [DONE]\n\n"
RESPONSES_STREAM = sse(
    {"type": "response.created", "response": {"id": "r1", "model": "gpt-5-2025-08-07", "usage": None}},
    {"type": "response.output_text.delta", "delta": "hi"},
    {"type": "response.completed", "response": {"id": "r1", "model": "gpt-5-2025-08-07", "usage": {
        "input_tokens": 10000, "input_tokens_details": {"cached_tokens": 8000}, "output_tokens": 900,
        "output_tokens_details": {"reasoning_tokens": 700}, "total_tokens": 10900}}})


# --- the usage of each API, and the cost ------------------------------------------------------------------------------

def test_usage_of_each_api_streamed_and_not():
    found = scanned(ANTHROPIC_STREAM)
    assert found["model"] == "claude-sonnet-4-5-20250929"
    assert rt.spend_tokens("messages", found["usage"]) == {"in": 12, "cached": 4000, "write": 1000, "write_1h": 400,
                                                           "out": 350, "reasoning": 0}
    chat = scanned(CHAT_STREAM, 3)
    assert rt.spend_tokens("chat", chat["usage"]) == {"in": 500, "cached": 1500, "write": 0, "write_1h": 0,
                                                      "out": 300, "reasoning": 0}
    responses = scanned(RESPONSES_STREAM, 4)
    assert responses["model"] == "gpt-5-2025-08-07"
    assert rt.spend_tokens("responses", responses["usage"]) == {"in": 2000, "cached": 8000, "write": 0,
                                                                "write_1h": 0, "out": 900, "reasoning": 700}
    whole = json.dumps({"id": "m", "model": "claude-haiku-4-5", "content": [], "usage": {
        "input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 0}}).encode()
    assert rt.spend_tokens("messages", scanned(whole, 1)["usage"])["out"] == 7
    plain = json.dumps({"model": "gpt-4o", "usage": {"prompt_tokens": 9, "completion_tokens": 2}}).encode()
    assert rt.spend_tokens("chat", scanned(plain, 2)["usage"]) == {"in": 9, "cached": 0, "write": 0, "write_1h": 0,
                                                                   "out": 2, "reasoning": 0}
    # Past the scanner's limit only the lines with "usage" are read: a long answer's last count is not lost.
    scanner = rt.UsageScanner()
    scanner.feed(b"data: " + json.dumps({"type": "message_start", "message": {"model": "claude-opus-4-5", "usage": {
        "input_tokens": 3, "output_tokens": 1}}}).encode() + b"\n\n")
    filler = b"data: " + json.dumps({"type": "content_block_delta", "delta": {"text": "x" * 60000}}).encode() + b"\n\n"
    for _ in range(rt.UsageScanner.LIMIT // len(filler) + 10):
        scanner.feed(filler)
    scanner.feed(b"data: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 99999}}).encode() + b"\n\n")
    assert scanner.result()["usage"]["output_tokens"] == 99999


def test_prices_cost_math_and_model_names():
    sonnet = rt.spend_price(TABLE, "anthropic", "claude-sonnet-4-5-20250929")
    assert sonnet[:2] == ("anthropic", "claude-sonnet-4-5")
    tokens = {"in": 12, "cached": 4000, "write": 1000, "write_1h": 400, "out": 350, "reasoning": 0}
    expected = (12 * 3 + 4000 * 0.3 + 600 * 3.75 + 400 * 6 + 350 * 15) / 1e6
    assert rt.spend_cost(tokens, sonnet[2]) == pytest.approx(expected)
    # A prompt above 200k tokens of Sonnet 4.5: its long prices.
    long = {"in": 250000, "cached": 0, "write": 0, "write_1h": 0, "out": 1000, "reasoning": 0}
    assert rt.spend_cost(long, sonnet[2]) == pytest.approx((250000 * 6 + 1000 * 22.5) / 1e6)
    gpt5 = rt.spend_price(TABLE, "openai", "gpt-5-2025-08-07")
    assert gpt5[1] == "gpt-5" and rt.spend_cost({"in": 2000, "cached": 8000, "out": 900}, gpt5[2]) == \
        pytest.approx((2000 * 1.25 + 8000 * 0.125 + 900 * 10) / 1e6)
    # The longest name that fits wins; a name only begins another at a separator.
    assert rt.spend_price(TABLE, "openai", "gpt-5-mini-2025-08-07")[1] == "gpt-5-mini"
    assert rt.spend_price(TABLE, "openai", "gpt-5.1-codex")[1] == "gpt-5.1-codex"
    assert rt.spend_price(TABLE, "anthropic", "claude-sonnet-4-50") is None
    assert rt.spend_price(TABLE, "anthropic", "claude-sonnet-4-7") is None  # not claude-sonnet-4's price: unknown
    assert rt.spend_price(TABLE, "anthropic", "claude-sonnet-4-20250514")[1] == "claude-sonnet-4"
    assert rt.spend_price(TABLE, None, "anthropic.claude-sonnet-4-5-20250929-v1:0")[1] == "claude-sonnet-4-5"
    assert rt.spend_price(TABLE, "anthropic", "claude-opus-4-1-20250805")[1] == "claude-opus-4-1"
    # Another provider's name for the model (OpenRouter, Bedrock, Vertex) is found by the model itself.
    assert rt.spend_price(TABLE, "openrouter", "anthropic/claude-sonnet-4.5")[1] == "claude-sonnet-4-5"
    assert rt.spend_price(TABLE, None, "us.anthropic.claude-haiku-4-5")[1] == "claude-haiku-4-5"
    assert rt.spend_price(TABLE, None, "claude-opus-4-5@20251101")[1] == "claude-opus-4-5"
    # Unknown: no price, so no cost - never a guess.
    assert rt.spend_price(TABLE, "openai", "some-new-model") is None and rt.spend_cost(tokens, None) is None
    assert rt.spend_provider("api.anthropic.com:443", "x", TABLE) == "anthropic"
    assert rt.spend_provider("127.0.0.1:9", "gpt-4o", TABLE) == "openai"  # by the model, where the host says nothing
    assert rt.spend_provider("h", "m", TABLE, {"name": "box"}) == "llm:box"


def test_billing_local_subscription_or_per_token():
    head = lambda *lines: b"POST /v1/messages HTTP/1.1\r\n" + b"".join(l + b"\r\n" for l in lines) + b"\r\n"  # noqa
    assert rt.spend_billing("api.anthropic.com", "/v1/messages", head(b"x-api-key: sk-ant-api03-x")) == "api"
    assert rt.spend_billing("api.anthropic.com", "/v1/messages",
                            head(b"Authorization: Bearer sk-ant-oat01-abc")) == "subscription"
    # Only the credential says so: a header the agent may add to any request is no way around a budget.
    assert rt.spend_billing("api.anthropic.com", "/v1/messages",
                            head(b"Authorization: Bearer x", b"anthropic-beta: oauth-2025-04-20")) == "api"
    assert rt.spend_billing("api.anthropic.com", "/v1/messages",
                            head(b"x-api-key: k", b"Authorization: Bearer sk-ant-oat01-abc")) == "api"
    assert rt.spend_billing("chatgpt.com", "/backend-api/codex/responses", head()) == "subscription"
    assert rt.spend_billing("api.openai.com", "/backend-api/codex/responses", head()) == "api"
    # The hosts whose every POST (and WebSocket) a used-up budget holds back: batches, embeddings too.
    assert rt.spend_host("api.anthropic.com:443") == "anthropic" and rt.spend_host("eu.api.openai.com") is None \
        or rt.spend_host("eu.api.openai.com") == "openai"
    assert rt.spend_host("github.com:443") is None and rt.spend_host(None) is None
    assert rt.spend_billing("api.openai.com", "/v1/responses", head(b"Authorization: Bearer sk-proj-x")) == "api"
    assert rt.spend_billing("x", "/v1/chat/completions", head(), {"kind": "lmstudio", "scheme": "https"}) == "local"
    assert rt.spend_billing("x", "/v1/chat/completions", head(), {"kind": "openai-compatible", "scheme": "http"}) == \
        "local"
    assert rt.spend_billing("x", "/v1/chat/completions", head(), {"kind": "openai-compatible", "scheme": "https",
                                                                  "addresses": ["192.168.1.5"]}) == "local"
    assert rt.spend_billing("x", "/v1/chat/completions", head(), {"kind": "openai-compatible", "scheme": "https",
                                                                  "addresses": ["100.101.2.3", "fd7a:115c:a1e0::1"]}) \
        == "local"  # Tailscale
    assert rt.spend_billing("openrouter.ai", "/api/v1/chat/completions", head(),
                            {"kind": "openai-compatible", "scheme": "https", "addresses": ["104.18.2.1"]}) == "api"


# --- the proxy's meter ------------------------------------------------------------------------------------------------

def meter(tmp_path, budgets, clock=None):
    path = tmp_path / "spend-20261117-100000-run-1.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    control = tmp_path / rt.SPEND_CONTROL
    found = rt.SpendMeter({"run": "run-1", "name": "box", "project": "/p", "project_key": "p-1", "agent": "claude",
                           "prices": TABLE, "budgets": budgets}, fd, control, **({"clock": clock} if clock else {}))
    return found, path, control


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def usage(out):  # 1M output tokens of claude-haiku-4-5: USD 5 each
    return {"input_tokens": 0, "output_tokens": out}


def test_meter_warns_refuses_and_a_raise_lets_it_go_on(tmp_path):
    found, path, control = meter(tmp_path, [{"id": "run", "scope": "run", "usd": 10.0, "used": 0,
                                             "label": "budget of this run"}])
    assert found.refusal("api") is None
    found.record("anthropic", "claude-haiku-4-5", "api", "messages", usage(1_000_000))  # USD 5: 50 %
    assert not [l for l in lines(path) if l.get("event") == "budget"]
    found.record("anthropic", "claude-haiku-4-5", "api", "messages", usage(700_000))    # USD 8.50: 85 %
    warned = [l for l in lines(path) if l.get("event") == "budget"]
    assert [w["level"] for w in warned] == [80] and warned[0]["used"] == pytest.approx(8.5)
    assert found.refusal("api") is None
    found.record("anthropic", "claude-haiku-4-5", "api", "messages", usage(400_000))    # USD 10.50: used up
    assert [l["level"] for l in lines(path) if l.get("event") == "budget"] == [80, 100]
    text = found.refusal("api")
    assert text.startswith("sandbox-ai: the budget of this run of USD 10.00 is used up")
    assert "sandbox-ai spend raise box USD" in text
    assert found.refusal("subscription") is None and found.refusal("local") is None  # they cost nothing per token
    assert found.refusal("api")  # still refused, written once
    assert len([l for l in lines(path) if l.get("event") == "refused"]) == 1
    owner = (os.getuid(), os.getgid())
    assert rt.give_spend({"raise": 5}, control, owner) == {"raised": 5.0}
    assert found.refusal("api") is None  # 10.50 of 15
    assert any(l.get("event") == "raised" for l in lines(path))
    assert rt.give_spend({"raise": 1.5}, control, owner) == {"raised": 6.5}  # raises add up
    with pytest.raises(ValueError, match="above 0"):
        rt.give_spend({"raise": -1}, control, owner)
    with pytest.raises(ValueError):
        rt.give_spend({"raise": True}, control, owner)
    first = lines(path)[0]
    assert first["event"] == "run" and first["agent"] == "claude" and first["budgets"][0]["usd"] == 10.0
    assert all("sk-" not in line for line in path.read_text().splitlines())


def test_meter_unknown_models_local_and_subscriptions(tmp_path):
    # (no budget: under one an unknown model is counted at the fallback price - test_with_a_budget_an_unknown_...)
    found, path, _ = meter(tmp_path, [])
    unknown = found.record("openai", "brand-new-model", "api", "chat", {"prompt_tokens": 10, "completion_tokens": 5})
    assert unknown["usd"] is None and found.unknown == 1 and found.refusal("api") is None  # counted, not priced
    local = found.record("llm:box", "qwen3-8b", "local", "chat", {"prompt_tokens": 10, "completion_tokens": 5},
                         "box")
    assert local["usd"] == 0.0 and local["endpoint"] == "box"
    subscription = found.record("anthropic", "claude-opus-4-5", "subscription", "messages", usage(10))
    assert subscription["usd"] is None and subscription["billing"] == "subscription"
    assert found.spent == 0 and found.refusal("api") is None


def test_shared_limits_other_runs_and_a_new_day(tmp_path):
    now = [1000.0]
    found, path, control = meter(tmp_path, [{"id": "project-day:p-1", "scope": "project", "period": "day",
                                             "usd": 10.0, "used": 6.0, "until": 2000.0, "label": "day limit"}],
                                 clock=lambda: now[0])
    found.record("anthropic", "claude-haiku-4-5", "api", "messages", usage(300_000))  # 6 + 1.5 = 7.5
    assert found.refusal("api") is None
    rt.give_spend({"others": {"project-day:p-1": 2.5}}, control, (os.getuid(), os.getgid()))
    assert found.refusal("api").startswith("sandbox-ai: the day limit of USD 10.00 is used up (USD 10.00)")
    now[0] = 2500.0  # the next day: what was used before counts no more
    assert found.refusal("api") is None
    found.record("anthropic", "claude-haiku-4-5", "api", "messages", usage(100_000))
    assert found.state(found.budgets[0])[0] == pytest.approx(0.5)


# --- end to end: the real proxy and a fake provider -------------------------------------------------------------------

class Provider(http.server.BaseHTTPRequestHandler):
    """Answers like Anthropic's Messages API (streamed) and OpenAI's Chat Completions (one JSON document)."""
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Provider.seen.append((self.path, body))
        if self.path == "/v1/messages":
            data = ANTHROPIC_STREAM
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            data = json.dumps({"id": "c", "model": "gpt-4o-2024-08-06", "choices": [],
                               "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


class Relay:
    """The real ProxyServer with the spend meter, the supervisor's spend log and control socket; its redirect goes to
    the fake provider."""

    def __init__(self, tmp_path, port, budgets, monkeypatch):
        run_dir = tmp_path / "run"
        rt.create_ca(run_dir, tmp_path / "ca", bundles=())
        monkeypatch.setattr(rt, "PROXY_RUN", run_dir)
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.logs = tmp_path / "logs"
        self.logs.mkdir()
        self.spend_fd = rt.open_spend_log({"spend": "spend-20261117-100000-run-9.jsonl"}, self.logs)
        self.path = self.logs / "spend-20261117-100000-run-9.jsonl"
        config = {"body_limit": 65536, "allow": {"loopback": [port]}, "spend": {
            "run": "run-9", "name": "box", "project": "/p", "project_key": "p-1", "agent": "claude",
            "prices": TABLE, "budgets": budgets}}
        self.server = rt.ProxyServer(config, self.fd, None, run_dir=run_dir,
                                     destination=lambda sock: ("127.0.0.1", port))
        self.server.spend = rt.SpendMeter(config["spend"], self.spend_fd, run_dir / rt.SPEND_CONTROL)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])
        self.control = tmp_path / "control"
        self.control.mkdir()
        self.stop = threading.Event()
        threading.Thread(target=rt.control_server, args=(self.control / "control.sock", None, self.stop),
                         daemon=True).start()
        deadline = time.monotonic() + 5
        while not (self.control / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    def post(self, path, document, credential=b"x-api-key: sk-ant-api03-test"):
        body = json.dumps(document).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=20) as sock:
            sock.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json"
                         b"\r\n" + credential + b"\r\n" + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
                         + body)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return data

    def close(self):
        self.stop.set()
        self.listener.close()
        os.close(self.fd)
        os.close(self.spend_fd)


@needs_openssl
def test_end_to_end_counted_refused_before_the_request_and_raised(tmp_path, monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Provider.seen = []
    try:
        cost = (12 * 3 + 4000 * 0.3 + 600 * 3.75 + 400 * 6 + 350 * 15) / 1e6  # one answer of the fake: ~USD 0.011
        relay = Relay(tmp_path, server.server_address[1], [
            {"id": "run", "scope": "run", "usd": 0.015, "used": 0, "label": "budget of this run (--budget)"}],
            monkeypatch)
        messages = {"model": "claude-sonnet-4-5", "stream": True, "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}]}
        first = relay.post("/v1/messages", messages)
        assert b" 200 " in first.split(b"\r\n")[0] and b"message_stop" in first  # streamed through, whole
        second = relay.post("/v1/messages", messages)  # 0.011 of 0.015: still goes
        assert b" 200 " in second.split(b"\r\n")[0] and len(Provider.seen) == 2
        third = relay.post("/v1/messages", messages)  # used up: refused before it goes out
        status, _, body = third.partition(b"\r\n\r\n")
        assert b" 402 " in status.split(b"\r\n")[0] and len(Provider.seen) == 2
        error = json.loads(body)
        assert error["type"] == "error" and error["error"]["message"].startswith("sandbox-ai: the budget of this run")
        chat = relay.post("/v1/chat/completions", {"model": "gpt-4o", "messages": []},
                          b"Authorization: Bearer sk-proj-test")
        assert b" 402 " in chat.split(b"\r\n")[0]
        assert json.loads(chat.partition(b"\r\n\r\n")[2])["error"]["type"] == "insufficient_quota"
        # A subscription costs nothing per token: it goes on.
        subscribed = relay.post("/v1/messages", messages, b"Authorization: Bearer sk-ant-oat01-test")
        assert b" 200 " in subscribed.split(b"\r\n")[0] and len(Provider.seen) == 3
        # The human raises it through the run's control socket (as `sandbox-ai spend raise` does).
        net = spend.module("net")
        monkeypatch.setattr(net, "running", lambda orphans=None: {"run-9": {"run": "run-9", "start": "box",
                                                                            "directory": str(relay.control)}})
        assert spend.raise_budget("box", "1") == {"run": "run-9", "raised": 1.0}
        fourth = relay.post("/v1/chat/completions", {"model": "gpt-4o", "messages": []},
                            b"Authorization: Bearer sk-proj-test")
        assert b" 200 " in fourth.split(b"\r\n")[0] and len(Provider.seen) == 4
        relay.close()
        records = lines(relay.path)
        requests = [r for r in records if "event" not in r]
        assert [r["billing"] for r in requests] == ["api", "api", "subscription", "api"]
        assert requests[0]["provider"] == "anthropic" and requests[0]["model"] == "claude-sonnet-4-5-20250929"
        assert requests[0]["usd"] == pytest.approx(cost) and requests[0]["cached"] == 4000
        assert requests[3]["usd"] == pytest.approx((1000 * 2.5 + 100 * 10) / 1e6) and requests[3]["provider"] == "openai"
        events = [r["event"] for r in records if "event" in r]
        assert events[0] == "run" and "budget" in events and "refused" in events and "raised" in events
        assert "sk-ant" not in relay.path.read_text() and "sk-proj" not in relay.path.read_text()
        denied = [json.loads(l) for l in relay.log.read_text().splitlines() if '"(budget)"' in l]
        assert len(denied) == 2 and denied[0]["event"] == "denied"
        # What `sandbox-ai spend run` shows of it.
        found = spend.read_run([relay.path])
        found["id"] = "run-9"
        state = spend.run_state(found, TABLE)
        assert state["requests"] == 4 and state["subscription"] == 1 and state["raised"] == 1.0
        assert state["budgets"][0]["usd"] == pytest.approx(1.015) and not state["refused"]
        assert state["usd"] == pytest.approx(2 * cost + 0.0035)
    finally:
        server.shutdown()


# --- the launcher -----------------------------------------------------------------------------------------------------

def launcher():
    return load("sandbox_launcher_spend", "sandbox/agent_sandbox.py")


def test_budget_option_defaults_and_project_file(tmp_path, monkeypatch):
    s = launcher()
    a = s.parse_args(["claude", "--budget", "2.5"])
    assert a.budget == 2.5 and s.run_budget(a) == (2.5, "--budget")
    assert s.parse_args(["claude", "-bu", "$3"]).budget == 3.0
    for wrong in ("-1", "lots", "2000000"):
        with pytest.raises(SystemExit):
            s.parse_args(["claude", "--budget", wrong])
    plain = s.parse_args(["claude"])
    assert s.run_budget(plain) == (None, None)
    defaults = tmp_path / "cfg" / "sandbox-ai" / "defaults.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(json.dumps({"version": 1, "budget": 20}))
    assert s.load_user_defaults(plain)["budget"] == 20
    plain.budget_default = 20
    assert s.run_budget(plain)[0] == 20
    defaults.write_text(json.dumps({"version": 1, "budget": "much"}))
    with pytest.raises(ValueError, match="budget"):
        s.load_user_defaults(plain)
    # The project file's "budget" is an option before the command line's: the command line wins.
    merged = s.merge_config(["claude", "--budget", "1"], s.parse_args(["claude"]), {"budget": 7})
    assert s.parse_args(merged).budget == 1.0
    assert s.parse_args(s.merge_config(["claude"], s.parse_args(["claude"]), {"budget": 7})).budget == 7.0
    project = tmp_path / "proj"
    project.mkdir()
    config = project / ".sandbox-ai.json"
    for budget, fine in (("x", False), (-2, False), (True, False), (12.5, True)):
        config.write_text(json.dumps({"version": 1, "agent": "claude", "budget": budget}))
        s.trust_config(config)
        a = s.parse_args(["claude", "--workspace", str(project)])
        if fine:
            assert s.load_project_config(a)[1]["budget"] == 12.5
        else:
            with pytest.raises(ValueError, match="budget"):
                s.load_project_config(a)


def test_the_start_s_budgets_count_what_the_period_used(tmp_path):
    s = launcher()
    project = tmp_path / "proj"
    project.mkdir()
    key = s.default_log_dir(project).name
    folder = s.default_log_dir(project)
    folder.mkdir(parents=True)
    now = time.time()
    earlier = folder / "spend-20261117-080000-old-1.jsonl"
    earlier.write_text("\n".join(json.dumps(r) for r in (
        {"v": 1, "time": now - 60, "event": "run", "run": "old-1", "project_key": key, "agent": "codex"},
        {"v": 1, "time": now - 50, "provider": "openai", "model": "gpt-4o", "billing": "api", "in": 10, "usd": 1.25},
        {"v": 1, "time": now - 40, "provider": "openai", "model": "gpt-4o", "billing": "api", "in": 10, "usd": 0.5},
    )) + "\n")
    assert spend.main(["limit", "set", "--project", str(project), "--day", "3"], out=lambda _: None) == 0
    assert spend.main(["limit", "set", "--all", "--month", "100"], out=lambda _: None) == 0
    a = s.parse_args(["claude", "--budget", "4", "--workspace", str(project)])
    a.run_id, a.workspace, a.start_name = "new-1", str(project), "box"
    config = s.spend_config(a)
    budgets = {b["id"]: b for b in config["budgets"]}
    assert budgets["run"]["usd"] == 4.0 and budgets["run"]["used"] == 0
    assert budgets[f"project-day:{key}"]["used"] == pytest.approx(1.75)
    assert budgets["all-month"]["used"] == pytest.approx(1.75) and budgets["all-month"]["until"] > now
    assert config["prices"]["providers"]["anthropic"]["claude-opus-4-5"]["in"] == 5
    assert a.spend_budgets == config["budgets"]
    plan = s.spend_plan(a)
    assert plan["budget"] == 4.0 and plan["origin"] == "--budget" and len(plan["limits"]) == 2


def test_the_watch_tells_the_warnings_and_the_other_runs(tmp_path):
    path = tmp_path / "spend-20261117-100000-me.jsonl"
    path.write_text(json.dumps({"v": 1, "time": 1, "event": "run", "run": "me"}) + "\n")
    told, others = [], []
    watch = spend.SpendWatch(path, "me", [{"id": "all-day", "scope": "all", "period": "day", "usd": 5}],
                             lambda title, text, record: told.append((title, text)), others.append,
                             started=time.time() - 10)
    watch.step()
    with open(path, "a") as stream:
        stream.write(json.dumps({"v": 1, "time": 2, "event": "budget", "budget": "all-day", "label": "day limit of "
                                 "all projects", "level": 80, "used": 4.1, "usd": 5}) + "\n")
    assert watch.look()[0]["level"] == 80
    assert told == [("sandbox-ai: budget nearly used up", "the day limit of all projects is 80 % used (USD 4.10 of "
                                                          "5.00)")]
    assert others == [] or others == [{"all-day": 0.0}]
    with open(path, "a") as stream:
        for record in ({"event": "budget", "budget": "all-day", "level": 100, "used": 5.2, "usd": 5},
                       {"event": "refused", "budget": "all-day", "used": 5.2, "usd": 5},
                       {"event": "raised", "raised": 1},
                       {"event": "refused", "budget": "all-day", "used": 6.1, "usd": 6}):
            stream.write(json.dumps({"v": 1, "time": 3, **record}) + "\n")
    assert [r["event"] for r in watch.look()] == ["budget", "refused"]  # the first refusal is said with the 100 %
    assert told[1][0] == "sandbox-ai: budget used up" and "raise it with sandbox-ai spend raise" in told[1][1]
    assert "USD 6.10 of 6.00" in told[2][1]


# --- the command ------------------------------------------------------------------------------------------------------

def sandbox_ai(tmp_path, *argv):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state"),
           "NO_COLOR": "1"}
    return subprocess.run([str(ROOT / "sandbox-ai"), "spend", *argv], capture_output=True,
                          text=True, env=env, timeout=60)


def test_the_command_end_to_end(tmp_path):
    folder = tmp_path / "state" / "sandbox-ai" / "logs" / "proj-0123456789ab"
    folder.mkdir(parents=True)
    now = time.time()
    (folder / "spend-20261117-100000-run-a.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"v": 1, "time": now - 100, "event": "run", "run": "run-a", "project": "/home/me/proj",
         "project_key": "proj-0123456789ab", "agent": "claude", "name": "box",
         "budgets": [{"id": "run", "usd": 5, "used": 0, "label": "budget of this run (--budget)"}]},
        {"v": 1, "time": now - 90, "provider": "anthropic", "model": "claude-sonnet-4-5", "billing": "api",
         "in": 1000, "cached": 5000, "out": 300, "usd": 2.0},
        {"v": 1, "time": now - 80, "provider": "openai", "model": "mystery-1", "billing": "api", "in": 10, "out": 5,
         "usd": None},
        {"v": 1, "time": now - 70, "provider": "anthropic", "model": "claude-opus-4-5", "billing": "subscription",
         "in": 7, "out": 3, "usd": None},
        {"v": 1, "time": now - 60, "event": "budget", "budget": "run", "level": 80, "used": 4.2, "usd": 5},
    )) + "\n")
    listed = sandbox_ai(tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert "run-a" in listed.stdout and "USD 2.00 + 1 unknown + subscription" in listed.stdout
    assert "no price for mystery-1" in listed.stdout and "subscription" in listed.stdout
    data = json.loads(sandbox_ai(tmp_path, "--by", "model", "--json").stdout)
    assert {r["key"] for r in data["rows"]} == {"claude-sonnet-4-5", "mystery-1", "claude-opus-4-5"}
    assert data["total"]["usd"] == 2.0 and data["total"]["unknown"] == 1 and data["total"]["subscription"] == 1
    # A price set later prices what was unknown.
    assert sandbox_ai(tmp_path, "prices", "set", "mystery-1", "in=1000", "out=2000").returncode == 0
    data = json.loads(sandbox_ai(tmp_path, "--json").stdout)
    assert data["total"]["unknown"] == 0 and data["total"]["usd"] == pytest.approx(2.0 + (10 * 1000 + 5 * 2000) / 1e6)
    assert "*mystery-1" in sandbox_ai(tmp_path, "prices").stdout.replace(" *", "*")
    assert sandbox_ai(tmp_path, "prices", "remove", "mystery-1").returncode == 0
    wrong = sandbox_ai(tmp_path, "prices", "set", "x", "in=1")
    assert wrong.returncode == 1 and "in and out" in wrong.stderr
    assert json.loads(sandbox_ai(tmp_path, "--by", "day", "--since", "1h", "--json").stdout)["rows"][0]["requests"] == 3
    assert json.loads(sandbox_ai(tmp_path, "--since", "2099-01-01", "--json").stdout)["rows"] == []
    shown = sandbox_ai(tmp_path, "run", "run-a")
    assert shown.returncode == 0, shown.stderr
    assert "84 % of USD 5.00 - budget of this run" in shown.stdout
    state = json.loads(sandbox_ai(tmp_path, "run", "run-a", "--json").stdout)
    assert state["budgets"][0]["used"] == pytest.approx(4.2) and state["agent"] == "claude"
    assert sandbox_ai(tmp_path, "run", "nothing").returncode == 1
    project = tmp_path / "proj"
    project.mkdir()
    assert sandbox_ai(tmp_path, "limit", "set", "--project", str(project), "--day", "10").returncode == 0
    assert "day limit of the project proj" in sandbox_ai(tmp_path, "limit").stdout
    limits = json.loads(sandbox_ai(tmp_path, "limit", "--json").stdout)["limits"]
    assert limits[0]["usd"] == 10 and limits[0]["id"].startswith("project-day:proj-")
    assert sandbox_ai(tmp_path, "limit", "remove", "--project", str(project), "--day").returncode == 0
    assert sandbox_ai(tmp_path, "limit", "remove", "--all", "--day").returncode == 1
    assert sandbox_ai(tmp_path, "--since", "yesterdayish").returncode == 1
    assert (tmp_path / "cfg" / "sandbox-ai" / "spend.json").stat().st_mode & 0o777 == 0o600


# --- stats usage and the report ---------------------------------------------------------------------------------------

def sample_logs(tmp_path):
    folder = tmp_path / "state" / "sandbox-ai" / "logs" / "proj-0123456789ab"
    folder.mkdir(parents=True)
    now = time.time()
    (folder / "spend-20261117-100000-run-a.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"v": 1, "time": now - 100, "event": "run", "run": "run-a", "project": "/home/me/proj",
         "project_key": "proj-0123456789ab", "agent": "claude", "name": "box",
         "budgets": [{"id": "run", "usd": 5, "used": 0, "label": "budget of this run (--budget)"}]},
        {"v": 1, "time": now - 90, "provider": "anthropic", "model": "claude-sonnet-4-5", "billing": "api",
         "in": 1000, "cached": 5000, "out": 300, "usd": 1.5},
        {"v": 1, "time": now - 80, "provider": "llm:box", "model": "qwen3", "billing": "local", "in": 10, "usd": 0},
    )) + "\n")
    return folder


def test_stats_usage_and_the_report_show_the_spend(tmp_path):
    folder = sample_logs(tmp_path)
    monitor = load("sandbox_monitor_spend", "sandbox/monitor.py")
    data = monitor.usage("1d", "agent")
    assert data["spend"]["total"]["usd"] == 1.5 and data["spend"]["groups"][0]["key"] == "claude"
    assert data["spend"]["runs"]["run-a"]["requests"] == 2
    text = monitor.usage_text(data)
    assert "model requests (sandbox-ai spend), by agent:" in text and "claude: USD 1.50 - 2 requests" in text
    report = load("sandbox_report_spend", "sandbox/report.py")
    found = report.spend_data([{"run": "run-a", "log_dir": str(folder)}, {"run": "other"}])
    assert found["total"]["usd"] == 1.5 and found["runs"][0]["budgets"][0]["share"] == 0.3
    assert found["runs"][0]["models"][0] == {"model": "claude-sonnet-4-5", "requests": 1, "usd": 1.5,
                                             "cost": "USD 1.50"}
    assert report.spend_data([{"run": "none", "log_dir": str(folder)}]) is None
    blocks = []
    report.spend_blocks(found, blocks, lambda level, text, anchor: blocks.append(("heading", level, text, anchor)),
                        "spend", {}, [])
    assert blocks[0][2] == "Model spend" and blocks[-1][0] == "table" and "30 % of USD 5.00" in str(blocks[-1])


def test_a_start_s_dry_run_names_its_budget_and_limits(tmp_path):
    """The real entry point: `sandbox-ai claude --budget 5 --dry-run` - the plan names the budget, where it came from
    and your limits."""
    project = tmp_path / "proj"
    project.mkdir()
    assert sandbox_ai(tmp_path, "limit", "set", "--all", "--day", "20").returncode == 0
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state"),
           "NO_COLOR": "1"}
    run = subprocess.run([str(ROOT / "sandbox-ai"), "claude", "--budget", "5", "--dry-run"], capture_output=True,
                         text=True, env=env, cwd=project, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    plan = json.loads(run.stdout)["spend"]
    assert plan == {"budget": 5.0, "origin": "--budget", "limits": ["day limit of all projects"], "counted": True,
                    "file": "spend-TIME-RUN.jsonl"}


@needs_openssl
def test_a_budget_without_the_proxy_is_refused_not_silently_dropped(tmp_path):
    """Review 3x-3: `--budget 5 --no-proxy` started, and its budget held nothing (no proxy prices or refuses a
    request) - the dry run said only "counted": false. Like --api-key and --publish, it needs the proxy."""
    project = tmp_path / "proj"
    project.mkdir()
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state"),
           "NO_COLOR": "1"}
    run = subprocess.run([str(ROOT / "sandbox-ai"), "claude", "--budget", "5", "--no-proxy", "--dry-run"],
                         capture_output=True, text=True, env=env, cwd=project, timeout=120)
    assert run.returncode != 0 and "--budget needs the connection proxy" in run.stderr, run.stdout + run.stderr
    config = project / ".sandbox-ai.json"
    config.write_text(json.dumps({"version": 1, "agent": "claude", "budget": 5}))
    launcher().trust_config(config)  # (the same XDG_CONFIG_HOME as the command's)
    run = subprocess.run([str(ROOT / "sandbox-ai"), "claude", "--no-proxy", "--dry-run"],
                         capture_output=True, text=True, env=env, cwd=project, timeout=120)
    assert run.returncode != 0 and "--budget needs the connection proxy" in run.stderr, run.stdout + run.stderr


def test_a_month_s_limit_starts_again_every_month_of_a_long_run(tmp_path):
    """Review 3x-3: a month's limit rolled over once - then its "until" was None and it never started again (a run
    across two month ends kept counting the second month on the first's)."""
    now = [0.0]
    import calendar as cal
    from datetime import datetime
    first_end = datetime(2026, 12, 1).timestamp()
    found, _path, _control = meter(tmp_path, [{"id": "all-month", "scope": "all", "period": "month", "usd": 10.0,
                                                "used": 9.0, "until": first_end, "label": "month limit"}],
                                   clock=lambda: now[0])
    now[0] = first_end + 10  # December: what November used counts no more
    found.record("anthropic", "claude-haiku-4-5", "api", "messages", usage(1_600_000))  # 8.0
    assert found.state(found.budgets[0])[0] == pytest.approx(8.0)
    assert found.budgets[0]["until"] == pytest.approx(datetime(2027, 1, 1).timestamp(), abs=3600)
    now[0] = datetime(2027, 1, 1).timestamp() + 10  # January: December's 8 count no more either
    assert found.refusal("api") is None and found.state(found.budgets[0])[0] == 0.0
    assert cal.monthrange(2027, 1)[1] == 31


def test_bedrock_s_regional_inference_profiles_are_priced():
    """Review 3x-3: Bedrock's cross-region inference profiles name the model with a region before anthropic.
    (us., eu., apac., jp., au., global.) - only us., eu. and global. were known, so apac./jp./au. requests had an
    unknown cost and no budget held them."""
    for prefix in ("", "anthropic.", "us.anthropic.", "eu.anthropic.", "apac.anthropic.", "jp.anthropic.",
                   "au.anthropic.", "global.anthropic.", "us-gov.anthropic."):
        model = f"{prefix}claude-sonnet-4-5-20250929-v1:0"
        assert rt.spend_model_key(model) == "claude-sonnet-4-5-20250929-v1:0", model
        assert rt.spend_price(TABLE, None, model) is not None, model


def test_a_shared_limit_counts_a_running_run_whose_logs_are_elsewhere(tmp_path, monkeypatch):
    """Review 3x-3: a run started with --log-dir DIR writes its spend log there; `sandbox-ai spend` reads the running
    runs' own log directories, but a day's or month's limit did not - neither at the start (what the period used)
    nor in the launcher's watch (what the others spend meanwhile): such a run's spending held no shared limit."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    s = launcher()
    project = tmp_path / "proj"
    project.mkdir()
    key = s.default_log_dir(project).name
    elsewhere = tmp_path / "my-logs"
    elsewhere.mkdir()
    now = time.time()
    (elsewhere / "spend-20261117-080000-other-1.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"v": 1, "time": now - 60, "event": "run", "run": "other-1", "project_key": key, "agent": "codex"},
        {"v": 1, "time": now - 50, "provider": "openai", "model": "gpt-4o", "billing": "api", "in": 10, "usd": 2.0},
    )) + "\n")
    registered = tmp_path / "state/sandbox-ai/runs/other-1"
    registered.mkdir(parents=True)
    (registered / "run.json").write_text(json.dumps({"run": "other-1", "pid": os.getpid(), "log_dir": str(elsewhere)}))
    assert spend.main(["limit", "set", "--project", str(project), "--day", "10"], out=lambda _: None) == 0
    a = s.parse_args(["claude", "--workspace", str(project)])
    a.run_id, a.workspace, a.start_name = "new-1", str(project), "box"
    [budget] = s.spend_config(a)["budgets"]
    assert budget["used"] == pytest.approx(2.0), "the running run's spending in its own log directory counts"
    watch = spend.SpendWatch(tmp_path / "spend-me.jsonl", "new-1", [budget], lambda *_: None, started=now - 120)
    assert watch.others() == {budget["id"]: pytest.approx(2.0)}


def test_the_system_monitor_s_cost_counts_a_running_run_whose_logs_are_elsewhere(tmp_path, monkeypatch):
    """Review 3x-3: `sandbox-ai stats usage` (the app's and the web's System monitor: the Cost column) read the state
    directory's logs only - a running run with --log-dir DIR had no cost there, though `sandbox-ai spend` names it."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    elsewhere = tmp_path / "my-logs"
    elsewhere.mkdir()
    now = time.time()
    (elsewhere / "spend-20261117-080000-far-1.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"v": 1, "time": now - 60, "event": "run", "run": "far-1", "project_key": "p-1", "agent": "codex"},
        {"v": 1, "time": now - 50, "provider": "openai", "model": "gpt-4o", "billing": "api", "in": 10, "usd": 2.0},
    )) + "\n")
    registered = tmp_path / "state/sandbox-ai/runs/far-1"
    registered.mkdir(parents=True)
    (registered / "run.json").write_text(json.dumps({"run": "far-1", "pid": os.getpid(), "log_dir": str(elsewhere)}))
    monitor = load("sandbox_monitor_spend_elsewhere", "sandbox/monitor.py")
    data = monitor.usage("1d", "run")
    assert data["spend"]["runs"]["far-1"]["usd"] == pytest.approx(2.0)


# --- review 3x-1 (security): an agent cannot keep its requests out of the count ------------------------------------

class Compressing(Provider):
    """A provider that answers as the request's Accept-Encoding asks: "br" gets an answer the proxy cannot read."""
    asked = []

    def do_POST(self):
        Compressing.asked.append(self.headers.get("Accept-Encoding"))
        if "br" in (self.headers.get("Accept-Encoding") or ""):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            data = b"\x8b\x03\x80" + os.urandom(64)  # (stands for a Brotli stream: nothing sandbox-ai can scan)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "br")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_POST()


@needs_openssl
def test_a_metered_request_asks_for_an_answer_the_proxy_can_count(tmp_path, monkeypatch):
    """Review 3x-1: the usage is read from the answer, and only an uncompressed, gzip or deflate one is read. An
    agent that sent Accept-Encoding: br (or zstd) got its answers that way - none was counted, no budget ever
    used up. A request the meter counts asks for an answer as it is (identity)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Compressing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Compressing.asked = []
    try:
        relay = Relay(tmp_path, server.server_address[1], [
            {"id": "run", "scope": "run", "usd": 0.002, "used": 0, "label": "budget of this run (--budget)"}],
            monkeypatch)
        chat = {"model": "gpt-4o", "messages": []}
        first = relay.post("/v1/chat/completions", chat, b"Authorization: Bearer sk-proj-test\r\nAccept-Encoding: br")
        assert b" 200 " in first.split(b"\r\n")[0]
        assert Compressing.asked == ["identity"], "the agent chose an encoding the proxy cannot read"
        second = relay.post("/v1/chat/completions", chat, b"Authorization: Bearer sk-proj-test\r\nAccept-Encoding: "
                                                          b"zstd, br")
        assert b" 402 " in second.split(b"\r\n")[0], "the first request (USD 0.0035) was not counted"
        relay.close()
        requests = [r for r in lines(relay.path) if "event" not in r]
        assert len(requests) == 1 and requests[0]["usd"] == pytest.approx(0.0035)
    finally:
        server.shutdown()


def test_a_login_s_token_beside_another_credential_is_no_subscription():
    """Review 3x-1: "subscription" (never held back by a budget) is told by the credential alone - a made-up
    Claude-plan token in Authorization beside the key that really pays (in the query, a second Authorization) is
    billed per token."""
    head = lambda line, *lines: line + b"\r\n" + b"".join(l + b"\r\n" for l in lines) + b"\r\n"  # noqa
    oat = b"Authorization: Bearer sk-ant-oat01-made-up"
    assert rt.spend_billing("api.anthropic.com", "/v1/messages", head(b"POST /v1/messages HTTP/1.1", oat)) == \
        "subscription"
    path = "/v1beta/models/gemini-2.5-pro:generateContent?key=AIzaReal"
    assert rt.spend_billing("generativelanguage.googleapis.com", path,
                            head(b"POST " + path.encode() + b" HTTP/1.1", oat)) == "api"
    for query in ("?api_key=x", "?a=1&api-key=x", "?access_token=x", "?a=1&KEY=x"):
        assert rt.spend_billing("gateway.example", "/v1/messages" + query,
                                head(b"POST /v1/messages" + query.encode() + b" HTTP/1.1", oat)) == "api", query
    assert rt.spend_billing("gateway.example", "/v1/messages",
                            head(b"POST /v1/messages HTTP/1.1", oat, b"authorization: Bearer sk-real-key")) == "api"
    assert rt.spend_billing("gateway.example", "/v1/messages",
                            head(b"POST /v1/messages HTTP/1.1", oat, b"X-Gateway-Key: real")) == "api"


# --- requests the meter could not count (review 3x-1, open item 16) ----------------------------------------------------
#
# With a budget set: a streamed OpenAI-style Chat Completions request without stream_options.include_usage gets it
# (its answer said no usage: never counted, never held back), and a model the price table does not know is counted
# at a conservative fallback price (the table's highest input and output prices), said in the spend log and by
# `sandbox-ai spend`. Without a budget nothing changes.

def chat_request(document, extra=b"", chunked=False):
    body = json.dumps(document).encode()
    if chunked:
        head = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: api.openai.com\r\nContent-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n" + extra + b"\r\n")
        parts = [(b"%x\r\n" % len(body[:20]) + body[:20] + b"\r\n", body[:20]),
                 (b"%x\r\n" % len(body[20:]) + body[20:] + b"\r\n", body[20:]), (b"0\r\n\r\n", b"")]
    else:
        head = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: api.openai.com\r\nContent-Type: application/json\r\n"
                + extra + b"Content-Length: %d\r\n\r\n" % len(body))
        parts = [(body, body)]
    headers = rt.parse_head(head)[1]
    return head, parts, headers, body


def sent_body(head, parts):
    body = b"".join(raw for raw, _ in parts)
    length = [line for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")]
    assert len(length) == 1 and int(length[0].split(b":")[1]) == len(body), head
    assert b"transfer-encoding" not in head.lower()
    return body


def test_a_streamed_chat_request_is_asked_for_its_usage():
    document = {"model": "gpt-4o", "stream": True, "temperature": 1e-05, "messages": [{"role": "user", "content": "ü"}]}
    head, parts, headers, body = chat_request(document)
    new_head, new_parts, added = rt.spend_include_usage(head, parts, headers)
    assert added
    sent = sent_body(new_head, list(new_parts))
    assert json.loads(sent) == {**document, "stream_options": {"include_usage": True}}
    assert body[1:] in sent  # every other field byte for byte as the agent sent it (1e-05 stays 1e-05)
    # Other options of the stream are kept; include_usage false (the agent turned it off) is turned on.
    for options in ({"chunk_size": 3}, {"include_usage": False, "x": 1}):
        head, parts, headers, _ = chat_request({**document, "stream_options": options})
        new_head, new_parts, added = rt.spend_include_usage(head, parts, headers)
        assert added and json.loads(sent_body(new_head, list(new_parts)))["stream_options"] == \
            {**options, "include_usage": True}
    # A chunked body: sent with its length.
    head, parts, headers, _ = chat_request(document, chunked=True)
    new_head, new_parts, added = rt.spend_include_usage(head, parts, headers)
    assert added and json.loads(sent_body(new_head, list(new_parts)))["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("document, extra", [
    ({"model": "gpt-4o", "messages": []}, b""),                                          # not streamed
    ({"model": "gpt-4o", "stream": False, "messages": []}, b""),
    ({"model": "gpt-4o", "stream": "yes", "messages": []}, b""),                         # not the API's true
    ({"model": "gpt-4o", "stream": True, "stream_options": {"include_usage": True}}, b""),  # asks already
    ({"model": "gpt-4o", "stream": True}, b"Content-Encoding: gzip\r\n"),                # not plain: unread
    (["not", "an", "object"], b""),
])
def test_a_request_that_needs_no_usage_question_passes_unchanged(document, extra):
    head, parts, headers, _ = chat_request(document, extra)
    new_head, new_parts, added = rt.spend_include_usage(head, parts, headers)
    assert not added and new_head == head and list(new_parts) == parts


def test_a_body_that_is_no_json_passes_unchanged():
    head = b"POST /v1/chat/completions HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 5\r\n\r\n"
    parts = [(b"{nope", b"{nope")]
    new_head, new_parts, added = rt.spend_include_usage(head, parts, rt.parse_head(head)[1])
    assert not added and new_head == head and list(new_parts) == parts
    big = b'{"stream": true, "x": "' + b"a" * (rt.LLM_BODY_LIMIT + 10) + b'"}'
    head = b"POST /v1/chat/completions HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % \
        len(big)
    parts = [(big[i:i + 65536], big[i:i + 65536]) for i in range(0, len(big), 65536)]
    new_head, new_parts, added = rt.spend_include_usage(head, iter(parts), rt.parse_head(head)[1])
    assert not added and new_head == head and list(new_parts) == parts  # too large to read: every piece still goes


def test_the_fallback_price_is_the_table_s_highest():
    assert rt.spend_fallback_price(TABLE) == {"in": 20.0, "out": 120.0}  # o3-pro's input, gpt-5-pro's output
    assert rt.spend_fallback_price({"providers": {"x": {"m": {"in": 99, "out": 1}}}}) == {"in": 99.0, "out": 120.0}
    assert rt.spend_fallback_price(None) == rt.SPEND_FALLBACK == {"in": 20.0, "out": 120.0}  # never below it


def test_with_a_budget_an_unknown_model_is_counted_at_the_fallback_price(tmp_path):
    found, path, _ = meter(tmp_path, [{"id": "run", "scope": "run", "usd": 1.0, "used": 0,
                                       "label": "budget of this run (--budget)"}])
    first = found.record("openai", "brand-new-model", "api", "chat", {"prompt_tokens": 10_000,
                                                                       "completion_tokens": 5_000})
    assert first["usd"] == pytest.approx((10_000 * 20 + 5_000 * 120) / 1e6)  # USD 0.80
    assert first["fallback"] == {"in": 20.0, "out": 120.0} and found.unknown == 0
    found.record("openai", "brand-new-model", "api", "chat", {"prompt_tokens": 10_000, "completion_tokens": 5_000})
    warned = [l for l in lines(path) if l.get("event") == "fallback"]
    assert warned == [{**warned[0], "model": "brand-new-model", "price": {"in": 20.0, "out": 120.0}}]  # once a model
    assert found.refusal("api").startswith("sandbox-ai: the budget of this run (--budget) of USD 1.00 is used up")
    # A known model and the others go on as before.
    known = found.record("openai", "gpt-4o", "api", "chat", {"prompt_tokens": 1000, "completion_tokens": 100})
    assert "fallback" not in known and known["usd"] == pytest.approx(0.0035)
    assert found.record("llm:box", "mystery", "local", "chat", {"prompt_tokens": 5})["usd"] == 0.0
    assert found.record("anthropic", "mystery", "subscription", "messages", usage(10))["usd"] is None


def test_without_a_budget_an_unknown_model_stays_unknown(tmp_path):
    found, path, _ = meter(tmp_path, [])
    record = found.record("openai", "brand-new-model", "api", "chat", {"prompt_tokens": 10, "completion_tokens": 5})
    assert record["usd"] is None and "fallback" not in record and found.unknown == 1
    assert not [l for l in lines(path) if l.get("event") == "fallback"]


def test_spend_says_what_was_priced_at_the_fallback(tmp_path):
    folder = tmp_path / "state" / "sandbox-ai" / "logs" / "proj-0123456789ab"
    folder.mkdir(parents=True)
    now = time.time()
    (folder / "spend-20261117-100000-run-f.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"v": 1, "time": now - 100, "event": "run", "run": "run-f", "project": "/home/me/proj",
         "project_key": "proj-0123456789ab", "agent": "codex", "name": "fb",
         "budgets": [{"id": "run", "usd": 5, "used": 0, "label": "budget of this run (--budget)"}]},
        {"v": 1, "time": now - 90, "event": "fallback", "model": "mystery-2", "price": {"in": 20.0, "out": 120.0}},
        {"v": 1, "time": now - 90, "provider": "openai", "model": "mystery-2", "billing": "api", "in": 10000,
         "out": 5000, "usd": 0.8, "fallback": {"in": 20.0, "out": 120.0}},
    )) + "\n")
    listed = sandbox_ai(tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert "USD 0.80" in listed.stdout
    assert ("note: 1 request(s) priced at the fallback price USD 20.00 in / USD 120.00 out per million tokens "
            "(no price known for mystery-2)") in listed.stdout
    shown = sandbox_ai(tmp_path, "run", "run-f")
    assert ("1 request(s) priced at the fallback price USD 20.00 in / USD 120.00 out per million tokens: no price "
            "known for mystery-2") in shown.stdout, shown.stdout
    state = json.loads(sandbox_ai(tmp_path, "run", "run-f", "--json").stdout)
    assert state["fallback"] == 1 and state["fallback_models"] == ["mystery-2"]
    assert state["fallback_price"] == {"in": 20.0, "out": 120.0} and state["usd"] == pytest.approx(0.8)
    # A price set since prices it at that price.
    assert sandbox_ai(tmp_path, "prices", "set", "mystery-2", "in=1", "out=2").returncode == 0
    state = json.loads(sandbox_ai(tmp_path, "run", "run-f", "--json").stdout)
    assert state["fallback"] == 0 and state["usd"] == pytest.approx((10000 * 1 + 5000 * 2) / 1e6)


def test_the_watch_tells_a_fallback_price(tmp_path):
    path = tmp_path / "spend-20261117-100000-me.jsonl"
    path.write_text(json.dumps({"v": 1, "time": 1, "event": "fallback", "model": "mystery-2",
                                "price": {"in": 20.0, "out": 120.0}}) + "\n")
    told = []
    watch = spend.SpendWatch(path, "me", [{"id": "run", "scope": "run", "usd": 5}],
                             lambda title, text, record: told.append((title, text)))
    assert [r["event"] for r in watch.look()] == ["fallback"]
    assert told == [("sandbox-ai: a model without a price", "mystery-2 has no price in the table: counted at the "
                     "fallback price USD 20.00 in / USD 120.00 out per million tokens for the budget; set one: "
                     "sandbox-ai spend prices set mystery-2 in=USD out=USD")]


class StreamingChat(Provider):
    """OpenAI's Chat Completions, streamed: the usage comes in the last event only when the request asked for it
    (stream_options.include_usage), as OpenAI's API does."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Provider.seen.append((self.path, body))
        document = json.loads(body)
        model = document.get("model")
        events = [{"id": "c", "model": model, "choices": [{"index": 0, "delta": {"content": "hi"}}]}]
        if (document.get("stream_options") or {}).get("include_usage") is True:
            events.append({"id": "c", "model": model, "choices": [],
                           "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})
        data = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@needs_openssl
@pytest.mark.parametrize("budgets", [[{"id": "run", "scope": "run", "usd": 0.006, "used": 0,
                                       "label": "budget of this run (--budget)"}], []])
def test_end_to_end_a_streamed_chat_request_is_counted_under_a_budget(tmp_path, monkeypatch, budgets):
    """The real proxy: an agent that streams Chat Completions without asking for the usage was never counted (and
    never held back). With a budget the proxy asks for it; without one the request goes exactly as it came."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingChat)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Provider.seen = []
    try:
        relay = Relay(tmp_path, server.server_address[1], budgets, monkeypatch)
        chat = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        first = relay.post("/v1/chat/completions", chat, b"Authorization: Bearer sk-proj-test")
        assert b" 200 " in first.split(b"\r\n")[0] and b"[DONE]" in first
        second = relay.post("/v1/chat/completions", chat, b"Authorization: Bearer sk-proj-test")
        third = relay.post("/v1/chat/completions", chat, b"Authorization: Bearer sk-proj-test")
        relay.close()
        requests = [r for r in lines(relay.path) if "event" not in r]
        if budgets:
            assert json.loads(Provider.seen[0][1]) == {**chat, "stream_options": {"include_usage": True}}
            assert b" 200 " in second.split(b"\r\n")[0]  # USD 0.0035 of 0.006
            assert b" 402 " in third.split(b"\r\n")[0] and len(Provider.seen) == 2  # 0.007: used up, held back
            assert [r["usd"] for r in requests] == [pytest.approx(0.0035)] * 2
            logged = [json.loads(l) for l in relay.log.read_text().splitlines()]
            assert any(entry.get("spend_include_usage") for entry in logged)
        else:
            assert [json.loads(body) for _, body in Provider.seen] == [chat] * 3  # untouched
            assert b" 200 " in third.split(b"\r\n")[0] and requests == []  # (no usage in the answer: nothing to count)
    finally:
        server.shutdown()


@needs_openssl
def test_end_to_end_an_unknown_model_is_held_to_the_budget(tmp_path, monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingChat)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Provider.seen = []
    try:
        relay = Relay(tmp_path, server.server_address[1], [
            {"id": "run", "scope": "run", "usd": 0.05, "used": 0, "label": "budget of this run (--budget)"}],
            monkeypatch)
        chat = {"model": "brand-new-model", "stream": True, "messages": []}
        statuses = [relay.post("/v1/chat/completions", chat, b"Authorization: Bearer sk-proj-test").split(b"\r\n")[0]
                    for _ in range(3)]
        relay.close()
        # 1000 in and 100 out at USD 20 / 120 per million: USD 0.032 each - the second uses the budget up.
        assert [b" 200 " in s for s in statuses] == [True, True, False] and b" 402 " in statuses[2]
        records = lines(relay.path)
        assert [r["usd"] for r in records if "event" not in r] == [pytest.approx(0.032)] * 2
        assert [r["model"] for r in records if r.get("event") == "fallback"] == ["brand-new-model"]
    finally:
        server.shutdown()
