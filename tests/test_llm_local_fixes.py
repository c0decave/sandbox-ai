"""The relay for local models: what the local-model research (phase 2) found with the real harness CLIs against real
models on LM Studio and verified through a standalone relay (/work/research-llm/phase2_fixes.md, phase2/tools/relay.py).
User, live: "a lot of mcp issues, web fetch failures, endless loops. and some took ages before they finished".

- G5 "took ages": the loop guard's note went into the result of the LATEST call of the repeated tool, in every
  request from the third repetition on. The note moved with every step, no request extended the one before it, and LM
  Studio read the whole conversation again each time (runs/t2_debug__codex-hf__qcn: 6.7 s at 7k tokens ... 29.5 s at
  27k, 1,084 of 1,200 s). Now the k-th identical call's result carries the note for k, in every later request too
  (runs/t2_debug__codex-hfsn__qcn: 0.34-0.46 s at every step).
- G6 "mcp issues": Codex 0.166 offers each MCP server as one {"type": "namespace"} tool; LM Studio's /v1/responses
  ignores those ("I don't have access to a write_file tool": probes/qcn/codex_ns.json). The relay offers them as
  plain functions NAMESPACE__TOOL and names the model's calls back (runs/smoke__codex-hfns__qcn: 4 MCP calls).
- G7 "endless loops": Codex refuses exec_command whenever the arguments hold `justification` without
  `sandbox_permissions`; a local model fills it at nearly every call (24 of 24 in runs/t2_debug__codex-hfsn__qcn).
  The relay does not offer the three approval parameters, and takes them out of a call that has them all the same.
- G8: a note in a tool result that is a LIST of parts was {"type": "text"}; the Responses API's parts are
  "input_text" (Codex sends MCP results that way) and LM Studio refused the request: HTTP 400 "Invalid type for
  'input'" (runs/t5_mcp__codex-tuned__qcn, request 31).

Unit tests of runtime.llm_*, and end to end through the real proxy (ProxyServer) with a scripted model server."""
import argparse
import copy
import http.server
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402 - its Relay (the real proxy) and answer readers

rt, llm = live.rt, live.llm


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def entry(kind="lmstudio", **changes):
    return live.entry(kind, **changes)


# --- the conversations: Codex (Responses API), chat completions, Anthropic Messages ----------------------------------

def codex_mcp_output(text):
    """An MCP tool's result as Codex 0.166 sends it back (runs/mock_codex_ns, request 2): a list of input_text parts."""
    return [{"type": "input_text", "text": "Wall time: 0.0343 seconds\nOutput:"}, {"type": "input_text", "text": text}]


def codex_steps(calls, namespace=None):
    """A Codex request's input: the human's message, then CALLS [(tool, arguments, output)] with their results."""
    items = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
             {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the report"}]}]
    for number, (name, arguments, output) in enumerate(calls):
        items += [{"type": "function_call", "status": "completed", "id": f"fc_{number}", "name": name,
                   **({"namespace": namespace} if namespace and name != "exec_command" else {}),
                   "arguments": json.dumps(arguments), "call_id": f"call_{number}"},
                  {"type": "function_call_output", "id": f"fco_{number}", "call_id": f"call_{number}",
                   "output": output}]
    return {"model": "qwen/qwen3-coder-next", "stream": True, "input": items, "tool_choice": "auto",
            "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {
                "cmd": {"type": "string"}}, "required": []}}]}


# --- G8: a note in a list of parts is a part of the list's own kind ---------------------------------------------------

def test_regression_a_note_in_codexs_mcp_result_is_an_input_text_part():
    """runs/t5_mcp__codex-tuned__qcn, request 31: the first request with a note on mcp__files__read_file - the
    result a list of input_text parts, the note a "text" part: HTTP 400 Invalid type for 'input' (invalid_union),
    and Codex ended the run with exit 1."""
    same = ("read_file", {"path": "logpipe/report.py"}, None)
    document = codex_steps([(name, arguments, codex_mcp_output('{"content": "import argparse"}'))
                            for name, arguments, _ in [same] * 3])
    done = rt.llm_request(entry(), "responses", document)
    assert done["loop"]["action"] == "note" and done["loop"]["tool"] == "read_file"
    parts = document["input"][-1]["output"]
    assert parts[-1]["text"].startswith(rt.LLM_NOTE)
    assert {part["type"] for part in parts} == {"input_text"}, "every part of a function_call_output is input_text"


@pytest.mark.parametrize("value,key,expected", [
    ([{"type": "input_text", "text": "a"}], "output", "input_text"),
    ([{"type": "text", "text": "a"}], "content", "text"),                      # Anthropic's tool_result, chat's parts
    ([{"type": "input_image", "image_url": "data:image/png;base64,AA=="}], "output", "input_text"),  # no text part
    ([], "output", "input_text"),
    ([{"type": "image", "source": {"type": "base64", "data": "AA=="}}], "content", "text"),
    ([{"type": "text", "text": "a"}, {"type": "output_text", "text": "b"}], "content", "output_text"),  # the last
])
def test_the_note_takes_the_kind_of_the_lists_last_text_part(value, key, expected):
    holder = {key: copy.deepcopy(value)}
    rt.llm_add_note((holder, key), "note")
    assert holder[key][:-1] == value and holder[key][-1] == {"type": expected, "text": "note"}


def test_a_note_in_a_string_and_in_a_list_of_strings_as_before():
    holder = {"content": "out"}
    rt.llm_add_note((holder, "content"), "note")
    assert holder["content"] == "out\n\nnote"
    holder = {"content": ["a", "b"]}
    rt.llm_add_note((holder, "content"), "note")
    assert holder["content"] == ["a", "b", "note"]


# --- G5: the notes keep their places: a later request extends the one before it ---------------------------------------

def conversation(api, calls, human_after=None):
    """A request of API after CALLS [(tool, arguments, result)], each with its result; HUMAN_AFTER {index: text}: a
    message of the human's before that call."""
    if api == "responses":
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the report"}]}]
    else:
        items = [{"role": "user", "content": "fix the report"}]
    if api == "chat":
        items.insert(0, {"role": "system", "content": "rules"})
    for number, (name, arguments, result) in enumerate([*calls, (None, None, None)]):
        if number in (human_after or {}):
            text = human_after[number]
            items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
                         if api == "responses" else {"role": "user", "content": text})
        if name is None:
            break  # (the end: only a message of the human's may stand there)
        if api == "responses":
            items += [{"type": "function_call", "call_id": f"call_{number}", "name": name,
                       "arguments": json.dumps(arguments)},
                      {"type": "function_call_output", "call_id": f"call_{number}", "output": result}]
        elif api == "messages":
            items += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_{number}", "name": name,
                                                         "input": arguments}]},
                      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{number}",
                                                    "content": [{"type": "text", "text": result}]}]}]
        else:
            items += [{"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{number}", "type": "function", "function": {"name": name,
                                                                         "arguments": json.dumps(arguments)}}]},
                {"role": "tool", "tool_call_id": f"call_{number}", "content": result}]
    return {"model": "qwen/qwen3-coder-next", "stream": True, ("input" if api == "responses" else "messages"): items,
            "tools": [{"type": "function", "name": "exec_command"}] if api == "responses" else
            [{"name": "exec_command", "input_schema": {"type": "object", "properties": {}, "required": []}}] if api == "messages" else
            [{"type": "function", "function": {"name": "exec_command"}}]}


def items_of(document):
    return document.get("input", document.get("messages"))


def results_of(api, document):
    """The text of each tool result of DOCUMENT, in order."""
    found = []
    for item in items_of(document):
        if api == "responses" and item.get("type") == "function_call_output":
            found.append(rt.llm_text(item["output"]))
        elif api == "chat" and item.get("role") == "tool":
            found.append(rt.llm_text(item["content"]))
        elif api == "messages" and isinstance(item.get("content"), list):
            found += [rt.llm_text(block["content"]) for block in item["content"] if block.get("type") == "tool_result"]
    return found


def noted(api, document):
    """{number of the tool result (from 1): its note} of a request as the relay sent it."""
    return {number: text[text.index(rt.LLM_NOTE):] for number, text in enumerate(results_of(api, document), 1)
            if rt.LLM_NOTE in text}


