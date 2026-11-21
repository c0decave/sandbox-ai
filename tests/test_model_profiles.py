"""Model x harness profiles (0.1.98). User: "the goal is perfect harmony and config files if for instance local model
qwen next coder and opencode 1 is chosen, or same model other harness, or qwen38-27B and codex. The idea is, have
perfect configs and settings for the models ready and auto loaded if the board is started." Decided with the user:
applied automatically and visibly, anything the user sets wins, and it can be switched off.

Here: the shipped table (sandbox/model_profiles.json: every entry valid, every key mapped to a mechanism sandbox-ai has,
nothing private in it), the matching (patterns, the harness wildcard, no match) and the module's checks. The launcher's
use of it (precedence, the start's note, the dry run, the switch) is tests/test_model_profiles_start.py."""
import copy
import importlib.util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mp = load("sandbox_model_profiles_test", "sandbox/model_profiles.py")
llm = load("sandbox_llm_model_profiles_test", "sandbox/llm.py")
TABLE_PATH = ROOT / "sandbox/model_profiles.json"
TABLE = json.loads(TABLE_PATH.read_text())


def checked(document):
    return mp.check_table(document, lambda key, value: llm.param_value("lmstudio", key, value),
                          lambda key, value: llm.loop_setting(key, value))


# --- the shipped table ----------------------------------------------------------------------------------------------

def test_the_shipped_table_is_valid_with_llms_own_checks():
    """Every family and pair: known keys and statuses, parameters sandbox-ai stores (llm.param_value: names, types,
    ranges), loop guard layers (llm.loop_setting), only the variables ENV allows."""
    assert checked(copy.deepcopy(TABLE)) == TABLE
    assert mp.load(TABLE_PATH, None, None)["schema"] == mp.SCHEMA == 1


def test_the_harnesses_are_the_launchers_agents():
    assert mp.HARNESSES == llm.AGENTS


def test_every_key_maps_to_a_mechanism_sandbox_ai_has():
    """params: llm.PARAMS (each sent somewhere, or AGENT_ONLY/LOAD_ONLY); loop_guard: llm.LOOP_DEFAULTS (never
    "enabled"); env: ENV's names for that harness; the BUILTIN list names llm's own constants and functions."""
    for family in TABLE["families"]:
        entries = [("*", family["all"])] + list(family.get("harnesses", {}).items())
        for agent, entry in entries:
            for key in entry.get("params", {}):
                assert key in llm.PARAMS and key in llm.kind_params("lmstudio"), (family["id"], key)
            for key in entry.get("loop_guard", {}):
                assert key in llm.LOOP_DEFAULTS and key != "enabled", (family["id"], key)
            for key in entry.get("env", {}):
                assert key in mp.ENV[agent], (family["id"], agent, key)
    for agent, items in mp.BUILTIN.items():
        assert agent == "*" or agent in llm.AGENTS
        for name, text in items:
            assert hasattr(llm, name), name
            assert text
    assert set(mp.MECHANISMS) == {"params", "num_ctx", "loop_guard", "env", "mcp_max", "tools"}


def test_nothing_private_in_the_table():
    """No address, host name, URL or user path: the table ships to everyone."""
    text = TABLE_PATH.read_text()
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    assert not re.search(r"https?://|\.local\b|\.lan\b|\.home\b|macbook|LMSTUDIO-HOST|/Users/|/home/|/root/|/work/",
                         text, re.I)


