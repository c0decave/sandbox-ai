"""--llm with LM Studio and Claude Code: the cached tokens are not counted twice (the window is no longer halved).

The local-model research, round 1 (runs/t2_debug__claude__laguna__r1; sandbox-ai's own proxy, LM Studio): Claude
Code compacted its conversation at 50,589 and again at 50,308 prompt tokens - of a model loaded with 131,072 and a
window told as such. Each time: a summary request of 65-81 s, the conversation started over at 22,000 tokens (30-40 s
to read it again), and what the model had found out was gone; the debugging task Laguna-S had solved in 5 minutes
when its prompt stayed under 45,000 tokens (phase 2) was not solved in 20.

Cause: LM Studio's Messages API (/v1/messages; its MLX and llama.cpp runtimes alike) reports

    "usage": {"input_tokens": 49217, "output_tokens": 565, "cache_read_input_tokens": 49191}

with input_tokens the WHOLE prompt and cache_read_input_tokens the part of it that came from the prompt cache. In
Anthropic's API input_tokens are the tokens that were NOT read from the cache: the prompt is input_tokens +
cache_creation_input_tokens + cache_read_input_tokens. Claude Code adds them up as Anthropic defines them - 98,408 -
and compacts when that reaches the window less the answer's tokens (131,072 - 32,768 = 98,304): at half the window.

The relay gives the agent the usage as the Messages API defines it: for an LM Studio endpoint, input_tokens without
the cached tokens (the sum is the prompt again). Unit tests of runtime.llm_messages_usage and LlmUsageAnswer, and end
to end through the real proxy (ProxyServer) with a scripted server that counts as LM Studio does."""
import copy
import http.server
import json
from pathlib import Path
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


# --- the usage --------------------------------------------------------------------------------------------------------

def test_regression_the_cached_tokens_are_taken_out_of_input_tokens():
    """runs/t2_debug__claude__laguna__r1, request 45: 49,217 prompt tokens, 49,191 of them from the cache. What
    Claude Code adds up is the prompt again: 26 + 49,191."""
    usage = {"input_tokens": 49217, "output_tokens": 565, "cache_read_input_tokens": 49191}
    assert rt.llm_messages_usage(usage) is True
    assert usage == {"input_tokens": 26, "output_tokens": 565, "cache_read_input_tokens": 49191}
    assert usage["input_tokens"] + usage["cache_read_input_tokens"] == 49217 < 131072 - 32768


@pytest.mark.parametrize("usage,expected", [
    # the first request of a conversation: nothing cached, nothing to change
    ({"input_tokens": 23890, "output_tokens": 1486, "cache_read_input_tokens": 0}, None),
    # no cache counter at all (another server, an older LM Studio)
    ({"input_tokens": 23890, "output_tokens": 12}, None),
    # a cache that was written as well: both go out of input_tokens
    ({"input_tokens": 1000, "output_tokens": 1, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 300},
     {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 300}),
    # the whole prompt from the cache
    ({"input_tokens": 500, "cache_read_input_tokens": 500}, {"input_tokens": 0, "cache_read_input_tokens": 500}),
    # counted as Anthropic defines it already (fewer input tokens than cached ones): as it came
    ({"input_tokens": 26, "output_tokens": 565, "cache_read_input_tokens": 49191}, None),
    # not numbers, negative ones, no usage
    ({"input_tokens": "49217", "cache_read_input_tokens": 49191}, None),
    ({"input_tokens": 49217, "cache_read_input_tokens": True}, None),
    ({"input_tokens": 49217, "cache_read_input_tokens": -5}, None),
    ({"input_tokens": 100, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 60}, None),
    ("usage", None), (None, None), ([], None),
])
def test_what_is_changed_and_what_stays(usage, expected):
    before = copy.deepcopy(usage)
    assert rt.llm_messages_usage(usage) is (expected is not None)
    assert usage == (before if expected is None else expected)


# --- the answer on its way to the agent -------------------------------------------------------------------------------

def stream(total=19585, cached=19055, output=45):
    """LM Studio's streamed answer on the Messages API (round1/runs/t2_debug__claude__laguna__r1, its 5th request:
    the usage in message_start and in message_delta)."""
    usage = {"input_tokens": total, "output_tokens": 0, "cache_read_input_tokens": cached}
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_rq6fdd6frray2cqrkwmb6k", "type": "message", "role": "assistant", "content": [],
            "model": "poolside/laguna-s-2.1", "stop_reason": None, "stop_sequence": None, "usage": usage}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "the usage: input_tokens 19585"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {**usage, "output_tokens": output}}),
        ("message_stop", {"type": "message_stop"})]
    return b"".join(f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
                    for name, data in events)


def usages(data):
    found = []
    for line in data.split(b"\n"):
        if line.startswith(b"data:"):
            event = json.loads(line[5:])
            usage = (event.get("message") or {}).get("usage") or event.get("usage")
            if usage:
                found.append(usage)
    return found


