"""Independent feature review 1 of sandbox-ai 0.1.95: each test failed before its fix (docs/releases/v0.1.95.md,
"Feature review 1")."""
import argparse
import importlib.util
import json
import os
from pathlib import Path

import pytest

from test_net import no_login_runtime, sandbox  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ap = load("approvals_review1", "sandbox/approvals.py")
s = load("launcher_review1", "sandbox/agent_sandbox.py")
m = load("mcp_review1", "sandbox/mcp.py")
runtime = load("runtime_review1", "sandbox/runtime.py")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


# --- 1. a refused tool call with long arguments is listed among the refusals -----------------------------------------

def test_a_refused_call_with_long_arguments_is_in_the_blocked_list(tmp_path):
    """The question of a tool call carries its arguments in full (up to 64 KiB, escaped once more in its file: far
    beyond 64 KiB for text outside ASCII). blocked_of_run read only the first 64 KiB of a question file, so such a
    call, refused or unanswered, never showed among the run's refusals (the app's Blocked list, approvals blocked)."""
    directory = tmp_path / "runs" / "claude-1"
    (directory / "approvals").mkdir(parents=True)
    post = {"text": "Grüße aus dem Sandkasten " * 2500}  # about 64 KiB of UTF-8, far more in the file (ü)
    ident = "1700000000-1-1"
    question = {"id": ident, "time": "2026-11-13T10:00:00Z", "kind": "mcp", "server": "bsky", "tool": "post",
                "rule": "posts", "ruleset": "rules.json", "arguments": json.dumps({"text": post["text"][:500] + "..."}),
                "arguments_full": runtime.full_arguments(post)}
    (directory / "approvals" / f"{ident}.json").write_text(json.dumps(question))
    assert (directory / "approvals" / f"{ident}.json").stat().st_size > (1 << 16)
    (directory / "approvals" / f"{ident}.done").write_text(json.dumps(
        {"id": ident, "decision": "deny", "scope": "once", "reason": "no answer within 60 s"}))
    found = ap.blocked_of_run("claude-1", {"run": "claude-1", "directory": str(directory)})
    assert [entry["question"] for entry in found] == [ident], found


def test_an_answered_call_with_long_arguments_is_in_the_activity_timeline(tmp_path):
    """The run's activity log records every answered question (activity.Approvals) - but read a question file only
    up to 64 KiB, so an answered tool call with long arguments never appeared there (and was opened again at every
    look, as it never counted as done)."""
    activity = load("activity_review1", "sandbox/activity.py")
    (tmp_path / "approvals").mkdir()
    post = {"text": "Grüße aus dem Sandkasten " * 2500}
    ident = "1700000000-1-1"
    (tmp_path / "approvals" / f"{ident}.json").write_text(json.dumps({
        "id": ident, "time": "2026-11-13T10:00:00Z", "kind": "mcp", "server": "bsky", "tool": "post", "rule": "posts",
        "ruleset": "rules.json", "arguments": json.dumps({"text": post["text"][:500] + "..."}),
        "arguments_full": runtime.full_arguments(post)}))
    assert (tmp_path / "approvals" / f"{ident}.json").stat().st_size > (1 << 16)
    (tmp_path / "approvals" / f"{ident}.done").write_text(json.dumps({"decision": "allow", "scope": "once"}))
    poller = activity.Approvals(tmp_path)
    events = poller.poll(None, 0)
    assert len(events) == 1 and "call the MCP tool post of bsky" in events[0]["summary"], events
    assert ident in poller.done


# --- 2. telemetry of the reviewed agents stays blocked without the proxy -----------------------------------------------

def start(agent, **more):
    return argparse.Namespace(**{"agent": agent, "allow_host": [], "no_inspect": [], "allow": [], "allow_tcp": [],
                                 "egress_all_allow": False, "no_proxy": False, "offline": False, **more})


