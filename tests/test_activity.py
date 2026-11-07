"""sandbox-ai activity: the timeline of what the agents did. Transcript fixtures per agent are small and
hand-written from the formats verified at Claude Code 2.1.317, Codex rust-v0.166.0 (legacy and paginated
history) and OpenCode v1.19.5 (SQLite) / v1.1.48 (JSON files)."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("activity_test", ROOT / "sandbox" / "activity.py")
activity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activity)


# Fake secrets for the redaction checks, assembled at run time so that no token-shaped text is in this file.
GITHUB_TOKEN = "gh" + "p_" + "abcdefghijklmnopqrstuvwx"
GITHUB_TOKEN2 = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123"
KEY_HEADER = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
KEY_FOOTER = "-----END OPENSSH " + "PRIVATE KEY-----"
URL_WITH_PASSWORD = "https://user" + ":" + "hunter2pass" + "@example.org/r.git"


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as stream:
        for record in records:
            stream.write((record if isinstance(record, str) else json.dumps(record)) + "\n")


def collect(home, **options):
    """Every record the transcripts in HOME give, read from their start, with a final read."""
    records = []
    collector = activity.Collector(home=home, sink=records.extend, from_start=True, run="r1", agent="claude",
                                   **options)
    while collector.poll(final=True)[1]:
        pass
    return records


def by_kind(records, kind):
    return [r for r in records if r["kind"] == kind]


# -- Claude Code --

CLAUDE_SESSION = "11111111-2222-3333-4444-555555555555"


def claude_lines():
    common = {"sessionId": CLAUDE_SESSION, "cwd": "/work", "version": "2.1.317", "isSidechain": False}
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
             "output_tokens": 50, "output_tokens_details": {"thinking_tokens": 5}}
    return [
        {**common, "type": "permission-mode", "permissionMode": "default"},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:00.000Z", "origin": {"kind": "human"},
         "message": {"role": "user", "content": "fix the tests"}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:00.100Z", "isMeta": True,
         "message": {"role": "user", "content": "<system-reminder>ignore me</system-reminder>"}},
        # One answer in two lines (one per content block), both with the same usage: counted once.
        {**common, "type": "assistant", "timestamp": "2026-11-04T10:00:01.000Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "role": "assistant", "usage": usage,
                     "content": [{"type": "text", "text": "Running the tests."}]}},
        {**common, "type": "assistant", "timestamp": "2026-11-04T10:00:01.500Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "role": "assistant", "usage": usage,
                     "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                  "input": {"command": "pytest -q tests", "description": "Run tests"}}]}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:04.000Z",
         "permissionDecision": {"decision": "accept", "source": "config", "reasonType": "mode"},
         "toolUseResult": {"stdout": "2 failed", "stderr": "", "interrupted": False,
                           "bashEditDiff": {"files": [{"filePath": "/work/out.txt", "hunks": []}]}},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                                  "content": "Exit code 1\n2 failed", "is_error": True}]}},
        {**common, "type": "assistant", "timestamp": "2026-11-04T10:00:05.000Z",
         "message": {"id": "msg_2", "model": "claude-opus-5-5", "role": "assistant", "usage": usage,
                     "content": [{"type": "tool_use", "id": "toolu_2", "name": "Edit",
                                  "input": {"file_path": "/work/a.py", "old_string": "x" * 5000, "new_string": "y"}},
                                 {"type": "tool_use", "id": "toolu_3", "name": "Write",
                                  "input": {"file_path": "/work/new.py", "content": "print(1)\n"}},
                                 {"type": "tool_use", "id": "toolu_4", "name": "mcp__github__get_issue",
                                  "input": {"number": 7, "token": GITHUB_TOKEN}},
                                 {"type": "tool_use", "id": "toolu_5", "name": "Agent",
                                  "input": {"subagent_type": "Explore", "description": "Find the parser",
                                            "prompt": "look"}}]}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:06.000Z",
         "toolUseResult": {"type": "update", "filePath": "/work/a.py"},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}]}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:06.100Z",
         "toolUseResult": {"type": "create", "filePath": "/work/new.py"},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_3", "content": "ok"}]}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:07.000Z", "toolDenialKind": "user-rejected",
         "toolUseResult": "User rejected tool use",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_4", "is_error": True,
                                                  "content": "The user doesn't want to proceed with this tool use."}]}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:09.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_5", "content": "found"}]}},
        {**common, "type": "system", "subtype": "turn_duration", "durationMs": 9000,
         "timestamp": "2026-11-04T10:00:10.000Z"},
        {**common, "type": "attachment", "timestamp": "2026-11-04T10:00:11.000Z",
         "attachment": {"type": "queued_command", "prompt": "also update the docs", "commandMode": "prompt"}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:12.000Z", "origin": {"kind": "human"},
         "message": {"role": "user", "content": "<command-message>review</command-message>\n"
                                                "<command-name>/review</command-name>\n<command-args>now</command-args>"}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:13.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
        {**common, "type": "user", "timestamp": "2026-11-04T10:00:14.000Z", "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": "<task-notification>\n<status>completed</status>\n"
                                                "<summary>Agent \"Find the parser\" finished</summary>"}},
        {**common, "type": "assistant", "timestamp": "2026-11-04T10:00:15.000Z",
         "message": {"id": "msg_3", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "toolu_6", "name": "Read",
                                  "input": {"file_path": "/work/README.md"}}]}},
    ]


def claude_home(tmp_path):
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-work"
    jsonl(project / f"{CLAUDE_SESSION}.jsonl", claude_lines())
    subagents = project / CLAUDE_SESSION / "subagents"
    jsonl(subagents / "agent-a1b2c3.jsonl", [
        {"type": "user", "isSidechain": True, "agentId": "a1b2c3", "sessionId": CLAUDE_SESSION,
         "timestamp": "2026-11-04T10:00:05.500Z", "message": {"role": "user", "content": "Find the parser"}},
        {"type": "assistant", "isSidechain": True, "agentId": "a1b2c3", "sessionId": CLAUDE_SESSION,
         "timestamp": "2026-11-04T10:00:06.000Z",
         "message": {"id": "msg_s1", "model": "claude-haiku", "role": "assistant",
                     "usage": {"input_tokens": 3, "output_tokens": 4},
                     "content": [{"type": "tool_use", "id": "toolu_s1", "name": "Grep",
                                  "input": {"pattern": "def parse", "path": "/work"}}]}},
        {"type": "user", "isSidechain": True, "agentId": "a1b2c3", "sessionId": CLAUDE_SESSION,
         "timestamp": "2026-11-04T10:00:06.200Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_s1", "content": "x"}]}},
    ])
    (subagents / "agent-a1b2c3.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "Find"}))
    (project / CLAUDE_SESSION / "tool-results").mkdir()
    jsonl(project / CLAUDE_SESSION / "tool-results" / "x.jsonl", [{"type": "user", "message": {"content": "no"}}])
    return home


def test_claude_transcript(tmp_path):
    records = collect(claude_home(tmp_path))
    main = [r for r in records if not r.get("subagent")]
    prompts = [r["summary"] for r in by_kind(main, "prompt")]
    assert prompts == ["fix the tests", "also update the docs", "/review now"]
    assert [r["summary"] for r in by_kind(main, "reply")] == ["Running the tests."]
    command = by_kind(main, "command")[0]
    assert command["summary"] == "pytest -q tests · exit 1 · 2.5 s"
    assert command["details"]["status"] == "error" and command["details"]["exit_code"] == 1
    assert command["details"]["duration_ms"] == 2500 and command["conversation"] == CLAUDE_SESSION
    assert "command" not in command["details"]["arguments"]  # kept once, as details.command
    files = {r["details"].get("path"): r for r in by_kind(main, "file")}
    assert files["/work/a.py"]["summary"] == "modified /work/a.py (Edit)"
    assert files["/work/a.py"]["details"]["arguments"]["old_string_size"] == 5000  # contents are not copied
    assert files["/work/new.py"]["summary"] == "created /work/new.py (Write)"
    assert files["/work/out.txt"]["details"]["by"] == "command"  # changed by the Bash call (bashEditDiff)
    mcp = by_kind(main, "mcp")[0]
    assert mcp["summary"].startswith("github: get_issue") and mcp["details"]["status"] == "denied"
    assert mcp["details"]["arguments"]["token"] == "[redacted]"
    approval = by_kind(main, "approval")[0]
    assert approval["details"]["answer"] == "denied" and approval["details"]["denial"] == "user-rejected"
    tools = {r["details"].get("tool"): r for r in by_kind(main, "tool")}
    assert tools["Agent"]["summary"] == "subagent Explore: Find the parser"
    assert tools["Read"]["details"]["status"] == "no result recorded"  # never answered: shown at the end
    assert any(r["summary"].startswith("notification: Agent") for r in by_kind(main, "tool"))
    assert by_kind(main, "session")[0]["details"]["event"] == "interrupt"
    tokens = by_kind(main, "tokens")
    assert tokens[0]["details"]["calls"] == 2 and tokens[0]["details"]["usage"]["input_tokens"] == 20
    assert tokens[0]["details"]["duration_ms"] == 9000
    sub = [r for r in records if r.get("subagent")]
    assert {r["subagent"] for r in sub} == {"Explore:a1b2c3"}
    assert by_kind(sub, "prompt")[0]["details"]["by"] == "agent"
    assert by_kind(sub, "tool")[0]["summary"] == "Grep def parse in /work"
    assert not any("tool-results" in json.dumps(r) for r in records)
    assert len({r["id"] for r in records}) == len(records)


# -- Codex --

def codex_legacy():
    return [
        {"timestamp": "2025-12-01T09:00:00.000Z", "type": "session_meta",
         "payload": {"id": "019a-legacy", "timestamp": "2025-12-01T09:00:00.000Z", "cwd": "/work",
                     "originator": "codex_cli_rs", "cli_version": "0.77.0", "model_provider": "openai",
                     "git": {"branch": "main"}}},
        {"timestamp": "2025-12-01T09:00:00.100Z", "type": "turn_context",
         "payload": {"cwd": "/work", "model": "gpt-6.1-sol", "approval_policy": "on-request"}},
        {"timestamp": "2025-12-01T09:00:00.200Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "<environment_context>...</environment_context>"}]}},
        {"timestamp": "2025-12-01T09:00:00.300Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "run the build", "images": None}},
        {"timestamp": "2025-12-01T09:00:01.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "call_1",
                     "arguments": json.dumps({"command": ["bash", "-lc", "make OPENAI_API_KEY=sk-live-123456"],
                                              "workdir": "/work"})}},
        {"timestamp": "2025-12-01T09:00:03.000Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_1",
                     "output": json.dumps({"output": "ok", "metadata": {"exit_code": 0, "duration_seconds": 1.9}})}},
        {"timestamp": "2025-12-01T09:00:04.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "call_id": "call_2", "name": "apply_patch",
                     "input": "*** Begin Patch\n*** Update File: src/a.rs\n@@\n-x\n+y\n*** Add File: src/b.rs\n+z\n"
                              "*** End Patch"}},
        {"timestamp": "2025-12-01T09:00:04.500Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "call_2", "output": "Success."}},
        {"timestamp": "2025-12-01T09:00:05.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "mcp__github__get_issue", "call_id": "call_3",
                     "arguments": "{\"number\": 7}"}},
        {"timestamp": "2025-12-01T09:00:05.500Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_3", "output": "issue text"}},
        {"timestamp": "2025-12-01T09:00:06.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "call_4",
                     "arguments": json.dumps({"command": ["rm", "-rf", "/tmp/x"]})}},
        {"timestamp": "2025-12-01T09:00:07.000Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_4", "output": "exec command rejected by user"}},
        {"timestamp": "2025-12-01T09:00:08.000Z", "type": "response_item",
         "payload": {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "rust"}}},
        {"timestamp": "2025-12-01T09:00:09.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
             "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
                                  "reasoning_output_tokens": 4, "total_tokens": 110}}}},
        {"timestamp": "2025-12-01T09:00:09.100Z", "type": "event_msg",  # a repeat (rate limits): not counted again
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
             "last_token_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}}},
        {"timestamp": "2025-12-01T09:00:10.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "The build passes."}]}},
        {"timestamp": "2025-12-01T09:00:10.100Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "The build passes."}},
        {"timestamp": "2025-12-01T09:00:10.200Z", "type": "event_msg",
         "payload": {"type": "task_complete", "last_agent_message": "The build passes."}},
    ]


def codex_paginated():
    return [
        {"timestamp": "2026-11-04T12:00:00.120Z", "ordinal": 0, "type": "session_meta",
         "payload": {"session_id": "019a-new", "id": "019a-new", "cwd": "/work", "cli_version": "0.166.0",
                     "source": "cli", "history_mode": "paginated"}},
        {"timestamp": "2026-11-04T12:00:01.000Z", "ordinal": 1, "type": "event_msg",
         "payload": {"type": "item_completed", "turn_id": "1",
                     "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "test it"}]}}},
        {"timestamp": "2026-11-04T12:00:05.410Z", "ordinal": 4, "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "call_abc",
                     "arguments": "{\"cmd\":\"cargo test\",\"workdir\":\"/work\"}"}},
        {"timestamp": "2026-11-04T12:00:09.003Z", "ordinal": 6, "type": "event_msg",
         "payload": {"type": "item_completed", "item": {
             "type": "CommandExecution", "id": "call_abc", "command": ["/bin/bash", "-lc", "cargo test"],
             "source": "agent", "status": "completed", "exit_code": 101, "duration": {"secs": 3, "nanos": 501200000}}}},
        {"timestamp": "2026-11-04T12:00:09.002Z", "ordinal": 5, "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_abc",
                     "output": "Chunk ID: 1a2b\nWall time: 3.5012 seconds\nProcess exited with code 101\nOutput:\n"}},
        {"timestamp": "2026-11-04T12:00:10.000Z", "ordinal": 7, "type": "event_msg",
         "payload": {"type": "item_completed", "item": {
             "type": "CommandExecution", "id": "u-shell", "command": ["/bin/bash", "-lc", "ls"], "source": "user_shell",
             "status": "completed", "exit_code": 0}}},
        {"timestamp": "2026-11-04T12:00:11.000Z", "ordinal": 8, "type": "event_msg",
         "payload": {"type": "item_completed", "item": {
             "type": "FileChange", "id": "fc1", "status": "declined",
             "changes": {"/work/secret.txt": {"type": "delete", "content": "x"}}}}},
        {"timestamp": "2026-11-04T12:00:12.000Z", "ordinal": 9, "type": "response_item",
         "payload": {"type": "function_call", "namespace": "mcp__github", "name": "get_issue", "call_id": "call_m1",
                     "arguments": "{\"number\":7}"}},
        {"timestamp": "2026-11-04T12:00:13.000Z", "ordinal": 10, "type": "event_msg",
         "payload": {"type": "item_completed", "item": {
             "type": "McpToolCall", "id": "call_m1", "server": "github", "tool": "get_issue", "status": "completed"}}},
        {"timestamp": "2026-11-04T12:00:13.100Z", "ordinal": 11, "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_m1", "output": [{"type": "input_text",
                                                                                     "text": "issue"}]}},
        {"timestamp": "2026-11-04T12:00:14.000Z", "ordinal": 12, "type": "event_msg",
         "payload": {"type": "turn_aborted", "turn_id": "1", "reason": "interrupted"}},
    ]


def test_codex_rollouts_legacy_and_paginated(tmp_path):
    home = tmp_path / "home"
    jsonl(home / ".codex/sessions/2025/12/01/rollout-2025-12-01T09-00-00-019a-legacy.jsonl", codex_legacy())
    jsonl(home / ".codex/sessions/2026/11/04/rollout-2026-11-04T12-00-00-019a-new.jsonl", codex_paginated())
    jsonl(home / ".codex/sessions/2026/11/04/not-a-rollout.jsonl", codex_legacy())
    records = collect(home, secrets=["sk-live-123456"])
    assert all(r["agent"] == "codex" for r in records)
    legacy = [r for r in records if r.get("conversation") == "019a-legacy"]
    assert [r["summary"] for r in by_kind(legacy, "prompt")] == ["run the build"]  # not the injected context
    assert [r["summary"] for r in by_kind(legacy, "reply")] == ["The build passes."]  # once, not twice
    commands = by_kind(legacy, "command")
    assert commands[0]["summary"].startswith("make OPENAI_API_KEY=[redacted]") and "sk-live" not in json.dumps(records)
    assert commands[0]["details"]["exit_code"] == 0 and commands[0]["details"]["duration_ms"] == 2000
    assert commands[1]["details"]["status"] == "denied"
    assert by_kind(legacy, "approval")[0]["details"]["answer"] == "denied"
    patch = by_kind(legacy, "file")[0]
    assert patch["summary"] == "changed 2 files (apply_patch): src/a.rs, src/b.rs"
    assert patch["details"]["files"] == [{"action": "modified", "path": "src/a.rs"},
                                         {"action": "created", "path": "src/b.rs"}]
    assert by_kind(legacy, "mcp")[0]["summary"] == "github: get_issue"
    assert by_kind(legacy, "tool")[0]["summary"] == "web search rust"
    tokens = by_kind(legacy, "tokens")
    assert len(tokens) == 1 and tokens[0]["details"]["usage"]["input_tokens"] == 100
    assert tokens[0]["details"]["model"] == "gpt-6.1-sol"
    meta = by_kind(legacy, "session")[0]
    assert meta["details"]["cli_version"] == "0.77.0" and meta["details"]["history_mode"] == "legacy"

    new = [r for r in records if r.get("conversation") == "019a-new"]
    assert [r["summary"] for r in by_kind(new, "prompt")] == ["test it"]
    cargo, user_ls = by_kind(new, "command")
    assert cargo["summary"] == "cargo test · exit 101 · 3.6 s" and cargo["details"]["status"] == "error"
    assert user_ls["summary"].startswith("! ls") and user_ls["details"]["by"] == "user"
    assert by_kind(new, "file")[0]["details"]["path"] == "/work/secret.txt"
    assert by_kind(new, "approval")[0]["summary"] == "declined: FileChange"
    assert len(by_kind(new, "mcp")) == 1  # the call, not the completed item again
    assert any(r["details"].get("event") == "interrupt" for r in by_kind(new, "session"))
    assert not [r for r in records if "not-a-rollout" in json.dumps(r)]


def test_codex_subagent_rollout(tmp_path):
    home = tmp_path / "home"
    jsonl(home / ".codex/sessions/2026/11/04/rollout-2026-11-04T12-00-01-019b.jsonl", [
        {"timestamp": "2026-11-04T12:00:01.000Z", "type": "session_meta",
         "payload": {"id": "019b", "cwd": "/work", "parent_thread_id": "019a-new",
                     "source": {"subagent": {"thread_spawn": {"parent_thread_id": "019a-new", "depth": 1,
                                                              "agent_nickname": "reviewer"}}}}},
        {"timestamp": "2026-11-04T12:00:02.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "review the diff"}}])
    records = collect(home)
    assert {r.get("subagent") for r in records} == {"reviewer"}
    assert by_kind(records, "prompt")[0]["details"]["by"] == "agent"


# -- OpenCode --

OPENCODE_SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT, title TEXT,
  version TEXT, agent TEXT, time_created INTEGER, time_updated INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER,
  data TEXT);
"""
T0 = 1762257600000  # 2025-11-04T12:00:00Z in milliseconds


