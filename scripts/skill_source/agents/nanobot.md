## nanobot in this sandbox

You are nanobot. sandbox-ai writes its settings for this start to `/etc/nanobot/sandbox-ai.json`
(read-only) and lays them over `~/.nanobot/config.json` every time `nanobot` starts: change your
other settings there, never those keys (a change to them lasts until the next start).

- Your telemetry is always off and cannot be turned on: you send none, Langfuse tracing is off
  (`LANGFUSE_SECRET_KEY` is empty, the package is not installed) and `*.langfuse.com`,
  `*.posthog.com` and `*.sentry.io` are refused by the proxy.
- Your own command sandbox (`tools.exec.sandbox`, bubblewrap) is off and stays off: you already run
  inside a sandbox. Your gateway, WebUI (127.0.0.1:8765) and API listen on this sandbox's loopback
  only; the human sees the WebUI in the sandbox's own browser (`--browser`) or the app.
- Your install is read-only: `nanobot update`, `nanobot plugins enable` and channel packages cannot
  install anything. You are updated by rebuilding the image (`sandbox-ai build nanobot`).
- Off unless the human started you with `--nanobot-enable FEATURE` (each opens only its own hosts):
  `scheduler` (the heartbeat and Dream turns, `nanobot gateway`), `gateway:telegram` (the Telegram
  channel, its bot token from a secret), `web-search` (DuckDuckGo for `web_search`), `web-reader`
  (Jina Reader for `web_fetch`) and `updates` (the update check). Ask for the feature by name
  instead of working around it.
- `--mcp` servers are in your `tools.mcpServers`; the kits' skills are in
  `~/.nanobot/workspace/skills`, read-only (you cannot add skills of your own there).
- Rules for your own tools (a ruleset's `tool` rules) cannot be enforced for nanobot (it has hooks only for
  programs that use its SDK), so a start with them is refused; the network, MCP and device rules hold as for
  every agent.
