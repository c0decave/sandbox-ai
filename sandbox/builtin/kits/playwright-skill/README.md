playwright-skill (lackeyjb, MIT) 5.0.0: browser automation with Playwright - the agent writes and runs Playwright scripts for screenshots, responsive checks, login flows - for Claude Code, Codex and OpenCode.

Built-in kit `playwright-skill` of sandbox-ai, off unless a start names it: `sandbox-ai claude --kit playwright-skill` (or codex, opencode), `"options": ["--kit", "playwright-skill"]` in a project's .sandbox-ai.json, or the app's Kits & plugins page (Use in the current project). Skills: playwright-skill.

Its setup (npm install and a Chromium download) cannot run in a sandbox: the skill folder is read-only and the firewall blocks both. Use the image variant from `sandbox-ai images recipe playwright --from AGENT` (Playwright pinned by its lockfile, Debian's Chromium, PW_EXECUTABLE_PATH set), or the signed MCP server playwright (--mcp playwright).

Upstream: https://github.com/lackeyjb/playwright-skill at dd47a6a023e249eb1b36e9e943eab89d0900865d (2026-08-14T09:51:33-04:00), licence MIT. The files are unchanged; kit.json lists each with its SHA-256 and git blob id. Review: docs/skills-review.md.
