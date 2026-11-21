"""--llm with LM Studio and structured output: the JSON a thinking model gives reaches the agent as the answer.

The local-model research, round 5 (use case 8, runs/j8_api__api__{orn35,orn122,q36}__b1 and probes/jsonprobe_orn35):
with "response_format": {"type": "json_schema", ...} on /v1/chat/completions, LM Studio answered Ornith-1.5-35B,
Ornith-1.5-122B and Qwen3.6-35B (Qwen3.5-family MLX models that think) with an EMPTY content and the schema's JSON as
the reasoning ("reasoning_content": "{ \\"1\\": \\"bug\\", \\"2\\": \\"billing\\" }", finish stop) - also with the
thinking switched off; three of six jobs of the small-jobs task got nothing. Without response_format the same model
answers in content. The relay gives the agent the JSON where it asked for it: for an LM Studio endpoint and a chat
request with response_format json_schema or json_object, an answer whose content is empty and whose reasoning is the
JSON gets it as its content (streamed: the reasoning deltas become content deltas; not streamed: the message's)."""
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

rt = live.rt
SCHEMA = {"type": "json_schema", "json_schema": {"name": "t", "strict": True, "schema": {"type": "object"}}}
JSON = '{ "1": "bug", "2": "billing" }'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def test_which_requests_ask_for_structured_output():
    assert rt.llm_structured_request({"response_format": SCHEMA}) is True
    assert rt.llm_structured_request({"response_format": {"type": "json_object"}}) is True
    for document in ({}, {"response_format": {"type": "text"}}, {"response_format": "json"}, None, []):
        assert rt.llm_structured_request(document) is False


def test_a_whole_answer_gets_its_json_as_content():
    answer = {"id": "c", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "", "reasoning_content": JSON}}]}
    out = json.loads(rt.LlmStructuredAnswer().whole(json.dumps(answer).encode()))
    assert out["choices"][0]["message"]["content"] == JSON and "reasoning_content" not in out["choices"][0]["message"]
    # LM Studio's other name for it, "reasoning"
    answer["choices"][0]["message"] = {"role": "assistant", "content": None, "reasoning": JSON}
    assert json.loads(rt.LlmStructuredAnswer().whole(json.dumps(answer).encode()))["choices"][0]["message"][
        "content"] == JSON


def test_an_answer_with_content_or_reasoning_that_is_no_json_stays_as_it_came():
    for message in ({"role": "assistant", "content": JSON, "reasoning_content": "thought"},
                    {"role": "assistant", "content": "", "reasoning_content": "I think the first is a bug"}):
        data = json.dumps({"choices": [{"index": 0, "message": message}]}).encode()
        assert rt.LlmStructuredAnswer().whole(data) == data
    assert rt.LlmStructuredAnswer().whole(b"not json") == b"not json"


def test_a_stream_gets_its_reasoning_deltas_as_content():
    def event(delta):
        return b"data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": delta, "finish_reason": None}]}).encode() + b"\n\n"
    stream = event({"role": "assistant"}) + event({"reasoning_content": '{ "1": '}) + \
        event({"reasoning_content": '"bug" }'}) + b"data: [DONE]\n\n"
    answer = rt.LlmStructuredAnswer()
    out = b"".join(answer.feed(stream[i:i + 17]) for i in range(0, len(stream), 17)) + answer.flush()
    deltas = [json.loads(line[5:])["choices"][0]["delta"] for line in out.split(b"\n")
              if line.startswith(b"data: {")]
    assert "".join(d.get("content", "") for d in deltas) == '{ "1": "bug" }'
    assert not any("reasoning_content" in d for d in deltas) and out.endswith(b"data: [DONE]\n\n")
    # a stream that has content of its own: the reasoning stays reasoning
    mixed = event({"reasoning_content": "hm"}) + event({"content": JSON}) + b"data: [DONE]\n\n"
    answer = rt.LlmStructuredAnswer()
    assert answer.feed(mixed) + answer.flush() == mixed   # reasoning that is no JSON: thinking, kept as it came


class Server(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        data = Server.script.pop(0)
        streamed = data.startswith(b"data:")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if streamed else "application/json")
        if streamed:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(data), 40):
                piece = data[start:start + 40]
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


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("kind,fixed", [("lmstudio", True), ("llamacpp", False)])
def test_regression_e2e_structured_output_of_a_thinking_model_reaches_the_agent(tmp_path, server, kind, fixed):
    """runs/j8_api__api__orn35__b1: content 0 characters, reasoning the JSON. Through the real proxy the agent gets the
    JSON as the answer's content - not streamed and streamed; a request without response_format passes as it came."""
    relay = live.Relay(tmp_path, server.server_address[1])
    try:
        relay.server.config["llm"][0]["kind"] = kind
        whole = json.dumps({"id": "c", "object": "chat.completion", "model": "m", "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "",
                                                              "reasoning_content": JSON}}]}).encode()
        chunk = lambda delta: b"data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "choices": [  # noqa: E731
            {"index": 0, "delta": delta, "finish_reason": None}]}).encode() + b"\n\n"
        stream = chunk({"role": "assistant"}) + chunk({"reasoning_content": JSON}) + b"data: [DONE]\n\n"
        Server.script = [whole, stream, whole]
        request = {"model": "m", "messages": [{"role": "user", "content": "classify"}], "response_format": SCHEMA}
        _, body = live.dechunk(relay.post("/v1/chat/completions", request))
        content = json.loads(body)["choices"][0]["message"].get("content")
        assert content == (JSON if fixed else "")
        _, body = live.dechunk(relay.post("/v1/chat/completions", {**request, "stream": True}))
        assert (live.read_answer("chat", body).get("text") or "") == (JSON if fixed else "")
        plain = {"model": "m", "messages": [{"role": "user", "content": "classify"}]}
        _, body = live.dechunk(relay.post("/v1/chat/completions", plain))
        assert body == whole
    finally:
        relay.close()
