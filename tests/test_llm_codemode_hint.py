"""OpenCode 2's code mode offers the tools for MCP resources inside its `execute` tool, listed in the system prompt's
"Code Mode" catalog (`tools.opencode.list_mcp_resources`, `tools.opencode.read_mcp_resource`) whatever the servers
publish - so neither the MCP relay's dropped capabilities nor the proxy's sentence in a tool's description reached
them (research round 1, open point 2: Laguna-S in OpenCode 2 called `tools.opencode.read_mcp_resource` 40 times with
guessed URIs, smoke__opencode2__laguna__r1/r2). No setting of OpenCode 2 2.2.6 takes them out (round 2: a permission
deny, `tools: {read_mcp_resource: false}`, `codemode: false` - the catalog unchanged, round2/tools/oc2cfg.sh). The
proxy now puts its sentence under each such catalog entry, as it does into the resource tools' descriptions."""
import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402

rt = live.rt

CATALOG = (
    "You are opencode.\n\n# Code Mode\n\nThe tools listed below cannot be called directly: they only work as "
    "JavaScript inside the `execute` tool.\n\nThe catalog is complete. Do not guess tool names.\n\n"
    "## Code Mode tools (call only inside `execute`)\n\n- opencode (8 tools) // Tools for managing OpenCode itself, "
    "such as working with sessions and worktrees, searching the available models, and reading MCP resources.\n"
    "  - tools.opencode.list_mcp_resources({\n  /** MCP server name as configured. Omit to list every server. */\n"
    "  server?: string,\n}): Promise<{\n  resources: Array<{ server: string, uri: string }>,\n}> // List documents, "
    "records, and other data exposed by MCP servers.\n"
    "  - tools.opencode.models({\n  query?: string,\n}): Promise<{ total: number }> // Search the models.\n"
    "  - tools.opencode.read_mcp_resource({\n  /** The server field of the discovered resource. */\n"
    "  server: string,\n"
    "  uri: string,\n}): Promise<{\n  contents: Array<{ type: \"text\", text: string }>,\n}> // Read one MCP resource "
    "by server and URI. Not for local files.\n"
    "  - tools.opencode.session_move({\n  directory: string,\n}): Promise<{}>\n\n"
    "<env>\n  Working directory: /w\n</env>")


def chat_tool(name):
    return {"type": "function", "function": {"name": name, "description": f"{name}.",
                                             "parameters": {"type": "object", "properties": {}, "required": []}}}


def opencode2_request(mcp=True):
    tools = [chat_tool(name) for name in ("edit", "read", "shell", "execute")]
    if mcp:
        tools += [chat_tool("files_read_file"), chat_tool("files_stat"), chat_tool("exec_run_command")]
    return {"model": "poolside/laguna-s-2.1", "stream": True, "tools": tools,
            "messages": [{"role": "system", "content": CATALOG}, {"role": "user", "content": "read notes/a.txt"}]}


def test_regression_opencode2s_code_mode_resource_tools_get_the_sentence():
    document = opencode2_request()
    done = rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "chat", document)
    text = document["messages"][0]["content"]
    assert done["tools"] == {"resources": ["tools.opencode.list_mcp_resources", "tools.opencode.read_mcp_resource"]}
    sentence = ("// (Only for resources an MCP server publishes; most servers publish none. This does not call a "
                "server's tools and does not read files. A server's tools are functions of their own: tool T of the "
                "MCP server files is the function files_T, of exec exec_T - for example files_read_file. Call that "
                "function directly.)")
    assert text.count(sentence) == 2
    # each under its own entry, before the next one; the rest of the prompt as it was
    assert text.index("Not for local files.\n    " + sentence + "\n  - tools.opencode.session_move") > 0
    assert text.index("other data exposed by MCP servers.\n    " + sentence + "\n  - tools.opencode.models") > 0
    assert text.replace("\n    " + sentence, "") == CATALOG
    assert document["tools"] == opencode2_request()["tools"]


def test_the_first_request_before_the_servers_are_connected_and_every_later_one_the_same():
    # OpenCode 2 asks the model before its MCP servers are connected: no function of theirs to name yet
    first = opencode2_request(mcp=False)
    rt.llm_request(live.entry("lmstudio", mcp=["files"]), "chat", first)
    assert "named after the server and the tool. Call that function directly.)" in first["messages"][0]["content"]
    later, again = opencode2_request(), opencode2_request()
    for document in (later, again):
        rt.llm_request(live.entry("lmstudio", mcp=["files"]), "chat", document)
    assert later == again, "the same text in every request (the system prompt is the server's cached beginning)"


def test_a_prompt_without_the_catalog_and_odd_ones_stay():
    for content in ("You are opencode.", None, ["a", "list"], CATALOG.replace("tools.opencode.", "tools.other.")):
        document = opencode2_request()
        document["messages"][0]["content"] = copy.deepcopy(content)
        done = rt.llm_request(live.entry("lmstudio", mcp=["files"]), "chat", document)
        assert document["messages"][0]["content"] == content and "tools" not in done
    # a user message that quotes the catalog is not the system prompt
    document = opencode2_request()
    document["messages"][1]["content"] = CATALOG
    document["messages"][0]["content"] = "You are opencode."
    rt.llm_request(live.entry("lmstudio", mcp=["files"]), "chat", document)
    assert document["messages"][1]["content"] == CATALOG
