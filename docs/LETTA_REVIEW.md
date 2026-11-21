# Letta Code: review for sandbox-ai

Letta Code (letta-ai/letta-code, Apache-2.0, a TypeScript CLI from the makers of MemGPT; the company sells Letta
Cloud) as a sandbox-ai agent, `sandbox-ai letta`. Letta Code's agents keep a long-term memory they rewrite themselves
(memory blocks in a git-tracked memory folder, "MemFS"), learn skills, dream between turns and call each other. This
review covers the release built into `localhost/letta-sandbox` (sandbox/Containerfile.letta): every host Letta Code
contacts by default, its telemetry, its updates, its two backends (Letta Cloud and the local one), its channels,
remote computers, skills and mods, MCP, its own sandbox, and the switch for each. Reviewed on 2026-11-14.

## The release

| | |
|---|---|
| Version | `0.34.36`: npm `@letta-ai/letta-code@0.34.36`, published 2026-10-31 02:24 UTC; GitHub release `v0.34.36` 2026-10-31 02:29 UTC; 14 days before the review |
| Commit | `8f72222dec142dfc2316692d68ada3ab2e439d46` (`git ls-remote`: `refs/tags/v0.34.36`; the npm package's `gitHead` says the same) |
| Why this one | the newest release at least 14 days old (sandbox-ai's rule for third-party releases). Letta Code releases several times a day: 23 newer ones up to 0.34.59 (2026-11-13), all younger than 14 days |
| Package | `sha512-zpzMgkFNh99QNuAh9lS423WxOFToLdscUBSnWEt1hTLEwl5vP0L7lT8hdPgCGeD78CqG5lHU7Wn+z6WEvUXfyw==` (the registry's integrity), with an SLSA v1 provenance attestation on the registry (read here, its Sigstore signature not verified here). The package ships its own `npm-shrinkwrap.json`: every one of its 406 dependencies pinned with a sha512 integrity |
| Install | `sandbox/Containerfile.letta`: `npm ci --ignore-scripts` from `Containerfile.letta.lock` (scripts/agent_npm_lock.py letta: 408 packages, each with its integrity, every version published before 2026-10-31 08:00 UTC; the shrinkwrap's pins), read-only in the image. No install script runs - not Letta Code's own `postinstall` (Ink patches for its unbundled copy; `letta.js` is one bundle with its Ink patched at build), not Scarf's (`@scarf/scarf`: install analytics), not `node-pty`'s (a native build), not `sharp`'s, not `@vscode/ripgrep`'s (a download from GitHub). The packages of other systems (darwin, win32, other processors) and the Electron-only `@janhapke/sharp-electron` are removed after the install (960 MB to about 400 MB). Never `npm install -g` at run time, never the PyPI wheel with its bundled Node |
| Licence | Apache-2.0 (GitHub's licence API, the package's `LICENSE`) |

Verified here: the dates, the commit and the integrity (npm's registry, GitHub's API, `git ls-remote`), the
lock (`npm install --package-lock-only --before=...`: the shrinkwrap's pins), the install (`npm ci --ignore-scripts`
from the lock, then the removal of other systems' packages; `letta --version`, `--help` of the subcommands), the
source at the tag (grep of every host, variable and switch named below), and runs of the installed package against a
local fake OpenAI-compatible model on the loopback, every DNS lookup, TCP connection and `fetch` of the process
logged and every one off the loopback refused (a `--require` preload in Node; Letta Code's `fetch` does not use a
proxy variable): headless (`letta -p`), interactive (the TUI in tmux, a message typed, 30 s and 135 s), with
upstream's defaults and with sandbox-ai's settings (below); `letta connect`, `letta agents create`, `letta mcp tools`
and sandbox-ai's start script with an MCP server. Not verified here: the image build (no Podman in this sandbox: the
image builds in CI or on the host) and a start in a real sandbox; hence the release ships Letta Code as not
live-tested yet (the start says so in one line; no opt-in).

## Hosts contacted by default (measured)

All runs with the local backend (`LETTA_LOCAL_BACKEND_EXPERIMENTAL=1`: what sandbox-ai uses; with Letta Cloud, the
upstream default, everything goes to `api.letta.com` after a sign-in at `app.letta.com`).

| Run | Upstream's defaults | sandbox-ai's settings |
|---|---|---|
| headless (`letta -p -- "hi"`) | `registry.npmjs.org` (the update check: `/@letta-ai/letta-code/latest`), `api.letta.com` (`/v1/health`: the server's version for telemetry; `POST /v1/metadata/telemetry`: the session's events - also with the local backend), the loopback's model ports (127.0.0.1:1234 LM Studio, localhost:11434 Ollama, localhost:8080 llama.cpp: its probe for local servers), the model | the loopback's model ports, the model |
| interactive (`letta`, a message), 135 s | the same, and `api.letta.com/v1/metadata/balance` twice (the billing tier, without a key), `api.github.com/repos/sharkdp/fd/releases/latest` (it downloads `fd` for its `@` file completion when none is installed) | the loopback's model ports, the model; `fd` comes with the image (`fd-find`), so nothing is downloaded |
| `letta update`, auto-update at start | `registry.npmjs.org`, then `npm install -g` | refused (`DISABLE_AUTOUPDATER=1`; `letta update` in the agent's arguments refused) unless `--letta-enable updates`; the install is read-only either way |
| `letta install`, `letta skills install` | GitHub, `clawhub.ai`, npm | refused unless `--letta-enable skills-install` |
| `letta server` (remote computer), `--computer`, `letta teleport`, `letta computers` | Letta Cloud | refused unless `--letta-enable cloud` |
| `letta server --channels telegram` and the other channels | their messaging services, and `npm install` of the channel's runtime at its first start | refused (not in a sandbox) |

## How sandbox-ai configures Letta Code

Letta Code reads its settings from the user's files only: `~/.letta/settings.json` (global; `LETTA_CONFIG_DIR`
moves it) and the project's `.letta/settings.json` and `.letta/settings.local.json`. There is no system layer that
wins over them, so what must hold is a variable of Letta Code's process, which the agent cannot change
(sandbox/letta.py: `BASE_ENV`, `TELEMETRY_ENV`, `environment`):

- `LETTA_LOCAL_BACKEND_EXPERIMENTAL=1`: the **local backend** (`src/backend/local/`). Agents, their memory (MemFS: a
  git repository per agent, `~/.letta/lc-local-backend/memfs/AGENT/memory`), conversations and provider keys live in
  `~/.letta/lc-local-backend` of the sandbox's HOME, kept with the start (`sandbox-ai resume NAME`). It needs no
  Letta server and no account (measured: offline against a model on the loopback). Upstream calls it experimental;
  its first-run screen offers it beside Letta Cloud ("proceed locally").
- `LETTA_BASE_URL=http://127.0.0.1:9`: the Letta server's address is the discard port on the sandbox's loopback. The
  local backend still asks its server for a few things (the billing tier at every interactive start, the server's
  version for telemetry); with this they end at once inside the sandbox (measured: no call to `api.letta.com`).
- `DISABLE_AUTOUPDATER=1`: no update check, no `npm install -g` at start.
- `LETTA_FS_SANDBOX=0`: Letta Code's own filesystem sandbox (bwrap or Seatbelt around its memory subagents) stays
  off: sandbox-ai is the boundary; its in-process cross-agent memory guard stays on.
- `LETTA_SKIP_KEYCHAIN_CHECK=1` (no Secret Service in a container), `LETTA_DISABLE_LSP_DOWNLOAD=1`.
- The telemetry opt-outs (below), never dropped.

sandbox-ai's start script (`/etc/letta/sandbox-ai-start.py`, read-only from the session directory; `letta.command`)
runs before Letta Code and then becomes it (`exec`): Letta Code keeps MCP servers per agent (`mcpServers` of the
agent's entry in `~/.letta/settings.json`; no flag or variable names them), so the script lays the `--mcp` servers
(each the launcher's stdio bridge) into every agent there, takes out the ones an earlier start laid in that this one
has not, and on a first start creates the agent Letta Code would create (`letta agents create`, local, no model
call) so its servers are there from the first turn. It never follows a symlink; if anything goes wrong, Letta Code
starts all the same. Measured: `letta mcp tools` lists the bridged server's tools, the model receives them.

- `-D` is `--yolo` (no approvals; Letta Code asks before what changes something, upstream's default).
- `--model MODEL` is Letta Code's `--model` (a handle such as `anthropic/claude-sonnet-4-5`).
- `--llm NAME[:MODEL]` (below) and `--api-key` (below).

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the firewall and the connection proxy refuse it even
if Letta Code tries. A switch in `code` is a feature of `--letta-enable FEATURE[,FEATURE...]` (`-lx`; per start, in
presets and project files, and in the app's Letta Code section), the same form as Hermes' `--hermes-enable`: each
opens only its own hosts and shows in the dry run's `"letta"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Telemetry (`src/telemetry/`: session, usage and error events, `POST /v1/metadata/telemetry` to its Letta server) | **on**, also with the local backend (measured) | **always off, not switchable** (see Telemetry) | none |
| Scarf install analytics (`@scarf/scarf`'s install script) | at `npm install` | never runs (`npm ci --ignore-scripts`); `*.scarf.sh` never opened | none |
| Backend | **Letta Cloud** (`api.letta.com`; sign-in at `app.letta.com`): agents, memory and conversations in the account | the local backend in the sandbox's HOME; Letta Cloud closed (its address is a closed port) | `cloud` (api.letta.com, app.letta.com): the variables of the local backend and the closed address are not set, `/login` signs in (kept in the sandbox's HOME), `--backend cloud`, remote computers (`letta server`, `--computer`, `letta teleport`) are allowed. Telemetry stays off: the proxy refuses its paths on api.letta.com |
| Updates (`src/updater/auto-update.ts`: a check at every start, then `npm install -g`; `letta update`) | **on** | off (`DISABLE_AUTOUPDATER=1`), `registry.npmjs.org` closed, `letta update` refused; the install is read-only | `updates` (registry.npmjs.org): the check and its notice; installing fails on the read-only install. A new Letta Code is a new review, then `sandbox-ai build letta` |
| Skills and mods from elsewhere (`letta install`, `letta skills install`: GitHub, ClawHub, npm packages) | on demand | closed; `letta install` refused | `skills-install` (github.com, api.github.com, codeload.github.com, raw.githubusercontent.com, objects.githubusercontent.com, clawhub.ai, registry.npmjs.org) |
| The project's own hooks while rules for its own tools hold (they run before sandbox-ai's rule check) | run | a project with hooks does not start Letta Code | `project-hooks` (no host) |
| Channels (`letta server --channels telegram,slack,discord,whatsapp,signal`) | on demand; each installs its runtime from npm at its first start (`src/channels/runtime-deps.ts`) | refused (`--channels`, `--install-channel-runtimes`): code installed at run time is not reviewed and not pinned | none |
| Remote computers (`letta server`, `letta computers`, `--computer`, `letta teleport`, `letta sandbox`) | with Letta Cloud | refused unless `cloud` | `cloud` |
| App Server (`letta server --listen [url]`, also OpenAI-compatible routes) | on demand, loopback by default | on the sandbox's loopback only; another address refused | - |
| Local endpoints' probe (127.0.0.1:1234, localhost:11434, localhost:8080: LM Studio, Ollama, llama.cpp) | at every start | on the sandbox's own loopback (nothing listens unless you run a server there); `--llm` reaches your server | - |
| Helper downloads (`fd` and `ripgrep` from GitHub when absent; language servers) | when absent | `fd-find` and `ripgrep` in the image; LSP downloads off (`LETTA_DISABLE_LSP_DOWNLOAD=1`), GitHub closed | - |
| MemFS sync to a git remote (`/memory-repository set git@github.com:...`) | off until set | the remote's host is closed (SSH and GitHub) | `--allow-github`, SSH keys of yours |
| Memory, dreaming, reflection subagents, crons and heartbeats (`letta cron`) | on | on, local: they run in the sandbox while it runs, with the same model | - |
| Hooks, permissions, mods (`~/.letta/mods`: local code that changes the harness) | the user's | the user's, in the sandbox's HOME: they run as the agent, inside the sandbox (`sandbox-ai resume --review-home` shows changes to them); with `--vpn` one of sandbox-ai's beside them, `sandbox-ai-network.mjs` (the run's network note before the first message of each conversation, written by the start script and taken out by a start without a note) | - |
| Cursor's agent as a tool (`@cursor/sdk`, `CURSOR_API_KEY`) | with a key | closed (Cursor's hosts) | `--allow-host`, `--api-key` |
| Its own sandbox (bwrap/Seatbelt for memory subagents, `LETTA_FS_SANDBOX`) | on where it works | off: this sandbox is the boundary | none |
| Docker | none (a version check of a self-hosted Letta server's `/v1/health`) | the loopback's discard port | - |

### Telemetry: always off, never switchable

The user's rule: no telemetry, analytics, crash reporting or usage pings, and no switch that turns them on - not a
feature of `--letta-enable`, not `--telemetry on`, not a preset or project file, not the agent's own arguments.

Also from inside the sandbox (sandbox/telemetry_guard.py, since 0.1.97): `letta.js` imports sandbox-ai's telemetry
guard first, which refuses `LETTA_CODE_TELEM` set to anything but `0`, `SCARF_ANALYTICS` but `false`, `DO_NOT_TRACK`
but `1` and an OpenTelemetry/OTLP variable of its own with one message (exit status 2), and pins them again.

What the review found: Letta Code's telemetry (`src/telemetry/index.ts`) is on by default: a device id
(`settings.json`), session start and end, errors (classified server-side, PostHog per its comments), tool usage,
reflection feedback and the like, queued and posted every two minutes and at exit to `POST /v1/metadata/telemetry`
of its Letta server - `api.letta.com` - with the local backend too (measured), and `/v1/health` for the server's
version. `LETTA_CODE_TELEM=0` (or `false`) and `DO_NOT_TRACK=1` turn it off before anything is queued
(`isTelemetryEnabled`, read at `init`, `track` and `flush`). The bundle names no other analytics or crash reporting
host (no PostHog, Sentry, Segment, Mixpanel, Amplitude client). `@scarf/scarf`'s install analytics run only from its
install script.

What sandbox-ai does in every Letta Code run, whatever else is enabled (sandbox/letta.py: `TELEMETRY_ENV`,
`TELEMETRY_HOSTS`, `TELEMETRY_PATHS`, `telemetry_rules`, `check_telemetry`):

- the variables are set, and no feature drops them: `LETTA_CODE_TELEM=0`, `DO_NOT_TRACK=1`, `SCARF_ANALYTICS=false`;
- without `cloud` the Letta server is a closed port on the loopback, so even a telemetry call that ignored the
  variables would go nowhere;
- the connection proxy refuses `api.letta.com/v1/metadata/telemetry/**` and `/v1/metadata/feedback/**` in every
  Letta Code run (the ruleset `telemetry`), also with `cloud`, where api.letta.com is open for Letta Cloud itself;
- `*.scarf.sh` is never opened and refused by the proxy; a start that would open it (`--allow-host`, a wildcard over
  it), hide it or api.letta.com from the proxy (`--no-inspect`) or go out without the proxy (`--egress-all-allow`
  with `--no-proxy`) is refused;
- the dry run's `"letta"` shows `"telemetry": {"switchable": false, ...}`.

Measured with these settings: no call to `api.letta.com` or `registry.npmjs.org` in any run. Tests: tests/test_letta.py
and the feature matrix's `letta-telemetry-is-never-switchable` (every feature with `--telemetry on`: the plan opens
no telemetry host, the variables and proxy rules hold, the refusals).

### What runs when Letta Code starts

sandbox-ai's start script (above), then `letta`: its settings (`~/.letta/settings.json`, the project's
`.letta/`), the backend (local), the update check (off), the telemetry (off), Letta Code's probe of the loopback for
local model servers, the provider's model list, `fd`'s and `ripgrep`'s lookup (the image's), skills (bundled, global
`~/.letta/skills`, the project's `.agents/skills`, the agent's own in MemFS), mods (`~/.letta/mods`), the agent and
its conversation (the project's last, else a new one). The first interactive start of a new HOME offers `/init`
(its memory's first pass over the project) and `/login` (Letta Cloud: closed here).

### Providers

- **API keys**: `--api-key api.anthropic.com=ANTHROPIC_API_KEY`, `api.openai.com=OPENAI_API_KEY`,
  `openrouter.ai=OPENROUTER_API_KEY` and the other providers Letta Code reads from variables (pi-ai's
  `getEnvApiKey`): Letta Code sees a placeholder, the proxy adds the key on the wire; `--model` names the handle
  (`anthropic/...`). `letta connect PROVIDER` keeps keys in `~/.letta/lc-local-backend/providers/auth.json` of the
  sandbox's HOME instead.
- **`--llm NAME[:MODEL]`** (LM Studio, Ollama, vLLM, llama.cpp, OpenAI-compatible services, OpenRouter): Letta Code's
  `llama-cpp` provider, the one OpenAI-compatible provider it reads from variables: `LLAMA_CPP_BASE_URL` (the
  server's `/v1` URL), `LLAMA_CPP_API_KEY` (the placeholder) and `--model llama-cpp/MODEL` (measured: chat completions
  with tools reach the server; it asks `/props` for llama.cpp's context window, which other servers answer with 404).
  The proxy's loop guard notes hold; Letta Code has no read-only place for the loop guard's instructions or a turn
  limit, so those are not given.
- **No login sandbox-ai takes in**: `sandbox-ai login letta` does not exist, nothing of the host's `~/.letta` is copied
  in; Letta Cloud's sign-in (`cloud`) happens in the sandbox and stays in its HOME.

### Memory

Letta Code's long-term memory is per agent: memory blocks (`persona.md`, `human.md`, `MEMORY.md` and others) in its
MemFS folder, compiled into the system prompt, plus its conversations (recall). With the local backend all of it is
in `~/.letta/lc-local-backend` of the start's HOME, kept with the start (`sandbox-ai resume NAME`, `--state-dir`).
sandbox-ai's project memory for Letta Code is `~/.letta/AGENTS.md`, kept per project like the other agents' memory
files (the sandbox-ai skill tells Letta Code to read and update it); Letta Code itself does not read an AGENTS.md at
start.

### Not (yet) for Letta Code

- `--with` (Letta Code is not in the team image): use `--helper` in either direction (`sandbox-ai letta --helper
  claude`, `sandbox-ai claude --helper letta`, which runs `letta -p -- TASK`).
- `--effort` (Letta Code sets reasoning per model with `/model`), teams, the activity feed of its turns,
  `mcp attach --live`.
- `exec_command` with `tty: true` (Codex-style toolset): its PTY needs `node-pty`'s native build, an install script
  that does not run here; commands without a TTY (every other shell tool) work.

## Rules for its own tools and kit hooks (2026-11-16)

Letta Code 0.34.36 has Claude Code's hooks (`src/hooks`), read from the user's `~/.letta/settings.json` and the
project's `.letta/settings.json` and `settings.local.json` - no system layer. An event's hooks run one after the
other, project-local first, the user's last, each on the call's original input; exit status 2 blocks; the first
`updatedInput` wins (`executor.ts`); `hooks.disabled` of the user's level wins over the project's (`loader.ts`).
Timeouts are milliseconds.

- sandbox-ai's start (`/etc/letta/sandbox-ai-start.py`) lays the run's hooks first into each event of the user's
  settings at every start, takes an earlier start's out (commands that are sandbox-ai's), sets `hooks.disabled:
  false` there, and does not start Letta Code when it cannot (a symlinked or broken settings file, an unreadable
  start configuration). The kits' `PreToolUse` hooks come before the rule hook (the wrapper checks their rewrite);
  the rule hook answers an allowed call with the input it checked as `updatedInput`, so a user's hook after it
  cannot rewrite the call.
- Letta Code names its tools as Claude Code does (`Bash`, `Write`, ...; its Codex-style set: `ExecCommand` Bash,
  `ApplyPatch` apply_patch).
- A project's hooks (`.letta/settings.local.json`, `.letta/settings.json` of the working folder) run before the
  user's and their `updatedInput` would win: Letta Code has no way to put the user's first or to switch off only the
  project's. While rules hold, the start script does not start Letta Code when either file has hooks (or cannot be
  read as JSON), naming the file - unless the human trusts them with `--letta-enable project-hooks` (it opens no
  host). Settings are read at Letta Code's start, so a hook the agent adds during a run counts at the next start,
  which checks again.
- Not covered: a `letta` the agent starts from its own shell skips the start script (a second agent, as for Claude
  Code); Letta Code's mods are trusted code in its process.

## Go / No-Go

**Go, not live-tested yet** (choosing it starts it; the start says so in one line), **with the local backend only**;
Letta Cloud is an explicit opt-in (`--letta-enable cloud`) that opens only its two hosts.

- With its telemetry and update check off and Letta Cloud's address closed, Letta Code contacts nothing but its model
  (and the sandbox's own loopback): measured, headless and interactive. The local backend works fully offline from
  Letta Cloud against an OpenAI-compatible server.
- Its telemetry has an off switch read before anything is queued (`LETTA_CODE_TELEM=0`, `DO_NOT_TRACK=1`), its
  server's address is closed, and the proxy refuses its paths even where Letta Cloud is opened.
- The package pins every dependency with an integrity (its shrinkwrap and sandbox-ai's lock); nothing runs at
  install; no self-update in the sandbox (read-only install, `DISABLE_AUTOUPDATER`, `letta update` refused).
- MCP, local models, skills, subagents and a self-edited long-term memory that lives in the sandbox's HOME.
- Against: the local backend is still called experimental upstream; the release cadence is extreme (several a day:
  the 14-day rule pins a release about 20 behind); the agents rewrite their own memory, skills and mods (all inside
  the sandbox's HOME, shown by `--review-home`); MCP servers are per agent, laid in by sandbox-ai's start script;
  not live-tested in a sandbox yet. Hence experimental, and the review is repeated for every release taken in.
