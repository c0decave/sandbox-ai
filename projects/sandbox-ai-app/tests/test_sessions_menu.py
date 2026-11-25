"""Sessions, a right click on a start (the user 2026-11-24: "wenn man die rechte Maustaste klickt, wird man erschlagen
von den Möglichkeiten - versuchen zu gruppieren und in Untermenüs zu packen, Kategorien mit durchgezogenen Linien
trennen"): the most used first (into it, its run), then the categories - what it may reach and has, what is inside it,
copy and move, manage - as submenus between separator lines; every command of the page's buttons still there, as
enabled as its button (with its reason)."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import widgets  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures
from test_tab_run_actions import running  # noqa: E402


def layout(menu):
    """[[entry, ...], ...]: the menu's groups between its separators; a submenu as (title, [its entries])."""
    groups, group = [], []
    for action in menu.actions():
        if action.isSeparator():
            groups.append(group)
            group = []
        elif action.menu() is not None:
            group.append((action.text(), [a.text() for a in action.menu().actions() if not a.isSeparator()]))
        else:
            group.append(action.text())
    return groups + [group]


def everything(menu):
    found = []
    for action in menu.actions():
        if action.menu() is not None:
            found += everything(action.menu())
        elif not action.isSeparator():
            found.append(action)
    return found


def test_the_right_click_is_grouped_with_lines_between_the_groups(window):  # noqa: F811
    running(window)
    page = window.sandboxes
    page.table.selectRow(0)
    page.update_buttons()
    menu = widgets.context_menu(page.table, page.table.menu_entries())
    try:
        groups = layout(menu)
        top = [entry if isinstance(entry, str) else entry[0] for group in groups for entry in group]
        assert len(top) <= 19, top  # (it had 45 lines; 18 and Show on the Board, todos.txt board 8)
        assert groups[0] == ["Show on the Board", "Attach", "Attach in the app", "Shell", "Browser"], groups[0]
        assert [e if isinstance(e, str) else e[0] for e in groups[1]] == [
            "Resume", "Resume with…", "Pause", "Stop…", "Kill switch"], groups[1]
        titles = [e[0] for group in groups[2:] for e in group if not isinstance(e, str)]
        assert titles == ["Network", "Add…", "Remove…", "Look inside", "Copy, move, export", "Manage"], titles
        sub = {e[0]: e[1] for group in groups for e in group if not isinstance(e, str)}
        assert sub["Network"] == ["Allow…", "Revoke…", "What it may reach…", "Connection log", "Reload rules"]
        assert sub["Look inside"] == ["Checkpoints…", "Memory…", "Session files…", "Storage…", "History…",
                                      "Report…"]
        assert sub["Copy, move, export"][:2] == ["Copy to host…", "Move to host…"] and "Export…" in \
            sub["Copy, move, export"]
        assert sub["Manage"][:3] == ["Rename…", "Delete", "Stop all…"] and "Clean up…" in sub["Manage"]
        assert groups[-1] == ["Refresh"]
        assert all(group for group in groups), "never two lines in a row, never one first or last"
        # Every command of the page's buttons is in it, as enabled as its button.
        actions = {action.text(): action for action in everything(menu)}
        buttons = [page.board_button, *page.session_buttons.values(), page.resume_with_button, page.stop_button, page.stop_all_button,
                   *page.run_buttons, page.credentials_button, *page.transfer_buttons.values(),
                   *page.manage_buttons.values(), page.refresh_button]
        for button in buttons:
            text = widgets.button_text(button)
            assert text in actions, text
            assert actions[text].isEnabled() == button.isEnabled(), text
        for action in page.cut_menu.actions():
            assert action.isSeparator() or action.text() in actions, action.text()
    finally:
        menu.deleteLater()


def test_a_disabled_command_in_a_submenu_says_why(window):  # noqa: F811
    running(window)
    page = window.sandboxes
    page.table.selectRow(0)
    page.update_buttons()
    menu = widgets.context_menu(page.table, page.table.menu_entries())
    try:
        delete = next(action for action in everything(menu) if action.text() == "Delete")
        assert not delete.isEnabled() and delete.toolTip() == page.session_buttons["delete"].toolTip()
    finally:
        menu.deleteLater()
