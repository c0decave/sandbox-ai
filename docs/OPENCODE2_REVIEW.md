# OpenCode 2: review for sandbox-ai

OpenCode 2 (anomalyco/opencode, MIT; the makers sell OpenCode Console, Zen and Go) as a sandbox-ai agent,
`sandbox-ai opencode2`, beside OpenCode 1 (`sandbox-ai opencode`). This review covers the release built into
`localhost/opencode2-sandbox` (sandbox/Containerfile.opencode2): what OpenCode 2 is, why it is an agent of its own,
every host it contacts by default, its telemetry, its updates, its background service, plugins, MCP, the Console,
remote pairing, its own sandbox, and the switch for each. Reviewed on 2026-11-15.

## What OpenCode 2 is

OpenCode 2 is the next major line of OpenCode, from the same repository and makers, published as a **new npm package**:

| | OpenCode 1 (`sandbox-ai opencode`) | OpenCode 2 (`sandbox-ai opencode2`) |
|---|---|---|
| npm | `opencode-ai` (`latest` = 1.19.5, 2026-10-28; `latest-1` dist-tag) | `@opencode/cli` (`latest` = 2.3.0, 2026-11-13); the binary in `@opencode/cli-<os>-<arch>` |
| First release | 1.0.0 2025-10-31 | 2.0.0 2026-09-11 (npm); GitHub releases from v2.2.0 (2026-10-27) |
| Upstream status | maintenance: 1.19.x bug fixes after 2.x went out (1.19.5 the day after v2.2.0); 1.19.0 added `opencode upgrade --major` "to upgrade to OpenCode 2" | the main line: the README installs `@opencode/cli`, opencode.ai/docs is 2.x (1.x moved to /v1/docs) |
| Command | `opencode` | `opencode` (and `opencode2`: the package's second bin name, same binary) |
| Program | Bun-compiled single binary, one process | Bun-compiled single binary; a CLI client plus a **shared background server** per user (`opencode serve --service`) that owns sessions, config, permissions and tools |

The three intentional breaking changes (upstream's migration guide, opencode.ai/docs/migrate-v1): a new plugin API (V1
plugins do not run in V2), a new server API and clients, and the terminal client's settings moved from `tui.json` to
one global `cli.json`. Beyond those V2 reads the V1 configuration (same files, same `OPENCODE_CONFIG`,
`OPENCODE_CONFIG_CONTENT`, V1 field shapes normalised in memory) next to its own native shape (`permissions` as an
ordered rule list, `agents`, `providers`, `mcp.servers`, `policies`, `update`). Other changes that matter to a sandbox
(measured or read below): `external_directory` is allowed by default (V1 asked), LSP is no longer run (no language
server downloads), `CLAUDE.md` is no longer read as a fallback, `OPENCODE_PERMISSION` is gone, the TUI has no
`--model` option, MCP tools sit behind Code Mode (`execute`) unless a server sets `codemode: false`, and session
sharing (V1's `share`, opncd.ai) is gone (only a `share_url` column of migrated V1 sessions remains).

### Decision: its own agent, `sandbox-ai opencode2`

The user asked for OpenCode 2.x "as a harness as well" (*ebenfalls*). The evidence says a separate agent, not an
upgrade of `sandbox-ai opencode`:

- Different package and release line: `opencode-ai` still ships 1.x fixes (1.19.5 after 2.2.0); `@opencode/cli` is
  2.x. sandbox-ai's `opencode` image installs `opencode-ai`; switching that silently to another package would change
  what every existing `sandbox-ai opencode` user gets.
- sandbox-ai's OpenCode 1 integration depends on what V2 broke: the rule hooks and the kits' OpenCode plugins are V1
  plugins (they do not run in V2), `-D` is `OPENCODE_PERMISSION` (gone in V2), `--model` is the TUI's `-m` (gone in
  V2), the login store keeps `auth.json` (V2 keeps credentials in its SQLite database and only imports `auth.json`
  once), and the team image (`--with opencode`) holds OpenCode 1.
- V1 and V2 share their configuration and data folders (`~/.config/opencode`, `~/.local/share/opencode`), and upstream
  warns not to point V1 at files converted to V2's native shape. As its own agent OpenCode 2 has its own HOMEs
  (sessions, `resume`), so a V2 start never rewrites what a V1 start reads.
- Upstream itself keeps a second command name, `opencode2`, for running both.

So `sandbox-ai opencode` stays OpenCode 1 exactly as before, and `sandbox-ai opencode2` is OpenCode 2 with an image,
module (sandbox/opencode2.py), switches and tests of its own, in the form of the newest integrations (Letta Code,
nanobot, OpenHands).

## The release

| | |
|---|---|
| Version | `2.2.5`: npm `@opencode/cli@2.2.5` 2026-10-30 23:14 UTC, its binaries `@opencode/cli-linux-x64@2.2.5` 23:14 UTC, `-linux-x64-baseline` 23:19 UTC, `-linux-arm64` 23:13 UTC; GitHub release `v2.2.5` 2026-10-30 23:17 UTC; 15 days before the review |
| Commit | `9bbb8ce18c80f099e0e448775f59d965ead40a21` (GitHub's API: `refs/tags/v2.2.5`; the npm packages carry no `gitHead`) |
| Why this one | the newest release at least 14 days old (sandbox-ai's rule for third-party releases). Newer: 2.2.6 (2026-11-06 23:33 UTC, eligible from **2026-11-20**), 2.2.7 (2026-11-10, from 2026-11-24), 2.3.0 (2026-11-13, from 2026-11-27). 2.2.6 fixes the one functional gap found here (MCP tools in `opencode run`, below): the pin moves to it once it is eligible, after the same measurements |
| Packages | `@opencode/cli` is a launcher: no dependencies, twelve optional platform packages and a `postinstall` script that copies the platform's binary over a stub (and, if that fails, runs `npm install` itself). sandbox-ai uses neither: the image fetches only the platform package's tarball from registry.npmjs.org and checks it against the registry's sha512 integrity in `Containerfile.opencode2.lock` (x64 `sha512-BkHAfTqN...`, x64 baseline `sha512-oPOT+EMm...`, arm64 `sha512-O4qOy/WR...`) before unpacking the one binary. Each package has an SLSA v1 provenance attestation on the registry (read here, its Sigstore signature not verified here) |
| Install | `sandbox/Containerfile.opencode2`: the binary of the build machine's architecture (on x64 the baseline build when the CPU has no AVX2, as upstream's postinstall chooses), installed read-only as `/usr/local/bin/opencode` with `opencode2` beside it. No install script runs, nothing is piped into a shell, no `npm install` at build or run time |
| Size | about 250 MB (one Bun-compiled binary) |
| Licence | MIT (GitHub's licence API, the packages' `license`) |

Verified here: the dates, the commit and the integrity (npm's registry, GitHub's API; the x64 tarball downloaded and
its sha512 recomputed), the binary (`--version`: `opencode v2.2.5`, `--help` of every subcommand, every string naming a
host or a variable), and runs of it against a local fake OpenAI-compatible model on the loopback with every
connection sent to a logging proxy that refuses all of them, and every socket of the processes watched for anything
that bypassed the proxy (none did): `opencode run` and the TUI (in tmux, a message typed, 75 s), with upstream's
defaults and with sandbox-ai's settings, an MCP server (stdio, sandbox-ai's demo server), `debug config` (which
configuration layer wins), `plugin add`, `upgrade`, `pair --remote`, `session import URL`, `stats`, `models`,
`auth list`. Not verified here: the image build (no Podman in this sandbox: it builds in CI or on the host) and a
start in a real sandbox; hence OpenCode 2 ships as not live-tested yet (the start says so in one line; no opt-in).

## Hosts contacted by default (measured)

| Run | Upstream's defaults | sandbox-ai's settings |
|---|---|---|
| `opencode run "..."` | `models.opencode.ai` (3x: `/experimental/v2.0/api.json`, the models.dev catalog), the model | the model only |
| interactive (`opencode`, a message, 75 s) | `models.opencode.ai`, `opencode.ai` (the update check: `/update/api/...`, from the CLI), the model | the model only |
| no provider configured | the free models of OpenCode Zen on `opencode.ai` (`opencode/big-pickle`, `flint-40b-free` and the like): the prompt goes to the maker's service | the same (opencode.ai is OpenCode's own provider, open as for OpenCode 1); with `--llm` or `--no-provider-defaults` it is closed |
| a project's `opencode.json` naming a model that does not exist | falls back to the free models on `opencode.ai` (measured) | `--llm`'s model is set in the highest layer (`OPENCODE_CONFIG_CONTENT`), which no project file overrides |
| `opencode upgrade` | `opencode.ai/update/api`, then npm, brew or the curl installer | refused (the install is read-only; a new OpenCode 2 is a new review and `sandbox-ai build opencode2`) |
| `opencode plugin add|update|check`, plugins in a configuration | `registry.npmjs.org` (and Git hosts for `github:` specs; the background install of configured plugins at every start) | refused unless `--opencode2-enable plugins`; npm closed |
| `opencode pair --remote` | `opentunnel.xyz` (`POST /api/tunnel`): the server is exposed to the internet | refused always; `opentunnel.xyz` never opened and refused by the proxy |
| `opencode session import URL`, `references` with a `repository`, `skills`/`instructions` URLs | that URL, GitHub | the sandbox's allow-list decides, as for every tool |

