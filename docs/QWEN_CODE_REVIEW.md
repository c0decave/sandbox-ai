# Qwen Code: review for sandbox-ai

Qwen Code (QwenLM/qwen-code, Apache-2.0, Node.js/TypeScript, a fork of Google's Gemini CLI) as a sandbox-ai agent,
`sandbox-ai qwen`. This review covers the release built into `localhost/qwen-sandbox` (sandbox/Containerfile.qwen):
every host Qwen Code contacts by default, its telemetry, its updates, extensions and MCP, its own sandboxing modes,
and the switch for each. The user's reason to have it: local models (LM Studio, Ollama, vLLM, llama.cpp) through
`--llm`. Reviewed on 2026-11-13.

## The release

| | |
|---|---|
| Version | `0.26.4`: npm `@qwen-code/qwen-code@0.26.4`, published 2026-10-29 14:41 UTC (npm registry `time`); GitHub release `v0.26.4` 2026-10-29 14:29 UTC (release API), 15 days before the review |
| Commit | `715a550cfca0135e00d4015d1ca82a8167e81fff` (`git ls-remote`: `refs/tags/v0.26.4`, a lightweight tag; the npm package's `gitHead` is the same commit) |
| Provenance | the npm package carries an SLSA v1 provenance attestation: built by `QwenLM/qwen-code`'s `.github/workflows/release.yml` |
| Package | `qwen-code-0.26.4.tgz`, integrity `sha512-YeRiUaH3d+JxPiYUS4eGalsKRTuIEUo75JWPWdVBoptdbAH3FJdWbGtjs/syECEUTH5qk0nu8tRb1HMEKn0I2Q==` (checked here against the downloaded file), 42.8 MB; a bundle: no runtime dependencies, only optional native ones |
| Why this one | the newest release at least 14 days old (sandbox-ai's rule for third-party releases). Newer: 0.26.5 (2026-11-01) ... 0.27.3 (2026-11-13), all younger than 14 days; previews and nightlies are not releases |
| Node.js | `engines.node >=22`; the image's base is Node's LTS image like the other agents' |
| Dependencies | `sandbox/Containerfile.qwen.lock`: an npm lock made by `scripts/agent_npm_lock.py qwen` with `npm install --package-lock-only --before=2026-10-30` (every version older than 14 days), every package with its sha512 integrity; the image installs it with `npm ci --ignore-scripts` (no install script runs). Optional packages kept: the prebuilt PTY (`@lydell/node-pty-linux-*`), `sharp` (images), the clipboard; left out after the install: Bun (`@oven/bun-*`, only for the experimental OpenTUI renderer) and `@qwen-code/audio-capture` (voice) |

Verified here: the dates and the commit (npm registry, GitHub's API, `git ls-remote`), the package's integrity, the
lock (installed with `npm ci --ignore-scripts` from the npm registry, which this sandbox reaches), `qwen --version`
and `--help`, and one-shot and interactive runs from that install against a local fake model with every other
connection sent to a logging proxy that refuses it: with upstream's defaults, and with sandbox-ai's settings (below);
the shell tool ran a command in both. Not verified here: the image build (no Podman in this sandbox: the image builds
in CI or on the host) and a start in a real sandbox; hence the release ships Qwen Code as not live-tested yet
(the start says so in one line; no opt-in).

## Hosts contacted by default (measured)

| Run | Upstream's defaults | sandbox-ai's settings |
|---|---|---|
| one-shot (`qwen "say hi"`, OpenAI-compatible model on loopback) | `models.dev` (model catalog refresh), `gb4w8c3ygj-default-sea.rum.aliyuncs.com` twice (usage statistics), the model | the model only |
| interactive, 60-90 s idle | `models.dev`, the RUM endpoint, `registry.npmjs.org` (update check), the model on the first prompt | nothing |

## How sandbox-ai configures Qwen Code

Qwen Code (like Gemini CLI) reads *system settings* that override the user's and the workspace's:
`/etc/qwen-code/settings.json` on Linux (`QWEN_CODE_SYSTEM_SETTINGS_PATH`, `cli/src/config/storage-paths-lite.ts`;
merged last in `cli/src/config/settings.ts`). sandbox-ai writes that file for each start into the session directory
and mounts it read-only at `/etc/qwen-code`, like Claude Code's, Codex's and Hermes' managed settings
(sandbox/qwen.py: `BASE_SETTINGS`, `TELEMETRY_PINNED`, `FEATURES`). It carries `"$version": 4`, so Qwen Code never
tries to migrate (rewrite) it. The user's `~/.qwen/settings.json` stays theirs for everything else.

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the firewall and the connection proxy refuse it even
if Qwen Code tries; the settings keep Qwen Code from trying. A switch in `code` is a feature of
`--qwen-enable FEATURE[,FEATURE...]` (`-qe`; per start, in presets and project files, and in the app's Qwen Code
section), the same form as Hermes' `--hermes-enable`: each opens only its own hosts and shows in the dry run's
`"qwen"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Usage statistics to Alibaba Cloud RUM (`gb4w8c3ygj-default-sea.rum.aliyuncs.com`) | **on** for every auth method | **always off, not switchable** (see Telemetry) | none |
| OpenTelemetry export (`telemetry.*`: local file, OTLP endpoint, Google Cloud target) | off | **always off, not switchable** | none |
| Update check (`registry.npmjs.org` dist-tags) and auto-update (npm, the standalone installer from `qwen-code-assets.oss-cn-hangzhou.aliyuncs.com`) | on (`general.enableAutoUpdate`) | off (`general.enableAutoUpdate: false`); the install is read-only in the sandbox, so an update can never be applied | `updates` (registry.npmjs.org: the check and its notice). A new Qwen Code is a new review, then `sandbox-ai build qwen` |
| Model catalog refresh (`models.dev/api.json`, once a day) | on | off (`QWEN_CODE_MODELS_DEV_REFRESH=off`; the bundled snapshot still serves) | `model-catalog` (models.dev) |
| Extensions: `qwen extensions install` from Git/GitHub, npm, the Claude/Gemini marketplaces | on demand | closed (installed extensions still load from `~/.qwen/extensions`) | `extensions` (github.com, api.github.com, codeload.github.com, raw.githubusercontent.com, objects.githubusercontent.com, registry.npmjs.org) |
| Its own sandbox (`--sandbox`, `tools.sandbox`, `QWEN_SANDBOX`: Docker, Podman, macOS Seatbelt) | off unless asked | off and refused: `tools.sandbox: false`, `QWEN_SANDBOX=false`, `SANDBOX=sandbox-ai` (Qwen Code then knows it is inside one); `--sandbox`/`-s`/`--sandbox-image` in the agent's arguments are refused. The image has no Docker or Podman and the sandbox no socket | none: a container of the host's engine would run the agent outside sandbox-ai's limits, rules and logs |
| Execution sandbox for its shell tool (`tools.executionSandbox`: bwrap/Landlock, inside this sandbox) | off | the user's choice (it confines further, inside sandbox-ai's sandbox) | - |
| Qwen OAuth (`chat.qwen.ai` device login, inference at the token's resource URL) | on `/auth` (free tier discontinued 2026-04-15) | open as Qwen Code's provider with DashScope (`chat.qwen.ai`, `portal.qwen.ai`, `dashscope.aliyuncs.com`, `dashscope-intl.aliyuncs.com`); `sandbox-ai login qwen` (`/auth` in a login sandbox; the device URL through the login browser) | on; `--no-provider-defaults` closes them |
| API keys (OpenAI-compatible, Anthropic, Gemini, Coding Plan, OpenRouter, ...) | from variables / settings | `--api-key HOST=NAME`: the variable holds a placeholder, the proxy adds the key on the wire | `--api-key`, `--allow-host` |
| `--llm NAME[:MODEL]` (LM Studio, Ollama, vLLM, llama.cpp, OpenAI-compatible services, OpenRouter) | - | the system settings select `openai` auth with the server's `/v1` URL, its model and context window (`modelProviders.openai`), the placeholder as key; the relay's per-model parameters and loop guard on the wire, the guard's turn limit as `model.maxSessionTurns`, its instructions with `--append-system-prompt` | `--llm` |
| Web search (DashScope Responses API, `tools.webSearch`) | on for DashScope keys | follows the provider: it reaches only the provider's own host | - |
| Web fetch tool | on | on; every site needs `--allow-host` (or an approval) | `--allow-host` |
| MCP servers (`mcpServers`, `--mcp-config`; remote ones with OAuth) | the user's | `--mcp` servers through the launcher's stdio bridge (`--mcp-config`), each with `alwaysLoadTools`: their tools are declared to the model, not deferred behind Qwen Code's `tool_search`/`tool_call` (its default for every MCP tool); remote servers need their host | `--mcp`, `--allow-host` |
| IDE companion (VS Code, Zed, JetBrains over a local port) | on when started from an IDE | off (`ide.enabled: false`): no IDE in the sandbox | none |
| Channels (`qwen channel`: Telegram, Discord, DingTalk ...), `qwen serve` daemon, web shell, Chrome extension / browser use, computer use (cua-driver), voice | opt-in | not offered in this release: no token, no hosts; servers would listen on the sandbox's loopback only. The image has no browser extension, cua-driver or audio capture | none yet |
| Team memory sync (`memory.enableTeamMemorySync`: pull/commit/push of a memory repository) | off | off; would need the remote's host like any git push | - |
| Hooks, skills, sub-agents, scheduled tasks, worktrees | the user's | inside the sandbox; the kits' skills read-only in `~/.qwen/skills` | - |
| Approvals (`--approval-mode`, `--yolo`) | ask | ask; `--dangerously-skip-permissions` adds `--yolo` | `-D` |

### Telemetry: always off, never switchable

The user's rule: no telemetry, analytics, crash reporting or usage pings, and no switch that turns them on - not a
feature of `--qwen-enable`, not `--telemetry` (whose former `on` is gone since 0.1.95), not a preset or project file, not the agent's own arguments.

What the review found: Qwen Code 0.26.4 has two telemetries. Its *usage statistics* (`QwenLogger`,
`core/src/telemetry/qwen-logger/qwen-logger.ts`: commands, tool calls, errors, performance, flushed every minute to
the Alibaba Cloud RUM endpoint `gb4w8c3ygj-default-sea.rum.aliyuncs.com`) are **on by default** for every auth method;
the opt-out is `privacy.usageStatisticsEnabled: false` or `QWEN_USAGE_STATISTICS_ENABLED=false` (the variable wins
over the setting). Its *OpenTelemetry* export (`telemetry.enabled`, `QWEN_TELEMETRY_*`, `OTEL_EXPORTER_OTLP_*`; a local
file, an OTLP collector or Google Cloud) is off by default; its command-line flags (`--telemetry`,
`--telemetry-target`, `--telemetry-otlp-endpoint` ...) win over both the variables and the settings. Gemini CLI's
Clearcut logger (`play.googleapis.com/log`) is gone from this fork: the bundle names no Clearcut, Sentry, PostHog,
Segment, Mixpanel, Amplitude or Google Analytics host; the RUM endpoint is the only analytics host in it.

What sandbox-ai does in every Qwen Code run, whatever else is enabled (sandbox/qwen.py: `TELEMETRY_PINNED`,
`TELEMETRY_ENV`, `TELEMETRY_HOSTS`, `REFUSED_ARGS`):

- the system settings pin `privacy.usageStatisticsEnabled: false` and `telemetry: {enabled: false, logPrompts: false,
  includeSensitiveSpanAttributes: false}`, laid over everything else (every feature included);
- the variables are set: `QWEN_USAGE_STATISTICS_ENABLED=false`, `QWEN_TELEMETRY_ENABLED=false`,
  `GEMINI_TELEMETRY_ENABLED=false`, `OTEL_SDK_DISABLED=true`, `DO_NOT_TRACK=1`;
- the agent's arguments may not hold `--telemetry` or any `--telemetry-*` option (they would win over the settings);
- `*.rum.aliyuncs.com` and `play.googleapis.com` are never opened: no feature names them, and a start that would
  open one (`--allow-host`, a wildcard over it), hide it from the proxy (`--no-inspect`) or go out without the proxy
  (`--egress-all-allow` with `--no-proxy`) is refused;
- the connection proxy refuses every request to them in every Qwen Code run (the ruleset `telemetry`, rules
  `telemetry-qwen-*`), also behind `--egress-all-allow`;
- the dry run's `"qwen"` shows `"telemetry": {"switchable": false, "blocked_hosts": [...], ...}`.

Tests: tests/test_qwen.py and the feature matrix's `qwen-telemetry-is-never-switchable` (every feature with
an old `--telemetry on`, which is ignored: the plan opens no telemetry host, the settings, variables and proxy rules hold, the refusals).

### What runs when Qwen Code starts

The interactive CLI (Ink) loads the settings (system defaults, user, workspace, system), the context files
(`~/.qwen/QWEN.md`, the project's `QWEN.md`/`AGENTS.md`), skills, extensions, hooks and MCP servers; after the first
paint it runs its deferred tasks: the update check (off here), the IDE connection (off here), the model catalog
refresh (off here), and the RUM logger's first flush (off here). A pre-connect to the model's host warms the
connection. `qwen-socks-*` sockets in `/tmp` serve its own sub-processes.

### Rendering in tmux (the flicker)

What the review found in 0.26.4's bundle: the interactive UI renders with Qwen Code's Ink fork into the alternate
screen ("Virtualized History", `ui.useTerminalBuffer`, on by default in an interactive terminal), up to 60 frames a
second; every frame erases all rows of the screen and writes them again (`log-update`'s standard mode - the
incremental one is an Ink option that Qwen Code neither sets nor offers as a setting; its redraw optimizer only bounds
the erase). Ink wraps each frame in a synchronized update (`ESC[?2026h` ... `ESC[?2026l`); Qwen Code's own wrapper for
everything else it writes (`installSynchronizedOutput`) is off whenever `TMUX`, `SSH_TTY` or `SSH_CLIENT` is set,
unless `QWEN_CODE_SYNCHRONIZED_OUTPUT=1`. tmux 3.3a (the images' Debian 12) ignores mode 2026 from the application
and passes each frame on in pieces, one per read of the pane (4 KiB); it marks its own updates for the terminal only
if the terminal has `Sync`, which for anything but iTerm2 it does not. A terminal painting between two pieces shows
the lower rows erased: the flicker seen in the app's terminal tab and in an attached terminal (measured: 60 frames
came in 192 pieces, 129 of them ending inside a frame).

What sandbox-ai does (no setting that exists could make Qwen Code redraw less: `ui.accessibility.enableLoadingPhrases`
only stops the phrases changing, the spinner and timers still redraw the whole screen):

- `QWEN_CODE_SYNCHRONIZED_OUTPUT=1` (sandbox/qwen.py `BASE_ENV`): everything Qwen Code writes at once is one update,
  also in tmux - held whole by tmux 3.7 and later (a host's tmux with `--backend bwrap`), ignored by tmux 3.3;
- tmux runs with `escape-time 10` and `terminal-overrides ',*:Sync=...'` (tmux 3.4's form of mode 2026; runtime.py
  `TMUX_OPTIONS`), so its full redraws reach every terminal as one update;
- the terminal relay sends tmux's output in bursts - until it is quiet for 5 ms, at most 25 ms - each one
  synchronized update, and never ends a burst inside an escape sequence or a UTF-8 character (runtime.py
  `TerminalBursts`): every frame reaches the terminal whole (measured: 60 frames in 63 bursts, none torn);
- the app's terminal tab hands xterm.js (which holds painting during an update, up to 1 s) one write per frame: the
  bridge waits for 3 ms of quiet (at most 20 ms, an open update up to 100 ms) before it sends (pty_bridge `Frames`).

Tests: tests/test_terminal_frames.py (core) and projects/sandbox-ai-app/tests/test_terminal_frames.py, both on a
recording of tmux 3.3a passing on Qwen-like redraws.

### Providers

- **Qwen OAuth**: `sandbox-ai login qwen` starts Qwen Code in a login sandbox; `/auth` and Qwen OAuth there (the
  device URL opens through the login browser). The credential, `~/.qwen/oauth_creds.json`, is kept in sandbox-ai's
  login store like the other agents' logins.
- **API keys**: `--api-key dashscope.aliyuncs.com=DASHSCOPE_API_KEY`, `--api-key openrouter.ai=OPENROUTER_API_KEY`,
  `--api-key api.openai.com=OPENAI_API_KEY` ...; Qwen Code sees a placeholder, the proxy adds the key on the wire.
- **`--llm NAME[:MODEL]`**: the system settings select `security.auth.selectedType: openai`, `model.name` and a
  `modelProviders.openai` entry with the server's URL (`/v1`), its context window when the server reports it, and
  `OPENAI_API_KEY` (the placeholder) as `envKey`; OpenRouter goes the same way (`https://openrouter.ai/api/v1`).

### Not (yet) for Qwen Code

- `--with` (Qwen Code is not in the team image): use `--helper` in either direction (`sandbox-ai qwen --helper
  claude`, `sandbox-ai claude --helper qwen`, which runs `qwen --prompt=...`).
- Rules for the agent's own tools and kit hooks (refused, never silently unenforced - so not the mode paranoid);
  Qwen Code's own hooks stay the user's.
- `--effort` (Qwen Code sets reasoning per model in its `generationConfig`).
- Teams, the activity feed of its turns, `mcp attach --live`, the channels and `qwen serve`.

## Go / No-Go

**Go, not live-tested yet** (choosing it starts it; the start says so in one line).

- Qwen Code contacts nothing by default beyond its model once its usage statistics, model catalog refresh and update
  check are off - and each has a clean setting in a system scope that overrides the user's and that the agent cannot
  change (read-only), plus a variable.
- Its own container sandbox is opt-in and has a clean off switch; the image has no container engine.
- The npm package is a single bundle with an integrity hash and an SLSA provenance attestation; the few optional
  native packages are hash-locked too, and nothing runs at install.
- Local models are first-class: OpenAI-compatible auth with a base URL, a model and a context window per model.
- Against: usage statistics are **on** by default upstream (pinned off here three ways and blocked at the proxy); the
  code base is large and moves fast (a release every one to three days); many features (channels, daemon, browser
  and computer use, extensions) reach out or run servers - off or absent here, each to be reviewed before it is
  offered; not live-tested in a sandbox yet. Hence experimental, and the review is repeated for every release taken in.
