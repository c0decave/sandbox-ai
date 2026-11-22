"""The web interface's running start: its Resources card lists the start's toolchains (GET
/api/hosts/HOST/toolchains/NAME: `sandbox-ai toolchain list --run NAME --json` there) and offers what that host's
store has (GET /api/hosts/HOST/toolchains) minus those; Add toolchain asks first and names the registries it opens;
Remove takes one added during the run, and a toolchain still in use is detached only after a second question."""
import json
import shutil

import pytest

from test_web import FAKE_DOM, run_driver

STORE = [{"name": "go", "version": "1.27.3", "commands": ["go", "gofmt"], "hosts": ["proxy.golang.org"], "allow": ["github"]},
         {"name": "jdk", "version": "21.0.13+9", "commands": ["java", "mvn"], "hosts": ["repo.maven.apache.org"], "allow": []}]
HELD = [{"name": "jdk", "version": "21.0.13+9", "source": "added", "commands": ["java", "mvn"]},
        {"name": "zig", "version": "0.17.1", "source": "start", "commands": ["zig"]}]

DRIVER = FAKE_DOM + r"""
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const byId = (id) => document.getElementById(id);
const STORE = __STORE__, HELD = __HELD__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.page = "logs"; state.hosts = [{ name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  let busyOnce = true;
  answer = (method, path, sent) => {
    if (path === "/api/hosts/ct1/toolchains") return STORE;
    if (path === "/api/hosts/ct1/toolchains/web" && method === "GET") return HELD;
    if (path === "/api/hosts/ct1/starts") return [{ name: "web", id: "web-1", kind: "session", state: "running", directory: "/d" }];
    if (path === "/api/hosts/ct1/toolchains/web/remove" && busyOnce && !JSON.parse(sent || "{}").force) {
      busyOnce = false;
      throw new Error("sandbox-ai toolchain: could not detach jdk: /opt/toolchains/jdk is in use in the sandbox: --force detaches it anyway");
    }
    if (method === "GET") return path.includes("/limits/") ? {} : [];
    return { output: "done" };
  };
  vm.runInContext('resourcesCard(__main, { name: "web", id: "web-1", host: "ct1", state: "running", kind: "session", directory: "/d", keep: true })', context);
  await settle();
  const card = byId("resources-card"), select = byId("tc-choice");
  result.asked = calls.filter(c => c.method === "GET" && c.path.includes("/toolchains")).map(c => c.path).sort();
  result.offered = select.children.map(o => [o.value, o.textContent]);
  result.rows = card.all().filter(n => n.tagName === "tr" && n.all().some(c => c.textContent === "at the start" || c.textContent === "during the run"))
    .map(r => r.children.map(c => c.textContent));
  button(card, "Add toolchain").fire("click");
  result.none = posts().length;
  select.value = "go"; select.fire("change");
  button(card, "Add toolchain").fire("click");
  result.question = card.all().map(n => n.textContent).find(text => text.startsWith("Mount go read-only into web"));
  button(card, "Yes, mount it").fire("click"); await settle();
  const remove = card.all().filter(n => n.tagName === "button" && n.textContent === "Remove").pop();
  remove.fire("click"); await settle();
  result.force = card.all().map(n => n.textContent).find(text => text.startsWith("jdk is still in use in web"));
  button(card, "Yes, detach it").fire("click"); await settle();
  result.posts = posts();
  vm.runInContext('setLanguage("de")', context);
  result.german = vm.runInContext('[t("Add toolchain"), t("Yes, mount it"), t("In the store of {host}", { host: "ct1" })]', context);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_resources_card_lists_attaches_and_detaches_toolchains(tmp_path):
    source = DRIVER.replace("__STORE__", json.dumps(STORE)).replace("__HELD__", json.dumps(HELD))
    result = run_driver(tmp_path, source, "toolchains")
    assert result["asked"][:2] == ["/api/hosts/ct1/toolchains", "/api/hosts/ct1/toolchains/web"]
    assert result["offered"] == [["", "Choose a toolchain…"], ["go", "go 1.27.3 (go, gofmt)"]], "what it has is not offered"
    assert [row[:3] for row in result["rows"]] == [["jdk", "21.0.13+9", "during the run"],
                                                   ["zig", "0.17.1", "at the start"]]
    assert result["none"] == 0, "nothing chosen: nothing sent"
    assert "at /opt/toolchains/go" in result["question"]
    assert "Its registries are opened for the run: proxy.golang.org, --allow-github" in result["question"]
    assert "stays usable until it is closed" in result["force"]
    assert result["posts"] == [
        ["/api/hosts/ct1/toolchains/web", {"toolchain": "go", "confirm": True}],
        ["/api/hosts/ct1/toolchains/web/remove", {"toolchain": "jdk"}],
        ["/api/hosts/ct1/toolchains/web/remove", {"toolchain": "jdk", "force": True, "confirm": True}]]
    assert result["german"] == ["Toolchain hinzufügen", "Ja, einhängen", "Im Lager von ct1"]
