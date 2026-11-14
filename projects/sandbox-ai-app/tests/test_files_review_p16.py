"""Review pass 16 of the Files page's operations: each test failed before its fix, unless it says it keeps something so.
The editor's Replace all took minutes on a long line (a minified file: each replacement laid the whole line out
again) and its find bar never found a non-breaking space typed or pasted into it (Qt's own find reads every one as a
space). The Markdown viewer decoded up to IMAGES images of IMAGE_PIXELS pixels each - one small PNG named two hundred
times took gigabytes, and an SVG image had Qt read the files its <image> elements named, anywhere on this computer.
Wrap lines on a long line, and Markdown with long lists, long tables or a long line, held up the window for minutes.
An Apply of Properties pressed as the late Apply before had ended, having changed nothing, did nothing and said
nothing. Everything changed is below tmp_path."""
import os
import stat
import time

import pytest

from sandbox_ai_app import files

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

from sandbox_ai_app import markdown_view  # noqa: E402
from sandbox_ai_app.file_editor import EditorWindow  # noqa: E402
from test_files_page import FakeSai, finish, names, select, settle, setup  # noqa: E402,F401


def editor(tmp_path, wait, data, name="t.txt", mode="auto"):
    (tmp_path / name).write_bytes(data)
    window = EditorWindow(None, files.Root(tmp_path), name, str(tmp_path / name), mode)
    assert wait(lambda: not window.loading, 10)
    return window


# -- the editor's find bar --

def test_replace_all_on_a_long_line_is_quick_and_one_step_to_undo(tmp_path, wait, qapp):
    """Replace all on one long line (a minified file: 300 KB, 100,000 matches) took 100 s - the window hung: each match
    replaced on its own laid the whole line out again. Now it is one change - still one step to undo."""
    data = b"ab " * 100_000 + b"\nab second line\n"
    window = editor(tmp_path, wait, data)
    bar = window.find_bar
    bar.open(replace=True)
    bar.find.setText("ab")
    bar.replacement.setText("c\\1")  # (taken as it is: no pattern of a regular expression)
    began = time.monotonic()
    assert bar.replace_all() == 100_001
    assert time.monotonic() - began < 10
    assert window.contents() == b"c\\1 " * 100_000 + b"\nc\\1 second line\n"
    assert window.dirty
    window.text.document().undo()
    assert window.contents() == data
    window.dirty = False
    window.close()


def test_replace_all_keeps_case_and_what_is_between(tmp_path, wait, qapp):
    """(kept so) Match case off: every case is replaced; on: only that one; a replacement that holds what is found is
    not found again; text before the first and after the last match stays."""
    window = editor(tmp_path, wait, "head Foo foo FOO\r\ntail foo\r\nend\r\n".encode())
    bar = window.find_bar
    bar.open(replace=True)
    bar.find.setText("foo")
    bar.replacement.setText("foofoo")
    bar.case.setChecked(True)
    assert bar.replace_all() == 2
    assert window.contents() == "head Foo foofoo FOO\r\ntail foofoo\r\nend\r\n".encode()
    window.text.document().undo()
    bar.case.setChecked(False)
    assert bar.replace_all() == 4
    assert window.contents() == "head foofoo foofoo foofoo\r\ntail foofoo\r\nend\r\n".encode()
    window.dirty = False
    window.close()


def test_a_non_breaking_space_is_found_as_typed(tmp_path, wait, qapp):
    """A non-breaking space typed or pasted into the find field was never found (Qt's QTextDocument.find reads each one
    of the text as a space), and a plain space found one: Replace all of "a b" changed "a\\u00a0b" as well, though it
    is another text. The find bar finds what was typed."""
    window = editor(tmp_path, wait, "a b and a b\n".encode())
    bar = window.find_bar
    bar.open(replace=True)
    bar.find.setText("a b")
    assert bar.status.text() == "1 of 1", bar.status.text()
    cursor = window.text.textCursor()
    assert (cursor.selectionStart(), cursor.selectionEnd()) == (0, 3)
    bar.find.setText("a b")
    assert bar.status.text() == "1 of 1", bar.status.text()
    bar.replacement.setText("X")
    assert bar.replace_all() == 1
    assert window.contents() == "a b and X\n".encode()
    window.dirty = False
    window.close()


