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
| The images `localhost/codex-sandbox`, `claude-sandbox`, `opencode-sandbox` | codex: most checks; claude: kits-attach, plugins-attach, local-llm; opencode: local-llm (harnesses-live builds every harness's image it lacks itself) | `sandbox-ai build NAME`, or `--build` |
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
  LLM server (local-llm, harnesses-live and cairn-workers-live, when asked), a missing harness image's packages
  (harnesses-live, cairn-workers-live; Cairn's source from GitHub for its image), and with `--build` the images'
  packages.
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
| `toolchain-live` | Build toolchains in a real running session, under Podman and under Bubblewrap: `sandbox-ai toolchain add go` and `python` (the signed catalog; `SAI_LIVE_TOOLCHAINS=FILE`: a `toolchains.json` of your own builds, added unsigned), `toolchain attach` into the running sandbox - mounted read-only, `nosuid`, `nodev` at `/opt/toolchains/NAME`, the commands found by a shell started before -, a hello world built and run with go, `python3` the toolchain's, `toolchain list --run`, `detach`; and a start with `--toolchain go` With `--accept-android-sdk-license` also `android-sdk`, the toolchain built on this machine: its recipe of the signed catalog, Google's files fetched and held to it, a start with `--toolchain android-sdk` (the `jdk` with it), `adb` and `aapt2` run, and a small app assembled with Gradle and the Android Gradle plugin against the read-only SDK (x86_64; skipped without the option: the license is yours to accept) | a backend, codex image; the catalog (about 90 MB from GitHub) or `SAI_LIVE_TOOLCHAINS` | the user, 2026-11-22 |
| `android-emulator-live` (optional) | The Android emulator as a service beside a real sandbox: its image built from sandbox-ai's own Containerfile and Google's downloads (`images build-image android-emulator --builtin android-emulator`, about 2.7 GB), a start with `--service android-emulator` - the agent starts once Android has booted, reaches it as `adb connect android-emulator:5555` (`sys.boot_completed` 1) and has no `/dev/kvm` itself; the image is removed again | Podman, codex image, x86_64 with `/dev/kvm`, `--accept-android-sdk-license` | Android as a service |
| `update-live` | Signed updates from the real GitHub: the newest release's `SHA256SUMS` is signed by a key of this checkout's `sandbox/release-signers` and lists every kind's file (the bundle's SHA-256 checked); `sandbox-ai update --check` of this checkout finds it and offers it only when it is newer (never a downgrade); a copy one version older is offered it; nothing is installed | network: api.github.com (read-only, no token), github.com's release downloads (about 16 MB) | signed updates (user 2026-11-22) |
| `remote-mcp` (optional) | A real host of `sandbox-ai hosts`: `hosts test`, its MCP servers listed over SSH, its demo server started and stopped there (only when none of it runs) | `--host NAME` | remote MCP |
| `local-llm` (optional) | One smoke run - a file written with the agent's own tool, a reply - of the three best models of the research (`qwen3.8-flash-next`, `ornith-1.5-35b-a3b-mlx`, `ornith-1.5-122b-mlx`) x Claude Code, OpenCode and Codex, through the real launcher with `--llm` and nothing else open; a model the server does not offer is skipped | an `--llm` server, claude, codex and opencode images | `research-llm/LOCAL_LLM_REPORT.md` section 7 |
| `harnesses-live` (optional) | Every harness the launcher knows - `claude`, `codex`, `opencode`, `opencode2`, `hermes`, `dsh`, `qwen`, `goose`, `openhands`, `nanobot`, `letta`, `pi` - does the same smoke run (a file written with its own tool, the nonce replied) through the real launcher with `--llm SERVER:MODEL` (the first of `--llm-models`) and nothing else open, with Podman and with Bubblewrap. Podman: a missing image is built first with `sandbox-ai build NAME` (the report's harness table says how long; an image that cannot be built here skips that harness with the build's last line). Bubblewrap runs this host's own installation of the harness: one that is not on PATH is skipped with that reason; so is a server on this host's loopback, which only Podman reaches. The report has a table per harness: its image, its Podman run, its Bubblewrap run | an `--llm` server; Podman and/or Bubblewrap; network for a missing image's packages | the user, 2026-11-22 ("all the harnesses have the (not tested yet)") |
| `presets-live` (optional, only with `--only presets-live`) | Every built-in preset (`sandbox/presets/*.json`, not the rulesets) is started for real through the launcher - `sandbox-ai AGENT --preset NAME`, its first agent - with the same smoke task (a file written with the agent's own tool, the nonce replied; a preset with a read-only workspace reads the nonce from a file there instead), with Podman and, where the preset can run there, Bubblewrap (a host port, a service, Cairn or a team: Podman only). The model: `--llm SERVER:MODEL` and nothing else open (a preset's `--api-key` gets a made-up key of the check's own, its host stays closed), or with `--provider-login` each agent's own provider with your login. A folder value gets a folder of the check's own, a text value a short text; each MCP server a preset names is added from the catalog it names (`mcp:binwalk`) into the check's own configuration and must say "started", each service must say "ready"; each toolchain a language preset brings (`--toolchain`) is added to the check's own store first (`SAI_LIVE_TOOLCHAINS=FILE`: your own builds, unsigned). What only you can give - a target to test, a VPN profile, your display (`--gui`), your sound, an image variant, a GPU - comes from `--preset-values FILE` or the preset is skipped with that reason and the exact command to run it yourself. The report has a table per preset: started, its part came up, skipped (why), failed (its last line); `scripts/import_live_report.py REPORT.json` makes it the presets' live light | an `--llm` server or `--provider-login`; Podman and/or Bubblewrap; network for the MCP servers' images, the toolchains' archives and a missing image's packages | the user, 2026-11-22 ("mir scheint das alle presets auch noch nicht getestet sind") |
| `mcp-live` (optional, only with `--only mcp-live`) | Every server of the signed MCP catalog (sandbox-ai-mcp) through the path a start uses: `sandbox-ai mcp add NAME` with its defaults (the catalog's current digest; `--backend both` where it has a Bubblewrap form) in the check's own configuration, a kept session with `--mcp NAME` (Podman, and Bubblewrap where the entry has a rootfs), the server started by sandbox-ai (supervisor, bridge, each connection's container or Bubblewrap sandbox), and its connection made from the agent's side through the session's relay: MCP initialize, tools/list (exactly the tools of `servers/NAME/tools.json`) and the smoke calls of `servers/NAME/smoke.json` - the calls its CI makes, judged as its `smoke_test.py` judges them (a rate-limited upstream is skipped, not failed). `/work` is a workspace with the smoke files (`tests/fixtures` and `scripts/fixtures.py` of the MCP repository); a server on the start's workspace gets a second session in a second workspace, and each instance must mount its own workspace only, no answer naming the other's file. The MCP repository: `--mcp-repo DIR`, else `projects/sandbox-ai-mcp` of the checkout, else cloned from GitHub (asked first). A server that needs a secret or a network only you can give is skipped with the reason and the commands; one that dies before the handshake shows the last lines of its `supervisor.log`. The report has a row per server and backend: added, started, handshake, tools, calls passed/failed (the first failure), isolation, seconds. `--mcp-vpn PROFILE`: the servers with network are added behind a Tor profile of that name (`mcp add --vpn`: each connection's container in the namespace of the server's own Tor container, the path of a server like gowitness-tor), made in the check's own configuration; the others, Bubblewrap and a server whose catalog says it does not work behind Tor are skipped with the reason, and a smoke call about an `.onion` address (whose refusal only a start without Tor shows) is left out. A VPN profile of yours needs your keys, which a check never copies: `sandbox-ai mcp check --all --vpn PROFILE` checks with it in your configuration (initialize and tools/list) | Podman and/or Bubblewrap (the codex image for Podman, built when missing); network for the catalog, the images and rootfs archives, and what the servers' smoke calls reach | the user, 2026-11-23 (binwalk and radare2 ended before the handshake; "in-depth testing aller mcp server") |
| `cairn-workers-live` (optional) | Every harness as one of Cairn's workers - `claude`, `codex`, `opencode`, `opencode2`, `hermes`, `dsh`, `qwen`, `goose`, `nanobot`, `letta`, `pi` - through real Cairn in a real pod: the harness is the lead with `--llm SERVER:MODEL` (the first of `--llm-models`) and nothing else open, with `--cairn --cairn-workers AGENT` and a small project made at the start (a code word to state in lower case). Cairn's dispatcher hands its task through sandbox-ai's relay to the harness's own one-shot run; the board must get the fact from the worker `sandbox-AGENT`, and the project is stopped at the end. Missing images (the harness's, `cairn`, `browser`) are built first. OpenHands is skipped with the reason (as the lead `--cairn` is refused: its Agent Canvas holds port 8000; beside another lead it needs its own login, not `--llm`). The report has a row per harness: its image, its run, and the status that gives `sandbox/cairn_workers.json` (verified, failing, untested) - copied there by hand with the report's name as the evidence; `sandbox-ai cairn workers` then shows it. `SAI_LIVE_CAIRN_SECONDS` sets how long it waits for the fact (900) | an `--llm` server; Podman; network for a missing image's packages | the user, 2026-11-22 ("why workers only for claude and not all harnesses?") |
| `artex-live` (optional) | ARTEX beside an agent (`--artex`, docs/ARTEX.md): its image built when missing (`sandbox-ai build artex`: GitHub's archive of the reviewed commit, its tree checked, sandbox-ai's patches), one session started with it; its console forwarded to this computer's 127.0.0.1 only; the password of `sandbox-ai artex console` logs in, a wrong one does not; `POST /api/auth/init` and `/api/update/check` are gone; another Host, another Origin and a token in the URL are refused; in its container it runs as user 1000, listens on the loopback only (8787, 8788; PostgreSQL on no TCP port) and ARTEX's own environment holds no password. Asks first | Podman, codex image; network for the image's build (about 2 GB, once) | the user, 2026-11-22 ("check for backdoors ... we still have to be careful") |
| `burp-live` (optional, only with `--only burp-live`) | Burp Suite as an MCP server. Host mode: your Burp with the extension of the signed image loaded and enabled (`sandbox-ai mcp burp extension`) answers at `--burp-url` (default `http://127.0.0.1:9876/mcp`): `mcp add burp` says so, `mcp burp check` names the edition, and through the server's real relay (`mcp start`, its socket, `mcp_http.py` on this host) initialize and tools/list answer with the extension's tools (27 with Professional, 24 with Community) and `url_encode` is called. Container mode (`--burp-jar JAR [--burp-edition E] [--burp-license-file FILE]`): your jar runs headless in the server's container - what only a real Burp proves: its headless questions (the terms, the licence key, online activation) answered, the extension loaded from the user options file, its Swing tab made headless - and `url_encode` and `get_proxy_http_history` answer through the relay; the licence key is in no log line. Skipped while the signed catalog has no image of burp | Podman; your Burp (host mode) and/or your Burp jar (container mode); network: container mode the image (ghcr.io) and portswigger.net for a Professional activation (once) | the user, 2026-11-24 (Burp MCP, Community and Pro) |
| `live-suites` (optional) | Every other live suite that needs no login (what `scripts/live_check.py` runs) | a backend, codex image | - |

**The optional sections.** A whole run adds `remote-mcp` when a host is configured and `local-llm`,
`harnesses-live` and `cairn-workers-live` when an LLM server is, and asks first on a terminal (they act on that machine or load models): `--host NAME` or `--llm NAME`
chooses one when you have several, `--yes` answers the questions. Without a terminal and without `--yes` they are
skipped with that reason. `live-suites` runs only with `--only live-suites`, and `presets-live` (a run of every
built-in preset: hours) only with `--only presets-live`:

    scripts/live_check.sh --only presets-live --llm NAME [--preset-values FILE] [--presets A,B] [--provider-login]
    scripts/live_check.sh --only mcp-live [--mcp-servers binwalk,radare2] [--mcp-repo DIR] [--mcp-vpn tor] [--yes]
    python3 scripts/import_live_report.py live-check/REPORT-DATE.json   # its results become the presets' live light

`scripts/import_live_report.py` writes `sandbox/live_evidence.json`: per preset (and per harness, from
`harnesses-live`) and backend the last run that passed or failed, with its date, the sandbox-ai version and the
report it comes from; a skipped run is no evidence and imports nothing. `sandbox-ai presets list` and `presets show`,
the web page's Presets and the app's gallery show it: a green light with the date and version when it passed, red
with the reason when it failed, nothing when there is no data yet.
`artex-live` runs only with `--only artex-live` (it asks first too: it builds ARTEX's image when missing).

## Options

| Option | |
|---|---|
| `--list` | The checks: what each proves, needs, sends; its timeout |
| `--only NAME[,NAME]` | Only these checks (repeatable; the optional ones too) |
| `--backend auto\|podman\|bwrap\|both` | The backends (auto: each one that works here) |
| `--build` | Build the missing images first (network: their packages) |
| `--host NAME` | remote-mcp on this host of `sandbox-ai hosts` |
| `--llm NAME`, `--llm-models M1,M2`, `--yes` | local-llm, harnesses-live, cairn-workers-live and presets-live on this server, with these models (all but local-llm: the first); the questions (also artex-live's) answered yes |
| `--preset-values FILE`, `--presets A,B`, `--provider-login` | presets-live: the values only you can give, as JSON `{"pentest-lab": {"target": "10.0.0.5"}}`; only these presets; the agents' own providers with your logins (your HOME and data folder are read for them, asked first), in place of or beside `--llm` |
| `--mcp-vpn PROFILE` | mcp-live: the servers with network behind a Tor profile of this name, made in the check's own configuration (the others skipped); every server once more behind Tor is a run of its own: `--only mcp-live --mcp-vpn tor` |
| `--mcp-servers A,B`, `--mcp-repo DIR` | mcp-live: only these servers of the signed catalog; the checkout of sandbox-ai-mcp whose smoke specs, tools.json and fixtures it takes (default: `projects/sandbox-ai-mcp`, else cloned from GitHub after the question) |
| `--device PATH` | The device of devices-live |
| `--accept-android-sdk-license` | toolchain-live, android-emulator-live: you accept the Android Software Development Kit License Agreement (https://developer.android.com/studio/terms) for what these checks fetch from Google (the Android SDK, the emulator); without it those parts are skipped |
| `--burp-url URL`, `--burp-jar JAR`, `--burp-edition community\|professional`, `--burp-license-file FILE` | burp-live: your Burp's MCP endpoint (host mode; default `http://127.0.0.1:9876/mcp`); your Burp jar for container mode, its edition (default: from its name) and a file with your Professional licence key (the check's own secret store; activated online once) |
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
- `local-llm` and `harnesses-live` with a server that needs a key or a VPN profile: run the smoke runs by hand
  (`sandbox-ai claude --llm NAME:MODEL ...`), the check never copies a secret.
