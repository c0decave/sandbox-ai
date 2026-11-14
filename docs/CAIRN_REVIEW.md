# Cairn: review for sandbox-ai

Cairn (oritera/Cairn, AGPL-3.0, Python) is a blackboard for several coding agents working on one problem: a project
holds *Facts* (what is known, from the origin to the goal), *Intents* (what a worker sets out to do) and *Hints* (what
a human adds); a server keeps them in SQLite and shows them as a graph in a web UI, a dispatcher hands tasks
(bootstrap, reason, explore) to *workers*, which are coding-agent CLIs (Claude Code, Codex, Pi) run with a prompt per
task. This review covers the commit built into `localhost/cairn-sandbox` (sandbox/Containerfile.cairn): what Cairn
contacts, its telemetry, what its dispatcher starts, its use of a Docker socket, and how sandbox-ai runs it. Reviewed
on 2026-11-13.

## The commit

| | |
|---|---|
| Repository | https://github.com/oritera/Cairn, licence **AGPL-3.0** (`LICENSE`; the README offers a commercial licence besides) |
| Commit | `8e7e0ea67552383851dfcabfba0c4e9c8d007878` on `main`, 2026-09-07 ("docs: add logo"), main's newest commit and more than 14 days old (`git log` of a clone; a fetch of exactly this commit is checked against it in the image) |
| Why not a release | the last release, v0.2.1 (2026-05-10, commit `e0ef2f85`), has no local execution mode: it came with commit `233f5e8505b9` (2026-07-15, "feat: add local execution mode for host-run workers"); without it the dispatcher only starts worker containers through a Docker socket. pyproject.toml still says 0.2.1 at the reviewed commit |
| Stack | Python >= 3.12: FastAPI and uvicorn (the server), click (the CLI), PyYAML, requests, docker (the Python client, used only by the Docker mode); the web UI is one HTML file with its JavaScript vendored (Tailwind, Alpine, Cytoscape and its layouts): no CDN |
| Dependencies | `sandbox/Containerfile.cairn.lock`: upstream's own `cairn/uv.lock` exported by `scripts/cairn_lock.py` (`uv export --frozen`, no dev group, Linux on CPython 3.12): 24 packages, each with every SHA-256 upstream locked. Upstream's lock names a PyPI mirror (mirrors.aliyun.com); the image installs from pypi.org with `--require-hashes`, and every file upstream locked has the same SHA-256 on pypi.org (checked here for all 24, installed here from pypi.org with the lock). CPython 3.12 from uv's own builds (their checksums are part of uv 0.13.5) |
| Image | `sandbox-ai build cairn`: on the team image (Claude Code, Codex, OpenCode), Cairn's source at the commit (only `cairn/src`, `LICENSE`, `README.md`; no `.git`), the venv at `/opt/cairn/venv`, the command `cairn`, sandbox-ai's dispatcher configuration at `/opt/cairn/dispatch.yaml` (sandbox/recipes/cairn/dispatch.yaml). Cairn's own files are unchanged |

Verified here: the commit and its dates (a clone, a fetch by commit), the lock (made from upstream's, compared with
pypi.org, installed with `--require-hashes` into a CPython 3.12 venv), the source (grep of every host, subprocess and
socket), and a run of `cairn serve` and `cairn dispatch` in local execution mode with a stand-in `claude` on the PATH
that records how it is called, every outbound connection sent to a logging proxy that refuses it and every
non-loopback socket of the processes sampled every 0.1 s; a project was created through the API and the dispatcher
ran for 45 s. Not verified here: the image build (no Podman in this sandbox) and a start in a real sandbox with the
real CLIs.

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

## How sandbox-ai runs it

The preset `cairn-pentest-board` (category security): `sandbox-ai claude --with codex --image
localhost/cairn-sandbox:latest --allow-tcp TARGET:PORT [--allow-tcp TARGET:PORT2] [--allow-domain DOMAIN] [--vpn
PROFILE] --browser --canaries all`. Claude Code leads; its first task starts `cairn serve` (127.0.0.1:8000), creates the
project (origin: the target; goal and the first hint: the preset's values) through the API, copies
`/opt/cairn/dispatch.yaml` and starts `cairn dispatch` (local execution, workers `claude` and `codex`), then watches,
stops the dispatcher when tasks keep failing or the goal is reached, and writes `/work/FINDINGS.md`. The human sees
the board at http://127.0.0.1:8000 in the sandbox's own browser (`--browser`, Podman: the same pod, the same loopback;
the app's Browser button). The network: exactly the target's ports, the optional domain on HTTPS, and Claude Code's
and Codex's providers (`--with codex` brings Codex's login and hosts).

## Licence: what the AGPL means here

Cairn is AGPL-3.0. sandbox-ai does not ship Cairn and does not change it: `sandbox-ai build cairn` fetches its source
from GitHub at the reviewed commit on the user's machine and installs it unchanged (its `LICENSE` and `README.md` in
`/opt/cairn`). sandbox-ai's own files around it (the Containerfile, the lock, the dispatcher configuration, the
preset) are not part of Cairn. For the user: running Cairn for oneself puts no duty on them. Who changes Cairn and lets
others use it over a network (AGPL section 13), or passes Cairn on, must offer those users its source with the
changes.

## Go / No-Go

| Question | Finding | Verdict |
|---|---|---|
| Hosts of its own | none (measured, and none in the source) | Go |
| Telemetry | none | Go |
| Updates | none (no update code); a new commit is a new review | Go |
| Docker socket | only in its Docker mode, which sandbox-ai never uses; the local mode never connects | Go, local mode only |
| Its workers | the team image's claude and codex with their own approvals off, inside the sandbox's boundary | Go |
| Autonomy and cost | keeps dispatching until complete; unlimited retries of failing tasks | Go, with the preset's warning and the lead told to stop it |
| Server | no authentication, loopback only | Go (loopback only) |
| Supply chain | one maintainer; a commit, not a release; dependencies hash-locked from upstream's own lock | Go, experimental |
| Licence | AGPL-3.0, installed unchanged by the user's own build | Go |

**Go**: Cairn as the image `localhost/cairn-sandbox` and the public preset `cairn-pentest-board`, local execution
only. No blocker was found.
