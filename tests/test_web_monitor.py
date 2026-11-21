"""The system monitor in the web interface: two fixed commands (sandbox-ai top --json --no-measure, sandbox-ai stats
usage --json --series --by run), and the page under node: the runs grouped by project with their trend, the host's
line, the history, the warnings with what helps - raising a limit and stopping an idle run, each asked first in
place and sent with the confirmation."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "sandbox" / "web"
spec = importlib.util.spec_from_file_location("test_web_for_monitor", ROOT / "tests" / "test_web.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)
w = T.w
GIB = 1 << 30
TOP = {"time": 1, "sort": "cpu", "host": {"sample": {"cpu_pct": 42.0, "cpus": 8, "load1": 1.5, "mem_total": 32 * GIB,
                                                     "mem_avail": 20 * GIB}, "warnings": []},
       "runs": [{"run": "claude-web1", "name": "web", "agent": "claude", "kind": "agent", "project": "/p/web",
                 "parent": None,
                 "sample": {"cpu": 1.5, "cpu_pct": 75.0, "mem": int(7.6 * GIB), "mem_lim": 8 * GIB, "rd": 2048,
                            "wr": 4096, "rx": 100, "tx": 50, "pids": 40},
                 "spark": [[i, 0.1 * i, GIB * i, 0, 0] for i in range(1, 10)],
                 "warnings": [{"kind": "memory", "level": "warning", "since": 1800000000,
                               "text": "web uses 7.6 of 8.0 GiB (95 %)", "advice": "raise --memory to 12g: peak 7.8 "
                               "of 8.0 GiB with 3 throttling events",
                               "argv": ["limits", "claude-web1", "--memory", "12g"]}]},
                {"run": "mcp-git", "name": "git", "agent": None, "kind": "mcp", "project": "/p/web", "parent": "web",
                 "sample": {"cpu": 0.2, "mem": GIB, "pids": 3}, "spark": [], "warnings": []},
                {"run": "codex-api1", "name": "api", "agent": "codex", "kind": "agent", "project": "/p/api",
                 "parent": None,
                 "sample": {"cpu": 0.0, "mem": 3 * GIB, "pids": 5}, "spark": [],
                 "warnings": [{"kind": "idle", "level": "warning", "since": 1800000000,
                               "text": "api has been idle for 2.0 h and holds 3.2 GiB",
                               "advice": "stop idle run api: 3.2 GiB for 2.0 h", "argv": ["stop", "api"]}]}]}
USAGE = {"runs": [{"run": "claude-web1", "name": "web", "series": [[i * 60, 1.5, 6 * GIB, 0, 0] for i in range(30)]}],
         "host": {"series": [[i * 60, 40.0, 20 * GIB, 0, 1.0] for i in range(30)]}}


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setattr(w, "SCRYPT", T.FAST)
    secret = T.add_user()
    runner = T.FakeRunner({("top",): (0, json.dumps(TOP), ""), ("stats",): (0, json.dumps(USAGE), "")})
    svc = T.service(runner)
    _, headers, body = T.login(svc, secret)
    ok = {**T.HEADERS, "Cookie": T.cookie_of(headers), "X-CSRF-Token": body["csrf"]}

    def call(method, path, data=None):
        status, _, out = svc.handle(method, path, ok, json.dumps(data).encode() if data is not None else b"",
                                    "192.0.2.1")
        return status, json.loads(out)
    call.runner = runner
    return call


def test_two_fixed_commands(logged_in):
    status, out = logged_in("GET", "/api/hosts/ct1/monitor")
    assert status == 200 and out == TOP
    assert logged_in.runner.calls[-1] == ("ct1", ["top", "--json", "--no-measure"])
    status, out = logged_in("GET", "/api/hosts/ct1/monitor/usage/24h")
    assert status == 200 and out == USAGE
    assert logged_in.runner.calls[-1] == ("ct1", ["stats", "usage", "--since", "24h", "--json", "--series", "--by",
                                                  "run"])
    calls = len(logged_in.runner.calls)
    for bad in ("--all", "24x", "1%20h", "99999d"):
        assert logged_in("GET", f"/api/hosts/ct1/monitor/usage/{bad}")[0] in (400, 404)
    assert len(logged_in.runner.calls) == calls, "nothing refused reaches the host"
    logged_in.runner.answers[("top",)] = (2, "", "invalid choice: 'top'")
    status, out = logged_in("GET", "/api/hosts/ct1/monitor")
    assert status == 502 and "invalid choice" in out["error"]


DRIVER = T.FAKE_DOM + r"""
(async () => {
  const result = {};
  const TOP = JSON.parse(process.argv[3]), USAGE = JSON.parse(process.argv[4]);
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "monitor"; state.host = "ct1";', context);
  answer = (method, path) => (path.endsWith("/monitor") ? TOP : path.includes("/monitor/usage/") ? USAGE : { output: "ok" });
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  const cells = (row) => row.children.map(c => c.textContent);
  const rows = main.all().filter(n => n.tagName === "tr");
  result.heading = main.all().find(n => n.tagName === "h1").textContent;
  result.host = main.all().find(n => n.className === "monitor-host").textContent;
  result.projects = rows.filter(r => r.className === "monitor-project").map(r => r.textContent);
  result.runs = rows.filter(r => r.children.length === 11 && r.children[0].tagName === "td").map(r => cells(r).slice(0, 7));
  result.trends = main.all().filter(n => n.tagName === "svg").length;
  result.history = main.all().filter(n => n.tagName === "p").map(n => n.textContent).find(t => t.includes("the last")) || "";
  const warnings = rows.filter(r => (r.className || "").startsWith("monitor-warning"));
  result.warnings = warnings.map(r => cells(r).slice(1, 5));
  // Raise: asked in place, sent with the confirmation
  button(warnings[0], "Raise to 12g").fire("click");
  result.asked = warnings[0].all().find(n => n.className === "confirm").textContent;
  calls.length = 0;
  button(warnings[0], "Yes, raise it").fire("click"); await settle();
  result.raised = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  const again = main.all().filter(n => (n.className || "").startsWith("monitor-warning"));
  button(again[1], "Stop").fire("click");
  calls.length = 0;
  button(again[1], "Yes, stop it").fire("click"); await settle();
  result.stopped = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  // choosing a run: its history
  const web = main.all().find(n => n.tagName === "button" && n.textContent === "web");
  web.fire("click"); await settle();
  result.chosen = main.all().filter(n => n.tagName === "p").map(n => n.textContent).find(t => t.includes("the last")) || "";
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_groups_by_project_and_acts_after_asking(tmp_path):
    driver = tmp_path / "monitor.js"
    driver.write_text(DRIVER)
    import subprocess
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), json.dumps(TOP), json.dumps(USAGE)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["heading"] == "System monitor on ct1"
    assert "CPU 42 % of 8" in result["host"] and "memory free 20.0 of 32.0 GiB" in result["host"]
    assert result["projects"] == ["web", "api"]
    assert result["runs"] == [["web", "claude", "1.50 (75 %)", "7.6 / 8.0 GiB", "2k/s / 4k/s", "100 B/s / 50 B/s",
                               "40"], ["↳ git", "", "0.20 (0 %)", "1.0 GiB", "-", "-", "3"],
                              ["api", "codex", "0.00 (0 %)", "3.0 GiB", "-", "-", "5"]]
    assert result["trends"] >= 4  # each run's trend and the host's history
    assert result["history"] == "ct1: CPU busy and free memory, the last 24 hours"
    assert result["warnings"][0][1:] == ["memory near the limit", "web uses 7.6 of 8.0 GiB (95 %)",
                                         "raise --memory to 12g: peak 7.8 of 8.0 GiB with 3 throttling events"]
    assert result["warnings"][1][1] == "idle, holding memory"
    assert "Raise --memory of web on ct1 to 12g" in result["asked"]
    assert result["raised"] == [["/api/hosts/ct1/limits/claude-web1", {"memory": "12g", "confirm": True}]]
    assert result["stopped"] == [["/api/hosts/ct1/starts/api/stop", {"confirm": True}]]
    assert result["chosen"] == "web: CPUs and memory, the last 24 hours"


def test_the_page_is_listed_and_draws_no_markup():
    script = (WEB / "app.js").read_text()
    assert '["monitor", N_("System monitor")]' in script and '"monitor"' in script.split("const HOST_PAGES")[1][:400]
    page = script.split("pages.monitor = ")[1].split("pages.canaries")[0]
    assert ".innerHTML" not in page and "confirm(" not in page.replace("confirmIn(", "").replace("confirm: true", "")


RACE = T.FAKE_DOM + r"""
(async () => {
  const TOP = JSON.parse(process.argv[3]), USAGE = JSON.parse(process.argv[4]);
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "monitor"; state.host = "ct1";', context);
  let late = null;
  answer = (method, path) => {
    if (path.endsWith("/monitor")) return TOP;
    if (path.endsWith("/monitor/usage/7d")) return new Promise(r => { late = () => r({ runs: [], host: { series: [] } }); });
    return USAGE;
  };
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  const select = main.all().find(n => n.tagName === "select");
  select.value = "7d"; select.fire("change"); await settle();
  select.value = "30d"; select.fire("change"); await settle();
  late(); await settle();  // the 7 days' answer comes last
  const texts = main.all().filter(n => n.tagName === "p").map(n => n.textContent);
  console.log(JSON.stringify({ caption: texts.find(t => t.includes("the last")) || "",
                               empty: texts.some(t => t.includes("No history recorded")) }));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_late_answer_for_an_earlier_window_does_not_replace_the_chosen_one(tmp_path):
    """Review 24h B: 7 days, then 30 days chosen; the 7 days' answer (slower: more to read) came last and was drawn
    under the caption "the last 30 days". Only the window chosen last is drawn."""
    driver = tmp_path / "race.js"
    driver.write_text(RACE)
    import subprocess
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), json.dumps(TOP), json.dumps(USAGE)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["caption"] == "ct1: CPU busy and free memory, the last 30 days"
    assert result["empty"] is False, "the 7 days' (empty) history replaced the 30 days' one"


HINT = T.FAKE_DOM + r"""
(async () => {
  const TOP = JSON.parse(process.argv[3]), USAGE = JSON.parse(process.argv[4]);
  for (const row of TOP.runs) row.interval = 30;
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "monitor"; state.host = "ct1";', context);
  answer = (method, path) => (path.endsWith("/monitor") ? TOP : USAGE);
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  console.log(JSON.stringify(main.all().filter(n => n.className === "hint").map(n => n.textContent)));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_hint_says_the_interval_the_runs_are_sampled_at(tmp_path):
    """Review 24h B: with monitor_interval 30 the page said "recorded every 10 seconds"."""
    driver = tmp_path / "hint.js"
    driver.write_text(HINT)
    import subprocess
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), json.dumps(TOP), json.dumps(USAGE)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    hints = json.loads(run.stdout)
    assert any("recorded every 30 seconds" in hint for hint in hints), hints


SPEND = {"run": "claude-web1", "name": "web", "requests": 12, "usd": 4.25, "unknown": 1, "unknown_models": ["mystery"],
         "fallback": 2, "fallback_models": ["brand-new"], "fallback_price": {"in": 20, "out": 120},
         "subscription": 0, "local": 0, "refused": True,
         "budgets": [{"id": "run", "label": "budget of this run (--budget)", "usd": 5, "used": 4.25, "share": 0.85}]}
SPEND_DRIVER = T.FAKE_DOM + r"""
(async () => {
  const TOP = JSON.parse(process.argv[3]), USAGE = JSON.parse(process.argv[4]), SPEND = JSON.parse(process.argv[5]);
  USAGE.spend = { total: { requests: 12, usd: 4.25 }, runs: { "claude-web1": { usd: 4.25, requests: 12, unknown: 1, subscription: 0 } } };
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "monitor"; state.host = "ct1";', context);
  answer = (method, path) => (path.endsWith("/monitor") ? TOP : path.includes("/monitor/usage/") ? USAGE
    : path.endsWith("/spend/claude-web1") ? SPEND : { output: "raised" });
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  const result = {};
  const rows = main.all().filter(n => n.tagName === "tr" && n.children.length === 11 && n.children[0].tagName === "td");
  result.cost = rows.map(r => r.children[9].textContent);
  main.all().find(n => n.tagName === "button" && n.textContent === "web").fire("click"); await settle();
  result.panel = main.all().filter(n => n.className === "spend-panel").map(n => n.textContent).join(" ");
  result.bars = main.all().filter(n => n.tagName === "progress").map(n => n.value);
  const panel = main.all().find(n => n.className === "spend-panel");
  panel.all().find(n => n.tagName === "input").value = "2.5";
  calls.length = 0;
  button(panel, "Raise budget…").fire("click");
  result.asked = panel.all().find(n => n.className === "confirm").textContent;
  result.before = calls.filter(c => c.method === "POST").length;
  button(panel, "Yes, raise it").fire("click"); await settle();
  result.raised = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_cost_of_each_run_its_budget_bar_and_a_raise_asked_first(tmp_path):
    """Spend limits: each running run's cost (stats usage's spend), the chosen run's cost so far with a bar per budget
    (sandbox-ai spend run), and Raise budget - asked in place, sent with the confirmation."""
    driver = tmp_path / "spend.js"
    driver.write_text(SPEND_DRIVER)
    import subprocess
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), json.dumps(TOP), json.dumps(USAGE),
                          json.dumps(SPEND)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["cost"][0] == "USD 4.25 + 1 of unknown cost" and result["cost"][1] == "-"
    assert "USD 4.25 + 1 of unknown cost so far - 12 model requests" in result["panel"]
    assert "85 % of USD 5.00 - budget of this run (--budget)" in result["panel"]
    assert "A budget is used up" in result["panel"] and "No price for mystery" in result["panel"]
    assert ("2 request(s) at the fallback price USD 20.00 in / USD 120.00 out per million tokens: no price for "
            "brand-new.") in result["panel"]
    assert result["bars"] == [85]
    assert "Let web on ct1 spend USD 2.5 more than its budgets?" in result["asked"] and result["before"] == 0
    assert result["raised"] == [["/api/hosts/ct1/spend/claude-web1/raise", {"usd": "2.5", "confirm": True}]]


def test_the_spend_routes(logged_in):
    logged_in.runner.answers[("spend",)] = (0, json.dumps(SPEND), "")
    status, out = logged_in("GET", "/api/hosts/ct1/spend/claude-web1")
    assert status == 200 and out == SPEND
    assert logged_in.runner.calls[-1] == ("ct1", ["spend", "run", "claude-web1", "--json"])
    calls = len(logged_in.runner.calls)
    status, out = logged_in("POST", "/api/hosts/ct1/spend/claude-web1/raise", {"usd": "3"})
    assert status == 400 and out.get("confirm") == "spend-raise"
    for bad in ({"usd": "-1", "confirm": True}, {"usd": "many", "confirm": True}, {"usd": True, "confirm": True},
                {"confirm": True}):
        assert logged_in("POST", "/api/hosts/ct1/spend/claude-web1/raise", bad)[0] == 400
    assert logged_in("POST", "/api/hosts/ct1/spend/a%20b/raise", {"usd": 1, "confirm": True})[0] in (400, 404)
    assert len(logged_in.runner.calls) == calls, "nothing refused reaches the host"
    status, _ = logged_in("POST", "/api/hosts/ct1/spend/claude-web1/raise", {"usd": 3, "confirm": True})
    assert status == 200 and logged_in.runner.calls[-1] == ("ct1", ["spend", "raise", "claude-web1", "3"])


LOGS_SPEND = T.FAKE_DOM + r"""
(async () => {
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "logs";', context);
  answer = (method, path) => path.endsWith("/starts") ? [{ name: "web", kind: "run", state: "running", workspace: "/srv/web" }]
    : path.endsWith("/logs") ? { directory: "/d", telemetry: { spend: { total: { cost: "USD 3.50 + subscription" },
        models: [{ key: "claude-sonnet-4-5", cost: "USD 3.00", requests: 1 }, { key: "claude-opus-4-5", cost: "subscription", requests: 2 }] } } }
    : {};
  await vm.runInContext("pages.logs(__main)", context); await settle();
  const box = main.all().find(n => n.className === "spend-summary");
  console.log(JSON.stringify({ text: box ? box.textContent : "", rows: box ? box.all().filter(n => n.tagName === "tr").length : 0 }));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_logs_page_says_what_the_models_cost(tmp_path):
    driver = tmp_path / "logs.js"
    driver.write_text(LOGS_SPEND)
    import subprocess
    run = subprocess.run(["node", str(driver), str(WEB / "app.js")], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert "In all: USD 3.50 + subscription (sandbox-ai spend)" in result["text"]
    assert "claude-sonnet-4-5" in result["text"] and result["rows"] >= 2


GOTO_DRIVER = T.FAKE_DOM + r"""
(async () => {
  const TOP = JSON.parse(process.argv[3]), USAGE = JSON.parse(process.argv[4]);
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "monitor"; state.host = "ct1"; ' +
    'state.hosts = [{ name: "local" }, { name: "ct1" }]; state.startsHost = "local"; var __opened = []; ' +
    'openTerminal = (params, title) => { __opened.push([params, title]); };', context);
  let STARTS = [{ name: "web", id: "web", kind: "session", state: "running", attachable: true, run: "claude-web1", agent: "claude" },
    { name: "api", kind: "run", state: "running", attachable: false, attach_why: "foreground", run: "codex-api1", agent: "codex" }];
  answer = (method, path) => (path.endsWith("/monitor") ? TOP : path.includes("/monitor/usage/") ? USAGE
    : path === "/api/hosts/ct1/starts" ? STARTS : path.endsWith("/presets") ? [] : { output: "ok" });
  const runRow = (name) => main.all().find(n => n.tagName === "tr" && n.children.length === 11 && n.children[0].tagName === "td" && n.children[0].textContent.endsWith(name));
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  result.buttons = ["web", "git", "api"].map(n => runRow(n).children[10].all().filter(b => b.tagName === "button").map(b => b.textContent));
  result.label = button(runRow("web"), "Terminal").attributes["aria-label"];
  button(runRow("web"), "Terminal").fire("click"); await settle();
  result.opened = vm.runInContext("__opened", context);
  button(runRow("api"), "Terminal").fire("click"); await settle();
  result.notAttachable = toasts().join(" | ");
  result.openedAfter = vm.runInContext("__opened.length", context);
  button(runRow("web"), "Session").fire("click"); await settle();
  result.page = vm.runInContext("[state.page, state.startsHost]", context);
  const web = body.all().find(n => n.attributes["data-name"] === "web");
  result.marked = [(web.className || "").split(" ").includes("focused"), globalThis.focused === web];
  // a run that ended meanwhile: not listed on the starts page - said, nothing marked
  vm.runInContext('state.page = "monitor";', context);
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  STARTS = STARTS.filter(s => s.name !== "api");
  button(runRow("api"), "Session").fire("click"); await settle();
  result.ended = toasts().join(" | ");
  result.endedMarked = body.all().filter(n => (n.className || "").split(" ").includes("focused")).length;
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_each_run_leads_to_its_terminal_and_its_row_on_the_starts_page(tmp_path):
    """As the desktop app's right click on a run (Go to Terminals, Go to Sessions): Terminal attaches it in a tab of
    the page (one per start: openTerminal shows an open one) or says why it has no terminal; Session shows the starts
    page of its host with its row marked and focused, or says it is not listed. An MCP server's row has neither."""
    driver = tmp_path / "goto.js"
    driver.write_text(GOTO_DRIVER)
    import subprocess
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), json.dumps(TOP), json.dumps(USAGE)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["buttons"] == [["Terminal", "Session"], [], ["Terminal", "Session"]]
    assert result["label"] == "Terminal web on ct1"
    assert result["opened"] == [[{"kind": "attach", "name": "web", "host": "ct1"}, "web"]]
    assert result["openedAfter"] == 1 and "api on ct1 has no terminal to attach to: runs in the foreground" in \
        result["notAttachable"]
    assert result["page"] == ["starts", "ct1"], "the run's host chosen on the starts page"
    assert result["marked"] == [True, True]
    assert "api is not listed among the starts of ct1" in result["ended"] and result["endedMarked"] == 0


CHART_TOP = json.loads(json.dumps(TOP))
CHART_TOP["runs"][0]["sample"].update(cpu_lim=2, pids_lim=100)
CHART_TOP["host"]["sample"].update(swap_total=4 * GIB, swap_used=GIB, rx=10, tx=20, pids=900)
CHART_USAGE = {
    "runs": [{"run": "claude-web1", "name": "web", "series": [[400 + i * 10, 1.0, 6 * GIB, 0, 0] for i in range(60)],
              "detail": {"t": [400 + i * 10 for i in range(60)], "cpu": [1.0] * 60, "cpu_lim": [2] * 60,
                         "mem": [6 * GIB] * 60, "mem_lim": [8 * GIB] * 60, "rd": [1000] * 60, "wr": [2000] * 60,
                         "rx": [300] * 60, "tx": [400] * 60, "pids": [40] * 30 + [None] * 10 + [41] * 20,
                         "pids_lim": [100] * 60}}],
    "host": {"series": [[400 + i * 10, 30.0, 20 * GIB, 0, 1.0] for i in range(60)],
             "detail": {"t": [400 + i * 10 for i in range(60)], "cpu_pct": [30.0] * 60, "mem_total": [32 * GIB] * 60,
                        "mem_avail": [20 * GIB] * 60, "rx": [70] * 60, "tx": [80] * 60, "pids": [900] * 60}}}
CHARTS = T.FAKE_DOM + r"""
(async () => {
  const TOP = JSON.parse(process.argv[3]), USAGE = JSON.parse(process.argv[4]);
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "monitor"; state.host = "ct1";', context);
  answer = (method, path) => (path.endsWith("/monitor") ? TOP : path.includes("/monitor/usage/") ? USAGE : { requests: 0, budgets: [] });
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  const charts = () => main.all().filter(n => n.tagName === "svg" && n.attributes["data-chart"]);
  const chart = (name) => charts().find(n => n.attributes["data-chart"] === name);
  const lines = (name) => chart(name).all().filter(n => n.attributes["data-line"] && n.tagName === "polyline").map(n => n.attributes["data-line"]);
  const gauges = () => main.all().filter(n => n.attributes.role === "meter").map(n => [n.attributes["aria-label"], n.className]);
  const result = { host: {}, run: {} };
  result.host.names = charts().map(n => n.attributes["data-chart"]);
  result.host.network = lines("network");
  result.host.memory = lines("memory");
  result.host.gauges = gauges();
  result.sparks = main.all().filter(n => n.tagName === "svg" && (n.attributes.class || "").startsWith("cell-spark")).map(n => n.attributes.class);
  main.all().find(n => n.tagName === "button" && n.textContent === "web").fire("click"); await settle();
  result.run.memory = lines("memory");
  result.run.io = lines("io");
  result.run.processes = chart("processes").all().filter(n => n.tagName === "polyline" && n.attributes["data-line"] === "processes and threads").length;
  result.run.gauges = gauges();
  result.run.label = chart("memory").attributes["aria-label"];
  chart("memory").fire("mousemove", { offsetX: 479 });
  result.run.tip = main.all().find(n => n.tagName === "figure" && n.attributes["data-tip"] && n.children[0] === chart("memory")).attributes["data-tip"];
  result.run.stacked = chart("all-mem").all().filter(n => n.tagName === "polygon" && n.attributes.class === "chart-stack").length;
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_charts_gauges_and_sparklines(tmp_path):
    """User 2026-11-20 (charts for CPU, memory, IO, network, processes): the web's monitor has the app's - the host's
    charts when no run is chosen (its network too), the run chosen's with the 92 % slow-down and the limit, a gap where
    samples are missing, the exact values at the time pointed at, every run stacked, gauges in the warnings' colours
    and sparklines in the CPU and memory cells."""
    driver = tmp_path / "charts.js"
    driver.write_text(CHARTS)
    import subprocess
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), json.dumps(CHART_TOP), json.dumps(CHART_USAGE)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["host"]["names"] == ["cpu", "memory", "io", "network", "processes", "all-cpu", "all-mem"]
    assert result["host"]["network"] == ["received", "sent"]
    assert result["host"]["memory"][:3] == ["used", "low (warning)", "total"]
    assert ["Memory: 12.0 of 32.0 GiB used, 20.0 GiB free", "gauge level-ok"] in result["host"]["gauges"]
    assert ["Swap: 1.0 of 4.0 GiB", "gauge level-ok"] in result["host"]["gauges"]
    assert result["sparks"] == ["cell-spark cpu", "cell-spark memory"], "web's 15 minutes (the others have none)"
    assert result["run"]["memory"] == ["Memory", "slowed down from (92 %)", "limit (processes ended)"]
    assert result["run"]["io"] == ["read", "written"]
    assert result["run"]["processes"] == 2, "the samples without a value are a gap"
    assert ["Memory: 7.6 of 8.0 GiB (95 %)", "gauge level-bad"] in result["run"]["gauges"]
    assert ["CPU: 1.50 of 2 CPUs (75 %)", "gauge level-ok"] in result["run"]["gauges"]
    assert result["run"]["label"].startswith("web: memory, the last 24 hours: Memory 7.6 GiB")
    assert "Memory: 7.6 GiB" in result["run"]["tip"] and "limit (processes ended): 8.0 GiB" in result["run"]["tip"]
    assert result["run"]["stacked"] >= 3, "web, git and api stacked"


def test_the_15_minute_window_is_read(logged_in):
    status, _ = logged_in("GET", "/api/hosts/ct1/monitor/usage/15m")
    assert status == 200
    assert logged_in.runner.calls[-1] == ("ct1", ["stats", "usage", "--since", "15m", "--json", "--series", "--by",
                                                  "run"])
