# sandbox-ai documentation

- [English README](README.md) and [quickstart tutorial](QUICKSTART.md)
- [Deutsche Anleitung](README_DE.md) und [Schnellstart-Tutorial](QUICKSTART_DE.md)
- [Releasing](RELEASING.md); validation reports: run the live tests, they write `evidence/` (not versioned)

| Command | Purpose |
|---|---|
| `sandbox-ai` | One command for all tools below, plus `doctor`, `logs`, `update` |
| `sandbox-ai claude\|codex\|opencode` | Start an agent in the sandbox (older form: `claude-sandbox` …, deprecated) |
| `sandbox-ai list`, `rename`, `attach\|pause\|resume\|stop\|delete NAME`, `gc` | Every start by its name: what runs and what is kept, detach and come back, rebirth of a kept start |
| `sandbox-ai build` | Build the agent images from the newest releases (older form: `sandbox-build-image`, deprecated) |
| `sandbox-ai wizard` | Create, edit and trust project configurations; list local LLM models (older form: `sandbox-wizard`, deprecated) |
| `sandbox-ai llm` | LLM servers (local or remote) for `--llm`; keys stay with the connection proxy |
| `sandbox-ai login` | Login store: log in inside a sandbox, kept for all sandboxes of an agent |
| `sandbox-ai plugins` | Plugin store: install the Claude plugins the kits declare, read-only in every sandbox |
| `sandbox-ai session` | The older form of list, attach, pause, resume, stop and delete (`sandbox-session`, deprecated) |
| `sandbox-ai mcp` | List, start and stop local MCP server sandboxes (older form: `sandbox-mcp`, deprecated) |
| `install.sh`, `sandbox-ai-update` | Install or update to the newest release; `--check`, `--uninstall` |
| `scripts/make_dist.py`, `scripts/publish_release.py` | Build `dist/` and publish a release ([RELEASING.md](RELEASING.md)) |

| Module | Role |
|---|---|
| `sandbox/agent_sandbox.py` | Host launcher: options, policy, credentials, Podman/Bubblewrap commands |
| `sandbox/runtime.py` | Runs inside the sandbox (the only file mounted there): bootstrap, guard, loggers, display, terminal relay, MCP ends |
| `sandbox/sessions.py`, `sandbox/starts.py`, `sandbox/home_review.py` | Background sessions with tmux and kept starts; the registry of every start (list, names, history, gc); the review of a kept HOME before it comes back |
| `sandbox/mcp.py`, `sandbox/mcp_demo.py` | MCP server sandboxes; built-in test server |
| `sandbox/wizard.py` | Project wizard and local LLM discovery |
| `sandbox/llm.py` | LLM endpoints: store, URL rules, model listing and `llm check`, the agents' configuration for `--llm`, the proxy's key entry |
| `sandbox/main.py`, `sandbox/doctor.py`, `sandbox/logs.py`, `sandbox/login.py` | `sandbox-ai` dispatcher, host check, log summary, login store |
| `sandbox/kits.py`, `sandbox/plugins.py` | Kits (read-only skills, agents, commands, prompts), project memory, plugin declarations and store |
| `sandbox/templates/`, `sandbox/marketplace/` | Default kit per agent (example skill, memory template); sandbox-ai's own Claude marketplace with the demo plugin |
| `sandbox/cli.py`, `sandbox/build_image.sh` | Entry points of the PyPI package; image builder |
