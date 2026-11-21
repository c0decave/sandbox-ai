"""A text note of the agent between two tool steps, folded into the tool result before it (local-model research,
round 2, focus 3: /work/research-llm/round2.md).

Measured with LM Studio and Qwen3.6-35B-A3B (round2/evidence/cache_shapes.txt): Qwen3's chat template keeps the
reasoning of the assistant's turns only after the last user message that is not a tool result. When an agent puts
a text of its own between two tool steps as a user message - Claude Code's reminders beside a tool result, or in a
message of their own - the template drops the reasoning of every step before it: the prompt changes near its
beginning, and the server reads it all again (round 1, t2_debug__claude__q36__r2, requests 68 and 76: 16,384 of
86,413 and 95,385 tokens from the cache, 92.8 s and 108.9 s; every other step 0.6-0.9 s). Claude Code 2.1.325 sends
its "changed on disk" note as a message of role system after the result, and LM Studio moves such a message into
the system prompt at the top: the whole prompt after the tools changes, a full prefill. The relay now puts such a
text into the tool result it follows: the model reads the same words, and the prompt extends the one before.

Unit tests of runtime.llm_fold_notes, llm_request with it, and end to end through the real proxy."""
import copy
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402 - its Relay (the real proxy) and answer readers

rt, llm = live.rt, live.llm

REMINDER = ("<system-reminder>\nNote: /work/logpipe/parser.py was modified, either by the user or by a linter. "
            "This change was intentional, so make sure to take it into account.\n</system-reminder>")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def claude_step(k, note=None, own_message=False):
    """One Claude Code step on the Messages API: thinking + a Bash call, then the result - with NOTE beside the result
    (Claude Code's way) or in a user message of its own."""
    steps = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": f"Step {k}: look at part {k}.", "signature": ""},
        {"type": "tool_use", "id": f"toolu_{k}", "name": "Bash", "input": {"command": f"sed -n {k}p data.txt"}}]}]
    result = {"type": "tool_result", "tool_use_id": f"toolu_{k}", "content": f"line {k}"}
    if note and not own_message:
        steps.append({"role": "user", "content": [result, {"type": "text", "text": note}]})
    else:
        steps.append({"role": "user", "content": [result]})
        if note:
            steps.append({"role": "user", "content": [{"type": "text", "text": note}]})
    return steps


