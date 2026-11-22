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
There is no terminal UI in this release (checked in the npm tarballs at 0.2.1-rc.5, each against its integrity
in the lock, on 2026-11-14): the shipped profiles are `PROFILE_TEMPLATES` of `@deepseek-ai/dsh-app-boot` - `acp`,
`web`, `headless`, `sdk`, `sdk-minimal` -; the launcher's README names a `tui` profile only as an example of a
custom one ("assuming the tui profile is installed"), and no package of the 687 in the lock provides one
(`dsh-terminal` and `dsh-experimental-terminal-bundle` are the agent's persistent shell sessions, not a chat).
`headless` runs one task per call ("no interactive follow-up"; `--session-id` continues a conversation), `acp` and
`sdk` speak JSON-RPC on stdio to a client. Its home is `~/.dsh` (sessions, profiles, credentials), its shared agents
root `~/.agents` (skills); instructions from `~/.dsh/AGENTS.md` and the project's `AGENTS.md`/`CLAUDE.md`.

`sandbox-ai dsh` (since 0.1.96) starts sandbox-ai's chat in the session's tmux window (`sandbox/dsh_chat.py`,
read-only at `/etc/dsh/chat.py`): a line-based REPL that runs `dsh headless --patch /etc/dsh/sandbox-ai-chat.patch.yml
--patch /etc/dsh/sandbox-ai.patch.yml --json [--session-id ID] -` once per message, the message on standard input.
Checked in `@deepseek-ai/dsh-headless` 0.2.1-rc.5 (its tarball against the lock's integrity): `--session-id` adopts
the persisted Session with that id and refuses an unknown one, one recorded in another working directory, a subagent
or forked one, or one under an agent preset; `--json` writes `session` (the id) first, then `status`, `thinking`,
`text`, `tool_call`, `tool_result` as each step commits (not per token), and `final` or `error` last; SIGINT is DSH's
clean shutdown (exit 130), which the chat sends to DSH's own process group on Ctrl+C. The first run's id continues
the conversation in every later message; when DSH had not saved it (a first run that failed), the chat says so and
starts a new one. Headless turns DSH's credential store off (launch-time keys only); the chat's patch turns it back
on (`credentials`, `authorization`, `deepseek-account`: the rows DSH's README names), before sandbox-ai's own,
which stays the last word. Approvals: headless composes no answerer (`dsh-user-approval`: "headless ... resolve
`unavailable` and fail closed"), so in the default `workspace-write` an action that needs one is refused and shown
as a failed tool result; `-D` (`danger-full-access`, policy `never`) asks nothing.

`sandbox-ai dsh -- web` starts `dsh web --no-open --host 127.0.0.1 --port 3080` in the window, through a
small script of sandbox-ai's (`sandbox/dsh_web.py`, read-only at `/etc/dsh/web.py`): it says first how to reach the
UI, and shows DSH's launch URL with its token masked; `sandbox-ai dsh --browser` runs the same script in the
background beside the chat (its log in `~/.dsh/sandbox-ai-web.log`). The UI is reached
from the sandbox's own browser only (`--browser`: it shares the pod's loopback, which the firewall accepts and the
connection proxy leaves alone); no port is published on the host. When the host's port 3080 is relayed into the
sandbox (`--allow-host-port 3080`, a git login for `http://localhost:3080`) or a service listens there, the UI takes
the next free port from 3081.
With `--browser` (control human or both) the script hands the launch URL to the launcher over the sandbox's browser
socket, and the launcher - for exactly that URL form, `http://127.0.0.1:PORT/?token=...`, at most 5 times a run -
puts it into the session browser's own folder, from which its runtime opens it (Chromium: a new tab; Firefox:
started again on it): the app's Browser tab shows the UI, signed in. launcher.log and the app's log mask the token.
`sandbox-ai dsh -- headless "task"` runs one task.

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
| Web UI and its API (`127.0.0.1:3080`, a token per start; `--host` refuses wildcard binds) | on in `web` | on the sandbox's loopback only: reached from the sandbox's browser, not from the host; with `--browser` opened there by itself, the token masked in the terminal and the logs | `--browser` |
| HMR, config editor, settings (rewrite the profile's patch) | on | on, in the start's HOME; sandbox-ai's patch is read-only and applied after them | - |
| MCP servers (`dsh-mcp-client`, stdio or streamable HTTP) | none | `--mcp` servers through the launcher's stdio bridge (one patch row each) | `--mcp` |
| Skills, sub-agents, workflows, scheduled jobs, plan mode | on | inside the sandbox; the kits' skills and the sandbox-ai skill read-only in `~/.agents/skills`, its own in `~/.dsh/skills` | - |

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
- the dry run's `"dsh"` shows `"telemetry": {"switchable": false, ...}`;
- not even from inside the sandbox (since 0.1.96). A `dsh` typed in the sandbox had no `--patch` (its rows enabled in
  the composed tree, measured with `dsh headless --dump-config`), and `env -u DSH_TELEMETRY_DISABLED` or another
  `DSH_TELEMETRY_OTLP_URL` undid the variables. So the image's entry file (`lib/bin.js`, what `dsh` runs) imports
  sandbox-ai's guard first (sandbox/telemetry_guard.py, `--install dsh` in Containerfile.dsh; the launcher mounts its
  current copy read-only): every profile it boots gets sandbox-ai's patch (/etc/dsh/sandbox-ai.patch.yml, where
  mounted) right after the profile's name - the last overlay, so `otel` and `session-telemetry-otel` are disabled over
  the profile's and the home's `cordis.patch.yml` - the variables are set again, and another value of them or any
  variable named for telemetry or OTLP (`DSH_PRODUCT_ANALYTICS_OTLP_URL` ...) is refused with "sandbox-ai: DeepSeek
  Harness' telemetry is always off in sandbox-ai and cannot be turned on". Measured with 0.2.1-rc.5: through the
  guarded entry both rows are disabled in the composed tree, the refusal holds, `dsh --version` runs. Left: `node
  .../lib/bin.js` copied or started some other way without the import, and a patch of the user's own that inserts
  DSH's OTel service and an exporter row under new ids, skip it - the collector stays refused by the proxy, so what is
  left is an export to a collector of the agent's own in the sandbox. The image needs `sandbox-ai build dsh` again.

Tests: tests/test_dsh.py, tests/test_telemetry_guard.py (with DSH itself: `SANDBOX_AI_TEST_DSH_PACKAGE`) and the
feature matrix's `dsh-telemetry-is-never-switchable`.

### Not (yet) for DSH

- `--with` (not in the team image): `--helper` both ways (`sandbox-ai claude --helper dsh` runs `dsh headless` with
  the task on standard input; `continue` starts a new task: headless resumes only a named session).
- `--effort` (reasoning efforts are per model route in DSH), teams, the activity feed, `mcp attach --live`.
- A login of its own in `sandbox-ai login`: the DeepSeek account sign-in is the web UI's (OAuth to its own loopback
  server); use it in the sandbox's browser, or an API key.

