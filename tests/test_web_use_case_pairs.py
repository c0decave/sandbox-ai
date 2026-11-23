"""Recommendations as model x harness pairs in the web interface (app.js in node; user 2026-11-20, step 3 of 4): the
start form's use case lists, below its choice, the chosen server's models as pairs ranked by the use case's table,
each in the agent it was measured best in (llm recommend --json's "pairs": the real sandbox-ai's on a table of the
test's own) - not measured said, an unsafe pair with its reason and asked in place before it is taken; Use this pair
sets the agent and the model; when the agent chosen is not the pair's harness the pair says so with the model's
status there (llm models --json's support). English and German."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM  # noqa: E402

GB = 1000 ** 3
ORN, MID, KAT = "ornith-1.5-35b-a3b-mlx", "mid-35b-mlx", "kat-coder-v2.5-dev"
TABLE = {
    "schema": 1, "source": "the test's own", "rounds": {"round 5": "2026-11-18"},
    "use_cases": [
        {"id": "untrusted-input", "label": "Untrusted input", "about": "planted instructions", "tasks": ["s1b_mcp"],
         "recommendations": [
             {"model": "ornith-?1\\.5-?35b", "examples": [ORN], "label": "Ornith-35B", "harness": "claude",
              "basis": "measured", "why": "no planted instruction worked (6/6)", "evidence": "round 5: s1b 6/6",
              "round": "round 5", "runs": 6, "solved": 6, "score": 1.0},
             {"model": "mid-?35b", "examples": [MID], "label": "Mid", "harness": "opencode", "basis": "measured",
              "why": "3 of 3 in OpenCode", "evidence": "round 5: s1b 3/3", "round": "round 5", "runs": 3,
              "solved": 3}],
         "unsafe": [{"model": "kat-?coder", "examples": [KAT], "label": "KAT", "harness": "opencode",
                     "why": "ran an MCP ticket's planted command", "evidence": "round 5: s1b 2/3",
                     "round": "round 5"}]}]}


def recommended(table=TABLE):
    spec = importlib.util.spec_from_file_location("sandbox_model_usecases_web_pairs", ROOT / "sandbox/model_usecases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.check_table(table)
    servers = [{"name": "studio", "memory_bytes": 128 * GB,
                "models": [{"name": m, "size": 37 * GB} for m in (KAT, MID, ORN)]}]
    return {"schema": 1, "use_cases": module.recommend_all(table, servers)}


DRIVER = FAKE_DOM + r"""
const byId = (id) => body.all().find(n => n.id === id);
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const RECOMMENDED = __RECOMMENDED__;
const DE = process.argv[3] === "de";
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  if (DE) vm.runInContext('setLanguage("de")', context);
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  const support = { light: "green", family: "ornith-1.5-35b", label: "Ornith", by_light: { green: ["claude"], yellow: ["opencode"], red: [] },
                    harnesses: [{ agent: "claude", status: "verified" }, { agent: "opencode", status: "partial" }], untested: ["pi"], status: null,
                    names: { claude: "Claude Code", opencode: "OpenCode" } };
  answer = (method, path) => (path.endsWith("/llm-recommend") ? RECOMMENDED : path.endsWith("/starts") ? [] : path.endsWith("/presets") ? []
    : path.endsWith("/llm") ? [{ name: "studio", kind: "lmstudio", agents: ["claude", "codex", "opencode"] }]
    : path.endsWith("/llm/studio/models") ? [{ name: "kat-coder-v2.5-dev" }, { name: "mid-35b-mlx" }, { name: "ornith-1.5-35b-a3b-mlx", support }] : []);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  const useCase = byId("start-use-case");
  byId("agent").value = "codex"; byId("agent").fire("change"); await settle();
  useCase.value = "untrusted-input"; useCase.fire("change"); await settle();
  const pairs = () => byId("start-use-case-pairs").children;
  result.agent = byId("agent").value; result.model = byId("start-model").value;
  result.pairs = pairs().map(li => li.textContent);
  result.midNote = pairs()[1].textContent;  // Mid (OpenCode's) while the agent is Claude Code
  // Mid in OpenCode
  buttonIn(pairs()[1], DE ? "Dieses Paar nehmen" : "Use this pair").fire("click"); await settle();
  result.afterMid = [byId("agent").value, byId("start-model").value];
  result.ornithNote = pairs()[0].textContent;
  // KAT: asked in place first; Cancel keeps what was chosen
  buttonIn(pairs()[2], DE ? "Dieses Paar nehmen" : "Use this pair").fire("click"); await settle();
  result.asked = pairs()[2].textContent;
  buttonIn(pairs()[2], DE ? "Abbrechen" : "Cancel").fire("click"); await settle();
  result.afterCancel = byId("start-model").value;
  buttonIn(pairs()[2], DE ? "Dieses Paar nehmen" : "Use this pair").fire("click"); await settle();
  buttonIn(pairs()[2], DE ? "Trotzdem nehmen" : "Use it anyway").fire("click"); await settle();
  result.afterKat = [byId("agent").value, byId("start-model").value];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def drive(tmp_path, language="en", table=TABLE):
    driver = tmp_path / f"pairs_{language}.js"
    driver.write_text(DRIVER.replace("__RECOMMENDED__", json.dumps(recommended(table))))
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_start_form_lists_the_pairs_and_takes_one(tmp_path):
    result = drive(tmp_path)
    assert result["agent"] == "claude" and result["model"] == f"studio:{ORN}"
    first, second, third = result["pairs"]
    assert first.startswith(f"{ORN} × Claude Code — rank 1, measured") and "no planted instruction worked" in first
    assert second.startswith(f"{MID} × OpenCode — rank 2, measured")
    assert third.startswith(f"{KAT} × OpenCode — ⚠ unsafe for this use case")
    assert "Unsafe for this use case: ran an MCP ticket's planted command" in third   # never hidden: with why
    assert result["afterMid"] == ["opencode", f"studio:{MID}"]
    assert f"{ORN} is measured best in Claude Code; in OpenCode: partly working" in result["ornithNote"]
    assert "unsafe for this use case: ran an MCP ticket's planted command. With untrusted input" in result["asked"]
    assert result["afterCancel"] == f"studio:{MID}"
    assert result["afterKat"] == ["opencode", f"studio:{KAT}"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_pairs_in_german(tmp_path):
    result = drive(tmp_path, "de")
    assert result["pairs"][0].startswith(f"{ORN} × Claude Code — Rang 1, gemessen")
    assert "⚠ unsicher für diesen Einsatzzweck" in result["pairs"][2]
    assert "Unsicher für diesen Einsatzzweck: ran an MCP ticket's planted command" in result["pairs"][2]
    assert f"{ORN} ist in Claude Code am besten gemessen; in OpenCode:" in result["ornithNote"]
    assert result["afterKat"] == ["opencode", f"studio:{KAT}"]


def with_mid_in_claude():
    """The test's table with Mid also in Claude Code: rank 3, from other tasks."""
    table = copy.deepcopy(TABLE)
    table["use_cases"][0]["recommendations"].append(
        {"model": "mid-?35b", "examples": [MID], "label": "Mid", "harness": "claude", "basis": "indirect",
         "why": "from the coding tasks", "evidence": "round 5: coding 4/5", "round": "round 5"})
    return table


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_another_agent_s_note_says_the_table_s_rank_there_as_the_app_does(tmp_path):
    """Follow-up of 0.1.172 (2026-11-20): when the start's agent was not the pair's harness, the web's note said only
    the model profile's status ("this pair untested" for Mid in Claude Code), never the table's rank of that model
    in that agent - the app says "in Claude Code: rank 3, from other tasks" (llm recommend --json's in_agents now)."""
    result = drive(tmp_path, table=with_mid_in_claude())
    assert result["agent"] == "claude"
    assert f"{MID} is measured best in OpenCode; in Claude Code: rank 3, from other tasks" in result["midNote"]
    # a model the table has in no other agent: the profile's status (llm models --json's support), as before
    assert f"{ORN} is measured best in Claude Code; in OpenCode: partly working" in result["ornithNote"]
    # without the rank (the table's own): the profile's word, never a rank made up
    plain = drive(tmp_path)
    assert f"{MID} is measured best in OpenCode; in Claude Code: no data yet" in plain["midNote"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_another_agent_s_rank_in_german(tmp_path):
    result = drive(tmp_path, "de", table=with_mid_in_claude())
    assert f"{MID} ist in OpenCode am besten gemessen; in Claude Code: Rang 3, aus anderen Aufgaben" in \
        result["midNote"]
