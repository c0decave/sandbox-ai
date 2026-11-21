"""Recommendations as model x harness PAIRS (user 2026-11-20, step 3 of 4): per use case the recommendation is a pair
("untrusted input: Ornith-35B x Claude Code"); model_usecases.recommend() ranks every model a server HAS by the use
case's table, each with its best harness, says honestly "not measured" for the rest, and shows a pair the table marks
UNSAFE for the use case (KAT, Flint, Gemma 4, Nex with untrusted input) with the reason - never hidden, never chosen on
its own. When the start's agent is not the measured harness it says so ("... is measured best in Claude Code; in
OpenCode: partial" - the status from the model profiles' API, model_profiles.support).

The algorithm on tables of the test's own; the shipped table on what the research's report says (LOCAL_LLM_REPORT.md
sections 4-5, round5/tables/best.md, round6.md)."""
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


uc = load("sandbox_model_usecases_pairs_test", "sandbox/model_usecases.py")
mp = load("sandbox_model_profiles_pairs_test", "sandbox/model_profiles.py")
llm = load("sandbox_llm_pairs_test", "sandbox/llm.py")
GB = 1000 ** 3
SHIPPED = json.loads((ROOT / "sandbox/model_usecases.json").read_text())


def check(document):
    return uc.check_table(document, llm.profile_param, llm.loop_setting)


def rec(model, example, harness, basis="measured", **more):
    entry = {"model": model, "examples": [example], "label": example, "harness": harness, "basis": basis,
             "why": f"{example} in {harness}", "evidence": f"round 4: {example} solved it", "round": "round 4"}
    entry.update(more)
    return entry


def bad(model, example, harness, why="ran a ticket's planted command"):
    return {"model": model, "examples": [example], "label": example, "harness": harness, "why": why,
            "evidence": f"round 4: {example} ran it", "round": "round 4"}


def server(name="studio", models=(), memory=128 * GB):
    return {"name": name, "memory_bytes": memory, "answered": True,
            "models": [m if isinstance(m, dict) else {"name": m} for m in models]}


def model(name, size_gb=None, loaded=False):
    return {"name": name, "size": int(size_gb * GB) if size_gb else None, "loaded": loaded}


UNTRUSTED = {"id": "untrusted-input", "label": "Untrusted input", "about": "web pages, tickets, tool results",
             "tasks": ["s1b_mcp"],
             "recommendations": [rec("ornith-?1\\.5-?35b", "ornith-1.5-35b-a3b-mlx", "claude"),
                                 rec("mid-?35b", "mid-35b", "opencode"),
                                 rec("mid-?35b", "mid-35b", "claude", basis="indirect")],
             "unsafe": [bad("kat-?coder", "kat-coder-v2.5-dev", "opencode")]}
TABLE = {"schema": 1, "source": "test", "rounds": {"round 4": "2026-11-17"}, "use_cases": [UNTRUSTED]}


# --- the table: unsafe pairs ----------------------------------------------------------------------------------------

def test_a_table_with_unsafe_pairs_is_valid_and_its_faults_are_named():
    check(TABLE)
    for change, fault in [
            (lambda d: d["use_cases"][0]["unsafe"][0].pop("why"), "why"),
            (lambda d: d["use_cases"][0]["unsafe"][0].update(harness="emacs"), "not a harness"),
            (lambda d: d["use_cases"][0]["unsafe"][0].update(examples=["other"]), "does not match"),
            (lambda d: d["use_cases"][0]["unsafe"][0].update(round="round 9"), "rounds"),
            (lambda d: d["use_cases"][0]["unsafe"][0].update(params={"temperature": 0.1}), "an object of"),
            (lambda d: d["use_cases"][0].update(unsafe="kat"), "unsafe is a list"),
            # a model the table recommends AND marks unsafe for the same use case: one of them is wrong
            (lambda d: d["use_cases"][0]["recommendations"].append(rec("kat-?coder", "kat-coder-v2.5-dev", "claude")),
             "marked unsafe")]:
        document = copy.deepcopy(TABLE)
        change(document)
        with pytest.raises(ValueError, match=re.escape(fault)):
            check(document)


