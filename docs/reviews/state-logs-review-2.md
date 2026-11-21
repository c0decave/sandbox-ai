# State and session logs: review 2 of 3 (2026-11-16)

Scope: the same as [review 1](state-logs-review-1.md) - everything sandbox-ai writes per run and per project under
`XDG_STATE_HOME/sandbox-ai` (and the data, cache and run directories) - starting from `main` at 692de0e5 (review 1
merged). Review 1's conclusions were not taken on trust: its measurements were repeated with other data, its code was
read for bugs of its own (rotation, references, budgets, the age limit, privacy), every reader was checked against the
new, the old and mixed formats, and then review 1's open list was done.

Branch: `review/state-logs-2`. Measurements: scripts outside the repository (deleted afterwards with their data, all
below 1 GB):

- `fake_anthropic.py`: a fake Anthropic API driving the **real Claude Code 2.1.325** (`claude -p`, telemetry and
  non-essential traffic off, `ANTHROPIC_BASE_URL` on 127.0.0.1) through a 40-round tool loop (Read of the
  repository's files); it saved the 41 request bodies as Claude Code sent them.
- `gen_conn.py`: a busy connection log written through the real `ProxyServer.log` and `body_record` from those
  bodies: 25 conversations x 41 model requests, a download every 13 requests (3 MB, binary), a telemetry post every
  10, a Git fetch every 25 (1300 records) - with round 1's code and with it switched off.
- `stress_rot.py`: 8 threads writing through `ProxyServer.log` (30 bodies of 8 KB, repeated at random), a ninth
  appending taint events as `Taint.append` does, and `rotate_held_log` running whenever the log passed 200 KB.
- `measure_files.py`: the real `file_logger` watching `sandbox/`, `tests/` and `/work/.venv/lib` for 60 s while
  pytest, `git status` and a recursive grep ran in a loop (227-231 rounds), once with `file_reads files`, once
  `folders`.
- `measure_traffic.py`: the 41 real Claude Code bodies as 10 conversations (410 requests) through the real
  `LlmTraffic` (the proxy's side) and `LlmTrafficFile` (the supervisor's), whole records and deltas.
- A state directory with an ended run's old-format connection log (89 MB), a new-format one (12.5 MB) and a raw part
  of `files.jsonl`: `sandbox-ai gc --usage --json`, then `sandbox-ai gc`, the bytes on disk before and after.

## 1. Findings

Severity: **H** high (records lost, unbounded or hidden growth, privacy), **M** medium, **L** low. **[R1]** marks a bug
in review 1's own work; the others were there before it.

1. **H - The LLM traffic log lost most of a conversation to its own redaction.** `LlmTraffic.line` redacted the
   serialized line as text. `SECRET_TEXT`'s patterns for `--password...=VALUE` and `password=VALUE` take any
   character but a quote and white space - also the backslash of an escaped quote: a file the agent read with
   `"--password-store=basic"` in it became `\"--password-store=[redacted]",` in the line, which is no JSON any more.
   Every reader skips such a line as damaged - and every later request of the conversation carries that file. In the
   measured Claude Code run (this repository's `browser_runtime.py` among the files read) **320 of 410 records were
   unreadable**. The same pass redacted the record's own `"session"` (the start's name) as a secret's key, so a run's
   session was never shown. Fixed: each string is redacted on its own (`runtime.llm_redacted`: `redact_text`, as the
   connection log redacts a body, and a string under a key named like a secret's as `[redacted]`), the line is then
   built from them; 410 of 410 readable.
2. **H - The per-folder caps deleted a running run's log, which then grew unseen.** `rotate_connection_logs` (at
   every start that logs, and in gc) deletes the oldest connection logs of the folder over 1 GiB, protected only by
   "written within the hour". A run that started earlier and was idle for an hour lost its older parts - and its
   live log, which the proxy holds open: the proxy wrote on into the deleted file, no reader saw it, and the
   run's own rotation (`rotate_logs` stats the name) never found it again - growing on disk without bound until the
   run ended. Review 1 listed the first half (its finding 9, "M"). `rotate_traffic_logs` did the same to the LLM traffic
   log. Fixed: neither counts nor deletes the files of a registered run (`live_run_log`, `live_runs`); their own
   in-run limits bound them. End to end through `./sandbox-ai` with a registered idle run and 2.6 GB of (sparse) logs.
3. **M - A taint event written while the connection log rotated was lost.** `Taint.append` writes into the run's
   connection log - held open by the proxy and rotated by `rotate_held_log`, which copies it and empties it under the
   lock the proxy writes under - with a plain `open(..., "a")`, without that lock. In the stress run **70 of 300
   taint events** fell between the last copy and the emptying (17 rotations); the proxy's own 3200 records all
   survived. A taint event is the record that the run became tainted. Fixed (`append_record` under the lock); a
   deterministic test injects the event at the moment of the emptying.
4. **M [R1] - The age limit's grace started in the background, said to nobody.** `expire_logs` wrote its "noticed"
   stamp in every pass, the daily pass included - which runs in the background after a start and prints nothing. 14
   days later the logs went, and the only trace was a line in `launcher.log`. `keep_days`' default expiry, from
   which review 1 copied the design, deliberately does not start its grace there. Fixed as for `keep_days`: the daily
   pass only records what it found; the grace starts when gc, `gc --usage`, the app or a start (once a day:
   `log_days_notice`) said it; a round-1 stamp without "said" waits again.
5. **M [R1] - The summed reads made the file statistics 120 times larger.** Review 1's readers counted a folder's
   record by its `"reads"` (every close of a file); the per-file log had counted each file once per 30 s. Same 60 s
   loop: 2734 read events with `file_reads files`, **330716** with `folders` - `sandbox-ai logs`, the Statistics page
   and `report` jumped by two orders of magnitude, and the busiest folders were those read most often, not those with
   most files. Fixed: a summed record counts its `"files"` (4095 in the same loop).
6. **M - `mcp-calls.jsonl` of a project grew without bound during a run** (review 1's finding 7, open). Fixed:
   `McpRelays.stream()` is a `RecordFile` - opened per record (whole lines; two threads printing never mix), so
   `mcp-calls.jsonl` joins `ROTATED_LOGS` and rotates in-run like `tools.jsonl`.
7. **M - The LLM traffic log grew with the square of a conversation** (review 1's finding 8, open) and its rotated
   parts stayed uncompressed until gc after the run. Fixed: deltas (section 3) and parts compressed in-run.
8. **M - `gc --usage` overstated what compressing frees, and missed some.** It assumed 4x for everything. Measured on
   one state directory: estimate 76.5 MB, freed 57.7 MB (connection logs shrink 2.4x with downloads' digests and
   streamed answers in them). It also counted shared logs of busy folders as splittable (gc never splits those), did
   not count raw older parts (`NAME.TIME`, which gc does compress) and counted logs that `log_days` removes twice.
   Fixed: per kind, measured on a 1 MiB sample of the kind's biggest file (`statedirs.compression_ratio`): estimate
   59.1 MB, freed 57.7 MB; the app's clean-up overview says the same.
9. **L - A line cut by a killed writer swallowed the next record.** A supervisor killed in the middle of a record
   (the OOM killer) leaves a line without its newline; the next run's first record of that shared log joined it and
   both were lost to every reader. Fixed in `append_locked`, `log_event`, `tools.jsonl`, `activity.append`: a log that
   does not end in a newline gets one first (review 1's finding 12).
10. **L - A record written into a shared log just renamed away could be lost.** `rotate_shared_log` renames
   `files.jsonl` and compresses the renamed file; a writer that opened it just before the rename and wrote after the
   compression's last read lost its record with the uncompressed file. Fixed: after a 0.2 s grace what the file got
   meanwhile is appended to the part as a second gzip member.
11. **L - `sessions/ID/supervisor.log` was never trimmed** (review 1's finding 11). Fixed: over 8 MiB at a start or
   resume it keeps its newest 4 MiB, with a first line that says how much was removed.
12. **L [R1] - Reads beyond the folders a window tells apart lost their folder.** With 100,000 folders read in one
   window (a `node_modules` tree), 95,904 reads were one record without a folder. Fixed: summed below their first two
   folders (`/work/node_modules`), in at most 64 records. Memory stays at 2.3 MB for such a window.
13. **L - The app's Connections page showed a reference instead of the body** (review 1's open item). Fixed: a body
   logged as `"same_as"` is shown in full - from the records read, else found in that part of the log in the
   background - with a note naming the connection it was logged with.
14. **L - The approvals' "blocked" list reads only the live part of a run's connection log**
   (`approvals.py`: `connections-*-RUN.jsonl`): refusals in a rotated part (`.N.gz`) or in an ended run's log that gc
   compressed are not listed. Open (round 3).
15. **L - `homepack.meta` did not tolerate deep nesting** (the stability guard `test_every_json_read_that_tolerates_
   damage_tolerates_deep_nesting_too` failed on `main`). Fixed in passing.

## 2. Review 1 re-verified

| Review 1 said | Verified how | Verdict |
|---|---|---|
| A model request's first 64 KiB repeat request after request (finding 2) | Real Claude Code 2.1.325: its body is `model, messages, system, tools, metadata, max_tokens, thinking, context_management, stream` - the **messages come first**. The first 64 KiB of requests 5-41 were identical (5 distinct prefixes in 41); requests 1-4 differed (their messages were shorter than 64 KiB) | Holds - from about the fifth request of every conversation (a sub-agent's too) on, and it is the conversation's start, not the system prompt and tools, that repeats |
| Connection log 98 -> 21 MB raw, 35 -> 3.7 MB compressed | `gen_conn.py` with the real bodies: **89.1 -> 12.5 MB raw, 39.0 -> 5.3 MB gzip-3** (7.1x / 7.3x); 125 of 1025 model requests were written whole (the first 5 of each conversation) | Holds |
| References are lossless; every part stands alone; the reference is built under the lock | `stress_rot.py`: 3200 of 3200 records, 18 parts, every `same_as` resolved in its own part; the code: `append_locked` takes the size and builds the line under the same `flock` the rotation empties under; a size below the last one forgets the bodies | Holds for the proxy; the second writer (taint events) lost records - finding 3. The size test would miss a rotation only if another writer appended more than the last size (128 MiB) before the proxy's next record: the taint writer cannot |
| A reference to a part gc deleted | References never leave their part; gc and the caps delete whole parts | Cannot happen |
| Secrets in bodies redacted the same way | `body_record` still redacts the raw body (unchanged); a reference holds connection, time and a 128-bit digest; `binary_answer` digests the redacted bytes | Holds for the connection log; the LLM traffic log's line redaction was broken before review 1 (finding 1) |
| files.jsonl 543 -> 36 MB/day | `measure_files.py`: **569 -> 30 MB/day** (395 KB -> 20.8 KB in 60 s) | Holds; the counting of the new records was wrong (finding 5) |
| Readers stop at their budget | Read `logs.iter_lines`: a file the budget cut sets it to 0 (plain and compressed); exactly the budget left (a file that fits) is not "cut"; a part beyond it is only counted (gzip's trailer: the last member - a part with a second member, finding 10, is undercounted in "older lines skipped", harmless) | Holds |
| `log_days` never removes the log a running run writes | `old_logs`: a registered run's live file is excluded, its numbered parts and every older part go by their age; activity is never imported again from transcripts (the collector starts at their end), so a removed `activity.jsonl` stays removed | Holds; the grace was started silently (finding 4) |
| `FileReads` is bounded | 100,000 folders in a window: 2.3 MB, 0.66 s, 2 records | Holds; the reads beyond lost their folder (finding 12) |
| SQLite rejected: the content was the problem | Every gain here came from what is written (references, deltas, summed reads), the losses from how lines were made (redaction, unlocked writers, caps): none would have been avoided by a database. The slowest reader measured: reading 410 LLM requests (163 MB) took 1.23 s, 0.09 s with deltas; `tail` 0.04 / 0.01 s | Agree; still no reader that needs an index |

Where I disagree with review 1:

- **"The proxy is the connection log's writer."** It is one of two: `Taint.append` writes there too, and it did not
  take the lock that makes the rotation lossless (finding 3).
- **"Without log_days ... 14 days after the first gc, gc --usage or daily pass that found such logs."** The daily
  pass must not start a grace nobody sees (finding 4); `keep_days` already got this right.
- **"logs and report count `reads`."** They must count files, or the numbers change meaning between two versions of
  the same log (finding 5).
- **Finding 9 is not "M"**: the cap could delete the live log of a running run, which then grows unseen (finding 2).
- **Round 1's LLM traffic numbers** ("quadratic", 320 MiB per run) are right, but a measurement of the log's
  readability would have found finding 1: most of a real conversation was not readable at all.

Kept starts and `log_days`: a kept start keeps its HOME for 90 days (`keep_days`), its logs for 30 (`log_days`).
Nothing a resume or a kept start needs is in a log (the timeline is not rebuilt from transcripts, approvals and canary
plans live elsewhere), so this is consistent: after 30 days `sandbox-ai activity`/`report` of that start show nothing
older. Said in the README. A `--log-dir` outside the state directory stays yours (in-run limits only), as before.

## 3. Design changes

**The LLM traffic log as deltas.** A record that continues one of the last 16 records written leaves out what that
one holds: the first `kept` messages (or Responses `input` items) it begins with, and each other field of 256 bytes or
more that is the same (`system`, `tools`, `instructions`) - `"delta": {"of": N, "key", "kept", "same", "digests",
"order"}`; the record's digests likewise. The proxy remembers only digests (16 bytes per message) of the last 16
records. A whole record at least every 64 requests of a chain and for every conversation after each 16 MiB written,
so each 64 MiB part holds whole records early on; a record cut to its limit is never a base, nor one that was not
written. `llm_traffic.Expander` puts them together for every reader - `read_records`, `tail_records` (which reads back
as far as the records it returns need, and no further), `--follow`, `report --llm-traffic`, the bridge's `watch_llm` -
keeping whole records as their lines until a delta needs one (a log of whole records costs no more memory than
before). A request whose start was removed keeps what was new in it, with a first item that says so and
`"delta": {"missing": true}`. Old logs (whole records) read as before; an older reader shows a delta's new messages
only - what its default view shows anyway. `--grep` matches a request's line: with deltas, the request where the text
is new (and every answer that holds it), no longer every later request of the conversation. Rotated parts are
compressed in a thread of the supervisor (`.N.gz`), never moved while that runs.

Why not a database (the same question as in review 1): the deltas are what a content-addressed store would have
done for the conversations, in a file the proxy appends to across the virtiofs boundary and that every part of which
stands (nearly) alone; the readers stay streaming.

| Data kind | Change in review 2 | Why (measured) |
|---|---|---|
| LLM traffic log | deltas; parts compressed in-run; strings redacted one by one | 163 -> 22 MB raw, 58.6 -> 6.2 MB gzip for 410 real requests; 90 -> 410 readable |
| connection log | taint events under the log's lock; app shows referenced bodies; live runs never capped | 70 of 300 taint events lost; a live log deletable |
| `files.jsonl` | summed reads count their files; reads beyond 4096 folders keep their top folder | statistics 120x off; 95,904 reads without a folder |
| `mcp-calls.jsonl` (project) | rotated in-run (`RecordFile`) | unbounded during a run |
| every shared log | a cut line ended before the next record; a late record of a rotated log kept | records lost after a crash or a rotation |
| `sessions/ID/supervisor.log` | newest 4 MiB kept once over 8 MiB, at a start or resume | grew with every resume |
| `log_days` | the grace starts only when shown (gc, `gc --usage`, app, a start once a day) | removals said to nobody |
| `gc --usage` | compression measured per kind on a sample | 33 % over before, 2.4 % now |

## 4. Before / after

| What | Before (main 692de0e5) | After | Source |
|---|---|---|---|
| LLM traffic log, 10 conversations x 41 real Claude Code requests | 163 MB raw during the run (58.6 MB gzip after gc), **90 of 410 records readable** | 22 MB raw (6.2 MB gzip; rotated parts gzip in-run), 410 of 410 | `measure_traffic.py` |
| ... writing them / reading all / the last 20 | 12.3 s / 1.23 s / 0.04 s | 5.5 s / 0.09 s / 0.01 s | same (only what is new is redacted) |
| connection log, 1300 records (review 1's change re-measured) | 89.1 MB, 39.0 MB gzip | 12.5 MB, 5.3 MB gzip | `gen_conn.py` |
| taint events during 17 rotations | 70 of 300 lost | none lost (deterministic test) | `stress_rot.py`, test |
| `files.jsonl`, pytest/git/grep loop (review 1's change re-measured) | 569 MB/day | 30 MB/day | `measure_files.py` |
| file statistics of that loop (read events per minute) | 2734 (per file) vs 330716 (summed, round 1) | 2734 vs 4095 | same |
| `gc --usage` "compressing frees" vs freed | 76.5 vs 57.7 MB | 59.1 vs 57.7 MB | `gc --usage --json`, `gc` |

A run that lasts weeks now keeps per project: the live files (128 MiB each, rotated; the LLM traffic log 64 MiB),
compressed parts of the last 30 days (`log_days`), at most 1 GiB of parts per log and per run's connection log, four
compressed LLM traffic parts per run - and nothing of it can be deleted under a running run or grow unseen.

## 5. For review round 3

- A live check on the user's host: `gc --usage` before and after a week of a busy project with this branch; the
  LLM traffic log of a real `--llm` run (local model, Responses API with `input` items) - how often a delta is
  written, how long chains get, whether a model's own context editing (Claude Code's `context_management`, compaction)
  breaks the prefix (it then writes whole records: measure how often).
- The approvals' "blocked" list over rotated and compressed parts (finding 14).
- `redact_text` on JSON-escaped text elsewhere: MCP call arguments (`McpGateway.arguments`) and the proxy's question
  bodies redact `json.dumps(...)` as text - harmless today (they stay strings), but the same patterns eat a `\` and
  miss `password=` after a JSON `\n` (`\npassword=x` has no word boundary): a redaction that knows JSON strings.
- The canary log reads connection logs oldest first with a 256 MiB budget per run: with references, a canary-bearing
  request whose body is a reference is still found (the record carries `"canary"`), but its offsets (`activity_id`)
  depend on the part - check the timeline links after a rotation.
- Readers of `activity.jsonl` over months (the app's Activity page): the only reader left that might want an index;
  measure it on a 30-day folder before deciding.
- `rotate_held_log` and a crash between `os.replace` of the part and `ftruncate`: the part's records are then in two
  places (duplicates, no loss); a reader-side de-duplication or a marker line would make it exact.
