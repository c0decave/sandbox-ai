"""The web page's MCP add form shows each folder's default (user 2026-11-22: "alle mcp server sollten bzgl. /work pfad
defaults haben, sodass sie simpel und fast hinzugefügt werden können"): Add… on an entry fills in its folders as
TARGET=default (the start's workspace for /work, a folder of the server's own for the others, with the mode its entry
declares), each said as "Folder on HOST for the container's TARGET"; the API takes default as a folder, and a default
is no writable folder of yours to be asked about."""
import shutil

import pytest

import test_web as tw
from test_web import home, logged_in  # noqa: F401  (fixtures)

DRIVER = tw.FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "mcp"; state.host = "ct1";', context);
  const catalog = { problems: [], entries: [
    { entry: "hub:binwalk", prefix: "hub", name: "binwalk", network: "none", title: "binwalk", published: true, signature: "good", env: [],
      mounts: [{ target: "/work", required: true, description: "the firmware", default: "workspace", mode: "ro" },
               { target: "/out", required: false, description: "extracted files", default: "own", mode: "rw" }] },
    { entry: "hub:old", prefix: "hub", name: "old", network: "none", title: "old", published: true, signature: "good", env: [],
      mounts: [{ target: "/work", required: true, description: "an older host's: no default" }] }] };
  answer = (method, path) => (method === "GET" ? [] : path.endsWith("/mcp-catalog") ? catalog : { output: "added" });
  await vm.runInContext("pages.mcp(__main)", context); await settle();
  button(main, "Show the catalog").fire("click"); await settle();
  const rowOf = (entry) => main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === entry);
  button(rowOf("hub:old"), "Add…").fire("click"); await settle();
  result.old = [byId("mcp-mounts").value, byId("mcp-mounts-help").all().filter(n => n.tagName === "li").map(n => n.textContent)];
  button(rowOf("hub:binwalk"), "Add…").fire("click"); await settle();
  result.mounts = byId("mcp-mounts").value;
  result.folders = byId("mcp-mounts-help").all().filter(n => n.tagName === "li").map(n => n.textContent);
  for (const id of ["mcp-as", "mcp-memory", "mcp-pids", "mcp-catalogs", "mcp-add-vpn", "mcp-env"]) byId(id).value = "";
  calls.length = 0;
  button(main, "Add the server").fire("click"); await settle();
  result.sent = calls.filter(c => c.method === "POST").map(c => JSON.parse(c.body).mounts);
  result.asked = main.textContent.includes("Yes, add it");
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
function __main_of() { return main; }
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_add_form_fills_in_each_folders_default(tmp_path):
    result = tw.run_driver(tmp_path, DRIVER, "mcp-folder-defaults")
    assert result["mounts"] == "/work=default:ro\n/out=default:rw"
    assert result["folders"] == [
        "Folder on ct1 for the container's /work: default - the start's workspace (read-only) - the firmware",
        "Folder on ct1 for the container's /out: default - its own folder, made by sandbox-ai (writable) - "
        "extracted files"]
    assert result["sent"] == [[{"target": "/work", "host": "default", "mode": "ro"},
                               {"target": "/out", "host": "default", "mode": "rw"}]]
    assert result["asked"] is False, "a default is no writable folder of yours: not asked about"
    assert result["old"][0] == "", "a host that says no defaults: nothing filled in"
    assert result["old"][1] == ["Folder on ct1 for the container's /work (required) - an older host's: no default"]


def test_the_api_takes_default_as_a_folder(logged_in):
    logged_in.runner.answers[("mcp", "list")] = (0, "[]", "")
    logged_in.runner.answers[("mcp", "add")] = (0, "sandbox-ai mcp: binwalk added\n", "")
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", {"entry": "hub:binwalk", "mounts": [
        {"target": "/work", "host": "default", "mode": "ro"}, {"target": "/out", "host": "default", "mode": "rw"}]})
    assert status == 200, out
    assert "--mount=/work=default:ro" in logged_in.runner.calls[-1][1]
    assert "--mount=/out=default:rw" in logged_in.runner.calls[-1][1]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", {"entry": "hub:binwalk", "mounts": [
        {"target": "/work", "host": "defaults", "mode": "ro"}]})
    assert status == 400
