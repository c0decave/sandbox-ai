"""The traffic light per local model (0.1.98). User: "anhand unserer Tests und Analysen ... farbliche Ampel zu den
Modellen, die auf einem lokalen LLM-Server sind: wie weit wir sie supporten und erfolgreich in welchem Harness hatten.
Detail-Infos, was funktioniert hat und was nicht, pro Modell, vorausgesetzt wir haben solche Infos."

Per (model, harness) a colour from the profile's status - green verified, yellow partial/smoke-only, red
failing/known-bad, grey not tested - with a symbol (colour-blind safe) and words; per model the harnesses it is green
in (the best colour first); the details per harness: the evidence, what worked and what failed, the recommended
settings, the measured speed, the round and its date, "no data yet" when nothing is known. Everything is read from
model_profiles.json at run time (round 4 adds models: nothing here names a model the table does not).

Here: model_profiles.support() and its texts, the table's optional keys (rounds, speed, round, date), `llm models`'
SUPPORT column and `llm profiles`' light (in-process against an LM Studio-like server, and through the real
./sandbox-ai)."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
QCN = "qwen/qwen3-coder-next"
Q38 = "qwen/qwen3.8-27b"
LAGUNA = "poolside/laguna-s-2.1"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mp = load("sandbox_model_profiles_light", "sandbox/model_profiles.py")
llm = load("sandbox_llm_light", "sandbox/llm.py")
TABLE_PATH = ROOT / "sandbox/model_profiles.json"
TABLE = json.loads(TABLE_PATH.read_text())
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402


def checked(document):
    return mp.check_table(document, lambda key, value: llm.param_value("lmstudio", key, value),
                          lambda key, value: llm.loop_setting(key, value))


# --- the colours ----------------------------------------------------------------------------------------------------

def test_every_status_has_a_colour_a_symbol_and_words():
    assert {status: mp.light(status) for status in (*mp.STATUSES, mp.UNTESTED)} == {
        "verified": "green", "smoke-only": "yellow", "partial": "yellow", "failing": "red", "known-bad": "red",
        "untested": "grey"}
    assert mp.light(None) == mp.light("something round 9 invents") == "grey"
    assert mp.LIGHT_ORDER == ("green", "yellow", "red", "grey")
    symbols = [mp.SYMBOLS[colour] for colour in mp.LIGHT_ORDER]
    assert len(set(symbols)) == 4, "a shape per colour: readable without colours"
    assert all(mp.LIGHT_WORDS[colour] for colour in mp.LIGHT_ORDER)
    assert set(mp.HARNESS_NAMES) == set(mp.HARNESSES)
    assert mp.HARNESS_NAMES["claude"] == "Claude Code" and mp.HARNESS_NAMES["opencode"] == "OpenCode"


# --- per model ------------------------------------------------------------------------------------------------------

def test_a_model_green_in_some_harnesses_yellow_in_others():
    found = mp.support(QCN, TABLE)
    family = next(f for f in TABLE["families"] if f["id"] == "qwen3-coder-next")
    assert found["family"] == "qwen3-coder-next" and found["label"] == family["label"]
    assert found["light"] == "green" and found["symbol"] == mp.SYMBOLS["green"]
    green = [a for a, e in family["harnesses"].items() if e["status"] == "verified"]
    yellow = [a for a, e in family["harnesses"].items() if e["status"] in ("partial", "smoke-only")]
    assert found["by_light"]["green"] == green and found["by_light"]["yellow"] == yellow
    assert found["text"].startswith("green in " + ", ".join(mp.HARNESS_NAMES[a] for a in green) + "; yellow in ")
    assert [h["agent"] for h in found["harnesses"]] == list(family["harnesses"])
    assert set(found["untested"]) == set(mp.HARNESSES) - set(family["harnesses"])
    assert mp.support_cell(found) == "✓ green: " + ", ".join(green[:3]) + (f" +{len(green) - 3}" if green[3:] else "")


def test_the_details_of_one_harness():
    """What worked, what failed, the recommended settings, the speed, the evidence's rounds and date (a derived
    pair's: its block's), its numbers in a few words."""
    claude = next(h for h in mp.support(QCN, TABLE)["harnesses"] if h["agent"] == "claude")
    entry = next(f for f in TABLE["families"] if f["id"] == "qwen3-coder-next")["harnesses"]["claude"]
    assert claude["harness"] == "Claude Code" and claude["status"] == "verified" and claude["light"] == "green"
    assert claude["evidence"] == mp.evidence_text(entry) and claude["runs"] == entry["runs"]
    assert claude["works"] == entry["works"] and claude["fails"] == entry.get("fails", [])
    assert claude["rounds"] == entry["evidence"]["rounds"] == ["phase 2", "round 1", "round 5"]
    assert claude["date"] == TABLE["rounds"]["round 5"]
    assert claude["measured"] == f"{entry['runs']} runs, {len(entry['evidence']['use_cases'])} use cases, " \
                                 "phase 2, rounds 1, 5"
    assert claude["measured_counts"] == {"runs": entry["runs"], "use_cases": len(entry["evidence"]["use_cases"]),
                                         "rounds": "phase 2, rounds 1, 5"}
    profile = mp.match(QCN, "claude", TABLE)
    assert claude["settings"] == mp.settings(profile)
    assert claude["settings_text"] == mp.values_text(mp.settings(profile))
    assert "tokens/s" in claude["speed"]
    lines = mp.support_lines(mp.support(QCN, TABLE))
    assert lines[0].startswith("✓ green in ") and "Claude Code" in lines[0].split(";")[0]   # round 8: more harnesses
    assert any(line.strip().startswith(f"✓ Claude Code: verified ({claude['measured']}, ") for line in lines)
    assert any("recommended: " in line and "temperature 0.7" in line for line in lines)


def test_a_status_without_measured_runs_is_grey():
    """The user, 2026-11-22 ("how you know otherwise what works"): a status entered by hand, without the pair's
    derived evidence block (its runs), gives no light - grey, no data yet, no warning."""
    document = copy.deepcopy(TABLE)
    family = next(f for f in document["families"] if f["id"] == "qwen3-coder-30b")
    family["harnesses"] = {"codex": {"status": "known-bad", "reason": "an API probe", "evidence": "phase 1: probe",
                                     "fails": ["HTTP 500"]},
                           "opencode": {"status": "verified", "evidence": "by hand", "works": ["smoke"]}}
    table = checked(document)
    found = mp.support("qwen3-coder-30b-a3b-instruct-mlx", table)
    assert found["light"] == "grey" and found["harnesses"] == [] and found["text"] == "no data yet"
    assert "codex" in found["untested"] and "opencode" in found["untested"]
    profile = mp.match("qwen3-coder-30b-a3b-instruct-mlx", "codex", table)
    assert profile["status"] == mp.UNTESTED and profile["warnings"] == [] and profile["light"] == "grey"


def test_a_model_measured_in_one_harness_is_grey_in_every_other():
    """Qwen3.8-27B was measured in Claude Code only (round 7, 6 graded runs: partial): yellow there, and every other
    harness grey - no colour taken from the family or from Claude Code (the user, 2026-11-22: "it says opencode
    verifiziert")."""
    found = mp.support(Q38, TABLE)
    assert found["light"] == "yellow" and found["symbol"] == "~" and [h["agent"] for h in found["harnesses"]] == ["claude"]
    assert found["status"] is None and found["reason"] is None
    assert found["text"] == "yellow in Claude Code"
    assert set(found["untested"]) == set(mp.HARNESSES) - {"claude"}
    assert mp.support_cell(found) == "~ yellow: claude"
    assert mp.support_lines(found)[-1].startswith("  ? no data yet in: Codex, OpenCode")
    assert mp.match(Q38, "opencode", TABLE)["light"] == "grey"


def test_a_model_of_no_family_is_grey_with_no_data_yet():
    found = mp.support("llama3:8b", TABLE)
    assert found["family"] is None and found["light"] == "grey" and found["symbol"] == "?"
    assert found["text"] == "no data yet"
    assert found["harnesses"] == [] and found["untested"] == list(mp.HARNESSES)
    assert mp.support_cell(found) == "? no data yet"
    assert mp.support_lines(found) == ["? no data yet"]


def test_red_only_where_tested_red():
    found = mp.support("mistralai/devstral-small-2-2512", TABLE)
    assert found["light"] == "red" and found["by_light"]["red"] == ["claude", "opencode", "codex"]
    assert found["text"] == "red in Claude Code, OpenCode, Codex"
    assert mp.match("mistralai/devstral-small-2-2512", "qwen", TABLE)["light"] == "grey"   # never run there


def derived(last_round, date=None):
    """A pair's evidence block as scripts/profile_status.py writes it (a few numbers, the run ids)."""
    return {"derived": "scripts/profile_status.py", "rule": "test", "runs": 2, "passed": 1, "rounds": [last_round],
            "last_round": last_round, "run_ids": f"{last_round}: TASK__x__brandnew__BLOCK",
            **({"date": date} if date else {})}


def test_the_table_is_read_at_run_time_never_hard_coded():
    """Round 4 adds families and statuses: a new family and a changed status show at once, with the new optional
    keys (speed, round, date) and a round of the table's "rounds"."""
    document = copy.deepcopy(TABLE)
    document["rounds"]["round 4"] = "2026-11-18"
    document["families"].append({
        "id": "brandnew-9b", "label": "Brandnew 9B", "pattern": "brandnew-?9b", "examples": ["acme/brandnew-9b"],
        "all": {"params": {"temperature": 0.5}, "speed": "prefill ~900 tokens/s, 70 tokens/s out"},
        "harnesses": {"pi": {"status": "partial", "evidence": derived("round 4"), "works": ["smoke"]},
                      "goose": {"status": "verified", "evidence": derived("round 4", "2026-11-19"),
                                "speed": "60 tokens/s out in Goose"},
                      # a status by hand, without measured runs: no light (grey)
                      "codex": {"status": "verified", "evidence": "smoke 2/2", "works": ["smoke"]}}})
    next(f for f in document["families"] if f["id"] == "qwen3-coder-next")["harnesses"]["opencode"]["status"] = \
        "failing"
    next(f for f in document["families"] if f["id"] == "qwen3-coder-next")["harnesses"]["opencode"]["reason"] = "x"
    table = checked(document)
    found = mp.support("ACME/Brandnew-9B-MLX", table)
    assert found["light"] == "green" and found["text"] == "green in Goose; yellow in Pi"
    assert "codex" in found["untested"]
    pi, goose = found["harnesses"]
    assert pi["rounds"] == ["round 4"] and pi["date"] == "2026-11-18" and pi["speed"] == found["speed"]
    assert goose["rounds"] == ["round 4"] and goose["date"] == "2026-11-19"
    assert goose["speed"] == "60 tokens/s out in Goose"
    assert "opencode" in mp.support(QCN, table)["by_light"]["red"]


def test_an_older_table_without_the_new_keys_still_works():
    """Backward compatible: a table of before (no rounds, speed, round, date, derived evidence) is valid; the speed
    comes from the bullets that name one; its hand-written statuses give no light."""
    document = copy.deepcopy(TABLE)
    document.pop("rounds")
    for family in document["families"]:
        for entry in [family["all"], *family.get("harnesses", {}).values()]:
            for key in ("speed", "round", "date"):
                entry.pop(key, None)
            if isinstance(entry.get("evidence"), dict):  # derived: a table of before had the hand-written text
                summary = entry.pop("evidence").get("summary")
                if summary:
                    entry["evidence"] = summary
    table = checked(document)
    # its statuses were entered by hand (no derived evidence block, no run ids): valid, but no light - grey in
    # every harness (2026-11-22: a light only from measured runs); the family's settings and speed still apply
    found = mp.support(QCN, table)
    assert found["light"] == "grey" and found["harnesses"] == [] and found["text"] == "no data yet"
    assert "tokens/s" in found["speed"]
    assert mp.match(QCN, "opencode", table)["params"]["temperature"] == 0.7
    assert mp.support(LAGUNA, table)["light"] == "grey"


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(rounds=["round 1"]), "rounds maps a round to its date"),
    (lambda d: d.update(rounds={"round 1": 5}), "rounds maps a round to its date"),
    (lambda d: d.update(rounds={"": "2026-11-16"}), "rounds maps a round to its date"),
    (lambda d: d["families"][0]["all"].update(speed=12), "speed: a line of printable text"),
    (lambda d: d["families"][0]["harnesses"]["opencode"].update(round="x" * 601), "round: a line"),
    (lambda d: d["families"][0]["harnesses"]["opencode"].update(date="a\nb"), "date: a line"),
    (lambda d: d["families"][0]["all"].update(round="round 1"), "an object of"),
])
def test_the_new_keys_are_checked(change, message):
    document = copy.deepcopy(TABLE)
    change(document)
    with pytest.raises(ValueError, match=re.escape(message)):
        checked(document)


