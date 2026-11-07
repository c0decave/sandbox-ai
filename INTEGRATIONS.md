# Integrations: MCP servers, kits and services

[Deutsch](INTEGRATIONS_DE.md) · [README](README.md)

Tools that were asked for, checked against their sources (release, commit,
license, what they send where), and where each one stands in sandbox-ai. Three
ways in, safest first:

- **MCP server in a container of its own** ([sandbox-ai-mcp](https://github.com/c0decave/sandbox-ai-mcp)):
  no capabilities, read-only root, network only as its entry says, files only
  through mounted folders, everything pinned. The agent's own sandbox stays as
  it is.
- **Kit** ([Kits](README.md#kits-project-memory-and-plugins)): skills, plugins
  and hooks for the agent itself, inside its sandbox. Hooks run only once you
  approve them (`sandbox-ai kits trust-hooks KIT`).
- **Remote service**: the agent talks to a hosted MCP endpoint. That needs its
  host allowed (`--allow-host HOST`) and usually a key (`--secret-env NAME`);
  everything the agent sends there passes the connection proxy and is logged.

| Tool | License | Version checked | Way in | State |
|---|---|---|---|---|
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | 0.0.84 (`be26c080`), still the latest | container | prepared (`candidates/playwright`), Debian pins found, not built |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Apache-2.0 | 1.12.0 (`af283441`) | container, full network | prepared (`candidates/chrome-devtools`); the npm integrity hash is open; Google's usage statistics, CrUX lookups and update check switched off in the image |
| [Context7](https://github.com/upstash/context7) | MIT | `@upstash/context7-mcp` 4.5.0 (`a138857c`) | container, `context7.com:443` only; or remote `https://mcp.context7.com/mcp` | prepared (`candidates/context7`), every pin set; `CONTEXT7_API_KEY` optional |
| [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) | MIT | 0.12.1 (`a7158ac2`) | container, no network, the workspace read-only plus a writable index | prepared (`candidates/codebase-memory`), release archive checked by SHA-256 |
| [Browserbase MCP](https://github.com/browserbase/mcp-server-browserbase) | Apache-2.0 | 3.0.0 (`f6bd321a`) | container, `api.browserbase.com`, the session's CDP host, Gemini; or remote `https://mcp.browserbase.com/mcp` | prepared (`candidates/browserbase`); needs `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `GEMINI_API_KEY` |
| [rea](https://github.com/morluto/rea) | MIT | 6.8.0 (`160d821a`) | container, no network | prepared (`candidates/rea`), static analysis only; Node ≥ 22.23 from nodejs.org is open; some of its ~230 tools start the sample |
| [Superpowers](https://github.com/obra/superpowers) | MIT | 7.0.2 (`22f0b463`) | kit | **built in**: `sandbox-ai plugins claude --kit superpowers`, then `sandbox-ai claude --kit superpowers` (Claude) |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | MIT | 1.16.0 (`27c05157`) | kit (a CLI and per-project files) | an image extension: the release binary `openspec-linux-x64.tar.gz` (SHA-256 `f27fb2ff…2ae1`), then `openspec init --tools claude,codex,opencode` in the project; its telemetry is off with `--telemetry off` (the default) |
| [Serena](https://github.com/oraios/serena) | GPL-3.0-or-later | 2.1.0 (`5e4f5cd1`) | an MCP server on the agent's side | it edits the project and has a shell tool, so it belongs in the agent's sandbox: `uv tool install -p 3.13 serena-agent==2.1.0`, `--enable-web-dashboard false`; language servers are downloaded at run time (allow their hosts) |
| [World Monitor](https://github.com/koala73/worldmonitor) | AGPL-3.0-only | 2.11.8 (`de602d00`) | remote service `https://worldmonitor.app/mcp` | `--allow-host worldmonitor.app` and its key; most tools need a paid plan; self-hosting is a whole web application |
| [MindsDB](https://github.com/mindsdb/mindsdb) | Elastic-2.0 (core), MIT (integrations) | 26.1.0 (`86be14b6`) | a server of yours, reached as a remote endpoint | a long-running stateful server (HTTP `:47334/mcp/streamable`), not one container per connection; allow it with `--allow-tcp IP:47334`; its one `query` tool runs any SQL on the databases you connect |
| [ruflo](https://github.com/ruvnet/ruflo) | MIT | 3.71.4 (`bf5d71a1`) | not recommended | several hundred MCP tools including `terminal_execute` and npm installs at run time, hooks on every tool call, a launcher that falls back to `npx …@latest` (unpinned), federation hosts; only after a review of your own, as a kit |
| [Blender MCP](https://www.blender.org/lab/mcp-server/) | GPL-3.0-or-later (unverified) | 1.0.0 (unverified) | host only | it needs Blender's GUI with the add-on and runs any `bpy` Python on the host, through `localhost:9876`; a sandbox would need `--allow-host-port 9876`, which hands it your Blender |
| [screenpipe](https://github.com/screenpipe/screenpipe) | Screenpipe Commercial License | 2.7.138 (`41d15f18`) | unsuitable | records the host's screen and audio, which is what a sandbox keeps from the agent; the license restricts commercial use |

**Prepared** means: the Dockerfile, the audit (`PROVENANCE.md`) and the
license are in `candidates/` of sandbox-ai-mcp, with every pin that could be
checked. A candidate becomes a catalog entry once it is built, its smoke test
passes in CI and its image digest is published; until then
`sandbox-ai mcp add` does not offer it.
