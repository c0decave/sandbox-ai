# Cairn: review for sandbox-ai

Cairn (oritera/Cairn, AGPL-3.0, Python) is a blackboard for several coding agents working on one problem: a project
holds *Facts* (what is known, from the origin to the goal), *Intents* (what a worker sets out to do) and *Hints* (what
a human adds); a server keeps them in SQLite and shows them as a graph in a web UI, a dispatcher hands tasks
(bootstrap, reason, explore) to *workers*, which are coding-agent CLIs (Claude Code, Codex, Pi) run with a prompt per
task. This review covers the commit built into `localhost/sandbox-ai-cairn` (sandbox/Containerfile.cairn): what Cairn
contacts, its telemetry, what its dispatcher starts, its use of a Docker socket, and how sandbox-ai runs it: since
0.1.97 as a part of its own (`--cairn`), a container beside the agent whose workers are the session's agents. Reviewed
on 2026-11-13; the design of the part on 2026-11-15.

## The commit

| | |
|---|---|
| Repository | https://github.com/oritera/Cairn, licence **AGPL-3.0** (`LICENSE`; the README offers a commercial licence besides) |
| Commit | `8e7e0ea67552383851dfcabfba0c4e9c8d007878` on `main`, 2026-09-07 ("docs: add logo"), main's newest commit and more than 14 days old (`git log` of a clone; a fetch of exactly this commit is checked against it in the image) |
| Why not a release | the last release, v0.2.1 (2026-05-10, commit `e0ef2f85`), has no local execution mode: it came with commit `233f5e8505b9` (2026-07-15, "feat: add local execution mode for host-run workers"); without it the dispatcher only starts worker containers through a Docker socket. pyproject.toml still says 0.2.1 at the reviewed commit |
| Stack | Python >= 3.12: FastAPI and uvicorn (the server), click (the CLI), PyYAML, requests, docker (the Python client, used only by the Docker mode); the web UI is one HTML file with its JavaScript vendored (Tailwind, Alpine, Cytoscape and its layouts): no CDN |
| Dependencies | `sandbox/Containerfile.cairn.lock`: upstream's own `cairn/uv.lock` exported by `scripts/cairn_lock.py` (`uv export --frozen`, no dev group, Linux on CPython 3.12): 24 packages, each with every SHA-256 upstream locked. Upstream's lock names a PyPI mirror (mirrors.aliyun.com); the image installs from pypi.org with `--require-hashes`, and every file upstream locked has the same SHA-256 on pypi.org (checked here for all 24, installed here from pypi.org with the lock). CPython 3.12 from uv's own builds (their checksums are part of uv 0.13.5) |
| Image | `sandbox-ai build cairn`: `localhost/sandbox-ai-cairn` on Debian 12 slim pinned by its digest (pulled once at the build, labelled `sandbox-ai.base`), Cairn's source at the commit (only `cairn/src`, `LICENSE`, `README.md`; no `.git`), CPython 3.12 and the venv at `/opt/cairn`, the command `cairn`. Git and uv only in the build stage; no agent, no Node.js, no Docker client use. sandbox-ai's runtime (`cairn_runtime.py`) and the relay's client (`cairn_relay.py`) are mounted read-only at every start, its dispatcher configuration is written by the launcher. Cairn's own files are unchanged |

