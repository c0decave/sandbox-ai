"""A terminal tab and its session (user reports from live use, 2026-11-14): closing the tab of a running session asks
Close tab / Stop session / Cancel (it said "End it?" and only closed the tab); a tab whose session ended closes
without a question, and Ctrl+W closes it then - only then (while it runs the key is the terminal's); a right click on
a tab offers Rename…, Restart, Stop session and Focus in Sandboxes & sessions; a tab that was closed is not opened
again at the next start of the app (only the tabs open at its end whose session still runs). Real terminal tabs
(offscreen QtWebEngine with a stand-in xterm.js) and a stand-in sandbox-ai."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from conftest import open_tab  # noqa: E402
from sandbox_ai_app import backend, state, widgets  # noqa: E402
from test_terminal_reopen import attached, running, sai, stub_web, terminal  # noqa: E402,F401


class Sessions:
    """The window's Sandboxes & sessions as the Terminals page uses it."""

    def __init__(self, *instances):
        self.instances = {i.key: i for i in instances}
        self.stoppable, self.stopped, self.refreshed = True, [], 0

    def stop(self, instance, asked=False):
        self.stopped.append((instance.name, asked))

    def refresh_soon(self, again=False):
        self.refreshed += 1


def session(name, state="running", host="local", tag=None):
    return backend.Instance("session", f"session:{name}", name, session=name, agent="claude", state=state, host=host,
                            record={"app_tab": tag} if tag else {})


@pytest.fixture
def page(terminal, stub_web, sai, monkeypatch):
    monkeypatch.setattr(widgets, "SHOWN", [])
    found = terminal.TerminalsPage(stub_web, sai=sai)
    found.sessions = Sessions(session("web"))
    found.sessions_page = lambda: found.sessions
    found.focused = []
    found.focus_start = found.focused.append
    yield found
    found.close_all()
    found.deleteLater()


def entries(page, index):
    return [(text, enabled) for text, enabled, *_ in filter(None, page.tab_actions(index))]


def test_closing_the_tab_of_a_running_session_asks_close_stop_or_cancel(page, sai, monkeypatch):
    view = open_tab(page, sai.command("attach", "web"), "web")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    page.close_tab(page.tabs.indexOf(view))
    assert page.tabs.count() == 1 and page.sessions.stopped == []
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Close terminal" and "keeps running when only its tab is closed" in text
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "close")
    page.close_tab(0)
    assert page.tabs.count() == 0 and page.sessions.stopped == [], "only the tab: the session runs on"
    view = open_tab(page, sai.command("attach", "web"), "web")
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "stop")
    page.close_tab(page.tabs.indexOf(view))
    assert page.tabs.count() == 0 and page.sessions.stopped == [("web", True)], "stopped without a second question"


