# TODO

## macOS support (required, 2026-11-05)

Linux and macOS are both required. As of 0.1.35 there is no macOS code at all. On a Mac,
Podman runs containers in a Linux VM (`podman machine`), so everything **inside** the sandbox
(runtime.py's bootstrap, guard, proxy, Firejail, nft, loggers, ApprovalBroker) stays as it is;
only the host side is ported. `podman` on a Mac is the remote client, so most host checks
should be skipped on Darwin exactly as they are for `--podman-url` today.

Three things decide most of the work (audit of 2026-11-05, file:line as of 0.1.35):

- **Sockets across virtiofs.** `control.sock`, `attach.sock`, the browser's `open.sock` and the
  image MCP bridge's `server.sock` are Unix sockets in host folders mounted into the container.
  A socket made on one side of virtiofs cannot be connected from the other. Replace them with
  `podman exec` relays into the VM (attach, `net allow/revoke`), a file-based request for the
  browser (as approvals already are), and the MCP bridge running in the VM.
- **`/proc` on the host.** Liveness, start times, command lines and children (`process_start`,
  `process_cmdline` in agent_sandbox.py, `sessions.alive`, `mcp.status`, `net.launcher_process`,
  `canary.process_start`, the app's `group_alive`). On a Mac they all say "gone": sessions look
  stopped, and `mcp.status` would remove a live MCP server. One portable helper (ctypes
  `sysctl(KERN_PROC_PID)` / `KERN_PROCARGS2`) replaces them.
- **Bubblewrap and host devices are Linux-only.** `--backend bwrap`, rootfs MCP servers,
  `--device`, `--gpu dri|rocm`, `--gui-wayland` are refused on Darwin with a clear message.

Done (0.1.45, tested on Linux with macOS simulated; not yet on a real Mac): stage 0 (local_engine(),
check_platform() refusing --backend bwrap, --device, --gpu dri|rocm, --gui-wayland on macOS; limits, routes and
the memory watch left to podman machine) and from stage 1: install.sh without GNU-only tools or bash 4 (mv -T,
mapfile, head -n -N, /proc, empty arrays), the PATH block in ~/.zshrc (made) and ~/.bash_profile on macOS,
sandbox-ai's scripts run with a bash 4 (Homebrew's when /bin/bash is 3.2), build_image.sh's cgroupfs fallback
Linux only, and doctor's macOS branch (podman machine: running, rootless, HOME and $TMPDIR shared, CPUs and
memory), every host path a start mounts checked against the machine's shares, --cpus from the VM's CPUs, and
tmux off by default on macOS until stage 2 (--tmux still asks for it). Stage 1 is complete in code; it needs a
real Mac (podman machine) to be verified: the first live test is the next step.

Stages:

1. **Stage 0: the switch.** `local_engine()` (false on Darwin or with `podman_url`) used where
   `podman_url` gates host checks today: `enforced_limits`, `check_subnets`, devices, doctor,
   `images.podman`, `build_image.sh` (`--cgroup-manager=cgroupfs`). Refuse the Linux-only
   options above.
2. **Stage 1: `sandbox-ai claude -w DIR` in the foreground on a Mac.**
   - install.sh: `mv -T` (:932), `mapfile` and `head -n -N` (:948), empty arrays under `set -u`,
     `/proc/*/cmdline` in `in_use` (:940), a PATH block in `~/.zprofile` and `~/.bash_profile`
     (a fresh Mac has no `~/.zshrc`; see the shell-rc rule).
   - build_image.sh: no `declare -A` (bash 3.2); `sandbox-ai-app:80` empty `"${args[@]}"`.
   - Every host path to mount (workspace, `--mount`, `--state-dir`, `runtime.py`, `$TMPDIR`, state
     dirs) checked against the machine's shared volumes before the pod is created.
   - `--cpus` default and maximum from `podman info` (the VM), not `os.cpu_count()`; warn when
     `--memory` exceeds the machine's memory.
   - doctor: a Darwin branch (`podman machine inspect`: running, rootless, shares cover `$HOME`
     and `$TMPDIR`, CPUs and memory); skip userns, subids, newuidmap, pasta, NAT module checks.
   - tmux sessions off by default on Darwin until stage 2.
3. **Stage 2: sessions and control.** The process helper; no `os.O_PATH` or `/proc/self/fd`
   (`attach_client`, `sessions.relay_listening`, `net.ask`); attach and `net allow/revoke`
   through `podman exec ... runtime.py --internal-attach-client|--internal-control-client`;
   `sandbox-ai limits` through `podman update`. Then tmux on by default again.
4. **Stage 3: host integration.**
   - `--allow-host-port` and `--llm`: pasta's `-T` reaches the VM's loopback, not the Mac's; use
     gvproxy's host address (`host.containers.internal`, pinned in `/etc/hosts`).
   - `open URL` instead of `xdg-open`; `osascript` notifications (text as `on run argv`, never
     interpolated); Keychain secrets with `security` (value from stdin, never argv).
   - Claude's login lives in the Keychain on a Mac (`auth_sources`): read it there or point to
     `sandbox-ai login claude`.
   - `private_locations`: add `~/Library` (LaunchAgents, Keychains, Application Support,
     Preferences); compare paths case-insensitively on APFS (`fcntl.F_GETPATH`).
5. **Stage 4: MCP.** Image MCP bridge inside the VM (or podman named volumes for socket dirs),
   `LOCAL_PEERPID` instead of `SO_PEERCRED` (an AttributeError on a Mac, not caught by
   `except OSError`), team board posting through `podman exec` (flock does not cross virtiofs).
6. **Stage 5: the app.** LaunchAgent autostart instead of `~/.config/autostart`, terminals
   (embedded, or Terminal/iTerm through `osascript`), `F_GETPATH` instead of `/proc/self/fd`
   (files.py:382), tray and notifications from an unbundled Python, then a `.app`.
   Optional later: `--gui-vnc` over TCP, GPU through krunkit.

To verify on a real Mac (not possible in the sandbox): a macOS CI runner (GitHub Actions
`macos-14`, arm64) for the unit tests and install.sh, and live tests with podman machine
(Podman 5, rootless and rootful) for each stage. `webui.py` needs OpenSSL's `-addext`; macOS
ships LibreSSL, so check it or fall back.

## Supervisor memory: the sandbox was OOM-killed (2026-11-05, again 2026-11-07)

