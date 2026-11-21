"""MCP tools against the model's class (round 5 of the local-model research): a warning, never a refusal.

User 2026-11-19: Gemma-4-12B/26B got worse with all four memory aids at once (resume 1.0 -> 0.67, jobs 0.9 -> 0.74;
research-llm/round5.md "Memory A/B", LOCAL_LLM_REPORT.md section 6). When a start uses an --llm model and the MCP tools
it would see exceed the budget of that model's class, the launcher says so (the count, the budget, which servers bring
the most), the Board (a finding with the servers to drop) and the Start page too. Budgets per class (small, mid,
large) in sandbox/model_profiles.json; a model of no family by its parameter count when its name has one, else no
warning. The count: each server's pinned tools/list, else the catalog's tool list (sandbox/mcp_catalog_tools.json),
else unknown ("about").

Here: the table's budgets and classes, the count, the report and its text, and the real ./sandbox-ai's dry run and
start (tests/live_fakes.py: only Podman stood in for)."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mp = load("sandbox_model_profiles_budget", "sandbox/model_profiles.py")
mb = load("sandbox_mcp_budget_test", "sandbox/mcp_budget.py")
pins = load("sandbox_mcp_pins_budget", "sandbox/mcp_pins.py")
TABLE = json.loads((ROOT / "sandbox/model_profiles.json").read_text())
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402

GEMMA12 = "gemma-4-12b-it"
GEMMA26 = "google/gemma-4-26b-a4b-qat"
ORN35 = "ornith-1.5-35b-a3b-mlx"
IMAGE = "ghcr.io/c0decave/sandbox-ai-mcp-{}@sha256:" + "a" * 64
AIDS = {"memory": "mcp:memory", "tasks": "mcp:tasks", "seqthink": "mcp:sequential-thinking",
        "codesearch": "mcp:code-search"}


def catalog_server(source):
    return {"image": IMAGE.format(source.split(":")[1]), "network": "none", "source": source}


# --- the budgets in the profiles ------------------------------------------------------------------------------------

def test_the_shipped_budgets_come_from_round_5():
    budgets = TABLE["tool_budgets"]
    assert list(budgets) == ["small", "mid", "large"] == list(mp.TOOL_CLASSES)
    assert (budgets["small"]["tools"], budgets["mid"]["tools"], budgets["large"]["tools"]) == (10, 35, 50)
    assert (budgets["small"]["params_below"], budgets["mid"]["params_below"]) == (30, 100)
    assert "params_below" not in budgets["large"]
    for entry in budgets.values():
        assert entry["evidence"].startswith("round 5: ")
    assert "Gemma 4 12B and 26B-A4B" in budgets["small"]["evidence"] and "0.67" in budgets["small"]["evidence"]
    assert "Ornith-1.5-35B" in budgets["mid"]["evidence"]
    assert "Ornith-1.5-122B" in budgets["large"]["evidence"]


@pytest.mark.parametrize("model, cls, tools, source", [
    (GEMMA12, "small", 10, "name"),            # Gemma 4: one family of every size - by the name's 12B
    (GEMMA26, "small", 10, "name"),            # 26B (its 4B active parameters are not the count)
    ("google/gemma-4-31b-qat", "mid", 35, "name"),
    (ORN35, "mid", 35, "family"),
    ("ornith-1.5-122b-mlx", "large", 50, "family"),
    ("qwen/qwen3.8-flash-next", "large", 50, "family"),  # no size in its name: the family's class
    ("qwen/qwen3-coder-next", "mid", 35, "family"),
    ("devstral-small-2-24b-instruct-2512", "small", 10, "family"),
    ("llama3.1:8b", "small", 10, "name"),     # a model of no family: by the parameter count in its name
    ("mixtral-8x7b-instruct", "mid", 35, "name"),
    ("qwen2.5-coder-1.5b-instruct", "small", 10, "name"),
    ("some-model-235b-a22b", "large", 50, "name"),
])
def test_the_class_of_a_model(model, cls, tools, source):
    budget = mp.tool_budget(model, TABLE)
    assert (budget["class"], budget["tools"], budget["from"]) == (cls, tools, source)
    assert budget["evidence"] == TABLE["tool_budgets"][cls]["evidence"]


@pytest.mark.parametrize("model", ["gpt-5", "anthropic/claude-sonnet-4.5", "poolside/laguna-s-2.1",
                                   "zai-org/glm-4.7-flash", "phi-3-mini-128k", "gemma-3n-e4b", "", None])
def test_no_class_no_budget(model):
    """A family without a class whose name has no size (Laguna, GLM-4.7-Flash), a cloud model: no warning ever."""
    assert mp.tool_budget(model, TABLE) is None


@pytest.mark.parametrize("name, billions", [("gemma-4-12b-it", 12), ("x-26b-a4b", 26), ("x-8x7b", 56),
                                            ("q-1.5b", 1.5), ("tiny-350m", 0.35), ("llama3.1:8b", 8),
                                            ("ornith-1.5-35b-a3b", 35), ("Qwen3-235B-A22B", 235), ("v3", None),
                                            ("phi-3-mini-128k", None), ("e4b", None)])
def test_the_parameter_count_in_a_name(name, billions):
    assert mp.params_of(name) == billions


def test_a_table_without_budgets_is_valid_and_gives_none():
    old = copy.deepcopy(TABLE)
    old.pop("tool_budgets")
    for family in old["families"]:
        family["all"].pop("tool_class", None)
    assert mp.check_table(old) == old
    assert mp.tool_budget(GEMMA12, old) is None


@pytest.mark.parametrize("change, message", [
    (lambda t: t["tool_budgets"].update(huge={"tools": 1}), "small, mid and large"),
    (lambda t: t["tool_budgets"]["small"].update(tools=-1), "a whole number"),
    (lambda t: t["tool_budgets"]["mid"].update(tools=5), "grow from class to class"),
    (lambda t: t["tool_budgets"]["mid"].update(params_below=20), "grow from class to class"),
    (lambda t: t["tool_budgets"]["small"].update(evidence=""), "printable text"),
    (lambda t: t["tool_budgets"]["small"].update(colour="red"), "an object of tools, params_below, evidence and short"),
    (lambda t: t["families"][0]["all"].update(tool_class="tiny"), "tool_class is small, mid or large"),
])
def test_damaged_budgets_are_refused(change, message):
    damaged = copy.deepcopy(TABLE)
    change(damaged)
    with pytest.raises(ValueError, match=message):
        mp.check_table(damaged)


def test_the_profile_carries_the_class():
    assert mp.match(ORN35, "claude", TABLE)["tool_class"] == "mid"
    assert mp.match(GEMMA12, "claude", TABLE)["tool_class"] is None  # (by its name: tool_budget)


def test_llm_profiles_says_the_budget(tmp_path, monkeypatch):
    llm = load("sandbox_llm_budget", "sandbox/llm.py")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    lines = []
    assert llm.main(["profiles", GEMMA12], out=lines.append) == 0
    assert any(line.startswith("MCP tools: about 10 at most (a small model: 12B, from its name; a start with more "
                               "says so) - round 5: Gemma 4 12B") for line in lines), lines
    lines.clear()
    assert llm.main(["profiles", ORN35, "--json"], out=lines.append) == 0
    assert json.loads(lines[0])["tool_budget"]["class"] == "mid"
    lines.clear()
    assert llm.main(["profiles", "gpt-5", "--json"], out=lines.append) == 0
    assert json.loads(lines[0])["tool_budget"] is None


# --- the count ------------------------------------------------------------------------------------------------------

def test_the_catalog_snapshot_has_the_researchs_servers():
    tools = mb.catalog_tools()
    assert len(tools["mcp:memory"]) == 9 and len(tools["mcp:tasks"]) == 10
    assert tools["mcp:sequential-thinking"] == ["sequentialthinking"] and len(tools["mcp:code-search"]) == 7
    assert all(key.split(":")[0] in ("mcp", "hub") for key in tools)


def test_the_snapshot_is_what_the_catalog_checkouts_list(tmp_path):
    """scripts/catalog_tools.py makes it from the catalogs' servers/NAME/tools.json (a checkout beside sandbox-ai, or
    a made-up one here): only entries of catalog.json, by their id."""
    script = load("catalog_tools_script", "scripts/catalog_tools.py")
    mcp = tmp_path / "mcp"
    (mcp / "servers/one").mkdir(parents=True)
    (mcp / "servers/pending").mkdir(parents=True)
    (mcp / "catalog.json").write_text(json.dumps({"entries": [{"id": "mcp:one", "name": "one"}]}))
    (mcp / "servers/one/tools.json").write_text(json.dumps({"tools": ["b", "a"]}))
    (mcp / "servers/pending/tools.json").write_text(json.dumps({"tools": ["x"]}))
    made = script.snapshot({"mcp": mcp, "hub": None}, {"servers": {"hub:old": ["y"], "mcp:gone": ["z"]},
                                                        "sources": {"hub": "axross-mcp-hub abc"}})
    assert made["servers"] == {"hub:old": ["y"], "mcp:one": ["a", "b"]}
    assert made["sources"] == {"mcp": "sandbox-ai-mcp", "hub": "axross-mcp-hub abc"}
    for prefix, repository in script.CATALOGS:
        checkout = ROOT / "projects" / repository
        if (checkout / "catalog.json").is_file():  # a development checkout: the snapshot is up to date with it
            ids = {entry["id"] for entry in json.loads((checkout / "catalog.json").read_text())["entries"]}
            assert {k for k in mb.catalog_tools() if k.startswith(prefix + ":")} <= ids


def test_the_snapshot_is_packaged():
    assert '"mcp_catalog_tools.json"' in (ROOT / "pyproject.toml").read_text()


def test_a_servers_tools_from_its_pin_then_the_catalog_then_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    pins.save(project, "brain", {f"t{n}": {"digest": "a" * 64, "definition": "{}"} for n in range(4)}, "first use")
    memory = catalog_server("mcp:memory")
    assert mb.server_tools("brain", memory, project) == {"server": "brain", "tools": 4, "from": "pin"}
    assert mb.server_tools("brain", memory, tmp_path / "other") == {"server": "brain", "tools": 9, "from": "catalog"}
    assert mb.server_tools("mine", {"command": ["./server"]}, project) == {"server": "mine", "tools": None,
                                                                         "from": None}
    assert mb.server_tools("demo", load("mcp_budget_mcp", "sandbox/mcp.py").BUILTIN["demo"], None)["tools"] == 2
    assert mb.server_tools("team-crew", {"command": ["python3"]}, None) == {"server": "team-crew", "tools": 5,
                                                                          "from": "sandbox-ai"}
    assert mb.server_tools("odd", catalog_server("mcp:not-in-the-catalog"), None)["tools"] is None


def test_sandbox_ais_own_counts_are_its_servers_own():
    demo = load("mcp_demo_budget", "sandbox/mcp_demo.py")
    board = load("team_board_budget", "sandbox/team_board.py")
    assert mb.OWN_TOOLS["demo"] == len(demo.TOOLS)
    assert mb.TEAM_TOOLS == len(board.tools(["a", "b"]))


def report(model, servers, table=TABLE):
    return mb.report(model, mp.tool_budget(model, table), servers)


def four_aids():
    return [mb.server_tools(name, catalog_server(source), None) for name, source in AIDS.items()]


def test_gemma_with_the_four_aids_is_over_its_budget():
    found = report(GEMMA12, four_aids())
    assert (found["tools"], found["budget"], found["class"], found["over"], found["about"]) == (27, 10, "small",
                                                                                               True, False)
    assert [s["server"] for s in found["servers"]] == ["tasks", "memory", "codesearch", "seqthink"]  # the most first
    assert found["drop"] == ["tasks", "memory"] and found["left"] == 8


def test_the_mid_class_keeps_them_and_a_model_of_no_class_counts_only():
    assert report(ORN35, four_aids())["over"] is False
    none = report("gpt-5", four_aids())
    assert none["budget"] is None and none["over"] is False and none["tools"] == 27


def test_an_unknown_server_makes_it_about():
    found = report(GEMMA12, [*four_aids(), {"server": "mine", "tools": None, "from": None}])
    assert found["about"] is True and found["unknown"] == ["mine"] and found["over"] is True
    assert report(GEMMA12, [{"server": "mine", "tools": None, "from": None}])["over"] is False  # nothing known: no


def test_the_warning_line():
    lines = mb.warning_lines(report(GEMMA12, four_aids()), "studio")
    assert lines == ["agent-sandbox: WARNING: --llm studio: gemma-4-12b-it would see 27 MCP tools, more than a small "
                     "model copes with (about 10; 12B, from its name): tasks brings 10, memory 9, codesearch 7. "
                     "Leave out tasks and memory (--mcp) to get to 8 - a warning: the start goes on (sandbox-ai's "
                     "research, round 5: small models did worse with many tools)"]
    about = mb.warning_lines(report(GEMMA12, [*four_aids(), {"server": "mine", "tools": None, "from": None}]), "s")
    assert "would see about 27 MCP tools" in about[0] and "(the tools of mine are not known yet)" in about[0]
    assert mb.warning_lines(report(ORN35, four_aids()), "studio") == []
    assert mb.warning_lines(None, "studio") == []


def test_start_report_reads_the_definitions_and_pins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    definitions = {name: catalog_server(source) for name, source in AIDS.items()}
    found = mb.start_report(GEMMA12, list(AIDS), tmp_path, definitions, TABLE)
    assert found["tools"] == 27 and found["over"]
    assert mb.start_report(GEMMA12, [], tmp_path, definitions, TABLE)["tools"] == 0
    assert mb.start_report(GEMMA12, ["memory"], tmp_path, definitions, None) is None  # no profiles: nothing


# --- end to end: the real ./sandbox-ai -------------------------------------------------------------------------------

def e2e_host(tmp_path, *images):
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image(*images)
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "mcp.json").write_text(json.dumps({name: catalog_server(source) for name, source in AIDS.items()}))
    host.env["SANDBOX_AI_MCP_NO_IMAGE_CHECK"] = "1"
    return host


def test_e2e_the_dry_run_warns_and_shows_the_count(tmp_path, monkeypatch):
    server, port = model_mock.start(models=(GEMMA12, ORN35), lmstudio=True, max_context=131072)
    try:
        host = e2e_host(tmp_path, "localhost/claude-sandbox:latest", "localhost/opencode-sandbox:latest")
        env = {"SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"}
        added = host.run("llm", "add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}",
                         "--model", GEMMA12, env=env)
        assert added.returncode == 0, added.stderr
        aids = [arg for name in AIDS for arg in ("--mcp", name)]

        dry = host.run("claude", "--llm", f"studio:{GEMMA12}", *aids, "--dry-run", env=env)
        assert dry.returncode == 0, dry.stderr  # a warning, never a refusal
        assert "WARNING: --llm studio: gemma-4-12b-it would see 27 MCP tools, more than a small model copes with " \
               "(about 10; 12B, from its name): tasks brings 10, memory 9, codesearch 7. Leave out tasks and " \
               "memory (--mcp) to get to 8" in " ".join(dry.stderr.split())
        counted = json.loads(dry.stdout)["mcp_tools"]
        assert counted["tools"] == 27 and counted["budget"] == 10 and counted["over"] is True
        assert counted["drop"] == ["tasks", "memory"]
        assert {s["server"]: s["from"] for s in counted["servers"]} == dict.fromkeys(AIDS, "catalog")

        fewer = host.run("claude", "--llm", f"studio:{GEMMA12}", "--mcp", "seqthink", "--dry-run", env=env)
        assert fewer.returncode == 0 and "MCP tools" not in fewer.stderr
        assert json.loads(fewer.stdout)["mcp_tools"]["over"] is False

        mid = host.run("opencode", "--llm", f"studio:{ORN35}", *aids, "--dry-run", env=env)
        assert mid.returncode == 0 and "MCP tools" not in mid.stderr, mid.stderr
        assert json.loads(mid.stdout)["mcp_tools"]["class"] == "mid"

        # A pin of the project counts over the catalog: this project's memory server lists two tools.
        monkeypatch.setenv("XDG_STATE_HOME", host.env["XDG_STATE_HOME"])
        pins.save(host.workspace, "memory", {n: {"digest": "a" * 64, "definition": "{}"} for n in ("a", "b")},
                  "first use")
        pinned = host.run("claude", "--llm", f"studio:{GEMMA12}", *aids, "--dry-run", env=env)
        counted = json.loads(pinned.stdout)["mcp_tools"]
        assert counted["tools"] == 20 and {s["server"]: s["from"] for s in counted["servers"]}["memory"] == "pin"

        none = host.run("claude", "--dry-run", *aids, env=env)  # no --llm: the provider's model, nothing counted
        assert none.returncode == 0 and json.loads(none.stdout).get("mcp_tools") is None
    finally:
        server.shutdown()


def test_e2e_a_real_start_says_it_and_goes_on(tmp_path):
    server, port = model_mock.start(models=(GEMMA26,), lmstudio=True, max_context=131072)
    try:
        host = e2e_host(tmp_path, "localhost/claude-sandbox:latest")
        env = {"SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"}
        assert host.run("llm", "add", "studio", "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}",
                        "--model", GEMMA26, env=env).returncode == 0
        done = host.run("claude", "--no-tmux", "--llm", f"studio:{GEMMA26}", "--mcp", "memory", "--mcp", "tasks",
                        env=env)
        assert "gemma-4-26b-a4b-qat would see 19 MCP tools, more than a small model copes with (about 10; 26B, " \
               "from its name)" in " ".join(done.stderr.split()), done.stderr
        assert host.stop_path.exists(), "the start went on as far as its pod: " + done.stderr[-2000:]
        # Its MCP servers serve on (they outlive a start, README "Local MCP servers"; the failed start is kept and a
        # resume uses them) - this host's HOME is the test's own: the test stops them, and nothing of it runs on
        # (2026-11-20: four processes of every run were left for hours).
        listed = json.loads(host.run("mcp", "list", "--json").stdout)
        assert {s["name"] for s in listed if s["status"] == "running"} == {"memory", "tasks"}, listed
        for name in ("memory", "tasks"):
            stopped = host.run("mcp", "stop", name)
            assert stopped.returncode == 0, stopped.stderr
        assert host.leftovers() == {}
    finally:
        server.shutdown()
