"""What the harnesses need from a --llm start beyond the server's address (live report, hotfix 0.1.96: "i had a lot of
mcp issues, web fetch failures, endless loops. and some took ages before they finished"), each found by the
local-model research with the harness's real CLI against a scripted model (/work/research-llm/phase2):

- C3: Hermes refuses a context window below 64,000 tokens, and the user sees only "Something went wrong and I
  couldn't finish this reply" (runs/grow32_hermes: ValueError in agent_init._enforce_minimum_context).
- M1: Claude Code put the MCP servers' tools behind its ToolSearch tool with sandbox-ai's environment (12 tools
  offered, none of the MCP tools: runs/mock_claude); a local model rarely finds them there.
- M3: the files MCP server's read_file answers up to 60,000 characters; OpenCode (tool_output.max_bytes 50000 from
  sandbox-ai) showed the model a preview and a file path instead (evidence/tool_output_limits.txt)."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


llm = load("sandbox_llm_harness_limits", "sandbox/llm.py")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")  # the server is never asked here
    lines = []
    assert llm.main(["add", "mac", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234", "--model",
                     "qwen/qwen3-coder-next"], out=lines.append) == 0, lines


def start(agent, *params, **options):
    defaults = {"llm": "mac:qwen/qwen3-coder-next", "agent": agent, "env": [], "secret_env": [],
                "llm_param": list(params), "vpn": None, "no_provider_defaults": False}
    return argparse.Namespace(**{**defaults, **options})


# --- C3: Hermes and its minimum context window -------------------------------------------------------------------------

def test_regression_hermes_below_64k_is_refused_with_the_fix(capsys):
    """runs/grow32_hermes: told 32,768 tokens, Hermes refused to run and said only 'Something went wrong'."""
    with pytest.raises(ValueError) as refused:
        llm.hermes_config(start("hermes", "num_ctx=32768"))
    message = str(refused.value)
    assert "Hermes" in message and "64,000" in message and "32768" in message and "--llm mac" in message
    assert "num_ctx=65536" in message and "--ctx-size" in message  # the fix named


def test_hermes_with_64k_or_an_unknown_window_starts():
    assert llm.hermes_config(start("hermes", "num_ctx=65536"))["model"]["context_length"] == 65536
    assert llm.hermes_config(start("hermes", "num_ctx=64000"))["model"]["context_length"] == 64000
    assert "context_length" not in llm.hermes_config(start("hermes"))["model"]  # Hermes asks the server itself


def test_hermes_as_a_helper_is_warned_about_not_refused(capsys):
    """Hermes beside another agent (--helper hermes): that agent's start goes on, the warning names the fix."""
    config = llm.hermes_config(start("claude", "num_ctx=32768"))
    assert config["model"]["context_length"] == 32768
    err = capsys.readouterr().err
    assert "WARNING" in err and "64,000" in err and "num_ctx=65536" in err


def test_e2e_a_hermes_start_with_a_32k_window_is_refused(tmp_path):
    """The real ./sandbox-ai (only Podman stood in for, live_fakes.py): `hermes --llm ... --llm-param num_ctx=32768`
    ends before anything runs, with the reason and the fix; with 65536 it starts."""
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/hermes-sandbox:latest")
    env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1", "SANDBOX_AI_SECRETS": "file"}
    added = host.run("llm", "add", "mac", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234", "--model",
                     "qwen/qwen3-coder-next", env=env)
    assert added.returncode == 0, added.stderr
    refused = host.run("hermes", "--no-tmux", "--no-auth", "--llm", "mac:qwen/qwen3-coder-next", "--llm-param",
                       "num_ctx=32768", env={**env, "FAKE_PODMAN_RUN": "0:0"})
    assert refused.returncode != 0 and "Hermes needs a context window of at least 64,000" in refused.stderr
    assert "num_ctx=65536" in refused.stderr and "Traceback" not in refused.stderr
    started = host.run("hermes", "--no-tmux", "--no-auth", "--llm", "mac:qwen/qwen3-coder-next", "--llm-param",
                       "num_ctx=65536", env={**env, "FAKE_PODMAN_RUN": "0:0",
                                             "FAKE_PODMAN_KEEP": str(tmp_path / "kept")})
    assert started.returncode == 0, started.stderr
    assert "context_length: 65536" in (tmp_path / "kept/session/hermes/config.yaml").read_text() or \
        '"context_length": 65536' in (tmp_path / "kept/session/hermes/config.yaml").read_text()


# --- M1: Claude Code offers the MCP servers' tools to the model, not behind ToolSearch ---------------------------------

def test_regression_claude_code_offers_the_mcp_tools_themselves():
    """runs/mock_claude: 12 tools offered, ToolSearch among them, none of the MCP servers' tools. Claude Code 2.1.322
    reads ENABLE_TOOL_SEARCH ('false': its "standard" mode, every tool in the request; "ENABLE_TOOL_SEARCH=false
    avoids the refused request" says its own log)."""
    env = llm.agent_environment(start("claude"))
    assert env["ENABLE_TOOL_SEARCH"] == "false"
    # Yours wins, and is not refused.
    a = start("claude", env=["ENABLE_TOOL_SEARCH"])
    assert "ENABLE_TOOL_SEARCH" not in llm.agent_environment(a)
    a.offline, a.no_inspect, a.agent_args, a.local_config = False, [], [], False
    llm.validate(a, True, True, lambda **kw: True)
    # The other agents: not theirs.
    assert "ENABLE_TOOL_SEARCH" not in llm.agent_environment(start("opencode"))


# --- M3: OpenCode keeps a whole part of the files server's read_file -------------------------------------------------

FILES_READ_CHARS = 60_000  # sandbox-ai-mcp servers/files: MAX_READ_CHARS, one read_file answer at most


def test_regression_opencode_keeps_a_whole_read_file_part():
    """evidence/tool_output_limits.txt: files.read_file of a large log answered 596 lines (60 KB); OpenCode, cut at
    tool_output.max_bytes 50000, showed the model 860 bytes - a preview and the path of a file in its own home."""
    config = llm.opencode_config(llm.endpoint("mac"), "qwen/qwen3-coder-next", loop=dict(llm.LOOP_DEFAULTS))
    limit = config["tool_output"]["max_bytes"]
    # The part as OpenCode gets it: the server's JSON (indent 2) with the text escaped; non-ASCII text in UTF-8.
    part = json.dumps({"path": "data/big.log", "start_line": 1, "end_line": 596, "total_lines": 20000,
                       "content": ("line ü \"quoted\"\tend\n" * 4000)[:FILES_READ_CHARS]}, indent=2,
                      ensure_ascii=False).encode()
    assert len(part) > 50000 and limit >= len(part) and limit >= 2 * FILES_READ_CHARS
    assert config["tool_output"]["max_lines"] >= 1000
    # The compaction layer off: OpenCode's own limits.
    assert "tool_output" not in llm.opencode_config(llm.endpoint("mac"), "qwen/qwen3-coder-next",
                                                    loop={**llm.LOOP_DEFAULTS, "compaction": False})
