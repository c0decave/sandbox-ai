# Final review 3x-2: user journeys and robustness (2026-11-18)

Scope: everything since the 0.1.98 release commit (`git diff d9ef48f6..main`, main at 9567aaa1): board and session
export/import, the Files page with the hosts' sandboxes (`list --folders`), phone notifications, scheduled runs,
spend limits, model per use case (`llm recommend`, `--use-case`), workspace checkpoints, backlog A, board icons, the
hacker (neon) theme. Focus: every new feature end to end as a user would (CLI in an isolated HOME, the app's and the
web's tests offscreen), crashes and kills mid-operation (a checkpoint, a restore, a scheduled run, an import), a full
disk, slow and unreachable servers and hosts, many checkpoints/runs/spend lines, upgrades from 0.1.98 state, GUI
thread and timers, descriptor and process leaks, performance (a checkpoint at the start of a big workspace, the
proxy's spend accounting per request). Then the full core and app suites.

Branch: `review/3x-2` (worktree `.claude/worktrees/review3x-2`), off main 9567aaa1, not merged, not pushed. Every fix
has a regression test that failed before it.

Severity: **H** high (data lost, the machine or a feature unusable), **M** medium, **L** low.

## Findings

| # | Sev | Where (main) | What | Status |
|---|-----|--------------|------|--------|
| 1 | H | sandbox/checkpoints.py:549 `Walk`, :799 `take` | A first checkpoint (at every start, default on) could fill the disk; its partial content stayed, so every start filled it again | fixed 159896a0, 85d505c2, 743fc948, 9c52782a, de067533 |
| 2 | M | sandbox/checkpoints.py:799 `take`, :1504 `prune` | A checkpoint ended mid-way left content that no prune and no gc ever removed | fixed 36303baf |
| 3 | M | sandbox/statedirs.py:1468 `old_logs` | Spend logs went after `log_days` while a month's spend limit still counted them | fixed 2af81a37 |
| 4 | L | sandbox/schedule.py:1069 `run_once` | A run killed mid-way left no history entry | fixed afd0a7dc |
| 5 | L | sandbox/schedule.py:1069 `run_once` | The kept start of a killed run was never deleted with the old ones | fixed 26ee68f7 |
| 6 | L | sandbox/checkpoints.py:885 `record_run` | One run record per start, never removed; all read by every `restore NAME` | fixed b25d4c3d |
| 7 | L | sandbox/checkpoints.py:1224 `folder_fd`, :1397 `apply_root` | `restore --path` into folders the agent removed made them 0700 with the time of now | fixed fd2ec0cc |
| 8 | L | sandbox/llm.py:3470 `recommend` | Servers asked one after the other: three unreachable ones made it wait 25 s | fixed a5f88490 |
| 9 | L | sandbox/uninstall.py:844 | Timer turned off, then "nothing removed" when a start would not stop | fixed 790d4232 |
| 10 | L | sandbox/checkpoints.py:1310 `write_into` | A restore killed mid-file leaves `.sandbox-ai-restore-*` in the workspace | open |
| 11 | L | sandbox/schedule.py:1202 `decide` | "N times due since the last tick: one run" is recorded as a *skipped* history row beside the run | open |
| 12 | L | sandbox/checkpoints.py:549 `Walk` | Memory of a checkpoint grows with the file count (~1.4 MB per 1000 files) | open |
| 13 | L | sandbox/spend.py:477 `start_budgets`, sandbox/monitor.py:1670 `spend_of` | A month's limit (at each start) and the Monitor's 30-day window parse every spend line of the period | open |
| 15 | L (suite) | tests/test_net.py:197 `child` | `test_stop_ends_a_one_off_sandbox` failed once in the full core suite under load (the child's command line read without its arguments) | fixed 5f1190bb |
| 14 | L | projects/sandbox-ai-app/sandbox_ai_app/schedules_view.py:336, notify_view.py:245 | A host with an older sandbox-ai shows "unknown command 'schedule'" instead of "update it" | fixed 77f25428 |

### 1. H - A checkpoint at the start could fill the disk, again at every start

A start checkpoints its writable workspaces before the agent runs (`checkpoint_start` defaults to true); a folder's
first checkpoint copies every file into `~/.local/state/sandbox-ai/checkpoints` (clones only on btrfs/XFS/APFS).
Nothing looked at the free space: a workspace with more data than the disk had free (datasets, model files, build
output not in `checkpoint_exclude`) filled the disk - the copy failed with ENOSPC, the start went on *with the disk
full for the whole run* (and the rest of the machine with it), and what had been copied stayed in the store. The
next start did the same. `checkpoint_gb` (10 GiB) does not help: it is applied after a checkpoint, and the newest
always stays.

Fixed: before a folder's first checkpoint its files are summed (a stat walk, `room_for`; where the store's file
system clones - tried with one large file - only the small files count) and a folder that cannot fit is said at once,
with its size, nothing copied. While copying, the free space is looked at every 64 MiB (the large files the pool
still reads count too) and the checkpoint stops before less than a 100th of the disk (at least 1 GiB, at most 4 GiB)
is free (`NoSpace`). A checkpoint that fails that way or with an I/O error removes what it wrote at once (`sweep`).
The start says it ("no checkpoint of the workspaces at this start (only 3.1 GiB free on the disk of the
checkpoints...): make room, leave large folders out (checkpoint_exclude) or use --no-checkpoint") and goes on.
Tests: `test_a_checkpoint_never_fills_the_disk_and_what_it_wrote_goes_at_once`,
`test_a_failed_checkpoint_removes_what_it_wrote`, `test_the_room_a_checkpoint_needs_counts_the_files_still_being_read`,
`test_a_folder_larger_than_the_room_left_is_refused_before_anything_is_copied`. README/README_DE, the app's help (EN,
DE) and the web help say it.

### 2. M - A checkpoint ended mid-way leaked its content for good

Ctrl-C during a start's checkpoint, a start that failed while its checkpoint ran in the background (the thread is a
daemon: it dies with the launcher), the end of a run while a periodic checkpoint was under way (it is stopped), the
machine off: the objects written so far, and the file in `objects/tmp`, belonged to no checkpoint. `prune` collects
unreferenced objects only when something is old enough to go (or the usage is unknown), so they stayed - also after
`sandbox-ai gc` - and `usage.json` did not count them. Reproduced: 5 objects of an interrupted take survived
`prune_all`. Fixed: `take` marks the store (`taking.json`) until its record is written; a prune (after the next
checkpoint, or gc) that finds the mark - under the store lock no checkpoint is being written - sweeps.
Test: `test_an_interrupted_checkpoint_leaves_nothing_behind_after_gc`.