def opencode_rows():
    sessions = [("ses_main", None, "build", "Fix"), ("ses_sub", "ses_main", "explore", "Look around")]
    messages = [
        ("msg_u1", "ses_main", {"role": "user", "time": {"created": T0}, "agent": "build",
                                "model": {"providerID": "anthropic", "modelID": "claude"}}),
        ("msg_a1", "ses_main", {"role": "assistant", "time": {"created": T0 + 1000, "completed": T0 + 9000},
                                "parentID": "msg_u1", "modelID": "claude-sonnet", "providerID": "anthropic",
                                "cost": 0.01, "tokens": {"input": 1200, "output": 340, "reasoning": 0,
                                                         "cache": {"read": 18000, "write": 0}}}),
        ("msg_a2", "ses_main", {"role": "assistant", "time": {"created": T0 + 10000}, "modelID": "claude-sonnet"}),
        ("msg_s1", "ses_sub", {"role": "user", "time": {"created": T0 + 2000}}),
    ]
    parts = [
        ("prt_1", "msg_u1", "ses_main", {"type": "text", "text": "make the tests pass"}),
        ("prt_2", "msg_u1", "ses_main", {"type": "text", "text": "<file>...</file>", "synthetic": True}),
        ("prt_3", "msg_a1", "ses_main", {"type": "text", "text": "Done.", "time": {"start": T0 + 8000, "end": T0 + 8500}}),
        ("prt_4", "msg_a1", "ses_main", {"type": "tool", "callID": "call_1", "tool": "bash", "state": {
            "status": "completed", "input": {"command": "bun test", "workdir": "/work"}, "output": "ok",
            "title": "bun test", "metadata": {"output": "ok", "exit": 0},
            "time": {"start": T0 + 2000, "end": T0 + 6000}}}),
        ("prt_5", "msg_a1", "ses_main", {"type": "tool", "callID": "call_2", "tool": "edit", "state": {
            "status": "completed", "input": {"filePath": "/work/a.ts", "oldString": "a", "newString": "b"},
            "time": {"start": T0 + 6100, "end": T0 + 6200}}}),
        ("prt_6", "msg_a1", "ses_main", {"type": "tool", "callID": "call_3", "tool": "github_get_issue", "state": {
            "status": "completed", "input": {"number": 7}, "time": {"start": T0 + 6300, "end": T0 + 6400}}}),
        ("prt_7", "msg_a1", "ses_main", {"type": "tool", "callID": "call_4", "tool": "bash", "state": {
            "status": "error", "input": {"command": "rm -rf /"},
            "error": "The user rejected permission to use this specific tool call.",
            "time": {"start": T0 + 6500, "end": T0 + 6600}}}),
        ("prt_8", "msg_a1", "ses_main", {"type": "step-finish", "reason": "stop", "cost": 0.01,
                                         "tokens": {"input": 1, "output": 1}}),
        ("prt_9", "msg_a2", "ses_main", {"type": "text", "text": "still typing"}),
        ("prt_10", "msg_s1", "ses_sub", {"type": "text", "text": "list the files"}),
        ("prt_11", "msg_a1", "ses_main", {"type": "patch", "hash": "abc", "files": ["/work/a.ts", "/work/b.ts"]}),
    ]
    return sessions, messages, parts


