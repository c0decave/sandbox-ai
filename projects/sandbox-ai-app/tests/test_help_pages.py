"""The Help's pages in one pattern (help_pages.py, help_document.py, help_shots.py): every section has one sentence of
what it is for, a screenshot of its page made from the running window with numbered callouts whose arrows end in the
real controls (named as the tours name them), "How do I…" in short steps, and its details folded away; no paragraph
of the first view longer than about three lines; both languages complete; readable type in a centred column with
WCAG AA contrast in every theme; "Show me" outlines the control in the window."""
import re

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QRectF  # noqa: E402

from sandbox_ai_app import (  # noqa: E402
    help_content as C, help_document as D, help_pages as P, help_shots as S, help_view, i18n, reading_fonts, theme,
    tours, ui,
)

from test_ui import ask, env, fake_run, window  # noqa: E402,F401 - the window of the UI tests

NAV_PAGES = {key for key, _title in ui.PAGES}
LIMIT = D.FIRST_VIEW_LINES * D.LINE_CHARACTERS  # about three lines of the column


def test_every_section_has_the_pattern_in_both_languages():
    assert set(P.VIEWS) == set(C.BY_ID), "a first view for every section, and none for a section that is gone"
    for section in C.SECTIONS:
        assert section.summary is not None and section.summary.en.strip() and section.summary.de.strip(), section.id
        assert section.summary.en != section.summary.de, section.id
        assert section.shot in NAV_PAGES, section.id
        assert section.callouts, f"{section.id}: at least one control numbered on its screenshot"
        assert 2 <= len(section.tasks) <= 6 or (section.group == "app" and len(section.tasks) >= 1), section.id
        for task in section.tasks:
            assert task.title.en.strip() and task.title.de.strip() and task.steps, (section.id, task.title.en)
            for step in task.steps:
                assert step.en.strip() and step.de.strip(), (section.id, task.title.en)
                assert step.en.startswith("$ ") == step.de.startswith("$ "), (section.id, step.en)
                if step.en.startswith("$ "):
                    assert step.en == step.de and "\n" not in step.en, "a command is the same in both languages"


def test_the_callouts_are_the_controls_the_tours_name():
    """One definition of a page's controls: every numbered control is a step of a tour on that page (its name in the
    legend is that step's title), so the Help and the tours never point at different things."""
    for section in C.SECTIONS:
        for target in section.callouts:
            name = tours.control(section.shot, target)
            assert name is not None, f"{section.id}: {section.shot}.{target} is in no tour of that page"
            assert name.en and name.de


def test_no_paragraph_of_the_first_view_is_longer_than_about_three_lines():
    for section in C.SECTIONS:
        for language in i18n.LANGUAGES:
            for text in D.first_view_paragraphs(section, language):
                if text.startswith("$ "):
                    continue
                assert len(text) <= LIMIT, f"{section.id} ({language}): {len(text)} characters: {text[:80]}…"
            for task in section.tasks:
                for step in task.steps:
                    text = getattr(step, language)
                    if not text.startswith("$ "):
                        assert len(text) <= 2 * D.LINE_CHARACTERS, f"{section.id} ({language}): a step is one line: {text}"


def words(key):
    return key


def test_the_details_are_folded_away_and_open_one_by_one():
    section = C.BY_ID["page-sandboxes"]
    shut, _ = D.render(section, "en", words, open_folds=set(), shot=None, sections=C.BY_ID)
    body = D.paragraphs(section.body.en)
    first_paragraph = re.sub(r"<[^>]+>", "", body.split("</p>")[0])[:60]
    assert first_paragraph and first_paragraph not in re.sub(r"<[^>]+>", "", shut), "never in the first view"
    assert 'href="fold:0"' in shut and "▸" in shut
    opened, commands = D.render(section, "en", words, open_folds={0}, shot=None, sections=C.BY_ID)
    assert first_paragraph in re.sub(r"<[^>]+>", "", opened) and "▾" in opened
    folds = D.fold_list(section, "en", words)
    everything, commands = D.render(section, "en", words, open_folds=set(range(len(folds))), shot=None,
                                    sections=C.BY_ID)
    assert all(command in commands for command in section.cli if command.startswith("sandbox-ai"))
    assert "sandbox-ai list --all" in commands, "the commands as blocks to copy"


def test_a_body_heading_starts_a_fold_of_its_own():
    assert C.folds("intro\n\n## One\n\ntext one\n\n## Two\n\ntext two") == [
        ("", "intro"), ("One", "text one"), ("Two", "text two")]
    assert C.folds("plain") == [("", "plain")]


def test_commands_in_the_steps_are_blocks_to_copy():
    section = C.BY_ID["network"]
    text, commands = D.render(section, "de", words, (), None, C.BY_ID)
    assert "sandbox-ai net allow RUN api.example.com" in commands
    index = commands.index("sandbox-ai net allow RUN api.example.com")
    assert f'href="copy:{index}"' in text and 'class="code"' in text


@pytest.mark.parametrize("mode", ["light", "dark", "nord", "hacker", "amber", "fallout", "unicorn", "mac"])
def test_the_help_text_keeps_aa_contrast_in_every_theme(qapp, mode):
    try:
        theme.apply(mode, qapp)
        style = help_view.HelpStyle()
        for name, foreground, background in style.pairs():
            assert theme.contrast(foreground.name(), background.name()) >= 4.5, (mode, name)
        accent, text, _halo = S.colours()
        assert theme.contrast(text.name(), accent.name()) >= 4.5, (mode, "callout number")
    finally:
        theme.apply("system", qapp)


