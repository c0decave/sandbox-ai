# sandbox-ai documentation

- [English README](README.md) and [quickstart tutorial](QUICKSTART.md)
- [Deutsche Anleitung](README_DE.md) und [Schnellstart-Tutorial](QUICKSTART_DE.md)
- [Integrations](INTEGRATIONS.md), [Proxmox and LXC](PROXMOX_LXC.md)
- [Releasing](RELEASING.md); validation reports: run the live tests, they write `evidence/` (not versioned)

| Command | Purpose |
|---|---|
| `sandbox-ai` | One command for all tools below, plus `doctor`, `logs`, `activity`, `privacy-check`, `update`, `version`, `completion` |
| `sandbox-ai claude\|codex\|opencode` | Start an agent in the sandbox (older form: `claude-sandbox` …, deprecated) |
| `sandbox-ai list`, `rename`, `attach\|pause\|resume\|stop\|delete NAME`, `gc` | Every start by its name: what runs and what is kept, detach and come back, rebirth of a kept start |
| `sandbox-ai build` | Build the agent images from the newest releases, their extensions, the team image and the VPN container (older form: `sandbox-build-image`, deprecated) |
| `sandbox-ai images` | The agent images, their packages, extensions, recipes and variants |
| `sandbox-ai wizard` | Create, edit and trust project configurations; list local LLM models (older form: `sandbox-wizard`, deprecated) |
| `sandbox-ai presets`, `sandbox-ai demo-device` | Ready setups for `--preset`; an emulated serial device to practise on |
| `sandbox-ai net`, `devices`, `limits` | Change what a running sandbox may reach, its devices and limits; take back its taint; end it |
| `sandbox-ai approvals`, `canaries` | Answer what running sandboxes ask; the decoy files of the runs |
| `sandbox-ai secrets`, `git-logins`, `publish-logins`, `identities` | Keys and tokens in your keyring; Git and publish logins added by the connection proxy; who a start works as |
| `sandbox-ai llm` | LLM servers (local or remote) for `--llm`; keys stay with the connection proxy |
| `sandbox-ai vpn` | VPN profiles for `--vpn` (WireGuard, OpenVPN, Mullvad, Tailscale, Tor) |
| `sandbox-ai login` | Login store: log in inside a sandbox, kept for all sandboxes of an agent |
| `sandbox-ai plugins`, `sandbox-ai kits` | Plugin store: install the Claude plugins the kits declare, read-only in every sandbox; see and change kits |
| `sandbox-ai team` | A team of agents, one sandbox each, with a task board |
| `sandbox-ai hosts`, `sandbox-ai web`, `sandbox-ai defaults` | Other machines over SSH; the web interface; your defaults for every start |
| `sandbox-ai session` | The older form of list, attach, pause, resume, stop and delete (`sandbox-session`, deprecated) |
| `sandbox-ai mcp` | List, start and stop local MCP server sandboxes, add servers from the catalogs (older form: `sandbox-mcp`, deprecated) |
| `install.sh`, `sandbox-ai-update` | Install or update to the newest release; `--check`, `--uninstall` |
| `scripts/make_dist.py`, `scripts/publish_release.py` | Build `dist/` and publish a release ([RELEASING.md](RELEASING.md)) |
| `scripts/make_pyz.py`, `scripts/build_appimage.sh`, `scripts/build_macos_app.sh`, `scripts/e2e_package.sh` | Build `sandbox-ai.pyz`, the AppImage and `sandbox-ai.app`, and check each as a user would start it |

| Module | Role |
|---|---|
| `sandbox/agent_sandbox.py` | Host launcher: options, policy, credentials, Podman/Bubblewrap commands |
| `sandbox/runtime.py` | Runs inside the sandbox (the only file mounted there): bootstrap, guard, loggers, connection proxy, DNS stub, display, terminal relay, MCP ends |
| `sandbox/modes.py` | Security modes, from `lax` to `paranoid` |
| `sandbox/sessions.py`, `sandbox/starts.py`, `sandbox/home_review.py` | Background sessions with tmux and kept starts; the registry of every start (list, names, history, gc); the review of a kept HOME before it comes back |
| `sandbox/net.py`, `sandbox/devices.py`, `sandbox/approvals.py` | Changes to a running sandbox (network, taint, stop; devices and limits); questions and their answers |
| `sandbox/privacy.py`, `sandbox/canary.py` | The privacy check of commits and pushes; decoy files with tokens made for each run |
| `sandbox/secret_store.py`, `sandbox/git_logins.py`, `sandbox/publish_logins.py`, `sandbox/identities.py` | Secrets in the keyring or private files; logins the connection proxy adds on the wire; identities |
| `sandbox/mcp.py`, `sandbox/mcp_rootfs.py`, `sandbox/mcp_demo.py`, `sandbox/agent_mcp.py` | MCP server sandboxes; catalog servers under Bubblewrap; built-in test server; an agent as another agent's helper |
| `sandbox/team.py`, `sandbox/team_board.py` | Teams of agents and their task board (an MCP server) |
| `sandbox/vpn.py`, `sandbox/vpn_runtime.py` | VPN profiles, checks and the VPN container's runtime |
| `sandbox/wizard.py`, `sandbox/presets.py`, `sandbox/presets/` | Project wizard and local LLM discovery; presets and the built-in ones |
| `sandbox/llm.py` | LLM endpoints: store, URL rules, model listing and `llm check`, the agents' configuration for `--llm`, the proxy's key entry |
| `sandbox/main.py`, `sandbox/doctor.py`, `sandbox/logs.py`, `sandbox/activity.py`, `sandbox/login.py` | `sandbox-ai` dispatcher, host check, log summary, the agents' timeline, login store |
| `sandbox/kits.py`, `sandbox/kitctl.py`, `sandbox/plugins.py` | Kits (read-only skills, agents, commands, prompts, hooks), project memory, plugin declarations and store; `sandbox-ai kits` |
| `sandbox/images.py`, `sandbox/build_image.sh`, `sandbox/Containerfile.*` | Image extensions, recipes and variants; image builder; the images |
| `sandbox/hosts.py`, `sandbox/webui.py`, `sandbox/web/`, `sandbox/defaults_cmd.py` | Other hosts over SSH; the web interface; `sandbox-ai defaults` |
| `sandbox/demo_device.py`, `sandbox/completion.py`, `sandbox/release_keys.py`, `sandbox/textout.py` | The emulated serial device; tab completion; release signing keys; tables for list and show commands |
| `sandbox/templates/`, `sandbox/marketplace/`, `sandbox/builtin/` | Default kit per agent (example skill, memory template); sandbox-ai's own Claude marketplace with the demo plugin; the built-in skill `sandbox-ai` and built-in kits |
| `sandbox/cli.py`, `scripts/dist_boot.py` | Entry points of the PyPI package; the start of the ready-made packages |
