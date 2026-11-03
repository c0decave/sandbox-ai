# sandbox-ai quickstart tutorial

[Deutsch](QUICKSTART_DE.md) · [Full documentation](README.md)

In about fifteen minutes you build the images, verify the isolation, run an
agent on a project, set the project up with the wizard, and work with a
detachable session. Optional steps cover a local LLM and browser tests.

Run every command as your ordinary user, never with `sudo`. The default
backend needs working rootless Podman with `pasta`;
`podman info --format '{{.Host.Security.Rootless}}'` should print `true`.

## 0. Install

```bash
./install.sh --from .     # from a checkout; update later: git pull && ./install.sh --from .
sandbox-ai doctor         # what is missing on this host, and how to fix it
```

This puts the commands into `~/.local/bin` (and `~/.bin` if it exists) and the
files into `~/.local/share/sandbox-ai`. Without `--from` it downloads the
newest release (`--source URL` picks the repository). You can also run
everything from the checkout as `./claude-sandbox` and so on, as below; just
never use the checkout itself as a workspace.

## 1. Build the images

```bash
./sandbox-build-image all
```

This builds `localhost/codex-sandbox`, `localhost/claude-sandbox` and
`localhost/opencode-sandbox` with the newest agent releases on the current
Node.js LTS. Build only what you need with `./sandbox-build-image claude`.
Run the same command later to update.

## 2. Verify the isolation

```bash
./claude-sandbox --workspace "$HOME/src/sandbox-demo" --offline --probe
```

You should see JSON with `"verified": true`: the outer sandbox, Firejail and
the firewall are in place. The workspace directory is created if missing.

## 3. Run an agent

Give the sandboxes a login. Either log in on the host (`claude auth login`,
`codex login` or `opencode auth login`): the sandboxes get a copy. Or, better
if you also run the agent on the host, give the sandboxes a login of their own,
made inside a sandbox and kept for all of them:

```bash
sandbox-ai login claude          # open the link it shows, paste the code
sandbox-ai login --status        # which login each agent's sandboxes get
```

Skills, agents and commands for the sandboxes go into the default kit
`~/.local/share/sandbox-ai/home/AGENT/` (created with an example skill on the first
start); the agent's memory is kept per project. For Claude plugins, including the
demo `/sandcastle`, run `sandbox-ai plugins claude` once.

Then:

```bash
./claude-sandbox --workspace "$HOME/src/sandbox-demo"
```

The start lines show what was copied into the sandbox and, for Claude, how long
the login is valid:

```text
agent-sandbox: auth copied: /home/me/.claude/.credentials.json -> /root/.claude/.credentials.json
agent-sandbox: auth copied: /home/me/.claude.json -> /root/.claude.json
agent-sandbox: Claude access token valid for 7.4 h
```

Inside, the project is `/work`; everything the agent writes there appears in
`~/src/sandbox-demo`. The agent can reach only its provider. Try a one-shot
task:

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  -- exec --skip-git-repo-check 'Write Hello into hello.txt and read it back.'
cat "$HOME/src/sandbox-demo/hello.txt"
```

Want to see what would happen without starting anything? Add `--dry-run`.

## 4. Set the project up with the wizard

```bash
./sandbox-wizard
```

```text
Project directory (mounted at /work) [/home/me/src/sandbox-demo]:
Additional workspaces, comma-separated (mounted at /workspaces/<name>): ~/src/shared-lib
Agent (codex/claude/opencode): claude
Backend (podman/bwrap) [podman]:
  default: the agent's own provider only; development: plus PyPI, GitHub and the OpenAI and Anthropic APIs; …
