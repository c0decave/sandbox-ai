"""The loop guard against the real harnesses (live report, hotfix 0.1.96): "i had a lot of mcp issues, web fetch
failures, endless loops. and some took ages before they finished" - local models with --llm. The local-model
research reproduced the causes with each harness's real CLI against a scripted model that repeats one tool call for
ever (/work/research-llm/phase2, runs/loopsh_guard_* and runs/loopfix_*):

- G1: Codex's exec_command result starts with "Chunk ID: <6 hex digits>", random per call: no two results were the
  same, the guard never fired (150 identical calls: runs/loopsh_guard_codex).
- G2: DSH puts notes of its own ("Repeated tool call detected ...") as user messages after a tool result; each
  restarted the count as if the human had written (14 identical calls instead of 6: runs/loopsh_guard_dsh).
- G3: the block step sent tool_choice none; LM Studio then renders the prompt without the tools (two full prefills
  of 15-18 s each on an 80B model). Now the request goes out unchanged with the note, and an answer that calls a tool
  again is answered by the relay in the model's place: the turn ends in every harness (runs/loopfix_*).

Unit tests of runtime.llm_* and end to end through the real proxy (ProxyServer) with a scripted model that always
calls a tool, in each API shape (chat completions, the Responses API, Anthropic Messages; streamed and not)."""
import argparse
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_loop_live", "sandbox/runtime.py")
llm = load("sandbox_llm_loop_live", "sandbox/llm.py")
safeguards = load("sandbox_safeguards_loop_live", "sandbox/safeguards.py")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def entry(kind="lmstudio", **changes):
    found = {"name": "mac", "kind": kind, "params": {}, "forced": {}, "override": {}, "presets": {},
             "sent": {api: list(keys) for api, keys in llm.SENT[kind].items()}, "loop": dict(llm.LOOP_DEFAULTS)}
    found.update(changes)
    return found


def check(document, api):
    return rt.llm_loop_check(rt.llm_tool_history(document, api), dict(llm.LOOP_DEFAULTS))


# --- G1: volatile tokens of the harnesses' tool results ---------------------------------------------------------------

def codex_output(chunk, wall="0.0512"):
    """Codex's exec_command result (unified exec) as it reaches the model."""
    return (f"Chunk ID: {chunk}\nWall time: {wall} seconds\nProcess exited with code 0\nOriginal token count: 9\n"
            "Output:\n/usr/bin/python3: No module named pytest\n")


def codex_loop(count):
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run the tests"}]}]
    for i in range(count):
        items += [{"type": "function_call", "call_id": f"call_{i}", "name": "exec_command",
                   "arguments": '{"cmd": "python3 -m pytest -q 2>&1 | tail -3"}'},
                  {"type": "function_call_output", "call_id": f"call_{i}",
                   "output": codex_output(f"{(0x8818fc + 7919 * i) % 0xffffff:06x}", f"0.0{i}12")}]
    return {"model": "qwen/qwen3-coder-next", "input": items, "tools": [{"type": "function", "name": "exec_command"}]}


def test_regression_codex_chunk_id_does_not_hide_a_loop():
    """runs/loopsh_guard_codex: 150 identical exec_command calls, no note - each result had its own Chunk ID."""
    assert rt.llm_result_key(codex_output("8818fc")) == rt.llm_result_key(codex_output("03a9e1", "1.25"))
    assert check(codex_loop(3), "responses")[:3] == ("warn", "exec_command", 3)
    assert check(codex_loop(6), "responses")[:3] == ("block", "exec_command", 6)


