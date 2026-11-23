"""LLM servers for all hosts in the web interface (sandbox-ai llm share): the route POST /api/hosts/HOST/llm/NAME/share
(the command it runs there, a key newly sent to a host and unsharing confirmed first, what it refuses) and the LLM
page (app.js in node): the column For all hosts with each host's state, For all hosts… with the flag and one key tick
per host, the questions, what is sent. A copy managed from another machine has no For all hosts…"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM, logged_in, home  # noqa: E402,F401 - the fixtures of the web tests

HOSTS = [{"host": "ct1", "copied": True, "in_sync": True, "key": "sent", "key_allowed": True, "keys_sent": ["LMS_KEY"],
          "keys_missing": [], "reachable": True, "http": 200, "reach_error": None, "error": None, "offline": False},
         {"host": "ct2", "copied": True, "in_sync": True, "key": "missing", "key_allowed": False, "keys_sent": [],
          "keys_missing": ["LMS_KEY"], "reachable": False, "http": None, "reach_error": "connection refused",
          "error": None, "offline": False}]
SHARED = {"name": "lms", "kind": "vllm", "url": "http://192.168.1.20:8000", "api_key_secret": "LMS_KEY", "headers": {},
          "model": "qwen", "shared_from": None, "shared": {"key_to": ["ct1"], "hosts": HOSTS}}
COPY = {"name": "gpu", "kind": "ollama", "url": "http://192.168.1.30:11434", "api_key_secret": None, "headers": {},
        "model": None, "shared_from": "box", "shared": None, "key_here": "none"}


def answering(call, show):
    call.runner.answers[("llm", "show")] = (0, json.dumps(show), "")
    call.runner.answers[("llm", "share")] = (0, "lms: copied to ct2\n", "")
    call.runner.answers[("llm", "unshare")] = (0, "lms is not shared any more\n", "")


def test_the_route_shares_and_unshares_asking_first_where_a_key_goes_somewhere_new(logged_in):
    call = logged_in
    answering(call, SHARED)
    # The key to ct2 as well: ct2 does not have it from this share yet - confirmed first.
    status, body = call("POST", "/api/hosts/local/llm/lms/share", {"shared": True, "key_to": ["ct2", "ct1"]})
    assert status == 400 and body.get("confirm") == "llm-share-key" and "lms to ct2" in body["error"]
    assert not any(args[:2] == ["llm", "share"] for _, args in call.runner.calls)
    status, body = call("POST", "/api/hosts/local/llm/lms/share", {"shared": True, "key_to": ["ct2", "ct1"],
                                                                   "confirm": True})
    assert status == 200 and body == {"ok": True, "output": "lms: copied to ct2"}
    assert call.runner.calls[-1] == ("local", ["llm", "share", "lms", "--key-to", "ct1,ct2"])
    # The hosts it goes to already (or none): nothing to confirm.
    for key_to, args in ((["ct1"], ["--key-to", "ct1"]), ([], ["--no-key"])):
        status, body = call("POST", "/api/hosts/local/llm/lms/share", {"shared": True, "key_to": key_to})
        assert status == 200, body
        assert call.runner.calls[-1] == ("local", ["llm", "share", "lms", *args])
    # Unshared: confirmed first.
    status, body = call("POST", "/api/hosts/ct1/llm/lms/share", {"shared": False})
    assert status == 400 and body.get("confirm") == "llm-unshare"
    status, body = call("POST", "/api/hosts/ct1/llm/lms/share", {"shared": False, "confirm": True})
    assert status == 200 and call.runner.calls[-1] == ("ct1", ["llm", "unshare", "lms"])
    # A host that did not answer: said in the output, the share stands.
    call.runner.answers[("llm", "share")] = (1, "note: lms is not on ct2 as it is here yet: ct2 did not answer\n", "")
    status, body = call("POST", "/api/hosts/local/llm/lms/share", {"shared": True, "key_to": ["ct1"]})
    assert status == 200 and body["ok"] is False and "ct2 did not answer" in body["output"]


@pytest.mark.parametrize("path, data", [
    ("/api/hosts/local/llm/-x/share", {"shared": True}),
    ("/api/hosts/local/llm/lms/share", {"shared": "yes"}),
    ("/api/hosts/local/llm/lms/share", {"shared": True, "key_to": "ct1"}),
    ("/api/hosts/local/llm/lms/share", {"shared": True, "key_to": ["-oProxyCommand=x"]}),
    ("/api/hosts/local/llm/lms/share", {"shared": True, "key_to": ["local"]}),
    ("/api/hosts/local/llm/lms/share", {"shared": True, "key_to": ["ct1,ct2"]}),
])
def test_what_the_route_refuses(logged_in, path, data):
    answering(logged_in, SHARED)
    status, body = logged_in("POST", path, dict(data, confirm=True))
    assert status == 400, body
    assert not any(args[:2] in (["llm", "share"], ["llm", "unshare"]) for _, args in logged_in.runner.calls)


DRIVER = FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const rowOf = (name) => main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === name);
const LIST = __LIST__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "local"; state.page = "llm"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }, { name: "ct2", kind: "ssh", ssh: "root@ct2", port: 22 }];', context);
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  answer = (method, path, body) => {
    if (method === "GET" && path.endsWith("/llm")) return LIST;
    if (path.endsWith("/llm-status")) return [];
    if (path.endsWith("/share")) return { ok: true, output: "lms: copied to ct2" };
    if (path.endsWith("/vpn") || path.endsWith("/secrets")) return [];
    return { output: "ok" };
  };
  await vm.runInContext("pages.llm(__main)", context); await settle();
  const cell = (name) => rowOf(name).all().find(n => n.tagName === "td" && n.className === "llm-shared");
  result.headers = main.all().find(n => n.tagName === "table").all().filter(n => n.tagName === "th").map(n => n.textContent);
  result.cells = { lms: [cell("lms").textContent, cell("lms").attributes.title], gpu: [cell("gpu").textContent, cell("gpu").attributes.title] };
  const shareButton = (name) => rowOf(name).all().find(n => n.tagName === "button" && /^(For all hosts…|Für alle Hosts…)$/.test(n.textContent));
  result.copyHasButton = !!shareButton("gpu");
  shareButton("lms").fire("click"); await settle();
  result.flag = byId("llm-share-flag").checked;
  result.ticks = ["ct1", "ct2"].map(h => [h, byId(`llm-share-key-${h}`).checked, byId(`llm-share-key-${h}`).disabled]);
  result.states = byId("llm-share").all().filter(n => n.tagName === "tr" && n.attributes["data-host"]).map(r => r.children[2].textContent);
  byId("llm-share-key-ct2").checked = true;
  calls.length = 0;
  byId("llm-share-save").fire("click"); await settle();
  result.question = byId("llm-share").all().filter(n => n.tagName === "p" || n.tagName === "div").map(n => n.textContent).filter(t => /ct2\?/.test(t)).slice(-1)[0] || null;
  result.sentBeforeYes = calls.filter(c => c.method === "POST").length;
  buttonIn(byId("llm-share"), process.argv[3] === "de" ? "Schlüssel schicken" : "Send the key").fire("click"); await settle();
  result.sent = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  // off: unshare, asked first
  shareButton("lms").fire("click"); await settle();
  byId("llm-share-flag").checked = false; byId("llm-share-flag").fire("change");
  result.disabledWhenOff = byId("llm-share-key-ct1").disabled;
  calls.length = 0;
  byId("llm-share-save").fire("click"); await settle();
  buttonIn(byId("llm-share"), process.argv[3] === "de" ? "Nicht mehr teilen" : "Unshare").fire("click"); await settle();
  result.unshared = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def drive(tmp_path, language="en"):
    driver = tmp_path / f"share_{language}.js"
    driver.write_text(DRIVER.replace("__LIST__", json.dumps([SHARED, COPY])))
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_llm_page_shares_with_a_key_tick_per_host(tmp_path):
    result = drive(tmp_path)
    assert "For all hosts" in result["headers"]
    text, title = result["cells"]["lms"]
    assert text == "yes: 1 of 2 hosts ready"
    assert "ct1: copied, in sync, key LMS_KEY sent, reachable from there" in title
    assert "ct2: copied, in sync, key missing there, NOT reachable from there: connection refused" in title
    assert result["cells"]["gpu"][0] == "from box" and result["cells"]["gpu"][1].startswith("Managed from box")
    assert result["copyHasButton"] is False, "a copy is shared from where it is managed"
    assert result["flag"] is True
    assert result["ticks"] == [["ct1", True, False], ["ct2", False, False]]
    assert result["states"][1].startswith("copied, in sync, key missing there")
    assert result["sentBeforeYes"] == 0, "asked first: a key to a host that has none from this share"
    assert result["question"] and "Send the key of lms (LMS_KEY) to ct2?" in result["question"]
    assert result["sent"] == [["/api/hosts/local/llm/lms/share", {"shared": True, "key_to": ["ct1", "ct2"],
                                                                   "confirm": True}]]
    assert result["disabledWhenOff"] is True
    assert result["unshared"] == [["/api/hosts/local/llm/lms/share", {"shared": False, "confirm": True}]]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_llm_page_in_german(tmp_path):
    result = drive(tmp_path, "de")
    assert "Für alle Hosts" in result["headers"]
    assert result["cells"]["lms"][0] == "ja: 1 von 2 Hosts bereit"
    assert "von dort NICHT erreichbar: connection refused" in result["cells"]["lms"][1]
    assert result["sent"][0][1]["key_to"] == ["ct1", "ct2"]
