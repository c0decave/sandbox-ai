"""Review pass 17 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
The editor's text and Markdown views showed the text as it was opened after other bytes were saved from the hex view (or
refused it for good), and bytes saved from the text view stayed marked as changed in the hex view; zoom changed a
rendering not shown. Many headings of one name or many footnotes held up the Markdown view (each counted against all
before it). Copies beside two long names were cut to one name; Edit on a link to a folder said it was not followed. In
German the dialogs' buttons (Rename, New folder, Owner…) said "Cancel". Everything changed is below tmp_path."""
import os
import time

import pytest

from sandbox_ai_app import files

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app.file_editor import EditorWindow  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401


def editor(tmp_path, wait, data, name="t.txt", mode="auto"):
    (tmp_path / name).write_bytes(data)
    window = EditorWindow(None, files.Root(tmp_path), name, str(tmp_path / name), mode)
    assert wait(lambda: not window.loading, 10)
    return window


def type_hex(window, offset, digits):
    """DIGITS typed in the hex view's hex column at OFFSET, as the human types them."""
    window.hex.in_ascii = False
    window.hex.go(offset)
    for digit in digits:
        assert window.hex.type_text(digit)


# -- the editor's views --

def test_mixed_text_saved_from_the_hex_view_shows_what_was_saved(tmp_path, wait, qapp):
    """A text with mixed line endings, changed in the hex view and saved there: Text (and Markdown) then showed the text
    as it was opened - not what is on disk now - since the file's bytes were again "the bytes as read" (the saved
    ones) and the text widget was taken to hold them still."""
    window = editor(tmp_path, wait, b"one\r\ntwo\nthree\n")
    assert window.view == "text" and not window.text_ok
    assert window.switch("hex")
    type_hex(window, 0, "4f")  # o -> O
    assert window.dirty and window.save()
    assert wait(lambda: not window.saving, 10) and not window.dirty
    assert (tmp_path / "t.txt").read_bytes() == b"One\r\ntwo\nthree\n"
    assert window.switch("text") and window.text.isReadOnly()
    assert window.text.toPlainText().startswith("One"), "the text shown is the file as saved"
    assert window.contents() == b"One\r\ntwo\nthree\n"
    assert window.switch("markdown") and "One" in window.markdown.browser.toPlainText()
    window.close()


def test_text_made_mixed_in_the_hex_view_and_saved_comes_back_read_only(tmp_path, wait, qapp):
    """A text that could be edited, given a lone CR in the hex view and saved there: the text view was refused from
    then on ("These bytes are no text the editor can show unchanged") though the file is now that very text - as after
    a Reload, it is shown read-only."""
    window = editor(tmp_path, wait, b"one\ntwo\n")
    assert window.view == "text" and window.text_ok and not window.text.isReadOnly()
    assert window.switch("hex")
    type_hex(window, 3, "0d")  # the first line feed becomes a CR
    assert not window.switch("text") and window.view == "hex", "not saved: it stays in the hex view"
    assert window.save() and wait(lambda: not window.saving, 10)
    assert (tmp_path / "t.txt").read_bytes() == b"one\rtwo\n"
    assert window.switch("text") and window.view == "text" and window.text.isReadOnly()
    assert window.contents() == b"one\rtwo\n"
    assert window.switch("hex") and window.hex.data() == b"one\rtwo\n" and not window.hex.edited
    window.close()


def test_bytes_saved_from_the_text_view_are_no_longer_marked_changed_in_the_hex_view(tmp_path, wait, qapp):
    """A byte changed in the hex view, then saved from the text view: back in the hex view that byte was still marked
    as changed (red), though it was saved."""
    window = editor(tmp_path, wait, b"abc\n")
    assert window.switch("hex")
    type_hex(window, 0, "41")
    assert window.hex.edited == {0}
    assert window.switch("text") and window.save() and wait(lambda: not window.saving, 10)
    assert (tmp_path / "t.txt").read_bytes() == b"Abc\n" and not window.dirty
    assert window.switch("hex")
    assert not window.hex.edited, "saved: nothing is marked changed"
    window.close()


def test_the_editor_on_a_link_to_a_folder_says_it_is_a_folder(setup, wait):
    """Edit, the hex editor or View as Markdown (the context menu) on a host's link to a folder said "... is a symbolic
    link to ...; it is not followed" - though Open follows it there; it is a folder, as for the folder itself."""
    page, ws, host, outside = setup
    (host / "there").symlink_to(outside)
    page.right.list()
    settle(page.right)
    entry = page.right.by_name["there"]
    for mode in ("text", "hex", "markdown"):
        page.message.setText("")
        page.right.open_entry(entry, mode)
        assert wait(lambda: page.message.text(), 5)
        assert "not followed" not in page.message.text() and "folder" in page.message.text(), page.message.text()
    assert not page.editors and page.right.path_text() == str(host)


