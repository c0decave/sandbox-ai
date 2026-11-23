"""What one test leaves for the next (round 2, 2026-11-22: test_responsive::test_stop_runs_in_the_background and
test_video_clicks::test_from_a_modal_dialog... failed in long runs only; test_presets.py and test_files_review_p13.py
in a CI part's order): conftest's nothing_left_running fails a test that leaves a modal dialog open or a call running
in the background, and app_tests_first keeps the app's own test modules ahead of sandbox-ai's on sys.path. Here the
pieces they are made of."""
import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

import conftest  # noqa: E402
from sandbox_ai_app import widgets  # noqa: E402

HERE = Path(__file__).resolve().parent


def test_a_modal_dialog_left_open_is_found_and_a_closed_one_is_not(qapp):
    owner = QtWidgets.QWidget()
    owner.show()
    dialog = QtWidgets.QDialog(owner)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.show()
    qapp.processEvents()
    modeless = QtWidgets.QDialog(owner)
    modeless.show()
    try:
        found = conftest.open_modal_dialogs()
        assert dialog in found and modeless not in found
    finally:
        dialog.close()
        modeless.close()
        owner.close()
    qapp.processEvents()
    assert dialog not in conftest.open_modal_dialogs()


def test_a_call_still_running_is_found_and_one_that_ended_is_not(qapp, wait):
    gate, answered = threading.Event(), []
    widgets.background(lambda: gate.wait(10), done=lambda result, error: answered.append(result),
                       what="a read the test holds")
    assert any(conftest.call_words(call) == "a read the test holds" for call in conftest.running_calls())
    gate.set()
    assert wait(lambda: answered)
    assert not any(conftest.call_words(call) == "a read the test holds" for call in conftest.running_calls())


def test_a_thread_of_the_app_is_found_while_it_runs(qapp):
    gate = threading.Event()

    def work():  # (as a thread of files_page or state.py runs: a function of the app's)
        gate.wait(10)
    work.__module__ = "sandbox_ai_app.stand_in"
    thread = threading.Thread(target=work, name="files-look-up", daemon=True)
    thread.start()
    try:
        assert thread in conftest.app_threads()
    finally:
        gate.set()
        thread.join(10)
    assert thread not in conftest.app_threads()


def test_the_apps_test_modules_come_first_on_the_path(monkeypatch):
    core = HERE.parents[2] / "tests"
    if not (core / "test_presets_all.py").is_file():
        pytest.skip("no sandbox-ai checkout beside the app")
    monkeypatch.setattr(sys, "path", [str(core), *sys.path])  # (as test_credentials_verify_a_app.py does)
    monkeypatch.delitem(sys.modules, "test_presets_all", raising=False)
    assert Path(importlib.util.find_spec("test_presets_all").origin).parent == core  # (the core suite's: wrong)
    conftest.app_tests_first()
    assert Path(importlib.util.find_spec("test_presets_all").origin).parent == HERE
    assert str(core) in sys.path  # (live_fakes, release_kit stay importable)
    assert os.path.samefile(sys.path[0], HERE)


def test_a_theme_chosen_last_is_left_pending(qapp):
    """(With the next test: a theme applied once its event is over - theme.apply_soon - and left pending by a test
    that ends at once, as test_ui's test_themes_are_applied_and_saved did, is applied and put back with the test that
    chose it - not in the next test's event loop, restyling every widget there.)"""
    from sandbox_ai_app import theme
    theme.apply_soon("hacker")


def test_the_next_test_has_the_look_it_found(qapp):
    from sandbox_ai_app import theme
    qapp.processEvents()
    assert theme.current() != "hacker"
