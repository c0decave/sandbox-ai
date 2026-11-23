# ARTEX in sandbox-ai: review, patches and design

ARTEX (RuoJi6/ARTEX, **AGPL-3.0**, Go with a Next.js console) is an AI penetration-testing platform: a server with a
web console, PostgreSQL for its settings, SQLite stores for what it finds, and agents of its own (the norma harness)
that run shell commands, nmap, a headless browser and custom tools against targets you give it, with an LLM of your
choice. sandbox-ai runs it as a part of its own, `--artex`: a container beside the agent in the session's pod, built
by sandbox-ai from the reviewed source with sandbox-ai's security patches. Reviewed on 2026-11-22 (phase 1); the
integration the same day (phase 2).

The user asked for it with a warning: "check if we can integrate artex ... but check for backdoors of it. if none we
add it" - "they claim they have removed the backdoors spreaded with other versions, we still have to be careful".

## The source

| | |
|---|---|
| Repository | https://github.com/RuoJi6/ARTEX (the original, Autumn-27/ARTEX, is gone; RuoJi6 is one of its developers), licence AGPL-3.0 |
| Commit | `a91b27feb7168c1486f827559bc48a589a920012`, tag `v0.3.15`, 2026-10-09 (44 days old when pinned); git tree `22083e0bb9db66b76b94814f368d0d1dc5c3adbb` - the tree reviewed, and the tree failsafe-security/artex imported |
| Agent harness | `github.com/Autumn-27/norma v0.4.3` (commit `320ae329`, 2026-09-30), Apache-2.0, its `h1:` hash in ARTEX's `go.sum` (recomputed from the tag in the review) |
| Never used | any `artex` image (the backdoored `tomicdd/artex` and every other), a release zip, `install.sh`, `update.sh`, the upstream `Dockerfile`; not failsafe-security/artex's HEAD (5 days old at the review) |

## The review (phase 1), in short

No backdoor was found in the source: not in ARTEX v0.3.15, not in norma v0.4.3, not in failsafe-security/artex. The
reported backdoor of the `tomicdd/artex` image (a `captcha` field of the login handled by `sh -c` before the password
check) exists in no published source: no `captcha`, no exec path in `server/auth.go` at v0.3.15 and seven later tags.
The only unauthenticated routes are `/api/auth/status|init|login|change-password`, `/api/health` and the static
console; no encoded payload, no hidden host, no telemetry. What reviewed means: the auth, the routes, self-update,
workspace, uploads, custom tools, MCP discovery and the start read line by line; the rest of the 63 k lines of Go and
56 k lines of console code by whole-tree scans for every exec, listener, outbound connection, URL, IP, decoder and
hidden character; every npm package's integrity compared with the registry (772 + 85); every direct Go module's hash
recomputed. Not read: the indirect Go modules, the npm packages' code, Chromium and Playwright.

Not clean without conditions - risks by design, each closed or contained below:

| | Finding | Severity | What sandbox-ai does |
|---|---|---|---|
| U1 | Self-update downloads and runs whatever the latest GitHub release of the hardcoded repository is (integrity, no signature), checked on every console load; at start `Bootstrap()` installs a staged binary | High | removed at the build (patch 1, `REMOVE`), checked in the source and the binary |
| U2 | The first caller of `POST /api/auth/init` becomes the admin, and the admin can run commands | High | the route is gone, the handler refuses; sandbox-ai sets the password before ARTEX listens (patches 1, 2) |
| U3 | `Access-Control-Allow-Origin: *` on every API answer, the token accepted from `?token=` | Medium | no CORS header, Host and Origin checked, `?token=` only for event streams (patch 2) |
| U4 | Listeners on every interface (`:8787`), an unauthenticated recording proxy on `:8788` | Medium | loopback only, enforced by the binary (patch 1); nothing of the pod published |
| U5, U6 | Authenticated command execution by design; its agents run with permissions bypassed, as root upstream | High (by design) | its own container: user 1000, no capabilities, read-only root, no file of yours, the sandbox's network (below) |
| U7 | The upstream image is unpinned and pipes NodeSource into a shell | Medium | not used: sandbox-ai's own `Containerfile.artex`, everything pinned |
| U8 | Runtime installs (ripgrep through npm, `npx @playwright/mcp`, skills telling the model to `npm install`) | Medium | `NORMA_RIPGREP_NO_INSTALL=1` with rg in the image, the browser MCP seeded as the image's binary (patch 4), no npm/npx in the image |
| U10 | Asset enrichment asks public DNS resolvers (Cloudflare, Google, Quad9, OpenDNS) for every target name | Low | the system's resolver, which is the sandbox's DNS (patch 3) |
| U9, U11-U14 | npmmirror in a skill's lock, TLS checks that can be switched off, LLM keys in PostgreSQL in clear, the JWT key readable by its agents | Low/Info | contained: nothing leaves but through the allow-list; with `--llm` ARTEX holds no key at all |

