"""Every harness as Cairn's worker, with its support shown (sandbox/cairn.py, cairn_workers.py, cairn_workers.json):
--cairn-workers takes every harness the launcher knows; Cairn calls each through its claudecode (or codex) driver and
the relay maps it; the start's notes, its dry run, `sandbox-ai cairn workers` and the refusals show each worker's
traffic light (the model lights' vocabulary: green verified, yellow partly working, red does not work, grey not
tested) from a table in which only what was run gives a colour; a red one is refused with its reason and another of
the session's agents offered.

The user (2026-11-22): "why workers only for claude and not all harnesses?" and "shouldnt all harnesses generally be
support? and if not the harnesses supported shown by ampel or desc?" """
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("launcher_for_cairn_worker_tests", "sandbox/agent_sandbox.py")
cairn = load("cairn_worker_tests", "sandbox/cairn.py")
workers = load("cairn_workers_tests", "sandbox/cairn_workers.py")
browser = load("browser_for_cairn_worker_tests", "sandbox/browser.py")
services = load("services_for_cairn_worker_tests", "sandbox/services.py")
relay = load("relay_for_cairn_worker_tests", "sandbox/cairn_relay.py")
cairn._launcher = browser._launcher = services._launcher = s
services._browser, services._cairn = browser, cairn
TABLE = json.loads((ROOT / "sandbox/cairn_workers.json").read_text())


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(s, "load_cairn_module", lambda: cairn)
    monkeypatch.setattr(s, "load_browser_module", lambda: browser)
    monkeypatch.setattr(s, "load_services_module", lambda: services)
    monkeypatch.setattr(s, "on_machine", lambda a: False)
    return tmp_path


def start(*options, agent="claude"):
    a = s.parse_args([agent, *options])
    a.vpn_profile, a.run_id, a.keep_home, a.services = None, f"{agent}-1234abcd", False, []
    return a


def checked(*options, **kwargs):
    a = start(*options, **kwargs)
    s.check_cairn(a)
    return a


def red(monkeypatch, folder, agent, reason="its one-shot run prints no answer"):
    """The table with AGENT marked red (failing), as a live check's report would make it."""
    table = json.loads(json.dumps(TABLE))
    table["harnesses"][agent].update({"status": "failing", "evidence": "live check REPORT-x", "reason": reason})
    path = Path(folder) / "cairn_workers.json"
    path.write_text(json.dumps(table))
    monkeypatch.setattr(cairn.workers(), "TABLE", path)
    return path


# --- every harness a worker -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("agent", ["hermes", "qwen", "dsh", "goose", "nanobot", "letta", "pi", "opencode",
                                   "opencode2"])
def test_every_harness_as_the_lead_is_its_own_worker(agent):
    """"why workers only for claude and not all harnesses?": the lead alone is Cairn's worker, whatever harness."""
    a = checked("--cairn", agent=agent)
    assert a.cairn_workers == [{"agent": agent, "model": None}]
    [entry] = cairn.dispatch(a)["workers"]
    assert entry["type"] == "claudecode", "Cairn keeps calling its claudecode driver; the relay maps it"
    assert entry["env"] == {"SANDBOX_AI_CAIRN_WORKER": agent} and entry["name"] == f"sandbox-{agent}"
    assert list(cairn.relay_config(a, s.launcher_namespace())["workers"]) == [agent]


def test_codex_keeps_its_own_driver_and_claude_its_own():
    a = checked("--cairn", "--with", "codex,goose")
    found = {w["name"]: w for w in cairn.dispatch(a)["workers"]}
    assert found["sandbox-claude"]["type"] == "claudecode" and found["sandbox-codex"]["type"] == "codex"
    assert found["sandbox-goose"]["type"] == "claudecode"
    assert found["sandbox-codex"]["env"] == {"SANDBOX_AI_CAIRN_WORKER": "codex"}


def test_pi_is_a_worker_through_the_fixed_mapping_never_cairn_s_shell_driver():
    a = checked("--cairn", agent="pi")
    assert cairn.dispatch(a)["workers"][0]["type"] == "claudecode"
    assert "pi" not in json.dumps(cairn.dispatch(a)["workers"][0]["type"])
    assert cairn.plan(a, s.launcher_namespace())["workers"][0]["run"].startswith("pi -p")


def test_a_worker_of_the_session_beside_the_lead():
    a = checked("--cairn", "--with", "hermes,qwen", "--cairn-workers", "qwen,claude")
    assert [w["agent"] for w in a.cairn_workers] == ["qwen", "claude"], "the order given: the first takes every task"