def opencode_database(path, rows=None, updated=T0):
    sessions, messages, parts = rows or opencode_rows()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(OPENCODE_SCHEMA)
    connection.execute("PRAGMA journal_mode=WAL")
    for ident, parent, agent, title in sessions:
        connection.execute("INSERT INTO session (id, parent_id, agent, title, time_created, time_updated) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (ident, parent, agent, title, updated, updated))
    for ident, session, data in messages:
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (ident, session, updated, updated,
                                                                          json.dumps(data)))
    for ident, message, session, data in parts:
        connection.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)", (ident, message, session, updated, updated,
                                                                          json.dumps(data)))
    connection.commit()
    return connection


def check_opencode(records):
    assert all(r["agent"] == "opencode" for r in records)
    main = [r for r in records if not r.get("subagent")]
    assert [r["summary"] for r in by_kind(main, "prompt")] == ["make the tests pass"]
    replies = [r["summary"] for r in by_kind(main, "reply")]
    assert "Done." in replies
    bun, rm = by_kind(main, "command")
    assert bun["summary"] == "bun test · exit 0 · 4.0 s" and bun["details"]["status"] == "ok"
    assert rm["details"]["status"] == "denied"
    assert by_kind(main, "approval")[0]["details"]["answer"] == "denied"
    files = by_kind(main, "file")
    assert files[0]["summary"] == "modified /work/a.ts (edit)"
    assert any(f["summary"].startswith("changed 2 files (snapshot)") for f in files)
    assert by_kind(main, "mcp")[0]["summary"] == "github: get_issue"
    tokens = by_kind(main, "tokens")
    assert tokens[0]["details"]["usage"] == {"input": 1200, "output": 340, "reasoning": 0, "cache_read": 18000,
                                             "cache_write": 0}
    assert tokens[0]["details"]["cost"] == 0.01
    sub = [r for r in records if r.get("subagent")]
    assert sub and sub[0]["subagent"] == "explore" and sub[0]["details"]["by"] == "agent"


