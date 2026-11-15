"""The LLM traffic log: what an agent and its model exchange, request by request. The answer put together from its
stream (runtime.LlmAnswer: OpenAI chat completions, the Responses API, Anthropic Messages; tool calls split over
chunks, usage at the end, answers not streamed, gzip, NDJSON, error pages), the record (the request as sent upstream,
the relay's changes, digests for what is new, the limit and its "truncated", redaction, no headers), the file the
supervisor writes (0600, rotated), the proxy end to end (the --llm server's requests by default, the cloud
provider's only with --llm-traffic all), the launcher's options, and `sandbox-ai llm traffic` (transcript, what is
new, summaries, one request, Markdown, runs, follow)."""
import argparse
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import stat
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


rt = load("sandbox_runtime_traffic", "sandbox/runtime.py")
llm = load("sandbox_llm_traffic_llm", "sandbox/llm.py")
traffic = load("sandbox_llm_traffic", "sandbox/llm_traffic.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
import model_mock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("NO_COLOR", "1")


def sse(*events, name=False):
    return b"".join((f"event: {e.get('type')}\n".encode() if name else b"") + b"data: " + json.dumps(e).encode()
                    + b"\n\n" for e in events)


def fed(api, data, pieces=7, **options):
    """LlmAnswer of API after DATA arrived in PIECES pieces of odd sizes (lines and characters split)."""
    answer = rt.LlmAnswer(api, **options)
    step = max(1, len(data) // pieces)
    for start in range(0, len(data), step):
        answer.feed(data[start:start + step])
    return answer.result()


# --- the answer, put together -----------------------------------------------------------------------------------------

def test_openai_chat_completions_stream():
    data = sse({"id": "c1", "model": "qwen3", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
               {"choices": [{"index": 0, "delta": {"content": "lo", "reasoning_content": "first I think"}}]},
               {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                  "function": {"name": "bash", "arguments": "{\"comm"}}]}}]},
               {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "and\": \"ls\"}"}},
                                                                 {"index": 1, "id": "call_2",
                                                                  "function": {"name": "read", "arguments": "{}"}}]}}]},
               {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
               {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 33, "total_tokens": 153,
                                         "prompt_tokens_details": {"cached_tokens": 100}}}) + b"data: [DONE]\n\n"
    result = fed("chat", data, pieces=13)
    assert result["text"] == "Hello" and result["reasoning"] == "first I think"
    assert result["tool_calls"] == [{"id": "call_1", "name": "bash", "arguments": '{"command": "ls"}'},
                                    {"id": "call_2", "name": "read", "arguments": "{}"}]
    assert result["finish_reason"] == "tool_calls" and result["model"] == "qwen3" and result["id"] == "c1"
    assert result["usage"] == {"prompt_tokens": 120, "completion_tokens": 33, "total_tokens": 153,
                               "cached_tokens": 100}
    assert result["streamed"] is True and result["events"] == 6 and "truncated" not in result


def test_responses_api_stream_and_a_stream_cut_before_its_end():
    events = [{"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5", "output": []}},
              {"type": "response.reasoning_summary_text.delta", "delta": "plan: "},
              {"type": "response.output_item.added", "output_index": 1,
               "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "shell", "arguments": ""}},
              {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"cmd\":"},
              {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "\"pwd\"}"},
              {"type": "response.output_text.delta", "item_id": "m1", "delta": "Running pwd"}]
    cut = fed("responses", sse(*events, name=True))
    assert cut["text"] == "Running pwd" and cut["reasoning"] == "plan: "
    assert cut["tool_calls"] == [{"id": "call_9", "name": "shell", "arguments": '{"cmd":"pwd"}'}]
    assert "usage" not in cut and "finish_reason" not in cut
    done = {"type": "response.completed", "response": {
        "id": "resp_1", "status": "completed", "usage": {"input_tokens": 50, "output_tokens": 9,
                                                          "output_tokens_details": {"reasoning_tokens": 4}},
        "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan: "}]},
                   {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "shell",
                    "arguments": "{\"cmd\":\"pwd\"}"},
                   {"type": "message", "content": [{"type": "output_text", "text": "Running pwd"}]}]}}
    whole = fed("responses", sse(*events, done, name=True))
    assert whole["text"] == "Running pwd" and whole["reasoning"] == "plan: "  # the deltas not counted twice
    assert whole["tool_calls"] == [{"id": "call_9", "name": "shell", "arguments": '{"cmd":"pwd"}'}]
    assert whole["usage"] == {"input_tokens": 50, "output_tokens": 9, "reasoning_tokens": 4}
    assert whole["finish_reason"] == "completed" and whole["model"] == "gpt-5"
    incomplete = fed("responses", sse({"type": "response.incomplete", "response": {
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}}))
    assert incomplete["finish_reason"] == "max_output_tokens"


