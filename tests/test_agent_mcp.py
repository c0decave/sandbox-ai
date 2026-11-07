"""An agent as an MCP server (sandbox/agent_mcp.py), the helper of another agent: its one tool `ask` runs the
agent's non-interactive mode in /work and answers with its final message. Against fake agents on $PATH."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_mcp_for_tests", ROOT / "sandbox" / "agent_mcp.py")
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

FAKE = {
    # Records its arguments, then answers the way the agent does.
    "claude": "import json, sys\nprint(json.dumps({'type': 'result', 'result': 'claude did: ' + sys.argv[-1], "
              "'session_id': 's1'}))\n",
    "codex": "import sys\nargs = sys.argv[1:]\nout = args[args.index('--output-last-message') + 1]\n"
             "open(out, 'w').write('codex did: ' + args[-1])\nprint('[progress lines]')\n",
    "opencode": "import sys\nprint('opencode did: ' + sys.argv[-1])\n",
}


@pytest.fixture
def agents(tmp_path, monkeypatch):
    """Fake claude, codex and opencode on $PATH; each writes its argv to ARGV.json."""
    bin_dir, work = tmp_path / "bin", tmp_path / "work"
    bin_dir.mkdir()
    work.mkdir()
    for name, body in FAKE.items():
        path = bin_dir / name
        path.write_text(f"#!/usr/bin/python3\nimport json, os, sys\n"
                        f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps([os.getcwd(), *sys.argv[1:]]))\n"
                        + body)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return tmp_path


def called(tmp_path):
    return json.loads((tmp_path / "argv.json").read_text())


@pytest.mark.parametrize("agent", am.AGENTS)
def test_ask_runs_the_agents_non_interactive_mode_in_work(agents, agent):
    helper = am.Helper(agent, cwd=str(agents / "work"), lock=agents / "lock")
    assert helper.ask("-rf the tests") == f"{agent} did: -rf the tests"
    cwd, *argv = called(agents)
    assert cwd == str(agents / "work") and argv[-2:] == ["--", "-rf the tests"], "the prompt is never an option"
    expected = {"claude": ["-p", "--output-format", "json"], "codex": ["exec", "--skip-git-repo-check"],
                "opencode": ["run"]}[agent]
    assert argv[:len(expected)] == expected and not set(argv) & {*am.SKIP["claude"], *am.SKIP["codex"]}
    if agent == "codex":
        assert argv[2:4] == ["--sandbox", "danger-full-access"], "its own sandbox cannot work inside sandbox-ai's"


def test_continue_and_skip_permissions_reach_the_agent(agents):
    am.Helper("claude", skip=True, cwd=str(agents / "work"), lock=agents / "lock").ask("more", resume=True)
    assert called(agents)[1:] == ["-p", "--output-format", "json", "--setting-sources", "user", "--strict-mcp-config",
                                  "--continue", "--dangerously-skip-permissions", "--", "more"]
    am.Helper("codex", skip=True, cwd=str(agents / "work"), lock=agents / "lock").ask("more", resume=True)
    argv = called(agents)[1:]
    assert argv[:3] == ["exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    assert argv[-4:] == ["resume", "--last", "--", "more"], "exec's options come before its subcommand"
    am.Helper("opencode", cwd=str(agents / "work"), lock=agents / "lock").ask("more", resume=True)
    assert called(agents)[1:] == ["run", "--continue", "--", "more"]


def test_a_failed_or_endless_task_is_an_error(agents, tmp_path):
    (agents / "bin" / "claude").write_text("#!/bin/sh\necho 'not logged in' >&2\nexit 3\n")
    with pytest.raises(ValueError, match="exit status 3: not logged in"):
        am.Helper("claude", cwd=str(agents / "work"), lock=agents / "lock").ask("x")
    (agents / "bin" / "opencode").write_text("#!/bin/sh\nsleep 30\n")
    with pytest.raises(ValueError, match="did not finish within 1 s"):
        am.Helper("opencode", limit=1, cwd=str(agents / "work"), lock=agents / "lock").ask("x")


def test_one_task_at_a_time_and_bad_arguments(agents):
    helper = am.Helper("claude", cwd=str(agents / "work"), lock=agents / "lock")
    helper.lock.acquire()
    with pytest.raises(ValueError, match="busy"):
        helper.ask("x")
    helper.lock.release()
    for prompt in ("", "   ", None, 3, "x" * (am.PROMPT_LIMIT + 1)):
        with pytest.raises(ValueError):
            helper.ask(prompt)
    with pytest.raises(ValueError, match="continue"):
        helper.ask("x", resume="yes")
    with pytest.raises(ValueError):
        am.Helper("gemini")


def test_a_long_answer_is_cut_and_says_so(agents):
    (agents / "bin" / "opencode").write_text(f"#!/usr/bin/python3\nprint('a' * {am.ANSWER_LIMIT + 10})\n")
    text = am.Helper("opencode", cwd=str(agents / "work"), lock=agents / "lock").ask("x")
    assert len(text) < am.ANSWER_LIMIT + 100 and text.endswith(f"[answer cut at {am.ANSWER_LIMIT} characters]")


def test_the_mcp_conversation(agents):
    helper = am.Helper("codex", cwd=str(agents / "work"), lock=agents / "lock")
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ask",
                                                                            "arguments": {"prompt": "fix it"}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "shell", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}]
    out = io.StringIO()
    helper.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\nnot json\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, None]  # the notification gets no reply
    assert replies[0]["result"]["serverInfo"]["name"] == "sandbox-ai-helper-codex"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["ask"]
    assert replies[2]["result"] == {"content": [{"type": "text", "text": "codex did: fix it"}], "isError": False}
    assert replies[3]["result"]["isError"] and "Unknown tool" in replies[3]["result"]["content"][0]["text"]
    assert replies[4]["error"]["code"] == -32601 and replies[5]["error"]["code"] == -32700


def test_main_takes_only_its_own_arguments():
    assert am.main([]) == 2 and am.main(["gemini"]) == 2
    assert am.main(["claude", "--limit", "0"]) == 2 and am.main(["claude", "--rm"]) == 2


def test_one_task_at_a_time_across_connections(agents):
    """Each connection gets a server of its own: the lock file in HOME holds across them."""
    import fcntl
    import os
    first = am.Helper("claude", cwd=str(agents / "work"), lock=agents / "lock")
    second = am.Helper("claude", cwd=str(agents / "work"), lock=agents / "lock")
    fd = os.open(agents / "lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)  # the first connection's task runs
    try:
        with pytest.raises(ValueError, match="busy"):
            second.ask("x")
    finally:
        os.close(fd)
    assert first.ask("x") == "claude did: x", "free again once it is done"
