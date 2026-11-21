"""A tool whose parameters have no "required" (or no "properties") made LM Studio refuse the whole request for
Seed-OSS-36B (research round 3: HTTP 500 "Error rendering prompt with jinja template: \"Cannot perform operation in on
undefined values\"" for every OpenCode and Claude Code request with tools, both the mlx-community and the
lmstudio-community build): its chat template asks `if name in item.function.parameters.required` and iterates
`parameters.properties.items()`. OpenCode's MCP tools without a required parameter (exec_lint, files_list_dir ...) and
its list_mcp_resources have none. The relay gives such an object schema an empty "required" and "properties" - the
same schema - in every request to a local server (the same tools every time: the prompt cache holds)."""
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402

rt = live.rt


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def chat_request():
    return {"model": "seed-oss-36b-instruct-mlx", "stream": True, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "exec_lint", "parameters": {
                          "type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False}}},
                      {"type": "function", "function": {"name": "list_jobs", "parameters": {"type": "object"}}},
                      {"type": "function", "function": {"name": "bash", "parameters": {
                          "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]}


def test_regression_tool_schemas_get_required_and_properties():
    document = chat_request()
    done = rt.llm_request(live.entry("lmstudio"), "chat", document)
    lint, jobs, bash = (tool["function"]["parameters"] for tool in document["tools"])
    assert lint == {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False,
                    "required": []}
    assert jobs == {"type": "object", "properties": {}, "required": []}
    assert bash == chat_request()["tools"][2]["function"]["parameters"], "a complete schema stays as it is"
    assert done["tools"]["schemas"] == ["exec_lint", "list_jobs"]


def test_responses_and_messages_apis_and_namespaces():
    responses = {"tools": [{"type": "function", "name": "a", "parameters": {"type": "object", "properties": {}}},
                           {"type": "namespace", "name": "mcp__files", "tools": [
                               {"type": "function", "name": "stat", "parameters": {"type": "object"}}]}]}
    assert sorted(rt.llm_schema_keys(responses)) == ["a", "stat"]
    assert responses["tools"][1]["tools"][0]["parameters"] == {"type": "object", "properties": {}, "required": []}
    assert responses["tools"][0]["parameters"]["required"] == []
    messages = {"tools": [{"name": "Glob", "input_schema": {"type": "object", "properties": {"p": {}}}}]}
    assert rt.llm_schema_keys(messages) == ["Glob"] and messages["tools"][0]["input_schema"]["required"] == []


def test_odd_schemas_stay():
    for parameters in (None, "x", {"type": "string"}, {"anyOf": [{}]}, {"type": "object", "required": "x",
                                                                         "properties": {}}):
        document = {"tools": [{"type": "function", "function": {"name": "t", "parameters": parameters}}]}
        assert rt.llm_schema_keys(document) == [] and document["tools"][0]["function"]["parameters"] == parameters
    assert rt.llm_schema_keys({"tools": "x"}) == [] and rt.llm_schema_keys(None) == []


def test_the_same_tools_in_every_request():
    first, second = chat_request(), chat_request()
    for document in (first, second):
        rt.llm_request(live.entry("lmstudio"), "chat", document)
    assert first["tools"] == second["tools"]
    again = chat_request()
    rt.llm_schema_keys(again)
    assert rt.llm_schema_keys(again) == [], "a second pass changes nothing"


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_server_gets_the_completed_schemas(tmp_path, scripted):
    relay = live.Relay(tmp_path, scripted.server_address[1])
    try:
        head, _ = live.dechunk(relay.post(live.PATHS["chat"], chat_request()))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
    finally:
        relay.close()
    got = live.Scripted.bodies[-1]["tools"]
    assert all(tool["function"]["parameters"].get("required") is not None for tool in got)


scripted = live.scripted
