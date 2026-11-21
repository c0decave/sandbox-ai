# Security review (sandbox isolation), part A: local channels and credentials

What became of the findings SAI-08, -12, -14, -15, -18, -19, -21, -22, -23, -24 and -26 of
`/work/sandbox-security-review.md` (a passive review of a running sandbox-ai 0.1.99 sandbox: rootless Podman,
Firejail 0.9.72 inside). The review's text was treated as claims: each finding was checked against the code of
`main` (0.1.116, commit 2c5fbadd) before anything was changed. Branch `security-fix-A`; line numbers are those of
that branch.

## The one fact behind most of them

The agent and every command it runs are **one user (the sandbox's root, your user on the host) in one sandbox**:
the guard executes the agent (`runtime.py:4567 inner_guard`), the agent starts its commands itself, and HOME is one
writable folder for both (`agent_sandbox.py:10672`). File modes, a socket's mode, `SO_PEERCRED` and a process's
ancestry do not tell the agent from a command of the agent: the command has the same user, is the agent's child,
and can start the agent's own helper programs with input of its choosing. SAI-18, -19, -21, -22, -23 and the rest
of -26 are this one fact seen at six doors. What can be closed without parting the two is closed below; the rest
needs the two parted, and the last section says how.

## The findings

| Finding | Verdict | Where (branch) | Severity (review) | Fix | Next step |
| --- | --- | --- | --- | --- | --- |
| **SAI-14** attach and shell socket names in a folder the sandbox writes | **Right.** Both relays bound `tmux/attach.sock` and `tmux/shell.sock` themselves, in the session's tmux folder, bound read-write into the sandbox; the host's client checked type and owner only. `tests/test_deep_round_2_097.py::test_journey_a_hostile_agent_on_the_shell_relays_socket` already showed on `main` that a fake relay there receives the request (`argv`) - as accepted behaviour. | `runtime.py:68` (RELAY_DIR), `:244` SealedRelays, `:360` take_relays, `:4440` firejail_command, `:4509` tmux_command, `:4567` inner_guard; `sessions.py:567-595` | High | **Fixed**, 14cc2ebb. The supervisor binds both sockets in `tmux/relay` before Firejail starts and hands the listeners to the guard once (SCM_RIGHTS; the guard is the only process of the sandbox that exists then); Firejail mounts the folder read-only (a mount point: not renamed or removed either); the guard's script gives each relay its listener and closes both for tmux, the agent and everything found on the agent's PATH. The client takes the place from the session's record (`shell_relay: 2`, written by the host), never from the folder: no fallback. A session started before keeps its place. Tests: `tests/test_security_review_relays.py` (16). | A live start (`tests/test_agent_sandbox_live.py::test_real_session_relays_cannot_be_replaced_from_inside_and_untrusted_is_confined`, `--agent-live`) - not runnable in the sandbox this was made in. A workspace that contains sandbox-ai's own state folder reaches `tmux/relay` by another path (the launcher warns about such a workspace; unchanged). |
| **SAI-08** raw terminal bytes on the host's terminal | **Right** for `attach` and `shell`. Partly mitigated before: the tmux in the images passes no title or passthrough by itself, and the desktop app's terminal asks before an OSC 52 write and never answers a read. But the workload controls the tmux server (SAI-21), so anything can come. | `runtime.py:596-900` (TerminalFilter, terminal_filter), `:904` attach_client, `:1209` shell_client; `agent_sandbox.py:8117` machine_exec | High | **Fixed** for attach and shell, 14cc2ebb. A stateful parser on the human's side: text (valid UTF-8, never C1), drawing CSI/ESC sequences of known shape, colours; dropped whole: clipboard and title queries, titles, OSC 8/7/9/50/777/1337, printer and window operations, DCS/APC/PM/SOS, ENQ, malformed, cut off or too long. Default: queries answered with numbers or the terminal's own name pass (cursor position, device attributes, mode state, sizes, colours) and a clipboard text may be *set*; `SANDBOX_AI_TERMINAL_FILTER=strict` passes none of both, `off` nothing is filtered. A file or pipe is never changed. Tests: `tests/test_security_review_terminal.py` (141: shapes, every cut of a stream, a fuzz against an independent reader, both commands through `./sandbox-ai` on a pty). | **Open:** a start in the foreground (a task, `--no-tmux`, `sandbox-ai login`) has Podman's own terminal - the launcher is not in that path; it needs a pty relay in the launcher. The review's letter ("no query is answered") is `strict`; the default lets the number-only queries through because interfaces stall or end without them (crossterm's cursor position). |
| **SAI-12** connection caps of the services the agent reaches | **Right** for the proxy (`accept_loop`: a thread per connection, no ceiling), the hook server and both relays; the MCP relay (16) and the browser relay (8) were bounded already. | `runtime.py:9507` PROXY_CONNECTIONS, `:9950` refuse_many; `:12805` HOOK_AT_ONCE; `:89` RELAY_AT_ONCE, `:212` serve_relay | Medium | **Fixed**, 14cc2ebb: 512 proxy connections, 64 hook messages, 16 attached terminals, 32 shells at once; one more is reset or refused in the accepting thread, before a thread, pty, process or parser exists for it; the proxy's log says it at most every 10 s with the count. Tests: `tests/test_security_review_services.py`, `test_security_review_relays.py`. | **Open** (the supervisor's and host's side, part B's SAI-09/10 territory): separate cgroups with their own `pids.max` for supervisor, proxy and workload, per-service `RLIMIT_NOFILE`. |
| **SAI-15** "allow once" = an address and port for 30 s | **Right**, and said so in the README ("the address is open for 30 seconds"). | `runtime.py:4895` once_allowed, `:13643` _commit, `:13788` allow_once, ProxyServer.handle | Medium | **Fixed** for starts with the connection proxy (the default), 14cc2ebb: a target allowed once is no entry of the proxy's allow-list any more (`"once"` beside `"tcp"`); only a connection that waited for that question passes on its answer; any other - another process, another TLS name on the same address - asks for itself. The firewall's rule stays for the proxy's own connection. | With `--no-proxy` the firewall's 30 s rule is all there is (documented). Binding the answer to the claimed name as well (a connection that waited with another SNI) is not done: the question is keyed by address and port. |
| **SAI-26** OAuth callback to whichever process owns the loopback port | **Right.** `oauth_callback` connected to the port; for `localhost` it tried `::1` first, so a process on `[::1]:PORT` beside the agent's `127.0.0.1:PORT` got the code. | `runtime.py:14717` oauth_listeners, `:14737` oauth_listener_refusal, `:14809` oauth_callback, control op `oauth-listener`; `oauth_relay.py:454` pin_listener, relay_once, start | High (conditional) | **Fixed in part**, 31fa0fe5. The supervisor looks who listens before it connects: two listening sockets of different processes - delivered to neither; the host keeps the socket that listened when the human allowed the login (`approvals/ID.listener`) and the supervisor delivers to that socket or to none; the answer to "allow" and the callback's record name the process. The agent's page was already shown inert (CSP `sandbox`), the catcher one-shot and bound to the login's state. Tests: `tests/test_security_review_oauth.py` (15), `tests/test_browser_callback_e2e.py`. | **Open:** a process that listens *before* the agent and asks for the login itself (it is told apart only by the name the answer shows); a question forwarded from another host is not pinned (its catcher runs elsewhere; the two-listener check holds there too). Closing it needs the login's listener outside the workload's reach (below). |
| **SAI-23** MCP relays do not authenticate the agent client | **Right** in both halves: any process of the sandbox can speak to a relay (the peer is recorded, never judged), and only `tools/call`/`tools/list` were gated - the README said so ("MCP rules cover `tools/call`, not `resources/read` ..."). | `runtime.py:1771-1810` McpConnection.PASSED, acts, act | Critical/High | **Fixed in part**, 14cc2ebb: every request that makes a server act (`resources/read`, `resources/subscribe`, `prompts/get`, `completion/complete`, unknown methods) is judged by the server's rules under its method's name, asked, recorded, and its result taints like a tool's; a request without an id is not passed on. Only the handshake, `ping`, the log level, `*/list` and notifications pass unjudged. Tests: `tests/test_security_review_mcp.py` (8). **Behaviour change:** a ruleset whose `mcp` default is `deny` or `ask` now holds for resources and prompts. | **Open:** who the client is. A command run with `--untrusted` cannot connect (kernel permitting, below); any other command of the agent can. An answer "for the run" covers the tool under that rule, by design; `--mode paranoid` and the policy's `max_approval_scope: once` make every answer one call's. |
| **SAI-22** hook socket requests not authenticated | **Right.** `decide.sock` was 0666, the message was parsed for whoever connected. Partly mitigated before: an answer is bound to the exact call (digest); "for the run" is not, by design. | `runtime.py:13102` HookServer, `:13135` peer, serve, answer | High (conditional) | **Fixed in part**, 14cc2ebb: the socket is the agent's user's alone (0600); a peer of another user, or one that is no process of the sandbox, is refused before a byte is read and said as a safeguard (never a tool record); the asking process (pid, program, parents) is in `tools.jsonl` and in the question; 64 at once. Tests: `tests/test_security_review_services.py`. | **Open:** a command of the agent that starts the hook's client with a message of its own - same user, same parent, same program. Needs the agent parted from its commands (below); `--untrusted` commands cannot reach the socket. |
| **SAI-18** agent credentials and state readable and writable by the workload | **Right**, and by construction (above): `~/.codex/auth.json`, `~/.claude`, transcripts, `/proc/AGENT/environ`, `/sandbox/session/config.json` (`env` with the run's secrets, `agent_sandbox.py:13746`). | `runtime.py:16250-16490` (untrusted, landlock_restrict, untrusted_paths) | Critical | **Fixed for commands run through the new executor**, c60516ec: `python3 -I /sandbox/runtime.py --untrusted -- COMMAND` confines the command and all it starts with Landlock, for good: no access to the agents' state and the credentials below HOME nor to `/sandbox`, HOME read-only, another process's environment, memory and descriptors out of reach, no secrets in its environment; not run when it cannot be confined. Tested under this kernel's real Landlock (ABI 10): `tests/test_security_review_untrusted.py` (10). Every agent's sandbox-ai skill tells it to use it. | **Open:** nothing makes an agent send its commands through it. The design below. Until then the review's containment advice stands: no hostile code in a sandbox that holds a login you care about; `--token-proxy` and `--api-key` keep Git and API keys out of the sandbox already - an OAuth login of Codex or Claude is in it. |
| **SAI-21** the tmux control socket shared by agent, shell and hostile commands | **Right**: one tmux server (`tmux_command`), its socket in `TMUX_TMPDIR=/sandbox/tmux` (`agent_sandbox.py:10324`), the same user. | `runtime.py:4509` tmux_command; `--untrusted` as for SAI-18 | High | **Fixed for `--untrusted` commands on kernels with Landlock's right for a Unix socket's path** (asked of the kernel, not inferred from a version; this kernel has it): they cannot connect to tmux, and (Linux 6.12) cannot signal it. Otherwise **open**. The relays (SAI-14) no longer depend on the tmux folder. | A tmux server the workload cannot reach needs the same parting as SAI-18. Not claimed: the window "shell" is a shell of the sandbox, as README says ("under exactly the agent's rules") - never a trusted console. |
| **SAI-19** a Codex loopback listener | **Observed, not reproduced here** (no Codex in the sandbox this was made in). It is Codex's own listener: sandbox-ai neither starts it nor passes an option for it (no `app-server` in the launcher), and the firewall accepts the sandbox's loopback (`agent_sandbox.py:6009`, `:6021`), as every local server of a project needs. Other agents have the same (OpenCode's server). | - | High (assurance) | **Open.** Nothing sandbox-ai can do to another program's listener in the workload's own network namespace is safe to do blind. | The executor can refuse TCP connections to the ports that listened when it started (Landlock's network rules, ABI 4: one rule per allowed port) - designed below, not built: it wants a measured cost (65 000 rules per confined command) and a test against a real Codex. Upstream: ask for a Unix socket or a capability token on that listener. |
| **SAI-24** GUI selection and clipboard forwarding | **Mostly not as described.** The agent's display (`--gui`, `--gui-vnc`: every program of the sandbox an X client) is always started with `-nosel` (`agent_sandbox.py:4009` gui_config never asks for a selection; `runtime.py:2953`). Only the browser's own display (its own container, no abstract X socket) passes the clipboard, and the app's viewer shares it only while *Share the clipboard* is ticked (off by default, said in README). | `tests/test_security_review_services.py::test_the_agents_display_is_never_started_with_a_shared_selection` | High (conditional) | **No change needed for the finding as written**; a regression test keeps it so. | **Open, small:** a third-party VNC viewer on the browser's socket shares its clipboard by its own default (TigerVNC: on), and an agent that drives that browser (CDP) can read what arrives. Next: no selection for the browser's display in mode `high`, with the app's checkbox saying so (app and launcher together). `paranoid` has no display at all. |

## What was verified, and how

- Every fix has tests that fail on `main` and pass on the branch; the new files are `tests/test_security_review_
  {relays,terminal,services,mcp,oauth,untrusted}.py` (206 tests; on `main` 170 of the first 174 fail, and the two files
  for the relays and `--untrusted` do not load). The reproductions run inside this sandbox against
  fixtures only: sockets and processes of the tests, a made-up HOME with made-up tokens, never a login of the sandbox.
- User-visible changes go through the real entry point: `./sandbox-ai shell` and `./sandbox-ai attach` on a real
  pty with a relay that prints a hostile stream; `./sandbox-ai approvals allow` with a real control socket and a
  real login server for the callback.
- `--untrusted` runs under the kernel's real Landlock here (ABI 10): the reads, writes, renames, links, `/proc`
  reads, signals and Unix-socket connections of the tests are refused by the kernel, not by a fake.
- The whole core suite, in CI's four parts (`ls tests/test_*.py | awk 'NR % 4 == part'`), on the branch's last
  commit: 3280 passed, 41 skipped; 4467 passed, 176 skipped; 2366 passed, 42 skipped; 9264 passed, 80 skipped - 19377
  passed, 339 skipped, none failed (one warning, a thread of an older approvals test that outlived its folder). The
  app is unchanged; its tests around terminals, approvals, the login callback and the doctor: 129 passed, 21 skipped.
- **Not verified here:** a real start. There is no Podman and no usable Firejail in the sandbox this was made in, so
  the hand-over of the relays' sockets through a real Firejail (`--read-only` on a folder below a bind mount) is
  proven only by its parts. The live test above must pass (`pytest --agent-live tests/test_agent_sandbox_live.py -k
  relays`) before a release; if Firejail refuses the read-only mount there, the session does not start (it fails
  closed, with the reason in `supervisor.log`).

## Parting the agent from its commands: the design

The goal is the review's: a command the agent runs cannot read or change the agent's login, state, terminal, hooks
or MCP relays, although sandbox-ai does not control how an agent starts its commands.

**1. The executor (built: `--untrusted`).** Landlock needs no privilege, works inside Firejail and rootless Podman,
and cannot be dropped by what it confines. Cost: one listing of each folder on the way to a
protected one (never what lies below the other entries) and one rule per entry beside the way, at most 10000 of one
folder (the rest unreadable, said); HOME is read-only for the command, so a tool that writes a new dotfolder in HOME fails
(`--write PATH` names what it may). Limits: Linux 5.13 (files and `/proc`), 6.12 (signals, abstract sockets), and a
kernel with the Unix-socket path right for tmux, hooks and relays; `/tmp` is shared with the agent.

**2. Every command of an agent through the executor (not built).** Per agent, where the agent has a documented
place for it - never by replacing `/bin/bash` (the agents' own hooks, MCP bridges and Git credential helper run
through a shell too, and must stay outside):

- Claude Code: `CLAUDE_CODE_SHELL_PREFIX` (present in the image's Claude Code) wraps every Bash tool command, hook
  and MCP server command. The prefix would be a wrapper that runs `--untrusted` for everything but sandbox-ai's own
  two helpers by their exact command line (`--internal-hook AGENT EVENT`, `--internal-mcp-bridge PATH`) - not
  `--internal-kit-hook`, which runs a command of a kit: kits' own hooks would be refused under it (or the wrapper
  confines the kit's command and lets only its question to the hook server through).
- Codex: its own Linux sandbox does this job (Landlock and seccomp below each command) and is switched off by the
  launcher because its Bubblewrap variant cannot map IDs inside ours (the review's SAI-05). Its legacy Landlock mode
  with a profile that denies `~/.codex` is the path; it needs a Codex release that reads such a profile.
- The others (OpenCode, Qwen Code, Hermes, Pi, DSH, Letta Code, Goose, OpenHands, nanobot): their hook plugins can
  rewrite a shell tool's command (sandbox-ai already ships such plugins for the rules); each needs its own check.

As an option of the launcher - `--untrusted-tools`, refused for an agent without such a place rather than silently
weaker - and the default in the strict modes once it holds for an agent. A start's notes and `sandbox-ai doctor`
would say whether the kernel has Landlock and which parts.

**3. The login out of the sandbox (not built).** What `--token-proxy` does for Git and `--api-key` for API keys,
for the agents' OAuth logins: the sandbox holds a placeholder, the connection proxy puts the token on the wire for
the provider's hosts only, and refreshes it. It is specific to each provider's token flow (Codex: `auth.json` with
access, id and refresh tokens and a refresh endpoint; Claude: `.credentials.json`), and the agent must accept a
placeholder it cannot refresh itself. Where it works, SAI-18's worst case (a stolen refresh token) is gone even for
an unconfined command; transcripts and settings still need 1 and 2.

**4. Two users, two namespaces (the review's own fix; not built).** The supervisor - the only place with the
capability - starts a second Firejail beside the agent's as another sandbox ID, with its own PID and mount
namespace (no `/root`, no `/sandbox/tmux`, no hook or MCP folder), and the agent's commands are sent to it over a
relay like the shell relay's. Cost: files a command makes in the workspace belong to a subordinate ID on the host
(the nested-containers mode already has `give_back` for that), every agent's shell tool must be redirected (as in
2), and loopback is still shared unless the executor gets a network namespace of its own behind the same firewall
and proxy. It is the only one of the four that also closes SAI-19 and the rest of SAI-26, and it is weeks, not a
review fix.

## What was deliberately not done

- No new start option and no new mode: the fixes hold in every mode; `strict` for the terminal filter is an
  environment variable on the human's side, beside the other `SANDBOX_AI_*` switches of the commands.
- No change to what "for the run" means for a tool or MCP answer (it covers the tool under that rule): the modes and
  the policy already cap answers to one call.
- No guess about the Codex listener (SAI-19), and no clipboard change for the browser's display without the app's
  side of it (SAI-24).
- Nothing was probed outside this sandbox, and no real credential was read: the tests' HOME and tokens are made up.
