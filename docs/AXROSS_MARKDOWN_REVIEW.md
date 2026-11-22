# axross-markdown: review for sandbox-ai

axross-markdown (c0decave/axross-markdown, Apache-2.0, Python) is the Markdown viewer of axross, the user's file
manager, as a program of its own: a Qt window (PyQt6) that renders GitHub Markdown with an outline, alerts, math as
text, Pygments-coloured code and the images beside the file, an editor with a live preview, `axross-mdcat` for the
terminal and an HTML/PDF export. Its `ui/markdown_view.py` says it began as a port of sandbox-ai's own
`sandbox_ai_app/markdown_view.py`, the Files page's viewer, and has grown since. This review asks whether the app's
viewer should take it in. Reviewed on 2026-11-21.

**Decision: not vendored.** Its ideas that are better than ours were ported into the app's reader as the app's own
code (see [What sandbox-ai took](#what-sandbox-ai-took)); two of them close holes found in ours during this review.

## The commit

| | |
|---|---|
| Repository | https://github.com/c0decave/axross-markdown, licence **Apache-2.0** (`LICENSE`: the full Apache License 2.0 text, its appendix's copyright line filled in - likely why GitHub's detection says NOASSERTION; `pyproject.toml` says `license = "Apache-2.0"`; no NOTICE file) |
| Commit | `0e782b2e92fcdbd595cec0a08a9283a6465aa27c` on `main`, 2026-11-20 19:52 UTC (0.16.8). The repository has 10 commits, the first public release (0.16.0) on 2026-11-19: **no commit is 14 days old**, so none may be pinned (third-party code is pinned only when at least 14 days old) |
| Size | 155 files: 69 Python modules under `src/` (38,863 lines), `resources/i18n/de.json` (5,200 lines, axross's whole catalogue), 35 test files, golden PNGs, packaging (PyInstaller, AppImage, dmg), two workflows |
| Stack | Python >= 3.11, **PyQt6** `>=6.6,<6.12`, markdown-it-py `>=3.0`, mdit-py-plugins `>=0.4`, Pygments `>=2.17`, Pillow `>=10.0`; linkify-it-py optional |
| Lock | `requirements.lock.txt`: every pin with its SHA-256 (uv), checked in CI by axross's supply-chain gate (`min_age_days = 14`, OSV, provenance, a reviewed baseline) |

Verified here: a clone into an empty folder (never run outside a check of our own; no file of it imported), every file
listed, the licence read, the release dates of the locked pins on pypi.org, a grep of every module for sockets, HTTP
clients, `subprocess`, `exec`/`eval`, `ctypes`, environment writes and hard-coded hosts, the rendering path read line by
line (`core/markdown_doc.py`'s sanitizer and link checks, `ui/markdown_safe.py`, `ui/markdown_view.py`'s loading,
links, images and SVG handling, `core/svg_gate.py`, `core/image_worker.py`'s child, `core/desktop_open.py`,
`ui/markup_guard.py`), the rest of `ui/markdown_view.py` (7,832 lines), `ui/markdown_html.py` and `core/markdown_doc.py`
by their functions and docstrings, axross's support modules by their docstrings and the grep. Each finding that
applies to sandbox-ai was measured against our reader (PySide6 6.12.0, offscreen).

## Dependencies

| Package (locked) | Released | Licence | Python | For sandbox-ai |
|---|---|---|---|---|
| PyQt6 6.11.0, PyQt6-Qt6 6.11.2 | 2026-03-30, 2026-08-23 | **GPL-3.0-only** (or Riverbank's commercial licence), Qt LGPL-3.0 | >= 3.10 | the app uses PySide6 (LGPL); the code would have to be ported |
| markdown-it-py 4.2.0 | 2026-05-07 | MIT | >= 3.10 | new dependency; not on Python 3.9 |
| mdit-py-plugins 0.6.1 | 2026-05-13 | MIT | >= 3.10 | new dependency; not on Python 3.9 |
| Pygments 2.21.0 | 2026-08-17 | BSD-2-Clause | >= 3.9 | new dependency |
| Pillow 13.0.0 | 2026-10-16 | MIT-CMU | >= 3.11 | new dependency (images, the terminal) |
| linkify-it-py 2.2.0, mdurl 0.1.2 | 2026-08-29, 2022-08-14 | MIT | >= 3.10 | optional / transitive |

Every locked pin is older than 14 days. The app depends on PySide6-Essentials alone and supports Python 3.9 to 3.13;
taking these in would raise its floor to 3.11 (or make the viewer optional on 3.9 and 3.10) and add four packages to
the wheel, the AppImage, the disk image and the pyz.

## How it renders

- **Parser:** markdown-it-py with mdit-py-plugins (tables, strike-through, footnotes, task lists, definition lists,
  front matter, heading anchors) and its own rules (GitHub's alerts, math, emoji short codes, `~single~` tildes,
  bare links). Without markdown-it-py (axross itself) Qt's own md4c parser, with a long chain of text passes.
- **Output:** HTML in Qt's rich-text subset, shown in a `QTextBrowser` (no QtWebEngine): no script can run, Qt's rich
  text has none. Code coloured by Pygments with inline styles, a style sheet in the palette's colours.
- **Raw HTML:** kept as an **allowed subset** - `b i em u s del ins mark small sub sup kbd code br img a`, and as blocks
  `p div center details summary h1-h6 hr blockquote pre table … ul ol li` - each with only its allowed attributes
  (`align`; an image's `src`, `alt`, `width`/`height` up to 2000 px; a link's checked `href`); `<script>`, `<style>`,
  `<iframe>`, `<svg>`, `<math>` and the like are dropped with their text; at most 16 tags deep and 2 tables in each
  other; the views write HTML of those events only (`core/markdown_doc._Sanitizer`, the standard library's
  `html.parser`). With md4c every `<` is escaped first (`ui/markdown_safe._escape_html`), so even a feature flag
  that lost its bits shows no raw HTML.
- **Links:** markdown-it's `validateLink` (no `javascript:`, `vbscript:`, `file:`, `data:` but for images; spaces and
  controls inside a scheme removed first); a refused link shows as text. A click never follows a link inside the
  view: `#heading` scrolls, anything else goes to the window, which asks before a web link opens - the address
  as sent (`QUrl.toEncoded`: an IDN host in punycode, bidi controls visible), its host, the link's text when it
  differs (`ui/markdown_safe.open_link_question`). Opening: `QDesktopServices.openUrl`, in a frozen Linux build
  `xdg-open` with a clean environment (a target starting with `-` refused).
- **Images:** only beside the file (never `../` above its folder, never through a symbolic link, never an absolute
  path, `file:` or another scheme; never a secret file of the home folder: `core/protected_places`), at most 8 MiB,
  40 million pixels each, 200 images and 100 million pixels together. Decoding runs in a killable child process
  (`python -I -c`, RLIMIT_DATA, a CPU budget, `PR_SET_PDEATHSIG`); an SVG passes `core/svg_gate` (nothing it names
  outside itself, no entity, its `<use>` references may draw at most 20,000 elements again) and is drawn in a child
  of its own. Remote images are never loaded: a chip names them.
- **Math:** its own TeX-to-text converter (`core/markdown_doc.math_runs`, about 800 lines: Greek, operators,
  scripts as Unicode, fractions and big operators laid out as grids, matrices and cases) - no MathJax or KaTeX,
  nothing loaded.
- **Bounds:** markdown-it's nesting limit, cuts for long lines, many list items, table cells, spans and formulas
  (shown as written where they stand), a parse time limit, Pygments in a killable child (a catastrophic regex of a
  lexer holds up no window), a page laid out in parts with stand-ins while the window stays responsive.
- **Everything else:** `ui/markup_guard.py` patches every `QLabel`, tooltip, message box and input dialog of the
  process when the `ui` package is imported, so no text from outside can make Qt load an image it names.

## What it reads, writes and sends

| | |
|---|---|
| Network | nothing in the viewer: no socket, HTTP client or Qt network class in `src/`; `core/tls_env.py` only points `SSL_CERT_FILE` at the host's CA bundle for axross's remote backends. No telemetry, no update check |
| Files read | the Markdown file and the images beside it, through axross's backend layer (`core/local_fs.py`, `backend_registry.py`, `path_policy.py`, `protected_places.py`) |
| Files written | its window state and settings (`ui/state_files.py`, `core/json_store.py`, `~/.config/axross…`), an export the user chooses, the editor's saves (atomically) |
| Processes | its own decoding, lexing and SVG children (`python -I -c`, or the frozen binary with `--image-worker` / `--lex-worker` / `--svg-worker`), `xdg-open` for a link (frozen builds), `less -R` for `axross-mdcat` |
| CI | actions pinned by commit, `permissions: contents read`, `pip install --require-hashes`, the supply-chain gate before the tests and before every release |

No backdoor, no obfuscated code, no hidden network access was found.

## Code quality

Careful and defensive: nearly every branch says in a comment what it fixed and how it was measured ("20,000 tags,
46 s", "a heading of 1,000 emoji was 0.4 s"), the CHANGELOG lists 22 review rounds, and 35 test files back it. The
cost is complexity: `ui/markdown_view.py` alone is 7,832 lines (ours: 563); the md4c path carries state through Qt's
parser in private-use characters and cleans them out of the document afterwards; the page is laid out in parts with
stand-in blocks, jumps, gaps and a held scroll position. About half of `src/` is axross's file-manager layer (backends
for SFTP, S3, SMB …, atomic writes, secure storage, protected places, connect workers) that a viewer needs only as
its file access.

## Compared with sandbox-ai's reader (before this review)

| | sandbox-ai 0.1.190 | axross-markdown 0.16.8 |
|---|---|---|
| Parser | Qt's md4c, GitHub dialect, no HTML | markdown-it-py (md4c without it) |
| Tables, task lists, strike-through, autolinks | yes | yes |
| Footnotes | yes (by their names) | yes (numbered by order of reference, back links) |
| Front matter | shown as a heading ("name: x description: y" in the outline) | a table or a code block |
| GitHub alerts | `[!NOTE]` shown as text | title, sign and colour |
| Math | as written (`$x^2$`) | text with Unicode signs, display formulas as grids |
| Code highlighting | any language: comments, strings, numbers, keywords, in the theme's colours | Pygments per language, GitHub's palette |
| Outline | yes, a click jumps | yes, the heading being read marked; Back / Forward |
| Raw HTML | shown as text | an allowed subset rendered (`<img>`, `<details>`, `<p align>` …) |
| Images | beside the file, through the pane's Root, bounded; natural size | beside the file, bounded; fitted to the width, a click zooms |
| Themes | the app's looks (light, dark, retro …) | its light and dark palette |
| i18n | English, German | English, German (axross's catalogue) |
| Link question | the URL as `QUrl.toString()` | the address as sent, its host, the link's text |
| SVG | nothing named outside it; no limit on what its references draw | as left, plus the 20,000-element limit and a killable child |
| Big files | the rendering stops before what would hold up the window and says so | shown as written where it stands, laid out in parts |
| Export | none | HTML (with a CSP that loads nothing), PDF, print; `axross-mdcat` |

## Findings that apply to sandbox-ai (measured)

1. **An SVG whose references draw it again and again held up a thread for good.** Our `svg_refers` let every `#id`
   reference through. Ten levels of ten `<use>` (1.9 KB, all references inside the file) took 0.06 s at five
   levels, 0.6 s at six, 6.2 s at seven - ten times longer each level - and the test run with nine never returned
   (stopped after three minutes): an agent's image pins a worker thread and a CPU core. **Fixed:**
   `markdown_view.svg_draws_too_much` (one streaming expat parse and one pass, no tree) refuses an SVG whose
   references draw more than 20,000 elements or 2,000,000 characters of attributes again, or refer to themselves;
   such an image is a box saying "draws too much" (nine levels: refused at once). Gradients count as nothing drawn,
   so 20,000 shapes filled by one gradient still show.
2. **A look-alike host showed as the host it imitates.** The question named `QUrl.toString()`: a link to
   `https://аpple.com/` (Cyrillic а) read "Open https://аpple.com/ …". **Fixed:** the question shows the address as
   it is sent (`xn--pple-43d.com`), the host on a line of its own, a bidi control percent-encoded, a long address
   cut with its length said, and the link's text when it differs from the address ("The link in the file reads:
   …"); a URL Qt finds invalid (a bidi control in its host) is not offered at all.
3. **PyQt6's Markdown flags on Python 3.11.2** (axross's finding: `GitHub | NoHTML` lost its bits, raw HTML on):
   **not ours.** Measured with PySide6 6.12.0 on Python 3.11.2 (Debian 12's) and 3.12: the union is `0x104f6c` on
   both and raw HTML stays text. A test now keeps the NoHTML bits in `FEATURES`.
4. **`](<my file.md>)`** (axross: Qt read the angle brackets as a tag): **not ours** - PySide6 6.12's md4c reads it.
5. **Deep nesting** (10,000 quote levels, 3,000 nested list levels, 100,000 `[`, 50,000 `*` around a word, 50,000
   `<div>`): **not ours** - each renders in under half a second; now a permanent test.

## What sandbox-ai took

Written anew in the app's style (Python 3.9, PySide6, the app's `tr()`, themes and Root), no file copied:

- `sandbox_ai_app/markdown_text.py` (new): front matter as a code block of its language (no longer a heading of the
  outline - every SKILL.md of an agent has one); GitHub's five alerts with their title in English or German, coloured
  in the look's colours; math - `$…$`, `$$…$$` (also over several lines and in a quote) and a `math` fence - as text
  with about 250 TeX commands as their Unicode signs or text, scripts as small letters, fractions, roots, accents,
  `\mathbb`, environments (cases, matrices) in columns; never inside code, a formula past 2,000 characters or 40
  levels of groups shown as written, every pass linear (a 5 MB file of formulas: 1.2 s; a 5 MB plain one: 7 ms).
- `markdown_view.py`: the SVG redraw limit (finding 1), the outline marking the heading being read as the page
  scrolls, *Copy code block* on a fenced block's right-click menu, the clicked link's text kept for the question.
- `file_editor.py`: the link question of finding 2.
- Tests: `tests/test_files_markdown_hostile.py` (a hostile file - `<script>`, `onerror=`, `<style>`, `<iframe>`,
  `javascript:`/`vbscript:`/`data:`/`file:` links, remote, absolute and `../` images, `/dev/zero`, deep nesting, the
  SVG bomb, look-alike hosts and bidi controls in the question - does nothing) and
  `tests/test_files_markdown_gfm.py` (the new rendering, also opened from the Files page).

## Not taken, and why

- **Vendoring the module.** No commit is 14 days old; the code is PyQt6 (GPL-3.0) and would have to be ported, about
  15,000 lines with its axross layer, which also patches Qt classes process-wide (`markup_guard`) and needs entry
  points of the frozen binary for its children; Python >= 3.11 and four new packages against the app's 3.9 and
  PySide6 alone. Our own reader is a fraction of the size and keeps the same safety properties.
- **Raw HTML as an allowed subset** (`<img>`, `<details>`, `<p align=center>`): GitHub READMEs use it, but it widens
  what an agent's file can make Qt do (sizes, nesting, tables); ours keeps NoHTML, raw HTML is shown as text.
- **Pygments per language** (a new dependency) and **emoji short codes** (axross froze on machines without a full
  emoji font, 0.16.7-0.16.8).
- **Laying out a big page in parts** and showing what is too big as written where it stands: ours stops before it and
  says so (the Edit view shows all). A candidate for later.
- **Decoding images in a killable child process**: ours decodes in a thread, bounded in bytes and pixels; the SVG
  limit covers the case measured here. A crafted raster that hangs a decoder remains a thread lost until the app
  ends. A candidate for later.
- Images fitted to the width with a zoom on click, Back / Forward, HTML/PDF export, footnotes numbered by order,
  definition lists, `axross-mdcat`.