@pytest.mark.parametrize("size", [1, 7, 100, 100000])
def test_regression_a_streamed_answers_usage_however_it_is_cut_into_pieces(size):
    data = stream()
    answer = rt.LlmUsageAnswer()
    out = b"".join(answer.feed(data[start:start + size]) for start in range(0, len(data), size)) + answer.flush()
    assert usages(out) == [{"input_tokens": 530, "output_tokens": 0, "cache_read_input_tokens": 19055},
                           {"input_tokens": 530, "output_tokens": 45, "cache_read_input_tokens": 19055}]
    assert answer.changed == 2
    # every other line as it came (the model's text, which names the numbers, too)
    other = [line for line in data.split(b"\n") if b'"usage"' not in line]
    assert [line for line in out.split(b"\n") if b'"usage"' not in line] == other
    assert live.read_answer("messages", out)["text"] == "the usage: input_tokens 19585"


def test_an_answer_with_nothing_cached_passes_byte_for_byte():
    data = stream(total=23890, cached=0)
    answer = rt.LlmUsageAnswer()
    assert answer.feed(data) + answer.flush() == data and answer.changed == 0
    odd = b'data: {"usage": \n\ndata: [1, 2]\n\nevent: x\ndata: {"usage": 5}\n\n: usage\n\n'
    assert answer.feed(odd) + answer.flush() == odd


def test_an_answer_that_is_one_json_document():
    document = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "DONE"}],
                "usage": {"input_tokens": 49217, "output_tokens": 565, "cache_read_input_tokens": 49191}}
    out = json.loads(rt.LlmUsageAnswer().whole(json.dumps(document).encode()))
    assert out == {**document, "usage": {"input_tokens": 26, "output_tokens": 565, "cache_read_input_tokens": 49191}}
    plain = json.dumps({**document, "usage": {"input_tokens": 100, "output_tokens": 5}}).encode()
    assert rt.LlmUsageAnswer().whole(plain) == plain
    assert rt.LlmUsageAnswer().whole(b"not json") == b"not json"


# --- end to end: Claude Code's request through the real proxy to a server that counts as LM Studio does -----------------

class Server(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        data = Server.script.pop(0)
        streamed = data.startswith(b"event:")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if streamed else "application/json")
        if streamed:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(data), 150):  # events split across chunks, as a server's buffers do
                piece = data[start:start + 150]
                self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


@pytest.fixture
def server():
    Server.script = []
    made = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=made.serve_forever, daemon=True).start()
    yield made
    made.shutdown()
    made.server_close()


REQUEST = {"model": "poolside/laguna-s-2.1", "max_tokens": 32768, "stream": True,
           "messages": [{"role": "user", "content": "fix the report"}]}


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("kind,fixed", [("lmstudio", True), ("llamacpp", False), ("ollama", False)])
def test_e2e_claude_code_gets_the_usage_as_the_messages_api_defines_it(tmp_path, server, kind, fixed):
    """Claude Code's streamed request and one that is not, through the real proxy. With an LM Studio endpoint the
    agent's numbers add up to the prompt (530 + 19,055 = 19,585), and the connection log says that the usage was
    changed; the other kinds of server were not measured: their answers pass as they come."""
    relay = live.Relay(tmp_path, server.server_address[1])
    try:
        relay.server.config["llm"][0]["kind"] = kind
        whole = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": [
            {"type": "text", "text": "DONE"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 49217, "output_tokens": 565, "cache_read_input_tokens": 49191}}).encode()
        Server.script = [stream(), whole]
        head, body = live.dechunk(relay.post("/v1/messages", REQUEST))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
        assert [usage["input_tokens"] for usage in usages(body)] == ([530, 530] if fixed else [19585, 19585])
        assert all(usage["cache_read_input_tokens"] == 19055 for usage in usages(body))
        assert live.read_answer("messages", body)["text"] == "the usage: input_tokens 19585"
        head, body = live.dechunk(relay.post("/v1/messages", {**REQUEST, "stream": False}))
        assert json.loads(body)["usage"]["input_tokens"] == (26 if fixed else 49217)
        assert int(head.lower().split(b"content-length:")[1].split(b"\r\n")[0]) == len(body)
        records = relay.records(2)
        assert [record.get("llm_usage") for record in records] == (
            [{"cached_tokens_out_of_input_tokens": 2}, {"cached_tokens_out_of_input_tokens": 1}] if fixed
            else [None, None])
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_other_apis_of_an_lm_studio_endpoint_pass_as_they_came(tmp_path, server):
    """Chat completions and the Responses API count the prompt once (prompt_tokens with cached_tokens as a detail of
    it): nothing to change, nothing read for it."""
    relay = live.Relay(tmp_path, server.server_address[1])
    try:
        relay.server.config["llm"][0]["kind"] = "lmstudio"
        chat = json.dumps({"id": "c", "object": "chat.completion", "model": "m", "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "DONE"}}],
            "usage": {"prompt_tokens": 49217, "completion_tokens": 5, "total_tokens": 49222,
                      "prompt_tokens_details": {"cached_tokens": 49191}}}).encode()
        Server.script = [chat]
        _, body = live.dechunk(relay.post("/v1/chat/completions", {"model": "m", "messages": REQUEST["messages"]}))
        assert body == chat and "llm_usage" not in relay.records(1)[0]
    finally:
        relay.close()