def test_anthropic_messages_stream():
    data = sse({"type": "message_start", "message": {"id": "msg_1", "model": "claude-x", "usage": {
                    "input_tokens": 400, "cache_read_input_tokens": 300, "output_tokens": 1}}},
               {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Look."}},
               {"type": "content_block_stop", "index": 0},
               {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "I'll read it."}},
               {"type": "content_block_start", "index": 2,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}},
               {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"pa"}},
               {"type": "content_block_delta", "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": "th\": \"/work/a.py\"}"}},
               {"type": "content_block_stop", "index": 2},
               {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 61}},
               {"type": "message_stop"}, name=True)
    result = fed("messages", data, pieces=29)
    assert result["text"] == "I'll read it." and result["reasoning"] == "Look."
    assert result["tool_calls"] == [{"id": "toolu_1", "name": "Read", "arguments": '{"path": "/work/a.py"}'}]
    assert result["finish_reason"] == "tool_use" and result["model"] == "claude-x"
    assert result["usage"] == {"input_tokens": 400, "cache_read_input_tokens": 300, "output_tokens": 61}


def test_answers_not_streamed_gzip_ndjson_and_error_pages():
    chat = {"id": "x", "model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "plain", "tool_calls": [{"id": "c", "type": "function", "function": {
            "name": "f", "arguments": "{\"a\":1}"}}]}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    result = fed("chat", json.dumps(chat).encode())
    assert result["text"] == "plain" and result["tool_calls"][0]["name"] == "f" and result["streamed"] is False
    packed = gzip.compress(json.dumps(chat).encode())
    assert fed("chat", packed, encoding="gzip")["text"] == "plain"  # gzip undone, in pieces
    assert fed("chat", gzip.compress(sse(*[{"choices": [{"delta": {"content": c}}]} for c in "abc"])),
               encoding="gzip")["text"] == "abc"
    message = {"type": "message", "id": "m1", "model": "claude", "stop_reason": "end_turn", "content": [
        {"type": "text", "text": "done"}, {"type": "tool_use", "id": "t", "name": "Bash", "input": {"c": "ls"}}],
        "usage": {"input_tokens": 9, "output_tokens": 4}}
    result = fed("messages", json.dumps(message).encode())
    assert result["text"] == "done" and result["tool_calls"] == [{"id": "t", "name": "Bash", "arguments": '{"c": "ls"}'}]
    response = {"object": "response", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "hi"}]}], "usage": {"input_tokens": 1}}
    assert fed("responses", json.dumps(response).encode())["text"] == "hi"
    ndjson = b"".join(json.dumps({"choices": [{"delta": {"content": c}}]}).encode() + b"\n" for c in "xyz")
    assert fed("chat", ndjson)["text"] == "xyz"
    page = fed("chat", b"<html>502 Bad Gateway</html>")
    assert page["error"] == {"body": "<html>502 Bad Gateway</html>"} and "text" not in page
    error = fed("chat", json.dumps({"error": {"message": "model not loaded", "code": 404}}).encode())
    assert error["error"]["message"] == "model not loaded"
    assert fed("chat", b"\x00", encoding="br") == {"streamed": False, "unreadable": "compressed (br)"}
    assert fed("chat", b"not gzip at all", encoding="gzip")["unreadable"] == "damaged compression"
    assert fed("chat", b"") == {"streamed": False}


def test_a_huge_answer_is_cut_and_says_so():
    data = sse(*[{"choices": [{"delta": {"content": "x" * 1000}}]} for _ in range(100)])
    result = fed("chat", data, limit=10000)
    assert len(result["text"]) == 10000 and result["truncated"] is True
    whole = json.dumps({"choices": [{"message": {"content": "y" * 50000}}]}).encode()
    assert fed("chat", whole, limit=1000)["truncated"] is True  # not streamed: the body beyond the limit is not kept


# --- the record ------------------------------------------------------------------------------------------------------

def loop_entry():
    return {"name": "gpu", "kind": "llamacpp", "params": {"*": {"temperature": 0.7, "max_tokens": 2048}},
            "forced": {"*": ["temperature"]}, "override": {}, "presets": {},
            "sent": {"chat": ["temperature", "max_tokens", "top_k"]},
            "loop": {"enabled": True, "warn": 3, "block": 6}}


def history(count):
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "fix the test"}]
    for number in range(count):
        messages += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{number}", "type": "function",
                                                                           "function": {"name": "bash",
                                                                                        "arguments": '{"c":"pytest"}'}}]},
                     {"role": "tool", "tool_call_id": f"c{number}", "content": "1 failed"}]
    return {"model": "qwen3", "temperature": 0.1, "max_tokens": 32000, "stream": True, "messages": messages}


