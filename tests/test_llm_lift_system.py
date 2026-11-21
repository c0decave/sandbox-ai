"""A system message before the model's first answer goes into the system prompt of a local model's request.

Claude Code 2.1.325 sends its environment ("# Environment\\nYou have been invoked in the following environment ...")
as a message of role system after the first user message. LM Studio renders the conversation with the model's own
chat template, and a template that allows a system message only at the beginning refuses every such request:
KAT-Coder-V2.5-Dev (Qwen3.6-35B-A3B post-train, GGUF) on LM Studio answered every request of Claude Code with HTTP 500
"System message must be at the beginning" - ten retries, then Claude Code gave up (the local-model research's round 4,
runs/smoke__claude__kat__a1). The connection proxy now puts the text of such a system message (one before any answer
of the assistant's: the start of the conversation, the same in every request of it) into the request's system prompt -
where LM Studio puts it for the templates that render it at all - so that the prompt is the same from request to
request and no template sees a system message in the middle. Later system messages (Claude Code's notes between tool
steps) are folded into the tool result before them as before (llm_fold_notes); the human's messages are never moved.

Unit tests of llm_lift_system, llm_request's record, the round-4 request shape, and end to end through the real
ProxyServer with a scripted model whose "template" refuses a system message that is not the first."""
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
import test_llm_loop_live as live  # noqa: E402

rt, llm = live.rt, live.llm
ENV = "# Environment\nYou have been invoked in the following environment: \n - Primary working directory: /w"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def claude_request(steps=0):
    """Claude Code 2.1.325's first request (and STEPS tool steps after it), as the research's relay recorded it."""
    document = {"model": "kat-coder-v2.5-dev", "max_tokens": 16384, "stream": True,
                "system": [{"type": "text", "text": "You are a Claude agent.", "cache_control": {"type": "ephemeral"}},
                           {"type": "text", "text": "You are an agent working with the user."}],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nRules\n"
                                                                                    "</system-reminder>"},
                                                          {"type": "text", "text": "Fix the bug."}]},
                             {"role": "system", "content": [{"type": "text", "text": ENV}]}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {}, "required": []}}]}
    for number in range(steps):
        document["messages"] += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{number}", "name": "Bash",
                                               "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{number}", "content": "a.py"}]}]
    return document


def roles(document):
    return [m.get("role") for m in document["messages"]]


# --- the function ----------------------------------------------------------------------------------------------------

def test_unit_messages_the_environment_goes_into_the_system_prompt():
    document = claude_request(steps=2)
    assert rt.llm_lift_system(document, "messages") == 1
    assert roles(document) == ["user", "assistant", "user", "assistant", "user"]
    assert document["system"][-1] == {"type": "text", "text": ENV}
    assert document["system"][0]["cache_control"] == {"type": "ephemeral"}  # the agent's own blocks as they were
    assert rt.llm_lift_system(document, "messages") == 0  # nothing left to lift


def test_unit_a_system_prompt_as_a_string_and_none():
    document = claude_request()
    document["system"] = "You are a Claude agent."
    rt.llm_lift_system(document, "messages")
    assert document["system"] == "You are a Claude agent.\n\n" + ENV
    document = claude_request()
    del document["system"]
    rt.llm_lift_system(document, "messages")
    assert document["system"] == [{"type": "text", "text": ENV}]


