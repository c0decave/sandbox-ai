"""A hostile Markdown file - an agent wrote it - does nothing in the Files page's viewer (markdown_view.py,
file_editor.py): no script runs, no event handler, nothing is fetched, no file outside the folder is read, no link is
followed without the human's yes (and the question shows the real address: its host in punycode, controls visible, the
link's text beside it when that differs), deep nesting and an SVG whose references draw it again and again hold up
nothing. Everything the tests change is below tmp_path."""
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtGui import QTextCursor, QTextDocument  # noqa: E402

from sandbox_ai_app import file_editor, files, markdown_view, widgets  # noqa: E402
from test_files_manager import editor_of  # noqa: E402
from test_files_page import settle, setup  # noqa: E402,F401

HOSTILE = """# Hostile

<script>alert(1)</script>
<img src=x onerror=alert(1)>
<img src="/dev/zero" width=100000 height=100000>
<style>body { display: none }</style>
<iframe src="https://evil.example/"></iframe>
<a href="javascript:alert(1)">html link</a>

[js](javascript:alert(1)) [vb](vbscript:msgbox) [data](data:text/html,<script>alert(1)</script>)
[passwd](file:///etc/passwd) [abs](/etc/passwd) [up](../../outside/secret.md)

![remote](https://evil.example/track.png) ![passwd](file:///etc/passwd) ![zero](/dev/zero) ![up](../outside/x.png)
![proto](//evil.example/x.png)

$\\href{https://evil.example}{x}$ and $\\includegraphics{/etc/passwd}$
"""


def view_of(qapp, root, text):
    view = markdown_view.MarkdownView(markdown_view.ImageSource(files.Root(root), "readme.md"))
    view.resize(700, 500)
    view.show()
    view.render(text)
    qapp.processEvents()
    return view


def test_nothing_of_a_hostile_file_runs_loads_or_is_read(qapp, tmp_path, wait, monkeypatch):
    asked = []
    monkeypatch.setattr(QtWidgets.QTextBrowser, "loadResource", lambda *a: asked.append(a) or None)
    read = []
    real = markdown_view.ImageSource.read
    monkeypatch.setattr(markdown_view.ImageSource, "read", lambda self, names: read.append(list(names)) or
                        real(self, names))
    view = view_of(qapp, tmp_path, HOSTILE)
    qapp.processEvents()
    assert asked == [], "Qt's own loading is never used"
    assert read == [], "no image of the file lies in its folder: none is read"
    document = view.browser.document()
    plain = document.toPlainText()
    for tag in ("<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "<style>", "<iframe"):
        assert tag in plain, f"{tag}: shown as the text it is"
    html = document.toHtml()
    assert "onerror" not in html.replace("onerror=alert(1)&gt;", ""), "no event handler in the page"
    # (md4c keeps a javascript: link a link - a click on it opens nothing: test_a_click_on_a_hostile_link_opens_nothing)
    for name in ("https://evil.example/track.png", "file:///etc/passwd", "/dev/zero", "../outside/x.png",
                 "//evil.example/x.png"):
        assert view.images[name].width() == 420, f"{name}: a placeholder, never fetched nor read"
    assert "evil.example" not in "".join(str(a) for a in asked)


def test_a_click_on_a_hostile_link_opens_nothing(setup, wait, monkeypatch):
    page, ws, host, outside = setup
    (ws / "readme.md").write_text(HOSTILE)
    opened = []
    monkeypatch.setattr(file_editor, "open_url", lambda url: opened.append(url.toString()))
    page.left.list()
    settle(page.left)
    window = page.open_editor(page.left.root, "readme.md", str(ws / "readme.md"), "markdown")
    editor_of(page, wait)
    widgets.SHOWN.clear()
    count = len(page.editors)
    for target in ("javascript:alert(1)", "vbscript:msgbox", "data:text/html,<script>alert(1)</script>",
                   "file:///etc/passwd", "/etc/passwd", "../../outside/secret.md"):
        window.open_link(QUrl(target))
        assert "Not opened" in window.notice.text(), target
    assert opened == [] and widgets.SHOWN == [] and len(page.editors) == count, "nothing opened, nothing asked"
    window.close()


def test_the_link_question_shows_the_real_address(setup, wait, monkeypatch):
    page, ws, host, outside = setup
    (ws / "readme.md").write_text("[the bank](https://аpple.com/login)\n")
    opened = []
    monkeypatch.setattr(file_editor, "open_url", lambda url: opened.append(url.toString()))
    page.left.list()
    settle(page.left)
    window = page.open_editor(page.left.root, "readme.md", str(ws / "readme.md"), "markdown")
    editor_of(page, wait)
    widgets.SHOWN.clear()
    window.open_link(QUrl("https://аpple.com/login"), "https://apple.com/login")
    [(title, text, _details)] = widgets.SHOWN
    assert title == "Open the link?"
    assert "https://xn--pple-43d.com/login" in text and "аpple" not in text, "the host as punycode, never look-alike"
    assert "Host: xn--pple-43d.com" in text
    assert "The link in the file reads: https://apple.com/login" in text, "its text beside it: it differs"
    assert opened == ["https://аpple.com/login"], "opened after the yes"
    widgets.SHOWN.clear()
    window.open_link(QUrl("https://example.org/a‮gnp.exe"), "click")
    [(_title, text, _details)] = widgets.SHOWN
    assert "‮" not in text and "%E2%80%AE" in text, "a bidi control is shown as what it is"
    widgets.SHOWN.clear()
    window.open_link(QUrl("https://example.org/same"), "https://example.org/same")
    assert "reads:" not in widgets.SHOWN[0][1], "the text is named only when it differs"
    widgets.SHOWN.clear()
    window.open_link(QUrl("https://exa‮mple.org/"), "x")
    assert widgets.SHOWN == [] and "Not opened" in window.notice.text(), "no valid address: not offered"
    window.close()


def test_the_link_text_comes_from_the_page(qapp, tmp_path):
    view = view_of(qapp, tmp_path, "Go [to the **bank**](https://example.org/b) now.\n")
    links = []
    view.link.connect(lambda url: links.append((url.toString(), view.last_link_text)))
    cursor = QTextCursor(view.browser.document())
    cursor.setPosition(view.browser.document().toPlainText().index("bank"))
    view.browser.pressed_at = view.browser.cursorRect(cursor).center()
    view.clicked(QUrl("https://example.org/b"))
    assert links == [("https://example.org/b", "to the bank")]


def test_deep_nesting_holds_up_nothing(qapp, tmp_path):
    view = view_of(qapp, tmp_path, "start")
    for text in ("> " * 10_000 + "deep\n", "".join("  " * i + "- x\n" for i in range(3000)),
                 "[" * 100_000 + "\n", "*" * 50_000 + "a" + "*" * 50_000 + "\n", "<div>" * 50_000 + "\n",
                 "[" * 5000 + "a" + "](b)" * 5000 + "\n", "$" + "{" * 5000 + "x" + "}" * 5000 + "$\n",
                 "> [!NOTE]\n" * 20_000):
        began = time.monotonic()
        view.render(text)
        qapp.processEvents()
        assert time.monotonic() - began < 10, text[:30]


def use_bomb(levels):
    """An SVG of a few KB whose <use> references draw 10**LEVELS paths."""
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="100" '
             'height="100"><defs><path id="a0" d="M0 0 L10 10 L0 10 Z"/>']
    for level in range(1, levels + 1):
        parts.append(f'<g id="a{level}">' + f'<use xlink:href="#a{level - 1}"/>' * 10 + "</g>")
    parts.append(f'</defs><use xlink:href="#a{levels}"/></svg>')
    return "".join(parts).encode()