def relayed(document, entry=None):
    """(document as sent, done) of DOCUMENT through the relay's body rewrite with the traffic log on."""
    body = json.dumps(document).encode()
    head = b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(body)
    _, parts, done = rt.llm_body(head, [(body, body)], [("Content-Type", "application/json")], entry or loop_entry(),
                                 "chat", keep=True)
    return done.pop("document"), done


def test_the_relay_changes_are_listed_and_the_note_does_not_hide_what_is_new():
    sent, done = relayed(history(3))
    before = done.pop("before")
    changes = rt.llm_changes(before, sent, done)
    fields = {c["field"]: c for c in changes}
    assert fields["temperature"] == {"field": "temperature", "from": 0.1, "to": 0.7, "why": "parameter"}
    assert fields["max_tokens"] == {"field": "max_tokens", "from": 32000, "to": 2048, "why": "parameter"}
    note = fields["messages"]
    assert note["why"] == "loop guard" and note["where"] == "the result of a tool call" and "[sandbox-ai loop guard]" in \
        note["note"]
    # The digests are the agent's: the next request (the note not in it) continues this one.
    original = history(3)
    digests = rt.llm_digests(sent, "chat", done["note"])
    assert digests == [rt.llm_digest("")] + [rt.llm_digest(m) for m in original["messages"]]
    assert "[sandbox-ai loop guard]" in sent["messages"][-1]["content"]  # put back after the digests
    # The block step changes no tool_choice and no tools (a server would render the prompt anew): the note only.
    paused, done = relayed(history(6))
    changes = rt.llm_changes(done.pop("before"), paused, done)
    assert not [c for c in changes if c["field"] in ("tool_choice", "tools")]
    assert [c for c in changes if c["field"] == "messages" and c["why"] == "loop guard"]
    ollama = {**loop_entry(), "kind": "ollama"}
    document = {**history(6), "tools": [{"type": "function", "function": {"name": "bash"}}]}
    kept, done = relayed(document, ollama)
    changes = rt.llm_changes(done.pop("before"), kept, done)
    assert kept["tools"] and not [c for c in changes if c["field"] in ("tool_choice", "tools")]
    # Nothing changed: no changes; extra_params nested.
    assert rt.llm_changes(rt.llm_snapshot({"a": 1}), {"a": 1}, {"params": {}}) == []
    assert rt.llm_changes(rt.llm_snapshot({"extra_params": {"k": 1}}), {"extra_params": {"k": 2}}, {"boost": ["k"]}) == \
        [{"field": "extra_params.k", "why": "boost", "from": 1, "to": 2}]


def item(document=None, answer=None, **more):
    found = {"time": "2026-11-13T10:00:00.000Z", "source": "llm", "endpoint": "gpu", "host": "127.0.0.1:1234",
             "url": "http://127.0.0.1:1234/v1/chat/completions", "api": "chat", "status": 200, "ms": 812,
             "first_byte_ms": 90, "bytes_out": 100, "bytes_in": 200, "done": {}}
    if document is not None:
        found["document"] = document
    if answer is not None:
        found["answer"] = answer
    found.update(more)
    return found


def recorder(tmp_path, **config):
    read_fd, write_fd = os.pipe()
    path = tmp_path / "llm-traffic-20261113-100000-run-1.jsonl"
    writer = rt.LlmTrafficFile(read_fd, path)
    thread = threading.Thread(target=writer.serve, daemon=True)
    thread.start()
    secrets = config.pop("secrets", ())
    traffic = rt.LlmTraffic({"mode": "llm", "run": "run-1", **config}, write_fd, lambda: secrets)

    def finish():
        traffic.close()
        os.close(write_fd)
        thread.join(5)
        return [json.loads(line) for line in path.read_text().splitlines()]
    return traffic, finish, path


