"""OpenCode 2 asks the model before its MCP servers are connected, and tells it about them later: from the request
after the connection on, the servers' tools are functions of the request (files_read_file ...), and a user message
`<system-update>` with the servers' `<mcp_instructions>` follows the tool results of the step (OpenCode 2 2.2.x,
escaped inside it: `&lt;server name="files"&gt;`). Research round 3 (smoke__opencode2__laguna__hint1, and round 1's
r1/r2/xcap1/xmcp1): Laguna-S planned without MCP tools at request 1, then looked for the files server inside code mode
(`execute`, `tools.opencode.list_mcp_resources` -> no resources), concluded "no MCP servers are registered in this
session" and read the file with its own tool - 0 MCP calls in 5 of 5 runs, though files_read_file was offered from
request 3 on. The relay now (1) names each server's functions in its entry of the notice ("call them directly, not
inside execute") and (2) puts the notice into the tool result before it, like the other harnesses' notes
(runtime.llm_fold_notes: a user text between two tool steps makes Qwen3's template drop the earlier reasoning)."""
import copy
import shutil
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402

rt = live.rt

NOTICE = ("<system-update>\n&lt;mcp_instructions&gt;\n  &lt;server name=\"exec\"&gt;\n    Runs commands for you in the "
          "project folder.\n  &lt;/server&gt;\n  &lt;server name=\"files\"&gt;\n    Reads and writes files in the "
          "project folder.\n  &lt;/server&gt;\n&lt;/mcp_instructions&gt;\n</system-update>")


def chat_tool(name):
    return {"type": "function", "function": {"name": name, "description": f"{name}.",
                                             "parameters": {"type": "object", "properties": {}, "required": []}}}


def opencode2_request(notice=NOTICE, mcp=True):
    tools = [chat_tool(name) for name in ("edit", "read", "shell", "execute")]
    if mcp:
        tools += [chat_tool("exec_run_command"), chat_tool("exec_run_tests"), chat_tool("files_read_file"),
                  chat_tool("files_stat")]
    messages = [{"role": "system", "content": "You are an AI agent running in OpenCode."},
                {"role": "user", "content": "Read notes/secret.txt with the MCP tool read_file of the server files."},
                {"role": "assistant", "content": "Looking around.", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{\"command\":\"ls\"}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "app\nnotes"}]
    if notice is not None:
        messages.append({"role": "user", "content": notice})
    return {"model": "poolside/laguna-s-2.1", "stream": True, "tools": tools, "messages": messages}


def test_regression_opencode2s_mcp_notice_names_each_servers_functions_and_goes_into_the_result_before_it():
    document = opencode2_request()
    rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "chat", document)
    messages = document["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"], \
        "the notice is no user turn of its own any more"
    text = messages[-1]["content"]
    assert text.startswith("app\nnotes\n\n<system-update>")
    assert ("&lt;server name=\"exec\"&gt;\n    Its tools are functions of this request: tool T of the MCP server exec "
            "is the function exec_T - for example exec_run_command. Call them directly, not inside execute.\n"
            "    Runs commands") in text
    assert ("&lt;server name=\"files\"&gt;\n    Its tools are functions of this request: tool T of the MCP server "
            "files is the function files_T - for example files_read_file. Call them directly, not inside execute.\n"
            "    Reads and writes") in text


def test_mcp_notice_unescaped_and_the_same_text_in_every_request():
    plain = NOTICE.replace("&lt;", "<").replace("&gt;", ">")
    first, again = opencode2_request(plain), opencode2_request(plain)
    for document in (first, again):
        rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "chat", document)
    assert first == again
    assert "<server name=\"files\">\n    Its tools are functions of this request: tool T of the MCP server files is " \
           "the function files_T" in first["messages"][-1]["content"]


def test_mcp_notice_unit_a_server_without_functions_and_one_told_already_stay():
    others = ["edit", "files_read_file", "files_stat"]
    text, told = rt.llm_mcp_notice_text(NOTICE, others)
    assert told == ["files"]
    assert "server name=\"exec\"&gt;\n    Runs commands" in text  # no function of exec in the request: nothing said
    assert rt.llm_mcp_notice_text(text, others) == (text, []), "said once"
    assert rt.llm_mcp_notice_text("no notice here", others) == ("no notice here", [])
    assert rt.llm_mcp_notice_text(NOTICE, []) == (NOTICE, [])


def test_a_notice_without_functions_yet_and_a_humans_text_stay_where_they_are():
    # before the connection: no function of the servers in the request - the notice is folded, nothing named
    document = opencode2_request(mcp=False)
    rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "chat", document)
    assert document["messages"][-1]["content"] == "app\nnotes\n\n" + NOTICE
    # a text of the human's typed while the agent worked stays a message of its own, untouched
    human = opencode2_request("Also check the server named files, please.")
    expected = copy.deepcopy(human)
    rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "chat", human)
    assert human["messages"] == expected["messages"]


def test_the_traffic_log_says_what_was_named():
    document = opencode2_request()
    done = rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "chat", document)
    assert done["tools"]["notice"] == ["exec", "files"]
    notes = [change["note"] for change in rt.llm_changes(opencode2_request(), document, done)
             if change.get("why") == "local server"]
    assert any("notice of the MCP servers names the functions of exec, files" in note for note in notes)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_server_gets_the_notice_with_the_functions_inside_the_result(tmp_path, scripted):
    relay = live.Relay(tmp_path, scripted.server_address[1])
    try:
        head, _ = live.dechunk(relay.post(live.PATHS["chat"], opencode2_request()))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
    finally:
        relay.close()
    got = live.Scripted.bodies[-1]["messages"]
    assert [m["role"] for m in got] == ["system", "user", "assistant", "tool"]
    assert got[-1]["content"].startswith("app\nnotes\n\n<system-update>")
    assert "is the function files_T - for example files_read_file. Call them directly, not inside execute." in \
        got[-1]["content"]


scripted = live.scripted