The background service: by default every `opencode` command finds or starts one shared server for the user
(`opencode serve --service` on 127.0.0.1 with a random port and password, registered in
`~/.local/state/opencode/service.json`) that outlives the command (measured: still running after `opencode run`
returned). In a sandbox that is a process nobody started on purpose. sandbox-ai writes `{"disabled": true}` into the
sandbox's `~/.config/opencode/service.json` (its documented switch, `opencode service set disabled true`), so every
command runs with a private server of its own that ends with it (measured: nothing left running, no state file).

## How sandbox-ai configures OpenCode 2

OpenCode 2 merges its configuration from the system folder `/etc/opencode`, the user's `~/.config/opencode`,
`OPENCODE_CONFIG` and the project's files; `OPENCODE_CONFIG_CONTENT` comes last and wins over all of them (measured with
`opencode debug config`). sandbox-ai sets (sandbox/opencode2.py: `BASE_ENV`, `TELEMETRY_ENV`, `config_content`):

- `OPENCODE_DISABLE_AUTOUPDATE=1` and `"update": "disable"`, `"autoupdate": false`: no update check (measured: no call
  to opencode.ai from the CLI). The proxy refuses `opencode.ai/update/**` too, unless `--opencode2-enable updates`.
- `OPENCODE_DISABLE_MODELS_FETCH=1`: the catalog bundled in the binary instead of models.opencode.ai (measured: the
  bundled catalog lists the built-in providers and models).
