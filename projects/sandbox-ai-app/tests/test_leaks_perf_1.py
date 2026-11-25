"""Leaks and performance, review 1 (docs/reviews/leaks-perf-1.md): what an app left open for hours must not do -
a page that lays itself out again and again while nothing changes, windows and panels that stay alive after closing,
work that grows with the length of a list. Each test is a bound with headroom (measured numbers in the report)."""
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402

from sandbox_ai_app import widgets  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests


def idle(qapp, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


class Events(QtCore.QObject):
    """Counts the events of TYPES that reach any object (an application-wide filter)."""

    def __init__(self, *types):
        super().__init__()
        self.types, self.seen = set(types), {}

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in self.types:
            self.seen[event.type()] = self.seen.get(event.type(), 0) + 1
        return False


def test_the_credentials_page_does_not_lay_itself_out_while_nothing_changes(qapp, window, wait):  # noqa: F811
    """Measured: the page shown and idle took ~40 % of a core - a header's LayoutRequest evened the headers out
    (credential_tab.even_out), whose measure (layout.invalidate) asked for another LayoutRequest: ~280 layouts and
    ~500 paints a second, for as long as the page was shown."""
    window.resize(1200, 800)
    window.show()
    window.show_page("credentials", raise_window=False)
    page = window.credentials
    assert wait(lambda: not widgets.busy(page) and not widgets.busy(page.vpn), 20)
    idle(qapp, 1.0)  # what the first reads changed is laid out
    events = Events(QtCore.QEvent.Type.LayoutRequest, QtCore.QEvent.Type.Paint)
    qapp.installEventFilter(events)
    try:
        idle(qapp, 1.5)
    finally:
        qapp.removeEventFilter(events)
    layouts = events.seen.get(QtCore.QEvent.Type.LayoutRequest, 0)
    paints = events.seen.get(QtCore.QEvent.Type.Paint, 0)
    assert layouts < 10 and paints < 40, events.seen
    # ... and the headers are still evened out when a header's text changes while it is shown
    tab = page.tabs.currentWidget()
    widgets.set_explanations_shown(True)  # (an about line is an explanation: shown only with Show explanations)
    tall = tab.add_about("a line\n" * 6)
    assert wait(lambda: all(page.tabs.widget(i).header.height() == tab.header.height()
                            and tab.header.height() >= tall.sizeHint().height()
                            for i in range(page.tabs.count())), 5)


def test_a_tour_replaced_by_another_goes_and_stops_asking(qapp, window):  # noqa: F811
    """Measured: every tour started (Help > Area tours, F1, the Help page) left its panel in the guide's dock (Qt's
    setWidget hides the widget it replaces, never deletes it): +10 widgets per tour, and a task tour left that way
    kept checking its step twice a second for good. A guided tour after an area tour made a second dock."""
    from sandbox_ai_app import tours
    from sandbox_ai_app.guide_view import AreaTourPanel, TaskTourPanel
    window.show()
    tasks = list(tours.TASK_TOURS)
    areas = [key for key in tours.AREA_TOURS][:3]
    for _ in range(3):
        for ident in tasks[:2]:
            window.start_task_tour(ident)
        for key in areas:
            window.start_area_tour(key)
    window.start_task_tour(tasks[0])
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    panels = window.findChildren(AreaTourPanel) + window.findChildren(TaskTourPanel)
    assert panels == [window.tour_dock.widget()], panels
    assert window.tour_dock.widget().timer.isActive()
    # The guided tour comes back into the same dock; the task tour it replaces goes and stops asking.
    task = window.tour_dock.widget()
    window.start_tour()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert len(window.findChildren(QtWidgets.QDockWidget, "dock-tour")) == 1
    assert window.tour_dock.widget() is window.tour and window.tour.isVisible()
    assert not window.findChildren(TaskTourPanel) and not window.findChildren(AreaTourPanel)
    del task
    # An area tour from the guided tour: the guided tour waits hidden, and comes back with Help > Guided tour.
    window.start_area_tour(areas[0])
    window.start_tour()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert window.tour_dock.widget() is window.tour and not window.findChildren(AreaTourPanel)


def test_the_monitors_announced_warnings_are_bounded(qapp, env, monkeypatch):  # noqa: F811
    """The System monitor remembers which warnings it announced (run, kind, since) for the whole session: a warning
    that comes and goes (memory high every few minutes, 8 hours) added one each time, never let go. Now the memory
    is bounded; a warning still listed is never announced twice."""
    from sandbox_ai_app import backend, monitor_page, short_notices
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page = monitor_page.MonitorPage(backend.SandboxAI())
    page.show()
    told = []
    monkeypatch.setattr(short_notices, "short_notice", lambda _owner, _title, text, **_kw: told.append(text))
    steady = {"kind": "memory", "level": "warning", "since": 1_800_000_000, "text": "steady"}
    for since in range(monitor_page.SEEN_KEPT * 3):
        flapping = {"kind": "cpu", "level": "warning", "since": 1_800_000_000 + since, "text": "flapping"}
        page.show_warnings([{"run": "claude-a", "name": "a", "warnings": [steady, flapping]}], None)
        assert len(page.seen) <= monitor_page.SEEN_KEPT + 2
    assert len(told) == monitor_page.SEEN_KEPT * 3  # each new warning once
    assert len([text for text in told if text.count("\n") >= 1]) <= 1, "the steady warning was announced again"
    page.deleteLater()


def test_a_burst_of_warnings_counts_as_unread_once(qapp):
    """A burst of 2,000 warnings while the log was folded set the log panel's title 2,000 times (55 ms of the 60 ms
    the burst took to show); now once, with the same count."""
    from sandbox_ai_app import logview
    panel = logview.LogPanel()
    panel.set_watching(False)
    said = []
    panel.unread.connect(said.append)
    panel.add([{"time": "t", "level": "WARNING" if i % 2 else "INFO", "where": "x", "message": f"m{i}"}
               for i in range(2000)])
    assert said == [1000]
    panel.add({"time": "t", "level": "ERROR", "where": "x", "message": "one more"})
    assert said == [1000, 1001] and panel.unread_count == 1001
    panel.deleteLater()


def test_a_closed_chat_window_goes(qapp, env, monkeypatch):  # noqa: F811
    """LLM traffic: every chat opened in a window of its own (a whole conversation's HTML) stayed, closed, a hidden
    child of the page for as long as the app ran."""
    from sandbox_ai_app import backend
    from sandbox_ai_app.traffic_page import ChatWindow, TrafficPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page = TrafficPage(backend.SandboxAI())
    page.detail = {"summary": {"n": 1, "model": "m"}, "view": {}}
    for _ in range(5):
        chat = page.show_window()
        assert chat.isVisible()
        chat.close()
    del chat
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert page.findChildren(ChatWindow) == [] and page.windows == []
    chat = page.show_window()  # one left open: the page's end closes it
    page.shutdown()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert page.findChildren(ChatWindow) == []
    page.deleteLater()


def sessions(count, running_every=4):
    from sandbox_ai_app import backend
    return [backend.Instance("session", f"session:s{i}", f"s{i}", session=f"s{i}", project=f"/p/{i % 30}",
                             agent="claude", backend="podman", started="2026-11-03T10:00:00Z",
                             state="running" if i % running_every == 0 else "stopped",
                             record={"session_dir": f"/nonexistent/{i}"}) for i in range(count)]


def test_a_look_at_unchanged_sandboxes_draws_no_tab_again(qapp, window, monkeypatch):  # noqa: F811
    """Measured with 1000 sessions, 250 of them running (a tab each): every look of the Sessions page (every few
    seconds) set every tab's text, icon and tip again - each a layout of the whole tab row, 125,000 tab sizes
    measured: 1.6-2.2 s of the GUI thread per look. Now only a tab whose start changed is drawn again."""
    window.show()
    window.show_page("sandboxes", raise_window=False)
    page = window.sandboxes
    page.apply(([], []), None)  # the first look: what runs already opens no tab
    page.apply((sessions(160), []), None)  # 40 sessions started since: a tab each
    page.apply((sessions(160), []), None)  # (the next look marks them seen running)
    assert window.tabs.count() >= 40
    calls = []

    def counted(name):
        original = getattr(window.tabs, name)

        def call(*args):
            calls.append(name)
            return original(*args)
        return call
    for name in ("setTabText", "setTabIcon", "setTabToolTip", "setTabData"):
        monkeypatch.setattr(window.tabs, name, counted(name))
    page.apply((sessions(160), []), None)  # the same, read again
    assert calls == [], calls[:10]
    changed = sessions(160)
    changed[0].state = "stopped"  # s0 ended: its tab (only) shows it
    page.apply((changed, []), None)
    assert 0 < calls.count("setTabIcon") <= 2 and calls.count("setTabText") == 0, calls
    index = window.tabs.find("session:s0")
    assert index >= 0 and window.tabs.entry(index)["state"] == "stopped"
