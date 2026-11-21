"""The local-model research's round 6 (2026-11-19/20): the two new families - Nex-N2.5-mini (fast; fooled by a
planted command in an MCP ticket in OpenCode) and Nanbeige4.3-33B (too slow for agent work on the research Mac) -
with their makers' sampling, their measured pairs and a warning each (research-llm/round6.md "17. New models")."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_model_profiles_r6", ROOT / "sandbox/model_profiles.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)
TABLE = mp.load()


def test_nex_n25_mini_matches_with_its_card_sampling_and_the_injection_warning():
    profile = mp.match("nex-n2.5-mini", "opencode", TABLE)
    assert profile["family"] == "nex-n2.5"
    assert profile["params"]["temperature"] == 0.7 and profile["params"]["top_k"] == 40
    assert profile["status"] == "partial" and any("MCP ticket" in w for w in profile["warnings"])
    assert mp.match("abenzerps/Nex-N2.5-mini-GGUF", "qwen", TABLE)["status"] == "partial"


def test_nanbeige_43_matches_and_warns_it_is_too_slow():
    profile = mp.match("nanbeige_nanbeige4.3-33b", "qwen", TABLE)
    assert profile["family"] == "nanbeige4.3"
    assert profile["params"]["temperature"] == 1.0
    assert any("tokens/s" in w for w in profile["warnings"])
    assert mp.match("Nanbeige/Nanbeige4.3-33B", "codex", TABLE)["status"] == "failing"
