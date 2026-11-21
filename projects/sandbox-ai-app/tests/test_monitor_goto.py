"""System monitor -> right click on a run -> "Go to Terminals" and "Go to Sessions" (user 2026-11-19: "in Systemmonitor
-> rechte Maustaste auf Lauf -> zu Terminals und dort falls noch nicht vorhanden Terminal öffnen, -> zu Sitzungen und
dort markieren"). The app offscreen (MainWindow), with the hosts ct1 and ct2 of `sandbox-ai hosts`: what the Sandboxes
page lists is given (this machine's starts and ct1's), the monitor shows rows of `sandbox-ai top --json`, and the
terminal tabs are stand-ins (no Chromium in a test) made by the Terminals page's own code."""
import json
import time
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Signal  # noqa: E402
from sandbox_ai_app import backend, i18n, terminal, widgets  # noqa: E402
from sandbox_ai_app.monitor_page import ROW_ROLE  # noqa: E402

from test_pages_follow_host import close, isolated, make_window, sai, wait_for  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("SANDBOX_AI_HOME")
                                and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

GO_TERMINALS, GO_SESSIONS = "Go to Terminals", "Go to Sessions"


class FakeView(QtWidgets.QWidget):
    """A terminal tab without Chromium: what the Terminals page reads of a TerminalView."""
    exited = Signal(int)
    made = []

    def __init__(self, argv, web=None, cwd=None, parent=None, env=None):
        super().__init__(parent)
        self.argv, self.tab_title, self.label = list(argv), " ".join(map(str, argv[:3])), None
        self.tab_tag = (env or {}).get(terminal.TAB_TAG)
        self.start_host = self.start_name = self.instance_key = None
        self.view = QtWidgets.QWidget(self)
        self.running = True
        FakeView.made.append(self)

    def close_terminal(self):
        self.running = False


def local_starts():
    work = backend.Instance("session", "session:work", "work", run="session-work", session="work", project="/p/web",
                            agent="claude", state="running", record={"run": "session-work", "workspace": "/p/web"})
    task = backend.Instance("run", "run:codex-task1", "codex-task1", run="codex-task1", project="/p/api",
                            agent="codex", state="running", record={"run": "codex-task1", "workspace": "/p/api"})
    return [work, task]


CT1 = [{"name": "lab", "id": "lab", "kind": "session", "state": "running", "attachable": True, "run": "claude-lab1",
        "agent": "claude", "workspace": "/srv/lab"}]


def ct1_starts():
    return [backend.listed_instance("ct1", entry) for entry in CT1]


ROWS = {"local": [{"run": "session-work", "name": "work", "agent": "claude", "kind": "agent", "project": "/p/web",
                   "sample": {"cpu": 0.5, "mem": 1 << 30}, "warnings": [], "spark": []},
                  {"run": "mcp-git", "name": "git", "agent": None, "kind": "mcp", "project": "/p/web",
                   "parent": "work", "sample": {}, "warnings": [], "spark": []},
                  {"run": "codex-task1", "name": "codex-task1", "agent": "codex", "kind": "agent",
                   "project": "/p/api", "sample": {}, "warnings": [], "spark": []},
                  {"run": "claude-gone1", "name": "gone", "agent": "claude", "kind": "agent", "project": "/p/api",
                   "sample": {}, "warnings": [], "spark": []}],
        "ct1": [{"run": "claude-lab1", "name": "lab", "agent": "claude", "kind": "agent", "project": "/srv/lab",
                 "sample": {}, "warnings": [], "spark": []}]}


@pytest.fixture
def window(qapp, sai, monkeypatch):
    FakeView.made = []
    monkeypatch.setattr(terminal, "TerminalView", FakeView)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    monkeypatch.setattr(terminal.chromium, "check", lambda: SimpleNamespace(ok=True, error=None))
    monkeypatch.setattr(sai, "instances", lambda: (local_starts(), []))
    sai.answers[("ct1", ("list", "--json"))] = (0, json.dumps(CT1), "")  # (a look at ct1 lists the same)
    shown = make_window(qapp, sai)
    from conftest import read_now
    read_now(shown.sandboxes)
    page = shown.sandboxes
    page.remote["ct1"], page.remote_seen["ct1"] = ct1_starts(), time.time()
    page.merge_remote()
    notices = []
    monkeypatch.setattr("sandbox_ai_app.short_notices.short_notice",
                        lambda parent, title, text, **kw: notices.append((title, text)))
    shown.notices = notices
    yield shown
    close(qapp, shown)


def monitor_on(qapp, window, host):
    """The monitor shows HOST's runs (as `sandbox-ai top --json` there lists them); nothing is read meanwhile."""
    page = window.monitor
    page.hosts.load()
    page.hosts.set_host(host)
    qapp.processEvents()
    page.data = {"runs": [dict(row) for row in ROWS[host]], "host": {}}
    page.show_data()
    return page


def choose(page, key):
    found = [item for item in page.tree.findItems("*", widgets.Qt.MatchFlag.MatchWildcard
                                                   | widgets.Qt.MatchFlag.MatchRecursive)
             if item.data(0, ROW_ROLE) == key]
    assert found, key
    page.tree.setCurrentItem(found[0])
    return found[0]


def entries(page):
    return dict(widgets.menu_entries(widgets.menu_of(page.tree)))


def trigger(page, text):
    menu = widgets.menu_of(page.tree)
    action = next(a for a in menu.actions() if a.text() == text)
    assert action.isEnabled(), text
    action.trigger()