def extends(later, earlier):
    """LATER (a request as sent to the server) begins with every message of EARLIER, byte for byte."""
    first, second = items_of(earlier), items_of(later)
    dumped = [json.dumps(item, sort_keys=True) for item in first]
    return len(second) >= len(first) and [json.dumps(item, sort_keys=True) for item in second[:len(first)]] == dumped


PYTEST = ("exec_command", {"cmd": "cd work && python3 -m pytest -q 2>&1 | tail -3"},
          "Chunk ID: 8818fc\nWall time: 0.0512 seconds\nOutput:\n/usr/bin/python3: No module named pytest\n")


def debug_run(steps):
    """runs/t2_debug__codex-hf__qcn as the harness sent it: pytest three times with the same result (the guard's
    note from request 4 on), then another command at every step (cat, ls, grep ...)."""
    return [PYTEST] * 3 + [("exec_command", {"cmd": f"cat logpipe/part{number}.py"},
                            f"Chunk ID: {number:06x}\nOutput:\ndef part{number}(): ...\n") for number in range(steps - 3)]


@pytest.mark.parametrize("api", ["responses", "chat", "messages"])
def test_regression_the_note_stays_where_it_was_put_and_every_request_extends_the_last(api):
    """runs/t2_debug__codex-hf__qcn: the note sat in the result of the LATEST exec_command call of every request from
    the 4th on - request 9 no longer began like request 8, and from there LM Studio read the whole conversation again
    at every step (6.7 s at 7k tokens ... 29.5 s at 27k; 1,084 of the run's 1,200 s). With the note where the third
    identical call's result is, and nowhere else, the same run took 0.34-0.46 s per step (codex-hfsn)."""
    previous = None
    for steps in range(2, 14):
        document = conversation(api, debug_run(steps)[:steps])
        done = rt.llm_request(entry(), api, document)
        if steps < 3:
            assert done["loop"] is None and not noted(api, document)
        else:
            assert done["loop"] == {"action": "note", "kind": "repeat", "tool": "exec_command", "count": 3}
            assert list(noted(api, document)) == [3], "one note, in the third identical call's result"
            assert "3 times" in noted(api, document)[3]
        if previous is not None:
            assert extends(document, previous), f"request {steps} does not begin like the one before it"
        previous = document


@pytest.mark.parametrize("api", ["responses", "chat", "messages"])
def test_each_repetition_has_its_own_note_and_the_sixth_tells_the_model_to_stop(api):
    """The guard's steps as before - a note from the third identical call, the stop at the sixth, the answer held -
    with each note in its own call's result: the third's says 3 times in every request, the sixth's says stop."""
    previous = None
    for steps in range(1, 9):
        document = conversation(api, [PYTEST] * steps)
        done = rt.llm_request(entry(), api, document)
        found = noted(api, document)
        assert list(found) == list(range(3, min(steps, 6) + 1)), steps
        for number, text in found.items():
            assert f"{number} times" in text
            assert ("Stop calling tools now" in text) == (number == 6)
            assert ("Do not call it again" in text) == (number < 6)
        assert (done["loop"] or {}).get("action") == (None if steps < 3 else "note" if steps < 6 else "paused tools")
        assert (done["loop"] or {}).get("count", steps) == steps
        assert ("stop" in done) == (steps >= 6), "the answer is held from the sixth identical call on"
        assert previous is None or extends(document, previous)
        previous = document


def test_two_calls_taking_turns_and_a_call_that_waits():
    grep = ("exec_command", {"cmd": "grep -n foo src/x.py"}, "3: foo")
    read = ("exec_command", {"cmd": "sed -n 1,5p src/x.py"}, "def foo(): ...")
    previous = None
    for steps in range(1, 13):
        document = conversation("chat", ([grep, read] * 6)[:steps])
        rt.llm_request(entry(), "chat", document)
        assert previous is None or extends(document, previous)
        previous = document
    assert list(noted("chat", previous)) == [5, 6, 7, 8, 9, 10, 11, 12]  # the third to the sixth of each pair
    assert "3 times" in noted("chat", previous)[5] and "3 times" in noted("chat", previous)[6]
    assert "Stop calling tools now" in noted("chat", previous)[11] and "6 times" in noted("chat", previous)[12]
    # Polling counts against three times both thresholds: notes from the 9th to the 18th call.
    poll = ("exec_command", {"cmd": "sleep 30; gh run view 42"}, "in progress")
    document = conversation("chat", [poll] * 20)
    done = rt.llm_request(entry(), "chat", document)
    assert list(noted("chat", document)) == list(range(9, 19)) and done["stop"]
    assert "Stop calling tools now" in noted("chat", document)[18] and "9 times" in noted("chat", document)[9]


def test_the_places_follow_the_thresholds():
    history = rt.llm_tool_history(conversation("chat", [PYTEST] * 8), "chat")

    def places(**loop):
        return [(count, stop) for count, _, stop, _ in rt.llm_note_places(history, loop)]
    assert places(warn=3, block=6) == [(3, False), (4, False), (5, False), (6, True)]
    assert places(warn=2, block=4) == [(2, False), (3, False), (4, True)]
    assert places(warn=0, block=6) == [(6, True)]                      # no warning: only the stop
    assert places(warn=3, block=0) == [(count, False) for count in range(3, 9)]  # no stop: a note at every one
    assert places(warn=4, block=4) == [(4, True)] and places(warn=0, block=0) == []
    assert rt.llm_note_places([], {"warn": 3, "block": 6}) == []
    # A call without a result yet (the request's last, in some harnesses) has no place.
    document = conversation("chat", [PYTEST] * 3)
    document["messages"].pop()
    assert rt.llm_note_places(rt.llm_tool_history(document, "chat"), {"warn": 3, "block": 6}) == []


@pytest.mark.parametrize("api", ["responses", "chat", "messages"])
def test_your_next_message_keeps_the_earlier_turns_notes_and_starts_the_count_again(api):
    """The notes of a turn stay in its results after the human's next message: the first request of the next turn
    extends the last one of the turn before it (no prefill of the whole conversation), and the count starts again."""
    calls = [PYTEST] * 4
    last = conversation(api, calls)
    rt.llm_request(entry(), api, last)
    assert list(noted(api, last)) == [3, 4]
    previous = last
    for more in range(0, 4):
        document = conversation(api, calls + [PYTEST] * more, human_after={4: "pytest is not installed: read the code"})
        done = rt.llm_request(entry(), api, document)
        assert extends(document, previous), more
        assert list(noted(api, document)) == [3, 4] + ([7] if more == 3 else [])
        assert (done["loop"] or {}).get("count") == (3 if more == 3 else None), "counted from your last message"
        assert len(done["notes"]) == len(noted(api, document))
        previous = document


def test_the_relay_sends_the_changed_body_also_when_only_an_earlier_turn_has_notes():
    document = conversation("chat", [PYTEST] * 4 + [("exec_command", {"cmd": "ls"}, "a b")],
                            human_after={4: "read the code instead"})
    body = json.dumps(document).encode()
    head = b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(body)
    found = entry(presets={}, families=[])
    new_head, parts, done = rt.llm_body(head, [(body, body)], [("Content-Type", "application/json")], found, "chat")
    sent = b"".join(payload for _, payload in parts)
    assert done["loop"] is None and not done["params"] and len(done["notes"]) == 2
    assert sent != body and list(noted("chat", json.loads(sent))) == [3, 4]
    assert b"Content-Length: %d\r\n" % len(sent) in new_head


def test_a_long_turn_keeps_its_places():
    """The whole turn counts, not a window of its last calls: the 3rd identical call stays the 3rd after 600 more."""
    calls = debug_run(3 + 600)
    document = conversation("chat", calls)
    rt.llm_request(entry(), "chat", document)
    assert list(noted("chat", document)) == [3]
    shorter = conversation("chat", calls[:400])
    rt.llm_request(entry(), "chat", shorter)
    assert extends(document, shorter)


