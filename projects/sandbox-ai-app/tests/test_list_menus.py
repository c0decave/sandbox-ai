"""A right click on every list of the app offers what its page's buttons offer (user report from live use,
2026-11-14: "Sandboxes & sessions: every command of the buttons must be in a context menu too"): the same commands,
enabled as the buttons are for the selected row(s) - a disabled one with the same reason -, a hidden button left
out, several selected rows kept selected (an action on all of them), a click on an unselected row selecting it."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def texts(view):
    """The entries of VIEW's context menu now (submenus' entries indented), without Copy."""
    return [text for text, _ in widgets.menu_entries(widgets.menu_of(view))]


def button_texts(*buttons):
    return [widgets.button_text(b) for b in buttons]


def test_the_menu_mirrors_buttons_their_state_and_reason(qapp):
    holder = QtWidgets.QWidget()
    view = QtWidgets.QTableWidget(3, 1, holder)
    view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
    for row in range(3):
        view.setItem(row, 0, QtWidgets.QTableWidgetItem(f"row {row}"))
    go, stop, hidden = (QtWidgets.QPushButton(text, holder) for text in ("&Go", "Stop…", "Secret"))
    hidden.hide()
    widgets.set_enabled(stop, False, "It does not run")
    menu_button = QtWidgets.QPushButton("More", holder)
    more = QtWidgets.QMenu(menu_button)
    more.addAction("Inner")
    menu_button.setMenu(more)
    clicked = []
    go.clicked.connect(lambda: clicked.append("go"))
    widgets.list_menu(view, lambda: [None, go, None, None, stop, hidden, menu_button, None])
    menu = widgets.menu_of(view)
    assert widgets.menu_entries(menu) == [("Go", True), ("Stop…", False), ("More", True), ("  Inner", True)]
    assert sum(a.isSeparator() for a in menu.actions()) == 1, "no separator first, last or twice"
    stop_action = next(a for a in menu.actions() if a.text() == "Stop…")
    assert "It does not run" in stop_action.toolTip()
    next(a for a in menu.actions() if a.text() == "Go").trigger()
    assert clicked == ["go"]
    # A right click on a row not selected selects it; on one of several selected keeps them all.
    point = view.visualItemRect(view.item(1, 0)).center()
    widgets.select_under(view, point)
    assert widgets.selected_rows(view) == [1]
    view.selectRow(0)
    view.selectionModel().select(view.model().index(2, 0), view.selectionModel().SelectionFlag.Select |
                                 view.selectionModel().SelectionFlag.Rows)
    widgets.select_under(view, view.visualItemRect(view.item(2, 0)).center())
    assert widgets.selected_rows(view) == [0, 2]
    widgets.select_under(view, QPoint(5, 2000))  # (no row there: nothing changes)
    assert widgets.selected_rows(view) == [0, 2]
    holder.deleteLater()


def test_every_list_of_the_app_offers_its_pages_actions(window):
    window.build_pages()
    s = window.sandboxes
    found = texts(s.table)
    for button in [*s.session_buttons.values(), s.resume_with_button, s.stop_button, s.cut_button, *s.run_buttons,
                   *s.transfer_buttons.values(), *s.manage_buttons.values()]:
        assert widgets.button_text(button) in found, widgets.button_text(button)
    a = window.approvals
    assert texts(a.open)[:6] == button_texts(a.allow_button, a.allow_run_button, a.allow_always_button, a.deny_button,
                                             a.deny_once_button, a.block_button)
    assert texts(a.always)[0] == "Forget…"
    assert texts(a.blocked.table)[:4] == button_texts(*a.blocked.buttons.values())
    assert texts(a.allowed.table)[0] == "Revoke…"
    m = window.mcp
    assert set(button_texts(*m.server_buttons)) <= set(texts(m.servers))
    assert {"Add", "Details"} & set(texts(m.catalog)) or texts(m.catalog)
    i = window.images
    assert texts(i.images)[:2] == ["Build", "Other builds"] and "Delete variant…" in texts(i.images)
    assert texts(i.user_table)[0] == "Remove…"
    assert texts(i.files) == ["Remove", "Add file…"]
    assert texts(i.lists["apt"].list) == ["Remove"]
    llm = window.llm
    assert texts(llm.servers)[:2] == ["Edit…", "Check"] and "Remove…" in texts(llm.servers)
    assert texts(llm.model_table) == ["Use as default", "Copy NAME:MODEL"]
    h = window.hosts
    assert texts(h.table)[:2] == ["Show starts", "Show questions"] and "Remove…" in texts(h.table)
    c = window.credentials
    # (every credentials tab: Edit… and Give to a running sandbox… - greyed out with the reason where a kind cannot
    # be given to a sandbox that runs: publish logins, identities, VPN profiles)
    # (Give to a sandbox on another host… is on the right click and in the Give dialog, not in the button row: the
    # row must fit a small window)
    assert texts(c.git.list)[:6] == ["Add…", "Edit…", "Remove…", "Check", "Give to a running sandbox…",
                                     "Give to a sandbox on another host…"]
    assert c.git.give_far_button not in [c.git.button_row.itemAt(i).widget() for i in range(c.git.button_row.count())]
    assert texts(c.publish.list)[:6] == ["Add…", "Edit…", "Remove…", "Check", "Kinds", "Give to a running sandbox…"]
    # (the fifth: the primary password's step that is due - Set password…, Unlock… or Lock)
    assert texts(c.secrets) == ["Add", "Edit…", "Remove…", "Give to a running sandbox…", "Unlock…",
                                "Give to a sandbox on another host…"]
    # (Refresh: on the right click - the row's Refresh is the one of the sessions' logins below, which reads both)
    assert texts(c.logins)[:3] == ["Log in…", "Log out…", "Give to a running sandbox…"] and "Refresh" in texts(c.logins)
    assert texts(c.vpn.profiles)[:6] == ["Add…", "Edit…", "Show", "Check", "Remove…", "Give to a running sandbox…"]
    assert texts(c.given.list) == ["Revoke…", "Refresh"]
    assert texts(c.session_logins.list)[:4] == ["Details…", "Import as central login…", "Use for a new start…",
                                                "Give to a running sandbox…"]
    if c.identities is not None:
        assert texts(c.identities.list)[:5] == ["Add…", "Edit…", "Remove…", "Check", "Give to a running sandbox…"]
    k = window.kits
    assert widgets.button_text(k.use_button) in texts(k.table)
    act = window.activity
    assert texts(act.table) == ["Show in Connections", "Show in Files"]
    assert texts(act.safeguards.tree) == ["Copy the fix", "Refresh"]
    assert texts(act.canaries.tree)[:3] == ["Canaries of the run", "Show the tokens…", "Resume…"]
    assert texts(window.traffic.table)[:2] == ["Open in a window", "Export…"]
    t = window.team
    assert texts(t.table)[:2] == ["Talk to the lead", "Post a task…"]
    assert texts(window.terminals.list) == ["Open terminal", "Refresh"]
    assert texts(window.board.findings) == ["Show the part"]
    files = window.files
    from sandbox_ai_app.files import Entry
    files.right.chosen = lambda: [Entry("notes.txt", "file", 3, 0.0, mode=0o100644)]
    menu = files.menu(files.right, at_row=True)
    found = [a.text() for a in menu.actions()]
    assert {"Rename…", "Delete…", "Permissions…", "Owner…"} <= set(found), found


def test_a_disabled_action_says_why_in_the_menu_as_on_its_button(window):
    window.build_pages()
    page = window.sandboxes
    page.update_buttons()
    menu = widgets.menu_of(page.table)
    stop = next(a for a in menu.actions() if a.text() == widgets.button_text(page.stop_button))
    assert stop.isEnabled() == page.stop_button.isEnabled()
    assert stop.toolTip() == page.stop_button.toolTip()


def test_a_preset_card_has_the_galleries_actions(window, monkeypatch):
    from sandbox_ai_app.preset_gallery import PresetGallery
    gallery = PresetGallery(window.sai, None, "claude", {}, window.board.board, window)
    card = gallery.cards[0]
    gallery.choose(card)
    entries = widgets.menu_entries(widgets.context_menu(gallery, gallery.menu_entries()))
    assert [text for text, _ in entries] == ["Create", "Export…", "Rename…", "Delete"]
    assert dict(entries)["Rename…"] == (card.preset.source == "mine")
    gallery.deleteLater()