def test_records_are_redacted_bounded_and_written_0600(tmp_path):
    key = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0"  # assembled: no scanner takes the file for a leak
    own = "my-endpoint-secret-value"
    log, finish, path = recorder(tmp_path, secrets=(own,), record_limit=1 << 16)
    answer = rt.LlmAnswer("chat")
    answer.feed(sse({"choices": [{"delta": {"content": f"the key is {key} and {own}"}}]}))
    document = {"model": "m", "messages": [{"role": "user", "content": f"use {key}"},
                                           {"role": "user", "content": '{"api_key": "hunter2-pass"}'}]}
    assert log.submit(item(document, answer))
    big = {"model": "m", "messages": [{"role": "user", "content": "z" * 300000}]}
    assert log.submit(item(big, rt.LlmAnswer("chat")))
    many = {"model": "m", "messages": [{"role": "system", "content": "s"}] +
            [{"role": "user", "content": f"message {n} " + "w" * 900} for n in range(400)]}
    assert log.submit(item(many, rt.LlmAnswer("chat")))
    records = finish()
    assert [r["n"] for r in records] == [1, 2, 3] and records[0]["run"] == "run-1"
    text = path.read_text()
    assert key not in text and own not in text and "hunter2-pass" not in text and "[redacted]" in text
    assert "headers" not in text.replace('"headers"', "") and "Authorization" not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(len(line.encode()) <= 1 << 16 for line in text.splitlines())
    assert records[0].get("truncated") is None and records[0]["response"]["text"].startswith("the key is [redacted]")
    assert records[1]["truncated"] == ["long strings"] and "characters cut" in records[1]["request"]["messages"][0]["content"]
    assert "older messages" in records[2]["truncated"]
    kept = records[2]["request"]["messages"]
    assert kept[0]["content"] == "s" and "older items" in kept[1]["sandbox_ai_omitted"] and \
        kept[-1]["content"].startswith("message 399")
    assert records[2]["digests"][0] == rt.llm_digest("") and len(records[2]["digests"]) == 402  # of everything


def test_the_line_limit_in_every_case():
    record = {"version": 1, "n": 1, "request": {"messages": ["a" * 5000] * 50}, "response": {"text": "b" * 5000}}
    line, cut = rt.llm_traffic_line(record, 4000)
    assert cut and len(line) <= 4000 and json.loads(line)["truncated"]
    line, cut = rt.llm_traffic_line({"version": 1, "n": 2}, 4000)
    assert not cut and json.loads(line) == {"version": 1, "n": 2}
    line, cut = rt.llm_traffic_line({"version": 1, "truncated": ["answer"], "response": {"text": "x"}}, 4000)
    assert cut and json.loads(line)["truncated"] == ["answer"]
    line, cut = rt.llm_traffic_line({"version": 1, "n": 3, "model": "m", "request": {"x": "y" * 9000}}, 300)
    assert cut and len(line) <= 300 and json.loads(line)["n"] == 3


def test_a_body_the_relay_did_not_read_is_read_afterwards(tmp_path):
    body = json.dumps({"model": "claude-x", "system": "sys", "messages": [{"role": "user", "content": "hi"}]}).encode()
    assert rt.llm_request_document(gzip.compress(body), len(gzip.compress(body)), "gzip")[0]["model"] == "claude-x"
    assert rt.llm_request_document(body[:10], len(body), None) == (None, "larger than 1 MiB")
    assert rt.llm_request_document(b"{x", 2, None) == (None, "not valid JSON")
    assert rt.llm_request_document(b"[]", 2, None) == (None, "not a JSON object")
    assert rt.llm_request_document(b"zz", 2, "br") == (None, "compressed (br)")
    log, finish, _ = recorder(tmp_path, mode="all")
    assert log.wants(False, "POST", "/v1/messages") == "provider" and log.wants(True, "POST", "/x") == "llm"
    assert log.wants(False, "GET", "/v1/messages") is None and log.wants(False, "POST", "/upload") is None
    packed = gzip.compress(body)
    log.submit(item(None, rt.LlmAnswer("messages"), api="messages", source="provider", raw=(packed, len(packed), "gzip")))
    log.submit(item(None, None, raw=(b"{x", 2, None)))
    first, second = finish()
    assert first["request"]["system"] == "sys" and first["source"] == "provider" and first["model"] == "claude-x"
    assert second["request"] == {"unread": "not valid JSON"} and "digests" not in second
    off = rt.LlmTraffic({"mode": "off"}, -1)
    assert off.wants(True, "POST", "/v1/chat/completions") is None
    off.close()


def test_the_file_is_rotated_and_old_parts_go(tmp_path):
    read_fd, write_fd = os.pipe()
    path = tmp_path / "llm-traffic-20261113-100000-r.jsonl"
    writer = rt.LlmTrafficFile(read_fd, path, part=1000, parts=2, line_limit=5000)
    thread = threading.Thread(target=writer.serve, daemon=True)
    thread.start()
    for number in range(12):
        os.write(write_fd, json.dumps({"n": number, "pad": "p" * 300}).encode() + b"\n")
    os.write(write_fd, b"x" * 6000)  # one line too long, then a good one
    os.write(write_fd, b"\n" + json.dumps({"n": 99}).encode() + b"\n")
    os.close(write_fd)
    thread.join(5)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [path.name, path.name + ".1", path.name + ".2"]
    assert all(p.stat().st_size <= 1000 for p in tmp_path.iterdir())
    numbers = [json.loads(line)["n"] for name in (path.name + ".2", path.name + ".1", path.name)
               for line in (tmp_path / name).read_text().splitlines()]
    assert numbers == sorted(numbers) and numbers[-1] == 99 and 0 not in numbers and writer.lost == 1
    assert writer.write(b"x\n") is True  # after the pipe ended, writing still works
    blocked = rt.LlmTrafficFile(-1, tmp_path / "missing" / "f.jsonl")
    assert blocked.write(b"x\n") is False and blocked.lost == 1