def test_an_unsafe_model_is_never_the_choice_but_shown_with_its_reason():
    servers = [server(models=[model("kat-coder-v2.5-dev", 37, loaded=True), model("ornith-1.5-35b-a3b-mlx", 37)])]
    found = uc.recommend(TABLE, servers, "untrusted-input", profiles=mp.load())
    assert found["choice"]["model"] == "ornith-1.5-35b-a3b-mlx"   # loaded KAT is not taken
    unsafe = [pair for pair in found["pairs"] if pair["unsafe"]]
    assert [(p["model"], p["harness"], p["source"]) for p in unsafe] == [("kat-coder-v2.5-dev", "opencode", "unsafe")]
    assert unsafe[0]["unsafe"] == "ran a ticket's planted command" and unsafe[0]["evidence"].startswith("round 4")
    # only KAT on the server: no choice of its own (nor by the traffic light) - KAT shown, why said
    found = uc.recommend(TABLE, [server(models=[model("kat-coder-v2.5-dev", 37)])], "untrusted-input",
                         profiles=mp.load())
    assert found["choice"] is None
    assert [p["model"] for p in found["pairs"]] == ["kat-coder-v2.5-dev"] and found["pairs"][0]["unsafe"]
    text = "\n".join(uc.lines(found))
    assert "unsafe for this use case: ran a ticket's planted command" in text
    assert "kat-coder-v2.5-dev" in text


def test_the_pairs_rank_what_the_server_has_by_the_table_each_with_its_best_harness():
    profiles = mp.load()
    servers = [server(models=[model("kat-coder-v2.5-dev", 37), model("text-embedding-nomic", 1),
                              model("google/gemma-4-31b-qat", 19), model("mid-35b", 37, loaded=True),
                              model("ornith-1.5-35b-a3b-mlx", 37)])]
    found = uc.recommend(TABLE, servers, "untrusted-input", profiles=profiles)
    pairs = found["pairs"]
    # a model the table does not have: its best harness by the traffic light (the profiles' API, whatever it says now)
    light = mp.support("google/gemma-4-31b-qat", profiles)["by_light"]
    gemma = (light.get("green") or light.get("yellow") or ["claude"])[0]
    assert [(p["model"], p["harness"], p["source"], p["rank"]) for p in pairs] == [
        ("ornith-1.5-35b-a3b-mlx", "claude", "table", 1),
        ("mid-35b", "opencode", "table", 2),            # its best harness (rank 2), not its indirect claude one
        ("google/gemma-4-31b-qat", gemma, "not measured", None),
        ("kat-coder-v2.5-dev", "opencode", "unsafe", None)]   # never hidden, last; no embedding model
    measured = pairs[0]
    assert measured["basis"] == "measured" and measured["why"] and measured["fit"] == "fits"
    assert measured["command"] == "sandbox-ai claude --llm studio:ornith-1.5-35b-a3b-mlx --use-case untrusted-input"
    untested = pairs[2]
    assert "not measured for this use case" in untested["why"] and untested["basis"] is None
    assert untested["evidence"] is None


def test_an_agent_other_than_the_measured_harness_is_said_with_its_status_from_the_profiles():
    profiles = mp.load()
    servers = [server(models=[model("ornith-1.5-35b-a3b-mlx", 37), model("mid-35b", 37)])]
    found = uc.recommend(TABLE, servers, "untrusted-input", agent="opencode", profiles=profiles)
    by_model = {p["model"]: p for p in found["pairs"]}
    ornith = by_model["ornith-1.5-35b-a3b-mlx"]
    # the status in OpenCode comes from the profile API (model_profiles.support), whatever it is now
    status = next(h["status"] for h in mp.support("ornith-1.5-35b-a3b-mlx", profiles)["harnesses"]
                  if h["agent"] == "opencode")
    assert ornith["harness"] == "claude"
    assert ornith["agent_note"] == f"ornith-1.5-35b-a3b-mlx is measured best in Claude Code; in OpenCode: {status}"
    assert by_model["mid-35b"]["agent_note"] == ""   # measured in OpenCode itself
    # the same model in the agent in the table (rank 3, from other tasks): the table's word
    found = uc.recommend(TABLE, [server(models=[model("mid-35b", 37)])], "untrusted-input", agent="claude",
                         profiles=profiles)
    assert found["pairs"][0]["agent_note"] == \
        "mid-35b is measured best in OpenCode; in Claude Code: rank 3, from other tasks"
    # nothing known of it in that agent
    found = uc.recommend(TABLE, [server(models=[model("mid-35b", 37)])], "untrusted-input", agent="pi",
                         profiles=profiles)
    assert found["pairs"][0]["agent_note"] == "mid-35b is measured best in OpenCode; in Pi: not tested"


