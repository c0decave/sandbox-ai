"""The loop guard's cut of a streamed answer ends between two server-sent events, never inside one.

The local-model research's round 4 (Nemotron 3.5 Super VL in OpenCode, runs/smoke__opencode__nemovl__b1): LM Studio
wrote an event's "data: {...}" line and its blank line in two writes; the guard tripped on the line (a tool call's
arguments repeating one block), the relay sent its ending right after it, and the agent's SSE parser read the
server's last event and the guard's ending as ONE event - "JSON parsing failed: Text: {...}\\n{...}" - so OpenCode
ended the session with an error instead of reading the note. The guard now waits for the end of the event it is in
(the blank line) before it cuts; the rest of the answer never reaches the agent either way.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_boundary", "sandbox/runtime.py")
llm = load("sandbox_llm_boundary", "sandbox/llm.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
import model_mock  # noqa: E402

pytestmark_e2e = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the proxy needs Unix sockets")

BLOCK = '"expected_sha256":"\\n\\n\\n\\n    return \\"Hello, name\\n\\n   def shout(name: str) -> str:\\n", '


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def tool_chunk(arguments):
    return {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "type": "function",
                                                               "function": {"arguments": arguments}}]},
                         "finish_reason": None}]}


def events_of(stream):
    """The data of each server-sent event of STREAM (bytes), as an SSE client joins them: the lines of one event
    (up to a blank line) joined with a line break."""
    found, data = [], []
    for line in stream.decode().split("\n"):
        line = line.rstrip("\r")
        if not line:
            if data:
                found.append("\n".join(data))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        found.append("\n".join(data))
    return found


def feed_split(guard, events):
    """Feed EVENTS (dicts) as a server that writes the data line and the blank line apart; (what went to the agent,
    the reason) once the guard cuts: everything fed up to and including the payload it cut on."""
    sent = b""
    for event in events:
        for payload in (b"data: " + json.dumps(event).encode() + b"\n", b"\n"):
            sent += payload
            tripped = guard.feed(payload)
            if tripped:
                return sent, tripped
    return sent, None


# --- the guard itself ------------------------------------------------------------------------------------------------

def test_a_cut_inside_an_event_waits_for_its_blank_line():
    """The failing case: the repetition is seen on a data line whose blank line has not come yet."""
    guard = rt.LlmStreamGuard("chat", 10)
    sent, tripped = feed_split(guard, [tool_chunk('{"path":"app/greet.py",')] + [tool_chunk(BLOCK)] * 200)
    assert tripped and "repeated one block" in tripped and "(tool0)" in tripped
    assert sent.endswith(b"}\n\n"), "the cut comes at the end of an event"
    stream = sent + guard.ending("[sandbox-ai loop guard] stopped")
    events = events_of(stream)
    assert events[-1] == "[DONE]"
    for data in events[:-1]:
        json.loads(data)  # every event the agent reads is one JSON object (before: the last two joined)
    last = json.loads(events[-2])
    assert last["choices"][0]["finish_reason"] == "stop" and "stopped" in last["choices"][0]["delta"]["content"]


def test_whole_events_are_cut_at_once_as_before():
    guard = rt.LlmStreamGuard("chat", 10)
    tripped, count = None, 0
    while not tripped and count < 300:
        tripped = guard.feed(b"data: " + json.dumps(tool_chunk(BLOCK)).encode() + b"\n\n")
        count += 1
    assert tripped and count < 300


def test_an_event_split_in_the_middle_of_its_line():
    """A payload that ends inside a data line: the guard neither reads the half line nor cuts before the event's end."""
    guard = rt.LlmStreamGuard("chat", 10)
    sent, tripped = b"", None
    for _ in range(300):
        line = b"data: " + json.dumps(tool_chunk(BLOCK)).encode() + b"\n\n"
        for payload in (line[:17], line[17:-1], line[-1:]):
            sent += payload
            tripped = guard.feed(payload)
            if tripped:
                break
        if tripped:
            break
    assert tripped and sent.endswith(b"\n\n")
    assert all(json.loads(data) for data in events_of(sent + guard.ending("[note]"))[:-1])


