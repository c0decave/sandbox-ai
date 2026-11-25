# Toolchains: build tools mounted into a sandbox

A toolchain is a compiler or interpreter with its package manager (Go, Rust, a JDK with Maven and Gradle, Node,
Python ...), kept on the host and mounted **read-only** at `/opt/toolchains/NAME` into a sandbox: at its start
(`--toolchain NAME`) or while it runs (`sandbox-ai toolchain attach RUN NAME`). The agent builds in its own shell,
under its own rules; nothing of the toolchain runs outside the sandbox. Every agent, Podman and Bubblewrap.

Code: `sandbox/toolchains.py` (the catalog, the store, the mount, the command line), the launcher
(`agent_sandbox.py`: `--toolchain`, the mounts, the plan), the supervisor (`runtime.py`: PATH of the agent's
processes). The toolchains themselves: `sandbox-ai-mcp/toolchains/NAME/` (built by its CI, never on your machine -
but for what nobody but its publisher may pass on: [built on your machine](#toolchains-built-on-this-machine-recipes),
`sandbox/toolchain_recipes.py`).

## What a toolchain is

One archive (`tar.xz`) of the files of one prefix, and its manifest, which is its entry in the signed catalog:

| key | meaning |
|---|---|
| `name` | `[a-z][a-z0-9-]{0,31}` (not `bin`): the folder `/opt/toolchains/NAME` |
| `version`, `title`, `description`, `license`, `homepage` | what it is; `version` is the upstream release's (`1.25.4`, `21.0.9+10`) |
| `arch` | `x86_64` or `aarch64`: an entry per architecture |
| `url`, `sha256`, `size`, `unpacked` | the archive (a release asset of sandbox-ai-mcp), its SHA-256 and sizes |
| `commands` | `{"go": "bin/go", ...}`: the programs put on PATH, each a file below the prefix. Named one by one: a toolchain never shadows a command it did not name |
| `env` | `{"JAVA_HOME": "{prefix}"}`: variables its programs need; `{prefix}` is `/opt/toolchains/NAME`. They reach the agent's own environment and every `bash -c` (env.sh), so none may steer code the toolchain does not ship (`toolchains.ENV_REFUSALS`, each with why: the loader and the C library - `LD_*`, `DYLD_*`, `MALLOC_*`, `GCONV_PATH`, `LOCPATH`, `GLIBC_TUNABLES`, a locale, the resolver's; interpreters and package tools - `PYTHON*`, `PERL5*`, `RUBY*`, `NODE_*`, `BUN_*`, `JAVA_TOOL_OPTIONS`, npm's and pip's code and trust keys; shells - `BASH_*`, `ENV`, `ZDOTDIR`, `PROMPT_COMMAND`; programs others start - `PATH`, `EDITOR`, `PAGER`, `LESSOPEN`, `SSH_*`; `GIT_*`; the sandbox's and the agents' configuration and hooks - `HOME`, `XDG_*`, `SANDBOX_AI_*`, `CLAUDE_*`, `CODEX_*`, `OPENCODE_*`, `QWEN_*` ...; connections - proxies, certificate files, `SSLKEYLOGFILE`, `OPENSSL_*`; telemetry but sandbox-ai's own opt-out values). Refused in a catalog; an entry in the store from before is listed with `refused` and refused at a start and an attach, and env.sh, the wrappers and the agent's environment never carry such a variable. One exception (`toolchains.PYTHON_PATH`): `PYTHONPATH` as one folder of the toolchain's own tree (`{prefix}/python`, pyside6's Python packages) - the signed, read-only tree, never a folder the sandbox can write, never a list; sandbox-ai's own hooks run `python3 -I`, which does not read it |
| `caches` | `{"NPM_CONFIG_PREFIX": ".local"}`: variables that name a writable folder, always below the sandbox's HOME. Nothing is ever written into the tree (it is read-only); most package managers keep their caches in HOME by themselves (`~/go`, `~/.cargo`, `~/.m2`, `~/.gradle`, `~/.npm`, `~/.nuget`, `~/.cache`) |
| `hosts` | the registries its package manager reaches (`proxy.golang.org`, `sum.golang.org`): opened as `--allow-host` does |
| `allow` | launcher presets opened with it (`github`, `pypi`, `npm`: what `--allow-github` ... open) |
| `needs` | what it takes from the system: `{"glibc": "2.28", "libraries": ["libstdc++.so.6"]}`. Everything else it brings itself |
| `requires` | other toolchains it does not work without (`rust` links with `cc`): attached with it |
| `sources` | where its files come from: each upstream URL with its SHA-256 (and the signature checked at build time) |

