"""Tours of single areas: every page has one, in both languages, every step points at a control that exists, and
the window starts them from Help, F1, the navigation and the guided tour."""
import string

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import i18n, tours, ui  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests


def fields(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


def test_every_page_has_a_tour_in_both_languages():
    assert set(tours.AREA_TOURS) == {key for key, _ in ui.PAGES}
    for page, steps in tours.AREA_TOURS.items():
        assert steps, page
        for step in steps:
            for text in (step.title, step.text, step.try_):
                assert text.en.strip() and text.de.strip(), (page, text)
                assert text.en != text.de or text.en in ("Kits", "Live"), (page, text)  # the same word in both
                assert fields(text.en) == fields(text.de), (page, text)
    i18n.set_language("de")
    try:
        assert tours.say(tours.AREA_TOURS["images"][1].title) == "Bauen"
    finally:
        i18n.set_language("en")


def test_every_control_a_step_points_at_is_there(window):
    from PySide6.QtWidgets import QWidget
    for page, steps in tours.AREA_TOURS.items():
        for step in steps:
            if step.target:
                assert isinstance(getattr(getattr(window, page), step.target, None), QWidget), (page, step.target)


def test_an_area_tour_outlines_each_control_and_finishes(window, qapp):
    window.resize(1280, 860)
    window.show()
    window.start_area_tour("images")
    panel = window.area_tour
    assert window.current_page() == "images" and window.tour_dock.isVisible()
    assert panel.where.text() == "Tour of Images · 1 of 5" and panel.title.text() == "The images"
    qapp.processEvents()
    spot = window.spotlight
    assert spot.isVisible() and spot.target is window.images.images
    panel.go(3)  # the extension's tabs: on the tab widget of the lower half
    qapp.processEvents()
    assert spot.target is window.images.editor_tabs and window.images.tabs.currentIndex() == 1  # its tab shown
    target = window.images.editor_tabs
    corner = target.mapTo(window, target.rect().topLeft())
    assert spot.geometry().contains(corner)  # around the control
    panel.next.click()
    assert panel.next.text() == "Finish"
    panel.next.click()
    qapp.processEvents()
    assert not window.tour_dock.isVisible() and not spot.isVisible()


def test_tours_start_from_help_f1_the_navigation_and_the_guided_tour(window):
    window.show_page("connections", raise_window=False)
    window.actions["area-tour"].trigger()  # F1
    assert window.area_tour.page == "connections"
    assert [key.toString() for key in window.actions["area-tour"].shortcuts()] == ["F1"]
    window.actions["area-tour:hosts"].trigger()
    assert window.area_tour.page == "hosts" and window.current_page() == "hosts"
    window.start_tour(2)  # the guided tour's step "Sandboxes & sessions"
    window.tour.area.click()
    assert window.area_tour.page == "sandboxes" and not window.tour.isVisible()