def test_the_tab_of_an_ended_session_closes_without_a_question_and_ctrl_w_only_then(page, sai, wait, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    view = open_tab(page, sai.command("attach", "web"), "web")
    assert page.close_key.isEnabled() is False, "while the session runs Ctrl+W is the terminal's"
    page.close_ended_tab()
    assert page.tabs.count() == 1
    # The session is listed as stopped (its attach not noticed yet): ended.
    page.sessions.instances["session:web"].state = "stopped"
    page.update_close_key()
    assert page.close_key.isEnabled()
    shown = len(widgets.SHOWN)
    page.close_ended_tab()
    assert page.tabs.count() == 0 and len(widgets.SHOWN) == shown, "closed at once, nothing asked"
    # A command that ended: closed without a question too, by Ctrl+W as by its x.
    view = open_tab(page, ["sh", "-c", "exit 0"], "done")
    assert wait(lambda: not view.running, 10)
    assert wait(lambda: page.close_key.isEnabled(), 5)
    page.close_tab(page.tabs.indexOf(view))
    assert page.tabs.count() == 0 and len(widgets.SHOWN) == shown


def test_a_build_that_still_runs_is_still_asked_about(page, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    open_tab(page, ["sh", "-c", "sleep 60"], "build")
    page.close_tab(0)
    assert page.tabs.count() == 1 and widgets.SHOWN[-1][1] == "The command in this tab still runs. End it?"


def test_the_tab_menu_offers_the_sessions_actions_with_their_reasons(page, sai):
    view = open_tab(page, sai.command("attach", "web"), "web")
    assert entries(page, 0) == [("Rename…", True), ("Restart", True), ("Stop session", True),
                                ("Focus in Sandboxes & sessions", True),
                                ("File manager", False),  # (no window to open the Files page in this page alone)
                                ("Open shell", False),  # (no window to open a shell tab from, in this page alone)
                                ("Add folder…", False),  # (no window to add a folder from, in this page alone)
                                ("Close tab", True),
                                ("Stop session and close tab", True), ("Close other tabs", False)]
    menu = widgets.context_menu(page, page.tab_actions(0))
    assert [a.text() for a in menu.actions() if not a.isSeparator()][:4] == [
        "Rename…", "Restart", "Stop session", "Focus in Sandboxes & sessions"]
    dict((text, f) for text, _, _, f in filter(None, page.tab_actions(0)))["Focus in Sandboxes & sessions"]()
    assert page.focused == ["session:web"]
    # A build: no session to stop, restart or focus - each disabled with the reason.
    open_tab(page, ["sh", "-c", "sleep 60"], "build")
    actions = {text: (enabled, why) for text, enabled, why, _ in filter(None, page.tab_actions(1))}
    assert actions["Stop session"] == (False, "Not a session the app knows (yet): see Sandboxes & sessions")
    assert actions["Restart"] == (False, "Its command still runs: stop it first")
    assert actions["Focus in Sandboxes & sessions"][0] is False and actions["Close other tabs"][0] is True
    # A start in a tab is found by its tag (run.json "app_tab").
    start = open_tab(page, sai.command("claude", "--workspace", "/srv"), "claude")
    page.sessions.instances["session:brisk"] = session("brisk", tag=start.tab_tag)
    assert page.instance_of(start).name == "brisk"
    assert view is page.tabs.widget(0)


def test_restart_restarts_the_session_and_attaches_again_in_the_same_tab(page, sai, wait, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    open_tab(page, ["sh", "-c", "sleep 60"], "first")
    view = open_tab(page, sai.command("attach", "web"), "web")
    assert page.restart(view)
    assert wait(lambda: page.tabs.count() == 2 and page.tabs.widget(1) is not view, 10)
    assert ("local", ("resume", "--restart", "web")) in sai.asked
    assert attached(page)[1] == ("web", ("here", "web")), "attached again, in its place"
    # A command that ended runs again in its tab.
    done = open_tab(page, ["sh", "-c", "exit 0"], "done")
    assert wait(lambda: not done.running, 10)
    assert page.restart(done)
    assert page.tabs.count() == 3 and page.tabs.widget(2) is not done and page.tabs.widget(2).argv == done.argv


def test_rename_renames_the_session_and_its_tab_or_only_a_tab(page, sai, wait, monkeypatch):
    view = open_tab(page, sai.command("attach", "web"), "web")
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("webapp", True))
    assert page.rename(view)
    assert wait(lambda: page.tabs.tabText(0) == "webapp", 10)
    assert ("local", ("rename", "web", "webapp")) in sai.asked and view.start_name == "webapp"
    assert page.sessions.refreshed == 1
    build = open_tab(page, ["sh", "-c", "sleep 60"], "build")
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("my build", True))
    asked = len(sai.asked)
    assert page.rename(build) and page.tabs.tabText(1) == "my build" and len(sai.asked) == asked


def test_a_closed_tab_is_not_opened_again_at_the_next_start(terminal, stub_web, sai, tmp_path, wait, monkeypatch):
    """Only the tabs open at the app's end whose session still runs come back; another running session (its tab
    closed before) stays without one - unless the setting asks for every running session."""
    store = state.State(tmp_path / "state.json")
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = store
    open_tab(page, sai.command("attach", "web"), "web")
    closed = open_tab(page, sai.command("attach", "idle"), "idle")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "close")
    page.close_tab(page.tabs.indexOf(closed))
    assert [e["name"] for e in store.data["terminal-tabs"]] == ["web"]
    page.store = None
    page.close_all()
    page.deleteLater()
    sai.listed = [running("web"), running("idle")]
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = store
    done = []
    assert page.reopen(done=lambda opened, skipped: done.append(opened))
    assert wait(lambda: done and page.tabs.count() == 1, 20)
    assert done == [["web"]] and [title for title, _ in attached(page)] == ["web"]
    page.store = None
    page.close_all()
    page.deleteLater()


def test_a_renamed_tab_keeps_its_title_after_a_restart(terminal, stub_web, sai, tmp_path, wait, monkeypatch):
    store = state.State(tmp_path / "state.json")
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = store
    view = open_tab(page, sai.command("attach", "web"), "web")
    page.relabel(view, "the web app")
    assert store.data["terminal-tabs"][0]["label"] == "the web app"
    page.store = None
    page.close_all()
    page.deleteLater()
    sai.listed = [running("web")]
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = store
    done = []
    page.reopen(done=lambda opened, skipped: done.append(opened))
    assert wait(lambda: done and page.tabs.count() == 1, 20)
    assert page.tabs.tabText(0) == "the web app"
    page.store = None
    page.close_all()
    page.deleteLater()
