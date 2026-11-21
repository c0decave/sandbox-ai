"""Model and harness per use case at the command line and at a start (user 2026-11-17, "B6"):

- `sandbox-ai llm set NAME --memory GB`: the server's memory, for the fit check (a server on this host's loopback: this
  host's own);
- `sandbox-ai llm recommend [--use-case NAME] [--server NAME] [--agent AGENT] [--host H] [--json]`: from the models the
  servers have now (what is loaded, what fits) the best tested choice per use case, else the nearest, with why;
- `sandbox-ai AGENT --llm SERVER --use-case NAME`: the use case picks the model of SERVER for AGENT and its settings go
  over the model's profile; the model you name (--llm SERVER:MODEL) and your parameters win, --no-llm-profile drops
  the settings (the choice stays).

The servers are tests/devtools/model_mock.py (an LM Studio-like one: sizes, loaded instances, vision); the table is
the tests' own (SANDBOX_AI_MODEL_USECASES), so round 5's ranking can change the shipped one."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
GB = 1000 ** 3
NAME = "studio"
BIG, MID, SMALL, FLINT = "big-122b-mlx", "mid-35b-mlx", "qwen3.6-35b-a3b-mlx", "flint"
QCN = "qwen/qwen3-coder-next"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


llm = load("sandbox_llm_usecases_start", "sandbox/llm.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402

TABLE = {
    "schema": 1, "source": "the tests' own", "rounds": {"round 4": "2026-11-17", "round 5": "2026-11-18"},
    "use_cases": [
        {"id": "coding", "label": "Coding and debugging", "about": "fix a bug", "tasks": ["t2n_debug"],
         "recommendations": [
             {"model": "big-?122b", "examples": [BIG], "label": "Big 122B", "harness": "claude", "basis": "measured",
              "params": {"temperature": 0.6, "num_ctx": 131072}, "why": "solved it 2 of 2",
              "evidence": "round 4: debug 2/2", "round": "round 4", "runs": 2, "solved": 2},
             {"model": "mid-?35b", "examples": [MID], "label": "Mid 35B", "harness": "opencode", "basis": "measured",
              "params": {"temperature": 0.5, "top_k": 30}, "loop_guard": {"steps": 200},
              "why": "solved it 2 of 2 in OpenCode", "fails": ["Qwen Code: changed the tests"],
              "evidence": "round 4: debug 2/2", "round": "round 4", "runs": 2, "solved": 2},
             {"model": "qwen-?3\\.6", "examples": [SMALL], "label": "Qwen3.6", "harness": "opencode",
              "basis": "measured", "params": {"temperature": 0.4}, "why": "4 of 4 on 20 GB",
              "evidence": "round 2: debug 4/4", "round": "round 4", "runs": 4, "solved": 4},
             {"model": "\\bflint\\b", "examples": [FLINT], "label": "Flint", "harness": "codex", "basis": "measured",
              "params": {"max_tokens": 16384}, "why": "the best Codex pairing",
              "evidence": "round 4: Codex 2/2", "round": "round 4", "runs": 2, "solved": 2}]},
        {"id": "code-review", "label": "Code review", "about": "review a branch", "tasks": ["r3_review_py"],
         "unknown": ["reviews: not tested before round 5"], "recommendations": []},
        {"id": "small-jobs", "label": "Small fast jobs", "about": "commit messages", "tasks": ["j8_jobs"],
         "recommendations": [
             {"model": "qwen-?3[^/]*coder[^/]*next", "examples": [QCN], "label": "QCN", "harness": "opencode",
              "basis": "measured", "params": {"temperature": 0.2, "max_tokens": 2048}, "why": "quick answers",
              "evidence": "round 5: j8_jobs 0.9", "round": "round 5", "score": 0.9}]},
    ]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    table = tmp_path / "usecases.json"
    table.write_text(json.dumps(TABLE))
    monkeypatch.setenv("SANDBOX_AI_MODEL_USECASES", str(table))
    monkeypatch.setattr(llm, "host_memory", lambda: 32 * GB)  # this host (a server on its loopback)


def run(*argv, status=0):
    lines = []
    found = llm.main(list(argv), out=lines.append)
    assert found == status, lines
    return "\n".join(lines)


@pytest.fixture
def studio():
    """An LM Studio-like server: Big 70 GB, Mid 37 GB, Qwen3.6 20 GB loaded, Flint 42 GB, QCN 45 GB."""
    server, port = model_mock.start(models=(BIG, MID, SMALL, FLINT, QCN), lmstudio=True,
                                    sizes={BIG: 70 * GB, MID: 37 * GB, SMALL: 20 * GB, FLINT: 42 * GB, QCN: 45 * GB},
                                    loaded={SMALL: 131072}, vision=(SMALL,))
    run("add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}")
    yield port
    server.shutdown()


def start(llm_value=NAME, agent="opencode", **options):
    defaults = {"llm": llm_value, "agent": agent, "env": [], "secret_env": [], "llm_param": [], "vpn": None,
                "no_provider_defaults": True, "llm_models": None, "offline": False, "no_inspect": [],
                "agent_args": [], "local_config": False, "dangerously_skip_permissions": False, "mcp": [],
                "no_llm_profile": False, "use_case": None, "dry_run": False}
    return argparse.Namespace(**{**defaults, **options})


# --- the server's memory ------------------------------------------------------------------------------------------

def test_llm_set_memory_is_stored_shown_and_dropped(studio):
    out = run("set", NAME, "--memory", "64")
    assert "memory: 64 GB" in out
    assert json.loads(run("show", NAME, "--json"))["memory_gb"] == 64
    run("set", NAME, "--memory", "0")
    assert json.loads(run("show", NAME, "--json"))["memory_gb"] is None
    lines = []
    assert llm.main(["set", NAME, "--memory", "-3"], out=lines.append) == 1
    with pytest.raises(ValueError, match="memory_gb"):
        llm.check_record("x", {"kind": "lmstudio", "url": "http://127.0.0.1:1234", "memory_gb": "lots"})


def test_the_memory_of_a_server_set_by_you_else_this_host_s_for_its_loopback(studio):
    found = llm.endpoint(NAME)
    assert llm.memory_of(found) == (32 * GB, "this host")
    run("set", NAME, "--memory", "128")
    assert llm.memory_of(llm.endpoint(NAME)) == (128 * GB, "llm set")
    run("add", "lan", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234")
    assert llm.memory_of(llm.endpoint("lan")) == (None, None)


# --- llm recommend --------------------------------------------------------------------------------------------------

def test_recommend_takes_what_the_server_has_and_what_fits(studio):
    run("set", NAME, "--memory", "64")
    found = json.loads(run("recommend", "--use-case", "coding", "--json"))
    choice = found["choice"]
    # Big (70 GB) is too big for 64 GB; Mid fits (46 of 48 GB) and ranks before Qwen3.6, loaded
    assert (choice["server"], choice["model"], choice["harness"], choice["rank"]) == (NAME, MID, "opencode", 2)
    assert choice["fit"] == "fits" and choice["settings"] == {"temperature": 0.5, "top_k": 30,
                                                              "loop_guard.steps": 200}
    assert [m["label"] for m in found["missing"]] == ["Big 122B"] and "too big" in found["missing"][0]["reason"]
    assert [a["model"] for a in found["alternatives"]] == [SMALL, FLINT]
    assert found["servers"] == [{"name": NAME, "answered": True, "error": None, "memory_bytes": 64 * GB,
                                 "models": 5}]


def test_recommend_in_words_and_for_every_use_case(studio):
    run("set", NAME, "--memory", "128")
    text = run("recommend", "--use-case", "coding")
    assert f"choice: {NAME}:{BIG} in Claude Code (rank 1, measured; fits: about 88 GB of 96 GB after unloading the " \
           "20 GB loaded now)" in text  # Qwen3.6 is loaded
    assert f"start: sandbox-ai claude --llm {NAME}:{BIG} --use-case coding" in text
    table = run("recommend")
    assert table.splitlines()[0].split() == ["USE", "CASE", "STATUS", "CHOICE", "HARNESS", "FIT"]
    assert "code-review" in table and "not tested" in table
    every = json.loads(run("recommend", "--json"))
    assert [entry["use_case"] for entry in every["use_cases"]] == ["coding", "code-review", "small-jobs"]
    assert every["use_cases"][2]["choice"]["model"] == QCN  # round 5's measured choice


def test_recommend_for_the_start_s_agent(studio):
    run("set", NAME, "--memory", "128")
    choice = json.loads(run("recommend", "--use-case", "coding", "--agent", "codex", "--json"))["choice"]
    assert (choice["model"], choice["harness"]) == (FLINT, "codex")


def test_recommend_of_an_unknown_use_case_names_the_known_ones(studio):
    lines = []
    assert llm.main(["recommend", "--use-case", "cooking"], out=lines.append) == 1


def test_a_server_that_does_not_answer_is_said_and_its_remembered_models_count(studio, tmp_path):
    run("models", NAME)  # remembered: the five names
    run("add", NAME, "--kind", "lmstudio", "--url", "http://127.0.0.1:9", "--replace")
    llm.remember_models(NAME, [MID])
    found = json.loads(run("recommend", "--use-case", "coding", "--server", NAME, "--json"))
    assert found["servers"][0]["answered"] is False and found["servers"][0]["error"]
    assert found["choice"]["model"] == MID and found["choice"]["fit"] == "unknown"
    assert "did not answer" in run("recommend", "--use-case", "coding")


def test_recommend_on_another_host_runs_there(monkeypatch):
    asked = []

    class Hosts:
        @staticmethod
        def run(host, argv, timeout=None):
            asked.append((host, argv))
            return 0, "coding  measured  box:m  Claude Code  fits\n", ""
    monkeypatch.setattr(llm, "load_hosts_module", lambda: Hosts)
    assert "box:m" in run("recommend", "--host", "box", "--use-case", "coding")
    assert asked == [("box", ["llm", "recommend", "--use-case", "coding"])]


# --- a start with --use-case ------------------------------------------------------------------------------------

def test_the_use_case_picks_the_model_of_the_server_for_the_agent(studio):
    run("set", NAME, "--memory", "64")
    a = start(use_case="coding")
    found, model = llm.selection(a)
    assert model == MID  # OpenCode: its own harness first
    assert found["llm_profile"] == {"agent": "opencode", "model": MID, "use_case": "coding"}
    report = a._llm_use_case
    assert report["id"] == "coding" and report["picked"] is True and report["choice"]["rank"] == 2
    assert llm.selection(start(agent="codex", use_case="coding"))[1] == MID  # Flint (42 GB) may not load in 64
    run("set", NAME, "--memory", "128")
    assert llm.selection(start(agent="codex", use_case="coding"))[1] == FLINT


def test_the_use_case_s_settings_go_over_the_profile_and_under_yours(studio):
    run("set", NAME, "--memory", "64")
    a = start(use_case="coding")
    found, model = llm.selection(a)
    values = llm.effective_params(found, model)[0]
    assert values["temperature"] == 0.5 and values["top_k"] == 30
    assert llm.loop_config(found)["steps"] == 200
    run("set", NAME, "--param", "temperature=0.3")  # the endpoint's own value wins
    found, model = llm.selection(start(use_case="coding"))
    assert llm.effective_params(found, model)[0]["temperature"] == 0.3
    mine = start(use_case="coding", llm_param=["top_k=5"])  # the start's own wins over both
    found, model = llm.selection(mine)
    override, _ = llm.start_params(mine, found)
    assert llm.effective_params(found, model, override)[0]["top_k"] == 5


def test_the_model_you_name_wins_and_gets_the_use_case_s_settings_only_for_its_own_pair(studio):
    a = start(f"{NAME}:{QCN}", use_case="small-jobs")
    found, model = llm.selection(a)
    assert model == QCN and a._llm_use_case["picked"] is False
    values = llm.effective_params(found, model)[0]
    assert values["temperature"] == 0.2 and values["max_tokens"] == 2048  # over qwen3-coder-next's profile
    assert values["top_p"] == 0.8  # the profile's own stays
    report = llm.profile_report(a, found, model)
    assert report["use_case"]["id"] == "small-jobs" and "temperature" in report["applied"]
    other = start(f"{NAME}:{QCN}", agent="claude", use_case="small-jobs")  # measured in OpenCode only
    found, model = llm.selection(other)
    assert llm.effective_params(found, model)[0]["temperature"] == 0.7  # the profile's: not the use case's
    assert "not measured for this model in Claude Code" in other._llm_use_case["note"]


def test_a_model_of_no_profile_gets_the_use_case_s_settings(studio):
    a = start(f"{NAME}:{MID}", use_case="coding")
    found, model = llm.selection(a)
    assert llm.effective_params(found, model)[0] == {"temperature": 0.5, "top_k": 30}
    report = llm.profile_report(a, found, model)
    assert report["family"] is None and report["use_case"]["id"] == "coding"
    lines = llm.profile_lines(report, NAME)
    assert any("use case coding" in line and "temperature 0.5" in line for line in lines)


def test_no_llm_profile_drops_the_settings_the_choice_stays(studio):
    run("set", NAME, "--memory", "64")
    a = start(use_case="coding", no_llm_profile=True)
    found, model = llm.selection(a)
    assert model == MID and "llm_profile" not in found
    assert llm.effective_params(found, model)[0] == {}


def test_nothing_of_the_table_on_the_server_the_nearest_or_the_server_s_default(studio, monkeypatch):
    a = start(use_case="code-review")  # not tested: the nearest by the traffic light, what fits this host's 32 GB
    found, model = llm.selection(a)
    assert model == SMALL and a._llm_use_case["choice"]["source"] == "traffic light"  # QCN: too big here
    monkeypatch.setattr(llm, "profile_table", lambda: None)
    run("add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{studio}", "--model", SMALL, "--replace")
    b = start(use_case="code-review")
    assert llm.selection(b)[1] == SMALL  # nothing to go by: the server's default model, said
    assert "the server's default model" in b._llm_use_case["note"]


def test_an_unknown_use_case_is_refused_with_the_known_ones(studio):
    with pytest.raises(ValueError, match="no use case 'cooking'.*coding, code-review, small-jobs"):
        llm.selection(start(use_case="cooking"))
    with pytest.raises(ValueError, match="no use case"):
        llm.selection(start(f"{NAME}:{MID}", use_case="cooking"))


def test_a_session_keeps_the_model_the_use_case_picked(studio):
    """A session keeps the options it was started with: the model picked is named in them, so the supervisor's run,
    a restart and a resume work with the same model whatever the server lists by then."""
    run("set", NAME, "--memory", "64")
    a = start(use_case="coding")
    llm.selection(a)
    argv = ["--llm", NAME, "--use-case", "coding", "-w", "/w", "--", "--llm", NAME]
    assert llm.pinned_argv(a, argv) == ["--llm", f"{NAME}:{MID}", "--use-case", "coding", "-w", "/w", "--", "--llm",
                                        NAME]
    assert llm.pinned_argv(a, [f"-lm={NAME}", "-uc", "coding"]) == [f"-lm={NAME}:{MID}", "-uc", "coding"]
    named = start(f"{NAME}:{QCN}", use_case="coding")
    llm.selection(named)
    assert llm.pinned_argv(named, ["--llm", f"{NAME}:{QCN}"]) == ["--llm", f"{NAME}:{QCN}"]  # yours: as it is
    assert llm.pinned_argv(start(), ["--llm", NAME]) == ["--llm", NAME]  # no use case: as it is


def test_the_dry_run_plan_says_the_use_case(studio):
    run("models", NAME)  # the dry run asks the server nothing: the remembered names
    a = start(use_case="coding", dry_run=True)
    plan = llm.plan(a)
    assert plan["model"] == MID  # sizes not known without asking: OpenCode's first
    assert plan["use_case"]["id"] == "coding" and plan["use_case"]["choice"]["fit"] == "unknown"
    assert plan["profile"]["use_case"]["id"] == "coding"


# --- end to end: the real ./sandbox-ai against the LM Studio-like server ---------------------------------------------

def test_e2e_recommend_and_a_start_with_a_use_case(tmp_path):
    from live_fakes import Host
    server, port = model_mock.start(models=(BIG, MID, SMALL), lmstudio=True,
                                    sizes={BIG: 70 * GB, MID: 37 * GB, SMALL: 20 * GB}, loaded={SMALL: 131072})
    try:
        host = Host(tmp_path / "host")
        host.add_image("localhost/opencode-sandbox:latest", "localhost/claude-sandbox:latest")
        env = {"SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_MODEL_USECASES": str(tmp_path / "usecases.json")}
        (tmp_path / "usecases.json").write_text(json.dumps(TABLE))
        added = host.run("llm", "add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", env=env)
        assert added.returncode == 0, added.stderr
        assert host.run("llm", "set", NAME, "--memory", "64", env=env).returncode == 0
        found = host.run("llm", "recommend", "--use-case", "coding", "--json", env=env)
        assert found.returncode == 0, found.stderr
        choice = json.loads(found.stdout)["choice"]
        assert (choice["model"], choice["harness"], choice["fit"]) == (MID, "opencode", "fits")
        words = host.run("llm", "recommend", env=env)
        assert words.returncode == 0 and "code-review" in words.stdout, words.stderr

        assert host.run("llm", "models", NAME, env=env).returncode == 0  # remembered for the dry run
        dry = host.run("opencode", "--llm", NAME, "--use-case", "coding", "--dry-run", env=env)
        assert dry.returncode == 0, dry.stderr
        plan = json.loads(dry.stdout)["llm"]
        assert plan["use_case"]["id"] == "coding" and plan["model"] == MID
        assert "use case coding" in dry.stderr
        named = host.run("opencode", "--llm", f"{NAME}:{MID}", "--use-case", "coding", "--llm-param",
                         "temperature=0.1", "--dry-run", env=env)
        assert named.returncode == 0, named.stderr
        plan = json.loads(named.stdout)["llm"]
        assert plan["model"] == MID and plan["profile"]["overridden"] == ["temperature"]
        assert plan["profile"]["values"]["top_k"] == 30
        short = host.run("opencode", "--llm", f"{NAME}:{MID}", "-uc", "coding", "--dry-run", env=env)
        assert json.loads(short.stdout)["llm"]["use_case"]["id"] == "coding", short.stderr
        # a preset of yours may carry the use case among its options
        presets = Path(host.env["XDG_CONFIG_HOME"]) / "sandbox-ai" / "presets"
        presets.mkdir(parents=True, exist_ok=True)
        (presets / "local-coding.json").write_text(json.dumps({
            "format": "sandbox-ai preset", "version": 1, "id": "local-coding", "title": "Coding on a local model",
            "description": "the model measured best for coding", "category": "llm", "agents": ["opencode"],
            "options": ["--llm", "{llm}", "--use-case", "coding", "--no-provider-defaults"],
            "values": {"llm": {"label": "LLM server", "kind": "llm"}}}))
        preset = host.run("opencode", "--preset", "local-coding", "--preset-value", f"llm={NAME}", "--dry-run",
                          env=env)
        assert preset.returncode == 0, preset.stderr
        assert json.loads(preset.stdout)["llm"]["use_case"]["id"] == "coding"
        alone = host.run("opencode", "--use-case", "coding", "--dry-run", env=env)
        assert alone.returncode != 0 and "--use-case" in alone.stderr and "--llm" in alone.stderr
        unknown = host.run("opencode", "--llm", NAME, "--use-case", "cooking", "--dry-run", env=env)
        assert unknown.returncode != 0 and "no use case 'cooking'" in unknown.stderr
    finally:
        server.shutdown()


def test_recommend_asks_its_servers_at_once_not_one_after_the_other(monkeypatch):
    """Review 3x-2: servers that do not answer (a laptop's LM Studio closed, an Ollama box off) each cost their time
    limit; asked one after the other three of them made `llm recommend` (and the web interface's use cases) wait half
    a minute. Asked at once, it waits for the slowest; the order of the servers stays."""
    import threading
    import time as clock
    for name in ("a1", "b2", "c3"):
        run("add", name, "--kind", "ollama", "--url", f"http://127.0.0.1:{9 + len(name)}")
    at_once, lock = [0, 0], threading.Lock()

    def slow(endpoint, get=None, timeout=None):
        with lock:
            at_once[0] += 1
            at_once[1] = max(at_once[1], at_once[0])
        clock.sleep(0.5)
        with lock:
            at_once[0] -= 1
        return [], False, f"{endpoint['name']}: timed out"
    monkeypatch.setattr(llm, "server_listing", slow)
    begun = clock.monotonic()
    found = json.loads(run("recommend", "--use-case", "coding", "--json"))
    assert clock.monotonic() - begun < 1.2 and at_once[1] == 3
    assert [server["name"] for server in found["servers"]] == ["a1", "b2", "c3"]
