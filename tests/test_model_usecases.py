"""Model and harness per use case (user 2026-11-17, "B6"): sandbox/model_usecases.json keeps, per use case of the
local-model research (round 5's eleven and the rounds' coding task), the ranked recommendations - model pattern,
harness, settings over the model's profile, a one-line why, known failures, evidence with round and date - only from
evidence, "not tested" said as such. model_usecases.recommend() picks from the models the user's servers actually have
(what is loaded, what fits the server's memory) the best tested choice, else the nearest, with why.

The table's schema is checked whole (llm's own rules for parameters and loop-guard layers); the algorithm is tested on
tables of the tests' own (round 5 changes the shipped ranking) and the shipped table on its invariants."""
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


uc = load("sandbox_model_usecases_test", "sandbox/model_usecases.py")
mp = load("sandbox_model_profiles_usecases_test", "sandbox/model_profiles.py")
llm = load("sandbox_llm_usecases_test", "sandbox/llm.py")
GB = 1000 ** 3
SHIPPED = json.loads((ROOT / "sandbox/model_usecases.json").read_text())
# Round 5's use cases (research-llm/round5_brief.md) and the rounds' own coding task.
ROUND5 = {"sandbox", "web-research", "code-review", "security", "large-codebase", "from-spec", "ops", "small-jobs",
          "vision", "teams", "recovery"}


def check(document):
    return uc.check_table(document, llm.profile_param, llm.loop_setting)


def rec(model, example, harness, basis="measured", **more):
    entry = {"model": model, "examples": [example], "label": example, "harness": harness, "basis": basis,
             "why": f"{example} in {harness}", "evidence": f"round 4: {example} solved it", "round": "round 4"}
    entry.update(more)
    return entry


def table(*cases):
    return {"schema": 1, "source": "test", "rounds": {"round 4": "2026-11-17"}, "use_cases": list(cases)}


def case(name, recs=(), **more):
    entry = {"id": name, "label": name.title(), "about": f"the {name} use case", "tasks": [f"{name}_task"],
             "recommendations": list(recs)}
    entry.update(more)
    return entry


BIG = rec("big-?122b", "big-122b", "claude", params={"temperature": 0.6, "num_ctx": 131072})
MID = rec("mid-?35b", "mid-35b", "claude", params={"temperature": 0.6})
SMALL = rec("small-?20b", "small-20b", "opencode")
CODEX = rec("flint", "flint", "codex", params={"max_tokens": 16384})
CODING = case("coding", [BIG, MID, SMALL, CODEX])
REVIEW = case("code-review", [], unknown=["reviews: not tested before round 5"])
LARGE = case("large-codebase", [], related=["coding"])
SEEING = case("vision", [rec("eyes-?31b", "eyes-31b", "claude", basis="indirect")], needs=["vision"])
TABLE = table(CODING, REVIEW, LARGE, SEEING)


def server(name="studio", models=(), memory=64 * GB, **more):
    listed = []
    for model in models:
        listed.append(model if isinstance(model, dict) else {"name": model})
    return {"name": name, "memory_bytes": memory, "models": listed, "answered": True, **more}


def model(name, size_gb=None, loaded=False, vision=False):
    entry = {"name": name, "size": int(size_gb * GB) if size_gb else None, "loaded": loaded}
    if vision:
        entry["capabilities"] = ["vision"]
    return entry


# --- the table: its schema ------------------------------------------------------------------------------------------

def test_the_shipped_table_is_valid_and_names_every_use_case_of_round_5():
    document = uc.load(None, llm.profile_param, llm.loop_setting)
    ids = [entry["id"] for entry in document["use_cases"]]
    assert ROUND5 <= set(ids) and "coding" in ids and len(ids) == len(set(ids))
    assert uc.names(document) == ids