def test_the_guard_off_and_the_notes_off_change_nothing():
    for loop in ({**llm.LOOP_DEFAULTS, "enabled": False}, {**llm.LOOP_DEFAULTS, "warn": 0, "block": 0}):
        document = conversation("chat", [PYTEST] * 7)
        before = json.dumps(document)
        done = rt.llm_request(entry(loop=loop, presets={}, families=[]), "chat", document)
        assert json.dumps(document) == before and "notes" not in done and done["loop"] is None


# --- the watchdog's note keeps its place too ---------------------------------------------------------------------------

def test_the_watchdogs_note_stays_in_the_result_it_was_first_put_into():
    turns = rt.LlmTurns()
    found = entry(loop={**llm.LOOP_DEFAULTS, "watchdog_requests": 5})
    sent = []
    for steps in range(1, 9):
        document = conversation("chat", debug_run(3 + steps)[3:])
        done = rt.llm_request(found, "chat", document, turns, now=float(steps))
        sent.append(document)
        assert (done["loop"] or {}).get("kind") == ("watchdog" if steps > 5 else None)
        assert ("stop" in done) == (steps > 5)
    assert [list(noted("chat", document)) for document in sent[5:]] == [[6], [6], [6]]
    assert "6 model requests in this turn" in noted("chat", sent[-1])[6], "the text of the first time, unchanged"
    assert extends(sent[6], sent[5]) and extends(sent[7], sent[6])
    # After your next message: the note is still there, and the new turn is counted from one.
    document = conversation("chat", debug_run(3 + 9)[3:], human_after={8: "go on with the parser"})
    done = rt.llm_request(found, "chat", document, turns, now=20.0)
    assert done["loop"] is None and list(noted("chat", document)) == [6] and extends(document, sent[7])
    # Without a tool result in the turn the note is a message of its own at the end, as before.
    turns = rt.LlmTurns()
    for _ in range(6):
        document = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
        done = rt.llm_request(found, "chat", document, turns)
    assert done["notes"] == [{"text": document["messages"][-1]["content"], "appended": True}]
    assert document["messages"][-1]["content"].startswith(rt.LLM_NOTE) and done["stop"]


# --- the LLM traffic log: every note listed, the digests the agent's --------------------------------------------------

def test_the_traffic_log_lists_each_note_and_its_digests_are_the_agents():
    original = conversation("chat", [PYTEST] * 5)
    body = json.dumps(original).encode()
    head = b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(body)
    _, parts, done = rt.llm_body(head, [(body, body)], [("Content-Type", "application/json")], entry(), "chat",
                                 keep=True)
    sent = done["document"]
    notes = [{**note, "message": rt.llm_note_message(sent, "chat", note["holder"])} for note in done["notes"]]
    changes = [c for c in rt.llm_changes(done["before"], sent, {**done, "notes": notes}) if c["field"] == "messages"]
    assert [(c["message"], c["where"]) for c in changes] == [(7, "the result of a tool call"),
                                                              (9, "the result of a tool call"),
                                                              (11, "the result of a tool call")]
    assert ["3 times" in changes[0]["note"], "4 times" in changes[1]["note"], "5 times" in changes[2]["note"]] == \
        [True] * 3
    assert rt.llm_digests(sent, "chat", done["notes"]) == [rt.llm_digest("")] + [rt.llm_digest(m) for m in
                                                                               original["messages"]]
    assert list(noted("chat", sent)) == [3, 4, 5], "put back after the digests"
    # Anthropic's list results and one note alone.
    original = conversation("messages", [PYTEST] * 3)
    document = copy.deepcopy(original)
    done = rt.llm_request(entry(), "messages", document)
    assert rt.llm_digests(document, "messages", done["notes"][0]) == \
        [rt.llm_digest("")] + [rt.llm_digest(m) for m in original["messages"]]


# --- end to end: the real proxy, a growing conversation ----------------------------------------------------------------

scripted = live.scripted


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("api", ["responses", "chat", "messages"])
def test_e2e_every_request_through_the_proxy_extends_the_one_before(tmp_path, scripted, api):
    """What LM Studio's prompt cache needs: each request the server gets begins with the whole request before it,
    the guard's notes included - through the real proxy, a request per step as the harness sends them (without any
    note: the proxy puts them in again, each where it was)."""
    relay = live.Relay(tmp_path, scripted.server_address[1])
    try:
        calls = debug_run(9) + [PYTEST] * 3
        for steps in range(2, len(calls) + 1):
            document = {**conversation(api, calls[:steps]), "model": "m", "stream": steps % 2 == 0}
            head, body = live.dechunk(relay.post(live.PATHS[api], document))
            assert b" 200 " in head.split(b"\r\n", 1)[0]
            result = live.read_answer(api, body)
            if steps < len(calls):
                assert result["tool_calls"], "the model's call passes: a note only"
            else:  # the sixth identical call: the model calls again, the relay's answer ends the turn
                assert not result.get("tool_calls") and "6 times" in result["text"] and "ended" in result["text"]
        bodies = live.Scripted.bodies
        assert len(bodies) == len(calls) - 1
        for earlier, later in zip(bodies, bodies[1:]):
            assert extends(later, earlier)
        assert not noted(api, bodies[0]) and list(noted(api, bodies[1])) == [3]
        assert list(noted(api, bodies[-1])) == [3, 10, 11, 12]
        assert "Stop calling tools now" in noted(api, bodies[-1])[12] and "tool_choice" not in bodies[-1]
        records = relay.records(len(calls) - 1)
        assert [bool(record.get("llm_loop")) for record in records] == [False] + [True] * (len(calls) - 2)
        assert records[-1]["llm_loop"]["action"] == "paused tools" and records[-1]["llm_loop"]["ended"]
    finally:
        relay.close()


# --- G6: Codex's namespace tools reach a local model as plain functions ------------------------------------------------

def function(name, **properties):
    return {"type": "function", "name": name, "description": f"{name}: what it does.", "strict": False,
            "parameters": {"type": "object", "properties": properties or {"path": {"type": "string"}},
                           "required": list(properties or {"path": 1})[:1], "additionalProperties": False}}


def codex_tools():
    """The tools of a Codex 0.166 request with --mcp files,exec (runs/mock_codex_ns, request 1: 13 tools, three of
    them namespaces with 5, 7 and 18 functions; a few of each here)."""
    return [function("exec_command", cmd={"type": "string"}), function("write_stdin"), function("view_image"),
            {"type": "namespace", "name": "multi_agent_v1", "description": "Tools for spawning and managing sub-agents.",
             "tools": [function("close_agent", target={"type": "string"}), function("spawn_agent")]},
            {"type": "namespace", "name": "mcp__exec", "description": "Runs commands for you in the project folder.",
             "tools": [function("run_command", command={"type": "string"}), function("run_tests")]},
            {"type": "namespace", "name": "mcp__files", "description": "Reads and writes files of the workspace.",
             "tools": [function("read_file"), function("write_file", path={"type": "string"},
                                                       content={"type": "string"})]},
            function("update_goal")]


FLAT = ["exec_command", "write_stdin", "view_image", "multi_agent_v1__close_agent", "multi_agent_v1__spawn_agent",
        "mcp__exec__run_command", "mcp__exec__run_tests", "mcp__files__read_file", "mcp__files__write_file",
        "update_goal"]


def codex_request(calls=(), stream=True):
    document = codex_steps(list(calls), namespace="mcp__files")
    return {**document, "tools": codex_tools(), "stream": stream, "parallel_tool_calls": True, "store": False}


