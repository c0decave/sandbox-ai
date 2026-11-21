"""Save as… in the file manager's editor and viewer (user 2026-11-18: "markdown and edit/text/hex save as"): what is
shown - the text, the Markdown's text or the bytes of the hex view - written as a new file in the same folder (through
the pane's Root, never through a link); an existing name asked about first; the window shows the new file then. A
file shown only in part (too big) is not saved as one."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import QInputDialog  # noqa: E402

from sandbox_ai_app import widgets  # noqa: E402
from test_files_manager import editor_of  # noqa: E402
from test_files_page import setup  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME")


def named(monkeypatch, name, ok=True):
    asked = []
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(
        lambda *a, **k: (asked.append(k.get("text", a[3] if len(a) > 3 else "")), (name, ok))[1]))
    return asked


def open_file(page, wait, name, mode="text"):
    page.open_editor(page.left.root, name, str(page.left.root.path) + "/" + name, mode)
    return editor_of(page, wait)


def test_save_as_writes_the_text_shown_as_a_new_file_and_the_window_follows_it(setup, wait, monkeypatch):
    page, ws, _host, _ = setup
    window = open_file(page, wait, "a.txt")
    window.text.selectAll()
    window.text.insertPlainText("changed")
    asked = named(monkeypatch, "copy.txt")
    assert window.save_as() is True
    assert asked == ["a.txt"], "the current name offered"
    assert wait(lambda: not window.saving, 10)
    assert (ws / "copy.txt").read_text() == "changed" and (ws / "a.txt").read_text() == "alpha"
    assert window.relative.endswith("copy.txt") and "copy.txt" in window.where.text() and not window.dirty
    window.close()


def test_save_as_from_the_hex_view_and_from_a_viewer(setup, wait, monkeypatch):
    page, ws, _host, _ = setup
    window = open_file(page, wait, "a.txt", "hex")
    assert window.view == "hex"
    named(monkeypatch, "bytes.bin")
    assert window.save_as() is True and wait(lambda: not window.saving, 10)
    assert (ws / "bytes.bin").read_bytes() == b"alpha"
    window.close()
    page.open_editor(page.left.root, "a.txt", str(ws / "a.txt"), "text", read_only=True)  # F3: a viewer
    viewer = editor_of(page, wait)
    named(monkeypatch, "kept.txt")
    assert viewer.save_as() is True and wait(lambda: not viewer.saving, 10)
    assert (ws / "kept.txt").read_text() == "alpha"
    viewer.close()


def test_an_existing_name_is_asked_about_and_bad_names_are_refused(setup, wait, monkeypatch):
    page, ws, _host, outside = setup
    window = open_file(page, wait, "a.txt")
    widgets.SHOWN.clear()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    named(monkeypatch, "run.sh")
    assert window.save_as() is True and wait(lambda: not window.saving, 10)
    assert any("Replace" in title for title, *_ in widgets.SHOWN)
    assert (ws / "run.sh").read_text() == "#!/bin/sh\n", "not replaced without a yes"
    for bad in ("sub/x.txt", "..", "a\nb", ""):
        named(monkeypatch, bad)
        assert window.save_as() is False, bad
    named(monkeypatch, "escape")  # a link the agent put there: never written through
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    window.save_as()
    assert wait(lambda: not window.saving, 10)
    assert (ws / "escape").is_symlink() and sorted(os.listdir(outside)) == ["precious.txt"]
    assert (outside / "precious.txt").read_text() == "the host's own"
    window.close()