## Rules for its own tools (2026-11-16)

DeepSeek Harness 0.2.1-rc.5 has no hooks a configuration names, but a `--patch` overlay may insert a plugin by its file
path (`dsh-app-boot`: anchorInsertedPluginNames), and its tool registry (`docs/subsystems/tools.md`) freezes a call's
arguments before policy, runs the `tools/pre-execute` waterfall (deny, ask, allow) and then monotonic guards that can
only deny (`ToolGuard`). `run_code`'s sub-calls re-enter the same pipeline.

- sandbox-ai's patch (read-only, the last overlay; also for a `dsh` typed in the sandbox) inserts the Cordis plugin
  `/etc/dsh/sandbox-ai-hooks.mjs`. Its `tools/pre-execute` listener asks the supervisor and denies with the
  `sandbox-ai:` reason; its guard refuses every call that listener did not allow (a policy answering before it, a
  listener that never ran); `tools/post-execute` records.
- Tools mapped: `bash` Bash, `read`/`write`/`edit`, `glob`, `grep`, `web_fetch`, `web_search`, `subagent` Task (its
  arguments already are Claude Code's).
- Kit hooks: none - DSH's Claude Code hook bridge (`dsh-hooks-claude-code`) is experimental and not in the image.
- Not covered: should the plugin fail to load, DSH would run without it (to be shown by the live test).

## Go / No-Go

**Go, not live-tested yet** (choosing it starts it; the start says so in one line).

- The official repository is verified (DeepSeek's organisation, its npm scope and maintainer); MIT.
- DSH reaches nothing by default beyond its model, and its one telemetry has a hard opt-out (variable and patch) that
  sandbox-ai pins, plus the proxy's refusal.
- Every layer of its configuration can be overlaid by a read-only patch given on the command line.
- Its dependencies install hash-locked without install scripts; its commands run inside sandbox-ai's sandbox (with
  DSH's own Landlock confinement on top by default).
- Against: a *developer preview* with only pre-releases and announced breaking changes; no terminal UI of its own
  (sandbox-ai's chat runs its headless mode once per message: no approvals there, output per step, a boot of DSH per
  message; its web UI with `--browser`); 687 packages; plugins are
  code in the agent's process; not live-tested in a sandbox. Hence experimental; each release taken in is reviewed
  again.