def test_regression_codexs_mcp_servers_reach_the_model_as_plain_functions():
    """probes/qcn/codex_ns.json: Codex's real request to LM Studio - "I don't have access to a `write_file` tool from
    the MCP server named files. The available tools ... exec_command, write_stdin ..."; the same request with the
    namespaces as functions: the model calls mcp__files__write_file (codex_flat.json)."""
    document = codex_request()
    original = copy.deepcopy(document["tools"])
    done = rt.llm_request(entry(), "responses", document)
    assert [tool["type"] for tool in document["tools"]] == ["function"] * 10
    assert [tool["name"] for tool in document["tools"]] == FLAT, "each namespace's tools where it stood, in order"
    assert done["tools"]["namespaces"] == 3 and done["tools"]["calls"] == 0
    assert done["tools"]["names"] == {
        "multi_agent_v1__close_agent": ["multi_agent_v1", "close_agent"],
        "multi_agent_v1__spawn_agent": ["multi_agent_v1", "spawn_agent"],
        "mcp__exec__run_command": ["mcp__exec", "run_command"], "mcp__exec__run_tests": ["mcp__exec", "run_tests"],
        "mcp__files__read_file": ["mcp__files", "read_file"], "mcp__files__write_file": ["mcp__files", "write_file"]}
    # A function is offered as it was, but for its name.
    written = next(tool for tool in document["tools"] if tool["name"] == "mcp__files__write_file")
    assert {**written, "name": "write_file"} == original[5]["tools"][1]
    assert document["tools"][0] == original[0] and document["tools"][-1] == original[-1]
    assert "namespace" not in json.dumps(document["tools"])


def test_the_conversations_calls_are_named_like_the_tools_the_model_is_offered():
    """Codex sends a call of an MCP tool back as {"name": "write_file", "namespace": "mcp__files"} (runs/mock_codex_ns,
    request 2); the server gets the name it offered. exec_command and the results stay as they came."""
    document = codex_request([("write_file", {"path": "a.txt", "content": "x"}, codex_mcp_output('{"action": "created"}')),
                              ("exec_command", {"cmd": "cat a.txt"}, "Chunk ID: 1a2b3c\nOutput:\nx\n")])
    results = copy.deepcopy([item for item in document["input"] if item["type"] == "function_call_output"])
    done = rt.llm_request(entry(), "responses", document)
    calls = [item for item in document["input"] if item["type"] == "function_call"]
    assert [(call["name"], "namespace" in call) for call in calls] == [("mcp__files__write_file", False),
                                                                       ("exec_command", False)]
    assert done["tools"]["calls"] == 1
    assert [item for item in document["input"] if item["type"] == "function_call_output"] == results
    # The loop guard's note names the tool as the model sees it (runs/t5_mcp__codex-tuned__qcn, request 31).
    same = ("read_file", {"path": "logpipe/report.py"}, codex_mcp_output('{"content": "import argparse"}'))
    document = codex_request([same] * 3)
    done = rt.llm_request(entry(), "responses", document)
    assert done["loop"] == {"action": "note", "kind": "repeat", "tool": "mcp__files__read_file", "count": 3}
    assert "mcp__files__read_file" in document["input"][-1]["output"][-1]["text"]


def test_requests_without_namespace_tools_pass_as_they_came():
    for api, document in (("responses", {**codex_request(), "tools": [function("exec_command")]}),
                          ("chat", conversation("chat", [PYTEST])), ("messages", conversation("messages", [PYTEST]))):
        body = json.dumps(document).encode()
        head = b"POST /v1/x HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(body)
        _, parts, done = rt.llm_body(head, [(body, body)], [("Content-Type", "application/json")],
                                     entry(presets={}, families=[]), api)
        assert b"".join(payload for _, payload in parts) == body and "tools" not in done
    assert rt.llm_flat_tools({"tools": "x"}) == {} and rt.llm_flat_tools({}) == {} and rt.llm_flat_tools(None) == {}


def test_odd_namespace_tools():
    # A name that would stand twice: nothing is changed (the server would refuse two tools of one name).
    document = {"tools": [function("mcp__files__read_file"),
                          {"type": "namespace", "name": "mcp__files", "tools": [function("read_file")]}]}
    before = copy.deepcopy(document)
    assert rt.llm_flat_tools(document) == {} and document == before
    # A namespace without a list of tools, or without a name, stays; a tool without a name inside one is left out.
    document = {"tools": [{"type": "namespace", "name": "a"}, {"type": "namespace", "tools": [function("x")]},
                          {"type": "namespace", "name": "ns", "tools": [function("x"), {"type": "function"}, 7]}]}
    assert rt.llm_flat_tools(document) == {"names": {"ns__x": ["ns", "x"]}, "namespaces": 1, "calls": 0}
    assert [tool.get("name") for tool in document["tools"]] == ["a", None, "ns__x"]
    # A call of a namespace no longer offered is still named the same way (the conversation stays what it was).
    document = {"tools": [function("exec_command")], "input": [
        {"type": "function_call", "name": "read_file", "namespace": "mcp__gone", "arguments": "{}", "call_id": "c"},
        {"type": "function_call", "name": "x", "namespace": 7, "arguments": "{}", "call_id": "d"}, "text", None]}
    assert rt.llm_flat_tools(document) == {"names": {}, "namespaces": 0, "calls": 1}
    assert document["input"][0]["name"] == "mcp__gone__read_file" and document["input"][1]["namespace"] == 7


NAMES = {"mcp__files__write_file": ["mcp__files", "write_file"], "mcp__exec__run_command": ["mcp__exec", "run_command"]}