def claude_request(steps):
    return {"model": "qwen3.6-35b-a3b", "max_tokens": 32768, "system": [{"type": "text", "text": "You are a tool."}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Count the warnings."}]}] + steps}


def chat_step(k, note=None):
    """One OpenCode-like step on the chat API: reasoning + a call, the tool's message, and NOTE as a user message."""
    steps = [{"role": "assistant", "content": "", "reasoning_content": f"Step {k}.",
              "tool_calls": [{"id": f"call_{k}", "type": "function",
                              "function": {"name": "bash", "arguments": json.dumps({"command": f"sed -n {k}p x"})}}]},
             {"role": "tool", "tool_call_id": f"call_{k}", "content": f"line {k}"}]
    if note:
        steps.append({"role": "user", "content": note})
    return steps


def chat_request(steps):
    return {"model": "qwen3.6-35b-a3b", "messages": [{"role": "system", "content": "You are a tool."},
                                                     {"role": "user", "content": "Count the warnings."}] + steps}


# --- what is folded -------------------------------------------------------------------------------------------------

def test_regression_claude_codes_reminder_beside_a_result_goes_into_the_result():
    document = claude_request(claude_step(0) + claude_step(1, REMINDER))
    assert rt.llm_fold_notes(document, "messages") == 1
    last = document["messages"][-1]
    assert last["role"] == "user" and [b["type"] for b in last["content"]] == ["tool_result"]
    assert last["content"][0]["content"] == "line 1\n\n" + REMINDER
    assert last["content"][0]["tool_use_id"] == "toolu_1"


def test_regression_a_reminder_in_a_message_of_its_own_goes_into_the_result_before_it():
    document = claude_request(claude_step(0) + claude_step(1, REMINDER, own_message=True))
    assert rt.llm_fold_notes(document, "messages") == 1
    assert len(document["messages"]) == 5  # the task, two steps of two messages: the note's message is gone
    assert document["messages"][-1]["content"][0]["content"] == "line 1\n\n" + REMINDER


def test_regression_a_user_message_after_a_tool_message_on_the_chat_api():
    document = chat_request(chat_step(0) + chat_step(1, REMINDER))
    assert rt.llm_fold_notes(document, "chat") == 1
    assert [m["role"] for m in document["messages"]] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert document["messages"][-1]["content"] == "line 1\n\n" + REMINDER


CHANGED = ("Note: /work/app/version.py changed on disk since you last read it. That's usually deliberate, so take it "
           "as the current state rather than reverting it. Here are the relevant changes (shown with line numbers):\n"
           "2\tVERSION = \"1.4.1\"")


def test_regression_claude_codes_changed_on_disk_note_a_system_message_goes_into_the_result():
    """Claude Code 2.1.325 (mock_w2__claude__mock__x2): a file it edited and a shell command then changed - the note
    comes as a message of role system after the tool result; LM Studio's template took it as a new turn."""
    document = claude_request(claude_step(0) + claude_step(1))
    document["messages"].append({"role": "system", "content": [{"type": "text", "text": CHANGED,
                                                                "cache_control": {"type": "ephemeral"}}]})
    assert rt.llm_fold_notes(document, "messages") == 1
    assert document["messages"][-1]["role"] == "user"
    assert document["messages"][-1]["content"][0]["content"] == "line 1\n\n" + CHANGED
    # the conversation's own system messages before any tool result stay (Claude Code's environment text)
    first = claude_request(claude_step(0))
    first["messages"].insert(1, {"role": "system", "content": [{"type": "text", "text": "# Environment"}]})
    before = copy.deepcopy(first)
    assert rt.llm_fold_notes(first, "messages") == 0 and first == before


def test_a_system_message_after_a_tool_message_on_the_chat_api():
    document = chat_request(chat_step(0))
    document["messages"].append({"role": "system", "content": CHANGED})
    assert rt.llm_fold_notes(document, "chat") == 1
    assert document["messages"][-1] == {"role": "tool", "tool_call_id": "call_0", "content": "line 0\n\n" + CHANGED}


def test_a_result_that_is_a_list_of_parts_gets_a_text_part():
    document = claude_request(claude_step(0, REMINDER))
    document["messages"][-1]["content"][0]["content"] = [{"type": "text", "text": "line 0"}]
    assert rt.llm_fold_notes(document, "messages") == 1
    assert document["messages"][-1]["content"][0]["content"] == [{"type": "text", "text": "line 0"},
                                                                 {"type": "text", "text": REMINDER}]
    chat = chat_request(chat_step(0, REMINDER))
    chat["messages"][-2]["content"] = [{"type": "text", "text": "line 0"}]
    assert rt.llm_fold_notes(chat, "chat") == 1
    assert chat["messages"][-1]["content"] == [{"type": "text", "text": "line 0"}, {"type": "text", "text": REMINDER}]


def test_two_texts_and_a_string_content():
    document = claude_request(claude_step(0))
    document["messages"][-1]["content"].append({"type": "text", "text": REMINDER})
    document["messages"].append({"role": "user", "content": "Repeated tool call detected: try another way."})
    assert rt.llm_fold_notes(document, "messages") == 2
    assert document["messages"][-1]["content"][0]["content"] == \
        "line 0\n\n" + REMINDER + "\n\nRepeated tool call detected: try another way."


def test_every_request_of_the_conversation_gets_the_same_and_the_next_one_extends_it():
    earlier = claude_request(claude_step(0) + claude_step(1, REMINDER))
    later = claude_request(claude_step(0) + claude_step(1, REMINDER) + claude_step(2))
    rt.llm_fold_notes(earlier, "messages")
    rt.llm_fold_notes(later, "messages")
    assert later["messages"][:len(earlier["messages"])] == earlier["messages"]


# --- what stays as it came ------------------------------------------------------------------------------------------

def test_the_humans_messages_stay():
    # what the human typed while the agent worked (beside a result, or a message of its own after it): a message of
    # the human's, not the tool's output - it stays where it was, the prompt cache then as before
    typed = claude_request(claude_step(0, "Use the UTC offset too.") + claude_step(1, "Stop.", own_message=True))
    chat_typed = chat_request(chat_step(0, "Use the UTC offset too."))
    mixed = claude_request(claude_step(0))
    mixed["messages"].append({"role": "user", "content": [{"type": "text", "text": REMINDER},
                                                          {"type": "text", "text": "and check the tests"}]})
    for api, value in (("messages", typed), ("chat", chat_typed), ("messages", mixed)):
        before = copy.deepcopy(value)
        assert rt.llm_fold_notes(value, api) == 0 and value == before
    # the task; a new message of the human's after an answer (not after a tool result); an image beside a result
    document = claude_request(claude_step(0) + [{"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
                                                {"role": "user", "content": "Now the errors too."}])
    image = claude_request(claude_step(0))
    image["messages"][-1]["content"].append({"type": "image", "source": {"type": "base64", "data": "AAAA"}})
    image["messages"].append({"role": "user", "content": [{"type": "image", "source": {"type": "base64",
                                                                                         "data": "AAAA"}}]})
    chat = chat_request(chat_step(0) + [{"role": "assistant", "content": "Done."},
                                        {"role": "user", "content": "More."}])
    for api, value in (("messages", document), ("messages", image), ("chat", chat)):
        before = copy.deepcopy(value)
        assert rt.llm_fold_notes(value, api) == 0 and value == before


def test_odd_documents_and_other_apis():
    for api, value in (("messages", {}), ("messages", None), ("messages", {"messages": "x"}),
                       ("chat", {"messages": [None, 3, {"role": "tool"}, {"role": "user", "content": 7}]}),
                       ("responses", {"input": [{"type": "function_call_output", "call_id": "c", "output": "x"},
                                                {"type": "message", "role": "user", "content": "y"}]}),
                       ("completions", {"prompt": "x"})):
        before = copy.deepcopy(value)
        assert rt.llm_fold_notes(value, api) == 0 and value == before


# --- in the relay ---------------------------------------------------------------------------------------------------

def test_llm_request_folds_and_says_so_and_the_traffic_log_lists_it():
    entry = live.entry("lmstudio")
    document = claude_request(claude_step(0) + claude_step(1, REMINDER, own_message=True))
    before = rt.llm_snapshot(document)
    done = rt.llm_request(entry, "messages", document)
    assert done["folded"] == 1
    assert document["messages"][-1]["content"][0]["content"].endswith(REMINDER)
    changes = rt.llm_changes(before, document, done)
    assert any(c.get("field") == "messages" and c.get("why") == "local server" and "tool result" in c.get("note", "")
               for c in changes)


def test_the_loop_guard_still_sees_the_turn_as_the_agent_sent_it():
    # Claude Code's reminder in a message of its own is a turn of the human's for the guard (llm_human_turns): the
    # fold comes after the guard, so its counting is what it was
    steps = []
    for k in range(4):
        steps += claude_step(k)
        steps[-2]["content"][1]["input"] = {"command": "python3 -m pytest -q"}
        steps[-1]["content"][0]["content"] = "1 failed"
    document = claude_request(steps)
    reference = claude_request(copy.deepcopy(steps))
    done = rt.llm_request(live.entry("lmstudio"), "messages", document)
    expected = rt.llm_request(live.entry("lmstudio"), "messages", reference)
    assert done["loop"] == expected["loop"] and done.get("folded") is None


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("api", ["messages", "chat"])
def test_e2e_the_server_gets_the_note_inside_the_result(tmp_path, scripted, api):
    document = claude_request(claude_step(0) + claude_step(1, REMINDER, own_message=True)) if api == "messages" else \
        chat_request(chat_step(0) + chat_step(1, REMINDER))
    relay = live.Relay(tmp_path, scripted.server_address[1])
    try:
        head, _ = live.dechunk(relay.post(live.PATHS[api], {**document, "stream": True}))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
    finally:
        relay.close()
    got = live.Scripted.bodies[-1]["messages"]
    if api == "messages":
        assert [m["role"] for m in got] == ["user", "assistant", "user", "assistant", "user"]
        assert got[-1]["content"][0]["content"] == "line 1\n\n" + REMINDER
    else:
        assert [m["role"] for m in got] == ["system", "user", "assistant", "tool", "assistant", "tool"]
        assert got[-1]["content"] == "line 1\n\n" + REMINDER


scripted = live.scripted
