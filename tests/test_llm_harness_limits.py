"""What the harnesses need from a --llm start beyond the server's address (live report, hotfix 0.1.96: "i had a lot of
mcp issues, web fetch failures, endless loops. and some took ages before they finished"), each found by the
local-model research with the harness's real CLI against a scripted model (/work/research-llm/phase2):

- C3: Hermes refuses a context window below 64,000 tokens, and the user sees only "Something went wrong and I
  couldn't finish this reply" (runs/grow32_hermes: ValueError in agent_init._enforce_minimum_context).
- M1: Claude Code put the MCP servers' tools behind its ToolSearch tool with sandbox-ai's environment (12 tools
  offered, none of the MCP tools: runs/mock_claude); a local model rarely finds them there.
- M3: the files MCP server's read_file answered up to 60,000 characters; OpenCode (tool_output.max_bytes 50000 from
  sandbox-ai) showed the model a preview and a file path instead (evidence/tool_output_limits.txt). 0.1.96 raised the
  limit to 128 KiB - and then one tool result cost up to 36,000 tokens (runs/t2_debug__opencode-hf__qcn, request 4).
  The fix is the server's: parts of 24,000 characters; OpenCode's limit is 50000 again (phase 2).
- S1: the loop guard's turn limit of 40 ended real work early (runs/t2_debug__opencode-hf__qcn: 41 requests, nothing
  fixed; solved in 102 with a limit of 120)."""
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


# --- S1: the turn limit does not end real work early ------------------------------------------------------------------

def test_regression_the_turn_limit_leaves_room_for_a_real_task():
    """runs/t2_debug__opencode-hf__qcn (steps 40): OpenCode ended after 41 requests with nothing fixed and an empty
    last answer; with 120 the same model solved the task in 102 requests (opencode-hfsn120). Requests the debugging
    task took where it was solved: Laguna 23-36, Qwen3.6 30, Nemotron 21-38, Qwen3-Coder-Next 102 - 40 covers only
    the thinking models. The proxy's guard and the watchdog are the protection against loops, not a low limit."""
    assert llm.LOOP_DEFAULTS["steps"] == 150 >= 102
    assert llm.loop_config(llm.endpoint("mac"))["steps"] == 150
    config = llm.opencode_config(llm.endpoint("mac"), "qwen/qwen3-coder-next", loop=dict(llm.LOOP_DEFAULTS))
    assert config["agent"] == {"build": {"steps": 150}, "plan": {"steps": 67}, "explore": {"steps": 67},
                               "general": {"steps": 100}}
    # The other agents' turn limits come from the same value.
    assert llm.goose_environment(start("goose"), *llm.selection(start("goose")))["GOOSE_MAX_TURNS"] == "150"
    assert llm.pi_llm(start("pi"))["steps"] == 150
    assert llm.proxy_params(start("codex"), *llm.selection(start("codex")))["loop"]["steps"] == 150


def test_your_own_turn_limit_still_wins():
    a = start("opencode", "loop_guard.steps=40")
    assert llm.loop_guard_of(a, llm.selection(a)[0])["steps"] == 40
    assert llm.goose_environment(start("goose", "loop_guard.steps=60"),
                                 *llm.selection(start("goose")))["GOOSE_MAX_TURNS"] == "60"
    a = start("opencode", "loop_guard.steps=off")
    assert llm.loop_guard_of(a, llm.selection(a)[0])["steps"] == 0  # OpenCode's own: no limit


# --- M3: OpenCode keeps a whole part of the files server's read_file -------------------------------------------------

FILES_READ_CHARS = 24_000  # sandbox-ai-mcp servers/files: MAX_READ_CHARS, one read_file answer at most (was 60,000)


