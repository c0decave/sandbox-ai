"""Claude Code's tools that local models never use are left out of their requests (--llm-tools, configurable).

The local-model research measured it: Claude Code 2.1.325 sends 21 tools, 58,660 characters of JSON (~10k tokens
in every request: research-llm/round2.md); in every Claude Code run of rounds 1-3 the models called Bash (1,977
times), Read (558), Edit (195), Write (27), the MCP servers' tools and Claude Code's MCP resource tools - never Agent,
CronCreate/CronDelete/CronList, EnterWorktree/ExitWorktree, ListAgents, Monitor, NotebookEdit, ReportFindings,
ScheduleWakeup, SendMessage, Skill, TaskStop, WebFetch, WebSearch or Workflow. And Workflow's schema has a property
without a type, which Seed-OSS-36B's chat template refuses: every Claude Code request failed with HTTP 500 (round 3,
t2n_debug__claude__seedl__fix4). The user's decision (2026-11-17): "1 + configurable".

So for an --llm start the connection proxy leaves out of Claude Code's requests (its User-Agent claude-cli/, the
Messages API) every tool but the core ones - Bash, Read, Edit, Write, Glob, Grep, TodoWrite - and every MCP tool
(mcp__SERVER__TOOL and Claude Code's MCP resource tools). --llm-tools all keeps every tool, --llm-tools core is the
default, --llm-tools core,WebFetch adds some, --llm-tools Bash,Read names the built-in ones exactly; a model profile
may say the same ("tools"), the start's option wins. A model that calls a tool that was left out all the same does not
silently get another tool: its call reaches Claude Code under a name Claude Code does not know (NAME plus a mark), so
Claude Code answers it with an error; in every later request the proxy gives the call its name back and puts sandbox-
ai's own error in its result - which tool, why, and how to offer it.

Unit tests of llm.parse_tools / start_tools / proxy_params, runtime.llm_hide_tools / llm_hidden_results and the
answer's rewrite (LlmUsageAnswer), and end to end through the real proxy (ProxyServer) with a mock model."""
import argparse
import http.server
import json
from pathlib import Path
import shutil
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402 - its Relay (the real proxy)

rt, llm = live.rt, live.llm
mp = live.load("sandbox_model_profiles_tools", "sandbox/model_profiles.py")
CLAUDE_UA = "claude-cli/2.1.325 (external, sdk-cli)"
# Claude Code 2.1.325's 21 tools (round 3's request bodies), with the MCP servers' and its MCP resource tools.
BUILTIN = ["Agent", "Bash", "CronCreate", "CronDelete", "CronList", "Edit", "EnterWorktree", "ExitWorktree",
           "ListAgents", "Monitor", "NotebookEdit", "Read", "ReportFindings", "ScheduleWakeup", "SendMessage", "Skill",
           "TaskStop", "WebFetch", "WebSearch", "Workflow", "Write"]
MCP = ["ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool", "mcp__files__read_file",
       "mcp__exec__run_command"]
