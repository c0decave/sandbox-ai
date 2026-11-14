# Agent harness candidates for sandbox-ai

Which agent harnesses sandbox-ai should take on as agents/presets, beside the ones it supports (Claude Code, Codex,
OpenCode, Hermes Agent, DeepSeek Harness `dsh`, Qwen Code) and Cairn (already planned). Candidates: OpenClaw,
Agent Zero, Moltis, nanobot, ZeroClaw, Goose, OpenHands, Letta. Researched on 2026-11-13.

**How this was done.** I read sources and did not run anything. The GitHub API (repo metadata, releases, contributors) and `git ls-remote` (tag ->
commit) gave the release facts. The npm registry and PyPI gave package dates and integrity. I read each candidate's source tarball at the
pinned tag and grepped it for telemetry, update checks and hosts. I also read the README and docs at that tag, and ran a few web searches
for security history. Nothing was installed or run, so no host list is **measured**. The "hosts contacted" rows come from code and
docs, and each one needs measuring (logging proxy, as in `docs/QWEN_CODE_REVIEW.md`) before an image ships. Anything I did not
confirm in code or official docs is marked *(unverified)*.

The 14-day rule: today is 2026-11-13, so a pin must be published on or before **2026-10-30**.

## Summary

| Rank | Candidate | Verdict | Why, in one line |
|---|---|---|---|
| 1 | **Goose** (aaif-goose/goose) | **Integrate now** (experimental) | Apache-2.0, Linux Foundation (AAIF). A single Rust CLI with sha256-digested release assets. Telemetry is opt-in and has a hard env kill switch. MCP-native, with local models (Ollama/OpenAI-compatible, built-in llama.cpp). Adds recipes, a scheduler and subagents. |
| - | **Cairn** (oritera/Cairn) | **Integrate now** (as planned, pin a commit) | Its `runtime.execution: local` mode exists only on `main` (since 2026-07-15), not in a release. Pin `main` 8e7e0ea6 (2026-09-07). AGPL-3.0. |
| 2 | **OpenHands** (Agent Canvas + Agent Server) | **Integrate later** (next after Goose) | Strong SWE agent with a web UI, automations and ACP. The project was restructured recently (CLI unmaintained, frontend-only main repo) and churns fast (Canvas 1.34->1.40 in 16 days). The Canvas sends a PostHog install event before consent unless it is switched off. |
| 3 | **Letta Code** (letta-ai/letta-code) | **Integrate later** | Unique long-term memory and self-improving agents (MemGPT lineage), Apache-2.0, MCP. Letta Cloud is the default and the local backend is still flagged experimental. Telemetry and auto-update are on by default. About 100 releases since mid-August. |
| 4 | **nanobot** (HKUDS/nanobot) | **Later, optional** | MIT, small Python core, WebUI + TUI, MCP, memory, cron, bwrap option. Mostly overlaps Hermes, which is already supported. |
| 5 | **ZeroClaw** (zeroclaw-labs/zeroclaw) | **Watch** | Apache-2.0 Rust single binary with SHA256SUMS. Gateway + dashboard, SOP engine, hardware. Overlaps Hermes; young, with config-schema churn. |
| 6 | **OpenClaw** (openclaw/openclaw) | **Not now** | Huge (npm package 250 MB unpacked, install scripts), very fast churn, serious security history (CVEs, ClawHub malware). A messaging-assistant niche that Hermes already covers. |
| 7 | **Moltis** (moltis-org/moltis) | **Not now** | MIT Rust, good supply-chain hygiene, but effectively one maintainer. Web-UI-first and built around its own container sandbox. Overlaps Hermes. |
| 8 | **Agent Zero** (agent0ai/agent-zero) | **No** | Built to *be* its own root Docker appliance (Kali, sshd, SearXNG, XFCE desktop). Very heavy Python stack (torch, Whisper, TTS, unstructured), self-updating via git, Cloudflare tunnel helper. |
| - | **Letta server** (letta-ai/letta) | No (as an agent) | A server/API rather than a harness. Last release 0.16.8 (2026-05-14). Letta Code replaces it for terminal use. |

---

## The candidates in detail

### 1. Goose