### 3. M - A month's spend limit lost the spend logs older than `log_days`

`spend limit set --month` counts what every run spent since the month began, from the runs' `spend-*.jsonl`; those
were removed with the other logs after `log_days` (README: "a month's limit counts what the logs still hold"). With
`log_days` 7 a month's limit saw the last week and let runs spend past it; with the default 30 days the first day of a
31-day month. Fixed: `old_logs` keeps a spend log last written in the current month; after it, `log_days` holds.
Test: `test_spend_logs_stay_while_a_months_limit_counts_them` (tests/test_state_logs.py).

### 4./5. L - A scheduled run killed mid-way

A run whose `schedule.py _run` process is killed (machine shut down, user session ended, OOM) wrote no history entry
(History... and `schedule history` did not show it at all; only a stale `current.json` remained), and its kept start
was named in the schedule's state only after the run - so `--keep-starts` never deleted it (a kept start with its
HOME per killed run). Fixed: the start is named in the state before it is launched; the next run finds the mark
(`current.json`, while it holds the lock) and records the killed run as `error`, "ended mid-way", with a summary of
the end of its output and its duration up to its last output. Tests:
`test_the_start_of_a_run_that_was_killed_is_deleted_with_the_old_ones`,
`test_a_run_killed_mid_way_is_in_the_history_after_the_next`; checked through `./sandbox-ai schedule run-now --wait`,
`history`, `report`.

### 6. L - Run records piled up

