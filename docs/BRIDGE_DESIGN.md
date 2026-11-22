# Session bridge: design (0.1.97; both sessions on other hosts: 0.1.98)

A bridge lets one running session (A) reach another running session (B) in a controlled way: send it a note, ask
its agent a question and wait for the answer, read or (asked, with the diff shown) write files under a path of its
workspace, or watch what it does as an observer. The decisions below were made by the user; this document says how
they are carried out, what a hostile agent on either side must not be able to do, and where each piece lives.

Status: the same-host core is built (`sandbox/bridge.py`, `sandbox-ai bridge add|list|show|revoke|resume`), and so
is the bridge across hosts (section 9: `sandbox/bridge_remote.py`, `sandbox-ai bridge-stub`), the observer's
terminal snapshot (section 5.5) and the app's part (the Bridges box and New bridge… on the Sandboxes page, with the
sessions of the user's hosts as HOST:NAME; a Board part; presets that propose a bridge, section 11). Since 0.1.98 both sessions may run on
other hosts (section 9.7): two of one host are bridged there, two of two other hosts each get their half on their own
host and this machine relays the frames between them; the web interface lists, makes, revokes and resumes bridges
too.

## 1. Words

- **Side**: one of the two sessions of a bridge (`a`, `b`). A side is a *running agent run* (run.json, its
  supervisor's control socket), never an MCP server's sandbox, a helper or a team board.
- **Capabilities** (granted per direction): `message`, `ask`, `files:ro:PATH`, `files:rw:PATH`, `watch`.
  `bridge add A B --allow CAPS` gives A these capabilities *over B*; `--both` gives B the same over A.
- **Observer actions** (only with `watch`): `alert` (to the human; on by default), `hint` (a note into B's inbox),
  `net-cut` (B's kill switch: `api`, only its model API stays reachable), `pause` (B frozen; only the human resumes
  it). Each one is switched on separately, per bridge.
- **Broker**: the host process of one bridge (`bridge.py --internal-broker ID`). It is the only thing that touches
  both sides.
- **bridge-PEER**: the MCP server name a side sees (`bridge-<peer's start name>`, or `bridge-<id>` when that does
  not fit the MCP name rules or is taken).

## 2. Threat model

Either agent may be hostile (prompt-injected, or simply misbehaving). The human and the host are trusted; the
supervisors of both runs are trusted (they are sandbox-ai's own code, outside the agents' reach).

A hostile agent on either side **must not be able to**:

| # | Attack | What stops it |
|---|---|---|
| T1 | Create, widen or chain a bridge (more capabilities, a third session, a longer lifetime) | No tool of the bridge server does any of this; the broker's tool list is fixed at `add` from the human's grant. Bridge state lives in `~/.local/state/sandbox-ai/bridges/` (0700, never mounted into a sandbox). `bridge add`, `revoke` and `resume` refuse to run inside a sandbox (`$SANDBOX_AI_RUN`, `/run/.containerenv`). Approvals of bridge questions are files in the bridge's directory: an agent cannot answer them. |
| T2 | Reach the peer's network, secrets or host | There is no network or file path between the pods. The broker is reached only through the run's supervisor relay (a detached clone of one socket folder handed over the control socket, as `mcp attach --live` does). The broker reads the peer's workspace on the host, only below the granted PATH, without following any link (each component opened with `O_NOFOLLOW` from the directory descriptor of its parent), refusing `..`, absolute paths, `.git` and the run's private/held trees, and refusing canary decoy files (which also suspends the bridge). Non-regular files (FIFOs, sockets, devices) are never opened for reading (`O_PATH` first, then checked). |
| T3 | Escape the granted PATH with links or a race (symlink swap, hard link, rename between check and use) | Every access walks from the workspace root's descriptor one component at a time (`O_NOFOLLOW|O_DIRECTORY`), so a component swapped for a link fails the walk instead of being followed; the workspace root itself is reached the same way from `/` (it may lie inside a folder another sandbox writes, which could turn it, or a folder above it, into a link). Writes go to a fresh temporary file in the same directory descriptor, then `rename` over the target: a hard link planted at the target is replaced, never written through. A write re-checks the file's content against what the human saw in the diff and refuses when it changed in between. |
| T4 | Write into the peer without the human seeing it | Every `write_file` is a question to the human with the unified diff (or, for binary content, sizes and digests) in its details; its answer covers that one write only (`once`, whatever scope is chosen; `--ask never` does not apply to writes). Nobody answers automatically: `--ask allow` of a run never reaches a bridge question. A peer whose workspace is read-only (`--workspace-ro`) cannot be granted `files:rw` at all. |
| T5 | Smuggle instructions without taint | Every tool whose result carries content of the peer (inbox, ask, list_files, read_file, watch_*) is marked untrusted in the relay's ruleset that `bridge add` hands to the receiving run's supervisor: its result taints that run before the agent reads it (`McpGateway.observe`), so the rules for tainted runs hold from then on. Results of the other tools carry no peer content by construction (an id, "sent", "written", the human's own words). Peer content is framed as untrusted data in the result text as well. |
| T6 | Relay content through a chain of bridges (A-B and B-C used as A-C), or loop it back | Bridges are not transitive: a broker serves exactly its two sides, and no tool takes a session name. Content is tracked per run (*exposure*: which origins' content entered a run, through which bridge, after how many hops). A transfer whose content would have passed more than `MAX_HOPS` (2) bridges is refused, and so is one that would carry content back to its origin over another bridge than it came on (loop guard). This is an over-approximation by run (like taint), stated in the refusal. |
| T7 | Exhaust resources (flood, oversize, many open asks, huge reads) | Per side: a call rate (token bucket, default 60/min), at most 8 connections and 64 calls in flight (a call of a closed connection still counts), 1 MiB per JSON-RPC line, 100 waiting inbox items, 4 open asks, 4 calls waiting for the human; per item: message/question/answer size (default 16 KiB); read and write size (256 KiB per call); listings (1000 entries); watch tails (200 records); a daily volume per bridge (default 64 MiB, counted in both directions); an ask timeout (default 300 s, at most 1800 s); a write's diff (one at a time, made by a process of its own ended after 10 s, refused at once when its size alone shows it cannot be shown whole); alerts at most 20 per hour. A limit reached is an error result, never a hang. |
| T8 | Keep a bridge alive after revoke, end of a side, expiry, a net cut or a canary alarm | Three layers: the broker exits and closes its sockets and connections; `revoke` itself closes both relays (`mcp-close` on each supervisor) and takes the server out of the agent's configuration; the relay's server socket is gone, so a reconnect fails. A bridge is never restored on resume (it is in no start's options). The broker checks every second: both runs alive (pid and start time of their launchers), `--until` not passed, not revoked, no kill switch on either side, no new canary event of either side - a cut or a canary alarm *suspends* the bridge (every tool refused except `info`) until the human lifts it (`bridge resume ID`) or revokes it. |
| T9 | Impersonate the other side | A side is known by the socket it came through (each side's socket folder is handed only to its own run's supervisor); nothing an agent sends names its side. |
| T10 | Raise the human's attention with crafted alerts (fake prompts, terminal escapes) | Alert text is the observer agent's: shown escaped (printable characters only), bounded (500 characters), rate limited, and always labelled with the bridge and the side that raised it. |
| T11 | Use a bridge between sessions of two other hosts, relayed by this machine, to reach more than its two halves allow: call what its side was not granted, reach the relaying machine, or open a way between the two hosts | The relay has no side (no socket, no relay in a sandbox, no files, no questions) and makes no call of its own: it passes on a call of side X only when X was granted the tool and the path lies below a granted one, while the bridge is active, at most at twice the bridge's rate; the receiving half checks it again against its own copy of the grant, on its host, with its own human. The hosts never connect to each other: each has one SSH connection *from* this machine, with every forwarding off, and gets no key or address of the other (section 9.7). |

Out of scope for v1: a hostile *human* on the same host (they own the state directory), a compromised supervisor,
and covert channels through timing. The hosts of `sandbox-ai hosts` and this machine are trusted as sandbox-ai's own
processes are (a compromised host could lie about its exposure, section 9.5, or about what it relays, 9.7).

## 3. Architecture

```
  run A (pod/bwrap)                         host                                   run B (pod/bwrap)
  agent --stdio--> runtime.py bridge        broker (bridge.py --internal-broker)   runtime.py bridge <--stdio-- agent
     /run/sandbox-mcp/bridge-B/server.sock   bridges/ID/a/server.sock                /run/sandbox-mcp/bridge-A/...
  supervisor relay (McpGateway: rules,  --->  [side a]   state, limits,  [side b] <---  supervisor relay (rules, taint,
  taint, mcp-calls.jsonl, approvals)          approvals, exposure, logs           mcp-calls.jsonl, approvals)
```

- **One broker process per bridge** (isolation: a flood on one bridge does not slow another; its end ends exactly
  that bridge). It listens on two Unix sockets, `bridges/ID/a/server.sock` and `bridges/ID/b/server.sock` (folders
  0700), and speaks MCP (newline-delimited JSON-RPC 2.0: `initialize`, `ping`, `tools/list`, `tools/call`).
- **Attachment reuses `mcp attach --live`**: for each side `bridge add` opens the side's socket folder (`O_PATH`),
  makes the detached clone of that folder alone in the run's user namespace (`mcp.socket_dir_handle`), and hands it
  to the run's supervisor with control op `mcp-open` (name `bridge-PEER`, relay configuration below). The supervisor
  opens a relay at `/run/sandbox-mcp/bridge-PEER/server.sock` with the run's broker (questions), taint and
  `mcp-calls.jsonl` - exactly as for any MCP server attached during a run, including the administrator's policy and
  the security mode (a run that refuses `--mcp` refuses the bridge). For Claude Code the server is written into the
  sandbox's `~/.claude.json` (`mcp.agent_config`, recorded in `mcp-live.json`, so the run's end takes it out).
  The bridge is **not** kept in the start's options (`--mcp`): it is not restored on resume.
- **Relay configuration** (per side): `{"server": "bridge-PEER", "rules": [run's rules for tainted runs for that
  name, if the run has a kept start] + [the bridge ruleset]}`; the bridge ruleset marks the content tools untrusted:
  `{"name": "sandbox-ai bridge ID", "version": 1, "untrusted": [{"id": "bridge-peer-content", "server":
  "bridge-PEER", "tool": [inbox, ask, list_files, read_file, watch_activity, watch_llm, watch_changes,
  watch_terminal]}]}`.

## 4. Where state lives

`~/.local/state/sandbox-ai/bridges/` (0700):

| Path | Written by | What |
|---|---|---|
| `ID/bridge.json` | `add`, broker, `revoke`, `resume` (under `ID/lock`) | the grant (sides, capabilities per direction, observer actions, ask policy, limits, until), state (`starting`, `active`, `suspended`, `revoked`, `ended`, `failed`) and why, broker pid and start time, warnings, counters (calls, bytes today) |
| `ID/a/`, `ID/b/` | broker | each side's socket folder (`server.sock`) |
| `ID/approvals/` | broker (questions), `sandbox-ai approvals` and the app (answers) | questions of kind `bridge`, the same file protocol as a run's (`ID.json`, `ID.answer`, `ID.done`); listed by `approvals list` as sandbox `bridge-ID` |
| `ID/bridge.jsonl` | broker | every call of both sides: time, side, tool, decision, bytes, result (no content) |
| `ID/broker.log` | broker | its own output |
| `exposure/RUN.json` | brokers (under `exposure.lock`) | which origins' content entered RUN through which bridges after how many hops (section 6.4); tied to the run's launcher pid and start time, so a later run of the same session starts empty |
| `../bridge-host-id` | the first bridge across hosts | this host's bridge identity (16 hex digits, 0600): how its runs are named in another host's exposure (section 9.5) |

A bridge across hosts has a record on each host under the same ID: the local one names the far side with `host`,
`start_name`, `host_id` and what its half reported (`run`, `agent`, `server`, `network`, `ready`); the far host's
record has `remote_half: true` and `origin` (the name the local host gave itself), and lists, shows, revokes and
resumes there like any bridge.

In each run: the relay's `mcp-calls.jsonl` (every call, as for any MCP server), `activity.jsonl` events of kind
`bridge` (created, attached, message, ask, answer, read, write asked/allowed/denied, alert, hint, net-cut, pause,
suspended, revoked, ended) in both runs' log directories, and `mcp-live.json` (what was written into the agent's
configuration).

## 5. Capabilities as MCP tools

What a side's `bridge-PEER` server lists depends on the grant; a tool not granted is not listed, and calling it is
refused ("not granted on this bridge") all the same.

| Tool | Listed when | Result carries peer content (taints) |
|---|---|---|
| `info` | always | no: the grant, limits and state as the human set them |
| `inbox(wait_seconds=0)` | the peer may `message` or `ask` this side, or `hint` is on for the peer as observer | yes |
| `answer(question_id, text)` | the peer may `ask` this side | no |
| `send_message(text)` | this side has `message` | no |
| `ask(question, timeout_seconds)` | this side has `ask` | yes (the peer's answer) |
| `list_files(path)`, `read_file(path, offset, max_bytes)` | this side has `files:ro:P` or `files:rw:P` | yes |
| `write_file(path, content)` | this side has `files:rw:P` | no |
| `watch_activity(limit)`, `watch_llm(limit)`, `watch_changes()`, `watch_terminal(lines)` | this side has `watch` | yes |
| `alert(message, severity)` | `watch` (alert is on by default; `--observer-actions` without `alert` turns it off) | no |
| `hint(text)`, `cut_network(reason)`, `pause_peer(reason)` | `watch` and that action switched on | no |

### 5.1 message
`send_message` queues a note in the peer's inbox (at most 100 waiting). The peer's agent reads it with `inbox`
(optionally waiting up to 60 s for one). Delivery is pull-based in v1: an agent learns of new items when it calls
`inbox` (its tool description says to), the human can tell it to. A push into the agent's prompt is not done (it
would type into the human's terminal).

### 5.2 ask
`ask` queues a question (at most 4 open per asker) and waits until the peer answers (`answer(question_id, text)`)
or the timeout passes (default 300 s, per bridge `--limit ask-timeout=S`, at most 1800 s). The answer is peer
content. A question nobody answered is withdrawn from the inbox at its timeout.

### 5.3 files:ro:PATH / files:rw:PATH
PATH is relative to the peer's main workspace (`.` for all of it; an absolute path inside the workspace is taken
relative to it). Tool paths are relative to the workspace too and must lie below a granted PATH. `files:rw` implies
reading the same PATH. Refused always: `..`, absolute paths outside, NUL, any `.git` component, the peer run's
private and held trees (`workspace_checks`), canary decoy files of either run (reading one also suspends the bridge
and alarms the human). `write_file` creates or replaces one regular file whose folder exists (no folders made, no
links followed, never a link or a non-regular file replaced); the question shows the unified diff (text) or sizes
and SHA-256 (binary), whole: a diff (or any question's details) over 64 KiB, more than approvals and the app show,
is refused before anyone is asked; allowed writes are atomic (temporary file and rename in the same folder descriptor,
the existing file's mode kept).

### 5.4 ask policy (`--ask each|first|never`, default `first`)
For the calls that move content across (send_message, ask, list_files, read_file, watch_*, hint): `each` asks the
human every time; `first` asks for the first call of each tool, and an allow then holds for that tool for the
bridge's life (a deny with scope `run` refuses it for the bridge's life, a deny `once` only this call); `never`
asks nothing. `write_file` is asked every time whatever the policy; `cut_network` and `pause_peer` are the human's
own setting (switched on for the bridge), and are told to the human, not asked. A question unanswered within its
timeout (120 s; writes 300 s) is a refusal.

### 5.5 watch / observer
- `watch_activity(limit)`: the newest records of the peer's `activity.jsonl` (its run only): time, kind, summary
  (already redacted by the activity collector).
- `watch_llm(limit)`: summaries of the peer's LLM traffic log, when it has one (model, status, tokens, tool calls,
  a 200-character preview).
- `watch_changes()`: files of the peer's workspace added, changed or removed since the bridge started (a manifest
  taken by the broker at its start and kept in its memory: size, mtime, SHA-256 (files over 8 MiB by size and
  time only); at most 20000 files, `.git` and canary files skipped; unified diffs for text files up to 64 KiB whose
  content the baseline kept, 16 MiB of them in all). No `git` runs on the host in an agent's workspace (its
  configuration could run commands).
- `watch_terminal(lines)`: a read-only snapshot of the text on the peer's agent window (tmux window `agent`), with
  LINES (0-2000, default 0) of scrollback above it. It is made *inside* the peer's sandbox: the broker connects to the
  peer's shell relay (`tmux/shell.sock` of its session: the relay `sandbox-ai shell` uses, started by the guard inside
  Firejail with the agent's confinement; opened as `connect_relay` opens it - no link followed, a socket of this user
  only) and asks it, on pipes, for `tmux capture-pane -p -t sandbox:agent [-S -LINES]`; only frames come back. A tmux
  client on the host talking to the agent's tmux server is never used (the server is the agent's and could tell a
  client to run commands). The relay and whatever answers on its socket are the peer's, so the result is hostile:
  at most 1 MiB is read within 5 s (a stall, a flood, a frame announced as larger than that, or bytes that are no
  frames end the read: a refusal when nothing came, else a snapshot marked as cut; the relay ends what it started when
  the connection closes); then every escape and control sequence is removed (CSI, OSC, DCS/SOS/PM/APC, the other ESC
  forms, the C1 forms), any other character that is not printable is shown as `\xNN`/`\uNNNN` (BEL, NUL, bidi
  overrides), tabs become spaces, lines are cut at 400 characters, at most the newest 3000 lines and 128 KiB are kept
  (a note says so), and the text is framed and taints the reader like any peer content. One snapshot per 2 s per side
  (on top of the bridge's rate); asked like the other watch tools. A run without a relay (a start in the foreground,
  `--no-tmux`) is refused with that reason. Across hosts the peer's half makes the snapshot on its host (the tool is
  one of the forwarded peer tools), and the calling half cleans the text again (the cleaning is idempotent) before it
  frames it.
