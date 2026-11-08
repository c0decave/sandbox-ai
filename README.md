# sandbox-ai

[Deutsch](README_DE.md) · [Quickstart tutorial](QUICKSTART.md) · [Learning video](docs/video/README.md)

![The sandbox-ai desktop app: a sandbox put together on the Board](docs/screenshots/board.png)

**One control centre for AI agents: on this machine or on every host you manage, sealed off, watched, and asked
before anything leaves.**

1. **Every agent, one place.** Claude Code, Codex and OpenCode, each with its own login, plugins and MCP servers,
   started from the command line, the desktop app or the browser.
2. **Two sandbox layers, verified.** Rootless Podman or Bubblewrap, then Firejail inside; the run stops unless the
   namespaces, capabilities, NoNewPrivs and Seccomp are confirmed. No silent fallback.
3. **Nothing leaves unasked.** Default-deny network, per-host allows, and a question on your desktop for anything
   else: once, for the run, or always for the project.
4. **Secrets never enter the sandbox.** API keys, Git and package-registry logins are put in by the proxy on the
   wire; the agent only ever sees placeholders.
5. **Every push checked.** Each `git push` and every publication is scanned for secrets and private data before the
   server sees it.
6. **You see everything.** Connections, prompts, tool calls, files and processes per run, live, with the process
   behind each connection; decoy files tell you when an agent goes looking for credentials.
7. **Build setups visually.** Drag parts onto the Board (workspaces, network, MCP servers, LLM servers, VPN, devices,
   limits), get a security score and the exact command line, save it as your own preset.
8. **Many machines, one window.** Manage other hosts and LXC containers over SSH: their sandboxes, approvals,
   terminals and updates, from the app or the web interface with password and TOTP.
9. **Teams, helpers and long runs.** Detachable sessions you can attach to from anywhere, agent teams with a task
   board, local or remote LLMs, Tor and VPN profiles, GPUs and USB devices handed over by rule.
10. **Safe by default, strict on demand.** Security modes from `lax` to `paranoid`, rulesets, run taint and an
    administrator's policy, while the agent keeps working the way it always does.

## A look at the app

| | |
|---|---|
| ![Start a sandbox](docs/screenshots/launch.png) **Start**: pick the agent and options, see what will be open | ![The Board](docs/screenshots/board.png) **Board**: a sandbox as parts you drag and connect, with a security score |
| ![Sandboxes and sessions](docs/screenshots/sandboxes.png) **Sandboxes & sessions**: everything that runs, however it was started | ![Approvals](docs/screenshots/approvals.png) **Approvals**: what the agents ask, answered in one click |
| ![Connections](docs/screenshots/connections.png) **Connections**: every request with the process behind it | ![Activity](docs/screenshots/activity.png) **Activity**: prompts, tool calls, files and network per run |
| ![Terminals](docs/screenshots/terminals.png) **Terminals**: attach to any running session in a tab | ![Kits and plugins](docs/screenshots/kits.png) **Kits & plugins**: skills, plugins and hooks for every agent |
| ![Hosts](docs/screenshots/hosts.png) **Hosts**: other machines over SSH, in the same window | ![Help](docs/screenshots/help.png) **Help**: every page and concept explained, in English and German, with tours |
| ![Notifications in the theme](docs/screenshots/notifications.png) **Notifications** in your theme: warnings and errors in its colours | ![The web interface](docs/screenshots/web.png) **Web interface**: the same from a browser, with password and TOTP |

Screenshots show staged demo data (a project in a temporary folder and a run with two open questions).

### Use it in five minutes

```bash
curl -fsSL https://github.com/c0decave/sandbox-ai/releases/latest/download/install.sh | bash   # or the AppImage / .app
sandbox-ai doctor                      # checks the host: Podman or Bubblewrap, Firejail, nftables
sandbox-ai build claude                # the agent's image (claude, codex or opencode)
sandbox-ai claude -w ~/src/myproject   # the agent in its sandbox; Ctrl-b d detaches, it keeps working
sandbox-ai-app                         # the desktop app: Start, Board, Approvals, Terminals, Help
```

In the app: open the project folder, choose the agent on **Start** (or drag a setup together on the **Board**),
press **Start in the app**, and answer what the agent asks under **Approvals**. **Help → Tours** walks you through
your first start, your own preset, approvals and the rest.

Linux launchers that run **Codex**, **Claude Code** and **OpenCode** inside two
sandbox layers, with a default-deny network:

```text
Host user → rootless Podman pod or Bubblewrap → Firejail → agent
```