**The floor.** Under Podman the sandbox's `/usr` is the agent image's (Debian bookworm, glibc 2.36), under Bubblewrap
it is the host's, of any distribution. A toolchain therefore brings everything but the kernel, the C library
(`needs.glibc`: the oldest glibc its programs run with) and the few libraries `needs.libraries` names. The build
checks this claim (every ELF file of the tree: its `NEEDED` libraries are in the tree, glibc's own, or named; its
`GLIBC_x.y` versions are not above the floor). sandbox-ai checks the other side before it mounts: under Bubblewrap
the host's glibc (`confstr`) and libraries (the loader's cache), under Podman what sandbox-ai's own images have
(`toolchains.OWN_IMAGE`: glibc 2.36, libstdc++, libgcc_s, libz, OpenSSL 3, and of their GUI base the libraries Qt
loads: GLib, EGL, GL, X11, fontconfig, FreeType, xkbcommon, D-Bus, Brotli, Kerberos, zstd). A
host below the floor is refused with the reason (`go needs glibc 2.28 or newer, this host has 2.26`); an image of
your own (`--image`, a variant on your base) is not known and gets a note instead.

## The catalog and its signature

`toolchains.json` at the top of sandbox-ai-mcp, beside `catalog.json`, with `toolchains.json.sig`: the same
signature as the MCP catalog's (`ssh-keygen -Y sign`, namespace `sandbox-ai-mcp`, principal `mcp@sandbox-ai`,
checked against `sandbox/mcp-signers` by `release_keys.verify`). No second key, no second trust path:

    signed toolchains.json  ->  url + sha256  ->  download, SHA-256 checked before anything is unpacked
                            ->  unpacked without trusting it (mcp_rootfs.unpack)  ->  the store  ->  mounted read-only

`{"schema": "sandbox-ai-toolchains", "version": 1, "toolchains": [ENTRY, ...]}`. The signature says: every entry
was built by the catalog's CI from the pinned upstream release (URL, SHA-256, the upstream signature where one is
published, each release at least 14 days old), its tree passed the floor check, and a hello world was compiled and
run with it through sandbox-ai's own mount path under Podman and under Bubblewrap (`scripts/sign_catalog.py`
refuses to sign without both reports for exactly the archive's SHA-256).

How an entry is made (sandbox-ai-mcp, `docs/TOOLCHAINS.md`): `toolchains/NAME/toolchain.json` is a recipe of data,
never a shell - `fetch` (used only with its pinned SHA-256 or SHA-512, with the hash a verified checksum file lists
for it, or after a `gpg` step verified it against pinned keys), `unpack` (without trusting the archive), `write`,
`link`, `remove` -; `scripts/toolchain.py build` runs it, checks the tree against `needs` and writes the archive
and the entry with its `sources`.

`toolchain add` takes an entry only from a catalog whose signature verifies. `--unsigned` takes one from an
unsigned catalog (your own, `--catalog FILE`), never one whose signature is bad; every start with such a
toolchain says so.

## Toolchains built on this machine (recipes)

The Android SDK is published under the Android Software Development Kit License Agreement
(https://developer.android.com/studio/terms; the text sandbox-ai relies on is the one Google's own repository ships
with every package, kept in `sandbox/recipes/android-dev/package-xml/`: "January 16, 2019"). Section 3.4: "you may not
copy (except for backup purposes), modify, adapt, redistribute ... the SDK or any part of the SDK." An archive of it
on a release page would be redistribution, so there is none: not on GitHub, not in a registry, not in sandbox-ai-mcp's
CI. What section 2 asks - that whoever uses the SDK accepts the agreement first - is done where the files arrive: on
your machine.

`toolchains.json` has a second list, `"recipes"`, signed with the first (a sandbox-ai from before reads
`"toolchains"` alone). An entry is a manifest as above and, in place of `url`:

| key | meaning |
|---|---|
| `recipe.terms` | the license you accept before anything is fetched: an id of `toolchain_recipes.TERMS` (`android-sdk-license`). What you accept is said by sandbox-ai itself; a recipe that names a license this sandbox-ai does not know is refused |
| `recipe.vars` | texts the steps name as `{KEY}` (the license text sdkmanager keeps in each `package.xml`, once) |
| `recipe.steps` | data, never a shell: `fetch` (an https URL of the publisher, with `size` and `sha256` and/or `sha1`), `unpack` (that zip into a folder of the tree, `strip` leading path parts), `write` (a small file), `link` (relative, inside the tree), `chmod` (644 or 755). `{prefix}`, `{version}`, `{arch}` and the `vars` stand in their texts |
| `sha256` | the SHA-256 of `{name, version, arch, recipe}` as canonical JSON (`toolchain_recipes.digest`): the tree's name in the store. A recipe changed under its name is another hash and is refused where the entry's does not match |
| `size`, `unpacked` | what it downloads, what its tree takes |

