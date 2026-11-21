"""The loop guard's failures note for a tool for MCP resources names the request's own MCP function (research round 3,
g9_fail_guess__codex__laguna__a1). Asked to read a file "with the MCP tool read_file of the server files", Laguna-S in
Codex called read_mcp_resource six times with guessed URIs (all "-32602 Invalid request parameters"); the failures
layer's note came at the sixth ("Do not guess further arguments: ... whether another tool does what you need") and the
model gave up: "I don't know how to form a valid uri for this server's read_file" - mcp__files__read_file was offered in
the same request. The note now says what such a tool is not and which function is the server's tool (the sentence of
llm_resource_hint), the same text at the same place in every request."""
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402

rt, llm = live.rt, live.llm


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def guessing(api, count, mcp_function=True):
    document = live.guessing(api, count)
    if mcp_function:
        document["tools"].append(live.shell_tool(api, "mcp__files__read_file", ("path",), ["path"]))
    return document


SENTENCE = ("read_mcp_resource (Only for resources an MCP server publishes; most servers publish none. This does not "
            "call a server's tools and does not read files. A server's tools are functions of their own: tool T of "
            "the MCP server files is the function mcp__files__T - for example mcp__files__read_file. Call that "
            "function directly.)")


@pytest.mark.parametrize("api", ["chat", "responses", "messages"])
def test_regression_the_failures_note_of_a_resource_tool_names_the_servers_function(api):
    document = guessing(api, 6)
    done = rt.llm_request(live.entry("lmstudio", mcp=["files"]), api, document)
    assert done["loop"]["kind"] == "failures"
    note = str(live.results_of(document, api)[5])
    assert "read_mcp_resource has failed 6 times in a row" in note and SENTENCE in note


def test_the_same_note_in_the_next_request_and_another_tools_note_as_it_was():
    first, second = guessing("chat", 6), guessing("chat", 7)
    for document in (first, second):
        rt.llm_request(live.entry("lmstudio", mcp=["files"]), "chat", document)
    assert second["messages"][:len(first["messages"])] == first["messages"]
    # a tool that is no resource tool: the note as before, no sentence
    plain = live.guessing("chat", 6, name="fetch_page")
    rt.llm_request(live.entry("lmstudio"), "chat", plain)
    note = live.results_of(plain, "chat")[5]
    assert rt.llm_failure_note_text("fetch_page", 6) in note and "Only for resources" not in note


def test_unit_the_note_with_and_without_a_hint():
    base = rt.llm_failure_note_text("read_mcp_resource", 6)
    assert rt.llm_failure_note_text("read_mcp_resource", 6, "x (y.)") == base + " x (y.)"
    assert rt.llm_failure_note_text("read_mcp_resource", 6, "") == base


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_server_gets_the_note_with_the_function(tmp_path, scripted):
    relay = live.Relay(tmp_path, scripted.server_address[1])
    try:
        head, _ = live.dechunk(relay.post(live.PATHS["chat"], {**guessing("chat", 6), "stream": True}))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
    finally:
        relay.close()
    got = [m["content"] for m in live.Scripted.bodies[-1]["messages"] if m.get("role") == "tool"]
    assert SENTENCE in got[5] and json.dumps(got).count("for example mcp__files__read_file") == 1


scripted = live.scripted
