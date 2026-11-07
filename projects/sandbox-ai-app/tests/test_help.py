"""The (?) buttons, the Help area and the tours through a task: every explanation in both languages with a Help
section that exists, a (?) on every page and for every part of the board, More in Help opening the right section,
a searchable Help page in English and German, and tours whose steps point at controls that exist and are ticked
off when done."""
import ast
from pathlib import Path
import string

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import board as B, help_content as C, help_texts as H, help_view, i18n, tours, ui  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"


def fields(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


def literal_tip_ids():
    """The tip ids written as HelpButton("...") in the package."""
    found = set()
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == \
                    "HelpButton" and node.args and isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
    return found


def test_every_explanation_has_both_languages_and_a_help_section():
    assert len(H.TIPS) >= 90
    for tip in H.TIPS.values():
        for text in (tip.title, tip.what) + ((tip.does,) if tip.does else ()):
            assert text.en.strip() and text.de.strip(), tip.id
            assert fields(text.en) == fields(text.de) == [], tip.id
        assert tip.what.en != tip.what.de, tip.id
        assert tip.section in C.BY_ID, (tip.id, tip.section)


def test_every_help_section_has_both_languages_and_valid_links():
    ids = [s.id for s in C.SECTIONS]
    assert len(ids) == len(set(ids)) >= 45
    for section in C.SECTIONS:
        assert section.group in ("app", "concepts"), section.id
        for text in (section.title, section.body):
            assert text.en.strip() and text.de.strip(), section.id
        assert section.body.en != section.body.de, section.id
        assert all(s in C.BY_ID for s in section.see), section.id
        for tour in section.tours:
            assert tour in tours.TASK_TOURS or (tour.startswith("area:") and tour[5:] in tours.AREA_TOURS), tour
    pages = {key for key, _ in ui.PAGES}
    assert set(H.PAGE_SECTIONS) == pages and set(H.PLACES) == pages
    for concept in ("sandbox", "sessions", "network", "proxy", "approvals", "taint", "canaries", "presets", "kits",
                    "rulesets", "mcp", "devices", "display", "vpn-tor", "llm", "web", "hosts", "teams",
                    "memory-watch", "privacy-check", "releases-update"):
        assert C.BY_ID[concept].group == "concepts", concept


def test_every_page_part_area_and_settings_tab_has_an_explanation():
    assert {f"page.{key}" for key, _ in ui.PAGES} <= set(H.TIPS)
    assert {f"kind.{key}" for key in B.KINDS} <= set(H.TIPS)
    placed = {tip for places in H.PLACES.values() for _, tip, _ in places}
    assert placed <= set(H.TIPS) and all(how in ("after", "corner", "group")
                                         for places in H.PLACES.values() for _, _, how in places)
    used = placed | literal_tip_ids() | set(H.SETTINGS_TABS) | {f"page.{key}" for key, _ in ui.PAGES} | \
        {f"kind.{key}" for key in B.KINDS}
    assert literal_tip_ids() <= set(H.TIPS)
    assert set(H.TIPS) == used, f"explanations no (?) shows: {sorted(set(H.TIPS) - used)}"


def test_every_page_has_its_question_marks_and_they_explain(window, qapp):
    window.resize(1360, 900)
    window.show()
    assert window.help_missing == []
    for key, _ in ui.PAGES:
        buttons = getattr(window, key).findChildren(help_view.HelpButton)
        assert buttons, f"no (?) on the page {key}"
        assert all(b.tip_id in H.TIPS for b in buttons), key
    window.show_page("connections", raise_window=False)
    assert window.page_help.tip_id == "page.connections" and "Connections" in window.page_help.toolTip()
    window.show_page("board", raise_window=False)  # a (?) is clicked where it is seen
    qapp.processEvents()
    button = next(b for b in window.help_buttons["board"] if b.tip_id == "board.canvas")
    assert button.isVisible()
    popover = button.explain()  # (offscreen Qt closes a popup at the next turn of the event loop: no grabs there)
    assert popover.isVisible() and popover.title.text() == "The board"
    assert popover.cli.text() == "Command line: .sandbox-ai.json" and popover.more.text() == "More in Help"
    assert popover.height() >= popover.layout().totalHeightForWidth(popover.width()), "no text is cut off"
    popover.more.click()
    qapp.processEvents()
    assert window.current_page() == "help" and window.help.shown == "page-board"
    assert "Drag a part from the parts list" in window.help.text.toPlainText()


def test_more_in_help_from_a_modal_dialog_opens_a_help_window(window, qapp):
    dialog = QtWidgets.QDialog(window)
    dialog.setModal(True)
    dialog.show()
    button = help_view.HelpButton("gallery.mine", dialog)
    shown = help_view.open_section("app-presets-gallery", button)
    assert isinstance(shown, help_view.HelpDialog) and shown.page.shown == "app-presets-gallery"
    assert not shown.page.tours.isVisible(), "no tours from a dialog: the window cannot be used meanwhile"
    shown.close()
    dialog.close()


def test_the_help_page_searches_both_languages_and_switches(window, qapp):
    page = window.show_help("network")
    assert window.current_page() == "help" and page.shown == "network"
    page.search.setText("canary")
    visible = [page.index.item(r).data(0x0100) for r in range(page.index.count()) if not page.index.item(r).isHidden()]
    assert "canaries" in visible and "page-kits" not in visible
    page.search.setText("Köderdateien")  # German words find too
    assert page.shown == "canaries"
    page.search.setText("zzzz-nothing")
    assert page.empty.isVisible() and "zzzz-nothing" in page.empty.text()
    page.search.setText("")
    assert not page.empty.isVisible()
    page.show_section("taint")
    page.language.setCurrentIndex(page.language.findData("de"))
    assert "Der Taint des Laufs" in page.text.toPlainText() and page.items["taint"].text().strip() == \
        "Der Taint des Laufs"
    page.language.setCurrentIndex(0)
    assert "The run's taint" in page.text.toPlainText()
    page.link("section:rulesets")
    assert page.shown == "rulesets"
    page.show_section("tours")
    assert all(tours.say(t.title) in page.text.toPlainText() for t in tours.TASK_TOURS.values())
    assert not page.show_section("no-such-section")


def test_help_and_tours_from_the_menu(window):
    assert [k.toString() for k in window.actions["help"].shortcuts()] == ["Shift+F1"]
    window.actions["help"].trigger()
    assert window.current_page() == "help"
    window.actions["task-tour:approvals-network"].trigger()
    assert window.task_tour.tour.id == "approvals-network" and window.current_page() == "launch"


def test_every_task_tour_step_points_at_a_control_and_its_check_is_known(window):
    from PySide6.QtWidgets import QWidget
    assert set(tours.TASK_TOURS) >= {"first-start", "own-construct", "approvals-network", "presets-gallery",
                                     "help-and-tips"}
    pages = {key for key, _ in ui.PAGES}
    for tour in tours.TASK_TOURS.values():
        for text in (tour.title, tour.summary):
            assert text.en.strip() and text.de.strip() and text.en != text.de, tour.id
        for step in tour.steps:
            assert step.page in pages, (tour.id, step.page)
            if step.target:
                assert isinstance(getattr(getattr(window, step.page), step.target, None), QWidget), \
                    (tour.id, step.target)
            for text in (step.title, step.text, step.try_):
                assert text.en.strip() and text.de.strip(), (tour.id, text)
            if step.check:
                kind = step.check.partition(":")[0]
                assert kind in ("project", "page", "board-has", "board-connected", "board-option", "board-no-errors",
                                "board-applied", "preset-mine", "help-search", "tip-opened"), step.check
                assert window.tour_check(step.check) in (True, False)


def test_the_construct_tour_ticks_steps_off_as_they_are_done(window, qapp, tmp_path):
    settings = window.board.settings
    window.board.settings = lambda: {**settings(), "guide_nodes": False}  # no node guide dialog in a test
    window.resize(1360, 900)
    window.show()
    window.help.tour_requested.emit("own-construct")  # from the Help page
    panel = window.task_tour
    assert panel.where.text() == "Your own construct, saved as a preset · 1 of 11"
    assert window.current_page() == "board" and window.tour_dock.isVisible()
    panel.go(1)  # add a mount
    qapp.processEvents()
    assert window.spotlight.target is window.board.palette and not panel.done.isVisible()
    window.board.drop(("part", "mount"), window.board.view.mapToScene(window.board.view.viewport().rect().center()))
    panel.look()
    assert panel.done.isVisible() and panel.done.text() == "✔ Done: go on with Next."
    panel.next.click()
    mount = window.board.board.first("mount")
    assert panel.index == 2 and window.tour_check("board-connected:mount") == window.board.board.connected(mount)
    panel.go(len(panel.steps) - 1)
    assert panel.next.text() == "Finish"
    panel.next.click()
    assert not window.tour_dock.isVisible()


def test_the_help_page_starts_area_tours_and_the_settings_explain_each_tab(window, qapp):
    window.start_any_tour("area:help")
    assert window.area_tour.page == "help" and window.current_page() == "help"
    from sandbox_ai_app.settings import SettingsDialog
    dialog = SettingsDialog(window.sai, window)
    assert dialog.tabs.count() == len(H.SETTINGS_TABS)
    for index, tip in enumerate(H.SETTINGS_TABS):
        dialog.tabs.setCurrentIndex(index)
        assert dialog.tab_help.tip_id == tip
    dialog.close()


def test_the_gallery_explains_itself(window):
    from sandbox_ai_app.preset_gallery import PresetGallery
    gallery = PresetGallery(window.sai, None, "claude", {}, window.board.board, window)
    assert {b.tip_id for b in gallery.findChildren(help_view.HelpButton)} == {"gallery.cards", "gallery.mine"}
    gallery.close()


def test_the_question_marks_follow_the_language(window):
    i18n.set_language("de")
    try:
        assert H.say(H.tip("kind.mount").title) == "Einbindung"
        button = help_view.HelpButton("board.palette")
        assert button.toolTip() == "Was ist das? Teile"
        popover = button.explain()
        assert popover.more.text() == "Mehr in der Hilfe"
        popover.close()
    finally:
        i18n.set_language("en")
