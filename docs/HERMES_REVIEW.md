# Hermes Agent: review for sandbox-ai

Hermes Agent (NousResearch/hermes-agent, MIT, Python) as sandbox-ai's fourth agent, `sandbox-ai hermes`. This
review covers the release built into `localhost/hermes-sandbox` (sandbox/Containerfile.hermes) with a focus on the
network and on security: every host Hermes contacts by default, what runs when it starts, its terminal backends, its
messaging gateway, the Kanban dispatcher, the skills it writes itself, and the switch for each. Reviewed on
2026-11-13.

## The release

| | |
|---|---|
| Tag | `v0.22.5`, published 2026-10-23 05:28 UTC (GitHub release API), 21 days before the review |
| Commit | `bf0203a794a07aac3ff764972ab9087baa4e4f4e` (`git ls-remote`: `refs/tags/v0.22.5^{}`; tag object `56b6a31fededdf7364e1a0a79548d144e7fd81a3`) |
| Source archive | `codeload.github.com/.../tar.gz/refs/tags/v0.22.5`, SHA-256 `a8f67e5ec14fea681acef6cc7ff1553f002bb6101fd2df22bfdd4b7eecc54eb2` (the image clones the tag and checks the commit instead: GitHub's archives are not guaranteed to stay byte-identical) |
| Why this one | the newest release at least 14 days old (sandbox-ai's rule for third-party releases). Newer: v0.22.6 (2026-11-02), v0.22.7 (2026-11-03), v0.23.0 (2026-11-05), v0.23.1 (2026-11-10) - all younger than 14 days. v0.22.2 (2026-10-15), the candidate named before the review, is older than v0.22.5 and superseded by it. |
| Python | 3.14 (upstream supports only 3.14; its dependencies are pinned for `python_version >= '3.14'`), from uv's own CPython builds: uv 0.13.5 (released 2026-10-22) is pinned and brings CPython 3.14.10 with its checksums |
| Dependencies | upstream's `uv.lock` decides every version and hash; `scripts/hermes_lock.py` exports it for the core and the extras `mcp`, `anthropic`, `telegram`, `discord`, `slack`, keeps only what applies to Linux (x86_64, aarch64) on CPython 3.14 and writes `sandbox/Containerfile.hermes.lock`: 95 packages, every one hash-pinned, installed with `uv pip install --require-hashes --no-deps` |

Verified here: the tags and their dates (GitHub's API and `git ls-remote`, both reachable from this sandbox), the
source at the tag, the lock (exported from upstream's `uv.lock`, installed with `--require-hashes` into a CPython 3.14
venv from PyPI, which is reachable here too), Hermes started from that install with sandbox-ai's managed configuration
(`hermes config get`, `hermes config set` refused for managed keys, a one-shot run against a local fake model).
Not verified here: the image build itself (no Podman in this sandbox: the image builds in CI or on the host) and a
start in a real sandbox; hence the release ships Hermes as not live-tested yet (the start says so; no opt-in).

## How sandbox-ai configures Hermes

Hermes reads an administrator's configuration from `/etc/hermes` (its *managed scope*,
`hermes_cli/managed_scope.py`): `config.yaml` is laid over the user's `~/.hermes/config.yaml` leaf by leaf, and every
key it sets is refused by `hermes config set` ("managed by your administrator ... cannot be changed"). sandbox-ai
writes it for each start into the session directory and mounts it read-only at `/etc/hermes`, as it does with
Claude Code's and Codex's managed settings (sandbox/hermes.py: `BASE_CONFIG`, `TELEMETRY_CONFIG`, `FEATURES`). The
user's own `~/.hermes/config.yaml` stays theirs for everything else (personality, display, memory sizes ...).
`TERMINAL_ENV=local` and `HERMES_DISABLE_LAZY_INSTALLS=1` are set in the environment as well, and once more in the
managed `.env`.

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the connection is refused by the firewall and the
connection proxy even if Hermes tries; the managed configuration keeps Hermes from trying. A switch in `code` is a
feature of `--hermes-enable FEATURE[,FEATURE...]` (`-he`; per start, in a preset's or a project file's options, and
in the app's Hermes section of the start): each opens only its own hosts and shows in the dry run's `"hermes"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Terminal backend (`local`, `docker`, `ssh`, `modal`, `daytona`, `vercel_sandbox`, Singularity as a plugin) | `local`; another with `terminal.backend` or `TERMINAL_ENV` | `local` only: pinned in the managed configuration and in `TERMINAL_ENV`; the SDKs of Modal, Daytona and Vercel are not installed, the image has no Docker CLI and the sandbox no Docker socket, SSH and the cloud hosts are closed | none: every other backend runs the agent's commands outside the sandbox (another machine, a container of the host's Docker), where none of sandbox-ai's limits, rules or logs hold |
| Update checks: the passive version check, `hermes update`, plugin update checks (at gateway start, every 24 h) | on (github.com) | off (`updates.check: false`, `plugins.auto_update_check_hours: 0`); Hermes cannot update itself at all: its install is on the read-only root file system, without `.git` | `updates` (github.com, api.github.com: the checks only). A new Hermes is a new review, then `sandbox-ai build hermes` |
| Shared metrics (`telemetry.nousresearch.com`) | off (opt-in with recorded consent) | **always off, not switchable** (see Telemetry below) | none |
| Monitoring export (OTLP) | off, no endpoint ships | **always off, not switchable** | none |
| The computer-use driver's PostHog events (cua-driver, macOS) | off unless `computer_use.cua_telemetry` | **always off** (pinned, `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`; the driver is not in the image) | none |
| Nous free tier: an anonymous Nous account on first use (`welcome-api.nousresearch.com`, connectors) | on (`nous.guest: true`) | off (`nous.guest: false`) | `free-tier` |
| Nous Portal login and inference (`portal.nousresearch.com`, `inference-api.nousresearch.com`) | used when logged in | open by default as Hermes' provider (like `api.anthropic.com` for Claude Code); the login: `sandbox-ai login hermes` (device code, no callback) | on; `--no-provider-defaults` closes them |
| Nous Tool Gateway and connectors (web search, images, speech through `*-gateway.nousresearch.com`; a Modal terminal) | on for a paid Portal account | closed | `tool-gateway` (its gateways, not Modal's) |
| Keyless web search: `web_search`/`web_extract` without a key send the queries to Exa's, Parallel's and Keenable's free MCP endpoints | on (`web.keyless_fallback`) | off | `web-search` (mcp.exa.ai, search.parallel.ai, api.keenable.ai) |
| Web search with your own key (Firecrawl, Tavily, Exa, Brave, Perplexity, SearXNG ...) | with a key | with a key and its host | `--api-key HOST=NAME` (the key stays with the proxy) |
| Model catalog (`hermes-agent.nousresearch.com/docs/api/model-catalog.json`, refreshed every 20 min) | on | off (`model_catalog.enabled: false`) | `model-catalog` |
| models.dev registry (context windows, prices) | fetched for some providers (seen for OpenRouter) | closed: Hermes falls back to its built-in data and the server's own answers | `model-catalog` |
| Skills hub (`hermes skills search/install`: GitHub, skills.sh, ClawHub; installs scanned by Hermes) | on demand | closed | `skills-hub` |
| Installs at run time (uv/pip from PyPI for speech, documents ...; npm language servers) | on | off (`security.allow_lazy_installs: false`, `HERMES_DISABLE_LAZY_INSTALLS=1`, `lsp.install_strategy: manual`) | `lazy-installs` (pypi.org, files.pythonhosted.org, registry.npmjs.org) |
| Borrowing the Claude Code and Codex logins (`auth.adopt_external_logins`) | on | off, pinned: their refresh tokens rotate and would be used up for the other agents | none: give Hermes its own (Nous login, `--api-key`, `--llm`) |
| Messaging gateway: Telegram, Discord, Slack (and 20 more platforms) | off until a platform's token is set | off: no token, hosts closed; only Telegram, Discord and Slack are in the image | `gateway:telegram`, `gateway:discord`, `gateway:slack` (`gateway:all`): the bot token from the secret `hermes_telegram`, `hermes_discord`, `hermes_slack` (+ `hermes_slack_app`), only that platform's API hosts. Who may talk to the bot: Hermes' own pairing (`hermes pairing approve PLATFORM CODE`) or `*_ALLOWED_USERS` |
| The gateway process (cron, the Kanban dispatcher, platforms) | started by Hermes' CLI itself in the background (`gateway run --idle-exit`), ends when idle | the same, inside the sandbox, on its loopback | - |
| Kanban dispatcher (in the gateway; spawns workers as local `hermes` processes) | on (`kanban.dispatch_in_gateway: true`) | off | `kanban` |
| Kanban dashboard (`hermes dashboard`, a web server) | on demand | not offered: a sandbox takes no connection from outside, and the image has no built frontend | none: `hermes kanban watch` in the tmux window shell |
| Browser tools (local Chromium over CDP; Browserbase, Browser Use, Firecrawl as cloud browsers) | local if a Chromium is found, else a cloud browser with its key | local once a Chromium is installed (`uvx playwright install chromium`: `--allow-pypi` and Playwright's CDN), each site needs `--allow-host`; cloud browsers closed | `--allow-host`, `--api-key api.browserbase.com=BROWSERBASE_API_KEY` |
| MCP sampling (a server asks the agent's model for completions) | on per server | off for every `--mcp` server: it would spend the agent's model on a server's behalf | none |
| Tool search (MCP tools and some cold built-ins behind `tool_search`/`tool_describe`/`tool_call`) | on (`tools.tool_search.enabled: auto`) | off when the start has `--mcp` servers: their tools are offered to the model directly (Hermes has no per-server exception; a model that does not search first never found them); without `--mcp` Hermes' default | none |
| Skills Hermes writes itself (`skill_manage` into `~/.hermes/skills`; the curator archives stale ones; `!cmd` snippets) | on; snippets off | on, kept with the start's HOME (`sandbox-ai resume`); snippets pinned off; the kits' skills read-only in `~/.agents/skills` | - |
| Hermes' own egress proxy (for its Docker backend) | off | off, pinned | none |
| API server and webhooks (gateway ports 8642, 8644) | off unless configured | off; unreachable from outside the sandbox anyway | - |
| Shell hooks (`hooks:` in config.yaml) | the user's | the user's; while rules for Hermes' own tools or approved kit hooks hold, the managed configuration's (and only those): see "Rules for its own tools and kit hooks" | - |
| Dangerous-command approvals | on | on; `--dangerously-skip-permissions` sets `HERMES_YOLO_MODE=1` | `--dangerously-skip-permissions` |
| Secret managers (Bitwarden, 1Password, a command) | off | off (their CLIs are not in the image) | none |
| Desktop app, voice and local speech models, wake word, computer use | optional | not in the image | none |

### Telemetry: always off, never switchable

The user's rule for Hermes: no telemetry, analytics, crash reporting, usage pings or "anonymous statistics", and no
switch that turns them on - not a feature of `--hermes-enable`, not `--telemetry` (whose former `on` is gone since 0.1.95), not a preset or project file.

What the review found: Hermes v0.22.5 has one telemetry of its own, its *shared metrics* (aggregates collected
locally through NVIDIA's NeMo Relay bindings, `nemo-relay`, and sent to `https://telemetry.nousresearch.com/v1/telemetry`
only after recorded consent; off upstream, and its endpoint is deliberately not overridable by a variable), an
operator's OTLP export (no default destination) and, for computer use on macOS, the cua-driver's PostHog events
(Hermes turns them off unless `computer_use.cua_telemetry`). It carries no analytics or crash-reporting SDK
(PostHog, Sentry, Segment, Mixpanel, Amplitude) itself; none of the 95 locked packages is one; `opentelemetry-api` is
in the lock, no exporter is.

What sandbox-ai does in every Hermes run, whatever else is enabled (sandbox/hermes.py: `TELEMETRY_PINNED`,
`TELEMETRY_ENV`, `TELEMETRY_HOSTS`):

- the managed configuration pins `telemetry.shared_metrics.enabled` to false, both OTLP exports
  (`monitoring.gateway_health_export`, `monitoring.export.otlp`) off and `computer_use.cua_telemetry` false - laid
  over everything else, every feature included;
- Hermes' first-run question about shared metrics ("Help improve Hermes?") is never asked: Hermes reads its answer -
  and its collection and sending switches - only from the profile's own `~/.hermes/config.yaml`
  (`hermes_cli/observability/shared_metrics_consent.py`, `relay_shared_metrics.py`), never from the managed scope.
  So before every start (the agent, a resumed session, a helper, one-shot) the launcher's command first answers it
  "No thanks" in each profile of the sandbox's HOME with Hermes' own reader and writer
  (`telemetry.shared_metrics: {enabled: false, send: false, offer_version: 2}`; an opt-in of an earlier run is taken
  back) and then becomes Hermes (sandbox/hermes.py: `TELEMETRY_ANSWER`, `start_command`). `.send` is not pinned on
  purpose: Hermes drops every pinned leaf from that file when it saves it, and with both pinned a save (a model
  switch, an "always allow") erased the answer - the next start asked again. `hermes setup telemetry` in the agent's
  arguments is refused;
- not even from inside the sandbox: that file is the agent's to write (`hermes setup telemetry`, the full setup's
  tools menu, the TUI's `shared_metrics.set` and the dashboard's consent route, `hermes config edit`, any editor), and
  `HERMES_MANAGED_DIR` or `PYTEST_CURRENT_TEST` make `managed_scope.get_managed_dir` drop `/etc/hermes`. So a guard
  (sandbox/hermes_guard.py) and a `.pth` that loads it are mounted read-only into the venv's site-packages in every
  sandbox of Hermes' image (a `--binary` shell too) and run in every process of Hermes' Python: a Hermes command line
  (`python -m hermes_cli.main`, which the image's `hermes` runs) with `setup telemetry`, `config set|unset` of a
  `telemetry`, `monitoring` or `computer_use.cua_telemetry` key, an opt-out variable set to another value or one of
  those two variables is refused with "sandbox-ai: Hermes' telemetry is always off in sandbox-ai and cannot be turned
  on"; the variables are pinned again; `hermes_cli.config`'s raw and merged reads see every telemetry switch off,
  `consent_state` is always answered and off, `set_answer`/`save_consent` write "No thanks", the collection gate
  (`relay_shared_metrics.enabled`) and the sending policy (`resolve_send_config`) are off, the Shared Metrics setup
  section only says the message, and the managed scope is always `/etc/hermes`. Checked against Hermes v0.22.5
  itself (tests/test_hermes_telemetry_guard.py with `SANDBOX_AI_TEST_HERMES_PYTHON`). Left: a Python started without
  its site initialization (`python -S`) and a hand-built path to Hermes' packages skips the guard - nothing it counts
  leaves the sandbox (the proxy), and the next start's answer turns it off again;
- the opt-out variables are set: `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` (the one Hermes documents), `OTEL_SDK_DISABLED=true`,
  `DO_NOT_TRACK=1`, `HF_HUB_DISABLE_TELEMETRY=1` (plus the launcher's own list for the tools an agent runs);
- `telemetry.nousresearch.com`, `*.posthog.com` and `*.sentry.io` are never opened: no feature names them, and a start
  that would open one (`--allow-host`, a wildcard over it), hide it from the proxy (`--no-inspect`) or go out
  without the proxy (`--egress-all-allow` with `--no-proxy`) is refused;
- the connection proxy refuses every request to them in every Hermes run (the ruleset `telemetry`, rules
  `telemetry-hermes-*`), also behind `--egress-all-allow`;
- the dry run's `"hermes"` shows `"telemetry": {"switchable": false, "blocked_hosts": [...], ...}`.

Tests: tests/test_hermes.py (the pins at every level, no feature reaching a telemetry host) and the feature matrix's
`hermes-telemetry-is-never-switchable` (every feature with an old `--telemetry on`, which is ignored: the plan opens no telemetry host, the
configuration, variables and proxy rules hold, the refusals).

### What runs when Hermes starts

The CLI (`hermes`, prompt_toolkit) loads its configuration, syncs its bundled skills into `~/.hermes/skills`
(local, hash-gated), and starts its gateway in the background if none runs (`hermes gateway run --idle-exit`): the
chat runs through it. The gateway starts cron, the Kanban notifier (and the dispatcher when enabled), the enabled
platforms, and a plugin update check (off here). The passive update check needs a Git checkout (none in the image).
A one-shot run against a local model, with every other connection sent to a logging proxy, contacted no other host -
with upstream's defaults and with sandbox-ai's; with the OpenRouter provider Hermes also asked `openrouter.ai`
(model metadata, below `/api`, which the start opens) and `models.dev` (closed).

### Providers

- **Nous Portal**: `sandbox-ai login hermes` (the device login in a sandbox, kept in sandbox-ai's login store like
  the other agents' logins; `~/.hermes/auth.json`).
- **API keys**: `--api-key api.anthropic.com=ANTHROPIC_API_KEY`, `--api-key openrouter.ai=OPENROUTER_API_KEY`, ...
  Hermes picks the provider from the variable; it sees a placeholder, the proxy adds the key on the wire.
- **`--llm NAME[:MODEL]`** (local servers, OpenAI-compatible ones, OpenRouter): Hermes gets the server in its
  managed configuration: `model.provider` (`custom`, or `openrouter` at openrouter.ai), `model.base_url` with `/v1`,
  `model.default`, the context window when the server reports it, the placeholder as key. Hermes ignores an
  OpenRouter key without OpenRouter's prefix in some of its credential paths (`KNOWN_PROVIDER_KEY_PREFIXES`), so
  for OpenRouter the placeholder is `sk-or-v1-sandbox-ai-placeholder`, exactly what the proxy replaces. The
  relay's per-model parameters and loop guard apply on the wire as for every agent; the loop guard's layers also
  become Hermes' own switches: `tool_loop_guardrails` with a hard stop, `agent.max_turns`, compression, and the
  guard's instructions as a skill loaded in every session (`/etc/hermes/skills/sandbox-ai-loop-guard`).

### Not (yet) for Hermes

- `--with` (Hermes is not in the team image): use `--helper` in either direction (`sandbox-ai hermes --helper
  claude`, `sandbox-ai claude --helper hermes`, which runs `hermes -z`).
- Claude-style plugins.
- Teams (`sandbox-ai team start`), the activity feed of Hermes' turns, `mcp attach --live`.
- `hermes kanban` boards shared between sandboxes (the board lives in the start's HOME).

## Rules for its own tools and kit hooks (2026-11-16)

Hermes v0.22.5 has shell hooks (`agent/shell_hooks.py`): `hooks:` in its configuration, each `{command, matcher,
timeout, fail_closed}` under an event of `VALID_HOOKS`; exit status 2 blocks a `pre_tool_call` with standard error as
the reason; a hook needs consent (an allowlist in the HOME, `--accept-hooks`, `HERMES_ACCEPT_HOOKS` or
`hooks_auto_accept`). The managed configuration at `/etc/hermes` is laid over the user's leaf by leaf (a list is one
leaf: `managed_scope.apply_managed_overlay`).

- The rule hooks go in there as `pre_tool_call` (`fail_closed: true`) and `post_tool_call`, the approved kit hooks
  after them under Hermes' names (`kits.HERMES_EVENTS`), with `hooks_auto_accept: true`. Every other shell-hook event
  is an empty list there, so the user's own shell hooks do not run while these hold (as Claude Code's
  `allowManagedHooksOnly`) and nothing unreviewed is auto-accepted.
- sandbox-ai's guard (`hermes_guard.py`, in every process of Hermes' Python) keeps them while the marker
  `/etc/hermes/sandbox-ai-hooks.json` is there: `--safe-mode` (no shell hook registered) is refused,
  `HERMES_SAFE_MODE` dropped, and `hermes_cli.plugins._dispatch_pre_tool_call_hooks` is wrapped: Hermes runs every
  `pre_tool_call` hook on the same arguments and applies a plugin's or shell hook's `modify` after them
  (`model_tools._pre_dispatch_guards`), so arguments changed that way are asked of the rules again before the call.
- Its tools are mapped to Claude Code's names (`terminal` Bash, `read_file`/`write_file` Read/Write, `patch` Edit or,
  in its patch mode, `apply_patch`, `search_files` Grep, `web_extract` WebFetch, `delegate_task` Task); `path`
  becomes `file_path`.
- Not covered: tool-execution middleware of a plugin can still change arguments after the check; a Hermes run with
  `python -S` (no site packages, so no guard) has neither the telemetry guard nor this one.

## Go / No-Go

**Go, not live-tested yet** (choosing it starts it; the start says so in one line).

- Hermes contacts nothing by default beyond its model provider, once its free tier, keyless search, model catalog,
  update checks and run-time installs are off - and each of them has a clean configuration key, pinned in a managed
  scope the agent cannot change.
- The parts that would leave the sandbox (remote terminal backends, Docker) can be pinned off and are absent from the
  image; nothing in Hermes needs them for local work.
- Upstream pins its dependencies exactly and ships a hash-locked `uv.lock`, so the image can install exactly the
  reviewed set with `--require-hashes`, from a Git tag checked against its commit.
- Telemetry is opt-in upstream and always off here, without a switch (above); Hermes itself carries no analytics SDK (PostHog, Sentry, Segment). The one
  third-party tool with PostHog telemetry, the optional macOS computer-use driver cua-driver, is turned off by
  Hermes and is not in the image.
- Against: the code base is large and moves fast (231 commits between v0.22.4 and v0.22.5), the gateway, Kanban
  workers and curator run processes in the background, and Hermes is not live-tested in a sandbox yet (its tool
  rules neither). Hence experimental, and the review is to be repeated for every new release taken in.
