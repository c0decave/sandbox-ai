# State and session logs: review 1 of 3 (2026-11-16)

Scope: everything sandbox-ai writes per run and per project under `XDG_STATE_HOME/sandbox-ai` (and the data, cache,
config and run directories), who writes it, how fast it grows, what bounds it, who reads it and how. Trigger: on the
user's host one project's log folder reached 14 GB in four days (`files.jsonl` 8.3 GB, a connection log 5.7 GB,
`processes.jsonl` 310 MB, `activity.jsonl` 256 MiB) and the sessions 26 GB. This review starts from the state of
`main` at 1a828622 (0.1.98 with in-run rotation, gc compaction, `--logs-older-than`, the daily pass, `keep_days` 90).

Branch: `review/state-logs-1`. Measurements: small scripts outside the repository - `measure_files.py` (the real
`file_logger` and `process_logger` watching this worktree and `/work/.venv` while pytest, git and grep run in a
loop), `gen_connections.py` (a synthetic busy connection log made with the proxy's own `body_record`),
`through_proxy.py` (that log written through `ProxyServer.log`), `rd.py` (`logs.telemetry` over a folder) - as
described with each finding; the synthetic data was deleted afterwards.

## 1. Inventory

Per project log folder `state/sandbox-ai/logs/PROJECT-HASH/` (mode 0700; `--log-dir DIR` elsewhere):

| File | Writer | Record | Growth of a busy run (measured / host) | Bound (before this review) | Readers |
|---|---|---|---|---|---|
| `connections-TIME-RUN.jsonl` | proxy (`ProxyServer.log`, held open, `append_locked`) | every HTTP exchange: URL, headers, **request and answer bodies, first 64 KiB each** (text redacted, binary as base64); other connections, refusals | 87 KB per model request (68 KB of it the request body); host: 1.4 GB/day | in-run: 128 MiB live + 1 GiB `.N.gz` per run; per folder 1 GiB, applied at the next start and by gc | `logs`/Statistics, `report`, canary check (oldest first, 64 MiB budget), app Connections page (newest 16 MiB, record detail shows the bodies), approvals context |
| `llm-traffic-TIME-RUN.jsonl[.N]` | supervisor (`LlmTrafficFile`) from the proxy's pipe | one model request **with its whole conversation** (up to 4 MiB) and the answer; default only the `--llm` server | quadratic in a conversation's length | 64 MiB parts, 4 kept per run (uncompressed until gc), 2 GiB per folder at the next start | `llm traffic`, app LLM traffic page (1 GiB read budget) |
| `files.jsonl` | supervisor `file_logger` (inotify on workspaces, `/root`, `/tmp`, mounts; only with `--log-dir`) | 144 B: action + path | **547 MB/day** (pytest+git+grep loop, 98.8 % reads); host 2.1 GB/day at the old 2 s repeat | rotate at 128 MiB, 1 GiB `.TIME.gz` per log | activity (changes only, grouped), `logs` (counts), `report` (counts, newest 8 MiB) |
| `processes.jsonl` | supervisor `process_logger` | 167 B: pid, ppid, command (redacted, 1000 chars) | 95 MB/day in the same loop; host 78 MB/day | 128 MiB rotation, 1 GiB parts | `logs`, `report` (counts), activity |
| `dns.jsonl`, `egress-denied.jsonl` | supervisor | a forwarded lookup / a dropped destination (first time) | small (KB-MB/day) | 128 MiB rotation | `logs`, `report`, activity, safeguards |
| `activity.jsonl` | launcher (`activity.append`), derived from transcripts and the logs above | the timeline: prompts/replies/commands (2000 chars), file changes grouped, network events counted per minute | host 64 MB/day | `.TIME` at 256 MiB, compressed by gc | `activity`, app Activity, `report` |
| `tools.jsonl` | supervisor `HookServer.write` (opened per record) | a tool call with its decision and redacted arguments | ~1 KB per call | **none during a run** | `report`, safeguards, `logs` |
| `mcp-calls.jsonl` | supervisor `McpRelays` (held open, line-buffered) | an MCP call with arguments | ~1 KB per call | **none during a run** | `report`, safeguards |
| `safeguards.jsonl`, `canaries.jsonl`, `devices.jsonl`, `workspaces.jsonl`, `vpn.jsonl`, `memory.jsonl` | launcher / supervisor, per record | rare events | KB | gc split at 96 MiB, only in a folder no run uses | `report`, safeguards, app |

Elsewhere: `sessions/ID/` (`session.json`, `supervisor.log` appended at every start and resume, `home/` - the HOME,
covered by `gc --usage --detail`, `--caches`, `--pack` ...), `mcp/NAME/` (`supervisor.log`, `mcp-calls.jsonl`
rotated at 32 MiB, 256 MiB parts), `history/NAME.jsonl` (256 KiB per name), `launcher.log` (2 MiB rotation),
`web/audit.jsonl` (16 MiB rotation), `compact.json`, `expiry.json`; the run directory's
`safeguard-events.jsonl` (4 MiB cap, tmpfs); data: MCP root file systems (gc after a week unused), kits, videos.

## 2. Findings

Severity: **H** high (unbounded or GB-scale waste, privacy, user-visible cost), **M** medium, **L** low.

1. **H - `files.jsonl` is 99 % reads nobody shows.** `runtime.py` `INOTIFY_EVENTS` logs `IN_CLOSE_NOWRITE` as
   `read`, one record per file and 30 s window. Measured with the real `file_logger` on this worktree and
   `/work/.venv` while running pytest, git and grep in a loop (61 rounds in 180 s, `measure_files.py`): 7916
   records, 7824 of them reads (1.13 MB of 1.14 MB) = 380 KB/min = **547 MB/day**; changes alone were 92 records
   (13 KB). The only reader of individual events, the activity timeline (`activity.py` `FILE_ACTIONS`), drops
   reads; `logs.py` `summarize` and `report.py` only count them. A tree read in 30 s with more than
   `FILE_RECENT_LIMIT` (4096) files (node_modules, a big venv) defeats the repeat window entirely: every read of
   every file is logged again. Proposal: sum reads per folder and window at the source; a setting for the old
   behaviour. **Fixed** (below).
2. **H - The connection log repeats the same 64 KiB request body in every model request.** `ProxyServer.exchange`
   logs `body_record(captured.data, ..., limit)` for every request; a model request begins with the same system
   prompt and tools (or the same first messages) for a whole conversation, so its logged first 64 KiB are identical
   request after request. gzip's 32 KiB window cannot see the repetition: the synthetic log of a busy Claude Code
   agent (`gen_connections.py`: 1000 model requests built like Anthropic's, 80 downloads, 100 telemetry posts, 40
   Git fetches, made with the proxy's own `body_record`) is 98 MB, **2.8x** with gzip-3 (the code says "a quarter
   or less"); lzma-0 gets 8.4x at 30 MB/s. Proposal: write a body this part of the log already holds as a
   reference (digest, connection, time) - lossless. **Fixed.**
3. **H - Binary answers are logged as base64 that nothing reads.** A download's first 64 KiB become 87 KB of
   incompressible base64 per record (`body_record` -> `record["base64"]`); after (2) they are 70 % of the
   compressed log (8 of 11.9 MB). Nothing in sandbox-ai decodes them; a human sees base64 in the app's detail view.
   Proposal: size, SHA-256 of the logged bytes and the first 64 bytes (file type); `connection_bodies all` for the
   old way; uploads (binary request bodies) stay whole - what left the sandbox is the forensic part. **Fixed.**
4. **H - No age limit on any log; bodies with secrets stay for months.** Only size caps exist (1 GiB of compressed
   parts per log, 1 GiB connection logs and 2 GiB LLM traffic per folder), and those apply per folder and per kind:
   a busy project may keep 4 x 1.1 GiB of shared logs + 1.1 GiB per running run's connection log + 2 GiB LLM
   traffic + unbounded activity parts (see 6) - compressed, so 10-50 GB of records - and the conversations and
   request bodies (the agent's code, anything it read; `redact_text` only knows token formats) for as long as that
   takes to fill, months at the new rates. `keep_days` covers starts only; `--logs-older-than` removes whole
   folders only, by hand. Proposal: `log_days` (default 30, 0: never) applied by gc and the daily pass to every
   log file whose last write is older - never the file a running run writes; announced with a grace like
   `keep_days`. **Fixed.**
5. **H - Readers unpack compressed parts after their budget is spent.** `logs.iter_lines` passes
   `max(0, min(tail, budget["left"]))` to `jsonltail.compressed_tail`, which unpacks the whole file even to keep
   0 bytes; and a budget almost spent keeps a remainder smaller than one line, so it never reaches 0 and every
   older part is unpacked. `logs.telemetry` (the Statistics page, `sandbox-ai logs`) and `report` read every
   connection log of the folder. Measured: a folder with a 98 MB log and 24 compressed parts: **12.6 s** per
   Statistics refresh on `main`, 0.13 s without the parts. Proposal: a file the budget cut spends it; a compressed
   part with no budget left is only counted (gzip's trailer) and never unpacked. **Fixed: 2.1 s.**
6. **M - `activity.jsonl`'s older parts grow without end in a folder a run uses.** `activity.append` renames to
   `activity.jsonl.TIME`; `statedirs.compact` compresses those (ARCHIVE_RAW) but calls `drop_archives` only after a
   split - never in a folder a run logs into, which is the busy one. Host rate: 64 MB/day raw, ~15 MB/day of parts
   kept forever. **Fixed** (capped at `ARCHIVE_LIMIT` like the others).
7. **M - `tools.jsonl` and `mcp-calls.jsonl` of a project are never bounded while any run uses the folder.**
   Neither is in `runtime.ROTATED_LOGS`; gc's split skips busy folders; a project with a start that lives for weeks
   keeps them growing. `tools.jsonl` is opened per record (`HookServer.write`): **fixed** (rotated in-run).
   `mcp-calls.jsonl` is held open by `McpRelays.stream()`: needs reopening after a rotation - **open** (round 2).
8. **M - The LLM traffic log is quadratic and uncompressed during the run.** Each record carries the whole
   conversation (`LlmTraffic.record` "document", up to `--llm-traffic-limit` 4 MiB); a conversation of N requests
   writes O(N^2) bytes. Rotated parts `.1-.4` stay uncompressed until the run ends (320 MiB per run). Default is
   the `--llm` server only. Proposal: store each request as the delta to the previous request of its conversation
   (the reader computes exactly that delta for display already: `llm_traffic.py` "what is new since the previous
   request") with a full record every K requests and at each part's start; compress parts in-run. **Open**
   (round 2; the local-LLM work owns this code today).
9. **M - The per-folder connection-log cap deletes a running run's older parts.**
   `agent_sandbox.rotate_connection_logs` (a new start, gc's caps) removes the oldest files of the folder over
   1 GiB, sorted by run start time - the `.N.gz` parts of a run that started earlier and still runs go first; only
   the hour-old rule protects them. Documented ("the oldest are deleted when they exceed 1 GiB"), so a rotation
   rule, but another process's. Proposal: count a live run's parts against its own in-run limit only. **Open.**
10. **M - Size estimates in `gc --usage` assume 4x compression.** `would_free.compress.saves = bytes * 3 // 4`;
    measured gzip-3: connection logs 2.8x (before the fixes), `files.jsonl` 16x, `processes.jsonl` 8.8x. **Open**
    (estimate per kind).
11. **L - `sessions/ID/supervisor.log` is appended at every start and resume, never trimmed** (`sessions.py`
    `open(directory / "supervisor.log", "ab")`); it holds the supervisor's stderr including one line per rotation.
    Small in practice; a start that lives for months with resumes grows it. Proposal: keep its newest 4 MiB at a
    resume. **Open.**
12. **L - A crash in the middle of a record cuts a line that the next record is appended to.** `log_event` writes
    through a text stream; a killed supervisor can leave a line without its newline, and the next record joins it:
    both are lost to every reader (they skip damaged lines). Proposal: a writer that finds the file not ending in
    a newline writes one first. **Open.**
13. **L - `processes.jsonl` logs every short-lived process again** (a test loop: 1183 records in 3 min, the same few
    command lines). Kept as it is: it is the record of what ran, and compresses 8.8x. Optional: count repeats of
    the same command line within a window. **Open (low value).**
14. **L - gc's dry run and `gc --usage` cannot say what the daily pass will remove of the LLM traffic logs** (the
    2 GiB folder cap is applied by `rotate_traffic_logs` at a start). **Open.**

Checked and fine: log folder mode 0700; files 0600 (proxy, LLM traffic) or umask (supervisor logs inside the 0700
folder); the connection log's lossless held rotation (`compress_held` + `append_locked`); rotation races between two
runs of one project (`rotate_shared_log` returns None when the part of that second exists; writers open per record);
the daily pass is niced, once a day, 10 minutes at most; `launcher.log`, `history/`, `web/audit.jsonl`, the MCP
servers' own logs are bounded.

## 3. Design per data kind

Decided by the measurements above, not by taste. SQLite was considered for all of them and rejected for now:

- The writers are four processes in two places: the proxy and the supervisor run inside the sandbox (on macOS in
  Podman's VM, the log folder reached over virtiofs), the launcher and the app on the host. SQLite's WAL needs a
  shared-memory file and POSIX locks that are not safe over virtiofs/9p; without WAL every writer blocks every
  reader. Append-only JSON lines with `O_APPEND` (and `flock` for the held connection log) work across that
  boundary today.
- The size problem was the content, not the format: 99 % reads, the same 64 KiB body in every request, base64 of
  downloads. A database would have stored the same bytes (SQLite pages do not compress).
- The readers read tails and counts; none needs random access by key except "the records of run X" (`activity
  RUN`, `report --run`), which the per-run file names and the rotated parts' names already answer.
- An index is worth it for one reader: the app's Activity/Statistics over months of a project. If round 2/3 shows
  that cost, the right shape is a host-side SQLite **index** built from the JSON lines by the launcher (one
  writer, on the host, rebuildable, never the source of truth) - not a store the sandbox writes.

| Data kind | Decision | Why (measured) |
|---|---|---|
| `files.jsonl` | Keep JSONL + rotation; **reads summed per folder and 30 s window at the source** (`FileReads`), at most `FILE_READ_RECORDS` (32) read records per window - the deepest folders merged into their parents; setting `file_reads` (`folders` default, `files` as before, `off`) | 99 % of records were reads that no reader shows one by one; a recursive grep alone read 231 folders per window; 543 -> 36 MB/day in the measured loop |
| connection logs | Keep JSONL + held rotation; **a body this part already holds is written as a reference** (lossless, per part, so every part stands alone); **binary answers by size, SHA-256, first 64 bytes** (setting `connection_bodies`: `text` default, `all` as before) | 98 -> 21 MB raw, 35 -> 3.7 MB compressed (9.5x) for the synthetic busy log; gzip suffices after this (lzma would add 1.3x at half the speed) |
| all logs | **Age limit `log_days`** (default 30, 0: never) by gc and the daily pass, never the file a running run writes; 14 days' grace after the first pass that finds such logs | privacy (bodies, conversations) and a bound that holds for weeks-long runs; the size caps alone allow 10-50 GB per project |
| readers | **A budget-cut file spends the budget; compressed parts beyond it are never unpacked** | 12.6 s -> 2.1 s per Statistics refresh with 24 parts |
| `activity.jsonl` parts | Capped like every other log's parts (1 GiB) also in busy folders | unbounded before |
| `tools.jsonl` | Rotated in-run like `files.jsonl` | unbounded while a run lives |
| `processes.jsonl`, `dns.jsonl`, `egress-denied.jsonl` | Unchanged: JSONL + rotation + age limit | 8.8x compression, the forensic record of what ran |
| LLM traffic | Round 2: delta records against the previous request of a conversation + key records, parts compressed in-run | quadratic today; owned by the local-LLM work this week |
| `mcp-calls.jsonl` (project) | Round 2: reopen after rotation in `McpRelays` | held open |
| `sessions/ID/supervisor.log` | Round 2: trim at resume | small |

Old logs stay readable: the read summaries are new records (`"folder"`, `"reads"`); old per-file read records are
counted as before. A body reference (`"same_as": {"connection", "time", "digest"}`) is new; old records carry
their bodies. Every reader that counts accepts both.

## 4. What was implemented (round 1)

- `runtime.FileReads` (`FILE_READ_NAMES` 5, `FILE_READ_FOLDERS` 4096 and `FILE_READ_KNOWN` 16384 names per window -
  the supervisor's memory -, `FILE_READ_RECORDS` 32 records per window), `file_logger(..., reads=)`, `FILE_READS`, `start_loggers` passes `file_reads`;
  `logs.summarize` and `report` count `"reads"`; defaults key `file_reads`.
- `runtime.logged_once` (digest per part, `LOG_BODY_SEEN` 512 remembered, bodies of 4096 characters and more),
  `append_locked` builds the line under the file's lock with the file's size (a smaller size is a rotation: the new
  part remembers nothing), `ProxyServer.log` uses both and forgets on a failed write.
- `runtime.binary_answer`, `BINARY_HEAD`, `CONNECTION_BODIES`; proxy config `connection_bodies`; defaults key.
- `jsonltail.unpacked_size`; `logs.iter_lines` spends a budget that cut a file and never unpacks past it.
- `statedirs.compact` caps the parts of a shared log after compressing one (activity in busy folders).
- `runtime.ROTATED_LOGS` + `tools.jsonl`.
- `statedirs.old_logs`, `remove_old_logs`, `LOG_DAYS` 30, `LOG_DAYS_GRACE` 14, `log-days.json`;
  `starts.log_days_setting`, `expire_logs`; gc (plain gc, `--dry-run`, `--json` "log_days"), the daily pass
  (`compact.json` "old_logs", `launcher.log`), `gc --usage` (row and `would_free.log_days`); defaults key
  `log_days` (`check_log_days`).

## 5. Before / after (busy run, per day)

| Log | Before | After | Source |
|---|---|---|---|
| `files.jsonl` | 543 MB/day (377 KB/min, 5227 records in 120 s, 5192 reads); host 2.1 GB/day | 36 MB/day (25 KB/min, 240 records, 141 of them read summaries) - 15x | `measure_files.py 120 DIR files` vs `folders`, same loop (47/48 rounds) |
| connection log raw | host 1.4 GB/day | ~0.31 GB/day (4.6x) | synthetic log through `ProxyServer.log` |
| connection log compressed parts | ~510 MB/day (2.8x) | ~54 MB/day (9.5x of the old) | same |
| `processes.jsonl` | 78-95 MB/day (9-11 MB compressed) | unchanged | measured loop |
| `activity.jsonl` | 64 MB/day, parts kept forever in a busy folder | 64 MB/day, parts capped, 30 days | host |
| everything | kept until size caps (months) | at most 30 days (`log_days`) | |

A run that lasts weeks now keeps, per project: the live files (at most 128 MiB each, rotated), 30 days of
compressed parts at the rates above (connection log ~1.6 GB, files ~0.1 GB, processes ~0.3 GB, activity ~0.5 GB),
and nothing older.

## 6. For review round 2

- Check the new code paths: `FileReads` memory bound under a tree of 100k folders read in one window
  (`FILE_READ_FOLDERS`), the merge of the deepest folders (what a reader learns from a merged record; more than 32
  top-level folders stay separate), the flush timing when inotify is quiet (the select loop), what `logs`/`report`
  show for a mix of old per-file and new summed records, and a summary lost when the logger ends by an exception.
- `logged_once`: a reference across a rotation boundary when `rotate_held_log` runs between `fstat` and `write`
  (both under the same lock now - verify), the 512-digest memory against a real Claude Code log (do the first
  64 KiB repeat in practice?), the app's Connections detail with a `same_as` body (show "same as connection N").
- `log_days`: the interaction with `--log-dir` folders outside the state directory (not covered: only
  `state/logs/`), with `keep_days` (a kept start's logs older than 30 days go while the start stays), the grace
  stamp written by `gc --usage` and the daily pass (said? the start does not print it).
- Findings 7 (`mcp-calls.jsonl`), 8 (LLM traffic deltas), 9 (cap deleting a live run's parts), 10 (estimates), 11,
  12 - and a live check on the user's host: `gc --usage` before/after a week of a busy project.
