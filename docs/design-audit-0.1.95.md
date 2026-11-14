# Design and code review of the desktop app, 0.1.95

The app (projects/sandbox-ai-app) examined with the skill kits that 0.1.94 vendored (sandbox/builtin/kits):
**hallmark** (its audit mode: the screenshots first, then the gates, the six-axis critique; app-screen rules from
`references/flow.md` § App screens), **design-system** (its audit: tokens, naming, near-duplicate components, missing
states, accessibility), **superpowers** (code review, systematic debugging: root cause before a fix, test-first),
**security-audit** (guidance mode: trust boundaries only, no full audit) and **tdd** (red, then green, one slice at a
time). The file manager (files_page, files, file_editor, file_properties, markdown_view, remote) was reviewed
elsewhere and is left out here; its pages were rendered but not judged.

## How the pictures were made

`docs/video/capture_app.py` (new: `--theme NAME`) renders every page and 24 states (dialogs, menus, tabs) offscreen
with its staged demo (a project, a run asking two questions, its logs, a host, a kit, secrets, an identity, an LLM
server), at 1600 x 900, scale 1, in **dark and light**, **English and German**: 4 x 42 = 168 pictures. They are not
in git; to make them again:

    SANDBOX_AI_HOME=$PWD QT_QPA_PLATFORM=offscreen python docs/video/capture_app.py \
        --app projects/sandbox-ai-app --out OUT/dark --lang both --scale 1 --theme dark   # and --theme light

Files looked at (each as `NAME-en.png` / `NAME-de.png` in `OUT/dark` and `OUT/light`): activity,
activity-safeguards, approvals, approvals-blocked, board, board-gallery, board-preset, connections,
connections-record, credentials, credentials-give, credentials-secrets, credentials-vpn, help, help-section, hosts,
hosts-defaults, images, images-extension, images-yours, kits, kits-marketplaces, launch, launch-network, llm,
llm-parameters, mcp, mcp-properties, sandboxes, sandboxes-cut, statistics, system, system-log, system-settings,
team, terminals (and files, files-editor, files-markdown, files-menu, files-properties, files-remote, not judged).
After the fixes, launch, approvals, credentials and images were taken again (light/en) and checked. Judged closely
before: every page in dark/en; launch, sandboxes, approvals in dark/de; approvals, credentials,
system-settings, board-gallery in light/en. Focus states were rendered separately (each focusable kind, resting and
reached with Tab, in light and dark).