def test_unit_chat_the_system_message_after_the_first_user_message_joins_the_first():
    document = {"messages": [{"role": "system", "content": "You are a helper."}, {"role": "user", "content": "hi"},
                             {"role": "system", "content": ENV}, {"role": "assistant", "content": "ok"}]}
    assert rt.llm_lift_system(document, "chat") == 1
    assert document["messages"] == [{"role": "system", "content": "You are a helper.\n\n" + ENV},
                                    {"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    lone = {"messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": ENV}]}
    rt.llm_lift_system(lone, "chat")
    assert lone["messages"] == [{"role": "system", "content": ENV}, {"role": "user", "content": "hi"}]


def codex_request():
    """Codex 0.166's first request (runs/smoke__codex__kat__a1): two developer messages, then the user's context."""
    def message(role, text):
        return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
    return {"model": "kat-coder-v2.5-dev", "stream": True, "input": [
        message("developer", "You are a coding agent running in the Codex CLI."),
        message("developer", "<skills_instructions>\n## Skills\n</skills_instructions>"),
        message("user", "<environment_context>\n  <cwd>/w</cwd>\n</environment_context>"),
        message("user", "Fix the bug.")]}


def test_unit_responses_codexs_developer_messages_become_one():
    """KAT's template refused Codex's requests the same way (smoke__codex__kat__a1: the second developer message is a
    second system message): the later ones join the first."""
    document = codex_request()
    assert rt.llm_lift_system(document, "responses") == 1
    assert [item["role"] for item in document["input"]] == ["developer", "user", "user"]
    assert [part["text"] for part in document["input"][0]["content"]] == [
        "You are a coding agent running in the Codex CLI.", "<skills_instructions>\n## Skills\n</skills_instructions>"]
    with_instructions = codex_request()
    with_instructions["instructions"] = "Be brief."
    assert rt.llm_lift_system(with_instructions, "responses") == 2
    assert with_instructions["instructions"].startswith("Be brief.\n\nYou are a coding agent") and \
        [item["role"] for item in with_instructions["input"]] == ["user", "user"]
    later = codex_request()
    later["input"] = later["input"][2:] + [{"type": "function_call", "name": "exec_command", "arguments": "{}",
                                           "call_id": "c"}, later["input"][1]]
    assert rt.llm_lift_system(later, "responses") == 0  # after the model's first output: not the start


def test_what_stays_where_it_is():
    """A system message after the assistant's first answer (the fold's business), one with an image, the human's
    messages, the system message already first, the Responses API, odd documents: unchanged."""
    later = claude_request(steps=1)
    later["messages"].append({"role": "system", "content": [{"type": "text", "text": "changed on disk"}]})
    later["messages"].pop(1)  # no system message before the first answer
    before = copy.deepcopy(later)
    assert rt.llm_lift_system(later, "messages") == 0 and later == before
    image = claude_request()
    image["messages"][1]["content"].append({"type": "image", "source": {"type": "base64", "data": "x"}})
    before = copy.deepcopy(image)
    assert rt.llm_lift_system(image, "messages") == 0 and image == before
    first = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]}
    assert rt.llm_lift_system(first, "chat") == 0
    for document, api in ((None, "chat"), ({}, "messages"), ({"messages": "x"}, "chat"), (claude_request(), "responses"),
                          ({"messages": [None, 3]}, "messages"), ({"input": "x"}, "responses"),
                          ({"input": [None, {"type": "message", "role": "developer", "content": 5}]}, "responses")):
        assert rt.llm_lift_system(document, api) == 0


def test_llm_request_lifts_it_and_says_so():
    document = claude_request(steps=1)
    done = rt.llm_request(live.entry("lmstudio"), "messages", document)
    assert done["lifted"] == 1 and "system" not in roles(document)


def test_regression_round4_kat_claude_smoke_shape():
    """smoke__claude__kat__a1: request 1 had the system message second; with it lifted the conversation starts with
    the user's message and nothing of role system follows (what the template asks)."""
    document = claude_request()
    rt.llm_request(live.entry("lmstudio"), "messages", document)
    assert roles(document) == ["user"] and any(block.get("text") == ENV for block in document["system"])


# --- end to end: a scripted "template" that refuses a system message in the middle ---------------------------------

class Strict(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bodies = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Strict.bodies.append(body)
        middle = [m for m in body.get("messages", [])[1:] if isinstance(m, dict) and m.get("role") == "system"]
        if middle:  # as KAT-Coder's chat template on LM Studio
            data = json.dumps({"type": "error", "error": {"type": "api_error", "message":
                               "Error rendering prompt: System message must be at the beginning."}}).encode()
            self.send_response(500)
        else:
            data = rt.llm_stop_answer("messages", "m", "ok", False)
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def strict():
    Strict.bodies = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Strict)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_claude_codes_first_request_is_rendered(tmp_path, strict):
    relay = live.Relay(tmp_path, strict.server_address[1])
    try:
        document = claude_request()
        document["stream"] = False
        answer = relay.post(live.PATHS["messages"], document)
        assert b" 200 " in answer.split(b"\r\n", 1)[0]
        sent = Strict.bodies[-1]
        assert [m["role"] for m in sent["messages"]] == ["user"]
        assert sent["system"][-1]["text"] == ENV and sent["system"][0]["text"] == "You are a Claude agent."
        # the next request of the conversation: the same prompt up to its new part (the cache holds)
        document = claude_request(steps=1)
        document["stream"] = False
        relay.post(live.PATHS["messages"], document)
        second = Strict.bodies[-1]
        assert second["system"] == sent["system"] and second["messages"][0] == sent["messages"][0]
    finally:
        relay.close()
