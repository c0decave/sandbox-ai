"""The System monitor's charts and gauges (monitor_charts.py), offscreen: a chart draws and follows its data, its
limits, an empty window, one sample, a gap, the exact values at the time under the mouse, a small and a large size,
the theme's colours (dark and light); the gauges' warning colours; the ring of looks bounded; the history and the
looks after it merged; several runs added up and stacked (user 2026-11-20: "es sollte weitere monitor status
visualisierungen fuer cpu/ram/io/... geben")."""
from pathlib import Path
import re

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from sandbox_ai_app import monitor_charts as mc, theme  # noqa: E402

GIB = 1 << 30
ROOT = Path(__file__).resolve().parents[3]


def memory_chart(points):
    chart = mc.TimeChart("Memory")
    chart.resize(480, 180)
    chart.set_data("web: memory", points, [
        mc.Line("Memory", "mem", "series1", "area"),
        mc.Line("slowed down", lambda p: p.get("mem_lim") and p["mem_lim"] * mc.SLOW_SHARE, "warn", "limit"),
        mc.Line("limit", "mem_lim", "bad", "limit")], unit=lambda v: f"{v / GIB:.1f} GiB")
    return chart


def rising(count=30, start=1_800_000_000, step=10):
    return [{"t": start + i * step, "mem": GIB * (1 + i / 10), "mem_lim": 8 * GIB, "cpu": i / 10}
            for i in range(count)]


def test_the_thresholds_are_those_of_the_sandbox_and_the_warnings():
    agent = (ROOT / "sandbox" / "agent_sandbox.py").read_text()
    monitor = (ROOT / "sandbox" / "monitor.py").read_text()
    for text, pattern, value in (
            (agent, r"^MEMORY_HIGH, MEMORY_STALL = ([0-9.]+),", mc.SLOW_SHARE),
            (monitor, r"^MEMORY_NEAR, MEMORY_CLEAR = ([0-9.]+),", mc.MEMORY_NEAR),
            (monitor, r"^PIDS_NEAR, PIDS_CLEAR, PIDS_RUNAWAY = ([0-9.]+),", mc.PIDS_NEAR),
            (monitor, r"^THROTTLE_RATIO, [A-Z_, ]+= ([0-9.]+),", mc.THROTTLE_RATIO),
            (monitor, r"^HOST_MEMORY_LOW, [A-Z_, ]+= ([0-9.]+),", mc.HOST_MEMORY_LOW)):
        assert float(re.search(pattern, text, re.M).group(1)) == value, pattern


def test_a_chart_draws_follows_its_data_and_its_limits(qapp):
    chart = memory_chart(rising())
    chart.grab()
    [line] = chart.segments(0)
    assert len(line) == 30
    assert all(b.y() < a.y() for a, b in zip(line, line[1:])), "more memory is higher up"
    assert all(b.x() > a.x() for a, b in zip(line, line[1:])), "later is further right"
    slowed, limit = chart.segments(1)[0], chart.segments(2)[0]
    assert limit[0].y() < slowed[0].y() < line[-1].y() + 1000  # the limit above the slow-down line
    area = chart.plot_rect()
    assert abs(limit[0].y() - chart.y_of(8 * GIB, chart.top(), area)) < 0.01
    assert chart.top() >= 8 * GIB, "the limit is in the picture"
    assert "web: memory" in chart.accessibleDescription() and "Memory 3.9 GiB" in chart.accessibleDescription()
    # New data: the line follows.
    chart.set_data(chart.title, [{**p, "mem": GIB} for p in rising()], chart.lines, chart.unit)
    flat = chart.segments(0)[0]
    assert len({round(spot.y(), 3) for spot in flat}) == 1