Verified here: the commit and its dates (a clone, a fetch by commit), the lock (made from upstream's, compared with
pypi.org, installed with `--require-hashes` into a CPython 3.12 venv), the source (grep of every host, subprocess and
socket), and a run of `cairn serve` and `cairn dispatch` in local execution mode with a stand-in `claude` on the PATH
that records how it is called, every outbound connection sent to a logging proxy that refuses it and every
non-loopback socket of the processes sampled every 0.1 s; a project was created through the API and the dispatcher
ran for 45 s. For the part (2026-11-15): Cairn at the commit with its locked dependencies in a CPython 3.12 venv,
started by sandbox-ai's own `cairn_runtime.main` - the server, the start's project made through the API (and not made
twice), the wait for the relay, the real dispatcher in local execution mode - with its `claude` the shim of
`cairn_relay.py` and the relay serving a stand-in `claude`: Cairn's real command lines (`--help`, `--session-id UUID
--dangerously-skip-permissions -p -- PROMPT`, `-r UUID ... -p -- PROMPT`) were taken, run in the project's folder and
answered; with answers that do not parse, the runtime paused the dispatcher after its failures. Not verified here: the
image build (no Podman in this sandbox) and a start in a real pod with the real CLIs (tests/test_cairn_live.py).

## Hosts contacted (measured)

| Run | Hosts |
|---|---|
| `cairn serve` (127.0.0.1:8000 by default), its web UI loaded | none |
| `cairn dispatch` in local execution mode, a project with a hint, 45 s | none of its own; its workers' traffic is theirs (Claude Code's and Codex's providers, under sandbox-ai's rules for those agents) |

The source names no analytics, crash reporting or update host; the only URLs in the Python code are the server's own
address and examples. The web UI loads nothing from outside (its scripts are vendored and served by the server).

## Telemetry

None. No analytics or telemetry library is among the dependencies (FastAPI 0.135, uvicorn 0.44 and the others in the
lock send nothing), the source has no reporting code, and the measured runs reached no host. Cairn's workers are
Claude Code and Codex: their telemetry is sandbox-ai's `--telemetry` (off by default) like in any other start.

## What the dispatcher starts

- At its start (local mode), each worker's CLI once with `--help` (is it on the PATH?).
- Per task, a process of the worker's CLI in the project's folder (`<workspace_root>/<project id>`), measured:
  `claude --session-id UUID --dangerously-skip-permissions -p -- PROMPT`, then for the conclusion
  `claude -r UUID --dangerously-skip-permissions -p -- PROMPT`; Codex: `codex exec
  --dangerously-bypass-approvals-and-sandbox -- PROMPT` and `codex exec resume ID ...`. The workers' own questions and
  Codex's own sandbox are off: Cairn relies on the environment it runs in. In sandbox-ai that environment is the
  sandbox: its firewall, connection proxy, approvals, rules (the managed hooks of Claude Code and Codex hold for
  `--with` agents), limits and logs.
- The environment of a worker is the dispatcher's own, merged with the configuration's `common_env` and the worker's
  `env` (sandbox-ai's file sets neither).
- Graph snapshots for the prompts are written to `/tmp/cairn-prompts/...`.
- **No limit on retries**: a task that fails (the worker's output does not parse, the worker errs) is handed out again
  on the next round (measured: every 3 seconds with a broken worker; `max_workers` and the timeouts bound the
  parallelism, not the number of attempts). With real workers every attempt spends model usage. sandbox-ai's preset
  says so and the lead agent is told to stop the dispatcher when tasks keep failing.
- What Cairn does autonomously: once the dispatcher runs, it keeps planning and running tasks until the project is
  marked complete or the dispatcher is stopped; workers run shell commands, write files in the project folder and
  reach whatever the sandbox lets through. The server has no authentication: anything in the sandbox (the lead, the
  workers, the sandbox's browser) may change the board; it listens on the sandbox's loopback only.

## The Docker socket

Cairn's default (docker-compose.yaml, `runtime.execution: docker`) mounts `/var/run/docker.sock` into the dispatcher
and creates a container per project with `docker.from_env()` (`dispatcher/runtime/containers.py`). A Docker socket is
root on the host: **never used in sandbox-ai**. The dispatcher creates its `ContainerManager` only when the execution
is not `local` (`dispatcher/scheduler/loop.py`); in local mode the `docker` Python package is imported but never
connects (measured: no socket, no connection). sandbox-ai's sandboxes have no Docker socket, its configuration says
`execution: local`, and the preset starts nothing else.

## A part of its own: the design

The wish: Cairn as a part of the Board of its own, like a service container beside the agent; its board opens in the
session's browser by itself; the session's agents are its workers. Up to 0.1.96 Cairn was an image on the team image,
started by the lead agent in its own sandbox (`cairn serve`, `cairn dispatch` in tmux windows), its dispatcher running
the claude and codex of that sandbox.

Cairn has a seam that allows the split: its server and web UI need nothing but the loopback, and its dispatcher talks
to the server only over HTTP; what binds the dispatcher to the workers is local execution - it starts the worker CLI
as a subprocess (`subprocess.Popen` of the driver's command line, in `<workspace_root>/<project id>`, with a graph
snapshot written to `/tmp/cairn-prompts/...` that the prompt names). The options considered:

| Option | Why (not) |
|---|---|
| The dispatcher (or all of Cairn) in the agent's container, as before | Cairn's code would run beside the agent's logins (`~/.claude/.credentials.json`, Codex's `auth.json`) with the agent's rights. Works, but it is not a part of its own, and the image is the team image with Cairn added |
| Cairn's Docker mode with worker containers | needs a Docker socket: root on the host. Never |
| A Podman socket in Cairn's container to `podman exec` the agents | the socket is the user's whole container engine (every container, image and volume of the user). Never |
| Cairn through the agents' helper MCP (`--helper`, agent_mcp.py) | a helper is another sandbox per agent with its own login and an `ask(prompt)` tool; Cairn speaks no MCP and calls CLIs. It would need a translation of Cairn's command lines into MCP calls plus one helper sandbox per worker: more parts, the same rights |
| **Cairn in its own container, its workers through a narrow relay** (chosen) | Cairn's code (server, web UI, dispatcher) runs where there is no login and no file of the project; the workers run where the agent runs, as the agent, with exactly its rights - nothing more for Cairn, nothing less for the workers |

How it runs (`--cairn`, sandbox/cairn.py):

- **Cairn's container** (`POD-service-cairn`, `localhost/sandbox-ai-cairn`) is started like the services and the
  browser (services.launch): created, initialised, proven to be in the pod's user namespace and, with `--vpn`, behind
  the VPN's kill switch, started only after the supervisor's firewall and proxy are up; the agent starts once port
  8000 answers. It shares the pod's network namespace (the agent's firewall, allow-list, connection proxy, approvals,
  kill switch, VPN or Tor), runs as the pod's root (your user on the host) with `--cap-drop=ALL`, no new privileges,
  a read-only root, `/tmp`, `/work` (the dispatcher's empty project folders) and `/data` (the board's SQLite database;
  `--cairn-board session` keeps it in the session's folder) as tmpfs, 1 GiB, 1 CPU, 512 processes. It holds no login,
  no key, no file of the project and no socket but the relay's.
- **sandbox-ai's runtime** in it (`cairn_runtime.py`, mounted read-only) starts `cairn serve` on 127.0.0.1:8000, makes
  the project the start names (`--cairn-origin`, `--cairn-goal`, `--cairn-hint`; not again when a kept board has it),
  waits for the relay, writes the dispatcher's configuration (local execution only, the session's workers, one project
  at a time) and starts `cairn dispatch` with the workers' shims first on its PATH.
- **The board opens in the session's browser**: `--cairn` brings `--browser` (Chromium, for you) where its live view
  works, and the browser starts on `http://127.0.0.1:8000/` (browser.config `start_url`, waited for up to 60 s); with
  your own `--browser` it starts there too. There was no web-UI hand-over to the browser on this branch (the hotfix
  branch's DeepSeek Harness hand-over came later); the start URL is the simpler means. On macOS the browser's live view
  is Linux-only for now: no browser is brought, the start says so, and the agent reaches the board at its address.

## The relay

`cairn_relay.py`, standard library only, both sides:

- In Cairn's container, `claude` and `codex` on the dispatcher's PATH are `cairn_relay.py worker AGENT`: they send their
  command line, their folder and the graph snapshots the prompt names (read from Cairn's `/tmp/cairn-prompts`) over a
  Unix socket and pass the output and exit status back to Cairn. Nothing of an agent is in that image.
- In the agent's container, `cairn_relay.py serve` is started by the supervisor's guard right before the agent -
  after the namespace checks, inside Firejail, with the agent's environment (runtime.start_cairn_relay) - so whatever
  it starts has exactly what the agent's own processes have: the same user, seccomp filter, mounts, firewall, proxy,
  CA, managed settings, hooks and rules. It runs under `cairn_relay.py supervise` (the same process tree, the same
  rights), which starts it again when it ends during the session - after 1, 2, 5, 10, 30 s, at most 5 times within
  10 minutes; beyond that a note beside the socket (`relay.ended`) says why, the workers say it, and Cairn's runtime
  stops the dispatcher with it (the board stays).
- It takes **only** the command lines of Cairn's `claudecode` and `codex` drivers at the reviewed commit: `--help`;
  `claude --session-id UUID|-r UUID --dangerously-skip-permissions -p -- PROMPT`; `codex exec
  --dangerously-bypass-approvals-and-sandbox -- PROMPT` and `codex exec resume ID ... -- PROMPT`. No other option, no
  other program, no shell line; the folder must be `/work/cairn/runs/PROJECT` (made, no symbolic link on the way); a
  file only a graph snapshot of Cairn's (`/tmp/cairn-prompts/PHASE-HEX/graph.yaml`, at most 4 MiB, 8 per call; the
  relay keeps the newest 64 of them, 64 MiB at most, and removes the older ones it wrote). What
  sandbox-ai adds - the worker's model, the lead's `--model`/`--effort`, Codex's `--llm` profile - comes from the
  launcher's configuration, never from Cairn. Claude Code is told `IS_SANDBOX=1` (as `-D` does), since Cairn gives it
  `--dangerously-skip-permissions`.
- Cairn's third driver, pi, runs `/bin/sh -lc SCRIPT` in local mode: an arbitrary shell line, which no narrow relay
  can take. It is never used: Pi is a worker through the claudecode driver like every other harness (below).
- The socket: a folder beside the run's directory (only yours; on macOS a Podman volume of the run in the VM, since a
  socket does not cross podman machine's shared folders), read-write for the agent's container (it makes the socket,
  0600), read-only for Cairn's. Nothing else in the pod mounts it; abstract sockets, which every container of the pod
  would reach, are not used.
- When the shim goes (Cairn's timeout, the dispatcher stopped), the CLI's process group gets SIGTERM, then SIGKILL; a
  worker runs at most 3 hours whatever Cairn's timeouts say; at most 8 calls at once.

## Every harness as a worker (2026-11-22)

The user asked why only Claude Code and Codex could be Cairn's workers. Every harness of the launcher can be one now,
without a change to Cairn. What Cairn's drivers expect (read at the reviewed commit:
`dispatcher/workers/adapters/claudecode.py`, `codex.py`, `workers/base.py`, `tasks/*.py`, `output_parser.py`):

| | claudecode driver | codex driver |
|---|---|---|
| Command lines | `claude --session-id UUID --dangerously-skip-permissions -p -- PROMPT`; the conclusion `claude -r UUID ... -p -- PROMPT` | `codex exec --dangerously-bypass-approvals-and-sandbox -- PROMPT`; `codex exec resume ID ...` |
| Session | made by Cairn (a UUID) before the call | read from the error output (`session id: ...`) |
| The answer | standard output as it is; Cairn looks for a JSON object in it (the whole text, a fenced block, or the first `{...}` that parses) | the same |
| Success | exit status 0 (124 or 137: a timeout); an answer that does not parse leads to the conclusion in the same session | the same |
| Environment | the dispatcher's, merged with the worker's `env` from the configuration | the same |

So sandbox-ai keeps Cairn calling its claudecode (or codex) driver and maps the worker in its own relay:

- The dispatcher's configuration (written by the launcher) gives every worker `env: {SANDBOX_AI_CAIRN_WORKER:
  AGENT}` and the type `claudecode` (`codex` for Codex). Cairn hands that variable to the process it starts - the
  shim `claude` - which sends it to the relay with the command line (`{"agent", "driver", "argv", "cwd", "files"}`).
- The relay checks, as before, that the harness is a worker of this session, that it is called through its own
  driver's shim (Codex through `codex`, every other one through `claude`), that the command line is exactly the
  claudecode driver's and the folder one of `/work/cairn/runs`. It then builds the harness's own one-shot command
  line itself (`cairn_relay.harness_argv`): `opencode run`, `opencode2 run --auto`, `hermes --oneshot=`,
  `dsh headless --patch ... -` (the task on standard input), `qwen --yolo --prompt=`, `goose run --quiet --text=`,
  `openhands task --text=`, `nanobot agent --no-markdown --session cairn-UUID --message=`, `letta -p --new --yolo`,
  `pi -p` - the forms of the helpers' one-shot runs (`agent_mcp.command`; a test keeps them the same), with the
  launcher's arguments for that worker (its model) and the harness's own switch to work without questions
  (`agent_mcp.SKIP`, the launcher's `DANGER_ENV`). Nothing of Cairn's but the prompt reaches the command line, and
  the prompt is never an option (after `--`, as an option's own value, or on standard input).
- The answer: the harness's standard output is kept (16 MiB at most), terminal escapes and nanobot's header taken
  out, and its final message sent back as the standard output Cairn reads, with the exit status; the error output
  is passed on as it comes.
- The conclusion (`-r UUID`) goes on in exactly the task's own conversation. Only nanobot's one-shot run can be
  named a conversation (`--session cairn-UUID`, the same for both calls). The others' "continue" goes on in the last
  conversation of the folder or of the HOME - which can be the lead's own, since the lead and the workers share the
  container. So for them the relay answers the conclusion with exit status 1 and says why (`NoSession`), and runs
  nothing: the task fails and Cairn hands it out again (the pauses below bound that). Never a conversation that may
  be another's.
- A model per worker (`--cairn-workers AGENT=MODEL`) only for a harness with a model option on its command line
  (claude, codex, opencode, hermes, qwen, letta, pi); for the others it is refused (their model is their settings').
- Codex workers get the lead's `-c` telemetry overrides (they win over every config.toml) on their command line.
- OpenHands is a worker beside another lead only: as the lead `--cairn` stays refused (its Agent Canvas holds port
  8000). With `--llm` only the lead is a worker (`--with` is refused with `--llm`).

What this gives a compromised Cairn container beyond what it had: the choice of which of the session's workers runs
a task (the variable) - which it had already by its configuration's task types. Each harness still runs as the
agent, with the launcher's arguments, in a project folder, with no shell line and no option of Cairn's.

**Support, shown as a traffic light.** `sandbox/cairn_workers.json` holds each harness's status in the model lights'
vocabulary (verified green, smoke-only and partial yellow, failing and known-bad red, untested grey - shown as "no data yet"); a status other
than untested must name its run (a CI job, a live check's report; `cairn_workers.load` refuses it otherwise), a red
one its reason. `sandbox-ai cairn workers`, the start's notes, its dry run, `presets show` and the app's Board
(each harness with its light in the worker field) show it; a red harness is refused with its reason and another
agent of the session offered. As of this change:

| Harness | Light | From |
|---|---|---|
| claude | partly working | CI's job agents-live (cairn): one task of the real dispatcher in a real pod through the relay to the real Claude Code (a scripted model); a real answer taken as a fact is not measured there |
| codex | no data yet (untested) | its relay command lines are tested with stand-ins (tests/test_cairn_relay.py) |
| opencode, opencode2, hermes, dsh, qwen, goose, openhands, nanobot, letta, pi | no data yet (untested) | the relay's command line, the answer and the refusals are tested with stand-ins (tests/test_cairn_relay_harnesses.py); real Cairn at the reviewed commit (its server and dispatcher in local execution mode, with sandbox-ai's runtime, shims and relay) completed a project with a stand-in of each, 2026-11-22 (tests/test_cairn_real_dispatcher.py, opt-in: SANDBOX_AI_CAIRN_PYTHON) - stand-ins, not the real CLIs |

The live check `cairn-workers-live` (`scripts/live_check.sh --only cairn-workers-live --llm NAME`) runs one small task
of real Cairn with each harness (OpenHands skipped with the reason) and gives a row per harness for the table.

What a compromised Cairn container could do: give the session's agents prompts in `/work/cairn/runs` - which is what
Cairn does anyway - and post to its own board. It cannot read the agents' logins, run a shell line, reach the engine or
leave the pod's network. The relay gives Cairn no more than the agents have, and the agents no more than their start.

**Cost.** Cairn hands a failing task out again without a limit, and a worker whose answer does not parse fails with
exit status 0 (measured: a new attempt about every second with an answer that does not parse). sandbox-ai bounds it
twice, without changing Cairn: the runtime reads the dispatcher's log and pauses it after 5 failed tasks within 10
minutes, the relay pauses the workers after 5 failed runs in a row (non-zero exit: a provider that refuses, a missing
login); each pause 60 s, doubled up to 15 minutes. Stopping the project on the board (or `PUT
/projects/ID/status {"status": "stopped"}`) stops it.

## Models and providers

Cairn itself calls no model: in local mode its workers are the CLIs, and its Docker mode's model settings
(`ANTHROPIC_BASE_URL`, `CODEX_BASE_URL`, its health checks) are never used. So every provider the agents support holds
for Cairn's workers, because they are the session's agents as they were started: a login (`sandbox-ai login`),
`--api-key` (the key added by the proxy on the wire), OpenRouter, and `--llm` servers - Claude Code's environment and
settings from the launcher, Codex's `--profile` added by the relay - with the relay's parameters and the **loop guard**
on the wire, the same for every request the workers make. `--cairn-workers claude=MODEL,codex=MODEL` gives each worker
a model of its own; without it the lead's worker gets the lead's `--model` and `--effort`, an agent `--with` its own
default. With `--llm` the model is the server's (`--llm NAME:MODEL`), and a model of its own for that worker is
refused.

The model's real context window: the hotfix branch (0.1.96) passes it to Claude Code as
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` in the agent's environment for an `--llm` model. The relay runs with the agent's
environment, so Cairn's Claude Code workers get the same window once that is merged; nothing of it is repeated here.