| | |
|---|---|
| Repository | https://github.com/aaif-goose/goose (moved from `block/goose`; GitHub redirects). Part of the Agentic AI Foundation (AAIF) at the Linux Foundation (README) |
| License | Apache-2.0 |
| Stack | Rust (workspace `crates/goose`, `goose-cli`, `goose-mcp`, `goose-providers`, `goose-local-inference`, ...). Desktop app in Electron/TypeScript (`ui/desktop`). `Cargo.lock` lists about 1,128 packages |
| Latest release | v1.59.0, 2026-11-10 (also v1.58.0/1.58.1, 2026-11-09/10) |
| Newest >= 14 days | **v1.57.0**, 2026-10-28T19:26Z, commit `30bba95105e59bc3e7df624cf6b4d433690cfe12` (lightweight tag). Asset digests from the GitHub release API: `goose-x86_64-unknown-linux-gnu.tar.gz` `sha256:10eb96cb6a159b6e1d38eeedbef6ab64cd28168aad91948f1fa5c34eefdf98e3`, `goose-aarch64-unknown-linux-gnu.tar.gz` `sha256:c32605acfd560aeb2adbf17ec359cc0217e28022a7e647b35a6af5dcfca91749`. musl and `-vulkan` variants exist; there is no SHA256SUMS file |
| Maturity | 56.5k stars, about 688 contributors, 15 releases between 2026-08-15 and today (roughly weekly). Foundation-governed (`GOVERNANCE.md`) |
| How it runs | CLI/TUI (`goose session`, `goose run`, recipes), desktop app, ACP server (`goose serve`, default `127.0.0.1:3284`), `goose gateway` (Telegram and similar), `goose schedule`, `goose update` (`goose-cli/src/cli.rs`). It needs no Docker, root or systemd. The tools run as the user, so "local" execution inside the sandbox is the normal mode |
| Providers / local | 15+ providers per README: Anthropic, OpenAI, Google, Ollama, OpenRouter, Azure, Bedrock, and others. ACP providers can use Claude/ChatGPT/Gemini subscriptions. OpenAI-compatible endpoints via custom providers *(unverified for LM Studio/vLLM/llama.cpp by name, but standard)*. A built-in **local inference** crate (llama.cpp, MLX, Hugging Face model downloads: `crates/goose-local-inference`) |
| MCP | Native: "extensions" are MCP servers (README: "70+ extensions via MCP"); `goose mcp` |
| Telemetry | PostHog (`us.i.posthog.com`, `crates/goose/src/posthog.rs`). **Opt-in**: sent only after an explicit yes (`GOOSE_TELEMETRY_ENABLED`). **`GOOSE_TELEMETRY_OFF=1`** forces it off. Exception: the *desktop UI's* onboarding events (`onboarding_*`, `telemetry_preference_set`) bypass the choice (`emit_event`, interface "desktop"). The CLI does not use that path *(verify by measurement)*. OpenTelemetry module present (`crates/goose/src/otel`), off unless configured *(unverified)* |
| Updates | `goose update` (manual, GitHub release download). No automatic update check found in the CLI by grep *(unverified at runtime)* |
| Hosts by default | The configured provider only *(expected; not measured)*. Possibly `models.dev`/model catalog lookups (`model_catalog.rs`) *(unverified)* |
| State | Linux: config `~/.config/goose/` (`config.yaml`; `secrets.yaml` when the keyring is off), data `~/.local/share/goose/` (sessions). **`GOOSE_PATH_ROOT`** moves everything. **`GOOSE_DISABLE_KEYRING`** is needed in a container without Secret Service (docs `environment-variables.md`) |
| Adds over the six | **Recipes** (shareable YAML workflows with parameters), **scheduler** (cron-run recipes), subagents, built-in local inference (no separate model server), ACP server, Linux-Foundation governance |
| Risks | A large Rust dependency tree (about 1,128 crates; prebuilt binary, so no build risk). Local inference downloads models from huggingface.co (keep it off). `goose gateway` (messaging) and `goose update` must be off. The desktop app is out of scope |

### 2. OpenHands (Agent Canvas + Agent Server)

