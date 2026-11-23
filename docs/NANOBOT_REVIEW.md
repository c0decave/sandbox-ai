# nanobot: review for sandbox-ai

nanobot (HKUDS/nanobot, HKU Data Intelligence Lab, MIT; PyPI `nanobot-ai`) as a sandbox-ai agent, `sandbox-ai
nanobot`. This review covers the release built into `localhost/nanobot-sandbox` (sandbox/Containerfile.nanobot):
every host nanobot contacts by default, what runs at its start, its updates and installs at run time, its own command
sandbox, its gateway, chat channels, scheduler, WebUI, skills and MCP, its telemetry, and the switch for each.
Reviewed on 2026-11-14.

## The release

| | |
|---|---|
| Version | `0.4.0`: tag `v0.4.0` (tag object `59d435ff79c2681023b2e85fc919b3cf1c367cca`), PyPI `nanobot-ai==0.4.0` uploaded 2026-10-28 11:06-11:08 UTC, 17 days before the review |
| Commit | `8f7a7d17590de7ab187650381894dba2b9e060ab` (`git ls-remote`: `refs/tags/v0.4.0^{}`) |
| Why this one | the newest release at least 14 days old (sandbox-ai's rule for third-party releases). Newer: 0.4.1 (2026-11-06), younger than 14 days |
| Files | `nanobot_ai-0.4.0-py3-none-manylinux_2_17_x86_64.whl` `sha256:6cd6ad40b9aaf3087f91ef8a0b5484bade1100595493755bef98d23045182a47` (55 MB; checked here against the downloaded file), `nanobot_ai-0.4.0-py3-none-manylinux_2_17_aarch64.whl` `sha256:4a4e6adb481718b06cb9e3acf4d0fc72a8ef00132a54ec457d04d265cfc7629d` (PyPI's digests). The wheels are per platform because each carries the native terminal client (below) |
| Size | about 144,000 lines of Python (`nanobot/`), the WebUI built into the wheel (`nanobot/web/dist`, 10 MB), the terminal client `nanobot/tui/bin/nanobot-tui-linux-x64` (a Bun-compiled TypeScript program, 129 MB) with its source (`nanobot-tui-source.tar.gz`), licences and a `MANIFEST.sha256`. Not "small" any more: the candidate list's "small Python core" was true of earlier releases |
| Dependencies | 41 direct (`anthropic`, `openai`, `mcp`, `httpx`, `aiohttp`, `ddgs`, `dulwich`, `sigstore`, `tiktoken`, `pydantic-settings`, `croniter`, `websockets`, document readers ...), 105 packages in all with the Telegram channel's. Upstream publishes no lock file: `scripts/nanobot_lock.py` has uv resolve `nanobot-ai==0.4.0` from pypi.org with every file published on or before 2026-10-31 (the 14-day rule for dependencies too) and writes each requirement with its SHA-256 hashes (`sandbox/Containerfile.nanobot.lock`) |
| Install | `sandbox/Containerfile.nanobot`: CPython 3.12 from uv's own builds, a venv at `/opt/nanobot/venv` with `uv pip install --require-hashes --no-deps` from the lock (pypi.org only), the tokenizer file `cl100k_base.tiktoken` fetched once and checked against tiktoken's own SHA-256, `nanobot` on the PATH. Nothing is piped into a shell, nothing of nanobot's runs at install (a wheel has no install scripts), and the install is read-only in the sandbox |

Verified here: the dates, the tag and commit (`git ls-remote`), both wheels' digests (PyPI's JSON API; the x86_64 wheel
and the sdist downloaded and hashed), the source of the release (the sdist, the wheel and the terminal client's source
archive: a grep of every host, variable, subprocess and switch named below), a venv installed from the lock on
CPython 3.12 (`nanobot --version`: v0.4.0), and runs of it against a local fake OpenAI-compatible model with every
other connection sent to a logging proxy that refuses it, while every non-loopback socket of nanobot's processes was
sampled every 0.2 s: with upstream's defaults and with sandbox-ai's settings. Not verified here: the image build (no
Podman in this sandbox; it builds in CI or on the host; `openaipublic.blob.core.windows.net`, where the tokenizer file
comes from, is not reachable from this sandbox) and a start in a real sandbox; hence it was not live-tested when this review was made (since then CI's job `agents-live (nanobot)` and the live check's `harnesses-live` run it in a real sandbox).

## Hosts contacted by default (measured)

nanobot's HTTP clients (httpx, requests, urllib) honour `HTTPS_PROXY`; its release check does not (below). No direct
connection was seen in any run.

| Run | Upstream's defaults | sandbox-ai's settings |
|---|---|---|
| one-shot (`nanobot -m "say hi"`, OpenAI-compatible model on loopback) | `openaipublic.blob.core.windows.net` (tiktoken's `cl100k_base` vocabulary, at the first count of tokens), the model | the model (the vocabulary is in the image) |
| interactive (`nanobot`, the terminal client; it starts the gateway on demand), one turn | the vocabulary, the model (the turn, a session title) | the model |
| `nanobot gateway`, 45 s idle | the vocabulary; it registers the heartbeat (every 30 min) and Dream (every 2 h) jobs, each a model turn of its own | nothing; no job registered (`Cron: 0 jobs`, `Dream: disabled`) |
| `nanobot webui --no-open --yes`, the page loaded | the vocabulary; it listens on 127.0.0.1:8765 (WebUI, a login secret) and 127.0.0.1:18790 (health) only | the same addresses; nothing outside |
| the WebUI's Settings > About, `nanobot update --check` | `raw.githubusercontent.com/HKUDS/nanobot/release-index/catalogue.json` and `pypi.org/pypi/nanobot-ai/...` (read in the source, `release_feed.py`, `update.py`; the release feed connects directly with pinned DNS, ignoring proxy variables - sandbox-ai's firewall sends every connection to its proxy anyway) | closed; refused quietly by the proxy unless `--nanobot-enable updates` |
| a `web_fetch` tool call | `r.jina.ai` (Jina Reader gets the URL first; nanobot falls back to fetching it itself) | nanobot fetches the page itself; Jina only with `--nanobot-enable web-reader` |
| a `web_search` tool call | DuckDuckGo through the `ddgs` package (several of its search backends) | closed unless `--nanobot-enable web-search` |

## How sandbox-ai configures nanobot

nanobot reads one file, `~/.nanobot/config.json`; its `NANOBOT_*` variables apply only when that file does not exist
(`config/loader.py`: `Config.model_validate` of the file's data bypasses the settings sources), and nanobot itself writes
the file (`nanobot onboard`, the WebUI's settings). So sandbox-ai writes its settings for each start into the session
directory, mounted read-only at `/etc/nanobot` (`sandbox-ai.json`), and the start lays them over the user's file
before nanobot runs (`/etc/nanobot/pin_config.py`: the user's keys stay, sandbox-ai's win; a new file is made when
there is none). nanobot's later changes to the same keys last until the next start; what a feature opens on the
network is held by the firewall and the proxy, not by the file. sandbox/nanobot.py: `BASE_CONFIG`, `managed_config`,
`PIN_SCRIPT`, `environment`.

- `tools.exec.sandbox: ""` - nanobot's own command sandbox (bubblewrap on Linux) is off: nanobot runs inside
  sandbox-ai's sandbox, which is the boundary (nested bwrap is untested and would need user namespaces inside it).
- `gateway.host`, `api.host`, `channels.websocket.host`: `127.0.0.1`, with the WebSocket's token required: the
  gateway (health, port 18790), the OpenAI-compatible API (`nanobot serve`, 8900) and the WebUI/terminal client's
  WebSocket (8765) listen on the sandbox's loopback only. `--host` with another address in nanobot's arguments is
  refused.
- `gateway.heartbeat.enabled: false`, `agents.defaults.dream.enabled: false` (both on upstream): no model turns
  nobody asked for, unless `--nanobot-enable scheduler`.
- `tools.web.fetch.useJinaReader: false`; `tools.webuiAllowRemotePackageInstall: false`.
- `--mcp` servers are `tools.mcpServers` entries, each running the launcher's bridge (stdio).
- `--llm` is nanobot's `custom` provider (any OpenAI-compatible endpoint): `providers.custom.apiBase` and the
  placeholder key, `agents.defaults.provider: custom`, the model, its context window when the server reports it, the
  loop guard's tool-iteration limit (`agents.defaults.maxToolIterations`). OpenRouter through `--llm` likewise.
- `--model` is `agents.defaults.model`, `--effort` `agents.defaults.reasoningEffort` (low, medium, high, xhigh, max).
- `--api-key api.anthropic.com=ANTHROPIC_API_KEY` (and OpenAI's, OpenRouter's, DeepSeek's, Gemini's): nanobot sees a
  placeholder in the provider's `apiKey`, the proxy puts the key in on the wire; with exactly one of them it is the
  provider nanobot starts with.
- Variables: `NANOBOT_TUI_NO_DOWNLOAD=1` (the client in the wheel is used; never a download of another),
  `TIKTOKEN_CACHE_DIR=/opt/nanobot/tiktoken` (the vocabulary in the image, read-only), the telemetry opt-outs.

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the firewall and the connection proxy refuse it even
if nanobot tries. A switch in `code` is a feature of `--nanobot-enable FEATURE[,FEATURE...]` (`-nb`; per start, in
presets and project files, and in the app's nanobot section), the same form as Hermes' `--hermes-enable`: each opens
only its own hosts and shows in the dry run's `"nanobot"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Telemetry, analytics, crash reports | none in the source (no analytics or crash-reporting library; Langfuse tracing only with `LANGFUSE_SECRET_KEY` and the `langfuse` package, which is not installed) | **always off, not switchable** (see Telemetry) | none |
| Own command sandbox (`tools.exec.sandbox`: `bwrap`, `seatbelt`) | none | none (pinned): sandbox-ai's sandbox is the boundary | none |
| Docker | the project's own `Dockerfile`/`docker_bootstrap.py` bind `0.0.0.0` | not used: nanobot is installed from the wheel; nothing binds outside the loopback | none |
| Scheduler: heartbeat (every 30 min a turn on `HEARTBEAT.md`), Dream (every 2 h a memory consolidation turn) | on (in the gateway) | off (pinned) | `scheduler`: both on, in the gateway while the sandbox runs; no host of its own (they use the model) |
| Cron jobs (`cron` tool, `/trigger`) | the agent sets them up when asked; they run in the gateway | they run in the gateway while the sandbox runs (the gateway ends with the session); nanobot 0.4.0 has no switch that removes the tool | - |
| Persistent gateway (`nanobot gateway --background`, `/detach`, `nanobot gateway restart\|stop`) | on demand | `nanobot gateway` in the agent's arguments is refused without `scheduler`; the gateway the terminal client starts ends with the session | `scheduler` |
| Chat channels (Telegram, Discord, Slack, WeChat, Feishu, DingTalk, Email, Matrix, Teams, WhatsApp, QQ, ...) | off until configured; their packages are installed at run time with pip | off; their packages are not in the image (the read-only venv takes no installs), their hosts closed | `gateway:telegram` (api.telegram.org): the Telegram channel (its packages are in the image), the bot token of the secret `nanobot_telegram` as `TELEGRAM_BOT_TOKEN` (`channels.telegram.token: "${TELEGRAM_BOT_TOKEN}"`, which nanobot resolves at start); who may talk to it: nanobot's pairing (the first message gets a code; `/pairing approve CODE` in nanobot or the WebUI's Settings) or `allowFrom` in `~/.nanobot/config.json` |
| Updates (`nanobot update`, the WebUI's update-and-restart: PyPI, the release catalogue on raw.githubusercontent.com, Sigstore checks) | manual (`nanobot update`), or from the WebUI's About page | closed; `nanobot update` in the agent's arguments is refused; the install is read-only, so an update can never be applied | `updates` (pypi.org, raw.githubusercontent.com): the check only. A new nanobot is a new review, then `sandbox-ai build nanobot` |
| Optional features (`nanobot plugins enable ...`, Langfuse, Bedrock, Azure, channel packages) | `pip install` into nanobot's own Python at run time (unpinned) | fails: the venv is read-only and PyPI closed | none (a new image) |
| Skills marketplace (WebUI: `skills.sh`, `skillhub.cn`) and nanobot's own skill writing (`skill-creator`) | on demand, into `~/.nanobot/workspace/skills` | closed; that folder holds the kits' skills (the sandbox-ai skill among them), read-only (sandbox/kits.py: LAYOUT), so nothing is installed there | none (a skill of yours goes into a kit: `sandbox-ai kits`) |
| CLI apps (CLI-Anything registry on hkuds.github.io and raw.githubusercontent.com, installed with pip) | on demand | closed | none (`--allow-host` if you must; installs fail on the read-only venv) |
| Web search (`web_search`: DuckDuckGo through `ddgs` by default; Brave, Tavily, SearXNG, Jina, Kagi, Exa ... with a key) | on | closed | `web-search` (duckduckgo.com, html.duckduckgo.com, lite.duckduckgo.com); a keyed provider needs its host (`--allow-host`, `--api-key`) |
| Jina Reader for `web_fetch` (`r.jina.ai`) | on | off (pinned): nanobot fetches pages itself | `web-reader` (r.jina.ai, s.jina.ai) |
| WebUI | `nanobot webui`, 127.0.0.1:8765 with a login link (a secret in its fragment) | on the sandbox's loopback only; the app and `--browser` show it | - |
| Terminal client (`nanobot`) | the Bun-compiled client from the wheel; a source checkout downloads Bun; a missing client downloads one from the GitHub release | the client from the wheel (`NANOBOT_TUI_NO_DOWNLOAD=1`) | - |
| Remote WebUI pairing (`nanobot remote`, Nanobot Desktop) | on demand | nothing to pair with: no host opened, no Desktop | none |
| OAuth providers (`nanobot provider login openai-codex\|github-copilot\|xai`) | on demand | closed (their hosts are not open) | the provider's hosts (`--allow-host`); sandbox-ai has no login for nanobot |
| MCP (`tools.mcpServers`, stdio and HTTP; WebUI MCP presets that run `npx`/`uvx`) | the user's | `--mcp` servers through the launcher's bridge; a preset that downloads needs its registry's host | `--mcp`, `--allow-host` |
| Subagents, goals (`/goal`), memory (`memory/MEMORY.md`, git history with dulwich) | on | on, local | - |

### Telemetry: always off, never switchable

The user's rule: no telemetry, analytics, crash reporting or usage pings, and no switch that turns them on.

Also from inside the sandbox (sandbox/telemetry_guard.py, since 0.1.97): a `.pth` in the venv's site-packages runs
sandbox-ai's telemetry guard at the start of every process of nanobot's Python: a Langfuse variable with a value of
its own (`LANGFUSE_*`: a key, its host) or an OpenTelemetry/OTLP one is refused with one message (exit status 2), the
empty keys and the opt-outs are pinned again.

What the review found: nanobot 0.4.0 sends none. Its source names no analytics or crash-reporting service; its only
tracing is Langfuse (`openai_compat_provider.py`, `openai_codex_provider.py`), started only when `LANGFUSE_SECRET_KEY`
is set and the `langfuse` package is installed (it is not, and nothing can install it in the sandbox). Its
`llm_usage.sqlite3` is a local record. The skills marketplace sets `DISABLE_TELEMETRY=1` for its own subprocess.

What sandbox-ai does in every nanobot run anyway (sandbox/nanobot.py: `TELEMETRY_ENV`, `TELEMETRY_HOSTS`,
`telemetry_rules`, `check_telemetry`):

- `LANGFUSE_SECRET_KEY` and `LANGFUSE_PUBLIC_KEY` are empty, `LANGFUSE_TRACING_ENABLED=false`,
  `OTEL_SDK_DISABLED=true`, `DO_NOT_TRACK=1` (and the common opt-outs of the tools it may run);
- `*.langfuse.com`, `*.posthog.com` and `*.sentry.io` are never opened: no feature names them, and a start that would
  open one (`--allow-host`, a wildcard over it), hide it from the proxy (`--no-inspect`) or go out without the proxy
  (`--egress-all-allow` with `--no-proxy`) is refused;
- the connection proxy refuses every request to them in every nanobot run (the ruleset `telemetry`);
- the dry run's `"nanobot"` shows `"telemetry": {"switchable": false, ...}`.

### What runs when nanobot starts

`nanobot` (no arguments) starts the terminal client, which starts the shared gateway on demand (a process of the
sandbox; the last client to leave ends it, a watchdog ends it if the client dies). The gateway: the configuration
(and its `${VAR}` references), the session database (`~/.nanobot/sessions/`), the workspace `~/.nanobot/workspace`
(`SOUL.md`, `USER.md`, `HEARTBEAT.md`, `memory/`, `skills/`), the tools (22 built in: files, `exec`, `rg`,
`apply_patch`, `web_search`, `web_fetch`, `cron`, `subagent`, sessions, goals, CLI apps), the MCP servers, the channels
(the WebSocket one for the clients), the cron service and, upstream, the heartbeat and Dream jobs. The launch
directory (`/work`) is the session's project: its `AGENTS.md` is read every turn. No update check, no catalogue
download, no telemetry at start.

How nanobot runs commands: the `exec` tool runs a shell command with the gateway's user and environment (no
approval prompt in 0.4.0; a deny list of patterns and an optional workspace restriction), long ones as sessions
(`exec_session`). Inside sandbox-ai that is a command in the sandbox, under its network rules.

### Not (yet) for nanobot

- `--with` (nanobot is not in the team image): use `--helper` in either direction (`sandbox-ai nanobot --helper
  claude`, `sandbox-ai claude --helper nanobot`, which runs `nanobot agent --message=TEXT`, a session of its own per task).
  nanobot's one-shot run works in its own workspace (`~/.nanobot/workspace`; its `--workspace` would write its
  files, and a git repository, into the project), so the task begins with a note that the project is `/work`; a
  preset's first task runs the same way (the terminal client takes no first task).
- Rules for the agent's own tools and kit hooks: they cannot be enforced (see above); refused.
- A `sandbox-ai login nanobot`: nanobot's own logins borrow other services' subscriptions (ChatGPT, Copilot, xAI);
  use `--api-key` or `--llm`.
- Teams, the activity feed of its turns, `mcp attach --live`.

## Rules for its own tools (2026-11-16): cannot be enforced

nanobot 0.4.0 has lifecycle hooks only for Python programs that use its SDK (`nanobot/agent/hook.py`: `AgentHook`,
passed to `Nanobot.run(hooks=...)`), whose errors it swallows; its CLI and gateway take none from a configuration.
A start with rules for its own tools (the mode paranoid's too) stays refused and says so; kit hooks likewise. What
holds: the network, MCP and device rules, its web fetches at the connection proxy.

## Go / No-Go

**Go** (choosing it starts it like any other agent).

- With sandbox-ai's settings nanobot contacts nothing but its model at start and during a turn (measured); its
  update check, marketplace, registries, keyless search and Jina Reader are on demand, closed and each switchable with
  only its own hosts.
- No telemetry; the Langfuse tracing it could do needs a package the sandbox cannot install.
- Its install is hash-pinned (wheel and every dependency), nothing runs at install, the venv is read-only, so neither
  its updater nor its run-time `pip install` paths can change it.
- Its own bubblewrap sandbox is off and its servers stay on the loopback.
- Against: a large and fast-moving code base (144,000 lines, an in-app updater with many modules, a broad channel
  surface, a 129 MB compiled terminal client); no upstream lock file (the lock is resolved here, with the 14-day rule
  for every file); the `exec` tool runs commands without asking (the sandbox is the boundary, `-D` changes nothing);
  user cron jobs have no off switch; and a big overlap with Hermes. Hence experimental, and the review is repeated
  for every release taken in.
