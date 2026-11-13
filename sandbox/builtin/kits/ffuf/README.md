ffuf-web-fuzzing (jthack, MIT per its README): guidance for the web fuzzer ffuf in authorised penetration tests - wordlists, raw requests, auto-calibration, result analysis - for Claude Code, Codex and OpenCode.

Built-in kit `ffuf` of sandbox-ai, off unless a start names it: `sandbox-ai claude --kit ffuf` (or codex, opencode), `"options": ["--kit", "ffuf"]` in a project's .sandbox-ai.json, or the app's Kits & plugins page (Use in the current project). Skills: ffuf-web-fuzzing.

ffuf itself is not in the agent images: `sandbox-ai images extend AGENT --add-apt ffuf` (Debian 12 has 1.1.0; the raw-request mode --request needs ffuf 2.x). Fuzz only systems you may test; the targets must be allowed (--allow-host).

Upstream: https://github.com/jthack/ffuf_claude_skill at 50fd4af4d8bd60cfc48fd84eda1720fbd718857d (2025-10-16T15:39:14-04:00), licence MIT (stated in the upstream README; no LICENSE file upstream). The files are unchanged; kit.json lists each with its SHA-256 and git blob id. Review: docs/skills-review.md.