def test_the_researchs_findings_are_in_it():
    """The pairs the research measured, with their status, and the per-pair settings of phase 2 (P1 Nemotron +
    Claude Code thinking off, P2 Laguna with few tools); known-bad models and pairs say why."""
    def entry(family, agent):
        return next(f for f in TABLE["families"] if f["id"] == family)["harnesses"][agent]
    # derived (scripts/profile_status.py): rounds 1-2 alone are no tier 1 (1a-1c) - partial; round 5's Claude Code
    # tier 1 made that pair verified
    assert entry("qwen3-coder-next", "opencode")["status"] == "verified"  # round 8 (derived): 10 tier-1 runs in OpenCode
    assert entry("qwen3-coder-next", "codex")["status"] == "partial"
    assert {agent for agent in next(f for f in TABLE["families"] if f["id"] == "qwen3-coder-next")["harnesses"]} == \
        {"opencode", "codex", "claude", "qwen", "goose", "hermes", "dsh", "pi", "opencode2"}
    assert entry("nemotron-3-super", "claude")["env"] == {"CLAUDE_CODE_DISABLE_THINKING": "1"}
    assert entry("nemotron-3-super-mlx", "claude")["status"] == "known-bad"
    laguna = next(f for f in TABLE["families"] if f["id"] == "laguna-s")["all"]
    # Round 1 (0.1.98's MCP fixes): the smoke test with two MCP servers passes in 8 of 9 harnesses -> mcp_max 2.
    assert laguna["mcp_max"] == 2 and "sampling" not in laguna and set(laguna["params"]) == {"num_ctx", "max_tokens"}
    # Round 1: Qwen Code x Laguna-S solved the debugging task once its own loop detection was off; round 3: OpenCode 2
    # x Laguna-S passes the smoke test with the MCP notice's functions named (3 of 4, 0 of 7 before) - both derived
    # partial: no tier 1 (1a-1c) in those harnesses yet.
    assert entry("laguna-s", "qwen")["status"] == "partial" and entry("laguna-s", "opencode2")["status"] == "partial"
    assert entry("qwen3-coder-next", "claude")["status"] == "verified"  # round 5's tier 1 in Claude Code
    qwen38 = next(f for f in TABLE["families"] if f["id"] == "qwen3.8-27b")["all"]
    assert qwen38["status"] == "known-bad" and "prefill" in qwen38["reason"]
    glm = next(f for f in TABLE["families"] if f["id"] == "glm-4.7")["all"]["params"]
    assert glm["max_tokens"] == 16384 and glm["temperature"] == 0.7 and glm["top_p"] == 1.0
    # P2 sampling presets: Nemotron, Gemma-4, GLM-4.7 (the makers' values)
    assert next(f for f in TABLE["families"] if f["id"] == "gemma-4")["all"]["params"]["top_k"] == 64


# --- matching -------------------------------------------------------------------------------------------------------

def test_a_model_and_a_harness_with_an_entry():
    profile = mp.match("qwen/qwen3-coder-next", "opencode", TABLE)
    assert profile["id"] == "qwen3-coder-next/opencode" and profile["name"] == "qwen3-coder-next × opencode"
    assert profile["pair"] is True and profile["status"] == "verified" and "102 requests" in profile["evidence"]
    assert profile["params"] == {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05,
                                 "max_tokens": 16384, "num_ctx": 131072}
    assert profile["loop_guard"] == {"steps": 150} and profile["env"] == {} and profile["warnings"] == []
    assert any("headerTimeout" in text for text in profile["builtin"])


def test_the_pattern_matches_other_spellings_of_the_family():
    for model in ("qwen3-coder-next", "Qwen/Qwen3-Coder-Next-MLX-4bit", "lmstudio-community/qwen3-coder-next-GGUF",
                  "qwen3-coder-next:latest"):
        assert mp.match(model, "codex", TABLE)["family"] == "qwen3-coder-next", model
    assert mp.match("qwen3-coder-30b-a3b-instruct-mlx", "opencode", TABLE)["family"] == "qwen3-coder-30b"
    assert mp.match("qwen3.6-35b-a3b-mlx", "opencode", TABLE)["family"] == "qwen3.6"
    assert mp.match("nvidia-nemotron-3-super-120b-a12b-mlx", "claude", TABLE)["family"] == "nemotron-3-super-mlx"
    assert mp.match("nvidia/nemotron-3-super", "claude", TABLE)["family"] == "nemotron-3-super"


def test_the_harness_wildcard_without_an_entry_for_the_pair():
    """Same model, another harness: the family's settings for every harness apply; the pair is "untested"."""
    profile = mp.match("qwen/qwen3-coder-next", "letta", TABLE)
    assert profile["pair"] is False and profile["status"] == mp.UNTESTED and profile["evidence"] is None
    assert profile["params"]["temperature"] == 0.7 and profile["loop_guard"] == {"steps": 150}