def test_the_shipped_table_dates_its_rounds():
    assert set(TABLE["rounds"]) >= {"phase 1", "phase 2", "round 1", "round 2", "round 3"}
    for family in TABLE["families"]:
        for agent, entry in family.get("harnesses", {}).items():
            pair = next(h for h in mp.support(family["examples"][0], TABLE)["harnesses"] if h["agent"] == agent)
            assert pair["rounds"] and pair["date"], (family["id"], agent)


def test_the_profiles_rows_carry_the_light():
    rows = mp.rows(TABLE, QCN)
    assert all(row["light"] == mp.light(row["status"]) and row["symbol"] == mp.SYMBOLS[row["light"]]
               for row in rows)


# --- the CLI --------------------------------------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")


def run(*argv):
    lines = []
    status = llm.main(list(argv), out=lines.append)
    assert status == 0, lines
    return "\n".join(lines)


@pytest.fixture
def studio(isolated):
    server, port = model_mock.start(models=(QCN, Q38, LAGUNA, "meta/llama3-8b"), lmstudio=True)
    run("add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")
    yield port
    server.shutdown()


def test_llm_models_has_the_support_column(studio):
    lines = run("models", "studio", "--sort", "name").splitlines()
    head = lines[0]
    assert "SUPPORT" in head
    column = head.index("SUPPORT")
    cells = {line.split()[1]: line[column:].strip() for line in lines[1:]}
    assert cells[QCN].startswith("✓ green: ") and cells[Q38] == "~ yellow: claude"   # round 7: Claude Code measured
    assert cells["meta/llama3-8b"] == "? no data yet"
    shown = {m["name"]: m["support"] for m in json.loads(run("models", "studio", "--json"))}
    assert shown[QCN]["light"] == "green" and shown[QCN]["family"] == "qwen3-coder-next"
    assert shown[QCN]["harnesses"][0]["works"] and shown[QCN]["harnesses"][0]["settings_text"]
    assert shown["meta/llama3-8b"] == mp.support("meta/llama3-8b", TABLE)