def test_lines_in_reads_of_any_size_are_written_whole_and_too_long_ones_lost():
    """0.1.95, performance round 1: the reader keeps what it has in place and searches only what is new (a 16 MB
    record in the pipe's 64 KB reads took 4.1 s, now 0.17 s): every line as it was sent, however the reads cut it."""
    import random
    chosen = random.Random(5)
    for _ in range(30):
        lines = [b"%d:" % n + b"x" * chosen.choice([0, 1, 10, 499, 500, 501, 2000]) + b"\n" for n in range(40)]
        read_fd, write_fd = os.pipe()
        writer = rt.LlmTrafficFile(read_fd, "unused", line_limit=505)
        written = []
        writer.write = written.append
        thread = threading.Thread(target=writer.serve, daemon=True)
        thread.start()
        data = b"".join(lines)
        while data:
            size = chosen.choice([1, 2, 7, 64, 300, 506, 4096])
            os.write(write_fd, data[:size])
            data = data[size:]
        os.close(write_fd)
        thread.join(5)
        assert written == [line for line in lines if len(line) <= 505]
        # (one too long is counted again for each time its unended part outgrows the limit, as before)
        assert writer.lost >= sum(1 for line in lines if len(line) > 505)


def test_start_traffic_log(tmp_path):
    assert rt.start_traffic_log({"log": "c.jsonl"}, tmp_path) is None
    assert rt.start_traffic_log({"traffic": "../x.jsonl"}, tmp_path) is None
    assert rt.start_traffic_log({"traffic": "llm-traffic-1-r.jsonl"}, tmp_path / "none") is None
    fd = rt.start_traffic_log({"traffic": "llm-traffic-1-r.jsonl"}, tmp_path)
    os.write(fd, b'{"version":1}\n')
    os.close(fd)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (tmp_path / "llm-traffic-1-r.jsonl").exists():
        time.sleep(0.02)
    time.sleep(0.1)
    assert (tmp_path / "llm-traffic-1-r.jsonl").read_text() == '{"version":1}\n'


# --- end to end: the real proxy ---------------------------------------------------------------------------------------

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


class Relay:
    """The real ProxyServer with an --llm entry and the LLM traffic log (pipe and file), its redirect replaced by the
    mock server."""

    def __init__(self, tmp_path, port, a, mode="llm", llm_port=None):
        run_dir = tmp_path / "run"
        rt.create_ca(run_dir, tmp_path / "ca", bundles=())
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        a.llm_addresses = ["127.0.0.1"]
        config = {"body_limit": 65536, "allow": {"loopback": [port]}, "llm": llm.proxy_entry(a)}
        self.server = rt.ProxyServer(config, self.fd, None, run_dir=run_dir,
                                     destination=lambda sock: ("127.0.0.1", port))
        read_fd, self.write_fd = os.pipe()
        self.path = tmp_path / "llm-traffic-20261113-100000-run-7.jsonl"
        self.writer = threading.Thread(target=rt.LlmTrafficFile(read_fd, self.path).serve, daemon=True)
        self.writer.start()
        self.server.traffic = rt.LlmTraffic({"mode": mode, "run": "run-7"}, self.write_fd, self.server.own_secrets)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def post(self, path, document, extra=b"", body=None):
        body = json.dumps(document).encode() if body is None else body
        with socket.create_connection(("127.0.0.1", self.port), timeout=20) as sock:
            sock.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                         + extra + b"Authorization: Bearer agent-placeholder-token\r\n"
                         + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return data

    def records(self, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = [json.loads(line) for line in self.path.read_text().splitlines()] if self.path.exists() else []
            if len(found) >= count:
                return found
            time.sleep(0.05)
        return found

    def close(self):
        self.listener.close()
        self.server.traffic.close()
        os.close(self.write_fd)
        self.writer.join(5)
        os.close(self.fd)


@needs_openssl
def test_end_to_end_the_llm_servers_requests_by_default(tmp_path):
    server, port = model_mock.start(models=("qwen3-8b",))
    try:
        assert llm.main(["add", "box", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port}", "--model",
                         "qwen3-8b", "--param", "max_tokens=2048"], out=lambda _: None) == 0
        relay = Relay(tmp_path, port, argparse.Namespace(llm="box", agent="opencode", llm_param=[]))
        first = {"model": "qwen3-8b", "max_tokens": 32000, "messages": [{"role": "user", "content": "hi"}]}
        relay.post("/v1/chat/completions", first)
        second = {"model": "qwen3-8b", "stream": True, "messages": [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "mock reply to: hi"},
            {"role": "user", "content": "more"}]}
        relay.post("/v1/chat/completions", second)
        relay.post("/v1/models", {})  # no model request: not recorded (and no POST to a model path)
        records = relay.records(2)
        relay.close()
        one, two = records
        assert one["source"] == "llm" and one["endpoint"] == "box" and one["api"] == "chat" and one["status"] == 200
        assert one["request"]["max_tokens"] == 2048  # as sent upstream
        assert {"field": "max_tokens", "from": 32000, "to": 2048, "why": "parameter"} in one["changes"]
        assert one["response"]["text"] == "mock reply to: hi" and one["ms"] >= 0 and one["bytes_in"] > 0
        assert two["stream"] is True and two["response"]["streamed"] is True and "mock reply" in two["response"]["text"]
        assert "placeholder" not in relay.path.read_text() and "Authorization" not in relay.path.read_text()
        thread = traffic.Thread()
        assert thread.new_from(one) == (0, None) and thread.new_from(two) == (1, 1)  # hi known; two new messages
    finally:
        server.shutdown()


