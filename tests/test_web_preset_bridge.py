"""A preset with a bridge in the web interface's start form (app.js in node; user 2026-11-22): its value of kind session
offers that host's running sessions, the form says what the preset proposes and that it is made only in the start's
terminal on the human's yes, and the start carries --preset NAME --preset-value KEY=SESSION (the launcher asks in that
terminal once the start runs). The preset is what the real sandbox-ai says of it (presets show --json); English and
German."""
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


def shown(name):
    spec = importlib.util.spec_from_file_location("sandbox_presets_web_bridge", ROOT / "sandbox/presets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    preset = module.catalog()[name]
    return {key: value for key, value in preset.items() if not key.startswith("_")} | {"origin": preset["_origin"]}


DRIVER = FAKE_DOM + r"""
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const byId = (id) => body.all().find(n => n.id === id);
const PRESET = __PRESET__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  answer = (method, path) => (path.endsWith("/presets") ? [{ id: PRESET.id, title: PRESET.title, agents: PRESET.agents, category: PRESET.category }]
    : path.endsWith(`/presets/${PRESET.id}`) ? PRESET
    : path.endsWith(`/presets/${PRESET.id}/check`) ? { ok: true, output: "" }
    : path.endsWith("/starts") ? [{ name: "web", kind: "session", state: "running", agent: "claude" },
                                  { name: "old", kind: "session", state: "stopped", agent: "claude" },
                                  { name: "probe", kind: "run", state: "running", agent: "codex", helper_of: "web" }]
    : []);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  byId("agent").value = "claude"; byId("agent").fire("change"); await settle();
  byId("preset").value = PRESET.id; byId("preset").fire("change"); await settle();
  result.proposed = (byId("preset-bridge") || { textContent: null }).textContent;
  const field = byId("preset-value-coder");
  result.placeholder = field.getAttribute("placeholder");
  const list = byId(field.getAttribute("list"));
  result.offered = list ? list.children.map(o => o.value !== undefined ? o.value : o.getAttribute("value")) : null;
  field.value = "web";
  byId("workspace").value = "/srv/review"; byId("options").value = ""; byId("name").value = "rev";
  buttonIn(main, process.argv[3] === "de" ? "In einem Terminal starten" : "Start in a terminal").fire("click"); await settle();
  result.checked = calls.filter(c => c.method === "POST" && c.path.endsWith("/check")).map(c => JSON.parse(c.body));
  result.started = vm.runInContext("terminalsOpened[terminalsOpened.length - 1]", context);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def drive(tmp_path, language="en"):
    driver = tmp_path / f"preset_bridge_{language}.js"
    driver.write_text(DRIVER.replace("__PRESET__", json.dumps(shown("pair-reviewer"))))
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_start_form_offers_the_running_sessions_and_says_what_is_proposed(tmp_path):
    result = drive(tmp_path)
    assert result["proposed"] == ("It proposes a bridge: this start may reach the session you choose (ask, "
                                  "files:ro:{code}). Nothing is made by itself: once the start runs, its terminal asks "
                                  "you, with the warnings of bridge add, and makes it only on your yes.")
    assert result["placeholder"] == "a running session (empty: chosen in the terminal once the start runs)"
    assert result["offered"] == ["web"], "running agent sessions of that host only (no stopped one, no helper)"
    assert result["checked"][-1]["values"]["coder"] == "web"
    options = json.loads(result["started"]["options"])
    assert options[:2] == ["--preset", "pair-reviewer"] and "coder=web" in options
    assert result["started"]["agent"] == "claude"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_proposal_in_german(tmp_path):
    result = drive(tmp_path, "de")
    assert result["proposed"].startswith("Sie schlägt eine Brücke vor: Dieser Start darf die Sitzung erreichen")
    assert "legt sie nur auf Ihr Ja an" in result["proposed"]
    assert result["placeholder"].startswith("eine laufende Sitzung")
