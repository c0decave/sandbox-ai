# Final review 3x-3: correctness and code quality (0.1.99, d9ef48f6..main)

Reviewer 3 of the three independent reviews of the features of the last 24 hours (brief: final-reviews-3x.md).
Focus: logic errors, edge cases, error handling, data formats and their versions, consistency between the CLI, the
app and the web interface, help and docs claims against behaviour, dead code, duplicated logic, tests that test the
wrong thing. Branch `review/3x-3` off main 9567aaa1; nothing merged or pushed.

Range read: board/session export and import (board_bundle.py), checkpoints (checkpoints.py and the launcher's
checkpoint_plan/ready), schedules (schedule.py), phone notifications (notify.py), spend (spend.py, the proxy's
SpendMeter and spend_* in runtime.py, the launcher's spend_config/watch), model per use case (model_usecases.py,
llm.py's recommend and use_case_start, the app's use_case.py), the new web routes (webui.py), list --folders
(starts.py), uninstall --purge, --with a,b, the bridge relay's rejoin (read, not changed), the redaction and the
release notes, README and help of all of them. Each module was also run by hand through `./sandbox-ai` with edge
input (past and impossible times, cron fields, empty or broken values, a restore across type changes - file to
folder, folder to file, link retargeted, nested deletes, partial restores by path).

Every fixed finding has a failing test first, then the fix; user-visible ones also through the real entry point
(`./sandbox-ai`). Severities: Medium - a promise of the feature does not hold in a reachable case; Low - a narrow
case, a wrong text, a parity gap.

## Findings

| # | Severity | Where | What | Status |
|---|---|---|---|---|
| 1 | Medium | sandbox/schedule.py:827, :853 (`run_tree`, `commit_changes`) | `--commit` committed (and `--push` pushed) the human's uncommitted work of before the run | fixed d5bdc1c2 |
| 2 | Medium | sandbox/agent_sandbox.py:9238 (`check_budget`, called at :5054) | `--budget` with `--no-proxy` held nothing | fixed 5d72afb2 |
| 3 | Medium | sandbox/checkpoints.py:799, :850 (`live_runs`, `take`) | the limits after a new checkpoint dropped another running run's start checkpoint | fixed 0597a546 |
| 4 | Low | sandbox/runtime.py:4468, :4618 (`spend_next_end`, `SpendMeter.roll`) | a month's limit started again only once | fixed 5d72afb2 |
| 5 | Low | sandbox/runtime.py:4367 (`SPEND_BEDROCK`, `spend_model_key`) | Bedrock's apac./jp./au./us-gov. profiles unpriced: no budget held them | fixed 5d72afb2 |
| 6 | Low | sandbox/spend.py:477, :626 (`start_budgets`, `SpendWatch.others`) | a shared limit did not count a running run with its own `--log-dir` | fixed 08ae67e3 |
| 7 | Low | sandbox/schedule.py:1481 (`build`) | `--at "DATE TIME"` in the past added a schedule that never runs | fixed 73a02308 |
| 8 | Low | sandbox/schedule.py:1780 (`set_paused`) | resume made up a push that came while paused | fixed 73a02308 |
| 9 | Low | sandbox/notify.py:83, :403 (`SINGULAR`, `render`) | "2 questions, 1 an alarm" - and the tests asserted it | fixed 91e477f4 |
| 10 | Low | sandbox/board_bundle.py:1668, :1850 (`apply_import`, `forget_image`) | a failed import left image records and an LLM CA file behind | fixed df7d1da9 |
| 11 | Low | sandbox/model_usecases.py:337 (`_nearest`) | a related use case's choice showed settings the start does not apply | fixed 4d8b5f01 |
| 12 | Low | projects/sandbox-ai-app/sandbox_ai_app/use_case.py:83 (`servers_of`) | `http://[::1]/v1` not this machine's loopback on the Board (own URL parsing) | fixed a72dc6c7 |
| 13 | Low | sandbox/webui.py:2045 (`checkpoints`) | a start with no checkpoints: error on the web, "none yet" in the app | fixed 5155b33c |
| 14 | Low | sandbox/monitor.py:1670 (`spend_of`) | the System monitor's cost missed a running run with its own `--log-dir` | fixed 7d5fab81 |
| 15 | Low | sandbox/web/app.js:1394 (`checkpointsCard`), webui.py:2064 | the web cannot restore single paths (CLI and app can) | open |
| 16 | Low | sandbox/runtime.py:4468 (`spend_next_end`, day) | a day's limit started again 24 h on: an hour off across a clock change | open |
| 17 | Low | sandbox/notify.py:699 (`Dispatcher.flush`) | a summary held before quiet hours was sent in them | fixed a6948a40 |
| 18 | Low | projects/sandbox-ai-app/sandbox_ai_app/backend.py:2197 (`file_roots`) vs sandbox/starts.py `folders` | two readings of "the folders of the starts" (this machine's / a host's) | open |

