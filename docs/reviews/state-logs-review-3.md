# State and session logs: review 3 of 3 (2026-11-16)

Scope: the same as [review 1](state-logs-review-1.md) and [review 2](state-logs-review-2.md) - everything a run, the
proxy, the supervisor, the MCP relays, the LLM relay, approvals, canaries and the file and tool logs write under
`XDG_STATE_HOME/sandbox-ai`, how it is rotated, compressed, aged and read - starting from `main` at afdf199f (reviews
1 and 2 merged). Neither write-up was taken on trust: their central numbers were measured again with other data,
their new code was read for what it does when something fails (a full disk, a kill in the middle, two runs at once,
several runs in one reader), and review 2's open list was done.

Branch: `review/state-logs-3`. Measurements: scripts outside the repository, deleted afterwards with their data (the
largest: a 30-day log folder, 1.9 GB of timeline in 570 MB on disk):

- `gen_activity.py`: a log folder as a busy project leaves it after 30 days at the host's measured rate (64 MB of
  `activity.jsonl` a day): 2.7 million records made with the real `activity.finalize`, in seven parts of 256 MiB
  compressed as gc leaves them and a current file of 128 MiB; 20 runs of a day and a half each.
- `p2.py`: conversations that do not only grow, through the real `LlmTraffic` (the proxy's side) and
  `LlmTrafficFile` (the supervisor's), 200 requests each, read back with `llm_traffic.read_records` and compared
  with what was sent: growing only, Claude Code's moving `cache_control`, compaction every 50 requests, a sliding
  window (the oldest tool round dropped), old tool results cleared one more per request, a clock in the first
  message, 2 to 20 conversations in turn, the Responses API's `input` items.
- `p6.py`: one part of the timeline (255 MiB, 360,169 records) as an SQLite table with an index by run and time, in
  WAL mode - plain rows and rows compressed one by one.
- `stress.py`: four threads through `ProxyServer.log` and four through `append_record` (a shared log) while
  `rotate_logs` rotates both every 20 ms (64 rotations in 8 s), after this review's changes to the rotation.
- `p9.py`: reviews 1 and 2 again - a connection log of 25 conversations x 41 requests with and without body
  references, `gc --usage --json` against what `gc` then freed, `sandbox-ai logs` over a log with 24 compressed parts.

## 1. Findings

Severity: **H** high (wrong or leaked data, records lost), **M** medium, **L** low. **[R1]**, **[R2]**: a bug in that
review's own work; the others were there before.

1. **H [R2] - A request of the LLM traffic log was put together with another run's request.** Review 2's deltas name
   the request they continue by its number (`"of": N`), and every run numbers its requests from 1.
   `llm_traffic.Expander` kept the records it had read by that number alone, `tail_records` its sets of numbers
   across every file it was given, and `finder` looked a number up in the last two files that could hold it. `sandbox-ai
   llm traffic --session NAME` reads all runs of a session: with two runs of 15 and 20 requests, `--tail 25` showed the
   earlier run's last five requests with the **later run's conversation** in front of their new messages - no
   `"missing"`, nothing said. Read in order, a delta whose own base was lost (a line not written on a full disk, a
   part removed) was completed from the run before in the same way. Fixed: a request continues one of its own log
   only (`lifetime`: run and start time of the file's name; `Expander.enter`, `tail_records` and `finder` keep to it),
   and never a later request.
2. **H - Secrets assigned to a variable stayed in the connection log and the LLM traffic log.** `redact_text` took
   `password=`, `secret=`, `api_key=` only as whole words (a form's fields): `DB_PASSWORD=...`,
   `AWS_SECRET_ACCESS_KEY=...`, `OPENAI_API_KEY=...` (unless the value has a known token's shape) of an `.env` file
   the agent read went into every later model request - and so into both logs - unredacted, for as long as the logs
   are kept; only the timeline had its own pattern for them (`activity.EXTRA_SECRETS`). In the connection log, which
   keeps a request's body as its JSON text, not even `PASSWORD=...` at the start of a line was redacted: the line
   break is `\n` there, and before `nPASSWORD` there is no word boundary. Review 2 saw the second half for MCP
   arguments and a question's body and called it harmless; the request bodies are where it is not. Fixed: one more
   pattern (names with `token`, `secret`, `password`, `passwd`, `api_key`, `access_key`, `private_key`,
   `credential(s)` anywhere in them; the value quoted - also as `\"...\"` - or up to white space, never past an
   escaped line break or quote, so JSON text stays JSON); MCP arguments and a question's body are redacted value by
   value (`redacted_json`), not as text. 0.59 ms for a 64 KiB body before and after.
3. **H - A rotation that failed moved every older part, every minute, until they were deleted.**
   `rotate_held_log` renamed `NAME.1.gz` to `NAME.2.gz` and so on before it compressed the log. On a full disk the
   compression fails and the rotator tries again a minute later: three failed tries left `.1.gz`/`.2.gz` as
   `.4.gz`/`.5.gz`; at 999 a part is deleted - 17 hours of a full disk and a running run had lost all its older
   parts, and with every move the keys the timeline and the canary log count by. Fixed: the parts move only once the
   new part is complete, under the lock, just before it is put in place.
4. **M - The timeline's readers after it rotated.** `activity.jsonl` moves away at 256 MiB (four days at the host's
   rate). (a) `sandbox-ai activity --since 7d` read the current file only: the answer was silently four days long.
   (b) The app's Activity page asks for the last 5000 events: just after a rotation it showed the two or three the
   new file held. (c) A run's records were looked for in every older part, each read twice (once for the run's name,
   once parsing every line): **11.5 s and 360 MB** for a run of three weeks ago in the 30-day folder, 10.3 s for its
   last 5000 events. Fixed: `--since` reads the parts written since then, a tail the current file does not fill
   while it is still under 32 MiB goes on into the newest older part (no further, and not from a current file of its
   usual size: a tail with a filter that matches little must not read a month - one more part cost a `--grep` that
   finds nothing 8 s), a run is read in one pass that parses only the lines naming it (searched a MiB at a time) - and which
   parts name which runs is remembered beside them (section 3). 4.5 s and 2.1 s. Plain `sandbox-ai activity` (no
   `--since`, `--tail` or run) shows the current file as before and now says how many older parts there are.
5. **M [R2] - Deltas left out only a common beginning.** Review 2 asked how often a model's context editing breaks
   the prefix. Measured (200 requests each): growing only x29.5, Claude Code's moving `cache_control` x23.2,
   compaction every 50 requests x13.7 (one whole record per compaction, as intended) - but a sliding window **x1.4**,
   old tool results cleared one by one **x2.1**, a clock in the first message **x1.0**: what a harness does at a
   local model's small context wrote nearly every request whole. Fixed: items after the common beginning that the
   base holds anywhere are left out too (`"items"`: a layout of written items and `[where, how many]` of the
   base's): x9.2, x8.3, x16.7; all read back equal to what was sent.
6. **M - The approvals' "blocked" list forgot what was refused before a log rotated** (review 2's finding 14, rated
   L there: it concerns a run that still runs, and the list is where a human decides what to allow). It read the
   current part of each log: after the connection log's rotation (emptied in place) or a shared log's (renamed
   away - until the next record there is no file at all) the refusals were gone. Fixed: what the current part does
   not fill of the 4 MiB read per log comes from its older parts, the newest first; a compressed part's tail is
   remembered while the part stays as it is (0.66 s for one of 128 MiB that compresses badly, once per process and
   only until the current part holds 4 MiB again).
7. **M - The canary log linked a request to the wrong event of the timeline after a rotation** (review 2: "check the
   links"). The timeline's key of a connection record is its offset in the part it was read from - `OFFSET` in the
   first part, `PART.OFFSET` after each rotation; `canary.model_requests` used `OFFSET` for every part. In the test
   the first request of each of three parts got the same id: all three linked to the first part's request. Fixed: the
   part's number from the files (`NAME.N.gz`, the highest the oldest; the log or `NAME.gz` the last).
8. **M - A rotation killed before its last step.** (a) `rotate_held_log` between putting the part in place and
   emptying the log (review 2's open item): the part's records in both, counted twice by every reader (398 requests
   in the Statistics for 199). (b) **[R2]** `rotate_shared_log` waits 0.2 s for a late writer between compressing the
   renamed log and removing it: a run that ends in that moment leaves `files.jsonl.TIME` beside
   `files.jsonl.TIME.gz`, and gc skipped such a pair for good ("never overwritten") - up to 128 MiB uncompressed
   until the age limit. The same after `LlmTrafficFile.pack` or `statedirs.compress` killed before their unlink.
   Fixed in gc (the run has ended by then): a connection log whose `.1.gz` holds the log's own beginning, byte for
   byte, loses that part and the parts before it get their numbers back (`undo_cut_rotation`); a raw file whose
   compressed copy holds its beginning is completed (what it got later is added as another gzip member) and removed
   (`finish_part`); another content under the name: both stay.
9. **L - Two runs of a project rotating a shared log in the same second.** The look for an existing part and the
   rename were two steps, and a rename replaces: the second run could put the new, small log over the first run's
   part. A window of microseconds once per 128 MiB; closed all the same (a second name for the file, which fails if
   the name is taken, then the first removed; a file system without links renames as before). The timeline's own
   rotation likewise.
10. **L - The part gc splits off a shared log was dated the day gc ran**, so the age limit kept it up to 30 days
    longer than its records. It takes the log's last write now.
11. **L - MCP arguments and a question's body were redacted as JSON text** (review 2's open item): `password=` after
    a line break missed, the backslash of an escaped quote after a value eaten. Fixed with finding 2.

Open, all low:

- The timeline's collector reads a log again from its start after a rotation; what was written between its last look
  (a second) and the rotation is in the rotated part and never reaches the timeline.
- `canary.activity_records` and `report` read the timeline's current part only (the report's appendix names what it
  left out of each file it read, not the older parts).
- `sandbox-ai activity --runs` reads every part (19 s over the 30-day folder; the command line only - the app does
  not call it). The parts' index could hold the counts.
- A line the supervisor could not write (a full disk) is unknown to the proxy: later requests written as deltas of it
  read as `"missing"` until the next whole record (64 requests or 16 MiB at most).
- More than 16 conversations in turn (20 measured: x2.2) write whole records - the proxy remembers its last 16.
- `--since 3d` without `--tail` holds the current file's records in memory (674 MB for 128 MiB; as before).
- The kept HOMEs (`gc --usage --detail`, `--transcripts`, `--pack`) were not reviewed again here.

## 2. Reviews 1 and 2 re-verified

| Claim | Verified how | Verdict |
|---|---|---|
| R1: a repeated body is written once; lossless; every part stands alone | `p9.py`: 1025 model requests, 68.0 -> 24.0 MB raw; `stress.py` after this review's changes: 38,679 of 38,679 connection records and 62,164 of 62,164 shared-log records through 64 rotations, every `same_as` resolved in its own part, no temporary file left | Holds. My bodies compress far better than real ones (repeated lines): the gzip ratios of this run say nothing; reviews 1 and 2 measured those with real requests |
| R1: readers stop at their budget | `sandbox-ai logs` over a 68 MB log, 24 compressed parts and two ended runs' logs: 0.68 s, 23,190 older lines named as skipped | Holds |
| R2: `gc --usage` estimates compression per kind from a sample | estimate 90.1 MB, freed 89.8 MB | Holds |
| R2: LLM traffic as deltas, every request read back whole | `p2.py`: 11 kinds of conversation, 200 requests each, all equal to what was sent | Holds for one run; wrong across runs (finding 1); weak when items move (finding 5) |
| R1/R2: the rotation of the held log loses nothing | the code and `stress.py` | Holds when it succeeds; when it fails it moved the older parts (finding 3), when it is killed it left duplicates (finding 8) |
| R2: a late record of a renamed shared log is kept (0.2 s grace) | read | Holds - and opened the window of finding 8b |
| R1: `log_days` goes by a file's last write | `compress` keeps the time, the in-run parts are written at their newest record, renames keep it | Holds; gc's split did not (finding 10) |
| R2: redaction keeps the LLM traffic log JSON | by construction (strings redacted one by one) | Holds; what the patterns did not know is finding 2 |

Where I disagree with reviews 1 and 2:

- **"redact_text on JSON-escaped text ... harmless today" (R2).** It is where a request's body is logged as its text:
  the connection log. And the larger gap was not JSON at all - `NAME_PASSWORD=` was never a secret to the patterns.
- **The blocked list (R2's finding 14) is not L.** It changes what a human sees of a run that still runs.
- **"Every part of [the LLM traffic log] stands (nearly) alone" (R2).** A part does; a number does not. `n` is unique
  within one proxy's life, and the reader was given several.
- **"None [of the readers] needs random access by key except the records of run X, which the per-run file names ...
  already answer" (R1).** The timeline is one file for every run of the project; "the records of run X" was its
  slowest question by two orders of magnitude (11.5 s against 0.12 s for the newest 5000 events).
- **Review 2's fix for the late writer** traded a lost record for a pair of files gc never touched again. The grace
  is right; what was missing is somebody finishing a rotation that was cut.

## 3. A database: no - with the numbers

Review 1 rejected SQLite on principle (the content was the problem; four writers on both sides of a container
boundary, on macOS across virtiofs), review 2 agreed and left one question: the timeline over months. Measured on the
30-day folder (1.9 GB of records, 570 MB on disk) and on one of its parts in SQLite:

| | JSON lines, gzip parts (before) | the same, after this review | SQLite |
|---|---|---|---|
| Disk for 255 MiB of records | 62 MiB | 62 MiB | **328 MiB** (169 MiB with each row compressed) |
| ... for the 30 days | 0.57 GB | 0.57 GB + 647 bytes | 2.4 GB (1.2 GB), extrapolated from the part |
| Writing 360,169 records | (the collector appends) | (the same) | 6.9 s (26 s compressed) |
| The newest 5000 events (the app's Activity page) | 0.12 s | 0.13 s | not measured (an index scan: milliseconds) |
| The last 5000 events of a run three weeks old | 10.3 s | **2.1 s** (the first look at a folder reads every part once: 10.8 s for the whole run) | 0.006 s |
| All 135,030 events of that run | 11.5 s, 360 MB | **4.5 s**, 255 MB | 1.0 s (one part) |
| The runs of the folder (`--runs`) | 19 s | 19 s | 0.07 s |

The request that started all three reviews was disk space. As the store, SQLite takes 2.7 to 5.3 times the disk of
the compressed parts for the same records (pages do not compress; compressing rows one by one loses what makes the
parts small - the repetition between records), and it needs one writer that both the sandbox's supervisor and the
host's launcher can be, across a boundary where its locks do not hold. What it would buy is the two rows in bold and
`--runs`: seconds, for questions asked by hand about old runs. The page a human looks at all day is 0.12 s either
way.

As an index it is the right idea at the wrong weight. The one question that needs an index is "which parts hold
run X", and a part never changes once it is renamed away: `.activity-parts.json` beside the parts
(`{"parts": {TIME: [run, ...]}}`, 647 bytes for the 30 days) is written by whichever reader first has to scan a part
(0.9 s for 256 MiB, once), keyed by the part's time (the same raw and compressed), made again when it is missing or
damaged, done without when the folder cannot be written, dated like the newest part so that reading a timeline never
makes its folder look used (gc's `--logs-older-than`). It is never the source of anything: a part it does not know is
read.

Decision: **no SQLite, neither as store nor as index.** Should `--runs` or whole old runs ever matter, the same file
can hold each run's count and first and last time per part; that is the next step, not a database.

## 4. Design changes

| Data kind | Change in review 3 | Why (measured) |
|---|---|---|
| LLM traffic log | a delta continues a request of its own run only; moved items are left out too (`"items"`); a delta never continues a later request | another run's conversation shown as this one's; x1.0-2.1 -> x8-17 for contexts that slide or are edited |
| connection log, LLM traffic log, MCP calls, questions | variables assigned a secret redacted, also in JSON text; MCP arguments and question bodies redacted as values | an `.env` file's `DB_PASSWORD=`, `AWS_SECRET_ACCESS_KEY=` kept for 30 days |
| held log's rotation | older parts move only once the new part is complete | a full disk moved them every minute until deleted |
| shared logs' and the timeline's rotation | never over an existing part | a rename replaces |
| gc | finishes a rotation that was killed: the duplicate part of a log never emptied removed, a part left raw and compressed completed; a split part keeps its log's time | records twice; 128 MiB a time skipped for good |
| timeline readers | `--since` and a tail read older parts; a run in one pass over the parts that name it (`.activity-parts.json`) | silently short answers; 10.3 s -> 2.1 s |
| blocked list | reads on into older parts within its 4 MiB per log | refusals gone at a rotation |
| canary log | keys per part | every part's requests linked to the first part's |

Old logs read as before: a delta without `"items"` is review 2's; an older reader given a delta with `"items"` would
put the written items after the first `kept` ones (both formats are of 0.1.98, which is not released).

## 5. Before / after

| What | Before (main afdf199f) | After | Source |
|---|---|---|---|
| `llm traffic --session` over two runs, `--tail 25` | 5 of 25 requests with the other run's conversation | 25 of 25 as sent | test, `p1.py` |
| LLM traffic, sliding window / cleared tool results / a clock in the first message (200 requests) | 16.6 / 9.0 / 93.1 MB (x1.4 / x2.1 / x1.0) | 2.5 / 2.2 / 5.8 MB (x9.2 / x8.3 / x16.7) | `p2.py` |
| ... growing only / Claude Code / compaction / Responses API | 3.3 / 4.1 / 2.0 / 3.2 MB | the same | `p2.py` |
| an `.env` file's six secrets in a logged request body | 5 of 6 readable (6 of 6 inside JSON) | none | test |
| older parts after three failed rotations | renumbered to `.4.gz`, `.5.gz` | where they were | test |
| requests counted after a rotation that was killed | 398 for 199 | 199 (after gc) | test through `./sandbox-ai` |
| the last 5000 events of a run three weeks old (30-day folder) | 10.3 s | 2.1 s | `timeit.py` |
| all events of that run | 11.5 s, 360 MB | 4.5 s, 255 MB | same |
| `activity --since 3d` (the current file holds two days) | 180,013 events (two days) | 269,066 (three days) | same |
| the blocked list after every log rotated | 1 of 5 refusals | 5 of 5 | test through `./sandbox-ai` |
| canary requests linked to their own timeline event after two rotations | 1 of 3 (the other two to the first's) | 3 of 3 | test |
| redaction of a 64 KiB body | 0.59 ms | 0.59 ms | `p8.py` |

## 6. What needs a live check

- A real `--llm` run against LM Studio with a harness that trims its context (OpenCode, Qwen Code at a 32k context):
  how many records carry `"items"`, and that the LLM traffic page shows each request whole.
- `sandbox-ai llm traffic --session NAME --tail 50` of a real session with several runs.
- The user's busy project after a week: `gc --usage` before and after, `sandbox-ai activity RUN` of an old run (the
  first look builds `.activity-parts.json`), the app's Activity page right after `activity.jsonl` passed 256 MiB.
- macOS: the rotation's second name for a shared log (`os.link`) on the log folder as Podman's VM shares it
  (virtiofs); it falls back to the rename when the file system refuses.
- A disk that really fills during a run: the rotation keeps its parts, the start warns, records resume.
