"""Security review SAI-23: every request that makes an MCP server act is judged by the server's rules, not only its
tool calls.

The run's relay (runtime.McpConnection) checked tools/call and filtered tools/list; every other request went to the
server unseen - resources/read, resources/subscribe, prompts/get, completion/complete, whatever a later protocol
adds. A ruleset that allowed two tools of a server still let every resource of it be read. Now such a request is
judged under its method's name, as a tool call under its tool's: rules, the ruleset's default, the human's question,
the record, and the run's taint. The handshake, the lists and notifications pass as before.

The real relay (McpRelays) and gateway on real sockets; the server is a socket that answers with what it was asked."""
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import tempfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime", "sandbox_runtime_review_mcp_test")


def fake_server(path, seen):
    """A server socket at PATH answering each request with its method (and keeping what reached it in SEEN)."""
    listener = rt.listen_unix(str(path))
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            def answer(connection=connection):
                with connection:
                    for line in connection.makefile("rb"):
                        request = json.loads(line)
                        seen.append(request.get("method"))
                        if "id" in request:
                            connection.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                                            "result": {"method": request.get("method")}})
                                                + "\n").encode())
            threading.Thread(target=answer, daemon=True).start()
        listener.close()
    threading.Thread(target=serve, daemon=True).start()
    return stop


class Broker:
    def __init__(self, decision="allow"):
        self.asked, self.decision = [], decision

    def ask(self, kind, details):
        self.asked.append((kind, details))
        return {"decision": self.decision, "scope": "once", "reason": "answered"}

    __call__ = ask


class Relayed:
    """One server NAME behind the run's real relay with RULES; ask(method, **params) is the agent's request."""

    def __init__(self, rules, broker=None, taint=None):
        self.base = Path(tempfile.mkdtemp(prefix="sr-mcp-", dir="/tmp"))
        (self.base / "up" / "files").mkdir(parents=True)
        self.seen = []
        self.stop = fake_server(self.base / "up" / "files" / "server.sock", self.seen)
        self.relays = rt.McpRelays(broker, taint, run_dir=self.base / "run", upstream_dir=self.base / "up",
                                   log=self.base / "calls.jsonl")
        self.relays.open("files", {"server": "files", "rules": rules})
        self.path = self.base / "run" / "files" / "server.sock"

    def ask(self, method, ident=7, **params):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(self.path))
            message = {"jsonrpc": "2.0", "method": method, **({"id": ident} if ident is not None else {}),
                       **({"params": params} if params else {})}
            client.sendall((json.dumps(message) + "\n").encode())
            if ident is None:
                client.sendall(b'{"jsonrpc": "2.0", "id": 99, "method": "ping"}\n')  # (the next answer: ping's)
            line = client.makefile("rb").readline()
            return json.loads(line) if line else None

    def records(self):
        path = self.base / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def close(self):
        for name in self.relays.names():
            self.relays.detach(name)
        self.stop.set()
        shutil.rmtree(self.base, ignore_errors=True)


@pytest.fixture
def relayed():
    made = []

    def make(rules, **options):
        made.append(Relayed(rules, **options))
        return made[-1]
    yield make
    for item in made:
        item.close()


ONLY_TWO_TOOLS = [{"name": "two-tools", "version": 1, "defaults": {"mcp": "deny"}, "mcp": [
    {"id": "read", "server": "files", "tool": ["read_file", "list_dir"], "decision": "allow"}]}]


def test_a_ruleset_that_allows_two_tools_no_longer_lets_every_resource_be_read(relayed):
    """The finding: with only tools/call judged, resources/read, prompts/get and completion/complete went to the
    server whatever the rules said. Now the ruleset's default holds for them too, and nothing reaches the server."""
    server = relayed(ONLY_TWO_TOOLS)
    assert server.ask("tools/call", name="read_file", arguments={"path": "a"})["result"] == {"method": "tools/call"}
    for method, params in (("resources/read", {"uri": "file:///etc/shadow"}), ("resources/subscribe", {"uri": "x:y"}),
                           ("prompts/get", {"name": "p"}), ("completion/complete", {"ref": {}, "argument": {}}),
                           ("tasks/result", {"taskId": "1"}), ("some/later/method", {})):
        refused = server.ask(method, **params)
        assert "error" in refused and "sandbox-ai:" in refused["error"]["message"], (method, refused)
        assert method not in server.seen
    refusals = [r for r in server.records() if r.get("event") == "call" and r.get("decision") == "deny"]
    assert {r["tool"] for r in refusals} >= {"resources/read", "prompts/get", "completion/complete"}
    assert all(r["rule"] == "(default)" and r["ruleset"] == "two-tools" for r in refusals)