A run ended when the kernel's memory-cgroup OOM killer took PID 1 of the sandbox, the
supervisor (`inner_bootstrap`, named `kthreadd` for Firejail): `Killed process ... (kthreadd)
total-vm:2239240kB, anon-rss:1236056kB` on 2026-11-05 (six worktrees with test suites and agents
at once, `--memory 8g`), and with 0.1.59 on 2026-11-07 with `anon-rss:2834008kB`. Killing it
ends the whole run. 0.1.36's `oom_score_adj` margin does not save it once it is itself the
largest process, and the memory watch never ends sandbox-ai's own processes.

Cause (found 2026-11-07, fixed in 0.1.65): the file logger (`--log-dir` watches the workspaces, HOME
and /tmp). When the inotify queue overflows, which test suites do several times a run, the
IN_IGNORED events of removed directories are lost; the kernel never reuses a watch number, so
their entries stayed for the whole run. Reproduced: one overflow left 5774 entries for 1 live
watch; tens of thousands of test directories per run, in several worktrees, add up to gigabytes
(an agent could do it on purpose). Fixed: the table is matched with the kernel's watches
(`/proc/self/fdinfo`) after every overflow and every minute.

Measured, not the cause: the process logger (bounded by the live processes), `poll_denied`
(about 150 MB at most with full nft sets), a thread per proxy question reading `/proc/net/tcp*`
(no malloc arena growth, also without `MALLOC_ARENA_MAX`).

Also in 0.1.65: the agent's processes get `oom_score_adj` +1000 (was +300), so the kernel takes
one of them while any is left, never the supervisor; MemoryGuard sheds from 768 MiB (the file
log stops with a note, caches are dropped) instead of growing into the OOM killer; the proxy
caps its TLS contexts (256) and server checks (4096) and gives memory back the same way. The
records of 0.1.60 stay: if a run still grows, `memory.jsonl` names what.

## A workspace added to a running sandbox (requested 2026-11-07)

Done: `sandbox-ai resume NAME [--restart] --workspace DIR` (a restart; the conversation goes on), and since
0.1.76 without a restart: `sandbox-ai workspace add|remove|list [RUN] DIR [--ro] [--force]` (workspaces.py).
Not the staging directory with `rslave` propagation first planned, but the way `devices add` already works: a
helper of the user enters the sandbox's user namespace, clones DIR there (`open_tree`, refused unless the
directory still has the device and inode that were checked: a symlink swap mounts nothing) and moves the clone
onto `/workspaces/NAME` in each of the sandbox's mount namespaces, the agent's under Firejail included
(`move_mount` with empty paths on descriptors opened `O_NOFOLLOW`, so a link the agent put there is refused).
Read-only and nosuid/nodev through `mount_setattr` (5.12); a failure in one namespace is undone in the others.
Every start has a small tmpfs at `/workspaces` (1 MiB, noexec) for the mount points: the root is read-only.
The checks of a start come from `workspace_checks` in run.json (the start's workspaces, mounts and HOME, the
host paths `host_sources` lists, private directories, stores); scope, private and package locations, planted
links and the policy are checked as at a start. `remove` refuses while a sandbox process has its cwd, an open
file or a mapping there (`--force` detaches lazily). A session keeps it in its options (`resume --restart` has it
at the same place). The file log watches it (control op `watch`).

Hardened after the review of 2026-11-07: only the sandbox's own mount namespaces are targets (never one the agent
made), a strict unmount first (the kernel refuses while anything holds the tree), the cgroup frozen during the
check, `--ro` adds are not kept for a restart, a directory holding a PATH directory is refused.

Open:
- Live tests with real Podman (rootless and rootful: rootful's user namespace is not the user's, so the helper
  cannot enter it, as for `devices add`) and Bubblewrap; none of that can run in the sandbox.
- A PID given out again between the scan of the sandbox's processes and the helper's open of its namespaces
  (pidfds from the scan would close it); the file log keeps its watches on a removed workspace's host inodes.
- `net.alive` compares start times from this PID namespace: a CLI run in another one would take a live run for
  ended (not reachable by the agent).
- Done in 0.1.78: the push check takes an added workspace (push-added.json beside push.json, written by `workspace
  add/remove` on the host with the run's derived rules and the project's own; the checker reads it for every push),
  and the after-run privacy check covers the writable ones added during the run.
- macOS: inside the podman machine VM (stage 4 of macOS support).
- The app and the web interface: a button for it.

## The app: a native crash in the board's drop test on CI (2026-11-08)

`projects/sandbox-ai-app/tests/test_board_dnd.py::test_folders_and_files_from_a_file_manager` crashes now and then
on GitHub's runners (offscreen, no GPU): SIGSEGV on the main thread inside Qt's event processing, faulthandler shows
"<no Python frame>" (the GIL released in processEvents), QtWebEngine loaded. Seen since the app job exists (0.1.66);
it never fails in this sandbox's runs nor with a display. Tearing the page down on the GUI thread (the fixture) made
it rarer, not gone. Set aside on CI (`CI=true` and offscreen) so the job shows every other result. Next: a
reproduction under xvfb-run on the runner, gdb's native backtrace (`catchsegv` or `gdb -batch -ex run -ex bt`), and
whether the drop's scene rebuild while the view still processes the drop's events is the cause.

## Every start: names, detach, keep, rebirth (2026-11-04)

Done: `sandbox-ai list|ps` (one registry: runs, sessions, kept starts, MCP
servers; `--json`, `--all`, `--history NAME`), names for every start
(`AGENT-PROJECT-xxxx`, `--name`, `--project-name`, the project key `"name"`),
`sandbox-ai rename`, `attach|pause|resume|stop|delete NAME` for any start,
interactive starts as detachable sessions (closing the terminal detaches), the
HOME kept by default (`--no-keep`, `"keep": false`, policy `forbid_keep`),
`sandbox-ai gc [--older-than DAYS] [--dry-run]` and the optional `keep_days`,
rebirth with a new run id per start, the HOME manifest compared at every
rebirth (`resume --review-home`), and a history per name.

Open:

