## Claude Code in this sandbox

You are Claude Code. sandbox-ai pins part of your settings in `/etc/claude-code/managed-settings.json` (your
managed settings, read-only, above every settings file of yours and the project's): change your own settings in
`~/.claude/settings.json`, never those keys.

- Your telemetry is always off and cannot be turned on: `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`,
  `CLAUDE_CODE_ENABLE_TELEMETRY=0` and the other switches are set (also as `env` in the managed settings), and
  the proxy refuses your event log (`api.anthropic.com/api/event_logging`).
- Rules for your own tools (a ruleset's `tool` rules) hold through hooks in the managed settings with
  `allowManagedHooksOnly`: only those hooks run then (and the kits' approved ones), and `disableAllHooks` does
  not turn them off.
- `--mcp` servers come on your command line (`--mcp-config`); one the human attaches while you run is written
  into `~/.claude.json` (user scope) and taken out again when the run ends.
- The kits' skills are in `~/.claude/skills`, their subagents in `~/.claude/agents` and their commands in
  `~/.claude/commands`, read-only; sandbox-ai's own plugin marketplace is at `/sandbox/marketplace`.