def responses_stream(calls, text=None, model="qwen/qwen3-coder-next", eol="\n"):
    """A Responses API answer as LM Studio streams it: CALLS [(name, arguments)] as function_call items (added,
    arguments delta and done, item done), TEXT as a message, then response.completed with the whole output."""
    output, events = [], []
    response = {"id": "resp_31be7d17", "object": "response", "status": "in_progress", "model": model, "output": [],
                "tools": [{"type": "function", "name": name} for name, _ in calls]}
    events.append({"type": "response.created", "response": response})
    for index, (name, arguments) in enumerate(calls):
        item = {"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}", "name": name,
                "arguments": arguments, "status": "completed"}
        events += [{"type": "response.output_item.added", "output_index": index,
                    "item": {**item, "arguments": "", "status": "in_progress"}},
                   {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": index,
                    "delta": arguments},
                   {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": index,
                    "arguments": arguments},
                   {"type": "response.output_item.done", "output_index": index, "item": item}]
        output.append(item)
    if text is not None:
        item = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
        events += [{"type": "response.output_item.added", "output_index": len(output), "item": {**item, "content": []}},
                   {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": len(output), "delta": text},
                   {"type": "response.output_item.done", "output_index": len(output), "item": item}]
        output.append(item)
    events.append({"type": "response.completed", "response": {
        **response, "status": "completed", "output": output,
        "usage": {"input_tokens": 14092, "output_tokens": 35, "total_tokens": 14127}}})
    return "".join(f"event: {event['type']}{eol}data: {json.dumps(event, separators=(',', ':'))}{eol}{eol}"
                   for event in events).encode()


def events_of(data):
    return [json.loads(line[5:]) for line in data.replace(b"\r\n", b"\n").split(b"\n") if line.startswith(b"data:")]


def calls_of(data):
    """[(event type, name, namespace)] of every function_call item in a stream, the completed output's too."""
    found = []
    for event in events_of(data):
        items = [event["item"]] if "item" in event else (event.get("response") or {}).get("output") or []
        found += [(event["type"], item["name"], item.get("namespace")) for item in items
                  if item.get("type") == "function_call"]
    return found


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_regression_the_models_calls_get_codexs_names_back(eol):
    """runs/smoke__codex-hfns__qcn: the model calls mcp__files__write_file; Codex knows the tool as write_file of the
    namespace mcp__files and runs it only under that name (4 MCP calls in the run, all points)."""
    arguments = '{"path":"hello.txt","content":"hello ü"}'
    data = responses_stream([("mcp__files__write_file", arguments), ("exec_command", '{"cmd":"ls"}'),
                             ("mcp__exec__run_command", '{"command":"cat hello.txt"}')], text="done", eol=eol)
    expected = [(kind, name, namespace) for name, namespace in (
        ("write_file", "mcp__files"), ("exec_command", None), ("run_command", "mcp__exec"))
        for kind in ("response.output_item.added", "response.output_item.done")]
    for size in (1, 7, 64, 1000, len(data)):  # the stream arrives in pieces of any size: lines and characters split
        answer = rt.LlmToolAnswer(NAMES)
        out = b"".join(answer.feed(data[start:start + size]) for start in range(0, len(data), size)) + answer.flush()
        found = calls_of(out)
        assert sorted(found[:6]) == sorted(expected)
        assert found[6:] == [("response.completed", "write_file", "mcp__files"), ("response.completed", "exec_command",
                                                                                 None),
                             ("response.completed", "run_command", "mcp__exec")]
        assert answer.changed == 5, "the events rewritten: added and done of two calls, and the completed response"
        # Nothing else changes: the arguments, the text, every other event, the line ends.
        before, after = events_of(data), events_of(out)
        assert len(before) == len(after) and out.count(eol.encode()) == data.count(eol.encode())
        for old, new in zip(before, after):
            if old["type"] in ("response.created", "response.function_call_arguments.delta",
                               "response.function_call_arguments.done", "response.output_text.delta"):
                assert old == new
        done = [event["item"] for event in after if event["type"] == "response.output_item.done"]
        assert done[0]["arguments"] == arguments and done[0]["call_id"] == "call_0" and done[3]["content"][0]["text"] \
            == "done"
        lines = [line for line in out.split(eol.encode()) if line.startswith(b"event:")]
        assert lines == [line for line in data.split(eol.encode()) if line.startswith(b"event:")]


def test_an_answer_without_such_a_call_passes_byte_for_byte():
    for data in (responses_stream([("exec_command", '{"cmd":"ls"}')], text="function_call is a word here"),
                 b"data: not json function_call\n\ndata: [DONE]\n\n", b"", b"data: {\"item\": 7, \"function_call\": 1}\n",
                 b"no line end and a function_call"):
        answer = rt.LlmToolAnswer(NAMES)
        assert b"".join(answer.feed(data[start:start + 5]) for start in range(0, len(data), 5)) + answer.flush() == data
        assert answer.changed == 0


def test_an_answer_that_is_one_json_document():
    item = {"type": "function_call", "id": "fc_1", "call_id": "c9", "name": "mcp__files__write_file",
            "arguments": '{"path":"a"}', "status": "completed"}
    data = json.dumps({"id": "resp_1", "object": "response", "status": "completed", "output": [item]}).encode()
    out = json.loads(rt.LlmToolAnswer(NAMES).whole(data))
    assert out["output"] == [{**item, "name": "write_file", "namespace": "mcp__files"}] and out["id"] == "resp_1"
    for same in (json.dumps({"output": [{**item, "name": "exec_command"}]}).encode(), b"[1, 2]", b"not json", b""):
        assert rt.LlmToolAnswer(NAMES).whole(same) == same
    # Events sent with a length (no stream): read as lines.
    stream = responses_stream([("mcp__files__write_file", "{}")])
    assert calls_of(rt.LlmToolAnswer(NAMES).whole(stream))[-1] == ("response.completed", "write_file", "mcp__files")


def test_the_pieces_of_a_rewritten_stream_are_whole_chunks():
    data = responses_stream([("mcp__files__write_file", "{}")])

    def chunked(size):
        for start in range(0, len(data), size):
            part = data[start:start + size]
            yield b"%x\r\n" % len(part), b""
            yield part, part
            yield b"\r\n", b""
        yield b"0\r\n\r\n", b""
    for size in (3, 50, len(data)):
        pieces = list(rt.llm_tool_pieces(chunked(size), rt.LlmToolAnswer(NAMES), True))
        assert pieces[-1] == (b"0\r\n\r\n", b"")
        assert all(raw == b"%x\r\n" % len(payload) + payload + b"\r\n" for raw, payload in pieces[:-1])
        head, body = live.dechunk(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + b"".join(r for r, _ in pieces))
        assert calls_of(body)[-1] == ("response.completed", "write_file", "mcp__files")
        # To the connection's end (no chunks): the bytes as they come, the rest at the end.
        plain = list(rt.llm_tool_pieces(((part, part) for _, part in chunked(size) if part), rt.LlmToolAnswer(NAMES),
                                        False))
        assert all(raw == payload for raw, payload in plain) and b"".join(raw for raw, _ in plain) == body


def test_the_traffic_log_says_what_became_of_the_tools():
    document = codex_request()
    before = rt.llm_snapshot(document)
    done = rt.llm_request(entry(), "responses", document)
    change = next(c for c in rt.llm_changes(before, document, done) if c["field"] == "tools")
    assert change["from"] == "3 namespace tools" and change["why"] == "local server"
    assert "6 plain functions NAMESPACE__TOOL" in change["note"]


# --- the agents' tools for MCP resources say that a server's tools are functions of their own -------------------------

RESOURCE_NAMES = ["list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"]


def chat_tool(name, description="What it does."):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": {}, "required": []}}}


def opencode_tools():
    """OpenCode 1.19.5's tools with --mcp files,exec (research round 1, runs/smoke__opencode__laguna__r1, request 2:
    38 tools in alphabetical order; a few here). An MCP server's tool is SERVER_TOOL."""
    return [chat_tool("bash"), chat_tool("edit"), chat_tool("exec_run_command"), chat_tool("files_read_file"),
            chat_tool("files_write_file"), chat_tool("glob"),
            chat_tool("list_mcp_resource_templates", "Lists resource templates provided by connected MCP servers. "
                      "Resource templates are parameterized resources that can be read after filling in their URI "
                      "template."),
            chat_tool("list_mcp_resources", "Lists resources provided by connected MCP servers. Resources provide "
                      "context such as files, database schemas, or application-specific information."),
            chat_tool("read"),
            chat_tool("read_mcp_resource", "Read a specific resource from an MCP server using the server name and "
                      "resource URI. The URI is an MCP identifier and does not need to be a file URL."),
            chat_tool("todowrite"), chat_tool("write")]


def hint_of(tool, before):
    """What the relay put after a tool's description BEFORE (the description is in the tool or in its "function")."""
    holder = tool.get("function", tool)
    assert holder["description"].startswith(before + " ("), "the agent's own text stays, the hint after it"
    return holder["description"][len(before) + 1:]


def resource_tools():
    """Codex 0.166's three tools for MCP resources as it offers them (research round 1, runs/smoke__codex__qcn__r1,
    request 1; the parameters shortened)."""
    def tool(name, description, **properties):
        return {"type": "function", "name": name, "description": description, "strict": False,
                "parameters": {"type": "object", "properties": properties, "required": [],
                               "additionalProperties": False}}
    return [tool("list_mcp_resources", "Lists resources provided by MCP servers. Resources allow servers to share "
                 "data that provides context to language models, such as files, database schemas, or "
                 "application-specific information. Prefer resources over web search when possible.",
                 server={"type": "string"}, cursor={"type": "string"}),
            tool("list_mcp_resource_templates", "Lists resource templates provided by MCP servers. Parameterized "
                 "resource templates allow servers to share data that takes parameters and provides context to "
                 "language models, such as files, database schemas, or application-specific information. Prefer "
                 "resource templates over web search when possible.", server={"type": "string"}),
            tool("read_mcp_resource", "Read a specific resource from an MCP server given the server name and "
                 "resource URI.", server={"type": "string"}, uri={"type": "string"})]


def test_regression_codexs_resource_tools_say_that_a_servers_tools_are_functions():
    """Research round 1 (sandbox-ai's own proxy, LM Studio), asked to read a file "with the MCP tool read_file of the
    server named files": runs/smoke__codex__laguna__r1 - Laguna-S called read_mcp_resource 27 times with guessed URIs
    ("resources/read failed ... Unknown resource": file://, cwd://, %2F ..., no two calls the same); runs/
    smoke__codex__qcn__r1 - Qwen3-Coder-Next five times before its first mcp__files__read_file. Codex's "Read a
    specific resource from an MCP server given the server name" reads like the way to an MCP server's tools."""
    document = {**codex_request(), "tools": codex_tools()[:3] + resource_tools() + codex_tools()[3:]}
    original = copy.deepcopy(document["tools"])
    done = rt.llm_request(entry(), "responses", document)
    told = {tool["name"]: tool for tool in document["tools"] if tool["name"] in RESOURCE_NAMES}
    assert list(told) == RESOURCE_NAMES
    for name, tool in told.items():
        before = next(t for t in original if t.get("name") == name)
        hint = hint_of(tool, before["description"])
        assert "tool T of the MCP server exec is the function mcp__exec__T, of files mcp__files__T - for example " \
               "mcp__exec__run_command." in hint, "how this very request names its MCP functions (not Codex's own)"
        assert "does not call a server's tools" in hint and "\n" not in hint and hint.endswith(")")
        assert {**tool, "description": before["description"]} == before, "nothing else of the tool changes"
    assert done["tools"]["resources"] == RESOURCE_NAMES and done["tools"]["namespaces"] == 3
    # The same request again gets the same text (the tools are the beginning of the server's prompt: its cache).
    again = {**codex_request(), "tools": codex_tools()[:3] + resource_tools() + codex_tools()[3:]}
    rt.llm_request(entry(), "responses", again)
    assert again["tools"] == document["tools"]


def test_regression_opencodes_resource_tools_name_a_tool_of_the_starts_mcp_servers():
    """runs/smoke__opencode__laguna__r1: list_mcp_resources, list_mcp_resource_templates, read_mcp_resource with
    three guessed URIs, then "The MCP `files` server isn't exposing any resources/templates, so I can't use it" - and
    OpenCode's own read tool instead of the server's (no MCP call in the run). OpenCode names an MCP tool
    SERVER_TOOL: the start's servers (the entry's "mcp", llm.proxy_params) tell which tools are theirs."""
    document = {"model": "poolside/laguna-s-2.1", "stream": True, "tools": opencode_tools(),
                "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "read it"}]}
    original = copy.deepcopy(document["tools"])
    done = rt.llm_request(entry(mcp=["files", "exec"]), "chat", document)
    assert done["tools"] == {"resources": ["list_mcp_resource_templates", "list_mcp_resources", "read_mcp_resource"]}
    for index in (6, 7, 9):
        hint = hint_of(document["tools"][index], original[index]["function"]["description"])
        assert "tool T of the MCP server files is the function files_T, of exec exec_T - for example " \
               "files_read_file." in hint  # each of the start's servers with the prefix its tools have here
    assert [tool for index, tool in enumerate(document["tools"]) if index not in (6, 7, 9)] == \
        [tool for index, tool in enumerate(original) if index not in (6, 7, 9)]
    # Without the servers' names (an older start's proxy.json): the sentence without an example.
    plain = {**document, "tools": copy.deepcopy(original)}
    rt.llm_request(entry(), "chat", plain)
    hint = hint_of(plain["tools"][9], original[9]["function"]["description"])
    assert "for example" not in hint and "named after the server and the tool. Call that function directly.)" in hint


@pytest.mark.parametrize("api,tools,servers,changed,example", [
    # Claude Code (Anthropic Messages): its two resource tools; an MCP tool is mcp__SERVER__TOOL
    ("messages", [{"name": "Bash", "description": "Runs a command.", "input_schema": {"type": "object", "properties": {}, "required": []}},
                  {"name": "ListMcpResourcesTool", "description": "List available resources from configured MCP "
                                                                  "servers.", "input_schema": {"type": "object", "properties": {}, "required": []}},
                  {"name": "ReadMcpResourceTool", "description": "Reads a specific resource from an MCP server.",
                   "input_schema": {"type": "object", "properties": {}, "required": []}},
                  {"name": "mcp__files__read_file", "description": "Reads a text file.",
                   "input_schema": {"type": "object", "properties": {}, "required": []}}],
     None, ["ListMcpResourcesTool", "ReadMcpResourceTool"], "mcp__files__read_file"),
    # Hermes: each server's own list_resources and read_resource - never the example themselves
    ("chat", [chat_tool("terminal"), chat_tool("mcp__files__list_resources", "List available resources on files."),
              chat_tool("mcp__files__read_resource", "Read a resource of files."), chat_tool("mcp__files__stat")],
     ["files"], ["mcp__files__list_resources", "mcp__files__read_resource"], "mcp__files__stat"),
    # Goose: the extension manager's; a server's tool is SERVER__TOOL; a server with a hyphen in its name
    ("chat", [chat_tool("shell"), chat_tool("extensionmanager__read_resource", "Read a resource from an extension."),
              chat_tool("web_fetch__fetch_page")], ["web-fetch"], ["extensionmanager__read_resource"],
     "web_fetch__fetch_page"),
    # no MCP tool to name: the sentence without an example
    ("chat", [chat_tool("bash"), chat_tool("read_mcp_resource", "Read a resource.")], ["files"],
     ["read_mcp_resource"], None),
])
def test_the_other_agents_resource_tools(api, tools, servers, changed, example):
    document = {"model": "m", "tools": copy.deepcopy(tools), "messages": [{"role": "user", "content": "x"}]}
    done = rt.llm_request(entry(**({"mcp": servers} if servers else {})), api, document)
    assert done["tools"] == {"resources": changed}
    for after, before in zip(document["tools"], tools):
        name = before.get("function", before)["name"]
        if name in changed:
            hint = hint_of(after, before.get("function", before)["description"])
            assert (f"for example {example}." in hint) if example else ("for example" not in hint)
        else:
            assert after == before


def test_requests_without_resource_tools_and_odd_ones_stay_as_they_came():
    for api, document in (("responses", codex_request()), ("chat", conversation("chat", [PYTEST])),
                          ("messages", conversation("messages", [PYTEST]))):
        before = copy.deepcopy(document.get("tools"))
        done = rt.llm_request(entry(mcp=["files"]), api, document)
        assert "resources" not in (done.get("tools") or {})
        assert api == "responses" or document.get("tools") == before
    # a tool of that name without a description, with one that is no text; tools that are no tools
    odd = {"tools": [{"type": "function", "name": "read_mcp_resource", "description": None},
                     {"type": "function", "name": "read_mcp_resource"}, "x", {"type": "function"}, None,
                     {"type": "function", "function": {"name": "list_mcp_resources", "description": ""}}]}
    before = copy.deepcopy(odd)
    assert rt.llm_resource_hint(odd, ["files"], {"mcp__files__read_file": ["mcp__files", "read_file"]}) == []
    assert odd == before
    # a name that only ends like one is no resource tool
    assert rt.llm_resource_hint({"tools": [{"name": "unread_resources", "description": "x"},
                                           {"name": "resource", "description": "x"}]}) == []


def test_the_traffic_log_says_that_the_resource_tools_were_told():
    document = {**codex_request(), "tools": resource_tools() + codex_tools()}
    before = rt.llm_snapshot(document)
    done = rt.llm_request(entry(), "responses", document)
    notes = [c["note"] for c in rt.llm_changes(before, document, done) if c["field"] == "tools"]
    assert any("read_mcp_resource" in note and "description" in note for note in notes), notes


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_server_gets_the_resource_tools_with_the_sentence_and_the_agent_its_answer(tmp_path, model):
    """Through the real proxy: OpenCode's chat request with its resource tools; the model server gets them with the
    sentence (and one of the start's MCP tools as the example), every other tool and the conversation as they came;
    the answer passes untouched; the connection log says which tools were told."""
    relay = live.Relay(tmp_path, model.server_address[1])
    try:
        relay.server.config["llm"][0]["mcp"] = ["files", "exec"]
        answer = json.dumps({"id": "c1", "object": "chat.completion", "model": "m", "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "DONE"}}]}).encode()
        Model.script = [answer]
        document = {"model": "m", "tools": opencode_tools(),
                    "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "read it"}]}
        head, body = live.dechunk(relay.post("/v1/chat/completions", document))
        assert b" 200 " in head.split(b"\r\n", 1)[0] and json.loads(body)["choices"][0]["message"]["content"] == "DONE"
        sent = Model.bodies[0]
        assert sent["messages"] == document["messages"]
        told = [tool["function"] for tool in sent["tools"] if tool["function"]["name"] == "read_mcp_resource"]
        assert "is the function files_T, of exec exec_T - for example files_read_file. Call that function " \
               "directly.)" in told[0]["description"]
        assert [tool for tool in sent["tools"] if "resource" not in tool["function"]["name"]] == \
            [tool for tool in document["tools"] if "resource" not in tool["function"]["name"]]
        assert relay.records(1)[0]["llm_tools"] == {
            "resources": ["list_mcp_resource_templates", "list_mcp_resources", "read_mcp_resource"]}
    finally:
        relay.close()


def test_the_starts_mcp_servers_reach_the_proxys_entry():
    """proxy.json's llm entry names the start's MCP servers (--mcp), each once, in their order; none: no key."""
    assert llm.main(["add", "mac", "--kind", "llamacpp", "--url", "http://127.0.0.1:8080", "--model", "m"],
                    out=lambda line: None) == 0

    def start(**more):
        a = argparse.Namespace(llm="mac", agent="opencode", llm_param=[], **more)
        a.llm_addresses = ["127.0.0.1"]
        return llm.proxy_entry(a)[0]
    assert start(mcp=["files", "exec", "files", "web-fetch"])["mcp"] == ["files", "exec", "web-fetch"]
    assert "mcp" not in start(mcp=[]) and "mcp" not in start(mcp=None) and "mcp" not in start()


# --- G7: Codex's approval parameters are not offered to a local model --------------------------------------------------

def exec_command_tool():
    """Codex 0.166's exec_command as it offers it (runs/mock_codex_ns, request 1; descriptions shortened)."""
    return {"type": "function", "name": "exec_command", "strict": False,
            "description": "Runs a command in a PTY, returning output or a session ID for ongoing interaction.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute."},
                "justification": {"type": "string", "description": "User-facing approval question for "
                                                                   "`require_escalated`; omit otherwise."},
                "login": {"type": "boolean"}, "max_output_tokens": {"type": "number"},
                "prefix_rule": {"type": "array", "items": {"type": "string"}},
                "sandbox_permissions": {"type": "string", "enum": ["use_default", "require_escalated"]},
                "shell": {"type": "string"}, "tty": {"type": "boolean"}, "workdir": {"type": "string"},
                "yield_time_ms": {"type": "number"}}, "required": ["cmd"], "additionalProperties": False}}


def test_regression_codexs_approval_parameters_are_not_offered_to_the_model():
    """runs/t2_debug__codex-hfsn__qcn: the model filled `justification` ("List files", "Read stats.py ...") at 24 of
    24 exec_command calls, Codex refused each ("`justification` requires an explicit `sandbox_permissions`"), the
    model repeated the command and the loop guard ended the turn after 25 requests, nothing done."""
    document = {**codex_request(), "tools": [exec_command_tool(), function("write_stdin")] + codex_tools()[3:]}
    original = copy.deepcopy(document["tools"])
    done = rt.llm_request(entry(), "responses", document)
    assert done["tools"]["plain"] == ["exec_command"] and done["tools"]["namespaces"] == 3
    offered = document["tools"][0]
    assert list(offered["parameters"]["properties"]) == ["cmd", "login", "max_output_tokens", "shell", "tty", "workdir",
                                                         "yield_time_ms"]
    assert offered["parameters"]["required"] == ["cmd"] and offered["description"] == original[0]["description"]
    assert offered["parameters"]["properties"]["cmd"] == original[0]["parameters"]["properties"]["cmd"]
    for word in rt.LLM_APPROVAL_PARAMETERS:
        assert word not in json.dumps(document["tools"])
    assert document["tools"][1] == original[1]


def test_only_codexs_own_approval_parameters_go():
    # A tool with a `justification` of its own (an MCP server's) keeps it: the mark is sandbox_permissions.
    own = function("request_access", justification={"type": "string"}, path={"type": "string"})
    escalating = function("shell", command={"type": "array"}, sandbox_permissions={"type": "string"},
                          justification={"type": "string"})
    escalating["parameters"]["required"] = ["command", "justification"]
    document = {"tools": [own, {"type": "namespace", "name": "ns", "tools": [copy.deepcopy(escalating)]}, escalating,
                          {"type": "function", "name": "odd", "parameters": {"properties": ["sandbox_permissions"]}},
                          {"type": "function", "name": "none"}, "text"]}
    before = copy.deepcopy(own)
    assert rt.llm_plain_tools(document) == ["shell", "shell"]
    assert document["tools"][0] == before
    for tool in (document["tools"][1]["tools"][0], document["tools"][2]):
        assert list(tool["parameters"]["properties"]) == ["command"] and tool["parameters"]["required"] == ["command"]
    assert rt.llm_plain_tools({}) == [] and rt.llm_plain_tools({"tools": 7}) == [] and rt.llm_plain_tools(None) == []
    # Chat completions and Anthropic Messages requests are not Codex's: untouched.
    chat = {"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": [
        {"type": "function", "function": {"name": "bash", "parameters": {"properties": {
            "sandbox_permissions": {"type": "string"}, "justification": {"type": "string"}}}}}]}
    before = json.dumps(chat)
    assert "tools" not in rt.llm_request(entry(presets={}, families=[]), "chat", chat) and json.dumps(chat) == before


@pytest.mark.parametrize("arguments,expected", [
    ('{"cmd":"ls -la work/","justification":"List files in the project"}', '{"cmd":"ls -la work/"}'),
    ('{"cmd":"python3 -m pytest 2>&1","justification":""}', '{"cmd":"python3 -m pytest 2>&1"}'),
    ('{"cmd": "ls", "justification": "x", "sandbox_permissions": "require_escalated", "prefix_rule": ["ls"]}',
     '{"cmd":"ls"}'),
    ('{"cmd":"cat ü.txt","workdir":"/work","justification":"read"}', '{"cmd":"cat \\u00fc.txt","workdir":"/work"}'),
    ('{"cmd":"grep justification notes.md"}', None),         # the word in a value: the call stays as it is
    ('{"cmd":"ls"}', None), ("not json justification", None), ('["justification"]', None), (None, None), ("", None),
])
def test_the_approval_parameters_are_taken_out_of_a_calls_arguments(arguments, expected):
    assert rt.llm_plain_arguments(arguments) == expected


def test_regression_a_call_that_fills_justification_reaches_codex_without_it():
    """The model fills `justification` although it is no longer offered (it knows Codex's tools from its training,
    and from earlier calls of the conversation): Codex gets the call without it, and runs it."""
    filled = '{"cmd":"ls -la work/","justification":"List files in the project"}'
    data = responses_stream([("exec_command", filled), ("mcp__files__write_file", '{"path":"a","justification":"mine"}')])
    for size in (1, 33, len(data)):
        answer = rt.LlmToolAnswer(NAMES, ["exec_command"])
        out = b"".join(answer.feed(data[start:start + size]) for start in range(0, len(data), size)) + answer.flush()
        events = events_of(out)
        done = [event["item"] for event in events if event["type"] == "response.output_item.done"]
        assert done[0]["name"] == "exec_command" and done[0]["arguments"] == '{"cmd":"ls -la work/"}'
        # Another tool's `justification` is its own: kept (and the tool named back).
        assert done[1]["name"] == "write_file" and done[1]["arguments"] == '{"path":"a","justification":"mine"}'
        assert [item["arguments"] for item in events[-1]["response"]["output"]] == [done[0]["arguments"],
                                                                                    done[1]["arguments"]]
        finished = [event for event in events if event["type"] == "response.function_call_arguments.done"]
        assert [event["arguments"] for event in finished] == ['{"cmd":"ls -la work/"}',
                                                              '{"path":"a","justification":"mine"}']
    # Not streamed.
    item = {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "exec_command", "arguments": filled}
    whole = rt.LlmToolAnswer({}, ["exec_command"]).whole(json.dumps({"output": [item]}).encode())
    assert json.loads(whole)["output"][0]["arguments"] == '{"cmd":"ls -la work/"}'
    # Without such a tool in the request nothing of an answer is touched.
    assert rt.LlmToolAnswer({}, []).whole(json.dumps({"output": [item]}).encode()) == \
        json.dumps({"output": [item]}).encode()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_codexs_shell_call_with_a_justification_runs(tmp_path, model):
    """Through the real proxy: the server is offered exec_command without the three parameters; the model's call
    with `justification` reaches Codex without it (streamed and not), and the logs say what was done."""
    relay = live.Relay(tmp_path, model.server_address[1])
    try:
        filled = '{"cmd":"cd work && ls -la","justification":"List the project files"}'
        item = {"type": "function_call", "id": "fc_0", "call_id": "call_0", "name": "exec_command", "arguments": filled,
                "status": "completed"}
        Model.script = [responses_stream([("exec_command", filled)]),
                        json.dumps({"id": "resp_2", "object": "response", "status": "completed", "model": "m",
                                    "output": [item]}).encode()]
        for stream in (True, False):
            document = {**codex_request(stream=stream), "tools": [exec_command_tool(), function("write_stdin")]}
            head, body = live.dechunk(relay.post("/v1/responses", document))
            assert b" 200 " in head.split(b"\r\n", 1)[0]
            sent = Model.bodies[-1]["tools"]
            assert list(sent[0]["parameters"]["properties"]) == ["cmd", "login", "max_output_tokens", "shell", "tty",
                                                                 "workdir", "yield_time_ms"] and sent[1] == \
                function("write_stdin")
            result = live.read_answer("responses", body)
            assert [(call["name"], call["arguments"]) for call in result["tool_calls"]] == [
                ("exec_command", '{"cmd":"cd work && ls -la"}')]
            if stream:  # only the arguments' deltas still hold it (Codex takes the call from the finished item)
                assert [event["type"] for event in events_of(body) if "justification" in json.dumps(event)] == [
                    "response.function_call_arguments.delta"]
            else:
                assert b"justification" not in body
        assert [record["llm_tools"] for record in relay.records(2)] == [{"plain": ["exec_command"]}] * 2
    finally:
        relay.close()


# --- end to end: Codex's requests through the real proxy to a scripted Responses server ---------------------------------

class Model(http.server.BaseHTTPRequestHandler):
    """A model server's /v1/responses: every request recorded, answered with the next of SCRIPT (bytes: a stream when
    they begin with "event:", chunked or - LENGTH - with a Content-Length; else one JSON document)."""
    protocol_version = "HTTP/1.1"
    script, bodies, heads, length = [], [], [], False

    def log_message(self, *args):
        pass

    def do_POST(self):
        Model.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
        Model.heads.append({name.lower(): value for name, value in self.headers.items()})
        data = Model.script.pop(0)
        stream = data.startswith(b"event:")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        if stream and not Model.length:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(data), 300):  # events split across chunks, as a server's buffers do
                piece = data[start:start + 300]
                self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


@pytest.fixture
def model():
    Model.script, Model.bodies, Model.heads, Model.length = [], [], [], False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


KEY = "sk-local-0123456789abcdef"


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("key", [None, KEY])
def test_e2e_codex_calls_an_mcp_tool_of_a_local_model_through_the_proxy(tmp_path, model, key):
    """runs/mock_codex_ns and smoke__codex-hfns__qcn, through the real proxy: Codex's request offers three
    namespaces; the server gets ten functions; the model calls mcp__files__write_file; Codex gets write_file of
    mcp__files, sends its result back with that call in the conversation, and the server gets the flat name again.
    With the server's key added by the proxy as well (the answer is scrubbed of it on the same way back)."""
    relay = live.Relay(tmp_path, model.server_address[1])
    try:
        if key:
            relay.server.config["llm"][0]["key"] = key
        arguments = '{"path":"mock.txt","content":"hello from mock\\n"}'
        Model.script = [responses_stream([("mcp__files__write_file", arguments)], text=f"key {key}" if key else None),
                        responses_stream([("mcp__exec__run_command", '{"command":"cat mock.txt"}')]),
                        responses_stream([], text="MOCK-DONE")]
        head, body = live.dechunk(relay.post("/v1/responses", codex_request()))
        assert b" 200 " in head.split(b"\r\n", 1)[0] and b"chunked" in head.lower()
        assert [tool["name"] for tool in Model.bodies[0]["tools"]] == FLAT
        assert calls_of(body) == [("response.output_item.added", "write_file", "mcp__files"),
                                  ("response.output_item.done", "write_file", "mcp__files"),
                                  ("response.completed", "write_file", "mcp__files")]
        result = live.read_answer("responses", body)
        assert result["tool_calls"][0]["name"] == "write_file" and result["tool_calls"][0]["arguments"] == arguments
        if key:
            assert Model.heads[0]["authorization"] == "Bearer " + key
            assert key.encode() not in body and b"key " + b"*" * len(key) in body
        # Codex's next request: the call as it knows it, the result as input_text parts.
        steps = [("write_file", json.loads(arguments), codex_mcp_output('{"action": "created"}'))]
        head, body = live.dechunk(relay.post("/v1/responses", codex_request(steps)))
        sent = [item for item in Model.bodies[1]["input"] if item["type"] == "function_call"]
        assert [(item["name"], item.get("namespace")) for item in sent] == [("mcp__files__write_file", None)]
        assert calls_of(body)[-1] == ("response.completed", "run_command", "mcp__exec")
        # A text answer passes; the connection log says what the relay did with the tools.
        head, body = live.dechunk(relay.post("/v1/responses", codex_request(steps)))
        assert live.read_answer("responses", body)["text"] == "MOCK-DONE"
        records = relay.records(3)
        assert records[0]["llm_tools"] == {"namespaces": 3, "calls": 0}
        assert records[1]["llm_tools"] == {"namespaces": 3, "calls": 1}
        assert key is None or key not in json.dumps(records)
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("length", ["json", "stream"])
def test_e2e_an_answer_with_a_length_gets_its_new_length_and_the_connection_stays_usable(tmp_path, model, length):
    """An answer that is not chunked (stream: false, or a server that buffers its events): rewritten whole, sent with
    the length it then has; the next request on the same connection works."""
    relay = live.Relay(tmp_path, model.server_address[1])
    try:
        Model.length = True
        item = {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "mcp__files__write_file",
                "arguments": '{"path":"a"}', "status": "completed"}
        answers = [json.dumps({"id": "resp_1", "object": "response", "status": "completed", "model": "m",
                               "output": [item]}).encode() if length == "json" else
                   responses_stream([("mcp__files__write_file", '{"path":"a"}')])] * 2
        Model.script = list(answers)
        import socket
        body = json.dumps(codex_request(stream=length == "stream")).encode()
        request = (b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                   b"Content-Length: %d\r\n\r\n" % len(body) + body)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=20) as sock:
            for number in range(2):
                sock.sendall(request)
                data = b""
                while not live.has_whole_answer(data):
                    chunk = sock.recv(65536)
                    assert chunk, "the proxy closed the connection"
                    data += chunk
                head, _, got = data.partition(b"\r\n\r\n")
                assert b" 200 " in head.split(b"\r\n", 1)[0]
                assert b"content-length: %d" % len(got) in head.lower() and len(got) != len(answers[0])
                found = json.loads(got)["output"] if length == "json" else \
                    [event["item"] for event in events_of(got) if event["type"] == "response.output_item.done"]
                assert [(call["name"], call["namespace"]) for call in found] == [("write_file", "mcp__files")]
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_loop_guard_ends_a_turn_of_repeated_mcp_calls(tmp_path, model):
    """The guard's stop works on the flattened request too: six identical MCP calls, the model calls again, the relay
    answers in its place; the notes in Codex's input_text results (G8) at their places (G5)."""
    relay = live.Relay(tmp_path, model.server_address[1])
    try:
        same = ("read_file", {"path": "logpipe/report.py"}, codex_mcp_output('{"content": "import argparse"}'))
        Model.script = [responses_stream([("mcp__files__read_file", '{"path":"logpipe/report.py"}')])] * 4
        for steps in (3, 4, 5, 6):
            head, body = live.dechunk(relay.post("/v1/responses", codex_request([same] * steps)))
            result = live.read_answer("responses", body)
            if steps < 6:
                assert calls_of(body)[-1] == ("response.completed", "read_file", "mcp__files")
            else:
                assert not result.get("tool_calls") and "mcp__files__read_file" in result["text"] and \
                    "ended" in result["text"]
        outputs = [item["output"] for item in Model.bodies[-1]["input"] if item["type"] == "function_call_output"]
        assert [len(parts) for parts in outputs] == [2, 2, 3, 3, 3, 3]
        assert {part["type"] for parts in outputs for part in parts} == {"input_text"}
        assert extends(Model.bodies[-1], Model.bodies[-2]) and extends(Model.bodies[1], Model.bodies[0])
    finally:
        relay.close()
