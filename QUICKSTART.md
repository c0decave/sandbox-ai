# sandbox-ai quickstart tutorial

[Deutsch](QUICKSTART_DE.md) · [Full documentation](README.md)

In about fifteen minutes you build the images, verify the isolation, run an
agent on a project, set the project up with the wizard, and work with a
detachable session. Optional steps cover a local LLM and browser tests.

Run every command as your ordinary user, never with `sudo`. The default
backend needs working rootless Podman with `pasta`;
`podman info --format '{{.Host.Security.Rootless}}'` should print `true`.
On macOS the sandboxes run in a rootless `podman machine` that shares `/Users`
(`sandbox-ai doctor` checks it; the README says what works there so far).

## 0. Install

```bash
./install.sh --from .     # from a checkout; update later: git pull && ./install.sh --from .
sandbox-ai doctor         # what is missing on this host, and how to fix it
```

This puts the commands into `~/.local/bin` (and `~/.bin` if it exists), that
directory on your `PATH` in `~/.bashrc` and `~/.zshrc` (`--no-modify-rc` leaves them alone), and
the files into `~/.local/share/sandbox-ai`. Open a new terminal afterwards. Without `--from` it downloads the
newest release (`--source URL` picks the repository). You can also run
everything from the checkout as `./sandbox-ai claude` and so on, as below; just
never use the checkout itself as a workspace. Tab completion for bash, zsh and
fish comes with it (`sandbox-ai completion install` sets it up again).