HIDDEN = ["Agent", "CronCreate", "CronDelete", "CronList", "EnterWorktree", "ExitWorktree", "ListAgents", "Monitor",
          "NotebookEdit", "ReportFindings", "ScheduleWakeup", "SendMessage", "Skill", "TaskStop", "WebFetch",
          "WebSearch", "Workflow"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def tool(name):
    schema = {"type": "object", "properties": {"command": {"type": "string"}}, "required": []}
    if name == "Workflow":  # its property without a type: what Seed-OSS's template refuses
        schema = {"type": "object", "properties": {"steps": {"description": "any"}}, "required": []}
    return {"name": name, "description": f"The {name} tool", "input_schema": schema}


def claude_request(names=BUILTIN + MCP, messages=None):
    return {"model": "m", "max_tokens": 32768, "stream": False, "tools": [tool(name) for name in names],
            "messages": messages or [{"role": "user", "content": "fix the bug"}]}


# --- the setting ------------------------------------------------------------------------------------------------------

def test_the_values_of_llm_tools():
    assert llm.parse_tools("all") is None and llm.parse_tools(None) == "core" and llm.parse_tools("core") == "core"
    assert llm.parse_tools("core,WebFetch") == ["core", "WebFetch"]
    assert llm.parse_tools("Bash, Read,Bash") == ["Bash", "Read"]
    for bad in ("", "a b", "Bash;rm", ",", "x" * 65, ",".join(f"T{n}" for n in range(65)), "all,Bash"):
        with pytest.raises(ValueError, match="--llm-tools"):
            llm.parse_tools(bad)


def test_what_the_proxy_keeps():
    assert llm.kept_tools("core") == list(llm.CLAUDE_CORE_TOOLS)
    assert llm.kept_tools(["core", "WebFetch"]) == [*llm.CLAUDE_CORE_TOOLS, "WebFetch"]
    assert llm.kept_tools(["Bash", "Read"]) == ["Bash", "Read"]
    assert llm.kept_tools(None) is None
    assert {"Bash", "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite"} <= set(llm.CLAUDE_CORE_TOOLS)
    assert {"ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool"} <= set(llm.CLAUDE_CORE_TOOLS)
    assert not set(HIDDEN) & set(llm.CLAUDE_CORE_TOOLS)


def endpoint(model="qwen/qwen3-coder-next"):
    llm.main(["add", "mac", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--model", model],
             out=lambda line: None)


def start(agent="claude", **more):
    values = {"llm": "mac", "agent": agent, "llm_param": [], "llm_addresses": ["127.0.0.1"], "no_llm_profile": False,
              "dry_run": True, "llm_tools": None}
    values.update(more)
    return argparse.Namespace(**values)


def test_the_start_and_the_profile_decide(tmp_path, monkeypatch):
    endpoint()
    assert llm.start_tools(start()) == ("core", "default")
    assert llm.start_tools(start(llm_tools="all")) == (None, "--llm-tools")
    assert llm.start_tools(start(llm_tools="core,WebFetch")) == (["core", "WebFetch"], "--llm-tools")
    table = json.loads((ROOT / "sandbox/model_profiles.json").read_text())
    table["families"][0]["harnesses"]["claude"]["tools"] = "all"
    (tmp_path / "profiles.json").write_text(json.dumps(table))
    monkeypatch.setenv("SANDBOX_AI_MODEL_PROFILES", str(tmp_path / "profiles.json"))
    assert llm.start_tools(start()) == (None, "profile")
    assert llm.start_tools(start(llm_tools="core")) == ("core", "--llm-tools"), "the start's option wins"
    assert llm.start_tools(start(no_llm_profile=True)) == ("core", "default")


def test_the_proxy_entry(tmp_path):
    endpoint()
    a = start()
    found, model = llm.selection(a)
    assert llm.proxy_params(a, found, model)["tools"] == {"keep": list(llm.CLAUDE_CORE_TOOLS)}
    a = start(llm_tools="all")
    found, model = llm.selection(a)
    assert "tools" not in llm.proxy_params(a, found, model)
    a = start(agent="opencode")
    found, model = llm.selection(a)
    assert "tools" not in llm.proxy_params(a, found, model), "Claude Code's tools: only where Claude Code runs"
    a = start(agent="opencode", companions=["claude"])
    found, model = llm.selection(a)
    assert llm.proxy_params(a, found, model)["tools"] == {"keep": list(llm.CLAUDE_CORE_TOOLS)}, "a team's Claude Code"


def test_profiles_may_set_it():
    table = json.loads((ROOT / "sandbox/model_profiles.json").read_text())
    for value in ("all", "core", ["core", "WebFetch"], ["Bash", "Read"]):
        table["families"][0]["all"]["tools"] = value
        mp.check_table(table, llm.profile_param, llm.loop_setting)
    for bad in ("some", ["a b"], [], 3, ["all"]):
        table["families"][0]["all"]["tools"] = bad
        with pytest.raises(ValueError, match="tools"):
            mp.check_table(table, llm.profile_param, llm.loop_setting)


# --- the request and the answer ---------------------------------------------------------------------------------------

def test_the_tools_left_out():
    document = claude_request()
    hidden = rt.llm_hide_tools(document, list(llm.CLAUDE_CORE_TOOLS))
    assert hidden == sorted(HIDDEN)
    assert [t["name"] for t in document["tools"]] == ["Bash", "Edit", "Read", "Write", *MCP]
    assert "Workflow" not in json.dumps(document), "Seed-OSS's template no longer meets Workflow's schema"


def test_a_tool_the_request_asks_for_stays_and_odd_documents_pass():
    document = {**claude_request(), "tool_choice": {"type": "tool", "name": "Workflow"}}
    rt.llm_hide_tools(document, ["Bash"])
    assert "Workflow" in [t["name"] for t in document["tools"]]
    for odd in (None, {}, {"tools": "x"}, {"tools": [None, 3, {"type": "web_search_20250305"}]}):
        before = json.dumps(odd)
        assert rt.llm_hide_tools(odd, ["Bash"]) == [] and json.dumps(odd) == before


def test_a_call_of_a_hidden_tool_gets_its_name_back_and_a_clear_error():
    marked = "Workflow" + rt.LLM_HIDDEN_MARK
    messages = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": [{"type": "text", "text": "I'll use a workflow."},
                                                  {"type": "tool_use", "id": "toolu_1", "name": marked, "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
                                              "content": f"<tool_use_error>Error: No such tool available: {marked}"
                                                         "</tool_use_error>"}]}]
    document = claude_request(messages=messages)
    assert rt.llm_hidden_results(document) == ["Workflow"]
    use = document["messages"][1]["content"][1]
    result = document["messages"][2]["content"][0]
    assert use["name"] == "Workflow" and result["is_error"] is True
    assert "Workflow is not offered to this model" in result["content"] and "--llm-tools" in result["content"]
    again = claude_request(messages=json.loads(json.dumps(messages)))
    rt.llm_hidden_results(again)
    assert again["messages"] == document["messages"], "the same every time: the server's prompt cache holds"
    assert rt.llm_hidden_results(claude_request()) == []


