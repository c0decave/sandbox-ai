# Review 3x-1 of the new features of 0.1.99: security (2026-11-18)

Scope: everything since the 0.1.98 release commit (`git diff d9ef48f6..main`: `sandbox/`, the app, `sandbox/web/`,
`scripts/`), read as whoever the feature does not trust would use it: an agent that writes its workspace (and the
workspace's `.git`), a bundle someone passes on, a remote host, a model server, a web page. Depth on the bundle
import, the checkpoints, the schedules (what the host does unattended with a workspace the agent wrote), the spend
accounting, the phone notifications, the bridge's rejoin, the Files page's host folders, the web routes.

Branch: `review/3x-1` (worktree `review3x-1`; not merged, not pushed). It was made off `main` at 9567aaa1; reviews
3x-2 and 3x-3 were merged into `main` while this one ran, so the branch was fast-forwarded to 76143a40 before its
first commit and every finding below was checked (and fixed) against that state. Their reports were not read.

Method: the code was read; where a claim could be attacked it was, with probes outside the repository (deleted
afterwards). Every bug has a failing test first, then the fix, and the test stays.

Severity: **H** high (a boundary broken: code run on the host, a host file read, an image replaced), **M** medium (a
promise of the feature broken by the party it does not trust), **L** low.

## 1. Findings

| # | Sev | Where | What | Status |
|---|-----|-------|------|--------|
| 1 | H | `schedule.py` `push_branch` (was :862) | `--push`: the workspace's `.git/config.worktree` was read by the host's `git push` | fixed, 1ce87cbb |
| 2 | H | `schedule.py` `snapshot` (was :745) | The summary's snapshot hashed host files into the workspace's `.git/objects` through a folder made a link | fixed, 1ce87cbb |
| 3 | H | `board_bundle.py` `load_images` (was :1857) | `board import --images` loaded any archive under any reference - `localhost/claude-sandbox:latest` too | fixed, 1ce87cbb |
| 4 | M | `checkpoints.py` `compare` (was :1181) | A path the agent made a FIFO, a socket or an unreadable folder was held back from `checkpoint restore` | fixed, fe841ecb |
| 5 | M | `schedule.py` `board_document` (:619), `board_bundle.py` `show_main` | `--board NAME` and `board show NAME` read a file `NAME` of the current folder before the board kept here | fixed, 1ce87cbb |
| 6 | M | `runtime.py` proxy (was :9822) | A model request with `Accept-Encoding: br` (or zstd) was never counted: no budget was ever used up | fixed, 56d4526f |
| 7 | M | `board_bundle.py` `read_bundle_bytes` (:879) | A sparse archive member was read as its full size: a few KiB became GiB in `/var/tmp`, before anything was asked | fixed, 1ce87cbb |
| 8 | L | `schedule.py` `snapshot`, `commit_changes` | A `.git` that is a file or link to another repository of the host: objects and a branch written there | fixed, 1ce87cbb |
| 9 | L | `board_bundle.py` `resolved_start` (:1549) | A bundle's start could set `--mcp-env NAME:LD_PRELOAD=...` (a passed-on preset may not) | fixed, 1ce87cbb |
| 10 | L | `board_bundle.py` `plan_import` (:1107) | A folder's default `/home/u/.ssh:rw` was no "private location", and became `--mcp-mount NAME:/t=/home/u/.ssh:rw` | fixed, 1ce87cbb |
| 11 | L | `mcp_copy.py` `load_image` (:1208) | `mcp image-load` of a pinned reference in the repository of one of sandbox-ai's own images let the archive name `localhost/claude-sandbox:latest` (older than the range; the same root as 3) | fixed, 1ce87cbb |
| 12 | L | `checkpoints.py` `Restorer.chmod` (was :1385) | A mode put back by name followed a link swapped in at that moment | fixed, fe841ecb |
| 13 | L | `schedule.py` `summary_markdown` (:1055) | A file name with three backticks ended the summary's code block (Markdown of the agent's in the report) | fixed, 1ce87cbb |
| 14 | L | `runtime.py` `spend_billing` (:4368) | A made-up Claude-plan token in `Authorization` beside the credential that pays counted as "subscription" | fixed, 56d4526f |
| 15 | L | `schedule.py` `run_tree`, `snapshot` | In a SHA-256 repository a deleted path was written with 40 zeros: the run's commit failed (found while testing 2) | fixed, 1ce87cbb |
| 19 | L | `board_bundle.py` `plan_parts` | The plan named a bundle's LLM server but not its address (where every prompt of a start with `--llm NAME` goes), nor the image or command of an MCP server it brings | fixed, ff97c766 |
| 21 | L | `schedule.py` `Git.env`, `run_once`, `internal_run` | The timer keeps the PATH `schedule enable` was typed with; a project's virtualenv or `node_modules/.bin` in it (or a relative entry: the run's launcher starts in the workspace) is the agent's to write - a `git` or `podman` planted there ran on the host, unattended | fixed, 983cfadc |
| 16 | M | `runtime.py` `UsageScanner` / the meter | Requests whose answer carries no usage are not counted (see 3.1) | under a budget: streamed Chat Completions asked for their usage, unknown models at a fallback price (follow-ups 1); batches open |
| 17 | L | `checkpoints.py` `room`, `room_for`, `restore` | A file that cannot fit ends the checkpoint - also the one a restore takes first (see 3.2) | open |
| 20 | L | `runtime.py` `spend_provider`, `SpendMeter.record` | An answer of any host on a model API's path is priced by the model's name it says (see 3.4) | open |
| 18 | L | `letta.py` `project_hooks` | The project's hooks are looked for when Letta Code starts, not while it runs (see 3.3) | open, not verified |

