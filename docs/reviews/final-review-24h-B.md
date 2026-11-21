# Final 24 h review B: user journeys and robustness (2026-11-17)

Scope: everything merged into `main` in `f4edf0a6..167ee6e9` (374 files; product code in `sandbox/`,
`projects/sandbox-ai-app/sandbox_ai_app/`, `sandbox/web/`): the traffic light per local model, the system monitor
(`monitor.py`, `top`, `stats usage`, the app's and the web's page), the login callback relay and Codex's device login,
the approvals rework, kits from the host's agents, local-model harness settings, sealed `.ovpn`/tokens and the primary
password, jadx/CFR presets, `mcp copy`, `list --all-users`, `uninstall`, remote folders, bridges across hosts, pages
that follow the host, Teams on the Board, allow-lists split by comma/space, MCP egress rules.

Focus (B): each feature used as a user does - the command line, the app offscreen, the web page under node, against a
host that answers slowly or not at all, with many runs; German texts and help against what happens; upgrades from
0.1.97 state; races, descriptors, timers while hidden, the GUI thread; the monitor's cost (it runs every 10 s for every
run) and the cost of the pages that read it; the full core and app suites run and every failure fixed.

Branch `review/24h-B` (worktree `.claude/worktrees/review24-B`), not merged, not pushed. Every bug got a failing test
first; the tests stay as regression tests. Independence: written before reading the other reviewer's report.

Severity: **M** medium (a feature misleads or stalls in normal use), **L** low (cosmetic, rare, or a test fragility),
**S** a red suite (the brief: every failure fixed).

## 1. Findings