## sandbox-ai's changes (AGPL: where they are)

sandbox-ai builds ARTEX unmodified except these security changes, each a file in `sandbox/artex-build/` (and in the
image at `/opt/artex/sandbox-ai`, with `/opt/artex/SOURCE` naming the commit), applied at every build and reviewed
line by line:

- `REMOVE`: `selfupdate/`, `server/update.go`, `server/update_test.go` - the self-update and its HTTP routes.
- `patches/0001-main-no-self-update-loopback-password.patch` (`cmd/artex/main.go`): no `selfupdate.Bootstrap()`, no
  boot state, no settle timer, no restart exit; `-addr` and `-proxy` must be loopback addresses (127.0.0.0/8, ::1,
  localhost; default `-addr 127.0.0.1:8787`); `-admin-password-file` (required) is read and the password set with
  `server.SetAdminPassword` before the server exists; `-keys` is the folder of the JWT key (upstream: beside the
  binary, read-only in the image).
- `patches/0002-server-admin-password-same-origin.patch` (`server/`): no `POST /api/auth/init` route and its handler
  refuses; `SetAdminPassword`; no `/api/update/*` routes; `cors()` adds no header; `sandbox_origin.go` refuses a
  request whose Host is not the loopback or whose Origin is another origin; `?token=` only on a GET with
  `Accept: text/event-stream`.
- `patches/0003-enrich-system-resolvers.patch` (`enrich/`): dnsx resolves through `/etc/resolv.conf`'s nameservers;
  none: DNS enrichment off.
- `patches/0004-browser-mcp-pinned.patch` (`db/db.go`): the seeded browser MCP is `playwright-mcp --headless
  --no-sandbox --executable-path /usr/lib/chromium/chromium`, not `npx @playwright/mcp`.
- `tools/package.json`, `tools/package-lock.json`: Playwright's MCP 0.0.84 and CLI 0.1.23 with their Playwright
  1.65.0-alpha-2026-10-30, integrity-locked.

The build checks after the patches apply: no `selfupdate` import, no `/api/update/` or `/api/auth/init` route, no CORS
header, no `dnsx.DefaultResolvers`; the patched lines there. In the built binary: `go list -deps` holds no
`selfupdate`, the route strings are absent, `-h` names `-admin-password-file`, and the binary refuses `-addr
0.0.0.0:8787`, `-proxy :8788` and a start without a password file. `tests/test_artex.py` checks every one of these, and
with `ARTEX_SOURCE` set to an unpacked archive of the commit also that the tree is the reviewed one and the patches
apply.

The AGPL: using ARTEX for yourself puts no duty on you. sandbox-ai does not ship ARTEX: your build fetches it from
GitHub and applies the patches above, and the modified program runs on your machine for you. If you let others use it
over a network, or pass the image on, the AGPL asks you to offer them its source: the commit above plus the patches
in `/opt/artex/sandbox-ai`.

## The image (`sandbox-ai build artex`, `sandbox/Containerfile.artex`)