@pytest.mark.parametrize("agent", ["hermes", "qwen", "dsh", "goose"])
def test_without_the_proxy_no_public_network_is_opened_for_an_agent_whose_telemetry_is_blocked(agent):
    """--egress-all-allow with --no-proxy was refused (nothing refuses the telemetry hosts then), but --allow
    0.0.0.0/0 (or any public network, or --allow-tcp to a public address) with --no-proxy opened the same: the
    telemetry hosts' addresses, with no proxy to refuse them."""
    for options in ({"allow": ["0.0.0.0/0"]}, {"allow": ["::/0"]}, {"allow": ["104.16.0.0/12"]},
                    {"allow_tcp": ["18.197.1.2:443"]}, {"allow": ["10.0.0.0/8", "8.8.8.0/24"]}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            s.check_telemetry_unproxied(start(agent, no_proxy=True, **options))
    # Your own networks stay possible without the proxy, and every network with it (it refuses the hosts).
    s.check_telemetry_unproxied(start(agent, no_proxy=True, allow=["10.0.0.0/8", "192.168.1.0/24", "fd00::/8"],
                                      allow_tcp=["192.168.1.5:11434", "[fe80::1]:8080"]))
    s.check_telemetry_unproxied(start(agent, allow=["0.0.0.0/0"], allow_tcp=["18.197.1.2:443"]))
    # Other agents' telemetry is switchable (--telemetry): not this check's.
    s.check_telemetry_unproxied(start("claude", no_proxy=True, allow=["0.0.0.0/0"]))


# --- 3. a start's own MCP variable never puts a secret's value into a definition -----------------------------------

IMAGE = "ghcr.io/example/bsky@sha256:" + "a" * 64


def test_mcp_env_takes_no_plain_value_for_a_variable_its_definition_takes_from_a_secret():
    """--mcp-env NAME:VAR=VALUE is "checked as mcp add -e NAME=VALUE" (README), which never keeps a plain value for a
    secret: a variable whose definition takes it from a secret (env_secrets: BSKY_APP_PW@bsky) took a plain value
    whose name did not say so - the password ended up in the instance's definition, its state and a saved board."""
    definition = {"image": IMAGE, "env": [], "env_secrets": {"BSKY_APP_PW": "bsky"}, "env_values": {"LANG_X": "de"}}
    with pytest.raises(ValueError, match="secret"):
        m.with_start_env("bsky", definition, {"BSKY_APP_PW": ("value", "hunter2hunter2")})
    # Another secret in its place, and a plain value for a variable that is no secret, stay possible.
    assert m.with_start_env("bsky", definition, {"BSKY_APP_PW": ("secret", "bsky_other")})["env_secrets"] == {
        "BSKY_APP_PW": "bsky_other"}
    assert m.with_start_env("bsky", definition, {"LANG_X": ("value", "en")})["env_values"] == {"LANG_X": "en"}


def test_llm_traffic_shows_an_answer_s_finish_reason_escaped():
    """Every text of the log is shown escaped (a model server's answer may hold escape sequences), but the answer's
    finish reason - the server's own word, up to 100 characters - went to the terminal and the Markdown report raw."""
    traffic = load("llm_traffic_review1", "sandbox/llm_traffic.py")
    record = {"version": 1, "n": 1, "time": "2026-11-13T10:00:00Z", "api": "chat", "model": "m", "status": 200,
              "request": {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
              "response": {"text": "ok", "finish_reason": "stop\x1b]0;owned\x07\x1b[2J"}}
    for text in ("\n".join(traffic.transcript(record)), "\n".join(traffic.transcript(record, full=True)),
                 traffic.markdown([record])):
        assert "\x1b" not in text and "\x07" not in text and "stop\\x1b]0;owned" in text


def test_net_allow_opens_no_telemetry_host_or_public_network_for_such_an_agent_without_the_proxy(sandbox, capsys):  # noqa: F811
    """At the start these are refused (hermes.check_telemetry, check_telemetry_unproxied), but `sandbox-ai net allow`
    during a run without the proxy opened them: the telemetry host itself, or a public network holding it."""
    import test_net as tn
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "agent": "hermes", "proxy": False}))
    for argv in (["--allow-host", "telemetry.nousresearch.com"], ["--allow", "0.0.0.0/0"],
                 ["--allow-tcp", "18.197.1.2:443"], ["--allow-host", "eu.i.posthog.com"]):
        assert tn.run_net(["allow", *argv])[0] == 1, argv
        assert "telemetry is always off" in capsys.readouterr().err, argv
    assert sandbox["applied"] == []
    assert tn.run_net(["allow", "--allow", "192.168.7.0/24"])[0] == 0  # a network of your own
    # With the proxy its rules refuse the telemetry hosts whatever is opened.
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "agent": "hermes", "proxy": True}))
    assert tn.run_net(["allow", "--allow", "203.0.113.0/24"])[0] == 0
