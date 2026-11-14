"""The parts of the agents' live checks that run without Podman: the scripted model server (tests/devtools/
agent_model.py) - it drives exactly one tool call over the OpenAI Chat Completions API and Anthropic's Messages API,
streamed or not, and tells a script what it saw -, the reading of a run's logs (tests/live_agents.py), the skip reasons,
scripts/live_check.py --agents and scripts/live_check_agents.sh, and CI's job agents-live."""
import importlib.util
import json
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


def test_skip_reasons_name_what_to_build(monkeypatch):
    monkeypatch.setattr(L, "PODMAN", None)
    assert "needs Podman" in L.skip_reason(["localhost/qwen-sandbox:latest"])
    monkeypatch.setattr(L, "PODMAN", "/usr/bin/podman")
    monkeypatch.setattr(L, "image_exists", lambda image: image == L.MCP_IMAGE)
    reason = L.skip_reason(["localhost/qwen-sandbox:latest"])
    assert "localhost/qwen-sandbox:latest" in reason and "sandbox-ai build qwen" in reason
    assert "codex" not in reason
    monkeypatch.setattr(L, "image_exists", lambda image: False)
    assert "sandbox-ai build cairn" in L.skip_reason(["localhost/cairn-sandbox:latest"], mcp=False)
    assert "codex" not in L.skip_reason(["localhost/cairn-sandbox:latest"], mcp=False)


def test_live_check_agents_builds_and_names(tmp_path, monkeypatch):
    assert lc.agent_names(None) == list(lc.AGENT_SUITES) and lc.agent_names("goose,hermes") == ["hermes", "goose"]
    with pytest.raises(SystemExit, match="not one of"):
        lc.agent_names("hermes,gpt")
    assert lc.agent_builds(["hermes", "qwen", "cairn", "browser"]) == ["codex", "hermes", "qwen", "team", "cairn",
                                                                        "claude", "browser"]
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
    for name in ("hermes", "qwen", "dsh", "goose", "cairn"):
        assert f"suite: tests/test_{name}_live.py" in job
    assert "fail-fast: false" in job and "--agent-live ${{ matrix.suite }}" in job and "grep -q SKIPPED" in job
    assert "SANDBOX_AI_EXPERIMENTAL" not in job  # nothing to enable: the agents start when chosen