def test_opencode_database(tmp_path):
    home = tmp_path / "home"
    connection = opencode_database(home / ".local/share/opencode/opencode.db")
    records = collect(home)
    check_opencode(records)
    assert "still typing" in [r["summary"] for r in by_kind(records, "reply")]  # the last read takes it as it is
    connection.close()
    # A database that holds views where tables belong is not read.
    other = tmp_path / "other"
    db = other / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True)
    bad = sqlite3.connect(db)
    bad.executescript("CREATE TABLE p (data TEXT); CREATE VIEW part AS SELECT 'x' AS id, 1 AS time_updated, "
                      "'s' AS session_id, 'm' AS message_id, data FROM p; CREATE TABLE message (id TEXT, "
                      "time_updated INTEGER, session_id TEXT, data TEXT);")
    bad.close()
    assert collect(other) == []


def test_opencode_database_is_followed_while_running(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "DATABASE_INTERVAL", 0.0)
    home = tmp_path / "home"
    connection = opencode_database(home / ".local/share/opencode/opencode.db")
    records = []
    collector = activity.Collector(home=home, sink=records.extend, run="r", agent="opencode")
    collector.snapshot()  # what is there already belongs to earlier runs
    collector.poll()
    assert records == []
    later = T0 + 60000
    connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("msg_u9", "ses_main", later, later,
                                                                      json.dumps({"role": "user", "time": {"created": later}})))
    connection.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)", ("prt_90", "msg_u9", "ses_main", later, later,
                                                                      json.dumps({"type": "text", "text": "next step"})))
    connection.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)", ("prt_91", "msg_u9", "ses_main", later, later,
                                                                      json.dumps({"type": "tool", "tool": "bash", "state": {
                                                                          "status": "running", "input": {"command": "sleep 60"},
                                                                          "time": {"start": later}}})))
    connection.commit()
    collector.poll()
    assert [r["summary"] for r in records] == ["next step", "sleep 60 · running"]  # a long call: shown running
    assert records[1]["details"]["status"] == "running"
    connection.execute("UPDATE part SET time_updated = ?, data = ? WHERE id = 'prt_91'", (
        later + 5000, json.dumps({"type": "tool", "tool": "bash", "state": {
            "status": "completed", "input": {"command": "sleep 60"}, "metadata": {"exit": 0},
            "time": {"start": later, "end": later + 5000}}})))
    connection.commit()
    collector.poll()
    assert records[-1]["update"] is True and records[-1]["id"] == records[1]["id"]
    assert records[-1]["details"]["status"] == "ok"
    collector.poll(final=True)
    assert collector.sources[2].scratch is None  # the private copy is gone
    connection.close()


