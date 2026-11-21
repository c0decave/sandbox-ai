# Leaks and performance, review 1: the desktop app

Branch `review/leaks-perf-1` (based on `merge/cloud-models`, 0.1.115). Scope: `projects/sandbox-ai-app`, with the
focus of the brief: widgets and windows that stay alive after closing, module-level caches, timers on hidden pages,
threads and processes, file descriptors, a long session, and performance (startup, idle CPU, GUI-thread blocking,
large lists, theme repaint). Every number below was measured offscreen (`QT_QPA_PLATFORM=offscreen`) on the shared
20-core machine while other suites ran, so absolute times are noisy; before/after pairs were taken back to back.

## How it was measured

- **Use cycles** (a scratch harness, not committed): the real `MainWindow`, every page shown, themes switched,
  Settings and the policy dialog, the log window, Help, the preset gallery, About, the navigation folded and compact,
  every panel toggled, an area tour - N cycles; after each cycle `ThreadCall.wait_all`, deferred deletes,
  `gc.collect()`, then Python objects by type, `QApplication.allWidgets()`, the window's QObject children, active
  timers, `/proc/self/fd`, threads and RSS after `malloc_trim`. Then each use alone (bisecting the growth), and the
  objects that grew traced to where they were allocated (`tracemalloc.get_object_traceback`) and to their referrers.
- **Idle**: every page shown and left alone 5-6 s: process CPU, the longest `processEvents()` (a GUI-thread stall),
  timer events by receiver, and the active repeating timers with their intervals and owners.
- **Large lists**: 1,000 sessions (250 running, a tab each), 10,000 answered questions, 100,000 log entries, a folder
  of 50,000 files, a Board of 100 parts - with cProfile.
- **Startup**: the real entry point `main.main()` with an isolated home, to the window's first paint and to idle
  (every page made, no background call left).
- **Soak**: `tests/soak_app.py` (the existing 1-minute-per-row soak with terminals, the Board, questions, toasts,
  themes, dialogs), 8 minutes before the fixes and 6 minutes after.

## Findings

| # | Severity | Where | What | Measured before → after | Fix |
|---|----------|-------|------|-------------------------|-----|
| 1 | High | `credential_tab.py:149-169` (`header_height`, `eventFilter`) | The Credentials page laid itself out for ever while shown: a header's LayoutRequest evened the headers out, whose measure (`layout.invalidate()`) asked for the next LayoutRequest. | Page shown, idle: **~40 % of a core**, ~280 LayoutRequests and ~500 paints a second (471 layouts, 858 paints in 1.5 s) → **1.3-1.5 %**, 0 layouts | d4c94ecf |
| 2 | Medium | `ui.py` `sync_tabs` / `ProjectTabs.decorate` | Every look of the Sandboxes page (every few seconds) set every project/session tab's text, icon and tip again; each set lays out the whole tab row (O(tabs²) size hints). | 1,000 sessions, 250 tabs: **1.6-2.2 s of the GUI thread per look** (125,000 `tabSizeHint` calls) → **45 ms**; one session changed 2.2-2.6 s → 104 ms | adc611b1 |
| 3 | Medium | `ui.py` `start_area_tour`, `start_task_tour`, `start_tour` | Qt's `QDockWidget.setWidget` hides the widget it replaces and never deletes it: every tour started stayed in the guide dock (+10 widgets, +8 per harness cycle); a task tour replaced that way kept checking its step every 500 ms for good (some checks read files); a guided tour after an area tour made a second dock. | widgets +8 per cycle (4,263 → 4,303 in 5 cycles) → flat (4,255 for 6 cycles); 16 panels left in the test → 1 | 49e75619 |
| 4 | Medium | `traffic_page.py:319` `ChatWindow` | LLM traffic: every request opened in a window of its own stayed, closed, a hidden child of the page with its whole conversation's HTML until the app ended. | 5 opened and closed: 5 left → 0 | c8e8125e |
| 5 | Low | `monitor_page.py` `show_warnings` (`self.seen`) | The System monitor's set of warnings it announced grew for the whole session (a warning that comes and goes adds one each time). | 3,000 flapping warnings: 3,001 kept → ≤ 1,002 (SEEN_KEPT), a warning still listed never announced twice | e56d7ae2 |
| 6 | Low | `logview.py` `LogPanel.add` | A burst of warnings while the log is folded set the panel's title once per warning. | 2,000 warnings: 55 ms → 19 ms | 7e8b80a7 |
| 7 | Low | Settings dialog (native) | Opening and closing Settings leaves ~50-75 KB of native memory each time, Python objects flat, also with the log at its cap (the log alone is flat there). A plain dialog of 300 widgets leaves 2-5 KB. | 30-40 opens: +2.3-2.9 MB, linear | open: needs a native heap profiler (heaptrack/valgrind are not installed here); ~5 MB after 100 opens |
| 8 | Low | PySide wrappers | Each theme change leaves 2 Python wrappers of deleted `QCommonStyle` proxies (`dock_fold.py:140` `self.style()` once the sheet changed), each dialog veil one `QAction` wrapper (`widgets.py:817` `findChildren(QAction)`); the C++ objects are gone. | ~100 bytes each | open: PySide's own bookkeeping, negligible |
| 9 | Info | `logview.py` `setMaximumBlockCount(MAX_ENTRIES * 2)` | The log panel keeps up to 10,000 blocks (the hub 5,000 entries); a laid-out block costs ~1.7 KB native, so a full panel holds ~17 MB (two with the log window). Bounded: flat once at the cap (measured). Most of the soak's RSS growth in its first minutes is this filling up. | log alone at the cap: flat over 30 cycles | open: by design (multi-line entries); halving it would change what the panel shows |
| 10 | Info | `ProjectTabs.add` | 250 sessions that started between two looks open 250 tabs in one go: 5.6 s (each insert, text, icon, tip lays out the row). | | open: rare; the per-look cost (finding 2) was the one that repeats |
| 11 | Info | `pages.py:1326` `show_history` | 10,000 answered questions of running sandboxes: a new answer refills the whole history table. | 0.55 s per change (3 ms when nothing changed); ~55 ms at 1,000 | open: acceptable at realistic sizes; an incremental history needs keyed row updates |
| 12 | Info | `board_view.py` | A Board of 100 parts rebuilds its scene per step. | 73 ms per property, 109 ms per part added (2.9 s for 100 adds) | open: acceptable |
| 13 | Info | `theme.py:994` | A theme change re-polishes every widget (`QApplication.setStyleSheet`, Qt). | 0.4-0.85 s, of it the log re-drawn 85 ms with 5,000 entries | open: Qt's |