### 1 (H) - `schedule --push`: the workspace's Git configuration ran a program on the host

The push refused a workspace whose `.git/config` names a credential helper, proxy, SSH command or URL rewrite, read
with `git config --local --list`. `--local` does not list `.git/config.worktree`, which Git reads once
`extensions.worktreeConfig` is set (a key the list did not refuse). The agent writes both files in its workspace;
the next scheduled run with `--commit BRANCH --push REMOTE` then ran, on the host and unattended:

- `credential.helper = !command` for an https remote that asks for a login (every push to GitHub does) - probed
  with a local HTTP server that answers 401: the command ran as the user;
- `url.<elsewhere>.insteadOf = <the recorded address>` - probed: the branch went to the other repository, although
  "only to the remote's address recorded when the schedule was added".

Fix: the push no longer runs in the workspace's repository. `push_branch` makes a bare repository of its own under
`~/.local/state/sandbox-ai/schedules/.push/` (removed afterwards), whose `objects/info/alternates` names the
workspace's objects, and pushes `COMMIT:refs/heads/BRANCH` from there (`--git-dir`): Git reads the user's
configuration (their credential helper, their SSH) and that repository's, nothing the agent can write. The old
check of `.git/config` stays (it refuses early, with the key's name). The forced `core.sshCommand=ssh` went: it
existed to override the workspace's and overrode the user's own too. Residual: the workspace's objects are read as
data; a commit the agent crafts can only name objects it can read or whose ids it knows.

Test: `test_push_reads_nothing_of_the_workspaces_git_configuration` (helper, sshCommand, hooksPath and insteadOf
in `config.worktree`: nothing runs, nothing goes elsewhere, the branch arrives at the recorded address).

### 2 (H) - the summary's snapshot read host files into the workspace

Before and after every scheduled run in a Git workspace, `snapshot` listed the files (`git ls-files`) and gave
their paths to `git hash-object -w --stdin-paths`. `hash-object` opens a path as it is - through a symbolic link on
the way to it. The agent commits `a/key` (any name it likes), then replaces the folder `a` by a link to
`~/.ssh`: the next run's first snapshot wrote the host's `~/.ssh/key` as a blob into the workspace's
`.git/objects`, where that run's agent reads it (probed: the blob was there). Any file the user can read, by
guessing its name; unattended.

Fix: sandbox-ai reads the work tree itself (`WorkTree`): every folder on the way to a path is opened relative to
its parent with `O_NOFOLLOW`, the file too; a path beyond a link is gone, as Git itself sees it. The object ids are
computed here (SHA-1 or SHA-256, as the repository says), `git cat-file --batch-check` says which objects are
missing, and only those are written - by one `git fast-import` (objects only; no hook, no filter), each file read
again without following a link and checked against its id. No race is left: nothing is opened by name.

Tests: `test_snapshot_never_reads_through_a_folder_the_agent_made_a_link`,
`test_snapshot_holds_modes_new_files_and_what_was_deleted`; the existing snapshot, commit and end-to-end tests pass
unchanged.

### 3 (H) - `board import --images` replaced an image of sandbox-ai's own

Every entry of a bundle's `images` list was given to `podman load` when `--images` was chosen. The archive was
checked against the entry's own `ref` ("it may name only its own repository") - but `ref` and `for` are the
bundle's words: an entry `{"ref": "localhost/claude-sandbox:latest", "for": "anything"}` with an archive of that
name was loaded, tied to no part of the bundle and shown nowhere in the plan. Every later start of Claude Code on
that machine would have run the bundle author's image (a start only asks Podman whether the image exists).

Fix (`image_entry_problem`): an image is loaded only as the image of a part of the bundle - an MCP server's, by
the pinned reference its definition names, or one of your images, by its record's reference - and never under a
name of sandbox-ai's own images (`mcp_copy.own_image_name`: `localhost/AGENT-sandbox[-VARIANT]`, the team images,
`localhost/sandbox-ai-*`), except your built image's own tag for that very part. The plan refuses such a bundle
(also without `--images`), `load_images` checks again, and an archive may not name one of sandbox-ai's own images
beside its own. Residual: an archive may still name other tags of its own repository (as `mcp image-load` allows
since 0.1.98); sandbox-ai runs an MCP server by its pinned digest and an image of yours by its recorded id, so
only your own use of such a tag outside sandbox-ai would see it. Finding 11 is the same hole in `mcp image-load`
(0.1.98): a pinned reference
`localhost/claude-sandbox@sha256:...` allowed the archive's `localhost/claude-sandbox:latest`.