No checkout needed: the [releases page](https://github.com/c0decave/sandbox-ai/releases)
has `sandbox-ai.pyz` (one file, any Python 3.9 or newer), an AppImage for Linux
(x86_64, aarch64) and `sandbox-ai.app` for macOS, ready to start
([four ways to install](README.md#four-ways-to-install)). With the `.pyz`, run
the commands below as `./sandbox-ai.pyz build all` and so on.

## 1. Build the images

```bash
./sandbox-ai build all
```

This builds `localhost/codex-sandbox`, `localhost/claude-sandbox` and
`localhost/opencode-sandbox` with the newest agent releases on the current
Node.js LTS. Build only what you need with `./sandbox-ai build claude`.
Run the same command later to update.

## 2. Verify the isolation

```bash
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --offline --probe
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
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo"
cd ~/src/sandbox-demo && sandbox-ai claude   # the same: the directory you start in is /work
```

The start lines say which folder is `/work` and which project configuration
applies, what was copied into the sandbox and, for Claude, how long the login
is valid:

```text
agent-sandbox: /work is /home/me/src/sandbox-demo; configuration: none
agent-sandbox: auth copied: /home/me/.claude/.credentials.json -> /root/.claude/.credentials.json
agent-sandbox: auth copied: /home/me/.claude.json -> /root/.claude.json
agent-sandbox: Claude access token valid for 7.4 h
```

Inside, the project is `/work`; everything the agent writes there appears in
`~/src/sandbox-demo`. The agent can reach only its provider. Try a one-shot
task:

```bash
./sandbox-ai codex --workspace "$HOME/src/sandbox-demo" \
  -- exec --skip-git-repo-check 'Write Hello into hello.txt and read it back.'
cat "$HOME/src/sandbox-demo/hello.txt"
```

Want to see what would happen without starting anything? Add `--dry-run`.

## 4. Set the project up with the wizard

```bash
./sandbox-ai wizard
```

```text
Project directory (mounted at /work) [/home/me/src/sandbox-demo]:
  Security modes (options can only narrow the chosen one):
    lax: every outbound destination (--egress-all-allow), logged with --log-debug; no approval questions
    …
Security mode (lax/low/medium/high/paranoid) [medium]:
Additional workspaces, comma-separated (mounted at /workspaces/<name>): ~/src/shared-lib
Agent (codex/claude/opencode): claude
Backend (podman/bwrap) [podman]:
  default: the agent's own provider only; development: plus PyPI, GitHub and the OpenAI and Anthropic APIs; …
Network (default/development/frontier/dns/egress-all/offline) [default]: development
Advanced: choose the sandbox's own network addresses (--subnet; default: the backend's own) [y/N]:
Start the agent without permission prompts (--dangerously-skip-permissions) [y/N]: y
Virtual display for GUI apps and browsers (--gui) [y/N]:
Run in tmux with an extra shell window (default of the launcher in a terminal; no adds --no-tmux) [Y/n]:
Log denied egress and file access (--log-dir) [y/N]: y
Allow further network destinations (presets such as github, hosts, TCP ports, ports on this host) [y/N]:
Set up Git (commit identity, push tokens for GitHub or your own Git server) [y/N]: y
Commit as your host's git user.name and user.email (--git-identity) [Y/n]:
Token for github.com (…): enter it now, use a host variable, a secret of `sandbox-ai secrets`, or none (none/enter/variable/secret) [none]:
Add a push token for this project's origin https://git.example.org [y/N]: y
Token for https://git.example.org: enter it now, use a host variable or a secret (enter/variable/secret) [enter]:
Token for https://git.example.org (input hidden, empty to skip):
Your own Git server (Gitea, Forgejo, GitLab ...) for a token, e.g. (…), or a Git credentials file (…), empty to finish:
Keep these tokens out of the sandbox: the connection proxy adds them to Git's requests, the agent never reads them (--token-proxy; no GH_TOKEN for gh then) [Y/n]:
  Kit: /home/me/.local/share/sandbox-ai/home/claude (…); project memory on. Change both later with: sandbox-ai wizard edit, part k
Further options (read-only workspaces, persistent HOME, mounts, variables, MCP, display, any other) [y/N]:
…
Write configuration [Y/n]:
Wrote the token to /home/me/.config/sandbox-ai/tokens/https-git.example.org (mode 0600; the project configuration names only this file)
Wrote and trusted /home/me/src/sandbox-demo/.sandbox-ai.json
Start with: sandbox-ai claude --workspace /home/me/src/sandbox-demo
```

Edit answers like in a shell (Backspace, arrow keys, Tab completes paths).
The security mode comes first: Enter keeps `medium` (or the mode of your
defaults), and the questions after it offer only what the mode allows (`high`
and `paranoid` open nothing beyond the agent's provider; README, "Security
modes"). `development` allows PyPI, GitHub and the OpenAI and Anthropic APIs.
The three last blocks are optional; answer `n` to skip them. The Git block offers a token
for your project's `origin` remote (answer `y`; the default is no, because the
agent can change that remote). A token you type is not shown and goes to a
private file (mode 0600) outside the project; the configuration only names
that file. Choose `variable` instead to keep the token in a host variable, or
`secret` for a secret of `sandbox-ai secrets`. For HTTPS servers the Git block also offers
`--token-proxy`: the connection proxy adds the token to Git's requests, and the
agent never reads it.

From now on the launcher applies these settings automatically (also to
`cd ~/src/sandbox-demo && sandbox-ai claude`: a directory with a project file is
always `/work`). Options on the command line still win. To change the configuration later, run
`sandbox-ai wizard edit ~/src/sandbox-demo`: it shows the parts as a menu, asks
again with the current values as defaults and shows what changed before
writing. If the file changes, or you clone a project that
contains one, review it and confirm with `./sandbox-ai wizard trust FILE`; an
untrusted file is never applied.

## 5. Work in a session you can leave and resume

```bash
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --name demo
```

You are now attached to tmux inside the sandbox: window `agent` runs Claude,
window `shell` is a bash in the same sandbox (`Ctrl-b n` switches). Press
`Ctrl-b d` to detach; the agent keeps working, even if you close the terminal.
Every start works like this; without `--name` it gets a codename such as `brisk-otter-lantern`
(the start prints it, `sandbox-ai list` shows it with its workspace).

```bash
./sandbox-ai list                  # NAME  KIND  AGENT  STATE  HOME  STARTED (UTC)  WORKSPACE
./sandbox-ai attach demo           # back in
./sandbox-ai pause demo            # freeze everything, e.g. to free the CPU
./sandbox-ai resume demo           # continue exactly where it was
./sandbox-ai stop demo             # end it; files and HOME are kept
./sandbox-ai resume demo           # later: a new sandbox, Claude continues the conversation
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
list models without the wizard: `./sandbox-ai wizard models --kind ollama --host IP`.

**For every agent, and for servers elsewhere** (another machine, a VPN, the
internet, with an API key): register the server once and start with `--llm`.
The key stays in `sandbox-ai secrets`; the agent only ever sees a placeholder.

```bash
sandbox-ai llm add gpu --kind ollama --url http://192.168.1.20:11434
sandbox-ai llm check gpu --agent claude      # Claude needs Ollama 0.14 or later
./sandbox-ai claude --llm gpu:qwen2.5-coder:14b --no-provider-defaults
```

The wizard offers registered servers too. Details: README, "LLM servers: local
and remote".

## 7. Optional: GUI apps and browser tests

```bash
mkdir -p "$HOME/.local/state/sandbox-ai/vnc-demo"
./sandbox-ai codex --workspace "$HOME/src/sandbox-demo" --egress-all-allow \
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
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --mcp demo \
  -- -p 'Call the version tool of the demo MCP server and show its result.'
./sandbox-ai mcp list
./sandbox-ai mcp stop demo
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

## 10. Optional: start from a preset

```bash
sandbox-ai presets list                          # analysis, development, testing, fun
sandbox-ai presets show software-analysis        # what it sets and what it asks for
sandbox-ai claude --workspace "$HOME/src/sandbox-demo" \
  --preset software-analysis --preset-value samples=~/samples
sandbox-ai demo-device start                     # an emulated serial device on 127.0.0.1:4000
sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --preset hardware-analysis
```

A preset is options, MCP servers and a first task; the command line wins over
it. `sandbox-ai presets check NAME` names the MCP servers still to add.

## 11. Optional: change things while it runs

```bash
sandbox-ai net allow --allow-host pypi.org       # more destinations, until the sandbox ends
sandbox-ai net allow --allow-host-port 3080      # a service on this host, through a relay
sandbox-ai git-logins give RUN http://localhost:3080 --env SANDBOX_GIT_TOKEN   # a Git login (RUN: sandbox-ai list)
sandbox-ai secrets give RUN DEVTO_API_KEY --api-key dev.to   # an API key the proxy puts in
sandbox-ai net allow --allow-host api.osv.dev --keep   # for the next starts too
sandbox-ai net show                              # what was added
```

Better still, keep your Git logins outside every sandbox: the connection proxy
adds them to Git's requests, so no token enters a sandbox at all:

```bash
sandbox-ai secrets set GITEA_TOKEN
sandbox-ai git-logins add http://localhost:3080 --secret GITEA_TOKEN --user you
```

API keys work the same way: with `--api-key dev.to=DEVTO_API_KEY` the agent's
`$DEVTO_API_KEY` is only a placeholder, and the proxy puts the stored key in on
the wire, only for HTTPS to that host. For npm, PyPI, crates.io, GitLab and
GitHub: `sandbox-ai publish-logins add pypi --secret PYPI_TOKEN`, then start with
`--publish pypi`; each publication is asked first. Several accounts (a public
one, a work one, none for an investigation) are identities:
`sandbox-ai identities add NAME ...`, then `--identity NAME`.

Near its memory limit the launcher warns you, and the agent can read
`/sandbox/session/memory.json`; at 95 % it ends the largest test or build so
that the sandbox survives. `sandbox-ai web` serves all of this as a web page
(password and one-time code), also for other machines over SSH (`sandbox-ai hosts`).

## Most important options

| Option | Purpose |
|---|---|
| `--workspace DIR` | Project at `/work`; repeat or separate with commas for more |
| `--name NAME` | Name the start (see step 5); `--no-keep` discards its HOME at the end |
| `--mode high` | Security mode: `lax`, `low`, `medium` (the default), `high`, `paranoid`; options can only narrow it |
| `--dangerously-skip-permissions` | No approval prompts; the sandbox stays in force |
| `--allow-github`, `--allow-pypi`, `--allow-host HOST` | Open individual HTTPS destinations |
| `--allow-host-port PORT` | Reach a service on your own host, e.g. a local Git server (during the run: `net allow --allow-host-port`) |
| `--preset NAME`, `--preset-value K=V` | Start from a preset (see step 10) |
| `--model opus`, `--effort high` | The agent's model and effort, in its own words (Claude Code, Codex) |
| `--git-token-env URL=NAME`, `--git-identity` | Push from the sandbox, e.g. `http://localhost:3080=SANDBOX_GIT_TOKEN` |
| `--git-token-env https://github.com=NAME` | Push to GitHub |
| `--git-credentials ~/.git-credentials` | Use your existing Git logins (the agent can read them) |
| `--kit NAME`, `--no-memory` | Add the skills, agents and commands of a kit (`~/.local/share/sandbox-ai/kits/NAME`); start without project memory |
| `--profile development` | Provider, PyPI, GitHub and the OpenAI and Anthropic APIs |
| `--state-dir DIR` | One sandbox HOME of your own for several starts (every start keeps its own anyway) |
| `--offline`, `--egress-all-allow` | No network / every outbound destination |
| `--gui`, `--gui-vnc DIR` | Virtual display (see step 7) |
| `--log-dir DIR` | Logs (see step 9) |
| `--dry-run`, `--probe` | Show the plan / verify the isolation |

Push to a local Gitea or Forgejo on port 3080:

```bash
export SANDBOX_GIT_TOKEN=...   # Gitea: Settings -> Applications
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" \
  --git-token-env http://localhost:3080=SANDBOX_GIT_TOKEN --git-identity
```

`./sandbox-ai claude --help` lists every option; the README's
[option reference](README.md#option-reference) groups them all, together with
`sandbox-ai list`, `sandbox-ai mcp`, `sandbox-ai wizard` and `sandbox-ai build`.

## Next

The [full documentation](README.md) explains every option: network modes,
multiple workspaces, authentication and the Claude token sync, Bubblewrap,
Wayland, and the test suites.