def test_empty_one_sample_and_a_gap(qapp):
    chart = memory_chart([])
    chart.grab()
    assert chart.segments(0) == [] and chart.text_at(100) == ""
    assert "No samples in this window yet." in chart.accessibleDescription()
    chart.set_data("one", rising(1), chart.lines, chart.unit)
    chart.grab()  # a dot
    assert [len(piece) for piece in chart.segments(0)] == [1]
    points = rising(10) + [{**p, "t": p["t"] + 3600} for p in rising(10, start=1_800_000_100)]
    chart.set_data("gap", points, chart.lines, chart.unit)
    chart.grab()
    assert [len(piece) for piece in chart.segments(0)] == [10, 10], "an hour without samples breaks the line"
    # A sample without the value: a gap too.
    chart.set_data("hole", [*rising(5), {"t": 1_800_000_050}, *rising(5, start=1_800_000_060)], chart.lines,
                   chart.unit)
    assert [len(piece) for piece in chart.segments(0)] == [5, 5]
    # Samples without any of the chart's values (an older host's network): what it says instead.
    chart.set_data("older", [{"t": 1_800_000_000}, {"t": 1_800_000_010}], chart.lines, chart.unit,
                   empty="Not recorded there")
    chart.grab()
    assert chart.segments(0) == [] and chart.accessibleDescription() == "older: Not recorded there"


def test_the_tooltip_says_the_exact_values_at_the_time_under_the_mouse(qapp, monkeypatch):
    chart = memory_chart(rising())
    chart.show()
    spot = chart.segments(0)[0][12]
    text = chart.text_at(spot.x())
    assert "Memory: 2.2 GiB" in text and "limit: 8.0 GiB" in text and "slowed down: 7.4 GiB" in text
    shown = []
    monkeypatch.setattr(mc.QToolTip, "showText", lambda pos, words, widget=None: shown.append(words))
    point = QtCore.QPoint(int(spot.x()), int(spot.y()))
    event = QtGui.QHelpEvent(QtCore.QEvent.Type.ToolTip, point, chart.mapToGlobal(point))
    chart.event(event)
    assert shown and "Memory: 2.2 GiB" in shown[0] and shown[0].startswith("<p")  # through safe_tooltip
    move = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(point), QtCore.QPointF(point),
                             QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton,
                             QtCore.Qt.KeyboardModifier.NoModifier)
    chart.mouseMoveEvent(move)
    assert chart.hover == 12
    chart.grab()
    chart.close()


def test_small_and_large_sizes(qapp):
    chart = memory_chart(rising())
    for width, height in ((140, 90), (220, 100), (1200, 400)):
        chart.resize(width, height)
        image = chart.grab().toImage()
        assert image.width() >= width
        pieces = chart.segments(0)
        assert pieces and all(chart.plot_rect().left() - 0.5 <= spot.x() <= chart.plot_rect().right() + 0.5
                              for spot in pieces[0])
    assert chart.minimumSizeHint().width() <= 140 or chart.minimumWidth() == 140


def test_the_theme_s_colours_are_used_at_each_paint(qapp):
    chart = memory_chart(rising())
    try:
        for mode in ("dark", "light"):
            theme.apply(mode)
            chart.grab()
            assert QtGui.QColor(theme.colors()["text"]).name() in chart.painted
            assert QtGui.QColor(theme.color("bad")).name() in chart.painted, "the limit in the theme's bad colour"
            assert QtGui.QColor(theme.color("warn")).name() in chart.painted
        light = mc.series_color(3).name()
        theme.apply("dark")
        assert mc.series_color(3).name() != light
        assert theme.contrast(mc.series_color(3), theme.colors()["base"]) >= 3.0
        theme.apply("light")
        assert theme.contrast(mc.series_color(3), theme.colors()["base"]) >= 3.0
    finally:
        theme.apply("system")


def test_stacked_areas_lie_on_each_other(qapp):
    chart = mc.TimeChart("All runs")
    chart.resize(400, 160)
    points = [{"t": 1_800_000_000 + i * 10, "a": 1.0, "b": 2.0} for i in range(5)]
    chart.set_data("CPU", points, [mc.Line("a", "a", "series0", "stack"), mc.Line("b", "b", "series1", "stack")],
                   unit=lambda v: f"{v:.1f}")
    chart.grab()
    a, b = chart.segments(0)[0], chart.segments(1)[0]
    area, top = chart.plot_rect(), chart.top()
    assert abs(a[0].y() - chart.y_of(1.0, top, area)) < 0.01 and abs(b[0].y() - chart.y_of(3.0, top, area)) < 0.01
    assert top >= 3.0


