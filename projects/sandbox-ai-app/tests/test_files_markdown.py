"""The Files page's Markdown viewer (markdown_view.py, file_editor.py): .md files recognised and opened rendered, GFM
(tables, task lists, strikethrough, autolinks), highlighted code, footnotes, the outline, images read through the
pane's Root (never remote, never through a link, bounded), links that are never followed inside the viewer, raw HTML
shown as text, zoom, find, Edit and back, the editor's live preview. Everything the tests change is below tmp_path."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QUrl, Qt  # noqa: E402
from PySide6.QtGui import QImage, QTextDocument, QTextFormat  # noqa: E402

from sandbox_ai_app import file_editor, files, markdown_view, widgets  # noqa: E402
from test_files_manager import editor_of  # noqa: E402
from test_files_page import press, select, settle, setup  # noqa: E402,F401

DOC = """# Guide

Some *text*, ~~gone~~, https://example.org/auto and <b>bold</b> <script>alert(1)</script>.

## Table

| a | b |
|---|---|
| 1 | 2 |

- [x] done
- [ ] todo

```python
def f():  # a comment
    return "s"
```

![pic](img/pic.png) ![far](https://example.org/x.png) ![up](../secret.png) ![abs](/etc/x.png)

[next](next.md) [web](https://example.org/page) [top](#guide) [file](file:///etc/passwd) [mail](mailto:a@b.c)

A note[^1].

[^1]: The note's text.
"""


# -- without a window --

def test_markdown_files_are_known_by_their_names():
    for name in ("README.md", "notes.MARKDOWN", "a.mdown", "b.Mkd", ".md"):
        assert markdown_view.is_markdown(name), name
    for name in ("readme.txt", "md", "a.md.bak", "markdown"):
        assert not markdown_view.is_markdown(name), name


def test_slugs_and_footnotes():
    assert markdown_view.slug("Hello, World!") == "hello-world" and markdown_view.slug("A_b c-d") == "a_b-c-d"
    text, notes = markdown_view.with_footnotes("x[^1] y[^n]\n\n[^1]: one\n[^n]: two\n```\n[^1] kept\n```\n")
    assert notes == ["1", "n"]
    assert "[\\[1\\]](#fn-1)" in text and "[\\[n\\]](#fn-n)" in text
    assert "[^1] kept" in text, "nothing inside fenced code changes"
    assert text.rstrip().endswith("\\[n\\] two") and "[^1]: one" not in text


def test_image_names_point_below_the_root_only(tmp_path):
    source = markdown_view.ImageSource(files.Root(tmp_path), "docs/readme.md")
    kind = source.kind
    assert kind("img/a.png") == ("local", "docs/img/a.png") and kind("../top.png") == ("local", "top.png")
    assert kind("a%20b.png") == ("local", "docs/a b.png")
    for name in ("../../etc/passwd", "/etc/passwd", "file:///etc/passwd", "C:/x.png", "", "javascript:x"):
        assert kind(name)[0] == "outside", name
    for name in ("https://example.org/x.png", "http://x/y", "//example.org/x.png", "ftp://h/x"):
        assert kind(name)[0] == "remote", name
    assert kind("data:image/png;base64,AAAA")[0] == "data"


def png(path, size=(4, 3)):
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(path), "PNG")
    return path


def test_images_are_read_through_the_root_bounded_and_never_through_a_link(qapp, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "img").mkdir(parents=True)
    png(ws / "img" / "pic.png")
    png(tmp_path / "outside.png")
    (ws / "img" / "link.png").symlink_to(tmp_path / "outside.png")
    (ws / "img" / "not.png").write_bytes(b"no image")
    source = markdown_view.ImageSource(files.Root(ws), "readme.md")
    found = source.read(["img/pic.png", "img/link.png", "img/not.png", "img/gone.png", "https://example.org/x.png"])
    assert isinstance(found["img/pic.png"], QImage) and found["img/pic.png"].width() == 4
    assert "symbolic link" in found["img/link.png"], "never through a link"
    assert found["img/not.png"] == "not an image" and "img/gone.png" in found
    assert "https://example.org/x.png" not in found, "a remote image is never read"
    monkeypatch.setattr(markdown_view, "IMAGE_LIMIT", 10)
    assert source.read(["img/pic.png"])["img/pic.png"] == "too big"
    monkeypatch.setattr(markdown_view, "IMAGE_LIMIT", 8 << 20)
    monkeypatch.setattr(markdown_view, "IMAGE_PIXELS", 5)
    assert source.read(["img/pic.png"])["img/pic.png"] == "too big", "pixels counted before decoding"


def rendered(qapp, tmp_path, text=DOC, wait=None):
    ws = tmp_path / "ws"
    (ws / "img").mkdir(parents=True, exist_ok=True)
    png(ws / "img" / "pic.png")
    view = markdown_view.MarkdownView(markdown_view.ImageSource(files.Root(ws), "readme.md"))
    view.resize(700, 600)
    view.show()
    view.render(text)
    qapp.processEvents()
    return view


def blocks(document):
    block = document.begin()
    while block.isValid():
        yield block
        block = block.next()


def test_tables_task_lists_strikethrough_autolinks_and_code_are_rendered(qapp, tmp_path, wait):
    view = rendered(qapp, tmp_path)
    document = view.browser.document()
    assert any(type(frame).__name__ == "QTextTable" for frame in document.rootFrame().childFrames()), "a table"
    markers = {block.text(): block.blockFormat().marker() for block in blocks(document)}
    assert markers["done"] == markers["done"].Checked and markers["todo"] == markers["todo"].Unchecked
    html = document.toHtml()
    assert "line-through" in html and 'href="https://example.org/auto"' in html, "strikethrough and an autolink"
    code = [block for block in blocks(document) if block.blockFormat().hasProperty(QTextFormat.Property.BlockCodeFence)]
    assert code and code[0].blockFormat().property(QTextFormat.Property.BlockCodeLanguage) == "python"
    assert any(code[0].layout().formats()), "fenced code highlighted"
    plain = document.toPlainText()
    assert "<b>bold</b> <script>alert(1)</script>" in plain, "raw HTML shown as the text it is, never interpreted"
    assert [view.outline.item(i).text() for i in range(view.outline.count())] == ["Guide", "    Table"]
    assert "fn-1" in view.anchors and "guide" in view.anchors and "table" in view.anchors


def test_nothing_remote_is_loaded_and_local_images_come_through_the_root(qapp, tmp_path, wait, monkeypatch):
    read = []
    real = markdown_view.ImageSource.read
    monkeypatch.setattr(markdown_view.ImageSource, "read", lambda self, names: read.append(list(names)) or
                        real(self, names))
    asked = []
    monkeypatch.setattr(QtWidgets.QTextBrowser, "loadResource", lambda *a: asked.append(a) or None)
    view = rendered(qapp, tmp_path)
    assert wait(lambda: isinstance(view.images.get("img/pic.png"), QImage) and
                view.images["img/pic.png"].width() == 4)
    assert read == [["img/pic.png"]], "only the image beside the file was read; remote and outside ones never"
    assert asked == [], "Qt's own loading is never used"
    for name in ("https://example.org/x.png", "../secret.png", "/etc/x.png"):
        assert view.images[name].width() == 420, f"{name}: a placeholder"
    document = view.browser.document()
    held = document.resource(QTextDocument.ResourceType.ImageResource, QUrl("https://example.org/x.png"))
    assert held.width() == 420, "the document holds the placeholder, nothing fetched"
    assert view.browser.loadResource(QTextDocument.ResourceType.StyleSheetResource, QUrl("x.css")) is None


def test_anchors_scroll_and_other_links_go_to_the_window(qapp, tmp_path, wait):
    view = rendered(qapp, tmp_path, DOC + "\n\n".join(f"line {n}" for n in range(120)) + "\n\n## Far\n\nend\n")
    links = []
    view.link.connect(links.append)
    view.clicked(QUrl("#far"))
    assert view.browser.verticalScrollBar().value() > 0 and links == []
    view.clicked(QUrl("#fn-1"))
    view.clicked(QUrl("#nowhere"))
    assert "No heading" in view.status.text() and links == []
    view.clicked(QUrl("next.md"))
    assert [url.toString() for url in links] == ["next.md"]
    view.hovered(QUrl("https://example.org/page"))
    assert view.status.text() == "Link: https://example.org/page"
    view.zoom_by(2)
    assert view.zoom == 2
    view.zoom_by(0)
    assert view.zoom == 0


# -- in the editor's window, from the Files page --

@pytest.fixture
def docs(setup):
    page, ws, host, outside = setup
    (ws / "readme.md").write_text(DOC)
    (ws / "next.md").write_text("# Next\n\nthe next file\n")
    (ws / "plain.txt").write_text("# not markdown by name\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    (outside / "secret.md").write_text("# secret")
    page.left.list()
    settle(page.left)
    return setup


def test_markdown_files_open_rendered_f3_views_and_f4_edits(docs, wait):
    page, ws, host, _ = docs
    from sandbox_ai_app import widgets as w
    page.left.open_row(w.find_row(page.left.table, "readme.md"))  # a double click
    window = editor_of(page, wait)
    assert window.view == "markdown" and not window.read_only and window.markdown.outline.count() == 2
    assert not window.edit_button.isHidden() and window.preview_check.isHidden()
    window.close()
    select(page.left, "readme.md")
    page.left.table.setCurrentCell(w.find_row(page.left.table, "readme.md"), 0)
    press(page.left, Qt.Key.Key_F3)
    viewer = editor_of(page, wait)
    assert viewer.view == "markdown" and viewer.read_only
    viewer.close()
    press(page.left, Qt.Key.Key_F4)
    editor = editor_of(page, wait)
    assert editor.view == "text" and not editor.read_only and not editor.preview_check.isHidden()
    editor.close()
    # Any file: View as Markdown in the context menu.
    select(page.left, "plain.txt")
    menu = page.menu(page.left)
    action = next(a for a in menu.actions() if a.text() == "View as Markdown")
    action.trigger()
    window = editor_of(page, wait)
    assert window.view == "markdown" and window.read_only
    assert any(type(f).__name__ == "QTextTable" for f in window.markdown.browser.document().rootFrame().childFrames())
    window.close()


def test_edit_and_back_the_live_preview_find_and_zoom(docs, wait):
    page, ws, host, _ = docs
    window = page.open_editor(page.left.root, "readme.md", str(ws / "readme.md"), "markdown", read_only=True)
    editor_of(page, wait)
    assert window.read_only and not window.save_button.isVisible() or window.read_only
    window.edit_button.click()  # Edit: the text, editable now
    assert window.view == "text" and not window.read_only and window.save_button.isVisibleTo(window)
    window.text.moveCursor(window.text.textCursor().MoveOperation.End)
    window.text.insertPlainText("\n## Added\n")
    window.as_markdown.click()  # and back: what was typed, rendered
    assert window.view == "markdown" and "Added" in [window.markdown.outline.item(i).text().strip()
                                                     for i in range(window.markdown.outline.count())]
    window.switch("text")
    window.preview_check.setChecked(True)
    assert not window.preview.isHidden()
    window.text.insertPlainText("\n## Live\n")
    assert wait(lambda: "Live" in window.preview.browser.document().toPlainText(), 3), "the preview follows the typing"
    # Find in the rendering (the editor's find bar), zoom.
    window.switch("markdown")
    bar = window.find_bar
    bar.open()
    bar.find.setText("todo")
    assert bar.status.text() == "1 of 1" and window.markdown.browser.textCursor().selectedText() == "todo"
    assert not bar.replace_toggle.isEnabled(), "nothing replaced in the rendering"
    size = window.markdown.browser.font().pointSizeF()
    window.zoom(1)
    assert window.markdown.browser.font().pointSizeF() > size
    window.zoom(0)
    assert window.markdown.browser.font().pointSizeF() == size
    saved = []
    window.on_saved = lambda: saved.append(True)
    assert window.save() and wait(lambda: not window.saving)
    assert (ws / "readme.md").read_text().endswith("## Added\n\n## Live\n")
    window.close()


def test_links_are_asked_opened_in_a_viewer_or_refused(docs, wait, monkeypatch):
    page, ws, host, _ = docs
    opened = []
    monkeypatch.setattr(file_editor, "open_url", lambda url: opened.append(url.toString()))
    window = page.open_editor(page.left.root, "readme.md", str(ws / "readme.md"), "markdown")
    editor_of(page, wait)
    widgets.SHOWN.clear()
    window.open_link(QUrl("https://example.org/page"))
    assert [title for title, *_ in widgets.SHOWN] == ["Open the link?"] and opened == ["https://example.org/page"]
    widgets.AUTO_ANSWER = False
    window.open_link(QUrl("https://example.org/other"))
    assert opened == ["https://example.org/page"], "not without a yes"
    widgets.AUTO_ANSWER = True
    count = len(page.editors)
    window.open_link(QUrl("next.md"))
    other = editor_of(page, wait)
    assert len(page.editors) == count + 1 and other.relative == "next.md" and other.view == "markdown"
    assert "the next file" in other.markdown.browser.document().toPlainText() and other.read_only
    other.close()
    assert wait(lambda: len(page.editors) == count)
    for target in ("../outside/secret.md", "escape/secret.md", "file:///etc/passwd", "mailto:a@b.c", "plain.txt",
                   "javascript:alert(1)"):
        count, asked = len(page.editors), len(widgets.SHOWN)
        window.open_link(QUrl(target))
        settle(page.left)
        if target == "escape/secret.md":  # (a link in the sandbox's folder: its viewer says it is not followed)
            viewer = editor_of(page, wait)
            assert viewer.read_only and "symbolic link" in viewer.notice.text()
            viewer.close()
            assert wait(lambda: len(page.editors) == count)
            continue
        assert len(page.editors) == count and len(widgets.SHOWN) == asked, target
        assert "Not opened" in window.notice.text(), target
    assert opened == ["https://example.org/page"]
    window.close()


def test_binary_files_are_not_shown_as_markdown(setup, wait):
    page, ws, host, _ = setup
    (ws / "blob.md").write_bytes(b"\x00\x01\x02 not text")
    window = page.open_editor(page.left.root, "blob.md", str(ws / "blob.md"), "markdown")
    editor_of(page, wait)
    assert window.view == "hex" and "Markdown" in window.notice.text() and not window.as_markdown.isEnabled()
    window.close()
    assert os.path.exists(ws / "blob.md")
