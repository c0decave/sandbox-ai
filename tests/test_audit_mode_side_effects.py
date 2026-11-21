"""An audit ruleset ("mode": "audit") only records what its rules would decide: it has no effect of its own on a run.

Found open after 0.1.132 (the Knostic follow-up): an audit ruleset with rules for the agent's own tools switched the
agent's tool hooks on (managed hooks: Claude Code's plugin hooks then need approval) and refused OpenHands and
nanobot starts, which have no hook layer; and the proxy read request bodies for an audit rule with a body condition
(held back until the body was there). Now: hooks only for enforcing tool rules (an audit ruleset's tool rules are
recorded when an enforcing one switched them on, said at the start otherwise); bodies only for enforcing rules - an
audit rule with a body condition is matched without the body then, its record says "body": "not read", and
`sandbox-ai rules audit` counts those. End to end: ./sandbox-ai starts (--dry-run), the real proxy on real sockets."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import test_proxy as tp
from test_proxy import pki  # noqa: F401  (fixture)
from test_rules import checked, needs_openssl, raw_exchange, recording, rule

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from live_fakes import Host  # noqa: E402

rt = tp.rt
TOOLS = {"version": 1, "tool": [{"id": "no-rm", "tool": "Bash", "args": {"command": {"pattern": "rm .*"}},
                                 "decision": "ask"}]}


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ruleset_file(tmp_path, document, name):
    folder = tmp_path / "rules"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(document))
    return path


@pytest.mark.parametrize("agent", ["openhands", "nanobot"])
def test_an_audit_ruleset_does_not_refuse_an_agent_without_hooks(tmp_path, agent):
    host = Host(tmp_path / "host")
    audit = ruleset_file(tmp_path, {**TOOLS, "mode": "audit"}, "audit.json")
    started = host.run(agent, "--rules", str(audit), "--dry-run")
    assert started.returncode == 0, started.stderr[-2000:]
    assert "cannot be enforced" not in started.stderr
    assert "audit.json" in started.stderr and "tool rules record nothing" in started.stderr
    enforcing = ruleset_file(tmp_path, TOOLS, "enforce.json")
    refused = host.run(agent, "--rules", str(enforcing), "--dry-run")
    assert refused.returncode != 0 and "cannot be enforced" in refused.stderr


def test_an_audit_ruleset_alone_switches_no_tool_hooks_on(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    launcher = load("agent_sandbox_audit_hooks", "agent_sandbox.py")
    audit = ruleset_file(tmp_path, {**TOOLS, "mode": "audit"}, "audit.json")
    enforcing = ruleset_file(tmp_path, {"version": 1, "tool": [{"id": "x", "tool": "WebFetch", "decision": "deny"}]},
                             "enforce.json")
    alone = launcher.parse_args(["claude", "--rules", str(audit)])
    assert launcher.hooks_active(alone) is False and launcher.hooks_config(alone) is None
    both = launcher.parse_args(["claude", "--rules", str(audit), "--rules", str(enforcing)])
    assert launcher.hooks_active(both) is True
    names = [source["name"] for source in launcher.hooks_config(both)["rules"]]
    assert any("audit.json" in name for name in names), "beside an enforcing one, the audit rules are recorded too"


def test_the_proxy_reads_a_body_only_for_an_enforcing_rule():
    audit = rt.Ruleset([{**checked({"version": 1, "mode": "audit", "http": [
        rule("big-release", "ask", host="api.example", methods="POST", path="/releases",
             body={"type": "object", "required": ["draft"]})]}, "trial"), "name": "trial"}])
    facts = rt.Ruleset.request("api.example", "POST", "/releases", [["Host", "api.example"]], None)
    assert audit.needs_body(facts) is False
    enforcing = rt.Ruleset([{**checked({"version": 1, "http": [
        rule("big-release", "ask", host="api.example", methods="POST", path="/releases",
             body={"type": "object", "required": ["draft"]})]}, "real"), "name": "real"}])
    assert enforcing.needs_body(facts) is True


@needs_openssl
def test_an_audit_body_rule_is_recorded_as_matched_without_the_body(tmp_path, pki):  # noqa: F811
    handler, seen = recording()
    upstream = tp.serve(handler)
    trial = checked({"version": 1, "mode": "audit", "http": [
        rule("drafts", "deny", host="api.example", methods="POST", path="/releases",
             body={"type": "object", "required": ["draft"]})]}, "trial")
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], {"rules": [trial]})
    try:
        body = b'{"tag_name": "v1"}'
        answer = raw_exchange(proxy.port, b"POST /releases HTTP/1.1\r\nHost: api.example\r\nContent-Type: "
                              b"application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        assert answer.startswith(b"HTTP/1.1 201") and seen[0][2] == body, "audit decides nothing"
        record = proxy.records(until=lambda r: r.get("audit"))[-1]
        assert record["decision"] == "allow"
        assert record["audit"] == [{"would": "deny", "rule": "drafts", "ruleset": "trial", "body": "not read",
                                    "reason": record["audit"][0]["reason"]}]
    finally:
        proxy.close()
        upstream.shutdown()


@needs_openssl
def test_beside_an_enforcing_body_rule_the_audit_rule_sees_the_body(tmp_path, pki):  # noqa: F811
    handler, seen = recording()
    upstream = tp.serve(handler)
    trial = checked({"version": 1, "mode": "audit", "http": [
        rule("drafts", "deny", host="api.example", methods="POST", path="/releases",
             body={"type": "object", "required": ["draft"]})]}, "trial")
    real = checked({"version": 1, "http": [
        rule("tags", "allow", host="api.example", methods="POST", path="/releases",
             body={"type": "object", "required": ["tag_name"]})]}, "real")
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], {"rules": [real, trial]})
    try:
        body = b'{"tag_name": "v1"}'
        answer = raw_exchange(proxy.port, b"POST /releases HTTP/1.1\r\nHost: api.example\r\nContent-Type: "
                              b"application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        assert answer.startswith(b"HTTP/1.1 201") and seen[0][2] == body
        record = proxy.records(until=lambda r: r.get("rule") == "tags")[-1]
        assert "audit" not in record, "the body was read: no draft in it, the audit rule does not match"
    finally:
        proxy.close()
        upstream.shutdown()


def test_rules_audit_counts_what_was_matched_without_the_body(tmp_path):
    log = tmp_path / "logs"
    log.mkdir()
    lines = [{"time": "2026-11-19T01:00:00Z", "event": "http", "method": "POST", "url": "https://api.example/x",
              "decision": "allow", "audit": [{"would": "deny", "rule": "drafts", "ruleset": "trial",
                                              "body": "not read", "reason": "r"}]},
             {"time": "2026-11-19T01:01:00Z", "event": "http", "method": "POST", "url": "https://api.example/x",
              "decision": "allow", "audit": [{"would": "deny", "rule": "drafts", "ruleset": "trial", "reason": "r"}]}]
    (log / "connections-1.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    done = subprocess.run([str(ROOT / "sandbox-ai"), "rules", "audit", "--log-dir", str(log), "--json"],
                          capture_output=True, text=True, timeout=120, env={**os.environ})
    assert done.returncode == 0, done.stderr
    [hit] = json.loads(done.stdout)["hits"]
    assert hit["count"] == 2 and hit["unread_body"] == 1
    text = subprocess.run([str(ROOT / "sandbox-ai"), "rules", "audit", "--log-dir", str(log)], capture_output=True,
                          text=True, timeout=120).stdout
    assert "1 of them matched without the request body" in text
