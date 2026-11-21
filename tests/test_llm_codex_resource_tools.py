"""Codex's tools for MCP resources are left out of a local model's requests when no attached MCP server publishes any.

Codex offers list_mcp_resources, list_mcp_resource_templates and read_mcp_resource whatever its MCP servers have (the
other agents offer theirs only for a server that advertises resources, which sandbox-ai's MCP relay leaves out when
the server lists none). Local models take them for the way to a server's tools: Laguna-S guessed resource URIs 115
times in round 1; in round 4 Ornith-1.5-122B (otherwise the best model of the round) failed Codex's smoke test with
them - list_mcp_resources twice, list_mcp_resource_templates, read_mcp_resource seven times with guessed URIs, then a
search of the disk for the server's source - although read_mcp_resource's description named mcp__files__read_file
(runs/smoke__codex__orn122__a1). The failures layer did not fire: the failed resource calls alternated with working
commands.

Where the knowledge lives: the supervisor's MCP relay asks every server what it lists at the handshake (McpConnection,
for its empty capabilities); it now also reports per server whether it publishes resources (McpGateway.note_resources:
False for no resources capability or empty lists, True for a list with something, more pages, an error or a list
that may change; nothing when the server did not answer in time), and McpRelays writes that to a mirror in the
proxy's run directory (mcp-resources.json, as taint.json). The proxy reads it (re-read on change) and leaves the three
tools out of a Responses API request only when every MCP server whose tools the request has is known to publish
none. Unknown servers, a server with resources, no mirror (no relay, an older supervisor), a request without any MCP
server's tools: the tools stay as Codex sent them.

Unit tests (llm_resource_tools_out, llm_request, McpConnection's report, McpRelays' mirror), a regression test on the
round-4 shape, and end to end through the real ProxyServer with a scripted model."""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402
import test_mcp_capabilities as caps  # noqa: E402

rt, llm = live.rt, live.llm
TRIO = ["list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def function(name, description="a tool"):
    return {"type": "function", "name": name, "description": description, "strict": False,
            "parameters": {"type": "object", "properties": {}, "required": []}}


def codex_request(servers=("files", "exec")):
    """A Codex request as it reaches the proxy: its own tools, the three resource tools, each MCP server's tools in a
    namespace (mcp__NAME)."""
    tools = [function("exec_command"), function("write_stdin")] + [function(name) for name in TRIO]
    for server in servers:
        tools.append({"type": "namespace", "name": f"mcp__{server}", "description": f"the {server} server",
                      "tools": [function("read_file" if server == "files" else "run_tests")]})
    return {"model": "ornith-1.5-122b-mlx", "stream": True, "tools": tools,
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read notes/secret.txt with the MCP tool read_file of the server files."}]}]}


def names(document):
    return [tool.get("name") for tool in document["tools"]]


# --- the proxy's side ----------------------------------------------------------------------------------------------

def test_unit_the_resource_tools_go_when_every_server_publishes_none():
    document = codex_request()
    rt.llm_flat_tools(document)
    assert rt.llm_resource_tools_out(document, {"files": False, "exec": False}) == TRIO
    assert names(document) == ["exec_command", "write_stdin", "mcp__files__read_file", "mcp__exec__run_tests"]
    assert rt.llm_resource_tools_out(document, {"files": False, "exec": False}) == []  # nothing left to take


@pytest.mark.parametrize("published", [
    {"files": False, "exec": True},          # one server publishes resources
    {"files": False, "exec": None},          # one server did not answer in time
    {"files": False},                        # one server not relayed (or attached after the record)
    {},                                      # no server known
    None,                                    # no mirror: no relay, an older supervisor
    "odd", {"files": "no", "exec": False},   # a mirror of another shape: nothing is concluded from it
])
def test_unit_the_resource_tools_stay_unless_every_server_is_known_to_publish_none(published):
    document = codex_request()
    rt.llm_flat_tools(document)
    before = names(document)
    assert rt.llm_resource_tools_out(document, published) == []
    assert names(document) == before


def test_unit_a_request_without_mcp_servers_keeps_them():
    """No server's tools in the request: which servers Codex has is not known from it - the tools stay."""
    document = codex_request(servers=())
    assert rt.llm_resource_tools_out(document, {"files": False}) == []
    assert all(name in names(document) for name in TRIO)


def test_unit_odd_documents():
    for document in (None, {}, {"tools": "x"}, {"tools": [None, 3, {"name": 5}]}):
        assert rt.llm_resource_tools_out(document, {"files": False}) == []


def test_llm_request_leaves_them_out_and_says_so():
    document = codex_request()
    done = rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "responses", document,
                          resources={"files": False, "exec": False})
    assert done["tools"]["resources_out"] == TRIO
    assert not any(name in names(document) for name in TRIO)
    assert "resources" not in done["tools"]  # no sentence for tools that are gone
    # the chat and Messages APIs and an unknown state: as before (the sentence in the description)
    document = codex_request()
    done = rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "responses", document)
    assert "resources_out" not in done["tools"] and done["tools"]["resources"] == TRIO


def test_regression_round4_codex_smoke_shape():
    """runs/smoke__codex__orn122__a1: 40 tools, the trio among them, mcp__files__* and mcp__exec__* flattened."""
    document = codex_request()
    for server, tool in (("files", "write_file"), ("files", "edit_file"), ("exec", "job_status")):
        next(t for t in document["tools"] if t.get("name") == f"mcp__{server}")["tools"].append(function(tool))
    done = rt.llm_request(live.entry("lmstudio", mcp=["files", "exec"]), "responses", document,
                          resources={"files": False, "exec": False})
    assert done["tools"]["resources_out"] == TRIO
    assert "mcp__files__read_file" in names(document) and "mcp__exec__job_status" in names(document)