Tests: `test_an_image_of_a_bundle_is_loaded_only_for_the_part_that_uses_it_never_as_sandbox_ais_own` (4 cases),
`test_the_image_of_a_part_is_still_loaded`, `test_mcp_image_load_never_takes_a_name_of_sandbox_ais_own_images`.

### 4 (M) - an agent took any path out of `checkpoint restore`

A restore first takes a checkpoint of the state now and compares. Paths that either checkpoint "skipped" were held
back: never touched. The state now is the agent's: it deletes `.git` (or `src`, or one file) and makes a FIFO of
that name - "a FIFO: not checkpointed" - and the restore left the FIFO there, without a word (probed: `restore`
reported one changed file and `keep.txt` stayed a FIFO). The same with `chmod 000 src` after changing what is in
it ("cannot be read"); a folder left without write permission ended the restore with an error.

Fix: held back is what the checkpoint being restored did not hold, the names excluded, and what the state now
leaves out because it is not the folder's own (a mount inside, sandbox-ai's own state, too deep). What is only now
a special file or unreadable is another kind of thing ("type": removed, put back as the checkpoint has it) or new
(added: removed). Before it compares, a restore makes the folders the agent locked their owner's again (`unlock`:
by name, never through a link; never one the restored checkpoint itself could not read), so what is in them is
compared like everything else - an excluded folder inside stays, what was added goes - and it opens a folder as
its owner whatever its bits say; the checkpoint's modes are put back at the end.

Tests: `test_a_path_made_a_special_file_since_is_restored_all_the_same`,
`test_a_folder_the_agent_locked_is_restored_all_the_same` (as root the scenario gives up CAP_DAC_OVERRIDE first:
root reads whatever the bits say). The existing test that a FIFO of before the checkpoint stays untouched passes.

### 5 (M) - a file of the current folder chose a scheduled run's options

`schedule add --board NAME` and `board show NAME` took `NAME` as a file when one of that name was in the current
folder, before the board kept under `~/.config/sandbox-ai/boards`. The run's process reads the board at every run:
`schedule run-now nightly` typed in a workspace in which the agent had written a file `nightly` started the
unattended run with that file's options (`--no-config --mount ~:/host:rw ...`).