- `alert(message, severity)`: to the human: a desktop notification (unless the app or the web watches), an event
  in both activity logs and the bridge log, `bridge show` lists it.
- `hint(text)` (switch `hint`): a note into the peer's inbox, marked as coming from the observer.
- `cut_network(reason)` (switch `net-cut`): the peer's kill switch `api` (as `sandbox-ai net cut PEER api`, on the
  peer's own behalf, so `sandbox-ai net cut PEER off` lifts it), the human alarmed. The cut suspends the bridge.
- `pause_peer(reason)` (switch `pause`): the peer frozen (as `sandbox-ai pause PEER`); only the human resumes it
  (`sandbox-ai resume PEER`); no tool resumes anything.

## 6. Broker protocol and checks

### 6.1 One call
1. The relay of the calling side checks the call (rules, taint rules, its own approvals) and logs it.
2. The broker: the bridge is `active` (else refused with the reason: suspended by X, revoked, ended); the tool is
   granted; the rate limit; argument types and sizes; the transfer check (6.4); the ask policy (5.4).
3. The action; the bytes counted against the daily volume; the call logged (`bridge.jsonl`, both activity logs).
4. The result goes back through the relay, which taints the run first if the tool is a content tool.

### 6.2 Lifetime
`add` writes `bridge.json` (`starting`), starts the broker (a process of its own session, so a closing terminal does
not end it), waits for both sockets, attaches both sides, and sets `active`. If anything fails, everything done is
undone (`failed`). The broker ends the bridge when a side's run ends, at `--until`, or on `revoke`; it suspends it on
a kill switch or a canary event of either side. Default lifetime: until either session ends; never restored on
resume.