def test_a_run_row_has_both_entries_and_other_rows_say_why_not(qapp, window):
    page = monitor_on(qapp, window, "local")
    choose(page, ("run", "session-work"))
    found = entries(page)
    assert found[GO_TERMINALS] and found[GO_SESSIONS], found
    assert list(found)[:2] == ["Stop run…", "Raise budget…"], "after the entries there were"
    choose(page, ("run", "mcp-git"))  # an MCP server's sandbox: no terminal, not a start of its own
    menu = widgets.menu_of(page.tree)
    for text in (GO_TERMINALS, GO_SESSIONS):
        action = next(a for a in menu.actions() if a.text() == text)
        assert not action.isEnabled() and "MCP server" in action.toolTip()
    choose(page, ("project", "/p/web"))  # a project's row
    found = entries(page)
    assert not found[GO_TERMINALS] and not found[GO_SESSIONS]


def test_go_to_terminals_opens_one_tab_and_shows_it_again(qapp, window):
    page = monitor_on(qapp, window, "local")
    choose(page, ("run", "session-work"))
    trigger(page, GO_TERMINALS)
    assert wait_for(qapp, lambda: len(FakeView.made) == 1)
    terminals = window.terminals
    view = FakeView.made[0]
    assert terminal.attach_key(view.argv) == ("here", "work"), view.argv
    assert window.pages.currentWidget() is not None and window.current_page() == "terminals"
    assert terminals.tabs.currentWidget() is view
    other = FakeView(["sh"])  # another tab (a command that ended) is current now; the monitor shown again
    other.running = False
    FakeView.made.remove(other)
    terminals.tabs.addTab(other, "other")
    terminals.tabs.setCurrentIndex(terminals.tabs.count() - 1)
    window.show_page("monitor")
    trigger(page, GO_TERMINALS)
    qapp.processEvents()
    wait_for(qapp, lambda: False, 0.3)
    assert len(FakeView.made) == 1, "never a second tab for the same run"
    assert window.current_page() == "terminals" and terminals.tabs.currentWidget() is view


def test_go_to_terminals_on_a_host_attaches_over_ssh(qapp, window):
    page = monitor_on(qapp, window, "ct1")
    choose(page, ("run", "claude-lab1"))
    trigger(page, GO_TERMINALS)
    assert wait_for(qapp, lambda: len(FakeView.made) == 1)
    argv = FakeView.made[0].argv
    assert argv[0] == "ssh" and "attach lab" in argv[-1], argv
    trigger(page, GO_TERMINALS)
    wait_for(qapp, lambda: False, 0.3)
    assert len(FakeView.made) == 1


def test_a_run_without_a_terminal_says_why(qapp, window):
    page = monitor_on(qapp, window, "local")
    choose(page, ("run", "codex-task1"))  # a one-off run in the foreground of its own terminal
    trigger(page, GO_TERMINALS)
    wait_for(qapp, lambda: False, 0.3)
    assert FakeView.made == []
    assert window.notices and "foreground" in window.notices[-1][1], window.notices


def test_go_to_sessions_selects_the_run_s_row(qapp, window):
    page = monitor_on(qapp, window, "local")
    sandboxes = window.sandboxes
    sandboxes.host_filter.setCurrentIndex(sandboxes.host_filter.findData("ct1"))  # another host shown there
    qapp.processEvents()
    page = monitor_on(qapp, window, "local")
    choose(page, ("run", "session-work"))
    trigger(page, GO_SESSIONS)
    qapp.processEvents()
    assert window.current_page() == "sandboxes"
    assert sandboxes.host_filter.currentData() == "local", "the run's host chosen first"
    assert sandboxes.current() is not None and sandboxes.current().key == "session:work"
    item = sandboxes.table.selected_item()
    assert sandboxes.table.viewport().rect().intersects(sandboxes.table.visualItemRect(item)), "scrolled to"
    assert QtWidgets.QApplication.focusWidget() is sandboxes.table, QtWidgets.QApplication.focusWidget()


def test_go_to_sessions_on_a_host(qapp, window):
    sandboxes = window.sandboxes
    sandboxes.host_filter.setCurrentIndex(sandboxes.host_filter.findData("ct2"))
    page = monitor_on(qapp, window, "ct1")
    choose(page, ("run", "claude-lab1"))
    trigger(page, GO_SESSIONS)
    qapp.processEvents()
    sandboxes = window.sandboxes
    assert window.current_page() == "sandboxes"
    assert sandboxes.current() is not None and sandboxes.current().key == ct1_starts()[0].key
    assert sandboxes.host_filter.currentData() == "ct1", "the run's host chosen there first"


def test_a_run_not_listed_there_is_said(qapp, window):
    page = monitor_on(qapp, window, "local")
    choose(page, ("run", "claude-gone1"))
    trigger(page, GO_SESSIONS)
    qapp.processEvents()
    assert window.notices and "gone" in window.notices[-1][1] and "not listed" in window.notices[-1][1]
    trigger(page, GO_TERMINALS)
    assert FakeView.made == [] and "not listed" in window.notices[-1][1]


def test_the_entries_in_german(qapp, window):
    i18n.set_language("de")
    try:
        assert i18n.tr(GO_TERMINALS) == "Zu Terminals" and i18n.tr(GO_SESSIONS) == "Zu Sitzungen"
    finally:
        i18n.set_language("en")