def answer_events(name):
    use = {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}
    return [b"event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {
                "id": "m", "type": "message", "role": "assistant", "content": [], "model": "m",
                "usage": {"input_tokens": 9, "output_tokens": 0}}}).encode() + b"\n\n",
            b"event: content_block_start\ndata: " + json.dumps({"type": "content_block_start", "index": 0,
                                                                "content_block": use}).encode() + b"\n\n",
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"]


def test_the_answer_renames_a_hidden_tools_call():
    answer = rt.LlmUsageAnswer(usage=False, hidden={"Workflow"})
    out = b"".join(answer.feed(event) for event in answer_events("Workflow")) + answer.flush()
    assert ('"name":"Workflow' + rt.LLM_HIDDEN_MARK + '"').encode() in out and answer.changed == 1
    plain = rt.LlmUsageAnswer(usage=False, hidden={"Workflow"})
    events = b"".join(answer_events("Bash"))
    assert plain.feed(events) + plain.flush() == events, "another call passes byte for byte"
    whole = rt.LlmUsageAnswer(usage=False, hidden={"Monitor"}).whole(json.dumps({
        "type": "message", "content": [{"type": "tool_use", "id": "t", "name": "Monitor", "input": {}}]}).encode())
    assert json.loads(whole)["content"][0]["name"] == "Monitor" + rt.LLM_HIDDEN_MARK
    usage = rt.LlmUsageAnswer()  # the usage rewrite alone, as before
    assert usage.hidden == set() and usage.usage is True


# --- end to end -------------------------------------------------------------------------------------------------------

class Model(http.server.BaseHTTPRequestHandler):
    """A model on the Messages API: answers with a call of CALL (a tool it was not offered, say)."""
    protocol_version = "HTTP/1.1"
    bodies, call = [], "Bash"

    def log_message(self, *args):
        pass

    def do_POST(self):
        Model.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
        data = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
                           "content": [{"type": "tool_use", "id": "toolu_1", "name": Model.call, "input": {}}],
                           "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def model():
    Model.bodies, Model.call = [], "Bash"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def post(relay, document, agent=CLAUDE_UA):
    import socket
    body = json.dumps(document).encode()
    with socket.create_connection(("127.0.0.1", relay.port), timeout=20) as sock:
        sock.sendall(b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                     b"User-Agent: " + agent.encode() + b"\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     % len(body) + body)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return live.dechunk(data)


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_claude_codes_request_reaches_the_model_with_the_core_tools(tmp_path, model):
    relay = live.Relay(tmp_path, model.server_address[1])
    relay.server.config["llm"][0]["tools"] = {"keep": list(llm.CLAUDE_CORE_TOOLS)}
    try:
        head, body = post(relay, claude_request())
        assert b" 200 " in head.split(b"\r\n", 1)[0]
        assert [t["name"] for t in Model.bodies[-1]["tools"]] == ["Bash", "Edit", "Read", "Write", *MCP]
        record = relay.records(1)[-1]
        assert record["llm_tools"]["hidden"] == sorted(HIDDEN)
        post(relay, claude_request(), agent="opencode/1.19.5 ai-sdk/provider-utils/4.0.23")
        assert len(Model.bodies[-1]["tools"]) == len(BUILTIN + MCP), "another harness's request as it came"
        relay.server.config["llm"][0].pop("tools")
        post(relay, claude_request())
        assert len(Model.bodies[-1]["tools"]) == len(BUILTIN + MCP), "--llm-tools all"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_a_call_of_a_hidden_tool_ends_in_a_clear_error(tmp_path, model):
    relay = live.Relay(tmp_path, model.server_address[1])
    relay.server.config["llm"][0]["tools"] = {"keep": list(llm.CLAUDE_CORE_TOOLS)}
    try:
        Model.call = "Workflow"
        head, body = post(relay, claude_request())
        marked = "Workflow" + rt.LLM_HIDDEN_MARK
        assert json.loads(body)["content"][0]["name"] == marked, "Claude Code gets a name it does not know"
        # Claude Code's own answer to an unknown tool, in its next request: named back, sandbox-ai's error in it.
        Model.call = "Bash"
        messages = [{"role": "user", "content": "go"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": marked,
                                                       "input": {}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
                                                  "content": f"<tool_use_error>Error: No such tool available: "
                                                             f"{marked}</tool_use_error>"}]}]
        post(relay, claude_request(messages=messages))
        sent = Model.bodies[-1]["messages"]
        assert sent[1]["content"][0]["name"] == "Workflow"
        assert "Workflow is not offered to this model" in sent[2]["content"][0]["content"]
        assert marked not in json.dumps(Model.bodies[-1])
    finally:
        relay.close()
