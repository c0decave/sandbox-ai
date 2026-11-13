"""The loop guard and the sampling parameters of an --llm server: the relay's parts (runtime.llm_*: which fields
reach which server and API, the tool-call loop detector with its note and its pause, the text-loop guard on a
streamed answer and its clean ending, the watchdog, the boost after a cut), their false positives (polling, a
read-edit-test cycle, long legitimate output), the layers' settings (llm.py: defaults, defaults.json, a server's, a
start's), keeping the other models loaded (LM Studio), and end to end: the real proxy between a client and the mock
model server (tests/devtools/model_mock.py)."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_loops", "sandbox/runtime.py")
llm = load("sandbox_llm_loops", "sandbox/llm.py")
safeguards = load("sandbox_safeguards_loops", "sandbox/safeguards.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
import model_mock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    return status, "\n".join(lines)


def entry(kind="llamacpp", **changes):
    """A proxy entry as llm.proxy_params makes it."""
    found = {"name": "gpu", "kind": kind, "params": {}, "forced": {}, "override": {}, "presets": {},
             "sent": {api: list(keys) for api, keys in llm.SENT[kind].items()}, "loop": dict(llm.LOOP_DEFAULTS)}
    found.update(changes)
    return found


# --- which field reaches which server -------------------------------------------------------------------------------

def test_the_api_of_a_path():
    assert rt.llm_api("/v1/chat/completions") == "chat" and rt.llm_api("/gw/v1/messages?beta=true") == "messages"
    assert rt.llm_api("/v1/responses") == "responses" and rt.llm_api("/v1/completions") == "completions"
    assert rt.llm_api("/v1/models") is None and rt.llm_api("/api/chat") is None


def test_parameters_fill_force_and_cap():
    found = entry(params={"*": {"temperature": 0.7, "top_k": 40, "max_tokens": 4096, "num_ctx": 32768},
                          "qwen": {"top_k": 20}}, forced={"*": ["top_k"]})
    document = {"model": "other", "temperature": 0.2, "max_tokens": 16000}
    applied = rt.llm_apply_params(document, found, "chat")
    # The agent's own temperature stays (a default), top_k forced, max_tokens lowered to the ceiling, num_ctx never.
    assert document == {"model": "other", "temperature": 0.2, "top_k": 40, "max_tokens": 4096}
    assert applied == {"top_k": 40, "max_tokens": 4096}
    document = {"model": "qwen", "max_tokens": 100}
    rt.llm_apply_params(document, found, "chat")
    # The model's own value replaces the forced one of every model, and is a default itself; a lower max stays.
    assert document == {"model": "qwen", "temperature": 0.7, "top_k": 20, "max_tokens": 100}
    document = {"model": "qwen", "max_output_tokens": 9000}
    rt.llm_apply_params(document, found, "responses")
    assert document["max_output_tokens"] == 4096 and document["top_k"] == 20  # without "nested": top level
    # llama.cpp's own fields beside a foreign API go into "extra_params" (its server README), the API's own stay.
    nested = {**found, "nested": {api: list(keys) for api, keys in llm.NESTED["llamacpp"].items()},
              "params": {"*": {"temperature": 0.7, "top_k": 40, "repeat_penalty": 1.1, "seed": 3}}}
    document = {"model": "m", "max_tokens": 100, "extra_params": {"seed": 9}}
    rt.llm_apply_params(document, nested, "messages")
    assert document == {"model": "m", "max_tokens": 100, "temperature": 0.7, "top_k": 40,
                        "extra_params": {"seed": 9, "repeat_penalty": 1.1}}
    document = {"model": "m"}
    rt.llm_apply_params(document, nested, "responses")
    assert document["extra_params"] == {"top_k": 40, "repeat_penalty": 1.1, "seed": 3} and "top_k" not in document
    document = {"model": "m"}
    rt.llm_apply_params(document, nested, "chat")
    assert document["repeat_penalty"] == 1.1 and "extra_params" not in document  # chat: top level
    document = {"model": "qwen", "max_output_tokens": 9000}
    rt.llm_apply_params(document, {**found, "sent": {api: list(keys) for api, keys in llm.SENT["vllm"].items()}},
                        "responses")
    assert document["max_output_tokens"] == 4096 and "top_k" not in document  # vLLM's Responses API: the standard
    # The start's override wins and is forced.
    document = {"model": "qwen", "temperature": 0.1}
    rt.llm_apply_params(document, {**found, "override": {"temperature": 1.0}}, "chat")
    assert document["temperature"] == 1.0


def test_parameters_reach_only_the_fields_the_kind_takes():
    found = entry("ollama", params={"*": {"temperature": 0.5, "top_k": 30, "repeat_penalty": 1.1}})
    chat, messages = {"model": "m"}, {"model": "m", "max_tokens": 1000}
    rt.llm_apply_params(chat, found, "chat")
    rt.llm_apply_params(messages, found, "messages")
    # Ollama's OpenAI-compatible API takes neither top_k nor repeat_penalty; its Anthropic one takes top_k.
    assert chat == {"model": "m", "temperature": 0.5}
    assert messages == {"model": "m", "max_tokens": 1000, "temperature": 0.5, "top_k": 30}


def test_a_family_that_warns_against_greedy_decoding_is_never_greedy():
    found = entry("vllm", presets={"qwen3-32b": llm.family_preset("qwen3-32b")})
    document = {"model": "qwen3-32b", "temperature": 0}
    rt.llm_apply_params(document, found, "chat")
    assert document["temperature"] == 0.6 and document["top_k"] == 20 and document["top_p"] == 0.95
    other = {"model": "llama-3.1-8b", "temperature": 0}
    rt.llm_apply_params(other, found, "chat")
    assert other == {"model": "llama-3.1-8b", "temperature": 0}  # no preset: as the agent asked
    assert llm.family_preset("Qwen3-Coder-30B-A3B")["repetition_penalty"] == 1.05
    assert llm.family_preset("qwen3-30b-a3b-instruct-2507")["temperature"] == 0.7
    assert llm.family_preset("gpt-oss-20b") == {"temperature": 1.0, "top_p": 1.0}
    assert llm.family_preset("mistral-small") == {}


# --- the text guard: a block repeated, and what is no loop ------------------------------------------------------------

def test_a_repeated_block_is_found():
    looping = "Intro text. " + "I will now check the file again to be sure it is right. " * 120
    assert rt.repeated_block(looping, 10) == len("I will now check the file again to be sure it is right. ")
    thinking = "Okay. " + "Wait, maybe the bug is in parse(). Let me re-read parse() carefully.\n" * 80
    assert rt.repeated_block(thinking, 10)


@pytest.mark.parametrize("text", [
    "=" * 20000,                                                         # a separator
    "0, " * 6000,                                                        # an array of zeros
    "".join(f"    assert value[{i}] == {i * 7}\n" for i in range(800)),  # similar, never equal lines
    "".join(f"| {i:4} | row {i % 7} | ok |\n" for i in range(600)),      # a table
    (ROOT / "sandbox/llm.py").read_text()[:30000],                      # real code
    "the the " * 4000,                                                   # a short run: max_tokens' matter
])
def test_what_is_no_loop(text):
    assert rt.repeated_block(text, 10) is None


def sse(*events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_the_stream_guard_ends_a_chat_answer_cleanly():
    guard = rt.LlmStreamGuard("chat", 10)
    block = "The same paragraph comes again and again, word for word. "
    tripped = None
    for _ in range(200):
        tripped = guard.feed(sse({"id": "c1", "model": "m", "created": 1, "choices": [{"index": 0, "delta": {
            "content": block}}]}))
        if tripped:
            break
    assert tripped and "repeated one block" in tripped
    ending = guard.ending("[note]").decode()
    events = [line[6:] for line in ending.split("\n") if line.startswith("data: ")]
    last = json.loads(events[0])
    assert last["id"] == "c1" and last["choices"][0]["finish_reason"] == "stop" and \
        last["choices"][0]["delta"]["content"] == "[note]" and events[-1] == "[DONE]"


def test_the_stream_guard_ends_anthropic_and_responses_answers_in_their_own_events():
    guard = rt.LlmStreamGuard("messages", 10)
    guard.feed(b"event: message_start\n" + sse({"type": "message_start", "message": {"id": "msg1", "model": "m"}}))
    guard.feed(sse({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}))
    block = "Let me think about this once more from the very beginning. "
    while not guard.feed(sse({"type": "content_block_delta", "index": 0,
                              "delta": {"type": "thinking_delta", "thinking": block}})):
        pass
    kinds = [json.loads(line[6:])["type"] for line in guard.ending("[note]").decode().split("\n")
             if line.startswith("data: ")]
    assert kinds == ["content_block_stop", "content_block_start", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    guard = rt.LlmStreamGuard("responses", 10)
    guard.feed(sse({"type": "response.created", "response": {"id": "resp1", "model": "m"}}))
    while not guard.feed(sse({"type": "response.output_text.delta", "item_id": "i1", "delta": block})):
        pass
    events = [json.loads(line[6:]) for line in guard.ending("[note]").decode().split("\n") if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["response.output_item.done", "response.completed"]
    assert events[1]["response"]["id"] == "resp1" and events[1]["response"]["status"] == "completed"


def test_the_stream_guard_lets_a_long_varied_answer_pass():
    guard = rt.LlmStreamGuard("chat", 10)
    text = (ROOT / "sandbox/runtime.py").read_text()[:60000]
    for start in range(0, len(text), 200):
        assert guard.feed(sse({"choices": [{"index": 0, "delta": {"content": text[start:start + 200]}}]})) is None


# --- the tool-call loop: detected from the conversation in each request ---------------------------------------------

def chat_history(calls, human_after=None):
    """An OpenAI chat conversation: a human message, then (name, arguments, result) calls with their results."""
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "fix the bug"}]
    for index, (name, arguments, result) in enumerate(calls):
        if human_after is not None and index == human_after:
            messages.append({"role": "user", "content": "go on"})
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": result})
    return {"model": "m", "messages": messages, "tools": [{"type": "function", "function": {"name": "bash"}}]}


def check(document, api="chat", **loop):
    return rt.llm_loop_check(rt.llm_tool_history(document, api), {**llm.LOOP_DEFAULTS, **loop})


def test_identical_calls_with_identical_results_warn_then_block():
    same = ("bash", {"command": "pytest -x"}, "1 failed in 0.53s")
    assert check(chat_history([same] * 2))[0] is None
    assert check(chat_history([same] * 3))[:3] == ("warn", "bash", 3)
    assert check(chat_history([same] * 6))[:3] == ("block", "bash", 6)
    # Volatile parts (durations, times) do not make a result new.
    varied = [("bash", {"command": "pytest -x"}, f"1 failed in 0.{i}3s at 12:00:0{i}") for i in range(6)]
    assert check(chat_history(varied))[0] == "block"


def test_an_alternating_loop_and_a_failing_command_between_others():
    a, b = ("grep", {"pattern": "foo"}, "src/x.py:3: foo"), ("read", {"path": "src/x.py"}, "def foo(): ...")
    assert check(chat_history([a, b] * 3))[0] == "warn"
    failing = ("bash", {"command": "make"}, "error: missing ; exit 2")
    others = [("read", {"path": f"f{i}.c"}, f"content {i}") for i in range(6)]
    mixed = [x for pair in zip(others, [failing] * 6) for x in pair]
    assert check(chat_history(mixed))[0] == "block"


def test_no_false_positives():
    # read -> edit -> test -> read with a changed file: every result is new.
    cycle = []
    for i in range(6):
        cycle += [("read", {"path": "a.py"}, f"version {i}"), ("edit", {"path": "a.py", "n": i}, "ok"),
                  ("bash", {"command": "pytest"}, f"{5 - i} failed")]
    level = check(chat_history(cycle))
    assert level[0] in (None, "warn") and level[0] != "block"  # "ok" of different edits: different arguments
    assert check(chat_history(cycle))[0] is None
    # Polling: the same wait with the same answer counts against three times the thresholds.
    poll = ("bash", {"command": "sleep 30; gh run view 42"}, "in progress")
    assert check(chat_history([poll] * 8))[0] is None and check(chat_history([poll] * 9))[0] == "warn"
    assert check(chat_history([poll] * 18))[0] == "block"
    # A new message of the human starts the count again.
    same = ("bash", {"command": "ls"}, "a b")
    assert check(chat_history([same] * 8, human_after=6))[0] is None
    # Off: nothing.
    assert check(chat_history([same] * 8), warn=0, block=0)[0] is None


def test_call_ids_that_repeat_across_answers_keep_their_own_results():
    """Regression: some servers number tool calls per answer (call_0 in every one); each result belongs to the latest
    call of its id, so six different results of one call are no loop."""
    messages = [{"role": "user", "content": "go"}]
    for i in range(6):
        messages += [{"role": "assistant", "tool_calls": [{"id": "call_0", "function": {
            "name": "read", "arguments": '{"path": "a.py"}'}}]}, {"role": "tool", "tool_call_id": "call_0",
                                                                    "content": f"version {i}"}]
    assert check({"messages": messages})[0] is None
    for message in messages[2::2]:
        message["content"] = "the same"
    assert check({"messages": messages})[0] == "block"


def test_the_history_of_anthropic_and_responses_requests():
    use = {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}}
    messages = [{"role": "user", "content": "go"}]
    for i in range(3):
        messages.append({"role": "assistant", "content": [{**use, "id": f"t{i}"}]})
        # Claude Code puts a reminder beside a tool result: that is no message of the human.
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "a"},
                                                     {"type": "text", "text": "<system-reminder>x</system-reminder>"}]})
    assert check({"messages": messages}, "messages")[:3] == ("warn", "Bash", 3)
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
    for i in range(6):
        items += [{"type": "function_call", "call_id": f"f{i}", "name": "shell", "arguments": '{"cmd": ["ls"]}'},
                  {"type": "function_call_output", "call_id": f"f{i}", "output": "a"}]
    assert check({"input": items}, "responses")[0] == "block"


def test_the_note_and_the_pause_go_into_the_request():
    same = ("bash", {"command": "pytest -x"}, "1 failed")
    document = chat_history([same] * 3)
    done = rt.llm_request(entry(), "chat", document)
    assert done["loop"]["action"] == "note" and "[sandbox-ai loop guard]" in document["messages"][-1]["content"]
    assert "tool_choice" not in document  # a note only: the model may still call tools
    document = chat_history([same] * 6)
    done = rt.llm_request(entry(), "chat", document)
    assert done["loop"]["action"] == "paused tools" and document["tool_choice"] == "none" and document["tools"]
    document = chat_history([same] * 6)
    rt.llm_request(entry("ollama"), "chat", document)
    assert "tools" not in document  # Ollama takes no tool_choice: its tools are left out for that answer
    messages = {"messages": [{"role": "user", "content": "go"}] + [
        m for i in range(6) for m in ({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"c": 1}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": [
                {"type": "text", "text": "same"}]}]})]}
    rt.llm_request(entry("vllm"), "messages", messages)
    assert messages["tool_choice"] == {"type": "none"}
    assert messages["messages"][-1]["content"][0]["content"][-1]["text"].startswith("[sandbox-ai loop guard]")
    # The guard off: the request stays as it came.
    document = chat_history([same] * 6)
    before = json.dumps(document)
    rt.llm_request(entry(loop={**llm.LOOP_DEFAULTS, "enabled": False}), "chat", document)
    assert json.dumps(document) == before


def test_the_watchdog_and_the_boost():
    turns = rt.LlmTurns()
    found = entry(loop={**llm.LOOP_DEFAULTS, "watchdog_requests": 5})
    for count in range(1, 7):
        document = chat_history([("read", {"path": f"f{count}"}, str(count))])
        done = rt.llm_request(found, "chat", document, turns, now=float(count))
    assert done["loop"]["kind"] == "watchdog" and document["tool_choice"] == "none"
    other = chat_history([("read", {"path": "x"}, "y")], human_after=0)  # a new turn
    assert rt.llm_request(found, "chat", other, turns, now=7.0)["loop"] is None
    minutes = entry(loop={**llm.LOOP_DEFAULTS, "watchdog_minutes": 1})
    turns = rt.LlmTurns()
    rt.llm_request(minutes, "chat", chat_history([("a", {}, "1")]), turns, now=0.0)
    assert rt.llm_request(minutes, "chat", chat_history([("a", {}, "1")]), turns, now=61.0)["loop"]["kind"] == \
        "watchdog"
    # After a cut answer the next request gets stronger anti-repetition sampling, once.
    turns.mark_cut("gpu")
    document = {"model": "m", "messages": [{"role": "user", "content": "go"}]}
    done = rt.llm_request(entry(), "chat", document, turns)
    assert document["presence_penalty"] == 0.5 and document["repeat_penalty"] == 1.15 and done["boost"]
    again = {"model": "m", "messages": [{"role": "user", "content": "go"}]}
    rt.llm_request(entry(), "chat", again, turns)
    assert "presence_penalty" not in again


def test_the_body_is_rewritten_with_a_length_and_odd_bodies_pass():
    found = entry(params={"*": {"temperature": 0.3}})
    body = json.dumps({"model": "m", "messages": []}).encode()
    head = b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
    headers = [("Host", "x"), ("Transfer-Encoding", "chunked"), ("Content-Type", "application/json")]
    new_head, parts, done = rt.llm_body(head, iter([(b"%x\r\n" % len(body), b""), (body, body), (b"\r\n", b""),
                                                    (b"0\r\n\r\n", b"")]), headers, found, "chat")
    sent = b"".join(raw for raw, _ in parts)
    assert json.loads(sent)["temperature"] == 0.3 and done["params"] == {"temperature": 0.3}
    assert b"Content-Length: %d\r\n" % len(sent) in new_head and b"chunked" not in new_head.lower()
    for type_, encoding, data in (("application/json", "gzip", body), ("text/plain", "identity", body),
                                  ("application/json", "identity", b"{not json")):
        hdrs = [("Content-Type", type_), ("Content-Encoding", encoding)]
        same_head, parts, done = rt.llm_body(head, iter([(data, data)]), hdrs, found, "chat")
        assert same_head == head and b"".join(raw for raw, _ in parts) == data and done.get("skipped")


# --- the settings: parameters and layers -----------------------------------------------------------------------------

def test_llm_set_add_and_params(capsys):
    assert run("add", "box", "--kind", "llamacpp", "--url", "http://127.0.0.1:8080", "--param", "temperature=0.7")[0] == 0
    status, text = run("set", "box", "--model", "qwen3-32b", "--param", "top_k=40", "--force-param",
                       "repeat_penalty=1.1", "--param", "num_ctx=32768", "--loop-guard", "warn=4",
                       "--loop-guard", "steps=off")
    assert status == 0 and "repeat_penalty=1.1 (forced)" in text and "warn=4" in text
    found = llm.endpoint("box")
    assert found["params"] == {"*": {"temperature": 0.7}, "qwen3-32b": {"top_k": 40, "repeat_penalty": 1.1,
                                                                        "num_ctx": 32768}}
    assert found["forced"] == {"qwen3-32b": ["repeat_penalty"]} and found["loop_guard"] == {"warn": 4, "steps": 0}
    assert run("set", "box", "--model", "qwen3-32b", "--unset", "top_k")[0] == 0
    assert "top_k" not in llm.endpoint("box")["params"]["qwen3-32b"]
    assert run("set", "box", "--model", "qwen3-32b", "--reset", "--param", "seed=7")[0] == 0
    assert llm.endpoint("box")["params"]["qwen3-32b"] == {"seed": 7} and "forced" not in \
        llm.record_of(llm.endpoint("box"))
    # Refused: unknown, not taken by the kind, out of range, the wrong type, an unknown layer.
    for argv, match in ((["--param", "temprature=1"], "unknown parameter"), (["--param", "repetition_penalty=1.1"],
                        "does not take it"), (["--param", "temperature=3"], "from 0.0 to 2.0"),
                        (["--param", "top_k=many"], "whole number"), (["--loop-guard", "speed=3"], "unknown layer"),
                        (["--loop-guard", "warn=1"], "0 \\(off\\) or 2-100"), ([], "nothing to change")):
        status, _ = run("set", "box", *argv)
        assert status == 1 and __import__("re").search(match, capsys.readouterr().err)
    # --replace keeps them (a front end cannot send them again); another kind drops what it does not take.
    assert run("add", "box", "--kind", "vllm", "--url", "http://127.0.0.1:8080", "--replace")[0] == 0
    kept = llm.endpoint("box")
    assert kept["params"] == {"*": {"temperature": 0.7}, "qwen3-32b": {"seed": 7}} and kept["loop_guard"]["warn"] == 4
    shown = json.loads(run("show", "box", "--json")[1])
    assert shown["loop_guard"]["warn"] == 4 and shown["loop_guard_own"] == {"warn": 4, "steps": 0}
    assert shown["evict"] == "never" and shown["params"]["qwen3-32b"] == {"seed": 7}
    catalogue = json.loads(run("params", "--kind", "ollama", "--json")[1])
    names = {entry["name"]: entry for entry in catalogue}
    assert "repetition_penalty" not in names and names["top_k"]["sent"]["ollama"] == ["messages"]
    assert names["num_ctx"]["never_sent"] and names["max_tokens"]["ceiling"]


def test_the_layers_over_each_other(tmp_path):
    defaults = tmp_path / "cfg/sandbox-ai/defaults.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(json.dumps({"version": 1, "loop_guard": {"warn": 5, "instructions": False}}))
    run("add", "box", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--loop-guard", "warn=7")
    found = llm.endpoint("box")
    assert llm.loop_config(found)["warn"] == 7 and llm.loop_config(found)["instructions"] is False
    a = argparse.Namespace(llm="box:qwen3-8b", llm_param=["loop_guard.warn=9", "temperature=0.4"], agent="opencode")
    values, start = llm.start_params(a, found)
    assert values == {"temperature": 0.4} and llm.loop_config(found, start)["warn"] == 9
    off = llm.loop_config(found, llm.start_params(argparse.Namespace(llm_param=["loop_guard=off"]), found)[1])
    assert not any(off[key] for key in llm.LOOP_DEFAULTS)
    entry_ = llm.proxy_params(a, found, "qwen3-8b")
    assert entry_["presets"]["qwen3-8b"]["top_k"] == 20 and entry_["override"] == {"temperature": 0.4}
    a.llm_param = ["loop_guard.sampling=off"]
    assert llm.proxy_params(a, found, "qwen3-8b")["presets"] == {}
    with pytest.raises(ValueError, match="unknown layer"):
        llm.check_loop({"speed": 1}, "x")


def test_opencode_with_dangerously_skip_permissions_keeps_its_doom_loop_question():
    run("add", "box", "--kind", "ollama", "--url", "http://127.0.0.1:11434", "--model", "qwen3:8b")
    a = argparse.Namespace(llm="box", agent="opencode", dangerously_skip_permissions=True, env=[], llm_param=[])
    assert json.loads(llm.agent_environment(a)["OPENCODE_PERMISSION"]) == {"*": "allow", "doom_loop": "ask"}
    a.llm_param = ["loop_guard=off"]
    assert "OPENCODE_PERMISSION" not in llm.agent_environment(a)
    a.llm_param, a.env = [], ["OPENCODE_PERMISSION"]
    assert "OPENCODE_PERMISSION" not in llm.agent_environment(a)  # yours wins


def test_the_context_the_agent_is_told_and_the_warning(capsys):
    run("add", "box", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234")
    found = llm.endpoint("box")
    assert llm.agent_context(found, "m", reported=131072, loaded=8192) == 8192  # what is loaded, not the maximum
    assert capsys.readouterr().err == ""
    run("set", "box", "--model", "m", "--param", "num_ctx=32768")
    assert llm.agent_context(llm.endpoint("box"), "m", reported=131072, loaded=8192) == 32768
    assert "told a context window of 32768" in capsys.readouterr().err
    run("set", "box", "--loop-guard", "context=off")
    llm.agent_context(llm.endpoint("box"), "m", reported=131072, loaded=8192)
    assert capsys.readouterr().err == ""


# --- keeping the other models loaded --------------------------------------------------------------------------------

@pytest.fixture
def lmstudio(tmp_path, monkeypatch):
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=("qwen3-8b", "gemma-3-12b"), lmstudio=True, log=str(log),
                                    log_bodies=True, loaded={"gemma-3-12b": 8192})
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    run("add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")
    yield port, log, server
    server.shutdown()


def requests_of(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_listing_status_and_check_never_load_a_model(lmstudio):
    """Regression (two sandboxes unloading each other's model on LM Studio): sandbox-ai's own reads only list."""
    port, log, _ = lmstudio
    assert run("models", "studio")[0] == 0
    run("status", "studio")
    run("check", "studio", "--agent", "opencode", "--model", "qwen3-8b")
    found = llm.endpoint("studio")
    llm.context_window(found, "qwen3-8b")
    llm.loaded_context(found, "qwen3-8b")
    posts = [r for r in requests_of(log) if r["method"] == "POST"]
    # Only the API probes, which name no model (an empty body: nothing to load), and no load.
    assert all(r["body"] == {} for r in posts) and not any(r["path"].endswith("/load") for r in posts)


def test_a_start_loads_its_model_explicitly_unless_evict_allow(lmstudio):
    port, log, server = lmstudio
    run("set", "studio", "--model", "qwen3-8b", "--param", "num_ctx=16384")
    note = llm.keep_loaded(llm.endpoint("studio"), "qwen3-8b")
    loads = [r for r in requests_of(log) if r["path"] == "/api/v1/models/load"]
    assert "loaded qwen3-8b" in note and loads[-1]["body"] == {"model": "qwen3-8b", "context_length": 16384}
    assert llm.loaded_context(llm.endpoint("studio"), "qwen3-8b") == 16384
    assert llm.keep_loaded(llm.endpoint("studio"), "gemma-3-12b") is None  # loaded already: left as it is
    run("set", "studio", "--evict", "allow")
    assert llm.keep_loaded(llm.endpoint("studio"), "gemma-3-12b") is None
    assert len([r for r in requests_of(log) if r["path"] == "/api/v1/models/load"]) == 1
    assert json.loads(run("show", "studio", "--json")[1])["evict"] == "allow"
    assert any("Auto-Evict" in line for line in llm.evict_notes(llm.endpoint("studio")))


# --- end to end: the real proxy between a client and the mock model server ------------------------------------------

pytestmark_e2e = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


class Relay:
    """The real ProxyServer with the --llm entry of a stored endpoint (llm.proxy_entry), its firewall redirect
    replaced by a fixed destination: the mock server."""

    def __init__(self, tmp_path, port, a):
        run_dir = tmp_path / "run"
        rt.create_ca(run_dir, tmp_path / "ca", bundles=())
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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
            sock.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                         + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return data

    def records(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            found = [json.loads(line) for line in self.log.read_text().splitlines()]
            if found:
                return found
            time.sleep(0.05)
        return []

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
def test_end_to_end_through_the_relay(tmp_path):
    log = tmp_path / "mock.jsonl"
    block = "I am not sure, so I will check the configuration file once more. "
    server, port = model_mock.start(models=("qwen3-8b",), log=str(log), log_bodies=True)
    try:
        run("add", "box", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port}", "--model", "qwen3-8b",
            "--param", "max_tokens=2048")
        run("set", "box", "--model", "qwen3-8b", "--force-param", "repeat_penalty=1.1")
        a = argparse.Namespace(llm="box", agent="opencode", llm_param=["seed=7"])
        relay = Relay(tmp_path, port, a)
        # 1. The parameters reach the server: the family's preset, the endpoint's, the start's.
        answer = relay.post("/v1/chat/completions", {"model": "qwen3-8b", "temperature": 0, "max_tokens": 32000,
                                                    "repeat_penalty": 1.0,
                                                    "messages": [{"role": "user", "content": "hi"}]})
        assert b" 200 " in answer.split(b"\r\n", 1)[0] and b"mock reply to: hi" in answer
        sent = requests_of(log)[-1]["body"]
        assert sent["temperature"] == 0.6 and sent["top_k"] == 20 and sent["max_tokens"] == 2048
        assert sent["repeat_penalty"] == 1.1 and sent["seed"] == 7
        # 2. A looping tool history: the note goes to the server, the answer comes back to the agent as usual.
        same = ("bash", {"command": "pytest -x"}, "1 failed")
        answer = relay.post("/v1/chat/completions", chat_history([same] * 3) | {"model": "qwen3-8b"})
        assert b"mock reply" in answer
        sent = requests_of(log)[-1]["body"]
        assert "[sandbox-ai loop guard]" in sent["messages"][-1]["content"] and "tool_choice" not in sent
        relay.post("/v1/chat/completions", chat_history([same] * 6) | {"model": "qwen3-8b"})
        assert requests_of(log)[-1]["body"]["tool_choice"] == "none"
        relay.close()
        # 3. A repeating stream is ended cleanly; the next request works and gets the boost.
        server.shutdown()
        server, port = model_mock.start(models=("qwen3-8b",), log=str(log), log_bodies=True, reply=block * 150,
                                        chunks=300)
        run("add", "box", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port}", "--model", "qwen3-8b",
            "--replace")
        (tmp_path / "second").mkdir()
        relay = Relay(tmp_path / "second", port, argparse.Namespace(llm="box", agent="opencode", llm_param=["seed=7"]))
        head, body = dechunk(relay.post("/v1/chat/completions", {"model": "qwen3-8b", "stream": True,
                                                                "messages": [{"role": "user", "content": "go"}]}))
        events = [line[6:] for line in body.decode().split("\n") if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        last = json.loads(events[-2])
        assert last["choices"][0]["finish_reason"] == "stop" and "[sandbox-ai loop guard]" in \
            last["choices"][0]["delta"]["content"]
        content = "".join(json.loads(e)["choices"][0]["delta"].get("content") or "" for e in events[:-2])
        assert block * 10 in content and len(content) < len(block) * 100  # cut well before its end (150 blocks)
        records = relay.records()
        loop = [r for r in records if r.get("llm_loop")]
        assert loop and loop[0]["llm_loop"]["kind"] == "text"
        record = safeguards.from_connection(loop[0], "run-1", safeguards.Redactor())
        assert record["kind"] == "loop" and "repeated one block" in record["why"]
        relay.post("/v1/chat/completions", {"model": "qwen3-8b", "messages": [{"role": "user", "content": "go on"}]})
        assert requests_of(log)[-1]["body"]["presence_penalty"] == 0.5
        relay.close()
    finally:
        server.shutdown()


@pytestmark_e2e
def test_other_servers_traffic_is_never_changed(tmp_path):
    """The relay touches the --llm server's requests only: the same body to another port passes as it came."""
    log = tmp_path / "mock.jsonl"
    server, port = model_mock.start(models=("qwen3-8b",), log=str(log), log_bodies=True)
    try:
        run("add", "box", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port + 1}", "--model", "qwen3-8b",
            "--force-param", "temperature=1.5")
        relay = Relay(tmp_path, port, argparse.Namespace(llm="box", agent="opencode", llm_param=[]))
        same = ("bash", {"command": "ls"}, "a")
        document = chat_history([same] * 6) | {"model": "qwen3-8b", "temperature": 0}
        relay.post("/v1/chat/completions", document)
        assert requests_of(log)[-1]["body"] == document
        relay.close()
    finally:
        server.shutdown()
