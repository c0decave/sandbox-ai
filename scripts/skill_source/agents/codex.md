## Codex in this sandbox

You are Codex. sandbox-ai pins part of your configuration in `/etc/codex/managed_config.toml` (the layer above
every `config.toml` and `-c` option, read-only): change your own settings in `~/.codex/config.toml`, never
those keys.

- Your telemetry is always off and cannot be turned on: `analytics.enabled`, `feedback.enabled` and
  `check_for_update_on_startup` are `false` and your OpenTelemetry exporters `"none"`, and the proxy refuses
  your analytics on `chatgpt.com`.
- Rules for your own tools (a ruleset's `tool` rules) hold through hooks in `/etc/codex/requirements.toml`
  (read-only, `allow_managed_hooks_only`): only managed hooks run then.
- `--mcp` servers come as `-c mcp_servers.NAME` options when you start.
- `codex login` runs `codex login --device-auth` here (no callback needed).
- The kits' skills are in `~/.agents/skills` and their prompts in `~/.codex/prompts`, read-only.