### 1. `schedule --commit` committed the human's uncommitted work (Medium, fixed d5bdc1c2)

`commit_changes` committed the snapshot after the run (`after["tree"]`: the whole work tree, tracked and untracked
files) on top of HEAD. A workspace with uncommitted work of the human from before the run (a `wip.txt`, an edited
file) had it in the commit - the run's summary said "1 file changed" (the agent's, between the two snapshots) - and
`--push REMOTE` pushed it. Reproduced with the module's own functions: summary `agent.txt | 1 +`, commit
`agent.txt, wip.txt`. Fix: `run_tree` builds the commit's tree from HEAD's (the agent's commits included) with only
the paths that changed between the run's two snapshots, as the run left them (an index of its own in the run's
folder; `diff-tree -z` of two trees; no filter, hook or program of the repository runs). A file both the human and
the agent changed goes as the run left it (it cannot be split). Test:
`test_commit_holds_what_the_agent_changed_never_what_was_uncommitted_before`; the existing ones unchanged.

### 2. `--budget` with `--no-proxy` held nothing (Medium, fixed 5d72afb2)

Scenario: `sandbox-ai claude --budget 5 --no-proxy` (or a trusted `.sandbox-ai.json` with `"budget": 5` and
`--no-proxy`) started normally. Without the connection proxy nothing prices a request or refuses the one past the
budget; only the dry run's `"spend": {"counted": false}` hinted at it. The other options that need the proxy
(`--api-key`, `--publish`) refuse `--no-proxy` with "drop --no-proxy". Fix: `check_budget` refuses it the same way
(`--offline` is allowed: nothing is spent); the option's help says "not with --no-proxy"; README EN/DE. A budget from
your defaults file is not refused (it would break every `--no-proxy` start; the release notes already say the proxy
sees no traffic it does not open). Tests: `test_check_budget` (contracts), and through `./sandbox-ai ... --dry-run`
with the option and with the project file (`test_a_budget_without_the_proxy_is_refused_not_silently_dropped`).

### 3. Another running run's start checkpoint was pruned (Medium, fixed 0597a546)

Help and release notes: "the newest checkpoint and the start checkpoints of running runs always stay". `take()`
applied the limits after each new checkpoint with `live=[run]` - only the run that took it. Two starts of one project
(or a manual checkpoint of one while another runs) let `checkpoint_count`, `checkpoint_days` or `checkpoint_gb`
drop the other running run's start checkpoint - `checkpoint restore NAME` then had nothing to go back to. Only
`sandbox-ai gc` passed every running run. Fix: `live_runs()` reads the runs whose launcher runs (their run.json,
read only - `net.running()` would clean up ended runs as a side effect) and `take()` protects their start
checkpoints. Test: `test_a_new_checkpoint_keeps_the_start_checkpoint_of_every_run_that_runs` (and that it is pruned
again once that run ended).

### 4. A month's limit started again only once (Low, fixed 5d72afb2)

`SpendMeter.roll` set a month budget's `until` to None after its first rollover: a run going over two month ends
counted the second month on the first's (a day's limit added 86400 each time). Fix: `spend_next_end` steps a month by
the days of that month (computed without new imports in the proxy's runtime). Test:
`test_a_month_s_limit_starts_again_every_month_of_a_long_run`.

### 5. Bedrock's regional inference profiles had no price (Low, fixed 5d72afb2)

`spend_model_key` stripped `anthropic.`, `us.anthropic.`, `eu.anthropic.` and `global.anthropic.` only. Bedrock's
cross-region profiles in Asia-Pacific, Japan, Australia and GovCloud (`apac.`, `jp.`, `au.`, `us-gov.`) kept their
prefix: cost "unknown" - and a request of unknown cost adds nothing to a budget, so no budget held such a run. Fix:
one pattern for an optional region before `anthropic.`. Test: `test_bedrock_s_regional_inference_profiles_are_priced`.

### 6. A shared limit missed a running run with its own --log-dir (Low, fixed 08ae67e3)

`sandbox-ai spend` reads the running runs' own log directories (`running_log_dirs`), but `start_budgets` (what the
period used before a start) and `SpendWatch.others` (what other runs spend meanwhile) read only the state
directory's logs: a run started with `--log-dir DIR` spent outside every day's and month's limit. Fix: both read the
running runs' directories too. Test: `test_a_shared_limit_counts_a_running_run_whose_logs_are_elsewhere`.

