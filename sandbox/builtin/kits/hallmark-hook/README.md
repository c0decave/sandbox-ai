Hallmark's edit-time lint hook (opt-in, Claude Code only): sloplint checks each page Claude writes and reports Hallmark's Floor failures at once.

Built-in kit `hallmark-hook` of sandbox-ai. Use it together with the kit `hallmark`, and approve its hook once:

    sandbox-ai kits trust-hooks hallmark-hook      # shows the hook in full; type yes
    sandbox-ai claude --kit hallmark --kit hallmark-hook

The hook is upstream's (`skills/hallmark/scripts/lint-hook.mjs` of Nutlope/hallmark at
8f6969f8922caabde9de1784959eae11ddf68073, vendored unchanged in the kit hallmark), wired the way upstream's
`install-hook.mjs` would wire it into settings.json - which sandbox-ai does not vendor: a kit's hooks run only as
the agent's managed hooks, after your approval, and the approval covers exactly this hooks.json. The hook reads
the written file and runs `node sloplint.mjs FILE --json --tier floor` for at most 4 seconds; it advises and
never blocks. Codex and OpenCode: Hallmark has no hook for them (its Step 7 sweep runs sloplint instead).
Review: docs/skills-review.md (hallmark).
