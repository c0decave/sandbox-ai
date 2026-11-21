Superpowers 7.0.1 as upstream's own plugin (opt-in; Claude Code, Codex, OpenCode 2): the skills of the kit superpowers, with upstream's start-up bootstrap where an agent has one - it runs once you approve it.

Built-in kit `superpowers-plugin` of sandbox-ai. It enables `superpowers@sandbox-ai`, upstream's plugin
(obra/superpowers, MIT) pinned in sandbox-ai's marketplace to the commit of its tag v7.0.1
(2ce8aa15d05bd57213e6f746f953aafb7ded7a8b, 2026-10-21). Install it once (it fetches that commit from GitHub into
sandbox-ai's plugin store; any of the three commands installs it for all three agents), then use the kit:

    sandbox-ai plugins codex --kit superpowers-plugin
    sandbox-ai codex --kit superpowers-plugin          # or claude, opencode2

What each agent gets from the same installed folder, read-only:

- Claude Code: the plugin's skills and its SessionStart hook (it prints the using-superpowers skill as context);
  while managed hooks are on the hook runs only once approved: `sandbox-ai plugins claude trust-hooks
  superpowers@sandbox-ai`.
- Codex: the skills, through upstream's Codex manifest (`.codex-plugin/plugin.json`, which declares no hooks).
- OpenCode 2: upstream's V2 plugin (`index.js`, `.opencode/plugins/superpowers.js`): it registers the skills and adds
  the using-superpowers bootstrap to a session's first message. It is code in OpenCode 2's own process, so it runs
  only once approved: `sandbox-ai plugins opencode2 trust-hooks superpowers@sandbox-ai`.

Use either this kit or the kit `superpowers` (the same skills as plain skills, no plugin, no install), not both: the
skills would be there twice.