@needs_openssl
def test_end_to_end_other_model_requests_only_with_all(tmp_path):
    server, port = model_mock.start(models=("qwen3-8b",))
    try:
        llm.main(["add", "box", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port + 1}", "--model", "qwen3-8b"],
                 out=lambda _: None)
        document = {"model": "qwen3-8b", "messages": [{"role": "user", "content": "elsewhere"}]}
        relay = Relay(tmp_path, port, argparse.Namespace(llm="box", agent="opencode", llm_param=[]))
        relay.post("/v1/chat/completions", document)  # not the --llm server: a provider's, not recorded by default
        time.sleep(0.3)
        relay.close()
        assert not relay.path.exists()
        (tmp_path / "all").mkdir()
        relay = Relay(tmp_path / "all", port, argparse.Namespace(llm="box", agent="opencode", llm_param=[]), mode="all")
        packed = gzip.compress(json.dumps(document).encode())
        relay.post("/v1/chat/completions", None, b"Content-Encoding: gzip\r\n", body=packed)
        records = relay.records(1)
        relay.close()
        assert records[0]["source"] == "provider" and records[0]["endpoint"] is None
        assert records[0]["request"]["messages"][0]["content"] == "elsewhere" and "changes" not in records[0]
    finally:
        server.shutdown()


# --- the launcher -------------------------------------------------------------------------------------------------------

def launcher():
    return load("sandbox_launcher_traffic", "sandbox/agent_sandbox.py")


def test_the_options_the_defaults_and_the_project_file(tmp_path):
    s = launcher()
    a = s.parse_args(["opencode", "--llm", "box", "-lt", "all", "-ll", "8"])
    a.run_id = "opencode-1"
    assert s.llm_traffic_mode(a) == "all"
    assert s.llm_traffic_config(a) == {"mode": "all", "record_limit": 8 << 20, "run": "opencode-1",
                                       "session": None}
    plain = s.parse_args(["opencode"])
    plain.run_id = "r"
    assert s.llm_traffic_mode(plain) == "llm" and s.llm_traffic_config(plain) is None  # no --llm: nothing to record
    plain.llm_traffic_default = "all"
    assert s.llm_traffic_config(plain)["mode"] == "all"
    assert s.llm_traffic_config(s.parse_args(["opencode", "--llm", "box", "--llm-traffic", "off"])) is None
    assert s.llm_traffic_mode(s.parse_args(["opencode", "--no-proxy", "--llm-traffic", "all"])) == "off"
    with pytest.raises(ValueError, match="from 1 to 64"):
        s.check_traffic_limit(65, "--llm-traffic-limit")
    with pytest.raises(SystemExit):
        s.parse_args(["opencode", "--llm-traffic", "some"])
    defaults = tmp_path / "cfg" / "sandbox-ai" / "defaults.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(json.dumps({"version": 1, "llm_traffic": "all", "llm_traffic_limit": 2}))
    assert s.load_user_defaults(s.parse_args(["opencode"]))["llm_traffic"] == "all"
    defaults.write_text(json.dumps({"version": 1, "llm_traffic": "everything"}))
    with pytest.raises(ValueError, match="llm_traffic is one of"):
        s.load_user_defaults(s.parse_args(["opencode"]))
    assert s.llm_traffic_notes(a) and "cloud provider" in s.llm_traffic_notes(a)[0]
    assert s.llm_traffic_plan(a)["record_limit_mib"] == 8 and s.llm_traffic_plan(plain)["origin"] != "--llm-traffic"


