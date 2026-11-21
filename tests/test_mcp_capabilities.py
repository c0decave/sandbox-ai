"""An MCP server's empty capabilities are not passed on to the agent (research round 1, local models with --llm).
User, live: "a lot of mcp issues ... endless loops. and some took ages before they finished".

Every FastMCP server advertises the "resources" and "prompts" capabilities, whether it publishes any or not (the
SDK registers the handlers for every server; sandbox-ai's catalog servers publish none). Agents act on the
capability: OpenCode, Claude Code and Pi then offer the model tools for MCP resources (list_mcp_resources,
read_mcp_resource; ListMcpResourcesTool, ReadMcpResourceTool), Hermes four more tools per server
(mcp__NAME__list_resources, read_resource, list_prompts, get_prompt). Local models take those for the way to the
server's tools (the research's round1/runs, Laguna-S-2.1 on LM Studio, asked for "the MCP tool read_file of the
server named files"): in Hermes mcp__files__read_resource again and again, then 60 more steps through Hermes' own
logs to find out how the "resource" is addressed, 10 minutes without one call of the server's tool
(smoke__hermes__laguna__r1); in OpenCode "the MCP files server isn't exposing any resources, so I can't use it"
(smoke__opencode__laguna__r1); in OpenCode 2 forty calls through its code mode (smoke__opencode2__laguna__r1).
With servers that advertise neither, these agents offer none of those tools (round1/evidence/capabilities.txt:
OpenCode 38 -> 35 tools, Claude Code 49 -> 46, Hermes 52 -> 44, Pi 32 -> 29).

The relay between the agent and the server (McpConnection) therefore asks a server that advertises resources or
prompts what it has - once, at the handshake - and answers the agent's initialize without a capability the server
has nothing for.

Unit tests of McpConnection with a scripted server, and end to end through mcp_serve with a server process."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_mcp_capabilities", "sandbox/runtime.py")

FASTMCP = {"experimental": {}, "prompts": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False},
           "tools": {"listChanged": False}}
INITIALIZE = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "agent", "version": "1"}}}
ANNOUNCE = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def answer(capabilities=None, ident=0):
    return {"jsonrpc": "2.0", "id": ident, "result": {
        "protocolVersion": "2025-06-18", "capabilities": FASTMCP if capabilities is None else capabilities,
        "serverInfo": {"name": "files-mcp", "version": "1.30.0"}, "instructions": "Reads and writes files."}}


class Session:
    """One McpConnection between a socket pair (the agent's end: self.agent) and a scripted server: what the relay
    writes to the server is read with sent(), the server's lines are given with say()."""

    def __init__(self):
        to_server, self.server_in = os.pipe()
        self.server_out, from_server = os.pipe()
        self.process = type("Process", (), {})()
        self.process.stdin = os.fdopen(self.server_in, "wb")
        self.process.stdout = os.fdopen(self.server_out, "rb")
        self.reads = os.fdopen(to_server, "rb")
        self.writes = os.fdopen(from_server, "wb")
        ours, self.agent = socket.socketpair()
        self.agent.settimeout(5)
        self.records = io.StringIO()
        self.gateway = rt.McpGateway({"server": "files"}, out=self.records, plain=True)
        self.connection = rt.McpConnection(self.gateway, ours, self.process)
        self.pump = threading.Thread(target=self.connection.pump, daemon=True)
        self.pump.start()
        self.buffer = b""

    def ask(self, message):
        self.connection.client_line((json.dumps(message) + "\n").encode())

    def sent(self, count=1):
        """The next COUNT messages the relay wrote to the server."""
        return [json.loads(self.reads.readline()) for _ in range(count)]

    def say(self, message):
        self.writes.write((message if isinstance(message, bytes) else (json.dumps(message) + "\n").encode()))
        self.writes.flush()

    def got(self, raw=False):
        """The next line the agent received."""
        while b"\n" not in self.buffer:
            data = self.agent.recv(65536)
            if not data:
                raise AssertionError("the relay closed the agent's connection")
            self.buffer += data
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line + b"\n" if raw else json.loads(line)

    def nothing_for_the_agent(self, seconds=0.3):
        self.agent.settimeout(seconds)
        try:
            data = self.agent.recv(65536)
        except socket.timeout:
            return True
        finally:
            self.agent.settimeout(5)
        self.buffer += data
        return False

    def handshake(self, capabilities=None, lists=None):
        """The agent's initialize, the server's answer, and the server's answers to what the relay asks it (LISTS:
        {method: result}; a method without one is left unanswered). Returns the relay's questions by method."""
        self.ask(INITIALIZE)
        assert self.sent() == [INITIALIZE]
        self.say(answer(capabilities))
        asked = {}
        given = FASTMCP if capabilities is None else capabilities
        families = [name for name in ("resources", "prompts") if isinstance(given.get(name), dict)
                    and not given[name].get("listChanged") and not given[name].get("subscribe")]
        count = 1 + (2 if "resources" in families else 0) + (1 if "prompts" in families else 0) if families else 0
        for message in self.sent(count):
            asked[message["method"]] = message
        for method, result in (lists or {}).items():
            self.say({"jsonrpc": "2.0", "id": asked[method]["id"], **result})
        return asked

    def log(self):
        return [json.loads(line) for line in self.records.getvalue().splitlines()]

    def close(self):
        for stream in (self.writes, self.reads):
            stream.close()
        self.agent.close()


EMPTY = {"resources/list": {"result": {"resources": []}}, "resources/templates/list": {"result": {"resourceTemplates": []}},
         "prompts/list": {"result": {"prompts": []}}}


@pytest.fixture
def session():
    made = Session()
    yield made
    made.close()


# --- the handshake ----------------------------------------------------------------------------------------------------

def test_regression_a_server_with_nothing_to_list_is_passed_on_without_the_capabilities(session):
    """sandbox-ai's files server (FastMCP 1.30: resources and prompts advertised, none published) behind the relay:
    the agent's initialize is answered with the tools capability only - what makes OpenCode, Claude Code, Hermes and
    Pi offer no resource tools for it."""
    asked = session.handshake(lists=EMPTY)
    assert list(asked) == ["notifications/initialized", "resources/list", "resources/templates/list", "prompts/list"]
    assert asked["notifications/initialized"] == ANNOUNCE, "the server is told first: it answers no request before"
    assert all(isinstance(asked[method]["id"], str) and asked[method]["id"].startswith("sandbox-ai-")
               for method in EMPTY), "the relay's own ids: never one the agent uses"
    got = session.got()
    assert got["id"] == 0 and got["result"]["capabilities"] == {"experimental": {}, "tools": {"listChanged": False}}
    assert {key: value for key, value in got["result"].items() if key != "capabilities"} == \
        {key: value for key, value in answer()["result"].items() if key != "capabilities"}
    assert session.nothing_for_the_agent(), "the relay's questions and their answers stay between it and the server"
    assert {"event": "capabilities", "hidden": ["resources", "prompts"]}.items() <= session.log()[-1].items()
    # The agent's own notification does not reach the server a second time; what follows passes as ever.
    session.ask(ANNOUNCE)
    session.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert session.sent() == [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    session.say({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "read_file"}]}})
    assert session.got() == {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "read_file"}]}}
    # A later notification of the agent's passes (only the one the relay sent in its place is left out).
    session.ask(ANNOUNCE)
    assert session.sent() == [ANNOUNCE]