Network (default/development/frontier/dns/egress-all/offline) [default]: development
Start the agent without permission prompts (--dangerously-skip-permissions) [y/N]: y
Virtual display for GUI apps and browsers (--gui) [y/N]:
Run in tmux with an extra shell window (default of the launcher in a terminal; no adds --no-tmux) [Y/n]:
Log denied egress and file access (--log-dir) [y/N]: y
Allow further network destinations (presets such as github, hosts, TCP ports, ports on this host) [y/N]:
Set up Git (commit identity, push tokens for GitHub or your own Git server) [y/N]: y
Commit as your host's git user.name and user.email (--git-identity) [Y/n]:
Token for github.com (…): enter it now, use a host variable, or none (none/enter/variable) [none]:
Add a push token for this project's origin http://localhost:3000 [y/N]: y
Token for http://localhost:3000: enter it now or use a host variable (enter/variable) [enter]:
Token for http://localhost:3000 (input hidden, empty to skip):
Your own Git server (Gitea, Forgejo, GitLab ...) for a token, e.g. http://localhost:3000, empty to finish:
Further options (read-only workspaces, persistent HOME, mounts, variables, MCP, display, any other) [y/N]:
…
Write configuration [Y/n]:
Wrote the token to /home/me/.config/sandbox-ai/tokens/http-localhost-3000 (mode 0600; the project configuration names only this file)
Wrote and trusted /home/me/src/sandbox-demo/.sandbox-ai.json
Start with: ./claude-sandbox --workspace /home/me/src/sandbox-demo
```

Edit answers like in a shell (Backspace, arrow keys, Tab completes paths).
`development` allows PyPI, GitHub and the OpenAI and Anthropic APIs. The three
last blocks are optional; answer `n` to skip them. The Git block offers a token
for your project's `origin` remote (answer `y`; the default is no, because the
agent can change that remote). A token you type is not shown and goes to a
private file (mode 0600) outside the project; the configuration only names
that file. Choose `variable` instead to keep the token in a host variable.

From now on the launcher applies these settings automatically. Options on the
command line still win. To change the configuration later, run
`sandbox-wizard edit ~/src/sandbox-demo`: it shows the parts as a menu, asks
again with the current values as defaults and shows what changed before
writing. If the file changes, or you clone a project that
contains one, review it and confirm with `./sandbox-wizard trust FILE`; an
untrusted file is never applied.

## 5. Work in a session you can leave and resume

```bash
./claude-sandbox --workspace "$HOME/src/sandbox-demo" --session demo
```

You are now attached to tmux inside the sandbox: window `agent` runs Claude,
window `shell` is a bash in the same sandbox (`Ctrl-b n` switches). Press
`Ctrl-b d` to detach; the agent keeps working, even if you close the terminal.

```bash
./sandbox-session list            # NAME  AGENT  BACKEND  STATUS  …
./sandbox-session attach demo     # back in
./sandbox-session pause demo      # freeze everything, e.g. to free the CPU
./sandbox-session resume demo     # continue exactly where it was
./sandbox-session stop demo       # end it; files and HOME are kept
./sandbox-session resume demo     # later: restart, Claude continues the conversation
```

## 6. Optional: a local LLM with OpenCode

Make the server reachable from the LAN, for example for Ollama:
`OLLAMA_HOST=0.0.0.0 ollama serve` (LM Studio: "Serve on Local Network",
vLLM: `--host 0.0.0.0`). Then run the wizard, choose `opencode`, answer `y` to
the local LLM question, pick the server type, enter the host's LAN IP (not
`127.0.0.1`), sort the list and pick a model:

```text
#  NAME               SIZE    LOADED  DETAILS
1  qwen2.5-coder:14b  9.0 GB  yes     14.8B Q4_K_M
2  llama3.2:3b        2.0 GB  no      3.2B Q4_K_M
```

OpenCode then talks only to that server; nothing else leaves the sandbox. To
list models without the wizard: `./sandbox-wizard models --kind ollama --host IP`.

## 7. Optional: GUI apps and browser tests

```bash
mkdir -p "$HOME/.local/state/sandbox-ai/vnc-demo"
./codex-sandbox --workspace "$HOME/src/sandbox-demo" --egress-all-allow \
  --gui-vnc "$HOME/.local/state/sandbox-ai/vnc-demo" --binary /bin/bash
# inside:
uvx --from playwright playwright install chromium
uv run --with playwright python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=False); pg = b.new_page()
    pg.goto('https://example.org'); pg.screenshot(path='/work/shot.png'); b.close()"
```

Watch the display meanwhile with `vncviewer ~/.local/state/sandbox-ai/vnc-demo/vnc.sock`.
`--egress-all-allow` is needed here only for the browser download.

## 8. Optional: an MCP server in its own sandbox

```bash
./claude-sandbox --workspace "$HOME/src/sandbox-demo" --mcp demo \
  -- -p 'Call the version tool of the demo MCP server and show its result.'
./sandbox-mcp list
./sandbox-mcp stop demo
```

The built-in `demo` server runs in a separate sandbox and answers with its
version and environment; `list_directory` lists its own `/work`. Define real
servers in `~/.config/sandbox-ai/mcp.json` (see the README).

## 9. Optional: look at the logs

With logging enabled (wizard question or `--log-dir DIR`):

```bash
cd ~/src/sandbox-demo && sandbox-ai logs   # blocked targets with the option that allows each, file access
```

The raw JSON lines are `egress-denied.jsonl` and `files.jsonl` in the log
directory under `~/.local/state/sandbox-ai/logs/`.

## Most important options

| Option | Purpose |
|---|---|
| `--workspace DIR` | Project at `/work`; repeat or separate with commas for more |
| `--session NAME` | Detachable session (see step 5) |
| `--dangerously-skip-permissions` | No approval prompts; the sandbox stays in force |
| `--allow-github`, `--allow-pypi`, `--allow-host HOST` | Open individual HTTPS destinations |
| `--allow-host-port PORT` | Reach a service on your own host, e.g. a local Git server |
| `--git-token-env URL=NAME`, `--git-identity` | Push from the sandbox, e.g. `http://localhost:3000=SANDBOX_GIT_TOKEN` |
| `--git-token-env https://github.com=NAME` | Push to GitHub |
| `--git-credentials ~/.git-credentials` | Use your existing Git logins (the agent can read them) |
| `--kit NAME`, `--no-memory` | Add the skills, agents and commands of a kit (`~/.local/share/sandbox-ai/kits/NAME`); start without project memory |
| `--profile development` | Provider, PyPI, GitHub and the OpenAI and Anthropic APIs |
| `--state-dir DIR` | Keep the sandbox HOME between runs |
| `--offline`, `--egress-all-allow` | No network / every outbound destination |
| `--gui`, `--gui-vnc DIR` | Virtual display (see step 7) |
| `--log-dir DIR` | Logs (see step 9) |
| `--dry-run`, `--probe` | Show the plan / verify the isolation |

Push to a local Gitea or Forgejo on port 3000:

```bash
export SANDBOX_GIT_TOKEN=...   # Gitea: Settings -> Applications
./claude-sandbox --workspace "$HOME/src/sandbox-demo" \
  --git-token-env http://localhost:3000=SANDBOX_GIT_TOKEN --git-identity
```

`./claude-sandbox --help` lists every option; the README's
[option reference](README.md#option-reference) groups them all, together with
`sandbox-session`, `sandbox-mcp`, `sandbox-wizard` and `sandbox-build-image`.

## Next

The [full documentation](README.md) explains every option: network modes,
multiple workspaces, authentication and the Claude token sync, Bubblewrap,
Wayland, and the test suites.
