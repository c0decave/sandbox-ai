# Board review 1: interplay and correctness (2026-11-18)

Scope: the Board (`projects/sandbox-ai-app/sandbox_ai_app/board.py`, `board_view.py`) and what it writes and reads:
the launcher options of every part kind (`backend.Option`, `agent_sandbox.build_parser`), the project file
`.sandbox-ai.json` and its trust (`project_trust.py`, `agent_sandbox.load_project_config` / `merge_config`), presets
(`presets.py` `from_board` / `build`), the bundle (`bundle_document` / `imported_board`), teams (`team_options`,
`team_problems`) and starts on another host (`launcher_argv(local=False)`, `unknown_options`).

Branch: `review/board-1`, made off `main` at c74768f1 (0.1.115); `main` (0.1.116) merged in at fb9de5a4. Not merged,
not pushed. Review 2's report was not read.

Method: a generated corpus (`tests/test_board_review1_corpus.py`): 300 boards per property, every part kind with a
random subset of its options, values drawn from pools with edge values (a leading `-`, spaces, `=`, commas,
non-ASCII, padding, relative and `~` paths), workspaces (some disconnected, some twice), mounts, MCP servers with
limits and variables, services, LLM parameters, the proxy off, the project file's keys (mode, canaries, keep,
log_debug, name, budget) and options no part holds (`Board.extra`: `--mode`, `--session`, `--project-name` ...).
Properties, each against the launcher's own code:

1. the launcher's parser accepts the board's start (`launcher_argv` + `--` + agent arguments) and every option the
   board means (`values()`) arrives as it means it - flags, choices, list items, values, the agent's arguments;
2. the project file's own keys arrive as `merge_config` (and `modes.resolve`) would apply them from the file;
3. Board -> argv -> Board means the same options;
4. Board -> `project_document` -> `from_project` means the same, holds only keys and options the launcher takes in a
   project file, and the launcher's merge of it parses.

Then the code paths around them were read: Start (`run_start`, `start_from_project_file`, `file_start_argv`), Apply,
the dry run (`check_argv`), presets from and to boards, the bundle, teams, another host.

Severity: **M** medium (a start refused or a setting lost that the human set), **L** low (a check later or in other
words than the launcher's, the dry run catches it).

## Findings

| # | Sev | Where | Scenario | Status |
|---|-----|-------|----------|--------|
| 1 | M | `board.py:908` `document_argv`, `project_trust.py` `ORDER`/`unshown` | A project file with `"budget": 5` opened on the Board: a board start passes `--no-config`, and `document_argv` gave mode, canaries, keep, log_debug and name as options but not the budget: the start ran without its spending limit. Nothing named the key either (not above the board, not in Apply's or the host-start question's list of what the file sets). | fixed, 81c68a2b |
| 2 | M | `backend.py:6046` `Option.tokens`/`pair` | A value that starts with `-` (a Cairn goal "- fix the tests", a folder `-old`, `--copy -notes.txt:.notes`, an `--env`) was given as its own word: the launcher's parser read it as an option and refused the start ("argument --cairn-goal: expected one argument"). Now attached, `--OPTION=VALUE` (the Start page's form uses the same tokens). | fixed, 81c68a2b |
| 3 | L | `board.py:1289` `workspace_overlaps` | The same folder added twice (drag and drop, Add) or a folder and one inside it: only the launcher's dry run refused it ("Workspaces must not overlap"); the board showed no problem, and read back the folder was there once. Now a problem in the launcher's words; `~` compared as written (the board may be another host's). | fixed, 81c68a2b, 01cf1b22 |
| 4 | L | `board.py:1313` `mount_dest_problem`, `problems` | The board's mount check was `dest.startswith(("/work", "/mnt", "/opt"))`: `/mntx`, `/workshop`, `/opt/../etc`, `/mnt/x/` passed the board and were refused by the launcher (`safe_dest`). A mount part whose source was cleared was left out of the start without a word; commas were not said. Now the launcher's rule, each with its own text. | fixed, f311c404 |
| 5 | M | `board.py:987` `file_start_argv`, `:1061` `project_document`, `:1008` `extra_groups` | A project opened with `"mode": "high"` saved as a preset (`from_board` writes `--mode high` into its options), the preset used for another project: the option sits in `Board.extra`. Apply dropped the `--mode` token but kept `high`, a bare word in the file's options (the launcher refused the file at the next start), and the start from the project file (a board with the project's own MCP servers) passed `--mode` without its value ("expected one argument"); the board's mode was lost from the file. Now `Board.extra` is taken apart by the launcher's own option arity, and `--mode`/`--project-name` become the file's `mode`/`name`. | fixed, 93e8dfc0 |
| 6 | L | `board.py:1737` `team_problems` (`team_options` :1695) | A team board whose Agent part has an effort or Show thinking: team start has no option of a member's own for them, so they go to every member; a member whose agent takes no such level (OpenCode, Goose `xhigh`) or has no thinking switch refused its start. The board said nothing until team start's own check. Now an error naming those members, as the launcher's `check_model_effort`/`check_show_thinking` refuse them. | fixed, 01cf1b22 (the real fix, a per-member effort, is team start's: open there) |
| 7 | L | `board.py:437` `from_argv` | `--mount SRC:DEST` (one colon) or `--mount SRC` read into a mount part becomes `SRC:DEST::ro` / `SRC::ro` again: not exactly what was given. Both forms are refused by the launcher ("Mount syntax") and the board shows the part's error. | open: refused either way; keeping the raw text would need a part state of its own |
| 8 | L | `board.py:1475` `problems` | `--tmux` and `--no-tmux` together: an error on the board, while the launcher takes the last one silently. | open: the board is stricter than the launcher, never looser; harmless |

Checked and found sound (no finding): the corpus' properties 1, 3 and 4 hold for every generated board after the
fixes (a workspace twice is finding 3); undo and redo restore whole states (`Snapshot`, `FolderChange`,
`HostFolderChange`) and the node items follow part ids, not objects; Apply writes and trusts exactly the document
shown in its question; an untrusted project file is never started locally, and on another host only after the
question naming what no part shows (finding 1 adds the budget there); a project's own ruleset and MCP servers are
refused for another host before anything starts; `check_argv` strips the project's own MCP servers in both spellings;
`unknown_options` and every other reader of the board's tokens (`team_options`, `paths_from_options`, `run_start`'s
`--name`) take the attached `--OPTION=VALUE` form; the bundle carries the project's ruleset and servers in `project`
and `board import` makes them yours.

## Tests

- `projects/sandbox-ai-app/tests/test_board_review1_corpus.py`: the four properties over 300 generated boards each
  (1200 cases), against the launcher's parser, `merge_config` and `load_project_config`'s rules.
- `projects/sandbox-ai-app/tests/test_board_review1.py`: one regression test (or a parametrized set) per finding
  1-6, each written failing first; finding 5 through a preset made from a project file and the launcher's
  `load_project_config` of the file Apply writes.