@pytest.mark.parametrize("lists,kept", [
    # a resource: resources stay, prompts go
    ({**EMPTY, "resources/list": {"result": {"resources": [{"uri": "docs://readme", "name": "readme"}]}}},
     ["resources"]),
    # a template only
    ({**EMPTY, "resources/templates/list": {"result": {"resourceTemplates": [{"uriTemplate": "file:///{path}",
                                                                              "name": "file"}]}}}, ["resources"]),
    # a prompt
    ({**EMPTY, "prompts/list": {"result": {"prompts": [{"name": "review"}]}}}, ["prompts"]),
    # more pages to come: not empty
    ({**EMPTY, "resources/list": {"result": {"resources": [], "nextCursor": "page2"}}}, ["resources"]),
    # an error, an answer of another shape: the relay does not know, the capability stays
    ({**EMPTY, "prompts/list": {"error": {"code": -32601, "message": "Method not found"}}}, ["prompts"]),
    ({**EMPTY, "resources/list": {"result": {"resources": "many"}}, "prompts/list": {"result": None}},
     ["resources", "prompts"]),
])
def test_a_capability_the_server_has_something_for_stays(session, lists, kept):
    session.handshake(lists=lists)
    got = session.got()
    assert [name for name in ("resources", "prompts") if name in got["result"]["capabilities"]] == kept
    assert got["result"]["capabilities"]["tools"] == {"listChanged": False}
    if kept == ["resources", "prompts"]:
        assert got == answer(), "nothing to hide: the server's answer as it came"