def test_the_shipped_table_is_honest_about_what_was_not_tested():
    """Only evidence: a recommendation has its evidence and round (a measured one its runs), every round it names is
    dated; a use case without one is "not tested" and says what is not known; round 5's use cases are measured, its
    own pairs first."""
    rounds = SHIPPED["rounds"]
    for entry in SHIPPED["use_cases"]:
        assert entry["tasks"], entry["id"]
        status = uc.status(entry)
        if not entry["recommendations"]:
            assert status == "not tested" and entry.get("unknown"), entry["id"]
        for found in entry["recommendations"]:
            assert found["evidence"] and found["round"] in rounds, (entry["id"], found["label"])
            assert re.match(r"(phase|round) \d", found["evidence"]), found["evidence"]
            if found["basis"] == "measured":
                assert isinstance(found.get("runs"), int), (entry["id"], found["label"])
        if entry["id"] in ROUND5:   # round 5 measured each of them with its own tasks: its pairs come first
            assert status == "measured", entry["id"]
            assert entry["recommendations"][0]["round"] == "round 5", entry["id"]


def test_every_recommendation_s_examples_match_its_pattern_and_its_settings_are_taken():
    for entry in SHIPPED["use_cases"]:
        for found in entry["recommendations"]:
            assert all(re.search(found["model"], example, re.I) for example in found["examples"])
            assert found["harness"] in mp.HARNESSES
            for key, value in (found.get("params") or {}).items():
                assert llm.profile_param(key, value) == value


@pytest.mark.parametrize("change, fault", [
    (lambda d: d.update(schema=2), "schema 1"),
    (lambda d: d.update(extra=1), "schema 1"),
    (lambda d: d["use_cases"].append(copy.deepcopy(d["use_cases"][0])), "a new short name"),
    (lambda d: d["use_cases"][0].update(id="Coding!"), "a new short name"),
    (lambda d: d["use_cases"][0].update(colour="red"), "an object of"),
    (lambda d: d["use_cases"][0].update(needs=["telepathy"]), "needs"),
    (lambda d: d["use_cases"][2].update(related=["nothing-such"]), "related"),
    (lambda d: d["use_cases"][0].update(fails=["x" * 121]), "at most 120"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(harness="emacs"), "not a harness"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(model="big-(122b"), "no regular expression"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(examples=["other-7b"]), "does not match"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(basis="hearsay"), "basis"),
    (lambda d: d["use_cases"][0]["recommendations"][0].pop("evidence"), "evidence"),
    (lambda d: d["use_cases"][0]["recommendations"][0].pop("why"), "why"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(why="x" * 200), "at most 120"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(round="round 9"), "rounds"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(params={"temperature": 9.0}), "params temperature"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(params={"nonsense": 1}), "params nonsense"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(loop_guard={"enabled": False}), "loop_guard"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(env={"PATH": "/x"}), "env"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(score=1.5), "score"),
    (lambda d: d["use_cases"][0]["recommendations"][0].update(solved=3, runs=2), "solved"),
    (lambda d: d["use_cases"][0]["recommendations"].append(copy.deepcopy(BIG)), "twice"),
])
def test_a_fault_in_the_table_is_named(change, fault):
    document = copy.deepcopy(TABLE)
    check(document)
    change(document)
    with pytest.raises(ValueError, match=re.escape(fault)):
        check(document)


def test_the_table_is_read_again_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "usecases.json"
    path.write_text(json.dumps(TABLE))
    assert uc.names(uc.load(path)) == ["coding", "code-review", "large-codebase", "vision"]
    path.write_text(json.dumps(table(CODING)) + " ")
    assert uc.names(uc.load(path)) == ["coding"]
    monkeypatch.setenv("SANDBOX_AI_MODEL_USECASES", str(path))
    assert uc.names(uc.load()) == ["coding"]
    path.write_text("{")
    with pytest.raises(ValueError, match="not JSON"):
        uc.load(path)