def test_the_handshake_the_lists_and_notifications_pass_as_before(relayed):
    server = relayed(ONLY_TWO_TOOLS)
    for method in ("initialize", "ping", "logging/setLevel", "resources/list", "resources/templates/list",
                   "prompts/list"):
        assert server.ask(method)["result"] == {"method": method}, method
    # Notifications: passed on, nothing answered (the answer read here is the ping's after it).
    for method in ("notifications/initialized", "notifications/cancelled", "notifications/roots/list_changed"):
        assert server.ask(method, ident=None)["id"] == 99 and method in server.seen
    assert not any(r.get("event") == "call" for r in server.records())


def test_a_rule_names_a_method_as_it_names_a_tool(relayed):
    """Allowed by a rule, with a condition on its arguments, and recorded with its result like a tool call."""
    rules = [{"name": "docs-only", "version": 1, "defaults": {"mcp": "deny"}, "mcp": [
        {"id": "docs", "server": "files", "tool": "resources/read", "args": {"uri": {"glob": "file:///work/docs/*"}},
         "decision": "allow"},
        {"id": "prompts", "server": "files", "tool": "prompts/get", "decision": "allow"}]}]
    server = relayed(rules)
    assert server.ask("resources/read", uri="file:///work/docs/a.md")["result"] == {"method": "resources/read"}
    assert "error" in server.ask("resources/read", uri="file:///root/.codex/auth.json")
    assert server.ask("prompts/get", name="p")["result"] == {"method": "prompts/get"}
    assert server.seen.count("resources/read") == 1
    events = [(r["event"], r.get("tool"), r.get("decision")) for r in server.records() if r.get("event") in
              ("call", "result")]
    assert ("call", "resources/read", "allow") in events and ("result", "resources/read", None) in events
    assert ("call", "resources/read", "deny") in events


def test_a_request_that_acts_is_asked_of_the_human_with_what_it_is(relayed):
    rules = [{"name": "ask-reads", "version": 1, "mcp": [
        {"id": "ask", "server": "files", "tool": "resources/*", "decision": "ask", "reason": "a resource"}]}]
    broker = Broker("deny")
    server = relayed(rules, broker=broker)
    refused = server.ask("resources/read", uri="file:///work/notes.txt")
    assert "error" in refused and "not approved" in refused["error"]["message"]
    (kind, details), = broker.asked
    assert kind == "mcp" and details["server"] == "files" and details["tool"] == "resources/read"
    assert "file:///work/notes.txt" in details["arguments"] and details["rule"] == "ask"
    assert "resources/read" not in server.seen
    broker.decision = "allow"
    assert server.ask("resources/read", uri="file:///work/notes.txt")["result"] == {"method": "resources/read"}


def test_without_rules_every_request_passes_and_is_recorded(relayed):
    """No ruleset for the server: nothing is refused that was not before - the requests are in the record now."""
    server = relayed([])
    for method in ("resources/read", "prompts/get", "completion/complete"):
        assert server.ask(method, uri="x:y")["result"] == {"method": method}
    assert [r["tool"] for r in server.records() if r.get("event") == "call"] == [
        "resources/read", "prompts/get", "completion/complete"]


def test_a_resources_result_taints_the_run_where_the_servers_results_are_untrusted(relayed):
    """What a server sends for a resource is content like a tool's result: where a ruleset marks the server's
    results untrusted (security mode paranoid: every MCP result), the run is tainted before the agent reads it."""
    rules = [{"name": "untrusted", "version": 1, "untrusted": [{"id": "all-results", "server": "*", "tool": "*"}]}]
    taint = rt.Taint()
    server = relayed(rules, taint=taint)
    assert not taint.shown().get("tainted")
    assert server.ask("resources/read", uri="https://example.org/")["result"] == {"method": "resources/read"}
    assert taint.shown()["tainted"] is True


def test_a_request_without_an_id_or_with_a_name_no_rule_could_match_is_not_passed_on(relayed):
    server = relayed(ONLY_TWO_TOOLS)
    refused = server.ask("resources/read", ident=None, uri="file:///etc/passwd")
    assert refused["id"] is None and "without an id" in refused["error"]["message"]
    assert "resources/read" not in server.seen
    refused = server.ask("resources/read me", uri="x")
    assert refused["error"]["code"] == -32601 and "resources/read me" not in server.seen
    assert "error" in server.ask("Resources/Read", uri="x") and "Resources/Read" not in server.seen


def test_what_passes_unjudged_is_only_what_describes_the_server():
    acts = rt.McpConnection.acts
    for method in ("resources/read", "resources/subscribe", "resources/unsubscribe", "prompts/get",
                   "completion/complete", "tasks/get", "tasks/cancel", "sampling/createMessage", "x"):
        assert acts(method), method
    for method in ("initialize", "ping", "logging/setLevel", "tools/list", "tools/call", "resources/list",
                   "resources/templates/list", "prompts/list", "tasks/list", "notifications/initialized"):
        assert not acts(method), method
