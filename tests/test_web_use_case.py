"""Model and harness per use case in the web interface (app.js in node; user 2026-11-17, "B6"): a new start's form has
a "Use case" field listing the host's use cases (GET /llm-recommend: `sandbox-ai llm recommend --json` there) with
their status; choosing one sets the agent and the model (SERVER:MODEL of that host's servers) to its choice, says why
with the evidence, what fits, the known failures and what is not known, and the start carries --use-case. Overridable:
another model chosen afterwards is what starts. A use case without a model server is refused with words. The answer
is what the real sandbox-ai says (model_usecases.recommend_all on a table of the test's own), English and German."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM  # noqa: E402

GB = 1000 ** 3
MID, QCN = "mid-35b-mlx", "qwen/qwen3-coder-next"
TABLE = {
    "schema": 1, "source": "the test's own", "rounds": {"round 4": "2026-11-17"},
    "use_cases": [
        {"id": "coding", "label": "Coding and debugging", "about": "fix a bug", "tasks": ["t2n_debug"],
         "unknown": ["servers other than LM Studio: not measured"],
         "recommendations": [
             {"model": "mid-?35b", "examples": [MID], "label": "Mid", "harness": "opencode", "basis": "measured",
              "params": {"temperature": 0.5}, "why": "solved it 2 of 2 in OpenCode",
              "fails": ["Qwen Code: changed the tests"], "evidence": "round 4: debug 2/2", "round": "round 4",
              "runs": 2, "solved": 2}]},
        {"id": "code-review", "label": "Code review", "about": "review a branch", "tasks": ["r3_review_py"],
         "unknown": ["reviews: not tested before round 5"], "recommendations": []}]}


def recommended():
    spec = importlib.util.spec_from_file_location("sandbox_model_usecases_web", ROOT / "sandbox/model_usecases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.check_table(TABLE)
    servers = [{"name": "studio", "memory_bytes": 64 * GB,
                "models": [{"name": MID, "size": 37 * GB}, {"name": QCN, "size": 45 * GB, "loaded": True}]}]
    return {"schema": 1, "use_cases": module.recommend_all(TABLE, servers)}


DRIVER = FAKE_DOM + r"""
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const byId = (id) => body.all().find(n => n.id === id);
const RECOMMENDED = __RECOMMENDED__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  answer = (method, path) => (path.endsWith("/llm-recommend") ? RECOMMENDED : path.endsWith("/starts") ? [] : path.endsWith("/presets") ? []
    : path.endsWith("/llm") ? [{ name: "studio", kind: "lmstudio", agents: ["claude", "codex", "opencode"] }]
    : path.endsWith("/llm/studio/models") ? [{ name: "mid-35b-mlx" }, { name: "qwen/qwen3-coder-next", loaded: true }] : []);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.asked = calls.filter(c => c.path.endsWith("/llm-recommend")).map(c => [c.method, c.path, c.headers["X-Background"] || null]);
  const useCase = byId("start-use-case");
  result.options = useCase.children.map(o => [o.value, o.textContent]);
  byId("agent").value = "claude"; byId("agent").fire("change"); await settle();
  useCase.value = "coding"; useCase.fire("change"); await settle();
  result.agent = byId("agent").value;
  result.model = byId("start-model").value;
  result.hint = byId("start-use-case-hint").textContent;
  result.known = byId("start-use-case-known").children.map(n => n.textContent);
  byId("workspace").value = "/srv/w"; byId("options").value = ""; byId("name").value = "";
  buttonIn(main, process.argv[3] === "de" ? "In einem Terminal starten" : "Start in a terminal").fire("click"); await settle();
  result.started = vm.runInContext("terminalsOpened[terminalsOpened.length - 1]", context);
  // overridable: another model chosen afterwards is what starts, with the use case
  byId("start-model").value = "studio:qwen/qwen3-coder-next"; byId("start-model").fire("change");
  buttonIn(main, process.argv[3] === "de" ? "In einem Terminal starten" : "Start in a terminal").fire("click"); await settle();
  result.other = vm.runInContext("terminalsOpened[terminalsOpened.length - 1]", context);
  // not tested: said; without a model server: refused with words, nothing starts
  useCase.value = "code-review"; useCase.fire("change"); await settle();
  result.untested = byId("start-use-case-hint").textContent;
  result.untestedKnown = byId("start-use-case-known").children.map(n => n.textContent);
  byId("start-model").value = ""; byId("start-model").fire("change");
  const before = vm.runInContext("terminalsOpened.length", context);
  buttonIn(main, process.argv[3] === "de" ? "In einem Terminal starten" : "Start in a terminal").fire("click"); await settle();
  result.refused = [vm.runInContext("terminalsOpened.length", context) - before, byId("start-status").textContent];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def drive(tmp_path, language="en"):
    import subprocess
    driver = tmp_path / f"use_case_{language}.js"
    driver.write_text(DRIVER.replace("__RECOMMENDED__", json.dumps(recommended())))
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_start_form_picks_by_use_case_and_starts_with_it(tmp_path):
    result = drive(tmp_path)
    assert result["asked"] == [["GET", "/api/hosts/ct1/llm-recommend", "1"]]
    assert result["options"] == [["", "none: choose the agent and the model yourself"],
                                 ["coding", "Coding and debugging — measured on this use case's tasks"],
                                 ["code-review", "Code review — no data yet"]]
    assert result["agent"] == "opencode" and result["model"] == f"studio:{MID}"
    hint = result["hint"]
    assert hint.startswith(f"Choice: studio:{MID} in OpenCode (rank 1) · solved it 2 of 2 in OpenCode")
    assert "Measured: round 4: debug 2/2, 2026-11-17" in hint and "Settings over the model profile: temperature 0.5" in hint
    assert "fits: about 46 GB of 48 GB" in hint
    assert result["known"] == ["✗ Qwen Code: changed the tests", "? servers other than LM Studio: not measured"]
    options = json.loads(result["started"]["options"])
    assert options[options.index("--llm") + 1] == f"studio:{MID}"
    assert options[options.index("--use-case") + 1] == "coding" and result["started"]["agent"] == "opencode"
    other = json.loads(result["other"]["options"])
    assert other[other.index("--llm") + 1] == f"studio:{QCN}" and "--use-case" in other
    assert result["untested"].startswith("No data yet") or result["untested"].startswith("Choice: ")
    assert "? reviews: not tested before round 5" in result["untestedKnown"]
    assert result["refused"][0] == 0 and "A use case chooses among the models of a server of yours" in \
        result["refused"][1]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_use_case_field_in_german(tmp_path):
    result = drive(tmp_path, "de")
    options = dict(result["options"])
    assert options["coding"] == "Programmieren und Fehlersuche — an den Aufgaben dieses Einsatzzwecks gemessen"
    assert options["code-review"] == "Code-Review — noch keine Daten"
    assert result["hint"].startswith(f"Wahl: studio:{MID} in OpenCode (Rang 1)")
    assert "Ein Einsatzzweck wählt unter den Modellen" in result["refused"][1]