@pytest.mark.parametrize("first,second", [
    # Codex: a process still running gets a session id of its own
    ("Process running with session ID 3\nOutput:\nwaiting", "Process running with session ID 17\nOutput:\nwaiting"),
    # UUIDs (session ids, request ids, Claude Code's tool-result files): every group, not only the long ones
    ("run 123e4567-e89b-12d3-a456-426614174000 failed", "run 9f1c2d3e-0a1b-4c5d-8e9f-0123456789ab failed"),
    # call and item ids the harnesses print into results (OpenCode's tool-output files, background shells)
    ("saved to /root/.local/share/opencode/tool-output/tool_01JX4Z9K2M3N", "saved to "
     "/root/.local/share/opencode/tool-output/tool_01JX5A0B7C8D"),
    ("shell started: call_Ab3dE9fG2h", "shell started: call_Zx81QwErTy"),
    ("output in toolu_01AbCdEf23GhIj.txt", "output in toolu_01XyZ987LmNoPq.txt"),
    # epoch timestamps (seconds, milliseconds)
    ('{"ok": false, "at": 1794761460}', '{"ok": false, "at": 1794761533}'),
    ('{"time": 1794761529562}', '{"time": 1794761641172}'),
    # process ids however written, durations, temporary files
    ("PID: 4242 exited 1", "PID: 4377 exited 1"),
    ("pid=4242 exited 1", "pid4377 exited 1"),
    ("Took 1.4s, exit 1", "Took 0.9s, exit 1"),
    ("wrote /tmp/tmpa1b2c3.log", "wrote /tmp/tmpz9y8x7.log"),
])
def test_volatile_parts_of_the_harnesses_results(first, second):
    assert rt.llm_result_key(first) == rt.llm_result_key(second)


@pytest.mark.parametrize("first,second", [
    ("1 failed", "2 failed"),
    ("tool_output is missing", "tool_result is missing"),   # words with an underscore are no ids
    ("exit code 1", "exit code 2"),
    ("line 1234: error", "line 1235: error"),                # small numbers are content
    ("Chunk ID: 8818fc\nOutput:\nA", "Chunk ID: 8818fc\nOutput:\nB"),
])
def test_what_stays_content(first, second):
    assert rt.llm_result_key(first) != rt.llm_result_key(second)


# --- G2: a harness's own note is not a message of the human -----------------------------------------------------------

def chat_loop(count, notes=None, human=None):
    """An OpenAI chat conversation of COUNT identical bash calls; NOTES {after call i: text} as user messages right
    after that call's result (DSH's own), HUMAN {after call i: text} likewise (typed while the agent worked)."""
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "run the tests"}]
    for i in range(count):
        messages += [{"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": "bash", "arguments": '{"command": "python3 -m pytest -q"}'}}]},
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "no tests ran in 0.00s"}]
        for extra in ((notes or {}).get(i), (human or {}).get(i)):
            if extra:
                messages.append({"role": "user", "content": extra})
    return {"model": "m", "messages": messages, "tools": [{"type": "function", "function": {"name": "bash"}}]}


DSH_NOTES = {2: "You are repeating the exact same tool call (bash) with the same arguments. Try something else.",
             4: "Repeated tool call detected: bash was called 5 times with identical arguments."}


def test_regression_dsh_notes_do_not_restart_the_count():
    """runs/loopsh_guard_dsh: DSH's two notes restarted the count each time - 14 identical calls instead of 6."""
    assert check(chat_loop(3, DSH_NOTES), "chat")[:3] == ("warn", "bash", 3)
    assert check(chat_loop(6, DSH_NOTES), "chat")[:3] == ("block", "bash", 6)
    document = chat_loop(6, DSH_NOTES)
    done = rt.llm_request(entry(), "chat", document)
    assert done["loop"]["action"] == "paused tools" and done["loop"]["count"] == 6


def test_a_message_of_the_human_still_restarts_the_count():
    # Typed while the agent worked: it follows a tool result too, but is no note of a harness.
    assert check(chat_loop(8, human={5: "stop running the tests, read the README"}), "chat")[0] is None
    # The human quoting a note, after the model's text answer: the human's.
    document = chat_loop(6)
    document["messages"] += [{"role": "assistant", "content": "The tests do not run."},
                             {"role": "user", "content": "Repeated tool call detected? Try pip install pytest."}]
    assert check(document, "chat")[0] is None
    # A note-like text as the first message: the human's.
    assert rt.llm_human_turns([{"role": "user", "content": "Repeated tool call detected"}], "chat") == [0]