`sandbox-ai claude|codex|opencode` run the host launcher `sandbox/agent_sandbox.py`; only
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
run on Windows, or cannot use rootless Podman or Bubblewrap. On macOS the
sandboxes run in a rootless `podman machine`, so far with
[limits](#limits-and-remaining-work).

### Get it

```bash
git clone https://github.com/c0decave/sandbox-ai && cd sandbox-ai
./install.sh --from .      # commands in ~/.local/bin; update later: git pull && ./install.sh --from .
sandbox-ai doctor          # what is missing on this host, with the command that fixes it
sandbox-ai build claude    # the agent image (or: codex, opencode, all)
sandbox-ai login claude    # unless you are logged in to Claude Code on this host
cd ~/src/myproject && sandbox-ai claude
```

The [quickstart](QUICKSTART.md) walks through this in fifteen minutes; the [learning video](docs/video/README.md) shows the whole platform in fourteen chapters (English and German).

No clone needed: [sandbox-ai.pyz, the AppImage or the macOS app](#four-ways-to-install), ready to start.

## Contents

1. [Requirements](#requirements)
2. [Installation and updates](#installation-and-updates)
3. [The sandbox-ai command](#the-sandbox-ai-command)
4. [Images: build and update](#images-build-and-update)
5. [Starting an agent](#starting-an-agent)
6. [Security modes](#security-modes)
7. [Workspaces and mounts](#workspaces-and-mounts)
8. [Authentication and state](#authentication-and-state)
9. [Kits, project memory and plugins](#kits-project-memory-and-plugins)
10. [Network policy](#network-policy)
11. [VPN](#vpn)
12. [LLM servers: local and remote](#llm-servers-local-and-remote)
13. [Local MCP servers](#local-mcp-servers)
14. [Other hosts](#other-hosts)
15. [Project configuration and wizard](#project-configuration-and-wizard)
16. [Every start: names, detach, keep, resume](#every-start-names-detach-keep-resume)
17. [tmux mode](#tmux-mode)
18. [GUI apps and browsers](#gui-apps-and-browsers)
19. [Hardware: devices and GPUs](#hardware-devices-and-gpus)
20. [Permission-free agents](#permission-free-agents)
21. [Egress and file access logs](#egress-and-file-access-logs)
22. [Agent activity](#agent-activity)
23. [Telemetry of the agents](#telemetry-of-the-agents)
24. [Privacy check before publishing](#privacy-check-before-publishing)
25. [Connection proxy](#connection-proxy)
26. [Approvals and browser requests](#approvals-and-browser-requests)
27. [Rulesets](#rulesets)
28. [Canary files](#canary-files)
29. [Secrets](#secrets)
30. [Resource limits](#resource-limits)
31. [Administrator policy](#administrator-policy)
32. [Web interface](#web-interface)
33. [Desktop app](#desktop-app)
34. [Option reference](#option-reference)
35. [Testing](#testing)
36. [Troubleshooting](#troubleshooting)
37. [Limits and remaining work](#limits-and-remaining-work)

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

### Four ways to install

| Way | For | Needs on the host | Updates |
|---|---|---|---|
| `install.sh` | the command line and the app, kept up to date (recommended) | Bash, Python 3.9+, `readlink`; for the app PySide6 (the installer fetches it) | `sandbox-ai-update`, signed |
| `pip install sandbox-ai` | the command line in a Python environment | Python 3.9+ | `pip install -U sandbox-ai` |
| `sandbox-ai.pyz` | the command line in one file, nothing installed | Python 3.9+ | download the new file |
| AppImage (Linux) / `sandbox-ai.app` (macOS) | the app with the command line inside, its own Python and Qt | Linux: glibc 2.34+ (aarch64: 2.39+) and the desktop's OpenGL/EGL, xkbcommon, fontconfig libraries; macOS 14+ | download the new file |

Every way runs the sandboxes the same way and needs the same backend: on Linux rootless **Podman** or
**Bubblewrap with Firejail** (see [Requirements](#requirements)); on macOS **Podman** with a rootless
`podman machine` that shares `/Users` (`sandbox-ai doctor` checks it and prints the commands), and Homebrew's
Bash for `sandbox-ai build` (`brew install bash`). The release files are on the
[releases page](https://github.com/c0decave/sandbox-ai/releases); `latest/download/NAME` always names the newest.

**sandbox-ai.pyz** - one file, any Python 3.9 or newer:

```bash
mkdir -p ~/.local/bin && cd ~/.local/bin   # not into a project: an agent could rewrite it there
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/sandbox-ai.pyz
chmod +x sandbox-ai.pyz
./sandbox-ai.pyz doctor                       # or: python3 -I sandbox-ai.pyz doctor (any Python 3.9+)
./sandbox-ai.pyz build claude
./sandbox-ai.pyz claude -w ~/src/myproject
```

sandbox-ai refuses a workspace, an rw mount or `--state-dir` that holds the `.pyz` (or the AppImage) or its unpacked copy.

On its first start it unpacks itself into `~/.cache/sandbox-ai/pyz/VERSION-DIGEST-PYTHON/` (about a second;
private, read-only, each file checked against the manifest inside the `.pyz`) and runs from there; every later
start checks that copy and refuses it once anything in it changed (remove it with the `chmod -R u+w ... && rm -rf
...` the message shows, the next start unpacks it again). A copy that lost files (a
cache cleaner) is unpacked again. The check finds damage, not a deliberate change with your own user's rights (the
record lies beside the copy); sandboxes never get `~/.cache/sandbox-ai` (a private location for the launcher), and
a cache inside a tree an earlier run exposed to a sandbox is refused. A start removes the copies of other versions that nothing runs from any more (on Linux; on macOS
remove them yourself: `chmod -R u+w ~/.cache/sandbox-ai/pyz && rm -rf ~/.cache/sandbox-ai/pyz`). `./sandbox-ai.pyz` runs
`python3 -I` (the file starts with a shell line); with another Python run it yourself with `-I`.

**AppImage (Linux, x86_64 and aarch64)** - the app, with the command line inside:

```bash
mkdir -p ~/.local/bin && cd ~/.local/bin   # not into a project (see above)
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/sandbox-ai-x86_64.AppImage
chmod +x sandbox-ai-x86_64.AppImage
./sandbox-ai-x86_64.AppImage                  # the app
./sandbox-ai-x86_64.AppImage cli doctor       # the command line: cli ARGS...
ln -s sandbox-ai-x86_64.AppImage ~/.local/bin/sandbox-ai   # called as sandbox-ai, it is the command line
```

(`sandbox-ai-aarch64.AppImage` on ARM.) Without FUSE (containers, some minimal systems) run it as
`TMPDIR=$(mktemp -d) ./sandbox-ai-x86_64.AppImage --appimage-extract-and-run ...`: the runtime unpacks into
`$TMPDIR/appimage_extracted_<hash>` and reuses a folder of that name, so it must not be the shared `/tmp`
(AppRun refuses a folder that belongs to another user or that others can write). The first start copies its Python and sandbox-ai into
`~/.cache/sandbox-ai/appimage/` (about 70 MB, checked like the `.pyz`), so that sessions and kept starts go on
after the AppImage has ended; PySide6 stays inside the AppImage. OpenGL/EGL comes from the system, as for every Qt
program (Debian/Ubuntu: `libegl1 libgl1 libopengl0 libxkbcommon0 libfontconfig1 libglib2.0-0 libdbus-1-3`, on a
desktop already there); an X11 session also needs Qt's xcb libraries (`libxcb-cursor0 libxkbcommon-x11-0
libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xkb1`; Qt names a missing one, a Wayland
session needs none of them). The app's terminal tabs (QtWebEngine) are not included: terminals open in a terminal
window. A start at login, set up in the app, runs the AppImage.

**macOS (Apple silicon and Intel, macOS 14 or newer)** - `sandbox-ai-arm64.dmg` or `sandbox-ai-x86_64.dmg`:
open it and drag `sandbox-ai.app` to Applications. The app is not signed with an Apple developer ID and not
notarised, so macOS refuses the first start: on macOS 14 right-click the app > **Open** > **Open**; on macOS 15
and newer open it once, then System Settings > Privacy & Security > **Open Anyway**. Or remove the quarantine
mark of the download yourself:

```bash
xattr -dr com.apple.quarantine /Applications/sandbox-ai.app
open /Applications/sandbox-ai.app
sudo ln -s /Applications/sandbox-ai.app/Contents/MacOS/sandbox-ai /usr/local/bin/sandbox-ai   # the command line
sandbox-ai doctor                             # podman machine: running, rootless, /Users shared
```

**Check a download.** `SHA256SUMS` lists every release file, `SHA256SUMS.sig` signs it once the project has a
release key ([Signed updates](#installation-and-updates), [RELEASING.md](RELEASING.md#check-a-release-by-hand)):

```bash
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/SHA256SUMS
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/SHA256SUMS.sig
printf 'release@sandbox-ai namespaces="sandbox-ai-release" %s\n' "ssh-ed25519 AAAA..." > signers  # the published key
ssh-keygen -Y verify -f signers -I release@sandbox-ai -n sandbox-ai-release -s SHA256SUMS.sig < SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS      # macOS: shasum -a 256 -c --ignore-missing SHA256SUMS
```

The packages do not update themselves: `sandbox-ai update` in one of them says where to download the new one.

### install.sh

`install.sh` installs the newest release, or updates to it, and keeps the
launcher out of every workspace:

```bash
./install.sh                    # from a checkout: newest release of the configured source
./install.sh --from .           # this checkout itself
sandbox-ai-update               # later: update to the newest release (an installation from a release)
sandbox-ai-update --check       # installed and newest version, its signature and the trusted keys; no change
sandbox-ai version --verbose    # where it is installed and which release keys it trusts
./install.sh --uninstall        # remove commands and installation (secrets, logins, kits, sessions and settings stay)
```

| Mode | Files | Commands |
|---|---|---|
| User (default) | `~/.local/share/sandbox-ai/versions/X.Y.Z`, `current` link | links in `~/.local/bin`, and in `~/.bin` if it exists |
| System (`--system`, or run as root) | `/usr/local/lib/sandbox-ai` (`--prefix`) | `/usr/local/bin` |

A user install puts the command directory on your `PATH`: `~/.bashrc` (and
`~/.zshrc` if there is one) gets one block between `# >>> sandbox-ai >>>`
and `# <<< sandbox-ai <<<`. The block adds the directory only when it is not
on `PATH` already. Installing again changes nothing, another `--bin-dir`
replaces the block, and `--uninstall` removes it. `--no-modify-rc` leaves
the files alone and prints the line to add instead. A new terminal (or
`source ~/.bashrc`) finds the commands.

Releases come from `https://github.com/c0decave/sandbox-ai` unless
`--source URL` or `SANDBOX_AI_SOURCE` names another Gitea, Forgejo or GitHub
repository. The installer downloads `sandbox-ai-bundle-X.Y.Z.tar.gz`,
`SHA256SUMS` and `SHA256SUMS.sig`, checks the signature, refuses a checksum
mismatch, unpacks only regular files below one directory, and switches the
`current` link atomically; the previous version is kept for a rollback. For a
private repository put a token into `SANDBOX_AI_TOKEN`: it goes only to the
repository's own host, never over plain http except to localhost. On GitHub a
token also lifts the API limit of 60 requests an hour per address (the
installer says when it is reached). Existing files in the bin directories that
do not belong to sandbox-ai are left alone (`--force` replaces them).

**Signed updates.** A release signs its `SHA256SUMS` with an SSH key
(`ssh-keygen -Y sign`, `SHA256SUMS.sig`); `sandbox/release-signers` in the
installed tree lists the keys it trusts. Once that file lists a key, an update
is installed only with a good signature by one of them, checked with
`ssh-keygen` (OpenSSH 8.1 or newer) before anything is unpacked: an unsigned
release, a signature by another key or for another purpose, and a changed
`SHA256SUMS` are refused. A release can change the keys (rotation) only when a
key trusted now signed it. A first install trusts the keys that came with
`install.sh` (the published `install.sh` carries them; trust on first use), so
compare the fingerprint `sandbox-ai version --verbose` shows with the one the
project publishes. Releases older than the installed version are refused, and
an older "newest" release than one seen before is pointed out.

| Option | Does |
|---|---|
| `--allow-downgrade` | install a release older than the installed one |
| `--allow-unsigned` | install a release without a good signature, with a loud warning; it still cannot change the trusted keys |
| `--from BUNDLE.tar.gz --verify` | check a downloaded bundle against the `SHA256SUMS` and `SHA256SUMS.sig` next to it |

An administrator can demand signatures with `"require_signed_updates": true`
in `/etc/sandbox-ai/policy.json` ([policy](#administrator-policy)):
`--allow-unsigned` and `--from DIR` are refused, `--from BUNDLE` is always
verified, and an installation that trusts no key cannot update. Keys,
rotation, revocation and the limits are in
[RELEASING.md](RELEASING.md#signed-releases).

`pip install sandbox-ai` (or `pipx install sandbox-ai`) installs the same
commands from PyPI once releases are published there. A checkout works
without installing (`./sandbox-ai claude`), but it must not be a workspace
itself: the agent could rewrite the launcher. How releases are built and
published is in [RELEASING.md](RELEASING.md).

## The sandbox-ai command

`sandbox-ai` bundles every tool. The older single commands (`claude-sandbox`,
`codex-sandbox`, `opencode-sandbox`, `sandbox-session`, `sandbox-mcp`,
`sandbox-wizard`, `sandbox-build-image`) still work but are **deprecated**: each
prints one note on stderr naming its `sandbox-ai` form
(`SANDBOX_AI_NO_DEPRECATION=1` hides it, for scripts), and they will be
removed in a later version.


**Tab completion** for bash, zsh and fish completes every command,
subcommand and option, the choices of options (`--mode`, `--telemetry`,
`--backend` …) and the names you have: starts (`attach`, `stop`, `resume`),
running sandboxes (`net`, `devices`, `canaries`), MCP servers, LLM servers,
VPN profiles and kits; paths are left to the shell. `install.sh` sets it up
for all three (the scripts in `~/.local/share/bash-completion/completions/`,
`~/.local/share/zsh/site-functions/` and `~/.config/fish/completions/`, and the
lines bash and zsh need in the `~/.bashrc`/`~/.zshrc` block). By hand:
`sandbox-ai completion install`, or `sandbox-ai completion bash|zsh|fish` for
the script itself. The candidates come from the commands' own parsers, so they
match the installed version.

| Command | Older form | Does |
|---|---|---|
| `sandbox-ai claude\|codex\|opencode [options] [-- args]` | `claude-sandbox` … (deprecated) | start an agent |
| `sandbox-ai list [--all] [--json]` | `sandbox-ai ps` | one table of every start: what runs or is paused, the starts whose HOME is kept, and the MCP server sandboxes ([every start](#every-start-names-detach-keep-resume)) |
| `sandbox-ai list --history NAME [--json]`, `sandbox-ai rename OLD NEW` | | what happened to a start (start, exit with status, pause, resume, stop, rebirth, rename); give a start another name (its run id stays) |
| `sandbox-ai attach\|pause\|resume\|stop\|delete NAME` | `sandbox-ai session …` | any start by its name or run id: attach this terminal (detach with Ctrl-b d or by closing it), freeze and thaw, end, remove a stopped one with its HOME |
| `sandbox-ai resume NAME [--restart] --workspace DIR` / `--mount SRC:DEST[:ro\|rw]` | | start a kept start (again) with another workspace or mount added; the paths get every check of a new start, the agent continues its conversation and the addition stays for later starts. `--restart` stops a running one first; `sandbox-ai workspace add` adds a workspace without a restart |
| `sandbox-ai resume NAME --review-home [--json]` | | what changed in a kept HOME's start files (shell, git, ssh, the agent's settings, hooks, plugins) since its last start; starts nothing ([every start](#every-start-names-detach-keep-resume)) |
| `sandbox-ai gc [--older-than DAYS] [--dry-run] [--json]` | | remove leftovers; with DAYS also the stopped kept starts unused for that long, with their HOME |
| `sandbox-ai session …` | `sandbox-session` (deprecated) | the same for sessions, in the older form |
| `sandbox-ai mcp …` | `sandbox-mcp` (deprecated) | MCP server sandboxes |
| `sandbox-ai wizard [new\|edit\|trust\|forget\|models] …` | `sandbox-wizard` (deprecated) | project setup, editing and trust |
| `sandbox-ai login claude\|codex\|opencode` | | log in inside a sandbox; the login is kept for all sandboxes of that agent ([login store](#login-store-for-sandboxes)) |
| `sandbox-ai login --status`, `sandbox-ai login AGENT --logout` | | which login each agent's sandboxes get; forget a stored login |
| `sandbox-ai plugins claude [--update]`, `sandbox-ai plugins --status`, `sandbox-ai plugins claude -- ARGS`, `sandbox-ai plugins claude trust-hooks PLUGIN` | | install the Claude plugins the kits declare into the plugin store ([plugins](#kits-project-memory-and-plugins)); show declared vs. installed; run `claude plugin ARGS` on the store; approve a plugin's hooks ([hooks in kits](#hooks-in-kits)) |
| `sandbox-ai kits [list\|show\|new\|add-skill\|new-skill\|add-plugin\|add-marketplace\|remove-skill\|remove-plugin\|add-hook\|remove-hook\|trust-hooks\|delete] …` | | every kit and what it brings (`--json` for programs); create your own kits, copy or write skills, declare plugins, add and approve hooks ([kits](#managing-kits-sandbox-ai-kits), [hooks in kits](#hooks-in-kits)) |
| `sandbox-ai build [--pin V] [--extension-only] AGENT\|all\|VARIANT`, `sandbox-ai build team\|vpn` | `sandbox-build-image` (deprecated) | build the images and their extensions ([extensions](#extensions-and-variants)), the team image for `--with` ([other agents in the same sandbox](#other-agents-in-the-same-sandbox)) or the VPN container ([VPN](#vpn)) |
| `sandbox-ai doctor [--backend podman\|bwrap\|both] [--plain] [--no-color] [--json]` | | a table by section (System, Engine, Agents, Project), in colour on a terminal (ok green, WARN orange, FAIL red; `--no-color` or `NO_COLOR` without; `--plain` one line per check); check the host: user, Python, user namespaces, Podman (rootless), pasta, subuid/subgid, images with agent versions, Bubblewrap tools, TIOCSTI, host logins (expiry only), the default kit and the kits with their skills, agents, commands and prompts, Claude plugins declared vs. installed, the memory of the project in the current directory, Git identity, project trust; each problem with the command that fixes it; exit 1 on a failure |
| `sandbox-ai logs [DIR\|--project P] [--json]` | | summarize blocked egress and file access of a `--log-dir` and suggest the option for each blocked target (`--allow-pypi`, `--allow-host`, `--allow-tcp`, `--allow-host-port` …) |
| `sandbox-ai activity [RUN\|--session NAME\|--project P\|--log-dir DIR] [-f] [--since T] [-k KIND] [--json]` | | what the agents did: prompts, replies, tool calls, commands, file changes, network, approvals, tokens ([agent activity](#agent-activity)) |
| `sandbox-ai net list\|show\|allow\|revoke\|untaint …` | | change what a running sandbox may reach ([network changes](#changing-the-network-of-a-running-sandbox)); it ends with `sandbox-ai stop NAME` |
| `sandbox-ai devices list\|add\|remove [RUN] PATH\|usb:VID:PID`, `sandbox-ai limits [RUN] …` | | devices and limits of a running sandbox ([hardware](#hardware-devices-and-gpus), [limits](#resource-limits)) |
| `sandbox-ai workspace list\|add\|remove [RUN] DIR [--ro] [--force]` | | a host directory added to a running sandbox at `/workspaces/<name>`, with the checks of a start, and taken back ([workspaces and mounts](#workspaces-and-mounts)) |
| `sandbox-ai approvals list\|allow\|deny\|history\|remembered\|forget …` | | answer what running sandboxes ask; what was allowed always ([approvals](#approvals-and-browser-requests)) |
| `sandbox-ai canaries list [RUN]`, `sandbox-ai canaries resume RUN` | | the decoy files of the runs and what happened to them; continue a sandbox a canary paused ([canary files](#canary-files)) |
| `sandbox-ai secrets list\|set\|delete\|backend` | | API keys and tokens in your system keyring ([secrets](#secrets)) |
| `sandbox-ai git-logins add\|list\|remove\|check …` | | your Git servers' logins, added by the connection proxy, never inside a sandbox ([Git logins](#git-logins-in-the-proxy-sandbox-ai-git-logins)) |
| `sandbox-ai publish-logins add\|list\|remove\|check\|kinds …` | | your logins for npm, PyPI, crates.io, GitLab and GitHub, for `--publish` ([publishing](#publishing-to-registries-and-forges-sandbox-ai-publish-logins---publish)) |
| `sandbox-ai identities add\|set\|login\|logout\|publish\|unpublish\|list\|show\|check\|remove …` | | commit name and address, Git and publish logins under one name, for `--identity` ([identities](#identities-sandbox-ai-identities---identity)) |
| `sandbox-ai presets list\|show\|check …`, `sandbox-ai demo-device start\|serve\|status\|stop` | | ready setups for `--preset`; an emulated serial device to practise on ([presets](#presets)) |
| `sandbox-ai team start\|list\|show\|post\|stop\|delete …` | | a team of agents, one sandbox each, with a task board ([a team of agents](#a-team-of-agents)) |
| `sandbox-ai hosts list\|add\|set\|remove\|test\|update\|run …` | | other machines (or LXC containers) whose sandbox-ai this one manages and updates over SSH ([other hosts](#other-hosts)) |
| `sandbox-ai web user\|vendor\|serve\|cert …`, `sandbox-ai defaults show\|set\|unset …` | | the web interface; your defaults for every start in `~/.config/sandbox-ai/defaults.json` ([web interface](#web-interface)) |
| `sandbox-ai completion bash\|zsh\|fish\|install` | | Tab completion (see above) |
| `sandbox-ai llm add\|list\|show\|remove\|models\|check …` | | LLM servers, local or remote, for `--llm NAME[:MODEL]`; their keys stay with the connection proxy ([LLM servers](#llm-servers-local-and-remote)) |
| `sandbox-ai vpn add\|list\|show\|remove\|check …` | | VPN profiles for `--vpn` (WireGuard, OpenVPN, Mullvad, Tailscale, Tor); their keys stay in `sandbox-ai secrets` ([VPN](#vpn)) |
| `sandbox-ai images [packages\|extend\|check\|recipe\|new\|remove\|history] [--json]` | | the agent images and variants with agent version, age, size and whether each is current with its extension; the packages in one; extensions, ready-made recipes (`media`, `tor`) and variants ([extensions](#extensions-and-variants)) |
| `sandbox-ai update [--check] [--version X.Y.Z] [--allow-downgrade]` | `sandbox-ai-update` | update an installation made by `install.sh`; signed releases ([installation](#installation-and-updates)) |
| `sandbox-ai version [--verbose]` | `-V`, `--version` | version; `--verbose`: where it is installed and the fingerprints of the trusted release keys |

## Images: build and update

```bash
./sandbox-ai build all                 # newest releases, all three images
./sandbox-ai build claude              # one image, newest Claude Code
./sandbox-ai build --pin 1.0.42 claude # exactly this agent version
```

Each agent has its own `sandbox/Containerfile.<agent>` and image
`localhost/<agent>-sandbox:latest`; an image contains only its own CLI.
Launchers never build or pull images implicitly. `./sandbox-ai build vpn`
builds `localhost/sandbox-ai-vpn:latest` from `sandbox/Containerfile.vpn`, the
container of [`--vpn`](#vpn) (Debian with WireGuard tools, OpenVPN and
nftables, wireguard-go built from source, Tailscale's binaries from its
official image).

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
add project tools with an [extension](#extensions-and-variants). `gh` comes from GitHub's own apt
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

### Extensions and variants

The images stay small; add what a project needs as an **extension** instead
of editing the recipes. An extension belongs to an agent's own image or to a
named **variant** of it and holds Debian packages, global npm packages, Python
tools (installed with `uv tool`), files to copy in and, optionally, a raw
Containerfile fragment:

```bash
sandbox-ai images extend claude --add-apt build-essential --add-pip ruff==0.6.9
sandbox-ai images extend claude --add-file ./lint.sh:/usr/local/bin/lint:0755
sandbox-ai images extend claude --fragment my.fragment   # or - for standard input
sandbox-ai images check claude      # findings and the final Containerfile
sandbox-ai build claude             # base image, then the extension layer

sandbox-ai images new gpu --from claude    # a variant: localhost/claude-sandbox-gpu
sandbox-ai images extend gpu --add-apt mesa-vulkan-drivers
sandbox-ai build gpu                       # claude's base, then this variant
sandbox-ai claude --image-variant gpu      # or "-iv gpu" in a project file's options
```

| Command | Does |
|---|---|
| `images extend AGENT\|VARIANT` | `--add-apt/--remove-apt PKG`, `--add-npm/--remove-npm PKG`, `--add-pip/--remove-pip PKG`, `--add-file SRC:TARGET[:MODE]`, `--remove-file TARGET`, `--fragment FILE\|-`, `--clear-fragment`; without changes (or with `--show`) it shows the extension; `--json` |
| `images check AGENT\|VARIANT [--fragment FILE\|-]` | validate (also an unsaved fragment) and print the final Containerfile; exit 1 on errors |
| `images new NAME --from AGENT [--empty]` | a variant, started from the agent's extension unless `--empty` |
| `images remove AGENT\|VARIANT [--keep-image]` | drop an extension; a variant's image goes too |
| `images history [AGENT\|VARIANT] [--json]` | every change and build, with the extension's hash |
| `build [-e\|--extension-only] AGENT\|all\|VARIANT` | `--extension-only` rebuilds only the layers, on the base image there is |

**Where they live.** `~/.config/sandbox-ai/images/NAME/` (`image.json`, the
optional `Containerfile.fragment` and `files/`), private to you (0700/0600).
Every command refuses that directory - and every `--add-file` source - when
it lies in a tree a sandbox can or could write (sandbox-ai's registry of
exposed workspaces, rw mounts and HOMEs) or is reached through a symlink
planted there. Files are copied in when you add them (regular files only,
never links, 50 MB each); a build uses exactly the bytes it hashed, from a
fresh private build context that holds nothing else.

**How it is built.** `sandbox-ai build AGENT` builds the base image as
before and tags it `localhost/AGENT-sandbox:base`. Without an extension that
is also `:latest`; with one, a layer `FROM localhost/AGENT-sandbox:base`
(`--pull=never`) becomes `:latest`, and the agent's variants are built on the
same base as `localhost/AGENT-sandbox-NAME:latest`. Each layer carries the
labels `sandbox-ai.extension-hash` (the generated Containerfile and the
files), `sandbox-ai.base-id` and, inherited, `sandbox-ai.agent-version`; the
build cache reuses unchanged layers. `sandbox-ai images` shows per image
whether it is **current**, **outdated** (the extension changed),
**base-changed** (the base was rebuilt since) or **invalid**. The
Containerfile always ends with `USER root` and `WORKDIR /work`, what the
launcher expects. `--image-variant NAME` checks that NAME is a variant of the
agent, applies to Podman only and excludes `--image`; an administrator's
`forbid: ["--image"]` refuses every image but the default one, variants
included.

**The validator** (`images check`, and every change and build): errors
refuse saving and building, warnings are shown.

| Rule | Severity | What |
|---|---|---|
| `from` | error | `FROM` - the base is always the agent's own image |
| `entrypoint` | error | `ENTRYPOINT`, `CMD` |
| `volume`, `onbuild` | error | `VOLUME` (a writable host volume past the read-only root), `ONBUILD` |
| `setuid`, `setcap` | error | `chmod u+s`/`4755`, `install -m 4755`, `COPY --chmod=4755`, a file mode with setuid/setgid; `setcap` |
| `protected` | error | writing, linking over or removing what the sandbox's setup runs as root: `python3`, `nft`, `firejail`, `setpriv`, `tmux`, `sh`, `bash` (also shadowed in `/usr/local/bin`), `/etc/ld.so.preload`, `/sandbox`, the CA bundle |
| `remove-protected` | error | `apt-get remove/purge` of firejail, nftables, python3, util-linux, tmux, bash, ca-certificates, ... |
| `add-url`, `add-git` | error | `ADD` from a URL without `--checksum=sha256:…`; `ADD` of a Git repository |
| `copy-from`, `copy-source` | error | `COPY --from`; a source that is not `files/NAME` of this extension |
| `loader` | error | `ENV`/`ARG` `LD_PRELOAD`, `LD_AUDIT`, `LD_LIBRARY_PATH` |
| `label` | error | labels `sandbox-ai.*` |
| `heredoc`, `control`, `syntax`, `unknown`, `size` | error | here-documents, control or bidi characters, unparsable lines, unknown instructions, over 64 KiB |
| `package`, `agent-package`, `file`, `mode` | error | invalid package names (no URLs, paths or git sources), the agent's own npm package, bad file entries, world-writable modes |
| `pipe-shell`, `ca-trust` | warning | `curl … \| sh`; new trusted CAs |
| `user`, `workdir`, `shell`, `env` | warning | `USER`, `WORKDIR`, `SHELL`; `ENV PATH`, `HOME`, proxy and CA variables the launcher sets |
| `masked`, `add-local`, `no-effect`, `unknown-flag` | warning | files into `/work`, `/root`, `/tmp` … (mounted over); `ADD` of a local file; `EXPOSE`, `HEALTHCHECK` …; unknown options |

The validator is a guard against mistakes and copied snippets, not a proof:
a `RUN` line can still do anything a build can. What you put into an image
runs in every sandbox started from it.

## Starting an agent

```bash
./sandbox-ai claude --workspace "$HOME/src/demo"
./sandbox-ai codex --workspace "$HOME/src/demo" \
  -- exec --skip-git-repo-check 'Create hello.txt containing Hello.'
./sandbox-ai opencode --workspace "$HOME/src/demo" --dry-run   # show the plan only
./sandbox-ai codex --workspace "$HOME/src/demo" --offline --probe   # verify isolation
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
Claude refreshes in the sandbox go back to their source while the run is still
going, and a newer login in the source reaches the running sandbox the same way
(see **Claude login** below); at the next start the source's login always
replaces the sandbox's.

**Codex** gets `--sandbox danger-full-access` by default: it relies on the
verified outer sandbox and Firejail, because its own nested sandbox cannot
create a UID map inside them. Explicit `--sandbox`, `-s` and
`--dangerously-bypass-approvals-and-sandbox` options are kept.

### Presets

A preset is a ready setup to start from: launcher options, MCP servers, a
ruleset and a first task for the agent, with placeholders for what only you know
(a folder, a port, a device). The command line wins over the preset; your
project file, defaults, the security mode and the administrator's policy apply
as always. A preset's `--mode` is a floor: it raises a laxer mode, never lowers a
stricter one from your project file or defaults (a `--mode` you type wins).

```bash
sandbox-ai presets list                                  # built-in and your own
sandbox-ai presets show software-analysis                # what it sets and asks for
sandbox-ai presets check software-analysis --agent claude --value samples=~/samples
sandbox-ai claude --preset software-analysis --preset-value samples=~/samples
```

Built in: **analysis** (`hardware-analysis`, `software-analysis`,
`network-analysis`, `forensics`, `firmware-iot`), **development**
(`development`, `code-audit`, `docs-writer`, `data-ml`, `mobile-android`,
`onboarding-buddy`), **testing** (`commit-censor`, `chaos-monkey`,
`docs-lie-detector`, `canary-course`, `network-maze`, `memory-hog`) and **fun**
(`time-capsule`, `mini-ctf`, `archivist-1995`, `radio-sandbox-ai`,
`agent-race`, `pixel-art-factory`, `sand-chess`). The analysis presets keep
the agent to its provider and the samples read-only; their tools (capa, FLOSS,
radare2, YARA, binwalk, TShark, The Sleuth Kit ...) are MCP servers from the
catalogs, each in a container of its own: `presets check` names the
`sandbox-ai mcp add` commands for the ones you have not added.

Your own presets are JSON files in `~/.config/sandbox-ai/presets/` (outside
every workspace: an agent must not write what it starts with); one with the id
of a built-in preset replaces it. A preset never holds a secret (options only
name them) and may not copy a login in (`--copy-auth`, `--auth-file`, ...),
choose the workspace or carry `--rules` (a `"rules"` file beside it is passed
for it). The format is in `sandbox-ai presets --help` and `sandbox/presets.py`.

Further preset directories, a private collection or a team's checkout, are
added with `sandbox-ai presets dirs add DIR` (listed with `presets dirs`,
removed with `presets dirs remove DIR`; kept as `"preset_dirs"` in
`~/.config/sandbox-ai/defaults.json`, also in the app under Settings >
sandbox-ai). Your own folder wins over them, the first listed over the later
ones, and they over the built-in presets. For the same reason as above, a
directory inside a workspace or another folder sandboxes can or could write is
refused (and skipped if it becomes one), and a preset or ruleset there that is
a symlink is not followed.

**A device to practise on.** `sandbox-ai demo-device start` runs an emulated
serial device on `127.0.0.1:4000` (the made-up "SB-42 sensor node": U-Boot, a
kernel's boot messages, a login and a small shell with a readable flash), the
way ser2net offers a real one; `hardware-analysis` uses it by default
(`--allow-host-port 4000`; pyserial speaks to it as `socket://127.0.0.1:4000`).
It is fixed text: nothing on your computer is read, run or changed.
`sandbox-ai demo-device status|stop`.

## Security modes

A security mode is a preset of the options below, from `lax` to `paranoid`.
The mode sets defaults; options may only narrow it. In `high` and `paranoid`
an option that would widen the sandbox beyond the mode is refused, with the
mode, where it came from and the laxer mode that would allow it:

```bash
./sandbox-ai claude --mode high            # short: -mo high
./sandbox-ai claude --mode high --allow-github
# agent-sandbox: --allow-github widens the security mode high (--mode): it opens hosts beyond the
# agent's provider ...; leave it out, or choose a laxer mode: --mode medium ...
```

| Mode | Network | Approvals | Workspaces, mounts | Devices, display | Other |
|---|---|---|---|---|---|
| `lax` | every outbound destination (`--egress-all-allow`), each connection logged by the proxy | off (`--ask=off`) | as given | as given | `--log-debug`; [canary files](#canary-files) only reported, a leaking token is not refused |
| `low` | `--profile development --allow-npm`: the provider, PyPI, npm, GitHub, the OpenAI and Anthropic APIs | on | as given | as given | |
| `medium` (default) | the agent's provider only | on | as given | as given | the launcher's own defaults: exactly a start without `--mode` (canaries: `credentials`, a read reported, a leaking token refused and the run stopped) |
| `high` | the provider only; nothing can be opened by options | on, answers at most for the run | `/work` writable, further workspaces read-only, no `rw` mounts | no `--device`, `--gpu`, `--gui-wayland` | privacy check of every push (`--privacy-check=always`), 4g memory, 2048 processes, CPUs at most the default; a ruleset asks before a write (POST, PUT, PATCH, DELETE) to any host but the provider, and once content from such a host [tainted the run](#rulesets), before every MCP tool call; canaries `credentials` and `home`, a read asked about (the sandbox paused meanwhile), a leaking token stops the run |
| `paranoid` | as `high`, or `--offline` | on, answers only once | every workspace read-only (`--workspace-ro`): results leave as text or patches | as `high`, and no `--gui`/`--gui-vnc` | as `high`, and: a ruleset refuses every HTTP request but the provider's, no `-D`, no MCP server with network, `--log-debug`, 2g memory, 2 CPUs, 1024 processes; a read of a canary stops the run; MCP tool results and the agent's WebSearch and WebFetch taint the run too, and then its shell, file edits and fetches ask as well (through [its hooks](#rulesets)) |

What narrowing means, option by option (in `high` and `paranoid`):

- **Network**: `--offline`, `--profile offline`, `--no-provider-defaults`,
  `--allow-http`, `--rules` and `--vpn` narrow; every option that opens a
  destination widens: `--profile` (other than `offline`), `--egress-all-allow`,
  the presets (`--allow-github` ...), `--frontier`, `--allow-host`, `--allow`,
  `--allow-tcp`, `--allow-host-port`, `--dns`, `--nameserver`, `--llm`, and a
  Git token (`--git-token-*`, `--git-credentials`), which opens its server.
  In `lax` and `low` a network choice (`--profile`, `--offline`,
  `--egress-all-allow`, `--no-provider-defaults`, `--local-config`) replaces
  the mode's network; other network options add to it.
- **Proxy and approvals**: `--no-proxy`, `--no-inspect` and `--ask=off` widen;
  `--ask=on` narrows `lax`. The widest approval scope is `run` (`high`) or
  `once` (`paranoid`), or the policy's `max_approval_scope` if that is
  narrower; below `always`, remembered approvals are not applied.
- **Privacy check**: `always` is the strictest, then `public`, then `off`;
  the strict modes take only `always`.
- **Workspaces and mounts**: `--workspace-ro` and `ro` mounts narrow; a `rw`
  mount widens.
- **Canary files**: `--no-canaries` widens both strict modes; their
  categories (`credentials`, `home`) are planted whatever `--canaries` adds.
- **Devices and display**: `--device`, `--gpu` and `--gui-wayland` widen in
  both strict modes, `--gui` and `--gui-vnc` in `paranoid`.
- **Limits**: a smaller `--memory`, `--cpus` or `--pids` narrows; a larger one
  or `none` widens.
- **Agent**: `-D` (`--dangerously-skip-permissions`) widens `paranoid`;
  `--mcp NAME` widens `paranoid` when the server has network (an image server
  with `"network": "full"`, a command server without `--offline`).
- **HOME**: `paranoid` keeps nothing beyond the run: every start discards
  its HOME at the end (as `--no-keep`), `--keep` and `--session` widen it, and
  a kept HOME from an earlier start cannot be brought back under it
  (`sandbox-ai resume` refuses). `high` and the laxer modes keep HOMEs by
  default ([every start](#every-start-names-detach-keep-resume)); the HOME
  review before every rebirth applies to them, and a kept HOME keeps its
  canary files (the same files and tokens) from run to run.
- **Taint**: the strict modes escalate once untrusted content entered the run
  (see *the run's taint* under [Rulesets](#rulesets)). In `high`, content from
  any host but the provider taints the run, and from then on every MCP tool
  call asks. In `paranoid`, every MCP tool result and the agent's WebSearch
  and WebFetch taint it as well; WebSearch runs at the provider and never
  passes the proxy. From then on the agent's shell, file edits and fetches ask
  too, through its hooks. `sandbox-ai net untaint` takes the taint back.

**During the run** the mode holds as well: the start records it in the run's
`run.json` and in the supervisor's configuration. In `high` and `paranoid`,
`sandbox-ai net allow` opens nothing the mode refuses at the start (`--allow`,
`--allow-host`, `--allow-tcp`, presets) and says which mode refuses it; the
supervisor refuses such a request too, so one written to the control socket by
hand gets no further. Answers to the sandbox's questions stay possible, capped
at the mode's scope (`run`, `once`) in `sandbox-ai approvals` and again in the
supervisor; `paranoid`'s HTTP rules keep refusing every request but the
provider's.

**Where the mode comes from**, the first that is set: `--mode`, the key
`"mode"` of the [project file](#project-configuration-and-wizard), `"mode"`
in `~/.config/sandbox-ai/defaults.json` (`{"version": 1, "mode": "high"}`),
else `medium`. A project file chooses its mode with the key, not with
`--mode` in its options. The administrator's `min_mode`
([policy](#administrator-policy)) is a floor: a mode below it is refused,
whoever chose it, and without a choice the floor is the mode. Options of a
trusted project file count like options on the command line. The sandbox of
an MCP server ignores your default mode (its definition decides), not the
policy's floor. The `--dry-run` plan shows the mode under `mode`: its name,
its origin, the options it added, the approval scope, whether further
workspaces are read-only and whether it brings a ruleset (listed under
`rulesets` as `mode NAME`). `sandbox-ai doctor` names the mode a start in the
current directory gets and lists the modes; `sandbox-ai wizard` asks for the mode
first and then offers only what it allows.

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
./sandbox-ai claude --workspace "$HOME/src/app,$HOME/src/shared-lib" --workspace "$HOME/notes"
#   /work = app, /workspaces/shared-lib, /workspaces/notes
```

**A workspace added while the sandbox runs.** `sandbox-ai workspace add [RUN] DIR
[--ro]` mounts DIR at `/workspaces/<name>` without a restart; `workspace list`
shows the run's workspaces, `workspace remove [RUN] DIR|NAME` takes an added one
away again. DIR gets the checks of `--workspace` at a start (no HOME, temporary
or run directory, no private location, no link a sandbox may have planted, no
overlap with the run's workspaces, mounts and HOME, none of the files the
launcher reads inside it, the administrator's policy), and it is read-only with
`--ro` and whenever `--workspace-ro` or the security mode keeps further
workspaces read-only. Nothing runs in the sandbox for it: a helper of your user
checks that the directory it opened is the one that was checked (device and
inode, so a symlink swapped in meanwhile mounts nothing), enters the sandbox's
user namespace and mounts a clone of it in each of the sandbox's mount
namespaces, the agent's under Firejail included (Linux 5.2 or later; read-only
needs 5.12), never into a namespace the agent made itself. A directory that
holds one of your PATH directories (a `.venv/bin`) is refused: start with it
instead, where the launcher checks those after the run. `remove` freezes the
sandbox for a moment (where its cgroup is its own), refuses while a process
there works in it or has a file open or mapped there, refuses while the agent
holds a copy in a user and mount namespace of its own (`unshare`) that the
mount tables show, and unmounts strictly, so the kernel refuses too while a
process in the sandbox's own namespaces still holds the tree; `--force`
detaches it anyway, and what is still open stays usable, also for writing,
until it is closed. A copy the agent detached and keeps only through an open
descriptor shows in no table: only a restart takes a workspace away for
sure. Added to a session,
the workspace is kept in its options, so `sandbox-ai resume NAME --restart`
starts with it at the same place; one added with `--ro` is not kept (it would
come back writable), unless the run keeps every further workspace read-only.
The file log (`--log-dir`) watches it from then on, and since 0.1.78 the push
check takes its commits with the run's rules and its project's own (the
command says so; where it cannot, its pushes to a public remote stay refused),
and the privacy check after the run looks at it too.
Runs started by 0.1.75 or older have no place for it: restart them with
`resume NAME --restart --workspace DIR`.

```bash
sandbox-ai workspace add claude-1a2b3c4d ~/src/shared-lib --ro
#   added /home/me/src/shared-lib to claude-1a2b3c4d at /workspaces/shared-lib (ro)
sandbox-ai workspace remove claude-1a2b3c4d shared-lib
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
(a new start's HOME always; a kept start, `--state-dir` or session that comes
back keeps its own); missing
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
| `-S`, `--state-dir DIR` | Persistent sandbox HOME (owned by you, mode `0700`, outside the workspace); one run at a time (locked). |

OS keyrings are not exported: an agent that keeps its login only in a keyring
needs a file-based login first. Of Claude's `.credentials.json` only the
`claudeAiOauth` login crosses, not the OAuth tokens of your MCP servers. The
host files are read only if they lie outside every workspace, rw mount and
`--state-dir` (`CODEX_HOME`, `CLAUDE_CONFIG_DIR` and `XDG_DATA_HOME` included).

**Claude login.** For Claude the launcher also prints how long the copied
access token is valid. An expired access token is no reason to log in again:
Claude refreshes it with the refresh token at its first request. Its token
endpoint (`platform.claude.com`, `console.anthropic.com` for older versions)
is one of Claude's provider hosts, open in every security mode, so a sandbox
left idle for hours refreshes as well. Anthropic rotates OAuth refresh tokens,
so the side that refreshes last holds the only valid pair, and that pair must
reach every other holder of the login before it refreshes with the used-up
token. With automatic copying or `--copy-auth` the launcher therefore keeps the
sandbox's `.credentials.json` and its source (host or login store) in step
**while the sandbox runs**: it looks at both files every two seconds (`lstat`
only) and, when one has changed, compares them under a lock that every
sandbox-ai process on the host shares
(`~/.local/state/sandbox-ai/claude-login.lock`):

- the sandbox refreshed and the source still holds the login the two last
  shared: the new pair goes to the source at once, so a sandbox started
  afterwards gets it and not the used-up one;
- the source holds another login (another sandbox's refresh, Claude on the
  host, a new `sandbox-ai login claude`) and the sandbox has not refreshed
  since: it goes into the sandbox. Before refreshing, Claude reads its
  credentials file again and takes a newer pair from it instead of sending its
  used-up refresh token;
- both changed: the source wins. This compare-and-swap means a stale pair never
  replaces a newer one.

What happened is reported after the run (the agent's terminal is busy during
it), and the same comparison runs once more at the end. Before the start, the
source's login replaces the one in a reused state directory. Only a pair newer
than the source's goes back, and only if the source had a login when the run
started. Only the login counts, not the whole
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
than 31 days ahead; symlinks planted in a reused sandbox HOME are refused. While
the agent runs, the sandbox file is read and replaced through directory
descriptors without following any link, since the agent can change its HOME at
any moment. Tokens are never printed or logged; the lock and the
`login-sync.json` record hold none. A failed write-back is reported but never
changes the agent's exit status. Residual risks: two holders of one login that
refresh within the same two seconds still race for its refresh token; a
compromised agent could substitute tokens of another account that the source
would then use. `--no-auth` or `--auth-file` keep the host file untouched.
Other agents' host files are never written.

**Before the start** the launcher checks the login the sandbox will get. If it
is missing it says so (`sandbox-ai login AGENT`); if Claude's refresh token has
expired, it asks in a terminal whether to log in now inside a sandbox, keeping
that login in the login store for all later sandboxes.

### Login store for sandboxes

Host and sandboxes that share one login also share its rotating refresh token:
whichever refreshes second with the same token is logged out. For Claude the
launcher passes each refresh on while sandboxes run (above), but Codex and
OpenCode logins are written back only when a run ends. `sandbox-ai login AGENT`
gives the sandboxes a login of their own:

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
agent could replace it for later sandboxes. Two Codex or OpenCode sandboxes that
run at the same time still race for one refresh token; Claude sandboxes do only
when they refresh within the same two seconds. `sandbox-ai login --status`
shows how long a Claude login can still be renewed, if Claude stored the
provider's figure for that (`refreshTokenExpiresAt`).

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
| `hooks/` ([once approved](#hooks-in-kits)) | `hooks.json` and the scripts it runs | `hooks.json` and the scripts it runs | plugin modules (`*.js`, `*.mjs`, `*.ts`) |

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
sandbox-ai claude                             # default kit only
sandbox-ai claude --kit review --kit python   # plus two named kits
sandbox-ai codex --kit-dir ~/my-kits          # plus ~/my-kits/codex
sandbox-ai claude --no-default-kit --kit review
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
HOME (a kept start, `--state-dir`, session) keeps the changes for its next start; a
HOME discarded at the end (`--no-keep`) loses them. `--no-memory` starts without project memory;
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
sandbox-ai plugins claude --kit egm   # also what the built-in kit egm declares
sandbox-ai plugins --status           # declared vs. installed, per kit
sandbox-ai plugins claude -- list     # any `claude plugin ...` command on the store
```

The demo plugin `sandcastle@sandbox-ai` adds `/sandcastle`: a robot building
sandcastles in its sandbox. A Claude sandbox started before `sandbox-ai plugins
claude` ran says which declared plugins are missing.

sandbox-ai's marketplace also offers **`egm@sandbox-ai`**, Google's
[Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model)
(Apache-2.0): skills and reviewer subagents to draft a design spec and have it
reviewed by subagents without the conversation's context
(`/egm:egm-brainstorm`, `/egm:egm-draft`, `/egm:egm-review`). It is off unless a
kit lists it. sandbox-ai ships the **built-in kit `egm`** that does (in
`sandbox/builtin/kits/egm/claude`; a kit `egm` of your own replaces it), so
choosing it takes no file of your own:

```bash
sandbox-ai plugins claude --kit egm   # install it: fetches the pinned commit from GitHub
sandbox-ai claude --kit egm              # enable it (or name kit egm in sandbox-ai wizard edit, part k)
```

sandbox-ai pins it to one upstream commit; installing fetches that commit from
GitHub (plain `sandbox-ai plugins claude` leaves built-in kits out, so it never
reaches GitHub for them). It uses the sandbox's own model and login, no extra
service or key. To have it in every Claude sandbox, add `"egm@sandbox-ai"` to
`plugins` in the default kit's `claude/marketplaces.json`. The kit is for Claude
only: upstream's Codex plugin needs Codex plugin support, which sandbox-ai does
not have yet (a Codex kit skill cannot reach the personas and docs the skills
load from the plugin root), and its OpenCode adapter is experimental, bound to
one OpenCode release and installed into a separate configuration rather than as
an npm plugin.

The built-in kit **`superpowers`** enables `superpowers@sandbox-ai`,
[obra/superpowers](https://github.com/obra/superpowers) 7.0.2 (MIT), pinned
to the commit of its tag: skills for brainstorming, writing and executing
plans, test-driven development, systematic debugging, code review, git
worktrees and subagent-driven development. Its SessionStart hook (it reads the
plugin's `using-superpowers` skill and prints it as context, nothing else) runs
only once you approve it:

```bash
sandbox-ai plugins claude --kit superpowers   # install the pinned commit
sandbox-ai kits trust-hooks superpowers       # review and approve its hook
sandbox-ai claude --kit superpowers
```

Claude only, for the same reasons as egm. More tools that were asked for
(MCP servers, kits, services) and where each stands:
[INTEGRATIONS.md](INTEGRATIONS.md).

**OpenCode** gets the npm
packages listed in `opencode/marketplaces.json` (`{"plugins": ["name@1.2.3"]}`)
as its `plugin` setting; OpenCode installs them itself, which needs
`--allow-host registry.npmjs.org`. **Codex** plugins are not supported yet.

Kits, memory and plugins are left out of `--probe`, MCP server sandboxes and
the login sandbox. `--copy` into a path they fill is refused. A kit or store
inside a workspace is refused. Residual risk: a plugin runs code in every
sandbox that enables it (hooks, MCP servers); install only plugins you trust.

### Managing kits: `sandbox-ai kits`

`sandbox-ai kits` shows every kit and changes your own ones without editing
files by hand:

```bash
sandbox-ai kits                                  # every kit and what it brings, per agent
sandbox-ai kits show review                      # one kit (also: default, built-in, a built-in kit)
sandbox-ai kits new review                       # kits/review/AGENT/skills; --agent A for fewer agents
sandbox-ai kits add-skill review ~/skills/lint   # copy a skill folder (SKILL.md at its top); --as NAME
sandbox-ai kits new-skill review lint-fix --description "Fixes lint errors; use after edits"
sandbox-ai kits add-plugin review sandcastle@sandbox-ai           # declares sandbox-ai's marketplace too
sandbox-ai kits add-plugin review tool@team --source team/plugins # declares the marketplace team (GitHub)
sandbox-ai kits add-plugin review some-plugin@1.2.3 --agent opencode
sandbox-ai kits add-marketplace review team https://git.example.org/team/plugins.git
sandbox-ai kits remove-skill review lint-fix     # remove-plugin review PLUGIN works alike
sandbox-ai kits add-hook review ~/hooks/check.sh --matcher Bash   # a PreToolUse hook (--event, --timeout)
sandbox-ai kits trust-hooks review               # review the kit's hooks, then approve them
sandbox-ai kits delete review --yes
```

`list --json` and `show NAME --json` print the inventory for programs (the
desktop app reads it): every kit with its kind (`always` for the built-in
skill, `default`, `builtin`, `user`), path and whether `sandbox-ai kits` may
change it; per agent its skills, agents, commands, prompts, memory template,
marketplaces, plugins, notes and errors; sandbox-ai's own marketplace with the
kits that list each plugin; and what the plugin store holds. A damaged kit is
reported for that agent and does not stop the listing.

Only your kits in `~/.local/share/sandbox-ai/kits` are changed. sandbox-ai's
built-in kits and skill are read-only (a kit of yours of the same name replaces
a built-in kit for its agents), and the default kit is edited by hand. Nothing
is changed through a symlink: a kit, agent directory, `skills/` directory or
`marketplaces.json` that is a symlink (to a checkout, say) is refused, so change
it where it lives; `delete` and `remove-skill` remove only such a link.
`add-skill` takes a **snapshot**: the folder itself must not be a symlink;
inside it, symlinks, FIFOs, sockets and devices are skipped with a note, never
followed or opened (`.git` is left out too); the kit limits (5000 files,
64 MiB, 16 levels) apply, and the copy appears only once it is complete. When
the folder lies in a workspace or another folder a sandbox can or could write,
the command says so: review the copy, an agent may have written it.
`marketplaces.json` is checked as at a start and written atomically;
`$comment` and other entries stay. A marketplace source is `OWNER/REPO`
(GitHub), `https://...` (Git, or the URL of a `marketplace.json`) or
`sandbox-ai`; a folder on this machine is refused, since sandboxes get only
sandbox-ai's own marketplace as a directory: publish yours on a Git server or
GitHub. Codex plugins are not supported yet.

### Hooks in kits

A kit can carry the agent's hooks in `AGENT/hooks/`. They run only after you
have reviewed and approved them, and only as long as nothing in them changes.

- **Claude Code and Codex:** `hooks.json` holds the hooks in the format of the
  `"hooks"` object of Claude Code's settings, next to the scripts they run. A
  command names a file of the folder as `${HOOK_DIR}/FILE`:

  ```json
  {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "${HOOK_DIR}/check.sh", "timeout": 30}]}]}}
  ```

  Only `"type": "command"` hooks run from a kit.
- **OpenCode:** plugin modules (`*.js`, `*.mjs`, `*.ts`) at the top of the
  folder, with their helper files beside them.

`sandbox-ai kits add-hook KIT FILE` adds a hook:

- a script, with `--event` (default `PreToolUse`), `--matcher` (default:
  every tool) and `--timeout`;
- a `.json` file of hook definitions;
- an OpenCode plugin module.

`remove-hook KIT FILE` takes a hook out again. `kits show KIT` lists each
hook (event, matcher, command, the files it runs) and whether it is approved.
`list --json` and `show --json` carry the same under `"hooks"`: `hooks`,
`files` (with size, SHA-256 and executable bit), `sha256`, `trusted`, `notes`
and `errors`.

**Approval.** `sandbox-ai kits trust-hooks KIT` shows every hook and the full
text of every file in the folder. After you confirm (or with `--yes`), it
records the SHA-256 of the whole folder: every file's path, content and
executable bit. KIT is a kit's name, `default` or a `--kit-dir` folder.
`--revoke` takes the approval back.

- Approvals are kept in `~/.config/sandbox-ai/trusted-hooks.json`, next to
  your rules. A start refuses a configuration directory a sandbox can write.
- Any change to the folder takes the approval away, whoever makes it.
- An approval holds for its kit only. The same hooks in another kit, a
  project's `--kit-dir` say, need an approval of their own.
- A symlink or a special file in the folder means its hooks never run and
  cannot be approved.
- Nothing in a kit or in a project file approves anything: a project file
  with an approval of its own is refused like any unknown key.

**At the start** each kit's hooks folder is copied into the run, and the copy
is hashed: what runs is exactly what was approved, even if the kit changes
while the run starts. Hooks without approval do not run. The start names them
together with the command that approves them:

```text
agent-sandbox: kit review: 1 hook(s) for claude not approved (new, or changed since they were
approved): they do not run; review and approve them with: sandbox-ai kits trust-hooks review --agent claude
```

Approved hooks become part of the agent's managed configuration, the same
place as the [hooks of the rules for its own tools](#rulesets):

- Claude Code: `/etc/claude-code/managed-settings.json`;
- Codex: `/etc/codex/requirements.toml`. A Codex older than 0.131.0 would
  ignore it, so the start is refused, with or without tool rules;
- OpenCode: its plugin list.

`${HOOK_DIR}` becomes the read-only copy in the run. The rule hooks come first
and the kits' hooks follow, in kit order. Kit hooks run whether or not there
are tool rules: one mechanism. The agent's own settings run no hooks then
(`allowManagedHooksOnly`, `allow_managed_hooks_only`).

A rewritten tool input is checked by the rules too. A kit hook may answer
with `updatedInput`, a new input for the tool. With tool rules, each kit
`PreToolUse` hook therefore runs through a wrapper (`runtime.py
--internal-kit-hook`). The wrapper runs the hook within its own timeout and
asks the rules about the new input before the agent gets it. If the rules
refuse it, the call is blocked. An answer that names `updatedInput` but
cannot be read is refused too. In OpenCode the rule plugin runs a second time
after the kits' plugins. That second run asks again only for a call whose
arguments a plugin changed.

**Hooks of Claude plugins.** A plugin's own hooks (`hooks/hooks.json`, or
`"hooks"` in `.claude-plugin/plugin.json`) do not run while the managed hooks
are on. `sandbox-ai plugins claude trust-hooks NAME@MARKETPLACE` approves
them like a kit's hooks. It shows every hook and the full text of the files
its commands name, and it records the SHA-256 of the plugin's whole
installed folder, its links included (their targets, never followed). The
start then puts them after the kits' hooks, with `CLAUDE_PLUGIN_ROOT` set
as Claude Code sets it. Only `"type": "command"` hooks can be approved.
Without managed hooks Claude Code runs a plugin's hooks itself, as before.

Limits:

- Claude Code runs the hooks that match a call in parallel. A kit hook may
  therefore run for a call the rules refuse, but its answer never overrides a
  refusal.
- OpenCode plugins run inside the agent's process and can do anything it can.
  The second check catches changed arguments, not a plugin that works around
  it. Approve only hooks you trust.

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
| `-A`, `--allow-host NAME` | Resolve and pin a name; allow TCP/443. Names are case-insensitive (`Example.org` is `example.org`). `*.DOMAIN` allows every name below DOMAIN (see below). |
| `-a`, `--allow IP_OR_CIDR` | All protocols and ports to that destination. |
| `-t`, `--allow-tcp IP:PORT` | Exactly one TCP port on one address (IPv6: `[ADDR]:PORT`). |
| `-P`, `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in the sandbox reaches the same port on the host's loopback (repeatable). |
| `-R`, `--dns` | Real DNS via `1.1.1.1` and `8.8.8.8` (UDP/TCP 53 to them only). |
| `-ns`, `--nameserver IP` | Use this resolver instead (repeatable; implies `--dns`). |
| `-E`, `--egress-all-allow` | Allow every outbound destination and port; implies `--dns`. |
| `-nd`, `--no-provider-defaults` | Drop the agent's default hosts. |
| `-o`, `--offline` | No external network at all. |
| `-sn`, `--subnet CIDR` | The sandbox's own IPv4 network on the default network (both backends): the sandbox `.100`, gateway `.2`; see below. |
| `-s6`, `--subnet6 CIDR` | Podman: the sandbox's own IPv6 `/64` (`::100`, gateway `::2`). |
| `-nw`, `--network NAME`, `-ip`, `--ip`, `-i6`, `--ip6` | Podman: existing private network, static addresses. |

`--dns` resolves names but does not open the resolved addresses; resolvers can
carry data out in query names, so it is a deliberate widening.
`--egress-all-allow` keeps inbound traffic and forwarding closed and the
host's own loopback hidden, but the local network is reachable, including
services the host offers on its LAN addresses. `127.0.0.1` means the
sandbox itself, except for ports opened with `--allow-host-port`. IP filtering
cannot distinguish domains that share CDN addresses. Podman uses `pasta`,
Bubblewrap `slirp4netns`; no ports are published.

**Host wildcards.** `--allow-host '*.example.com'` (at the start, or `sandbox-ai net
allow` during the run) allows every name below `example.com` (`api.example.com`,
`a.b.example.com`), not `example.com` itself: give both if you want both. The
names are not known in advance, so none is pinned at the start: when the agent
looks one up, the DNS stub holds the lookup and files its question, the launcher
answers it on the host at once (resolved there, only public addresses, inside
the administrator's `allow_hosts`), and the stub hands the addresses back while
the client still waits; from then on the name is pinned and open for the run, as
an approved lookup is. A wildcard needs two labels below it (`*.com` is refused)
and a sandbox that asks (not `--ask off`); `net revoke` takes a wildcard back,
the names opened below it stay open for the run.

**Services on your own host** (a local Git server, a database) cannot be
reached with `--allow-tcp`: `pasta` gives the sandbox the host's own IP
address, so that address leads back into the sandbox. This avoids clashing
with any other machine on your network, but hides the host. Use
`--allow-host-port PORT` instead: pasta then forwards `127.0.0.1:PORT`
in the sandbox to the same port on the host's loopback, and
nothing else on the host becomes reachable. A Git remote such as
`http://localhost:3080/...` works unchanged inside the sandbox. The host is
the machine running Podman, so with `--podman-url` it is that machine. Only
the Podman backend with its default network supports this; it conflicts
with `--offline`, `--network` and `--backend bwrap`.

```bash
./sandbox-ai claude --workspace "$HOME/src/demo" --allow-host-port 3080
```

**The sandbox's own addresses** (`--subnet`, `--subnet6`). Without them,
Podman's `pasta` gives the sandbox the host's own addresses, and Bubblewrap's
`slirp4netns` uses `10.0.2.0/24` (the sandbox `10.0.2.100`, gateway
`10.0.2.2`, DNS forwarder `10.0.2.3`) and `fd00::/64`. `-sn`, `--subnet CIDR`
gives either backend another IPv4 network, for example when a VPN or LAN
uses `10.0.2.0/24`; both lay it out as slirp4netns does: the sandbox at
`.100`, the gateway at `.2`, the DNS forwarder at `.3` (Podman: `pasta -a .100
-n PREFIX -g .2 --dns-forward .3`; Bubblewrap: `slirp4netns --cidr`).
`-s6`, `--subnet6 CIDR` does the same for IPv6 with Podman, a `/64` in
`fc00::/7` (the sandbox `::100`, gateway `::2`); slirp4netns has no option
for its IPv6 network and always uses `fd00::/64`, so Bubblewrap refuses it.

```bash
./sandbox-ai claude --subnet 10.20.0.0/24 --subnet6 fd20::/64
```

Refused are: networks outside the private ranges (`10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `fc00::/7`), and so
loopback, link-local and multicast; an address instead of the network
(`10.20.0.100/24`); IPv4 prefixes outside `/8` to `/25` (the sandbox takes the
100th address) and IPv6 prefixes other than `/64`; a network that overlaps one
this host routes to (`/proc/net/route`, `/proc/net/ipv6_route`; not checked
with `--podman-url`, whose routes are another machine's); and a network that
holds a destination the run allows (`--allow`, `--allow-tcp`, a resolver, the
address a pinned host resolves to): such an address would reach the sandbox's
own link, gateway or DNS forwarder instead, never that destination. The one
exception is `--nameserver` at `.3`, the backend's forwarder to the host's
resolvers. Remembered approvals inside the network are skipped with a note,
and `sandbox-ai net allow` refuses destinations inside it. `--subnet`
conflicts with `--offline`, and with `--network NAME` the Podman network
decides (use `--ip`/`--ip6` there).

Nothing else depends on the sandbox's address: the firewall, the connection
proxy (it redirects to loopback), `/etc/resolv.conf` and `--allow-host-port`
stay the same. With `--subnet`, Podman's sandbox no longer shares the host's
address, so the host's own LAN address leads to the host, as with Bubblewrap,
not back into the sandbox; it is reachable only where the options allow it.
The `--dry-run` plan shows the addresses as `sandbox_network`, and the start
names a chosen network.

**Your defaults**: `~/.config/sandbox-ai/defaults.json` sets the network for
every start that does not choose one (options and project files win):

```json
{"version": 1, "subnet": "10.20.0.0/24", "subnet6": "fd20::/64", "keep_days": 90}
```

`keep_days` (optional) lets kept starts expire: every new start removes the
stopped ones unused for more than that many days, and `sandbox-ai gc` uses it
when no `--older-than` is given. Without it kept starts stay until you remove
them ([every start](#every-start-names-detach-keep-resume)).

It is read only if it lies outside every workspace, rw mount and
`--state-dir`, and it is not used with `--offline` or `--network`; on
Bubblewrap its `subnet6` is left out. The administrator's policy can set a
default and a ceiling as well ([Administrator policy](#administrator-policy)).
The key `"mode"` of `defaults.json` chooses the [security mode](#security-modes) of every start
that does not choose one.

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
./sandbox-ai claude --git-token-env https://github.com=SANDBOX_GH_TOKEN --git-identity
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
`http://localhost:3080` does not answer for `http://127.0.0.1:3080`. As
with GitHub, helpers from a `~/.gitconfig` in the sandbox are ignored for
that URL, and the agent can read the token (as `SANDBOX_GIT_TOKEN_0`, ...),
so create one limited to the needed repositories.

```bash
export SANDBOX_GITEA_TOKEN=...   # Gitea: Settings -> Applications, scope repository
./sandbox-ai claude --git-token-env http://alice@git.example.internal:3000=SANDBOX_GITEA_TOKEN --git-identity
```

Instead of a variable, `--git-token-file URL=FILE` reads the token from a file that belongs to you, has mode `0600`, is not
a symlink and lies outside every workspace. `sandbox-ai wizard` writes such files
under `~/.config/sandbox-ai/tokens/` when you type a token there (input
hidden); the project configuration then names only the file, never the token.

**Tokens the agent never sees**: with `--token-proxy` (`-tp`), the tokens of
`--git-token-env`, `--git-token-file` and `--git-token-secret` stay out of the
sandbox. They go only to the connection proxy, whose directory the agent
cannot read, and the proxy adds them to Git's own requests (`info/refs`,
`git-upload-pack`, `git-receive-pack`) to exactly the HTTPS server it checked the
certificate of. The agent clones and pushes as usual, through the privacy
check, but cannot read a token, use it for the server's API or send it
elsewhere. The token never appears in the connection log. Plain `http` servers
(a Gitea on the host's loopback) and `--git-credentials` are not supported this
way; `gh` gets no `GH_TOKEN` then.

```bash
sandbox-ai claude --git-token-secret https://github.com=GH_PUSH --token-proxy --git-identity
```

**Your existing Git logins**: `--git-credentials FILE` takes the logins from a
file in the format of Git's `store` credential helper, usually
`~/.git-credentials` (one `https://USER:PASSWORD@HOST[:PORT]` per line). Each
login answers for its server exactly as a `--git-token-*` option would, with
the same network rules: a login for `http://localhost:3080` implies
`--allow-host-port 3080`, one for `https://github.com` `--allow-github` and
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
./sandbox-ai claude --git-credentials ~/.git-credentials --git-identity
```

### Git logins in the proxy (sandbox-ai git-logins)

Your Git servers' logins can live outside every sandbox: list each server once,
and the connection proxy of every sandbox adds its token on the wire to Git's
smart HTTP requests to that server (clone, fetch, push), and nowhere else. The
agent's Git needs no credentials, and no token ever enters its sandbox; the
privacy check of pushes knows the tokens.

```bash
sandbox-ai secrets set GITEA_TOKEN                        # the token, from standard input
sandbox-ai git-logins add http://localhost:3080 --secret GITEA_TOKEN --user you
sandbox-ai secrets set GH_PUSH
sandbox-ai git-logins add https://github.com --secret GH_PUSH
sandbox-ai git-logins list | check | remove URL
```

The list (`~/.config/sandbox-ai/git-logins.json`, mode `0600`) holds servers,
users and secret names, never a token. Plain http is for this host's loopback
only (a local Gitea): the proxy adds that token only to the loopback port, which
the sandbox reaches as the host's (`--allow-host-port 3080`, or during the run
`sandbox-ai net allow --allow-host-port 3080`); an HTTPS login goes only to the
server whose certificate the proxy checked. The logins do not open the servers:
they must be reachable as usual (`--allow-github`, `--allow-host`). A
`--git-token-*` option for the same server wins for its start;
`--git-logins off` starts without them; runs without the connection proxy and
MCP servers' sandboxes never get them. With `--token-proxy` the
`--git-token-*` tokens join them in the proxy, the local Gitea's included.

### Publishing to registries and forges (sandbox-ai publish-logins, --publish)

npm, PyPI, TestPyPI, crates.io, GitLab and GitHub work the same way: the
token stays with you, and the proxy adds it on the wire. Unlike Git logins,
a start uses them only when it says so, because a package once published
cannot be taken back:

```bash
sandbox-ai secrets set PYPI_TOKEN
sandbox-ai publish-logins add pypi --secret PYPI_TOKEN
sandbox-ai publish-logins add gitlab --secret GL_TOKEN --url https://git.example.org   # your own server
sandbox-ai publish-logins kinds                       # what each kind opens, sets and asks about
sandbox-ai claude --publish pypi,npm                  # this start may publish there
```

With `--publish KIND[,KIND]` the sandbox reaches that kind's upload and API
hosts. The agent's tools get their usual variables, holding only a
placeholder: `TWINE_USERNAME`/`TWINE_PASSWORD`, `UV_PUBLISH_TOKEN`,
`POETRY_PYPI_TOKEN_PYPI`, `HATCH_INDEX_AUTH`, `FLIT_PASSWORD` (PyPI),
`NPM_TOKEN` with an npm configuration that uses it, `CARGO_REGISTRY_TOKEN`,
`GITLAB_TOKEN` and `GH_TOKEN`. The proxy puts the token in their place, only
in a credential header of HTTPS requests to that kind's hosts, PyPI's Basic
login included. The ruleset `publish` asks before every request that publishes
or takes something back: an upload, an npm publish, dist-tag or unpublish, a
crate publish, yank or owner change, a GitHub or GitLab release, a package,
and a file or commit written through the API, which a push's privacy check
would not see. With `--ask off` these requests are refused. `--publish github`
and a `--git-token-*` option for github.com together need `--token-proxy`,
since that option would give the agent `GH_TOKEN` itself. The list
(`~/.config/sandbox-ai/publish-logins.json`, mode `0600`) holds kinds, hosts
and secret names, never a token.

### Identities (sandbox-ai identities, --identity)

An identity is who a sandbox works as: a commit name and address, Git logins
and publish logins under one name. Choose it per start, in a preset or in the
project's `.sandbox-ai.json` (`"options": ["--identity", "c0decave"]`):

```bash
sandbox-ai identities add c0decave --name dash --email c0decave@users.noreply.github.com
sandbox-ai identities login c0decave https://github.com --secret GH_C0DECAVE --user c0decave
sandbox-ai identities publish c0decave pypi --secret PYPI_C0DECAVE
sandbox-ai identities add investigation --description "OSINT, no accounts"
sandbox-ai identities list | show NAME | check | logout NAME URL | unpublish NAME KIND | remove NAME
sandbox-ai claude --identity c0decave --publish pypi
```

Without `--identity` a start works as `default`: the logins of
`sandbox-ai git-logins` and `publish-logins`, and with `--git-identity` your
host's `user.name` and `user.email`. With `--identity NAME` it works as NAME
only: NAME's commit name and address (as `GIT_CONFIG_*`, so `--git-identity`
is refused alongside), NAME's Git logins instead of default's, and with
`--publish` NAME's publish logins. An identity without logins has none at all,
so a sandbox for an investigation carries none of your accounts. Two accounts
on one server are two identities. The tokens stay in `sandbox-ai secrets` and
reach only the connection proxy; the list
(`~/.config/sandbox-ai/identities.json`, mode `0600`) holds names, addresses,
servers and secret names. A URL with a password in it is refused everywhere a
Git server is named.

**Answers.** When the proxy has added a credential (a Git login, an `--llm`
or `--api-key` key, a publish token), it asks the server for an uncompressed
answer and masks that credential in it, in headers and body (chunked too),
with stars of the same length. A server that compresses anyway gets its
answer passed on as it came, and the log notes it as `unscrubbed`.

### Changing the network of a running sandbox

The network options are read at the start, but a running sandbox can be given
more (or less) without a restart:

```bash
sandbox-ai net list                                    # running sandboxes
sandbox-ai net allow --allow-host pypi.org --allow-github
sandbox-ai net allow claude-1a2b3c4d --allow-tcp 192.0.2.10:5432
sandbox-ai net show                                    # what was added during the run
sandbox-ai net revoke --allow-host pypi.org
sandbox-ai net allow --dns                             # real DNS lookups (or --nameserver IP)
sandbox-ai net allow --git-token-env http://localhost:3080=GITEA_TOKEN   # a Git login
sandbox-ai net allow --allow-host-port 3080                               # the host's 127.0.0.1:3080
sandbox-ai stop claude-1a2b3c4d                        # end it; its launcher cleans up
sandbox-ai net untaint claude-1a2b3c4d                 # take back the run's taint (see Rulesets)
sandbox-ai net allow --allow-host api.osv.dev --keep   # and for the next starts too (.sandbox-ai.json)
```

`--keep` also writes the options into the project's `.sandbox-ai.json` (in the
run's main workspace) and trusts the new file at once, so the next start has
them. It does so only while that file is exactly the one you trusted (the
agent can write the workspace, and its change must never be trusted along), or
creates one for the run's agent; when it cannot, nothing changes, not even the
run. A Git login is kept as its reference (`--git-token-env URL=VAR`), never as
a token.

`sandbox-ai net show [RUN] --keep` keeps what the run was given so far the same
way: what you gave with `net allow`, wildcards, networks, DNS, host ports,
devices and added workspaces, and what was approved "always". What a question
opened only once or for the run is left out (and named); `--keep-all` keeps it
too. Both print every option before the file is written, and both are refused
while the administrator's policy caps answers below "always".

The name of a run is shown by `list` and given to the agent as
`$SANDBOX_AI_RUN`, so it can name the exact command; it may be left out when
only one sandbox runs (or one for the current directory). `--allow-host` names
are resolved on the host and added to the sandbox's `/etc/hosts`; the
supervisor adds the addresses to the firewall and the proxy. Changes last
until the sandbox ends; the command prints the options that keep them. Only
you can do this: the control socket lies in `$XDG_RUNTIME_DIR/sandbox-ai/runs/RUN`
(mode `0700`) and is hidden from the agent. Connections already open stay
open after a `revoke`. `--dns` (or `--nameserver IP`, repeatable) gives real
DNS during the run: the sandbox's DNS stub forwards every name that is not
pinned to those nameservers and logs each lookup (`dns.jsonl` in the log
directory); only UDP and TCP 53 to them are opened, the addresses the names
resolve to stay closed unless you allow them too. A name that carries a canary
token is never forwarded. It needs the stub, which every run with the
connection proxy or `--log-dir` has.

`--allow-host-port PORT` (repeatable) lets the running sandbox reach the host's
`127.0.0.1:PORT`, as the start option does, through a relay: pasta cannot add a
forward to a running sandbox. The command starts a small process on the host
that listens on a Unix socket in the run's directory (mode `0700`, hidden from
the agent) and connects each connection to `127.0.0.1:PORT` of the host, never
anywhere else; in the sandbox the supervisor listens on `127.0.0.1:PORT` and
`[::1]:PORT` and joins the agent's connections to it. They pass the connection
proxy first, as the ports forwarded at the start do (so a push to a local Git
server meets the privacy check). Only you can start a relay (the agent reaches
neither the control socket nor the run's directory); at most 16 ports and 64
connections per port at a time; `revoke --allow-host-port PORT` ends it, and it
ends by itself when the run ends. A port forwarded since the start, or one
something in the sandbox listens on already, is refused. It works for runs
started with sandbox-ai 0.1.37 or newer; the administrator's policy and the
security modes `high` and `paranoid` refuse it as they refuse the start option.

`--git-token-env URL=NAME`, `--git-token-file URL=FILE` and
`--git-token-secret URL=SECRET` give the running sandbox a Git login, as at the
start: the token is read on the host (your variable, a file of yours with mode
`0600`, or `sandbox-ai secrets`) and written into the run's session directory,
which the sandbox sees read-only and which is removed when the run ends. A Git
helper that every start configures (before the logins of the start options,
which win for their server) answers Git with it; `show` lists the servers and
users, never the token, and `revoke --git-token-env URL=ANY` takes a login back.
The server is opened as at the start: `https://github.com` the GitHub preset,
another HTTPS server its name; a server on the host's loopback (`http://localhost:3080`)
is reached if the run forwards its port (`--allow-host-port`, or a login for it
at the start) or relays it (`net allow --allow-host-port PORT`, below). It works for runs started with sandbox-ai 0.1.37 or newer and
not with `--token-proxy` (the tokens stay outside the sandbox there); `gh` does
not see a login added this way (`GH_TOKEN` is set at the start only), and the
privacy check of pushes knows its token only by its shape. A run in the security mode `high` or `paranoid` is given nothing its
mode refuses ([security modes](#security-modes)).

`sandbox-ai stop NAME` ends a sandbox from another terminal, whatever kind of
start it is (a run, a session, an MCP server). For a run it sends SIGTERM to
the sandbox's launcher, which cleans up as after a normal exit, once it has
checked that the PID still is that launcher (yours, with the recorded start
time, running `agent_sandbox.py`), and waits up to `--wait SECONDS` (default
30) for it to end. `sandbox-ai net stop RUN` is the older form of it for runs. `list` also finds sandboxes started with another
`XDG_RUNTIME_DIR` (or none, e.g. from cron or `su`): it reads
`/run/user/UID/sandbox-ai/runs` and `~/.local/state/sandbox-ai/runs` too.

## VPN

A VPN can sit in front of a sandbox (Podman backend): the tunnel runs in a
container of its own in the pod's network namespace, a **kill switch** lets
nothing else out (IPv4 and IPv6), and before the agent starts, the addresses
and DNS seen from outside are **checked**. sandbox-ai's own firewall,
connection proxy and approvals work as always; their connections leave through
the tunnel.

```bash
sandbox-ai build vpn                                        # once: localhost/sandbox-ai-vpn
sandbox-ai vpn add home --wireguard ~/Downloads/wg-home.conf  # any wg-quick file: your own server, Proton, IVPN, AirVPN ...
sandbox-ai vpn add mv --mullvad --location se-sto           # asks for the account number (not shown, not kept)
sandbox-ai vpn add office --openvpn office.ovpn --auth-secret OFFICE_LOGIN
sandbox-ai vpn add ts --tailscale --auth-key-secret TS_KEY --exit-node exit-1
sandbox-ai vpn add anon --tor --socks --exit-country de     # Tor; --bridges FILE for obfs4 bridges
sandbox-ai vpn list                                         # also: show NAME, remove NAME, --json
sandbox-ai vpn check mv                                     # bring it up alone, check it, take it down
sandbox-ai claude --vpn mv                                  # or "options": ["--vpn", "mv"] in .sandbox-ai.json
```

| Type | Profile from | Tunnel |
|---|---|---|
| WireGuard | `--wireguard FILE`: any wg-quick configuration: several peers, endpoints as names or IPv4/IPv6 addresses, `AllowedIPs` subsets (a split tunnel), `PresharedKey`, `PersistentKeepalive`, `MTU`, `DNS`. Refused: `PreUp`/`PostUp`/`PreDown`/`PostDown` (wg-quick runs them as root), `Table`, `SaveConfig = true` | `wg0`: the kernel's WireGuard when the host has its module loaded, else wireguard-go through `/dev/net/tun` |
| Mullvad | `--mullvad [--account-secret SECRET] [--location se\|se-sto\|RELAY] [--no-ipv6] [--endpoint-ipv6]`: a new WireGuard key is registered with Mullvad's API (a device of your account), a relay chosen from its list. The account number comes from `sandbox-ai secrets` or is typed in, is used for that one request and is never written or printed | as WireGuard, DNS `10.64.0.1`; the check asks `am.i.mullvad.net`, which confirms a Mullvad exit |
| Proton VPN | Proton has no simple key API (its login is SRP): download a WireGuard configuration in your Proton account and add it with `--wireguard` | as WireGuard |
| OpenVPN | `--openvpn FILE [--auth-secret SECRET] [--split]`: a client `.ovpn`. Files it names beside it (`ca`, `cert`, `key`, `tls-auth`, `tls-crypt` ...) are taken in; a user name and password (`auth-user-pass FILE` beside it, or inline) move to `sandbox-ai secrets` as `USER:PASSWORD`. Refused: whatever runs programs (`up`, `down`, `script-security`, `plugin` ...), reads or writes other files (`config`, `log`, `management`, `askpass` ...), proxies, tap devices, `<connection>` blocks and unknown directives | `tun0`, a full tunnel unless `--split` (then the server's routes; the rest stays blocked) |
| Tailscale, Headscale | `--tailscale [--auth-key-secret SECRET] [--exit-node HOST] [--login-server URL] [--ephemeral] [--tailnet-allow CIDR]` | `tailscale0`: tailscaled in tun mode |
| Tor | `--tor [--bridges FILE \| --bridge-secret SECRET] [--exit-country CC[,CC]] [--socks]`: bridges are obfs4 or plain lines as bridges.torproject.org gives them, from a `0600` file (kept in `sandbox-ai secrets`) or a secret of yours | no interface: tor's TransPort takes every TCP connection, its DNSPort every lookup ([Tor](#tor)) |

**How it runs.** The launcher creates the pod, and the VPN container joins it:
`localhost/sandbox-ai-vpn` (WireGuard tools, wireguard-go, OpenVPN, Tailscale,
Tor, nftables), read-only, with no capability but `NET_ADMIN` (and `NET_RAW`
for tailscaled; for Tor `SETUID`, `SETGID` and `NET_BIND_SERVICE` instead of
`/dev/net/tun`: tor binds port 53, then becomes `debian-tor`), `/dev/net/tun`
and nothing of the host but its runtime script
(and Tailscale's node state). Profile and keys arrive on its standard input.
It resolves the endpoints once, loads the kill switch, brings the tunnel up,
runs the checks and resolves the agent's allowed host names through the VPN's
resolver; then the agent's container starts in the same pod. The tunnel lasts
until the agent ends.

**Kill switch** (nftables table `sandbox_ai_vpn` in the namespace, IPv4 and
IPv6 alike): packets leave only over `lo`, the tunnel's interface, and as the
tunnel's own packets, which carry its mark (WireGuard's fwmark and OpenVPN's
`--mark` `0xca6c`; tailscaled's `0x80000`), to the endpoint addresses, ports
and protocols resolved at the start. Setting a mark needs `CAP_NET_ADMIN`,
which neither the agent nor the proxy has, so sending to the endpoint itself
gets them nowhere; the endpoint's name is not looked up again. A connection is
not let through by its state: nothing that went through the tunnel continues
over the uplink when the tunnel goes away. A family the tunnel has no address
of (an IPv4-only VPN) is blocked, not leaked; a split tunnel routes only its
networks, the rest is refused. A tunnel that dies leaves the kill switch behind
(the run says so); nothing gets out until the sandbox ends. Tailscale with an
exit node lets tailscaled's marked traffic (coordination server, DERP relays,
peers) go anywhere, everything else only through `tailscale0`.

**Checks** before the agent starts (and with `sandbox-ai vpn check NAME`),
for IPv4 and IPv6 separately, from inside the namespace:

- the address the echo services see (default `api64.ipify.org` and
  `ifconfig.co`; for Mullvad `am.i.mullvad.net` first) must differ from this
  host's own public address, asked from the host at the same time; Mullvad
  must confirm its exit; a family the tunnel does not carry must stay
  unreachable;
- every VPN resolver must be reached through the tunnel's interface and
  answer; `whoami.akamai.net`, which answers with the address of the resolver
  that asked, must not show this host's own address (a DNS leak).

A failed check stops the start with the reasons; `--vpn-check warn` only
warns. The results go to the terminal and to `vpn.jsonl` in the run's log
directory (no keys). Choose the echo services per profile (`vpn add ...
--check-url URL`, `--no-dns-probe`) or for every profile in
`~/.config/sandbox-ai/vpn/checks.json` (`{"urls": [...], "dns_probe":
"whoami.akamai.net", "timeout": 8}`). A VPN that ends where this host is (your
own server at home, used from home) fails the address check by design; use
`--vpn-check warn` there.

**DNS.** Without `--dns` the allowed names stay pinned as always, resolved
through the VPN at the start. With `--dns` the sandbox's resolvers are the
VPN's: the configuration's `DNS`, Mullvad's `10.64.0.1`, what the OpenVPN
server pushes, Tailscale's `100.100.100.100`; reached through the tunnel.
`--nameserver` conflicts with `--vpn`; choose others in the profile (`vpn add
NAME ... --dns IP --force`).

**Tailscale.** With `--exit-node` every connection goes through the exit node.
Without one only the tailnet goes through Tailscale: the agent may reach
`100.64.0.0/10` and `fd7a:115c:a1e0::/48` (or what `--tailnet-allow` names;
the tailnet's ACLs apply too), everything else follows the sandbox's own rules
over the normal network, and there is no address check. The node's state is
kept per profile in `~/.local/share/sandbox-ai/vpn/NAME` (only the VPN
container mounts it), so a profile runs once at a time; `--ephemeral` (with an
auth key) gives every start a node of its own. Without an auth key the first
start prints a login URL. `--login-server` uses a Headscale server.

<a id="tor"></a>**Tor.** tor runs in the VPN container as a client (with obfs4proxy and
the GeoIP files). There is no tunnel interface: every TCP connection of the
namespace is redirected to tor's TransPort (`127.0.0.1:9040`; the agent's own
reach the connection proxy first, as always, so the allow-list, approvals, TLS
inspection and the connection log apply), every lookup on port 53 to its
DNSPort (`127.0.53.1`). The kill switch lets only tor's own user out
(`debian-tor`, uid 950, fixed in the image; only a process with `CAP_SETUID`
could take it on; an MCP server behind a tor profile whose image runs as uid 950
or a tor user name is refused, since it would share that exemption), over IPv4 TCP, to its bridges alone when it has some;
other UDP and all IPv6 are dropped. Everything leaves through Tor, the
agent's own API connections included. `.onion` names resolve into
`10.192.0.0/10` (AutomapHostsOnResolve), which the sandbox may reach unless the
policy's `allow_within` excludes it: with `--dns` every `.onion` the agent looks
up, without it the `.onion` names of its allow-list. `--socks` adds a SocksPort
at `127.0.0.1:9050` in the sandbox (`IsolateDestAddr`) for programs that
speak SOCKS: Tor Browser with `TOR_SKIP_LAUNCH=1`, Chromium or Playwright with
`--proxy-server=socks5://127.0.0.1:9050`, `curl --socks5-hostname`, torsocks.
What goes through it passes the connection proxy by (no allow-list, approvals
or connection log; Tor alone carries it), so it is off unless asked for. The
bridges never appear in the profile, the plan, the logs or a message, only
their number. The checks: tor must bootstrap to 100% within 240 s (the error
says how far it got), `check.torproject.org/api/ip` must answer `"IsTor":
true` for the address seen outside, tor's DNSPort must answer and map an
`.onion` name, a lookup sent to `1.1.1.1` must reach it too, and a literal
IPv6 address must stay unreachable. `sandbox-ai images recipe tor --from
claude` makes an image variant with torsocks and torbrowser-launcher (it
downloads Tor Browser and checks its signature at its first start; `--gui`).
Limits: TCP only (no UDP, so no QUIC: browsers fall back to TCP; no ping or
NTP), IPv4 only, and slower; many sites, the agent's provider among them, may
refuse or challenge Tor exits. The exit relay sees what is not encrypted: use
HTTPS. Inside the sandbox the connection proxy still opens TLS for its
inspection, as without Tor (a program behind the SocksPort meets the real
certificates). `--no-proxy` is refused. tor's DNSPort is closed to the agent
unless the start has `--dns` or `--egress-all-allow`; `sandbox-ai net allow
--dns` during a run does not open it, and a name allowed during a run is
looked up on the host, so an `.onion` name cannot be (start with it, or with
`--dns`).

**Secrets.** Private and preshared keys, OpenVPN credentials and auth keys
live in `sandbox-ai secrets` (keyring or `0600` files); the profiles in
`~/.config/sandbox-ai/vpn` (`0600`) hold the rest, and an OpenVPN profile's
configuration with its client key (`0600` too). Only the VPN container gets
them, on its standard input: never the agent's or an MCP server's files,
environment, logs or the dry-run plan (which shows profile, endpoints,
interface and checks). Once there are profiles, no workspace, mount
(read-only ones too), kit, `--copy` or file option may show these directories
or the secret files to a sandbox, and a workspace or mount that would let the
agent change a profile is refused.

**MCP servers.** A server with a command takes `--vpn PROFILE` in its
`options`; a catalog server with network gets one with `sandbox-ai mcp add NAME
--network full --vpn PROFILE` (or `"vpn": "PROFILE"` in `mcp.json`); `sandbox-ai
mcp start NAME --vpn PROFILE` puts one in front for that start. A catalog
server's containers join the VPN container's namespace (`--network
container:...`); a failed check stops the server.

**LLM servers.** An [LLM server](#llm-servers-local-and-remote) added with
`sandbox-ai llm add NAME ... --vpn PROFILE` brings its VPN along: `--llm NAME`
implies `--vpn PROFILE`, and the server's name is resolved through the tunnel
like the other allowed names.

**Limits.** Bubblewrap is not supported (`--vpn` with `--backend bwrap` is
refused, see TODO.md). The checks run once at the start. `--allow-host-port`
still reaches the host's loopback, past the tunnel, as asked. Names allowed
during a run (`sandbox-ai net allow --allow-host`, approvals) are looked up on
the host; the connections themselves go through the tunnel. An MCP server has
a network of its own: give it `--vpn` too (`require_vpn` does). The desktop
app's board keeps `--vpn` as an option of the command line until it has a VPN
node.

## LLM servers: local and remote

Every agent can work with a model on your own server instead of the cloud:
**Ollama**, **LM Studio**, the **llama.cpp** server (`llama-server`), **vLLM**
or any other **OpenAI-compatible** server (a LiteLLM gateway, for one), on
this machine, in your LAN, behind a VPN or on the internet. Register it once
by name; its key stays in `sandbox-ai secrets`:

```bash
sandbox-ai secrets set GPU_KEY            # the server's API key, if it has one
sandbox-ai llm add gpu --kind vllm --url https://gpu.example.org:8443 --api-key-secret GPU_KEY
sandbox-ai llm models gpu                 # what it serves now
sandbox-ai llm check gpu --agent codex    # address, TLS, key, model, the API Codex needs
sandbox-ai codex --llm gpu:Qwen/Qwen3-Coder-30B --no-provider-defaults
```

`--llm NAME[:MODEL]` opens exactly that server's address and port, configures
the agent for it and hands the agent a **placeholder instead of the key**.
Without `:MODEL` the start uses the endpoint's model (`llm add --model`) or
the only one the server listed. With `--no-provider-defaults` the agent
reaches nothing but the server (and what other options allow). The agent's
cloud login is not copied in (`--copy-auth` still does), and `--llm` takes the
place of `--local-config`. The wizard offers registered servers for every
agent, lists their models and writes `--llm NAME:MODEL`; a project file may
name one too, and an administrator's policy can forbid `--llm` or confine it
with `allow_within`/`allow_hosts` like any other destination.

| Agent | API | What sandbox-ai writes | Servers with that API |
|---|---|---|---|
| OpenCode | Chat Completions (`/v1/chat/completions`) | `~/.config/opencode/sandbox-ai-llm.json` (provider `llm-NAME` through `@ai-sdk/openai-compatible`, every model the server listed) and `OPENCODE_CONFIG` | all of them |
| Codex | Responses (`/v1/responses`; Codex dropped `wire_api = "chat"` in 0.95.0) | `~/.codex/sandbox-ai-llm.config.toml` (provider `llm-NAME`, `wire_api = "responses"`, `env_key` with the placeholder; update check, analytics, metrics, plugin sync and the hosted web search off), started with `--profile sandbox-ai-llm` (Codex 0.134 or later) | Ollama 0.13.3, LM Studio 0.3.29, llama.cpp b7793 (b9161 for Codex's tool types), vLLM 0.10.0 (streamed tool calls 0.18.0) |
| Claude Code | Anthropic Messages (`/v1/messages`) | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (the placeholder), `ANTHROPIC_MODEL` and the Opus/Sonnet/Haiku/subagent model variables, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; `skipWebFetchPreflight` in its settings | Ollama 0.14.0, LM Studio 0.4.1, llama.cpp b7187, vLLM 0.11.1 (and later); another server only with `llm add --anthropic` |

Claude Code speaks only the Anthropic Messages API: a start of Claude with a
generic `openai-compatible` server is refused and names the alternatives
(Codex or OpenCode with it, one of the four servers above, or a server that
offers `/v1/messages`, added with `--anthropic`). The version numbers are
where each server introduced the API; `sandbox-ai llm check NAME --agent
AGENT` asks the server itself (an empty request, nothing is generated).

**URLs.** `--url` names the server's root, with a path prefix if a gateway
puts one in front (`https://gw.example.org/ollama`); each agent adds its API
path, and a trailing `/v1` is dropped. IPv4, IPv6 (`https://[2001:db8::5]:8443`)
and host names work. `https` is checked against the system's CAs, or against
`--ca FILE` (copied to `~/.config/sandbox-ai/llm/NAME.pem`; the proxy trusts it
for that server only). Plain `http` goes only to:

- this host's loopback (`127.0.0.1`, `localhost`, `[::1]`): Podman only,
  implies `--allow-host-port PORT`; the agent uses `127.0.0.1`, so the server
  must listen there;
- private addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), CGNAT
  and Tailscale (`100.64.0.0/10`), link-local IPv4 and unique-local IPv6
  (`fc00::/7`, Tailscale's `fd7a:115c:a1e0::/48` included);
- a host name whose addresses are all of these (checked at `llm add` and at
  every start).

Anything else over plain `http` needs `llm add --insecure-http` and is warned
about at every start: prompts, code and the key would cross networks you do
not control unencrypted.

**Network.** An address is opened like `--allow-tcp IP:PORT`, a name on port
443 like `--allow-host NAME`; a name on another port is pinned at the start and
opened on that port alone (TCP/443 stays closed). With a path prefix on a host
name, the proxy lets only requests below the prefix reach that host (an
`--allow-http 'HOST * /PREFIX/**'` rule).

**The key stays out of the sandbox.** `--api-key-secret SECRET` names a
secret of `sandbox-ai secrets`; the endpoints file
(`~/.config/sandbox-ai/llm.json`, mode `0600`) holds only its name. At the
start the key goes into `proxy.json` in the connection proxy's directory,
which the agent cannot read. The agent's configuration carries the placeholder
`sandbox-ai-llm-key-placeholder`; the proxy replaces it in `Authorization`,
`x-api-key` or `api-key` with the key (and adds `Authorization: Bearer KEY`
when the agent sends no credentials), only for requests to that server: over
its scheme and port, to the name whose certificate it checked (`https`) or to
its pinned address (`http`), below its path prefix. A request elsewhere keeps
the placeholder. `--header-secret HEADER=SECRET` adds headers the same way, for
example `CF-Access-Client-Id` and `CF-Access-Client-Secret` of a Cloudflare
Access gateway. The key is in neither the agent's environment nor its files,
the dry-run plan or the connection log (the log drops the exact values, even
should a server echo one back; the connection shows `"llm": "NAME"`).
Because the proxy adds it, an endpoint with a key, header secrets or its own
CA refuses `--no-proxy` and `--no-inspect` for its host. Limits: a server
that echoed request headers in its answers would show the key to the agent
(the four servers above do not); over plain `http` the key crosses your LAN
unencrypted.

**VPN.** `llm add --vpn PROFILE` names the [VPN](#vpn) profile a server is
reached through. `--llm NAME` then starts the sandbox behind it, as `--vpn
PROFILE` would (Podman only, with its kill switch and checks), and resolves
the server's name through the VPN's resolver, so a name only the VPN knows
works. A start with another `--vpn` is refused (a sandbox has one VPN); a
policy that forbids `--vpn` refuses such an endpoint, and one that requires a
VPN counts it. `llm check` and `llm models` still run from the host's own
network.

```bash
sandbox-ai llm add NAME --kind ollama|lmstudio|llamacpp|vllm|openai-compatible --url URL \
    [--api-key-secret SECRET] [--header-secret HEADER=SECRET] [--ca FILE] [--vpn PROFILE] \
    [--model MODEL] [--anthropic] [--insecure-http] [--replace]
sandbox-ai llm list [--json]
sandbox-ai llm show NAME [--json]
sandbox-ai llm remove NAME              # the secrets stay in sandbox-ai secrets
sandbox-ai llm models NAME [--sort name|size|loaded] [--json]
sandbox-ai llm check NAME [--agent claude|codex|opencode] [--model MODEL]
```

| Server (`--kind`) | Default port | Models from | Size | Loaded |
|---|---|---|---|---|
| Ollama (`ollama`) | 11434 | `/api/tags`, `/api/ps` | yes | yes |
| LM Studio (`lmstudio`) | 1234 | `/api/v0/models` (fallback `/v1/models`) | when reported | yes |
| llama.cpp (`llamacpp`) | 8080 | `/v1/models` | yes | yes (router mode: per model) |
| vLLM (`vllm`) | 8000 | `/v1/models` | no | always (served = loaded) |
| other (`openai-compatible`) | none | `/v1/models` | no | unknown |

The listing sends the key as a Bearer token and the header secrets, uses the
endpoint's CA, no HTTP proxy of the host, follows no redirect (it would carry
the key elsewhere) and gives up after 10 seconds; the model names are kept for
the next start.

### OpenCode with a server in your LAN, without registering it

The wizard can also write a local OpenCode configuration directly (the way
before `sandbox-ai llm`, for servers without a key): it lists the models with
name, size and load state, sortable by each, and writes the following.
Manually:

```bash
./sandbox-ai wizard models --kind ollama --host 192.168.1.20 --sort size
./sandbox-ai opencode --workspace "$HOME/src/demo" \
  --local-config ./opencode-local.json --allow-tcp 192.168.1.20:11434
```

`--local-config` copies the OpenCode configuration privately, removes every
default cloud permission and disables project configuration, model catalog
fetching, auto-updates and sharing. `--allow-tcp` opens exactly the server's
port. The wizard's configuration uses OpenCode's OpenAI-compatible provider
(`http://IP:PORT/v1`). The sandbox cannot reach the host's `localhost` this
way: bind the server to the LAN (`OLLAMA_HOST=0.0.0.0`, LM Studio "Serve on
Local Network", `llama-server --host 0.0.0.0`, `vllm serve … --host 0.0.0.0`)
and use the host's LAN IP, or register it with `sandbox-ai llm add` as
`http://localhost:PORT`.

## Local MCP servers

Local MCP servers run **in their own sandbox** (Podman pod or Bubblewrap,
same Firejail guard), without credentials and, unless their definition says
otherwise, without network. `--mcp NAME` starts the server sandbox if it is not
already running, then connects the agent; the server keeps running for other
agents and sessions.

```bash
./sandbox-ai claude --workspace "$HOME/src/app" --mcp demo   # built-in test server
./sandbox-ai mcp list                                        # NAME  STATUS  SERVES  COMMAND
./sandbox-ai mcp stop demo
```

### An MCP server for a start that exists

```bash
./sandbox-ai mcp attach demo web               # web gets demo (kept for its later starts)
./sandbox-ai mcp attach demo web --restart     # web runs: restarted with it now
./sandbox-ai mcp start demo --for web          # start demo as web starts it, and attach it
./sandbox-ai resume web --mcp demo             # the same for a stopped start, started now
```

`mcp attach NAME START` (and `mcp start NAME --for START`) adds `--mcp NAME`
to the kept start's options, checked as its start checks it: the
administrator's policy (`forbid` `--mcp`), its security mode (`paranoid`
refuses a server with network) and the server's definition. `--for` starts
the server now with that start's backend and rules, so the start finds it
running. A running sandbox cannot take a new MCP server while it runs, with
either backend: its agent reads its MCP servers once, when it starts
(`--mcp-config`, `-c mcp_servers`, `opencode.json`), and the server's socket
reaches the sandbox only through a folder mounted at its start (a running
Podman container takes no new mount, and neither does Bubblewrap's mount
namespace). So a running start is restarted with it (`--restart`, or yes in
a terminal; the agent continues its conversation), else it reaches the
server from its next start, and the command says how to restart it. A run
without a kept start (`--no-keep`) has nothing to start again: end it and
start anew with `--mcp NAME`. `mcp list` and `sandbox-ai list` show whom
each server serves; the app's MCP page has **Attach to…**, the web
interface the same on a server's row of Starts and sessions.

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

**Signed catalogs.** `add` takes an entry only from a catalog whose
`catalog.json.sig` verifies against a key in `sandbox/mcp-signers` (an SSH
signature in the namespace `sandbox-ai-mcp`, checked with `ssh-keygen -Y
verify`, as releases are). The signature covers every image digest and rootfs
hash of the catalog, and sandbox-ai-mcp's `scripts/sign_catalog.py` makes it
only for servers built from their pinned sources, audited, and passed their
smoke tests with sandbox-ai's own run lines under Podman and Bubblewrap.
`sandbox-ai mcp catalog` shows `signed` or `unsigned` per entry; a definition
records the key that signed it (`signed_by`). A catalog that is not signed (one
of your own) needs `--unsigned`; a signature that does not verify is refused
whatever you pass.

**Bubblewrap.** Without Podman (or with `--backend bwrap`), `add` takes the
entry's rootfs instead of its image (from `rootfs.json` beside the catalog,
signed like it, since axross reads the catalog itself): an archive of the same image, checked
against the SHA-256 the signed catalog gives before anything of it is
unpacked, and unpacked without trusting it (no absolute paths, `..`, devices,
setuid bits or writes through a link) into
`~/.local/share/sandbox-ai/mcp-rootfs/SHA256/`, read-only. Each connection then
runs `bwrap --unshare-all --uid U --ro-bind ROOTFS / --proc /proc --dev /dev
--tmpfs /tmp --cap-drop ALL --clearenv --die-with-parent --new-session` with
the entry's folders and variables, in a systemd scope with the memory and
process limits, and, for network `full`, slirp4netns without the host's
loopback (as pasta for the containers). The arguments reach bwrap through a
pipe (`--args`), so a variable's value never shows on a command line.
`--backend both` keeps both forms; a run with `--backend bwrap` then uses the
rootfs.

The relay in front of every server is also a **gateway**: `mcp` rules in a
[ruleset](#rulesets) allow, ask or deny each tool call by tool and arguments,
`tools/list` leaves out the tools that are always denied, and every call is
written to `~/.local/state/sandbox-ai/mcp/NAME/mcp-calls.jsonl` with its
decision and result. JSON-RPC batches are refused (MCP has none since
2025-06-18).

### Another agent as a helper

`--helper AGENT` gives the agent a second (or third) agent to hand tasks to.
Claude Code, Codex and OpenCode can each lead, and each can help:

```bash
sandbox-ai claude --workspace ~/src/app --helper codex --helper opencode
sandbox-ai codex --helper claude
```

Each helper runs as an MCP server in a sandbox of its own, unlike other MCP
servers with everything an agent's start has:

- its login and only its provider's hosts;
- its kits, project memory and plugins;
- the run's `--rules` and the project's `rules` for its own tools, with its
  managed hooks (and the Codex version check);
- `--dangerously-skip-permissions` if the lead has it.

What narrows the lead narrows its helper too: `--workspace-ro`, the security
mode and `--no-auth`. `--helper` is refused with `--offline` and `--llm` (a
helper reaches its own provider) and in the modes `high` and `paranoid`.
An administrator's policy that forbids `--mcp` forbids it too.

It works on the lead's main workspace (`/work`). There is one helper per
agent and main workspace (`helper-AGENT-HASH` in `sandbox-ai mcp list`):
another project never gets this one's helper, and two leads on the same
workspace share it.

The lead sees one tool, `ask(prompt, continue)`. The helper does the task in
`/work` with its non-interactive mode and answers with its final message:
`claude -p`, `codex exec` (with `--sandbox danger-full-access`, as every Codex
here), `opencode run`. With `continue` it goes on in its last conversation.
The call goes through the relay like every MCP call: `mcp` rules, questions,
the run's taint and `mcp-calls.jsonl` hold for it. A Codex lead waits up to
2000 seconds for it (`tool_timeout_sec`).

- **One task at a time.** A lock in the helper's HOME holds across
  connections. A second lead's task is refused as "busy" while one runs.
- **30 minutes.** A task that runs longer is stopped.
- **Fewer settings from the lead.** The lead shares `/work`, so the helper
  reads no project settings or project MCP servers from it. Claude runs with
  `--setting-sources user --strict-mcp-config`, Codex does not trust the
  folder, and OpenCode runs with `OPENCODE_DISABLE_PROJECT_CONFIG=1`. Git
  runs no fsmonitor, repository hooks or `.gitattributes` filters there (the
  filters need Git 2.42 or newer).

Limits:

- Both agents write the same workspace.
- The helper still reads the instructions in `/work` (`AGENTS.md`,
  `CLAUDE.md`) and does what the lead asks of it, and other Git settings in
  `/work/.git/config` (aliases, `core.sshCommand`, diff drivers) can still
  run code when it uses them. A lead you do not trust can
  steer its helper with words. Give a helper what you would give the lead,
  its login included.
- A Claude helper without `--dangerously-skip-permissions` refuses tools
  that would need your approval, since nobody can answer its questions.
  Codex and OpenCode helpers act without asking in any case: Codex's own
  sandbox cannot set up inside sandbox-ai's, and the helper's sandbox is the
  boundary.
- The helpers' commands were checked against Claude Code 2.1. Those of Codex
  and OpenCode follow their documentation and have not been run here yet.

### Other agents in the same sandbox

`--with AGENT` puts other agents into the agent's own sandbox, for it to call
from the shell:

```bash
sandbox-ai build team                                   # once: all three agents in one image
sandbox-ai claude --with codex --with opencode
```

In the sandbox the lead runs `codex exec -- "TASK"`, `opencode run -- "TASK"`
or `claude -p -- "TASK"`, and `$SANDBOX_AI_COMPANIONS` names the agents that
are there. Each agent --with gets:

- **Its login,** copied like the lead's automatic login, and `--no-auth`
  holds for it too. A `--copy` of its login file keeps that account instead.
  A refreshed Claude login is written back to the host, and refreshed Codex
  and OpenCode logins to sandbox-ai's login store.
- **Its provider's hosts.** They count as this run's providers, for the
  rules and the taint.
- **Rule hooks:** the rule hooks of the run's rules in its managed
  configuration, and the Codex version check.
- **Codex:** `sandbox_mode = "danger-full-access"` in `~/.codex/config.toml`
  unless that file chooses otherwise, as the launcher gives the lead Codex.

`--with` is refused with `--offline` and `--llm`, and in the modes `high` and
`paranoid`: it opens another provider. The start uses
`localhost/team-sandbox:latest` unless `--image` names another. With
Bubblewrap the host's own executables are used.

Compared with [helpers](#another-agent-as-a-helper), this is simpler and
weaker:

- All agents share one network, one HOME and one workspace.
- The kits are the lead's only.
- OpenCode's rule hooks come with `OPENCODE_CONFIG_CONTENT`, which the lead
  can leave out when it starts OpenCode (like any second agent started from
  Bash).

### A team of agents

`sandbox-ai team` starts one sandbox per agent on a shared workspace, and they
work together through a task board:

```bash
sandbox-ai team start app --agents claude,codex,opencode -w ~/src/app -- --allow-pypi
sandbox-ai attach app-claude                    # talk to the lead
sandbox-ai team show app                        # members, their state, the board
sandbox-ai team post app codex "run the tests"  # a task from you
sandbox-ai team stop app                        # every member and the board
sandbox-ai team delete app --yes                # with the members' HOMEs
```

Each member is a named session (`app-claude`, `app-codex`, ...), and you
attach, pause, resume or stop it like any start. The options after `--` hold
for every member. A team never takes over a session whose name is in use
already. `stop` and `delete` end with status 1 when a member could not be
stopped or deleted, and `delete` then keeps the team.

The first agent leads. It starts with a prompt that names its team and the
board's tools. The others start with a prompt to wait for tasks, do them and
report back.

The board is the MCP server `team-NAME` in a sandbox of its own, without
network. It has these tools:

- `post(to, task)`;
- `wait_for_task(agent)`;
- `done(id, answer)`;
- `wait_for_answer(id)`;
- `board()`.

Every member reaches it through its relay, so `mcp` rules, questions and
`mcp-calls.jsonl` hold. Its file lies in
`~/.local/state/sandbox-ai/teams/NAME/board/`, and `team show` prints its text
with control characters escaped.

- **Waiting:** a wait lasts 50 seconds by default.
- **Lost tasks:** a task a member took but never reported comes again at its
  next `wait_for_task`, in case the answer that handed it out was lost.
- **Size:** the board keeps up to 1000 tasks and 48 MiB. Done tasks make
  room, open ones never.

Limits:

- The members share the workspace. The lead's prompt asks it to split the
  work, but nothing stops two agents from changing the same file.
- The board does not know who calls it: a member names itself. The members
  are your own agents, but one of them could take another's task.
- OpenCode's first prompt is given with `--prompt`, which has not been run
  here yet.

## Other hosts

`sandbox-ai hosts` manages other machines (or Proxmox LXC containers, see
[PROXMOX_LXC.md](PROXMOX_LXC.md)) whose sandbox-ai this one controls over SSH.
The desktop app and the web interface use the same list.

```bash
sandbox-ai hosts add ct1 user@ct1.lan --description "Proxmox 101"
sandbox-ai hosts test ct1                 # SSH, its sandbox-ai version, its doctor
sandbox-ai hosts run ct1 -- list          # sandbox-ai list on ct1
sandbox-ai hosts run ct1 -- attach web    # into a session there (a terminal over SSH)
sandbox-ai hosts list                     # each host's sandbox-ai version and whether an update is there
sandbox-ai hosts install ct1              # sandbox-ai onto ct1 (this machine's copy; --download: its signed release)
sandbox-ai hosts install ct1 --system     # for every user there: as root over SSH, else with sudo
sandbox-ai hosts update ct1               # sandbox-ai update on ct1: old -> new version
sandbox-ai hosts update --all             # every host of the list, one after the other, and a summary
sandbox-ai hosts remove ct1
```

The list lies in `~/.config/sandbox-ai/hosts.json`, next to `local` (this
machine). Each host is reached with your SSH keys:

- `BatchMode`: never a password prompt that hangs;
- the host key is learnt at the first contact (in
  `~/.config/sandbox-ai/known_hosts`, `~/.ssh/known_hosts` is read too) and
  checked strictly after that;
- each argument is quoted on its own;
- `--identity FILE` picks a key, `--port N` a port.

ssh refuses a key file that others can read ("UNPROTECTED PRIVATE KEY
FILE"). `hosts add` and `hosts test` (and the Hosts pages of the app and the
web interface) make such a key private (`chmod 600`), say which file and its
old mode, and try the connection again: the host's `--identity`, else your
`~/.ssh/id_*`, and a key ssh names as too open. Only regular files you own
are changed, never through a symbolic link (that one is named, with the
command); a `~/.ssh` that others can write to is named, not changed.

Nothing listens on the network on the hosts: they need sshd and sandbox-ai
only. A non-interactive SSH login reads no `~/.bashrc` on Debian, so the
command falls back to `~/.local/bin/sandbox-ai` (where `install.sh` puts it);
`--command PATH` names another place.

### Updating the hosts

`sandbox-ai hosts update NAME...` (or `--all`) runs each host's own
`sandbox-ai update`: install.sh's update, checked against the release keys
of the installation there, as the same kind of installation. It prints the
old and the new version per host, what went wrong, and a summary;
`--version X.Y.Z` picks a release, `--jobs N` updates N hosts at a time,
`--json` is for programs. `hosts list` shows each host's version and whether
a newer release is there (the host's own `update --check`, kept for 10
minutes; `--refresh` asks now, `--cached` asks no host).

| Installation on the host | What `hosts update` does |
|---|---|
| install.sh for a user (`~/.local/share/sandbox-ai`) | `sandbox-ai update --user` as that user |
| install.sh as root (`PREFIX/lib/sandbox-ai`) | `sudo -n sandbox-ai update --system --prefix PREFIX` with `--sudo` or the host's setting (`hosts set NAME --update-sudo on`, or `hosts add ... --update-sudo`); without it, it says so |
| a checkout, pip, a package (pyz, AppImage) | nothing: it names the installation and the way to update it (git, pip, the release page), or to install it with install.sh |

`sudo -n` never asks for a password. When sudo there wants one, `hosts
update` says so and, from a terminal, runs the update there with `ssh -t ...
sudo`, where sudo asks; the app and the web interface open it in a terminal
tab. A rule in sudoers (`NOPASSWD` for that one command) spares the
question. This machine updates with `sandbox-ai update` (`--all` leaves it
out).

## Project configuration and wizard

```bash
./sandbox-ai wizard                  # interactive setup, writes .sandbox-ai.json
./sandbox-ai wizard edit [DIR]       # change the existing configuration of a project
./sandbox-ai wizard trust FILE       # review and trust a configuration
./sandbox-ai wizard forget FILE      # remove it from the trusted list
```

The wizard asks for the project directory, the [security mode](#security-modes)
(Enter keeps the inherited one: nothing is written then; later questions offer
only what the mode allows), extra workspaces, agent, backend,
network (or an LLM server registered with `sandbox-ai llm`, for every agent;
for OpenCode also a local server directly), permission-free mode, GUI, tmux and
logs. Network is one of `default` (the agent's provider only), `development`
(plus PyPI, GitHub and the OpenAI and Anthropic APIs), `frontier`, `dns`,
`egress-all` or `offline`; unless offline, an advanced question (default no)
then sets the sandbox's own addresses (`--subnet`, with Podman `--subnet6`),
checked against this host's routes as they are typed. Answers can be edited like in a shell (Backspace,
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

**Editing.** `sandbox-ai wizard edit [DIR|FILE]` (or `new` on a project that has a
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
(`--copy-auth`, `--no-auth`, `--auth-file`, `--import-codex-auth`, a `--copy`
of a credential file) replaces the file's, and agent arguments after `--` replace the saved ones; a switch the file
sets (e.g. `--offline`) cannot be unset from the command line, so use
`--no-config` for that. **A project file only takes effect once you trust it:** a cloned
repository could otherwise widen the sandbox (`--egress-all-allow`,
`--mount ~/.ssh:…`). The SHA-256 of each trusted file is kept in
`~/.config/sandbox-ai/trusted.json`; the wizard trusts what it writes, and any
change requires `sandbox-ai wizard trust` again, which trusts exactly the bytes it
showed you. The file is hashed and parsed from a single read, must be a
regular file and is refused if it is a symlink. Deleting a trusted file does
not silently drop its restrictions: the launcher stops with an error until it is
restored, `--no-config` is given, or `sandbox-ai wizard forget FILE` removes it from
the trusted list. The file cannot set `--workspace` (the main workspace is where
the file lives), `--session`, `--config` or internal options, and option names
must be written in full (no abbreviations). Paths (`workspaces`, `--mount`,
`--state-dir`, token files ...) must be absolute or start with `~`, so the file
means the same wherever the launcher is started. The key `"rules"` holds a
[ruleset](#rulesets), hashed with the file; it can only narrow what your own
rulesets allow. `--rules` cannot be set in a project file: a rules file
elsewhere would not be covered by the hash. `"log_debug": true` is the same as
`--log-debug`. `"mode": "high"` chooses the project's
[security mode](#security-modes) (`--mode` on the command line wins). `"name": "shop"` is the PROJECT part of the automatic names
(`claude-shop-1a2b`; default: the main workspace's folder name; `--project-name`
on the command line wins); `--name` and `--project-name` cannot be set in
`options`. `"keep": false` discards the HOME of the project's starts at the end
(`--no-keep`; `--keep` on the command line wins).

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

## Every start: names, detach, keep, resume

Every start of an agent has a name, is listed by `sandbox-ai list` and can be
managed by that name. Its HOME is kept, so a stopped start can be brought back
later: a new sandbox with the same options and HOME, in which the agent
continues its last conversation.

```bash
sandbox-ai claude -w ~/src/app                    # claude-app-3f9a: in tmux, this terminal attached
# Ctrl-b d or closing the terminal detaches; the agent works on.
sandbox-ai list                                    # what runs or is paused, and the kept starts
sandbox-ai attach claude-app-3f9a                  # back into it
sandbox-ai rename claude-app-3f9a app              # another name; the run id stays
sandbox-ai pause app                               # freeze every process
sandbox-ai resume app                              # thaw it
sandbox-ai stop app                                # end it; HOME, options and logs are kept
sandbox-ai resume app                              # later: reborn, the agent continues its conversation
sandbox-ai resume app --restart -w ~/src/lib       # stopped and started again with /workspaces/lib added (kept)
sandbox-ai list --history app                      # start, exit, pause, resume, stop, rebirth, rename
sandbox-ai delete app --yes                        # remove it with its HOME
sandbox-ai gc --older-than 30 --dry-run            # kept starts unused for 30 days, and what would go
sandbox-ai claude --no-keep -- -p "review the diff"   # for scripts: the HOME goes when it ends
```

**Names.** A start is named `AGENT-PROJECT-xxxx` (`claude-app-3f9a`): PROJECT
is `--project-name`, else the project file's `"name"`, else the main
workspace's folder name made fit for a name; `xxxx` is random. `--name NAME`
chooses the whole name. Names have 1-40 letters, digits, `_`, `.` or `-` and
start with a letter or digit; `mcp-…` belongs to the MCP server sandboxes. A
name in use by a running, paused or kept start is refused with a hint
(`sandbox-ai resume NAME`, `delete`, or another name). `sandbox-ai rename OLD
NEW` changes only the name: the run id (the sandbox's `$SANDBOX_AI_RUN`, its
control socket, connection logs, approvals and activity) stays. Every command
takes the name or the run id, and everything that shows a run shows its name
(`net list`, approvals, `activity`).

**Interactive starts are sessions.** A start from a terminal that runs the
agent's own interface (no prompt, no subcommand) runs as a detachable session
([tmux mode](#tmux-mode)): a supervisor in the background runs the sandbox,
your terminal attaches to tmux inside it (window `agent`, window `shell`).
Ctrl-b d or closing the terminal detaches; the agent works on, and
`sandbox-ai attach NAME` brings the terminal back. When the agent ends, the
command returns with its exit status.

**Tasks stay in the foreground.** `-p`, `codex exec`, `opencode run`, a start
without a terminal (piped, from a script), `--no-tmux`, `--probe` and
`--dry-run` run as before: in the foreground, with their output and exit
status. They are listed while they run, can be paused, resumed and stopped by
name, and have a history; they cannot be attached. Their HOME is kept too (not a
probe's or a plan's), so
`sandbox-ai resume NAME` can continue such a task's conversation interactively.

**The HOME is kept.** By default a start keeps its HOME, its options and its
logs in `~/.local/state/sandbox-ai/sessions/ID/` when it ends: `home/` (the
sandbox's `/root`: conversations, settings, logins, what was installed there),
`session.json` (agent, options, agent arguments, name, run), `supervisor.log`
(a session's launcher output), `exit.json` (the last run's exit status),
`home-manifest.json` (see below). `--no-keep` (or `"keep": false` in the
project file) discards the HOME when the start ends, as every start did before
0.1.30: a fresh HOME each time means a compromised agent cannot leave anything
for the next start. It suits scripts and one-off tasks. `--keep` is the
default made explicit (it overrides a project file's `"keep": false`).
`--state-dir DIR` is a HOME of your own: it stays anyway, and the start keeps
no second one. Probes, plans and MCP servers keep nothing. The
[security mode](#security-modes) `paranoid` keeps no HOME at all: every start
discards it, `--keep` and `--session` are refused, and a kept HOME is not
brought back under it; `high` keeps HOMEs. A kept HOME keeps its
[canary files](#canary-files) from run to run (the same files and tokens).

Kept starts never expire on their own. `sandbox-ai gc` removes what is left
over (sessions that ended without keeping their HOME), `sandbox-ai gc
--older-than DAYS` also the stopped kept starts unused for that long and the
histories of names that are gone; `--dry-run` only lists, `--json` for
programs. With `"keep_days": N` in `~/.config/sandbox-ai/defaults.json` kept
starts expire: every new start removes the stopped ones unused for more than N
days (and says so), and `sandbox-ai gc` uses N when no `--older-than` is given.

**Rebirth.** `sandbox-ai resume NAME` thaws a paused start; on a stopped kept
start it creates a new sandbox with the same options and HOME (a *rebirth*,
with a new run id, linked to the name in its history), and the agent continues
its last conversation: `claude --continue`, `codex resume --last`,
`opencode --continue` (each limited to `/work`, the same for every start). The
original agent options (from the command line, else the project file's
`agent_args`, e.g. `--model sonnet`) are kept before them up to the first
prompt; prompts, one-shot tasks (`-p`, `exec`, `run`) and conversation choices
(`--resume`) are not repeated. With `--binary` the original arguments are used
again. The project's `.sandbox-ai.json` is read again at every start.
`--session NAME` (the older form) starts a kept, named session, or brings a
stopped one of that name back; options given then are not applied (the
launcher warns and names them). **Running processes are not checkpointed:**
what survives is `home/` and the workspaces. Processes, `/tmp`, the tmux
scrollback and files written elsewhere in the container are gone; a dev server
or a build the agent had started must be started again. "Last conversation" is
the most recent one in that HOME, so one started by hand in the `shell` window
takes precedence.

**What changed in the HOME.** A kept HOME is the one thing a compromised agent
could use to reach its next start: a line in `.bashrc`, a hook in
`.claude/settings.json`, a plugin or skill, an MCP server in `~/.claude.json`,
a git alias, an SSH configuration, a `.pth` file Python runs. So at every start
of a kept HOME the launcher records a manifest of these files (hashes, modes,
link targets, and the text of small files that hold no keys or tokens) in the
start's directory, outside the HOME, where the sandbox cannot reach it. When
the start is reborn, the HOME is compared with it and every change since the
last start is named before the sandbox starts; in a terminal you confirm with
Enter (or stop with `n`). `sandbox-ai resume NAME --review-home` shows the
differences instead of starting (`--json` for the app: `changes` with `path`,
`change`: added, removed, changed, mode or type, and `diff`). Changes you made
yourself, or that the agent made at your request (a setting, a plugin), are
expected; anything else is a reason to look before going on. Keys and tokens
(`~/.ssh/id_*`, `.git-credentials`, `.netrc`, ...) are compared by hash only
and never shown.

**Pause and stop.** `pause` freezes the whole Podman pod (cgroup freezer) or,
with Bubblewrap, sends SIGSTOP to every process of the sandbox tree (for a
foreground task: below its launcher, which keeps your terminal); `resume`
thaws it. `stop` sends SIGTERM to the supervisor (or the task's launcher),
which runs the normal cleanup, including the Claude credential write-back;
MCP server sandboxes (`mcp-NAME` in the list) are stopped the same way.
Commands that change a start hold a lock, so a second one at the same time
fails with a message instead of launching twice. After a crash, `kill -9` or a
reboot the supervisor could not remove its Podman pod; the next rebirth
removes it, but only if the pod carries this start's label
(`sandbox-ai.session`); any other pod of that name is left alone and the
error names the `podman pod rm --force` command.

**History.** Each name has a small history in
`~/.local/state/sandbox-ai/history/NAME.jsonl` (start, exit with status,
pause, resume, stop, rebirth, rename, delete; time and run id each), shown by
`sandbox-ai list --history NAME` (`--json` for programs). A rename moves the
start's entries to the new name. `sandbox-ai list --all` adds the MCP servers
that do not run.

**The older forms** stay: `--session NAME` and `sandbox-ai session
list|attach|pause|resume|stop|delete` work as before, on the same starts (the
single command `sandbox-session` is deprecated).

Attaching never runs a tmux client on the host: the tmux server belongs to the
agent and could tell a connected client to execute commands. Instead a relay
inside the sandbox, behind Firejail, runs `tmux attach` on its own
pseudo-terminal, and `sandbox-ai attach` only exchanges terminal bytes and the
window size with it over `tmux/attach.sock`. `--session` cannot be combined
with `--state-dir` or `--probe`.

## tmux mode

tmux is the default, and with it every interactive start is detachable:
whenever stdin and stdout are a terminal, the start runs as a
[session](#every-start-names-detach-keep-resume). A supervisor in the background
runs the sandbox, the agent runs in tmux inside it with window `agent` and
window `shell` (both behind the same Firejail and firewall), and your terminal
attaches. **Ctrl-b d or closing the terminal detaches**; the agent works on, and
`sandbox-ai attach NAME` brings it back. When the agent exits, the shell
stays; leaving the last window ends the sandbox and the command returns with
the agent's exit status. The start's name (`AGENT-PROJECT-xxxx` or `--name`) is
printed when it begins and shown by `sandbox-ai list`.

tmux is used only when the agent arguments are options alone (`claude`,
`claude --model opus`); a subcommand, prompt or print-and-exit option
(`claude -p`, `claude mcp list`, `codex exec`, `opencode run`, `--version`)
runs in the foreground without tmux, so its output and exit status stay
visible. It also stays off for probes, MCP servers, `--binary` and with
`--backend bwrap` when the host has no tmux. Such starts are tracked (listed
while they run, paused and stopped by name, with a history) but cannot be
attached. `--no-tmux` runs the agent in the foreground too; `--tmux` insists
(and fails without a terminal).

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
./sandbox-ai codex --workspace "$HOME/src/app" --gui --egress-all-allow \
  --state-dir "$HOME/.local/state/sandbox-ai/pw" --binary /bin/bash
# inside: uvx --from playwright playwright install chromium
#         uv run --with playwright python my_browser_test.py   # headless=False works
```

**Sound** (`--audio virtual`): a PulseAudio server inside the sandbox, with a
null sink: what programs play can be recorded from its monitor (text to speech,
screen videos with sound, `ffmpeg -f pulse -i sandbox.monitor`), and nothing
reaches your sound card or your microphones. It runs as a user of its own,
without capabilities; the agent's programs find it through `PULSE_SERVER`. The
image needs PulseAudio: `sandbox-ai images recipe media --from claude` makes the
variant `media` (ffmpeg, PulseAudio, SoX, espeak-ng, ImageMagick, scrot, Piper),
`sandbox-ai build media` builds it, `--image-variant media` starts it. Without
PulseAudio in the image the start says so and goes on without sound. Playing
through your own speakers is not offered: your sound server would hand the
sandbox your microphones as well.

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
| GPU for models | Ollama, llama.cpp or vLLM | `--llm NAME` or `--allow-host-port 11434` and similar, see [LLM servers](#llm-servers-local-and-remote) |

Only the protocol of that service reaches the host. The device can be
replugged at will, because the service, not the sandbox, opens it.

### 2. `--device PATH`

```bash
sandbox-ai claude --device /dev/serial/by-id/usb-Raspberry_Pi_Pico_E660-if00
sandbox-ai claude --device usb:2e8a:000a       # the same board by its USB IDs, whatever node it gets
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

**By USB identity.** `--device usb:VENDOR:PRODUCT[:SERIAL]` (the IDs as
`lsusb` shows them) finds the device through sysfs and passes its nodes: its
serial port, camera or sound nodes and its raw USB node
(`/dev/bus/usb/BBB/DDD`). Nodes `--device` refuses (`hidraw`, `input`) are left
out, and so is the raw node of a device with a HID, mass-storage, smart-card or
Bluetooth interface: through raw USB the agent could take such a device from
its kernel driver. Several devices with the same IDs are refused unless a
serial tells them apart, and so are two that claim the same serial.

**During a run.** `sandbox-ai devices add [RUN] PATH|usb:VENDOR:PRODUCT[:SERIAL]`
hands a device to a running sandbox, with the same checks as `--device`
(refused devices, the udev rule, Bluetooth, the administrator's policy, the
[device rules](#rulesets)); `devices remove [RUN] PATH|usb:...|ID` takes an
added one away. `devices list [RUN] [--json]` shows every device of the run,
given at the start or added: its nodes, what it is (IDs, product name, the
serial as a short hash, USB port), the rule that decided and its state
(`attached`, `held` for a question, `gone`, `dropped`, `refused`, `removed`).
Nothing runs in the sandbox for it: a helper of your user enters the sandbox's
user namespace, which you own, and mounts the node into each of the sandbox's
mount namespaces, the agent's under Firejail included (Linux 5.2 or later).
Devices given at the start stay.

**Unplugging and replugging.** While the sandbox runs, the launcher watches
each single USB device it was given, through sysfs once a second:

- Unplugged, or another device on its node now: its nodes are taken from the
  sandbox (`detach`). A node left there would open whatever device gets its
  device number next.
- Given by identity (`usb:...`): when a device with these IDs (and serial)
  comes back, under whatever node (`ttyACM1`, a bootloader mode), and the
  device rules still allow it, its nodes are attached again (`replug`). With
  several candidates the one on the same USB port is taken, else none. Given by
  path: only the same device (IDs and serial) back at the same path.
- Gone for more than 5 minutes: dropped (`drop`); plugging it in later does
  nothing.

Directories (`/dev/bus/usb`, `/dev/dri`, `/dev/snd`) are bound whole, so
devices plugged in later appear in them, unwatched.

**Device rules.** The `"devices"` rules of the [rulesets](#rulesets) judge
each device at the start, at `devices add` and at each replug. A deny refuses
it (the start stops). An ask holds it: the sandbox starts without it, the
question (kind `device`: its nodes, IDs, product, the serial's hash, the rule)
appears in `sandbox-ai approvals`, the app and as a desktop notification, and
once allowed the device is attached. *Allow always* counts for the run, and
with `run` a replug is not asked about again. With `--ask off` a start with a
device an ask rule holds is refused; `devices add` waits for the answer at the
terminal.

**The log.** With a log directory (the default with the proxy),
`devices.jsonl` records each grant, refusal, question, answer, attach, detach,
replug, drop and removal, with the device's IDs, product, port, the rule and
the ruleset. Serial numbers are written as a short hash (`sha256:...`; in full
with `--log-debug`): they can identify a person's device. Opens of the nodes
are counted through inotify on the host and written at most every 10 seconds
per node (`open`: how many opens, how many closes after writing). The watch is
on the device node's inode, which the sandbox's bind mount shares, so it sees
opens inside the sandbox; it also sees the host's own (ModemManager, udev
probes), which it cannot tell apart, and nothing of what a process does with
a node it keeps open.

**What USB IDs are worth.** USB IDs, serial numbers, product names and the
`/dev/serial/by-id` links udev makes from them are what a device reports about
itself. A device built for it can claim any of them: a match identifies, it
does not authenticate. The watcher tells one plug from the next by what the
kernel assigns (the node's device number, the USB bus and device number, new at
every plug), and reads sysfs only below `/sys/devices`, without following a
symlink planted where an attribute should be.

**Bluetooth** does not work through `--device`: the host's kernel driver
(`btusb`) owns the adapter, so its `/dev/bus/usb` node gives the agent nothing
useful, and Bluetooth itself goes through BlueZ on the host's system D-Bus,
which a sandbox never gets. Run the Bluetooth part on the host and let the
agent reach it over the network (`--allow-host-port PORT`). The launcher says
so when a device you pass is a Bluetooth adapter, and for a device your user
cannot open it prints the udev rule with the device's USB IDs.

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
- `tools.jsonl`: every call of the agent's own tools with its decision, when
  rulesets have rules for them ([rulesets](#rulesets)).

The collectors run in the supervisor outside Firejail; the directory is
hidden from the agent. With real DNS (`--dns`) names resolve, so they no
longer appear as denied lookups. Logs are not size-limited. `activity.jsonl` holds the timeline of what the agents did (see
[Agent activity](#agent-activity)).

**Who was refused.** Every record of the connection log, every refused
lookup and every packet the firewall dropped names the process behind it:

- `process`: `pid` as the sandbox's supervisor sees it and `ns_pid` as the
  agent's own tools show it, `comm`, `argv` (at most 32 arguments and 2,000
  characters, redacted like `processes.jsonl`), `exe`, `cwd`, `uid`,
  `parents` (the names of up to four ancestors, for example `bash`,
  `claude`, `tmux: server`) and `start`, the start time that tells the
  process apart from a later one with the same PID.
- `sandbox`: the run, the agent, the backend, the role (`agent`, or
  `mcp-server` for an MCP server's sandbox) and the pod or session, if any.

The supervisor, PID 1 of the sandbox and outside Firejail, finds the process
through the socket. It looks up the socket's inode in `/proc/net/tcp`, `tcp6`,
`udp` and `udp6` and then the process that holds the inode in `/proc/*/fd`.
Nothing the agent sends, such as a header or a variable, counts for this. A
packet the firewall dropped is matched by its destination: a TCP socket stays
in `SYN_SENT` while it retries, and `state` records that. A process chooses
its own name and command line, so both are shown escaped and cut. A lookup
gives `{"unknown": why}` instead of a process when it fails, when it is slow
(a refusal waits 50 ms for it, any other record 250 ms), when the PID changed
owner meanwhile, or when it is one of more than 50 lookups a second. It never
holds a connection.

**What was sent.** A refused connection also records what the client sent:
the server name (`sni`) and `alpn` of a TLS ClientHello, or the
`request_line` and `http_host` of plain HTTP. A refused HTTP request records
its headers, with credentials redacted as always. `--log-debug` (or
`"log_debug": true` in the project file) adds more: the bodies of refused
requests up to the body limit, the ClientHello's TLS versions, cipher count
and extensions, and for the process its whole chain of ancestors with their
command lines and the sockets it holds. Credentials stay redacted in debug
mode too.

The MCP gateway records the calling process only where its relay runs on the
host, as it does for catalog image servers: the kernel names the process at
the other end of the socket (`SO_PEERCRED`). A relay inside a server's own
sandbox sees no process of another sandbox, and which agent sandbox connected
is not known there.

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
calls of the MCP servers started with `sandbox-ai mcp` (from their
`mcp-calls.jsonl`, with how many were not allowed outright). It ends with the
newest refusals: what, why, the process and its command line, the sandbox and
what the client sent.

## Agent activity

`sandbox-ai activity` shows what the agents did in a sandbox as one timeline:
your prompts, their replies, every tool call with its arguments, result and
duration, the shell commands, file changes, connections allowed and refused,
approvals, MCP tool calls, the session's start, pause, resume, attach and end,
and the tokens of each turn. Subagents (Claude's Task/Agent tool, the agents
Codex spawns, OpenCode's task sessions) appear under their own name.

While an agent sandbox with a log directory runs (`--log-dir`, or the default
one the connection proxy keeps unless `--no-proxy`), the launcher collects the
timeline into `activity.jsonl` in that directory. It reads the agents' own
transcripts in the sandbox HOME and sandbox-ai's logs:

| Source | Read from | Format checked against |
|---|---|---|
| Claude Code | `~/.claude/projects/*/SESSION.jsonl`; subagents in `SESSION/subagents/agent-*.jsonl` | 2.1.317 |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, `~/.codex/archived_sessions/` (legacy and paginated history) | rust-v0.77.0 to rust-v0.166.0 |
| OpenCode | `~/.local/share/opencode/opencode*.db` (SQLite, from v1.2.0); `storage/session\|message\|part/` (JSON, before) | v1.1.48, v1.19.5 |
| sandbox-ai | `files.jsonl`, `egress-denied.jsonl`, the run's `connections-*.jsonl`, the run's approvals | |

It polls every two seconds (an OpenCode database at most every ten) and reads
everything once more when the agent has ended, before a throwaway HOME is
deleted. Only what was written after the start counts: older conversations in
a kept HOME (kept starts, `--state-dir`, sessions) are not repeated.

Everything in the HOME is written by the agent. Files are opened without
following a symbolic link anywhere on the way, at most 16 MiB per read; damaged
lines are skipped; an OpenCode database is copied (up to 512 MiB, never
through a link) into a private directory and read there with SQLite's
defensive settings. Prompts, replies and commands are kept up to 2000
characters, other values up to 500; file contents and tool outputs are not
copied (only their size; a failed call keeps the first 300 characters of its
error). Tokens, keys and passwords are redacted with the connection log's
patterns (credential headers, token formats, passwords in URLs,
`--token`-style options), assignments such as `API_KEY=…`, private keys and
the values of `--env` and `--secret-env`; environment variables are kept by
name only. `activity.jsonl` has mode `0600` and stops at 256 MiB.

```bash
sandbox-ai activity                        # every run of the current project
sandbox-ai activity claude-3f9a1c2e        # one run, running or ended
sandbox-ai activity --session dev -f       # a session, live
sandbox-ai activity -k command,file -v     # only commands and file changes, with details
sandbox-ai activity --since 2h --json      # JSON lines for scripts
sandbox-ai activity --runs                 # the runs a log directory holds
sandbox-ai activity --home ~/.local/state/sandbox-ai/sessions/dev/home  # read a HOME directly
```

```
2026-11-04
  10:00:00  prompt    claude          fix the tests
  10:00:00  network   sandbox         allowed: POST https://api.anthropic.com/v1/messages → 200 (claude)
  10:00:01  reply     claude          Running the tests.
  10:00:01  command   claude          pytest -q tests · exit 1 · 2.5 s
  10:00:05  file      claude          modified /work/a.py (Edit)
  10:00:05  mcp       claude          github: get_issue · denied
  10:00:05  tool      claude          subagent Explore: Find the parser
  10:00:06  tool      claude/Explore  Grep def parse in /work
  10:00:07  approval  claude          denied: mcp__github__get_issue
  10:00:09  network   sandbox         denied: 198.51.100.7:22 (SSH to a public Git host) (rule no-ssh, ssh)
  10:00:10  tokens    claude          2 model call(s) to claude-opus-5-5: input_tokens 20, …
```

The agent column says `sandbox` for what sandbox-ai observed itself (files,
network, approvals, the session). Options: `RUN`, `-s/--session NAME`,
`-p/--project PATH`, `-l/--log-dir DIR` or `--home DIR` choose the timeline
(default: every run of the current project); `-f/--follow` goes on with new
events; `--since TIME` (`10m`, `2h`, `3d`, `14:30`, `2026-11-04`, an ISO time),
`-k/--kind KIND[,KIND]` and `-g/--grep TEXT` narrow it; `-n/--tail N` shows
the last N; `-v` adds the details; `-j/--json` prints the records;
`--runs` lists the runs. The desktop app shows the same on its Activity page.

Each line of `activity.jsonl` is one record:

| Field | Meaning |
|---|---|
| `v` | schema version (1) |
| `time` | when it happened, UTC: `2026-11-04T10:00:01.500Z` |
| `run`, `session` | the run (`claude-3f9a1c2e`, `session-NAME`) and the session name or `null` |
| `agent` | the run's agent: `claude`, `codex` or `opencode` |
| `kind` | one of the kinds below |
| `summary` | one line |
| `details` | everything else: `tool`, `arguments`, `status` (`ok`, `error`, `denied`, `running`, …), `exit_code`, `duration_ms`, `path`, `verdict`, `rule`, `process`, `usage` … |
| `source` | `claude`, `codex`, `opencode` (the agent's transcript) or `files`, `egress`, `proxy`, `approvals`, `sandbox-ai` (observed by sandbox-ai) |
| `id` | the event's id; a later record with the same `id` and `"update": true` replaces it (a tool call still open after five seconds is shown as `running`, then again when it ends) |
| `conversation`, `subagent` | the agent's own session id and the subagent, when known |

| Kind | What |
|---|---|
| `prompt` | what you asked (for a subagent: what its agent asked), queued prompts, slash commands |
| `reply` | the agent's answer text |
| `tool` | other tool calls: reading, searching, the web, subagents, plans |
| `command` | shell commands the agent ran with its tool (and `!` commands you typed), with exit code and duration |
| `file` | files created, changed, deleted or moved: by the agents' edit and patch tools, by commands (Claude reports them), and as inotify saw them (`files.jsonl`, with `--log-dir`; no reads; more than five in one folder at once are one event; the agents' own state such as `~/.claude` is left out) |
| `network` | refused lookups and connections, checked and refused pushes, the first request to each host (with the rule and the process when the proxy names them); repeats are counted and shown once a minute |
| `approval` | the sandbox's questions with their answers; tool calls the agent's own permission check refused |
| `mcp` | MCP tool calls (`mcp__server__tool`, Codex's `mcp__server` namespace, OpenCode's `server_tool`) |
| `session` | start (workspaces, backend), pause, resume, attach, detach, stop request, end with exit status; interruptions and model errors |
| `tokens` | the model usage of each turn (Claude: once per message; Codex: `token_count`; OpenCode: per answer, with cost) |

Codex does not store approvals in its rollouts and OpenCode stores only
refusals: their own permission prompts show up as tool calls with status
`denied`. inotify names no process. An agent version that changes its format
may hide some events until sandbox-ai supports it: unknown lines are skipped,
never fatal.

## Telemetry of the agents

Claude Code, Codex and OpenCode can report to their makers: usage statistics,
error reports, feedback and bug reports with your conversation, shared
sessions, update checks. sandbox-ai turns this **off by default** in every
sandbox, for the lead, the agents `--with` and helpers, with each agent's own
switches (checked in Claude Code 2.1.317, Codex 0.166 and OpenCode 1.19.5):

| Agent | `--telemetry off` (the default) | `strict` adds |
|---|---|---|
| Claude Code | `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_BUG_COMMAND`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY`, `DISABLE_AUTOUPDATER` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (also plugin and marketplace downloads, release notes) |
| Codex | in the sandbox's `~/.codex/config.toml`: `analytics.enabled = false`, `feedback.enabled = false`, `otel.metrics_exporter = "none"`, `check_for_update_on_startup = false` | `features.plugins = false` (the plugin sync) |
| OpenCode | `OPENCODE_DISABLE_AUTOUPDATE`; `"share": "disabled"`, `"autoupdate": false` in `OPENCODE_CONFIG_CONTENT` | `OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_DISABLE_LSP_DOWNLOAD` |

OpenCode sends no usage statistics of its own; it exports OpenTelemetry only
to an endpoint you configure.

Every agent also gets the switches of tools it may run: `DO_NOT_TRACK=1`,
`OPENSPEC_TELEMETRY=0`, `HF_HUB_DISABLE_TELEMETRY`, `NEXT_TELEMETRY_DISABLED`,
`DOTNET_CLI_TELEMETRY_OPTOUT`, `GATSBY_TELEMETRY_DISABLED`,
`ASTRO_TELEMETRY_DISABLED`.

```bash
sandbox-ai claude                       # off
sandbox-ai claude --telemetry strict    # nothing but the model's calls
sandbox-ai claude --telemetry on        # the agents' own settings
```

- **Your default** for every start: `"telemetry": "off" | "strict" | "on"` in
  `~/.config/sandbox-ai/defaults.json`; `--telemetry` on the command line wins.
- **Security modes:** `high` refuses `--telemetry on`; `paranoid` makes
  `strict` the least (a default of yours below it is raised, an option below it
  refused).
- **A project file** (`.sandbox-ai.json`) may make it stricter, never turn it
  on: `--telemetry on` there is refused.
- **What you set yourself wins:** a variable you pass with `--env` or
  `--secret-env`, and what a kept HOME's `~/.codex/config.toml` already says
  (the start names each such key). A `config.toml` that cannot be added to
  safely (an inline `analytics = {...}`, keys of a table only as dotted keys) is
  left as it is, with a warning.
- **The proxy refuses it too** where it goes to a host the agent needs anyway
  (the ruleset `telemetry`): Claude Code's event log
  (`api.anthropic.com/api/event_logging/**`) and Codex's analytics
  (`chatgpt.com/backend-api/codex/analytics-events/**`), even if an agent
  ignores its switches; the model's own requests pass. Other telemetry hosts
  (Datadog, Sentry, Statsig) are not among the hosts a sandbox reaches by
  default. A host passed with `--no-inspect` gets no such rule (the proxy cannot
  see its paths). The dry run (`-n`) shows the level, where it came from, and
  every variable and key it sets under `"telemetry"`.

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
sandbox-ai claude --allow-github \
  --allow-http 'api.github.com GET,HEAD /**' \
  --allow-http 'api.github.com POST /repos/me/app/pulls'
```

Rules hold for every name a request could reach: the verified server name, the
`Host` header (one certificate often covers many names, and servers such as
GitHub route by `Host`) and the host's pinned addresses, so another name or the
bare IP is no way around them; a `Host` other than the verified name is refused
where rules apply. They need the proxy, and a host under `--no-inspect`
cannot have them. `--allow-http` is one [ruleset](#rulesets) among others:
rules files add `deny` and `ask`, conditions on the query, headers and JSON
body, and rule IDs in the log.

| Logged as | Meaning |
|---|---|
| `http` | One request and its response (`push` holds the check's verdict) |
| `tls-passthrough` | TLS passed through unopened (`--no-inspect`) |
| `tls`, `tcp` | Other protocols, inside TLS or plain |
| `upgraded` | A WebSocket after the upgrade |
| `denied` | Not allowed by the network options or a rule, or SSH refused; with the process and what the client sent ([logs](#egress-and-file-access-logs)) |
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
- a **request** or an **MCP tool call** that an `ask` rule holds (see
  [Rulesets](#rulesets)); approved for the run at most;
- a **device** a device rule asks about, before it is attached (at the start,
  `sandbox-ai devices add` or a replug; see
  [Hardware](#hardware-devices-and-gpus)); approved for the run at most;
- **opening a URL** in your browser: `xdg-open`, `sensible-browser`,
  `x-www-browser`, `www-browser`, `open` and `$BROWSER` in the sandbox ask the
  host to open http(s) URLs, one question per URL (for example the login page
  of a tool). Nothing opens without your answer; the program in the sandbox
  only learns that you were asked.

Each question names the process behind it, for example `connect to
203.0.113.9:443 (tls), named example.org by the agent (not verified); asked by
curl (pid 7, /usr/bin/curl) in claude-1a2b3c4d` (see
[logs](#egress-and-file-access-logs)).

Every question shows up as a desktop notification (`notify-send`, when
available; its title names the asking sandbox: run, workspace and session),
in the app, and in the commands below. With libnotify 0.7.9 or later the
notification has buttons that answer it: **Allow**, **Allow always**, **Block**
and **Block always** (a URL to open: **Open**, **Ignore**). *Always* is
remembered for the project, both ways: what is blocked always is refused at
every later start without a question, and allowing it always later lifts the
block.

```sh
sandbox-ai approvals list                     # ID  RUN  what is asked
sandbox-ai approvals allow ID --scope run     # once (default), run, always
sandbox-ai approvals deny ID [--scope once|always] # default: not asked again in this run; always: blocked for the project
sandbox-ai approvals history [RUN]            # questions and their answers
sandbox-ai approvals remembered [--json]      # what is allowed/blocked always, per project: name or address, how, when, from which question
sandbox-ai approvals forget PROJECT ENTRY     # forget one (a host name or IP:PORT)
```

A question nobody answers is refused after 60 seconds and, like a deny, not
asked again in that run. `once` allows this one connection (the address is
open for 30 seconds), `run` until the sandbox ends, `always` also every later
start of the same project (for a lookup the name; for a connection the name
when it can be proved, else the address, see below; kept in
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

**What "Allow always" keeps for a connection.** The name in a connection
question (TLS server name or `Host` header) is what the agent sent, so
sandbox-ai remembers the name only when it can prove it; otherwise it
remembers the address. Each question says which it will be:

- `Allow always: api.example.com (verified by its certificate)` or
  `(verified: sandbox-ai pinned it to this address)`: sandbox-ai pinned that
  name to this address itself, or a certificate the proxy checked earlier in
  the run is valid for it.
- `Allow always: api.example.com if its certificate proves it on this
  connection, else 203.0.113.7:443 (address only)`: the approved connection
  decides. The proxy checks the real server's certificate against the
  system's CAs; if it is valid for the name, the name is remembered, and if
  not (a certificate for another name, a wildcard that does not cover the
  name, no connection within 15 seconds), the address is.
- `Allow always: 203.0.113.7:443 (address only: …)`: nothing can prove a
  name. Examples: no name was sent, or the name is an address; the
  connection is not TLS or not to port 443; the address is not public; the
  host is under `--no-inspect`.

The certificate is never checked before you answer, because that would send
the server name the agent chose to the address it chose without your
approval. A remembered name is opened at later starts like `--allow-host`
(HTTPS to the addresses it resolves to then), so it keeps working when the
service moves to new addresses. Only public addresses, or addresses inside
the administrator's `allow_within`, are opened. A name that resolves to
anything else (a service moved into the LAN, DNS rebinding) or that no longer
resolves is left out with a note; the start goes on. Names the administrator's
policy does not list in `allow_hosts` are remembered as the address.
`sandbox-ai approvals remembered` shows for each entry whether it is a name or
an address, how a name was verified, when it was added, and from which
question.

## Rulesets

The network options decide which hosts the agent reaches. Rulesets decide what
it may do there and which MCP tools it may call. They **allow, ask or deny**
each HTTP request, by host, method, path, query, headers and JSON or form
body, each MCP tool call, by tool and arguments, and each device the agent
gets, by USB IDs, serial, class, subsystem and path. The model for this is
Progent's privilege rules for tool calls. The rules are enforced outside the
agent: by the connection proxy and by the relay in front of each MCP server,
two processes the agent can neither reach nor change, and for devices by the
launcher on the host. Rules for the agent's own tools (Bash, file edits,
WebFetch) go through its hooks: a guard rail, not a boundary (see *The agent's
own tools* below).

Rulesets come from five places. Each one is a JSON file of the same format:

| Ruleset | Controlled by |
|---|---|
| `/etc/sandbox-ai/policy.json`, key `"rules"` | the administrator |
| `~/.config/sandbox-ai/rules.json` (if it exists) | you, at every start |
| `--rules FILE` (repeatable; outside every workspace) | you, for this start |
| `--allow-http 'HOST METHODS PATH'` | you, as before |
| the trusted project file, key `"rules"` | the project |

**Each ruleset decides on its own, and the strictest decision wins** (deny
over ask over allow). No ruleset can widen what another narrows: a project's
`allow` never overrides your `deny`, and your `allow` never answers the
administrator's `ask`. No rule opens the firewall either: a request a rule
allows must still be one the network options let through.

`--allow-github` opens `api.github.com` for every request, so the agent could
also create gists or issues. This file keeps those out and leaves the rest of
the API working:

```json
{
  "version": 1,
  "http": [
    {"id": "no-gists", "host": "api.github.com", "path": "/gists/**", "decision": "deny",
     "reason": "gists can publish workspace data"},
    {"id": "no-issue-writes", "host": "api.github.com", "methods": ["POST", "PATCH", "PUT", "DELETE"],
     "path": "/repos/*/*/issues/**", "decision": "deny"},
    {"id": "releases", "host": "api.github.com", "methods": "POST", "path": "/repos/*/*/releases",
     "decision": "ask"}
  ],
  "mcp": [
    {"id": "reads", "server": "files", "tool": ["read_file", "list_directory"], "decision": "allow"},
    {"id": "writes", "server": "files", "tool": "write_file",
     "args": {"path": {"path_glob": "/work/src/**"}}, "decision": "allow"},
    {"id": "deletes", "server": "files", "tool": "delete_file", "decision": "ask"},
    {"id": "rest", "server": "files", "tool": "*", "decision": "deny", "priority": 1000}
  ]
}
```

```bash
sandbox-ai claude --allow-github --rules ~/.config/sandbox-ai/github.json --mcp files
sandbox-ai claude --rules ~/.config/sandbox-ai/github.json --dry-run   # check a file: lists the rulesets, stops on errors
```

**The format.** A ruleset has `"version": 1`, optionally `"description"`,
`"defaults"`, the lists `"http"`, `"mcp"`, `"devices"` and `"tool"`, `"untrusted"`
(sources of untrusted content, see *the run's taint* below) and `"canary"` (what follows
a read of a [canary file](#canary-files) and a canary token leaving). Every rule has an `id` (up to
64 letters, digits, `.`, `_`, `:` or `-`; it names the rule in logs and
questions) and a `decision`: `allow`, `ask` or `deny`. A rule can also have a
`priority` (default 100, lower comes first), a `reason` (one line, shown to the
agent and in the question) and a `description`.

| HTTP rule | Meaning |
|---|---|
| `host` (required) | A name, `*.DOMAIN` (the names below DOMAIN), an address, or `*` for every host; a string or a list |
| `methods` | `GET`, `["POST", "PUT"]` or `*` (the default) |
| `path` | A pattern or a list (default `/**`): `*` matches within one segment, `**` across segments; `/gists/**` also covers `/gists` |
| `query`, `headers` | `{"NAME": condition}` for query parameters and headers (header names in any case) |
| `body` | A condition on the body: JSON (whatever the declared type), or a form with `application/x-www-form-urlencoded` |

| MCP rule | Meaning |
|---|---|
| `server` | The MCP server's name (as in `--mcp NAME`), a list, or `*` (the default) |
| `tool` (required) | A tool name or a pattern with `*`, or a list |
| `args` | `{"NAME": condition}` for the call's arguments |

| Device rule | Meaning |
|---|---|
| `usb` | `VENDOR:PRODUCT` in hex (`2e8a:000a`) or `VENDOR:*`; a string or a list |
| `serial` | The USB serial number, `*` as a wildcard; a string or a list |
| `class` | A USB class of the device or one of its interfaces: `hid`, `cdc`, `cdc-data`, `video`, `audio`, `mass-storage`, `smart-card`, `wireless`, `printer`, `hub`, `misc`, `vendor` and the others of the USB specification, or two hex digits |
| `subsystem` | The node's kernel subsystem: `tty`, `usb` (raw USB), `video4linux`, `sound`, `drm`, `misc` and so on |
| `node` | A path pattern under `/dev` (`*` within a segment, `**` across), compared with the node, the path given and the links udev made to it (`/dev/serial/by-id/...`); `cdi:nvidia.com/gpu=all` for `--gpu nvidia` with Podman |

A device rule needs at least one of these, and each one given must match.
Device rules judge `--device`, `--gpu` (`/dev/dri` as one node), `sandbox-ai
devices add` and every replug. This ruleset lets the agent have one board and
`/dev/kvm`, asks before raw USB to the board, and refuses everything else:

```json
{
  "version": 1,
  "defaults": {"devices": "deny"},
  "devices": [
    {"id": "pico", "usb": "2e8a:000a", "decision": "allow"},
    {"id": "pico-raw", "usb": "2e8a:000a", "subsystem": "usb", "decision": "ask", "priority": 50,
     "reason": "raw USB to the Pico (picotool, flashing)"},
    {"id": "kvm", "node": "/dev/kvm", "decision": "allow"},
    {"id": "no-cameras", "class": "video", "decision": "deny", "priority": 10}
  ]
}
```

Within a ruleset the rules are tried with the lowest `priority` number first,
`deny` before `ask` before `allow` at equal priority, then in file order. The
first rule that matches decides. If none matches, these hold in order:

- **Allow-lists.** Once a host has an `allow` rule in a ruleset (`*` does not
  count), that ruleset refuses every other request to the host, as
  `--allow-http` always did. In the same way, once a tool has an `allow` rule,
  the ruleset refuses every other call of that tool, and once a
  vendor:product has an `allow` rule, every other device of it (another
  serial, another class). To limit a whole server to its allowed tools, add a
  `deny` for its tool `*` with a high priority number, as above.
- **Defaults.** Otherwise the ruleset's `"defaults"` apply: `{"http": "ask"}`,
  `{"mcp": "deny"}`, `{"devices": "deny"}` or `{"tool": "ask"}`. When none is set, the ruleset has no say and other
  rulesets decide; with no rule anywhere, the request or call is allowed.

**Conditions** are a subset of JSON Schema: `type`, `const`, `enum`,
`pattern`, `minLength`, `maxLength`, `minimum`, `maximum`, `items`,
`properties`, `required`, `additionalProperties: false`, `not`, `anyOf` and
`allOf`. sandbox-ai adds `glob` (`*` stands for any characters), `path_glob`
(absolute paths compared after resolving `.`, `..` and repeated slashes; `*`
and `**` work as in `path`) and `optional`. Three rules differ from JSON
Schema, so that a rule cannot be dodged:

- A `pattern` must match the whole value, not only its start. `.` matches a
  line break only where both readings agree: write `(?s)` when it should.
- A name under `args`, `query`, `headers` or `properties` must be present. A
  left-out argument fails its condition and never skips the rule. Write
  `"optional": true` when it may be left out; on a `deny` rule this also
  denies the call that leaves it out.
- A keyword for another type than the value's (a `pattern` applied to a list)
  makes the result unknown instead of passing.

**What servers could read differently is unknown.** This covers a JSON key
given twice or also in another case, a body that cannot be parsed (not JSON
or a form, compressed other than with gzip or deflate, larger than 1 MiB), a
query with `;` or a repeated name, a method override
(`X-HTTP-Method-Override`, `X-HTTP-Method`, `X-Method-Override`, `_method`), a
path override (`X-Original-URL`, `X-Rewrite-URL`), a path that matches only
when case is ignored or only without a NUL or trailing dots, a relative path
under `path_glob`, and a value longer than 100,000 characters under `pattern`.
An `allow` rule needs a definite match, while `deny` and `ask` rules also take
an unknown one. What cannot be judged is therefore never allowed by a rule.
For a device, unknown is what cannot be read from sysfs: a node sysfs does not
know, a link that leads out of `/sys/devices`, an attribute that is not a
regular file, a serial that is not printable ASCII.

**In the proxy.** HTTP rules hold for every name a request could reach, as
`--allow-http` rules do. That means the verified server name, the `Host`
header, and the host's pinned addresses, so a bare IP is no way around them.
Paths are compared as servers resolve them. A request a rule denies gets a
`403` with the reason, which the agent can read. An `ask` holds the request for
a question (kind `http`: method, URL, rule, and the start of the body,
redacted); *Allow always* counts for the run only. With `--ask off` the request
is refused. When a body condition could decide, the body (up to 1 MiB) is read
before anything reaches the server. Once any ruleset has HTTP rules, every
`http` and `denied` record in the connection log carries `decision`, `rule`
and `ruleset`. The `rule` field holds the rule's `id`; `(unlisted)` for a
request outside a host's allow rules; `(default)`; `(host)` for a `Host`
header other than the verified name; or `null` when no ruleset had a say. HTTP
rules need the proxy, and a host under `--no-inspect` cannot have any: such a
start is refused.

**In the MCP relay (the gateway).** The relay checks every `tools/call` before
the server sees it. It answers a denied call itself, with an error result that
gives the reason, and `tools/list` leaves out the tools whose every call
would be denied. The relay refuses JSON-RPC batches, lines that are not
exactly one JSON message, keys given twice, method names in another case and
tool names with other characters than `A-Z a-z 0-9 _ - . : /`. Messages are
passed on re-encoded, so the server parses what was checked. Every call is
written to `~/.local/state/sandbox-ai/mcp/NAME/mcp-calls.jsonl`, with its
arguments (shortened and redacted), decision, rule, ruleset and answer, then
its result (size, `is_error`, duration). A question about a call appears in
`sandbox-ai approvals` (run `mcp-NAME`) and as a desktop notification; for
an MCP server, `run` means until the server stops. A server's rules are part of
what it is: an agent with other rules for a running server is refused until
`sandbox-ai mcp stop NAME`.

**Untrusted content: the run's taint.** A ruleset can mark sources of
untrusted content (responses of hosts, results of MCP tools) under
`"untrusted"`. A rule with `"when": {"tainted": true}` holds only once such
content has entered the run:

```json
{
  "version": 1,
  "untrusted": [
    {"id": "web", "host": "*"},
    {"id": "fetch", "server": "fetch", "tool": "*"}
  ],
  "http": [
    {"id": "tainted-writes", "host": "*", "methods": ["POST", "PUT", "PATCH", "DELETE"],
     "when": {"tainted": true}, "decision": "ask"}
  ],
  "mcp": [
    {"id": "tainted-files", "server": "files", "tool": ["write_file", "move_file"],
     "when": {"tainted": true}, "decision": "ask"}
  ]
}
```

This is CaMeL's rule at sandbox-ai's layer. sandbox-ai cannot see how data
flows inside the model, so once untrusted content is in the agent's context,
any later action may follow instructions hidden in it. With the file above,
every write to a host other than the agent's own provider asks first, and so
does every push, since a push is a POST. So do the chosen MCP tools.

- **The taint comes first.** The proxy holds the head of a response from such
  a host until the supervisor has recorded the taint, and the MCP relay
  records it before it passes a tool's result on. The agent's next request
  already meets the rules for tainted runs.
- **Sources** take `"host"` (and optionally `"path"`) for responses of hosts,
  or `"tool"` (and optionally `"server"`) for results of MCP tools, or
  `"agent"` and `"tool"` for the agent's own tools (its hooks, see below).
  `"host": "*"` marks every host except the provider. Content that is not
  HTTP, or that passes unopened (`--no-inspect`), counts by its host alone.
- **The agent's own provider** (its model API and login hosts, those of
  `--frontier`, the `--llm` server) is never a source and is never held.
  This holds only under its verified name: the provider's name in a `Host`
  header sent to another server counts for nothing.
- **Visible:**
  - A question such a rule holds says why: "asked because untrusted content
    from news.example entered this run at 10:00:00Z".
  - The connection log gets a `taint` event, and every later record carries
    `tainted`. The MCP calls of the run carry it in `mcp-calls.jsonl` in the
    log directory.
  - `sandbox-ai activity` shows the event, and `sandbox-ai net show` shows
    since when the run is tainted and from where.
- **Only you take it back:** `sandbox-ai net untaint [RUN]`. A new run starts
  untainted.
- **MCP servers per run:** the agent reaches each MCP server through a relay
  of its own sandbox's supervisor. The relay applies these rules, puts their
  questions to this run's approvals and logs this run's calls. The servers'
  own sockets are hidden from the agent.

Limits:

- The taint is coarse by design: every later write asks, however unrelated
  to the content.
- Content written to a file and read in a new run is not seen there.
- Requests already on their way on other connections when the taint is
  recorded are not held.
- Only content from hosts and tools that a ruleset marks is tracked.

**The agent's own tools: its hooks.** Rules under `"tool"` hold for the
agent's own tools before they run: Bash, file edits, WebFetch and the like.

```json
{
  "version": 1,
  "untrusted": [{"id": "search", "agent": "*", "tool": ["WebSearch", "WebFetch"]}],
  "tool": [
    {"id": "downloads", "tool": "Bash", "args": {"command": {"pattern": "(?s).*\\b(curl|wget)\\b.*"}},
     "decision": "ask"},
    {"id": "edits-in-work", "tool": ["Write", "Edit", "MultiEdit", "apply_patch"],
     "args": {"paths": {"not": {"items": {"path_glob": ["/work/**", "/tmp/**"]}}}}, "decision": "deny"},
    {"id": "docs", "tool": "WebFetch", "args": {"url": {"glob": "https://docs.python.org/*"}},
     "decision": "allow"},
    {"id": "tainted-bash", "tool": "Bash", "when": {"tainted": true}, "decision": "ask"}
  ]
}
```

| Tool rule | Meaning |
|---|---|
| `agent` | `claude`, `codex`, `opencode`, a list, or `*` (the default) |
| `tool` (required) | A tool name or a pattern with `*`, or a list |
| `args` | `{"NAME": condition}` for the tool's input |

`"defaults": {"tool": "ask"}` makes every other tool call ask. The allow-list
rule and `when` work as for MCP tools.

- **Tool names** are Claude Code's:
  - `Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `LS`,
    `WebFetch`, `WebSearch` and `Task`.
  - `mcp__SERVER__TOOL` for MCP tools.
  - Codex calls its shell `Bash` and its file edits `apply_patch`.
  - OpenCode's names are mapped to these (`bash` to `Bash`, `write` to
    `Write`, `webfetch` to `WebFetch`, `patch` to `apply_patch`, and the
    argument `filePath` to `file_path`). One rule holds for all three agents.
- **Arguments** are the tool's input as the agent sends it: `command` for
  Bash, `file_path` for Read, Write and Edit, `url` for WebFetch. sandbox-ai
  adds `paths`, the files a call names, made absolute against the agent's
  working directory:
  - `file_path`, `notebook_path` or `path`;
  - every file of a Codex patch;
  - the working directory of a search without `path`.

  The agent cannot set `paths` itself. A string longer than 256 KiB is
  unknown to the rules; it is never cut short.
- **Untrusted tools.** A source with `"agent"` and `"tool"` marks the results
  of the agent's own tool as untrusted. This matters for `WebSearch`: Claude
  Code runs it at Anthropic, so the proxy never sees it. The run is tainted
  as soon as such a call is allowed, before its result arrives.
- **How they are installed.** The launcher writes the hooks as the agent's
  managed configuration and mounts it read-only:
  - Claude Code: `/etc/claude-code/managed-settings.json` with
    `allowManagedHooksOnly`. Only these hooks run, and the agent's own
    `disableAllHooks` does not turn them off.
  - Codex: `/etc/codex/requirements.toml` with
    `allow_managed_hooks_only = true` and the `hooks` feature pinned on. A
    configuration that turns the feature off stops Codex from starting. A
    Codex older than 0.131.0 would ignore these keys without a word, so the
    start is refused (the supervisor runs `codex --version`).
  - OpenCode: a plugin (`tool.execute.before` and `after`), named in
    `OPENCODE_CONFIG_CONTENT`.

  Every tool call runs `python3 -I /sandbox/runtime.py --internal-hook AGENT
  EVENT`. That hook asks the supervisor over a socket the agent can reach but
  cannot replace. The supervisor applies the rules and asks you where a rule
  says `ask` (kind `tool`). *Allow always* counts for the run only, and with
  `--ask off` the call is refused. Every call is written to `tools.jsonl` in
  the log directory, with its input (shortened and redacted), decision, rule
  and ruleset.
- **Fail closed.** Only an answer `allow` lets a call through. A missing or
  malformed answer refuses it, and so does a hook input that cannot be read,
  an unknown agent or event, or no answer within 110 seconds (the agents wait
  120). A refused call reaches the agent as an error that starts with
  `sandbox-ai:` and gives the reason.
- **Only when needed.** The hooks are installed only when a ruleset has tool
  rules, a `tool` default other than `allow`, or an untrusted tool. Hooks from
  plugins and the agent's own settings do not run then (Claude Code and
  Codex); hooks from kits run after the rule hooks once you approved them
  ([hooks in kits](#hooks-in-kits)). With `--binary`, Claude Code and Codex still read the managed
  configuration, but OpenCode gets no plugin.

**A guard rail, not a boundary.** The hooks see the call the agent makes, not
what the program it starts does:

- `bash -c "$(echo Y3VybCB4 | base64 -d)"`, a script written first, or
  `python3 -c` gets past a rule on `curl`.
- A program run from Bash writes files that a `paths` rule refuses to Write
  and Edit.
- A second agent started from Bash, with other settings, is not seen.

Rules on Bash commands catch mistakes and plain prompt injections, not an
agent that works around them. The boundary is where it was: the network
options, the proxy with its HTTP rules, the MCP relay and the mounts.

Limits: the rules see the HTTP/1.1 that the proxy opens. They do not see
WebSocket messages after an upgrade, and they do not see HTTP the proxy does
not recognise as such on ports other than 80 and 443 to hosts without named
rules (a request line with a lowercase method, for example). Data deliberately
encoded (base64, encryption) passes any pattern. MCP rules cover `tools/call`,
not `resources/read`, `prompts/get` or a server's own methods. A server that
runs in its own sandbox shares that sandbox's user with the relay, so a tool
that runs any code (a shell) could replace the relay: deny such tools. Rules
for the agents' built-in tools (Bash, WebFetch) are not part of this yet (see
TODO.md). Device rules go by what a device reports about itself (see
[Hardware](#hardware-devices-and-gpus)); they do not see what the agent does
with a device it was given, and a directory passed whole (`/dev/bus/usb`) is
one device to them.

## Canary files

A canary is a decoy: a file that looks like a credential, planted in the
sandbox for one run. Nothing legitimate needs it, so whoever opens it, and
above all whoever sends its contents somewhere, is worth knowing about (a
prompt injection that tells the agent to collect keys, a malicious package's
install script, a tool that uploads your HOME).

```bash
sandbox-ai claude                                  # credentials and home, the default
sandbox-ai claude --canaries credentials,workspace # also a .env in each workspace
sandbox-ai claude --canaries all                   # credentials, home and workspace
sandbox-ai claude --no-canaries
sandbox-ai canaries list                        # runs with canaries
sandbox-ai canaries list RUN [--tokens] [--json] # their files, tokens and what happened
sandbox-ai canaries log [RUN] [--json]          # every event: by which process, next to the agent's turn
sandbox-ai canaries resume RUN                  # continue a sandbox a canary paused
```

| Category | Files | Default |
|---|---|---|
| `credentials` | `~/.aws/credentials` (a named profile), `~/.config/gcloud/legacy_credentials/ACCOUNT/adc.json`, `~/.ssh/id_ed25519_{prod,deploy,backup}`, `~/.config/gh/hosts.yml`, `~/.netrc`, `~/.git-credentials` | on, in the sandbox HOME |
| `home` | `~/Documents/passwords.txt`, `~/.kube/config` | on, in the sandbox HOME |
| `workspace` | `.env` in each workspace | off: it changes your project directory |

**The tokens.** Each run (each kept HOME) gets its own random tokens in the
shape of real ones, which no service accepts: AWS keys in the form of AWS's
documented example (`AKIA…EXAMPLE`, a secret ending in `EXAMPLEKEY`), a
GitHub token (`ghp_…`) whose checksum is wrong, a GitLab token, a gcloud
refresh token, random passwords, and hosts under `.internal` (a name reserved
for private use) that the sandbox cannot resolve. The SSH key has a private
key's armour but does not decode to a key. The files are not the default ones
tools use on their own (a named AWS profile, `id_ed25519_prod` rather than
`id_ed25519`, a gh login for a host under `.internal`), so `aws`, `ssh`, `gh`
or `git` never send a canary by themselves. Nothing is planted over a file
that exists, a mount (`--mount`, kits) on top of it, or through a link (an
agent may have left one in a kept HOME or a workspace); files are `0600` in
`0700` directories and dated weeks back. The workspace `.env` uses variable
names no framework reads (`PROD_DATABASE_URL`, `DEPLOY_API_KEY`).

**You can tell them from your own files:** they exist only in the sandbox
HOME (a temporary one, or a kept one: `--state-dir`, sessions) and, with
`workspace`, as `.env` in a workspace that had none. `sandbox-ai canaries
list RUN` names each one with its path in the sandbox and on the host;
`--tokens` shows the tokens. A workspace canary is listed in
`.git/info/exclude` while the run lasts (so `git add -A` leaves it out) and
removed with that line when the run ends; a run that was killed is tidied up
by the next start. A throwaway HOME's canaries go with it; a kept HOME keeps
them from run to run with the same tokens (a canary you or the agent changed
is no canary any more and stays as it is; `--no-canaries` removes the
unchanged ones). Their record is kept outside the HOME, in
`~/.local/state/sandbox-ai/canaries/`.

**What is detected.**

| Signal | Seen by | Event |
|---|---|---|
| A canary is opened or read | the supervisor (inotify `IN_OPEN`/`IN_ACCESS` on the file) | `read` |
| A canary is changed, renamed, deleted or replaced | the supervisor (inotify on the file and its directory) | `modify`, `move`, `delete`, `replace` |
| A token in a request's URL, headers or body: plain, URL-encoded (either case), JSON-escaped, hex, base64 or base64url (at any offset), in a gzip or deflate body too, across the pieces of a body | the connection proxy, before the request (or the rest of its body) reaches the server | `send` |
| A token in the name a TLS client asks for (SNI) or in the `Host` header (plain, hex, base32, base64url, across labels) | the proxy | `send` |
| A token in a DNS lookup the sandbox does not allow | the supervisor's DNS stub | `send` |
| A token in pushed commits (files, messages, names), to any remote | the push check | `send` |
| A token in a request to the agent's own model provider | the proxy | `context`: the agent read a canary into its conversation; never blocked as a leak |

The list of canaries and their tokens never enter the part of the sandbox the
agent can read: the supervisor gets the paths and the DNS forms in the run's
control directory, the proxy and the push check get the tokens in the proxy's
directory; Firejail hides both from the agent. Nothing is in its environment
or in `/sandbox/session`.

**What follows** is decided by the [rulesets](#rulesets) (the strictest wins,
the administrator's policy included):

```json
{"version": 1, "canary": {"on_read": "ask", "on_send": "stop"}}
```

| Key | Values | Default |
|---|---|---|
| `on_read` (a `read` of an alerting canary, and `context`) | `report` (notification), `ask` (pause the sandbox and ask: continue or stop), `pause`, `stop` | `report` |
| `on_send` (a token leaving) | `report` (let it pass, notify), `block` (refuse the request, notify), `pause` (refuse and pause), `stop` (refuse and stop the sandbox) | `stop` |

Changes (`modify`, `move`, `delete`, `replace`) are always reported, never
acted on (`git clean -x` deletes a workspace `.env`). Reads of `~/.netrc` and
`~/.config/gh/hosts.yml` are only logged: curl, pip, requests, git and gh open
them by themselves; their tokens leaving still counts. Every event is written
to `canaries.jsonl` in the log directory and to `sandbox-ai canaries list
RUN`; a report appears as a desktop notification (once a minute per canary
and kind), in `sandbox-ai approvals history`, in the app and in the
[activity](#agent-activity) timeline (records of kind `canary`, shown as approvals); a refused
request is a `denied` record with `canary` in the connection log. `ask` is a
question of kind `canary` (`sandbox-ai approvals allow ID` continues, `deny
ID` stops; the notification has buttons); unanswered after 5 minutes the
sandbox stays paused. A pause freezes the Podman pod or every process of the
Bubblewrap sandbox; `sandbox-ai canaries resume RUN` (the run id or the
start's name; `sandbox-ai resume NAME` works too) continues it, `sandbox-ai stop NAME` ends
it.

**The canary log.** `sandbox-ai canaries log [RUN] [--json]` (in the app: Activity, tab Canaries; in the web
interface: Canaries) lists every event with its time, canary, path and action, and who did it: for a token
that left, the process the proxy or the DNS stub found; for a file event, the processes the supervisor found
holding the file open right after inotify reported it (inotify itself names nobody: a reader that opened,
read and closed the file before the lookup is shown as `unknown`, honestly). Each event is linked to the
agent's tool call in the same time window (the one that names the file first), its prompt turn and the
requests to the model provider just before and after it (the one that carried the token, if any), by the
ids of their [activity](#agent-activity) records (or the connection log's file and offset for a request
the timeline only counted). Everything is redacted as the activity log is, and the run's tokens never show.

**Choosing.** `--canaries CATEGORIES` / `--no-canaries` on the command line
or in the project file's `"options"`; the project file's key `"canaries":
["credentials", "workspace"]` (or `false`); `~/.config/sandbox-ai/defaults.json`
`"canaries"` (same form) for every start where neither chose. The
administrator's policy can require them (`"require_canaries": true` for the
default categories, or a list) and forbid `--no-canaries`; its `rules` can
set the `canary` section. Probes and MCP server sandboxes get none.

**Security modes** weigh in like a ruleset (the strictest wins, so a ruleset
can be stricter than the mode, never laxer) and plant at least their
categories; `--dry-run` shows the result under `canaries`, with `decided_by`:

| Mode | Categories | `on_read` | `on_send` |
|---|---|---|---|
| `lax` | `credentials`, `home` | `report` | `report` |
| `low`, `medium` | `credentials`, `home` (the launcher's defaults) | `report` | `stop` |
| `high` | `credentials`, `home`; `--no-canaries` refused | `ask` | `stop` |
| `paranoid` | `credentials`, `home`; `--no-canaries` refused (workspaces are read-only, so no workspace canaries) | `stop` | `stop` |

**Limits.** inotify does not say which process opened a file, and it sees
accesses from the host too (a backup, an indexer, your editor on the
workspace `.env`). A runtime that loads `.env` by itself (Bun, so OpenCode;
dotenv in your app) reads the workspace canary at its start. What the proxy
does not open is not searched: `--no-inspect` hosts (only their TLS name is),
TCP relayed to `--allow-tcp`/`--allow` destinations, SSH, WebSocket messages
after an upgrade, and DNS with `--dns` (it goes to the resolvers directly).
A token cut into pieces sent in separate requests, encrypted, compressed in
another way, or base64 with line breaks, passes; so does a token rewritten
by hand. A push is searched by the push check, which needs `--privacy-check`
other than `off`. The DNS stub runs with a log directory (the default unless
`--no-proxy`).

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
sandbox-ai claude --secret-env OPENROUTER_API_KEY    # the agent gets the variable
sandbox-ai claude --secret-env API_KEY=OPENROUTER_API_KEY --git-token-secret https://github.com=GH_PUSH
```

A sandbox gets a secret only when it is named: `--secret-env NAME[=SECRET]`
(an environment variable for the agent), `--api-key HOST=NAME[=SECRET]` (see
below), `--git-token-secret URL=SECRET` (like `--git-token-env`) or an MCP
server's variable. Values are never printed, not in the dry-run plan and not
in logs (the proxy redacts them in recorded requests as it does every
credential).

**API keys that stay out of the sandbox.** `--api-key HOST=NAME[=SECRET]`
gives the agent the variable NAME with a placeholder
(`sandbox-ai-api-key-placeholder-NAME`), not the key. The connection proxy
puts the secret SECRET (default: NAME) in the placeholder's place on the
wire, and only in a credential header (`Authorization`, `X-API-Key`,
`API-Key`) of an HTTPS request to HOST on port 443, whose certificate it
checked. Anywhere else (another host, plain http, a body, another header) the
placeholder stays a placeholder. The option opens HOST (TCP/443), needs the
proxy and refuses `--no-inspect HOST`. Like the `--llm` key, the key is in
no log; an answer reaches the agent as the server sent it, so give no key to
a server that echoes credentials back.

```bash
sandbox-ai secrets set DEVTO_API_KEY
sandbox-ai claude --api-key dev.to=DEVTO_API_KEY \
    --api-key gql.hashnode.com=HASHNODE_TOKEN    # curl -H "api-key: $DEVTO_API_KEY" https://dev.to/api/articles
```

## Resource limits

Every sandbox runs with limits on memory, CPU and processes (change them while
it runs with `sandbox-ai limits [RUN] --memory 16g --cpus 4 --pids none`, which
sets the sandbox's systemd unit or, where delegated, its cgroup files):

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

**Memory watch.** When the memory limit is full, the kernel ends a process,
and if that is the sandbox's supervisor the whole run ends. So the launcher
watches the sandbox's cgroup from the host while it runs (the sandbox cannot
see it):

- at 85 % it tells you (a desktop notification, and `memory.jsonl` in the log
  directory) and the agent: `/sandbox/session/memory.json` gives used and
  limit, how much the files in its `/tmp` and HOME take, and advice;
- from the start it sets `memory.high` to 92 % of the limit: above it the
  kernel reclaims and slows the sandbox's allocations down instead of letting
  it reach the limit, where its OOM killer would end a process before the next
  look (a burst of a few gigabytes within two seconds); it follows a limit
  changed with `sandbox-ai limits --memory`;
- at 95 %, or when its processes are stalled by memory (`memory.pressure`:
  all of them waiting 10 % of the last 10 seconds), it ends the process with
  the most memory that is neither sandbox-ai's own nor the agent itself
  (typically a test run or a build): SIGTERM, then SIGKILL after 3 seconds;
  the agent sees its command fail and the run goes on;
- after the run it says what it ended, and whether the kernel ended processes
  anyway (then start with `--memory 16g`, or run fewer things at once).

Files in `/tmp` and `/var/tmp` (and a HOME that is not kept) live in memory
(tmpfs) and count against the limit; with a limit, `/tmp` and `/var/tmp` each
hold at most half of it, so a full `/tmp` is an error the agent sees ("No space
left on device"), not the end of the run. Since 0.1.36 the agent's processes
are also the kernel's first choice (`oom_score_adj` 300 above the
supervisor's). The watch needs a local engine on Linux (not `--podman-url`).

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
  "min_mode": "high",
  "allow_within": ["10.0.0.0/8"],
  "allow_hosts": ["pypi.org", "files.pythonhosted.org", "*.corp.example"],
  "mcp_network": "none",
  "subnet": "10.200.0.0/24",
  "subnet_within": ["10.200.0.0/16"],
  "require_vpn": ["corp"],
  "rules": {"version": 1, "http": [{"id": "writes", "host": "*", "methods": ["POST", "PUT", "PATCH", "DELETE"],
                                    "decision": "ask"}]},
  "require_signed_updates": true,
  "require_canaries": ["credentials", "workspace"],
  "forbid_keep": true,
  "message": "see the developer handbook, section 4"
}
```

`forbid` lists options (`--name`, or `--name=value` for one value); they are
refused however they were chosen: on the command line, by a short form, a
trusted project file, a profile or option that implies them (`--nameserver`
and `--egress-all-allow` imply `--dns`), or an MCP server's definition.
`backends` limits the backends. `max_approval_scope` caps answers to
questions (`once`, `run` or `always`); below `always`, nothing is remembered
for a project and remembered approvals are not applied. `min_mode` is the
laxest [security mode](#security-modes) a start may have: a mode below it is
refused, whether chosen with `--mode`, by the project file or in
`defaults.json`, and without a choice `min_mode` itself applies (when it is
stricter than `medium`); MCP server sandboxes keep to it too. `allow_within` is a
network ceiling: every `--allow`, `--allow-tcp`, resolver (`--dns`,
`--nameserver`), host port (as `127.0.0.1`), approval and `sandbox-ai net
allow` must lie inside it, and `--egress-all-allow` is refused; named hosts
(`--allow-host`, presets, `--frontier`) must be listed in `allow_hosts`, apart
from the agent's own provider. Remembered approvals outside it are not applied. Changes during
a run (`sandbox-ai net allow`) obey `forbid` and the ceiling too: the sandbox's
supervisor checks them. `mcp_network: none` (implied by `allow_within`) refuses
MCP image servers with network. `require_vpn` is `true` (every sandbox and MCP
server with network needs `--vpn`, and a failed check always stops it) or a
list of the [VPN](#vpn) profiles allowed; `"forbid": ["--vpn"]` forbids VPNs,
`"forbid": ["--vpn-check=warn"]` only the warn mode.
`subnet` and `subnet6` are the sandbox's own network (`--subnet`,
`--subnet6`) for starts where no option, project file or
`~/.config/sandbox-ai/defaults.json` chose one. `subnet_within` is their
ceiling: for each IP version it lists, the sandbox's own network must lie
inside, whoever chose it; on Bubblewrap that is slirp4netns's network
(`10.0.2.0/24` unless chosen, `fd00::/64` always). It needs `subnet` (and
`subnet6` for IPv6 entries) as the default inside it, because Podman would
otherwise give the sandbox the host's address. With `--network` the Podman
network decides; forbid `--network` to hold the ceiling there. `forbid:
["--subnet"]` keeps users from choosing (a `defaults.json` value counts as a
choice); the policy's own `subnet` still applies.
`rules` is a [ruleset](#rulesets) that every
run applies first; no other ruleset can widen it, and a broken one stops every
start; its `canary` section sets the least that follows a canary's read or a
token leaving. `require_canaries` (`true`: the default categories, or a list
of categories) adds [canary files](#canary-files) to every agent sandbox and
refuses `--no-canaries`. `require_signed_updates`
makes `install.sh` and `sandbox-ai update` accept only releases signed by a
trusted key ([signed updates](#installation-and-updates)). `forbid_keep: true`
makes every start discard its HOME when it ends, as `--no-keep` does: `--keep`
(and a project file's `"keep": true`) is ignored with a note, `--session` (a
kept session by definition) is refused, and kept starts from before cannot be
brought back (`sandbox-ai resume` refuses; `delete` removes them). `--state-dir`
is not covered: sandbox-ai's own logins, plugins and MCP servers use it; add it
to `forbid` to keep users from choosing a HOME of their own. The policy binds
sandbox-ai as installed; it cannot stop a user who runs a modified copy of
their own, so it guards against convenience, project files and mistakes, not
against the machine's own users.

## Web interface

`sandbox-ai web` is the desktop app's counterpart in the browser, with the same
themes: one service on one machine manages it and every host of
[`sandbox-ai hosts`](#other-hosts) over SSH. Starts and sessions (attach in a
terminal in the page, start, pause, resume, stop, delete; while one runs, its
network, workspaces, devices and resource limits, with a writable workspace, a
device or a forced removal confirmed first), approvals (with a
badge for open questions), the doctor, the logs, LLM servers (add, check,
models, remove), each host's defaults (the agents' telemetry, the security
mode, the canaries) and the hosts themselves. The Terminals page lists every
start running on the host, however it began (a terminal, the desktop app, the
page, another machine): one click attaches a start in tmux (one tab per
start); a start in the foreground (`--no-tmux`, a task, no terminal) has no
terminal to attach, and the page says why (`attach_why` in `list --json`).

```bash
sandbox-ai web user add admin      # a password (12+ characters) and a TOTP app (the otpauth URI is shown once)
sandbox-ai web vendor              # xterm.js for the terminals (pinned, checked against its SHA-512)
sandbox-ai web serve               # https://localhost:8443/ (its certificate's fingerprint is printed)
sandbox-ai web serve --listen 0.0.0.0:8443 --hostname box.lan   # reachable from your network
```

- **HTTPS only**, with a certificate of its own made at the first start (or
  `--cert`/`--key`); `127.0.0.1` unless `--listen` says otherwise. To reach it
  from elsewhere, prefer an SSH tunnel (`ssh -L 8443:localhost:8443 box`) or a
  reverse proxy of yours.
- **Login: password and TOTP.** The password is kept as a scrypt hash in
  `~/.config/sandbox-ai/web/users.json` (mode 0600), a TOTP code counts once.
  Failed logins slow down per address, and per address and user (doubling
  waits); many failures for one user slow every address down that fails again,
  but never lock the owner out: the right password and code still get in. Only
  the connection's own address counts (`X-Forwarded-For` is never read).
- **Sessions** live in memory: 30 minutes without use (the page's own polls do
  not count as use, so a forgotten tab logs out), 12 hours at most. The
  cookie is `__Host-`, `HttpOnly`, `Secure`, `SameSite=Strict`. Every change
  needs the session's CSRF token, a same-origin request and JSON; the `Host`
  header must be one of the service's names (no DNS rebinding). The page runs
  with a strict Content Security Policy and shows server text only as text.
  At most 64 connections at once; one without a session is kept 10 seconds.
- **Every action is one sandbox-ai command**, with each argument quoted on its
  own, on this machine or over SSH: the browser cannot send a shell command.
  Terminals are a WebSocket into a pty running `sandbox-ai attach` or a start
  there (`ssh -tt` for a host); closing the tab detaches, the start runs on.
- **An audit log** (`~/.local/state/sandbox-ai/web/audit.jsonl`) records logins,
  failures, changes and terminals, secrets left out.
- **No sandbox reaches it:** the launcher refuses `--allow-host-port` for the
  port of a running web service.

`sandbox-ai defaults show|set|unset KEY VALUE` changes
`~/.config/sandbox-ai/defaults.json` without editing it (each value checked by
the launcher before the file is replaced); the web interface sets other hosts'
defaults through it.

## Desktop app

[sandbox-ai-app](https://github.com/c0decave/sandbox-ai-app) is a graphical
front end (Python, Qt) for everything above: start sandboxes with every
option, see the running ones, follow what their agents do and their
connections, answer approvals,
change their network, manage MCP servers, secrets, logins and images, browse
the files of HOME and the workspaces, and work in terminals of the sandboxes.
It only calls the `sandbox-ai` commands and reads their files; the sandboxes
do not depend on it.

`install.sh` installs it along with sandbox-ai (from a release, or from a
checkout that contains `projects/sandbox-ai-app`) as the `sandbox-ai-app`
command and a desktop menu entry "sandbox-ai". It needs PySide6 (Qt for
Python): when `python3` has none, the installer puts `PySide6-Essentials` from
PyPI into an environment of the app's own (`~/.local/share/sandbox-ai/app-env`),
never into the system's Python.

```bash
./install.sh --from .                   # sandbox-ai and the app
sandbox-ai-app                          # start it (or from the desktop menu); from a terminal it goes into the background
sandbox-ai-app --foreground             # stay in this terminal (--debug: also Qt's debug output)
./install.sh --from . --no-app-terminal # without terminal tabs (they need QtWebEngine and xterm.js)
./install.sh --from . --no-app-deps     # never download PySide6 (use the distribution's python3-pyside6)
./install.sh --from . --no-app          # without the app
./sandbox-ai-app                        # from a checkout, without installing
```

Qt itself needs the system's OpenGL/EGL, xkbcommon and fontconfig libraries
(Debian/Ubuntu: `libegl1 libgl1 libxkbcommon0 libfontconfig1`); the command
says so when they are missing. Its pages are described in
[projects/sandbox-ai-app/README.md](projects/sandbox-ai-app/README.md).

## Option reference

`sandbox-ai codex`, `sandbox-ai claude` and `sandbox-ai opencode` share all options
below; `./sandbox-ai claude --help` prints them too. Arguments after `--` go to
the agent unchanged. Options marked *repeatable* may be given more than once.

Every option has a short form. One-letter forms can be bundled and take their
value directly: `sandbox-ai claude -Dn -w~/src/app` is
`--dangerously-skip-permissions --dry-run --workspace ~/src/app`. Two-letter
forms such as `-gl` (`--allow-gitlab`) are options of their own, not two
bundled letters. Project files and MCP definitions may use short forms too.
The other commands have short forms as well (`-V` version, `-j` JSON, `-y`
yes, `-b` backend; see their `--help`).

### Most important options

| Option | Purpose |
|---|---|
| `-w`, `--workspace DIR` | Project at `/work` (default: current directory) |
| `-mo`, `--mode MODE` | [Security mode](#security-modes): `lax`, `low`, `medium` (default), `high` or `paranoid`; options may only narrow it |
| `-ml`, `--mode-at-least MODE` | A floor: the mode MODE only when it is stricter than the one the project's or your defaults give (a preset's `--mode`, the app's sandbox-ai presets) |
| `-N`, `--name NAME`, `-nkp`, `--no-keep` | Name the start (default `AGENT-PROJECT-xxxx`); discard its HOME at the end instead of keeping it |
| `-s`, `--session NAME` | Kept, named session; manage with `sandbox-ai list\|attach\|stop\|resume` |
| `-D`, `--dangerously-skip-permissions` | Agent without approval prompts; the sandbox stays in force |
| `-g`, `--allow-github`, `-y`, `--allow-pypi`, `-A`, `--allow-host HOST` | Open individual HTTPS destinations |
| `-P`, `--allow-host-port PORT` | Reach a service on the host's loopback, e.g. a local Git server |
| `-ge`, `--git-token-env URL=NAME`, `-gf`, `--git-token-file URL=FILE`, `-I`, `--git-identity` | Push over HTTP(S) from the sandbox, also to github.com |
| `-p`, `--profile development` | Provider, PyPI, GitHub and the OpenAI and Anthropic APIs |
| `-S`, `--state-dir DIR` | A sandbox HOME of your own, kept between runs |
| `-o`, `--offline`, `-E`, `--egress-all-allow` | No network at all / every outbound destination |
| `-gu`, `--gui`, `-gv`, `--gui-vnc DIR` | Virtual display for GUI apps and browsers |
| `-l`, `--log-dir DIR` | Log denied egress and file access |
| `-n`, `--dry-run`, `-pb`, `--probe` | Show the plan / verify the isolation without an agent |

### Backend and image

| Option | Meaning |
|---|---|
| `-b`, `--backend podman\|bwrap` | Sandbox backend (default: `podman`) |
| `-i`, `--image IMAGE` | Prebuilt Podman image (default: `localhost/<agent>-sandbox:latest`); never pulled implicitly |
| `-iv`, `--image-variant NAME` | Your image variant NAME of this agent (`sandbox-ai images new NAME --from AGENT`): `localhost/<agent>-sandbox-NAME:latest`; Podman only, not with `--image` |
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
| `-S`, `--state-dir DIR` | Private persistent sandbox HOME of your own (mode 0700); without it a start keeps its HOME in its own directory (`--no-keep`: deleted after exit) |
| `-e`, `--env NAME` | Pass one host variable to the agent only; values never printed; repeatable |
| `-se`, `--secret-env NAME[=SECRET]` | The agent gets NAME with the value of a stored secret (default: the secret NAME); repeatable ([secrets](#secrets)) |
| `-id`, `--identity NAME` | Work as an [identity](#identities-sandbox-ai-identities---identity): its commit name and address, its Git and publish logins instead of default's |
| `-pl`, `--publish KIND[,KIND]` | Publish with your [publish logins](#publishing-to-registries-and-forges-sandbox-ai-publish-logins---publish) (npm, pypi, testpypi, crates, gitlab, github): opens their hosts, placeholders for the tools, tokens added by the proxy, every publication asked; repeatable |
| `-mm`, `--model MODEL` | The agent's model: Claude Code's alias (`fable`, `opus`, `sonnet`, `haiku`) or full name, Codex's model, OpenCode's `provider/model`; with `--llm` the model is `--llm NAME:MODEL` |
| `-ef`, `--effort LEVEL` | How hard the agent thinks: Claude Code `low`, `medium`, `high`, `xhigh`, `max` (`--effort`); Codex `minimal` … `xhigh` (`model_reasoning_effort`); OpenCode sets it per model in its configuration |
| `-ky`, `--api-key HOST=NAME[=SECRET]` | The agent gets NAME with a placeholder; the proxy puts the stored secret in on the wire, only in a credential header of HTTPS to HOST; opens HOST; repeatable ([secrets](#secrets)) |

See [Authentication and state](#authentication-and-state).

### Kits, project memory and plugins

| Option | Meaning |
|---|---|
| `-k`, `--kit NAME` | Also use the kit `~/.local/share/sandbox-ai/kits/NAME/AGENT`, else sandbox-ai's built-in kit NAME (`egm`, Claude only); repeatable |
| `-K`, `--kit-dir DIR` | Also use the kit in `DIR/AGENT`; repeatable |
| `-nk`, `--no-default-kit` | Leave out the default kit `~/.local/share/sandbox-ai/home/AGENT` |
| `-nm`, `--no-memory` | No project memory (memory file and Claude's auto memory neither loaded nor kept) |
| `-np`, `--no-plugins` | No plugins from the plugin store (Claude) or the kits (OpenCode); a reused Claude HOME loses the plugin settings of earlier runs |

See [Kits, project memory and plugins](#kits-project-memory-and-plugins).

### Network

| Option | Meaning |
|---|---|
| `-p`, `--profile NAME` | `development`, `frontier`, `dns`, `egress-all` or `offline`; repeatable |
| `-f`, `--frontier` | Also allow OpenAI and Anthropic API/auth hosts (TCP/443) |
| `-nd`, `--no-provider-defaults` | Drop the agent's default hosts |
| `-y`, `--allow-pypi`, `-g`, `--allow-github`, `-gl`, `--allow-gitlab`, `-ak`, `--allow-kernel-org`, `-dd`, `--allow-duckduckgo`, `-an`, `--allow-npm` | Host presets (TCP/443) |
| `-A`, `--allow-host HOST` | Pin a host name and allow TCP/443; repeatable |
| `-a`, `--allow IP_OR_CIDR` | All protocols and ports to that destination; repeatable |
| `-t`, `--allow-tcp IP:PORT` | One TCP port on one address (IPv6: `[ADDR]:PORT`); repeatable |
| `-P`, `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in the sandbox reaches the same port on the host's loopback; repeatable |
| `-R`, `--dns` | Real DNS via `1.1.1.1` and `8.8.8.8` (UDP/TCP 53 to them only) |
| `-ns`, `--nameserver IP` | Use this resolver instead; implies `--dns`; repeatable |
| `-E`, `--egress-all-allow` | Allow every outbound destination; implies `--dns`; inbound stays closed |
| `-o`, `--offline` | No outside network, including provider defaults |
| `-sn`, `--subnet CIDR` | The sandbox's own IPv4 network (pasta, slirp4netns): a private `/8` to `/25`; the sandbox `.100`, gateway `.2`, DNS forwarder `.3` |
| `-s6`, `--subnet6 CIDR` | Podman: the sandbox's own IPv6 `/64` in `fc00::/7`; the sandbox `::100`, gateway `::2` |
| `-nw`, `--network NAME`, `-ip`, `--ip IP`, `-i6`, `--ip6 IP` | Podman: existing private network and static addresses |
| `-vp`, `--vpn PROFILE` | Podman: put the VPN profile in front, with a kill switch and checks (see [VPN](#vpn)) |
| `-vc`, `--vpn-check stop\|warn` | What a failed VPN check does (default: `stop`) |
| `-X`, `--no-proxy` | No connection proxy: no connection log, pushes checked only by the hooks |
| `-x`, `--no-inspect HOST` | Pass TLS to HOST (or `*.DOMAIN`) through unopened; repeatable |
| `-ah`, `--allow-http 'HOST METHODS PATH'` | Only matching requests reach HOST (or `*.DOMAIN`); repeatable |
| `-ru`, `--rules FILE` | A [ruleset](#rulesets): allow, ask or deny per HTTP request and per MCP tool call; outside every workspace; repeatable |
| `-Q`, `--ask on\|off` | Ask before what the options do not allow; unanswered after 60 s: refused (default: `on`; [approvals](#approvals-and-browser-requests)) |
| `-cn`, `--canaries CATEGORIES` | [Canary files](#canary-files): `credentials` and `home` (default), `workspace`, comma-separated, or `all` |
| `-nn`, `--no-canaries` | No canary files |
| `-tm`, `--telemetry LEVEL` | [Telemetry of the agents](#telemetry-of-the-agents): `off` (default), `strict`, `on` |

See [Network policy](#network-policy) and [Connection proxy](#connection-proxy).

### Git

| Option | Meaning |
|---|---|
| `-ge`, `--git-token-env URL=NAME` | Token from host variable `NAME` for Git at `URL` (`http(s)://[USER@]HOST[:PORT]`); loopback URLs imply `--allow-host-port`, others `--allow-host`; repeatable |
| `-gf`, `--git-token-file URL=FILE` | Like `--git-token-env`, token read from a private file (mode `0600`, outside workspaces); repeatable |
| `-tp`, `--token-proxy` | Git tokens stay with the connection proxy, which adds them to Git requests to their HTTPS server; the agent never reads them |
| `-gc`, `--git-credentials FILE` | Every login in `FILE` (Git's credential store format, e.g. `~/.git-credentials`; private, outside workspaces), each with the network rules of `--git-token-env` |
| `-I`, `--git-identity` | Commit as the host's global `user.name` and `user.email` |
| `-pc`, `--privacy-check public\|always\|off` | After the run, show what the next push would publish that should stay private: of workspaces with a public remote (default), of every Git workspace, or never; unless `off`, the agent's `pre-commit` hook warns and `pre-push` to a public remote refuses |
| `-pr`, `--privacy-rules DIR` | Your own rules: `DIR/global.rules.toml` and `DIR/PROJECT.rules.toml` (or `.json`); outside workspaces, never shown to the agent; default `~/.config/sandbox-ai/privacy` if it exists |

See [Network policy](#network-policy) (pushing to GitHub and to your own Git server) and
[Privacy check before publishing](#privacy-check-before-publishing).

### Agent behaviour, MCP and LLM servers

| Option | Meaning |
|---|---|
| `-D`, `--dangerously-skip-permissions` | Start the agent without approval prompts |
| `-lm`, `--llm NAME[:MODEL]` | Use the LLM server NAME of `sandbox-ai llm`: opens exactly its address and port, configures the agent, its key added by the proxy (the agent sees a placeholder); no cloud login copied; a server added with `--vpn PROFILE` implies `--vpn PROFILE` |
| `-L`, `--local-config FILE` | OpenCode: local-only configuration, cloud access disabled |
| `-M`, `--mcp NAME` | Connect a local MCP server in its own sandbox; started if needed; repeatable |

See [Permission-free agents](#permission-free-agents),
[LLM servers](#llm-servers-local-and-remote) and [Local MCP servers](#local-mcp-servers).

### Sessions, tmux and GUI

| Option | Meaning |
|---|---|
| `-N`, `--name NAME` | Name of this start (default `AGENT-PROJECT-xxxx`); `sandbox-ai list` shows it, the other commands take it; refused while another start runs or is kept under it |
| `-pn`, `--project-name NAME` | The PROJECT part of the automatic name (default: the main workspace's folder name; project file key `"name"`) |
| `-kp`, `--keep`, `-nkp`, `--no-keep` | Keep the HOME, options and logs when the start ends, so that `sandbox-ai resume NAME` brings it back (the default); `--no-keep` discards the HOME (suits scripts; project file key `"keep"`) |
| `-s`, `--session NAME` | A kept, named session (the older form of `--name`): a stopped one of that name comes back |
| `-T`, `--tmux`, `-nt`, `--no-tmux` | tmux inside the sandbox (window `agent` plus window `shell`) is the default in a terminal; `--tmux` insists, `--no-tmux` switches it off |
| `-gu`, `--gui` | Virtual X display `:99` (Xvfb) |
| `-gs`, `--gui-screen WxHxDEPTH` | Virtual screen size (default: `1920x1080x24`) |
| `-gv`, `--gui-vnc DIR` | Implies `--gui`; VNC on the Unix socket `DIR/vnc.sock` |
| `-gw`, `--gui-wayland` | Expose the host's Wayland socket (weakens isolation) |

See [Every start](#every-start-names-detach-keep-resume), [tmux](#tmux-mode) and
[GUI](#gui-apps-and-browsers).

### Hardware

| Option | Meaning |
|---|---|
| `-d`, `--device PATH\|usb:VID:PID[:SERIAL]` | Hand a host device to the agent: a character device under `/dev`, `/dev/bus/usb`, `/dev/dri`, `/dev/snd` whole, or a USB device by its IDs (re-attached when replugged); repeatable (weakens isolation) |
| `-G`, `--gpu nvidia\|dri\|rocm` | The GPU devices (NVIDIA through CDI with Podman) |
| `-me`, `--memory SIZE`, `-cu`, `--cpus N`, `-pi`, `--pids N` | Resource limits (defaults `8g`, half the CPUs, `4096`; `none` for none; [limits](#resource-limits)) |

See [Hardware](#hardware-devices-and-gpus); a service on the host reached with
`--allow-host-port` is safer.

### Configuration, logs and checks

| Option | Meaning |
|---|---|
| `-c`, `--config FILE` | Project configuration (default: trusted `.sandbox-ai.json` in the main workspace) |
| `-nc`, `--no-config` | Ignore `.sandbox-ai.json` |
| `-l`, `--log-dir DIR` | Write `egress-denied.jsonl`, `files.jsonl`, `connections-*.jsonl` and `activity.jsonl` there (hidden from the agent); default for the connection log and the activity: `~/.local/state/sandbox-ai/logs/PROJECT-HASH` |
| `-ld`, `--log-debug` | Log more of what is refused: request bodies, the TLS ClientHello, the process's whole ancestry and sockets; credentials stay redacted |
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
| `sandbox-ai activity [RUN]` | `[-s NAME\|-p PATH\|-l DIR\|--home DIR] [-f] [--since TIME] [-k KIND[,KIND]] [-g TEXT] [-n N] [-v] [-j] [--runs]`: the agents' timeline (see [Agent activity](#agent-activity)) |
| `sandbox-ai privacy-check [PATH ...]` | `[--rules-dir DIR] [--project NAME] [--all] [--json]`: what the next push of each repository would publish that should stay private; exit 1 for blocking findings, 2 when it cannot check |
| `sandbox-ai net list\|show\|allow\|revoke [RUN] [--allow-host H] [--allow-tcp IP:PORT] [--allow NET] [--allow-PRESET] [--dns] [--nameserver IP] [--allow-host-port PORT] [--git-token-env\|--git-token-file\|--git-token-secret URL=REF]`, `allow ... --keep` | Change what a running sandbox may reach; `--keep` also writes it into the project's `.sandbox-ai.json` (see [Network policy](#changing-the-network-of-a-running-sandbox)) |
| `sandbox-ai net stop RUN [--wait SECONDS]` | The older form of `sandbox-ai stop NAME` for a run: SIGTERM to its checked launcher, which cleans up |
| `sandbox-ai net untaint [RUN]` | Take back the run's taint: the rules for tainted runs no longer hold, until untrusted content enters it again (see [Rulesets](#rulesets)) |
| `sandbox-ai devices list\|add\|remove [RUN] PATH\|usb:VID:PID[:SERIAL]`, `sandbox-ai limits [RUN] [--memory SIZE] [--cpus N] [--pids N]` | Hand devices to a running sandbox, take them away; change its limits |
| `sandbox-ai workspace list [RUN] [--json]`, `add [RUN] DIR [--ro]`, `remove [RUN] DIR\|NAME [--force]` | Mount a host directory into a running sandbox at `/workspaces/<name>` (the checks of `--workspace`; kept for a session's next start), take it away again |
| `sandbox-ai approvals list\|history [RUN]`, `allow ID [--scope once\|run\|always]`, `deny ID`, `remembered`, `forget PROJECT ENTRY` | Answer the questions of running sandboxes; what was allowed always, per project, and forget one |
| `sandbox-ai canaries list [RUN] [--tokens] [--json]`, `canaries log [RUN] [--json]`, `canaries resume RUN` | The [canary files](#canary-files) of the runs and what happened to them; every event with its process, tool call and model requests; continue a sandbox a canary paused |
| `sandbox-ai secrets list\|set NAME [--from-env VAR]\|delete NAME\|backend` | Secrets in the system keyring, else private files |
| `sandbox-ai git-logins add URL --secret NAME [--user USER]`, `list [--json]`, `remove URL`, `check [URL]` | Your Git servers' logins, added by the connection proxy (see [Git logins](#git-logins-in-the-proxy-sandbox-ai-git-logins)) |
| `sandbox-ai publish-logins add KIND --secret NAME [--url URL]`, `list [--json]`, `remove KIND`, `check [KIND]`, `kinds` | Logins for `--publish` (see [Publishing](#publishing-to-registries-and-forges-sandbox-ai-publish-logins---publish)) |
| `sandbox-ai identities add\|set NAME [--name NAME] [--email ADDRESS]`, `login NAME URL --secret SECRET`, `publish NAME KIND --secret SECRET`, `list\|show\|check\|remove …` | Identities for `--identity` (see [Identities](#identities-sandbox-ai-identities---identity)) |
| `sandbox-ai presets list\|show NAME\|check NAME [--value KEY=VALUE]... [--json]` | Presets for `--preset` (see [Presets](#presets)) |
| `sandbox-ai demo-device start\|serve\|status\|stop` | The emulated serial device for practice (see [Presets](#presets)) |
| `sandbox-ai team start NAME --agents A,B[,C] [-w DIR] [-- options]`, `list\|show\|post\|stop\|delete …` | A team of agents (see [A team of agents](#a-team-of-agents)) |
| `sandbox-ai hosts add NAME USER@HOST [--port PORT] [--identity FILE]`, `list\|remove\|test NAME`, `run NAME -- ARGS...` | Other machines managed over SSH (see [Other hosts](#other-hosts)) |
| `sandbox-ai web user\|vendor\|serve\|cert …`, `sandbox-ai defaults show\|set\|unset …` | The web interface and your defaults (see [Web interface](#web-interface)) |
| `sandbox-ai completion bash\|zsh\|fish\|install` | Tab completion scripts, or set them up |
| `sandbox-ai vpn add NAME --wireguard FILE\|--openvpn FILE\|--mullvad\|--tailscale\|--tor [...]`, `vpn list\|show NAME\|remove NAME\|check NAME [--json]` | VPN profiles for `--vpn` (see [VPN](#vpn)) |
| `sandbox-ai images [--json]`, `sandbox-ai images packages AGENT\|VARIANT [--json]` | Agent images and variants, current with their extensions or not, and their packages (read offline from the image) |
| `sandbox-ai images extend\|check\|recipe\|new\|remove\|history …` | Extensions, ready-made recipes (`media`, `tor`) and variants (see [Extensions and variants](#extensions-and-variants)) |
| `sandbox-ai build` | `[--podman-url URL] [--pin VERSION] [--uv VERSION] [--node IMAGE] [--extension-only] AGENT\|all\|VARIANT [podman build options]`; newest releases unless pinned; then the extension layers. `sandbox-ai build [--podman-url URL] team\|vpn` builds the team image or the VPN container |
| `sandbox-ai list [--all] [--json]`, `list --history NAME` | Every start: what runs or is paused, the kept starts, MCP servers; the history of a name |
| `sandbox-ai attach\|pause\|resume\|stop NAME` | Attach (detach with `Ctrl-b d` or by closing the terminal), freeze, thaw or bring a stopped kept start back, stop |
| `sandbox-ai resume NAME --review-home [--json]` | What changed in the kept HOME's start files since its last start; starts nothing |
| `sandbox-ai rename OLD NEW`, `sandbox-ai delete NAME [--yes]` | Another name (the run id stays); remove a stopped start and its HOME |
| `sandbox-ai gc [--older-than DAYS] [--dry-run] [--json]` | Remove leftovers, and kept starts unused for DAYS days |
| `sandbox-ai session list\|attach\|pause\|resume\|stop\|delete` | The older form (`sandbox-session`, deprecated) |
| `sandbox-ai mcp list` | Defined MCP servers and their status |
| `sandbox-ai mcp catalog`, `sandbox-ai mcp add NAME [-m TARGET=HOST[:ro\|rw]] [-e VAR] [-n none\|full] [-M SIZE] [-p N] [-v VPN]` | Servers of the MCP catalogs; add one as a container server (see [Local MCP servers](#servers-from-an-mcp-catalog)) |
| `sandbox-ai mcp start NAME [--backend podman\|bwrap] [--vpn PROFILE] [--for START [--restart]]`, `sandbox-ai mcp stop NAME` | Start or stop an MCP server sandbox (`--for`: for that start, attached to it) |
| `sandbox-ai mcp attach NAME START [--restart] [--json]` | Connect an MCP server to a kept agent start (a running one: restarted with it) |
| `sandbox-ai wizard [new]` | Interactive project setup; offers to edit an existing configuration |
| `sandbox-ai wizard edit [DIR\|FILE]` | Change the existing configuration of a project (menu, diff before writing) |
| `sandbox-ai login AGENT [launcher options] [-- login arguments]`, `--status`, `AGENT --logout` | Login store for sandboxes |
| `sandbox-ai plugins [claude [--update] [launcher options] [-- claude plugin arguments]] [--status]` | Plugin store for Claude sandboxes |
| `sandbox-ai kits [list [--json] [--agent A]]`, `show NAME [--json]` | Every kit and what it brings per agent; one kit (see [Managing kits](#managing-kits-sandbox-ai-kits)) |
| `sandbox-ai kits new NAME [--agent A]...`, `delete NAME --yes` | Create or delete a kit of yours |
| `sandbox-ai kits add-skill NAME PATH [--agent A]... [--as SKILL]`, `new-skill NAME SKILL [--agent A]... [--description TEXT]`, `remove-skill NAME SKILL [--agent A]...` | Copy a skill folder into a kit (a snapshot, no symlinks followed), write one from a template, remove one |
| `sandbox-ai kits add-plugin NAME PLUGIN [--agent claude\|opencode] [--source SOURCE]`, `remove-plugin NAME PLUGIN [--agent A]`, `add-marketplace NAME MARKETPLACE SOURCE` | Declare plugins and marketplaces in a kit's `marketplaces.json` (SOURCE: `OWNER/REPO`, `https://...`, `sandbox-ai`) |
| `sandbox-ai kits add-hook NAME FILE [--agent A]... [--event E] [--matcher M] [--timeout S] [--as FILE]`, `remove-hook NAME FILE [--agent A]...` | Add a hook to a kit (a script, a `hooks.json`, an OpenCode plugin) or remove one ([hooks in kits](#hooks-in-kits)) |
| `sandbox-ai kits trust-hooks KIT [--agent A]... [--yes\|--revoke]` | Review a kit's hooks (every hook and file shown) and approve exactly that content, or take the approval back |
| `sandbox-ai wizard trust FILE [--yes]`, `sandbox-ai wizard forget FILE` | Trust a project configuration, or remove it from the trusted list |
| `sandbox-ai llm add NAME --kind KIND --url URL [...]`, `list`, `show NAME`, `remove NAME`, `models NAME`, `check NAME [--agent AGENT]` | LLM servers, local or remote, for `--llm` (see [LLM servers](#llm-servers-local-and-remote)) |
| `sandbox-ai wizard models --kind ollama\|lmstudio\|llamacpp\|vllm --host HOST [--port PORT] [--sort name\|size\|loaded]`, `sandbox-ai wizard models --llm NAME` | List the models of a local LLM server, or of a registered one |

## Testing

```bash
# Unit, contract, matrix and end-to-end tests (no sandbox started)
python3 -m pytest tests/test_agent_profiles.py tests/test_agent_sandbox.py \
  tests/test_agent_sandbox_{contracts,matrix,edges}.py tests/test_sessions.py tests/test_starts.py \
  tests/test_home_review.py tests/test_feature_matrix.py tests/test_wizard.py tests/test_modes.py \
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
`sandbox-ai wizard` script against a local Ollama-compatible HTTP server and
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

`tests/test_hosts_ssh.py` drives `sandbox-ai hosts` end to end: the real
OpenSSH client against an SSH server on 127.0.0.1 (asyncssh's, so no sshd is
needed: `pip install asyncssh`; skipped without it). It checks public-key
login with BatchMode, arguments with quotes, `$(...)` and globs through a real
remote shell, exit status, stderr, standard input and large output, a terminal
for attach (`ssh -tt`), the host key learnt once and a changed key refused,
an unknown key or user refused at once, nobody listening, a host's own
command, and the `sandbox-ai hosts` command line.

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
| `Project configuration … is not trusted` | Review the file, then `./sandbox-ai wizard trust FILE`, or start with `--no-config`. |
| LLM server unreachable or refused | `sandbox-ai llm check NAME --agent AGENT` (address, TLS, key, model, API); without `sandbox-ai llm`: server bound to the LAN, host LAN IP used, `--allow-tcp IP:PORT` matches. |
| `Unknown MCP server` / server fails to start | `./sandbox-ai mcp list`; read `~/.local/state/sandbox-ai/mcp/NAME/supervisor.log`. |
| `-T`, `--tmux needs an interactive terminal` | Drop `--tmux` (tmux is the default in a terminal anyway) or use `--session` for background runs. |
| `The trusted project configuration … is missing` | Restore the file, start with `--no-config`, or `./sandbox-ai wizard forget FILE`. |
| `… is a symlink; refusing` | Project files and sandbox credentials must be regular files; replace the link. |
| `inotify watch limit reached` | Raise `fs.inotify.max_user_watches` or watch fewer directories. |
| A build fails with `sd-bus call: Permission denied` | No systemd user session (an LXC container, a login without systemd): since 0.1.30 `sandbox-ai build` uses `--cgroup-manager=cgroupfs` then by itself. For limits and the systemd cgroup manager: `sudo loginctl enable-linger $USER` and log in again (a container needs systemd as its init); `sandbox-ai doctor` shows the session. |
| `pasta failed with exit code 1: Failed to open() /dev/net/tun` (exit status 127) | pasta needs `/dev/net/tun`; an LXC or Proxmox container has none unless its host passes it in: `lxc.cgroup2.devices.allow: c 10:200 rwm` and `lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file` in `/etc/pve/lxc/ID.conf`, then restart the container. On a host: `sudo modprobe tun`. `sandbox-ai doctor` checks it with pasta. |
| Codex nested UID-map error | Another explicit Codex sandbox mode re-enables its native sandbox. |
| In a Proxmox or LXC container, anything | [PROXMOX_LXC.md](PROXMOX_LXC.md): what the host and a container template need, and the problems of a first setup. |
| `Podman failed (exit status 125)`, `newuidmap: write to uid_map failed: Operation not permitted` (an unprivileged LXC or Proxmox container) | The delegated IDs in `/etc/subuid` lie outside the container's own (usually only 0-65535; `cat /proc/self/uid_map`). On the host: `root:100000:1065536` in `/etc/subuid` and `/etc/subgid`, `lxc.idmap: u 0 100000 1065536` and `lxc.idmap: g 0 100000 1065536` in `/etc/pve/lxc/ID.conf`, `features: nesting=1,keyctl=1,fuse=1`; restart the container, then `podman system migrate`. Images pulled or built before still carry the old IDs: a build then fails with `Permission denied` (apt on `/var/cache/apt/archives/partial`) until `podman system reset` (it removes this user's images and containers) and a new `sandbox-ai build`. If the reset itself fails with `unlinkat ... permission denied` (files of IDs that are no longer yours), root removes the storage: `sudo rm -rf ~/.local/share/containers/storage`, then `podman system migrate`. The host also loads `nft_chain_nat` and `nft_redir` for the proxy. `sandbox-ai doctor` checks the ranges. A smaller range that fits inside the container's own IDs (e.g. `user:10000:50000`) is no way out: Podman starts, but every image build fails in apt (`setgroups 65534 failed - Invalid argument`), which needs the ID 65534; only more IDs for the container help. |
| `newuidmap`/`newgidmap` fails | Helpers, delegated ranges, host policy. |
| Missing image | `./sandbox-ai build AGENT` or `--image`. |
| Copy target exists | Omit repeated copy options with a reused `--state-dir`. |

## Limits and remaining work

1. **P1 — State separation:** `/`, HOME, the temporary directory,
   `XDG_RUNTIME_DIR` and the launcher's own directory are refused as
   workspaces, but one `--state-dir` used by two runs at once is not locked.
2. **P1 — macOS:** the sandboxes run in a rootless `podman machine`. This is
   complete in code for starts in the foreground but not yet tested on a real
   Mac. `--backend bwrap`, `--device`, `--gpu dri|rocm` and `--gui-wayland`
   are refused there. A start runs without tmux unless `--tmux` asks for it;
   attaching to a start, `sandbox-ai net allow|revoke` and `sandbox-ai limits`
   during a run, `--allow-host-port` and `--llm` with a server on the Mac's own
   loopback, `xdg-open` in the sandbox, the Keychain and MCP servers from images
   are not ported yet.
3. **P2 — Host coverage:** the `e2e` workflow runs the live tests on Ubuntu
   24.04 with both backends; other distributions and kernels, and hardware
   (devices, GPUs, Wayland), are covered by unit and contract tests only.
4. **P2 — Operational bounds:** resource limits need a systemd user session;
   without one sandboxes start unlimited. `files.jsonl` and
   `egress-denied.jsonl` are not size-limited (the connection logs and
   `processes.jsonl` are).
5. **P2 — Claude lifecycle:** the live login sync is tested against a fake
   OAuth endpoint and a client that refreshes like Claude Code 2.1.317, not yet
   over a long session against the real service; account binding of
   written-back tokens is not verified.
6. **P3 — Reproducible builds:** builds take the newest releases by design;
   `--pin`, `--uv` and `--node` fix agent, uv and base image, apt packages are
   not pinned.

Passing functional tests is not a kernel-escape audit or a guarantee for every
host configuration. Firejail runs with `--noprofile` and the launcher's own
options; personal Firejail profiles are not loaded.