`checkpoint_ready` writes `checkpoints/runs/KEY.json` for every start (every scheduled run too); nothing removed them
and `resolve_run` (every `checkpoint list NAME`, `restore NAME`) reads them all. Fixed: `sandbox-ai gc` forgets those
a month old whose start and run no checkpoint names any more (never a running one's). Test:
`test_gc_forgets_the_run_records_no_checkpoint_needs_any_more`.

### 7. L - Restoring a path made its missing parents 0700

`checkpoint restore ID --path src/pkg` after the agent removed `src`: `src` came back with mode 0700 and the time of
now (only the chosen paths' folders got their modes). Fixed: the folders made on the way get their checkpointed mode
and time. Test: `test_a_path_restored_into_folders_that_are_gone_puts_them_back_as_they_were`.

### 8. L - `llm recommend` waited for each unreachable server in turn

The servers were listed one after the other; each that does not answer costs its time limit (measured: 15 s for one,
25 s for three). The web interface's use cases call it with a 120 s limit. Fixed: asked in parallel (those behind a
VPN still one after the other, each through its own tunnel). Test:
`test_recommend_asks_its_servers_at_once_not_one_after_the_other`.

### 9. L - Uninstall said "nothing removed" after it turned the timer off

The schedules' timer is turned off first (on purpose: no scheduled run starts while the rest goes). When a running
start would then not stop, the uninstall ended with "nothing removed" although the timer was off and its unit files
gone. Fixed: it says the timer is off now and how to turn it on again. Test:
`test_a_start_that_would_not_stop_says_the_timer_is_off_already`.

### 14. L - Schedules and phone channels of an older host

On a host whose sandbox-ai predates them (0.1.98: "unknown command 'schedule'") the Schedules box and the phone
channels showed that error (the local check `has_schedule` covers only this machine). Fixed: "The sandbox-ai of HOST
has no schedules yet; update it (Hosts page)." (German too). Test:
`test_a_host_whose_sandbox_ai_has_no_schedules_or_channels_says_update_it`.

### 10.-13. Open (low)

10. A restore SIGKILLed while writing a file leaves `.sandbox-ai-restore-XXXXXXXXXXXX` (a partial copy, possibly
    large) beside its place. It is no data loss (the "before" checkpoint holds the state before the restore), the next
    restore of that folder removes it (it is "added"), but a later checkpoint holds it meanwhile. Left: sweeping
    those names needs care where two projects share a folder; worth a backlog item.
11. When a tick finds a schedule due more than once (a wake after hours) it records a history row with status
    `skipped` and the note "N times due since the last tick: one run", beside the run it starts. The list's LAST
    ignores skipped rows, so only History... shows it; cosmetic, and changing the status would change how the app and
    web colour it.
12. A checkpoint keeps its manifest in memory: measured 68 MB for 50 000 files (about 1.4 GB for a million) in the
    launcher at the start, and two manifests during a restore. Fine for workspaces as they are; a streaming manifest
    is a larger change.
13. With a month's limit every start parses the spend lines of the month (600 000 lines: 2.5 s added to the start),
    and the System monitor's 30-day window does the same every minute while it is shown (7 s CPU). A per-file cache of
    the totals of ended runs would make both cheap.
## Journeys checked (no finding)

- Checkpoints: 50 001 files / 214 MiB: first checkpoint 2.4 s, unchanged ones 0.6-0.7 s (stat only, 0 new bytes);
  `diff` 0.5 s; restore of 252 deleted + 1 changed + 2 added 1.6 s; restore killed (SIGKILL) at 0.9, 1.2, 1.5 and
  1.8 s, then restored again: the workspace complete each time, `objects/tmp` empty; `--path` (sandbox and host
  paths), a path outside refused, `--dry-run`, no terminal without `--yes`, delete asked.
- Spend: the proxy's accounting 16-21 µs per request (pricing, budgets, the control file's stat, the log line);
  `spend --by day` over 600 000 lines 4 s; a damaged `spend.json` stops a start with the reason (the dry run names
  it); raise and the control file (one control connection at a time: no lost raise).
- Schedules: add, list, show, run-now `--wait` without Podman (failed cleanly with a summary and the output's tail),
  history, report; the systemd unit keeps `KillMode=process` (runs outlive the tick).
- Notify: add, test against an unreachable server (10 s limit, the failure kept for `list`), a webhook over plain
  http to another host refused.
- Board export on one machine (HOME), `import --check`, import with `--map` on another; plan, names, `board list`.
- `list --folders` (none), `llm recommend` with no servers and with three unreachable ones.
- App: no new QTimer in the range; the new views load in the background (`background`, owner, token); the Files page
  reads hosts' folders only while shown, one thread per host with a time limit; board icons cached with a bound.
- Web: new routes run the CLI with time limits on a threading server; no new polling in app.js.

### 15. L (suite) - A flaky stop test

`tests/test_net.py::test_stop_ends_a_one_off_sandbox` failed once in the full core run (load average about 6, other
reviewers' suites beside it): "PID N does not run a sandbox-ai launcher (no agent_sandbox.py in its command line)" -
the stand-in launcher's command line was read without its arguments (a process not yet, or no longer, running as
itself). It passed 15 times alone under 8 busy loops and in 3 full runs of the file. The fixture now waits until the
child runs with its arguments, or fails with the child's stderr when it ended - a test never races the child's start
and a real cause would be named.

## Test results

- Full core suite (`tests/`, run while the fixes landed): 18 762 passed, 337 skipped, 120 subtests passed, 1 failed
  (finding 15, fixed) in 81 min. Afterwards the 20 files of the areas the fixes touch (checkpoints, schedules, spend, state logs, uninstall, use
  cases, starts, net, web, docs and help, contracts, monitor) after the last core change: 4 245 passed, 0 failed.
- Full app suite (258 files, chunks of 20, offscreen) after the last app change: 3 869 passed, 20 skipped, 0 failed.
- `test_web_matrix`, `test_web_use_case`, `test_feature_matrix` after the checkpoint changes: 4 481 passed.