A probe (a `sitecustomize` that logs every subprocess started and every `time.sleep` of 50 ms or more on the GUI
thread from the app's code) ran through the same 42 pages and states: **nothing** was started or slept on the GUI
thread.

## The read (hallmark audit, four lines)

- **For:** someone who runs coding agents in sandboxes and needs to see, allow, refuse and stop what they do; the
  one action is answering what a sandbox asks (Approvals) and starting one (Start, Board).
- **Works and stays:** the board (a real object this tool is known by: the start drawn as parts and wires, with its
  security score), the Approvals table with the countdown, the Activity timeline with its kinds in colour, the
  themes' contrast discipline (every text pair 4.5:1, borders 3:1, tested), one accent per theme, tables before
  cards, nothing decorative, no invented figure anywhere.
- **Reads as generated / template:** tracked-capital section labels in the navigation and the Help list (PROJECT,
  RUNNING, THE APP: hallmark floor 9), middle-dot-joined meta strings (`App 0.1.94 · sandbox-ai 0.1.94`,
  `easy · score 100`: floor 10), many (?) marks (one per row of the board's parts list: restraint).
- **Was broken:** keyboard focus invisible on tool buttons, primary buttons, check boxes, tabs and the navigation
  (floor 6); the last column title cut on both sides wherever a table carries a (?) (`tat`, `yte`, `uestion`);
  short columns fitted while still empty (`39…go`, `no ti…limit`); a never-filled table at Qt's 100 px per column
  (`gent versio`); labels 5 px above the middle of their fields in every wrapping row; the project folder's own name
  never shown in its board node; the app's version a release behind (`App 0.1.93`).

## Six-axis scores (hallmark § 3, applied to the screens as they are)

| Screen | P | H | E | S | R | V | Weakest |
|---|---|---|---|---|---|---|---|
| Start (Launch) | 3 | 2 | 3 | 4 | 2 | - | H: a toolbar of combo, label, (?) and button runs into the options; three rows of choices before the form |
| Board | 4 | 4 | 3 | 5 | 3 | - | E: inspector fields cut at the right at 1600 px with both docks |
| Sandboxes | 3 | 3 | 3 | 4 | 3 | - | E: run name and host cut in the middle in German while the path takes 25 % |
| Approvals | 4 | 4 | 3 | 5 | 4 | - | E: Asked / Refused in cut (fixed) |
| Kits | 3 | 3 | 3 | 3 | 2 | - | R: tab pane > group box > table, three frames deep |
| MCP / LLM / Images / Teams | 3 | 3 | 3 | 3 | 3 | - | E: failures in the ordinary grey (fixed for Images, LLM); empty tables with no word inside |
| Credentials | 3 | 4 | 3 | 4 | 2 | - | R: whole rows in the status colour |
| Connections / Activity / Statistics | 4 | 4 | 4 | 4 | 4 | - | E: last title cut (fixed) |
| System / Hosts / Help | 3 | 3 | 3 | 3 | 3 | - | H: Help's section list scrolls sideways, titles cut |
| Settings dialog | 3 | 3 | 3 | 3 | 3 | - | E: a 2-digit spin box 480 px wide |
| Preset gallery | 4 | 4 | 4 | 4 | 3 | - | R: meta lines joined by middle dots |

V (variety against earlier builds) does not apply to an existing app. No axis of a screen is 1; the 2s are the
proposals below.

## Design-system audit

**Summary.** Components reviewed: 14 (button and its roles, tool button, (?) help button, field, combo, check box,
radio, tab bar, project tabs, navigation, table/tree, group box, status line, toast). Issues found: 23, 10 fixed
here. Score: 68/100 before, 78/100 after (tokens and contrast strong; states and type scale weak).

### Token coverage

| Category | Defined (theme.py) | Hardcoded found |
|---|---|---|
| Colours | 24 colour tokens + 4 status per theme, 19 themes, contrast-tested | theme extras: mac `#b9b9be #a1a1a6 #e5e5ea #dedee3 #d1d1d6` (and `#7fb0ee`, now the accent), cyberpunk and unicorn gradients; `widgets.py` NavDelegate badge `#d93025`/`#ffffff`; `intro.py` mixes with `#000000`/`#ffffff`; `video_player.py` subtitles `#000`/`#fff`; `theme.app_icon` `#1f63c6`/`#e8710a` |
| Spacing | none (Qt's layout defaults; QSS paddings per control) | 18 distinct `setContentsMargins` sets (`(18,14,18,10)`, `(14,12,14,12)`, `(10,10,6,10)`, `(6,2,2,2)`, ...), spacings 0, 2, 3, 4, 6, 8, 10 |
| Type | base size per theme (9-11 pt), families per theme | sizes 8, 10, 11, 13, 14, 17, 18 pt in the stylesheet, `+3 pt` (Approvals URL, pages.py:345), 30 px (preset gallery, preset_gallery.py:113), 13 pt toasts, 15 px subtitles, `-1 pt` small labels (ui.py:310, widgets.py:2592) |
| Radius | one per theme, `small = radius - 2` | indicators `min(3, r)`, radio 8 px, scroll bars `min(5, r)` |
| Control heights | none | buttons 32 px (6 px padding + 18 min), fields and combos 26 px (4 px padding): a row of both never lines up at the edges |

### Naming

| Issue | Where | Recommendation |
|---|---|---|
| The host chooser is "This machine" (Start, MCP, Board) and "local (this machine)" (Kits, LLM, Images, Credentials, Teams, Activity), with and without a "Host" label | ui.py, host_choice.py, pages | one component, one label: "Host: this machine" |
| Status lines are `self.status`, `self.note`, `self.summary`, `server_status`, ... each set with `setText` | every page | `widgets.set_notice(label, text, level)` (new) everywhere a failure is said |
| A flag option's row label and its check box say the same words ("Mount all workspaces read-only" twice) | ui.py:419-424 (deliberate, see its comment) | the check box text "on" or the row label alone (needs the accessibility test to read the form label) |

### Component completeness (states)

| Component | Default | Hover | Focus | Disabled | Error | Loading / empty | Score |
|---|---|---|---|---|---|---|---|
| Push button | yes | yes | yes | yes | (danger role) | - | 9/10 |
| Primary button | yes | yes | **was none, fixed** | yes | - | - | 9/10 |
| Tool button | yes | yes | **was none, fixed** | Qt's | - | - | 8/10 |
| (?) help button | yes | lit | lit | - | - | - | 9/10 |
| Field / combo / spin | yes | - | yes | Qt's | `invalid` property on QLineEdit only | - | 7/10 |
| Check box / radio | yes | yes | **faint dotted frame, fixed** | yes | - | - | 8/10 |
| Tab bar | yes | yes | **was none, fixed** | Qt's | - | - | 8/10 |
| Navigation | yes | yes | **was none (outline: none), fixed** | section rows | count in the status colour | - | 8/10 |
| Table / tree | yes | - | Qt's | - | rows in a status colour | **no empty text inside a table** | 6/10 |
| Status line | yes | - | - | - | **plain on most pages** (fixed: Images, LLM) | "Asking…", "Reading…" | 5/10 |

The costumed looks (hacker, amber, Fallout, cyberpunk, unicorn, Windows 3.11) draw their own focus marks; the test
covers the plain ones (light, dark, Mac, the nine editor themes). Open there: the navigation of hacker and amber (the
selected row is the accent, the ring is close to it), Fallout's buttons (a double border in two greens 1.9:1 apart),
amber's and unicorn's tabs (below the ring's measure).

### Priority actions

1. Focus visible everywhere (done for the plain looks; the costumed ones listed above).
2. One status-line component with ok/warn/error (started: `widgets.set_notice`), then every page's failure through it.
3. A control-height token (30 px for buttons, fields and combos) and a named type scale (caption 9, body 10, section
   11, page title 17, display 22): proposal, it moves every page by a few pixels.

## Fixed in this pass (test-first: each test failed first, on the cause found)

| Finding | Cause | Fix | Test |
|---|---|---|---|
| Keyboard focus invisible on tool buttons, primary buttons, tabs, the navigation; faint on check boxes and radios | no `:focus` rule for them; the primary's focus border was its own accent border; `outline: none` on the navigation | `theme.focus_rings`: a 2 px ring of 3:1 or more (accent; accent text on a primary; the selected text colour in the navigation), sizes unchanged, the style's dotted frame not drawn beside it; Mac's focus colour is its accent | test_theme.py `test_the_keyboard_focus_is_visible_on_every_control` (12 looks x 7 kinds) |
| "tat", "yte", "uestion": the last column title cut on both sides beside a table's (?) | the room was kept twice: the header's viewport ends before the (?), and a `padding-right` on `::section:last` that Qt does not count in a title's width | the padding is gone (help_view.Corner) | test_help_hover.py `test_the_last_column_title_is_whole_beside_the_question_mark` |
| Approvals "39…go", "no ti…limit" | the columns were fitted while Asked and Refused in were still empty | fitted again when those cells grow (pages.py) | test_ui.py `test_approvals_asked_and_refused_in_are_shown_whole` |
| Images "gent versio" (a listing that failed) | a table never filled was never fitted: Qt's 100 px per column | shown before its first fill, a table is fitted to its titles (tables._Watcher) | test_tables.py `test_a_table_never_filled_keeps_its_titles_whole_when_shown` |
| Labels 5 px above the middle of their fields (Start, Board, every wrapping row) | `FlowLayout` put every item at the row's top | each item centred in its row's height | test_layout.py `test_a_row_of_labels_fields_and_buttons_shares_one_middle` |
| Board: "Project folder: /" over "tmp/sai-demo-y4u…" - the folder's name never shown | the title wrapped anywhere and was cut at its end | a path title too long keeps its end: "Project folder:" over the path cut in the middle | test_board_mouse.py (the long-title test, its expectation changed to this) |
| "Cannot list the images", "gpu-box: not reachable" in the ordinary grey | no error/warning state on status lines | `widgets.set_notice`; Images in the error colour, the LLM page's line in the warning colour while a server is down | test_board_services.py, test_llm_status.py |
| "App 0.1.93 · sandbox-ai 0.1.94"; the app's tests/test_version.py failed | scripts/bump_version.py raised only sandbox-ai's version | it raises the app's too; the app is 0.1.94 | tests/test_bump_version.py |
| With the focus visible, the window opened with a ring on the small « that folds the navigation | the first widget in the tab order took the focus | the keyboard starts in the navigation, on the page shown | test_ui.py `test_the_keyboard_starts_in_the_navigation` |
| The window had no icon; the tray showed Qt's question-mark icon whether or not a question waited | theme.app_icon (shield, orange dot) was never used | the window's and the tray's icon, the dot while questions wait | test_ui.py `test_the_window_and_the_tray_show_the_app_s_icon_with_a_dot_while_questions_wait` |

## Code review (non-file-manager parts)

Method: superpowers' reviewer checklist (correctness, error handling, architecture, tests) over the app's modules,
systematic debugging for each defect (reproduced, root cause, failing test, fix), security-audit's trust-boundary view
(what text from outside the app - an agent, a host, a project's `.sandbox-ai.json`, a preset - reaches a place that
interprets it).

Fixed:

- **activity_page.ActivityPage.stop**: a killed command was deleted by a Python lambda on its `finished` signal; when
  the page (its parent) went first - the app quitting right after stop() - Qt emitted `finished` from the QProcess's
  destructor and the lambda ran on the deleted object (`RuntimeError: Internal C++ object (QProcess) already deleted`,
  seen at the end of every capture run). Connected to Qt's own `deleteLater` slot now, which goes with the object.
  Test: test_activity_page.py `test_a_page_closed_while_its_killed_command_ends_says_nothing_wrong`.
- **prerequisites.ask_vpn_kind** (trust boundary): the name of a missing VPN profile comes from the board, a preset or
  a project's configuration and was shown in a QMessageBox in Qt's AutoText - a name like `<b>home</b>` was drawn as
  markup. Plain text now. Test: test_prepare_progress.py `test_the_vpn_question_shows_a_profile_name_as_it_is`.
- **Dead code** (a name found nowhere else in the app, its tests and docs/video): `SandboxAI.doctor_argv`,
  `BoardPage.new_session_name`, `SandboxesPage.current_run`, `widgets.row_texts`, `widgets.standard_icon`,
  `ThreadCall.abandon` (`abandon_all` sets the flag itself) removed; `theme.app_icon` is used now (above).

Checked and found sound: no subprocess or sleep on the GUI thread (the probe above; `SandboxAI.call` runs only in
`widgets.background`/`Task` threads, QProcess for the timeline); each of the 143 broad `except Exception` carries
its reason beside it; texts from agents and hosts go through `plain_label`
(PlainText), `shown_as_text` (escaped for QInputDialog and static message boxes) or `printable` (control characters).

Open (not changed here):

- `kits_page.remote_path` (kits_page.py:1530) puts a host label into a QInputDialog without `shown_as_text`, as every
  other such dialog does; host names are validated (`[A-Za-z0-9.-]`), so it is consistency, not a hole.
- credentials_page.py:1015-1077 uses `QMessageBox.information/warning` directly where the other pages use
  `widgets.inform`/`warn` (constant texts: no trust issue; `nonblocking_dialogs` covers both).
- 18 `self.status.setText(tr("Could not …"))` calls (and more with other words) say failures in the ordinary colour; they should go
  through `widgets.set_notice(..., "error")` page by page.

## Proposals (bigger changes, not made without a plan)

1. **Navigation and Help section labels in sentence case** (hallmark floor 9): "Project", "Running", ... in the
   navigation's secondary colour, 9 pt semibold, instead of `tr(section).upper()` (ui.py:6910, help_view.py:744, 754,
   763). Tests pin the capitals (test_projects.py:122, test_ui.py:122, 1768); a one-commit change with them.
2. **No middle-dot meta strings** (floor 10): the status bar's version line, the project bar's "1 running here ·
   open questions: 2 · path", the gallery's "easy · score 100": separate labels, or "; " where it is a sentence.
3. **A control-height token and a type scale** (design-system): buttons, fields and combos 30 px; sizes from a named
   scale (ratio ~1.2) instead of the nine sizes above. Every page moves by a few pixels: check with test_layout's
   clipping tests in both languages.
4. **Flatten frames inside tab panes** (hallmark "a card inside a card"): a group box inside a QTabWidget pane drawn
   without its border (Kits, Images, Credentials), the title as a section label.
5. **Empty states inside tables**: "No team on this machine yet - Start a team…" drawn in the empty table (a
   delegate or an overlay label shown while `rowCount() == 0`), instead of a line below the buttons (Teams, Images,
   MCP, Hosts, the doctor tables).
6. **Identifiers before paths in squeezed tables**: in tables.py a column that names a row (run name, host) keeps
   its content; the stretch column (a path) gives way first and is cut in the middle (Sandboxes in German: "clau…2b1c",
   "die…ner").
7. **Status colour on the status cell only** in the Credentials overview (now every cell of a row is orange or
   green): the colour marks state, the text stays the text colour.
8. **Fewer (?) marks**: one per group instead of one per row in the board's parts list; the terms explain themselves
   on hover already.
9. **One host chooser** with one label everywhere ("Host: this machine").
10. **The board inspector** wraps its form rows (QFormLayout.WrapLongRows) below ~420 px instead of cutting the
    fields at the dock's edge.
11. **Help's section list** wraps long titles instead of scrolling sideways.

## Tests

The full app suite (`projects/sandbox-ai-app/tests`, offscreen, the default temporary folder): 2628 passed, 12
skipped, 1 failed - the strip of icons' row was 2 px wider than the folded navigation with the focus ring's edge;
fixed (the folded row gives the edge from its padding) and that test, test_ui, test_theme, test_navigation,
test_projects, test_layout, test_inspector_focus and test_no_flash run again: all passed.