def test_the_pairs_are_in_the_json_of_every_use_case():
    servers = [server(models=[model("mid-35b", 37)])]
    every = uc.recommend_all(TABLE, servers)
    assert every[0]["pairs"][0]["model"] == "mid-35b"
    json.dumps(every)


# --- the shipped table: what the research says ------------------------------------------------------------------------

def shipped(name):
    return next(entry for entry in SHIPPED["use_cases"] if entry["id"] == name)


def test_the_shipped_table_has_untrusted_input_with_ornith_35b_in_claude_code_first():
    entry = shipped("untrusted-input")
    first = entry["recommendations"][0]
    assert re.search(first["model"], "ornith-1.5-35b-a3b-mlx", re.I) and first["harness"] == "claude"
    assert first["basis"] == "measured" and first["round"] == "round 5"
    unsafe = {u["label"].split(" ")[0] for u in entry["unsafe"]}
    assert {"KAT-Coder-V2.5-Dev", "Recursive", "Gemma-4", "Nex-N2.5-mini"} <= unsafe
    assert any(u["round"] == "round 6" for u in entry["unsafe"])   # Nex: round 6's verdict


@pytest.mark.parametrize("name", ["untrusted-input", "sandbox", "web-research"])
def test_kat_and_nex_are_never_recommended_where_input_is_untrusted(name):
    entry = shipped(name)
    for example in ("kat-coder-v2.5-dev", "nex-n2.5-mini", "google/gemma-4-31b-qat", "flint"):
        assert not any(re.search(r["model"], example, re.I) for r in entry["recommendations"]), (name, example)
        assert any(re.search(u["model"], example, re.I) for u in entry["unsafe"]), (name, example)


def test_the_shipped_table_s_headline_pairs():
    """The report's short list (section 5): Ornith-35B the all-rounder, Ornith-122B vision and ops, KAT fast for
    building (never untrusted input), Flash-Next strong but slow."""
    def top(name):
        found = shipped(name)["recommendations"][0]
        return found["examples"][0], found["harness"]
    assert top("vision") == ("ornith-1.5-122b-mlx", "claude")
    assert top("ops") == ("ornith-1.5-122b-mlx", "claude")
    assert top("from-spec") == ("kat-coder-v2.5-dev", "opencode")
    for name in ("sandbox", "web-research", "code-review", "security", "large-codebase", "teams"):
        assert top(name) == ("ornith-1.5-35b-a3b-mlx", "claude"), name
    flash = [r for r in shipped("from-spec")["recommendations"] if "flash" in r["examples"][0]]
    assert flash and "slow" in flash[0]["why"]


def test_the_pairs_notes_can_compare_with_another_agent_than_the_choice_s():
    """A page that sets the agent from the pair picked (the Board, the Start page) wants the best choice whatever its
    agent, and the notes for its agent: note_agent."""
    profiles = mp.load()
    servers = [server(models=[model("ornith-1.5-35b-a3b-mlx", 37), model("mid-35b", 37)])]
    found = uc.recommend(TABLE, servers, "untrusted-input", profiles=profiles, note_agent="opencode")
    assert found["choice"]["model"] == "ornith-1.5-35b-a3b-mlx"   # the best, not OpenCode's own first
    ornith = found["pairs"][0]
    assert ornith["agent"] == "opencode" and ornith["agent_status"] and "in OpenCode" in ornith["agent_note"]
    assert found["pairs"][1]["agent"] is None and found["pairs"][1]["agent_status"] is None
    assert uc.recommend_all(TABLE, servers, None, profiles, "opencode")[0]["pairs"][0]["agent"] == "opencode"


