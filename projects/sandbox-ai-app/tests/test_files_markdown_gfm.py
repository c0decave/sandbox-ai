"""The Markdown viewer's GitHub additions to Qt's own parser (markdown_text.py, markdown_view.py): front matter shown
as the code it is (never a heading of the outline), GitHub's alerts ("> [!NOTE]") with their title, math ($...$,
$$...$$, a ``math`` fence) as text with the common TeX commands as their Unicode signs - never inside code, bounded in
length and depth -, the outline marking the heading being read, Copy code block. The ideas come from axross-markdown
(docs/AXROSS_MARKDOWN_REVIEW.md); the code is the app's own."""
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QTextCursor, QTextFormat  # noqa: E402

from sandbox_ai_app import files, i18n, markdown_text, markdown_view  # noqa: E402
from sandbox_ai_app import widgets as w  # noqa: E402
from test_files_manager import editor_of  # noqa: E402
from test_files_page import settle, setup  # noqa: E402,F401


# -- the text passes (no Qt) --

def test_front_matter_is_a_code_block_of_its_language():
    text = "---\nname: x\ndescription: y\n---\n\n# Title\n"
    shown = markdown_text.prepared(text)
    assert shown.startswith("```yaml\nname: x\ndescription: y\n```\n") and shown.endswith("# Title\n")
    assert markdown_text.prepared("+++\na = 1\n+++\nbody\n").startswith("```toml\na = 1\n```\n")
    assert markdown_text.prepared("---\na: b\n...\nbody\n").startswith("```yaml\na: b\n```\n"), "YAML's own end"
    # (a fence longer than any run of backticks inside: the block ends where the front matter does)
    assert markdown_text.prepared("---\nx: ```\n---\n").startswith("````yaml\nx: ```\n````\n")
    for text in ("text\n---\na: b\n---\n", "---\nnever closed\n", "--- \n"):
        assert markdown_text.prepared(text) == text, text


def test_alerts_get_their_title_on_a_line_of_its_own():
    shown = markdown_text.prepared("> [!NOTE]\n> Read this.\n\n> [!warning]\n> Careful.\n")
    assert shown == "> **ℹ Note**\n>\n> Read this.\n\n> **⚠ Warning**\n>\n> Careful.\n"
    for kind, title in (("TIP", "★ Tip"), ("IMPORTANT", "‼ Important"), ("CAUTION", "✖ Caution")):
        assert markdown_text.prepared(f"> [!{kind}]\n> x\n").startswith(f"> **{title}**\n"), kind
    for text in ("> [!NOTE] same line\n", "> [!OTHER]\n> x\n", "[!NOTE]\n", "```\n> [!NOTE]\n```\n"):
        assert markdown_text.prepared(text) == text, f"not an alert: {text!r}"
    i18n.set_language("de")
    try:
        assert markdown_text.prepared("> [!TIP]\n> x\n").startswith("> **★ Tipp**")
    finally:
        i18n.set_language("en")


def test_math_is_text_with_the_signs_of_its_commands():
    math = markdown_text.math_text
    assert math(r"\alpha + \beta \leq \gamma") == "α + β ≤ γ"
    assert math("x^2 + y_1 = z^{n+1}") == "x² + y₁ = zⁿ⁺¹"
    assert math("e^{i\\pi}") == "e^(iπ)", "no small letter for every character: written with ^(...)"
    assert math(r"\frac{a+b}{2}") == "(a+b)/2" and math(r"\frac12") == "1/2"
    assert math(r"\sqrt{x+1} \cdot \sqrt x") == "√(x+1) ⋅ √x"
    assert math(r"\sum_{i=1}^{n} i") == "∑ᵢ₌₁ⁿ i" and math(r"\frac{1}{i^2} + \frac{2a}{b}") == "1/i² + (2a)/b"
    assert math(r"\mathbb{R}^n \to \mathbb{N}") == "ℝⁿ → ℕ"
    assert math(r"\left( x \right) \text{ for all } x \in A") == "( x ) for all x ∈ A"
    assert math(r"\vec v \quad \hat{x}") == "v\u20d7 \u2003 x\u0302"
    assert math(r"\begin{cases} 1 & x > 0 \\ 0 & \text{else} \end{cases}") == "1   x > 0\n0   else"
    assert math(r"\unknown{x}") == r"\unknown{x}", "an unknown command as written"
    deep = "{" * 500 + "x" + "}" * 500
    assert math(deep) is None, "too deep: shown as written"


