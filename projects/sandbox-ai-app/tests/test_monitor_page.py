"""The System monitor page, offscreen, against the real `sandbox-ai top` and `sandbox-ai stats usage` of the checkout
in SANDBOX_AI_HOME: a run registered here (this test's own process stands in for its launcher), its collector's live
record and its series written as the collector writes them. The runs grouped by project (an MCP server under the
start it serves), the trend, the host's line, the history of what is selected, the warnings in German with what
helps, raising a limit after asking, stopping a run, no reading while the page is hidden, an older sandbox-ai."""
import json
import os
from pathlib import Path
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, i18n, monitor_page, widgets  # noqa: E402
from sandbox_ai_app.monitor_page import MonitorPage, SPARK_ROLE, warning_texts  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
GIB = 1 << 30


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def register(env, run, name, workspace, **extra):
    """A running run as its launcher registers it (run.json; its PID: this process)."""
    directory = env / "runtime" / "sandbox-ai" / "runs" / run
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({"run": run, "name": name, "agent": extra.pop("agent", "claude"),
                                                     "backend": "podman", "workspace": workspace,
                                                     "pid": os.getpid(), "kind": "agent", **extra}))


def live(env, run, sample, warnings=(), spark=None):
    path = env / "state" / "sandbox-ai" / "monitor" / "live" / f"{run}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run": run, "updated": time.time() + 3600, "interval": 10, "source": "cgroup",
                                "sample": sample, "warnings": list(warnings),
                                "spark": spark or [[i, 0.1 * i, GIB * i, 0, 0] for i in range(1, 10)]}))