def test_gauges_in_the_warnings_colours(qapp):
    assert mc.level_of(0.5, 0.9, 0.92) == "ok" and mc.level_of(0.91, 0.9, 0.92) == "warn"
    assert mc.level_of(0.95, 0.9, 0.92) == "bad" and mc.level_of(None, 0.9, 0.92) == "muted"
    assert mc.level_of(0.2, 0.9, 0.92, "warning") == "warn" and mc.level_of(0.2, 0.9, 0.92, "error") == "bad"
    gauge = mc.Gauge("Memory")
    gauge.resize(240, 26)
    gauge.set_value("Memory", 0.95, "7.6 of 8.0 GiB (95 %)", "bad")
    gauge.grab()
    assert gauge.accessibleName() == "Memory: 7.6 of 8.0 GiB (95 %)" and gauge.toolTip().startswith("<p")
    gauge.set_value("Memory", 3.0, "more than its limit", "bad")
    assert gauge.share == 1.0
    gauge.resize(120, 26)
    gauge.grab()


def test_the_ring_is_bounded_and_lets_gone_runs_go():
    ring = mc.Ring(points=50, keys=3)
    for i in range(200):
        ring.add("a", {"t": i, "cpu": 1.0})
    ring.add("a", {"t": 199, "cpu": 9.0})  # the same sample again
    assert len(ring.points("a")) == 50 and ring.points("a")[-1] == {"t": 199.0, "cpu": 1.0}
    ring.add("a", {"t": 150, "cpu": 9.0})  # older: not taken
    assert ring.points("a")[-1]["t"] == 199.0
    for key in ("b", "c", "d"):
        ring.add(key, {"t": 1, "cpu": 1})
    assert set(ring.rings) == {"a", "b", "c"}, "at most KEYS kept"
    ring.keep({"a", "host"})
    assert set(ring.rings) == {"a"}
    ring.add("x", {"cpu": "much", "mem": 5}, now=7)
    assert ring.points("x") == [{"t": 7.0, "mem": 5}]
    ring.seed("y", [[100, 0.5, GIB, 0, 0], [110, 0.6, GIB, 0, 0]])
    ring.seed("y", [[120, 9, 9, 9, 9]])  # only when nothing is kept of it
    assert [p["cpu"] for p in ring.points("y")] == [0.5, 0.6]


def test_history_and_looks_merged_and_runs_added_up():
    detail = {"t": [100, 200, 300], "cpu": [1.0, None, 3.0], "mem": [GIB, 2 * GIB, 3 * GIB], "bad": [1]}
    points = mc.from_detail(detail)
    assert points == [{"t": 100.0, "cpu": 1.0, "mem": GIB}, {"t": 200.0, "mem": 2 * GIB},
                      {"t": 300.0, "cpu": 3.0, "mem": 3 * GIB}]
    assert mc.from_detail({"t": "x"}) == [] and mc.from_detail(None) == []
    assert mc.from_series([[100, 1.0, GIB, 5, 6], ["x"]]) == [{"t": 100.0, "cpu": 1.0, "mem": GIB, "io": 5, "net": 6}]
    recent = [{"t": 250.0, "cpu": 9}, {"t": 310.0, "cpu": 4}, {"t": 320.0, "cpu": 5}]
    assert [p["t"] for p in mc.merged(points, recent, 150)] == [200.0, 300.0, 310.0, 320.0]
    assert [p["t"] for p in mc.merged([], recent, 0)] == [250.0, 310.0, 320.0]
    a = [{"t": 10.0, "cpu": 1.0, "mem": GIB, "mem_lim": 4 * GIB}, {"t": 20.0, "cpu": 1.0, "mem": GIB}]
    b = [{"t": 12.0, "cpu": 2.0, "mem": 2 * GIB, "mem_lim": 4 * GIB}]
    summed = mc.total([a, b], 10)
    assert summed[0] == {"t": 20.0, "cpu": 3.0, "mem": 3 * GIB, "mem_lim": 8 * GIB}
    assert summed[1] == {"t": 30.0, "cpu": 1.0, "mem": GIB}
    both = mc.joined({"a": a, "b": b}, "cpu", 10)
    assert both == [{"t": 20.0, "a": 1.0, "b": 2.0}, {"t": 30.0, "a": 1.0}]
