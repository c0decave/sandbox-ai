"""The web interface's start form offers each agent's cloud models (GET /models: `sandbox-ai models --json` on that
host, the table the launcher refuses an --effort by) grouped by provider, and the Effort choice the chosen model
takes; a level the next model does not take is reset with a note; the start carries --model and --effort. The table
is the real one (the command's own output), in English and German."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM  # noqa: E402

DRIVER = FAKE_DOM + r"""
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const byId = (id) => body.all().find(n => n.id === id);
const MODELS = __MODELS__;
(async () => {
  const result = {};
  const de = process.argv[3] === "de";
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  if (de) vm.runInContext('setLanguage("de")', context);
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  answer = (method, path) => (path.endsWith("/models") ? MODELS : path.endsWith("/starts") ? [] : path.endsWith("/llm") ? [] : []);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.asked = calls.filter(c => c.path.endsWith("/models")).map(c => [c.method, c.path]);
  const model = byId("start-model"), effort = byId("start-effort");
  byId("agent").value = "claude"; byId("agent").fire("change"); await settle();
  result.groups = model.children.filter(n => n.tagName === "optgroup").map(g => [g.getAttribute("label"), g.children.length]);
  const levels = () => effort.children.map(o => o.value);
  model.value = "=claude-opus-5-5"; model.fire("change");
  result.opus = [levels(), effort.children[0].textContent];
  effort.value = "xhigh"; effort.fire("change");
  model.value = "=claude-opus-4-6"; model.fire("change");
  result.reset = [levels(), effort.value, byId("start-effort-hint").textContent];
  effort.value = "max"; effort.fire("change");
  byId("workspace").value = "/srv/w"; byId("options").value = "--model sonnet --effort low"; byId("name").value = "";
  buttonIn(main, de ? "In einem Terminal starten" : "Start in a terminal").fire("click"); await settle();
  result.started = vm.runInContext("terminalsOpened[terminalsOpened.length - 1]", context);
  model.value = "=claude-haiku-4-5"; model.fire("change");
  result.haiku = [levels(), effort.disabled];
  byId("agent").value = "opencode"; byId("agent").fire("change"); await settle();
  result.opencode = [effort.disabled, effort.title, model.children.filter(n => n.tagName === "optgroup").map(g => g.getAttribute("label")).slice(0, 3)];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def models_json():
    done = subprocess.run([str(ROOT / "sandbox-ai"), "models", "--json"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def drive(tmp_path, language="en"):
    driver = tmp_path / f"cloud_models_{language}.js"
    driver.write_text(DRIVER.replace("__MODELS__", json.dumps(models_json())))
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_start_form_offers_the_cloud_models_and_their_efforts(tmp_path):
    result = drive(tmp_path)
    assert result["asked"] == [["GET", "/api/hosts/ct1/models"]]
    assert ["Cloud: Anthropic (aliases)", 6] in result["groups"] and ["Cloud: Anthropic", 13] in result["groups"]
    assert result["opus"] == [["", "low", "medium", "high", "xhigh", "max"], "the agent's own default (medium for this model)"]
    assert result["reset"] == [["", "low", "medium", "high", "max"], "",
                               "The effort xhigh was reset: claude-opus-4-6 does not take it."]
    options = json.loads(result["started"]["options"])
    # the form's choice wins over the options' own --model and --effort, once each
    assert options.count("--model") == 1 and options[options.index("--model") + 1] == "claude-opus-4-6"
    assert options.count("--effort") == 1 and options[options.index("--effort") + 1] == "max"
    assert "--llm" not in options
    assert result["haiku"] == [[""], True]
    disabled, title, groups = result["opencode"]
    assert disabled and title.startswith("opencode sets its effort per model in its own configuration: ")
    assert groups == ["Cloud: Anthropic", "Cloud: OpenAI", "Cloud: Google"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_start_form_in_german(tmp_path):
    result = drive(tmp_path, "de")
    assert ["Cloud: Anthropic", 13] in result["groups"]
    assert result["opus"][1] == "der Standard des Agenten (medium für dieses Modell)"
    assert result["reset"][2] == "Der Effort xhigh wurde zurückgesetzt: claude-opus-4-6 nimmt ihn nicht."
