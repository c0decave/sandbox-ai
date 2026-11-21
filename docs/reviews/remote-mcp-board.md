# Remote MCP servers on the Board: why a start on another host ended in "Unknown MCP server" (2026-11-19)

Branch: `fix/remote-mcp-folder`, off `main` at 1f543b29 (0.1.133). Not merged, not pushed.

The user, starting a board on another host, again and again: "Der Launcher lehnt ab: agent-sandbox: Unknown MCP
server: playwright, radare2; defined: binwalk, demo" - and nothing else; earlier in the same session the start's
preparation *had* added binwalk on that host. Question: "is it possible that the MCP servers available in the Board
are not refreshed for a remote host?"

Method: each lead reproduced end to end against a fake host "lab" - the Board (`BoardPage`, offscreen), lab's
folders served by the fake SFTP server (`tests/fake_sftp_server.py`), lab's `sandbox-ai` answered by a stand-in for
`sai.host_run` (`mcp list --json`, `mcp catalog --json`, `mcp add` as the real one takes and refuses them) and the
launcher's check answered as lab's launcher would (`Unknown MCP server: ...` while a server is not defined there).
Every test was run before the fix (failing) and after it. Tests: `projects/sandbox-ai-app/tests/test_remote_mcp_folder.py`.

## The cause the user hit: Start never added what the launcher's check had refused