def test_by_default_every_agent_of_the_session_the_best_supported_first():
    a = checked("--cairn", "--with", "codex,goose", agent="opencode")
    order = [w["agent"] for w in a.cairn_workers]
    assert set(order) == {"opencode", "codex", "goose"}
    ranks = [workers.rank(agent) for agent in order]
    assert ranks == sorted(ranks), "the best light first (Cairn's first worker takes every kind of task)"
    b = checked("--cairn", "--with", "codex,goose")
    assert [w["agent"] for w in b.cairn_workers][0] == "claude", "yellow before grey"
    assert [w["agent"] for w in b.cairn_workers][1:] == ["codex", "goose"], "else the session's order"


@pytest.mark.parametrize("agent", ["dsh", "goose", "nanobot", "opencode2"])
def test_a_model_for_a_harness_without_a_model_option_is_refused(agent):
    with pytest.raises(ValueError, match=f"{agent}=MODEL: .*no model option"):
        checked("--cairn", "--cairn-workers", f"{agent}=m1", agent=agent)


def test_a_model_for_a_harness_with_one_reaches_its_command_line():
    a = checked("--cairn", "--with", "hermes", "--cairn-workers", "hermes=m1,claude")
    assert cairn.relay_config(a, s.launcher_namespace())["workers"]["hermes"] == ["--model", "m1"]


def test_openhands_as_the_lead_is_still_refused_and_beside_another_it_works():
    with pytest.raises(ValueError, match="port 8000"):
        checked("--cairn", agent="openhands")
    a = checked("--cairn", "--with", "openhands", "--cairn-workers", "openhands")
    assert a.cairn_workers == [{"agent": "openhands", "model": None}]


def test_an_agent_not_in_the_session_is_offered_with():
    with pytest.raises(ValueError, match="add --with goose"):
        checked("--cairn", "--cairn-workers", "goose")


def test_an_unknown_harness_is_refused_with_the_known_ones():
    with pytest.raises(ValueError, match="none of the launcher's harnesses.*claude, codex, opencode"):
        checked("--cairn", "--cairn-workers", "bash")


def test_codex_s_telemetry_stays_off_in_its_worker():
    """Codex's own -c telemetry overrides, as the lead's command line has them (they win over every config.toml):
    a Codex worker run by the relay gets them too."""
    a = checked("--cairn", agent="codex")
    own = cairn.relay_config(a, s.launcher_namespace())["workers"]["codex"]
    expected = s.codex_telemetry_args(s.telemetry_level(a))
    assert expected and own[:len(expected)] == expected
    team = checked("--cairn", "--with", "codex")
    assert cairn.relay_config(team, s.launcher_namespace())["workers"]["codex"][:len(expected)] == expected


# --- the traffic light ----------------------------------------------------------------------------------------------

def test_the_table_has_every_harness_and_a_colour_only_with_evidence():
    table = workers.load()
    assert set(table) == set(relay.HARNESSES) == s.AGENT_PROGRAMS
    for agent, entry in table.items():
        if entry["status"] != "untested":
            assert re.search(r"CI's job \S+|REPORT-\d", entry["evidence"]), (agent, "names its run")
        assert entry.get("tests"), (agent, "what sandbox-ai's own tests prove is said")