# -- a copy beside the original --

def test_copies_beside_two_long_names_get_names_of_their_own(setup, wait):
    """Two long names that differ only near their end were cut to the same name for their copies beside them ("xxx…
    (copy).txt"): the second copy was refused ("File exists") - or, with Replace on, replaced the first one."""
    page, ws, host, _ = setup
    first, second = "x" * 248 + "1.txt", "x" * 248 + "2.txt"  # (254 bytes each)
    (ws / first).write_text("one")
    (ws / second).write_text("two")
    page.left.list()
    select(page.left, first, second)
    assert page.copy_items(page.left)
    assert page.paste(page.left)
    assert wait(lambda: any("(copy" in name for name in os.listdir(ws)), 10) and finish(page, wait) is not None
    copies = sorted(name for name in os.listdir(ws) if "(copy" in name)
    assert len(copies) == 2, (copies, page.message.text())
    assert sorted((ws / name).read_text() for name in copies) == ["one", "two"]
    assert "exists" not in page.message.text()


def test_zoom_changes_only_a_rendering_that_is_shown(tmp_path, wait, qapp):
    """Ctrl+Plus in the hex view (or in the text view without Preview) zoomed the rendering that was not shown: nothing
    changed on the screen, and the Markdown view came back larger later, unasked."""
    window = editor(tmp_path, wait, b"# Title\n\ntext\n", name="r.md", mode="markdown")
    assert window.view == "markdown"
    window.zoom(1)
    assert window.markdown.zoom == 1, "(kept so) the rendering shown is zoomed"
    window.zoom(0)
    assert window.switch("hex")
    window.zoom(1)
    window.zoom(1)
    assert window.switch("text") and not window.preview_check.isChecked()
    window.zoom(1)
    assert window.switch("markdown") and window.markdown.zoom == 0
    assert window.switch("text")
    window.preview_check.setChecked(True)
    window.zoom(-1)
    assert window.preview.zoom == -1, "(kept so) the preview shown is zoomed"
    window.close()


# -- German --

def test_the_dialogs_buttons_are_german_in_german(qapp, monkeypatch):
    """In German, Rename, New folder, New file, Permissions, SSH… (QInputDialog) and Owner… (QDialogButtonBox) offered
    "Cancel", and a field's context menu "Undo", "Paste", "Select All": Qt's own words came without their translation.
    The app loads Qt's German (qtbase_de) when its language is German - not in English."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QInputDialog
    from sandbox_ai_app import i18n, main
    monkeypatch.setattr(i18n, "_language", "en")
    assert main.qt_translations(qapp) is None
    assert QInputDialog().cancelButtonText() == "Cancel"
    monkeypatch.setattr(i18n, "_language", "de")
    translator = main.qt_translations(qapp)
    try:
        assert translator is not None
        assert QInputDialog().cancelButtonText() == "Abbrechen"
        assert QCoreApplication.translate("QLineEdit", "&Paste") != "&Paste"
    finally:
        qapp.removeTranslator(translator)
    assert QInputDialog().cancelButtonText() == "Cancel"


# -- the Markdown view --

def test_many_headings_of_one_name_render_in_time(qapp):
    """Headings of one name are numbered as GitHub does ("a", "a-1", "a-2" ...) - each one counted up from 1 past every
    one before it: 20,000 of them (a file of 100 KB an agent wrote) held up the window for half a minute, more grew with
    their square."""
    from sandbox_ai_app.markdown_view import MarkdownView
    view = MarkdownView(None)
    began = time.monotonic()
    view.render("# a\n\n" * 20_000 + "a-1\n===\n")
    assert time.monotonic() - began < 8
    assert view.outline.count() == 20_001
    assert {"a", "a-1", "a-2", "a-19999", "a-1-1"} <= set(view.anchors), "(kept so) numbered as GitHub does"
    assert view.anchors["a-1-1"] > view.anchors["a-19999"]


def test_many_footnotes_render_in_time(qapp):
    """Each paragraph of the rendering was compared with every footnote's name to find the notes: 10,000 footnotes
    held up the window for over 15 s (growing with their square). The notes are still found."""
    from sandbox_ai_app.markdown_view import MarkdownView
    view = MarkdownView(None)
    text = "".join(f"x[^{i}]\n\n[^{i}]: note {i}\n\n" for i in range(10_000))
    began = time.monotonic()
    view.render(text)
    assert time.monotonic() - began < 8
    assert "fn-0" in view.anchors and "fn-9999" in view.anchors
    assert view.browser.document().findBlock(view.anchors["fn-9999"]).text() == "[9999] note 9999"
