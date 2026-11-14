# Goose: review for sandbox-ai

Goose (aaif-goose/goose, formerly block/goose, Apache-2.0, a Rust CLI; part of the Agentic AI Foundation at the
Linux Foundation) as a sandbox-ai agent, `sandbox-ai goose`. This review covers the release built into
`localhost/goose-sandbox` (sandbox/Containerfile.goose): every host Goose contacts by default, its telemetry, its
updates, local inference, scheduler, gateways, recipes, subagents and extensions (MCP), and the switch for each.
Reviewed on 2026-11-13.

## The release

| | |
|---|---|
| Version | `1.57.0`: GitHub release `v1.57.0`, published 2026-10-28 19:26 UTC (release API), 16 days before the review |
| Commit | `30bba95105e59bc3e7df624cf6b4d433690cfe12` (`git ls-remote`: `refs/tags/v1.57.0`, a lightweight tag; a shallow clone of the tag checks out this commit) |
| Why this one | the newest release at least 14 days old (sandbox-ai's rule for third-party releases). Newer: 1.58.0 (2026-11-09), 1.58.1 (2026-11-10), 1.59.0 (2026-11-10), all younger than 14 days |
| Assets | `goose-x86_64-unknown-linux-gnu.tar.gz` `sha256:10eb96cb6a159b6e1d38eeedbef6ab64cd28168aad91948f1fa5c34eefdf98e3` (62 MB; checked here against the downloaded file), `goose-aarch64-unknown-linux-gnu.tar.gz` `sha256:c32605acfd560aeb2adbf17ec359cc0217e28022a7e647b35a6af5dcfca91749`: the digests GitHub's release API gives; the release has no SHA256SUMS file. Each tarball holds one file, `./goose` (208 MB, dynamically linked against glibc and libstdc++ only) |
| Provenance | GitHub's attestation API holds an SLSA v1 provenance for the release's assets, built by `aaif-goose/goose`'s `.github/workflows/release.yml` at `refs/tags/v1.57.0`, commit `30bba951` (read here; its Sigstore signature was not verified here: `gh attestation verify` needs Sigstore's TUF repository, which this sandbox does not reach) |
| Install | `sandbox/Containerfile.goose`: the tarball of the image's architecture from the release, `sha256sum -c` against `Containerfile.goose.lock` before anything is unpacked, the binary installed read-only at `/usr/local/bin/goose`. Never upstream's `download_cli.sh` (a script piped into a shell), no desktop app, not the `-vulkan` builds |
| Build from source? | not needed: the release's assets carry digests; the source tarball (`goose-source-v1.57.0.tar.gz`, 315 MB, about 1,128 crates) is the fallback should a release ever come without them |

Verified here: the dates, the commit and the digests (GitHub's API, `git ls-remote`, a clone of the tag), the x86_64
tarball's SHA-256, the binary (`goose --version`: 1.57.0, `--help` of every subcommand), the source at the tag (grep
of every host, variable and switch named below), and runs of the binary against a local fake OpenAI-compatible model
with every other connection sent to a logging proxy that refuses it, while every non-loopback socket of the process
was sampled every 0.1 s: with upstream's defaults, and with sandbox-ai's settings (below); the developer extension's
shell tool ran a command in each. Not verified here: the image build (no Podman in this sandbox: the image builds in
CI or on the host) and a start in a real sandbox; hence the release ships Goose as not live-tested yet
(the start says so in one line; no opt-in).

## Hosts contacted by default (measured)