def test_old_traffic_logs_go_beyond_the_limit(tmp_path):
    s = launcher()
    old = time.time() - 7200
    for name in ("llm-traffic-20261101-100000-a.jsonl", "llm-traffic-20261101-100000-a.jsonl.1",
                 "llm-traffic-20261102-100000-b.jsonl", "llm-traffic-20261103-100000-c.jsonl", "notes.txt"):
        (tmp_path / name).write_bytes(b"x" * 100)
        os.utime(tmp_path / name, (old, old))
    removed = s.rotate_traffic_logs(tmp_path, limit=250)
    assert removed == ["llm-traffic-20261101-100000-a.jsonl.1", "llm-traffic-20261101-100000-a.jsonl"]
    assert (tmp_path / "notes.txt").exists() and s.rotate_traffic_logs(tmp_path / "none") == []


# --- sandbox-ai llm traffic -------------------------------------------------------------------------------------------

def conversation_log(directory, run="run-1"):
    """Three requests of one conversation and one of another, as the proxy writes them."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"llm-traffic-20261113-100000-{run}.jsonl"
    base = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "list the files"}]
    turns = [base, base + [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{\"c\":\"ls\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py\nb.py"}]]
    turns.append(turns[1] + [{"role": "assistant", "content": "a.py and b.py"}, {"role": "user", "content": "thanks"}])
    answers = [{"tool_calls": [{"id": "c1", "name": "bash", "arguments": "{\"c\":\"ls\"}"}],
                "finish_reason": "tool_calls"}, {"text": "a.py and b.py", "finish_reason": "stop"},
               {"text": "You're welcome\u001b[31m", "finish_reason": "stop"}]
    lines = []
    for number, (messages, answer) in enumerate(zip(turns, answers), 1):
        document = {"model": "qwen3", "temperature": 0.7, "messages": messages}
        lines.append({"version": 1, "n": number, "time": f"2026-11-13T10:00:0{number}.000Z", "run": run,
                      "source": "llm", "endpoint": "gpu", "host": "127.0.0.1:1234", "api": "chat", "model": "qwen3",
                      "stream": True, "status": 200, "ms": 1500, "digests": rt.llm_digests(document, "chat"),
                      "request": document, "response": {**answer, "usage": {"prompt_tokens": 10 * number,
                                                                            "completion_tokens": number}},
                      "changes": [{"field": "temperature", "from": 0.1, "to": 0.7, "why": "parameter"}]})
    title = {"model": "qwen3", "messages": [{"role": "user", "content": "a title for: list the files"}]}
    lines.append({"version": 1, "n": 4, "time": "2026-11-13T10:00:05.000Z", "run": run, "api": "chat",
                  "model": "qwen3", "status": 200, "ms": 20, "digests": rt.llm_digests(title, "chat"),
                  "request": title, "response": {"text": "Files"}, "loop": {"action": "note", "kind": "repeat"},
                  "truncated": ["long strings"]})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json\n")
    return path


def cli(*argv):
    lines = []
    status = traffic.cli(list(argv), out=lines.append)
    return status, "\n".join(lines)


def test_the_transcript_shows_what_is_new(tmp_path):
    conversation_log(tmp_path / "logs")
    status, text = cli("--log-dir", str(tmp_path / "logs"))
    assert status == 0
    blocks = text.split("\n\n")
    assert "#1" in blocks[0] and "new in this request: 2 of 2 messages (a new conversation)" in blocks[0]
    assert "be brief" in blocks[0] and "tool call bash" in blocks[0]
    assert "new in this request: 2 of 4 messages (continues #1)" in blocks[1] and "a.py" in blocks[1]
    assert "list the files" not in blocks[1].split("-- answer")[0].split("messages (continues #1)")[1]
    assert "relay changed temperature: 0.1 -> 0.7 (parameter)" in blocks[1]
    assert "continues #2" in blocks[2] and "\\x1b[31m" in blocks[2] and "\x1b" not in text  # escapes shown escaped
    assert "a new conversation" in blocks[3] and "loop guard: note" in blocks[3] and "truncated: long strings" in blocks[3]
    full = cli("--log-dir", str(tmp_path / "logs"), "--full", "--request", "3")[1]
    assert "list the files" in full and "parameters:" in full and "temperature" in full


def test_summaries_one_request_markdown_runs_tail_grep(tmp_path):
    logs = tmp_path / "logs"
    conversation_log(logs)
    conversation_log(logs, "run-0")
    (logs / "llm-traffic-20261112-090000-run-0.jsonl").write_text(
        (logs / "llm-traffic-20261113-100000-run-0.jsonl").read_text())
    (logs / "llm-traffic-20261113-100000-run-0.jsonl").unlink()
    status, text = cli("--log-dir", str(logs), "--summary", "--json")
    rows = [json.loads(line) for line in text.splitlines()]
    assert [r["n"] for r in rows] == [1, 2, 3, 4] and rows[0]["run"] == "run-1"  # the newest run
    assert rows[1]["new"] == 2 and rows[1]["continues"] == 1 and rows[1]["tokens_in"] == 20
    assert rows[3]["loop"] == ["note"] and rows[3]["truncated"] == ["long strings"]
    assert "#2" in cli("--log-dir", str(logs), "--summary")[1]
    one = json.loads(cli("--log-dir", str(logs), "--request", "2", "--json")[1])
    assert one["record"]["n"] == 2 and one["view"]["new_from"] == 2 and one["continues"] == 1
    assert [m["new"] for m in one["view"]["messages"]] == [False, False, True, True]
    with pytest.raises(ValueError, match="no request 9"):
        cli("--log-dir", str(logs), "--request", "9")
    markdown = cli("--log-dir", str(logs), "--markdown")[1]
    assert markdown.startswith("# LLM traffic of run-1") and "### Answer (tool_calls)" in markdown and "```" in markdown
    runs = json.loads(cli("--log-dir", str(logs), "--runs", "--json")[1])
    assert [r["run"] for r in runs] == ["run-1", "run-0"] and runs[0]["started"] == "2026-11-13 10:00:00"
    assert "run-0" in cli("--log-dir", str(logs), "--runs")[1]
    tail = [json.loads(line) for line in cli("--log-dir", str(logs), "--tail", "2", "--summary", "--json")[1].splitlines()]
    assert [r["n"] for r in tail] == [3, 4] and tail[0]["continues"] == 2  # what came before counted
    found = [json.loads(line) for line in cli("--log-dir", str(logs), "--grep", "WELCOME", "--summary", "--json")[1]
             .splitlines()]
    assert [r["n"] for r in found] == [3]
    assert cli("--log-dir", str(logs), "run-0", "--summary", "--json")[1].count('"run-0"') == 4
    with pytest.raises(ValueError, match="no LLM traffic log"):
        cli("--log-dir", str(tmp_path / "empty"))
    assert llm.main(["traffic", "--log-dir", str(logs), "--summary"]) == 0


def test_follow_reads_what_comes_also_across_a_rotation(tmp_path):
    logs = tmp_path / "logs"
    path = conversation_log(logs)
    follow = traffic.Follow(logs, ["run-1"])
    follow.mark()
    assert follow.read() == []
    record = json.loads(path.read_text().splitlines()[0])
    with open(path, "a") as stream:
        stream.write(json.dumps({**record, "n": 5}) + "\n" + json.dumps({**record, "n": 6})[:20])
    assert [r["n"] for r in follow.read()] == [5]
    os.replace(path, path.with_name(path.name + ".1"))
    with open(path.with_name(path.name + ".1"), "a") as stream:
        stream.write(json.dumps({**record, "n": 6})[20:] + "\n")
    path.write_text(json.dumps({**record, "n": 7}) + "\n")
    assert [r["n"] for r in follow.read()] == [6, 7]
    follow.close()
    waiting = traffic.Follow(tmp_path / "later", [])
    waiting.mark()
    assert waiting.read() == []
    conversation_log(tmp_path / "later", "run-9")
    assert [r["n"] for r in waiting.read()] == [1, 2, 3, 4]
    waiting.close()


def test_a_full_queue_drops_and_the_next_record_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "LLM_TRAFFIC_QUEUE_BYTES", 100)
    log, finish, _ = recorder(tmp_path)
    with log.lock:  # the writer waits: what comes meanwhile queues
        assert log.submit({**item({"model": "m", "messages": []}), "size": 80})
        assert not log.submit({**item({"model": "m", "messages": []}), "size": 80})  # over the bytes: dropped
    time.sleep(0.2)
    assert log.submit({**item({"model": "m", "messages": []}), "size": 80})  # alone in the queue: always taken
    first, second = finish()
    # the next record written says how many were lost (here the first: it was written after the drop)
    assert first["n"] == 1 and first["lost_before"] == 1 and second["n"] == 2 and "lost_before" not in second