`sandbox-ai toolchain add NAME`:

    the catalog's signature  ->  the license said, accepted (--accept-android-sdk-license, or a yes on this terminal;
                                 never asked in the modes high and paranoid; nothing is fetched before)
                             ->  each fetch: https only, streamed to a file, its size and hashes checked before use
                             ->  each zip unpacked without trusting it  ->  the small files written
                             ->  every file and folder read-only  ->  the store (SHA256/), installed.json

The zip is read as an archive of a stranger: no member path that leaves the tree, nothing written through a link,
link targets relative and inside, no set-user-ID, set-group-ID or world-writable bit, no device, no more bytes per
member than it says, a bound on all of it; on a file system that ignores case two names it would take for one are
refused (the NDK has such headers). Each download is removed as soon as it is unpacked; whatever fails, nothing of
the build is left. The entry in `installed.json` keeps the recipe, who signed it and which license was accepted,
when and how (`"terms": {"id", "how": "option"|"asked", "accepted"}`). From there it is a toolchain like any other:
the same mount, wrappers, variables, registries, `attach` and `detach`.

A start that names one not in the store offers `toolchain add NAME --accept-...` as a question of its own (its yes
is the acceptance; No first; `--fix` never does it; `--fix-only toolchain:NAME` does; the modes high and paranoid
refuse the offer and name the command). The app and the web interface ask the same way: never by "Fix all".

**android-sdk** (the lines 37, 36, 35: one API level each - its platform and build-tools - with platform-tools, the
command-line tools, the NDK and CMake; x86_64). Its files are the rows of `sandbox/recipes/android-dev/sdk.lock`,
each with Google's SHA-1 and the SHA-256 of a download that was made and compared (2026-11-24). It requires `jdk`.
Read-only has consequences the recipe meets: `licenses/android-sdk-license` and each package's `package.xml` are
written by it (sdkmanager and the Android Gradle plugin would write them), `GRADLE_OPTS` carries
`-Dorg.gradle.project.android.builder.sdkDownload=false` so that the plugin names a missing package instead of
trying to install it, and `sdkmanager` is no command of it (it makes a lock folder in the SDK even to list). Tried
with the real files on 2026-11-24: the tree built in a minute, `gradle assembleDebug` of a small app (the plugin
9.5.2, Gradle 9.9.0 of `jdk`, compileSdk 37, buildToolsVersion 37.0.0) built its APK and wrote nothing into the tree.
What its ELF files need was measured on that tree (glibc 2.28: CMake; `libgcc_s`, `libz`); nobody's CI builds it.

## The store

`~/.local/share/sandbox-ai/toolchains/` (`XDG_DATA_HOME`):

- `SHA256/`: the unpacked tree of the archive with that SHA-256. Unpacked by `mcp_rootfs.unpack`: no absolute
  path, no `..`, nothing written through a link, no device or FIFO, no set-user-ID, set-group-ID or world-writable
  bit; files and folders read-only. On a file system that ignores case (macOS) an archive with two names that
  differ only in case is refused.
- `installed.json`: `{NAME: {VERSION: manifest + "signed_by" + "added"}}`.

The archive is streamed to a file in the store while it is hashed (never held in memory), checked, unpacked and
deleted. `toolchain remove NAME[@VERSION]` removes the entry and its tree (refused while a running sandbox uses
it). Nothing in the store is writable for a sandbox.

## Versions

The catalog may have several versions of a name (sandbox-ai-mcp: one recipe each, `toolchains/NAME/toolchain.json`
the newest, `toolchains/NAME/LINE/toolchain.json` an older line), and the store keeps them side by side (each its own
tree, `installed.json` by name and version). A value `NAME[@VERSION]` (`toolchains.picked`) is: without `@` the newest
(of the catalog for `add`, of the store at a start, an attach, `show`); `@VERSION` that version, else the newest whose
version starts with VERSION followed by `.`, `+`, `-` or `_` (`jdk@17` -> 17.0.21+9, `python@3.12` -> 3.12.16;
`go@1.2` does not take 1.26.0). `toolchain list` and `catalog` show for each version the shortest value that picks it
(`toolchains.selector`: `jdk` for the newest, `jdk@17` for an older line; JSON `"toolchain"`). A start resolves every
`--toolchain` value first: two that pick two versions of one name are refused (both would be
`/opt/toolchains/NAME`); a toolchain another requires (`rust` requires `cc`) is the version the start picks for it,
else its newest. `attach` of a version other than the one attached is refused until that one is detached. A store
written by an older sandbox-ai, one version per name, is the same format.