Every connection went through the proxy (Goose's HTTP client, reqwest, honours `HTTPS_PROXY`); no direct connection
was seen in any run.

| Run | Upstream's defaults | sandbox-ai's settings |
|---|---|---|
| one-shot (`goose run -t "say hi"`, OpenAI-compatible model on loopback) | `models.dev` (the model catalog, at every start), the model (a session name, the turn, the tool result) | `models.dev` (refused quietly by sandbox-ai's proxy), the model |
| interactive (`goose session`), the first start, 75 s | the consent dialog "Share anonymous usage data?" with **Yes** preselected; Enter: `us.i.posthog.com` (a session event), `models.dev`, the model | no dialog; `models.dev` (refused), the model |
| interactive, with a user's `config.yaml` that says `GOOSE_TELEMETRY_ENABLED: true` and `otel_exporter_otlp_endpoint: http://otel.example.org:4318` | `us.i.posthog.com`, `otel.example.org:4318/v1/logs` every few seconds, `models.dev`, the model | `models.dev` (refused), the model: the variables win over the user's file |
| `goose serve` (ACP over HTTP with its scheduler), 20 s idle | - | `models.dev` (refused); it listens on 127.0.0.1:3284 only |
| `goose update` | `github.com` (the release download), then `api.github.com` (attestations) and the bundle's host | refused unless `--goose-enable updates` |
| `goose local-models search qwen` | `huggingface.co` (its API) | refused unless `--goose-enable local-inference` |
| `goose gateway start --bot-token ... telegram` | `api.telegram.org` | refused unless `--goose-enable gateway:telegram` |

## How sandbox-ai configures Goose

Goose reads a setting from a variable of its name in capitals first, then from `/etc/goose/config.yaml` (the
system's), the files of `GOOSE_ADDITIONAL_CONFIG_FILES` and last the user's `~/.config/goose/config.yaml`, each laid
over the one before (`crates/goose/src/config/base.rs`: `get_param`, `Config::default`, `load`). The system file is
the lowest layer: what must hold is a variable of Goose's process, which the agent cannot change; the system file
carries the sandbox's defaults, which a user's own configuration may change. sandbox-ai writes the file for each start
into the session directory and mounts it read-only at `/etc/goose` (sandbox/goose.py: `BASE_CONFIG`,
`managed_config`, `TELEMETRY_ENV`, `environment`), like the other agents' managed settings. Measured: an extension
and a provider given only in that layer are used.

- `GOOSE_MODE: smart_approve` (Goose asks before what changes something); upstream's default is `auto` (no
  questions), which `-D` gives back (`GOOSE_MODE=auto`). A headless run in an asking mode stops at its first question
  ("Approve/SmartApprove modes require an interactive terminal", measured), so a helper gets `auto` only with `-D`.
- No keyring (`GOOSE_DISABLE_KEYRING=1`): a container has no Secret Service; `goose configure` keeps keys in
  `~/.config/goose/secrets.yaml` of the sandbox's HOME. sandbox-ai's way is `--api-key HOST=NAME`: Goose sees a
  placeholder, the proxy puts the key in on the wire.
- The provider: with exactly one `--api-key` of a provider Goose knows under the variable it reads
  (`api.anthropic.com=ANTHROPIC_API_KEY`, `api.openai.com=OPENAI_API_KEY`, `openrouter.ai=OPENROUTER_API_KEY`, ...)
  the system file names it (`GOOSE_PROVIDER`); `--llm` sets it as variables (below).
- `--mcp` servers are stdio extensions in the system file, each running the launcher's bridge.
- `--model` and `--effort` are `GOOSE_MODEL` and `GOOSE_THINKING_EFFORT` (off, low, medium, high, max): Goose has no
  such options before its subcommand.

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the firewall and the connection proxy refuse it even
if Goose tries. A switch in `code` is a feature of `--goose-enable FEATURE[,FEATURE...]` (`-gx`; per start, in presets
and project files, and in the app's Goose section), the same form as Hermes' `--hermes-enable`: each opens only its
own hosts and shows in the dry run's `"goose"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Usage data to PostHog (`us.i.posthog.com`, `crates/goose/src/posthog.rs`) | opt-in, asked at the first interactive start with **Yes** preselected | **always off, not switchable** (see Telemetry) | none |
| OpenTelemetry export (`otel_exporter_otlp_endpoint`, `OTEL_EXPORTER_OTLP_*`) | off unless configured | **always off, not switchable** | none |
| Model catalog (`models.dev/api.json`, `model_catalog.rs`) | at every start of the CLI, ACP and `serve`, no setting turns it off | closed; the proxy refuses it quietly (a rule of the ruleset telemetry), so `--ask` never asks; the catalog Goose keeps serves | `model-catalog` (models.dev) |
| Updates (`goose update`: the release from GitHub, its Sigstore provenance through GitHub's attestation API) | manual only; no automatic check (none in the source, none measured) | closed; `goose update` in the agent's arguments is refused; the install is read-only in the sandbox, so an update can never be applied | `updates` (github.com, api.github.com, objects.githubusercontent.com, release-assets.githubusercontent.com, tmaproduction.blob.core.windows.net): it fetches and checks; replacing the binary fails. A new Goose is a new review, then `sandbox-ai build goose` |
| Local inference (`goose local-models search|download`: GGUF models from Hugging Face, llama.cpp in the binary) | on demand | closed; `goose local-models` in the agent's arguments is refused. This build has no GPU support (not the `-vulkan` asset): a model runs on the sandbox's processors within its memory limit | `local-inference` (huggingface.co, cdn-lfs.hf.co, cdn-lfs-us-1.hf.co, cdn-lfs-eu-1.hf.co, cas-bridge.xethub.hf.co) |
| Scheduler (`goose schedule add|list|run-now`: cron-run recipes) | the jobs run inside `goose serve` (or the desktop app) | the jobs can be set up, nothing runs them | `scheduler`: `goose serve` in the background on 127.0.0.1:3284 with a key of its own per start (`GOOSE_SERVER__SECRET_KEY`), log `~/.local/state/goose/logs/sandbox-ai-serve.log`; the recipes run while the sandbox runs, reaching only what it lets through. `goose serve` on another address or without its key is refused |
| Gateways (`goose gateway start --bot-token TOKEN telegram`; Telegram is the only one in 1.57.0) | on demand | closed; `goose gateway` in the agent's arguments is refused | `gateway:telegram` (api.telegram.org): the bot token of the secret `goose_telegram` as `TELEGRAM_BOT_TOKEN` (`sandbox-ai secrets set goose_telegram`), the gateway in the background (log `sandbox-ai-gateway-telegram.log`); who may talk to it: `goose gateway pair telegram` |
| Recipes (`goose run --recipe FILE`, sub-recipes, `goose recipe`) | local files; a GitHub repository when `GOOSE_RECIPE_GITHUB_REPO` is set | local files work; a recipe repository needs its host (`--allow-github`) | - |
| Subagents (`delegate`, the Summon extension) | on | on: they use the same provider and run inside the sandbox | - |
| Extensions (MCP: `stdio`, `streamable_http`; `goose session --with-extension`) | the user's | `--mcp` servers through the launcher's bridge (system file); a remote extension needs its host | `--mcp`, `--allow-host` |
| Built-in platform extensions (developer, analyze, apps, extension manager, skills, top of mind, summon, todo) | on (the first seven) | on, local: they read and write the workspace and run commands inside the sandbox | - |
| Goose's docs skill (`goose-doc-guide`, `https://goose-docs.ai`) | when the model asks for it | closed | `--allow-host goose-docs.ai` |
| ACP server (`goose acp` on standard input, `goose serve` over HTTP) | on demand | `goose acp` works; `goose serve` only on the loopback with its key | `scheduler` starts it |
| Peer-to-peer roaming (`goose roam`, iroh relays) | not in the release build (a cargo feature it is built without) | absent | none |
| Desktop app, voice dictation | the app's | absent (no desktop app in the image) | none |

### Telemetry: always off, never switchable

The user's rule: no telemetry, analytics, crash reporting or usage pings, and no switch that turns them on - not a
feature of `--goose-enable`, not `--telemetry` (whose former `on` is gone since 0.1.95), not a preset or project file, not the agent's own arguments.

What the review found: Goose 1.57.0 has two telemetries. Its *usage data* (`posthog.rs`: a `session_started` event
with the OS, Goose's version, the provider and model, extension and session counts, to PostHog's
`us.i.posthog.com/capture/`; error and slash-command events are written but "temporarily disabled") is opt-in: sent
only after an explicit yes - but the CLI asks at its first interactive start with **Yes** preselected, so Enter opts
in (measured). `GOOSE_TELEMETRY_OFF=1` forces it off before any choice is read; `GOOSE_TELEMETRY_ENABLED=false` is the
choice itself (no dialog). The desktop app's onboarding events (`emit_event`, `onboarding_*`, which bypass the choice)
are not called anywhere in the CLI's code. Its *OpenTelemetry* export (`crates/goose/src/otel/otlp.rs`) starts when an
OTLP endpoint is configured - also from a user's `config.yaml` (measured: logs every few seconds to it);
`OTEL_SDK_DISABLED=true` turns every signal off, and `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none` say the same per
signal. The source names no other analytics or crash reporting host (no Sentry, Segment, Mixpanel, Amplitude).

What sandbox-ai does in every Goose run, whatever else is enabled (sandbox/goose.py: `TELEMETRY_ENV`,
`TELEMETRY_PINNED`, `TELEMETRY_HOSTS`, `telemetry_rules`, `check_telemetry`):

- the variables are set, and they win over every configuration file Goose reads: `GOOSE_TELEMETRY_OFF=1`,
  `GOOSE_TELEMETRY_ENABLED=false`, `OTEL_SDK_DISABLED=true`, `OTEL_TRACES_EXPORTER=none`,
  `OTEL_METRICS_EXPORTER=none`, `OTEL_LOGS_EXPORTER=none`, `DO_NOT_TRACK=1`; the system file says
  `GOOSE_TELEMETRY_ENABLED: false` as well;
- `*.posthog.com` is never opened: no feature names it, and a start that would open it (`--allow-host`, a wildcard
  over it), hide it from the proxy (`--no-inspect`) or go out without the proxy (`--egress-all-allow` with
  `--no-proxy`) is refused;
- the connection proxy refuses every request to it in every Goose run (the ruleset `telemetry`, rule
  `telemetry-goose-1`), also behind `--egress-all-allow`;
- the dry run's `"goose"` shows `"telemetry": {"switchable": false, "blocked_hosts": ["*.posthog.com"], ...}`.

Measured with these settings: no dialog, no PostHog request, no OTLP request, also with a user's file that turns both
on. Tests: tests/test_goose.py and the feature matrix's `goose-telemetry-is-never-switchable` (every feature with
an old `--telemetry on`, which is ignored: the plan opens no telemetry host, the variables and proxy rules hold, the refusals).

### What runs when Goose starts

`goose` (no subcommand) opens `goose configure` when no configuration file exists at all - the system file always
exists here, so it starts its interactive session. At every start: logging to `~/.local/state/goose/logs`, the
model catalog's download (in the background, 15 s timeout; refused here), the configuration, the extensions (the
platform ones in the process, stdio ones as children), the hints (`~/.config/goose/.goosehints` and
`~/.config/goose/AGENTS.md`, the project's `.goosehints`/`AGENTS.md`), the skills (`~/.agents/skills`,
`~/.config/goose/skills`, the project's), the provider's model list (`/v1/models`), and a short request to the model
that names the session.

### Providers

- **API keys**: `--api-key api.anthropic.com=ANTHROPIC_API_KEY` (and `api.openai.com=OPENAI_API_KEY`,
  `openrouter.ai=OPENROUTER_API_KEY`, `generativelanguage.googleapis.com=GOOGLE_API_KEY`, `api.x.ai=XAI_API_KEY`):
  Goose sees a placeholder, the proxy adds the key on the wire, and with exactly one of them Goose starts with that
  provider. Its other providers (Bedrock, Vertex, Azure, Databricks, Ollama ...) work with their hosts and
  `goose configure`.
- **`--llm NAME[:MODEL]`** (LM Studio, Ollama, vLLM, llama.cpp, OpenAI-compatible services, OpenRouter): variables
  that win over Goose's files: `GOOSE_PROVIDER=openai`, `GOOSE_MODEL`, `OPENAI_BASE_URL` (the server's `/v1` URL),
  `OPENAI_API_KEY` (the placeholder), `GOOSE_CONTEXT_LIMIT` when the server reports it; the loop guard's turn limit
  as `GOOSE_MAX_TURNS`, its instructions in every turn through Goose's Top Of Mind extension
  (`GOOSE_MOIM_MESSAGE_FILE=/etc/goose/sandbox-ai-loop-guard.md`, read-only; measured: the text reaches the model).
- **Subscriptions through other CLIs** (Goose's `claude-code`, `codex`, `gemini-cli` providers run those CLIs): not in
  this image.
- **No login of its own**: `sandbox-ai login goose` does not exist; nothing of the host's Goose is copied in.

### Not (yet) for Goose

- `--with` (Goose is not in the team image): use `--helper` in either direction (`sandbox-ai goose --helper claude`,
  `sandbox-ai claude --helper goose`, which runs `goose run --text=...`).
- Rules for the agent's own tools and kit hooks (refused, never silently unenforced - so not the mode paranoid);
  Goose's own hooks stay the user's.
- Teams, the activity feed of its turns, `mcp attach --live`.

## Go / No-Go

**Go, not live-tested yet** (choosing it starts it; the start says so in one line).

- With its telemetry off, Goose contacts nothing by default beyond its model and the model catalog, which the proxy
  refuses quietly; every other outbound part (updates, local models, gateways, the scheduler's server) is on demand,
  closed here and each switchable with only its own hosts.
- Its telemetry has a hard off switch (`GOOSE_TELEMETRY_OFF`) and the OTLP export one (`OTEL_SDK_DISABLED`), both
  variables that win over every file the agent could write; measured off even against a user's file that turns them
  on.
- One binary with published SHA-256 digests and an SLSA provenance; nothing runs at install; no self-update in the
  sandbox (read-only install, `goose update` refused).
- MCP-native, local models first-class (an OpenAI-compatible base URL, a model, a context limit, a turn limit).
- Against: the consent dialog preselects Yes upstream (never shown here); the model catalog download has no switch
  (refused at the proxy instead); a large code base that releases weekly (about 1,128 crates); its scheduler and
  gateway are long-running servers (off here, on the loopback when on); not live-tested in a sandbox yet. Hence
  experimental, and the review is repeated for every release taken in.
