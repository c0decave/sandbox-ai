"""OpenCode's session title with a local model: the start's name, never a model's answer.

The local-model research's round 3 (research-llm/round3.md): OpenCode asks the model for a session title beside the
first real request - OpenCode 1.19.5 with max_tokens 32000 -, and at LM Studio's parallel 1 the real request waits
for it. Seed-OSS-36B (it thinks without a budget) spent 462 and 544 s on the title (9,466 and 10,819 tokens),
Qwen3.6 18 s. The user's decision (2026-11-17): "simply give it the session name the session runs under".

Neither pinned OpenCode has a setting for a fixed title (OpenCode 1.19.5's agent "title" takes a prompt, a model or
disable - disabled, the title stays "New session - DATE"; OpenCode 2.2.5's only a plugin's hook). So the connection
proxy answers OpenCode's title request itself, in the model's place, with the name the start runs under (sandbox-ai
list): nothing goes to the server. It knows the request by OpenCode's own title prompt (OpenCode 1: "You are a title
generator. You output ONLY a thread title." and "Generate a title for this conversation:"; OpenCode 2: "You name
conversations so the user can find them later ..." and "Reply with only the title."), a request without tools; any
other request - one that only quotes the prompt, a title agent with your own prompt (agent.title.prompt in your
OpenCode configuration), anything with tools - goes to the model as it came. Your own setting wins: OpenCode's
agent.title (your prompt, your model, disable) is never touched.

Unit tests of runtime.llm_title_request, llm.proxy_entry's "title", and end to end through the real proxy
(ProxyServer) with a mock model server."""
import argparse
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402 - its Relay (the real proxy) and its scripted model

rt, llm = live.rt, live.llm
NAME = "brisk-otter-lantern"

# The requests as the research's runs captured them (round3/runs/smoke__opencode__seedl__a1, OpenCode 1.19.5;
# smoke__opencode2__laguna__notice2, OpenCode 2.2.5), the prompts' first lines and frames verbatim.
OC1_SYSTEM = ("You are a title generator. You output ONLY a thread title. Nothing else.\n\n<task>\nGenerate a brief "
              "title that would help the user find this conversation later.\n</task>")
OC2_SYSTEM = ("You name conversations so the user can find them later in a list of past sessions.\n\nYou will "
              "receive the first message of a conversation, or for an ongoing conversation its original request and "
              "recent messages. Reply with a title for it and nothing else.")
TASK = "Do these three small things in this order:\n1. Run the tests with your own shell tool: python3 -m pytest -q"


def oc1_title(stream=True):
    return {"model": "seed-oss-36b-instruct-mlx", "max_tokens": 32000, "stream": stream,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": OC1_SYSTEM},
                         {"role": "user", "content": "Generate a title for this conversation:\n"},
                         {"role": "user", "content": TASK}]}


def oc2_title(stream=True, ongoing=False):
    framed = (f"Here is the conversation so far:\n\n<conversation>\nOriginal request:\n{TASK}\n</conversation>\n\n"
              "Reply with only the title." if ongoing else
              f"Here is the first message of the conversation:\n\n<message>\n{TASK}\n</message>\n\n"
              "Reply with only the title.")
    return {"model": "poolside/laguna-s-2.1", "stream": stream, "stream_options": {"include_usage": True},
            "reasoning_effort": "low",
            "messages": [{"role": "system", "content": OC2_SYSTEM}, {"role": "user", "content": framed}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


@pytest.mark.parametrize("document", [oc1_title(), oc1_title(False), oc2_title(), oc2_title(False),
                                      oc2_title(ongoing=True)])
def test_opencodes_title_requests_are_recognised(document):
    assert rt.llm_title_request(document) is True


def test_a_system_prompt_given_as_parts_is_read_too():
    document = oc1_title()
    document["messages"][0]["content"] = [{"type": "text", "text": OC1_SYSTEM}]
    assert rt.llm_title_request(document) is True


def test_real_requests_are_never_taken_for_one():
    with_tools = {**oc1_title(), "tools": [{"type": "function", "function": {"name": "bash"}}]}
    quoted = {"model": "m", "messages": [{"role": "system", "content": "You are opencode, an agent."},
                                         {"role": "user", "content": OC1_SYSTEM + "\nGenerate a title for this "
                                                                                  "conversation:\n"}]}
    own_prompt = oc1_title()
    own_prompt["messages"][0]["content"] = "Name this chat in three words."  # your agent.title.prompt
    no_frame = oc1_title()
    no_frame["messages"][1]["content"] = "What is the title of this book?"
    oc2_no_frame = oc2_title()
    oc2_no_frame["messages"][1]["content"] = "Here is the first message of the conversation: fix the bug."
    for document in (with_tools, quoted, own_prompt, no_frame, oc2_no_frame, {}, None, {"messages": "x"},
                     {"messages": [None, 3]}, {"input": oc1_title()["messages"]}):
        assert rt.llm_title_request(document) is False, document


def test_the_start_gives_the_proxy_its_name(tmp_path):
    llm.main(["add", "mac", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--model", "m"],
             out=lambda line: None)
    for agent in ("opencode", "opencode2", "claude"):
        a = argparse.Namespace(llm="mac", agent=agent, llm_param=[])
        a.llm_addresses = ["127.0.0.1"]
        assert llm.proxy_entry(a, title=NAME)[0]["title"] == NAME
    a = argparse.Namespace(llm="mac", agent="opencode", llm_param=[], llm_addresses=["127.0.0.1"])
    assert "title" not in llm.proxy_entry(a)[0], "no name known (an MCP server's start): OpenCode asks the model"
    odd = llm.proxy_entry(a, title="evil\x1b[31mname\n" + "x" * 300)[0]["title"]
    assert odd.startswith("evil") and odd.isprintable() and len(odd) == 100, \
        "printable, at most 100 characters (OpenCode cuts a title there)"
    assert "title" not in llm.proxy_entry(a, title=" \x1b ")[0]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("make", [oc1_title, oc2_title])
@pytest.mark.parametrize("stream", [True, False])
def test_e2e_the_proxy_answers_the_title_request_with_the_starts_name(tmp_path, scripted, make, stream):
    relay = live.Relay(tmp_path, scripted.server_address[1])
    relay.server.config["llm"][0]["title"] = NAME
    try:
        head, body = live.dechunk(relay.post(live.PATHS["chat"], make(stream)))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
        answer = live.read_answer("chat", body)
        assert answer["text"] == NAME and not answer.get("tool_calls") and answer["streamed"] == stream
        assert live.Scripted.bodies == [], "nothing went to the model"
        record = relay.records(1)[-1]
        assert record["llm_title"] == NAME and record["status"] == 200
        # The real request after it goes to the model as it came.
        real = {**make(stream), "tools": [{"type": "function", "function": {"name": "bash", "parameters": {
            "type": "object", "properties": {}, "required": []}}}]}
        relay.post(live.PATHS["chat"], real)
        assert len(live.Scripted.bodies) == 1
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_without_a_name_the_title_request_goes_to_the_model(tmp_path, scripted):
    live.Scripted.mode = "text"
    relay = live.Relay(tmp_path, scripted.server_address[1])
    try:
        relay.post(live.PATHS["chat"], oc1_title())
        assert len(live.Scripted.bodies) == 1
    finally:
        relay.close()


scripted = live.scripted