def test_a_use_case_is_found_by_its_id_or_label_and_an_unknown_one_names_the_known():
    assert uc.use_case(TABLE, "coding")["id"] == "coding"
    assert uc.use_case(TABLE, "Code-Review")["id"] == "code-review"
    with pytest.raises(ValueError, match="no use case 'cooking'.*coding, code-review, large-codebase, vision"):
        uc.use_case(TABLE, "cooking")


# --- what fits: the size of the model against the server's memory ---------------------------------------------------

@pytest.mark.parametrize("size, loaded, memory, others, state", [
    (70, True, 64, 0, "loaded"),          # loaded now: it fits, whatever the numbers say
    (37, False, 64, 0, "fits"),           # 37 GB x 1.25 = 46 GB <= 48 GB (three quarters of 64)
    (37, False, 64, 20, "fits"),          # only after the 20 GB loaded now are unloaded: said so
    (45, False, 64, 0, "tight"),          # 56 GB: more than three quarters, less than all of it
    (70, False, 64, 0, "too big"),        # 87.5 GB > 64 GB
    (None, False, 64, 0, "unknown"),      # the server does not say the size
    (37, False, None, 0, "unknown"),      # nobody said the server's memory
])
def test_the_fit_of_a_model(size, loaded, memory, others, state):
    listed = [model("candidate", size, loaded)]
    if others:
        listed.append(model("other", others, True))
    found = uc.fit(listed[0], server(models=listed, memory=memory * GB if memory else None))
    assert found["state"] == state
    if state == "fits" and others:
        assert "after unloading" in found["text"]
    if memory is None:
        assert "sandbox-ai llm set studio --memory GB" in found["text"]
    if state == "too big":
        assert "88 GB" in found["text"] and "64 GB" in found["text"]


# --- the recommendation from the models the servers have -------------------------------------------------------------

def test_the_best_tested_choice_that_fits_wins():
    servers = [server(models=[model("big-122b", 70), model("mid-35b", 37), model("small-20b", 20, loaded=True)])]
    found = uc.recommend(TABLE, servers, "coding")
    choice = found["choice"]
    assert (choice["server"], choice["model"], choice["harness"], choice["rank"]) == ("studio", "mid-35b", "claude", 2)
    assert choice["source"] == "table" and choice["basis"] == "measured" and choice["fit"] == "fits"
    assert choice["settings"] == {"temperature": 0.6}
    assert choice["why"] == "mid-35b in claude" and choice["evidence"].startswith("round 4")
    assert choice["date"] == "2026-11-17"  # the table's date of its round
    assert choice["command"] == "sandbox-ai claude --llm studio:mid-35b --use-case coding"
    missing = {entry["label"]: entry["reason"] for entry in found["missing"]}
    assert "too big" in missing["big-122b"]
    assert [a["model"] for a in found["alternatives"]] == ["small-20b"]
    assert found["status"] == "measured"


def test_a_loaded_model_of_the_same_rank_on_another_server_is_preferred():
    servers = [server("cold", [model("mid-35b", 37)]), server("warm", [model("mid-35b", 37, loaded=True)])]
    assert uc.recommend(TABLE, servers, "coding")["choice"]["server"] == "warm"


def test_a_model_whose_fit_is_tight_comes_after_every_one_that_fits():
    servers = [server(models=[model("mid-35b", 45), model("small-20b", 20)])]
    choice = uc.recommend(TABLE, servers, "coding")["choice"]
    assert choice["model"] == "small-20b"
    servers = [server(models=[model("mid-35b", 45)])]
    choice = uc.recommend(TABLE, servers, "coding")["choice"]
    assert choice["model"] == "mid-35b" and choice["fit"] == "tight" and "may not load" in choice["fit_text"]


def test_unknown_memory_does_not_hide_a_model_and_says_how_to_tell_it():
    servers = [server(models=[model("big-122b", 70)], memory=None)]
    choice = uc.recommend(TABLE, servers, "coding")["choice"]
    assert choice["model"] == "big-122b" and choice["fit"] == "unknown"