def test_next_previous_and_replace_go_by_characters_beyond_the_first_plane(tmp_path, wait, qapp):
    """(kept so) Next, Previous, the count and Replace on a text with characters beyond U+FFFF (two UTF-16 units each
    in Qt's document): the match marked is the one found."""
    window = editor(tmp_path, wait, "\U0001f600x one \U0001f600x two\n\U0001f600x\n".encode())
    bar = window.find_bar
    bar.open(replace=True)
    bar.find.setText("\U0001f600x")
    assert bar.status.text() == "1 of 3"
    assert window.text.textCursor().selectedText() == "\U0001f600x"
    bar.search()
    assert bar.status.text() == "2 of 3" and window.text.textCursor().selectionStart() == 8
    bar.search()
    assert bar.status.text() == "3 of 3"
    bar.search()
    assert bar.status.text() == "1 of 3", "round the end"
    bar.search(backward=True)
    assert bar.status.text() == "3 of 3", "round the beginning"
    bar.search(backward=True)
    assert bar.status.text() == "2 of 3"
    bar.replacement.setText("y")
    bar.replace()
    assert window.contents() == "\U0001f600x one y two\n\U0001f600x\n".encode()
    assert bar.status.text() == "2 of 2"
    window.dirty = False
    window.close()


def test_the_rendering_is_searched_too(tmp_path, wait, qapp):
    """(kept so) The find bar in the Markdown view finds in the rendering (a table, an image in between)."""
    window = editor(tmp_path, wait, b"# Title\n\n| a | b |\n|---|---|\n| one | two |\n\n![i](x.png) two\n",
                    name="r.md", mode="markdown")
    assert window.view == "markdown"
    bar = window.find_bar
    bar.open()
    bar.find.setText("TWO")
    assert bar.status.text() == "1 of 2"
    assert window.markdown.browser.textCursor().selectedText() == "two"
    bar.case.setChecked(True)
    assert bar.status.text() == "No match"
    window.close()


# -- the Markdown viewer's images: bounded together --

def test_the_images_of_one_document_are_bounded_together(tmp_path, monkeypatch, qapp):
    """Each image was bounded (IMAGE_PIXELS), all of them not: one small PNG of 6000 x 6000 pixels named 200 times
    ("big.png?1", "./big.png", ...) was decoded 200 times - 144 MB each, 28 GB together, from a file an agent wrote.
    Past IMAGES_PIXELS together the others are "too big"."""
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(tmp_path / "big.png"), "PNG")
    monkeypatch.setattr(markdown_view, "IMAGES_PIXELS", 25_000)
    source = markdown_view.ImageSource(files.Root(tmp_path), "readme.md")
    found = source.read([f"big.png?{number}" for number in range(5)])
    shown = [name for name, value in found.items() if isinstance(value, QImage)]
    assert len(shown) == 2, found
    assert found["big.png?4"] == "too big"
    # (a later read of the same document is bounded by what the reads before took)
    assert source.read(["./big.png"])["./big.png"] == "too big"
    source.forget()  # (Reload: read anew)
    assert isinstance(source.read(["./big.png"])["./big.png"], QImage)


def test_the_views_of_one_window_share_the_bound(tmp_path, monkeypatch, qapp, wait):
    """(and Reload reads them anew) The rendering and the preview of one window read through one ImageSource."""
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(tmp_path / "big.png"), "PNG")
    monkeypatch.setattr(markdown_view, "IMAGES_PIXELS", 25_000)
    text = "".join(f"![x](big.png?{number})\n\n" for number in range(4)).encode()
    (tmp_path / "r.md").write_bytes(text)
    window = EditorWindow(None, files.Root(tmp_path), "r.md", str(tmp_path / "r.md"), "markdown")
    window.show()  # (as the page opens it: shown while it reads)
    view = window.markdown
    assert wait(lambda: sum(1 for value in view.images.values() if isinstance(value, QImage) and value.width() == 100)
                == 2, 10)
    window.reload()
    assert wait(lambda: not window.loading, 10)
    assert wait(lambda: sum(1 for value in view.images.values() if isinstance(value, QImage) and value.width() == 100)
                == 2, 10), "read anew after Reload"
    window.close()


