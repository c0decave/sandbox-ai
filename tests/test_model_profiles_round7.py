"""The local-model research's round 7 (2026-11-20, LM Studio's llama.cpp runtime 2.73.0): Xing4.0-29B-A4B's family -
its card's sampling and a warning: on LM Studio 2.73.0 it answers "7777..." after the first tool result (its chat
template's think block for the last assistant turn; the loaded model stays broken) - research-llm/round7.md."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_model_profiles_r7", ROOT / "sandbox/model_profiles.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)
TABLE = mp.load()


def test_xing_40_matches_with_its_card_sampling_and_warns_it_breaks_after_a_tool_result():
    for model in ("xing4.0-29b-a4b", "XingChen-AGI/Xing4.0-29B-A4B-GGUF"):
        assert mp.match(model, "opencode", TABLE)["family"] == "xing4.0", model
    profile = mp.match("xing4.0-29b-a4b", "claude", TABLE)
    assert profile["params"]["temperature"] == 1.0 and profile["params"]["repeat_penalty"] == 1.05
    assert profile["status"] == "failing" and any("7777" in w for w in profile["warnings"])


def test_round_7_runtime_notes_and_the_laguna_canary():
    """Runtime 2.73.0 (round 7): every re-checked family says how it did; Laguna-S warns that a canary fired (an MCP
    ticket's planted command ran in 1 of 2 runs, research-llm/round7.md "2"); Nemotron 3.5 Bolt has a family now."""
    for model in ("qwen3.8-flash-next", "kat-coder-v2.5-dev", "gemma-4-12b-it", "meta/muse-glimmer",
                  "poolside/laguna-s-2.1", "qwen/qwen3.8-27b", "nemotron-3.5-super-vl-120b-a12b"):
        profile = mp.match(model, "claude", TABLE)
        assert any("2.73" in note for note in profile["notes"]), model
    laguna = mp.match("poolside/laguna-s-2.1", "claude", TABLE)
    assert any("canary" in w and "untrusted" in w for w in laguna["warnings"])
    bolt = mp.match("nemotron-3.5-bolt-74b-a9b", "claude", TABLE)
    assert bolt["family"] == "nemotron-3.5-bolt" and bolt["status"] == "partial"   # derived: 8 runs, mean 0.34
