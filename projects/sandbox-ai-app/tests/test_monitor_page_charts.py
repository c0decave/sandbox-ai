"""The System monitor's charts on the page, offscreen, against the real `sandbox-ai top` and `sandbox-ai stats usage`
of the checkout (user 2026-11-20: "es sollte weitere monitor status visualisierungen fuer cpu/ram/io/... geben"):
the run chosen's CPU, memory (with the 92 % slow-down and the limit), disk IO, network and processes over the window
from its history and every look since, its gauges in the warnings' colours, the sparklines in its CPU and memory
cells, the host's charts (network and processes too), a project's sum, every run stacked, the 15-minute window, an
older host without the detail, a host changed."""
import json
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, monitor_charts as mc, monitor_page  # noqa: E402
from sandbox_ai_app.monitor_page import LIMIT_ROLE, MonitorPage  # noqa: E402
from test_monitor_page import GIB, env, live, register  # noqa: E402,F401 - env: the fixture

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("SANDBOX_AI_HOME")
                                and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def full_series(env, run, workspace, minutes=10, mem=6 * GIB):
    folder = env / "state" / "sandbox-ai" / "logs" / "web-1"
    folder.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    lines = [{"header": 1, "run": run, "name": run, "workspace": workspace, "interval": 10}]
    lines += [{"t": now - minutes * 60 + i * 10, "dt": 10, "cpu": 1.0, "cpu_pct": 50.0, "cpu_lim": 2,
               "mem": mem, "mem_lim": 8 * GIB, "rd": 1000, "wr": 2000, "rx": 300, "tx": 400, "pids": 40,
               "pids_lim": 100} for i in range(minutes * 6)]
    (folder / f"monitor-20261117-100000-{run}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))


def host_series(env, minutes=10):
    folder = env / "state" / "sandbox-ai" / "monitor"
    folder.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    lines = [{"header": 1, "run": "host", "kind": "host", "interval": 10}]
    lines += [{"t": now - minutes * 60 + i * 10, "dt": 10, "cpu_pct": 30.0, "cpus": 8, "mem_total": 32 * GIB,
               "mem_avail": 20 * GIB, "swap_total": 4 * GIB, "swap_used": GIB, "rd": 50, "wr": 60, "rx": 70,
               "tx": 80, "pids": 900} for i in range(minutes * 6)]
    (folder / "monitor-00000000-000000-host.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))


@pytest.fixture
def page(qapp, env):
    register(env, "claude-web1", "web", "/p/web")
    register(env, "codex-api1", "api", "/p/web", agent="codex")
    # Each look at top: a sample with its own time, as the collectors write it (t), newer than the history's - a look
    # after the history was read. (Without a time of its own a look was placed at the moment the app got it: before
    # or after the end of the history's last slot - when `stats usage` was read - as the clock fell, and this file's
    # tests failed now and then in a full run.)
    after = int(time.time()) + 60
    live(env, "claude-web1", {"t": after, "cpu": 1.9, "cpu_pct": 95.0, "cpu_lim": 2, "thr": 10.0,
                              "mem": int(7.6 * GIB), "mem_lim": 8 * GIB, "rd": 2048, "wr": 4096, "rx": 100, "tx": 50,
                              "pids": 40, "pids_lim": 100},
         [{"kind": "memory", "level": "warning", "since": 1_800_000_000, "text": "near", "advice": "raise",
           "values": {"used": int(7.6 * GIB), "limit": 8 * GIB, "peak": 8 * GIB, "high": 1}}])
    live(env, "codex-api1", {"t": after, "cpu": 0.5, "mem": 2 * GIB, "pids": 5})
    live(env, "host", {"t": after, "cpu_pct": 42.0, "cpus": 8, "load1": 1.5, "mem_total": 32 * GIB, "mem_avail": 2 * GIB,
                       "swap_total": 4 * GIB, "swap_used": 3 * GIB, "rx": 1000, "tx": 2000, "pids": 950})
    full_series(env, "claude-web1", "/p/web")
    full_series(env, "codex-api1", "/p/web", mem=GIB)
    host_series(env)
    shown = MonitorPage(backend.SandboxAI())
    shown.resize(1000, 900)
    yield shown
    shown.close()
    shown.deleteLater()


def item_of(page, name):
    found = [item for item in page.tree.findItems(name, monitor_page.Qt.MatchFlag.MatchRecursive)
             if item.data(0, monitor_page.ROW_ROLE)[0] == "run"]
    return found[0]


def labels(chart):
    return [line.label for line in chart.lines]


def ready(page, wait):
    page.show()
    assert wait(lambda: page.data is not None and page.history is not None, timeout=60), page.status.text()


def test_the_run_chosen_has_its_charts_and_gauges(page, wait):
    ready(page, wait)
    page.tree.setCurrentItem(item_of(page, "web"))
    memory = page.memory_chart
    assert len(memory.points) >= 3 and memory.points[0]["mem"] == 6 * GIB, "the history is there at once"
    assert labels(memory) == ["Memory", "slowed down from (92 %)", "limit (processes ended)"]
    assert memory.points[-1]["mem"] == int(7.6 * GIB), "the latest look at the end"
    assert memory.title.startswith("web: memory") and memory.span[1] - memory.span[0] == 86400
    assert labels(page.cpu_chart) == ["CPU", "CPU limit"] and page.cpu_chart.points[0]["cpu_lim"] == 2
    assert labels(page.io_chart) == ["read", "written"] and page.io_chart.points[0]["rd"] == 1000
    assert labels(page.net_chart) == ["received", "sent"] and page.net_chart.points[0]["tx"] == 400
    assert labels(page.pids_chart) == ["processes and threads", "warning (90 %)", "limit"]
    for chart in (page.cpu_chart, memory, page.io_chart, page.net_chart, page.pids_chart):
        page.chart_tabs.setCurrentWidget(chart)
        chart.grab()
        assert chart.segments(0), chart.title
    spot = page.pids_chart.segments(0)[0][-1]
    assert "processes and threads: 40" in page.pids_chart.text_at(spot.x())
    assert "limit: 100" in page.pids_chart.text_at(spot.x())
    # The gauges: memory 95 % of its limit (above the 92 % where it is slowed: bad), CPU 95 % of its 2, processes.
    assert page.memory_gauge.level == "bad" and "7.6 of 8.0 GiB (95 %)" in page.memory_gauge.text
    assert page.cpu_gauge.level == "warn" and "1.90 of 2 CPUs" in page.cpu_gauge.text
    assert page.pids_gauge.level == "ok" and page.pids_gauge.text == "40 of 100 (40 %)"
    assert page.pids_gauge.accessibleName() == "Processes: 40 of 100 (40 %)"


def test_the_latest_look_stays_at_the_end_however_late_the_history_is_read(page, wait, monkeypatch):
    """The run chosen's charts end with the latest look at `top` also when the app got that look a few seconds before
    the history (`stats usage --series`, whose last slot ends when it was read) was read, as on a busy machine: a
    look is placed by its sample's own time, never by when the app got it. (The test above failed now and then in a
    full run: its looks had no time of their own, and the app's clock against the moment of the history's read
    decided whether the latest look was drawn.)"""
    add = mc.Ring.add  # (the app got each look 5 s before the history was read)
    monkeypatch.setattr(mc.Ring, "add", lambda self, key, sample, now=None:
                        add(self, key, sample, now=time.time() - 5 if now is None else now))
    ready(page, wait)
    page.tree.setCurrentItem(item_of(page, "web"))
    assert page.memory_chart.points[-1]["mem"] == int(7.6 * GIB), page.memory_chart.points[-3:]


def test_the_sparklines_in_the_cpu_and_memory_cells(page, wait):
    ready(page, wait)
    web = item_of(page, "web")
    assert isinstance(page.tree.itemDelegateForColumn(2), mc.CellSpark)
    assert isinstance(page.tree.itemDelegateForColumn(3), mc.CellSpark)
    assert web.data(3, LIMIT_ROLE) == 8 * GIB and web.data(2, LIMIT_ROLE) == 2
    index = page.tree.indexFromItem(web, 3)
    delegate = page.tree.itemDelegateForColumn(3)
    assert delegate.values(index)[-1] == 9 * GIB  # the spark of the live record
    width = page.tree.fontMetrics().horizontalAdvance(web.text(3))
    assert page.tree.columnWidth(3) >= width + mc.SPARK_WIDTH, "the text and the sparkline both fit"
    page.tree.grab()


def test_the_host_s_charts_a_project_s_sum_and_every_run_stacked(page, wait):
    ready(page, wait)
    page.tree.setCurrentItem(None)
    page.selection_changed()
    assert page.memory_chart.title.startswith("local (this machine): memory")
    assert labels(page.memory_chart) == ["used", "low (warning)", "total", "swap used"]
    assert labels(page.net_chart) == ["received", "sent"] and page.net_chart.points[0]["rx"] == 70
    assert page.pids_chart.points[0]["pids"] == 900
    assert page.memory_gauge.level == "bad", "2 of 32 GiB free: below the host's warning"
    assert page.pids_gauge.label == "Swap" and page.pids_gauge.level == "warn"
    assert not page.gauges_row.isHidden()
    project = page.tree.topLevelItem(0)
    page.tree.setCurrentItem(project)
    summed = page.memory_chart.points
    assert summed and max(point["mem"] for point in summed[:-1]) == 7 * GIB, "the project's runs added up"
    assert page.gauges_row.isHidden(), "a project has no limit of its own"
    page.chart_tabs.setCurrentWidget(page.all_box)
    assert labels(page.all_memory_chart)[:2] == ["web", "api"] and labels(page.all_memory_chart)[-1] == \
        "the host's memory"
    assert all(line.kind == "stack" for line in page.all_cpu_chart.lines[:2])
    page.all_memory_chart.grab()
    assert len(page.all_memory_chart.segments(1)) >= 1


def test_the_15_minute_window_and_an_older_host_without_the_detail(page, wait, monkeypatch):
    ready(page, wait)
    asked = []
    real = page.sai.host_run
    monkeypatch.setattr(page.sai, "host_run", lambda host, *argv, **k: asked.append(argv) or real(host, *argv, **k))
    page.window_box.setCurrentIndex(page.window_box.findData("15m"))
    assert page.window_box.itemText(0) == "15 minutes"
    assert wait(lambda: page.history_key and page.history_key[1] == "15m", timeout=60)
    assert any(argv[:4] == ("stats", "usage", "--since", "15m") for argv in asked)
    page.tree.setCurrentItem(item_of(page, "web"))
    assert page.cpu_chart.span[1] - page.cpu_chart.span[0] == 900
    # An older sandbox-ai there: its series only (CPUs, memory, IO and network together).
    for row in page.history["runs"]:
        row.pop("detail", None)
    page.history["host"].pop("detail", None)
    page.history_points = {}
    page.selection_changed()
    # (its history as one sum, the looks at top since with read and written each)
    assert labels(page.io_chart) == ["read", "written", "read + written"]
    page.ring.rings.pop("claude-web1")
    page.selection_changed()
    assert labels(page.io_chart) == ["read + written"] and labels(page.net_chart) == ["received + sent"]
    assert len(page.memory_chart.points) >= 2
    page.tree.setCurrentItem(None)
    page.selection_changed()
    assert labels(page.memory_chart) == ["Free memory"] or "used" in labels(page.memory_chart)


def test_german_and_another_host_clears_the_charts(page, wait):
    ready(page, wait)
    i18n.set_language("de")
    try:
        page.tree.setCurrentItem(item_of(page, "web"))
        page.selection_changed()
        assert labels(page.memory_chart)[0] == "Speicher" and "verlangsamt" in labels(page.memory_chart)[1]
        assert page.memory_gauge.text == "7.6 von 8.0 GiB (95 %)"
    finally:
        i18n.set_language("en")
    page.host_changed()
    assert page.memory_chart.points == [] and page.ring.rings == {}