def series(env, run, workspace, cpu, mem):
    folder = env / "state" / "sandbox-ai" / "logs" / "web-1"
    folder.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    lines = [{"header": 1, "run": run, "name": run, "workspace": workspace, "interval": 10}]
    lines += [{"t": now - 600 + i * 10, "dt": 10, "cpu": cpu, "mem": mem} for i in range(60)]
    (folder / f"monitor-20261117-100000-{run}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))


MEMORY_WARNING = {"kind": "memory", "level": "warning", "since": 1_800_000_000, "text": "web uses 7.6 of 8.0 GiB",
                  "advice": "raise --memory to 12g", "argv": ["limits", "claude-web1", "--memory", "12g"],
                  "values": {"used": int(7.6 * GIB), "limit": 8 * GIB, "peak": int(7.8 * GIB), "high": 3}}


@pytest.fixture
def page(qapp, env):
    register(env, "claude-web1", "web", "/p/web", mcp_servers=["git"])
    register(env, "mcp-git", "git", "/srv", kind="mcp", mcp_name="git", agent=None)
    register(env, "codex-api1", "api", "/p/api", agent="codex")
    live(env, "claude-web1", {"cpu": 1.5, "cpu_pct": 75.0, "mem": int(7.6 * GIB), "mem_lim": 8 * GIB, "rd": 2048,
                              "wr": 4096, "rx": 100, "tx": 50, "pids": 40}, [MEMORY_WARNING])
    live(env, "mcp-git", {"cpu": 0.2, "mem": GIB, "pids": 3})
    live(env, "codex-api1", {"cpu": 0.01, "mem": 3 * GIB, "pids": 5})
    live(env, "host", {"cpu_pct": 42.0, "cpus": 8, "load1": 1.5, "mem_total": 32 * GIB, "mem_avail": 20 * GIB})
    series(env, "claude-web1", "/p/web", 1.5, 6 * GIB)
    shown = MonitorPage(backend.SandboxAI())
    yield shown
    shown.close()
    shown.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def tree_rows(page):
    rows = []
    for index in range(page.tree.topLevelItemCount()):
        group = page.tree.topLevelItem(index)
        rows.append((group.text(0), [(child.text(0), [child.child(i).text(0) for i in range(child.childCount())])
                                     for child in (group.child(i) for i in range(group.childCount()))]))
    return rows


def test_the_runs_by_project_the_host_the_trend_and_the_history(page, wait):
    page.show()
    assert wait(lambda: page.data is not None, timeout=60), page.status.text()
    assert tree_rows(page) == [("web", [("web", ["git"])]), ("api", [("api", [])])]  # heaviest CPU first
    web = page.tree.topLevelItem(0).child(0)
    assert web.text(2) == "1.50 (75 %)" and web.text(3) == "7.6 / 8.0 GiB" and web.text(6) == "40"
    assert web.text(8) == "memory near the limit" and len(web.data(7, SPARK_ROLE)) == 9
    assert page.tree.topLevelItem(0).text(3) == "8.6 GiB"  # the project's sum: the run and its MCP server
    assert "CPU 42 % of 8" in page.host_line.text() and "memory free 20.0 of 32.0 GiB" in page.host_line.text()
    page.tree.setCurrentItem(web)
    assert wait(lambda: page.history is not None, timeout=60)
    page.selection_changed()
    assert len(page.chart.points) == 60 and page.chart.title.startswith("web: CPUs and memory")
    page.chart.grab()  # it paints
    page.tree.setCurrentItem(page.tree.topLevelItem(0))
    assert page.chart.title.startswith("web: CPUs and memory of its runs") and len(page.chart.points) == 60
    # Sorted by memory: the idle run holding 3 GiB comes first.
    page.sort.setCurrentIndex(page.sort.findData("mem"))
    assert wait(lambda: page.data is not None and page.data.get("sort") == "mem", timeout=60)
    assert [row[0] for row in tree_rows(page)] == ["web", "api"]
    assert page.data["runs"][0]["name"] == "web" and page.data["runs"][1]["name"] == "api"


def test_a_warning_says_what_helps_and_raising_the_limit_asks_first(page, wait, monkeypatch):
    page.show()
    assert wait(lambda: page.warnings.rowCount() == 1, timeout=60)
    assert "Raise --memory to 12g: peak 7.8 of 8.0 GiB with 3 throttling events" == page.warnings.item(0, 3).text()
    assert not page.apply_button.isEnabled()
    page.warnings.selectRow(0)
    assert page.apply_button.isEnabled()
    asked, done = [], []
    monkeypatch.setattr(monitor_page, "ask_first", lambda *a, **k: asked.append((a[1], a[4], k)) or True)
    monkeypatch.setattr(page.sai, "set_limits", lambda run, **k: done.append((run, k)) or (0, "memory: 12g", ""))
    entries = [text for text, _ in widgets.menu_entries(widgets.menu_of(page.warnings))]
    assert "Do what it suggests…" in entries and "Stop run…" in entries
    page.apply_button.click()
    assert wait(lambda: done, timeout=30)
    assert asked[0][0] == "limits-raise" and done == [("claude-web1", {"host": "local", "memory": "12g"})]
    stopped = []
    monkeypatch.setattr(page.sai, "host_run", lambda host, *argv, **k: stopped.append((host, argv)) or (0, "", ""))
    page.stop_button.click()
    assert wait(lambda: any(argv[:1] == ("stop",) for _, argv in stopped), timeout=30)
    assert ("local", ("stop", "web")) in stopped


def test_german_texts_of_every_kind():
    i18n.set_language("de")
    try:
        what, advice = warning_texts(MEMORY_WARNING, "web")
        assert what == "web belegt 7.6 von 8.0 GiB"
        assert advice == "--memory auf 12g anheben: Spitze 7.8 von 8.0 GiB mit 3 Drosselungen"
        idle = {"kind": "idle", "values": {"memory": int(3.2 * GIB), "seconds": 7200}, "argv": ["stop", "api"]}
        assert warning_texts(idle, "api")[1] == "Untätigen Lauf api stoppen: 3.2 GiB seit 2.0 h"
        for kind in ("oom", "cpu", "io", "pids", "host-memory", "host-swap"):
            assert len(warning_texts({"kind": kind, "values": {}, "text": "t", "advice": "a"}, "x")) == 2  # no error
        broken = {"kind": "cpu", "values": {"throttled": "x"}, "text": "plain", "advice": "do"}
        assert warning_texts(broken, "x") == ("plain", "do")  # the command's own text then
    finally:
        i18n.set_language("en")


def test_nothing_is_read_while_hidden(page, wait, monkeypatch):
    calls = []
    original = page.sai.host_run
    monkeypatch.setattr(page.sai, "host_run", lambda host, *argv, **k: calls.append(argv) or original(host, *argv, **k))
    page.timer.timeout.emit()  # hidden: the timer is not even running, and a tick reads only when shown
    assert not page.timer.isActive()
    page.show()
    assert wait(lambda: page.data is not None, timeout=60)
    assert page.timer.isActive() and page.timer.interval() == 10000
    page.hide()
    assert not page.timer.isActive()


def test_an_older_sandbox_ai_on_a_host_is_said(page, wait, monkeypatch):
    monkeypatch.setattr(page.sai, "host_run", lambda host, *argv, **k: (
        2, "", "usage: sandbox-ai [OPTIONS] COMMAND\nsandbox-ai: error: argument command: invalid choice: 'top'"))
    page.show()
    assert wait(lambda: "does not have the system monitor yet" in page.status.text(), timeout=30)


def test_another_host_chosen_while_a_slow_host_is_read_is_read_at_once(page, wait, monkeypatch):
    """Review 24h B: a host that answers slowly (or not at all: up to READ_SECONDS + 15 s) must not hold the page
    after another host was chosen - the new host's look starts at once, and the slow answer, when it comes, changes
    nothing (its ticket is old)."""
    import threading
    release, calls = threading.Event(), []
    original = page.sai.host_run

    def host_run(host, *argv, **kwargs):
        calls.append((host, argv[0]))
        if host == "slow":
            release.wait(30)
            return 255, "", "ssh: connect to host slow: Connection timed out"
        return original(host, *argv, **kwargs)
    monkeypatch.setattr(page.sai, "host_run", host_run)
    page.hosts.box.blockSignals(True)
    page.hosts.box.addItem("slow", "slow")
    page.hosts.box.blockSignals(False)
    page.hosts.set_host("slow")
    page.show()
    assert wait(lambda: ("slow", "top") in calls, timeout=10)
    page.hosts.set_host("local")
    try:
        assert wait(lambda: page.data is not None, timeout=20), "this machine is read while the slow host still hangs"
        assert tree_rows(page)[0][0] == "web"
    finally:
        release.set()
    assert wait(lambda: page.reading is None, timeout=20)
    QtWidgets.QApplication.processEvents()
    assert page.data is not None and tree_rows(page)[0][0] == "web", "the slow host's late answer changes nothing"
    assert "slow" not in page.status.text()


def test_the_status_says_the_interval_the_runs_are_sampled_at(page, wait, env):
    """Review 24h B: with monitor_interval 30 the page said "sampled every 10 s"."""
    for run in ("claude-web1", "mcp-git", "codex-api1"):
        path = env / "state" / "sandbox-ai" / "monitor" / "live" / f"{run}.json"
        record = json.loads(path.read_text())
        path.write_text(json.dumps({**record, "interval": 30}))
    page.show()
    assert wait(lambda: page.data is not None, timeout=60)
    assert page.status.text().endswith("sampled every 30 s"), page.status.text()


def test_many_runs_keep_the_scroll_position_and_the_open_projects_across_looks(page, wait, env):
    """Review 24h B: every look (10 s) rebuilt the tree: with many runs the list jumped back to the top while the
    human read further down, and a project the human had folded opened again."""
    for index in range(60):
        register(env, f"claude-many{index}", f"many{index}", f"/p/many{index % 3}")
        live(env, f"claude-many{index}", {"cpu": 0.01 * index, "mem": GIB, "pids": 2})
    page.resize(900, 500)
    page.show()
    assert wait(lambda: page.data is not None and len(page.data["runs"]) == 63, timeout=60)
    QtWidgets.QApplication.processEvents()
    bar = page.tree.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum() // 2)
    assert bar.value() > 0
    folded = next(page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount())
                  if page.tree.topLevelItem(i).text(0) == "many1")
    folded.setExpanded(False)
    before = bar.value()
    page.data = None
    page.refresh()
    assert wait(lambda: page.data is not None, timeout=60)
    assert bar.value() == before, "the list stays where the human scrolled to"
    again = next(page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount())
                 if page.tree.topLevelItem(i).text(0) == "many1")
    assert not again.isExpanded(), "a project folded stays folded"


