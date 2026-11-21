"""The loop guard's progress_notes layer (local-model research round 5, the memory A/B): a one-line instruction to
keep PROGRESS.md (what is done, what is next; read it first after an interruption or a compaction) beside the loop
guard's instructions - off by default, switched on by a model profile's pair (loop_guard.progress_notes) or the start
(--llm-param loop_guard.progress_notes=on). Claude Code, which gets no loop-guard text, gets the line through
--append-system-prompt; the others with their loop-guard instructions. Round 5: Ornith-1.5-35B in Claude Code got
the forced-compaction task done only with the PROGRESS.md line (1.0 vs 0.5: runs/c11_compact-notes__claude__orn35__m1,
c11_compact__claude__orn35__b1)."""
import argparse
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_llm_progress_notes", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")
    lines = []
    assert llm.main(["add", "mac", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234", "--model",
                     "qwen/qwen3-coder-next"], out=lines.append) == 0, lines


def start(agent, *params):
    return argparse.Namespace(llm="mac:qwen/qwen3-coder-next", agent=agent, env=[], secret_env=[],
                              llm_param=list(params), vpn=None, no_provider_defaults=False, llm_models=None)


def test_off_by_default_everywhere():
    assert llm.LOOP_DEFAULTS["progress_notes"] is False
    assert llm.agent_arguments(start("claude")) == []
    assert "PROGRESS.md" not in llm.agent_arguments(start("qwen"))[1]
    assert llm.loop_text({"instructions": True}) == llm.LOOP_TEXT


def test_switched_on_the_line_reaches_every_harness():
    on = "loop_guard.progress_notes=on"
    claude = llm.agent_arguments(start("claude", on))
    assert claude[0] == "--append-system-prompt" and "PROGRESS.md" in claude[1]
    qwen = llm.agent_arguments(start("qwen", on))
    assert qwen[1].startswith(llm.LOOP_TEXT) and "PROGRESS.md" in qwen[1]
    assert "PROGRESS.md" in llm.goose_loop_text(start("goose", on))
    found, _ = llm.selection(start("opencode", on))
    assert "PROGRESS.md" in llm.loop_text(llm.loop_guard_of(start("opencode", on), found))


def test_the_layer_is_checked_like_the_others():
    assert llm.loop_setting("progress_notes", "on") is True and llm.loop_setting("progress_notes", "off") is False
    with pytest.raises(ValueError):
        llm.loop_setting("progress_notes", "7")
    # the whole guard off switches it off too
    assert "PROGRESS.md" not in (llm.goose_loop_text(start("goose", "loop_guard.progress_notes=on",
                                                           "loop_guard=off")) or "")


# The pairs whose model profile switches the line on: measured in round 5's memory A/B (3 runs per cell each way,
# research pass M3) - and only those; every other pair keeps it off.
PROFILED = {("qwen/qwen3.8-flash-next", "claude"), ("ornith-1.5-35b-a3b-mlx", "claude"),
            ("ornith-1.5-35b-a3b-mlx", "opencode")}


def start_model(model, agent, *params):
    return argparse.Namespace(llm=f"mac:{model}", agent=agent, env=[], secret_env=[], llm_param=list(params),
                              vpn=None, no_provider_defaults=False, llm_models=None)


@pytest.mark.parametrize("model,agent", sorted(PROFILED))
def test_regression_a_profile_switches_the_line_on_where_it_was_measured(model, agent):
    """Round 5: Flash-Next in Claude Code ended its turn after a team member's first task in 2 of 3 runs without the
    line (t10_member 0.40/1.00/0.40) and waited for the next task with it (1.00/1.00/1.00); Ornith-35B lost its
    place in the forced-compaction task without it in 6 of 6 runs (c11_compact: Claude Code 0.50/0.33/0.33, OpenCode
    0.33/0.33/0.33) and finished it with it in 4 of 6 (Claude Code 1.00/1.00/0.54, OpenCode 1.00/1.00/0.33)."""
    found, _ = llm.selection(start_model(model, agent))
    assert llm.loop_guard_of(start_model(model, agent), found).get("progress_notes") is True
    if agent == "claude":
        arguments = llm.agent_arguments(start_model(model, agent))
        assert arguments[:1] == ["--append-system-prompt"] and "PROGRESS.md" in arguments[1]
        # the start's own switch still wins
        assert llm.agent_arguments(start_model(model, agent, "loop_guard.progress_notes=off")) == []
    else:
        assert "PROGRESS.md" in llm.loop_text(llm.loop_guard_of(start_model(model, agent), found))


@pytest.mark.parametrize("model,agent", [("qwen/qwen3.8-flash-next", "opencode"), ("qwen/qwen3-coder-next", "claude"),
                                         ("google/gemma-4-12b-it", "claude"), ("ornith-1.5-122b-mlx", "claude"),
                                         ("ornith-1.5-35b-a3b-mlx", "codex")])
def test_pairs_without_a_measured_gain_keep_it_off(model, agent):
    found, _ = llm.selection(start_model(model, agent))
    assert llm.loop_guard_of(start_model(model, agent), found).get("progress_notes") is False
