"""The Help menu in few entries (the user 2026-11-24: "Hier haben wir x mal Tour, Hilfen ... unter 1-2 Punkten bzw.
Untermenüs subsummieren, z.B. Tour… -> oder Hilfe…"): the tours under Tours ▸, the videos under Videos ▸; every entry
is still there, and every key still works with its entry in a submenu."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from sandbox_ai_app import backend  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def help_menu(window):
    return next(action.menu() for action in window.menuBar().actions() if action.text() == "&Help")


def entries(menu):
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def submenu(menu, title):
    return next(action.menu() for action in menu.actions() if action.text() == title and action.menu() is not None)


def test_the_tours_and_videos_are_one_submenu_each(window):  # noqa: F811
    menu = help_menu(window)
    top = entries(menu)
    assert top == ["Help: every topic, in English and German", "Tours", "Videos", "Check for updates",
                   "Update sandbox-ai…", "Show the log", "Read the README", "About sandbox-ai-app"], top
    tours = submenu(menu, "Tours")
    assert entries(tours) == ["Tour of this page", "Guided tour", "Tour about…", "Area tours", "Tours through a task"]
    assert submenu(tours, "Area tours").actions() and submenu(tours, "Tours through a task").actions()
    assert entries(submenu(menu, "Videos")) == ["Watch the video about this page", "Videos about the pages",
                                                "Watch the intro again"]
    # Nothing lost: every entry of before, with its keys.
    keys = {key: [k.toString() for k in window.actions[key].shortcuts()] for key in (
        "help", "tour", "area-tour", "tour-about", "page-video", "videos", "check-updates", "self-update", "log",
        "intro", "about", "readme")}
    assert keys["help"] == ["Shift+F1"] and keys["area-tour"] == ["F1"] and keys["tour-about"] == ["Ctrl+F1"]
    assert keys["page-video"] == ["Shift+F2"]
    assert any(key.startswith("area-tour:") for key in window.actions)
    assert any(key.startswith("task-tour:") for key in window.actions)


def test_the_keys_of_the_entries_in_the_submenus_still_work(window, monkeypatch, qapp):  # noqa: F811
    from PySide6.QtTest import QTest
    called = []
    monkeypatch.setattr(window, "start_area_tour", lambda page=None: called.append(("area-tour", page)))
    monkeypatch.setattr(window, "tour_about", lambda text="": called.append(("tour-about", text)))
    monkeypatch.setattr(window, "watch_video", lambda page=None: called.append(("video", page)))
    window.show()
    window.activateWindow()
    qapp.processEvents()
    window.show_page("files")
    window.files.setFocus()
    for key, modifiers in ((Qt.Key.Key_F1, Qt.KeyboardModifier.NoModifier),
                           (Qt.Key.Key_F1, Qt.KeyboardModifier.ControlModifier),
                           (Qt.Key.Key_F2, Qt.KeyboardModifier.ShiftModifier)):
        QTest.keyClick(window.windowHandle(), key, modifiers)
        qapp.processEvents()
    assert [what for what, _ in called] == ["area-tour", "tour-about", "video"], called
    window.hide()