def test_notes_in_the_responses_and_messages_apis():
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
    for i in range(6):
        items += [{"type": "function_call", "call_id": f"f{i}", "name": "shell", "arguments": '{"cmd": "ls"}'},
                  {"type": "function_call_output", "call_id": f"f{i}", "output": "a"}]
        if i == 3:
            items.append({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Repeated tool call detected: shell"}]})
    assert check({"input": items}, "responses")[0] == "block"
    assert rt.llm_human_turns(items, "responses") == [0]
    messages = [{"role": "user", "content": "go"}]
    for i in range(6):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                        "input": {"command": "ls"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "a"}]}]
        if i == 3:
            messages.append({"role": "user", "content": "You are repeating the exact same tool call"})
    assert check({"messages": messages}, "messages")[0] == "block"


def test_the_watchdog_turn_goes_on_over_a_harness_note():
    """The watchdog's turn is told apart by the human's last message: a harness's note starts none."""
    before = rt.LlmTurns.key(chat_loop(2), "chat")
    assert rt.LlmTurns.key(chat_loop(5, DSH_NOTES), "chat") == before
    assert rt.LlmTurns.key(chat_loop(8, human={5: "try another way"}), "chat") != before


# --- G3: the block step without tool_choice none; a tool call after it is answered by the relay ------------------------

def responses_loop(count):
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run the tests"}]}]
    for i in range(count):
        items += [{"type": "function_call", "call_id": f"c{i}", "name": "exec_command", "arguments": '{"cmd": "ls"}'},
                  {"type": "function_call_output", "call_id": f"c{i}", "output": "a b"}]
    return {"model": "m", "input": items, "tools": [{"type": "function", "name": "exec_command"}]}


def messages_loop(count):
    messages = [{"role": "user", "content": "run the tests"}]
    for i in range(count):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                        "input": {"command": "ls"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "a b"}]}]
    return {"model": "m", "messages": messages, "tools": [{"name": "Bash", "input_schema": {"type": "object"}}]}


LOOPS = {"chat": lambda n: chat_loop(n), "responses": responses_loop, "messages": messages_loop}


@pytest.mark.parametrize("api", ["chat", "responses", "messages"])
@pytest.mark.parametrize("kind", ["lmstudio", "ollama", "vllm"])
def test_the_block_step_leaves_the_tools_and_tool_choice_alone(api, kind):
    """LM Studio renders a prompt without the tools for tool_choice none: two full prefills of a long conversation
    (15-18 s each on an 80B model). The request keeps its tools and tool_choice; the note says to stop."""
    document = LOOPS[api](6)
    document["tool_choice"] = "auto" if api != "messages" else {"type": "auto"}
    tools, choice = json.dumps(document["tools"]), json.dumps(document["tool_choice"])
    done = rt.llm_request(entry(kind), api, document)
    assert done["loop"]["action"] == "paused tools" and done["loop"]["count"] == 6
    assert json.dumps(document["tools"]) == tools and json.dumps(document["tool_choice"]) == choice
    assert rt.LLM_NOTE in json.dumps(document) and "summarize" in json.dumps(document)
    assert done["stop"].startswith(rt.LLM_NOTE) and "6 times" in done["stop"] and "ended" in done["stop"]
    # A note only (warn): nothing to hold.
    assert "stop" not in rt.llm_request(entry(kind), api, LOOPS[api](3))


def test_the_watchdog_holds_the_answer_too():
    turns = rt.LlmTurns()
    found = entry(loop={**llm.LOOP_DEFAULTS, "watchdog_requests": 2})
    for count in range(1, 4):
        document = chat_loop(1)
        document["messages"][-1]["content"] = str(count)
        done = rt.llm_request(found, "chat", document, turns)
    assert done["loop"]["kind"] == "watchdog" and "tool_choice" not in document and "watchdog" in done["stop"]