# --- the relay's side: what it learns at the handshake ----------------------------------------------------------------

@pytest.fixture
def session():
    made = caps.Session()
    made.reports = []
    made.gateway.on_resources = lambda server, value: made.reports.append((server, value))
    yield made
    made.close()


def test_relay_reports_none_for_a_server_whose_lists_are_empty(session):
    session.handshake(lists=caps.EMPTY)
    session.got()
    assert session.reports == [("files", False)] and session.gateway.resources is False


@pytest.mark.parametrize("lists", [
    {**caps.EMPTY, "resources/list": {"result": {"resources": [{"uri": "file:///a", "name": "a"}]}}},
    {**caps.EMPTY, "resources/templates/list": {"result": {"resourceTemplates": [], "nextCursor": "2"}}},
    {**caps.EMPTY, "resources/list": {"error": {"code": -32601, "message": "no"}}},
])
def test_relay_reports_resources_for_a_list_with_something_or_unknown(session, lists):
    session.handshake(lists=lists)
    session.got()
    assert session.reports == [("files", True)]


def test_relay_reports_none_without_the_capability_and_resources_for_one_that_may_change(session):
    session.handshake(capabilities={"tools": {}})
    session.got()
    assert session.reports == [("files", False)]
    other = caps.Session()
    other.reports = []
    other.gateway.on_resources = lambda server, value: other.reports.append((server, value))
    try:
        other.handshake(capabilities={"tools": {}, "resources": {"listChanged": True}})
        other.got()
        assert other.reports == [("files", True)]
    finally:
        other.close()


def test_relay_reports_nothing_when_the_server_does_not_answer_in_time(session, monkeypatch):
    held = []
    monkeypatch.setattr(rt.McpConnection, "unanswered", lambda self, h: held.append(h))
    session.handshake(lists={})  # the relay's questions stay open
    assert session.reports == []


def test_mcp_relays_write_the_mirror(tmp_path):
    mirror = tmp_path / "run" / "mcp-resources.json"
    mirror.parent.mkdir()
    relays = rt.McpRelays(run_dir=tmp_path / "mcp", upstream_dir=tmp_path / "up", log=tmp_path / "calls.jsonl",
                          resources_mirror=mirror)
    relays.record_resources("files", False)
    relays.record_resources("exec", True)
    assert json.loads(mirror.read_text()) == {"servers": {"files": False, "exec": True}}
    relays.record_resources("exec", False)
    assert json.loads(mirror.read_text()) == {"servers": {"files": False, "exec": False}}
    relays.forget_resources("exec")
    assert json.loads(mirror.read_text()) == {"servers": {"files": False}}
    assert oct(mirror.stat().st_mode & 0o777) == "0o644"  # the proxy user reads it
    rt.McpRelays(run_dir=tmp_path / "m2", upstream_dir=tmp_path / "u2", log=tmp_path / "c2.jsonl").record_resources(
        "files", False)  # no mirror: nothing written, nothing raised


# --- end to end: the real proxy reads the mirror ----------------------------------------------------------------------

class Relay(live.Relay):
    """live.Relay with the proxy's resources_file (the supervisor's mirror) and the start's MCP servers."""

    def __init__(self, tmp_path, port, mirror):
        run_dir = tmp_path / "run"
        rt.create_ca(run_dir, tmp_path / "ca", bundles=())
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        llm.main(["add", "mac", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model", "m"],
                 out=lambda line: None)
        a = argparse.Namespace(llm="mac", agent="codex", llm_param=[], mcp=["files", "exec"])
        a.llm_addresses = ["127.0.0.1"]
        config = {"body_limit": 1 << 20, "allow": {"loopback": [port]}, "llm": llm.proxy_entry(a),
                  "resources_file": str(mirror)}
        self.server = rt.ProxyServer(config, self.fd, None, run_dir=run_dir,
                                     destination=lambda sock: ("127.0.0.1", port))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_server_gets_codexs_request_without_the_resource_tools(tmp_path, scripted):
    mirror = tmp_path / "mcp-resources.json"
    relay = Relay(tmp_path, scripted.server_address[1], mirror)
    try:
        # 1. no mirror yet: the tools go as Codex sent them (flattened, with the proxy's sentence)
        live.Scripted.mode = "text"
        relay.post(live.PATHS["responses"], codex_request())
        sent = [tool["name"] for tool in live.Scripted.bodies[-1]["tools"]]
        assert all(name in sent for name in TRIO) and "mcp__files__read_file" in sent
        # 2. the relay found that neither server publishes resources: left out from the next request on
        mirror.write_text(json.dumps({"servers": {"files": False, "exec": False}}))
        relay.post(live.PATHS["responses"], codex_request())
        sent = [tool["name"] for tool in live.Scripted.bodies[-1]["tools"]]
        assert not any(name in sent for name in TRIO)
        assert sent == ["exec_command", "write_stdin", "mcp__files__read_file", "mcp__exec__run_tests"]
        # 3. a server that publishes resources (the mirror re-read on change): they stay
        mirror.write_text(json.dumps({"servers": {"files": False, "exec": True}}))
        os.utime(mirror, ns=(1, 1))  # a different mtime even within the clock's resolution
        relay.post(live.PATHS["responses"], codex_request())
        sent = [tool["name"] for tool in live.Scripted.bodies[-1]["tools"]]
        assert all(name in sent for name in TRIO)
    finally:
        relay.close()


scripted = live.scripted