- **Live tests.** Everything above is tested with faked processes, a faked
  `/proc` and a recorder instead of Podman and Bubblewrap. To check on a real
  host, for each backend and agent:
  - `sandbox-ai claude -w DIR` in a terminal: the name is printed, tmux comes
    up, Ctrl-b d detaches (the command prints the detach hint and returns 0),
    closing the terminal window detaches too (the sandbox runs on, `sandbox-ai
    list` shows it running), `sandbox-ai attach NAME` comes back, `/exit` in the
    agent and leaving the shell window end it and the command returns the
    agent's exit status (`echo $?`).
  - `sandbox-ai pause NAME`, `resume NAME` on a session and on a foreground
    task (`sandbox-ai codex -- exec "sleep 600 in a shell"`); with bwrap the
    task's launcher must keep its terminal (no "Stopped" from the shell).
  - `sandbox-ai stop NAME`, then `sandbox-ai resume NAME`: a new run id
    (`sandbox-ai list --history NAME`), the same HOME, and the agent continues
    the last conversation (`claude --continue`, `codex resume --last`,
    `opencode --continue`); a task started with `-p` comes back interactive.
  - Write to `~/.bashrc` inside the sandbox, stop, resume: the notice names
    `.bashrc`, Enter goes on, `n` stops; `resume NAME --review-home` shows the
    line.
  - `sandbox-ai rename` of a running session: `net list`, `approvals list` and
    `activity --session NEW` show and accept the new name.
  - A Podman pod left by `kill -9` of a supervisor is removed at the rebirth
    (its label matches) and refused for a pod of the same name from elsewhere.
  - `sandbox-ai stop mcp-demo` ends the MCP server sandbox.
- **Checkpointing running processes** (CRIU, `podman container checkpoint`)
  is not done: a rebirth starts the agent again with its HOME. Podman's
  checkpoint needs root (or a rootless CRIU setup) and does not cover
  Bubblewrap; worth a look only if users ask for long-running processes to
  survive a reboot.