def test_a_model_known_bad_for_every_harness_warns_with_its_reason():
    """qwen3.8-27b and codex: too slow for big contexts, said as a warning; its settings still apply (a smaller
    window so the agents compact in time) - never a refusal."""
    profile = mp.match("qwen/qwen3.8-27b", "codex", TABLE)
    assert profile["status"] == "known-bad" and profile["pair"] is False
    assert profile["warnings"] and "prefill" in profile["warnings"][0]
    assert profile["params"]["num_ctx"] == 49152


def test_a_pair_known_bad_and_the_pairs_own_settings():
    assert mp.match("nvidia-nemotron-3-super-120b-a12b-mlx", "claude", TABLE)["status"] == "known-bad"
    assert "HTTP 500" in mp.match("nvidia-nemotron-3-super-120b-a12b-mlx", "claude", TABLE)["warnings"][0]
    assert mp.match("nvidia-nemotron-3-super-120b-a12b-mlx", "opencode", TABLE)["status"] == mp.UNTESTED
    claude = mp.match("nvidia/nemotron-3-super", "claude", TABLE)
    assert claude["env"] == {"CLAUDE_CODE_DISABLE_THINKING": "1"} and claude["status"] == "partial"
    assert mp.match("nvidia/nemotron-3-super", "opencode", TABLE)["env"] == {}
    laguna = mp.match("poolside/laguna-s-2.1", "codex", TABLE)
    # Round 1: the family's warning (a long tool list, resource tools); Codex's own about read_mcp_resource went
    # with 0.1.98's sentence in its resource tools. Round 7: a second one - a canary fired on runtime 2.73.0.
    assert laguna["mcp_max"] == 2 and len(laguna["warnings"]) == 3 and "resource" in laguna["warnings"][0]
    assert "canary" in laguna["warnings"][1] and "canary" in laguna["warnings"][2]   # round 8: the derived one too


def test_no_match():
    for model in ("llama3:8b", "mistral-small", "", None, "qwen2.5-coder:7b"):
        assert mp.match(model, "opencode", TABLE) is None, model
    assert mp.match("qwen/qwen3-coder-next", "not-an-agent", TABLE) is None


def test_settings_and_their_text():
    profile = mp.match("nvidia/nemotron-3-super", "claude", TABLE)
    values = mp.settings(profile)
    assert values == {"temperature": 1.0, "top_p": 0.95, "max_tokens": 16384, "num_ctx": 131072,
                      "loop_guard.steps": 150, "env.CLAUDE_CODE_DISABLE_THINKING": "1"}
    assert mp.values_text(values) == ("temperature 1.0, top_p 0.95, max_tokens 16384, context 131072, "
                                      "loop_guard.steps 150, CLAUDE_CODE_DISABLE_THINKING=1")
    assert mp.headline(profile) == "profile nemotron-3-super × claude (partial)"


def test_rows_of_the_listing():
    every = mp.rows(TABLE)
    assert {row["agent"] for row in every} >= {"*", "opencode", "codex", "claude"}
    assert sum(1 for row in every if row["agent"] == "*") == len(TABLE["families"])
    one = mp.rows(TABLE, "qwen/qwen3-coder-next", "opencode")
    assert [row["id"] for row in one] == ["qwen3-coder-next/opencode"]
    pairs = mp.rows(TABLE, "qwen/qwen3-coder-next")
    assert len(pairs) == 9
    assert [row["agent"] for row in mp.rows(TABLE, "qwen/qwen3.8-27b")] == ["claude"]   # round 7: a measured pair
    assert mp.rows(TABLE, "llama3:8b") == []


# --- what we know: works, fails, unknown --------------------------------------------------------------------------

def with_knowledge():
    document = copy.deepcopy(TABLE)
    family = next(f for f in document["families"] if f["id"] == "qwen3-coder-next")
    for entry in [family["all"], *family["harnesses"].values()]:
        for key in mp.KNOWLEDGE:
            entry.pop(key, None)
    family["all"].update(works=["fast per step"], unknown=["servers other than LM Studio: not measured"])
    family["harnesses"]["opencode"].update(works=["smoke test passed"], fails=["debug task: time limit"],
                                           unknown=["web task not run", "servers other than LM Studio: not measured"])
    return checked(document)


