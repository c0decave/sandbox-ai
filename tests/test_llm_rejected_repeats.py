"""The loop guard's "rejected" layer: an identical request the model server rejected (HTTP 4xx) is not sent again
and again.

Found in the Strata test (research-llm/strata-report.md, 2026-11-17): Strata answers a prompt over its context with
HTTP 400 ("requests are never truncated"), and OpenCode answered that with a tight compaction/retry loop - 551
identical requests in 150 s (1,103 requests in all, ~4 per second). The loop guard did not stop it: it looks at the
tool calls of a conversation, and these requests never got an answer. Any server with a strict context and no
truncation (Strata, llama.cpp) can trigger it.

So the proxy remembers a request the --llm server rejected (its digest: the path and the body as it went upstream,
the loop guard's own notes included) and, from the LAYER-th rejection of the very same request on, answers it itself:
HTTP with the server's status, an error in the APIs' shape that says what happened - worded so that no harness takes
it for a context overflow (OpenCode would compact again) or a transient error (retried) -, the server's reason in the
connection log. A different request always goes to the server (its first try is never blocked), a request that
succeeded is forgotten, a rejection older than the window counts no more. Every harness alike.

Unit tests of runtime.LlmRejected, llm_rejected_reply and the layer (llm.LOOP_DEFAULTS), and end to end through the
real proxy (ProxyServer) with a mock model server that rejects every request."""
import http.server
import json
from pathlib import Path
import re
import shutil
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402 - its Relay (the real proxy)

rt, llm = live.rt, live.llm


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


# OpenCode 1.19.5's own patterns (packages/llm/src/provider-error.ts isContextOverflow, session/retry.ts
# RETRYABLE_MESSAGE_PATTERNS): sandbox-ai's answer must match none of them, or OpenCode compacts or retries again.
OVERFLOW = [r"prompt is too long", r"request_too_large", r"input is too long", r"exceeds the context window",
            r"maximum context length", r"input token count.*exceeds", r"more than max tokens", r"maximum prompt length",
            r"reduce the length of the messages", r"exceeds the limit of \d+", r"exceeds the available context size",
            r"greater than the context length", r"context window exceeds limit", r"exceeded model token limit",
            r"context[_ ]length[_ ]exceeded", r"request entity too large", r"context length is only",
            r"prompt too long", r"too large for model", r"configured context size", r"context_window_exceeded",
            r"too many tokens", r"token limit exceeded"]
RETRYABLE = [r"429|500|502|503|504|524", r"rate limit|too many requests", r"overloaded|unavailable|internal|server.error",
             r"terminated|fetch failed|network|connection|socket|econn|etimedout", r"time(d)? ?out",
             r"try your request again|retry your request|resource.exhausted", r"try again (later|in\b)|at capacity"]


# --- the memory of rejected requests ----------------------------------------------------------------------------------

def test_the_same_rejected_request_is_refused_from_the_third_rejection_on():
    memory = rt.LlmRejected()
    assert memory.refusal("a", 3, 600, now=0) is None, "never a first try"
    for count in (1, 2):
        memory.failed("a", 400, "requests are never truncated", now=count)
        assert memory.refusal("a", 3, 600, now=count) is None
    memory.failed("a", 400, "requests are never truncated", now=3)
    assert memory.refusal("a", 3, 600, now=4) == {"count": 3, "status": 400,
                                                   "detail": "requests are never truncated"}
    assert memory.refusal("b", 3, 600, now=4) is None, "a different request: its first try goes"


def test_alternating_requests_are_each_counted():
    """OpenCode's loop alternates a compaction request and the retried one: each is counted on its own."""
    memory = rt.LlmRejected()
    for now in range(6):
        memory.failed("compact" if now % 2 else "retry", 400, "x", now=now)
    assert memory.refusal("compact", 3, 600, now=7)["count"] == 3
    assert memory.refusal("retry", 3, 600, now=7)["count"] == 3


def test_a_success_forgets_and_the_window_expires():
    memory = rt.LlmRejected()
    for now in range(3):
        memory.failed("a", 400, "x", now=now)
    memory.passed("a")
    assert memory.refusal("a", 3, 600, now=4) is None
    for now in range(3):
        memory.failed("a", 400, "x", now=now)
    assert memory.refusal("a", 3, 600, now=600) is not None
    assert memory.refusal("a", 3, 600, now=603) is None, "600 s after its last rejection it may go once more"
    memory.failed("a", 400, "x", now=604)
    assert memory.refusal("a", 3, 600, now=605) is None, "counted from one again"


def test_the_memory_stays_small():
    memory = rt.LlmRejected()
    for number in range(memory.LIMIT + 50):
        memory.failed(str(number), 400, "x" * 5000, now=number)
    assert len(memory.entries) == memory.LIMIT
    assert len(memory.entries[str(memory.LIMIT + 49)]["detail"]) <= 300


