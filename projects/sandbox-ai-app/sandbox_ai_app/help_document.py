"""A Help section as the page shows it (HTML for Qt's text browser; no Qt here): every section in one pattern -

- its title and one sentence of what it is for;
- a screenshot of its page with numbered callouts (help_shots.py draws it; here it is an image named shot:ID) and
  the legend: number, the control's name as the tours call it, "Show me";
- "How do I…": a few tasks, each in short numbered steps, a command as a block to copy;
- the details folded away (a link opens each fold), the command line, related sections.

Texts are escaped: nothing in them is markup but `code`, "- " bullets and paragraphs. The style (fonts, sizes,
colours) comes from help_view.HelpStyle; links are handled there: fold:N, copy:N, show:TARGET, zoom:shot,
section:ID.
"""
from __future__ import annotations

import html
import re

from . import i18n
from .i18n import N_


# Words the page passes through words(); marked so the translation guard sees them.
WORDS = (N_("How do I…"), N_("More about this"), N_("Related"), N_("Show me"), N_("not on screen now"),
         N_("Screenshot of the page with its controls numbered"))


def say_in(text, language=None):
    """TEXT (tours.Text) in LANGUAGE (default: the app's)."""
    return text.de if (language or i18n.language()) == "de" else text.en

FIRST_VIEW_LINES = 3  # no paragraph of the first view is longer than about this many lines
LINE_CHARACTERS = 78  # the text column: at most about this many characters a line


def inline(text):
    """TEXT escaped, with `code` as <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text))


def paragraphs(text):
    """TEXT (paragraphs separated by a blank line, "- " bullets) as HTML."""
    parts = []
    for block in text.split("\n\n"):
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        run, bullets = [], []

        def flush():
            if run:
                parts.append(f"<p>{inline(' '.join(run))}</p>")
                run.clear()
            if bullets:
                parts.append("<ul>" + "".join(f"<li>{inline(b)}</li>" for b in bullets) + "</ul>")
                bullets.clear()
        for line in lines:
            if line.startswith("- "):
                if run:
                    flush()
                bullets.append(line[2:])
            else:
                if bullets:
                    flush()
                run.append(line)
        flush()
    return "\n".join(parts)


def code_block(command, index, copy_label):
    """COMMAND as a block (one cell, its own background) with a link to copy it."""
    return (f'<table class="code" width="100%" cellspacing="0" cellpadding="8"><tr><td class="code">'
            f"<code>{html.escape(command)}</code></td>"
            f'<td class="copy" align="right" valign="top" width="90"><a href="copy:{index}">'
            f"{html.escape(copy_label)}</a></td>"
            "</tr></table>")


def is_command(text):
    return text.startswith("$ ")


def render(section, language, words, open_folds=(), shot=None, sections=None):
    """SECTION (help_content.Section) as HTML in LANGUAGE; WORDS translates the page's own words
    (words(english) -> text). OPEN_FOLDS: the numbers of the folds shown open. SHOT: (width, height, legend) of the
    screenshot when there is one - legend [(number, target, name, shown)], name already in LANGUAGE. Returns
    (html, commands): the commands the copy:N links copy."""
    commands = []
    parts = [f"<h1>{html.escape(say_in(section.title, language))}</h1>"]
    if section.summary is not None:
        parts.append(f'<p class="lead">{inline(say_in(section.summary, language))}</p>')
    if shot is not None:
        width, height, legend = shot
        alt = html.escape(words("Screenshot of the page with its controls numbered"))
        parts.append(f'<p class="shot" align="center"><a href="zoom:shot"><img src="shot:{section.id}" '
                     f'width="{width}" height="{height}" alt="{alt}"></a></p>')
        if legend:
            rows = []
            for number, target, name, shown in legend:
                note = "" if shown else f' <span class="note">({html.escape(words("not on screen now"))})</span>'
                badge = (f'<img src="badge:{number}" width="22" height="22" alt="{number}">' if number else
                         '<span class="note">–</span>')
                rows.append(f'<tr><td width="30" valign="middle" align="center">{badge}</td>'
                            f'<td valign="middle">{html.escape(name)}{note}</td>'
                            f'<td align="right" valign="middle"><a href="show:{html.escape(target)}">'
                            f"{html.escape(words('Show me'))}</a></td></tr>")
            parts.append('<table class="legend" width="100%" cellspacing="0" cellpadding="3">' + "".join(rows)
                         + "</table>")
    if section.tasks:
        parts.append(f"<h2>{html.escape(words('How do I…'))}</h2>")
        for task in section.tasks:
            parts.append(f'<h3>{inline(say_in(task.title, language))}</h3>')
            steps, start = [], 1
            for step in task.steps:
                text = say_in(step, language)
                if is_command(text):
                    if steps:
                        parts.append(ordered(steps, start))
                        start += len(steps)
                        steps = []
                    commands.append(text[2:])
                    parts.append(code_block(text[2:], len(commands) - 1, words("Copy")))
                else:
                    steps.append(text)
            if steps:
                parts.append(ordered(steps, start))
    folds = fold_list(section, language, words)
    if folds:
        parts.append(f"<h2>{html.escape(words('Details'))}</h2>")
    for number, (heading, body, cli) in enumerate(folds):
        is_open = number in open_folds
        mark = "▾" if is_open else "▸"
        parts.append(f'<p class="fold"><a href="fold:{number}">{mark} {html.escape(heading)}</a></p>')
        if is_open:
            parts.append('<div class="folded">')
            if body:
                parts.append(paragraphs(body))
            for command in cli:
                if command.startswith("sandbox-ai"):
                    commands.append(command)
                    parts.append(code_block(command, len(commands) - 1, words("Copy")))
            options = [c for c in cli if not c.startswith("sandbox-ai")]
            if options:
                parts.append("<p>" + ", ".join(f"<code>{html.escape(c)}</code>" for c in options) + "</p>")
            parts.append("</div>")
    if sections is not None:
        links = [f'<a href="section:{s}">{html.escape(say_in(sections[s].title, language))}</a>'
                 for s in section.see if s in sections]
        if links:
            parts.append(f'<p class="related"><b>{html.escape(words("Related"))}:</b> ' + " · ".join(links) + "</p>")
    return "\n".join(parts), commands


def ordered(steps, start=1):
    return f'<ol start="{start}">' + "".join(f"<li>{inline(s)}</li>" for s in steps) + "</ol>"


def fold_list(section, language, words):
    """The folds of SECTION: [(heading, body text, commands)] - its details (one fold per "## Heading" of the body,
    "More about this" for the text before the first) and "Command line" with its commands and options."""
    from .help_content import folds
    found = []
    parts = folds(say_in(section.body, language))
    for heading, body in parts:
        found.append((heading or words("Overview" if len(parts) > 1 else "More about this"), body, ()))
    if section.cli:
        found.append((words("Command line"), "", tuple(section.cli)))
    return found


def first_view_paragraphs(section, language):
    """The texts of SECTION's first view (what is shown before anything is unfolded): the sentence, the tasks'
    titles and steps."""
    texts = []
    if section.summary is not None:
        texts.append(say_in(section.summary, language))
    for task in section.tasks:
        texts.append(say_in(task.title, language))
        texts.extend(say_in(step, language) for step in task.steps)
    return texts


def fold_texts(section, language):
    """[(heading, text)] of SECTION's folds in LANGUAGE (for the search: which folds hold a word)."""
    from .help_content import folds
    found = [(heading, body) for heading, body in folds(say_in(section.body, language))]
    if section.cli:
        found.append(("", " ".join(section.cli)))
    return found