def test_the_start_s_agent_takes_its_own_harness_first_else_says_where_it_was_measured():
    servers = [server(models=[model("mid-35b", 37), model("flint", 42)], memory=128 * GB)]
    choice = uc.recommend(TABLE, servers, "coding", agent="codex")["choice"]
    assert (choice["model"], choice["harness"]) == ("flint", "codex")
    servers = [server(models=[model("mid-35b", 37)])]
    choice = uc.recommend(TABLE, servers, "coding", agent="goose")["choice"]
    assert choice["model"] == "mid-35b" and choice["harness"] == "claude"
    assert "measured in Claude Code, not in Goose" in choice["note"]
    assert choice["settings"] == {}  # the use case's settings are the pair's: not for another harness


def test_nothing_of_the_table_on_the_servers_the_nearest_with_why():
    profiles = mp.load()
    servers = [server(models=[model("qwen/qwen3-coder-next", 45), model("google/gemma-4-31b-qat", 19)],
                      memory=128 * GB)]
    found = uc.recommend(TABLE, servers, "coding", profiles=profiles)
    choice = found["choice"]
    assert choice["source"] == "traffic light" and choice["model"] == "qwen/qwen3-coder-next"
    assert "not in this use case's table" in choice["note"] and choice["rank"] is None
    assert [entry["label"] for entry in found["missing"]] == ["big-122b", "mid-35b", "small-20b", "flint"]
    assert all(entry["reason"] == "not on your servers" for entry in found["missing"])


def test_a_use_case_without_tests_takes_its_related_one_and_says_so():
    servers = [server(models=[model("mid-35b", 37)])]
    found = uc.recommend(TABLE, servers, "large-codebase")
    assert found["status"] == "not tested"
    choice = found["choice"]
    assert choice["source"] == "related" and choice["related"] == "coding" and choice["model"] == "mid-35b"
    assert "not tested for Large-Codebase" in choice["note"] and "Coding" in choice["note"]


def test_a_use_case_without_anything_says_not_tested_and_offers_the_traffic_light_only_as_such():
    profiles = mp.load()
    servers = [server(models=[model("qwen3.6-35b-a3b-mlx", 20)])]
    found = uc.recommend(TABLE, servers, "code-review", profiles=profiles)
    assert found["status"] == "not tested" and found["unknown"] == ["reviews: not tested before round 5"]
    assert found["choice"]["source"] == "traffic light"
    assert "not tested" in found["choice"]["note"]
    assert uc.recommend(TABLE, servers, "code-review")["choice"] is None  # without the profiles: nothing to go by


def test_a_use_case_that_needs_images_takes_only_a_model_that_reads_them():
    profiles = mp.load()
    servers = [server(models=[model("eyes-31b", 19)])]  # the server does not say: the table's evidence does
    choice = uc.recommend(TABLE, servers, "vision", profiles=profiles)["choice"]
    assert choice["model"] == "eyes-31b" and "does not say whether it reads images" in choice["note"]
    servers = [server(models=[model("qwen/qwen3-coder-next", 45)])]  # nearest: only one the server says reads images
    assert uc.recommend(TABLE, servers, "vision", profiles=profiles)["choice"] is None
    servers = [server(models=[model("qwen/qwen3-coder-next", 45), model("google/gemma-4-31b-qat", 19, vision=True)])]
    assert uc.recommend(TABLE, servers, "vision", profiles=profiles)["choice"]["model"] == "google/gemma-4-31b-qat"


def test_a_server_that_did_not_answer_is_named_and_its_remembered_models_still_count():
    servers = [server("down", ["mid-35b"], answered=False, error="connection refused")]
    found = uc.recommend(TABLE, servers, "coding")
    assert found["choice"]["model"] == "mid-35b" and found["choice"]["fit"] == "unknown"
    assert found["servers"] == [{"name": "down", "answered": False, "error": "connection refused",
                                 "memory_bytes": 64 * GB, "models": 1}]