def test_the_messages_api_waits_for_the_end_of_its_event_too():
    guard = rt.LlmStreamGuard("messages", 10)
    guard.feed(b"event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {
        "id": "msg1", "model": "m"}}).encode() + b"\n\n")
    guard.feed(b"event: content_block_start\ndata: " + json.dumps({"type": "content_block_start", "index": 0,
                                                                  "content_block": {"type": "tool_use"}}).encode()
               + b"\n\n")
    sent, tripped = b"", None
    for _ in range(300):
        for payload in (b"event: content_block_delta\n", b"data: " + json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": BLOCK}}).encode() + b"\n", b"\n"):
            sent += payload
            tripped = guard.feed(payload)
            if tripped:
                break
        if tripped:
            break
    assert tripped and sent.endswith(b"\n\n")
    kinds = [json.loads(data)["type"] for data in events_of(sent + guard.ending("[note]"))]
    assert kinds[-6:] == ["content_block_stop", "content_block_start", "content_block_delta", "content_block_stop",
                          "message_delta", "message_stop"]


def test_an_answer_that_ends_before_its_event_does_is_not_cut():
    """The repetition seen on the last line of an answer that then ends: nothing to cut (the guard returns nothing
    without a blank line, the agent gets the answer as the server sent it)."""
    guard = rt.LlmStreamGuard("chat", 10)
    sent, tripped = b"", None
    for _ in range(200):
        payload = b"data: " + json.dumps(tool_chunk(BLOCK)).encode() + b"\n"
        sent += payload
        tripped = guard.feed(payload)  # one long event of many data lines: never a blank line
    assert tripped is None


# --- end to end: the real proxy between a client and a server that writes its blank lines apart --------------------

class Relay:
    def __init__(self, tmp_path, port, a):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self.fd = os.open(run_dir / "traffic.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        a.llm_addresses = ["127.0.0.1"]
        config = {"body_limit": 65536, "allow": {"loopback": [port]}, "llm": llm.proxy_entry(a)}
        self.server = rt.ProxyServer(config, self.fd, None, run_dir=run_dir,
                                     destination=lambda sock: ("127.0.0.1", port))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def post(self, path, document):
        body = json.dumps(document).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=20) as sock:
            sock.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                         % len(body) + body)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return data

    def close(self):
        self.listener.close()
        os.close(self.fd)


def dechunk(data):
    head, _, body = data.partition(b"\r\n\r\n")
    if b"chunked" not in head.lower():
        return head, body
    out = b""
    while body:
        size, _, rest = body.partition(b"\r\n")
        size = int(size, 16)
        if not size:
            break
        out, body = out + rest[:size], rest[size + 2:]
    return head, out


@pytestmark_e2e
@pytest.mark.parametrize("split", [True, False])
def test_end_to_end_a_repeating_tool_call_is_cut_between_events(tmp_path, split):
    """A server that streams a tool call whose arguments repeat one block, writing each event's blank line apart
    (split) or with it: the agent gets valid events only - the call's start, the note, finish_reason stop, [DONE]."""
    server, port = model_mock.start(models=("nemotron-3.5-super-vl-120b-a12b",), tool_arguments=BLOCK * 300,
                                    chunks=300, split_events=split)
    try:
        llm.main(["add", "box", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port}", "--model",
                  "nemotron-3.5-super-vl-120b-a12b"], out=lambda line: None)
        relay = Relay(tmp_path, port, argparse.Namespace(llm="box", agent="opencode", llm_param=[]))
        head, body = dechunk(relay.post("/v1/chat/completions", {
            "model": "nemotron-3.5-super-vl-120b-a12b", "stream": True,
            "messages": [{"role": "user", "content": "fix it"}],
            "tools": [{"type": "function", "function": {"name": "edit", "parameters": {
                "type": "object", "properties": {"path": {"type": "string"}}, "required": []}}}]}))
        relay.close()
        assert b" 200 " in head.split(b"\r\n", 1)[0]
        events = events_of(body)
        assert events[-1] == "[DONE]"
        parsed = [json.loads(data) for data in events[:-1]]  # the agent's parser: one JSON object per event
        assert any(choice["delta"].get("tool_calls") for p in parsed for choice in p["choices"])
        last = parsed[-1]["choices"][0]
        assert last["finish_reason"] == "stop" and "[sandbox-ai loop guard]" in last["delta"]["content"]
        arguments = "".join(c["function"]["arguments"] for p in parsed for choice in p["choices"]
                            for c in choice["delta"].get("tool_calls") or [])
        assert BLOCK * 10 in arguments and len(arguments) < len(BLOCK) * 100  # cut long before the 300 blocks
    finally:
        server.shutdown()
