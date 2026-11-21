"""A terminal tab's right click -> "While it runs" (user 2026-11-19: "Go to Netzwerk / allow hosts / domain ..., MCP
add, ..."): the Sandboxes page's actions for a running start, for that tab's session - as enabled as the page's own
buttons (with their reasons), acting on that start without showing the page."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, terminal  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures


def running(window, name="web", run="claude-abc"):
    instance = backend.Instance("session", f"local:{name}", name, run=run, state="running", agent="claude",
                                session=name)
    window.sandboxes.instances[instance.key] = instance
    window.sandboxes.show_instances()
    return instance


def test_the_tab_menu_has_the_run_actions_of_its_session(window, monkeypatch):  # noqa: F811
    instance = running(window)
    shown = window.current_page()
    menu = window.run_menu_for(window, instance)
    assert menu is not None and menu.title() == "While it runs"
    texts = [action.text() for action in menu.actions()]
    for wanted in ("Allow…", "Revoke…", "Allowed during the run", "Add MCP server…", "Add device…", "Limits…"):
        assert wanted in texts, texts
    assert window.current_page() == shown, "the Sandboxes page is not shown for it"
    called = []
    monkeypatch.setattr(window.sandboxes, "network", lambda op: called.append((op, window.sandboxes.target())))
    allow = next(a for a in menu.actions() if a.text() == "Allow…")
    # (the button's own slot was bound when the page was made: its click goes through the page's network)
    window.sandboxes.run_named["allow"].clicked.disconnect()
    window.sandboxes.run_named["allow"].clicked.connect(lambda _=False: window.sandboxes.network("allow"))
    allow.trigger()
    assert called == [("allow", ("local", "claude-abc"))]


def test_no_submenu_for_a_start_the_page_does_not_list(window):  # noqa: F811
    ghost = backend.Instance("session", "local:ghost", "ghost", run="claude-x", state="running")
    assert window.run_menu_for(window, ghost) is None


def test_the_terminals_page_puts_it_into_the_tab_menu():
    import inspect
    source = inspect.getsource(terminal.TerminalsPage.tab_actions)
    assert source.index("self.workspace_entry(instance)") < source.index("self.run_menu(instance)")
    page = terminal.TerminalsPage.__new__(terminal.TerminalsPage)
    page.run_menu_request = lambda parent, inst: ("menu", inst)
    assert terminal.TerminalsPage.run_menu(page, "x") == ("menu", "x")
    assert terminal.TerminalsPage.run_menu(page, None) is None
