Hallmark (Nutlope, MIT): a design skill for web pages and UIs that look designed, not generated - a floor of design rules, dealt themes, a static linter (sloplint) - for Claude Code, Codex and OpenCode.

Built-in kit `hallmark` of sandbox-ai, off unless a start names it: `sandbox-ai claude --kit hallmark` (or codex, opencode), `"options": ["--kit", "hallmark"]` in a project's .sandbox-ai.json, or the app's Kits & plugins page (Use in the current project). Skills: hallmark.

Its edit-time lint hook is the separate kit hallmark-hook (Claude Code, runs only once approved). Left out: the theme pictures (references/themes/*.jpg; scripts/picture.mjs renders them where puppeteer-core and Chromium are there), the installers install.mjs and install-hook.mjs and the self-test fixtures. Its render tier needs puppeteer-core and Chromium (the image recipe hyperframes brings both); generated images need TOGETHER_API_KEY and network access to api.together.xyz, library photos upload.wikimedia.org.

Upstream: https://github.com/Nutlope/hallmark at 8f6969f8922caabde9de1784959eae11ddf68073 (2026-10-28T14:52:01+00:00), licence MIT. The files are unchanged; kit.json lists each with its SHA-256 and git blob id. Review: docs/skills-review.md.