def test_formulas_are_found_outside_code_only():
    prepared = markdown_text.prepared
    shown = prepared("Euler: $e^{i\\pi} + 1 = 0$, costs $5 and $10.\n")
    assert "e\\^\\(iπ\\) \\+ 1 \\= 0" in shown and "$5 and $10" in shown, "money is no formula"
    assert prepared("`$x^2$` and ``a $b$ c``\n") == "`$x^2$` and ``a $b$ c``\n", "code spans kept"
    assert prepared("```\n$x^2$\n```\n") == "```\n$x^2$\n```\n", "fenced code kept"
    assert prepared("para\n\n    $x^2$ indented code\n") == "para\n\n    $x^2$ indented code\n"
    assert prepared("\\$x^2$ escaped\n") == "\\$x^2$ escaped\n"
    assert prepared("[a](https://x.org/$a$) $b$\n") == "[a](https://x.org/$a$) b\n", "never in a link's address"
    display = prepared("before\n\n$$\n\\int_0^1 x\\,dx\n$$\n\nafter\n")
    assert "\n\n∫₀¹ x\u2009dx\n\n" in display and "$$" not in display
    assert prepared("$$a^2$$\n").strip() == "a²"
    quoted = prepared("> $$\n> \\alpha\n> $$\n")
    assert "> α" in quoted and "$$" not in quoted
    assert prepared("```math\n\\beta\n```\n").strip() == "β"
    assert prepared("$$\nnever closed\n") == "$$\nnever closed\n"


def test_the_passes_are_bounded_on_hostile_texts():
    for text in ("$" * 200_000, "$a" * 100_000, "$\\frac{" * 20_000 + "$", "> [!NOTE]\n" * 50_000,
                 "$$\n" + "x\n" * 100_000, "`$x$" * 50_000, "---\n" + "a: b\n" * 100_000 + "---\n"):
        began = time.monotonic()
        markdown_text.prepared(text)
        assert time.monotonic() - began < 3, text[:20]
    long = "$" + "x" * (markdown_text.MATH_CHARS + 1) + "$"
    assert markdown_text.prepared(long) == long, "a formula past MATH_CHARS stays as written"


# -- rendered --

def view_of(qapp, tmp_path, text):
    view = markdown_view.MarkdownView(markdown_view.ImageSource(files.Root(tmp_path), "readme.md"))
    view.resize(700, 400)
    view.show()
    view.render(text)
    qapp.processEvents()
    return view


def test_front_matter_alerts_and_math_rendered(qapp, tmp_path):
    view = view_of(qapp, tmp_path, "---\nname: skill\ndescription: does things\n---\n\n# Skill\n\n"
                   "> [!WARNING]\n> Mind it.\n\nArea $\\pi r^2$.\n")
    plain = view.browser.document().toPlainText()
    assert "name: skill" in plain and "⚠ Warning" in plain and "Area πr²." in plain.replace("π r", "πr")
    assert [view.outline.item(i).text() for i in range(view.outline.count())] == ["Skill"], \
        "front matter is no heading"
    block = view.browser.document().begin()
    while block.isValid() and "name: skill" not in block.text():
        block = block.next()
    assert block.blockFormat().property(QTextFormat.Property.BlockCodeLanguage) == "yaml"


def test_the_outline_marks_the_heading_being_read(qapp, tmp_path):
    text = "".join(f"# Part {n}\n\n" + "line\n\n" * 30 for n in range(6))
    view = view_of(qapp, tmp_path, text)
    assert view.outline.currentRow() == 0
    view.jump(view.outline.item(4))
    qapp.processEvents()
    assert view.outline.currentRow() == 4
    view.browser.verticalScrollBar().setValue(0)
    qapp.processEvents()
    assert view.outline.currentRow() == 0


def test_copy_code_block(qapp, tmp_path):
    view = view_of(qapp, tmp_path, "text\n\n```sh\nmake\nmake test\n```\n\nafter\n")
    document = view.browser.document()
    block = document.begin()
    while block.isValid() and block.text() != "make test":
        block = block.next()
    assert view.browser.code_block_at(block.position()) == "make\nmake test"
    assert view.browser.code_block_at(document.begin().position()) is None, "no code there"
    menu = view.browser.menu_at(view.browser.cursorRect(QTextCursor(block)).center())
    assert tr_copy() in [action.text() for action in menu.actions()]
    [action] = [action for action in menu.actions() if action.text() == tr_copy()]
    action.trigger()
    assert QtWidgets.QApplication.clipboard().text() == "make\nmake test"
    assert tr_copy() not in [a.text() for a in view.browser.menu_at(QPoint(1, 1)).actions()]


def tr_copy():
    return i18n.tr("Copy code block")


def test_a_skill_file_opened_from_the_files_page(setup, wait):
    page, ws, host, outside = setup
    (ws / "SKILL.md").write_text("---\nname: deploy\ndescription: ships it\n---\n\n# Deploy\n\n> [!CAUTION]\n"
                                 "> Production.\n\nCost $O(n \\log n)$.\n\n## Steps\n")
    page.left.list()
    settle(page.left)
    page.left.open_row(w.find_row(page.left.table, "SKILL.md"))  # a double click
    window = editor_of(page, wait)
    view = window.markdown
    assert wait(lambda: "Production." in view.browser.document().toPlainText())
    plain = view.browser.document().toPlainText()
    assert "name: deploy" in plain and "✖ Caution" in plain and "O(n log n)" in plain
    assert [view.outline.item(i).text() for i in range(view.outline.count())] == ["Deploy", "    Steps"]
    window.close()
