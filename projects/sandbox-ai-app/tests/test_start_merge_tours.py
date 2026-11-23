"""The tours and the merged Start page: every tour that shows or names the Start page or the Board - the guided tour,
the two views' area tours, the tours through a task - leads to that page in the view its step is about and has its
texts in English and German (the window, offscreen). That every step's control is there and visible in its view, in
both languages, test_tours.py checks for every tour of every page."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, guide, i18n, start_link, tours, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
VIEWS = start_link.VIEW_KEYS


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_the_guided_tour_has_a_step_for_each_view_and_names_the_switch(window, qapp):
    steps = {step.page: step for step in guide.TOUR if step.page in VIEWS}
    assert set(steps) == set(VIEWS)
    for page, step in steps.items():
        for language in ("en", "de"):
            i18n.set_language(language)
            try:
                title, text, try_ = i18n.tr(step.title), i18n.tr(step.text), i18n.tr(step.try_)
            finally:
                i18n.set_language("en")
            assert title and text and try_
            assert "Start" in title or "Start" in text, (page, language)
            assert ("Expert" in text or "Experte" in text) and "Board" in text, (page, language)
        assert i18n.TEXTS[step.text]["de"] != step.text and i18n.TEXTS[step.try_]["de"] != step.try_
    window.start_tour(index=[step.page for step in guide.TOUR].index("launch"))
    qapp.processEvents()
    assert window.current_page() == "launch" and window.view_switch.isVisible()
    assert window.view_switch.view() == "launch"
    window.tour.go([step.page for step in guide.TOUR].index("board"))
    qapp.processEvents()
    assert window.current_page() == "board" and window.view_switch.view() == "board"
    assert window.start_view == "board", "a tour shows a view; it does not choose the one the page opens in"


def test_the_tours_of_both_views_show_the_switch_and_what_is_new(window):
    for page in VIEWS:
        targets = [step.target for step in tours.AREA_TOURS[page]]
        assert "view_switch" in targets and "start_controls" in targets and "dry_button" in targets, page
        first = tours.AREA_TOURS[page][0]
        assert "Start page" in first.text.en and "Seite Start" in first.text.de
        assert "Expert" in first.text.en and "Experte" in first.text.de
    assert {"preview", "line_note", "undo_redo"} <= {step.target for step in tours.AREA_TOURS["launch"]}
    assert any(step.page == "board" and step.target == "view_switch" for step in tours.TASK_TOURS["first-start"].steps)
    assert any(step.page == "launch" and step.target == "preview" for step in tours.TASK_TOURS["own-construct"].steps)


def test_every_text_of_the_steps_on_the_start_page_is_in_both_languages():
    for page in VIEWS:
        for step in tours.AREA_TOURS[page]:
            for text in (step.title, step.text, step.try_):
                assert text.en.strip() and text.de.strip() and text.en != text.de
    for tour in tours.TASK_TOURS.values():
        for step in tour.steps:
            if step.page in VIEWS:
                for text in (step.title, step.text, step.try_):
                    assert text.en.strip() and text.de.strip() and text.en != text.de


def test_the_tours_about_the_page_are_found_by_what_it_is_called_now(window):
    """Tour about…: "expert", "command line" and "Befehlszeile" find the tours of the Start page."""
    from sandbox_ai_app import tour_search
    for query in ("expert view", "command line", "Befehlszeile", "Experte", "board switch"):
        hits = [hit.ident for hit in tour_search.search(query)[:6]]
        assert {"area:launch", "area:board"} & set(hits), (query, hits)
