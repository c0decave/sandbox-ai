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
    for wanted in ("Allow…", "Revoke…", "Allowed during the run", "Add…", "Remove…", "Limits…"):
        assert wanted in texts, texts
    # (the user 2026-11-23: what adds or takes out is under Add… and Remove…, each opening the kinds)
    sub = {action.text(): action.menu() for action in menu.actions() if action.menu() is not None}
    assert [a.text() for a in sub["Add…"].actions()] == ["Add MCP server…", "Add skills/plugins…", "Add toolchain…",
                                                          "Add workspace…", "Add device…"]
    assert [a.text() for a in sub["Remove…"].actions()][0] == "Remove MCP server…"
    assert not any(text.startswith(("Add ", "Remove ")) for text in texts), "none of them flat any more"
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


def test_the_pages_own_right_click_groups_what_adds_and_what_takes_out(window):  # noqa: F811
    """Sandboxes & sessions, a right click on a start (the user 2026-11-23: "das rechte Maustastenmenü ist riesig ...
    MCP/Skills/Kits/Toolchain/Ordner/Gerät hinzufügen/entfernen jeweils unter Hinzufügen… / Entfernen…"): the add
    and remove actions are two submenus, each entry as enabled as its button, with its reason."""
    from sandbox_ai_app import widgets
    running(window)
    page = window.sandboxes
    page.table.selectRow(0)
    page.update_buttons()
    menu = widgets.context_menu(page.table, page.table.menu_entries())
    try:
        top = [action.text() for action in menu.actions() if not action.isSeparator()]
        assert "Add…" in top and "Remove…" in top and not any(t.startswith(("Add MCP", "Remove MCP")) for t in top)
        add = next(a.menu() for a in menu.actions() if a.text() == "Add…")
        texts = [a.text() for a in add.actions()]
        assert texts == ["Add MCP server…", "Add skills/plugins…", "Add toolchain…", "Add workspace…", "Add device…"]
        for action in add.actions():
            button = next(b for b in page.run_buttons if b.accessibleName() == action.text()
                          or getattr(b, "label", "") == action.text())
            assert action.isEnabled() == button.isEnabled()
        assert add.parent() is menu, "the submenu goes with its menu"
    finally:
        menu.deleteLater()