def test_opencode_json_files(tmp_path):
    home = tmp_path / "home"
    storage = home / ".local/share/opencode/storage"
    sessions, messages, parts = opencode_rows()
    for ident, parent, agent, title in sessions:
        path = storage / "session" / "proj1" / f"{ident}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": ident, "parentID": parent, "agent": agent, "title": title}))
    for ident, session, data in messages:
        path = storage / "message" / session / f"{ident}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data, "id": ident, "sessionID": session}))
    for ident, message, session, data in parts:
        path = storage / "part" / message / f"{ident}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data, "id": ident, "sessionID": session, "messageID": message}))
    (storage / "part" / "msg_u1" / "garbage.json").write_text("{not json")
    check_opencode(collect(home))


# -- following, bounds, links, garbage --

def test_only_what_comes_after_the_start_is_this_runs(tmp_path):
    home = claude_home(tmp_path)
    transcript = home / ".claude/projects/-work" / f"{CLAUDE_SESSION}.jsonl"
    records = []
    collector = activity.Collector(home=home, sink=records.extend, run="r2", agent="claude")
    collector.snapshot()
    collector.poll()
    assert records == []  # an earlier conversation in a kept HOME
    jsonl(transcript, [{"type": "user", "sessionId": CLAUDE_SESSION, "timestamp": "2026-11-05T08:00:00Z",
                        "message": {"role": "user", "content": "continue"}}])
    with open(transcript, "a") as stream:
        stream.write('{"type": "user", "message": {"role": "user", "content": "half a li')  # still being written
    collector.poll()
    assert [r["summary"] for r in records] == ["continue"]
    with open(transcript, "a") as stream:
        stream.write('ne"}}\n')
    collector.poll()
    assert [r["summary"] for r in records] == ["continue", "half a line"]


def test_links_are_never_followed(tmp_path):
    secret = tmp_path / "host-secrets"
    jsonl(secret / "projects" / "-work" / "x.jsonl", [{"type": "user", "message": {"content": "host data"}}])
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").symlink_to(secret)  # the agent points its config at a host directory
    assert collect(home) == []
    home2 = tmp_path / "home2"
    (home2 / ".claude" / "projects" / "-work").mkdir(parents=True)
    (home2 / ".claude" / "projects" / "-work" / "x.jsonl").symlink_to(secret / "projects" / "-work" / "x.jsonl")
    (home2 / ".claude" / "projects" / "-y").symlink_to(secret / "projects" / "-work")
    os.mkfifo(home2 / ".claude" / "projects" / "-work" / "fifo.jsonl")  # never blocks the reader
    (home2 / ".codex").symlink_to(secret)
    db = home2 / ".local/share/opencode"
    db.mkdir(parents=True)
    (db / "opencode.db").symlink_to(secret / "projects" / "-work" / "x.jsonl")
    assert collect(home2) == []


def test_garbage_and_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "READ_LIMIT", 4096)
    home = tmp_path / "home"
    transcript = home / ".claude/projects/-work/s.jsonl"
    nested = "[" * 100000 + "]" * 100000
    jsonl(transcript, ["not json", "[1, 2]", "null", nested, json.dumps({"type": "user", "message": "text"}),
                       json.dumps({"type": "assistant", "message": {"content": [None, 1, {"type": "tool_use"}]}}),
                       json.dumps({"type": "user", "message": {"content": "x" * 10000}}),  # longer than READ_LIMIT
                       json.dumps({"type": "user", "timestamp": "garbage", "message": {"content": "\x1b[31mred\x07"}}),
                       json.dumps({"type": "user", "timestamp": 1e300, "message": {"content": "\ud800 lone"}})])
    records = collect(home)
    assert [r["summary"] for r in records] == ["\x1b[31mred\x07", "? lone"]
    out = []
    activity.TextOutput(out.append, colour=False).show(records[0])
    assert "\x1b" not in "".join(out) and "\\x1b[31mred\\x07" in "".join(out)  # escaped for the terminal
    # Budgets: a poll reads at most its budget; the rest comes with the next polls.
    many = home / ".claude/projects/-work/many.jsonl"
    jsonl(many, [{"type": "user", "message": {"content": f"p{i}"}} for i in range(300)])
    found = []
    collector = activity.Collector(home=home, sink=found.extend, from_start=True)
    polls = 0
    while collector.poll(budget=2000)[1] or polls == 0:
        polls += 1
    collector.poll(final=True)
    assert polls > 1 and len([r for r in found if r["summary"].startswith("p")]) == 300


def test_record_bounds_and_redaction():
    redact = activity.Redactor(["s3cr3t-value-123"])
    texts = {
        "curl -H 'Authorization: Bearer abcdefghijklmnop' https://x": "abcdefghijklmnop",
        f"git clone {URL_WITH_PASSWORD}": "hunter2pass",
        "export ANTHROPIC_API_KEY=plainvalue42 && run": "plainvalue42",
        "tool --password=topsecret9 --verbose": "topsecret9",
        f"echo {GITHUB_TOKEN2}": GITHUB_TOKEN2,
        "use s3cr3t-value-123 here": "s3cr3t-value-123",
        f"{KEY_HEADER}\nb3BlbnNzaA\n{KEY_FOOTER}": "b3BlbnNzaA",
        "curl -H 'x-api-key: key-0123456789' https://api": "key-0123456789",
    }
    for text, secret in texts.items():
        assert secret not in redact(text), text
    details = activity.clean({"env": {"API_KEY": "v", "PATH": "/bin"}, "api_key": "v2", "nested": [{"password": "v3"}],
                              "n": 1, "f": float("nan"), "big": "y" * 5000, "deep": [[[[[[[["x"]]]]]]]]}, redact)
    assert details["env"] == ["API_KEY", "PATH"] and details["api_key"] == "[redacted]"
    assert details["nested"][0]["password"] == "[redacted]" and details["f"] is None
    assert len(details["big"]) <= activity.VALUE_LIMIT + 1 and "…" in json.dumps(details["deep"], ensure_ascii=False)
    record = activity.finalize(activity.raw("command", "x" * 1000, details={"command": "y" * 100000}), "r", None,
                               "claude", redact)
    assert len(record["summary"]) <= activity.SUMMARY_LIMIT + 1
    assert len(json.dumps(record)) < activity.RECORD_LIMIT


def test_the_log_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "activity.jsonl"
    records = [activity.finalize(activity.raw("prompt", f"p{i}"), "r", None, "claude", activity.Redactor())
               for i in range(50)]
    assert activity.append(path, records[:10], limit=2000) is True
    while activity.append(path, records[10:20], limit=2000):
        pass
    assert activity.append(path, records[20:], limit=2000) is False
    lines = path.read_text().splitlines()
    assert json.loads(lines[-1])["details"]["event"] == "log-full" and path.stat().st_size < 4000
    (tmp_path / "link.jsonl").symlink_to(tmp_path / "elsewhere.jsonl")
    with pytest.raises(OSError):
        activity.append(tmp_path / "link.jsonl", records[:1])
    assert not (tmp_path / "elsewhere.jsonl").exists()


# -- sandbox-ai's own logs --