def tool_answer(api, stream):
    """A model's answer that calls a tool, in API's shape (streamed: server-sent events)."""
    def sse(events):
        return b"".join((f"event: {name}\n" if name else "").encode() + b"data: " +
                        (data if isinstance(data, bytes) else json.dumps(data).encode()) + b"\n\n"
                        for name, data in events)
    if api == "chat":
        call = {"id": "call_9", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
        if not stream:
            return json.dumps({"id": "x", "object": "chat.completion", "model": "m", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                 "finish_reason": "tool_calls"}]}).encode()
        base = {"id": "x", "object": "chat.completion.chunk", "model": "m"}
        return sse([(None, {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                        {"index": 0, **call}]}, "finish_reason": None}]}),
                    (None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
                    (None, b"[DONE]")])
    if api == "responses":
        item = {"type": "function_call", "id": "fc_1", "call_id": "c9", "name": "exec_command",
                "arguments": '{"cmd": "ls"}', "status": "completed"}
        response = {"id": "resp_1", "object": "response", "status": "completed", "model": "m", "output": [item]}
        if not stream:
            return json.dumps(response).encode()
        return sse([("response.created", {"type": "response.created", "response": {**response, "output": [],
                                                                                   "status": "in_progress"}}),
                    ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                                    "item": {**item, "arguments": ""}}),
                    ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                                                                "item_id": "fc_1", "output_index": 0,
                                                                "delta": item["arguments"]}),
                    ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                                                   "item": item}),
                    ("response.completed", {"type": "response.completed", "response": response})])
    use = {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "ls"}}
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": [use],
               "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 3}}
    if not stream:
        return json.dumps(message).encode()
    return sse([("message_start", {"type": "message_start", "message": {**message, "content": [],
                                                                        "stop_reason": None}}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {**use, "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                   "usage": {"output_tokens": 3}}),
                ("message_stop", {"type": "message_stop"})])


def read_answer(api, data):
    answer = rt.LlmAnswer(api)
    answer.feed(data)
    return answer.result()


STOP = "[sandbox-ai loop guard] the same call 6 times; sandbox-ai ended this turn"


@pytest.mark.parametrize("api", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_the_relay_answers_in_the_models_place(api, stream):
    made = rt.llm_stop_answer(api, "qwen/qwen3-coder-next", STOP, stream)
    result = read_answer(api, made)
    assert result["text"] == STOP and not result.get("tool_calls") and result["streamed"] == stream
    assert result["finish_reason"] in ("stop", "end_turn", "completed") and result["model"] == "qwen/qwen3-coder-next"
    if stream:  # every event a complete server-sent event: what the agents' SSE parsers take
        assert made.endswith(b"\n\n") and all(part.startswith((b"event: ", b"data: "))
                                              for part in made.split(b"\n\n") if part)
    # The model's answer calls a tool again: replaced; a text answer passes as it came.
    replaced = rt.llm_held_answer(api, tool_answer(api, stream), None, stream, "m", STOP)
    assert replaced is not None and read_answer(api, replaced)["text"] == STOP
    assert rt.llm_held_answer(api, rt.llm_stop_answer(api, "m", "Summary: stuck.", stream), None, stream, "m",
                              STOP) is None


# --- end to end: the real proxy between a client and a scripted model that always calls a tool ------------------------

class Scripted(http.server.BaseHTTPRequestHandler):
    """The model: every POST answered in the shape of its path (/v1/chat/completions, /v1/responses, /v1/messages),
    streamed when the request says so (chunked, like the servers), with a tool call (MODE "tool") or a text."""
    protocol_version = "HTTP/1.1"
    mode, bodies = "tool", []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Scripted.bodies.append(body)
        api = "chat" if self.path.endswith("/chat/completions") else "responses" if \
            self.path.endswith("/responses") else "messages"
        stream = bool(body.get("stream"))
        data = tool_answer(api, stream) if Scripted.mode == "tool" else \
            rt.llm_stop_answer(api, "m", "Summary: the tests do not run.", stream)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        if stream:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in data.split(b"\n\n"):
                if part:
                    piece = part + b"\n\n"
                    self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


class Relay:
    """The real ProxyServer with the --llm entry of a stored endpoint, its firewall redirect replaced by a fixed
    destination: the scripted model."""

    def __init__(self, tmp_path, port):
        run_dir = tmp_path / "run"
        rt.create_ca(run_dir, tmp_path / "ca", bundles=())
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        llm.main(["add", "mac", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port}", "--model", "m"],
                 out=lambda line: None)
        a = argparse.Namespace(llm="mac", agent="opencode", llm_param=[])
        a.llm_addresses = ["127.0.0.1"]
        config = {"body_limit": 65536, "allow": {"loopback": [port]}, "llm": llm.proxy_entry(a)}
        self.server = rt.ProxyServer(config, self.fd, None, run_dir=run_dir,
                                     destination=lambda sock: ("127.0.0.1", port))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def post(self, path, document, close=True):
        body = json.dumps(document).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=20) as sock:
            sock.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json"
                         b"\r\nContent-Length: %d\r\n" % len(body) + (b"Connection: close\r\n" if close else b"") +
                         b"\r\n" + body)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
                if not close and has_whole_answer(data):
                    break
        return data

    def records(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = [json.loads(line) for line in self.log.read_text().splitlines()]
            if len(found) >= count:
                return found
            time.sleep(0.05)
        return found

    def close(self):
        self.listener.close()
        os.close(self.fd)


def has_whole_answer(data):
    head, _, body = data.partition(b"\r\n\r\n")
    length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length:")), None)
    return (length is not None and len(body) >= length) or body.endswith(b"0\r\n\r\n")


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


PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "messages": "/v1/messages"}


@pytest.fixture
def scripted():
    Scripted.mode, Scripted.bodies = "tool", []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("api", ["chat", "responses", "messages"])
def test_e2e_a_repeated_call_after_the_note_ends_the_turn(tmp_path, scripted, api):
    """runs/loopfix_*: the 6th identical call - the note goes to the model with the tools as they were; the model calls
    the tool again; the agent gets the relay's text answer in its API's shape (streamed and not) and its turn ends."""
    relay = Relay(tmp_path, scripted.server_address[1])
    try:
        for number, stream in enumerate((False, True), 1):
            document = {**LOOPS[api](6), "model": "m", "stream": stream}
            head, body = dechunk(relay.post(PATHS[api], document))
            assert b" 200 " in head.split(b"\r\n", 1)[0]
            sent = Scripted.bodies[-1]
            assert "tool_choice" not in sent and sent["tools"] == document["tools"]  # no re-prefill without tools
            assert rt.LLM_NOTE in json.dumps(sent)
            result = read_answer(api, body)
            assert not result.get("tool_calls") and result["text"].startswith(rt.LLM_NOTE)
            assert "6 times" in result["text"] and "ended" in result["text"]
            assert (b"text/event-stream" in head.lower()) == stream and result["streamed"] == stream
            loop = relay.records(number)[-1]["llm_loop"]
            assert loop["action"] == "paused tools" and loop["ended"]
            record = safeguards.from_connection(relay.records(number)[-1], "run-1", safeguards.Redactor())
            assert record["kind"] == "loop" and "ended the turn" in record["what"]
        # The model stops by itself (a text answer): it passes as it came.
        Scripted.mode = "text"
        head, body = dechunk(relay.post(PATHS[api], {**LOOPS[api](6), "model": "m", "stream": True}))
        assert read_answer(api, body)["text"] == "Summary: the tests do not run."
        assert "ended" not in relay.records(3)[-1]["llm_loop"]
        # A note only (3 times): the tool call passes.
        Scripted.mode = "tool"
        head, body = dechunk(relay.post(PATHS[api], {**LOOPS[api](3), "model": "m"}))
        assert read_answer(api, body)["tool_calls"]
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_connection_stays_usable_after_a_replaced_answer(tmp_path, scripted):
    """Keep-alive: the replaced answer has its own length; the next request on the same connection works."""
    relay = Relay(tmp_path, scripted.server_address[1])
    try:
        body = json.dumps({**chat_loop(6), "stream": True}).encode()
        request = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                   b"Content-Length: %d\r\n\r\n" % len(body) + body)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=20) as sock:
            sock.sendall(request)
            data = b""
            while not has_whole_answer(data):
                data += sock.recv(65536)
            assert rt.LLM_NOTE.encode() in data
            sock.sendall(request.replace(b"Content-Type", b"Connection: close\r\nContent-Type"))
            rest = b""
            while chunk := sock.recv(65536):
                rest += chunk
        assert b" 200 " in rest.split(b"\r\n", 1)[0] and rt.LLM_NOTE.encode() in rest
    finally:
        relay.close()
