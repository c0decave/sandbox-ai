# sandbox-ai

[Deutsch](README_DE.md) · [Quickstart tutorial](QUICKSTART.md)

Linux launchers that run **Codex**, **Claude Code** and **OpenCode** inside two
sandbox layers, with a default-deny network:

```text
Host user → rootless Podman pod or Bubblewrap → Firejail → agent
```

`sandbox-ai` (and its shortcuts `codex-sandbox`, `claude-sandbox`,
`opencode-sandbox`) run the host launcher `sandbox/agent_sandbox.py`; only
`sandbox/runtime.py` is mounted into the sandbox, read-only, as its bootstrap.
Before the agent starts, a guard verifies that
Firejail created additional mount and PID namespaces, dropped all five
capability sets and enabled NoNewPrivs and Seccomp; any failed check aborts.
Outbound traffic is limited by nftables to the agent's own provider unless you
widen it.

### Why

An agent with shell access runs whatever it decides is a good idea, with your
user's rights: it can read `~/.ssh`, push to any remote, or send your code to
any host. sandbox-ai gives it only the project directory and the network
destinations you name, and makes this checkable rather than promised:

- **Verified confinement.** The run aborts if the guard cannot confirm the
  namespaces, capabilities, NoNewPrivs and Seccomp; no silent fallback.
- **Default-deny egress** with per-host allows, and a connection proxy that
  logs every request and checks every `git push` over HTTP(S) for secrets and
  private data before the server sees it.
- **Ask instead of fail.** A connection the options do not allow becomes a
  question on your desktop (`sandbox-ai approvals`), naming the sandbox that
  asks; you allow it once, for the run or for the project.