Fix: `board_bundle.kept_board` (only the board kept here) for a schedule and for `board show`; `board export
--board NAME` prefers the kept board too (`./NAME` names the file).

### 6 (M) - a model request could ask for an answer the meter cannot read

The usage is read from the provider's answer; only an uncompressed, gzip or deflate answer is read, and the
request's `Accept-Encoding` was the agent's own unless the proxy had added a credential. With `Accept-Encoding: br`
no request of the run was counted and no budget ever used up. Fix: a request the meter counts asks for `identity`.
Test: `test_a_metered_request_asks_for_an_answer_the_proxy_can_count` (through the real proxy).

### 7-15

7: `tarfile` counts a GNU sparse member as a regular file and reads it as its full size; with a digest of the
zeros it passed every check and `board import FILE --check` wrote up to 64 archives of 64 GiB. Only plain members
now. 8: `git_folder` refuses a `.git` whose folder, common folder, objects or branch file lie outside the
workspace (a linked worktree or a submodule gets no summary any more: its `.git` is not the sandbox's either).
9, 10: as `presets.apply` refuses them for a preset passed on. 12: `chmod_nofollow`. 13: as the output's block.
14: only as the one credential of a request - not beside a key in the query, a second `Authorization`, a header
that ends in `key` or `token`. 15: the zero id has the length of the repository's ids. 19: a note of the plan each
(command line, app and web show the plan's notes): what is confirmed says where the conversations go. 21:
`safe_path` - the host-side Git of a repository, the push, the run's launcher and what the run's process starts
get a PATH without relative entries and without the workspace's folders (a start typed by hand has the launcher's
own note about programs the agent changed on the PATH; a scheduled run has nobody to read it).

## 2. Looked at and found sound

- Bundle: nothing is written before the plan is confirmed; archive members are data (names, order, sizes,
  digests); kit and variant files stay inside their folder and are scanned; presets are checked as an extra
  directory's; MCP servers as `mcp import` checks them; rulesets are validated; the app builds an imported board
  from the checked start (`from_argv`), from the bundle's state only bridge and team parts; a host's plan is made
  printable (`mcp_copy.received`) before it is shown.
- Checkpoints: the store is sandbox-ai's own state (a start refuses to mount it); walks and restores open every
  folder relative to its parent without following a link; the content's hash is taken from the bytes written (a
  clone is hashed as cloned): a file changed while read cannot poison an object; manifests are checked; the
  settings come from the user's defaults or the trusted (hashed) project file, never from the workspace as it is.
- Schedules: the project file is the trusted one; `--ask` is the schedule's; the timer's files are only ours; the
  host-side Git runs no hook, filter, textconv, fsmonitor, external diff or transport of the repository's; the
  commit goes to a branch that is not checked out; web and app add a schedule only after the confirmation.
- Spend: the control op that raises a budget is the control socket's (a peer inside the sandbox is refused before
  the op is read); `spend-control.json` and the spend log are the proxy's and the supervisor's; "subscription" is
  told by the credential, not by a header; the project's `budget` is the trusted file's.
- Notify: tokens only by secret name; requests are https, follow no redirect, time out; a failure's text is
  redacted of every secret used; texts a sandbox chose are shortened and stripped of control and markup
  characters, sent as plain text; a tool call's arguments, a URL's path or query never go out. (With `--detail
  full` a destination is a host name the sandbox chose; phones link what looks like one. By design.)
