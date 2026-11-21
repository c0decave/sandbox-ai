# Leaks and performance, review 2: core and the long-running processes (0.1.99)

Reviewer 2 of the two independent reviews (brief: leaks-perf-2x.md). Branch `review/leaks-perf-2` off main 2383df92
(0.1.112); nothing merged or pushed. The other reviewer's report was not read; the older reports in docs/reviews and
the release notes were read only afterwards, to mark what was known (one thing: the proxy's own `TCP_NODELAY` of
0.1.92 - the same wait in the relays and the web interface was not).

Focus: what runs for days - the supervisor's parts in the sandbox (connection proxy, `--llm` relay with the spend
meter and the LLM traffic log, MCP relay, DNS stub, port relays), the launcher's parts on the host (activity
timeline, approvals watch, monitor, launcher log), `sandbox-ai web serve` - and what a human waits for: the commands'
start, the listings, the log readers, checkpoints.

How: soaks against the real code with stand-in servers (the scripts' short forms are kept as tests), each watched
for resident memory (VmRSS), open descriptors, threads, child processes, the tables each part keeps, and with
`tracemalloc` where memory moved; latency against the same exchange without the part in between; cProfile for the
hot spots; a scale environment for the commands (1000 kept starts, 368 MiB of logs made from a soak's real records).
The machine ran other test suites meanwhile: times are the best or the median of several runs, and each pair
(before, after) was taken in the same minute. "Before" is main 2383df92 exported beside the branch.

Every fixed finding has a test that failed before its fix (a timing budget with room, a count, or a bound) and
passes after; the soaks are kept as regression tests (tests/test_leaks_perf_2.py, 25 tests, 16 s).

Severities: Medium - a wait or a growth a user of the feature meets in normal use (or without end in a long run);
Low - a narrow case, or milliseconds.

## Findings

| # | Severity | Where | What | Measured (before -> after) | Status |
|---|---|---|---|---|---|
| 1 | Medium | sandbox/runtime.py:7543 (`LlmClientWatch`) | an `--llm` answer that ends with its connection reached the agent half a second late; a watch thread per request lived as long | streamed request read to its end: 509 ms -> 20 ms (2.9 ms without the relay) | fixed 62821a8c |
| 2 | Medium | sandbox/webui.py:5330, :5382 (`Handler`, `send`) | every answer of `sandbox-ai web serve` waited for the browser's delayed ACK | kept-alive API request: 42 ms -> 0.7 ms; new connection: 45 ms -> 3.8 ms | fixed 4a48ad2c |
| 3 | Medium | sandbox/runtime.py:12739 (`join_streams`: `PortRelay`, net.py `serve_relay`), sandbox/vpn_runtime.py:1770 (`splice`) | a relayed host port and a lent tunnel waited for delayed ACKs, each way | exchange with head and body 1 ms apart: 83 ms -> 3.0 ms (2.5 ms direct) | fixed efc5d9f8 |
| 4 | Medium | sandbox/launchlog.py:313 (`_states_file`) | every start's status parsed the whole launcher.states.json again | 1000 kept starts: `list` 788 -> 286 ms, `list --json` 1014 -> 484 ms, `session list` 773 -> 269 ms, completion of a name 786 -> 316 ms | fixed 4a66d2d6 |
| 5 | Medium | sandbox/launchlog.py:43 (`STATES_KEPT`) | past 1000 logged states every state was said again and the file rewritten at every poll | 1010 kept starts, each `list`: 1010 lines into launcher.log, 1010 rewrites of 68 KB, 2.4 s -> 0 lines, 0.3 s | fixed ac7ca3b6 |
| 6 | Medium | sandbox/starts.py:958, :973; sandbox/workspaces.py:962; sandbox/agent_sandbox.py:2567 (`parse_args`) | `list --folders` built the launcher's parser (150 options) for every kept start | 1000 kept starts: 4605 -> 617 ms (`--json` 3971 -> 978 ms) | fixed db789d60 |
| 7 | Medium | sandbox/activity.py:1848 (`Connections.repeats`) | the launcher's timeline kept an entry per destination ever named, and looked at each in every poll, for the whole run | 20000 destinations: 20000 entries (about 9 MB), idle poll 3.2 -> 22 ms; now 4096 entries, 1.8 ms, flat | fixed be72ec38 |
| 8 | Low | sandbox/runtime.py:4080 (`SECRET_TEXT[0]`) | redaction walked a long word after a quote once per key name | request with 64 KB of base64url: 9.6 -> 0.7 ms for the pattern (redact_text 9.7 -> 1.2 ms); 8 KB of it 1.28 -> 0.15 ms | fixed 14310c19 |
| 9 | Low | sandbox/llm_traffic.py:61 (`safe`) | `llm traffic` looked at every character of the transcript in Python | 57 MB traffic log: 2650 -> 1420 ms | fixed 96e194ce |
| 10 | Low | sandbox/logs.py:66, :331 (`iter_lines`, `summarize`) | `sandbox-ai logs` on a full log folder takes seconds | 368 MiB of logs: 4.9 s, peak 93 MiB | open |
| 11 | Low | sandbox/net.py (imports), sandbox/main.py | a listing's start loads `secrets`/`hashlib`, `subprocess`, `socket`, `tempfile` it does not use | about 8 of the 45 ms `list` takes beyond Python's own start | open |
| 12 | Low | sandbox/runtime.py:1662 (`McpRelays.threads`) | a finished relay's thread object is kept per `mcp attach --live` | a few hundred bytes per attach, human-driven | open |

### 1. The watch of the agent's connection held the connection open (fixed)

While the `--llm` server works on a request, a thread polls the agent's socket (0.5 s at a time) to end the server's
work when the agent goes. `stop()` only set a flag. A thread in `poll()` holds the socket's file in the kernel: the
relay's `close()` of the agent's connection after the answer sent no FIN until the poll's time was over. Every answer
whose end is the connection's - `Connection: close`, HTTP/1.0, an answer without a length (a server that streams and
closes: mlx_lm.server and other servers on Python's http.server) - took up to half a second longer than the server
took, and each request's watch thread stayed that long (a burst of requests: as many threads).

Measured with tests/devtools/model_mock.py behind the real proxy (a 55 KB request with 20 tools, 40 chunks, read to
the end of the connection): 2.9 ms direct, 509 ms through the relay, 57 ms with the poll set to 0.05 s (the wait is
the poll), 20 ms after the fix. The watch now polls a pipe beside the socket; `stop()` writes a byte, the thread
ends and closes both ends. Without a pipe to be had (out of descriptors) it runs as before.

Tests: `test_a_watch_that_is_stopped_ends_at_once_and_lets_go_of_the_agents_connection`,
`test_a_watch_still_ends_the_servers_connection_when_the_agent_goes`,
`test_a_watch_without_a_descriptor_to_wake_it_works_as_before`,
`test_an_llm_answer_ends_when_the_server_ended_it_not_half_a_second_later` (end to end).

### 2. The web interface answered in two writes with Nagle's algorithm on (fixed)

`Handler.send` wrote the headers (http.server writes them when they end), then the body: the second write waited for
the browser's ACK of the first, which the browser's system holds back when it has nothing to send (40 ms on Linux,
up to 200 ms on Windows). Every request of the page - its polls, each list, each click - took 42 ms and more on this
machine itself: `GET /api/me` on a kept connection 42.3 ms median, `GET /api/hosts` 42.1 ms, a page on a new
connection 45.0 ms. Now the answer is one write (one TLS record for a small one) and the connections have
`TCP_NODELAY` (a terminal's output frames too): 0.70 ms, 0.57 ms, 3.8 ms.

Tests: `test_the_web_interface_answers_in_one_piece_without_waiting_for_the_browsers_ack` (real HTTPS),
`test_the_web_interfaces_connections_send_small_pieces_at_once`, `test_an_answer_of_the_web_interface_is_written_once`.
The web suites (4510 tests) pass.

### 3. The relays copied bytes with Nagle's algorithm on (fixed)

0.1.92 gave the proxy `TCP_NODELAY`; the byte relays kept the wait: a host port relayed into a sandbox
(`sandbox-ai net allow --allow-host-port`: `PortRelay` in the sandbox, `net.serve_relay` on the host; on a Mac every
host port and `--llm` server of a start goes this way) and a lent VPN tunnel's connections (`vpn_runtime.splice`: the
borrower's end and the lender's). A relay does not know where a message ends: a request's body after its head, an
answer's after its headers, a stream's next token each waited for the peer's delayed ACK. An exchange whose two
pieces come 1 ms apart: 2.5 ms direct, 83 ms through the port relay (58 of 58), 3.0 ms after; through one end of a
lent tunnel the same 83 ms. `join_streams` and `splice` set `TCP_NODELAY` on their ends (a Unix socket has none).

Tests: `test_a_relayed_host_port_answers_as_fast_as_the_port_itself` (both real ends),
`test_join_streams_turns_nagles_algorithm_off_on_its_tcp_sides`,
`test_a_lent_tunnels_connections_pass_on_what_they_have_at_once`.

### 4. and 5. The launcher log's states (fixed)

`sandbox-ai list`, `session list` and the shell's completion of a start's name ask every start's status, and each
status says its state through `launchlog.changed()` - which took the lock, read launcher.states.json and parsed all
of it, for each start (1000 starts: 68 KB of JSON a thousand times, half the command). The file's bytes are now
compared with the last reading of this process and parsed only when they differ (what another process wrote counts
as before: the comparison is of the content, not of times).

Past `STATES_KEPT` (1000) the oldest state was dropped for each new one. With more starts, hosts and images than
that, the oldest was one still polled: said again at the next poll, which dropped the next, and so on - with 1010
kept starts every `sandbox-ai list` wrote 1010 lines into launcher.log (2 MB a part, four parts: its history gone
within a minute of the app's polls), rewrote the states file 1010 times (68 MB of writes) and took 2.2-2.5 s. Past
the first 1000 a state now goes by its age (30 days since it was said), the oldest first, and the file never holds
more than 20000: 0 lines, 0.28 s.

Tests: `test_a_listing_parses_the_logged_states_once_not_once_per_start`,
`test_a_state_another_process_changed_is_seen_at_once`, `test_what_is_handed_out_of_the_states_is_a_copy`,
`test_more_live_states_than_are_kept_are_not_said_again_at_every_poll`,
`test_old_states_go_first_and_the_file_never_holds_more_than_the_most`.

### 6. `list --folders` built the launcher's parser for every kept start (fixed)

Each kept start's options are read as the launcher reads them (`workspaces.next_start_targets` -> `parse_args`), and
each reading built the whole parser anew: 150 options, 4 ms (85 % of the command in the profile). The desktop app's
Files page asks this of every host. A listing now builds one parser per agent (`parse_args(argv, parser)`); the
lists `parse_args` adds to are each reading's own (argparse hands out the default object itself: without that a
parser used again would carry one start's hosts into the next).

Tests: `test_list_folders_builds_the_launchers_parser_once_per_agent_not_once_per_start` (tests/test_starts.py: the
same folders, the same reading start by start), `test_options_parsed_with_a_parser_used_again_read_as_with_a_parser_of_their_own`.

### 7. The activity timeline's table of destinations had no bound (fixed)

The collector runs in the launcher for as long as the run. Its `Connections` source counts repeats per destination
("allowed: 12 more requests to ...", once a minute) in a dict that kept one entry for every destination the
connection log ever named, and every poll (2 s) walked all of them. What the log names is the agent's choice: an
agent that tries address after address (a scan, a crawler, a refused lookup per name) made the launcher - a process
on the host - grow and poll slower for the rest of the run: 470 bytes and a look per destination; after 20000
refused destinations 20000 entries and 22 ms per idle poll instead of 3. Now at most 4096 destinations are counted
at a time; those quiet the longest are forgotten (what was counted for them is said first), one that comes back is
shown as a new destination. Below the limit nothing changes.

Tests: `test_the_timeline_counts_repeats_for_a_bounded_number_of_destinations`,
`test_what_was_counted_for_a_forgotten_destination_is_said_first_and_it_is_new_when_it_comes_back`.

### 8. Redaction: a long word after a quote (fixed)

The first pattern (JSON keys with a secret's name) begins `"[A-Za-z0-9_-]*(?:token|secret|...)`: after any quote it
took the whole run of word characters, then walked back through it once for every name. A body with long runs no
white space breaks - Codex's encrypted reasoning items (base64url), hashes, minified identifiers - cost 1.3 ms per
8 KB (prose: 0.01 ms), under the log's lock and holding the GIL. A lookahead now says first what the rest of the
pattern asks for anyway (word characters up to the closing quote and a colon); every match is the same - compared
over 40000 generated texts and every character re.I folds.

Tests: `test_the_key_pattern_with_its_lookahead_matches_what_it_matched`,
`test_a_long_word_after_a_quote_is_redacted_in_a_fraction_of_the_time`; tests/test_performance_core.py as before.

### 9. `sandbox-ai llm traffic` (fixed)

`safe()` escaped control characters with a generator over every character: 31 million calls for a 57 MB log, two
thirds of the command. A text with nothing to escape is now known as such in one look (`str.isprintable`); the
escaping is the same (4000 generated texts, both modes).

Test: `test_traffic_text_for_the_terminal_is_escaped_as_before_and_at_once_when_there_is_nothing_to_escape`.

### Open

- **10. `sandbox-ai logs`**: reads the newest 128 MiB of a folder by design (its bound); about a
  million lines parsed with `json.loads`, 4.9 s. No single hot spot left to take out safely (the profile is the
  parser, the line reader and the counters in thirds); a faster reading would mean summaries kept beside the logs.
- **11. Imports**: `list` takes 61 ms here, 16 of them Python's own start. net.py loads `secrets` (with hashlib's
  OpenSSL: 3.8 ms), `subprocess` (3.2 ms), `socket` (2 ms) and `tempfile` at its top for commands that do not use
  them. Loading them where they are used would save about 8 ms of each listing; it touches many functions of a
  module every command shares, for milliseconds: left.
- **12. `McpRelays.threads`** keeps each relay's finished thread object after `mcp detach --live`: a few hundred
  bytes per attach, and only a human attaches.

## What was soaked and held

Numbers are from the soak scripts (not kept in the repository; their short forms are the tests named `test_soak_*`).

| Part | Load | Resident memory | Descriptors | Threads | Tables |
|---|---|---|---|---|---|
| Connection proxy, HTTPS (real TLS both sides) | 3000 requests on one kept connection, 3000 connections dropped at four stages (part of a hello, after the handshake, in a request's body, before the answer), 750 POSTs of 20 KB, 750 names without a certificate, 3000 new connections | 36.3 -> 38.3 MB | 7 -> 7 | 4 -> 4 | live 0, contexts 1 |
| `--llm` relay with spend meter and traffic log | 20000 requests: streamed with 20 tools, conversations growing to 80 messages, not streamed, given up after the first bytes, a model not offered | 30.3 -> 41.5 MB, flat in every repeated phase (stream 36.5 -> 36.5 -> 37.1 -> 37.1; five times 2500 given up: 34.7, 37.6, 38.75, 38.76, 38.76) | 11 -> 11 | 6 -> 6 | live 0, queue 0 |
| MCP relay (`mcp_serve`, a server per connection) | 750 connections: five calls each, a server that crashes on its call, an agent that goes while a call hangs, results of 200 KB | (the harness's own buffer) | 5 -> 5 | 2 -> 2 | children 0 |
| `sandbox-ai web serve` | 200-400 each: pages, API requests on a kept connection, without a session, refused logins, connections dropped at four stages, terminals (WebSocket into a pty) closed without a close frame | requests and drops flat (31.35 -> 31.47 MB); 2800 terminals: +11 MB for the first 400, then +0.25 MB per 400 - `tracemalloc` shows no Python memory kept | 5 -> 5 | 3 -> 3 | active 0, sessions 1 |
| Activity collector (launcher) | 2000 transcripts in HOME, 20000 refused destinations and lookups | see finding 7 | - | - | repeats 4096 |

Also measured, without a change:

- **The proxy's own cost.** HTTPS on a kept connection: GET +0.8 ms (0.08 -> 0.88 ms), a POST of 8 KB +1.3 ms
  (0.29 -> 1.65 ms; no part of the profile above 20 %: TLS reads and writes, the log's append under its lock, the
  redaction). A streamed chunk of an `--llm` answer: 0.1-0.3 ms (2000 chunks: 205 ms, 65 ms direct), with the usage
  scanner, the loop guard, the scrubber and the traffic log. An idle proxy: 5 ms of CPU in 15 s (its look at the
  kill switch four times a second).
- **The start of the commands** (empty HOME, best of 7; `python3 -I -c pass` 16.2 ms): `--version` 42.2,
  `--help` 42.1, `list` 61.5, `list --json` 79.9, `list --folders` 66.2, `net list` 60.3, `approvals list` 69.8,
  `schedule list` 87.7, `spend` 93.5, `llm list` 74.4, `mcp list` 95.6, `hosts list` 65.4, `completion bash` 48.6,
  `__complete` 41.4, `claude --help` 127.2 ms.
- **Checkpoints** (30000 files, 81 MiB): the first 2.63 s, one with nothing changed 0.61 s, one with five files
  changed 0.56 s, `diff` 0.46 s, peak 113 MiB.
- **The other readers at scale** (1000 kept starts, 368 MiB of logs): `spend` 0.5 s (peak 87 MiB), `gc --dry-run`
  3.5 s, `safeguards --json --tail 50` 60 ms.
- **Read, nothing found**: the DNS stub (eight forwarders at once, a back-off per nameserver), the approvals broker
  and gateway (questions capped per run), the attribution cache (4096), the TLS contexts (256) and verified names
  (4096), the logs' rotation during a run (128 MiB a part, 1 GiB per log) and the launcher log's (2 MiB, four parts),
  the monitor's recorder (its series trimmed, its own CPU recorded), the supervisor's memory guard.

## Tests

- tests/test_leaks_perf_2.py: 25 tests - each fix's failing test, what stays the same, and four soaks
  (`test_soak_the_proxy_...`, `..._the_llm_relay_...`, `..._the_mcp_relay_...`, `..._the_web_interface_...`).
- tests/test_starts.py: `test_list_folders_builds_the_launchers_parser_once_per_agent_not_once_per_start`.

## Seen while running the suite (not this review's, left)

The full core suite ran in four shards with the files dealt out by size - another order than the usual one, on a
machine busy with other suites. Two tests failed that this branch does not touch; both fail the same way on the
branch point (2383df92):

- `tests/test_allow_domain.py::test_net_allow_opens_the_name_and_the_names_below_it` fails whenever
  tests/test_allow_lists.py ran before it in the same process (`*.wild.example` is still in the runtime's
  wildcards): an order the usual alphabetical run never has. A test's isolation, no fault of the product.
- `tests/test_stability_round3.py::test_the_web_interface_ended_by_a_signal_cleans_up_and_starts_again[1]` failed
  once (the process died of SIGHUP: status -1, not 129) and passes alone, three times of three. `webui.serve` writes
  listen.json before it installs its SIGTERM/SIGHUP handlers (sandbox/webui.py: `write_private(listen_file(), ...)`,
  the handlers after `ready`); the test signals as soon as the file is there. On a busy machine the signal can come
  in between - and then listen.json stays, which is what the test is about. A window of microseconds; installing the
  handlers before the file is written would close it.

Both fixed on fix/followups-1: the `net` tests' fixture gives each run's supervisor a wildcard set of its own, and
`webui.serve` installs its handlers before it writes the file (a test signals from inside the write).
