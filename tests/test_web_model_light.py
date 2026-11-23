"""The traffic light per local model in the web interface (app.js in node, 0.1.98). User: "farbliche Ampel zu den
Modellen, die auf einem lokalen LLM-Server sind: wie weit wir sie supporten und erfolgreich in welchem Harness hatten.
Detail-Infos, was funktioniert hat und was nicht, pro Modell".

The LLM page's models table gets a Support column when the host's `llm models --json` carries "support" (an older
host's does not: no column): the symbol in its colour and the words, a click opens the details per agent (what
worked, what failed, the recommended settings, the speed, the round and its date; "no data yet" for a model of no
family); the Test connection list, the default-model select and a new start's model select say it in words. The
answers are what the real sandbox-ai says (model_profiles.support on the shipped table), in English and German."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM  # noqa: E402

QCN = "qwen/qwen3-coder-next"
Q38 = "mistralai/devstral-small-2-2512"   # red where it was run: Claude Code, OpenCode, Codex (0 passed, round 3)
OTHER = "meta/llama3-8b"


def support(model):
    spec = importlib.util.spec_from_file_location("sandbox_model_profiles_web_light", ROOT / "sandbox/model_profiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.support(model, module.load())


def models():
    return [{"name": QCN, "loaded": True, "size": 45000000000, "support": support(QCN)},
            {"name": Q38, "loaded": False, "support": support(Q38)},
            {"name": OTHER, "loaded": False, "support": support(OTHER)}]


DRIVER = FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const rowOf = (name) => main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === name);
const MODELS = __MODELS__;
const OLD = MODELS.map(m => ({ name: m.name, loaded: m.loaded }));
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "llm"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  let listed = MODELS;
  answer = (method, path) => {
    if (method === "GET" && path.endsWith("/llm")) return [{ name: "studio", kind: "lmstudio", url: "http://192.168.1.20:1234", model: MODELS[0].name }];
    if (path.endsWith("/llm/studio/models")) return listed;
    if (path.endsWith("/llm-models")) return listed;
    if (path.endsWith("/llm-status")) return [];
    if (path.endsWith("/starts") || path.endsWith("/presets") || path.endsWith("/vpn") || path.endsWith("/mcp") || path.endsWith("/secrets")) return [];
    return { output: "ok" };
  };
  await vm.runInContext("pages.llm(__main)", context); await settle();
  const studio = rowOf("studio");
  const modelsButton = studio.all().find(n => n.tagName === "button" && /^(Models|Modelle)$/.test(n.textContent));
  modelsButton.fire("click"); await settle();
  const table = main.all().find(n => n.tagName === "table" && n.all().some(x => x.attributes && x.attributes["data-model"]));
  result.columns = table.all().filter(n => n.tagName === "th").map(n => n.textContent);
  const rows = table.all().filter(n => n.tagName === "tr" && n.attributes["data-model"]);
  const supportCell = (row) => row.all().find(n => n.tagName === "details");
  result.cells = rows.map(r => {
    const d = supportCell(r), summary = d.children[0], dot = summary.children[0];
    return [r.attributes["data-model"], summary.textContent, dot.className, dot.attributes["aria-hidden"], summary.attributes.title, summary.attributes["aria-label"]];
  });
  result.details = rows.map(r => supportCell(r).children[1].all().filter(n => ["p", "h4", "li"].includes(n.tagName)).map(n => n.textContent));
  // the Test connection list and the default-model select
  for (const id of ["llm-key-secret", "llm-key-value", "llm-headers", "llm-ca", "llm-vpn", "llm-name", "llm-model"]) byId(id).value = "";
  byId("llm-kind").value = "lmstudio"; byId("llm-url").value = "http://192.168.1.20:1234";
  buttonIn(main, process.argv[3] === "de" ? "Verbindung testen" : "Test connection").fire("click"); await settle();
  result.picks = byId("llm-model-pick").children.map(o => [o.value, o.textContent, o.attributes.title || null]);
  // an older host's answer has no support: no column, the select as before
  listed = OLD;
  clear_ = vm.runInContext("clear", context);
  clear_(main); await vm.runInContext("pages.llm(__main)", context); await settle();
  rowOf("studio").all().find(n => n.tagName === "button" && /^(Models|Modelle)$/.test(n.textContent)).fire("click"); await settle();
  const old = main.all().find(n => n.tagName === "table" && n.all().some(x => x.attributes && x.attributes["data-model"]));
  result.oldColumns = old.all().filter(n => n.tagName === "th").map(n => n.textContent);
  // a new start's model select: the light in words
  listed = MODELS;
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  clear_(main);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  byId("agent").value = "opencode"; byId("agent").fire("change"); await settle();
  result.start = byId("start-model").children.filter(o => o.value.startsWith("studio:")).map(o => [o.value, o.textContent, o.attributes.title || null]);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def drive(tmp_path, language="en"):
    import subprocess
    driver = tmp_path / f"light_{language}.js"
    driver.write_text(DRIVER.replace("__MODELS__", json.dumps(models())))
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_llm_page_shows_each_models_light_and_its_details(tmp_path):
    result = drive(tmp_path)
    assert result["columns"][-2:] == ["Support", ""]
    cells = {row[0]: row[1:] for row in result["cells"]}
    text, dot, hidden, title, label = cells[QCN]
    assert text.startswith("✓ verified: ") and "Claude Code" in text and dot == "light light-green" and hidden == "true"
    assert title.startswith("verified in ") and "Claude Code" in title.split(";")[0] and "; partly working in " in title
    assert label == f"Support of {QCN}: {title}"
    # red only in the agents it was run in; every other agent has no data yet (2026-11-22: a light only from runs)
    assert cells[Q38][:2] == ["✗ does not work: Claude Code, OpenCode, Codex", "light light-red"]
    assert cells[Q38][2:4] == ["true", "does not work in Claude Code, OpenCode, Codex"]
    assert cells[OTHER][:2] == ["? no data yet", "light light-grey"] and cells[OTHER][3] == "no data yet"
    qcn, q38, other = (result["details"][[row[0] for row in result["cells"]].index(m)] for m in (QCN, Q38, OTHER))
    found = support(QCN)
    claude = next(pair for pair in found["harnesses"] if pair["agent"] == "claude")
    counts = claude["measured_counts"]
    assert qcn[0] == f"Model family: {found['label']}" and any(line.startswith("Measured speed: ") for line in qcn)
    # where the status is shown, the derived evidence: runs, use cases, rounds
    assert f"✓ Claude Code: verified ({counts['runs']} runs, {counts['use_cases']} use cases, phase 2, rounds 1, 5)" \
        in qcn
    assert f"Evidence of phase 2, round 1, round 5, {claude['date']}" in qcn
    assert f"Measured: {claude['evidence']}" in qcn and f"✓ {claude['works'][0]}" in qcn
    assert f"Recommended settings: {claude['settings_text']}" in qcn
    assert any(line.startswith("~ Codex: partly working (") for line in qcn)   # round 8: OpenCode verified
    assert qcn[-1].startswith("No data yet in: ")
    assert any(line.startswith("✗ Claude Code: failing") for line in q38) and q38[-1].startswith("No data yet in: ")
    assert not any(line.startswith("Known not to work") for line in q38), "no family-wide verdict"
    assert other == ["No data yet: no model family of sandbox-ai's research matches this name."]
    picks = {value: (text, title) for value, text, title in result["picks"]}
    assert picks[QCN][0].startswith(f"{QCN}  — ✓ verified: ") and "Claude Code" in picks[QCN][0] and picks[QCN][1].startswith("verified in ")
    assert picks[OTHER] == (f"{OTHER}  — ? no data yet", "no data yet")
    assert "Support" not in result["oldColumns"], "an older host's answer: no column"
    start = {value: (text, title) for value, text, title in result["start"]}
    # a start's select, OpenCode chosen: OpenCode's own measured light first, then the rest
    assert start[f"studio:{Q38}"] == (f"studio:{Q38}  — ✗ In OpenCode: does not work",
                                      "In OpenCode: does not work. does not work in Claude Code, OpenCode, Codex")
    assert start[f"studio:{QCN}"][0] == f"studio:{QCN}  (loaded, 45.0 GB)  — ✓ In OpenCode: verified"
    assert start[f"studio:{OTHER}"][0] == f"studio:{OTHER}  — ? In OpenCode: no data yet"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_start_s_select_never_shows_another_agents_light_for_the_chosen_one(tmp_path):
    """The user, 2026-11-22: "it says opencode verifiziert" for a model never run in OpenCode. Qwen3.8-27B was run in
    Claude Code only: with OpenCode chosen its entry is grey (no data yet), never Claude Code's yellow."""
    global QCN
    saved, QCN = QCN, "qwen/qwen3.8-27b"
    try:
        result = drive(tmp_path)
    finally:
        QCN = saved
    start = {value: (text, title) for value, text, title in result["start"]}
    text, title = start["studio:qwen/qwen3.8-27b"]
    assert text.endswith("— ? In OpenCode: no data yet") and title.startswith("In OpenCode: no data yet. partly working in Claude Code")


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_light_in_german(tmp_path):
    result = drive(tmp_path, "de")
    assert result["columns"][-2] == "Unterstützung"
    cells = {row[0]: row[1:] for row in result["cells"]}
    assert cells[QCN][0].startswith("✓ verifiziert: ") and "Claude Code" in cells[QCN][0]
    qcn = result["details"][[row[0] for row in result["cells"]].index(QCN)]
    counts = next(p for p in support(QCN)["harnesses"] if p["agent"] == "claude")["measured_counts"]
    assert f"✓ Claude Code: verifiziert ({counts['runs']} Läufe, {counts['use_cases']} Anwendungsfälle, " \
           "phase 2, rounds 1, 5)" in qcn
    assert cells[OTHER][3] == "noch keine Daten"
    assert cells[QCN][4].startswith(f"Unterstützung von {QCN}: verifiziert in ")
    other = result["details"][[row[0] for row in result["cells"]].index(OTHER)]
    assert other[0].startswith("Noch keine Daten")
