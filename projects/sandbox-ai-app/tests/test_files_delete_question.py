"""The file manager's delete question names WHAT is deleted by its full path (user 2026-11-18: "1 Einträge in
<the folder shown> löschen?" with "nsm" only below read as if that folder itself were deleted - misleading and
dangerous). One item: its full path and its kind in the question itself; several: each full path listed; the folder
they are in is no longer the sentence's object."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import widgets  # noqa: E402
from test_files_page import select, setup  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME")


def asked(page, pane, monkeypatch):
    monkeypatch.setattr(page, "delete_items", lambda chosen: True)
    widgets.SHOWN.clear()
    assert page.delete_selected(pane) is not False
    (_title, text, details), = widgets.SHOWN
    return text, details or ""


def test_one_folder_is_named_by_its_full_path_and_as_a_folder(setup, monkeypatch):
    page, ws, _host, _ = setup
    select(page.left, "sub")
    text, _ = asked(page, page.left, monkeypatch)
    full = page.left.path_text().rstrip("/") + "/sub"
    assert full in text, text
    assert text.index(full) < 120, "the path comes first, not after the folder they are in"
    assert "folder" in text and "everything in it" in text
    assert "item(s) in" not in text


def test_one_file_and_one_link(setup, monkeypatch):
    page, _ws, _host, _ = setup
    select(page.left, "a.txt")
    text, _ = asked(page, page.left, monkeypatch)
    assert page.left.path_text().rstrip("/") + "/a.txt" in text and "file" in text
    select(page.left, "escape")
    text, _ = asked(page, page.left, monkeypatch)
    assert page.left.path_text().rstrip("/") + "/escape" in text
    assert "link" in text and "not what it points to" in text


def test_several_are_each_listed_with_their_full_path(setup, monkeypatch):
    page, _ws, _host, _ = setup
    select(page.left, "a.txt", "sub")
    text, details = asked(page, page.left, monkeypatch)
    base = page.left.path_text().rstrip("/")
    for name in ("a.txt", "sub"):
        assert f"{base}/{name}" in text, text
    assert "2" in text and "cannot be undone" in text