- `OPENCODE_CONFIG_CONTENT`: the `--mcp` servers (native `mcp.servers`, each the launcher's stdio bridge with
  `codemode: false`, so their tools are the model's direct tools as with every other agent; its timeouts), `-D`
  (below), `--model` (OpenCode 2's TUI has no model option: the configuration's `model`), the update switches.
- `~/.config/opencode/service.json`: `"disabled": true` (above).
- `-D` is `"permissions": [{"action": "*", "resource": "*", "effect": "allow"}]` in the highest layer: no questions
  (upstream's `--auto` does the same per command, but not for `mini` and not before a subcommand). Without `-D`
  OpenCode 2's own rules apply (it asks before what it asks before).
- `--llm NAME[:MODEL]`: the same OpenAI-compatible provider as OpenCode 1's, read-only in sandbox-ai's directory
  for it (`/etc/opencode2/sandbox-ai-llm.json`, read through `OPENCODE_CONFIG`; V2 reads the V1 shape, measured), its model also in the highest layer, the loop
  guard's steps, compaction and instructions; `compaction.prune` is left out (V2 ignores it with a warning).
- `--api-key HOST=VARIABLE` (Anthropic, OpenAI, OpenRouter, ...): OpenCode 2 reads the providers' variables from its
  server process ("Environment" connections), which with the service off is the agent's own process; the proxy adds
  the key on the wire.

## Every feature: upstream, sandbox-ai, and the switch

"Closed" means: the host is not on the sandbox's allow-list, so the firewall and the connection proxy refuse it even
if OpenCode 2 tries. A switch is a feature of `--opencode2-enable FEATURE[,FEATURE...]` (`-oe`): each opens only its
own hosts and shows in the dry run's `"opencode2"`.

| Feature | Upstream default | Default in sandbox-ai | Switch to turn it on |
|---|---|---|---|
| Telemetry, analytics, crash reports | none found: no analytics client in the binary (no PostHog, Sentry, Segment, Mixpanel, Amplitude; `posthog` only names PostHog's MCP server for OAuth), OpenTelemetry only with an `OTEL_EXPORTER_OTLP_*` endpoint of yours; Bun's crash reporter (`bun.report`, `BUN_ENABLE_CRASH_REPORTING`) | **always off, not switchable** (see Telemetry) | none |
| Update check (`update`, `opencode upgrade`) | **notify**: at every interactive start (`opencode.ai/update/api`) | off (`OPENCODE_DISABLE_AUTOUPDATE=1`, `update: disable`), `opencode.ai/update/**` refused by the proxy, `opencode upgrade`/`update`/`uninstall` refused | `updates`: the check and its notice (`update: notify`); installing is refused either way |
| Model catalog (models.dev data from `models.opencode.ai`) | at every start | off (`OPENCODE_DISABLE_MODELS_FETCH=1`): the bundled catalog | `models-catalog` (models.opencode.ai) |
| Plugins from npm and Git (`plugins`, `opencode plugin add|update|check`; configured plugins installed in the background at start) | on demand | closed (registry.npmjs.org), `opencode plugin add|update|check` refused; local plugins in `.opencode/plugins/` load (they are files of the project) | `plugins` (registry.npmjs.org) |
| Web search (Exa, Firecrawl, Parallel, Tavily, TinyFish; asks the first time) | on demand, with a provider's key | closed | `web-search` (mcp.exa.ai, mcp.firecrawl.dev, search.parallel.ai, api.tavily.com, agent.tinyfish.ai) with `--api-key` for a provider that needs one |
| OpenCode Zen / Console / Go (the maker's models, the `opencode` provider, `/connect`) | open | open (`opencode.ai`, the provider host as for OpenCode 1); closed with `--llm` or `--no-provider-defaults` | - |
| Background service shared per user | on | off (`service.json` `disabled`); `opencode serve` on the sandbox's loopback only | - |
| Remote pairing (`opencode pair --remote`: OpenTunnel) | on demand | refused; `opentunnel.xyz` never opened | none |
| `opencode serve` / `pair` (local) | loopback | loopback only; `--hostname` off the loopback refused | - |
| LSP | not run in V2 | - | - |
| ripgrep, tree-sitter grammars, Bun (downloaded from GitHub when absent or needed) | when needed | `ripgrep` in the image; GitHub closed (a grammar for an exotic language's highlighting is missing) | `--allow-github` |
| Claude Code as a harness (`OPENCODE_CLAUDE_PATH`) | when installed | not in the image | - |
| Code Mode (`execute`: model-written JavaScript in an in-process VM, `fetch` with the webfetch rules) | on | on (inside the sandbox; its `fetch` meets the allow-list); sandbox-ai's `--mcp` servers are direct tools (`codemode: false`) | - |
| Its own sandbox | none (no bwrap, Seatbelt or container of its own; `bwrap` only names Claude Code's settings) | - | - |
| Docker | none | - | - |

### Telemetry: always off, never switchable

The user's rule: no telemetry, analytics, crash reporting or usage pings, and no switch that turns them on.

Also from inside the sandbox (sandbox/telemetry_guard.py, since 0.1.97): `opencode2` and the image's `opencode` (the
same binary) are shims of sandbox-ai's telemetry guard, which refuses an OpenTelemetry/OTLP variable of its own,
`BUN_ENABLE_CRASH_REPORTING` set to anything but `0` and `OPENCODE_TEST_MANAGED_CONFIG_DIR` with one message (exit
status 2) and pins the opt-outs before it becomes the binary (`/usr/local/lib/sandbox-ai/real/opencode2`).

What the review found: OpenCode 2.2.5 sends no usage telemetry (measured: nothing but the catalog, the update check and
the model, by default). The binary carries OpenTelemetry's SDK, which exports only to an `OTEL_EXPORTER_OTLP_*`
endpoint set by the user, and Bun's crash handler, which can send a crash report to `bun.report`.

What sandbox-ai does in every OpenCode 2 run, whatever else is enabled (sandbox/opencode2.py: `TELEMETRY_ENV`,
`TELEMETRY_HOSTS`, `telemetry_rules`, `check_telemetry`):

- the variables are set, after `--env` (they win), and no feature drops them: `DO_NOT_TRACK=1`,
  `BUN_ENABLE_CRASH_REPORTING=0`, `OTEL_SDK_DISABLED=true`, `OTEL_TRACES_EXPORTER=none`,
  `OTEL_METRICS_EXPORTER=none`, `OTEL_LOGS_EXPORTER=none`;
- `bun.report` is never opened and refused by the proxy; a start that would open it (`--allow-host`, a wildcard over
  it), hide it from the proxy (`--no-inspect`) or go out without the proxy (`--egress-all-allow` with `--no-proxy`) is
  refused; `--telemetry on` does not change any of it;
- the dry run's `"opencode2"` shows `"telemetry": {"switchable": false, ...}`.

### What runs when OpenCode 2 starts

`opencode2` (the image's binary): the CLI reads `cli.json` (created on the first start), starts its private server
(the service is off), which reads the configuration layers, opens the SQLite database
(`~/.local/share/opencode/opencode.db`: sessions, saved provider accounts), starts a file watcher, the `--mcp`
servers, the configured plugins (the bundled ones; npm closed), and Git snapshots of the project for undo
(`~/.local/share/opencode/snapshot`). Then the TUI, or `run`'s one prompt.

### Providers and login

- **API keys**: `--api-key api.anthropic.com=ANTHROPIC_API_KEY` and the like: OpenCode 2 reads them from its server's
  environment; the proxy adds the key on the wire.
- **`--llm`**: above. `--model provider/model` sets the model.
- **No login sandbox-ai takes in**: OpenCode 2 keeps saved accounts in its SQLite database; `/connect` in the sandbox
  saves them in the sandbox's HOME (kept with `sandbox-ai resume`). OpenCode 1's login (`sandbox-ai login opencode`)
  is not copied in.

### Known gap in 2.2.5

`opencode run` (the helper's form and one-shot starts) sends its first model call before the `--mcp` servers have
connected (measured three times: the server connected 6 ms after the call), so a one-shot task does not see their
tools; the TUI does (measured: the demo server's `demo_version` called and answered). 2.2.6 waits up to two seconds
for connecting servers before a model call (its release notes); it becomes eligible on 2026-11-20.

## Rules for its own tools and kit hooks (2026-11-16)

OpenCode 2.2.5's plugin API V2 (`packages/plugin/src/promise`): a plugin is `{id, setup}`; `ctx.tool.hook
("execute.before", fn)` runs before every tool call - also each call of a Code Mode script - and a rejection refuses
that call alone; OpenCode 2 then runs `event.tool` with `event.input` as the hooks left them (`core/src/tool.ts`). A
configured local plugin must be a directory (`config/plugin/source.ts`).

- sandbox-ai's plugin directory `/etc/opencode2/sandbox-ai-hooks` (read-only: `package.json`, `server.js`,
  `config.json`) is named in `OPENCODE_CONFIG_CONTENT`'s `plugins` (the layer above every file). Its `execute.before`
  asks the supervisor (refused with the `sandbox-ai:` reason) and freezes the checked event, so a plugin loaded or
  hot-reloaded after it cannot change the call (its write throws and the call fails); `execute.after` records.
- Approved kit hooks are V2 plugin modules (`.js`, `.mjs`) that sandbox-ai's plugin hosts after its check, then
  checks again what one of them changed.
- Tools mapped: `shell` Bash, `read`/`write`/`edit` (`path` `file_path`, `oldString` `old_string`), `glob`, `grep`,
  `webfetch`, `websearch`, `patch` apply_patch (`patchText` `command`), `subagent` Task. The mode paranoid starts.
- Not covered: should the plugin fail to load, OpenCode 2 logs it and runs without (to be shown by the live test).

## Plugins from kits (2026-11-16)

What 2.2.5 does with a configured local plugin (read in its source): `config/plugin/source.ts` turns each entry of
`plugins` into an operation (a path must be a directory; `-ID` removes one by its id); `plugin/host.ts` resolves the
directory's module - through `package.json` `exports` when the package names itself and maps them, else `server`,
else `index`, with Bun's extensions; `plugin/supervisor.ts` activates them in the order of the operations after the
built-ins, keeps the first of two with one id; `plugin.ts` gives each its own activation and switches a whole
activation off when one of its transforms (`skill.transform`, ...) fails. The module's `setup(ctx)` gets the whole
plugin context in OpenCode 2's own process: tools (and replacing them), tool hooks, skills, sessions and their context
hook, MCP servers (`mcp.transform`), the shell.

sandbox-ai therefore gives a kit's plugin (`opencode2/marketplaces.json`, a plugin of a marketplace in Claude's
format installed into sandbox-ai's one plugin store) only once the user approved the whole plugin folder
(`sandbox-ai plugins opencode2 trust-hooks PLUGIN`: the module and every file it imports shown in full, the SHA-256 of
every file recorded; any change takes the approval away). An approved plugin is mounted read-only at
`/sandbox/plugins/MARKETPLACE/NAME` and named in `OPENCODE_CONFIG_CONTENT` (the last layer) after
`/etc/opencode2/sandbox-ai-hooks`: the rule plugin's `execute.before` runs first and freezes the checked call, and
a plugin that fails is switched off alone - kit plugins are never hosted inside the rule plugin, whose activation a
failing transform of theirs would switch off with it. Upstream's Superpowers 7.0.1 (its root `index.js` re-exports
`.opencode/plugins/superpowers.js`: skills via `ctx.skill.transform`, the bootstrap via `ctx.session.hook("context")`)
is the first one (the built-in kit `superpowers-plugin`). Needs a live test: the plugin loading after the rule plugin
from the read-only mount, its skills and bootstrap, a refused call still refused.

## Go / No-Go

**Go, not live-tested yet** (choosing it starts it; the start says so in one line).

- With its update check and catalog download off and the shared service disabled, OpenCode 2 contacts nothing but its
  model (measured, headless and interactive, every socket watched).
- No telemetry upstream; the crash reporter and OpenTelemetry are pinned off by variables no option drops, and their
  host is refused by the proxy.
- The binary is fetched from npm's registry and checked against its sha512 integrity; no install script; no
  self-update in the sandbox (read-only install, update check off, `upgrade` refused).
- Against: a very young major line with a fast release cadence (2.0.0 to 2.3.0 in nine weeks; the 14-day rule pins
  three releases behind); the one-shot MCP gap of 2.2.5; plugins and Code Mode run code inside the sandbox (as the
  agent); not live-tested in a sandbox yet. Hence not live-tested yet, and the review is repeated for every release
  taken in.