| | |
|---|---|
| Repositories | https://github.com/OpenHands/OpenHands (moved from `All-Hands-AI/OpenHands`) is now **Agent Canvas**, the TypeScript frontend and launcher, MIT. The agent itself is https://github.com/OpenHands/software-agent-sdk (Python SDK + Agent Server, MIT). https://github.com/OpenHands/OpenHands-CLI says **"no longer actively maintained"** (last release 1.16.0, 2026-05-08). There is also a separate `OpenHands/automation` (scheduling/webhooks) |
| License | MIT (Canvas `LICENSE`, SDK per GitHub) |
| Stack | Canvas: Node >= 24, React; npm `@openhands/agent-canvas` (51 deps). Agent Server: Python 3.12, LiteLLM; PyPI `openhands-agent-server`, `openhands-sdk` |
| Latest | Canvas v1.40.0 (2026-11-12); SDK v1.62.0 (2026-11-11) |
| Newest >= 14 days | Canvas **v1.34.0**, 2026-10-27, commit `6c120073f5a1e1c838ba6fdd9a075c79ff997b98`; npm `@openhands/agent-canvas@1.34.0` (2026-10-27, provenance attestation, a cosmetic `postinstall`). SDK **v1.59.0**, 2026-10-28, commit `ccae18cb897bef7960f210aa3f380b577a84be7c`; PyPI `openhands-agent-server==1.59.0` / `openhands-sdk==1.59.0` (2026-10-28). That the two are compatible is *(unverified)* |
| Maturity | Main repo 93.4k stars; SDK 1.4k stars, about 156 contributors. Canvas had 31 releases since 2026-08-15 and the SDK 34. A company (All Hands AI) sells OpenHands Cloud |
| How it runs | `agent-canvas` starts a **web UI** on `127.0.0.1:8000` plus the Agent Server and Automation backend **via `uvx`**, fetched from PyPI at start; pin with `OH_AGENT_SERVER_VERSION`/`OH_AGENT_SERVER_LOCAL_PATH` (`bin/agent-canvas.mjs`). "Option 1: without a sandbox" runs the agent server directly on the machine: that is the local mode sandbox-ai needs. Option 3 (`OH_CONVERSATION_RUNTIME=docker`) needs the Docker CLI/socket and must be refused |
| Providers / local | Anything LiteLLM supports, including OpenAI-compatible endpoints, Ollama, Anthropic and OpenRouter (README: "Use with any LLM"; LLM profiles) |
| MCP | The SDK has MCP support (Laminar instruments `litellm,mcp`; docs) *(details unverified)* |
| ACP | The Canvas can drive **Claude Code, Codex, Gemini, Pi, OpenCode** as ACP agents (README) |
| Telemetry | **Canvas: PostHog via `z.openhands.dev`** (a reverse proxy "to avoid ad blockers"). An **install event is sent on first use regardless of consent**; session events only after consent (`src/services/telemetry.ts`). It is off completely with **`AGENT_CANVAS_DISABLE_TELEMETRY=1`** / `--disable-telemetry` (or `VITE_DO_NOT_TRACK=1`, browser DNT). Agent Server: exporter default `none` (`agent_server/config.py`); PostHog only when configured. Laminar tracing (`api.lmnr.ai`) only with `LMNR_PROJECT_API_KEY` |
| Hosts by default | `z.openhands.dev` (Canvas telemetry), `pypi.org`/`files.pythonhosted.org` (uvx fetch, if not pinned locally), the provider *(not measured)* |
| State | `~/.openhands` (README Docker mount `/home/openhands/.openhands`); Canvas `$STATE_DIR/api-key.txt` |
| Adds over the six | A **web control centre** across agents and backends, **automations** (schedule, webhooks: Slack, GitHub, Linear), a strong SWE-bench-style coding agent, conversation history in a UI |
| Risks | Ongoing restructuring (CLI deprecated, repo split into four), very fast release cadence, a runtime `uvx` download, Docker runtime options, the automation integrations (Slack/GitHub tokens), a PostHog reverse proxy that host lists alone will not recognise |

### 3. Letta Code (and the Letta server)

| | |
|---|---|
| Repository | https://github.com/letta-ai/letta-code (the CLI harness). The server is https://github.com/letta-ai/letta (formerly MemGPT), whose README now sends terminal users to Letta Code |
| License | Apache-2.0 (both) |
| Stack | Letta Code: TypeScript, Node (`^22.23.4 \|\| ^24.22.1 \|\| ^26.12.1`) or Bun; npm `@letta-ai/letta-code` (22 deps, including `@earendil-works/pi-ai`, `@cursor/sdk`, `@modelcontextprotocol/sdk`, `node-pty`, `sharp`, **`@scarf/scarf`**). Also published as a PyPI wheel with bundled Node. Server: Python (118 requirements in `letta` 0.16.8) |
| Latest | Letta Code v0.34.59 (2026-11-13; several releases a day) |
| Newest >= 14 days | **v0.34.31**, 2026-10-29T08:34Z, commit `a8900b882446d7aa99fedf2845d400743affd1b6`; npm 0.34.31 published 2026-10-29T08:30Z with provenance attestation. Server: 0.16.8 (2026-05-14), commit `1131535716e8a31c9a437f8695e25ac98f203a24`; no release since |
| Maturity | Letta Code 3.8k stars, about 52 contributors (cpacker 1,368 commits), 100+ releases since 2026-08-15. Server 25.4k stars; repo last pushed 2026-10-18. A VC-funded company (Letta Cloud) |
| How it runs | Terminal CLI/TUI `letta`. Also `letta server` (connects this machine to Letta Cloud as a "remote computer"), channels (Telegram, Slack, Discord) and chat.letta.com. **Letta Cloud is the default backend**; `letta backend local` / `--backend local` / `LETTA_LOCAL_BACKEND_EXPERIMENTAL=1` runs with local state (`LETTA_LOCAL_BACKEND_DIR`; `src/backend/local/`). No Docker needed |
| Providers / local | `/connect` for OpenAI/ChatGPT, Anthropic, Z.ai and others. The local backend uses `pi-ai` and includes local endpoints (`pi-stream-adapter-local-endpoint` tests) *(exact OpenAI-compatible support unverified)* |
| MCP | Yes (`@modelcontextprotocol/sdk`, MCP OAuth code) |
| Telemetry | **On by default** (`src/telemetry/`, PostHog-classified errors). Off with **`LETTA_CODE_TELEM=0`** or **`DO_NOT_TRACK=1`**. `@scarf/scarf` is a dependency (install analytics; `SCARF_ANALYTICS=false` / `DO_NOT_TRACK=1`, and `npm ci --ignore-scripts` keeps it from running) *(Scarf behaviour per its docs, unverified here)* |
| Updates | **Auto-update on by default** (npm self-update at start, `src/updater/auto-update.ts`). Off with **`DISABLE_AUTOUPDATER=1`** |
| Hosts by default | `api.letta.com` (cloud backend, telemetry), `registry.npmjs.org` (auto-update), the provider *(not measured)* |
| State | `~/.letta` (settings, global skills), project `.letta/`. "MemFS" keeps memory in git (it can sync to a GitHub repo) |
| Adds over the six | **Persistent agents with long-term, self-edited memory** ("dreaming"/sleep-time compute, `/palace`, `/search` across agents), skill learning, agents calling agents, crons and heartbeats |
| Risks | Cloud-first design (agent state in Letta Cloud by default), the local backend is experimental, extreme release cadence, "self-improving" agents rewrite their own prompts/skills and "even the harness itself (through mods)" (README), messaging channels, remote-computer routing |