# --- sandbox-ai's answer ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_the_reply_says_what_happened_and_reads_as_neither_an_overflow_nor_a_transient_error(status):
    reply, text = rt.llm_rejected_reply(3, status)
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 %d " % status) and b"Content-Length: %d" % len(body) in head
    document = json.loads(body)
    assert document["error"]["message"] == text and document["error"]["code"] == "sandbox_ai_rejected_repeat"
    assert "rejected this exact request 3 times" in text and "has not sent it again" in text
    for pattern in OVERFLOW + RETRYABLE:
        assert not re.search(pattern, body.decode(), re.I), pattern


def test_the_layer_is_on_by_default_and_tunable():
    assert llm.LOOP_DEFAULTS["rejected"] == 3
    assert llm.loop_setting("rejected", "5") == 5 and llm.loop_setting("rejected", "0") == 0
    with pytest.raises(ValueError):
        llm.loop_setting("rejected", "1")


# --- end to end: the real proxy and a server that rejects every request -----------------------------------------------

class Rejecting(http.server.BaseHTTPRequestHandler):
    """Strata's answer to a prompt over its context: HTTP 400, every time. MODE "ok": 200 with a text answer."""
    protocol_version = "HTTP/1.1"
    bodies, mode = [], "reject"

    def log_message(self, *args):
        pass

    def do_POST(self):
        Rejecting.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
        if Rejecting.mode == "ok":
            data = rt.llm_stop_answer("chat", "m", "fine", False)
            self.send_response(200)
        else:
            data = json.dumps({"error": {"message": "the request exceeds the available context size (32768): "
                                                    "requests are never truncated", "type": "invalid_request_error"}
                               }).encode()
            self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def rejecting():
    Rejecting.bodies, Rejecting.mode = [], "reject"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Rejecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def request(text="hello"):
    return {"model": "m", "messages": [{"role": "user", "content": text}],
            "tools": [{"type": "function", "function": {"name": "bash", "parameters": {
                "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]}


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_fourth_identical_rejected_request_is_answered_by_the_proxy(tmp_path, rejecting):
    relay = live.Relay(tmp_path, rejecting.server_address[1])
    try:
        answers = [relay.post(live.PATHS["chat"], request()) for _ in range(6)]
        assert len(Rejecting.bodies) == 3, "the server got the request three times, then never again"
        for answer in answers[:3]:
            assert b" 400 " in answer.split(b"\r\n", 1)[0] and b"never truncated" in answer
        for answer in answers[3:]:
            head, _, body = answer.partition(b"\r\n\r\n")
            assert b" 400 " in head.split(b"\r\n", 1)[0]
            assert json.loads(body)["error"]["code"] == "sandbox_ai_rejected_repeat"
        records = relay.records(6)
        refused = [record for record in records if (record.get("llm_loop") or {}).get("kind") == "rejected"]
        assert len(refused) == 3
        assert refused[0]["llm_loop"]["action"] == "refused" and refused[0]["llm_loop"]["count"] == 3
        assert "never truncated" in refused[0]["llm_loop"]["detail"], "the server's reason is in the log"
        assert refused[0]["status"] == 400 and refused[0]["llm_loop"]["endpoint"] == "mac"
        record = live.safeguards.from_connection(refused[0], "run-1", live.safeguards.Redactor())
        assert record["kind"] == "loop" and "not sent to the server again" in record["what"]
        assert "rejected this exact request 3 times (HTTP 400)" in record["why"]
        # A different request: its first try goes to the server.
        relay.post(live.PATHS["chat"], request("something else"))
        assert len(Rejecting.bodies) == 4 and Rejecting.bodies[-1]["messages"][0]["content"] == "something else"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_a_request_that_succeeds_is_never_held_and_the_layer_switches_off(tmp_path, rejecting):
    Rejecting.mode = "ok"
    relay = live.Relay(tmp_path, rejecting.server_address[1])
    try:
        for _ in range(5):
            assert b" 200 " in relay.post(live.PATHS["chat"], request()).split(b"\r\n", 1)[0]
        assert len(Rejecting.bodies) == 5
        Rejecting.mode = "reject"
        relay.server.config["llm"][0]["loop"] = {**relay.server.config["llm"][0]["loop"], "rejected": 0}
        for _ in range(5):
            relay.post(live.PATHS["chat"], request("off"))
        assert len(Rejecting.bodies) == 10, "with the layer off every request goes"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_regression_opencodes_compaction_retry_loop_is_stopped(tmp_path, rejecting):
    """research-llm/strata-report.md: 551 identical requests in 150 s. Two requests in turn (compaction, retry), each
    sent over and over: each reaches the server three times, then the proxy answers."""
    relay = live.Relay(tmp_path, rejecting.server_address[1])
    try:
        for number in range(40):
            relay.post(live.PATHS["chat"], request("compact" if number % 2 else "retry"))
        assert len(Rejecting.bodies) == 6
    finally:
        relay.close()
