"""Claude Code with --llm and a model it does not know (live report, hotfix 0.1.96). The user ran
`sandbox-ai claude --llm macbook-free-models:qwen/qwen3-coder-next` against LM Studio on a MacBook, and Claude Code
2.1.325 said:

    ⚠ Model "qwen/qwen3-coder-next" isn't recognized · context assumed to be 1m tokens · update Claude Code, or see
    https://code.claude.com/docs/en/model-config

- "bin nicht sicher ob es sauber funktioniert." Claude Code compacts the conversation at the window it assumes for an
ID it does not know (1m tokens in 2.1.325, 200k in 2.1.322): a local model's 32k-256k overflow long before, and the
server refuses or cuts the conversation. Claude Code's documented override (model-config, "Correct the window for a
gateway or custom model ID"): CLAUDE_CODE_MAX_CONTEXT_TOKENS, for an ID that does not start with claude-; and
CLAUDE_CODE_MAX_OUTPUT_TOKENS (its default for an unknown ID, 32000, is reserved out of the window). sandbox-ai
knows the server's window (llm.context_window, loaded_context) and now tells Claude Code both.

Here: the variables (unit), the real ./sandbox-ai start (end to end, tests/live_fakes.py with an LM Studio-like
server), and the relay between Claude Code and a server's Messages API (the real proxy, tests/devtools/model_mock.py):
streaming, a multi-turn tool call, the system prompt, thinking blocks, stop reasons, count_tokens (Claude Code asks
for no model list unless CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY is set)."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen/qwen3-coder-next"
NAME = "macbook-free-models"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


llm = load("sandbox_llm_claude_window", "sandbox/llm.py")
rt = load("sandbox_runtime_claude_window", "sandbox/runtime.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    assert status == 0, lines
    return "\n".join(lines)


@pytest.fixture
def studio(monkeypatch):
    """LM Studio with qwen/qwen3-coder-next loaded at 65536 tokens (its maximum 262144), registered as the user did."""
    def serve(context=65536, **options):
        server, port = model_mock.start(models=(MODEL,), lmstudio=True, max_context=262144,
                                        loaded={MODEL: context} if context else None, **options)
        servers.append(server)
        run("add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model", MODEL, "--replace")
        return port
    servers = []
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    yield serve
    for server in servers:
        server.shutdown()


def start(**options):
    defaults = {"llm": f"{NAME}:{MODEL}", "agent": "claude", "env": [], "secret_env": [], "llm_param": [],
                "vpn": None}
    return argparse.Namespace(**{**defaults, **options})


def test_regression_claude_code_is_told_the_real_window(studio, capsys):
    """User: '⚠ Model "qwen/qwen3-coder-next" isn't recognized · context assumed to be 1m tokens' - "bin nicht
    sicher ob es sauber funktioniert". Claude Code is told the window LM Studio has the model loaded with, and answers
    of a quarter of it, so it compacts in time."""
    studio(65536)
    env = llm.agent_environment(start())
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "65536"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"
    assert env["ANTHROPIC_MODEL"] == MODEL and "1m tokens" not in capsys.readouterr().err


def test_the_output_limit_follows_the_window_and_the_max_tokens_ceiling(studio):
    studio(262144)
    env = llm.agent_environment(start())
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144" and env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32768"
    run("set", NAME, "--model", MODEL, "--param", "max_tokens=8192")
    assert llm.agent_environment(start())["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192"
    # num_ctx (the start's) is what the agent is told, as for the other agents.
    env = llm.agent_environment(start(llm_param=["num_ctx=131072"]))
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "131072"


def test_your_own_values_win_and_are_not_refused(studio):
    studio(65536)
    a = start(env=["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], secret_env=["CLAUDE_CODE_MAX_OUTPUT_TOKENS=OUT"])
    env = llm.agent_environment(a)
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env and "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env
    a.offline, a.no_inspect, a.agent_args, a.local_config = False, [], [], False
    llm.validate(a, True, True, lambda **kw: True)  # not "--llm sets ... drop it from --env"


def test_an_unknown_window_says_what_claude_code_assumes_and_how_to_set_it(studio, capsys, monkeypatch):
    studio(65536)
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")  # (as behind a VPN: the host does not ask)
    env = llm.agent_environment(start())
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env
    err = capsys.readouterr().err
    assert f"Claude Code does not know the model {MODEL}" in err and "isn't recognized" in err
    assert f"sandbox-ai llm set {NAME} --model {MODEL} --param num_ctx=TOKENS" in err
    # Told once num_ctx is set: no note.
    env = llm.agent_environment(start(llm_param=["num_ctx=65536"]))
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "65536" and "does not know" not in capsys.readouterr().err


def test_a_window_too_small_for_claude_code_is_warned_about(studio, capsys):
    studio(4096)  # LM Studio's default context length for many models
    env = llm.agent_environment(start())
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "4096"
    err = capsys.readouterr().err
    assert "WARNING" in err and "4096 tokens" in err and "Claude Code's own instructions and tools" in err


def test_a_claude_model_keeps_claude_codes_own_window(studio):
    """An ID Claude Code knows (a gateway's claude-sonnet-...): its own window; the variables would not apply."""
    studio(65536)
    run("set", NAME, "--model", "claude-sonnet-4-5", "--param", "num_ctx=65536")
    env = llm.agent_environment(start(llm=f"{NAME}:claude-sonnet-4-5"))
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env and "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env


# --- end to end: the real ./sandbox-ai ------------------------------------------------------------------------------

def test_e2e_a_claude_start_with_lm_studio_passes_the_window(tmp_path):
    """The real ./sandbox-ai (only Podman stood in for, live_fakes.py) starts `claude --llm
    macbook-free-models:qwen/qwen3-coder-next` against an LM Studio-like server: the agent's environment in the
    sandbox's session holds the window LM Studio has the model loaded with."""
    from live_fakes import Host
    server, port = model_mock.start(models=(MODEL,), lmstudio=True, max_context=262144, loaded={MODEL: 65536})
    try:
        host = Host(tmp_path / "host")
        host.add_image("localhost/claude-sandbox:latest")
        env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "", "SANDBOX_AI_SECRETS": "file"}
        added = host.run("llm", "add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model",
                         MODEL, env=env)
        assert added.returncode == 0, added.stderr
        done = host.run("claude", "--no-tmux", "--no-auth", "--llm", f"{NAME}:{MODEL}",
                        env={**env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / "kept")})
        assert done.returncode == 0, done.stderr
        # Claude Code 2.1.322 and 2.1.325 take an ID with '/' as it is (sent unchanged): no doubt cast on it.
        assert "may not accept the model name" not in done.stderr and "does not know the model" not in done.stderr
        given = json.loads((tmp_path / "kept/session/config.json").read_text())["env"]
        assert given["ANTHROPIC_MODEL"] == MODEL and given["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{port}"
        assert given["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "65536" and given["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"
    finally:
        server.shutdown()


# --- the relay between Claude Code and the server's Messages API ----------------------------------------------------

def events_of(body):
    """[(event, data)] of an Anthropic stream."""
    found, kind = [], None
    for line in body.decode().split("\n"):
        if line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("data: "):
            found.append((kind, json.loads(line[6:])))
    return found


def assembled(events):
    """The message an Anthropic stream carries: its content blocks and stop reason."""
    blocks, stop = {}, None
    for kind, data in events:
        if kind == "content_block_start":
            blocks[data["index"]] = dict(data["content_block"], partial="")
        elif kind == "content_block_delta":
            delta, block = data["delta"], blocks[data["index"]]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "input_json_delta":
                block["partial"] += delta["partial_json"]
        elif kind == "message_delta":
            stop = data["delta"]["stop_reason"]
    for block in blocks.values():
        if block["type"] == "tool_use":
            block["input"] = json.loads(block["partial"])
        block.pop("partial")
    return [blocks[i] for i in sorted(blocks)], stop


BASH = {"name": "Bash", "description": "Run a command", "input_schema": {
    "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}
SYSTEM = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
          {"type": "text", "text": "Use the tools.", "cache_control": {"type": "ephemeral"}}]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_relay_serves_claude_code_a_multi_turn_tool_call(tmp_path):
    """Claude Code's requests as it sends them (streamed, its system prompt in blocks, thinking asked for, a tool)
    through the real proxy to a server's Messages API and back: the tool call arrives whole, its result goes back,
    the stop reasons and the thinking block pass, the model's sampling preset is set; count_tokens passes as it
    came."""
    from test_llm_loops import Relay, dechunk, requests_of
    log = tmp_path / "mock.jsonl"
    server, port = model_mock.start(models=(MODEL,), lmstudio=True, loaded={MODEL: 65536}, log=str(log),
                                    log_bodies=True, thinking="The user wants ls.", chunks=5)
    try:
        run("add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model", MODEL)
        relay = Relay(tmp_path, port, argparse.Namespace(llm=f"{NAME}:{MODEL}", agent="claude", llm_param=[]))
        first = {"model": MODEL, "max_tokens": 16384, "stream": True, "system": SYSTEM, "tools": [BASH],
                 "thinking": {"type": "adaptive"}, "metadata": {"user_id": "u"},
                 "messages": [{"role": "user", "content": [{"type": "text", "text": "run: ls -la"}]}]}
        head, body = dechunk(relay.post("/v1/messages?beta=true", first))
        assert b" 200 " in head.split(b"\r\n", 1)[0] and b"text/event-stream" in head
        events = events_of(body)
        assert events[0][0] == "message_start" and events[-1][0] == "message_stop"
        content, stop = assembled(events)
        assert stop == "tool_use" and [b["type"] for b in content] == ["thinking", "tool_use"]
        call = content[1]
        assert call["name"] == "Bash" and call["input"] == {"command": "ls -la"}
        sent = requests_of(log)[-1]["body"]
        assert sent["system"] == SYSTEM and sent["tools"] == [BASH] and sent["thinking"] == {"type": "adaptive"}
        assert sent["max_tokens"] == 16384 and sent["top_k"] == 20  # Qwen3-Coder's recommended sampling
        # The second turn: the assistant's thinking and call, the tool's result.
        second = dict(first, messages=first["messages"] + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                          "content": "total 0\nfile.txt", "cache_control": {"type": "ephemeral"}}]}])
        head, body = dechunk(relay.post("/v1/messages?beta=true", second))
        content, stop = assembled(events_of(body))
        assert stop == "end_turn" and content[-1] == {"type": "text", "text": "mock saw: total 0\nfile.txt"}
        assert requests_of(log)[-1]["body"]["messages"] == second["messages"]  # the history as Claude Code sent it
        # Not streamed (Claude Code's small requests): the message whole.
        head, body = dechunk(relay.post("/v1/messages", dict(second, stream=False)))
        assert json.loads(body)["stop_reason"] == "end_turn"
        # count_tokens is none of the relay's business: the request as it came, the server's answer as it came.
        count = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "tools": [BASH]}
        head, body = dechunk(relay.post("/v1/messages/count_tokens?beta=true", count))
        assert b" 404 " in head.split(b"\r\n", 1)[0] and requests_of(log)[-1]["body"] == count
        relay.close()
    finally:
        server.shutdown()