@pytest.mark.parametrize("capabilities", [
    {"tools": {"listChanged": True}},                                   # a tools-only server (context7)
    {"tools": {}, "resources": None, "prompts": None},
    {"tools": {}, "logging": {}, "completions": {}},
    "odd", [],
])
def test_a_server_that_advertises_neither_is_not_asked(session, capabilities):
    session.ask(INITIALIZE)
    assert session.sent() == [INITIALIZE]
    line = (json.dumps(answer(capabilities), separators=(",", ":")) + "\n").encode()
    session.say(line)
    assert session.got(raw=True) == line, "byte for byte"
    session.ask(ANNOUNCE)
    assert session.sent() == [ANNOUNCE], "the agent's own notification reaches the server: the relay sent none"
    assert session.log() == []


def test_a_capability_that_may_change_is_not_asked_about(session):
    """listChanged or subscribe: the server may publish something later and says it will tell - the capability
    stays, whatever it lists now."""
    capabilities = {"tools": {}, "resources": {"listChanged": True}, "prompts": {"listChanged": False}}
    asked = session.handshake(capabilities, lists={"prompts/list": {"result": {"prompts": []}}})
    assert list(asked) == ["notifications/initialized", "prompts/list"]
    assert session.got()["result"]["capabilities"] == {"tools": {}, "resources": {"listChanged": True}}
    session.close()
    other = Session()
    try:
        asked = other.handshake({"tools": {}, "resources": {"subscribe": True}})
        assert asked == {} and other.got()["result"]["capabilities"] == {"tools": {}, "resources": {"subscribe": True}}
    finally:
        other.close()


def test_an_error_answer_to_initialize_passes(session):
    session.ask(INITIALIZE)
    session.sent()
    error = {"jsonrpc": "2.0", "id": 0, "error": {"code": -32602, "message": "Unsupported protocol version"}}
    session.say(error)
    assert session.got() == error


# --- a server that does not answer the relay's questions --------------------------------------------------------------