## Measured and fine

- **Startup** (real entry point, no intro/tour): window made and shown 0.65 s, **first paint 0.71-0.79 s**, every page
  made and idle 3.6 s (the lazy build by design: 1 s after the paint, a page at a time). Of the 0.7 s: imports 0.3 s,
  the style sheet 0.13 s, the window 0.26 s.
- **Idle CPU** (offscreen, every page): 1.3-2.0 % (including the measuring loop), worst GUI-thread stall 1-24 ms
  (Terminals 24 ms, Traffic 10 ms). Repeating timers while idle: the Sandboxes page 3 s (its look) and 60 s (other
  hosts; 10 s while shown), the Approvals page's Blocked list 5 s (a no-op unless on screen), one parentless 2 s
  timer (the cycle collector on the GUI thread), Connections 2 s, System monitor 10 s, LLM page 20 s - each only while
  its page is shown; the questions' poll (1 s in the app) and the window's heartbeat (5 s). In the tray: the same
  minus the pages' own.
- **No growth over use cycles** after fixes 1 and 3: widgets, the window's QObjects, active timers, fds (14-16),
  threads (4-8) and Python objects flat over 12 cycles of each use (themes +5 objects/cycle and ~50 KB RSS/cycle
  is finding 8 and allocator noise; pages, panels, tours, nav, About, Help, log window flat).
- **Large lists**: Files page, a folder of 50,000 files (listed up to `files.MAX_ENTRIES` 20,000) shown in 184 ms;
  100,000 log entries in bursts of 1,000: 3.0 s in all (30 µs each, app.log included), the filter typed 29 ms;
  Sandboxes page 1,000 sessions with nothing changed 45 ms (after fix 2).
- **Theme repaint**: a whole-window grab costs 5-21 ms in every one of the 20 themes; none stands out.
- **Module-level caches** (`board_icons`, `theme._glyphs`, `help_view._MARKS`, `preset_gallery._thumbs`,
  `name_fields._kept` with weak references, `logview.LINKS` 50, `applog._changes` 5,000, `host_choice`/`name_fields`
  hubs, `vnc_view._open` popped on `destroyed`, `login_browser`/`video_player` weak sets): keyed by bounded sets or
  pruned; none holds a window or a backend (`test_the_caches_keep_no_backend_alive` covers the backend).
- **Threads and processes**: `ThreadCall` workers are pooled and retire; `AsyncCommand` processes are deleted on
  their end; the soak's threads 6-9 and child processes 4-6 stay level.

## Soak

8 minutes before the fixes (with fixes 1 and 3 already in): RSS after trim 389 → 458 MB, Python 42 → 45 MB (flat from
minute 5, when the log reached its 5,000 entries), Python objects ~112,000 flat, widgets 4,256-4,318, fds 56-85
(terminal tabs come and go), the longest stall 0.5-1.0 s "after theme" (finding 13). The soak itself failed on its
own error-level notifications and its allows for made-up `.example` hosts; it now leaves those out (tests/soak_app.py,
not part of the suite). After all fixes, 6 minutes (passed): RSS after trim 392 → 444 MB, rising 16-17 MB a minute
while the log fills (minutes 1-4: the log's 5,000 entries and finding 9) and 1.9 MB a minute after (minutes 5-7:
439.6 → 441.4 → 443.5); Python 42.3 → 44.9 MB (flat from minute 4), Python objects 109,000 → 112,500 (flat from
minute 4), widgets 4,280 → 4,300, threads 9 → 6, fds 67 → 75 (terminal tabs), no error in the log. What still grows
after the log's cap is native and slow (findings 7 and 8 and the terminals' web views); an hour's soak would say
whether it levels off - not run here (the machine is shared; the brief asks for bounded soaks).

## Tests

- `tests/test_leaks_perf_1.py` (new): the Credentials page idles without layouts (< 10 LayoutRequests and < 40 paints in
  1.5 s; it was 471 and 858) and still evens its headers out when one grows; tours replaced go and stop checking, one
  guide dock; an unchanged look draws no tab again, one change draws one tab; the monitor's memory is bounded; a burst
  of warnings counts as unread once; a closed chat window goes.
- Adjusted: `test_logview.py` (unread is said once per burst: `[2]` instead of `[1, 2]`), `test_traffic_page.py` (the
  closed chat window is deleted).