def test_the_knowledge_of_a_pair_and_of_the_family():
    """A pair's works/fails/unknown first, then the family's own (every harness), each bullet once."""
    table = with_knowledge()
    profile = mp.match("qwen/qwen3-coder-next", "opencode", table)
    assert profile["works"] == ["smoke test passed", "fast per step"]
    assert profile["fails"] == ["debug task: time limit"]
    assert profile["unknown"] == ["web task not run", "servers other than LM Studio: not measured"]
    untested = mp.match("qwen/qwen3-coder-next", "letta", table)
    assert untested["pair"] is False and untested["works"] == ["fast per step"] and untested["fails"] == []
    every = next(row for row in mp.rows(table) if row["id"] == "qwen3-coder-next/*")
    assert every["works"] == ["fast per step"] and every["unknown"] == ["servers other than LM Studio: not measured"]
    assert mp.knowledge_lines(profile) == ["✓ smoke test passed", "✓ fast per step", "✗ debug task: time limit",
                                           "? web task not run", "? servers other than LM Studio: not measured"]


def test_every_entry_of_the_shipped_table_says_what_we_know():
    """User: "a box ... with agent + harness what we know and what we dont with short bullet points and what is
    working and what not". Every family and every pair with an entry: what works or fails; every family what is not
    known (at least the servers not measured, or that no agent ran it); numbers from the research, not words."""
    pairs = 0
    for family in TABLE["families"]:
        every = family["all"]
        assert every.get("works") or every.get("fails"), family["id"]
        assert every.get("unknown"), family["id"]
        for agent, entry in family.get("harnesses", {}).items():
            pairs += 1
            assert entry.get("works") or entry.get("fails"), (family["id"], agent)
            assert list(entry)[-1] in mp.KNOWLEDGE, "the lists come after the entry's own keys"
            for key in mp.KNOWLEDGE:
                assert all(len(text) <= mp.BULLET_LIMIT for text in entry.get(key, []))
    # round 4: seven new families (40 pairs); round 6: Nex-N2.5, Nanbeige4.3 (8 pairs); the derived statuses: four
    # measured pairs that had no entry (Devstral x Codex; Ornith-122B, Flash-Next, KAT x OpenCode 2); round 7:
    # Xing4.0 (2), Bolt (1)
    assert pairs == 111   # round 8: Ornith-35B x OpenCode 2, Qwen3.8-27B x Claude Code (derived)
    qcn = mp.match("qwen/qwen3-coder-next", "opencode", TABLE)
    assert any("168 requests" in text for text in qcn["fails"])
    assert any("8 of 10" in text for text in qcn["works"])
    assert "Ollama, llama.cpp and vLLM servers: not measured (LM Studio only)" in qcn["unknown"]
    laguna = mp.match("poolside/laguna-s-2.1", "claude", TABLE)
    assert any("22 of 22" in text for text in laguna["works"])
    nemotron = mp.match("nvidia-nemotron-3-super-120b-a12b-mlx", "claude", TABLE)
    assert any("HTTP 500" in text for text in nemotron["fails"]) and not nemotron["works"][2:]
    assert "no run in a real agent" in mp.match("qwen/qwen3.8-27b", "codex", TABLE)["unknown"]


def test_an_older_table_without_the_knowledge_is_valid():
    """Tables of 0.1.98 before the lists: valid, the profile's lists empty."""
    document = copy.deepcopy(TABLE)
    for family in document["families"]:
        for entry in [family["all"], *family.get("harnesses", {}).values()]:
            for key in mp.KNOWLEDGE:
                entry.pop(key, None)
    table = checked(document)
    profile = mp.match("qwen/qwen3-coder-next", "opencode", table)
    assert profile["works"] == profile["fails"] == profile["unknown"] == []
    assert mp.knowledge_lines(profile) == []


@pytest.mark.parametrize("where, value, message", [
    ("pair", "smoke ok", "works: a list of short texts"),
    ("pair", ["x" * 121], "at most 120 characters"),
    ("pair", [""], "at most 120 characters"),
    ("pair", [3], "at most 120 characters"),
    ("pair", ["a\nb"], "at most 120 characters"),
    ("pair", ["x"] * 13, "works: a list of short texts"),
    ("all", ["x" * 121], "all: works[0]"),
])
def test_the_knowledge_is_checked(where, value, message):
    document = copy.deepcopy(TABLE)
    family = document["families"][0]
    (family["all"] if where == "all" else family["harnesses"]["opencode"])["works"] = value
    with pytest.raises(ValueError, match=re.escape(message)):
        checked(document)


