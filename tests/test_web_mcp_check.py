"""The web page's MCP Check (the user, 2026-11-25: every server of a host checked in minutes): a Check button in each
row and Check all below the table, nothing asked first (it changes nothing); the API runs `sandbox-ai mcp check
[NAME...|--all] --json` on the host and gives its rows - a failed server is a row (exit status 1), not a failure of
the call; the Check column shows ok with its tools, or failed with the reason and the fix; a server's last failure
(mcp list's last_problem) is in its state cell with its log."""
import json
import shutil

import pytest

import test_web as tw
from test_web import home, logged_in  # noqa: F401  (fixtures)

DRIVER = tw.FAKE_DOM + r"""
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "mcp"; state.host = "ct1";', context);
  const servers = [
    { name: "gowitness", status: "stopped", command: "x", serves: [], network: "full", definition: {},
      last_problem: { time: "2026-11-25T09:00:00Z", what: "w", why: "exit status 126: Error: crun", fix: "doctor",
                      log: "/h/.local/state/sandbox-ai/mcp/gowitness/supervisor.log" } },
    { name: "time", status: "stopped", command: "y", serves: [], network: "none", definition: {} }];
  const rows = [
    { server: "gowitness", result: "failed", reason: "the MCP server ended before it answered (exit status 126)", fix: "sandbox-ai doctor" },
    { server: "time", result: "ok", tools: 2, seconds: 1.5 }];
  answer = (method, path, body) => (method === "GET" ? servers : rows.filter(r => !JSON.parse(body).names.length || JSON.parse(body).names.includes(r.server)));
  await vm.runInContext("pages.mcp(__main)", context); await settle();
  const rowOf = (name) => main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === name);
  result.state = rowOf("gowitness").children[1].textContent;
  calls.length = 0;
  button(main, "Check all").fire("click"); await settle();
  result.sent = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  result.bad = rowOf("gowitness").children[6].textContent;
  result.good = rowOf("time").children[6].textContent;
  calls.length = 0;
  button(rowOf("time"), "Check").fire("click"); await settle();
  result.one = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_checks_all_and_one_and_shows_the_last_failure(tmp_path):
    result = tw.run_driver(tmp_path, DRIVER, "mcp-check")
    assert "last failure 2026-11-25T09:00:00Z" in result["state"] and "exit status 126: Error: crun" in result["state"]
    assert "Its log: /h/.local/state/sandbox-ai/mcp/gowitness/supervisor.log" in result["state"]
    assert result["sent"] == [["/api/hosts/ct1/mcp-check", {"names": []}]], "nothing asked first"
    assert result["bad"].startswith("failed") and "exit status 126" in result["bad"] and "Fix: sandbox-ai doctor" in \
        result["bad"]
    assert result["good"] == "ok, 2 tools, 1.5 s"
    assert result["one"] == [["/api/hosts/ct1/mcp-check", {"names": ["time"]}]]


def test_the_api_runs_mcp_check_and_takes_its_rows_whatever_failed(logged_in):
    printed = json.dumps([{"server": "gowitness", "result": "failed", "reason": "x"}])
    logged_in.runner.answers[("mcp", "check")] = (1, printed, "")
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-check", {})
    assert status == 200 and out == json.loads(printed), out
    assert logged_in.runner.calls[-1][1][-4:] == ["mcp", "check", "--all", "--json"]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-check", {"names": ["time", "gowitness"], "vpn": "tor"})
    assert status == 200
    assert logged_in.runner.calls[-1][1][-6:] == ["mcp", "check", "time", "gowitness", "--json", "--vpn=tor"]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-check", {"names": ["../x"]})
    assert status == 400
    logged_in.runner.answers[("mcp", "check")] = (2, "", "sandbox-ai mcp: Unknown MCP server: x")
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-check", {"names": ["x"]})
    assert status != 200