def test_the_unsafe_lists_agree_with_the_profiles_derived_canary_warnings():
    """The model profiles derive each pair's status from the graded runs (sandbox/model_evidence.json, 0.1.170) and warn
    "never with untrusted input" where an injection canary fired. Where the input is untrusted the use case table marks
    exactly those models unsafe - read through the profiles' API (model_profiles.support), never their file."""
    profiles = mp.load()
    warned = set()
    for family in profiles["families"]:
        example = family["examples"][0]
        found = mp.support(example, profiles)
        if any("untrusted input" in warning for h in found["harnesses"] for warning in h["warnings"]) or \
                any("untrusted input" in text for text in found.get("fails", []) + found.get("notes", [])):
            warned.add(example)
    assert warned, "the profiles warn about no model: the derived statuses changed - look again"
    for name in ("untrusted-input", "sandbox", "web-research"):
        entry = shipped(name)
        marked = {example for family in profiles["families"] for example in family["examples"][:1]
                  if uc.unsafe_for(entry, example)}
        assert marked == warned, (name, sorted(marked ^ warned))
    # and no recommendation anywhere is a pair the profiles say does not work
    for entry in SHIPPED["use_cases"]:
        for found in entry["recommendations"]:
            known = mp.support(found["examples"][0], profiles)
            status = next((h["status"] for h in known["harnesses"] if h["agent"] == found["harness"]), known["status"])
            assert status not in ("failing", "known-bad"), (entry["id"], found["label"], found["harness"], status)


def test_each_pair_says_what_is_known_of_it_in_every_other_agent():
    """Follow-up of 0.1.172 (2026-11-20): the web's start asks llm recommend --json without an agent, so the pairs had
    no agent_note to show - its note said only the profile status, never the table's rank of the model in the other
    agent as the app says it. Each pair has "in_agents" now: for every other harness something is known of, the
    table's rank there (measured or from other tasks) or the profiles' status, and the app's words for it."""
    profiles = mp.load()
    servers = [server(models=[model("ornith-1.5-35b-a3b-mlx", 37), model("mid-35b", 37)])]
    found = uc.recommend(TABLE, servers, "untrusted-input", profiles=profiles)
    by_model = {p["model"]: p for p in found["pairs"]}
    mid = by_model["mid-35b"]["in_agents"]
    assert mid["claude"] == {"rank": 3, "measured": False, "status": None, "text": "rank 3, from other tasks"}
    assert "opencode" not in mid   # its own harness
    ornith = by_model["ornith-1.5-35b-a3b-mlx"]["in_agents"]
    known = {h["agent"]: h["status"] for h in mp.support("ornith-1.5-35b-a3b-mlx", profiles)["harnesses"]}
    for agent, status in known.items():
        if agent != "claude":
            assert ornith[agent] == {"rank": None, "measured": None, "status": status, "text": status}
    assert "claude" not in ornith
    # the same words as the app's note for that agent
    noted = uc.recommend(TABLE, servers, "untrusted-input", agent="claude", profiles=profiles)
    assert next(p for p in noted["pairs"] if p["model"] == "mid-35b")["agent_status"] == mid["claude"]["text"]
    # nothing known (no profiles): only the table's ranks
    bare = {p["model"]: p for p in uc.recommend(TABLE, servers, "untrusted-input")["pairs"]}
    assert bare["ornith-1.5-35b-a3b-mlx"]["in_agents"] == {}
    assert bare["mid-35b"]["in_agents"] == {"claude": mid["claude"]}
    assert found["harness_names"]["claude"] == "Claude Code"   # the agents' names, for a page's note
    json.dumps(found)