def test_every_use_case_at_once():
    servers = [server(models=[model("mid-35b", 37)])]
    found = uc.recommend_all(TABLE, servers)
    assert [entry["use_case"] for entry in found] == ["coding", "code-review", "large-codebase", "vision"]
    assert found[0]["choice"]["model"] == "mid-35b" and found[1]["choice"] is None


def test_the_settings_of_a_use_case_for_a_model_in_a_harness():
    assert uc.settings_for(TABLE, "coding", "lmstudio-community/big-122b-mlx", "claude")["label"] == "big-122b"
    assert uc.settings_for(TABLE, "coding", "big-122b", "opencode") is None   # another harness: not this pair
    assert uc.settings_for(TABLE, "coding", "unknown-7b", "claude") is None


def test_the_overlay_on_a_profile_and_on_a_model_of_no_family():
    profiles = mp.load()
    profile = mp.match("qwen/qwen3-coder-next", "claude", profiles)
    found = rec("qwen-?3[^/]*coder[^/]*next", "qwen/qwen3-coder-next", "claude", params={"temperature": 0.2},
                loop_guard={"steps": 300})
    merged = uc.overlay(profile, found, "small-jobs", "Small jobs")
    assert merged["params"]["temperature"] == 0.2 and merged["params"]["top_p"] == profile["params"]["top_p"]
    assert merged["loop_guard"]["steps"] == 300 and merged["use_case"]["id"] == "small-jobs"
    assert profile["params"]["temperature"] == 0.7  # the profile itself unchanged
    bare = uc.overlay(None, MID, "coding", "Coding", agent="claude")
    assert bare["params"] == {"temperature": 0.6} and bare["status"] == "untested" and bare["agent"] == "claude"
    assert bare["name"] == "use case coding × claude" and bare["family"] is None
    assert bare["use_case"] == {"id": "coding", "label": "Coding", "rank": None, "basis": "measured",
                                "evidence": "round 4: mid-35b solved it", "why": "mid-35b in claude"}


# --- in words -----------------------------------------------------------------------------------------------------

def test_the_lines_of_one_use_case_and_of_all():
    servers = [server(models=[model("mid-35b", 37), model("big-122b", 70)])]
    text = "\n".join(uc.lines(uc.recommend(TABLE, servers, "coding")))
    assert "coding: Coding (measured on this use case's tasks)" in text
    assert "choice: studio:mid-35b in Claude Code (rank 2" in text
    assert "settings: temperature 0.6" in text
    assert "start: sandbox-ai claude --llm studio:mid-35b --use-case coding" in text
    assert "not available: 1. big-122b in Claude Code: too big" in text
    rows = uc.table_lines(uc.recommend_all(TABLE, servers))
    assert rows[0].split() == ["USE", "CASE", "STATUS", "CHOICE", "HARNESS", "FIT"]
    assert any(line.startswith("code-review") and "not tested" in line for line in rows)


def test_the_choice_of_a_related_use_case_shows_no_settings_the_start_would_not_apply():
    """Review 3x-3: a use case without tests takes its related one's choice - with that one's settings, shown by
    `llm recommend` and the Board ("settings: temperature=0.6") - but the start with --use-case of this use case
    applies only this use case's settings (settings_for: none for that model): what was shown did not hold."""
    servers = [server(models=[model("mid-35b", 37)])]
    choice = uc.recommend(TABLE, servers, "large-codebase")["choice"]
    assert choice["source"] == "related" and choice["model"] == "mid-35b"
    assert uc.settings_for(TABLE, "large-codebase", "mid-35b", "claude") is None  # what the start applies
    assert choice["settings"] == {} and choice["settings_text"] == ""
    assert "--use-case coding" in choice["note"]  # where its settings are
    assert uc.recommend(TABLE, servers, "coding")["choice"]["settings"] == {"temperature": 0.6}