def test_the_help_is_set_in_a_readable_font_and_size(qapp):
    reading_fonts.register()
    style = help_view.HelpStyle()
    points = qapp.font().pointSizeF()
    assert style.points == pytest.approx(max(10.5, points * 1.2), abs=0.1), "about 16 px at the default 10 pt"
    assert style.families[0] == reading_fonts.HELP_FAMILY and style.mono[0] == reading_fonts.HELP_MONO
    assert style.LINE_HEIGHT == 150


def demo(env):
    run = fake_run(env, "claude-help", session="help-demo")
    ask(run, "1700000000-9-1", kind="connect", destination="198.51.100.7:443", address="198.51.100.7", port=443,
        host="registry.example")
    return run


def test_every_page_and_screenshot_renders_in_both_themes_with_callouts_in_the_controls(window, env, qapp, wait):
    """Every section shown on the Help page: its screenshot made from the window's page, each callout's arrow ending
    inside the rectangle of the control it numbers; in the light and the dark theme, in English and German."""
    demo(env)
    window.resize(1280, 860)
    window.show()
    window.approvals.poll()
    assert wait(lambda: window.approvals.open.rowCount() > 0, 10)
    help_page = window.show_help("page-help")
    try:
        for mode in ("light", "dark"):
            theme.apply(mode, qapp)
            for language in ("en", "de"):
                help_page.language.setCurrentIndex(help_page.language.findData(language))
                for section in C.SECTIONS:
                    assert help_page.show_section(section.id)
                    shot = help_page.shot
                    assert shot is not None, f"{section.id}: no screenshot of {section.shot}"
                    assert shot.marks, f"{section.id} ({mode}): no control of {section.callouts} on screen"
                    for mark in shot.marks:
                        assert QRectF(0, 0, shot.width, shot.height).contains(mark.badge), (section.id, mark.target)
                        assert mark.rect.contains(mark.tip), f"{section.id}: the arrow of {mark.target} ends outside it"
                    text = help_page.text.toPlainText()
                    title = section.title.de if language == "de" else section.title.en
                    assert title in text and ("Wie mache ich…" if language == "de" else "How do I…") in text
                    assert f"shot:{section.id}" in help_page.text.images
    finally:
        theme.apply("system", qapp)


def test_the_text_is_a_centred_column_of_about_78_characters(window, qapp):
    window.resize(1600, 900)
    window.show()
    page = window.show_help("network")
    qapp.processEvents()
    text = page.text
    from PySide6.QtGui import QFontMetricsF
    column = text.column()
    characters = column / QFontMetricsF(text.document().defaultFont()).averageCharWidth()
    assert 60 <= characters <= 80
    frame = text.document().rootFrame().frameFormat()
    assert abs(frame.leftMargin() - frame.rightMargin()) < 1 and frame.leftMargin() >= 16, "centred"
    assert text.document().defaultFont().families()[0] == reading_fonts.HELP_FAMILY
    blocks, spaced = 0, 0
    block = text.document().begin()
    while block.isValid():
        if block.text().strip() and "￼" not in block.text():
            blocks += 1
            spaced += block.blockFormat().lineHeight() == 150
        block = block.next()
    assert blocks and spaced == blocks, "every line of text 1.5 apart"


def test_folds_open_by_a_click_and_by_a_search(window, qapp):
    page = window.show_help("page-terminals")
    sentence = "Ctrl-b d detaches from a session"
    assert sentence not in page.text.toPlainText()
    page.link("fold:0")
    assert sentence in page.text.toPlainText()
    page.link("fold:0")
    assert sentence not in page.text.toPlainText()
    page.search.setText("xterm.js")
    page.show_section("page-terminals")
    assert "QtWebEngine" in page.text.toPlainText(), "a fold holding a word searched for opens by itself"
    page.search.setText("")


def test_copy_puts_the_command_on_the_clipboard(window, qapp):
    page = window.show_help("sessions")
    index = page.commands.index("sandbox-ai attach NAME")
    page.link(f"copy:{index}")
    assert qapp.clipboard().text() == "sandbox-ai attach NAME"


def test_show_me_shows_the_page_and_outlines_the_control_the_tour_outlines(window, env, qapp):
    window.resize(1280, 860)
    window.show()
    page = window.show_help("page-hosts")
    assert page.shot is not None and page.shot.marks
    page.link("show:test_button")
    assert window.current_page() == "hosts"
    assert window.spotlight.target is window.hosts.test_button
    window.show_help("page-launch")
    assert window.current_page() == "help", "a picture of another page leaves the Help shown"


def test_the_picture_leaves_the_page_shown_and_its_panels_as_they_were(window, qapp):
    window.resize(1280, 860)
    window.show()
    window.show_page("sandboxes")
    qapp.processEvents()
    visible = {name: dock.isVisible() for name, dock in window.docks.items()}
    shot = S.capture(window, "approvals", ("open", "allow_button"), 640)
    assert shot is not None and window.current_page() == "sandboxes"
    assert {name: dock.isVisible() for name, dock in window.docks.items()} == visible


def test_a_page_without_the_window_still_shows_the_pattern(qapp):
    page = help_view.HelpPage(with_tours=False)
    try:
        page.show_section("network")
        text = page.text.toPlainText()
        assert "Network and allow-list" in text and "How do I…" in text and "Details" in text
    finally:
        page.deleteLater()


def test_the_reading_font_sets_the_help_too(qapp):
    reading_fonts.register()
    style = help_view.HelpStyle()
    from sandbox_ai_app import backend
    real = backend.app_settings
    backend.app_settings = lambda: {"reading_font": "opendyslexic"}
    try:
        assert help_view.HelpStyle().families[0] == "OpenDyslexic"
    finally:
        backend.app_settings = real
    assert style.families[0] == reading_fonts.HELP_FAMILY
