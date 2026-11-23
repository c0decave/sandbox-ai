# Pi: review for sandbox-ai

Pi (earendil-works/pi, MIT; Mario Zechner's coding agent, started as `badlogic/pi-mono` and now kept by Earendil Works,
Armin Ronacher's company: GitHub moved the repository, id 1035029907, `badlogic/pi-mono` redirects to it) as a
sandbox-ai agent, `sandbox-ai pi`. Pi is a small terminal coding agent: four tools by default (`read`, `bash`, `edit`,
`write`), sessions kept as trees, compaction, skills, prompt templates, extensions in TypeScript, MCP, and a print
mode for one task. It is the same Pi that the other reviews name: Cairn's third worker (`pi` CLI,
docs/CAIRN_REVIEW.md), one of the ACP agents OpenHands' Canvas can drive (docs/OPENHANDS_REVIEW.md), and the maker of
`@earendil-works/pi-ai`, the provider library inside Letta Code and DSH (their locks hold it). This review covers the
release built into `localhost/pi-sandbox` (sandbox/Containerfile.pi): every host Pi contacts by default, what runs at
its start, its update and package installs, its telemetry, its own sandboxing, MCP, logins, and the switch for each.
Reviewed on 2026-11-15.

## The release

| | |
|---|---|
| Version | `1.4.0`: npm `@earendil-works/pi-coding-agent@1.4.0`, published 2026-10-21 22:48 UTC; GitHub release `v1.4.0` 2026-10-21 22:49 UTC; 24 days before the review |
| Commit | `4a080252f06caf63a525d7873c5d94d3c23887e0` ("Release v1.4.0", 2026-10-21; `git ls-remote`: `refs/tags/v1.4.0`, a lightweight tag) |
| Why this one | the newest release at least 14 days old (sandbox-ai's rule for third-party releases). Newer: 1.5.0 (2026-11-02, 13 days), 1.5.1 and 1.5.2 (2026-11-09/10). The old package `@mariozechner/pi-coding-agent` (last 0.73.1, 2026-05-07) is deprecated upstream ("please use @earendil-works/pi-coding-agent") |
| Package | `sha512-Z41LpZ3yYHSC/UUps6Mr39UMFPwH8elhJBG9DY1P7kuzsE5qSK/6eMj2EHzO5evJRQWLbKSqUMw+4nCNtIyiQg==` (the registry's integrity), with an SLSA v1 provenance attestation on the registry (read here, its Sigstore signature not verified here). 1,279 files, 23 MB; the command `pi` is `dist/bundle/cli.js`, a bundle of Pi and most of its libraries. Its 22 dependencies are pinned to exact versions (Pi's own packages `pi-ai`, `pi-agent-core`, `pi-tui`, `pi-mcp`, `pi-codemode`, all 1.4.0); theirs (the provider SDKs: `openai`, `@anthropic-ai/sdk`, `@google/genai`, the AWS SDK for Bedrock, ...) are ranges |
| Install | `sandbox/Containerfile.pi`: `npm ci --ignore-scripts` from `Containerfile.pi.lock` (scripts/agent_npm_lock.py pi: 121 packages, each with its sha512 integrity, every version published before 2026-11-01 00:00 UTC, so at least 14 days old), read-only in the image. No install script runs: of the 121 only `@google/genai` and `protobufjs` have one (protobufjs' `postinstall` checks its own version; neither is needed). `pi-tui`'s prebuilt native helpers for other systems (darwin, win32) and processors are removed after the install. Never `npm install -g` at run time, never Pi's installer (`curl -fsSL https://pi.dev/install.sh \| sh`) |
| Licence | MIT (GitHub's licence API, the package); the 121 packages: MIT, Apache-2.0, ISC, BSD-3-Clause, 0BSD, BlueOak-1.0.0, Unlicense |

Why the lock's date is not the release's: with the dependencies resolved as of the release day, the AWS SDK's
`@smithy/core` would be 3.36.2, which npm marks deprecated for a bug in its logger's redaction of secrets (AWS SDK
issue 8393; 3.34.1 to 3.38.0 are all marked). Resolved as of 2026-11-01 it is 3.38.1 (2026-10-30), the first without
the mark and still older than 14 days. Pi's own packages stay at 1.4.0 (exact pins).

Verified here: the dates, the commit and the integrity (npm's registry, GitHub's API, `git ls-remote`), that
`badlogic/pi-mono` is the same repository (GitHub's API: the same id), the lock (`npm install --package-lock-only
--ignore-scripts --before=2026-11-01T00:00:00Z`), the install (`npm ci --ignore-scripts` from the lock; `pi
--version`: 1.4.0), the package's source (the unbundled `dist/` beside the bundle: a grep of every host, variable,
subprocess and switch named below; its `docs/`), and runs of the installed package against a local scripted
OpenAI-compatible model on the loopback, every TCP connection of the process logged and every one off the loopback
refused (an `--import` preload in Node): headless (`pi -p`, with a tool call), interactive (the terminal UI in a tmux
server of its own, a message typed, 40 s), with upstream's defaults and with sandbox-ai's settings (below); and
sandbox-ai's extension (below) with an MCP server, the `--llm` provider and the loop guard. Not verified here: the
image build (no Podman in this sandbox: the image builds in CI or on the host) and a start in a real sandbox; hence it was not live-tested when this review was made (since then CI's job `agents-live (pi)` and the live check's `harnesses-live` run it in a real sandbox).

## Hosts contacted by default (measured)

| Run | Upstream's defaults | sandbox-ai's settings |
|---|---|---|
| headless (`pi -p --model fake/m1 "..."`, a `bash` tool call) | the model | the model |
| interactive (`pi`, one message), 40 s | `pi.dev` (two connections: the version check `GET /api/latest-version` at every start, and the install report `GET /api/report-install?version=1.4.0` at the first start of a new HOME and after every update; with a provider key the model catalog `/api/models/providers/ID` too, at most daily), `github.com` (three connections: Pi downloads `fd` and `ripgrep` from their GitHub releases when none is installed), the model | the model; `fd` and `ripgrep` come with the image, and Pi does not download them anyway (`Offline mode enabled, skipping download`) |
| `pi update` | Pi itself: `npm install -g` (or its managed install from `pi.dev/api/installer/releases`), packages from npm and git | refused: Pi's install is read-only, a new Pi is a new review |
| `pi install`, `pi update --extensions`, `-e npm:...`/`git:...`, missing packages declared in settings | `registry.npmjs.org`, GitHub (git clones), `npm install` of the package with its scripts | refused unless `--pi-enable packages`; declared but missing packages are never installed at start (`PI_OFFLINE`) |
| `/share` | Radius (Earendil's service) when signed in, else a secret GitHub gist through `gh gist create` (`api.github.com`) | the proxy refuses creating gists in every Pi run unless `--pi-enable share`; Radius is closed |
| `/bug` | `POST radius.pi.dev/v1/bug-reports` (a report with the environment, the provider configuration without keys, error diagnostics and optionally the transcript) | refused: `PI_OFFLINE` stops the upload (Pi offers the zip instead), and the proxy refuses the path in every Pi run |
| `/login PROVIDER` | the provider's sign-in (OAuth: Anthropic, OpenAI ChatGPT, GitHub Copilot, Google, ...; a callback on the loopback or a pasted code) | inside the sandbox: the provider's hosts as for every agent (`sandbox-ai login pi` opens the frontier profile's) |
| a remote MCP server's OAuth (`/mcp login`) | the server's authorization server; with `clientRegistration: cimd` Pi's client document at `pi.dev/oauth/...` | the server's hosts as you open them; `pi.dev` closed |

Pi itself never calls a telemetry, analytics or crash-reporting service other than the install report: the bundle
and the unbundled source name no PostHog, Sentry, Segment, Mixpanel, Amplitude, Statsig or Datadog client, and
`@earendil-works/pi-telemetry` (a dependency of `pi-ai`) is an in-process tracing contract with no exporter and no
network code (its README and source).

## How sandbox-ai configures Pi

Pi reads its settings from the user's files only: `~/.pi/agent/settings.json`, `models.json`, `mcp.json`,
`auth.json` (`PI_CODING_AGENT_DIR` moves the folder) and, after project trust, the project's `.pi/`. There is no
system layer that wins over them, so what must hold is either a variable of Pi's process, which the agent cannot
change, or code of sandbox-ai's that Pi loads read-only (sandbox/pi.py: `BASE_ENV`, `TELEMETRY_ENV`, `environment`,
`EXTENSION`):

- `PI_OFFLINE=1`: "disable automatic network activity" (`main.js`, `interactive-mode.js`, `package-manager.js`,
  `tools-manager.js`, `bug-report.js`): no model catalog refresh from pi.dev, no version check, no install report, no
  check for package updates, no install of missing packages at start, no download of `fd` and `ripgrep`, no `/bug`
  upload. Measured: nothing but the model in every run. The model itself, MCP servers, `/login` and explicit
  `pi install` are not affected.
- `PI_SKIP_VERSION_CHECK=1`: the version check, also where a feature drops `PI_OFFLINE`.
- The telemetry opt-out (below), never dropped.
- sandbox-ai's extension `/etc/pi/sandbox-ai.js` (read-only, from the session directory) with what changes per start
  in `/etc/pi/sandbox-ai.json`: the image's `pi` command (`/usr/local/bin/pi`, a shell script in front of Pi's
  bundle) loads it with `-e` whenever it is there, so every `pi` of the sandbox - the agent, a helper's, one the agent
  starts from its shell - gets it; Pi's own subcommands (`install`, `update`, `mcp`, `config`, `auth`, ...) run
  without it. Extensions are Pi's own API (`docs/extensions.md`); ours only calls it:
  - `pi.registerMcpServer` for each `--mcp` server (the launcher's stdio bridge, `exposure: "direct"`: declared to
    the model like a built-in tool, so even a small local model sees it; Pi's default `codemode` exposure would hide
    it behind its JavaScript tool). Registered servers are not saved; a server of the same name in the user's
    `mcp.json` wins (Pi's rule).
  - `pi.registerProvider("sandbox-ai", ...)` for `--llm` (below).
  - the loop guard: its instructions as a section of the system prompt (`before_agent_start`) and its turn limit
    (`turn_end`: at the limit the run is stopped with `ctx.abort()` and the reason is shown).
  Measured: the bridged server's tools reach the model as `mcp__NAME__TOOL` and a call returns its result; the
  provider's requests carry the placeholder key; the guard's section is in the system prompt; the limit stops a run.
- With `--llm` at a model server of your own, Pi's two waits in `~/.pi/agent/settings.json` of the sandbox's HOME:
  `httpIdleTimeoutMs` (`core/http-dispatcher.js`: undici's `headersTimeout` and `bodyTimeout`, 300000 by default) and
  `retry.provider.timeoutMs` (`core/sdk.js`: the provider request's; `httpIdleTimeoutMs` where it is not set), both
  the start's wait (`stream_idle_timeout`, an hour by default; `llm.pi_wait_settings`). They are settings of the
  user's - Pi has no variable or option for them - so the launcher merges them into that file at each such start and
  keeps everything else in it. Why: a large local model is silent for minutes while it reads a long prompt, and Pi
  ended each request after 300 s ("terminated" when the headers had come, "Request timed out." when not), three
  retries, then failed (the local-model research, with a server silent for 420 s). Measured with Pi 1.4.0 against a
  silent scripted server: with a wait of 10 s it gives up on 25 s of silence in both forms, with 60 s it answers.
- Pi has **no approvals**: it runs every tool call without asking (upstream's design, `docs/security.md`), so `-D`
  changes nothing for Pi. Its project trust (a question before a folder's `.pi/` settings, extensions, skills and MCP
  servers load) stays upstream's: asked in the terminal, never in print mode (`defaultProjectTrust: "ask"`).
- `--model MODEL` is Pi's `--model` (`provider/id`, fuzzy names, an optional `:thinking`); `--effort` is Pi's
  `--thinking` (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`).

### Own sandboxing

Pi has none (`docs/security.md`: "lack of a built-in sandbox"; `docs/containerization.md` recommends running all of
Pi in a container, which is what sandbox-ai does). It never starts Docker or a VM itself; the Gondolin micro-VM
extension and Docker Sandboxes are separate products it documents, not in this install. Its `codemode` tool runs model
scripts in QuickJS (WebAssembly) workers inside Pi's process: a language boundary for its own tool calls, not an OS
one; the scripts reach only Pi's tools. Commands run with Pi's own permissions, inside sandbox-ai's sandbox (shell
`bash -c`, `PI_*` session variables in the environment).

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the firewall and the connection proxy refuse it even
if Pi tries. A switch is a feature of `--pi-enable FEATURE[,FEATURE...]` (`-px`; per start, in presets and project
files, and in the app's Pi section), the form of Hermes' `--hermes-enable`: each opens only its own hosts and shows
in the dry run's `"pi"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Install report and provider attribution (`pi.dev/api/report-install`; `X-Title`/`HTTP-Referer`-style attribution headers to some providers) | **on** (`enableInstallTelemetry: true`) | **always off, not switchable** (see Telemetry) | none |
| Bug reports (`/bug`: `radius.pi.dev/v1/bug-reports`) | on demand | **refused, not switchable** (see Telemetry); the zip export to a file stays | none |
| Model catalogs (`pi.dev/api/models/providers/ID`, at start and in `/model`, at most daily) | on | off (`PI_OFFLINE`): the catalog that came with the release | `model-catalog` (pi.dev): `PI_OFFLINE` is not set (the version check and the install report stay off by their own variables) |
| Version check (`pi.dev/api/latest-version`, at every interactive start; a notice) | on | off (`PI_OFFLINE`, `PI_SKIP_VERSION_CHECK`) | `updates` (pi.dev): the notice only. `pi update` (Pi itself) is refused either way: the install is read-only, and Pi's own advice is to pipe its installer into a shell |
| Pi packages (`pi install`, `pi update --extensions`, `pi -e npm:/git:`, packages declared in settings: extensions, skills, prompts and themes from npm and git, installed with `npm install`, scripts included) | on demand; declared packages installed at start | closed; `pi install` and `pi update` with a package refused; never installed at start | `packages` (registry.npmjs.org, github.com, codeload.github.com): installs go to `~/.pi/agent` of the sandbox's HOME and run there; packages already installed load without it |
| Session sharing (`/share`: a Radius artifact or a secret GitHub gist through `gh`) | on demand | the proxy refuses `POST api.github.com/gists` in Pi's runs; Radius closed | `share` (api.github.com): the gist (it needs `gh` signed in in the sandbox) |
| Radius (Earendil's AI gateway, `/login radius`, `RADIUS_API_KEY`, its MCP server) | off until signed in | closed | `--api-key radius.pi.dev=RADIUS_API_KEY` (a provider like any other); bug reports there stay refused |
| `fd`/`ripgrep` download from GitHub (`@` file completion, `grep`/`find` tools) | when absent | never: both in the image (`fd-find`, `ripgrep`), and `PI_OFFLINE` | - |
| llama.cpp extension (`/llama`: a llama.cpp server's models, downloads from Hugging Face) | on demand | the server you give it (`--llm` reaches yours); Hugging Face closed | `--allow-host` |
| MCP (stdio and streamable HTTP; `~/.pi/agent/mcp.json`, the project's `.pi/mcp.json` after trust, OAuth sign-in) | the user's | the user's, plus `--mcp` servers through sandbox-ai's extension | - |
| Extensions, skills, prompt templates, themes (`~/.pi/agent/...`, `~/.agents/skills`, the project's after trust) | the user's | the user's, in the sandbox's HOME: they run as the agent, inside the sandbox (`sandbox-ai resume --review-home` shows changes to them) | - |
| Context files (`AGENTS.md`, `CLAUDE.md` in the agent folder, the project and its parents) | read at every start, without trust | the same; `~/.pi/agent/AGENTS.md` is sandbox-ai's project memory for Pi | - |
| Its own sandbox / Docker | none | none (this sandbox is the boundary) | - |

### Telemetry: always off, never switchable

The user's rule: no telemetry, analytics, crash reporting or usage pings, and no switch that turns them on - not a
feature of `--pi-enable`, not `--telemetry on`, not a preset or project file, not the agent's own arguments.

Also from inside the sandbox (sandbox/telemetry_guard.py, since 0.1.97): Pi's bundle (`dist/bundle/cli.js`, an ES
module) imports sandbox-ai's telemetry guard first, which refuses `PI_TELEMETRY` set to anything but `0`, `PI_OFFLINE`
set to anything Pi does not read as on - `1`, `true` or `yes` in any case, as Pi reads it (unset stays allowed:
`--pi-enable` leaves it out) - and an OpenTelemetry/OTLP variable of its own with
one message (exit status 2), and pins `PI_TELEMETRY=0` and the common opt-outs.

What the review found: Pi's only telemetry is the install report (`reportInstallTelemetry` in
`interactive-mode.js`: `GET https://pi.dev/api/report-install?version=VERSION` with `User-Agent: pi/VERSION (OS;
node/VERSION; ARCH)`, at the first interactive start of a HOME and after every update) and the attribution of
requests to Pi as a CLI agent for some providers (`core/client-identity.js`: `PI_ATTRIBUTION`, only while install
telemetry is on). Both follow `enableInstallTelemetry` (default `true`) unless `PI_TELEMETRY` is set, which wins
(`core/telemetry.js`: `isInstallTelemetryEnabled`); `PI_OFFLINE` stops the report as well. `enableAnalytics`
(default `false`) is read only by an experimental first-run setup that sends nothing in 1.4.0. `/bug` uploads a
report to the developers (`core/bug-report-upload.js`) - crash reporting, if one the user starts.

What sandbox-ai does in every Pi run, whatever else is enabled (sandbox/pi.py: `TELEMETRY_ENV`, `TELEMETRY_PATHS`,
`telemetry_rules`, `check_telemetry`):

- the variables are set, and no feature drops them: `PI_TELEMETRY=0`; `PI_OFFLINE=1` unless `model-catalog` or
  `updates` (which still leave `PI_TELEMETRY=0`);
- the connection proxy refuses `pi.dev/api/report-install/**` and `radius.pi.dev/v1/bug-reports/**` in every Pi run
  (the ruleset `telemetry`), also where a feature or `--api-key` opens pi.dev or radius.pi.dev;
- a start that would hide pi.dev or radius.pi.dev from the proxy (`--no-inspect`) or go out without the proxy
  (`--egress-all-allow` with `--no-proxy`) is refused;
- the image sets the same variables (`ENV`), so a `pi` started with `--binary` or from the agent's shell has them too;
- the dry run's `"pi"` shows `"telemetry": {"switchable": false, ...}`.

Measured with these settings: no connection to pi.dev, radius.pi.dev or GitHub in any run. Tests: tests/test_pi.py and
the feature matrix's `pi-telemetry-is-never-switchable` (every feature with `--telemetry on`: the plan opens no
telemetry path, the variables and proxy rules hold, the refusals).

### What runs when Pi starts

The image's `pi` command (sandbox-ai's extension when it is there), then Pi: its settings (`~/.pi/agent/settings.json`;
the project's after trust), `PI_OFFLINE` (no catalog refresh, version check or report), the credentials
(`auth.json`, the providers' variables), the models (`models.json`, the extensions' providers), the extensions (the
four built-in ones - MCP, llama.cpp, codemode, tool search - the user's, `-e` ones; the project's after trust),
skills, prompts and themes, context files (`AGENTS.md`/`CLAUDE.md`: the agent folder's, the project's and its
parents'), the session (`~/.pi/agent/sessions/<cwd>/`: a new one; `--continue` the project's last), the MCP servers
(connected in the background), the `fd`/`ripgrep` lookup (the image's). The first interactive start of a folder with
`.pi/` resources asks for project trust.

### Providers

- **API keys**: `--api-key api.anthropic.com=ANTHROPIC_API_KEY`, `api.openai.com=OPENAI_API_KEY`,
  `openrouter.ai=OPENROUTER_API_KEY` and the others Pi reads from variables (`docs/providers.md`: DeepSeek, Gemini,
  Mistral, Groq, xAI, Fireworks, Together, ...): Pi sees a placeholder, the proxy adds the key on the wire; `--model`
  names the model (`anthropic/claude-sonnet-4-5`, or a fuzzy name).
- **`--llm NAME[:MODEL]`** (LM Studio, Ollama, vLLM, llama.cpp, OpenAI-compatible services, OpenRouter): sandbox-ai's
  extension registers a provider `sandbox-ai` (`api: "openai-completions"`, the server's `/v1` URL, the placeholder
  as key, the model with its real context window - the start's `num_ctx`, else what the server reports; Pi's default
  of 128,000 tokens when neither says) and the launcher starts Pi with `--model sandbox-ai/MODEL`. The loop guard:
  its instructions in the system prompt, its turn limit enforced by the extension, the proxy's sampling and notes as
  for every agent. Measured against the scripted model: chat completions with tools, the placeholder key.
- **Login**: Pi signs in to subscriptions and providers with `/login` (OAuth or a key), kept in
  `~/.pi/agent/auth.json`: one entry per provider, `{"type": "oauth", "access", "refresh", "expires"}` or
  `{"type": "api_key", "key"}` - the form of OpenCode's `auth.json`. sandbox-ai treats it like OpenCode's:
  `sandbox-ai login pi` starts Pi in a login sandbox (the frontier profile's hosts; type `/login`, then `/quit`), keeps
  the file in sandbox-ai's own store, every Pi sandbox gets it from there (else from the host's `~/.pi/agent/auth.json`
  with `--copy-auth`), and tokens a sandbox refreshes go back only for the same provider entries. Subscription tokens
  (Claude Pro/Max, ChatGPT) are real tokens inside the sandbox, as with OpenCode; prefer `--api-key` where a key does.

### Memory and sessions

Pi keeps no memory of its own beyond its sessions (`~/.pi/agent/sessions/`, JSONL trees, kept with the start:
`sandbox-ai resume NAME` goes on with `--continue`) and the context files it reads at every start. sandbox-ai's
project memory for Pi is `~/.pi/agent/AGENTS.md` (Pi reads it as the user's instructions in every folder), kept per
project like the other agents' memory files; the sandbox-ai skill tells Pi to keep it.

### Not (yet) for Pi

- `--with` (Pi is not in the team image): use `--helper` in either direction (`sandbox-ai pi --helper claude`,
  `sandbox-ai claude --helper pi`, which runs `pi -p -- TASK`).
- Cairn's Pi worker: Cairn runs on the team image, which has no Pi; see the 0.1.96 release notes.
- The activity feed of its turns, `mcp attach --live`, Pi's RPC mode as a bridge.

## Rules for its own tools and kit hooks (2026-11-16)

Pi 1.4.0's extension API (`docs/extensions.md`): `tool_call` handlers run in extension load order on the call's input
object, can block it (`{block, reason}`), and a handler that throws blocks it too (`agent-session.ts`: "Extension
failed, blocking execution"); Pi runs the call with that same object (`agent-loop.ts`: prepareToolCall). Extensions
given with `-e` load first and also with `--no-extensions` (`resource-loader.ts`).

- sandbox-ai's extension (`/etc/pi/sandbox-ai.js`, read-only, loaded with `-e` by the image's `pi`) asks the
  supervisor in `tool_call` (blocked with the `sandbox-ai:` reason, the human asked where a rule says ask) and
  records `tool_result`. The checked input is then frozen: an extension loaded after it that tries to change the call
  throws, which Pi turns into a block.
- Approved kit hooks are Pi extension modules (`.js`, `.mjs`) that sandbox-ai's extension imports after its check; a
  last handler asks again for a call whose input one of them changed, and then freezes it.
- Tools mapped: `bash` Bash, `read`/`write`/`edit` Read/Write/Edit (`path` becomes `file_path`), `find` Glob, `grep`
  Grep, `ls` LS; MCP tools keep `mcp__SERVER__TOOL`.
- Not covered: the real Pi started without the image's command (`node .../cli.js`) has no sandbox-ai extension.

## Go / No-Go

**Go** (choosing it starts it like any other agent).

- With `PI_OFFLINE` and `PI_TELEMETRY=0`, Pi contacts nothing but its model: measured, headless and interactive. Its
  only telemetry has two off switches read before anything is sent, and the proxy refuses its paths even where pi.dev
  is opened.
- The package and every dependency are pinned with an integrity; nothing runs at install; no self-update in the
  sandbox (read-only install, `pi update` refused); packages from npm and git only with `--pi-enable packages`.
- No sandbox of its own to turn off, no Docker; MCP, skills, extensions and local models through Pi's own API, laid in
  read-only by sandbox-ai.
- Against: Pi asks before nothing (by design; the sandbox is the boundary, and its extensions run as the agent); its
  release cadence is fast (the 14-day rule pins a release three behind); `/share` and Radius are on-demand uploads we
  refuse or leave closed rather than switch off in Pi; not live-tested in a sandbox when reviewed; it has been since (see above). The
  review is repeated for every release taken in.