### 4. nanobot

| | |
|---|---|
| Repository | https://github.com/HKUDS/nanobot (HKU Data Intelligence Lab) |
| License | MIT |
| Stack | Python >= 3.11 (PyPI `nanobot-ai`, 41 base deps: `anthropic`, `openai`, `mcp`, `httpx`, `dulwich`, `sigstore`, ...); WebUI bundled in the wheel; a TypeScript TUI client (`tui/`) *(its runtime packaging unverified)* |
| Latest | v0.4.1, 2026-11-06 |
| Newest >= 14 days | **v0.4.0**, 2026-10-28T10:50Z, tag object `59d435ff...` -> commit `8f7a7d17590de7ab187650381894dba2b9e060ab`; PyPI `nanobot-ai==0.4.0` uploaded 2026-10-28 |
| Maturity | 50.1k stars, about 504 contributors, two named maintainers (Re-bin, chengyongru). Three releases since 2026-08-15 |
| How it runs | `nanobot` (TUI), `nanobot -m` (one-shot), `nanobot webui` (`127.0.0.1:8765`, login link with a secret fragment), a shared local **gateway** (background process; `/detach`), channels (Telegram, Discord, Slack, WeChat, Feishu, DingTalk, Email, Matrix, Teams, Linear, ...), an OpenAI-compatible API. No Docker needed; exec sandbox option `tools.exec.sandbox` = none / `bwrap` / `seatbelt` |
| Providers / local | OpenAI-compatible APIs, local model servers, Anthropic, OpenRouter (docs `providers.md`) |
| MCP | Yes (`mcp` dependency, MCP presets) |
| Telemetry | None found by grep (no analytics library). The skills marketplace sets `DISABLE_TELEMETRY=1` for its own subprocess |
| Updates | The WebUI update flow reads `raw.githubusercontent.com/HKUDS/nanobot/release-index/catalogue.json` and `pypi.org` (`nanobot/release_feed.py`) and can install updates with consent. Whether it checks automatically on load is *(unverified)* |
| State | `~/.nanobot/config.json`, the agent workspace (`USER.md`, `memory/MEMORY.md`) |
| Adds over the six | A lightweight local-LLM-friendly personal agent with WebUI and TUI sharing sessions, "Dream" memory consolidation, `/goal` long-horizon tasks, automations, many Chinese messaging platforms |
| Risks | Broad channel surface, an in-app updater (package mutation), and a big overlap with Hermes |

### 5. ZeroClaw