### 7. A once time that has passed (Low, fixed 73a02308)

`schedule add NAME --at "2026-11-01 10:00"` on the 17th was added ("next run -", state "done") and never ran - also
from the app's and the web's forms, which run the same command (`add_arguments`). Fix: refused, "that time has
passed". A daily `--at HH:MM` is unaffected. README EN/DE. Test through `./sandbox-ai` with the fake clock
(`test_a_once_time_that_has_passed_is_refused_not_kept_as_a_schedule_that_never_runs`).

### 8. Resume made up a push (Low, fixed 73a02308)

`schedule pause|resume`: "runs missed while paused are not made up" - `set_paused` reset `last_due` for the
timetable, but not `push_seen`: a push to `--on-push`'s repository while paused started a run at the first tick
after resume. Fix: resume takes the branches as they are as seen. Test:
`test_resume_does_not_make_up_a_push_that_came_while_paused`.

### 9. "1 an alarm" (Low, fixed 91e477f4) - a test that tested the wrong thing

A rate-limited summary put the count before the minimal message's start for a kind that came once: "2 questions,
1 an alarm on box", "1 a safeguard acted", "1 a run ended". `test_notify.py` asserted exactly those texts; the help,
README and release notes promise "3 questions, 1 safeguard event" (and the module's own help said "1 safeguard").
Fix: a singular per event; the two tests corrected; a new test checks every kind and the help's example.

### 10. A failed import left parts behind (Low, fixed df7d1da9)

`apply_import` takes back what it wrote when a later step fails, except the records of your images
(`services.record_entry`) and an LLM server's CA file - written before its record was even checked, and overwriting
the CA of a server it replaced. Fix: both get undo steps (the record and the CA as they were). Test with a real
certificate (openssl) and a failure at the board's state:
`test_an_import_that_fails_half_way_takes_back_the_image_records_it_wrote`; checked to fail without each half.

### 11. Settings shown that the start does not apply (Low, fixed 4d8b5f01)

A use case without tests takes its related use case's choice (`_nearest`); `llm recommend` and the Board showed that
choice's settings ("settings: temperature=0.6 (over the model's profile, under yours)"), but the start with
`--use-case THIS` applies only this use case's settings (`settings_for` finds none for that model; `use_case_start`
copies settings only for source "table"). Fix: the related choice shows none and its note says where they are
(`--use-case OTHER`). Test: `test_the_choice_of_a_related_use_case_shows_no_settings_the_start_would_not_apply`.

### 12. The Board's own URL parsing (Low, fixed a72dc6c7) - duplicated logic

`use_case.servers_of` found a server's host by cutting the URL at its last `:` - `http://[::1]/v1` became `[:` and
was not this machine's loopback, so its models' fit was "not known" on the Board while `llm recommend`
(`llm.parse_url`/`memory_of`) used this machine's memory. Fix: `urllib.parse.urlsplit(...).hostname`, the loopback
names as `llm.LOOPBACK_HOSTS`. App test: `test_an_ipv6_loopback_server_without_a_port_has_this_machines_memory`.
(The app keeps its own copy of the loopback test: the app reads `llm list --json` of a host, which has no parsed
URL; sharing `llm.parse_url` would mean loading this machine's llm.py for a host's record.)

### 13. Web vs app: a start with no checkpoints (Low, fixed 5155b33c)

`checkpoint list NAME` exits 1 with "no start named NAME has checkpoints" when nothing can be found for it; the app
turns that into "No checkpoints of NAME yet", the web card showed the command's failure. Fix: the web route answers
`[]` for that text (anything else stays a failure). Test: `test_a_start_with_no_checkpoints_lists_none_as_in_the_app`.

### 14. The System monitor's cost and --log-dir (Low, fixed 7d5fab81)