def test_the_table_says_nothing_it_did_not_run(tmp_path):
    bad = json.loads(json.dumps(TABLE))
    bad["harnesses"]["goose"]["status"] = "verified"
    (tmp_path / "t.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="goose: verified names no evidence"):
        workers.load(tmp_path / "t.json")
    bad["harnesses"]["goose"].update({"status": "failing", "evidence": "REPORT-1"})
    (tmp_path / "t.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="says no reason"):
        workers.load(tmp_path / "t.json")
    del bad["harnesses"]["goose"]
    (tmp_path / "t.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="one entry for each harness"):
        workers.load(tmp_path / "t.json")


def test_a_row_is_the_model_lights_vocabulary():
    profiles = load("profiles_for_cairn_worker_tests", "sandbox/model_profiles.py")
    for found in workers.rows():
        assert found["light"] == profiles.light(found["status"])
        assert found["symbol"] == profiles.SYMBOLS[found["light"]] and found["words"] == profiles.LIGHT_WORDS[
            found["light"]]
        assert found["name"] == profiles.HARNESS_NAMES[found["agent"]]
        assert found["driver"] == ("codex" if found["agent"] == "codex" else "claudecode")


def test_the_start_s_notes_show_each_worker_s_light():
    a = checked("--cairn", "--with", "codex,goose")
    said = "\n".join(cairn.notes(a))
    assert "~ claude (Claude Code): partly working" in said and "? goose (Goose): no data yet" in said
    assert "? codex (Codex): no data yet" in said, "missing evidence is the neutral grey, never a warning"
    assert "not tested" not in said
    assert not re.search(r"live[- ]tested", said)


def test_the_dry_run_shows_each_worker_s_light_and_command_line():
    a = checked("--cairn", "--with", "goose")
    plan = {w["agent"]: w for w in cairn.plan(a, s.launcher_namespace())["workers"]}
    assert plan["goose"]["light"] == "grey" and plan["goose"]["driver"] == "claudecode"
    assert plan["goose"]["run"] == "goose run --quiet --text=PROMPT" and plan["goose"]["mapped"]
    assert plan["claude"]["light"] == "yellow" and not plan["claude"]["mapped"]


def test_a_red_harness_is_refused_with_its_reason_and_another_offered(monkeypatch, tmp_path):
    red(monkeypatch, tmp_path, "goose")
    with pytest.raises(ValueError) as found:
        checked("--cairn", "--with", "goose", "--cairn-workers", "goose")
    text = str(found.value)
    assert "goose does not work as Cairn's worker: its one-shot run prints no answer" in text
    assert "--cairn-workers claude" in text, "the exact command of another of the session's agents"


def test_by_default_a_red_harness_is_left_out_and_the_start_says_so(monkeypatch, tmp_path):
    red(monkeypatch, tmp_path, "goose")
    a = checked("--cairn", "--with", "goose")
    assert [w["agent"] for w in a.cairn_workers] == ["claude"]
    assert any("goose is not one of Cairn's workers here: its one-shot run prints no answer" in note
               for note in cairn.notes(a))
    with pytest.raises(ValueError, match="goose does not work as Cairn's worker"):
        checked("--cairn", agent="goose")


# --- sandbox-ai cairn workers ---------------------------------------------------------------------------------------

def sai(*args):
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "NO_COLOR": "1"})


def test_the_listing_command_shows_every_harness_with_its_light():
    done = sai("cairn", "workers")
    assert done.returncode == 0, done.stderr
    text = done.stdout
    for agent in relay.HARNESSES:
        assert re.search(rf"^[✓~✗?] {agent} \(", text, re.M), agent
    assert "~ claude (Claude Code): partly working" in text
    assert "scripts/live_check.sh --only cairn-workers-live" in text
    assert not re.search(r"live[- ]tested", text)
    # Journeys round 1: the listing was ~120 lines (each harness's command line, evidence, tests and limits); it is
    # one line a harness now, and one harness's details are a command away.
    assert len([line for line in text.splitlines() if line.strip()]) <= len(relay.HARNESSES) + 3, text
    assert "goose run --quiet --text=PROMPT" not in text and "sandbox-ai cairn workers AGENT" in text
    one = sai("cairn", "workers", "goose").stdout
    assert "goose run --quiet --text=PROMPT" in one and "evidence:" in one and "limit:" in one, one


def test_the_listing_as_json_and_for_some():
    done = sai("cairn", "workers", "dsh", "codex", "--json")
    assert done.returncode == 0, done.stderr
    rows = json.loads(done.stdout)
    assert [r["agent"] for r in rows] == ["dsh", "codex"]
    assert rows[0]["run"].endswith("- < PROMPT") and rows[0]["light"] == "grey" and rows[0]["limits"]
    assert rows[1]["driver"] == "codex"
    wrong = sai("cairn", "workers", "bash")
    assert wrong.returncode == 1 and "no harness of the launcher" in wrong.stderr


# --- the presets ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ident, named", [("cairn-pentest-board", ["codex"]), ("cairn-local-llm", ["claude"])])
def test_the_cairn_presets_show_their_workers_lights(ident, named):
    done = sai("presets", "show", ident)
    assert done.returncode == 0, done.stderr
    [line] = [line for line in done.stdout.splitlines() if line.startswith("  Cairn's workers' support")]
    for agent in named:
        found = workers.row(agent)
        assert f"{found['symbol']} {agent} {found['words']}" in line
    assert "sandbox-ai cairn workers" in line
    assert ("and the lead's" in line) == (ident == "cairn-pentest-board"), "the lead is the agent it is started with"
    preset = json.loads((ROOT / f"sandbox/presets/{ident}.json").read_text())
    assert any("sandbox-ai cairn workers" in note for note in preset["notes"]), "its notes say where the lights are"