## The mount

Every start gets `/opt/toolchains`: the folder `toolchains/` of the run's session directory on the host, mounted
read-only (`nosuid`, `nodev`). sandbox-ai writes into it on the host; the sandbox only reads:

    /opt/toolchains/bin/       one small wrapper per command of the attached toolchains (first on the agent's PATH)
    /opt/toolchains/env.sh     their variables, for a shell to read
    /opt/toolchains/NAME/      the mount point of a toolchain (an empty folder until it is mounted)

- **At the start** (`--toolchain NAME[@VERSION]`): Podman `--mount type=bind,src=STORE/SHA256,dst=/opt/toolchains/NAME,ro,nodev,nosuid`,
  Bubblewrap `--ro-bind STORE/SHA256 /opt/toolchains/NAME` (bwrap's binds are `nosuid`, `nodev`).
- **Into a running sandbox** (`toolchain attach`): nothing runs in the sandbox. sandbox-ai makes the mount point in
  the session's folder, then a helper of your own user - the one `devices add` and `workspace add` use - enters the
  sandbox's user namespace, clones the tree (`open_tree`), makes the clone read-only, `nosuid`, `nodev`
  (`mount_setattr`; Linux 5.12, without it nothing is attached) and moves it onto `/opt/toolchains/NAME` in each of
  the sandbox's own mount namespaces (`move_mount`), the agent's under Firejail included, never one the agent
  made. The tree is checked to be the one of the store (device and inode) and every PID to be the process found
  (its start time). A mount that fails in one namespace is taken out of those it reached. Then the wrappers and
  `env.sh` are written.
- **Detach** (`toolchain detach RUN NAME`): the wrappers go first (no new process starts from it), then the tree
  is unmounted strictly in every namespace; the kernel refuses while a process still runs from it or has a file of
  it open (a build daemon, a language server), and `--force` detaches it anyway (what is open stays usable until
  it is closed). A toolchain given at the start stays for the run.

## PATH and variables: what a running process sees

The agent's processes start with `/opt/toolchains/bin` first on PATH (the supervisor's own processes do not:
they keep the image's programs). A wrapper is three lines of `sh`: the toolchain's variables where you have not
set them yourself, then `exec /opt/toolchains/NAME/bin/go "$@"`.

| who | after `toolchain attach` |
|---|---|
| a process that is running (the agent, its shell, a watch task) | finds the new commands the next time it starts one: PATH is searched at each start of a program, the folder was on PATH already. The toolchain's variables (`JAVA_HOME` ...) are set for the toolchain's own commands by their wrappers; the process's own environment does not change |
| a new `bash -c ...` (what agents run commands with) | also has the variables: `BASH_ENV=/opt/toolchains/env.sh` is in the agent's environment (unless you set `BASH_ENV` yourself) |
| a new login shell under Podman (`bash -l`) | `/etc/profile` resets PATH; `/etc/profile.d/sandbox-ai-toolchains.sh` (mounted read-only) puts the folder back and reads `env.sh` |
| a sandbox started with `--toolchain` | the variables are in the agent's environment from the start |

A toolchain's command comes before the image's or the host's of the same name (`node`, `python3`) for every
process the agent starts from then on; the image's is still there by its path (`/usr/bin/python3`). When two
attached toolchains name the same command, the first keeps it and the attach says so. A sandbox started by a
sandbox-ai before this feature has no `/opt/toolchains`: `attach` says so (restart it with `--toolchain`).

## A display: Qt

The Qt toolchains (`pyside6`, `qt`) set `QT_QPA_PLATFORM=offscreen`: a sandbox has no display, and Qt would end with
"no Qt platform plugin could be initialized". With `--gui` the launcher sets `QT_QPA_PLATFORM=xcb` in the agent's
environment (`--gui-wayland`: `wayland;xcb`; `--env QT_QPA_PLATFORM`: yours) - for every start with a display, not
only one with a Qt toolchain, so that one attached later finds it set (env.sh and the wrappers set a variable only
where none is). Qt's own `QT_PLUGIN_PATH` and `QML_IMPORT_PATH` are not set: each Qt finds its plugins and QML
modules from where its libraries are, and the variables would send every other Qt of the sandbox (a project's own
PySide6 or PyQt6 in a virtual environment) to this one's.

## The network

A toolchain's `hosts` and `allow` are opened as the language presets open theirs: at the start they join
`--allow-host` / `--allow-PRESET`, said in the start's notes and shown in the plan; for a running sandbox `attach`
calls `sandbox-ai net allow` (the supervisor's own check, the run's log). The security modes decide as they do for
those options: in `high` and `paranoid`, which refuse every option that opens a host, the toolchain is mounted and
its registries stay closed - the start and `attach` say which hosts, and each connection is then asked or refused
as the mode does with any other. `--toolchain-offline` (and `attach --offline`) mounts without opening anything in
every mode; `--offline` does too. The package guard and the secret guard hold for these hosts as for all others.

## Security

- **Human-chosen**: only `--toolchain`, a project file, a preset (shown before its start) or `toolchain attach`
  give a sandbox a toolchain; the agent cannot ask for one (the control socket is not reachable from the sandbox).
- **Signed before mounted**: an entry enters the store only with a good catalog signature (or `--unsigned`, said
  at every start); the archive's SHA-256 is checked before the first byte is unpacked.
- **Read-only, `nosuid`, `nodev`**; the tree holds no set-user-ID file and no device; the agent's processes run
  with `no_new_privs` and without capabilities anyway. The agent cannot change a compiler another run will use.
- **Nothing of it runs on the host**: sandbox-ai reads the manifest (JSON, every value checked) and never starts a
  file of the tree. The wrappers and `env.sh` are written from checked values (names, paths below the prefix,
  values quoted for `sh`).
- **Logged**: `toolchains.jsonl` in the run's log directory (attach, detach, refused, the hosts opened), the
  launcher's log, `sandbox-ai report`, the plan (`--dry-run`: `"toolchains"`).
- **The administrator's policy**: `forbid: ["--toolchain"]` refuses it at the start and for running sandboxes.
  Its other rules bind a toolchain's registries as they bind the options: a host outside its ceiling
  (`allow_hosts`), a forbidden `--allow-host` (or one value of it), `--allow-PRESET` or shared wildcard stays
  closed at the start (the toolchain is mounted, the start says which), and `attach` asks `net allow`, which checks
  them too.

Threat model, short. *A toolchain that is malicious or was tampered with*: the signature and the SHA-256 keep a
changed archive out; what a signed toolchain does, it does inside the sandbox, as any program the agent downloads
would - behind the firewall, the proxy and the rules. It is code the agent runs, never code the host runs.
*The agent attacks the toolchain*: read-only mount of a read-only tree; the mount point's folder is the host's.
*The agent attacks the attach*: the helper never follows a link the sandbox made (the mount point is checked to
be the session folder's own, by device and inode), enters only the sandbox's own namespaces, and mounts nothing
but the store's tree. *A registry is hostile*: as for every allowed host - the package guard (malicious packages,
release age, new names), the secret guard, the modes' questions.

## macOS

Podman runs in `podman machine`'s VM; toolchains are Linux programs of the VM's architecture (`aarch64` on Apple
silicon: an entry of that architecture is needed, else `toolchain add` says there is none). `--toolchain` at the
start works as on Linux: the store lies below your HOME, which the VM shares, and the tree is mounted from there.
`toolchain attach` into a running sandbox is not possible there (the sandbox's namespaces are in the VM, out of
the helper's reach): it says so and names the restart (`sandbox-ai resume NAME --restart --toolchain NAME`).
Bubblewrap does not exist on macOS.

## Commands

    sandbox-ai toolchain catalog [--json]            what the signed catalog offers for this machine
    sandbox-ai toolchain add NAME[@VERSION]          download, check, unpack into the store
                 [--accept-android-sdk-license]      android-sdk: built here from Google's downloads; you accept
                                                     their license by the option (else asked on a terminal)
    sandbox-ai toolchain list [--run [RUN]] [--json] the store; which running sandboxes use what (--run: one's)
    sandbox-ai toolchain show NAME[@VERSION]         its manifest: commands, variables, hosts, needs, sources
    sandbox-ai toolchain remove NAME[@VERSION]       out of the store
    sandbox-ai toolchain attach [RUN] NAME[@VERSION] [--offline]     into a running sandbox
    sandbox-ai toolchain detach [RUN] NAME [--force]                 out of it again
    sandbox-ai claude --toolchain go --toolchain jdk@21.0.9          at a start; .sandbox-ai.json: "options": ["--toolchain", "go"]