# --- the checks -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(schema=2), "schema 1"),
    (lambda d: d.update(extra=1), "schema 1"),
    (lambda d: d["families"][0].update(pattern="(["), "no regular expression"),
    (lambda d: d["families"][0].update(id="Bad Id"), "id is a new short name"),
    (lambda d: d["families"][1].update(id=d["families"][0]["id"]), "id is a new short name"),
    (lambda d: d["families"][0].update(examples=["llama3"]), "does not match"),
    (lambda d: d["families"][0].update(examples=[]), "at least one"),
    (lambda d: d["families"][0]["harnesses"]["opencode"].update(status="great"), "status is one of"),
    (lambda d: d["families"][0]["harnesses"]["opencode"].pop("status"), "has a status"),
    (lambda d: d["families"][0]["harnesses"]["opencode"].update(status="known-bad"), "says why"),
    (lambda d: d["families"][0]["harnesses"].update(vim={"status": "verified"}), "is not a harness"),
    (lambda d: d["families"][0]["all"]["params"].update(temperature=9.0), "params temperature: --param temperature: from 0.0 to 2.0"),
    (lambda d: d["families"][0]["all"]["params"].update(nonsense=1), "unknown parameter"),
    (lambda d: d["families"][0]["all"]["params"].update(top_k=2.5), "a whole number"),
    (lambda d: d["families"][0]["all"]["loop_guard"].update(enabled=False), "never enabled"),
    (lambda d: d["families"][0]["all"]["loop_guard"].update(steps=3), "0 (off) or"),
    (lambda d: d["families"][0]["harnesses"]["opencode"].update(env={"CLAUDE_CODE_DISABLE_THINKING": "1"}),
     "env sets only"),
    (lambda d: d["families"][0]["all"].update(colour="red"), "an object of"),
    (lambda d: d["families"][0]["all"].update(notes=["a\x1b[31m red note"]), "printable text"),
])
def test_a_damaged_table_is_refused(change, message):
    document = copy.deepcopy(TABLE)
    change(document)
    with pytest.raises(ValueError, match=re.escape(message)):
        checked(document)


def test_load_reads_a_changed_file_again_and_refuses_damage(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(TABLE))
    assert mp.load(path)["families"][0]["id"] == "qwen3-coder-next"
    changed = copy.deepcopy(TABLE)
    changed["families"] = changed["families"][1:]
    path.write_text(json.dumps(changed) + " ")
    assert mp.load(path)["families"][0]["id"] == changed["families"][0]["id"]
    path.write_text("{" * 100000)
    with pytest.raises(ValueError, match="not JSON"):
        mp.load(path)
    path.write_text(" " * (mp.TABLE_LIMIT + 1))
    with pytest.raises(ValueError, match="larger than"):
        mp.load(path)


def test_nothing_private_in_the_local_model_docs():
    """docs/LOCAL_MODELS.md and its cheat sheet, moved from the research: no address or host name of the machine
    it ran on, no paths of the research tree; commands with sandbox-ai's real options."""
    for name in ("docs/LOCAL_MODELS.md", "docs/LOCAL_MODELS_CHEATSHEET.md"):
        text = (ROOT / name).read_text()
        assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), name
        assert not re.search(r"macbook|LMSTUDIO-HOST|research-llm|/Users/|/home/|/root/|\.local\b|\.lan\b|phase2/",
                             text, re.I), name
        assert "--llm studio:" in text and "sandbox-ai llm profiles" in text, name
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "docs/LOCAL_MODELS.md" in text and "docs/LOCAL_MODELS_CHEATSHEET.md" in text and \
            "--no-llm-profile" in text, readme


def test_a_family_bullet_on_the_pairs_topic_is_dropped():
    table = with_knowledge()
    family = next(f for f in table["families"] if f["id"] == "qwen3-coder-next")
    family["all"]["unknown"] = ["MCP-heavy and web tasks: run in Claude Code only", "Other: open"]
    family["harnesses"]["opencode"]["unknown"] = ["mcp-heavy and web tasks: not run", "Other: open", "Other: open"]
    profile = mp.match("qwen/qwen3-coder-next", "opencode", table)
    assert profile["unknown"] == ["mcp-heavy and web tasks: not run", "Other: open"]
    assert mp.match("qwen/qwen3-coder-next", "letta", table)["unknown"] == \
        ["MCP-heavy and web tasks: run in Claude Code only", "Other: open"]