MEMORY_DRIVER = FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const rowOf = (name) => main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === name);
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "llm"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  const show = { name: "mac", kind: "lmstudio", url: "http://192.168.1.20:1234", model: null, api_key_secret: null, headers: {},
                 ca: null, vpn: null, anthropic: false, insecure_http: false, models: [], checked: null, memory_gb: 64 };
  answer = (method, path) => {
    if (method === "GET" && path.endsWith("/llm")) return [{ name: "mac", kind: "lmstudio", url: show.url, memory_gb: 64 }];
    if (method === "GET" && path.endsWith("/llm/mac")) return show;
    if (method === "GET") return [];
    return { output: "ok" };
  };
  await vm.runInContext("pages.llm(__main)", context); await settle();
  // a new server with its memory (the form is drawn again after an Add: filled each time)
  const fill = (gb) => {
    for (const id of ["llm-model", "llm-key-secret", "llm-key-value", "llm-headers", "llm-ca", "llm-vpn"]) byId(id).value = "";
    byId("llm-name").value = "gpu"; byId("llm-kind").value = "vllm"; byId("llm-url").value = "http://100.100.1.5:8000";
    byId("llm-memory").value = gb;
  };
  fill("128");
  calls.length = 0;
  buttonIn(main, "Add").fire("click"); await settle();
  result.added = posts().filter(p => p[0].endsWith("/llm")).map(p => p[1].memory_gb);
  fill("0.5");
  calls.length = 0;
  buttonIn(main, "Add").fire("click"); await settle();
  result.refused = [posts().length, byId("llm-status") ? byId("llm-status").textContent : main.textContent.includes("1 to 65536 GB")];
  // Edit…: the memory shown; emptied: forgotten (0)
  buttonIn(rowOf("mac"), "Edit…").fire("click"); await settle();
  result.shown = byId("llm-memory").value;
  byId("llm-memory").value = "";
  calls.length = 0;
  buttonIn(main, "Save").fire("click"); await settle();
  const yes = buttonIn(main, "Yes, replace it"); if (yes) { yes.fire("click"); await settle(); }
  result.saved = posts().filter(p => p[0].endsWith("/llm")).map(p => p[1].memory_gb);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_llm_page_keeps_a_servers_memory(tmp_path):
    """The web's LLM page has the server's memory (llm add --memory; llm show's memory_gb) for what fits it."""
    import subprocess
    driver = tmp_path / "memory.js"
    driver.write_text(MEMORY_DRIVER)
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), "en"], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["added"] == [128]
    assert result["refused"][0] == 0
    assert result["shown"] == "64" and result["saved"] == [0]