def test_sandbox_logs_and_approvals(tmp_path):
    logs, control = tmp_path / "logs", tmp_path / "control"
    jsonl(logs / "files.jsonl", [{"time": "2026-11-04T10:00:00Z", "action": "write", "path": "/work/old.py"}])
    jsonl(logs / "egress-denied.jsonl", [{"time": "2026-11-04T09:00:00Z", "host": "old.example"}])
    (control / "approvals").mkdir(parents=True)
    records = []
    collector = activity.Collector(log_dir=logs, run="claude-1", agent="claude", session="s1", control_dir=control,
                                   connection_log="connections-1-claude-1.jsonl", sink=records.extend)
    collector.start({"summary": "claude started", "agent": "claude"})
    jsonl(logs / "files.jsonl", [
        {"time": "2026-11-04T10:00:01Z", "action": "read", "path": "/work/a.py"},
        {"time": "2026-11-04T10:00:01Z", "action": "create", "path": "/work/new.py"},
        {"time": "2026-11-04T10:00:01Z", "action": "write", "path": "/work/new.py"},
        {"time": "2026-11-04T10:00:02Z", "action": "write", "path": "/work/a.py"},
        {"time": "2026-11-04T10:00:02Z", "action": "write", "path": "/root/.claude/projects/-work/s.jsonl"},
        *[{"time": "2026-11-04T10:00:03Z", "action": "create", "path": f"/work/node_modules/x/f{i}.js"} for i in range(20)],
        {"error": "inotify watches exhausted"}])
    jsonl(logs / "egress-denied.jsonl", [{"time": "2026-11-04T10:00:04Z", "host": "evil.example"},
                                         {"time": "2026-11-04T10:00:04Z", "address": "192.0.2.9", "protocol": "udp",
                                          "port": 53}])
    jsonl(logs / "connections-1-claude-1.jsonl", [
        {"time": "2026-11-04T10:00:05Z", "event": "http", "host": "api.anthropic.com", "method": "POST",
         "url": "https://api.anthropic.com/v1/messages", "status": 200, "process": {"pid": 7, "name": "claude"}},
        *[{"time": "2026-11-04T10:00:06Z", "event": "http", "host": "api.anthropic.com", "method": "POST",
           "url": "https://api.anthropic.com/v1/messages", "status": 200}] * 4,
        {"time": "2026-11-04T10:00:07Z", "event": "denied", "destination": "198.51.100.7:22", "reason": "SSH",
         "rule": {"id": "no-ssh"}, "process": "ssh"},
        {"time": "2026-11-04T10:00:08Z", "event": "http", "url": "https://github.com/o/r.git/git-receive-pack",
         "host": "github.com", "push": {"ok": False, "refs": ["refs/heads/main"]}}])
    (control / "approvals" / "1-2-3.json").write_text(json.dumps({"id": "1-2-3", "kind": "dns", "name": "pypi.org",
                                                                  "time": "2026-11-04T10:00:09Z"}))
    (control / "approvals" / "1-2-3.done").write_text(json.dumps({"decision": "allow", "scope": "run"}))
    (control / "approvals" / "1-2-4.json").write_text(json.dumps({"id": "1-2-4", "kind": "connect",
                                                                  "destination": "203.0.113.5:443"}))
    collector.poll()
    collector.finish(status=0)
    summaries = [r["summary"] for r in records]
    assert records[0]["details"]["event"] == "start" and records[-1]["details"]["event"] == "stop"
    assert records[-1]["details"]["status"] == 0 and records[-1]["summary"].startswith("claude ended (exit status 0)")
    assert all(r["run"] == "claude-1" and r["session"] == "s1" for r in records)
    files = [r["summary"] for r in by_kind(records, "file")]
    assert "created /work/new.py" in files and "modified /work/a.py" in files
    assert "created 20 files in /work/node_modules/x" in files
    assert not any("old.py" in s or ".claude" in s or "read" in s.split()[0] for s in files)
    assert any("inotify" in s for s in files)
    network = by_kind(records, "network")
    texts = [r["summary"] for r in network]
    assert "denied: lookup of evil.example" in texts and "denied: udp to 192.0.2.9:53" in texts
    assert not any("old.example" in t for t in texts)
    assert texts.count("allowed: POST https://api.anthropic.com/v1/messages → 200 (claude)") == 1
    assert "allowed: 4 more request(s) to api.anthropic.com (POST 4)" in texts  # counted, shown at the end
    ssh = next(r for r in network if "198.51.100.7" in r["summary"])
    assert ssh["details"]["rule"] == "no-ssh" and ssh["details"]["process"] == "ssh"
    assert ssh["summary"] == "denied: 198.51.100.7:22 (SSH) (rule no-ssh, ssh)"
    push = next(r for r in network if r["details"].get("event") == "push")
    assert push["summary"].startswith("push REFUSED: refs/heads/main") and push["details"]["verdict"] == "denied"
    approvals = by_kind(records, "approval")
    assert approvals[0]["summary"] == "allow (run): look up pypi.org"
    assert approvals[1]["details"]["answer"] == "unanswered"  # still open when the run ended
    assert len(summaries) == len({r["id"] for r in records})


# -- the launcher and sessions --

def test_launcher_hooks_write_the_timeline(tmp_path, monkeypatch):
    logs, home = tmp_path / "logs", claude_home(tmp_path)
    a = SimpleNamespace(log_dir=logs, run_id="claude-abcd", agent="claude", backend="bwrap", pod_name=None,
                        tmux_socket_dir=None, session=None, state_dir=None, control_dir=None, workspaces=["/p"])
    logs.mkdir()
    collector = activity.start(a, home, None, {"MY_TOKEN": "tok-value-999", "PATH": "/bin"}, ["OTHER"],
                               [{"host": "/p", "sandbox": "/work"}])
    jsonl(home / ".claude/projects/-work" / f"{CLAUDE_SESSION}.jsonl", [
        {"type": "user", "timestamp": "2026-11-05T08:00:00Z", "message": {"content": "say tok-value-999"}}])
    activity.finish(collector, 3)
    records = activity.read_records(logs / "activity.jsonl")
    assert [r["kind"] for r in records] == ["session", "prompt", "session"]
    assert records[0]["details"]["workspaces"] == [{"host": "/p", "sandbox": "/work"}]
    assert records[1]["summary"] == "say [redacted]"
    assert records[2]["details"]["status"] == 3
    assert (logs / "activity.jsonl").stat().st_mode & 0o777 == 0o600
    # A session's pause, resume, attach: through the run registry's log_dir.
    runs = tmp_path / "runs"
    (runs / "session-dev").mkdir(parents=True)
    (runs / "session-dev" / "run.json").write_text(json.dumps({"run": "session-dev", "agent": "codex",
                                                               "log_dir": str(logs)}))
    assert activity.session_event("dev", "pause", runs) is True
    assert activity.session_event("missing", "pause", runs) is False
    last = activity.read_records(logs / "activity.jsonl")[-1]
    assert last["summary"] == "session dev paused" and last["session"] == "dev" and last["agent"] == "codex"
    assert activity.start(SimpleNamespace(log_dir=None), home) is None
    session_a = SimpleNamespace(pod_name="agent-session-dev", tmux_socket_dir="/x", session=None)
    assert activity.session_name(session_a) == "dev"
    # Since 0.1.30 the supervisor names its session by --start-id (its id stays when it is renamed).
    session_b = SimpleNamespace(pod_name="agent-session-dev", tmux_socket_dir="/x", session=None, start_id="web")
    assert activity.session_name(session_b) == "web"
    task = SimpleNamespace(pod_name=None, tmux_socket_dir=None, session=None, start_id=None, start_key="fix")
    assert activity.session_name(task) == "fix"  # a kept task: its start


