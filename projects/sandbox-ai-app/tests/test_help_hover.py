"""The setting help_buttons ("Show the (?) help buttons") reaches every (?) of the app, and the terms that have a help
text explain themselves on hover, with or without the (?)s.

The walk: the window starts with the setting off; every page, every tab of a page and the dialogs that carry (?)s
are visited, and every (?) found - a widget showing "?" or named helpButton, or anything that paints
help_view.paint_mark (the board's nodes and its parts list paint theirs) - must be hidden. Switched on live, they are
all there again; off again, gone, also in dialogs opened after the switch."""
import inspect

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QApplication, QDialog, QGroupBox, QLabel, QTabWidget, QWizard,
)

from sandbox_ai_app import backend, help_texts as H, help_view, i18n, ui, widgets  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests


@pytest.fixture
def off_at_start(env):  # noqa: F811
    """The setting is off before the window is made (it is read at the start)."""
    backend.SandboxAI().save_setting("help_buttons", False)
    yield
    help_view.set_shown(True)


@pytest.fixture
def painted(monkeypatch):
    """Who painted a (?) since the list was emptied: help_view.paint_mark's callers."""
    found = []
    real = help_view.paint_mark

    def spy(painter, rect, lit=False, font=None):
        caller = inspect.stack()[1]
        found.append(type(caller.frame.f_locals.get("self")).__name__)
        return real(painter, rect, lit, font)
    monkeypatch.setattr(help_view, "paint_mark", spy)
    return found


@pytest.fixture
def dialogs(monkeypatch):
    """exec() shows the dialog without waiting: the walk looks into it."""
    opened = []

    def show(dialog):
        opened.append(dialog)
        dialog.show()
        return 0
    monkeypatch.setattr(QDialog, "exec", show)
    monkeypatch.setattr(QWizard, "exec", show)
    yield opened
    for dialog in opened:
        dialog.close()


def marks(painted):
    """Every (?) on the screen: widgets that show one, and the painters of one (each visible window is painted)."""
    found = []
    for top in QApplication.topLevelWidgets():
        if not top.isVisible():
            continue
        for widget in [top, *top.findChildren(QtWidgets.QWidget)]:
            if not widget.isVisible():
                continue
            text = widget.text() if isinstance(widget, (QAbstractButton, QLabel)) else ""
            if widget.objectName() == "helpButton" or text.strip() in ("?", "(?)"):
                found.append(f"{type(widget).__name__} in {type(widget.window()).__name__}")
        painted.clear()
        top.grab()
        found += [f"painted by {who} in {type(top).__name__}" for who in sorted(set(painted))]
    return found


def walk(window, qapp, painted):
    """Every page and every tab of it: the (?)s seen there."""
    seen = []
    for key, _ in ui.PAGES:
        window.show_page(key, raise_window=False)
        qapp.processEvents()
        for tabs in getattr(window, key).findChildren(QTabWidget):
            for index in range(tabs.count()):
                tabs.setCurrentIndex(index)
                qapp.processEvents()
                seen += [f"{key}, tab {index}: {mark}" for mark in marks(painted)]
        seen += [f"{key}: {mark}" for mark in marks(painted)]
    return sorted(set(seen))


def open_dialogs(window, qapp):
    window.open_settings()
    window.board.open_gallery()
    window.board.open_wizard(ask=False)
    window.help.tour_about("network")
    qapp.processEvents()


def test_every_question_mark_follows_the_setting_on_every_page_and_dialog(off_at_start, window, qapp, painted,
                                                                          dialogs):
    assert not help_view.buttons_shown(), "read at the start"
    window.resize(1360, 900)
    window.show()
    window.board.add_node("mount")  # a node with its (?) in the title band
    assert walk(window, qapp, painted) == []
    open_dialogs(window, qapp)
    assert len(dialogs) >= 3 and marks(painted) == []
    for dialog in dialogs:
        dialog.close()
    dialogs.clear()

    help_view.set_shown(True)  # live: every (?) is back
    window.show_page("board", raise_window=False)
    qapp.processEvents()
    shown = " ".join(marks(painted))
    assert "HelpButton" in shown and "NodeHelpMark" in shown and "PartsDelegate" in shown, shown
    hidden = [b.tip_id for b in window.help_buttons["board"] if not b.isVisible() and b.parentWidget().isVisible()]
    assert hidden == [], "a (?) whose area is shown is shown"
    open_dialogs(window, qapp)
    assert all(b.isVisible() for d in dialogs for b in d.findChildren(help_view.HelpButton)), "in the dialogs too"

    help_view.set_shown(False)  # and gone again, in the dialogs open now and in those opened later
    qapp.processEvents()
    assert marks(painted) == []
    for dialog in dialogs:
        dialog.close()
    open_dialogs(window, qapp)
    assert marks(painted) == []
    assert walk(window, qapp, painted) == []


