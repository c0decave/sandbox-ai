"""The parts of the agents' live checks that run without Podman: the scripted model server (tests/devtools/
agent_model.py) - it drives exactly one tool call over the OpenAI Chat Completions API and Anthropic's Messages API,
streamed or not, and tells a script what it saw -, the reading of a run's logs (tests/live_agents.py), the skip reasons,
scripts/live_check.py --agents and scripts/live_check_agents.sh, and CI's job agents-live."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import urllib.request

import pytest

import live_agents as L

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_check_agents_test", ROOT / "scripts/live_check.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
TOOLS = [{"type": "function", "function": {"name": "list_directory"}},
         {"type": "function", "function": {"name": "mcp__demo__version"}}]


@pytest.fixture
def model(tmp_path):
    module = L.load_model_server()
    server, port = module.start(token="TOKEN42", log=str(tmp_path / "model.jsonl"))
    yield f"http://127.0.0.1:{port}", tmp_path / "model.jsonl"
    server.shutdown()


def post(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        return answer.read().decode()


def events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]


def test_one_tool_call_then_the_answer(model):
    url, log = model
    first = json.loads(post(url + "/v1/chat/completions", {"model": "m", "tools": TOOLS,
                                                          "messages": [{"role": "user", "content": "go"}]}))
    call = first["choices"][0]["message"]["tool_calls"][0]
    assert call["function"] == {"name": "mcp__demo__version", "arguments": "{}"}  # the demo server's, preferred
    assert first["choices"][0]["finish_reason"] == "tool_calls"
    second = json.loads(post(url + "/v1/chat/completions", {"model": "m", "tools": TOOLS, "messages": [
        {"role": "user", "content": "go TOKEN42"}, {"role": "assistant", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": '{"server": "sandbox-ai-demo"}'}]}))
    text = second["choices"][0]["message"]["content"]
    assert text.startswith("LIVE-CHECK-ANSWER") and "sandbox-ai-demo" in text
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["called"] for r in records] == ["mcp__demo__version", None]
    assert records[1]["tool_result"] == '{"server": "sandbox-ai-demo"}' and records[1]["token"] is True
    with urllib.request.urlopen(url + "/live/seen", timeout=10) as answer:
        assert json.loads(answer.read()) == {"requests": 2, "messages": 0, "token": True}


def test_streamed_chat_carries_the_call_and_the_text(model):
    url, _ = model
    chunks = events(post(url + "/v1/chat/completions", {"stream": True, "tools": [
        {"type": "function", "function": {"name": "demo__version"}}], "messages": [{"role": "user", "content": "x"}],
        "stream_options": {"include_usage": True}}))
    assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "demo__version"
    assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert chunks[2]["choices"][0]["finish_reason"] == "tool_calls" and chunks[-1]["usage"]["total_tokens"] == 15
    chunks = events(post(url + "/v1/chat/completions", {"stream": True, "messages": [{"role": "user", "content": "x"}]}))
    assert "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks) == "LIVE-CHECK-ANSWER no tool"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_anthropics_messages_streamed_and_not(model):
    url, _ = model
    reply = json.loads(post(url + "/v1/messages", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}))
    assert reply["content"] == [{"type": "text", "text": "LIVE-CHECK-ANSWER no tool"}] and reply["stop_reason"] == \
        "end_turn"
    streamed = events(post(url + "/v1/messages", {"stream": True, "tools": [{"name": "mcp__demo__version"}],
                                                  "messages": [{"role": "user", "content": "hi"}]}))
    assert [e["type"] for e in streamed] == ["message_start", "content_block_start", "content_block_delta",
                                            "content_block_stop", "message_delta", "message_stop"]
    assert streamed[1]["content_block"]["name"] == "mcp__demo__version"
    result = json.loads(post(url + "/v1/messages", {"tools": [{"name": "mcp__demo__version"}], "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "text", "text": "sandbox-ai-demo 1.0.0"}]}]}]}))
    assert "sandbox-ai-demo 1.0.0" in result["content"][0]["text"]
    assert json.loads(post(url + "/v1/messages/count_tokens", {})) == {"input_tokens": 10}
    with urllib.request.urlopen(url + "/v1/models", timeout=10) as answer:
        assert json.loads(answer.read())["data"][0]["id"] == "live-model"


def test_no_tool_to_call_and_unknown_paths(model):
    url, _ = model
    reply = json.loads(post(url + "/v1/chat/completions", {"tools": TOOLS[:1], "messages": []}))
    assert reply["choices"][0]["message"]["content"] == "LIVE-CHECK-ANSWER no tool"
    with pytest.raises(urllib.error.HTTPError):
        post(url + "/v1/responses", {})


# Letta Code 0.34.36's reminder, as buildMcpServersReminderText writes it (src/reminders), and some of its own tools.
LETTA_REMINDER = ("<system-reminder>\nMCP servers with available tools: other-server, demo\nDo not automatically store "
                  "results of principal-restricted operations in shared memory.\nFind tools (with schemas) with "
                  "`letta mcp search \"<what you want to do>\"`, ...\n</system-reminder>")
LETTA_TOOLS = [{"type": "function", "function": {"name": name}} for name in
               ("EnterWorktree", "Agent", "Skill", "Memory", "CodeMode", "Workflow", "exec_command", "write_stdin")]


def test_letta_codes_mcp_tools_are_called_through_its_shell(model):
    """CI 0.1.187 (agents-live letta: "the model was never offered the demo server's tool version"): Letta Code
    0.34.36 offers MCP tools not as the model's tools but through `letta mcp call`, named in a system reminder. The
    scripted model calls the demo server's version that way (Letta's exec_command), then answers with its output; no
    reminder (or "None"), no shell: no call."""
    url, log = model
    messages = [{"role": "system", "content": "You are Letta Code."},
                {"role": "user", "content": [{"type": "text", "text": LETTA_REMINDER}, {"type": "text", "text": "go"}]}]
    first = json.loads(post(url + "/v1/chat/completions", {"model": "m", "tools": LETTA_TOOLS, "messages": messages}))
    call = first["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "exec_command"
    arguments = json.loads(call["function"]["arguments"])
    assert arguments["cmd"] == "letta mcp call mcp__demo__version --args '{}'" and arguments["description"]
    streamed = events(post(url + "/v1/chat/completions", {"stream": True, "messages": messages, "tools": [
        {"type": "function", "function": {"name": "Bash"}}]}))
    assert json.loads(streamed[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])["command"] == \
        "letta mcp call mcp__demo__version --args '{}'"
    output = '{"content": [{"type": "text", "text": "{\\"server\\": \\"sandbox-ai-demo\\"}"}]}'
    second = json.loads(post(url + "/v1/chat/completions", {"model": "m", "tools": LETTA_TOOLS, "messages": [
        *messages, {"role": "assistant", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": output}]}))
    assert "sandbox-ai-demo" in second["choices"][0]["message"]["content"]
    for other in ([{"role": "user", "content": "go"}],
                  [{"role": "user", "content": "<system-reminder>\nMCP servers with available tools: None\n"}]):
        reply = json.loads(post(url + "/v1/chat/completions", {"tools": LETTA_TOOLS, "messages": other}))
        assert reply["choices"][0]["message"]["content"] == "LIVE-CHECK-ANSWER no tool"
    no_shell = json.loads(post(url + "/v1/chat/completions", {"tools": LETTA_TOOLS[:2], "messages": messages}))
    assert no_shell["choices"][0]["message"]["content"] == "LIVE-CHECK-ANSWER no tool"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["called"] for r in records] == ["exec_command", "Bash", None, None, None, None]
    assert records[0]["mcp_listed"] == ["other-server", "demo"] and records[3]["mcp_listed"] == []
    # A tool of the MCP server's own among the model's tools is still the one called, without arguments.
    direct = json.loads(post(url + "/v1/chat/completions", {"tools": [*LETTA_TOOLS, *TOOLS], "messages": messages}))
    assert direct["choices"][0]["message"]["tool_calls"][0]["function"] == {"name": "mcp__demo__version",
                                                                            "arguments": "{}"}


def test_a_runs_logs_say_what_it_reached_and_what_was_refused(tmp_path):
    (tmp_path / "connections-20261113-run.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"event": "tls", "host": "127.0.0.1:40000"}, {"event": "denied", "sni": "us.i.posthog.com"},
        {"event": "http", "url": "http://models.dev/api.json"}, {"event": "error", "host": "error.example"},
        {"event": "tcp", "destination": "10.0.2.2:40000"}]) + "\nnot json\n")
    (tmp_path / "egress-denied.jsonl").write_text(json.dumps({"host": "telemetry.nousresearch.com"}) + "\n")
    (tmp_path / "dns.jsonl").write_text(json.dumps({"host": "pypi.org", "forwarded": "1.1.1.1"}) + "\n")
    reached, refused = L.reached_and_refused(tmp_path)
    assert reached == {"127.0.0.1", "models.dev", "10.0.2.2", "pypi.org"}
    assert refused == {"us.i.posthog.com", "telemetry.nousresearch.com"}
    hermes = L.load_module("hermes")
    assert L.telemetry_in(hermes, refused) == ["telemetry.nousresearch.com", "us.i.posthog.com"]
    assert L.telemetry_in(hermes, reached) == []
    assert "live-check.posthog.com" in L.telemetry_samples(hermes)


def test_the_switches_checked_and_the_lookup_script():
    goose = L.load_module("goose")
    checked = dict(L.features_to_check(goose))
    assert "gateway:telegram" not in checked and "scheduler" not in checked and checked["model-catalog"] == ["models.dev"]
    assert L.features_to_check(L.load_module("dsh")) == []
    script = L.lookups_script(["a.example", "b.example"])
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    found = L.lookups(done.stdout)
    assert set(found) == {"a.example", "b.example"} and set(found.values()) <= {"ok", "refused"}


@pytest.mark.parametrize("agent", sorted(L.AGENTS))
def test_the_lookup_probe_passes_the_launchers_own_check_of_the_agents_arguments(agent, tmp_path):
    """CI 0.1.186 (agents-live nanobot, "the probe did not run"): the switch checks ran --binary /bin/sh -c SCRIPT, and
    nanobot's check of its arguments - which holds under --binary too - refused the shell's -c as its config file.
    The probe's arguments (probe_args) pass each agent's check, here through the launcher's own dry run."""
    args, content = L.probe_args(agent, L.lookups_script(["a.example"]))
    assert (content is None) == (args[0] == "-c") and (content is None or args == [L.PROBE])
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "XDG_STATE_HOME": str(tmp_path / "state")}
    done = subprocess.run([str(L.SAI), agent, "--dry-run", "--workspace", str(work), "--no-config", "--no-auth",
                           "--no-tmux", "--binary", "/bin/sh", "--", *args], capture_output=True, text=True,
                          env=env, timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 0 and "arguments:" not in done.stderr, done.stderr
    if agent == "nanobot":  # (its -c is its config file: still refused, as an argument of nanobot's)
        assert args == [L.PROBE] and L.load_module("nanobot").refused_arg(["-c", "x"]) is not None


def test_skip_reasons_name_what_to_build(monkeypatch):
    monkeypatch.setattr(L, "PODMAN", None)
    assert "needs Podman" in L.skip_reason(["localhost/qwen-sandbox:latest"])
    monkeypatch.setattr(L, "PODMAN", "/usr/bin/podman")
    monkeypatch.setattr(L, "image_exists", lambda image: image == L.MCP_IMAGE)
    reason = L.skip_reason(["localhost/qwen-sandbox:latest"])
    assert "localhost/qwen-sandbox:latest" in reason and "sandbox-ai build qwen" in reason
    assert "codex" not in reason
    monkeypatch.setattr(L, "image_exists", lambda image: False)
    reason = L.skip_reason(["localhost/sandbox-ai-cairn:latest", "localhost/sandbox-ai-browser:latest"], mcp=False)
    assert "sandbox-ai build cairn && sandbox-ai build browser" in reason and "codex" not in reason


def test_live_check_agents_builds_and_names(tmp_path, monkeypatch):
    assert lc.agent_names(None) == list(lc.AGENT_SUITES) and lc.agent_names("goose,hermes") == ["hermes", "goose"]
    with pytest.raises(SystemExit, match="not one of"):
        lc.agent_names("hermes,gpt")
    assert lc.agent_builds(["hermes", "qwen", "cairn", "browser"]) == ["codex", "hermes", "qwen", "claude", "cairn",
                                                                        "browser"]
    for suite in lc.AGENT_SUITES.values():
        assert (ROOT / suite).is_file()


def test_live_check_agents_without_podman(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "inside_a_container", lambda: False)
    monkeypatch.setattr(lc.shutil, "which", lambda name: None)
    ran = []

    def fake_run(command, timeout=600, **kwargs):
        ran.append(command)
        return (0, "sandbox-ai 0.1.95\n", "") if command[0].endswith("sandbox-ai") else (127, "", "not found")
    monkeypatch.setattr(lc, "run", fake_run)
    monkeypatch.setattr(lc, "pytest_python", lambda directory: pytest.fail("no Podman: no pytest run"))
    assert lc.main(["--out", str(tmp_path), "--agents", "qwen", "--build"]) == 1
    data = json.loads((tmp_path / "report.json").read_text())
    assert {c["check"]: c["outcome"] for c in data["checks"]} == {"podman installed": "failed"}
    assert data["facts"]["agents"] == "qwen" and not any("build" in command for command in ran)


def test_live_check_agents_reports_what_the_agents_saw(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lc, "inside_a_container", lambda: False)
    monkeypatch.setattr(lc.shutil, "which", lambda name: "/usr/bin/" + name)
    ran = []

    def fake_run(command, timeout=600, **kwargs):
        ran.append(command)
        if "pytest" in command:
            seen = Path(kwargs["env"]["SANDBOX_AI_LIVE_REPORT_DIR"])
            (seen / "qwen-answer.json").write_text(json.dumps({"agent": "qwen", "answered": True, "refused": [
                "play.googleapis.com"], "telemetry_reached": []}))
            (tmp_path / "live-agents.xml").write_text(
                '<testsuites><testsuite><testcase classname="tests.test_qwen_live" '
                'name="test_qwen_answers_with_one_mcp_tool_and_no_telemetry" time="80"/></testsuite></testsuites>')
            return 0, "1 passed", ""
        return 0, "ok\n", ""
    monkeypatch.setattr(lc, "run", fake_run)
    monkeypatch.setattr(lc, "pytest_python", lambda directory: "python3")
    assert lc.main(["--out", str(tmp_path), "--agents", "qwen", "--build"]) == 0
    assert [c[1:3] for c in ran if "build" in c] == [["build", "codex"], ["build", "qwen"]]
    pytest_command = next(c for c in ran if "pytest" in c)
    assert "tests/test_qwen_live.py" in pytest_command and "--agent-live" in pytest_command
    text = (tmp_path / "report.md").read_text()
    assert "## What the agents reached" in text and '"play.googleapis.com"' in text
    assert "qwen-answer" in capsys.readouterr().out


def test_the_wrapper_passes_the_agents_on(tmp_path):
    script = (ROOT / "scripts/live_check_agents.sh").read_text()
    assert 'live_check.py" --agents "${agents:-all}" $build' in script and "SANDBOX_AI_EXPERIMENTAL" not in script
    assert subprocess.run(["sh", "-n", str(ROOT / "scripts/live_check_agents.sh")]).returncode == 0


def test_ci_runs_one_job_per_agent():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    job = ci[ci.index("\n  agents-live:"):]
    for name in ("hermes", "qwen", "dsh", "goose", "openhands", "nanobot", "cairn"):
        assert f"suite: tests/test_{name}_live.py" in job
    assert "fail-fast: false" in job and "--agent-live ${{ matrix.suite }}" in job and "grep -q SKIPPED" in job
    assert "SANDBOX_AI_EXPERIMENTAL" not in job  # nothing to enable: the agents start when chosen
