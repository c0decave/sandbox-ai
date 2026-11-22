## OpenCode in this sandbox

You are OpenCode. sandbox-ai sets part of your configuration in `OPENCODE_CONFIG_CONTENT` (a variable of your
process: the `--mcp` servers, sharing and autoupdate off) and in `/etc/opencode/opencode.json` (managed,
read-only, merged after every file of yours): change your own settings in `~/.config/opencode/opencode.json`,
never those keys.

- Your telemetry is always off and cannot be turned on: `OPENCODE_DISABLE_AUTOUPDATE` is set, `"share"` is
  `"disabled"` and OpenTelemetry is off in your managed configuration.
- Rules for your own tools (a ruleset's `tool` rules) hold through sandbox-ai's plugin, named in
  `OPENCODE_CONFIG_CONTENT`.
- The kits' skills, agents and commands are in `~/.config/opencode/skills`, `~/.config/opencode/agents` and
  `~/.config/opencode/commands`, read-only.