def test_an_svg_whose_references_draw_too_much_is_not_drawn(qapp):
    began = time.monotonic()
    image, why = markdown_view.decode(use_bomb(9))  # (drawn, 10**9 paths: hours)
    assert image is None and why == "draws too much" and time.monotonic() - began < 2
    assert markdown_view.svg_draws_too_much(use_bomb(3)) is None, "a thousand: drawn"
    assert markdown_view.svg_draws_too_much(use_bomb(5)), "a hundred thousand: not"
    # one long path drawn a thousand times: few elements, much to draw
    path = "M0 0 " + "L1 1 " * 10_000
    heavy = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><defs>'
             f'<path id="p" d="{path}"/></defs>' + '<use xlink:href="#p"/>' * 1000 + "</svg>").encode()
    assert markdown_view.svg_draws_too_much(heavy)
    cycle = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
             b'<g id="a"><use xlink:href="#b"/></g><g id="b"><use xlink:href="#a"/></g></svg>')
    assert markdown_view.svg_draws_too_much(cycle)
    twice = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><g id="a"/>'
             b'<g id="a">' + b'<use xlink:href="#p"/>' * 10 + b'</g><path id="p" d="M0 0"/>' +
             b'<use xlink:href="#a"/>' * 3000 + b'</svg>')
    assert markdown_view.svg_draws_too_much(twice), "an id named twice counts its biggest element"
    sprite = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="40" '
              b'height="10"><defs><linearGradient id="g"><stop offset="0" stop-color="red"/></linearGradient>'
              b'<circle id="c" r="4" fill="url(#g)"/></defs>' +
              b"".join(b'<use xlink:href="#c" x="%d" y="5"/>' % (5 + 10 * i) for i in range(4)) + b"</svg>")
    assert markdown_view.svg_draws_too_much(sprite) is None
    image, why = markdown_view.decode(sprite)
    assert image is not None and image.width() == 40, why
    assert markdown_view.svg_draws_too_much(b"<svg><unclosed") is not None, "not XML: not drawn"


def test_the_markdown_flags_have_no_raw_html():
    # (PyQt6 on Python 3.11.2 lost the bits of a union of these flags - axross-markdown's finding; PySide6 keeps them,
    # and this keeps it so)
    no_html = QTextDocument.MarkdownFeature.MarkdownNoHTML.value
    assert markdown_view.FEATURES.value & no_html == no_html
    assert markdown_view.FEATURES.value & QTextDocument.MarkdownFeature.MarkdownDialectGitHub.value


def test_an_svg_of_many_shapes_filled_by_one_gradient_is_drawn():
    shapes = b"".join(b'<rect x="%d" y="0" width="1" height="1" fill="url(#g)"/>' % i for i in range(20_000))
    data = (b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="10"><defs><linearGradient id="g">' +
            b'<stop offset="0" stop-color="red"/>' * 4 + b"</linearGradient></defs>" + shapes + b"</svg>")
    assert markdown_view.svg_draws_too_much(data) is None, "a gradient draws no element again"
