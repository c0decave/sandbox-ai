# Board review 2: user journeys and robustness (2026-11-18)

Scope: the Board of the app (`projects/sandbox-ai-app/sandbox_ai_app/board_view.py`, `board.py`, `board_icons.py`)
and what it opens and works with - the guide, the wizard, the gallery and presets, the model picker, the remote
chooser, Export and Import, the inspector with "More options", the panels (`dock_fold.py`, `ui.py`), the project
tabs, the hosts. Focus (the second of two independent reviews; the first: interplay and correctness): every way a
human builds and starts a board - wizard, gallery, drag and drop, clone, import, another host, a team or bridge, a
local LLM -, slow and unreachable hosts, many parts, floating and docked panels, themes, 1280x800 and 800 px, the
keyboard only, German, a crash mid-operation with the drafts restored, the GUI thread, timers, leaks after dialogs.
Then the full app suite.

Branch: `review/board-2` (worktree `.claude/worktrees/agent-a656c72df7e0f00a3`), off main 76f2a174 (0.1.125); main
merged again twice before the final suite (5857a7d8, 0.1.127: the quieter app and security review A; 676e214c,
0.1.128: skills and plugins for running sandboxes). Not merged into main, not pushed. Written without reading
`board-review-1.md`; the last section marks what the older reports knew.

Severity: **H** high (work lost, a feature unusable), **M** medium, **L** low.

## How it was reviewed

- Read: `board_view.py` (the scene and view, the page's model, drafts, hosts, Apply, Start, Change folder, Clone,
  the inspector's rebuild and focus), `board.py` (the model, `checked`, import), `name_fields.py`,
  `remote_chooser.py`, `bundle_dialogs.py`, `guide_view.py`, the window's side in `ui.py` (docks, tabs, state,
  shutdown), `backend.py` (`host_run`, `host_dry_run`).
