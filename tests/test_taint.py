"""The run's taint (CaMeL's rule at sandbox-ai's layer): once untrusted content entered a run (a response of a host,
or the result of an MCP tool, that a ruleset marks untrusted), the rules with "when": {"tainted": true} hold for the
rest of the run, and only the human takes the taint back.

The proxy and the supervisor's MCP relay are the real code on real sockets; the supervisor's side of the proxy's
channel is the real serve_asks with a real Taint. Bypass tests assume an agent that knows the rules: the provider's
name in a Host header, the source's bare address, a tool name in another case, the relay's upstream socket."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import tempfile
import threading
import time

import pytest

import test_proxy as tp
from test_proxy import pki  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
rt = tp.rt


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_taint_test", "agent_sandbox.py")
ap = load("approvals_taint_test", "approvals.py")
net = load("net_taint_test", "net.py")
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")

RULES = {"version": 1, "untrusted": [
    {"id": "news", "host": ["news.example", "*.forum.example"]},
    {"id": "fetch", "server": "web", "tool": "fetch"}],
    "http": [{"id": "tainted-writes", "host": "*", "methods": ["POST", "PUT", "PATCH", "DELETE"],
              "when": {"tainted": True}, "decision": "ask"}],
    "mcp": [{"id": "tainted-files", "server": "files", "tool": ["write_file", "move_file"],
             "when": {"tainted": True}, "decision": "ask"}]}
STATE = {"tainted": True, "since": "2026-11-04T10:00:00Z", "sources": 1,
         "source": {"layer": "proxy", "rule": "news", "ruleset": "rules.json", "what": "news.example"}}


def ruleset(document=RULES, providers=("api.provider.example",), pinned=None):
    return rt.Ruleset([{"name": "rules.json", **document}], pinned, providers)


def facts(host, method="GET", target="/", origin=None, taint=None, address=None):
    found = rt.Ruleset.request(origin or host, method, target, [["Host", host]], address)
    found["taint"] = taint
    return found


# --- the rulesets ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("source,message", [
    ({"id": "x"}, 'either "host"'),
    ({"id": "x", "host": "a.example", "tool": "fetch"}, 'either "host"'),
    ({"id": "x", "host": "a.example", "server": "web"}, '"server" belongs to a source with "tool"'),
    ({"id": "x", "tool": "fetch", "path": "/x"}, '"path" belongs to a source with "host"'),
    ({"id": "x", "host": "a.example", "labels": ["untrusted"]}, "unknown key 'labels'"),
    ({"host": "a.example"}, '"id" is required'),
    ({"id": "x", "host": "bad host"}, '"host"'),
    ("a.example", "a source is a JSON object"),
])
def test_malformed_sources_are_refused(source, message):
    with pytest.raises(rt.RuleError, match=message):
        rt.Ruleset.validated({"version": 1, "untrusted": [source]}, "rules.json")


def test_when_takes_only_tainted_true_and_ids_are_shared():
    for when in ({"tainted": False}, {"tainted": "yes"}, {"untrusted": True}, "tainted"):
        with pytest.raises(rt.RuleError, match='"when" takes only'):
            rt.Ruleset.validated({"version": 1, "http": [{"id": "x", "host": "*", "when": when, "decision": "ask"}]}, "r")
    with pytest.raises(rt.RuleError, match="used twice"):
        rt.Ruleset.validated({"version": 1, "untrusted": [{"id": "x", "host": "a.example"}],
                              "http": [{"id": "x", "host": "*", "decision": "deny"}]}, "r")
    checked = rt.Ruleset.validated(RULES, "r")
    assert rt.Ruleset.validated(checked, "r") == checked  # the normal form stays as it is
    assert checked["untrusted"][0] == {"id": "news", "host": ["news.example", "*.forum.example"], "path": ["/**"]}


def test_sources_never_include_the_provider_by_a_claimed_name():
    rules = ruleset({**RULES, "untrusted": [{"id": "all", "host": "*"}]})
    assert rules.untrusted_http(facts("anything.example"))["rule"] == "all"
    assert rules.untrusted_http(facts("api.provider.example")) is None  # the agent's own model API
    # The provider's name in a Host header at another server proves nothing: still a source.
    assert rules.untrusted_http(facts("api.provider.example", origin="evil.example"))["rule"] == "all"
    assert rules.untrusted_http(facts("api.provider.example", origin="203.0.113.7:80"))["rule"] == "all"


def test_a_source_is_known_by_every_name_of_the_request():
    rules = ruleset(pinned={"news.example": ["203.0.113.9"]})
    assert rules.untrusted_http(facts("news.example"))["what"] == "news.example"
    assert rules.untrusted_http(facts("NEWS.Example."))["rule"] == "news"
    assert rules.untrusted_http(facts("a.forum.example"))["rule"] == "news"
    # Its bare address under another name: the pinned address still names it.
    assert rules.untrusted_http(facts("cdn.example", origin="203.0.113.9:80", address="203.0.113.9"))["rule"] == "news"
    assert rules.untrusted_http(facts("forum.example")) is None  # *.DOMAIN is below DOMAIN only
    assert rules.untrusted_tool("web", "fetch")["what"] == "the MCP tool fetch of web"
    assert rules.untrusted_tool("web", "FETCH")["rule"] == "fetch"  # its name in another case is no way around
    assert rules.untrusted_tool("other", "fetch") is None


def test_rules_for_tainted_runs_hold_only_once_tainted_and_never_for_the_provider():
    rules = ruleset()
    assert rules.http(facts("api.example", "POST"))["rule"] is None
    verdict = rules.http(facts("api.example", "POST", taint=STATE))
    assert verdict["decision"] == "ask" and verdict["rule"] == "tainted-writes" and verdict["taint"] == STATE
    assert verdict["reason"].endswith("untrusted content from news.example (rule news of rules.json) entered this "
                                      "run at 2026-11-04T10:00:00Z")
    assert rules.http(facts("api.example", "GET", taint=STATE))["rule"] is None  # reading is not a write
    assert rules.http(facts("api.provider.example", "POST", taint=STATE))["rule"] is None
    # The provider's name claimed in a Host header elsewhere: held.
    assert rules.http(facts("api.provider.example", "POST", origin="203.0.113.7:80", taint=STATE))["decision"] == "ask"
    assert rules.mcp("files", "write_file", {}, None)["rule"] is None
    assert rules.mcp("files", "write_file", {}, STATE)["decision"] == "ask"
    assert rules.mcp("files", "Write_File", {}, STATE)["decision"] == "ask"
    assert not rules.hidden("files", "write_file")  # held in tainted runs only: still listed


# --- the taint ------------------------------------------------------------------------------------------

def test_the_taint_is_kept_once_and_only_the_human_takes_it_back(tmp_path):
    taint = rt.Taint(mirror=tmp_path / "taint.json", log=tmp_path / "connections.jsonl", clock=lambda: 1793800000)
    assert taint.shown() == {"tainted": False} and not (tmp_path / "taint.json").exists()
    first = taint.mark({"layer": "proxy", "rule": "news", "ruleset": "r", "what": "news.example", "other": "x"})
    assert first["tainted"] and first["source"] == {"layer": "proxy", "rule": "news", "ruleset": "r", "what": "news.example"}
    later = taint.mark({"layer": "mcp", "rule": "fetch", "what": "the MCP tool fetch of web"})
    assert later["since"] == first["since"] and later["source"] == first["source"] and later["sources"] == 2
    assert json.loads((tmp_path / "taint.json").read_text())["since"] == first["since"]
    assert oct((tmp_path / "taint.json").stat().st_mode & 0o777) == "0o644"  # the proxy's user reads it
    assert taint.reset()["tainted"] and taint.shown() == {"tainted": False}
    events = [json.loads(line) for line in (tmp_path / "connections.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["taint", "untaint"] and events[1]["was"]["sources"] == 2
    # Escaped: what the agent chose cannot add lines to a question or the log.
    assert taint.mark({"what": "x\nFAKE"})["source"]["what"] == "x%0AFAKE"
    # A new run starts untainted (laundering through a file into the next run is not seen: see the README).
    assert rt.Taint().shown() == {"tainted": False}


# --- the proxy: a response from an untrusted host taints before the agent reads it ---------------------------

class Broker:
    def __init__(self):
        self.asked = []

    def ask(self, kind, details, wait=True):
        self.asked.append((kind, details))
        return {"decision": "deny", "scope": "once", "reason": "answered"}


def supervised(proxy, taint, broker=None, delay=0.0):
    """The supervisor's end of the proxy's channel (serve_asks with TAINT), answering after DELAY seconds."""
    proxy_end, supervisor_end = socket.socketpair()
    if delay:
        original = taint.mark

        def slow(source):
            time.sleep(delay)
            return original(source)
        taint.mark = slow
    threading.Thread(target=rt.serve_asks, args=(supervisor_end, broker, None, taint), daemon=True).start()
    channel = rt.AskChannel(proxy_end)
    proxy.server.who, proxy.server.asker = channel, channel if broker else None
    proxy.server.taint_file = taint.mirror


def plain(port, method, host, body=b""):
    with socket.create_connection(("127.0.0.1", port)) as client:
        client.sendall(f"{method} /x HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                       .encode() + body)
        client.settimeout(10)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
        return data


def rules_config(**extra):
    return {"rules": [{"name": "rules.json", **rt.Ruleset.validated(RULES, "rules.json")}],
            "providers": ["example.test"], **extra}


@needs_openssl
def test_an_untrusted_response_holds_later_writes(tmp_path, pki):  # noqa: F811
    upstream = tp.serve(tp.Handler)
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], rules_config())
    taint, broker = rt.Taint(mirror=tmp_path / "taint.json", log=tmp_path / "connections.jsonl"), Broker()
    supervised(proxy, taint, broker)
    try:
        assert plain(proxy.port, "POST", "api.example", b"{}").startswith(b"HTTP/1.1 201")  # not tainted yet
        assert plain(proxy.port, "GET", "news.example").startswith(b"HTTP/1.1 200")
        assert taint.shown()["source"]["what"] == "news.example"
        answer = plain(proxy.port, "POST", "api.example", b'{"leak": 1}')
        assert answer.startswith(b"HTTP/1.1 403") and b"untrusted content from news.example" in answer
        kind, details = broker.asked[0]
        assert kind == "http" and details["taint"]["source"]["rule"] == "news"
        # Plain http: the question names the address it goes to; the Host header is only a claim (review 5 M1).
        assert ap.describe({"kind": kind, **details}).startswith(
            f"POST http://127.0.0.1:{upstream.server_address[1]}/x (rule tainted-writes of rules.json); its Host "
            "header names api.example (not verified): a server that routes by it may pass the request on there; "
            "asked because untrusted content from news.example entered this run at ")
        records = proxy.records(until=lambda r: r.get("rule") == "tainted-writes")
        held = next(r for r in records if r.get("rule") == "tainted-writes")
        assert held["tainted"] == taint.shown()["since"] and held["decision"] == "ask"
        # Taken back by the human: writes pass again.
        taint.reset()
        assert plain(proxy.port, "POST", "api.example", b"{}").startswith(b"HTTP/1.1 201")
    finally:
        proxy.close()
        upstream.shutdown()


@needs_openssl
def test_the_agents_provider_is_never_held_but_its_name_in_a_host_header_is(tmp_path, pki):  # noqa: F811
    upstream = tp.serve(tp.Handler, pki["server"])
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], rules_config())
    taint = rt.Taint(mirror=tmp_path / "taint.json")
    taint.mark({"what": "news.example"})
    supervised(proxy, taint, Broker())
    try:
        answer = tp.https_request(proxy.port, pki["ca"], b"POST /v1/messages HTTP/1.1\r\nHost: example.test\r\n"
                                                         b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
        assert answer.startswith(b"HTTP/1.1 201")  # the provider (example.test here), verified by its certificate
    finally:
        proxy.close()
        upstream.shutdown()
    plain_upstream = tp.serve(tp.Handler)
    proxy = tp.Proxy(tmp_path, pki, plain_upstream.server_address[1], rules_config())
    supervised(proxy, taint, Broker())
    try:  # plain HTTP to some address claiming to be the provider: held
        assert plain(proxy.port, "POST", "example.test", b"{}").startswith(b"HTTP/1.1 403")
    finally:
        proxy.close()
        plain_upstream.shutdown()


@needs_openssl
def test_the_answer_waits_for_the_taint_to_be_recorded(tmp_path, pki):  # noqa: F811
    """No race between reading untrusted content and the next request: the response's head reaches the agent only
    once the supervisor has recorded the taint."""
    upstream = tp.serve(tp.Handler)
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], rules_config())
    taint = rt.Taint(mirror=tmp_path / "taint.json")
    supervised(proxy, taint, Broker(), delay=0.5)
    try:
        started = time.monotonic()
        assert plain(proxy.port, "GET", "news.example").startswith(b"HTTP/1.1 200")
        assert time.monotonic() - started >= 0.5 and taint.shown()["tainted"]
        assert plain(proxy.port, "POST", "api.example", b"{}").startswith(b"HTTP/1.1 403")
    finally:
        proxy.close()
        upstream.shutdown()


# --- the supervisor's MCP relay: per run, with the run's questions and taint -----------------------------------

FAKE = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message or "method" not in message:
        continue
    result = {"content": [{"type": "text", "text": "ran " + message["params"]["name"]}], "isError": False} \
        if message["method"] == "tools/call" else {}
    if message["method"] == "tools/call" and message["params"].get("arguments", {}).get("other_id"):
        # answers with an id that matches nothing pending (a server that rewrote it)
        print(json.dumps({"jsonrpc": "2.0", "id": "rewritten", "result": result}), flush=True)
        continue
    if message["method"] == "tools/call" and message["params"].get("arguments", {}).get("deep"):
        # nested deeper than Python's json can parse: the relay cannot read it
        print('{"jsonrpc": "2.0", "id": %s, "result": {"x": ' % json.dumps(message["id"]) + "[" * 100000
              + "]" * 100000 + "}}", flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.fixture
def relay(tmp_path):
    directory = Path(tempfile.mkdtemp(prefix="relay-", dir="/tmp"))
    taint, broker = rt.Taint(), Broker()
    for name in ("web", "files"):  # the servers' own sockets (their gateways have no rules here)
        (directory / "up" / name).mkdir(parents=True)
        threading.Thread(target=rt.mcp_serve, args=(str(directory / "up" / name / "server.sock"),
                                                    [os.sys.executable, "-c", FAKE]),
                         kwargs={"cwd": "/", "out": io.StringIO()}, daemon=True).start()
    for _ in range(200):
        if all((directory / "up" / name / "server.sock").exists() for name in ("web", "files")):
            break
        time.sleep(0.02)
    checked = {"name": "rules.json", **rt.Ruleset.validated(RULES, "rules.json")}
    relays = {name: {"server": name, "rules": [checked]} for name in ("web", "files")}
    rt.start_mcp_relays(relays, broker, taint, run_dir=directory / "run", upstream_dir=directory / "up",
                        log=directory / "mcp-calls.jsonl")
    yield {"dir": directory, "taint": taint, "broker": broker}
    shutil.rmtree(directory, ignore_errors=True)


def session(path, *messages):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(20)
        client.connect(str(path))
        for message in messages:
            client.sendall((json.dumps(message) + "\n").encode())
        client.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    return [json.loads(line) for line in data.splitlines()]


def call(name, ident=1, **arguments):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_an_untrusted_tool_result_taints_the_run_and_holds_chosen_tools(relay):
    files, web = relay["dir"] / "run/files/server.sock", relay["dir"] / "run/web/server.sock"
    assert session(files, call("write_file", path="/work/a"))[0]["result"]["isError"] is False  # not tainted yet
    assert session(web, call("fetch", url="https://news.example/"))[0]["result"]["content"][0]["text"] == "ran fetch"
    assert relay["taint"].shown()["source"] == {"layer": "mcp", "rule": "fetch", "ruleset": "rules.json",
                                                "what": "the MCP tool fetch of web"}
    reply = session(files, call("write_file", path="/work/b"))[0]["result"]
    assert reply["isError"] and "not approved (answered)" in reply["content"][0]["text"]
    kind, details = relay["broker"].asked[0]
    assert kind == "mcp" and details["tool"] == "write_file" and details["server"] == "files"
    assert "asked because untrusted content from the MCP tool fetch of web entered this run" in \
        ap.describe({"kind": kind, **details})
    records = [json.loads(line) for line in (relay["dir"] / "mcp-calls.jsonl").read_text().splitlines()]
    held = [r for r in records if r.get("event") == "call" and r.get("rule") == "tainted-files"]
    assert held and held[0]["tainted"] and held[0]["approval"]["decision"] == "deny"
    assert session(files, call("read_file", path="/work/a"))[0]["result"]["isError"] is False  # not a chosen tool


def test_the_relay_cannot_be_gone_around(relay, monkeypatch, tmp_path):
    """The servers' own sockets lie in MCP_UPSTREAM_DIR, which Firejail hides from the agent; its bridge is pointed
    at the relay."""
    upstream = tmp_path / "mcp-upstream"
    upstream.mkdir()
    monkeypatch.setattr(rt, "MCP_UPSTREAM_DIR", upstream)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **kw: type("R", (), {"stdout": ""})())
    assert f"--blacklist={upstream}" in rt.firejail_command({})
    assert s.mcp_bridge_args("files")[-1] == "/run/sandbox-mcp/files/server.sock"
    assert s.MCP_AGENT_DIR == str(rt.MCP_RUN_DIR) and s.MCP_UPSTREAM_DIR == "/sandbox/mcp-upstream"


# --- control socket, net, activity, launcher ------------------------------------------------------------

def test_net_shows_the_taint_and_untaint_takes_it_back(tmp_path):
    taint = rt.Taint()
    taint.mark({"layer": "proxy", "rule": "news", "ruleset": "rules.json", "what": "news.example"})
    directory = Path(tempfile.mkdtemp(prefix="ctl-", dir="/tmp"))
    stop = threading.Event()
    network = rt.RuntimeNetwork({}, None, lambda rules: None)
    threading.Thread(target=rt.control_server, args=(str(directory / "c.sock"), network, stop, ()),
                     kwargs={"taint": taint},
                     daemon=True).start()
    try:
        for _ in range(200):
            if (directory / "c.sock").exists():
                break
            time.sleep(0.02)

        def request(op):
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(directory / "c.sock"))
                client.sendall((json.dumps({"op": op}) + "\n").encode())
                return json.loads(client.makefile("r").readline())
        shown = request("show")
        assert shown["taint"]["tainted"] and shown["taint"]["source"]["what"] == "news.example"
        lines = []
        net.show(shown, lines.append, run="claude-1a2b3c4d")
        assert any("untrusted content: tainted since" in line and "by news.example, rule news of rules.json" in line
                   for line in lines)
        assert any("sandbox-ai net untaint claude-1a2b3c4d" in line for line in lines)
        answer = request("untaint")
        assert answer["was"]["tainted"] and answer["taint"] == {"tainted": False} and not taint.shown()["tainted"]
        assert request("show")["taint"] == {"tainted": False}
    finally:
        stop.set()
        shutil.rmtree(directory, ignore_errors=True)
    assert net.tainted_text({"tainted": False}) == "not tainted"
    assert net.tainted_text({**STATE, "sources": 3}).endswith("(and 2 later sources)")


def test_the_activity_timeline_shows_taint_events(tmp_path):
    activity = load("activity_taint_test", "activity.py")
    log = tmp_path / "connections-1.jsonl"
    log.write_text(json.dumps({"time": "2026-11-04T10:00:00Z", "event": "taint", "taint": STATE}) + "\n" +
                   json.dumps({"time": "2026-11-04T10:05:00Z", "event": "untaint", "by": "the human", "was": STATE}) + "\n")
    source = activity.Connections(tmp_path, "connections-1.jsonl")
    events = source.poll([1 << 20], time.time(), final=True)
    assert [e["summary"] for e in events] == ["TAINTED: untrusted content from news.example entered the run",
                                              "taint taken back by the human"]


def test_the_launcher_hands_the_rules_to_the_proxy_and_the_relays(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "rules.json").write_text(json.dumps(RULES))
    a = s.parse_args(["claude", "--rules", str(tmp_path / "rules.json"), "--frontier"])
    assert s.provider_hosts(a)[:2] == ["api.anthropic.com", "console.anthropic.com"] and "api.openai.com" in \
        s.provider_hosts(a)
    config = s.proxy_config(a, {}, "l")
    assert config["providers"] == s.provider_hosts(a)
    assert config["rules"][0]["untrusted"] == [{"id": "news", "host": ["news.example", "*.forum.example"],
                                                "path": ["/**"]}]
    relay = s.mcp_relay_config(a, "files")
    assert [rule["id"] for rule in relay["rules"][0]["mcp"]] == ["tainted-files"] and relay["rules"][0]["untrusted"] == []
    assert s.mcp_relay_config(a, "web")["rules"][0]["untrusted"][0]["id"] == "fetch"
    assert s.mcp_relay_config(a, "other") == {"server": "other", "rules": []}


@needs_openssl
def test_canary_scan_rules_and_taint_work_together(tmp_path, pki):  # noqa: F811
    """One pass of the proxy's exchange(): the rules (with the taint), the canary scan and the taint's ordering.
    An untrusted response taints with canary scanning on, a canary token leaving is refused in a tainted run as in
    any other, and a tainted write is held by its rule before a byte of it reaches the server."""
    canary = load("canary_taint_test", "canary.py")
    token = canary.github_token()
    forms = [[text, "gh", encoding] for text, encoding in canary.forms(token).items()]
    names = [[text, "gh", encoding] for text, encoding in canary.name_forms(token).items()]
    upstream = tp.serve(tp.Handler)
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], rules_config(
        canary={"forms": forms, "names": names, "block": True, "model_hosts": []}))
    taint, broker = rt.Taint(mirror=tmp_path / "taint.json"), Broker()
    supervised(proxy, taint, broker)
    try:
        assert plain(proxy.port, "GET", "news.example").startswith(b"HTTP/1.1 200")
        assert taint.shown()["tainted"]
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(f"GET /x?key={token} HTTP/1.1\r\nHost: api.example\r\nConnection: close\r\n\r\n".encode())
            client.settimeout(10)
            refused = b""
            with contextlib.suppress(OSError):
                while chunk := client.recv(65536):
                    refused += chunk
        assert not refused.startswith(b"HTTP/1.1 200")  # the token never left
        record = next(r for r in proxy.records(until=lambda r: r.get("canary")) if r.get("canary"))
        assert record["canary"]["canary"] == "gh" and token not in proxy.log.read_text()
        answer = plain(proxy.port, "POST", "api.example", b'{"x": 1}')
        assert answer.startswith(b"HTTP/1.1 403") and b"untrusted content from news.example" in answer
        assert [kind for kind, _ in broker.asked] == ["http"]
    finally:
        proxy.close()
        upstream.shutdown()


def test_a_result_the_relay_cannot_read_still_taints_the_run(relay):
    """A reply too deep (or too long) to parse may be any pending call's result: the pending untrusted tool counts
    as answered, the run is tainted before the agent reads it (fail closed)."""
    web = relay["dir"] / "run/web/server.sock"
    assert not relay["taint"].shown().get("tainted")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(20)
        client.connect(str(web))
        client.sendall((json.dumps(call("fetch", url="https://x.example/", deep=True)) + "\n").encode())
        client.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := client.recv(1 << 20):
            data += chunk
    assert len(data) > 200000  # passed on as it came
    assert relay["taint"].shown()["tainted"] is True


def test_a_proxy_whose_supervisor_did_not_answer_stays_tainted_until_the_supervisor_decides(tmp_path):
    """observe() taints locally when the supervisor's answer does not come in time; taint() must keep that, also
    with the supervisor's mirror file, until the mirror changes (the supervisor recorded it, or the human untainted)."""
    mirror = tmp_path / "taint.json"
    mirror.write_text(json.dumps({"tainted": False}))
    server = rt.ProxyServer.__new__(rt.ProxyServer)
    server.taint_file, server.taint_mtime, server.taint_state, server.local_taint = mirror, None, {"tainted": False}, None
    assert server.taint() is None
    server.local_taint = {"tainted": True, "source": {"what": "news.example"}, "_mirror": server.taint_mtime}
    assert server.taint()["tainted"] is True, "the timeout must not leave the proxy untainted"
    time.sleep(0.01)
    mirror.write_text(json.dumps({"tainted": False}))  # e.g. the human took it back after the supervisor recorded it
    os.utime(mirror, ns=(time.time_ns(), time.time_ns()))
    assert server.taint() is None and server.local_taint is None


def test_ids_a_server_may_rewrite_are_refused_and_an_unmatched_reply_taints(relay):
    """1.0 (JSON numbers a server may rewrite as 1) and integers beyond 2^53 are refused; a reply whose id matches no
    pending call counts for the pending untrusted calls (fail closed)."""
    web = relay["dir"] / "run/web/server.sock"
    for ident in (1.0, 1 << 60):
        answer = session(web, {**call("fetch", url="https://x.example/"), "id": ident})[0]
        assert answer["error"]["code"] == -32600 and "integer" in answer["error"]["message"]
    assert not relay["taint"].shown().get("tainted")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(20)
        client.connect(str(web))
        client.sendall((json.dumps(call("fetch", ident=7, url="https://x.example/", other_id=True)) + "\n").encode())
        client.shutdown(socket.SHUT_WR)
        while client.recv(65536):
            pass
    assert relay["taint"].shown()["tainted"] is True