- **A rebirth's options.** Done for workspaces and mounts (0.1.60: `resume NAME
  [--restart] --workspace DIR --mount SPEC`, added and kept). Other options: a reborn start keeps its saved options; there is no
  way to change one (`sandbox-ai resume NAME --gui`). It would need the
  checks of a new start for the changed options (policy, private paths) and a
  clear rule for what is replaced and what is added.
- **The HOME manifest** names changes but does not undo them; a `sandbox-ai
  resume NAME --restore-home` could put the recorded text of small files back.
  Files beyond the limits (5000 entries, 64 MiB per file hashed) are reported
  as "not every change can be named".
- **Old run records** (from versions before 0.1.30) have no name, start or
  pod: they are listed under their run id, and such a task cannot be paused
  with Podman (no pod recorded).
- **The old single commands** (`claude-sandbox`, `codex-sandbox`,
  `opencode-sandbox`, `sandbox-session`, `sandbox-mcp`, `sandbox-wizard`,
  `sandbox-build-image`) print a deprecation notice since 0.1.30
  (`SANDBOX_AI_NO_DEPRECATION=1` silences it) and are to be removed in a later
  version: then drop the wrappers, the `[project.scripts]` entries, their
  installation in `install.sh`, and the tests that call them.
- **The wizard** does not ask for the new project keys yet: `"name"` (the
  PROJECT part of the automatic names) and `"keep"` (false: the project's
  starts discard their HOME); both can be written into the file by hand (and
  trusted again). Its question for `--state-dir` should say that starts keep
  their HOME anyway now.
- **Desktop app.** It can use `sandbox-ai list --json`, `list --history NAME
  --json`, `resume NAME --review-home --json`, `rename`, `attach|pause|resume|
  stop|delete NAME` and the project key `"name"`; integrating them is a task
  of the app.

## Future: GitHub push proxy on the host (token kept outside)

Partly done in 0.1.21: `--token-proxy` gives the Git tokens only to the
connection proxy inside the sandbox (its directory is hidden from the agent),
which adds them to Git's smart-HTTP requests to the token's HTTPS server. Still
open below: plain-http loopback servers, a ref policy, and the proxy running on
the host rather than in the pod.

`--git-token-env`/`--git-token-file` put the token into the sandbox, where the agent can read
it. A push proxy would give the agent the ability to push without the secret:

- **Host-side proxy** for Git's smart HTTP protocol, reachable from the
  sandbox only through a forwarded loopback port or a mounted Unix socket
  (same path as `--allow-host-port`). The token stays on the host; the proxy
  adds the `Authorization` header itself.
- **Repository allowlist**, e.g. `--github-push owner/repo` (repeatable).
  Every other repository and the GitHub API are refused.
- **Ref policy**: parse the ref update commands at the start of
  `git-receive-pack` and allow, e.g., only `agent/*` branches; refuse
  force pushes (non-fast-forward), deletions and pushes to the default
  branch. A fine-grained token cannot express this.
- **Audit log**: one JSON line per push with repository, ref, old and new
  object IDs, next to `egress-denied.jsonl`.
- ~~Privacy check in the proxy~~: done in 0.1.20 by the connection proxy in
  the sandbox (every push over HTTP(S) is checked with the full rules before
  the server sees it). What remains here is keeping the token out of the
  sandbox.
- Without `--allow-github`: the sandbox reaches only the proxy, never
  github.com directly.

## The sandbox's own network (`--subnet`, `--subnet6`): open points

- **Live check with real Podman and slirp4netns.** The unit and matrix tests
  check the command lines only: `pasta -a .100 -n PREFIX -g .2 --dns-forward
  .3` (and `-a ::100 -g ::2`) through `podman pod create --network pasta:...`,
  and `slirp4netns --cidr=CIDR`. Still to confirm on a real host: that Podman
  passes a user `--dns-forward` through without adding its own
  (`169.254.1.1`), that `--nameserver .3` resolves on both backends, and that
  with `--subnet` the host's own LAN address leads to the host under pasta.
  Add a case to `tests/test_agent_sandbox_live.py`.
- **IPv6 with Bubblewrap.** slirp4netns has no option for its IPv6 prefix
  (`fd00::/64` is built in); `--subnet6` stays Podman-only unless slirp4netns
  gains one.
- **A chosen sandbox address** (`--sandbox-ip`): slirp4netns always puts the
  sandbox at `.100`, so it would be Podman-only; not done.
- **Helper containers keep pasta's defaults**: the DNS lookup container of a
  Podman start and MCP image servers with `network: full` do not follow
  `--subnet` or the policy's `subnet_within`.

## LLM servers (`--llm`): what stays open

Done: endpoints of `sandbox-ai llm` (Ollama, LM Studio, llama.cpp, vLLM, any
OpenAI-compatible server), `--llm NAME[:MODEL]` for all three agents, the key
added on the wire by the connection proxy, `llm models`/`llm check`, the
wizard's model step, and the VPN wiring: an endpoint added with `--vpn
PROFILE` makes `--llm` start behind that VPN (`llm.apply_vpn`: it sets `--vpn`,
another `--vpn` is refused, `forbid`/`require_vpn` see it), and the server's
name is resolved through the tunnel with the other names (`llm.pinned_names`).
Open:

- **`llm check`/`llm models` through the VPN.** They still use the host's own
  network, so a server only the VPN reaches (or a name only its resolver knows)
  fails there. Bring the profile up as `sandbox-ai vpn check` does and run the
  requests inside its namespace (a small client in the VPN container, the key
  passed on its standard input).
- **Rules engine.** A path prefix becomes an `--allow-http 'HOST * /PREFIX/**'`
  rule (`llm.prefix_rule`), for host names only; an endpoint given as an
  address gets its whole port. Once the rules engine lands in runtime.py, emit
  its rule entry (scheme, host or address, port, path prefix) instead, so that
  addresses are narrowed too.
- **Echoed credentials.** The connection log drops the exact values of the
  keys and header secrets; a server that echoed request headers in its answers
  would still show them to the agent. Masking the values in identity-encoded
  answers (same length, so the framing holds) would close the common case;
  compressed answers would remain.
- **Context window.** Local models are in neither Codex's nor OpenCode's
  catalog: set `model_context_window` (Codex) and `limit.context`/`limit.output`
  (OpenCode) from what the servers report (vLLM `max_model_len`, llama.cpp
  `n_ctx`, LM Studio `max_context_length`, Ollama `/api/show`).
- **Older and newer agents.** Codex before 0.134 has no profile files
  (`--profile` selected `[profiles.x]`); `-c` overrides would cover it.
  OpenCode 2's native `providers` shape is not written (it reads the v1 shape
  written now).
- **Claude Code and model names with `/`** (vLLM serves `org/model` by
  default) may be refused by Claude Code; the start says so, and vLLM's
  `--served-model-name` avoids it.
- **Not supported:** TLS client certificates (mTLS) to the server, rotating
  keys (an `apiKeyHelper`-like command); keys are static secrets.
- **Live tests** against real servers: `tests/test_live_e2e.py` has
  `--local-llm` for OpenCode only; extend it to `--llm NAME` with each agent.

## Open findings from the security review (2026-11-01)

Low severity; each needs a larger change than a local fix. Fixed since: a
project file's `--copy` of a credential file is an auth choice in
`merge_config`, replaced by one on the command line; the file logger walks with
`os.fwalk` and adds each watch through the walked directory's descriptor (0.1.76).

- **Claude write-back within one run.** Write-back now requires an existing
  host login, an unchanged host file and tokens that differ from the ones
  copied in; the host wins at every start. During a single run the agent can
  still replace the tokens with another account's, which then reach the host.
  Closing this needs an account check against Anthropic (or an opt-out such
  as `--no-auth-writeback`).
- **Signals during cleanup.** The pod removal and the bwrap/slirp4netns stop
  now hold SIGTERM, SIGHUP and SIGINT (`signals_deferred`) and deliver them
  afterwards; the write-back, memory and privacy steps after it stay
  interruptible. Session pods carry a `sandbox-ai.session` label and a
  restart removes only its own stale pod; one-off pods still use random names
  without a label.
- **apt packages** in the images are not pinned; agent, uv and Node.js base
  can be pinned with `--pin`, `--uv` and `--node` (newest is the default).

## Open findings from the review of 2026-11-02

Fixed in the same change: Claude's `.credentials.json` copied in full (MCP
OAuth tokens), write-back into a host file without a login, discovered
credential sources and workspaces inside agent-writable trees, TIOCSTI through
the controlling terminal, risky workspaces (HOME, `/`, temp, the launcher),
`--allow` scope IDs, relative paths in project and MCP files, session
`agent_args`/backend from the project file, stale session and MCP pods,
resize frames inside input frames, MCP servers frozen with a bwrap session,
the wizard's origin default; the run registry compares the launcher's recorded
start time, not the PID alone (0.1.76). Still open, low severity:

- **bwrap pause is best-effort.** Two SIGSTOP passes; a process that keeps
  forking can escape. A cgroup freezer would be exact.
- **Xvfb outside Firejail.** It runs in the outer mount namespace where
  `/sandbox/log` and `/sandbox/vnc` are visible; run it inside Firejail with
  the same blacklist.
- **Tokens inside the sandbox.** `--git-token-*` and `--git-credentials` hand
  the tokens (every login in the file) to the agent; the push proxy above would
  keep them on the host.

## Open findings from the reviews of 2026-11-03

Fixed in 0.1.21: approvals naming the sandbox, one host-path policy (registry
of exposed trees, planted symlinks, private locations and PATH directories for
workspaces, mounts, --state-dir, --log-dir, --kit-dir and MCP image mounts),
SSH as address and port, safe tool-call redaction, approvals remembered only
once applied. Still open:

- **Check, then mount.** The checks run before Podman resolves the paths
  itself; a sandbox running at the same time can swap a directory for a link
  in between (`RENAME_EXCHANGE`). MCP image servers now compare every folder
  with its device and inode before each connection, which narrows this to
  milliseconds. Bubblewrap could bind file descriptors (`--bind-fd`); for
  Podman, re-check from inside the started container against the inode
  recorded at check time.
- **MCP server egress.** A server with network acts for the agent past its
  proxy and approvals; route server traffic through the same proxy and
  allow-list. One agent holding all 16 connections of a shared server locks
  other sandboxes out (per-client limit or idle timeout).
- **Registry seeding** covers approvals, trusted configurations, sessions and
  MCP definitions; plain runs of older versions left no trace.

## Device rules (2026-11-04)

Item 5 of the CaMeL/Progent analysis: `"devices"` rules in the rulesets (usb,
serial, class, subsystem, node; allow/ask/deny), `--device
usb:VID:PID[:SERIAL]` resolved through sysfs, re-attach on replug by identity
(5 minutes), detach on unplug, question kind `device`, `devices.jsonl` with
hashed serials, opens counted through inotify on the host, `devices list
--json`. Unit and matrix tests fake sysfs and udev; open:

- **Live tests** (`tests/test_agent_sandbox_live.py`, both backends, a real
  USB serial board such as a Pico):
  - `--device usb:2e8a:000a`: the agent opens `/dev/ttyACM0`; the board reset
    into its bootloader (another product ID, so not re-attached) and back
    (`replug` in `devices.jsonl`, the agent opens the new node).
  - Unplug: the node is gone from the agent's and the supervisor's mount
    namespace, Firejail's included (`detach`). Under Podman, check that the
    helper's `umount2` takes away a node Podman bound with `--device` at the
    start (rootless crun bind-mounts it; a rootful or `mknod` node would need
    `unlink` instead).
  - A second board with the same IDs plugged meanwhile: not attached, the
    `waiting` record; `usb:...:SERIAL` picks the right one.
  - An ask rule at the start: the sandbox starts without the device, the
    desktop notification answers it, the node appears inside.
  - `open` records: opens inside the sandbox are counted (inotify on the
    host's devtmpfs inode; checked here on a bind-mounted `/dev/null` on
    Linux 7.2, not yet on a device the sandbox opens through a cloned mount).
  - `sandbox-ai devices add usb:...` of a run without devices at the start
    spawns the detached watcher (`devices.py --internal-watch`); it ends with
    the run.
- **Not done:** attribution of an open to a process of the sandbox (the
  inotify watch also counts the host's own opens); per-rule time limits
  (`max_duration`) instead of the fixed 5 minutes; a device absent at the
  start that is attached when it first appears; rules for single devices
  inside a directory passed whole (`/dev/bus/usb` is one device to them).

## Rulesets (2026-11-04)

Items 1 and 2 of the CaMeL/Progent analysis are done: one rules engine
(`Ruleset` in runtime.py, JSON version 1), HTTP rules in the connection proxy
and an MCP gateway in the relay (README "Rulesets").

- **Done:** rulesets from the administrator's policy (`"rules"`),
  `~/.config/sandbox-ai/rules.json`, `--rules FILE`, `--allow-http` and the
  trusted project file (`"rules"`). Each decides on its own and the strictest
  decision wins, so none can widen another. Rules allow, ask or deny by host,
  method, path, query, headers and JSON/form body, and by MCP tool and
  arguments. Conditions are three-valued: whatever servers could read
  differently is unknown and never allowed by a rule. Missing arguments fail,
  patterns match the whole value, and nothing matching falls to an explicit
  default (the Progent defects). `ask` goes through the approvals as kinds
  `http` and `mcp`. Rule IDs are logged in `connections-*.jsonl` and in the
  new `mcp-calls.jsonl`. The gateway also filters `tools/list`, refuses
  batches and lines that are not one JSON message, and re-encodes what it
  passes on. Bypass tests that assume a fully attacker-controlled agent are in
  `tests/test_rules.py`.
- **Managed agent hooks** (item 3): done, see "Agent hooks" below.
- **Session taint** (item 4): done, see "Run taint" below.
- ~~**Device rules** (item 5)~~: done, see "Device rules" below.
- **Per-run gateway sockets.** Partly done with the taint: each run reaches
  its MCP servers through a relay in its own supervisor, which holds the
  rules for tainted runs, asks this run's approvals and logs this run's
  calls. The other MCP rules still sit in the server's own gateway: their
  questions go to the server's approvals directory, `run` lasts until the
  server stops, and an agent with other rules for a running server is
  refused. Moving all MCP rules into the relay would end that.
- **Smaller:** `sandbox-ai rules check|show|reload|suggest` (`--dry-run`
  checks a file today); reloading rules during a run; per-rule limits (bytes,
  requests per minute) and log levels; rules inside `mcp.json` definitions;
  wizard and desktop app support for rules. Not covered by the rules:
  WebSocket messages after an upgrade, and HTTP the proxy does not recognise
  as such (a lowercase method) on ports other than 80 and 443 to hosts without
  named rules. MCP rules cover `tools/call` only. A command server's relay
  runs as the server's user: a server tool that runs code can replace it.
- **Not run here:** the rules through real Podman and Bubblewrap sandboxes
  (`test_live_e2e.py`, `test_escape_live.py` with a rules file and an MCP
  server sandbox with the gateway). This environment has no Podman. The
  `--mcp-approvals-dir` binding and the server sandbox's relay arguments are
  covered by unit tests only.

## Run taint (2026-11-04)

Item 4 of the CaMeL/Progent analysis (README "Untrusted content: the run's
taint").

- **Done:** `"untrusted"` sources in rulesets (hosts with an optional path,
  MCP tools with an optional server) and `"when": {"tainted": true}` on HTTP
  and MCP rules. The supervisor holds the taint, mirrors it to the proxy's
  run directory and logs `taint`/`untaint` events to the connection log. The
  proxy holds the head of a response from an untrusted host until the
  supervisor acknowledges the taint, and the per-run MCP relay records it
  before passing a tool's result on. The provider is exempt only under its
  verified name. Questions say why they were asked, `sandbox-ai activity`
  and `net show` show the taint, and `sandbox-ai net untaint [RUN]` is the
  only reset. The servers' own sockets moved to `/sandbox/mcp-upstream` and
  are hidden from the agent (Firejail blacklist). Bypass tests are in
  `tests/test_taint.py`.
- **Security modes** escalate (modes.taint_settings): in `high` content from
  any host but the provider taints and then every MCP tool call asks; in
  `paranoid` MCP results and the agent's WebSearch/WebFetch taint too, and
  then its shell, edits and fetches ask through the hooks. The shared MCP
  server gateways leave the rules for tainted runs to the per-run relays, so
  a strict mode does not change the configuration other agents share.
- **Open:**
  - Taint is per run, not per value: every later sink asks, however
    unrelated. Finer tracking needs the agent's cooperation (item 3's hooks
    can mark built-in tools such as WebSearch, which runs at the provider and
    never passes the proxy).
  - Content saved to `/work` or memory and read in a later run starts that
    run untainted. A per-project taint that survives runs until the user
    clears it would close this.
  - Requests already in flight on other connections when the taint is
    recorded are not held.
  - WebSocket messages after an upgrade count by the upgrade request, and
    content the proxy does not open by its host alone: a source's `path`
    cannot narrow them.
  - The rules for tainted runs at the MCP relay are not run through real
    Podman and Bubblewrap sandboxes here (no Podman in this environment).

## Agent hooks (2026-11-04)

Item 3 of the CaMeL/Progent analysis (README "The agent's own tools: its
hooks").

- **Done:** `"tool"` rules (agent, tool, args, when) and `"agent"` sources
  in rulesets. The launcher writes the hooks as managed configuration on
  read-only mounts: Claude Code `/etc/claude-code/managed-settings.json` with
  `allowManagedHooksOnly`, Codex `/etc/codex/requirements.toml` with
  `allow_managed_hooks_only = true` and `[features] hooks = true` (checked
  against the Codex source: the key is in rust-v0.131.0 and later, and a
  user config that turns the feature off is refused), and an OpenCode plugin
  (`tool.execute.before`/`after`) named as `file://` in
  `OPENCODE_CONFIG_CONTENT`. The hook client (`runtime.py --internal-hook`)
  fails closed; the supervisor answers on `/run/sandbox-hooks/decide.sock`,
  which Firejail makes read-only for the agent (so is `/run/sandbox-mcp`).
  The supervisor asks as kind `tool`, taints the run when an untrusted tool
  is allowed, and logs to `tools.jsonl`. `paths` gives rules the files a call
  names, Codex patches included. Bypass tests are in `tests/test_hooks.py`;
  the OpenCode plugin is tested under Node.
- **Open:**
  - Not run with the real agents here (no Podman, no API access): a live test
    that Claude Code, Codex and OpenCode in the images load the managed
    configuration and stop a refused call. An older Codex ignores unknown
    keys in requirements.toml, so the supervisor refuses a Codex older than
    0.131.0 (`codex --version`); Claude Code before `allowManagedHooksOnly`
    would still run the managed hooks, next to the user's own.
  - A guard rail by design: Bash, scripts and a second agent started from
    Bash get around the rules. A seccomp or LSM layer would be needed for
    more.
  - PostToolUse cannot hold a result back. The taint is therefore recorded
    when an untrusted tool is allowed, before it runs.
  - The hooks are installed only when rules need them. A mode that only logs
    every tool call to `tools.jsonl` could come, and so could `tools.jsonl` in
    `sandbox-ai activity` and `sandbox-ai logs`.
  - Kits' hooks: done ("Kit hooks" below). Claude plugins' own hooks: done
    (2026-11-05), approved with `sandbox-ai plugins claude trust-hooks`.
  - Tool rules in the wizard and the desktop app.

## Kit hooks (2026-11-04)

Kits carry the agent's hooks in `AGENT/hooks/`: Claude Code's and Codex's
`hooks.json` with the scripts it runs, OpenCode plugin modules (README
"Hooks in kits").

- **Done:**
  - `sandbox-ai kits add-hook`, `remove-hook` and `trust-hooks`; `show`
    lists every hook; `list`/`show --json` carry hooks and approval.
  - Approval by SHA-256 of the whole folder, kept in
    `~/.config/sandbox-ai/trusted-hooks.json` (a start refuses a
    configuration directory a sandbox can write).
  - The start copies each folder, hashes the copy and folds only approved
    copies into the managed configuration, after the rule hooks: one
    mechanism, with or without tool rules. Unapproved hooks are named with the
    command that approves them.
  - Bypass tests are in `tests/test_kit_hooks.py`: an edited approved script,
    a symlinked hook file or folder, a trust file inside the kit, a project
    file with its own approval.
- **Open:**
  - The desktop app's Kits page: inspect, upload and approve. The app agent
    does this on top of `kits list --json`, `add-hook` and `trust-hooks --yes`.
  - Claude Code runs matching hooks in parallel, so "rule hooks first" is
    their order in the configuration, not in time. A kit hook may run for a
    call the rules refuse (its answer never overrides the refusal).
- **Done (2026-11-05):**
  - A kit hook's `updatedInput` goes past the rules: with tool rules each kit
    `PreToolUse` hook runs through `runtime.py --internal-kit-hook`, which
    asks the rules about the rewritten input (unreadable answers naming
    `updatedInput` are refused). OpenCode: the rule plugin runs again after
    the kits' plugins for calls whose arguments changed.
  - Codex older than 0.131.0 refuses the start for kit hooks alone too
    (`hook_agent` in config.json, `runtime.hook_problem`).
  - Approvals hold per kit and content: the same hooks in another kit need
    their own approval; revoking one kit leaves the other's.

## Web interface and remote hosts (requested 2026-11-05)

A web interface beside the desktop app, with the same themes, reachable from
anywhere with authentication; one place for every host. Python on the
server, plain JavaScript in the browser, no Node.js (no npm, no build step;
xterm.js is vendored as files, as the app does).

- **Hosts (done: `sandbox-ai hosts`, hosts.py).** The app and the web
  interface use the same list and reach a host with `hosts.run`/`run_json`
  (`ssh host sandbox-ai ... --json`) and `hosts.argv(tty=True)` for attach.
- **The service: `sandbox-ai web`.** Standard library HTTP server, HTTPS
  only (its own certificate at the first start), 127.0.0.1 unless
  `--listen` says otherwise; JSON API plus static pages; WebSocket (RFC 6455,
  by hand) for terminals.
- **Authentication: password and TOTP.** `sandbox-ai web user add NAME`
  (scrypt hash, a TOTP secret shown once as otpauth URI); sessions in
  memory, cookies HttpOnly, Secure, SameSite=Strict; CSRF token on every
  change; login rate limit; every action in an audit log. No sandbox may
  reach the service (its port is refused for --allow-host-port).
- **Themes.** The app's theme data (theme.py THEMES_DATA) moves to a shared
  JSON both read.
- **Stages:** 1. service, login, hosts page; 2. sandboxes and sessions
  (list, pause, resume, stop), terminal attach; 3. approvals; 4. doctor,
  connections, activity, statistics; 5. start and the board (board.py of
  the app is Qt-free and reusable).
- **App:** a host selector; its backend's commands through hosts.argv.

## Several agents in one setup (requested 2026-11-05)

Claude Code, Codex and OpenCode working together, any of them as the lead.
Three modes, built in this order:

1. **Agents as MCP servers** (done 2026-11-05: `--helper AGENT`,
   `agent_mcp.py`, README "Another agent as a helper"; open: a live run
   with Codex and OpenCode, whose non-interactive flags are from their
   documentation, and the helper's own `--allow-*` options). The lead runs in its sandbox as now; each
   helper agent runs as an MCP server in a sandbox of its own (`codex
   mcp-server`, `claude mcp serve`, OpenCode through a small stdio bridge
   around `opencode run`), with its own login, provider hosts, kits, rules
   and hooks, and its own workspace mount. The lead calls it through the
   relay, so `mcp` rules, approvals, the taint and `mcp-calls.jsonl` hold.
   Needs: a server definition kind `"agent"` (login and provider network
   allowed there, unlike other servers), `--helper AGENT[:NAME]` on the
   lead's start, the helper's workspace (shared read-only, or its own copy).
2. **One sandbox, all CLIs** (done 2026-11-05: `--with AGENT`,
   `sandbox-ai build team`; open: kits for the agents --with, and a live
   run of the team image). An image with all three agents
   (`sandbox-build-image all`); `--with codex,opencode` adds their provider
   hosts, logins, managed hooks and kits next to the lead's, and the lead
   calls `codex exec` / `opencode run` from Bash. Weaker isolation: one
   network, one HOME, one workspace.
3. **A team of sandboxes** (done 2026-11-05: `sandbox-ai team`,
   `team_board.py`; open: a live run, the board knowing its caller, the
   desktop app). One sandbox per agent on a shared workspace,
   started and stopped together (`sandbox-ai team`, a board preset), with a
   coordinator handing tasks between them.

## Process attribution in the logs (2026-11-04)

Done: refused and logged connections, refused lookups, dropped packets,
questions and MCP tool calls name the process behind them. The record holds
the PID inside the sandbox and inside Firejail, comm, argv (escaped, cut and
redacted), exe, cwd, uid, the parents' names and the start time, plus the
sandbox (run, agent, backend, role, pod or session). The supervisor reads
these from `/proc/net/*` and `/proc/*/fd`, rate-limited, time-bounded,
cached, and checked against PID reuse. A refusal also records what the client
sent: SNI and ALPN, or the request line, `Host` and headers (redacted).
`--log-debug` (or the project key `log_debug`) adds bodies, the ClientHello
summary, and the process's ancestry and sockets. `sandbox-ai logs` lists the
newest refusals with these fields. Open:

- **MCP callers in server sandboxes.** A relay inside a server's sandbox
  sees no process of the agent's sandbox (`SO_PEERCRED` gives PID 0), and no
  relay knows which agent sandbox connected. Per-run sockets (see Rulesets)
  would name the run; the agent's supervisor could name the bridge process.
- **Dropped packets.** The firewall's `denied` sets hold the first drop per
  destination, without the source port, and are polled every second. The
  process is found by the socket's destination, so an unconnected UDP
  `sendto` and a socket closed before the poll stay unknown. Adding the
  source port to the sets (with a timeout) needs a live nftables test.
- **Timing.** A refusal waits 50 ms for the lookup (the kernel forgets a
  socket once it is reset); a busier sandbox may need more, or a lookup
  started before the decision for every new connection.
- **Formatting.** `sandbox-ai logs` prints the refusals plainly; main's
  `textout.py` tables are not used on this branch yet.
- **Debug for the MCP gateway**: `--log-debug` does not reach the relay of
  a server sandbox.

## Kits, memory and plugins (2026-11-02)

Fixed: one `--state-dir` is locked for the whole run (a second run stops with
the directory and the holder's PID); `--no-plugins` removes `enabledPlugins`,
`extraKnownMarketplaces` and the plugin index copies an earlier run left.

Not verified live yet (no Podman/Bubblewrap or agent images where this was built):

- **Skill locations of Codex and OpenCode.** Kits mount Codex skills at
  `~/.agents/skills` and OpenCode's at `~/.config/opencode/skills`, as their
  current documentation says; older releases read `~/.codex/skills` and
  `~/.config/opencode/skill`. Check with the images' versions.
- **Claude with a read-only plugin cache.** `cache/` and `marketplaces/` of the
  store are read-only, the index files writable copies with `autoUpdate` off.
  Check that Claude loads the plugins and does not fail on a background update.
- **Codex plugins**: no integration yet; marketplaces.json for Codex is noted
  and ignored. OpenCode's npm plugins are passed on, not installed by sandbox-ai.
- **Memory of a crashed run** in a throwaway HOME is lost (copy-in/copy-out);
  a persistent HOME is saved at its next start.
- **`--state-dir` lock** (`.sandbox-ai.lock`, flock for the whole run) lives in
  the agent-writable HOME: the agent can hold it to block later runs while it
  runs, or delete it so a second run starts beside it. A launcher killed with
  SIGKILL releases it while a Podman pod may still run.

## Logins (2026-11-02)

Fixed: the Claude write-back compared the whole host credentials file, so a
host Claude rewriting it (MCP OAuth tokens) made the launcher drop the
sandbox's rotated tokens, leaving the host with a used-up refresh token and the
next start with "Login expired". It now compares only the login; a missed
write-back on a persistent HOME is recovered at the next start. New: the login
store (`sandbox-ai login AGENT`). Open:

- **One refresh token per stored login.** Since the live login sync
  (`watch_claude_login`), Claude sandboxes, the store and the host pass each
  refresh on within two seconds, under a host-wide lock with compare-and-swap;
  they race only when two refresh within that window. Codex and OpenCode still
  write back only at the end of a run (same race as before; the same watcher
  would fit `store_writeback`). A host-side token broker (the sandbox asks the
  launcher for fresh access tokens, never holds the refresh token) would end the
  race and keep refresh tokens out of the sandbox.
- **Claude's `clientId`.** Claude Code 2.1.317 stores a `clientId` with logins
  made through a custom OAuth client and refreshes with it; the launcher copies
  only `OAUTH_TOKEN_FIELDS`/`OAUTH_INFO_FIELDS`, so such a login refreshes with
  the default client in the sandbox. Logins through the default client store
  none (its refresh code treats a missing `clientId` as the default client).
- **Login commands not verified live.** `claude auth login`,
  `codex login --device-auth` and `opencode auth login` inside the sandbox
  need a run against the real services; other arguments can be passed after
  `--` (`sandbox-ai login codex -- login`).
- **Online check of a login.** Expiry is checked offline only (Claude's
  `refreshTokenExpiresAt`); a refresh token revoked or used up elsewhere shows
  only when the agent refreshes.

## Connection proxy: next steps

- HTTP/2 towards clients that insist on it (gRPC); today only HTTP/1.1 is offered.
- QUIC/HTTP3 is not proxied; UDP stays with the firewall rules.
- The push check finds what its rules describe. An agent that deliberately encodes data (base64,
  archives, encryption) gets past any pattern check; the connection log records what was sent.
- Live tests of the NAT redirect on real Podman and Bubblewrap hosts.

## VPN (2026-11-04)

`--vpn PROFILE` (Podman), `sandbox-ai vpn`, MCP servers with a VPN, `require_vpn`. Built and unit-tested without
Podman: profile parsing, secrets, the Mullvad client against a fake HTTPS API, the kill-switch rules, the
container and pod command lines, the checks with faked answers, MCP, policy and project file. Open:

- **Live tests on a real host** (none ran where this was built): kernel WireGuard and wireguard-go in a rootless pasta namespace, the kill switch with pasta's IPv4
  and IPv6, OpenVPN's `--mark` and `--redirect-gateway def1 ipv6`, Mullvad's API (`auth/v1/token`,
  `accounts/v1/devices`, `app/v1/relays`) with a real account, the echo services' answers.
  `test_rulesets_load_together_in_a_namespace` loads both nftables tables where `unshare -rn nft` works.
- **Tailscale's marks.** The kill switch (exit node) and the agent's firewall let tailscaled's traffic through by
  its bypass mark `0x80000/0xff0000`. tailscaled sets it on its sockets in tun mode with `CAP_NET_ADMIN`; if a
  version does not (or not before `up`), the tunnel fails closed: verify, else match its uid in a container
  user of its own.
- **Bubblewrap.** `--vpn` is refused there. A way: the bootstrap (PID 1 with all capabilities of the sandbox's
  user namespace, before Firejail) creates `wg0` with the kernel module, configures it from a key passed on an
  inherited pipe, loads the kill switch beside the agent's table and runs the checks; wireguard-go, openvpn and
  tailscaled would have to come from the host's `/usr` and run outside Firejail in the bootstrap's PID namespace.
- **Checks during the run.** They run once at the start; a tunnel whose daemon dies is reported (the kill switch
  keeps everything in), but the exit address is not re-checked later. A periodic re-check (`vpn.jsonl`) and a
  stop on failure would close that.
- **DNS leak probe.** `whoami.akamai.net` shows the egress of the resolver chain; a provider's own leak test
  (Mullvad's web check) has no documented API to use.
- **Endpoint addresses** are resolved once per start: a provider that moves an endpoint during a run stalls the
  tunnel (fail closed) until the next start. Mullvad's relay is chosen at `vpn add`; `vpn add --force` registers
  a new device (the old one stays until removed in the account); key rotation through
  `PUT accounts/v1/devices/ID/pubkey` is not used yet.
- **Proton** has no simple key API (SRP login): only its downloaded WireGuard or OpenVPN configurations.
- **OpenVPN**: `<connection>` blocks, tap devices, proxies and PKCS#12 with a passphrase are not supported;
  pushed `dhcp-option DNS` comes through the route-up hook (OpenVPN 2.6 `--dns` options too).
- **Remote Podman** (`--podman-url`): the host's own public address for the comparison is asked from the machine
  that runs the launcher, not the engine's; on a remote engine the address check compares with the wrong host.
- **Desktop app**: the board keeps `--vpn` among the options no node owns, and the app's
  `test_every_launcher_option_is_in_the_form` fails until its option groups list `--vpn` and `--vpn-check`. A
  patch for projects/sandbox-ai-app (a VPN node with its problems and policy checks, the options in the Network
  group, German texts, tests) was prepared beside this change; a profile picker (`sandbox-ai vpn list --json`)
  is still to do.
- **Lookups during a run**: names allowed while a sandbox runs (`sandbox-ai net allow --allow-host`, approvals)
  are resolved on the host, not through the VPN's resolver; the connections go through the tunnel. Resolve them
  in the VPN container (it could keep a control channel) to keep those lookups private too.
- **Image pinning**: `Containerfile.vpn` takes the newest Debian packages, wireguard-go `latest` and Tailscale
  `stable` unless pinned with build arguments.

## Signed updates: open points

- **The first key.** `sandbox/release-signers` ships empty: until the
  maintainer creates a key (`scripts/release_key.sh new`), registers it and
  signs a release with it, updates are checked against `SHA256SUMS` only.
  Installations from 0.1.27 or older update with their old installer, which
  checks no signature; the version they get checks from then on.
- **Freeze.** A source that keeps serving an old release as the newest goes
  unnoticed as long as it is not older than the installed (refused) or the
  newest seen (pointed out). A signed, dated statement of the newest version
  with an expiry (as TUF's timestamp role does) would detect it; it needs
  regular signing by the maintainer.
- **CI cannot sign.** With keys listed, `release.yml` builds and tests but the
  maintainer publishes the release files. A GitHub draft release that the
  maintainer only signs and publishes would save the upload; not done (drafts
  are invisible to the tag API that `publish_release.py` uses).
- **PyPI.** `pip install -U sandbox-ai` relies on PyPI's own trusted publishing;
  the SSH signature covers only `install.sh` installations.

## Canary files (2026-11-04)

Done: decoy credentials per run (`--canaries credentials|home|workspace|all`,
default `credentials` in the sandbox HOME; project key, `defaults.json`,
policy `require_canaries`), tokens shaped like real ones but worthless; reads
and changes seen by the supervisor (inotify), tokens leaving seen by the proxy
(URL, headers, body in plain/URL/JSON/hex/base64 forms, gzip bodies, TLS
name, `Host`), the DNS stub and the push check (any remote); rulesets'
`canary` section (`on_read`, `on_send`) with notification, question, pause
(pod or process tree) and stop; `canaries.jsonl`, `sandbox-ai canaries
list|resume`. Open:

- **Who read it.** inotify names no process. fanotify (`FAN_OPEN_PERM` with
  the PID) needs `CAP_SYS_ADMIN` in the initial user namespace; a seccomp
  user-notification on `openat` for the canary paths would name it from
  inside, at a cost for every open.
- **Uninspected traffic.** `--no-inspect` hosts, `--allow-tcp`/`--allow`
  relays, SSH and WebSocket frames after an upgrade are not searched; DNS with
  `--dns` bypasses the stub. Searching the relayed bytes (a rolling window in
  `relay()`) would cover plain tokens there.
- **Split and transformed tokens.** A token cut across requests, encrypted,
  base64 with line breaks or otherwise rewritten passes. Remembering a
  per-connection tail across requests, and base64 with CR/LF removed, would
  catch the simple cases.
- **After the run.** A token the agent copied into a workspace file is found
  only when it is pushed; a scan of the workspaces' changed files after the
  run (as the privacy check does) would point it out.
- **Earlier runs' tokens.** Only the tokens of this run (and of a kept HOME)
  are searched; a token of a throwaway HOME the agent stashed in the
  workspace leaves unnoticed in a later run.
- **App.** The desktop app shows canary records as approvals; a page of its
  own (`sandbox-ai canaries list --json`) is not written.
- **Not run here:** planting and detection through real Podman and
  Bubblewrap sandboxes (the unit tests use real inotify, a real proxy with
  TLS and real Git, but no sandbox).
