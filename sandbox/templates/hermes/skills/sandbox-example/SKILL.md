---
name: sandbox-example
description: Explains the sandbox-ai environment this agent runs in - the project at /work, the network allow-list, what is kept between runs and where memory lives. Use when the user asks about the sandbox, when a download or connection is blocked, or when deciding where to keep notes.
---

# The sandbox-ai environment

- **Project**: the first workspace is at `/work`; further workspaces are under `/workspaces/NAME`. Only these
  directories are shared with the host.
- **Network**: outbound connections are limited to an allow-list. When a download, `git push` or API call fails with
  a connection or DNS error, the host is most likely not allowed. Name the host to the user; they can allow it with
  `--allow-host HOST` and see what was blocked with `sandbox-ai logs`.
- **Privacy check**: `git commit` and `git push` run sandbox-ai's privacy hooks. When a push to a public remote is
  refused because it would publish a private path, address, account name or token, remove that from the commits or
  ask the user; do not bypass the hooks.
- **HOME** (`/root`) is kept when the start ends and comes back with `sandbox-ai resume NAME`, unless the start ran
  with `--no-keep` or in the security mode `paranoid`; a new start begins with a fresh one.
- **Memory**: `~/.hermes/memories/MEMORY.md` (your memory tool's file). It belongs to this project only: sandbox-ai
  keeps it between runs, and it never reaches other projects or the agent's own setup on the host. Put lasting notes
  about this project there.
- **Kits**: skills (in `~/.agents/skills`) from sandbox-ai kits are read-only in the sandbox. The user changes them
  on the host, in `~/.local/share/sandbox-ai/home/hermes/` or a kit under `~/.local/share/sandbox-ai/kits/`.

More in the skill `sandbox-ai`, which sandbox-ai ships beside this one. This is sandbox-ai's example skill. Edit or
delete it on the host in `~/.local/share/sandbox-ai/home/hermes/skills/sandbox-example/`; a deleted example is not
created again.