@pytest.mark.parametrize("query,ids", [("laguna", ["laguna-s"]), ("laguna-s", ["laguna-s"]), ("LAGUNA-S-2", ["laguna-s"]),
                                       ("qwen3.6", ["qwen3.6"]), ("poolside/laguna-s-2.1", ["laguna-s"]),
                                       ("Nemotron", ["nemotron-3-super-mlx", "nemotron-3-super", "nemotron-3.5-super",
                                                     "nemotron-3.5-bolt"]),
                                       ("nothing-like-it", [])])
def test_a_query_finds_the_families_it_names(query, ids):
    assert [f["id"] for f in mp.families_of(query, TABLE)] == ids
    assert [row["family"] for row in mp.rows(TABLE, query, "goose")] == ids


def test_round4_families_and_their_findings():
    """Round 4 of the local-model research (2026-11-17/18): eight new models; the profiles of the seven that loaded,
    each matched by its LM Studio key and its other spellings, with the round's statuses and settings."""
    def family_of(model):
        found = mp.match(model, "opencode", TABLE)
        return found["family"] if found else None
    for model, family in (("qwen3.8-flash-next", "qwen3.8-flash-next"), ("Qwen/Qwen3.8-Flash-Next", "qwen3.8-flash-next"),
                          ("ornith-1.5-122b-mlx", "ornith-1.5-122b"), ("ornith-ai/Ornith-1.5-122B-GGUF", "ornith-1.5-122b"),
                          ("ornith-1.5-35b-a3b-mlx", "ornith-1.5-35b"), ("kat-coder-v2.5-dev", "kat-coder"),
                          ("bartowski/Kwaipilot_KAT-Coder-V2.5-Dev-GGUF", "kat-coder"), ("flint", "flint"),
                          ("lmstudio-community/flint-GGUF", "flint"), ("meta/muse-glimmer", "muse-glimmer"),
                          ("nemotron-3.5-super-vl-120b-a12b", "nemotron-3.5-super"),
                          ("nvidia/nemotron-3-super", "nemotron-3-super"), ("qwen/qwen3.8-27b", "qwen3.8-27b"),
                          ("skyflint-7b", None), ("flintstone", None)):
        assert family_of(model) == family, model
    flash = mp.match("qwen3.8-flash-next", "pi", TABLE)
    assert flash["status"] == "verified" and flash["params"]["temperature"] == 1.0   # round 8: 10 runs in Pi
    assert all(mp.match("qwen3.8-flash-next", agent, TABLE)["status"] == "verified"
               for agent in ("claude", "opencode", "codex", "qwen"))
    assert mp.match("ornith-1.5-122b-mlx", "claude", TABLE)["status"] == "verified"
    assert mp.match("ornith-1.5-122b-mlx", "codex", TABLE)["status"] == "verified"  # round 5-6: 17 runs, 6 cases
    assert mp.match("ornith-1.5-35b-a3b-mlx", "claude", TABLE)["params"]["temperature"] == 0.6
    assert mp.match("flint", "codex", TABLE)["status"] == "partial"  # round 5: its injection canary fired
    assert mp.match("flint", "claude", TABLE)["params"]["max_tokens"] == 16384  # not the card's 32k: a runaway
    assert any("2.73.0" in text for text in mp.match("flint", "codex", TABLE)["warnings"])
    assert any("no edit" in text or "0 of 10" in text for text in mp.match("meta/muse-glimmer", "claude", TABLE)["warnings"])
    assert mp.match("meta/muse-glimmer", "claude", TABLE)["status"] == "partial"  # round 5: review 2/2, vision 1/3
    nemotron = mp.match("nemotron-3.5-super-vl-120b-a12b", "opencode", TABLE)
    assert nemotron["params"]["temperature"] == 0.6 and any("1.0" in text for text in nemotron["warnings"])
    assert "round 4" in TABLE["rounds"]