| # | Sev | Where | What (scenario) | Fix |
|---|-----|-------|-----------------|-----|
| 1 | M | `projects/sandbox-ai-app/sandbox_ai_app/monitor_page.py:322` (`host_changed`), `:348` (`refresh`), `:361`, `:402` | System monitor page, host A slow or unreachable (SSH timeout, `READ_SECONDS` 60 + 15): the human chooses this machine (or the Sandboxes page's host changes it) - `refresh()` returned while A's read was in flight, so the page stayed empty for up to 75 s; a history window changed during a history read was never read until the next minute. | c41a246d: the read in flight is a ticket; a host change forgets it and reads the new host at once; the old answer is dropped by its ticket; a window changed meanwhile is read when the old answer comes. Test `test_another_host_chosen_while_a_slow_host_is_read_is_read_at_once`. |
| 2 | M (perf) | `sandbox/monitor.py:1285` (`read_series`), `:1327` (`read_run`) | `stats usage` (the app asks every 60 s while the page is shown, the web on every window change, over SSH for a host): for the 24 h window (minute tier) every raw line of every run was parsed only to take the minute begun; for 1 h the whole day of raw samples. 20 runs with a day each: 2.3 s CPU per 24 h look, 1.9 s per 30 d look. | c41a246d: sample lines before the window (or before the coarser tier's end) are skipped by their `{"t":N,` prefix without JSON parsing; events are still read for the whole window. Same results; 24 h 0.6 s, 30 d 0.16 s. Test `test_a_window_parses_only_its_own_samples_and_the_finer_tiers_tail`. |
| 3 | M | `projects/sandbox-ai-app/sandbox_ai_app/approvals_sections.py:53` (`why_text`), `ui.py:11211` (`questions_refused_without_window`), `i18n.py` | Approvals rework leftover: the Blocked list's "Not answered in time" explanation still said (EN and DE) "It is refused for 2 minutes, then asked again" - since the rework the try fails and the question stays open, its answer counts for the next try (`approvals.blocked_why` says so). The quit question said "each is refused after 60 seconds". | 3ca419ca: both texts say what happens, in English and German. Test `test_the_blocked_words_and_the_quit_question_say_an_unanswered_question_stays_open`. |
| 4 | M | `sandbox/hostkits.py:81` (`toml_loads`), `config_parts`, `toml_description`, `command_parts` | `kits host-list` on Python 3.9/3.10 (macOS's `python3`): `import tomllib` failed and Codex's valid `config.toml` was listed as "could not be read (not valid TOML)"; Qwen's and Gemini's TOML commands could not be imported. (The py39 suite test flagged it.) | 3a188a6b: `tomllib`, else the `tomli` package; without either the listing says "reading TOML needs Python 3.11 or later, or the tomli package". Test `test_toml_reads_without_tomllib_use_tomli_or_say_why`. |
| 5 | L | `sandbox/approvals.py:2253` (`mark_changed`), `:2497` (`decide_again`) | `approvals allow ID --scope always` on an answered lookup or connection kept it for the project, but the lists (`approvals list --state allowed`, the app's and web's Allowed tab) showed scope "run". | d412bc41: the scope given is recorded. Test `test_an_answered_question_allowed_always_is_listed_as_always`. |
| 6 | L | `sandbox/web/app.js:4851` (`loadHistory`) | Web System monitor: 7 days, then 30 days chosen; the slower 7 days' answer arrived last and was drawn under the caption "the last 30 days". | a17dba28: only the window chosen last is drawn. Test (node) `test_a_late_answer_for_an_earlier_window_does_not_replace_the_chosen_one`. |
| 7 | L | `projects/sandbox-ai-app/sandbox_ai_app/monitor_page.py:420` (`show_data`) | Every look (10 s) rebuilt the tree with `expandAll()`: a project the human folded opened again; with a selected run that ended, the chart kept its history. | 9cfc3872: folded projects stay folded, the scroll position is kept, a vanished selection shows the host's history. Test `test_many_runs_keep_the_scroll_position_and_the_open_projects_across_looks`. |
| 8 | L | `sandbox/monitor.py:1659` (`top_rows`), `monitor_page.py` | With `monitor_interval 30` the page said "sampled every 10 s". | f5e59d4e: `top --json` rows carry their recorder's interval; the page says it. Tests `test_top_rows_say_each_runs_own_interval`, `test_the_status_says_the_interval_the_runs_are_sampled_at`. |
| 8b | L | `sandbox/web/app.js` (`pages.monitor` hint, `monitorSeconds`), `sandbox/web/i18n.js` | The web page's hint said "recorded every 10 seconds" whatever `monitor_interval` is. | ba0d173f: it says the rows' interval (EN, DE). Test (node) `test_the_hint_says_the_interval_the_runs_are_sampled_at`. |
| 9 | S | `docs/video/storyboard.py`, `capture_app.py`, `pronunciation.py`, `sandbox_ai_app/videos.py` | The System monitor page had no video in the series: `test_video_storyboard` (core) and `test_video_player` (app) red. | 3a188a6b: a two-scene video (EN, DE), the capture list, `PAGE_TITLES`, lexicon terms (CPU, Limits, top, stats, usage). |
| 10 | S | `sandbox/monitor.py` (`stats usage --by/--json`), `sandbox/hostkits.py:65` (`module`), `sandbox/oauth_relay.py:584` (`main`) | Suite: two options without help; the lazy loader published a module before it ran; the catcher's internal arguments did not tolerate deep JSON (`RecursionError`) nor a missing `--host` value. | 3a188a6b. |
| 11 | S | `pages.py:501`, `labels.py`, `monitor_page.py:631-633` | App suite: the approvals' new state tabs were a plain `QTabBar` (the app's one tab row is `ArrowTabBar`), `--ask-timeout` had no form words (German start form), the monitor's notices escaped line breaks (`printable` instead of `printable_text`); tests still expected the nav without the System monitor and the software-analysis preset without jadx/CFR. | 3ca419ca. |
| 13 | L | `sandbox/runtime.py` (`mcp_bridge`, new `connect_long`) | The MCP bridge connected to the server's socket by its whole name, while the server binds it with `bind_long` (through `/proc/self/fd/N` when the name is longer than a socket allows): under a long HOME/state path the bridge said "MCP server socket unavailable: ... AF_UNIX path too long" for a server that was there (the egress e2e tests showed it under a longer `--basetemp`). In a sandbox the path is short (`MCP_AGENT_DIR`); on the host it is not. | 16a50c86: `connect_long`, contract `test_connect_long` (happy/sad/edge); the two egress e2e tests pass under a long basetemp. |
| 12 | S/L | `tests/test_secrets_primary_password.py` | Two socket tests failed with the brief's `--basetemp /root/tt-r24X/st` (and any longer one): `tmp_path/run/sandbox-ai/secrets/agent-….sock` passed the 100-byte socket limit, so the store rightly fell back to TMPDIR. | c19ec095: those tests use a short `XDG_RUNTIME_DIR`/`TMPDIR` in /tmp (removed after). The product is right. |

### Open (not changed, why)

- **L** `sandbox/monitor.py` `Recorder.stop`: joins its thread up to `interval + 5` s. A tick that is inside a
  `podman stats` call (macOS / `--podman-url`, `timeout=30`) delays the launcher's end by that call. Rare and bounded.
- **L** `sandbox/monitor.py` `tree_counters` (a Bubblewrap run without a scope of its own): the CPU of children that
  ended is lost (only the root's waited-for children count), so a sample after many short processes undercounts
  (clamped at 0, never negative). An approximation by design; runs with limits use their scope.
- **L (design)** an MCP server idle for 30 min with a gigabyte gets the "idle" warning with "stop" - stopping it breaks
  the agent that uses it. Servers are idle between calls by nature; the warning could be left out for `kind: mcp`.
- **Not flaky - the length of `--basetemp`**: `test_mcp_egress_e2e.py` (2, fixed by #13),
  `test_agent_sandbox_contracts.py::test_sandbox_outlived[happy]` (its fake control socket; 1588e561 binds it with
  `bind_long`) and `test_deep_round_3_097.py::test_bridge_across_hosts_suspend_resume_flood...` failed only under the
  longer basetemps of the parallel shards (`/root/tt-r24B/run2-shard.ac/st`) and pass under the brief's
  `/root/tt-r24B/st`: sockets below tmp_path pass the ~100-byte limit. The final runs use basetemps as short as the
  brief's. The bridge test's own socket paths were not changed. The app's
  `test_responsive.py::test_mcp_add_runs_in_the_background` failed once in the first run and passed in every run since.

## 2. Journeys tried (and found sound)

- `top`, `top --json`, `top --watch 1 | head` (no traceback on a closed pipe), `top --sort io --project /nonexistent`,
  `stats usage` (empty, bad `--since`), the collector's cost and bounded files (a day of samples written through
  `Series`, trimmed as it should), `stats usage` timing for 1 h / 24 h / 7 d / 30 d over 20 runs.
- `llm profiles MODEL [--json]`, the traffic light's cost (240 models in 6 ms: fine on the GUI thread).
- `kits host-list|host-show|host-import` on a made-up HOME: a symlink to `/etc/passwd` refused, a skill/command/subagent
  imported into a new kit, a second import of the same refused with what to do.
- `approvals` CLI help and the list/allow/deny/ask-again paths with a timed-out question (unit level), the app's
  Approvals page with tabs and Undo, the browser-callback relay tests end to end (59).
- `uninstall --dry-run` from a checkout (says what to do, removes nothing), `list --all-users` on a machine without
  other users.
- Upgrades: a 0.1.97 sandbox's refusals (`no answer…`) are listed as timed out; runs without a live record show `-`
  in the app and are measured by `top`; an older host's `top` is said ("does not have the system monitor yet").

## 3. Test results

Baseline (main at 167ee6e9, before any change): core 11 failed (option help x2, video storyboard, deep-JSON
tolerance, lazy loader, Python 3.9 tomllib, secrets sockets x2, MCP egress e2e x2, sandbox_outlived - the last five
from the basetemp's length), app 7 failed (error lines, tab rows, start form German, sai presets, nav list, video
button; test_responsive once).

Final, branch at 1588e561 for the core (4 shards, basetemps `/root/tt-r24B/s{aa..ad}`, as short as the brief's
`/root/tt-r24B/st`): **17897 passed, 337 skipped, 120 subtests passed, 0 failed** (4149 + 3174 + 4475 + 6099).

App (offscreen, `SANDBOX_AI_HOME` = this worktree), all 258 files in 13 chunks of 20 (3 at a time): **3760 passed,
20 skipped**, 1 failed in the last chunk under load - `test_stop_all.py::test_stop_all_asks_with_the_list_then_stops_here_and_on_the_hosts`
(the page's re-read after Stop all, due a moment later, emptied the hosts' starts the test went on with; 77f46a5f
holds it) - then `test_stop_all.py` 2 passed (3 runs). The chunk with `test_ui.py` was run again after the quit
question's new words (7310a4ec): 505 passed, 2 skipped (that one failure aside).