| Stage | What | Pinned |
|---|---|---|
| source | GitHub's archive of the commit (`codeload.github.com/RuoJi6/ARTEX/tar.gz/COMMIT`), its git tree computed and compared, `REMOVE` and the patches, the source checks | the commit and the tree; Debian at the snapshot |
| web | `npm ci --ignore-scripts` of ARTEX's own `web/package-lock.json` (npm checks every package's integrity), `NEXT_TELEMETRY_DISABLED=1`, `next build --webpack` as a static export (Turbopack's production build needed more than 5 GB of memory) | `node:22.23.4-bookworm-slim@sha256:5ec23b7c...` (2026-11-05) |
| build | `go mod download && go mod verify` with `GOSUMDB=sum.golang.org` and `GOPROXY=https://proxy.golang.org`, `GOFLAGS=-mod=readonly`, `GOTOOLCHAIN=local`, `CGO_ENABLED=0`, `go telemetry off`; `go vet`; sandbox-ai's Go tests of its patches; `go build -tags embedui`; the binary checks | `golang:1.26.10-bookworm@sha256:5de7c99e...` (2026-11-05) |
| tools | `npm ci --ignore-scripts` of `artex-build/tools`, no browser download | the lock (2026-10-30/31) |
| what runs | Debian 12 from `snapshot.debian.org/.../20261104T000000Z` (apt's signatures checked): PostgreSQL 15.19, Chromium 156.0.8078.79, nmap 7.93, ripgrep, python3, git, curl, jq, dig, whois, netcat; Node 22 (no npm, no npx); the binary, ARTEX's skills, its LICENSE and README; user `artex` (1000) | `debian:bookworm-20261104-slim@sha256:734a72b3...` (2026-11-05) |

Every pin was at least 14 days old on 2026-11-22. Another commit, tree, base or snapshot given as a build argument is
refused by `sandbox-ai build artex`. A new ARTEX release is reviewed first, then its commit, tree and patches change
together.

## The part (`--artex`, `sandbox/artex.py`, `sandbox/artex_runtime.py`)

- **Where it runs**: `POD-service-artex`, a container in the session's pod started like a service (`sandbox/services.py`):
  created, initialised, proven to be in the pod's user namespace (and behind the VPN's kill switch with `--vpn`), and
  started only once the sandbox's supervisor has its firewall and proxy up. It shares the pod's network: the agent's
  firewall, allow-list, connection proxy, approvals, kill switch, VPN or Tor - whatever ARTEX's agents try.
- **What it may do**: user 1000 (PostgreSQL refuses root, and ARTEX's agents get no root), `--cap-drop=ALL`,
  `no-new-privileges`, a read-only root, its own `/tmp` and `/dev/shm`, limits of 4 GB, 2 CPUs and 2048 processes.
  nmap runs without raw sockets (connect scans), Chromium without its own sandbox (the container is the boundary).
- **What of this computer it sees**: read-only files only - `artex_runtime.py`, its configuration (`artex.json`: ports,
  data folder, the LLM's placeholders), the sandbox's `resolv.conf` and `hosts`, the run's CA certificate. No folder of
  the project, no HOME, no login, no engine socket. Its data (PostgreSQL's cluster, ARTEX's stores and workspace, its
  JWT key, its skills) is a tmpfs, or with `--artex-data session` a Podman volume kept with the session and removed
  with it.
- **Its start** (`artex_runtime.py serve`): PostgreSQL on a Unix socket only (`listen_addresses` empty), trusting only
  user 1000; then ARTEX with `-addr 127.0.0.1:8787 -proxy 127.0.0.1:8788`, its environment made from scratch (the
  database, the folders, the LLM's placeholders, the CA, `NORMA_RIPGREP_NO_INSTALL=1`, `NEXT_TELEMETRY_DISABLED=1`,
  `DO_NOT_TRACK=1`, Playwright's MCP on the image's Chromium - nothing inherited), and the admin password through a
  pipe on `-admin-password-file /dev/fd/N`.
- **The admin password**: chosen by sandbox-ai at every start (24 random bytes, URL-safe), handed to podman in its
  environment (`--env SANDBOX_AI_ARTEX_PASSWORD`: the name on the command line, never the value), taken out of the
  runtime's environment before anything starts (ARTEX and its agents' shell never get it in theirs), set by ARTEX
  before it listens. A password changed in the console holds until the next start. Kept for `sandbox-ai artex
  console` in the run's ARTEX folder beside the run's directory (0700, the file 0600), removed with the run. What the
  password guards is the console against everything outside ARTEX's container (this computer's other processes and
  browser pages, the sandbox's agent): ARTEX's own agents run as ARTEX's user and can read what ARTEX can - its JWT
  key, its database, the container's initial environment - which is ARTEX's design (U5, U14), not a way out of the
  container.
- **The console**: once the services' answer says ARTEX started, the launcher listens on this computer's
  `127.0.0.1:8787` (or `--artex-port`, or the next free of 20 ports) and hands each connection to its own `podman exec
  -i POD-service-artex python3 -I /sandbox-artex/artex_runtime.py connect`, which connects to ARTEX's loopback. No
  port of the pod is published and nothing listens on another interface; it works the same with podman machine on
  macOS. Any local user of this computer can reach 127.0.0.1: the password and ARTEX's Host and Origin checks hold.
  `sandbox-ai artex console [RUN] [--json]` shows the address, the user (`ARTEX`) and the password.
- **Its LLM**: with `--llm NAME[:MODEL]`, ARTEX starts with that server (OpenAI-compatible API at the address the
  sandbox reaches, its model) and the placeholder `sandbox-ai-llm-key-placeholder` as its key: the connection proxy
  swaps it for the real key on the wire, so ARTEX never holds one. A profile saved in ARTEX's console wins. Without
  `--llm`, you configure the model in the console: its host must be on the allow-list (`--allow-host`), and a key typed
  there is stored in ARTEX's database (in clear, U14).
- **Its targets**: only what the allow-list opens (`--allow-tcp IP:PORT`, `--allow-host`, `--allow-domain`, `sandbox-ai
  net allow` during the run). The preset `artex-pentest` asks for the target and opens exactly its ports.
- **The agent beside it**: `SANDBOX_AI_ARTEX=http://127.0.0.1:8787` (the API in the pod); it gets no password.
- **Modes**: the mode `paranoid` refuses `--artex` (ARTEX's agents act without asking); `--artex` counts as an option
  that weakens a project's mode.

## What was checked here, and what only a live run proves

Checked in the development sandbox on 2026-11-23 (Go 1.27.3 from sandbox-ai's signed toolchain catalog, GOSUMDB on,
`GOFLAGS=-mod=readonly`, `GOTOOLCHAIN=local`, Go's telemetry off; Node 26 for the console):

- GitHub's archive of the commit: its git tree is `22083e0b...`; `REMOVE` and the four patches apply exactly as the
  build does, and the build's source checks hold.
- `go mod download` through proxy.golang.org and `go mod verify`: every module verified - except that the zips of
  `modernc.org/sqlite` and `modernc.org/libc` (and `klauspost/compress`) come from storage.googleapis.com, which this
  sandbox does not reach. `klauspost/compress` was fetched from GitHub instead (verified against go.sum all the same);
  the SQLite driver could not be had, so the server and the binary were compiled with an empty stand-in for that one
  package (it is imported only for its driver registration, `_ "modernc.org/sqlite"`).
- `go build` of every package and of `cmd/artex` (also with the console embedded, `-tags embedui`), `go vet` of every
  package whose test dependencies were there, `gofmt` of the patched files: clean.
- Go unit tests: every package's that runs without PostgreSQL passes; the tests that need PostgreSQL skip or fail
  for want of it (22), the SQLite ones for want of the real driver (29) - none for anything else. sandbox-ai's own
  tests of its patches (added by them: `TestLoopbackOnly`, `TestSystemResolvers`, `TestLoopbackHost`,
  `TestSameOrigin`, `TestSandboxOriginRefusesOtherHostsAndOrigins`, `TestCorsAddsNoHeader`,
  `TestExtractTokenTakesAURLTokenOnlyForAnEventStream`, `TestAuthInitAlwaysRefuses`,
  `TestSetAdminPasswordNeedsTheStoreAndAValidPassword`) pass, and the build runs them.
- The binary checks of the build against that binary: no `selfupdate` in `go list -deps`, no update or init route
  strings (the embedded console still names `/api/update/check` in its JavaScript, which is why the check looks for
  the routes, not the path), `-h` names `-admin-password-file`, and `-addr 0.0.0.0:8787`, `-proxy :8788` and a start
  without the password file are refused with sandbox-ai's messages.
- The console: `npm ci --ignore-scripts` (720 packages) and `next build` as a static export. Turbopack's build was
  killed at the sandbox's memory limit (about 5 GB free); `next build --webpack` built every page, so the recipe
  uses it.

Only the image build and a live start can show the rest; `scripts/live_check.sh --only artex-live` (it asks first)
builds the image (`sandbox-ai build artex`), starts an agent with `--artex` and checks the console on 127.0.0.1
(login with the password of `sandbox-ai artex console`, `POST /api/auth/init` refused, a foreign Origin refused,
`/api/update/check` 404), that PostgreSQL has no TCP port, and that ARTEX's own process environment holds no
password. Still to be seen there: the build with the real SQLite driver and Go 1.26.10, the Debian snapshot's
packages, PostgreSQL's cluster under the container's user on a tmpfs and a volume, ARTEX's start and its
migrations, the console through the forward in a real browser, that its tasks take the `--llm` configuration from
its environment (a profile of its console is the usual way; the environment is its fallback), the proxy's key and
CA for its Go client, and that the Playwright MCP takes the image's Chromium.