def test_a_server_that_never_answers_the_questions_does_not_hold_the_agent(session, monkeypatch):
    """The agent's initialize is answered after MCP_PROBE_SECONDS at the latest, with the server's own answer; what
    the server says to the questions afterwards never reaches the agent (its ids are the relay's)."""
    monkeypatch.setattr(rt, "MCP_PROBE_SECONDS", 0.3)
    started = time.monotonic()
    asked = session.handshake(lists={"prompts/list": {"result": {"prompts": []}}})
    assert session.got() == answer() and 0.25 < time.monotonic() - started < 3
    assert {"event": "capabilities", "unanswered": True}.items() <= session.log()[-1].items()
    session.say({"jsonrpc": "2.0", "id": asked["resources/list"]["id"], "result": {"resources": []}})
    session.say({"jsonrpc": "2.0", "id": asked["resources/templates/list"]["id"], "result": {"resourceTemplates": []}})
    assert session.nothing_for_the_agent()
    session.ask({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert session.sent() == [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]
    session.say({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert session.got() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_a_server_that_ends_during_the_questions_leaves_its_answer_and_no_strangers(session):
    """The server's answer to initialize was there: the agent gets it; the relay's own questions are never
    answered to the agent (it did not ask them)."""
    session.handshake(lists={})
    session.writes.close()
    assert session.got() == answer()
    session.agent.settimeout(5)
    rest = b""
    while chunk := session.agent.recv(65536):
        rest += chunk
    assert rest == b"", "no error answers for ids the agent never used"
    session.writes = open(os.devnull, "wb")  # (for close)


def test_a_server_the_relay_could_not_tell_hears_it_from_the_agent(session):
    """The relay's notifications/initialized could not be written (once): the server's answer goes on as it came,
    and the agent's own notification is passed on - a server never misses it."""
    written, real = [], session.process.stdin

    class Stdin:
        def write(self, data):
            if b"notifications/initialized" in data and not written:
                written.append("failed")
                raise OSError(32, "Broken pipe")
            written.append(json.loads(data))
            return real.write(data)

        def flush(self):
            real.flush()
    session.ask(INITIALIZE)
    session.sent()
    session.process.stdin = Stdin()
    session.say(answer())
    assert session.got() == answer()
    session.ask(ANNOUNCE)
    assert written == ["failed", ANNOUNCE] and session.sent() == [ANNOUNCE]


def test_the_servers_own_messages_pass_while_the_relay_asks(session):
    session.ask(INITIALIZE)
    session.sent()
    session.say(answer())
    asked = {message["method"]: message for message in session.sent(4)}
    log = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "ready"}}
    session.say(log)
    assert session.got() == log
    for method, result in EMPTY.items():
        session.say({"jsonrpc": "2.0", "id": asked[method]["id"], **result})
    assert "resources" not in session.got()["result"]["capabilities"]


# --- end to end: mcp_serve with a server process ----------------------------------------------------------------------

SERVER = r'''
import json, sys
have = json.loads(sys.argv[1])
seen = open(sys.argv[2], "a")
ready = False
for line in sys.stdin:
    message = json.loads(line)
    seen.write(json.dumps(message) + "\n"); seen.flush()
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "demo", "version": "1"},
                  "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False},
                                   "resources": {"subscribe": False, "listChanged": False}}}
    elif method == "notifications/initialized":
        ready = True
        continue
    elif not ready:   # as the MCP SDK's server: no request before the client said it is ready
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "not initialized"}}), flush=True)
        continue
    elif method == "resources/list":
        result = {"resources": have.get("resources", [])}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    elif method == "tools/list":
        result = {"tools": [{"name": "read_file", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "the file"}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
'''


def serve(tmp_path, have):
    """mcp_serve on a socket with the scripted server; returns (connect, the file of what the server received)."""
    script, seen, sock = tmp_path / "server.py", tmp_path / "seen.jsonl", tmp_path / "s.sock"
    script.write_text(SERVER)
    seen.write_text("")
    threading.Thread(target=rt.mcp_serve, args=(str(sock), [sys.executable, str(script), json.dumps(have), str(seen)]),
                     kwargs={"cwd": str(tmp_path), "out": io.StringIO()}, daemon=True).start()
    for _ in range(100):
        if sock.exists() and sock.is_socket():
            break
        time.sleep(0.05)

    def connect():
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(10)
        client.connect(str(sock))
        return client, client.makefile("rb")
    return connect, seen


def exchange(client, stream, message):
    client.sendall((json.dumps(message) + "\n").encode())
    return json.loads(stream.readline()) if "id" in message else None


@pytest.mark.parametrize("have,kept", [({}, []), ({"resources": [{"uri": "docs://a", "name": "a"}]}, ["resources"])])
def test_e2e_an_agent_connects_to_a_server_through_the_relay(tmp_path, have, kept):
    """An agent's whole start with an MCP server through mcp_serve: initialize, its notification, tools/list, a
    call. The server process sees one notifications/initialized (the relay's), the relay's three questions, and the
    agent's requests; the agent sees the capabilities the server has something for."""
    connect, seen = serve(tmp_path, have)
    client, stream = connect()
    try:
        got = exchange(client, stream, INITIALIZE)
        assert sorted(got["result"]["capabilities"]) == sorted(["tools", *kept])
        exchange(client, stream, ANNOUNCE)
        assert exchange(client, stream, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"][0][
            "name"] == "read_file"
        called = exchange(client, stream, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                           "params": {"name": "read_file", "arguments": {"path": "a"}}})
        assert called["result"]["content"][0]["text"] == "the file"
    finally:
        client.close()
    received = [json.loads(line) for line in seen.read_text().splitlines()]
    assert [message["method"] for message in received] == [
        "initialize", "notifications/initialized", "resources/list", "resources/templates/list", "prompts/list",
        "tools/list", "tools/call"]
    # A second connection (the agent's next session) is asked again: each has its own server instance.
    client, stream = connect()
    try:
        assert sorted(exchange(client, stream, INITIALIZE)["result"]["capabilities"]) == sorted(["tools", *kept])
    finally:
        client.close()