| | |
|---|---|
| Repository | https://github.com/zeroclaw-labs/zeroclaw |
| License | Apache-2.0 (GitHub license API) |
| Stack | Rust, a 22-crate workspace, about 1,297 packages in `Cargo.lock`; prebuilt binaries |
| Latest | v0.9.0, 2026-11-12 |
| Newest >= 14 days | **v0.8.7**, 2026-10-27T22:50Z, commit `99bd353c756f4e64f381cfe9c3002ebff32f3055`; `zeroclaw-x86_64-unknown-linux-gnu.tar.gz` `sha256:e10e6e4ba7bf8a081af4a8769d8c16ca3eb48e8e897eac42b94ca4335b120a34`; a `SHA256SUMS` asset |
| Maturity | 33.4k stars, about 479 contributors; created 2026-02-13. Four releases since 2026-08-15 (v0.8.5 -> v0.9.0) |
| How it runs | `zeroclaw agent` (interactive CLI), a gateway (HTTP/WS) with a **web dashboard**, `zeroclaw service install` (systemd/launchd; not needed), ACP over stdio, SOP engine (MQTT/webhook/cron), 30+ channels. Own OS sandboxes (Landlock, Bubblewrap, Seatbelt, Docker) and "supervised" autonomy by default |
| Providers / local | Anthropic, OpenAI (incl. Codex subscription), Ollama, any OpenAI-compatible endpoint, about 20 more; fallback chains (README) |
| MCP | Yes (custom MCP servers; docs `tools/mcp.md`) |
| Telemetry | No product telemetry found by grep. OpenTelemetry is behind the `observability-otel` feature and only with a configured endpoint. The gateway has a version-check handler (`crates/zeroclaw-gateway/src/version.rs`); whether it phones home on its own is *(unverified)* |
| State | `~/.zeroclaw/config.toml`; `ZEROCLAW_CONFIG_DIR`, `ZEROCLAW_DATA_DIR`; SQLite memory |
| Adds over the six | Hardware peripherals (GPIO/I2C/SPI/USB), event-triggered SOPs with approval gates, cryptographic tool receipts |
| Risks | Young and fast-changing (config schema "V3"). Third-party reviews flag security modules that are not wired in (LumaDock via eesel.ai) and an early path-traversal bug (GitHub Discussion #2867). Its own Bubblewrap/Landlock sandbox nested inside sandbox-ai's would need testing or turning off. Overlaps Hermes |

### 6. OpenClaw

| | |
|---|---|
| Repository | https://github.com/openclaw/openclaw (OpenClaw Foundation, a 501(c)(3); founded by Peter Steinberger) |
| License | MIT |
| Stack | TypeScript, Node `>=24.22.1 <25 \|\| >=26.12.1`, pnpm workspace; npm `openclaw` (66 deps including `playwright-core`, `@trycua/cua-driver` (computer use), `node-pty`, `koffi` (FFI), `npm` itself (plugin installs), `@homebridge/ciao` (mDNS), `@modelcontextprotocol/sdk`) |
| Latest | v2026.11.3 (2026-11-10); also a maintenance line v2026.9.37 (2026-11-10); betas |
| Newest >= 14 days | **v2026.10.9**, 2026-10-29T15:54Z, tag object `88a19e0d...` -> commit `8b18b5578f66eab1501cba4cef2310d88bfb9a41`; npm `openclaw@2026.10.9` 2026-10-29, provenance attestation, **unpacked 250 MB**, **`preinstall` + `postinstall` scripts** (`postinstall-bundled-plugins.mjs`). (The maintenance line's v2026.9.36 is from 2026-10-29 too.) |
| Maturity | 394k stars, about 3,789 contributors (steipete about 69k commits), 51+ releases since 2026-08-15 (first page only) |
| How it runs | A **Gateway** daemon (`openclaw onboard --install-daemon`: systemd/launchd) plus the Control UI (web), CLI and TUI. **`openclaw tui --local`** runs the embedded agent runtime without a Gateway (`docs/cli/tui.md`). Tool sandboxing is **off by default**: tools run on the host for the main session (README "Security"). The optional sandbox backends are Docker, Podman, SSH, OpenShell and Crabbox (`docs/gateway/sandboxing.md`). Inside sandbox-ai, "off" is the local mode |
| Providers / local | Anthropic, OpenAI, Ollama, LM Studio, vLLM, SGLang, llama.cpp, OpenRouter (`docs/concepts/model-providers.md`). Claude and Codex harnesses via plugins/ACP |
| MCP | Client registry (`openclaw mcp add/set/...`, `mcp.servers`) and `openclaw mcp serve` (`docs/cli/mcp.md`) |
| Telemetry | **A daily update check, on by default**: `GET https://telemetry.openclaw.ai/api/latest-version` with version/OS/Node/arch in the User-Agent; Cloudflare-derived location kept for 3 months. Anonymous feature statistics are opt-in. **`update.checkOnStart: false`** or **`OPENCLAW_NO_AUTO_UPDATE=1`** stops all of it; `CI=1` also suppresses; `DO_NOT_TRACK` does *not* stop the update check (`docs/gateway/telemetry.md`). Bonjour/mDNS advertising is opt-in on Linux (`docs/gateway/bonjour.md`) |
| State | `~/.openclaw` (`openclaw.json`, `.env`, agents, credentials); `OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH` (`docs/help/environment.md`) |
| Adds over the six | 20+ messaging channels, companion apps/nodes (voice, camera, screen), Canvas, cron jobs, active memory, ClawHub skills/plugins |
| Risks | **Security record**: CVE-2026-25253 (one-click RCE, fixed in 2026.1.29), CVE-2026-32922 (CVSS 9.9 privilege escalation), "ClawJacked", and the ClawHub malware campaigns ("ClawHavoc": 341 malicious skills in the first audit, more than 1,000 later; Unit 42 found skills evading scanning through May 2026). The package is huge, with install scripts and `npm` at runtime for plugins. The release cadence makes every review stale within days. Its niche (personal assistant in chat apps) is Hermes' |

### 7. Moltis

| | |
|---|---|
| Repository | https://github.com/moltis-org/moltis |
| License | MIT |
| Stack | Rust, 59 crates, about 1,384 packages in `Cargo.lock`; prebuilt binaries, deb/rpm/pkg, ghcr image |
| Latest / newest >= 14 days | **20261023.01**, 2026-10-23T19:29Z, commit `177f8f13f4e69cf2042eee1b2c5b115671861840` (lightweight tag). It is both the latest and >= 14 days old; repo last pushed 2026-10-23. Assets carry `.sha256`, `.sha512`, `.sig`, GitHub attestations and Sigstore (README) |
| Maturity | 3.0k stars, about 71 contributors, but **one dominant maintainer** (penso: 3,622 commits; next 61). 15 releases since 2026-08-15 |
| How it runs | A persistent **gateway server with a web UI** (onboarding in the browser, random port, password/passkey, an offer to trust a local CA), channels (Telegram, WhatsApp, Signal, Discord, Teams, Matrix, Slack, Nostr), a native desktop client, `moltis acp`. Exec sandbox `auto`: Apple Container -> Podman -> Docker -> `restricted-host` (env clearing + rlimits) -> none (`docs/src/sandbox.md`). Inside sandbox-ai it would have to be `restricted-host`/none. `allow_host_podman` mounts a host socket and must stay off |
| Providers / local | OpenAI, Anthropic, OpenRouter, local LLMs (`docs/src/local-llm.md`), many hosted providers |
| MCP | stdio + HTTP/SSE |
| Telemetry / updates | No product analytics found. An **update checker** fetches `https://www.moltis.org/releases.json` (`update_releases_url`); a switch that turns it off is *(unverified)*. Langfuse/OTLP/Prometheus are optional |
| State | `~/.config/moltis/moltis.toml` (`MOLTIS_CONFIG_DIR`), `~/.moltis/` (data, sandbox, docs) |
| Adds over the six | An encrypted vault, voice I/O, CalDAV, WASM tools, OpenClaw import |
| Risks | Bus factor of one. `enable_self_improvement` defaults to true (it writes its own skills). Web-UI-first, with its own TLS/CA handling. Overlaps Hermes |

### 8. Agent Zero

| | |
|---|---|
| Repository | https://github.com/agent0ai/agent-zero |
| License | MIT text with a copyright/contact header ("Agent Zero, s.r.o"); GitHub shows `NOASSERTION` because of the header |
| Stack | Python (Flask/uvicorn web UI), `litellm`, `sentence-transformers` (pulls in torch), `openai-whisper`, `kokoro` (TTS), `unstructured[all-docs]`, `faiss-cpu`, `docker`, `paramiko`, `patchright` (Chromium), `flaredantic` (Cloudflare tunnel), `fastmcp`/`mcp`, `boto3`, `exchangelib`, ... (`requirements.txt`, 75 lines) |
| Latest | v2.15, 2026-11-01 |
| Newest >= 14 days | **v2.14**, 2026-10-13, tag object `47558208...` -> commit `ff2a5b9c9c4e490ba38dd7c313e779510bb2071a` |
| Maturity | 19.8k stars, about 73 contributors, two main authors (3clyp50, frdel). Releases about every 2-3 weeks |
| How it runs | **Designed as its own Docker container**: base `kalilinux/kali-rolling`, sshd, SearXNG, an XFCE desktop and browser in the "Canvas", web UI on port 80 (`docker/base/Dockerfile`, README). It installs via the "A0 Launcher"/"A0 Install", which drive Docker. Code execution goes over SSH into the container, or a local shell when `ssh_enabled` is off (`plugins/_code_execution`). A dev mode runs the framework on the host with RFC to a Docker instance (`docs/setup/dev-setup.md`) |
| Providers / local | Via LiteLLM: OpenAI, OpenRouter, Ollama, Codex OAuth, ... (`A0_SET_chat_model_provider=ollama`) |
| MCP | Client and server; A2A |
| Telemetry / updates | No analytics library found by grep *(runtime unverified)*. **Self-update** from the Web UI pulls from the official repo via git (`docs/guides/self-update.md`). Plugin Hub index from `github.com/agent0ai/a0-plugins` releases; models (embeddings, Whisper, Kokoro) downloaded at first use *(unverified which)* |
| State | `/a0/usr` (projects, memory, secrets, settings) |
| Adds over the six | A full Linux desktop for computer use, browser DOM annotation, LibreOffice co-editing, "Time Travel" snapshots |
| Risks | An appliance that wants to be the sandbox itself (root, sshd, its own services); multi-GB image and dependency tree; self-modifying (self-update, plugin hub, editable prompts/tools); tunnels |

### Cairn (already planned)

| | |
|---|---|
| Repository | https://github.com/oritera/Cairn |
| License | **AGPL-3.0**, with a commercial dual licence offered and a contributor grant to both (README "License") |
| Stack | Python >= 3.12 with uv; server + dispatcher; web UI (server, port 8000) |
| Releases | v0.1.0 (2026-05-03), v0.2.0 (2026-05-05), **v0.2.1 (2026-05-10, commit `e0ef2f850e5805f824815ee38f049e066deeb7d1`)** |
| Local mode | `runtime.execution: local` (workers run as host processes, reusing installed `claude`/`codex`/`pi` CLIs) was added in commit `233f5e8505b9`, **2026-07-15, after v0.2.1** (`dispatch.local.example.yaml` is 404 at v0.2.1). So pin a commit: **`main` = `8e7e0ea67552383851dfcabfba0c4e9c8d007878`, 2026-09-07** ("docs: add logo"), 14 commits ahead of v0.2.1 and more than 14 days old |
| Maturity | 5.1k stars, effectively one author (l3yx, 70 commits), last push 2026-09-07 |
| How it runs | The default dispatcher creates per-project worker containers **via the host Docker socket**, which is not acceptable here. Local mode needs no Docker and no API keys in the workers; the README warns that it runs "with your user's permissions and no sandbox", which inside sandbox-ai is exactly the point |
| Workers | Claude Code, Codex, **Pi** (Pi is not a sandbox-ai agent: leave it out or add it later) |
| Telemetry | No analytics found by grep in `cairn/` (only a vendored graph-layout JS) |
| Integration shape | As in the plan: a uv lock with hashes at the pinned commit, `runtime.execution: local`, workers from the team image, server bound to `127.0.0.1:8000` and shown with `--browser`. Ship it unmodified with a link to the source (AGPL §13 applies to modified network use). Keep the pentest preset's egress allow-list to the authorised target only |

---

## Overlap with what sandbox-ai already has

- **Hermes Agent** already covers the "personal assistant gateway" category: messaging platforms, cron, a self-written skills/learning loop, Kanban. OpenClaw, nanobot, ZeroClaw and Moltis are all in this category and add little that a coding-sandbox user lacks. The main exceptions are ZeroClaw's hardware and SOP engine and Moltis' vault/voice.
- **Coding CLIs** (Claude Code, Codex, OpenCode, Qwen Code, DSH): Goose is the one candidate in this space with a distinct value: recipes, a scheduler, built-in local inference and foundation governance. It also has the cleanest telemetry story.
- **Missing categories that candidates fill**: a web control centre plus automations across agents (OpenHands Agent Canvas); persistent self-editing long-term memory (Letta Code); a blackboard multi-agent search (Cairn); full desktop computer use (Agent Zero, though there is a better route via sandbox-ai's own display/browser features).

## Recommendations and integration shapes

### Integrate now

**Goose v1.57.0** as `sandbox-ai goose` (not live-tested yet, like Hermes/Qwen/DSH: no opt-in, the start says so).

- Image: `sandbox/Containerfile.goose` downloads the release tarball `goose-<arch>-unknown-linux-gnu.tar.gz` from the v1.57.0 release and checks the sha256 digests above. Use the non-vulkan build. Put the binary on the read-only root and do not install the desktop app.
- Forced env and settings: `GOOSE_TELEMETRY_OFF=1`, `GOOSE_TELEMETRY_ENABLED: false` in config, `GOOSE_DISABLE_KEYRING=1`, and `GOOSE_PATH_ROOT` under the session HOME, kept per session. Block `us.i.posthog.com` at the proxy as well. Refuse `goose update` (the read-only root can't be updated anyway).
- Features off by default, each switchable through `--goose-enable` (same form as `--hermes-enable`/`--qwen-enable`):
  - `local-inference`: opens huggingface.co for model downloads; otherwise point Goose at `--llm`.
  - `gateway:telegram` and similar: a bot token from a named secret.
  - `schedule`: the cron scheduler, only while the session runs.
  - `acp-serve`: `goose serve` on the pod loopback.
  - Extensions: sandbox-ai's MCP servers mapped into Goose's extension config.
- How the human uses it: a tmux CLI (`goose session`) like the other CLI agents.
- Presets: `coding-goose`, `goose-local-llm` (OpenAI-compatible `--llm` to LM Studio/Ollama/vLLM/llama.cpp), maybe `goose-recipe` (runs a recipe file from the workspace headless).
- Before shipping: a measured host list with upstream defaults and with sandbox-ai's settings, as in `docs/QWEN_CODE_REVIEW.md`.

**Cairn**, as planned: see the Cairn section. Pin commit `8e7e0ea6` rather than v0.2.1.

### Integrate later

1. **OpenHands** (next). Pair Agent Canvas 1.34.0 with Agent Server 1.59.0 (or newer pairs once 14 days old, after checking compatibility).
   - Image: an npm lock (`npm ci --ignore-scripts`) plus a uv lock with hashes for `openhands-agent-server`. Start with `OH_AGENT_SERVER_LOCAL_PATH` (or a preinstalled venv) so that `uvx` never fetches at start.
   - Forced settings: `AGENT_CANVAS_DISABLE_TELEMETRY=1`; block `z.openhands.dev`, `*.posthog.com` and `api.lmnr.ai`. Refuse `OH_CONVERSATION_RUNTIME=docker` and remote backends.
   - Features: the Automation backend off by default (`automations`); Slack/GitHub/Linear integrations per switch.
   - How the human uses it: the **web UI on `127.0.0.1:8000` in the pod, shown with `--browser`**. This needs the Podman backend, because `--browser` is Podman-only.
   - Wait until the restructuring settles (the CLI was deprecated this year; four repos).
2. **Letta Code**: only with the local backend.
   - Image: `npm ci --ignore-scripts` from a lock.
   - Forced env: `LETTA_LOCAL_BACKEND_EXPERIMENTAL=1` / `letta backend local`, `LETTA_CODE_TELEM=0`, `DO_NOT_TRACK=1`, `DISABLE_AUTOUPDATER=1`, `SCARF_ANALYTICS=false`. Close `api.letta.com` unless a `cloud` switch is given. Channels, remote computers and MemFS GitHub sync are separate switches.
   - How the human uses it: a tmux CLI. Preset `letta-local-llm`.
   - Revisit when the local backend is no longer "experimental" and the cadence slows. Today the 14-day rule always pins a version about 30 releases behind.
3. **nanobot** (optional): only if users want a lighter local-LLM personal agent than Hermes.
   - Image: a pinned wheel and uv lock with hashes.
   - Forced settings: the WebUI updater disabled and the `raw.githubusercontent.com` release index closed. `tools.exec.sandbox` none (sandbox-ai is the sandbox; nested bwrap untested). Channels per switch.
   - How the human uses it: a tmux TUI, or the WebUI on `127.0.0.1:8765` via `--browser`.

### Watch or not now

- **ZeroClaw**: watch. It is a good shape (single binary, SHA256SUMS, Apache-2.0), but it is young, overlaps Hermes, and has its own nested sandbox and config-schema churn. Reconsider at 1.0.
- **OpenClaw**: not now. If users ask: `openclaw tui --local` only, plus `update.checkOnStart: false`, `OPENCLAW_NO_AUTO_UPDATE=1`, `telemetry.enabled: false`, plugins/ClawHub/channels/Bonjour closed, and `npm ci --ignore-scripts`. Its 250 MB package and its release pace make the review cost high.
- **Moltis**: not now (one maintainer, web-UI-first, its own sandbox and update checker, overlaps Hermes).
- **Agent Zero**: no. It conflicts with sandbox-ai's model (root appliance with sshd and its own services), its dependency tree is multi-GB, and it self-updates. Computer use is better served by sandbox-ai's own display/browser features with an existing agent.

## Open items (unverified here)

- Host lists for every candidate: none was measured. Each integration needs the logging-proxy run with upstream defaults and with sandbox-ai's settings.
- Goose: model-catalog lookups at start; whether the CLI ever sends the desktop-only onboarding events.
- OpenHands: that Canvas 1.34.0 and Agent Server 1.59.0 work together; MCP configuration details.
- Letta Code: whether the local backend works fully offline against an OpenAI-compatible server; Scarf behaviour with `--ignore-scripts`.
- nanobot: whether the WebUI checks for updates automatically.
- ZeroClaw, Moltis: whether their update/version checks run unasked, and how to turn them off.

## Sources

- Repository metadata, releases, contributors: GitHub REST API (`/repos/{owner}/{repo}`, `/releases`, `/contributors`), 2026-11-13; tags -> commits via `git ls-remote`.
- Packages: npm registry (`openclaw`, `@letta-ai/letta-code`, `@openhands/agent-canvas`), PyPI (`nanobot-ai`, `openhands-agent-server`, `openhands-sdk`, `letta`).
- Source and docs at the pinned tags, for example:
  - OpenClaw `docs/gateway/telemetry.md`, `docs/gateway/sandboxing.md`, `docs/cli/tui.md`, `docs/cli/mcp.md`, `docs/gateway/bonjour.md`, `docs/help/environment.md`, `docs/concepts/model-providers.md`
  - Goose `crates/goose/src/posthog.rs`, `crates/goose-cli/src/cli.rs`
  - OpenHands `src/services/telemetry.ts`, `bin/agent-canvas.mjs`, SDK `openhands-agent-server/openhands/agent_server/config.py`
  - Letta Code `src/telemetry/index.ts`, `src/updater/auto-update.ts`, `src/backend/local/paths.ts`
  - nanobot `nanobot/release_feed.py`, `nanobot/agent/tools/sandbox.py`
  - Moltis `docs/src/sandbox.md`, `docs/src/configuration-reference.md`
  - Agent Zero `docker/base/Dockerfile`, `requirements.txt`, `docs/setup/dev-setup.md`, `docs/guides/self-update.md`
  - Cairn `README.md`, `dispatch.local.example.yaml`, `docs/specs/dispatcher-design.md`
- OpenHands-CLI deprecation: https://github.com/OpenHands/OpenHands-CLI (README "Project status").
- OpenClaw security: [Unit 42](https://unit42.paloaltonetworks.com/openclaw-ai-supply-chain-risk/), [Dark Reading](https://www.darkreading.com/cyber-risk/malicious-openclaw-skills-clawhub-threaten-ai-supply-chain), [Immersive Labs](https://www.immersivelabs.com/resources/c7-blog/openclaw-what-you-need-to-know-before-it-claws-its-way-into-your-organization). The vendor blogs' counts vary and are not relied on.
- ZeroClaw reviews: [GitHub Discussion #2867](https://github.com/zeroclaw-labs/zeroclaw/discussions/2867), [eesel.ai review](https://www.eesel.ai/blog/zeroclaw-review) (secondary).