`BoardPage.start()` collects `errors()` - the board's own problems *and* the launcher's findings of its last check.
"Unknown MCP server: playwright, radare2" is such a finding (`board.launcher_problems`). With it, `start()` checked
once more (the finding could be stale), was refused again, and said "Not started" with the launcher's words. The
preparation that adds missing MCP servers (`prerequisites.check_prerequisites`) comes *after* that point in
`start()`, so it never ran for such a board. It ran only when a preset was loaded (`new_from_preset` ->
`check_prerequisites(why="preset")`) - which is why binwalk (in the preset) was added and playwright and radare2
(added to the board by hand afterwards, or a second preset's) never were. The same on this machine: a probe with the
live harness (real sandbox-ai, `--mcp fetch` not added) showed `check_prerequisites` not called and
`Not started: Unknown MCP server: fetch ...` - the host was not the cause, the order in `start()` was.

Fix: when every error is the launcher's "Unknown MCP server" for servers the board uses (`unknown_servers_only`),
they are held, the remaining checks run, then `check_prerequisites` reads the host and adds what is missing (asked
first, folders and network as for a preset), and the start goes on after one more check of the launcher. Tests:
`test_a_start_adds_the_mcp_servers_its_host_lacks_and_goes_on` (binwalk needing a folder, playwright needing the
internet, radare2 in two catalogs), `test_a_start_whose_servers_were_added_elsewhere_meanwhile_is_checked_again_and_runs`.

## Lead 1 - the check remembered "everything is there" for good: confirmed, fixed

`check_prerequisites` returned at once when `prerequisites_done == (host, needs)`. That key was set after a complete
read with nothing missing and cleared only after the preparation made something. So once a start had found its
servers on lab, no later Start read lab again for the same needs in that window's life: a server removed on lab
since (the MCP page, a terminal, a host made anew, another machine under the same host name) was taken as there, and
the launcher refused the start. The key exists to stop a loop (the start that goes on checks again from inside the
check); it is now set only while that start goes on and cleared right after, so every Start reads its host again
(one `mcp list --json`). Test: `test_every_start_reads_its_host_again` (failed before: the second Start was refused).

## Lead 2 - a failed read fell through silently: confirmed, fixed

When `host_mcp_servers` failed (`mcp list --json` over SSH did not answer in 60 s, an older host), `read()` set the
option to None, the log said "the launcher checks it at the start" at INFO, and the start went on: the user saw only
the launcher's refusal. Worse, the start that went on had no "done" key for an incomplete read, so its own check
read again, went on, read again - without end while the host did not answer (a slow loop the loop guard of 8 checks
in 5 s never caught). Now: the failure is said on the page ("lab did not say what it has of MCP server playwright
(...): the launcher checks it at the start"); the start that goes on is not read again; and when the launcher then
refuses, the Board says why in words - "lab did not say which MCP servers it has (...), so playwright could not be
added there" - with Start again and Open the MCP servers page beside it. Test:
`test_a_host_that_does_not_say_its_servers_is_not_asked_without_end` (reads <= 2, checks <= 3, the words and buttons).

## Lead 3 - the Board's lists of a host's servers and catalog: confirmed, fixed

- `BoardPage.mcp_names()` (the MCP part's choices, a new part's default name) read another host's `mcp list --json`
  once per host and kept it until that host was chosen again (or 60 s after a *failed* read). A server added there -
  by a start's preparation, the MCP page, a terminal - was not offered while the app ran. Now it is read again when
  older than `MCP_KEEP` (30 s; the list read before offered meanwhile) and at once after a start's preparation made
  something on that host (`prerequisites_made`).
- `load_mcp_catalog()` (the chooser's "catalog" entries, an MCP part's variables) read *this machine's* catalogs
  (`sai.mcp_catalog`) once, whatever host was chosen. Now it reads the chosen host's (`mcp catalog --json` there) and
  again when another host is chosen. Its entries now say "added at the start, asked first" (what Start does now)
  instead of "add it on the MCP page first".
- `name_fields` (KEEP_SECONDS 30) keeps secrets, VPN profiles, images, variants and LLM servers - no MCP servers; the
  MCP page's own list is read per host on every refresh. Neither held MCP state for the start.
- None of these lists decided whether a start may go on - the start reads the host itself (`check_prerequisites`).
  So a stale list misled the human (offered or did not offer a server), it did not by itself cause the refusal.
Test: `test_the_boards_list_of_a_hosts_servers_and_catalog_is_that_hosts_and_read_again` (this machine's catalog
must not be read for a board on lab; lab's list read again after the start and after MCP_KEEP).

## Lead 4 - names: catalog-prefixed names and the project's own servers

- `board_needs()` takes the `--mcp` values of `launcher_argv`, which are the MCP parts' names (`radare2`); a
  preset's catalog entry (`mcp:radare2`) is the part's `entry`, used only for `mcp add`. Plain names match what
  `mcp list --json` and the launcher use. But a part *named* `hub:radare2` (typed, or a preset's raw option) went to
  the launcher as `--mcp hub:radare2` (refused as unknown) and, now that Start adds servers, would have been added as
  `radare2`, still missed as `hub:radare2`, and added again. The board now says it as an error of that part ("--mcp
  takes the name of a server ("radare2"); its catalog (hub) is chosen when it is added") and the preparation leaves
  such a value out. Test: `test_a_part_named_with_its_catalog_is_said_on_the_board_not_added_without_end`.
- A name in several catalogs: radare2 and binwalk are in both `mcp` (sandbox-ai-mcp) and `hub` (axross-mcp-hub).
  `mcp add radare2` refuses that as ambiguous; the preparation passed the bare name for a part without an `entry`.
  Now it asks which catalog (Cancel: not added, said). A name no catalog of the host has is not tried: the start
  stops before anything runs, says so and offers the MCP servers page
  (`test_a_server_no_catalog_has_stops_the_start_before_it_runs_and_says_why`).
- `prerequisite_own_servers` (the project file's servers, defined by `.sandbox-ai.json`) hides only parts marked as
  the project's; on another host such a board is refused before (`start()` says Apply is this machine's). Nothing
  else hides a server. The Start page reads its project's own servers only for this machine.

## Other start paths

The Start page (`LaunchPage.start`) runs `check_prerequisites` before it launches, with no launcher pre-check in the
way: it was not affected by the main cause, only by leads 1 and 2 (fixed in the shared code). A team start goes the
Board's way (fixed with it). Resume, restart and attach start what a session was started with (its servers were
there then); a schedule runs on the host without the app and cannot ask - left as they are: the launcher's refusal
there is what the human reads in that run's log.

## Not done, and why

- No `mcp add --create` for a read-only folder in the CLI: the app makes a missing folder of a host over SFTP after
  asking (the MCP page's Add…); the CLI's message now names the machine. A CLI option would need the docs tests and
  completion for one more option; left for a decision.
- The MCP page's list does not tell the Board when it adds or removes a server (the Board reads again after 30 s or
  after its own start's preparation): `McpPage.run_add`/`added` are changed on another branch
  (`fix/mcp-parallel-add`) and were left alone here to merge cleanly.