# -- Properties: an Apply pressed as the late one before ended, having changed nothing --

def test_apply_after_a_late_apply_that_changed_nothing_applies(setup, wait, qapp):
    """The Apply before, given up while the file system did not answer, ended later having changed nothing (cancelled
    before its first item). An Apply pressed before the dialog's next tick learnt that, and returned - nothing changed,
    nothing said: the human's click was lost. It applies now (only an Apply before that changed something is shown
    first, "took effect")."""
    import threading
    page, _ws, host, _ = setup
    (host / "p.txt").write_text("p")
    os.chmod(host / "p.txt", 0o600)
    right = page.right
    right.list()
    settle(right)
    dialog = page.properties(right, [right.by_name["p.txt"]])
    ended = threading.Event()
    ended.set()
    dialog.pending = {"ended": ended, "set": {}, "done": 0, "mode": 0o640, "started": True}
    dialog.late_timer.setInterval(3600 * 1000)
    dialog.late_timer.start()
    dialog.checks[3].setChecked(True)  # (the group's read: 0640)
    assert dialog.apply() is not False
    assert wait(lambda: stat.S_IMODE(os.stat(host / "p.txt").st_mode) == 0o640, 10), dialog.message.text()
    assert not dialog.late_timer.isActive()
    dialog.close()


# -- the Markdown viewer's images: an SVG never reads another file --

def svg(body, size=50):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{size}" '
            f'height="{size}">{body}</svg>').encode()


def test_an_svg_image_never_reads_a_file_it_names(tmp_path, qapp):
    """An image is read through the pane's Root only (never through a link, never above the root or outside the
    sandbox's folder) - but an SVG image beside the file shown had Qt read the files its <image> elements name, by
    their paths on this computer: an agent's docs/pic.svg showed the human's ~/private.png (a FIFO named so held the
    reading thread for good). An SVG that names anything but a part of itself is not shown; one that does not is."""
    import base64
    import gzip
    secret = QImage(50, 50, QImage.Format.Format_RGB32)
    secret.fill(Qt.GlobalColor.blue)
    assert secret.save(str(tmp_path / "secret.png"), "PNG")
    ws = tmp_path / "ws"
    ws.mkdir()
    named = [f'<image width="50" height="50" xlink:href="{tmp_path / "secret.png"}"/>',
             f'<image width="50" height="50" href="file://{tmp_path / "secret.png"}"/>',
             '<image width="50" height="50" href="../secret.png"/>',
             f'<svg:image xmlns:svg="http://www.w3.org/2000/svg" width="50" height="50" '
             f'xlink:href = \'{tmp_path / "secret.png"}\'/>']
    for number, body in enumerate(named):
        (ws / f"p{number}.svg").write_bytes(svg(body))
    (ws / "packed.svgz").write_bytes(gzip.compress(svg(named[0])))
    (ws / "entity.svg").write_bytes(b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY e "x">]>' + svg("<g>&e;</g>"))
    (ws / "wide.svg").write_bytes(svg(named[0]).decode().encode("utf-16"))
    (ws / "plain.svg").write_bytes(svg('<defs><linearGradient id="g"><stop offset="0" stop-color="red"/>'
                                       '</linearGradient></defs><rect width="50" height="50" fill="url(#g)"/>'))
    (ws / "plain.svgz").write_bytes(gzip.compress(svg('<rect width="50" height="50" fill="red"/>')))
    source = markdown_view.ImageSource(files.Root(ws), "readme.md")
    refused = [f"p{number}.svg" for number in range(len(named))] + ["packed.svgz", "entity.svg", "wide.svg"]
    found = source.read(refused + ["plain.svg", "plain.svgz"])
    for name in refused:
        assert found[name] == "refers to other files", (name, found[name])
    for name in ("plain.svg", "plain.svgz"):
        assert isinstance(found[name], QImage) and found[name].width() == 50, (name, found[name])
    data = "data:image/png;base64," + base64.b64encode(svg(named[0])).decode()  # (an SVG said to be a PNG)
    assert source.read([data])[data] == "refers to other files"


