"""The Sessions page's own tabs and icon rows (user 2026-11-19), offscreen through the MainWindow: a tab row "Sessions
& sandboxes" (the tree and its actions) and "Schedules" (the scheduled runs), the chosen tab kept for the next start
and every way to a start (Show in Sandboxes & sessions, Hosts › Show starts, a tour step, a row selected from
elsewhere) landing on the right tab; the buttons of the groups "Session" and "While it runs" drawn as theme-aware SVG
icons in two rows above the tree (Session first), each with its text as tooltip and accessible name and no text by
default; Settings › Appearance › "Show button texts" brings the texts back at once; the right-click menu keeps the
texts; German; nothing clipped at the smallest window in every theme, with and without texts."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from sandbox_ai_app import backend, button_icons, i18n, theme, widgets  # noqa: E402

from conftest import read_now  # noqa: E402
from test_ui import env, fake_run, window  # noqa: E402,F401 - the isolated home and window of the UI tests

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def shown(window, qapp):
    window.show()
    window.show_page("sandboxes", raise_window=False)
    qapp.processEvents()
    return window.sandboxes


def session_row(page):
    """The buttons of the group Session, as they were in its group box."""
    return [*page.session_buttons.values(), page.resume_with_button, *page.transfer_buttons.values()]


def run_row(page):
    """The buttons of the group While it runs."""
    return [*page.run_buttons, page.credentials_button, page.stop_all_button]


# -- the tabs --

def test_the_page_has_two_tabs_that_switch(qapp, window):  # noqa: F811
    page = shown(window, qapp)
    tabs = page.tabs
    assert [tabs.tabText(i).replace("&&", "&") for i in range(tabs.count())] == ["Sessions & sandboxes",
                                                                                 "Schedules"]
    assert "&" not in tabs.tabText(0).replace("&&", ""), "the & is shown, not a mnemonic"
    assert tabs.currentWidget() is page.sessions_tab, "the sessions first"
    assert page.table.isVisible() and not page.schedules_box.isVisible()
    assert page.sessions_tab.isAncestorOf(page.table) and page.sessions_tab.isAncestorOf(page.bridges_box)
    assert page.schedules_tab.isAncestorOf(page.schedules_box), "the schedules moved to their own tab"
    assert not page.sessions_tab.isAncestorOf(page.schedules_box)
    page.show_schedules()
    qapp.processEvents()
    assert tabs.currentWidget() is page.schedules_tab and page.schedules_box.isVisible() and not page.table.isVisible()
    page.show_sessions()
    qapp.processEvents()
    assert tabs.currentWidget() is page.sessions_tab and page.table.isVisible()
    tabs.setCurrentIndex(1)  # the human clicks the tab
    assert page.schedules_box.isVisibleTo(window)


def test_the_tab_chosen_is_kept_for_the_next_start(qapp, env, monkeypatch):  # noqa: F811
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)

    def started():
        made = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
        if made.tray is not None:
            made.tray.hide()
            made.tray = None
        return made

    def ended(made):
        made.shutdown()
        made.close()
        made.deleteLater()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    first = started()
    try:
        first.sandboxes.show_schedules()
        first.state.flush()
    finally:
        ended(first)
    saved = json.loads((env / "config" / "sandbox-ai" / "app" / "state.json").read_text())
    assert saved["tabs-shown"]["sandboxes"] == 1
    assert "tabs" not in saved or isinstance(saved["tabs"], list), "the terminal tabs' list is not touched"
    second = started()
    try:
        assert second.sandboxes.tabs.currentWidget() is second.sandboxes.schedules_tab
        second.sandboxes.show_sessions()
        second.state.flush()
    finally:
        ended(second)
    third = started()
    try:
        assert third.sandboxes.tabs.currentWidget() is third.sandboxes.sessions_tab
    finally:
        ended(third)


def test_every_way_to_a_start_lands_on_the_sessions_tab(qapp, window, env):  # noqa: F811
    fake_run(env, "claude-abc")
    page = shown(window, qapp)
    read_now(page)
    key = next(key for key, i in page.instances.items() if i.name == "claude-abc")
    # Show in Sandboxes & sessions (a terminal tab's menu, the System monitor's Go to Sessions)
    page.show_schedules()
    window.show_instance(key)
    assert page.tabs.currentWidget() is page.sessions_tab and page.current().key == key
    # a row selected from elsewhere (the table's own API)
    page.show_schedules()
    page.table.selectRow(0)
    assert page.tabs.currentWidget() is page.sessions_tab
    # Hosts › Show starts
    page.show_schedules()
    page.show_host("local")
    assert page.tabs.currentWidget() is page.sessions_tab
    # its own refresh does not take the human away from the schedules
    page.show_schedules()
    page.wanted = key
    page.table.clearSelection()
    page.show_instances()
    assert page.tabs.currentWidget() is page.schedules_tab
    # a tour step: the control's tab comes to the front
    window.point_at("sandboxes", "run_label")
    assert page.tabs.currentWidget() is page.sessions_tab
    window.point_at("sandboxes", "schedules_box")
    assert page.tabs.currentWidget() is page.schedules_tab
    window.point_at("sandboxes", "")


def test_the_tours_and_the_help_point_at_controls_that_are_there(qapp, window):  # noqa: F811
    from sandbox_ai_app import help_texts, tours
    page = shown(window, qapp)
    steps = tours.AREA_TOURS["sandboxes"]
    targets = [step.target for step in steps]
    assert "schedules_box" in targets and "run_label" in targets and "session_label" in targets
    assert "devices_label" not in targets, "the While it runs step points at its row now"
    for target in targets:
        assert isinstance(getattr(page, target, None), QtWidgets.QWidget), target
    for attribute, _tip, _how in help_texts.PLACES["sandboxes"]:
        assert isinstance(getattr(page, attribute, None), QtWidgets.QWidget), attribute
    assert not [entry for entry in window.help_missing if entry[0] == "sandboxes"]


# -- the icon rows --

def test_both_groups_are_icon_rows_above_the_table(qapp, window):  # noqa: F811
    page = shown(window, qapp)
    assert page.session_buttons and page.run_buttons
    for button in session_row(page) + run_row(page):
        assert isinstance(button, button_icons.IconButton), widgets.button_text(button)
        assert button.text() == "", f"{button.label!r} shows its text"
        assert not button.icon().isNull() and button.icon_key in button_icons.SVGS, button.label
        assert button.accessibleName(), button.label
        tip = button.shown_tooltip()
        assert button.label.rstrip("…") in QtGui.QTextDocumentFragment.fromHtml(tip).toPlainText(), (button.label, tip)
        assert page.session_row.isAncestorOf(button) if button in session_row(page) else \
            page.run_row.isAncestorOf(button), button.label
        assert button.isVisibleTo(page) or button is page.run_named["browser"] or button in page.run_buttons, \
            button.label
    keys = [button.icon_key for button in session_row(page) + run_row(page)]
    assert len(keys) == len(set(keys)), "every action its own icon"
    drawings = [button_icons.SVGS[key] for key in keys]
    assert len(drawings) == len(set(drawings)), "every icon its own drawing"
    # Session above While it runs, both above the table; a label names each row
    top = lambda w: w.mapTo(page, QtCore.QPoint(0, 0)).y()  # noqa: E731
    assert top(page.session_row) < top(page.run_row) < top(page.table)
    assert top(page.run_row) >= top(page.session_row) + page.session_row.height() - 1
    assert page.session_label.text() == "Session" and page.run_label.text() == "While it runs"
    assert page.session_row.isAncestorOf(page.session_label) and page.run_row.isAncestorOf(page.run_label)
    # the old group boxes are gone
    assert not [box for box in page.findChildren(QtWidgets.QGroupBox) if box.title() in ("Session", "While it runs")]


def test_a_disabled_icon_button_says_its_name_and_why(qapp, window):  # noqa: F811
    page = shown(window, qapp)
    page.table.clearSelection()
    page.update_buttons()
    pause = page.session_buttons["pause"]
    assert not pause.isEnabled()
    plain = QtGui.QTextDocumentFragment.fromHtml(pause.shown_tooltip()).toPlainText()
    assert plain.startswith("Pause") and page.no_start() in plain, plain
    # the reason alone stays the button's own tooltip (as the other pages' tests read it)
    assert pause.toolTip() and "Pause" not in QtGui.QTextDocumentFragment.fromHtml(pause.toolTip()).toPlainText()
    # a tooltip event shows the name too
    shown_tip = []
    original = QtWidgets.QToolTip.showText
    try:
        QtWidgets.QToolTip.showText = lambda *args, **kw: shown_tip.append(args[1])
        event = QtGui.QHelpEvent(QtCore.QEvent.Type.ToolTip, QtCore.QPoint(2, 2), pause.mapToGlobal(QtCore.QPoint(2, 2)))
        QtWidgets.QApplication.sendEvent(pause, event)
    finally:
        QtWidgets.QToolTip.showText = original
    assert shown_tip and "Pause" in shown_tip[0]


def test_the_icons_follow_the_theme(qapp, window):  # noqa: F811
    page = shown(window, qapp)
    button = page.run_named["allow"]

    def ink(icon_button, mode=QtGui.QIcon.Mode.Normal):
        image = icon_button.icon().pixmap(QtCore.QSize(18, 18), mode).toImage()
        colours = {}
        for x in range(image.width()):
            for y in range(image.height()):
                pixel = image.pixelColor(x, y)
                if pixel.alpha() > 200:
                    colours[pixel.name()] = colours.get(pixel.name(), 0) + 1
        return max(colours, key=colours.get) if colours else None
    try:
        for mode in ("light", "dark"):
            theme.apply(mode, qapp)
            qapp.processEvents()
            assert ink(button) == QtGui.QColor(theme.colors()["text"]).name(), mode
            assert ink(button, QtGui.QIcon.Mode.Disabled) == QtGui.QColor(theme.colors()["disabled"]).name(), mode
            assert ink(page.stop_all_button) == QtGui.QColor(theme.colors()["danger"]).name(), "a danger button"
    finally:
        theme.apply("system", qapp)


def test_every_drawing_renders(qapp):
    for key, drawing in button_icons.SVGS.items():
        assert drawing.startswith("<svg") and "currentColor" in drawing, key
        image = button_icons.pixmap(key, 24, "#000000").toImage()
        assert any(image.pixelColor(x, y).alpha() > 0 for x in range(image.width()) for y in range(image.height())), key


def test_the_menu_keeps_the_texts(qapp, window):  # noqa: F811
    page = shown(window, qapp)
    assert widgets.button_text(page.transfer_buttons["export"]) == "Export…"
    assert widgets.button_text(page.stop_all_button) == "Stop all…"
    assert widgets.button_text(page.run_named["allow"]) == "Allow…"
    entries = [action.text() for action in widgets.menu_of(page.table).actions() if action.text()]
    for text in ("Attach", "Resume with…", "Export…", "Allow…", "Connection log", "Secrets and logins…", "Stop all…"):
        assert text in entries, (text, entries)


def test_the_setting_brings_the_texts_back_at_once(qapp, window):  # noqa: F811
    page = shown(window, qapp)
    assert backend.SETTINGS["button_texts"] is False
    narrow = page.session_row.sizeHint().width()
    window.resize(1280, 800)
    qapp.processEvents()
    high = page.height()  # (the page in its scroll area: as high as the window's room, or as it needs)
    dialog = window.settings_dialog()
    try:
        box = dialog.fields["button_texts"]
        assert box.text() == "Show button texts beside the icons" and not box.isChecked()
        box.setChecked(True)
        qapp.processEvents()
        assert backend.setting("button_texts") is True, "kept in the settings"
        for button in session_row(page) + run_row(page):
            assert button.text() == button.label and not button.icon().isNull(), button.label
        assert page.run_named["allow"].text() == "Allow…" and page.transfer_buttons["export"].text() == "Export…"
        plain = QtGui.QTextDocumentFragment.fromHtml(page.run_named["allow"].shown_tooltip()).toPlainText()
        assert not plain.startswith("Allow…"), "with its text shown, the tooltip says only what it does"
        assert page.session_row.sizeHint().width() > narrow
        # a text that changes (another host) is shown as it changes
        page.update_credentials_button(None)
        assert page.credentials_button.text() == "Secrets and logins…"
        box.setChecked(False)
        qapp.processEvents()
        assert all(button.text() == "" for button in session_row(page) + run_row(page))
        assert page.height() == high, "the room of the texts goes with them (no scroll bar left behind)"
        assert page.credentials_button.label == "Secrets and logins…"
    finally:
        dialog.deleteLater()


def test_german(qapp, env, monkeypatch):  # noqa: F811
    from sandbox_ai_app import settings, terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language("de")
    made = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    try:
        page = made.sandboxes
        assert [page.tabs.tabText(i).replace("&&", "&") for i in range(page.tabs.count())] == \
            ["Sitzungen & Sandboxen", "Zeitpläne"]
        assert page.session_label.text() == "Sitzung" and page.run_label.text() == "Während sie läuft"
        assert widgets.button_text(page.run_named["allow"]) == "Erlauben…"
        dialog = settings.SettingsDialog(made.sai, made)
        assert dialog.fields["button_texts"].text() == "Beschriftungen der Schaltflächen neben den Symbolen zeigen"
        dialog.deleteLater()
        for key in ("Sessions & sandboxes", "Show button texts beside the icons"):
            assert key in i18n.TEXTS and i18n.TEXTS[key]["de"], key
    finally:
        i18n.set_language("en")
        made.shutdown()
        made.close()
        made.deleteLater()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def settle(qapp, rounds=5):
    """The layouts done: a row that wraps (the button texts on) asks its parents for room again, one event loop pass
    at a time - measured after one pass, a label of the Schedules tab was still at its old height in one full run
    (round 2, 2026-11-22: "needs 112 px high, has 32", passed alone)."""
    for _ in range(rounds):
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.LayoutRequest)
        qapp.processEvents()


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", ["light", "dark", "win311"])
def test_nothing_is_clipped_on_either_tab_at_the_smallest_window(qapp, env, monkeypatch, language, mode):  # noqa: F811
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    from test_layout import clipped
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    made = None
    try:
        theme.apply(mode, qapp)
        made = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
        made.resize(made.minimumSizeHint())
        made.show()
        made.show_page("sandboxes", raise_window=False)
        qapp.processEvents()
        page = made.sandboxes
        found = []
        for texts in (False, True):
            button_icons.set_texts_shown(texts)
            for index in range(page.tabs.count()):
                page.tabs.setCurrentIndex(index)
                settle(qapp)
                found += [f"texts {texts}, tab {index}: {item}" for item in clipped(page)]
                right = page.width()
                for button in session_row(page) + run_row(page):
                    if button.isVisibleTo(page):
                        edge = button.mapTo(page, QtCore.QPoint(button.width(), 0)).x()
                        if edge > right + 1:
                            found.append(f"texts {texts}: {button.label!r} beyond the page's edge ({edge} > {right})")
        assert made.width() <= max(made.minimumSizeHint().width(), 1000) + 2, "the rows wrap; the window is not widened"
        assert found == [], "\n".join(found)
    finally:
        button_icons.set_texts_shown(False)
        i18n.set_language("en")
        theme.apply("system", qapp)
        if made is not None:
            made.shutdown()
            made.close()
            made.deleteLater()
            QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