def read_file_part(characters, line="line ü \"quoted\"\tend\n"):
    """One read_file answer as OpenCode gets it: the server's JSON (indent 2) with the text escaped, non-ASCII text
    in UTF-8."""
    return json.dumps({"path": "data/big.log", "start_line": 1, "end_line": 596, "total_lines": 20000,
                       "content": (line * (characters // len(line) + 1))[:characters]}, indent=2,
                      ensure_ascii=False).encode()


def test_regression_opencode_keeps_a_whole_read_file_part():
    """evidence/tool_output_limits.txt: files.read_file of a large log answered 596 lines (60 KB); OpenCode, cut at
    tool_output.max_bytes 50000, showed the model 860 bytes - a preview and the path of a file in its own home. The
    files server now answers at most 24,000 characters a part: a whole one fits OpenCode's limit, escapes and UTF-8
    counted."""
    config = llm.opencode_config(llm.endpoint("mac"), "qwen/qwen3-coder-next", loop=dict(llm.LOOP_DEFAULTS))
    limit = config["tool_output"]["max_bytes"]
    assert len(read_file_part(FILES_READ_CHARS)) <= limit
    assert len(read_file_part(FILES_READ_CHARS, "\t\"wert\": \"größe äöü\",\n")) <= limit  # every line escapes
    assert len(read_file_part(60_000)) > limit, "the old part size is why the limit alone could not be the fix"
    assert config["tool_output"]["max_lines"] >= 1000


def test_regression_one_tool_result_does_not_cost_tens_of_thousands_of_tokens():
    """runs/t2_debug__opencode-hf__qcn, request 4 (tool_output.max_bytes 128 KiB): one `read` took the request from
    39 KB and 9,297 tokens to 98 KB and 45,143 tokens - 46.7 s before the first token on the fastest model, and a
    third of a 131k window gone. Digit-heavy text (a log) is about 1.5 bytes a token: 128 KiB allowed 87,000 tokens
    in one result, 50,000 bytes allow 33,000."""
    assert llm.OPENCODE_TOOL_OUTPUT == {"max_lines": 1000, "max_bytes": 50000}
    config = llm.opencode_config(llm.endpoint("mac"), "qwen/qwen3-coder-next", loop=dict(llm.LOOP_DEFAULTS))
    assert config["tool_output"] == {"max_lines": 1000, "max_bytes": 50000}
    assert config["tool_output"]["max_bytes"] / 1.5 < 131072 / 3, "one result stays under a third of a 131k window"
    # The compaction layer off: OpenCode's own limits.
    assert "tool_output" not in llm.opencode_config(llm.endpoint("mac"), "qwen/qwen3-coder-next",
                                                    loop={**llm.LOOP_DEFAULTS, "compaction": False})


# --- Qwen Code: its heuristic loop detection takes a local model's reasoning for a loop --------------------------------

def test_regression_qwen_codes_thought_detection_does_not_stop_a_thinking_local_model():
    """Research round 1 (runs/t2_debug__qwen__laguna__r1 and __r2, t2_debug__qwen__q36__r1; Qwen Code 0.26.4, LM
    Studio): 61-72 s into the debugging task Qwen Code ended the run - "Loop detection halted the run
    (repetitive_thoughts: the model repeated the same reasoning thought). Set the `model.skipLoopDetection` setting
    to true to disable." - with Laguna-S twice of two runs and with Qwen3.6-35B; its detector compares the first
    200 characters of the last three "thoughts", and a local server streams one thought in many pieces. The setting
    switches Qwen Code's heuristics off (thoughts, repeated sentences, read loops); its protection against a
    repeated tool call stays (loopProtection), and sandbox-ai's own guard holds on the wire as for every agent."""
    settings = llm.qwen_settings(start("qwen"))
    assert settings["model"]["skipLoopDetection"] is True
    assert settings["model"]["loopProtection"] == "stop"
    # The loop guard off: Qwen Code's tool-call protection is its own again; its heuristics stay off for --llm.
    off = llm.qwen_settings(start("qwen", "loop_guard.enabled=false"))
    assert off["model"]["skipLoopDetection"] is True and "loopProtection" not in off["model"]


# --- Qwen Code: its tool for MCP resources, taken for the way to an MCP server's tools ---------------------------------

def test_regression_qwen_code_offers_no_mcp_resource_tool_to_a_local_model():
    """Research round 1 (runs/smoke__qwen__laguna__r2; Qwen Code 0.26.4, LM Studio, sandbox-ai's MCP relay): asked for
    "the MCP tool read_file of the server named files", Laguna-S found Qwen Code's deferred read_mcp_resource
    ("Reads a resource from a configured MCP server by server_name and URI") with tool_search and called it six times
    through tool_call with guessed URIs - "Invalid request parameters", "Unknown resource" - until Qwen Code stopped
    the run; mcp__files__read_file was offered all along. Qwen Code lists the tool whatever a server advertises (the
    MCP relay's dropped capabilities do not reach it), and its description is not in the request's tools, where the
    proxy's sentence goes (Codex, DSH, Goose). With --llm the tool is left out (tools.exclude: no longer in the
    system prompt's list of deferred tools, checked with the real CLI: evidence/qwen_resource_tool.txt)."""
    settings = llm.qwen_settings(start("qwen"))
    assert settings["tools"]["exclude"] == ["read_mcp_resource"]
    assert "read_mcp_resource" in llm.qwen_settings(start("qwen", "loop_guard.enabled=false"))["tools"]["exclude"]