def test_renamed_session_is_found_by_its_name(tmp_path, monkeypatch):
    """--session NAME and the session events reach a session after a rename (its id and run stay)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sessions = activity.module("sessions")
    directory = sessions.root() / "web"
    directory.mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    sessions.save("web", {"agent": "codex", "options": ["--log-dir", str(logs)], "cwd": str(tmp_path),
                          "name": "frontend", "run": "codex-1a2b3c4d"})
    assert activity.session_key("frontend") == "web" and activity.session_key("web") == "web"
    assert activity.session_key("nothing") == "nothing"
    runs = tmp_path / "runs"
    (runs / "codex-1a2b3c4d").mkdir(parents=True)
    (runs / "codex-1a2b3c4d" / "run.json").write_text(json.dumps({"run": "codex-1a2b3c4d", "agent": "codex",
                                                                  "log_dir": str(logs)}))
    assert activity.session_event("web", "pause", runs) is True
    assert activity.read_records(logs / "activity.jsonl")[-1]["run"] == "codex-1a2b3c4d"
    assert activity.session_log_dir("frontend") == logs.resolve()
    # A run is named by its id, the name of its start, or a kept start's last run.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    assert activity.run_id("codex-1a2b3c4d") == "codex-1a2b3c4d"
    assert activity.run_id("frontend") == "codex-1a2b3c4d" and activity.run_id("web") == "codex-1a2b3c4d"
    assert activity.run_id("unknown") == "unknown"
    running = activity.module("agent_sandbox").runs_dir() / "claude-77"
    running.mkdir(parents=True)
    (running / "run.json").write_text(json.dumps({"run": "claude-77", "name": "review", "pid": os.getpid()}))
    assert activity.run_id("review") == "claude-77"


def test_the_launcher_starts_and_finishes_the_collector(tmp_path, monkeypatch):
    s_spec = importlib.util.spec_from_file_location("agent_sandbox_activity_test", ROOT / "sandbox" / "agent_sandbox.py")
    s = importlib.util.module_from_spec(s_spec)
    s_spec.loader.exec_module(s)
    source = (ROOT / "sandbox" / "agent_sandbox.py").read_text()
    assert "activity.finish(status)" in source and '"log_dir": str(Path(a.log_dir)' in source
    a = SimpleNamespace(log_dir=tmp_path / "logs", probe=True, mcp_socket_dir=None)
    assert s.start_activity(a, tmp_path, None, {"env": {}, "secret_names": []}) is None  # probes keep no timeline
    sessions = (ROOT / "sandbox" / "sessions.py").read_text()
    for event in ("pause", "resume", "attach", "detach", "stop"):
        assert f'activity_event(name, "{event}")' in sessions


# -- the command --

def write_timeline(path):
    records = []
    for run, session, agent in (("claude-1", None, "claude"), ("session-dev", "dev", "codex")):
        redact = activity.Redactor()
        records += [activity.finalize(event, run, session, agent, redact) for event in [
            activity.raw("session", f"{agent} started", "2026-11-04T10:00:00.000Z", {"event": "start"}, key="start"),
            activity.raw("prompt", "fix it\nplease", "2026-11-04T10:00:01.000Z", {"text": "fix it\nplease"}, key="p",
                         source=agent),
            activity.raw("command", "pytest · exit 1", "2026-11-04T10:00:02.000Z",
                         {"command": "pytest", "status": "error", "exit_code": 1}, key="c", source=agent),
            activity.raw("network", "denied: lookup of evil.example", "2026-11-04T10:00:03.000Z",
                         {"verdict": "denied", "host": "evil.example"}, key="n", source="egress"),
            activity.raw("session", f"{agent} ended (exit status 0)", "2026-11-04T10:00:04.000Z",
                         {"event": "stop", "status": 0}, key="stop")]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "garbage\n")


def run_cli(args):
    out = []
    status = activity.cli(args, out=out.append)
    return status, out


def test_cli_text_and_json(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setenv("NO_COLOR", "1")
    logs = tmp_path / "logs"
    write_timeline(logs / "activity.jsonl")
    status, out = run_cli(["--log-dir", str(logs)])
    assert status == 0 and out[0] == "2026-11-04"
    assert out[1].split() == ["10:00:00", "session", "sandbox", "claude", "started"]
    assert any(line.split()[:3] == ["10:00:01", "prompt", "claude"] and line.endswith("fix it please") for line in out)
    status, out = run_cli(["claude-1", "--log-dir", str(logs), "--kind", "command,network", "--json"])
    records = [json.loads(line) for line in out]
    assert [r["kind"] for r in records] == ["command", "network"] and {r["run"] for r in records} == {"claude-1"}
    status, out = run_cli(["--log-dir", str(logs), "-k", "command", "-v"])
    assert any("exit_code" in line and "1" in line for line in out)
    status, out = run_cli(["--log-dir", str(logs), "--since", "2026-11-04T10:00:03Z", "--json"])
    assert {json.loads(line)["kind"] for line in out} == {"network", "session"}
    status, out = run_cli(["--log-dir", str(logs), "--grep", "evil", "--json", "--tail", "1"])
    assert len(out) == 1 and json.loads(out[0])["run"] == "session-dev"
    status, out = run_cli(["--log-dir", str(logs), "--runs", "--json"])
    runs = json.loads("\n".join(out))
    assert [(r["run"], r["running"], r["status"], r["events"]) for r in runs] == [("claude-1", False, 0, 5),
                                                                                    ("session-dev", False, 0, 5)]
    status, out = run_cli(["--log-dir", str(logs), "--runs"])
    assert out[0].split()[:3] == ["RUN", "AGENT", "SESSION"] and "claude-1" in out[1]
    with pytest.raises(ValueError):
        run_cli(["--log-dir", str(logs), "--kind", "bogus"])
    with pytest.raises(ValueError):
        run_cli(["--log-dir", str(logs), "--since", "yesterday-ish"])
    assert run_cli(["--log-dir", str(tmp_path / "none")])[0] == 1


def test_cli_finds_runs_and_sessions(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    project = tmp_path / "project"
    project.mkdir()
    s = activity.module("agent_sandbox")
    logs = s.default_log_dir(project.resolve())
    write_timeline(logs / "activity.jsonl")
    status, out = run_cli(["claude-1", "--json"])  # an ended run: found in the default log directories
    assert status == 0 and {json.loads(line)["run"] for line in out} == {"claude-1"}
    status, out = run_cli(["--project", str(project), "--json"])
    assert len(out) == 10
    monkeypatch.chdir(project)
    assert len(run_cli(["--json"])[1]) == 10  # the current project
    with pytest.raises(ValueError, match="no activity of a run"):
        run_cli(["claude-zzz"])
    monkeypatch.setattr(activity, "running_record", lambda run: {"log_dir": str(logs)} if run == "session-dev" else None)
    status, out = run_cli(["--session", "dev", "--json"])
    assert {json.loads(line)["session"] for line in out} == {"dev"}


def test_cli_reads_a_home_directly(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    home = claude_home(tmp_path)
    status, out = run_cli(["--home", str(home), "--kind", "prompt", "--json"])
    assert [json.loads(line)["summary"] for line in out][:2] == ["fix the tests", "Find the parser"]


def test_follow_shows_what_is_appended(tmp_path):
    path = tmp_path / "activity.jsonl"
    write_timeline(path)
    shown, stop = [], threading.Event()
    keep = activity.matcher(kinds=["prompt"])
    thread = threading.Thread(target=activity.follow_file, args=(path, keep, shown.append, 0.05, stop), daemon=True)
    thread.start()
    time.sleep(0.2)
    assert shown == []  # what was there is shown before following, not again
    redact = activity.Redactor()
    with open(path, "a") as stream:
        stream.write(json.dumps(activity.finalize(activity.raw("prompt", "new one"), "r", None, "claude", redact)) + "\n")
        stream.write(json.dumps(activity.finalize(activity.raw("reply", "skip"), "r", None, "claude", redact)) + "\n")
        stream.write('{"kind": "prompt", "summary": "half')
    deadline = time.monotonic() + 5
    while not shown and time.monotonic() < deadline:
        time.sleep(0.05)
    with open(path, "a") as stream:
        stream.write(' done"}\n')
    deadline = time.monotonic() + 5
    while len(shown) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert [r["summary"] for r in shown] == ["new one", "half done"]


def test_follow_through_the_command(tmp_path):
    """sandbox-ai activity --follow --json as the app runs it: the existing events, then new ones as they come."""
    import subprocess
    path = tmp_path / "logs" / "activity.jsonl"
    write_timeline(path)
    process = subprocess.Popen([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), "activity", "--log-dir",
                                str(path.parent), "--json", "--follow", "--kind", "prompt"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    import queue
    lines_read = queue.Queue()
    threading.Thread(target=lambda: [lines_read.put(text) for text in process.stdout], daemon=True).start()

    def line():  # never waits forever: a missing line fails the test instead of hanging the suite
        try:
            return lines_read.get(timeout=20)
        except queue.Empty:
            raise AssertionError("no line from sandbox-ai activity --follow within 20 s") from None
    try:
        lines = [line() for _ in range(2)]
        assert [json.loads(text)["summary"] for text in lines] == ["fix it please"] * 2
        record = activity.finalize(activity.raw("prompt", "live"), "r", None, "claude", activity.Redactor())
        with open(path, "a") as stream:
            stream.write(json.dumps(record) + "\n")
        assert json.loads(line())["summary"] == "live"
    finally:
        process.terminate()
        process.wait(10)


def test_a_record_appended_between_the_listing_and_follow_is_shown_once(tmp_path, monkeypatch):
    """Regression: --follow took the file's end anew after the listing, so a record appended in between was lost
    (the app's live view missed it; the test of the command hung on it). It goes on where the listing stopped; a
    line still being written is shown once it is complete."""
    import threading
    path = tmp_path / "logs" / "activity.jsonl"
    write_timeline(path)
    live = activity.finalize(activity.raw("prompt", "live"), "r", None, "claude", activity.Redactor())
    late = activity.finalize(activity.raw("prompt", "late"), "r", None, "claude", activity.Redactor())
    real_read, real_follow = activity.read_records, activity.follow_file

    def read_then_append(*args, **kwargs):
        found = real_read(*args, **kwargs)
        with open(path, "a") as stream:  # between the listing and --follow: one whole record, one half written
            stream.write(json.dumps(live) + "\n" + json.dumps(late)[:10])
        return found

    def follow_briefly(*args, **kwargs):
        stop = threading.Event()

        def finish_the_line():
            with open(path, "a") as stream:
                stream.write(json.dumps(late)[10:] + "\n")
            threading.Timer(0.5, stop.set).start()
        threading.Timer(0.3, finish_the_line).start()
        real_follow(*args, interval=0.05, stop=stop, **kwargs)
    monkeypatch.setattr(activity, "read_records", read_then_append)
    monkeypatch.setattr(activity, "follow_file", follow_briefly)
    out = []
    assert activity.cli(["--log-dir", str(path.parent), "--json", "--follow", "--kind", "prompt"], out=out.append) == 0
    assert [json.loads(line)["summary"] for line in out] == ["fix it please", "fix it please", "live", "late"]


def test_times():
    assert activity.epoch("2026-11-04T10:00:00Z") == activity.epoch("2026-11-04T11:00:00+01:00") == 1793786400
    assert activity.epoch(1793786400000) == activity.epoch(1793786400) == 1793786400
    assert activity.epoch("2026-11-04T10:00:00.5Z") == 1793786400.5
    for bad in ("", "x", None, True, float("inf"), -1, [], "2026-13-99"):
        assert activity.epoch(bad) is None or bad == "2026-13-99"
    assert activity.stamp(1793786400.25) == "2026-11-04T10:00:00.250Z"
    now = 1793786400
    assert activity.since_value("10m", now) == now - 600 and activity.since_value("2d", now) == now - 172800
    assert activity.since_value("2026-11-04T10:00:00Z") == now


def test_main_lists_the_command():
    main = (ROOT / "sandbox" / "main.py").read_text()
    assert '"activity": ("activity.py",)' in main and "sandbox-ai activity" in main


def test_tags_are_found_in_linear_time_whatever_the_agent_writes():
    """The agent writes the transcripts: a line of a million "<command-name>" without its end took the regex hours,
    holding the launcher's other threads (canaries, memory watch). tagged() finds what the regex found, at once."""
    import re
    import time
    for text, name in (("x<command-name> /review </command-name><command-args>a b</command-args>", "command-name"),
                       ("<a>1</a><a>2</a>", "a"), ("<summary>no end", "summary"), ("", "status"),
                       ("<status>done</status>", "status"), ("<a></a>", "a"), ("a <b>x\ny</b>", "b")):
        regex = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
        assert activity.tagged(text, name) == (regex.group(1) if regex else None), text
    started = time.monotonic()
    assert activity.tagged("<command-name>" * 1_000_000, "command-name") is None
    assert time.monotonic() - started < 2