# -- what Qt lays out in time: long lines wrapped, long lists and tables rendered --

def test_wrap_lines_never_holds_up_the_window_on_a_long_line(tmp_path, wait, qapp):
    """Wrap lines on a minified file of 1 MB (one line without a space) held up the window for about 40 s (5 MB: about
    20 minutes) - Qt wraps such a line in time that grows with its square. Past LONG_RUN characters without a space
    the lines are not wrapped: the box goes off again, and the window says why; a text without such a line wraps."""
    window = editor(tmp_path, wait, b"x" * (1 << 20) + b"\nshort line\n")
    window.show()
    began = time.monotonic()
    window.wrap.setChecked(True)
    qapp.processEvents()
    assert time.monotonic() - began < 5
    assert not window.wrap.isChecked()
    assert window.text.lineWrapMode() == QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
    assert "without a space" in window.notice.text() and window.notice.isVisible()
    window.close()
    plain = editor(tmp_path, wait, b"word " * 50_000 + b"\n", name="words.txt")
    plain.wrap.setChecked(True)
    assert plain.wrap.isChecked()
    assert plain.text.lineWrapMode() == QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth
    plain.close()


def test_a_reload_with_wrap_on_never_lays_out_a_long_line_wrapped(tmp_path, wait, qapp):
    """Wrap lines ticked, then the file became one long line on disk (an agent minified it): Reload laid it out
    wrapped - the window held up again. The box goes off before the text is shown."""
    window = editor(tmp_path, wait, b"short\n")
    window.show()
    window.wrap.setChecked(True)
    assert window.wrap.isChecked()
    (tmp_path / "t.txt").write_bytes(b"y" * (1 << 20))
    began = time.monotonic()
    window.reload()
    assert wait(lambda: not window.loading, 30)
    qapp.processEvents()
    assert time.monotonic() - began < 5
    assert not window.wrap.isChecked() and "without a space" in window.notice.text()
    window.close()


@pytest.mark.parametrize("text, why", [
    ("".join(f"- item {number}\n" for number in range(20_000)), "list"),
    ("| a | b |\n|---|---|\n" + "".join(f"| {number} | x |\n" for number in range(20_000)), "table"),
    ("# Title\n\n" + "z" * (1 << 20) + "\n\nafter\n", "without a space"),
], ids=["list", "table", "long line"])
def test_the_rendering_never_holds_up_the_window(qapp, tmp_path, text, why):
    """A Markdown file an agent wrote with a list of 20,000 items took 16 s to render (Qt numbers each item in time that
    grows with the list), a table of 20,000 rows 24 s, a line of 1 MB without a space over a minute - each held up the
    window (and the live preview did so again after each pause in the typing). The rendering stops before the line
    that goes past LIST_ITEMS items, TABLE_ROWS rows or LONG_RUN characters without a space, and says so."""
    view = markdown_view.MarkdownView(markdown_view.ImageSource(files.Root(tmp_path), "readme.md"))
    view.resize(700, 600)
    view.show()
    began = time.monotonic()
    view.render(text)
    qapp.processEvents()
    assert time.monotonic() - began < 5
    assert view.notice.isVisible() and why in view.notice.text(), view.notice.text()
    assert "Only the first" in view.notice.text()
    view.render("# Short\n\n- one\n- two\n")
    assert not view.notice.isVisible()
    view.close()