### 6.3 Egress warning
At `add`, the network permissions of both runs are read from their supervisors (`op show`: allowed networks, hosts,
TCP targets, DNS names, host ports, cut) and their VPN or Tor. For each direction in which the grant moves data
(A to B: message, ask, files:rw, hint; B to A: ask's answers, files:ro/rw reads, watch), the receiving side's
permissions are compared with the sending side's: when the receiver reaches something the sender cannot (or leaves
without the VPN or Tor the sender is behind), `add` warns, naming the direction and up to five examples: "data can
flow from A (more restricted) to B, which reaches what A cannot: github.com, ...". Allowed, never refused. Runs
whose permissions cannot be read are warned about as "could differ".

### 6.4 Exposure, hops and loops
When content of origin O crosses a bridge X into run R, R's exposure records O (hops = O's hops in the sender + 1,
the bridges it came through). A transfer from S to D over X is refused when 1 + the largest hop count in S's
exposure exceeds `MAX_HOPS` (2: A-B-C passes, A-B-C-D does not), or when D is among S's origins through a bridge
other than X (content of D would come back to it by another way: a loop). Replies on the same bridge are not loops.
Exposure is per run (like taint): it lasts until the run ends.

## 7. Approvals and logs

Questions are kind `bridge` in `bridges/ID/approvals/`, with `tool`, `from` and `to` (start names), a short
`arguments` line and `arguments_full` (the diff, or the call's arguments). `sandbox-ai approvals list|show|allow|
deny` and the app read them like a run's (the app's approvals scan includes the bridges' directories); an
`always` answer is taken as `run` (the bridge's life), and for writes as `once`. Records: the relay's
`mcp-calls.jsonl` in each run (as for every MCP server), `bridge.jsonl` in the bridge directory, `activity.jsonl`
events of kind `bridge` in both runs (Monitoring's activity pages and `sandbox-ai report` read them); `bridge show
ID` prints the grant, the state, the counters and the newest calls.

## 8. Failure modes

| Failure | Effect |
|---|---|
| The broker crashes | Both relays fail to connect (no socket): every call fails; `bridge list` shows the broker gone (`ended`, "broker not running"); `revoke` cleans up the relays and configurations. |
| A side's run ends | The broker ends the bridge within a second, closes the other side's relay and configuration; open asks fail. |
| A side is paused | Its relay is frozen; the other side's ask times out; reads of its files and watch still work (host side). `revoke` cannot reach a paused side's supervisor: its relay stays listed until the run ends, but the broker's socket behind it is gone, so every call through it fails (the run's end takes the configuration out). |
| A net cut or canary alarm of either side | Suspended until `bridge resume ID` (refused while the cut or the alarm holds) or `revoke`. |
| The human does not answer a question | Refused after its timeout, the agent told so. |
| The agent reads its MCP servers only at its start (Codex, OpenCode, others) | The relay is open, but the agent does not see the server until it restarts, and a restart does not restore the bridge (not in its options). `add` says so. Claude Code takes it up live (`/mcp`). An open item: a run-only start option that carries a bridge's relay into a restart. |
| The peer's workspace is read-only | `files:rw` is refused at `add`. |
| A different machine (`--podman-url`) or macOS podman machine | Refused at `add` (no descriptor can be handed to the VM), as `mcp attach --live` refuses. A session on another *host* of `sandbox-ai hosts` is bridged across hosts (section 9). |
| The SSH connection of a bridge across hosts drops | Both halves end it (section 9.4). |
| The other host's sandbox-ai is older than 0.1.97 | Refused at `add`, naming its version and `sandbox-ai hosts update HOST`. |
| Both sessions on two other hosts, either older than 0.1.98 | Refused at `add` (its half cannot describe its session), naming the host and `sandbox-ai hosts update HOST`. |
| This machine sleeps or loses either host while it relays a bridge between two other hosts | Both halves hear nothing from the relay for 75 s (or see the connection end) and end their side, their record marked `link_lost`. The relay sees the sleep (the wall clock ran ahead of the monotonic one by more than 75 s) or the lost leg, says "reconnecting" and starts both halves again (for 15 minutes, 2-30 s apart): each rejoins (`open` with `rejoin`, feature "rejoin" in its hello, 0.1.99) only when its own record says the link was lost less than 24 h ago and the side, both sessions' runs, the grant, questions, limits and time are exactly the ones it had (bridge_remote.rejoinable); else it ends, with why. |
| This machine shuts down while it relays | The halves end the bridge after 75 s of silence; nothing re-establishes it (the relay's process is gone). A bridge of two sessions of one other host is made there and does not depend on this machine. |

## 9. Across hosts

Peers on the user's other sandbox-ai hosts (`sandbox-ai hosts`), over SSH, only to hosts the user added.

### 9.1 Naming and refusals
`bridge add A HOST:B ...` (or `HOST:A B`) names a session of HOST by its start name. HOST must be a name of
`hosts.json`; `local:NAME` is this machine's. Anything else with a colon is refused as "never an SSH address" (a
`user@1.2.3.4:x` or `-oProxyCommand=...:x` is not a host name). Both sides may be on other hosts (section 9.7). A
path in `files:ro|rw:PATH` for a far side must be relative, or below `/work` as its agent sees it (its workspace is
not known here before its half answers). A host whose sandbox-ai is known to be
older than 0.1.97 (`hosts.ask_version`: its last status, else asked) is refused before anything is started.

### 9.2 Two halves instead of one broker and a relay
The first design had the local broker decide every call and the remote stub relay raw MCP lines
(`{"conn": N, "line": ...}`) between the far side's socket and SSH. Built that way, the far side's files, approvals,
canaries and kill switch would have been judged by a process that cannot see them, and the remote stub would have
had to trust whatever MCP the local broker sent. Instead **each host runs a half: the same `bridge.Broker` for its own
side**. The local half is the broker `bridge add` starts; the far half is `sandbox-ai bridge-stub ID` on HOST, which
the local broker starts through `hosts.argv` and which is a broker in its own process there. Each half:

- listens on its own side's socket and attaches the bridge to its own side's sandbox (`mcp-open`, its own relay
  configuration: the bridge ruleset that marks the content tools untrusted is built there, so its supervisor taints
  that run whatever the other host says);
- answers its own side's `info`, `inbox` and `answer`, and does what the other side's calls do *to its side*: the
  file walker (section 5.3, with its own run's private trees and canaries), watch (its own logs and baseline), the
  inbox and open questions, `cut_network` and `pause_peer` (its own run's kill switch and pause);
- asks **its own human**: the questions about its side's files (and its side's inbox) are in its own
  `bridges/ID/approvals/`, so `sandbox-ai approvals` on that host and the app's approvals page (which lists every
  host's questions, tagged with the host) show and answer them where the files are;
- keeps its own limits (the rate per side, sizes, the day's volume, open asks, alerts), its own `bridge.jsonl`, the
  activity log of its run, its run's exposure, and checks its own run, `--until`, kill switch and canaries every
  second.

A call by side X of a tool that acts on the peer (`send_message`, `ask`, `list_files`, `read_file`, `write_file`,
`watch_*`, `hint`, `cut_network`, `pause_peer`) is checked by X's half first (granted, the rate, the argument types,
the path below a granted path, the sizes: a path outside the grant never leaves X's host), then sent as a `call`
frame; the peer's half checks it again against **its own copy of the grant** (taken apart and rebuilt from the
`open` frame by `check_grant`, never widened afterwards: no frame changes a grant) and does it. A `call` frame names
no side: what arrives at a half is always the other side's. Only peer tools are served (`info`, `inbox`, `answer`
asked of the other half are refused). Results: content (`read_file`, `list_files`, `ask`, `watch_*`) is framed by
the receiving half as the peer's content (the far half's framing is not trusted) and taints through the relay; the
result of a call without content is worded by the receiving half (`sent; ...`, `written: PATH`), never taken from
the other half (its words would reach the agent untainted); a refusal from there is marked `refused on HOST:`,
escaped and cut to 400 characters.

Since each half is a `Broker`, the bridge's security review holds on both: the far half reaches its workspace from
`/` without following a link, refuses a question whose details it cannot show whole (a write's diff is bounded in
size and time there, made by its own `--internal-diff` process), ends a question that waits when the bridge is
suspended on either host and checks the state again after the answer, bounds its side's questions and calls in
flight (the other side's calls over the link count against its 64, and at most 16 are served at once), and counts
every call against the rate. Content that crosses is framed by the reading half with an end marker made for that
result there.

### 9.3 The link
The local broker runs `ssh ... sandbox-ai bridge-stub ID` with the host's own settings (`hosts.argv`: BatchMode, the
host key learnt once and checked strictly, its key, port and `command`), and in front of them (ssh takes the first
value of each option): `ClearAllForwardings=yes`, `ForwardAgent=no`, `ForwardX11=no`, `PermitLocalCommand=no`,
`Tunnel=no`, `ControlMaster=no`, `ControlPath=none`, `RequestTTY=no`, `-e none`. It is a connection of its own,
not the shared master of `sandbox-ai hosts` (that one is recycled after a minute of quiet, and a stop of it would cut
every bridge). No port is opened on either host. The stub moves stdout aside at its start (whatever else prints goes
to stderr), so stdout carries frames only.

Frames are JSON lines of at most 4 MiB: `{"t": "hello", "protocol": 1, "version", "host_id"}` once from the stub;
`{"t": "req", "id", "op", "body"}` / `{"t": "res", "id", "body"}` (ops: `open` once, then `call`); `{"t": "ev", "op",
"body"}` with op `state` (suspended, and why), `resume`, `alert`, `end` (and why), `ping` (every 15 s from each half).
A frame that is too large, not JSON, not an object, of no known kind or event, a call without its number, an answer
that is not well formed, more than 200 frames a second (400 at once) or more than 10 changes of state (`state`,
`resume`) a minute is a protocol violation: the half says so (`end`) and the bridge ends on both hosts. At most 16
calls of the other half are served at once (more: refused, not queued); every call waits at most as long as the work
there can take (an ask: its timeout plus a question to the human).

### 9.4 Lifetime
- `add`: the broker starts the stub; the stub's `hello` must come within 45 s with protocol 1 (an older sandbox-ai
  does not know `bridge-stub` and sends no hello: refused, naming 0.1.97 and `sandbox-ai hosts
  update HOST`); the `open` carries the bridge's ID, the far side's start name, the local side (its name, run, agent,
  this host's identity), the grants, the ask policy, the limits and `--until`. The stub refuses an ID it has had
  before (a bridge is never opened again: no reconnect after a revoke), checks that the session runs there, that
  `files:rw` goes only to a writable workspace, writes its record, attaches its side and answers with its side's
  name, run, agent, server name and network permissions (for the egress warning, which `add` prints as for a local
  bridge). Then `add` attaches the local side and activates. A failure on either host undoes both.
- A dropped SSH connection ends the bridge on both hosts (not a suspension, as first designed: the far half closes
  its side's relay when its stdin ends, so there is nothing left to resume; adding it again is one command). A revoke
  on either host ends both (the half that ends tells the other, with the reason). The end of either run ends both.
  A connection that drops without a word (a laptop that sleeps) ends it too: this machine's ssh gives up after its
  keepalives, and a half that hears nothing from the other for 75 s (not even its `ping`) ends the bridge - sshd on
  the other host would keep the stub's end open for hours.
- A suspension on either host (a kill switch, a canary, an observer's cut) suspends both halves. `bridge resume ID`
  on either host lifts it there and tells the other half, which lifts it too unless its own cause still holds (then
  it says so, and both stay suspended).
- `bridge list`, `show`, `revoke`, `resume` work on both hosts (`list --json` adds `a_host`, `b_host`, `remote_half`;
  the app marks only the sides without a host as its own sessions, since a run id of another host may be a local
  one's too). `bridge add --dry-run` with a side on another host checks this machine's side and the grant and starts
  nothing there: its note says the far session, workspace and network are checked when the bridge is added.

### 9.5 Exposure across hosts
Each host keeps the exposure of its own runs (section 6.4). A call that carries content into a run brings the
sending run's exposure along (in the `call` frame, or in its result), and the receiving half checks hops and loops
for its own run and records it. Runs of another host are named `HOST-ID:RUN` (`HOST-ID`: that host's bridge
identity, `bridge-host-id`); a host takes its own identity off again, so content of a run that comes back to it over
another bridge is recognised as a loop. An exposure that is not well formed (more than 64 origins, a hop count out of
range, an unknown kind of name) refuses the call. Limits: a half reports its run's exposure truthfully only as far as
the other host is trusted (the threat model's: the host and sandbox-ai's own processes are, the agents are not); a
compromised host could hide hops.

### 9.6 What a hostile agent still cannot do across hosts
Everything of section 2 holds per host. In addition: an agent cannot reach SSH or the link (the stub runs on the
host, `bridge-stub` refuses to run inside a sandbox); cannot name a host (only the human's `bridge add` does, and only
hosts of `hosts.json`); cannot widen what the other half allows (its grant is its own); cannot make the other half
read or write beyond its granted paths (checked on both hosts); and cannot keep the far relay alive past the end of
the connection.

### 9.7 Both sessions on other hosts (0.1.98)

**Two sessions of one other host** (`bridge add lab1:a lab1:b`): the bridge is made *there*. `add` checks what it
is given here (the capabilities, the actions, the limits; `--until` is turned into the time it names on this
machine's clock, never an `HH:MM` read on the other host's), then runs `sandbox-ai bridge add a b ... --json` on
lab1 over SSH (`hosts.run`, as `sandbox-ai hosts run lab1 -- bridge add` would). The bridge is lab1's own: one
broker there, both relays attached there, its questions asked there - no frame passes through this machine, and it
lasts while lab1 runs, whatever this machine does. Here it is named `lab1:ID`: `bridge list --hosts`, `bridge show|
revoke|resume lab1:ID` run on lab1 the same way. Nothing is widened: the human could type the same command on lab1;
lab1 refuses what it refuses for its own bridges (a session that does not run, `files:rw` into a read-only
workspace) and says so, with its name.

**Two sessions of two other hosts** (`bridge add lab1:a lab2:b`): this machine *relays*. The designs weighed:

1. *One host connects to the other* (lab1 runs `bridge add a lab2:b`, its broker starts lab2's half over SSH): lab1
   would need lab2 in its own `hosts.json` and a key that logs into lab2 - a way from one host into the other that
   the human never set up and that outlives the bridge. A host taken over through its session (or otherwise) could
   use it. Refused as a widening.
2. *The relay* (built): this machine already holds an SSH connection to each host for `sandbox-ai hosts`, and each
   host already accepts this machine's half of a bridge. The broker process of the bridge here (`bridge.py
   --internal-broker ID`, `bridge_remote.Relay` because both sides are remote) starts **both** halves, each
   `sandbox-ai bridge-stub ID` over its own connection with the options of section 9.3, and passes the frames of
   the one to the other.

The relay's start: both hellos (each must say `features: ["describe"]`, 0.1.98: an older half is refused with its
host and `sandbox-ai hosts update`); the two bridge identities must differ (two names of one machine are refused:
bridge it there); each half is asked to `describe` its session (it runs there, its run, agent and workspace's mode:
the other half's record of its peer needs the run, for the exposure names of section 9.5, and `files:rw` into a
read-only workspace is refused before either half opens); then both are opened with the human's grant (the same
for both; each half takes it apart and keeps its own copy, as in 9.2), the other's session (`origin`: this
machine's name for the other host, so lab1 names it `lab2:b`) and `relay`: this machine's name (recorded on each
host as `relayed_by`). Each half attaches its own side, answers with its side, and from then on is exactly the half
of section 9.2: it checks its own run, kill switch, canaries and `--until`, asks its own human, keeps its limits,
logs and exposure, and frames and taints what crosses into its run.

What the relay does, and what it never does:

- It has **no side**: no socket, nothing attached to a sandbox here, no files read or written, no question asked here
  (writes and reads are asked on the host where the files are; the app's approvals page lists every host's).
- It **never makes a call**. A `call` frame from side X's half is passed on to the other half only when X was
  granted the tool (the tools of X's server under the grant), the path lies below a path granted to X (`..`, `.git`
  and absolute paths refused as everywhere), the bridge is active, and X stayed within twice the bridge's rate (the
  halves keep the rate themselves; this only bounds what a half that ignores it costs this machine). Anything else
  (`open`, `info`, `inbox`, `answer`, a tool not granted) is refused here with "sandbox-ai bridge (relay): ...",
  logged, and never reaches the other host. The other half checks the call again against its own grant.
- Answers go back cut to `{ok, text, exposure}`; one that is not well formed ends the bridge (a protocol violation
  of that host). The receiving half frames the content with its own end marker and words the results without
  content itself (9.2): nothing the relay passes on reaches an agent as sandbox-ai's own words.
- Events pass on: a suspension of either half (`state`) suspends the other and the relay's record; a `resume` (the
  human on either host, or `bridge resume ID` here, which the relay sends to both) lifts it on the other unless its
  own cause holds (it says so: all stay suspended); an observer's `alert` is passed on (the other half checks its
  own grant) and recorded and notified here, rate limited; `end` from either ends the bridge on both. A half
  applies the other's suspensions and resumes one after the other, in the order they came.
- Its link limits hold per leg (frame size and rate, 16 calls in flight, 10 changes of state a minute, pings every
  15 s, 75 s of silence ends it). Its log (`bridges/ID/bridge.jsonl`) has the side, tool, outcome, size and time of
  each call, never content; calls refused for the rate are one line a minute.
- Its end: a revoke here (the relay tells both halves why), a revoke on either host, the end of either session, a
  `--until`; a dropped or silent leg or a sleep of this machine is re-established when both sessions still run
  (rejoin: the same grant, checked by each half against its own record), else it ends. When this machine shuts
  down, both halves end it (said at `add`, as a note, and on the dry run).

Why the relay widens nothing: the human's grant is given once, at `add`, and fixed in each half at its open - the
same as a bridge with one far side, where this machine's broker gives the far half its grant. The relay holds no
more than a broker with one far side (it is this machine's sandbox-ai, trusted as in section 2); it holds less (no
side), and every call it passes is checked on both ends besides. No new listener, no new credential, no path
between the two hosts: each leg is an outbound SSH connection of this machine to a host the human added. The content
that crosses passes through this machine's memory (never its disk); this machine can already read both workspaces
over the same SSH connections, so that exposes nothing new. Hops: a relayed bridge is one bridge (the relay carries
no run); exposure crosses as in 9.5, the runs named by their hosts' identities.

## 10. How it is tested

- `tests/test_bridge.py`, `test_bridge_remote.py`, `test_bridge_relay.py`: the grant, the walker, limits, hostile
  agents and hostile halves, with registered runs (real supervisor control sockets and MCP relays) and brokers in
  threads.
- `tests/test_bridge_e2e_processes.py`: the same runs, everything else in processes of its own - the broker
  `bridge add` starts, each agent's MCP client (`runtime.py --internal-mcp-bridge` on its relay), the human's
  `sandbox-ai approvals` and `bridge list|show`, and across hosts the other hosts' halves (`bridge-stub`) reached
  through a stand-in `ssh` (each host this machine with its own state): message, ask (answered and timed out), files:ro and
  files:rw (asked, the diff shown, allowed and denied), watch and the observer's actions, the limits, a kill switch and
  a canary suspending it until `resume`, a session's end, `--until`, both directions, revoke; made on the other host
  and relayed between two.
- The live check `bridge-live` (`scripts/live_check.sh`, docs/LIVE_CHECK.md): two real sessions (Podman and
  Bubblewrap), each call made inside the sandbox through its real relay, and the app's New bridge… between them.
- The app: `projects/sandbox-ai-app/tests/test_bridge_clicks_app.py` clicks New bridge… in the real window in every
  state of the Sandboxes page (no session, one, two, only on hosts, a slow host, a host that answers garbage, still
  reading, an error, German, after a page switch, the right click) and goes through the real dialog and question.

## 11. CLI

```
sandbox-ai bridge add A B --allow CAP[,CAP...] [--both] [--until T] [--ask each|first|never]
                     [--observer-actions alert,hint,net-cut,pause] [--limit KEY=VALUE ...] [--json] [--dry-run]
                     (A or B or both: HOST:NAME, a session on a host of sandbox-ai hosts)
sandbox-ai bridge list [--all] [--hosts] [--json]     --hosts: every host's bridges too (HOST:ID), each once
sandbox-ai bridge show [HOST:]ID [--json]
sandbox-ai bridge revoke [HOST:]ID|--all
sandbox-ai bridge resume [HOST:]ID   lift a suspension (a cut or a canary alarm) once its cause is gone
sandbox-ai bridge-stub ID            the other host's half of a bridge across hosts (started over SSH, not typed)
```

`--until`: `30m`, `2h`, `1d`, `HH:MM` (today, or tomorrow when passed) or an ISO time. `--limit`: `rate=N` (calls
per minute and side), `size=BYTES` (one message, question or answer), `ask-timeout=S`, `daily=BYTES` (volume per
day), sizes with k/m suffixes.

## 11. Presets that propose a bridge

A preset may carry a `"bridge"` section (`sandbox/presets.py`: `bridge_problems`, `bridge_proposal`,
`propose_bridge`): the parts of `bridge add` (`allow`, `both`, `ask`, `observer_actions`, `limits`, `until`, and
`direction`: `out`, the start reaches the peer, or `in`) and a `peer` that must be a value of kind `session`. The
built-in ones: `observer` (watch, alert), `pair-reviewer` (ask, files:ro below a folder the human gives) and
`docs-from-code` (message, files:ro). What holds:

- **A preset only proposes.** Nothing is made from the preset file: the proposal reaches the start
  (`a.preset_bridge`, from `presets.apply` on the command line's `--preset`; never from a project file, a kept start,
  a session's saved options or a restart), and a new session asks once it is up, before its terminal attaches
  (`sessions.start` -> `propose_preset_bridge`): the peer (given with `--preset-value`, else picked from the running
  sessions), then `bridge add --dry-run`'s warnings, then "[y/N]"; the yes runs `bridge.main(["add", ...])`, the very
  code of the command, with every check of section 6. Anything else, an error or the end of input makes nothing and
  says the command. Without a terminal, and for a start in the foreground (no session to ask once it is up), only the
  exact command is said; `--yes` does not answer it.
- **The peer is never fixed in the file**: a session value has no default and is the peer only (never an option, a
  server, a path or the task's text); the human names it or picks it.
- **Shape checks like the rest of a preset**: unknown keys refused, capabilities from the known set, paths relative to
  the peer's workspace (no `..`, `.git`, control characters or commas; a `{value}` of kind text may stand in one and is
  checked again once filled in), the limits' ranges and actions are bridge.py's own (a test keeps them equal),
  `until` a duration or HH:MM (never a date a passed-on preset would outlive), `"ask": "never"` only in a preset of
  the user's own.
- **Agents**: only Claude Code takes an MCP server while it runs, so the built-in ones are for it; a start of another
  agent whose side would get a tool of the bridge says so (`bridge_agent_note`), as the Board's `bridge_problems`.
- **The app** turns the section into a Bridge part of the preset's board (peer left to choose; a path's value a
  placeholder of the board), made by the Board's own flow once the start runs (its dry run and question). **The web
  interface** starts with `--preset` in a terminal: the launcher asks there; the form offers the host's running
  sessions for the value and says what is proposed.
