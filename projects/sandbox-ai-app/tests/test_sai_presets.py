"""sandbox-ai's own presets in the gallery: converted to the board's presets and checked as an imported one."""
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend  # noqa: E402
from sandbox_ai_app import presets as PR  # noqa: E402


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    sai = backend.SandboxAI()
    try:
        sai.module("presets")
    except Exception:  # noqa: BLE001
        pytest.skip("this sandbox-ai (SANDBOX_AI_HOME) has no presets yet")
    return sai


def test_every_preset_of_sandbox_ai_becomes_one_of_the_gallery(sai):
    found, problems = PR.sandbox_ai_presets(sai)
    assert problems == [] and len(found) >= 20
    by_id = {p.id: p for p in found}
    analysis = by_id["sai-software-analysis"]
    assert analysis.category == PR.SAI_CATEGORY and "--offline" not in analysis.options
    assert [p.marker for p in analysis.placeholders] == ["<folder:samples>"]
    assert "<folder:samples>:/mnt/samples:ro" in analysis.options
    assert list(analysis.mcp) == ["capa", "floss", "radare2", "yara", "binwalk"]
    capsule = by_id["sai-time-capsule"]
    assert capsule.agent_args == () and "code archaeologist" in capsule.prompt and "<text:question>" in capsule.prompt
    assert by_id["sai-sand-chess"].agents == ("claude", "opencode")


def test_a_converted_preset_builds_a_board(sai, tmp_path):
    preset = {p.id: p for p in PR.sandbox_ai_presets(sai)[0]}["sai-chaos-monkey"]
    board = PR.build(sai, preset, str(tmp_path), agent="claude")
    assert board is not None


def test_an_older_sandbox_ai_has_none(sai, monkeypatch):
    monkeypatch.setattr(sai, "module", lambda name: (_ for _ in ()).throw(FileNotFoundError(name)))
    assert PR.sandbox_ai_presets(sai) == ([], [])


def test_a_value_with_quotes_stays_inside_the_agents_argument(sai, tmp_path):
    """The task typed for {question} holds a quote and a dash: it stays the one argument the prompt is."""
    import shlex
    from sandbox_ai_app import board as B
    preset = {p.id: p for p in PR.sandbox_ai_presets(sai)[0]}["sai-time-capsule"]
    built = PR.build(sai, preset, str(tmp_path), agent="claude", values={"<text:question>": "why don't --we 'merge'"})
    agent = next(n for n in built.nodes if n.kind == "agent")
    tokens = shlex.split(agent.props["arguments"])
    assert len(tokens) == 1 and "why don't --we 'merge'" in tokens[0] and "code archaeologist" in tokens[0]


def test_opencode_gets_the_task_as_prompt_and_a_preset_keeps_to_its_agents(sai, tmp_path):
    import shlex
    found = {p.id: p for p in PR.sandbox_ai_presets(sai)[0]}
    board = PR.build(sai, found["sai-time-capsule"], str(tmp_path), agent="opencode", values={"<text:question>": "why"})
    tokens = shlex.split(next(n for n in board.nodes if n.kind == "agent").props["arguments"])
    assert tokens[0] == "--prompt" and "code archaeologist" in tokens[1]
    chess = PR.build(sai, found["sai-sand-chess"], str(tmp_path), agent="codex")  # not for codex: its first agent
    assert next(n for n in chess.nodes if n.kind == "agent").props.get("agent") == "claude"


def test_a_value_holding_another_marker_is_not_filled_again(sai, tmp_path):
    import shlex
    from sandbox_ai_app import board as B
    board = B.from_argv(sai, "claude", [], ["look at <text:a> and <text:b>"], [str(tmp_path)])
    B.fill(board, {"<text:a>": "<text:b>", "<text:b>": "x' --dangerously-skip-permissions '"})
    tokens = shlex.split(next(n for n in board.nodes if n.kind == "agent").props["arguments"])
    assert len(tokens) == 1 and tokens[0].startswith("look at <text:b> and x'")


def test_a_presets_mode_is_a_floor_not_a_weakening(sai):
    """A sandbox-ai preset's --mode becomes --mode-at-least: started from the board it must not weaken a stricter
    mode of the user's defaults or policy (sandbox-ai's own --preset start keeps it as a floor too)."""
    assert PR.floor_mode(["--mode", "strict", "--gui"]) == ["--mode-at-least", "strict", "--gui"]
    assert PR.floor_mode(["-m", "paranoid"]) == ["--mode-at-least", "paranoid"]
    assert PR.floor_mode(["--mode=strict"]) == ["--mode-at-least=strict"]
    assert PR.floor_mode(["--mode"]) == ["--mode"]
    by_id = {p.id: p for p in PR.sandbox_ai_presets(sai)[0]}
    audit = by_id["sai-code-audit"]
    assert "--mode" not in audit.options and "--mode-at-least" in audit.options