- Driven offscreen through the real entry points (the Board page and the app's `MainWindow`):
  - a clipboard filled by "another program" (15 shapes of parts), pasted;
  - 58 parts on one board (40 from the palette, every kind): places, the time of a step (add 30-50 ms, Arrange
    50 ms, 30 steps of Undo 0.6 s, the properties of 20 parts 0.8 s: no finding), copy and paste of all of them;
  - a host that does not answer (ssh's exit status 255 after a delay) and one that answers slowly, with every
    command on the host recorded with its thread: every part added and inspected, Dry run, Start, Apply, Clone,
    Change folder, Open project folder, the gallery, the wizard, Export, Import - **none ran on the GUI thread**
    (the longest step on the GUI thread 0.1 s);
  - typing into a property while a late answer makes the inspector anew;
  - a crash (the process gone, the state as its timer wrote it) and a normal restart, with a project's board, the
    board of no project folder and the board of another host's folder;
  - every theme (20) with every part kind, in German at 1280x800, the inspector docked and floating, each part's
    properties shown and the window painted: no exception, no finding; English and German at 1280x800 and at the
    window's least width (R2-19);
  - German: every text of the Board, its parts list, the properties of every part kind and the findings looked
    at for English left in it: none of the app's own (the launcher's own words - its refusals, option help - are
    English by design);
  - each dialog of the Board opened and closed seven times (guide, gallery, wizard, Export, Import, Save as
    preset, the part's and the canvas's menu): what stays alive afterwards (see "No finding");
  - **a random walk**: a seeded sequence of 220-350 of the steps a human takes (see
    `tests/test_board_review2_journeys.py`), each dialog that opens used at random and accepted or cancelled, a host
    that does not answer chosen and left, German on every second seed; after every step no slot may have raised,
    the scene must show exactly the model, the part inspected must exist, every kept board must have its Undo, and
    nothing may have run on the host from the GUI thread. 18 seeds, 14 of them again on the fixed code (12 with 350
    steps); it found R2-10 and R2-11.

## Findings

| # | Sev | Where (main 76f2a174) | What | Status |
|---|-----|-----------------------|------|--------|
| R2-1 | M | board_view.py:2457 `paste`, :2429 `clipboard_parts`, :2970 `inspect` | Parts another program put on the clipboard raised while they were added, some on the board already with no step of Undo; a part whose settings the inspector cannot show raised at every look | fixed 2672310b |
| R2-2 | L | board_view.py:2045 `add_node` | Parts added from the parts list (double click, Enter) lay on each other from the eighth part on | fixed d86f30e2 |
| R2-3 | M | board_view.py:2889 `focus_memo`, :2918 `restore_focus`, :2970 `inspect` | What was being typed in a property was dropped when a late answer made the inspector anew | fixed 910aba9e |
| R2-4 | L | board_view.py:6117 `host_options_read` | The note of a host whose options cannot be read grew by a sentence at every failed read | fixed 2cdbfd18 |
| R2-5 | M | backend.py:4371 `host_dry_run`; board_view.py:2624 `checked`, :6305 `dry_run_done`; ui.py:1834 | A host that does not answer was shown as "The launcher refuses: ssh: connect to host …" (verdict, Dry run of the Board and of the Start page, Start) | fixed 2cdbfd18 |
| R2-6 | H | board_view.py:5518 `keep_boards` | The board of no project folder (a new installation's first, a preset's, another host's folder's) was never kept: closing the app, a restart or a crash lost it | fixed 35b68389 |
| R2-7 | L | board_view.py:5661 `change_folder` (`looked`), :5711 `change_folder_on_host` | Change folder's background look at the new folder moved the board of whatever tab was shown by then | fixed 2b6b8ac8 |
| R2-8 | M | board_view.py:842 `BoardView.keyPressEvent` | The keyboard alone could not choose a part on the canvas: no way to an existing part's properties, menu, place or connections | fixed 4d356a8d |
| R2-9 | L | name_fields.py:116 `names_of` | Names that could not be read (no Podman, a host that does not answer) were asked for again at every rebuild of the inspector: a process or an SSH attempt per change | fixed 2cdbfd18 |
| R2-10 | H | board_view.py:5821 `move_to_folder`, :5480 `load`, :1840 `activate` | After Change folder two kept boards shared one Undo stack; loading one deleted it under the other: an exception at every later switch to that tab, the wrong board's history for a project | fixed 2b6b8ac8 |
| R2-11 | L | board_view.py:1947 `changeEvent`, :2304 `showEvent`, :405, :907 | A restyle, first fit, drop or remove queued (`QTimer.singleShot(0, …)`) for a Board that went ran on the deleted page | fixed 2b6b8ac8 |
| R2-12 | M | board_view.py:5849 `new_from_preset`, :5971 `show_imported` | A board made from a preset or an import counted as unchanged: gone with the next start of the app (a locked preset's board replaced by the project's default), no question when its tab closed | fixed 35b68389 |
| R2-13 | M | board_view.py:5473 `may_discard_unnamed`, :2306 `canvas_menu` | The board of no folder, kept while a project's tab is shown, could never be shown again: "Keep them" kept what nothing led back to | fixed 35b68389 |
| R2-14 | M | guide_view.py:566 `apply` | The agent's guide on a board with a model server kept the model answered and dropped every other answer of the same guide | fixed 6d8e49f5 |
| R2-15 | L | board_view.py:4522 `load_llm_servers`, :5212 `mcp_names` | The model servers and MCP servers of a host that did not answer read as "none", with no word, until another host was chosen | fixed 2cdbfd18 |
| R2-16 | M | board.py:2595 `imported_board`; board_view.py:5971 `show_imported` | An imported board's Bridge and Team parts and their places were taken from the bundle's state as they came: a bundle made elsewhere raised while the board was shown | fixed 2672310b |
| R2-17 | L | board_view.py:5661 `change_folder` | Change folder onto a folder whose own board had changes not applied replaced them without a question (Clone asks) | fixed 2b6b8ac8 |
| R2-22 | L | preset_gallery.py:208 `PresetCard.sizeHint` | "New from preset…" (and the Start page's Gallery…) held the window while the gallery was made: its flow layouts measured each of the 106 cards seven times (2226 measurements, a third of the 0.23 s; 0.5 to 1.2 s on a loaded machine) | fixed 02f54f1b |
| R2-18 | L | board_view.py:5518 `keep_boards`, :120 `history_of` | A draft is kept without its steps of Undo once a Change folder is among its last 20 steps (`history_of` gives up at a step that is no `Snapshot`): after a restart Undo goes back to the project's file in one step | fixed 45570194: Change folder steps (this machine's and another host's) are kept with both folders and their trust; Undo onto a folder whose file is not trusted or changed meanwhile leaves the board untrusted |
| R2-19 | L | ui.py:9983 `make_docks` (the panels' least widths), board_view.py `BoardPage` | The window's least width on the Board is 835 px with the properties folded and 1049 px with them docked and open (navigation 224 + parts 150 + the Board's own controls 387 + properties 240 + separators): at 1280x800 it fits; on a 1024 px wide screen 25 px of the window are off it while a part's properties are docked, on 800 px the window never fits | fixed on fix/board-width (user 2026-11-19: "wrap when narrow"): the Start controls and the header's buttons wrap onto a second line when narrow; every theme in both languages fits 1024x768 with the properties docked open and 800 px folded, nothing clipped (test_layout `test_the_board_fits_1024_with_its_properties_docked_and_800_folded`) |
| R2-20 | L | board_view.py:6305 `dry_run_done` | Dry run's answer is shown also when the board or the host changed while it ran (titled only "Plan (dry run)") | fixed 73dabb34: an answer for a board, form or host changed meanwhile is titled "out of date" and names its host and time (Board and Start page) |
| R2-21 | L | board_view.py:4065 `bridge_entries`, bridges_view.py:420 `BridgeWaiter` | A Board closed (the app quit) while a start's Bridge parts still wait for the other session makes no bridge: nothing says so at the next start of the app | fixed eb5af16f: the wait is kept in the state file; the next start waits on until the same time and says so, or says which bridges were not made |
| R2-23 | L | preset_gallery.py:392 `PresetGallery.populate` | The gallery is still made in one go on the GUI thread, a card per preset (0.15 s for 106 presets here; more on a slow or loaded machine, and with every preset added) | fixed d013664d: the first 12 cards are made with the gallery, the rest 8 per turn of the event loop; search, counts and keys see every preset meanwhile |

## The findings in detail

### R2-1 (M) - a paste of what another program put on the clipboard

`clipboard_parts` reads the clipboard as untrusted data (known kinds, settings as an object, bounded numbers) but
kept an `id` and `edges` of any JSON type, and `paste` handed the settings to `Board.add(kind, connect=False,
**props)`. A part id that is a list raised `TypeError: unhashable type` in the middle of the loop - the part before
it already on the board, with no scene item and no step of Undo (`change` catches `ValueError` only); a setting
named `connect` or `kind` raised "got multiple values"; a server name that is a list was pasted and raised in the
inspector at every look (`QComboBox.setEditText(list)`), with the rows of the part shown before left standing.
Fix: `Board.part_problem(kind, props, sai, policy)` - the shapes this app gives a part's settings (a launcher
option's flag, list or value; a part's own name, path or choice a text) and what a board computes of it (title,
command line, problems, badges), all inside a try; `paste` leaves out what it refuses and says how many (the log
names them), adds the settings as data (never as keyword arguments) and maps only ids that are numbers or texts.
`inspect` says "These properties cannot be shown …" in place of an exception and still offers Remove.
Tests: `test_a_paste_of_what_another_program_put_on_the_clipboard_never_raises` (9 shapes),
`test_a_part_that_cannot_be_read_is_left_out_and_said_the_others_are_pasted`,
`test_a_part_of_this_app_is_still_pasted_with_its_settings_and_connections`,
`test_properties_the_app_cannot_show_say_so_and_still_offer_remove`.

### R2-2 (L) - many parts from the parts list

`add_node` placed a part without a drop point at `40 + 110 * len(nodes) % 700` in its kind's column: the count is
the whole board's, and it wraps. 40 parts added with Enter (the keyboard's only way to add): 66 pairs covering each
other, a part hidden under another until Arrange. Fix: `free_place` - the first place in the column that covers no
part (also for a kit's part and a model server's). Tests: `test_many_parts_added_from_the_palette_never_cover_each_other`
(36 parts and every kind), `test_the_first_parts_still_go_into_their_kinds_column`.

### R2-3 (M) - typing while a late answer arrives

The inspector is made anew by `inspect_timer` after every change of the model and after every late answer: the
host's model servers (two steps, up to 150 s), your images, kits, session logins, the hosts' sessions, MCP servers and
catalog, secrets, a team's verdict. `focus_memo`/`restore_focus` brought the keyboard and the cursor back but put the
typed text back only when it differed from the committed form by whitespace: text typed into a line (taken when the
field is left) was dropped, the cursor left in a field showing the old value. On another host those answers come
seconds after the part was opened - while the human types. Fix: the memo carries whether the text was typed and not
taken (`typed_in`: edited, and not what the field showed when it was made; also a name field's); it goes into the
new field, still to be taken when the field is left; a list typed within its 600 ms is taken before the rows go. A
text taken with Enter, or typed and deleted again, is not "typed": the field follows the model, so an Undo of that
very value shows in the field the keyboard is still in. Tests:
`test_what_is_typed_in_a_field_survives_the_inspector_made_anew` (a value, a path, a VPN profile's and a variant's
name field), `test_a_field_not_typed_in_follows_the_model_when_the_inspector_is_made_anew`,
`test_a_text_taken_with_enter_or_typed_and_deleted_follows_undo`,
`test_a_list_typed_a_moment_ago_is_taken_when_the_inspector_is_made_anew`.

### R2-4, R2-5, R2-9, R2-15 - a host that does not answer

- R2-5: `host_dry_run` returned ssh's failure (exit status 255, or nothing within 90 s) as `(None, message)`, the
  shape of a refusal: the verdict read "The launcher refuses: ssh: connect to host lab port 22: Connection timed
  out", Dry run was titled "The launcher refused these options" (the Start page's too). Fix:
  `backend.HostUnreachable`; the Board says "{host} does not answer: …" in the verdict, in Dry run and - after one
  more try, as for a refusal - before a start instead of opening a terminal that only shows ssh failing.
- R2-4: `host_options_read` appended "Its options could not be read: …" to the note's text at every failed read
  (again after `HOST_RETRY`, at every check): four sentences after three edits. Fix: set from the note's base text.
- R2-15: a failed read left `llm_servers = []` and `mcp_known[host] = []`: "none", with no word, and never read
  again until another host was chosen. Fix: said under Model server ("The model servers of {host} could not be
  read: …") and both read again after `HOST_RETRY` seconds when the part is looked at.
- R2-9: `name_fields.names_of` kept only reads that succeeded; every field made (each rebuild of the inspector: every
  change of a property) asked again at once - without Podman a `sandbox-ai images` process per change of the Engine
  part, on a host that does not answer a connection attempt per change. Fix: a failure is kept for
  `FAILED_SECONDS` (20 s; a list opened asks after 3 s as before, a change of the names at once).
Tests: `test_the_note_of_a_host_whose_options_cannot_be_read_does_not_grow`,
`test_a_host_that_does_not_answer_is_not_a_launcher_that_refuses`,
`test_the_launchers_own_refusal_on_a_host_is_still_a_refusal`, `test_host_dry_run_tells_no_answer_from_a_refusal`,
`test_names_that_could_not_be_read_are_not_asked_for_again_at_once`,
`test_a_hosts_servers_that_could_not_be_read_are_said_and_asked_for_again`, and
`test_a_slow_hosts_late_verdict_is_never_shown_for_another_host` (no finding: it held).

### R2-6, R2-12, R2-13 - the board of no project folder, and a preset's board

- R2-6: `keep_boards` skipped the key "" - the board shown while no project folder is chosen, and every board of
  another host's folder (`open_remote_project`, Clone and Import on a host: `load(board, None)`). Nothing asked when
  the app was closed. A new installation's first board, or an afternoon on a remote host's board, was gone after a
  restart, an update or a crash. Fix: kept as a project's (draft, Undo, zoom) with the host's folder it is for,
  the project file it came from when nobody trusted that one (Start asks first, as before the restart) and whether
  it was the board shown; restored into the board the page starts with, and shown again after the window's project
  tab when it was the one shown (only then: a project opened later stays shown). Discard works for it.
- R2-13: once a project's tab was chosen that board was kept (`sessions[""]`) with nothing leading back to it;
  opening another host's folder asked "Drop them? / Keep them", and "Keep them" kept what could not be reached.
  Fix: the canvas menu's "Show the board without a project folder" while one is kept with changes, and "Keep them"
  shows it.
- R2-12: `new_from_preset` and `show_imported` loaded the board with a clean Undo stack although no project file
  says it: `dirty()` false, so no draft was kept (after a restart the project showed its file's board, or the
  default - the "locked" preset's `--offline` gone without a word), closing the tab asked nothing, and the
  tutorial's "applied" step counted it. Fix: `resetClean()`, as Clone does.
Tests: `test_a_board_without_a_project_folder_comes_back_with_its_undo` (closed, crashed),
`test_a_board_of_another_hosts_folder_comes_back_shown_as_it_was_left`,
`test_a_project_opened_after_the_restart_is_shown_not_the_board_that_came_back`,
`test_a_board_kept_without_a_folder_can_be_shown_again`,
`test_the_board_without_a_folder_can_be_discarded_and_stays_so`,
`test_a_presets_board_is_a_change_not_applied_and_survives_a_restart`.

### R2-7, R2-10, R2-11, R2-17 - Change folder and the project tabs

- R2-10 (found by the random walk, seed 11): `stash` keeps the board shown under its folder's key and `activate`
  never removes that entry, so the tab of the board shown also has a kept copy sharing its Undo stack. Change
  folder moved the board shown to another key (`move_to_folder`) and left both entries: the old folder's (this very
  board, as it was when its tab was last left) and the new folder's own. Switching to the old folder's tab then
  showed the stale copy with the moved board's Undo; `load` for either key deleted that stack under the other, and
  from then on `activate` raised `Internal C++ object (QUndoStack) already deleted` at every switch to that tab.
  Fix: `move_to_folder` drops what is kept under both keys (`drop_kept`: a stack still used by the board shown or
  another kept board is never deleted), `activate` reads a project again when its kept Undo is gone, `keep_boards`
  skips such an entry.
- R2-17: the new folder's own board with changes not applied was replaced without a question: asked now, as Clone
  asks.
- R2-7: the look whether the new folder has a project file runs in the background (up to 30 s on a disk that
  hangs, 90 s on a host): its answer moved the board of whatever tab was shown by then. Fix: dropped unless the
  board that asked is still the one shown.
- R2-11: `QTimer.singleShot(0, self.restyle)` (a theme change), `…self.fit` (the first show), the drop and the [x]
  ran on a page deleted meanwhile. Fix: the page (or view) as their context object.
Tests: `test_change_folder_then_the_tabs_never_leaves_a_board_without_its_undo`,
`test_change_folder_onto_a_folder_with_unapplied_changes_asks_first`,
`test_a_kept_board_whose_undo_is_gone_is_read_again_instead_of_raising`,
`test_change_folders_late_answer_never_moves_another_projects_board`,
`test_a_restyle_queued_for_a_board_that_went_does_not_run`.

### R2-8 (M) - the keyboard only

The parts list adds a part with Enter, and the canvas had Delete, Esc, copy, cut, paste and the menu key - but no
key chose a part (`NodeItem` is not focusable, the arrow keys scrolled): the agent's, the proxy's and the network's
properties, a part's menu and its place were out of reach without a mouse, as was connecting two parts. Fix: the
arrow keys choose the next part that way (the agent first), Enter opens its properties with the keyboard in their
first field, OK and Cancel bring it back to the canvas, Alt and an arrow key move the chosen parts by a step of the
grid (one step of Undo), Ctrl+A chooses all, and a part's menu has Connect with and Disconnect from. With the
properties floated (a window of their own) Enter makes that window the active one and OK the app's again. Help (the
canvas's (?) and the Board's section, both languages) and the app's README. Tests:
`test_the_arrow_keys_choose_parts_and_enter_opens_their_properties` (the main window),
`test_enter_reaches_the_properties_when_they_float_and_ok_comes_back`,
`test_every_part_of_a_large_board_is_reached_with_the_arrow_keys`,
`test_alt_and_an_arrow_key_move_the_chosen_parts_as_one_step_of_undo`,
`test_a_parts_menu_connects_and_disconnects_it_without_the_mouse`.

### R2-14 (M) - the agent's guide with a model server

`guide_view.apply` took the part, called `choose_llm_server` when the model answered is another of the server's
(a step of Undo of its own: every step makes the board's parts anew, `Board.from_state`), then set the remaining
answers on the part it had taken before - an object of the board before that step. `change` saw no difference and
dropped them: the effort, Show thinking and "Agent without approval prompts" answered in the same guide. Fix: the
part is taken again inside the step. Test:
`test_the_agents_guide_keeps_every_answer_when_it_also_changes_the_servers_model`.

### R2-22 (L) - the gallery's cards measured seven times

Found by the full suite: `test_responsive_pages` (a click may hold the window 0.5 s) failed for "New from preset…"
and the Start page's "Gallery…" - 0.51 to 1.26 s - while other suites ran on the machine, with main's sources too.
Timed alone (the best of five): the gallery is made in 0.23 s for 106 presets - the catalogue 0.02 s, the boards
0.01 s, the scores 0.01 s, the cards 0.04 s, and the rest layout: `PresetCard.sizeHint` computes the height of
the wrapped description (`totalHeightForWidth`) and was asked 2226 times per gallery. Fix: a card measures itself
once, and anew when Qt asks it for a new layout or its font or style changes: 321 measurements, 0.15 s. Test:
`test_a_gallery_card_is_measured_once_until_something_in_it_changes`. Still made on the GUI thread, card by card
(R2-23, fixed later: see "The open findings, fixed").

### R2-16 (M) - an imported board's state

`sandbox-ai board import` hands the bundle's board state on with its markers filled in; `imported_board` took the
places of every part and the Bridge and Team parts from it: a place that is a text raised in `NodeItem.setPos`
while the board was shown (after `show_imported`'s own try), a setting named `connect` raised in `Board.add`.
Fix: parts of known kinds at finite places only, Bridge and Team parts through `Board.part_problem`, and the whole
board through `Board.checked` before it is shown. Tests:
`test_an_imported_boards_own_parts_come_with_their_places_and_connections`,
`test_an_imported_board_with_a_state_made_elsewhere_is_shown_without_what_cannot_be_used` (8 shapes).

### The open findings, fixed (2026-11-19, branch fix/board-review2-rest)

- R2-18: `history_of` keeps a Change folder as a step (`kind` "folder": both folders and, per folder, the path of
  a project file nobody trusted and the version the board was read from; "host-folder": both of that host's
  folders). `restore_history` puts them back while only the board changes (`history_restoring`), when the folders
  still exist and the steps lead from one folder to the next to the draft's; a history that starts on another
  folder has no "Restore" step and counts as changed at its first step. Undo onto a folder then takes its trust as
  kept (`take_trust`, once per step and folder): trusted only when nothing said otherwise and its file is the very
  version it was, else Start waits for Apply. Found on the way: Change folder of a board read from a project file
  nobody trusted counted as trusted on the new folder (Clone kept it untrusted) - it is not trusted now either.
- R2-20: both pages remember what a dry run was asked for (the board's state or the form's agent and options, the
  host, the time); an answer for something else is titled "out of date" and says which host and time it is from.
- R2-21: `BridgeWaiter.kept` (values and the wall-clock end of the wait) is written with the boards at once and as
  each bridge is done; the quit ends the waits after the state is written and logs them; the next start
  (`resume_bridge_waits`) checks each kept wait as a bridge's values are checked and waits on (said in a window
  that does not block) or says the ones whose time is over and where to make them.
- R2-23: `PresetGallery.populate` makes every section with its count at once and the first `FIRST_CARDS` cards (and
  the one to select); `make_next` makes `BATCH_CARDS` per turn of the event loop. `cards` and `visible_cards` make
  the rest at once for what needs them all (the arrow keys, a category's place); a search without a match among the
  cards made makes the rest. Not done: the catalogue itself (sandbox-ai's preset files read and checked) is still
  read on the GUI thread at each opening (0.02 s alone, 0.1-0.2 s on a loaded machine).
Tests: `tests/test_board_review2_rest.py`.

## No finding (looked at, measured)

- **The GUI thread**: with every command on a host recorded with its thread, no journey ran one on the GUI thread
  (the walk asserts it at its end; `tests/test_references.py` passes). The longest GUI step with 58 parts: 0.1 s.
- **Timers**: the Board has no periodic timer of its own - `check_timer`, `inspect_timer` and a list field's timer
  are single shots, the Bridge waiter ends at its deadline; a hidden Board asks nothing.
- **A slow host's late answers**: a verdict for another host or an older board is dropped (`checked`: token and
  host), a Start that waits for a check starts only the board and host it was pressed for.
- **Dialogs**: the guide, the gallery, the wizard, Export, Import, Save as preset, a part's and the canvas's menu
  and the properties of every part, each opened and closed six more times after a first: not one dialog, menu,
  widget or running timer more than before (`drop_after_exec`, `exec_dropped` and the inspector's `removeRow`
  hold).
- **Themes, German, widths**: no exception painting every part kind in each of the 20 themes, docked and floating;
  no English text of the app's own in the German Board; the toolbar wraps (test_board_toolbar_groups covers 360 to
  1400 px).
- **A crash with a project's board**: draft, Undo and zoom came back (the state is written 0.4 s after a change);
  what is being typed in a field at that moment is not taken (by design: `commit_edits` takes it when quitting).
- **Remote chooser, Export, Import dialogs** on a host that does not answer: listed and run in the background,
  cancelled when closed; nothing to fix.

## Tests

Each finding's test is in `projects/sandbox-ai-app/tests/test_board_review2.py` (55 tests), the walk and the slow
host in `tests/test_board_review2_journeys.py` (3 tests). Run against main's sources (the fixes taken out of the
working tree, the tests kept), 42 of the then 51 tests failed and 9 passed (the controls: what must hold before and
after); with the fixes all pass.

The full app suite, final run - `review/board-2` with main 676e214c (0.1.128) merged, 278 test files in 14 chunks
of 20 files, one process at a time beside other suites on the machine (load average 10 to 13):

| chunk | result | time |
|-------|--------|------|
| 0 | 258 passed | 122 s |
| 1 | 1537 passed | 426 s |
| 2 | 160 passed, 1 skipped | 390 s |
| 3 | 303 passed, 2 skipped | 88 s |
| 4 | 309 passed, 4 skipped | 83 s |
| 5 | 254 passed | 223 s |
| 6 | 288 passed | 309 s |
| 7 | 206 passed, 6 skipped | 127 s |
| 8 | 219 passed | 218 s |
| 9 | 682 passed | 312 s |
| 10 | 243 passed, 1 skipped | 227 s |
| 11 | 170 passed | 151 s |
| 12 | 423 passed, 2 skipped | 249 s |
| 13 | 326 passed, 4 skipped | 532 s |
| all | **5378 passed, 20 skipped, 0 failed** | 3457 s |

What the full runs before it failed with, and what became of it:

- `test_error_lines.py::test_no_message_goes_through_the_names_printable`: this review's own text of a host that
  does not answer went through `printable` - a message goes through `printable_text` (fixed, 9726cb95).
- `test_board_props_fold.py::test_the_agents_options_after_its_questions_option_are_folded_at_first` (1 of 1534 in
  its chunk, once): the test's `shown()` found a field in the very round of events in which a late answer made the
  inspector anew, and read it as hidden. It looks once nothing makes the inspector anew any more (9726cb95).
- `test_responsive.py::test_a_host_install_runs_in_the_background` (the event loop stood still for 0.26 s and
  0.28 s, the limit 0.2 s; in two runs, and passing in between): the gap was measured with the time other
  processes kept the test's own from running. Measured without it now, as `test_responsive_pages` does (02f54f1b).
- `test_responsive_pages.py::test_every_button_of_every_page_leaves_slow_work_to_the_background` ("New from
  preset…" and "Gallery…" 0.51 to 1.26 s, the limit 0.5 s; with main's sources too): R2-22, fixed (02f54f1b).
- `test_hotfix_round_2.py::test_a_presets_mcp_server_is_added_from_the_catalog_the_preset_names` (2) and
  `test_live_regressions.py::test_exiftool_needs_folder`, in one run: `sandbox-ai mcp add` answered "no catalog
  entry" after "The read operation timed out" - these tests read sandbox-ai's real catalogs from the network, which
  took 30 s for one listing at that time. Nothing of the review; they passed in the runs after it (not changed).

Core (the docs and the exported web help the review touched): `tests/test_release_notes.py`,
`test_docs_claims.py`, `test_docs_every_option.py`, `test_web_i18n.py`, `test_pypi_package.py`: 60 passed.
The walk beyond its two seeds of the suite: 12 seeds of 350 steps (`MONKEY_SEEDS=11,5,6,7,9,10,12,13,14,15,16,17
MONKEY_STEPS=350`): 12 passed in 388 s (on the fixes as of 6d8e49f5, before the follow-ups and the merges of
main; the two seeds of the suite ran on the final tree).

Main went on to 0.1.129 (security review B) after the final run began: in the app it changes the version number
only, and it merges into this branch without a conflict (`git merge-tree`); not merged here, so that the branch is
what the suite ran on.

## Known before

Read after the review was done: `board-review-1.md` (interplay and correctness) and the older reports in
`docs/reviews`. None of the findings above is in them: review 1's eight findings are about the options a board
generates (the project file's budget, values starting with `-`, overlapping workspaces, mount destinations, `--mode`
as a file's key, a team's effort per member) - no overlap with the journeys here; the older reports have nothing on
the Board's paste, drafts without a folder, Change folder with tabs, the inspector's rebuild or the canvas's keyboard.