- Bridge rejoin: only the relay (the human's SSH) asks a half to rejoin; `rejoinable` takes only a half ended for
  a lost link at most 24 h ago, for the same side, session, run, peer identity and exactly the grant, questions,
  limits and time it had; a rejoin widens nothing.
- Files page: a host's folders are paths of that host (absolute, normalised, labels printable), opened over its
  SFTP connection, never as paths of this machine. `llm recommend`: a server's strings are printed printable.
- Web: every new route that changes something is in `NEEDS_CONFIRM`; ids and names are checked before they become
  arguments (`--name=value` forms, `--` before free words); the page builds its nodes with `el()` (no `innerHTML`),
  a report is shown as text. The `/tmp` fix (`temp_root`), the OAuth catcher's state and `uninstall --purge` read
  as described.

## 3. Open

### 3.1 (M) Requests the meter cannot count

The meter counts what the answer says. Not counted, and so never held back by a budget: OpenAI Chat Completions
streamed without `stream_options.include_usage` (the agent chooses the request's body), batch and background
requests (the answer comes later, by GET), a model the price table does not know (counted in tokens, cost
"unknown" - said by `sandbox-ai spend`). Closing the first needs the proxy to rewrite request bodies (it does not
today, and a provider that does not know the field may refuse it); the others need a decision: refuse what cannot
be priced on a run with a budget, or leave it said. Not done here: a change of behaviour for every agent, to be
decided by the user. A budget is a guard against a run that costs more than meant, not yet one an agent that
means to cannot get around.

Follow-up (fix/followups-1, decided while the user was away): on a run with a budget the proxy adds
`stream_options.include_usage` to a streamed Chat Completions request without it, and a model the table does not know
is counted at the table's highest prices (said with the model and the price). Batch and background requests stay as
described: held back once a budget is used up, not priced.

### 3.2 (L) A file that cannot fit ends the checkpoint

Review 3x-2 made a checkpoint stop before the disk is full, with a message. An agent can use that: one sparse file
of a terabyte in the workspace, and every later start runs without a checkpoint (said at each start), and a
restore's own "before" checkpoint fails the same way (said; `checkpoint diff` still names the file, which the
human deletes). Skipping such a file by name would keep the rest protected; it changes what 3x-2 just settled
("stops and says so"), so it is left for a decision.

### 3.3 (L) Letta Code's project hooks

`letta.project_hooks` refuses a start while rules hold and the workspace's `.letta/settings*.json` has hooks. It
looks when Letta Code starts. Whether Letta Code reads a hook the agent writes there during the run could not be
checked here (no Letta Code in this sandbox); if it does, the rule hook's verdict can be rewritten mid-run.

### 3.4 (L) Usage a host made up

The meter reads `model` and `usage` from the answer of every POST to a model API's path, whatever the host, and a
host it does not know is priced by the model name the answer gives ("the provider of the price table that knows
MODEL": a gateway of yours is priced that way). A host the run may reach that answers what the agent sends it (its
own server, an echo service) lets the agent write any cost into the spend log: its own budget is used up (its own
loss), but a limit shared with other runs (`spend limit set --all --month`) too - they are refused until you raise
it - and the statistics hold a cost nobody paid. No money is lost and the agent gains nothing it could not also do
by spending for real where it has a provider. Pricing only known providers, `--llm` servers and hosts the proxy
adds a key for would end it, and would stop pricing a gateway the agent holds the key for itself: a decision for
the user.

## 4. Tests

Core (`/work/.venv/bin/python -m pytest -q -p no:cacheprovider`, no xdist), on the branch's last code commit:

- the suites of what changed - `tests/test_schedule.py tests/test_board_bundle.py tests/test_checkpoints.py
  tests/test_checkpoints_e2e.py tests/test_spend.py tests/test_mcp_copy.py tests/test_web_mcp_copy.py`: 233 passed;
- the whole of `tests/` (292 files), in four parts one after and beside the other: files 1-113 (alphabetical) 7035
  passed, 135 skipped; files 114-172 1386 passed, 80 skipped; files 173-232 3701 passed, 74 skipped; files 233-292
  6743 passed, 47 skipped - 18865 passed, 336 skipped, none failed. (The first part ran as one process that was
  stopped after file 113 to go on in three; the test that failed at that moment, in `test_live_approvals.py`,
  failed from the stop signal and passes: 18 passed alone, and in its part. `test_board_bundle.py` and
  `test_checkpoints*.py` of the first part were run again after the last changes: in the 233 above.)

App (offscreen, the pinned PySide6): `tests/test_board_bundle_app.py tests/test_checkpoints_app.py
tests/test_notify_settings.py tests/test_references.py tests/test_schedules_view.py tests/test_use_case_board.py
tests/test_i18n.py`: 40 passed. No file of the app was changed; the whole app suite was not run.