def test_llm_models_without_a_usable_table_shows_no_column(studio, tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    monkeypatch.setenv("SANDBOX_AI_MODEL_PROFILES", str(broken))
    assert "SUPPORT" not in run("models", "studio")
    assert all("support" not in m for m in json.loads(run("models", "studio", "--json")))


def test_llm_profiles_shows_the_light(isolated):
    listing = run("profiles").splitlines()
    assert any(re.match(r"qwen3-coder-next × claude\s+✓ verified", line) for line in listing)
    assert any(re.match(r"qwen3-coder-next × opencode\s+✓ verified", line) for line in listing)   # round 8
    assert any(re.match(r"seed-oss-36b × claude\s+✗ known-bad", line) for line in listing)   # derived: 0 of 4
    assert not any(line.startswith("qwen3-coder-30b × codex") for line in listing)   # phase 1's probe: no pair
    one = run("profiles", QCN).splitlines()
    assert one[0].startswith("Qwen3-Coder-Next 80B: ✓ green in ") and "Claude Code" in one[0].split(";")[0]
    rows = json.loads(run("profiles", QCN, "--json"))
    assert [found["light"] for found in rows["support"]] == ["green"]
    assert {row["agent"]: row["light"] for row in rows["profiles"]}["claude"] == "green"
    assert [found["family"] for found in json.loads(run("profiles", "Nemotron", "--json"))["support"]] == \
        ["nemotron-3-super-mlx", "nemotron-3-super", "nemotron-3.5-super",   # round 4: Nemotron 3.5 Super
         "nemotron-3.5-bolt"]   # round 7: Bolt
    assert json.loads(run("profiles", "llama3:8b", "--json"))["support"] == []
    assert json.loads(run("profiles", "--json"))["support"] is None  # every family: no one model
    assert run("profiles", "llama3:8b").startswith("no profile for llama3:8b")


def test_e2e_llm_models_and_profiles_through_sandbox_ai(tmp_path):
    """The real ./sandbox-ai (tests/live_fakes.py) against the LM Studio-like server."""
    from live_fakes import Host
    server, port = model_mock.start(models=(QCN, "meta/llama3-8b"), lmstudio=True)
    try:
        host = Host(tmp_path / "host")
        env = {"SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"}
        added = host.run("llm", "add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", env=env)
        assert added.returncode == 0, added.stderr
        listed = host.run("llm", "models", "studio", env=env)
        assert listed.returncode == 0, listed.stderr
        assert "SUPPORT" in listed.stdout and "✓ green: " in listed.stdout and "? no data yet" in listed.stdout
        shown = host.run("llm", "models", "studio", "--json", env=env)
        assert {m["name"]: m["support"]["light"] for m in json.loads(shown.stdout)} == \
            {QCN: "green", "meta/llama3-8b": "grey"}
        profiles = host.run("llm", "profiles", QCN, env=env)
        assert profiles.returncode == 0 and profiles.stdout.startswith("Qwen3-Coder-Next 80B: ✓ green in ")
    finally:
        server.shutdown()


def test_test_connection_lists_the_light_too(studio, capsys):
    """wizard models --url (the app's and the web's "Test connection", the setup wizard's list): the same support."""
    wizard = load("sandbox_wizard_light", "sandbox/wizard.py")
    url = f"http://127.0.0.1:{studio}"
    assert wizard.cli(["models", "--kind", "lmstudio", "--url", url, "--json"]) == 0
    shown = {m["name"]: m["support"]["light"] for m in json.loads(capsys.readouterr().out)}
    assert shown == {QCN: "green", Q38: "yellow", LAGUNA: "yellow", "meta/llama3-8b": "grey"}   # rounds 7-8
    assert wizard.cli(["models", "--kind", "lmstudio", "--url", url, "--status", "--json"]) == 0
    assert all(m["support"] for m in json.loads(capsys.readouterr().out)["models"])
    assert wizard.cli(["models", "--kind", "lmstudio", "--url", url]) == 0
    assert "~ yellow: claude" in capsys.readouterr().out   # Qwen3.8-27B: round 7 measured Claude Code