def spend_log(env, run, records):
    """The spend log the connection proxy writes for RUN (sandbox-ai spend)."""
    folder = env / "state" / "sandbox-ai" / "logs" / "web-1"
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    lines = [{"v": 1, "time": now - 120, "event": "run", "run": run, "name": "web", "project": "/p/web",
              "project_key": "web-1", "agent": "claude",
              "budgets": [{"id": "run", "usd": 5, "used": 0, "label": "budget of this run (--budget)"}]}]
    lines += [{"v": 1, "time": now - 100 + i, **record} for i, record in enumerate(records)]
    (folder / f"spend-20261117-100000-{run}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))


def test_the_model_spend_of_a_run_its_budget_bar_and_a_raise_asked_first(page, wait, monkeypatch, env):
    """Spend limits: each run's cost in the list (stats usage's spend), the chosen run's cost so far with a bar per
    budget (sandbox-ai spend run, the checkout's own), Raise budget - also on the list's right-click menu - asks how
    much, then confirms (spend-raise) and runs sandbox-ai spend raise."""
    spend_log(env, "claude-web1", [
        {"provider": "anthropic", "model": "claude-sonnet-4-5", "billing": "api", "in": 1000, "out": 200, "usd": 4.0},
        {"provider": "openai", "model": "mystery-1", "billing": "api", "in": 10, "out": 5, "usd": None},
        {"event": "budget", "budget": "run", "level": 80, "used": 4.0, "usd": 5}])
    page.show()
    assert wait(lambda: page.history is not None and page.data is not None, timeout=60)
    web = page.tree.topLevelItem(0).child(0)
    assert wait(lambda: page.tree.topLevelItem(0).child(0).text(9) == "USD 4.00 + 1 of unknown cost", timeout=30)
    assert page.tree.topLevelItem(1).child(0).text(9) == "-"  # api: no model request counted
    page.tree.setCurrentItem(page.tree.topLevelItem(0).child(0))
    assert wait(lambda: "so far" in page.spend_line.text(), timeout=60), page.spend_line.text()
    assert page.spend_line.text().startswith("web: USD 4.00 + 1 of unknown cost so far - 2 model requests")
    assert "No price for mystery-1" in page.spend_line.text()
    bar = page.spend_bars[0]
    assert not bar.isHidden() and bar.value() == 80 and bar.format() == "80 % of USD 5.00 - budget of this run (--budget)"
    assert all(b.isHidden() for b in page.spend_bars[1:])
    assert "Raise budget…" in [text for text, _ in widgets.menu_entries(widgets.menu_of(page.tree))]
    asked, ran = [], []
    monkeypatch.setattr(monitor_page.QInputDialog, "getDouble", staticmethod(lambda *a, **k: (2.5, True)))
    monkeypatch.setattr(monitor_page, "ask_first", lambda *a, **k: asked.append((a[1], a[3])) or True)
    original = page.sai.host_run
    monkeypatch.setattr(page.sai, "host_run", lambda host, *argv, **k: ran.append(argv) or (
        (0, "claude-web1: may spend USD 2.50 more", "") if argv[:2] == ("spend", "raise") else original(host, *argv, **k)))
    assert page.raise_button.isEnabled()
    page.raise_button.click()
    assert wait(lambda: ("spend", "raise", "claude-web1", "2.50") in ran, timeout=30)
    assert asked[0][0] == "spend-raise" and "spend USD 2.50 more than its budgets" in asked[0][1]
    del web


def test_a_model_counted_at_the_fallback_price_is_said(page, wait, env):
    """Under a budget a model without a price is counted at the table's highest prices: the run's spend says how many
    requests, at which price, for which model (sandbox-ai spend run)."""
    spend_log(env, "claude-web1", [
        {"event": "fallback", "model": "brand-new", "price": {"in": 20.0, "out": 120.0}},
        {"provider": "openai", "model": "brand-new", "billing": "api", "in": 1000, "out": 100, "usd": 0.032,
         "fallback": {"in": 20.0, "out": 120.0}}])
    page.show()
    assert wait(lambda: page.history is not None and page.data is not None, timeout=60)
    page.tree.setCurrentItem(page.tree.topLevelItem(0).child(0))
    assert wait(lambda: "so far" in page.spend_line.text(), timeout=60), page.spend_line.text()
    assert ("1 request(s) at the fallback price USD 20.00 in / USD 120.00 out per million tokens: no price for "
            "brand-new.") in page.spend_line.text()
    assert "No price for brand-new: counted in tokens only" not in page.spend_line.text()


def test_without_a_run_chosen_the_window_s_total_and_no_raise(page, wait, env):
    spend_log(env, "claude-web1", [{"provider": "anthropic", "model": "claude-haiku-4-5", "billing": "api", "in": 10,
                                    "out": 10, "usd": 1.25}])
    page.show()
    assert wait(lambda: page.history is not None, timeout=60)
    page.tree.setCurrentItem(None)
    page.selection_changed()
    assert "Model requests of the last 24 hours: USD 1.25 (1 requests)" in page.spend_line.text()
    assert not page.raise_button.isEnabled()


def test_the_statistics_page_says_what_the_models_cost(qapp, env, wait):
    """Spend limits on the Statistics page: `sandbox-ai logs DIR --json` (the checkout's) sums the spend log of the
    folder per model - its cost, requests and a bar - and the summary names the total."""
    from sandbox_ai_app.pages import StatisticsPage
    spend_log(env, "claude-web1", [
        {"provider": "anthropic", "model": "claude-sonnet-4-5", "billing": "api", "in": 1000, "out": 200, "usd": 3.0},
        {"provider": "anthropic", "model": "claude-haiku-4-5", "billing": "api", "in": 10, "out": 5, "usd": 0.5},
        {"provider": "anthropic", "model": "claude-opus-4-5", "billing": "subscription", "in": 7, "out": 3}])
    page = StatisticsPage(backend.SandboxAI())
    try:
        page.refresh_directories(select=str(env / "state" / "sandbox-ai" / "logs" / "web-1"))
        assert wait(lambda: page.data is not None, timeout=60), page.summary.text()
        table = page.tables["spend"]
        rows = [[table.item(r, c).text() for c in range(3)] for r in range(table.rowCount())]
        assert ["claude-sonnet-4-5", "USD 3.00", "1"] in rows and ["claude-opus-4-5", "subscription", "1"] in rows
        assert "Model spend: USD 3.50 + subscription." in page.summary.text()
    finally:
        page.deleteLater()
def test_the_idle_warning_says_what_fits_the_run():
    """Backlog A: a kept session is stopped and kept, a run that is not kept ends for good, an idle MCP server and a
    helper are no run to stop with one click (their warning has no command: Apply stays off)."""
    values = {"memory": int(3.2 * GIB), "seconds": 7200}
    session = {"kind": "idle", "values": {**values, "role": "session"}, "argv": ["stop", "web"]}
    assert warning_texts(session, "web")[1] == ("Stop idle session web: 3.2 GiB for 2.0 h; it is kept: sandbox-ai "
                                                "resume web starts it again")
    run = {"kind": "idle", "values": {**values, "role": "run"}, "argv": ["stop", "web"]}
    assert warning_texts(run, "web")[1].endswith("it is not kept: what runs in it ends for good")
    mcp = {"kind": "idle", "values": {**values, "role": "mcp", "owner": "fetch"}, "argv": None}
    assert "sandbox-ai mcp stop fetch" in warning_texts(mcp, "fetch")[1]
    helper = {"kind": "idle", "values": {**values, "role": "helper", "owner": "web"}, "argv": None}
    assert warning_texts(helper, "codex-h1")[1].endswith("it ends with web (sandbox-ai stop web)")
    old = {"kind": "idle", "values": values, "argv": ["stop", "api"]}  # (an older host's warning: no role)
    assert warning_texts(old, "api")[1] == "Stop idle run api: 3.2 GiB for 2.0 h"
    i18n.set_language("de")
    try:
        assert warning_texts(session, "web")[1].startswith("Untätige Sitzung web stoppen")
        assert "sandbox-ai mcp stop fetch" in warning_texts(mcp, "fetch")[1]
    finally:
        i18n.set_language("en")
