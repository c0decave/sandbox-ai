# OpenHands: review for sandbox-ai

OpenHands (OpenHands/OpenHands, formerly All-Hands-AI/OpenHands, MIT; All Hands AI) as a sandbox-ai agent,
`sandbox-ai openhands`. OpenHands today is two projects: **Agent Canvas** (the OpenHands/OpenHands repository, npm
`@openhands/agent-canvas`), a web UI whose launcher starts the agent server and an automation backend, and the
**Agent Server** (OpenHands/software-agent-sdk, PyPI `openhands-agent-server` with `openhands-sdk`, `openhands-tools`,
`openhands-workspace`; the automation backend is OpenHands/automation, PyPI `openhands-automation`). The old OpenHands
CLI (OpenHands/OpenHands-CLI) is "no longer actively maintained" and is not used. This review covers what is built into
`localhost/openhands-sandbox` (sandbox/Containerfile.openhands): every host OpenHands contacts by default, its
telemetry, updates and downloads at start, its runtimes, automations, MCP, skills and plugins, and the switch for each.
Reviewed on 2026-11-14 (the integration shape follows docs/AGENT_CANDIDATES.md, section 2).

## The releases

| | |
|---|---|
| Agent Canvas | `1.34.0`: npm `@openhands/agent-canvas@1.34.0`, published 2026-10-27 14:50 UTC (18 days before the review), integrity `sha512-1/XtmD1IcaXevKzjakUQDVCpV4W5f7s1+mP9YTQ6LqaopNsHeR7mVzzlnrKhY6K86xajz7jgI8d4vBrXRCL9Eg==`, an SLSA provenance attestation on the registry; tag `v1.34.0` = commit `6c120073f5a1e1c838ba6fdd9a075c79ff997b98` (`git ls-remote`) |
| Agent Server | `1.58.1`: PyPI `openhands-agent-server`, `openhands-sdk`, `openhands-tools`, `openhands-workspace` `==1.58.1`, published 2026-10-23 (22 days); tag `v1.58.1` = commit `60192ed54db73ac658ac1a07462916126c32d3a0` |
| Automation backend | `1.26.0`: PyPI `openhands-automation==1.26.0`, published 2026-10-26 (19 days); tag `1.26.0` = commit `3e8c54cb7c4f72f46178acef2ac8d3afc7cbe118` |
| Why these | Canvas 1.34.0 is the newest Canvas at least 14 days old (sandbox-ai's rule); newer: 1.35.0 (2026-11-04) to 1.40.0 (2026-11-12). Its own `config/defaults.json` names the stack it was released with: agent server 1.58.1 (minimum 1.57.0), automation 1.26.0, and automation 1.26.0 pins `openhands-sdk==1.58.1`. The candidates document proposed agent server 1.59.0 (2026-10-28, also old enough), but whether it works with Canvas 1.34.0 was unverified and the automation backend cannot share a venv with it; 1.58.1 is the pair upstream shipped and tested |
| npm side | `Containerfile.openhands.lock` (scripts/agent_npm_lock.py openhands): 577 packages, every version published before 2026-10-31 (`npm --before`), each with its sha512 integrity (six bundled ones covered by their parent's); `npm ci --ignore-scripts` installs exactly it. The install scripts left out: the Canvas' own `postinstall` (prints a message), `@heroui/shared-utils`, `@openrouter/sdk`, `core-js` (a funding note), `fsevents` (macOS only) |
| Python side | `Containerfile.openhands-python.lock` (scripts/openhands_lock.py): 214 requirements for Linux on CPython 3.12, resolved by `uv pip compile --universal --exclude-newer 2026-10-31`, each with the SHA-256 of its files; `uv pip install --require-hashes --no-deps` installs exactly it from pypi.org. CPython 3.12 from uv's own builds (uv 0.13.5, the release Hermes' image pins) |
| Install | sandbox/Containerfile.openhands: the two locks, then sandbox-ai's own `openhands` command (sandbox/openhands_cli.py) at `/usr/local/bin/openhands` and as the Canvas' `uvx` (`/opt/openhands/bin/uvx`). Nothing is piped into a shell; no Docker, no desktop (Electron) app, no OpenVSCode server |

Verified here: the dates, commits and integrities (npm's registry, PyPI's JSON API, `git ls-remote`); the Canvas
tarball's SHA-1 against the registry; both locks installed exactly (npm ci and uv's hash check passed, 749 MB of
`node_modules`, a 589 MB venv); the source of the Canvas (`bin/agent-canvas.mjs`, `scripts/dev-with-automation.mjs`,
`scripts/dev-safe.mjs`, `scripts/static-server.mjs`, the built pages' `telemetry` module), the agent server, SDK, tools
and automation wheels and the telemetry paths of browser-use, LiteLLM and Laminar (grep of every host, variable and
switch named below); and runs of the installed stack against a local fake OpenAI-compatible model, with every other
connection sent to a logging proxy that refuses it, while every non-loopback socket of the processes was sampled every
0.1 s: the Canvas with upstream's defaults and with sandbox-ai's settings, a conversation through the agent server's
API in each, a consent granted through the API in each, and `openhands task` (one task without the UI, twice, the
second resuming the first). Not verified here: the image build and a start in a real sandbox (no Podman in this
sandbox: the image builds in CI or on the host), and the Canvas page in a browser (no browser here; its kill switch
was checked in the HTML the Canvas serves and in the page's code); hence it was not live-tested when this review was made (since then CI's job `agents-live (openhands)` and the live check's `harnesses-live` run it in a real sandbox).

## Hosts contacted by default (measured)

Every connection of the Python processes went through the proxy (httpx, requests and LiteLLM honour `HTTPS_PROXY`);
no direct connection of the processes was seen in any run. The Canvas' Node.js launcher makes no outbound call at
start (its only outbound code, the DigitalOcean "MARS" remote backends, runs when a user signs in there).

| Run | Upstream's defaults | sandbox-ai's settings |
|---|---|---|
| Canvas start (upstream: uvx fetches the agent server, the automation backend and CPython from PyPI and GitHub at every start) | `pypi.org`/`files.pythonhosted.org` and `github.com`/`releases.astral.sh` (uvx; measured before the image's uvx was in front: CPython 3.14 from GitHub), `raw.githubusercontent.com` (LiteLLM's model catalog, from the agent server and the automation backend, 3 tries each) | nothing: the image's uvx runs the installed releases, LiteLLM uses the catalog it ships |
| a conversation (fake model on the loopback) | the model | the model |
| the same after the consent is granted (`misc_settings.telemetry.consent = granted`, what the Canvas' dialog writes) | `us.i.posthog.com` (the agent server's PostHog exporter: the Canvas launcher gives it upstream's key, 12 connections in 10 s) | nothing: `DO_NOT_TRACK=1` and `OH_TELEMETRY_EXPORTER=none` win over the consent, which the next start sets back to denied |
| `openhands task --text=...` and `--resume` | - | the model only |
| the Canvas page in a browser (code, not measured) | an install event to PostHog through `z.openhands.dev` at the first visit, before any consent; session events after consent | none: the page gets `window.__AGENT_CANVAS_DO_NOT_TRACK__=true` (checked in the served HTML), which every capture path checks first; the proxy refuses `z.openhands.dev` and `*.posthog.com` anyway |

## How sandbox-ai configures OpenHands

Three places, each out of the agent's reach:

- **Variables** (sandbox/openhands.py: `BASE_ENV`, `TELEMETRY_ENV`, `environment`), set for every OpenHands process
  after the start's own `--env` (they win): the telemetry kill switches (below), the reviewed versions
  (`OH_AGENT_SERVER_VERSION=1.58.1`, `OH_AUTOMATION_VERSION=1.26.0`, and the local-path and git-ref variables empty),
  the local runtime only (`OH_CONVERSATION_RUNTIME=local`, not selectable), no editor (`OH_ENABLE_VSCODE=false`: the
  image has no OpenVSCode server), no browser tool (`OH_ENABLE_BROWSER=false`: the image has no Chromium; the
  sandbox's own `--browser` is the human's), `LITELLM_LOCAL_MODEL_COST_MAP=True`, the Canvas bound to `127.0.0.1`.
  A start that sets one of the runtime or version variables itself, or `LOCAL_BACKEND_API_KEY`, is refused.
- **The image's uvx** (sandbox/openhands_cli.py): the Canvas launcher calls `uvx --from
  openhands-agent-server==1.58.1 ... agent-server` and `uvx --from openhands-automation==1.26.0 uvicorn ...`; the image
  puts its own `uvx` first on the Canvas' PATH, which runs the installed venv's `agent-server` and `uvicorn` for exactly
  those versions, refuses any other version, a git ref, an editable or local source and another index, and passes every
  other uvx call to the real uvx unchanged (MCP servers, kits). Measured: another version is refused with a message.
- **The start's settings**: OpenHands keeps its settings (model, MCP servers, approvals, the frontend's consent) in
  `~/.openhands/settings.json`, encrypted with a key of its own, written through its API. The launcher writes what the
  start sets into the session directory, read-only at `/etc/openhands/settings.json` (`managed_settings`), and the
  image's command applies it with `PATCH /api/settings` once the agent server answers (`apply_settings`; log
  `~/.openhands/sandbox-ai-settings.log`). Measured: applied within a second of the server's start. It sets the
  frontend's consent (denied, managed: no dialog), the approvals, and, when the start gives them, the model and the
  MCP servers; the servers an earlier start set and this one does not are removed. What the user changes in the UI is
  kept until a start sets that value again. A model change the server refuses (an ACP agent chosen in the UI) is
  dropped and the rest applied again.

Defaults:

- **Approvals**: the confirmation mode with the LLM security analyzer (OpenHands asks before an action the analyzer
  rates risky); upstream's default is no confirmation, which `-D` gives back. A task without the UI (`openhands task`,
  the helper form) runs without confirmation: nobody could answer.
- **Key per start**: the Canvas gets a new session key at every start (`LOCAL_BACKEND_API_KEY`), which it puts into the
  page it serves on the loopback; every request to the agent server needs it.
- **The model**: `--llm NAME[:MODEL]` (LM Studio, Ollama, vLLM, llama.cpp, OpenAI-compatible services, OpenRouter) is
  LiteLLM's `openai/MODEL` at the server's `/v1` URL with the placeholder as key (the proxy swaps it) and the context
  window when known; the loop guard's turn limit is the conversation's `max_iterations`, its instructions the agent's
  `system_message_suffix` (measured: the text reaches the model). With exactly one `--api-key` of a provider LiteLLM
  reads (`api.anthropic.com=ANTHROPIC_API_KEY`, `api.openai.com=OPENAI_API_KEY`, `openrouter.ai=OPENROUTER_API_KEY`,
  `generativelanguage.googleapis.com=GEMINI_API_KEY`, `api.x.ai=XAI_API_KEY`) its placeholder is the LLM's key and
  `--model` gets the provider's prefix (`anthropic/claude-...`). `--effort` is the LLM's `reasoning_effort` (none, low,
  medium, high, xhigh).
- **MCP**: the `--mcp` servers are stdio servers in `agent_settings.mcp_config`, each running the launcher's bridge.
- **Skills and memory**: the Canvas loads user and project skills for every conversation (`load_user_skills`,
  `load_project_skills`): the kits and the sandbox-ai skill in `~/.agents/skills` (read-only), the project's
  `AGENTS.md` and `.agents/skills`; sandbox-ai's per-project memory is `~/.openhands/skills/AGENTS.md`. The public
  skills catalogue comes from the Canvas' own package (`EXTENSIONS_PATH`), never a clone of GitHub.

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the firewall and the connection proxy refuse it even
if OpenHands tries. A switch in `code` is a feature of `--openhands-enable FEATURE[,FEATURE...]` (`-ox`; per start, in
presets and project files, and in the app's OpenHands section), the form of Goose's `--goose-enable`: each opens only
its own hosts and shows in the dry run's `"openhands"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Canvas analytics (PostHog through `z.openhands.dev`; an install event before consent, session events after) | on (install event always; the rest after consent) | **always off, not switchable** (see Telemetry) | none |
| Agent server and automation backend analytics (PostHog `us.i.posthog.com`, the Canvas' key) | after consent | **always off, not switchable** | none |
| browser-use telemetry (`eu.i.posthog.com`), Laminar tracing (`api.lmnr.ai`), OpenTelemetry | browser-use on by default; Laminar and OTLP when configured | **always off, not switchable** | none |
| Fetching the agent server, automation backend and Python at start (uvx: PyPI, GitHub, astral.sh) | at every start | never: the image's install, the image's uvx in front; another version refused | none (a new release is a new review, then `sandbox-ai build openhands`) |
| LiteLLM's model catalog (`raw.githubusercontent.com/BerriAI/litellm/.../model_prices_and_context_window.json`) | at every start of the agent server and the automation backend | off (`LITELLM_LOCAL_MODEL_COST_MAP=True`: the catalog LiteLLM ships) | `model-catalog` (raw.githubusercontent.com) |
| Automation backend (`openhands-automation`: scheduled and on-demand automations, its own SQLite database) | started with the Canvas | not started: the image's uvx holds its place, so the Canvas shows automations as unavailable | `automations` (no host: the backend on the loopback, 127.0.0.1:18001 behind the Canvas; automations run while the sandbox runs and reach what it lets through) |
| Docker runtime (`OH_CONVERSATION_RUNTIME=docker`: each conversation in a container of `ghcr.io/openhands/agent-server`) | off (local) | **refused**: local only, not selectable; no Docker CLI or socket in the sandbox | none |
| Remote backends (DigitalOcean Managed Agents, "MARS", `api.digitalocean.com`; OpenHands Cloud) | on sign-in | closed | `--allow-host` (not reviewed further) |
| ACP agents (the Canvas driving Claude Code, Codex, Gemini, Pi, OpenCode through `npx` packages) | on demand | closed: `npx` would fetch unreviewed packages from registry.npmjs.org | none (use sandbox-ai's own agents, `--helper`) |
| Public skills and plugins (`OpenHands/extensions`; marketplace plugins cloned from GitHub) | public skills from the package; plugins on demand | public skills from the Canvas' package; plugin installs closed | `--allow-github` |
| Git provider integrations (GitHub, GitLab, Bitbucket, Azure DevOps tokens in the UI) | on demand | closed | `--allow-host`/`--allow-github` with your token |
| OpenHands' hosted model proxy (`llm-proxy.app.all-hands.dev`), the critic and Gray Swan analyzers | when chosen | closed | `--allow-host` |
| Editor (OpenVSCode server), browser tool (browser-use with Chromium) | on (when installed) | off: not in the image | none |
| MCP (stdio, http, sse servers in the settings) | the user's | `--mcp` servers through the launcher's bridge; a remote server needs its host | `--mcp`, `--allow-host` |
| Terminal and file editor tools | on, as the user | on: they run inside the sandbox (tmux sessions of the agent server, `/tmp/openhands-agent-server-*`) | - |

### Telemetry: always off, never switchable

The user's rule: no telemetry, analytics, crash reporting or usage pings, and no switch that turns them on - not a
feature of `--openhands-enable`, not `--telemetry on`, not a preset or project file, not the agent's own arguments.

Also from inside the sandbox (sandbox/telemetry_guard.py, since 0.1.97): a `.pth` in the venv's site-packages runs
sandbox-ai's telemetry guard at the start of every process of OpenHands' Python (the agent server, the automation
backend, a task), and the image's `openhands` calls it before the Canvas: a variable of `TELEMETRY_ENV` set otherwise
(the consent granted, an exporter, browser-use's sync, the Canvas' switches), Laminar's `LMNR_*` or an
OpenTelemetry/OTLP variable of its own is refused with one message (exit status 2), the opt-outs are pinned again.

What the review found: OpenHands has five. **The Canvas page** (`src/services/telemetry.ts`, built into the page)
sends PostHog events through `z.openhands.dev`, OpenHands' own reverse proxy "to avoid ad blockers", so a host list
alone would not recognise it; a `canvas_install` event goes out at the first visit *regardless of consent*, session
events after consent. Its kill switch is `window.__AGENT_CANVAS_DO_NOT_TRACK__`, which the Canvas' static server
injects with `AGENT_CANVAS_DISABLE_TELEMETRY=1` and which the install and every capture path check first. **The agent
server** (`agent_server/telemetry`) has a PostHog exporter that delivers after consent; the exporter itself defaults to
`none`, but the Canvas launcher hands it upstream's PostHog key unless `VITE_DO_NOT_TRACK`/`DO_NOT_TRACK` is `1`
(measured: `us.i.posthog.com` right after consent). `DO_NOT_TRACK=1` is its kill switch (`policy.py`:
`kill_switch_engaged`), before any consent is read. **The automation backend** sends to PostHog when the launcher
gives it a key (never with `VITE_DO_NOT_TRACK=1`). **browser-use** (a dependency of the tools) sends anonymised
telemetry to `eu.i.posthog.com` unless `ANONYMIZED_TELEMETRY=false`. **Laminar/OpenTelemetry tracing** in the SDK
starts when `LMNR_PROJECT_API_KEY` or an OTLP endpoint variable is set.

What sandbox-ai does in every OpenHands run, whatever else is enabled (sandbox/openhands.py: `TELEMETRY_ENV`,
`TELEMETRY_PINNED`, `TELEMETRY_HOSTS`, `telemetry_rules`, `check_telemetry`):

- the variables, set after the start's own (they win): `AGENT_CANVAS_DISABLE_TELEMETRY=1`, `VITE_DO_NOT_TRACK=1`,
  `DO_NOT_TRACK=1`, `OH_TELEMETRY_EXPORTER=none`, `OH_TELEMETRY_CONSENT=denied` with
  `OH_TELEMETRY_CONSENT_MODE=override`, `ANONYMIZED_TELEMETRY=false`, `BROWSER_USE_CLOUD_SYNC=false`,
  `LMNR_PROJECT_API_KEY=` (empty), `OTEL_SDK_DISABLED=true`, `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`, the OTLP
  endpoint variables empty, `HF_HUB_DISABLE_TELEMETRY=1`; the image sets the first eight for every process as well;
- the frontend's consent is written as denied and managed at every start (no dialog);
- `z.openhands.dev`, `*.posthog.com` and `api.lmnr.ai` are never opened: no feature names them, and a start that would
  open one (`--allow-host`, a wildcard over it), hide it from the proxy (`--no-inspect`) or go out without the proxy
  (`--egress-all-allow` with `--no-proxy`) is refused;
- the connection proxy refuses every request to them in every OpenHands run (the ruleset `telemetry`, rules
  `telemetry-openhands-1` to `-4`), also behind `--egress-all-allow`;
- the dry run's `"openhands"` shows `"telemetry": {"switchable": false, "blocked_hosts": [...], ...}`.

Measured with these settings: no PostHog, Laminar or OTLP request, also after a consent granted through the API.
Tests: tests/test_openhands.py and the feature matrix's `openhands-telemetry-is-never-switchable` (every feature with
`--telemetry on`: the plan opens no telemetry host, the variables and proxy rules hold, the refusals).

### What runs when OpenHands starts

`openhands` (sandbox-ai's command) checks its options, gives the Canvas a key and its uvx, forks the settings step and
becomes the Canvas launcher (`bin/agent-canvas.mjs`): it picks free ports (ingress 8000 or the given one, agent server
18000, automation 18001, static 3001), starts the agent server (FastAPI on 127.0.0.1; it imports its tools, preloads
them - the browser's preload fails quietly without Chromium -, opens its tmux server), seeds the automation key into
its secrets, starts the automation backend (held by the image's uvx unless `automations`), the static page server and
the ingress, all on 127.0.0.1. State: `~/.openhands` (settings, conversations, the session and secret keys, the
automation database), kept with the session's HOME. Conversations run their tools in the sandbox as the agent.

### Not (yet) for OpenHands

- `--with` (OpenHands is not in the team image): use `--helper` in either direction (`sandbox-ai openhands --helper
  claude`, `sandbox-ai claude --helper openhands`, which runs `openhands task --text=...`).
- Rules for the agent's own tools and kit hooks: they cannot be enforced (see above); refused.
- A login of its own: none (`sandbox-ai login openhands` does not exist); the model's key comes with `--api-key`,
  `--llm` or the settings page.
- Teams, the activity feed of its turns, `mcp attach --live`.

## Rules for its own tools (2026-11-16): cannot be enforced

OpenHands' agent server 1.58.1 runs Claude Code-style hooks (`openhands.sdk.hooks`) only from a conversation's own
`hook_config`, which its client sends when it starts the conversation (the Canvas 1.34.0: the conversation's settings,
else the project's `.openhands/hooks.json` through the server's `/api/hooks`, a file the agent can write); there is
no administrator's layer for every conversation, and a conversation the agent starts through the server's API on the
loopback would have none. A start with rules for its own tools (the mode paranoid's too) stays refused and says so;
kit hooks likewise. What holds: the network, MCP and device rules, its web fetches at the connection proxy.

## Go / No-Go

**Go** (choosing it starts it like any other agent).

- With sandbox-ai's settings OpenHands contacts nothing by default but its model (measured); the parts that reach out
  (the automation backend, the model catalog) are off and each switchable with only its own hosts; the remote runtimes
  and agents are closed.
- Its telemetry has kill switches in every part (`AGENT_CANVAS_DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
  `ANONYMIZED_TELEMETRY`, the tracing keys), all variables the agent cannot change, and the proxy refuses their hosts,
  including OpenHands' own PostHog proxy; measured off even after a consent granted.
- Exactly the reviewed code: both sides hash-locked, nothing fetched at start (the image's uvx), no install script run,
  no self-update, read-only in the sandbox; the Docker runtime refused.
- Against: a young structure (the CLI deprecated this year, four repositories) that releases very fast (Canvas
  1.34 -> 1.40 in 16 days), so the 14-day rule keeps it several releases behind; a large install (about 1.3 GB);
  the page's install event before consent upstream (never here); the settings are applied through the agent server's
  API after it starts rather than read from a file it cannot change; not live-tested in a sandbox when reviewed; it has been since (see above). The review is repeated for every release taken in.
