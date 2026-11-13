security-audit (Cloudflare, MIT): security guidance and source-first vulnerability review of codebases, with a full audit mode that writes reports - for Claude Code, Codex and OpenCode.

Built-in kit `security-audit` of sandbox-ai, off unless a start names it: `sandbox-ai claude --kit security-audit` (or codex, opencode), `"options": ["--kit", "security-audit"]` in a project's .sandbox-ai.json, or the app's Kits & plugins page (Use in the current project). Skills: security-audit.

Its full audit writes into ~/security-audit-skill/REPO/run-N: in a sandbox that is the start's kept HOME (on the host ~/.local/state/sandbox-ai/sessions/ID/home/security-audit-skill), lost with --no-keep and in the mode paranoid. Left out: the validators' tests (*.test.cjs).

Upstream: https://github.com/cloudflare/security-audit-skill at c1c8a8c1471069fb0e188eeaff69b8e8db6564a8 (2026-09-14T20:28:54+01:00), licence MIT. The files are unchanged; kit.json lists each with its SHA-256 and git blob id. Review: docs/skills-review.md.
