"""The web page's MCP Update (the user, 2026-11-23: "add mcp update"): a server whose catalog pins a newer image says
so in its state cell (the digests) and has an Update button, asked in the page first; Update all above the table when
one of them is signed; the API runs `sandbox-ai mcp update [NAME...] --json` on the host - --check without a
confirmation, the update itself only confirmed (mcp-update) and with --yes."""
import json
import shutil

import pytest

import test_web as tw
from test_web import home, logged_in  # noqa: F401  (fixtures)

OLD, NEW = "1" * 64, "2" * 64
DRIVER = tw.FAKE_DOM + r"""
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "mcp"; state.host = "ct1";', context);
  const servers = [
    { name: "radare2", status: "stopped", command: "x", serves: [], network: "none", definition: {},
      newer: { image: ["ghcr.io/c0decave/r2@sha256:__OLD__", "ghcr.io/c0decave/r2@sha256:__NEW__"], signed: true } },
    { name: "time", status: "stopped", command: "y", serves: [], network: "none", definition: {}, newer: null }];
  answer = (method, path) => (method === "GET" ? servers : { servers: [{ server: "radare2", state: "newer" }], updated: ["radare2"] });
  await vm.runInContext("pages.mcp(__main)", context); await settle();
  const rowOf = (name) => main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === name);
  result.state = rowOf("radare2").children[1].textContent;
  result.timeButtons = rowOf("time").all().filter(n => n.tagName === "button").map(n => n.textContent);
  calls.length = 0;
  button(rowOf("radare2"), "Update").fire("click"); await settle();
  result.before = calls.filter(c => c.method === "POST").length;
  result.asked = main.textContent.includes("Yes, update it");
  button(main, "Yes, update it").fire("click"); await settle();
  result.sent = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  result.all = !!button(main, "Update all");
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
""".replace("__OLD__", OLD).replace("__NEW__", NEW)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_shows_a_newer_image_and_updates_it_asked_first(tmp_path):
    result = tw.run_driver(tmp_path, DRIVER, "mcp-update")
    assert "a newer image" in result["state"] and "111111111111 → 222222222222" in result["state"]
    assert "Update" not in result["timeButtons"], "its catalog pins the image it runs"
    assert result["before"] == 0 and result["asked"], "asked in the page first"
    assert result["sent"] == [["/api/hosts/ct1/mcp-update", {"names": ["radare2"], "unsigned": False, "confirm": True}]]
    assert result["all"]


def test_the_api_checks_without_and_updates_only_with_a_confirmation(logged_in):
    printed = json.dumps({"servers": [{"server": "radare2", "state": "newer"}], "updated": []})
    logged_in.runner.answers[("mcp", "update")] = (0, printed, "")
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-update", {"names": ["radare2"], "check": True})
    assert status == 200, out
    assert logged_in.runner.calls[-1][1][-4:] == ["mcp", "update", "radare2", "--json", "--check"][-4:]
    assert "--yes" not in logged_in.runner.calls[-1][1]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-update", {"names": ["radare2"]})
    assert status == 400 and "mcp-update" in json.dumps(out)
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-update", {"names": ["radare2"], "unsigned": True,
                                                                  "confirm": True})
    assert status == 200, out
    assert logged_in.runner.calls[-1][1][-5:] == ["update", "radare2", "--json", "--unsigned", "--yes"]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-update", {"names": ["../x"], "confirm": True})
    assert status == 400