The same as 6 for `sandbox-ai stats usage` (the app's and the web's System monitor, its Cost column): a running run
with `--log-dir DIR` had no cost there, though `sandbox-ai spend` names it. Test:
`test_the_system_monitor_s_cost_counts_a_running_run_whose_logs_are_elsewhere`.

### 15. The web cannot restore single paths (Low, open)

`checkpoint restore ID --path P` and the app's dialog (its "Restore only these paths" field, preview and restore with
the paths) restore single files or folders; the web route `POST /checkpoints/ID/restore` takes `paths`, but its
preview route takes none and the page never sends any. Open: a UI addition (field, preview with the paths, German
strings, help) rather than a fix; nothing wrong happens today - the web restores the whole checkpoint after a preview
of the whole.

### 16. A day's limit across a clock change (Low, open)

`spend_next_end` (as before) moves a day's end 24 hours on; on the two nights a year the clock changes, the day's
limit starts again an hour early or late for a run that goes over that midnight. The proxy runs in the sandbox's
time zone, not necessarily the host's; the exact boundary would need the launcher to pass the next ends (or the
zone). Left: an hour of one night, and the next start computes it exactly again.

### 17. Quiet hours and a held summary (Low, fixed a6948a40)

Events held for a summary (`--rate`) shortly before a channel's quiet hours began were sent by the summary's timer
inside them; the help says quiet hours hold back everything but alarms and "it is not sent later". `flush` now drops
what it holds once the quiet hours began (alarms are never held). Test:
`test_what_was_held_is_not_sent_once_the_quiet_hours_began`.

### 18. Two readings of the starts' folders (Low, open) - duplicated logic

The Files page reads this machine's folders with `backend.file_roots` (its own walk over runs and sessions,
`paths_from_options`) and a host's with `sandbox-ai list --folders` (`starts.folders`, `workspaces.listing` and
`next_start_targets`). The two can drift (they already differ in how a session's extra workspaces are read).
Open: moving the local side onto `starts.folders` changes the page's labels and order and its tests; worth doing as
its own change.

## Checked and found right

- Checkpoints: take/diff/restore across type changes (file to folder, folder to file, a retargeted link, nested
  deletes, an excluded folder left alone), partial restores by relative, sandbox and host paths, a workspace that is
  a file, the reentrant store lock under `restore()` (filelock is reentrant per thread), prune's three limits.
- Schedules: cron fields (names, ranges, steps, 7 as Sunday, day-of-month OR day-of-week, impossible dates refused
  unless the weekday is restricted), DST handling, missed/skip, the lock, add_arguments shared by app and web.
- Spend: the proxy's refusal before the request, the raise (cumulative, reset of the warnings), subscription and
  local billing, `--since` parsing, the price table merge, `limit set/remove`; prices `set` with nan/inf refused.
- Notify: quiet hours over midnight (and equal start/end refused), the rate's coalescing across processes, the
  dispatcher's final flush; channel checks shared by CLI, app and web.
- The names' patterns (schedules, channels, checkpoint ids) are the modules' own in the CLI, app and web.
- No dead functions in the new modules (every top-level function and method is referenced).
- `--with a,b`, `uninstall --purge` for packages, the redaction across `;`, `&`, `|`, `)` (as the notes say).

## Tests

- Core suite, whole (`tests/`, without `tests/devtools`), on this branch: 18772 passed, 337 skipped, 1 failed in
  1:21:45 - the failure was this review's own: `test_contract_manifest_covers_every_launcher_function` (the new
  `runtime.spend_next_end` had no contract); fixed in ca5901ab, then `tests/test_agent_sandbox_contracts.py`:
  2757 passed.
- Three fixes were committed while that run went (notify's quiet hours, schedule's commit, the monitor's cost); the
  suites they and the docs touch, run again at the end: test_notify, test_schedule, test_spend, test_monitor,
  test_web, test_web_matrix, test_web_use_case, test_model_usecases, test_model_usecases_start, test_checkpoints,
  test_checkpoints_e2e, test_board_bundle, test_docs_claims, test_docs_every_option, test_release_notes,
  test_uninstall, test_completion: 4691 passed, 2 skipped, 0 failed.
- App suite, whole (`projects/sandbox-ai-app/tests`, 13 chunks of 20 files, offscreen): 3869 passed, 20 skipped,
  0 failed.