- **Your agent as it is.** Claude Code, Codex and OpenCode with their own
  logins, plugins and MCP servers, in a terminal, a tmux session you can detach
  from, or the [desktop app](#desktop-app).

Use something else if you need protection against kernel exploits (use a VM),
run on macOS or Windows, or cannot use rootless Podman or Bubblewrap.

### Get it

```bash
git clone https://github.com/c0decave/sandbox-ai && cd sandbox-ai
./install.sh --from .      # commands in ~/.local/bin; update later: git pull && ./install.sh --from .
sandbox-ai doctor          # what is missing on this host, with the command that fixes it
sandbox-ai build claude    # the agent image (or: codex, opencode, all)
sandbox-ai login claude    # unless you are logged in to Claude Code on this host
cd ~/src/myproject && claude-sandbox
```

The [quickstart](QUICKSTART.md) walks through this in fifteen minutes.

## Contents

1. [Requirements](#requirements)
2. [Installation and updates](#installation-and-updates)
3. [The sandbox-ai command](#the-sandbox-ai-command)
4. [Images: build and update](#images-build-and-update)
5. [Starting an agent](#starting-an-agent)
6. [Workspaces and mounts](#workspaces-and-mounts)
7. [Authentication and state](#authentication-and-state)
8. [Kits, project memory and plugins](#kits-project-memory-and-plugins)
9. [Network policy](#network-policy)
10. [Local LLMs with OpenCode](#local-llms-with-opencode)
11. [Local MCP servers](#local-mcp-servers)
12. [Project configuration and wizard](#project-configuration-and-wizard)
13. [Sessions: detach, pause, resume, stop](#sessions-detach-pause-resume-stop)
14. [tmux mode](#tmux-mode)
15. [GUI apps and browsers](#gui-apps-and-browsers)
16. [Hardware: devices and GPUs](#hardware-devices-and-gpus)
17. [Permission-free agents](#permission-free-agents)
18. [Egress and file access logs](#egress-and-file-access-logs)
19. [Privacy check before publishing](#privacy-check-before-publishing)
20. [Connection proxy](#connection-proxy)
21. [Approvals and browser requests](#approvals-and-browser-requests)
22. [Secrets](#secrets)
23. [Resource limits](#resource-limits)
24. [Administrator policy](#administrator-policy)
25. [Desktop app](#desktop-app)
26. [Option reference](#option-reference)
27. [Testing](#testing)
28. [Troubleshooting](#troubleshooting)
29. [Limits and remaining work](#limits-and-remaining-work)

## Requirements

Run as an ordinary host user, without `sudo`. The launchers need Bash,
Python 3.9 or newer and `readlink`.

- **Podman (default backend):** working rootless Podman, delegated UID/GID
  ranges, `pasta`, and one prebuilt image per agent.
- **Bubblewrap:** host-installed agents, `bwrap`, `firejail`, `nft`, `setpriv`,
  `newuidmap`, `newgidmap` and, for networking, `slirp4netns`. `/etc/subuid`
  and `/etc/subgid` need a delegated range of at least 65536 IDs. Bubblewrap
  uses the host's `/usr`, so tools such as `tmux`, `Xvfb` or `x11vnc` must be
  installed on the host for the matching features. An NVM-based installation
  may need an explicit read-only `--runtime` directory. Bubblewrap keeps your
  terminal, so it refuses to start interactively on kernels that allow
  `TIOCSTI` input injection (`dev.tty.legacy_tiocsti` not 0, or a kernel
  before 6.2).

The host kernel and policy must allow the required namespaces, helpers and
nftables; the launchers do not override host restrictions.

## Installation and updates

`install.sh` installs the newest release, or updates to it, and keeps the
launcher out of every workspace:

```bash
./install.sh                    # from a checkout: newest release of the configured source
./install.sh --from .           # this checkout itself
sandbox-ai-update               # later: update to the newest release (an installation from a release)
sandbox-ai-update --check       # installed and newest version, no change
./install.sh --uninstall        # remove commands and installation (sessions and settings stay)
```

| Mode | Files | Commands |
|---|---|---|
| User (default) | `~/.local/share/sandbox-ai/versions/X.Y.Z`, `current` link | links in `~/.local/bin`, and in `~/.bin` if it exists |
| System (`--system`, or run as root) | `/usr/local/lib/sandbox-ai` (`--prefix`) | `/usr/local/bin` |

Releases come from `https://github.com/c0decave/sandbox-ai` unless
`--source URL` or `SANDBOX_AI_SOURCE` names another Gitea, Forgejo or GitHub
repository. The installer downloads `sandbox-ai-bundle-X.Y.Z.tar.gz` and
`SHA256SUMS`, refuses a checksum mismatch, unpacks only regular files below one
directory, and switches the `current` link atomically; the previous version is
kept for a rollback. For a private repository put a token into
`SANDBOX_AI_TOKEN`: it goes only to the repository's own host, never over
plain http except to localhost. Existing files in the bin directories that do
not belong to sandbox-ai are left alone (`--force` replaces them).

`pip install sandbox-ai` (or `pipx install sandbox-ai`) installs the same
commands from PyPI once releases are published there. A checkout works
without installing (`./claude-sandbox`), but it must not be a workspace
itself: the agent could rewrite the launcher. How releases are built and
published is in [RELEASING.md](RELEASING.md).

## The sandbox-ai command

`sandbox-ai` bundles every tool; the older names stay as shortcuts.

| Command | Shortcut | Does |
|---|---|---|
| `sandbox-ai claude\|codex\|opencode [options] [-- args]` | `claude-sandbox` … | start an agent |
| `sandbox-ai session …` | `sandbox-session` | detachable sessions |
| `sandbox-ai mcp …` | `sandbox-mcp` | MCP server sandboxes |
| `sandbox-ai wizard [new\|edit\|trust\|forget\|models] …` | `sandbox-wizard` | project setup, editing and trust |
| `sandbox-ai login claude\|codex\|opencode` | | log in inside a sandbox; the login is kept for all sandboxes of that agent ([login store](#login-store-for-sandboxes)) |
| `sandbox-ai login --status`, `sandbox-ai login AGENT --logout` | | which login each agent's sandboxes get; forget a stored login |
| `sandbox-ai plugins claude [--update]`, `sandbox-ai plugins --status`, `sandbox-ai plugins claude -- ARGS` | | install the Claude plugins the kits declare into the plugin store ([plugins](#kits-project-memory-and-plugins)); show declared vs. installed; run `claude plugin ARGS` on the store |
| `sandbox-ai build [--pin V] AGENT\|all` | `sandbox-build-image` | build the images |
| `sandbox-ai doctor [--backend podman\|bwrap\|both] [--json]` | | check the host: user, Python, user namespaces, Podman (rootless), pasta, subuid/subgid, images with agent versions, Bubblewrap tools, TIOCSTI, host logins (expiry only), the default kit and the kits with their skills, agents, commands and prompts, Claude plugins declared vs. installed, the memory of the project in the current directory, Git identity, project trust; each problem with the command that fixes it; exit 1 on a failure |
| `sandbox-ai logs [DIR\|--project P] [--json]` | | summarize blocked egress and file access of a `--log-dir` and suggest the option for each blocked target (`--allow-pypi`, `--allow-host`, `--allow-tcp`, `--allow-host-port` …) |
| `sandbox-ai net list\|show\|allow\|revoke …` | | change what a running sandbox may reach ([network changes](#changing-the-network-of-a-running-sandbox)) |
| `sandbox-ai approvals list\|allow\|deny\|history …` | | answer what running sandboxes ask ([approvals](#approvals-and-browser-requests)) |
| `sandbox-ai secrets list\|set\|delete\|backend` | | API keys and tokens in your system keyring ([secrets](#secrets)) |
| `sandbox-ai images [packages AGENT] [--json]` | | the agent images with agent version, age and size; the Debian and npm packages in one |
| `sandbox-ai update [--check]` | `sandbox-ai-update` | update an installation made by `install.sh` |
| `sandbox-ai version` | `-V`, `--version` | version |

## Images: build and update

```bash
./sandbox-build-image all                 # newest releases, all three images
./sandbox-build-image claude              # one image, newest Claude Code
./sandbox-build-image --pin 1.0.42 claude # exactly this agent version
```

Each agent has its own `sandbox/Containerfile.<agent>` and image
`localhost/<agent>-sandbox:latest`; an image contains only its own CLI.
Launchers never build or pull images implicitly.

**Base and updates.** The base is `node:lts-bookworm-slim`: the current Active
LTS line of Node.js with its newest patch release, on Debian 12. Every build
uses `--pull=newer`, so LTS bug and security fixes arrive with each rebuild;
when a new LTS line is promoted, the tag follows it. Pin a line with
`--build-arg NODE_IMAGE=docker.io/library/node:24-bookworm-slim`.

**Newest by default, pinned on request.** Every build asks the npm registry
for the newest release of each selected agent first (via `npm view`, or Python
when npm is absent) and passes it as `<AGENT>_VERSION`, so the cached
`npm install` layer is rebuilt whenever a new release exists; if any lookup
fails, nothing is built. To pin instead:

| Option | Pins |
|---|---|
| `--pin VERSION` (or `--build-arg CLAUDE_VERSION=…`) | the agent (one agent per build) |
| `--uv VERSION` | the uv image tag (default `latest`) |
| `--node IMAGE` | the Node.js base image, as a tag or `@sha256:` digest |

Each image carries the label `sandbox-ai.agent-version`, which
`sandbox-ai doctor` shows. For a remote engine add `--podman-url URL`.

**Image contents.** Bash, Git, the GitHub CLI `gh`, Vim, Python 3 with pip
and venv, uv/uvx, ripgrep, curl, wget, `nc` (netcat-traditional),
`host`/`dig`, `ping`, `traceroute`, `ss`/`ip` (iproute2), `ssh` (openssh-client), jq, openssl, tmux, Firejail and
nftables; for `--gui` also Xvfb, x11vnc, x11-utils, fonts and the
runtime libraries of Playwright's Chromium and Firefox. No compiler toolchain;
extend the recipe for project tools. `gh` comes from GitHub's own apt
repository (`cli.github.com`, newest release at build time); in the sandbox it
needs `--allow-github` and a token, e.g. `--git-token-env
https://github.com=NAME`, which also sets `GH_TOKEN`.

Network tools obey the sandbox firewall: `dig`/`host` need `--dns`, `ping`
needs ICMP and `traceroute` its UDP probes (`--allow IP` or
`--egress-all-allow`); `ss` shows the sandbox's own sockets only. The root filesystem is
read-only and Debian marks the system Python as externally managed, so install
packages into a virtual environment (`python3 -m venv .venv`, `uv venv`) or use
`uvx`/`uv tool`. Package indexes are blocked by default; allow them with
`--allow-host pypi.org --allow-host files.pythonhosted.org`.

## Starting an agent

```bash
./claude-sandbox --workspace "$HOME/src/demo"
./codex-sandbox --workspace "$HOME/src/demo" \
  -- exec --skip-git-repo-check 'Create hello.txt containing Hello.'
./opencode-sandbox --workspace "$HOME/src/demo" --dry-run   # show the plan only
./codex-sandbox --workspace "$HOME/src/demo" --offline --probe   # verify isolation
```

Arguments after `--` go to the agent unchanged. `--dry-run` prints the full
plan (workspaces, network, auth copies, GUI, session) without reading
credentials, resolving names or creating anything.

**Claude** defaults to `claude-opus-5-5` with effort `high`: the launcher
writes these into `~/.claude/settings.json` of the sandbox HOME where they are
not already set, so choices kept in a `--state-dir` win, and
`-- --model sonnet --effort medium` overrides them per run. When the sandbox
holds a usable Claude login, the launcher also sets `hasCompletedOnboarding`
and a `theme` in the sandbox `~/.claude.json` if missing; otherwise Claude's
first-run setup would ask for a new login despite valid credentials.
Of the host's `~/.claude.json` only the account part (`oauthAccount`,
`userID`, onboarding state, theme) is copied; MCP server definitions, which
often hold API keys, and the list of host projects stay on the host. Tokens
Claude refreshes in the sandbox are written back only if the host had a login
and did not change it during the run; at the next start the host's login
always replaces the sandbox's.

**Codex** gets `--sandbox danger-full-access` by default: it relies on the
verified outer sandbox and Firejail, because its own nested sandbox cannot
create a UID map inside them. Explicit `--sandbox`, `-s` and
`--dangerously-bypass-approvals-and-sandbox` options are kept.

## Workspaces and mounts

`--workspace PATH` appears at `/work`, the agent's working directory, and is
writable unless `--workspace-ro` is given. Relative paths resolve from the
invocation directory; missing directories are created (an existing one gives a
warning). Seeing `root:root` inside is expected: namespace UID 0 maps to your
user, no `chmod 777` needed.

A workspace may not be or contain your HOME, `/`, the temporary directory,
`XDG_RUNTIME_DIR` or sandbox-ai's own launcher, and may not lie inside
`--state-dir` or an rw `--mount`: the agent could change shell start files,
credentials or the launcher, or redirect the path with a symlink. Nor may a
workspace, rw mount or `--state-dir` be, contain or lie in `~/.config`,
`~/.local`, `~/.ssh`, `~/.gnupg`, `~/.mozilla`, `~/bin` or `~/.bin` (the XDG
directories wherever they are set), or be or lie in a directory on your
`PATH`; sandbox-ai's own session, MCP, login and plugin HOMEs are the
exception. sandbox-ai records every tree a sandbox could write
(`~/.local/state/sandbox-ai/exposed.json`); a workspace, mount, `--state-dir`
or `--log-dir` whose path passes a symlink inside such a tree is refused,
because an agent may have planted it to get another directory mounted. Your own
links elsewhere (`~/src -> /data/src`) are fine. A workspace may contain a
directory on your `PATH` only if it is the project's own `.venv/bin`,
`venv/bin`, `node_modules/.bin` or `$VIRTUAL_ENV/bin`; that is convenience, not
protection: the agent can put a `git` there that your shell runs next, so
sandbox-ai warns after the run when programs there were added or changed. A
workspace's `.git/hooks` and `.git/config` run code when you use git on the
host, so review them after a run.

**Several workspaces.** Repeat `--workspace` or separate entries with commas.
The **first entry is always `/work`**; every further one appears at
`/workspaces/<directory name>` (`-2`, `-3` … for repeated names). They stay out
of `/work` so that no mount points appear inside the main repository on the
host. Duplicate or nested workspaces are rejected; `--workspace-ro` applies to
all. Claude Code edits outside its working directory only after approval: pass
`-- --add-dir /workspaces/<name>` or use `--dangerously-skip-permissions`.

```bash
./claude-sandbox --workspace "$HOME/src/app,$HOME/src/shared-lib" --workspace "$HOME/notes"
#   /work = app, /workspaces/shared-lib, /workspaces/notes
```

**Extra mounts:** `--mount SOURCE:DESTINATION:ro|rw`, repeatable. Sources must
exist; destinations must lie under `/work`, `/mnt` or `/opt`. Commas, socket
and device files are rejected (for devices see [Hardware](#hardware-devices-and-gpus)). Sockets inside mounted directories can still
expose host services, so mount only what the agent needs.

Claude and Codex ask on their first start in a directory whether you trust
it. You chose the workspaces yourself, so sandbox-ai marks `/work` and every
`/workspaces/NAME` as trusted in the sandbox HOME (`hasTrustDialogAccepted` in
`~/.claude.json`, `trust_level = "trusted"` in `~/.codex/config.toml`); a
decision already stored there is kept.

## Authentication and state

Without an auth option, the agent's existing credential files are copied
privately (mode `0600`) into the sandbox HOME whenever it has no login yet
(a fresh HOME always; a reused `--state-dir` or session keeps its own); missing
files do not stop the start. The source is the [login store](#login-store-for-sandboxes)
if it holds a login for the agent, otherwise the host files below. Every copy is reported on stderr with source and target, never
contents, and `--dry-run` lists planned copies under `auth_copies`.

| Agent | Host source | Sandbox target |
|---|---|---|
| Codex | `~/.codex/auth.json` (`CODEX_HOME`) | `/root/.codex/auth.json` |
| Claude | `~/.claude/.credentials.json` (`CLAUDE_CONFIG_DIR`) | `/root/.claude/.credentials.json` |
| Claude metadata | `~/.claude.json` (account part only) | `/root/.claude.json` |
| OpenCode | `~/.local/share/opencode/auth.json` (`XDG_DATA_HOME`) | `/root/.local/share/opencode/auth.json` |

| Option | Effect |
|---|---|
| `-ca`, `--copy-auth` | Require the copy; a missing primary file is an error. A login already in a reused sandbox HOME is kept. |
| `-na`, `--no-auth` | Copy nothing. |
| `-af`, `--auth-file FILE` | Copy this file to the agent's auth target. |
| `-ic`, `--import-codex-auth FILE` | OpenCode only: convert Codex ChatGPT OAuth into an OpenCode login. |
| `-cp`, `--copy SRC:DEST` | Copy any file to `DEST` relative to the sandbox HOME. |
| `-S`, `--state-dir DIR` | Persistent sandbox HOME (owned by you, mode `0700`, outside the workspace). |

OS keyrings are not exported: an agent that keeps its login only in a keyring
needs a file-based login first. Of Claude's `.credentials.json` only the
`claudeAiOauth` login crosses, not the OAuth tokens of your MCP servers. The
host files are read only if they lie outside every workspace, rw mount and
`--state-dir` (`CODEX_HOME`, `CLAUDE_CONFIG_DIR` and `XDG_DATA_HOME` included).

**Claude login.** For Claude the launcher also prints how long the copied
access token is valid. Anthropic rotates OAuth refresh tokens, so the side that
refreshes last holds the only valid pair. With automatic copying or
`--copy-auth`, `.credentials.json` is therefore synchronised by `expiresAt`,
newest wins: newer source credentials replace those in a reused state directory
before start, and newer sandbox credentials are written back to the source
(host or login store) after exit, but only if the source had a login when the
run started and that login is unchanged. Only the login counts, not the whole
file: Claude on the host rewrites the file for other reasons (MCP OAuth
tokens), and refusing the write-back then would leave the host with a refresh
token the sandbox has already used up, so that the next start ends in "Login
expired". If a run with a persistent HOME (`--state-dir`, sessions) was killed
before writing back, the next start notices it from a private record of token
digests (`~/.local/state/sandbox-ai/login-sync.json`, no tokens) and writes
the newer pair back first; a run that did not share the login (`--no-auth`,
another account) clears that record, so its login is never taken for one. Only typed token fields (`accessToken`, `refreshToken`, `expiresAt`,
`refreshTokenExpiresAt`) cross back, atomically with mode `0600`, and only from
a regular file of bounded size that is not a symlink, with an expiry no more
than 31 days ahead; symlinks planted in a reused sandbox HOME are refused. A
failed write-back is reported but never changes the agent's exit status. Residual
risk: a compromised agent could substitute tokens of another account that the
host would then use. `--no-auth` or `--auth-file` keep the host file untouched.
Other agents' host files are never written.

**Before the start** the launcher checks the login the sandbox will get. If it
is missing it says so (`sandbox-ai login AGENT`); if Claude's refresh token has
expired, it asks in a terminal whether to log in now inside a sandbox, keeping
that login in the login store for all later sandboxes.

### Login store for sandboxes

Host and sandboxes that share one login also share its rotating refresh token:
whichever refreshes second is logged out. `sandbox-ai login AGENT` gives the
sandboxes a login of their own:

```bash
sandbox-ai login claude            # runs `claude auth login` inside a sandbox; open the link it shows
sandbox-ai login codex             # `codex login --device-auth` (no localhost callback needed)
sandbox-ai login opencode          # `opencode auth login`, with the frontier hosts allowed
sandbox-ai login claude -- auth login --console   # other login arguments after --
sandbox-ai login --status          # which login each agent's sandboxes get, and its state
sandbox-ai login claude --logout   # forget it; sandboxes use the host's login again
```

The login runs in a sandbox with a fresh HOME, no copied credentials, no
project configuration and the agent's provider hosts allowed; launcher options
such as `--backend bwrap` or `--podman-url URL` are passed on. Afterwards only
the login is taken from that HOME (for Claude the `claudeAiOauth` block and the
account part of `.claude.json`), without following symlinks, and kept in
`~/.local/share/sandbox-ai/auth/AGENT/` (directories `0700`, files `0600`).
From then on every sandbox of that agent gets its login from there, and tokens
a sandbox refreshes go back there; the host's own login is neither used nor
changed. For Codex and OpenCode only the refreshed tokens of the stored account
go back (Codex: `access_token`, `refresh_token`, `id_token` with the same
`account_id`; OpenCode: OAuth tokens of providers already stored), never an API
key, another account or a new provider, and a store without the agent's
structure is ignored. A persistent HOME (`--state-dir`, session) that still
holds the login it got from the store takes over newer tokens another sandbox
wrote back. A workspace that contains the store is
refused. Residual risk: sandboxes of one agent share the stored login, so an
agent could replace it for later sandboxes; two sandboxes refreshing at the
same time still race for one refresh token.

## Kits, project memory and plugins

Every sandbox can bring the agent's skills, agents, commands and prompts with
it, keep the agent's memory per project and enable plugins, without exposing
the host's own `~/.claude`, `~/.codex` or `~/.config/opencode`.

**Kits.** A kit is a directory with one subdirectory per agent:

```text
~/.local/share/sandbox-ai/
  home/claude/                  default kit: part of every Claude sandbox
    skills/sandbox-example/SKILL.md
    agents/  commands/  CLAUDE.md  marketplaces.json
  home/codex/                   skills/  prompts/  AGENTS.md
  home/opencode/                skills/  agents/  commands/  AGENTS.md  marketplaces.json
  kits/review/claude/           --kit review
  kits/review/codex/
```

| In the kit | Claude | Codex | OpenCode |
|---|---|---|---|
| `skills/NAME/SKILL.md` | `~/.claude/skills` | `~/.agents/skills` | `~/.config/opencode/skills` |
| `agents/NAME.md` | `~/.claude/agents` | — | `~/.config/opencode/agents` |
| `commands/NAME.md` | `~/.claude/commands` | — | `~/.config/opencode/commands` |
| `prompts/NAME.md` | — | `~/.codex/prompts` | — |
| memory template | `CLAUDE.md` | `AGENTS.md` | `AGENTS.md` |
| `marketplaces.json` | marketplaces and plugins | — (not yet) | npm plugins |

The first start of an agent creates its default kit from sandbox-ai's
templates, with an example skill (`sandbox-example`, which explains the
sandbox to the agent), a memory template and, for Claude, the official
Anthropic marketplace and sandbox-ai's demo plugin. Edit or delete them; an
existing default kit is never changed again, a deleted example stays deleted.

Every sandbox also gets sandbox-ai's **built-in skill** `sandbox-ai`
(shipped in `sandbox/builtin/AGENT`, updated with sandbox-ai): how to work in
the sandbox, what is shared, how network, proxy, privacy check, hardware,
displays and sessions behave, and which start option or
`sandbox-ai net` command the human needs for what. A skill named `sandbox-ai`
in one of your kits replaces it.

```bash
claude-sandbox                             # default kit only
claude-sandbox --kit review --kit python   # plus two named kits
codex-sandbox --kit-dir ~/my-kits          # plus ~/my-kits/codex
claude-sandbox --no-default-kit --kit review
```

Entries of all kits are merged; the same skill, agent, command or prompt name
from two kits, or two memory templates, stop the start with a message naming
both kits; nothing is replaced silently. sandbox-ai copies the entries into a
private snapshot and mounts it **read-only**: the agent can use, not change
them. A kit entry may be a symlink (handy for a skill kept in a Git checkout);
below an entry, links are followed only while they stay inside it. Neither a
kit nor a link target may lie in a workspace, a rw mount or the `--state-dir`,
nor may one of those lie inside a kit or store; loops, more than 5000 files or 64 MiB stop the start. Names
that only work for another agent (e.g. `prompts/` in a Claude kit) are reported
and ignored. Directory names `--kit` accepts: letters, digits, `_`, `.`, `-`.

**Project memory.** The agent's memory file (`~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md`) and Claude's auto memory
(`~/.claude/projects/-work/memory/`) are kept per project, the project being
the first workspace, in `~/.local/state/sandbox-ai/memory/NAME-HASH/AGENT/`.
The first run of a project starts from the kit's memory template; from then on
the file belongs to the project. The agent works on a copy; after the run,
the files it changed or deleted are taken back into the project's memory, and
never into a kit or the host's own memory. Symlinks, special files and more
than 1000 files or 16 MiB are not taken back. Two sandboxes of one project
keep both of their changes: only files a run changed are written, and a file
it deleted is removed only if nobody changed it meanwhile. All of this reads
and writes the HOME without following a symlink the agent planted, also one
swapped in while copying. If the launcher dies before saving, a persistent
HOME (`--state-dir`, session) keeps the changes for its next start; a
throwaway HOME loses them. `--no-memory` starts without project memory;
`sandbox-ai doctor` shows the memory of the project in the current directory.

**Claude plugins.** Marketplaces and plugins are declared in a kit's
`claude/marketplaces.json`, in the format of Claude's
`extraKnownMarketplaces` plus a list of plugins:

```json
{
  "marketplaces": {
    "claude-plugins-official": {"source": {"source": "github", "repo": "anthropics/claude-plugins-official"}},
    "sandbox-ai": {"source": {"source": "directory", "path": "/sandbox/marketplace"}}
  },
  "plugins": ["sandcastle@sandbox-ai"]
}
```

Sources: `github` (`repo`), `git` and `url` (`https://` only), and sandbox-ai's
own marketplace (`/sandbox/marketplace`). `sandbox-ai plugins claude` installs
everything the default kit and all kits declare into the plugin store
`~/.local/share/sandbox-ai/plugins/claude`: it runs `claude plugin marketplace
add` and `claude plugin install` in a sandbox with no login, no project and
network access to the marketplace's host only (GitHub preset, or the host of a
`git`/`url` source). Every Claude sandbox then gets the store's `cache` and
`marketplaces` read-only and enables exactly the plugins its kits list; a
plugin installed for another kit stays off. Marketplaces do not update
themselves in a sandbox; `sandbox-ai plugins claude --update` does.

```bash
sandbox-ai plugins claude             # install what the kits declare
sandbox-ai plugins claude --update    # also update marketplaces and plugins
sandbox-ai plugins --status           # declared vs. installed, per kit
sandbox-ai plugins claude -- list     # any `claude plugin ...` command on the store
```

The demo plugin `sandcastle@sandbox-ai` adds `/sandcastle`: a robot building
sandcastles in its sandbox. A Claude sandbox started before `sandbox-ai plugins
claude` ran says which declared plugins are missing. **OpenCode** gets the npm
packages listed in `opencode/marketplaces.json` (`{"plugins": ["name@1.2.3"]}`)
as its `plugin` setting; OpenCode installs them itself, which needs
`--allow-host registry.npmjs.org`. **Codex** plugins are not supported yet.

Kits, memory and plugins are left out of `--probe`, MCP server sandboxes and
the login sandbox. `--copy` into a path they fill is refused. A kit or store
inside a workspace is refused. Residual risk: a plugin runs code in every
sandbox that enables it (hooks, MCP servers); install only plugins you trust.

## Network policy

Each invocation gets its own network namespace with nftables, **default
deny**. Only the agent's provider hosts are resolved at start, pinned in
`/etc/hosts` and allowed on **TCP/443**. There is no DNS inside by default.

| Agent | Default HTTPS hosts |
|---|---|
| Codex | `api.openai.com`, `chatgpt.com`, `auth.openai.com` |
| Claude | `api.anthropic.com`, `console.anthropic.com`, `claude.ai`, `claude.com`, `platform.claude.com` |
| OpenCode | `opencode.ai` |

Whenever `api.anthropic.com` is allowed, Anthropic's published ingress ranges
`160.79.104.0/23` and `2607:6bc0::/48` are allowed on TCP/443 too, so the API
stays reachable if DNS rotates during a long session. OpenAI publishes no
stable ranges; its hosts stay DNS-pinned.

| Option | Effect |
|---|---|
| `-p`, `--profile NAME` | A named bundle (repeatable): `development` = provider + PyPI + GitHub + OpenAI and Anthropic APIs; `frontier` = `--frontier`; `dns`, `egress-all`, `offline` = the options below. The wizard uses the same profiles. |
| `-f`, `--frontier` | Also allow all OpenAI and Anthropic hosts above, for any agent. |
| `-y`, `--allow-pypi` | `pypi.org`, `files.pythonhosted.org` |
| `-g`, `--allow-github` | `github.com`, `api.github.com`, `codeload.github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` |
| `-gl`, `--allow-gitlab` | `gitlab.com`, `registry.gitlab.com` |
| `-ak`, `--allow-kernel-org` | `kernel.org`, `www.kernel.org`, `git.kernel.org`, `cdn.kernel.org`, `mirrors.kernel.org`, `lore.kernel.org` |
| `-dd`, `--allow-duckduckgo` | `duckduckgo.com`, `html.duckduckgo.com`, `lite.duckduckgo.com`, `links.duckduckgo.com`, `api.duckduckgo.com` |
| `-A`, `--allow-host NAME` | Resolve and pin a name; allow TCP/443. |
| `-a`, `--allow IP_OR_CIDR` | All protocols and ports to that destination. |
| `-t`, `--allow-tcp IP:PORT` | Exactly one TCP port on one address (IPv6: `[ADDR]:PORT`). |
| `-P`, `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in the sandbox reaches the same port on the host's loopback (repeatable). |
| `-R`, `--dns` | Real DNS via `1.1.1.1` and `8.8.8.8` (UDP/TCP 53 to them only). |
| `-ns`, `--nameserver IP` | Use this resolver instead (repeatable; implies `--dns`). |
| `-E`, `--egress-all-allow` | Allow every outbound destination and port; implies `--dns`. |
| `-nd`, `--no-provider-defaults` | Drop the agent's default hosts. |
| `-o`, `--offline` | No external network at all. |
| `-nw`, `--network NAME`, `-ip`, `--ip`, `-i6`, `--ip6` | Podman: existing private network, static addresses. |

`--dns` resolves names but does not open the resolved addresses; resolvers can
carry data out in query names, so it is a deliberate widening.
`--egress-all-allow` keeps inbound traffic and forwarding closed and the
host's own loopback hidden, but the local network is reachable, including
services the host offers on its LAN addresses. `127.0.0.1` means the
sandbox itself, except for ports opened with `--allow-host-port`. IP filtering
cannot distinguish domains that share CDN addresses. Podman uses `pasta`,
Bubblewrap `slirp4netns`; no ports are published.

**Services on your own host** (a local Git server, a database) cannot be
reached with `--allow-tcp`: `pasta` gives the sandbox the host's own IP
address, so that address leads back into the sandbox. This avoids clashing
with any other machine on your network, but hides the host. Use
`--allow-host-port PORT` instead: pasta then forwards `127.0.0.1:PORT`
in the sandbox to the same port on the host's loopback, and
nothing else on the host becomes reachable. A Git remote such as
`http://localhost:3000/...` works unchanged inside the sandbox. The host is
the machine running Podman, so with `--podman-url` it is that machine. Only
the Podman backend with its default network supports this; it conflicts
with `--offline`, `--network` and `--backend bwrap`.

```bash
./claude-sandbox --workspace "$HOME/src/demo" --allow-host-port 3000
```

**Pushing to GitHub**: `--git-token-env https://github.com=NAME` hands the
token in the host variable `NAME` to Git for `https://github.com` only,
through a credential helper set via `GIT_CONFIG_*` variables, also as
`GH_TOKEN` for the `gh` CLI, and implies `--allow-github`. Nothing is written
to the sandbox HOME, and helpers from a `~/.gitconfig` there are ignored for
GitHub. `--git-identity` commits as the host's `user.name` and `user.email`.

The agent can read the token and could pass it on over GitHub itself, for
example in a gist. Use a **fine-grained token** limited to the repositories
the agent needs, with only `Contents: Read and write` and a short expiry, and
protect important branches on GitHub. A host-side push proxy that keeps the
token out of the sandbox is planned, see [TODO.md](TODO.md).

```bash
export SANDBOX_GH_TOKEN=github_pat_...   # fine-grained, selected repositories only
./claude-sandbox --git-token-env https://github.com=SANDBOX_GH_TOKEN --git-identity
```

**Pushing to your own Git server** (Gitea, Forgejo, GitLab, ...):
`--git-token-env URL=NAME` hands the token in the host variable `NAME` to
Git for exactly that `URL` (scheme, host and port, no path; repeatable).
Write the user as `http://USER@HOST:PORT`; without one, Git sends
`x-access-token`, which Gitea, Forgejo and GitLab accept with a token. A URL
on the host's loopback (`localhost`, `127.0.0.1`, `[::1]`) implies
`--allow-host-port PORT` and needs the Podman backend with its default
network; plain `http` is accepted only there. Other hosts need `https`; the
option implies `--allow-host HOST` (name pinned, TCP/443), a different port
additionally needs `--allow-tcp IP:PORT`. The URL must match the remote's host exactly: a token for
`http://localhost:3000` does not answer for `http://127.0.0.1:3000`. As
with GitHub, helpers from a `~/.gitconfig` in the sandbox are ignored for
that URL, and the agent can read the token (as `SANDBOX_GIT_TOKEN_0`, ...),
so create one limited to the needed repositories.

```bash
export SANDBOX_GITEA_TOKEN=...   # Gitea: Settings -> Applications, scope repository
./claude-sandbox --git-token-env http://alice@localhost:3000=SANDBOX_GITEA_TOKEN --git-identity
```

Instead of a variable, `--git-token-file URL=FILE` reads the token from a file that belongs to you, has mode `0600`, is not
a symlink and lies outside every workspace. `sandbox-wizard` writes such files
under `~/.config/sandbox-ai/tokens/` when you type a token there (input
hidden); the project configuration then names only the file, never the token.

**Your existing Git logins**: `--git-credentials FILE` takes the logins from a
file in the format of Git's `store` credential helper, usually
`~/.git-credentials` (one `https://USER:PASSWORD@HOST[:PORT]` per line). Each
login answers for its server exactly as a `--git-token-*` option would, with
the same network rules: a login for `http://localhost:3000` implies
`--allow-host-port 3000`, one for `https://github.com` `--allow-github` and
`GH_TOKEN`, others `--allow-host HOST`. The file must belong to you, have mode
`0600`, not be a symlink and lie outside every workspace; it is read on the
host and never mounted. As in Git, the first line for a server wins and a
path in a line does not narrow it; a `--git-token-*` option for the same
server wins over the file. Lines the sandbox cannot use (plain `http` to
another host, a loopback server without the Podman default network,
malformed lines) are skipped with a note naming the line number, never its
content; a file without any usable login is refused. **The agent can read
every login it is given** (`SANDBOX_GIT_USER_0`/`SANDBOX_GIT_TOKEN_0`, ...),
so keep only tokens in that file, not account passwords, or use a separate
file with just the logins the sandbox needs.

```bash
./claude-sandbox --git-credentials ~/.git-credentials --git-identity
```

### Changing the network of a running sandbox

The network options are read at the start, but a running sandbox can be given
more (or less) without a restart:

```bash
sandbox-ai net list                                    # running sandboxes
sandbox-ai net allow --allow-host pypi.org --allow-github
sandbox-ai net allow claude-1a2b3c4d --allow-tcp 192.0.2.10:5432
sandbox-ai net show                                    # what was added during the run
sandbox-ai net revoke --allow-host pypi.org
```

The name of a run is shown by `list` and given to the agent as
`$SANDBOX_AI_RUN`, so it can name the exact command; it may be left out when
only one sandbox runs (or one for the current directory). `--allow-host` names
are resolved on the host and added to the sandbox's `/etc/hosts`; the
supervisor adds the addresses to the firewall and the proxy. Changes last
until the sandbox ends; the command prints the options that keep them. Only
you can do this: the control socket lies in `$XDG_RUNTIME_DIR/sandbox-ai/runs/RUN`
(mode `0700`) and is hidden from the agent. Connections already open stay
open after a `revoke`. Real DNS (`--dns`) and `--allow-host-port` still need a
restart.

## Local LLMs with OpenCode

OpenCode can use a local **Ollama**, **LM Studio** or **vLLM** server, with no
cloud access at all. The easiest way is the wizard (next section); it lists the
server's models with name, size and load state, sortable by each, and writes
everything below. Manually:

```bash
./sandbox-wizard models --kind ollama --host 192.168.1.20 --sort size
./opencode-sandbox --workspace "$HOME/src/demo" \
  --local-config ./opencode-local.json --allow-tcp 192.168.1.20:11434
```

`--local-config` copies the OpenCode configuration privately, removes every
default cloud permission and disables project configuration, model catalog
fetching, auto-updates and sharing. `--allow-tcp` opens exactly the server's
port. The wizard's configuration uses OpenCode's OpenAI-compatible provider
(`http://IP:PORT/v1`).

| Server | Default port | Models from | Size | Loaded |
|---|---|---|---|---|
| Ollama | 11434 | `/api/tags`, `/api/ps` | yes | yes |
| LM Studio | 1234 | `/api/v0/models` (fallback `/v1/models`) | when reported | yes |
| vLLM | 8000 | `/v1/models` | no | always (served = loaded) |

The sandbox cannot reach the host's `localhost`. Bind the server to the LAN
(`OLLAMA_HOST=0.0.0.0`, LM Studio "Serve on Local Network",
`vllm serve … --host 0.0.0.0`) and use the host's LAN IP.

## Local MCP servers

Local MCP servers run **in their own sandbox** (Podman pod or Bubblewrap,
same Firejail guard), without credentials and, unless their definition says
otherwise, without network. `--mcp NAME` starts the server sandbox if it is not
already running, then connects the agent; the server keeps running for other
agents and sessions.

```bash
./claude-sandbox --workspace "$HOME/src/app" --mcp demo   # built-in test server
./sandbox-mcp list                                        # NAME  STATUS  COMMAND
./sandbox-mcp stop demo
```

There is no network path between the sandboxes: the server sandbox listens on
a Unix socket in `~/.local/state/sandbox-ai/mcp/NAME/sock/` and starts one
instance of the stdio server per connection; the agent sandbox mounts that
directory read-only and talks to it through a small stdio bridge. The launcher
registers each server with the agent: Claude via `--mcp-config`, Codex via
`-c mcp_servers.NAME…`, OpenCode via `OPENCODE_CONFIG_CONTENT`.

The built-in **`demo`** server is a dependency-free test server with two
tools: `list_directory(path)` (default `/work` of the server sandbox) and
`version`, which reports its version together with hostname, kernel, PID
namespace and Seccomp state, showing that it runs confined in its own sandbox.

Define further servers in `~/.config/sandbox-ai/mcp.json` or in a trusted
project file under `"mcp_servers"`:

```json
{
  "files": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/work"],
            "workspace": "/home/me/src/app", "options": ["--allow-host", "registry.npmjs.org"]},
  "fetch": {"command": ["uvx", "mcp-server-fetch"], "options": ["--allow-pypi", "--egress-all-allow"]}
}
```

`command` runs inside the server sandbox; `options` are launcher options for
it (network, mounts; `--backend` and `--podman-url` are the agent's and cannot be set); `workspace` is
mounted at `/work` (default: an empty private directory); `agent` picks the
image (default `codex`; every image has Node.js, Python and uv). Agent logins,
Git credentials and identity, sessions, tmux, `--binary` and nested `--mcp` are
not allowed in a definition; an API key the server needs comes in with
`--secret-env NAME` (or `--env`), and only that variable.
A server that downloads its package at start (`npx`, `uvx`) needs the matching
registry allowed. Paths in a definition (`workspace`, `--mount` ...) must be absolute
or start with `~`: a running server is shared by every agent that uses it.

### Servers from an MCP catalog

Ready-made servers come as container images from
[sandbox-ai-mcp](https://github.com/c0decave/sandbox-ai-mcp) (search and web: DuckDuckGo, Brave
Search, web pages as text; Ghidra and Playwright prepared) and
[axross-mcp-hub](https://github.com/c0decave/axross-mcp-hub) (security tools: radare2, capa,
binwalk, YARA, tshark, Nmap, nuclei and more). Both are audited and pinned by digest.

```bash
sandbox-ai mcp catalog                               # entries of both catalogs
sandbox-ai mcp add duckduckgo --network full         # pull by digest, write the definition
sandbox-ai mcp add brave-search --network full --env BRAVE_API_KEY
sandbox-ai mcp add hub:binwalk --mount /work=~/firmware:ro --mount /out=~/extracted:rw
sandbox-ai claude --mcp duckduckgo --mcp binwalk
```

`add` refuses an entry without a digest, pulls `IMAGE@DIGEST` and writes a definition with
`"image"`, the network, the folders (`-m TARGET=HOST[:ro|rw]`) and the names of variables to pass
(`-e NAME`; the value comes from your environment when the server starts and is never stored).
Each agent connection starts a container with the hardened run line both projects test their
images with: `podman run --rm -i --init --pull=never --cap-drop=ALL
--security-opt=no-new-privileges --read-only`, a small `noexec` /tmp, `--memory=512m`
(`-M` changes it), `--pids-limit=128` (`-p`), `--network=none` or the same private pasta network
the agents get. A catalog entry that needs only some hosts (an *allowlist*) gets the full outside
network only when you add it with `--network full`: sandbox-ai cannot limit a server to hosts
yet. Your agent's own sandbox keeps its firewall: the server has the network or the folders,
the agent only the socket. But the agent decides what the server does: a fetch or browser
server with network sends whatever the agent asks it to, past the agent's firewall, proxy and
approvals. Give a server network only where you would give it to the agent. At most 16
connections to one server run at once. The host folders of an image server
pass the same checks as the agent's mounts: no symlink a sandbox may have
planted on the way, no private location for `rw`, and not inside a tree an
agent can write; its `rw` folders are recorded like workspaces.

## Project configuration and wizard

```bash
./sandbox-wizard                  # interactive setup, writes .sandbox-ai.json
./sandbox-wizard edit [DIR]       # change the existing configuration of a project
./sandbox-wizard trust FILE       # review and trust a configuration
./sandbox-wizard forget FILE      # remove it from the trusted list
```

The wizard asks for the project directory, extra workspaces, agent, backend,
network (or a local LLM for OpenCode), permission-free mode, GUI, tmux and
logs. Network is one of `default` (the agent's provider only), `development`
(plus PyPI, GitHub and the OpenAI and Anthropic APIs), `frontier`, `dns`,
`egress-all` or `offline`. Answers can be edited like in a shell (Backspace,
arrow keys, Ctrl-A/E/U, history with Up), and Tab completes paths. Three
optional blocks follow, each behind one yes/no question:

- **Further network destinations**: presets (`--allow-github`, ...),
  `--allow-host`, `--allow-tcp`, `--allow` and, with Podman,
  `--allow-host-port` (a service on this host, e.g. a local Gitea).
- **Git**: `--git-identity` and push tokens for github.com and your own
  servers (e.g. a local Gitea). Type the token (input hidden; it goes to a
  private file under `~/.config/sandbox-ai/tokens/`) or name a host variable
  (default `SANDBOX_GH_TOKEN`, or `GH_TOKEN`/`GITHUB_TOKEN` if set). It offers a token for the project's `origin` remote,
  read as text from `.git/config` without running git, only behind an
  explicit yes (default no): the agent can rewrite that file. Only variable
  names are stored, never tokens; the wizard notes variables that are not
  set yet.
- **Further options**: `--workspace-ro`, `--state-dir`, `--mount`, `--env`,
  `--mcp`, `--gui-screen`, `--gui-vnc`, and a free field for any other
  launcher option, checked by the launcher's own parser.

**Editing.** `sandbox-wizard edit [DIR|FILE]` (or `new` on a project that has a
configuration, answering `edit`) loads the existing `.sandbox-ai.json` and
shows its parts as a menu: agent, backend, workspaces, network, behaviour
(permission prompts, display, tmux, logging), network extras, Git, further
options, agent arguments, and all launcher options as one line for experts.
Each part asks its questions again with the current values as defaults: Enter
keeps a value, `-` empties a list, and in a terminal a line of options is shown
ready to edit. Existing push tokens can be kept one by one; options the wizard
does not ask about stay in the free options line; description, agent arguments
and MCP definitions are kept. Before writing, the wizard shows the new file and
what changed (`- old option`, `+ new option`). A file that is not trusted (new,
or changed since: the agent can write it) is shown in full first and used only
after an explicit yes; writing then trusts exactly the reviewed result.

Every entry is validated as it is typed, and the finished configuration
through the launcher's checks; if it would refuse to start (for example
because a token variable is not exported yet), the wizard says why before
writing. It then shows the result and writes `.sandbox-ai.json` into the
project. For a
local LLM, the OpenCode configuration goes to
`~/.config/sandbox-ai/opencode/`, outside the workspace: the launcher refuses
`--local-config`, `--copy`, `--auth-file` and `--mount` sources the agent
could replace with a symlink.

A launcher then reads `.sandbox-ai.json` from the main workspace automatically;
`--config FILE` selects another file and `--no-config` ignores it. Options with
a value given on the command line override the file's, an auth choice
(`--copy-auth`, `--no-auth`, `--auth-file`, `--import-codex-auth`) replaces the
file's, and agent arguments after `--` replace the saved ones; a switch the file
sets (e.g. `--offline`) cannot be unset from the command line, so use
`--no-config` for that. **A project file only takes effect once you trust it:** a cloned
repository could otherwise widen the sandbox (`--egress-all-allow`,
`--mount ~/.ssh:…`). The SHA-256 of each trusted file is kept in
`~/.config/sandbox-ai/trusted.json`; the wizard trusts what it writes, and any
change requires `sandbox-wizard trust` again, which trusts exactly the bytes it
showed you. The file is hashed and parsed from a single read, must be a
regular file and is refused if it is a symlink. Deleting a trusted file does
not silently drop its restrictions: the launcher stops with an error until it is
restored, `--no-config` is given, or `sandbox-wizard forget FILE` removes it from
the trusted list. The file cannot set `--workspace` (the main workspace is where
the file lives), `--session`, `--config` or internal options, and option names
must be written in full (no abbreviations). Paths (`workspaces`, `--mount`,
`--state-dir`, token files ...) must be absolute or start with `~`, so the file
means the same wherever the launcher is started.

```json
{
  "version": 1,
  "agent": "opencode",
  "options": ["--local-config", "/home/me/.config/sandbox-ai/opencode/app-1a2b3c4d5e6f.json",
              "--allow-tcp", "192.168.1.20:11434", "--no-tmux"],
  "workspaces": ["/home/me/src/shared-lib"],
  "agent_args": []
}
```

## Sessions: detach, pause, resume, stop

```bash
./claude-sandbox --workspace "$HOME/src/app" --session app    # start and attach
# Ctrl-b d detaches; the agent keeps working, even if the terminal closes.
./sandbox-session list
./sandbox-session attach app
./sandbox-session pause app      # freeze every process
./sandbox-session resume app     # thaw; or restart a stopped session
./sandbox-session stop app       # stop; state is kept
./sandbox-session delete app --yes
```

A session runs the launcher as a background supervisor; inside the sandbox a
detached tmux session holds window `agent` and window `shell` (bash, same
sandbox). State lives in `~/.local/state/sandbox-ai/sessions/NAME/`: `home/`
(persistent HOME), `tmux/` (socket), `session.json`, `supervisor.log`.

- **pause** freezes the whole Podman pod (cgroup freezer) or, with Bubblewrap,
  sends SIGSTOP to every process of the sandbox tree; **resume** thaws it.
- **stop** sends SIGTERM to the supervisor, which runs the normal cleanup,
  including the Claude credential write-back. Workspace and HOME stay.
- **resume** of a stopped session (or starting `--session NAME` again)
  restarts it with its saved options, and the agent continues its last
  conversation: `claude --continue`, `codex resume --last`,
  `opencode --continue`. With `--binary` the original arguments are reused.
  The original agent options (from the command line, else the project file's
  `agent_args`, e.g. `--model sonnet` or Codex's `--sandbox read-only`) are
  kept before the continue arguments, up to the first prompt; prompts,
  one-shot tasks (`-p`, `exec`, `run`) and conversation choices (`--resume`)
  are not repeated. If the first start never came
  up, the original arguments are used again.
  Options and agent arguments given when restarting are not applied; the
  launcher warns and names them. Use a new session name for other options.
  The project's `.sandbox-ai.json` is read again at every start. Commands
  that change a session (start, resume, pause, stop, delete) hold a lock, so a
  second one at the same time fails with a message instead of launching twice;
  a second agent starting the same MCP server waits and then shares it.
- **After a crash, `kill -9` or a reboot** the supervisor could not remove
  its Podman pod. The next resume removes it, but only if the pod carries
  this session's label (`sandbox-ai.session`); any other pod of that name,
  for example from a session under another `XDG_STATE_HOME` or from a
  version before the label, is left alone and the error names the
  `podman pod rm --force` command.

What survives a stop is `home/` and the workspaces: conversations, logins,
settings and anything installed below `/root`. Running processes, `/tmp`,
tmux scrollback and files written elsewhere in the read-only container are
gone. "Last conversation" means the most recent one in that HOME, so one
started by hand in the `shell` window takes precedence.

Attaching never runs a tmux client on the host: the tmux server belongs to the
agent and could tell a connected client to execute commands. Instead a relay
inside the sandbox, behind Firejail, runs `tmux attach` on its own
pseudo-terminal, and `sandbox-session attach` only exchanges terminal bytes and
the window size with it over `tmux/attach.sock`. `--session` implies tmux and
cannot be combined with `--state-dir` or `--probe`.

## tmux mode

tmux is the default: whenever stdin and stdout are a terminal, the agent runs
in tmux inside the sandbox with window `agent` and window `shell`, both behind
the same Firejail and firewall. When the agent exits, the shell stays; leaving
the last window ends the sandbox. It is used only when the agent arguments are
options alone (`claude`, `claude --model opus`); a subcommand, prompt or
print-and-exit option (`claude -p`, `claude mcp list`, `codex exec`,
`opencode run`, `--version`) runs without tmux, so its output and exit status
stay visible. It also stays off for probes, MCP servers, sessions (tmux
already), `--binary` and with `--backend bwrap` when the host has no tmux.
`--no-tmux` switches it off, `--tmux` insists (and fails without a terminal).
Use sessions for detaching.

## GUI apps and browsers

| Option | Effect |
|---|---|
| `-gu`, `--gui` | Virtual X display `:99` (Xvfb); `DISPLAY` is set, `/dev/shm` enlarged for Chromium. |
| `-gs`, `--gui-screen WxHxD` | Screen size, default `1920x1080x24`. |
| `-gv`, `--gui-vnc DIR` | Implies `--gui`; VNC on the Unix socket `DIR/vnc.sock` (mode `0700`, no TCP). |
| `-gw`, `--gui-wayland` | Expose the host's Wayland socket: windows appear on your desktop. |

Xvfb and x11vnc run in the supervisor with every capability removed; the VNC
directory is hidden from the agent and must lie outside workspaces. View the
display with a client that speaks Unix sockets, e.g.
`vncviewer DIR/vnc.sock` (TigerVNC) or
`socat TCP-LISTEN:5901,bind=127.0.0.1 UNIX-CONNECT:DIR/vnc.sock` plus any
viewer on `127.0.0.1:5901`. `--gui-wayland` gives the agent access to your
compositor and prints a warning; X11 passthrough is deliberately not offered,
because X11 exposes the keystrokes and screen of every host window.

Browsers are not in the image; Playwright fetches them (network needed):

```bash
./codex-sandbox --workspace "$HOME/src/app" --gui --egress-all-allow \
  --state-dir "$HOME/.local/state/sandbox-ai/pw" --binary /bin/bash
# inside: uvx --from playwright playwright install chromium
#         uv run --with playwright python my_browser_test.py   # headless=False works
```

## Hardware: devices and GPUs

Out of the box a sandbox sees no host hardware: Podman and Bubblewrap give it
its own minimal `/dev`, and `--mount` refuses device files. There are three
ways to give the agent hardware, the safest first:

| Way | How | Isolation | Works out of the box? |
|---|---|---|---|
| 1. Service on the host | The device stays on the host; a program there offers it over TCP, the sandbox may reach that port | No host driver reachable, replugging does not matter | You run and set up the service |
| 2. `--device PATH` | The device file itself | The agent talks to the device's driver in your host's kernel | Often (see table below) |
| 3. `--gpu nvidia\|dri\|rocm` | All GPU devices in one option | As `--device` | Yes for NVIDIA with CDI; Intel/AMD need GPU libraries in the image |

`sandbox-ai claude --help` sums this up at its end.

### 1. A service on the host (safest)

The agent gets a network port, not a device. With Podman,
`--allow-host-port PORT` makes `127.0.0.1:PORT` in the sandbox reach the same
port on the host's loopback; with Bubblewrap use `--allow-tcp HOST_IP:PORT`.

| Hardware | On the host | In the sandbox |
|---|---|---|
| Serial port (microcontroller) | `ser2net` with RFC 2217, or `socat TCP-LISTEN:4000,bind=127.0.0.1,fork,reuseaddr FILE:/dev/ttyACM0,raw,echo=0` | `--allow-host-port 4000`; esptool and pyserial speak `rfc2217://127.0.0.1:4000` (ser2net) or `socket://127.0.0.1:4000` (socat) |
| Debug probe (JTAG/SWD) | `openocd` (GDB port 3333) or a GDB server | `--allow-host-port 3333`; `target extended-remote 127.0.0.1:3333` |
| Android device | `adb start-server` (port 5037) | `--allow-host-port 5037`; `adb -H 127.0.0.1 -P 5037 ...` |
| GPU for models | Ollama, llama.cpp or vLLM | `--allow-host-port 11434` and similar, see [Local LLMs](#local-llms-with-opencode) |

Only the protocol of that service reaches the host. The device can be
replugged at will, because the service, not the sandbox, opens it.

### 2. `--device PATH`

```bash
sandbox-ai claude --device /dev/serial/by-id/usb-Raspberry_Pi_Pico_E660-if00
sandbox-ai claude --device /dev/kvm            # emulators, QEMU
sandbox-ai claude --device /dev/bus/usb        # raw USB, devices plugged in later appear too
```

- Accepted are character devices under `/dev` and the directories
  `/dev/bus/usb`, `/dev/dri` and `/dev/snd` (or one below them). Symlinks
  such as `/dev/serial/by-id/...` are resolved; the agent sees the real name
  (`/dev/ttyACM0`).
- Always refused: block devices (disks), `/dev/mem`, `/dev/kmem`, `/dev/port`,
  consoles (`/dev/console`, `/dev/tty`, `/dev/tty1`, `/dev/vcs*`), input
  devices (`/dev/input/*`, `/dev/uinput`, `/dev/hidraw*`, which are also
  security keys), raw disk access (`/dev/sg*`, `/dev/nvme0`, `/dev/vfio`,
  `/dev/mapper/control`, `/dev/loop-control`), host internals (`/dev/kmsg`,
  watchdog, TPM, Intel ME, `/dev/cpu`, MTD flash) and channels around the
  firewall (`/dev/vsock`, `/dev/vhost-*`).
- Cameras (`/dev/video*`) and sound (`/dev/snd`) are passed with a warning:
  the agent can take pictures and record through your microphones.
- The start lists every device. With Podman single devices become
  `--device`, directories bind mounts; with Bubblewrap both become
  `--dev-bind`.

**Access rights.** The sandbox keeps none of your supplementary groups
(Firejail drops them). The agent can open a device only if your user may as
owner, through its primary group, as anyone, or through an ACL entry, which a
desktop session adds for devices udev tags `uaccess`. The launcher checks this
and warns otherwise.

| Device | Usual rights | Out of the box? |
|---|---|---|
| `/dev/kvm` | `0666` (systemd) | Yes |
| `/dev/dri/renderD*`, `/dev/kfd` | `0666` (systemd, `kfd` on newer versions) | Yes |
| `/dev/nvidia*` | `0666` (NVIDIA driver) | Yes |
| `/dev/video*`, `/dev/snd/*` | ACL for the user at the desktop (`uaccess`) | At the desktop, not over SSH |
| `/dev/ttyUSB*`, `/dev/ttyACM*` | Group `dialout` or `uucp` | No: udev rule |
| `/dev/bus/usb/...` | `0664` root, ACL for some devices | Depends on the device's udev rules |

A udev rule gives your user one device (find the IDs with `lsusb`):

```bash
# /etc/udev/rules.d/70-sandbox-ai.rules (before 73-seat-late.rules, which applies uaccess)
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000a", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="3748", TAG+="uaccess"
# on a server without a desktop session instead: ..., OWNER="youruser"
```

Then run `sudo udevadm control --reload && sudo udevadm trigger` and replug
the device.

**Replugging.** A single device is bound once at the start. After replugging
under the same name it is the same device number and usually works again;
under a new name (`ttyACM1`, a bootloader mode) the sandbox does not see it.
Directories (`/dev/bus/usb`, `/dev/dri`, `/dev/snd`) are bound whole, so
devices plugged in later appear in them.

**What it costs.** Every device is kernel code on your host that the agent
can call, past both sandbox layers. Raw USB (`/dev/bus/usb`) reaches every USB
device your user may open, including ones plugged in later. A modem, Wi-Fi
adapter or SDR can also send data past the egress firewall. Pass only what the
task needs, and prefer way 1.

### 3. `--gpu`

| Value | Podman | Bubblewrap | GPU libraries |
|---|---|---|---|
| `nvidia` | CDI device `nvidia.com/gpu=all`; first `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` (NVIDIA Container Toolkit) | `/dev/nvidia0...`, `/dev/nvidiactl`, `/dev/nvidia-uvm*`, `/dev/nvidia-modeset` | CDI adds the driver's libraries and `nvidia-smi`; with Bubblewrap they come from the host's `/usr` |
| `dri` | `/dev/dri` (Intel/AMD render nodes) | as Podman | Mesa, Vulkan, VA-API or OpenCL are not in the images: build your own on top and start it with `--image` |
| `rocm` | `/dev/dri` and `/dev/kfd` | as Podman | ROCm likewise in your own image |

```dockerfile
# Containerfile.gpu, built with: podman build -t localhost/claude-gpu -f Containerfile.gpu .
FROM localhost/claude-sandbox:latest
RUN apt-get update && apt-get install -y --no-install-recommends \
      mesa-vulkan-drivers mesa-opencl-icd vainfo && rm -rf /var/lib/apt/lists/*
```

`sandbox-ai claude --gpu dri --image localhost/claude-gpu`. With
`--podman-url` only `--gpu nvidia` is possible (the remote's CDI); `--device`
and `--gpu dri|rocm` name devices of this host.

## Permission-free agents

`--dangerously-skip-permissions` starts the agent without approval prompts:
Codex gets `--dangerously-bypass-approvals-and-sandbox`, Claude
`--dangerously-skip-permissions` (plus `IS_SANDBOX=1`, which Claude requires as
root), OpenCode `OPENCODE_PERMISSION={"*":"allow"}`. Only the agent's own
prompts disappear; Podman/Bubblewrap, Firejail and the firewall remain.

## Egress and file access logs

`--log-dir DIR` (mode `0700`, outside workspaces) writes JSON lines (the
proxy's `connections-*.jsonl` is described in
[Connection proxy](#connection-proxy); without `--log-dir` it, and the
egress log, go to `~/.local/state/sandbox-ai/logs/PROJECT-HASH`, but file
access is not logged):

- `egress-denied.jsonl`: each dropped destination (address, TCP/UDP, port;
  first occurrence) and each lookup of a name that is not allowed. With
  logging, lookups go to a stub on `127.0.0.1:53` inside the sandbox that
  records the name and answers NXDOMAIN.
- `processes.jsonl`: every process that appears in the sandbox with its
  PID, parent and command line (polled twice a second, so very short-lived
  processes can be missed). Written whenever a log directory is in use,
  including the default one. Command lines are redacted like the connection
  log (token formats, passwords in URLs, `--token`/`--password`-style
  options) and by value for every `--env` and `--secret-env` variable;
  the file stops at 256 MiB.
- `files.jsonl`: `read`, `write`, `create`, `delete`, `move-from`, `move-to`
  under the workspaces, `/root`, `/tmp` and every mount (inotify; repeats per
  path collapsed for two seconds). inotify has no process ID; `/usr` and
  `/etc` reads are not recorded. Watches count against the host user's
  `fs.inotify.max_user_watches`; when exhausted, one error is logged.

The collectors run in the supervisor outside Firejail; the directory is
hidden from the agent. With real DNS (`--dns`) names resolve, so they no
longer appear as denied lookups. Logs are not size-limited.

`sandbox-ai logs` (default: the wizard's log directory of the current project)
counts blocked targets and file events and proposes, for each blocked target,
the option that would allow it: a preset for known hosts, `--profile frontier`
for model APIs, `--allow-host` for other names, `--allow-tcp IP:PORT`,
`--allow-host-port` for this host's loopback, or `--allow IP`. Every
suggestion widens the sandbox, so review it first. Names and paths come from
the agent: they are shown with every control character escaped, and a name
that is not a valid host name gets no suggestion.

It also summarizes what the connection log and `processes.jsonl` recorded:
HTTP requests per host, pushes checked and refused, model API calls with the
token usage they reported (Anthropic and OpenAI-style responses, streamed or
not), which credential headers went to which host (names only; values are
redacted in the log), refused connections, the programs started, and the tool
calls of the MCP servers started with `sandbox-mcp`.

## Privacy check before publishing

An agent that may push can publish what was never meant to leave your
machine: your home path in a config file, the address of your LAN model
server, the name of your account on your own Git server, a token. sandbox-ai
looks for these **before** they are published, in three places:

- **After every run**, on the host: for each writable workspace that is a Git
  repository with a public remote, it shows what the next push would publish
  and should stay private. A remote is public unless it is a local path,
  on this machine's loopback, in a private network (RFC 1918, `.lan`,
  `.local`, ...) or one of your own Git servers from `--git-token-*` or
  `--git-credentials`.
- **In the sandbox**, as Git hooks for the agent: `pre-commit` warns about
  staged changes, `pre-push` to a public remote **refuses** blocking findings
  and tells the agent to remove them or ask you. Afterwards the repository's
  own hook runs (`core.hooksPath` of the repository or `.git/hooks`).
- **By hand**: `sandbox-ai privacy-check [PATH ...]`, also for repositories
  you push from the host.

"What the next push would publish" is: commits on local branches that no
remote-tracking branch has (the lines they add, author, committer and
message), staged changes, tracked files changed in the work tree and
untracked files Git does not ignore. Lines that were there before are not
reported again; `privacy-check --all` checks every file.

The rules come from three sources:

| Source | What it finds | Severity |
|---|---|---|
| built-in | private keys; AWS, GitHub, GitLab, Anthropic, OpenAI, Slack, Google, Stripe, npm, PyPI, Hugging Face tokens | block |
| derived from this run | your host HOME path, private addresses from `--allow-tcp`, your account names on your own Git servers, the tokens you hand over (never shown, only masked) | block |
| | your git e-mail address (not a noreply one), your machine's name, your own Git server on this machine | warn |
| your own files | `global.rules.toml` and `PROJECT.rules.toml` (or `.json`; `PROJECT` is the workspace's directory name) in `--privacy-rules DIR`, default `~/.config/sandbox-ai/privacy` | as written |

Your own rule files use the format of the `scrub_guard` privacy gate:

```toml
[[pattern]]
id = "internal-codename"
regex = '(?i)\bbluefin\b'
severity = "block"            # or "warn"
reason = "codename of an unreleased product"

[[allow]]                     # a line this matches is not reported
id = "public-docs"
regex = 'bluefin-sdk'
path_glob = "docs/*"          # optional
reason = "the SDK's public name"
```

The directory has to lie outside every workspace, rw mount and `--state-dir`:
it holds the very strings it forbids, so it never enters the sandbox. The
agent's hooks get only the built-in and derived rules, without the tokens.
A rule file that cannot be read stops the start; TOML needs Python 3.11 or
the `tomli` package, JSON works everywhere.

`--privacy-check always` checks every writable Git workspace after the run,
`off` switches the check and the hooks off. `privacy-check` exits with 1 for
blocking findings and 2 when it cannot check (rules unreadable, not a
repository).

**Limits.** The hooks guard against accidents, not against an agent that
wants to publish: `git push --no-verify`, `git -c core.hooksPath=...`, the
GitHub API or `gh` bypass them. The check after the run comes after any such
push. Patterns find what they describe; a secret in an unknown format, in
binary files or in files over 1 MiB is not found. On the host the check runs
only Git plumbing commands that start no program from the repository's
configuration (no hooks, fsmonitor, filters or textconv) and reads work tree
files without following symlinks.

## Connection proxy

Every TCP connection of the agent passes a proxy that sandbox-ai starts with
the sandbox. It is on by default; `--no-proxy` switches it off (and
`--offline` has nothing to proxy).

- **Transparent:** the firewall redirects each connection (nftables NAT in the
  sandbox's network namespace). Programs need no proxy settings. The proxy
  connects to the original destination itself, still behind the firewall's
  allow-list, and refuses at once what the network options do not allow.
- **TLS is opened** with a CA made for this run alone (key and certificates
  stay in the proxy's private directory and disappear with the run). The
  agent trusts that CA through the system bundle and through `SSL_CERT_FILE`,
  `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `PIP_CERT`,
  `CARGO_HTTP_CAINFO`, `NODE_EXTRA_CA_CERTS` and similar variables. The proxy
  checks the real server against the system's CAs. Only HTTP/1.1 is offered;
  WebSockets work, QUIC/HTTP3 (UDP) is not proxied.
- **Logged:** every HTTP request with method, full URL, status, headers and
  bodies (first 64 KiB of each, gzip/deflate unpacked), every other connection
  with destination, bytes and duration, and everything refused. Credentials are
  redacted: `Authorization`, cookies, headers named like keys, tokens or
  secrets, token-like query parameters and JSON fields, and known token
  formats. The log is `connections-TIME-RUN.jsonl` in the log directory:
  `--log-dir`, else `~/.local/state/sandbox-ai/logs/PROJECT-HASH`. Each run
  writes its own file; the oldest are deleted when they exceed 1 GiB. After the
  run sandbox-ai prints a summary.
- **Push check on the network:** a Git push over HTTP(S) is held until the
  privacy check has seen it. The proxy reads the ref updates, a checker
  (without any capability, outside Firejail) finds the commits in the
  workspaces and scans what the remote does not have yet, using the full rules
  including your own (which the agent never sees). A blocking finding refuses
  the push the way a Git server would, with the findings shown as `remote:`
  lines. Hooks, `--no-verify` or another `core.hooksPath` make no difference.
  A push the checker cannot check (commits from a clone outside the workspaces,
  an unknown format) is refused too.
- **SSH** is refused except to ranges allowed with `--allow` and to the port
  given with `--allow-tcp IP:PORT` (at the start or with `sandbox-ai net
  allow`), never to a public Git host or into a range that contains one, and
  never to what was only approved, because a push over SSH would bypass the
  check. With `--privacy-check off` neither pushes nor
  SSH are checked.

The push check finds what its rules describe; it stops accidental
publication, not a determined agent: data deliberately encoded (base64, an
archive, encryption) passes any pattern check. The connection log records
what was sent either way.

Programs that pin certificates or bring their own CA list reject the session
CA; the proxy logs such a connection as `tls-refused` with a hint. Start with
`--no-inspect HOST` (or `--no-inspect '*.example.org'`) to pass TLS to that
host through unopened: only its name and the bytes are logged, and pushes to it
are not checked, so public Git hosts cannot be exempted while the privacy check
is on. The same applies to servers whose certificate the system does not trust
(an internal server with its own CA): `tls-upstream-failed` in the log.

**Requests per host.** Allowing a host allows every request to it: with
`--allow-github` an agent could also create gists or issues through
`api.github.com`. `--allow-http 'HOST METHODS PATH'` narrows that: once a host
(or `*.DOMAIN`) has rules, only matching requests reach it, everything else gets
a `403` the agent can read and a `denied` line in the log. In the path, `*`
matches within one segment and `**` across segments; paths are compared as the
server resolves them: decoded, without the query and `;parameters`, with `..`
and repeated slashes resolved. A request with two `Host` headers is refused.

```bash
claude-sandbox --allow-github \
  --allow-http 'api.github.com GET,HEAD /**' \
  --allow-http 'api.github.com POST /repos/me/app/pulls'
```

Rules hold for every name a request could reach: the verified server name, the
`Host` header (one certificate often covers many names, and servers such as
GitHub route by `Host`) and the host's pinned addresses, so another name or the
bare IP is no way around them; a `Host` other than the verified name is refused
where rules apply. They need the proxy, and a host under `--no-inspect`
cannot have them.

| Logged as | Meaning |
|---|---|
| `http` | One request and its response (`push` holds the check's verdict) |
| `tls-passthrough` | TLS passed through unopened (`--no-inspect`) |
| `tls`, `tcp` | Other protocols, inside TLS or plain |
| `upgraded` | A WebSocket after the upgrade |
| `denied` | Not allowed by the network options, or SSH refused |
| `tls-refused`, `tls-upstream-failed` | The program rejected the session CA, or the server's certificate is not trusted |

The proxy runs as its own user in the sandbox, without capabilities and with
`no_new_privs`; the firewall's redirect skips only that user. It parses what
the agent sends but stays inside the sandbox's namespaces. It needs the
nftables NAT modules on the host (`nft_chain_nat`, `nft_redir`; loaded on most
systems, otherwise `sudo modprobe -a nft_chain_nat nft_redir`) and `openssl` in
the image (included) or, with Bubblewrap, on the host.

## Approvals and browser requests

With the proxy on, a sandbox asks you instead of failing silently (`--ask on`,
the default):

- a **connection** to a destination the network options do not allow (held
  until you answer). The question shows the address, the protocol and the
  server name the agent sent in TLS or the `Host` header. That name is the
  agent's claim and is not verified: what you allow is the address, for any
  protocol except SSH;
- a **lookup** of a name that is not pinned (refused at once; once you allow
  it, sandbox-ai resolves the name on the host, pins its public addresses and
  opens HTTPS to them, so the agent's next try works; a name that resolves
  only to private, loopback or link-local addresses is refused, allow those
  explicitly with `sandbox-ai net allow`);
- a **push** that the privacy check passes but has warnings about (pushes with
  blocking findings are refused without asking);
- **opening a URL** in your browser: `xdg-open`, `sensible-browser`,
  `x-www-browser`, `www-browser`, `open` and `$BROWSER` in the sandbox ask the
  host to open http(s) URLs, one question per URL (for example the login page
  of a tool). Nothing opens without your answer; the program in the sandbox
  only learns that you were asked.

Every question shows up as a desktop notification (`notify-send`, when
available; its title names the asking sandbox: run, workspace and session),
in the app, and in:

```sh
sandbox-ai approvals list                     # ID  RUN  what is asked
sandbox-ai approvals allow ID --scope run     # once (default), run, always
sandbox-ai approvals deny ID [--scope once]   # default: not asked again in this run
sandbox-ai approvals history [RUN]            # questions and their answers
sandbox-ai approvals remembered [--json]      # what was allowed always, per project
sandbox-ai approvals forget PROJECT ENTRY     # forget one (a host name or IP:PORT)
```

A question nobody answers is refused after 60 seconds and, like a deny, not
asked again in that run. `once` allows this one connection (the address is
open for 30 seconds), `run` until the sandbox ends, `always` also every later
start of the same project (the address, or for a lookup the name, is kept in
`~/.config/sandbox-ai/approved.json` once the sandbox has applied the answer;
remove entries there to forget them; the launcher names the ones it adds). URLs
with a user part (`https://github.com@evil.example/`) are refused, and every
URL question names the host it opens. A lookup allowed `once` stays pinned for
the run; a push is approved for the run at most. At most 20 questions are open
at a time and 500 are asked per run; beyond that the sandbox refuses without
asking. Everything the agent chose (names, URLs, refs) is shown with control
characters and non-ASCII escaped, so it cannot fake lines in the list. Telemetry hosts the agents contact in
the background (`statsig.com`, `sentry.io` and similar) are refused without
asking. `--ask off` refuses everything the options do not allow, as before;
`--no-proxy` and `--offline` ask nothing.

The questions live in the run's control directory (under `$XDG_RUNTIME_DIR`),
which the agent cannot see; only processes of your own user can answer.

## Secrets

`sandbox-ai secrets` keeps API keys and tokens in your system keyring (Secret
Service: GNOME Keyring, KWallet, KeePassXC; through `secret-tool`), or, when
there is none, in private files (`0600`) under
`~/.local/share/sandbox-ai/secrets` (`SANDBOX_AI_SECRETS=file` forces the
files).

```sh
sandbox-ai secrets set OPENROUTER_API_KEY        # value from stdin, never an argument
sandbox-ai secrets set GH_PUSH --from-env GH_TOKEN
sandbox-ai secrets list                          # names and where they are kept, never values
claude-sandbox --secret-env OPENROUTER_API_KEY    # the agent gets the variable
claude-sandbox --secret-env API_KEY=OPENROUTER_API_KEY --git-token-secret https://github.com=GH_PUSH
```

A sandbox gets a secret only when it is named: `--secret-env NAME[=SECRET]`
(an environment variable for the agent), `--git-token-secret URL=SECRET`
(like `--git-token-env`) or an MCP server's variable. Values are never
printed, not in the dry-run plan and not in logs (the proxy redacts them in
recorded requests as it does every credential).

## Resource limits

Every sandbox runs with limits on memory, CPU and processes:

| Option | Default | `none` |
|---|---|---|
| `-me`, `--memory SIZE` | `8g` | no memory limit |
| `-cu`, `--cpus N` | half of the host's CPUs | no CPU limit |
| `-pi`, `--pids N` | `4096` | no process limit |

Podman passes them to the container (`--memory`, `--cpus`, `--pids-limit`).
Bubblewrap runs in a transient systemd scope (`systemd-run --user --scope`).
Both need a systemd user session with delegated cgroup controllers, as on
every desktop; without one (a container, some servers) the sandbox starts
without limits and says so.

## Administrator policy

An administrator can fix what nobody on the machine may choose, in
`/etc/sandbox-ai/policy.json` (owned by root, writable by root only, in a
directory of the same kind; a broken or unsafe file stops every start):

```json
{
  "version": 1,
  "forbid": ["--egress-all-allow", "--no-proxy", "--privacy-check=off", "--ask=off"],
  "backends": ["podman"],
  "max_approval_scope": "run",
  "allow_within": ["10.0.0.0/8"],
  "allow_hosts": ["pypi.org", "files.pythonhosted.org", "*.corp.example"],
  "mcp_network": "none",
  "message": "see the developer handbook, section 4"
}
```

`forbid` lists options (`--name`, or `--name=value` for one value); they are
refused however they were chosen: on the command line, by a short form, a
trusted project file, a profile that implies them, or an MCP server's
definition. `backends` limits the backends. `max_approval_scope` caps answers to
questions (`once`, `run` or `always`); below `always`, nothing is remembered
for a project and remembered approvals are not applied. `allow_within` is a
network ceiling: every `--allow`, `--allow-tcp`, resolver (`--dns`,
`--nameserver`), host port (as `127.0.0.1`), approval and `sandbox-ai net
allow` must lie inside it, and `--egress-all-allow` is refused; named hosts
(`--allow-host`, presets, `--frontier`) must be listed in `allow_hosts`, apart
from the agent's own provider. Remembered approvals outside it are not applied. Changes during
a run (`sandbox-ai net allow`) obey `forbid` and the ceiling too: the sandbox's
supervisor checks them. `mcp_network: none` (implied by `allow_within`) refuses
MCP image servers with network. The policy binds
sandbox-ai as installed; it cannot stop a user who runs a modified copy of
their own, so it guards against convenience, project files and mistakes, not
against the machine's own users.

## Desktop app

[sandbox-ai-app](https://github.com/c0decave/sandbox-ai-app) is a graphical
front end (Python, Qt) for everything above: start sandboxes with every
option, see the running ones, follow their connections, answer approvals,
change their network, manage MCP servers, secrets, logins and images, browse
the files of HOME and the workspaces, and work in terminals of the sandboxes.
It only calls the `sandbox-ai` commands and reads their files; the sandboxes
do not depend on it.

## Option reference

`codex-sandbox`, `claude-sandbox` and `opencode-sandbox` share all options
below; `./claude-sandbox --help` prints them too. Arguments after `--` go to
the agent unchanged. Options marked *repeatable* may be given more than once.

Every option has a short form. One-letter forms can be bundled and take their
value directly: `claude-sandbox -Dn -w~/src/app` is
`--dangerously-skip-permissions --dry-run --workspace ~/src/app`. Two-letter
forms such as `-gl` (`--allow-gitlab`) are options of their own, not two
bundled letters. Project files and MCP definitions may use short forms too.
The other commands have short forms as well (`-V` version, `-j` JSON, `-y`
yes, `-b` backend; see their `--help`).

### Most important options

| Option | Purpose |
|---|---|
| `-w`, `--workspace DIR` | Project at `/work` (default: current directory) |
| `-s`, `--session NAME` | Detachable session; manage with `sandbox-session` |
| `-D`, `--dangerously-skip-permissions` | Agent without approval prompts; the sandbox stays in force |
| `-g`, `--allow-github`, `-y`, `--allow-pypi`, `-A`, `--allow-host HOST` | Open individual HTTPS destinations |
| `-P`, `--allow-host-port PORT` | Reach a service on the host's loopback, e.g. a local Git server |
| `-ge`, `--git-token-env URL=NAME`, `-gf`, `--git-token-file URL=FILE`, `-I`, `--git-identity` | Push over HTTP(S) from the sandbox, also to github.com |
| `-p`, `--profile development` | Provider, PyPI, GitHub and the OpenAI and Anthropic APIs |
| `-S`, `--state-dir DIR` | Keep the sandbox HOME between runs |
| `-o`, `--offline`, `-E`, `--egress-all-allow` | No network at all / every outbound destination |
| `-gu`, `--gui`, `-gv`, `--gui-vnc DIR` | Virtual display for GUI apps and browsers |
| `-l`, `--log-dir DIR` | Log denied egress and file access |
| `-n`, `--dry-run`, `-pb`, `--probe` | Show the plan / verify the isolation without an agent |

### Backend and image

| Option | Meaning |
|---|---|
| `-b`, `--backend podman\|bwrap` | Sandbox backend (default: `podman`) |
| `-i`, `--image IMAGE` | Prebuilt Podman image (default: `localhost/<agent>-sandbox:latest`); never pulled implicitly |
| `-pu`, `--podman-url URL` | Podman service URL, e.g. `unix:///example/podman.sock` |
| `-B`, `--binary PATH` | Agent executable (host path with bwrap, image path with Podman) |
| `-rt`, `--runtime DIR` | bwrap: expose a runtime directory read-only at the same path; repeatable |

See [Starting an agent](#starting-an-agent) and [Requirements](#requirements).

### Workspaces and mounts

| Option | Meaning |
|---|---|
| `-w`, `--workspace DIR[,DIR...]` | First directory at `/work`, further ones at `/workspaces/<name>`; repeatable |
| `-W`, `--workspace-ro` | Mount every workspace read-only |
| `-m`, `--mount SRC:DEST:ro\|rw` | Extra mount; `DEST` under `/work`, `/mnt` or `/opt`; repeatable |

See [Workspaces and mounts](#workspaces-and-mounts).

### Authentication, state and environment

| Option | Meaning |
|---|---|
| `-ca`, `--copy-auth` | Require copying this agent's credential files (login store, else host); default without it: copied when found and the sandbox HOME has no login yet |
| `-na`, `--no-auth` | Copy no host credentials |
| `-af`, `--auth-file FILE` | Copy this JSON file to the agent's standard auth path |
| `-ic`, `--import-codex-auth FILE` | OpenCode only: import a Codex ChatGPT `auth.json` as a private OAuth copy |
| `-cp`, `--copy SRC:HOME_RELATIVE_DEST` | Copy one config or credential file into the sandbox HOME; repeatable |
| `-S`, `--state-dir DIR` | Private persistent sandbox HOME (mode 0700); default: deleted after exit |
| `-e`, `--env NAME` | Pass one host variable to the agent only; values never printed; repeatable |
| `-se`, `--secret-env NAME[=SECRET]` | The agent gets NAME with the value of a stored secret (default: the secret NAME); repeatable ([secrets](#secrets)) |

See [Authentication and state](#authentication-and-state).

### Kits, project memory and plugins

| Option | Meaning |
|---|---|
| `-k`, `--kit NAME` | Also use the kit `~/.local/share/sandbox-ai/kits/NAME/AGENT`; repeatable |
| `-K`, `--kit-dir DIR` | Also use the kit in `DIR/AGENT`; repeatable |
| `-nk`, `--no-default-kit` | Leave out the default kit `~/.local/share/sandbox-ai/home/AGENT` |
| `-nm`, `--no-memory` | No project memory (memory file and Claude's auto memory neither loaded nor kept) |
| `-np`, `--no-plugins` | No plugins from the plugin store (Claude) or the kits (OpenCode) |

See [Kits, project memory and plugins](#kits-project-memory-and-plugins).

### Network

| Option | Meaning |
|---|---|
| `-p`, `--profile NAME` | `development`, `frontier`, `dns`, `egress-all` or `offline`; repeatable |
| `-f`, `--frontier` | Also allow OpenAI and Anthropic API/auth hosts (TCP/443) |
| `-nd`, `--no-provider-defaults` | Drop the agent's default hosts |
| `-y`, `--allow-pypi`, `-g`, `--allow-github`, `-gl`, `--allow-gitlab`, `-ak`, `--allow-kernel-org`, `-dd`, `--allow-duckduckgo` | Host presets (TCP/443) |
| `-A`, `--allow-host HOST` | Pin a host name and allow TCP/443; repeatable |
| `-a`, `--allow IP_OR_CIDR` | All protocols and ports to that destination; repeatable |
| `-t`, `--allow-tcp IP:PORT` | One TCP port on one address (IPv6: `[ADDR]:PORT`); repeatable |
| `-P`, `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in the sandbox reaches the same port on the host's loopback; repeatable |
| `-R`, `--dns` | Real DNS via `1.1.1.1` and `8.8.8.8` (UDP/TCP 53 to them only) |
| `-ns`, `--nameserver IP` | Use this resolver instead; implies `--dns`; repeatable |
| `-E`, `--egress-all-allow` | Allow every outbound destination; implies `--dns`; inbound stays closed |
| `-o`, `--offline` | No outside network, including provider defaults |
| `-nw`, `--network NAME`, `-ip`, `--ip IP`, `-i6`, `--ip6 IP` | Podman: existing private network and static addresses |
| `-X`, `--no-proxy` | No connection proxy: no connection log, pushes checked only by the hooks |
| `-x`, `--no-inspect HOST` | Pass TLS to HOST (or `*.DOMAIN`) through unopened; repeatable |
| `-ah`, `--allow-http 'HOST METHODS PATH'` | Only matching requests reach HOST (or `*.DOMAIN`); repeatable |
| `-Q`, `--ask on\|off` | Ask before what the options do not allow; unanswered after 60 s: refused (default: `on`; [approvals](#approvals-and-browser-requests)) |

See [Network policy](#network-policy) and [Connection proxy](#connection-proxy).

### Git

| Option | Meaning |
|---|---|
| `-ge`, `--git-token-env URL=NAME` | Token from host variable `NAME` for Git at `URL` (`http(s)://[USER@]HOST[:PORT]`); loopback URLs imply `--allow-host-port`, others `--allow-host`; repeatable |
| `-gf`, `--git-token-file URL=FILE` | Like `--git-token-env`, token read from a private file (mode `0600`, outside workspaces); repeatable |
| `-gc`, `--git-credentials FILE` | Every login in `FILE` (Git's credential store format, e.g. `~/.git-credentials`; private, outside workspaces), each with the network rules of `--git-token-env` |
| `-I`, `--git-identity` | Commit as the host's global `user.name` and `user.email` |
| `-pc`, `--privacy-check public\|always\|off` | After the run, show what the next push would publish that should stay private: of workspaces with a public remote (default), of every Git workspace, or never; unless `off`, the agent's `pre-commit` hook warns and `pre-push` to a public remote refuses |
| `-pr`, `--privacy-rules DIR` | Your own rules: `DIR/global.rules.toml` and `DIR/PROJECT.rules.toml` (or `.json`); outside workspaces, never shown to the agent; default `~/.config/sandbox-ai/privacy` if it exists |

See [Network policy](#network-policy) (pushing to GitHub and to your own Git server) and
[Privacy check before publishing](#privacy-check-before-publishing).

### Agent behaviour, MCP and local LLMs

| Option | Meaning |
|---|---|
| `-D`, `--dangerously-skip-permissions` | Start the agent without approval prompts |
| `-L`, `--local-config FILE` | OpenCode: local-only configuration, cloud access disabled |
| `-M`, `--mcp NAME` | Connect a local MCP server in its own sandbox; started if needed; repeatable |

See [Permission-free agents](#permission-free-agents),
[Local LLMs](#local-llms-with-opencode) and [Local MCP servers](#local-mcp-servers).

### Sessions, tmux and GUI

| Option | Meaning |
|---|---|
| `-s`, `--session NAME` | Named, detachable session in tmux |
| `-T`, `--tmux`, `-nt`, `--no-tmux` | tmux inside the sandbox (window `agent` plus window `shell`) is the default in a terminal; `--tmux` insists, `--no-tmux` switches it off |
| `-gu`, `--gui` | Virtual X display `:99` (Xvfb) |
| `-gs`, `--gui-screen WxHxDEPTH` | Virtual screen size (default: `1920x1080x24`) |
| `-gv`, `--gui-vnc DIR` | Implies `--gui`; VNC on the Unix socket `DIR/vnc.sock` |
| `-gw`, `--gui-wayland` | Expose the host's Wayland socket (weakens isolation) |

See [Sessions](#sessions-detach-pause-resume-stop), [tmux](#tmux-mode) and
[GUI](#gui-apps-and-browsers).

### Hardware

| Option | Meaning |
|---|---|
| `-d`, `--device PATH` | Hand a host device to the agent: a character device under `/dev` or `/dev/bus/usb`, `/dev/dri`, `/dev/snd` whole; repeatable (weakens isolation) |
| `-G`, `--gpu nvidia\|dri\|rocm` | The GPU devices (NVIDIA through CDI with Podman) |
| `-me`, `--memory SIZE`, `-cu`, `--cpus N`, `-pi`, `--pids N` | Resource limits (defaults `8g`, half the CPUs, `4096`; `none` for none; [limits](#resource-limits)) |

See [Hardware](#hardware-devices-and-gpus); a service on the host reached with
`--allow-host-port` is safer.

### Configuration, logs and checks

| Option | Meaning |
|---|---|
| `-c`, `--config FILE` | Project configuration (default: trusted `.sandbox-ai.json` in the main workspace) |
| `-nc`, `--no-config` | Ignore `.sandbox-ai.json` |
| `-l`, `--log-dir DIR` | Write `egress-denied.jsonl`, `files.jsonl` and `connections-*.jsonl` there (hidden from the agent); default for the connection log: `~/.local/state/sandbox-ai/logs/PROJECT-HASH` |
| `-n`, `--dry-run` | Print the plan; no DNS, auth reads, copies, pods or processes |
| `-pb`, `--probe` | Verify both sandbox layers and the firewall, then exit without agent |
| `-h`, `--help` | Full help |
| `-V`, `--version` | Version (every command has it) |

See [Project configuration](#project-configuration-and-wizard) and
[Logs](#egress-and-file-access-logs).

### Helper commands

| Command | Arguments |
|---|---|
| `sandbox-ai COMMAND …` | one command for all of the below, plus `doctor`, `logs [DIR\|--project P] [--json]`, `update [--check]`, `version` |
| `sandbox-ai privacy-check [PATH ...]` | `[--rules-dir DIR] [--project NAME] [--all] [--json]`: what the next push of each repository would publish that should stay private; exit 1 for blocking findings, 2 when it cannot check |
| `sandbox-ai net list\|show\|allow\|revoke [RUN] [--allow-host H] [--allow-tcp IP:PORT] [--allow NET] [--allow-PRESET]` | Change what a running sandbox may reach (see [Network policy](#changing-the-network-of-a-running-sandbox)) |
| `sandbox-ai approvals list\|history [RUN]`, `allow ID [--scope once\|run\|always]`, `deny ID` | Answer the questions of running sandboxes |
| `sandbox-ai secrets list\|set NAME [--from-env VAR]\|delete NAME\|backend` | Secrets in the system keyring, else private files |
| `sandbox-ai images [--json]`, `sandbox-ai images packages AGENT [--json]` | Agent images and their packages (read offline from the image) |
| `sandbox-build-image` | `[--podman-url URL] [--pin VERSION] [--uv VERSION] [--node IMAGE] AGENT\|all [podman build options]`; newest releases unless pinned |
| `sandbox-session list` | Sessions and their status |
| `sandbox-session attach\|pause\|resume\|stop NAME` | Attach (detach with `Ctrl-b d`), freeze, thaw or restart, stop |
| `sandbox-session delete NAME [--yes]` | Remove a stopped session and its HOME |
| `sandbox-mcp list` | Defined MCP servers and their status |
| `sandbox-mcp catalog`, `sandbox-mcp add NAME [-m TARGET=HOST[:ro\|rw]] [-e VAR] [-n none\|full] [-M SIZE] [-p N]` | Servers of the MCP catalogs; add one as a container server (see [Local MCP servers](#servers-from-an-mcp-catalog)) |
| `sandbox-mcp start NAME [--backend podman\|bwrap]`, `sandbox-mcp stop NAME` | Start or stop an MCP server sandbox |
| `sandbox-wizard [new]` | Interactive project setup; offers to edit an existing configuration |
| `sandbox-wizard edit [DIR\|FILE]` | Change the existing configuration of a project (menu, diff before writing) |
| `sandbox-ai login AGENT [launcher options] [-- login arguments]`, `--status`, `AGENT --logout` | Login store for sandboxes |
| `sandbox-ai plugins [claude [--update] [launcher options] [-- claude plugin arguments]] [--status]` | Plugin store for Claude sandboxes |
| `sandbox-wizard trust FILE [--yes]`, `sandbox-wizard forget FILE` | Trust a project configuration, or remove it from the trusted list |
| `sandbox-wizard models --kind ollama\|lmstudio\|vllm --host HOST [--port PORT] [--sort name\|size\|loaded]` | List the models of a local LLM server |

## Testing

```bash
# Unit, contract, matrix and end-to-end tests (no sandbox started)
python3 -m pytest tests/test_agent_profiles.py tests/test_agent_sandbox.py \
  tests/test_agent_sandbox_{contracts,matrix,edges}.py tests/test_sessions.py tests/test_wizard.py \
  tests/test_mcp.py -q

# Live tests: real Podman/Bubblewrap sandboxes
python3 -m pytest tests/test_agent_sandbox_live.py --agent-live -q
# … plus real model requests, internet downloads, a local LLM server
python3 -m pytest tests/test_agent_sandbox_live.py --agent-live --agent-provider-tests \
  --agent-network-tests --local-llm ollama:192.168.1.20:11434 -q
```

Every launcher function has happy, sad and edge contracts; a manifest test
rejects new functions without them. Matrices combine agents, backends, auth
modes and workspace states; the wizard end-to-end test drives the real
`sandbox-wizard` script against a local Ollama-compatible HTTP server and
checks the launcher plan that results; the MCP test carries real JSON-RPC
through the socket server and the bridge to the demo server with two
concurrent clients. Live tests cover isolation, auth copies
and mounts, the firewall in every mode, multiple workspaces, logs, GUI/VNC,
Wayland (when present), tmux, MCP servers in their own sandbox, the full
session lifecycle (pause really
freezes, resume continues, stop keeps state), image tools, and, opt-in,
practical model runs, Playwright and a local LLM. Options:
`--agent-backend podman|bwrap`, `--agent-image`, `--agent-podman-url`,
`--codex-model`, `--claude-model`, `--free-model`, `--opencode-model`.
Practical model runs write their reports to `evidence/` (not versioned, so
reports always belong to the code that produced them).

`tests/test_live_e2e.py` (also `--agent-live`) checks the features of 0.1.20
and later on a real host: the proxy behind the real NAT redirect with real TLS,
pushes to a Git server on the host refused and accepted, SSH refused, network
changes and approvals (connection, lookup, browser request) while a sandbox
runs, devices, MCP catalog containers, resource limits in the container's
cgroup, secrets as variables and the process log. The GitHub Actions workflow
`e2e` runs them, with the image builds, on Ubuntu 24.04 with rootless Podman
and with Bubblewrap; failures are written as annotations of the job.

## Troubleshooting

Start with `sandbox-ai doctor`: it checks every requirement and prints the
command that fixes each problem.

| Symptom | Check |
|---|---|
| Claude "not logged in" / "Login expired · Please run /login" | `sandbox-ai login --status` shows the login sandboxes get. `sandbox-ai login claude` gives them their own, independent of the host's (recommended when Claude also runs on the host: both refreshing one login logs one of them out). Refresh needs `console.anthropic.com`/`platform.claude.com`, allowed by default. |
| Login code or OAuth token rejected although copied correctly | In tmux the agent's interface takes the mouse, so a plain selection copies nothing or a wrapped piece: hold Shift while selecting (Option/Alt in some macOS terminals) and paste the code as one line. `sandbox-ai login claude` logs in without tmux. |
| `Project configuration … is not trusted` | Review the file, then `./sandbox-wizard trust FILE`, or start with `--no-config`. |
| Local LLM unreachable | Server bound to the LAN, host LAN IP used, `--allow-tcp IP:PORT` matches. |
| `Unknown MCP server` / server fails to start | `./sandbox-mcp list`; read `~/.local/state/sandbox-ai/mcp/NAME/supervisor.log`. |
| `-T`, `--tmux needs an interactive terminal` | Drop `--tmux` (tmux is the default in a terminal anyway) or use `--session` for background runs. |
| `The trusted project configuration … is missing` | Restore the file, start with `--no-config`, or `./sandbox-wizard forget FILE`. |
| `… is a symlink; refusing` | Project files and sandbox credentials must be regular files; replace the link. |
| `inotify watch limit reached` | Raise `fs.inotify.max_user_watches` or watch fewer directories. |
| Codex nested UID-map error | Another explicit Codex sandbox mode re-enables its native sandbox. |
| `newuidmap`/`newgidmap` fails | Helpers, delegated ranges, host policy. |
| Missing image | `./sandbox-build-image AGENT` or `--image`. |
| Copy target exists | Omit repeated copy options with a reused `--state-dir`. |

## Limits and remaining work

1. **P1 — State separation:** `/`, HOME, the temporary directory,
   `XDG_RUNTIME_DIR` and the launcher's own directory are refused as
   workspaces, but one `--state-dir` used by two runs at once is not locked.
2. **P2 — Host coverage:** the `e2e` workflow runs the live tests on Ubuntu
   24.04 with both backends; other distributions and kernels, and hardware
   (devices, GPUs, Wayland), are covered by unit and contract tests only.
3. **P2 — Operational bounds:** resource limits need a systemd user session;
   without one sandboxes start unlimited. `files.jsonl` and
   `egress-denied.jsonl` are not size-limited (the connection logs and
   `processes.jsonl` are).
4. **P2 — Claude lifecycle:** verify token refresh and write-back over a long
   session; account binding of written-back tokens is not verified.
5. **P3 — Reproducible builds:** builds take the newest releases by design;
   `--pin`, `--uv` and `--node` fix agent, uv and base image, apt packages are
   not pinned.

Passing functional tests is not a kernel-escape audit or a guarantee for every
host configuration. Firejail runs with `--noprofile` and the launcher's own
options; personal Firejail profiles are not loaded.