def test_the_parts_list_without_question_marks_gives_the_title_the_row_and_ignores_the_corner(window, qapp):
    palette = window.board.palette
    window.resize(1360, 900)
    window.show()
    window.show_page("board", raise_window=False)
    qapp.processEvents()
    rect = palette.visualItemRect(palette.item(0))
    delegate = palette.itemDelegate()
    assert not delegate.mark(rect).isNull()
    help_view.set_shown(False)
    try:
        assert delegate.mark(rect).isNull(), "no (?) to click either"
        assert not delegate.mark(rect).contains(QPoint(rect.right() - 8, rect.center().y()))
    finally:
        help_view.set_shown(True)


def test_a_table_keeps_room_for_its_question_mark_only_while_it_is_shown(window):
    header = window.hosts.table.horizontalHeader()
    assert header.property("helpRoom") is True
    help_view.set_shown(False)
    try:
        assert header.property("helpRoom") is False
    finally:
        help_view.set_shown(True)
    assert header.property("helpRoom") is True


def header_tip(view, title):
    model = view.model()
    for section in range(model.columnCount()):
        if model.headerData(section, Qt.Orientation.Horizontal) == title:
            return model.headerData(section, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole) or ""
    raise AssertionError(f"no column {title}")


def make_window(monkeypatch):
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    made = ui.MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if made.tray is not None:
        made.tray.hide()
        made.tray = None
    return made


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("buttons", [True, False])
def test_terms_explain_themselves_on_hover_with_or_without_question_marks(qapp, env, monkeypatch, language,  # noqa: F811
                                                                          buttons, dialogs):
    backend.SandboxAI().save_setting("help_buttons", buttons)
    i18n.set_language(language)
    window = make_window(monkeypatch)
    try:
        assert help_view.buttons_shown() is buttons
        tr = i18n.tr

        def says(tip_id):  # the first words of a (?)'s own explanation
            return H.say(H.tip(tip_id).what).split()[0]

        # the navigation: each page's title, and what it is for
        nav = window.nav
        for key, row in window.nav_rows.items():
            assert "\n" in nav.item(row).toolTip() and says(f"page.{key}") in nav.item(row).toolTip(), key
        # the Start page: the controls with a (?), their labels, every option of the form
        launch = window.launch
        assert says("launch.agent") in launch.agent.toolTip() or launch.agent.toolTip()
        assert all(label.toolTip() for label in launch.form.labels.values()) if hasattr(launch.form, "labels") else True
        # column titles, on many pages
        samples = [(window.hosts.table, "Answers"), (window.kits.table, "Brings"), (window.approvals.open, "Refused in"),
                   (window.mcp.servers, "Serves"), (window.mcp.catalog, "Signature"), (window.connections.table, "Method"),
                   (window.sandboxes.table, "Backend"), (window.credentials.secrets, "Kept in"),
                   (window.images.images, "Built"), (window.team.table, "Members"), (window.llm.servers, "VPN")]
        for view, title in samples:
            tip = header_tip(view, tr(title))
            assert H.say(H.term(title)).split()[0] in tip, (title, tip)
        # group titles: their (?)'s explanation over the title, nowhere else in the box
        group = help_view.group_of(window.approvals.open)
        assert says("approvals.open") in help_view.hover_text(group) and not group.toolTip()
        # the board: each part in the palette and on the board
        palette = window.board.palette
        assert all(palette.item(row).toolTip() for row in range(palette.count()))
        assert all(item.toolTip() for item in window.board.items.values())
        # the settings: every tab and every setting
        settings = window.settings_dialog()
        assert all(settings.tabs.tabToolTip(i) for i in range(settings.tabs.count()))
        for key, field in settings.fields.items():
            assert help_view.hover_text(field), key
        assert H.say(H.SETTING_TIPS["help_buttons"]).split()[0] in settings.fields["help_buttons"].toolTip()
        settings.close()
        # a dialog made later (the LLM server's): its terms too, once it has the focus
        from sandbox_ai_app.llm_page import LlmDialog
        dialog = LlmDialog(window, "this computer")
        dialog.show()
        dialog.activateWindow()
        dialog.setFocus()
        next(w for w in dialog.findChildren(QtWidgets.QLineEdit) if w.isVisible()).setFocus()
        qapp.processEvents()
        labels = {label.text(): label for label in dialog.findChildren(QLabel)}
        assert labels[tr("Default model")].toolTip(), "explained on hover"
        dialog.close()
    finally:
        i18n.set_language("en")
        help_view.set_shown(True)
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.sendPostedEvents(None, 52)  # QEvent.DeferredDelete


def test_short_explanations_are_one_or_two_sentences():
    for ident in H.TIPS:
        for language in ("en", "de"):
            text = help_view.summary(ident, language)
            assert text and len(text) <= help_view.HOVER_LIMIT + 2, (ident, text)
    for key, text in {**H.TERMS, **H.SETTING_TIPS}.items():
        assert text.en.strip() and text.de.strip() and len(text.en) <= 160 and len(text.de) <= 170, key
    i18n.set_language("de")
    try:
        assert H.term("Signatur") is H.TERMS["Signature"] and H.term("&Signatur:") is H.TERMS["Signature"]
    finally:
        i18n.set_language("en")
    assert H.term("nothing like this") is None
