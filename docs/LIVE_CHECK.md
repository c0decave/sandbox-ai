# The live check: what the development sandbox cannot run, on your machine

sandbox-ai is developed inside a sandbox that has no Podman and no user namespaces, so the parts that only show
themselves in a real sandbox were proven there with stand-ins: Firejail's blacklist was read, not run; the firewall
opening a login's server was a line in a list; a Git login given to a running sandbox reached a fake container. One
command on your own Linux machine runs all of them for real and writes one report - and on your Mac, the checks only
a Mac can run ([On a Mac](#on-a-mac)):

```bash
scripts/live_check.sh
```

Run it in a checkout of sandbox-ai, as yourself. It needs about 15-30 minutes with the images built (`--build`
builds the missing ones first). The report is `live-check/REPORT-DATE.md` (and `.json`, the same as data) in the
checkout: when the checkout is the one your sandbox works on, the agent reads it there; otherwise copy both files
into the project's `live-check/` folder. The exit status is 0 whatever the checks found (2 only when it refused to
run or an option was wrong): the report says passed, failed or skipped for each check.

## Before it starts

It checks, and says what is missing and how to fix it:

| Prerequisite | Needed for | When it is missing |
|---|---|---|
| Linux | every check but the macOS ones | on a Mac the Linux checks are skipped, the report says so ([On a Mac](#on-a-mac)) |
| User namespaces (`user.max_user_namespaces`, `kernel.unprivileged_userns_clone`, `unshare --user`) | Podman and Bubblewrap | the `sysctl` to run; Ubuntu's AppArmor restriction is noted (Podman has its profile) |
| `/etc/subuid` and `/etc/subgid` ranges for you | rootless Podman | the `usermod --add-subuids ... --add-subgids ...` to run |
| Podman, rootless (`podman info`) | most checks | install `podman` |
| Bubblewrap (`bwrap --unshare-user` works) | the checks again with the second backend | optional: without it they run with Podman only |
| The images `localhost/codex-sandbox`, `claude-sandbox`, `opencode-sandbox` | codex: most checks; claude: kits-attach, plugins-attach, local-llm; opencode: local-llm | `sandbox-ai build NAME`, or `--build` |
| git | git-login-give (its Git server on this host) | install git |
| pytest | running the checks | taken from your Python, else installed once from PyPI with hashes checked (`packaging/requirements-ci.txt`) into `~/.cache/sandbox-ai/live-check/venv` |

## What it never does

- **No root, no sudo.** It refuses to run as root, and inside a container or a sandbox.
- **Your sandbox-ai stays untouched.** Every check runs with a HOME, configuration, data, state, cache and runtime
  folder of its own in a temporary folder (`sai-lc-*` in the system's temporary folder; `--work-dir`), its own
  throwaway workspaces and its own sessions, stopped and deleted at its end. Your sessions, logins, kits, secrets,
  projects and defaults are never read or changed. The only things read from your configuration are, when you ask
  for them, the one host of `--host` (its entry and sandbox-ai's `known_hosts`) and the one LLM server of `--llm`
  (its address and kind - an endpoint with a key, a header secret or a VPN profile is refused rather than copied).
- **Podman alone sees your HOME.** A small wrapper first on the checks' PATH starts your `podman` with your own
  HOME and XDG folders, so the images you built are used; the checks only make and remove their own containers.
- **No secret store of yours.** The checks' secrets are plain files of their own (`SANDBOX_AI_SECRETS=file`), never
  the system keyring or the Keychain; the made-up tokens they use are generated per run and redacted.
- **Nothing leaves your machine but what a check needs, said before it starts** (`--list` says it per check): one
  HTTPS request to codeberg.org (firewall-login-hosts), SSH to your host (remote-mcp, when asked), prompts to your
  LLM server (local-llm, when asked), and with `--build` the images' packages.
- **The report has no secret and no home path:** your HOME becomes `~`, any other `/home/NAME` `/home/<user>`, the
  checks' folder `<tmp>`; tokens, keys, passwords, `Authorization` headers, a URL's password and private key blocks
  become `<secret>`, as do the values of your environment's variables named like a secret.
- **Cleanup:** sessions a check left behind (a timeout) are stopped and deleted, the temporary folder is removed
  (through `podman unshare` for what the containers' users wrote; `--keep-temp` keeps it, with the full logs).

## The checks

`scripts/live_check.sh --list` prints them with what each proves, needs and sends. The live tests are
`tests/test_live_gaps.py` (and the release gates in `tests/test_agent_sandbox_live.py`); each check runs with every
backend that works here (`--backend`).

| Check | What it proves | Needs | Where the gap was named |
|---|---|---|---|
| `release-gates` | `pytest --agent-live tests/test_agent_sandbox_live.py -k "isolation or strict_mode_starts or relays"`: sandbox-ai's system call filters beside Firejail's, nosuid/nodev mounts, Firejail's sealed folder and the manifest; `--mode high` starts with every part; the terminal relays cannot be replaced from inside; `--untrusted` confines a command | a backend, codex image | security reviews A and B (must pass before a release) |
| `firejail-hides-control` | The agent and `sandbox-ai shell` cannot list `/sandbox/proxy` (the proxy's logins and keys) or `/sandbox/control`, nor read `proxy.json`; `/sandbox/session` is readable | a backend, codex image | credentials-verify A/B |
| `firewall-login-hosts` | `--git-token-env https://codeberg.org=VAR` opens codeberg.org and no other host (gitlab.com refused), with the connection proxy and with the firewall alone (`--no-proxy`) | a backend, codex image; network: codeberg.org | credentials-verify B |
| `git-login-give` | `git-logins give RUN URL --env VAR`: a running sandbox's `git push` to a Git server on this host that wants a login fails before, works after, fails after `revoke`; no listing shows the token | Podman, codex image, git | credentials-verify A/B |
| `hide-workspace-add` | `--hide` applies to a folder given with `sandbox-ai workspace add` while the sandbox runs: the matches empty and read-only, the rest readable, the host's files unchanged | a backend, codex image | Knostic gaps (5) |
| `untrusted-home` | `--untrusted` (Landlock) under the real kernel: in a sandbox with a HOME of over 10000 entries and a credential folder reached through a link (neither readable, it still runs, in time); on this host with your real HOME (`--check`, and a confined command opens none of the credential and agent folders there - only whether each opens is printed, never a name inside or a byte) | a backend, codex image | 0.1.99 "`--untrusted` with a crowded or linked HOME" |
| `kits-attach` | `kits attach --skill`/`--command` into a running Claude Code sandbox: there at once, read-only, gone after `kits detach` | a backend, claude image | runtime kits |
| `plugins-attach` | `plugins attach RUN sandcastle@sandbox-ai` (the built-in marketplace, no network): installed into the plugin store, enabled in the running sandbox; `plugins detach` turns it off | a backend, claude image | runtime kits |
| `bridge-live` | `sandbox-ai bridge` between two real sessions - one Podman, one Bubblewrap when both work here: message, ask (answered by the other side), `files:ro:src` and `files:rw:out` (the write asked, its diff shown by `sandbox-ai approvals show` and allowed with `approvals allow`), watch (activity, changes, terminal) with the observer's alert and hint, each call made by the session's agent side through its real relay in the sandbox; revoke ends it for both. Then the app's New bridge… between the same two sessions, offscreen: the real window, dialog, dry run and question (`tests/live_bridge_app_driver.py`; skipped, saying so, without a Python with PySide6 - `SANDBOX_AI_APP_PYTHON`, as `sandbox-ai-app` takes it, is kept for the checks) | a backend, codex image | the user, 2026-11-21/22: New bridge did nothing; bridges checked in depth |
| `devices-scan` | `devices scan` on your real hardware: valid JSON, every listed node exists, every refusal has a reason | Linux | Board devices |
| `devices-live` | A device added to a running sandbox appears there and in `devices list`, and is gone after `devices remove`; the device is `--device PATH` or the first serial port, camera, render node or `/dev/kvm` the scan offers | a backend, codex image, a device | Board devices |
| `update-live` | Signed updates from the real GitHub: the newest release's `SHA256SUMS` is signed by a key of this checkout's `sandbox/release-signers` and lists every kind's file (the bundle's SHA-256 checked); `sandbox-ai update --check` of this checkout finds it and offers it only when it is newer (never a downgrade); a copy one version older is offered it; nothing is installed | network: api.github.com (read-only, no token), github.com's release downloads (about 16 MB) | signed updates (user 2026-11-22) |
| `remote-mcp` (optional) | A real host of `sandbox-ai hosts`: `hosts test`, its MCP servers listed over SSH, its demo server started and stopped there (only when none of it runs) | `--host NAME` | remote MCP |
| `local-llm` (optional) | One smoke run - a file written with the agent's own tool, a reply - of the three best models of the research (`qwen3.8-flash-next`, `ornith-1.5-35b-a3b-mlx`, `ornith-1.5-122b-mlx`) x Claude Code, OpenCode and Codex, through the real launcher with `--llm` and nothing else open; a model the server does not offer is skipped | an `--llm` server, claude, codex and opencode images | `research-llm/LOCAL_LLM_REPORT.md` section 7 |
| `live-suites` (optional) | Every other live suite that needs no login (what `scripts/live_check.py` runs) | a backend, codex image | - |

**The optional sections.** A whole run adds `remote-mcp` when a host is configured and `local-llm` when an LLM
server is, and asks first on a terminal (they act on that machine or load models): `--host NAME` or `--llm NAME`
chooses one when you have several, `--yes` answers the questions. Without a terminal and without `--yes` they are
skipped with that reason. `live-suites` runs only with `--only live-suites`.

## Options

| Option | |
|---|---|
| `--list` | The checks: what each proves, needs, sends; its timeout |
| `--only NAME[,NAME]` | Only these checks (repeatable; the optional ones too) |
| `--backend auto\|podman\|bwrap\|both` | The backends (auto: each one that works here) |
| `--build` | Build the missing images first (network: their packages) |
| `--host NAME` | remote-mcp on this host of `sandbox-ai hosts` |
| `--llm NAME`, `--llm-models M1,M2`, `--yes` | local-llm on this server, with these models; the questions answered yes |
| `--device PATH` | The device of devices-live |
| `--out DIR` | Where the report goes (default: `live-check/` of the checkout) |
| `--work-dir DIR` | Where the temporary folder is made (keep its path short: socket paths) |
| `--dmg-old DMG`, `--dmg-new DMG` | On a Mac: macos-update's disk images (a .dmg or a downloaded release's folder) |
| `--app PATH` | On a Mac: macos-app's sandbox-ai.app (only read and started; default: the one in `--dmg-new`) |
| `--keep-temp` | Keep the temporary folder with the full logs (it may hold paths of yours) |

## The report

`REPORT-DATE.md` has the machine's facts (sandbox-ai version and commit, kernel, distribution, Podman and its cgroup
version, the backends used), every prerequisite, what left the machine, a table of the checks (outcome, seconds,
what each proves) and per check: what was missing (a skipped check), the exact pytest command, every test with its
outcome and, for a failure, its message, and the last 60 lines of its output. `REPORT-DATE.json` holds the same:
`{"version": 1, "date", "facts", "prerequisites": [{"name", "ok", "detail"}], "network", "counts", "checks":
[{"name", "outcome", "seconds", "proves", "needs", "missing", "network", "command", "tests": [{"name", "outcome",
"seconds", "detail"}], "tail"}], "left_behind_and_stopped"}`. `live-check/` is in `.gitignore`.

A check is **passed** when its tests passed (some may be skipped, e.g. the second backend), **failed** when a test
failed or it ran out of time, **skipped** when a prerequisite was missing or every test skipped itself (the reason is
in the report), and **error** when pytest ended without telling which test failed.

## One check by hand

Each check is a pytest run; the report has its exact command. The same by hand (your own sandbox-ai is still left
alone: each test makes its own folders and the Podman wrapper):

```bash
python3 -m pytest --agent-live --agent-backend podman -q tests/test_live_gaps.py::test_live_hide_covers_a_workspace_added_while_it_runs
```

## On a Mac

The macOS app, its update from one disk image to the next and back, doctor's macOS checks and devices on macOS had
only run against stand-ins (a tar archive for the disk image, `cp` for `ditto`, a `codesign` that says yes). On a Mac
the same command runs them for real; the Linux checks are skipped there and say so:

```bash
scripts/live_check.sh --dmg-old ~/Downloads/sandbox-ai-0.1.154 --dmg-new ~/Downloads/sandbox-ai-0.1.155
```

Run it in a checkout of sandbox-ai, as yourself (never with sudo), in a Terminal of your Mac's desktop (not over
SSH: `macos-app` starts the app on the display), with Python 3.10 or later (`brew install python`; macOS's own is
3.9) and bash 4 or later (`brew install bash`: sandbox-ai's starts want it). `--dmg-old` and
`--dmg-new` each take a disk image or the folder it lies in:

- **Downloaded releases** (the way to check the update a user gets): from each release page this Mac's
  `sandbox-ai-arm64.dmg` (or `-x86_64`), `SHA256SUMS` and `SHA256SUMS.sig` into one folder per release. The update
  then checks them with the release's own key, as it does for a user.
- **Local builds** (`scripts/build_macos_app.sh --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz --out DIR`, two
  versions): a disk image alone. The check writes its SHA256SUMS, signs it with a key of its own made for the run and
  tells the old app it put into its folder to trust that key (the release's key is not exercised then; the report
  says "a local build").

The old one must be 0.1.154 or later (the first macOS app that replaces itself from a disk image), the new one newer.

| Check | What it proves | Needs |
|---|---|---|
| `macos-doctor` | `sandbox-ai doctor --json` on this Mac (the checkout's, and the app's command line with `--app`): the macOS set - `podman machine` in place of user namespaces, subordinate ids, systemd, pasta, Landlock and Bubblewrap, none of which it lists -, every check with a state, and its verdict on podman machine as `podman machine inspect` says it (no machine, stopped, rootful: a failure with the fix; running: ok, or what is not shared) | macOS |
| `macos-devices` | `devices scan --json` on this Mac: the documented fields, every listed node exists, every refusal has a reason, nothing offered for `--device` (a start on macOS refuses every `--device`: the sandbox runs in podman machine's Linux VM); the text form; `sandbox-ai codex --dry-run --device /dev/null` refused with that reason | macOS |
| `macos-app` | sandbox-ai.app (`--app PATH`, else the one in `--dmg-new`, put into the check's folder): `codesign --verify --deep --strict` holds, `Contents/MacOS/sandbox-ai --version` and doctor say its version and that it is the macOS app (`package: macos-app`), `sandbox-ai-app --version` names the sandbox-ai inside it, `sandbox-ai-app --self-test` builds the window against it on this Mac's display and quits | macOS, an app |
| `macos-update` | The old disk image's app, put into a folder of the check's (dragged out as a human does: `ditto`), updates itself with `sandbox-ai update` from releases the check serves on 127.0.0.1 (`SANDBOX_AI_SOURCE`): up to date while only its own is there; then the new one found with a good signature, downloaded (the disk image, SHA256SUMS and its signature - nothing else), installed (`hdiutil`, `ditto`; the new app's signature holds; nothing left beside it); `update --rollback` brings the old one back (`update mode --json` says both). Then the same through the old app's own Settings > Updates - Check now, Install now… (answered yes), Roll back… - on its own Python and PySide6 (`tests/live_macos_app_driver.py`, offscreen: nothing appears on the screen) | macOS, `--dmg-old` and `--dmg-new` |

What it never does on a Mac: nothing is put into `/Applications` and your own app is only read and started (`--app`:
`--version`, `--self-test`, `doctor`); every check has its own HOME, configuration, state and cache in the temporary
folder; the releases come from 127.0.0.1 - nothing leaves the Mac (pytest itself may come from PyPI once, as on
Linux). The report has the macOS version and the architecture among the machine's facts. The tests are
`tests/test_live_macos.py`; their logic is tested on Linux with stand-ins (`tests/test_live_macos_logic.py`).

## Not covered

- The Linux checks on a Mac (sandboxes in podman machine's VM): the macOS checks cover what is the Mac's own; a
  sandbox's isolation there is podman machine's Linux, which the Linux checks prove on Linux.
- A notarised app and Gatekeeper's first start of a downloaded one (the app is signed ad hoc: see the README's
  "Install"); the check copies the app out of the disk image itself, so macOS sets no quarantine on it.
- Whether Claude Code *takes up* an attached skill or plugin by itself needs a model and a login: the checks prove
  the files are where it reads them, read-only, at once.
- A start on another host with an MCP server on a folder of that host (the Board's path): `remote-mcp` covers SSH,
  the host's sandbox-ai and its MCP servers, not a start there.
- `local-llm` with a server that needs a key or a VPN profile: run the smoke runs by hand
  (`sandbox-ai claude --llm NAME:MODEL ...`), the check never copies a secret.
