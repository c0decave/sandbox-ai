# DeepSeek Harness: review for sandbox-ai

DeepSeek Harness (`dsh`, deepseek-ai/deepseek-harness, MIT, Node.js/TypeScript on the Cordis plugin framework) as a
sandbox-ai agent, `sandbox-ai dsh`. This review covers the release built into `localhost/dsh-sandbox`
(sandbox/Containerfile.dsh): which repository is the official one, every host DSH contacts by default, its
telemetry, its plugins, its own sandbox, and the switch for each. Reviewed on 2026-11-13.

## Which repository is DeepSeek's

| Check | Result |
|---|---|
| GitHub organisation | `deepseek-ai` (organisation id 148330874), the home of DeepSeek-V3, DeepSeek-R1, DeepEP, 3FS ...; the repository `deepseek-ai/deepseek-harness` is no fork, created 2026-08-13 11:56 UTC, licence MIT (GitHub's licence API), homepage `https://deepseek.com/harness`, README "developed by DeepSeek AI" |
| Companion repositories in the same organisation | `dsh-libreoffice-kit`, `dsh-node-addon-require-builtin` ("an internal component used by DeepSeek Harness"), `dsh-superagent-prototype` |
| npm | `@deepseek-ai/dsh`, `repository: git+https://github.com/deepseek-ai/deepseek-harness.git` (directory `apps/cli`); maintainers `imccyu` and `tianyicui-deepseek` (`tianyi@deepseek.com`) |
| Look-alikes, not taken | DeepSeek-TUI, deepcode-cli and other community CLIs: not in the `deepseek-ai` organisation, not DeepSeek's |

## The release

| | |
|---|---|
| Version | `0.2.1-rc.5`: tag `dsh-v0.2.1-rc.5` (a GitHub *pre-release*, published 2026-10-30 02:12 UTC), npm `@deepseek-ai/dsh@0.2.1-rc.5` published 2026-10-29 14:15 UTC, 14 to 15 days before the review |
| Commit | `b830ec950faba0a46dda564be3e2d854afed94f1` (`git ls-remote`: `refs/tags/dsh-v0.2.1-rc.5`, a lightweight tag) |
| Package | `dsh-0.2.1-rc.5.tgz`, integrity `sha512-WkyJZv4zl9sCAxSzRctGm58m0rilGqwR6pniBiLOz5CPDfHfDuAUicg0AAazXI7oj/Cph6/cjNTncA/rpLTXpw==`; the launcher only: its ~70 `@deepseek-ai/dsh-*` plugin packages are dependencies with exact versions |
| Why this one | DSH has published only pre-releases so far ("developer preview ... THERE WILL BE COMPATIBILITY-BREAKING CHANGES"); 0.2.1-rc.5 is the newest at least 14 days old (sandbox-ai's rule). Newer: 0.2.2-alpha.3 (2026-11-03), 0.2.2-rc.1 (2026-11-04, npm `latest`), 0.2.2-rc.2, 0.2.2-rc.3 |
| Dependencies | `sandbox/Containerfile.dsh.lock`: an npm lock of 687 packages made by `scripts/agent_npm_lock.py dsh` with `npm install --package-lock-only --before=2026-10-30` (every version older than 14 days), each with its sha512 integrity; the image installs it with `npm ci --ignore-scripts`. Five packages have install scripts, none is needed: node-pty and koffi ship prebuilt Linux binaries, `dsh-subprocess-local` only restores an executable bit of a macOS helper, protobufjs and @google/genai only print notices |

Verified here: the organisation and repository (GitHub's API), the dates and the commit (npm registry, GitHub's API,
`git ls-remote`), the lock (installed with `npm ci --ignore-scripts`), `dsh --version`, `dsh headless --help`, a
one-shot `dsh headless` against a local fake model with sandbox-ai's patch (its bash tool ran a command, in the
default `workspace-write` mode with DSH's own Landlock confinement and in `danger-full-access`), and `dsh web`
starting on loopback - every other connection sent to a logging proxy that refuses it. Not verified here: the image
build (no Podman here: it builds in CI or on the host) and a start in a real sandbox; hence not live-tested yet
(the start says so in one line; no opt-in).

## What DSH is

A launcher (`dsh`) that boots a *profile*: an ordered stack of plugin bundles (`dsh-base`, `dsh-web-app`,
`dsh-headless`, `dsh-sdk-app`, `dsh-acp-app`), then the profile's `cordis.patch.yml`, the home's
`$DSH_HOME/cordis.patch.yml`, and last the `--patch FILE` overlays. Shipped profiles: `web` (a browser UI on
`127.0.0.1:3080` with a token in its URL), `headless` (one task, answer on stdout), `acp`, `sdk`, `sdk-minimal`.
There is no terminal UI in this release. Its home is `~/.dsh` (sessions, profiles, credentials), its shared agents
root `~/.agents` (skills); instructions from `~/.dsh/AGENTS.md` and the project's `AGENTS.md`/`CLAUDE.md`.

`sandbox-ai dsh` starts `dsh web --no-open --host 127.0.0.1 --port 3080` in the session's tmux window: the UI is
reached from the sandbox's own browser (`--browser`, the app's Browser button: it shares the pod's loopback), never
from outside the sandbox. `sandbox-ai dsh -- headless "task"` runs one task.

## How sandbox-ai configures DSH

DSH's last configuration layer is `--patch FILE`. sandbox-ai writes `sandbox-ai.patch.yml` (JSON, which YAML reads)
for each start into the session directory, mounts it read-only at `/etc/dsh`, and gives it to the launcher right
after the profile name (sandbox/dsh.py: `BASE_PATCH`, `TELEMETRY_PINNED`). The user's own profile and home patches
stay theirs; this one is applied after them.

## Hosts contacted by default (measured)

| Run | Upstream's defaults | sandbox-ai's patch |
|---|---|---|
| `dsh headless "say hi"` (an OpenAI-compatible route on loopback) | the model only | the model only |
| `dsh web --no-open`, 60 s, no browser attached | nothing | nothing |

## Every feature: upstream, sandbox-ai, and the switch

DSH v0.2.1-rc.5 reaches out with nothing but its model provider and, after a user's explicit feedback, its session
telemetry. So there is no `--dsh-enable`: nothing else to turn on (Hermes' `--hermes-enable` exists because Hermes has
such features). Telemetry is never switchable.

| Feature | Upstream default | Default in sandbox-ai | Switch |
|---|---|---|---|
| Session telemetry (`dsh-session-telemetry-otel`: a prefix of the session log, with the home's anonymous user id, to `https://dsh-otel-collector.deepseeksvc.com/v1/logs` after the user gives feedback: `/feedback`, a message's thumbs) | on, mode `FEEDBACK_ONLY` | **always off, not switchable** (see Telemetry) | none |
| Product analytics (`dsh-host-product-telemetry-otel`, `dsh-client-product-analytics`, same collector) | Desktop profile only (mounted when the profile is `desktop`, which the npm CLI refuses) | **always off**: the collector is refused, the profile cannot be `desktop` | none |
| DeepSeek API (`api.deepseek.com`: chat, its web search tool) and the DeepSeek platform account (`platform.deepseek.com`: sign-in in the web UI, balance) | used when configured | open as DSH's provider; a key with `--api-key api.deepseek.com=DEEPSEEK_API_KEY` (placeholder in the sandbox, the key on the wire), or the account sign-in in the web UI through the sandbox's browser | on; `--no-provider-defaults` closes them |
| Other providers (`dsh-llm-pi-ai`: OpenAI, Anthropic, OpenRouter, any OpenAI-compatible gateway) | dormant until configured | `--llm NAME[:MODEL]`: a route `sandbox-ai` (OpenAI chat completions at the server's `/v1`, its model and context window, the key from `SANDBOX_AI_LLM_KEY` - a placeholder) as the default model, with the relay's per-model parameters and loop guard on the wire, the guard's instructions in the system prompt; `--api-key HOST=NAME` for the others | `--llm`, `--api-key` |
| Web fetch tool (anonymous fetch of public URLs) | on | on; every site needs `--allow-host` or an approval | `--allow-host` |
| Plugins (`dsh plugin --profile P add PKG`, the web UI's plugin manager: pnpm from registry.npmjs.org or registry.npmmirror.com; the UI pings both) | on demand | not offered: the image has no pnpm and both registries are closed; a plugin is code inside the agent's process, so a new one is a review and a new image | none |
| Its own confinement of commands (`dsh-sandbox-local`: bwrap, else Landlock; `DSH_PERMISSION_MODE` read-only, workspace-write, danger-full-access) | workspace-write with approvals | the same, inside sandbox-ai's sandbox (Landlock works there; there is no bwrap in the image); `--dangerously-skip-permissions` sets `danger-full-access` (no approvals, no inner confinement) | `-D` |
| Optional plugins outside the shipped profiles (SSH executor, browser use, computer use, speech models from huggingface.co, GitHub webhooks) | not mounted | not mounted; the SSH executor would run commands outside the sandbox | none |
| Web UI and its API (`127.0.0.1:3080`, a token per start; `--host` refuses wildcard binds) | on in `web` | on the sandbox's loopback only: reached from the sandbox's browser, not from the host | - |
| HMR, config editor, settings (rewrite the profile's patch) | on | on, in the start's HOME; sandbox-ai's patch is read-only and applied after them | - |
| MCP servers (`dsh-mcp-client`, stdio or streamable HTTP) | none | `--mcp` servers through the launcher's stdio bridge (one patch row each) | `--mcp` |
| Skills, sub-agents, workflows, scheduled jobs, plan mode | on | inside the sandbox; the kits' skills read-only in `~/.agents/skills`, the sandbox-ai skill in `~/.dsh/skills` | - |

### Telemetry: always off, never switchable

What the review found: DSH has one telemetry of its own in the shipped profiles, the session telemetry (above): it
releases a session-log prefix only after explicit feedback, but then sends it, with an anonymous user id, to
DeepSeek's collector. Its opt-out is `DSH_TELEMETRY_DISABLED` (any non-empty value; the launcher then patches the row
disabled, `packages/boot/app-boot/src/profile-context.ts`); `DSH_TELEMETRY_OTLP_URL` redirects it. The product
analytics is the Desktop's. The sources name no Sentry, PostHog, Segment, Mixpanel, Amplitude or Google Analytics
host; the collector is the only telemetry host.

What sandbox-ai does in every DSH run (sandbox/dsh.py: `TELEMETRY_PINNED`, `TELEMETRY_ENV`, `TELEMETRY_HOSTS`):

- the patch disables the rows `session-telemetry-otel` and `otel` (the shared OTel service), laid over everything;
- the variables: `DSH_TELEMETRY_DISABLED=1`, `DSH_TELEMETRY_OTLP_URL=http://127.0.0.1:9/v1/logs` (a closed port, should
  a row ever be enabled again), `OTEL_SDK_DISABLED=true`, `DO_NOT_TRACK=1` - for every `dsh` in the sandbox, also one
  the agent starts itself without the patch;
- `dsh-otel-collector.deepseeksvc.com` is never opened (`--allow-host`, a wildcard, `--no-inspect`, `--egress-all-allow`
  without the proxy are refused) and the proxy refuses it in every DSH run (rules `telemetry-dsh-*`);
- the dry run's `"dsh"` shows `"telemetry": {"switchable": false, ...}`.

Tests: tests/test_dsh.py and the feature matrix's `dsh-telemetry-is-never-switchable`.

### Not (yet) for DSH

- `--with` (not in the team image): `--helper` both ways (`sandbox-ai claude --helper dsh` runs `dsh headless` with
  the task on standard input; `continue` starts a new task: headless resumes only a named session).
- `--effort` (reasoning efforts are per model route in DSH), rules for its own tools and kit hooks (refused, so not
  the mode paranoid), teams, the activity feed, `mcp attach --live`.
- A login of its own in `sandbox-ai login`: the DeepSeek account sign-in is the web UI's (OAuth to its own loopback
  server); use it in the sandbox's browser, or an API key.

## Go / No-Go

**Go, not live-tested yet** (choosing it starts it; the start says so in one line).

- The official repository is verified (DeepSeek's organisation, its npm scope and maintainer); MIT.
- DSH reaches nothing by default beyond its model, and its one telemetry has a hard opt-out (variable and patch) that
  sandbox-ai pins, plus the proxy's refusal.
- Every layer of its configuration can be overlaid by a read-only patch given on the command line.
- Its dependencies install hash-locked without install scripts; its commands run inside sandbox-ai's sandbox (with
  DSH's own Landlock confinement on top by default).
- Against: a *developer preview* with only pre-releases and announced breaking changes; the interactive surface is a
  web UI (no terminal UI), so `sandbox-ai dsh` needs `--browser` to be used interactively; 687 packages; plugins are
  code in the agent's process; not live-tested in a sandbox. Hence experimental; each release taken in is reviewed
  again.