The public preset `cairn-local-llm` (category Local LLMs): `claude --llm SERVER --no-provider-defaults --llm-param
loop_guard=on --cairn --cairn-workers claude`, the project's origin, goal and first hint optional. `cairn-pentest-board`
(security): `claude --with codex --cairn` with the target's ports, the project from the preset's values, the
sandbox's browser on the board, the lead told how to watch, hint, stop and write `/work/FINDINGS.md` through Cairn's
API.

## Licence: what the AGPL means here

Cairn is AGPL-3.0. sandbox-ai does not ship Cairn and does not change it: `sandbox-ai build cairn` fetches its source
from GitHub at the reviewed commit on the user's machine and installs it unchanged (its `LICENSE` and `README.md` in
`/opt/cairn`). sandbox-ai's own files around it (the Containerfile, the lock, the runtime, the relay, the dispatcher
configuration the launcher writes, the presets) are not part of Cairn and do not modify it: they start Cairn's own
commands and put programs of sandbox-ai's on its PATH. For the user: running Cairn for oneself puts no duty on them. Who changes Cairn and lets
others use it over a network (AGPL section 13), or passes Cairn on, must offer those users its source with the
changes.

## Go / No-Go

| Question | Finding | Verdict |
|---|---|---|
| Hosts of its own | none (measured, and none in the source) | Go |
| Telemetry | none | Go |
| Updates | none (no update code); a new commit is a new review | Go |
| Docker socket | only in its Docker mode, which sandbox-ai never uses; the local mode never connects | Go, local mode only |
| Its workers | the session's agents of any harness through the relay (Cairn's claudecode and codex driver command lines only, every other harness's own one-shot line made by the relay), their own approvals off, inside the agent's boundary; the conclusion only in the task's own session | Go |
| Its container | no login, no project files, no capabilities, read-only, the pod's network; only the relay's socket | Go |
| Autonomy and cost | keeps dispatching until complete; unlimited retries of failing tasks | Go, with sandbox-ai's pauses (runtime and relay), the start's warning and the lead told to stop it |
| Server | no authentication, loopback only | Go (loopback only) |
| Supply chain | one maintainer; a commit, not a release; dependencies hash-locked from upstream's own lock | Go, experimental |
| Licence | AGPL-3.0, installed unchanged by the user's own build | Go |

**Go**: Cairn as a part of its own (`--cairn`, the image `localhost/sandbox-ai-cairn`), its workers the session's
agents through the relay, and the public presets `cairn-pentest-board` and `cairn-local-llm`; local execution only. No
blocker was found.
